"""Tests for live (mid-run) editing of electrolysis experiments.

Covers the primitives that let a running experiment be retuned or switched to a
different method without losing the accumulated charge:

  * ``live_stop_limits`` re-reads charge/time limits, keeping the old ones if a
    live edit would leave the run with no stop criterion.
  * The DC control loops seed their charge/elapsed totals from a handoff and
    return ``OUTCOME_METHOD_CHANGE`` when the UI requests a switch.
  * ``ElectrolysisWorker`` runs successive methods on one open port and one CSV,
    carrying the coulomb count across a Constant Current -> Constant Potential
    switch so the charge column stays continuous.

Everything runs against the mock potentiostat, so no hardware is required.
"""

import threading
import time
from collections import deque

import pytest

from moles.driver.mock import MockPotentiostat
from moles.methods import constant_current as cc
from moles.methods import constant_potential as cp
from moles.methods._common import (
    LiveControl,
    OUTCOME_METHOD_CHANGE,
    live_stop_limits,
)


def _mock_ps():
    ps = MockPotentiostat(serial_port="MOCK_T")
    ps.connect()
    return ps


class _SwitchAfter:
    """Output queue that requests a method switch once ``n`` points arrive.

    Lets the switch fire deterministically from inside the control loop instead
    of racing a background thread.
    """

    def __init__(self, control, n):
        self._control = control
        self._n = n
        self.items = []

    def put(self, item, *args, **kwargs):
        self.items.append(item)
        if len(self.items) == self._n:
            self._control.request_switch()

    put_nowait = put


# --------------------------------------------------------------------------
# live_stop_limits
# --------------------------------------------------------------------------

def test_live_stop_limits_reads_new_values():
    assert live_stop_limits({"total_charge_C": 12.0}, 5.0, None) == (12.0, None)
    assert live_stop_limits(
        {"total_charge_C": 3.0, "total_time_s": 60.0}, 5.0, None
    ) == (3.0, 60.0)


def test_live_stop_limits_keeps_previous_when_both_blank():
    # Clearing both fields on a running experiment must not create a run that
    # can never stop — the previous limits are retained.
    assert live_stop_limits({"total_charge_C": 0, "total_time_s": 0}, 5.0, 90.0) == (5.0, 90.0)
    assert live_stop_limits({}, 7.0, None) == (7.0, None)


def test_live_stop_limits_ignores_unparseable():
    assert live_stop_limits({"total_charge_C": "oops"}, 4.0, None) == (4.0, None)


# --------------------------------------------------------------------------
# LiveControl
# --------------------------------------------------------------------------

def test_livecontrol_switch_and_carry():
    lc = LiveControl()
    assert lc.switch_requested() is False
    lc.request_switch()
    assert lc.switch_requested() is True
    lc.clear_switch()
    assert lc.switch_requested() is False
    lc.carry(4.5, 12.0)
    assert lc.carried_charge_C == 4.5
    assert lc.carried_elapsed_s == 12.0


# --------------------------------------------------------------------------
# DC loops: seeding + switch handoff
# --------------------------------------------------------------------------

def test_cc_seeds_charge_and_reports_switch(monkeypatch):
    monkeypatch.setattr(cc, "MEASUREMENT_INTERVAL_S", 0.005)
    ps = _mock_ps()
    control = LiveControl()
    q = _SwitchAfter(control, n=5)
    params = {
        "method": "constant_current",
        "current_mA": 2.0,
        "initial_current_mA": 0.1,
        "total_charge_C": 1e6,   # effectively never reached on its own
        "total_time_s": None,
    }
    outcome = cc.run_constant_current(
        ps, params, q, "T", threading.Event(),
        control=control, initial_charge_C=3.0, initial_elapsed_s=8.0,
    )
    assert outcome == OUTCOME_METHOD_CHANGE
    # Charge continued from the seed, not from zero.
    assert q.items[0][3] >= 3.0
    # Elapsed continued from the seed too.
    assert q.items[0][0] >= 8.0
    # The worker is handed the running totals at the point of switch.
    assert control.carried_charge_C == pytest.approx(q.items[-1][3])
    assert control.carried_charge_C > 3.0


def test_cp_seeds_charge_and_reports_switch(monkeypatch):
    monkeypatch.setattr(cp, "MEASUREMENT_INTERVAL_S", 0.005)
    ps = _mock_ps()
    control = LiveControl()
    q = _SwitchAfter(control, n=5)
    params = {
        "method": "constant_potential",
        "potential_V": 0.5,
        "total_charge_C": 1e6,
        "total_time_s": None,
    }
    outcome = cp.run_constant_potential(
        ps, params, q, "T", threading.Event(),
        control=control, initial_charge_C=2.0,
    )
    assert outcome == OUTCOME_METHOD_CHANGE
    assert q.items[0][3] >= 2.0
    assert control.carried_charge_C > 2.0


def test_cc_live_current_change_is_applied(monkeypatch):
    """Mutating params['current_mA'] mid-run retunes the commanded current."""
    monkeypatch.setattr(cc, "MEASUREMENT_INTERVAL_S", 0.005)
    ps = _mock_ps()
    stop = threading.Event()
    params = {
        "method": "constant_current",
        "current_mA": 1.0,
        "initial_current_mA": 0.1,
        "total_charge_C": 1e6,
        "total_time_s": None,
    }
    seen = []

    class Q:
        def put(self, item, *a, **k):
            seen.append(item)
            if len(seen) == 3:
                params["current_mA"] = 6.0   # live edit: raise the target
            if len(seen) >= 40:
                stop.set()

        put_nowait = put

    cc.run_constant_current(ps, params, Q(), "T", stop)
    # The commanded current ramps toward the new 6 mA target; by the end the
    # mock's measured current should have climbed well above the original 1 mA.
    assert seen[-1][1] > 3.0


# --------------------------------------------------------------------------
# Worker-level integration: CC -> CP switch keeps charge continuous
# --------------------------------------------------------------------------

def _read_csv_charges(path):
    """Return (pre, post, saw_switch) charge columns split at the switch comment.

    ``pre`` are the charge values written before the '# method change' line,
    ``post`` those after, so the two methods' segments can be checked for
    continuity independently of the (differing) decimal rounding each uses.
    """
    pre, post = [], []
    saw_switch = False
    with open(path) as f:
        for line in f:
            s = line.strip()
            if s.startswith("#"):
                if "method change" in s:
                    saw_switch = True
                continue
            if not s or s.startswith("Time"):
                continue
            parts = s.split(",")
            if len(parts) >= 4:
                (post if saw_switch else pre).append(float(parts[3]))
    return pre, post, saw_switch


def test_worker_cc_to_cp_switch_is_continuous(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "MEASUREMENT_INTERVAL_S", 0.005)
    monkeypatch.setattr(cp, "MEASUREMENT_INTERVAL_S", 0.005)
    import moles.data_io as data_io
    monkeypatch.setattr(data_io, "get_data_folder", lambda: tmp_path)

    from moles.ui.electrolysis.workers import ElectrolysisSignals, ElectrolysisWorker

    signals = ElectrolysisSignals()
    finished = {}
    statuses = []
    # Emitter and receiver both live on this (main) thread -> direct, synchronous
    # slot calls, so no Qt event loop is needed while worker.run() executes here.
    signals.finished.connect(lambda p, s, m, fp: finished.update(success=s, msg=m, fp=fp))
    signals.status_updated.connect(lambda p, m: statuses.append(m))

    buf = deque()
    ps = _mock_ps()
    params = {
        "method": "constant_current",
        "experiment_name": "switch_test",
        "mmol": 0.1, "electrons_per_mol": 2.0,
        "current_mA": 5.0, "initial_current_mA": 0.1,
        "total_charge_C": 1e6, "total_time_s": None,
    }
    worker = ElectrolysisWorker("T", ps, params, signals, buf, claim_key=None, port="")

    cp_params = {
        "method": "constant_potential",
        "experiment_name": "switch_test",
        "mmol": 0.1, "electrons_per_mol": 2.0,
        "potential_V": 0.5,
        "total_charge_C": 1e6, "total_time_s": None,
    }

    # Switch only once a clearly-nonzero amount of charge has built up, so a
    # hypothetical reset would be caught rather than hidden by decimal rounding.
    SWITCH_AT_C = 0.02

    def _max_charge():
        return max((pt[3] for pt in buf), default=0.0)

    def controller():
        deadline = time.time() + 25
        while _max_charge() < SWITCH_AT_C and time.time() < deadline:
            time.sleep(0.02)
        worker.request_switch(cp_params)
        # Wait for the switch to take effect, then for some CP data, then stop.
        while not any("Switching" in s for s in statuses) and time.time() < deadline:
            time.sleep(0.02)
        n = len(buf)
        while len(buf) < n + 5 and time.time() < deadline:
            time.sleep(0.02)
        worker.stop()

    ctl = threading.Thread(target=controller)
    ctl.start()
    worker.run()          # blocks on this thread until stopped
    ctl.join(timeout=25)

    assert finished.get("success") is True
    assert finished.get("msg") == "Stopped"
    assert any("Switching" in s for s in statuses)

    pre, post, saw_switch = _read_csv_charges(finished["fp"])
    assert saw_switch, "expected a '# method change' provenance comment in the CSV"
    assert pre and post, "both the CC and CP segments should have written data"
    # Each segment integrates charge monotonically (tolerance covers rounding).
    for seg in (pre, post):
        for a, b in zip(seg, seg[1:]):
            assert b >= a - 2e-3
    # The CP segment resumed from where CC left off — the coulomb count carried
    # across the switch instead of resetting to zero.
    assert pre[-1] >= SWITCH_AT_C - 2e-3
    assert post[0] >= pre[-1] - 2e-3
    assert post[-1] >= pre[-1] - 2e-3

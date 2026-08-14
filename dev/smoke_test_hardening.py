"""Console smoke test for the stack-hardening changes (no GUI required).

Exercises, against MockPotentiostat:
  - normal completion and the structured OUTCOME_* return values
  - serial-timeout tolerance (transient faults are skipped, runs survive)
  - comm-failure abort after MAX_CONSECUTIVE_READ_FAILURES
  - user stop -> OUTCOME_STOPPED
  - alternating polarity batch path incl. one injected chunk failure
  - shutdown_hardware attempting later steps after an earlier one fails
  - the ElectrolysisWorker end-to-end path with the flushing CSV writer

Run:  python dev/smoke_test_hardening.py
"""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import moles.data_io as data_io
import moles.methods.alternating_current as ac
import moles.methods.alternating_polarity as ap
import moles.methods.constant_current as cc
import moles.methods.constant_potential as cp
from moles.driver.mock import MockPotentiostat
from moles.methods._common import (
    OUTCOME_COMM_FAILURE,
    OUTCOME_COMPLETED,
    OUTCOME_STOPPED,
    shutdown_hardware,
)

# Speed the 1 Hz loops up for the test.
cc.MEASUREMENT_INTERVAL_S = 0.01
cp.MEASUREMENT_INTERVAL_S = 0.01

PASS = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    PASS.append(bool(condition))


def make_ps():
    ps = MockPotentiostat(serial_port="SMOKE")
    ps.connect()
    return ps


class ListQueue:
    def __init__(self):
        self.items = []

    def put(self, item, block=True, timeout=None):
        self.items.append(item)


def test_cc_completes():
    print("\nCC: normal completion")
    ps, q = make_ps(), ListQueue()
    params = {"current_mA": 5.0, "total_charge_C": 0.001, "initial_current_mA": 0.1}
    outcome = cc.run_constant_current(ps, params, q, "T1", threading.Event())
    check("outcome == completed", outcome == OUTCOME_COMPLETED, repr(outcome))
    check("data streamed", len(q.items) > 0, f"{len(q.items)} points")
    check("switch opened after run", ps._switch_state is False)


def test_cc_tolerates_transient_timeouts():
    print("\nCC: transient serial timeouts are tolerated")
    ps, q = make_ps(), ListQueue()
    ps.trigger_serial_timeout(3)  # fewer than MAX_CONSECUTIVE_READ_FAILURES
    params = {"current_mA": 5.0, "total_charge_C": 0.001, "initial_current_mA": 0.1}
    outcome = cc.run_constant_current(ps, params, q, "T2", threading.Event())
    check("outcome == completed", outcome == OUTCOME_COMPLETED, repr(outcome))


def test_cc_aborts_on_persistent_failure():
    print("\nCC: persistent failure aborts with comm_failure (not 'Completed')")
    ps, q = make_ps(), ListQueue()
    ps.trigger_serial_timeout(10**6)
    params = {"current_mA": 5.0, "total_charge_C": 100.0, "initial_current_mA": 0.1}
    outcome = cc.run_constant_current(ps, params, q, "T3", threading.Event())
    check("outcome == comm_failure", outcome == OUTCOME_COMM_FAILURE, repr(outcome))
    check("switch opened after abort", ps._switch_state is False)


def test_cp_stop():
    print("\nCP: user stop -> stopped outcome")
    ps, q = make_ps(), ListQueue()
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    params = {"potential_V": 1.0, "total_charge_C": 100.0}
    outcome = cp.run_constant_potential(ps, params, q, "T4", stop)
    check("outcome == stopped", outcome == OUTCOME_STOPPED, repr(outcome))
    check("switch opened after stop", ps._switch_state is False)


def test_ac_completes_and_tolerates_timeouts():
    print("\nAC: completes, tolerating transient timeouts mid-waveform")
    ps, q = make_ps(), ListQueue()
    params = {
        "ac_pos_peak_mA": 5.0, "ac_neg_peak_mA": -5.0, "ac_frequency_hz": 1.0,
        "ac_duty_cycle": 0.5, "ac_waveform": "Square", "total_charge_C": 0.001,
    }
    ps.trigger_serial_timeout(3)
    outcome = ac.run_alternating_current(ps, params, q, "T5a", threading.Event())
    check("outcome == completed", outcome == OUTCOME_COMPLETED, repr(outcome))
    check("data streamed", len(q.items) > 0, f"{len(q.items)} points")
    check("switch opened after run", ps._switch_state is False)


def test_ap_with_chunk_retry():
    print("\nAP: batch path completes, surviving one injected chunk failure")
    ps, q = make_ps(), ListQueue()
    params = {
        "ap_pos_peak_V": 1.0, "ap_neg_peak_V": -1.0, "ap_frequency_hz": 10.0,
        "ap_duty_cycle": 0.5, "ap_waveform": "Square", "ap_sample_hz": 250,
        "total_charge_C": 0.005,
    }
    ps.trigger_serial_timeout(1)  # first chunk fails, retry must succeed
    outcome = ap.run_alternating_polarity(ps, params, q, "T5", threading.Event())
    check("outcome == completed", outcome == OUTCOME_COMPLETED, repr(outcome))
    check("data streamed", len(q.items) > 0, f"{len(q.items)} points")
    check("switch opened after run", ps._switch_state is False)


def test_shutdown_hardware_runs_all_steps():
    print("\nshutdown_hardware: later steps run even when an earlier one fails")
    calls = []

    def boom():
        raise RuntimeError("zeroing failed")

    ok = shutdown_hardware(object(), "T6", [
        ("zero output", boom),
        ("open switch", lambda: calls.append("switch")),
    ])
    check("switch step still attempted", calls == ["switch"])
    check("failure reported", ok is False)


def test_worker_end_to_end():
    print("\nElectrolysisWorker: end-to-end with flushing CSV writer")
    from collections import deque
    from moles.ui.electrolysis.workers import ElectrolysisWorker

    tmp = Path(tempfile.mkdtemp())
    data_io.DATA_FOLDER = tmp  # keep smoke-test CSVs out of Dropbox

    results = []

    class FakeSignals:
        class _Sig:
            def __init__(self, sink=None):
                self.sink = sink

            def emit(self, *args):
                if self.sink is not None:
                    self.sink.append(args)

        def __init__(self):
            self.status_updated = self._Sig()
            self.finished = self._Sig(results)

    ps = make_ps()
    buf = deque(maxlen=20000)
    params = {"method": "constant_current", "current_mA": 5.0,
              "total_charge_C": 0.001, "initial_current_mA": 0.1,
              "experiment_name": "smoke", "mmol": 0.1}
    worker = ElectrolysisWorker("T7", ps, params, FakeSignals(), buf)
    worker.run()

    check("finished emitted once", len(results) == 1)
    pid, success, msg, path = results[0]
    check("reported success/Completed", success is True and msg == "Completed", f"{success}, {msg}")
    csv_lines = Path(path).read_text().strip().splitlines()
    check("CSV written and flushed", len(csv_lines) > 3, f"{len(csv_lines)} lines")
    check("data buffer filled", len(buf) > 0, f"{len(buf)} points")

    # Unknown method must still emit finished (used to leak the worker slot).
    results.clear()
    worker2 = ElectrolysisWorker("T8", make_ps(), {"method": "nope"}, FakeSignals(), deque())
    worker2.run()
    check("unknown method emits finished(False)", len(results) == 1 and results[0][1] is False)


if __name__ == "__main__":
    test_cc_completes()
    test_cc_tolerates_transient_timeouts()
    test_cc_aborts_on_persistent_failure()
    test_cp_stop()
    test_ac_completes_and_tolerates_timeouts()
    test_ap_with_chunk_retry()
    test_shutdown_hardware_runs_all_steps()
    test_worker_end_to_end()

    total, passed = len(PASS), sum(PASS)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)

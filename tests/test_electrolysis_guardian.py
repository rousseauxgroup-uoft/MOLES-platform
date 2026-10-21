"""Tests for the non-Qt guardian safety thread.

The guardian exists because the 2026-09-17 freeze took out every GUI-thread
intervention at once (Stop Checked, the hung-worker watchdog, closeEvent) while
the experiments kept driving current. These tests pin the three things it must
keep doing with the event loop dead: honour the emergency stop file, dump
thread stacks when the GUI heartbeat goes stale, and force-close the port of a
worker hung after a stop request.

Timing-dependent tests shrink the guardian's own timeouts rather than mocking
the clock, so the real deadline arithmetic is exercised.

The window-level tests at the bottom check the wiring only — that MainWindow
starts a guardian over its live registries and routes the results back onto the
GUI thread.
"""

import threading
import time
from pathlib import Path

import pytest

from moles.ui.electrolysis.guardian import Guardian


class FakeWorker:
    """Stand-in for ElectrolysisWorker: only ``stop()`` matters here."""

    def __init__(self, raises=False):
        self.stop_calls = 0
        self._raises = raises

    def stop(self):
        self.stop_calls += 1
        if self._raises:
            raise RuntimeError("serial port gone")


class FakePotentiostat:
    def __init__(self, raises=False):
        self.force_close_calls = 0
        self._raises = raises

    def force_close(self):
        self.force_close_calls += 1
        if self._raises:
            raise OSError("handle already invalid")


class Recorder:
    """Collects the guardian's UI callbacks in call order."""

    def __init__(self):
        self.status = []
        self.reaped = []
        self.emergency_stop = []

    def as_kwargs(self):
        return {
            "on_status": lambda pid, text: self.status.append((pid, text)),
            "on_reaped": self.reaped.append,
            "on_emergency_stop": self.emergency_stop.append,
        }


@pytest.fixture
def paths(tmp_path):
    """Keep every guardian file under the test's temp dir, never ~/.moles."""
    return {
        "stop_file": tmp_path / "STOP_ALL",
        "dump_file": tmp_path / "stalls.log",
    }


def _guardian(paths, workers=None, potentiostats=None, recorder=None, **kwargs):
    rec = recorder if recorder is not None else Recorder()
    return Guardian(
        workers if workers is not None else {},
        potentiostats if potentiostats is not None else {},
        stop_file=paths["stop_file"],
        dump_file=paths["dump_file"],
        **rec.as_kwargs(),
        **kwargs,
    )


# --------------------------------------------------------------------------
# 1. Sentinel-file emergency stop
# --------------------------------------------------------------------------

def test_stop_file_stops_every_running_worker(paths):
    workers = {"A": FakeWorker(), "B": FakeWorker(), "C": FakeWorker()}
    rec = Recorder()
    g = _guardian(paths, workers=workers, recorder=rec)

    paths["stop_file"].touch()
    g.poll_once()

    assert [w.stop_calls for w in workers.values()] == [1, 1, 1]
    assert rec.emergency_stop == [3]


def test_stop_file_is_removed_after_acting(paths):
    g = _guardian(paths, workers={"A": FakeWorker()})
    paths["stop_file"].touch()
    g.poll_once()
    assert not paths["stop_file"].exists()


def test_stop_file_arms_the_hang_escalation(paths):
    """A worker hung on a dead port won't honour stop(); the port close must."""
    workers = {"A": FakeWorker(), "B": FakeWorker()}
    g = _guardian(paths, workers=workers)

    paths["stop_file"].touch()
    g.poll_once()

    assert g.armed_channels() == {"A", "B"}


def test_no_stop_file_leaves_workers_alone(paths):
    workers = {"A": FakeWorker()}
    rec = Recorder()
    g = _guardian(paths, workers=workers, recorder=rec)

    g.poll_once()

    assert workers["A"].stop_calls == 0
    assert rec.emergency_stop == []
    assert g.armed_channels() == set()


def test_stop_file_with_nothing_running_is_still_consumed(paths):
    rec = Recorder()
    g = _guardian(paths, recorder=rec)

    paths["stop_file"].touch()
    g.poll_once()

    assert rec.emergency_stop == [0]
    assert not paths["stop_file"].exists()


def test_one_failing_worker_does_not_block_the_others(paths):
    workers = {"A": FakeWorker(raises=True), "B": FakeWorker()}
    rec = Recorder()
    g = _guardian(paths, workers=workers, recorder=rec)

    paths["stop_file"].touch()
    g.poll_once()

    assert workers["B"].stop_calls == 1
    assert rec.emergency_stop == [1]


def test_stop_file_reports_each_channel_to_the_ui(paths):
    rec = Recorder()
    g = _guardian(paths, workers={"A": FakeWorker()}, recorder=rec)

    paths["stop_file"].touch()
    g.poll_once()

    assert [pid for pid, _ in rec.status] == ["A"]
    assert "EMERGENCY STOP" in rec.status[0][1]


# --------------------------------------------------------------------------
# 2. GUI heartbeat watchdog
# --------------------------------------------------------------------------

def test_heartbeat_is_not_watched_until_the_event_loop_proves_it_runs(paths):
    """A slow startup must not be reported as a stall."""
    g = _guardian(paths, heartbeat_stale_s=0.0)

    g.poll_once()

    assert not paths["dump_file"].exists()


def test_fresh_heartbeat_produces_no_dump(paths):
    g = _guardian(paths, heartbeat_stale_s=30.0)
    g.beat()

    g.poll_once()

    assert not paths["dump_file"].exists()


def test_stale_heartbeat_dumps_every_thread_stack(paths):
    parked = threading.Event()
    done = threading.Event()

    def a_parked_worker_thread():
        parked.set()
        done.wait(5.0)

    t = threading.Thread(target=a_parked_worker_thread, daemon=True)
    t.start()
    parked.wait(5.0)

    g = _guardian(paths, heartbeat_stale_s=0.05)
    g.beat()
    time.sleep(0.1)
    try:
        g.poll_once()
    finally:
        done.set()
        t.join(5.0)

    text = paths["dump_file"].read_text()
    assert "GUI stalled" in text
    # The dumping thread's own stack, and an unrelated thread's — proof that
    # this captures every thread, not just the one that noticed.
    assert "poll_once" in text
    assert "a_parked_worker_thread" in text


def test_dumps_are_rate_limited_during_one_long_stall(paths):
    g = _guardian(paths, heartbeat_stale_s=0.05, dump_interval_s=30.0)
    g.beat()
    time.sleep(0.1)

    g.poll_once()
    first = paths["dump_file"].read_text()
    g.poll_once()
    g.poll_once()

    assert paths["dump_file"].read_text() == first


def test_a_new_stall_dumps_again_after_the_gui_recovers(paths):
    g = _guardian(paths, heartbeat_stale_s=0.05, dump_interval_s=0.0)
    g.beat()
    time.sleep(0.1)
    g.poll_once()
    first_len = len(paths["dump_file"].read_text())

    # Event loop comes back, then stalls a second time.
    g.beat()
    g.poll_once()
    time.sleep(0.1)
    g.poll_once()

    assert len(paths["dump_file"].read_text()) > first_len


def test_dump_file_is_created_with_its_parent_directory(paths, tmp_path):
    nested = tmp_path / "logs" / "deeper" / "stalls.log"
    g = Guardian({}, {}, stop_file=paths["stop_file"], dump_file=nested,
                 heartbeat_stale_s=0.05)
    g.beat()
    time.sleep(0.1)
    g.poll_once()

    assert nested.exists()


# --------------------------------------------------------------------------
# 3. Hung-worker escalation
# --------------------------------------------------------------------------

def test_escalation_does_not_fire_before_the_timeout(paths):
    ps = FakePotentiostat()
    g = _guardian(paths, workers={"A": FakeWorker()}, potentiostats={"A": ps},
                  stop_timeout_s=60.0)

    g.arm_stop("A")
    g.poll_once()

    assert ps.force_close_calls == 0
    assert g.armed_channels() == {"A"}


def test_hung_worker_has_its_port_force_closed(paths):
    ps = FakePotentiostat()
    rec = Recorder()
    g = _guardian(paths, workers={"A": FakeWorker()}, potentiostats={"A": ps},
                  recorder=rec, stop_timeout_s=0.1, reap_timeout_s=60.0)

    g.arm_stop("A")
    time.sleep(0.15)
    g.poll_once()

    assert ps.force_close_calls == 1
    assert any("hung" in text for _, text in rec.status)
    # Still watched: the port close is meant to unblock the worker, and it gets
    # the reap window to report finished before its slot is taken away.
    assert g.armed_channels() == {"A"}
    assert rec.reaped == []


def test_worker_still_hung_after_the_port_close_is_reaped(paths):
    ps = FakePotentiostat()
    rec = Recorder()
    g = _guardian(paths, workers={"A": FakeWorker()}, potentiostats={"A": ps},
                  recorder=rec, stop_timeout_s=0.1, reap_timeout_s=0.1)

    g.arm_stop("A")
    time.sleep(0.15)
    g.poll_once()
    time.sleep(0.15)
    g.poll_once()

    assert rec.reaped == ["A"]
    assert g.armed_channels() == set()
    # The port is closed once, not once per poll.
    assert ps.force_close_calls == 1


def test_port_is_not_closed_twice_while_waiting_to_reap(paths):
    ps = FakePotentiostat()
    g = _guardian(paths, workers={"A": FakeWorker()}, potentiostats={"A": ps},
                  stop_timeout_s=0.1, reap_timeout_s=60.0)

    g.arm_stop("A")
    time.sleep(0.15)
    g.poll_once()
    g.poll_once()
    g.poll_once()

    assert ps.force_close_calls == 1


def test_worker_that_exits_normally_is_dropped_without_a_port_close(paths):
    ps = FakePotentiostat()
    workers = {"A": FakeWorker()}
    g = _guardian(paths, workers=workers, potentiostats={"A": ps},
                  stop_timeout_s=0.0)

    g.arm_stop("A")
    del workers["A"]          # what on_process_finished does
    g.poll_once()

    assert ps.force_close_calls == 0
    assert g.armed_channels() == set()


def test_disarm_cancels_a_pending_escalation(paths):
    ps = FakePotentiostat()
    g = _guardian(paths, workers={"A": FakeWorker()}, potentiostats={"A": ps},
                  stop_timeout_s=0.0)

    g.arm_stop("A")
    g.disarm_stop("A")
    g.poll_once()

    assert ps.force_close_calls == 0


def test_a_failing_port_close_still_escalates_to_a_reap(paths):
    """A board whose handle is already dead must not strand its UI row."""
    ps = FakePotentiostat(raises=True)
    rec = Recorder()
    g = _guardian(paths, workers={"A": FakeWorker()}, potentiostats={"A": ps},
                  recorder=rec, stop_timeout_s=0.0, reap_timeout_s=0.0)

    g.arm_stop("A")
    g.poll_once()
    g.poll_once()

    assert rec.reaped == ["A"]


def test_missing_potentiostat_does_not_break_the_escalation(paths):
    rec = Recorder()
    g = _guardian(paths, workers={"A": FakeWorker()}, potentiostats={},
                  recorder=rec, stop_timeout_s=0.0, reap_timeout_s=0.0)

    g.arm_stop("A")
    g.poll_once()
    g.poll_once()

    assert rec.reaped == ["A"]


# --------------------------------------------------------------------------
# Robustness: the guardian must outlive anything it is watching
# --------------------------------------------------------------------------

def test_a_broken_ui_callback_does_not_stop_the_guardian(paths):
    """The callbacks end up in Qt; a dying event loop must not silence this."""
    def explode(*args):
        raise RuntimeError("event loop is gone")

    ps = FakePotentiostat()
    g = Guardian({"A": FakeWorker()}, {"A": ps},
                 on_status=explode, on_reaped=explode, on_emergency_stop=explode,
                 stop_file=paths["stop_file"], dump_file=paths["dump_file"],
                 stop_timeout_s=0.0, reap_timeout_s=0.0)

    g.arm_stop("A")
    g.poll_once()
    g.poll_once()

    assert ps.force_close_calls == 1
    assert g.armed_channels() == set()


def test_guardian_imports_no_qt():
    """Importing Qt here would put the safety net back on the event loop."""
    import moles.ui.electrolysis.guardian as guardian_module

    source = Path(guardian_module.__file__).read_text()
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "PyQt5" not in code
    assert "QtCore" not in code


def test_guardian_runs_as_a_daemon_thread(paths):
    g = _guardian(paths)
    assert g.daemon is True


def test_running_thread_acts_on_the_stop_file_and_shuts_down(paths):
    workers = {"A": FakeWorker()}
    g = _guardian(paths, workers=workers, poll_interval_s=0.02)
    g.start()
    try:
        paths["stop_file"].touch()
        deadline = time.monotonic() + 5.0
        while workers["A"].stop_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert workers["A"].stop_calls == 1
    finally:
        g.shutdown()
        g.join(5.0)
    assert not g.is_alive()


def test_a_failing_poll_does_not_kill_the_thread(paths):
    """A guardian that dies on a surprise is worse than none — the window
    still reports one as running."""
    calls = []

    def flaky_poll():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")

    g = _guardian(paths, poll_interval_s=0.02)
    g.poll_once = flaky_poll
    g.start()
    try:
        deadline = time.monotonic() + 5.0
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) >= 3
        assert g.is_alive()
    finally:
        g.shutdown()
        g.join(5.0)


# --------------------------------------------------------------------------
# Window wiring
# --------------------------------------------------------------------------

@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    """A test-mode electrolysis window whose guardian writes only to tmp_path."""
    import moles.ui.electrolysis.guardian as guardian_module
    import moles.ui.electrolysis.main_window as mw

    monkeypatch.setattr(guardian_module, "STOP_FILE", tmp_path / "STOP_ALL")
    monkeypatch.setattr(guardian_module, "LOG_DIR", tmp_path)
    # Don't let a registry on the developer's machine put real boards in the table.
    monkeypatch.setattr(mw, "get_potentiostat_ports", lambda: {})

    win = mw.MainWindow()
    win.test_mode_cb.setChecked(True)   # re-lists the table with mock boards
    yield win
    win.guardian.shutdown()
    win.guardian.join(5.0)


def test_window_starts_a_guardian_over_its_own_registries(window, tmp_path):
    assert window.guardian.is_alive()
    assert window.guardian.stop_file == tmp_path / "STOP_ALL"
    # The guardian must see later additions, so it has to hold the live dicts.
    window.workers["TEST_A"] = FakeWorker()
    assert "TEST_A" in window.guardian._workers


def test_stop_checked_arms_the_guardian(window):
    from PyQt5 import QtCore

    window.workers["TEST_A"] = FakeWorker()
    window._set_all_checks(QtCore.Qt.Checked)
    window.stop_checked()

    assert window.workers["TEST_A"].stop_calls == 1
    assert window.guardian.armed_channels() == {"TEST_A"}


def test_finished_run_disarms_the_guardian(window):
    window.guardian.arm_stop("TEST_A")
    window.on_process_finished("TEST_A", True, "Stopped", "")
    assert window.guardian.armed_channels() == set()


def test_reaped_worker_frees_its_row_and_slot(window):
    from moles.ui.electrolysis.table import COL_STATUS

    window.workers["TEST_A"] = FakeWorker()
    row = window._find_row_for_pid("TEST_A")
    window._set_row_locked(row, True)

    window._on_worker_reaped("TEST_A")

    assert "TEST_A" not in window.workers
    assert "reconnect" in window.ps_table.item(row, COL_STATUS).text().lower()


def test_emergency_stop_is_reported_in_the_status_bar(window):
    window._on_emergency_stop(2)
    assert "EMERGENCY STOP" in window.status_label.text()


def test_close_shuts_the_guardian_down(window):
    from PyQt5 import QtGui

    window.closeEvent(QtGui.QCloseEvent())
    window.guardian.join(5.0)
    assert not window.guardian.is_alive()

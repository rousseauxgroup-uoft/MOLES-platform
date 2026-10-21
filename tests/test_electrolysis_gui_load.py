"""Regression tests for the electrolysis GUI's data-intake path.

These cover the failure seen on 2026-09-17, where twelve channels running
together starved the Qt event loop for 41 minutes: the window stopped
repainting while the experiments themselves kept running correctly. Two
properties matter, and neither is visible from a screenshot:

* no single GUI timer tick may be unbounded, whatever the backlog;
* the work per repaint must not grow with how long a run has been going.

A third test covers the other half of that incident — experiments handed to a
full thread pool that silently queued instead of starting.
"""

import pytest

from moles.ui.electrolysis import main_window as mw


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    """A MainWindow with three mock boards and its timers stopped.

    The timers are stopped so each test drives the drain/repaint steps itself
    rather than racing a live event loop, and the guardian's files are kept
    out of the developer's real ~/.moles.
    """
    import moles.ui.electrolysis.guardian as guardian_module

    monkeypatch.setattr(guardian_module, "STOP_FILE", tmp_path / "STOP_ALL")
    monkeypatch.setattr(guardian_module, "LOG_DIR", tmp_path)
    monkeypatch.setattr(mw, "get_potentiostat_ports", lambda: {})

    win = mw.MainWindow()
    win.test_mode_cb.setChecked(True)
    win.auto_connect()
    win._drain_timer.stop()
    win._plot_timer.stop()
    win._heartbeat_timer.stop()
    win._claims_timer.stop()
    yield win
    win.guardian.shutdown()
    win.guardian.join(5.0)


def _arm_channel(win, pid, is_ac=False):
    """Give a channel the buffers and curves that _start_row would create."""
    from collections import deque

    chart = win.plots[pid]
    plot = chart['plot']
    plot.clear()
    chart['is_ac'] = is_ac
    chart['i_curve'] = plot.plot()
    chart['v_curve'] = plot.plot()
    win.plot_data[pid] = {
        't': deque(maxlen=mw.DISPLAY_BUFFER_SIZE),
        'i': deque(maxlen=mw.DISPLAY_BUFFER_SIZE),
        'v': deque(maxlen=mw.DISPLAY_BUFFER_SIZE),
        'history_t': [], 'history_i': [], 'history_v': [], 'history_counter': 0,
    }
    win._data_buffers[pid] = deque(maxlen=mw.DATA_BUFFER_MAXLEN)
    return win._data_buffers[pid]


def test_drain_tick_is_bounded_by_backlog(window):
    """One tick takes a bounded slice of a backlog and leaves the rest.

    The ceilings here are deliberately literals rather than the module
    constants: a test that reads the constant it is checking would keep
    passing if that constant were raised back to unbounded.
    """
    pid = next(iter(window.potentiostats))
    buf = _arm_channel(window, pid)
    # Below DATA_BUFFER_MAXLEN, so the backlog under test is the whole of what
    # was queued rather than whatever the bounded deque kept.
    for n in range(10_000):
        buf.append((float(n), 1.0, 0.5, 0.001 * n))
    backlog = len(buf)
    assert backlog == 10_000, "buffer dropped points before the drain ran"

    window._drain_data_buffers()
    taken = backlog - len(buf)
    assert taken <= 5_000, f"one tick consumed {taken} points"
    assert len(buf) > 0, "a single tick drained the whole backlog"

    # Successive ticks work it down rather than extending any single tick.
    for _ in range(200):
        window._drain_data_buffers()
        if not buf:
            break
    assert not buf


def test_draining_defers_the_repaint(window):
    """Drain only marks the channel dirty; curves are drawn on the plot timer."""
    pid = next(iter(window.potentiostats))
    buf = _arm_channel(window, pid)
    for n in range(10):
        buf.append((float(n), 1.0, 0.5, 0.001 * n))

    window._drain_data_buffers()
    assert pid in window._dirty_plots
    assert window.plots[pid]['i_curve'].getData()[0] is None

    window._refresh_dirty_plots()
    assert pid not in window._dirty_plots
    assert len(window.plots[pid]['i_curve'].getData()[0]) == 10


def test_repaint_spreads_over_ticks(window):
    """A refresh tick services at most PLOTS_PER_REFRESH channels."""
    pids = list(window.potentiostats)
    for pid in pids:
        buf = _arm_channel(window, pid)
        buf.append((1.0, 1.0, 0.5, 0.001))
    window._drain_data_buffers()
    assert len(window._dirty_plots) == len(pids)

    window._plot_timer.stop()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mw, "PLOTS_PER_REFRESH", 1)
        window._refresh_dirty_plots()
    assert len(window._dirty_plots) == len(pids) - 1


def _drawn_points(window, pid, n_samples, is_ac=False):
    """Feed a channel n_samples, let the GUI catch up, return points drawn."""
    buf = _arm_channel(window, pid, is_ac=is_ac)
    for n in range(n_samples):
        buf.append((float(n), 1.0, 0.5, 0.001 * n))
    for _ in range(500):
        window._drain_data_buffers()
        if not buf:
            break
    assert not buf, "test needs the whole backlog drained"
    window._refresh_dirty_plots()
    return len(window.plots[pid]['i_curve'].getData()[0])


def test_curve_cost_does_not_scale_with_run_length(window):
    """Five times the samples must not mean five times the points drawn.

    The old code handed pyqtgraph history-plus-buffer arrays that grew with
    elapsed time, so a five-hour run cost several times its own first hour on
    every repaint. Ceilings are literals on purpose (see the note above).
    """
    pids = list(window.potentiostats)
    short = _drawn_points(window, pids[0], 4_000)    # ~1 h at the 1 Hz DC rate
    long_ = _drawn_points(window, pids[1], 20_000)   # ~5.5 h — the run that broke

    assert long_ <= 2_500, f"a 5 h run draws {long_} points per curve"
    assert long_ < 2 * short, (
        f"point count tracks run length: {short} -> {long_} for 5x the samples"
    )
    # The newest samples stay at full resolution rather than being thinned away.
    assert long_ >= 1_000


def test_ac_curve_is_capped_too(window):
    """The rolling-window (AC/AP) path is bounded by the same point budget."""
    pid = next(iter(window.potentiostats))
    window.plots[pid]['window_duration'] = 10_000.0  # window wider than the data
    drawn = _drawn_points(window, pid, mw.DISPLAY_BUFFER_SIZE, is_ac=True)
    assert drawn <= 2_500, f"AC path drew {drawn} points"


def test_pool_is_private_and_sized_for_boards(window):
    """Experiments are not rationed by CPU count (the queued-forever bug)."""
    from PyQt5 import QtCore

    assert window.threadpool is not QtCore.QThreadPool.globalInstance()
    assert window.threadpool.maxThreadCount() == mw.WORKER_POOL_SIZE
    assert window.threadpool.maxThreadCount() > 16  # a full rig, plus headroom


def test_cancelled_worker_never_touches_hardware(qapp):
    """A worker abandoned while queued does nothing if the pool starts it later."""
    from moles.ui.electrolysis.workers import ElectrolysisSignals, ElectrolysisWorker

    ps = object()  # any hardware access at all would raise on this
    worker = ElectrolysisWorker(
        "A", ps, {"method": "constant_current", "current_mA": 1.0},
        ElectrolysisSignals(), [], claim_key=None, port="",
    )
    worker.cancel()
    worker.run()  # must return without claiming, connecting or emitting


class _NeverStarts:
    """Stand-in for a worker still queued behind a full pool."""

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def test_guardian_cancels_a_worker_that_never_began(tmp_path):
    """A queued worker is cancelled off the GUI thread, so a stall can't skip it.

    This is the safety half of the fix: an uncancelled worker keeps its stale
    parameters and starts the moment a pool thread frees up.
    """
    import time

    from moles.ui.electrolysis.guardian import Guardian

    worker = _NeverStarts()
    never_started = []
    guardian = Guardian(
        {"A": worker}, {},
        on_never_started=never_started.append,
        stop_file=tmp_path / "STOP_ALL", dump_file=tmp_path / "stalls.log",
        start_timeout_s=0.01,
    )
    guardian.arm_start("A")
    assert guardian.starting_channels() == {"A"}

    time.sleep(0.02)
    guardian.poll_once()

    assert worker.cancelled
    assert never_started == ["A"]
    assert guardian.starting_channels() == set()


def test_guardian_leaves_a_worker_alone_before_its_deadline(tmp_path):
    from moles.ui.electrolysis.guardian import Guardian

    worker = _NeverStarts()
    guardian = Guardian(
        {"A": worker}, {},
        stop_file=tmp_path / "STOP_ALL", dump_file=tmp_path / "stalls.log",
        start_timeout_s=30.0,
    )
    guardian.arm_start("A")
    guardian.poll_once()
    assert not worker.cancelled
    assert guardian.starting_channels() == {"A"}


def test_window_frees_the_row_of_a_run_that_never_began(window):
    """The GUI side of a cancelled start: row unlocked, reason on the row."""
    pid = next(iter(window.potentiostats))
    row = window._find_row_for_pid(pid)
    window.workers[pid] = _NeverStarts()
    window._set_row_locked(row, True)

    window._on_worker_never_started(pid)

    assert pid not in window.workers
    assert "never started" in window.ps_table.item(row, mw.COL_STATUS).text()


def test_first_status_disarms_the_start_watchdog(window):
    """A worker that reports in is not treated as never-started."""
    pid = next(iter(window.potentiostats))
    window.guardian.arm_start(pid)
    window.on_status_updated(pid, "Connecting...")
    assert window.guardian.starting_channels() == set()


def test_finished_run_disarms_the_start_watchdog(window):
    """A run that ends before its first status still clears the watchdog."""
    pid = next(iter(window.potentiostats))
    window.guardian.arm_start(pid)
    window.on_process_finished(pid, False, "In use — electroanalysis", "")
    assert window.guardian.starting_channels() == set()


def test_window_tabs_are_electrolysis_and_data_preview(window):
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles == ["Electrolysis", "Data Preview"]

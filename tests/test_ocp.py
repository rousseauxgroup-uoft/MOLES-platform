"""Unit tests for the open-circuit potential monitoring loop.

Drives ``monitor_ocp`` against a fake potentiostat, so the timing, the
stop-early behaviour, and the read-failure tolerance can be checked without
hardware and without a GUI.
"""

import threading

import numpy as np
import pytest

from moles.driver.ps4_ref import SerialReadTimeout
from moles.methods.cv import ExperimentStopped
from moles.methods.ocp import monitor_ocp


class FakePotentiostat:
    """Minimal stand-in exposing only what ``monitor_ocp`` calls.

    Records how the switch was handled and can be told to fail a given number
    of reads, so the loop's tolerance can be exercised.
    """

    device_ID = 1

    def __init__(self, values=None, fail_reads=()):
        self.values = list(values) if values is not None else []
        self.fail_reads = set(fail_reads)   # 1-based indices of reads that fail
        self.switch_closed = True
        self.restore_flags = []             # read_ocp's restore_switch_state args
        self.n_reads = 0                    # read_potential_calibrated calls
        self.on_read = None                 # optional hook, called each read

    def read_ocp(self, restore_switch_state=True):
        self.restore_flags.append(restore_switch_state)
        self.switch_closed = False
        return 0.500

    def read_potential_calibrated(self):
        self.n_reads += 1
        if self.on_read is not None:
            self.on_read(self)
        if self.n_reads in self.fail_reads:
            raise SerialReadTimeout("injected")
        if self.values:
            return np.array([self.values[(self.n_reads - 1) % len(self.values)]])
        return np.array([0.500])


def test_records_expected_number_of_samples():
    ps = FakePotentiostat()
    data = monitor_ocp(ps, duration_s=0.4, sample_hz=20.0)

    # The t=0 reading plus one per interval up to and including the duration.
    assert data.shape == (9, 2)
    assert data[0, 0] == 0.0
    assert data[0, 1] == pytest.approx(0.500)
    # Timestamps increase and stay within the requested window (plus the
    # scheduling slack of one read).
    assert np.all(np.diff(data[:, 0]) > 0)
    assert data[-1, 0] < 0.5


def test_cell_is_disconnected_and_left_that_way():
    ps = FakePotentiostat()
    monitor_ocp(ps, duration_s=0.2, sample_hz=20.0)

    # The switch is opened exactly once, and read_ocp is asked not to put it
    # back — closing it mid-run would polarize the cell being measured.
    assert ps.restore_flags == [False]
    assert ps.switch_closed is False


def test_sample_rate_sets_the_spacing():
    ps = FakePotentiostat()
    data = monitor_ocp(ps, duration_s=1.0, sample_hz=5.0)

    assert data.shape[0] == 6
    spacing = np.diff(data[:, 0])
    assert np.all(spacing > 0.15)
    assert np.all(spacing < 0.35)


def test_potentials_are_recorded_in_order():
    ps = FakePotentiostat(values=[0.1, 0.2, 0.3])
    data = monitor_ocp(ps, duration_s=0.15, sample_hz=20.0)

    assert list(data[1:, 1]) == [0.1, 0.2, 0.3]


def test_callback_receives_every_point():
    ps = FakePotentiostat()
    seen = []
    monitor_ocp(ps, duration_s=0.25, sample_hz=20.0,
                data_callback=lambda t, v: seen.append((t[0], v[0])))

    data_points = 1 + 5
    assert len(seen) == data_points
    assert seen[0][0] == 0.0


def test_stop_event_raises_experiment_stopped():
    ps = FakePotentiostat()
    stop = threading.Event()
    collected = []

    # Trip the stop after a few readings, the way a Stop press would.
    def maybe_stop(fake):
        collected.append(fake.n_reads)
        if fake.n_reads >= 3:
            stop.set()

    ps.on_read = maybe_stop

    with pytest.raises(ExperimentStopped):
        monitor_ocp(ps, duration_s=60.0, sample_hz=20.0, stop_event=stop)

    # It stops promptly rather than running out the full minute.
    assert collected == [1, 2, 3]


def test_stop_before_start_raises_without_touching_the_cell():
    ps = FakePotentiostat()
    stop = threading.Event()
    stop.set()

    with pytest.raises(ExperimentStopped):
        monitor_ocp(ps, duration_s=10.0, sample_hz=2.0, stop_event=stop)
    assert ps.restore_flags == []


def test_isolated_read_failure_only_costs_that_sample():
    ps = FakePotentiostat(fail_reads=(2,))
    data = monitor_ocp(ps, duration_s=0.25, sample_hz=20.0)

    # Five samples were due after t=0; one read failed, leaving a gap.
    assert ps.n_reads == 5
    assert data.shape[0] == 5


def test_repeated_read_failures_abort_the_run():
    ps = FakePotentiostat(fail_reads=range(1, 20))

    with pytest.raises(SerialReadTimeout):
        monitor_ocp(ps, duration_s=5.0, sample_hz=20.0,
                    max_consecutive_timeouts=3)
    assert ps.n_reads == 3


@pytest.mark.parametrize("duration_s, sample_hz", [(0, 2.0), (-1, 2.0),
                                                   (10, 0), (10, -2.0)])
def test_nonsense_settings_are_refused(duration_s, sample_hz):
    with pytest.raises(ValueError):
        monitor_ocp(FakePotentiostat(), duration_s=duration_s, sample_hz=sample_hz)

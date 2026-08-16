"""Open-circuit potential (OCP) monitoring.

The open-circuit potential is the potential a working electrode settles at
against the reference when nothing is being driven through the cell. Watching
it over time is the standard way to check that a cell has equilibrated before
an experiment, that the reference electrode is not drifting, and that the
solution has relaxed back afterwards.

Measuring it means keeping the counter electrode *disconnected* for the whole
run — as soon as current flows the electrode is polarized and what comes back
is no longer an open-circuit potential — and reading the sensed WE-RE potential
on a slow schedule. Nothing needs to be driven, so there is no waveform to
build and no batch stream to service: this module is just that timed read loop.

It takes a potentiostat object and knows nothing about the GUI, so it works the
same from a script, a notebook, or the electroanalysis worker thread.
"""

import logging
import time

import numpy as np

from ..driver.ps4_ref import SerialReadTimeout
from ._common import MAX_CONSECUTIVE_READ_FAILURES
from .cv import ExperimentStopped

logger = logging.getLogger(__name__)

# Default points per second. An OCP trace is a slow relaxation, so a couple of
# readings a second resolve it comfortably while keeping the serial link quiet
# and the files small on hour-long runs.
DEFAULT_SAMPLE_HZ = 2.0

# Longest uninterrupted sleep while waiting for the next sample. Samples can be
# seconds apart, so the wait is broken into slices to keep a Stop press
# responsive rather than making the user wait out the interval.
STOP_POLL_S = 0.05


def _wait_until(t0, due_s, stop_event):
    """Sleep until ``due_s`` seconds after ``t0``, watching for a stop request.

    Returns False if the run was stopped while waiting, True once the sample
    is due.
    """
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        remaining = due_s - (time.monotonic() - t0)
        if remaining <= 0:
            return True
        time.sleep(min(STOP_POLL_S, remaining))


def monitor_ocp(ps, duration_s, sample_hz=DEFAULT_SAMPLE_HZ, data_callback=None,
                stop_event=None,
                max_consecutive_timeouts=MAX_CONSECUTIVE_READ_FAILURES):
    """Record the cell's open-circuit potential over time.

    Opens the cell (counter electrode disconnected) and samples the resting
    WE-RE potential on a fixed schedule until ``duration_s`` has elapsed. The
    switch is opened once, at the start, and left open for the whole run, so
    the cell is never polarized by the measurement itself.

    Args:
        ps: a connected potentiostat (real or mock).
        duration_s: how long to monitor for, in seconds.
        sample_hz: readings per second.
        data_callback: called with ``(time_s, potential_V)`` numpy arrays as
            each reading is taken, for live plotting.
        stop_event: a ``threading.Event``; when set, the loop ends promptly and
            raises ``ExperimentStopped`` so the caller can offer the partial
            trace for saving.
        max_consecutive_timeouts: serial read failures tolerated back-to-back
            before giving up. Isolated failures only cost that one sample.

    Returns:
        Nx2 float array with column 0 = time since the first reading (s) and
        column 1 = potential (V).

    Raises:
        ValueError: if the duration or sample rate is not positive.
        ExperimentStopped: if ``stop_event`` was set before the run finished.
        SerialReadTimeout: if reads keep failing back-to-back.
    """
    duration_s = float(duration_s)
    if duration_s <= 0:
        raise ValueError("OCP monitoring needs a positive duration.")
    sample_hz = float(sample_hz)
    if sample_hz <= 0:
        raise ValueError("OCP monitoring needs a positive sample rate.")
    interval_s = 1.0 / sample_hz

    if stop_event is not None and stop_event.is_set():
        raise ExperimentStopped("Stop requested by user")

    # read_ocp disconnects the counter electrode and leaves it disconnected
    # (restore_switch_state=False), and takes its reading after the auto-gain
    # settling reads — so it both puts the cell in the state this loop needs
    # and supplies the t=0 sample.
    first_V = float(ps.read_ocp(restore_switch_state=False))
    t0 = time.monotonic()

    times = [0.0]
    potentials = [first_V]
    if data_callback:
        data_callback(np.array([0.0]), np.array([first_V]))

    consecutive_failures = 0
    sample = 1
    while True:
        # Each sample is due at a fixed offset from the start rather than an
        # interval after the previous one, so a slow read does not push every
        # later sample further behind.
        due_s = sample * interval_s
        if due_s > duration_s + 1e-9:
            break
        if not _wait_until(t0, due_s, stop_event):
            raise ExperimentStopped("Stop requested by user")

        sample += 1
        try:
            value = float(ps.read_potential_calibrated()[0])
        except SerialReadTimeout:
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_timeouts:
                raise
            # One lost reading leaves a gap in the trace; the run continues.
            logger.warning(
                "[PS %s] OCP read timed out (%d in a row) — skipping this sample.",
                getattr(ps, 'device_ID', '?'), consecutive_failures,
            )
            continue
        consecutive_failures = 0

        elapsed = time.monotonic() - t0
        times.append(elapsed)
        potentials.append(value)
        if data_callback:
            data_callback(np.array([elapsed]), np.array([value]))

    return np.column_stack([times, potentials])

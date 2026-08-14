"""Shared constants and error-policy helpers used by every electrolysis method.

Kept in one place so that a tweak (e.g. making a soft-start gentler, or the
number of tolerated serial failures) only needs to be made once and is
consistent across all methods.
"""

import logging
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Standardized ramp step sizes for soft-starting an experiment, used to avoid
# sudden current/voltage jumps at t=0 that could shock the cell.
CURRENT_RAMP_STEP_MA = 0.5
POTENTIAL_RAMP_STEP_V = 0.1

# Time the controller must continuously observe a deviation before raising a
# compliance warning. Prevents brief glitches from spamming the status line.
COMPLIANCE_DURATION_THRESHOLD = 5.0  # seconds

# Interval between measurements during steady-state electrolysis. 1 Hz is plenty
# for visualizing current/voltage trends over hour-scale runs and keeps the CSV
# size and USB traffic low on long experiments.
MEASUREMENT_INTERVAL_S = 1.0

# Abort the run if this many serial read failures occur back-to-back, which
# suggests a persistent comm problem rather than a transient glitch.
MAX_CONSECUTIVE_READ_FAILURES = 5

# Structured outcomes returned by the run_* methods so the worker can report
# what actually happened. A comm-failure abort used to be indistinguishable
# from normal completion ("Completed"), hiding lost experiments.
OUTCOME_COMPLETED = "completed"
OUTCOME_STOPPED = "stopped"
OUTCOME_COMM_FAILURE = "comm_failure"
# The user edited a running experiment in a way that cannot be applied in place
# (a different method, or a waveform change the running loop can't retune). The
# loop returns this so the worker can gracefully stop it and start the new
# method, carrying the accumulated charge/elapsed forward (see LiveControl).
OUTCOME_METHOD_CHANGE = "method_change"


class LiveControl:
    """Coordinates live edits to a running experiment between GUI and loop.

    Two kinds of edit are supported while a run is in progress:

    * **In-place tweak** (same method, e.g. changing the target current or the
      charge target). The GUI mutates the shared ``params`` dict in place and
      the method loop re-reads the affected values each iteration — no restart,
      so charge integration is never interrupted. ``LiveControl`` is not
      involved in this path beyond existing.
    * **Method change / retune** (e.g. Constant Current → Constant Potential, or
      a new alternating-polarity waveform). The GUI calls ``request_switch``;
      the running loop notices via ``switch_requested`` at its next checkpoint,
      records how much charge/time it has accumulated with ``carry``, and
      returns ``OUTCOME_METHOD_CHANGE``. The worker then starts the new method,
      seeding it with the carried charge/elapsed so the coulomb count and the
      progress bar continue seamlessly.

    All state is guarded by a lock because the GUI thread requests switches
    while the worker thread reads them.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._switch = False
        # Charge (C) and elapsed time (s) the running loop had accumulated at
        # the moment it handed off, so the next method can pick up where it left.
        self.carried_charge_C = 0.0
        self.carried_elapsed_s = 0.0

    def request_switch(self):
        """Ask the running loop to stop and hand off to a new method."""
        with self._lock:
            self._switch = True

    def clear_switch(self):
        """Reset the switch flag (called by the worker before each method run)."""
        with self._lock:
            self._switch = False

    def switch_requested(self) -> bool:
        """True once the GUI has asked for a method change / retune."""
        with self._lock:
            return self._switch

    def carry(self, charge_C: float, elapsed_s: float) -> None:
        """Record the accumulated charge/elapsed for the next method to resume from."""
        with self._lock:
            self.carried_charge_C = float(charge_C)
            self.carried_elapsed_s = float(elapsed_s)


def close_switch_at_ocp(ps):
    """Connect the cell with the setpoint matched to its resting potential.

    With the switch open, the sensed WE-RE potential IS the cell's open-circuit
    potential. Parking the setpoint there before closing means no potential
    step and no current surge at the moment of contact (closing onto a
    mismatched setpoint was the bench-verified cause of start-of-run spikes).
    Returns the measured OCP so the caller can soft-start from it instead of
    from 0 V.
    """
    ocp = float(ps.read_potential_calibrated()[0])
    ps.write_potential_calibrated(ocp)
    time.sleep(0.2)
    ps.write_switch(1)
    time.sleep(0.2)
    return ocp


def read_measurement(ps):
    """Read one calibrated (voltage_V, current_mA) sample.

    Uses the combined single-transaction read when the driver provides it,
    falling back to two separate reads for older drivers. Serial failures
    (``SerialReadTimeout``) propagate to the caller's tolerance logic.
    """
    try:
        vals = ps.read_potential_current_calibrated()
        return vals[0], vals[1]
    except AttributeError:
        return ps.read_potential_calibrated()[0], ps.read_current_calibrated()[0]


def shutdown_hardware(ps, pot_id, steps, signals=None):
    """Drive the cell to a safe state, attempting every step even if one fails.

    ``steps`` is a list of (description, callable) pairs executed in order.
    A failed step no longer aborts the remaining ones (previously a failed
    zeroing write skipped the switch-off entirely, leaving the cell powered).
    Returns True only if every step succeeded.
    """
    all_ok = True
    for name, step in steps:
        try:
            step()
        except Exception as e:
            all_ok = False
            logger.critical(
                "[PS %s] SHUTDOWN STEP FAILED (%s): %s — cell may still be energized!",
                pot_id, name, e,
            )
            if signals:
                signals.status_updated.emit(pot_id, f"SHUTDOWN FAILED: {name} — check cell!")
    if all_ok:
        logger.info("[PS %s] Output zeroed and switch opened.", pot_id)
    return all_ok


def resolve_stop_limits(params: Dict) -> Tuple[Optional[float], Optional[float]]:
    """Read the charge and time stopping criteria from a method's params dict.

    Either or both may be set; an experiment stops on whichever limit is
    reached first. A value of ``None`` (or non-positive) means "no limit"
    on that axis. Raises ``ValueError`` if neither is active, since a run
    with no stop criterion would never terminate on its own.
    """
    raw_charge = params.get('total_charge_C')
    raw_time = params.get('total_time_s')
    charge_limit = float(raw_charge) if raw_charge is not None and float(raw_charge) > 0 else None
    time_limit = float(raw_time) if raw_time is not None and float(raw_time) > 0 else None
    if charge_limit is None and time_limit is None:
        raise ValueError(
            "No stopping criterion set: provide total_charge_C, total_time_s, or both."
        )
    return charge_limit, time_limit


def live_stop_limits(
    params: Dict, prev_charge: Optional[float], prev_time: Optional[float]
) -> Tuple[Optional[float], Optional[float]]:
    """Re-read the charge/time stop limits mid-run for a live edit.

    Like ``resolve_stop_limits`` but non-raising: if the freshly-read params
    would leave the run with *no* stop criterion at all (both blank/non-positive),
    the previous limits are kept instead. This makes clearing both fields on a
    running experiment a no-op rather than a way to create a run that never ends.
    """
    raw_charge = params.get('total_charge_C')
    raw_time = params.get('total_time_s')
    try:
        charge = float(raw_charge) if raw_charge not in (None, "") and float(raw_charge) > 0 else None
    except (TypeError, ValueError):
        charge = prev_charge
    try:
        time_limit = float(raw_time) if raw_time not in (None, "") and float(raw_time) > 0 else None
    except (TypeError, ValueError):
        time_limit = prev_time
    if charge is None and time_limit is None:
        return prev_charge, prev_time
    return charge, time_limit


def should_stop(charge_passed: float, elapsed_s: float,
                charge_limit: Optional[float], time_limit: Optional[float]) -> bool:
    """Return True once either the charge or time limit has been reached."""
    if charge_limit is not None and charge_passed >= charge_limit:
        return True
    if time_limit is not None and elapsed_s >= time_limit:
        return True
    return False

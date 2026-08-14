"""Constant-current (galvanostatic) electrolysis control loop.

Holds a target current at the working electrode and integrates total charge
passed until a target charge (typically derived from Faraday's law) is reached
or the user stops the run. Used by the multichannel electrolysis UI but has
no GUI dependency of its own.
"""

import logging
import threading
import time
from typing import Dict

from ._common import (
    CURRENT_RAMP_STEP_MA,
    COMPLIANCE_DURATION_THRESHOLD,
    MAX_CONSECUTIVE_READ_FAILURES,
    MEASUREMENT_INTERVAL_S,
    OUTCOME_COMM_FAILURE,
    OUTCOME_COMPLETED,
    OUTCOME_METHOD_CHANGE,
    OUTCOME_STOPPED,
    close_switch_at_ocp,
    live_stop_limits,
    read_measurement,
    resolve_stop_limits,
    should_stop,
    shutdown_hardware,
)
from ..driver.ps4_ref import SerialReadTimeout

logger = logging.getLogger(__name__)


def run_constant_current(
    ps,
    params: Dict,
    output_queue,
    pot_id,
    stop_event: threading.Event,
    signals=None,
    csv_writer=None,
    control=None,
    initial_charge_C: float = 0.0,
    initial_elapsed_s: float = 0.0,
):
    """Run one constant-current electrolysis experiment to completion or stop.

    Streams ``(elapsed, current, voltage, charge)`` tuples to ``output_queue``
    and writes them to ``csv_writer`` if supplied. ``signals`` is an optional
    duck-typed object with a ``status_updated.emit(pot_id, msg)`` method, used
    by the UI to surface compliance warnings; pass ``None`` to silence them.

    Live edits: the target current and the charge/time stop limits are re-read
    from ``params`` every iteration, so a mid-run tweak takes effect without
    restarting (the commanded current is ramped gently toward the new target).
    ``control`` (a ``LiveControl``) lets the UI request a method change; when
    it does, the loop hands back its accumulated charge/elapsed and returns
    ``OUTCOME_METHOD_CHANGE``. ``initial_charge_C``/``initial_elapsed_s`` seed
    those totals so a method that took over from another continues its count.

    Returns one of the ``OUTCOME_*`` constants from ``_common`` so the caller
    can distinguish completion, a user stop, and a comm-failure abort.
    """
    current_mA = params['current_mA']
    # Run stops on whichever limit (charge or time) is reached first.
    charge_limit_C, time_limit_s = resolve_stop_limits(params)
    outcome = OUTCOME_COMPLETED

    initial_current = params.get('initial_current_mA', 0.01)
    if current_mA < 0:
        initial_current = -abs(initial_current)

    # Compliance detection state
    compliance_start_time = None
    last_compliance_status = False

    # Tracks back-to-back serial read failures so a persistent comm problem aborts the run.
    consecutive_read_failures = 0

    logger.info("[PS %s] Starting constant current electrolysis at %s mA...", pot_id, current_mA)

    # Seeded so a hand-off from a previous method keeps a continuous count.
    total_charge_passed = float(initial_charge_C)

    try:
        # Ramping is kept inside the try block so any failure during soft-start
        # triggers the finally block's write_switch(0) fail-state.
        current = initial_current
        time.sleep(1.0)

        # Connect at the cell's resting potential BEFORE starting the hold:
        # a hold running with the switch open walks the DAC away from the cell
        # potential, so the old hold-then-close order slammed the cell at contact
        ocp = close_switch_at_ocp(ps)
        # Anchor the hold's first setpoint at the OCP (the zero-current point)
        # instead of the static current-model guess
        ps.write_current_hold_calibrated(initial_current, learning_rate=0.05,
                                         voltage_guess=ocp)

        # Soft-start: ramp toward the target current in 0.5 mA steps to avoid spikes.
        ramp_step = CURRENT_RAMP_STEP_MA
        if current_mA >= 0:
            while current < current_mA and not stop_event.is_set():
                current = min(current + ramp_step, current_mA)
                ps.write_current_hold_calibrated(current)
                time.sleep(0.1)
        else:
            while current > current_mA and not stop_event.is_set():
                current = max(current - ramp_step, current_mA)
                ps.write_current_hold_calibrated(current)
                time.sleep(0.1)

        ps.write_current_hold_calibrated(current)
        # The current the hardware is presently commanded to hold. Live edits to
        # the target are approached from here in small steps, not jumped to.
        commanded_mA = current

        # Reset timing references after the ramp so elapsed/charge start at the steady-state hold.
        start_time = time.time()
        last_time = start_time

        while not stop_event.is_set():
            # Live edit: re-read the target current and step the commanded value
            # gently toward it, so a mid-run change ramps rather than jumps. In
            # steady state (commanded == target) nothing is written to the board.
            target_mA = float(params.get('current_mA', current_mA))
            if abs(target_mA - commanded_mA) > 1e-9:
                delta = target_mA - commanded_mA
                if abs(delta) > CURRENT_RAMP_STEP_MA:
                    commanded_mA += CURRENT_RAMP_STEP_MA if delta > 0 else -CURRENT_RAMP_STEP_MA
                else:
                    commanded_mA = target_mA
                ps.write_current_hold_calibrated(commanded_mA)
            current_mA = target_mA

            # Live edit: re-read the stop limits (a blank/zero pair keeps the old ones).
            charge_limit_C, time_limit_s = live_stop_limits(params, charge_limit_C, time_limit_s)

            current_time = time.time()
            elapsed = round(current_time - start_time + initial_elapsed_s, 2)
            dt = current_time - last_time

            try:
                volt_val, curr_val = read_measurement(ps)
            except SerialReadTimeout:
                # Driver flushed and retried once; both attempts came up short.
                # Skip this iteration so a one-off USB glitch doesn't kill a long run.
                # last_time is intentionally NOT updated: the next successful read uses
                # an enlarged dt to credit charge for the gap.
                consecutive_read_failures += 1
                msg = f"WARNING: Serial read timeout @ {(elapsed/60):.2f} min ({consecutive_read_failures}/{MAX_CONSECUTIVE_READ_FAILURES})"
                if signals:
                    signals.status_updated.emit(pot_id, msg)
                logger.warning("[PS %s] %s", pot_id, msg)
                if consecutive_read_failures >= MAX_CONSECUTIVE_READ_FAILURES:
                    logger.error("[PS %s] Aborting: %d consecutive serial read failures",
                                 pot_id, consecutive_read_failures)
                    outcome = OUTCOME_COMM_FAILURE
                    break
                time.sleep(MEASUREMENT_INTERVAL_S)
                continue
            consecutive_read_failures = 0

            # Compliance check: a sustained deviation from the *commanded* current
            # suggests the controller has hit a hardware compliance limit (e.g. USB
            # voltage rail). Compared against commanded (not the final target) so a
            # live setpoint change that is still ramping isn't misread as compliance.
            deviation = abs(commanded_mA - curr_val)
            is_deviating = (deviation > abs(commanded_mA * 0.05)) and (deviation > 0.1)

            if is_deviating:
                if compliance_start_time is None:
                    compliance_start_time = current_time
                elif (current_time - compliance_start_time) > COMPLIANCE_DURATION_THRESHOLD:
                    if not last_compliance_status:
                        if signals:
                            signals.status_updated.emit(pot_id, "WARNING: Low Current - USB Compliance Limit?")
                        logger.warning("[PS %s] Compliance limit may have been reached @ %.2f min (target %s mA, meas %.2f mA)",
                                       pot_id, elapsed / 60, current_mA, curr_val)
                        last_compliance_status = True
            else:
                compliance_start_time = None
                if last_compliance_status:
                    if signals:
                        signals.status_updated.emit(pot_id, "Running...")
                    last_compliance_status = False

            charge_increment = (curr_val / 1000) * dt
            total_charge_passed += abs(charge_increment)

            if csv_writer:
                csv_writer.writerow([elapsed, round(curr_val, 4), round(volt_val, 4), round(total_charge_passed, 4)])
            output_queue.put((elapsed, curr_val, volt_val, total_charge_passed))

            if should_stop(total_charge_passed, elapsed, charge_limit_C, time_limit_s):
                if charge_limit_C is not None and total_charge_passed >= charge_limit_C:
                    logger.info("[PS %s] Target charge reached!", pot_id)
                else:
                    logger.info("[PS %s] Time limit reached (%.1f s)", pot_id, elapsed)
                break

            # Live edit: the user changed the method (or asked to retune). Hand the
            # running total off to the worker and let it start the new method.
            if control is not None and control.switch_requested():
                control.carry(total_charge_passed, elapsed)
                outcome = OUTCOME_METHOD_CHANGE
                break

            last_time = current_time
            time.sleep(MEASUREMENT_INTERVAL_S)

        if outcome == OUTCOME_COMPLETED and stop_event.is_set():
            outcome = OUTCOME_STOPPED
        return outcome

    finally:
        # Attempt every step even if one fails — the switch-off must not be
        # skipped because the zeroing write raised (see shutdown_hardware).
        # The switch is opened via _safe_open_switch (stops the hold, matches
        # the setpoint to the sensed cell potential, then opens) so the
        # disconnect itself is spike-free.
        shutdown_hardware(ps, pot_id, [
            ("zero current hold", lambda: ps.write_current_hold_calibrated(0)),
            ("stop current hold", ps.write_current_hold_stop),
            ("open switch", ps._safe_open_switch),
        ], signals=signals)

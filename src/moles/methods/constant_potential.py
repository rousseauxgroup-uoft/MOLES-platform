"""Constant-potential (potentiostatic) electrolysis control loop.

Holds a target potential at the working electrode (relative to the reference
electrode) and integrates absolute charge passed until a target charge is
reached or the user stops the run. Has no GUI dependency.
"""

import logging
import threading
import time
from typing import Dict

from ._common import (
    POTENTIAL_RAMP_STEP_V,
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


def run_constant_potential(
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
    """Run one constant-potential electrolysis experiment to completion or stop.

    Streams ``(elapsed, current, voltage, charge)`` tuples to ``output_queue``
    and writes them to ``csv_writer`` if supplied. ``signals`` is an optional
    duck-typed status reporter (see ``run_constant_current`` for details).

    Live edits: the target potential and the charge/time stop limits are re-read
    from ``params`` every iteration (the commanded potential ramps gently toward
    a new target). ``control`` lets the UI request a method change, on which the
    loop returns ``OUTCOME_METHOD_CHANGE`` carrying its charge/elapsed;
    ``initial_charge_C``/``initial_elapsed_s`` seed those totals when this method
    is taking over from a previous one. See ``run_constant_current`` for detail.

    Returns one of the ``OUTCOME_*`` constants from ``_common`` so the caller
    can distinguish completion, a user stop, and a comm-failure abort.
    """
    potential_V = params['potential_V']
    # Run stops on whichever limit (charge or time) is reached first.
    charge_limit_C, time_limit_s = resolve_stop_limits(params)
    outcome = OUTCOME_COMPLETED

    logger.info("[PS %s] Starting constant potential electrolysis at %s V...", pot_id, potential_V)

    start_time = time.time()
    last_time = start_time
    # Seeded so a hand-off from a previous method keeps a continuous count.
    total_charge_passed = float(initial_charge_C)

    # Compliance detection state
    compliance_start_time = None
    last_compliance_status = False

    # Tracks back-to-back serial read failures so a persistent comm problem aborts the run.
    consecutive_read_failures = 0

    try:
        # Inside the try so a failure while energizing still runs the
        # shutdown steps in the finally block.
        ps.write_gain(0)  # Force 100 ohm resistor (0 index) for electrolysis currents
        # Connect at the cell's resting potential so contact itself is spike-free
        ocp = close_switch_at_ocp(ps)

        # Soft-start: ramp OCP -> target potential in 0.1 V steps to avoid a sudden jump.
        ramp_step = POTENTIAL_RAMP_STEP_V if potential_V >= ocp else -POTENTIAL_RAMP_STEP_V
        ramp_v = ocp
        while (ramp_step > 0 and ramp_v < potential_V) or (ramp_step < 0 and ramp_v > potential_V):
            if stop_event.is_set():
                break
            if ramp_step > 0:
                ramp_v = min(ramp_v + ramp_step, potential_V)
            else:
                ramp_v = max(ramp_v + ramp_step, potential_V)
            ps.write_potential_calibrated(ramp_v)
            time.sleep(0.1)
        ps.write_potential_calibrated(potential_V)
        # The potential the hardware is presently commanded to hold. Live edits
        # to the target are approached from here in small steps, not jumped to.
        commanded_V = potential_V

        # Reset timing references after the ramp so elapsed/charge start at the steady-state hold.
        start_time = time.time()
        last_time = start_time

        while not stop_event.is_set():
            # Live edit: re-read the target potential and step the commanded value
            # gently toward it, so a mid-run change ramps rather than jumps. In
            # steady state (commanded == target) nothing is written to the board.
            target_V = float(params.get('potential_V', potential_V))
            if abs(target_V - commanded_V) > 1e-9:
                delta = target_V - commanded_V
                if abs(delta) > POTENTIAL_RAMP_STEP_V:
                    commanded_V += POTENTIAL_RAMP_STEP_V if delta > 0 else -POTENTIAL_RAMP_STEP_V
                else:
                    commanded_V = target_V
                ps.write_potential_calibrated(commanded_V)
            potential_V = target_V

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

            # Stability check (against the *commanded* potential, so a live
            # setpoint change that is still ramping isn't misread as a fault):
            #   1. Voltage error too large -> short circuit / overload.
            #   2. Driving > 100 mV but seeing nearly no current -> open circuit / high impedance.
            v_err = abs(commanded_V - volt_val)
            v_fail = (v_err > 0.2)
            i_low = (abs(commanded_V) > 0.1) and (abs(curr_val) < 0.05)

            if v_fail or i_low:
                if compliance_start_time is None:
                    compliance_start_time = current_time
                elif (current_time - compliance_start_time) > COMPLIANCE_DURATION_THRESHOLD:
                    if not last_compliance_status:
                        if signals:
                            msg = "WARNING: Voltage Error" if v_fail else "WARNING: Low Current"
                            signals.status_updated.emit(pot_id, msg)
                        logger.warning("[PS %s] Control issue @ %.2f min (set %s V, meas %.2f V, %.2f mA)",
                                       pot_id, elapsed / 60, potential_V, volt_val, curr_val)
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
                csv_writer.writerow([elapsed, round(curr_val, 3), round(volt_val, 3), round(total_charge_passed, 3)])
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
        # Disconnect before zeroing: _safe_open_switch matches the setpoint to
        # the cell and opens spike-free; zeroing afterwards happens on an open
        # circuit instead of stepping the still-connected cell to 0 V.
        shutdown_hardware(ps, pot_id, [
            ("open switch", ps._safe_open_switch),
            ("zero potential", lambda: ps.write_potential_calibrated(0)),
        ], signals=signals)

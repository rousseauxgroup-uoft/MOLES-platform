"""Alternating-current (AC) electrolysis control loop.

Generates a periodic current waveform (sine or square) in software and
streams the target points to the potentiostat continuously. Update rate is
capped to keep the USB serial link from saturating, so very high frequencies
will see waveform fidelity degrade.
"""

import logging
import math
import threading
import time
from typing import Dict

from ._common import (
    CURRENT_RAMP_STEP_MA,
    MAX_CONSECUTIVE_READ_FAILURES,
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


def run_alternating_current(
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
    """Run one AC electrolysis experiment to completion or stop.

    Stops on whichever limit is reached first: ``params['total_charge_C']``
    worth of *absolute* charge (since net charge of an AC waveform can be near
    zero, total absolute charge is the meaningful criterion) or an optional
    ``params['total_time_s']`` time limit. Streams measured points to
    ``output_queue`` and ``csv_writer`` if supplied.

    Live edits: the waveform (peaks, frequency, duty, shape) and the stop limits
    are re-read from ``params`` each iteration, so a mid-run tweak retunes the
    software-generated waveform without a restart. ``control`` lets the UI
    request a method change (returns ``OUTCOME_METHOD_CHANGE`` carrying the
    charge/elapsed); ``initial_charge_C``/``initial_elapsed_s`` seed those totals
    when taking over from a previous method.
    """
    pos_peak_mA = params.get('ac_pos_peak_mA', 1.0)
    neg_peak_mA = params.get('ac_neg_peak_mA', -1.0)
    frequency_hz = params.get('ac_frequency_hz', 0.1)
    duty_cycle = params.get('ac_duty_cycle', 0.5)
    waveform_type = params.get('ac_waveform', 'Sine')
    # Run stops on whichever limit (charge or time) is reached first.
    charge_limit_C, time_limit_s = resolve_stop_limits(params)

    period = 1.0 / frequency_hz if frequency_hz > 0 else 10.0
    t_pos = period * duty_cycle
    t_neg = period * (1.0 - duty_cycle)
    offset_mA = (pos_peak_mA + neg_peak_mA) / 2.0
    amplitude_mA = (pos_peak_mA - neg_peak_mA) / 2.0

    # Update rate: target ~10x the signal frequency, but capped between 5-20 Hz
    # to keep the serial link from saturating at high frequencies.
    target_update_rate = max(5.0, min(20.0, frequency_hz * 20.0))
    update_interval = 1.0 / target_update_rate

    logger.info("[PS %s] Starting AC Electrolysis (%s): Pos Peak %s mA, Neg Peak %s mA, %s Hz, Duty %.1f%%",
                pot_id, waveform_type, pos_peak_mA, neg_peak_mA, frequency_hz, duty_cycle * 100)
    logger.info("[PS %s] Approx update rate: %.1f Hz (interval: %.3f s)",
                pot_id, target_update_rate, update_interval)

    if frequency_hz > 7.0:
        if signals:
            signals.status_updated.emit(pot_id, "WARNING: Freq > 7Hz may be unstable")
        logger.warning("[PS %s] High frequency (%s Hz) may result in poor waveform fidelity due to latency.",
                       pot_id, frequency_hz)

    # Synchronize logging with the update interval to capture the waveform faithfully.
    logging_interval = update_interval

    # Seeded so a hand-off from a previous method keeps a continuous count.
    total_charge_passed = float(initial_charge_C)
    outcome = OUTCOME_COMPLETED

    # Tracks back-to-back serial read failures so a persistent comm problem
    # aborts the run instead of a single 10 s USB hiccup killing it outright.
    consecutive_read_failures = 0

    try:
        # Inside the try so a failure while energizing still runs the
        # shutdown steps in the finally block.
        ps.write_gain(0)  # Start with correct gain (100 Ohm for mA range)

        # Soft-start: ramp 0 -> pos_peak in 0.5 mA steps before beginning the waveform.
        # The waveform loop is then phase-aligned so its first sample sits at the
        # positive peak, eliminating the discontinuity that would otherwise occur
        # at hand-off.
        ramp_step = CURRENT_RAMP_STEP_MA if pos_peak_mA >= 0 else -CURRENT_RAMP_STEP_MA
        ramp_i = 0.0
        # Connect at the cell's resting potential BEFORE starting the hold, so
        # contact is spike-free and the hold cannot wind up on an open switch
        ocp = close_switch_at_ocp(ps)
        ps.write_current_hold_calibrated(0.0, learning_rate=0.05, voltage_guess=ocp)
        while (ramp_step > 0 and ramp_i < pos_peak_mA) or (ramp_step < 0 and ramp_i > pos_peak_mA):
            if stop_event.is_set():
                break
            if ramp_step > 0:
                ramp_i = min(ramp_i + ramp_step, pos_peak_mA)
            else:
                ramp_i = max(ramp_i + ramp_step, pos_peak_mA)
            ps.write_current_hold_calibrated(ramp_i)
            time.sleep(0.1)
        ps.write_current_hold_calibrated(pos_peak_mA)

        # Phase alignment: choose a virtual start time so that elapsed=0 of the loop
        # corresponds to the first positive peak of the waveform.
        #   - Sine: peak occurs at phase_time = t_pos / 2.
        #   - Square: peak is the entire t_pos segment, so phase_time = 0 is already a peak.
        if waveform_type == 'Sine':
            phase_offset = t_pos / 2.0
        else:
            phase_offset = 0.0
        start_time = time.time() - phase_offset
        last_log_time = time.time()

        while not stop_event.is_set():
            # Live edit: re-read the waveform and stop limits each iteration so a
            # mid-run tweak retunes the software-generated waveform in place.
            pos_peak_mA = float(params.get('ac_pos_peak_mA', pos_peak_mA))
            neg_peak_mA = float(params.get('ac_neg_peak_mA', neg_peak_mA))
            frequency_hz = float(params.get('ac_frequency_hz', frequency_hz))
            duty_cycle = float(params.get('ac_duty_cycle', duty_cycle))
            waveform_type = params.get('ac_waveform', waveform_type)
            period = 1.0 / frequency_hz if frequency_hz > 0 else 10.0
            t_pos = period * duty_cycle
            t_neg = period * (1.0 - duty_cycle)
            offset_mA = (pos_peak_mA + neg_peak_mA) / 2.0
            amplitude_mA = (pos_peak_mA - neg_peak_mA) / 2.0
            update_interval = 1.0 / max(5.0, min(20.0, frequency_hz * 20.0))
            logging_interval = update_interval
            charge_limit_C, time_limit_s = live_stop_limits(params, charge_limit_C, time_limit_s)

            loop_time = time.time()
            elapsed = loop_time - start_time
            # Reported/stored time continues any carried-over elapsed from a
            # prior method; the waveform phase still keys off the local elapsed.
            report_elapsed = elapsed + initial_elapsed_s

            # 1. Calculate target current at this point in the waveform.
            phase_time = elapsed % period if period > 0 else 0.0
            if waveform_type == 'Sine':
                if phase_time < t_pos:
                    mapped_phase = (phase_time / t_pos) * math.pi
                else:
                    mapped_phase = math.pi + ((phase_time - t_pos) / t_neg) * math.pi
                target_current = offset_mA + amplitude_mA * math.sin(mapped_phase)
            elif waveform_type == 'Square':
                if phase_time < t_pos:
                    target_current = pos_peak_mA
                else:
                    target_current = neg_peak_mA
            else:
                target_current = offset_mA  # Fallback

            # 2. Command the potentiostat. Prefer the explicit AC method if the
            # driver supports it; otherwise use the generic current hold.
            if hasattr(ps, 'set_alternating_current'):
                ps.set_alternating_current(target_current)
            else:
                ps.write_current_hold_calibrated(target_current)

            # 3. Read measured values to track reality vs target. Tolerate
            # transient read failures the same way the DC methods do — the
            # waveform commanding in step 2 keeps running regardless.
            try:
                meas_v, meas_i = read_measurement(ps)
            except SerialReadTimeout:
                consecutive_read_failures += 1
                msg = f"WARNING: Serial read timeout ({consecutive_read_failures}/{MAX_CONSECUTIVE_READ_FAILURES})"
                if signals:
                    signals.status_updated.emit(pot_id, msg)
                logger.warning("[PS %s] %s", pot_id, msg)
                if consecutive_read_failures >= MAX_CONSECUTIVE_READ_FAILURES:
                    logger.error("[PS %s] Aborting: %d consecutive serial read failures",
                                 pot_id, consecutive_read_failures)
                    outcome = OUTCOME_COMM_FAILURE
                    break
                continue
            consecutive_read_failures = 0

            # 4. Integrate absolute charge. Net charge of an AC waveform can be
            # near zero, so absolute charge is the meaningful "current passed"
            # metric for stopping the experiment.
            charge_increment = (abs(meas_i) / 1000.0) * update_interval
            total_charge_passed += charge_increment

            # Log at slower rate than the update loop
            if (loop_time - last_log_time) >= logging_interval:
                if csv_writer:
                    csv_writer.writerow([float(report_elapsed), round(meas_i, 4), round(meas_v, 4), round(total_charge_passed, 4)])
                output_queue.put((report_elapsed, meas_i, meas_v, total_charge_passed))
                last_log_time = loop_time

            # Check stop condition (charge or time, whichever is reached first)
            if should_stop(total_charge_passed, report_elapsed, charge_limit_C, time_limit_s):
                if charge_limit_C is not None and total_charge_passed >= charge_limit_C:
                    logger.info("[PS %s] Target active charge reached (approximated)", pot_id)
                else:
                    logger.info("[PS %s] Time limit reached (%.1f s)", pot_id, report_elapsed)
                break

            # Live edit: the user changed the method (or asked to retune the
            # waveform). Hand the running total off and let the worker restart.
            if control is not None and control.switch_requested():
                control.carry(total_charge_passed, report_elapsed)
                outcome = OUTCOME_METHOD_CHANGE
                break

            # Sleep to maintain update rate
            work_dur = time.time() - loop_time
            sleep_time = max(0.0, update_interval - work_dur)
            time.sleep(sleep_time)

        if outcome == OUTCOME_COMPLETED and stop_event.is_set():
            outcome = OUTCOME_STOPPED
        return outcome

    except Exception:
        logger.exception("[PS %s] AC loop error", pot_id)
        raise
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

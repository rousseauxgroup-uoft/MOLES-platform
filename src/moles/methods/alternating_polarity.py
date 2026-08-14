"""Alternating-polarity (potentiostatic) electrolysis control loop.

Pre-computes a voltage waveform and executes it via the potentiostat firmware's
batch protocol.  Because each voltage step is written directly to the DAC — no
PID feedback loop to converge on a target current — waveforms run at higher
frequencies than the ~7 Hz software-loop limit of the current-based AC method.

The sample rate is capped (see ``SAFE_SAMPLE_HZ_MAX`` below). The binding
constraint is no longer the host's USB feed rate (batched ring-buffer feeding
lifted that ceiling well past 5000 points/s) but the firmware itself: its
per-step delay is a whole number of milliseconds (500 Hz = 2 ms/step is the
fastest even step), and each step must fit its DAC write, averaged ADC read
and USB transmit inside that period. If the firmware ever falls behind or
starves, its guard holds the present potential rather than corrupting — the
``starved_steps`` diagnostic counts exactly this.

The waveform runs as ONE continuous hardware-timed stream for the whole
experiment (``stream_voltage_batch``): the driver keeps the firmware's ring
buffer topped up while measurements flow back in short "blocks", between which
charge, the stop criteria and live edits are checked. Streaming replaced the
old chunked execution (one finite batch per ~2 s, re-uploaded each time),
whose re-upload gap parked the DAC for ~0.5 s between every chunk.
"""

import logging
import threading
import time
from typing import Dict

import numpy as np

from ._common import (
    POTENTIAL_RAMP_STEP_V,
    OUTCOME_COMM_FAILURE,
    OUTCOME_COMPLETED,
    OUTCOME_METHOD_CHANGE,
    OUTCOME_STOPPED,
    close_switch_at_ocp,
    live_stop_limits,
    resolve_stop_limits,
    should_stop,
    shutdown_hardware,
)
from ..driver.ps4_ref import SerialAckError, SerialReadTimeout

logger = logging.getLogger(__name__)

# Abort after this many back-to-back failed stream attempts with no data
# delivered in between — a persistent link problem, not a one-off glitch.
# (The counter resets as soon as any measurement block arrives.)
MAX_CONSECUTIVE_STREAM_FAILURES = 3



# Ceiling for the firmware's per-step rate (delay_ms = 1000/sample_hz).
# 500 Hz = 2 ms/step, the fastest whole-millisecond step with a plausible
# firmware time budget (DAC write + averaged ADC read + USB transmit per
# step; 4 ms/step is bench-proven, 2 ms is pending bench verification —
# check the waveform on a scope and that the starved_steps diagnostic stays
# 0). The old 250 Hz ceiling was set by the host's USB feed rate, which
# batched ring-buffer feeding has since lifted ~10x.
SAFE_SAMPLE_HZ_MAX = 500

# Target wall-clock duration of one measurement block delivered by the
# stream. Smaller blocks = faster stop-button response and more frequent GUI
# updates; larger blocks = less per-block Python overhead.
_TARGET_BLOCK_DURATION_S = 0.5

# Number of waveform samples pushed to the live plot per cycle. Dense enough
# to render a recognizable waveform shape without flooding the GUI queue.
# (The CSV always gets every sample, so post-hoc fidelity is unaffected.)
GUI_POINTS_PER_CYCLE = 30

# A sine needs enough samples per cycle for the staircase to resemble the
# requested waveform. Below this the output is badly distorted — and at the
# 2-point floor the samples land exactly on the sine's zero crossings,
# silently producing a flat 0 V output (observed on the bench July 2026).
MIN_SINE_POINTS_PER_CYCLE = 10


def _build_waveform_cycle(pos_peak_V, neg_peak_V, duty_cycle, waveform_type,
                          points_per_cycle):
    """Build one cycle of the voltage waveform as a numpy array."""
    t = np.linspace(0, 1, points_per_cycle, endpoint=False)

    if waveform_type == 'Square':
        return np.where(t < duty_cycle, pos_peak_V, neg_peak_V)

    # Sine: stretch positive/negative half-cycles by the duty cycle,
    # matching the phase mapping used by the AC electrolysis method.
    offset = (pos_peak_V + neg_peak_V) / 2.0
    amplitude = (pos_peak_V - neg_peak_V) / 2.0
    dc = np.clip(duty_cycle, 1e-6, 1.0 - 1e-6)
    phase = np.where(
        t < dc,
        (t / dc) * np.pi,
        np.pi + ((t - dc) / (1.0 - dc)) * np.pi,
    )
    return offset + amplitude * np.sin(phase)


def _calibrate_batch(raw_data, ps):
    """Convert raw write_voltage_batch output to calibrated (V, mA).

    write_voltage_batch returns col 0 = current (A), col 1 = potential (V)
    in the same raw scale as read_potential_current().  This applies the same
    calibration as read_potential_current_calibrated().
    """
    cal_v = raw_data[:, 1] * ps._calib_v_read_slope + ps._calib_v_read_intercept
    cal_i_mA = (raw_data[:, 0] * 1000.0) * ps._calib_read_slope + ps._calib_read_intercept
    return cal_v, cal_i_mA


def run_alternating_polarity(
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
    """Run one alternating-polarity electrolysis experiment to completion or stop.

    Unlike AC electrolysis (which targets a current via PID), this method
    directly controls the working-electrode potential through the firmware's
    batch-execution protocol, giving precise hardware-timed voltage waveforms.

    Stops on whichever limit is reached first: ``params['total_charge_C']`` of
    absolute charge or an optional ``params['total_time_s']`` time limit, or
    when the user triggers ``stop_event``.

    Live edits: the charge/time stop limits are re-read from ``params`` after
    each measurement block, so extending or shortening a run applies without a
    restart. The hardware-timed waveform itself cannot be retuned mid-stream,
    so a waveform change arrives as a ``control`` switch request — the loop returns
    ``OUTCOME_METHOD_CHANGE`` carrying its charge/elapsed and the worker restarts
    the method with the new waveform. ``initial_charge_C``/``initial_elapsed_s``
    seed the totals when taking over from a previous method.
    """
    pos_peak_V = params.get('ap_pos_peak_V', 1.0)
    neg_peak_V = params.get('ap_neg_peak_V', -1.0)
    frequency_hz = params.get('ap_frequency_hz', 1.0)
    duty_cycle = params.get('ap_duty_cycle', 0.5)
    waveform_type = params.get('ap_waveform', 'Square')
    sample_hz = params.get('ap_sample_hz', SAFE_SAMPLE_HZ_MAX)
    # Run stops on whichever limit (charge or time) is reached first.
    charge_limit_C, time_limit_s = resolve_stop_limits(params)

    # Clamp the sample rate to protect against ring-buffer starvation (see
    # module docstring). A warning is logged so the user can see their
    # requested rate was reduced.
    if sample_hz > SAFE_SAMPLE_HZ_MAX:
        logger.warning(
            "[PS %s] Requested ap_sample_hz=%.0f Hz exceeds safe ceiling; clamping to %s Hz.",
            pot_id, sample_hz, SAFE_SAMPLE_HZ_MAX,
        )
        sample_hz = SAFE_SAMPLE_HZ_MAX

    # --- Timing ---
    delay_ms = max(1, int(round(1000 / sample_hz)))
    actual_sample_hz = 1000.0 / delay_ms
    points_per_cycle = max(2, int(round(actual_sample_hz / frequency_hz)))
    dt = delay_ms / 1000.0  # seconds per data point

    # --- Waveform ---
    one_cycle = _build_waveform_cycle(
        pos_peak_V, neg_peak_V, duty_cycle, waveform_type, points_per_cycle,
    )

    # Refuse waveforms the sample rate cannot actually draw, before any
    # hardware is energized. Checking the built cycle (not just the ratio)
    # also catches a duty cycle so extreme the square loses one level.
    if waveform_type == 'Square':
        if not (np.any(one_cycle == pos_peak_V) and np.any(one_cycle == neg_peak_V)):
            raise ValueError(
                f"Square wave at {frequency_hz} Hz with {duty_cycle:.0%} duty has no "
                f"samples on one of its two levels at {actual_sample_hz:.0f} Hz "
                f"sampling. Raise the sample rate, lower the frequency, or use a "
                f"less extreme duty cycle."
            )
    elif points_per_cycle < MIN_SINE_POINTS_PER_CYCLE:
        max_f = actual_sample_hz / MIN_SINE_POINTS_PER_CYCLE
        raise ValueError(
            f"Sine at {frequency_hz} Hz gets only {points_per_cycle} points per "
            f"cycle at {actual_sample_hz:.0f} Hz sampling (minimum "
            f"{MIN_SINE_POINTS_PER_CYCLE}). Lower the frequency to {max_f:.1f} Hz "
            f"or below, or raise the sample rate."
        )
    # Apply the same voltage set calibration that write_potential_calibrated
    # uses, so the actual WE-RE potential matches the user's request. The
    # driver tiles this single cycle endlessly while streaming.
    v_slope = ps._calib_v_slope if abs(ps._calib_v_slope) > 1e-6 else 1.0
    cycle_calibrated = (one_cycle - ps._calib_v_intercept) / v_slope

    # Measurements arrive in short blocks (multiple of the protocol's 6-point
    # transfer granularity) so stop/live-edit checks stay responsive.
    block_points = max(6, int(round((_TARGET_BLOCK_DURATION_S / dt) / 6)) * 6)

    logger.info(
        "[PS %s] Starting Alternating Polarity (%s): %s V / %s V, %s Hz, Duty %.1f%%",
        pot_id, waveform_type, pos_peak_V, neg_peak_V, frequency_hz, duty_cycle * 100,
    )
    logger.info(
        "[PS %s] Sample rate: %.0f Hz (%d pts/cycle, %d ms/step, %d pts/block)",
        pot_id, actual_sample_hz, points_per_cycle, delay_ms, block_points,
    )

    # Seeded so a hand-off from a previous method keeps a continuous count.
    total_charge_passed = float(initial_charge_C)
    total_elapsed = 0.0
    outcome = OUTCOME_COMPLETED

    # Tracks back-to-back failed stream attempts so a persistent comm problem
    # aborts the run while a transient glitch only restarts the stream.
    consecutive_stream_failures = 0

    try:
        # Inside the try so a failure while energizing still runs the
        # shutdown steps in the finally block.
        ps.write_gain(0)  # 100 ohm resistor for mA-range currents
        ps.write_current_hold_stop()

        # --- Soft-start ramp: OCP -> first waveform voltage ---
        # Connect at the cell's resting potential so contact itself is spike-free
        start_V = one_cycle[0]
        ocp = close_switch_at_ocp(ps)

        ramp_step = POTENTIAL_RAMP_STEP_V if start_V >= ocp else -POTENTIAL_RAMP_STEP_V
        ramp_v = ocp
        while (ramp_step > 0 and ramp_v < start_V) or \
              (ramp_step < 0 and ramp_v > start_V):
            if stop_event.is_set():
                return OUTCOME_STOPPED
            if ramp_step > 0:
                ramp_v = min(ramp_v + ramp_step, start_V)
            else:
                ramp_v = max(ramp_v + ramp_step, start_V)
            ps.write_potential_calibrated(ramp_v)
            time.sleep(0.1)
        ps.write_potential_calibrated(start_V)

        # Reset elapsed time after the ramp so t=0 is the start of the waveform
        # (continuing any elapsed carried over from a previous method).
        total_elapsed = float(initial_elapsed_s)

        # --- Main loop: one continuous stream, restarted only on failure ---
        while not stop_event.is_set() and not should_stop(
                total_charge_passed, total_elapsed, charge_limit_C, time_limit_s):
            # Live edit: the user changed the method or the waveform. The
            # hardware-timed stream can't be retuned mid-flight, so hand off
            # and let the worker restart with the new settings.
            if control is not None and control.switch_requested():
                control.carry(total_charge_passed, total_elapsed)
                outcome = OUTCOME_METHOD_CHANGE
                break

            def on_block(raw):
                """Process one block of streamed measurements; False stops.

                Runs inside the driver's stream with the port lock held, so it
                must not issue any driver commands of its own.
                """
                nonlocal total_charge_passed, total_elapsed
                nonlocal charge_limit_C, time_limit_s, consecutive_stream_failures
                # Data is flowing, so any earlier failure was transient.
                consecutive_stream_failures = 0

                cal_v, cal_i_mA = _calibrate_batch(raw, ps)

                # Progressive charge integration across the block.
                abs_i_A = np.abs(cal_i_mA) / 1000.0
                cumulative_q = np.cumsum(abs_i_A) * dt + total_charge_passed

                # Push ~GUI_POINTS_PER_CYCLE samples per cycle to the live plot
                # so the waveform shape is visible; the CSV gets every point.
                n_points = len(cal_v)
                step = max(1, points_per_cycle // GUI_POINTS_PER_CYCLE)
                for idx in range(0, n_points, step):
                    output_queue.put((
                        total_elapsed + idx * dt, cal_i_mA[idx], cal_v[idx],
                        cumulative_q[idx],
                    ))

                # --- Write every data point to CSV for full fidelity ---
                if csv_writer:
                    times = total_elapsed + np.arange(n_points) * dt
                    for idx in range(n_points):
                        csv_writer.writerow([
                            round(times[idx], 6),
                            round(cal_i_mA[idx], 4),
                            round(cal_v[idx], 4),
                            round(cumulative_q[idx], 6),
                        ])

                total_charge_passed = cumulative_q[-1]
                total_elapsed += n_points * dt

                if signals:
                    # Report progress against whichever limit is the active one.
                    if charge_limit_C is not None:
                        progress = f"Running... {total_charge_passed:.2f} / {charge_limit_C:.2f} C"
                    else:
                        progress = f"Running... {total_elapsed:.0f} / {time_limit_s:.0f} s"
                    signals.status_updated.emit(pot_id, progress)

                # Live edit: re-read the stop limits (extend/shorten mid-run).
                charge_limit_C, time_limit_s = live_stop_limits(
                    params, charge_limit_C, time_limit_s)

                if stop_event.is_set():
                    return False
                if should_stop(total_charge_passed, total_elapsed,
                               charge_limit_C, time_limit_s):
                    return False
                if control is not None and control.switch_requested():
                    return False
                return True

            try:
                ps.stream_voltage_batch(cycle_calibrated, delay_ms, on_block,
                                        block_points)
            except (SerialReadTimeout, SerialAckError, ValueError) as e:
                # The driver halts the stream on its way out, so a restart
                # re-arms from a quiet link. Elapsed/charge only advance for
                # delivered blocks, keeping the accounting consistent with
                # what actually reached the cell.
                consecutive_stream_failures += 1
                msg = (f"WARNING: Waveform stream failed "
                       f"({consecutive_stream_failures}/{MAX_CONSECUTIVE_STREAM_FAILURES}): {e}")
                if signals:
                    signals.status_updated.emit(pot_id, msg)
                logger.warning("[PS %s] %s", pot_id, msg)
                if consecutive_stream_failures >= MAX_CONSECUTIVE_STREAM_FAILURES:
                    logger.error("[PS %s] Aborting: %d consecutive stream failures",
                                 pot_id, consecutive_stream_failures)
                    outcome = OUTCOME_COMM_FAILURE
                    break
            # A normal return means a callback condition fired (stop, limit
            # reached, or method change); the checks at the top of the loop
            # decide the outcome on re-entry.

        if should_stop(total_charge_passed, total_elapsed, charge_limit_C, time_limit_s):
            if charge_limit_C is not None and total_charge_passed >= charge_limit_C:
                logger.info("[PS %s] Target charge reached!", pot_id)
            else:
                logger.info("[PS %s] Time limit reached (%.1f s)", pot_id, total_elapsed)

        if outcome == OUTCOME_COMPLETED and stop_event.is_set():
            outcome = OUTCOME_STOPPED
        return outcome

    except Exception:
        logger.exception("[PS %s] Alternating polarity error", pot_id)
        raise

    finally:
        # Attempt every step even if one fails — the switch-off must not be
        # skipped because the zeroing write raised (see shutdown_hardware).
        # The stream is halted first so a still-running batch (comm-failure
        # abort) cannot drown the shutdown commands' acknowledgements, and
        # the switch is opened with the setpoint matched to the cell so the
        # disconnect itself is spike-free (see _safe_open_switch).
        shutdown_hardware(ps, pot_id, [
            ("halt stream", ps.halt_batch_stream),
            ("open switch", ps._safe_open_switch),
            ("zero potential", lambda: ps.write_potential_calibrated(0)),
        ], signals=signals)

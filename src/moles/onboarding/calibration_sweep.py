"""Hardware sequence for calibrating one potentiostat against a multimeter.

The user wires the potentiostat (WE/CE/RE) in series with a known resistor
and a current-measuring multimeter, then steps through a fixed list of
target currents. For each step the script asks the firmware to hold the
target current, the user reads the multimeter and types the actual measured
value, and a linear fit of measured-vs-target gives the per-board slope and
intercept used by all subsequent experiments.

This module is hardware-aware but UI-agnostic: it exposes small helpers that
the Qt worker thread calls in sequence. The worker is responsible for the
modal prompt to the user and for guaranteeing the switch is opened on every
exit path (success, cancel, exception).
"""

import time
from typing import Sequence, Tuple

import numpy as np


# Six-point ramp covering the hardware's useful current range. Positive only —
# negative-side calibration would require running through the zero crossing
# and is not currently part of the onboarding flow.
#
# The top point must stay well below the driver's command clamp (30 mA as of
# July 2026) and leave settling headroom: a point the hardware cannot actually
# reach within the settle time gets recorded against its requested value and
# skews the whole fit (this corrupted every calibration made with the old
# 30 mA top point, which the driver then silently clamped to 28 mA).
CALIBRATION_CURRENTS_MA: Tuple[float, ...] = (0.1, 1.0, 5.0, 10.0, 15.0, 20.0)

# Six-point voltage ramp for the voltage calibration sweep. Spans both
# polarities (unlike the current sweep, no zero crossing of a feedback loop is
# involved — the potential is set directly) and stays well inside the
# hardware's +/-5 V range. At the top point a 100-ohm dummy cell draws 20 mA,
# matching the current sweep's maximum.
CALIBRATION_POTENTIALS_V: Tuple[float, ...] = (-2.0, -1.2, -0.4, 0.4, 1.2, 2.0)

# Time the firmware holds each target before the user is asked for the
# multimeter reading. Long enough for the current-hold control loop to
# settle; short enough that a six-point sweep doesn't feel sluggish.
SETTLE_SECONDS = 2.0


def apply_and_hold(ps, current_mA: float) -> None:
    """Close the switch and command the firmware to hold ``current_mA``.

    Uses the raw (uncalibrated) ``write_current_hold`` because the goal of
    the sweep is to *measure* the per-board correction — applying the
    existing slope/intercept here would produce a circular calibration.
    """
    ps.write_switch(1)
    ps.write_current_hold(target_current_mA=current_mA)


def apply_and_hold_voltage(ps, potential_V: float) -> None:
    """Close the switch and apply ``potential_V`` through the raw DAC path.

    Uses the raw (uncalibrated) ``write_potential`` for the same reason the
    current sweep uses ``write_current_hold``: the fit must span the whole
    signal chain, so no existing correction may be applied on the way in.
    The 100-ohm shunt is selected explicitly since, unlike the current hold,
    a plain potential write does not set a gain of its own.
    """
    ps.write_gain(0)  # Resistors.R_100 — mA-scale shunt for the dummy cell
    ps.write_switch(1)
    ps.write_potential(potential_V)


def read_device_potential_V(ps, samples: int = 5, interval_s: float = 0.05) -> float:
    """Average the board's own potential reading at the applied point.

    Returns the *uncorrected* reading in volts. Paired with the multimeter
    value entered by the user, these give the voltage read calibration —
    the analogue of ``read_device_current_mA`` for the potential channel.
    """
    readings = []
    for _ in range(samples):
        readings.append(float(np.asarray(ps.read_potential()).flat[0]))
        time.sleep(interval_s)
    return sum(readings) / len(readings)


def read_device_current_mA(ps, samples: int = 5, interval_s: float = 0.05) -> float:
    """Average the board's own current reading at the settled hold point.

    Returns the *uncorrected* reading in nominal mA (raw amps x 1000). Paired
    with the multimeter value entered by the user, these readings give the
    second linear fit of the sweep: the read calibration, which maps what the
    board reports back to the true current during experiments.
    """
    readings = []
    for _ in range(samples):
        readings.append(float(np.asarray(ps.read_current()).flat[0]) * 1000.0)
        time.sleep(interval_s)
    return sum(readings) / len(readings)


def stop_point(ps) -> None:
    """End the current step safely.

    Always called from the worker's ``finally`` blocks, including on user
    cancel and on exception, so the potentiostat never stays energised after
    a calibration step ends. ``write_switch(0)`` is the project-wide
    fail-safe (see CLAUDE.md).
    """
    try:
        ps.write_current_hold_stop()
    except Exception:
        pass
    try:
        ps.write_switch(0)
    except Exception:
        pass


def fit(applied_mA: Sequence[float], measured_mA: Sequence[float]) -> Tuple[float, float, float]:
    """Linear least-squares fit of measured vs applied current.

    Returns ``(slope, intercept, r_squared)``. Slope and intercept are saved
    to the registry; R-squared is shown next to the plot so the user can see
    at a glance whether the multimeter readings were consistent.
    """
    if len(applied_mA) != len(measured_mA):
        raise ValueError("applied and measured arrays must have the same length")
    if len(applied_mA) < 2:
        raise ValueError("need at least two points to fit a line")

    x = np.asarray(applied_mA, dtype=float)
    y = np.asarray(measured_mA, dtype=float)

    slope, intercept = np.polyfit(x, y, 1)

    # R^2 = 1 - SS_res / SS_tot. Guard against the degenerate case where the
    # user enters the same measured value for every point (SS_tot == 0).
    y_pred = slope * x + intercept
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return float(slope), float(intercept), r_squared

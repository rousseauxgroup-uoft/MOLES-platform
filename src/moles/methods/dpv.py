"""Differential pulse voltammetry (DPV) waveform timing and signal processing.

A DPV experiment applies a staircase of potential steps; on top of each step a
short voltage *pulse* is added. The chemically meaningful signal is the
*difference* in measured current between the end of the pulse and the end of the
plain step — plotted against the step potential it produces a peak-shaped curve
whose peak height scales with concentration.

The raw data that streams off the hardware is just the current measured at every
sample, so it looks like a noisy staircase. The functions here turn that raw
stream into the differential curve (delta-i vs potential) that a chemist expects
to see.

These helpers operate purely on numpy arrays and the experiment parameters; they
do not touch the potentiostat or the driver, so they are safe to unit-test.
"""

import numpy as np


def samples_per_segment(potential_hold_ms, pulse_hold_ms, sample_hz):
    """Return how many samples make up one step-hold and one pulse-hold.

    The hardware writes one sample every ``step_ms`` milliseconds, so a hold of a
    given duration covers ``duration / step_ms`` samples. Mirrors the timing the
    driver uses when it builds the DPV waveform, so processing stays aligned with
    acquisition.

    Returns ``(n_step, n_pulse, step_ms)``.
    """
    step_ms = int(np.round(np.ceil(1000.0 / sample_hz)))
    n_step = max(1, int(np.round(potential_hold_ms / step_ms)))
    n_pulse = max(1, int(np.round(pulse_hold_ms / step_ms)))
    return n_step, n_pulse, step_ms


def cyclic_sweeps(min_V, max_V, cycles, direction='Positive',
                  start_V=None, end_V=None):
    """Return the ``(from_V, to_V)`` leg list for a cyclic DPV run.

    Mirrors the cyclic-voltammetry sweep shape: the scan begins at ``start_V``,
    heads to the first vertex, oscillates between ``min_V`` and ``max_V`` for
    ``cycles`` full there-and-back cycles, then leaves the last vertex for
    ``end_V``. ``direction`` sets the first move — ``'Positive'`` sweeps up to
    the maximum first, ``'Negative'`` sweeps down to the minimum first.

    ``min_V`` and ``max_V`` are ordered here, so which box holds which value does
    not matter. ``start_V`` and ``end_V`` default to that first vertex, giving a
    clean cycle that starts and ends there; a lead-in or lead-out leg of zero
    length (already sitting at the vertex) is dropped so no step is emitted
    twice. The result feeds straight into :func:`build_dpv_waveform`.
    """
    lo, hi = (min_V, max_V) if max_V >= min_V else (max_V, min_V)
    # The vertex the scan heads to first and returns to each cycle; the far
    # vertex is the turning point in between.
    apex, far = (hi, lo) if direction == 'Positive' else (lo, hi)
    if start_V is None:
        start_V = apex
    if end_V is None:
        end_V = apex

    points = [start_V, apex]
    for _ in range(max(1, int(cycles))):
        points += [far, apex]
    points.append(end_V)

    # Consecutive points become legs; drop any zero-length leg (a lead-in or
    # lead-out that already sits at its vertex).
    return [(a, b) for a, b in zip(points[:-1], points[1:]) if a != b]


def build_dpv_waveform(sweeps, pulse_V, step_V, potential_hold_ms,
                       pulse_hold_ms, sample_hz):
    """Build the per-sample target-potential array for a DPV run.

    Each entry in ``sweeps`` is a ``(from_V, to_V)`` staircase; the sweeps run
    back to back, so a single-sweep DPV is ``[(start, end)]`` and a cyclic DPV
    is a list of legs — a lead-in, the cycle turns, and a lead-out (see
    :func:`cyclic_sweeps`). Within a sweep the potential steps from ``from_V``
    toward ``to_V`` in ``|step_V|``
    increments, and on top of every step a pulse of height ``pulse_V`` is
    applied *in the direction of that sweep* — added while scanning up,
    subtracted while scanning down, which is the DPV convention. Each step is
    held for ``potential_hold_ms`` and each pulse for ``pulse_hold_ms``; a
    single trailing step-hold parks the DAC at the final level.

    The result is laid out as consecutive (step-hold, pulse-hold) periods that
    line up exactly with :func:`process_dpv`, so acquisition and the
    differential analysis stay aligned.

    Returns a 1-D ``float32`` array of target potentials, one entry per sample
    (empty if ``sweeps`` is empty).
    """
    n_step, n_pulse, _ = samples_per_segment(
        potential_hold_ms, pulse_hold_ms, sample_hz
    )
    step_V = abs(step_V) or 0.001  # guard against a zero step (flat staircase)

    pieces = []
    last_level = None
    for v_from, v_to in sweeps:
        direction = 1.0 if v_to >= v_from else -1.0
        n_levels = max(2, int(np.round(abs(v_to - v_from) / step_V)))
        levels = np.linspace(v_from, v_to, n_levels)
        # Drop the last level of each sweep: it becomes the first step of the
        # next sweep (cyclic), or the trailing park-hold (single / final sweep),
        # so no step is emitted twice at a turning point.
        for level in levels[:-1]:
            pieces.append(np.full(n_step, level, dtype=np.float32))
            pieces.append(np.full(n_pulse, level + direction * pulse_V,
                                  dtype=np.float32))
        last_level = levels[-1]

    if last_level is not None:
        pieces.append(np.full(n_step, last_level, dtype=np.float32))
    if not pieces:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(pieces)


def process_dpv(potential, current, potential_hold_ms, pulse_hold_ms,
                sample_hz, sample_window=4):
    """Convert a raw DPV current stream into a differential voltammogram.

    Walks the raw data one (step-hold + pulse-hold) period at a time. For each
    period it averages the last few samples of the step-hold (the baseline
    current) and the last few samples of the pulse-hold (the pulsed current),
    and records their difference against the step potential. Averaging the tail
    of each segment lets the capacitive (non-faradaic) current decay first, which
    is the whole point of the pulse technique.

    Args:
        potential: 1-D array of measured potential at every sample (V).
        current: 1-D array of measured current at every sample (uA).
        potential_hold_ms: time held at each staircase step (ms).
        pulse_hold_ms: time held at the pulsed potential (ms).
        sample_hz: sampling rate used during acquisition (Hz).
        sample_window: number of trailing samples averaged in each segment.

    Returns:
        Nx2 array with column 0 = step potential (V), column 1 = delta-i (uA).
        Empty (shape ``(0, 2)``) if there is not even one full period of data.
    """
    potential = np.asarray(potential, dtype=float)
    current = np.asarray(current, dtype=float)

    n_step, n_pulse, _ = samples_per_segment(
        potential_hold_ms, pulse_hold_ms, sample_hz
    )
    period = n_step + n_pulse
    n_periods = len(current) // period
    if n_periods == 0:
        return np.empty((0, 2))

    # Average at most as many points as the shorter segment can supply.
    win = max(1, min(sample_window, n_step, n_pulse))

    out = np.empty((n_periods, 2))
    for k in range(n_periods):
        base = k * period
        step_end = base + n_step          # last sample of the plain step-hold
        pulse_end = base + period         # last sample of the pulse-hold

        i_step = np.mean(current[step_end - win:step_end])
        i_pulse = np.mean(current[pulse_end - win:pulse_end])
        v_step = np.mean(potential[step_end - win:step_end])

        out[k] = [v_step, i_pulse - i_step]
    return out

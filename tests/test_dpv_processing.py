"""Unit tests for the DPV differential processing helpers."""

import numpy as np

from moles.methods.dpv import (
    build_dpv_waveform,
    cyclic_sweeps,
    process_dpv,
    samples_per_segment,
)


def test_samples_per_segment_basic():
    # At 1000 Hz each sample is 1 ms, so segment lengths equal the hold times.
    n_step, n_pulse, step_ms = samples_per_segment(200, 50, 1000)
    assert step_ms == 1
    assert n_step == 200
    assert n_pulse == 50


def test_samples_per_segment_rounds_step_ms():
    # At 250 Hz the per-sample period rounds up to 4 ms.
    n_step, n_pulse, step_ms = samples_per_segment(200, 40, 250)
    assert step_ms == 4
    assert n_step == 50
    assert n_pulse == 10


def _make_staircase(step_potentials, n_step, n_pulse, pulse_height, slope):
    """Build a fake (potential, current) stream for a DPV sweep.

    Current is modelled as a linear function of applied potential, so the
    pulse adds a constant ``slope * pulse_height`` on every pulse segment and
    the differential should recover that constant at every step.
    """
    potential, current = [], []
    for v in step_potentials:
        potential.extend([v] * n_step)
        current.extend([slope * v] * n_step)
        potential.extend([v + pulse_height] * n_pulse)
        current.extend([slope * (v + pulse_height)] * n_pulse)
    return np.array(potential), np.array(current)


def test_process_dpv_recovers_constant_difference():
    steps = np.linspace(-0.2, 0.6, 9)
    slope, pulse = 10.0, 0.05
    n_step, n_pulse, _ = samples_per_segment(200, 50, 1000)
    potential, current = _make_staircase(steps, n_step, n_pulse, pulse, slope)

    out = process_dpv(potential, current, 200, 50, 1000)
    assert out.shape == (len(steps), 2)
    # delta-i should equal slope * pulse at every step.
    assert np.allclose(out[:, 1], slope * pulse, atol=1e-6)
    # Step potentials should be recovered.
    assert np.allclose(out[:, 0], steps, atol=1e-6)


def test_process_dpv_peak_location():
    # A current that rises sharply near 0.2 V gives the largest delta-i there.
    steps = np.linspace(-0.2, 0.6, 41)
    n_step, n_pulse, _ = samples_per_segment(40, 20, 1000)
    pulse = 0.05
    potential, current = [], []
    for v in steps:
        base = 1.0 / (1.0 + np.exp(-(v - 0.2) / 0.02))
        pulsed = 1.0 / (1.0 + np.exp(-((v + pulse) - 0.2) / 0.02))
        potential.extend([v] * n_step + [v + pulse] * n_pulse)
        current.extend([base] * n_step + [pulsed] * n_pulse)

    out = process_dpv(np.array(potential), np.array(current), 40, 20, 1000)
    peak_v = out[np.argmax(out[:, 1]), 0]
    assert abs(peak_v - 0.2) < 0.03


def test_process_dpv_insufficient_data_returns_empty():
    out = process_dpv(np.zeros(3), np.zeros(3), 200, 50, 1000)
    assert out.shape == (0, 2)


def test_cyclic_sweeps_cycles_between_vertices():
    # With no explicit Start/End V the scan starts and ends at the first vertex
    # (Max, for the default positive direction) and runs full Max<->Min cycles.
    assert cyclic_sweeps(-0.5, 1.0, 1) == [(1.0, -0.5), (-0.5, 1.0)]
    assert cyclic_sweeps(-0.5, 1.0, 2) == [
        (1.0, -0.5), (-0.5, 1.0), (1.0, -0.5), (-0.5, 1.0)]


def test_cyclic_sweeps_orders_min_and_max():
    # Passing the values high-then-low makes no difference: they are sorted.
    assert cyclic_sweeps(1.0, -0.5, 1) == cyclic_sweeps(-0.5, 1.0, 1)


def test_cyclic_sweeps_negative_initial_direction():
    # A negative initial direction heads down to Min first and cycles there.
    assert cyclic_sweeps(-0.5, 1.0, 1, direction='Negative') == [
        (-0.5, 1.0), (1.0, -0.5)]


def test_cyclic_sweeps_start_and_end_lead_in_out():
    # Start V and End V add a lead-in and lead-out, CV-style: begin at Start V,
    # cycle between the vertices, finish at End V.
    assert cyclic_sweeps(-0.5, 1.0, 1, start_V=0.0, end_V=0.2) == [
        (0.0, 1.0), (1.0, -0.5), (-0.5, 1.0), (1.0, 0.2)]
    # A lead-in that already sits at the first vertex is dropped, not emitted
    # as a zero-length leg.
    assert cyclic_sweeps(-0.5, 1.0, 1, start_V=1.0, end_V=1.0) == [
        (1.0, -0.5), (-0.5, 1.0)]


def test_build_dpv_waveform_single_roundtrip():
    # A single sweep whose current is a linear function of potential should,
    # once differentiated, recover a constant delta-i of slope * pulse.
    sweeps = [(0.0, 0.4)]
    pulse, step, slope = 0.05, 0.1, 3.0
    waveform = build_dpv_waveform(
        sweeps, pulse_V=pulse, step_V=step,
        potential_hold_ms=10, pulse_hold_ms=5, sample_hz=1000,
    )
    out = process_dpv(waveform, slope * waveform, 10, 5, 1000)
    # linspace(0, 0.4, 4) has 4 levels; the last is the park-hold, so 3 pulses.
    assert out.shape[0] == 3
    assert np.allclose(out[:, 1], slope * pulse, atol=1e-6)


def test_build_dpv_waveform_cyclic_reverses_pulse_direction():
    # On the forward segment the pulse adds to the step (delta-i > 0); on the
    # reverse segment it subtracts (delta-i < 0). Both must appear.
    sweeps = cyclic_sweeps(0.0, 0.4, 2)
    pulse, step, slope = 0.05, 0.1, 3.0
    waveform = build_dpv_waveform(
        sweeps, pulse_V=pulse, step_V=step,
        potential_hold_ms=10, pulse_hold_ms=5, sample_hz=1000,
    )
    di = process_dpv(waveform, slope * waveform, 10, 5, 1000)[:, 1]
    assert np.any(np.isclose(di, slope * pulse, atol=1e-6))
    assert np.any(np.isclose(di, -slope * pulse, atol=1e-6))


def test_build_dpv_waveform_empty_sweeps():
    waveform = build_dpv_waveform(
        [], pulse_V=0.05, step_V=0.1,
        potential_hold_ms=10, pulse_hold_ms=5, sample_hz=1000,
    )
    assert waveform.shape == (0,)

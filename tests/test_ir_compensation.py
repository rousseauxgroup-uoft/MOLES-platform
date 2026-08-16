"""Tests for iR compensation: the driver methods, the R_u worker, and the UI wiring.

Everything runs against the mock potentiostat — no hardware involved.
"""

import threading
from pathlib import Path

import numpy as np
import pytest

from moles.driver.mock import MockPotentiostat
from moles.driver.ps4_ref import (HW_VOLTAGE_COMPLIANCE_V, Potentiostat,
                                  Resistors, SerialReadTimeout)


# ----------------------------------------------------------------------
# Driver: waveform, correction arithmetic, guards
# ----------------------------------------------------------------------

def _reference_waveform(min_V, max_V, cycles, step_V, start_V, last_V, direction):
    """The waveform code as it was written inline in perform_CV, for comparison."""
    cv = []
    if direction == 'Positive':
        first, turn_a, turn_b = max_V, min_V, max_V
    else:
        first, turn_a, turn_b = min_V, max_V, min_V
    r1 = np.abs(first - start_V)
    if r1 > 0:
        cv = np.append(cv, np.linspace(start_V, first, int(np.round(r1 / step_V))))
    p2 = np.linspace(first, turn_a, int(np.round(np.abs(turn_a - first) / step_V)))
    p3 = np.linspace(turn_a, turn_b, int(np.round(np.abs(turn_b - turn_a) / step_V)))
    for _ in range(cycles):
        cv = np.append(cv, p2)
        cv = np.append(cv, p3)
    r4 = np.abs(last_V - turn_b)
    if r4 > 0:
        cv = np.append(cv, np.linspace(turn_b, last_V, int(np.round(r4 / step_V))))
    return cv


@pytest.mark.parametrize("direction", ['Negative', 'Positive'])
@pytest.mark.parametrize("cycles", [1, 3])
def test_build_cv_waveform_matches_original_inline_code(direction, cycles):
    """The extraction out of perform_CV must not have changed the sweep."""
    ref = _reference_waveform(-0.5, 0.8, cycles, 0.001, 0.0, 0.0, direction)
    new = Potentiostat._build_cv_waveform(
        None, -0.5, 0.8, cycles, 0.001, 0.0, 0.0, direction)
    assert np.array_equal(np.asarray(ref, dtype=float), np.asarray(new, dtype=float))


def test_build_cv_waveform_skips_zero_length_segments():
    """Starting exactly at the first turning point drops the lead-in segment."""
    with_lead = Potentiostat._build_cv_waveform(
        None, -0.5, 0.5, 1, 0.001, 0.0, 0.0, 'Negative')
    without_lead = Potentiostat._build_cv_waveform(
        None, -0.5, 0.5, 1, 0.001, -0.5, -0.5, 'Negative')
    assert len(without_lead) < len(with_lead)


class _IRMock(MockPotentiostat):
    """Mock cell with the real iR methods grafted on, plus gain tracking."""

    _build_cv_waveform = Potentiostat._build_cv_waveform
    _fit_ru_decay = staticmethod(Potentiostat._fit_ru_decay)
    _acquire_step_trace = Potentiostat._acquire_step_trace
    measure_R_u = Potentiostat.measure_R_u
    perform_CV_iR = Potentiostat.perform_CV_iR

    def __init__(self):
        super().__init__()
        self._port_lock = threading.RLock()
        self._auto_gain = False
        self.Resistor = Resistors.R_100
        self.applied = []
        self.auto_gain_history = []

    def write_gain(self, gain=Resistors.R_100):
        # The real driver records the active resistor; the plain mock does not.
        self.Resistor = gain

    def write_auto_gain(self, state=False):
        self._auto_gain = state
        self.auto_gain_history.append(bool(state))
        return state

    def write_potential_calibrated(self, potential_V):
        self.applied.append(float(potential_V))
        super().write_potential_calibrated(potential_V)


CV_ARGS = dict(min_V=-0.2, max_V=0.2, cycles=1, mV_s=1000.0, step_hz=1000.0,
               start_V=0.0, last_V=0.0)

# Disable the feedback guards so a test can check the correction arithmetic
# itself: a 1-sample median with full weighting and no slew limit reproduces
# plain previous-point feedback.
RAW_LOOP = dict(feedback_median=1, feedback_alpha=1.0,
                correction_slew_V=np.inf)


def test_cv_ir_returns_perform_cv_column_format():
    ps = _IRMock()
    data = ps.perform_CV_iR(R_u_ohm=0.0, **CV_ARGS)
    assert data.shape[1] == 6
    assert np.all(np.diff(data[:, 0]) >= 0)          # time increases
    assert ps._switch_state is False                  # cell left open


@pytest.mark.parametrize("fraction", [0.0, 0.5, 0.9, 1.0])
def test_compensation_fraction_scales_the_correction(fraction):
    ps = _IRMock()
    R_u = 20.0
    data = ps.perform_CV_iR(R_u_ohm=R_u, compensation_fraction=fraction,
                            **RAW_LOOP, **CV_ARGS)
    cv = data[:, 5]
    I_A = data[:, 2] / 1e6
    applied = np.array(ps.applied[1:])   # index 0 is the pre-switch park

    expected = cv.copy()
    expected[1:] = cv[1:] + I_A[:-1] * R_u * fraction
    assert np.allclose(applied, expected, atol=1e-9)


def test_default_compensation_is_below_full():
    """The discrete loop eats stability margin, so the default backs off to 0.7."""
    ps = _IRMock()
    data = ps.perform_CV_iR(R_u_ohm=20.0, **RAW_LOOP, **CV_ARGS)
    cv = data[:, 5]
    I_A = data[:, 2] / 1e6
    applied = np.array(ps.applied[1:])
    expected = cv.copy()
    expected[1:] = cv[1:] + I_A[:-1] * 20.0 * 0.7
    assert np.allclose(applied, expected, atol=1e-9)


def test_correction_slew_is_limited_per_point():
    """The correction term may walk but never jump between points."""
    ps = _IRMock()
    slew = 0.010
    data = ps.perform_CV_iR(R_u_ohm=20000.0, compensation_fraction=1.0,
                            feedback_median=1, feedback_alpha=1.0,
                            correction_slew_V=slew, **CV_ARGS)
    applied = np.array(ps.applied[1:])
    correction = applied - data[:, 5]
    step_V = 1.0 / 1000.0   # 1000 mV/s at 1000 points/s
    assert np.max(np.abs(np.diff(correction))) <= slew + 2 * step_V + 1e-9


def test_auto_gain_is_frozen_during_the_scan_and_restored():
    """A mid-wave range change must not feed the loop a mis-scaled sample."""
    ps = _IRMock()
    ps.write_auto_gain(True)
    ps.auto_gain_history.clear()
    ps.perform_CV_iR(R_u_ohm=10.0, **CV_ARGS)
    assert ps.auto_gain_history[0] is False    # frozen before the sweep
    assert ps.auto_gain_history[-1] is True    # restored afterwards
    assert ps._auto_gain is True


def test_dac_parked_before_switch_closes():
    """The cell must not see a stale setpoint when the switch closes."""
    ps = _IRMock()
    data = ps.perform_CV_iR(R_u_ohm=10.0, **CV_ARGS)
    assert ps.applied[0] == pytest.approx(data[0, 5])


def test_overestimated_r_u_is_capped_at_compliance():
    ps = _IRMock()
    ps.perform_CV_iR(R_u_ohm=5000.0, compensation_fraction=1.0, **CV_ARGS)
    applied = np.abs(np.array(ps.applied))
    assert np.all(applied <= HW_VOLTAGE_COMPLIANCE_V + 1e-9)
    assert ps._switch_state is False


def test_transient_dropped_readings_are_survived():
    ps = _IRMock()
    ps.trigger_serial_timeout(count=3)     # below the abort threshold
    data = ps.perform_CV_iR(R_u_ohm=10.0, max_consecutive_timeouts=5, **CV_ARGS)
    assert data.shape[0] > 0
    assert ps._switch_state is False


def test_sustained_dropouts_abort_with_the_cell_switched_off():
    ps = _IRMock()
    ps.trigger_serial_timeout(count=50)
    with pytest.raises(SerialReadTimeout):
        ps.perform_CV_iR(R_u_ohm=10.0, max_consecutive_timeouts=5, **CV_ARGS)
    assert ps._switch_state is False


def test_step_beyond_compliance_is_refused():
    ps = _IRMock()
    with pytest.raises(ValueError):
        ps.measure_R_u(step_mV=6000.0, n_readings=5, settle_s=0.0)
    assert ps._switch_state is False


def test_measure_r_u_restores_gain_settings():
    ps = _IRMock()
    ps.write_gain(Resistors.R_10K)
    ps.write_auto_gain(True)
    result = ps.measure_R_u(step_mV=50.0, n_readings=10, settle_s=0.0)

    # The legacy keys must survive alongside the fit diagnostics
    assert {'R_u_ohm', 'fit_ok', 'message', 'polarities',
            'step_V', 'I_peak_A', 'times_ms', 'currents_A'} <= set(result)
    assert len(result['currents_A']) == 10
    assert ps.Resistor == Resistors.R_10K
    assert ps._auto_gain is True
    assert ps._switch_state is False


def test_measure_r_u_steps_both_polarities():
    """Alternating the step direction cancels the drift a repeated one-sided
    step leaves behind (bench: ten repeats wandered 1056 -> 250 ohm)."""
    ps = _IRMock()
    result = ps.measure_R_u(step_mV=50.0, n_readings=10, settle_s=0.0)

    assert len(result['polarities']) == 2
    steps = [p['step_V'] for p in result['polarities']]
    assert steps[0] == pytest.approx(+0.050)
    assert steps[1] == pytest.approx(-0.050)
    # The mock cell is purely resistive, so both steps give a usable estimate
    assert result['fit_ok'] is True
    assert np.isfinite(result['R_u_ohm']) and result['R_u_ohm'] > 0


def test_measure_r_u_uses_serial_burst_without_capture_support():
    """A mock with no CMD_STEP_CAPTURE support must take the legacy path."""
    ps = _IRMock()
    result = ps.measure_R_u(step_mV=50.0, n_readings=10, settle_s=0.0)
    assert all(p['source'] == 'serial' for p in result['polarities'])


class _FwCaptureMock(_IRMock):
    """Mock whose firmware 'has' CMD_STEP_CAPTURE: step_capture returns a
    synthetic exponential decay encoded as counts around a midpoint, and the
    converter decodes them back to mA."""

    _step_capture_supported = None
    TAU_MS = 5.0
    I0_MA = 0.25       # current through R_u at t=0
    RESIDUAL_MA = 0.02

    def __init__(self):
        super().__init__()
        self.capture_calls = 0

    def step_capture(self, n_samples, period_us):
        # Capture 1 is the zero-step control (artifact-free in this mock),
        # then the positive and negative steps.
        self.capture_calls += 1
        t_ms = (np.arange(n_samples) + 0.5) * period_us / 1000.0
        if self.capture_calls == 1:
            i_mA = np.zeros(n_samples)
        else:
            sign = 1.0 if self.capture_calls == 2 else -1.0
            i_mA = sign * (self.I0_MA * np.exp(-t_ms / self.TAU_MS)
                           + self.RESIDUAL_MA)
        counts = (32768 + i_mA * 10000.0).astype(np.uint16)
        return counts, n_samples * period_us, 0

    def _capture_counts_to_mA(self, counts, gain_idx):
        return (np.asarray(counts, dtype=float) - 32768) / 10000.0


def test_measure_r_u_prefers_the_firmware_capture():
    """With capture-capable firmware, the fit sees the transient from t=0
    and recovers R_u = step / I(0)."""
    ps = _FwCaptureMock()
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)

    assert all(p['source'] == 'firmware' for p in result['polarities'])
    assert ps._step_capture_supported is True
    assert result['fit_ok'] is True
    expected = 0.05 / ((ps.I0_MA + ps.RESIDUAL_MA) / 1000.0)
    assert result['R_u_ohm'] == pytest.approx(expected, rel=0.05)
    assert ps._switch_state is False


class _OffsetFwMock(_FwCaptureMock):
    """Symmetric cell plus a constant +0.1 mA reading offset in the captured
    traces only — the switch-open baseline read does not see it, as with a
    calibration-intercept error the baseline reads share."""

    OFFSET_MA = 0.1

    def step_capture(self, n_samples, period_us):
        counts, dur, gain = super().step_capture(n_samples, period_us)
        # The offset appears only after the control capture — modelling an
        # offset the template subtraction cannot remove, which the combined
        # estimator must still cancel.
        if self.capture_calls >= 2:
            counts = (counts.astype(np.int64)
                      + int(self.OFFSET_MA * 10000)).astype(np.uint16)
        return counts, dur, gain


def test_reading_offset_cancels_in_the_combined_estimate():
    """A constant offset skews the two single-sided estimates in opposite
    directions (bench: 85 ohm reported where ~140 compensated best), but
    cancels exactly in the difference of the two t=0 currents."""
    ps = _OffsetFwMock()
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)

    expected = 0.05 / ((ps.I0_MA + ps.RESIDUAL_MA) / 1000.0)
    per_pol = sorted(p['R_u_ohm'] for p in result['polarities'])
    assert per_pol[1] > 1.25 * per_pol[0]      # single-sided values skewed
    assert result['fit_ok'] is True
    assert result['R_u_ohm'] == pytest.approx(expected, rel=0.05)
    assert "offset" in result['message']


class _BaselineOffsetMock(_FwCaptureMock):
    """Offset visible to BOTH the switch-open baseline read and the capture,
    as a genuinely drifted current zero appears."""

    OFFSET_MA = 0.08

    def read_current_calibrated(self):
        if not self._switch_state:
            return np.array([self.OFFSET_MA])
        return super().read_current_calibrated()

    def step_capture(self, n_samples, period_us):
        counts, dur, gain = super().step_capture(n_samples, period_us)
        counts = (counts.astype(np.int64)
                  + int(self.OFFSET_MA * 10000)).astype(np.uint16)
        return counts, dur, gain


def test_switch_open_baseline_is_subtracted_from_each_trace():
    """The zero-current reading taken with the switch open must be removed
    from the trace, making even the single-sided estimates come out right."""
    ps = _BaselineOffsetMock()
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)

    expected = 0.05 / ((ps.I0_MA + ps.RESIDUAL_MA) / 1000.0)
    for p in result['polarities']:
        assert p['baseline_A'] == pytest.approx(ps.OFFSET_MA / 1000.0, rel=1e-6)
        assert p['R_u_ohm'] == pytest.approx(expected, rel=0.05)
    assert result['R_u_ohm'] == pytest.approx(expected, rel=0.05)


class _ArtifactFwMock(_FwCaptureMock):
    """A one-sample instrument spike rides on every capture, control
    included — the structure the bench traces showed (0.565 mA first
    sample against a ~0.2 mA transient)."""

    SPIKE_MA = 0.4

    def step_capture(self, n_samples, period_us):
        counts, dur, gain = super().step_capture(n_samples, period_us)
        counts = counts.astype(np.int64)
        counts[0] += int(self.SPIKE_MA * 10000)
        return counts.astype(np.uint16), dur, gain


def test_switch_close_artifact_is_subtracted_via_the_control():
    """The control capture measures the artifact with zero driving force;
    subtracting it must leave both step fits on the true value instead of
    the spike-dominated one."""
    ps = _ArtifactFwMock()
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)

    expected = 0.05 / ((ps.I0_MA + ps.RESIDUAL_MA) / 1000.0)
    assert result['fit_ok'] is True
    assert result['R_u_ohm'] == pytest.approx(expected, rel=0.05)
    assert float(np.max(np.abs(result['control']['currents_A']))) > 3e-4


def test_ru_traces_are_saved_for_diagnosis(qapp, tmp_path, monkeypatch):
    """Every measurement's decay traces land in ru_traces/ so a suspect
    value can be diagnosed from what the fit actually saw."""
    import moles.ui.electroanalysis.workers as workers
    monkeypatch.setattr(workers, "LOG_DIR", tmp_path)
    from moles.ui.electroanalysis.workers import RuWorker

    result = _FwCaptureMock().measure_R_u(step_mV=50.0, settle_s=0.0)
    RuWorker('A', 'MOCK_A', use_mock=True)._save_traces(result)

    files = list((tmp_path / "ru_traces").glob("RU_PS-A_*.csv"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "R_u_ohm" in text and "+50 mV" in text and "baseline" in text
    assert "control" in text
    data = np.genfromtxt(files[0], delimiter=",")
    assert data.shape[1] == 3                      # step, time, current
    expected_rows = (len(result['control']['times_ms'])
                     + sum(len(p['times_ms']) for p in result['polarities']))
    assert data.shape[0] == expected_rows


class _NoCaptureMock(_IRMock):
    """Mock that advertises unknown capture support but whose step_capture
    times out, as an old firmware build does."""

    _step_capture_supported = None

    def __init__(self):
        super().__init__()
        self.capture_calls = 0

    def step_capture(self, n_samples, period_us):
        self.capture_calls += 1
        raise SerialReadTimeout("no ack")


def test_capture_probe_failure_falls_back_and_is_cached():
    """Old firmware costs one probe timeout, then the serial burst is used
    for the rest of the connection without re-probing."""
    ps = _NoCaptureMock()
    result = ps.measure_R_u(step_mV=50.0, n_readings=10, settle_s=0.0)

    assert ps.capture_calls == 1                  # probed once, not per step
    assert ps._step_capture_supported is False
    assert all(p['source'] == 'serial' for p in result['polarities'])
    assert np.isfinite(result['R_u_ohm'])


def test_capture_counts_convert_through_the_read_calibration():
    """The count conversion must follow read_current_calibrated's chain."""
    ps = Potentiostat(serial_port='UNCONNECTED')
    # ADC mid-scale is 1.65 V, which maps to +0.0494 V physical (the default
    # voltage offset), i.e. -0.494 raw mA through the 100-ohm gain, then the
    # default read calibration (x0.9769 - 0.6).
    mA = ps._capture_counts_to_mA(np.array([32768]), Resistors.R_100)
    assert mA[0] == pytest.approx(-1.0833, rel=1e-3)
    # More counts = more positive WE_OUT volts = more negative current (the
    # transimpedance stage inverts), so the mapping must be monotonic down.
    seq = ps._capture_counts_to_mA(np.array([20000, 32768, 45000]), Resistors.R_100)
    assert seq[0] > seq[1] > seq[2]


# ----------------------------------------------------------------------
# The decay fit behind measure_R_u
# ----------------------------------------------------------------------

def _decay_trace(R_u, tau_ms, step_V=0.05, residual_A=0.0, n=30,
                 t0_ms=3.0, dt_ms=3.5, noise_A=0.0, seed=0):
    """Synthetic post-step transient: I(t) = (step/R)*exp(-t/tau) + residual."""
    rng = np.random.default_rng(seed)
    t = t0_ms + np.arange(n) * dt_ms
    i = (step_V / R_u) * np.exp(-t / tau_ms) + residual_A
    return t, i + rng.normal(0, noise_A, n)


def test_fit_recovers_r_u_from_a_partial_decay():
    """The first sample lands one serial round trip after the step, well down
    the decay; the fit must reach back to t=0 where argmax cannot."""
    t, i = _decay_trace(R_u=250.0, tau_ms=8.0, noise_A=2e-6)
    fit = Potentiostat._fit_ru_decay(t, i, 0.05)

    assert fit['ok'] is True
    assert fit['R_u_ohm'] == pytest.approx(250.0, rel=0.05)
    # argmax on the same trace lands far off, because the peak was missed
    argmax_R = abs(0.05 / i[np.argmax(np.abs(i))])
    assert argmax_R > 1.2 * 250.0


def test_fit_refuses_a_decay_too_fast_to_extrapolate():
    t, i = _decay_trace(R_u=250.0, tau_ms=2.0, residual_A=3e-6)
    fit = Potentiostat._fit_ru_decay(t, i, 0.05)
    assert fit['ok'] is False
    assert not np.isfinite(fit['R_u_ohm'])
    assert "too fast" in fit['message']


def test_fit_treats_a_flat_trace_as_resistive():
    """A dummy-cell resistor shows no transient at all; that is a valid
    measurement, but the message says what was assumed."""
    rng = np.random.default_rng(1)
    t = 3.0 + np.arange(30) * 3.5
    i = np.full(30, 5e-4) + rng.normal(0, 5e-6, 30)
    fit = Potentiostat._fit_ru_decay(t, i, 0.05)
    assert fit['ok'] is True
    assert fit['R_u_ohm'] == pytest.approx(100.0, rel=0.05)
    assert "resistive" in fit['message']


def test_fit_refuses_when_no_current_flows():
    t = 3.0 + np.arange(30) * 3.5
    fit = Potentiostat._fit_ru_decay(t, np.zeros(30), 0.05)
    assert fit['ok'] is False
    assert not np.isfinite(fit['R_u_ohm'])


def test_fit_sees_a_transient_buried_in_a_long_capture():
    """A sub-millisecond transient inside a 150 ms firmware capture must
    reach the fit — a head-mean flatness check diluted it into a 'purely
    resistive' verdict on the bench."""
    t, i = _decay_trace(R_u=250.0, tau_ms=0.3, residual_A=5e-6, n=3000,
                        t0_ms=0.025, dt_ms=0.05, noise_A=1e-6)
    fit = Potentiostat._fit_ru_decay(t, i, 0.05)
    assert fit['ok'] is True
    assert fit['R_u_ohm'] == pytest.approx(0.05 / (0.05 / 250.0 + 5e-6), rel=0.05)


class _AsymFlatMock(_IRMock):
    """Both polarities flat but wildly different — a faradaically active
    cell whose transient is too fast to see, not a resistor."""

    _step_capture_supported = None

    def __init__(self):
        super().__init__()
        self.capture_calls = 0

    def step_capture(self, n_samples, period_us):
        self.capture_calls += 1
        if self.capture_calls == 1:      # zero-step control
            i_mA = 0.0
        else:
            i_mA = 0.5 if self.capture_calls == 2 else 0.005
        counts = np.full(n_samples, 32768 + int(i_mA * 10000), dtype=np.uint16)
        return counts, n_samples * period_us, 0

    def _capture_counts_to_mA(self, counts, gain_idx):
        return (np.asarray(counts, dtype=float) - 32768) / 10000.0


def test_two_flat_polarities_that_disagree_are_refused():
    """A genuine resistance is symmetric; asymmetric flat currents mean
    sustained faradaic current and must not be averaged into a number
    (bench 2026-08-14: 104 and 7803 ohm averaged to 3953)."""
    ps = _AsymFlatMock()
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)
    assert result['fit_ok'] is False
    assert not np.isfinite(result['R_u_ohm'])
    assert "faradaic" in result['message']


class _MixedPolarityMock(_IRMock):
    """One polarity shows a clean transient, the other only sustained
    faradaic current."""

    _step_capture_supported = None
    TAU_MS = 5.0

    def __init__(self):
        super().__init__()
        self.capture_calls = 0

    def step_capture(self, n_samples, period_us):
        self.capture_calls += 1
        t_ms = (np.arange(n_samples) + 0.5) * period_us / 1000.0
        if self.capture_calls == 1:      # zero-step control
            i_mA = np.zeros(n_samples)
        elif self.capture_calls == 2:    # first polarity: real decay
            i_mA = 0.25 * np.exp(-t_ms / self.TAU_MS) + 0.02
        else:                            # second polarity: flat faradaic
            i_mA = np.full(n_samples, 0.005)
        counts = (32768 + i_mA * 10000.0).astype(np.uint16)
        return counts, n_samples * period_us, 0

    def _capture_counts_to_mA(self, counts, gain_idx):
        return (np.asarray(counts, dtype=float) - 32768) / 10000.0


def test_flat_polarity_is_ignored_when_the_other_fits():
    ps = _MixedPolarityMock()
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)
    assert result['fit_ok'] is True
    assert result['R_u_ohm'] == pytest.approx(0.05 / 0.00027, rel=0.05)
    assert "ignored" in result['message']


# ----------------------------------------------------------------------
# Voltage calibration on the batch paths
# ----------------------------------------------------------------------

def test_batch_voltage_calibration_defaults_to_identity():
    """The raw pipeline already carries the legacy +/-0.0494 correction, so
    a board without a stored voltage calibration must get identity here —
    the old +0.0494 defaults double-applied the offset (bench 2026-08-14:
    asymmetric R_u steps, +49 mV live-CV axis)."""
    ps = Potentiostat(serial_port='UNCONNECTED')
    v = np.array([-0.5, 0.0, 1.0])
    assert np.allclose(ps.calibrate_batch_potential_V(v), v)
    assert np.allclose(ps._waveform_to_raw_V(v), v)
    assert ps._calib_v_intercept == 0.0
    assert ps._calib_v_read_intercept == 0.0


def test_batch_voltage_calibration_applies_stored_pairs():
    ps = Potentiostat(serial_port='UNCONNECTED')
    ps.set_voltage_calibration_params(1.02, 0.01, 1.03, -0.02)
    assert ps.calibrate_batch_potential_V(np.array([0.5]))[0] == pytest.approx(0.5 * 1.03 - 0.02)
    assert ps._waveform_to_raw_V(np.array([0.5]))[0] == pytest.approx((0.5 - 0.01) / 1.02)


# ----------------------------------------------------------------------
# Live callback and stop support
# ----------------------------------------------------------------------

def test_live_callback_receives_every_point_in_blocks():
    ps = _IRMock()
    blocks = []
    data = ps.perform_CV_iR(R_u_ohm=10.0,
                            data_callback=lambda v, i: blocks.append((v, i)),
                            **CV_ARGS)
    emitted_v = np.concatenate([b[0] for b in blocks])
    emitted_i = np.concatenate([b[1] for b in blocks])

    assert len(emitted_v) == data.shape[0]
    assert np.allclose(emitted_v, np.round(data[:, 1], 5), atol=1e-6)
    assert np.allclose(emitted_i, np.round(data[:, 2], 5), atol=1e-6)
    # Batched, not one signal per point
    assert len(blocks) < data.shape[0] / 2


def test_stop_event_truncates_every_column():
    ps = _IRMock()
    stop = threading.Event()
    seen = {'n': 0}

    def stop_after_200(v, i):
        seen['n'] += len(v)
        if seen['n'] >= 200:
            stop.set()

    data = ps.perform_CV_iR(R_u_ohm=10.0, data_callback=stop_after_200,
                            stop_event=stop, **CV_ARGS)

    full = len(ps._build_cv_waveform(-0.2, 0.2, 1, 0.001, 0.0, 0.0, 'Negative'))
    assert 200 <= data.shape[0] < full
    assert data.shape[1] == 6
    assert np.all(data[1:, 0] > 0)        # no zero-padding left behind
    assert ps._switch_state is False


def test_stop_before_first_point_returns_empty_trace():
    ps = _IRMock()
    stop = threading.Event()
    stop.set()
    data = ps.perform_CV_iR(R_u_ohm=10.0, stop_event=stop, **CV_ARGS)
    assert data.shape[0] == 0
    assert ps._switch_state is False


# ----------------------------------------------------------------------
# UI wiring
# ----------------------------------------------------------------------

@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    import moles.ui.electroanalysis.workers as workers
    monkeypatch.setattr(workers, "get_cv_data_folder", lambda: tmp_path)

    from moles.ui.electroanalysis.main_window import MainWindow
    win = MainWindow()
    win.cv_tab.test_mode_cb.setChecked(True)
    return win


def test_ir_compensation_is_off_by_default(window):
    params = window.cv_tab._panels['A'].get_params()
    assert params['ir_enabled'] is False
    assert params['ir_mode'] == 'posthoc'
    assert params['compensation_fraction'] == 0.7


def test_ir_params_reach_the_worker(window):
    config = window.cv_tab._panels['A'].config
    config.ir_box.setChecked(True)
    config.r_u.setValue(142.0)
    config.ir_percent.setValue(85)
    config.ir_mode.setCurrentIndex(config.ir_mode.findData('live'))

    params = window.cv_tab._panels['A'].get_params()
    assert params['ir_enabled'] is True
    assert params['ir_mode'] == 'live'
    assert params['R_u_ohm'] == pytest.approx(142.0)
    assert params['compensation_fraction'] == pytest.approx(0.85)


def test_fraction_only_applies_to_live_mode(window):
    """Post-hoc always subtracts the full drop, so the percent box is
    disabled unless the live loop is selected."""
    config = window.cv_tab._panels['A'].config
    config.ir_box.setChecked(True)
    assert config.ir_percent.isEnabled() is False
    config.ir_mode.setCurrentIndex(config.ir_mode.findData('live'))
    assert config.ir_percent.isEnabled() is True


def test_ir_run_streams_and_saves(window):
    from moles.ui.electroanalysis.workers import VoltammetryWorker

    config = window.cv_tab._panels['A'].config
    config.ir_box.setChecked(True)
    config.r_u.setValue(200.0)

    params = window.cv_tab._panels['A'].get_params()
    worker = VoltammetryWorker('A', 'MOCK_A', params, use_mock=True)
    captured = {}
    worker.signals.finished.connect(
        lambda p, s, m, r, fp: captured.update(success=s, msg=m, result=r, fp=fp))
    worker.run()

    assert captured['success'] is True
    assert len(captured['result']['raw_v']) > 0
    assert Path(captured['fp']).exists()
    # The parameters file records what the correction actually used
    params_text = Path(captured['fp']).with_suffix('.params.txt').read_text()
    assert "ir_enabled: True" in params_text
    assert "R_u_ohm: 200.0" in params_text


def test_ir_run_without_r_u_is_refused(window):
    from moles.ui.electroanalysis.workers import VoltammetryWorker

    config = window.cv_tab._panels['A'].config
    config.ir_box.setChecked(True)
    config.r_u.setValue(0.0)          # never measured

    params = window.cv_tab._panels['A'].get_params()
    worker = VoltammetryWorker('A', 'MOCK_A', params, use_mock=True)
    captured = {}
    worker.signals.finished.connect(
        lambda p, s, m, r, fp: captured.update(success=s, msg=m))
    worker.run()

    assert captured['success'] is False
    assert "R_u" in captured['msg']


def test_r_u_worker_measures_and_reports(window):
    from moles.ui.electroanalysis.workers import RuWorker

    worker = RuWorker('A', 'MOCK_A', use_mock=True)
    captured = {}
    worker.signals.finished.connect(
        lambda p, ok, msg, r: captured.update(ok=ok, msg=msg, r_u=r))
    worker.run()

    assert captured['ok'] is True
    assert captured['r_u'] > 0


def test_measured_r_u_lands_in_the_form(window):
    window.cv_tab._on_r_u_measured('A', True, "R_u = 175.0 ohm", 175.0)
    assert window.cv_tab._panels['A'].config.r_u.value() == pytest.approx(175.0)


def test_failed_r_u_measurement_re_enables_the_button(window):
    panel = window.cv_tab._panels['A']
    panel.set_measuring_ru(True)
    assert panel.config.measure_ru_btn.isEnabled() is False

    window.cv_tab._on_r_u_measured('A', False, "No current flowed", None)
    assert panel.config.measure_ru_btn.isEnabled() is True
    assert panel.run_btn.isEnabled() is True


# ----------------------------------------------------------------------
# The reported potential must be the one the electrode experienced
# ----------------------------------------------------------------------

def test_interfacial_potential_subtracts_the_ohmic_drop():
    from moles.ui.electroanalysis.workers import VoltammetryWorker

    # 1 mA through 150 ohm is a 150 mV drop
    phi = VoltammetryWorker._interfacial_potential(
        measured_V=[0.500], current_uA=[1000.0], R_u=150.0)
    assert phi[0] == pytest.approx(0.350)

    # Reduction: negative current, so the drop is subtracted the other way
    phi = VoltammetryWorker._interfacial_potential(
        measured_V=[-0.500], current_uA=[-1000.0], R_u=150.0)
    assert phi[0] == pytest.approx(-0.350)


def test_zero_current_leaves_the_potential_untouched():
    from moles.ui.electroanalysis.workers import VoltammetryWorker
    phi = VoltammetryWorker._interfacial_potential([0.25, -0.25], [0.0, 0.0], 500.0)
    assert phi == pytest.approx([0.25, -0.25])


def _peak_separation(v, i):
    """Potential gap between the largest and smallest current in a trace."""
    return abs(float(v[np.argmax(i)]) - float(v[np.argmin(i)]))


def _run_ir(window, tmp_path, R_u, fraction=0.7, mode='posthoc'):
    from moles.ui.electroanalysis.workers import VoltammetryWorker

    config = window.cv_tab._panels['A'].config
    config.ir_box.setChecked(True)
    config.r_u.setValue(R_u)
    config.ir_percent.setValue(int(fraction * 100))
    config.ir_mode.setCurrentIndex(config.ir_mode.findData(mode))

    params = window.cv_tab._panels['A'].get_params()
    worker = VoltammetryWorker('A', 'MOCK_A', params, use_mock=True)
    captured = {}
    chunks = []
    worker.signals.data_chunk.connect(lambda p, v, i: chunks.append((v, i)))
    worker.signals.finished.connect(
        lambda p, s, m, r, fp: captured.update(success=s, result=r, fp=fp))
    worker.run()
    return captured, chunks


def test_saved_potential_is_corrected_not_the_applied_overshoot(window, tmp_path):
    """Peaks must not appear further apart because of the applied overshoot.

    Runs the live loop: only there does the instrument deliberately overshoot,
    which is the axis error this test guards against. (The mock's plain CV
    contains no ohmic drop, so the post-hoc path is checked separately.)
    """
    R_u = 20000.0     # large enough to dominate the mock's synthetic noise
    captured, _ = _run_ir(window, tmp_path, R_u, mode='live')
    assert captured['success'] is True

    data = np.genfromtxt(captured['fp'], delimiter=',', skip_header=1)
    corrected_V, current_uA = data[:, 1], data[:, 2]
    # What the instrument sensed, reconstructed from the saved trace
    applied_V = corrected_V + current_uA * 1e-6 * R_u

    assert (_peak_separation(corrected_V, current_uA)
            < _peak_separation(applied_V, current_uA))


def test_live_plot_receives_the_corrected_potential(window, tmp_path):
    R_u = 20000.0
    captured, chunks = _run_ir(window, tmp_path, R_u, mode='live')

    streamed_V = np.concatenate([c[0] for c in chunks])
    streamed_I = np.concatenate([c[1] for c in chunks])
    data = np.genfromtxt(captured['fp'], delimiter=',', skip_header=1)

    # The live stream and the saved file must agree on the potential axis
    assert len(streamed_V) == data.shape[0]
    assert np.allclose(streamed_V, data[:, 1], atol=1e-4)
    assert (_peak_separation(streamed_V, streamed_I)
            < _peak_separation(streamed_V + streamed_I * 1e-6 * R_u, streamed_I))


def test_posthoc_stream_and_file_share_the_corrected_axis(window, tmp_path):
    """In the default mode the live plot and the saved file must both carry
    the same, already-corrected potential."""
    captured, chunks = _run_ir(window, tmp_path, 500.0, mode='posthoc')
    assert captured['success'] is True

    streamed_V = np.concatenate([c[0] for c in chunks])
    data = np.genfromtxt(captured['fp'], delimiter=',', skip_header=1)
    assert len(streamed_V) == data.shape[0]
    assert np.allclose(streamed_V, data[:, 1], atol=1e-4)


def test_ir_files_declare_the_corrected_column(window, tmp_path):
    captured, _ = _run_ir(window, tmp_path, 500.0)
    header = Path(captured['fp']).read_text().splitlines()[0]
    assert "iR-corrected" in header
    # A comma inside a column name would shift every column after it
    assert len(header.split(',')) == 6


def test_plain_cv_header_is_unchanged(window, tmp_path):
    from moles.ui.electroanalysis.workers import VoltammetryWorker

    params = window.cv_tab._panels['A'].get_params()   # iR off by default
    worker = VoltammetryWorker('A', 'MOCK_A', params, use_mock=True)
    captured = {}
    worker.signals.finished.connect(
        lambda p, s, m, r, fp: captured.update(fp=fp))
    worker.run()

    header = Path(captured['fp']).read_text().splitlines()[0]
    assert header == "Time (s),Potential (V),Current (uA),Cycle,Index,Target V"


def test_corrected_files_still_load_in_the_analysis_tab(window, tmp_path):
    """The renamed column must not break the Analysis tab's column detection."""
    from moles.ui.electroanalysis.loader import KIND_VOLTAMMOGRAM, load_trace

    captured, _ = _run_ir(window, tmp_path, 500.0)
    trace = load_trace(captured['fp'])

    data = np.genfromtxt(captured['fp'], delimiter=',', skip_header=1)
    assert np.allclose(trace.x, data[:, 1])      # picked the corrected column
    assert np.allclose(trace.y, data[:, 2])
    assert trace.y_label == 'Current (uA)'
    assert trace.kind == KIND_VOLTAMMOGRAM
    # The corrected column keeps its own name, so an edited export still says
    # the potential axis was corrected.
    assert trace.x_label == "Potential iR-corrected (V)"


def test_step_size_note_warns_at_coarse_scan_rates(window):
    from moles.ui.electroanalysis.widgets import IR_COARSE_STEP_MV, IR_STEP_HZ

    config = window.cv_tab._panels['A'].config
    config.ir_box.setChecked(True)
    config.ir_mode.setCurrentIndex(config.ir_mode.findData('live'))

    config.scan_rate.setValue(100.0)          # fine steps
    assert "coarse" not in config.ir_note.text()

    config.scan_rate.setValue(IR_COARSE_STEP_MV * IR_STEP_HZ * 2)
    assert "coarse" in config.ir_note.text()


def test_posthoc_note_states_the_axis_only_caveat(window):
    """The default mode's note must say the electrode is not corrected."""
    config = window.cv_tab._panels['A'].config
    config.ir_box.setChecked(True)
    assert config.ir_mode.currentData() == 'posthoc'
    assert "electrode" in config.ir_note.text()


def test_live_mode_runs_and_saves(window, tmp_path):
    """The advanced live loop stays reachable through the worker."""
    captured, chunks = _run_ir(window, tmp_path, 500.0, mode='live')
    assert captured['success'] is True
    assert len(chunks) > 0
    header = Path(captured['fp']).read_text().splitlines()[0]
    assert "iR-corrected" in header

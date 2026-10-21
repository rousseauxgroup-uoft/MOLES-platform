"""Tests for iR compensation: the driver methods, the R_u worker, and the UI wiring.

Everything runs against the mock potentiostat — no hardware involved.
"""

import threading
from pathlib import Path

import numpy as np
import pytest

from moles.driver.mock import MockLivePotentiostat, MockPotentiostat
from moles.driver.ps4_ref import (FW_FEATURE_DAC_STEP, HW_VOLTAGE_COMPLIANCE_V,
                                  RU_ADC_WINDOW_MS, RU_DAC_FIT_START_MS,
                                  RU_FILTER_TAUS_MS, RU_MIN_BLANK_MS,
                                  RU_PRE_SAMPLES, Potentiostat, Resistors,
                                  SerialReadTimeout, _format_ms)


# ----------------------------------------------------------------------
# Driver: waveform, correction arithmetic, guards
# ----------------------------------------------------------------------

class _IRMock(MockPotentiostat):
    """Mock cell with the real iR methods grafted on, plus gain tracking."""

    _build_cv_waveform = Potentiostat._build_cv_waveform
    _fit_ru_decay = staticmethod(Potentiostat._fit_ru_decay)
    _fit_ru_steps = staticmethod(Potentiostat._fit_ru_steps)
    _fit_ru_step_response = staticmethod(Potentiostat._fit_ru_step_response)
    _filtered_step_response = staticmethod(Potentiostat._filtered_step_response)
    _ru_blank_ms = staticmethod(Potentiostat._ru_blank_ms)
    _check_step_targets = Potentiostat._check_step_targets
    _switch_step_traces = Potentiostat._switch_step_traces
    _dac_step_traces = Potentiostat._dac_step_traces
    _acquire_step_trace = Potentiostat._acquire_step_trace
    _quiet_wait = Potentiostat._quiet_wait
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


# No quiet time: these tests look at the scan itself, not the hold before it
CV_ARGS = dict(min_V=-0.2, max_V=0.2, cycles=1, mV_s=1000.0, step_hz=1000.0,
               start_V=0.0, last_V=0.0, quiet_s=0)

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
    assert {'R_u_ohm', 'fit_ok', 'message', 'fit', 'polarities', 'control',
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
    """Symmetric cell plus a constant +0.1 mA reading offset in every capture
    that the switch-open baseline read does not see, as with a
    calibration-intercept error the baseline reads share."""

    OFFSET_MA = 0.1

    def step_capture(self, n_samples, period_us):
        counts, dur, gain = super().step_capture(n_samples, period_us)
        counts = (counts.astype(np.int64)
                  + int(self.OFFSET_MA * 10000)).astype(np.uint16)
        return counts, dur, gain


def test_reading_offset_cancels_in_the_difference():
    """A constant offset skews each step's own estimate in opposite
    directions (bench: 85 ohm reported where ~140 compensated best), but
    cancels in the difference of the two traces."""
    ps = _OffsetFwMock()
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)

    expected = 0.05 / ((ps.I0_MA + ps.RESIDUAL_MA) / 1000.0)
    assert result['fit_ok'] is True, result['message']
    assert result['R_u_ohm'] == pytest.approx(expected, rel=0.05)


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
    """The zero-current reading taken with the switch open is removed from
    every saved trace, so each settles on its own residual current."""
    ps = _BaselineOffsetMock()
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)

    expected = 0.05 / ((ps.I0_MA + ps.RESIDUAL_MA) / 1000.0)
    for p in result['polarities']:
        assert p['baseline_A'] == pytest.approx(ps.OFFSET_MA / 1000.0, rel=1e-6)
        assert abs(np.mean(p['currents_A'][-100:])) == pytest.approx(
            ps.RESIDUAL_MA / 1000.0, rel=0.05)
    assert result['R_u_ohm'] == pytest.approx(expected, rel=0.05)


class _RecoveryFwMock(_FwCaptureMock):
    """The amplifier's recovery from the switch closing as the bench traces
    showed it (2026-09-27): ~0.5 mA in the first sample and some left in the
    second, its sign following the way the amplifier was pushed, so the
    zero-step control's differs from each step's."""

    RECOVERY_MA = {1: (-0.49, -0.05), 2: (+0.45, +0.2), 3: (-0.53, -0.17)}

    def step_capture(self, n_samples, period_us):
        counts, dur, gain = super().step_capture(n_samples, period_us)
        counts = counts.astype(np.int64)
        for k, extra in enumerate(self.RECOVERY_MA[self.capture_calls]):
            counts[k] += int(extra * 10000)
        return counts.astype(np.uint16), dur, gain


def test_switch_close_recovery_is_skipped_not_subtracted():
    """Subtracting the control's recovery doubled it on the step of the other
    sign, and that step's fit latched onto the spike ('decay too fast',
    bench 2026-09-27); skipping the recovery leaves the true value."""
    ps = _RecoveryFwMock()
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)

    expected = 0.05 / ((ps.I0_MA + ps.RESIDUAL_MA) / 1000.0)
    assert result['fit_ok'] is True, result['message']
    assert result['R_u_ohm'] == pytest.approx(expected, rel=0.05)
    assert result['fit']['blank_ms'] == pytest.approx(RU_MIN_BLANK_MS)
    # The saved control keeps its spike, for diagnosis
    assert float(np.max(np.abs(result['control']['currents_A']))) > 3e-4


def _capture_times(n=6000, period_ms=0.025):
    return (np.arange(n) + 0.5) * period_ms


def test_recovery_length_is_read_off_the_control():
    """The bench's two-sample recovery keeps the minimum skip; a slower one
    extends it."""
    t = _capture_times()
    rng = np.random.default_rng(3)
    quick = rng.normal(0, 4e-6, t.size)
    quick[:2] += (-4.9e-4, -5.4e-5)
    assert Potentiostat._ru_blank_ms(t, quick) == pytest.approx(RU_MIN_BLANK_MS)

    slow = rng.normal(0, 4e-6, t.size) + 5e-4 * np.exp(-t / 0.05)
    assert Potentiostat._ru_blank_ms(t, slow) > 0.1


def test_a_recovery_longer_than_the_transient_is_refused():
    t = _capture_times()
    control = {'times_ms': t, 'currents_A': 5e-4 * np.exp(-t / 0.3),
               'source': 'firmware'}
    up = {'times_ms': t, 'currents_A': 2.7e-4 * np.exp(-t / 5.0), 'step_V': 0.05}
    down = {'times_ms': t, 'currents_A': -2.7e-4 * np.exp(-t / 5.0), 'step_V': -0.05}
    fit = Potentiostat._fit_ru_steps(up, down, control=control)
    assert fit['ok'] is False
    assert not np.isfinite(fit['R_u_ohm'])
    assert "to settle" in fit['message']


def test_short_durations_are_shown_in_microseconds():
    """A 25 us sample interval once printed as '0.0 ms'."""
    assert _format_ms(0.0224) == "22 µs"
    assert _format_ms(2.5) == "2.5 ms"


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
    """Both steps flat and in their own directions, but 25-fold apart — a
    faradaically active cell whose transient is too fast to see, not a
    resistor."""

    _step_capture_supported = None

    def __init__(self):
        super().__init__()
        self.capture_calls = 0

    def step_capture(self, n_samples, period_us):
        self.capture_calls += 1
        i_mA = {1: 0.0, 2: 0.5, 3: -0.02}[self.capture_calls]
        counts = np.full(n_samples, 32768 + int(i_mA * 10000), dtype=np.uint16)
        return counts, n_samples * period_us, 0

    def _capture_counts_to_mA(self, counts, gain_idx):
        return (np.asarray(counts, dtype=float) - 32768) / 10000.0


def test_steps_that_draw_very_different_currents_are_refused():
    """A genuine resistance is symmetric; lopsided currents mean sustained
    faradaic current and must not be turned into a number (bench 2026-08-14:
    104 and 7803 ohm averaged to 3953)."""
    ps = _AsymFlatMock()
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)
    assert result['fit_ok'] is False
    assert not np.isfinite(result['R_u_ohm'])
    assert "faradaic" in result['message']


class _WrongWayMock(_IRMock):
    """The first step shows a clean transient; the second draws a small
    current the wrong way, as in the tail of a faradaic wave."""

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
        elif self.capture_calls == 2:    # +50 mV: real decay
            i_mA = 0.25 * np.exp(-t_ms / self.TAU_MS) + 0.02
        else:                            # -50 mV: small, and positive
            i_mA = np.full(n_samples, 0.02)
        counts = (32768 + i_mA * 10000.0).astype(np.uint16)
        return counts, n_samples * period_us, 0

    def _capture_counts_to_mA(self, counts, gain_idx):
        return (np.asarray(counts, dtype=float) - 32768) / 10000.0


def test_step_drawing_current_the_wrong_way_is_refused():
    ps = _WrongWayMock()
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)
    assert result['fit_ok'] is False
    assert not np.isfinite(result['R_u_ohm'])
    assert result['message'].startswith("the -50 mV step drew current against")


class _SpikeOnlyMock(_WrongWayMock):
    """The second step's only current is its recovery spike."""

    def step_capture(self, n_samples, period_us):
        if self.capture_calls == 2:          # about to take the second step
            self.capture_calls += 1
            i_mA = np.zeros(n_samples)
            i_mA[0] = 0.25
            counts = (32768 - i_mA * 10000.0).astype(np.uint16)
            return counts, n_samples * period_us, 0
        return super().step_capture(n_samples, period_us)


def test_a_step_with_nothing_after_its_recovery_is_refused():
    """Bench 2026-08-15: fits of such a step read 20-50 us 'time constants'
    that were really the amplifier recovering."""
    ps = _SpikeOnlyMock()
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)
    assert result['fit_ok'] is False
    assert not np.isfinite(result['R_u_ohm'])
    assert ("-50 mV step drew" in result['message']
            or "very different currents" in result['message'])


def test_long_status_message_does_not_widen_the_panel(qapp):
    """An unwrapped refusal message once stretched the CV panel ~1500 px wide,
    and on macOS the iR box then centred its rows off-screen."""
    from moles.ui.electroanalysis.widgets import PotentiostatPanel

    panel = PotentiostatPanel('A', 'CV')
    narrow = panel.minimumSizeHint().width()
    panel.update_status("Error: " + "a long refusal message " * 20)
    assert panel.minimumSizeHint().width() <= narrow + 10


# ----------------------------------------------------------------------
# R_u by DAC steps with the cell connected (firmware 1.7+)
# ----------------------------------------------------------------------

def _board_reading(t_ms, A, tau_ms, C, taus=RU_FILTER_TAUS_MS,
                   window_ms=RU_ADC_WINDOW_MS):
    """Exact reading of the cell current ``A*exp(-t/tau) + C`` (from t=0)
    through first-order filters ``taus``, averaged over each ADC window:
    partial fractions, independent of the driver's numerical model."""
    t = np.asarray(t_ms, dtype=float)
    b = [1.0 / x for x in taus]
    gain = float(np.prod(b))

    def terms(first_pole):
        poles = [first_pole] + [-x for x in b]
        out = []
        for k, pk in enumerate(poles):
            den = np.prod([pk - pj for j, pj in enumerate(poles) if j != k])
            out.append((gain / den, pk))
        return out

    def averaged(r, p):
        lo = np.clip(t - window_ms / 2, 0.0, None)
        hi = t + window_ms / 2
        if p == 0:
            return r * (hi - lo) / window_ms
        return r * (np.exp(p * hi) - np.exp(p * lo)) / (p * window_ms)

    y = np.zeros_like(t)
    for r, p in terms(-1.0 / tau_ms):
        y += A * averaged(r, p)
    for r, p in terms(0.0):
        y += C * averaged(r, p)
    return np.where(t > -window_ms / 2, y, 0.0)


def _dac_step_trace(R, tau_ms, step_V, leak_A=0.0, noise_A=0.0, seed=0,
                    centre_ms=0.005, n=6000):
    rng = np.random.default_rng(seed)
    t = (np.arange(n) - RU_PRE_SAMPLES) * 0.025 + centre_ms
    i = _board_reading(t, step_V / R, tau_ms, leak_A)
    return t, i + rng.normal(0.0, noise_A, n) if noise_A else i


def test_step_response_model_matches_the_exact_filters():
    t = np.linspace(-0.2, 1.0, 97)
    exact = _board_reading(t, 2e-4, 0.3, 1e-5)
    model = Potentiostat._filtered_step_response(np.clip(t, 0.0, None), 2e-4, 0.3, 1e-5)
    model[t < -RU_ADC_WINDOW_MS / 2] = 0.0
    assert np.max(np.abs(model - exact)) < 1e-3 * 2.1e-4


@pytest.mark.parametrize("tau_ms", [0.1, 0.3, 0.7, 5.0])
def test_dac_step_fit_sees_through_the_board_filters(tau_ms):
    """The fit models the board's filters, so it recovers R_u whatever the
    cell's time constant."""
    t, i_up = _dac_step_trace(250.0, tau_ms, 0.05, leak_A=2e-5)
    _, i_down = _dac_step_trace(250.0, tau_ms, -0.05, leak_A=-2e-5)
    up = {'times_ms': t, 'currents_A': i_up, 'step_V': 0.05, 'pre_std_A': 0.0}
    down = {'times_ms': t, 'currents_A': i_down, 'step_V': -0.05, 'pre_std_A': 0.0}
    fit = Potentiostat._fit_ru_steps(up, down)
    expected = 0.1 / (0.1 / 250.0 + 4e-5)
    assert fit['ok'] is True, fit['message']
    assert fit['R_u_ohm'] == pytest.approx(expected, rel=0.01)
    assert fit['blank_ms'] == pytest.approx(RU_DAC_FIT_START_MS)


def test_ignoring_the_filters_misreads_a_typical_cell():
    """On a 0.3 ms cell a plain exponential reads R_u high when fitted from
    30 us (the filtered rise drags it) and low from 100 us (the filters delay
    the whole decay)."""
    t, i_up = _dac_step_trace(250.0, 0.3, 0.05)
    _, i_down = _dac_step_trace(250.0, 0.3, -0.05)
    diff = i_up - i_down
    early = (t >= RU_DAC_FIT_START_MS) & (t <= RU_DAC_FIT_START_MS + 1.0)
    late = (t >= 0.1) & (t <= 1.1)
    assert Potentiostat._fit_ru_decay(t[early], diff[early], 0.1)['R_u_ohm'] > 1.1 * 250.0
    assert Potentiostat._fit_ru_decay(t[late], diff[late], 0.1)['R_u_ohm'] < 0.93 * 250.0


def test_dac_step_on_a_plain_resistor():
    t, i_up = _dac_step_trace(465.0, 1e9, 0.05, noise_A=4e-6, seed=1)
    _, i_down = _dac_step_trace(465.0, 1e9, -0.05, noise_A=4e-6, seed=2)
    up = {'times_ms': t, 'currents_A': i_up, 'step_V': 0.05, 'pre_std_A': 4e-6}
    down = {'times_ms': t, 'currents_A': i_down, 'step_V': -0.05, 'pre_std_A': 4e-6}
    fit = Potentiostat._fit_ru_steps(up, down)
    assert fit['ok'] is True, fit['message']
    assert fit['R_u_ohm'] == pytest.approx(465.0, rel=0.02)
    assert "resistive" in fit['message']


def test_dac_step_refuses_a_current_lost_in_the_noise():
    """50 mV into ~10 kohm draws about one reading's noise on range 0."""
    t, i_up = _dac_step_trace(9920.0, 1e9, 0.05, noise_A=4e-6, seed=3)
    _, i_down = _dac_step_trace(9920.0, 1e9, -0.05, noise_A=4e-6, seed=4)
    up = {'times_ms': t, 'currents_A': i_up, 'step_V': 0.05, 'pre_std_A': 4e-6}
    down = {'times_ms': t, 'currents_A': i_down, 'step_V': -0.05, 'pre_std_A': 4e-6}
    fit = Potentiostat._fit_ru_steps(up, down)
    assert fit['ok'] is False
    assert not np.isfinite(fit['R_u_ohm'])
    assert "noise" in fit['message']


class _DacStepMock(_IRMock):
    """Firmware 1.7 board: DAC-step captures of a 250 ohm, 0.3 ms cell with
    a small faradaic leak, riding on a +30 uA reading offset that the zero
    before each step must remove."""

    R, TAU_MS, LEAK_A, OFFSET_A = 250.0, 0.3, 1e-5, 3e-5

    def __init__(self):
        super().__init__()
        self.captures = []

    def firmware_features(self):
        return FW_FEATURE_DAC_STEP

    def read_ocp(self, restore_switch_state=True):
        self._safe_open_switch()
        return 0.123

    def dac_step_capture(self, n_samples, period_us, n_pre, potential_V):
        self.captures.append((n_samples, period_us, n_pre, potential_V,
                              self._switch_state))
        step_V = potential_V - 0.123
        t = (np.arange(n_samples) - n_pre) * period_us / 1000.0 + 0.005
        i_A = _board_reading(t, step_V / self.R, self.TAU_MS,
                             np.sign(step_V) * self.LEAK_A) + self.OFFSET_A
        counts = np.round(32768 + i_A * 1e3 * 10000.0).astype(np.uint16)
        return counts, n_samples * period_us, 0, 5.0

    def _capture_counts_to_mA(self, counts, gain_idx):
        return (np.asarray(counts, dtype=float) - 32768) / 10000.0


def test_measure_r_u_steps_the_dac_when_the_firmware_can():
    ps = _DacStepMock()
    ps.write_gain(Resistors.R_10K)
    result = ps.measure_R_u(step_mV=50.0, settle_s=0.0)

    assert result['control'] is None
    assert [p['source'] for p in result['polarities']] == ['dac-step'] * 2
    assert result['fit_ok'] is True, result['message']
    expected = 0.1 / (0.1 / ps.R + 2 * ps.LEAK_A)
    assert result['R_u_ohm'] == pytest.approx(expected, rel=0.01)
    # Each capture started from rest at OCP with the cell already connected
    targets = [c[3] for c in ps.captures]
    assert targets == pytest.approx([0.173, 0.073])
    assert all(c[2] == RU_PRE_SAMPLES and c[4] for c in ps.captures)
    for p in result['polarities']:
        assert p['baseline_A'] == pytest.approx(ps.OFFSET_A, abs=2e-7)
    assert ps._switch_state is False
    assert ps.Resistor == Resistors.R_10K


class _FakeSerial:
    """Records what the driver writes and plays back scripted reads."""

    def __init__(self, reads):
        self.reads = list(reads)
        self.written = []
        self.timeout = 1.0

    def write(self, data):
        self.written.append(bytes(data))

    def read(self, n):
        return self.reads.pop(0) if self.reads else b""

    def reset_input_buffer(self):
        pass


def _dac_step_ack(captured, duration_us=150000, gain=0, centre_ns=4900):
    return (np.uint32([captured, duration_us]).tobytes() + bytes([gain])
            + np.uint32([centre_ns]).tobytes())


def test_dac_step_capture_request_and_reply_on_the_wire():
    ps = Potentiostat(serial_port='UNCONNECTED')
    counts = np.arange(100, 106, dtype=np.uint16)
    ps.serial = _FakeSerial([_dac_step_ack(6), counts.tobytes()])
    got, duration_us, gain, centre_us = ps.dac_step_capture(6, 25, 2, 0.2)

    req = ps.serial.written[0]
    assert req[1] == 22 and len(req) == 18
    assert list(np.frombuffer(req, np.uint32, 3, 2)) == [6, 25, 2]
    expected_dac = float(ps._scale_input_voltages(
        (0.2 - ps._calib_v_intercept) / ps._calib_v_slope))
    assert np.frombuffer(req, np.float32, 1, 14)[0] == pytest.approx(expected_dac)
    assert list(got) == list(counts)
    assert (duration_us, gain, centre_us) == (150000, 0, pytest.approx(4.9))


def test_dac_step_capture_refused_with_the_cell_disconnected():
    ps = Potentiostat(serial_port='UNCONNECTED')
    ps.serial = _FakeSerial([_dac_step_ack(0)])
    with pytest.raises(RuntimeError, match="not connected"):
        ps.dac_step_capture(6, 25, 2, 0.2)


# ----------------------------------------------------------------------
# Current tare: the open-circuit zero error is removed before a CV
# ----------------------------------------------------------------------

# Board P's registry read pair, and a raw reading that calibrates to the
# +42 uA zero error its CVs showed (bench 2026-09-21).
_P_READ = (1.07433, -0.621413)
_P_ZERO_MA = 0.042


def _tare_board(raw_mA=None):
    """Unconnected driver with board P's calibration and a fixed raw reading."""
    ps = Potentiostat(serial_port='UNCONNECTED')
    ps.set_calibration_params(1.21331, 0.0283254, *_P_READ)
    if raw_mA is None:
        raw_mA = (_P_ZERO_MA - _P_READ[1]) / _P_READ[0]
    ps.read_current = lambda: np.array([raw_mA / 1000.0])
    return ps, raw_mA


def test_tare_removes_the_open_circuit_zero_error():
    ps, raw_mA = _tare_board()
    assert ps.read_current_calibrated()[0] == pytest.approx(_P_ZERO_MA)

    removed = ps.tare_current()
    assert removed == pytest.approx(_P_ZERO_MA)
    assert ps.read_current_calibrated()[0] == pytest.approx(0.0, abs=1e-9)
    # The batch stream and the live loop's combined read share the intercept
    assert ps.calibrate_batch_current_uA(raw_mA / 1000.0) == pytest.approx(0.0, abs=1e-6)
    ps.read_potential_current = lambda: [0.1, raw_mA / 1000.0]
    assert ps.read_potential_current_calibrated()[1] == pytest.approx(0.0, abs=1e-9)


def test_repeated_tares_replace_rather_than_stack():
    ps, _ = _tare_board()
    ps.tare_current()
    intercept = ps._calib_read_intercept
    assert ps.tare_current() == pytest.approx(_P_ZERO_MA)    # measured afresh
    assert ps._calib_read_intercept == pytest.approx(intercept)


def test_new_calibration_clears_the_tare():
    ps, _ = _tare_board()
    ps.tare_current()
    ps.set_calibration_params(1.21331, 0.0283254, *_P_READ)
    assert ps._current_tare_mA == 0.0
    assert ps._calib_read_intercept == pytest.approx(_P_READ[1])


def test_tare_takes_the_median_so_one_glitch_cannot_skew_it():
    ps, raw_mA = _tare_board()
    readings = iter([raw_mA / 1000.0] * 9 + [5.0e-3])
    ps.read_current = lambda: np.array([next(readings)])
    assert ps.tare_current() == pytest.approx(_P_ZERO_MA)


def test_tare_refuses_a_connected_cell():
    ps, _ = _tare_board()
    ps._switch_state = True
    with pytest.raises(RuntimeError):
        ps.tare_current()


class _ZeroErrorLive(MockLivePotentiostat):
    """Test Mode device whose open-circuit reading shows a 42 uA zero error."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, chunk_delay_s=0, **kwargs)
        self.tare_calls = 0

    def tare_current(self, n_readings=10):
        assert self._switch_state is False
        self.tare_calls += 1
        return _P_ZERO_MA


def test_worker_tares_before_a_cv_and_records_it(window, monkeypatch):
    import moles.ui.electroanalysis.workers as workers
    monkeypatch.setattr(workers, "MockLivePotentiostat", _ZeroErrorLive)

    params = window.cv_tab._panels['A'].get_params()
    worker = workers.VoltammetryWorker('A', 'MOCK_A', params, use_mock=True)
    captured = {}
    worker.signals.finished.connect(
        lambda p, s, m, r, fp: captured.update(success=s, fp=fp))
    worker.run()

    assert captured['success'] is True
    assert worker.ps.tare_calls == 1
    params_text = Path(captured['fp']).with_suffix('.params.txt').read_text()
    assert "current_zero_uA: 42.0" in params_text


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
    names = header.split(',')
    assert "iR-corrected" in names[1]
    # A comma inside a column name would shift every column after it
    assert len(names) == 9
    assert names[6:] == ["Potential measured (V)", "iR drop subtracted (V)", "Range"]


@pytest.mark.parametrize("mode", ['posthoc', 'live'])
def test_ir_files_keep_the_sensed_potential_for_recorrection(window, tmp_path, mode):
    """An axis built from a wrong R_u must be repairable from the file alone
    (bench 2026-09-21: a 621 ohm value on a ~460 ohm cell collapsed ΔEp)."""
    R_u = 500.0
    captured, _ = _run_ir(window, tmp_path, R_u, mode=mode)
    data = np.genfromtxt(captured['fp'], delimiter=',', skip_header=1)
    corrected, current_uA, sensed, drop = data[:, 1], data[:, 2], data[:, 6], data[:, 7]

    assert np.allclose(drop, current_uA * 1e-6 * R_u, atol=2e-5)
    assert np.allclose(sensed - drop, corrected, atol=2e-5)

    # Rebuilding with a different R_u needs only the saved columns
    better_R_u = 420.0
    rebuilt = sensed - current_uA * 1e-6 * better_R_u
    assert np.allclose(rebuilt - corrected,
                       current_uA * 1e-6 * (R_u - better_R_u), atol=3e-5)


def test_combine_ru_repeats_reports_the_median_of_agreeing_values():
    from moles.ui.electroanalysis.workers import combine_ru_repeats
    ok, R_u, note = combine_ru_repeats(
        [{'R_u_ohm': v, 'fit_ok': True} for v in (455.0, 470.0, 460.0)])
    assert ok is True
    assert R_u == pytest.approx(460.0)
    assert "455, 470, 460" in note


def test_combine_ru_repeats_refuses_values_that_disagree():
    """The bench spread (427-621 ohm) must be refused, not averaged."""
    from moles.ui.electroanalysis.workers import combine_ru_repeats
    ok, R_u, note = combine_ru_repeats(
        [{'R_u_ohm': v, 'fit_ok': True} for v in (621.0, 496.0, 427.0)])
    assert ok is False and R_u is None
    assert "disagree" in note and "621" in note


def test_combine_ru_repeats_passes_a_refusal_through():
    from moles.ui.electroanalysis.workers import combine_ru_repeats
    ok, R_u, note = combine_ru_repeats([
        {'R_u_ohm': 460.0, 'fit_ok': True},
        {'R_u_ohm': float('inf'), 'fit_ok': False,
         'message': "decay too fast to extrapolate"}])
    assert ok is False and R_u is None
    assert note.startswith("Repeat 2 was refused") and "too fast" in note


class _ScriptedRuMock(MockPotentiostat):
    """Mock whose successive R_u measurements return scripted values."""

    VALUES = (460.0, 455.0, 470.0)

    def __init__(self, serial_port=None):
        super().__init__(serial_port)
        self.calls = 0

    def measure_R_u(self, step_mV=50.0, n_readings=30, settle_s=2.0):
        value = self.VALUES[self.calls]
        self.calls += 1
        ok = np.isfinite(value)
        return {'R_u_ohm': value, 'fit_ok': ok,
                'message': '' if ok else "decay too fast to extrapolate",
                'polarities': []}


def _run_ru_worker(monkeypatch, mock_cls):
    import moles.ui.electroanalysis.workers as workers
    made = []

    def _factory(serial_port=None):
        made.append(mock_cls(serial_port))
        return made[-1]

    monkeypatch.setattr(workers, "MockPotentiostat", _factory)
    worker = workers.RuWorker('A', 'MOCK_A', use_mock=True)
    captured = {}
    worker.signals.finished.connect(
        lambda p, ok, msg, r: captured.update(ok=ok, msg=msg, r_u=r))
    worker.run()
    return captured, made[0]


def test_r_u_worker_repeats_and_reports_the_median(qapp, monkeypatch):
    captured, ps = _run_ru_worker(monkeypatch, _ScriptedRuMock)
    assert ps.calls == 3
    assert captured['ok'] is True
    assert captured['r_u'] == pytest.approx(460.0)
    assert "median" in captured['msg']


def test_r_u_worker_refuses_repeats_that_disagree(qapp, monkeypatch):
    class _Scattered(_ScriptedRuMock):
        VALUES = (621.0, 496.0, 427.0)

    captured, _ = _run_ru_worker(monkeypatch, _Scattered)
    assert captured['ok'] is False
    assert captured['r_u'] is None
    assert "disagree" in captured['msg']


def test_r_u_worker_stops_at_the_first_refused_repeat(qapp, monkeypatch):
    class _RefusedFirst(_ScriptedRuMock):
        VALUES = (float('inf'), 460.0, 460.0)

    captured, ps = _run_ru_worker(monkeypatch, _RefusedFirst)
    assert ps.calls == 1                       # no further cell stepping
    assert captured['ok'] is False
    assert "too fast" in captured['msg']


def test_repeat_traces_get_their_own_files(qapp, tmp_path, monkeypatch):
    import moles.ui.electroanalysis.workers as workers
    monkeypatch.setattr(workers, "LOG_DIR", tmp_path)
    result = _FwCaptureMock().measure_R_u(step_mV=50.0, settle_s=0.0)
    worker = workers.RuWorker('A', 'MOCK_A', use_mock=True)
    worker._save_traces(result, repeat=1)
    worker._save_traces(result, repeat=2)
    names = sorted(p.name for p in (tmp_path / "ru_traces").glob("RU_PS-A_*.csv"))
    assert [n[-7:] for n in names] == ["_r1.csv", "_r2.csv"]


def test_plain_cv_header_has_no_ir_columns(window, tmp_path):
    from moles.ui.electroanalysis.workers import VoltammetryWorker

    params = window.cv_tab._panels['A'].get_params()   # iR off by default
    worker = VoltammetryWorker('A', 'MOCK_A', params, use_mock=True)
    captured = {}
    worker.signals.finished.connect(
        lambda p, s, m, r, fp: captured.update(fp=fp))
    worker.run()

    header = Path(captured['fp']).read_text().splitlines()[0]
    assert header == "Time (s),Potential (V),Current (uA),Cycle,Index,Target V,Range"


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

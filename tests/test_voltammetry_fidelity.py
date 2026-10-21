"""The cell gets the waveform that was programmed, and the files say how it went.

Cover for the September 2026 voltammetry review (bench data, 2026-09-23):
each CV leg ran one step short and repeated its vertex; a step rate that does
not divide 1000 Hz ran at the wrong scan rate; the cell sat at Start V for the
whole pre-fill upload (~2.7 s, unrecorded); the firmware read the current right
after each DAC step (+/-3.3 uA charging spikes), which the driver now asks it
not to do; DPV pulses came out 48.4 or 50.9 mV for a 50 mV request and the
differential curve was built from the live plot's stream, which a skipped
chunk shifts; and nothing in a saved run said whether the firmware had to
hold, drop or re-time any points.

Driver tests run against the byte-level ``FakeFirmware`` from the upload
tests; GUI tests run the worker in Test Mode.
"""

import threading
from pathlib import Path

import numpy as np
import pytest

from moles.driver import ps4_ref
from moles.driver.mock import MockLivePotentiostat
from moles.driver.ps4_ref import (Commands, NOMINAL_DAC_STEP_V, Potentiostat,
                                  WaveformTrackingError, build_cv_waveform,
                                  measure_step_period_ms)
from moles.methods.cv import LivePotentiostat
from moles.methods.dpv import (applied_dpv_settings, build_dpv_waveform,
                               cyclic_sweeps, process_dpv, samples_per_segment)

from test_waveform_upload import FakeFirmware, _make, _sweep


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ps4_ref.time, "sleep", lambda s: None)


def _with_firmware(fw, cls=Potentiostat):
    ps = cls.__new__(cls)
    Potentiostat.__init__(ps, serial_port="FAKE")
    ps.serial = fw
    ps.ResistorVal = 100.0
    ps.Resistor = 0
    return ps


def _run_execute(fw):
    """The EXECUTE message that started the run (the probe's is one step)."""
    return [w for w in fw.writes if w[1] == Commands.EXECUTE_VOLTAGE_BATCH
            and np.frombuffer(w[2:10], np.uint32)[1] > 1][0]


def _cv(ps, **kwargs):
    args = dict(min_V=-0.5, max_V=0.5, cycles=1, mV_s=100.0, step_hz=250,
                start_V=0.0, last_V=0.0, quiet_s=0)
    args.update(kwargs)
    return ps.perform_CV(**args)


# ----------------------------------------------------------------------
# CV waveform
# ----------------------------------------------------------------------

@pytest.mark.parametrize("mV_s", [10, 100, 1000, 10000])
def test_every_leg_has_equal_steps_and_ends_on_its_vertex(mV_s):
    step = mV_s / 1000 / 250
    w = build_cv_waveform(-0.5, 1.0, 3, step, 0.0, 0.0, 'Negative')
    d = np.diff(w)
    assert np.all(d != 0), "no point repeats, not even at a vertex"
    turns = np.nonzero(np.sign(d[1:]) != np.sign(d[:-1]))[0] + 1
    assert set(np.round(w[turns], 12)) <= {-0.5, 1.0}
    assert w[0] == 0.0 and w[-1] == 0.0
    for leg in np.split(d, turns):
        assert np.allclose(leg, leg[0])
        # Whole steps per leg: the step is off by at most half a step per leg
        assert abs(abs(leg[0]) - step) <= 0.5 * step / len(leg) * (1 + 1e-9)


def test_a_leg_shorter_than_half_a_step_is_one_step():
    w = build_cv_waveform(0.0, 0.001, 1, 0.01, 0.0, 0.0, 'Positive')
    assert np.allclose(w, [0.0, 0.001, 0.0])


def test_scan_rate_is_exact_when_step_hz_does_not_divide_1000():
    ps, fw = _make(timing=True)
    data = _cv(ps, step_hz=300)               # the device can only run 3 ms steps
    step_s = round(1000 / 300) / 1000
    dE = np.median(np.abs(np.diff(data[:, 5])))
    assert dE / step_s == pytest.approx(0.100, rel=1e-3)
    assert np.diff(data[:, 0]) == pytest.approx(step_s)


# ----------------------------------------------------------------------
# Run sequencing and sampling
# ----------------------------------------------------------------------

def test_cell_connects_after_the_upload_and_holds_for_the_quiet_time(monkeypatch):
    ps, fw = _make(timing=True)
    sleeps = []
    monkeypatch.setattr(ps4_ref.time, "sleep", lambda s: sleeps.append((len(fw.writes), s)))
    _cv(ps, quiet_s=1.5)

    switch_on = [k for k, w in enumerate(fw.writes)
                 if w[1] == Commands.WRITE_SWITCH and w[2] == 1]
    start = fw.writes.index(_run_execute(fw))
    uploads = [k for k, w in enumerate(fw.writes[:start])
               if w[1] in (Commands._WRITE_BUFFER, Commands._WRITE_BUFFER_AT)]
    assert len(switch_on) == 1
    assert uploads[-1] < switch_on[0] < start, "connected after the upload, before the start"
    assert (switch_on[0] + 1, 1.5) in sleeps, "quiet time held right after connecting"


@pytest.mark.parametrize("timing, n_words, first_t", [(True, 3, 0.004), (False, 2, 0.0)])
def test_batch_options_follow_what_the_firmware_offers(timing, n_words, first_t):
    ps, fw = _make(timing=timing)
    data = _cv(ps)
    assert (len(_run_execute(fw)) - 2) // 4 == n_words
    # End-of-step sampling and the starvation fail-safe, when offered
    assert fw.batch_flags[-1] == (0b11 if timing else 0)
    assert ps.last_batch_report['sample_at_end'] is timing
    # A sample taken at the end of its step belongs one period after it began
    assert data[0, 0] == pytest.approx(first_t)
    assert np.diff(data[:, 0]) == pytest.approx(0.004)


def test_alternating_polarity_stream_keeps_the_historical_batch_options():
    """The AP loop restarts a failed stream without reconnecting the cell, so
    its stream must not ask for the starvation fail-safe."""
    ps, fw = _make(timing=True)
    square = np.r_[np.full(60, 0.5), np.full(60, -0.5)]
    ps.stream_voltage_batch(square, 4, lambda block: False, block_points=60)
    stream = [w for w in fw.writes if w[1] == Commands.EXECUTE_VOLTAGE_BATCH
              and np.frombuffer(w[2:10], np.uint32)[1] == 0xFFFFFFFF]
    assert len(stream) == 1 and len(stream[0]) == 2 + 8   # two words, no options
    assert fw.batch_flags[-1] == 0


def test_clean_run_report():
    ps, fw = _make(timing=True)
    _cv(ps)
    report = ps.last_batch_report
    assert report['degraded'] == []
    assert report['health']['starved_steps'] == 0
    assert report['health']['tx_dropped'] == 0
    assert report['time_axis'] == 'nominal'          # an instant fake run is too short to time


def test_run_report_lists_what_the_firmware_saw_go_wrong():
    ps, fw = _make(timing=True)
    fw.inject = {'starved': 3, 'late_steps': 5, 'max_late_ms': 20, 'resyncs': 1}
    _cv(ps)
    report = ps.last_batch_report
    assert report['health']['starved_steps'] == 3
    assert report['health']['resyncs'] == 1
    assert len(report['degraded']) == 3    # held points, re-anchor, a 20 ms late step


def test_firmware_without_diagnostics_still_runs():
    class _NoDiagnostics(FakeFirmware):
        def _legacy(self, cmd, p):
            if cmd != Commands.READ_DIAGNOSTICS:
                super()._legacy(cmd, p)
    ps = _with_firmware(_NoDiagnostics(positional=False, new_framing=False))
    data = _cv(ps)
    assert data.shape[0] > 0
    assert ps._diagnostics_supported is False
    assert ps.last_batch_report['health'] == {}
    assert ps.last_batch_report['sample_at_end'] is False


def test_diagnostics_report_all_ten_counters():
    ps, fw = _make(timing=True)
    fw.late_steps, fw.max_late_ms, fw.resyncs, fw.starve_aborts = 4, 9, 1, 2
    report = ps.read_diagnostics()
    assert (report['late_steps'], report['max_late_ms'], report['resyncs'],
            report['starve_aborts'], report['features']) == (4, 9, 1, 2, 0x7)


def test_a_run_the_firmware_ended_for_want_of_data_is_explained():
    class _HostVanished(FakeFirmware):
        """Ends the run after 100 points, as the firmware does once waveform
        data has stopped arriving for longer than its starvation limit."""
        def _step(self):
            if self.batch_count > 1 and len(self.batches[-1]) >= 100:
                self.starve_aborts, self.batch_count, self.switch = 1, 0, 0
                return False
            return super()._step()
    fw = _HostVanished(timing=True)
    ps = _with_firmware(fw)
    with pytest.raises(WaveformTrackingError, match="stopped arriving"):
        ps.write_voltage_batch(_sweep(), 4)
    assert fw.switch == 0


def test_live_path_marks_a_corrupted_gain_byte_instead_of_crashing():
    ps, fw = _make(cls=LivePotentiostat, timing=True)
    ps.data_callback = lambda v, i: None
    ps.stop_event = threading.Event()
    ps._auto_gain = fw.auto_gain = True
    fw.bad_gain_at = 700
    out = ps.write_voltage_batch(_sweep(), 4)
    assert np.isnan(out[700]).all()
    assert np.isfinite(np.delete(out, 700, axis=0)).all()


# ----------------------------------------------------------------------
# Step period
# ----------------------------------------------------------------------

def test_measured_period_recovers_a_fast_device_clock():
    k = np.arange(2000)                                   # 48 s of 6-point chunks
    arrivals = 5.0 + k * 6 * 3.992e-3 + np.random.default_rng(0).normal(0, 0.002, k.size)
    period, trusted = measure_step_period_ms(arrivals, 6, 4.0)
    assert trusted
    assert period == pytest.approx(3.992, rel=2e-4)


def test_measured_period_is_not_trusted_for_short_irregular_or_implausible_runs():
    assert measure_step_period_ms(np.arange(200) * 0.024, 6, 4.0)[1] is False
    k = np.arange(2000)
    noisy = k * 0.024 + np.random.default_rng(1).normal(0, 0.2, k.size)
    assert measure_step_period_ms(noisy, 6, 4.0)[1] is False
    assert measure_step_period_ms(k * 6 * 4.4e-3, 6, 4.0)[1] is False


def test_time_axis_uses_the_measured_period_only_when_trusted():
    ps = Potentiostat.__new__(Potentiostat)
    ps.last_batch_report = {'step_ms_nominal': 4.0, 'step_ms_measured': 3.992,
                            'time_axis': 'measured', 'sample_at_end': True}
    assert np.allclose(ps._batch_time_axis(3), [0.003992, 0.007984, 0.011976])
    ps.last_batch_report.update(time_axis='nominal', sample_at_end=False)
    assert np.allclose(ps._batch_time_axis(3), [0.0, 0.004, 0.008])


# ----------------------------------------------------------------------
# DPV
# ----------------------------------------------------------------------

def _board_p():
    ps = Potentiostat.__new__(Potentiostat)
    ps.set_voltage_calibration_params(0.991143, 0.048, 0.99149, 0.0517278)
    return ps


def test_dpv_levels_sit_on_dac_codes_and_every_pulse_is_the_same_height():
    ps = _board_p()
    sweeps = cyclic_sweeps(-0.5, 1.0, 1, direction='Positive', start_V=0.0, end_V=0.0)
    w = build_dpv_waveform(sweeps, 0.05, 0.005, 200, 48, 250, dac_grid=ps.dac_grid())
    # The firmware's own conversion: host scaling, float32, x 4095/3.3
    x = np.float32(ps._scale_input_voltages(ps._waveform_to_raw_V(w)))
    codes = x.astype(np.float64) * (4095 / 3.3)
    assert np.max(np.abs(codes - np.round(codes))) < 1e-3

    n_step, n_pulse, _ = samples_per_segment(200, 48, 250)
    per = n_step + n_pulse
    n = (len(w) - n_step) // per
    blocks = np.round(codes[:n * per]).reshape(n, per)
    assert set(np.abs(blocks[:, n_step] - blocks[:, 0])) == {21}   # 50.9 mV, every pulse
    steps = np.abs(np.diff(blocks[:, 0]))
    assert (steps == 2).mean() > 0.99                               # 4.84 mV, all but the vertices


def test_applied_dpv_settings_report_what_really_runs():
    grid = (NOMINAL_DAC_STEP_V, 0.0)
    a = applied_dpv_settings(0.05, 0.005, 200, 50, 250, dac_grid=grid)
    assert (a['pulse_ms'], a['step_hold_ms']) == (48, 200)
    assert a['pulse_V'] == pytest.approx(21 * NOMINAL_DAC_STEP_V)
    assert a['step_V'] == pytest.approx(2 * NOMINAL_DAC_STEP_V)
    # A step below one DAC level still moves the staircase by one level
    assert applied_dpv_settings(0.05, 0.001, 200, 48, 250, dac_grid=grid)['step_V'] \
        == pytest.approx(NOMINAL_DAC_STEP_V)


def test_process_dpv_never_averages_the_pulse_edge_sample():
    n_step, n_pulse, _ = samples_per_segment(40, 8, 250)          # 10 + 2 samples
    period = [0.0] * n_step + [100.0, 1.0]                          # edge spike, then settled
    current = np.tile(period, 5)
    out = process_dpv(np.zeros_like(current), current, 40, 8, 250)
    assert np.allclose(out[:, 1], 1.0)


# ----------------------------------------------------------------------
# GUI runs (Test Mode)
# ----------------------------------------------------------------------

@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    import moles.ui.electroanalysis.control_tab as control_tab
    import moles.ui.electroanalysis.workers as workers
    monkeypatch.setattr(workers, "get_cv_data_folder", lambda: tmp_path)
    monkeypatch.setattr(control_tab, "get_cv_data_folder", lambda: tmp_path)
    from moles.ui.electroanalysis.main_window import MainWindow
    win = MainWindow()
    win.cv_tab.test_mode_cb.setChecked(True)
    win.dpv_tab.test_mode_cb.setChecked(True)
    return win


def _run_worker(tab, pid='A'):
    from moles.ui.electroanalysis.workers import VoltammetryWorker
    worker = VoltammetryWorker(pid, "MOCK_" + pid, tab._panels[pid].get_params(),
                               use_mock=True)
    captured = {}
    worker.signals.finished.connect(
        lambda p, s, m, r, fp: captured.update(success=s, msg=m, result=r, fp=fp))
    worker.run()
    assert captured['success'] is True, captured.get('msg')
    return captured


def test_dpv_run_saves_an_aligned_raw_file_and_what_was_applied(window):
    from moles.ui.electroanalysis.loader import load_trace
    out = _run_worker(window.dpv_tab)
    diff_path = Path(out['fp'])
    raw_path = diff_path.with_name(diff_path.stem + "_RAW.csv")
    assert raw_path.read_text().splitlines()[0] == "Time (s),Potential (V),Current (uA),Target V,Range"
    raw = np.loadtxt(raw_path, delimiter=",", skiprows=1)
    n_step, n_pulse, _ = samples_per_segment(200, 48, 250)
    assert (raw.shape[0] - n_step) % (n_step + n_pulse) == 0     # whole periods + the park
    assert load_trace(raw_path).x.size == raw.shape[0]            # still loads for analysis
    params = diff_path.with_suffix('.params.txt').read_text()
    for entry in ("applied_pulse_mV: 50.8", "applied_pulse_ms: 48", "quiet_time_s: 2.0",
                  "sampling: end of each step", "current_range: auto",
                  "current_ranges_used:"):
        assert entry in params


def test_dpv_result_survives_a_chunk_the_live_plot_skipped(window, monkeypatch):
    import moles.ui.electroanalysis.workers as workers

    class _PlotSkipsAChunk(MockLivePotentiostat):
        def _stream(self, potentials, currents_uA):
            keep = np.ones(len(potentials), bool)
            keep[120:126] = False
            super()._stream(potentials[keep], currents_uA[keep])
    monkeypatch.setattr(workers, "MockLivePotentiostat", _PlotSkipsAChunk)

    diff = _run_worker(window.dpv_tab)['result']['processed']
    # A shifted stream would pair each pulse with the next step's base current
    assert np.all(diff[:, 1] > -0.25)
    assert 0.3 < diff[np.argmax(diff[:, 1]), 0] < 0.7


def test_cv_run_records_quiet_time_sampling_and_step_timing(window):
    out = _run_worker(window.cv_tab)
    params = Path(out['fp']).with_suffix('.params.txt').read_text()
    for entry in ("quiet_time_s: 2.0", "sampling: end of each step",
                  "step_ms_nominal: 4.0", "time_axis: nominal"):
        assert entry in params
    data = np.loadtxt(out['fp'], delimiter=",", skiprows=1)
    assert data[0, 0] == pytest.approx(0.004)


def test_quiet_time_from_the_form_reaches_the_driver(window, monkeypatch):
    import moles.ui.electroanalysis.workers as workers
    seen = {}

    class _Spy(MockLivePotentiostat):
        def perform_CV(self, *args, quiet_s=None, **kwargs):
            seen['quiet_s'] = quiet_s
            return super().perform_CV(*args, quiet_s=quiet_s, **kwargs)
    monkeypatch.setattr(workers, "MockLivePotentiostat", _Spy)
    window.cv_tab._panels['A'].config.quiet_time.setValue(7.5)
    _run_worker(window.cv_tab)
    assert seen['quiet_s'] == 7.5


def test_dpv_form_rounds_widths_to_whole_samples_and_states_the_applied_pulse(window):
    cfg = window.dpv_tab._panels['A'].config
    for typed, runs in ((50, 48), (6, 8), (200, 200)):
        cfg.pulse_width.setValue(typed)
        cfg.pulse_width.editingFinished.emit()
        assert cfg.pulse_width.value() == runs
    assert "50.9 mV pulses" in cfg.applied_note.text()


def test_cv_form_states_the_real_step_and_warns_when_coarse(window):
    cfg = window.cv_tab._panels['A'].config
    cfg.scan_rate.setValue(100.0)
    assert "2.4 mV levels" in cfg.scan_note.text()
    cfg.scan_rate.setValue(2000.0)
    assert "coarse" in cfg.scan_note.text()

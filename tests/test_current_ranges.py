"""Current ranges: per-range calibration, range selection, auto-ranged batches.

Firmware 1.4 made gain indices 1-4 the real current ranges (~1 kohm to
1 Mohm; bench-confirmed on board TEST, 2026-09-27). The driver converts each
reading with the range it was taken on, scaling the board's range-0
calibration by the ratio of the range resistances, and an auto-ranged batch
tags every reading with its range. These tests drive the real driver code
against the byte-level fake firmware from test_waveform_upload, extended with
current ranges.
"""

from pathlib import Path

import numpy as np
import pytest

from moles.driver import ps4_ref
from moles.driver.ps4_ref import (AMP_SATURATION_V, BATCH_FLAG_AUTO_RANGE,
                                  BATCH_RANGE_PERIOD_SHIFT, Commands, Potentiostat,
                                  RANGE_OHMS)
from moles.methods.cv import LivePotentiostat
from test_waveform_upload import FakeFirmware, _dac_volts

# Board TEST's stored read calibration: range 0 reads 1.069x high before the
# fit, with a -0.592 mA intercept that is really a fixed ~57 mV offset
READ_SLOPE = 1.06911
READ_INTERCEPT_MA = -0.592
# The amplifier output (driver volts) at zero current that intercept implies
ZERO_V = READ_INTERCEPT_MA / (10.0 * READ_SLOPE)


def amplifier_V(current_mA, index, zero_V=ZERO_V):
    """Amplifier output (driver volts) for a true current on a range, on a
    board whose range-0 behaviour matches the stored calibration."""
    return zero_V - current_mA / (10.0 * READ_SLOPE) * RANGE_OHMS[index] / RANGE_OHMS[0]


def raw_A(amp_V, index):
    """What the driver's raw conversion makes of that output."""
    return -amp_V / Potentiostat.res_vals[index]


def calibrated_board():
    ps = Potentiostat(serial_port="FAKE")
    ps.set_calibration_params(1.2081, 0.049, READ_SLOPE, READ_INTERCEPT_MA)
    return ps


class RangeFirmware(FakeFirmware):
    """FakeFirmware with current ranges: a gain index, auto-range decisions
    at chosen points, and a dummy resistor read through the selected range."""

    def __init__(self, features=0x1F, switch_at=None, saturate_at=(), **kwargs):
        kwargs.setdefault("timing", True)
        super().__init__(**kwargs)
        self.features = features
        self.gain = 0
        self.gain_writes = []
        self.switch_at = dict(switch_at or {})
        self.saturate_at = set(saturate_at)

    def _legacy(self, cmd, p):
        if cmd == Commands.WRITE_GAIN:
            self.gain = p[0]
            self.gain_writes.append(p[0])
            self._ok()
        elif cmd == Commands.WRITE_AUTO_GAIN:
            self.auto_gain = bool(p[0])
            self._ok()
        elif cmd == Commands.READ_DIAGNOSTICS:
            vals = [0x0C000000, 0, self.rx_overflow, self.starved, self.rejected,
                    0, 0, 0, 0, self.features]
            self.out += np.uint32(vals).tobytes()
        else:
            super()._legacy(cmd, p)

    @property
    def auto_ranging(self):
        return bool(self.batch_flags and self.batch_flags[-1] & BATCH_FLAG_AUTO_RANGE)

    def _step(self):
        if self.batch_ind >= self.batch_count:
            return False
        point = self.batch_ind
        if self.auto_ranging and point in self.switch_at:
            self.gain = self.switch_at[point]
        start = len(self.out)
        if not super()._step():
            return False
        # Replace the range-0 current reading the parent wrote with one read
        # through the selected range, and tag it with that range
        potential = (_dac_volts(self.dac) / 3.3) * 10 - 5
        current_mA = potential / self.dummy_ohm * 1000.0
        amp = amplifier_V(current_mA, self.gain)
        if point in self.saturate_at:
            amp = -4.9
        we_out = ((amp - 0.0494) / 0.9919 + 5) / 10 * 3.3
        self.out[start:start + 4] = np.float32(we_out).tobytes()
        if self.auto_gain:
            self.out[start + 8] = self.gain
        return True


def make(cls=Potentiostat, **fw_kwargs):
    fw = RangeFirmware(**fw_kwargs)
    ps = cls.__new__(cls)
    Potentiostat.__init__(ps, serial_port="FAKE")
    if cls is LivePotentiostat:
        ps.data_callback = None
        ps.stop_event = None
    ps.serial = fw
    ps.Resistor = 0
    ps.ResistorVal = 100.0
    ps.set_calibration_params(1.2081, 0.049, READ_SLOPE, READ_INTERCEPT_MA)
    return ps, fw


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ps4_ref.time, "sleep", lambda s: None)


def expected_current_uA(fw, n, skip=0):
    """Ohm's law through the dummy resistor for each applied setpoint, after
    the first ``skip`` (a discarded lead-in)."""
    applied = np.array([_dac_volts(v) for v in fw.applied[skip:skip + n]])
    return ((applied / 3.3) * 10 - 5) / fw.dummy_ohm * 1e6


class TestPerRangeCalibration:
    def test_range_zero_is_the_historical_calibration(self):
        ps = calibrated_board()
        raw = np.array([-2e-3, 0.0, 1.5e-4, 3e-3])
        np.testing.assert_allclose(ps._calibrated_mA(raw, 0),
                                   raw * 1000.0 * READ_SLOPE + READ_INTERCEPT_MA)

    @pytest.mark.parametrize("index", range(5))
    def test_every_range_reads_the_true_current(self, index):
        ps = calibrated_board()
        full_scale_mA = 4.0 / RANGE_OHMS[index] * 1000.0
        for current_mA in (-0.8 * full_scale_mA, 0.0, 0.3 * full_scale_mA):
            amp = amplifier_V(current_mA, index)
            assert float(ps._calibrated_mA(raw_A(amp, index), index)) == pytest.approx(
                current_mA, abs=1e-9 + 1e-9 * abs(current_mA))

    def test_zero_current_reads_zero_on_every_range(self):
        ps = calibrated_board()
        for index in range(5):
            assert float(ps._calibrated_mA(raw_A(ZERO_V, index), index)) == pytest.approx(0, abs=1e-9)

    def test_tare_on_a_sensitive_range_zeroes_every_range(self):
        # The zero has drifted 2 mV since onboarding; a tare taken on range 3
        # must correct the reading on every range
        ps = calibrated_board()
        ps.Resistor = 3
        ps._switch_state = False
        drifted = ZERO_V + 0.002
        ps.read_current = lambda: np.array([raw_A(drifted, ps.Resistor)])
        ps.tare_current()
        for index in range(5):
            assert float(ps._calibrated_mA(raw_A(drifted, index), index)) == pytest.approx(0, abs=1e-9)

    def test_batch_calibration_follows_each_points_range(self):
        ps = calibrated_board()
        ranges = np.array([0, 1, 2, 3, 4])
        currents_mA = np.array([10.0, 1.0, -0.1, 0.01, 0.001])
        raws = [raw_A(amplifier_V(c, r), r) for c, r in zip(currents_mA, ranges)]
        np.testing.assert_allclose(ps.calibrate_batch_current_uA(raws, ranges),
                                   currents_mA * 1000.0, rtol=1e-9)


class TestRangeSelection:
    def test_auto_starts_on_the_least_sensitive_range_with_tagged_readings(self):
        ps, fw = make(features=0x1F)
        ps.Resistor = 3
        assert ps.set_current_range('auto') == 'auto'
        assert fw.gain == 0 and fw.auto_gain

    def test_auto_without_firmware_support_runs_on_range_zero(self):
        ps, fw = make(features=0x0F)
        assert ps.set_current_range('auto') == 0
        assert fw.gain == 0 and not fw.auto_gain

    def test_fixed_range_is_set_even_if_the_driver_thinks_it_already_is(self):
        ps, fw = make(features=0x0F)
        ps.Resistor = 2
        fw.gain = 0
        assert ps.set_current_range(2) == 2
        assert fw.gain == 2 and not fw.auto_gain

    def test_sensitive_range_is_refused_on_firmware_with_the_old_table(self):
        ps, fw = make(features=0x07)
        with pytest.raises(ValueError, match="firmware 1.4"):
            ps.set_current_range(3)
        assert fw.gain_writes == []
        assert ps.set_current_range(0) == 0


class TestAutoRangedBatches:
    def run_cv(self, cls=Potentiostat, current_range='auto', **fw_kwargs):
        ps, fw = make(cls, **fw_kwargs)
        data = ps.perform_CV(-0.4, 0.4, 1, 100, 250, start_V=0.0, last_V=0.0,
                             quiet_s=0, current_range=current_range)
        return ps, fw, data

    @pytest.mark.parametrize("cls", [Potentiostat, LivePotentiostat])
    def test_readings_follow_the_range_changes(self, cls):
        lead = Potentiostat._auto_range_lead_in()
        ps, fw, data = self.run_cv(cls, switch_at={lead + 200: 2, lead + 700: 1}, dummy_ohm=20e3)
        flags = fw.batch_flags[-1]
        assert flags & BATCH_FLAG_AUTO_RANGE
        assert flags >> BATCH_RANGE_PERIOD_SHIFT == 1
        ranges = ps.last_batch_ranges
        assert (ranges[:200] == 0).all() and (ranges[200:700] == 2).all() and (ranges[700:] == 1).all()
        np.testing.assert_allclose(data[:, 2], expected_current_uA(fw, len(data), lead),
                                   atol=0.02)
        report = ps.last_batch_report
        assert report['auto_range'] and report['ranges_used'] == [0, 1, 2]
        assert report['range_changes'] == 2
        assert report['saturated_points'] == 0 and not report['degraded']

    def test_dpv_asks_for_range_changes_between_pulse_pairs_only(self):
        ps, fw = make()
        waveform = np.repeat(np.linspace(-0.2, 0.2, 40), 25).astype(np.float32)
        data = ps.perform_DPV(waveform, sample_hz=250, quiet_s=0, current_range='auto',
                              range_period=25)
        assert fw.batch_flags[-1] >> BATCH_RANGE_PERIOD_SHIFT == 25
        # The lead-in is whole pulse pairs, so the saved ones keep their alignment
        lead = ps.last_batch_report['lead_in_points']
        assert lead % 25 == 0 and lead >= 4 * 8
        assert len(data) == len(waveform)
        np.testing.assert_allclose(data[:, 3], waveform)

    @pytest.mark.parametrize("cls", [Potentiostat, LivePotentiostat])
    def test_the_climb_to_the_right_range_is_run_but_not_kept(self, cls):
        # The firmware climbs one range per 8 points from the least sensitive
        ps, fw = make(cls, switch_at={8: 1, 16: 2, 24: 3}, dummy_ohm=1e6)
        plotted = []
        if cls is LivePotentiostat:
            ps.data_callback = lambda v, i: plotted.extend(i)
        data = ps.perform_CV(-0.4, 0.4, 1, 100, 250, start_V=0.0, last_V=0.0,
                             quiet_s=0, current_range='auto')
        lead = ps.last_batch_report['lead_in_points']
        assert lead == Potentiostat._auto_range_lead_in() == 32
        assert all(_dac_volts(v) == _dac_volts(fw.applied[0]) for v in fw.applied[:lead])
        cv = ps._build_cv_waveform(-0.4, 0.4, 1, 0.0004, 0.0, 0.0, 'Negative')
        assert len(data) == len(cv) == ps.last_batch_report['points']
        assert (ps.last_batch_ranges == 3).all()
        if cls is LivePotentiostat:
            assert len(plotted) == len(data)

    def test_fixed_range_runs_have_no_lead_in(self):
        ps, fw, data = self.run_cv(current_range=2, features=0x1F)
        assert ps.last_batch_report['lead_in_points'] == 0
        assert len(fw.applied) >= len(data)

    def test_fixed_range_run_is_read_on_that_range(self):
        ps, fw, data = self.run_cv(current_range=3, features=0x0F, dummy_ohm=1e6)
        assert not fw.batch_flags[-1] & BATCH_FLAG_AUTO_RANGE
        assert (ps.last_batch_ranges == 3).all()
        np.testing.assert_allclose(data[:, 2], expected_current_uA(fw, len(data)),
                                   atol=1e-4)

    def test_old_firmware_auto_runs_on_range_zero(self):
        ps, fw, data = self.run_cv(features=0x07, switch_at={100: 3})
        assert ps.last_batch_report['lead_in_points'] == 0
        assert not fw.batch_flags[-1] & BATCH_FLAG_AUTO_RANGE
        assert (ps.last_batch_ranges == 0).all()
        assert ps.last_batch_report['ranges_used'] == [0]

    def test_readings_at_a_ranges_limit_are_reported(self):
        ps, fw, data = self.run_cv(saturate_at={50, 51, 52})
        report = ps.last_batch_report
        assert report['saturated_points'] == 3
        assert any("limit of their current range" in d for d in report['degraded'])

    def test_saturation_threshold_sits_inside_the_amplifier_swing(self):
        assert 4.5 < AMP_SATURATION_V < 4.9


# ----------------------------------------------------------------------
# The Current range choice in the app (Test Mode)
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


def run_worker(tab, pid='A'):
    from moles.ui.electroanalysis.workers import VoltammetryWorker
    worker = VoltammetryWorker(pid, "MOCK_" + pid, tab._panels[pid].get_params(),
                               use_mock=True)
    captured = {}
    worker.signals.finished.connect(
        lambda p, s, m, r, fp: captured.update(success=s, msg=m, result=r, fp=fp))
    worker.run()
    assert captured['success'] is True, captured.get('msg')
    return captured


def spy_driver(monkeypatch):
    """Record the current range each run asks the driver for."""
    import moles.ui.electroanalysis.workers as workers
    from moles.driver.mock import MockLivePotentiostat
    seen = {}

    class _Spy(MockLivePotentiostat):
        def set_current_range(self, current_range):
            seen.setdefault('set', []).append(current_range)
            return super().set_current_range(current_range)

        def perform_CV(self, *args, **kwargs):
            seen['cv'] = kwargs.get('current_range')
            return super().perform_CV(*args, **kwargs)

        def perform_DPV(self, *args, **kwargs):
            seen['dpv'] = (kwargs.get('current_range'), kwargs.get('range_period'))
            return super().perform_DPV(*args, **kwargs)
    monkeypatch.setattr(workers, "MockLivePotentiostat", _Spy)
    return seen


@pytest.mark.parametrize("tab_name", ["cv_tab", "dpv_tab"])
def test_forms_offer_auto_by_default_and_every_fixed_range(window, tab_name):
    combo = getattr(window, tab_name)._panels['A'].config.current_range
    assert combo.currentData() == 'auto'
    assert [combo.itemData(i) for i in range(combo.count())] == ['auto', 0, 1, 2, 3, 4]
    assert combo.itemText(5) == "±5 µA"
    assert getattr(window, tab_name)._panels['A'].get_params()['current_range'] == 'auto'


def test_cv_asks_for_the_chosen_range_and_records_it(window, monkeypatch):
    seen = spy_driver(monkeypatch)
    window.cv_tab._panels['A'].config.current_range.setCurrentIndex(3)   # +/-500 uA
    out = run_worker(window.cv_tab)
    assert seen['set'][0] == 2 and seen['cv'] == 2
    params = Path(out['fp']).with_suffix('.params.txt').read_text()
    assert "current_range: 2" in params and "current_ranges_used: +/-500 uA" in params
    header = Path(out['fp']).read_text().splitlines()[0]
    assert header.endswith(",Range")
    data = np.loadtxt(out['fp'], delimiter=",", skiprows=1)
    assert (data[:, -1] == 2).all()


def test_dpv_auto_ranges_only_between_pulse_pairs(window, monkeypatch):
    from moles.methods.dpv import samples_per_segment
    seen = spy_driver(monkeypatch)
    out = run_worker(window.dpv_tab)
    n_step, n_pulse, _ = samples_per_segment(200, 48, 250)
    assert seen['dpv'] == ('auto', n_step + n_pulse)
    params = Path(out['fp']).with_suffix('.params.txt').read_text()
    assert "current_range: auto" in params and "current_range_changes:" in params


def test_live_ir_scan_runs_auto_on_the_least_sensitive_range(window, monkeypatch):
    seen = spy_driver(monkeypatch)
    cfg = window.cv_tab._panels['A'].config
    cfg.ir_box.setChecked(True)
    cfg.ir_mode.setCurrentIndex(cfg.ir_mode.findData('live'))
    cfg.r_u.setValue(100.0)
    run_worker(window.cv_tab)
    assert seen['set'][0] == 0


def test_the_old_auto_gain_checkbox_is_gone(window):
    for tab in (window.cv_tab, window.dpv_tab):
        params = tab._panels['A'].get_params()
        assert 'auto_gain' not in params


@pytest.mark.parametrize("tab_name", ["cv_tab", "dpv_tab"])
def test_every_cv_and_dpv_is_zeroed_at_open_circuit_and_records_the_ocp(window, monkeypatch, tab_name):
    import moles.ui.electroanalysis.workers as workers
    from moles.driver.mock import MockLivePotentiostat
    order = []

    class _Spy(MockLivePotentiostat):
        def read_ocp(self, restore_switch_state=True):
            order.append(('ocp', self._switch_state))
            return 0.1234

        def tare_current(self, n_readings=10):
            order.append(('tare', self._switch_state))
            return 0.0015

        def perform_CV(self, *args, **kwargs):
            order.append(('run', None))
            return super().perform_CV(*args, **kwargs)

        def perform_DPV(self, *args, **kwargs):
            order.append(('run', None))
            return super().perform_DPV(*args, **kwargs)
    monkeypatch.setattr(workers, "MockLivePotentiostat", _Spy)
    out = run_worker(getattr(window, tab_name))
    # OCP, then the zero with the cell still disconnected, then the run
    assert order == [('ocp', False), ('tare', False), ('run', None)]
    params = Path(out['fp']).with_suffix('.params.txt').read_text()
    assert "ocp_V: 0.1234" in params and "current_zero_uA: 1.5" in params


# ----------------------------------------------------------------------
# Run status on the panel
# ----------------------------------------------------------------------

@pytest.mark.parametrize("tab_name", ["cv_tab", "dpv_tab"])
def test_status_goes_preparing_running_saving_finished(window, tab_name):
    from moles.ui.electroanalysis.workers import VoltammetryWorker
    tab = getattr(window, tab_name)
    worker = VoltammetryWorker('A', 'MOCK_A', tab._panels['A'].get_params(), use_mock=True)
    seen = []
    worker.signals.status.connect(lambda pid, msg: seen.append((pid, msg)))
    worker.run()
    messages = [m for _, m in seen]
    assert messages[0] == "Preparing..."
    assert messages.count("Running...") == 1
    assert messages.index("Running...") < messages.index("Saving...")
    assert messages[-1] == "Finished"
    assert all(pid == 'A' for pid, _ in seen)


def test_panel_shows_the_message_not_the_board_name(window):
    panel = window.cv_tab._panels['A']
    window.cv_tab._on_worker_status('A', "Running...")
    assert panel.status_label.text() == "Status: Running..."


def test_any_failed_run_ends_as_an_error_and_frees_the_run_button(window):
    panel = window.cv_tab._panels['A']
    panel.run_requested.disconnect()   # the panel's own state only; start no run
    panel._on_run()
    assert panel.status_label.text() == "Status: Preparing..."
    assert not panel.run_btn.isEnabled()
    window.cv_tab._on_finished('A', False, "Potentiostat A unreachable after retry", None, None)
    assert panel.status_label.text() == "Status: Error: Potentiostat A unreachable after retry"
    assert panel.run_btn.isEnabled()

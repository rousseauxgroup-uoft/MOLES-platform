"""On-board iR compensation (firmware 1.8): the driver's configuration of the
board's loop, a CV run through it, and the worker's choice of loop.

The board side is emulated at the byte level: the fake firmware from
test_current_ranges, extended with CMD_IR_CONFIG / CMD_IR_REPORT and a Python
copy of the firmware's correction (Ir_Update and the setpoint path of
Batch_Apply_Next), so the real driver code configures it, streams through it
and reads back what it did.
"""

import numpy as np
import pytest

from moles.driver import ps4_ref
from moles.driver.ps4_ref import (BATCH_FLAG_IR_COMP, BATCH_FLAG_SAMPLE_AT_END,
                                  FW_FEATURE_IR_COMP, HW_VOLTAGE_COMPLIANCE_V,
                                  IR_FEEDBACK_ALPHA, IR_LIMIT_MS, IR_MAX_FRACTION,
                                  IR_RUNAWAY_MS, RANGE_OHMS, Commands,
                                  Potentiostat, WaveformTrackingError)
from test_current_ranges import (READ_INTERCEPT_MA, READ_SLOPE, ZERO_V,
                                 RangeFirmware, amplifier_V)
from test_waveform_upload import _dac_volts


def _we_out(amp_V):
    """ADC volts on the current channel for an amplifier output (driver volts)."""
    return ((amp_V - 0.0494) / 0.9919 + 5) / 10 * 3.3


class IrFirmware(RangeFirmware):
    """RangeFirmware with firmware 1.8's on-board iR loop."""

    def __init__(self, features=0x7F, honour_ir=True, **kwargs):
        super().__init__(features=features, **kwargs)
        self.honour_ir = honour_ir
        self.ir_config = None
        self.ir_on = False
        self.zero = _we_out(ZERO_V)      # the board's switch-open reading
        self._reset_ir_report()

    def _reset_ir_report(self):
        self.ir_runs = self.ir_limited = self.ir_slewed = 0
        self.ir_runaways = self.ir_limits = 0
        self.ir_max_abs = 0.0

    def _legacy(self, cmd, p):
        if cmd == Commands.IR_CONFIG and self.features & FW_FEATURE_IR_COMP:
            if len(p) >= 40:
                f = np.frombuffer(bytes(p[:32]), np.float32)
                runaway, limit = (int(v) for v in np.frombuffer(bytes(p[32:40]), np.uint32))
                if (np.all(np.isfinite(f)) and 0 < f[5] <= 3.3 and 0 < f[6] <= 3.3
                        and 0 < f[7] <= 1 and runaway > 0 and limit > 0):
                    self.ir_config = {'k': f[:5].astype(float), 'max': float(f[5]),
                                      'step': float(f[6]), 'alpha': float(f[7]),
                                      'runaway': runaway, 'limit': limit}
                    self._ok()
        elif cmd == Commands.IR_REPORT and self.features & FW_FEATURE_IR_COMP:
            self.out += (np.uint32([self.ir_runs, self.ir_limited, self.ir_slewed,
                                    self.ir_runaways, self.ir_limits]).tobytes()
                         + np.float32([self.ir_max_abs, getattr(self, 'ir_corr', 0.0)]).tobytes())
        elif cmd == Commands._RESET_BUFFER:
            self._reset_ir_report()
            super()._legacy(cmd, p)
        elif cmd == Commands.EXECUTE_VOLTAGE_BATCH:
            super()._legacy(cmd, p)
            flags = self.batch_flags[-1] if (self.batch_count and self.batch_flags) else 0
            self.ir_on = bool(self.honour_ir and self.ir_config is not None
                              and flags & BATCH_FLAG_IR_COMP
                              and flags & BATCH_FLAG_SAMPLE_AT_END)
            self.ir_filtered = self.ir_corr = self.ir_gap = 0.0
            self.ir_pinned = self.ir_run = 0
            if self.ir_on and self.batch_count:
                self.ir_runs += 1
        else:
            super()._legacy(cmd, p)

    def _step(self):
        if not self.ir_on or self.batch_ind >= self.batch_count:
            return super()._step()
        if self.bytes_written - self.batch_ind * 4 < 4:
            return super()._step()           # starved: the parent counts it
        cfg = self.ir_config
        at = self.batch_byte
        setpoint = float(np.frombuffer(bytes(self.ring[at:at + 4]), np.float32)[0])
        applied = setpoint + self.ir_corr
        pinned = abs(self.ir_corr) >= cfg['max']
        if applied < 0.0 or applied > 3.3:
            applied = min(max(applied, 0.0), 3.3)
            pinned = True
        if pinned:
            self.ir_limited += 1
            self.ir_pinned += 1
            if self.ir_pinned >= cfg['limit']:
                self.ir_limits += 1
                self.batch_count = 0
                self.switch = 0
                return False
        else:
            self.ir_pinned = 0
        self.ir_max_abs = max(self.ir_max_abs, abs(self.ir_corr))
        self.ring[at:at + 4] = np.float32(applied).tobytes()
        start = len(self.out)
        gain = self.gain
        if not super()._step():
            return False
        # The end-of-step reading just produced sets the next correction
        we = float(np.frombuffer(bytes(self.out[start:start + 4]), np.float32)[0])
        wanted = cfg['k'][gain] * (we - self.zero)
        self.ir_filtered += cfg['alpha'] * (wanted - self.ir_filtered)
        nxt = min(max(self.ir_filtered, self.ir_corr - cfg['step']),
                  self.ir_corr + cfg['step'])
        slewed = nxt != self.ir_filtered
        nxt = min(max(nxt, -cfg['max']), cfg['max'])
        gap = abs(self.ir_filtered - nxt)
        self.ir_slewed += slewed
        if slewed and abs(nxt) > abs(self.ir_corr) and gap >= self.ir_gap:
            self.ir_run += 1
            if self.ir_run >= cfg['runaway']:
                self.ir_runaways += 1
                self.batch_count = 0
                self.switch = 0
        else:
            self.ir_run = 0
        self.ir_gap = gap if slewed else 0.0
        self.ir_corr = nxt
        return True


def make(**fw_kwargs):
    fw = IrFirmware(**fw_kwargs)
    ps = Potentiostat.__new__(Potentiostat)
    Potentiostat.__init__(ps, serial_port="FAKE")
    ps.serial = fw
    ps.Resistor = 0
    ps.ResistorVal = 100.0
    ps.set_calibration_params(1.2081, 0.049, READ_SLOPE, READ_INTERCEPT_MA)
    return ps, fw


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ps4_ref.time, "sleep", lambda s: None)


CV = dict(min_V=-0.4, max_V=0.4, cycles=1, mV_s=200.0, step_hz=250.0,
          start_V=0.0, last_V=0.0, quiet_s=0.0)


# ----------------------------------------------------------------------
# The driver's configuration of the board's loop
# ----------------------------------------------------------------------

def test_factors_turn_a_reading_into_R_times_the_recorded_current():
    """What the board adds for a reading must be R_comp times the current the
    driver records for that reading, in DAC volts, on every range."""
    ps, _ = make()
    R_comp = 170.0
    k = ps._ir_factors(R_comp)
    zero_adc = _we_out(ZERO_V)
    for r in range(len(RANGE_OHMS)):
        current_mA = 0.2 * RANGE_OHMS[0] / RANGE_OHMS[r]
        we = _we_out(amplifier_V(current_mA, r))
        recorded_A = ps._calibrated_mA(
            -ps._scale_output_voltages(np.array([we])) / ps._res_table()[r], [r])[0] / 1000.0
        want_dac = (ps._scale_input_voltages(ps._waveform_to_raw_V(R_comp * recorded_A))
                    - ps._scale_input_voltages(ps._waveform_to_raw_V(0.0)))
        assert k[r] * (we - zero_adc) == pytest.approx(want_dac, rel=1e-3)
    # An oxidation current raises the setpoint
    assert k[0] * (_we_out(amplifier_V(0.2, 0)) - zero_adc) > 0


def test_configuration_on_the_wire():
    ps, fw = make()
    ps.configure_ir_compensation(170.0, 4.0, 2.1)
    cfg = fw.ir_config
    assert cfg is not None
    assert cfg['k'] == pytest.approx(ps._ir_factors(170.0), rel=1e-6)
    assert cfg['max'] == pytest.approx(2.1 * ps._dac_V_per_cell_V(), rel=1e-6)
    assert cfg['alpha'] == pytest.approx(IR_FEEDBACK_ALPHA)
    assert cfg['runaway'] == int(np.ceil(IR_RUNAWAY_MS / 4.0))
    assert cfg['limit'] == int(np.ceil(IR_LIMIT_MS / 4.0))
    assert len(fw.writes[-1]) == 2 + 40     # one USB packet


# ----------------------------------------------------------------------
# A CV through the board's loop
# ----------------------------------------------------------------------

def test_board_adds_R_times_the_current_to_each_setpoint():
    ps, fw = make(dummy_ohm=1000.0)
    data = ps.perform_CV(current_range=0, ir_compensation_ohm=150.0, **CV)
    assert fw.batch_flags[-1] & BATCH_FLAG_IR_COMP
    measured, current_uA, target = data[:, 1], data[:, 2], data[:, 5]
    # The sensed potential runs ahead of the programmed one by R_comp x I:
    # a straight line through the whole scan, give or take one 2.4 mV DAC
    # level and one step of lag
    slope, offset = np.polyfit(current_uA * 1e-6, measured - target, 1)
    assert slope == pytest.approx(150.0, rel=0.03)
    assert abs(offset) < 2e-3
    report = ps.last_batch_report
    assert report['ir']['runs'] == 1 and report['ir']['runaway_aborts'] == 0
    assert report['ir']['max_correction_V'] == pytest.approx(
        150.0 * np.max(np.abs(current_uA)) * 1e-6, rel=0.05)
    assert not report['degraded']


def test_board_loop_auto_ranges():
    ps, fw = make(dummy_ohm=100000.0, switch_at={40: 1, 80: 2})
    data = ps.perform_CV(current_range='auto', ir_compensation_ohm=5000.0, **CV)
    measured, current_uA, target = data[:, 1], data[:, 2], data[:, 5]
    slope, _ = np.polyfit(current_uA * 1e-6, measured - target, 1)
    assert slope == pytest.approx(5000.0, rel=0.05)
    assert fw.batch_flags[-1] & ps4_ref.BATCH_FLAG_AUTO_RANGE


def test_plain_cv_is_untouched():
    ps, fw = make()
    ps.perform_CV(current_range=0, **CV)
    assert not fw.batch_flags[-1] & BATCH_FLAG_IR_COMP
    assert fw.ir_config is None
    assert ps.last_batch_report['ir'] is None


def test_firmware_without_the_loop_is_refused_before_anything_runs():
    ps, fw = make(features=0x3F)
    with pytest.raises(RuntimeError, match="1.8"):
        ps.perform_CV(current_range=0, ir_compensation_ohm=150.0, **CV)
    assert not fw.batches


def test_a_run_the_board_did_not_compensate_is_flagged():
    ps, fw = make(honour_ir=False)
    ps.perform_CV(current_range=0, ir_compensation_ohm=150.0, **CV)
    assert any("not compensated" in d for d in ps.last_batch_report['degraded'])


def test_runaway_ends_the_run_with_the_cell_disconnected():
    """R_u overestimated threefold on a low-resistance cell: the correction
    grows until it pins, and the board stops the run."""
    ps, fw = make(dummy_ohm=100.0)
    with pytest.raises(WaveformTrackingError, match="ran away"):
        ps.perform_CV(current_range=0, ir_compensation_ohm=300.0, **CV)
    assert fw.switch == 0 and fw.ir_runaways == 1 and fw.ir_limits == 0
    assert ps._switch_state is False
    # Stopped a few tens of mV past the program, not at the output's limit
    assert fw.ir_max_abs < 0.35 * ps._dac_V_per_cell_V()


def test_correction_may_use_all_the_room_beyond_the_sweep():
    """The cap is the output's headroom past the sweep's largest potential,
    not a fixed volt: a scan to -2.5 V keeps ~2.1 V of room."""
    ps, fw = make(dummy_ohm=1000.0)
    ps.perform_CV(current_range=0, ir_compensation_ohm=150.0,
                  **dict(CV, min_V=-2.5, max_V=0.0, mV_s=1000.0))
    limit = ps.last_batch_report['ir']['limit_V']
    assert limit == pytest.approx(HW_VOLTAGE_COMPLIANCE_V - 2.5, abs=0.01)
    assert fw.ir_config['max'] == pytest.approx(limit * ps._dac_V_per_cell_V(), rel=1e-3)


def test_a_drop_larger_than_the_room_beyond_the_sweep_stops_the_run():
    """80 % of a 1 kohm cell's resistance compensated up to 1 V: the
    correction would need ~4 V, past the 3.6 V of room, so the board stops."""
    ps, fw = make(dummy_ohm=1000.0)
    with pytest.raises(WaveformTrackingError, match="beyond what the potentiostat"):
        ps.perform_CV(current_range=0, ir_compensation_ohm=800.0,
                      **dict(CV, min_V=0.0, max_V=1.0, start_V=0.0, last_V=0.0))
    assert fw.ir_limits == 1 and fw.ir_runaways == 0 and fw.switch == 0


# ----------------------------------------------------------------------
# The worker's choice of loop
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
    return win


def spy(monkeypatch, features):
    """Mock board announcing ``features``; records how each run was driven."""
    import moles.ui.electroanalysis.workers as workers
    from moles.driver.mock import MockLivePotentiostat
    seen = {}

    class _Spy(MockLivePotentiostat):
        def firmware_features(self):
            return features

        def perform_CV(self, *args, ir_compensation_ohm=0.0, **kwargs):
            seen['cv'] = dict(kwargs, ir_compensation_ohm=ir_compensation_ohm)
            return super().perform_CV(*args, **kwargs)

        def perform_CV_iR(self, *args, **kwargs):
            seen['host_loop'] = kwargs
            return super().perform_CV_iR(*args, **kwargs)

    monkeypatch.setattr(workers, "MockLivePotentiostat", _Spy)
    return seen


def run_live(window, R_u, percent):
    from pathlib import Path
    from moles.ui.electroanalysis.workers import VoltammetryWorker
    cfg = window.cv_tab._panels['A'].config
    cfg.ir_box.setChecked(True)
    cfg.ir_mode.setCurrentIndex(cfg.ir_mode.findData('live'))
    cfg.r_u.setValue(R_u)
    cfg.ir_percent.setValue(percent)
    params = window.cv_tab._panels['A'].get_params()
    worker = VoltammetryWorker('A', 'MOCK_A', params, use_mock=True)
    captured = {}
    worker.signals.finished.connect(
        lambda p, s, m, r, fp: captured.update(success=s, msg=m, fp=fp))
    worker.run()
    assert captured['success'] is True, captured.get('msg')
    return Path(captured['fp']).with_suffix('.params.txt').read_text()


def test_live_mode_uses_the_boards_loop_when_it_has_one(window, monkeypatch):
    seen = spy(monkeypatch, FW_FEATURE_IR_COMP)
    params = run_live(window, 240.0, 80)
    assert 'host_loop' not in seen
    assert seen['cv']['ir_compensation_ohm'] == pytest.approx(0.8 * 240.0)
    assert seen['cv']['current_range'] == 'auto'     # no longer pinned to range 0
    assert "ir_loop: on the board" in params


def test_live_mode_falls_back_to_this_computer_on_older_firmware(window, monkeypatch):
    seen = spy(monkeypatch, 0x3F)
    params = run_live(window, 240.0, 70)
    assert 'host_loop' in seen and 'cv' not in seen
    assert seen['host_loop']['current_range'] == 0
    assert "ir_loop: this computer" in params


def test_compensation_share_is_capped(window):
    cfg = window.cv_tab._panels['A'].config
    assert cfg.ir_percent.maximum() == int(round(IR_MAX_FRACTION * 100))


def test_worker_caps_a_share_that_did_not_come_through_the_form(window, monkeypatch):
    from moles.ui.electroanalysis.workers import VoltammetryWorker
    seen = spy(monkeypatch, FW_FEATURE_IR_COMP)
    cfg = window.cv_tab._panels['A'].config
    cfg.ir_box.setChecked(True)
    cfg.ir_mode.setCurrentIndex(cfg.ir_mode.findData('live'))
    cfg.r_u.setValue(240.0)
    params = dict(window.cv_tab._panels['A'].get_params(), compensation_fraction=0.97)
    worker = VoltammetryWorker('A', 'MOCK_A', params, use_mock=True)
    worker.run()
    assert seen['cv']['ir_compensation_ohm'] == pytest.approx(IR_MAX_FRACTION * 240.0)

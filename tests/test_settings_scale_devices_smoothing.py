"""Interface scale, unplugged-board listing, and smoothing methods with defaults."""

import numpy as np
import pytest

from moles import settings
from moles.analysis import smoothing


# ----------------------------------------------------------------------
# Smoothing methods
# ----------------------------------------------------------------------

def _noisy_peak(n=600, width=12.0, seed=1):
    rng = np.random.default_rng(seed)
    x = np.arange(n, dtype=float)
    clean = 10.0 * np.exp(-0.5 * ((x - n / 2) / width) ** 2)
    return clean, clean + rng.normal(0, 0.3, n)


def test_savitzky_golay_keeps_a_peak_that_a_rolling_average_flattens():
    clean, noisy = _noisy_peak()
    window = 31
    sg = smoothing.savitzky_golay(noisy, window)
    ma = smoothing.moving_average(noisy, window)
    # Both remove noise...
    for smoothed in (sg, ma):
        assert np.std(np.diff(smoothed)) < np.std(np.diff(noisy)) / 3
    # ...but only Savitzky-Golay keeps the peak height and overall shape.
    assert abs(sg.max() - clean.max()) < 0.5
    assert clean.max() - ma.max() > 1.0
    assert np.sqrt(np.mean((sg - clean) ** 2)) < np.sqrt(np.mean((ma - clean) ** 2))


def test_savitzky_golay_edge_cases():
    ramp = np.arange(20, dtype=float)
    np.testing.assert_allclose(smoothing.savitzky_golay(ramp, 7), ramp, atol=1e-9)  # a line passes through
    np.testing.assert_allclose(smoothing.savitzky_golay(ramp, 6),         # even rounds up to 7
                               smoothing.savitzky_golay(ramp, 7))
    np.testing.assert_array_equal(smoothing.savitzky_golay(ramp, 3), ramp)  # too short to smooth
    assert len(smoothing.savitzky_golay(np.ones(4), 51)) == 4              # window cut to fit
    assert len(smoothing.savitzky_golay(np.array([]), 5)) == 0


def test_smooth_dispatches_and_none_leaves_data_alone():
    _, noisy = _noisy_peak()
    np.testing.assert_array_equal(smoothing.smooth(noisy, "none", 25), noisy)
    np.testing.assert_array_equal(smoothing.smooth(noisy, "moving_average", 25),
                                  smoothing.moving_average(noisy, 25))
    np.testing.assert_array_equal(smoothing.smooth(noisy, "savgol", 25),
                                  smoothing.savitzky_golay(noisy, 25))
    assert smoothing.describe("none", 25) == "off"
    assert smoothing.describe("moving_average", 1) == "off"
    assert smoothing.describe("moving_average", 9) == "rolling average, 9 points"


def test_rolling_average_skips_missing_points():
    y = np.arange(10, dtype=float)
    y[4] = np.nan
    out = smoothing.moving_average(y, 3)
    assert np.isnan(out[4])                         # the gap stays a gap
    assert np.isfinite(np.delete(out, 4)).all()     # and does not spread
    assert out[3] == pytest.approx((2 + 3) / 2)     # averages only real points


def test_smoothed_current_caches_per_method():
    class Data:
        current = np.linspace(0, 1, 50) ** 2
    d = Data()
    ma = smoothing.smoothed_current(d, 9, "moving_average")
    sg = smoothing.smoothed_current(d, 9, "savgol")
    assert not np.array_equal(ma, sg)
    assert smoothing.smoothed_current(d, 9, "savgol") is sg
    assert smoothing.smoothed_current(d, 9, "none") is d.current


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

def test_new_settings_defaults():
    assert settings.get_interface_scale_percent() == 100
    assert settings.get_show_unconnected_potentiostats() is False
    assert settings.get_electroanalysis_smoothing() == ("none", 5)


def test_new_settings_round_trip_and_bad_values():
    settings.set_interface_scale_percent(125)
    settings.set_show_unconnected_potentiostats(True)
    settings.set_electroanalysis_smoothing("savgol", 15)
    assert settings.get_interface_scale_percent() == 125
    assert settings.get_show_unconnected_potentiostats() is True
    assert settings.get_electroanalysis_smoothing() == ("savgol", 15)
    with pytest.raises(ValueError):
        settings.set_interface_scale_percent(133)
    with pytest.raises(ValueError):
        settings.set_electroanalysis_smoothing("median", 5)

    # A hand-edited file with nonsense falls back to the defaults.
    settings._save_raw({"interface_scale_percent": "big",
                        "electroanalysis_smoothing": {"method": "x", "window": 10_000}})
    assert settings.get_interface_scale_percent() == 100
    assert settings.get_electroanalysis_smoothing() == ("none", 200)


def test_interface_scale_sets_qt_scale_factor_once_saved(monkeypatch):
    monkeypatch.setenv("QT_SCALE_FACTOR", "1.7")      # set by hand
    settings.apply_interface_scale()
    import os
    assert os.environ["QT_SCALE_FACTOR"] == "1.7"     # untouched until a scale is saved
    settings.set_interface_scale_percent(150)
    settings.apply_interface_scale()
    assert os.environ["QT_SCALE_FACTOR"] == "1.5"
    settings.set_interface_scale_percent(100)
    settings.apply_interface_scale()
    assert os.environ["QT_SCALE_FACTOR"] == "1"       # a stale scale from a parent is undone


def test_settings_dialog_saves_the_new_controls(qapp):
    from moles.ui.settings.dialog import SettingsDialog
    settings.set_data_folders_from_parent(settings.SETTINGS_PATH.parent / "data")
    dlg = SettingsDialog()
    assert dlg.scale_combo.currentData() == 100
    assert not dlg.show_unconnected_check.isChecked()
    assert dlg.smoothing_control.value() == ("none", 5)

    dlg.scale_combo.setCurrentIndex(dlg.scale_combo.findData(175))
    dlg.show_unconnected_check.setChecked(True)
    dlg.smoothing_control.set_value("moving_average", 21)
    dlg._on_save()
    assert settings.get_interface_scale_percent() == 175
    assert settings.get_show_unconnected_potentiostats() is True
    assert settings.get_electroanalysis_smoothing() == ("moving_average", 21)


# ----------------------------------------------------------------------
# Default smoothing reaches new traces
# ----------------------------------------------------------------------

def test_data_preview_traces_start_with_the_default_smoothing(qapp):
    from moles.ui.electroanalysis.analysis_tab import AnalysisTab
    settings.set_electroanalysis_smoothing("savgol", 11)
    tab = AnalysisTab()
    x = np.linspace(-0.5, 0.5, 200)
    tab.add_dataset("demo", x, np.sin(x * 20))
    row = next(iter(tab._rows.values()))
    assert row.smoothing.value() == ("savgol", 11)
    assert row.smoothing.window_spin.isEnabled()


def test_data_analysis_experiments_start_with_the_default_smoothing():
    from moles.analysis.model import ExperimentView, PlotWindowState

    class Data:
        name, sha256 = "demo", "abc"
        current = np.zeros(50)

        def __len__(self):
            return 50

    state = PlotWindowState("Plot 1")
    exp = state.add_experiment(Data(), "demo.csv", smoothing=("savgol", 15))
    assert (exp.smoothing_method, exp.smoothing_points) == ("savgol", 15)
    assert ExperimentView.from_dict(exp.to_dict()).smoothing_method == "savgol"

    # Sessions saved before the method choice existed meant a rolling average.
    old = {k: v for k, v in exp.to_dict().items() if k != "smoothing_method"}
    assert ExperimentView.from_dict(old).smoothing_method == "moving_average"


# ----------------------------------------------------------------------
# Unplugged boards
# ----------------------------------------------------------------------

@pytest.fixture
def ports(tmp_path):
    """One registered board whose port exists (plugged in) and one whose doesn't."""
    plugged = tmp_path / "tty.usbmodemPLUGGED"
    plugged.touch()
    return {"A": str(plugged), "B": str(tmp_path / "tty.usbmodemGONE")}


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX port paths")
def test_connected_ports_checks_presence_without_opening(ports):
    from moles.calibration import connected_ports
    assert connected_ports(ports.values()) == {ports["A"]}
    assert connected_ports(["", None]) == set()


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX port paths")
@pytest.mark.parametrize("show", [False, True])
def test_electroanalysis_lists_unplugged_boards_only_when_asked(qapp, ports, monkeypatch, show):
    import moles.ui.electroanalysis.control_tab as control_tab
    monkeypatch.setattr(control_tab, "get_potentiostat_ports", lambda: dict(ports))
    settings.set_show_unconnected_potentiostats(show)
    tab = control_tab.ControlTab("CV")
    if not show:
        assert list(tab._panels) == ["A"]
    else:
        assert sorted(tab._panels) == ["A", "B"]
        assert tab._panels["A"].isEnabled()
        assert not tab._panels["B"].isEnabled()
        assert "Not connected" in tab._panels["B"].status_label.text()


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX port paths")
def test_electroanalysis_explains_hidden_boards(qapp, tmp_path, monkeypatch):
    import moles.ui.electroanalysis.control_tab as control_tab
    monkeypatch.setattr(control_tab, "get_potentiostat_ports",
                        lambda: {"A": str(tmp_path / "gone")})
    tab = control_tab.ControlTab("CV")
    assert tab._panels == {}
    assert "1 registered board hidden" in tab.empty_label.text()


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX port paths")
@pytest.mark.parametrize("show", [False, True])
def test_electrolysis_lists_unplugged_boards_only_when_asked(qapp, ports, tmp_path,
                                                            monkeypatch, show):
    import moles.ui.electrolysis.guardian as guardian_module
    import moles.ui.electrolysis.main_window as mw
    monkeypatch.setattr(guardian_module, "STOP_FILE", tmp_path / "STOP_ALL")
    monkeypatch.setattr(guardian_module, "LOG_DIR", tmp_path)
    monkeypatch.setattr(mw, "get_potentiostat_ports", lambda: dict(ports))
    settings.set_show_unconnected_potentiostats(show)
    win = mw.MainWindow()
    try:
        for timer in (win._drain_timer, win._plot_timer, win._heartbeat_timer, win._claims_timer):
            timer.stop()
        rows = {win._row_pid(r): win.ps_table.item(r, mw.COL_STATUS).text()
                for r in range(win.ps_table.rowCount())}
        if not show:
            assert list(rows) == ["A"]
            assert "1 registered board(s) not plugged in are hidden" in win.status_label.text()
        else:
            assert rows["B"] == mw.NOT_CONNECTED and rows["A"] != mw.NOT_CONNECTED
            # Starting an unplugged board is refused before any worker is made.
            for r in range(win.frozen_col.rowCount()):
                win.frozen_col.item(r, win.FROZEN_COL_CHECK).setCheckState(
                    mw.QtCore.Qt.Checked if win._row_pid(r) == "B" else mw.QtCore.Qt.Unchecked)
            win.start_checked()
            assert "B" not in win.workers and "B" not in win._pending_starts
            assert "Not plugged in: PS B" in win.status_label.text()
    finally:
        win.guardian.shutdown()
        win.guardian.join(5.0)


def test_live_marker_is_a_quarter_smaller(qapp):
    from moles.ui.electroanalysis.control_tab import ControlTab
    tab = ControlTab("CV")
    tab.test_mode_cb.setChecked(True)
    marker = tab._plots["A"]["marker"]
    assert marker.opts["symbolSize"] == pytest.approx(15 * 0.75)

"""Data-folder setup, scroll-wheel guard, and live newest-point marker."""

from pathlib import Path

import numpy as np
import pytest

from moles import settings


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Point moles.settings at an empty settings file in the test folder."""
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    return tmp_path


# ----------------------------------------------------------------------
# Data folders
# ----------------------------------------------------------------------

def test_defaults_do_not_point_at_dropbox():
    for folder in (settings.DEFAULT_DATA_FOLDER, settings.DEFAULT_CV_DATA_FOLDER):
        assert "Dropbox" not in folder.parts
        assert folder.parent == settings.DEFAULT_PARENT_FOLDER


def test_fresh_install_is_not_configured(settings_file):
    assert settings.data_folders_configured() is False
    assert settings.get_data_folder() == settings.DEFAULT_DATA_FOLDER


def test_parent_folder_sets_both_subfolders(settings_file):
    parent = settings_file / "MOLES Data"
    settings.set_data_folders_from_parent(parent)
    assert settings.data_folders_configured() is True
    assert settings.get_data_folder() == parent / "Electrolysis Data"
    assert settings.get_cv_data_folder() == parent / "Electroanalysis Data"


def test_prompt_saves_and_creates_new_folders(qapp, settings_file):
    from moles.ui.settings.data_folder_prompt import DataFolderPrompt
    dlg = DataFolderPrompt(offer_legacy=False)
    assert dlg.keep_radio is None
    parent = settings_file / "chosen"
    dlg.folder_edit.setText(str(parent))
    dlg._on_save()
    assert dlg.result() == dlg.Accepted
    assert (parent / "Electrolysis Data").is_dir()
    assert (parent / "Electroanalysis Data").is_dir()
    assert settings.get_cv_data_folder() == parent / "Electroanalysis Data"


def test_prompt_can_keep_old_dropbox_folders(qapp, settings_file, monkeypatch):
    import moles.ui.settings.data_folder_prompt as prompt
    old_e, old_cv = settings_file / "EChem Results", settings_file / "CV Results"
    monkeypatch.setattr(prompt, "LEGACY_DATA_FOLDER", old_e)
    monkeypatch.setattr(prompt, "LEGACY_CV_DATA_FOLDER", old_cv)
    dlg = prompt.DataFolderPrompt(offer_legacy=True)
    assert dlg.keep_radio.isChecked()  # existing users keep their folders by default
    assert not dlg.folder_edit.isEnabled()
    dlg._on_save()
    assert settings.get_data_folder() == old_e
    assert settings.get_cv_data_folder() == old_cv


def test_ensure_skips_prompt_once_configured(qapp, settings_file, monkeypatch):
    import moles.ui.settings.data_folder_prompt as prompt
    settings.set_data_folders_from_parent(settings_file)

    def fail(*a, **k):
        raise AssertionError("prompt shown although folders are set")
    monkeypatch.setattr(prompt.DataFolderPrompt, "exec_", fail)
    assert prompt.ensure_data_folders() is True


# ----------------------------------------------------------------------
# Scroll wheel
# ----------------------------------------------------------------------

def _wheel(widget, dy=-120):
    from PyQt5 import QtCore, QtGui
    pos = QtCore.QPointF(widget.rect().center())
    return QtGui.QWheelEvent(
        pos, QtCore.QPointF(widget.mapToGlobal(pos.toPoint())),
        QtCore.QPoint(0, 0), QtCore.QPoint(0, dy),
        QtCore.Qt.NoButton, QtCore.Qt.NoModifier, QtCore.Qt.NoScrollPhase, False)


def test_wheel_over_inputs_leaves_values_and_passes_event_on(qapp):
    """The guard skips the box's own wheel handler and leaves the event
    un-accepted. For a real mouse wheel, Qt then hands an un-accepted event to
    the parent widgets, so the surrounding panel scrolls. (Events sent by a
    test are never passed to parents, so that last step can't be shown here.)
    """
    from PyQt5 import QtWidgets
    from moles.ui.wheel_guard import install_wheel_guard
    install_wheel_guard(qapp)
    install_wheel_guard(qapp)  # a second call must not stack a second filter

    spin = QtWidgets.QDoubleSpinBox()
    spin.setValue(5.0)
    combo = QtWidgets.QComboBox()
    combo.addItems(["a", "b", "c"])
    for widget in (spin, combo):
        event = _wheel(widget)
        QtWidgets.QApplication.sendEvent(widget, event)
        assert not event.isAccepted()
    assert spin.value() == 5.0
    assert combo.currentIndex() == 0


def test_wheel_guard_leaves_other_widgets_alone(qapp):
    from PyQt5 import QtWidgets
    from moles.ui.wheel_guard import install_wheel_guard
    install_wheel_guard(qapp)
    slider = QtWidgets.QSlider()
    slider.setRange(0, 100)
    slider.setValue(50)
    QtWidgets.QApplication.sendEvent(slider, _wheel(slider))
    assert slider.value() != 50


# ----------------------------------------------------------------------
# Newest-point marker
# ----------------------------------------------------------------------

@pytest.fixture
def cv_tab(qapp, tmp_path, monkeypatch):
    import moles.ui.electroanalysis.control_tab as control_tab
    monkeypatch.setattr(control_tab, "get_cv_data_folder", lambda: tmp_path)
    from moles.ui.electroanalysis.main_window import MainWindow
    win = MainWindow()
    win.cv_tab.test_mode_cb.setChecked(True)
    yield win.cv_tab  # yield, not return: the window must outlive the test


def _marker_xy(item):
    x, y = item.getData()
    return ([] if x is None else list(x)), ([] if y is None else list(y))


def test_marker_follows_newest_point_while_running(cv_tab, monkeypatch):
    from PyQt5 import QtWidgets
    pid = 'A'
    cv_tab._popout_channel(pid)
    cv_tab._active_workers[pid] = object()  # stands in for a running worker
    cv_tab._on_data_chunk(pid, [0.0, 0.1, 0.2], [1.0, 2.0, 3.0])
    cv_tab._flush_live_plots()
    for item in (cv_tab._plots[pid]['marker'], cv_tab._popouts[pid]['marker']):
        assert _marker_xy(item) == ([0.2], [3.0])

    cv_tab._on_data_chunk(pid, np.array([0.15]), np.array([2.5]))
    cv_tab._flush_live_plots()
    assert _marker_xy(cv_tab._plots[pid]['marker']) == ([0.15], [2.5])

    # A stopped run removes the marker (its partial-save offer is declined).
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.No))
    cv_tab._on_finished(pid, False, "Stopped", None, None)
    for item in (cv_tab._plots[pid]['marker'], cv_tab._popouts[pid]['marker']):
        assert _marker_xy(item) == ([], [])
    cv_tab._popouts[pid]['win'].close()


def test_marker_absent_when_no_run_active(cv_tab):
    pid = 'B'
    cv_tab._on_data_chunk(pid, [0.0, 0.1], [1.0, 2.0])
    cv_tab._flush_live_plots()
    assert _marker_xy(cv_tab._plots[pid]['marker']) == ([], [])

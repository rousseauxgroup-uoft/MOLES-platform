"""Saving, reopening, and protecting analysis sessions in the Data Analysis window."""

import os
import shutil

import numpy as np
import pytest

CV_HEADER = "Time (s),Potential (V),Current (uA),Cycle,Index,Target V,Range"


def _write_cv(path, scale=1.0):
    n = 2000
    target = np.concatenate([np.linspace(0, -1, n // 2), np.linspace(-1, 0, n // 2)])
    current = scale * 40 * np.sin(np.linspace(0, 4 * np.pi, n))
    rows = np.column_stack([np.arange(n) * 0.004, target, current, np.zeros(n),
                            np.zeros(n), target, np.full(n, 3)])
    np.savetxt(path, rows, delimiter=",", header=CV_HEADER, comments="", fmt="%.6f")


@pytest.fixture
def raw(tmp_path, monkeypatch):
    import moles.analysis.workspace as workspace_module
    folder = tmp_path / "raw"
    folder.mkdir()
    _write_cv(folder / "fc.csv")
    _write_cv(folder / "ni.csv", scale=2.0)
    monkeypatch.setattr(workspace_module, "moles_data_folders", lambda: [folder])
    return folder


class Answers:
    """What the stand-in dialogs answer, and what they were asked."""

    def __init__(self):
        from moles.ui.data_analysis.main_window import DISCARD
        self.save_changes = DISCARD
        self.session_name = "Figure 3"
        self.overwrite = True
        self.chosen = None
        self.asked = []
        self.relinks = []


def _make_window(tmp_path, answers):
    from PyQt5 import QtCore
    from moles.ui.data_analysis.main_window import DataAnalysisWindow
    win = DataAnalysisWindow(QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat))

    def ask_save():
        answers.asked.append("save changes")
        return answers.save_changes

    def ask_name(default):
        answers.asked.append(f"name (default {default})")
        return answers.session_name

    win.ask_save_changes = ask_save
    win.ask_session_name = ask_name
    win.confirm_overwrite = lambda name: answers.overwrite
    win.choose_session = lambda saved: answers.chosen
    win.report_relink = answers.relinks.append
    win.explorer.show_error = lambda title, text: None
    return win


@pytest.fixture
def answers():
    return Answers()


@pytest.fixture
def window(qapp, tmp_path, raw, answers):
    folder = tmp_path / "Analysis"
    folder.mkdir()
    win = _make_window(tmp_path, answers)
    assert win.open_workspace(folder)
    win.raw = raw
    yield win
    from moles.ui.data_analysis.main_window import DISCARD
    answers.save_changes = DISCARD
    win.close()


def _plot_files(window, *names):
    rels = window.explorer.add_paths([window.raw / n for n in names])
    panel = window.plot_area.current_panel()
    return panel, panel.add_files(rels)


def test_a_new_workspace_starts_an_unsaved_but_clean_session(window):
    assert window.session_name is None
    assert not window.isWindowModified()
    assert window.windowTitle().endswith("Unsaved session[*]")


def test_edits_mark_the_session_unsaved_but_looking_around_does_not(window, qapp):
    panel, (uid,) = _plot_files(window, "fc.csv")
    assert window.isWindowModified()
    window.save_session()
    assert not window.isWindowModified()

    panel.select(None)
    panel.select(uid)
    panel.fit_view()
    window.plot_area.pop_out(panel)
    window.plot_area.dock_back(panel)
    assert not window.isWindowModified()

    panel._cards[uid].x_spin.setValue(0.2)
    assert window.isWindowModified()


def test_saving_asks_for_a_name_once(window, answers):
    _plot_files(window, "fc.csv")

    assert window.save_session()
    assert answers.asked == ["name (default Session 1)"]
    assert (window.workspace.sessions_dir / "Figure 3.molesanalysis").is_file()
    assert window.windowTitle().endswith("Figure 3[*]")

    window.plot_area.new_plot()
    assert window.save_session()
    assert answers.asked == ["name (default Session 1)"]   # Save reuses the name


def test_save_as_will_not_replace_another_session_without_asking(window, answers):
    _plot_files(window, "fc.csv")
    window.save_session()
    answers.session_name = "Other"
    window.save_session_as()
    answers.session_name, answers.overwrite = "Figure 3", False

    assert not window.save_session_as()
    assert window.session_name == "Other"


def test_a_saved_session_reopens_exactly_with_the_workspace(window, tmp_path, answers, qapp):
    from moles.analysis.model import LabelledPoint, PeakSettings
    from moles.ui.data_analysis.main_window import DataAnalysisWindow

    panel, (fc, ni) = _plot_files(window, "fc.csv", "ni.csv")
    panel.set_title("Ni intermediates")
    card = panel._cards[ni]
    card.x_spin.setValue(-0.36)
    card.y_spin.setValue(12.5)
    card.style_combo.setCurrentIndex(card.style_combo.findData("dash"))
    card.start_spin.setValue(100)
    exp = panel.state.experiment(ni)
    exp.add_label(LabelledPoint(500, "shoulder", "min", text_offset=(-30.0, 12.0)))
    panel.refresh_labels(ni)
    panel.state.peak_settings = PeakSettings(smoothing_mV=25.0, prominence_uA=3.0)
    second = window.plot_area.new_plot()
    second.add_files(["fc.csv"])
    window.plot_area.pop_out(second)
    window.plot_area.tabs.setCurrentWidget(panel)
    assert window.save_session()
    saved = window.plot_area.to_session().to_dict()

    again = _make_window(tmp_path, answers)
    try:
        assert again.restore_last_workspace()
        assert again.session_name == "Figure 3" and not again.isWindowModified()
        restored = again.plot_area.to_session().to_dict()
        for w in saved["windows"] + restored["windows"]:
            w.pop("geometry")   # where a pop-out sits depends on the screen
        assert restored == saved
        assert again.plot_area.is_popped_out(again.plot_area.panels[1])
        assert again.plot_area.current_panel().title == "Ni intermediates"
        drawn = again.plot_area.panels[0]._drawn
        assert len(drawn) == 2 and len(drawn[ni].labels) == 1
    finally:
        again.close()


def test_closing_with_unsaved_changes_asks_first(window, answers):
    from moles.ui.data_analysis.main_window import CANCEL, SAVE
    _plot_files(window, "fc.csv")

    answers.save_changes = CANCEL
    assert not window.close()
    assert answers.asked == ["save changes"]

    answers.save_changes = SAVE
    assert window.close()
    assert (window.workspace.sessions_dir / "Figure 3.molesanalysis").is_file()


def test_switching_workspace_can_be_cancelled(window, tmp_path, answers):
    from moles.ui.data_analysis.main_window import CANCEL
    _plot_files(window, "fc.csv")
    other = tmp_path / "Other"
    other.mkdir()

    answers.save_changes = CANCEL
    assert not window.open_workspace(other)
    assert window.workspace.root.name == "Analysis"
    assert window.plot_area.panels[0].state.experiments


def test_new_session_starts_over(window, answers):
    _plot_files(window, "fc.csv")
    window.save_session()
    window.plot_area.new_plot()

    window.new_session()                      # unsaved plot: discarded on the answer

    assert answers.asked[-1] == "save changes"
    assert window.session_name is None and len(window.plot_area.panels) == 1
    assert window.plot_area.panels[0].state.experiments == []


def test_open_session_switches_between_saved_sessions(window, answers):
    _plot_files(window, "fc.csv")
    window.save_session()                     # "Figure 3": one experiment
    window.new_session()
    answers.session_name = "Empty"
    window.save_session()

    answers.chosen = "Figure 3"
    window.open_session_dialog()

    assert window.session_name == "Figure 3"
    assert [e.file for e in window.plot_area.panels[0].state.experiments] == ["fc.csv"]


def test_a_missing_file_stays_in_the_session_and_comes_back(window, tmp_path, answers):
    panel, (fc, ni) = _plot_files(window, "fc.csv", "ni.csv")
    window.save_session()
    os.remove(window.workspace.root / "ni.csv")       # gone, outside the app

    window.load_session("Figure 3")

    panel = window.plot_area.panels[0]
    assert answers.relinks[-1].missing == ["ni.csv"]
    assert panel.missing_files() == ["ni.csv"] and ni not in panel._drawn
    assert [e.file for e in panel.state.experiments] == ["fc.csv", "ni.csv"]
    window.save_session()                                # still saved with it
    assert "ni.csv" in (window.workspace.sessions_dir / "Figure 3.molesanalysis").read_text()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copy2(window.raw / "ni.csv", elsewhere / "ni_again.csv")
    window.explorer.add_paths([elsewhere / "ni_again.csv"])

    assert panel.missing_files() == [] and ni in panel._drawn
    assert panel.state.experiment(ni).file == "ni_again.csv"
    assert window.isWindowModified()


def test_a_file_renamed_in_finder_is_relinked(window, answers):
    _plot_files(window, "fc.csv")
    window.save_session()
    os.rename(window.workspace.root / "fc.csv", window.workspace.root / "ferrocene.csv")

    window.load_session("Figure 3")

    assert answers.relinks[-1].relinked == [("fc.csv", "ferrocene.csv")]
    assert window.plot_area.panels[0].state.experiments[0].file == "ferrocene.csv"
    assert window.isWindowModified()   # the new path is only kept once saved


def test_a_new_order_is_unsaved_work_and_is_kept(window, tmp_path, answers):
    panel, (fc, ni) = _plot_files(window, "fc.csv", "ni.csv")
    window.save_session()

    panel._cards[ni].up_btn.click()

    assert window.isWindowModified()
    window.save_session()
    window.load_session("Figure 3")
    assert [e.uid for e in window.plot_area.panels[0].state.experiments] == [ni, fc]

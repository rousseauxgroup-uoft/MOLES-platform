"""Offscreen tests of the Data Analysis window and its Data Explorer."""

import os
from pathlib import Path

import pytest

CV_TEXT = ("Time (s),Potential (V),Current (uA),Cycle,Index,Target V,Range\n"
           "0.004,-0.001,5.0,0,0,0.0,3\n0.008,-0.002,4.9,0,0,-0.0004,3\n")


@pytest.fixture
def originals(tmp_path, monkeypatch):
    """A raw-data folder that MOLES' settings name as such."""
    import moles.analysis.workspace as workspace_module

    raw = tmp_path / "CV Results"
    raw.mkdir()
    (raw / "Fc_run.csv").write_text(CV_TEXT)
    (raw / "Fc_run.params.txt").write_text("method: CV\nmV_s: 100.0\n")
    (raw / "Ni_run.csv").write_text(CV_TEXT.replace("5.0", "-7.5"))
    monkeypatch.setattr(workspace_module, "moles_data_folders", lambda: [raw])
    return raw


@pytest.fixture
def window(qapp, tmp_path, originals):
    from PyQt5 import QtCore
    from moles.ui.data_analysis.main_window import DataAnalysisWindow

    settings = QtCore.QSettings(str(tmp_path / "settings.ini"), QtCore.QSettings.IniFormat)
    win = DataAnalysisWindow(settings=settings)
    _no_dialogs(win)
    errors = []
    win.explorer.show_error = lambda title, text: errors.append((title, text))
    win.errors = errors
    yield win
    win.close()


@pytest.fixture
def explorer(window, tmp_path):
    folder = tmp_path / "Analysis"
    folder.mkdir()
    assert window.open_workspace(folder)
    return window.explorer


def _no_dialogs(win):
    """Answer the window's questions without showing modal dialogs."""
    from moles.ui.data_analysis.main_window import DISCARD
    win.ask_save_changes = lambda: DISCARD
    win.report_relink = lambda report: None


def _names(explorer):
    return sorted(item.data(0, 256) for item in explorer._all_items())


def _process_events():
    from PyQt5 import QtCore
    QtCore.QCoreApplication.processEvents()


def test_window_starts_without_a_workspace(window):
    assert window.windowTitle() == "MOLES Data Analysis"
    assert window.workspace is None
    assert not window.explorer.add_files_btn.isEnabled()


def test_raw_data_folders_are_refused_as_workspaces(window, originals, monkeypatch):
    from PyQt5 import QtWidgets
    warnings = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *args, **kwargs: warnings.append(args[2]))

    assert not window.open_workspace(originals)
    assert window.workspace is None and len(warnings) == 1


def test_the_last_workspace_reopens(window, explorer, tmp_path):
    from moles.ui.data_analysis.main_window import DataAnalysisWindow

    again = DataAnalysisWindow(settings=window.settings)
    _no_dialogs(again)
    try:
        assert again.restore_last_workspace()
        assert again.workspace.root == window.workspace.root
        assert again.windowTitle().startswith("MOLES Data Analysis — Analysis — ")
    finally:
        again.close()


def test_adding_files_copies_them_and_shows_only_data(explorer, originals):
    before = {p: p.read_bytes() for p in originals.iterdir()}
    added = []
    explorer.files_added.connect(added.append)

    rels = explorer.add_paths([originals / "Fc_run.csv", originals / "Ni_run.csv"])

    assert rels == ["Fc_run.csv", "Ni_run.csv"] and added == [rels]
    assert _names(explorer) == ["Fc_run.csv", "Ni_run.csv"]   # no params, no hidden folder
    assert {p: p.read_bytes() for p in originals.iterdir()} == before
    assert sorted(i.data(0, 256) for i in explorer.tree.selectedItems()) == rels


def test_files_dropped_from_finder_are_copied_into_the_folder_under_them(explorer, originals):
    explorer.workspace.make_folder("", "Figure 3")

    explorer.tree.external_drop.emit([str(originals / "Fc_run.csv")], "Figure 3")

    assert (explorer.workspace.root / "Figure 3" / "Fc_run.csv").is_file()
    assert (originals / "Fc_run.csv").is_file()


def test_duplicates_are_copied_only_when_confirmed(explorer, originals):
    explorer.add_paths([originals / "Fc_run.csv"])

    explorer.confirm_duplicates = lambda duplicates: False
    assert explorer.add_paths([originals / "Fc_run.csv"]) == []

    explorer.confirm_duplicates = lambda duplicates: True
    assert explorer.add_paths([originals / "Fc_run.csv"]) == ["Fc_run (2).csv"]


def test_renaming_in_the_tree_renames_the_copy(explorer, originals):
    explorer.add_paths([originals / "Fc_run.csv"])
    moved = []
    explorer.files_moved.connect(moved.append)

    explorer.item_for("Fc_run.csv").setText(0, "Ferrocene")
    _process_events()

    root = explorer.workspace.root
    assert (root / "Ferrocene.csv").is_file() and (root / "Ferrocene.params.txt").is_file()
    assert moved == [[("Fc_run.csv", "Ferrocene.csv")]]
    assert _names(explorer) == ["Ferrocene.csv"]
    assert (originals / "Fc_run.csv").is_file()


def test_a_refused_rename_is_reported_and_undone(window, explorer, originals):
    explorer.add_paths([originals / "Fc_run.csv", originals / "Ni_run.csv"])

    explorer.item_for("Fc_run.csv").setText(0, "Ni_run.csv")
    _process_events()

    assert window.errors and window.errors[0][0] == "Could not rename"
    assert _names(explorer) == ["Fc_run.csv", "Ni_run.csv"]


def test_moving_into_a_folder(explorer, originals):
    explorer.add_paths([originals / "Fc_run.csv", originals / "Ni_run.csv"])
    folder = explorer.new_folder()
    moved = []
    explorer.files_moved.connect(moved.append)

    explorer.move_into(["Fc_run.csv", "Ni_run.csv"], folder)

    assert folder == "New Folder"
    assert explorer.workspace.data_files() == ["New Folder/Fc_run.csv", "New Folder/Ni_run.csv"]
    assert moved == [[("Fc_run.csv", "New Folder/Fc_run.csv"),
                      ("Ni_run.csv", "New Folder/Ni_run.csv")]]
    # A new folder goes inside the selected one; with nothing selected, at the top.
    assert explorer.new_folder() == "New Folder/New Folder"
    explorer.tree.clearSelection()
    assert explorer.new_folder() == "New Folder 2"


def test_dragging_carries_workspace_paths(explorer, originals):
    from moles.ui.data_analysis.explorer import workspace_paths_from_mime
    explorer.add_paths([originals / "Fc_run.csv"])

    mime = explorer.tree.mimeData([explorer.item_for("Fc_run.csv")])

    assert workspace_paths_from_mime(mime) == ["Fc_run.csv"]
    assert [Path(u.toLocalFile()) for u in mime.urls()] == [explorer.workspace.root / "Fc_run.csv"]


def test_removal_waits_until_no_plot_uses_the_file(window, explorer, originals):
    explorer.add_paths([originals / "Fc_run.csv"])
    explorer.select(["Fc_run.csv"])
    explorer.confirm_remove = lambda rels: True
    trashed = []
    explorer.move_to_trash = lambda path: trashed.append(path) or os.remove(path) or True

    explorer.in_use = lambda rel: True
    assert explorer.remove_selected() == []
    assert window.errors[-1][0] == "Files are open in a plot"

    explorer.in_use = lambda rel: False
    explorer.select(["Fc_run.csv"])
    assert explorer.remove_selected() == ["Fc_run.csv"]
    assert sorted(p.name for p in trashed) == ["Fc_run.csv", "Fc_run.params.txt"]
    assert (originals / "Fc_run.csv").is_file()


def test_tooltip_describes_the_file_and_its_origin(explorer, originals):
    explorer.add_paths([originals / "Fc_run.csv"])

    text = explorer.tooltip_for("Fc_run.csv")

    assert "CV" in text
    assert "Scan rate (mV/s): 100.0" in text
    assert f"Copied from: {(originals / 'Fc_run.csv').resolve()}" in text

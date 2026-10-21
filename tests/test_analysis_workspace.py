"""Workspaces of copied data files (moles.analysis.workspace).

The guarantee these tests hold the workspace to: original data files are only
ever read, and every operation acts on the copies inside the workspace.
"""

import json
import os
from pathlib import Path

import pytest

from moles.analysis.datasets import load_dataset
from moles.analysis.workspace import (
    META_DIR, DuplicateFileError, Workspace, WorkspaceError, file_sha256,
    location_problem,
)

CV_TEXT = ("Time (s),Potential (V),Current (uA),Cycle,Index,Target V,Range\n"
           "0.004,-0.001,5.0,0,0,0.0,3\n0.008,-0.002,4.9,0,0,-0.0004,3\n")


def _snapshot(folder):
    """Content and modification time of every file under ``folder``."""
    return {p: (p.read_bytes(), os.stat(p).st_mtime_ns)
            for p in Path(folder).rglob("*") if p.is_file()}


@pytest.fixture
def originals(tmp_path):
    """A raw-data folder like 'CV Results': two runs with their params files."""
    raw = tmp_path / "CV Results"
    raw.mkdir()
    (raw / "Fc_run.csv").write_text(CV_TEXT)
    (raw / "Fc_run.params.txt").write_text("method: CV\nmV_s: 100.0\n")
    (raw / "Ni_run.csv").write_text(CV_TEXT.replace("5.0", "-7.5"))
    (raw / "Ni_run.params.txt").write_text("method: CV\nmV_s: 50.0\n")
    return raw


@pytest.fixture
def ws(tmp_path, originals):
    folder = tmp_path / "Analysis"
    folder.mkdir()
    return Workspace.open(folder, protected=[originals])


# --- Where a workspace may live ---

def test_a_workspace_cannot_overlap_a_raw_data_folder(tmp_path, originals):
    inside = originals / "sub"
    inside.mkdir()

    assert location_problem(originals, [originals])            # is it
    assert location_problem(inside, [originals])               # inside it
    assert location_problem(tmp_path, [originals])             # contains it
    with pytest.raises(WorkspaceError):
        Workspace.open(originals, protected=[originals])


def test_only_an_empty_folder_becomes_a_new_workspace(tmp_path):
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.csv").write_text("x")
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / ".DS_Store").write_text("")  # hidden clutter does not count

    assert location_problem(busy) is not None
    assert location_problem(empty) is None


def test_an_existing_workspace_reopens_with_its_files(ws, originals):
    ws.add_file(originals / "Fc_run.csv")

    reopened = Workspace.open(ws.root, protected=[originals])

    assert reopened.data_files() == ["Fc_run.csv"]
    assert (ws.root / META_DIR).is_dir()


# --- Adding copies ---

def test_adding_copies_the_file_and_its_params_and_leaves_the_original(ws, originals):
    before = _snapshot(originals)

    result = ws.add_file(originals / "Fc_run.csv")

    assert _snapshot(originals) == before
    assert result.rel == "Fc_run.csv" and result.companion == "Fc_run.params.txt"
    assert (ws.root / "Fc_run.csv").read_bytes() == (originals / "Fc_run.csv").read_bytes()
    assert load_dataset(ws.root / "Fc_run.csv").params["mV_s"] == "100.0"


def test_the_origin_of_each_copy_is_recorded(ws, originals):
    ws.add_file(originals / "Fc_run.csv")

    origins = ws.origins("Fc_run.csv")

    assert [o["original_path"] for o in origins] == [str((originals / "Fc_run.csv").resolve())]
    # The record follows the copy through a rename done outside the app.
    os.rename(ws.root / "Fc_run.csv", ws.root / "renamed.csv")
    assert ws.origins("renamed.csv") == origins


def test_a_raw_dpv_staircase_brings_its_runs_params(tmp_path, ws):
    src = tmp_path / "dpv"
    src.mkdir()
    (src / "run_RAW.csv").write_text("Time (s),Potential (V),Current (uA),Target V,Range\n"
                                      "0.004,-0.5,-1.1,-0.5,3\n0.008,-0.5,-1.2,-0.5,3\n")
    (src / "run.params.txt").write_text("method: DPV\npulse_V: 0.05\n")

    result = ws.add_file(src / "run_RAW.csv")

    assert result.companion == "run_RAW.params.txt"
    assert load_dataset(ws.root / result.rel).params["pulse_V"] == "0.05"


def test_a_taken_name_gets_a_number(tmp_path, ws, originals):
    other = tmp_path / "other"
    other.mkdir()
    (other / "Fc_run.csv").write_text(CV_TEXT + "0.012,-0.003,4.8,0,0,-0.0008,3\n")
    (other / "Fc_run.params.txt").write_text("method: CV\n")
    ws.add_file(originals / "Fc_run.csv")

    result = ws.add_file(other / "Fc_run.csv")

    assert result.rel == "Fc_run (2).csv"
    assert result.companion == "Fc_run (2).params.txt"


def test_identical_content_is_flagged_before_a_second_copy(ws, originals):
    ws.add_file(originals / "Fc_run.csv")

    with pytest.raises(DuplicateFileError) as caught:
        ws.add_file(originals / "Fc_run.csv")
    assert caught.value.existing == ["Fc_run.csv"]

    assert ws.add_file(originals / "Fc_run.csv", allow_duplicate=True).rel == "Fc_run (2).csv"


def test_files_already_inside_or_not_data_are_refused(ws, originals):
    ws.add_file(originals / "Fc_run.csv")

    with pytest.raises(WorkspaceError):
        ws.add_file(ws.root / "Fc_run.csv")
    with pytest.raises(WorkspaceError):
        ws.add_file(originals / "Fc_run.params.txt")


def test_adding_a_folder_keeps_its_layout_and_reports_duplicates(tmp_path, ws, originals):
    nested = originals / "October"
    nested.mkdir()
    (nested / "late.csv").write_text(CV_TEXT.replace("4.9", "4.1"))
    (originals / "empty sub").mkdir()
    ws.add_file(originals / "Fc_run.csv")
    before = _snapshot(originals)

    report = ws.add_folder(originals)

    assert _snapshot(originals) == before
    assert sorted(r.rel for r in report.added) == [
        "CV Results/Ni_run.csv", "CV Results/October/late.csv"]
    assert [(Path(s).name, folder) for s, folder, _ in report.duplicates] == [
        ("Fc_run.csv", "CV Results")]
    assert not (ws.root / "CV Results" / "empty sub").exists()


# --- Organising the copies ---

def test_renaming_a_data_file_renames_its_params_and_keeps_csv(ws, originals):
    ws.add_file(originals / "Fc_run.csv")

    result = ws.rename("Fc_run.csv", "Ferrocene reference")

    assert result.rel == "Ferrocene reference.csv"
    assert result.moved == [("Fc_run.csv", "Ferrocene reference.csv")]
    assert (ws.root / "Ferrocene reference.params.txt").is_file()
    assert not (ws.root / "Fc_run.params.txt").exists()


def test_bad_names_and_clashes_are_refused(ws, originals):
    ws.add_file(originals / "Fc_run.csv")
    ws.add_file(originals / "Ni_run.csv")

    for bad in ("", "  ", "a/b", ".hidden", "x:y"):
        with pytest.raises(WorkspaceError):
            ws.rename("Fc_run.csv", bad)
    with pytest.raises(WorkspaceError):
        ws.rename("Fc_run.csv", "Ni_run.csv")


def test_moving_files_and_folders(ws, originals):
    ws.add_file(originals / "Fc_run.csv")
    ws.add_file(originals / "Ni_run.csv")
    ws.make_folder("", "Figure 3")
    ws.make_folder("", "Paper")

    moved_file = ws.move("Fc_run.csv", "Figure 3")

    assert moved_file.rel == "Figure 3/Fc_run.csv"
    assert (ws.root / "Figure 3" / "Fc_run.params.txt").is_file()

    moved_folder = ws.move("Figure 3", "Paper")

    assert moved_folder.rel == "Paper/Figure 3"
    assert moved_folder.moved == [("Figure 3/Fc_run.csv", "Paper/Figure 3/Fc_run.csv")]
    assert ws.companions_of("Paper/Figure 3/Fc_run.csv") == ["Paper/Figure 3/Fc_run.params.txt"]
    with pytest.raises(WorkspaceError):
        ws.move("Paper", "Paper/Figure 3")   # into itself


def test_paths_outside_the_workspace_are_refused(ws):
    for rel in ("../escape.csv", META_DIR, f"{META_DIR}/provenance.json"):
        with pytest.raises(WorkspaceError):
            ws.path(rel)


def test_removing_sends_file_and_params_to_the_trash(ws, originals):
    ws.add_file(originals / "Fc_run.csv")
    trashed = []

    def fake_trash(path):
        trashed.append(Path(path).name)
        Path(path).unlink()
        return True

    removed = ws.remove("Fc_run.csv", fake_trash)

    assert removed == ["Fc_run.csv"]
    assert sorted(trashed) == ["Fc_run.csv", "Fc_run.params.txt"]
    assert (originals / "Fc_run.csv").is_file()
    with pytest.raises(WorkspaceError):
        ws.remove("", fake_trash)


def test_a_failed_trash_is_reported(ws, originals):
    ws.add_file(originals / "Fc_run.csv")

    with pytest.raises(WorkspaceError):
        ws.remove("Fc_run.csv", lambda path: False)
    assert (ws.root / "Fc_run.csv").is_file()


def test_find_by_hash_relinks_a_file_moved_outside_the_app(ws, originals):
    ws.add_file(originals / "Ni_run.csv")
    sha = file_sha256(originals / "Ni_run.csv")
    (ws.root / "elsewhere").mkdir()
    os.rename(ws.root / "Ni_run.csv", ws.root / "elsewhere" / "Ni.csv")

    assert ws.find_by_hash(sha) == ["elsewhere/Ni.csv"]


def test_params_files_and_the_hidden_folder_are_not_data(ws, originals):
    ws.add_file(originals / "Fc_run.csv")
    (ws.root / "notes.txt").write_text("not data")

    assert ws.data_files() == ["Fc_run.csv"]
    provenance = json.loads((ws.root / META_DIR / "provenance.json").read_text())
    assert len(provenance) == 1

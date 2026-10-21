"""Saved analysis sessions (moles.analysis.sessions)."""

import os
import time

import pytest

from moles.analysis import sessions
from moles.analysis.datasets import load_dataset
from moles.analysis.model import (
    AnalysisSession, LabelledPoint, PeakSettings, PlotWindowState,
)
from moles.analysis.workspace import Workspace, WorkspaceError

CV_TEXT = ("Time (s),Potential (V),Current (uA),Cycle,Index,Target V,Range\n"
           + "".join(f"{i * 0.004:.3f},{-i * 0.001:.4f},{i % 7}.0,0,0,{-i * 0.001:.4f},3\n"
                     for i in range(50)))


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    workspace = Workspace(root)
    (root / "a.csv").write_text(CV_TEXT)
    (root / "b.csv").write_text(CV_TEXT.replace(",3\n", ",2\n"))
    return workspace


def _session(ws, *files):
    window = PlotWindowState("Plot 1", peak_settings=PeakSettings(smoothing_mV=20.0))
    for f in files:
        exp = window.add_experiment(load_dataset(ws.root / f), f)
        exp.add_label(LabelledPoint(10, "peak", "max", text_offset=(5.0, 20.0)))
    return AnalysisSession(windows=[window], active_window=window.uid)


def test_save_list_and_load(ws):
    original = _session(ws, "a.csv", "b.csv")

    path = sessions.save_session(ws, "Figure 3", original)

    assert path == ws.sessions_dir / "Figure 3.molesanalysis"
    assert [s.name for s in sessions.list_sessions(ws)] == ["Figure 3"]
    assert sessions.load_session(ws, "Figure 3").to_dict() == original.to_dict()
    assert sessions.last_session_name(ws) == "Figure 3"


def test_sessions_are_listed_newest_first(ws):
    sessions.save_session(ws, "older", _session(ws, "a.csv"))
    past = time.time() - 3600
    os.utime(sessions.session_path(ws, "older"), (past, past))
    sessions.save_session(ws, "newer", _session(ws, "a.csv"))

    assert [s.name for s in sessions.list_sessions(ws)] == ["newer", "older"]


def test_bad_names_and_unreadable_files_are_refused(ws):
    with pytest.raises(WorkspaceError):
        sessions.save_session(ws, "../escape", _session(ws, "a.csv"))
    sessions.session_path(ws, "broken").write_text("{not json")
    with pytest.raises(WorkspaceError):
        sessions.load_session(ws, "broken")
    with pytest.raises(WorkspaceError):
        sessions.load_session(ws, "absent")


def test_a_deleted_last_session_is_forgotten(ws):
    sessions.save_session(ws, "gone", _session(ws, "a.csv"))
    sessions.session_path(ws, "gone").unlink()

    assert sessions.last_session_name(ws) is None


def test_files_renamed_outside_the_app_are_found_again(ws):
    session = _session(ws, "a.csv", "b.csv")
    (ws.root / "sub").mkdir()
    os.rename(ws.root / "a.csv", ws.root / "sub" / "renamed.csv")

    report = sessions.relink(ws, session)

    assert report.relinked == [("a.csv", "sub/renamed.csv")] and report.missing == []
    assert session.windows[0].experiments[0].file == "sub/renamed.csv"


def test_files_gone_from_the_workspace_are_reported_and_kept(ws):
    session = _session(ws, "a.csv", "b.csv")
    (ws.root / "b.csv").unlink()

    report = sessions.relink(ws, session)

    assert report.missing == ["b.csv"]
    assert [e.file for e in session.windows[0].experiments] == ["a.csv", "b.csv"]


def test_a_different_file_under_the_same_name_is_not_trusted(ws):
    session = _session(ws, "a.csv")
    (ws.root / "a.csv").write_text(CV_TEXT.replace("0.004", "0.005"))

    assert sessions.relink(ws, session).missing == ["a.csv"]

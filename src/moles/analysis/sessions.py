"""Saved analysis sessions of a workspace.

A session holds every plot window (experiments, offsets, colours, ranges,
labels, peak settings, layout) as JSON in the workspace's hidden
``.moles_analysis/sessions`` folder, one ``<name>.molesanalysis`` file per
session. It never holds data: experiments refer to workspace files by path and
content fingerprint, so a file renamed or moved outside the app is found
again by its content, and one that has gone is reported rather than silently
dropped.

The workspace also remembers which session was open last, so it reopens with
the workspace.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from .model import AnalysisSession
from .workspace import WorkspaceError, check_name

SESSION_SUFFIX = ".molesanalysis"
_STATE_FILE = "state.json"


@dataclass
class SessionInfo:
    name: str
    path: Path
    modified: datetime


@dataclass
class RelinkReport:
    """What resolving a session's files found.

    ``relinked`` pairs an experiment's saved path with where its content was
    found instead; ``missing`` lists saved paths whose content is nowhere in
    the workspace.
    """

    relinked: List[Tuple[str, str]] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)


def session_path(workspace, name) -> Path:
    return workspace.sessions_dir / (check_name(name) + SESSION_SUFFIX)


def list_sessions(workspace) -> List[SessionInfo]:
    """Saved sessions, most recently saved first."""
    sessions = []
    for path in workspace.sessions_dir.glob("*" + SESSION_SUFFIX):
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        sessions.append(SessionInfo(path.name[:-len(SESSION_SUFFIX)], path, modified))
    return sorted(sessions, key=lambda s: s.modified, reverse=True)


def save_session(workspace, name, session: AnalysisSession) -> Path:
    """Write a session (replacing any of the same name) and remember it as the last one."""
    path = session_path(workspace, name)
    workspace.sessions_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(session.to_json(), encoding="utf-8")
    os.replace(tmp, path)
    remember_last_session(workspace, name)
    return path


def load_session(workspace, name) -> AnalysisSession:
    path = session_path(workspace, name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise WorkspaceError(f"Could not read session {name}: {e}") from e
    try:
        return AnalysisSession.from_json(text)
    except (ValueError, KeyError, TypeError) as e:
        raise WorkspaceError(f"Session {name} could not be read: {e}") from e


def _state_path(workspace):
    return workspace.meta / _STATE_FILE


def last_session_name(workspace) -> Optional[str]:
    """The session open when the workspace was last used, if it still exists."""
    try:
        name = json.loads(_state_path(workspace).read_text(encoding="utf-8")).get("last_session")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(name, str):
        return None
    try:
        return name if session_path(workspace, name).is_file() else None
    except WorkspaceError:
        return None


def remember_last_session(workspace, name: Optional[str]):
    _state_path(workspace).write_text(json.dumps({"last_session": name}), encoding="utf-8")


def resolve(workspace, experiment) -> Optional[str]:
    """Where an experiment's file is now: its saved path if that still holds the
    same content, else wherever that content is found, else None."""
    try:
        path = workspace.path(experiment.file)
        if path.is_file() and (not experiment.sha256
                               or workspace.file_hash(experiment.file) == experiment.sha256):
            return experiment.file
    except (WorkspaceError, OSError):
        pass
    if experiment.sha256:
        matches = workspace.find_by_hash(experiment.sha256)
        if matches:
            return matches[0]
    return None


def relink(workspace, session: AnalysisSession) -> RelinkReport:
    """Point every experiment of a session at its file's current workspace path."""
    report = RelinkReport()
    for window in session.windows:
        for exp in window.experiments:
            found = resolve(workspace, exp)
            if found is None:
                report.missing.append(exp.file)
            elif found != exp.file:
                report.relinked.append((exp.file, found))
                exp.file = found
    return report

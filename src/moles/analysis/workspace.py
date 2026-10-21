"""A Data Analysis workspace: a folder holding copies of data files.

Raw data must never be altered, so the app never works on a file where an
experiment saved it. Adding a file to the workspace copies it, together with
the params file describing its run, and every later operation (new folder,
rename, move, removal) acts only on the copies inside the workspace.

Two guards keep the copies apart from the originals:

* a workspace may not be, contain, or sit inside a folder MOLES saves
  experiment data into, and
* an ordinary folder only becomes a workspace while it is empty, so a folder
  of raw data cannot be adopted by mistake.

Where each copy came from (original path and modification time, when it was
copied) is recorded by content fingerprint (SHA-256) in the hidden
``.moles_analysis`` folder, which also holds saved analysis sessions. Keyed by
content, a record follows its copy however it is renamed or moved, in the app
or in Finder.

A data file in the workspace owns the params file of the same name
(``run.csv`` owns ``run.params.txt``, or ``run_params.csv`` for older runs);
renaming, moving, or removing the data file does the same to its params file.
"""

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from .datasets import params_path_for

META_DIR = ".moles_analysis"
SESSIONS_DIR = "sessions"
PROVENANCE_FILE = "provenance.json"

DATA_SUFFIXES = (".csv",)
PARAMS_SUFFIXES = (".params.txt", "_params.csv")

# Characters a file or folder name may not contain on any platform MOLES runs on.
_FORBIDDEN_NAME_CHARS = set('/\\:*?"<>|')

_HASH_CHUNK = 1 << 20


class WorkspaceError(Exception):
    """An operation the workspace refuses; the message is meant for the user."""


class DuplicateFileError(WorkspaceError):
    """The file's exact content is already in the workspace, at ``existing``."""

    def __init__(self, source, existing):
        self.source = Path(source)
        self.existing = list(existing)
        super().__init__(f"{self.source.name} is already in the workspace as "
                         f"{', '.join(self.existing)}.")


@dataclass
class AddResult:
    """One file copied in: its workspace path, its params file's, and its origin."""

    rel: str
    companion: Optional[str]
    original: Path


@dataclass
class Relocation:
    """Where a renamed or moved entry went, and the data files it carried.

    ``moved`` pairs the old and new workspace paths of every data file whose
    path changed (one for a file, all of those inside for a folder).
    """

    rel: str
    moved: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class FolderAddReport:
    """What adding a folder did.

    ``duplicates`` are files whose content was already in the workspace and
    were not copied, as (source, workspace folder it would go to, existing
    copies). ``failed`` pairs a source with the reason it was not copied.
    """

    added: List[AddResult] = field(default_factory=list)
    duplicates: List[Tuple[Path, str, List[str]]] = field(default_factory=list)
    failed: List[Tuple[Path, str]] = field(default_factory=list)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_companion(name: str) -> bool:
    """True for a params file, which travels with its data file."""
    return name.endswith(PARAMS_SUFFIXES)


def is_data_file(name: str) -> bool:
    return (not name.startswith(".") and Path(name).suffix.lower() in DATA_SUFFIXES
            and not is_companion(name))


def _within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def moles_data_folders() -> List[Path]:
    """The folders MOLES experiments save raw data into (from MOLES settings)."""
    from ..settings import get_cv_data_folder, get_data_folder
    return [get_data_folder(), get_cv_data_folder()]


def location_problem(folder, protected: Iterable = ()) -> Optional[str]:
    """Why ``folder`` cannot be a workspace, or None if it can."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        return f"{folder} is not a folder."
    folder = folder.resolve()
    for data_folder in protected:
        try:
            data_folder = Path(data_folder).expanduser().resolve()
        except OSError:
            continue
        if _within(folder, data_folder) or _within(data_folder, folder):
            return (f"{folder} overlaps {data_folder}, where MOLES saves raw "
                    "experiment data. Choose a workspace folder outside it, so "
                    "your original files are never touched.")
    if not (folder / META_DIR).is_dir():
        if any(not entry.name.startswith(".") for entry in folder.iterdir()):
            return (f"{folder} already holds files. A new workspace must start "
                    "as an empty folder, so files already in it are never "
                    "mistaken for workspace copies.")
    return None


def check_name(name: str) -> str:
    """Return ``name`` stripped of surrounding spaces, or raise if it is not a usable name."""
    name = name.strip()
    if not name or name in (".", ".."):
        raise WorkspaceError("A name cannot be empty.")
    if name.startswith("."):
        raise WorkspaceError("A name cannot start with a dot (that would hide it).")
    bad = sorted(set(name) & _FORBIDDEN_NAME_CHARS)
    if bad:
        raise WorkspaceError(f"A name cannot contain {' '.join(bad)}")
    return name


def iter_data_files(folder) -> Iterator[Path]:
    """Data files under ``folder``, recursively, skipping hidden files and folders."""
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if is_data_file(name):
                yield Path(dirpath) / name


def _copy_verified(source: Path, dest: Path, sha256: Optional[str] = None):
    """Copy ``source`` to ``dest`` (timestamps kept) and check the copy's content.

    The copy is written under a temporary name and only then renamed, so a
    half-written file never appears under the real name.
    """
    partial = dest.with_name(dest.name + ".partial")
    shutil.copy2(source, partial)
    if sha256 is not None and file_sha256(partial) != sha256:
        partial.unlink()
        raise WorkspaceError(f"The copy of {source.name} did not match the original; "
                             "nothing was added.")
    os.replace(partial, dest)


# ----------------------------------------------------------------------
# Workspace
# ----------------------------------------------------------------------

class Workspace:
    """An open workspace folder. Create one with :meth:`open`."""

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.meta = self.root / META_DIR
        self.meta.mkdir(exist_ok=True)
        self.sessions_dir.mkdir(exist_ok=True)
        self._hashes: Dict[str, Tuple[int, int, str]] = {}

    @classmethod
    def open(cls, folder, protected: Optional[Iterable] = None):
        """Open (or start) a workspace, refusing folders that could hold raw data.

        ``protected`` defaults to the folders MOLES saves experiment data into.
        """
        if protected is None:
            protected = moles_data_folders()
        problem = location_problem(folder, protected)
        if problem:
            raise WorkspaceError(problem)
        return cls(folder)

    @property
    def name(self):
        return self.root.name

    @property
    def sessions_dir(self):
        return self.meta / SESSIONS_DIR

    # --- Paths ---

    def path(self, rel="") -> Path:
        """Absolute path of a workspace path, refusing anything outside the workspace."""
        p = (self.root / PurePosixPath(rel)).resolve() if rel else self.root
        if not _within(p, self.root) or _within(p, self.meta):
            raise WorkspaceError(f"{rel} is not inside the workspace.")
        return p

    def rel(self, path) -> str:
        """Workspace path (forward slashes, '' for the root) of an absolute path."""
        p = Path(path).resolve()
        if not _within(p, self.root) or _within(p, self.meta):
            raise WorkspaceError(f"{path} is not inside the workspace.")
        return "" if p == self.root else p.relative_to(self.root).as_posix()

    def contains(self, path) -> bool:
        try:
            p = Path(path).resolve()
        except OSError:
            return False
        return _within(p, self.root)

    # --- Contents ---

    def data_files(self, rel="") -> List[str]:
        """Workspace paths of every data file at or under ``rel``."""
        p = self.path(rel)
        if p.is_file():
            return [self.rel(p)] if is_data_file(p.name) else []
        return [self.rel(f) for f in iter_data_files(p)]

    def companions_of(self, rel) -> List[str]:
        """Workspace paths of the params files a data file owns."""
        p = self.path(rel)
        found = []
        for suffix in PARAMS_SUFFIXES:
            companion = p.with_name(p.stem + suffix)
            if companion.is_file():
                found.append(self.rel(companion))
        return found

    def file_hash(self, rel) -> str:
        """Content fingerprint of a workspace file, remembered until the file changes."""
        p = self.path(rel)
        st = p.stat()
        cached = self._hashes.get(rel)
        if cached and cached[:2] == (st.st_size, st.st_mtime_ns):
            return cached[2]
        digest = file_sha256(p)
        self._hashes[rel] = (st.st_size, st.st_mtime_ns, digest)
        return digest

    def find_by_hash(self, sha256) -> List[str]:
        """Every data file in the workspace with this exact content."""
        return [rel for rel in self.data_files() if self.file_hash(rel) == sha256]

    # --- Adding ---

    def add_file(self, source, folder_rel="", allow_duplicate=False) -> AddResult:
        """Copy a data file (and its params file) into a workspace folder.

        The original is only read. A name already taken in the folder gets a
        ' (2)'-style suffix; content already in the workspace raises
        ``DuplicateFileError`` unless ``allow_duplicate``.
        """
        source = Path(source).expanduser()
        if not source.is_file():
            raise WorkspaceError(f"{source} is not a file.")
        source = source.resolve()
        if not is_data_file(source.name):
            raise WorkspaceError(f"{source.name} is not a .csv data file.")
        if self.contains(source):
            raise WorkspaceError(f"{source.name} is already inside the workspace.")
        folder = self.path(folder_rel)
        if not folder.is_dir():
            raise WorkspaceError(f"{folder_rel} is not a folder.")

        sha256 = file_sha256(source)
        if not allow_duplicate:
            existing = self.find_by_hash(sha256)
            if existing:
                raise DuplicateFileError(source, existing)

        dest = self._free_name(folder, source.name)
        _copy_verified(source, dest, sha256)
        self._hashes[self.rel(dest)] = (dest.stat().st_size, dest.stat().st_mtime_ns, sha256)

        companion = None
        params = params_path_for(source)
        if params is not None:
            suffix = "_params.csv" if params.name.endswith("_params.csv") else ".params.txt"
            dest_params = dest.with_name(dest.stem + suffix)
            _copy_verified(params, dest_params)
            companion = self.rel(dest_params)

        self._record_origin(sha256, source)
        return AddResult(self.rel(dest), companion, source)

    def add_folder(self, source, folder_rel="", copy_duplicates=False) -> FolderAddReport:
        """Copy every data file under ``source`` into a new folder of the same name.

        Sub-folders are kept; folders holding no data files are not created.
        """
        source = Path(source).expanduser().resolve()
        if not source.is_dir():
            raise WorkspaceError(f"{source} is not a folder.")
        if self.contains(source) or _within(self.root, source):
            raise WorkspaceError(f"{source.name} overlaps the workspace itself.")
        parent = self.path(folder_rel)
        top = self._free_name(parent, source.name)
        report = FolderAddReport()
        for file in iter_data_files(source):
            target = top / file.parent.relative_to(source)
            target.mkdir(parents=True, exist_ok=True)
            target_rel = self.rel(target)
            try:
                report.added.append(self.add_file(file, target_rel, copy_duplicates))
            except DuplicateFileError as e:
                report.duplicates.append((file, target_rel, e.existing))
            except (WorkspaceError, OSError) as e:
                report.failed.append((file, str(e)))
        self._remove_empty_folders(top)
        return report

    def _remove_empty_folders(self, top: Path):
        """Remove the folders a folder add created but put nothing into."""
        if not top.exists():
            return
        for dirpath, _dirnames, _filenames in sorted(os.walk(top), key=lambda w: -len(w[0])):
            try:
                os.rmdir(dirpath)  # only succeeds on an empty folder
            except OSError:
                pass

    def _free_name(self, folder: Path, name: str) -> Path:
        """``folder/name``, or ``name (2)``, ``name (3)``... when that or its params file is taken."""
        stem, suffix = Path(name).stem, Path(name).suffix

        def taken(path):
            if path.exists():
                return True
            return any(path.with_name(path.stem + s).exists() for s in PARAMS_SUFFIXES)

        candidate = folder / name
        n = 2
        while taken(candidate):
            candidate = folder / f"{stem} ({n}){suffix}"
            n += 1
        return candidate

    # --- Provenance ---

    def _provenance_path(self):
        return self.meta / PROVENANCE_FILE

    def _load_provenance(self) -> Dict[str, list]:
        try:
            data = json.loads(self._provenance_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _record_origin(self, sha256, source: Path):
        records = self._load_provenance()
        entries = records.setdefault(sha256, [])
        if not any(e.get("original_path") == str(source) for e in entries):
            entries.append({
                "original_path": str(source),
                "original_modified": datetime.fromtimestamp(
                    source.stat().st_mtime).isoformat(timespec="seconds"),
                "copied_at": datetime.now().isoformat(timespec="seconds"),
            })
        tmp = self._provenance_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
        os.replace(tmp, self._provenance_path())

    def origins(self, rel) -> List[dict]:
        """Where a workspace file was copied from (one entry per original with its content)."""
        return list(self._load_provenance().get(self.file_hash(rel), []))

    # --- Organising ---

    def make_folder(self, parent_rel, name) -> str:
        name = check_name(name)
        parent = self.path(parent_rel)
        if not parent.is_dir():
            raise WorkspaceError(f"{parent_rel} is not a folder.")
        target = parent / name
        if target.exists():
            raise WorkspaceError(f"{name} already exists here.")
        target.mkdir()
        return self.rel(target)

    def rename(self, rel, new_name) -> Relocation:
        """Rename a file or folder (a data file keeps its .csv ending)."""
        src = self._existing(rel)
        new_name = check_name(new_name)
        if src.is_file() and is_data_file(src.name):
            if Path(new_name).suffix.lower() not in DATA_SUFFIXES:
                new_name += src.suffix
            if is_companion(new_name):
                raise WorkspaceError(f"{new_name} is the name of a params file.")
        return self._move_entry(src, src.with_name(new_name))

    def move(self, rel, folder_rel) -> Relocation:
        """Move a file or folder into another workspace folder."""
        src = self._existing(rel)
        folder = self.path(folder_rel)
        if not folder.is_dir():
            raise WorkspaceError(f"{folder_rel} is not a folder.")
        if src.is_dir() and _within(folder, src):
            raise WorkspaceError(f"{src.name} cannot be moved into itself.")
        return self._move_entry(src, folder / src.name)

    def remove(self, rel, move_to_trash: Callable[[Path], bool]) -> List[str]:
        """Send a file (with its params file) or a folder to the system Trash.

        ``move_to_trash`` does the actual move and returns whether it worked.
        Returns the workspace paths of the data files removed.
        """
        src = self._existing(rel)
        removed = self.data_files(rel)
        targets = [src]
        if src.is_file():
            targets += [self.path(c) for c in self.companions_of(rel)]
        for target in targets:
            if not move_to_trash(target):
                raise WorkspaceError(f"Could not move {target.name} to the Trash.")
        for r in removed:
            self._hashes.pop(r, None)
        return removed

    def _existing(self, rel) -> Path:
        if not rel:
            raise WorkspaceError("The workspace folder itself cannot be changed here.")
        p = self.path(rel)
        if not p.exists():
            raise WorkspaceError(f"{rel} no longer exists.")
        return p

    def _move_entry(self, src: Path, dest: Path) -> Relocation:
        if dest == src:
            return Relocation(self.rel(src))
        # A case-only rename finds 'dest' existing on a case-insensitive disk:
        # it is the same file, so the rename may go ahead.
        if dest.exists() and not (src.name.lower() == dest.name.lower()
                                  and os.path.samefile(src, dest)):
            raise WorkspaceError(f"{dest.name} already exists there.")

        if src.is_dir():
            before = self.data_files(self.rel(src))
            old_prefix, new_prefix = self.rel(src), self.rel(dest.parent / dest.name)
            os.rename(src, dest)
            moved = [(r, new_prefix + r[len(old_prefix):]) for r in before]
        else:
            companions = []
            for suffix in PARAMS_SUFFIXES:
                c = src.with_name(src.stem + suffix)
                if c.is_file():
                    target = dest.with_name(dest.stem + suffix)
                    if target.exists() and not os.path.samefile(c, target):
                        raise WorkspaceError(f"{target.name} already exists there.")
                    companions.append((c, target))
            old_rel = self.rel(src)
            os.rename(src, dest)
            for c, target in companions:
                os.rename(c, target)
            moved = [(old_rel, self.rel(dest))] if is_data_file(src.name) else []

        for old, new in moved:
            if old in self._hashes:
                self._hashes[new] = self._hashes.pop(old)
        return Relocation(self.rel(dest), moved)

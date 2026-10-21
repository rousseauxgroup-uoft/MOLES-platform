"""Recognise and load saved MOLES data files for analysis.

Files are grouped into *families* by what they plot against: voltammograms
(current against potential: CV, DPV, the raw DPV staircase) are the only family
the Data Analysis app can plot so far. Electrolysis runs and OCP traces are
recognised so they can be named in a message, then refused. A new family is
added by teaching ``identify`` to recognise it and giving it a loader beside
``_load_voltammogram``.

Loaded data is read-only: several plot windows can share one loaded file, and
nothing they do may alter it. Currents are converted to microamps; every
original column is also kept, under its original header, for export.
"""

import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

FAMILY_VOLTAMMETRY = "voltammetry"
FAMILY_ELECTROLYSIS = "electrolysis"
FAMILY_OCP = "ocp"

TECH_CV = "CV"
TECH_DPV = "DPV"
TECH_DPV_RAW = "DPV raw"
TECH_VOLTAMMOGRAM = "Voltammogram"  # a voltammogram whose technique the file does not say

# Families the Data Analysis app can plot.
SUPPORTED_FAMILIES = (FAMILY_VOLTAMMETRY,)

# Electrolysis methods, as written in the '# method:' line of their files.
_ELECTROLYSIS_METHODS = {
    "constant_current", "constant_potential",
    "alternating_current", "alternating_polarity",
}

# How far the potential must turn back before it counts as a new sweep branch
# rather than noise. Target potentials are exact, so a small margin is enough;
# measured potentials carry noise of up to a few mV.
_REVERSAL_TARGET_V = 0.005
_REVERSAL_MEASURED_V = 0.02

# The raw DPV staircase pulses back and forth every step, so a turn only
# counts as a sweep reversal once it exceeds this multiple of the pulse height.
_DPV_RAW_PULSE_MARGIN = 2.0
_DEFAULT_DPV_PULSE_V = 0.05

# Lines read when identifying a file without loading it.
_SNIFF_LINES = 20


class UnsupportedDataError(ValueError):
    """The file is not one the Data Analysis app can plot (yet)."""


@dataclass(frozen=True)
class FileKind:
    """What a file is, as far as its first lines and params file can tell.

    ``supported`` is False for data the app recognises but cannot plot yet
    (and for files it does not recognise at all); ``reason`` then says why.
    """

    family: Optional[str]
    technique: Optional[str]
    supported: bool
    reason: str = ""


@dataclass(frozen=True, eq=False)
class Voltammogram:
    """One loaded voltammetry file.

    ``potential`` (V) and ``current`` (uA) are what gets plotted, in recorded
    order. ``branch`` numbers the single-direction sweep branches point by
    point (a CV's vertices start new branches) and ``direction`` gives each
    point's sweep direction: +1 while the potential is rising, -1 while it is
    falling, 0 if it never moves. ``columns`` holds every column of the file
    under its original header and in its original units.
    """

    path: Path
    sha256: str
    technique: str
    potential: np.ndarray
    current: np.ndarray
    potential_label: str
    current_label: str
    branch: np.ndarray
    direction: np.ndarray
    columns: Mapping[str, np.ndarray] = field(default_factory=dict)
    params: Mapping[str, str] = field(default_factory=dict)

    family = FAMILY_VOLTAMMETRY

    def __len__(self):
        return len(self.potential)

    @property
    def name(self):
        return self.path.stem

    def branch_bounds(self, index):
        """First and last index (inclusive) of the sweep branch holding ``index``."""
        b = self.branch[index]
        first = int(np.searchsorted(self.branch, b, side="left"))
        last = int(np.searchsorted(self.branch, b, side="right")) - 1
        return first, last


# ----------------------------------------------------------------------
# Companion params files
# ----------------------------------------------------------------------

def params_path_for(data_path):
    """Return the params file belonging to a data file, or None if it has none.

    MOLES writes ``<run>.params.txt`` beside ``<run>.csv``; the raw DPV
    staircase ``<run>_RAW.csv`` shares its run's file. Older exports used
    ``<run>_params.csv``.
    """
    data_path = Path(data_path)
    stem = data_path.stem
    stems = [stem]
    if stem.endswith("_RAW"):
        stems.append(stem[:-len("_RAW")])
    for s in stems:
        for candidate in (data_path.with_name(s + ".params.txt"),
                          data_path.with_name(s + "_params.csv")):
            if candidate.is_file():
                return candidate
    return None


def read_params(path):
    """Read a params file as {key: value} strings ('key: value' or 'key,value' lines)."""
    params = {}
    if path is None:
        return params
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                for sep in (":", ","):
                    if sep in line:
                        key, value = line.split(sep, 1)
                        params[key.strip()] = value.strip()
                        break
    except OSError:
        pass
    return params


# ----------------------------------------------------------------------
# Identifying files
# ----------------------------------------------------------------------

def _is_header(line):
    """True for a row of column names; numbers such as '8.3e-09' do not count."""
    for field_text in line.split(","):
        try:
            float(field_text)
        except ValueError:
            return True
    return False


def _split_header(raw_text):
    """Return ('#' metadata dict, header names or None, first data line) from a file's opening lines."""
    meta = {}
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            body = stripped[1:].strip()
            if ":" in body:
                key, value = body.split(":", 1)
                meta[key.strip().lower()] = value.strip()
            continue
        if _is_header(stripped):
            return meta, [n.strip() for n in stripped.split(",")], None
        return meta, None, stripped
    return meta, None, None


def _find_column(names, *keywords):
    """Index of the first column whose name contains any keyword, else None."""
    for i, name in enumerate(names):
        lowered = name.lower()
        if any(kw in lowered for kw in keywords):
            return i
    return None


def _classify(names, meta, params, path):
    """Work out a file's family and technique from its headers and metadata."""
    method = (meta.get("method") or "").lower()
    params_method = (params.get("method") or "").upper()
    if method in _ELECTROLYSIS_METHODS:
        return FileKind(FAMILY_ELECTROLYSIS, method, False,
                        "Electrolysis runs can be kept in the workspace but not plotted yet.")

    if names is None:
        # Only voltammograms were ever exported without a header.
        return FileKind(FAMILY_VOLTAMMETRY, TECH_VOLTAMMOGRAM, True)

    if _find_column(names, "charge") is not None:
        return FileKind(FAMILY_ELECTROLYSIS, "electrolysis", False,
                        "Electrolysis runs can be kept in the workspace but not plotted yet.")

    v_col = _find_column(names, "potential", "voltage")
    i_col = _find_column(names, "delta-i", "delta", "current")
    t_col = _find_column(names, "time")
    if (i_col is None and v_col is not None and t_col is not None) or params_method == "OCP":
        return FileKind(FAMILY_OCP, "OCP", False,
                        "OCP traces can be kept in the workspace but not plotted yet.")
    if v_col is None or i_col is None:
        return FileKind(None, None, False, "No potential and current columns found.")

    if "delta" in names[i_col].lower():
        technique = TECH_DPV
    elif path.stem.endswith("_RAW") or (params_method == "DPV"
                                        and _find_column(names, "cycle") is None):
        technique = TECH_DPV_RAW
    elif params_method == "CV" or _find_column(names, "cycle") is not None:
        technique = TECH_CV
    else:
        technique = TECH_VOLTAMMOGRAM
    return FileKind(FAMILY_VOLTAMMETRY, technique, True)


def identify(path):
    """Say what a data file is from its first lines, without loading it all."""
    path = Path(path)
    try:
        with open(path, "r", errors="replace") as f:
            head = "".join(f.readline() for _ in range(_SNIFF_LINES))
    except OSError as e:
        return FileKind(None, None, False, f"Could not read the file: {e}")
    meta, names, first_data = _split_header(head)
    if names is None and first_data is None:
        return FileKind(None, None, False, "The file holds no data.")
    return _classify(names, meta, read_params(params_path_for(path)), path)


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------

def _current_scale_to_uA(column_name):
    """Factor converting a current column to microamps, from the unit in its header."""
    name = column_name.lower().replace(" ", "")
    if "(a)" in name:
        return 1e6
    if "(ma)" in name:
        return 1e3
    if "(na)" in name:
        return 1e-3
    return 1.0  # microamps, or no unit given


def _readonly(array):
    array = np.ascontiguousarray(array, dtype=float)
    array.setflags(write=False)
    return array


def load_dataset(path):
    """Load a data file the app can plot.

    Raises ``UnsupportedDataError`` for files that are recognised but not
    plottable yet (electrolysis, OCP) or not recognised at all.
    """
    path = Path(path)
    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace")
    meta, names, first_data = _split_header("\n".join(text.splitlines()[:_SNIFF_LINES]))
    if names is None and first_data is None:
        raise UnsupportedDataError(f"{path.name} holds no data.")

    params = read_params(params_path_for(path))
    kind = _classify(names, meta, params, path)
    if not kind.supported:
        raise UnsupportedDataError(f"{path.name}: {kind.reason}")

    frame = pd.read_csv(io.StringIO(text), comment="#",
                        header=0 if names is not None else None,
                        skipinitialspace=True)
    data = frame.apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    if data.ndim != 2 or data.shape[0] == 0:
        raise UnsupportedDataError(f"{path.name} holds no data.")
    data = data[~np.isnan(data).all(axis=1)]
    return _load_voltammogram(path, sha256, kind.technique, names, data, params)


def _load_voltammogram(path, sha256, technique, names, data, params):
    if names is None:
        if data.shape[1] < 2:
            raise UnsupportedDataError(f"{path.name}: expected at least 2 columns.")
        col0, col1 = data[:, 0], data[:, 1]
        # A column of currents in amps is tiny, which marks the legacy
        # 'current (A), potential (V)' export; otherwise potential comes first.
        if np.nanmax(np.abs(col0)) < 0.01:
            names = ["Current (A)", "Potential (V)"]
        else:
            names = ["Potential (V)", "Current (uA)"]
        data = data[:, :2]

    v_col = _find_column(names, "potential", "voltage")
    i_col = _find_column(names, "delta-i", "delta", "current")
    potential = data[:, v_col]
    current = data[:, i_col] * _current_scale_to_uA(names[i_col])
    keep = ~(np.isnan(potential) | np.isnan(current))
    data, potential, current = data[keep], potential[keep], current[keep]
    if len(potential) == 0:
        raise UnsupportedDataError(f"{path.name} holds no numeric data.")

    current_label = "Δi (µA)" if "delta" in names[i_col].lower() else "Current (µA)"

    # Sweep direction comes from the target potential where the file has one:
    # it is exact, while the measured potential carries noise.
    t_col = _find_column(names, "target")
    if t_col is not None and technique == TECH_CV:
        reference, threshold = data[:, t_col], _REVERSAL_TARGET_V
    else:
        reference, threshold = potential, _REVERSAL_MEASURED_V
    if technique == TECH_DPV_RAW:
        try:
            pulse = abs(float(params.get("pulse_V", _DEFAULT_DPV_PULSE_V)))
        except ValueError:
            pulse = _DEFAULT_DPV_PULSE_V
        threshold = max(threshold, _DPV_RAW_PULSE_MARGIN * pulse)
    branch, direction = sweep_branches(reference, threshold)

    columns = {name: _readonly(data[:, i]) for i, name in enumerate(names)
               if i < data.shape[1]}
    return Voltammogram(
        path=path, sha256=sha256, technique=technique,
        potential=_readonly(potential), current=_readonly(current),
        potential_label=names[v_col], current_label=current_label,
        branch=branch, direction=direction,
        columns=columns, params=dict(params),
    )


# ----------------------------------------------------------------------
# Sweep branches
# ----------------------------------------------------------------------

def sweep_branches(potential, threshold):
    """Split a potential trace into single-direction sweep branches.

    A vertex is the extreme point reached before the potential turns back by
    more than ``threshold``; it closes the branch it ends. Returns
    ``(branch, direction)`` arrays, one entry per point: the branch number
    (0, 1, ...) and its direction (+1 rising, -1 falling, 0 if the potential
    never moves by ``threshold`` at all).
    """
    e = np.asarray(potential, dtype=float)
    n = len(e)
    vertices: List[int] = []
    first_direction = 0
    d = 0
    hi = lo = e[0] if n else 0.0
    hi_i = lo_i = 0
    ext = ext_i = 0
    for i in range(n):
        v = e[i]
        if d == 0:
            if v > hi:
                hi, hi_i = v, i
            if v < lo:
                lo, lo_i = v, i
            if hi - lo >= threshold:
                d = first_direction = 1 if hi_i > lo_i else -1
                ext, ext_i = (hi, hi_i) if d > 0 else (lo, lo_i)
        elif d > 0:
            if v > ext:
                ext, ext_i = v, i
            elif ext - v >= threshold:
                vertices.append(ext_i)
                d, ext, ext_i = -1, v, i
        else:
            if v < ext:
                ext, ext_i = v, i
            elif v - ext >= threshold:
                vertices.append(ext_i)
                d, ext, ext_i = 1, v, i

    # A point's branch number is how many vertices come before it.
    branch = np.searchsorted(np.asarray(vertices, dtype=int), np.arange(n), side="left")
    if first_direction == 0:
        direction = np.zeros(n, dtype=np.int8)
    else:
        signs = np.where(branch % 2 == 0, first_direction, -first_direction)
        direction = signs.astype(np.int8)
    branch.setflags(write=False)
    direction.setflags(write=False)
    return branch, direction


def branch_spans(branch) -> List[Tuple[int, int]]:
    """(first, last) index, inclusive, of every sweep branch in order."""
    branch = np.asarray(branch)
    if len(branch) == 0:
        return []
    starts = np.flatnonzero(np.diff(branch)) + 1
    firsts = np.concatenate(([0], starts))
    lasts = np.concatenate((starts - 1, [len(branch) - 1]))
    return list(zip(firsts.tolist(), lasts.tolist()))


def describe(dataset) -> Dict[str, str]:
    """A short summary of a loaded dataset, for tooltips and export headers."""
    info = {
        "Technique": dataset.technique,
        "Points": str(len(dataset)),
        "Potential range (V)": f"{np.min(dataset.potential):.3f} to {np.max(dataset.potential):.3f}",
        "Sweep branches": str(int(dataset.branch[-1]) + 1 if len(dataset) else 0),
    }
    for key in ("mV_s", "step_V", "pulse_V", "cycles", "ir_enabled", "current_zero_uA"):
        if key in dataset.params:
            info[key] = dataset.params[key]
    return info

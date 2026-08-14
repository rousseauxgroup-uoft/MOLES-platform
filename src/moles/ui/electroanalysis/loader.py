"""Load voltammetry CSV files into a common (potential, current) form.

The Analysis tab accepts files from several sources, so this module hides their
format differences behind one function, ``load_voltammogram``. It understands:

* MOLES CV files     — 6 columns with a ``Time (s),Potential (V),...`` header.
* MOLES 2-column     — a ``Potential (V),Current (uA)`` or ``...,Delta-i (uA)``
                       header (raw DPV staircase and processed DPV curves).
* Legacy 2-column    — no header, ordered ``current (A), potential (V)``.
* Generic 2-column   — no header, ordered ``potential (V), current``.

Everything is returned with current already expressed in microamps so the plot
and offset maths stay in one consistent unit.
"""

from pathlib import Path

import numpy as np


def _has_alpha(line):
    """True if the line contains letters — i.e. it looks like a header row."""
    return any(ch.isalpha() for ch in line)


def _find_column(names, *keywords):
    """Return the index of the first column whose name contains any keyword."""
    for i, name in enumerate(names):
        lowered = name.lower()
        if any(kw in lowered for kw in keywords):
            return i
    return None


def _current_to_uA(values, column_name):
    """Scale a current column to microamps based on the unit in its header."""
    name = column_name.lower()
    if '(a)' in name:
        return values * 1e6
    if '(ma)' in name:
        return values * 1e3
    return values  # already microamps (or unspecified — assume uA)


def load_voltammogram(path):
    """Load a CSV and return ``(potential_V, current_uA, current_label)``.

    Raises ``ValueError`` if the file cannot be interpreted as voltammetry data.
    """
    path = Path(path)
    with open(path, 'r') as f:
        first_line = f.readline().strip()

    if _has_alpha(first_line):
        return _load_with_header(path, first_line)
    return _load_headerless(path)


def _load_with_header(path, header_line):
    """Parse a file whose first row names its columns."""
    names = [n.strip() for n in header_line.split(',')]
    data = np.genfromtxt(path, delimiter=',', skip_header=1)
    if data.ndim == 1:  # a single data row collapses to 1-D; restore 2-D
        data = data.reshape(1, -1)

    v_col = _find_column(names, 'potential', 'voltage')
    i_col = _find_column(names, 'delta-i', 'delta', 'current')
    if v_col is None or i_col is None:
        raise ValueError(f"Could not find potential/current columns in {path.name}")

    potential = data[:, v_col]
    current = _current_to_uA(data[:, i_col], names[i_col])
    label = 'Delta-i (uA)' if 'delta' in names[i_col].lower() else 'Current (uA)'
    return potential, current, label


def _load_headerless(path):
    """Parse a 2-column file with no header, guessing the column order.

    A column of currents in amps is tiny (|values| well below 1), so if the
    first column looks like that we treat the file as the legacy
    ``current (A), potential (V)`` layout; otherwise we assume the more common
    ``potential (V), current`` order.
    """
    data = np.genfromtxt(path, delimiter=',')
    if data.ndim == 1:
        data = data.reshape(1, -1)
    if data.shape[1] < 2:
        raise ValueError(f"Expected at least 2 columns in {path.name}")

    col0, col1 = data[:, 0], data[:, 1]
    if np.nanmax(np.abs(col0)) < 0.01:
        # Legacy MOLES export: current in amps first, potential second.
        return col1, col0 * 1e6, 'Current (uA)'
    return col0, col1, 'Current (uA)'

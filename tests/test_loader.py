"""Unit tests for the Analysis-tab voltammogram loader."""

import numpy as np
import pytest

from moles.ui.electroanalysis.loader import load_voltammogram


def _write(path, array, header=None, fmt='%.6f'):
    kwargs = {'delimiter': ',', 'fmt': fmt}
    if header is not None:
        kwargs.update(header=header, comments='')
    np.savetxt(path, array, **kwargs)


def test_loads_moles_six_column_cv(tmp_path):
    f = tmp_path / "cv.csv"
    n = 5
    arr = np.column_stack([
        np.arange(n), np.linspace(-0.5, 0.5, n), np.linspace(1, 5, n),
        np.zeros(n), np.zeros(n), np.linspace(-0.5, 0.5, n),
    ])
    _write(f, arr, header="Time (s),Potential (V),Current (uA),Cycle,Index,Target V")

    v, i, label = load_voltammogram(f)
    assert np.allclose(v, np.linspace(-0.5, 0.5, n))
    assert np.allclose(i, np.linspace(1, 5, n))
    assert label == 'Current (uA)'


def test_loads_two_column_differential(tmp_path):
    f = tmp_path / "dpv.csv"
    n = 5
    arr = np.column_stack([np.linspace(0, 1, n), np.linspace(0.1, 2, n)])
    _write(f, arr, header="Potential (V),Delta-i (uA)")

    v, i, label = load_voltammogram(f)
    assert np.allclose(v, np.linspace(0, 1, n))
    assert label == 'Delta-i (uA)'


def test_loads_legacy_headerless_amps(tmp_path):
    # Old MOLES order: current (A) first, potential (V) second. Tiny currents
    # mark the file as the legacy layout; they are scaled to microamps.
    f = tmp_path / "legacy.csv"
    n = 5
    arr = np.column_stack([np.linspace(1e-6, 5e-6, n), np.linspace(-0.3, 0.3, n)])
    _write(f, arr, fmt='%.8f')

    v, i, label = load_voltammogram(f)
    assert np.allclose(v, np.linspace(-0.3, 0.3, n))
    assert np.allclose(i, np.linspace(1.0, 5.0, n))  # 1e-6 A -> 1 uA


def test_loads_generic_headerless_potential_current(tmp_path):
    f = tmp_path / "generic.csv"
    n = 5
    arr = np.column_stack([np.linspace(-0.4, 0.4, n), np.linspace(10, 50, n)])
    _write(f, arr)

    v, i, label = load_voltammogram(f)
    assert np.allclose(v, np.linspace(-0.4, 0.4, n))
    assert np.allclose(i, np.linspace(10, 50, n))


def test_rejects_unparseable_header(tmp_path):
    f = tmp_path / "bad.csv"
    _write(f, np.column_stack([np.arange(3), np.arange(3)]),
           header="Alpha,Beta")
    with pytest.raises(ValueError):
        load_voltammogram(f)

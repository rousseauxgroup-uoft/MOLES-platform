"""Unit tests for the Analysis-tab trace loader."""

import numpy as np
import pytest

from moles.ui.electroanalysis.loader import (
    KIND_OCP, KIND_VOLTAMMOGRAM, load_trace,
)


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

    trace = load_trace(f)
    assert np.allclose(trace.x, np.linspace(-0.5, 0.5, n))
    assert np.allclose(trace.y, np.linspace(1, 5, n))
    assert trace.y_label == 'Current (uA)'
    # A CV file has a time column too, but its current column makes it a
    # voltammogram, not a time series.
    assert trace.kind == KIND_VOLTAMMOGRAM
    assert trace.x_label == 'Potential (V)'


def test_loads_two_column_differential(tmp_path):
    f = tmp_path / "dpv.csv"
    n = 5
    arr = np.column_stack([np.linspace(0, 1, n), np.linspace(0.1, 2, n)])
    _write(f, arr, header="Potential (V),Delta-i (uA)")

    trace = load_trace(f)
    assert np.allclose(trace.x, np.linspace(0, 1, n))
    assert trace.y_label == 'Delta-i (uA)'
    assert trace.kind == KIND_VOLTAMMOGRAM


def test_loads_ocp_time_series(tmp_path):
    f = tmp_path / "ocp.csv"
    n = 5
    times = np.linspace(0, 2, n)
    potentials = np.linspace(0.30, 0.24, n)
    _write(f, np.column_stack([times, potentials]),
           header="Time (s),Potential (V)")

    trace = load_trace(f)
    assert trace.kind == KIND_OCP
    assert np.allclose(trace.x, times)
    assert np.allclose(trace.y, potentials)
    assert trace.x_label == 'Time (s)'
    assert trace.y_label == 'Potential (V)'


def test_loads_legacy_headerless_amps(tmp_path):
    # Old MOLES order: current (A) first, potential (V) second. Tiny currents
    # mark the file as the legacy layout; they are scaled to microamps.
    f = tmp_path / "legacy.csv"
    n = 5
    arr = np.column_stack([np.linspace(1e-6, 5e-6, n), np.linspace(-0.3, 0.3, n)])
    _write(f, arr, fmt='%.8f')

    trace = load_trace(f)
    assert np.allclose(trace.x, np.linspace(-0.3, 0.3, n))
    assert np.allclose(trace.y, np.linspace(1.0, 5.0, n))  # 1e-6 A -> 1 uA
    assert trace.kind == KIND_VOLTAMMOGRAM


def test_loads_generic_headerless_potential_current(tmp_path):
    f = tmp_path / "generic.csv"
    n = 5
    arr = np.column_stack([np.linspace(-0.4, 0.4, n), np.linspace(10, 50, n)])
    _write(f, arr)

    trace = load_trace(f)
    assert np.allclose(trace.x, np.linspace(-0.4, 0.4, n))
    assert np.allclose(trace.y, np.linspace(10, 50, n))
    assert trace.kind == KIND_VOLTAMMOGRAM


def test_rejects_unparseable_header(tmp_path):
    f = tmp_path / "bad.csv"
    _write(f, np.column_stack([np.arange(3), np.arange(3)]),
           header="Alpha,Beta")
    with pytest.raises(ValueError):
        load_trace(f)


def test_rejects_a_current_only_time_series(tmp_path):
    # No potential column at all: not a voltammogram and not an OCP trace.
    f = tmp_path / "current_vs_time.csv"
    _write(f, np.column_stack([np.arange(3), np.arange(3)]),
           header="Time (s),Current (uA)")
    with pytest.raises(ValueError):
        load_trace(f)

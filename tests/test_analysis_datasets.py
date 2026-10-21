"""Loading data files for the Data Analysis app (moles.analysis.datasets)."""

import hashlib
import os

import numpy as np
import pytest

from moles.analysis.datasets import (
    FAMILY_ELECTROLYSIS, FAMILY_OCP, FAMILY_VOLTAMMETRY, TECH_CV, TECH_DPV,
    TECH_DPV_RAW, TECH_VOLTAMMOGRAM, UnsupportedDataError, branch_spans,
    identify, load_dataset, params_path_for, sweep_branches,
)

CV_HEADER = "Time (s),Potential (V),Current (uA),Cycle,Index,Target V,Range"


def _triangle(start, low, high, cycles, step=0.001):
    """Target potentials of a CV: start -> low -> high -> start, ``cycles`` times."""
    one = np.concatenate([np.arange(start, low, -step), np.arange(low, high, step),
                          np.arange(high, start, -step)])
    return np.tile(one, cycles)


def _write_cv(path, cycles=2, params=True):
    target = _triangle(0.0, -1.0, 0.5, cycles)
    n = len(target)
    current = 50 * np.sin(np.linspace(0, 6, n))
    rows = np.column_stack([np.arange(n) * 0.004, target + 0.0005, current,
                            np.zeros(n), np.zeros(n), target, np.full(n, 3)])
    np.savetxt(path, rows, delimiter=",", header=CV_HEADER, comments="", fmt="%.5f")
    if params:
        path.with_suffix(".params.txt").write_text("method: CV\nmV_s: 100.0\ncycles: 2\n")
    return target, current


def test_cv_file_loads_with_all_columns(tmp_path):
    path = tmp_path / "run.csv"
    target, current = _write_cv(path)

    ds = load_dataset(path)

    assert ds.technique == TECH_CV and ds.family == FAMILY_VOLTAMMETRY
    assert len(ds) == len(target)
    assert ds.current_label == "Current (µA)"
    np.testing.assert_allclose(ds.current, current, atol=1e-5)
    assert set(ds.columns) == set(CV_HEADER.split(","))
    assert ds.params["mV_s"] == "100.0"
    assert ds.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_cv_sweep_branches_follow_the_target_potential(tmp_path):
    path = tmp_path / "run.csv"
    _write_cv(path, cycles=2)

    ds = load_dataset(path)
    spans = branch_spans(ds.branch)

    # Two cycles of start -> low -> high -> start: down, up, down, up, down.
    directions = [int(ds.direction[first]) for first, _last in spans]
    assert directions == [-1, 1, -1, 1, -1]
    for first, last in spans:
        moves = np.diff(ds.columns["Target V"][first:last + 1])
        assert np.all(np.sign(moves[moves != 0]) == ds.direction[first])


def test_loaded_arrays_are_read_only_and_the_file_untouched(tmp_path):
    path = tmp_path / "run.csv"
    _write_cv(path)
    before = (path.read_bytes(), os.stat(path).st_mtime_ns)

    ds = load_dataset(path)

    with pytest.raises(ValueError):
        ds.current[0] = 1.0
    assert (path.read_bytes(), os.stat(path).st_mtime_ns) == before


def test_ir_corrected_cv_plots_the_corrected_potential(tmp_path):
    path = tmp_path / "ir.csv"
    header = ("Time (s),Potential iR-corrected (V),Current (uA),Cycle,Index,Target V,"
              "Potential measured (V),iR drop subtracted (V),Range")
    rows = np.array([[0.004, -0.10, 5.0, 0, 0, -0.1, -0.12, -0.02, 3],
                     [0.008, -0.11, 6.0, 0, 0, -0.11, -0.13, -0.02, 3]])
    np.savetxt(path, rows, delimiter=",", header=header, comments="", fmt="%.5f")

    ds = load_dataset(path)

    assert ds.potential_label == "Potential iR-corrected (V)"
    np.testing.assert_allclose(ds.potential, [-0.10, -0.11])


def test_dpv_files_processed_and_raw(tmp_path):
    base = tmp_path / "dpv.csv"
    np.savetxt(base, [[-0.5, -4.2], [-0.505, -4.5], [-0.51, -4.9]], delimiter=",",
               header="Potential (V),Delta-i (uA)", comments="", fmt="%.5f")
    base.with_suffix(".params.txt").write_text("method: DPV\npulse_V: 0.05\n")
    raw = tmp_path / "dpv_RAW.csv"
    np.savetxt(raw, [[0.004, -0.5, -1.1, -0.5, 3], [0.008, -0.5, -1.2, -0.5, 3]],
               delimiter=",", header="Time (s),Potential (V),Current (uA),Target V,Range",
               comments="", fmt="%.5f")

    processed = load_dataset(base)
    staircase = load_dataset(raw)

    assert processed.technique == TECH_DPV
    assert processed.current_label == "Δi (µA)"
    assert staircase.technique == TECH_DPV_RAW
    assert staircase.params["pulse_V"] == "0.05"  # shares its run's params file


def test_legacy_headerless_file_in_amps(tmp_path):
    path = tmp_path / "legacy.csv"
    # current (A), potential (V); scientific notation must not read as a header
    path.write_text("8.28e-09,3.05e-03\n5.82e-09,3.49e-03\n5.64e-09,3.27e-03\n")

    ds = load_dataset(path)

    assert ds.technique == TECH_VOLTAMMOGRAM
    np.testing.assert_allclose(ds.current, [8.28e-3, 5.82e-3, 5.64e-3])
    np.testing.assert_allclose(ds.potential, [3.05e-3, 3.49e-3, 3.27e-3])


def test_headerless_potential_first(tmp_path):
    path = tmp_path / "generic.csv"
    path.write_text("-0.5,12.0\n-0.6,15.0\n")

    ds = load_dataset(path)

    np.testing.assert_allclose(ds.potential, [-0.5, -0.6])
    np.testing.assert_allclose(ds.current, [12.0, 15.0])


def test_milliamp_currents_are_converted_to_microamps(tmp_path):
    path = tmp_path / "ma.csv"
    path.write_text("Potential (V),Current (mA)\n0.1,0.5\n0.2,-1.25\n")

    ds = load_dataset(path)

    np.testing.assert_allclose(ds.current, [500.0, -1250.0])


def test_ocp_files_are_recognised_but_refused(tmp_path):
    path = tmp_path / "ocp.csv"
    path.write_text("Time (s),Potential (V)\n0.0,0.31\n1.0,0.30\n")

    kind = identify(path)

    assert kind.family == FAMILY_OCP and not kind.supported
    with pytest.raises(UnsupportedDataError):
        load_dataset(path)


@pytest.mark.parametrize("text", [
    "# method: constant_current\n# mmol: 0.2\n"
    "Time (s),Current (mA),Voltage (V),Charge Passed (C)\n0,1,2.5,0\n1,1,2.6,0.001\n",
    # Older electrolysis files carry no metadata, only the charge column.
    "Time (s),Current (mA),Voltage (V),Charge Passed (C)\n0,1,2.5,0\n",
])
def test_electrolysis_files_are_recognised_but_refused(tmp_path, text):
    path = tmp_path / "electrolysis.csv"
    path.write_text(text)

    kind = identify(path)

    assert kind.family == FAMILY_ELECTROLYSIS and not kind.supported
    with pytest.raises(UnsupportedDataError):
        load_dataset(path)


def test_params_path_for_each_naming_scheme(tmp_path):
    run = tmp_path / "run.csv"
    (tmp_path / "run.params.txt").write_text("method: CV\n")
    legacy = tmp_path / "old.csv"
    (tmp_path / "old_params.csv").write_text("cycles,3\n")

    assert params_path_for(run) == tmp_path / "run.params.txt"
    assert params_path_for(tmp_path / "run_RAW.csv") == tmp_path / "run.params.txt"
    assert params_path_for(legacy) == tmp_path / "old_params.csv"
    assert params_path_for(tmp_path / "none.csv") is None


def test_sweep_branches_ignore_noise_and_an_initial_hold():
    rng = np.random.default_rng(0)
    hold = np.zeros(50)
    sweep = np.concatenate([hold, np.linspace(0, -1, 500), np.linspace(-1, 0.5, 750)])
    noisy = sweep + rng.normal(0, 0.003, len(sweep))

    branch, direction = sweep_branches(noisy, 0.02)
    spans = branch_spans(branch)

    assert len(spans) == 2
    assert direction[0] == -1          # the hold takes the first sweep's direction
    assert abs(spans[0][1] - 549) <= 15  # vertex at the end of the downward sweep
    assert direction[-1] == 1

"""Offscreen tests for the Analysis-tab dataset row controls."""

import numpy as np
import pytest


@pytest.fixture
def dataset_row(qapp):
    from moles.ui.electroanalysis.analysis_tab import DatasetRow
    potential = np.linspace(-0.5, 0.5, 100)
    current = np.linspace(0.0, 10.0, 100)
    return DatasetRow(0, "demo", potential, current, '#1f77b4')


@pytest.fixture
def ocp_row(qapp):
    from moles.ui.electroanalysis.analysis_tab import DatasetRow
    from moles.ui.electroanalysis.loader import KIND_OCP
    times = np.linspace(0.0, 49.5, 100)
    potentials = np.linspace(0.30, 0.24, 100)
    return DatasetRow(0, "ocp", times, potentials, '#1f77b4',
                      y_label='Potential (V)', x_label='Time (s)',
                      kind=KIND_OCP)


def test_offset_shifts_potential(dataset_row):
    dataset_row.offset_spin.setValue(0.1)
    v, _ = dataset_row.rendered_data()
    assert np.isclose(v[0], -0.5 - 0.1)
    assert np.isclose(v[-1], 0.5 - 0.1)


def test_offset_shifts_the_potential_axis_of_an_ocp_trace(ocp_row):
    """Potential is y on a time series, so that is the axis the offset moves."""
    ocp_row.offset_spin.setValue(0.1)
    t, v = ocp_row.rendered_data()

    assert np.isclose(t[0], 0.0)         # time is untouched
    assert np.isclose(t[-1], 49.5)
    assert np.isclose(v[0], 0.30 - 0.1)
    assert np.isclose(v[-1], 0.24 - 0.1)


def test_hidden_row_renders_nothing(dataset_row):
    dataset_row.visible_cb.setChecked(False)
    assert dataset_row.rendered_data() is None


def test_range_slider_subsets_data(dataset_row):
    dataset_row.range_slider.setValues(10, 40)
    v, i = dataset_row.rendered_data()
    assert len(v) == 31  # inclusive slice 10..40
    assert len(i) == 31


def test_smoothing_reduces_noise(dataset_row):
    rng = np.random.default_rng(0)
    dataset_row.y = np.linspace(0, 10, 100) + rng.normal(0, 1.0, 100)
    dataset_row.smooth_spin.setValue(1)
    _, raw = dataset_row.rendered_data()
    dataset_row.smooth_spin.setValue(11)
    _, smoothed = dataset_row.rendered_data()
    # A moving average should lower the point-to-point variation.
    assert np.std(np.diff(smoothed)) < np.std(np.diff(raw))


def test_smoothing_applies_to_ocp_potential(ocp_row):
    rng = np.random.default_rng(0)
    ocp_row.y = np.linspace(0.30, 0.24, 100) + rng.normal(0, 0.01, 100)
    ocp_row.smooth_spin.setValue(1)
    _, raw = ocp_row.rendered_data()
    ocp_row.smooth_spin.setValue(11)
    _, smoothed = ocp_row.rendered_data()
    assert np.std(np.diff(smoothed)) < np.std(np.diff(raw))


def test_edited_arrays_ignore_visibility(dataset_row):
    # Hidden traces render nothing but must still be exportable.
    dataset_row.visible_cb.setChecked(False)
    assert dataset_row.rendered_data() is None
    v, i = dataset_row._edited_arrays()
    assert len(v) == 100 and len(i) == 100


def test_write_edited_csv_applies_offset_and_range(dataset_row, tmp_path):
    dataset_row.offset_spin.setValue(0.1)
    dataset_row.range_slider.setValues(10, 40)
    out = tmp_path / "demo_edited.csv"
    count = dataset_row.write_edited_csv(str(out))

    assert count == 31  # inclusive slice 10..40
    with open(out) as f:
        header = f.readline().strip()
    assert header == "Potential (V),Current (uA)"

    data = np.genfromtxt(out, delimiter=",", skip_header=1)
    assert data.shape == (31, 2)
    # Potential column carries the applied offset; matches the plotted slice.
    v, i = dataset_row.rendered_data()
    assert np.allclose(data[:, 0], v, atol=1e-5)
    assert np.allclose(data[:, 1], i, atol=1e-5)


def test_write_edited_csv_keeps_ocp_columns(ocp_row, tmp_path):
    ocp_row.range_slider.setValues(0, 9)
    out = tmp_path / "ocp_edited.csv"
    count = ocp_row.write_edited_csv(str(out))

    assert count == 10
    with open(out) as f:
        assert f.readline().strip() == "Time (s),Potential (V)"
    data = np.genfromtxt(out, delimiter=",", skip_header=1)
    assert np.allclose(data[:, 0], ocp_row.x[:10], atol=1e-5)


def test_write_edited_csv_writes_provenance_note(dataset_row, tmp_path):
    dataset_row.name = "demo"
    dataset_row.offset_spin.setValue(0.42)
    dataset_row.smooth_spin.setValue(5)
    dataset_row.range_slider.setValues(10, 40)
    out = tmp_path / "demo_edited.csv"
    dataset_row.write_edited_csv(str(out))

    notes = tmp_path / "demo_edited.edits.txt"
    assert notes.exists()
    text = notes.read_text()
    assert "Offset applied (V): 0.420" in text
    assert "Smoothing (moving-average window, points): 5" in text
    assert "Selected point range (indices): 10 to 40 of 0 to 99" in text
    assert "Points exported: 31" in text


def test_ocp_provenance_note_reports_the_potential_window(ocp_row, tmp_path):
    """The note's potential window must come from the y column here, not x."""
    out = tmp_path / "ocp_edited.csv"
    ocp_row.write_edited_csv(str(out))

    text = (tmp_path / "ocp_edited.edits.txt").read_text()
    assert "Potential window after offset (V): 0.240 to 0.300" in text


def test_write_edited_csv_labels_delta_i(qapp, tmp_path):
    from moles.ui.electroanalysis.analysis_tab import DatasetRow
    row = DatasetRow(
        0, "dpv", np.linspace(-0.5, 0.5, 20), np.linspace(0, 5, 20),
        '#1f77b4', y_label='Delta-i (uA)',
    )
    out = tmp_path / "dpv_edited.csv"
    row.write_edited_csv(str(out))
    with open(out) as f:
        assert f.readline().strip() == "Potential (V),Delta-i (uA)"


def test_write_edited_csv_refuses_to_overwrite_original(qapp, tmp_path):
    from moles.ui.electroanalysis.analysis_tab import DatasetRow
    original = tmp_path / "run.csv"
    original.write_text("Potential (V),Current (uA)\n0,0\n")
    row = DatasetRow(
        0, "run", np.linspace(-0.5, 0.5, 20), np.linspace(0, 5, 20),
        '#1f77b4', source_path=str(original),
    )
    with pytest.raises(ValueError):
        row.write_edited_csv(str(original))
    # The original file must be left exactly as it was.
    assert original.read_text() == "Potential (V),Current (uA)\n0,0\n"

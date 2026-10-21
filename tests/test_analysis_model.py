"""Plot-window data model of the Data Analysis app (moles.analysis.model)."""

from pathlib import Path

import numpy as np
import pytest

from moles.analysis.datasets import Voltammogram, sweep_branches
from moles.analysis.model import (
    COLOR_CYCLE, LABEL_MANUAL, LABEL_PEAK, LABEL_SHOULDER, MIN_RANGE_POINTS,
    AnalysisSession, ExperimentView, LabelledPoint, PeakSettings, PlotWindowState,
    format_label,
)


def _dataset(n=200, name="run"):
    potential = np.linspace(-1.0, 0.0, n)
    current = np.linspace(-50.0, 50.0, n)
    branch, direction = sweep_branches(potential, 0.005)
    return Voltammogram(path=Path(f"/data/{name}.csv"), sha256="ab" * 32, technique="CV",
                        potential=potential, current=current,
                        potential_label="Potential (V)", current_label="Current (µA)",
                        branch=branch, direction=direction)


def test_label_format_and_shoulder_prefix():
    assert format_label(-1.46213, -45.11849, LABEL_PEAK) == "(-1.462 V, -45.1 µA)"
    assert format_label(-1.65, -62.36, LABEL_SHOULDER) == "sh (-1.650 V, -62.4 µA)"
    assert format_label(0.5, 1.0, LABEL_MANUAL) == "(0.500 V, 1.0 µA)"


def test_labels_follow_the_offsets():
    ds = _dataset()
    exp = ExperimentView.for_dataset(ds, "run.csv")
    label = LabelledPoint(100, LABEL_PEAK, "max")
    exp.add_label(label)
    raw_x, raw_y = float(ds.potential[100]), float(ds.current[100])

    exp.x_offset, exp.y_offset = 0.25, -10.0

    assert exp.displayed_point(ds, 100) == pytest.approx((raw_x + 0.25, raw_y - 10.0))
    assert exp.label_text(ds, label) == format_label(raw_x + 0.25, raw_y - 10.0, LABEL_PEAK)


def test_displayed_arrays_apply_offsets_and_range():
    ds = _dataset()
    exp = ExperimentView.for_dataset(ds, "run.csv")
    exp.x_offset, exp.y_offset = 0.1, 5.0
    exp.set_range(20, 79)

    indices, x, y = exp.displayed_arrays(ds)

    assert indices[0] == 20 and indices[-1] == 79 and len(x) == 60
    np.testing.assert_allclose(x, ds.potential[20:80] + 0.1)
    np.testing.assert_allclose(y, ds.current[20:80] + 5.0)


def test_range_is_clamped_ordered_and_never_too_short():
    exp = ExperimentView.for_dataset(_dataset(n=200), "run.csv")

    assert exp.set_range(-5, 500) == (0, 199)
    start, end = exp.set_range(150, 140)      # end before start
    assert end - start + 1 == MIN_RANGE_POINTS
    start, end = exp.set_range(199, 199)      # squeezed against the end
    assert (start, end) == (200 - MIN_RANGE_POINTS, 199)


def test_labels_outside_the_range_are_hidden_but_kept():
    exp = ExperimentView.for_dataset(_dataset(), "run.csv")
    exp.add_label(LabelledPoint(10, LABEL_MANUAL))
    exp.add_label(LabelledPoint(150, LABEL_PEAK, "min"))

    exp.set_range(50, 199)
    assert [lp.index for lp in exp.shown_labels()] == [150]
    assert len(exp.labels) == 2

    exp.set_range(0, 199)
    assert [lp.index for lp in exp.shown_labels()] == [10, 150]


def test_a_point_is_labelled_once_and_labels_can_be_removed():
    exp = ExperimentView.for_dataset(_dataset(), "run.csv")

    assert exp.add_label(LabelledPoint(42, LABEL_PEAK, "max"))
    assert not exp.add_label(LabelledPoint(42, LABEL_MANUAL))
    with pytest.raises(IndexError):
        exp.add_label(LabelledPoint(500, LABEL_MANUAL))
    assert exp.remove_label(42) and not exp.remove_label(42)


def test_windows_showing_one_file_are_independent():
    ds = _dataset()
    first, second = PlotWindowState("Plot 1"), PlotWindowState("Plot 2")
    a = first.add_experiment(ds, "run.csv")
    b = second.add_experiment(ds, "run.csv")

    a.x_offset = 0.3
    a.add_label(LabelledPoint(5, LABEL_MANUAL))
    a.display_name = "renamed"

    assert b.x_offset == 0.0 and b.labels == [] and b.display_name == "run"


def test_a_window_gives_new_experiments_free_colours_and_unique_names():
    ds = _dataset()
    window = PlotWindowState("Plot 1")

    first = window.add_experiment(ds, "run.csv")
    again = window.add_experiment(ds, "run.csv")  # the same file twice is allowed

    assert first.color == COLOR_CYCLE[0] and again.color == COLOR_CYCLE[1]
    assert (first.display_name, again.display_name) == ("run", "run (2)")
    window.selected_uid = again.uid
    window.remove_experiment(again.uid)
    assert window.selected is None and len(window.experiments) == 1


def test_session_round_trips_through_json():
    ds = _dataset()
    session = AnalysisSession()
    window = PlotWindowState(session.next_title(), view_range=(-1.0, 0.0, -60.0, 60.0),
                             peak_settings=PeakSettings(smoothing_mV=20.0, prominence_uA=2.5,
                                                        report_valleys=True),
                             popped_out=True, geometry=(10, 20, 800, 600))
    exp = window.add_experiment(ds, "folder/run.csv")
    exp.x_offset, exp.y_offset, exp.line_style = -0.36, 1.5, "dash"
    exp.set_range(10, 150)
    exp.add_label(LabelledPoint(60, LABEL_SHOULDER, "min"))
    window.selected_uid = exp.uid
    session.windows.append(window)
    session.active_window = window.uid

    restored = AnalysisSession.from_json(session.to_json())

    assert restored.to_dict() == session.to_dict()
    assert session.next_title() == "Plot 2"


def test_session_from_a_newer_format_is_refused():
    with pytest.raises(ValueError):
        AnalysisSession.from_dict({"version": 99, "windows": []})


# ----------------------------------------------------------------------
# Moving-average smoothing
# ----------------------------------------------------------------------

def test_moving_average_is_centred_and_does_not_dip_at_the_ends():
    from moles.analysis.smoothing import moving_average

    flat = np.full(50, 7.0)
    ramp = np.arange(20, dtype=float)

    np.testing.assert_allclose(moving_average(flat, 9), flat)   # no fall-off at the ends
    np.testing.assert_allclose(moving_average(ramp, 5)[2:-2], ramp[2:-2])  # centred: no shift
    np.testing.assert_array_equal(moving_average(ramp, 1), ramp)
    assert moving_average(ramp, 5)[0] == pytest.approx((0 + 1 + 2) / 3)


def test_moving_average_reduces_noise():
    from moles.analysis.smoothing import moving_average
    noise = np.random.default_rng(0).normal(0, 1, 20_000)

    assert np.std(moving_average(noise, 25)) < 0.25


def test_smoothing_changes_what_an_experiment_displays():
    from moles.analysis.smoothing import moving_average
    ds = _dataset()
    ds_current = np.array(ds.current)
    exp = ExperimentView.for_dataset(ds, "run.csv")
    exp.add_label(LabelledPoint(0, LABEL_MANUAL))
    exp.y_offset = 1.0

    exp.smoothing_points = 11
    _i, _x, y = exp.displayed_arrays(ds)

    smoothed = moving_average(ds_current, 11)
    np.testing.assert_allclose(y, smoothed + 1.0)
    assert exp.displayed_point(ds, 0)[1] == pytest.approx(smoothed[0] + 1.0)
    assert exp.label_text(ds, exp.labels[0]) == format_label(
        float(ds.potential[0]), smoothed[0] + 1.0, LABEL_MANUAL)
    np.testing.assert_array_equal(ds.current, ds_current)        # the data is untouched
    assert ExperimentView.from_dict(exp.to_dict()).smoothing_points == 11
    assert ExperimentView.from_dict({k: v for k, v in exp.to_dict().items()
                                     if k != "smoothing_points"}).smoothing_points == 1


def test_experiments_move_within_their_window():
    window = PlotWindowState("Plot 1")
    a, b, c = (window.add_experiment(_dataset(name=n), f"{n}.csv") for n in "abc")

    assert window.move_experiment(c.uid, -1)
    assert [e.display_name for e in window.experiments] == ["a", "c", "b"]
    assert not window.move_experiment(a.uid, -1)      # already first
    assert not window.move_experiment(b.uid, 1)       # already last
    assert [e.display_name for e in window.experiments] == ["a", "c", "b"]


def test_plot_style_and_legend_place_are_saved():
    from moles.analysis.model import PlotStyle
    window = PlotWindowState("Plot 1", style=PlotStyle(label_font_pt=12.5, legend_line_pt=6.0),
                             legend_box=(0.6, 0.05))

    restored = PlotWindowState.from_dict(window.to_dict())

    assert restored.style == window.style and restored.legend_box == (0.6, 0.05)
    older = {k: v for k, v in window.to_dict().items() if k not in ("style", "legend_box")}
    assert PlotWindowState.from_dict(older).style == PlotStyle()

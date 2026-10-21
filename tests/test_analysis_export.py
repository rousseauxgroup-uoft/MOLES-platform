"""Exported figures and workbooks (moles.analysis.export), and the plot window's Export menu."""

import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from moles.analysis import export
from moles.analysis.datasets import load_dataset
from moles.analysis.model import LabelledPoint, PlotWindowState

CV_HEADER = "Time (s),Potential (V),Current (uA),Cycle,Index,Target V,Range"
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _write_cv(path, scale=1.0, n=1000):
    target = np.concatenate([np.linspace(0, -1, n // 2), np.linspace(-1, 0, n - n // 2)])
    current = scale * 40 * np.sin(np.linspace(0, 4 * np.pi, n))
    rows = np.column_stack([np.arange(n) * 0.004, target, current, np.zeros(n),
                            np.zeros(n), target, np.full(n, 3)])
    np.savetxt(path, rows, delimiter=",", header=CV_HEADER, comments="", fmt="%.6f")
    Path(str(path)[:-4] + ".params.txt").write_text("method: CV\nmV_s: 100.0\n")


@pytest.fixture
def window_state(tmp_path):
    _write_cv(tmp_path / "a.csv")
    _write_cv(tmp_path / "b.csv", scale=0.5)
    a, b = load_dataset(tmp_path / "a.csv"), load_dataset(tmp_path / "b.csv")
    state = PlotWindowState("Ni intermediates")
    ea, eb = state.add_experiment(a, "a.csv"), state.add_experiment(b, "b.csv")
    ea.display_name, eb.display_name = "NiBr2(bpy)", "NiBr2(bpy) + 1"
    ea.x_offset, ea.y_offset = -0.36, 2.0
    ea.set_range(100, 599)
    eb.line_style = "dash"
    ea.add_label(LabelledPoint(200, "peak", "max"))
    ea.add_label(LabelledPoint(300, "shoulder", "min", text_offset=(-60.0, 30.0)))
    ea.add_label(LabelledPoint(900, "manual"))          # outside the data range
    eb.add_label(LabelledPoint(50, "manual"))
    return state, {ea.uid: a, eb.uid: b}


def _read_xlsx(path):
    """{sheet name: rows of cell values (strings)} of a workbook, plus its file list."""
    z = zipfile.ZipFile(path)
    strings = [si.findtext(".//m:t", namespaces=_NS) for si in
               ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS)]
    book = ET.fromstring(z.read("xl/workbook.xml"))
    sheets = {}
    for i, sheet in enumerate(book.find("m:sheets", _NS), start=1):
        root = ET.fromstring(z.read(f"xl/worksheets/sheet{i}.xml"))
        rows = []
        for row in root.find("m:sheetData", _NS).findall("m:row", _NS):
            values = []
            for c in row.findall("m:c", _NS):
                v = c.findtext("m:v", namespaces=_NS)
                values.append(strings[int(v)] if c.get("t") == "s" else v)
            rows.append(values)
        sheets[sheet.get("name")] = rows
    return sheets, z.namelist()


# ----------------------------------------------------------------------
# Figure
# ----------------------------------------------------------------------

def test_the_figure_shows_what_the_window_shows(window_state):
    state, datasets = window_state
    state.view_range = (-1.5, 0.2, -60.0, 60.0)
    ea, eb = state.experiments

    fig = export.render_figure(state, datasets)
    ax = fig.axes[0]

    traces = [line for line in ax.get_lines() if not line.get_label().startswith("_")]
    assert [t.get_label() for t in traces] == ["NiBr2(bpy)", "NiBr2(bpy) + 1"]
    assert traces[1].get_linestyle() == "--"
    np.testing.assert_allclose(traces[0].get_xdata()[:3], datasets[ea.uid].potential[100:103] - 0.36)
    assert ax.get_xlim() == (-1.5, 0.2) and ax.get_ylim() == (-60.0, 60.0)
    assert [t.get_text() for t in ax.get_legend().get_texts()] == ["NiBr2(bpy)", "NiBr2(bpy) + 1"]


def test_labels_keep_their_text_position_and_leader(window_state):
    state, datasets = window_state
    ea = state.experiments[0]

    ax = export.render_figure(state, datasets).axes[0]
    notes = {a.get_text(): a for a in ax.texts}

    shown = [ea.label_text(datasets[ea.uid], lp) for lp in ea.shown_labels()]
    assert set(shown) <= set(notes) and len(ax.texts) == 3          # the out-of-range one is left out
    dragged = notes[ea.label_text(datasets[ea.uid], ea.labels[1])]
    assert dragged.xyann == (-60.0, 30.0) and dragged.arrow_patch is not None
    assert notes[shown[0]].arrow_patch is None


def test_hidden_experiments_stay_out_of_the_figure(window_state):
    state, datasets = window_state
    state.experiments[1].visible = False

    ax = export.render_figure(state, datasets).axes[0]

    assert [l.get_label() for l in ax.get_lines() if not l.get_label().startswith("_")] == ["NiBr2(bpy)"]


def test_the_figure_follows_the_figure_style(window_state):
    state, datasets = window_state

    ax = export.render_figure(state, datasets).axes[0]

    assert all(spine.get_visible() for spine in ax.spines.values())
    assert ax.xaxis.get_ticks_position() == "bottom" and ax.yaxis.get_ticks_position() == "left"
    assert ax.xaxis.get_tick_params(which="major")["direction"] == "out"
    assert "Potential" in ax.get_xlabel() and "(V)" in ax.get_xlabel()
    assert "Current" in ax.get_ylabel() and "(µA)" in ax.get_ylabel()


def test_png_and_svg_files(tmp_path, window_state):
    state, datasets = window_state
    fig = export.render_figure(state, datasets)

    export.save_figure(fig, tmp_path / "plot.png")
    export.save_figure(fig, tmp_path / "plot.svg")

    assert (tmp_path / "plot.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    svg = (tmp_path / "plot.svg").read_text()
    assert "<svg" in svg and "NiBr2(bpy) + 1" in svg   # text stays editable text


# ----------------------------------------------------------------------
# Workbook
# ----------------------------------------------------------------------

def test_workbook_has_the_plot_each_experiment_and_the_labels(tmp_path, window_state):
    state, datasets = window_state
    ea, eb = state.experiments
    png = export.figure_png(export.render_figure(state, datasets), dpi=50)

    export.export_workbook(tmp_path / "out.xlsx", state, datasets, png,
                           origins={ea.uid: ["/raw/a.csv"]}, missing=["gone.csv"])
    sheets, files = _read_xlsx(tmp_path / "out.xlsx")

    assert list(sheets) == ["Plot", "NiBr2(bpy)", "NiBr2(bpy) + 1", "Labelled points"]
    assert any(f.startswith("xl/media/") for f in files)
    assert any("gone.csv" in " ".join(r) for r in sheets["Plot"])

    a_rows = sheets["NiBr2(bpy)"]
    details = {r[0]: r[1] for r in a_rows if len(r) == 2}
    assert details["Experiment"] == "NiBr2(bpy)" and details["Copied from"] == "/raw/a.csv"
    assert float(details["X offset (V)"]) == -0.36 and details["Scan rate (mV/s)"] == "100.0"
    header = next(i for i, r in enumerate(a_rows) if r and r[0] == "Point")
    assert a_rows[header] == ["Point", "Time (s)", "Cycle", "Potential (V)", "Current (µA)"]
    data = a_rows[header + 1:]
    assert len(data) == 500 and data[0][0] == "100" and data[-1][0] == "599"
    assert float(data[0][3]) == pytest.approx(datasets[ea.uid].potential[100] - 0.36)
    assert float(data[0][4]) == pytest.approx(datasets[ea.uid].current[100] + 2.0)

    labels = sheets["Labelled points"]
    assert labels[0] == ["Experiment", "Potential (V)", "Current (µA)", "Type", "Point"]
    assert [(r[0], r[3], r[4]) for r in labels[1:]] == [
        ("NiBr2(bpy)", "peak", "200"), ("NiBr2(bpy)", "shoulder", "300"),
        ("NiBr2(bpy) + 1", "manual", "50")]
    x, y = ea.displayed_point(datasets[ea.uid], 200)
    assert float(labels[1][1]) == pytest.approx(x) and float(labels[1][2]) == pytest.approx(y)


def test_sheet_names_are_excel_safe_and_unique():
    names = export.sheet_names(["Ni", "ni", "a/b:c*?[x]", "y" * 40, "Plot", "Labelled points", ""])

    assert names[:3] == ["Ni", "ni (2)", "abcx"]
    assert len(names[3]) == 31
    assert names[4] == "Plot (2)" and names[5] == "Labelled points (2)"
    assert names[6] == "Experiment"


def test_a_range_too_long_for_excel_is_refused(tmp_path, window_state, monkeypatch):
    state, datasets = window_state
    monkeypatch.setattr(export, "_EXCEL_MAX_ROWS", 100)

    with pytest.raises(ValueError):
        export.export_workbook(tmp_path / "out.xlsx", state, datasets, b"")


# ----------------------------------------------------------------------
# The Export menu of a plot window
# ----------------------------------------------------------------------

@pytest.fixture
def panel(qapp, tmp_path):
    from moles.analysis.model import PlotWindowState
    from moles.analysis.store import DatasetStore
    from moles.analysis.workspace import Workspace
    from moles.ui.data_analysis.plot_panel import PlotPanel

    root = tmp_path / "ws"
    root.mkdir()
    _write_cv(root / "a.csv")
    p = PlotPanel(PlotWindowState("Fig 3: Ni/bpy"), DatasetStore(Workspace(root)))
    p.resize(1000, 600)
    p.add_files(["a.csv"])
    p.show_error = lambda title, text: (_ for _ in ()).throw(AssertionError(text))
    yield p
    p.deleteLater()


def test_export_to_excel_then_offer_the_image(panel, tmp_path):
    asked = []
    panel.offer_image = lambda: True
    panel.ask_save_path = lambda title, suggested, filters: (
        asked.append(suggested) or str(tmp_path / "figure.svg"))

    assert panel.export_excel(tmp_path / "out")

    assert (tmp_path / "out.xlsx").is_file() and (tmp_path / "figure.svg").is_file()
    assert asked == ["Fig 3_ Ni_bpy.png"]          # a safe file name from the title
    assert "Saved the plot as figure.svg" in panel.status.text()


def test_save_image_defaults_to_png(panel, tmp_path):
    assert panel.save_image(tmp_path / "figure")

    assert (tmp_path / "figure.png").read_bytes()[:4] == b"\x89PNG"


def test_a_smoothed_experiment_exports_both_currents(tmp_path, window_state):
    from moles.analysis.smoothing import moving_average
    state, datasets = window_state
    ea = state.experiments[0]
    ea.smoothing_points = 9
    png = export.figure_png(export.render_figure(state, datasets), dpi=30)

    export.export_workbook(tmp_path / "out.xlsx", state, datasets, png)
    rows = _read_xlsx(tmp_path / "out.xlsx")[0]["NiBr2(bpy)"]

    details = {r[0]: r[1] for r in rows if len(r) == 2}
    assert details["Smoothing (moving average)"] == "9 points"
    header = next(i for i, r in enumerate(rows) if r and r[0] == "Point")
    assert rows[header][-2:] == ["Current (µA)", "Current unsmoothed (µA)"]
    first = rows[header + 1]
    raw = datasets[ea.uid].current
    assert float(first[-2]) == pytest.approx(moving_average(raw, 9)[100] + 2.0)
    assert float(first[-1]) == pytest.approx(raw[100] + 2.0)


# ----------------------------------------------------------------------
# Matching the screen
# ----------------------------------------------------------------------

def test_axis_ticks_are_round_in_range_and_follow_the_axis_length():
    from moles.analysis.appearance import axis_ticks

    ticks = axis_ticks(-2.1, -0.25, 500, 10, "x")
    values = [v for v, _t in ticks]
    assert [t for _v, t in ticks][:3] == ["-2.0", "-1.8", "-1.6"]
    assert all(-2.1 <= v <= -0.25 for v in values)
    assert len(axis_ticks(-2.1, -0.25, 150, 10, "x")) < len(ticks)    # shorter axis, fewer ticks
    assert [t for _v, t in axis_ticks(-0.05, 0.05, 300, 10)] == ["-0.04", "-0.02", "0.00", "0.02", "0.04"]
    assert axis_ticks(1.0, 1.0, 300, 10) == []


def test_the_figure_takes_the_windows_size_layout_and_style(window_state):
    from moles.analysis.appearance import axis_ticks
    from moles.analysis.model import PlotStyle
    state, datasets = window_state
    state.style = PlotStyle(label_font_pt=13, axis_title_pt=15, tick_font_pt=9,
                            legend_font_pt=12, legend_line_pt=6)
    state.legend_box = (0.1, 0.2)
    layout = export.FigureLayout(7.0, 5.0, (0.1, 0.12, 0.85, 0.8))

    fig = export.render_figure(state, datasets, layout=layout, view_range=(-1.2, 0.1, -50, 50))
    ax = fig.axes[0]

    assert tuple(fig.get_size_inches()) == (7.0, 5.0)
    assert tuple(np.round(ax.get_position().bounds, 6)) == (0.1, 0.12, 0.85, 0.8)
    assert ax.xaxis.label.get_fontsize() == 15
    assert ax.get_xticklabels()[0].get_fontsize() == 9
    expected = axis_ticks(-1.2, 0.1, 7.0 * 0.85 * 72, 9, "x")
    assert [t.get_text() for t in ax.get_xticklabels()] == [text for _v, text in expected]
    assert {a.get_fontsize() for a in ax.texts} == {13}
    legend = ax.get_legend()
    assert [h.get_linewidth() for h in legend.legend_handles] == [6, 6]
    assert legend.get_texts()[0].get_fontsize() == 12


def test_a_dashed_trace_uses_the_screens_dash_pattern(window_state):
    from moles.analysis.appearance import DASH_PATTERNS, TRACE_LINE_PT
    state, datasets = window_state

    ax = export.render_figure(state, datasets).axes[0]
    dashed = [l for l in ax.get_lines() if l.get_label() == "NiBr2(bpy) + 1"][0]

    offset, pattern = dashed._unscaled_dash_pattern
    assert tuple(pattern) == DASH_PATTERNS["dash"] and dashed.get_linewidth() == TRACE_LINE_PT

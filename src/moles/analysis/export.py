"""Exporting a plot window: a publication-style figure and an Excel workbook.

The figure is drawn again with matplotlib rather than captured from the
screen, so an SVG keeps real, editable text, and it is drawn to look like the
window: at the plot's on-screen size with its plot area in the same place
(``FigureLayout``), the same view range, ticks, fonts and text sizes, line
widths and dashes, legend place, and labels where they were dragged. Both
read their sizes from ``appearance`` and the window's ``PlotStyle``.

The workbook holds the figure, one sheet per experiment (its data with
smoothing, offsets and data range applied, plus where it came from), and one
sheet of every labelled point in displayed coordinates. Exports always use every data
point; only the figure's lines are thinned the way the screen thins them.
"""

import io
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import matplotlib
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from .appearance import (
    DASH_PATTERNS, FONT_FAMILIES, FRAME_COLOR, FRAME_LINE_PT, LABEL_MARKER_PT, LEGEND_BACKING,
    LEADER_LINE_PT, LEGEND_GAP_PT, LEGEND_PAD_PT, LEGEND_ROW_EM, LEGEND_ROW_GAP_PT, LEGEND_SAMPLE_PT,
    MARKER_RIM_PT, TEXT_COLOR, TICK_LENGTH_PT,
    TICK_PAD_PT, TRACE_LINE_PT, axis_ticks,
)
from .display import every_nth, thin
from .model import LABEL_MANUAL, LABEL_PEAK, LABEL_SHOULDER, PlotWindowState

# Size of a figure drawn without a window to copy (height from ``aspect``).
DEFAULT_WIDTH_IN = 6.5
PNG_DPI = 300

_STYLE = {
    "font.family": "sans-serif",
    "font.sans-serif": list(FONT_FAMILIES),
    # Bold quantity names in axis titles, in the same font as the rest.
    "mathtext.fontset": "custom",
    "mathtext.rm": "sans",
    "mathtext.bf": "sans:bold",
    # Text stays text in an SVG, so it can be edited for a paper.
    "svg.fonttype": "none",
}

# Excel's limits on sheet names, and the sheets this export names itself.
_SHEET_NAME_MAX = 31
_SHEET_BAD_CHARS = re.compile(r"[\[\]:*?/\\]")
PLOT_SHEET = "Plot"
LABELS_SHEET = "Labelled points"
_EXCEL_MAX_ROWS = 1_048_576

KIND_NAMES = {LABEL_PEAK: "peak", LABEL_SHOULDER: "shoulder", LABEL_MANUAL: "manual"}


@dataclass
class FigureLayout:
    """A figure's size (inches) and its plot area (left, bottom, width, height as
    fractions of the figure), copied from a plot window on screen."""

    width_in: float
    height_in: float
    axes: Tuple[float, float, float, float]


def _mathbf(word):
    return r"$\mathbf{" + word.replace(" ", r"\ ") + "}$"


# ----------------------------------------------------------------------
# Figure
# ----------------------------------------------------------------------

def _shown(state: PlotWindowState, datasets):
    """Experiments drawn in the figure: visible ones whose data is loaded."""
    return [e for e in state.experiments if e.visible and e.uid in datasets]


def render_figure(state: PlotWindowState, datasets: Dict[str, object],
                  layout: Optional[FigureLayout] = None,
                  view_range: Optional[Tuple[float, float, float, float]] = None,
                  aspect: float = 0.75) -> Figure:
    """Draw a plot window as a matplotlib figure.

    ``datasets`` maps experiment uid to its loaded data (experiments without
    data are left out). ``layout`` copies the window's size and plot area;
    without one the figure is ``DEFAULT_WIDTH_IN`` wide and ``aspect`` as
    tall, laid out by matplotlib. ``view_range`` overrides the window's saved
    one, e.g. with what the screen shows right now.
    """
    style = state.style
    with matplotlib.rc_context(_STYLE):
        if layout is not None:
            fig = Figure(figsize=(layout.width_in, layout.height_in))
            ax = fig.add_axes(layout.axes)
        else:
            fig = Figure(figsize=(DEFAULT_WIDTH_IN, DEFAULT_WIDTH_IN * aspect))
            ax = fig.add_subplot(111)
        FigureCanvasAgg(fig)
        _style_axes(ax, style)

        shown = _shown(state, datasets)
        for exp in shown:
            _indices, x, y = exp.displayed_arrays(datasets[exp.uid])
            x, y = thin(x, y) if exp.line_style == "solid" else every_nth(x, y)
            dashes = DASH_PATTERNS.get(exp.line_style)
            ax.plot(x, y, color=exp.color, lw=TRACE_LINE_PT,
                    linestyle=(0, dashes) if dashes else "-",
                    label=exp.display_name, zorder=2)

        _set_view(ax, state, datasets, view_range)
        for exp in shown:
            _draw_labels(ax, exp, datasets[exp.uid], style)
        if shown:
            _draw_legend(ax, state)
        if layout is None:
            fig.tight_layout()
        _set_ticks(ax, fig, style)
        if layout is None:
            fig.tight_layout()
    return fig


def _style_axes(ax, style):
    for spine in ax.spines.values():
        spine.set_color(FRAME_COLOR)
        spine.set_linewidth(FRAME_LINE_PT)
    ax.tick_params(which="both", direction="out", color=FRAME_COLOR, labelcolor=TEXT_COLOR,
                   top=False, right=False, length=TICK_LENGTH_PT, width=FRAME_LINE_PT,
                   pad=TICK_PAD_PT, labelsize=style.tick_font_pt)
    ax.minorticks_off()
    ax.set_xlabel(f"{_mathbf('Potential')} (V)", fontsize=style.axis_title_pt, color=TEXT_COLOR)
    ax.set_ylabel(f"{_mathbf('Current')} (µA)", fontsize=style.axis_title_pt, color=TEXT_COLOR)


def _set_ticks(ax, fig, style):
    """The ticks the window shows for this axis length (``appearance.axis_ticks``)."""
    box = ax.get_position()
    width_pt = box.width * fig.get_figwidth() * 72
    height_pt = box.height * fig.get_figheight() * 72
    for axis, (lo, hi), length, kind in ((ax.xaxis, ax.get_xlim(), width_pt, "x"),
                                         (ax.yaxis, ax.get_ylim(), height_pt, "y")):
        ticks = axis_ticks(lo, hi, length, style.tick_font_pt, kind)
        axis.set_ticks([v for v, _text in ticks])
        axis.set_ticklabels([text for _v, text in ticks])


def _set_view(ax, state, datasets, view_range=None):
    view_range = view_range or state.view_range
    if view_range is not None:
        x0, x1, y0, y1 = view_range
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        return
    xs, ys = [], []
    for exp in _shown(state, datasets):
        _i, x, y = exp.displayed_arrays(datasets[exp.uid])
        xs.append(x)
        ys.append(y)
    if xs:
        x, y = np.concatenate(xs), np.concatenate(ys)
        # Room above and below for the labels of the highest and lowest peaks.
        pad_x = 0.02 * (x.max() - x.min() or 1.0)
        pad_y = 0.10 * (y.max() - y.min() or 1.0)
        ax.set_xlim(x.min() - pad_x, x.max() + pad_x)
        ax.set_ylim(y.min() - pad_y, y.max() + pad_y)


def _draw_legend(ax, state):
    """The legend as on screen: rows ``LEGEND_ROW_EM`` tall with the names centred in them."""
    style = state.style
    em = style.legend_font_pt  # legend spacings are given in font sizes
    # matplotlib makes a row as tall as its text; the extra space a screen row
    # has goes between the rows instead, and half of it above the first.
    text_pt = _text_height_pt(ax.figure, em)
    extra_pt = max(0.0, LEGEND_ROW_EM * em - text_pt)
    options = dict(frameon=True, facecolor=LEGEND_BACKING[:3], framealpha=LEGEND_BACKING[3],
                   edgecolor="none", fancybox=False, fontsize=em,
                   handlelength=LEGEND_SAMPLE_PT / em, handletextpad=LEGEND_GAP_PT / em,
                   borderpad=(LEGEND_PAD_PT + extra_pt / 2) / em,
                   labelspacing=(LEGEND_ROW_GAP_PT + extra_pt) / em,
                   borderaxespad=0.0)
    if state.legend_box is not None:
        fx, fy = state.legend_box
        # The larger pad above is matched by the box reaching higher by as much.
        shift = (extra_pt / 2) / (ax.get_position().height * ax.figure.get_figheight() * 72)
        options.update(loc="upper left", bbox_to_anchor=(fx, 1 - fy + shift))
    else:
        options.update(loc="upper right", borderaxespad=0.6)
    legend = ax.legend(**options)
    for handle in legend.legend_handles:
        handle.set_linewidth(style.legend_line_pt)


def _text_height_pt(fig, size_pt):
    """Height matplotlib gives a line of text (what a legend row is sized by), in points."""
    from matplotlib.font_manager import FontProperties
    renderer = fig.canvas.get_renderer()
    _w, height, _d = renderer.get_text_width_height_descent(
        "lp", FontProperties(size=size_pt), ismath=False)
    return height * 72.0 / fig.dpi


def _draw_labels(ax, exp, dataset, style):
    for lp in exp.shown_labels():
        x, y = exp.displayed_point(dataset, lp.index)
        ax.plot([x], [y], "o", color=exp.color, mec="white", mew=MARKER_RIM_PT,
                ms=LABEL_MARKER_PT, zorder=4)
        arrow = (dict(arrowstyle="-", color=exp.color, lw=LEADER_LINE_PT, shrinkA=0, shrinkB=3)
                 if lp.has_leader() else None)
        text = ax.annotate(exp.label_text(dataset, lp), xy=(x, y), xytext=lp.offset_pt(),
                           textcoords="offset points", ha="center", va="center",
                           color=exp.color, fontsize=style.label_font_pt, zorder=5,
                           bbox=dict(boxstyle="round,pad=0.15", fc=(1, 1, 1, 0.75), ec="none"),
                           arrowprops=arrow)
        # Cut at the frame like the screen does, never over the tick numbers.
        text.set_clip_on(True)


def save_figure(fig: Figure, path, dpi: int = PNG_DPI):
    """Save as PNG or SVG, chosen by the file's extension."""
    with matplotlib.rc_context(_STYLE):
        fig.savefig(str(path), dpi=dpi, facecolor="white")


def figure_png(fig: Figure, dpi: int = PNG_DPI) -> bytes:
    buffer = io.BytesIO()
    with matplotlib.rc_context(_STYLE):
        fig.savefig(buffer, format="png", dpi=dpi, facecolor="white")
    return buffer.getvalue()


# ----------------------------------------------------------------------
# Workbook
# ----------------------------------------------------------------------

def sheet_names(names: Sequence[str]) -> List[str]:
    """Excel-safe, unique sheet names for experiments, in order.

    Characters Excel forbids are dropped, names are cut to 31 characters, and
    clashes (with each other or with the export's own sheets, ignoring case)
    get a number.
    """
    taken = {PLOT_SHEET.lower(), LABELS_SHEET.lower()}
    result = []
    for name in names:
        base = _SHEET_BAD_CHARS.sub("", name).strip().strip("'") or "Experiment"
        candidate = base[:_SHEET_NAME_MAX]
        n = 2
        while candidate.lower() in taken:
            suffix = f" ({n})"
            candidate = base[:_SHEET_NAME_MAX - len(suffix)] + suffix
            n += 1
        taken.add(candidate.lower())
        result.append(candidate)
    return result


def _column(dataset, keyword):
    for name, values in dataset.columns.items():
        if keyword in name.lower():
            return name, values
    return None, None


def experiment_rows(exp, dataset):
    """Header and rows of one experiment's sheet: index, time, cycle, potential, current.

    The current is as displayed (smoothed, if the experiment is, and offset);
    a smoothed experiment also gets its unsmoothed current, offset alike.
    """
    sl = slice(exp.range_start, exp.range_end + 1)
    indices, x, y = exp.displayed_arrays(dataset)
    headers, columns = ["Point"], [indices]
    for keyword, title in (("time", "Time (s)"), ("cycle", "Cycle")):
        _name, values = _column(dataset, keyword)
        if values is not None:
            headers.append(title)
            columns.append(values[sl])
    headers += ["Potential (V)", dataset.current_label]
    columns += [x, y]
    if exp.smoothing_points > 1:
        quantity, unit = dataset.current_label.split(" (")
        headers.append(f"{quantity} unsmoothed ({unit}")
        columns.append(dataset.current[sl] + exp.y_offset)
    return headers, columns


def export_workbook(path, state: PlotWindowState, datasets: Dict[str, object],
                    figure_png_bytes: bytes, origins: Optional[Dict[str, Iterable[str]]] = None,
                    missing: Sequence[str] = ()):
    """Write a plot window to one .xlsx file.

    ``origins`` maps an experiment uid to the original paths its file was
    copied from; ``missing`` names workspace files that could not be exported
    because they are no longer in the workspace.
    """
    import xlsxwriter

    origins = origins or {}
    exported = [e for e in state.experiments if e.uid in datasets]
    for exp in exported:
        if exp.range_end - exp.range_start + 12 > _EXCEL_MAX_ROWS:
            raise ValueError(f"{exp.display_name} has more points in its range than an "
                             "Excel sheet holds; narrow its data range first.")

    book = xlsxwriter.Workbook(str(path), {"nan_inf_to_errors": True})
    try:
        bold = book.add_format({"bold": True})
        volts = book.add_format({"num_format": "0.000"})
        microamps = book.add_format({"num_format": "0.0"})

        plot = book.add_worksheet(PLOT_SHEET)
        plot.write(0, 0, state.title, bold)
        plot.write(1, 0, f"Exported {datetime.now():%Y-%m-%d %H:%M} by MOLES Data Analysis")
        row = 2
        if missing:
            plot.write(row, 0, "Not exported (file no longer in the workspace): "
                       + ", ".join(missing))
            row += 1
        plot.insert_image(row + 1, 0, "plot.png",
                          {"image_data": io.BytesIO(figure_png_bytes),
                           "x_scale": 96 / PNG_DPI, "y_scale": 96 / PNG_DPI})

        for exp, name in zip(exported, sheet_names([e.display_name for e in exported])):
            _write_experiment(book.add_worksheet(name), exp, datasets[exp.uid],
                              list(origins.get(exp.uid, [])), bold)

        labels = book.add_worksheet(LABELS_SHEET)
        headers = ["Experiment", "Potential (V)", "Current (µA)", "Type", "Point"]
        for col, title in enumerate(headers):
            labels.write(0, col, title, bold)
        row = 1
        for exp in exported:
            dataset = datasets[exp.uid]
            for lp in exp.shown_labels():
                x, y = exp.displayed_point(dataset, lp.index)
                labels.write(row, 0, exp.display_name)
                labels.write_number(row, 1, x, volts)
                labels.write_number(row, 2, y, microamps)
                labels.write(row, 3, KIND_NAMES.get(lp.kind, lp.kind))
                labels.write_number(row, 4, lp.index)
                row += 1
        labels.set_column(0, 0, 28)
        labels.set_column(1, 4, 14)
        labels.freeze_panes(1, 0)
    finally:
        book.close()


def _write_experiment(sheet, exp, dataset, original_paths, bold):
    details = [
        ("Experiment", exp.display_name),
        ("Workspace file", exp.file),
        ("Copied from", "; ".join(original_paths) or "not recorded"),
        ("Technique", dataset.technique),
        ("X offset (V)", exp.x_offset),
        ("Y offset (µA)", exp.y_offset),
        ("Data range (points)", f"{exp.range_start} to {exp.range_end} of 0 to {len(dataset) - 1}"),
        ("Smoothing (moving average)",
         f"{exp.smoothing_points} points" if exp.smoothing_points > 1 else "off"),
        ("Shown in the plot", "yes" if exp.visible else "no"),
    ]
    for key, label in (("mV_s", "Scan rate (mV/s)"), ("step_V", "Step (V)"),
                       ("pulse_V", "Pulse (V)")):
        if key in dataset.params:
            details.append((label, dataset.params[key]))
    for row, (key, value) in enumerate(details):
        sheet.write(row, 0, key, bold)
        sheet.write(row, 1, value)

    headers, columns = experiment_rows(exp, dataset)
    top = len(details) + 1
    for col, title in enumerate(headers):
        sheet.write(top, col, title, bold)
    for col, values in enumerate(columns):
        sheet.write_column(top + 1, col, np.asarray(values).tolist())
    sheet.set_column(0, 0, 20)
    sheet.set_column(1, len(headers) - 1, 14)
    sheet.freeze_panes(top + 1, 0)

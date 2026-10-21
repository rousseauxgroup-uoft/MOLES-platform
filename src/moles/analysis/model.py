"""What a Data Analysis plot window holds, and how it is saved.

A loaded file (``datasets.Voltammogram``) is shared and never changes. Each plot
window keeps its own ``ExperimentView`` of every file it shows: the name in the
legend, offsets, colour, line style, data range, and labelled points. Two
windows showing the same file therefore never affect each other or the file.

Labelled points are stored by data point index, not by coordinates, so a label
follows its trace when the offsets change and always reads the trace's
displayed values: smoothed, if the experiment is, and offset.

Everything here converts to and from plain dicts, which is what an analysis
session file stores.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

from .smoothing import smoothed_current

LABEL_PEAK = "peak"
LABEL_SHOULDER = "shoulder"
LABEL_MANUAL = "manual"
LABEL_KINDS = (LABEL_PEAK, LABEL_SHOULDER, LABEL_MANUAL)

EXTREMUM_MAX = "max"
EXTREMUM_MIN = "min"

LINE_STYLES = ("solid", "dash", "dot", "dashdot", "dashdotdot")

# Smallest number of points the data range may hold. Below this a range is
# too short to smooth or to read anything from.
MIN_RANGE_POINTS = 10

# Colours handed to new experiments in turn (same cycle as the Data Preview tabs).
COLOR_CYCLE = (
    "#d62728", "#1f77b4", "#2ca02c", "#9467bd",
    "#ff7f0e", "#17becf", "#8c564b", "#e377c2",
)

SESSION_FORMAT_VERSION = 1

# Where a label's text sits until it is dragged: centred this many points
# (1/72 inch) above a maximum or manual label, or below a minimum.
DEFAULT_LABEL_OFFSET_PT = 14.0

# Label text dragged further than this (points) from its point gets a thin
# line back to it, on screen and in exported figures.
LABEL_LEADER_MIN_PT = 22.0


def _new_uid():
    return uuid.uuid4().hex


@dataclass
class LabelledPoint:
    """One labelled data point of an experiment.

    ``extremum`` is 'max' or 'min' for detected peaks and shoulders (a shoulder's
    is the hidden peak's) and None for manual labels. ``text_offset`` is where
    the user dragged the label's text: the text's centre relative to the
    point, in points (1/72 inch) to the right and up, so it means the same on
    screen and in an exported figure. None places the text automatically.
    """

    index: int
    kind: str
    extremum: Optional[str] = None
    text_offset: Optional[Tuple[float, float]] = None

    def __post_init__(self):
        # Indices often arrive as numpy integers, which a session file cannot hold.
        self.index = int(self.index)
        if self.text_offset is not None:
            self.text_offset = (float(self.text_offset[0]), float(self.text_offset[1]))

    def offset_pt(self):
        """The text's offset from its point, in points (right, up)."""
        if self.text_offset is not None:
            return self.text_offset
        below = self.extremum == EXTREMUM_MIN
        return (0.0, -DEFAULT_LABEL_OFFSET_PT if below else DEFAULT_LABEL_OFFSET_PT)

    def has_leader(self):
        """Whether the text was dragged far enough to need a line back to its point."""
        if self.text_offset is None:
            return False
        return (self.text_offset[0] ** 2 + self.text_offset[1] ** 2) ** 0.5 > LABEL_LEADER_MIN_PT

    def to_dict(self):
        return {"index": self.index, "kind": self.kind, "extremum": self.extremum,
                "text_offset": list(self.text_offset) if self.text_offset else None}

    @classmethod
    def from_dict(cls, d):
        offset = d.get("text_offset")
        return cls(index=int(d["index"]), kind=d["kind"], extremum=d.get("extremum"),
                   text_offset=tuple(float(v) for v in offset) if offset else None)


def format_label(x, y, kind):
    """Label text for a point at displayed coordinates ``(x, y)``.

    Potential to the millivolt, current to a tenth of a microamp: finer
    current digits are below what these measurements resolve.
    """
    text = f"({x:.3f} V, {y:.1f} µA)"
    return "sh " + text if kind == LABEL_SHOULDER else text


@dataclass
class ExperimentView:
    """One file as shown in one plot window.

    ``file`` is the file's path inside the workspace (forward slashes) and
    ``sha256`` its content fingerprint, so the file can be found again if it is
    renamed outside the app. ``range_start`` and ``range_end`` are inclusive
    point indices.
    """

    file: str
    sha256: str
    display_name: str
    n_points: int
    x_offset: float = 0.0  # V
    y_offset: float = 0.0  # µA
    color: str = COLOR_CYCLE[0]
    line_style: str = "solid"
    smoothing_points: int = 1  # moving-average window; 1 = off
    visible: bool = True
    range_start: int = 0
    range_end: int = -1
    labels: List[LabelledPoint] = field(default_factory=list)
    uid: str = field(default_factory=_new_uid)

    def __post_init__(self):
        if self.range_end < 0:
            self.range_end = self.n_points - 1

    @classmethod
    def for_dataset(cls, dataset, file, color=COLOR_CYCLE[0], display_name=None):
        """A fresh view of ``dataset`` showing all of it, named after its file."""
        return cls(file=file, sha256=dataset.sha256,
                   display_name=display_name or dataset.name,
                   n_points=len(dataset), color=color)

    # --- Data range ---

    def set_range(self, start, end):
        """Set the data range, clamped to the data and at least MIN_RANGE_POINTS long.

        Returns the ``(start, end)`` actually applied.
        """
        last = self.n_points - 1
        span = min(MIN_RANGE_POINTS, self.n_points) - 1
        start = int(max(0, min(start, last - span)))
        end = int(min(last, max(end, start + span)))
        self.range_start, self.range_end = start, end
        return start, end

    def in_range(self, index):
        return self.range_start <= index <= self.range_end

    # --- Displayed values ---

    def current(self, dataset):
        """The whole file's current (µA) as this experiment shows it: smoothed, before offsets."""
        return smoothed_current(dataset, self.smoothing_points)

    def displayed_arrays(self, dataset):
        """``(indices, x, y)`` of the points inside the data range, smoothing and offsets applied."""
        sl = slice(self.range_start, self.range_end + 1)
        indices = np.arange(self.range_start, self.range_end + 1)
        return (indices,
                dataset.potential[sl] + self.x_offset,
                self.current(dataset)[sl] + self.y_offset)

    def displayed_point(self, dataset, index):
        """Displayed ``(x, y)`` of one data point."""
        return (float(dataset.potential[index]) + self.x_offset,
                float(self.current(dataset)[index]) + self.y_offset)

    def label_text(self, dataset, label):
        x, y = self.displayed_point(dataset, label.index)
        return format_label(x, y, label.kind)

    # --- Labels ---

    def add_label(self, label):
        """Add a labelled point unless that data point is already labelled.

        Returns True if it was added.
        """
        if not 0 <= label.index < self.n_points:
            raise IndexError(f"Point {label.index} is outside the data (0-{self.n_points - 1}).")
        if any(existing.index == label.index for existing in self.labels):
            return False
        self.labels.append(label)
        self.labels.sort(key=lambda lp: lp.index)
        return True

    def remove_label(self, index):
        """Remove the label on data point ``index``; True if there was one."""
        before = len(self.labels)
        self.labels = [lp for lp in self.labels if lp.index != index]
        return len(self.labels) != before

    def shown_labels(self):
        """Labels inside the data range: the ones drawn and exported.

        Labels outside it are kept, so widening the range brings them back.
        """
        return [lp for lp in self.labels if self.in_range(lp.index)]

    # --- Saving ---

    def to_dict(self):
        return {
            "uid": self.uid, "file": self.file, "sha256": self.sha256,
            "display_name": self.display_name, "n_points": self.n_points,
            "x_offset": self.x_offset, "y_offset": self.y_offset,
            "color": self.color, "line_style": self.line_style,
            "smoothing_points": self.smoothing_points,
            "visible": self.visible,
            "range": [self.range_start, self.range_end],
            "labels": [lp.to_dict() for lp in self.labels],
        }

    @classmethod
    def from_dict(cls, d):
        start, end = d.get("range", [0, -1])
        return cls(
            uid=d.get("uid") or _new_uid(), file=d["file"], sha256=d.get("sha256", ""),
            display_name=d["display_name"], n_points=int(d["n_points"]),
            x_offset=float(d.get("x_offset", 0.0)), y_offset=float(d.get("y_offset", 0.0)),
            color=d.get("color", COLOR_CYCLE[0]),
            line_style=d.get("line_style", "solid"),
            smoothing_points=int(d.get("smoothing_points", 1)),
            visible=bool(d.get("visible", True)),
            range_start=int(start), range_end=int(end),
            labels=[LabelledPoint.from_dict(x) for x in d.get("labels", [])],
        )


@dataclass
class PeakSettings:
    """Peak-picking settings of one plot window.

    ``smoothing_mV`` is the Savitzky-Golay window as a stretch of potential,
    converted to a point count per sweep so it suits any scan rate or step.
    ``prominence_uA`` of None means automatic: ``auto_noise_multiple`` times
    the smoothed noise measured in the box, but never below ``auto_floor_uA``.
    ``report_valleys`` also reports the extrema between two peaks of the
    sweep's own direction (a maximum on a negative-going sweep, a minimum on a
    positive-going one). They are left out by default: a box around two
    neighbouring waves almost always catches the valley between them.
    """

    smoothing_mV: float = 30.0
    polyorder: int = 3
    prominence_uA: Optional[float] = None
    auto_noise_multiple: float = 5.0
    auto_floor_uA: float = 1.0
    report_valleys: bool = False

    def to_dict(self):
        return {"smoothing_mV": self.smoothing_mV, "polyorder": self.polyorder,
                "prominence_uA": self.prominence_uA,
                "auto_noise_multiple": self.auto_noise_multiple,
                "auto_floor_uA": self.auto_floor_uA,
                "report_valleys": self.report_valleys}

    @classmethod
    def from_dict(cls, d):
        defaults = cls()
        prominence = d.get("prominence_uA")
        return cls(
            smoothing_mV=float(d.get("smoothing_mV", defaults.smoothing_mV)),
            polyorder=int(d.get("polyorder", defaults.polyorder)),
            prominence_uA=None if prominence is None else float(prominence),
            auto_noise_multiple=float(d.get("auto_noise_multiple", defaults.auto_noise_multiple)),
            auto_floor_uA=float(d.get("auto_floor_uA", defaults.auto_floor_uA)),
            report_valleys=bool(d.get("report_valleys", defaults.report_valleys)),
        )


@dataclass
class PlotStyle:
    """Text sizes (points) and legend line width (points) of one plot window.

    They apply on screen and in the exported figure alike.
    """

    label_font_pt: float = 9.0
    axis_title_pt: float = 11.0
    tick_font_pt: float = 10.0
    legend_font_pt: float = 10.0
    legend_line_pt: float = 4.0

    def to_dict(self):
        return {"label_font_pt": self.label_font_pt, "axis_title_pt": self.axis_title_pt,
                "tick_font_pt": self.tick_font_pt, "legend_font_pt": self.legend_font_pt,
                "legend_line_pt": self.legend_line_pt}

    @classmethod
    def from_dict(cls, d):
        defaults = cls()
        return cls(**{key: float(d.get(key, getattr(defaults, key)))
                      for key in defaults.to_dict()})


@dataclass
class PlotWindowState:
    """One plot window: its experiments (legend order) and its own settings.

    ``view_range`` is ``(x_min, x_max, y_min, y_max)``, or None to fit the data.
    ``geometry`` is ``(x, y, width, height)`` of a popped-out window.
    ``legend_box`` is where the legend's top-left corner sits, as fractions of
    the plot area's width and height from its top-left corner (None: the
    legend's starting place, top right).
    """

    title: str
    experiments: List[ExperimentView] = field(default_factory=list)
    selected_uid: Optional[str] = None
    view_range: Optional[Tuple[float, float, float, float]] = None
    peak_settings: PeakSettings = field(default_factory=PeakSettings)
    style: PlotStyle = field(default_factory=PlotStyle)
    legend_box: Optional[Tuple[float, float]] = None
    popped_out: bool = False
    geometry: Optional[Tuple[int, int, int, int]] = None
    uid: str = field(default_factory=_new_uid)

    def experiment(self, uid):
        for exp in self.experiments:
            if exp.uid == uid:
                return exp
        return None

    @property
    def selected(self):
        return self.experiment(self.selected_uid) if self.selected_uid else None

    def next_color(self):
        """The first colour of the cycle not yet used here (cycling once all are)."""
        used = {exp.color.lower() for exp in self.experiments}
        for color in COLOR_CYCLE:
            if color not in used:
                return color
        return COLOR_CYCLE[len(self.experiments) % len(COLOR_CYCLE)]

    def unique_name(self, name):
        """``name``, or ``name (2)``, ``name (3)``... if an experiment already uses it."""
        taken = {exp.display_name for exp in self.experiments}
        if name not in taken:
            return name
        n = 2
        while f"{name} ({n})" in taken:
            n += 1
        return f"{name} ({n})"

    def add_experiment(self, dataset, file):
        """Add a view of ``dataset`` with the next free colour and a unique name."""
        exp = ExperimentView.for_dataset(dataset, file, color=self.next_color(),
                                         display_name=self.unique_name(dataset.name))
        self.experiments.append(exp)
        return exp

    def move_experiment(self, uid, step):
        """Move an experiment ``step`` places later (negative: earlier); False if it cannot."""
        index = next((i for i, e in enumerate(self.experiments) if e.uid == uid), None)
        if index is None or not 0 <= index + step < len(self.experiments):
            return False
        self.experiments.insert(index + step, self.experiments.pop(index))
        return True

    def remove_experiment(self, uid):
        self.experiments = [e for e in self.experiments if e.uid != uid]
        if self.selected_uid == uid:
            self.selected_uid = None

    def to_dict(self):
        return {
            "uid": self.uid, "title": self.title,
            "experiments": [e.to_dict() for e in self.experiments],
            "selected_uid": self.selected_uid,
            "view_range": list(self.view_range) if self.view_range else None,
            "peak_settings": self.peak_settings.to_dict(),
            "style": self.style.to_dict(),
            "legend_box": list(self.legend_box) if self.legend_box else None,
            "popped_out": self.popped_out,
            "geometry": list(self.geometry) if self.geometry else None,
        }

    @classmethod
    def from_dict(cls, d):
        view_range = d.get("view_range")
        geometry = d.get("geometry")
        legend_box = d.get("legend_box")
        return cls(
            uid=d.get("uid") or _new_uid(), title=d.get("title", "Plot"),
            experiments=[ExperimentView.from_dict(e) for e in d.get("experiments", [])],
            selected_uid=d.get("selected_uid"),
            view_range=tuple(float(v) for v in view_range) if view_range else None,
            peak_settings=PeakSettings.from_dict(d.get("peak_settings", {})),
            style=PlotStyle.from_dict(d.get("style", {})),
            legend_box=tuple(float(v) for v in legend_box) if legend_box else None,
            popped_out=bool(d.get("popped_out", False)),
            geometry=tuple(int(v) for v in geometry) if geometry else None,
        )


@dataclass
class AnalysisSession:
    """Every plot window of one saved analysis session."""

    windows: List[PlotWindowState] = field(default_factory=list)
    active_window: Optional[str] = None

    def next_title(self):
        """'Plot 1', 'Plot 2', ...: the lowest number no window uses yet."""
        taken = {w.title for w in self.windows}
        n = 1
        while f"Plot {n}" in taken:
            n += 1
        return f"Plot {n}"

    def to_dict(self):
        return {
            "format": "moles-analysis-session",
            "version": SESSION_FORMAT_VERSION,
            "windows": [w.to_dict() for w in self.windows],
            "active_window": self.active_window,
        }

    @classmethod
    def from_dict(cls, d):
        version = int(d.get("version", 1))
        if version > SESSION_FORMAT_VERSION:
            raise ValueError(
                f"This session was saved by a newer MOLES (format {version}); "
                f"this one reads up to format {SESSION_FORMAT_VERSION}.")
        return cls(windows=[PlotWindowState.from_dict(w) for w in d.get("windows", [])],
                   active_window=d.get("active_window"))

    def to_json(self):
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(json.loads(text))

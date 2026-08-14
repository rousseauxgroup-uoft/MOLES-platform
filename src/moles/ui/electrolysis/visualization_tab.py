"""Offline data-visualization workspace for finished electrolysis runs.

Overlays several saved electrolysis traces on one plot so they can be compared
directly. The processing options mirror the standalone matplotlib plotter
(``moles.ui.plots.electrolysis``): choose the x-axis (elapsed time or charge
passed in F/mol), pick which metrics to show (current, voltage, OCP), and
optionally view the first derivative.

Each loaded run gets a row of per-trace controls:

* **Colour** — pick the trace colour (shared by all of that run's metrics).
* **Smoothing** — moving-average window applied to every metric.
* **Data range** — a two-handle slider that plots only a slice of the run.
* **Show / Remove** — toggle visibility or drop the run entirely.

Metrics are drawn in a shared colour per run but with a distinct line style
(current solid, voltage dashed, OCP dotted) so overlapping traces stay legible.

Double-clicking on the plot drops a labelled marker at that point; double-
clicking an existing marker removes it — handy for annotating features.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from ..electroanalysis.range_slider import RangeSlider

# Faraday constant (C/mol), used to convert charge passed into F/mol.
_FARADAY = 96485

# Fallback substrate loading for older files whose CSV has no '# mmol:' line.
_DEFAULT_MMOL = 0.2

# Colours handed out to new traces in turn.
_COLOR_CYCLE = [
    '#d62728', '#1f77b4', '#2ca02c', '#9467bd',
    '#ff7f0e', '#17becf', '#8c564b', '#e377c2',
]

# Metrics this tab knows how to plot, each with a distinct line style so that
# several metrics from one run (same colour) stay distinguishable.
_METRIC_STYLE = {
    "Current (mA)": QtCore.Qt.SolidLine,
    "Voltage (V)": QtCore.Qt.DashLine,
    "OCP (V)": QtCore.Qt.DotLine,
}

# Methods whose data is an oscillating waveform. These render as a max/min
# envelope (two lines + shaded fill) rather than a raw line, because at typical
# durations the oscillations otherwise overlap into a solid block.
_ALTERNATING_METHODS = {"alternating_current", "alternating_polarity"}

# How close (in pixels) a double-click must land to an existing peak label to
# count as "remove this one" rather than "add a new one".
_LABEL_HIT_RADIUS_PX = 16


def _read_metadata(path):
    """Return the '#'-prefixed ``key: value`` metadata lines at the top of the
    CSV as a lower-cased dict. Empty for older files with no metadata header."""
    meta = {}
    try:
        with open(path, 'r') as f:
            for line in f:
                stripped = line.strip()
                if not stripped.startswith('#'):
                    break
                body = stripped[1:].strip()
                if ':' in body:
                    key, value = body.split(':', 1)
                    meta[key.strip().lower()] = value.strip()
    except OSError:
        pass
    return meta


def load_electrolysis(path):
    """Read a saved electrolysis CSV.

    Returns ``(time_s, charge_c, metrics, method, mmol)`` where ``metrics`` maps
    each available metric name to its data array. ``charge_c`` is the cumulative
    charge column if present, else ``None``. ``mmol`` is the substrate loading
    from the file's metadata, or ``None`` for older files that didn't record it.
    Raises ``ValueError`` if the file has no time column or no plottable data.
    """
    # comment='#' skips the metadata lines the live-run writer prepends
    # (see moles.data_io._write_metadata_header).
    df = pd.read_csv(path, comment='#')
    if "Time (s)" not in df.columns:
        raise ValueError("CSV is missing the required 'Time (s)' column.")

    time_s = df["Time (s)"].to_numpy(dtype=float)
    metrics = {}
    for col in _METRIC_STYLE:
        if col in df.columns:
            metrics[col] = df[col].to_numpy(dtype=float)
    if not metrics:
        raise ValueError("CSV has no plottable columns (Current / Voltage / OCP).")

    charge_c = None
    if "Charge Passed (C)" in df.columns:
        charge_c = df["Charge Passed (C)"].to_numpy(dtype=float)

    meta = _read_metadata(path)
    mmol = None
    try:
        if meta.get('mmol'):
            mmol = float(meta['mmol'])
    except ValueError:
        mmol = None

    return time_s, charge_c, metrics, meta.get('method'), mmol


def _envelope_bins(x, y, n_bins):
    """Bin ``x`` into ``n_bins`` equal-width bins and return the per-bin min and
    max of ``y``. Empty bins come back as NaN and are dropped by the caller.

    Uses NumPy's unbuffered scatter-reduce (``np.minimum.at``) so the whole run
    is binned in a single pass, rather than scanning the array once per bin.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    x_edges = np.linspace(np.min(x), np.max(x), n_bins + 1)
    idx = np.clip(np.searchsorted(x_edges, x, side='right') - 1, 0, n_bins - 1)
    x_mid = 0.5 * (x_edges[:-1] + x_edges[1:])

    # Drop NaN samples so they don't poison a bin's min/max.
    finite = ~np.isnan(y)
    idx = idx[finite]
    y = y[finite]

    # Seed with +/-inf so bins that receive no points stay identifiable.
    y_min = np.full(n_bins, np.inf)
    y_max = np.full(n_bins, -np.inf)
    np.minimum.at(y_min, idx, y)
    np.maximum.at(y_max, idx, y)

    untouched = y_min == np.inf
    y_min[untouched] = np.nan
    y_max[untouched] = np.nan
    return x_mid, y_min, y_max


class DatasetRow(QtWidgets.QGroupBox):
    """One loaded electrolysis run plus its per-trace display controls."""

    changed = QtCore.pyqtSignal(int)           # dataset id
    remove_requested = QtCore.pyqtSignal(int)  # dataset id

    def __init__(self, dataset_id, name, time_s, charge_c, metrics, method, mmol, color):
        super().__init__()
        self.dataset_id = dataset_id
        self.name = name
        self.time_s = np.asarray(time_s, dtype=float)
        self.charge_c = None if charge_c is None else np.asarray(charge_c, dtype=float)
        self.metrics = {k: np.asarray(v, dtype=float) for k, v in metrics.items()}
        self.method = method
        self.is_alternating = method in _ALTERNATING_METHODS
        self.color = QtGui.QColor(color)

        # Cache of fully processed (pre-slice) series, keyed by the options that
        # affect them. Dragging the range slider then only re-slices, instead of
        # re-smoothing / re-differentiating the whole (possibly 50k-point) run.
        self._cache = None
        self._cache_key = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)

        # Header: visibility toggle + name + remove.
        header = QtWidgets.QHBoxLayout()
        self.visible_cb = QtWidgets.QCheckBox()
        self.visible_cb.setChecked(True)
        self.visible_cb.setToolTip("Show / hide this run")
        self.visible_cb.stateChanged.connect(self._emit_changed)
        header.addWidget(self.visible_cb)

        name_label = QtWidgets.QLabel(name)
        name_label.setToolTip(name)
        name_label.setStyleSheet("font-weight: bold;")
        header.addWidget(name_label, stretch=1)

        self.remove_btn = QtWidgets.QToolButton()
        self.remove_btn.setText("✕")  # ✕
        self.remove_btn.setToolTip("Remove this run")
        self.remove_btn.clicked.connect(
            lambda: self.remove_requested.emit(self.dataset_id)
        )
        header.addWidget(self.remove_btn)
        layout.addLayout(header)

        # Colour + smoothing on one row.
        row1 = QtWidgets.QHBoxLayout()
        self.color_btn = QtWidgets.QPushButton()
        self.color_btn.setFixedWidth(36)
        self.color_btn.setToolTip("Pick trace colour")
        self.color_btn.clicked.connect(self._pick_color)
        self._refresh_color_button()
        row1.addWidget(self.color_btn)

        row1.addWidget(QtWidgets.QLabel("Smoothing:"))
        self.smooth_spin = QtWidgets.QSpinBox()
        self.smooth_spin.setRange(1, 100)
        self.smooth_spin.setValue(1)
        self.smooth_spin.setSuffix(" pts")
        self.smooth_spin.valueChanged.connect(self._emit_changed)
        row1.addWidget(self.smooth_spin)
        layout.addLayout(row1)

        # Substrate loading for this run — read from the file's metadata when
        # present, and used to scale the Charge (F/mol) axis. Editable so older
        # files (no recorded loading) can still be set correctly.
        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("Scale:"))
        self.mmol_spin = QtWidgets.QDoubleSpinBox()
        self.mmol_spin.setRange(0.0001, 100000.0)
        self.mmol_spin.setDecimals(2)
        self.mmol_spin.setValue(mmol)
        self.mmol_spin.setSuffix(" mmol")
        self.mmol_spin.setToolTip("Substrate loading used to scale this run's charge axis (F/mol).")
        self.mmol_spin.valueChanged.connect(self._emit_changed)
        row2.addWidget(self.mmol_spin)
        layout.addLayout(row2)

        # Data range slider — selects which slice of the run is plotted.
        layout.addWidget(QtWidgets.QLabel("Data range:"))
        self.range_slider = RangeSlider(0, max(1, len(self.time_s) - 1))
        self.range_slider.rangeChanged.connect(lambda lo, hi: self._emit_changed())
        layout.addWidget(self.range_slider)

    # ----- Control callbacks -----

    def _emit_changed(self, *args):
        self.changed.emit(self.dataset_id)

    def _pick_color(self):
        color = QtWidgets.QColorDialog.getColor(self.color, self, "Trace Colour")
        if color.isValid():
            self.color = color
            self._refresh_color_button()
            self._emit_changed()

    def _refresh_color_button(self):
        self.color_btn.setStyleSheet(
            f"background-color: {self.color.name()}; border: 1px solid #888;"
        )

    # ----- Data preparation -----

    def _x_axis(self, x_mode):
        """Return the x-axis array for the requested mode, or ``None`` if charge
        is requested but cannot be computed."""
        if x_mode == "Charge (F/mol)":
            return self._charge_fmol()
        return self.time_s / 60.0  # seconds -> minutes

    def _charge_fmol(self):
        """Charge passed in F/mol at this run's loading, from the saved charge
        column if present, otherwise integrated from the current."""
        mmol = self.mmol_spin.value()
        if mmol <= 0:
            return None
        denom = _FARADAY * (mmol / 1000.0)
        if self.charge_c is not None:
            return self.charge_c / denom
        if "Current (mA)" in self.metrics:
            dt = np.diff(self.time_s, prepend=self.time_s[0])
            charge_c = np.cumsum(self.metrics["Current (mA)"] / 1000.0 * dt)
            return charge_c / denom
        return None

    def rendered_series(self, x_mode, derivative, metrics=None):
        """Return ``{metric: (x, y)}`` for each requested metric after smoothing,
        x-axis selection, optional derivative, and range slicing.

        ``metrics`` limits the result to those metric names (``None`` = all).
        Returns an empty dict when the run is hidden or the x-axis is undefined.
        The heavy per-metric processing is cached, so repeated calls that only
        move the range slider just re-slice the cached arrays.
        """
        if not self.visible_cb.isChecked():
            return {}

        window = self.smooth_spin.value()
        key = (x_mode, round(self.mmol_spin.value(), 6), window, bool(derivative))
        if key != self._cache_key:
            self._cache = self._compute_full(x_mode, derivative, window)
            self._cache_key = key
        if self._cache is None:
            return {}

        low, high = self.range_slider.values()
        sl = slice(low, high + 1)
        out = {}
        for metric, (x_full, y_full) in self._cache.items():
            if metrics is not None and metric not in metrics:
                continue
            out[metric] = (x_full[sl], y_full[sl])
        return out

    def _compute_full(self, x_mode, derivative, window):
        """Process every metric over the full run (no range slice) for the given
        options. Returns ``{metric: (x, y)}``, or ``None`` if the x-axis (e.g.
        charge with no valid loading) cannot be built."""
        x_full = self._x_axis(x_mode)
        if x_full is None:
            return None
        result = {}
        for metric, y_full in self.metrics.items():
            y = y_full
            if window > 1 and len(y) > window:
                kernel = np.ones(window) / window
                y = np.convolve(y, kernel, mode='same')
            x = x_full
            if derivative:
                dx = np.gradient(x)
                dy = np.gradient(y)
                with np.errstate(divide='ignore', invalid='ignore'):
                    y = np.where(dx == 0, np.nan, dy / dx)
            result[metric] = (x, y)
        return result


class VisualizationTab(QtWidgets.QWidget):
    """Tab for overlaying and annotating saved electrolysis runs."""

    def __init__(self):
        super().__init__()
        self._rows = {}        # dataset id -> DatasetRow
        self._items = {}       # dataset id -> flat list of plot items (for removal)
        self._rendered = {}    # dataset id -> {metric: {'kind', 'items'}} for reuse
        self._peak_labels = []  # list of {'x', 'y', 'text', 'marker'}
        self._next_id = 0
        self._color_index = 0
        self._used_names = set()

        # Re-rendering a run is the expensive step, so per-run control changes
        # (range-slider drags, smoothing, etc.) are coalesced: a burst of changes
        # triggers a single redraw after a short pause ("debouncing").
        self._pending = set()
        self._render_timer = QtCore.QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(40)  # ms
        self._render_timer.timeout.connect(self._flush_pending)

        main_layout = QtWidgets.QHBoxLayout(self)

        # --- Left: load button, global display options, dataset rows ---
        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)

        self.add_btn = QtWidgets.QPushButton("Add Files...")
        self.add_btn.clicked.connect(self.add_files)
        left_layout.addWidget(self.add_btn)

        left_layout.addWidget(self._build_options_group())

        hint = QtWidgets.QLabel("Double-click the plot to label a point;\n"
                                "double-click a label to remove it.")
        hint.setStyleSheet("color: gray; font-size: 11px;")
        left_layout.addWidget(hint)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        self._row_container = QtWidgets.QWidget()
        self._row_layout = QtWidgets.QVBoxLayout(self._row_container)
        self._row_layout.addStretch()
        scroll.setWidget(self._row_container)
        left_layout.addWidget(scroll, stretch=1)

        # --- Right: overlay plot ---
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('w')
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.addLegend()
        # For long runs, draw at most ~one min/max pair per horizontal pixel and
        # only render the visible x-range. This cuts the points drawn per curve
        # from tens of thousands to a few hundred without changing the shape.
        self.plot_widget.setDownsampling(auto=True, mode='peak')
        self.plot_widget.setClipToView(True)
        self.plot_widget.scene().sigMouseClicked.connect(self._on_plot_clicked)
        self._refresh_axes()

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(left_panel)
        splitter.addWidget(self.plot_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([380, 820])
        main_layout.addWidget(splitter)

    # ------------------------------------------------------------------
    # Global display options
    # ------------------------------------------------------------------

    def _build_options_group(self):
        group = QtWidgets.QGroupBox("Display Options")
        form = QtWidgets.QFormLayout(group)

        # X-axis mode. The loading used to scale the charge axis is per-run
        # (set on each dataset's row), read from that run's CSV metadata.
        self.x_mode_combo = QtWidgets.QComboBox()
        self.x_mode_combo.addItems(["Time (min)", "Charge (F/mol)"])
        self.x_mode_combo.currentTextChanged.connect(self._on_x_mode_changed)
        form.addRow("X-axis:", self.x_mode_combo)

        # Metric checkboxes.
        self.metric_cbs = {}
        for metric in _METRIC_STYLE:
            cb = QtWidgets.QCheckBox(metric)
            cb.setChecked(True)
            cb.stateChanged.connect(lambda _s: self._render_all())
            form.addRow(cb)
            self.metric_cbs[metric] = cb

        # First-derivative toggle.
        self.derivative_cb = QtWidgets.QCheckBox("1st Derivative")
        self.derivative_cb.stateChanged.connect(self._on_derivative_changed)
        form.addRow(self.derivative_cb)

        return group

    def _on_x_mode_changed(self, _mode):
        self._refresh_axes()
        self._render_all()

    def _on_derivative_changed(self, _state):
        self._refresh_axes()
        self._render_all()

    def _refresh_axes(self):
        """Update the x- and y-axis labels to match the current options."""
        x_mode = self.x_mode_combo.currentText()
        x_label = x_mode  # "Time (min)" or "Charge (F/mol)"
        if self.derivative_cb.isChecked():
            unit = x_mode.split(' ')[0]
            y_label = f"d(value) / d({unit})"
        else:
            y_label = "Current (mA) / Voltage (V)"
        self.plot_widget.setLabel('bottom', x_label)
        self.plot_widget.setLabel('left', y_label)

    # ------------------------------------------------------------------
    # Loading datasets
    # ------------------------------------------------------------------

    def add_files(self):
        """Prompt for CSV files and add each as a new run."""
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Select electrolysis CSV file(s)", "", "CSV files (*.csv)"
        )
        for path in paths:
            self.add_dataset_from_file(path)

    def add_dataset_from_file(self, path):
        """Load a CSV and add it as a run, reporting failures inline."""
        try:
            time_s, charge_c, metrics, method, mmol = load_electrolysis(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self, "Could Not Load File",
                f"Failed to load {Path(path).name}:\n{e}",
            )
            return
        self.add_dataset(Path(path).stem, time_s, charge_c, metrics, method, mmol)

    def add_dataset(self, name, time_s, charge_c, metrics, method=None, mmol=None):
        """Create a run, its control row, and its plot curves.

        ``mmol`` is this run's substrate loading (used for the F/mol axis);
        ``None`` falls back to ``_DEFAULT_MMOL`` for older files that didn't
        record it.
        """
        dataset_id = self._next_id
        self._next_id += 1
        color = _COLOR_CYCLE[self._color_index % len(_COLOR_CYCLE)]
        self._color_index += 1
        if not mmol or mmol <= 0:
            mmol = _DEFAULT_MMOL

        display_name = self._unique_name(name)
        row = DatasetRow(
            dataset_id, display_name, time_s, charge_c, metrics, method, mmol, color
        )
        row.changed.connect(self._schedule_render)
        row.remove_requested.connect(self._remove_dataset)
        self._row_layout.insertWidget(self._row_layout.count() - 1, row)
        self._rows[dataset_id] = row
        self._items[dataset_id] = []
        self._rendered[dataset_id] = {}

        self._render_dataset(dataset_id)

    def _unique_name(self, name):
        """Return a display name that isn't already in use, suffixing if needed."""
        candidate = name
        counter = 1
        while candidate in self._used_names:
            candidate = f"{name}_{counter}"
            counter += 1
        self._used_names.add(candidate)
        return candidate

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_all(self):
        self._refresh_axes()
        for dataset_id in list(self._rows):
            self._render_dataset(dataset_id)

    def _schedule_render(self, dataset_id):
        """Queue one run for redraw, coalescing bursts of control changes."""
        self._pending.add(dataset_id)
        self._render_timer.start()  # restarts the short countdown

    def _flush_pending(self):
        """Redraw every run queued since the last flush."""
        pending, self._pending = self._pending, set()
        for dataset_id in pending:
            if dataset_id in self._rows:
                self._render_dataset(dataset_id)

    def _render_dataset(self, dataset_id):
        """Re-draw one run from its row's controls and the global options.

        Curves are reused in place (via ``setData``) wherever possible — only
        metrics that were switched off, or that change between line and envelope
        form, are torn down and rebuilt. Reusing curves avoids allocating new
        plot items (and churning the legend) on every control change.
        """
        row = self._rows.get(dataset_id)
        if row is None:
            return

        x_mode = self.x_mode_combo.currentText()
        derivative = self.derivative_cb.isChecked()
        enabled = {m for m, cb in self.metric_cbs.items() if cb.isChecked()}
        series = row.rendered_series(x_mode, derivative, enabled)

        # Envelope only makes sense for the raw oscillating signal, so it
        # defers to a plain line when the derivative view is on.
        use_envelope = row.is_alternating and not derivative
        rendered = self._rendered.setdefault(dataset_id, {})

        # Drop curves for metrics that are no longer shown (hidden or filtered).
        for metric in list(rendered):
            if metric not in series:
                self._discard_metric(dataset_id, metric)

        for metric, (x, y) in series.items():
            kind = 'envelope' if use_envelope else 'line'
            existing = rendered.get(metric)
            # A line<->envelope switch can't be done in place, so rebuild it.
            if existing is not None and existing['kind'] != kind:
                self._discard_metric(dataset_id, metric)
                existing = None
            if kind == 'line':
                self._draw_line(dataset_id, row, metric, x, y, existing)
            else:
                self._draw_envelope(dataset_id, row, metric, x, y, existing)

        # Rebuild the flat item list used for removal and introspection.
        self._items[dataset_id] = [
            item for entry in rendered.values() for item in entry['items']
        ]

    def _draw_line(self, dataset_id, row, metric, x, y, existing):
        """Create or update a single line curve for one metric."""
        pen = pg.mkPen(color=row.color, width=4, style=_METRIC_STYLE[metric])
        if existing is not None:
            curve = existing['items'][0]
            curve.setData(x, y, connect='finite')
            curve.setPen(pen)
        else:
            curve = self.plot_widget.plot(
                x, y, pen=pen, name=f"{row.name} - {metric}", connect='finite'
            )
            self._rendered[dataset_id][metric] = {'kind': 'line', 'items': [curve]}

    def _draw_envelope(self, dataset_id, row, metric, x, y, existing):
        """Create or update a shaded min/max envelope for one oscillating metric."""
        # Bin count scales with data length so short runs still resolve and
        # long runs stay smooth.
        n_bins = min(500, max(30, len(x) // 200))
        x_mid, y_min, y_max = _envelope_bins(x, y, n_bins)
        valid = ~(np.isnan(y_min) | np.isnan(y_max))
        if not valid.any():
            # Nothing drawable; drop any stale envelope for this metric.
            if existing is not None:
                self._discard_metric(dataset_id, metric)
            return
        x_plot, y_lo, y_hi = x_mid[valid], y_min[valid], y_max[valid]

        pen = pg.mkPen(color=row.color, width=4, style=_METRIC_STYLE[metric])
        c = row.color
        brush = pg.mkBrush(c.red(), c.green(), c.blue(), 40)
        if existing is not None:
            curve_hi, curve_lo, fill = existing['items']
            curve_hi.setData(x_plot, y_hi)
            curve_hi.setPen(pen)
            curve_lo.setData(x_plot, y_lo)
            curve_lo.setPen(pen)
            fill.setBrush(brush)
        else:
            curve_hi = self.plot_widget.plot(
                x_plot, y_hi, pen=pen, name=f"{row.name} - {metric}"
            )
            curve_lo = self.plot_widget.plot(x_plot, y_lo, pen=pen)
            fill = pg.FillBetweenItem(curve_hi, curve_lo, brush=brush)
            self.plot_widget.addItem(fill)
            self._rendered[dataset_id][metric] = {
                'kind': 'envelope', 'items': [curve_hi, curve_lo, fill]
            }

    def _discard_metric(self, dataset_id, metric):
        """Remove the plot item(s) currently drawn for one metric of a run."""
        entry = self._rendered.get(dataset_id, {}).pop(metric, None)
        if entry is None:
            return
        for item in entry['items']:
            self.plot_widget.removeItem(item)

    def _remove_dataset(self, dataset_id):
        """Drop a run, its control row, and its curves."""
        row = self._rows.pop(dataset_id, None)
        if row is not None:
            self._used_names.discard(row.name)
            row.setParent(None)
        self._pending.discard(dataset_id)
        for item in self._items.pop(dataset_id, []):
            self.plot_widget.removeItem(item)
        self._rendered.pop(dataset_id, None)

    # ------------------------------------------------------------------
    # Peak / feature labelling
    # ------------------------------------------------------------------

    def _on_plot_clicked(self, event):
        """Add a label on double-click, or remove one if clicked again."""
        if not event.double():
            return
        vb = self.plot_widget.plotItem.vb
        scene_pos = event.scenePos()
        if not self.plot_widget.sceneBoundingRect().contains(scene_pos):
            return

        # If the click landed on an existing label, remove that label instead.
        for entry in self._peak_labels:
            anchor_scene = vb.mapViewToScene(QtCore.QPointF(entry['x'], entry['y']))
            dx = anchor_scene.x() - scene_pos.x()
            dy = anchor_scene.y() - scene_pos.y()
            if (dx * dx + dy * dy) ** 0.5 <= _LABEL_HIT_RADIUS_PX:
                self.plot_widget.removeItem(entry['text'])
                self.plot_widget.removeItem(entry['marker'])
                self._peak_labels.remove(entry)
                return

        data_pt = vb.mapSceneToView(scene_pos)
        self._add_peak_label(data_pt.x(), data_pt.y())

    def _add_peak_label(self, x, y):
        """Place a marker and coordinate label at a point on the plot."""
        marker = pg.ScatterPlotItem(
            [x], [y], size=11, symbol='x',
            pen=pg.mkPen('k', width=2), brush=pg.mkBrush('k'),
        )
        self.plot_widget.addItem(marker)

        text = pg.TextItem(f"({x:.3f}, {y:.3f})", color='k', anchor=(0, 1))
        text.setPos(x, y)
        self.plot_widget.addItem(text)

        self._peak_labels.append({'x': x, 'y': y, 'text': text, 'marker': marker})

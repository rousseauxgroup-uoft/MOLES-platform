"""Offline analysis workspace for comparing saved electroanalysis traces.

The Analysis tab overlays several loaded traces so they can be compared
directly. Two kinds of trace are handled, on two separate plots, because they
are read against different axes: voltammograms (current against potential, from
CV and DPV) and OCP traces (resting potential against time). A plot appears
only once something of its kind has been loaded.

Each trace gets a row of controls, whichever kind it is:

* **Offset (V)** — shift the potential axis, e.g. to reference against an
  internal standard such as ferrocene. Potential is the x-axis of a
  voltammogram and the y-axis of an OCP trace, so the offset follows it.
* **Colour** — pick the trace colour.
* **Smoothing** — moving-average window applied to the measured signal (the
  current of a voltammogram, the potential of an OCP trace).
* **Data range** — a two-handle slider that plots only a slice of the trace.
* **Show / Remove** — toggle visibility or drop the trace entirely.
* **Save edited CSV** — write the currently displayed data (selected range,
  applied offset, and smoothing) to a new CSV for import into Excel, alongside
  a small ``.edits.txt`` note recording those settings. The original file is
  left untouched and the new file is labelled ``_edited``.

Double-clicking on either plot drops a labelled marker at that point; double-
clicking an existing marker removes it.
"""

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from .loader import KIND_OCP, KIND_VOLTAMMOGRAM, load_trace
from .range_slider import RangeSlider

# Colours handed out to new traces in turn.
_COLOR_CYCLE = [
    '#d62728', '#1f77b4', '#2ca02c', '#9467bd',
    '#ff7f0e', '#17becf', '#8c564b', '#e377c2',
]

# How close (in pixels) a double-click must land to an existing peak label to
# count as "remove this one" rather than "add a new one".
_LABEL_HIT_RADIUS_PX = 16


class DatasetRow(QtWidgets.QGroupBox):
    """One loaded trace plus its display controls.

    ``x``/``y`` are whatever the trace's kind plots: potential and current for
    a voltammogram, time and potential for an OCP trace. The labels are the
    column names they came in with, reused when an edited copy is exported.
    """

    changed = QtCore.pyqtSignal(int)           # dataset id
    remove_requested = QtCore.pyqtSignal(int)  # dataset id

    def __init__(self, dataset_id, name, x, y, color,
                 y_label='Current (uA)', x_label='Potential (V)',
                 kind=KIND_VOLTAMMOGRAM, source_path=None):
        super().__init__()
        self.dataset_id = dataset_id
        self.name = name
        self.x = np.asarray(x, dtype=float)
        self.y = np.asarray(y, dtype=float)
        self.color = QtGui.QColor(color)
        # Column headers and the file this trace came from — all used when
        # exporting an edited copy of the data.
        self.x_label = x_label
        self.y_label = y_label
        self.kind = kind
        self.source_path = source_path

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)

        # Header: visibility toggle + name + remove.
        header = QtWidgets.QHBoxLayout()
        self.visible_cb = QtWidgets.QCheckBox()
        self.visible_cb.setChecked(True)
        self.visible_cb.setToolTip("Show / hide this trace")
        self.visible_cb.stateChanged.connect(self._emit_changed)
        header.addWidget(self.visible_cb)

        name_label = QtWidgets.QLabel(name)
        name_label.setToolTip(name)
        name_label.setStyleSheet("font-weight: bold;")
        header.addWidget(name_label, stretch=1)

        self.remove_btn = QtWidgets.QToolButton()
        self.remove_btn.setText("✕")  # ✕
        self.remove_btn.setToolTip("Remove this trace")
        self.remove_btn.clicked.connect(
            lambda: self.remove_requested.emit(self.dataset_id)
        )
        header.addWidget(self.remove_btn)
        layout.addLayout(header)

        # Offset + colour on one row.
        row1 = QtWidgets.QHBoxLayout()
        row1.addWidget(QtWidgets.QLabel("Offset (V):"))
        self.offset_spin = QtWidgets.QDoubleSpinBox()
        self.offset_spin.setRange(-10, 10)
        self.offset_spin.setSingleStep(0.01)
        self.offset_spin.setDecimals(3)
        self.offset_spin.setToolTip(
            "Shift the potential axis, e.g. to reference against an internal "
            "standard."
        )
        self.offset_spin.valueChanged.connect(self._emit_changed)
        row1.addWidget(self.offset_spin)

        self.color_btn = QtWidgets.QPushButton()
        self.color_btn.setFixedWidth(36)
        self.color_btn.setToolTip("Pick trace colour")
        self.color_btn.clicked.connect(self._pick_color)
        self._refresh_color_button()
        row1.addWidget(self.color_btn)
        layout.addLayout(row1)

        # Smoothing.
        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("Smoothing:"))
        self.smooth_spin = QtWidgets.QSpinBox()
        self.smooth_spin.setRange(1, 100)
        self.smooth_spin.setValue(1)
        self.smooth_spin.setSuffix(" pts")
        self.smooth_spin.valueChanged.connect(self._emit_changed)
        row2.addWidget(self.smooth_spin)
        layout.addLayout(row2)

        # Data range slider.
        layout.addWidget(QtWidgets.QLabel("Data range:"))
        self.range_slider = RangeSlider(0, max(1, len(self.x) - 1))
        self.range_slider.rangeChanged.connect(lambda lo, hi: self._emit_changed())
        layout.addWidget(self.range_slider)

        # Export the currently displayed data to a new CSV.
        self.save_btn = QtWidgets.QPushButton("Save Edited CSV…")
        self.save_btn.setToolTip(
            "Save the displayed range (with offset and smoothing applied) to a "
            "new CSV. The original file is left untouched."
        )
        self.save_btn.clicked.connect(self._save_edited_csv)
        layout.addWidget(self.save_btn)

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

    def _potential_is_y(self):
        """True when potential is this trace's y-axis (an OCP time series)."""
        return self.kind == KIND_OCP

    def _edited_arrays(self):
        """Return the ``(x, y)`` arrays exactly as drawn on the plot — offset,
        smoothing, and the selected data range all applied. Visibility is
        ignored so a hidden trace can still be exported.

        The offset references the potential axis, which is x on a voltammogram
        and y on an OCP trace; smoothing always applies to the measured signal
        on y.
        """
        x, y = self.x, self.y
        offset = self.offset_spin.value()
        if self._potential_is_y():
            y = y - offset
        else:
            x = x - offset

        window = self.smooth_spin.value()
        if window > 1 and len(y) > window:
            kernel = np.ones(window) / window
            y = np.convolve(y, kernel, mode='same')

        low, high = self.range_slider.values()
        sl = slice(low, high + 1)
        return x[sl], y[sl]

    def rendered_data(self):
        """Return the ``(x, y)`` arrays to plot, after applying offset,
        smoothing, and the selected data range. Returns ``None`` when the
        trace is hidden."""
        if not self.visible_cb.isChecked():
            return None
        return self._edited_arrays()

    def write_edited_csv(self, path):
        """Write the currently displayed data to ``path`` as a two-column CSV
        and return the number of points written. A companion ``.edits.txt``
        note recording the applied offset, smoothing, and range is written
        beside it so the export can be traced back to the original run.

        Raises ``ValueError`` if the selected range is empty or if ``path`` is
        the file this trace was loaded from (the original must stay untouched).
        """
        x, y = self._edited_arrays()
        if len(x) == 0:
            raise ValueError("The selected data range is empty.")
        if self.source_path is not None and \
                Path(path).resolve() == Path(self.source_path).resolve():
            raise ValueError("Refusing to overwrite the original file.")
        np.savetxt(
            path, np.column_stack([x, y]), delimiter=",",
            header=f"{self.x_label},{self.y_label}", comments='', fmt='%.5f',
        )
        self._write_edit_notes(Path(path).with_suffix('.edits.txt'), x, y)
        return len(x)

    def _write_edit_notes(self, notes_path, exported_x, exported_y):
        """Write a plain-text note describing which edits produced the CSV:
        the source file, offset, smoothing window, and selected point range."""
        low, high = self.range_slider.values()
        last_index = max(0, len(self.x) - 1)
        source = Path(self.source_path).name if self.source_path else "(unsaved trace)"
        potential = exported_y if self._potential_is_y() else exported_x
        lines = [
            f"Edited copy of: {source}",
            f"Trace name: {self.name}",
            f"Offset applied (V): {self.offset_spin.value():.3f}",
            f"Smoothing (moving-average window, points): {self.smooth_spin.value()}",
            f"Selected point range (indices): {low} to {high} of 0 to {last_index}",
            f"Points exported: {len(exported_x)}",
            "Potential window after offset (V): "
            f"{potential.min():.3f} to {potential.max():.3f}",
        ]
        with open(notes_path, 'w') as f:
            f.write("\n".join(lines) + "\n")

    def _save_edited_csv(self):
        """Prompt for a filename and save the displayed data, defaulting to
        ``<original name>_edited.csv`` beside the source file."""
        if self.source_path is not None:
            src = Path(self.source_path)
            suggested = src.with_name(src.stem + "_edited.csv")
        else:
            suggested = Path(self.name + "_edited.csv")

        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Edited CSV", str(suggested), "CSV files (*.csv)"
        )
        if not path:
            return

        try:
            count = self.write_edited_csv(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self, "Could Not Save File",
                f"Failed to save {Path(path).name}:\n{e}",
            )
            return

        notes_name = Path(path).with_suffix('.edits.txt').name
        QtWidgets.QMessageBox.information(
            self, "Saved",
            f"Saved {count} points to {Path(path).name}.\n"
            f"Edit details written to {notes_name}.",
        )


class AnalysisTab(QtWidgets.QWidget):
    """Tab for overlaying and annotating saved traces of either kind."""

    def __init__(self):
        super().__init__()
        self._rows = {}          # dataset id → DatasetRow
        self._curves = {}        # dataset id → PlotDataItem
        self._curve_plots = {}   # dataset id → the PlotWidget it was drawn on
        self._peak_labels = []   # list of {'x', 'y', 'text', 'marker', 'plot'}
        self._next_id = 0
        self._color_index = 0

        # Coalesce rapid control changes (e.g. range-slider drags) into a single
        # redraw after a short pause, instead of redrawing on every event.
        self._pending = set()
        self._render_timer = QtCore.QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(40)  # ms
        self._render_timer.timeout.connect(self._flush_pending)

        main_layout = QtWidgets.QHBoxLayout(self)

        # --- Left: load button + scrollable dataset rows ---
        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)

        self.add_btn = QtWidgets.QPushButton("Add Files...")
        self.add_btn.clicked.connect(self.add_files)
        left_layout.addWidget(self.add_btn)

        hint = QtWidgets.QLabel("Double-click a plot to label a point;\n"
                                "double-click a label to remove it.")
        hint.setStyleSheet("color: gray; font-size: 11px;")
        left_layout.addWidget(hint)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        self._row_container = QtWidgets.QWidget()
        self._row_layout = QtWidgets.QVBoxLayout(self._row_container)
        self._row_layout.addStretch()
        scroll.setWidget(self._row_container)
        left_layout.addWidget(scroll)

        # --- Right: one overlay plot per kind of trace ---
        # Current-vs-potential and potential-vs-time cannot share axes, so they
        # get a plot each, stacked. Each is shown only once a trace of its kind
        # is loaded; with nothing loaded the voltammogram plot stands as the
        # empty state.
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('w')
        self.plot_widget.setLabel('left', 'Current', units='uA')
        self.plot_widget.setLabel('bottom', 'Potential', units='V')
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.addLegend()
        # NOTE: pyqtgraph's clip-to-view and auto-downsampling both assume the
        # x-axis increases uniformly. That holds for time-series but NOT for a
        # voltammogram, whose x-axis (potential) sweeps down and back up and
        # returns to its start. On such data the auto-downsample factor is
        # computed from the average x-spacing, which collapses to ~zero and can
        # silently drop the entire trace. CV traces are small, so we render them
        # at full resolution instead.

        self.ocp_plot_widget = pg.PlotWidget()
        self.ocp_plot_widget.setBackground('w')
        self.ocp_plot_widget.setLabel('left', 'Potential', units='V')
        # The time unit lives in the label text rather than being passed as an
        # axis unit: pyqtgraph SI-rescales a unit-bearing axis, which would
        # report an hour-long run as "3.6 ks" instead of counting seconds.
        self.ocp_plot_widget.setLabel('bottom', 'Time (s)')
        self.ocp_plot_widget.showGrid(x=True, y=True)
        self.ocp_plot_widget.addLegend()
        # Safe here, unlike above: an OCP trace's x-axis is elapsed time, which
        # does increase uniformly, so hour-long runs can be thinned for display.
        self.ocp_plot_widget.setDownsampling(auto=True, mode='peak')
        self.ocp_plot_widget.setClipToView(True)
        self.ocp_plot_widget.setVisible(False)

        for widget in (self.plot_widget, self.ocp_plot_widget):
            widget.scene().sigMouseClicked.connect(
                lambda event, w=widget: self._on_plot_clicked(event, w)
            )

        plot_panel = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        plot_panel.addWidget(self.plot_widget)
        plot_panel.addWidget(self.ocp_plot_widget)

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(left_panel)
        splitter.addWidget(plot_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([380, 820])
        main_layout.addWidget(splitter)

    # ------------------------------------------------------------------
    # Loading datasets
    # ------------------------------------------------------------------

    def add_files(self):
        """Prompt for CSV files and add each as a new trace."""
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Select electroanalysis CSV file(s)", "", "CSV files (*.csv)"
        )
        for path in paths:
            self.add_dataset_from_file(path)

    def add_dataset_from_file(self, path):
        """Load a CSV and add it as a trace, reporting failures inline."""
        try:
            trace = load_trace(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self, "Could Not Load File",
                f"Failed to load {Path(path).name}:\n{e}",
            )
            return
        self.add_dataset(Path(path).stem, trace.x, trace.y, trace.y_label,
                         x_label=trace.x_label, kind=trace.kind,
                         source_path=path)

    def add_dataset(self, name, x, y, y_label='Current (uA)',
                    x_label='Potential (V)', kind=KIND_VOLTAMMOGRAM,
                    source_path=None):
        """Create a trace, its control row, and its plot curve.

        The curve goes on the plot for its ``kind``, which is revealed if this
        is the first trace of that kind.
        """
        dataset_id = self._next_id
        self._next_id += 1
        color = _COLOR_CYCLE[self._color_index % len(_COLOR_CYCLE)]
        self._color_index += 1

        row = DatasetRow(dataset_id, name, x, y, color, y_label=y_label,
                         x_label=x_label, kind=kind, source_path=source_path)
        row.changed.connect(self._schedule_render)
        row.remove_requested.connect(self._remove_dataset)
        self._row_layout.insertWidget(self._row_layout.count() - 1, row)
        self._rows[dataset_id] = row

        plot = self._plot_for(kind)
        curve = plot.plot([], [], name=name)
        self._curves[dataset_id] = curve
        self._curve_plots[dataset_id] = plot

        self._update_plot_visibility()
        self._render_dataset(dataset_id)

    def _plot_for(self, kind):
        """Return the plot widget a trace of this kind belongs on."""
        return self.ocp_plot_widget if kind == KIND_OCP else self.plot_widget

    def _update_plot_visibility(self):
        """Show each plot only while it has traces on it.

        With nothing loaded at all the voltammogram plot stays up as the empty
        state, so the tab never shows a blank panel.
        """
        kinds = {row.kind for row in self._rows.values()}
        has_ocp = KIND_OCP in kinds
        has_voltammogram = KIND_VOLTAMMOGRAM in kinds
        self.ocp_plot_widget.setVisible(has_ocp)
        self.plot_widget.setVisible(has_voltammogram or not has_ocp)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _schedule_render(self, dataset_id):
        """Queue one trace for redraw, coalescing bursts of control changes."""
        self._pending.add(dataset_id)
        self._render_timer.start()  # restarts the short countdown

    def _flush_pending(self):
        """Redraw every trace queued since the last flush."""
        pending, self._pending = self._pending, set()
        for dataset_id in pending:
            if dataset_id in self._rows:
                self._render_dataset(dataset_id)

    def _render_dataset(self, dataset_id):
        """Re-draw one trace from its row's current control values."""
        row = self._rows.get(dataset_id)
        curve = self._curves.get(dataset_id)
        if row is None or curve is None:
            return

        rendered = row.rendered_data()
        if rendered is None:
            curve.setData([], [])
            return

        x, y = rendered
        curve.setData(x, y)
        curve.setPen(pg.mkPen(color=row.color, width=2))

    def _remove_dataset(self, dataset_id):
        """Drop a trace, its control row, and its curve."""
        self._pending.discard(dataset_id)
        row = self._rows.pop(dataset_id, None)
        if row is not None:
            row.setParent(None)
        curve = self._curves.pop(dataset_id, None)
        plot = self._curve_plots.pop(dataset_id, self.plot_widget)
        if curve is not None:
            plot.removeItem(curve)
        # Removing the last trace of a kind puts that plot away again.
        self._update_plot_visibility()

    # ------------------------------------------------------------------
    # Peak labelling
    # ------------------------------------------------------------------

    def _on_plot_clicked(self, event, plot):
        """Add a label on double-click, or remove one if clicked again.

        ``plot`` is the widget whose scene emitted the click, so a label only
        ever matches against the markers on that same plot.
        """
        if not event.double():
            return
        vb = plot.plotItem.vb
        scene_pos = event.scenePos()
        if not plot.sceneBoundingRect().contains(scene_pos):
            return

        # If the click landed on an existing label, remove that label instead.
        for entry in self._peak_labels:
            if entry['plot'] is not plot:
                continue
            anchor_scene = vb.mapViewToScene(
                QtCore.QPointF(entry['x'], entry['y'])
            )
            dx = anchor_scene.x() - scene_pos.x()
            dy = anchor_scene.y() - scene_pos.y()
            if (dx * dx + dy * dy) ** 0.5 <= _LABEL_HIT_RADIUS_PX:
                plot.removeItem(entry['text'])
                plot.removeItem(entry['marker'])
                self._peak_labels.remove(entry)
                return

        data_pt = vb.mapSceneToView(scene_pos)
        self._add_peak_label(data_pt.x(), data_pt.y(), plot)

    def _add_peak_label(self, x, y, plot=None):
        """Place a marker and coordinate label at a point on a plot."""
        if plot is None:
            plot = self.plot_widget
        marker = pg.ScatterPlotItem(
            [x], [y], size=11, symbol='x',
            pen=pg.mkPen('k', width=2), brush=pg.mkBrush('k'),
        )
        plot.addItem(marker)

        text = pg.TextItem(f"({x:.3f}, {y:.3f})", color='k', anchor=(0, 1))
        text.setPos(x, y)
        plot.addItem(text)

        self._peak_labels.append(
            {'x': x, 'y': y, 'text': text, 'marker': marker, 'plot': plot})

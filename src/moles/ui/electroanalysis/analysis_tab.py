"""Offline analysis workspace for comparing saved voltammograms.

The Analysis tab overlays several loaded traces on one plot so they can be
compared directly. Each trace gets a row of controls:

* **Offset (V)** — shift the potential axis, e.g. to reference against an
  internal standard such as ferrocene.
* **Colour** — pick the trace colour.
* **Smoothing** — moving-average window applied to the current.
* **Data range** — a two-handle slider that plots only a slice of the trace.
* **Show / Remove** — toggle visibility or drop the trace entirely.
* **Save edited CSV** — write the currently displayed data (selected range,
  applied offset, and smoothing) to a new CSV for import into Excel, alongside
  a small ``.edits.txt`` note recording those settings. The original file is
  left untouched and the new file is labelled ``_edited``.

Double-clicking on the plot drops a labelled marker at that point; double-
clicking an existing marker removes it — handy for annotating peak positions.
"""

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from .loader import load_voltammogram
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
    """One loaded trace plus its display controls."""

    changed = QtCore.pyqtSignal(int)           # dataset id
    remove_requested = QtCore.pyqtSignal(int)  # dataset id

    def __init__(self, dataset_id, name, potential, current, color,
                 label='Current (uA)', source_path=None):
        super().__init__()
        self.dataset_id = dataset_id
        self.name = name
        self.potential = np.asarray(potential, dtype=float)
        self.current = np.asarray(current, dtype=float)
        self.color = QtGui.QColor(color)
        # Column header for the current values and the file this trace came
        # from — both used when exporting an edited copy of the data.
        self.label = label
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
        self.range_slider = RangeSlider(0, max(1, len(self.potential) - 1))
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

    def _edited_arrays(self):
        """Return the ``(potential, current)`` arrays exactly as drawn on the
        plot — offset, smoothing, and the selected data range all applied.
        Visibility is ignored so a hidden trace can still be exported."""
        potential = self.potential - self.offset_spin.value()
        current = self.current

        window = self.smooth_spin.value()
        if window > 1 and len(current) > window:
            kernel = np.ones(window) / window
            current = np.convolve(current, kernel, mode='same')

        low, high = self.range_slider.values()
        sl = slice(low, high + 1)
        return potential[sl], current[sl]

    def rendered_data(self):
        """Return the ``(potential, current)`` arrays to plot, after applying
        offset, smoothing, and the selected data range. Returns ``None`` when
        the trace is hidden."""
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
        potential, current = self._edited_arrays()
        if len(potential) == 0:
            raise ValueError("The selected data range is empty.")
        if self.source_path is not None and \
                Path(path).resolve() == Path(self.source_path).resolve():
            raise ValueError("Refusing to overwrite the original file.")
        np.savetxt(
            path, np.column_stack([potential, current]), delimiter=",",
            header=f"Potential (V),{self.label}", comments='', fmt='%.5f',
        )
        self._write_edit_notes(Path(path).with_suffix('.edits.txt'), potential)
        return len(potential)

    def _write_edit_notes(self, notes_path, exported_potential):
        """Write a plain-text note describing which edits produced the CSV:
        the source file, offset, smoothing window, and selected point range."""
        low, high = self.range_slider.values()
        last_index = max(0, len(self.potential) - 1)
        source = Path(self.source_path).name if self.source_path else "(unsaved trace)"
        lines = [
            f"Edited copy of: {source}",
            f"Trace name: {self.name}",
            f"Offset applied (V): {self.offset_spin.value():.3f}",
            f"Smoothing (moving-average window, points): {self.smooth_spin.value()}",
            f"Selected point range (indices): {low} to {high} of 0 to {last_index}",
            f"Points exported: {len(exported_potential)}",
            "Potential window after offset (V): "
            f"{exported_potential.min():.3f} to {exported_potential.max():.3f}",
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
    """Tab for overlaying and annotating saved voltammograms."""

    def __init__(self):
        super().__init__()
        self._rows = {}          # dataset id → DatasetRow
        self._curves = {}        # dataset id → PlotDataItem
        self._peak_labels = []   # list of {'x', 'y', 'text', 'marker'}
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

        hint = QtWidgets.QLabel("Double-click the plot to label a peak;\n"
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

        # --- Right: overlay plot ---
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
        self.plot_widget.scene().sigMouseClicked.connect(self._on_plot_clicked)

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(left_panel)
        splitter.addWidget(self.plot_widget)
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
            self, "Select voltammogram CSV file(s)", "", "CSV files (*.csv)"
        )
        for path in paths:
            self.add_dataset_from_file(path)

    def add_dataset_from_file(self, path):
        """Load a CSV and add it as a trace, reporting failures inline."""
        try:
            potential, current, label = load_voltammogram(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self, "Could Not Load File",
                f"Failed to load {Path(path).name}:\n{e}",
            )
            return
        self.add_dataset(Path(path).stem, potential, current, label,
                         source_path=path)

    def add_dataset(self, name, potential, current, label='Current (uA)',
                    source_path=None):
        """Create a trace, its control row, and its plot curve."""
        dataset_id = self._next_id
        self._next_id += 1
        color = _COLOR_CYCLE[self._color_index % len(_COLOR_CYCLE)]
        self._color_index += 1

        row = DatasetRow(dataset_id, name, potential, current, color,
                         label=label, source_path=source_path)
        row.changed.connect(self._schedule_render)
        row.remove_requested.connect(self._remove_dataset)
        self._row_layout.insertWidget(self._row_layout.count() - 1, row)
        self._rows[dataset_id] = row

        curve = self.plot_widget.plot([], [], name=name)
        self._curves[dataset_id] = curve

        self._render_dataset(dataset_id)

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

        potential, current = rendered
        curve.setData(potential, current)
        curve.setPen(pg.mkPen(color=row.color, width=2))

    def _remove_dataset(self, dataset_id):
        """Drop a trace, its control row, and its curve."""
        self._pending.discard(dataset_id)
        row = self._rows.pop(dataset_id, None)
        if row is not None:
            row.setParent(None)
        curve = self._curves.pop(dataset_id, None)
        if curve is not None:
            self.plot_widget.removeItem(curve)

    # ------------------------------------------------------------------
    # Peak labelling
    # ------------------------------------------------------------------

    def _on_plot_clicked(self, event):
        """Add a peak label on double-click, or remove one if clicked again."""
        if not event.double():
            return
        vb = self.plot_widget.plotItem.vb
        scene_pos = event.scenePos()
        if not self.plot_widget.sceneBoundingRect().contains(scene_pos):
            return

        # If the click landed on an existing label, remove that label instead.
        for entry in self._peak_labels:
            anchor_scene = vb.mapViewToScene(
                QtCore.QPointF(entry['x'], entry['y'])
            )
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

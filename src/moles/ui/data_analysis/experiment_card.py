"""One experiment's controls in a plot window's side panel.

The card edits the window's own ``ExperimentView`` directly and then reports
what changed, so the plot can redraw; it never touches the loaded data or the
file. Clicking the card (anywhere but its controls) selects the experiment.
"""

from PyQt5 import QtCore, QtGui, QtWidgets

from ...analysis.datasets import branch_spans
from ...analysis.model import LABEL_SHOULDER, LINE_STYLES
from .range_slider import IndexRangeSlider

# What a change touches, so the plot redraws only what it must.
CHANGED_NAME = "name"          # legend text
CHANGED_STYLE = "style"        # colour, line style, visibility
CHANGED_DATA = "data"          # offsets or data range: the curve and its labels
CHANGED_LABELS = "labels"

LINE_STYLE_NAMES = {
    "solid": "Solid", "dash": "Dashed", "dot": "Dotted",
    "dashdot": "Dash-dot", "dashdotdot": "Dash-dot-dot",
}

_UNSELECTED_BORDER = "#d0d0d0"


def _add_move_buttons(card, layout):
    """Up and down arrows that ask to move ``card``'s experiment in the list."""
    card.up_btn = QtWidgets.QToolButton()
    card.up_btn.setArrowType(QtCore.Qt.UpArrow)
    card.up_btn.setToolTip("Move up: earlier in the list and the legend")
    card.down_btn = QtWidgets.QToolButton()
    card.down_btn.setArrowType(QtCore.Qt.DownArrow)
    card.down_btn.setToolTip("Move down: later in the list and the legend")
    for button, step in ((card.up_btn, -1), (card.down_btn, 1)):
        button.setAutoRaise(True)
        button.clicked.connect(lambda _checked=False, s=step: card.move_requested.emit(card.exp.uid, s))
        layout.addWidget(button)


def _set_move_enabled(card, up, down):
    card.up_btn.setEnabled(up)
    card.down_btn.setEnabled(down)


def _volts_per_point(dataset):
    """Average potential step between points, for showing a window in mV."""
    spans = branch_spans(dataset.branch)
    travelled = sum(abs(dataset.potential[last] - dataset.potential[first])
                    for first, last in spans)
    return travelled / max(1, len(dataset) - 1)


class ExperimentCard(QtWidgets.QFrame):
    selected = QtCore.pyqtSignal(str)          # experiment uid
    changed = QtCore.pyqtSignal(str, str)      # experiment uid, CHANGED_*
    remove_requested = QtCore.pyqtSignal(str)  # experiment uid
    move_requested = QtCore.pyqtSignal(str, int)  # experiment uid, -1 up / +1 down

    def __init__(self, experiment, dataset, parent=None):
        super().__init__(parent)
        self.exp = experiment
        self.dataset = dataset
        self._is_selected = False
        self.setObjectName("experimentCard")

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 8)
        outer.setSpacing(4)

        # --- Header: collapse, show, name, remove ---
        header = QtWidgets.QHBoxLayout()
        self.collapse_btn = QtWidgets.QToolButton()
        self.collapse_btn.setArrowType(QtCore.Qt.DownArrow)
        self.collapse_btn.setAutoRaise(True)
        self.collapse_btn.setToolTip("Show or hide this experiment's settings")
        self.collapse_btn.clicked.connect(lambda: self.set_collapsed(self.body.isVisible()))
        header.addWidget(self.collapse_btn)

        self.visible_cb = QtWidgets.QCheckBox()
        self.visible_cb.setToolTip("Show this experiment on the plot")
        self.visible_cb.toggled.connect(self._on_visible)
        header.addWidget(self.visible_cb)

        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setMinimumWidth(80)
        self.name_edit.setToolTip(f"Name shown in the legend.\nFile: {self.exp.file}")
        self.name_edit.textEdited.connect(self._on_name)
        header.addWidget(self.name_edit, stretch=1)

        self.remove_btn = QtWidgets.QToolButton()
        self.remove_btn.setText("✕")
        self.remove_btn.setAutoRaise(True)
        self.remove_btn.setToolTip("Remove this experiment from the plot (the file stays in the workspace)")
        self.remove_btn.clicked.connect(lambda: self.remove_requested.emit(self.exp.uid))
        _add_move_buttons(self, header)
        header.addWidget(self.remove_btn)
        outer.addLayout(header)

        # --- Body ---
        self.body = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(self.body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)

        grid.addWidget(QtWidgets.QLabel("X offset"), 0, 0)
        self.x_spin = QtWidgets.QDoubleSpinBox()
        self.x_spin.setRange(-10.0, 10.0)
        self.x_spin.setDecimals(4)
        self.x_spin.setSingleStep(0.001)
        self.x_spin.setSuffix(" V")
        self.x_spin.setToolTip("Shift the trace along the potential axis, e.g. to reference it to Fc/Fc+")
        self.x_spin.valueChanged.connect(self._on_offsets)
        grid.addWidget(self.x_spin, 0, 1)
        grid.addWidget(QtWidgets.QLabel("Y offset"), 0, 2)
        self.y_spin = QtWidgets.QDoubleSpinBox()
        self.y_spin.setRange(-1e6, 1e6)
        self.y_spin.setDecimals(3)
        self.y_spin.setSingleStep(0.1)
        self.y_spin.setSuffix(" µA")
        self.y_spin.setToolTip("Shift the trace along the current axis")
        self.y_spin.valueChanged.connect(self._on_offsets)
        grid.addWidget(self.y_spin, 0, 3)
        for spin in (self.x_spin, self.y_spin):
            spin.setMinimumWidth(100)
            spin.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

        grid.addWidget(QtWidgets.QLabel("Colour"), 1, 0)
        self.color_btn = QtWidgets.QPushButton()
        self.color_btn.setFixedWidth(48)
        self.color_btn.setToolTip("Pick the line colour")
        self.color_btn.clicked.connect(self._pick_color)
        grid.addWidget(self.color_btn, 1, 1)
        grid.addWidget(QtWidgets.QLabel("Line"), 1, 2)
        self.style_combo = QtWidgets.QComboBox()
        for key in LINE_STYLES:
            self.style_combo.addItem(LINE_STYLE_NAMES[key], key)
        self.style_combo.currentIndexChanged.connect(self._on_line_style)
        grid.addWidget(self.style_combo, 1, 3)

        grid.addWidget(QtWidgets.QLabel("Smoothing"), 2, 0)
        self.smooth_spin = QtWidgets.QSpinBox()
        self.smooth_spin.setRange(1, 200)
        self.smooth_spin.setSuffix(" pts")
        self.smooth_spin.setSpecialValueText("Off")
        self.smooth_spin.setKeyboardTracking(False)
        self.smooth_spin.setToolTip(
            "Moving average over this many points, centred on each point.\n"
            "Smooths the plotted trace, its labels, Box Pick and the export.")
        self.smooth_spin.valueChanged.connect(self._on_smoothing)
        grid.addWidget(self.smooth_spin, 2, 1)
        self.smooth_readout = QtWidgets.QLabel()
        self.smooth_readout.setStyleSheet("color: gray;")
        grid.addWidget(self.smooth_readout, 2, 2, 1, 2)
        self._volts_per_point = _volts_per_point(dataset)

        grid.addWidget(QtWidgets.QLabel("Data range (points)"), 3, 0, 1, 4)
        last = max(1, len(dataset) - 1)
        self.slider = IndexRangeSlider(last)
        self.slider.rangeChanged.connect(self._on_slider)
        grid.addWidget(self.slider, 4, 0, 1, 4)

        self.start_spin = QtWidgets.QSpinBox()
        self.end_spin = QtWidgets.QSpinBox()
        for spin, tip in ((self.start_spin, "First point plotted"),
                          (self.end_spin, "Last point plotted")):
            spin.setRange(0, last)
            spin.setKeyboardTracking(False)
            spin.setToolTip(tip)
            spin.valueChanged.connect(self._on_range_spin)
        self.start_readout = QtWidgets.QLabel()
        self.end_readout = QtWidgets.QLabel()
        for readout in (self.start_readout, self.end_readout):
            readout.setStyleSheet("color: gray;")
        grid.addWidget(QtWidgets.QLabel("Start"), 5, 0)
        grid.addWidget(self.start_spin, 5, 1)
        grid.addWidget(self.start_readout, 5, 2, 1, 2)
        grid.addWidget(QtWidgets.QLabel("End"), 6, 0)
        grid.addWidget(self.end_spin, 6, 1)
        grid.addWidget(self.end_readout, 6, 2, 1, 2)

        grid.addWidget(QtWidgets.QLabel("Labelled points"), 7, 0, 1, 4)
        self.labels_box = QtWidgets.QVBoxLayout()
        self.labels_box.setSpacing(2)
        grid.addLayout(self.labels_box, 8, 0, 1, 4)
        outer.addWidget(self.body)

        self.sync_from_model()

    # ------------------------------------------------------------------
    # Showing the model
    # ------------------------------------------------------------------

    def sync_from_model(self):
        """Show the experiment's current settings without reporting them as edits."""
        exp = self.exp
        widgets = (self.visible_cb, self.name_edit, self.x_spin, self.y_spin,
                   self.style_combo, self.smooth_spin, self.start_spin, self.end_spin)
        for w in widgets:
            w.blockSignals(True)
        try:
            self.visible_cb.setChecked(exp.visible)
            if self.name_edit.text() != exp.display_name:
                self.name_edit.setText(exp.display_name)
            self.x_spin.setValue(exp.x_offset)
            self.y_spin.setValue(exp.y_offset)
            self.style_combo.setCurrentIndex(max(0, self.style_combo.findData(exp.line_style)))
            self.smooth_spin.setValue(exp.smoothing_points)
            self.start_spin.setValue(exp.range_start)
            self.end_spin.setValue(exp.range_end)
        finally:
            for w in widgets:
                w.blockSignals(False)
        self.slider.setValues(exp.range_start, exp.range_end)
        self._refresh_color()
        self._refresh_readouts()
        self._refresh_smoothing_readout()
        self.refresh_labels()

    def set_selected(self, selected):
        self._is_selected = selected
        self._refresh_color()

    def set_move_enabled(self, up, down):
        _set_move_enabled(self, up, down)

    def set_collapsed(self, collapsed):
        self.body.setVisible(not collapsed)
        self.collapse_btn.setArrowType(QtCore.Qt.RightArrow if collapsed else QtCore.Qt.DownArrow)

    def _refresh_color(self):
        color = self.exp.color
        self.color_btn.setStyleSheet(f"background-color: {color}; border: 1px solid #888;")
        border = (f"2px solid {color}" if self._is_selected
                  else f"1px solid {_UNSELECTED_BORDER}")
        self.setStyleSheet(f"QFrame#experimentCard {{ border: {border}; border-radius: 6px; }}")

    def _refresh_smoothing_readout(self):
        points = self.exp.smoothing_points
        if points <= 1 or not self._volts_per_point:
            self.smooth_readout.setText("")
        else:
            self.smooth_readout.setText(f"≈ {points * self._volts_per_point * 1000:.1f} mV")

    def _refresh_readouts(self):
        for index, readout in ((self.exp.range_start, self.start_readout),
                               (self.exp.range_end, self.end_readout)):
            x, _y = self.exp.displayed_point(self.dataset, index)
            readout.setText(f"at {x:.3f} V")

    def refresh_labels(self):
        """List every labelled point; ones outside the data range are greyed."""
        while self.labels_box.count():
            widget = self.labels_box.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        if not self.exp.labels:
            none = QtWidgets.QLabel("None yet")
            none.setStyleSheet("color: gray;")
            self.labels_box.addWidget(none)
            return
        for lp in self.exp.labels:
            row = QtWidgets.QWidget()
            layout = QtWidgets.QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            kind = lp.kind if lp.kind == LABEL_SHOULDER or not lp.extremum \
                else f"{lp.kind}, {lp.extremum}"
            text = QtWidgets.QLabel(f"{self.exp.label_text(self.dataset, lp)}   {kind}")
            text.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            # Wrap rather than widen the card past the side panel.
            text.setWordWrap(True)
            text.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            if not self.exp.in_range(lp.index):
                text.setText(text.text() + "   (outside data range)")
                text.setStyleSheet("color: gray;")
            layout.addWidget(text, stretch=1)
            delete = QtWidgets.QToolButton()
            delete.setText("✕")
            delete.setAutoRaise(True)
            delete.setToolTip("Delete this label")
            delete.clicked.connect(lambda _checked=False, i=lp.index: self._delete_label(i))
            layout.addWidget(delete)
            self.labels_box.addWidget(row)

    # ------------------------------------------------------------------
    # Edits
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        self.selected.emit(self.exp.uid)
        super().mousePressEvent(event)

    def _on_visible(self, checked):
        self.exp.visible = checked
        self.changed.emit(self.exp.uid, CHANGED_STYLE)

    def _on_name(self, text):
        self.exp.display_name = text
        self.changed.emit(self.exp.uid, CHANGED_NAME)

    def _on_offsets(self, *_args):
        self.exp.x_offset = self.x_spin.value()
        self.exp.y_offset = self.y_spin.value()
        self._refresh_readouts()
        self.refresh_labels()
        self.changed.emit(self.exp.uid, CHANGED_DATA)

    def _on_smoothing(self, points):
        self.exp.smoothing_points = points
        self._refresh_smoothing_readout()
        self.refresh_labels()
        self.changed.emit(self.exp.uid, CHANGED_DATA)

    def _pick_color(self):
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.exp.color), self, "Line Colour")
        if color.isValid():
            self.set_color(color.name())

    def set_color(self, color):
        self.exp.color = color
        self._refresh_color()
        self.changed.emit(self.exp.uid, CHANGED_STYLE)

    def _on_line_style(self, _index):
        self.exp.line_style = self.style_combo.currentData()
        self.changed.emit(self.exp.uid, CHANGED_STYLE)

    def _on_slider(self, low, high):
        self._apply_range(low, high)

    def _on_range_spin(self, _value):
        self._apply_range(self.start_spin.value(), self.end_spin.value())

    def _apply_range(self, start, end):
        start, end = self.exp.set_range(start, end)
        for spin, value in ((self.start_spin, start), (self.end_spin, end)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.slider.setValues(start, end)
        self._refresh_readouts()
        self.refresh_labels()
        self.changed.emit(self.exp.uid, CHANGED_DATA)

    def _delete_label(self, index):
        if self.exp.remove_label(index):
            self.refresh_labels()
            self.changed.emit(self.exp.uid, CHANGED_LABELS)


class MissingCard(QtWidgets.QFrame):
    """Stands in for an experiment whose file is no longer in the workspace.

    The experiment stays in the window's state, so saving the session keeps
    it; it comes back as soon as its file (by content) is in the workspace.
    """

    remove_requested = QtCore.pyqtSignal(str)
    move_requested = QtCore.pyqtSignal(str, int)

    def __init__(self, experiment, reason, parent=None):
        super().__init__(parent)
        self.exp = experiment
        self.setObjectName("experimentCard")
        self.setStyleSheet("QFrame#experimentCard { border: 1px dashed #d08000; border-radius: 6px; }")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        header = QtWidgets.QHBoxLayout()
        name = QtWidgets.QLabel(f"⚠ {experiment.display_name}")
        name.setStyleSheet("font-weight: bold;")
        header.addWidget(name, stretch=1)
        _add_move_buttons(self, header)
        remove = QtWidgets.QToolButton()
        remove.setText("✕")
        remove.setAutoRaise(True)
        remove.setToolTip("Remove this experiment from the plot")
        remove.clicked.connect(lambda: self.remove_requested.emit(self.exp.uid))
        header.addWidget(remove)
        layout.addLayout(header)
        text = QtWidgets.QLabel(
            f"Its file is not in the workspace: {experiment.file}\n{reason}\n"
            "It stays in this session, labels and all, and reappears once the "
            "file is added to the workspace again.")
        text.setWordWrap(True)
        text.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        text.setStyleSheet("color: gray;")
        layout.addWidget(text)

    def set_selected(self, selected):
        pass

    def set_move_enabled(self, up, down):
        _set_move_enabled(self, up, down)

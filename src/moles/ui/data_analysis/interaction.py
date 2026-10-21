"""Mouse tools of a plot window: modes, the picking box, draggable label text.

A plot is in one of three modes:

* **Pan/Zoom** — pyqtgraph's usual dragging and scrolling.
* **Box Pick** — dragging draws a box; releasing it looks for peaks and
  shoulders of the selected trace inside it.
* **Label Point** — clicking near the selected trace labels the nearest
  data point.

Clicking a trace (or one of its labels) selects it in every mode; clicking
empty space clears the selection, except in Label Point, where a click labels.
Label text can be dragged in every mode.
"""

import pyqtgraph as pg
from pyqtgraph import Point
from PyQt5 import QtCore, QtGui, QtWidgets

from ...analysis.model import PeakSettings, PlotStyle

MODE_PAN = "pan"
MODE_BOX = "box"
MODE_LABEL = "label"

_BOX_PEN = (40, 110, 220)
_BOX_FILL = (40, 110, 220, 40)


class PickViewBox(pg.ViewBox):
    """A ViewBox that, in the picking modes, reports boxes and clicks instead of moving.

    It only hears clicks no trace, marker or label took, so a click here is
    one on empty space.
    """

    box_drawn = QtCore.pyqtSignal(object)    # QRectF in data coordinates
    clicked_at = QtCore.pyqtSignal(object)   # QPointF in data coordinates (Label Point)
    empty_clicked = QtCore.pyqtSignal()      # a left click on empty space (other modes)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = MODE_PAN

    def mouseDragEvent(self, ev, axis=None):
        if self.mode != MODE_BOX or axis is not None \
                or ev.button() != QtCore.Qt.LeftButton:
            super().mouseDragEvent(ev, axis)
            return
        ev.accept()
        box = self.rbScaleBox
        box.setPen(pg.mkPen(_BOX_PEN, width=1))
        box.setBrush(pg.mkBrush(*_BOX_FILL))
        if ev.isFinish():
            box.hide()
            rect = QtCore.QRectF(Point(ev.buttonDownPos(ev.button())), Point(ev.pos()))
            self.box_drawn.emit(self.childGroup.mapRectFromParent(rect).normalized())
        else:
            self.updateScaleBox(ev.buttonDownPos(), ev.pos())

    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            ev.accept()
            if self.mode == MODE_LABEL:
                self.clicked_at.emit(self.mapSceneToView(ev.scenePos()))
            else:
                self.empty_clicked.emit()
            return
        super().mouseClickEvent(ev)


class StillLegendSample(pg.ItemSample):
    """A legend sample with its own line width that ignores clicks.

    pyqtgraph's own hides the trace when clicked, behind the side panel's
    back; visibility belongs to the experiment's checkbox. Ignored clicks
    fall through to the plot like any click on empty space. The line is
    drawn at the window's legend line width and sample length, which the
    exported figure uses too.
    """

    def __init__(self, item):
        super().__init__(item)
        self._line_px = None
        self._length_px = 20.0
        self._height_px = 20.0

    def set_line(self, line_px, length_px, height_px):
        """Draw the sample's line ``line_px`` wide and ``length_px`` long, centred
        in a box ``height_px`` tall (which sets the legend's row height)."""
        self._line_px = line_px
        self._length_px = length_px
        self._height_px = height_px
        self.setFixedWidth(length_px)
        self.setFixedHeight(height_px)
        self.update()

    def boundingRect(self):
        return QtCore.QRectF(0, 0, self._length_px, self._height_px)

    def paint(self, p, *args):
        if self._line_px is None:
            super().paint(p, *args)
            return
        pen = pg.mkPen(self.item.opts["pen"])
        pen.setWidthF(self._line_px)
        if pen.style() != QtCore.Qt.SolidLine:
            pen.setCapStyle(QtCore.Qt.FlatCap)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(pen)
        middle = self._height_px / 2
        p.drawLine(QtCore.QPointF(0, middle), QtCore.QPointF(self._length_px, middle))

    def mouseClickEvent(self, event):
        event.ignore()


class LabelText(pg.TextItem):
    """A label's text, which can be dragged away from its point.

    ``moved(offset_px, finished)`` is called with the text centre's offset
    from its point in screen pixels (right, down) while dragging and once
    more when the drag ends. Right-clicking offers ``menu_actions``: a list of
    (text, callable).
    """

    def __init__(self, text, color, point_scene_pos, moved, menu_actions, clicked=None):
        super().__init__(text, color=color, anchor=(0.5, 0.5))
        self._point_scene_pos = point_scene_pos  # callable -> QPointF
        self._moved = moved
        self._menu_actions = menu_actions
        self._clicked = clicked  # called on a left click
        self._start = None
        self.setCursor(QtCore.Qt.OpenHandCursor)
        self.setToolTip("Drag to move this label; right-click for more")

    def _offset_px(self, scene_pos):
        anchor = self._point_scene_pos()
        return (scene_pos.x() - anchor.x(), scene_pos.y() - anchor.y())

    def mouseDragEvent(self, ev):
        if ev.button() != QtCore.Qt.LeftButton:
            ev.ignore()
            return
        ev.accept()
        if ev.isStart():
            centre = self.mapToScene(self.boundingRect().center())
            self._start = centre - ev.buttonDownScenePos()
            self.setCursor(QtCore.Qt.ClosedHandCursor)
        if self._start is None:
            return
        offset = self._offset_px(ev.scenePos() + self._start)
        self._moved(offset, ev.isFinish())
        if ev.isFinish():
            self._start = None
            self.setCursor(QtCore.Qt.OpenHandCursor)

    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            # Taken here, so clicking a label never counts as clicking empty space.
            ev.accept()
            if self._clicked is not None:
                self._clicked()
            return
        if ev.button() != QtCore.Qt.RightButton or not self._menu_actions:
            ev.ignore()
            return
        ev.accept()
        menu = QtWidgets.QMenu()
        for text, callback in self._menu_actions:
            menu.addAction(text, callback)
        menu.exec_(ev.screenPos().toPoint())


class PeakSettingsDialog(QtWidgets.QDialog):
    """Smoothing window, prominence threshold, and valleys for box picking."""

    def __init__(self, settings: PeakSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Peak Picking Settings")
        self._base = settings
        form = QtWidgets.QFormLayout(self)

        self.smoothing = QtWidgets.QDoubleSpinBox()
        self.smoothing.setRange(1.0, 500.0)
        self.smoothing.setDecimals(1)
        self.smoothing.setSingleStep(5.0)
        self.smoothing.setSuffix(" mV")
        self.smoothing.setToolTip(
            "Savitzky-Golay smoothing window, as a stretch of potential.\n"
            "30 mV is 75 points of a 100 mV/s CV, and the 7-point minimum for 5 mV DPV steps.")
        form.addRow("Smoothing window", self.smoothing)

        self.auto = QtWidgets.QRadioButton(
            f"Automatic: {settings.auto_noise_multiple:g} × the noise, "
            f"at least {settings.auto_floor_uA:g} µA")
        self.fixed = QtWidgets.QRadioButton("Fixed")
        self.fixed_value = QtWidgets.QDoubleSpinBox()
        self.fixed_value.setRange(0.001, 1e6)
        self.fixed_value.setDecimals(3)
        self.fixed_value.setSuffix(" µA")
        self.fixed.toggled.connect(self.fixed_value.setEnabled)
        fixed_row = QtWidgets.QHBoxLayout()
        fixed_row.addWidget(self.fixed)
        fixed_row.addWidget(self.fixed_value, stretch=1)
        prominence = QtWidgets.QVBoxLayout()
        prominence.addWidget(self.auto)
        prominence.addLayout(fixed_row)
        form.addRow("Minimum prominence", prominence)

        self.valleys = QtWidgets.QCheckBox("Also report valleys between peaks")
        self.valleys.setToolTip("A maximum on a negative-going sweep, or a minimum on a "
                                "positive-going one, between two peaks")
        form.addRow("", self.valleys)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
            | QtWidgets.QDialogButtonBox.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QtWidgets.QDialogButtonBox.RestoreDefaults).clicked.connect(
            lambda: self._show(PeakSettings()))
        form.addRow(buttons)
        self._show(settings)

    def _show(self, settings):
        self.smoothing.setValue(settings.smoothing_mV)
        fixed = settings.prominence_uA is not None
        self.fixed.setChecked(fixed)
        self.auto.setChecked(not fixed)
        self.fixed_value.setValue(settings.prominence_uA if fixed else 2.0)
        self.fixed_value.setEnabled(fixed)
        self.valleys.setChecked(settings.report_valleys)

    def settings(self):
        """The settings as edited, keeping anything the dialog does not show."""
        base = self._base
        return PeakSettings(
            smoothing_mV=self.smoothing.value(), polyorder=base.polyorder,
            prominence_uA=self.fixed_value.value() if self.fixed.isChecked() else None,
            auto_noise_multiple=base.auto_noise_multiple, auto_floor_uA=base.auto_floor_uA,
            report_valleys=self.valleys.isChecked())


class PlotStyleDialog(QtWidgets.QDialog):
    """Text sizes and the legend's line width of one plot window."""

    _FIELDS = (
        ("label_font_pt", "Point labels", " pt", 4.0, 30.0),
        ("axis_title_pt", "Axis titles", " pt", 4.0, 36.0),
        ("tick_font_pt", "Tick numbers", " pt", 4.0, 30.0),
        ("legend_font_pt", "Legend text", " pt", 4.0, 30.0),
        ("legend_line_pt", "Legend line width", " pt", 0.5, 12.0),
    )

    def __init__(self, style: PlotStyle, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Plot Style")
        form = QtWidgets.QFormLayout(self)
        note = QtWidgets.QLabel("Sizes apply on screen and in exported figures alike.")
        note.setStyleSheet("color: gray;")
        form.addRow(note)
        self.spins = {}
        for key, label, suffix, low, high in self._FIELDS:
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(low, high)
            spin.setDecimals(1)
            spin.setSingleStep(0.5)
            spin.setSuffix(suffix)
            form.addRow(label, spin)
            self.spins[key] = spin
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
            | QtWidgets.QDialogButtonBox.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QtWidgets.QDialogButtonBox.RestoreDefaults).clicked.connect(
            lambda: self._show(PlotStyle()))
        form.addRow(buttons)
        self._show(style)

    def _show(self, style):
        for key, spin in self.spins.items():
            spin.setValue(getattr(style, key))

    def style(self):
        return PlotStyle(**{key: spin.value() for key, spin in self.spins.items()})


def mode_cursor(mode):
    return {MODE_BOX: QtCore.Qt.CrossCursor,
            MODE_LABEL: QtCore.Qt.PointingHandCursor}.get(mode, QtCore.Qt.ArrowCursor)


def points_to_pixels(widget, points):
    """Screen pixels in ``points`` (1/72 inch) on the screen showing ``widget``."""
    return points * widget.logicalDpiY() / 72.0

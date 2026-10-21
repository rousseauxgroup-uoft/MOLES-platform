"""One plot window: overlaid voltammograms and their side panel of controls.

Each window owns its ``PlotWindowState``: which experiments it shows and how
(offsets, colours, ranges, labels). Loaded files come from the shared
``DatasetStore`` and are never changed, so windows showing the same file stay
independent of each other.

Files dragged from the Data Explorer onto the plot or the side panel are
added. Files dragged in from outside the workspace are first copied into it
by the main window (``outside_files_dropped``), so a plot only ever shows
workspace copies.

Clicking a trace, its card, or one of its labels selects it; the selected
trace is drawn with a soft glow underneath. Clicking empty space clears the
selection. Labelled points are drawn as circles in the trace's
colour with their displayed coordinates beside them; the text can be dragged
aside, and keeps that offset (in points) for the exported figure.

The toolbar's modes (see ``interaction``) pick peaks in a box or label single
points of the selected trace. Each batch of new labels can be undone.

Export writes the window to an Excel workbook, or its figure to PNG or SVG,
drawn again in the figure style by ``analysis.export``.
"""

import logging
import re
from collections import Counter
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from ...analysis.datasets import UnsupportedDataError
from ...analysis.appearance import (
    DASH_PATTERNS, LABEL_MARKER_PT, LEADER_LINE_PT, LEGEND_BACKING, LEGEND_GAP_PT,
    LEGEND_PAD_PT, LEGEND_ROW_EM, LEGEND_ROW_GAP_PT, LEGEND_SAMPLE_PT, MARKER_RIM_PT, TEXT_COLOR,
    TRACE_LINE_PT,
)
from ...analysis.display import every_nth, thin
from ...analysis.export import (
    FigureLayout, export_workbook, figure_png, render_figure, save_figure,
)
from ...analysis.model import LABEL_MANUAL, LabelledPoint
from ...analysis.peaks import detect_in_box
from ...analysis.sessions import resolve
from ...analysis.workspace import WorkspaceError
from .experiment_card import (
    CHANGED_DATA, CHANGED_LABELS, CHANGED_NAME, CHANGED_STYLE, ExperimentCard, MissingCard,
)
from .explorer import WORKSPACE_MIME, workspace_paths_from_mime
from .interaction import (
    MODE_BOX, MODE_LABEL, MODE_PAN, LabelText, PeakSettingsDialog, PickViewBox,
    PlotStyleDialog, StillLegendSample, mode_cursor, points_to_pixels,
)
from .style import BACKGROUND, TickSync, apply_text_style, plot_font, style_plot

logger = logging.getLogger(__name__)

GLOW_WIDTH = 10
GLOW_ALPHA = 80
# A white rim keeps a label's marker visible on top of its own trace.
MARKER_RIM = "w"

CLICK_TOLERANCE_PX = 8
SIDE_PANEL_WIDTH = 430

# Pause after an edit before redrawing, so dragging a slider redraws only
# as fast as the eye can follow.
_RENDER_DELAY_MS = 30

# How close (in pixels) a Label Point click must land to the selected trace.
SNAP_RADIUS_PX = 15

# Light backing behind label text, so it stays readable over a curve.
LABEL_FILL = (255, 255, 255, 190)

# Batches of new labels that Undo can take back.
UNDO_LIMIT = 50

_Z_CURVE, _Z_SELECTED, _Z_LEADER, _Z_LABELS = 1, 10, 19, 20

_MODE_HINTS = {
    MODE_PAN: "Drag to pan, scroll to zoom. Click a trace to select it, "
              "or empty space to clear the selection.",
    MODE_BOX: "Drag a box over part of the selected trace to find its peaks and shoulders.",
    MODE_LABEL: "Click near the selected trace to label its nearest point.",
}


@dataclass
class _DrawnLabel:
    label: LabelledPoint
    point: Tuple[float, float]  # displayed coordinates of the labelled point
    marker: pg.ScatterPlotItem
    text: LabelText
    leader: pg.PlotCurveItem


@dataclass
class _Drawn:
    """The plot items drawn for one experiment."""

    curve: pg.PlotCurveItem
    glow: pg.PlotCurveItem
    labels: List[_DrawnLabel] = field(default_factory=list)


def _droppable(mime):
    if mime.hasFormat(WORKSPACE_MIME):
        return True
    return mime.hasUrls() and any(u.isLocalFile() for u in mime.urls())


class _DropFilter(QtCore.QObject):
    """Turns file drops on a widget into ``dropped(workspace paths, outside paths)``."""

    dropped = QtCore.pyqtSignal(list, list)

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind in (QtCore.QEvent.DragEnter, QtCore.QEvent.DragMove):
            if _droppable(event.mimeData()):
                event.setDropAction(QtCore.Qt.CopyAction)
                event.accept()
                return True
            return False
        if kind == QtCore.QEvent.Drop:
            mime = event.mimeData()
            if not _droppable(mime):
                return False
            rels = workspace_paths_from_mime(mime)
            outside = [] if rels else [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
            event.setDropAction(QtCore.Qt.CopyAction)
            event.accept()
            # Let the drag finish before anything the drop leads to (dialogs).
            QtCore.QTimer.singleShot(0, lambda: self.dropped.emit(rels, outside))
            return True
        return False


class PlotPanel(QtWidgets.QWidget):
    """A plot and its side panel, docked as a tab or popped out on its own."""

    changed = QtCore.pyqtSignal()        # content a session saves: experiments, settings, labels
    view_changed = QtCore.pyqtSignal()   # zoom, pan, selection: saved too, but not "unsaved work"
    selection_changed = QtCore.pyqtSignal(object)  # workspace path of the selected trace, or None
    title_changed = QtCore.pyqtSignal(object)           # self
    pop_toggled = QtCore.pyqtSignal(object)             # self: pop out or dock back
    outside_files_dropped = QtCore.pyqtSignal(object, list)  # self, paths to copy in first

    def __init__(self, state, store, parent=None):
        super().__init__(parent)
        self.state = state
        self.store = store
        self._drawn: Dict[str, _Drawn] = {}
        self._cards: Dict[str, ExperimentCard] = {}
        self._datasets = {}
        self._missing = set()  # experiments whose file is not in the workspace
        self._pending = set()
        self._popped_out = False
        self._undo: List[Tuple[str, List[int]]] = []
        self.mode = MODE_PAN

        self._render_timer = QtCore.QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(_RENDER_DELAY_MS)
        self._render_timer.timeout.connect(self._flush)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_toolbar())

        self.view_box = PickViewBox()
        self.view_box.box_drawn.connect(self.pick_in_box)
        self.view_box.clicked_at.connect(self.label_at)
        self.view_box.empty_clicked.connect(lambda: self.select(None))
        # Label text sits a fixed distance from its point on screen, so it is
        # placed again whenever zooming or resizing changes the scale.
        self.view_box.sigTransformChanged.connect(self._place_all_labels)
        self.plot_widget = pg.PlotWidget(background=BACKGROUND, viewBox=self.view_box)
        self.plot_item = self.plot_widget.getPlotItem()
        style_plot(self.plot_item, self.state.style, self._px_per_pt())
        self._ticks = TickSync(self.plot_item, lambda: self.state.style, self._px_per_pt, self)
        self.legend = self.plot_item.addLegend(offset=(-10, 10), labelTextColor=TEXT_COLOR,
                                               labelTextSize=f"{self.state.style.legend_font_pt}pt",
                                               brush=pg.mkBrush(*(int(round(255 * c))
                                                                  for c in LEGEND_BACKING)),
                                               sampleType=StillLegendSample)
        if self.state.legend_box is not None:
            self.legend.anchor(itemPos=(0, 0), parentPos=self.state.legend_box)
        # NOTE: no pyqtgraph clip-to-view or auto-downsampling here: both assume
        # an x-axis that only increases, which a voltammogram's is not. Long
        # traces are thinned along point order instead (analysis.display).
        self.plot_item.getViewBox().sigRangeChangedManually.connect(self._on_view_moved)

        self.side_panel = self._build_side_panel()
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.addWidget(self.plot_widget)
        self.splitter.addWidget(self.side_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setCollapsible(0, False)
        self.splitter.setSizes([900, SIDE_PANEL_WIDTH])
        layout.addWidget(self.splitter, stretch=1)

        self.status = QtWidgets.QLabel()
        self.status.setStyleSheet("color: gray; padding: 2px 6px;")
        layout.addWidget(self.status)

        self._drop_filter = _DropFilter(self)
        self._drop_filter.dropped.connect(self._on_dropped)
        for target in (self.plot_widget.viewport(), self._scroll.viewport()):
            target.setAcceptDrops(True)
            target.installEventFilter(self._drop_filter)

        for exp in list(self.state.experiments):
            self._attach(exp)
        if self.state.selected_uid in self._missing:
            self.state.selected_uid = None
        self._refresh_legend()
        self._apply_selection()
        self._restore_view()
        self.set_mode(MODE_PAN)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_toolbar(self):
        bar = QtWidgets.QToolBar()
        bar.setIconSize(QtCore.QSize(16, 16))
        self.mode_actions = {}
        group = QtWidgets.QActionGroup(self)
        for mode, text, key, tip in (
                (MODE_PAN, "Pan/Zoom", "P", "Drag to pan, scroll to zoom (P)"),
                (MODE_BOX, "Box Pick", "B", "Drag a box over the selected trace to find "
                                            "its peaks and shoulders (B)"),
                (MODE_LABEL, "Label Point", "L", "Click near the selected trace to label "
                                                 "a point by hand (L)")):
            action = QtWidgets.QAction(text, self)
            action.setCheckable(True)
            action.setShortcut(key)
            # Within this plot only, and never while typing in a box.
            action.setShortcutContext(QtCore.Qt.WidgetWithChildrenShortcut)
            action.setToolTip(tip)
            action.triggered.connect(lambda _checked, m=mode: self.set_mode(m))
            group.addAction(action)
            bar.addAction(action)
            self.addAction(action)
            self.mode_actions[mode] = action
        self.settings_action = bar.addAction("Peak Settings…", self.open_peak_settings)
        self.style_action = bar.addAction("Plot Style…", self.open_plot_style)
        self.style_action.setToolTip("Text sizes and the legend's line width, on screen "
                                     "and in exported figures")
        self.settings_action.setToolTip("Smoothing window and minimum prominence for Box Pick")
        self.undo_action = QtWidgets.QAction("Undo Labels", self)
        self.undo_action.setShortcut(QtGui.QKeySequence.Undo)
        self.undo_action.setShortcutContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self.undo_action.setToolTip("Take back the labels added last")
        self.undo_action.triggered.connect(self.undo_labels)
        self.undo_action.setEnabled(False)
        bar.addAction(self.undo_action)
        self.addAction(self.undo_action)
        bar.addSeparator()
        export_button = QtWidgets.QToolButton()
        export_button.setText("Export")
        export_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        export_menu = QtWidgets.QMenu(export_button)
        export_menu.addAction("Export to Excel…", self.export_excel)
        export_menu.addAction("Save Image (PNG/SVG)…", self.save_image)
        export_button.setMenu(export_menu)
        export_button.setToolTip("Save this plot's data, labels and figure")
        bar.addWidget(export_button)
        self.fit_action = bar.addAction("Fit View", self.fit_view)
        self.fit_action.setToolTip("Zoom to show every visible trace")
        bar.addSeparator()
        self.side_action = bar.addAction("Side Panel")
        self.side_action.setCheckable(True)
        self.side_action.setChecked(True)
        self.side_action.toggled.connect(lambda shown: self.side_panel.setVisible(shown))
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        bar.addWidget(spacer)
        self.pop_action = bar.addAction("Pop Out", lambda: self.pop_toggled.emit(self))
        self.pop_action.setToolTip("Show this plot in its own window")
        return bar

    def _build_side_panel(self):
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        title = QtWidgets.QLabel("Experiments")
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        holder = QtWidgets.QWidget()
        self._cards_layout = QtWidgets.QVBoxLayout(holder)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(6)
        self._empty_hint = QtWidgets.QLabel(
            "Drag files from the Data Explorer onto the plot or here.")
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setStyleSheet("color: gray;")
        self._cards_layout.addWidget(self._empty_hint)
        self._cards_layout.addStretch(1)
        self._scroll.setWidget(holder)
        layout.addWidget(self._scroll, stretch=1)
        panel.setMinimumWidth(260)
        return panel

    @property
    def title(self):
        return self.state.title

    def set_title(self, title):
        self.state.title = title
        self.title_changed.emit(self)
        self.changed.emit()

    def set_popped_out(self, popped_out):
        self._popped_out = popped_out
        self.state.popped_out = popped_out
        self.pop_action.setText("Dock Back" if popped_out else "Pop Out")
        self.pop_action.setToolTip("Put this plot back among the tabs" if popped_out
                                   else "Show this plot in its own window")

    def _update_hint(self):
        empty = not self.state.experiments
        self._empty_hint.setVisible(empty)
        self.status.setText("Drop files from the Data Explorer to plot them." if empty
                            else _MODE_HINTS[self.mode])

    def set_mode(self, mode):
        """Switch between Pan/Zoom, Box Pick and Label Point."""
        self.mode = mode
        self.view_box.mode = mode
        self.mode_actions[mode].setChecked(True)
        self.plot_widget.viewport().setCursor(mode_cursor(mode))
        self._update_hint()

    # ------------------------------------------------------------------
    # Experiments
    # ------------------------------------------------------------------

    def uses(self, rel):
        return any(exp.file == rel for exp in self.state.experiments)

    def add_files(self, rels):
        """Add workspace files as new experiments; returns the new experiments' uids."""
        added, failed = [], []
        for rel in rels:
            try:
                dataset = self.store.get(rel)
            except (UnsupportedDataError, WorkspaceError, OSError, ValueError) as e:
                failed.append(f"{rel}: {e}")
                continue
            exp = self.state.add_experiment(dataset, rel)
            self._attach(exp, dataset)
            added.append(exp.uid)
        if failed:
            self.show_error("Some files could not be plotted", "\n".join(failed))
        if added:
            first_traces = len(self.state.experiments) == len(added)
            self._refresh_legend()
            self.select(added[-1])
            if first_traces or self.state.view_range is None:
                self.fit_view()
            self._update_hint()
            self.changed.emit()
        return added

    def _attach(self, exp, dataset=None, position=None):
        """Create the curve, glow, and card for an experiment already in the state.

        An experiment whose file cannot be loaded, or whose content is not
        what was saved, gets a stand-in card instead and is not drawn.
        """
        if dataset is None:
            try:
                dataset = self.store.get(exp.file)
            except (UnsupportedDataError, WorkspaceError, OSError, ValueError) as e:
                self._attach_missing(exp, str(e), position)
                return
            if exp.sha256 and dataset.sha256 != exp.sha256:
                self._attach_missing(exp, "A different file now has its name.", position)
                return
        self._datasets[exp.uid] = dataset
        glow = pg.PlotCurveItem()
        glow.setZValue(_Z_CURVE - 0.5)
        curve = pg.PlotCurveItem()
        curve.setClickable(True, width=CLICK_TOLERANCE_PX)
        curve.sigClicked.connect(lambda _item, ev, uid=exp.uid: self._on_curve_clicked(uid, ev))
        self.plot_item.addItem(glow)
        self.plot_item.addItem(curve)
        self._drawn[exp.uid] = _Drawn(curve, glow)

        card = ExperimentCard(exp, dataset)
        card.selected.connect(self.select)
        card.changed.connect(self._on_card_changed)
        card.remove_requested.connect(self.remove_experiment)
        card.move_requested.connect(self.move_experiment)
        self._insert_card(card, position)
        self._cards[exp.uid] = card
        self._update_move_buttons()
        self._render(exp.uid)

    def _insert_card(self, card, position=None):
        # The layout ends with a stretch, which stays last.
        last = self._cards_layout.count() - 1
        self._cards_layout.insertWidget(last if position is None else min(position, last), card)

    def _attach_missing(self, exp, reason, position=None):
        card = MissingCard(exp, reason)
        card.remove_requested.connect(self.remove_experiment)
        card.move_requested.connect(self.move_experiment)
        self._insert_card(card, position)
        self._cards[exp.uid] = card
        self._missing.add(exp.uid)
        self._update_move_buttons()

    def missing_files(self):
        return [e.file for e in self.state.experiments if e.uid in self._missing]

    def retry_missing(self):
        """Draw experiments whose file has come back to the workspace; returns how many."""
        found = 0
        for exp in self.state.experiments:
            if exp.uid not in self._missing:
                continue
            path = resolve(self.store.workspace, exp)
            if path is None:
                continue
            exp.file = path
            self._missing.discard(exp.uid)
            stand_in = self._cards.pop(exp.uid)
            position = self._cards_layout.indexOf(stand_in)
            stand_in.setParent(None)
            stand_in.deleteLater()
            self._attach(exp, position=position)
            if exp.uid in self._drawn:
                found += 1
        if found:
            self._refresh_legend()
            self.changed.emit()
        return found

    def remove_experiment(self, uid):
        exp = self.state.experiment(uid)
        if exp is None:
            return False
        if uid in self._missing:
            self._missing.discard(uid)
            card = self._cards.pop(uid)
            card.setParent(None)
            card.deleteLater()
            self.state.remove_experiment(uid)
            self._update_move_buttons()
            self._update_hint()
            self.changed.emit()
            return True
        if exp.labels and not self.confirm(
                "Remove Experiment",
                f"Remove {exp.display_name} from this plot?\n"
                f"Its {len(exp.labels)} labelled point(s) will be lost. "
                "The file stays in the workspace."):
            return False
        self._pending.discard(uid)
        drawn = self._drawn.pop(uid)
        self._remove_label_items(drawn)
        for item in (drawn.curve, drawn.glow):
            self.plot_item.removeItem(item)
        self._undo = [entry for entry in self._undo if entry[0] != uid]
        self.undo_action.setEnabled(bool(self._undo))
        card = self._cards.pop(uid)
        card.setParent(None)
        card.deleteLater()
        self._datasets.pop(uid, None)
        was_selected = self.state.selected_uid == uid
        self.state.remove_experiment(uid)
        self._refresh_legend()
        self._apply_selection()
        self._update_move_buttons()
        self._update_hint()
        self.changed.emit()
        if was_selected:
            self.selection_changed.emit(None)
        return True

    def move_experiment(self, uid, step):
        """Move an experiment up (-1) or down (+1): its card, legend entry, and export order."""
        if not self.state.move_experiment(uid, step):
            return False
        cards = [self._cards[e.uid] for e in self.state.experiments if e.uid in self._cards]
        for card in cards:
            self._cards_layout.removeWidget(card)
        for card in cards:
            # The layout ends with a stretch, which stays last.
            self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)
        self._update_move_buttons()
        self._refresh_legend()
        self._scroll.ensureWidgetVisible(self._cards[uid])
        self.changed.emit()
        return True

    def _update_move_buttons(self):
        uids = [e.uid for e in self.state.experiments]
        for i, uid in enumerate(uids):
            card = self._cards.get(uid)
            if card is not None:
                card.set_move_enabled(i > 0, i < len(uids) - 1)

    def files_moved(self, pairs):
        """Follow workspace files renamed or moved in the Data Explorer."""
        renamed = dict(pairs)
        for exp in self.state.experiments:
            if exp.file in renamed:
                exp.file = renamed[exp.file]
                card = self._cards.get(exp.uid)
                if card is not None:
                    card.name_edit.setToolTip(f"Name shown in the legend.\nFile: {exp.file}")
                self.changed.emit()

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def select(self, uid):
        if uid is not None and (self.state.experiment(uid) is None or uid in self._missing):
            return
        if self.state.selected_uid != uid:
            self.state.selected_uid = uid
            self._apply_selection()
            self.view_changed.emit()
            self.selection_changed.emit(self.selected_file())

    def selected_file(self):
        """Workspace path of the selected trace's file, or None."""
        exp = self.state.selected
        return exp.file if exp is not None and exp.uid not in self._missing else None

    def _apply_selection(self):
        selected = self.state.selected_uid
        for uid, drawn in self._drawn.items():
            is_selected = uid == selected
            drawn.curve.setZValue(_Z_SELECTED if is_selected else _Z_CURVE)
            drawn.glow.setZValue((_Z_SELECTED if is_selected else _Z_CURVE) - 0.5)
            exp = self.state.experiment(uid)
            drawn.glow.setVisible(is_selected and exp is not None and exp.visible)
            self._cards[uid].set_selected(is_selected)
        if selected in self._cards:
            self._scroll.ensureWidgetVisible(self._cards[selected])

    def _on_curve_clicked(self, uid, ev):
        self.select(uid)
        if self.mode == MODE_LABEL and ev is not None:
            self.label_at(self.view_box.mapSceneToView(ev.scenePos()))

    def keyPressEvent(self, event):
        # Escape leaves a picking mode first, and only then clears the selection.
        if event.key() == QtCore.Qt.Key_Escape:
            if self.mode != MODE_PAN:
                self.set_mode(MODE_PAN)
            else:
                self.select(None)
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _on_card_changed(self, uid, what):
        if what == CHANGED_NAME:
            self._refresh_legend()
        elif what == CHANGED_STYLE:
            self._render(uid)
            self._refresh_legend()
            self._apply_selection()
        elif what == CHANGED_LABELS:
            self._render_labels(uid)
        elif what == CHANGED_DATA:
            self._pending.add(uid)
            self._render_timer.start()
        self.changed.emit()

    def _flush(self):
        pending, self._pending = self._pending, set()
        for uid in pending:
            if uid in self._drawn:
                self._render(uid)

    def _render(self, uid):
        exp = self.state.experiment(uid)
        drawn = self._drawn.get(uid)
        if exp is None or drawn is None:
            return
        dataset = self._datasets[uid]
        color = QtGui.QColor(exp.color)
        pen = pg.mkPen(color, width=TRACE_LINE_PT * self._px_per_pt())
        if exp.line_style in DASH_PATTERNS:
            # The same dashes as the exported figure, which ends them flat too.
            pen.setDashPattern(list(DASH_PATTERNS[exp.line_style]))
            pen.setCapStyle(QtCore.Qt.FlatCap)
        glow_color = QtGui.QColor(color)
        glow_color.setAlpha(GLOW_ALPHA)
        glow_pen = pg.mkPen(glow_color, width=GLOW_WIDTH)
        glow_pen.setCapStyle(QtCore.Qt.RoundCap)
        glow_pen.setJoinStyle(QtCore.Qt.RoundJoin)

        _indices, x, y = exp.displayed_arrays(dataset)
        x, y = thin(x, y) if exp.line_style == "solid" else every_nth(x, y)
        drawn.curve.setData(x, y, pen=pen, antialias=True)
        drawn.glow.setData(x, y, pen=glow_pen, antialias=True)
        drawn.curve.setVisible(exp.visible)
        drawn.glow.setVisible(exp.visible and uid == self.state.selected_uid)
        self._render_labels(uid)

    def _remove_label_items(self, drawn):
        for dl in drawn.labels:
            for item in (dl.marker, dl.text, dl.leader):
                self.plot_item.removeItem(item)
        drawn.labels = []

    def _render_labels(self, uid):
        exp = self.state.experiment(uid)
        drawn = self._drawn[uid]
        self._remove_label_items(drawn)
        if not exp.visible:
            return
        dataset = self._datasets[uid]
        color = QtGui.QColor(exp.color)
        font = plot_font(self.state.style.label_font_pt)
        px = self._px_per_pt()
        for lp in exp.shown_labels():
            x, y = exp.displayed_point(dataset, lp.index)
            marker = pg.ScatterPlotItem([x], [y], size=LABEL_MARKER_PT * px, symbol="o",
                                        pen=pg.mkPen(MARKER_RIM, width=MARKER_RIM_PT * px),
                                        brush=pg.mkBrush(color))
            leader = pg.PlotCurveItem(pen=pg.mkPen(color, width=LEADER_LINE_PT * px))
            dl = _DrawnLabel(lp, (x, y), marker, None, leader)
            text = LabelText(
                exp.label_text(dataset, lp), color,
                point_scene_pos=lambda p=(x, y): self.view_box.mapViewToScene(QtCore.QPointF(*p)),
                moved=lambda offset, done, uid=uid, dl=dl: self._on_label_dragged(uid, dl, offset, done),
                menu_actions=[
                    ("Reset Label Position", lambda uid=uid, lp=lp: self._reset_label(uid, lp)),
                    ("Delete Label", lambda uid=uid, i=lp.index: self.delete_label(uid, i)),
                ],
                clicked=lambda uid=uid: self.select(uid))
            text.setFont(font)
            text.fill = pg.mkBrush(*LABEL_FILL)
            dl.text = text
            marker.setZValue(_Z_LABELS)
            text.setZValue(_Z_LABELS + 1)
            leader.setZValue(_Z_LEADER)
            for item in (leader, marker, text):
                self.plot_item.addItem(item)
            drawn.labels.append(dl)
            self._place_label(dl)

    def _place_label(self, dl):
        """Put a label's text at its offset (in points) from its point, at the current zoom."""
        x, y = dl.point
        dx_pt, dy_pt = dl.label.offset_pt()
        px_per_pt = self._px_per_pt()
        sx, sy = self.view_box.viewPixelSize()
        tx, ty = x + dx_pt * px_per_pt * sx, y + dy_pt * px_per_pt * sy
        dl.text.setPos(tx, ty)
        dl.leader.setData([x, tx], [y, ty])
        dl.leader.setVisible(dl.label.has_leader())

    def _place_all_labels(self, *_args):
        for drawn in self._drawn.values():
            for dl in drawn.labels:
                self._place_label(dl)

    def _on_label_dragged(self, uid, dl, offset_px, finished):
        px_per_pt = self._px_per_pt()
        # Screen y runs down; label offsets are stored running up, like a figure's.
        dl.label.text_offset = (offset_px[0] / px_per_pt, -offset_px[1] / px_per_pt)
        self._place_label(dl)
        if finished:
            self.changed.emit()

    def _reset_label(self, uid, label):
        label.text_offset = None
        self._render_labels(uid)
        self.changed.emit()

    def delete_label(self, uid, index):
        exp = self.state.experiment(uid)
        if exp is not None and exp.remove_label(index):
            self.refresh_labels(uid)

    def refresh_labels(self, uid):
        """Redraw an experiment's labels and its card's list after they changed."""
        self._render_labels(uid)
        self._cards[uid].refresh_labels()
        self.changed.emit()

    def _refresh_legend(self):
        style = self.state.style
        self.legend.clear()
        self.legend.setLabelTextSize(f"{style.legend_font_pt}pt")
        for exp in self.state.experiments:
            if exp.visible and exp.uid in self._drawn:
                self.legend.addItem(self._drawn[exp.uid].curve, exp.display_name)
        px = self._px_per_pt()
        # Spacing in points, as the exported figure lays its legend out.
        pad = LEGEND_PAD_PT * px
        self.legend.layout.setContentsMargins(pad, pad, pad, pad)
        self.legend.layout.setHorizontalSpacing(LEGEND_GAP_PT * px)
        self.legend.layout.setVerticalSpacing(LEGEND_ROW_GAP_PT * px)
        font = plot_font(style.legend_font_pt)
        row_px = LEGEND_ROW_EM * style.legend_font_pt * px
        for sample, label in self.legend.items:
            sample.set_line(style.legend_line_pt * px, LEGEND_SAMPLE_PT * px, row_px)
            label.item.document().setDocumentMargin(0)
            label.item.setFont(font)
            label.setText(label.text)  # measure the name again in its font
            self.legend.layout.setAlignment(label, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self.legend.updateSize()

    def capture_legend(self):
        """Note where the legend sits (it can be dragged) for the session and the export."""
        parent = self.legend.parentItem()
        if parent is None:
            return
        box = self.legend.mapRectToParent(self.legend.boundingRect())
        area = parent.boundingRect()
        if area.width() > 0 and area.height() > 0:
            self.state.legend_box = ((box.left() - area.left()) / area.width(),
                                     (box.top() - area.top()) / area.height())

    def _px_per_pt(self):
        widget = getattr(self, "plot_widget", None)
        return points_to_pixels(widget, 1.0) if widget is not None else 1.0

    def open_plot_style(self):
        dialog = PlotStyleDialog(self.state.style, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self.set_style(dialog.style())

    def set_style(self, style):
        """Change the window's text sizes and legend line width, on screen and in exports."""
        self.state.style = style
        apply_text_style(self.plot_item, style, self._px_per_pt())
        self._ticks.update()
        self._refresh_legend()
        for uid in self._drawn:
            self._render_labels(uid)
        self.changed.emit()

    # ------------------------------------------------------------------
    # Peak picking and manual labels
    # ------------------------------------------------------------------

    def _pick_target(self):
        """The experiment to pick on: the selected one, or the only one shown."""
        exp = self.state.selected
        if exp is None:
            shown = [e for e in self.state.experiments if e.visible]
            if len(shown) == 1:
                exp = shown[0]
                self.select(exp.uid)
        if exp is None:
            self.status.setText("Select a trace first: click it, or its card in the side panel.")
            return None
        if not exp.visible:
            self.status.setText(f"{exp.display_name} is hidden; show it to pick on it.")
            return None
        return exp

    def pick_in_box(self, rect):
        """Find peaks and shoulders of the selected trace inside a box (data coordinates)."""
        exp = self._pick_target()
        if exp is None:
            return []
        dataset = self._datasets[exp.uid]
        # Picks on the trace as shown, smoothing included.
        detection = detect_in_box(
            dataset.potential, exp.current(dataset), dataset.branch, dataset.direction,
            (rect.left(), rect.right(), rect.top(), rect.bottom()),
            self.state.peak_settings, index_range=(exp.range_start, exp.range_end),
            offsets=(exp.x_offset, exp.y_offset))
        if not detection.runs:
            self.status.setText(f"The box holds none of {exp.display_name}'s points.")
            return []

        added, repeated = [], 0
        for f in detection.features:
            if exp.add_label(LabelledPoint(f.index, f.kind, f.extremum)):
                added.append(f)
            else:
                repeated += 1
        if added:
            self._remember(exp.uid, [f.index for f in added])
            self.refresh_labels(exp.uid)

        run = detection.runs[0]
        counts = Counter(f.kind for f in added)
        found = ", ".join(f"{n} {kind}{'s' if n != 1 else ''}" for kind, n in counts.items())
        message = f"Found {found}." if added else "Nothing new found in the box."
        if repeated:
            message += f" {repeated} already labelled."
        message += (f"  ({len(detection.runs)} sweep segment{'s' if len(detection.runs) != 1 else ''}, "
                    f"smoothing {run.window_points} pts, threshold {run.threshold_uA:.2f} µA)")
        notes = sorted({r.note for r in detection.runs if r.note})
        if notes:
            message += "  Note: " + "; ".join(notes) + "."
        self.status.setText(message)
        return [f.index for f in added]

    def label_at(self, pos):
        """Label the selected trace's point nearest ``pos`` (data coordinates) on screen."""
        exp = self._pick_target()
        if exp is None:
            return None
        dataset = self._datasets[exp.uid]
        indices, x, y = exp.displayed_arrays(dataset)
        sx, sy = self.view_box.viewPixelSize()
        # Distance on screen, so a steep and a flat stretch of trace count alike.
        dist = np.hypot((x - pos.x()) / sx, (y - pos.y()) / sy)
        nearest = int(np.argmin(dist))
        if dist[nearest] > SNAP_RADIUS_PX:
            self.status.setText(f"Click within {SNAP_RADIUS_PX} pixels of {exp.display_name}.")
            return None
        index = int(indices[nearest])
        if not exp.add_label(LabelledPoint(index, LABEL_MANUAL)):
            self.status.setText("That point is already labelled.")
            return None
        self._remember(exp.uid, [index])
        self.refresh_labels(exp.uid)
        self.status.setText(f"Labelled point {index} of {exp.display_name}.")
        return index

    def _remember(self, uid, indices):
        self._undo.append((uid, indices))
        del self._undo[:-UNDO_LIMIT]
        self.undo_action.setEnabled(True)

    def undo_labels(self):
        """Take back the most recent batch of new labels (one box, or one click)."""
        while self._undo:
            uid, indices = self._undo.pop()
            exp = self.state.experiment(uid)
            if exp is None:
                continue
            removed = [i for i in indices if exp.remove_label(i)]
            if removed:
                self.refresh_labels(uid)
                self.status.setText(f"Removed {len(removed)} label{'s' if len(removed) != 1 else ''}.")
                break
        self.undo_action.setEnabled(bool(self._undo))

    def open_peak_settings(self):
        dialog = PeakSettingsDialog(self.state.peak_settings, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self.state.peak_settings = dialog.settings()
            self.changed.emit()

    # ------------------------------------------------------------------
    # View
    # ------------------------------------------------------------------

    def fit_view(self):
        visible = [self._drawn[e.uid].curve for e in self.state.experiments
                   if e.visible and e.uid in self._drawn]
        if visible:
            self.plot_item.getViewBox().autoRange(items=visible)
        self.state.view_range = None
        self.view_changed.emit()

    def _on_view_moved(self, *_args):
        (x0, x1), (y0, y1) = self.plot_item.getViewBox().viewRange()
        self.state.view_range = (x0, x1, y0, y1)
        self.view_changed.emit()

    def _restore_view(self):
        if self.state.view_range is not None:
            x0, x1, y0, y1 = self.state.view_range
            self.plot_item.getViewBox().setRange(xRange=(x0, x1), yRange=(y0, y1), padding=0)
        elif self.state.experiments:
            self.fit_view()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def figure_layout(self):
        """The plot's size and plot area as they are on screen, for a figure that matches."""
        viewport = self.plot_widget.viewport()
        width, height = viewport.width(), viewport.height()
        if width <= 0 or height <= 0:
            return None
        dpi = viewport.logicalDpiX()
        area = self.plot_widget.mapFromScene(self.view_box.sceneBoundingRect()).boundingRect()
        return FigureLayout(width / dpi, height / dpi,
                            (area.left() / width, 1 - (area.top() + area.height()) / height,
                             area.width() / width, area.height() / height))

    def _figure(self):
        """The plot redrawn as a figure: the screen's size, layout, view and legend place."""
        self.capture_legend()
        (x0, x1), (y0, y1) = self.view_box.viewRange()
        return render_figure(self.state, self._datasets, layout=self.figure_layout(),
                             view_range=(x0, x1, y0, y1))

    def _file_stem(self):
        return re.sub(r'[\\/:*?"<>|]', "_", self.title).strip() or "Plot"

    def export_excel(self, path=None):
        """Write this window to an .xlsx file; returns whether it was written."""
        if path is None:
            path = self.ask_save_path("Export to Excel", f"{self._file_stem()}.xlsx",
                                      "Excel workbook (*.xlsx)")
            if not path:
                return False
        path = Path(path)
        if path.suffix.lower() != ".xlsx":
            path = path.with_name(path.name + ".xlsx")
        origins = {}
        for exp in self.state.experiments:
            if exp.uid in self._datasets:
                try:
                    origins[exp.uid] = [o["original_path"]
                                        for o in self.store.workspace.origins(exp.file)]
                except (OSError, WorkspaceError):
                    pass
        try:
            export_workbook(path, self.state, self._datasets, figure_png(self._figure()),
                            origins=origins, missing=self.missing_files())
        except (OSError, ValueError) as e:
            self.show_error("Export Failed", str(e))
            return False
        count = len(self._datasets)
        self.status.setText(f"Exported {count} experiment{'s' if count != 1 else ''} "
                            f"to {path.name}.")
        if self.offer_image():
            self.save_image()
        return True

    def save_image(self, path=None):
        """Save the plot as a PNG or SVG figure; returns whether it was written."""
        if path is None:
            path = self.ask_save_path("Save Plot Image", f"{self._file_stem()}.png",
                                      "PNG image (*.png);;SVG image (*.svg)")
            if not path:
                return False
        path = Path(path)
        if path.suffix.lower() not in (".png", ".svg"):
            path = path.with_name(path.name + ".png")
        try:
            save_figure(self._figure(), path)
        except (OSError, ValueError) as e:
            self.show_error("Image Not Saved", str(e))
            return False
        self.status.setText(f"Saved the plot as {path.name}.")
        return True

    # ------------------------------------------------------------------
    # Drops and dialogs
    # ------------------------------------------------------------------

    def _on_dropped(self, rels, outside):
        if rels:
            self.add_files(rels)
        elif outside:
            self.outside_files_dropped.emit(self, outside)

    def confirm(self, title, text):
        answer = QtWidgets.QMessageBox.question(
            self, title, text, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return answer == QtWidgets.QMessageBox.Yes

    def show_error(self, title, text):
        QtWidgets.QMessageBox.warning(self, title, text)

    # Folder the last export went to, shared by every plot window.
    _export_folder = None

    def ask_save_path(self, title, suggested, filters):
        folder = PlotPanel._export_folder or str(self.store.workspace.root)
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, title, str(Path(folder) / suggested), filters)
        if path:
            PlotPanel._export_folder = str(Path(path).parent)
        return path

    def offer_image(self):
        return self.confirm("Save Plot Image",
                            "Also save the plot on its own as a PNG or SVG image?")

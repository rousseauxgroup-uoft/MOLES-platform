"""Peak picking, manual labels, and draggable label text in a plot window."""

import json

import numpy as np
import pytest

CV_HEADER = "Time (s),Potential (V),Current (uA),Cycle,Index,Target V,Range"
PEAK_V = 0.3


def _write_sweep(path, peak_uA=50.0, seed=0):
    """A positive-going sweep at real MOLES density with one oxidation peak."""
    rng = np.random.default_rng(seed)
    target = np.arange(-0.5, 1.0, 0.0004)
    n = len(target)
    current = peak_uA * np.exp(-(target - PEAK_V) ** 2 / (2 * 0.05 ** 2)) + rng.normal(0, 0.7, n)
    rows = np.column_stack([np.arange(n) * 0.004, target, current, np.zeros(n),
                            np.zeros(n), target, np.full(n, 3)])
    np.savetxt(path, rows, delimiter=",", header=CV_HEADER, comments="", fmt="%.6f")


@pytest.fixture
def panel(qapp, tmp_path):
    from moles.analysis.model import PlotWindowState
    from moles.analysis.store import DatasetStore
    from moles.analysis.workspace import Workspace
    from moles.ui.data_analysis.plot_panel import PlotPanel

    root = tmp_path / "ws"
    root.mkdir()
    ws = Workspace(root)
    _write_sweep(root / "a.csv")
    _write_sweep(root / "b.csv", peak_uA=30.0, seed=1)
    p = PlotPanel(PlotWindowState("Plot 1"), DatasetStore(ws))
    p.show_error = lambda title, text: None
    p.confirm = lambda title, text: True
    p.resize(1200, 700)
    p.show()
    qapp.processEvents()
    yield p
    p.close()
    p.deleteLater()


def _box(x0, x1, y0, y1):
    from PyQt5 import QtCore
    return QtCore.QRectF(QtCore.QPointF(x0, y0), QtCore.QPointF(x1, y1)).normalized()


def _point(x, y):
    from PyQt5 import QtCore
    return QtCore.QPointF(x, y)


def _labels(panel, uid):
    exp = panel.state.experiment(uid)
    ds = panel._datasets[uid]
    return [(lp.kind, lp.extremum, round(float(ds.potential[lp.index]), 2)) for lp in exp.labels]


# ----------------------------------------------------------------------
# Box picking
# ----------------------------------------------------------------------

def test_a_box_over_the_selected_trace_labels_its_peak(panel):
    (a,) = panel.add_files(["a.csv"])

    added = panel.pick_in_box(_box(0.1, 0.5, 20, 60))

    assert len(added) == 1
    assert _labels(panel, a) == [("peak", "max", PEAK_V)]
    assert "Found 1 peak" in panel.status.text()
    assert len(panel._drawn[a].labels) == 1 and "peak, max" in \
        panel._cards[a].labels_box.itemAt(0).widget().findChildren(type(panel.status))[0].text()


def test_picking_the_same_box_again_adds_nothing(panel):
    panel.add_files(["a.csv"])
    panel.pick_in_box(_box(0.1, 0.5, 20, 60))

    assert panel.pick_in_box(_box(0.1, 0.5, 20, 60)) == []
    assert "already labelled" in panel.status.text()


def test_picking_needs_a_selected_visible_trace(panel):
    a, b = panel.add_files(["a.csv", "b.csv"])
    panel.select(None)

    assert panel.pick_in_box(_box(0.1, 0.5, 0, 60)) == []
    assert "Select a trace first" in panel.status.text()

    panel.select(a)
    panel._cards[a].visible_cb.setChecked(False)
    assert panel.pick_in_box(_box(0.1, 0.5, 0, 60)) == []
    assert "hidden" in panel.status.text()


def test_a_lone_trace_is_picked_without_selecting_it(panel):
    (a,) = panel.add_files(["a.csv"])
    panel.select(None)

    assert panel.pick_in_box(_box(0.1, 0.5, 20, 60))
    assert panel.state.selected_uid == a


def test_only_the_selected_trace_is_picked(panel):
    a, b = panel.add_files(["a.csv", "b.csv"])
    panel.select(b)

    panel.pick_in_box(_box(0.1, 0.5, 0, 60))

    assert _labels(panel, a) == [] and _labels(panel, b) == [("peak", "max", PEAK_V)]


def test_the_box_uses_displayed_coordinates_and_the_data_range(panel):
    (a,) = panel.add_files(["a.csv"])
    card = panel._cards[a]
    card.x_spin.setValue(-0.4)           # the peak is drawn at -0.1 V
    card.y_spin.setValue(100.0)          # and 150 µA

    assert panel.pick_in_box(_box(0.1, 0.5, 20, 60)) == []      # where it was
    assert len(panel.pick_in_box(_box(-0.2, 0.0, 120, 160))) == 1

    exp = panel.state.experiment(a)
    exp.remove_label(exp.labels[0].index)
    card.start_spin.setValue(int(np.argmin(abs(panel._datasets[a].potential - 0.6))))
    assert panel.pick_in_box(_box(-0.2, 0.0, 120, 160)) == []   # peak outside the range


def test_box_with_none_of_the_trace_says_so(panel):
    panel.add_files(["a.csv"])

    assert panel.pick_in_box(_box(0.1, 0.5, 500, 600)) == []
    assert "holds none" in panel.status.text()


def test_undo_takes_back_the_last_batch(panel):
    (a,) = panel.add_files(["a.csv"])
    panel.pick_in_box(_box(0.1, 0.5, 20, 60))
    panel.label_at(_point(-0.2, 0.0))

    panel.undo_labels()
    assert _labels(panel, a) == [("peak", "max", PEAK_V)]
    panel.undo_labels()
    assert _labels(panel, a) == []
    assert not panel.undo_action.isEnabled()


def test_picked_labels_can_be_saved(panel):
    from moles.analysis.model import AnalysisSession
    panel.add_files(["a.csv"])
    panel.pick_in_box(_box(0.1, 0.5, 20, 60))

    text = AnalysisSession(windows=[panel.state]).to_json()

    assert json.loads(text)["windows"][0]["experiments"][0]["labels"][0]["kind"] == "peak"


# ----------------------------------------------------------------------
# Manual labels
# ----------------------------------------------------------------------

def test_a_click_labels_the_nearest_point(panel):
    (a,) = panel.add_files(["a.csv"])
    ds = panel._datasets[a]
    i = 1000

    click = (ds.potential[i], ds.current[i] + 0.5)
    index = panel.label_at(_point(*click))

    # The snapped point is the one closest to the click on screen.
    sx, sy = panel.view_box.viewPixelSize()
    on_screen = np.hypot((ds.potential - click[0]) / sx, (ds.current - click[1]) / sy)
    assert index == int(np.argmin(on_screen))
    assert panel.state.experiment(a).labels[0].kind == "manual"
    assert panel.label_at(_point(ds.potential[index], ds.current[index])) is None  # already labelled


def test_a_click_far_from_the_trace_labels_nothing(panel):
    (a,) = panel.add_files(["a.csv"])

    assert panel.label_at(_point(0.3, -40.0)) is None
    assert "Click within" in panel.status.text()
    assert panel.state.experiment(a).labels == []


def test_clicking_a_trace_in_label_mode_selects_and_labels_it(panel):
    from moles.ui.data_analysis.interaction import MODE_LABEL

    a, b = panel.add_files(["a.csv", "b.csv"])
    panel.set_mode(MODE_LABEL)
    ds = panel._datasets[a]
    scene = panel.view_box.mapViewToScene(_point(ds.potential[500], ds.current[500]))

    class Click:
        def scenePos(self):
            return scene

    curve = panel._drawn[a].curve
    curve.sigClicked.emit(curve, Click())

    assert panel.state.selected_uid == a
    assert len(panel.state.experiment(a).labels) == 1


# ----------------------------------------------------------------------
# Modes and the view box
# ----------------------------------------------------------------------

class _Drag:
    """Just enough of a pyqtgraph drag event for the view box."""

    def __init__(self, start, end, finish):
        from pyqtgraph import Point
        self._start, self._end, self._finish = Point(start), Point(end), finish

    def button(self):
        from PyQt5 import QtCore
        return QtCore.Qt.LeftButton

    def isFinish(self):
        return self._finish

    def buttonDownPos(self, _button=None):
        return self._start

    def pos(self):
        return self._end

    def lastPos(self):
        return self._end

    def accept(self):
        pass


def test_box_mode_turns_a_drag_into_a_box(panel):
    from moles.ui.data_analysis.interaction import MODE_BOX, MODE_PAN
    vb = panel.view_box
    boxes = []
    vb.box_drawn.connect(boxes.append)
    start = vb.childGroup.mapToParent(_point(0.1, 10))
    end = vb.childGroup.mapToParent(_point(0.5, 40))

    panel.set_mode(MODE_PAN)
    vb.mouseDragEvent(_Drag(start, end, True))
    assert boxes == []

    panel.set_mode(MODE_BOX)
    vb.mouseDragEvent(_Drag(start, end, False))
    vb.mouseDragEvent(_Drag(start, end, True))
    assert len(boxes) == 1
    rect = boxes[0]
    assert rect.left() == pytest.approx(0.1, abs=1e-6) and rect.right() == pytest.approx(0.5, abs=1e-6)
    assert rect.top() == pytest.approx(10, abs=1e-4) and rect.bottom() == pytest.approx(40, abs=1e-4)


def test_mode_keys_and_escape(panel):
    from PyQt5 import QtCore, QtGui, QtWidgets
    from moles.ui.data_analysis.interaction import MODE_BOX, MODE_PAN
    (a,) = panel.add_files(["a.csv"])

    panel.mode_actions[MODE_BOX].trigger()
    assert panel.mode == MODE_BOX and panel.view_box.mode == MODE_BOX
    assert panel.mode_actions["box"].shortcut().toString() == "B"

    escape = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Escape, QtCore.Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(panel, escape)
    assert panel.mode == MODE_PAN and panel.state.selected_uid == a
    QtWidgets.QApplication.sendEvent(panel, escape)
    assert panel.state.selected_uid is None


def test_peak_settings_dialog_edits_the_window_settings(panel):
    from moles.analysis.model import PeakSettings
    from moles.ui.data_analysis.interaction import PeakSettingsDialog

    dialog = PeakSettingsDialog(PeakSettings(), panel)
    dialog.smoothing.setValue(20.0)
    dialog.fixed.setChecked(True)
    dialog.fixed_value.setValue(4.5)
    dialog.valleys.setChecked(True)
    edited = dialog.settings()

    assert (edited.smoothing_mV, edited.prominence_uA, edited.report_valleys) == (20.0, 4.5, True)
    dialog._show(PeakSettings())
    assert dialog.settings() == PeakSettings()


def test_a_high_fixed_prominence_finds_nothing(panel):
    from moles.analysis.model import PeakSettings
    panel.add_files(["a.csv"])
    panel.state.peak_settings = PeakSettings(prominence_uA=80.0)

    assert panel.pick_in_box(_box(0.1, 0.5, 20, 60)) == []
    assert "threshold 80.00 µA" in panel.status.text()


# ----------------------------------------------------------------------
# Draggable label text
# ----------------------------------------------------------------------

def test_dragging_label_text_stores_its_offset_in_points(panel):
    from moles.ui.data_analysis.interaction import points_to_pixels
    (a,) = panel.add_files(["a.csv"])
    panel.pick_in_box(_box(0.1, 0.5, 20, 60))
    dl = panel._drawn[a].labels[0]
    px_per_pt = points_to_pixels(panel.plot_widget, 1.0)

    panel._on_label_dragged(a, dl, (60 * px_per_pt, -30 * px_per_pt), True)

    label = panel.state.experiment(a).labels[0]
    assert label.text_offset == pytest.approx((60.0, 30.0))    # right and up
    assert dl.leader.isVisible()
    text_scene = dl.text.pos()
    assert text_scene.x() > dl.point[0] and text_scene.y() > dl.point[1]


def test_label_text_follows_a_real_drag(panel):
    from PyQt5 import QtCore
    (a,) = panel.add_files(["a.csv"])
    panel.pick_in_box(_box(0.1, 0.5, 20, 60))
    dl = panel._drawn[a].labels[0]
    text = dl.text
    centre = text.mapToScene(text.boundingRect().center())
    moves = []
    text._moved = lambda offset, done: moves.append((offset, done))

    class Drag:
        def __init__(self, dx, start, finish):
            self.dx, self.start, self.finish = dx, start, finish

        def button(self):
            return QtCore.Qt.LeftButton

        def accept(self):
            pass

        def isStart(self):
            return self.start

        def isFinish(self):
            return self.finish

        def buttonDownScenePos(self):
            return centre

        def scenePos(self):
            return centre + QtCore.QPointF(self.dx, 0)

    text.mouseDragEvent(Drag(0, True, False))
    text.mouseDragEvent(Drag(40, False, True))

    point = panel.view_box.mapViewToScene(QtCore.QPointF(*dl.point))
    (dx0, _), _ = moves[0]
    (dx1, _), done = moves[-1]
    assert done and dx1 - dx0 == pytest.approx(40, abs=0.5)
    assert dx0 == pytest.approx(centre.x() - point.x(), abs=0.5)


def test_reset_and_delete_from_the_label_menu(panel):
    (a,) = panel.add_files(["a.csv"])
    panel.pick_in_box(_box(0.1, 0.5, 20, 60))
    label = panel.state.experiment(a).labels[0]
    label.text_offset = (50.0, 50.0)

    panel._reset_label(a, label)
    assert label.text_offset is None and not panel._drawn[a].labels[0].leader.isVisible()

    panel.delete_label(a, label.index)
    assert panel.state.experiment(a).labels == [] and panel._drawn[a].labels == []


def test_label_offsets_survive_a_session_round_trip():
    from moles.analysis.model import AnalysisSession, LabelledPoint, PlotWindowState, ExperimentView

    exp = ExperimentView(file="a.csv", sha256="x", display_name="a", n_points=100)
    exp.add_label(LabelledPoint(np.int64(5), "peak", "max", text_offset=(12, -30)))
    session = AnalysisSession(windows=[PlotWindowState("Plot 1", experiments=[exp])])

    restored = AnalysisSession.from_json(session.to_json())

    label = restored.windows[0].experiments[0].labels[0]
    assert label.index == 5 and label.text_offset == (12.0, -30.0)
    assert LabelledPoint(1, "peak", "min").offset_pt()[1] < 0   # minima: text below by default


def test_box_pick_works_on_the_smoothed_trace(panel):
    from moles.analysis.smoothing import moving_average
    (a,) = panel.add_files(["a.csv"])
    exp = panel.state.experiment(a)
    panel._cards[a].smooth_spin.setValue(51)
    smoothed = moving_average(panel._datasets[a].current, 51)

    panel.pick_in_box(_box(0.1, 0.5, 20, 60))

    label = exp.labels[0]
    assert exp.displayed_point(panel._datasets[a], label.index)[1] == pytest.approx(smoothed[label.index])
    # The reported point is a smoothed-curve maximum within the smoothing's reach.
    near = smoothed[label.index - 40:label.index + 41]
    assert smoothed[label.index] >= near.max() - 0.5


# ----------------------------------------------------------------------
# Clearing the selection
# ----------------------------------------------------------------------

class _Click:
    """Just enough of a pyqtgraph click event."""

    def __init__(self, scene_pos=None, button=None):
        from PyQt5 import QtCore
        self._pos = scene_pos or QtCore.QPointF(0, 0)
        self._button = button or QtCore.Qt.LeftButton
        self.accepted = None

    def button(self):
        return self._button

    def scenePos(self):
        return self._pos

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


@pytest.mark.parametrize("mode", ["pan", "box"])
def test_a_click_on_empty_space_clears_the_selection(panel, mode):
    (a,) = panel.add_files(["a.csv"])
    panel.set_mode(mode)
    assert panel.state.selected_uid == a and panel._drawn[a].glow.isVisible()

    panel.view_box.mouseClickEvent(_Click())

    assert panel.state.selected_uid is None and not panel._drawn[a].glow.isVisible()


def test_in_label_mode_a_click_labels_instead(panel):
    from moles.ui.data_analysis.interaction import MODE_LABEL
    (a,) = panel.add_files(["a.csv"])
    panel.set_mode(MODE_LABEL)
    far = panel.view_box.mapViewToScene(_point(0.3, -40.0))

    panel.view_box.mouseClickEvent(_Click(far))

    assert panel.state.selected_uid == a
    assert "Click within" in panel.status.text()


def test_clicking_a_label_selects_its_trace(panel):
    a, b = panel.add_files(["a.csv", "b.csv"])
    panel.select(a)
    panel.pick_in_box(_box(0.1, 0.5, 20, 60))
    panel.select(b)
    click = _Click()

    panel._drawn[a].labels[0].text.mouseClickEvent(click)

    assert click.accepted and panel.state.selected_uid == a


def test_clicking_the_legend_does_not_hide_a_trace(panel):
    from moles.ui.data_analysis.interaction import StillLegendSample
    (a,) = panel.add_files(["a.csv"])
    sample, _label = panel.legend.items[0]
    click = _Click()

    sample.mouseClickEvent(click)

    assert isinstance(sample, StillLegendSample) and click.accepted is False
    assert panel._drawn[a].curve.isVisible() and panel.state.experiment(a).visible


def test_real_clicks_clear_and_restore_the_selection(panel, qapp):
    from PyQt5 import QtCore
    from PyQt5.QtTest import QTest
    (a,) = panel.add_files(["a.csv"])
    ds = panel._datasets[a]

    def click(x, y):
        scene = panel.view_box.mapViewToScene(_point(x, y))
        QTest.mouseClick(panel.plot_widget.viewport(), QtCore.Qt.LeftButton,
                         QtCore.Qt.NoModifier, panel.plot_widget.mapFromScene(scene))
        qapp.processEvents()

    click(0.8, 40.0)                                    # empty space
    assert panel.state.selected_uid is None

    click(float(ds.potential[2000]), float(ds.current[2000]))   # on the trace
    assert panel.state.selected_uid == a

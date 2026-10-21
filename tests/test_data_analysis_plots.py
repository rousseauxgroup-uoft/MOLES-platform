"""Plot windows of the Data Analysis app: slider, cards, plot panel, pop-outs."""

import os
from pathlib import Path

import numpy as np
import pytest

from moles.analysis.display import DISPLAY_POINTS, every_nth, thin
from moles.analysis.model import LabelledPoint, MIN_RANGE_POINTS

CV_HEADER = "Time (s),Potential (V),Current (uA),Cycle,Index,Target V,Range"


def _write_cv(path, n=2000, scale=1.0):
    half = n // 2
    target = np.concatenate([np.linspace(0, -1, half), np.linspace(-1, 0, n - half)])
    current = scale * (np.sin(np.linspace(0, 4 * np.pi, n)) * 40)
    rows = np.column_stack([np.arange(n) * 0.004, target, current, np.zeros(n),
                            np.zeros(n), target, np.full(n, 3)])
    np.savetxt(path, rows, delimiter=",", header=CV_HEADER, comments="", fmt="%.6f")


# ----------------------------------------------------------------------
# Display thinning (no Qt)
# ----------------------------------------------------------------------

def test_thinning_keeps_every_extreme_in_order():
    rng = np.random.default_rng(0)
    x = np.arange(400_000, dtype=float)
    y = rng.normal(0, 1, len(x))
    y[123_457] = 50.0
    y[300_001] = -60.0

    tx, ty = thin(x, y)

    assert len(tx) <= DISPLAY_POINTS + 16
    assert ty.max() == 50.0 and ty.min() == -60.0
    assert np.all(np.diff(tx) > 0)        # still in point order
    assert tx[-1] == x[-1]


def test_every_nth_keeps_both_ends():
    x = np.arange(10_001, dtype=float)

    tx, _ty = every_nth(x, x, max_points=2000)

    assert tx[0] == 0 and tx[-1] == 10_000 and len(tx) <= 2002


def test_store_shares_loads_and_follows_moves(tmp_path):
    from moles.analysis.store import DatasetStore
    from moles.analysis.workspace import Workspace

    (tmp_path / "ws").mkdir()
    ws = Workspace(tmp_path / "ws")
    _write_cv(ws.root / "a.csv")
    store = DatasetStore(ws)

    first = store.get("a.csv")
    assert store.get("a.csv") is first
    ws.make_folder("", "f")
    store.moved(ws.move("a.csv", "f").moved)
    assert store.get("f/a.csv") is first
    _write_cv(ws.root / "f" / "a.csv", scale=2.0)
    os.utime(ws.root / "f" / "a.csv", ns=(1, 1))
    assert store.get("f/a.csv") is not first   # a changed copy is reloaded


# ----------------------------------------------------------------------
# Range slider
# ----------------------------------------------------------------------

def _mouse(widget, kind, x):
    from PyQt5 import QtCore, QtGui, QtWidgets
    types = {"press": QtCore.QEvent.MouseButtonPress, "move": QtCore.QEvent.MouseMove,
             "release": QtCore.QEvent.MouseButtonRelease}
    button = QtCore.Qt.NoButton if kind == "move" else QtCore.Qt.LeftButton
    event = QtGui.QMouseEvent(types[kind], QtCore.QPointF(x, widget.height() / 2),
                              button, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(widget, event)


@pytest.fixture
def slider(qapp):
    from moles.ui.data_analysis.range_slider import IndexRangeSlider
    s = IndexRangeSlider(1000)
    s.resize(400, 30)
    s.setValues(200, 600)
    emitted = []
    s.rangeChanged.connect(lambda lo, hi: emitted.append((lo, hi)))
    s.emitted = emitted
    return s


def _centre(slider, which):
    return slider.handle_rect(which).center().x()


def test_pressing_or_dragging_the_track_does_nothing(slider):
    for x in (slider._px(400), slider._px(50), slider._px(900), 2):
        _mouse(slider, "press", x)
        _mouse(slider, "move", x + 60)
        _mouse(slider, "release", x + 60)

    assert slider.values() == (200, 600) and slider.emitted == []


def test_dragging_a_handle_moves_it(slider):
    start = _centre(slider, "low")
    _mouse(slider, "press", start)
    _mouse(slider, "move", start + (slider._px(300) - slider._px(200)))
    _mouse(slider, "release", start)

    low, high = slider.values()
    assert abs(low - 300) <= 3 and high == 600
    assert slider.emitted


def test_handles_cannot_cross_or_come_too_close(slider):
    start = _centre(slider, "low")
    _mouse(slider, "press", start)
    _mouse(slider, "move", start + 1000)
    _mouse(slider, "release", start + 1000)
    assert slider.values() == (600 - (MIN_RANGE_POINTS - 1), 600)

    start = _centre(slider, "high")
    _mouse(slider, "press", start)
    _mouse(slider, "move", -1000)
    _mouse(slider, "release", -1000)
    low, high = slider.values()
    assert high - low == MIN_RANGE_POINTS - 1


def test_handles_stay_separate_even_at_the_same_pixel(qapp):
    from moles.ui.data_analysis.range_slider import IndexRangeSlider
    s = IndexRangeSlider(20_000)
    s.resize(300, 30)
    s.setValues(5000, 5000 + MIN_RANGE_POINTS - 1)   # a few points: same pixel

    low, high = s.handle_rect("low"), s.handle_rect("high")

    assert not low.intersects(high)
    assert s._hit(low.center().toPoint()) == "low"
    assert s._hit(high.center().toPoint()) == "high"


# ----------------------------------------------------------------------
# Plot panel and experiment cards
# ----------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    from moles.analysis.workspace import Workspace
    root = tmp_path / "ws"
    root.mkdir()
    ws = Workspace(root)
    _write_cv(root / "a.csv")
    _write_cv(root / "b.csv", scale=0.5)
    return ws


@pytest.fixture
def panel(qapp, workspace):
    from moles.analysis.model import PlotWindowState
    from moles.analysis.store import DatasetStore
    from moles.ui.data_analysis.plot_panel import PlotPanel
    p = PlotPanel(PlotWindowState("Plot 1"), DatasetStore(workspace))
    p.errors = []
    p.show_error = lambda title, text: p.errors.append(text)
    p.confirm = lambda title, text: True
    p.resize(1200, 700)
    yield p
    p.deleteLater()


def _flush(panel):
    from PyQt5 import QtCore
    panel._flush()
    QtCore.QCoreApplication.processEvents()


def _legend_names(panel):
    return [label.text for _sample, label in panel.legend.items]


def test_added_files_get_a_curve_a_card_and_a_legend_entry(panel):
    uids = panel.add_files(["a.csv", "b.csv"])

    assert len(uids) == 2 and set(panel._cards) == set(uids)
    assert _legend_names(panel) == ["a", "b"]
    assert panel.state.selected_uid == uids[-1]   # the last added is selected
    assert len(panel._drawn[uids[0]].curve.xData) == 2000


def test_only_the_selected_trace_glows(panel):
    a, b = panel.add_files(["a.csv", "b.csv"])

    _mouse(panel._cards[a], "press", 5)       # clicking a card selects its trace

    assert panel.state.selected_uid == a
    assert panel._drawn[a].glow.isVisible() and not panel._drawn[b].glow.isVisible()
    assert panel._drawn[a].curve.zValue() > panel._drawn[b].curve.zValue()

    curve = panel._drawn[b].curve
    curve.sigClicked.emit(curve, None)         # and so does clicking the trace

    assert panel.state.selected_uid == b and panel._drawn[b].glow.isVisible()


def test_card_edits_change_only_this_window(panel, qapp, workspace):
    from moles.analysis.model import PlotWindowState
    from moles.ui.data_analysis.plot_panel import PlotPanel
    other = PlotPanel(PlotWindowState("Plot 2"), panel.store)
    (a,) = panel.add_files(["a.csv"])
    (a2,) = other.add_files(["a.csv"])
    card = panel._cards[a]

    card.name_edit.setText("Ferrocene")
    card.name_edit.textEdited.emit("Ferrocene")
    card.x_spin.setValue(0.25)
    card.y_spin.setValue(-5.0)
    card.style_combo.setCurrentIndex(card.style_combo.findData("dot"))
    _flush(panel)

    exp = panel.state.experiment(a)
    assert (exp.display_name, exp.x_offset, exp.y_offset, exp.line_style) == \
        ("Ferrocene", 0.25, -5.0, "dot")
    assert _legend_names(panel) == ["Ferrocene"]
    dataset = panel.store.get("a.csv")
    np.testing.assert_allclose(panel._drawn[a].curve.xData[:3], dataset.potential[:3] + 0.25)
    assert panel._drawn[a].curve.opts["pen"].dashPattern() == [1.0, 2.0]   # as exported
    untouched = other.state.experiment(a2)
    assert (untouched.display_name, untouched.x_offset, untouched.line_style) == ("a", 0.0, "solid")
    other.deleteLater()


def test_range_spin_boxes_slider_and_model_agree(panel):
    (a,) = panel.add_files(["a.csv"])
    card = panel._cards[a]

    card.start_spin.setValue(100)
    card.end_spin.setValue(105)   # too short: pushed out to the minimum span
    _flush(panel)

    exp = panel.state.experiment(a)
    assert (exp.range_start, exp.range_end) == (100, 100 + MIN_RANGE_POINTS - 1)
    assert card.slider.values() == (exp.range_start, exp.range_end)
    assert card.end_spin.value() == exp.range_end
    assert len(panel._drawn[a].curve.xData) == MIN_RANGE_POINTS


def test_labels_are_listed_drawn_and_hidden_outside_the_range(panel):
    (a,) = panel.add_files(["a.csv"])
    exp = panel.state.experiment(a)
    exp.add_label(LabelledPoint(50, "peak", "max"))
    exp.add_label(LabelledPoint(1500, "manual"))
    panel.refresh_labels(a)
    card = panel._cards[a]

    assert [dl.label.index for dl in panel._drawn[a].labels] == [50, 1500]
    card.start_spin.setValue(1000)
    _flush(panel)
    assert [dl.label.index for dl in panel._drawn[a].labels] == [1500]
    texts = [card.labels_box.itemAt(i).widget().findChild(type(card.start_readout)).text()
             for i in range(card.labels_box.count())]
    assert "outside data range" in texts[0] and "outside data range" not in texts[1]


def test_deleting_a_label_from_its_card(panel):
    (a,) = panel.add_files(["a.csv"])
    exp = panel.state.experiment(a)
    exp.add_label(LabelledPoint(50, "peak", "max"))
    panel.refresh_labels(a)

    panel._cards[a]._delete_label(50)

    assert exp.labels == [] and panel._drawn[a].labels == []


def test_hidden_experiments_leave_the_plot_and_legend(panel):
    a, _b = panel.add_files(["a.csv", "b.csv"])

    panel._cards[a].visible_cb.setChecked(False)

    assert not panel._drawn[a].curve.isVisible()
    assert _legend_names(panel) == ["b"]


def test_removing_an_experiment(panel):
    a, b = panel.add_files(["a.csv", "b.csv"])

    assert panel.remove_experiment(a)

    assert [e.uid for e in panel.state.experiments] == [b]
    assert a not in panel._cards and _legend_names(panel) == ["b"]
    assert not panel.uses("a.csv") and panel.uses("b.csv")


def test_unplottable_files_are_reported(panel, workspace):
    (workspace.root / "ocp.csv").write_text("Time (s),Potential (V)\n0,0.3\n1,0.31\n")

    assert panel.add_files(["ocp.csv"]) == []
    assert panel.errors and "OCP" in panel.errors[0]


def test_long_traces_are_thinned_for_display(panel, workspace):
    _write_cv(workspace.root / "long.csv", n=120_000)

    (uid,) = panel.add_files(["long.csv"])

    assert len(panel._drawn[uid].curve.xData) <= DISPLAY_POINTS + 16


def test_axes_follow_the_figure_style(panel):
    left, top = panel.plot_item.getAxis("left"), panel.plot_item.getAxis("top")

    assert left.style["tickLength"] > 0            # ticks point out of the frame
    assert top.isVisible() and not top.style["showValues"]
    assert "Current" in left.labelText and left.labelUnits == ""
    assert "µA" in left.labelString()
    assert "Potential" in panel.plot_item.getAxis("bottom").labelString()


def test_dropping_workspace_files_adds_them(panel):
    panel._drop_filter.dropped.emit(["a.csv"], [])

    assert [e.file for e in panel.state.experiments] == ["a.csv"]


def test_dropping_outside_files_asks_for_workspace_copies(panel):
    asked = []
    panel.outside_files_dropped.connect(lambda p, paths: asked.append(paths))

    panel._drop_filter.dropped.emit([], ["/somewhere/run.csv"])

    assert asked == [["/somewhere/run.csv"]] and panel.state.experiments == []


# ----------------------------------------------------------------------
# Main window: tabs, pop-outs, Explorer hooks
# ----------------------------------------------------------------------

def _fail_on_error(panel, title, text):
    raise AssertionError(f"{title}: {text}")


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    from PyQt5 import QtCore
    import moles.analysis.workspace as workspace_module
    from moles.ui.data_analysis.main_window import DataAnalysisWindow
    from moles.ui.data_analysis.plot_panel import PlotPanel

    raw = tmp_path / "raw"
    raw.mkdir()
    _write_cv(raw / "run.csv")
    (raw / "run.params.txt").write_text("method: CV\nmV_s: 100.0\n")
    monkeypatch.setattr(workspace_module, "moles_data_folders", lambda: [raw])
    from moles.ui.data_analysis.main_window import DISCARD
    win = DataAnalysisWindow(QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat))
    win.ask_save_changes = lambda: DISCARD
    win.report_relink = lambda report: None
    win.explorer.show_error = lambda title, text: None
    monkeypatch.setattr(PlotPanel, "show_error", _fail_on_error)
    folder = tmp_path / "Analysis"
    folder.mkdir()
    assert win.open_workspace(folder)
    win.raw = raw
    yield win
    win.close()


def test_a_new_workspace_starts_with_one_empty_plot(window):
    area = window.plot_area

    assert [p.title for p in area.panels] == ["Plot 1"]
    assert area.new_plot().title == "Plot 2" and area.tabs.count() == 2


def test_popping_out_and_docking_back(window, qapp):
    from moles.ui.data_analysis.plot_area import PopOutWindow
    area = window.plot_area
    panel = area.panels[0]

    area.pop_out(panel)

    assert area.tabs.count() == 0 and isinstance(panel.window(), PopOutWindow)
    assert panel.state.popped_out and panel.pop_action.text() == "Dock Back"

    area.popout_window(panel).close()      # closing the window docks it back
    qapp.processEvents()

    assert area.tabs.count() == 1 and area.tabs.widget(0) is panel
    assert not panel.state.popped_out and panel.state.geometry is not None


def test_closing_a_plot_with_experiments_asks_first(window):
    area = window.plot_area
    panel = area.panels[0]
    rel = window.explorer.add_paths([window.raw / "run.csv"])[0]
    panel.add_files([rel])

    panel.confirm = lambda title, text: False
    assert not area.close_plot(panel)
    panel.confirm = lambda title, text: True
    assert area.close_plot(panel) and area.panels == []


def test_plotted_files_cannot_be_removed_and_follow_renames(window):
    explorer, panel = window.explorer, window.plot_area.panels[0]
    rel = explorer.add_paths([window.raw / "run.csv"])[0]
    panel.add_files([rel])

    assert explorer.in_use(rel)
    explorer.rename(rel, "Ferrocene")

    assert panel.state.experiments[0].file == "Ferrocene.csv"
    assert panel.store.get("Ferrocene.csv") is not None
    assert explorer.in_use("Ferrocene.csv") and not explorer.in_use(rel)


def test_double_clicked_files_open_in_the_current_plot(window):
    rel = window.explorer.add_paths([window.raw / "run.csv"])[0]

    window.explorer.open_requested.emit([rel])

    assert [e.file for e in window.plot_area.panels[0].state.experiments] == [rel]


def test_outside_files_dropped_on_a_plot_use_workspace_copies(window, tmp_path):
    panel = window.plot_area.panels[0]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write_cv(elsewhere / "new.csv", scale=3.0)
    rel = window.explorer.add_paths([window.raw / "run.csv"])[0]
    before = (window.raw / "run.csv").read_bytes()

    window._add_outside_files(panel, [str(window.raw / "run.csv"), str(elsewhere / "new.csv")])

    files = [e.file for e in panel.state.experiments]
    assert files == [rel, "new.csv"]                    # existing copy reused, new one copied
    assert sorted(window.workspace.data_files()) == ["new.csv", "run.csv"]
    assert (window.raw / "run.csv").read_bytes() == before
    assert Path(window.workspace.root / "new.csv").is_file()


def test_smoothing_from_the_card_smooths_the_trace_and_its_labels(panel):
    from moles.analysis.smoothing import moving_average
    (a,) = panel.add_files(["a.csv"])
    exp = panel.state.experiment(a)
    exp.add_label(LabelledPoint(1000, "manual"))
    panel.refresh_labels(a)
    card = panel._cards[a]
    dataset = panel.store.get("a.csv")

    card.smooth_spin.setValue(25)
    _flush(panel)

    smoothed = moving_average(dataset.current, 25)
    assert exp.smoothing_points == 25
    np.testing.assert_allclose(panel._drawn[a].curve.yData, smoothed)
    assert panel._drawn[a].labels[0].point[1] == pytest.approx(smoothed[1000])
    assert card.smooth_readout.text().startswith("≈ ") and card.smooth_readout.text().endswith(" mV")
    card.smooth_spin.setValue(1)
    assert card.smooth_spin.text() == "Off" and card.smooth_readout.text() == ""


def _card_order(panel):
    layout = panel._cards_layout
    by_card = {card: uid for uid, card in panel._cards.items()}
    widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
    return [by_card[w] for w in widgets if w in by_card]


def test_cards_move_up_and_down(panel):
    a, b = panel.add_files(["a.csv", "b.csv"])
    changes = []
    panel.changed.connect(lambda: changes.append(1))
    card_a, card_b = panel._cards[a], panel._cards[b]
    assert not card_a.up_btn.isEnabled() and card_a.down_btn.isEnabled()
    assert card_b.up_btn.isEnabled() and not card_b.down_btn.isEnabled()

    card_b.up_btn.click()

    assert [e.uid for e in panel.state.experiments] == [b, a] == _card_order(panel)
    assert _legend_names(panel) == ["b", "a"]
    assert card_b.down_btn.isEnabled() and not card_b.up_btn.isEnabled()
    assert not card_a.down_btn.isEnabled() and changes

    card_b.down_btn.click()
    assert _card_order(panel) == [a, b] and _legend_names(panel) == ["a", "b"]


def test_selecting_a_trace_highlights_its_file(window):
    explorer = window.explorer
    _write_cv(window.raw / "other.csv", scale=3.0)
    window.workspace.make_folder("", "Fig 2")
    first = explorer.add_paths([window.raw / "run.csv"])[0]
    second = explorer.add_paths([window.raw / "other.csv"], "Fig 2")[0]
    panel = window.plot_area.panels[0]
    a, b = panel.add_files([first, second])

    panel.select(a)
    assert explorer.selected_rels() == [first]
    panel.select(b)
    assert explorer.selected_rels() == ["Fig 2/other.csv"]
    assert explorer.item_for("Fig 2").isExpanded()

    panel.select(None)                                # a click on empty space
    assert explorer.selected_rels() == []

    panel.select(a)
    explorer.select(["Fig 2"])                        # the user picks something else
    panel.select(None)
    assert explorer.selected_rels() == ["Fig 2"]      # their choice is kept


def test_switching_plots_follows_their_selection(window):
    explorer = window.explorer
    rel = explorer.add_paths([window.raw / "run.csv"])[0]
    first = window.plot_area.panels[0]
    first.add_files([rel])                             # selects it
    second = window.plot_area.new_plot()              # nothing selected there

    assert explorer.selected_rels() == []
    window.plot_area.tabs.setCurrentWidget(first)
    assert explorer.selected_rels() == [rel]


# ----------------------------------------------------------------------
# Plot style, and the exported figure matching the screen
# ----------------------------------------------------------------------

def test_plot_style_changes_the_screen(panel):
    from moles.analysis.model import PlotStyle
    from moles.ui.data_analysis.interaction import PlotStyleDialog
    (a,) = panel.add_files(["a.csv"])
    panel.state.experiment(a).add_label(LabelledPoint(100, "manual"))
    panel.refresh_labels(a)
    px = panel._px_per_pt()

    dialog = PlotStyleDialog(PlotStyle(), panel)
    dialog.spins["label_font_pt"].setValue(14)
    dialog.spins["tick_font_pt"].setValue(8)
    dialog.spins["legend_line_pt"].setValue(6)
    panel.set_style(dialog.style())

    assert panel._drawn[a].labels[0].text.textItem.font().pointSizeF() == 14
    assert panel.plot_item.getAxis("bottom").style["tickFont"].pointSizeF() == 8
    sample, _label = panel.legend.items[0]
    assert sample._line_px == pytest.approx(6 * px)
    dialog._show(PlotStyle())
    assert dialog.style() == PlotStyle()


def test_the_exported_figure_matches_the_screen(panel, qapp):
    (a,) = panel.add_files(["a.csv"])
    panel.show()
    qapp.processEvents()
    panel._ticks.update()

    fig = panel._figure()
    ax = fig.axes[0]

    viewport = panel.plot_widget.viewport()
    dpi = viewport.logicalDpiX()
    assert fig.get_size_inches()[0] == pytest.approx(viewport.width() / dpi)
    assert ax.get_xlim() == pytest.approx(tuple(panel.view_box.viewRange()[0]))
    screen_x = [text for _v, text in panel.plot_item.getAxis("bottom")._tickLevels[0]]
    screen_y = [text for _v, text in panel.plot_item.getAxis("left")._tickLevels[0]]
    assert [t.get_text() for t in ax.get_xticklabels()] == screen_x
    assert [t.get_text() for t in ax.get_yticklabels()] == screen_y


def test_a_moved_legend_keeps_its_place(panel, qapp):
    from moles.analysis.model import PlotWindowState
    from moles.ui.data_analysis.plot_panel import PlotPanel
    panel.add_files(["a.csv"])
    panel.show()
    panel.legend.anchor(itemPos=(0, 0), parentPos=(0.1, 0.3))
    qapp.processEvents()

    panel.capture_legend()

    assert panel.state.legend_box == pytest.approx((0.1, 0.3), abs=0.01)
    again = PlotPanel(PlotWindowState.from_dict(panel.state.to_dict()), panel.store)
    again.resize(1200, 700)
    again.show()
    qapp.processEvents()
    again.capture_legend()
    assert again.state.legend_box == pytest.approx((0.1, 0.3), abs=0.01)
    again.deleteLater()

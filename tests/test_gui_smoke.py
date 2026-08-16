"""End-to-end offscreen smoke test of the electroanalysis interface.

Drives the CV, DPV, and OCP workers against the mock potentiostat (no hardware)
and checks that data streams, files are written, the DPV differential curve is
produced, and finished runs land in the Analysis tab.
"""

from pathlib import Path

import numpy as np
import pytest


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    # Redirect saved data into the test's temp folder, not the user's Dropbox.
    # Data folders now come from moles.settings via get_cv_data_folder(), so we
    # override that getter in the worker module rather than a module constant.
    import moles.ui.electroanalysis.control_tab as control_tab
    import moles.ui.electroanalysis.workers as workers
    monkeypatch.setattr(workers, "get_cv_data_folder", lambda: tmp_path)
    # The tab writes partial saves itself, through its own imported getter.
    monkeypatch.setattr(control_tab, "get_cv_data_folder", lambda: tmp_path)

    from moles.ui.electroanalysis.main_window import MainWindow
    win = MainWindow()
    win.cv_tab.test_mode_cb.setChecked(True)
    win.dpv_tab.test_mode_cb.setChecked(True)
    win.ocp_tab.test_mode_cb.setChecked(True)
    return win


def _run_worker(tab, pid):
    """Run one worker synchronously and return the captured finished payload."""
    from moles.ui.electroanalysis.workers import VoltammetryWorker
    params = tab._panels[pid].get_params()
    worker = VoltammetryWorker(pid, "MOCK_" + pid, params, use_mock=True)
    captured = {}
    worker.signals.finished.connect(
        lambda p, s, m, r, fp: captured.update(success=s, msg=m, result=r, fp=fp)
    )
    worker.run()
    return captured


def test_window_has_a_tab_per_method_plus_analysis(window):
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles == ["Cyclic Voltammetry", "Differential Pulse",
                      "Open Circuit Potential", "Analysis"]


def test_test_mode_creates_mock_panels(window):
    assert sorted(window.cv_tab._panels.keys()) == ['A', 'B', 'C']
    assert sorted(window.cv_tab._plots.keys()) == ['A', 'B', 'C']


def test_cv_run_streams_and_saves(window):
    out = _run_worker(window.cv_tab, 'A')
    assert out['success'] is True
    assert out['result']['method'] == 'CV'
    assert len(out['result']['raw_v']) > 0
    assert Path(out['fp']).exists()


def test_dpv_run_produces_differential(window):
    out = _run_worker(window.dpv_tab, 'A')
    assert out['success'] is True
    diff = out['result']['processed']
    assert diff.ndim == 2 and diff.shape[1] == 2 and diff.shape[0] > 0

    # Default DPV sweeps 0 -> 1 V; the mock's redox wave peaks near the midpoint.
    peak_v = diff[np.argmax(diff[:, 1]), 0]
    assert 0.3 < peak_v < 0.7

    # Both the differential and the raw staircase should be saved.
    diff_path = Path(out['fp'])
    raw_path = diff_path.with_name(diff_path.stem + "_RAW.csv")
    assert diff_path.exists()
    assert raw_path.exists()


def test_dpv_cyclic_run_produces_forward_and_reverse(window):
    # Switch the DPV panel to a CV-style cyclic run: Start -> Max -> Min -> ...
    cfg = window.dpv_tab._panels['A'].config
    cfg.type_combo.setCurrentText("Cyclic")
    cfg.cyc_start_v.setValue(0.0)
    cfg.min_v.setValue(-0.2)
    cfg.max_v.setValue(0.6)
    cfg.cyc_end_v.setValue(0.0)
    cfg.cycles.setValue(1)

    out = _run_worker(window.dpv_tab, 'A')
    assert out['success'] is True
    diff = out['result']['processed']
    assert diff.ndim == 2 and diff.shape[1] == 2 and diff.shape[0] > 0

    # The forward leg adds the pulse (delta-i > 0 through the wave); the reverse
    # leg subtracts it (delta-i < 0), so both branches must be present.
    di = diff[:, 1]
    assert di.max() > 0.5
    assert di.min() < -0.5


def _shown(widget):
    """True if a widget is set to show. The test window is never actually put
    on screen, so isVisible() is False for everything in it; isHidden() is what
    reflects the explicit setVisible calls the tab makes."""
    return not widget.isHidden()


def _short_ocp_panel(window, pid):
    """Configure one OCP panel for a sub-second run and return its params."""
    cfg = window.ocp_tab._panels[pid].config
    cfg.duration_unit.setCurrentText("seconds")
    cfg.duration.setValue(0.5)
    cfg.sample_hz.setValue(10.0)
    return cfg


def test_ocp_run_streams_and_saves(window):
    _short_ocp_panel(window, 'A')
    out = _run_worker(window.ocp_tab, 'A')

    assert out['success'] is True
    assert out['result']['method'] == 'OCP'

    times, potentials = out['result']['raw_v'], out['result']['raw_i']
    assert len(times) == 6           # t=0 plus 5 samples at 10 Hz over 0.5 s
    assert times[0] == 0.0
    assert np.all(np.diff(times) > 0)
    # The mock cell relaxes toward ~0.05 V from above, so the trace should sit
    # in a believable window rather than being empty or railed.
    assert np.all(np.abs(potentials) < 1.0)

    path = Path(out['fp'])
    assert path.exists()
    with open(path) as f:
        assert f.readline().strip() == "Time (s),Potential (V)"


def test_ocp_plot_axes_are_time_and_potential(window):
    tab = window.ocp_tab
    assert tab._xlabels['A'] == ('Time (s)', None)
    assert tab._ylabels['A'] == ('Potential', 'V')


def test_stopped_ocp_run_offers_its_partial_data(window, monkeypatch):
    import moles.ui.electroanalysis.control_tab as control_tab
    from moles.ui.electroanalysis.workers import VoltammetryWorker

    _short_ocp_panel(window, 'A')
    tab = window.ocp_tab
    params = tab._panels['A'].get_params()
    params['duration_s'] = 60.0  # long enough that the stop is what ends it

    worker = VoltammetryWorker('A', 'MOCK_A', params, use_mock=True)
    captured = {}
    worker.signals.finished.connect(
        lambda p, s, m, r, fp: captured.update(msg=m))
    worker.signals.data_chunk.connect(tab._on_data_chunk)
    # Stop as soon as a few points have streamed.
    worker.signals.data_chunk.connect(
        lambda p, x, y: worker.stop() if len(tab._live_data[p]['v_new']) >= 3
        else None)
    worker.run()

    assert captured['msg'] == "Stopped"

    # The tab should offer to save, and write what streamed when told to.
    monkeypatch.setattr(control_tab.QtWidgets.QMessageBox, "question",
                        lambda *a, **k: control_tab.QtWidgets.QMessageBox.Yes)
    saved = {}
    monkeypatch.setattr(control_tab.QtWidgets.QMessageBox, "information",
                        lambda *a, **k: saved.update(shown=True))
    tab._offer_partial_save('A')

    assert saved.get('shown') is True
    partials = list(Path(control_tab.get_cv_data_folder()).glob("*_PARTIAL.csv"))
    assert len(partials) == 1
    with open(partials[0]) as f:
        assert f.readline().strip() == "Time (s),Potential (V)"
    assert len(np.genfromtxt(partials[0], delimiter=",", skip_header=1)) >= 3


def test_finished_run_auto_adds_to_analysis(window):
    out = _run_worker(window.cv_tab, 'A')
    window.cv_tab.run_finished.emit(out['fp'])
    assert len(window.analysis_tab._rows) == 1


def test_finished_ocp_run_auto_adds_to_analysis(window):
    from moles.ui.electroanalysis.loader import KIND_OCP

    _short_ocp_panel(window, 'A')
    out = _run_worker(window.ocp_tab, 'A')
    window.ocp_tab.run_finished.emit(out['fp'])

    tab = window.analysis_tab
    assert len(tab._rows) == 1
    row = next(iter(tab._rows.values()))
    assert row.kind == KIND_OCP
    # It goes on the time-series plot, which appears now that it has a trace.
    assert tab._curve_plots[row.dataset_id] is tab.ocp_plot_widget
    assert _shown(tab.ocp_plot_widget)


def test_both_kinds_of_trace_keep_their_own_plot(window):
    _short_ocp_panel(window, 'A')
    cv_out = _run_worker(window.cv_tab, 'A')
    ocp_out = _run_worker(window.ocp_tab, 'A')

    tab = window.analysis_tab
    tab.add_dataset_from_file(cv_out['fp'])
    tab.add_dataset_from_file(ocp_out['fp'])

    plots = {tab._rows[i].kind: tab._curve_plots[i] for i in tab._rows}
    assert plots['voltammogram'] is tab.plot_widget
    assert plots['ocp'] is tab.ocp_plot_widget
    assert _shown(tab.plot_widget) and _shown(tab.ocp_plot_widget)

    # Dropping the OCP trace puts its plot away again; the CV plot stays.
    ocp_id = next(i for i in tab._rows if tab._rows[i].kind == 'ocp')
    tab._remove_dataset(ocp_id)
    assert not _shown(tab.ocp_plot_widget)
    assert _shown(tab.plot_widget)


def test_ocp_only_workspace_hides_the_voltammogram_plot(window):
    _short_ocp_panel(window, 'A')
    out = _run_worker(window.ocp_tab, 'A')

    tab = window.analysis_tab
    assert _shown(tab.plot_widget)   # empty state before anything loads
    tab.add_dataset_from_file(out['fp'])
    assert not _shown(tab.plot_widget)


def test_peak_label_add_and_remove(window):
    tab = window.analysis_tab
    tab.add_dataset("demo", np.linspace(-0.5, 0.5, 50), np.linspace(0, 5, 50))
    tab._add_peak_label(0.1, 2.0)
    assert len(tab._peak_labels) == 1
    assert tab._peak_labels[0]['plot'] is tab.plot_widget
    # Removing the same dataset should leave the plot consistent.
    dataset_id = next(iter(tab._rows))
    tab._remove_dataset(dataset_id)
    assert dataset_id not in tab._rows


def test_peak_labels_are_kept_per_plot(window):
    tab = window.analysis_tab
    tab._add_peak_label(0.1, 2.0)
    tab._add_peak_label(30.0, 0.25, tab.ocp_plot_widget)

    owners = [entry['plot'] for entry in tab._peak_labels]
    assert owners == [tab.plot_widget, tab.ocp_plot_widget]

"""End-to-end offscreen smoke test of the electroanalysis interface.

Drives the CV and DPV workers against the mock potentiostat (no hardware) and
checks that data streams, files are written, the DPV differential curve is
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
    import moles.ui.electroanalysis.workers as workers
    monkeypatch.setattr(workers, "get_cv_data_folder", lambda: tmp_path)

    from moles.ui.electroanalysis.main_window import MainWindow
    win = MainWindow()
    win.cv_tab.test_mode_cb.setChecked(True)
    win.dpv_tab.test_mode_cb.setChecked(True)
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


def test_window_has_three_tabs(window):
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles == ["Cyclic Voltammetry", "Differential Pulse", "Analysis"]


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


def test_finished_run_auto_adds_to_analysis(window):
    out = _run_worker(window.cv_tab, 'A')
    window.cv_tab.run_finished.emit(out['fp'])
    assert len(window.analysis_tab._rows) == 1


def test_peak_label_add_and_remove(window):
    tab = window.analysis_tab
    tab.add_dataset("demo", np.linspace(-0.5, 0.5, 50), np.linspace(0, 5, 50))
    tab._add_peak_label(0.1, 2.0)
    assert len(tab._peak_labels) == 1
    # Removing the same dataset should leave the plot consistent.
    dataset_id = next(iter(tab._rows))
    tab._remove_dataset(dataset_id)
    assert dataset_id not in tab._rows

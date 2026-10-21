"""Shared pytest fixtures for the MOLES test suite.

Qt-based tests run "offscreen" so they need no display. Importing ``moles.ui``
before any ``QApplication`` is created applies the macOS Qt plugin-path fix.
"""

import os

import pytest

# Force the headless Qt backend before PyQt is imported anywhere.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """Return a single QApplication for the whole test session.

    Skips the test cleanly if PyQt5 is unavailable so the non-GUI unit tests
    can still run in a minimal environment.
    """
    import moles.ui  # noqa: F401  (sets QT_QPA_PLATFORM_PLUGIN_PATH on import)
    try:
        from PyQt5 import QtWidgets
    except Exception as e:  # pragma: no cover - environment-dependent
        pytest.skip(f"PyQt5 unavailable: {e}")

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return app


@pytest.fixture(autouse=True)
def no_modal_dialogs(request, monkeypatch):
    """In the Data Analysis tests, fail on any dialog a test did not answer itself.

    Offscreen, a modal dialog waits for a click that never comes and the run
    hangs; failing instead names the dialog. Tests that expect one replace the
    method that would show it.
    """
    if not request.module.__name__.startswith(("test_data_analysis", "test_analysis")):
        return
    try:
        from PyQt5 import QtWidgets
    except Exception:  # pragma: no cover - environment-dependent
        return

    def unexpected(*args, **kwargs):
        raise AssertionError(f"Unexpected dialog: {args[1:3] if len(args) > 2 else args}")

    for name in ("warning", "information", "question", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, staticmethod(unexpected))
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", staticmethod(unexpected))
    monkeypatch.setattr(QtWidgets.QDialog, "exec_", unexpected)

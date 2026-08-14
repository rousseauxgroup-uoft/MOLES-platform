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

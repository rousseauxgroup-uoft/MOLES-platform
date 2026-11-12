"""Shared visual identity for MOLES apps — Moley the Mole everywhere.

Every app's ``main()`` calls ``apply_app_icon`` with its own package right
after creating its ``QApplication``, so its Dock (macOS) or taskbar (Windows)
icon is the same picture as its button in the launcher, rather than the stock
Python icon. The launcher itself shows the MOLES mascot.
"""

import logging
import sys
from importlib.resources import files

from PyQt5 import QtGui

logger = logging.getLogger(__name__)

LAUNCHER = "moles.ui.launcher"

# Each app's icon in moles.resources, by its package: shown on its launcher
# button and as its Dock / taskbar icon.
APP_ICONS = {
    LAUNCHER: "moles_logo.png",
    "moles.ui.electrolysis": "launcher_electrolysis.png",
    "moles.ui.electroanalysis": "launcher_electroanalysis.png",
    "moles.ui.data_analysis": "launcher_data_analysis.png",
    "moles.ui.onboarding": "launcher_onboarding.png",
    "moles.ui.settings": "launcher_settings.png",
}

# Windows groups taskbar buttons by AppUserModelID. Every MOLES app runs on the
# same pythonw.exe, so without one of its own each app would join a single
# button showing one icon; with one, each gets its own button and its own icon.
# The launcher's matches the Start menu and desktop shortcuts the installer
# makes (installer/windows/moles.iss), so pinning it pins the shortcut.
APP_USER_MODEL_IDS = {
    LAUNCHER: "MOLES.Platform.Launcher",
    "moles.ui.electrolysis": "MOLES.Platform.Electrolysis",
    "moles.ui.electroanalysis": "MOLES.Platform.Electroanalysis",
    "moles.ui.data_analysis": "MOLES.Platform.DataAnalysis",
    "moles.ui.onboarding": "MOLES.Platform.Onboarding",
    "moles.ui.settings": "MOLES.Platform.Settings",
}


def load_resource_pixmap(name: str) -> QtGui.QPixmap:
    """Load an image from ``moles.resources``.

    Returns a null pixmap if the image is missing (e.g. a trimmed install);
    callers should simply do without it in that case rather than fail.
    """
    try:
        data = (files("moles.resources") / name).read_bytes()
        pix = QtGui.QPixmap()
        pix.loadFromData(data)
        return pix
    except Exception:
        logger.warning("%s not found in package resources", name, exc_info=True)
        return QtGui.QPixmap()


def load_logo_pixmap() -> QtGui.QPixmap:
    """Load the MOLES mascot from package resources (null if missing)."""
    return load_resource_pixmap(APP_ICONS[LAUNCHER])


def load_app_icon_pixmap(app: str) -> QtGui.QPixmap:
    """The icon of the app in package ``app`` (the mascot for one not listed)."""
    pix = load_resource_pixmap(APP_ICONS.get(app, APP_ICONS[LAUNCHER]))
    return pix if not pix.isNull() else load_logo_pixmap()


def apply_app_icon(app, package: str = LAUNCHER) -> None:
    """Give this process the icon of the MOLES app in ``package``.

    This must be done at the application level, not per-window: on macOS the
    Dock icon is owned by the application (otherwise it shows the Python
    interpreter's rocket), and Qt only maps ``QApplication.setWindowIcon``
    — not per-window icons — onto the Dock. Windows and Linux taskbars
    inherit the same icon for every window automatically. Call it before
    showing any window, so Windows files the process under its own taskbar
    button from the start.
    """
    _set_windows_app_id(package)
    pix = load_app_icon_pixmap(package)
    if not pix.isNull():
        app.setWindowIcon(QtGui.QIcon(pix))


def _set_windows_app_id(package: str) -> None:
    if sys.platform != "win32":
        return
    app_id = APP_USER_MODEL_IDS.get(package, APP_USER_MODEL_IDS[LAUNCHER])
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        # Only the taskbar grouping suffers; the app runs as before.
        logger.warning("Could not set the Windows AppUserModelID", exc_info=True)

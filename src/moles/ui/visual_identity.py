"""Shared visual identity for MOLES apps — Moley the Mole everywhere.

Every app's ``main()`` calls ``apply_app_icon`` right after creating its
``QApplication`` so all MOLES processes show the mascot instead of the stock
Python icon.
"""

import logging
from importlib.resources import files

from PyQt5 import QtGui

logger = logging.getLogger(__name__)


def load_logo_pixmap() -> QtGui.QPixmap:
    """Load the MOLES mascot from package resources.

    Returns a null pixmap if the image is missing (e.g. a trimmed install);
    callers should simply skip the logo in that case rather than fail.
    """
    try:
        data = (files("moles.resources") / "MOLES_cropped.png").read_bytes()
        pix = QtGui.QPixmap()
        pix.loadFromData(data)
        return pix
    except Exception:
        logger.warning("MOLES logo not found in package resources", exc_info=True)
        return QtGui.QPixmap()


def apply_app_icon(app) -> None:
    """Set the MOLES mascot as the application icon.

    This must be done at the application level, not per-window: on macOS the
    Dock icon is owned by the application (otherwise it shows the Python
    interpreter's rocket), and Qt only maps ``QApplication.setWindowIcon``
    — not per-window icons — onto the Dock. Windows and Linux taskbars
    inherit the same icon for every window automatically.
    """
    pix = load_logo_pixmap()
    if not pix.isNull():
        app.setWindowIcon(QtGui.QIcon(pix))

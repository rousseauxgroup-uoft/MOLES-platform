"""Central logging configuration for the MOLES applications.

Why a log file instead of the console:
  - On Windows, a console window in QuickEdit/Mark/select mode blocks every
    write to it, process-wide. Any thread that prints — including the GUI
    thread emitting Qt warnings during painting — freezes until the selection
    is dismissed. Long electrolysis runs must not depend on console liveness.
  - PyQt5 (>= 5.5) treats an unhandled Python exception in a slot as fatal:
    it calls qFatal()/abort(), which also skips ``atexit`` and therefore the
    hardware ``emergency_cleanup``. Installing our own ``sys.excepthook``
    disables that behaviour: the exception is logged and the app keeps
    running with its safety nets intact.

Usage: application entry points call ``setup_logging("electrolysis")`` once,
then ``install_qt_message_handler()`` after PyQt5 is importable. Library
modules just use ``logging.getLogger(__name__)`` — when run from scripts that
never call ``setup_logging`` (e.g. examples/), messages of WARNING and above
still reach stderr through Python's last-resort handler.

Set ``MOLES_LOG_CONSOLE=1`` to additionally echo log records to the console
(useful during development; avoid for unattended runs, see above).
"""

import ctypes
import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path

LOG_DIR = Path.home() / ".moles" / "logs"

# Interval between GDI/USER handle-count log lines (Windows only). These
# counters exist to catch slow GUI resource leaks: a steady climb toward the
# default 10,000 per-process quota means the UI will eventually misrender and
# freeze, so a warning is logged once usage looks like a leak.
RESOURCE_MONITOR_INTERVAL_S = 60
RESOURCE_WARN_THRESHOLD = 8000

_configured = False


def setup_logging(app_name: str = "moles") -> logging.Logger:
    """Configure root logging to a rotating file. Idempotent.

    Returns the ``moles`` logger for convenience.
    """
    global _configured
    if _configured:
        return logging.getLogger("moles")
    _configured = True

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s"
    )
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / f"{app_name}.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if os.environ.get("MOLES_LOG_CONSOLE"):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    _install_exception_hooks()
    if sys.platform == "win32":
        _start_resource_monitor()

    log = logging.getLogger("moles")
    log.info("=== %s started (pid %d, python %s) — logging to %s ===",
             app_name, os.getpid(), sys.version.split()[0], LOG_DIR)
    return log


def install_qt_message_handler():
    """Route Qt's qDebug/qWarning/qCritical output into ``logging``.

    Must be called after PyQt5 is available (any time before or after
    QApplication creation). Keeps Qt's own diagnostics — e.g. paint-time
    warnings emitted by the GUI thread — off the console entirely.
    """
    from PyQt5 import QtCore

    qt_log = logging.getLogger("qt")
    level_map = {
        QtCore.QtDebugMsg: logging.DEBUG,
        QtCore.QtInfoMsg: logging.INFO,
        QtCore.QtWarningMsg: logging.WARNING,
        QtCore.QtCriticalMsg: logging.ERROR,
        QtCore.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, context, message):
        qt_log.log(level_map.get(mode, logging.WARNING), message)

    QtCore.qInstallMessageHandler(handler)


def _install_exception_hooks():
    """Log uncaught exceptions from any thread instead of dying silently.

    Replacing ``sys.excepthook`` also stops PyQt5 from calling qFatal() on
    slot exceptions (see module docstring), so one bad GUI update can no
    longer abort the whole multi-channel session and skip atexit cleanup.
    """
    log = logging.getLogger("moles.unhandled")

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        log.critical(
            "Unhandled exception in thread %r",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def get_gui_resource_counts():
    """Return (gdi_count, user_count) for this process. Windows only."""
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    gdi = ctypes.windll.user32.GetGuiResources(handle, 0)   # GR_GDIOBJECTS
    user = ctypes.windll.user32.GetGuiResources(handle, 1)  # GR_USEROBJECTS
    return gdi, user


def _start_resource_monitor():
    log = logging.getLogger("moles.resources")

    def monitor():
        warned = False
        while True:
            try:
                gdi, user = get_gui_resource_counts()
                log.info("GDI objects: %d, USER objects: %d", gdi, user)
                if not warned and max(gdi, user) > RESOURCE_WARN_THRESHOLD:
                    warned = True
                    log.warning(
                        "GUI resource usage above %d of the 10,000 per-process "
                        "quota — likely a handle leak; the UI will misrender "
                        "and freeze when the quota is exhausted.",
                        RESOURCE_WARN_THRESHOLD,
                    )
            except Exception:
                log.exception("Resource monitor failed; stopping it.")
                return
            time.sleep(RESOURCE_MONITOR_INTERVAL_S)

    threading.Thread(target=monitor, name="resource-monitor", daemon=True).start()

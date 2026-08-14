"""Main window of the MOLES launcher.

The launcher itself never opens a serial port — it only reads the board
registry (``~/.moles/potentiostats.csv``) and the claim files
(``~/.moles/claims/``), so it can stay open indefinitely without ever
interfering with running experiments. Apps are spawned as *detached*
processes: closing the launcher never kills a running experiment.
"""

import logging
import os
import subprocess
import sys

from PyQt5 import QtCore, QtGui, QtWidgets
from serial.tools import list_ports

# moles/ui/__init__.py runs automatically when this subpackage is imported,
# applying the macOS Qt plugin fix before QApplication is created.

from ... import claims
from ...logging_setup import install_qt_message_handler, setup_logging
from ...onboarding.registry import load_registry
from ..visual_identity import apply_app_icon, load_logo_pixmap

logger = logging.getLogger(__name__)

# How often the board table re-reads the claim files and the USB bus.
REFRESH_MS = 2000

# Launchable apps: button label -> python module to spawn.
APPS = (
    ("Electrolysis", "moles.ui.electrolysis"),
    ("Electroanalysis (CV / DPV)", "moles.ui.electroanalysis"),
    ("Onboarding", "moles.ui.onboarding"),
    ("Settings", "moles.ui.settings"),
)

COL_NAME, COL_PORT, COL_CAL, COL_STATUS = range(4)
TABLE_HEADERS = ("Board", "Port", "Calibrated", "Status")

# Display height of the mascot logo next to the launch buttons.
LOGO_HEIGHT_PX = 128


def _connected_usb_serials():
    """Map USB serial number -> current port path for boards present right now.

    Read purely from the OS's device metadata — no serial port is opened, so
    calling this can never disturb a running experiment on any board.
    """
    present = {}
    try:
        for p in list_ports.comports():
            sn = (getattr(p, "serial_number", None) or "").strip()
            if sn:
                present[sn] = p.device
    except Exception:
        logger.exception("Could not enumerate USB serial ports")
    return present


def _detached_python():
    """Pick the interpreter to spawn detached GUI apps with.

    On Windows, ``sys.executable`` is ``python.exe``, a console-subsystem
    binary. Even with DETACHED_PROCESS, Windows 11's "default terminal
    application" feature (Windows Terminal) still allocates a terminal host
    for it, which then lingers for the child's entire lifetime.
    ``pythonw.exe`` — the GUI-subsystem twin sitting next to it in the same
    Scripts/ directory — never triggers that console allocation at all.
    """
    if os.name == "nt":
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(pythonw):
            return pythonw
    return sys.executable


def launch_detached(module: str):
    """Start a MOLES app as a fully independent process.

    The child survives the launcher closing, gets no console handles from us,
    and runs on the same Python interpreter (and therefore the same
    environment) as the launcher.
    """
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    logger.info("Launching %s", module)
    return subprocess.Popen([_detached_python(), "-m", module], **kwargs)


class LauncherWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MOLES Launcher")
        self.resize(760, 480)

        # row index -> claim key, rebuilt on every refresh, so the
        # force-release button knows what the selected row refers to.
        self._row_keys = []

        self._build_ui()
        self.refresh()

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)

    def _build_ui(self):
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(14, 14, 14, 10)
        layout.setSpacing(10)

        # --- Top row: mascot logo + app launch buttons ---
        top_row = QtWidgets.QHBoxLayout()

        logo = load_logo_pixmap()
        if not logo.isNull():
            logo_label = QtWidgets.QLabel()
            logo_label.setPixmap(
                logo.scaledToHeight(LOGO_HEIGHT_PX, QtCore.Qt.SmoothTransformation)
            )
            logo_label.setToolTip("Moley the Mole — MOLES mascot")
            top_row.addWidget(logo_label, alignment=QtCore.Qt.AlignVCenter)

        apps_group = QtWidgets.QGroupBox("Launch")
        apps_layout = QtWidgets.QHBoxLayout(apps_group)
        for label, module in APPS:
            btn = QtWidgets.QPushButton(label)
            btn.setMinimumHeight(40)
            btn.clicked.connect(lambda _checked, m=module, l=label: self._launch(m, l))
            apps_layout.addWidget(btn)
        top_row.addWidget(apps_group, stretch=1)
        layout.addLayout(top_row)

        # --- Board status table ---
        boards_group = QtWidgets.QGroupBox("Boards")
        boards_layout = QtWidgets.QVBoxLayout(boards_group)

        self.table = QtWidgets.QTableWidget(0, len(TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._update_force_btn)
        boards_layout.addWidget(self.table)

        # --- Force release (guarded) ---
        force_row = QtWidgets.QHBoxLayout()
        self.force_btn = QtWidgets.QPushButton("Force Release Selected")
        self.force_btn.setEnabled(False)
        self.force_btn.setToolTip(
            "Remove the selected board's claim even though another app holds it.\n"
            "Only for stuck claims — never while that app is actually running."
        )
        self.force_btn.clicked.connect(self._force_release_selected)
        force_row.addStretch(1)
        force_row.addWidget(self.force_btn)
        boards_layout.addLayout(force_row)

        layout.addWidget(boards_group, stretch=1)

        self.status_label = QtWidgets.QLabel("")
        layout.addWidget(self.status_label)

        self.setCentralWidget(central)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh(self):
        """Rebuild the board table from the registry + current claims."""
        try:
            entries = load_registry()
        except Exception:
            logger.exception("Could not read the potentiostat registry")
            entries = []
        all_claims = claims.list_claims()
        present = _connected_usb_serials()

        selected_key = self._selected_key()

        rows = []
        seen_keys = set()
        for entry in entries:
            key = claims.claim_key_for(entry.name, entry.port)
            seen_keys.add(key)
            # Prefer the port the OS reports right now — the registry's stored
            # path can go stale when the OS re-enumerates USB devices.
            live_port = present.get(entry.usb_serial)
            rows.append((entry.name, live_port or entry.port or "—",
                         "yes" if entry.is_calibrated else "no",
                         key, all_claims.get(key), live_port is not None))
        # Claims that match no registry row (e.g. legacy port-keyed claims, or
        # a board that was renamed) still need to be visible and releasable.
        for key, claim in sorted(all_claims.items()):
            if key not in seen_keys:
                rows.append(("?", claim.port or "—", "—", key, claim, True))

        self.table.setRowCount(len(rows))
        self._row_keys = []
        for r, (name, port, calibrated, key, claim, connected) in enumerate(rows):
            self._row_keys.append(key)
            # A claim outranks presence: a claimed-but-unplugged board is a
            # problem the user should see described, not hidden behind
            # "Not connected".
            if claim is not None and claims.is_stale(claim):
                status = f"STALE claim — {claim.describe()} (app no longer running)"
                color = QtCore.Qt.darkYellow
            elif claim is not None:
                status, color = f"In use — {claim.describe()}", QtCore.Qt.darkRed
            elif not connected:
                status, color = "Not connected", QtCore.Qt.gray
            else:
                status, color = "Free", QtCore.Qt.darkGreen

            for c, text in ((COL_NAME, name), (COL_PORT, port),
                            (COL_CAL, calibrated), (COL_STATUS, status)):
                item = QtWidgets.QTableWidgetItem(text)
                if c == COL_STATUS:
                    item.setForeground(QtGui.QBrush(color))
                    item.setToolTip(status)
                elif not connected and claim is None:
                    # Grey the whole row for absent boards so free ones pop.
                    item.setForeground(QtGui.QBrush(QtCore.Qt.gray))
                self.table.setItem(r, c, item)

        if not rows:
            self.status_label.setText(
                "No boards registered yet — use Onboarding to detect and name them."
            )
        else:
            in_use = sum(1 for row in rows if row[4] is not None)
            absent = sum(1 for row in rows if row[4] is None and not row[5])
            free = len(rows) - in_use - absent
            self.status_label.setText(
                f"{len(rows)} board(s): {free} free, {in_use} in use, {absent} not connected."
            )

        # Restore selection across the rebuild so the force button doesn't
        # flicker off every REFRESH_MS.
        if selected_key in self._row_keys:
            self.table.selectRow(self._row_keys.index(selected_key))
        self._update_force_btn()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _launch(self, module, label):
        try:
            launch_detached(module)
            self.status_label.setText(f"Started {label}.")
        except Exception as e:
            logger.exception("Failed to launch %s", module)
            QtWidgets.QMessageBox.critical(
                self, "Launch failed", f"Could not start {label}:\n{e}"
            )

    def _selected_key(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows or rows[0].row() >= len(self._row_keys):
            return None
        return self._row_keys[rows[0].row()]

    def _update_force_btn(self):
        key = self._selected_key()
        self.force_btn.setEnabled(key is not None and claims.read_claim(key) is not None)

    def _force_release_selected(self):
        key = self._selected_key()
        if key is None:
            return
        claim = claims.read_claim(key)
        if claim is None:
            self._update_force_btn()
            return

        if claims.is_stale(claim):
            question = (
                f"This claim was left behind by a crashed or killed app:\n\n"
                f"    {claim.describe()}\n\n"
                "Release the board?"
            )
        else:
            question = (
                f"WARNING: the owning app appears to be RUNNING right now:\n\n"
                f"    {claim.describe()} (PID {claim.pid})\n\n"
                "Releasing lets another app try to take this board. If an "
                "experiment is actually in progress it keeps exclusive use of "
                "the serial port either way, but statuses will be misleading.\n\n"
                "Only continue if you are certain the claim is stuck. Continue?"
            )
        answer = QtWidgets.QMessageBox.warning(
            self, "Force release board", question,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer == QtWidgets.QMessageBox.Yes:
            claims.force_release(key)
            self.refresh()


def main():
    setup_logging("launcher")
    install_qt_message_handler()

    app = QtWidgets.QApplication(sys.argv)
    apply_app_icon(app)
    win = LauncherWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

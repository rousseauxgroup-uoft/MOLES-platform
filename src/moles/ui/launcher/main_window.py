"""Main window of the MOLES launcher.

The launcher's table comes from the board registry
(``~/.moles/potentiostats.csv``), the claim files (``~/.moles/claims/``) and
the firmware notes apps leave when they connect (``~/.moles/firmware/``), so
it can stay open indefinitely without ever interfering with running
experiments. It opens a serial port only when asked to check firmware, and
then only on boards no app holds, each under a claim of its own for the
second or so it takes. Apps are spawned as *detached* processes: closing the
launcher never kills a running experiment.
"""

import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from PyQt5 import QtCore, QtGui, QtWidgets
from serial.tools import list_ports

# moles/ui/__init__.py runs automatically when this subpackage is imported,
# applying the macOS Qt plugin fix before QApplication is created.

from ... import __version__, claims
from ...logging_setup import install_qt_message_handler, setup_logging
from ...onboarding import firmware
from ...onboarding.discovery import probe_firmware
from ...onboarding.registry import load_registry
from ..visual_identity import apply_app_icon, load_logo_pixmap
from .update_offer import UpdateCheckThread, UpdateOffer

logger = logging.getLogger(__name__)

# How often the board table re-reads the claim files and the USB bus.
REFRESH_MS = 2000

# Launchable apps: button label -> python module to spawn.
APPS = (
    ("Electrolysis", "moles.ui.electrolysis"),
    ("Electroanalysis (CV / DPV)", "moles.ui.electroanalysis"),
    ("Data Analysis", "moles.ui.data_analysis"),
    ("Onboarding", "moles.ui.onboarding"),
    ("Settings", "moles.ui.settings"),
)

COL_NAME, COL_PORT, COL_CAL, COL_FW, COL_STATUS = range(5)
TABLE_HEADERS = ("Board", "Port", "Calibrated", "Firmware", "Status")

# Display height of the mascot logo next to the launch buttons.
LOGO_HEIGHT_PX = 128

# Delay before asking GitHub about updates, so the window appears first.
UPDATE_CHECK_DELAY_MS = 1500


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


def check_firmware(boards):
    """Read the firmware release of each idle board and note it.

    ``boards`` holds (claim key, USB serial, port) triples. Each board is
    claimed for the duration of its read, so an app starting on it at that
    moment is told the launcher has it rather than colliding on the port; a
    board that is claimed already is skipped. Returns {USB serial: version,
    or None when it could not be read}.
    """
    def read_one(board):
        key, usb_serial, port = board
        try:
            claims.acquire(key, app="launcher",
                           experiment="reading firmware version", port=port)
        except claims.ClaimBusyError:
            return usb_serial, None
        try:
            version = probe_firmware(port)
        finally:
            claims.release(key)
        if version is not None:
            firmware.record(usb_serial, version)
        return usb_serial, version

    if not boards:
        return {}
    with ThreadPoolExecutor(max_workers=len(boards)) as pool:
        return dict(pool.map(read_one, boards))


class FirmwareCheckThread(QtCore.QThread):
    """Runs check_firmware away from the window so it stays responsive."""

    checked = QtCore.pyqtSignal(dict)

    def __init__(self, boards, parent=None):
        super().__init__(parent)
        self._boards = boards

    def run(self):
        try:
            results = check_firmware(self._boards)
        except Exception:
            logger.exception("Firmware check failed")
            results = {serial: None for _key, serial, _port in self._boards}
        self.checked.emit(results)


class LauncherWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"MOLES Launcher {__version__}")
        self.resize(860, 480)

        # row index -> claim key, rebuilt on every refresh, so the
        # force-release button knows what the selected row refers to.
        self._row_keys = []
        # (claim key, USB serial, port, name) of boards present and unclaimed
        # at the last refresh: the ones a firmware check can read.
        self._idle_boards = []
        self._fw_thread = None
        self._fw_names = {}

        self._build_ui()
        self.refresh()

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)

        self._update_check = None
        self._update_offer = UpdateOffer(self)
        self._update_offer.installer_opened.connect(self.close)
        QtCore.QTimer.singleShot(UPDATE_CHECK_DELAY_MS, self._check_for_update)

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
        self.fw_btn = QtWidgets.QPushButton("Check Firmware")
        self.fw_btn.setToolTip(
            "Read the firmware version of every connected board that no app is using.\n"
            "Boards in use keep the version noted when their app connected."
        )
        self.fw_btn.clicked.connect(self._check_firmware)
        force_row.addWidget(self.fw_btn)
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
                         key, all_claims.get(key), live_port is not None,
                         entry.usb_serial))
        # Claims that match no registry row (e.g. legacy port-keyed claims, or
        # a board that was renamed) still need to be visible and releasable.
        for key, claim in sorted(all_claims.items()):
            if key not in seen_keys:
                rows.append(("?", claim.port or "—", "—", key, claim, True, ""))

        self.table.setRowCount(len(rows))
        self._row_keys = []
        self._idle_boards = []
        for r, (name, port, calibrated, key, claim, connected, usb_serial) in enumerate(rows):
            self._row_keys.append(key)
            if connected and claim is None and usb_serial:
                self._idle_boards.append((key, usb_serial, port, name))
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

            fw_text, fw_tip, fw_outdated = _firmware_cell(usb_serial)
            for c, text in ((COL_NAME, name), (COL_PORT, port),
                            (COL_CAL, calibrated), (COL_FW, fw_text),
                            (COL_STATUS, status)):
                item = QtWidgets.QTableWidgetItem(text)
                if c == COL_STATUS:
                    item.setForeground(QtGui.QBrush(color))
                    item.setToolTip(status)
                elif not connected and claim is None:
                    # Grey the whole row for absent boards so free ones pop.
                    item.setForeground(QtGui.QBrush(QtCore.Qt.gray))
                elif c == COL_FW and fw_outdated:
                    item.setForeground(QtGui.QBrush(QtCore.Qt.darkYellow))
                if c == COL_FW:
                    item.setToolTip(fw_tip)
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
        self.fw_btn.setEnabled(self._fw_thread is None and bool(self._idle_boards))

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

    def _check_firmware(self):
        boards = list(self._idle_boards)
        if not boards or self._fw_thread is not None:
            return
        self._fw_names = {serial: name for _key, serial, _port, name in boards}
        self._fw_thread = FirmwareCheckThread(
            [(key, serial, port) for key, serial, port, _name in boards], self)
        self._fw_thread.checked.connect(self._on_firmware_checked)
        self.fw_btn.setEnabled(False)
        self.fw_btn.setText("Checking…")
        self._fw_thread.start()

    def _on_firmware_checked(self, results):
        self._fw_thread.wait()
        self._fw_thread = None
        self.fw_btn.setText("Check Firmware")
        read = [f"{self._fw_names.get(s, s)} {v}" for s, v in results.items() if v]
        missed = [self._fw_names.get(s, s) for s, v in results.items() if not v]
        self.refresh()
        message = "Firmware: " + (", ".join(read) if read else "none read")
        if missed:
            message += f". Could not read {', '.join(missed)} (busy or not responding)."
        self.status_label.setText(message)

    def _check_for_update(self):
        self._update_check = UpdateCheckThread(self)
        self._update_check.found.connect(self._update_offer.offer)
        self._update_check.start()

    def closeEvent(self, event):
        # A check in progress holds claims and open ports; let it finish (it
        # takes about a second) rather than tear the thread down mid-read.
        if self._fw_thread is not None:
            self._fw_thread.wait(10_000)
        self._update_offer.shutdown()
        if self._update_check is not None:
            self._update_check.wait(10_000)
        super().closeEvent(event)

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


def _firmware_cell(usb_serial):
    """Text, tooltip, and whether it is out of date, for a board's firmware."""
    note = firmware.read(usb_serial)
    if note is None:
        return ("—",
                "Not read yet. MOLES notes a board's firmware whenever an app "
                "connects to it;\nCheck Firmware reads it now from boards no app "
                "is using.", False)
    version = note["version"]
    when = note["read_at"].replace("T", " ")[:16]
    tip = f"Firmware {version}, read {when}."
    if firmware.is_outdated(version):
        tip += (f"\nVersion {firmware.LATEST_VERSION} is in firmware/prebuilt/; "
                "its README lists what each release adds.")
        return f"{version} (update to {firmware.LATEST_VERSION})", tip, True
    return version, tip, False


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

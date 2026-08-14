"""Top-level window for ``moles-onboard``.

Two panels side by side: an editable table of registered potentiostats on
the left, and a calibration plot on the right that updates as the user
walks through a six-point current sweep against an external multimeter.
The Rescan button on the toolbar runs USB discovery in a background thread
and merges the results into the saved registry. Each row carries a
Calibrate button that drives one potentiostat through the sweep, asking
for a multimeter reading at every step.
"""

import sys
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from ..visual_identity import apply_app_icon
from ...calibration import load_calibrations  # legacy bundled loader for first-run seed
from ...driver.mock import MockPotentiostat
from ...driver.ps4_ref import Potentiostat
from ...onboarding import calibration_sweep
from ...onboarding.discovery import DetectedDevice
from ...onboarding.registry import (
    USER_REGISTRY_PATH,
    Entry,
    load_registry,
    next_name,
    reconcile,
    save_registry,
)
from .workers import CalibrationWorker, ScanWorker


# Column indices for the registry table.
COL_NAME = 0
COL_USB_SERIAL = 1
COL_PORT = 2
COL_SLOPE = 3
COL_INTERCEPT = 4
COL_STATUS = 5
COL_CALIBRATE = 6
COL_CALIBRATE_V = 7
TABLE_HEADERS = ("Name", "USB Serial", "Port", "Slope", "Intercept", "Status", "", "")

# Axis labels and default plot extents for the two sweep modes.
_MODE_LABELS = {
    "current": ("Applied current (mA)", "Measured current (mA)"),
    "voltage": ("Applied potential (V)", "Measured potential (V)"),
}

# Foreground colour applied to rows for unplugged boards. Their calibration
# data is still valid and editable; we just want to make it visually obvious
# that the device isn't currently connected.
_DISCONNECTED_FG = QtGui.QColor("#888888")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MOLES: Onboarding & Calibration")
        self.resize(1400, 700)

        self.entries: List[Entry] = []
        # Sweep state for the currently-running calibration. None when idle.
        self._calib_worker: Optional[CalibrationWorker] = None
        self._calib_row: Optional[int] = None
        self._calib_mode: str = "current"
        self._calib_points: List[Tuple[float, float]] = []
        # Snapshot of the previous calibration line for the row being
        # recalibrated, restored on cancel/failure so the plot reverts.
        self._calib_previous: Optional[Tuple[float, float]] = None
        self._scan_worker: Optional[ScanWorker] = None

        self._build_ui()
        self._load_initial_entries()
        self._refresh_table()

    # ---- UI construction ----

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # Top toolbar: scan button, mock-mode checkbox, status line.
        toolbar = QtWidgets.QHBoxLayout()
        self.scan_button = QtWidgets.QPushButton("Rescan for Potentiostats")
        self.scan_button.clicked.connect(self._start_scan)
        toolbar.addWidget(self.scan_button)

        self.mock_checkbox = QtWidgets.QCheckBox("Test mode (use mock potentiostats)")
        self.mock_checkbox.setToolTip(
            "When on, calibration uses a simulated potentiostat instead of real hardware. "
            "Useful for trying out the UI without devices connected."
        )
        toolbar.addWidget(self.mock_checkbox)

        toolbar.addStretch(1)
        self.status_label = QtWidgets.QLabel("Ready.")
        self.status_label.setStyleSheet("color: #555;")
        toolbar.addWidget(self.status_label)
        outer.addLayout(toolbar)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        outer.addWidget(splitter, stretch=1)

        # Left: registry table.
        self.table = QtWidgets.QTableWidget(0, len(TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.itemChanged.connect(self._on_item_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_USB_SERIAL, QtWidgets.QHeaderView.Interactive)
        header.setSectionResizeMode(COL_PORT, QtWidgets.QHeaderView.Stretch)
        splitter.addWidget(self.table)

        # Right: calibration plot + equation label.
        right_panel = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("w")
        self.plot_widget.setLabel("bottom", "Applied current (mA)")
        self.plot_widget.setLabel("left", "Measured current (mA)")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self._scatter = self.plot_widget.plot(
            [], [], pen=None, symbol="o", symbolBrush="b", symbolSize=8, name="points"
        )
        self._fit_line = self.plot_widget.plot([], [], pen=pg.mkPen("r", width=2), name="fit")
        right_layout.addWidget(self.plot_widget, stretch=1)

        self.equation_label = QtWidgets.QLabel("")
        self.equation_label.setAlignment(QtCore.Qt.AlignCenter)
        eq_font = self.equation_label.font()
        eq_font.setPointSize(eq_font.pointSize() + 1)
        self.equation_label.setFont(eq_font)
        right_layout.addWidget(self.equation_label)
        splitter.addWidget(right_panel)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

    # ---- Initial load and migration seed ----

    def _load_initial_entries(self) -> None:
        """Populate ``self.entries`` from the user CSV, or seed from bundled.

        On a brand-new machine that's never run onboarding, we copy the
        legacy bundled calibration values into in-memory entries with empty
        USB serial fields. The user can then run a scan; new detections
        whose serial doesn't match anything will trigger a one-time "map
        this device to which existing letter?" prompt before being saved.
        """
        if USER_REGISTRY_PATH.is_file():
            self.entries = load_registry(USER_REGISTRY_PATH)
            return

        bundled = load_calibrations()
        for name in sorted(bundled.keys()):
            cal = bundled[name]
            self.entries.append(
                Entry(
                    name=name,
                    usb_serial="",
                    port="",
                    slope=cal.get("slope"),
                    intercept=cal.get("intercept"),
                    comments="seeded from legacy calibration",
                )
            )

    # ---- Table rendering ----

    def _refresh_table(self) -> None:
        """Redraw the table from ``self.entries`` without firing edit signals."""
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(self.entries))
            for row, entry in enumerate(self.entries):
                self._set_row(row, entry)
        finally:
            self.table.blockSignals(False)
        self._update_calibrate_button_states()
        self._show_plot_for_selected()

    def _set_row(self, row: int, entry: Entry) -> None:
        name_item = QtWidgets.QTableWidgetItem(entry.name)
        name_item.setFlags(name_item.flags() | QtCore.Qt.ItemIsEditable)
        self.table.setItem(row, COL_NAME, name_item)

        serial_item = QtWidgets.QTableWidgetItem(entry.usb_serial)
        serial_item.setFlags(serial_item.flags() & ~QtCore.Qt.ItemIsEditable)
        self.table.setItem(row, COL_USB_SERIAL, serial_item)

        port_item = QtWidgets.QTableWidgetItem(entry.port)
        port_item.setFlags(port_item.flags() & ~QtCore.Qt.ItemIsEditable)
        self.table.setItem(row, COL_PORT, port_item)

        slope_item = QtWidgets.QTableWidgetItem(_fmt_optional(entry.slope))
        slope_item.setFlags(slope_item.flags() & ~QtCore.Qt.ItemIsEditable)
        self.table.setItem(row, COL_SLOPE, slope_item)

        intercept_item = QtWidgets.QTableWidgetItem(_fmt_optional(entry.intercept))
        intercept_item.setFlags(intercept_item.flags() & ~QtCore.Qt.ItemIsEditable)
        self.table.setItem(row, COL_INTERCEPT, intercept_item)

        status_text = self._row_status_text(row, entry)
        status_item = QtWidgets.QTableWidgetItem(status_text)
        status_item.setFlags(status_item.flags() & ~QtCore.Qt.ItemIsEditable)
        self.table.setItem(row, COL_STATUS, status_item)

        # Grey out the disconnected rows so the user can see which boards
        # aren't currently plugged in. Calibration values stay editable in
        # principle but the Calibrate button is disabled.
        if not entry.connected:
            for col in range(self.table.columnCount() - 1):
                item = self.table.item(row, col)
                if item is not None:
                    item.setForeground(_DISCONNECTED_FG)

        button = QtWidgets.QPushButton("Calibrate I")
        button.setToolTip("Current sweep: multimeter in series, set to DC current.")
        button.clicked.connect(lambda _checked, r=row: self._on_calibrate_clicked(r, "current"))
        self.table.setCellWidget(row, COL_CALIBRATE, button)

        v_button = QtWidgets.QPushButton("Calibrate V")
        v_button.setToolTip("Voltage sweep: multimeter across WE and RE, set to DC voltage.")
        v_button.clicked.connect(lambda _checked, r=row: self._on_calibrate_clicked(r, "voltage"))
        self.table.setCellWidget(row, COL_CALIBRATE_V, v_button)

    def _row_status_text(self, row: int, entry: Entry) -> str:
        if self._calib_row == row:
            return "Calibrating…"
        if not entry.usb_serial:
            return "Not yet detected"
        if not entry.connected:
            return "Not connected"
        if entry.is_calibrated and entry.is_v_calibrated:
            return "Connected — I+V calibrated"
        if entry.is_calibrated:
            return "Connected — I calibrated"
        if entry.is_v_calibrated:
            return "Connected — V calibrated"
        return "Connected — uncalibrated"

    def _update_calibrate_button_states(self) -> None:
        sweep_running = self._calib_worker is not None
        for row, entry in enumerate(self.entries):
            connected = entry.connected or self.mock_checkbox.isChecked()
            for col in (COL_CALIBRATE, COL_CALIBRATE_V):
                button = self.table.cellWidget(row, col)
                if isinstance(button, QtWidgets.QPushButton):
                    button.setEnabled(connected and not sweep_running)

    # ---- Editable cells: name, comments ----

    def _on_item_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        row = item.row()
        col = item.column()
        if row < 0 or row >= len(self.entries):
            return
        if col != COL_NAME:
            return

        new_name = item.text().strip()
        if not new_name:
            QtWidgets.QMessageBox.warning(self, "Invalid name", "Name cannot be empty.")
            self.table.blockSignals(True)
            item.setText(self.entries[row].name)
            self.table.blockSignals(False)
            return

        # Reject duplicates against other rows.
        for i, e in enumerate(self.entries):
            if i != row and e.name == new_name:
                QtWidgets.QMessageBox.warning(self, "Duplicate name",
                                              f"Name '{new_name}' is already in use.")
                self.table.blockSignals(True)
                item.setText(self.entries[row].name)
                self.table.blockSignals(False)
                return

        self.entries[row].name = new_name
        self._save()

    # ---- Scan ----

    def _start_scan(self) -> None:
        if self._calib_worker is not None:
            self._set_status("Cannot scan during a calibration sweep.")
            return
        self.scan_button.setEnabled(False)
        self._set_status("Scanning USB ports…")
        self._scan_worker = ScanWorker(self)
        self._scan_worker.finished_with_result.connect(self._on_scan_done)
        self._scan_worker.start()

    def _on_scan_done(self, detected: list, error_message: str) -> None:
        self.scan_button.setEnabled(True)
        if error_message:
            self._set_status(f"Scan failed: {error_message}")
            QtWidgets.QMessageBox.warning(self, "Scan failed", error_message)
            return

        self._merge_detected(detected)

    def _merge_detected(self, detected: List[DetectedDevice]) -> None:
        """Reconcile a fresh scan against the existing entries.

        Two paths: the easy path is when every detected serial either
        matches an entry already (we just refresh the port) or is brand new
        (we assign the next free name). The migration path runs when there
        are seeded rows with empty USB serials (legacy calibrations from the
        bundled CSV) — for each detected device that doesn't match anything,
        we ask the user whether it should adopt one of those seeded letters
        (preserving the legacy slope/intercept) or take a fresh name.
        """
        # Devices that didn't match any existing entry — handled separately
        # so the user can map them to seeded rows if any exist.
        unmatched: List[DetectedDevice] = []
        by_serial = {e.usb_serial: e for e in self.entries if e.usb_serial}

        # First pass: refresh ports for known devices, collect unknowns.
        detected_serials = set()
        for dev in detected:
            detected_serials.add(dev.usb_serial)
            if dev.usb_serial in by_serial:
                by_serial[dev.usb_serial].port = dev.port
                by_serial[dev.usb_serial].connected = True
            else:
                unmatched.append(dev)

        for entry in self.entries:
            if entry.usb_serial and entry.usb_serial not in detected_serials:
                entry.connected = False

        # Second pass: assign each unmatched device to either a seeded row
        # or a brand-new entry.
        if unmatched:
            self._handle_unmatched_devices(unmatched)

        self.entries.sort(key=lambda e: _sort_key(e.name))
        self._save()
        self._refresh_table()
        self._set_status(
            f"Scan complete: {sum(1 for e in self.entries if e.connected)} connected."
        )

    def _handle_unmatched_devices(self, unmatched: List[DetectedDevice]) -> None:
        for dev in unmatched:
            seeded = [
                e for e in self.entries
                if not e.usb_serial and e.is_calibrated
            ]
            chosen_entry: Optional[Entry] = None
            if seeded:
                # Migration prompt: let the user attach this physical board to
                # one of the seeded letters that has legacy calibration data.
                options = [e.name for e in seeded] + ["(assign new name)"]
                choice, ok = QtWidgets.QInputDialog.getItem(
                    self,
                    "New potentiostat detected",
                    f"Detected device on {dev.port}\n"
                    f"USB serial: {dev.usb_serial}\n\n"
                    "Map this to which existing letter?",
                    options,
                    0,
                    False,
                )
                if not ok:
                    # User cancelled the dialog: skip this device for now.
                    continue
                if choice != "(assign new name)":
                    chosen_entry = next((e for e in seeded if e.name == choice), None)

            if chosen_entry is not None:
                chosen_entry.usb_serial = dev.usb_serial
                chosen_entry.port = dev.port
                chosen_entry.connected = True
                # The seeded comment is no longer accurate once a real serial
                # is attached, so clear it unless the user wrote something.
                if chosen_entry.comments == "seeded from legacy calibration":
                    chosen_entry.comments = ""
            else:
                used = {e.name for e in self.entries}
                self.entries.append(
                    Entry(
                        name=next_name(used),
                        usb_serial=dev.usb_serial,
                        port=dev.port,
                        connected=True,
                    )
                )

    # ---- Plot ----

    def _on_selection_changed(self) -> None:
        if self._calib_worker is not None:
            # Don't redraw while a sweep is in progress — the plot is
            # showing the live sweep, not the saved fit.
            return
        self._show_plot_for_selected()

    def _show_plot_for_selected(self) -> None:
        if self._calib_worker is not None:
            return
        row = self.table.currentRow()
        if row < 0 or row >= len(self.entries):
            self._render_plot([], None)
            return
        entry = self.entries[row]
        if entry.is_calibrated:
            self._render_plot([], (entry.slope, entry.intercept))
        else:
            self._render_plot([], None)

    def _render_plot(
        self,
        points: List[Tuple[float, float]],
        fit: Optional[Tuple[float, float]],
        r_squared: Optional[float] = None,
    ) -> None:
        if points:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            self._scatter.setData(xs, ys)
        else:
            self._scatter.setData([], [])

        if fit is not None:
            slope, intercept = fit
            # Default plot extent follows the active sweep's point range.
            if self._calib_mode == "voltage":
                sweep = calibration_sweep.CALIBRATION_POTENTIALS_V
            else:
                sweep = calibration_sweep.CALIBRATION_CURRENTS_MA
            xmin = min(0.0, min(sweep))
            xmax = max(sweep)
            if points:
                xmin = min(xmin, min(p[0] for p in points))
                xmax = max(xmax, max(p[0] for p in points))
            xs = np.array([xmin * 1.05, xmax * 1.05])
            ys = slope * xs + intercept
            self._fit_line.setData(xs, ys)
            r2_text = f"   (R² = {r_squared:.4f})" if r_squared is not None else ""
            self.equation_label.setText(
                f"y = {slope:.4f} · x + {intercept:.4f}{r2_text}"
            )
        else:
            self._fit_line.setData([], [])
            self.equation_label.setText("")

    # ---- Calibration sweep ----

    def _on_calibrate_clicked(self, row: int, mode: str = "current") -> None:
        if self._calib_worker is not None:
            return
        if row < 0 or row >= len(self.entries):
            return
        entry = self.entries[row]

        is_done = entry.is_v_calibrated if mode == "voltage" else entry.is_calibrated
        if is_done:
            old_slope = entry.v_slope if mode == "voltage" else entry.slope
            old_intercept = entry.v_intercept if mode == "voltage" else entry.intercept
            kind = "voltage" if mode == "voltage" else "current"
            reply = QtWidgets.QMessageBox.question(
                self,
                "Re-calibrate?",
                f"Potentiostat {entry.name} already has a {kind} calibration "
                f"(slope = {old_slope:.4f}, intercept = {old_intercept:.4f}).\n\n"
                "Re-calibrate and overwrite these values?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Cancel,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return

        if mode == "voltage":
            # One-time wiring reminder: the voltage sweep needs the meter in a
            # different configuration than the current sweep.
            proceed = QtWidgets.QMessageBox.information(
                self,
                "Voltage calibration setup",
                "Wire the dummy cell as for current calibration, but set the "
                "multimeter to DC voltage and connect it across WE and RE.\n\n"
                "The sweep applies potentials from "
                f"{min(calibration_sweep.CALIBRATION_POTENTIALS_V):g} V to "
                f"{max(calibration_sweep.CALIBRATION_POTENTIALS_V):g} V.",
                QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Ok,
            )
            if proceed != QtWidgets.QMessageBox.Ok:
                return

        ps = self._open_potentiostat(entry)
        if ps is None:
            return

        # Snapshot the existing fit so we can restore the plot if the user
        # cancels mid-sweep without losing the visual reference.
        if is_done:
            if mode == "voltage":
                self._calib_previous = (entry.v_slope, entry.v_intercept)
            else:
                self._calib_previous = (entry.slope, entry.intercept)
        else:
            self._calib_previous = None

        self._calib_row = row
        self._calib_mode = mode
        self._calib_points = []
        x_label, y_label = _MODE_LABELS[mode]
        self.plot_widget.setLabel("bottom", x_label)
        self.plot_widget.setLabel("left", y_label)
        self._render_plot([], None)
        self.table.item(row, COL_STATUS).setText("Calibrating…")
        self._set_status(f"Calibrating {entry.name} ({mode}) — follow the on-screen prompts.")

        self._calib_worker = CalibrationWorker(ps, mode=mode, parent=self)
        self._calib_worker.point_ready.connect(self._on_point_ready)
        self._calib_worker.point_recorded.connect(self._on_point_recorded)
        self._calib_worker.finished_ok.connect(self._on_calibration_finished_ok)
        self._calib_worker.cancelled.connect(self._on_calibration_cancelled)
        self._calib_worker.failed.connect(self._on_calibration_failed)
        self._update_calibrate_button_states()
        self.scan_button.setEnabled(False)
        self._calib_worker.start()

    def _open_potentiostat(self, entry: Entry):
        """Open a connection to the entry's potentiostat (real or mock)."""
        if self.mock_checkbox.isChecked():
            ps = MockPotentiostat(serial_port=entry.port or f"MOCK_{entry.name}")
            ps.connect()
            return ps
        if not entry.port:
            QtWidgets.QMessageBox.warning(
                self, "No port",
                f"No serial port is recorded for {entry.name}. Run a scan first."
            )
            return None
        try:
            ps = Potentiostat(serial_port=entry.port)
            ps.connect()
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "Connection failed",
                f"Could not open {entry.port}:\n{e}"
            )
            return None
        return ps

    def _on_point_ready(self, applied: float) -> None:
        """The worker is holding the requested output; ask the user for the reading."""
        if self._calib_mode == "voltage":
            prompt = f"Applied {applied:g} V — enter the measured potential (V):"
        else:
            prompt = f"Applied {applied:g} mA — enter the measured current (mA):"
        value, ok = QtWidgets.QInputDialog.getDouble(
            self,
            "Multimeter reading",
            prompt,
            value=applied,
            min=-1000.0,
            max=1000.0,
            decimals=4,
        )
        if not ok:
            if self._calib_worker is not None:
                self._calib_worker.cancel()
            return
        if self._calib_worker is not None:
            self._calib_worker.submit_measured_value(value)

    def _on_point_recorded(self, applied: float, measured: float) -> None:
        self._calib_points.append((applied, measured))
        self._render_plot(self._calib_points, None)

    def _on_calibration_finished_ok(self, result: dict) -> None:
        slope = result["slope"]
        intercept = result["intercept"]
        r_squared = result["r_squared"]
        read_slope = result.get("read_slope")
        read_intercept = result.get("read_intercept")
        read_r2 = result.get("read_r_squared")
        mode = result.get("mode", "current")

        row = self._calib_row
        if row is not None and 0 <= row < len(self.entries):
            entry = self.entries[row]
            if mode == "voltage":
                entry.v_slope = slope
                entry.v_intercept = intercept
                entry.v_read_slope = read_slope
                entry.v_read_intercept = read_intercept
            else:
                entry.slope = slope
                entry.intercept = intercept
                entry.read_slope = read_slope
                entry.read_intercept = read_intercept
            self._save()

        self._render_plot(self._calib_points, (slope, intercept), r_squared)
        kind = "Voltage" if mode == "voltage" else "Current"
        status = (
            f"{kind} calibration complete: slope = {slope:.4f}, intercept = {intercept:.4f}, "
            f"R² = {r_squared:.4f}"
        )
        if read_slope is not None:
            status += (
                f" | read: slope = {read_slope:.4f}, intercept = {read_intercept:.4f}, "
                f"R² = {read_r2:.4f}"
            )
        else:
            status += " | read calibration could not be collected — re-run recommended"
        self._set_status(status)
        self._end_calibration()
        self._refresh_table()

    def _on_calibration_cancelled(self) -> None:
        self._set_status("Calibration cancelled. Previous values kept.")
        self._end_calibration()
        # Restore the previous fit on the plot if there was one.
        if self._calib_previous is not None:
            self._render_plot([], self._calib_previous)
        else:
            self._render_plot([], None)
        self._refresh_table()

    def _on_calibration_failed(self, message: str) -> None:
        self._set_status(f"Calibration failed: {message}")
        QtWidgets.QMessageBox.warning(
            self, "Calibration failed",
            f"The calibration sweep ended with an error:\n\n{message}\n\n"
            "Previous values were kept."
        )
        self._end_calibration()
        if self._calib_previous is not None:
            self._render_plot([], self._calib_previous)
        else:
            self._render_plot([], None)
        self._refresh_table()

    def _end_calibration(self) -> None:
        if self._calib_worker is not None:
            self._calib_worker.wait(2000)
            self._calib_worker.deleteLater()
        self._calib_worker = None
        self._calib_row = None
        self._calib_points = []
        self._calib_previous = None
        self.scan_button.setEnabled(True)
        self._update_calibrate_button_states()

    # ---- Persistence helpers ----

    def _save(self) -> None:
        try:
            save_registry(self.entries, USER_REGISTRY_PATH)
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "Save failed",
                f"Could not write {USER_REGISTRY_PATH}:\n{e}"
            )

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        # Refuse to close while hardware is energised. Cancel first, then
        # let the worker's finally block open the switch and disconnect.
        if self._calib_worker is not None:
            reply = QtWidgets.QMessageBox.question(
                self, "Calibration running",
                "A calibration is in progress. Cancel it and quit?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
            self._calib_worker.cancel()
            self._calib_worker.wait(3000)
        super().closeEvent(event)


def _fmt_optional(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.4f}"


def _sort_key(name: str) -> int:
    """Excel-style column-name sort key (A < B < ... < Z < AA < AB)."""
    if not name:
        return 10**9
    n = 0
    for ch in name:
        if not ch.isalpha():
            return 10**9
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return n


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    apply_app_icon(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

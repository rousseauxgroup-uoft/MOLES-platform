"""Top-level window for the multichannel electrolysis UI.

Owns the experiment table, the live plot grid, the AC waveform preview, and
the buttons that start/stop/reconnect potentiostats. Each row in the table
maps 1:1 to a connected potentiostat; running an experiment hands its row
parameters off to an ``ElectrolysisWorker`` which executes one of the
``moles.methods`` control loops on a background thread.
"""

import bisect
import logging
import os
import sys
import threading
from collections import deque

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from ... import claims
from ..visual_identity import apply_app_icon
from ...calibration import POTENTIOSTAT_CALIBRATIONS, get_potentiostat_ports
from ...data_io import calculate_total_charge
from ...driver.mock import MockPotentiostat
from ...driver.ps4_ref import (
    HW_VOLTAGE_COMPLIANCE_V,
    VOLTAGE_WARN_THRESHOLD_V,
    Potentiostat,
)
from ...logging_setup import install_qt_message_handler, setup_logging
from ...session import connect_with_retry, emergency_cleanup
from ...settings import get_show_electrolysis_plot_after_run
from .guardian import Guardian
from .table import (
    ALL_METHOD_COLS,
    COL_AC_DUTY,
    COL_AC_FREQ,
    COL_AC_NEG,
    COL_AC_POS,
    COL_AC_WAVE,
    COL_AP_NEG,
    COL_AP_POS,
    COL_AP_SAMPLE,
    COL_CC_CURRENT,
    COL_CP_POTENTIAL,
    COL_ELECTRONS,
    COL_EXP_NAME,
    COL_ID,
    COL_METHOD,
    COL_MMOL,
    COL_PROGRESS,
    COL_STATUS,
    COL_TARGET_C,
    COL_TIME_LIMIT_MIN,
    METHOD_COLS,
    METHOD_LABELS,
    ROW_DEFAULTS,
    TABLE_HEADERS,
    _BackgroundPreservingDelegate,
    _increment_exp_name,
)
from ..scrollable_plots import PlotScrollArea, apply_scroll_height
from .visualization_tab import VisualizationTab
from .widgets import WaveformPreviewWidget
from .workers import (
    DatasetLoadWorker,
    ElectrolysisSignals,
    ElectrolysisWorker,
    ReconnectWorker,
)


logger = logging.getLogger(__name__)


# ----- Display constants -----
# Number of recent points kept in the live plot buffer (60 min at the 1 Hz DC rate).
DISPLAY_BUFFER_SIZE = 3600
# Keep 1 history point per N data points for the full-run trend overview.
HISTORY_DOWNSAMPLE = 10
# How often the GUI drains worker data buffers into the plots/table.
DRAIN_INTERVAL_MS = 100
# Cap per-channel data buffered for the GUI; if the GUI stalls, old points are
# dropped from the display (the CSV still receives every sample).
DATA_BUFFER_MAXLEN = 20000
# Ceiling on points one drain tick takes from a single channel. A tick with no
# ceiling can outlast its own interval on a backlogged channel, and because the
# backlog then keeps growing the event loop never regains control — the cause of
# the 2026-09-17 multi-channel freeze. Bounding the tick keeps the UI alive and
# lets a backlog drain over several ticks instead of one unbounded one.
MAX_DRAIN_PER_CHANNEL = 500

# ----- Live plot refresh -----
# Curves repaint on their own timer rather than once per incoming sample, so
# plot cost can never be multiplied by the size of a data backlog.
PLOT_REFRESH_MS = 250
# Channels repainted per refresh tick, oldest-dirty first. A full 16-board grid
# spreads its repaint over several ticks rather than blocking on one long one.
PLOTS_PER_REFRESH = 4
# Points drawn per curve: the newest RECENT_CURVE_POINTS samples at full
# resolution plus the decimated earlier history, capped at MAX_CURVE_POINTS.
# Without a cap the arrays handed to pyqtgraph grow with elapsed run time, so a
# five-hour run costs several times what its first minute did.
MAX_CURVE_POINTS = 2000
RECENT_CURVE_POINTS = 1200

# ----- Experiment worker pool -----
# Experiment workers spend almost all their time asleep between 1 Hz samples, so
# the pool is sized by how many boards a rig can hold, not by CPU count. Qt's
# default (one thread per logical processor) silently queues the 13th experiment
# on a 12-thread machine and it never starts.
WORKER_POOL_SIZE = 32

# ----- Guardian heartbeat -----
# How often the GUI thread tells the guardian it is still alive. Must be well
# under the guardian's staleness threshold so a healthy but busy event loop
# isn't mistaken for a stalled one. The worker watchdogs themselves live in
# guardian.py, on a thread a stalled event loop cannot take down.
HEARTBEAT_MS = 1000

# How often idle rows re-check the cross-process claim files to show which
# boards other MOLES apps are currently using.
CLAIMS_POLL_MS = 3000


class GuardianSignals(QtCore.QObject):
    """Carries the guardian thread's findings back onto the GUI thread.

    The guardian deliberately knows nothing about Qt, but the user still has to
    see what it did. These signals are emitted from the guardian thread and
    delivered as queued events, so the slots run on the GUI thread whenever it
    next gets around to them — and the hardware recovery never waits for that.
    """
    status = QtCore.pyqtSignal(object, str)    # pot_id, status text
    reaped = QtCore.pyqtSignal(object)         # pot_id of an unrecoverable worker
    never_started = QtCore.pyqtSignal(object)  # pot_id of a run that never began
    emergency_stop = QtCore.pyqtSignal(int)    # experiments the stop file halted


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MOLES: Multichannel Electrolysis")
        self.resize(1600, 900)

        # pot_id -> driver object. Ports are NOT held open between runs: each
        # experiment claims the board, opens the port, and closes/releases it
        # when done, so other MOLES apps can use idle boards in parallel.
        self.potentiostats = {}
        self._ports = {}       # pot_id -> serial port path
        self._claim_keys = {}  # pot_id -> cross-process claim key (real boards only)
        self.workers = {}  # pot_id -> worker
        # Tracks rows scheduled to start via the staggered Start Checked action
        # but not yet in self.workers — prevents a double-click from queueing a
        # duplicate worker for the same potentiostat within the 200 ms stagger.
        self._pending_starts = set()
        self.signals = ElectrolysisSignals()
        self.signals.finished.connect(self.on_process_finished)
        self.signals.status_updated.connect(self.on_status_updated)

        # A private pool, so this window's long-lived experiment workers are
        # never rationed against Qt's CPU-count default (see WORKER_POOL_SIZE).
        self.threadpool = QtCore.QThreadPool(self)
        self.threadpool.setMaxThreadCount(WORKER_POOL_SIZE)

        # Per-channel data buffers filled by workers and drained here on a
        # timer (replaces one queued signal per sample — see SignalQueue).
        self._data_buffers = {}  # pot_id -> deque
        self._drain_timer = QtCore.QTimer(self)
        self._drain_timer.timeout.connect(self._drain_data_buffers)
        self._drain_timer.start(DRAIN_INTERVAL_MS)

        # Channels whose curves hold undrawn samples. Draining only records the
        # pid here; the repaint itself happens on its own slower timer so a data
        # backlog cannot turn into a proportional pile of repaints.
        self._dirty_plots = set()
        self._dirty_order = deque()  # same pids, oldest-dirty first
        self._plot_timer = QtCore.QTimer(self)
        self._plot_timer.timeout.connect(self._refresh_dirty_plots)
        self._plot_timer.start(PLOT_REFRESH_MS)

        # Safety net that survives a stalled event loop: it force-closes the
        # ports of workers hung after a stop request, cancels runs that never
        # started, honours the emergency stop file, and dumps thread stacks if
        # this GUI thread stops responding. See guardian.py for why none of
        # that can live here.
        self.guardian_signals = GuardianSignals()
        self.guardian_signals.status.connect(self.on_status_updated)
        self.guardian_signals.reaped.connect(self._on_worker_reaped)
        self.guardian_signals.never_started.connect(self._on_worker_never_started)
        self.guardian_signals.emergency_stop.connect(self._on_emergency_stop)
        self.guardian = Guardian(
            self.workers,
            self.potentiostats,
            on_status=self.guardian_signals.status.emit,
            on_reaped=self.guardian_signals.reaped.emit,
            on_never_started=self.guardian_signals.never_started.emit,
            on_emergency_stop=self.guardian_signals.emergency_stop.emit,
        )
        self.guardian.start()

        self._heartbeat_timer = QtCore.QTimer(self)
        self._heartbeat_timer.timeout.connect(self.guardian.beat)
        self._heartbeat_timer.start(HEARTBEAT_MS)

        # Reflect other apps' board claims ("In use: electroanalysis ...") in
        # the status column of idle rows.
        self._claims_timer = QtCore.QTimer(self)
        self._claims_timer.timeout.connect(self._refresh_claim_statuses)
        self._claims_timer.start(CLAIMS_POLL_MS)

        # Guard so we can mutate cells programmatically without re-entering
        # the itemChanged handler (which would cause infinite recursion).
        self._suppress_item_changed = False

        # Guard + state for the frozen-column batch-select checkboxes.
        self._suppress_frozen_item_changed = False
        self._check_anchor_row = None  # last checkbox toggled, for shift-click ranges
        # pids of running rows the user has unlocked for live ("mid-run") editing.
        self._live_edit_rows = set()

        # Setup UI
        self.setup_ui()
        self.auto_connect()

    def setup_ui(self):
        # ----- Left Panel: experiment table + preview + controls -----
        # Becomes the QMainWindow's central widget; the live plot grid lives in
        # a QDockWidget on the right so users can pop it out onto a second screen.
        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_panel.setMinimumWidth(700)

        # Experiment table — one row per potentiostat, every parameter in-place editable.
        self.ps_table = QtWidgets.QTableWidget()
        self.ps_table.setColumnCount(len(TABLE_HEADERS))
        self.ps_table.setHorizontalHeaderLabels(TABLE_HEADERS)
        header = self.ps_table.horizontalHeader()
        # Interactive, not ResizeToContents: auto-sizing re-measures every cell
        # in the table on each data change, and the progress cells change once a
        # second per running channel. Widths are set once from the populated
        # rows instead (see _size_table_columns).
        header.setSectionResizeMode(QtWidgets.QHeaderView.Interactive)
        self.ps_table.verticalHeader().setVisible(False)
        self.ps_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.ps_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        # Edit on double-click or F2; cells we don't want editable have ItemIsEditable stripped.
        self.ps_table.setEditTriggers(
            QtWidgets.QAbstractItemView.DoubleClicked
            | QtWidgets.QAbstractItemView.EditKeyPressed
            | QtWidgets.QAbstractItemView.AnyKeyPressed
        )
        self.ps_table.itemSelectionChanged.connect(self.on_ps_selected)
        self.ps_table.itemChanged.connect(self._on_item_changed)
        # Right-click context menu for the "fill selected rows" action.
        self.ps_table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.ps_table.customContextMenuRequested.connect(self._show_table_context_menu)
        self.ps_table.setItemDelegate(_BackgroundPreservingDelegate(self.ps_table))

        # Guard to prevent selection-sync feedback loops between the main table
        # and the frozen ID column overlay.
        self._syncing_selection = False
        self._setup_frozen_id_column()

        # Container puts the frozen ID column beside the main table so
        # the ID stays visible during horizontal scrolling.
        table_container = QtWidgets.QWidget()
        table_hbox = QtWidgets.QHBoxLayout(table_container)
        table_hbox.setContentsMargins(0, 0, 0, 0)
        table_hbox.setSpacing(0)
        table_hbox.addWidget(self.frozen_col)
        table_hbox.addWidget(self.ps_table, stretch=1)

        left_layout.addWidget(QtWidgets.QLabel("Experiments (one row per potentiostat):"))
        left_layout.addWidget(table_container, stretch=2)

        # ----- Preview panel: big charge readout + AC waveform preview -----
        preview_group = QtWidgets.QGroupBox("Selected Potentiostat")
        preview_layout = QtWidgets.QVBoxLayout(preview_group)

        self.charge_label = QtWidgets.QLabel("Charge: \u2014")
        self.charge_label.setStyleSheet("font-weight: bold; font-size: 20px; color: magenta;")
        self.charge_label.setAlignment(QtCore.Qt.AlignCenter)
        preview_layout.addWidget(self.charge_label)

        self.waveform_preview = WaveformPreviewWidget()
        preview_layout.addWidget(self.waveform_preview)
        self.waveform_preview.setVisible(False)  # only shown when selected row is AC

        left_layout.addWidget(preview_group, stretch=1)

        # ----- Buttons -----
        # These act on the rows whose checkbox is ticked in the frozen ID column,
        # so users can run, stop, or reset multiple potentiostats in one click.
        btn_layout = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start Checked")
        self.start_btn.setToolTip(
            "Start an experiment on every checked potentiostat.\n"
            "Starts are staggered by 200 ms to limit simultaneous current draw on shared hubs."
        )
        self.start_btn.clicked.connect(self.start_checked)
        self.stop_btn = QtWidgets.QPushButton("Stop Checked")
        self.stop_btn.setToolTip("Stop every checked potentiostat that is currently running.")
        self.stop_btn.clicked.connect(self.stop_checked)
        self.clear_btn = QtWidgets.QPushButton("Clear Checked")
        self.clear_btn.setToolTip(
            "Reset every checked potentiostat's status, progress, and plot so a new\n"
            "experiment can be started on the same row. Running rows are skipped."
        )
        self.clear_btn.clicked.connect(self.clear_checked)
        self.apply_btn = QtWidgets.QPushButton("Apply Changes")
        self.apply_btn.setToolTip(
            "Push edits from any row unlocked with 'Edit live' to its running\n"
            "experiment. Same-method tweaks (current, potential, charge target)\n"
            "apply without interrupting; a method change gracefully restarts the\n"
            "board, carrying the charge passed so far forward."
        )
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(lambda: self._apply_live_edits())
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addWidget(self.apply_btn)
        left_layout.addLayout(btn_layout)

        # Reconnect row — checkbox guards the button to prevent accidental reconnects.
        reconnect_layout = QtWidgets.QHBoxLayout()
        self.reconnect_confirm_cb = QtWidgets.QCheckBox("Allow reconnect")
        self.reconnect_confirm_cb.setToolTip(
            "Check to enable reconnection of the selected potentiostat.\n"
            "Automatically unchecked after use. Disabled while an experiment is running."
        )
        self.reconnect_btn = QtWidgets.QPushButton("Reconnect Selected")
        self.reconnect_btn.setEnabled(False)
        self.reconnect_btn.clicked.connect(self.reconnect_selected)
        self.reconnect_confirm_cb.stateChanged.connect(self._update_reconnect_btn_state)
        reconnect_layout.addWidget(self.reconnect_confirm_cb)
        reconnect_layout.addWidget(self.reconnect_btn)
        left_layout.addLayout(reconnect_layout)

        self.status_label = QtWidgets.QLabel("Ready")
        left_layout.addWidget(self.status_label)

        # Test Mode Checkbox
        self.test_mode_cb = QtWidgets.QCheckBox("Test Mode (Mock Hardware)")
        self.test_mode_cb.stateChanged.connect(self.auto_connect)  # Re-scan on toggle
        left_layout.addWidget(self.test_mode_cb)

        # --- Mock Simulation Controls ---
        self.mock_group = QtWidgets.QGroupBox("Simulation Events")
        self.mock_group.setVisible(False)
        mock_layout = QtWidgets.QVBoxLayout(self.mock_group)

        self.btn_compliance = QtWidgets.QPushButton("Trigger 5V Limit")
        self.btn_compliance.clicked.connect(lambda: self.trigger_mock_event("compliance"))
        mock_layout.addWidget(self.btn_compliance)

        self.btn_short = QtWidgets.QPushButton("Trigger Short Circuit")
        self.btn_short.clicked.connect(lambda: self.trigger_mock_event("short"))
        mock_layout.addWidget(self.btn_short)

        self.btn_disconnect = QtWidgets.QPushButton("Trigger Disconnect")
        self.btn_disconnect.clicked.connect(lambda: self.trigger_mock_event("disconnect"))
        mock_layout.addWidget(self.btn_disconnect)

        self.btn_timeout = QtWidgets.QPushButton("Trigger Serial Timeout")
        self.btn_timeout.clicked.connect(lambda: self.trigger_mock_event("timeout"))
        mock_layout.addWidget(self.btn_timeout)

        self.btn_reset_mock = QtWidgets.QPushButton("Reset Normal")
        self.btn_reset_mock.clicked.connect(lambda: self.trigger_mock_event("reset"))
        mock_layout.addWidget(self.btn_reset_mock)

        left_layout.addWidget(self.mock_group)

        # ----- "Electrolysis" tab: control panel (left) + live plot dock -----
        # The control UI and live plots live inside their own nested window so
        # the plot dock can be popped out (floated onto a second monitor) while
        # staying scoped to this tab — switching to Data Preview hides it.
        self.control_window = QtWidgets.QMainWindow()
        self.control_window.setCentralWidget(left_panel)

        # Live plot grid. Users can float it into its own window via
        # View → Pop Out Plot Window or by dragging its title bar; double-
        # clicking a single plot zooms it (see _on_plot_scene_clicked).
        self.plot_win = pg.GraphicsLayoutWidget()
        self.plot_win.setBackground('w')
        self._focused_pid = None  # pid of the single plot currently zoomed, if any
        self.plot_win.scene().sigMouseClicked.connect(self._on_plot_scene_clicked)

        # Scroll wrapper keeps each plot legible when many boards are connected:
        # 8 plots fill the view, the rest scroll (see scrollable_plots).
        self.plot_scroll = PlotScrollArea(self.plot_win)
        self.plot_scroll.resized.connect(self._update_plot_scroll_height)

        self.plot_dock = QtWidgets.QDockWidget("Live Plots", self.control_window)
        self.plot_dock.setObjectName("plot_dock")
        self.plot_dock.setWidget(self.plot_scroll)
        self.plot_dock.setFeatures(
            QtWidgets.QDockWidget.DockWidgetMovable
            | QtWidgets.QDockWidget.DockWidgetFloatable
        )
        self.plot_dock.setAllowedAreas(
            QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea
        )
        self.control_window.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.plot_dock)
        # Approximate the old 3:4 splitter ratio at startup (1600 px window → ~900 px plots).
        self.control_window.resizeDocks([self.plot_dock], [900], QtCore.Qt.Horizontal)

        # ----- "Data Preview" tab: overlay & analyse finished runs -----
        self.viz_tab = VisualizationTab()

        # Outer tab widget holds the two workspaces side by side.
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.control_window, "Electrolysis")
        self.tabs.addTab(self.viz_tab, "Data Preview")
        self.setCentralWidget(self.tabs)

        # ----- Menu bar: View menu with a pop-out toggle for the plots -----
        view_menu = self.menuBar().addMenu("&View")
        self.pop_out_action = QtWidgets.QAction("Pop Out Plot Window", self, checkable=True)
        self.pop_out_action.setToolTip(
            "Detach the live plots into their own window — useful for dragging "
            "onto a second monitor. Uncheck to dock back in."
        )
        self.pop_out_action.triggered.connect(self.plot_dock.setFloating)
        # Keep the menu checkbox in sync if the user floats/docks via the title bar.
        self.plot_dock.topLevelChanged.connect(self.pop_out_action.setChecked)
        view_menu.addAction(self.pop_out_action)

        # Qt hides QAction tooltips in menus unless asked, and the emergency
        # stop entry below is nothing but its explanation.
        view_menu.setToolTipsVisible(True)
        view_menu.addSeparator()
        self.emergency_stop_action = QtWidgets.QAction("Emergency Stop File...", self)
        self.emergency_stop_action.setToolTip(
            "If this window ever stops responding, creating the file\n"
            f"{self.guardian.stop_file}\n"
            "stops every running experiment. A background thread watches for "
            "it, so it works even when the window is frozen and the Stop "
            "button is dead."
        )
        self.emergency_stop_action.triggered.connect(self._show_emergency_stop_help)
        view_menu.addAction(self.emergency_stop_action)

        # Data & Plots Storage
        self.plots = {}             # pot_id -> dict with plot/curves
        self.plot_data = {}         # pot_id -> dict of buffers

    def _show_emergency_stop_help(self):
        """Show how to stop every experiment when the window is unresponsive."""
        stop_file = self.guardian.stop_file
        QtWidgets.QMessageBox.information(
            self,
            "Emergency Stop File",
            "If this window freezes, the Stop Checked button and the window's "
            "close handler stop working — but the experiments keep running.\n\n"
            "Creating this file stops every running experiment:\n\n"
            f"{stop_file}\n\n"
            "A background thread checks for it every couple of seconds and is "
            "unaffected by a frozen window. It deletes the file once it has "
            "acted, so the next run is unaffected.\n\n"
            "From a terminal:\n"
            f"    Windows:  type nul > \"{stop_file}\"\n"
            f"    macOS/Linux:  touch \"{stop_file}\"\n\n"
            "Do NOT End-task a frozen window: that skips the hardware cleanup "
            "and leaves every board driving current."
        )

    # ----- Frozen ID column -----

    # Column indices inside the frozen overlay (not the main table).
    FROZEN_COL_CHECK = 0
    FROZEN_COL_ID = 1

    # Glyphs shown in the checkbox column header to reflect the select-all state;
    # clicking the header toggles every row's checkbox.
    _CHECK_ALL_NONE = "☐"   # ☐ empty
    _CHECK_ALL_SOME = "◪"   # ◪ partially filled
    _CHECK_ALL_ALL = "☑"    # ☑ checked

    # Background tint for cells unlocked for live (mid-run) editing.
    _LIVE_EDIT_BG = QtGui.QColor(255, 249, 196)

    def _setup_frozen_id_column(self):
        """Place a two-column table to the left of ps_table so the batch-select
        checkbox and potentiostat ID stay visible during horizontal scrolling.

        The main table's COL_ID column is hidden; the frozen column mirrors
        its content and syncs vertical scroll position with the main table.
        Column 0 holds per-row checkboxes used by the batch Start/Stop/Clear
        actions.
        """
        fc = QtWidgets.QTableWidget()
        fc.setColumnCount(2)
        fc.setHorizontalHeaderLabels(["", "ID"])
        fc.horizontalHeaderItem(0).setToolTip(
            "Check rows to include them in the Start / Stop / Clear Checked actions."
        )
        fc.horizontalHeaderItem(0).setText(self._CHECK_ALL_NONE)
        fc.verticalHeader().setVisible(False)
        fc.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        fc.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        fc.horizontalHeader().setSectionResizeMode(
            self.FROZEN_COL_CHECK, QtWidgets.QHeaderView.ResizeToContents
        )
        fc.horizontalHeader().setSectionResizeMode(
            self.FROZEN_COL_ID, QtWidgets.QHeaderView.ResizeToContents
        )
        fc.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        fc.setFocusPolicy(QtCore.Qt.NoFocus)
        fc.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        fc.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        fc.setItemDelegate(_BackgroundPreservingDelegate(fc))
        fc.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Expanding)

        # Sync vertical scroll (main -> frozen only; frozen has no visible scrollbar).
        self.ps_table.verticalScrollBar().valueChanged.connect(
            fc.verticalScrollBar().setValue
        )
        # Allow clicking the ID column to select the corresponding row.
        fc.itemSelectionChanged.connect(self._on_frozen_col_selected)
        # Update the fixed width whenever either column auto-resizes.
        fc.horizontalHeader().sectionResized.connect(
            lambda _idx, _old, _new: self._update_frozen_col_width()
        )
        # Clicking the checkbox-column header toggles every row (check-all).
        fc.horizontalHeader().setSectionsClickable(True)
        fc.horizontalHeader().sectionClicked.connect(self._on_check_all_clicked)
        # Track individual checkbox toggles for the header glyph and shift-click.
        fc.itemChanged.connect(self._on_frozen_check_changed)

        self.frozen_col = fc
        self.ps_table.setColumnHidden(COL_ID, True)
        self._update_frozen_col_width()

    def _update_frozen_col_width(self):
        """Resize the frozen overlay to fit both its columns' content."""
        fc = self.frozen_col
        fc.resizeColumnToContents(self.FROZEN_COL_CHECK)
        fc.resizeColumnToContents(self.FROZEN_COL_ID)
        total = fc.columnWidth(self.FROZEN_COL_CHECK) + fc.columnWidth(self.FROZEN_COL_ID)
        fc.setFixedWidth(total + fc.frameWidth() * 2 + 4)

    def _on_frozen_col_selected(self):
        """Propagate a click on the frozen ID column to the main table selection."""
        if self._syncing_selection:
            return
        rows = self.frozen_col.selectionModel().selectedRows()
        if not rows:
            return
        self._syncing_selection = True
        try:
            self.ps_table.selectRow(rows[0].row())
        finally:
            self._syncing_selection = False

    def auto_connect(self):
        """Build the board table from the registry — without opening any port.

        Ports are opened per-experiment by the worker (claim, connect, run,
        disconnect, release), so listing a board here never blocks another
        MOLES app from using it. The method keeps its historical name because
        the Test Mode checkbox and tests are wired to it.
        """
        logger.info("Listing registered potentiostats...")
        self.ps_table.setRowCount(0)
        self.frozen_col.setRowCount(0)
        self.potentiostats.clear()
        self._ports.clear()
        self._claim_keys.clear()
        self._check_anchor_row = None
        self._live_edit_rows.clear()
        self._sync_check_all_header()

        # Check for Test Mode
        test_mode = getattr(self, 'test_mode_cb', None) and self.test_mode_cb.isChecked()

        if test_mode:
            if MockPotentiostat is None:
                logger.error("MockPotentiostat class not found.")
                return

            logger.info("Test Mode enabled: creating mock potentiostats...")
            mock_ports = {"TEST_A": "MOCK_A", "TEST_B": "MOCK_B", "TEST_C": "MOCK_C"}
            for pid, port in mock_ports.items():
                ps = MockPotentiostat(serial_port=port)
                ps.connect()
                self.potentiostats[pid] = ps
                self._ports[pid] = port
                self.add_ps_ui(pid)
            self._size_table_columns()
            self.status_label.setText(f"Created {len(mock_ports)} Mock devices.")
            return

        # Ensure imports worked
        if not Potentiostat:
            QtWidgets.QMessageBox.warning(self, "No Hardware", "Potentiostat module not loaded.")
            return

        # Re-read the registry on every rescan so boards onboarded while this
        # app is open show up without a restart.
        ports = get_potentiostat_ports()

        for pid, port in ports.items():
            ps = Potentiostat(serial_port=port)
            # Calibration lives on the Python object, not the hardware, so it
            # can be applied before the board is ever connected.
            if pid in POTENTIOSTAT_CALIBRATIONS:
                cal = POTENTIOSTAT_CALIBRATIONS[pid]
                ps.set_calibration_params(cal['slope'], cal['intercept'],
                                          cal.get('read_slope'), cal.get('read_intercept'))
                if cal.get('v_slope') is not None:
                    ps.set_voltage_calibration_params(
                        cal['v_slope'], cal['v_intercept'],
                        cal.get('v_read_slope'), cal.get('v_read_intercept'))
            self.potentiostats[pid] = ps
            self._ports[pid] = port
            self._claim_keys[pid] = claims.claim_key_for(pid, port)
            self.add_ps_ui(pid)
            logger.info("Listed board %s on %s", pid, port)

        board_count = len(ports)
        if board_count == 0:
            self.status_label.setText("No potentiostats registered — run moles-onboard.")
        else:
            self.status_label.setText(
                f"{board_count} board(s) listed. Ports open when an experiment starts."
            )
        self._size_table_columns()
        self._refresh_claim_statuses()

        # Safety: Disable Test Mode if real hardware is listed
        if board_count > 0:
            self.test_mode_cb.setEnabled(False)
            self.test_mode_cb.setToolTip("Test Mode disabled while hardware is listed.")
        else:
            self.test_mode_cb.setEnabled(True)
            self.test_mode_cb.setToolTip("")

    # Widest text each live-updating column has to hold once a run is going.
    # Sizing from the initial cell contents ("Idle", "—") would leave these two
    # columns too narrow to read for the rest of the session.
    _COL_WIDTH_SAMPLES = {
        COL_STATUS: "WARNING: Serial read timeout (0/0)",
        COL_PROGRESS: "0000.00 / 0000.00 C (100.0%)",
    }

    def _size_table_columns(self):
        """Fit the columns to their contents once, after the rows are built.

        Replaces per-change auto-sizing: the widths are computed here and then
        left alone, so updating a progress cell no longer re-measures the table.
        """
        self.ps_table.resizeColumnsToContents()
        metrics = QtGui.QFontMetrics(self.ps_table.font())
        for col, sample in self._COL_WIDTH_SAMPLES.items():
            if self.ps_table.isColumnHidden(col):
                continue
            needed = metrics.horizontalAdvance(sample) + 16  # cell padding
            if self.ps_table.columnWidth(col) < needed:
                self.ps_table.setColumnWidth(col, needed)

    def _refresh_claim_statuses(self):
        """Show which idle boards other MOLES apps are using right now.

        Only rows whose status is "Idle" or a previous "In use ..." are
        touched — experiment outcomes (Completed / Stopped / Error) stay
        visible until the user clears or restarts the row.
        """
        for r in range(self.ps_table.rowCount()):
            pid = self._row_pid(r)
            if pid is None or pid in self.workers or pid in self._pending_starts:
                continue
            key = self._claim_keys.get(pid)
            if key is None:  # Mock rows have no cross-process claims.
                continue
            item = self.ps_table.item(r, COL_STATUS)
            if item is None:
                continue
            current = item.text()
            if current != "Idle" and not current.startswith("In use"):
                continue
            claim = claims.read_claim(key)
            busy = claim is not None and not claims.is_stale(claim)
            new_text = f"In use — {claim.describe()}" if busy else "Idle"
            if new_text == current:
                continue
            self._suppress_item_changed = True
            try:
                item.setText(new_text)
                item.setForeground(
                    QtGui.QBrush(QtCore.Qt.darkYellow if busy else QtCore.Qt.black)
                )
                item.setToolTip(new_text if busy else "")
            finally:
                self._suppress_item_changed = False

    # ----- Table row construction & helpers -----

    def add_ps_ui(self, pid):
        """Add a new row to the experiment table for this potentiostat and create its plot."""
        self._suppress_item_changed = True
        self._suppress_frozen_item_changed = True
        try:
            row_idx = self.ps_table.rowCount()
            self.ps_table.insertRow(row_idx)

            # ID — non-editable, stores pid in UserRole for lookup.
            id_item = QtWidgets.QTableWidgetItem(str(pid))
            id_item.setData(QtCore.Qt.UserRole, pid)
            id_item.setFlags(id_item.flags() & ~QtCore.Qt.ItemIsEditable)
            id_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.ps_table.setItem(row_idx, COL_ID, id_item)

            # Mirror into the frozen overlay. Column 0 is a batch-select checkbox;
            # column 1 is the (non-editable) ID label. Both stay visible while the
            # main table scrolls horizontally.
            self.frozen_col.insertRow(row_idx)

            check_item = QtWidgets.QTableWidgetItem()
            check_item.setFlags(
                QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable
            )
            check_item.setCheckState(QtCore.Qt.Unchecked)
            check_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.frozen_col.setItem(row_idx, self.FROZEN_COL_CHECK, check_item)

            fc_item = QtWidgets.QTableWidgetItem(str(pid))
            fc_item.setFlags(fc_item.flags() & ~QtCore.Qt.ItemIsEditable)
            fc_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.frozen_col.setItem(row_idx, self.FROZEN_COL_ID, fc_item)

            self.frozen_col.setRowHeight(row_idx, self.ps_table.rowHeight(row_idx))
            self._update_frozen_col_width()

            # Status — non-editable.
            status_item = QtWidgets.QTableWidgetItem("Idle")
            status_item.setFlags(status_item.flags() & ~QtCore.Qt.ItemIsEditable)
            self.ps_table.setItem(row_idx, COL_STATUS, status_item)

            # Editable text/number cells with their defaults.
            self._set_editable_cell(row_idx, COL_EXP_NAME, ROW_DEFAULTS[COL_EXP_NAME])
            self._set_editable_cell(row_idx, COL_MMOL, ROW_DEFAULTS[COL_MMOL])
            self._set_editable_cell(row_idx, COL_ELECTRONS, ROW_DEFAULTS[COL_ELECTRONS])
            self._set_editable_cell(row_idx, COL_CC_CURRENT, ROW_DEFAULTS[COL_CC_CURRENT])
            self._set_editable_cell(row_idx, COL_CP_POTENTIAL, ROW_DEFAULTS[COL_CP_POTENTIAL])
            self._set_editable_cell(row_idx, COL_AC_POS, ROW_DEFAULTS[COL_AC_POS])
            self._set_editable_cell(row_idx, COL_AC_NEG, ROW_DEFAULTS[COL_AC_NEG])
            self._set_editable_cell(row_idx, COL_AC_FREQ, ROW_DEFAULTS[COL_AC_FREQ])
            self._set_editable_cell(row_idx, COL_AC_DUTY, ROW_DEFAULTS[COL_AC_DUTY])
            self._set_editable_cell(row_idx, COL_AP_POS, ROW_DEFAULTS[COL_AP_POS])
            self._set_editable_cell(row_idx, COL_AP_NEG, ROW_DEFAULTS[COL_AP_NEG])
            self._set_editable_cell(row_idx, COL_AP_SAMPLE, ROW_DEFAULTS[COL_AP_SAMPLE])

            # Optional time-based stopping criterion. Left blank by default so the
            # row's stop condition is governed solely by Target (C) unless the
            # user opts in. Accepts minutes; converted to seconds at run time.
            self._set_editable_cell(row_idx, COL_TIME_LIMIT_MIN, "")

            # Method combobox cell widget.
            method_combo = QtWidgets.QComboBox()
            method_combo.addItems(list(METHOD_LABELS.keys()))
            method_combo.currentTextChanged.connect(
                lambda _txt, p=pid: self._on_method_changed(p)
            )
            self.ps_table.setCellWidget(row_idx, COL_METHOD, method_combo)

            # Waveform combobox cell widget.
            wave_combo = QtWidgets.QComboBox()
            wave_combo.addItems(["Sine", "Square"])
            wave_combo.currentTextChanged.connect(
                lambda _txt, p=pid: self._on_waveform_changed(p)
            )
            self.ps_table.setCellWidget(row_idx, COL_AC_WAVE, wave_combo)

            # Auto-computed target charge — non-editable.
            target_item = QtWidgets.QTableWidgetItem("")
            target_item.setFlags(target_item.flags() & ~QtCore.Qt.ItemIsEditable)
            self.ps_table.setItem(row_idx, COL_TARGET_C, target_item)

            # Progress — non-editable.
            progress_item = QtWidgets.QTableWidgetItem("\u2014")
            progress_item.setFlags(progress_item.flags() & ~QtCore.Qt.ItemIsEditable)
            self.ps_table.setItem(row_idx, COL_PROGRESS, progress_item)

            # Compute initial target charge from defaults and apply method styling.
            self._refresh_target_charge(row_idx)
            self._apply_method_styling(row_idx)
        finally:
            self._suppress_item_changed = False
            self._suppress_frozen_item_changed = False

        # Now that this row has a method, hide any columns that no row uses.
        self._refresh_visible_method_columns()
        # Reflect the new (unchecked) row in the select-all header glyph.
        self._sync_check_all_header()

        # ----- Plot setup -----
        # New plots are placed in the full-grid view, so restore it first if a
        # single plot is currently zoomed.
        if self._focused_pid is not None:
            self._restore_grid()

        plots_so_far = len(self.plots)
        r = plots_so_far // 2
        c = plots_so_far % 2

        p = self.plot_win.addPlot(row=r, col=c, title=f"PS {pid}")
        p.setLimits(xMin=0, minXRange=1.0 / 60.0)
        p.setLabel('bottom', 'Time (min)')
        p.showGrid(x=True, y=True)
        p.addLegend()
        p.setLimits(xMin=0)

        # Record the grid position so the plot can be restored to its slot
        # after a single-plot focus zoom.
        self.plots[pid] = {'plot': p, 'curve': None, 'row': r, 'col': c}
        self.plot_data[pid] = {
            't': deque(maxlen=DISPLAY_BUFFER_SIZE),
            'i': deque(maxlen=DISPLAY_BUFFER_SIZE),
            'v': deque(maxlen=DISPLAY_BUFFER_SIZE),
            'history_t': [], 'history_i': [], 'history_v': [], 'history_counter': 0,
        }
        self._data_buffers[pid] = deque(maxlen=DATA_BUFFER_MAXLEN)

        # Grow the scrollable plot area to keep each plot legible (8 in view).
        self._update_plot_scroll_height()

    # ----- Cell helpers -----

    def _row_time_limit_s(self, row):
        """Return the row's time-limit cell value in seconds, or None if unset."""
        minutes = self._cell_float(row, COL_TIME_LIMIT_MIN, 0.0)
        return minutes * 60.0 if minutes > 0 else None

    def _format_progress(self, row, q_passed, t_passed_s):
        """Format the progress cell text for either a charge- or time-bound run.

        Charge is preferred when both criteria are set, since chemists typically
        target a coulomb count via Faraday's law; time-only runs (target charge
        is zero / blank) display elapsed-versus-limit minutes instead.
        """
        try:
            target_c = float(self.ps_table.item(row, COL_TARGET_C).text())
        except (AttributeError, ValueError):
            target_c = 0.0
        if target_c > 0:
            percent = (q_passed / target_c * 100.0)
            return f"{q_passed:.2f} / {target_c:.2f} C ({percent:.1f}%)"
        time_limit_s = self._row_time_limit_s(row)
        if time_limit_s is not None:
            percent = (t_passed_s / time_limit_s * 100.0)
            return (
                f"{t_passed_s / 60.0:.1f} / {time_limit_s / 60.0:.1f} min "
                f"({percent:.1f}%)"
            )
        return "—"

    def _set_editable_cell(self, row, col, value):
        """Create or update an editable text cell."""
        item = self.ps_table.item(row, col)
        if item is None:
            item = QtWidgets.QTableWidgetItem()
            self.ps_table.setItem(row, col, item)
        if isinstance(value, float):
            text = f"{value:g}"
        else:
            text = str(value)
        item.setText(text)
        item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)

    def _find_row_for_pid(self, pid):
        for r in range(self.ps_table.rowCount()):
            item = self.ps_table.item(r, COL_ID)
            if item and item.data(QtCore.Qt.UserRole) == pid:
                return r
        return -1

    def _row_pid(self, row):
        item = self.ps_table.item(row, COL_ID)
        return item.data(QtCore.Qt.UserRole) if item else None

    def _row_method_key(self, row):
        combo = self.ps_table.cellWidget(row, COL_METHOD)
        if combo is None:
            return "constant_current"
        return METHOD_LABELS.get(combo.currentText(), "constant_current")

    def _cell_float(self, row, col, default=0.0):
        item = self.ps_table.item(row, col)
        if item is None:
            return default
        try:
            return float(item.text())
        except (TypeError, ValueError):
            return default

    def _cell_text(self, row, col, default=""):
        item = self.ps_table.item(row, col)
        return item.text() if item else default

    def _refresh_target_charge(self, row):
        mmol = self._cell_float(row, COL_MMOL, 0.0)
        electrons = self._cell_float(row, COL_ELECTRONS, 0.0)
        target_c = calculate_total_charge(mmol, electrons)
        item = self.ps_table.item(row, COL_TARGET_C)
        if item is not None:
            item.setText(f"{target_c:.2f}")

    def _refresh_visible_method_columns(self):
        """Hide method-specific columns that no current row uses.

        Keeps the table narrower when not every method is in play — e.g. if
        every row is set to Constant Current, the AC/AP/CP columns all hide.
        Non-method columns (ID, Status, Exp Name, Progress, Method, Scale,
        e⁻, Target) are always visible.
        """
        if self.ps_table.rowCount() == 0:
            # With no rows yet, leave all method columns visible so the header
            # gives the user a preview of available parameters.
            for col in ALL_METHOD_COLS:
                self.ps_table.setColumnHidden(col, False)
            return
        needed = set()
        for r in range(self.ps_table.rowCount()):
            needed |= METHOD_COLS.get(self._row_method_key(r), set())
        for col in ALL_METHOD_COLS:
            self.ps_table.setColumnHidden(col, col not in needed)

    def _apply_method_styling(self, row):
        """Grey out and lock the method-irrelevant cells in this row.

        Disabled cells get a filled light-grey background so it's obvious at a
        glance which parameters apply to the row's current method.
        """
        method = self._row_method_key(row)
        relevant = METHOD_COLS.get(method, set())
        disabled_bg = QtGui.QBrush(QtGui.QColor(220, 220, 220))
        enabled_bg = QtGui.QBrush(QtCore.Qt.white)
        for col in ALL_METHOD_COLS:
            is_relevant = col in relevant
            if col == COL_AC_WAVE:
                # Combobox cell widget — toggle enabled state.
                w = self.ps_table.cellWidget(row, col)
                if w is not None:
                    w.setEnabled(is_relevant)
                continue
            item = self.ps_table.item(row, col)
            if item is None:
                continue
            flags = item.flags()
            if is_relevant:
                item.setFlags(flags | QtCore.Qt.ItemIsEditable)
                item.setForeground(QtGui.QBrush(QtCore.Qt.black))
                item.setBackground(enabled_bg)
            else:
                item.setFlags(flags & ~QtCore.Qt.ItemIsEditable)
                item.setForeground(QtGui.QBrush(QtCore.Qt.gray))
                item.setBackground(disabled_bg)

    # ----- Cell change handlers -----

    def _on_item_changed(self, item):
        if self._suppress_item_changed:
            return
        row = item.row()
        col = item.column()
        if col in (COL_MMOL, COL_ELECTRONS):
            self._refresh_target_charge(row)
        # If waveform parameters changed and this row is currently selected, redraw preview.
        if col in (COL_AC_POS, COL_AC_NEG, COL_AC_FREQ, COL_AC_DUTY, COL_AP_POS, COL_AP_NEG):
            sel = self.ps_table.selectionModel().selectedRows()
            if sel and sel[0].row() == row:
                self._update_preview_for_row(row)

    def _on_method_changed(self, pid):
        row = self._find_row_for_pid(pid)
        if row < 0:
            return
        self._apply_method_styling(row)
        self._refresh_visible_method_columns()
        # Keep the live-edit tint on the (now different) setpoint cells if this
        # row is mid-edit — _apply_method_styling above repainted them.
        if pid in self._live_edit_rows:
            self._paint_live_edit_row(row, True)
        sel = self.ps_table.selectionModel().selectedRows()
        if sel and sel[0].row() == row:
            self._update_preview_for_row(row)

    def _on_waveform_changed(self, pid):
        row = self._find_row_for_pid(pid)
        if row < 0:
            return
        sel = self.ps_table.selectionModel().selectedRows()
        if sel and sel[0].row() == row:
            self._update_preview_for_row(row)

    # ----- Preview panel -----

    def _update_preview_for_row(self, row):
        method = self._row_method_key(row)
        # Charge label
        try:
            target_c = float(self.ps_table.item(row, COL_TARGET_C).text())
        except (AttributeError, ValueError):
            target_c = 0.0
        progress_text = self._cell_text(row, COL_PROGRESS, "\u2014")
        self.charge_label.setText(f"Target: {target_c:.2f} C   |   {progress_text}")

        # Waveform preview for AC and AP methods.
        if method == "alternating_current":
            self.waveform_preview.setVisible(True)
            self.waveform_preview.getPlotItem().setLabel('left', 'mA')
            wave_combo = self.ps_table.cellWidget(row, COL_AC_WAVE)
            wave_type = wave_combo.currentText() if wave_combo else "Sine"
            self.waveform_preview.set_waveform(
                pos_peak=self._cell_float(row, COL_AC_POS, 0.0),
                neg_peak=self._cell_float(row, COL_AC_NEG, 0.0),
                freq=self._cell_float(row, COL_AC_FREQ, 0.1),
                duty_pct=self._cell_float(row, COL_AC_DUTY, 50.0),
                wave_type=wave_type,
            )
        elif method == "alternating_polarity":
            self.waveform_preview.setVisible(True)
            self.waveform_preview.getPlotItem().setLabel('left', 'V')
            wave_combo = self.ps_table.cellWidget(row, COL_AC_WAVE)
            wave_type = wave_combo.currentText() if wave_combo else "Square"
            self.waveform_preview.set_waveform(
                pos_peak=self._cell_float(row, COL_AP_POS, 1.0),
                neg_peak=self._cell_float(row, COL_AP_NEG, -1.0),
                freq=self._cell_float(row, COL_AC_FREQ, 1.0),
                duty_pct=self._cell_float(row, COL_AC_DUTY, 50.0),
                wave_type=wave_type,
            )
        else:
            self.waveform_preview.setVisible(False)

    def on_ps_selected(self):
        rows = self.ps_table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        self._update_preview_for_row(row)
        if not self._syncing_selection:
            self._syncing_selection = True
            try:
                self.frozen_col.selectRow(row)
            finally:
                self._syncing_selection = False

        # Check if Mock and show controls
        pid = self._row_pid(row)
        ps = self.potentiostats.get(pid)
        is_mock = (MockPotentiostat is not None) and isinstance(ps, MockPotentiostat)
        if hasattr(self, 'mock_group'):
            self.mock_group.setVisible(is_mock)

        self._update_reconnect_btn_state()

    def _update_reconnect_btn_state(self):
        """Enable the Reconnect button only when the checkbox is checked, a row is
        selected, and that potentiostat is not currently running an experiment."""
        rows = self.ps_table.selectionModel().selectedRows()
        confirmed = self.reconnect_confirm_cb.isChecked()
        if not rows or not confirmed:
            self.reconnect_btn.setEnabled(False)
            return
        pid = self._row_pid(rows[0].row())
        self.reconnect_btn.setEnabled(pid is not None and pid not in self.workers)

    def reconnect_selected(self):
        """Disconnect and reconnect the selected potentiostat to recover from serial errors."""
        rows = self.ps_table.selectionModel().selectedRows()
        if not rows:
            return
        pid = self._row_pid(rows[0].row())
        if pid is None or pid in self.workers:
            return
        ps = self.potentiostats.get(pid)
        if ps is None:
            return

        # Disable button and uncheck guard immediately to prevent duplicate attempts.
        self.reconnect_btn.setEnabled(False)
        self.reconnect_confirm_cb.setChecked(False)
        self.status_label.setText(f"Cycling PS {pid} port...")

        worker = ReconnectWorker(
            pid, ps, POTENTIOSTAT_CALIBRATIONS,
            claim_key=self._claim_keys.get(pid),
            port=self._ports.get(pid, ""),
        )
        worker.signals.success.connect(self._on_reconnect_success)
        worker.signals.failure.connect(self._on_reconnect_failure)
        self.threadpool.start(worker)

    def _on_reconnect_success(self, pid):
        self.status_label.setText(f"PS {pid} port cycled OK — board is responsive.")
        logger.info("[PS %s] Port cycled successfully.", pid)

    def _on_reconnect_failure(self, pid, error):
        self.status_label.setText(f"PS {pid} port cycle failed: {error}")
        logger.error("[PS %s] Port cycle failed: %s", pid, error)

    # ----- Right-click fill-below context menu -----

    def _show_table_context_menu(self, pos):
        index = self.ps_table.indexAt(pos)
        if not index.isValid():
            return
        col = index.column()
        source_row = index.row()
        pid = self._row_pid(source_row)
        menu = QtWidgets.QMenu(self)

        # Live-edit actions for a running row (see feature: mid-run editing).
        edit_action = apply_action = cancel_action = None
        if pid is not None and pid in self.workers:
            if pid in self._live_edit_rows:
                apply_action = menu.addAction("Apply changes to this experiment")
                cancel_action = menu.addAction("Cancel live edit (relock row)")
            else:
                edit_action = menu.addAction("Edit live (unlock this running row)")

        # Fill-below only on editable parameter columns, with a row below to fill.
        fill_action = incr_action = None
        if col not in (COL_ID, COL_STATUS, COL_TARGET_C, COL_PROGRESS) \
                and source_row < self.ps_table.rowCount() - 1:
            if not menu.isEmpty():
                menu.addSeparator()
            fill_action = menu.addAction("Fill cells below with this value")
            if col == COL_EXP_NAME:
                incr_action = menu.addAction("Fill cells below with incremented name")

        if menu.isEmpty():
            return
        chosen = menu.exec_(self.ps_table.viewport().mapToGlobal(pos))
        # Parented to the window, so it would otherwise be kept alive (with its
        # native popup) for the lifetime of the app, once per right-click.
        menu.deleteLater()
        if chosen is None:
            return
        if chosen == edit_action:
            self._enter_live_edit(source_row, pid)
        elif chosen == apply_action:
            self._apply_live_edits(rows=[source_row])
        elif chosen == cancel_action:
            self._exit_live_edit(source_row, pid)
        elif chosen == fill_action:
            self._fill_below(source_row, col, increment=False)
        elif incr_action and chosen == incr_action:
            self._fill_below(source_row, col, increment=True)

    def _fill_below(self, source_row, col, increment=False):
        """Write the value from source_row into every unlocked row below it.

        If increment=True and col is COL_EXP_NAME, each successive cell gets an
        auto-incremented name (e.g. Exp-1 → Exp-2 → Exp-3, or Exp-A → Exp-B).
        """
        if col == COL_METHOD or col == COL_AC_WAVE:
            src_widget = self.ps_table.cellWidget(source_row, col)
            if src_widget is None:
                return
            value = src_widget.currentText()
        else:
            src_item = self.ps_table.item(source_row, col)
            if src_item is None:
                return
            value = src_item.text()

        self._suppress_item_changed = True
        try:
            current_value = value
            for r in range(source_row + 1, self.ps_table.rowCount()):
                pid = self._row_pid(r)
                if pid in self.workers:
                    continue  # Don't modify running rows.
                if increment and col == COL_EXP_NAME:
                    current_value = _increment_exp_name(current_value)
                if col == COL_METHOD:
                    w = self.ps_table.cellWidget(r, col)
                    if w is not None:
                        w.setCurrentText(current_value)
                elif col == COL_AC_WAVE:
                    w = self.ps_table.cellWidget(r, col)
                    if w is not None and w.isEnabled():
                        w.setCurrentText(current_value)
                else:
                    item = self.ps_table.item(r, col)
                    if item is not None and (item.flags() & QtCore.Qt.ItemIsEditable):
                        item.setText(current_value)
                        if col in (COL_MMOL, COL_ELECTRONS):
                            self._refresh_target_charge(r)
        finally:
            self._suppress_item_changed = False

    # ----- Row locking while running -----

    def _set_row_locked(self, row, locked):
        """Lock or unlock every editable cell + cell widget in a row."""
        for col in range(self.ps_table.columnCount()):
            if col in (COL_ID, COL_STATUS, COL_TARGET_C, COL_PROGRESS):
                continue
            if col == COL_METHOD or col == COL_AC_WAVE:
                w = self.ps_table.cellWidget(row, col)
                if w is not None:
                    if locked:
                        w.setEnabled(False)
                    else:
                        # Re-enable waveform combo only if AC or AP is the current method.
                        if col == COL_AC_WAVE:
                            method = self._row_method_key(row)
                            w.setEnabled(method in ("alternating_current", "alternating_polarity"))
                        else:
                            w.setEnabled(True)
                continue
            item = self.ps_table.item(row, col)
            if item is None:
                continue
            if locked:
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
            else:
                # Re-enable editing on every input cell; method-irrelevant cells
                # are re-locked below by _apply_method_styling.
                item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)
        if not locked:
            self._apply_method_styling(row)

    # ----- Live (mid-run) editing -----

    # Editable columns offered for a live edit, on top of the method-specific
    # setpoint columns (which are added per row from METHOD_COLS).
    _LIVE_EDIT_BASE_COLS = (COL_EXP_NAME, COL_MMOL, COL_ELECTRONS, COL_TIME_LIMIT_MIN)

    def _live_edit_cols(self, row):
        """Columns unlocked for a live edit on this row (base + method setpoints)."""
        method = self._row_method_key(row)
        return list(self._LIVE_EDIT_BASE_COLS) + list(METHOD_COLS.get(method, set()))

    def _paint_live_edit_row(self, row, editing):
        """Tint (or clear the tint on) the cells unlocked for a live edit."""
        self._suppress_item_changed = True
        try:
            for col in self._live_edit_cols(row):
                if col == COL_AC_WAVE:
                    continue  # combobox cell widget — no background to tint
                item = self.ps_table.item(row, col)
                if item is None:
                    continue
                item.setBackground(
                    QtGui.QBrush(self._LIVE_EDIT_BG) if editing
                    else QtGui.QBrush(QtCore.Qt.white)
                )
        finally:
            self._suppress_item_changed = False

    def _enter_live_edit(self, row, pid):
        """Unlock a running row so its parameters can be edited on the fly."""
        if pid is None or pid not in self.workers:
            return
        self._live_edit_rows.add(pid)
        self._set_row_locked(row, False)   # unlock + re-apply method styling
        self._paint_live_edit_row(row, True)
        self._update_apply_btn_state()
        self.status_label.setText(
            f"PS {pid}: editing live — change values, then Apply Changes."
        )

    def _exit_live_edit(self, row, pid):
        """Leave live-edit mode for a row, re-locking it if it is still running."""
        self._live_edit_rows.discard(pid)
        self._paint_live_edit_row(row, False)
        if pid in self.workers:
            self._set_row_locked(row, True)
        self._update_apply_btn_state()

    def _update_apply_btn_state(self):
        """Enable Apply Changes only while at least one row is being live-edited."""
        self.apply_btn.setEnabled(bool(self._live_edit_rows))

    @staticmethod
    def _is_live_applicable(old_params, new_params):
        """True if an edit can be pushed to the running loop without a restart.

        A method change always restarts. Same-method DC/AC edits are re-read by
        their loops each iteration, so they apply in place. Alternating polarity
        runs a hardware-timed batch waveform that can't be retuned mid-flight, so
        only a change confined to its stop target applies in place — a waveform
        change restarts the method (carrying the charge passed so far).
        """
        if old_params.get('method') != new_params.get('method'):
            return False
        if new_params.get('method') == 'alternating_polarity':
            wave_keys = ('ap_pos_peak_V', 'ap_neg_peak_V', 'ap_frequency_hz',
                         'ap_duty_cycle', 'ap_waveform', 'ap_sample_hz')
            return all(old_params.get(k) == new_params.get(k) for k in wave_keys)
        return True

    def _apply_live_edits(self, rows=None):
        """Commit live edits from the given rows (or all rows in edit mode).

        For each row still running: builds fresh params, pushes an in-place tweak
        or requests a graceful method restart, then re-locks the row. Rows whose
        experiment already ended are simply taken out of edit mode.
        """
        if rows is None:
            rows = [self._find_row_for_pid(pid) for pid in list(self._live_edit_rows)]
        applied = 0
        switched = 0
        for row in rows:
            if row is None or row < 0:
                continue
            pid = self._row_pid(row)
            if pid is None:
                continue
            worker = self.workers.get(pid)
            if worker is None:
                # No longer running — just drop out of edit mode.
                self._exit_live_edit(row, pid)
                continue
            new_params = self._build_params_for_row(row)
            old_params = dict(worker.live_params)
            if self._is_live_applicable(old_params, new_params):
                worker.apply_live(new_params)
            else:
                worker.request_switch(new_params)
                switched += 1
            self._exit_live_edit(row, pid)
            applied += 1
        if applied:
            note = f" ({switched} restarting)" if switched else ""
            self.status_label.setText(f"Applied live edits to {applied} experiment(s){note}.")
        else:
            self.status_label.setText("No running rows to apply edits to.")

    # ----- Build params dict from a row -----

    def _build_params_for_row(self, row):
        method = self._row_method_key(row)
        mmol = self._cell_float(row, COL_MMOL, 0.0)
        electrons = self._cell_float(row, COL_ELECTRONS, 0.0)
        total_charge = calculate_total_charge(mmol, electrons)

        exp_name = self._cell_text(row, COL_EXP_NAME, "Experiment").strip() or "Experiment"
        # Sanitize for Windows-compatible filenames: \ / : * ? " < > |
        for ch in r'\/:*?"<>|':
            exp_name = exp_name.replace(ch, "_")

        # Optional time-based stop limit (minutes in the UI, seconds in params);
        # blank cell or non-positive entry means "no time limit", in which case
        # the run is governed by the charge target alone.
        time_limit_min = self._cell_float(row, COL_TIME_LIMIT_MIN, 0.0)
        total_time_s = time_limit_min * 60.0 if time_limit_min > 0 else None

        params = {
            "method": method,
            "experiment_name": exp_name,
            "mmol": mmol,
            "electrons_per_mol": electrons,
            "total_charge_C": total_charge,
            "total_time_s": total_time_s,
        }
        if method == "constant_current":
            params["current_mA"] = self._cell_float(row, COL_CC_CURRENT, 0.0)
            params["initial_current_mA"] = 0.1
        elif method == "constant_potential":
            params["potential_V"] = self._cell_float(row, COL_CP_POTENTIAL, 0.0)
        elif method == "alternating_current":
            params["ac_pos_peak_mA"] = self._cell_float(row, COL_AC_POS, 0.0)
            params["ac_neg_peak_mA"] = self._cell_float(row, COL_AC_NEG, 0.0)
            params["ac_frequency_hz"] = self._cell_float(row, COL_AC_FREQ, 0.1)
            params["ac_duty_cycle"] = self._cell_float(row, COL_AC_DUTY, 50.0) / 100.0
            wave_combo = self.ps_table.cellWidget(row, COL_AC_WAVE)
            params["ac_waveform"] = wave_combo.currentText() if wave_combo else "Sine"
        elif method == "alternating_polarity":
            params["ap_pos_peak_V"] = self._cell_float(row, COL_AP_POS, 1.0)
            params["ap_neg_peak_V"] = self._cell_float(row, COL_AP_NEG, -1.0)
            params["ap_frequency_hz"] = self._cell_float(row, COL_AC_FREQ, 1.0)
            params["ap_duty_cycle"] = self._cell_float(row, COL_AC_DUTY, 50.0) / 100.0
            params["ap_sample_hz"] = self._cell_float(row, COL_AP_SAMPLE, 250)
            wave_combo = self.ps_table.cellWidget(row, COL_AC_WAVE)
            params["ap_waveform"] = wave_combo.currentText() if wave_combo else "Square"
        return params

    def trigger_mock_event(self, event_type):
        rows = self.ps_table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        pid = self.ps_table.item(row, 0).data(QtCore.Qt.UserRole)

        ps = self.potentiostats.get(pid)
        if isinstance(ps, MockPotentiostat):
            if event_type == "compliance":
                ps.trigger_param_compliance()
            elif event_type == "short":
                ps.trigger_short_circuit()
            elif event_type == "disconnect":
                ps.trigger_disconnect()
            elif event_type == "timeout":
                ps.trigger_serial_timeout()
            elif event_type == "reset":
                ps.reset_normal()
            self.status_label.setText(f"Triggered {event_type} on {pid}")

    # ----- Checked-row batch helpers -----

    def _checked_rows(self):
        """Return indices of rows whose batch-select checkbox is ticked."""
        rows = []
        for r in range(self.frozen_col.rowCount()):
            item = self.frozen_col.item(r, self.FROZEN_COL_CHECK)
            if item is not None and item.checkState() == QtCore.Qt.Checked:
                rows.append(r)
        return rows

    def _set_all_checks(self, state):
        """Set every row's batch-select checkbox to ``state`` in one pass."""
        self._suppress_frozen_item_changed = True
        try:
            for r in range(self.frozen_col.rowCount()):
                item = self.frozen_col.item(r, self.FROZEN_COL_CHECK)
                if item is not None:
                    item.setCheckState(state)
        finally:
            self._suppress_frozen_item_changed = False
        self._sync_check_all_header()

    def _on_check_all_clicked(self, section):
        """Toggle every checkbox when the checkbox-column header is clicked.

        If every row is already checked, this clears them all; otherwise it
        checks them all — the one-click "select all / none" affordance so a user
        need not tick 16 rows individually before Start Checked.
        """
        if section != self.FROZEN_COL_CHECK:
            return
        rows = self.frozen_col.rowCount()
        if rows == 0:
            return
        all_checked = all(
            (item := self.frozen_col.item(r, self.FROZEN_COL_CHECK)) is not None
            and item.checkState() == QtCore.Qt.Checked
            for r in range(rows)
        )
        self._set_all_checks(QtCore.Qt.Unchecked if all_checked else QtCore.Qt.Checked)

    def _on_frozen_check_changed(self, item):
        """Maintain shift-click ranges and the header glyph as checkboxes toggle.

        Shift-clicking a checkbox sets every row between it and the previously
        toggled checkbox to the same new state (the familiar list-selection
        gesture), so a contiguous block can be (un)checked in two clicks.
        """
        if self._suppress_frozen_item_changed:
            return
        if item.column() != self.FROZEN_COL_CHECK:
            return
        row = item.row()
        new_state = item.checkState()
        mods = QtWidgets.QApplication.keyboardModifiers()
        if (mods & QtCore.Qt.ShiftModifier) and self._check_anchor_row is not None \
                and self._check_anchor_row != row:
            lo, hi = sorted((self._check_anchor_row, row))
            self._suppress_frozen_item_changed = True
            try:
                for r in range(lo, hi + 1):
                    it = self.frozen_col.item(r, self.FROZEN_COL_CHECK)
                    if it is not None:
                        it.setCheckState(new_state)
            finally:
                self._suppress_frozen_item_changed = False
        self._check_anchor_row = row
        self._sync_check_all_header()

    def _sync_check_all_header(self):
        """Update the checkbox-column header glyph to none/some/all checked."""
        header_item = self.frozen_col.horizontalHeaderItem(self.FROZEN_COL_CHECK)
        if header_item is None:
            return
        rows = self.frozen_col.rowCount()
        checked = len(self._checked_rows())
        if rows == 0 or checked == 0:
            glyph = self._CHECK_ALL_NONE
        elif checked == rows:
            glyph = self._CHECK_ALL_ALL
        else:
            glyph = self._CHECK_ALL_SOME
        header_item.setText(glyph)

    def start_checked(self):
        """Start an experiment on every checked row that is not already running.

        Starts are staggered by 200 ms per row so hubs that share a USB/wall-adaptor
        supply across multiple potentiostats don't see all channels draw inrush
        current at the same instant.
        """
        rows = self._checked_rows()
        if not rows:
            self.status_label.setText("No potentiostats checked.")
            return
        to_start = []
        for r in rows:
            pid = self._row_pid(r)
            if pid is None or pid in self.workers or pid in self._pending_starts:
                continue
            to_start.append(r)
            self._pending_starts.add(pid)
        if not to_start:
            self.status_label.setText("Checked potentiostats are already running.")
            return

        # Warn once (for the whole batch) about potential setpoints beyond the
        # hardware's output compliance: those runs would saturate rather than
        # hold the requested value.
        offenders = []
        for r in to_start:
            params = self._build_params_for_row(r)
            pid = self._row_pid(r)
            if params.get("method") == "constant_potential":
                v = params.get("potential_V", 0.0)
                if abs(v) > VOLTAGE_WARN_THRESHOLD_V:
                    offenders.append(f"PS {pid}: {v:+.2f} V (constant potential)")
            elif params.get("method") == "alternating_polarity":
                for v in (params.get("ap_pos_peak_V", 0.0), params.get("ap_neg_peak_V", 0.0)):
                    if abs(v) > VOLTAGE_WARN_THRESHOLD_V:
                        offenders.append(f"PS {pid}: {v:+.2f} V (alternating polarity peak)")
        if offenders:
            reply = QtWidgets.QMessageBox.warning(
                self,
                "Potential beyond hardware limit",
                "These setpoints exceed what the potentiostat can drive "
                f"(about ±{HW_VOLTAGE_COMPLIANCE_V:g} V, less in a resistive cell):\n\n"
                + "\n".join(offenders)
                + "\n\nThe output will saturate below the requested value. Start anyway?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Cancel,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                for r in to_start:
                    self._pending_starts.discard(self._row_pid(r))
                self.status_label.setText("Start cancelled (potential limit).")
                return
        for i, row in enumerate(to_start):
            QtCore.QTimer.singleShot(i * 200, lambda r=row: self._start_row(r))
        self.status_label.setText(
            f"Starting {len(to_start)} experiment(s) (200 ms stagger)..."
        )

    def _start_row(self, row):
        """Build params from the row and hand off to an ElectrolysisWorker."""
        pid = self._row_pid(row)
        # Always release the pending-start reservation, even on early exit.
        if pid is not None:
            self._pending_starts.discard(pid)
        if pid is None or pid in self.workers:
            return

        params = self._build_params_for_row(row)
        ps = self.potentiostats[pid]

        # Lock the row so the user can't edit a running experiment.
        self._set_row_locked(row, True)

        # Setup Plots
        chart = self.plots[pid]
        p = chart['plot']
        p.clear()
        p.addLegend()

        # Both AC and AP produce a periodic waveform and benefit from the
        # seconds-axis rolling-window view; DC methods use the minutes-axis
        # full-run view instead.
        method = params.get('method')
        is_ac = method in ('alternating_current', 'alternating_polarity')
        chart['is_ac'] = is_ac
        p.setLabel('left', 'Current (mA) or Voltage (V)')

        if is_ac:
            p.setLabel('bottom', 'Time (s)')
            freq_key = 'ap_frequency_hz' if method == 'alternating_polarity' else 'ac_frequency_hz'
            freq = params.get(freq_key, 1.0)
            period = 1.0 / freq if freq > 0 else 1.0
            window_size = max(1.0, period)
            chart['window_duration'] = window_size

            chart['duration_label'] = pg.TextItem(text="Duration: 0.0s", color='k', anchor=(0, 0))
            p.addItem(chart['duration_label'])
            p.setTitle(f"PS {pid} (Live - {window_size:.1f}s Window)")
            p.enableAutoRange(axis=pg.ViewBox.YAxis)
            p.disableAutoRange(axis=pg.ViewBox.XAxis)
        else:
            p.setLabel('bottom', 'Time (min)')
            p.setTitle(f"PS {pid}")

        chart['i_curve'] = p.plot(pen={'color': 'r', 'width': 5}, name='Current (mA)')
        chart['v_curve'] = p.plot(pen={'color': 'b', 'width': 5}, name='WE-RE Voltage (V)')

        self.plot_data[pid] = {
            't': deque(maxlen=DISPLAY_BUFFER_SIZE),
            'i': deque(maxlen=DISPLAY_BUFFER_SIZE),
            'v': deque(maxlen=DISPLAY_BUFFER_SIZE),
            'history_t': [], 'history_i': [], 'history_v': [], 'history_counter': 0,
        }
        # Fresh buffer so leftovers from a previous run can't bleed into this one.
        self._data_buffers[pid] = deque(maxlen=DATA_BUFFER_MAXLEN)

        # Reset progress cell + global preview label color.
        progress_item = self.ps_table.item(row, COL_PROGRESS)
        if progress_item is not None:
            progress_item.setText(self._format_progress(row, 0.0, 0.0))
        self.charge_label.setStyleSheet("font-weight: bold; font-size: 20px; color: green;")

        # Start Worker — it claims the board and opens the port itself, off
        # the GUI thread (connecting takes ~1s per board).
        worker = ElectrolysisWorker(
            pid, ps, params, self.signals, self._data_buffers[pid],
            claim_key=self._claim_keys.get(pid),
            port=self._ports.get(pid, ""),
        )
        self.workers[pid] = worker
        # A handed-off worker that never reports in has not run at all; the
        # guardian releases the row instead of leaving it locked and silent.
        self.guardian.arm_start(pid)
        self.threadpool.start(worker)

    def stop_checked(self):
        """Signal every checked, currently-running experiment to stop."""
        rows = self._checked_rows()
        if not rows:
            self.status_label.setText("No potentiostats checked.")
            return
        stopped = 0
        for r in rows:
            pid = self._row_pid(r)
            if pid in self.workers:
                self.workers[pid].stop()
                # Arm the guardian: if the worker doesn't emit finished within
                # the timeout, it is hung on the port and gets force-recovered
                # from a thread that a frozen event loop cannot take down.
                self.guardian.arm_stop(pid)
                stopped += 1
        if stopped == 0:
            self.status_label.setText("Checked potentiostats are not running.")
        else:
            self.status_label.setText(f"Stopping {stopped} experiment(s)...")

    def clear_checked(self):
        """Reset every idle/finished checked row so a new experiment can run.

        Running rows are skipped — Clear Checked never interrupts a live
        experiment. Use Stop Checked first if you want to end then reset.
        """
        rows = self._checked_rows()
        if not rows:
            self.status_label.setText("No potentiostats checked.")
            return
        cleared = 0
        skipped = 0
        for r in rows:
            pid = self._row_pid(r)
            if pid is None:
                continue
            if pid in self.workers:
                skipped += 1
                continue
            self._clear_row(r, pid)
            cleared += 1
        if skipped and cleared:
            self.status_label.setText(
                f"Cleared {cleared}; skipped {skipped} still running."
            )
        elif skipped:
            self.status_label.setText(
                f"Skipped {skipped} still running — Stop them before clearing."
            )
        else:
            self.status_label.setText(f"Cleared {cleared} potentiostat(s).")

    def _clear_row(self, row, pid):
        """Reset a single row's status/progress/plot for a fresh experiment."""
        self._suppress_item_changed = True
        try:
            status_item = self.ps_table.item(row, COL_STATUS)
            if status_item is not None:
                status_item.setText("Idle")
                status_item.setForeground(QtGui.QBrush(QtCore.Qt.black))
                status_item.setToolTip("")
            progress_item = self.ps_table.item(row, COL_PROGRESS)
            if progress_item is not None:
                progress_item.setText("—")
            # Reset the experiment name so the next run starts from the default label.
            self._set_editable_cell(row, COL_EXP_NAME, ROW_DEFAULTS[COL_EXP_NAME])
        finally:
            self._suppress_item_changed = False

        # Reset the live plot for this channel: drop curves and restore DC defaults.
        chart = self.plots.get(pid)
        if chart is not None:
            p = chart['plot']
            p.clear()
            p.addLegend()
            p.setLabel('bottom', 'Time (min)')
            p.setLabel('left', 'Current (mA) or Voltage (V)')
            p.setTitle(f"PS {pid}")
            p.enableAutoRange(axis=pg.ViewBox.XAxis)
            p.enableAutoRange(axis=pg.ViewBox.YAxis)
            chart['i_curve'] = None
            chart['v_curve'] = None
            chart.pop('is_ac', None)
            chart.pop('window_duration', None)
            chart.pop('duration_label', None)
        self._dirty_plots.discard(pid)

        # Drop all buffered samples for this channel.
        self.plot_data[pid] = {
            't': deque(maxlen=DISPLAY_BUFFER_SIZE),
            'i': deque(maxlen=DISPLAY_BUFFER_SIZE),
            'v': deque(maxlen=DISPLAY_BUFFER_SIZE),
            'history_t': [], 'history_i': [], 'history_v': [], 'history_counter': 0,
        }
        self._data_buffers[pid] = deque(maxlen=DATA_BUFFER_MAXLEN)

        # If this is the currently-selected row, refresh the preview panel too.
        sel = self.ps_table.selectionModel().selectedRows()
        if sel and sel[0].row() == row:
            self.charge_label.setStyleSheet(
                "font-weight: bold; font-size: 20px; color: magenta;"
            )
            self._update_preview_for_row(row)

    # ----- Live-plot focus toggle -----

    def _on_plot_scene_clicked(self, event):
        """Zoom a single channel's plot on double-click, restore on the next.

        Double-clicking a plot in the grid expands it to fill the whole plot
        area for a closer look; double-clicking again returns to the full
        multi-channel overview. Live updates keep flowing in both views because
        the plot objects (and their curves) are preserved, only re-laid-out.
        """
        if not event.double():
            return
        if self._focused_pid is not None:
            self._restore_grid()
            return
        scene_pos = event.scenePos()
        for pid, chart in self.plots.items():
            vb = chart['plot'].vb
            if vb is not None and vb.sceneBoundingRect().contains(scene_pos):
                self._focus_plot(pid)
                return

    def _focus_plot(self, pid):
        """Show only the given channel's plot, filling the grid."""
        chart = self.plots.get(pid)
        if chart is None:
            return
        self.plot_win.clear()
        self.plot_win.addItem(chart['plot'], row=0, col=0)
        self._focused_pid = pid
        # A single focused plot fills the viewport rather than scrolling.
        self._update_plot_scroll_height()

    def _restore_grid(self):
        """Re-lay-out every channel plot in its saved grid slot."""
        self.plot_win.clear()
        for chart in self.plots.values():
            self.plot_win.addItem(chart['plot'], row=chart['row'], col=chart['col'])
        self._focused_pid = None
        self._update_plot_scroll_height()

    def _update_plot_scroll_height(self):
        """Size the live-plot grid so 8 plots stay in view and the rest scroll."""
        apply_scroll_height(
            self.plot_scroll, self.plot_win, len(self.plots),
            focused=self._focused_pid is not None,
        )

    def _drain_data_buffers(self):
        """Pull pending data points from the worker buffers (GUI timer).

        Plot buffers must see every point, but the table/label progress text
        only needs the most recent one — significant during AP bursts where a
        single drain can carry hundreds of points.

        Each channel yields at most MAX_DRAIN_PER_CHANNEL points per tick. That
        bound is what keeps the GUI thread returning to the event loop even when
        a channel has fallen behind; anything still buffered waits for the next
        tick rather than extending this one.
        """
        for pid, buf in self._data_buffers.items():
            if not buf:
                continue
            data = None
            for _ in range(MAX_DRAIN_PER_CHANNEL):
                if not buf:
                    break
                data = buf.popleft()
                self._ingest_data_point(pid, data)
            self._update_progress_display(pid, data)

    def _ingest_data_point(self, pid, data):
        # data: (elapsed, current, voltage, charge)
        t, i, v, q = data

        # Store Data
        if pid not in self.plot_data:
            return
        d = self.plot_data[pid]
        chart = self.plots.get(pid)
        if chart is None:
            return
        is_ac = chart.get('is_ac', False)

        if is_ac:
            d['t'].append(t)  # Seconds for AC
        else:
            d['t'].append(t / 60.0)  # Convert s to min for DC

        d['i'].append(i if i is not None else np.nan)
        d['v'].append(v if v is not None else np.nan)

        # Sample into the history buffer every HISTORY_DOWNSAMPLE points for the full-run trend
        if not is_ac:
            d['history_counter'] += 1
            if d['history_counter'] >= HISTORY_DOWNSAMPLE:
                d['history_t'].append(d['t'][-1])
                d['history_i'].append(d['i'][-1])
                d['history_v'].append(d['v'][-1])
                d['history_counter'] = 0

        self._mark_plot_dirty(pid)

    # ----- Live plot repaint (timer-driven, see PLOT_REFRESH_MS) -----

    def _mark_plot_dirty(self, pid):
        """Note that a channel has undrawn samples, without drawing anything."""
        if pid not in self._dirty_plots:
            self._dirty_plots.add(pid)
            self._dirty_order.append(pid)

    def _refresh_dirty_plots(self):
        """Repaint a few channels that have undrawn samples (GUI timer).

        Only PLOTS_PER_REFRESH channels are redrawn per tick, oldest-dirty
        first, so every channel is serviced in turn and no single tick has to
        carry the repaint cost of a whole 16-board grid.
        """
        for _ in range(PLOTS_PER_REFRESH):
            if not self._dirty_order:
                return
            pid = self._dirty_order.popleft()
            if pid not in self._dirty_plots:
                continue  # cleared or restarted since it went dirty
            self._dirty_plots.discard(pid)
            self._redraw_plot(pid)

    @staticmethod
    def _decimate(xs, ys, budget):
        """Thin two parallel series to at most ``budget`` points by even strides."""
        if budget <= 0:
            return [], []
        if len(xs) <= budget:
            return xs, ys
        step = len(xs) // budget + 1
        return xs[::step], ys[::step]

    def _redraw_plot(self, pid):
        """Push a channel's buffered samples into its two curves."""
        d = self.plot_data.get(pid)
        chart = self.plots.get(pid)
        if d is None or chart is None or not d['t']:
            return
        # Deques don't slice, so materialise them as lists once.
        t_all = list(d['t'])
        i_all = list(d['i'])
        v_all = list(d['v'])

        if chart.get('is_ac', False):
            t = t_all[-1]
            if 'duration_label' in chart:
                chart['duration_label'].setText(f"Duration: {t:.1f}s")
                chart['plot'].setTitle(f"PS {pid} (Live) - T={t:.1f}s")

            # Rolling window: times ascend, so its start is a slice point rather
            # than a per-point comparison. Whatever falls inside the window is
            # then thinned to the point budget, which is what keeps a wide
            # window (a low AC frequency) from costing more than a narrow one.
            win_size = chart.get('window_duration', 1.0)
            limit_t = t - win_size
            first = bisect.bisect_right(t_all, limit_t)
            plot_t, plot_i = self._decimate(
                t_all[first:], i_all[first:], MAX_CURVE_POINTS
            )
            _, plot_v = self._decimate(
                t_all[first:], v_all[first:], MAX_CURVE_POINTS
            )
            if chart.get('i_curve'):
                chart['i_curve'].setData(plot_t, plot_i)
            if chart.get('v_curve'):
                chart['v_curve'].setData(plot_t, plot_v)
            chart['plot'].setXRange(limit_t, t, padding=0)
            return

        # DC: the newest samples at full resolution, plus as much of the earlier
        # history as the remaining budget allows. bisect finds the cutoff so the
        # two halves don't overlap. Total point count is therefore flat over a
        # long run instead of growing with it.
        t_buf, i_buf, v_buf = (
            t_all[-RECENT_CURVE_POINTS:],
            i_all[-RECENT_CURVE_POINTS:],
            v_all[-RECENT_CURVE_POINTS:],
        )
        if d['history_t']:
            n_hist = bisect.bisect_left(d['history_t'], t_buf[0])
            budget = MAX_CURVE_POINTS - len(t_buf)
            hist_t, hist_i = self._decimate(
                d['history_t'][:n_hist], d['history_i'][:n_hist], budget
            )
            _, hist_v = self._decimate(
                d['history_t'][:n_hist], d['history_v'][:n_hist], budget
            )
            t_full = hist_t + t_buf
            i_full = hist_i + i_buf
            v_full = hist_v + v_buf
        else:
            t_full, i_full, v_full = t_buf, i_buf, v_buf

        if chart.get('i_curve'):
            chart['i_curve'].setData(t_full, i_full)
        if chart.get('v_curve'):
            chart['v_curve'].setData(t_full, v_full)

    def _update_progress_display(self, pid, data):
        """Update the row's progress cell + preview label from one data point."""
        if data is None:
            return
        t, i, v, q = data
        # Update progress on the row + (if selected) the big preview label.
        if i is None:
            return
        row = self._find_row_for_pid(pid)
        if row < 0:
            return
        progress_text = self._format_progress(row, q, t)
        self._suppress_item_changed = True
        try:
            progress_item = self.ps_table.item(row, COL_PROGRESS)
            if progress_item is not None:
                progress_item.setText(progress_text)
        finally:
            self._suppress_item_changed = False
        sel = self.ps_table.selectionModel().selectedRows()
        if sel and sel[0].row() == row:
            try:
                target_c = float(self.ps_table.item(row, COL_TARGET_C).text())
            except (AttributeError, ValueError):
                target_c = 0.0
            if target_c > 0:
                self.charge_label.setText(f"Target: {target_c:.2f} C   |   {progress_text}")
            else:
                time_limit_s = self._row_time_limit_s(row)
                limit_label = (
                    f"{time_limit_s / 60.0:.1f} min" if time_limit_s else "—"
                )
                self.charge_label.setText(f"Target: {limit_label}   |   {progress_text}")

    def on_status_updated(self, pot_id, status_msg):
        # Any status at all proves the worker reached its own code.
        self.guardian.disarm_start(pot_id)
        found_row = self._find_row_for_pid(pot_id)
        if found_row < 0:
            return
        # Sanitize emojis for better rendering across platforms.
        clean_msg = status_msg.replace("⚠️", "[!]").replace("✓", "[OK]")
        item = self.ps_table.item(found_row, COL_STATUS)
        if item is None:
            return
        self._suppress_item_changed = True
        try:
            item.setText(clean_msg)
            if "compliance" in status_msg.lower() or "error" in status_msg.lower() or "limit" in status_msg.lower():
                item.setForeground(QtGui.QBrush(QtCore.Qt.red))
                item.setToolTip(clean_msg)
            elif "running" in status_msg.lower():
                item.setForeground(QtGui.QBrush(QtCore.Qt.darkGreen))
            else:
                item.setForeground(QtGui.QBrush(QtCore.Qt.black))
        finally:
            self._suppress_item_changed = False

    def on_process_finished(self, pid, success, msg, data_path):
        if pid in self.workers:
            del self.workers[pid]
        self.guardian.disarm_stop(pid)
        self.guardian.disarm_start(pid)

        # Update Table Status. On success the message is meaningful in the
        # status cell ("Completed" / "Stopped"); on failure show "Error" and
        # keep the detail in the main status bar below. "In use" refusals keep
        # their full message so the cell names the app holding the board.
        show_full = success or msg.startswith("In use")
        self.on_status_updated(pid, msg if show_full else "Error")

        # Unlock the row so the user can edit it again.
        row = self._find_row_for_pid(pid)
        if row >= 0:
            self._set_row_locked(row, False)
            # If the run ended while unlocked for a live edit, drop that state
            # and clear the edit tint so the row looks like a normal idle row.
            if pid in self._live_edit_rows:
                self._live_edit_rows.discard(pid)
                self._paint_live_edit_row(row, False)
                self._update_apply_btn_state()

        self.status_label.setText(f"PS {pid} Finished: {msg}")
        logger.info("[PS %s] Finished (success=%s): %s", pid, success, msg)

        # Auto-load the finished run into the Data Preview tab so it can be
        # overlaid and compared with other runs — unless the user disabled the
        # post-run plot in moles-settings.
        #
        # Both the existence check and the read happen on a worker: the data
        # files live in a cloud-synced folder where either call can block for
        # a long time, and a multi-hour run is tens of thousands of rows.
        if success and data_path and get_show_electrolysis_plot_after_run():
            loader = DatasetLoadWorker(data_path)
            loader.signals.loaded.connect(self._on_dataset_loaded)
            loader.signals.failed.connect(self._on_dataset_load_failed)
            self.threadpool.start(loader)

    def _on_dataset_loaded(self, path, payload):
        """Add a background-loaded run to the Data Preview tab."""
        time_s, charge_c, metrics, method, mmol = payload
        try:
            self.viz_tab.add_dataset(
                os.path.splitext(os.path.basename(path))[0],
                time_s, charge_c, metrics, method, mmol,
            )
        except Exception:
            logger.exception("Failed to add run to Data Preview")

    def _on_dataset_load_failed(self, path, error):
        logger.warning("Could not auto-load %s into Data Preview: %s", path, error)

    def _on_worker_reaped(self, pid):
        """Free the UI slot of a worker the guardian could not recover.

        The guardian has already force-closed the port from its own thread;
        this only undoes the UI side of a run that will never report finished,
        so the channel isn't locked until the app restarts.
        """
        self.workers.pop(pid, None)
        row = self._find_row_for_pid(pid)
        if row >= 0:
            self._set_row_locked(row, False)
            if pid in self._live_edit_rows:
                self._live_edit_rows.discard(pid)
                self._paint_live_edit_row(row, False)
                self._update_apply_btn_state()
        self.on_status_updated(pid, "Error: worker hung — reconnect before reuse")
        self.status_label.setText(
            f"PS {pid} worker hung; its port was closed. Reconnect before reusing."
        )

    def _on_worker_never_started(self, pid):
        """Free the row of a run the guardian cancelled before it ever began.

        The guardian has already cancelled the worker, so it stays inert even
        if the pool starts it later; this only undoes the UI side.
        """
        self.workers.pop(pid, None)
        row = self._find_row_for_pid(pid)
        if row >= 0:
            self._set_row_locked(row, False)
        self.on_status_updated(pid, "Error: never started — try again")
        self.status_label.setText(
            f"PS {pid} never started and was cancelled. Check the log, then retry."
        )

    def _on_emergency_stop(self, stopped):
        """Report an emergency stop that the sentinel file triggered."""
        if stopped:
            self.status_label.setText(
                f"EMERGENCY STOP file detected — stopping {stopped} experiment(s)."
            )
        else:
            self.status_label.setText(
                "EMERGENCY STOP file detected — no experiments were running."
            )

    def closeEvent(self, event):
        # Stop all experiment threads, then run hardware cleanup in a background thread
        # so the GUI can close immediately without freezing on serial communication.
        for w in self.workers.values():
            w.stop()
        self.guardian.shutdown()
        threading.Thread(target=emergency_cleanup, daemon=True).start()
        event.accept()


def main():
    # Logging + exception hooks first: nothing in the app should write to the
    # console (a console in QuickEdit/select mode blocks the writing thread),
    # and an unhandled slot exception must be logged instead of aborting the
    # process via PyQt's qFatal (which would also skip emergency_cleanup).
    setup_logging("electrolysis")
    install_qt_message_handler()

    app = QtWidgets.QApplication(sys.argv)
    apply_app_icon(app)
    pg.setConfigOption('background', 'w')
    pg.setConfigOption('foreground', 'k')

    win = MainWindow()
    win.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

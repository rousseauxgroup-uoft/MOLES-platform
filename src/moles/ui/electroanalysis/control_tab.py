"""A multichannel control tab for one voltammetry method (CV or DPV).

Shows a scrollable column of per-potentiostat control panels on the left and a
grid of live plots on the right — one plot per device, mirroring the layout of
the electrolysis interface so several channels can be watched at once.

The same class powers both the CV and the DPV tab; the ``method`` argument
selects which parameter form the panels show and how data is plotted. A CV plots
measured current against measured potential directly. A DPV cannot: its potential
jumps forward and back with every pulse, so plotting current against it just
scribbles. Instead the DPV live plot transforms the accumulated raw stream on the
fly — into the differential curve (delta-i vs step potential, built up one pulse
period at a time), or the raw current against time for acquisition debugging.
"""

from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets

from ...calibration import get_potentiostat_ports
from ...driver.ps4_ref import HW_VOLTAGE_COMPLIANCE_V, VOLTAGE_WARN_THRESHOLD_V
from ...methods.dpv import process_dpv
from ...settings import get_cv_data_folder
from ..scrollable_plots import PlotScrollArea, apply_scroll_height
from .widgets import PotentiostatPanel
from .workers import DPV_SAMPLE_HZ, VoltammetryWorker


def _potentials_beyond_compliance(params):
    """Return the requested potentials this run cannot physically reach.

    Collects the extreme potentials the waveform will ask for (for DPV the
    pulse rides on top of the staircase in the scan direction, so the pulsed
    points sit beyond the plain sweep limits) and returns those beyond the
    warning threshold.
    """
    if params.get('method') == 'DPV':
        pulse = abs(params.get('pulse_V', 0.0))
        if params.get('dpv_type') == 'cyclic':
            lo = params.get('min_V', 0.0)
            hi = params.get('max_V', 0.0)
            # Pulses push past the window: up at the top, down at the bottom.
            # Start/End V can sit outside the vertices, so include them too.
            extremes = [lo, hi, hi + pulse, lo - pulse,
                        params.get('start_V', 0.0), params.get('end_V', 0.0)]
        else:
            start = params.get('start_V', 0.0)
            end = params.get('end_V', 0.0)
            # The pulse rides in the scan direction, so the far end is pushed
            # further out than End V itself.
            direction = 1.0 if end >= start else -1.0
            extremes = [start, end, end + direction * pulse]
    else:
        extremes = [params.get(k, 0.0) for k in ('start_V', 'min_V', 'max_V', 'last_V')]
    return sorted({v for v in extremes if abs(v) > VOLTAGE_WARN_THRESHOLD_V})

# Per-channel trace colours, one per potentiostat slot A–H.
_CHANNEL_COLORS = {
    'A': 'r', 'B': 'g', 'C': 'b', 'D': 'c',
    'E': 'm', 'F': 'y', 'G': 'k', 'H': (255, 165, 0),
}

# Mock devices offered when Test Mode is enabled (no hardware attached).
_MOCK_PORTS = {'A': 'MOCK_A', 'B': 'MOCK_B', 'C': 'MOCK_C'}

# Most points ever handed to the plot library in one repaint. Long runs are
# thinned for *display only* (data is saved at full resolution). Without this
# cap the repaint cost grows with every cycle until the GUI starves the worker
# thread that services the instrument — the root of the long-CV USB failures.
MAX_RENDER_POINTS = 20000


class _PopoutWindow(QtWidgets.QWidget):
    """A standalone top-level window that announces when it is closed.

    Used both for the detached plot grid and for single-channel enlargements,
    so the control tab can move its plots back or drop its reference once the
    user closes the window.
    """

    closed = QtCore.pyqtSignal()

    def __init__(self, title, parent=None):
        # Qt.Window makes this a real top-level window even though it keeps a
        # parent, so it closes automatically when the main window does.
        super().__init__(parent, QtCore.Qt.Window)
        self.setWindowTitle(title)
        self.resize(760, 560)
        self._vbox = QtWidgets.QVBoxLayout(self)
        self._vbox.setContentsMargins(0, 0, 0, 0)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


class ControlTab(QtWidgets.QWidget):
    """Multichannel CV or DPV control surface with a live plot per device."""

    # Emitted with the path of a freshly-saved result so the Analysis tab can
    # pick it up automatically.
    run_finished = QtCore.pyqtSignal(str)

    def __init__(self, method):
        super().__init__()
        self.method = method

        main_layout = QtWidgets.QHBoxLayout(self)

        # --- Left: controls + scrollable list of per-device panels ---
        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)

        self.refresh_btn = QtWidgets.QPushButton("Refresh Ports")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        left_layout.addWidget(self.refresh_btn)

        self.test_mode_cb = QtWidgets.QCheckBox("Test Mode (Mock Hardware)")
        self.test_mode_cb.setToolTip(
            "Run against simulated potentiostats so the interface can be "
            "exercised with no hardware attached."
        )
        self.test_mode_cb.stateChanged.connect(self.refresh_ports)
        left_layout.addWidget(self.test_mode_cb)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        self._panel_container = QtWidgets.QWidget()
        self._panel_layout = QtWidgets.QVBoxLayout(self._panel_container)
        self._panel_layout.addStretch()
        scroll.setWidget(self._panel_container)
        left_layout.addWidget(scroll)

        # --- Right: grid of live plots, one per device ---
        # The grid sits in its own panel with a pop-out button so the whole grid
        # can be floated into a separate window (handy for a second monitor);
        # double-clicking any single plot enlarges just that channel.
        plot_panel = QtWidgets.QWidget()
        self._plot_layout = QtWidgets.QVBoxLayout(plot_panel)
        self._plot_layout.setContentsMargins(0, 0, 0, 0)

        bar = QtWidgets.QHBoxLayout()
        bar.addStretch()
        self.popout_btn = QtWidgets.QPushButton("⤢ Pop Out Plots")
        self.popout_btn.setToolTip(
            "Detach the live plots into their own window — useful for a second "
            "monitor.\nDouble-click any single plot to enlarge just that channel."
        )
        self.popout_btn.clicked.connect(self._toggle_grid_popout)
        bar.addWidget(self.popout_btn)
        self._plot_layout.addLayout(bar)

        self.plot_win = pg.GraphicsLayoutWidget()
        self.plot_win.setBackground('w')
        self.plot_win.scene().sigMouseClicked.connect(self._on_scene_clicked)
        # Scroll wrapper keeps each plot legible with many channels: 8 plots fill
        # the view, the rest scroll (see scrollable_plots).
        self.plot_scroll = PlotScrollArea(self.plot_win)
        self.plot_scroll.resized.connect(self._update_plot_scroll_height)
        self._plot_layout.addWidget(self.plot_scroll)

        # Shown in the tab in place of the grid while it is floating elsewhere.
        self._plot_placeholder = QtWidgets.QLabel(
            "Plots are in a separate window.\n"
            "Close that window (or click “Dock Plots”) to bring them back."
        )
        self._plot_placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._plot_placeholder.setStyleSheet("color: gray;")
        self._plot_placeholder.setVisible(False)
        self._plot_layout.addWidget(self._plot_placeholder)

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(left_panel)
        splitter.addWidget(plot_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([420, 780])
        main_layout.addWidget(splitter)

        self._panels = {}          # pot_id → PotentiostatPanel
        self._plots = {}           # pot_id → {'plot', 'raw', 'smooth'}
        self._ylabels = {}         # pot_id → (label, units) for the y-axis
        self._xlabels = {}         # pot_id → (label, units) for the x-axis
        self._live_data = {}       # pot_id → {'v', 'i', 'v_new', 'i_new'}; see _empty_live_data
        self._run_params = {}      # pot_id → params of its current/last run (DPV needs the timing)
        self._dpv_view = {}        # pot_id → 'differential' | 'time'; which live view a DPV plot shows
        self._active_workers = {}  # pot_id → VoltammetryWorker
        self._popouts = {}         # pot_id → single-channel pop-out state
        self._grid_window = None   # whole-grid pop-out window, if open

        # Live data arrives ~40 chunks/s per device, and a full repaint gets
        # slower as the run grows — repainting per chunk eventually swamps the
        # GUI and starves the acquisition threads. Instead, chunks are only
        # buffered on arrival and this timer repaints at a fixed, safe rate.
        self._dirty = set()        # pot_ids with data not yet on screen
        self._plot_timer = QtCore.QTimer(self)
        self._plot_timer.setInterval(150)  # ms between live repaints
        self._plot_timer.timeout.connect(self._flush_live_plots)

        self.refresh_ports()

    # ------------------------------------------------------------------
    # Port / panel management
    # ------------------------------------------------------------------

    def _current_ports(self):
        """Return the {pot_id: port} mapping for the current mode."""
        if self.test_mode_cb.isChecked():
            return dict(_MOCK_PORTS)
        return get_potentiostat_ports()

    def refresh_ports(self):
        """Re-scan for devices and rebuild both the panel list and plot grid."""
        # Clear panels.
        for i in reversed(range(self._panel_layout.count())):
            w = self._panel_layout.itemAt(i).widget()
            if w:
                w.setParent(None)
        self._panels = {}

        # Close any single-channel pop-outs; their channels may not survive a
        # re-scan (a device could disappear between scans).
        for pid in list(self._popouts):
            self._popouts[pid]['win'].close()

        # Clear plots.
        self.plot_win.clear()
        self._plots = {}
        self._ylabels = {}
        self._xlabels = {}
        self._live_data = {}
        self._run_params = {}
        self._dpv_view = {}
        self._dirty.clear()

        ports = self._current_ports()
        for idx, pid in enumerate(sorted(ports.keys())):
            self._add_panel(pid)
            self._add_plot(pid, idx)
        self._panel_layout.addStretch()
        # Resize the scroll grid so 8 plots stay in view for the new device count.
        self._update_plot_scroll_height()

    def _update_plot_scroll_height(self):
        """Size the live-plot grid so 8 plots stay in view and the rest scroll."""
        apply_scroll_height(self.plot_scroll, self.plot_win, len(self._plots))

    def _add_panel(self, pid):
        panel = PotentiostatPanel(pid, self.method)
        panel.run_requested.connect(self.start_experiment)
        panel.smoothing_changed.connect(self.update_smoothing)
        panel.live_view_changed.connect(self.update_live_view)
        panel.stop_requested.connect(self.stop_experiment)
        self._panel_layout.insertWidget(self._panel_layout.count() - 1, panel)
        self._panels[pid] = panel

    def _add_plot(self, pid, idx):
        r, c = idx // 2, idx % 2
        p = self.plot_win.addPlot(row=r, col=c, title=f"PS {pid}")
        p.setLabel('left', 'Current', units='uA')
        p.setLabel('bottom', 'Potential', units='V')
        p.showGrid(x=True, y=True)
        p.addLegend()

        pen_raw, pen_smooth = self._make_pens(pid)
        raw = p.plot([], [], pen=pen_raw, name="Raw")
        smooth = p.plot([], [], pen=pen_smooth, name="Smoothed")

        self._plots[pid] = {'plot': p, 'raw': raw, 'smooth': smooth}
        self._ylabels[pid] = ('Current', 'uA')
        self._xlabels[pid] = ('Potential', 'V')
        self._dpv_view[pid] = 'differential'
        self._live_data[pid] = self._empty_live_data()

    def _make_pens(self, pid):
        """Return the (raw, smoothed) pens that colour-code one channel."""
        color = _CHANNEL_COLORS.get(pid, 'b')
        pen_raw = pg.mkPen(color=color, width=1, style=QtCore.Qt.DotLine)
        pen_smooth = pg.mkPen(color=color, width=2)
        return pen_raw, pen_smooth

    # ------------------------------------------------------------------
    # Experiment lifecycle
    # ------------------------------------------------------------------

    def start_experiment(self, pot_id, params):
        """Launch a worker for one device, resetting its plot first."""
        # Warn before starting a sweep the hardware cannot deliver: beyond the
        # output compliance the recorded potential flattens while the software
        # keeps scanning, which silently corrupts that segment of the data.
        offenders = _potentials_beyond_compliance(params)
        if offenders:
            listed = ", ".join(f"{v:+.2f} V" for v in offenders)
            reply = QtWidgets.QMessageBox.warning(
                self,
                "Potential beyond hardware limit",
                f"This run asks for {listed}, but the potentiostat can only "
                f"drive about ±{HW_VOLTAGE_COMPLIANCE_V:g} V — and a real cell "
                "saturates earlier, since solution resistance and the counter "
                "electrode consume part of that headroom.\n\n"
                "Beyond the limit the applied potential stops following the "
                "sweep while data keeps being recorded.\n\nStart anyway?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Cancel,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                self._panels[pot_id].update_status("Start cancelled (potential limit).")
                return

        ports = self._current_ports()
        port = ports.get(pot_id)
        if not port:
            self._panels[pot_id].update_status("Error: Port not found")
            return

        self._live_data[pot_id] = self._empty_live_data()
        # Capture the params this run will use: DPV differencing needs the step
        # and pulse hold times to segment the stream while it is still arriving.
        self._run_params[pot_id] = params
        if pot_id in self._plots:
            self._set_curve(pot_id, 'raw', [], [])
            self._set_curve(pot_id, 'smooth', [], [])
            # Point the axes at whatever this run will plot (a prior run may have
            # relabelled them, and DPV depends on the selected live view).
            if self.method == 'DPV':
                self._apply_dpv_labels(pot_id)
            else:
                self._set_left_label(pot_id, 'Current', 'uA')
                self._set_bottom_label(pot_id, 'Potential', 'V')

        worker = VoltammetryWorker(
            pot_id, port, params, use_mock=self.test_mode_cb.isChecked()
        )
        worker.signals.status.connect(self._panels[pot_id].update_status)
        worker.signals.data_chunk.connect(self._on_data_chunk)
        worker.signals.finished.connect(self._on_finished)

        self._active_workers[pot_id] = worker
        QtCore.QThreadPool.globalInstance().start(worker)
        if not self._plot_timer.isActive():
            self._plot_timer.start()

    def stop_experiment(self, pot_id):
        """Forward a stop request to the running worker for this device.

        A second press while the first stop is still pending force-closes the
        serial port — the escape hatch for a device that has stopped
        responding and would otherwise leave the worker blocked forever.
        """
        worker = self._active_workers.get(pot_id)
        if not worker:
            return
        if worker.stop_event.is_set():
            self._panels[pot_id].update_status("Force-disconnecting...")
            worker.force_abort()
        else:
            worker.stop()

    # ------------------------------------------------------------------
    # Live plotting
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_live_data():
        """Fresh per-channel store: cached full arrays plus not-yet-merged
        chunks. Chunks are appended as small numpy arrays (cheap) and folded
        into the cached arrays once per repaint, so no step in the streaming
        path costs more than one pass over the data."""
        return {'v': np.array([]), 'i': np.array([]), 'v_new': [], 'i_new': []}

    @staticmethod
    def _merged_arrays(data):
        """Fold any pending chunks into the cached full arrays and return them."""
        if data.get('v_new'):
            data['v'] = np.concatenate([np.asarray(data['v'], dtype=float)] + data['v_new'])
            data['i'] = np.concatenate([np.asarray(data['i'], dtype=float)] + data['i_new'])
            data['v_new'] = []
            data['i_new'] = []
        return np.asarray(data['v'], dtype=float), np.asarray(data['i'], dtype=float)

    def _on_data_chunk(self, pot_id, v_chunk, i_chunk):
        """Buffer an incoming chunk; ``_flush_live_plots`` repaints on a timer."""
        data = self._live_data.setdefault(pot_id, self._empty_live_data())
        data['v_new'].append(np.asarray(v_chunk, dtype=float))
        data['i_new'].append(np.asarray(i_chunk, dtype=float))
        self._dirty.add(pot_id)

    def _flush_live_plots(self):
        """Repaint every channel that received data since the last timer tick.

        Runs at a fixed cadence, and rendering is decimated past
        ``MAX_RENDER_POINTS``, so the plotting cost per second stays bounded
        no matter how fast data streams in or how long the run has grown. An
        unbounded per-repaint cost is not cosmetic: it starves the worker
        loop that must keep pace with the device's data stream.
        """
        for pid in list(self._dirty):
            self._dirty.discard(pid)
            if pid not in self._plots:
                continue
            data = self._live_data.get(pid)
            if data is None:
                continue
            v, i = self._merged_arrays(data)
            x, y = self._live_xy(pid, v, i)
            window = self._panels[pid].smooth_spin.value()
            self._render_curves(pid, x, y, window)
        # Nothing left to stream and nothing left to paint: idle the timer.
        if not self._active_workers and not self._dirty:
            self._plot_timer.stop()

    def _live_xy(self, pid, v, i):
        """Turn one channel's accumulated raw stream into the (x, y) to plot.

        CV plots measured current against measured potential directly, so the
        raw arrays pass straight through. DPV cannot — its potential jumps with
        every pulse — so the raw stream is transformed into the selected live
        view: the differential curve (delta-i vs step potential, recomputed from
        every complete pulse period so far) or the raw current against time.

        Differencing is cheap: :func:`process_dpv` over a whole run is a couple
        of milliseconds, and a new differential point only appears once every
        pulse period completes, so recomputing it each repaint is negligible.
        """
        if self.method != 'DPV':
            return v, i
        if self._dpv_view.get(pid, 'differential') == 'time':
            t = np.arange(len(i), dtype=float) / DPV_SAMPLE_HZ
            return t, np.asarray(i, dtype=float)
        params = self._run_params.get(pid)
        if params is None or len(i) == 0:
            return np.array([]), np.array([])
        diff = process_dpv(
            v, i,
            potential_hold_ms=params['p_hold'],
            pulse_hold_ms=params['pulse_duration'],
            sample_hz=DPV_SAMPLE_HZ,
        )
        if diff.size == 0:
            return np.array([]), np.array([])
        return diff[:, 0], diff[:, 1]

    def _render_curves(self, pid, v, i, window):
        """Draw the raw and smoothed traces, thinned for display.

        ``v``/``i`` are the display coordinates (already transformed for DPV by
        :meth:`_live_xy`), not necessarily the raw potential/current. Beyond
        ``MAX_RENDER_POINTS`` only every Nth point is drawn (the full data is
        untouched and saved at full resolution). The smoothing window is scaled
        by the same factor so the smoothed trace still averages over the same
        span of the sweep.
        """
        v = np.asarray(v, dtype=float)
        i = np.asarray(i, dtype=float)
        stride = max(1, int(np.ceil(v.size / MAX_RENDER_POINTS)))
        v_r, i_r = v[::stride], i[::stride]
        window_r = max(1, int(round(window / stride)))
        self._set_curve(pid, 'raw', v_r, i_r)
        self._apply_smoothing(pid, v_r, i_r, window_r)

    def update_smoothing(self, pot_id, window):
        """Re-apply smoothing when the user changes the smoothing spinbox."""
        data = self._live_data.get(pot_id)
        if not data or pot_id not in self._plots:
            return
        v, i = self._merged_arrays(data)
        if i.size == 0:
            return
        x, y = self._live_xy(pot_id, v, i)
        self._render_curves(pot_id, x, y, window)

    def update_live_view(self, pot_id, view):
        """Switch a DPV channel between the differential and time-series view."""
        self._dpv_view[pot_id] = view
        if pot_id not in self._plots:
            return
        self._apply_dpv_labels(pot_id)
        data = self._live_data.get(pot_id)
        if data is None:
            return
        v, i = self._merged_arrays(data)
        x, y = self._live_xy(pot_id, v, i)
        window = self._panels[pot_id].smooth_spin.value()
        self._render_curves(pot_id, x, y, window)

    def _apply_dpv_labels(self, pid):
        """Label the axes for the DPV live view this channel is showing."""
        if self._dpv_view.get(pid, 'differential') == 'time':
            self._set_left_label(pid, 'Current', 'uA')
            self._set_bottom_label(pid, 'Time', 's')
        else:
            self._set_left_label(pid, 'Delta-i', 'uA')
            self._set_bottom_label(pid, 'Potential', 'V')

    def _apply_smoothing(self, pot_id, v_data, i_data, window):
        """Update the smoothed trace using a moving average of width ``window``."""
        if window > 1 and len(i_data) > window:
            kernel = np.ones(window) / window
            i_smooth = np.convolve(i_data, kernel, mode='same')
            self._set_curve(pot_id, 'smooth', v_data, i_smooth)
        else:
            self._set_curve(pot_id, 'smooth', v_data, i_data)

    def _set_curve(self, pid, which, x, y):
        """Update one trace ('raw' or 'smooth') on the grid plot and, if the
        channel is also showing in a pop-out window, on that copy too."""
        self._plots[pid][which].setData(x, y)
        po = self._popouts.get(pid)
        if po is not None:
            po[which].setData(x, y)

    def _set_left_label(self, pid, text, units):
        """Set a channel's y-axis label on the grid plot and any pop-out."""
        self._ylabels[pid] = (text, units)
        self._plots[pid]['plot'].setLabel('left', text, units=units)
        po = self._popouts.get(pid)
        if po is not None:
            po['plot'].setLabel('left', text, units=units)

    def _set_bottom_label(self, pid, text, units):
        """Set a channel's x-axis label on the grid plot and any pop-out."""
        self._xlabels[pid] = (text, units)
        self._plots[pid]['plot'].setLabel('bottom', text, units=units)
        po = self._popouts.get(pid)
        if po is not None:
            po['plot'].setLabel('bottom', text, units=units)

    # ------------------------------------------------------------------
    # Pop-out windows
    # ------------------------------------------------------------------

    def _toggle_grid_popout(self):
        """Float the whole plot grid into its own window, or dock it back."""
        if self._grid_window is None:
            self._grid_window = _PopoutWindow(
                f"Live Plots — {self.method}", self
            )
            self._grid_window.closed.connect(self._dock_grid)
            # Adding the grid to the new window reparents it out of the tab.
            self._grid_window._vbox.addWidget(self.plot_scroll)
            self._plot_placeholder.setVisible(True)
            self.popout_btn.setText("⤢ Dock Plots")
            self._grid_window.show()
        else:
            self._grid_window.close()  # closed signal triggers _dock_grid

    def _dock_grid(self):
        """Move the plot grid back into the tab once its window has closed."""
        if self._grid_window is None:
            return
        # Re-insert just after the button bar (index 0) and above the placeholder.
        self._plot_layout.insertWidget(1, self.plot_scroll)
        self._plot_placeholder.setVisible(False)
        self.popout_btn.setText("⤢ Pop Out Plots")
        window, self._grid_window = self._grid_window, None
        window.deleteLater()

    def _on_scene_clicked(self, event):
        """Enlarge a single channel when its plot is double-clicked."""
        if not event.double():
            return
        pos = event.scenePos()
        for pid, entry in self._plots.items():
            if entry['plot'].vb.sceneBoundingRect().contains(pos):
                self._popout_channel(pid)
                break

    def _popout_channel(self, pid):
        """Open (or raise) a larger standalone window mirroring one channel."""
        if pid in self._popouts:
            win = self._popouts[pid]['win']
            win.raise_()
            win.activateWindow()
            return

        win = _PopoutWindow(f"PS {pid} — {self.method}", self)
        plot_w = pg.PlotWidget()
        plot_w.setBackground('w')
        p = plot_w.getPlotItem()
        ytext, yunits = self._ylabels.get(pid, ('Current', 'uA'))
        xtext, xunits = self._xlabels.get(pid, ('Potential', 'V'))
        p.setLabel('left', ytext, units=yunits)
        p.setLabel('bottom', xtext, units=xunits)
        p.showGrid(x=True, y=True)
        p.addLegend()
        pen_raw, pen_smooth = self._make_pens(pid)
        raw = p.plot([], [], pen=pen_raw, name="Raw")
        smooth = p.plot([], [], pen=pen_smooth, name="Smoothed")
        win._vbox.addWidget(plot_w)

        # Register before seeding so the helpers below also fill the new window.
        self._popouts[pid] = {'win': win, 'plot': p, 'raw': raw, 'smooth': smooth}
        win.closed.connect(lambda: self._close_channel_popout(pid))

        data = self._live_data.get(pid, self._empty_live_data())
        v, i = self._merged_arrays(data)
        x, y = self._live_xy(pid, v, i)
        window = self._panels[pid].smooth_spin.value()
        self._render_curves(pid, x, y, window)
        win.show()

    def _close_channel_popout(self, pid):
        """Drop a single-channel pop-out once its window has closed."""
        po = self._popouts.pop(pid, None)
        if po is not None:
            po['win'].deleteLater()

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def _on_finished(self, pot_id, success, msg, result, filepath):
        """Handle worker completion: finalize the plot and announce the save."""
        self._active_workers.pop(pot_id, None)
        self._panels[pot_id].update_status(msg)

        if msg == "Stopped":
            self._offer_partial_save(pot_id)
            return

        if not success or result is None:
            return

        if result['method'] == 'DPV':
            # The live view already built up the differential (or time series)
            # from the raw stream; render once more so the final frame includes
            # every last chunk. _live_data stays raw, so the partial-save path
            # and a later view/smoothing change still have the real stream.
            self._apply_dpv_labels(pot_id)
            v, i = self._merged_arrays(self._live_data[pot_id])
            x, y = self._live_xy(pot_id, v, i)
            window = self._panels[pot_id].smooth_spin.value()
            self._render_curves(pot_id, x, y, window)
        else:
            # CV: sync the plot to the final calibrated trace.
            v, i = np.asarray(result['raw_v'], dtype=float), np.asarray(result['raw_i'], dtype=float)
            self._live_data[pot_id] = {'v': v, 'i': i, 'v_new': [], 'i_new': []}
            window = self._panels[pot_id].smooth_spin.value()
            self._render_curves(pot_id, v, i, window)

        if filepath:
            self.run_finished.emit(filepath)

    def _offer_partial_save(self, pot_id):
        """Offer to save whatever data was streamed before an early stop.

        For DPV this is the raw staircase (a clean differential curve needs the
        full sweep), so the partial file is always saved as plain
        potential/current pairs.
        """
        data = self._live_data.get(pot_id, self._empty_live_data())
        v_data, i_data = self._merged_arrays(data)
        n_pts = len(v_data)

        reply = QtWidgets.QMessageBox.question(
            self, "Save Partial Data?",
            f"The run on PS {pot_id} was stopped.\n"
            f"{n_pts} data point{'s' if n_pts != 1 else ''} were collected.\n\n"
            "Would you like to save the partial data?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes or n_pts == 0:
            return

        data_folder = get_cv_data_folder()
        data_folder.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        name = self._panels[pot_id].get_params().get('name', 'Experiment')
        filename = data_folder / f"{name}_PS-{pot_id}_{timestamp}_PARTIAL.csv"

        np.savetxt(
            filename, np.column_stack([v_data, i_data]), delimiter=",",
            header="Potential (V),Current (uA)", comments='', fmt='%.5f',
        )
        QtWidgets.QMessageBox.information(
            self, "Partial Data Saved",
            f"Saved {n_pts} data points to:\n{filename}",
        )

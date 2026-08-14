"""Per-potentiostat parameter forms and control panel for the CV / DPV tabs.

Each electroanalysis tab shows one ``PotentiostatPanel`` per connected device.
A panel wraps a method-specific parameter form (``CVConfigWidget`` or
``DPVConfigWidget``), a smoothing control, and the Run / Stop buttons, and
reports user actions back to its tab through Qt signals.
"""

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets


class CVConfigWidget(QtWidgets.QWidget):
    """Form for entering cyclic voltammetry parameters."""

    def __init__(self):
        super().__init__()
        layout = QtWidgets.QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText("Scan name")
        layout.addRow("Name:", self.name_edit)

        self.start_v = QtWidgets.QDoubleSpinBox()
        self.start_v.setRange(-5, 5)
        self.start_v.setValue(0.0)
        layout.addRow("Start V:", self.start_v)

        self.min_v = QtWidgets.QDoubleSpinBox()
        self.min_v.setRange(-5, 5)
        self.min_v.setValue(-0.5)
        layout.addRow("Min V:", self.min_v)

        self.max_v = QtWidgets.QDoubleSpinBox()
        self.max_v.setRange(-5, 5)
        self.max_v.setValue(1.0)
        layout.addRow("Max V:", self.max_v)

        self.last_v = QtWidgets.QDoubleSpinBox()
        self.last_v.setRange(-5, 5)
        self.last_v.setValue(0.0)
        layout.addRow("Last V:", self.last_v)

        self.direction_combo = QtWidgets.QComboBox()
        self.direction_combo.addItems(["Negative", "Positive"])
        self.direction_combo.setToolTip(
            "Initial scan direction from Start V.\n"
            "Negative: Start → Min\nPositive: Start → Max"
        )
        layout.addRow("Direction:", self.direction_combo)

        self.cycles = QtWidgets.QSpinBox()
        self.cycles.setRange(1, 100)
        self.cycles.setValue(3)
        layout.addRow("Cycles:", self.cycles)

        self.scan_rate = QtWidgets.QDoubleSpinBox()
        self.scan_rate.setRange(1, 10000)
        self.scan_rate.setValue(100.0)
        self.scan_rate.setSuffix(" mV/s")
        layout.addRow("Scan Rate:", self.scan_rate)

        self.auto_gain_cb = QtWidgets.QCheckBox("Auto-Gain")
        self.auto_gain_cb.setChecked(True)
        self.auto_gain_cb.setToolTip(
            "Enable automatic gain switching.\n"
            "Disable if spikes or oscillations appear in the data."
        )
        layout.addRow("", self.auto_gain_cb)

    def get_params(self):
        """Return the CV experiment parameters as a dict."""
        return {
            'method': 'CV',
            'name': self.name_edit.text() or "Experiment",
            'start_V': self.start_v.value(),
            'min_V': self.min_v.value(),
            'max_V': self.max_v.value(),
            'last_V': self.last_v.value(),
            'scan_direction': self.direction_combo.currentText(),
            'cycles': self.cycles.value(),
            'mV_s': self.scan_rate.value(),
            'auto_gain': self.auto_gain_cb.isChecked(),
        }


class DPVWaveformPreview(pg.PlotWidget):
    """Static preview of the DPV potential waveform.

    Draws a few steps of the applied-potential staircase with its pulses and
    labels the four parameters that define the shape — step height, pulse
    height, step width, and pulse width — so the user can see how the pulse is
    applied before running anything.
    """

    _N_STEPS = 3            # how many staircase steps to draw in the preview
    _DIM_COLOR = '#666666'  # colour of the dimension lines / tick marks

    def __init__(self):
        super().__init__()
        self.setFixedHeight(210)
        self.setBackground('w')
        self.getPlotItem().showGrid(x=True, y=True, alpha=0.3)
        self.getPlotItem().setLabel('left', 'Potential (V)')
        self.getPlotItem().setLabel('bottom', 'Time (ms)')
        # A preview should not be pannable/zoomable like a data plot.
        self.setMouseEnabled(x=False, y=False)
        self.hideButtons()
        self.setMenuEnabled(False)

    def set_waveform(self, start_V, end_V, step_V, pulse_V, step_ms, pulse_ms):
        """Redraw the preview for the given DPV parameters."""
        try:
            self.clear()
            step_V = abs(step_V) or 0.05  # avoid a flat staircase in the preview
            pulse_V = abs(pulse_V)
            step_ms = max(1.0, float(step_ms))
            pulse_ms = max(1.0, float(pulse_ms))
            direction = 1.0 if end_V >= start_V else -1.0

            # Build the staircase-plus-pulse polyline and remember each step's
            # segment boundaries so we can attach dimension labels to one of them.
            t = 0.0
            times, volts, marks = [], [], []

            def segment(level, width):
                nonlocal t
                t0 = t
                times.extend([t0, t0 + width])
                volts.extend([level, level])
                t = t0 + width
                return t0, t

            for k in range(self._N_STEPS):
                base = start_V + direction * step_V * k
                b0, b1 = segment(base, step_ms)
                p0, p1 = segment(base + direction * pulse_V, pulse_ms)
                marks.append({'base': base, 'b0': b0, 'b1': b1, 'p0': p0, 'p1': p1})

            self.plot(times, volts, pen=pg.mkPen('#1f77b4', width=2))

            self._annotate(marks, start_V, step_V, pulse_V, step_ms, pulse_ms,
                           direction, t)
        except Exception as e:
            print(f"DPV preview error: {e}")

    def _annotate(self, marks, start_V, step_V, pulse_V, step_ms, pulse_ms,
                  direction, t_total):
        """Label the four DPV dimensions, keeping every label clear of the trace.

        The two heights are marked with a vertical dimension on the middle step
        and pulse; because the step-height gap is tiny, its label is lifted into
        the empty band above the trace with a leader line. The two width labels
        sit on separate rows in the empty band below the trace. This keeps every
        label off the waveform and away from the other labels.
        """
        top_base = start_V + direction * step_V * (self._N_STEPS - 1)
        levels = [start_V, top_base,
                  start_V + direction * pulse_V, top_base + direction * pulse_V]
        v_min, v_max = min(levels), max(levels)
        v_span = (v_max - v_min) or 1.0
        tick_x = 0.012 * t_total
        tick_y = 0.03 * v_span

        # Annotate the middle step so a previous step is visible for "step height".
        m = marks[1]
        prev_base = marks[0]['base']
        base = m['base']
        pulse_top = base + direction * pulse_V

        # Empty bands above and below the trace to park labels in.
        y_label_top = v_max + 0.34 * v_span
        y_width = v_min - 0.26 * v_span   # one row carries both width dimensions

        # Pulse height: dimension on the middle pulse, label out to its right in
        # the clear gap between the step baseline and the pulse tops.
        self._dim_v(m['p1'], base, pulse_top, f"Pulse height: {pulse_V:g} V",
                    tick_x, side='right')

        # Step height: the gap between two baselines is too small to label in
        # place, so draw the dimension on a flat stretch of the middle step
        # (with the lower level carried across as a dashed guide) and lift the
        # label into the band above the trace, joined by a leader line.
        x_sd = m['b0'] + 0.20 * (m['b1'] - m['b0'])
        self._dashed_h(marks[0]['b1'], x_sd, prev_base)
        self._dim_v(x_sd, prev_base, base, "", tick_x)
        self._leader(x_sd, base, y_label_top)
        self._label(x_sd, y_label_top, f"Step height: {step_V:g} V",
                    anchor=(0.5, 1))

        # Widths: one continuous dimension chain below the trace (step then
        # pulse). The step span is wide, so its label sits centred beneath it;
        # the pulse span is short, so its label is placed out to the right in
        # clear space instead of stacking under the step-width label.
        self._dim_h(m['b0'], m['b1'], y_width, f"Step width: {step_ms:g} ms",
                    tick_y)
        self._dim_h(m['p0'], m['p1'], y_width, "", tick_y)
        self._label(m['p1'] + 1.5 * tick_x, y_width,
                    f"Pulse width: {pulse_ms:g} ms", anchor=(0, 0.5))

        self.setXRange(-0.06 * t_total, 1.24 * t_total)
        self.setYRange(y_width - 0.22 * v_span, y_label_top + 0.22 * v_span)

    # --- Dimension-drawing helpers ---

    def _pen(self):
        return pg.mkPen(self._DIM_COLOR, width=1)

    def _label(self, x, y, text, anchor=(0.5, 0)):
        """Place a dimension label at a point, off the trace."""
        item = pg.TextItem(text, color=self._DIM_COLOR, anchor=anchor)
        item.setPos(x, y)
        self.addItem(item)

    def _leader(self, x, y0, y1):
        """Thin dotted connector from a dimension up to a parked label."""
        self.plot([x, x], [y0, y1],
                  pen=pg.mkPen(self._DIM_COLOR, width=1, style=QtCore.Qt.DotLine))

    def _dim_h(self, x0, x1, y, label, tick):
        """Horizontal dimension line with end ticks and a centred label below."""
        self.plot([x0, x1], [y, y], pen=self._pen())
        self.plot([x0, x0], [y - tick, y + tick], pen=self._pen())
        self.plot([x1, x1], [y - tick, y + tick], pen=self._pen())
        if label:
            self._label((x0 + x1) / 2.0, y - tick, label, anchor=(0.5, 0))

    def _dim_v(self, x, y0, y1, label, tick, side='right'):
        """Vertical dimension line with end ticks and an optional side label."""
        self.plot([x, x], [y0, y1], pen=self._pen())
        self.plot([x - tick, x + tick], [y0, y0], pen=self._pen())
        self.plot([x - tick, x + tick], [y1, y1], pen=self._pen())
        if label:
            if side == 'right':
                self._label(x + tick, (y0 + y1) / 2.0, label, anchor=(0, 0.5))
            else:
                self._label(x - tick, (y0 + y1) / 2.0, label, anchor=(1, 0.5))

    def _dashed_h(self, x0, x1, y):
        """Faint dashed guide line, used to extend a level for comparison."""
        self.plot([x0, x1], [y, y],
                  pen=pg.mkPen(self._DIM_COLOR, width=1, style=QtCore.Qt.DashLine))


class DPVConfigWidget(QtWidgets.QWidget):
    """Form for entering differential pulse voltammetry parameters.

    A DPV run is either a *single sweep* — one staircase between an initial and
    a final potential — or a *cyclic* run shaped like a CV: it begins at Start V,
    cycles between Min V and Max V for the requested number of cycles, then
    finishes at End V. The DPV Type selector swaps between the two sets of
    potential inputs; the pulse/step shape controls below are shared by both.
    """

    def __init__(self):
        super().__init__()
        layout = QtWidgets.QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText("Scan name")
        layout.addRow("Name:", self.name_edit)

        self.type_combo = QtWidgets.QComboBox()
        self.type_combo.addItems(["Single Sweep", "Cyclic"])
        self.type_combo.setToolTip(
            "Single Sweep: one staircase from Initial V to Final V.\n"
            "Cyclic: alternating sweeps between Min V and Max V\n"
            "(segment 1 = Min → Max, segment 2 = Max → Min, ...)."
        )
        layout.addRow("DPV Type:", self.type_combo)

        # --- Single-sweep potentials (shown when DPV Type is "Single Sweep") ---
        self.single_box = QtWidgets.QWidget()
        single_form = QtWidgets.QFormLayout(self.single_box)
        single_form.setContentsMargins(0, 0, 0, 0)

        self.start_v = QtWidgets.QDoubleSpinBox()
        self.start_v.setRange(-5, 5)
        self.start_v.setValue(0.0)
        single_form.addRow("Initial V:", self.start_v)

        self.end_v = QtWidgets.QDoubleSpinBox()
        self.end_v.setRange(-5, 5)
        self.end_v.setValue(1.0)
        single_form.addRow("Final V:", self.end_v)
        layout.addRow(self.single_box)

        # --- Cyclic potentials (shown when DPV Type is "Cyclic") ---
        self.cyclic_box = QtWidgets.QWidget()
        cyclic_form = QtWidgets.QFormLayout(self.cyclic_box)
        cyclic_form.setContentsMargins(0, 0, 0, 0)

        self.cyc_start_v = QtWidgets.QDoubleSpinBox()
        self.cyc_start_v.setRange(-5, 5)
        self.cyc_start_v.setValue(0.0)
        self.cyc_start_v.setToolTip(
            "Potential the scan begins at, before heading to the first vertex."
        )
        cyclic_form.addRow("Start V:", self.cyc_start_v)

        self.min_v = QtWidgets.QDoubleSpinBox()
        self.min_v.setRange(-5, 5)
        self.min_v.setValue(-0.5)
        cyclic_form.addRow("Min V:", self.min_v)

        self.max_v = QtWidgets.QDoubleSpinBox()
        self.max_v.setRange(-5, 5)
        self.max_v.setValue(1.0)
        cyclic_form.addRow("Max V:", self.max_v)

        self.cyc_end_v = QtWidgets.QDoubleSpinBox()
        self.cyc_end_v.setRange(-5, 5)
        self.cyc_end_v.setValue(0.0)
        self.cyc_end_v.setToolTip(
            "Potential the scan finishes at, after the last cycle's vertex."
        )
        cyclic_form.addRow("End V:", self.cyc_end_v)

        self.direction_combo = QtWidgets.QComboBox()
        self.direction_combo.addItems(["Positive", "Negative"])
        self.direction_combo.setToolTip(
            "Direction of the first sweep away from Start V.\n"
            "Positive: sweep up to Max V first.\n"
            "Negative: sweep down to Min V first."
        )
        cyclic_form.addRow("Initial Direction:", self.direction_combo)

        self.cycles = QtWidgets.QSpinBox()
        self.cycles.setRange(1, 100)
        self.cycles.setValue(1)
        self.cycles.setToolTip(
            "Number of full cycles between Min V and Max V.\n"
            "One cycle returns to the first vertex before the run leaves for End V."
        )
        cyclic_form.addRow("Cycles:", self.cycles)
        layout.addRow(self.cyclic_box)

        self.pulse_v = QtWidgets.QDoubleSpinBox()
        self.pulse_v.setRange(0, 1.0)
        self.pulse_v.setSingleStep(0.01)
        self.pulse_v.setValue(0.05)
        layout.addRow("Pulse Height (V):", self.pulse_v)

        self.step_v = QtWidgets.QDoubleSpinBox()
        self.step_v.setRange(0.001, 0.5)
        self.step_v.setSingleStep(0.001)
        self.step_v.setDecimals(3)
        self.step_v.setValue(0.005)
        layout.addRow("Step Height (V):", self.step_v)

        self.pulse_width = QtWidgets.QSpinBox()
        self.pulse_width.setRange(1, 5000)
        self.pulse_width.setValue(50)
        self.pulse_width.setSuffix(" ms")
        layout.addRow("Pulse Width:", self.pulse_width)

        self.step_width = QtWidgets.QSpinBox()
        self.step_width.setRange(1, 5000)
        self.step_width.setValue(200)
        self.step_width.setSuffix(" ms")
        layout.addRow("Step Width:", self.step_width)

        self.auto_gain_cb = QtWidgets.QCheckBox("Auto-Gain")
        self.auto_gain_cb.setChecked(True)
        self.auto_gain_cb.setToolTip(
            "Enable automatic gain switching.\n"
            "Disable if spikes or oscillations appear in the data."
        )
        layout.addRow("", self.auto_gain_cb)

        # Live preview of the pulse waveform, redrawn whenever a value changes.
        self.preview = DPVWaveformPreview()
        layout.addRow(self.preview)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        self.direction_combo.currentIndexChanged.connect(self._update_preview)
        for spin in (self.start_v, self.end_v, self.cyc_start_v, self.cyc_end_v,
                     self.min_v, self.max_v, self.pulse_v, self.step_v,
                     self.pulse_width, self.step_width):
            spin.valueChanged.connect(self._update_preview)
        self._on_type_changed()  # set initial visibility and draw the preview

    def _on_type_changed(self):
        """Show the inputs for the selected DPV type and redraw the preview."""
        cyclic = self.type_combo.currentText() == "Cyclic"
        self.single_box.setVisible(not cyclic)
        self.cyclic_box.setVisible(cyclic)
        self._update_preview()

    def _update_preview(self):
        """Redraw the waveform preview from the current parameter values.

        For a cyclic run the preview shows the first leg — Start V to the first
        vertex (Max for Positive, Min for Negative); for a single sweep it shows
        Initial → Final. Either way the sweep and pulse direction in the preview
        match the first leg the hardware will run.
        """
        if self.type_combo.currentText() == "Cyclic":
            lo = min(self.min_v.value(), self.max_v.value())
            hi = max(self.min_v.value(), self.max_v.value())
            apex = hi if self.direction_combo.currentText() == "Positive" else lo
            v_from, v_to = self.cyc_start_v.value(), apex
        else:
            v_from, v_to = self.start_v.value(), self.end_v.value()
        self.preview.set_waveform(
            v_from, v_to, self.step_v.value(), self.pulse_v.value(),
            self.step_width.value(), self.pulse_width.value(),
        )

    def get_params(self):
        """Return the DPV experiment parameters as a dict.

        ``p_hold`` is the time held at each staircase step and
        ``pulse_duration`` the time held at the pulsed potential; both are
        passed straight through to the waveform builder and the differential
        processing so acquisition and analysis stay aligned. ``dpv_type``
        selects the sweep shape: ``'single'`` carries ``start_V``/``end_V``,
        ``'cyclic'`` carries ``start_V``/``min_V``/``max_V``/``end_V`` plus
        ``cycles`` (like a CV: begin at Start V, cycle between the vertices,
        finish at End V).
        """
        params = {
            'method': 'DPV',
            'name': self.name_edit.text() or "Experiment",
            'pulse_V': self.pulse_v.value(),
            'step_V': self.step_v.value(),
            'pulse_duration': self.pulse_width.value(),
            'p_hold': self.step_width.value(),
            'auto_gain': self.auto_gain_cb.isChecked(),
        }
        if self.type_combo.currentText() == "Cyclic":
            params.update({
                'dpv_type': 'cyclic',
                'start_V': self.cyc_start_v.value(),
                'min_V': self.min_v.value(),
                'max_V': self.max_v.value(),
                'end_V': self.cyc_end_v.value(),
                'direction': self.direction_combo.currentText(),
                'cycles': self.cycles.value(),
            })
        else:
            params.update({
                'dpv_type': 'single',
                'start_V': self.start_v.value(),
                'end_V': self.end_v.value(),
            })
        return params


# Maps a method name to the form widget class used to configure it.
_CONFIG_WIDGETS = {'CV': CVConfigWidget, 'DPV': DPVConfigWidget}


class PotentiostatPanel(QtWidgets.QGroupBox):
    """Control panel for one potentiostat within a CV or DPV tab."""

    run_requested = QtCore.pyqtSignal(str, object)   # pot_id, params dict
    smoothing_changed = QtCore.pyqtSignal(str, int)  # pot_id, window size
    live_view_changed = QtCore.pyqtSignal(str, str)  # pot_id, DPV view key
    stop_requested = QtCore.pyqtSignal(str)          # pot_id

    # DPV live-view options: (menu label, key used by ControlTab).
    _DPV_LIVE_VIEWS = [
        ("Differential (Δi vs V)", 'differential'),
        ("Current vs time", 'time'),
    ]

    def __init__(self, pot_id, method):
        super().__init__(f"Potentiostat {pot_id}")
        self.pot_id = pot_id
        self.method = method
        layout = QtWidgets.QVBoxLayout(self)

        self.status_label = QtWidgets.QLabel("Status: Idle")
        self.status_label.setStyleSheet("color: gray;")
        layout.addWidget(self.status_label)

        self.config = _CONFIG_WIDGETS[method]()
        layout.addWidget(self.config)

        # DPV can't plot current against its own jumping potential, so the live
        # plot shows a transformed view; let the user pick which one.
        self.live_view_combo = None
        if method == 'DPV':
            view_layout = QtWidgets.QHBoxLayout()
            view_layout.addWidget(QtWidgets.QLabel("Live view:"))
            self.live_view_combo = QtWidgets.QComboBox()
            self.live_view_combo.addItems([lbl for lbl, _ in self._DPV_LIVE_VIEWS])
            self.live_view_combo.setToolTip(
                "What the plot shows while a DPV run streams.\n"
                "Differential: the Δi-vs-potential peak, built up as each pulse "
                "period completes — the curve you read peak height and position "
                "from.\n"
                "Current vs time: the raw measured current, for spotting "
                "acquisition problems (railing, gain saturation, dropouts)."
            )
            self.live_view_combo.currentIndexChanged.connect(self._on_live_view_changed)
            view_layout.addWidget(self.live_view_combo)
            layout.addLayout(view_layout)

        smooth_layout = QtWidgets.QHBoxLayout()
        smooth_layout.addWidget(QtWidgets.QLabel("Smoothing:"))
        self.smooth_spin = QtWidgets.QSpinBox()
        self.smooth_spin.setRange(1, 100)
        self.smooth_spin.setValue(1)
        self.smooth_spin.setSuffix(" pts")
        self.smooth_spin.valueChanged.connect(self._on_smooth_changed)
        smooth_layout.addWidget(self.smooth_spin)
        layout.addLayout(smooth_layout)

        self.run_btn = QtWidgets.QPushButton("Run Experiment")
        self.run_btn.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold; padding: 5px;"
        )
        self.run_btn.clicked.connect(self._on_run)
        layout.addWidget(self.run_btn)

        self.stop_btn = QtWidgets.QPushButton("Stop Experiment")
        self.stop_btn.setStyleSheet(
            "background-color: #f44336; color: white; font-weight: bold; padding: 5px;"
        )
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setVisible(False)
        layout.addWidget(self.stop_btn)

    def get_params(self):
        """Return the current parameter dict from the embedded config form."""
        return self.config.get_params()

    def _on_run(self):
        self.run_requested.emit(self.pot_id, self.config.get_params())
        self.run_btn.setEnabled(False)
        self.stop_btn.setVisible(True)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Status: Starting...")
        self.status_label.setStyleSheet("color: blue;")

    def _on_stop(self):
        self.stop_requested.emit(self.pot_id)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("Status: Stopping...")

    def _on_smooth_changed(self, val):
        self.smoothing_changed.emit(self.pot_id, val)

    def _on_live_view_changed(self, idx):
        self.live_view_changed.emit(self.pot_id, self._DPV_LIVE_VIEWS[idx][1])

    def update_status(self, msg):
        """Update the status label and reset buttons once a run reaches a
        terminal state (finished, stopped, or errored)."""
        self.status_label.setText(f"Status: {msg}")
        terminal_states = ("Error", "Done", "Finished", "Stopped", "Completed")
        if any(s in msg for s in terminal_states):
            self.run_btn.setEnabled(True)
            self.stop_btn.setVisible(False)
            self.stop_btn.setEnabled(True)
            if "Error" in msg:
                self.status_label.setStyleSheet("color: red;")
            elif "Stopped" in msg:
                self.status_label.setStyleSheet("color: orange;")
            else:
                self.status_label.setStyleSheet("color: green;")
        else:
            self.status_label.setStyleSheet("color: blue;")

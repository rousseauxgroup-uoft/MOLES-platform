"""Standalone display widgets used by the electrolysis UI.

Currently contains the AC waveform preview widget. New visualization widgets
that don't tie directly into the main window's table logic should land here.
"""

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore


class WaveformPreviewWidget(pg.PlotWidget):
    """Static one-cycle preview of an AC waveform.

    The widget is reused across all potentiostats: whenever the user selects a
    different row in the table, ``set_waveform`` is called with that row's AC
    parameters and the plot redraws.
    """

    def __init__(self):
        super().__init__()
        self.setFixedHeight(220)
        self.setBackground('w')
        self.getPlotItem().showGrid(x=True, y=True, alpha=0.3)
        self.getPlotItem().setLabel('left', 'mA')
        self.getPlotItem().setLabel('bottom', 'Time (1 cycle)')
        self.setMouseEnabled(x=False, y=False)
        self.hideButtons()
        self.setMenuEnabled(False)

    def set_waveform(self, pos_peak, neg_peak, freq, duty_pct, wave_type):
        try:
            duty = max(0.01, min(0.99, duty_pct / 100.0))
            if freq <= 0:
                freq = 0.1
            period = 1.0 / freq
            t_pos = period * duty
            t_neg = period * (1.0 - duty)

            if period < 1.0:
                time_scale, time_unit = 1000.0, "ms"
            else:
                time_scale, time_unit = 1.0, "s"
            self.getPlotItem().setLabel('bottom', f"Time ({time_unit})")

            t = np.linspace(0, period, 200)
            y = np.zeros_like(t)
            offset = (pos_peak + neg_peak) / 2.0
            amp = (pos_peak - neg_peak) / 2.0

            for i, phase_time in enumerate(t):
                if wave_type == "Sine":
                    if phase_time < t_pos:
                        mapped_phase = (phase_time / t_pos) * np.pi
                    else:
                        mapped_phase = np.pi + ((phase_time - t_pos) / t_neg) * np.pi
                    y[i] = offset + amp * np.sin(mapped_phase)
                elif wave_type == "Square":
                    y[i] = pos_peak if phase_time < t_pos else neg_peak
                else:
                    y[i] = offset

            t_plot = t * time_scale
            x_max = period * time_scale

            self.clear()
            # Bold zero reference line.
            self.plot([0, x_max], [0, 0], pen=pg.mkPen('k', width=2))
            # Dashed line at the DC offset of the waveform.
            self.plot([0, x_max], [offset, offset], pen=pg.mkPen('k', style=QtCore.Qt.DashLine, width=1))
            self.plot(t_plot, y, pen=pg.mkPen('b', width=2))

            y_min = min(float(np.min(y)), offset)
            y_max = max(float(np.max(y)), offset)
            margin = (y_max - y_min) * 0.2 or 1.0
            self.setYRange(y_min - margin, y_max + margin)
            self.setXRange(0, x_max)
        except Exception as e:
            print(f"Preview Error: {e}")

    def clear_waveform(self):
        self.clear()

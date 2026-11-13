"""The smoothing control shared by every MOLES trace: a method menu and a window.

The menu offers no smoothing, a rolling average, or Savitzky-Golay (see
``moles.analysis.smoothing``); the window, in points, is greyed out while no
smoothing is chosen. Settings, Data Preview, the live electroanalysis plots,
the electrolysis Visualization tab and the Data Analysis app all use this one
widget, so the choice reads the same everywhere.
"""

from PyQt5 import QtCore, QtWidgets

from ..analysis.smoothing import (
    METHOD_LABELS,
    METHOD_MOVING_AVERAGE,
    METHOD_NONE,
    METHOD_SAVGOL,
    METHODS,
    SAVGOL_ORDER,
)

_METHOD_TOOLTIP = (
    "None: the data as measured.\n"
    "Rolling average: each point becomes the mean of the window around it. "
    "Strong, but it flattens and widens sharp peaks.\n"
    "Savitzky–Golay: a quadratic is fitted to the window around each point. "
    "Removes noise while keeping peak height, width and position."
)


class SmoothingControl(QtWidgets.QWidget):
    """Method menu + window spin box. Emits ``changed(method, window)``."""

    changed = QtCore.pyqtSignal(str, int)

    def __init__(self, method=METHOD_NONE, window=5, max_window=100,
                 keyboard_tracking=True, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.method_combo = QtWidgets.QComboBox()
        for key in METHODS:
            self.method_combo.addItem(METHOD_LABELS[key], key)
        self.method_combo.setToolTip(_METHOD_TOOLTIP)
        layout.addWidget(self.method_combo, stretch=1)

        self.window_spin = QtWidgets.QSpinBox()
        self.window_spin.setRange(1, max_window)
        self.window_spin.setSuffix(" pts")
        self.window_spin.setKeyboardTracking(keyboard_tracking)
        layout.addWidget(self.window_spin)

        self.set_value(method, window)
        self.method_combo.currentIndexChanged.connect(self._on_changed)
        self.window_spin.valueChanged.connect(self._on_changed)

    def method(self):
        return self.method_combo.currentData()

    def window(self):
        return self.window_spin.value()

    def value(self):
        """``(method, window)`` as currently chosen."""
        return self.method(), self.window()

    def set_value(self, method, window):
        """Show ``method`` and ``window`` without emitting ``changed``."""
        index = self.method_combo.findData(method if method in METHODS else METHOD_NONE)
        for w in (self.method_combo, self.window_spin):
            w.blockSignals(True)
        try:
            self.method_combo.setCurrentIndex(index)
            self.window_spin.setValue(int(window))
        finally:
            for w in (self.method_combo, self.window_spin):
                w.blockSignals(False)
        self._sync()

    def _sync(self):
        method = self.method()
        self.window_spin.setEnabled(method != METHOD_NONE)
        if method == METHOD_SAVGOL:
            self.window_spin.setToolTip(
                f"Points in each fitted window (polynomial order {SAVGOL_ORDER}). "
                "Even windows round up to the next odd number; 3 or fewer leave "
                "the data unchanged.")
        elif method == METHOD_MOVING_AVERAGE:
            self.window_spin.setToolTip("Points averaged around each point.")
        else:
            self.window_spin.setToolTip("Choose a smoothing method to set a window.")

    def _on_changed(self, *_):
        self._sync()
        self.changed.emit(*self.value())


__all__ = ["SmoothingControl", "METHOD_NONE", "METHOD_MOVING_AVERAGE", "METHOD_SAVGOL"]

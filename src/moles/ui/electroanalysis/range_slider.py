"""A compact two-handle range slider.

PyQt5 ships only a single-handle ``QSlider``, so this widget provides the
two-handle control the Analysis tab uses to pick which slice of a voltammogram
to display. Values are integer positions (data-point indices); the handles
cannot cross. It emits ``rangeChanged(low, high)`` while either handle is moved.
"""

from PyQt5 import QtCore, QtGui, QtWidgets


class RangeSlider(QtWidgets.QWidget):
    """Horizontal slider with independent low and high handles."""

    rangeChanged = QtCore.pyqtSignal(int, int)

    _HANDLE_W = 10  # px
    _GROOVE_H = 4   # px

    def __init__(self, minimum=0, maximum=100, parent=None):
        super().__init__(parent)
        self._min = minimum
        self._max = max(maximum, minimum + 1)
        self._low = self._min
        self._high = self._max
        self._dragging = None  # 'low', 'high', or None
        self.setMinimumHeight(24)
        self.setMinimumWidth(120)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )

    # --- Public API ---

    def setRange(self, minimum, maximum):
        """Reset the value bounds and snap both handles to the full extent."""
        self._min = minimum
        self._max = max(maximum, minimum + 1)
        self._low = self._min
        self._high = self._max
        self.update()

    def values(self):
        """Return the current ``(low, high)`` handle positions."""
        return self._low, self._high

    def setValues(self, low, high):
        """Set both handle positions, clamped to the bounds and kept ordered."""
        low = max(self._min, min(low, self._max - 1))
        high = max(low + 1, min(high, self._max))
        self._low, self._high = low, high
        self.update()
        self.rangeChanged.emit(self._low, self._high)

    # --- Geometry helpers ---

    def _span_px(self):
        return self.width() - self._HANDLE_W

    def _value_to_px(self, value):
        frac = (value - self._min) / (self._max - self._min)
        return int(frac * self._span_px()) + self._HANDLE_W // 2

    def _px_to_value(self, px):
        frac = (px - self._HANDLE_W // 2) / max(1, self._span_px())
        frac = min(1.0, max(0.0, frac))
        return round(self._min + frac * (self._max - self._min))

    # --- Painting ---

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        mid_y = self.height() // 2

        # Groove.
        groove = QtCore.QRect(
            self._HANDLE_W // 2, mid_y - self._GROOVE_H // 2,
            self._span_px(), self._GROOVE_H,
        )
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor('#cccccc'))
        painter.drawRoundedRect(groove, 2, 2)

        # Selected span.
        low_px = self._value_to_px(self._low)
        high_px = self._value_to_px(self._high)
        span = QtCore.QRect(
            low_px, mid_y - self._GROOVE_H // 2,
            max(0, high_px - low_px), self._GROOVE_H,
        )
        painter.setBrush(QtGui.QColor('#4C8BF5'))
        painter.drawRoundedRect(span, 2, 2)

        # Handles.
        painter.setBrush(QtGui.QColor('#ffffff'))
        painter.setPen(QtGui.QPen(QtGui.QColor('#4C8BF5'), 1.5))
        for px in (low_px, high_px):
            handle = QtCore.QRect(
                px - self._HANDLE_W // 2, mid_y - 8, self._HANDLE_W, 16
            )
            painter.drawRoundedRect(handle, 3, 3)

    # --- Mouse interaction ---

    def mousePressEvent(self, event):
        x = event.pos().x()
        low_px = self._value_to_px(self._low)
        high_px = self._value_to_px(self._high)
        # Grab whichever handle is closest to the click.
        self._dragging = 'low' if abs(x - low_px) <= abs(x - high_px) else 'high'
        self._move_handle(x)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._move_handle(event.pos().x())

    def mouseReleaseEvent(self, event):
        self._dragging = None

    def _move_handle(self, x):
        value = self._px_to_value(x)
        if self._dragging == 'low':
            self._low = min(value, self._high - 1)
            self._low = max(self._low, self._min)
        else:
            self._high = max(value, self._low + 1)
            self._high = min(self._high, self._max)
        self.update()
        self.rangeChanged.emit(self._low, self._high)

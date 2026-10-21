"""Two-handle slider choosing a trace's data range by point index.

Only the handles respond to the mouse: pressing or dragging anywhere else,
the track included, does nothing, so a stray click can never change the data
range. The start handle sits just left of its position and the end handle
just right of its own, so the two never cover each other even when their
values are close, and they cannot cross: the end always stays at least
``min_span`` points after the start. Exact values are typed in the spin boxes
beside the slider; on a long file one pixel spans many points.

This is the Data Analysis app's own slider; the Data Preview tabs keep theirs.
"""

from PyQt5 import QtCore, QtGui, QtWidgets

from ...analysis.model import MIN_RANGE_POINTS

LOW, HIGH = "low", "high"


class IndexRangeSlider(QtWidgets.QWidget):
    """Start and end handles over the point indices 0 to ``maximum``."""

    rangeChanged = QtCore.pyqtSignal(int, int)  # while a handle is dragged

    HANDLE_W = 11
    HANDLE_H = 18
    GROOVE_H = 4
    _HIT_MARGIN = 2  # px of slack around a handle

    def __init__(self, maximum=100, min_span=MIN_RANGE_POINTS - 1, parent=None):
        super().__init__(parent)
        self._min_span = min_span
        self._max = 1
        self._low = self._high = 0
        self._drag = None
        self._grab_dx = 0.0
        self.setMaximum(maximum)
        self.setMouseTracking(True)
        self.setMinimumSize(120, self.HANDLE_H + 8)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.setFocusPolicy(QtCore.Qt.NoFocus)

    # --- Values ---

    def maximum(self):
        return self._max

    def setMaximum(self, maximum):
        """Set the last index and select everything."""
        self._max = max(1, int(maximum))
        self._span = min(self._min_span, self._max)
        self._low, self._high = 0, self._max
        self.update()

    def values(self):
        return self._low, self._high

    def setValues(self, low, high):
        """Set both handles (kept in bounds and apart); does not emit ``rangeChanged``."""
        low = max(0, min(int(low), self._max - self._span))
        high = min(self._max, max(int(high), low + self._span))
        self._low, self._high = low, high
        self.update()

    # --- Geometry ---

    def _track(self):
        """Left and right ends of the track, in pixels."""
        return self.HANDLE_W, max(self.HANDLE_W + 1, self.width() - self.HANDLE_W)

    def _px(self, value):
        left, right = self._track()
        return left + (right - left) * value / self._max

    def _value(self, px):
        left, right = self._track()
        frac = (px - left) / (right - left)
        return round(min(1.0, max(0.0, frac)) * self._max)

    def handle_rect(self, which):
        """Where a handle is drawn: the start handle ends at its position, the end handle begins at its."""
        top = (self.height() - self.HANDLE_H) / 2
        if which == LOW:
            return QtCore.QRectF(self._px(self._low) - self.HANDLE_W, top,
                                 self.HANDLE_W, self.HANDLE_H)
        return QtCore.QRectF(self._px(self._high), top, self.HANDLE_W, self.HANDLE_H)

    def _hit(self, pos):
        m = self._HIT_MARGIN
        for which in (LOW, HIGH):
            if self.handle_rect(which).adjusted(-m, -m, m, m).contains(QtCore.QPointF(pos)):
                return which
        return None

    # --- Painting ---

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        left, right = self._track()
        mid = self.height() / 2
        enabled = self.isEnabled()
        accent = QtGui.QColor("#4C8BF5" if enabled else "#a0a0a0")

        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor("#cccccc"))
        painter.drawRoundedRect(QtCore.QRectF(left, mid - self.GROOVE_H / 2,
                                              right - left, self.GROOVE_H), 2, 2)
        painter.setBrush(accent)
        lo_px, hi_px = self._px(self._low), self._px(self._high)
        painter.drawRoundedRect(QtCore.QRectF(lo_px, mid - self.GROOVE_H / 2,
                                              max(0.0, hi_px - lo_px), self.GROOVE_H), 2, 2)

        painter.setBrush(QtGui.QColor("#ffffff"))
        painter.setPen(QtGui.QPen(accent, 1.5))
        for which in (LOW, HIGH):
            painter.drawRoundedRect(self.handle_rect(which), 3, 3)

    # --- Mouse: handles only ---

    def mousePressEvent(self, event):
        # Accepted either way, so a press on the track is not passed on to
        # the widgets around the slider either.
        event.accept()
        if event.button() != QtCore.Qt.LeftButton or not self.isEnabled():
            return
        self._drag = self._hit(event.pos())
        if self._drag is None:
            return
        anchor = self._px(self._low if self._drag == LOW else self._high)
        self._grab_dx = event.pos().x() - anchor
        self.setCursor(QtCore.Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        event.accept()
        if self._drag is None:
            hovering = self.isEnabled() and self._hit(event.pos()) is not None
            self.setCursor(QtCore.Qt.OpenHandCursor if hovering else QtCore.Qt.ArrowCursor)
            return
        value = self._value(event.pos().x() - self._grab_dx)
        if self._drag == LOW:
            low, high = min(value, self._high - self._span), self._high
        else:
            low, high = self._low, max(value, self._low + self._span)
        low, high = max(0, low), min(self._max, high)
        if (low, high) != (self._low, self._high):
            self._low, self._high = low, high
            self.update()
            self.rangeChanged.emit(low, high)

    def mouseReleaseEvent(self, event):
        event.accept()
        self._drag = None
        self.setCursor(QtCore.Qt.OpenHandCursor if self._hit(event.pos()) else QtCore.Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.ignore()  # let the side panel scroll instead

"""Stop the mouse wheel from changing number boxes and drop-down menus.

By default Qt changes a spin box's value or a combo box's selection whenever
the wheel turns over it, even if the user never clicked it. While scrolling
down a parameter panel this silently edits whatever box passes under the
cursor. The filter below blocks those wheel events and lets them carry on to
the parent widget instead, so the panel scrolls as the user expects. Values
still change by typing, the arrow buttons, or the arrow keys.
"""

from PyQt5 import QtCore, QtWidgets

_GUARDED = (QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox)


class _WheelGuard(QtCore.QObject):
    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Wheel and isinstance(obj, _GUARDED):
            # Returning True skips the box's own wheel handler. Leaving the
            # event un-accepted makes Qt pass it on to the parent widget (and
            # up to the nearest scroll area), exactly as for a widget that
            # ignores the wheel.
            event.ignore()
            return True
        return False


def install_wheel_guard(app: QtWidgets.QApplication) -> None:
    """Apply the guard to every widget in ``app``. Safe to call twice."""
    if getattr(app, "_moles_wheel_guard", None) is None:
        guard = _WheelGuard(app)
        app.installEventFilter(guard)
        app._moles_wheel_guard = guard

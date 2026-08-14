"""Background threads for the onboarding GUI.

``ScanWorker`` runs the synchronous USB enumeration + probe on a thread so
the UI doesn't freeze during the ~1 s wait per port. ``CalibrationWorker``
drives one potentiostat through the six-point calibration sweep, holding
each target current while the main thread shows a modal that asks the user
for the multimeter reading. The two threads communicate via Qt signals;
the user's answer comes back to the worker through a small ``queue.Queue``.
"""

import queue
import time
from typing import List, Optional

from PyQt5 import QtCore

from ...onboarding import calibration_sweep, discovery


# Sentinel placed on the answer queue when the user cancels mid-sweep. A
# distinct object (rather than ``None``) avoids any ambiguity with a real
# measured value of zero.
_CANCEL_SENTINEL = object()

# Upper bound on how long the worker will wait for the user to answer the
# modal before giving up and stopping the current. Ten minutes is generous
# for someone reading a multimeter; if the user has stepped away longer than
# that, abandoning the sweep with the switch open is the safe outcome.
_USER_RESPONSE_TIMEOUT_S = 600.0


class ScanWorker(QtCore.QThread):
    """Run ``discovery.scan()`` on a background thread.

    Emits ``finished_with_result`` when done. Errors are caught and
    reported with an empty device list and the exception text — the UI
    surfaces them as a status message rather than crashing.
    """
    finished_with_result = QtCore.pyqtSignal(list, str)  # detected, error_message

    def run(self):
        try:
            detected = discovery.scan()
            self.finished_with_result.emit(detected, "")
        except Exception as e:
            self.finished_with_result.emit([], str(e))


class CalibrationWorker(QtCore.QThread):
    """Drive one potentiostat through a calibration sweep.

    ``mode`` selects the sweep: ``"current"`` steps through target currents
    (multimeter in current mode), ``"voltage"`` through target potentials
    (multimeter in DC voltage mode across WE and RE). Both produce the same
    two fits — set (applied vs multimeter) and read (board's own reading vs
    multimeter) — just in different units.

    The owning window is responsible for opening a connected ``Potentiostat``
    instance and passing it in; this worker takes care of stepping through
    the targets, prompting via signals, and guaranteeing that the switch is
    opened on every exit path. The potentiostat is disconnected here as part
    of cleanup, so the caller should not reuse it after the worker finishes.
    """

    point_ready = QtCore.pyqtSignal(float)             # applied value — main thread should now prompt
    point_recorded = QtCore.pyqtSignal(float, float)   # applied, measured (mA or V per mode)
    # Both fits from the sweep: keys are mode, slope, intercept, r_squared
    # (set fit) and read_slope, read_intercept, read_r_squared (read fit;
    # None values if the board's own readings could not be collected).
    finished_ok = QtCore.pyqtSignal(dict)
    cancelled = QtCore.pyqtSignal()
    failed = QtCore.pyqtSignal(str)                    # error message

    def __init__(self, ps, mode: str = "current", parent=None):
        super().__init__(parent)
        if mode not in ("current", "voltage"):
            raise ValueError(f"Unknown calibration mode: {mode}")
        self.ps = ps
        self.mode = mode
        self._answer_queue: "queue.Queue" = queue.Queue(maxsize=1)
        self._cancel_requested = False

    def submit_measured_value(self, value_mA: float) -> None:
        """Called by the main thread after the user types a measurement."""
        self._answer_queue.put(float(value_mA))

    def cancel(self) -> None:
        """Called by the main thread when the user cancels the sweep."""
        self._cancel_requested = True
        # Unblock the worker if it's currently waiting on the user's answer.
        try:
            self._answer_queue.put_nowait(_CANCEL_SENTINEL)
        except queue.Full:
            pass

    def run(self):
        # Per-mode hooks: the sweep points, how to apply one, and how to
        # sample the board's own reading for the read fit.
        if self.mode == "voltage":
            points = calibration_sweep.CALIBRATION_POTENTIALS_V
            apply_point = calibration_sweep.apply_and_hold_voltage
            read_device = calibration_sweep.read_device_potential_V
        else:
            points = calibration_sweep.CALIBRATION_CURRENTS_MA
            apply_point = calibration_sweep.apply_and_hold
            read_device = calibration_sweep.read_device_current_mA

        applied: List[float] = []
        measured: List[float] = []
        device_read: List[Optional[float]] = []
        try:
            for target in points:
                if self._cancel_requested:
                    break

                apply_point(self.ps, target)
                # Let the output settle before the user is asked to read the
                # multimeter. Slept in small slices so a cancel mid-settle
                # aborts promptly.
                deadline = time.monotonic() + calibration_sweep.SETTLE_SECONDS
                while time.monotonic() < deadline:
                    if self._cancel_requested:
                        break
                    time.sleep(0.05)
                if self._cancel_requested:
                    calibration_sweep.stop_point(self.ps)
                    break

                # Sample the board's own reading at the settled point — the
                # raw material for the read calibration. A failed sample only
                # drops this point from the read fit, never aborts the sweep.
                try:
                    point_read = read_device(self.ps)
                except Exception:
                    point_read = None

                self.point_ready.emit(target)

                try:
                    answer = self._answer_queue.get(timeout=_USER_RESPONSE_TIMEOUT_S)
                except queue.Empty:
                    calibration_sweep.stop_point(self.ps)
                    self.failed.emit("Timed out waiting for measured value.")
                    return

                # Stop the output before processing the answer, so the
                # switch is open while we do bookkeeping or fit math.
                calibration_sweep.stop_point(self.ps)

                if answer is _CANCEL_SENTINEL or self._cancel_requested:
                    break

                applied.append(target)
                measured.append(float(answer))
                device_read.append(point_read)
                self.point_recorded.emit(target, float(answer))

            if self._cancel_requested or len(applied) < len(points):
                self.cancelled.emit()
                return

            slope, intercept, r_squared = calibration_sweep.fit(applied, measured)
            result = {
                "mode": self.mode,
                "slope": slope,
                "intercept": intercept,
                "r_squared": r_squared,
                "read_slope": None,
                "read_intercept": None,
                "read_r_squared": None,
            }

            # Read fit: board reading -> true (multimeter) value, using only
            # the points where a reading was successfully collected.
            read_pairs = [(r, m) for r, m in zip(device_read, measured) if r is not None]
            if len(read_pairs) >= 2:
                read_x = [p[0] for p in read_pairs]
                read_y = [p[1] for p in read_pairs]
                read_slope, read_intercept, read_r2 = calibration_sweep.fit(read_x, read_y)
                result.update(read_slope=read_slope, read_intercept=read_intercept,
                              read_r_squared=read_r2)

            self.finished_ok.emit(result)

        except Exception as e:
            # The fail-safe: any unexpected error closes the switch before
            # the worker exits. CLAUDE.md mandates that we never leave the
            # potentiostat energised when an exception is raised.
            calibration_sweep.stop_point(self.ps)
            self.failed.emit(str(e))
        finally:
            # Belt-and-braces: even on the success path, ensure the switch
            # is open before we tear down the connection.
            try:
                self.ps.write_switch(0)
            except Exception:
                pass
            try:
                self.ps.disconnect()
            except Exception:
                pass

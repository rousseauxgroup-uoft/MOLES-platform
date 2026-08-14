"""PyQt worker threads that bridge the GUI to the synchronous control loops.

The electrolysis methods in ``moles.methods`` are plain blocking functions
that would freeze the UI if called on the main thread. ``ElectrolysisWorker``
is a ``QRunnable`` that runs one experiment in a background thread and emits
PyQt signals as data arrives, so the UI thread can update plots/labels live.

``ReconnectWorker`` does the same for the slower disconnect/reconnect cycle
that recovers a serial port from a stuck state.
"""

import csv
import logging
import threading
import time

from PyQt5 import QtCore

from ... import claims
from ...data_io import open_data_csv
from ...methods import (
    run_alternating_current,
    run_alternating_polarity,
    run_constant_current,
    run_constant_potential,
)
from ...methods._common import (
    LiveControl,
    OUTCOME_COMM_FAILURE,
    OUTCOME_METHOD_CHANGE,
    OUTCOME_STOPPED,
)
from ...session import (
    ACTIVE_CONFIGURATIONS,
    ACTIVE_POTENTIOSTATS,
    connect_with_retry,
    initialize_potentiostat,
)
from .table import METHOD_LABELS_REVERSE

logger = logging.getLogger(__name__)


class SignalQueue:
    """Adapts a shared deque to look like a ``queue.Queue.put`` method.

    Lets the synchronous experiment loops in ``moles.methods`` push data
    toward the GUI without knowing anything about PyQt — they just call
    ``output_queue.put((elapsed, current, voltage, charge))``. The main
    window drains the deque on a timer in the GUI thread. Compared to the
    old emit-per-sample design this bounds memory if the GUI ever stalls
    (the deque has a maxlen; the Qt event queue does not) and spares the
    event loop hundreds of queued events per second during AP bursts.
    """

    def __init__(self, buffer):
        self.buffer = buffer

    def put(self, item, block=True, timeout=None):
        # item is typically (elapsed, current, voltage, charge)
        self.buffer.append(item)

    def put_nowait(self, item):
        self.put(item)


class _FlushingCsvWriter:
    """csv.writer lookalike that flushes the underlying file periodically.

    Rows used to sit in the ~8 KB stdio buffer for minutes at 1 Hz, so a hard
    crash lost the tail of the run. Flushing at most once per second keeps
    the on-disk file current without a syscall per row during AP bursts.
    """

    def __init__(self, csv_file, flush_interval_s=1.0):
        self._file = csv_file
        self._writer = csv.writer(csv_file)
        self._flush_interval_s = flush_interval_s
        self._last_flush = 0.0

    def writerow(self, row):
        self._writer.writerow(row)
        now = time.monotonic()
        if now - self._last_flush >= self._flush_interval_s:
            self._file.flush()
            self._last_flush = now

    def comment(self, text):
        """Write a '#'-prefixed provenance line and flush immediately.

        Used to mark a mid-run method change in the data file. CSV/numpy readers
        skip '#' lines (``comment='#'``), so this annotates the run without
        disturbing the columns.
        """
        self._file.write(f"# {text}\n")
        self._file.flush()
        self._last_flush = time.monotonic()


class ElectrolysisSignals(QtCore.QObject):
    """Signals emitted by ``ElectrolysisWorker`` for the main window to consume.

    Data points do not travel as signals (see ``SignalQueue``); only the
    low-rate status and completion notifications do.
    """
    status_updated = QtCore.pyqtSignal(object, str)         # pot_id, status text
    finished = QtCore.pyqtSignal(object, bool, str, str)    # pot_id, success, message, data path


class ElectrolysisWorker(QtCore.QRunnable):
    """Run one electrolysis experiment in a thread-pool worker.

    The worker owns the board for the duration of the run: it claims it
    (cross-process, see ``moles.claims``), opens the serial port, runs the
    experiment, then closes the port and releases the claim. Between runs the
    board is free for any other MOLES app.

    ``data_buffer`` is a thread-safe deque owned by the main window; data
    points are appended here and drained by a GUI timer (see ``SignalQueue``).
    """

    RUNNERS = {
        'constant_current': run_constant_current,
        'constant_potential': run_constant_potential,
        'alternating_current': run_alternating_current,
        'alternating_polarity': run_alternating_polarity,
    }

    def __init__(self, pot_id, ps, params, signals, data_buffer, claim_key=None, port=""):
        super().__init__()
        self.pot_id = pot_id
        self.ps = ps
        self.params = params
        # Live params the running method reads each iteration. In-place tweaks
        # mutate this dict; a method change rebinds it (see run / request_switch).
        self.live_params = dict(params)
        self.signals = signals
        self.data_buffer = data_buffer
        # None means "no cross-process claim needed" (mock devices).
        self.claim_key = claim_key
        self.port = port
        self.stop_event = threading.Event()
        # Mediates live edits between the GUI thread and the running method loop.
        self.control = LiveControl()
        self._params_lock = threading.Lock()
        self._next_params = None  # queued params for a pending method change
        self.setAutoDelete(True)

    def stop(self):
        self.stop_event.set()

    def apply_live(self, new_params):
        """Apply an in-place parameter tweak to the running method (no restart).

        The running control loop re-reads its setpoint and stop limits from the
        shared params dict every iteration, so mutating it here retunes the
        experiment without interrupting charge integration. Used when only
        same-method parameters changed (e.g. target current, charge target).
        """
        with self._params_lock:
            self.live_params.update(new_params)

    def request_switch(self, new_params):
        """Stop the running method and hand off to a new method/waveform.

        The accumulated charge and elapsed time carry into the new method so the
        coulomb count and progress stay continuous across the change. Used when
        the method itself changed, or an alternating-polarity waveform was edited
        (things the running loop cannot retune in place).
        """
        with self._params_lock:
            self._next_params = dict(new_params)
        self.control.request_switch()

    def run(self):
        output_queue = SignalQueue(self.data_buffer)

        # Refuse to touch a board another app is using — and tell the user who.
        if self.claim_key is not None:
            try:
                claims.acquire(
                    self.claim_key,
                    app="electrolysis",
                    experiment=self.live_params.get('experiment_name', ''),
                    port=self.port,
                )
            except claims.ClaimBusyError as e:
                # The "In use — ..." prefix keeps this row under the claim
                # poller's control, so it reverts to Idle once the board frees up.
                self.signals.finished.emit(
                    self.pot_id, False, f"In use — {e.claim.describe()}", ""
                )
                return

        ACTIVE_POTENTIOSTATS[self.pot_id] = self.ps
        ACTIVE_CONFIGURATIONS[self.pot_id] = self.live_params

        csv_file = None
        saved_path = ""

        try:
            if self.RUNNERS.get(self.live_params.get('method')) is None:
                # Must still emit finished: the main window only unlocks the
                # row and frees the workers[pid] slot on that signal.
                self.signals.finished.emit(
                    self.pot_id, False,
                    f"Unknown method: {self.live_params.get('method')!r}", "",
                )
                return

            self.signals.status_updated.emit(self.pot_id, "Connecting...")
            connect_with_retry(self.ps, self.pot_id)

            self.signals.status_updated.emit(self.pot_id, "Initializing...")
            initialize_potentiostat(self.ps, self.pot_id)

            # Open the CSV before the experiment starts so each row is written as
            # it streams in. Even if the experiment is stopped early, the file
            # already contains everything received up to that moment.
            csv_file, saved_path = open_data_csv(self.live_params, self.pot_id)
            csv_writer = _FlushingCsvWriter(csv_file)

            # A run can change method mid-flight (see LiveControl / request_switch).
            # Each pass runs one method until it completes, is stopped, aborts, or
            # hands off; on a hand-off the accumulated charge/elapsed carry into
            # the next method so the coulomb count stays continuous, and both
            # methods write to the same CSV.
            initial_charge_C = 0.0
            initial_elapsed_s = 0.0
            outcome = None
            while True:
                with self._params_lock:
                    params = self.live_params
                method = params.get('method')
                runner = self.RUNNERS.get(method)
                if runner is None:
                    self.signals.finished.emit(
                        self.pot_id, False, f"Unknown method: {method!r}", saved_path,
                    )
                    return
                self.control.clear_switch()
                ACTIVE_CONFIGURATIONS[self.pot_id] = params
                self.signals.status_updated.emit(self.pot_id, "Running...")
                outcome = runner(
                    self.ps, params, output_queue, self.pot_id, self.stop_event,
                    signals=self.signals, csv_writer=csv_writer, control=self.control,
                    initial_charge_C=initial_charge_C, initial_elapsed_s=initial_elapsed_s,
                )
                if outcome != OUTCOME_METHOD_CHANGE:
                    break
                # Hand off to the queued method, carrying the running totals.
                initial_charge_C = self.control.carried_charge_C
                initial_elapsed_s = self.control.carried_elapsed_s
                with self._params_lock:
                    if self._next_params is not None:
                        self.live_params = self._next_params
                        self._next_params = None
                    new_method = self.live_params.get('method')
                label = METHOD_LABELS_REVERSE.get(new_method, new_method)
                csv_writer.comment(
                    f"method change -> {new_method} @ {initial_elapsed_s:.1f}s, "
                    f"{initial_charge_C:.4f} C passed"
                )
                self.signals.status_updated.emit(self.pot_id, f"Switching to {label}...")

            if outcome == OUTCOME_COMM_FAILURE:
                # Used to be reported as "Completed" — a lost experiment that
                # looked successful in the table.
                self.signals.finished.emit(
                    self.pot_id, False,
                    "Aborted: persistent serial communication failure", saved_path,
                )
            elif outcome == OUTCOME_STOPPED:
                self.signals.finished.emit(self.pot_id, True, "Stopped", saved_path)
            else:
                self.signals.finished.emit(self.pot_id, True, "Completed", saved_path)

        except Exception as e:
            logger.exception("[PS %s] Experiment failed", self.pot_id)
            # Pass saved_path so the plotter can still open any partial data that was written.
            self.signals.finished.emit(self.pot_id, False, str(e), saved_path)
        finally:
            if csv_file:
                csv_file.close()
            # Close the port so other apps can use the board between runs.
            # The method runners have already driven the output to a safe
            # state by this point (or the port is dead and disconnect fails,
            # in which case nothing else could talk to the board anyway).
            try:
                self.ps.disconnect()
            except Exception:
                pass
            if self.pot_id in ACTIVE_POTENTIOSTATS:
                del ACTIVE_POTENTIOSTATS[self.pot_id]
            if self.pot_id in ACTIVE_CONFIGURATIONS:
                del ACTIVE_CONFIGURATIONS[self.pot_id]
            # Only after the port is closed is the board safe for another app.
            if self.claim_key is not None:
                claims.release(self.claim_key)


class ReconnectSignals(QtCore.QObject):
    """Signals emitted by ``ReconnectWorker``."""
    success = QtCore.pyqtSignal(object)        # pot_id
    failure = QtCore.pyqtSignal(object, str)   # pot_id, error message


class ReconnectWorker(QtCore.QRunnable):
    """Cycle a potentiostat's serial port in a background thread.

    Recovery hatch for a board whose OS port handle is stuck (typically after
    a fault on Windows): close it, wait, open it, verify the handshake, then
    close it again — ports stay closed between runs so the board remains
    available to other MOLES apps. The open involves a hardware
    initialization delay (~1s), so this runs off the main thread.
    """

    def __init__(self, pot_id, ps, calibrations, claim_key=None, port=""):
        super().__init__()
        self.pot_id = pot_id
        self.ps = ps
        self.calibrations = calibrations
        self.claim_key = claim_key
        self.port = port
        self.signals = ReconnectSignals()
        self.setAutoDelete(True)

    def run(self):
        claimed = False
        try:
            # Hold the claim while cycling so another app can't grab the port
            # halfway through — and so we get a readable "who has it" error
            # instead of a bare serial failure if it's already in use.
            if self.claim_key is not None:
                try:
                    claims.acquire(self.claim_key, app="electrolysis",
                                   experiment="port cycle", port=self.port)
                    claimed = True
                except claims.ClaimBusyError as e:
                    self.signals.failure.emit(self.pot_id, str(e))
                    return

            # Close the existing connection. Errors here are ignored — the port
            # may already be in a broken state, and we want to proceed regardless.
            try:
                self.ps.disconnect()
            except Exception:
                pass
            # Give Windows time to fully release the COM port handle before re-opening.
            time.sleep(2.0)
            self.ps.connect()
            # Re-apply calibration coefficients, which live on the Python object and
            # are not stored on the hardware, so they must be set again after reconnect.
            if self.pot_id in self.calibrations:
                cal = self.calibrations[self.pot_id]
                self.ps.set_calibration_params(cal['slope'], cal['intercept'],
                                               cal.get('read_slope'), cal.get('read_intercept'))
                if cal.get('v_slope') is not None:
                    self.ps.set_voltage_calibration_params(
                        cal['v_slope'], cal['v_intercept'],
                        cal.get('v_read_slope'), cal.get('v_read_intercept'))
            # Leave the port closed: the next experiment start opens it itself.
            self.ps.disconnect()
            self.signals.success.emit(self.pot_id)
        except Exception as e:
            self.signals.failure.emit(self.pot_id, str(e))
        finally:
            if claimed:
                claims.release(self.claim_key)

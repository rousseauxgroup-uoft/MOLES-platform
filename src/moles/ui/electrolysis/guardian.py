"""Non-Qt safety net for the electrolysis window.

The 2026-09-17 freeze exposed a single point of failure: every way the app
offered to intervene in a running campaign — the Stop Checked button, the
hung-worker watchdog, and the window's close handler — is a GUI-thread slot.
When the Qt event loop stalled for 41 minutes, all three were unavailable at
once, while the experiments themselves kept driving current perfectly well on
their worker threads. Killing the frozen window from Task Manager is not an
escape either: ``TerminateProcess`` skips the ``atexit`` hook in
``moles.session``, leaving every board's switch closed and the cell energized.

``Guardian`` is a plain daemon thread that imports no Qt, so a stalled event
loop cannot take it down with it. It provides:

  * an emergency stop the operator can trigger with no working UI at all, by
    creating a sentinel file (``~/.moles/STOP_ALL``);
  * a heartbeat watchdog that dumps every thread's stack once the GUI stops
    responding, so the next stall diagnoses itself instead of requiring
    after-the-fact log archaeology;
  * the stop-watchdog escalation that force-closes the serial port of a worker
    hung after a stop request — serial recovery is the part that must not be
    hostage to the event loop it is meant to rescue;
  * cancellation of runs that were handed to the thread pool but never began.
    A queued worker keeps its stale parameters and will start the moment a
    thread frees up, so on 2026-09-17 one fired 42 minutes late and was stopped
    only because another process happened to hold the board's claim. Cancelling
    it is a safety action, which is why it belongs here rather than on a timer
    that a stalled event loop switches off.

Consequences the user must *see* (status cells, row unlocking) are handed back
through callbacks rather than applied here, so the window can marshal them onto
the GUI thread with a queued signal. Nothing in this module touches a widget.
"""

import faulthandler
import logging
import os
import threading
import time
from pathlib import Path

from ...logging_setup import LOG_DIR

logger = logging.getLogger(__name__)

# Creating this file asks every running experiment to stop. It is the one
# control that survives a dead event loop, so it must be something an operator
# can do from a file manager or a shell under pressure.
STOP_FILE = Path.home() / ".moles" / "STOP_ALL"

# How often the guardian wakes to check the sentinel file, the heartbeat, and
# the stop watchdogs.
POLL_INTERVAL_S = 2.0

# Heartbeat age past which the GUI thread is considered stalled. Well above the
# few-hundred-millisecond event-loop latencies seen at full channel count, so
# ordinary load does not trip it.
HEARTBEAT_STALE_S = 10.0
# A stall lasting tens of minutes must not fill the disk with stack dumps.
DUMP_INTERVAL_S = 60.0

# Seconds after a stop request before a non-responding worker is considered
# hung. Sized beyond the worst case of two back-to-back 10 s serial read
# timeouts.
WORKER_STOP_TIMEOUT_S = 25.0
# After force-closing the port, seconds before the worker's UI slot is reaped.
WORKER_REAP_TIMEOUT_S = 10.0
# Seconds a started worker may go without reporting any status before its run is
# treated as never having begun. A worker reports in within about a second of
# getting a thread, so this only catches one that got no thread at all.
WORKER_START_TIMEOUT_S = 30.0


class Guardian(threading.Thread):
    """Watch the electrolysis window from outside Qt and intervene when it dies.

    ``workers`` and ``potentiostats`` are the window's live registries, read
    (never mutated) from this thread. The callbacks report what happened so the
    caller can show it on the GUI thread:

      ``on_status(pot_id, text)``     status text for one channel's row
      ``on_reaped(pot_id)``           a hung worker's UI slot may be freed
      ``on_never_started(pot_id)``    a run was cancelled before it ever began
      ``on_emergency_stop(count)``    the sentinel file stopped ``count`` runs
    """

    def __init__(self, workers, potentiostats, on_status=None, on_reaped=None,
                 on_never_started=None, on_emergency_stop=None,
                 stop_file=None, dump_file=None,
                 poll_interval_s=POLL_INTERVAL_S,
                 heartbeat_stale_s=HEARTBEAT_STALE_S,
                 dump_interval_s=DUMP_INTERVAL_S,
                 stop_timeout_s=WORKER_STOP_TIMEOUT_S,
                 reap_timeout_s=WORKER_REAP_TIMEOUT_S,
                 start_timeout_s=WORKER_START_TIMEOUT_S):
        super().__init__(name="moles-guardian", daemon=True)
        self._workers = workers
        self._potentiostats = potentiostats
        self._on_status = on_status
        self._on_reaped = on_reaped
        self._on_never_started = on_never_started
        self._on_emergency_stop = on_emergency_stop

        # Resolved here rather than as default arguments so tests (and any
        # future per-app override) can redirect both paths.
        self.stop_file = Path(stop_file) if stop_file is not None else STOP_FILE
        self.dump_file = (
            Path(dump_file) if dump_file is not None
            else LOG_DIR / f"electrolysis-stalls-{os.getpid()}.log"
        )

        self._poll_interval_s = poll_interval_s
        self._heartbeat_stale_s = heartbeat_stale_s
        self._dump_interval_s = dump_interval_s
        self._stop_timeout_s = stop_timeout_s
        self._reap_timeout_s = reap_timeout_s
        self._start_timeout_s = start_timeout_s

        self._shutdown = threading.Event()
        self._lock = threading.Lock()
        # pot_id -> {'armed_at', 'deadline', 'forced'}
        self._stop_watchdogs = {}
        # pot_id -> deadline by which the worker must have reported in
        self._start_watchdogs = {}

        self._last_beat = time.monotonic()
        # The heartbeat only means something once the event loop has proven it
        # runs at all; arming on the first beat keeps a slow startup (port
        # enumeration, registry reads) from being reported as a stall.
        self._heartbeat_armed = False
        self._stalled_since = None
        self._last_dump = 0.0
        self._dump_fh = None

    # ----- Called from the GUI thread -----

    def beat(self):
        """Record that the Qt event loop is still turning."""
        self._last_beat = time.monotonic()
        self._heartbeat_armed = True

    def arm_stop(self, pot_id):
        """Start the hang timer for a worker that has been asked to stop."""
        armed_at = time.monotonic()
        with self._lock:
            self._stop_watchdogs[pot_id] = {
                'armed_at': armed_at,
                'deadline': armed_at + self._stop_timeout_s,
                'forced': False,
            }

    def disarm_stop(self, pot_id):
        """Drop a channel's hang timer — its worker exited as asked."""
        with self._lock:
            self._stop_watchdogs.pop(pot_id, None)

    def arm_start(self, pot_id):
        """Start the never-began timer for a worker just handed to the pool."""
        with self._lock:
            self._start_watchdogs[pot_id] = time.monotonic() + self._start_timeout_s

    def disarm_start(self, pot_id):
        """Drop a channel's never-began timer — its worker has reported in."""
        with self._lock:
            self._start_watchdogs.pop(pot_id, None)

    def armed_channels(self):
        """Channels currently being watched for a failure to stop."""
        with self._lock:
            return set(self._stop_watchdogs)

    def starting_channels(self):
        """Channels currently being watched for a failure to begin."""
        with self._lock:
            return set(self._start_watchdogs)

    def shutdown(self):
        """Ask the guardian to exit; safe to call more than once."""
        self._shutdown.set()

    # ----- Guardian thread -----

    def run(self):
        # The operator needs somewhere to put the sentinel file even on a
        # machine where nothing has written to ~/.moles yet.
        try:
            self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create %s for the emergency stop file",
                           self.stop_file.parent, exc_info=True)

        logger.info("Guardian thread started — emergency stop file: %s", self.stop_file)
        while not self._shutdown.wait(self._poll_interval_s):
            try:
                self.poll_once()
            except Exception:
                # A guardian that dies on an unexpected error is worse than no
                # guardian, because the window still reports one as running.
                logger.exception("Guardian poll failed; continuing")
        self._close_dump_file()
        logger.info("Guardian thread stopped")

    def poll_once(self):
        """Run one full check. Also the unit-test entry point."""
        now = time.monotonic()
        self._check_stop_file()
        self._check_heartbeat(now)
        self._check_never_started(now)
        self._check_hung_workers(now)

    # ----- 1. Sentinel-file emergency stop -----

    def _check_stop_file(self):
        try:
            if not self.stop_file.exists():
                return
        except OSError:
            logger.warning("Could not check for %s", self.stop_file, exc_info=True)
            return

        # Clear the trigger before acting: a file that cannot be deleted would
        # otherwise re-fire every poll. Re-firing is harmless anyway (stopping
        # is idempotent), so failing to delete it must not block the stop.
        try:
            self.stop_file.unlink()
        except OSError:
            logger.warning("Emergency stop file %s could not be removed",
                           self.stop_file, exc_info=True)

        stopped = 0
        for pot_id, worker in self._snapshot_workers().items():
            try:
                worker.stop()
            except Exception:
                logger.exception("[PS %s] Emergency stop request failed", pot_id)
                continue
            # A worker hung on a dead port will not honour the request; the
            # same escalation the Stop button relies on force-closes its port.
            self.arm_stop(pot_id)
            self._notify(self._on_status, pot_id, "EMERGENCY STOP requested")
            stopped += 1

        logger.critical(
            "EMERGENCY STOP via %s — stop requested on %d running experiment(s)",
            self.stop_file, stopped,
        )
        self._notify(self._on_emergency_stop, stopped)

    # ----- 2. GUI heartbeat watchdog -----

    def _check_heartbeat(self, now):
        if not self._heartbeat_armed:
            return

        age = now - self._last_beat
        if age <= self._heartbeat_stale_s:
            if self._stalled_since is not None:
                logger.warning(
                    "GUI event loop responsive again after %.1fs stalled",
                    self._last_beat - self._stalled_since,
                )
                self._stalled_since = None
            return

        if self._stalled_since is None:
            self._stalled_since = self._last_beat
        if now - self._last_dump < self._dump_interval_s:
            return
        self._last_dump = now
        self._dump_all_threads(age)

    def _dump_all_threads(self, stalled_for_s):
        """Record where every thread is standing while the GUI is unresponsive.

        The stall is invisible in the log itself — the GUI thread is not
        logging, it is stuck — so the stack of whatever it is stuck in is the
        only direct evidence of the cause.
        """
        logger.critical(
            "GUI event loop unresponsive for %.1fs — dumping thread stacks to %s",
            stalled_for_s, self.dump_file,
        )
        try:
            fh = self._open_dump_file()
            fh.write("\n===== %s  GUI stalled %.1fs  (pid %d) =====\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), stalled_for_s, os.getpid()))
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)
            fh.flush()
        except Exception:
            logger.exception("Could not write thread dump to %s", self.dump_file)

    def _open_dump_file(self):
        if self._dump_fh is None:
            self.dump_file.parent.mkdir(parents=True, exist_ok=True)
            # faulthandler writes through the file descriptor, so this must be
            # a real file rather than a logging handler.
            self._dump_fh = open(self.dump_file, "a", encoding="utf-8")
        return self._dump_fh

    def _close_dump_file(self):
        if self._dump_fh is not None:
            try:
                self._dump_fh.close()
            except Exception:
                pass
            self._dump_fh = None

    # ----- 3. Runs that never began -----

    def _check_never_started(self, now):
        """Cancel workers that were handed to the pool but never reported in.

        Such a run leaves its row locked and showing Idle with no experiment
        behind it, and the worker is still queued: it would start with stale
        parameters whenever a thread eventually frees up. Cancelling is done
        here rather than by the window because that is precisely the case where
        the event loop may be the thing that is stuck.
        """
        with self._lock:
            overdue = [pid for pid, deadline in self._start_watchdogs.items()
                       if now >= deadline]
        for pot_id in overdue:
            worker = self._snapshot_workers().get(pot_id)
            self.disarm_start(pot_id)
            if worker is None:
                continue  # finished on its own between the check and here
            try:
                worker.cancel()
            except Exception:
                logger.exception("[PS %s] Could not cancel a never-started worker", pot_id)
                continue
            logger.error("[PS %s] Worker never started within %.0fs — cancelled",
                         pot_id, self._start_timeout_s)
            self._notify(self._on_never_started, pot_id)

    # ----- 4. Hung-worker escalation -----

    def _check_hung_workers(self, now):
        """Recover channels whose worker didn't exit after a stop request.

        Escalation: first force-close the serial port (which makes the worker's
        blocked read/write raise and unblocks it into its normal error path),
        then — if even that produced no finished signal — report the channel as
        reapable so its row and worker slot aren't locked until app restart.
        The device must be reconnected before reuse either way.

        Only the port close happens here. The registries belong to the GUI
        thread, which frees the slot when it processes ``on_reaped``; if it
        never does, the board is at least no longer being driven.
        """
        with self._lock:
            pot_ids = list(self._stop_watchdogs)

        for pot_id in pot_ids:
            if pot_id not in self._workers:
                self.disarm_stop(pot_id)
                continue
            with self._lock:
                state = self._stop_watchdogs.get(pot_id)
            if state is None or now < state['deadline']:
                continue

            elapsed = now - state['armed_at']
            if not state['forced']:
                logger.error(
                    "[PS %s] Worker unresponsive %.0fs after stop — force-closing port",
                    pot_id, elapsed,
                )
                self._notify(self._on_status, pot_id,
                             "WARNING: worker hung — forcing port closed")
                ps = self._potentiostats.get(pot_id)
                if ps is not None and hasattr(ps, 'force_close'):
                    try:
                        ps.force_close()
                    except Exception:
                        logger.exception("[PS %s] force_close failed", pot_id)
                with self._lock:
                    state = self._stop_watchdogs.get(pot_id)
                    if state is not None:
                        state['forced'] = True
                        state['deadline'] = now + self._reap_timeout_s
            else:
                logger.critical(
                    "[PS %s] Worker still hung %.0fs after stop — freeing its UI slot",
                    pot_id, elapsed,
                )
                self.disarm_stop(pot_id)
                self._notify(self._on_reaped, pot_id)

    # ----- Shared -----

    def _snapshot_workers(self):
        """Copy the worker registry, which the GUI thread mutates as runs end.

        Copying a dict that another thread resizes mid-copy raises, and losing
        that race on the emergency-stop path would leave part of the campaign
        running. Collisions are momentary, so retrying wins immediately.
        """
        for _ in range(10):
            try:
                return dict(self._workers)
            except RuntimeError:
                continue
        logger.error("Could not read the worker registry — it kept changing")
        return {}

    @staticmethod
    def _notify(callback, *args):
        """Invoke a UI callback without letting its failure stop the guardian."""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("Guardian callback %r failed", callback)

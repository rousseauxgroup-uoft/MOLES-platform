"""Per-experiment device session handling and emergency cleanup.

Responsibilities:
  - Initialize a freshly-connected potentiostat into a known safe state.
  - Track which potentiostats currently have an active experiment running, so
    that an interpreter exit (clean or crashing) can drive every output back
    to zero before the program dies.

The ``atexit`` hook is registered when this module is imported. That makes
emergency cleanup automatic for any code path that uses ``moles.session`` —
the user (or a future script author) can never forget to wire it up. The
tradeoff is that ``import moles.session`` has an observable side effect, which
is unusual for Python modules but intentional here for safety reasons.
"""

import atexit
import logging
import time
from typing import Dict

_CONNECT_RETRIES = 1      # number of extra attempts after the first failure
_CONNECT_RETRY_DELAY = 2  # seconds to wait between attempts

from .driver.ps4_ref import DAC

logger = logging.getLogger(__name__)

# Registries shared across the application. Each running experiment writes its
# potentiostat object and parameters here on start, and removes them on stop.
# ``emergency_cleanup`` walks both registries to bring every device down safely.
ACTIVE_POTENTIOSTATS: Dict = {}
ACTIVE_CONFIGURATIONS: Dict = {}


def _close_if_open(ps):
    """Close a potentiostat's port if a previous session left it open.

    ``connect()`` replaces the serial object outright; with exclusive-mode
    opens, an old handle left open would hold the OS-level port lock and make
    every reconnect of our own board fail. Safe on never-connected objects.
    """
    try:
        ser = getattr(ps, "serial", None)
        if ser is not None and getattr(ser, "is_open", False):
            ps.disconnect()
    except Exception:
        logger.warning("[PS] Could not close leftover port handle", exc_info=True)


def connect_with_retry(ps, pot_id):
    """Connect to a potentiostat, retrying once on failure.

    On the first connection failure (typically a transient OS serial port
    issue), waits ``_CONNECT_RETRY_DELAY`` seconds and tries again. If the
    retry also fails, raises the exception so the caller can skip or report
    the device.
    """
    _close_if_open(ps)
    try:
        ps.connect()
        return
    except Exception as first_error:
        logger.warning(
            "[PS %s] Connection failed: %s. Retrying in %ss...",
            pot_id, first_error, _CONNECT_RETRY_DELAY,
        )

    time.sleep(_CONNECT_RETRY_DELAY)

    _close_if_open(ps)
    try:
        ps.connect()
    except Exception as retry_error:
        raise ConnectionError(
            f"Potentiostat {pot_id} unreachable after retry: {retry_error}"
        ) from retry_error


def initialize_potentiostat(ps, pot_id):
    """Bring a freshly-connected potentiostat into a safe, known starting state.

    Sets a default gain, opens the output switch, zeros the DAC channels, and
    measures the open-circuit potential as a sanity check. If the first attempt
    fails with ``PermissionError`` (typical of a stale Windows COM port handle),
    the method automatically disconnects, waits, reconnects, and retries once.
    """
    logger.info("Initializing Potentiostat %s...", pot_id)
    try:
        ps.write_gain(0)
        ps.write_switch(0)
        ps.write_dac(channels=[DAC.CE_IN, DAC.A_REF, DAC.V_AN], voltages=[0, -5, 0])
        ocp = ps.read_ocp()
        logger.info("[PS %s] Open circuit potential: %.3f V", pot_id, ocp)
        return ocp
    except PermissionError as e:
        logger.warning("[PS %s] Initialization error: %s. Attempting auto-recovery (disconnect/reconnect)...",
                       pot_id, e)
        try:
            ps.disconnect()
            time.sleep(5.0)  # Give Windows time to release the COM port handle
            ps.connect()

            # Calibration parameters live on the Python object so they survive
            # the reconnect; only hardware-side state needs to be re-applied.
            logger.info("[PS %s] Reconnection successful. Retrying commands...", pot_id)
            ps.write_gain(0)
            ps.write_switch(0)
            ps.write_dac(channels=[DAC.CE_IN, DAC.A_REF, DAC.V_AN], voltages=[0, -5, 0])
            ocp = ps.read_ocp()
            logger.info("[PS %s] Open circuit potential: %.3f V (recovered)", pot_id, ocp)
            return ocp
        except Exception as recovery_e:
            logger.error("[PS %s] Recovery failed: %s", pot_id, recovery_e)
            raise recovery_e


def emergency_cleanup():
    """Walk every active potentiostat and drive its output to a safe zero state.

    Called automatically by ``atexit`` on interpreter shutdown, and also
    invoked manually by the UI when the main window closes. Tolerates errors
    from individual devices so that one stuck potentiostat cannot block the
    cleanup of the others.
    """
    logger.critical("EMERGENCY CLEANUP: Closing all potentiostat switches...")
    # Snapshot the registry: workers may still be deregistering themselves
    # concurrently, and a dict mutated mid-iteration would abort the cleanup
    # of every remaining device.
    for pot_id, ps in list(ACTIVE_POTENTIOSTATS.items()):
        try:
            ps.write_switch(0)
            params = ACTIVE_CONFIGURATIONS.get(pot_id)
            if params is not None:
                # Both CC and AC are current-controlled — clear the current hold.
                # CP is potential-controlled — drive the output potential to 0.
                if params.get('method') in ('constant_current', 'alternating_current'):
                    try:
                        ps.write_current_hold(0)
                        ps.write_current_hold_stop()
                    except Exception:
                        pass
                else:
                    try:
                        ps.write_potential_calibrated(0)
                    except Exception:
                        pass
        except Exception as e:
            logger.critical("[PS %s] Error during cleanup: %s — cell may still be energized!",
                            pot_id, e)


# Register the cleanup hook on import. See module docstring for the reasoning.
atexit.register(emergency_cleanup)

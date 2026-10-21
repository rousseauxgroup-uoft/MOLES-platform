"""Which firmware each board runs, and where that is remembered.

The firmware has no command that reports its version number, but every
release since 1.1 answers the diagnostics query, and each release changed the
answer: 1.1 sends four counters, 1.2 five, and 1.3 onward ten, the last of
which is a set of capability flags that later releases each added one to.
That is enough to name the release without changing the firmware.

The version a board reported is noted per board under ``~/.moles/firmware/``
whenever MOLES connects to it, so the launcher can show every board's version
without opening a serial port itself.
"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

FIRMWARE_DIR = Path.home() / ".moles" / "firmware"

# The newest release this copy of MOLES knows: the image in firmware/prebuilt/.
LATEST_VERSION = "1.8"

# The diagnostics report's counters in the order the board sends them; older
# firmware sends a prefix. Mirrors DIAGNOSTIC_FIELDS in moles.driver.ps4_ref,
# repeated here so the launcher does not have to load the driver.
DIAGNOSTIC_FIELDS = ('reset_flags', 'tx_dropped', 'rx_overflow', 'starved_steps',
                     'rejected_setpoints', 'late_steps', 'max_late_ms',
                     'resyncs', 'starve_aborts', 'features')

# The release that introduced each capability flag (FW_FEATURE_* in the
# driver), newest first. 1.6 changed only how auto-ranging measures and
# reports the same flags as 1.5; 1.5 only ever ran on the test board during
# development, so those flags are shown as 1.6.
_VERSION_BY_FLAG = (
    (1 << 6, "1.8"),   # on-board iR compensation
    (1 << 5, "1.7"),   # DAC-step R_u capture
    (1 << 4, "1.6"),   # batch auto-ranging
    (1 << 3, "1.4"),   # current ranges match the TMUX6212 switch
)
_KNOWN_FLAGS = (1 << 7) - 1


def version_from_diagnostics(report) -> Optional[str]:
    """The firmware release that sent a diagnostics report.

    ``report`` maps DIAGNOSTIC_FIELDS names to values for as many counters as
    the board sent, or is None when the board did not answer the query at all
    (1.0 ignores it). Returns None for flags no release sends.
    """
    if report is None:
        return "1.0"
    if "features" not in report:
        return "1.2" if "rejected_setpoints" in report else "1.1"
    flags = int(report["features"])
    # Every release has kept the flags before it, so real flags are always
    # the lowest few bits, all set. Anything else is not a report, such as
    # data from a board still streaming a run whose app was cut off.
    if flags < 0x07 or flags & (flags + 1):
        return None
    if flags & ~_KNOWN_FLAGS:
        return f"newer than {LATEST_VERSION}"
    for flag, version in _VERSION_BY_FLAG:
        if flags & flag:
            return version
    return "1.3"


def _version_key(version: str):
    try:
        return tuple(int(part) for part in version.split("."))
    except (AttributeError, ValueError):
        return None


def is_outdated(version: str) -> bool:
    """True when a board's firmware is older than LATEST_VERSION."""
    key = _version_key(version)
    return key is not None and key < _version_key(LATEST_VERSION)


def _note_path(usb_serial: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", usb_serial.strip()) or "unknown"
    return FIRMWARE_DIR / f"{safe}.json"


def record(usb_serial: str, version: str) -> None:
    """Remember the version a board reported.

    Bookkeeping only: a failure is logged, never raised, so it can never stop
    a connection or a run.
    """
    if not usb_serial:
        return
    payload = json.dumps({
        "usb_serial": usb_serial,
        "version": version,
        "read_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2)
    tmp = None
    try:
        FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
        # Written whole and then renamed over the old note, so another app
        # reading at the same moment never sees half a file
        fd, tmp = tempfile.mkstemp(dir=FIRMWARE_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, _note_path(usb_serial))
        tmp = None
    except OSError:
        logger.warning("Could not note firmware %s for board %s", version,
                       usb_serial, exc_info=True)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def read(usb_serial: str) -> Optional[dict]:
    """The last version noted for a board, as ``{"version", "read_at"}``, or
    None when none has been noted (or the note is unreadable)."""
    if not usb_serial:
        return None
    try:
        data = json.loads(_note_path(usb_serial).read_text(encoding="utf-8"))
        return {"version": str(data["version"]),
                "read_at": str(data.get("read_at", ""))}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        logger.debug("Unreadable firmware note for board %s", usb_serial)
        return None


def usb_serial_for_port(port: str) -> str:
    """The USB serial number of the device on a port, from the OS's device
    list (no port is opened), or "" when it cannot be found."""
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.device == port:
                return (getattr(p, "serial_number", None) or "").strip()
    except Exception:
        logger.warning("Could not look up the USB serial of %s", port,
                       exc_info=True)
    return ""

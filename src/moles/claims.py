"""Cross-process potentiostat claims.

Multiple MOLES apps (electrolysis, electroanalysis, ...) can now run at the
same time on one machine. Before any app opens a board's serial port, it must
acquire a *claim*: a small JSON file at ``~/.moles/claims/<key>.json`` that
records which process is using the board, for what, and since when. Apps that
find an existing claim refuse to touch the board and can show the user a
useful message ("Board D is running electrolysis 'X' since 09:14") instead of
a cryptic serial error.

Claims are advisory — the hard guarantee against two processes talking to one
board at once is the ``exclusive=True`` serial open in the driver. Claims are
the friendly layer on top.

Two properties make this safe without a coordinating server process:

* Acquisition is atomic. The claim file is created with ``O_CREAT | O_EXCL``
  ("create only if it does not already exist"), which the OS guarantees can
  succeed for at most one process, so two apps can never both believe they
  won the same board.

* Crashes cannot wedge a board forever. Every claim records its owner's
  process ID; a claim whose process is no longer alive is *stale* and is
  silently replaced on the next acquire. (If the app exits normally, an
  ``atexit`` hook releases its claims immediately.)

Claims are keyed by the board's USB serial number (stable across replugs and
OS port renames) when the board is in the ``~/.moles/potentiostats.csv``
registry, falling back to the port path on machines that haven't been
onboarded.
"""

import atexit
import getpass
import json
import logging
import os
import re
import socket
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .onboarding.registry import USER_REGISTRY_PATH, load_registry

logger = logging.getLogger(__name__)

CLAIMS_DIR = Path.home() / ".moles" / "claims"

# Keys of claims acquired by this process, so the atexit hook knows what to
# release without scanning the directory.
_owned_keys = set()


@dataclass
class Claim:
    """The contents of one claim file — who holds a board and why."""
    key: str            # claim key (USB serial or sanitized port path)
    pid: int            # process ID of the owning app
    app: str            # e.g. "electrolysis", "electroanalysis"
    user: str = ""      # OS login name, for multi-user lab machines
    host: str = ""      # machine name (claims are per-machine; informational)
    experiment: str = ""
    port: str = ""      # serial port at claim time, informational
    started: str = ""   # ISO timestamp

    def describe(self) -> str:
        """One human-readable line for status cells and error messages."""
        when = self.started.replace("T", " ")[:16]  # "YYYY-MM-DD HH:MM"
        exp = f" (exp: {self.experiment})" if self.experiment else ""
        who = f" by {self.user}" if self.user else ""
        return f"{self.app}{exp}{who} since {when}"


class ClaimBusyError(RuntimeError):
    """Raised when a board is already claimed by another live process."""

    def __init__(self, claim: Claim):
        self.claim = claim
        super().__init__(f"Board in use: {claim.describe()}")


def _sanitize(text: str) -> str:
    """Make an arbitrary identifier (e.g. "/dev/tty.usbmodem...") filename-safe."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", text.strip()) or "unknown"


def _claim_path(key: str) -> Path:
    return CLAIMS_DIR / f"{_sanitize(key)}.json"


def _pid_alive(pid: int) -> bool:
    """Check whether a process with this ID is currently running.

    On Windows, ``os.kill(pid, 0)`` would TERMINATE the process (signal
    numbers other than the CTRL events map to TerminateProcess), so a
    query-only handle via the Win32 API is used instead.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to another user.
        return True
    return True


def is_stale(claim: Claim) -> bool:
    """A claim is stale when its owning process is no longer running."""
    return not _pid_alive(claim.pid)


def read_claim(key: str) -> Optional[Claim]:
    """Return the current claim for a key, or None if the board is free.

    A malformed claim file (interrupted write, manual edit) is treated as a
    claim held by a dead process: reported with pid 0 so it shows up as stale
    and gets replaced on the next acquire.
    """
    path = _claim_path(key)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Could not read claim file %s: %s", path, e)
        return None
    try:
        data = json.loads(raw)
        return Claim(
            key=str(data.get("key", key)),
            pid=int(data.get("pid", 0)),
            app=str(data.get("app", "unknown")),
            user=str(data.get("user", "")),
            host=str(data.get("host", "")),
            experiment=str(data.get("experiment", "")),
            port=str(data.get("port", "")),
            started=str(data.get("started", "")),
        )
    except (ValueError, TypeError):
        logger.warning("Malformed claim file %s — treating as stale", path)
        return Claim(key=key, pid=0, app="unknown")


def acquire(key: str, app: str, experiment: str = "", port: str = "") -> Claim:
    """Claim a board for this process, or raise ``ClaimBusyError``.

    Stale claims (owner process dead) are replaced automatically. A repeat
    acquire by this same process refreshes the claim (e.g. a new experiment
    name) rather than failing.
    """
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    path = _claim_path(key)

    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    claim = Claim(
        key=key,
        pid=os.getpid(),
        app=app,
        user=user,
        host=socket.gethostname(),
        experiment=experiment,
        port=port,
        started=datetime.now().isoformat(timespec="seconds"),
    )
    payload = json.dumps(asdict(claim), indent=2)

    # Two create attempts: if the first finds an existing file that turns out
    # to be stale (or our own), it is removed and the create retried. A second
    # FileExistsError means another process won the race in between — report
    # the board as busy rather than looping.
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            _owned_keys.add(key)
            logger.info("Claimed board %s for %s", key, app)
            return claim
        except FileExistsError:
            existing = read_claim(key)
            if existing is None:
                continue  # Freed between our open and read — retry.
            if existing.pid == os.getpid():
                # Our own claim — refresh in place.
                path.write_text(payload, encoding="utf-8")
                _owned_keys.add(key)
                return claim
            if attempt == 1 and is_stale(existing):
                logger.warning(
                    "Replacing stale claim on %s (dead pid %s, was %s)",
                    key, existing.pid, existing.app,
                )
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise ClaimBusyError(existing)

    raise ClaimBusyError(read_claim(key) or Claim(key=key, pid=0, app="unknown"))


def release(key: str) -> bool:
    """Release a claim held by this process. Returns True if removed.

    A claim owned by a different live process is left untouched — releasing
    someone else's board is only possible via ``force_release``.
    """
    _owned_keys.discard(key)
    existing = read_claim(key)
    if existing is None:
        return False
    if existing.pid != os.getpid():
        logger.warning(
            "Not releasing claim on %s — owned by pid %s (%s), not us",
            key, existing.pid, existing.app,
        )
        return False
    try:
        _claim_path(key).unlink()
    except FileNotFoundError:
        return False
    logger.info("Released board %s", key)
    return True


def force_release(key: str) -> bool:
    """Unconditionally remove a claim, whoever owns it.

    For the launcher's guarded "force release" action only — never call this
    from an experiment code path.
    """
    _owned_keys.discard(key)
    try:
        _claim_path(key).unlink()
    except FileNotFoundError:
        return False
    logger.warning("Force-released claim on %s", key)
    return True


def release_all_owned():
    """Release every claim this process still holds (atexit safety net)."""
    for key in list(_owned_keys):
        try:
            release(key)
        except Exception:
            logger.exception("Failed to release claim on %s during exit", key)


def list_claims() -> Dict[str, Claim]:
    """Return every current claim on this machine, keyed by claim key."""
    claims: Dict[str, Claim] = {}
    try:
        paths = sorted(CLAIMS_DIR.glob("*.json"))
    except OSError:
        return claims
    for path in paths:
        claim = read_claim(path.stem)
        if claim is not None:
            claims[claim.key] = claim
    return claims


def claim_key_for(pot_id, port: str = "", registry_path: Optional[Path] = None) -> str:
    """Map a board's UI name (e.g. "A") to its claim key.

    Prefers the USB serial number from the onboarding registry, so every app
    resolves the same physical board to the same key no matter which port the
    OS assigned it today. Falls back to the port path (legacy, un-onboarded
    machines), then to the name itself.
    """
    try:
        entries = load_registry(registry_path or USER_REGISTRY_PATH)
    except Exception:
        entries = []
    for entry in entries:
        if entry.name == str(pot_id) and entry.usb_serial:
            return _sanitize(entry.usb_serial)
    if port:
        return _sanitize(port)
    return _sanitize(str(pot_id))


# Released automatically on normal interpreter exit; crashes are covered by
# the stale-pid check on the next acquire.
atexit.register(release_all_owned)

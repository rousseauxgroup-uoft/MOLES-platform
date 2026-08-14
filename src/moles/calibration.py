"""Per-potentiostat calibration coefficients and serial port mappings.

Two sources are consulted, in order of preference:

1. The user-writable registry at ``~/.moles/potentiostats.csv``, populated by
   ``moles-onboard``. This is the single source of truth on a configured
   machine: it carries both the calibration values and the current OS-specific
   port path for each board, keyed by an auto-assigned letter (A, B, ...).

2. As a fallback for machines that haven't been onboarded yet, the legacy
   bundled calibration CSV (``moles/resources/Potentiostat_Calibrations_*.csv``)
   supplies slope/intercept, and a hardcoded OS-specific dict supplies port
   paths. This keeps the existing electrolysis/CV UIs working out of the box
   on the lab's standard hardware naming convention until the user runs
   ``moles-onboard``.

The module exposes the same public names that the rest of the codebase has
always imported (``POTENTIOSTAT_CALIBRATIONS``, ``POTENTIOSTAT_PORTS``,
``get_potentiostat_ports``) so call sites don't change.
"""

import csv
import platform
from importlib.resources import files
from typing import Dict

from .onboarding.registry import USER_REGISTRY_PATH, load_registry

# Bundled calibration CSV used only when the user registry doesn't exist yet.
CALIBRATION_RESOURCE = "Potentiostat_Calibrations_01122026.csv"

# The bundled CSV predates moles-onboard and stores its constants in the old
# two-layer convention: each board's slope/intercept was measured as a small
# residual correction on top of a global hardware factor (1.0989 on the set
# side; the 976.9/-0.6 raw-to-mA formula on the read side). The driver now
# expects single end-to-end corrections, so legacy rows are converted once at
# load time. The math below is the exact composition of the old formulas.
_LEGACY_SET_FACTOR = 1.0989
_LEGACY_READ_SCALE = 976.9 / 1000.0   # old raw_A * 976.9 == (raw_A * 1000) * 0.9769
_LEGACY_READ_OFFSET = 0.6


def _convert_legacy(slope: float, intercept: float) -> Dict[str, float]:
    """Convert an old-convention slope/intercept pair to end-to-end form."""
    return {
        "slope": slope * _LEGACY_SET_FACTOR,
        "intercept": intercept,
        "read_slope": slope * _LEGACY_READ_SCALE,
        "read_intercept": intercept - _LEGACY_READ_OFFSET * slope,
    }


def _load_from_user_registry():
    """Return ``(calibrations, ports)`` from the user CSV, or ``None`` if absent.

    Only entries with both slope and intercept set contribute to the
    calibrations dict; uncalibrated rows still contribute their port mapping
    so the user can connect to a freshly-detected board before calibrating it.
    """
    if not USER_REGISTRY_PATH.is_file():
        return None

    try:
        entries = load_registry(USER_REGISTRY_PATH)
    except Exception as e:
        print(f"Warning: Could not read user registry at {USER_REGISTRY_PATH}: {e}")
        return None

    if not entries:
        return None

    calibrations: Dict[str, Dict[str, float]] = {}
    ports: Dict[str, str] = {}
    for entry in entries:
        if entry.is_calibrated:
            calibrations[entry.name] = {
                "slope": float(entry.slope),
                "intercept": float(entry.intercept),
                # None on rows saved before the read fit existed; the driver
                # then derives a read correction from the set pair.
                "read_slope": None if entry.read_slope is None else float(entry.read_slope),
                "read_intercept": None if entry.read_intercept is None else float(entry.read_intercept),
                # Voltage calibration; None until the board runs the voltage
                # sweep, in which case the driver keeps its historical default.
                "v_slope": None if entry.v_slope is None else float(entry.v_slope),
                "v_intercept": None if entry.v_intercept is None else float(entry.v_intercept),
                "v_read_slope": None if entry.v_read_slope is None else float(entry.v_read_slope),
                "v_read_intercept": None if entry.v_read_intercept is None else float(entry.v_read_intercept),
            }
        if entry.port:
            ports[entry.name] = entry.port

    print(f"Loaded {len(entries)} potentiostat(s) from {USER_REGISTRY_PATH}")
    return calibrations, ports


def load_calibrations() -> Dict[str, Dict[str, float]]:
    """Load per-potentiostat slope/intercept calibration values.

    Reads the user registry first; on machines that haven't been onboarded,
    falls back to the bundled CSV. Missing or malformed rows are skipped
    with a printed warning rather than raising, so a single bad row never
    blocks startup.
    """
    user = _load_from_user_registry()
    if user is not None:
        return user[0]

    calibrations: Dict[str, Dict[str, float]] = {}
    try:
        csv_path = files("moles.resources") / CALIBRATION_RESOURCE
    except (ModuleNotFoundError, FileNotFoundError) as e:
        print(f"Warning: Could not locate calibration resource: {e}. Using defaults.")
        return calibrations

    print(f"Loading calibrations from: {csv_path}")
    if not csv_path.is_file():
        print(f"Warning: Calibration file not found at {csv_path}. Using defaults (1.0, 0.0).")
        return calibrations

    try:
        with csv_path.open("r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            headers = next(reader, None)
            print(f"CSV Headers: {headers}")
            for i, row in enumerate(reader):
                if len(row) >= 3:
                    # Format: "PS A", Slope, Intercept
                    ps_name = row[0].strip()
                    if ps_name.upper().startswith("PS"):
                        parts = ps_name.split()
                        pid = parts[1].strip() if len(parts) > 1 else ps_name
                    else:
                        pid = ps_name

                    try:
                        slope = float(row[1])
                        intercept = float(row[2])
                        calibrations[pid] = _convert_legacy(slope, intercept)
                        print(f"  Loaded [{pid}] (legacy, converted): Slope={slope}, Int={intercept}")
                    except ValueError:
                        print(f"Skipping row {i + 2}: Invalid numbers {row}")
                        continue
        print(f"Final Calibration Keys: {list(calibrations.keys())}")
    except Exception as e:
        print(f"Error loading calibrations: {e}")

    return calibrations


def _legacy_os_ports() -> Dict[str, str]:
    """Hardcoded OS-specific port paths for the lab's standard 8-board setup.

    Used only when the user registry isn't present. New deployments should
    run ``moles-onboard`` so that ports are discovered dynamically by USB
    serial number rather than hardcoded — the values here are correct only
    for the original lab machine and are fragile to USB enumeration order.
    """
    os_name = platform.system()
    if os_name == "Windows":
        return {
            "A": "COM3", "B": "COM4", "C": "COM5", "D": "COM6",
            "E": "COM7", "F": "COM8", "G": "COM9", "H": "COM10",
        }
    elif os_name == "Darwin":
        return {
            "A": "/dev/tty.usbmodem206630AE42321",
            "B": "/dev/tty.usbmodem2084308342321",
            "C": "/dev/tty.usbmodem206630AD42321",
            "D": "/dev/tty.usbmodem204A308642321",
            "E": "/dev/tty.usbmodem2068309042321",
            "F": "/dev/tty.usbmodem2066309042321",
            "G": "/dev/tty.usbmodem204A308142321",
            "H": "/dev/tty.usbmodem206830A742321",
        }
    else:
        return {
            "A": "/dev/ttyUSB0", "B": "/dev/ttyUSB1", "C": "/dev/ttyUSB2", "D": "/dev/ttyUSB3",
            "E": "/dev/ttyUSB4", "F": "/dev/ttyUSB5", "G": "/dev/ttyUSB6", "H": "/dev/ttyUSB7",
        }


def get_potentiostat_ports() -> Dict[str, str]:
    """Return the mapping of potentiostat letter -> serial port path.

    Pulled from the user registry when present, otherwise from the legacy
    hardcoded OS-specific dict. Re-evaluated each call so callers see the
    latest registry contents after a ``moles-onboard`` run without needing
    to restart Python.
    """
    user = _load_from_user_registry()
    if user is not None:
        ports = user[1]
        if ports:
            return ports
    return _legacy_os_ports()


# Loaded once on import so other modules can read these as plain dicts.
POTENTIOSTAT_CALIBRATIONS: Dict[str, Dict[str, float]] = load_calibrations()
POTENTIOSTAT_PORTS: Dict[str, str] = get_potentiostat_ports()

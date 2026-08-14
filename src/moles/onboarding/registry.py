"""Persisted potentiostat registry — name, USB serial, port, calibration.

The registry is the single source of truth that downstream UIs (electrolysis,
CV) read at startup to know which boards exist and how to talk to them. It
lives at ``~/.moles/potentiostats.csv`` so the user can edit it manually if
needed and so the file survives package reinstalls.

This module handles only file I/O, name allocation, and merging detected
devices into the saved table. It has no Qt or hardware dependencies.
"""

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

from .discovery import DetectedDevice


# User-writable registry path. The directory is created on save when missing.
USER_REGISTRY_PATH = Path.home() / ".moles" / "potentiostats.csv"

# Column order written to disk. Kept stable so users editing the file by hand
# don't get surprised by reshuffling.
CSV_HEADERS = ("Name", "USB Serial", "Port", "Slope", "Intercept",
               "Read Slope", "Read Intercept",
               "V Slope", "V Intercept", "V Read Slope", "V Read Intercept",
               "Comments")


@dataclass
class Entry:
    """One row in the potentiostat registry.

    Slope/intercept map a requested current to the true delivered current
    (used when *setting* a current); read slope/intercept map the board's own
    current reading to the true current (used when *measuring*). Both pairs
    come from the same onboarding sweep. Rows saved before the read fit
    existed have the read pair empty — the driver falls back to deriving it
    from the set pair until the board is re-onboarded.
    """
    name: str                            # auto-assigned letter (A, B, ..., Z, AA, AB, ...)
    usb_serial: str                      # stable per-board identifier from the STM32 chip UID
    port: str = ""                       # current OS-specific path; refreshed on every scan
    slope: Optional[float] = None        # set-current correction; None until calibrated
    intercept: Optional[float] = None    # set-current offset (mA); None until calibrated
    read_slope: Optional[float] = None   # measured-current correction; None on pre-rework rows
    read_intercept: Optional[float] = None  # measured-current offset (mA)
    v_slope: Optional[float] = None      # set-potential correction; None until V-calibrated
    v_intercept: Optional[float] = None  # set-potential offset (V)
    v_read_slope: Optional[float] = None      # measured-potential correction
    v_read_intercept: Optional[float] = None  # measured-potential offset (V)
    comments: str = ""
    connected: bool = field(default=False, compare=False)  # populated at scan time, not saved

    @property
    def is_calibrated(self) -> bool:
        return self.slope is not None and self.intercept is not None

    @property
    def is_v_calibrated(self) -> bool:
        return self.v_slope is not None and self.v_intercept is not None


def load_registry(path: Path = USER_REGISTRY_PATH) -> List[Entry]:
    """Read entries from the registry CSV.

    Returns an empty list if the file doesn't exist yet (first run). Rows
    with an unparseable slope/intercept get those fields set to ``None`` so
    the UI can flag them as uncalibrated rather than crash on a bad cell.
    """
    if not path.is_file():
        return []

    entries: List[Entry] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Name") or "").strip()
            usb_serial = (row.get("USB Serial") or "").strip()
            if not name or not usb_serial:
                continue
            entries.append(
                Entry(
                    name=name,
                    usb_serial=usb_serial,
                    port=(row.get("Port") or "").strip(),
                    slope=_parse_float(row.get("Slope")),
                    intercept=_parse_float(row.get("Intercept")),
                    read_slope=_parse_float(row.get("Read Slope")),
                    read_intercept=_parse_float(row.get("Read Intercept")),
                    v_slope=_parse_float(row.get("V Slope")),
                    v_intercept=_parse_float(row.get("V Intercept")),
                    v_read_slope=_parse_float(row.get("V Read Slope")),
                    v_read_intercept=_parse_float(row.get("V Read Intercept")),
                    comments=(row.get("Comments") or "").strip(),
                )
            )
    return entries


def save_registry(entries: Iterable[Entry], path: Path = USER_REGISTRY_PATH) -> None:
    """Write entries to the registry CSV, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        for e in entries:
            writer.writerow([
                e.name,
                e.usb_serial,
                e.port,
                "" if e.slope is None else _format_float(e.slope),
                "" if e.intercept is None else _format_float(e.intercept),
                "" if e.read_slope is None else _format_float(e.read_slope),
                "" if e.read_intercept is None else _format_float(e.read_intercept),
                "" if e.v_slope is None else _format_float(e.v_slope),
                "" if e.v_intercept is None else _format_float(e.v_intercept),
                "" if e.v_read_slope is None else _format_float(e.v_read_slope),
                "" if e.v_read_intercept is None else _format_float(e.v_read_intercept),
                e.comments,
            ])


def next_name(existing: Set[str]) -> str:
    """Return the lowest unused Excel-style column name (A..Z, AA, AB, ...).

    Names already in ``existing`` are skipped, regardless of the order they
    were originally assigned, so deleting row "B" and rescanning will refill
    "B" before allocating "I".
    """
    n = 1
    while True:
        candidate = _index_to_name(n)
        if candidate not in existing:
            return candidate
        n += 1


def reconcile(
    detected: Iterable[DetectedDevice],
    existing: Iterable[Entry],
) -> Tuple[List[Entry], List[str]]:
    """Merge a fresh scan into the saved registry.

    Existing entries are matched to detected devices by USB serial. When a
    match is found, the entry's ``port`` is refreshed to the current value
    and ``connected`` is set to True; calibration values are never touched
    here. Detected devices with no matching entry get the next free name
    appended as a new row. Saved entries that were not detected stay in the
    list with ``connected=False`` so the user doesn't lose calibration data
    for a board that's merely unplugged.

    Returns ``(merged_entries, newly_added_names)`` so the GUI can highlight
    which rows are brand new.
    """
    merged: List[Entry] = []
    by_serial = {e.usb_serial: e for e in existing}
    used_names = {e.name for e in by_serial.values()}
    detected_serials: Set[str] = set()
    new_names: List[str] = []

    for dev in detected:
        detected_serials.add(dev.usb_serial)
        existing_entry = by_serial.get(dev.usb_serial)
        if existing_entry is not None:
            existing_entry.port = dev.port
            existing_entry.connected = True
        else:
            name = next_name(used_names)
            used_names.add(name)
            new_entry = Entry(
                name=name,
                usb_serial=dev.usb_serial,
                port=dev.port,
                connected=True,
            )
            by_serial[dev.usb_serial] = new_entry
            new_names.append(name)

    for entry in by_serial.values():
        if entry.usb_serial not in detected_serials:
            entry.connected = False
        merged.append(entry)

    merged.sort(key=lambda e: _name_to_index(e.name))
    return merged, new_names


def _parse_float(value) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _format_float(value: float) -> str:
    """Round-trip-friendly float formatting for the CSV.

    Uses repr-style precision so re-reading a saved value yields exactly the
    number written, but trims trailing zeros for readability when the value
    is short.
    """
    text = f"{value:.6g}"
    return text


def _index_to_name(n: int) -> str:
    """Convert 1-based index to Excel-style column name (1->A, 27->AA)."""
    if n < 1:
        raise ValueError("index must be >= 1")
    name = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


def _name_to_index(name: str) -> int:
    """Inverse of ``_index_to_name``. Used only for stable sort order."""
    if not name:
        return 0
    n = 0
    for ch in name:
        if not ch.isalpha():
            return 10**9  # sort unparseable names to the end
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return n

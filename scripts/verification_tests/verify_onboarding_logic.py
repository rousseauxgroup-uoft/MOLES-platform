"""Hardware-free checks for the ``moles-onboard`` logic modules.

Run with: ``python scripts/verification_tests/verify_onboarding_logic.py``

Covers the parts that are easy to get wrong without hardware in the loop:
- Excel-style name allocation across the A->Z->AA boundary.
- Reconciling a fresh scan against an existing registry (refresh, add, miss).
- CSV round-trip (write then read should yield equal entries).
- Linear fit on a known-good data set.

Does not exercise serial I/O, the GUI, or the driver — those need a real
device and a person watching the multimeter, per the project's hardware
safety policy.
"""

import sys
import tempfile
from pathlib import Path

# Make the package importable when running this script directly from a
# checkout that hasn't been ``pip install``ed.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from moles.onboarding import calibration_sweep
from moles.onboarding.discovery import DetectedDevice
from moles.onboarding.registry import (
    Entry,
    _index_to_name,
    load_registry,
    next_name,
    reconcile,
    save_registry,
)


def _check(label, ok, detail=""):
    marker = "PASS" if ok else "FAIL"
    print(f"  [{marker}] {label}{(' — ' + detail) if detail else ''}")
    return ok


def test_index_to_name():
    print("Excel-style name allocation:")
    cases = [
        (1, "A"),
        (2, "B"),
        (26, "Z"),
        (27, "AA"),
        (28, "AB"),
        (52, "AZ"),
        (53, "BA"),
        (702, "ZZ"),
        (703, "AAA"),
    ]
    all_ok = True
    for idx, expected in cases:
        got = _index_to_name(idx)
        all_ok &= _check(f"index {idx} -> {expected}", got == expected, f"got {got!r}")
    return all_ok


def test_next_name_skips_used():
    print("next_name skips used letters:")
    used = {"A", "B", "C"}
    ok = _check("returns D when A-C used", next_name(used) == "D")
    used_with_gap = {"A", "C"}
    ok &= _check("fills gap (B) before D", next_name(used_with_gap) == "B")
    used_to_z = {_index_to_name(i) for i in range(1, 27)}
    ok &= _check("rolls over to AA after Z", next_name(used_to_z) == "AA")
    return ok


def test_reconcile_basic():
    print("reconcile(): refresh + add + miss:")
    existing = [
        Entry(name="A", usb_serial="SERIAL_A", port="/dev/ttyOLD_A", slope=1.0, intercept=0.0),
        Entry(name="B", usb_serial="SERIAL_B", port="/dev/ttyOLD_B", slope=1.1, intercept=0.05),
    ]
    detected = [
        DetectedDevice(port="/dev/ttyNEW_A", usb_serial="SERIAL_A", vid=0x0483, pid=0x5740),
        DetectedDevice(port="/dev/ttyNEW_C", usb_serial="SERIAL_C", vid=0x0483, pid=0x5740),
    ]
    merged, new_names = reconcile(detected, existing)

    by_name = {e.name: e for e in merged}
    ok = True
    ok &= _check("A's port refreshed",
                 by_name["A"].port == "/dev/ttyNEW_A")
    ok &= _check("A's calibration preserved",
                 by_name["A"].slope == 1.0 and by_name["A"].intercept == 0.0)
    ok &= _check("A flagged connected", by_name["A"].connected is True)
    ok &= _check("B kept (not detected this scan)", "B" in by_name)
    ok &= _check("B flagged not connected", by_name["B"].connected is False)
    ok &= _check("B's calibration preserved",
                 by_name["B"].slope == 1.1)
    ok &= _check("New device assigned name 'C'", "C" in by_name)
    ok &= _check("Newly added list contains 'C'", new_names == ["C"])
    ok &= _check("C is uncalibrated", by_name["C"].slope is None)
    return ok


def test_csv_round_trip():
    print("CSV round-trip:")
    entries = [
        Entry(name="A", usb_serial="SN1", port="/dev/A", slope=1.093, intercept=0.02, comments="verified"),
        Entry(name="B", usb_serial="SN2", port="/dev/B", slope=None, intercept=None, comments=""),
        Entry(name="AA", usb_serial="SN3", port="COM47", slope=0.987, intercept=-0.015, comments="rebuilt"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "potentiostats.csv"
        save_registry(entries, path)
        loaded = load_registry(path)

    ok = _check("row count matches", len(loaded) == len(entries),
                f"saved {len(entries)} -> loaded {len(loaded)}")
    if not ok:
        return False
    for original, restored in zip(entries, loaded):
        ok &= _check(f"name {original.name} preserved", restored.name == original.name)
        ok &= _check(f"serial for {original.name} preserved", restored.usb_serial == original.usb_serial)
        ok &= _check(f"port for {original.name} preserved", restored.port == original.port)
        ok &= _check(f"slope for {original.name} preserved", restored.slope == original.slope)
        ok &= _check(f"intercept for {original.name} preserved", restored.intercept == original.intercept)
    return ok


def test_fit():
    print("Linear fit:")
    # Perfect line: y = 0.95x + 0.05
    applied = [0.1, 1.0, 5.0, 10.0, 15.0, 30.0]
    measured = [0.95 * x + 0.05 for x in applied]
    slope, intercept, r2 = calibration_sweep.fit(applied, measured)
    ok = True
    ok &= _check("slope ≈ 0.95", abs(slope - 0.95) < 1e-9, f"got {slope}")
    ok &= _check("intercept ≈ 0.05", abs(intercept - 0.05) < 1e-9, f"got {intercept}")
    ok &= _check("R² ≈ 1.0 for perfect line", abs(r2 - 1.0) < 1e-9, f"got {r2}")

    # Mismatched-length input should raise.
    raised = False
    try:
        calibration_sweep.fit([1, 2, 3], [1, 2])
    except ValueError:
        raised = True
    ok &= _check("mismatched lengths raise ValueError", raised)
    return ok


def main() -> int:
    print("=" * 60)
    print("moles-onboard logic verification")
    print("=" * 60)
    results = [
        test_index_to_name(),
        test_next_name_skips_used(),
        test_reconcile_basic(),
        test_csv_round_trip(),
        test_fit(),
    ]
    print("=" * 60)
    if all(results):
        print("All checks passed.")
        return 0
    failed = sum(1 for r in results if not r)
    print(f"{failed} group(s) had failures.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

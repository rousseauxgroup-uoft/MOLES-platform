"""Read a potentiostat's crash diagnostics after a suspected reboot.

Run this right after an experiment dies with a serial-communication error,
WITHOUT unplugging the board first — the "why did I last reboot" flags are
captured when the firmware boots, so a manual power-cycle overwrites the
evidence with a normal power-up flag.

Usage (from the repository root, with all MOLES apps closed):

    python scripts/read_crash_diagnostics.py            # single registered board
    python scripts/read_crash_diagnostics.py --board A  # pick a board by ID
    python scripts/read_crash_diagnostics.py --port COM5  # or give the port directly

Connecting also drives the board to a safe state (output switch off), so it
is safe to run with a cell still wired up.
"""

import argparse
import os
import sys

# Allow running straight from a checkout without `pip install -e .`
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from moles.calibration import get_potentiostat_ports  # noqa: E402
from moles.driver.ps4_ref import Potentiostat  # noqa: E402

# STM32 reset-cause register bits (see firmware ProcessDiagnosticsRead)
RESET_BITS = {
    31: "low-power reset",
    30: "window watchdog reset",
    29: "independent watchdog reset (firmware froze >2 s mid-run)",
    28: "software reset",
    27: "brown-out reset (supply voltage dipped)",
    26: "pin reset / normal power-up",
}


def pick_port(args):
    if args.port:
        return args.port, args.port
    ports = get_potentiostat_ports()
    if not ports:
        sys.exit("No boards in the registry. Pass the port directly: --port COM5")
    if args.board:
        if args.board not in ports:
            sys.exit(f"Board {args.board!r} not in registry. Known: {', '.join(ports)}")
        return args.board, ports[args.board]
    if len(ports) == 1:
        return next(iter(ports.items()))
    listing = ", ".join(f"{pid} ({port})" for pid, port in ports.items())
    sys.exit(f"Several boards registered: {listing}. Pick one with --board ID")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--board", help="board ID from the registry (e.g. A)")
    parser.add_argument("--port", help="serial port, bypassing the registry")
    args = parser.parse_args()

    board, port = pick_port(args)
    print(f"Connecting to board {board} on {port} ...")
    ps = Potentiostat(serial_port=port)
    try:
        ps.connect()
        diag = ps.read_diagnostics()
    finally:
        try:
            ps.disconnect()
        except Exception:
            pass

    print()
    print(f"Raw reset flags : 0x{diag['reset_flags']:08X}")
    causes = [name for bit, name in RESET_BITS.items()
              if diag["reset_flags"] & (1 << bit)]
    for cause in causes or ["(none set — register may have been cleared)"]:
        print(f"  - {cause}")
    print(f"tx_dropped      : {diag['tx_dropped']} (messages dropped, host not reading)")
    print(f"rx_overflow     : {diag['rx_overflow']} (command frames discarded)")
    print(f"starved_steps   : {diag['starved_steps']} (steps spent waiting for waveform data)")
    print()

    if diag["watchdog_reset"]:
        print("VERDICT: the firmware froze and the watchdog rebooted the board.")
    elif diag["reset_flags"] & (1 << 27):
        print("VERDICT: brown-out — the board's supply voltage dipped and reset it.")
    else:
        print("VERDICT: no crash-type reset recorded. Either the board never")
        print("rebooted (host/USB-side failure), or it was power-cycled before")
        print("this script ran, which erases the evidence.")
    print("Note: the three counters reset to zero when the board reboots, so")
    print("after a genuine crash they describe the fresh boot, not the bad run.")


if __name__ == "__main__":
    main()

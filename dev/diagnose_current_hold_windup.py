#!/usr/bin/env python3
"""
Current-hold windup diagnosis (Experiment #3).

The firmware's current-hold mode keeps adjusting the DAC on its own to reach
a target current. Opening the CE-WE switch does NOT stop that loop, so with
the circuit broken the firmware chases a current it can never reach and walks
the DAC toward its 0 / 3.3 V limit ("windup"). Re-closing the switch then
slams the cell with a railed setpoint.

Two modes:

  PASSIVE (default) — detect a leftover current-hold from a previous session:
    opens the switch (safe: output disconnected) and polls the DAC. A flat
    DAC means no hold is active; a steadily walking DAC means windup is
    happening right now.

  PROVOKE (--provoke) — demonstrate the mechanism on purpose: starts a small
    current-hold on a dummy cell, opens the switch mid-hold, and records the
    DAC walking to the rail. DUMMY CELL ONLY (resistor between the leads).

Either way the script finishes by stopping the hold and re-matching the DAC
to the sensed potential, so the device is left safe.

SAFETY: Run manually; never auto-run. --provoke requires a dummy cell.

Usage:
    python diagnose_current_hold_windup.py --port /dev/tty.usbmodemXXXX
    python diagnose_current_hold_windup.py --port ... --provoke
"""
import argparse
import csv
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

# Make the moles package importable when running straight from the repo
current_dir = Path(__file__).resolve().parent
src_dir = current_dir.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

try:
    from moles.driver.ps4_ref import Potentiostat, SwitchState, DAC
except ImportError:
    print("Error: Could not import Potentiostat module.")
    sys.exit(1)


def get_default_port():
    # Replace with your own potentiostat's serial port.
    # Find it via: `python -m serial.tools.list_ports` (macOS/Linux)
    # or Device Manager -> Ports (Windows).
    os_name = platform.system()
    if os_name == "Darwin":
        return "/dev/tty.usbmodemREPLACE"
    elif os_name == "Windows":
        return "COM3"
    return "/dev/ttyUSB0"


def safe_shutdown(ps):
    """Fail-state: stop the firmware current-hold, re-match the DAC to the
    sensed potential (so the next switch-close is gentle), and cut the output."""
    try:
        ps.write_current_hold_stop()
    except Exception:
        pass
    try:
        sensed = float(ps.read_potential_calibrated()[0])
        ps.write_potential_calibrated(sensed)
    except Exception:
        pass
    try:
        ps.write_switch(SwitchState.Off)
    except Exception:
        pass


def read_dac_v(ps):
    """Current DAC setpoint on the CE input channel, in host-side Volts."""
    return float(ps.read_DAC(DAC.CE_IN)[0])


def watch_dac(ps, seconds, label, writer):
    """Poll the DAC setpoint and report whether it is drifting or flat."""
    print(f"\n--- Watching DAC ({label}) for {seconds:.0f} s ---")
    t0 = time.perf_counter()
    trace = []
    while (time.perf_counter() - t0) < seconds:
        t = time.perf_counter() - t0
        v = read_dac_v(ps)
        trace.append((t, v))
        writer.writerow([label, f"{t:.3f}", f"{v:.5f}"])
        time.sleep(0.1)

    ts = np.array([t for t, _ in trace])
    vs = np.array([v for _, v in trace])
    drift = float(vs[-1] - vs[0])
    # Slope in V/s tells apart real windup (steady walk) from read noise
    rate = float(np.polyfit(ts, vs, 1)[0]) if len(ts) > 2 else 0.0

    # The DAC rails, converted to the same host-side Volt scale the reads use
    rail_lo, rail_hi = (float(x) for x in ps._scale_output_voltages([0.0, 3.3]))
    at_rail = vs[-1] <= rail_lo + 0.05 or vs[-1] >= rail_hi - 0.05

    print(f"start {vs[0]:+.4f} V -> end {vs[-1]:+.4f} V | "
          f"drift {drift*1000:+.1f} mV | rate {rate*1000:+.2f} mV/s | "
          f"rails [{rail_lo:+.2f}, {rail_hi:+.2f}] V"
          + ("  ** AT RAIL **" if at_rail else ""))
    return drift, rate, at_rail


def verdict(drift, rate, at_rail, context):
    walking = abs(drift) > 0.02 and abs(rate) > 0.002  # >20 mV total, >2 mV/s
    if walking or at_rail:
        print(f"\nVERDICT ({context}): DAC is walking on its own — a firmware "
              "current-hold is active with the switch open. Windup CONFIRMED.")
    else:
        print(f"\nVERDICT ({context}): DAC is flat — no current-hold running. "
              "Windup is not the mechanism in this state.")
    return walking or at_rail


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--port", default=get_default_port())
    parser.add_argument("--watch", type=float, default=10.0,
                        help="Seconds to poll the DAC after opening the switch")
    parser.add_argument("--provoke", action="store_true",
                        help="Actively start a current-hold, then open the "
                             "switch to demonstrate windup. DUMMY CELL ONLY.")
    parser.add_argument("--target-ma", type=float, default=0.1,
                        help="Current-hold target for --provoke, in mA")
    args = parser.parse_args()

    out_dir = current_dir / "diagnostic_logs"
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"windup_{datetime.now():%Y%m%d_%H%M%S}.csv"

    print(f"Connecting to {args.port} ...")
    ps = Potentiostat(serial_port=args.port)
    try:
        ps.connect()
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["phase", "t_s", "dac_V"])

            # PASSIVE: deliberately do NOT stop current-hold first — the whole
            # point is to catch one left running by a previous session
            print("\n=== Passive check: is a leftover current-hold active? ===")
            watch_dac(ps, 3.0, "baseline_switch_untouched", writer)
            ps.write_switch(SwitchState.Off)
            d, r, rail = watch_dac(ps, args.watch, "passive_switch_open", writer)
            verdict(d, r, rail, "passive")

            if args.provoke:
                print("\n=== Provoke mode: demonstrating windup on purpose ===")
                confirm = input("A DUMMY CELL (resistor) is connected, no "
                                "chemistry attached? [y/N] ").strip().lower()
                if confirm != "y":
                    print("Aborting provoke phase.")
                else:
                    # Start from a clean, matched state
                    ps.write_current_hold_stop()
                    sensed = float(ps.read_potential_calibrated()[0])
                    ps.write_potential_calibrated(sensed)
                    ps.write_switch(SwitchState.On)
                    time.sleep(0.5)

                    print(f"Starting current-hold at {args.target_ma} mA ...")
                    ps.write_current_hold_calibrated(args.target_ma)
                    time.sleep(3.0)
                    held = ps.read_potential_current_calibrated()
                    print(f"Holding: {float(held[0]):+.4f} V, "
                          f"{float(held[1]):+.4f} mA")

                    print("Opening switch WITH the hold still active ...")
                    ps.write_switch(SwitchState.Off)
                    d, r, rail = watch_dac(ps, args.watch,
                                           "provoked_switch_open", writer)
                    verdict(d, r, rail, "provoked")
                    print("\nNote: had this been read_ocp(), the switch would "
                          "now RE-CLOSE onto this railed setpoint — that is "
                          "the re-close slam described in the analysis.")

        print(f"\nRaw data: {csv_path}")
    except Exception:
        safe_shutdown(ps)
        raise
    finally:
        safe_shutdown(ps)
        try:
            ps.disconnect()
        except Exception:
            pass
        print("Device left safe: hold stopped, DAC matched, switch open.")


if __name__ == "__main__":
    main()

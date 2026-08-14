#!/usr/bin/env python3
"""
Switch-open spike diagnosis: DAC-setpoint sweep (Experiments #1 and #2).

Tests whether the voltage transient at CE-WE switch OPEN scales with how far
the DAC setpoint sits from the cell's open-circuit potential (OCP) at the
moment of opening.

For each offset delta in a list, the script:
  1. matches the DAC to the measured OCP and closes the switch,
  2. steps the setpoint to (OCP + delta) and lets the current settle,
  3. opens the switch and immediately reads the potential as fast as the
     serial link allows (~ms resolution),
  4. records how the potential jumps and relaxes back to OCP.

How to read the results:
  - Jump size growing with |delta|  -> confirms the "loop opened while current
    was flowing" mechanism (Experiments #1/#2). The instantaneous part of the
    jump is the ohmic (i*R) step; anything that overshoots BEYOND the band
    between the held potential and the final OCP points to the control
    amplifier slamming open-loop (#1) rather than passive relaxation (#2).
  - A residual blip at delta = 0 (no current flowing) -> switch charge
    injection, not the control loop.

The serial link cannot see microsecond-scale transients. For those, put an
oscilloscope on CE and WE and trigger on the falling edge of the CE_EN line
(STM32 pin PB13); this script tells you WHICH conditions to scope.

SAFETY: Use a dummy cell (e.g. a 10 kOhm resistor between the electrode
leads), not a chemical cell. Run manually; never auto-run.

Usage:
    python diagnose_switch_open_delta_sweep.py --port /dev/tty.usbmodemXXXX
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
    from moles.driver.ps4_ref import Potentiostat, SwitchState, Resistors
except ImportError:
    print("Error: Could not import Potentiostat module.")
    sys.exit(1)

GAIN_CHOICES = {
    "100": Resistors.R_100,
    "1k": Resistors.R_1K,
    "10k": Resistors.R_10K,
    "100k": Resistors.R_100K,
    "1m": Resistors.R_1M,
}


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
    """Fail-state: cut the output and stop any firmware current-hold so the
    device is never left driving the cell after an error or at exit."""
    for action in (ps.write_current_hold_stop,
                   lambda: ps.write_switch(SwitchState.Off)):
        try:
            action()
        except Exception:
            pass  # best effort — device may already be unplugged


def read_v(ps):
    """Single calibrated WE-RE potential reading in Volts."""
    return float(ps.read_potential_calibrated()[0])


def mean_v(ps, n=5, pause=0.05):
    """Average several potential readings to smooth out noise."""
    vals = []
    for _ in range(n):
        vals.append(read_v(ps))
        time.sleep(pause)
    return float(np.mean(vals))


def run_sweep(ps, deltas, repeats, settle_s, burst_s, writer):
    summary = []

    # OCP baseline with the switch open — the reference every jump is measured against
    ps.write_switch(SwitchState.Off)
    time.sleep(2.0)
    ocp = mean_v(ps)
    print(f"Baseline OCP: {ocp:+.4f} V")

    for rep in range(repeats):
        for delta in deltas:
            target = ocp + delta

            # Close the switch with the DAC already matched to OCP, so the
            # CLOSE event is gentle and only the OPEN event is under test
            ps.write_potential_calibrated(ocp)
            time.sleep(0.2)
            ps.write_switch(SwitchState.On)
            time.sleep(0.5)

            # Step to the offset setpoint and let the current settle
            ps.write_potential_calibrated(target)
            time.sleep(settle_s)
            pre = ps.read_potential_current_calibrated()
            pre_v, pre_i_mA = float(pre[0]), float(pre[1])

            # Open the switch, then read potential as fast as possible
            t0 = time.perf_counter()
            ps.write_switch(SwitchState.Off)
            burst = []
            while (time.perf_counter() - t0) < burst_s:
                burst.append((time.perf_counter() - t0, read_v(ps)))

            final_ocp = mean_v(ps, n=3)
            burst_v = np.array([v for _, v in burst])

            # Jump: first reading after open vs. the held potential.
            # Overshoot: any excursion outside the band spanned by the held
            # potential and the final OCP — passive relaxation cannot do that.
            jump = burst_v[0] - pre_v
            band_lo, band_hi = min(pre_v, final_ocp), max(pre_v, final_ocp)
            margin = 0.010  # 10 mV noise allowance
            overshoot = max(0.0, float(band_lo - burst_v.min()) - margin,
                            float(burst_v.max() - band_hi) - margin)

            for t, v in burst:
                writer.writerow([rep, f"{delta:+.3f}", "burst",
                                 f"{t:.5f}", f"{v:.5f}", ""])
            writer.writerow([rep, f"{delta:+.3f}", "pre_open",
                             "", f"{pre_v:.5f}", f"{pre_i_mA:.5f}"])

            summary.append((rep, delta, pre_i_mA, jump, overshoot, final_ocp))
            print(f"  rep {rep}  delta {delta:+.3f} V | held {pre_v:+.4f} V, "
                  f"{pre_i_mA:+.4f} mA | jump {jump*1000:+.1f} mV | "
                  f"overshoot {overshoot*1000:.1f} mV | OCP now {final_ocp:+.4f} V")

    return ocp, summary


def print_interpretation(summary):
    deltas = sorted(set(d for _, d, *_ in summary))
    print("\n=== Summary (averaged over repeats) ===")
    print(f"{'delta (V)':>10} | {'I at open (mA)':>15} | {'jump (mV)':>10} | {'overshoot (mV)':>15}")
    for d in deltas:
        rows = [s for s in summary if s[1] == d]
        i_mean = np.mean([r[2] for r in rows])
        j_mean = np.mean([abs(r[3]) for r in rows]) * 1000
        o_max = np.max([r[4] for r in rows]) * 1000
        print(f"{d:>+10.3f} | {i_mean:>+15.4f} | {j_mean:>10.1f} | {o_max:>15.1f}")

    print("""
How to interpret:
  - Jump growing with |delta| (and with current): the transient comes from
    opening the loop while current flows. Instantaneous part = ohmic step
    (Experiment #2, expected physics).
  - Overshoot column nonzero: potential briefly left the band between the
    held value and OCP — passive relaxation can't do that. That is the
    control amplifier slamming open-loop (Experiment #1).
  - Nonzero jump at delta = 0: charge injection from the switch itself.
  - This link samples at ~ms. For the us-scale spike, scope CE and WE,
    trigger on the falling edge of CE_EN (PB13), at the deltas that showed
    the largest jumps here.""")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--port", default=get_default_port())
    parser.add_argument("--deltas", default="0,0.1,-0.1,0.25,-0.25,0.5,-0.5",
                        help="Comma-separated setpoint offsets from OCP, in Volts")
    parser.add_argument("--repeats", type=int, default=3,
                        help="Repeats per delta (the fault is intermittent)")
    parser.add_argument("--gain", choices=GAIN_CHOICES, default="10k",
                        help="Fixed gain resistor (match your dummy cell)")
    parser.add_argument("--settle", type=float, default=2.0,
                        help="Seconds to hold the setpoint before opening")
    parser.add_argument("--burst", type=float, default=1.5,
                        help="Seconds of fast reads after opening")
    args = parser.parse_args()

    deltas = [float(d) for d in args.deltas.split(",")]

    out_dir = current_dir / "diagnostic_logs"
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"delta_sweep_{datetime.now():%Y%m%d_%H%M%S}.csv"

    print(f"Connecting to {args.port} ...")
    print("Reminder: use a DUMMY CELL (resistor), not a chemical cell.")
    ps = Potentiostat(serial_port=args.port)
    try:
        ps.connect()
        # Kill any leftover firmware current-hold BEFORE the first switch-open,
        # otherwise its background loop contaminates every measurement
        ps.write_current_hold_stop()
        ps.write_auto_gain(False)
        ps.write_gain(GAIN_CHOICES[args.gain])
        time.sleep(0.3)

        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["repeat", "delta_V", "phase", "t_s",
                             "potential_V", "current_mA"])
            ocp, summary = run_sweep(ps, deltas, args.repeats,
                                     args.settle, args.burst, writer)

        print_interpretation(summary)
        print(f"\nRaw data: {csv_path}")

        # Leave the DAC parked at OCP so the next session starts matched
        ps.write_potential_calibrated(ocp)
    except Exception:
        safe_shutdown(ps)
        raise
    finally:
        safe_shutdown(ps)
        try:
            ps.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()

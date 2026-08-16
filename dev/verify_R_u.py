#!/usr/bin/env python3
"""**Hardware-facing.** Validate `measure_R_u` against a resistor of known value.

Run this manually with a plain resistor wired in place of the cell (a "dummy
cell"): connect the working, reference, and counter electrode leads across the
same resistor. Because a resistor has no double-layer capacitance, the current
after the potential step is flat rather than decaying, and the measured
resistance should simply equal the resistor's marked value.

What it reports:
  * measured R_u against the known value, at several step sizes and repeats
  * the same trace analysed three ways (peak / first sample / mean), which
    exposes how much the peak-picking is being pulled around by noise
  * the raw current traces, saved as CSV for inspection

Never run automatically. Start with a resistor in the 100 ohm - 1 kohm range;
the measurement forces the 100 ohm gain, so larger resistors give very small
currents and correspondingly noisy results (the script warns when that
happens).

Usage:
    python dev/verify_R_u.py --resistor 100
    python dev/verify_R_u.py --resistor 470 --board A --repeats 5
    python dev/verify_R_u.py --resistor 100 --port /dev/tty.usbmodem1234
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

# Add src to path so the script runs from a checkout without installing
current_dir = Path(__file__).resolve().parent
src_dir = current_dir.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from moles.driver.ps4_ref import Potentiostat, Resistors
from moles.calibration import load_calibrations, get_potentiostat_ports
from moles import session

# Below this, the step current is too small for the forced 100 ohm gain to
# resolve cleanly and the result should be treated as indicative only.
MIN_USEFUL_CURRENT_MA = 0.05


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--resistor", type=float, required=True,
                   help="Known resistance of the dummy cell, in ohms")
    p.add_argument("--board", default=None,
                   help="Board name from the registry (A, B, ...). "
                        "Defaults to the only connected board.")
    p.add_argument("--port", default=None,
                   help="Serial port, bypassing registry lookup")
    p.add_argument("--steps", type=float, nargs="+", default=[20.0, 50.0, 100.0],
                   help="Step sizes to try, in mV (default: 20 50 100)")
    p.add_argument("--repeats", type=int, default=3,
                   help="Repeat measurements per step size (default: 3)")
    p.add_argument("--readings", type=int, default=30,
                   help="Current readings per step (default: 30)")
    p.add_argument("--outdir", default=str(Path.home() / "moles_data"),
                   help="Where to save the trace CSV")
    return p.parse_args()


def resolve_board(args):
    """Pick a serial port and its calibration, from the registry or --port."""
    calibrations = load_calibrations()

    if args.port:
        return args.port, calibrations.get(args.board or "", {})

    ports = get_potentiostat_ports()
    if not ports:
        raise SystemExit("No potentiostats found. Connect one, or pass --port.")

    if args.board:
        if args.board not in ports:
            raise SystemExit(f"Board {args.board} not found. Connected: "
                             f"{', '.join(sorted(ports))}")
        name = args.board
    elif len(ports) == 1:
        name = next(iter(ports))
    else:
        raise SystemExit(f"Several boards connected ({', '.join(sorted(ports))}). "
                         f"Pick one with --board.")

    return ports[name], calibrations.get(name, {})


def apply_calibration(ps, cal):
    """Load the board's calibration onto the driver object.

    measure_R_u works from calibrated current readings, so an uncalibrated
    board carries its gain error straight into R_u.
    """
    if not cal:
        print("  ! No calibration found for this board — using driver defaults. "
              "R_u will inherit any gain error, so onboard the board for "
              "quantitative work.")
        return
    ps.set_calibration_params(cal['slope'], cal['intercept'],
                              cal.get('read_slope'), cal.get('read_intercept'))
    if cal.get('v_slope') is not None:
        ps.set_voltage_calibration_params(
            cal['v_slope'], cal['v_intercept'],
            cal.get('v_read_slope'), cal.get('v_read_intercept'))
    print("  Calibration applied from the registry.")


def analyse_trace(result):
    """Re-derive R_u from a trace three ways, to expose noise sensitivity.

    measure_R_u takes the largest current in the burst. On a real cell the
    decay makes that the genuine ohmic peak, but on a flat (resistor) trace
    the largest reading is just the noisiest one, which biases R_u low. The
    first-sample and mean values bracket how much that matters here.
    """
    currents = np.asarray(result['currents_A'], dtype=float)
    step_V = result['step_V']

    def r_from(current_A):
        return abs(step_V / current_A) if abs(current_A) > 1e-12 else float('inf')

    return {
        'peak':  r_from(currents[np.argmax(np.abs(currents))]),
        'first': r_from(currents[0]),
        'mean':  r_from(float(np.mean(currents))),
        'spread_pct': (100.0 * np.std(currents) / abs(np.mean(currents))
                       if abs(np.mean(currents)) > 1e-12 else float('nan')),
        'peak_index': int(np.argmax(np.abs(currents))),
    }


def main():
    args = parse_args()
    known_R = args.resistor

    port, cal = resolve_board(args)
    print(f"\nDummy cell: {known_R:.1f} ohm resistor")
    print(f"Port: {port}")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    ps = Potentiostat(serial_port=port)
    apply_calibration(ps, cal)

    rows = []          # summary, one row per measurement
    trace_records = [] # raw traces for the CSV

    session.connect_with_retry(ps, "R_u-test")
    try:
        session.initialize_potentiostat(ps, "R_u-test")

        # A resistor has no redox chemistry, so its rest potential should sit
        # near zero. A large value usually means a lead is off or the dummy
        # cell is not what you think it is.
        ocp = float(ps.read_ocp())
        print(f"Rest potential: {ocp:+.4f} V", end="")
        print("  (expected near 0 V for a resistor)"
              if abs(ocp) < 0.05 else "  <-- unexpectedly large, check the wiring")

        print(f"\n{'step':>7} {'rep':>4} {'R_peak':>10} {'R_first':>10} "
              f"{'R_mean':>10} {'err%':>8} {'noise%':>8} {'pk@':>4}")
        print("-" * 68)

        for step_mV in args.steps:
            expected_mA = (step_mV / 1000.0) / known_R * 1000.0
            if expected_mA < MIN_USEFUL_CURRENT_MA:
                print(f"  ! {step_mV:.0f} mV across {known_R:.0f} ohm gives only "
                      f"{expected_mA:.3f} mA — near the noise floor at the "
                      f"100 ohm gain. Treat these rows as indicative.")

            for rep in range(args.repeats):
                result = ps.measure_R_u(step_mV=step_mV,
                                        n_readings=args.readings,
                                        settle_s=0.5)
                stats = analyse_trace(result)
                err_pct = 100.0 * (stats['peak'] - known_R) / known_R

                print(f"{step_mV:7.0f} {rep + 1:4d} {stats['peak']:10.2f} "
                      f"{stats['first']:10.2f} {stats['mean']:10.2f} "
                      f"{err_pct:+8.1f} {stats['spread_pct']:8.1f} "
                      f"{stats['peak_index']:4d}")

                rows.append((step_mV, stats['peak'], stats['first'], stats['mean']))
                for t_ms, i_A in zip(result['times_ms'], result['currents_A']):
                    trace_records.append((step_mV, rep + 1, t_ms, i_A))

                # Let the dummy cell settle between measurements
                time.sleep(1.0)

    finally:
        # Fail-state: never leave the cell energized, whatever happened above
        ps.write_switch(0)
        print("\nSwitch open.")
        ps.disconnect()
        print("Potentiostat disconnected.")

    if not rows:
        return

    arr = np.array([r[1] for r in rows], dtype=float)
    print("\n" + "=" * 68)
    print(f"R_u across all {len(arr)} measurements: "
          f"{arr.mean():.2f} +/- {arr.std():.2f} ohm  (known: {known_R:.1f} ohm)")
    print(f"Mean error: {100.0 * (arr.mean() - known_R) / known_R:+.1f}%")
    print("\nReading the columns: R_peak is what measure_R_u returns; R_first")
    print("and R_mean come from the same trace, for comparison.")
    print("\nExpect R_peak to land slightly BELOW the true value here. A resistor")
    print("gives a flat trace, so 'largest reading' just picks the noisiest")
    print("sample, which reads as extra current and so as less resistance. The")
    print("bias scales with read noise and grows with --readings (roughly -5% at")
    print("2% noise, worse if the step current is small). A real cell decays, so")
    print("its peak is a genuine signal and the bias is much smaller.")
    print("\nSo: R_peak a few percent low, with R_first close to R_mean, is a")
    print("PASS. R_peak far off, or drifting with step size, is not.")

    stamp = datetime.now().strftime('%Y_%m_%d_%H_%M_%S')
    trace_path = outdir / f"R_u_traces_{stamp}.csv"
    np.savetxt(trace_path, np.array(trace_records),
               delimiter=",", header="step_mV,repeat,time_ms,current_A",
               comments="", fmt="%.6g")
    print(f"\nTraces saved to {trace_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""**Hardware-facing.** Validate `measure_R_u` against a dummy cell of known R_u.

Run this manually with a dummy cell wired in place of the cell, either:

  * a plain resistor: counter and reference leads on one end, working lead on
    the other. The current after the step is flat, and R_u is the resistor.
  * a resistor in series with a capacitor (an "RC dummy cell"): counter and
    reference leads on the resistor's free end, working lead on the
    capacitor's. The current decays with time constant R*C, like a real
    cell's double layer charging, and R_u is still the resistor. This is the
    better test of the transient fit; e.g. 470 ohm with 1 uF gives ~0.47 ms,
    close to a real analytical cell. Pass --capacitor to have the expected
    time constant printed.

What it reports, per measurement: R_u and its error against the known value,
how the steps were made (by the DAC with the cell connected on firmware 1.7+,
otherwise by closing the switch), the fitted time constant, and any note or
refusal from the fit. The raw current traces are saved as CSV.

Never run automatically. Keep R in the 100 ohm - 2 kohm range: the
measurement runs on the least sensitive current range (+/-50 mA), so larger
resistors give currents too small to measure, and the fit refuses.

Usage:
    python dev/verify_R_u.py --resistor 470 --capacitor 1
    python dev/verify_R_u.py --resistor 100 --board A --repeats 5
    python dev/verify_R_u.py --resistor 470 --port /dev/tty.usbmodem1234
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

from moles.driver.ps4_ref import Potentiostat
from moles.calibration import load_calibrations, get_potentiostat_ports
from moles import session

# Below this, the step current is too small for the forced +/-50 mA range to
# resolve cleanly and the fit will likely refuse.
MIN_USEFUL_CURRENT_MA = 0.02


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--resistor", type=float, required=True,
                   help="Known resistance of the dummy cell, in ohms")
    p.add_argument("--capacitor", type=float, default=None,
                   help="Series capacitor of an RC dummy cell, in uF")
    p.add_argument("--board", default=None,
                   help="Board name from the registry (A, B, ...). "
                        "Defaults to the only connected board.")
    p.add_argument("--port", default=None,
                   help="Serial port, bypassing registry lookup")
    p.add_argument("--steps", type=float, nargs="+", default=[50.0],
                   help="Step sizes to try, in mV (default: 50, as the app uses)")
    p.add_argument("--repeats", type=int, default=3,
                   help="Repeat measurements per step size (default: 3)")
    p.add_argument("--readings", type=int, default=30,
                   help="Current readings per step on firmware without the "
                        "on-board capture (default: 30)")
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

    rows = []          # R_u of each accepted measurement
    trace_records = [] # raw traces for the CSV

    session.connect_with_retry(ps, "R_u-test")
    try:
        session.initialize_potentiostat(ps, "R_u-test")

        # A dummy cell has no redox chemistry, so its rest potential should
        # sit near zero. A large value usually means a lead is off or the
        # dummy cell is not what you think it is.
        ocp = float(ps.read_ocp())
        print(f"Rest potential: {ocp:+.4f} V", end="")
        print("  (expected near 0 V for a dummy cell)"
              if abs(ocp) < 0.05 else "  <-- unexpectedly large, check the wiring")
        if args.capacitor:
            print(f"Expected time constant: {known_R * args.capacitor * 1e-3:.3f} ms")

        print(f"\n{'step':>6} {'rep':>4} {'R_u':>9} {'err%':>7} {'steps by':>9} "
              f"{'tau':>9}  note")
        print("-" * 72)

        for step_mV in args.steps:
            expected_mA = (step_mV / 1000.0) / known_R * 1000.0
            if expected_mA < MIN_USEFUL_CURRENT_MA:
                print(f"  ! {step_mV:.0f} mV across {known_R:.0f} ohm gives only "
                      f"{expected_mA * 1000:.0f} uA, near the noise on the "
                      f"+/-50 mA range; expect refusals.")

            for rep in range(args.repeats):
                result = ps.measure_R_u(step_mV=step_mV,
                                        n_readings=args.readings,
                                        settle_s=0.5)
                fit = result.get('fit') or {}
                method = "switch" if result.get('control') is not None else "DAC"
                tau = fit.get('tau_ms')
                tau_text = f"{tau:.3f}ms" if tau is not None else "-"
                if result['fit_ok']:
                    R = result['R_u_ohm']
                    rows.append(R)
                    print(f"{step_mV:6.0f} {rep + 1:4d} {R:9.1f} "
                          f"{100.0 * (R - known_R) / known_R:+7.1f} {method:>9} "
                          f"{tau_text:>9}  {result['message']}")
                else:
                    print(f"{step_mV:6.0f} {rep + 1:4d} {'refused':>9} {'':>7} "
                          f"{method:>9} {tau_text:>9}  {result['message']}")

                for p in result.get('polarities') or []:
                    for t_ms, i_A in zip(p['times_ms'], p['currents_A']):
                        trace_records.append((step_mV, rep + 1, p['step_V'] * 1e3,
                                              t_ms, i_A))

                # Let the dummy cell settle between measurements
                time.sleep(1.0)

    finally:
        # Fail-state: never leave the cell energized, whatever happened above
        ps.write_switch(0)
        print("\nSwitch open.")
        ps.disconnect()
        print("Potentiostat disconnected.")

    if rows:
        arr = np.array(rows, dtype=float)
        print("\n" + "=" * 72)
        print(f"R_u across {len(arr)} accepted measurements: "
              f"{arr.mean():.1f} +/- {arr.std():.1f} ohm  (known: {known_R:.1f} ohm)")
        print(f"Mean error: {100.0 * (arr.mean() - known_R) / known_R:+.1f}%")
        print("\nA PASS is every repeat within a few percent of the known value, "
              "with no drift across step sizes.")

    if trace_records:
        stamp = datetime.now().strftime('%Y_%m_%d_%H_%M_%S')
        trace_path = outdir / f"R_u_traces_{stamp}.csv"
        np.savetxt(trace_path, np.array(trace_records),
                   delimiter=",", header="step_mV,repeat,step_polarity_mV,time_ms,current_A",
                   comments="", fmt="%.6g")
        print(f"\nTraces saved to {trace_path}")


if __name__ == "__main__":
    main()

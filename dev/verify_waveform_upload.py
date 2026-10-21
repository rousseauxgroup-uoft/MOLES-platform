#!/usr/bin/env python3
"""**Hardware-facing.** Confirm the fixes for the random CV/DPV output oscillation.

The fault (diagnosed 2026-09-23): CV/DPV uploaded their waveform in 66-byte
messages, which USB delivers as a 64-byte and a 2-byte packet. Firmware from
before September 2026 could run its command parser between the two, store
62 bytes, and leave every later waveform point 2 bytes off its float
boundary. The board then turned garbage numbers into rail-to-rail output
until its 24 kB waveform buffer ("ring") wrapped around.

This script uploads waveforms into the ring over and over, runs them, and
checks whether the ring came out intact. It is SAFE with anything connected:
the cell switch stays OPEN for the whole test, so no current flows, and every
test waveform sits within one float step (far below one DAC count) of 0 V,
so the output does not even move.

How corruption is spotted: the test waveform steps up by exactly one float
unit per point, so every point is a different number. After each run the
script reads back the setpoint the board applied last and looks it up:

  intact      the expected final point                      -> fine
  shifted     another waveform point: whole points lost      -> alignment kept
  misaligned  not a waveform point at all: read off its       -> THE FAULT
              float boundary
  short       the run stopped early; the board's diagnostics say whether
              points were missing (starved) or a corrupted point was refused

Modes:
  old-prefill          66-byte messages, board idle: the historical CV upload
  old-streamed         66-byte messages sent while the board steps (on the
                       first bench run this hit the fault less often than
                       old-prefill: 4 vs 8 of 20 uploads)
  legacy48-streamed    today's single-packet legacy messages (fix 1)
  positional-streamed  today's self-framing positional messages (fix 3;
                       skipped on firmware without CMD_BUFFER_WRITE_AT)
  driver-prefill       the exact upload code CV/DPV now use

What to expect:
  firmware before this fix    old-* modes: "misaligned" results (first bench
                              run, board TEST: 8 and 4 of 20 uploads); every
                              other mode: all intact
  firmware with this fix      no "misaligned" results in any mode, old-*
                              included. An occasional "short" means messages
                              were cut or merged in transit and the firmware
                              dropped them instead of misaligning (seen once
                              in legacy48-streamed on the first bench run)

Run it on the test board before flashing (to see the fault) and again after
flashing (to confirm it is gone). Never run automatically. Close all MOLES
apps first; the board must not be claimed by another program.

Usage:
    python dev/verify_waveform_upload.py
    python dev/verify_waveform_upload.py --board TEST --iterations 30
    python dev/verify_waveform_upload.py --port /dev/cu.usbmodem1234 --modes old-streamed legacy48-streamed
"""
import argparse
import csv
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

from moles.driver.ps4_ref import (Commands, Potentiostat, SerialReadTimeout,  # noqa: E402
                                  SwitchState)
from moles.calibration import get_potentiostat_ports  # noqa: E402

MODES = ["old-prefill", "old-streamed", "legacy48-streamed",
         "positional-streamed", "driver-prefill"]
RING_POINTS = 6000          # the board's 24 kB ring, as float32 points
STREAM_LEAD_POINTS = 960    # uploaded before streaming starts (~1 s at 1 ms/step)
STEP_MS = 1                 # fastest stepping: most USB traffic per second
SAMPLE_BYTES = 8            # float32 current + float32 potential (auto-gain off)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--board", default=None,
                   help="Board name from the registry (A, B, ...). "
                        "Defaults to the only connected board.")
    p.add_argument("--port", default=None, help="Serial port, bypassing registry lookup")
    p.add_argument("--modes", nargs="+", choices=MODES, default=MODES,
                   help="Which upload styles to test (default: all)")
    p.add_argument("--iterations", type=int, default=20,
                   help="Uploads per mode (default: 20; ~5-11 s each)")
    p.add_argument("--outdir", default=str(Path.home() / "moles_data"),
                   help="Where to save the per-upload CSV log")
    return p.parse_args()


def resolve_port(args):
    if args.port:
        return args.port, args.board or args.port
    ports = get_potentiostat_ports()
    if not ports:
        raise SystemExit("No potentiostats found. Connect one, or pass --port.")
    if args.board:
        if args.board not in ports:
            raise SystemExit(f"Board {args.board} not found. Connected: "
                             f"{', '.join(sorted(ports))}")
        return ports[args.board], args.board
    if len(ports) == 1:
        name = next(iter(ports))
        return ports[name], name
    raise SystemExit(f"Several boards connected ({', '.join(sorted(ports))}). "
                     f"Pick one with --board.")


def test_pattern(ps):
    """6000 distinct floats, all within a few float steps of the 0 V setpoint."""
    start = np.float32(ps._scale_input_voltages(0.0))
    pattern = np.empty(RING_POINTS, np.float32)
    value = start
    for k in range(RING_POINTS):
        pattern[k] = value
        value = np.nextafter(value, np.float32(3.3))
    return pattern


def send(ps, data, ring_pos, msg_bytes, positional, paced):
    """Upload bytes in fixed-size messages, legacy (append) or positional."""
    for off in range(0, len(data), msg_bytes):
        chunk = data[off:off + msg_bytes]
        if positional:
            header = np.uint16([len(chunk), ring_pos + off]).tobytes()
            ps._write_cmd(Commands._WRITE_BUFFER_AT, header + chunk, False)
        else:
            ps._write_cmd(Commands._WRITE_BUFFER, chunk, False)
        ps.serial.flush()
        if paced:
            time.sleep(0.01)    # the driver's pacing for an idle upload


def run_upload(ps, mode, pattern):
    """Upload the pattern in the mode's style, run it, and classify the ring."""
    data = pattern.tobytes()
    n_points = len(pattern)
    streamed = mode.endswith("streamed")
    msg_bytes = 64 if mode.startswith("old") else 48
    positional = (mode == "positional-streamed")
    messages = 0

    if mode == "driver-prefill":
        ps._start_waveform_upload(data)
        messages = int(np.ceil(len(data) / 48))
        pos = len(data)
    else:
        ps._reset_buffer(0)
        lead = STREAM_LEAD_POINTS * 4 if streamed else len(data)
        send(ps, data[:lead], 0, msg_bytes, positional, paced=True)
        messages += int(np.ceil(lead / msg_bytes))
        pos = lead

    ps._write_cmd(Commands.EXECUTE_VOLTAGE_BATCH, np.uint32([STEP_MS, n_points]),
                  True, retry=False)
    received = 0
    old_timeout = ps.serial.timeout
    ps.serial.timeout = 1.0
    try:
        while received < n_points:
            # Streamed modes read one message's worth of samples, then top the
            # ring up by one message, so uploads land while the board steps
            want = msg_bytes // 4 if (streamed and pos < len(data)) else 60
            want = min(want, n_points - received)
            got = ps.serial.read(SAMPLE_BYTES * want)
            received += len(got) // SAMPLE_BYTES
            if len(got) < SAMPLE_BYTES * want:
                break
            if streamed and pos < len(data):
                send(ps, data[pos:pos + msg_bytes], pos, msg_bytes, positional,
                     paced=False)
                messages += 1
                pos += msg_bytes
    finally:
        ps.serial.timeout = old_timeout

    stopped_early = received < n_points
    if stopped_early:
        ps.halt_batch_stream()
    diag = safe_diagnostics(ps)
    applied = np.float32(ps._read_dac_setpoint())

    matches = np.flatnonzero(pattern == applied)
    if stopped_early:
        verdict = "misaligned" if matches.size == 0 else "short"
    elif matches.size == 0:
        verdict = "misaligned"
    elif matches[0] != n_points - 1:
        verdict = "shifted"
    else:
        verdict = "intact"
    return {
        "mode": mode, "verdict": verdict, "messages": messages,
        "points_run": received, "starved_steps": diag.get("starved_steps", ""),
        "rejected_setpoints": diag.get("rejected_setpoints", ""),
        "rx_overflow": diag.get("rx_overflow", ""),
        "last_setpoint": repr(applied),
    }


_HAS_DIAGNOSTICS = True


def safe_diagnostics(ps):
    """The board's health counters, or {} on firmware that predates them
    (checked once: each failed attempt costs two read timeouts)."""
    global _HAS_DIAGNOSTICS
    if not _HAS_DIAGNOSTICS:
        return {}
    try:
        return ps.read_diagnostics()
    except (SerialReadTimeout, Exception):
        _HAS_DIAGNOSTICS = False
        return {}


def main():
    args = parse_args()
    port, name = resolve_port(args)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    log_path = outdir / f"waveform_upload_{name}_{datetime.now():%Y%m%d_%H%M%S}.csv"

    print(f"Connecting to {name} on {port} ...")
    ps = Potentiostat(serial_port=port)
    ps.connect()      # opens the cell switch, 100 ohm range, auto-gain off
    rows = []
    try:
        if int(np.asarray(ps.read_switch()).ravel()[0]) != 0:
            raise SystemExit("The cell switch did not open; stopping without testing.")
        ps.write_potential(0.0)
        pattern = test_pattern(ps)

        diag = safe_diagnostics(ps)
        new_firmware = "rejected_setpoints" in diag
        positional_ok = ps._probe_positional_writes()
        print(f"  Firmware: {'this fix (Sept 2026 or later)' if new_firmware else 'before this fix'}; "
              f"positional writes {'supported' if positional_ok else 'NOT supported'}")
        print("  The cell switch stays open for the whole test.\n")

        for mode in args.modes:
            if mode == "positional-streamed" and not positional_ok:
                print(f"{mode:20s} skipped (firmware lacks CMD_BUFFER_WRITE_AT)")
                continue
            print(f"{mode:20s} ", end="", flush=True)
            for _ in range(args.iterations):
                row = run_upload(ps, mode, pattern)
                rows.append(row)
                print({"intact": ".", "shifted": "s", "short": "!",
                       "misaligned": "X"}[row["verdict"]], end="", flush=True)
            print()
    finally:
        try:
            ps.halt_batch_stream()
            ps.write_potential(0.0)
            ps.write_switch(SwitchState.Off)
        finally:
            ps.disconnect()

    if rows:
        with open(log_path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    print("\n  . intact   s shifted   ! stopped short   X misaligned (the fault)\n")
    print(f"{'mode':20s} {'uploads':>7s} {'messages':>9s} {'intact':>7s} "
          f"{'shifted':>8s} {'short':>6s} {'MISALIGNED':>11s}")
    for mode in args.modes:
        sel = [r for r in rows if r["mode"] == mode]
        if not sel:
            continue
        count = {v: sum(r["verdict"] == v for r in sel)
                 for v in ("intact", "shifted", "short", "misaligned")}
        print(f"{mode:20s} {len(sel):7d} {sum(r['messages'] for r in sel):9d} "
              f"{count['intact']:7d} {count['shifted']:8d} {count['short']:6d} "
              f"{count['misaligned']:11d}")
    print(f"\nPer-upload log: {log_path}")
    fixed_modes = [r for r in rows if not r["mode"].startswith("old")]
    if any(r["verdict"] == "misaligned" for r in fixed_modes):
        print("RESULT: a fixed upload path came out misaligned. The fix is NOT "
              "working; keep the log and report it.")
    elif rows:
        print("RESULT: every upload through the fixed paths came out intact.")


if __name__ == "__main__":
    main()

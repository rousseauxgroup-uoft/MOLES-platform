#!/usr/bin/env python3
"""
Driver-side switch-event logger.

Wraps the Potentiostat class so that EVERY write_switch() call records, to a
CSV, the device state at that instant:

  - requested switch state (open/close) and the previous state
  - DAC setpoint on the CE input channel
  - sensed WE-RE potential and current
  - setpoint-vs-sensed mismatch (the suspected spike driver)
  - gain resistor index and auto-gain flag
  - on OPEN events: a short burst of fast potential reads right after the
    switch opens, to capture the jump and relaxation

Correlating "spike seen / not seen" with the logged mismatch and gain state
is the no-oscilloscope way to confirm the open-loop-slam hypothesis.

Two ways to use it:

  1. Standalone: runs N read_ocp() cycles — the exact call implicated in the
     original symptom (each one is a switch OPEN + RE-CLOSE pair):
         python log_switch_events.py --port /dev/tty.usbmodemXXXX --cycles 5

  2. Imported into your own experiment script, as a drop-in replacement:
         from log_switch_events import SwitchEventLogger
         ps = SwitchEventLogger(serial_port=...)   # instead of Potentiostat(...)

CAVEAT: the pre-switch snapshot adds a few serial round-trips (tens of ms)
BEFORE the switch physically moves, and the burst delays whatever follows.
Fine for diagnosis; do not leave in production runs where timing matters.

SAFETY: Run manually; never auto-run. Dummy cell recommended standalone.
"""
import argparse
import csv
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

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


class SwitchEventLogger(Potentiostat):
    """Potentiostat that snapshots device state around every switch toggle.

    Log rows go to CSV (one per event, plus one per burst sample). Events are
    also printed so mismatch warnings are visible live.
    """

    # Burst duration after each OPEN event, seconds. Set to 0 to disable.
    burst_s = 0.5
    # Setpoint-vs-sensed gap that earns a loud warning, Volts.
    mismatch_warn_V = 0.1

    def __init__(self, *args, log_path=None, **kwargs):
        super().__init__(*args, **kwargs)
        if log_path is None:
            log_dir = current_dir / "diagnostic_logs"
            log_dir.mkdir(exist_ok=True)
            log_path = log_dir / f"switch_events_{datetime.now():%Y%m%d_%H%M%S}.csv"
        self.log_path = Path(log_path)
        self._event_n = 0
        with open(self.log_path, "w", newline="") as f:
            csv.writer(f).writerow([
                "event", "wallclock", "requested", "previous",
                "dac_setpoint_V", "sensed_V", "sensed_mA", "mismatch_V",
                "gain_idx", "auto_gain", "burst_t_s", "burst_V",
            ])

    def _snapshot(self):
        """Read DAC setpoint, sensed V/I and gain state; never let a failed
        read block the actual switch command."""
        try:
            dac_v = float(self.read_DAC(DAC.CE_IN)[0])
            sensed = self.read_potential_current_calibrated()
            sensed_v, sensed_i = float(sensed[0]), float(sensed[1])
            gain = getattr(self, "Resistor", None)
            return dac_v, sensed_v, sensed_i, gain
        except Exception as exc:
            print(f"[switch-log] snapshot failed: {exc}")
            return None, None, None, None

    def write_switch(self, state):
        # Hold the port lock across snapshot + switch + burst so a competing
        # thread can't interleave commands into the middle of an event record
        with self._port_lock:
            self._event_n += 1
            n = self._event_n
            requested = "ON(close)" if state else "OFF(open)"
            previous = "ON" if self._switch_state else "OFF"

            dac_v, sensed_v, sensed_i, gain = self._snapshot()
            mismatch = (dac_v - sensed_v) if (dac_v is not None) else None

            super().write_switch(state)

            # Fast potential reads right after an OPEN capture the jump/relaxation
            burst = []
            if (not state) and self.burst_s > 0:
                t0 = time.perf_counter()
                while (time.perf_counter() - t0) < self.burst_s:
                    try:
                        burst.append((time.perf_counter() - t0,
                                      float(self.read_potential_calibrated()[0])))
                    except Exception:
                        break

        fmt = lambda x, p=4: "" if x is None else f"{x:+.{p}f}"
        with open(self.log_path, "a", newline="") as f:
            w = csv.writer(f)
            w.writerow([n, datetime.now().isoformat(timespec="milliseconds"),
                        requested, previous, fmt(dac_v), fmt(sensed_v),
                        fmt(sensed_i), fmt(mismatch), gain, self._auto_gain,
                        "", ""])
            for t, v in burst:
                w.writerow([n, "", "", "", "", "", "", "", "", "",
                            f"{t:.5f}", f"{v:.5f}"])

        line = (f"[switch-log] #{n} {previous} -> {requested} | "
                f"DAC {fmt(dac_v)} V, sensed {fmt(sensed_v)} V, "
                f"{fmt(sensed_i)} mA | mismatch {fmt(mismatch)} V | gain {gain}")
        if burst:
            jump = burst[0][1] - sensed_v if sensed_v is not None else None
            line += f" | first post-open read jumped {fmt(jump)} V"
        print(line)
        if mismatch is not None and abs(mismatch) > self.mismatch_warn_V:
            kind = ("re-closing onto a mismatched setpoint — slam risk"
                    if state else "opening while servoing away from OCP — spike risk")
            print(f"[switch-log]   ** WARNING: {kind} ({mismatch:+.3f} V) **")


def safe_shutdown(ps):
    """Fail-state: cut the output and stop any firmware current-hold."""
    for action in (ps.write_current_hold_stop,
                   lambda: ps.write_switch(SwitchState.Off)):
        try:
            action()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--port", default=get_default_port())
    parser.add_argument("--cycles", type=int, default=5,
                        help="Number of read_ocp() cycles to run and log")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Seconds between cycles")
    args = parser.parse_args()

    print(f"Connecting to {args.port} ...")
    ps = SwitchEventLogger(serial_port=args.port)
    try:
        ps.connect()
        ps.write_current_hold_stop()

        # Reproduce the implicated startup pattern: close, then read_ocp()
        # (which opens and re-closes). Every toggle lands in the CSV.
        print(f"\nRunning {args.cycles} read_ocp() cycles ...")
        ps.write_switch(SwitchState.On)
        for i in range(args.cycles):
            ocp = ps.read_ocp()
            print(f"cycle {i + 1}: OCP = {float(ocp):+.4f} V")
            time.sleep(args.interval)

        print(f"\nEvent log: {ps.log_path}")
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

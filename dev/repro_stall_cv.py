#!/usr/bin/env python3
"""Bench reproduction of the extended-CV USB failure: run a CV while
deliberately stalling the host's read loop, and report how the system coped.

*** HARDWARE-FACING SCRIPT — run it yourself, never automatically. ***
*** Use a dummy cell (e.g. a resistor between the electrode leads):  ***
*** the injected stall deliberately degrades potential control.      ***

What it does
------------
Runs a small CV through the same live batch loop the GUI uses, but with a
data callback that sleeps once, mid-run, for ``--stall`` seconds. Because the
callback runs inside the read loop, this simulates the GUI starving the
worker — the trigger of the July 2026 failures.

Expected outcomes
-----------------
* Old firmware + old host code, stall > ~5 s: Windows cuts its read pump at
  ~12 kB of backlog, the firmware wedges in its transmit busy-wait, the
  watchdog resets the chip ~2 s later, and the port dies (ClearCommError /
  Write timeout). Capture it with USBPcap and confirm with
  ``python dev/diagnose_usb_capture.py capture.pcapng``.
* Patched host code: stalls approaching the abort threshold end the run
  cleanly with a HostLagError (cell switched off, port still alive).
* Patched firmware: even if the read pump dies, the device drops samples
  instead of freezing; afterwards ``read_diagnostics()`` shows tx_dropped > 0
  and watchdog_reset False, and the device responds without a power-cycle.

Usage
-----
    python dev/repro_stall_cv.py --port COM5 --stall 8
    python dev/repro_stall_cv.py --port COM5 --stall 3   # should survive

Capture USB traffic in Wireshark (USBPcap interface) while this runs to get
a before/after comparison against the July 2026 failure signature.
"""
import argparse
import logging
import time

from moles.methods.cv import ExperimentStopped, HostLagError, LivePotentiostat


class StallInjector:
    """Data callback that sleeps once, ``stall_after`` seconds into the run.

    Runs synchronously inside the batch read loop, so the sleep stalls the
    host exactly the way an overloaded GUI does.
    """

    def __init__(self, stall_after_s, stall_s):
        self.stall_after_s = stall_after_s
        self.stall_s = stall_s
        self.t0 = None
        self.fired = False

    def __call__(self, potential_V, current_uA):
        now = time.monotonic()
        if self.t0 is None:
            self.t0 = now
        if not self.fired and (now - self.t0) >= self.stall_after_s:
            self.fired = True
            print(f"\n[repro] >>> injecting a {self.stall_s:.1f}s reader stall NOW <<<\n")
            time.sleep(self.stall_s)


def print_diagnostics(ps, label):
    """Show the firmware health counters, or note that the firmware predates them."""
    try:
        diag = ps.read_diagnostics()
    except Exception as e:
        print(f"[repro] {label}: diagnostics query unavailable ({e.__class__.__name__}) "
              f"— firmware predates it")
        return
    print(f"[repro] {label}: {diag}")
    if diag['watchdog_reset']:
        print("[repro]   !! the device's last reboot was a WATCHDOG reset "
              "(firmware froze mid-run)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True, help="serial port of the potentiostat")
    ap.add_argument("--stall", type=float, default=8.0,
                    help="length of the injected reader stall in seconds (default 8)")
    ap.add_argument("--stall-after", type=float, default=30.0,
                    help="when to inject the stall, seconds into the run (default 30)")
    ap.add_argument("--cycles", type=int, default=10,
                    help="CV cycles; keep the run comfortably longer than the stall point")
    args = ap.parse_args()

    # Show the batch loop's own telemetry (loop rate, backlog) on the console.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    injector = StallInjector(args.stall_after, args.stall)
    ps = LivePotentiostat(serial_port=args.port, data_callback=injector, stop_event=None)
    outcome = "unknown"
    try:
        ps.connect()
        print_diagnostics(ps, "before run")
        ps.write_switch(True)
        t_start = time.monotonic()
        # Small, safe sweep on a dummy cell; 250 Hz to match real experiments.
        ps.perform_CV(min_V=-0.2, max_V=0.2, cycles=args.cycles, mV_s=50,
                      step_hz=250, start_V=0.0, last_V=0.0,
                      scan_direction='Negative')
        outcome = "completed normally in %.1f s" % (time.monotonic() - t_start)
    except HostLagError as e:
        outcome = f"aborted safely by the backlog guard: {e}"
    except ExperimentStopped:
        outcome = "stopped by user"
    except KeyboardInterrupt:
        outcome = "interrupted (Ctrl+C) — halting stream"
        try:
            ps._halt_batch_stream()
        except Exception:
            pass
    except Exception as e:
        outcome = f"FAILED with {e.__class__.__name__}: {e}"
    finally:
        # Fail-state: always try to cut cell power, whatever happened above.
        for step in (lambda: ps.write_switch(False), ps.disconnect):
            try:
                step()
            except Exception:
                pass

    print(f"\n[repro] outcome: {outcome}")
    print("[repro] reconnect and query diagnostics to see how the device coped:")
    print("[repro]   watchdog_reset=True  -> firmware froze and was reset (old behavior)")
    print("[repro]   tx_dropped>0, no reset -> bounded-wait fix worked (new behavior)")
    print("[repro]   starved_steps>0        -> device paused for waveform data instead of")
    print("[repro]                             replaying stale potentials")


if __name__ == "__main__":
    main()

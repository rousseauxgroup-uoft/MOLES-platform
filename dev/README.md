# Developer utilities

Internal scripts used during MOLES development. **Not intended for end
users** — most require a connected potentiostat with a known serial port,
and the demo scripts contain placeholder values that need to be filled in
before they will run.

If you're a chemist trying to run experiments, use the GUI instead:

```bash
moles-electrolysis     # for electrosynthesis runs
moles-electroanalysis  # for cyclic voltammetry, differential pulse, and analysis
```

## What's here

| Script | Purpose |
| --- | --- |
| `demo_constant_potential.py` | Minimal scripted example: connect, hold a potential, then ramp a constant current. Useful as a starting point when writing your own automation around the driver. |
| `demo_multichannel.py` | Same demo, but discovers and connects to multiple potentiostats by serial port. Used to test parallel-channel logic. |
| `diagnose_cv_gain.py` | Logs raw gain indices and ADC readings during a CV to diagnose gain-switching glitches. |
| `diagnose_usb_capture.py` | Offline analyzer for a Wireshark/USBPcap `.pcapng` capture of the USB traffic. Reports where the data stream stops, bus errors, re-enumeration, whether the *host* stopped reading (canceled read pump), and how far the host app fell behind the device (backlog + loop-rate history). No hardware needed; reads a saved capture. |
| `repro_stall_cv.py` | **Hardware-facing.** Reproduces the extended-CV USB failure on demand by injecting a deliberate reader stall mid-CV. Use with a dummy cell, capture with USBPcap, and compare before/after firmware fixes. Never run automatically. |
| `verify_ac_math.py` | Standalone sanity check on the alternating-current waveform math (peak currents, frequency, duty cycle). No hardware needed. |

## The extended-CV failure signature (July 2026)

Root cause of the "Write timeout" / ClearCommError failures on long CV runs,
as established from a full USBPcap capture:

1. The host GUI/worker loop must sustain one iteration per 6 samples
   (41.7 it/s at 250 Hz). Late in long runs it slowed below that, and unread
   data piled up in the Windows serial buffer ("backlog").
2. At ~12 kB of backlog, Windows **canceled the read pump** and stopped
   reading from the device entirely.
3. The firmware's `CDC_Transmit_FS` then blocked forever in an unbounded
   busy-wait (`while (TxState != 0)`), freezing the main task.
4. The independent watchdog (IWDG, 2.05 s) reset the chip: the device dropped
   off the bus (XACT_ERROR), the port close babbled, and the device
   re-enumerated under a new address. The stale COM handle then produced the
   Write timeout / `ClearCommError` `ERROR_BAD_COMMAND` errors in Python.

A separate defect found in the same capture: when the host lags, the firmware
batch stepper reads *stale* waveform bytes instead of pausing, silently
applying ~24 s-old potentials. Fixes for all of this live in the host batch
loop (backlog guard, bigger pre-fill, bounded plot cost) and in the firmware
(bounded transmit waits, starvation guard, reset-cause diagnostics).

In a capture of a healthy run, `diagnose_usb_capture.py` should show no
canceled reads, backlog peaking well under 8 kB, and a loop rate pinned at
the required it/s for the whole run.

### Verifying the fixes (bench procedure, run manually)

1. Flash the updated firmware from `firmware/driver-master/potentiostat/`
   (build in STM32CubeIDE). The new diagnostics query (command 19) reports
   the reset cause and the dropped/starved counters.
2. With a dummy cell attached, run `repro_stall_cv.py --stall 8` while
   capturing with USBPcap.
   * Before the fixes this reproduces the failure: read pump canceled,
     ~2 s pause, XACT_ERROR, re-enumeration, dead COM port.
   * After the fixes the run either aborts cleanly (`HostLagError`, cell
     switched off) or completes; the device stays responsive and
     `read_diagnostics()` shows `watchdog_reset: False`.
3. After any *organic* failure, reconnect and call
   `Potentiostat.read_diagnostics()` before power-cycling — it tells you
   whether the firmware froze (watchdog), dropped data (host stopped
   reading), or starved (host too slow to supply the waveform).

## Before running the demos

Both `demo_*.py` scripts have `"/dev/tty.usbmodemREPLACE…"` placeholder
serial ports. Find your potentiostat's real port with:

```bash
python -m serial.tools.list_ports
```

(or open Device Manager → Ports on Windows). Replace the placeholder strings
with what you find, then run the script.

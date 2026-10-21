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
| `measure_current_ranges.py` | **Hardware-facing.** Measures what each current range (gain index 0-4) really is, using a resistor wired as a dummy cell: the feedback resistance, the largest readable current, the noise of one reading, and the zero-current reading. Compares the results with the known explanations and says which one fits. `--simulate` runs it on a simulated board with no hardware. Never run automatically. |
| `repro_stall_cv.py` | **Hardware-facing.** Reproduces the extended-CV USB failure on demand by injecting a deliberate reader stall mid-CV. Use with a dummy cell, capture with USBPcap, and compare before/after firmware fixes. Never run automatically. |
| `verify_ac_math.py` | Standalone sanity check on the alternating-current waveform math (peak currents, frequency, duty cycle). No hardware needed. |
| `verify_R_u.py` | **Hardware-facing.** Validates `measure_R_u` against a dummy cell of known R_u: a plain resistor, or a resistor in series with a capacitor (`--capacitor`), which decays like a real cell. Reports each measurement's R_u and error, how the steps were made (DAC or switch), the fitted time constant, and any refusal; saves the traces. Never run automatically. |
| `verify_waveform_upload.py` | **Hardware-facing, safe with a cell connected** (the cell switch stays open throughout). Uploads test waveforms into the board's waveform buffer over and over and checks whether they arrive intact, comparing the historical 66-byte uploads with the fixed ones. Run before and after flashing to confirm the random CV/DPV oscillation fix (see below). Never run automatically. |

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


## The random CV/DPV oscillation (September 2026)

Symptom: every so often a CV or DPV suddenly drove the output rail to rail,
in bursts, for up to ~24 s, then carried on normally. Root cause, confirmed
against a recorded episode (board A, 16 Aug, `BC-7-53C_-3.00V_500mVs_..._PARTIAL.csv`):

1. The host uploaded the first ~16 s of the waveform as 66-byte messages,
   which USB carries as a 64-byte and a 2-byte packet.
2. The firmware ends a command frame after 2 ms of silence, but compared a
   timestamp taken at the top of its loop with the packet's arrival time. A
   packet arriving mid-pass made the unsigned difference wrap to ~4e9, so the
   parser ran after the first packet: it stored 62 bytes instead of 64.
3. Every later waveform point then sat 2 bytes off its float boundary. The
   stepper read garbage floats (NaN, 1e-20, 1e+30...) and the DAC turned them
   into rail-to-rail output until the ring wrapped. The first bad sample
   lands exactly at 16 x (message number) + 15; for the recorded episode
   that was 3727 = 16 x 232 + 15, and a byte-level model reproduces the
   recorded output to 97%.

Fixes: host messages now fit one USB packet and name their ring position
(`CMD_BUFFER_WRITE_AT` when the firmware has it); the firmware waits for real
silence, keeps bytes that arrive mid-parse, drops cut-short writes, and
refuses impossible setpoints (disconnecting the cell); the host stops any run
whose measured potential jumps away from the programmed waveform.

### Verifying the fixes (bench procedure, run manually)

1. On the test board with its current firmware, run
   `python dev/verify_waveform_upload.py --board TEST`. The `old-*` modes may
   show `X` (misaligned) uploads; the fixed modes should all be intact.
2. Flash the new image from `firmware/prebuilt/` and run it again: every
   mode should be intact, `old-*` included.
3. Run one CV and one DPV on a dummy resistor as a regression check.

## The current ranges (September 2026)

The board measures current with an amplifier whose feedback resistance sets
the range; the gain index (0-4) picks which resistors a four-channel switch
chip connects. The software converts readings assuming 100 ohm, 1 kohm,
10 kohm, 100 kohm and 1 Mohm. On the boards built from the April/May 2026
PCBWay order the switched resistors are 100 ohm, 1k, 10k and 100k (plus
1 Mohm always connected), and the switch chip (TMUX6212) turns a channel on
when its control pin is HIGH. Firmware before 1.4 has a switch table written
for the opposite logic. Measured on board TEST (27 Sep 2026), and what
firmware 1.4 should give:

| Index | Software assumes | Firmware before 1.4 (measured) | Firmware 1.4 (expected) |
| --- | --- | --- | --- |
| 0 | 100 ohm | 93.0-93.4 ohm (100, 1k, 10k) | ~93 ohm (unchanged) |
| 1 | 1 kohm | 93.7-94.2 ohm (100, 1k, 100k) | ~1.0 kohm (1k) |
| 2 | 10 kohm | 102.5-103.0 ohm (100, 10k, 100k) | ~9.9 kohm (10k) |
| 3 | 100 kohm | 901-902 ohm (1k, 10k, 100k) | ~91 kohm (100k) |
| 4 | 1 Mohm | 92.9-93.3 ohm (all four) | 1 Mohm (base only) |

Index 0 is what every experiment used until now; every onboarded board
calibrates there at 93.1-93.6 ohm, and firmware 1.4 leaves it as it was. With
no current flowing, every range reads a fixed ~57 mV at the amplifier output,
so the driver converts a reading on any range with the board's range-0
calibration scaled by the ratio of the two ranges' resistances.

Firmware 1.5 adds auto-ranging for CV and DPV: the range changes only as a
step begins (so every end-of-step reading is on a settled range), a DPV only
changes range between pulse pairs, and a range is only made more sensitive if
the charging spike after each step would fit it too. The CV and DPV forms'
"Current range" choice is "Auto" by default, or one fixed range. Saved CV and
DPV raw files end with a `Range` column (0 = +/-50 mA ... 4 = +/-5 uA), and
the params file lists the ranges a run used and how often it changed range.

### Measuring them (bench procedure, run manually)

1. Measure two resistors with a multimeter: one near 1 kohm and one near
   100 kohm. Write down the measured values.
2. Close the MOLES apps; the script needs the board to itself.
3. Wire the 1 kohm resistor as a dummy cell: WE lead on one end, RE and CE
   leads together on the other end.
4. Run, with the measured value:
   `python dev/measure_current_ranges.py --resistor 998.7 --board TEST --label before-fix`
5. Repeat with the 100 kohm resistor. It measures any range the first run
   reports as "too sensitive"; ranges it reports as "weak signal" are better
   measured by the 1 kohm run.
6. Each run prints a table and a verdict and saves two CSV files in
   `~/moles_data/range_measurements/`: `..._summary.csv` (one row per range)
   and `..._samples.csv` (every reading).
7. After flashing firmware 1.4 (from `firmware/prebuilt/`), repeat steps 3-5
   with `--label after-fix`. The script prints "Firmware switch table:
   corrected" when it connects, and the verdict should read "The ranges match
   the corrected switch table". With the 1 kohm resistor, range 4 (1 Mohm) is
   reported as too sensitive; the 100 kohm run measures it.

The script touches only the switch, the potential and the gain index, stays
within 2 V and 10 mA across the resistor by default, and always finishes with
the cell switch open. `--simulate as-built` shows what a run looks like
without a board.

## Before running the demos

Both `demo_*.py` scripts have `"/dev/tty.usbmodemREPLACE…"` placeholder
serial ports. Find your potentiostat's real port with:

```bash
python -m serial.tools.list_ports
```

(or open Device Manager → Ports on Windows). Replace the placeholder strings
with what you find, then run the script.

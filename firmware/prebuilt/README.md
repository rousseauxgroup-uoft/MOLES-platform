# Pre-built firmware

Compiled firmware for the open-source potentiostat used by MOLES. If you're
setting up a brand-new potentiostat and don't want to install a C/embedded
toolchain, flash one of these files directly onto the STM32G473 microcontroller.

## Files

- **`potentiostat.bin`** — raw binary. Most flashing tools accept this format.
- **`potentiostat.elf`** — ELF executable with symbols, debug info stripped.
  Some programmers (e.g. STM32CubeProgrammer) accept this directly.

Both files carry the same flash image; `.bin` is just the ELF's loadable
sections laid out flat. If you're unsure which to use, pick `.bin`.

## Provenance

| | |
|---|---|
| Firmware version | 1.8 |
| Source commit | `ee79345` — *Firmware 1.7-1.8: DAC-step R_u capture and on-board iR compensation* |
| Source path | `firmware/driver-master/potentiostat/` |
| Build config | CubeIDE `Debug` (`-Og`), linked against `STM32G473CCTX_FLASH.ld` |
| Toolchain | GNU Tools for STM32 14.3.rel1 (GCC 14.3.1) |
| `potentiostat.bin` | 56,340 bytes, sha256 `de77010001849345badc4d3816162382054c4101d2173ac55e0d1965e7584746` |
| `potentiostat.elf` | sha256 `68bbcac1ef06f06f8ceee1f20769453c627fddfa9b76951142e674e8ae152341` |
| Bench status | 1.4-1.8 run on board TEST (27 Sep 2026): DAC-step R_u 230-249 ohm on a ferrocene cell, and on-board iR compensation at 70 % took its peak separation from 190 to 80 mV |

Flash usage ~54 KB of 256 KB; RAM usage ~111 KB of 128 KB, leaving ~19 KB of
stack headroom below the initial SP at `0x20020000`.

What this image adds over the previous one (1.1, `99e9c05`):

- 1.2: waveform uploads that cannot be misaligned by a split USB packet, and
  a setpoint guard (the random CV/DPV oscillation fix, PR #25).
- 1.3: CV/DPV steps on an absolute millisecond schedule, optional sampling at
  the end of each step, a cell disconnect if waveform data stops arriving
  (opt-in), and timing counters in the diagnostics report.
- 1.4: current ranges 1-4 connect one feedback resistor each (about 1 kohm,
  9.9 kohm, 91 kohm and 1 Mohm) instead of the wrong combinations the old
  switch table gave on these boards; range 0 is unchanged. The diagnostics
  report's capability flags include bit 3 for this.
- 1.5: CV/DPV runs can change current range by themselves (auto-ranging,
  capability bit 4), only as a step begins and, for DPV, only between pulse
  pairs. The old auto-gain logic, which never moved to a more sensitive
  range, is gone; batch readings still carry their range when the host asks.
- 1.6: auto-ranging measures currents from the zero-current level, read as
  the cell connects. 1.5 counted the fixed ~57 mV amplifier offset as
  current, which held it on insensitive ranges for seconds (bench,
  27 Sep 2026).
- 1.7: a second R_u capture command (`CMD_DAC_STEP_CAPTURE`, capability bit
  5) that makes the step by moving the DAC with the cell already connected,
  instead of by closing the switch. Closing the switch lets the control
  amplifier wind up while it is open, and its ~0.5 mA recovery both hid the
  first ~75 us of the transient and pushed charge into the double layer,
  which read R_u high. The new capture also records the zero before the step
  and reports when the first reading after it was taken.
- 1.8: on-board iR compensation for CV (capability bit 6). A batch started
  with the new IR flag adds the ohmic drop back onto each setpoint, from the
  previous step's end-of-step reading, on the board's own clock; before, the
  host did this one point per USB round trip, reading right after each
  potential change. `CMD_IR_CONFIG` (23) sets it up and `CMD_IR_REPORT` (24)
  reports it. The correction is smoothed, walks a limited amount per step,
  and is capped at the output's headroom beyond the sweep. Two conditions end
  the run with the cell disconnected: a runaway (the correction climbing at
  its walking limit while still falling further behind, the signature of an
  overestimated R_u), and a correction or output held at its limit, where
  the board cannot deliver the sweep. Batches without the flag run exactly
  as before, and the diagnostics report keeps its length.

Driver compatibility: every change is additive for the host. Older drivers
send the old commands, run CV/DPV and electrolysis on range 0 (unchanged), and
read a prefix of the longer diagnostics report; newer drivers check the
capability flags before using any new behaviour.

This is the `Debug` build deliberately. Debug and Release differ only in
optimization level — there is no `#ifdef DEBUG` anywhere in `Core/` — and the
Debug (`-Og`) build is the one that has been bench-verified, including the continuous
DAC feed, 500 Hz sampling, and the self-framing `CMD_BUFFER_WRITE_AT` parser.
`-Os` would shift instruction timing across known timing-sensitive USB and
parser paths for a flash saving the device does not need. The image is
flash-linked and built with `--specs=nosys.specs` with no semihosting, so it
runs standalone on power-up with no debugger attached.

## How to flash

1. Download [STM32CubeProgrammer](https://www.st.com/en/development-tools/stm32cubeprog.html)
   (free, from STMicroelectronics).
2. Put the potentiostat into bootloader mode by holding **SW1** while
   connecting it to USB.
3. In STM32CubeProgrammer, connect via USB DFU (you may need to refresh to see the USB device listed).
4. Open `potentiostat.bin`, click **Download** to flash the binary, and then **Verify** to check for a successful transfer.
5. Power-cycle the device by unplugging and replugging its USB connection.

After the power cycle the board enumerates as a USB CDC serial port
(VID `0x0483`, PID `0x5740`, "STM32 Virtual ComPort") and MOLES should connect
to it normally.

### If it enumerates but MOLES won't connect

A serial port appearing is not proof the firmware is good — an image with no
application layer still enumerates, because the USB descriptors live in the
CubeMX-generated code. `PotentiostatDriver.connect()` issues a command and
waits for a reply, so a firmware that can't answer shows up as a 10 s timeout
rather than a missing port. Confirm you flashed a current image by checking its
sha256 against the table above.

## Building from source

To modify the firmware, open the project in
[STM32CubeIDE](https://www.st.com/en/development-tools/stm32cubeide.html):
`File → Open Projects from File System →` then point it at
`firmware/driver-master/potentiostat/`. The IDE will regenerate the `Debug/`
build directory on first compile.

### Regenerating these files

From `firmware/driver-master/potentiostat/Debug/` after a successful build, with
`arm-none-eabi-objcopy` from the CubeIDE toolchain on your `PATH`:

```bash
arm-none-eabi-objcopy -O binary potentiostat_rev_B.elf ../../../prebuilt/potentiostat.bin
```

```bash
arm-none-eabi-objcopy --strip-debug potentiostat_rev_B.elf ../../../prebuilt/potentiostat.elf
```

`--strip-debug` is what keeps the shipped ELF small and free of absolute build
paths from the machine that compiled it, while retaining the symbol table so
the image stays inspectable with `objdump -t`.

Then update the provenance table above — commit hash, sizes, and both hashes.
Keeping it current is what makes a stale image detectable: these files were
previously a years-old skeleton build that had never contained any application
code, and nothing in the folder recorded where they came from.

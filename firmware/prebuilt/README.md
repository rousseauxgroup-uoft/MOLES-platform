# Pre-built firmware

This folder contains the compiled firmware image for the open-source
potentiostat used by MOLES. If you're setting up a brand-new potentiostat
and don't want to install a C/embedded toolchain, flash one of these files
directly onto the STM32G473 microcontroller.

## Files

- **`potentiostat.bin`** — raw binary. Most flashing tools accept this format.
- **`potentiostat.elf`** — ELF executable, includes section headers. Some
  programmers (e.g. STM32CubeProgrammer) accept this directly.

Both files are the same firmware, just packaged differently. If you're
unsure which to use, pick `.bin`.

## How to flash

The simplest path:

1. Download [STM32CubeProgrammer](https://www.st.com/en/development-tools/stm32cubeprog.html)
   (free, from STMicroelectronics).
2. Put the potentiostat into bootloader mode by holding **SW1** while
   connecting it to USB.
3. In STM32CubeProgrammer, connect via USB DFU, then open `potentiostat.bin`
   and click **Download**.
4. Power-cycle the device.

## Building from source

To modify the firmware, open the project in
[STM32CubeIDE](https://www.st.com/en/development-tools/stm32cubeide.html):
`File → Open Projects from File System → ` then point it at
`firmware/driver-master/potentiostat/`. The IDE will regenerate the
`Debug/` and `Release/` build directories on first compile.

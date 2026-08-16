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

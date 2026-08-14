"""Detect MOLES potentiostats connected to the host machine.

The scan iterates over every USB CDC serial device the OS reports, optionally
narrows to those carrying ST Microelectronics' USB vendor ID, and then
fingerprints each candidate by exchanging two read-only commands with the
firmware. Devices that respond with bytes inside the expected ranges are
classified as MOLES potentiostats; everything else is rejected. No write
commands are issued at any point, so probing an unrelated device on the same
machine cannot change its state.
"""

import time
from dataclasses import dataclass
from typing import List, Optional

import serial
from serial.tools import list_ports


# STM32G473 USB CDC firmware ships with ST's vendor ID. PID 0x5740 is the
# default ST Virtual COM Port identifier used by the STM32 USB CDC class.
ST_VENDOR_ID = 0x0483
ST_CDC_PRODUCT_ID = 0x5740

# Wire protocol constants. Mirror the values in ``moles.driver.ps4_ref``;
# duplicated here so discovery has no import-time dependency on the driver
# (which pulls in numpy/serial-heavy modules).
_DEFAULT_DEVICE_ID = 1
_CMD_READ_GAIN = 16
_CMD_READ_SWITCH = 17
_VALID_GAIN_BYTES = frozenset({0, 1, 2, 3, 4})
_VALID_SWITCH_BYTES = frozenset({0, 1})

_BAUD = 115200
# Per-read timeout. Probes either succeed in well under this window or the
# port is not a MOLES device — long timeouts only slow the scan down.
_PROBE_TIMEOUT_S = 0.5
# Delay after opening the port before the first command, matching the wait
# the driver uses on connect to let the STM32 USB UART stack come up.
_UART_INIT_DELAY_S = 1.0


@dataclass
class DetectedDevice:
    """One potentiostat found on the bus during a scan."""
    port: str             # current OS-specific path (e.g. "/dev/tty.usbmodem...", "COM3")
    usb_serial: str       # USB serial number string (stable per board, derived from STM32 chip UID)
    vid: Optional[int]    # USB vendor ID, if reported by the OS
    pid: Optional[int]    # USB product ID, if reported by the OS
    description: str = ""


def list_candidate_ports():
    """Return the USB serial ports worth probing on the current machine.

    If any ports advertise the ST vendor ID, only those are returned — this
    keeps the scan fast and avoids touching unrelated CDC devices (Arduinos,
    FTDI cables, etc.) on a busy machine. If no ST devices are present, all
    CDC ports are returned as a fallback so nonstandard firmware still has a
    chance to be detected.
    """
    all_ports = list(list_ports.comports())
    st_ports = [p for p in all_ports if getattr(p, "vid", None) == ST_VENDOR_ID]
    return st_ports if st_ports else all_ports


def probe_port(port_path: str) -> Optional[str]:
    """Probe one serial port for a MOLES potentiostat.

    Sends two short read-only commands and validates each response byte
    falls within its expected range. On a positive identification, returns
    the device's USB serial number string (read separately from OS metadata
    by the caller). On any failure — port unopenable, timeout, wrong byte
    length, out-of-range value — returns ``None`` and leaves the port closed.
    No write/switch/DAC commands are ever sent.
    """
    ser = None
    try:
        # ``dsrdtr=False`` prevents pyserial from toggling DTR on open, which
        # would reset Arduinos and other DTR-sensitive boards that happen to
        # be plugged into the same machine during a scan.
        # ``exclusive=True`` makes this open fail on a port another MOLES app
        # is using, so a scan can never inject probe commands into the middle
        # of a running experiment's command stream.
        ser = serial.Serial(
            port_path,
            baudrate=_BAUD,
            timeout=_PROBE_TIMEOUT_S,
            dsrdtr=False,
            exclusive=True,
        )
    except (serial.SerialException, OSError):
        return None

    try:
        time.sleep(_UART_INIT_DELAY_S)

        if not _send_and_validate(ser, _CMD_READ_GAIN, _VALID_GAIN_BYTES):
            return None
        if not _send_and_validate(ser, _CMD_READ_SWITCH, _VALID_SWITCH_BYTES):
            return None

        # Both probes passed; the OS-level metadata carries the actual USB
        # serial number, so the caller looks that up via list_ports.
        return port_path
    except (serial.SerialException, OSError):
        return None
    finally:
        try:
            ser.close()
        except Exception:
            pass


def _send_and_validate(ser, cmd_byte: int, valid_bytes) -> bool:
    """Send a 2-byte command and check that the 1-byte reply is in range."""
    try:
        ser.reset_input_buffer()
    except Exception:
        pass
    ser.write(bytes([_DEFAULT_DEVICE_ID, cmd_byte]))
    reply = ser.read(1)
    if len(reply) != 1:
        return False
    return reply[0] in valid_bytes


def scan() -> List[DetectedDevice]:
    """Probe every candidate port and return the MOLES potentiostats found.

    Each detected device carries its current port path, USB serial number,
    and any VID/PID the OS reported. The order of the returned list mirrors
    the order returned by ``list_candidate_ports``; callers that need stable
    ordering across runs (e.g. for table display) should sort by USB serial.
    """
    detected: List[DetectedDevice] = []
    for port_info in list_candidate_ports():
        port_path = port_info.device
        identified = probe_port(port_path)
        if identified is None:
            continue

        usb_serial = (getattr(port_info, "serial_number", None) or "").strip()
        if not usb_serial:
            # Without a stable serial number we can't track this device
            # across reboots — skip rather than write a row that can't be
            # matched on the next scan.
            continue

        detected.append(
            DetectedDevice(
                port=port_path,
                usb_serial=usb_serial,
                vid=getattr(port_info, "vid", None),
                pid=getattr(port_info, "pid", None),
                description=getattr(port_info, "description", "") or "",
            )
        )
    return detected

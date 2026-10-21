"""Naming a board's firmware release from its diagnostics report, noting it
per board, and reading it from an idle board without changing its state."""

import re
import struct
from pathlib import Path

import pytest
import serial

from moles.driver import ps4_ref
from moles.onboarding import discovery, firmware

REPO = Path(__file__).resolve().parents[1]
FIRMWARE_SRC = REPO / "firmware" / "driver-master" / "potentiostat" / "Core" / "Src"


def _report(n_fields, features=None):
    values = [0] * n_fields
    if features is not None:
        values[9] = features
    return dict(zip(firmware.DIAGNOSTIC_FIELDS, values))


@pytest.mark.parametrize("report, version", [
    (None, "1.0"),
    (_report(4), "1.1"),
    (_report(5), "1.2"),
    (_report(10, 0x07), "1.3"),
    (_report(10, 0x0F), "1.4"),
    (_report(10, 0x1F), "1.6"),
    (_report(10, 0x3F), "1.7"),
    (_report(10, 0x7F), "1.8"),
    (_report(10, 0xFF), "newer than 1.8"),
    # Flags no release sends, as from a board still streaming a cut-off run
    (_report(10, 0x00), None),
    (_report(10, 0x03), None),
    (_report(10, 0x5F), None),
    (_report(10, 0x3F800000), None),
])
def test_each_release_is_named_from_its_report(report, version):
    assert firmware.version_from_diagnostics(report) == version


def test_tables_match_the_driver():
    assert firmware.DIAGNOSTIC_FIELDS == ps4_ref.DIAGNOSTIC_FIELDS
    # A release sends its own flag and every flag before it
    for newest, version in ((ps4_ref.FW_FEATURE_IR_COMP, "1.8"),
                            (ps4_ref.FW_FEATURE_DAC_STEP, "1.7"),
                            (ps4_ref.FW_FEATURE_AUTO_RANGE, "1.6"),
                            (ps4_ref.FW_FEATURE_TRUE_RANGES, "1.4"),
                            (ps4_ref.FW_FEATURE_STARVE_FAILSAFE, "1.3")):
        assert firmware.version_from_diagnostics(_report(10, 2 * newest - 1)) == version


def test_the_firmware_source_reports_its_own_version():
    """The flags the firmware in this repo sends must name the version it
    declares. When this fails after a firmware release, that release either
    needs a flag of its own or MOLES cannot tell it from the one before."""
    user_c = (FIRMWARE_SRC / "user.c").read_text(errors="replace")
    major = re.search(r"\.major\s*=\s*(\d+)", user_c).group(1)
    minor = re.search(r"\.minor\s*=\s*(\d+)", user_c).group(1)
    declared = f"{major}.{minor}"

    query_c = (FIRMWARE_SRC / "QueryList.c").read_text(errors="replace")
    bits = {name: int(bit) for name, bit in re.findall(
        r"#define\s+(FW_FEATURE_\w+)\s+\(1u\s*<<\s*(\d+)\)", query_c)}
    expression = re.search(r"response\[9\]\s*=([^;]*);", query_c).group(1)
    flags = 0
    for name in re.findall(r"FW_FEATURE_\w+", expression):
        flags |= 1 << bits[name]

    assert firmware.version_from_diagnostics(_report(10, flags)) == declared
    assert firmware.LATEST_VERSION == declared
    prebuilt = (REPO / "firmware" / "prebuilt" / "README.md").read_text()
    assert re.search(r"\| Firmware version \| ([\d.]+) \|", prebuilt).group(1) == declared


def test_outdated_compares_releases_as_numbers():
    assert firmware.is_outdated("1.2")
    assert firmware.is_outdated("1.0")
    assert not firmware.is_outdated(firmware.LATEST_VERSION)
    assert not firmware.is_outdated("1.10")
    assert not firmware.is_outdated("newer than 1.8")


# --- notes ------------------------------------------------------------------

@pytest.fixture
def notes_dir(tmp_path, monkeypatch):
    path = tmp_path / "firmware"
    monkeypatch.setattr(firmware, "FIRMWARE_DIR", path)
    return path


def test_a_note_round_trips(notes_dir):
    assert firmware.read("SER_A") is None
    firmware.record("SER_A", "1.6")
    firmware.record("SER_A", "1.8")
    note = firmware.read("SER_A")
    assert note["version"] == "1.8"
    assert note["read_at"]
    assert [p.suffix for p in notes_dir.iterdir()] == [".json"]


def test_bad_or_missing_notes_read_as_unknown(notes_dir):
    firmware.record("", "1.8")
    assert not notes_dir.exists()
    notes_dir.mkdir()
    (notes_dir / "SER_B.json").write_text("{not json", encoding="utf-8")
    assert firmware.read("SER_B") is None
    assert firmware.read("") is None


def test_an_unwritable_note_is_not_an_error(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(firmware, "FIRMWARE_DIR", blocker / "firmware")
    firmware.record("SER_A", "1.8")
    assert firmware.read("SER_A") is None


# --- reading an idle board --------------------------------------------------

class _Board:
    """A serial port with a board behind it that answers the switch read and,
    if it has one, the diagnostics query with ``diag_bytes``."""

    def __init__(self, diag_bytes=b"", switch_reply=b"\x00"):
        self.diag_bytes = diag_bytes
        self.switch_reply = switch_reply
        self.timeout = 0.5
        self.writes = []
        self.out = b""
        self.closed = False

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if data == bytes([1, ps4_ref.Commands.READ_SWITCH]):
            self.out += self.switch_reply
        elif data == bytes([1, ps4_ref.Commands.READ_DIAGNOSTICS]):
            self.out += self.diag_bytes
        return len(data)

    def read(self, n):
        chunk, self.out = self.out[:n], self.out[n:]
        return chunk

    def reset_input_buffer(self):
        self.out = b""

    def close(self):
        self.closed = True


def _diag(n_fields, features=0x7F):
    values = [0x0C000000] + [0] * (n_fields - 1)
    if n_fields == 10:
        values[9] = features
    return struct.pack(f"<{n_fields}I", *values)


@pytest.fixture
def board_on_port(monkeypatch):
    boards = {}

    def fake_serial(port, **kwargs):
        if port not in boards:
            raise serial.SerialException("could not open port")
        assert kwargs.get("exclusive") is True
        return boards[port]

    monkeypatch.setattr(discovery.serial, "Serial", fake_serial)
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)
    return boards


@pytest.mark.parametrize("diag, version", [
    (_diag(10), "1.8"),
    (_diag(10, 0x1F), "1.6"),
    (_diag(5), "1.2"),
    (_diag(4), "1.1"),
    (b"", "1.0"),
])
def test_probe_names_the_release(board_on_port, diag, version):
    board = board_on_port["COM3"] = _Board(diag)
    assert discovery.probe_firmware("COM3") == version
    assert board.closed
    # Nothing but the two read commands went to the board
    assert set(board.writes) <= {bytes([1, ps4_ref.Commands.READ_SWITCH]),
                                 bytes([1, ps4_ref.Commands.READ_DIAGNOSTICS])}


def test_probe_rejects_what_it_cannot_identify(board_on_port):
    board_on_port["COM3"] = _Board(_diag(10), switch_reply=b"")
    board_on_port["COM4"] = _Board(_diag(10)[:10])
    board_on_port["COM5"] = _Board(struct.pack("<10f", *range(10)))
    assert discovery.probe_firmware("COM3") is None   # not a MOLES board
    assert discovery.probe_firmware("COM4") is None   # garbled report
    assert discovery.probe_firmware("COM5") is None   # streamed data, not a report
    assert discovery.probe_firmware("COM9") is None   # port in use / gone


# --- the driver notes the version on connect -------------------------------

@pytest.fixture
def driver(notes_dir, monkeypatch):
    monkeypatch.setattr(ps4_ref.firmware_notes, "usb_serial_for_port",
                        lambda port: "SER_P")
    ps = ps4_ref.Potentiostat(serial_port="COM7")
    return ps


@pytest.mark.parametrize("diag, version, features", [
    (_diag(10), "1.8", 0x7F),
    (_diag(5), "1.2", 0),
    (b"", "1.0", 0),
])
def test_connect_notes_the_version(driver, diag, version, features):
    driver.serial = _Board(diag)
    driver._diagnostics_supported = None
    driver._note_firmware()
    assert driver.firmware_version == version
    assert driver.firmware_features() == features
    assert firmware.read("SER_P")["version"] == version


def test_a_garbled_report_notes_nothing(driver):
    driver.serial = _Board(_diag(10, 0x5F))
    driver._diagnostics_supported = None
    driver._note_firmware()
    assert driver.firmware_version is None
    assert firmware.read("SER_P") is None


def test_a_failed_read_notes_nothing(driver):
    class Broken(_Board):
        def write(self, data):
            raise serial.SerialException("device reports readiness but returned no data")

    driver.serial = Broken()
    driver._diagnostics_supported = None
    driver._note_firmware()
    assert driver.firmware_version is None
    assert firmware.read("SER_P") is None


def test_connecting_notes_the_version(driver, monkeypatch):
    board = _Board(_diag(10))
    monkeypatch.setattr(ps4_ref.serial, "Serial", lambda *a, **k: board)
    monkeypatch.setattr(ps4_ref.time, "sleep", lambda s: None)
    # The handshake's own commands are covered elsewhere; here they only
    # have to get out of the way
    for name in ("halt_batch_stream", "write_switch", "write_gain",
                 "write_auto_gain", "read_switch", "read_gain",
                 "read_auto_gain", "write_dac"):
        monkeypatch.setattr(driver, name, lambda *a, **k: None)
    driver.connect()
    assert driver.firmware_version == "1.8"
    assert firmware.read("SER_P")["version"] == "1.8"

"""GUI smoke tests for the MOLES launcher window."""

import json
import os

import pytest

from moles import claims
from moles.onboarding import firmware

# Table columns (mirroring the launcher's COL_* constants)
COL_CAL, COL_FW, COL_STATUS = 2, 3, 4


@pytest.fixture
def present_ports():
    """USB serial -> port map the fake OS reports; tests mutate it to
    simulate plugging and unplugging boards."""
    return {"SER_A": "COM3", "SER_B": "COM4"}


@pytest.fixture
def launcher(qapp, tmp_path, monkeypatch, present_ports):
    """A LauncherWindow backed by a fake registry and a temp claims dir."""
    from moles.onboarding.registry import Entry
    from moles.ui.launcher import main_window as launcher_mod

    monkeypatch.setattr(claims, "CLAIMS_DIR", tmp_path / "claims")
    monkeypatch.setattr(firmware, "FIRMWARE_DIR", tmp_path / "firmware")
    claims._owned_keys.clear()
    assert (launcher_mod.COL_CAL, launcher_mod.COL_FW,
            launcher_mod.COL_STATUS) == (COL_CAL, COL_FW, COL_STATUS)

    entries = [
        Entry(name="A", usb_serial="SER_A", port="COM3", slope=1.0, intercept=0.0),
        Entry(name="B", usb_serial="SER_B", port="COM4"),
    ]
    monkeypatch.setattr(launcher_mod, "load_registry", lambda: entries)
    # claim_key_for reads the real registry file; route it to the fake entries.
    monkeypatch.setattr(
        launcher_mod.claims, "claim_key_for",
        lambda name, port="", registry_path=None: {
            "A": "SER_A", "B": "SER_B"}.get(str(name), str(name)),
    )
    # Presence comes from the fake map, not the real USB bus.
    monkeypatch.setattr(
        launcher_mod, "_connected_usb_serials", lambda: dict(present_ports)
    )

    win = launcher_mod.LauncherWindow()
    yield win
    win.close()


def _write_claim(tmp_path, key, pid, app="electrolysis"):
    d = tmp_path / "claims"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.json").write_text(json.dumps({
        "key": key, "pid": pid, "app": app, "user": "u",
        "experiment": "e", "started": "2026-07-10T09:00:00",
    }), encoding="utf-8")


def test_launcher_lists_registry_boards(launcher):
    assert launcher.table.rowCount() == 2
    assert launcher.table.item(0, 0).text() == "A"
    assert launcher.table.item(0, COL_STATUS).text() == "Free"
    assert launcher.table.item(1, COL_CAL).text() == "no"  # B is uncalibrated


def test_launcher_shows_live_and_stale_claims(launcher, tmp_path):
    _write_claim(tmp_path, "SER_A", os.getppid())   # live foreign process
    _write_claim(tmp_path, "SER_B", 99999999)       # dead pid -> stale
    launcher.refresh()
    assert launcher.table.item(0, COL_STATUS).text().startswith("In use")
    assert launcher.table.item(1, COL_STATUS).text().startswith("STALE")


def test_launcher_shows_orphan_claims(launcher, tmp_path):
    _write_claim(tmp_path, "UNKNOWN_KEY", os.getppid())
    launcher.refresh()
    assert launcher.table.rowCount() == 3
    names = {launcher.table.item(r, 0).text() for r in range(3)}
    assert "?" in names


def test_unplugged_board_shows_not_connected(launcher, present_ports):
    del present_ports["SER_B"]
    launcher.refresh()
    assert launcher.table.item(0, COL_STATUS).text() == "Free"
    assert launcher.table.item(1, COL_STATUS).text() == "Not connected"


def test_claim_outranks_missing_board(launcher, present_ports, tmp_path):
    # A claimed board that disappears from the bus mid-run should keep showing
    # who holds it, not hide the problem behind "Not connected".
    del present_ports["SER_A"]
    _write_claim(tmp_path, "SER_A", os.getppid())
    launcher.refresh()
    assert launcher.table.item(0, COL_STATUS).text().startswith("In use")


def test_force_button_enabled_only_for_claimed_rows(launcher, tmp_path):
    _write_claim(tmp_path, "SER_A", os.getppid())
    launcher.refresh()
    launcher.table.selectRow(0)
    assert launcher.force_btn.isEnabled()
    launcher.table.selectRow(1)
    assert not launcher.force_btn.isEnabled()


def test_firmware_column_shows_noted_versions(launcher):
    firmware.record("SER_A", firmware.LATEST_VERSION)
    firmware.record("SER_B", "1.2")
    launcher.refresh()
    assert launcher.table.item(0, COL_FW).text() == firmware.LATEST_VERSION
    outdated = launcher.table.item(1, COL_FW)
    assert outdated.text() == f"1.2 (update to {firmware.LATEST_VERSION})"
    assert "firmware/prebuilt" in outdated.toolTip()


def test_firmware_column_is_blank_until_read(launcher):
    assert launcher.table.item(0, COL_FW).text() == "—"
    assert "Check Firmware" in launcher.table.item(0, COL_FW).toolTip()


def test_check_firmware_reads_idle_boards_only(launcher, tmp_path, monkeypatch, qapp):
    from moles.ui.launcher import main_window as launcher_mod

    probed = []

    def fake_probe(port):
        # The board is held by the launcher while it is read
        assert claims.read_claim({"COM3": "SER_A", "COM4": "SER_B"}[port]).app == "launcher"
        probed.append(port)
        return "1.6"

    monkeypatch.setattr(launcher_mod, "probe_firmware", fake_probe)
    _write_claim(tmp_path, "SER_B", os.getppid())   # B is in use by an app
    launcher.refresh()
    assert launcher.fw_btn.isEnabled()

    launcher.fw_btn.click()
    assert launcher._fw_thread.wait(5000)
    qapp.processEvents()

    assert probed == ["COM3"]
    assert launcher._fw_thread is None
    assert launcher.table.item(0, COL_FW).text().startswith("1.6")
    assert launcher.table.item(1, COL_FW).text() == "—"
    assert claims.read_claim("SER_A") is None          # released again
    assert claims.read_claim("SER_B").app == "electrolysis"
    assert "A 1.6" in launcher.status_label.text()


def test_check_firmware_skips_a_board_claimed_meanwhile(tmp_path, monkeypatch):
    from moles.ui.launcher import main_window as launcher_mod

    monkeypatch.setattr(claims, "CLAIMS_DIR", tmp_path / "claims")
    monkeypatch.setattr(firmware, "FIRMWARE_DIR", tmp_path / "firmware")
    monkeypatch.setattr(launcher_mod, "probe_firmware",
                        lambda port: pytest.fail("a claimed board was probed"))
    _write_claim(tmp_path, "SER_A", os.getppid())
    assert launcher_mod.check_firmware([("SER_A", "SER_A", "COM3")]) == {"SER_A": None}
    assert firmware.read("SER_A") is None


def test_check_firmware_is_off_with_no_idle_board(launcher, present_ports, tmp_path):
    present_ports.clear()
    launcher.refresh()
    assert not launcher.fw_btn.isEnabled()


def test_every_app_including_data_analysis_can_be_launched():
    import importlib.util
    from moles.ui.launcher.main_window import APPS

    assert dict(APPS)["Data Analysis"] == "moles.ui.data_analysis"
    for _label, module in APPS:
        assert importlib.util.find_spec(module + ".__main__") is not None

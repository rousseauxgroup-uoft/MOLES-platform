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


def test_every_app_has_an_icon_tile_that_launches_it(launcher, monkeypatch):
    from moles.ui.launcher import main_window as launcher_mod
    from moles.ui.visual_identity import APP_ICONS, load_resource_pixmap

    started = []
    monkeypatch.setattr(launcher_mod, "launch_detached", started.append)

    assert list(launcher.app_tiles) == [module for _label, module in launcher_mod.APPS]
    for module, tile in launcher.app_tiles.items():
        icon = load_resource_pixmap(APP_ICONS[module])
        assert not icon.isNull(), f"{APP_ICONS[module]} is missing"
        assert tile._icon.toImage() == icon.toImage()
        tile.click()
    assert started == [module for _label, module in launcher_mod.APPS]
    assert launcher.app_tiles["moles.ui.electroanalysis"].text() == "Electroanalysis"


def test_the_logo_is_level_with_the_middle_of_the_app_icons(launcher, qapp):
    from PyQt5 import QtCore
    from moles.ui.launcher.main_window import APP_ICON_PX, APP_ICON_TOP_PX

    launcher.show()
    qapp.processEvents()
    central = launcher.centralWidget()
    logo = launcher._logo_label
    pix = logo.pixmap()
    logo_mid = (logo.mapTo(central, QtCore.QPoint(0, 0)).y()
                + logo.contentsMargins().top()
                + pix.height() / pix.devicePixelRatio() / 2)
    for tile in launcher.app_tiles.values():
        icon_mid = tile.mapTo(central, QtCore.QPoint(0, APP_ICON_TOP_PX + APP_ICON_PX // 2)).y()
        assert abs(logo_mid - icon_mid) <= 1


def test_an_app_tile_rings_its_icon_when_highlighted(qapp):
    from PyQt5 import QtGui
    from moles.ui.launcher.main_window import APP_RING_COLOR, AppTile
    from moles.ui.visual_identity import load_app_icon_pixmap

    tile = AppTile("Settings", load_app_icon_pixmap("moles.ui.settings"))
    ring_color = QtGui.QColor(APP_RING_COLOR).rgb()

    def ring_pixels():
        image = tile.grab().toImage()
        return sum(image.pixel(x, y) == ring_color
                   for x in range(image.width()) for y in range(image.height()))

    assert ring_pixels() == 0
    tile._highlighted = lambda: True
    assert ring_pixels() > 200


def test_each_app_shows_its_launcher_icon_as_its_own_icon(qapp, monkeypatch):
    """Every app's main() names its package, so its Dock / taskbar icon is its
    launcher button's picture, and each has its own Windows taskbar ID."""
    import inspect
    import importlib
    from moles.ui import visual_identity
    from moles.ui.launcher.main_window import APPS

    packages = [module for _label, module in APPS] + [visual_identity.LAUNCHER]
    assert set(visual_identity.APP_ICONS) == set(packages)
    assert set(visual_identity.APP_USER_MODEL_IDS) == set(packages)
    assert len(set(visual_identity.APP_USER_MODEL_IDS.values())) == len(packages)
    entry = {"moles.ui.settings": "moles.ui.settings.dialog"}
    for package in packages:
        module = importlib.import_module(entry.get(package, f"{package}.main_window"))
        assert f'apply_app_icon(app, "{package}")' in inspect.getsource(module.main)

    class App:
        icon = None

        def setWindowIcon(self, icon):
            self.icon = icon

    app = App()
    visual_identity.apply_app_icon(app, "moles.ui.electrolysis")
    expected = visual_identity.load_resource_pixmap("launcher_electrolysis.png")
    assert app.icon.pixmap(expected.size()).toImage() == expected.toImage()


def test_windows_gets_each_apps_taskbar_id(monkeypatch):
    import ctypes
    from moles.ui import visual_identity

    seen = []

    class Shell32:
        def SetCurrentProcessExplicitAppUserModelID(self, app_id):
            seen.append(app_id)

    class WinDLL:
        shell32 = Shell32()

    monkeypatch.setattr(visual_identity.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", WinDLL(), raising=False)
    visual_identity._set_windows_app_id("moles.ui.data_analysis")
    assert seen == ["MOLES.Platform.DataAnalysis"]

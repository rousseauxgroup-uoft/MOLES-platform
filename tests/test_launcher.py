"""GUI smoke tests for the MOLES launcher window."""

import json
import os

import pytest

from moles import claims


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
    claims._owned_keys.clear()

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
    assert launcher.table.item(0, 3).text() == "Free"
    assert launcher.table.item(1, 2).text() == "no"  # B is uncalibrated


def test_launcher_shows_live_and_stale_claims(launcher, tmp_path):
    _write_claim(tmp_path, "SER_A", os.getppid())   # live foreign process
    _write_claim(tmp_path, "SER_B", 99999999)       # dead pid -> stale
    launcher.refresh()
    assert launcher.table.item(0, 3).text().startswith("In use")
    assert launcher.table.item(1, 3).text().startswith("STALE")


def test_launcher_shows_orphan_claims(launcher, tmp_path):
    _write_claim(tmp_path, "UNKNOWN_KEY", os.getppid())
    launcher.refresh()
    assert launcher.table.rowCount() == 3
    names = {launcher.table.item(r, 0).text() for r in range(3)}
    assert "?" in names


def test_unplugged_board_shows_not_connected(launcher, present_ports):
    del present_ports["SER_B"]
    launcher.refresh()
    assert launcher.table.item(0, 3).text() == "Free"
    assert launcher.table.item(1, 3).text() == "Not connected"


def test_claim_outranks_missing_board(launcher, present_ports, tmp_path):
    # A claimed board that disappears from the bus mid-run should keep showing
    # who holds it, not hide the problem behind "Not connected".
    del present_ports["SER_A"]
    _write_claim(tmp_path, "SER_A", os.getppid())
    launcher.refresh()
    assert launcher.table.item(0, 3).text().startswith("In use")


def test_force_button_enabled_only_for_claimed_rows(launcher, tmp_path):
    _write_claim(tmp_path, "SER_A", os.getppid())
    launcher.refresh()
    launcher.table.selectRow(0)
    assert launcher.force_btn.isEnabled()
    launcher.table.selectRow(1)
    assert not launcher.force_btn.isEnabled()

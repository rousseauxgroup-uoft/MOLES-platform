"""Tests for the cross-process board-claim mechanism (moles.claims)."""

import json
import os
import subprocess
import sys

import pytest

from moles import claims


@pytest.fixture(autouse=True)
def claims_dir(tmp_path, monkeypatch):
    """Point the claims directory at a temp dir so tests never touch ~/.moles."""
    d = tmp_path / "claims"
    monkeypatch.setattr(claims, "CLAIMS_DIR", d)
    # Each test starts with no bookkeeping from previous tests.
    claims._owned_keys.clear()
    yield d
    claims._owned_keys.clear()


def _dead_pid():
    """Return the PID of a process that has already exited."""
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True, text=True, check=True,
    )
    return int(proc.stdout.strip())


def _write_foreign_claim(claims_dir, key, pid, app="electrolysis", experiment="run-1"):
    """Simulate a claim file written by some other process."""
    claims_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "key": key, "pid": pid, "app": app, "user": "someone",
        "host": "labpc", "experiment": experiment, "port": "COM9",
        "started": "2026-07-10T09:14:00",
    }
    (claims_dir / f"{key}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_acquire_creates_claim_and_read_roundtrips(claims_dir):
    claim = claims.acquire("SER123", app="electroanalysis", experiment="cv-scan", port="COM4")
    assert (claims_dir / "SER123.json").is_file()

    read_back = claims.read_claim("SER123")
    assert read_back is not None
    assert read_back.pid == os.getpid()
    assert read_back.app == "electroanalysis"
    assert read_back.experiment == "cv-scan"
    assert read_back.port == "COM4"
    assert read_back.key == claim.key == "SER123"


def test_read_claim_returns_none_when_free():
    assert claims.read_claim("NOPE") is None


def test_acquire_raises_busy_for_live_foreign_claim(claims_dir):
    # The parent process (pytest's launcher or shell) is alive and isn't us.
    live_foreign_pid = os.getppid()
    _write_foreign_claim(claims_dir, "SER123", live_foreign_pid)

    with pytest.raises(claims.ClaimBusyError) as excinfo:
        claims.acquire("SER123", app="electroanalysis")
    # The error message should tell the user who has the board.
    assert "electrolysis" in str(excinfo.value)
    assert "run-1" in str(excinfo.value)


def test_acquire_replaces_stale_claim(claims_dir):
    _write_foreign_claim(claims_dir, "SER123", _dead_pid())

    claim = claims.acquire("SER123", app="electroanalysis")
    assert claim.pid == os.getpid()
    assert claims.read_claim("SER123").app == "electroanalysis"


def test_acquire_by_same_process_refreshes(claims_dir):
    claims.acquire("SER123", app="electrolysis", experiment="first")
    claim = claims.acquire("SER123", app="electrolysis", experiment="second")
    assert claim.experiment == "second"
    assert claims.read_claim("SER123").experiment == "second"


def test_release_removes_own_claim(claims_dir):
    claims.acquire("SER123", app="electrolysis")
    assert claims.release("SER123") is True
    assert claims.read_claim("SER123") is None


def test_release_leaves_foreign_claim_alone(claims_dir):
    _write_foreign_claim(claims_dir, "SER123", os.getppid())
    assert claims.release("SER123") is False
    assert claims.read_claim("SER123") is not None


def test_force_release_removes_any_claim(claims_dir):
    _write_foreign_claim(claims_dir, "SER123", os.getppid())
    assert claims.force_release("SER123") is True
    assert claims.read_claim("SER123") is None


def test_release_all_owned_releases_everything(claims_dir):
    claims.acquire("SER1", app="electrolysis")
    claims.acquire("SER2", app="electrolysis")
    claims.release_all_owned()
    assert claims.read_claim("SER1") is None
    assert claims.read_claim("SER2") is None


def test_list_claims(claims_dir):
    claims.acquire("SER1", app="electrolysis", experiment="a")
    _write_foreign_claim(claims_dir, "SER2", os.getppid())
    all_claims = claims.list_claims()
    assert set(all_claims) == {"SER1", "SER2"}
    assert all_claims["SER2"].app == "electrolysis"


def test_malformed_claim_file_is_treated_as_stale(claims_dir):
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / "SER123.json").write_text("{not json", encoding="utf-8")

    read_back = claims.read_claim("SER123")
    assert read_back is not None
    assert claims.is_stale(read_back)

    # And a fresh acquire should replace it rather than fail.
    claim = claims.acquire("SER123", app="electroanalysis")
    assert claim.pid == os.getpid()


def test_pid_alive_detects_dead_and_live():
    assert claims._pid_alive(os.getpid()) is True
    assert claims._pid_alive(_dead_pid()) is False
    assert claims._pid_alive(0) is False
    assert claims._pid_alive(-5) is False


def test_claim_key_prefers_usb_serial_from_registry(tmp_path):
    registry = tmp_path / "potentiostats.csv"
    registry.write_text(
        "Name,USB Serial,Port,Slope,Intercept,Comments\n"
        "A,206630AE4232,COM3,1.0,0.0,\n",
        encoding="utf-8",
    )
    assert claims.claim_key_for("A", port="COM3", registry_path=registry) == "206630AE4232"


def test_claim_key_falls_back_to_port_then_name(tmp_path):
    missing_registry = tmp_path / "does_not_exist.csv"
    key = claims.claim_key_for("B", port="/dev/tty.usbmodem42", registry_path=missing_registry)
    assert key == "_dev_tty.usbmodem42"
    assert claims.claim_key_for("B", registry_path=missing_registry) == "B"


def test_describe_is_human_readable():
    claim = claims.Claim(
        key="SER1", pid=1234, app="electrolysis", user="benjamin",
        experiment="cyanation-42", started="2026-07-10T09:14:00",
    )
    text = claim.describe()
    assert "electrolysis" in text
    assert "cyanation-42" in text
    assert "benjamin" in text
    assert "09:14" in text

"""Tests for the launcher's update check (moles.updates)."""

import http.server
import json
import sys
import threading

import pytest

from moles import claims, settings, updates


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Settings and claims in a temp dir, no test-mode variable, no network."""
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(claims, "CLAIMS_DIR", tmp_path / "claims")
    monkeypatch.delenv(updates.TEST_VERSION_ENV, raising=False)

    def no_network():
        raise AssertionError("network used")
    monkeypatch.setattr(updates, "fetch_latest_release", no_network)
    return tmp_path


def installed(monkeypatch, tmp_path):
    (tmp_path / updates.INSTALL_MARKER).write_text("{}")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))


def api_json(tag="v0.2.0"):
    return {
        "tag_name": tag,
        "html_url": "https://github.com/x/y/releases/tag/" + tag,
        "body": "Notes",
        "assets": [
            {"name": "MOLES-0.2.0.pkg", "browser_download_url": "https://dl/pkg", "size": 10},
            {"name": "MOLES-Setup-0.2.0.exe", "browser_download_url": "https://dl/exe", "size": 20},
        ],
    }


@pytest.mark.parametrize("a, b, newer", [
    ("v0.2.0", "0.1.0", True),
    ("0.10.0", "0.9.9", True),
    ("0.2", "0.2.0", False),
    ("0.2.0", "0.2.0", False),
    ("0.1.9", "0.2.0", False),
    ("1.0.0rc1", "0.9", True),
])
def test_version_comparison(a, b, newer):
    assert updates.is_newer(a, b) is newer


@pytest.mark.parametrize("platform, asset", [
    ("darwin", "MOLES-0.2.0.pkg"),
    ("win32", "MOLES-Setup-0.2.0.exe"),
    ("linux", ""),
])
def test_release_picks_this_platforms_installer(monkeypatch, platform, asset):
    monkeypatch.setattr(sys, "platform", platform)
    release = updates.release_from_api(api_json())
    assert release.version == "0.2.0"
    assert release.asset_name == asset


def test_release_without_version_tag_is_ignored():
    assert updates.release_from_api({"tag_name": "nightly"}) is None


def test_developer_copies_never_ask_github(isolated):
    assert updates.check_for_update() is None


def test_newer_release_is_offered_once_unless_skipped(isolated, monkeypatch):
    installed(monkeypatch, isolated)
    monkeypatch.setattr(updates, "__version__", "0.1.0")
    monkeypatch.setattr(updates, "fetch_latest_release",
                        lambda: updates.release_from_api(api_json("v0.2.0")))
    assert updates.check_for_update().version == "0.2.0"

    settings.set_skipped_update_version("0.2.0")
    assert updates.check_for_update() is None


def test_same_or_older_release_is_not_offered(isolated, monkeypatch):
    installed(monkeypatch, isolated)
    monkeypatch.setattr(updates, "__version__", "0.2.0")
    monkeypatch.setattr(updates, "fetch_latest_release",
                        lambda: updates.release_from_api(api_json("v0.2.0")))
    assert updates.check_for_update() is None


def test_unreachable_github_means_no_offer(isolated, monkeypatch):
    installed(monkeypatch, isolated)
    monkeypatch.setattr(updates, "fetch_latest_release", lambda: None)
    assert updates.check_for_update() is None


def test_test_mode_pretends_a_release(isolated, monkeypatch):
    monkeypatch.setenv(updates.TEST_VERSION_ENV, "v99.0")
    release = updates.check_for_update()
    assert release.version == "99.0"
    assert release.asset_url == ""  # nothing to download or install


def test_only_live_claims_block_updating(isolated, monkeypatch):
    claims.acquire("BOARD_A", app="electrolysis", experiment="run 1")
    assert len(updates.boards_in_use()) == 1
    monkeypatch.setattr(claims, "is_stale", lambda claim: True)
    assert updates.boards_in_use() == []
    claims.release("BOARD_A")


@pytest.fixture
def file_server():
    """A local web server whose response body the test sets."""
    state = {"body": b"", "length": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            length = state["length"] if state["length"] is not None else len(state["body"])
            self.send_header("Content-Length", str(length))
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/MOLES-Setup.exe"
    yield state
    server.shutdown()


def release_at(url):
    return updates.Release(version="0.2.0", page_url="", asset_name="MOLES-Setup.exe",
                           asset_url=url)


def test_download_saves_the_whole_file(file_server, tmp_path):
    file_server["body"] = b"x" * 600_000
    seen = []
    path = updates.download_installer(release_at(file_server["url"]), tmp_path,
                                      lambda done, total: seen.append((done, total)) or True)
    assert path.read_bytes() == file_server["body"]
    assert seen[-1] == (600_000, 600_000)
    assert not list(tmp_path.glob("*.part"))


def test_cancelled_download_leaves_nothing(file_server, tmp_path):
    file_server["body"] = b"x" * 600_000
    path = updates.download_installer(release_at(file_server["url"]), tmp_path,
                                      lambda done, total: False)
    assert path is None
    assert list(tmp_path.iterdir()) == []


def test_truncated_download_is_an_error(file_server, tmp_path):
    file_server["body"] = b"x" * 1000
    file_server["length"] = 5000
    with pytest.raises(Exception):
        updates.download_installer(release_at(file_server["url"]), tmp_path,
                                   lambda done, total: True)
    assert list(tmp_path.iterdir()) == []


def test_update_refused_while_a_board_is_in_use(qapp, isolated, monkeypatch):
    from PyQt5 import QtWidgets
    from moles.ui.launcher.update_offer import UpdateOffer

    claims.acquire("BOARD_A", app="electroanalysis", experiment="CV")
    warnings = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *args: warnings.append(args[2]))
    window = QtWidgets.QWidget()
    offer = UpdateOffer(window)
    offer._start_update(release_at("http://127.0.0.1:9/never-fetched"))
    assert warnings and "electroanalysis" in warnings[0]
    assert not offer.busy
    claims.release("BOARD_A")


def test_api_json_round_trip_through_fetch(monkeypatch):
    """fetch_latest_release parses what GitHub sends."""
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args):
            return json.dumps(api_json("v0.3.1")).encode()

    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert updates.fetch_latest_release().version == "0.3.1"

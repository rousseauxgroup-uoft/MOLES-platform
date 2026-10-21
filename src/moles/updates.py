"""Finding and fetching newer MOLES releases for copies set up by an installer.

The launcher asks GitHub whether the public MOLES repository has published a
newer release than the one running. Nothing is ever installed without the
user's say-so: the launcher offers the update, and only if the user accepts
does it download that release's installer and open it, so the user still goes
through the installer's own windows.

Only copies installed by the MOLES installers are offered updates. Their
Python environment carries a marker file the installer writes; a developer
checkout (``pip install -e .``) has none and is updated with git instead.

Every network step fails quietly: no internet, a firewall, or GitHub being
down simply means no offer this time.

For testing the offer before any release exists, start the launcher with the
environment variable ``MOLES_TEST_UPDATE`` set to a version number (e.g.
``MOLES_TEST_UPDATE=99.0 moles``): it pretends that version was released.
"""

import json
import logging
import os
import re
import ssl
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import __version__, claims, settings

logger = logging.getLogger(__name__)

# Releases are published from the public repository, which GitHub lets
# anyone query without signing in.
RELEASE_REPO = "rousseauxgroup-uoft/MOLES-platform"
LATEST_RELEASE_API = f"https://api.github.com/repos/{RELEASE_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{RELEASE_REPO}/releases/latest"

# Written by the installers into the root of the Python environment they build.
INSTALL_MARKER = "moles-install.json"

TEST_VERSION_ENV = "MOLES_TEST_UPDATE"

CHECK_TIMEOUT_S = 5
DOWNLOAD_TIMEOUT_S = 30
DOWNLOAD_CHUNK = 256 * 1024


@dataclass
class Release:
    version: str                  # e.g. "0.2.0" (no leading "v")
    page_url: str                 # the release's web page
    notes: str = ""               # release description, as written on GitHub
    asset_name: str = ""          # this platform's installer, if published
    asset_url: str = ""
    asset_size: int = 0


def parse_version(text: str) -> Tuple[int, ...]:
    """(0, 2, 0) from "v0.2.0" / "0.2.0"; stops at the first non-number part,
    so "0.2.0rc1" compares as (0, 2, 0)."""
    parts = []
    for piece in text.strip().lstrip("vV").split("."):
        match = re.match(r"\d+", piece)
        if not match:
            break
        parts.append(int(match.group()))
        if match.end() != len(piece):
            break
    while parts and parts[-1] == 0:
        parts.pop()  # 0.2 == 0.2.0
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def installed_by_installer() -> bool:
    return (Path(sys.prefix) / INSTALL_MARKER).is_file()


def installer_suffix() -> str:
    """File ending of the installer for this operating system ("" if none)."""
    if sys.platform == "win32":
        return ".exe"
    if sys.platform == "darwin":
        return ".pkg"
    return ""


def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # The Python the installer downloads may not know where macOS keeps its
    # trusted certificates; point it at the system bundle explicitly.
    if sys.platform == "darwin" and os.path.isfile("/etc/ssl/cert.pem"):
        context.load_verify_locations("/etc/ssl/cert.pem")
    return context


def _request(url: str, accept: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": f"MOLES/{__version__}",
    })


def release_from_api(data: dict) -> Optional[Release]:
    """Build a Release from GitHub's JSON, picking this platform's installer."""
    tag = data.get("tag_name") or ""
    if not parse_version(tag):
        return None
    release = Release(
        version=tag.lstrip("vV"),
        page_url=data.get("html_url") or RELEASES_PAGE,
        notes=(data.get("body") or "").strip(),
    )
    suffix = installer_suffix()
    for asset in data.get("assets") or []:
        name = asset.get("name") or ""
        if suffix and name.lower().endswith(suffix):
            release.asset_name = name
            release.asset_url = asset.get("browser_download_url") or ""
            release.asset_size = int(asset.get("size") or 0)
            break
    return release


def fetch_latest_release() -> Optional[Release]:
    """The newest published release, or None if it could not be fetched."""
    try:
        request = _request(LATEST_RELEASE_API, "application/vnd.github+json")
        with urllib.request.urlopen(request, timeout=CHECK_TIMEOUT_S,
                                    context=_ssl_context()) as response:
            data = json.load(response)
    except Exception as e:  # offline, firewalled, rate-limited, no releases yet
        logger.info("Update check skipped: %s", e)
        return None
    return release_from_api(data)


def check_for_update() -> Optional[Release]:
    """A release worth offering the user, or None.

    Offered only to installer copies, only when strictly newer than this one,
    and never one the user asked to skip.
    """
    test_version = os.environ.get(TEST_VERSION_ENV, "").strip()
    if test_version:
        return Release(version=test_version.lstrip("vV"), page_url=RELEASES_PAGE,
                       notes="(Test release from MOLES_TEST_UPDATE; nothing will be installed.)")
    if not installed_by_installer():
        return None
    latest = fetch_latest_release()
    if latest is None or not is_newer(latest.version, __version__):
        return None
    if latest.version == settings.get_skipped_update_version():
        return None
    logger.info("MOLES %s is available (running %s)", latest.version, __version__)
    return latest


def boards_in_use() -> List[str]:
    """One line per board an app is actively using, for refusing to update
    mid-experiment. Claims left by crashed apps don't count."""
    return [claim.describe() for claim in claims.list_claims().values()
            if not claims.is_stale(claim)]


def download_folder() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def download_installer(release: Release, dest_dir: Path,
                       progress: Callable[[int, int], bool]) -> Optional[Path]:
    """Download the release's installer into ``dest_dir``.

    ``progress(done_bytes, total_bytes)`` is called as data arrives and
    returns False to cancel. Returns the file, or None if cancelled; raises
    on network errors. A partial file never keeps the final name, so a
    cancelled or broken download can't be mistaken for a complete one.
    """
    target = dest_dir / release.asset_name
    partial = target.with_name(target.name + ".part")
    request = _request(release.asset_url, "application/octet-stream")
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S,
                                    context=_ssl_context()) as response, \
                partial.open("wb") as out:
            total = int(response.headers.get("Content-Length") or release.asset_size or 0)
            done = 0
            cancelled = False
            while True:
                chunk = response.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if not progress(done, total):
                    cancelled = True
                    break
        if cancelled:
            partial.unlink(missing_ok=True)
            return None
        if total and done < total:
            raise OSError(f"Download ended early ({done} of {total} bytes)")
        os.replace(partial, target)
        return target
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def open_installer(path: Path) -> None:
    """Hand the downloaded installer to the operating system to run."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - the user asked for this update
    else:
        subprocess.Popen(["open", str(path)])

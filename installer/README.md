# MOLES installers

The Windows (`MOLES-Setup-<version>.exe`) and macOS (`MOLES-<version>.pkg`)
installers are built by GitHub Actions
([`.github/workflows/build-installers.yml`](../.github/workflows/build-installers.yml)).
Nobody needs to build them by hand.

## How they work

Each installer is small (Windows ~15 MB, macOS ~40 MB). It carries three things:
- [uv](https://docs.astral.sh/uv/), a tool for installing Python
- the MOLES package itself, as a wheel
- the pinned package list, [`requirements.txt`](requirements.txt)

When it runs, it downloads its own private copy of Python
([`python-version.txt`](python-version.txt)) and the exact package versions in
that list, checking each download against its recorded hash. Everything goes
inside the install folder (Windows: `%LOCALAPPDATA%\Programs\MOLES`; macOS:
inside `MOLES.app`). It never touches any other Python on the computer or the
user's `~/.moles` folder. The steps are in
[`windows/install_env.cmd`](windows/install_env.cmd) and
[`macos/install_env.sh`](macos/install_env.sh).

The install also writes a marker file, `moles-install.json`, next to that
Python. The launcher only checks GitHub for updates when the marker is there
([`src/moles/updates.py`](../src/moles/updates.py)), so developer copies
installed with `pip install -e .` are never offered installer updates.

## Making a release

1. Set `version` in `pyproject.toml` (e.g. `0.2.0`) and commit.
2. Push a matching tag, `v0.2.0`, to the **public** repository
   (rousseauxgroup-uoft/MOLES-platform).
3. The workflow takes about 15 minutes. It builds both installers,
   test-installs them on fresh Windows, Apple Silicon and Intel Mac machines,
   and creates a **draft** release.
4. On the repo's Releases page, edit the draft's notes (the launcher shows
   them in its update offer) and click **Publish**. From then on, launchers
   offer the update.

To test a build without releasing, open **Actions → Build installers → Run
workflow**. The installers appear as downloadable Artifacts on that run.

To test the launcher's update offer before any release exists, start it with
`MOLES_TEST_UPDATE=99.0` set in the environment (for example, from a terminal:
`MOLES_TEST_UPDATE=99.0 moles`). It pretends version 99.0 was released.

## Changing package versions

Edit [`constraints.txt`](constraints.txt), the versions MOLES is tested with.
Then run [`lock_requirements.sh`](lock_requirements.sh) (it needs uv). The
script regenerates `requirements.txt` and checks that every package has a
ready-made download for Windows and for both Mac chips. Run the test suite
before releasing.

## Signing (not yet done)

The installers are unsigned, so Windows SmartScreen and macOS Gatekeeper warn
on first run (the main README explains how to get past the warnings). Removing
the warnings needs:
- an Apple Developer ID (US$99/yr), to sign and notarize the `.pkg`
- a Windows code-signing certificate, to sign the `.exe`

Both are extra steps in the workflow.

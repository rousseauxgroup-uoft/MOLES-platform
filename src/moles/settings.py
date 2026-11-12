"""User-editable MOLES settings backed by a small JSON file.

Settings live at ``~/.moles/settings.json`` so they survive package upgrades
and can be inspected or hand-edited if needed. Each call to a getter re-reads
the file, so changes made via the ``moles-settings`` dialog take effect on the
next save without needing to restart any running UI.

Currently exposed:
  - ``data_folder``: where electrolysis CSV files are written.
  - ``cv_data_folder``: where CV / DPV / OCP CSV files are written.
  - ``show_electrolysis_plot_after_run``: whether the post-run plotter window
    is launched automatically when an electrolysis experiment finishes.
  - ``skipped_update_version``: a release the launcher should stop offering.
"""

import json
from pathlib import Path
from typing import Any, Dict


# Location of the settings file. Same parent directory as the potentiostat
# registry so all user-side state for MOLES lives in one place.
SETTINGS_PATH = Path.home() / ".moles" / "settings.json"

# Data folders: one parent folder holding one subfolder per app. The user
# picks the parent the first time the launcher opens (see
# moles.ui.settings.data_folder_prompt); the defaults below are only used
# until then, e.g. if an app is started before the prompt was answered.
DEFAULT_PARENT_FOLDER = Path.home() / "Documents" / "MOLES Data"
ELECTROLYSIS_SUBFOLDER = "Electrolysis Data"
ELECTROANALYSIS_SUBFOLDER = "Electroanalysis Data"
DEFAULT_DATA_FOLDER = DEFAULT_PARENT_FOLDER / ELECTROLYSIS_SUBFOLDER
DEFAULT_CV_DATA_FOLDER = DEFAULT_PARENT_FOLDER / ELECTROANALYSIS_SUBFOLDER

# Where MOLES 1.0 and earlier saved data when no folder was set. Only used to
# offer existing users the choice of continuing to save there.
LEGACY_DATA_FOLDER = Path.home() / "Dropbox" / "EChem Results"
LEGACY_CV_DATA_FOLDER = Path.home() / "Dropbox" / "CV Results"

# Default behaviour: pop the electrolysis plot window when a run ends.
DEFAULT_SHOW_PLOT = True


def _load_raw() -> Dict[str, Any]:
    """Read the settings file, returning ``{}`` if absent or unreadable.

    A corrupt file should not block the user from running an experiment, so
    any parse error falls back to defaults rather than raising.
    """
    if not SETTINGS_PATH.is_file():
        return {}
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_raw(data: Dict[str, Any]) -> None:
    """Write ``data`` to the settings file, creating parent dirs as needed."""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def _read_folder(key: str, default: Path) -> Path:
    """Return a folder setting, falling back to ``default`` if it's missing,
    blank, or not a string.
    """
    raw = _load_raw().get(key)
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return default


def _write_folder(key: str, folder: Path) -> None:
    """Persist a folder setting. The folder itself is created on first save,
    not here, so an unmounted network drive doesn't block updating the path.
    """
    data = _load_raw()
    data[key] = str(Path(folder).expanduser())
    _save_raw(data)


def get_data_folder() -> Path:
    """Folder where electrolysis CSV files are saved."""
    return _read_folder("data_folder", DEFAULT_DATA_FOLDER)


def set_data_folder(folder: Path) -> None:
    _write_folder("data_folder", folder)


def get_cv_data_folder() -> Path:
    """Folder where CV / DPV CSV files are saved."""
    return _read_folder("cv_data_folder", DEFAULT_CV_DATA_FOLDER)


def set_cv_data_folder(folder: Path) -> None:
    _write_folder("cv_data_folder", folder)


def data_folders_configured() -> bool:
    """True once both data folders have been saved to the settings file."""
    raw = _load_raw()
    return all(isinstance(raw.get(k), str) and raw[k].strip()
               for k in ("data_folder", "cv_data_folder"))


def set_data_folders_from_parent(parent: Path) -> None:
    """Save both data folders as the standard subfolders of ``parent``."""
    parent = Path(parent).expanduser()
    data = _load_raw()
    data["data_folder"] = str(parent / ELECTROLYSIS_SUBFOLDER)
    data["cv_data_folder"] = str(parent / ELECTROANALYSIS_SUBFOLDER)
    _save_raw(data)


def legacy_data_folders_exist() -> bool:
    """True if either old default Dropbox folder exists on this computer."""
    return LEGACY_DATA_FOLDER.is_dir() or LEGACY_CV_DATA_FOLDER.is_dir()


def get_show_electrolysis_plot_after_run() -> bool:
    """Whether to open the plot viewer when an electrolysis run completes."""
    raw = _load_raw().get("show_electrolysis_plot_after_run")
    if isinstance(raw, bool):
        return raw
    return DEFAULT_SHOW_PLOT


def set_show_electrolysis_plot_after_run(value: bool) -> None:
    """Persist the post-run plot toggle."""
    data = _load_raw()
    data["show_electrolysis_plot_after_run"] = bool(value)
    _save_raw(data)


def get_skipped_update_version() -> str:
    """The release the user chose "Skip This Version" for, or "" if none."""
    raw = _load_raw().get("skipped_update_version")
    return raw if isinstance(raw, str) else ""


def set_skipped_update_version(version: str) -> None:
    data = _load_raw()
    data["skipped_update_version"] = str(version)
    _save_raw(data)

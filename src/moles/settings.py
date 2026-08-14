"""User-editable MOLES settings backed by a small JSON file.

Settings live at ``~/.moles/settings.json`` so they survive package upgrades
and can be inspected or hand-edited if needed. Each call to a getter re-reads
the file, so changes made via the ``moles-settings`` dialog take effect on the
next save without needing to restart any running UI.

Currently exposed:
  - ``data_folder``: where electrolysis CSV files are written.
  - ``cv_data_folder``: where CV / DPV CSV files are written.
  - ``show_electrolysis_plot_after_run``: whether the post-run plotter window
    is launched automatically when an electrolysis experiment finishes.
"""

import json
from pathlib import Path
from typing import Any, Dict


# Location of the settings file. Same parent directory as the potentiostat
# registry so all user-side state for MOLES lives in one place.
SETTINGS_PATH = Path.home() / ".moles" / "settings.json"

# Default folders for each experiment type. Match the original hardcoded
# values so existing users see no change until they pick a different folder.
DEFAULT_DATA_FOLDER = Path.home() / "Dropbox" / "EChem Results"
DEFAULT_CV_DATA_FOLDER = Path.home() / "Dropbox" / "CV Results"

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

"""One-time prompt asking where MOLES should save experimental data.

Shown when the settings file has no data folders yet: on a new install, and
once for users upgrading from a version that silently defaulted to Dropbox.
The user picks one parent folder; MOLES saves electrolysis runs to its
"Electrolysis Data" subfolder and CV / DPV / OCP runs to its "Electroanalysis
Data" subfolder. Users whose old Dropbox folders exist may instead keep
saving there. Either folder can be changed later in Settings.
"""

from pathlib import Path

from PyQt5 import QtCore, QtWidgets

from ...settings import (
    DEFAULT_PARENT_FOLDER,
    ELECTROANALYSIS_SUBFOLDER,
    ELECTROLYSIS_SUBFOLDER,
    LEGACY_CV_DATA_FOLDER,
    LEGACY_DATA_FOLDER,
    data_folders_configured,
    legacy_data_folders_exist,
    set_cv_data_folder,
    set_data_folder,
    set_data_folders_from_parent,
)


def default_parent_folder() -> Path:
    """The suggested parent folder: "MOLES Data" in the user's Documents.

    Asks Qt for the Documents folder because on Windows it is often moved
    (e.g. into OneDrive), so ``~/Documents`` may not be the one the user sees.
    """
    docs = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.DocumentsLocation)
    if docs:
        return Path(docs) / DEFAULT_PARENT_FOLDER.name
    return DEFAULT_PARENT_FOLDER


class DataFolderPrompt(QtWidgets.QDialog):
    """Ask for the parent data folder (or to keep the old Dropbox folders)."""

    def __init__(self, parent=None, offer_legacy=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Where MOLES Saves Data")
        self.setMinimumWidth(560)
        if offer_legacy is None:
            offer_legacy = legacy_data_folders_exist()
        self._offer_legacy = offer_legacy
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(12)

        intro = QtWidgets.QLabel(
            "MOLES saves every run as a CSV file. Choose a folder for your "
            "data. MOLES creates two folders inside it: "
            f"<b>{ELECTROLYSIS_SUBFOLDER}</b> and "
            f"<b>{ELECTROANALYSIS_SUBFOLDER}</b> (CV, DPV and OCP).<br><br>"
            "You can change either folder later in Settings."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        if self._offer_legacy:
            self.new_radio = QtWidgets.QRadioButton("Use this folder:")
            layout.addWidget(self.new_radio)
        else:
            self.new_radio = None

        row = QtWidgets.QHBoxLayout()
        self.folder_edit = QtWidgets.QLineEdit(str(default_parent_folder()))
        row.addWidget(self.folder_edit, stretch=1)
        self.browse_btn = QtWidgets.QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._browse)
        row.addWidget(self.browse_btn)
        layout.addLayout(row)

        if self._offer_legacy:
            self.keep_radio = QtWidgets.QRadioButton(
                "Keep saving to the folders this computer already uses:")
            layout.addWidget(self.keep_radio)
            legacy = QtWidgets.QLabel(
                f"Electrolysis: {LEGACY_DATA_FOLDER}\n"
                f"CV, DPV and OCP: {LEGACY_CV_DATA_FOLDER}")
            legacy.setStyleSheet("color: #555; margin-left: 22px;")
            legacy.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            layout.addWidget(legacy)
            # Existing users keep their current behaviour unless they choose
            # otherwise.
            self.keep_radio.setChecked(True)
            self.new_radio.toggled.connect(self._sync_enabled)
            self._sync_enabled()
        else:
            self.keep_radio = None

        layout.addStretch(1)
        buttons = QtWidgets.QDialogButtonBox()
        save = buttons.addButton("Save", QtWidgets.QDialogButtonBox.AcceptRole)
        save.setDefault(True)
        buttons.addButton("Ask Me Later", QtWidgets.QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _sync_enabled(self) -> None:
        on = self.new_radio.isChecked()
        self.folder_edit.setEnabled(on)
        self.browse_btn.setEnabled(on)

    def _browse(self) -> None:
        start = Path(self.folder_edit.text().strip() or str(default_parent_folder()))
        start = start.expanduser()
        # Start in the nearest folder that exists, since the suggested
        # "MOLES Data" folder usually does not exist yet.
        while not start.is_dir() and start != start.parent:
            start = start.parent
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose folder for MOLES data", str(start))
        if chosen:
            self.folder_edit.setText(chosen)

    def keep_legacy_selected(self) -> bool:
        return self.keep_radio is not None and self.keep_radio.isChecked()

    def _on_save(self) -> None:
        try:
            if self.keep_legacy_selected():
                set_data_folder(LEGACY_DATA_FOLDER)
                set_cv_data_folder(LEGACY_CV_DATA_FOLDER)
            else:
                text = self.folder_edit.text().strip()
                if not text:
                    QtWidgets.QMessageBox.warning(
                        self, "No folder chosen", "Please choose a folder.")
                    return
                parent = Path(text).expanduser()
                # Create the folders now, so a location that cannot be
                # written to is reported here rather than when a run saves.
                for sub in (ELECTROLYSIS_SUBFOLDER, ELECTROANALYSIS_SUBFOLDER):
                    (parent / sub).mkdir(parents=True, exist_ok=True)
                set_data_folders_from_parent(parent)
        except OSError as e:
            QtWidgets.QMessageBox.critical(
                self, "Could not use this folder",
                f"MOLES could not create or save this folder:\n{e}")
            return
        self.accept()


def ensure_data_folders(parent=None) -> bool:
    """Show the prompt if no data folders are saved yet.

    Returns True if folders are configured afterwards. If the user picks
    "Ask Me Later", nothing is saved, the prompt returns next launch, and runs
    meanwhile save under the default "MOLES Data" folder in the home folder's
    Documents.
    """
    if data_folders_configured():
        return True
    dlg = DataFolderPrompt(parent)
    dlg.exec_()
    return data_folders_configured()

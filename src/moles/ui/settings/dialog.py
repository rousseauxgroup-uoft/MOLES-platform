"""Settings dialog for the ``moles-settings`` command.

A small standalone window that lets the user pick where experimental data
is saved and toggle whether the electrolysis plot pops up after a run. The
values are read from and written to ``moles.settings``; nothing else in
this module persists state of its own.
"""

import sys
from pathlib import Path

from PyQt5 import QtCore, QtWidgets

from ..visual_identity import apply_app_icon

# moles/ui/__init__.py runs automatically when this subpackage is imported,
# applying the macOS Qt plugin fix before QApplication is created.

from ...settings import (
    DEFAULT_CV_DATA_FOLDER,
    DEFAULT_DATA_FOLDER,
    SETTINGS_PATH,
    get_cv_data_folder,
    get_data_folder,
    get_show_electrolysis_plot_after_run,
    set_cv_data_folder,
    set_data_folder,
    set_show_electrolysis_plot_after_run,
)


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MOLES Settings")
        self.setMinimumWidth(560)

        self._build_ui()
        self._load_current_values()

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(14)

        # --- Folder rows: one for electrolysis, one for CV / DPV. ---
        self.elec_folder_edit = self._add_folder_row(
            layout,
            label="Folder for electrolysis data:",
            placeholder=DEFAULT_DATA_FOLDER,
        )
        self.cv_folder_edit = self._add_folder_row(
            layout,
            label="Folder for CV / DPV data:",
            placeholder=DEFAULT_CV_DATA_FOLDER,
        )

        folder_hint = QtWidgets.QLabel(
            "CSV files are written to these folders; each is created on "
            "first save if it doesn't exist."
        )
        folder_hint.setWordWrap(True)
        folder_hint.setStyleSheet("color: #555;")
        layout.addWidget(folder_hint)

        # --- Plot toggle ---
        self.show_plot_check = QtWidgets.QCheckBox(
            "Open plot window automatically when an electrolysis run finishes"
        )
        layout.addWidget(self.show_plot_check)

        # --- Footer: settings file path + buttons ---
        path_label = QtWidgets.QLabel(f"Settings file: {SETTINGS_PATH}")
        path_label.setStyleSheet("color: #888; font-size: 11px;")
        path_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(path_label)

        layout.addStretch(1)

        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        button_box.accepted.connect(self._on_save)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _add_folder_row(
        self,
        layout: QtWidgets.QVBoxLayout,
        *,
        label: str,
        placeholder: Path,
    ) -> QtWidgets.QLineEdit:
        """Build a "label + line edit + Browse…" row and return its line edit."""
        layout.addWidget(QtWidgets.QLabel(label))

        row = QtWidgets.QHBoxLayout()
        line_edit = QtWidgets.QLineEdit()
        line_edit.setPlaceholderText(str(placeholder))
        row.addWidget(line_edit, stretch=1)

        browse_btn = QtWidgets.QPushButton("Browse…")
        browse_btn.clicked.connect(lambda: self._browse_into(line_edit, placeholder))
        row.addWidget(browse_btn)
        layout.addLayout(row)
        return line_edit

    def _load_current_values(self) -> None:
        self.elec_folder_edit.setText(str(get_data_folder()))
        self.cv_folder_edit.setText(str(get_cv_data_folder()))
        self.show_plot_check.setChecked(get_show_electrolysis_plot_after_run())

    def _browse_into(self, line_edit: QtWidgets.QLineEdit, fallback: Path) -> None:
        # Start the picker in the currently-displayed folder if it exists,
        # so the user doesn't have to navigate from scratch.
        start = line_edit.text().strip() or str(fallback)
        start_path = Path(start).expanduser()
        if not start_path.exists():
            start_path = Path.home()

        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose folder", str(start_path)
        )
        if chosen:
            line_edit.setText(chosen)

    def _on_save(self) -> None:
        elec_text = self.elec_folder_edit.text().strip()
        cv_text = self.cv_folder_edit.text().strip()
        if not elec_text or not cv_text:
            QtWidgets.QMessageBox.warning(
                self, "Missing folder",
                "Please pick a folder for both electrolysis and CV data.",
            )
            return

        try:
            set_data_folder(Path(elec_text))
            set_cv_data_folder(Path(cv_text))
            set_show_electrolysis_plot_after_run(self.show_plot_check.isChecked())
        except OSError as e:
            QtWidgets.QMessageBox.critical(
                self, "Could not save settings",
                f"Failed to write {SETTINGS_PATH}:\n{e}",
            )
            return

        self.accept()


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    apply_app_icon(app)
    dlg = SettingsDialog()
    dlg.exec_()
    app.quit()

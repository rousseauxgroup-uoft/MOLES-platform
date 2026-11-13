"""Top-level window for the unified MOLES electroanalysis interface.

Four tabs sit side by side: a CV control tab, a DPV control tab, an OCP
monitoring tab, and a Data Preview tab. Every finished run is handed straight to
the Data Preview tab so a result can be inspected without reloading it from disk;
the Data Preview tab keeps voltammograms and OCP traces on separate plots, since
they are read against different axes.
"""

import sys

from PyQt5 import QtCore, QtWidgets

# Importing moles.ui applies the macOS Qt plugin fix before QApplication exists.
from .analysis_tab import AnalysisTab
from .control_tab import ControlTab
from ..visual_identity import apply_app_icon
from ..settings.data_folder_prompt import ensure_data_folders
from ..wheel_guard import install_wheel_guard
from ...logging_setup import install_qt_message_handler, setup_logging
from ...settings import apply_interface_scale


class MainWindow(QtWidgets.QMainWindow):
    """Tabbed window combining CV control, DPV control, OCP monitoring, and
    analysis."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MOLES Electroanalysis")
        self.resize(1280, 820)

        tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(tabs)

        self.cv_tab = ControlTab('CV')
        self.dpv_tab = ControlTab('DPV')
        self.ocp_tab = ControlTab('OCP')
        self.analysis_tab = AnalysisTab()

        tabs.addTab(self.cv_tab, "Cyclic Voltammetry")
        tabs.addTab(self.dpv_tab, "Differential Pulse")
        tabs.addTab(self.ocp_tab, "Open Circuit Potential")
        tabs.addTab(self.analysis_tab, "Data Preview")
        self.tabs = tabs

        # Send finished runs to the Data Preview tab automatically.
        self.cv_tab.run_finished.connect(self.analysis_tab.add_dataset_from_file)
        self.dpv_tab.run_finished.connect(self.analysis_tab.add_dataset_from_file)
        self.ocp_tab.run_finished.connect(self.analysis_tab.add_dataset_from_file)


def main():
    """Launch the MOLES electroanalysis interface."""
    # File logging + exception hooks (see moles.logging_setup): keeps every
    # thread off the console and stops PyQt's fatal-abort on slot exceptions.
    setup_logging("electroanalysis")
    install_qt_message_handler()

    if hasattr(QtCore.Qt, 'AA_EnableHighDpiScaling'):
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    if hasattr(QtCore.Qt, 'AA_UseHighDpiPixmaps'):
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)

    apply_interface_scale()
    app = QtWidgets.QApplication(sys.argv)
    apply_app_icon(app, "moles.ui.electroanalysis")
    install_wheel_guard(app)
    window = MainWindow()
    window.show()
    # Ask where to save data if no folder is set yet (new install, or an
    # upgrade from a version that defaulted to Dropbox). Queued so the
    # prompt opens over the window rather than before it.
    QtCore.QTimer.singleShot(0, lambda: ensure_data_folders(window))
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

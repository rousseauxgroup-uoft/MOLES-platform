"""Top-level window for the unified MOLES electroanalysis interface.

Four tabs sit side by side: a CV control tab, a DPV control tab, an OCP
monitoring tab, and an Analysis tab. Every finished run is handed straight to
the Analysis tab so a result can be inspected without reloading it from disk;
the Analysis tab keeps voltammograms and OCP traces on separate plots, since
they are read against different axes.
"""

import sys

from PyQt5 import QtCore, QtWidgets

# Importing moles.ui applies the macOS Qt plugin fix before QApplication exists.
from .analysis_tab import AnalysisTab
from .control_tab import ControlTab
from ..visual_identity import apply_app_icon
from ...logging_setup import install_qt_message_handler, setup_logging


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
        tabs.addTab(self.analysis_tab, "Analysis")
        self.tabs = tabs

        # Send finished runs to the Analysis tab automatically.
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

    app = QtWidgets.QApplication(sys.argv)
    apply_app_icon(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

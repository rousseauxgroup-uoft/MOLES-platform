"""The main window's plot area: plot windows as tabs, any of which can pop out.

A popped-out plot is an ordinary top-level window (it can sit on another
screen, behind the main window, or in Mission Control). Closing it, or its
Dock Back button, returns it to the tabs; only the tab's close button (after
confirming) closes a plot for good.
"""

from PyQt5 import QtCore, QtWidgets

from ...analysis.model import AnalysisSession, PlotWindowState
from .plot_panel import PlotPanel

_DEFAULT_POPOUT_SIZE = (1100, 720)
_POPOUT_CASCADE_PX = 40

_PAGE_EMPTY, _PAGE_TABS = 0, 1


class PopOutWindow(QtWidgets.QMainWindow):
    """A top-level window holding one popped-out plot."""

    dock_requested = QtCore.pyqtSignal(object)  # the plot panel

    def __init__(self, panel, parent=None):
        super().__init__(parent, QtCore.Qt.Window)
        self.panel = panel
        self._released = False
        self.setCentralWidget(panel)
        panel.show()
        self.update_title()

    def update_title(self):
        self.setWindowTitle(f"{self.panel.title} — MOLES Data Analysis")

    def release(self):
        """Give the plot back, so the window can go without taking the plot with it."""
        self._released = True
        return self.takeCentralWidget()

    def closeEvent(self, event):
        if self._released:
            event.accept()
            return
        # Closing a popped-out plot docks it back rather than discarding it.
        event.ignore()
        QtCore.QTimer.singleShot(0, lambda: self.dock_requested.emit(self.panel))


class PlotArea(QtWidgets.QStackedWidget):
    """Every plot window of the open workspace, docked as tabs or popped out."""

    changed = QtCore.pyqtSignal()                            # a plot was edited, added or closed
    view_changed = QtCore.pyqtSignal()                       # zoom, selection, popping out
    selected_file_changed = QtCore.pyqtSignal(object)        # workspace path, or None
    outside_files_dropped = QtCore.pyqtSignal(object, list)  # panel, paths from outside

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = None
        self.panels = []
        self._popouts = {}

        empty = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(empty)
        layout.addStretch(1)
        self._empty_label = QtWidgets.QLabel()
        self._empty_label.setAlignment(QtCore.Qt.AlignCenter)
        self._empty_label.setStyleSheet("color: gray;")
        layout.addWidget(self._empty_label)
        new_btn = QtWidgets.QPushButton("New Plot")
        new_btn.clicked.connect(lambda: self.new_plot())
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        row.addWidget(new_btn)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(2)
        self.addWidget(empty)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setMovable(True)
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(lambda i: self.close_plot(self.tabs.widget(i)))
        self.tabs.tabBarDoubleClicked.connect(self._rename_tab)
        # The plot brought forward speaks for the selection.
        self.tabs.currentChanged.connect(self._on_tab_changed)
        plus = QtWidgets.QToolButton()
        plus.setText("+")
        plus.setToolTip("New plot window")
        plus.setAutoRaise(True)
        plus.clicked.connect(lambda: self.new_plot())
        self.tabs.setCornerWidget(plus, QtCore.Qt.TopRightCorner)
        self.addWidget(self.tabs)
        self._update_page()

    # ------------------------------------------------------------------
    # Plots
    # ------------------------------------------------------------------

    def set_store(self, store):
        self.store = store

    def _next_title(self):
        taken = {p.title for p in self.panels}
        n = 1
        while f"Plot {n}" in taken:
            n += 1
        return f"Plot {n}"

    def new_plot(self, state=None):
        """Open a plot window (empty, or showing a saved ``PlotWindowState``)."""
        state = state or PlotWindowState(self._next_title())
        panel = PlotPanel(state, self.store)
        panel.changed.connect(self.changed)
        panel.view_changed.connect(self.view_changed)
        panel.selection_changed.connect(self.selected_file_changed)
        panel.title_changed.connect(self._on_title_changed)
        panel.pop_toggled.connect(self.toggle_pop)
        panel.outside_files_dropped.connect(self.outside_files_dropped)
        self.panels.append(panel)
        self.tabs.addTab(panel, state.title)
        self.tabs.setCurrentWidget(panel)
        if state.popped_out:
            self.pop_out(panel)
        self._update_page()
        self.changed.emit()
        return panel

    def current_panel(self):
        """The plot the user is looking at: a popped-out one if it is the active window."""
        active = QtWidgets.QApplication.activeWindow()
        # Only a pop-out this area still holds: one just docked back or closed
        # can briefly remain the "active" window.
        if isinstance(active, PopOutWindow) and self._popouts.get(active.panel) is active:
            return active.panel
        return self.tabs.currentWidget()

    def close_plot(self, panel, confirm=True):
        if panel not in self.panels:
            return False
        if confirm and panel.state.experiments and not panel.confirm(
                "Close Plot", f"Close {panel.title}? Its experiments, offsets and "
                              "labels will be lost unless the session is saved."):
            return False
        window = self._popouts.pop(panel, None)
        if window is not None:
            window.release()
            window.hide()
            window.deleteLater()
        else:
            self.tabs.removeTab(self.tabs.indexOf(panel))
        self.panels.remove(panel)
        panel.setParent(None)
        panel.deleteLater()
        self._update_page()
        self.changed.emit()
        return True

    def close_all(self):
        for panel in list(self.panels):
            self.close_plot(panel, confirm=False)

    def is_popped_out(self, panel):
        return panel in self._popouts

    def popout_window(self, panel):
        return self._popouts.get(panel)

    # ------------------------------------------------------------------
    # Popping out and docking back
    # ------------------------------------------------------------------

    def toggle_pop(self, panel):
        if panel in self._popouts:
            self.dock_back(panel)
        else:
            self.pop_out(panel)

    def pop_out(self, panel):
        index = self.tabs.indexOf(panel)
        if index < 0:
            return
        self.tabs.removeTab(index)
        window = PopOutWindow(panel, self.window())
        window.dock_requested.connect(self.dock_back)
        if panel.state.geometry:
            window.setGeometry(*panel.state.geometry)
        else:
            window.resize(*_DEFAULT_POPOUT_SIZE)
            offset = _POPOUT_CASCADE_PX * (1 + len(self._popouts))
            origin = self.window().geometry().topLeft()
            window.move(origin.x() + offset, origin.y() + offset)
        self._popouts[panel] = window
        panel.set_popped_out(True)
        window.show()
        window.raise_()
        self._update_page()
        self.view_changed.emit()

    def dock_back(self, panel):
        window = self._popouts.pop(panel, None)
        if window is None:
            return
        g = window.geometry()
        panel.state.geometry = (g.x(), g.y(), g.width(), g.height())
        window.release()
        window.hide()
        window.deleteLater()
        panel.set_popped_out(False)
        self.tabs.addTab(panel, panel.title)
        self.tabs.setCurrentWidget(panel)
        self._update_page()
        self.view_changed.emit()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def to_session(self):
        """Every plot window as a session: docked ones in tab order, then popped-out ones."""
        docked = [self.tabs.widget(i) for i in range(self.tabs.count())]
        popped = [p for p in self.panels if p in self._popouts]
        for panel in docked + popped:
            panel.capture_legend()
        for panel in popped:
            g = self._popouts[panel].geometry()
            panel.state.geometry = (g.x(), g.y(), g.width(), g.height())
        current = self.tabs.currentWidget()
        return AnalysisSession(windows=[p.state for p in docked + popped],
                               active_window=current.state.uid if current else None)

    def load(self, session):
        """Replace every plot window with the windows of a session."""
        self.close_all()
        for state in session.windows:
            self.new_plot(state)
        for panel in self.panels:
            if panel.state.uid == session.active_window and panel not in self._popouts:
                self.tabs.setCurrentWidget(panel)

    def retry_missing(self):
        return sum(panel.retry_missing() for panel in self.panels)

    def missing_files(self):
        return [f for panel in self.panels for f in panel.missing_files()]

    def close_popouts(self):
        """Close popped-out windows for good (the main window is closing)."""
        for window in list(self._popouts.values()):
            window.release()
            window.close()
        self._popouts.clear()

    # ------------------------------------------------------------------
    # Titles and pages
    # ------------------------------------------------------------------

    def _on_tab_changed(self, index):
        panel = self.tabs.widget(index)
        if panel is not None:
            self.selected_file_changed.emit(panel.selected_file())

    def _rename_tab(self, index):
        panel = self.tabs.widget(index)
        if panel is not None:
            self.rename(panel)

    def rename(self, panel):
        title, ok = QtWidgets.QInputDialog.getText(self, "Rename Plot", "Plot name:",
                                                   text=panel.title)
        if ok and title.strip():
            panel.set_title(title.strip())

    def _on_title_changed(self, panel):
        index = self.tabs.indexOf(panel)
        if index >= 0:
            self.tabs.setTabText(index, panel.title)
        window = self._popouts.get(panel)
        if window is not None:
            window.update_title()

    def _update_page(self):
        if self.tabs.count():
            self.setCurrentIndex(_PAGE_TABS)
            return
        popped = len(self._popouts)
        if popped == 1:
            text = "1 plot is open in its own window."
        elif popped:
            text = f"{popped} plots are open in their own windows."
        else:
            text = "No plot windows open."
        self._empty_label.setText(text)
        self.setCurrentIndex(_PAGE_EMPTY)

    # ------------------------------------------------------------------
    # Workspace files
    # ------------------------------------------------------------------

    def uses(self, rel):
        return any(panel.uses(rel) for panel in self.panels)

    def files_moved(self, pairs):
        for panel in self.panels:
            panel.files_moved(pairs)

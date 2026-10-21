"""Top-level window of the MOLES Data Analysis app.

The Data Explorer docks on the left and stays visible, so files can always be
dragged from it onto a plot. Plot windows fill the rest of the window as tabs
(see ``plot_area``); a new workspace starts with one empty plot.

Until a workspace is open the window offers to choose one. The last workspace
used reopens on the next launch, as long as it still qualifies (see
``moles.analysis.workspace``), and with it the session last open in it.

A session (``moles.analysis.sessions``) saves every plot window. Edits to
plots mark the session unsaved; zooming, panning, selecting and popping out
are saved with it but do not on their own count as unsaved work. Closing the
window, switching workspace, or opening another session asks before
unsaved changes are lost.
"""

import logging
import sys
from pathlib import Path

from PyQt5 import QtCore, QtWidgets

# Importing moles.ui applies the macOS Qt plugin fix before QApplication exists.
from ...analysis import sessions
from ...analysis.store import DatasetStore
from ...analysis.workspace import Workspace, WorkspaceError, file_sha256, is_data_file
from ...logging_setup import install_qt_message_handler, setup_logging
from ..visual_identity import apply_app_icon
from .explorer import DataExplorer, reveal_in_file_manager
from .plot_area import PlotArea

logger = logging.getLogger(__name__)

# Starting width of the Data Explorer, wide enough for most MOLES run names.
_EXPLORER_WIDTH_PX = 380

# Window layout and the last workspace, kept with the rest of MOLES' user state.
SETTINGS_PATH = Path.home() / ".moles" / "data_analysis.ini"

_KEY_LAST_WORKSPACE = "workspace/last"
_KEY_GEOMETRY = "window/geometry"
_KEY_STATE = "window/state"

_PAGE_WELCOME, _PAGE_WORKSPACE = 0, 1

SAVE, DISCARD, CANCEL = "save", "discard", "cancel"


class DataAnalysisWindow(QtWidgets.QMainWindow):
    """Data Explorer dock plus the plot area, for one workspace at a time."""

    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings or QtCore.QSettings(str(SETTINGS_PATH),
                                                     QtCore.QSettings.IniFormat)
        self.workspace = None
        self.store = None
        self.session_name = None   # None until the session is first saved
        self._dirty = False
        self._loading = False
        self.resize(1400, 880)

        self.explorer = DataExplorer()
        self.explorer.files_moved.connect(self._on_files_moved)
        self.explorer.files_removed.connect(self._on_files_removed)
        self.explorer.open_requested.connect(self._open_in_current_plot)
        self.explorer.files_added.connect(self._on_files_added)
        self.explorer_dock = QtWidgets.QDockWidget("Data Explorer", self)
        self.explorer_dock.setObjectName("data_explorer")
        self.explorer_dock.setWidget(self.explorer)
        self.explorer_dock.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable
                                       | QtWidgets.QDockWidget.DockWidgetFloatable
                                       | QtWidgets.QDockWidget.DockWidgetClosable)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.explorer_dock)

        self._pages = QtWidgets.QStackedWidget()
        self._pages.addWidget(self._build_welcome())
        self._pages.addWidget(self._build_plot_area())
        self.setCentralWidget(self._pages)

        self._build_menus()
        self._restore_layout()
        self._update_title()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_welcome(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.addStretch(1)
        title = QtWidgets.QLabel("No workspace open")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        title.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(title)
        text = QtWidgets.QLabel(
            "A workspace is a folder where MOLES keeps copies of the data you\n"
            "analyse. Choose an empty folder to start one, or a folder you have\n"
            "used as a workspace before. Your original data files are never changed.")
        text.setAlignment(QtCore.Qt.AlignCenter)
        text.setStyleSheet("color: gray;")
        layout.addWidget(text)
        button = QtWidgets.QPushButton("Choose Workspace Folder…")
        button.clicked.connect(self.choose_workspace)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(2)
        return page

    def _build_plot_area(self):
        self.plot_area = PlotArea()
        self.plot_area.outside_files_dropped.connect(self._add_outside_files)
        self.plot_area.changed.connect(self._mark_dirty)
        self.plot_area.selected_file_changed.connect(self.explorer.show_file)
        self.explorer.in_use = self.plot_area.uses
        return self.plot_area

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("&File")
        self._session_actions = []
        for text, slot, keys in (("New Session", self.new_session, "Ctrl+Shift+N"),
                                 ("Open Session…", self.open_session_dialog, "Ctrl+Shift+O"),
                                 ("Save Session", self.save_session, "Ctrl+S"),
                                 ("Save Session As…", self.save_session_as, "Ctrl+Shift+S")):
            action = file_menu.addAction(text, slot)
            action.setShortcut(keys)
            action.setEnabled(False)
            self._session_actions.append(action)
        file_menu.addSeparator()
        open_action = file_menu.addAction("Open Workspace…", self.choose_workspace)
        open_action.setShortcut("Ctrl+O")
        self._reveal_action = file_menu.addAction(
            "Show Workspace in Finder" if sys.platform == "darwin"
            else "Show Workspace in File Manager",
            lambda: reveal_in_file_manager(self.workspace.root))
        self._reveal_action.setEnabled(False)
        file_menu.addSeparator()
        quit_action = file_menu.addAction("Quit", self.close)
        quit_action.setShortcut("Ctrl+Q")

        plot_menu = self.menuBar().addMenu("&Plot")
        self._new_plot_action = plot_menu.addAction("New Plot Window",
                                                    lambda: self.plot_area.new_plot())
        self._new_plot_action.setShortcut("Ctrl+N")
        self._new_plot_action.setEnabled(False)

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.explorer_dock.toggleViewAction())

    def _restore_layout(self):
        geometry = self.settings.value(_KEY_GEOMETRY)
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = self.settings.value(_KEY_STATE)
        if state is None or not self.restoreState(state):
            self.resizeDocks([self.explorer_dock], [_EXPLORER_WIDTH_PX], QtCore.Qt.Horizontal)

    def _update_title(self):
        title = "MOLES Data Analysis"
        if self.workspace is not None:
            title += f" — {self.workspace.name} — {self.session_name or 'Unsaved session'}[*]"
        self.setWindowTitle(title)
        self.setWindowModified(self._dirty)

    # ------------------------------------------------------------------
    # Workspace
    # ------------------------------------------------------------------

    def choose_workspace(self):
        last = self.settings.value(_KEY_LAST_WORKSPACE)
        start = str(Path(last).parent) if last else str(Path.home())
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose an Empty Folder or an Existing Workspace", start)
        if folder:
            self.open_workspace(folder)

    def open_workspace(self, folder, quiet=False):
        """Open ``folder`` as the workspace; returns whether it could be used.

        A refused folder is explained in a message box, or only logged when
        ``quiet`` (reopening the last workspace at launch).
        """
        try:
            workspace = Workspace.open(folder)
        except (WorkspaceError, OSError) as e:
            if quiet:
                logger.warning("Not reopening workspace %s: %s", folder, e)
            else:
                QtWidgets.QMessageBox.warning(self, "Cannot Use This Folder", str(e))
            return False
        if not self.maybe_save():
            return False
        self.plot_area.close_all()
        self.workspace = workspace
        self.store = DatasetStore(workspace)
        self.plot_area.set_store(self.store)
        self.explorer.set_workspace(workspace)
        self._pages.setCurrentIndex(_PAGE_WORKSPACE)
        self._reveal_action.setEnabled(True)
        self._new_plot_action.setEnabled(True)
        for action in self._session_actions:
            action.setEnabled(True)
        self.explorer_dock.show()
        self.settings.setValue(_KEY_LAST_WORKSPACE, str(workspace.root))
        logger.info("Opened workspace %s", workspace.root)
        last = sessions.last_session_name(workspace)
        if last is None or not self.load_session(last, quiet=True):
            self._start_new_session()
        return True

    def restore_last_workspace(self):
        last = self.settings.value(_KEY_LAST_WORKSPACE)
        if last and Path(last).is_dir():
            return self.open_workspace(last, quiet=True)
        return False

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def _mark_dirty(self):
        if not self._loading and self.workspace is not None and not self._dirty:
            self._dirty = True
            self._update_title()

    def _set_clean(self):
        self._dirty = False
        self._update_title()

    def _start_new_session(self):
        self._loading = True
        try:
            self.plot_area.close_all()
            self.plot_area.new_plot()
        finally:
            self._loading = False
        self.session_name = None
        self._set_clean()

    def new_session(self):
        if self.workspace is not None and self.maybe_save():
            self._start_new_session()

    def open_session_dialog(self):
        if self.workspace is None:
            return
        saved = sessions.list_sessions(self.workspace)
        if not saved:
            QtWidgets.QMessageBox.information(self, "Open Session",
                                              "This workspace has no saved sessions yet.")
            return
        name = self.choose_session(saved)
        if name and name != self.session_name and self.maybe_save():
            self.load_session(name)

    def load_session(self, name, quiet=False):
        """Replace the plot windows with a saved session's; returns whether it loaded.

        Experiments whose file moved are pointed at it again; ones whose file
        is gone stay in the session and are listed for the user.
        """
        try:
            session = sessions.load_session(self.workspace, name)
        except WorkspaceError as e:
            if quiet:
                logger.warning("Could not reopen session %s: %s", name, e)
            else:
                QtWidgets.QMessageBox.warning(self, "Cannot Open Session", str(e))
            return False
        report = sessions.relink(self.workspace, session)
        self._loading = True
        try:
            self.plot_area.load(session)
            if not self.plot_area.panels:
                self.plot_area.new_plot()
        finally:
            self._loading = False
        self.session_name = name
        sessions.remember_last_session(self.workspace, name)
        # Files found at new places change what would be saved.
        self._dirty = bool(report.relinked)
        self._update_title()
        if report.relinked or report.missing:
            self.report_relink(report)
        logger.info("Opened session %s", name)
        return True

    def save_session(self):
        """Save to the current session's file (asking for a name the first time)."""
        if self.workspace is None:
            return False
        if self.session_name is None:
            return self.save_session_as()
        return self._write_session(self.session_name)

    def save_session_as(self):
        if self.workspace is None:
            return False
        name = self.ask_session_name(self.session_name or self._free_session_name())
        if not name:
            return False
        try:
            path = sessions.session_path(self.workspace, name)
        except WorkspaceError as e:
            QtWidgets.QMessageBox.warning(self, "Cannot Use This Name", str(e))
            return False
        if path.exists() and name != self.session_name and not self.confirm_overwrite(name):
            return False
        return self._write_session(name)

    def _write_session(self, name):
        try:
            sessions.save_session(self.workspace, name, self.plot_area.to_session())
        except (OSError, WorkspaceError) as e:
            QtWidgets.QMessageBox.warning(self, "Session Not Saved", str(e))
            return False
        self.session_name = name
        self._set_clean()
        logger.info("Saved session %s", name)
        return True

    def _free_session_name(self):
        taken = {s.name for s in sessions.list_sessions(self.workspace)}
        n = 1
        while f"Session {n}" in taken:
            n += 1
        return f"Session {n}"

    def maybe_save(self):
        """Offer to save unsaved changes; False if the user cancels what they were doing."""
        if self.workspace is None or not self._dirty:
            return True
        answer = self.ask_save_changes()
        if answer == SAVE:
            return self.save_session()
        return answer == DISCARD

    # --- Dialogs (replaceable in tests) ---

    def ask_save_changes(self):
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("Unsaved Changes")
        box.setText(f"Save the changes to {self.session_name or 'this new session'}?")
        box.setInformativeText("Plot windows, offsets and labels that are not saved are lost.")
        box.setStandardButtons(QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard
                               | QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(QtWidgets.QMessageBox.Save)
        answer = box.exec_()
        return {QtWidgets.QMessageBox.Save: SAVE,
                QtWidgets.QMessageBox.Discard: DISCARD}.get(answer, CANCEL)

    def ask_session_name(self, default):
        name, ok = QtWidgets.QInputDialog.getText(self, "Save Session", "Session name:",
                                                  text=default)
        return name.strip() if ok and name.strip() else None

    def confirm_overwrite(self, name):
        return self._confirm("Replace Session", f"A session called {name} exists. Replace it?")

    def choose_session(self, saved):
        dialog = SessionPicker(saved, self)
        return dialog.chosen() if dialog.exec_() == QtWidgets.QDialog.Accepted else None

    def report_relink(self, report):
        lines = []
        if report.relinked:
            lines.append("Found at a new place in the workspace:")
            lines += [f"  • {old} → {new}" for old, new in report.relinked]
        if report.missing:
            lines.append("No longer in the workspace (kept in the session; they reappear "
                         "once the files are added again):")
            lines += [f"  • {f}" for f in report.missing]
        QtWidgets.QMessageBox.information(self, "Session Files", "\n".join(lines))

    def _confirm(self, title, text):
        answer = QtWidgets.QMessageBox.question(
            self, title, text, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return answer == QtWidgets.QMessageBox.Yes

    # ------------------------------------------------------------------
    # Between the Data Explorer and the plots
    # ------------------------------------------------------------------

    def _on_files_moved(self, pairs):
        self.store.moved(pairs)
        self.plot_area.files_moved(pairs)

    def _on_files_added(self, _rels):
        # A file added back may be one a session's experiment was missing.
        self.plot_area.retry_missing()

    def _on_files_removed(self, rels):
        self.store.forget(rels)

    def _open_in_current_plot(self, rels):
        panel = self.plot_area.current_panel() or self.plot_area.new_plot()
        panel.add_files(rels)

    def _add_outside_files(self, panel, paths):
        """Plot files dropped from outside the workspace, through workspace copies.

        A file whose content is already in the workspace uses that copy;
        anything else is copied into the workspace's top folder first.
        """
        rels, problems = [], []
        for p in map(Path, paths):
            if p.is_dir():
                problems.append(f"{p.name}: add folders through the Data Explorer.")
                continue
            if not is_data_file(p.name):
                problems.append(f"{p.name}: not a .csv data file.")
                continue
            try:
                if self.workspace.contains(p):
                    rels.append(self.workspace.rel(p))
                    continue
                existing = self.workspace.find_by_hash(file_sha256(p))
                rels.append(existing[0] if existing else self.workspace.add_file(p).rel)
            except (WorkspaceError, OSError) as e:
                problems.append(f"{p.name}: {e}")
        self.explorer.refresh()
        if problems:
            panel.show_error("Some files were not added", "\n".join(problems))
        if rels:
            panel.add_files(rels)

    def closeEvent(self, event):
        if not self.maybe_save():
            event.ignore()
            return
        self.settings.setValue(_KEY_GEOMETRY, self.saveGeometry())
        self.settings.setValue(_KEY_STATE, self.saveState())
        self.plot_area.close_popouts()
        super().closeEvent(event)


class SessionPicker(QtWidgets.QDialog):
    """Lists a workspace's saved sessions, newest first."""

    def __init__(self, saved, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open Session")
        self.resize(420, 320)
        layout = QtWidgets.QVBoxLayout(self)
        self.list = QtWidgets.QListWidget()
        for info in saved:
            item = QtWidgets.QListWidgetItem(
                f"{info.name}    (saved {info.modified:%Y-%m-%d %H:%M})")
            item.setData(QtCore.Qt.UserRole, info.name)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Open
                                             | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def chosen(self):
        item = self.list.currentItem()
        return item.data(QtCore.Qt.UserRole) if item else None


def main():
    """Launch the MOLES Data Analysis app."""
    setup_logging("data_analysis")
    install_qt_message_handler()

    if hasattr(QtCore.Qt, 'AA_EnableHighDpiScaling'):
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    if hasattr(QtCore.Qt, 'AA_UseHighDpiPixmaps'):
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)

    app = QtWidgets.QApplication(sys.argv)
    apply_app_icon(app)
    window = DataAnalysisWindow()
    window.show()
    window.restore_last_workspace()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

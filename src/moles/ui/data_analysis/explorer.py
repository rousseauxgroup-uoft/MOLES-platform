"""The Data Explorer: a workspace's folders and data files as a tree.

Everything here acts on the copies inside the workspace, through
``moles.analysis.workspace``; original data files are only ever read, when
they are added. Files dropped in from Finder (or Explorer on Windows) are
copied, never moved. Dragging a file out of the tree carries its workspace path
(under ``WORKSPACE_MIME``) so plot windows know which copy it is.

Params files travel with their data file and are not shown; neither is the
workspace's hidden ``.moles_analysis`` folder.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

from PyQt5 import QtCore, QtGui, QtWidgets

from ...analysis import datasets
from ...analysis.workspace import (
    DuplicateFileError, WorkspaceError, is_data_file,
)

logger = logging.getLogger(__name__)

# MIME type carrying a JSON list of workspace paths in a drag.
WORKSPACE_MIME = "application/x-moles-workspace-files"

REL_ROLE = QtCore.Qt.UserRole
DIR_ROLE = QtCore.Qt.UserRole + 1

# Most files a message box lists before summarising the rest.
_LIST_LIMIT = 10

# Pause after a change on disk before the tree is rebuilt, so a burst of
# changes (a folder copied in, say) rebuilds it once.
_REFRESH_DELAY_MS = 150


def workspace_paths_from_mime(mime):
    """Workspace paths dragged out of a Data Explorer, or [] if there are none."""
    if not mime.hasFormat(WORKSPACE_MIME):
        return []
    try:
        rels = json.loads(bytes(mime.data(WORKSPACE_MIME)).decode("utf-8"))
    except ValueError:
        return []
    return [r for r in rels if isinstance(r, str)]


def move_to_trash(path):
    """Send a file or folder to the system Trash (Recycle Bin on Windows)."""
    ok, _where = QtCore.QFile.moveToTrash(str(path))
    return bool(ok)


def reveal_in_file_manager(path):
    """Show ``path`` selected in Finder / Explorer, or open its folder elsewhere."""
    path = Path(path)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
            return
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
            return
    except OSError:
        logger.exception("Could not reveal %s", path)
    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path.parent)))


def _parent(rel):
    """Workspace path of the folder holding ``rel`` ('' for the workspace root)."""
    parent = PurePosixPath(rel).parent.as_posix()
    return "" if parent == "." else parent


def _bullets(lines):
    shown = [f"  • {line}" for line in lines[:_LIST_LIMIT]]
    if len(lines) > _LIST_LIMIT:
        shown.append(f"  … and {len(lines) - _LIST_LIMIT} more")
    return "\n".join(shown)


class WorkspaceTree(QtWidgets.QTreeWidget):
    """The tree itself: drags out carry workspace paths, drops in become signals.

    Dropping never lets Qt move items on its own: a drop from this tree asks
    for a move inside the workspace, a drop from elsewhere asks for a copy in.
    """

    internal_drop = QtCore.pyqtSignal(list, str)  # workspace paths, target folder
    external_drop = QtCore.pyqtSignal(list, str)  # absolute paths, target folder

    def __init__(self, parent=None):
        super().__init__(parent)
        self.workspace_root = None
        self.tooltip_for = None  # callable: workspace path -> tooltip text
        self.setHeaderHidden(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDrop)
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditKeyPressed
                             | QtWidgets.QAbstractItemView.SelectedClicked)
        self.setUniformRowHeights(True)
        # Keep the selection as visible when the tree is not focused: it also
        # shows which file the selected trace on a plot came from.
        palette = self.palette()
        for role in (QtGui.QPalette.Highlight, QtGui.QPalette.HighlightedText):
            palette.setColor(QtGui.QPalette.Inactive, role,
                             palette.color(QtGui.QPalette.Active, role))
        self.setPalette(palette)
        # MOLES run names end in a timestamp, which tells runs apart: keep both ends.
        self.setTextElideMode(QtCore.Qt.ElideMiddle)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)

    # --- Dragging out ---

    def mimeTypes(self):
        return [WORKSPACE_MIME, "text/uri-list"]

    def mimeData(self, items):
        rels = [item.data(0, REL_ROLE) for item in items]
        mime = QtCore.QMimeData()
        mime.setData(WORKSPACE_MIME, json.dumps(rels).encode("utf-8"))
        if self.workspace_root is not None:
            mime.setUrls([QtCore.QUrl.fromLocalFile(str(self.workspace_root / r))
                          for r in rels])
        return mime

    def startDrag(self, supported_actions):
        items = self.selectedItems()
        if not items:
            return
        drag = QtGui.QDrag(self)
        drag.setMimeData(self.mimeData(items))
        # Offer only a copy: whatever receives the drag (a plot, Finder) must
        # never take the file away, and Qt must not delete the dragged rows.
        drag.exec_(QtCore.Qt.CopyAction)

    # --- Dropping in ---

    def _accepts(self, mime):
        return mime.hasFormat(WORKSPACE_MIME) or mime.hasUrls()

    def dragEnterEvent(self, event):
        if self._accepts(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._accepts(event.mimeData()):
            event.setDropAction(QtCore.Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        folder = self.folder_at(event.pos())
        mime = event.mimeData()
        rels = workspace_paths_from_mime(mime) if event.source() is self else []
        if rels:
            signal, payload = self.internal_drop, rels
        elif mime.hasUrls():
            signal = self.external_drop
            payload = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        else:
            event.ignore()
            return
        event.setDropAction(QtCore.Qt.CopyAction)
        event.accept()
        # Let the drag finish before any dialog the drop may raise appears.
        QtCore.QTimer.singleShot(0, lambda: signal.emit(payload, folder))

    def folder_at(self, pos):
        """The workspace folder a drop at ``pos`` lands in."""
        item = self.itemAt(pos)
        if item is None:
            return ""
        if item.data(0, DIR_ROLE):
            return item.data(0, REL_ROLE)
        parent = item.parent()
        return parent.data(0, REL_ROLE) if parent is not None else ""

    # --- Tooltips, built only when asked for ---

    def viewportEvent(self, event):
        if event.type() == QtCore.QEvent.ToolTip and self.tooltip_for is not None:
            item = self.itemAt(event.pos())
            if item is None:
                QtWidgets.QToolTip.hideText()
            else:
                QtWidgets.QToolTip.showText(event.globalPos(),
                                            self.tooltip_for(item.data(0, REL_ROLE)),
                                            self.viewport())
            return True
        return super().viewportEvent(event)


class DataExplorer(QtWidgets.QWidget):
    """The workspace's file tree with its add / organise / remove actions.

    ``in_use`` is a callable the main window sets: given a workspace path, it
    says whether an open plot shows that file, which blocks removing it.
    """

    files_added = QtCore.pyqtSignal(list)    # workspace paths
    files_moved = QtCore.pyqtSignal(list)    # (old, new) workspace paths
    files_removed = QtCore.pyqtSignal(list)  # workspace paths
    open_requested = QtCore.pyqtSignal(list)  # workspace paths double-clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self.workspace = None
        self.in_use = lambda rel: False
        self.move_to_trash = move_to_trash
        self._refreshing = False
        self._shown_file = None  # file highlighted for a plot's selected trace
        self._kinds = {}  # workspace path -> ((size, mtime), FileKind)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self.path_label = QtWidgets.QLabel()
        self.path_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.path_label)

        buttons = QtWidgets.QHBoxLayout()
        self.add_files_btn = QtWidgets.QPushButton("Add Files…")
        self.add_files_btn.setToolTip("Copy data files into the workspace. "
                                      "The originals are never changed.")
        self.add_files_btn.clicked.connect(self.add_files_dialog)
        self.add_folder_btn = QtWidgets.QPushButton("Add Folder…")
        self.add_folder_btn.setToolTip("Copy every data file in a folder into the workspace.")
        self.add_folder_btn.clicked.connect(self.add_folder_dialog)
        self.new_folder_btn = QtWidgets.QPushButton("New Folder")
        self.new_folder_btn.clicked.connect(self.new_folder)
        for btn in (self.add_files_btn, self.add_folder_btn, self.new_folder_btn):
            buttons.addWidget(btn)
        layout.addLayout(buttons)

        self.tree = WorkspaceTree()
        self.tree.tooltip_for = self.tooltip_for
        self.tree.internal_drop.connect(self.move_into)
        self.tree.external_drop.connect(self.add_paths)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.tree, stretch=1)

        hint = QtWidgets.QLabel("Drag files onto a plot to show them.")
        hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(hint)

        self._remove_action = QtWidgets.QAction("Remove from Workspace…", self)
        self._remove_action.setShortcuts([QtGui.QKeySequence.Delete,
                                          QtGui.QKeySequence(QtCore.Qt.Key_Backspace)])
        # Only while the tree itself has focus, never inside a name being edited.
        self._remove_action.setShortcutContext(QtCore.Qt.WidgetShortcut)
        self._remove_action.triggered.connect(self.remove_selected)
        self.tree.addAction(self._remove_action)

        self._watcher = QtCore.QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._schedule_refresh)
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(_REFRESH_DELAY_MS)
        self._refresh_timer.timeout.connect(self.refresh)

        self.set_workspace(None)

    # ------------------------------------------------------------------
    # Workspace and tree
    # ------------------------------------------------------------------

    def set_workspace(self, workspace):
        self.workspace = workspace
        self._kinds.clear()
        self.tree.workspace_root = workspace.root if workspace else None
        enabled = workspace is not None
        for widget in (self.add_files_btn, self.add_folder_btn, self.new_folder_btn, self.tree):
            widget.setEnabled(enabled)
        self.path_label.setText(workspace.name if workspace else "No workspace open.")
        self.path_label.setToolTip(str(workspace.root) if workspace else "")
        self.refresh()

    def _schedule_refresh(self, *_args):
        self._refresh_timer.start()

    def refresh(self):
        """Rebuild the tree from disk, keeping what was expanded and selected."""
        expanded, selected = set(), set()
        for item in self._all_items():
            rel = item.data(0, REL_ROLE)
            if item.isExpanded():
                expanded.add(rel)
            if item.isSelected():
                selected.add(rel)
        current = self.tree.currentItem()
        current_rel = current.data(0, REL_ROLE) if current else None

        self._refreshing = True
        try:
            self.tree.clear()
            watched = self._watcher.directories()
            if watched:
                self._watcher.removePaths(watched)
            if self.workspace is None:
                return
            folders = [self.workspace.root]
            self._fill(self.tree.invisibleRootItem(), self.workspace.root, folders)
            self._watcher.addPaths([str(f) for f in folders])
            for item in self._all_items():
                rel = item.data(0, REL_ROLE)
                if rel in expanded:
                    item.setExpanded(True)
                if rel in selected:
                    item.setSelected(True)
                if rel == current_rel:
                    self.tree.setCurrentItem(item, 0, QtCore.QItemSelectionModel.NoUpdate)
        finally:
            self._refreshing = False

    def _fill(self, parent_item, folder, folders):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        dirs = sorted((e for e in entries if e.is_dir() and not e.name.startswith(".")),
                      key=lambda e: e.name.lower())
        files = sorted((e for e in entries if e.is_file() and is_data_file(e.name)),
                       key=lambda e: e.name.lower())
        style = self.style()
        for entry in dirs:
            item = self._make_item(parent_item, Path(entry.path), True,
                                   style.standardIcon(QtWidgets.QStyle.SP_DirIcon))
            folders.append(Path(entry.path))
            self._fill(item, Path(entry.path), folders)
        for entry in files:
            item = self._make_item(parent_item, Path(entry.path), False,
                                   style.standardIcon(QtWidgets.QStyle.SP_FileIcon))
            kind = self._kind(item.data(0, REL_ROLE))
            if kind is not None and not kind.supported:
                item.setForeground(0, QtGui.QBrush(QtCore.Qt.gray))

    def _make_item(self, parent_item, path, is_dir, icon):
        item = QtWidgets.QTreeWidgetItem(parent_item, [path.name])
        item.setData(0, REL_ROLE, self.workspace.rel(path))
        item.setData(0, DIR_ROLE, is_dir)
        item.setIcon(0, icon)
        flags = (QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable
                 | QtCore.Qt.ItemIsEditable | QtCore.Qt.ItemIsDragEnabled)
        if is_dir:
            flags |= QtCore.Qt.ItemIsDropEnabled
        item.setFlags(flags)
        return item

    def _all_items(self):
        it = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while it.value() is not None:
            yield it.value()
            it += 1

    def item_for(self, rel):
        for item in self._all_items():
            if item.data(0, REL_ROLE) == rel:
                return item
        return None

    def _kind(self, rel):
        """What a workspace file is, remembered until the file changes."""
        try:
            path = self.workspace.path(rel)
            st = path.stat()
        except (OSError, WorkspaceError):
            return None
        stamp = (st.st_size, st.st_mtime_ns)
        cached = self._kinds.get(rel)
        if cached and cached[0] == stamp:
            return cached[1]
        kind = datasets.identify(path)
        self._kinds[rel] = (stamp, kind)
        return kind

    def tooltip_for(self, rel):
        if self.workspace is None:
            return ""
        try:
            path = self.workspace.path(rel)
        except WorkspaceError:
            return ""
        if path.is_dir():
            return rel or str(self.workspace.root)
        lines = [path.name]
        kind = self._kind(rel)
        if kind is not None:
            name = kind.technique or "Unrecognised file"
            lines.append(name if kind.supported else f"{name}: {kind.reason}")
        params = datasets.read_params(datasets.params_path_for(path))
        for key, label in (("mV_s", "Scan rate (mV/s)"), ("cycles", "Cycles"),
                           ("step_V", "Step (V)"), ("pulse_V", "Pulse (V)")):
            if key in params:
                lines.append(f"{label}: {params[key]}")
        try:
            origins = self.workspace.origins(rel)
        except OSError:
            origins = []
        for origin in origins:
            lines.append(f"Copied from: {origin['original_path']}")
        if not origins:
            lines.append("Original location not recorded.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def selected_rels(self):
        """Selected workspace paths, leaving out any inside another selected folder."""
        rels = [item.data(0, REL_ROLE) for item in self.tree.selectedItems()]
        folders = {r for r in rels if self.workspace and self.workspace.path(r).is_dir()}
        return [r for r in rels
                if not any(r != f and r.startswith(f + "/") for f in folders)]

    def current_folder(self):
        """The folder new files go into: the selected folder, or the selected file's."""
        item = self.tree.currentItem()
        if item is None or not item.isSelected():
            return ""
        if item.data(0, DIR_ROLE):
            return item.data(0, REL_ROLE)
        return _parent(item.data(0, REL_ROLE))

    def show_file(self, rel):
        """Highlight the file a plot's selected trace came from (None: that trace was deselected).

        A deselection only clears the highlight if it is still the one this
        put there, so a selection made in the tree since is left alone.
        """
        if rel is not None:
            self._shown_file = rel
            self.select([rel])
            return
        if self._shown_file is not None and self.selected_rels() == [self._shown_file]:
            self.tree.clearSelection()
        self._shown_file = None

    def select(self, rels):
        self.tree.clearSelection()
        for rel in rels:
            item = self.item_for(rel)
            if item is None:
                continue
            parent = item.parent()
            while parent is not None:
                parent.setExpanded(True)
                parent = parent.parent()
            item.setSelected(True)
            self.tree.setCurrentItem(item, 0, QtCore.QItemSelectionModel.NoUpdate)
            self.tree.scrollToItem(item)

    # ------------------------------------------------------------------
    # Adding (always a copy)
    # ------------------------------------------------------------------

    def _start_folder(self):
        from ...settings import get_cv_data_folder
        folder = get_cv_data_folder()
        return str(folder) if folder.is_dir() else str(Path.home())

    def add_files_dialog(self):
        if self.workspace is None:
            return
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Copy Data Files into the Workspace", self._start_folder(),
            "Data files (*.csv)")
        if paths:
            self.add_paths(paths, self.current_folder())

    def add_folder_dialog(self):
        if self.workspace is None:
            return
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Copy a Folder of Data Files into the Workspace", self._start_folder())
        if folder:
            self.add_paths([folder], self.current_folder())

    def add_paths(self, paths, folder_rel=""):
        """Copy files and folders into a workspace folder; returns the new workspace paths."""
        if self.workspace is None:
            return []
        added, duplicates, failed = [], [], []
        for p in paths:
            p = Path(p)
            try:
                if p.is_dir():
                    report = self.workspace.add_folder(p, folder_rel)
                    added += report.added
                    duplicates += report.duplicates
                    failed += report.failed
                else:
                    added.append(self.workspace.add_file(p, folder_rel))
            except DuplicateFileError as e:
                duplicates.append((p, folder_rel, e.existing))
            except (WorkspaceError, OSError) as e:
                failed.append((p, str(e)))

        if duplicates and self.confirm_duplicates(duplicates):
            for source, target, _existing in duplicates:
                try:
                    added.append(self.workspace.add_file(source, target, allow_duplicate=True))
                except (WorkspaceError, OSError) as e:
                    failed.append((source, str(e)))
        if failed:
            self.show_error("Some files were not added",
                            "These were not copied into the workspace:\n\n"
                            + _bullets([f"{Path(s).name}: {msg}" for s, msg in failed]))

        rels = [result.rel for result in added]
        self.refresh()
        if rels:
            self.select(rels)
            self.files_added.emit(rels)
        return rels

    def confirm_duplicates(self, duplicates):
        """Ask whether files whose content is already in the workspace should be copied again."""
        lines = [f"{Path(source).name}  (already here as {', '.join(existing)})"
                 for source, _folder, existing in duplicates]
        answer = QtWidgets.QMessageBox.question(
            self, "Already in the Workspace",
            "These files are already in the workspace with identical content:\n\n"
            + _bullets(lines) + "\n\nAdd another copy of them anyway?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return answer == QtWidgets.QMessageBox.Yes

    # ------------------------------------------------------------------
    # Organising (copies only)
    # ------------------------------------------------------------------

    def new_folder(self):
        if self.workspace is None:
            return None
        parent = self.current_folder()
        existing = {p.name for p in self.workspace.path(parent).iterdir()}
        name, n = "New Folder", 2
        while name in existing:
            name, n = f"New Folder {n}", n + 1
        try:
            rel = self.workspace.make_folder(parent, name)
        except (WorkspaceError, OSError) as e:
            self.show_error("Could not create the folder", str(e))
            return None
        self.refresh()
        self.select([rel])
        item = self.item_for(rel)
        if item is not None and self.tree.isVisible():
            self.tree.editItem(item, 0)
        return rel

    def rename(self, rel, new_name):
        """Rename a workspace file or folder; returns its new workspace path, or None."""
        try:
            result = self.workspace.rename(rel, new_name)
        except (WorkspaceError, OSError) as e:
            self.show_error("Could not rename", str(e))
            self.refresh()
            return None
        self.refresh()
        self.select([result.rel])
        if result.moved:
            self.files_moved.emit(result.moved)
        return result.rel

    def _on_item_changed(self, item, _column):
        if self._refreshing:
            return
        rel = item.data(0, REL_ROLE)
        if item.text(0) != PurePosixPath(rel).name:
            # Defer: renaming rebuilds the tree, which must not happen while
            # the item's editor is still closing.
            text = item.text(0)
            QtCore.QTimer.singleShot(0, lambda: self.rename(rel, text))

    def move_into(self, rels, folder_rel):
        """Move workspace files and folders into a workspace folder."""
        if self.workspace is None:
            return []
        rels = [r for r in rels
                if not any(r != o and r.startswith(o + "/") for o in rels)]
        moved, landed, failed = [], [], []
        for rel in rels:
            if _parent(rel) == folder_rel or rel == folder_rel:
                continue  # already there, or dropped onto itself
            try:
                result = self.workspace.move(rel, folder_rel)
            except (WorkspaceError, OSError) as e:
                failed.append((rel, str(e)))
                continue
            landed.append(result.rel)
            moved += result.moved
        if failed:
            self.show_error("Some items were not moved",
                            _bullets([f"{r}: {msg}" for r, msg in failed]))
        self.refresh()
        self.select(landed)
        if moved:
            self.files_moved.emit(moved)
        return moved

    def remove_selected(self):
        if self.workspace is None:
            return []
        rels = self.selected_rels()
        if not rels:
            return []
        busy = [f for rel in rels for f in self.workspace.data_files(rel) if self.in_use(f)]
        if busy:
            self.show_error("Files are open in a plot",
                            "Remove these from every plot window first:\n\n" + _bullets(busy))
            return []
        if not self.confirm_remove(rels):
            return []
        removed, failed = [], []
        for rel in rels:
            try:
                removed += self.workspace.remove(rel, self.move_to_trash)
            except (WorkspaceError, OSError) as e:
                failed.append((rel, str(e)))
        if failed:
            self.show_error("Some items were not removed",
                            _bullets([f"{r}: {msg}" for r, msg in failed]))
        self.refresh()
        if removed:
            self.files_removed.emit(removed)
        return removed

    def confirm_remove(self, rels):
        answer = QtWidgets.QMessageBox.question(
            self, "Remove from Workspace",
            "Move these workspace copies to the Trash?\n\n" + _bullets(rels)
            + "\n\nOriginal data files are not affected.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return answer == QtWidgets.QMessageBox.Yes

    def show_error(self, title, text):
        QtWidgets.QMessageBox.warning(self, title, text)

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def _on_double_click(self, item, _column):
        if not item.data(0, DIR_ROLE):
            self.open_requested.emit([item.data(0, REL_ROLE)])

    def _show_context_menu(self, pos):
        if self.workspace is None:
            return
        item = self.tree.itemAt(pos)
        menu = QtWidgets.QMenu(self)
        menu.addAction("New Folder", self.new_folder)
        menu.addAction("Add Files…", self.add_files_dialog)
        if item is not None:
            menu.addSeparator()
            menu.addAction("Rename", lambda: self.tree.editItem(item, 0))
            menu.addAction(self._remove_action)
            rel = item.data(0, REL_ROLE)
            label = "Show in Finder" if sys.platform == "darwin" else "Show in File Manager"
            menu.addAction(label, lambda: reveal_in_file_manager(self.workspace.path(rel)))
        menu.exec_(self.tree.viewport().mapToGlobal(pos))

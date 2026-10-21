"""The launcher's side of updating: asking the user, downloading, handing off.

The network work runs on background threads so the board table keeps
refreshing; every decision point is a dialog the user answers. See
``moles.updates`` for what counts as an update and when one is offered.
"""

import logging

from PyQt5 import QtCore, QtGui, QtWidgets

from ... import __version__, settings, updates

logger = logging.getLogger(__name__)


class UpdateCheckThread(QtCore.QThread):
    """Asks GitHub for a newer release; emits ``found`` only if there is one."""

    found = QtCore.pyqtSignal(object)

    def run(self):
        try:
            release = updates.check_for_update()
        except Exception:
            logger.exception("Update check failed")
            return
        if release is not None:
            self.found.emit(release)


class DownloadThread(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, int)
    done = QtCore.pyqtSignal(str)     # path to the installer, "" if cancelled
    failed = QtCore.pyqtSignal(str)

    def __init__(self, release, parent=None):
        super().__init__(parent)
        self._release = release
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        def report(done, total):
            self.progress.emit(done, total)
            return not self._cancel
        try:
            path = updates.download_installer(
                self._release, updates.download_folder(), report)
        except Exception as e:
            logger.exception("Downloading %s failed", self._release.asset_url)
            self.failed.emit(str(e))
            return
        self.done.emit(str(path) if path else "")


class UpdateOffer(QtCore.QObject):
    """Walks the user through one offered release.

    Emits ``installer_opened`` once the new installer is running, at which
    point the launcher should close so the installer can replace its files.
    """

    installer_opened = QtCore.pyqtSignal()

    def __init__(self, parent_window):
        super().__init__(parent_window)
        self._window = parent_window
        self._download = None
        self._progress = None

    @property
    def busy(self):
        return self._download is not None

    def offer(self, release):
        box = QtWidgets.QMessageBox(self._window)
        box.setIcon(QtWidgets.QMessageBox.Information)
        box.setWindowTitle("MOLES update available")
        box.setText(f"MOLES {release.version} is available. "
                    f"This computer has version {__version__}.")
        box.setInformativeText(
            "Updating downloads the new installer and opens it for you. "
            "Your settings, boards, calibrations and data are kept.\n\n"
            "Update now?")
        if release.notes:
            box.setDetailedText(release.notes)
        update_btn = box.addButton("Update Now", QtWidgets.QMessageBox.AcceptRole)
        box.addButton("Later", QtWidgets.QMessageBox.RejectRole)
        skip_btn = box.addButton("Skip This Version", QtWidgets.QMessageBox.ActionRole)
        box.setDefaultButton(update_btn)
        box.exec_()

        clicked = box.clickedButton()
        if clicked is skip_btn:
            settings.set_skipped_update_version(release.version)
        elif clicked is update_btn:
            self._start_update(release)

    def _start_update(self, release):
        busy = updates.boards_in_use()
        if busy:
            QtWidgets.QMessageBox.warning(
                self._window, "Update after the experiment",
                "MOLES can't update while it is using a board:\n\n    "
                + "\n    ".join(busy)
                + "\n\nThe update will be offered again the next time the "
                  "launcher opens.")
            return

        if not release.asset_url:
            # No installer for this computer in the release (or a test
            # release): the release page is the best we can do.
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(release.page_url))
            return

        self._progress = QtWidgets.QProgressDialog(
            f"Downloading MOLES {release.version}…", "Cancel", 0, 0, self._window)
        self._progress.setWindowTitle("MOLES update")
        self._progress.setWindowModality(QtCore.Qt.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)

        self._download = DownloadThread(release, self)
        self._download.progress.connect(self._on_progress)
        self._download.done.connect(self._on_downloaded)
        self._download.failed.connect(self._on_failed)
        self._progress.canceled.connect(self._download.cancel)
        self._download.start()
        self._progress.show()

    def _on_progress(self, done, total):
        if total:
            # Megabytes keep the numbers inside a QProgressDialog's int range.
            self._progress.setMaximum(max(1, total // 1_000_000))
            self._progress.setValue(done // 1_000_000)

    def _finish_download(self):
        self._download.wait()
        self._download = None
        self._progress.close()
        self._progress = None

    def _on_failed(self, message):
        self._finish_download()
        QtWidgets.QMessageBox.warning(
            self._window, "Update failed",
            f"The new installer could not be downloaded:\n\n{message}\n\n"
            "Check the internet connection and try again later, or download "
            f"it from:\n{updates.RELEASES_PAGE}")

    def _on_downloaded(self, path):
        self._finish_download()
        if not path:
            return  # cancelled
        QtWidgets.QMessageBox.information(
            self._window, "Ready to install",
            "The MOLES installer will now open and this launcher will close.\n\n"
            "Close any other MOLES windows before clicking Install. "
            f"(The installer was saved as {path}.)")
        try:
            updates.open_installer(path)
        except Exception as e:
            logger.exception("Could not open %s", path)
            QtWidgets.QMessageBox.warning(
                self._window, "Update failed",
                f"The installer was downloaded but could not be opened:\n{e}\n\n"
                f"Open it yourself from:\n{path}")
            return
        self.installer_opened.emit()

    def shutdown(self):
        """Stop a download in progress before the launcher closes."""
        if self._download is not None:
            self._download.cancel()
            self._download.wait(5_000)

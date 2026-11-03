"""Update notification, version history, and progress dialogs.

Features
--------
- Background update check via a worker thread.
- Version history browser — fetch all releases and pick one to download.
- Single-threaded streaming download with real-time progress.
- Cancellable download (user can abort mid-download).
- After download the user must explicitly **Accept** or **Reject** the update;
  the app never restarts without consent.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QProgressBar,
    QTextEdit,
)

from ...i18n import tr


# ═══════════════════════════════════════════════════════════════════════════════
# Background threads
# ═══════════════════════════════════════════════════════════════════════════════


class UpdateCheckThread(QThread):
    """Runs the update check in a background thread."""

    finished_with_result = Signal(object)  # UpdateCheckResult or None on error
    error_msg = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._owner: str = ""
        self._repo: str = ""
        self._token: str | None = None
        self._channel: str = "stable"

    def configure(self, owner: str, repo: str, token: str | None = None,
                  channel: str = "stable") -> None:
        self._owner = owner
        self._repo = repo
        self._token = token
        self._channel = channel

    def run(self) -> None:
        from ...application.updater import check_for_update

        try:
            project_path = f"{self._owner}/{self._repo}"
            result = check_for_update(
                project_path=project_path,
                token=self._token,
                channel=self._channel,
            )
        except Exception as exc:
            self.error_msg.emit(str(exc))
            self.finished_with_result.emit(None)
            return
        self.finished_with_result.emit(result)


class FetchReleasesThread(QThread):
    """Fetches all releases from GitHub in a background thread."""

    finished_with_result = Signal(object)  # list[ReleaseInfo] or None on error
    error_msg = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._owner: str = ""
        self._repo: str = ""
        self._token: str | None = None
        self._channel: str = "stable"

    def configure(self, owner: str, repo: str, token: str | None = None,
                  channel: str = "stable") -> None:
        self._owner = owner
        self._repo = repo
        self._token = token
        self._channel = channel

    def run(self) -> None:
        from ...application.updater import fetch_all_releases

        try:
            project_path = f"{self._owner}/{self._repo}"
            releases = fetch_all_releases(
                project_path=project_path,
                token=self._token,
                channel=self._channel,
            )
            self.finished_with_result.emit(releases)
        except Exception as exc:
            self.error_msg.emit(str(exc))
            self.finished_with_result.emit([])


class InstallUpdateThread(QThread):
    """Downloads and stages the update with real-time progress.

    Supports cancellation via ``cancel()``.
    """

    progress_pct = Signal(int)      # 0..100
    status_msg = Signal(str)        # human-readable status
    finished = Signal(bool)         # True = success (ready to accept), False = failed/cancelled
    error = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._release = None
        self._token: str | None = None
        self._cancel_event = threading.Event()
        self._downloaded_path = None  # stored on success for the caller to use

    def configure(self, release, token: str | None = None) -> None:
        self._release = release
        self._token = token

    def cancel(self) -> None:
        """Signal the download to stop."""
        self._cancel_event.set()

    @property
    def downloaded_path(self):
        """Path of the downloaded file (only valid after successful finish)."""
        return self._downloaded_path

    def run(self) -> None:
        from ...application.updater import _download, _get_token, _exe_path

        try:
            self._run_install()
        except Exception as exc:
            self.error.emit(str(exc))
            self.finished.emit(False)

    def _run_install(self) -> None:
        from ...application.updater import _download, _get_token, _exe_path

        self._cancel_event.clear()
        self.status_msg.emit(tr("updater.downloading", "Downloading update..."))
        self.progress_pct.emit(0)

        def _on_progress(downloaded: int, total: int) -> None:
            if total > 0:
                pct = int(downloaded * 100 / total)
                self.progress_pct.emit(pct)
                self.status_msg.emit(
                    tr("updater.download_progress",
                       "Downloading... {pct}% ({downloaded} / {total})").format(
                        pct=pct,
                        downloaded=_format_bytes(downloaded),
                        total=_format_bytes(total),
                    )
                )

        token = self._token or _get_token()

        from pathlib import Path

        # Download next to the running EXE so the user can always find it.
        app_dir = _exe_path().parent
        new_exe = app_dir / f"Count and Design v{self._release.version}.exe"

        # If the file already exists with a reasonable size, skip the download.
        if new_exe.exists() and new_exe.stat().st_size >= 1024:
            self._downloaded_path = str(new_exe)
            self.progress_pct.emit(100)
            self.status_msg.emit(
                tr("updater.already_downloaded",
                   "Update already downloaded. You can now accept or reject the update.")
            )
            self.finished.emit(True)
            return

        ok, err_msg = _download(
            self._release.download_url,
            new_exe,
            self._release.asset_size,
            token=token,
            progress_callback=_on_progress,
            cancel_event=self._cancel_event,
        )

        if ok:
            if new_exe.stat().st_size < 1024:
                self.error.emit(
                    tr("updater.download_failed",
                       "Failed to download the update: {error}").format(
                        error="Downloaded file is corrupt (too small)")
                )
                self.finished.emit(False)
                return
            self._downloaded_path = str(new_exe)
            self.progress_pct.emit(100)
            self.status_msg.emit(
                tr("updater.ready_to_install",
                   "Update downloaded. You can now accept or reject the update.")
            )
            self.finished.emit(True)
        elif self._cancel_event.is_set():
            self.status_msg.emit(
                tr("updater.download_cancelled", "Download cancelled.")
            )
            self.finished.emit(False)
        else:
            self.error.emit(
                tr("updater.download_failed",
                   "Failed to download the update: {error}").format(error=err_msg)
            )
            self.finished.emit(False)


def _format_bytes(n: int) -> str:
    """Format bytes as a human-readable string."""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    else:
        return f"{n / (1024 * 1024):.1f} MB"


# ═══════════════════════════════════════════════════════════════════════════════
# Dialogs
# ═══════════════════════════════════════════════════════════════════════════════


class UpdateAvailableDialog(QDialog):
    """Dialog shown when a new version is available.

    Workflow:
    1. Show version info and release notes.
    2. User clicks "Update Now" → download begins.
    3. During download: progress bar + status label; user can click "Cancel".
    4a. If cancelled → return to step 1 (buttons re-enabled).
    4b. If download fails → show error, return to step 1.
    4c. If download succeeds → show "Accept Update" / "Reject" buttons.
    5a. Accept → launch replacement script and quit.
    5b. Reject → close dialog, delete downloaded file.
    """

    # States
    _STATE_PROMPT = 0     # initial prompt (Update Now / Skip / Remind Later)
    _STATE_DOWNLOADING = 1  # download in progress
    _STATE_CONFIRM = 2     # download complete, awaiting accept/reject

    def __init__(self, current_version: str, release, parent=None,
                 pre_quit_callback=None) -> None:
        super().__init__(parent)
        self._release = release
        self._current_version = current_version
        self._install_thread: InstallUpdateThread | None = None
        self._mode = self._STATE_PROMPT
        self._pre_quit_callback = pre_quit_callback
        self.setWindowTitle(tr("updater.dialog.title", "Update Available"))
        self.setMinimumWidth(480)
        self.setMaximumWidth(560)
        self._build_ui()
        self._enter_prompt_state()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Header
        self._header = QLabel("")
        self._header.setWordWrap(True)
        layout.addWidget(self._header)

        # Version info
        self._version_info = QLabel(
            tr(
                "updater.version_info",
                "Current version: <b>{current}</b><br>"
                "Latest version: <b>{latest}</b>",
            ).format(current=self._current_version, latest=self._release.version)
        )
        self._version_info.setWordWrap(True)
        layout.addWidget(self._version_info)

        # Release notes
        if self._release.body:
            notes_label = QLabel(tr("updater.release_notes", "Release Notes:"))
            layout.addWidget(notes_label)

            notes = QTextEdit()
            notes.setReadOnly(True)
            notes.setMaximumHeight(200)
            notes.setMarkdown(self._release.body)
            layout.addWidget(notes)

        # File size info
        size_label = QLabel(
            tr("updater.file_size", "File size: {size}").format(
                size=_format_bytes(self._release.asset_size)
            )
        )
        size_label.setStyleSheet("QLabel { color: #888; }")
        layout.addWidget(size_label)

        # Progress bar (hidden initially)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # Status label (hidden initially)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        layout.addWidget(self._status)

        # Buttons area
        self._btn_layout = QHBoxLayout()
        layout.addLayout(self._btn_layout)

    # ── State management ────────────────────────────────────────────────

    def _enter_prompt_state(self) -> None:
        """Show the initial prompt buttons."""
        self._mode = self._STATE_PROMPT
        self._header.setText(
            f"<h2>{tr('updater.new_version_found', 'A new version is available!')}</h2>"
        )
        self._progress.setVisible(False)
        self._progress.setValue(0)
        self._status.setVisible(False)
        self._status.setText("")
        self._rebuild_buttons_for_prompt()

    def _enter_downloading_state(self) -> None:
        """Show progress bar and Cancel button."""
        self._mode = self._STATE_DOWNLOADING
        self._header.setText(
            f"<h3>{tr('updater.downloading_title', 'Downloading Update...')}</h3>"
        )
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._status.setVisible(True)
        self._status.setText(tr("updater.downloading", "Downloading update..."))
        self._rebuild_buttons_for_downloading()

    def _enter_confirm_state(self) -> None:
        """Download complete — show Accept / Reject buttons."""
        self._mode = self._STATE_CONFIRM
        self._header.setText(
            f"<h2>{tr('updater.install_confirm', 'Update Ready to Install')}</h2>"
        )
        self._progress.setVisible(False)
        self._rebuild_buttons_for_confirm()

    # ── Button builders ─────────────────────────────────────────────────

    def _clear_buttons(self) -> None:
        while self._btn_layout.count():
            item = self._btn_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _rebuild_buttons_for_prompt(self) -> None:
        self._clear_buttons()

        skip_btn = QPushButton(tr("updater.skip", "Skip This Version"))
        skip_btn.clicked.connect(self._skip_version)
        self._btn_layout.addWidget(skip_btn)

        remind_btn = QPushButton(tr("updater.remind_later", "Remind Later"))
        remind_btn.clicked.connect(self.reject)
        self._btn_layout.addWidget(remind_btn)

        self._btn_layout.addStretch()

        update_btn = QPushButton(tr("updater.update_now", "Update Now"))
        update_btn.setDefault(True)
        update_btn.clicked.connect(self._start_update)
        self._btn_layout.addWidget(update_btn)

    def _rebuild_buttons_for_downloading(self) -> None:
        self._clear_buttons()

        self._btn_layout.addStretch()

        self._cancel_btn = QPushButton(tr("updater.cancel_download", "Cancel Download"))
        self._cancel_btn.clicked.connect(self._cancel_download)
        self._btn_layout.addWidget(self._cancel_btn)

    def _rebuild_buttons_for_confirm(self) -> None:
        self._clear_buttons()

        reject_btn = QPushButton(tr("updater.reject_update", "Reject Update"))
        reject_btn.clicked.connect(self._reject_update)
        self._btn_layout.addWidget(reject_btn)

        self._btn_layout.addStretch()

        accept_btn = QPushButton(tr("updater.accept_update", "Accept & Restart"))
        accept_btn.setDefault(True)
        accept_btn.setStyleSheet(
            "QPushButton { font-weight: bold; background-color: #4a90d9; "
            "color: white; padding: 6px 20px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #357abd; }"
        )
        accept_btn.clicked.connect(self._accept_update)
        self._btn_layout.addWidget(accept_btn)

    # ── Actions ─────────────────────────────────────────────────────────

    def _skip_version(self) -> None:
        """Remember this version as skipped so we don't nag again."""
        from ...application.updater import set_skipped_version

        set_skipped_version(self._release.version)
        self.reject()

    def _start_update(self) -> None:
        """Initiate the download process."""
        self._enter_downloading_state()

        self._install_thread = InstallUpdateThread(self)
        self._install_thread.configure(self._release)
        self._install_thread.progress_pct.connect(self._on_progress)
        self._install_thread.status_msg.connect(self._status.setText)
        self._install_thread.error.connect(self._on_install_error)
        self._install_thread.finished.connect(self._on_install_finished)
        self._install_thread.start()

    def _on_progress(self, pct: int) -> None:
        self._progress.setValue(pct)

    def _cancel_download(self) -> None:
        """User clicked Cancel during download."""
        if self._install_thread and self._install_thread.isRunning():
            self._status.setText(
                tr("updater.cancelling", "Cancelling download...")
            )
            self._cancel_btn.setEnabled(False)
            self._install_thread.cancel()

    def _on_install_error(self, msg: str) -> None:
        """Show an error message in the status label."""
        self._status.setText(msg)
        # Stay in downloading state but let finished signal handle the rest

    def _on_install_finished(self, success: bool) -> None:
        """Download thread finished (success, failure, or cancelled)."""
        # Guard against dialog already closed while thread was running
        if not self.isVisible():
            return
        if success:
            self._enter_confirm_state()
        else:
            # Save the error/cancel message before entering prompt state
            saved_msg = self._status.text()
            # Go back to prompt state so user can retry
            self._enter_prompt_state()
            if saved_msg:
                # Restore the error / cancel message so it's visible
                self._status.setText(saved_msg)
                self._status.setVisible(True)

    def _accept_update(self) -> None:
        """User accepts the update — launch new version and quit."""
        from ...application.updater import launch_new_version

        if not self._install_thread or not self._install_thread.downloaded_path:
            return

        launch_new_version(self._install_thread.downloaded_path)
        self.accept()
        if self._pre_quit_callback:
            self._pre_quit_callback()
        self._quit_app()

    def _reject_update(self) -> None:
        """User rejects the update — delete downloaded file and close."""
        if self._install_thread and self._install_thread.downloaded_path:
            from pathlib import Path
            p = Path(self._install_thread.downloaded_path)
            p.unlink(missing_ok=True)
        self.reject()

    def reject(self) -> None:
        """Cancel any running download before closing."""
        self._disconnect_install_thread()
        super().reject()

    def _disconnect_install_thread(self) -> None:
        """Disconnect all signals from the install thread so stale signals
        don't reach already-destroyed widgets."""
        if self._install_thread is not None:
            try:
                self._install_thread.finished.disconnect(self._on_install_finished)
            except (RuntimeError, TypeError):
                pass
            try:
                self._install_thread.error.disconnect(self._on_install_error)
            except (RuntimeError, TypeError):
                pass
            try:
                self._install_thread.progress_pct.disconnect(self._on_progress)
            except (RuntimeError, TypeError):
                pass
            try:
                self._install_thread.status_msg.disconnect(self._status.setText)
            except (RuntimeError, TypeError):
                pass
            if self._install_thread.isRunning():
                self._install_thread.cancel()

    @staticmethod
    def _quit_app() -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()


class NoUpdateDialog(QDialog):
    """Small info box when the app is already up-to-date."""

    def __init__(self, current_version: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("updater.no_update_title", "Up to Date"))
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        header = QLabel(
            f"<h3>{tr('updater.already_latest', 'You are running the latest version.')}</h3>"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        info = QLabel(
            tr("updater.current_version_label", "Version: {version}").format(
                version=current_version
            )
        )
        layout.addWidget(info)

        close_btn = QPushButton(tr("common.ok", "OK"))
        close_btn.clicked.connect(self.accept)
        close_btn.setDefault(True)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)


class VersionHistoryDialog(QDialog):
    """Dialog that lists all downloadable releases for the user to pick from.

    Workflow:
    1. Fetch all releases in the background (thread).
    2. Show a list with version, name, size, and published date.
    3. User selects a release and clicks "Download".
    4. Download proceeds with progress (same as UpdateAvailableDialog).
    5. On success → Accept / Reject (same as UpdateAvailableDialog).
    """

    _STATE_LIST = 0
    _STATE_DOWNLOADING = 1
    _STATE_CONFIRM = 2

    def __init__(self, current_version: str, parent=None,
                 pre_quit_callback=None) -> None:
        super().__init__(parent)
        self._current_version = current_version
        self._releases: list = []
        self._selected_release = None
        self._fetch_thread: FetchReleasesThread | None = None
        self._install_thread: InstallUpdateThread | None = None
        self._mode = self._STATE_LIST
        self._pre_quit_callback = pre_quit_callback

        self.setWindowTitle(tr("updater.version_history_title", "Version History"))
        self.setMinimumWidth(560)
        self.setMaximumWidth(700)
        self.setMinimumHeight(400)
        self._build_ui()
        self._start_fetch()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Header
        self._header = QLabel(
            f"<h3>{tr('updater.version_history_header', 'Available Versions')}</h3>"
        )
        self._header.setWordWrap(True)
        layout.addWidget(self._header)

        # Subtitle
        self._subtitle = QLabel(
            tr(
                "updater.version_history_subtitle",
                "Current version: <b>{current}</b>. Select a version to download.",
            ).format(current=self._current_version)
        )
        self._subtitle.setWordWrap(True)
        layout.addWidget(self._subtitle)

        # Loading indicator
        self._loading_label = QLabel(
            tr("updater.fetching_releases", "Fetching releases...")
        )
        self._loading_label.setStyleSheet("QLabel { color: #888; }")
        layout.addWidget(self._loading_label)

        # Release list
        from PySide6.QtWidgets import QListWidget

        self._list_widget = QListWidget()
        self._list_widget.setVisible(False)
        self._list_widget.currentRowChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list_widget, stretch=1)

        # Selected release info
        self._release_info = QLabel("")
        self._release_info.setWordWrap(True)
        self._release_info.setVisible(False)
        self._release_info.setStyleSheet(
            "QLabel { color: #666; padding: 4px; border: 1px solid #ddd; "
            "border-radius: 3px; background: #f9f9f9; }"
        )
        layout.addWidget(self._release_info)

        # Progress bar (hidden initially)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        # Status label (hidden initially)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        layout.addWidget(self._status)

        # Buttons
        self._btn_layout = QHBoxLayout()
        layout.addLayout(self._btn_layout)
        self._rebuild_buttons_for_list()

    # ── State management ────────────────────────────────────────────────

    def _rebuild_buttons_for_list(self) -> None:
        self._clear_buttons()
        self._btn_layout.addStretch()

        close_btn = QPushButton(tr("updater.close", "Close"))
        close_btn.clicked.connect(self.reject)
        self._btn_layout.addWidget(close_btn)

        self._download_btn = QPushButton(
            tr("updater.download_selected", "Download Selected")
        )
        self._download_btn.setDefault(True)
        self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._start_download)
        self._btn_layout.addWidget(self._download_btn)

    def _rebuild_buttons_for_downloading(self) -> None:
        self._clear_buttons()
        self._btn_layout.addStretch()

        self._cancel_btn = QPushButton(
            tr("updater.cancel_download", "Cancel Download")
        )
        self._cancel_btn.clicked.connect(self._cancel_download)
        self._btn_layout.addWidget(self._cancel_btn)

    def _rebuild_buttons_for_confirm(self) -> None:
        self._clear_buttons()

        reject_btn = QPushButton(tr("updater.reject_update", "Reject Update"))
        reject_btn.clicked.connect(self._reject_update)
        self._btn_layout.addWidget(reject_btn)

        self._btn_layout.addStretch()

        accept_btn = QPushButton(tr("updater.accept_update", "Accept & Restart"))
        accept_btn.setDefault(True)
        accept_btn.setStyleSheet(
            "QPushButton { font-weight: bold; background-color: #4a90d9; "
            "color: white; padding: 6px 20px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #357abd; }"
        )
        accept_btn.clicked.connect(self._accept_update)
        self._btn_layout.addWidget(accept_btn)

    def _clear_buttons(self) -> None:
        while self._btn_layout.count():
            item = self._btn_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # ── Fetch ───────────────────────────────────────────────────────────

    def _start_fetch(self) -> None:
        from ...application.updater import _get_token, _repo_owner_name, get_update_channel

        try:
            owner, repo = _repo_owner_name()
        except RuntimeError:
            self._loading_label.setText(
                tr(
                    "updater.fetch_error",
                    "Unable to fetch releases: {error}",
                ).format(error="repository not configured")
            )
            return

        token = _get_token()
        if not token:
            self._loading_label.setText(
                tr(
                    "updater.fetch_error",
                    "Unable to fetch releases: {error}",
                ).format(error="update checking not available in this build")
            )
            return

        channel = get_update_channel()
        self._fetch_thread = FetchReleasesThread(self)
        self._fetch_thread.configure(owner, repo, token, channel=channel)
        self._fetch_thread.finished_with_result.connect(self._on_releases_fetched)
        self._fetch_thread.error_msg.connect(self._on_fetch_error)
        self._fetch_thread.start()

    def _on_fetch_error(self, msg: str) -> None:
        self._loading_label.setText(
            tr(
                "updater.fetch_error",
                "Unable to fetch releases: {error}",
            ).format(error=msg)
        )

    def _on_releases_fetched(self, releases: list) -> None:
        self._loading_label.setVisible(False)

        if not releases:
            self._header.setText(
                f"<h3>{tr('updater.no_releases', 'No releases found.')}</h3>"
            )
            return

        self._releases = releases
        self._list_widget.setVisible(True)

        for rel in releases:
            # Build a nice display line
            size_str = _format_bytes(rel.asset_size)
            label = f"v{rel.version}  —  {rel.name}"
            if rel.version == self._current_version:
                label += f"  [{tr('updater.current', 'current')}]"
            # Mark pre-releases
            from ...application.updater import _normalize_version
            if len(_normalize_version(rel.version)) > 3:
                label += "  [preview]"
            label += f"  ({size_str})"
            self._list_widget.addItem(label)

        self._header.setText(
            f"<h3>{tr('updater.version_history_header', 'Available Versions')} "
            f"({len(releases)})</h3>"
        )

    def _on_selection_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._releases):
            self._download_btn.setEnabled(False)
            self._release_info.setVisible(False)
            return

        rel = self._releases[row]
        self._selected_release = rel
        self._download_btn.setEnabled(True)

        # Show release info
        body = rel.body or ""
        if len(body) > 500:
            body = body[:497] + "..."
        self._release_info.setText(body)
        self._release_info.setVisible(bool(body))

    # ── Download flow ───────────────────────────────────────────────────

    def _start_download(self) -> None:
        if not self._selected_release:
            return

        self._mode = self._STATE_DOWNLOADING
        self._list_widget.setEnabled(False)
        self._download_btn.setEnabled(False)
        self._release_info.setVisible(False)

        self._header.setText(
            f"<h3>{tr('updater.downloading_title', 'Downloading Update...')}</h3>"
        )
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._status.setVisible(True)
        self._status.setText(
            tr("updater.downloading", "Downloading update...")
        )
        self._rebuild_buttons_for_downloading()

        self._install_thread = InstallUpdateThread(self)
        self._install_thread.configure(self._selected_release)
        self._install_thread.progress_pct.connect(self._on_progress)
        self._install_thread.status_msg.connect(self._status.setText)
        self._install_thread.error.connect(self._on_install_error)
        self._install_thread.finished.connect(self._on_install_finished)
        self._install_thread.start()

    def _on_progress(self, pct: int) -> None:
        self._progress.setValue(pct)

    def _cancel_download(self) -> None:
        if self._install_thread and self._install_thread.isRunning():
            self._status.setText(
                tr("updater.cancelling", "Cancelling download...")
            )
            self._cancel_btn.setEnabled(False)
            self._install_thread.cancel()

    def _on_install_error(self, msg: str) -> None:
        self._status.setText(msg)

    def _on_install_finished(self, success: bool) -> None:
        # Guard against dialog already closed while thread was running
        if not self.isVisible():
            return
        if success:
            self._mode = self._STATE_CONFIRM
            self._header.setText(
                f"<h2>{tr('updater.install_confirm', 'Update Ready to Install')}</h2>"
            )
            self._progress.setVisible(False)
            self._rebuild_buttons_for_confirm()
        else:
            # Go back to list state so user can retry
            self._mode = self._STATE_LIST
            self._list_widget.setEnabled(True)
            self._header.setText(
                f"<h3>{tr('updater.version_history_header', 'Available Versions')} "
                f"({len(self._releases)})</h3>"
            )
            self._progress.setVisible(False)
            # Rebuild buttons FIRST — the old _download_btn was destroyed
            # by _clear_buttons() when _start_download was called.
            self._rebuild_buttons_for_list()
            self._download_btn.setEnabled(self._selected_release is not None)
            if self._status.text():
                self._status.setVisible(True)

    def _accept_update(self) -> None:
        """User accepts the update — launch new version and quit."""
        from ...application.updater import launch_new_version

        if not self._install_thread or not self._install_thread.downloaded_path:
            return

        launch_new_version(self._install_thread.downloaded_path)
        self.accept()
        if self._pre_quit_callback:
            self._pre_quit_callback()
        self._quit_app()

    def _reject_update(self) -> None:
        """User rejects the update — delete downloaded file and go back."""
        if self._install_thread and self._install_thread.downloaded_path:
            from pathlib import Path
            p = Path(self._install_thread.downloaded_path)
            p.unlink(missing_ok=True)
        # Return to list
        self._mode = self._STATE_LIST
        self._list_widget.setEnabled(True)
        self._header.setText(
            f"<h3>{tr('updater.version_history_header', 'Available Versions')} "
            f"({len(self._releases)})</h3>"
        )
        self._progress.setVisible(False)
        self._progress.setValue(0)
        self._status.setVisible(False)
        self._rebuild_buttons_for_list()
        self._download_btn.setEnabled(self._selected_release is not None)

    def reject(self) -> None:
        """Cancel any running download before closing."""
        self._disconnect_install_thread()
        super().reject()

    def _disconnect_install_thread(self) -> None:
        """Disconnect all signals from the install thread so stale signals
        don't reach already-destroyed widgets."""
        if self._install_thread is not None:
            try:
                self._install_thread.finished.disconnect(self._on_install_finished)
            except (RuntimeError, TypeError):
                pass
            try:
                self._install_thread.error.disconnect(self._on_install_error)
            except (RuntimeError, TypeError):
                pass
            try:
                self._install_thread.progress_pct.disconnect(self._on_progress)
            except (RuntimeError, TypeError):
                pass
            try:
                self._install_thread.status_msg.disconnect(self._status.setText)
            except (RuntimeError, TypeError):
                pass
            if self._install_thread.isRunning():
                self._install_thread.cancel()

    @staticmethod
    def _quit_app() -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()


# ═══════════════════════════════════════════════════════════════════════════════
# Version Manager Dialog — unified update management
# ═══════════════════════════════════════════════════════════════════════════════


class VersionManagerDialog(QDialog):
    """Unified version manager that combines channel selection, version
    browsing, download with progress, and update installation.

    Layout (top → bottom):
    1. Current version display
    2. Update channel dropdown
    3. Available version list (fixed scrollable area)
    4. Progress bar (hidden until download)
    5. Download / Cancel action buttons
    6. Status tip bar
    """

    _STATE_IDLE = 0
    _STATE_FETCHING = 1
    _STATE_DOWNLOADING = 2
    _STATE_READY = 3  # download complete, awaiting user confirmation

    def __init__(self, current_version: str, parent=None,
                 pre_quit_callback=None) -> None:
        super().__init__(parent)
        self._current_version = current_version
        self._releases: list = []
        self._selected_release = None
        self._fetch_thread: FetchReleasesThread | None = None
        self._install_thread: InstallUpdateThread | None = None
        self._mode = self._STATE_IDLE
        self._pre_quit_callback = pre_quit_callback

        self.setWindowTitle(tr("updater.version_manager_title", "Update"))
        self.setMinimumWidth(520)
        self.setMaximumWidth(680)
        self.setMinimumHeight(500)
        self._build_ui()
        self._start_fetch()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # ── 1. Current version ────────────────────────────────────────
        version_layout = QHBoxLayout()
        version_label = QLabel(tr("updater.current_version", "Current version:"))
        version_label.setStyleSheet("QLabel { font-size: 13px; color: #666; }")
        version_layout.addWidget(version_label)

        self._version_value = QLabel(f"<b>v{self._current_version}</b>")
        self._version_value.setStyleSheet(
            "QLabel { font-size: 16px; font-weight: bold; color: #333; }"
        )
        version_layout.addWidget(self._version_value)
        version_layout.addStretch()
        layout.addLayout(version_layout)

        # ── 2. Channel selector ───────────────────────────────────────
        channel_layout = QHBoxLayout()
        channel_label = QLabel(tr("updater.update_channel_label", "Update channel:"))
        channel_label.setStyleSheet("QLabel { font-size: 12px; color: #666; }")
        channel_layout.addWidget(channel_label)

        from PySide6.QtWidgets import QComboBox
        from ...application.updater import get_update_channel

        self._channel_combo = QComboBox()
        self._channel_combo.addItem(
            tr("updater.channel_stable", "Stable (recommended)"), "stable"
        )
        self._channel_combo.addItem(
            tr("updater.channel_preview", "Preview (nightly builds)"), "preview"
        )
        current_channel = get_update_channel()
        self._channel_combo.setCurrentIndex(0 if current_channel == "stable" else 1)
        self._channel_combo.currentIndexChanged.connect(self._on_channel_changed)
        channel_layout.addWidget(self._channel_combo)
        channel_layout.addStretch()
        layout.addLayout(channel_layout)

        # ── 3. Version list ───────────────────────────────────────────
        list_header = QLabel(
            tr("updater.available_versions", "Available versions:")
        )
        list_header.setStyleSheet("QLabel { font-size: 12px; color: #666; }")
        layout.addWidget(list_header)

        from PySide6.QtWidgets import QListWidget

        self._list_widget = QListWidget()
        self._list_widget.setMinimumHeight(180)
        self._list_widget.setStyleSheet(
            "QListWidget { border: 1px solid #ccc; border-radius: 4px; "
            "background: #fafafa; font-size: 12px; }"
            "QListWidget::item { padding: 6px 8px; }"
            "QListWidget::item:selected { background: #4a90d9; color: white; }"
        )
        self._list_widget.currentRowChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list_widget, stretch=1)

        # ── 4. Progress bar ───────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        self._progress.setMinimumHeight(20)
        layout.addWidget(self._progress)

        # ── 5. Action buttons ─────────────────────────────────────────
        self._btn_layout = QHBoxLayout()
        layout.addLayout(self._btn_layout)
        self._rebuild_buttons_for_idle()

        # ── 6. Status tip bar ─────────────────────────────────────────
        self._status_bar = QLabel("")
        self._status_bar.setWordWrap(True)
        self._status_bar.setStyleSheet(
            "QLabel { color: #888; font-size: 11px; padding: 6px 10px; "
            "border: 1px solid #e0e0e0; border-radius: 3px; "
            "background: #f5f5f5; min-height: 20px; }"
        )
        layout.addWidget(self._status_bar)
        self._show_random_tip()

    # ── Button builders ───────────────────────────────────────────────

    def _clear_buttons(self) -> None:
        while self._btn_layout.count():
            item = self._btn_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _rebuild_buttons_for_idle(self) -> None:
        self._clear_buttons()

        self._log_btn = QPushButton(tr("updater.log_button", "Log"))
        self._log_btn.clicked.connect(self._show_logs)
        self._btn_layout.addWidget(self._log_btn)

        self._btn_layout.addStretch()

        self._cancel_btn = QPushButton(tr("updater.close", "Close"))
        self._cancel_btn.clicked.connect(self.reject)
        self._btn_layout.addWidget(self._cancel_btn)

        self._download_btn = QPushButton(
            tr("updater.download", "Download")
        )
        self._download_btn.setDefault(True)
        self._download_btn.setEnabled(False)
        self._download_btn.setStyleSheet(
            "QPushButton { font-weight: bold; background-color: #4a90d9; "
            "color: white; padding: 6px 20px; border-radius: 3px; }"
            "QPushButton:hover { background-color: #357abd; }"
            "QPushButton:disabled { background-color: #ccc; color: #888; }"
        )
        self._download_btn.clicked.connect(self._start_download)
        self._btn_layout.addWidget(self._download_btn)

    def _rebuild_buttons_for_downloading(self) -> None:
        self._clear_buttons()

        self._log_btn = QPushButton(tr("updater.log_button", "Log"))
        self._log_btn.clicked.connect(self._show_logs)
        self._btn_layout.addWidget(self._log_btn)

        self._btn_layout.addStretch()

        self._cancel_btn = QPushButton(
            tr("updater.cancel", "Cancel")
        )
        self._cancel_btn.clicked.connect(self._cancel_download)
        self._btn_layout.addWidget(self._cancel_btn)

    # ── Tips ──────────────────────────────────────────────────────────

    def _show_random_tip(self) -> None:
        """Show a random fun tip in the status bar."""
        import random
        keys = [
            "updater.tip_fetching",
            "updater.tip_select_version",
            "updater.tip_download",
            "updater.tip_preview",
            "updater.tip_restart",
        ]
        key = random.choice(keys)
        self._status_bar.setText(tr(key, ""))

    def _show_logs(self) -> None:
        """Open the log viewer dialog (non-modal)."""
        viewer = LogViewerDialog(self)
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        viewer.show()

    def _set_status(self, key: str, fallback: str = "", **fmt) -> None:
        text = tr(key, fallback)
        if fmt:
            text = text.format(**fmt)
        self._status_bar.setText(text)

    # ── Channel switching ─────────────────────────────────────────────

    def _on_channel_changed(self, index: int) -> None:
        """Re-fetch releases when the user switches the update channel."""
        channel = self._channel_combo.currentData()
        if channel is None:
            return
        from ...application.updater import set_update_channel
        set_update_channel(channel)
        self._start_fetch()

    # ── Fetch ─────────────────────────────────────────────────────────

    def _start_fetch(self) -> None:
        from ...application.updater import _get_token, _repo_owner_name, get_update_channel

        self._mode = self._STATE_FETCHING
        self._list_widget.clear()
        # Keep the list visible with a placeholder to avoid layout jumps
        self._list_widget.addItem(tr("updater.fetching_releases", "Fetching releases..."))
        self._list_widget.setEnabled(False)
        self._releases = []
        self._selected_release = None
        self._download_btn.setEnabled(False)
        self._set_status("updater.status_fetching", "Fetching releases...")

        try:
            owner, repo = _repo_owner_name()
        except RuntimeError:
            self._set_status(
                "updater.fetch_error",
                "Unable to fetch releases: {error}",
                error="repository not configured",
            )
            self._mode = self._STATE_IDLE
            return

        token = _get_token()
        if not token:
            self._set_status(
                "updater.fetch_error",
                "Unable to fetch releases: {error}",
                error="update checking not available in this build",
            )
            self._mode = self._STATE_IDLE
            return

        channel = get_update_channel()
        self._fetch_thread = FetchReleasesThread(self)
        self._fetch_thread.configure(owner, repo, token, channel=channel)
        self._fetch_thread.finished_with_result.connect(self._on_releases_fetched)
        self._fetch_thread.error_msg.connect(self._on_fetch_error)
        self._fetch_thread.start()

    def _on_fetch_error(self, msg: str) -> None:
        self._mode = self._STATE_IDLE
        self._list_widget.clear()
        self._list_widget.setEnabled(True)
        self._set_status(
            "updater.fetch_error",
            "Unable to fetch releases: {error}",
            error=msg,
        )
        # Also show the raw error in a message box for debugging
        QMessageBox.warning(
            self,
            tr("updater.download_failed_title", "Download Failed"),
            msg,
        )

    def _on_releases_fetched(self, releases: list) -> None:
        self._mode = self._STATE_IDLE
        self._list_widget.clear()
        self._list_widget.setEnabled(True)

        if not releases:
            self._set_status(
                "updater.no_releases_status", "No releases found."
            )
            return

        self._releases = releases

        for rel in releases:
            size_str = _format_bytes(rel.asset_size)
            label = f"v{rel.version}  —  {rel.name}"
            if rel.version == self._current_version:
                label += f"  [{tr('updater.current', 'current')}]"
            # Mark pre-releases
            from ...application.updater import _normalize_version
            if len(_normalize_version(rel.version)) > 3:
                label += "  [preview]"
            label += f"  ({size_str})"
            self._list_widget.addItem(label)

        count = len(releases)
        self._set_status(
            "updater.releases_loaded",
            f"{count} release(s) loaded. Select a version to download.",
            count=count,
        )
        self._show_random_tip()

    # ── Selection ─────────────────────────────────────────────────────

    def _on_selection_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._releases):
            self._download_btn.setEnabled(False)
            self._selected_release = None
            self._show_random_tip()
            return

        rel = self._releases[row]
        self._selected_release = rel
        self._download_btn.setEnabled(True)

        if rel.version == self._current_version:
            self._set_status(
                "updater.already_current",
                "This is the currently running version.",
            )
        elif rel.version > self._current_version:
            self._set_status(
                "updater.newer_version_selected",
                f"Newer version v{rel.version} selected. Click Download to proceed.",
                version=rel.version,
            )
        else:
            self._set_status(
                "updater.older_version_selected",
                f"Older version v{rel.version} selected. You can downgrade if needed.",
                version=rel.version,
            )

    # ── Download flow ─────────────────────────────────────────────────

    def _start_download(self) -> None:
        if not self._selected_release:
            return

        self._mode = self._STATE_DOWNLOADING
        self._channel_combo.setEnabled(False)
        self._list_widget.setEnabled(False)
        self._download_btn.setEnabled(False)

        rel = self._selected_release
        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._set_status(
            "updater.downloading",
            f"Downloading v{rel.version} — {rel.name}...",
            version=rel.version, name=rel.name,
        )
        self._rebuild_buttons_for_downloading()

        self._install_thread = InstallUpdateThread(self)
        self._install_thread.configure(self._selected_release)
        self._install_thread.progress_pct.connect(self._on_progress)
        self._install_thread.status_msg.connect(self._status_bar.setText)
        self._install_thread.error.connect(self._on_install_error)
        self._install_thread.finished.connect(self._on_install_finished)
        self._install_thread.start()

    def _on_progress(self, pct: int) -> None:
        self._progress.setValue(pct)

    def _cancel_download(self) -> None:
        """User clicked Cancel during download."""
        if self._install_thread and self._install_thread.isRunning():
            self._set_status(
                "updater.cancelling", "Cancelling download..."
            )
            self._cancel_btn.setEnabled(False)
            self._install_thread.cancel()

    def _on_install_error(self, msg: str) -> None:
        self._status_bar.setText(msg)

    def _on_install_finished(self, success: bool) -> None:
        if not self.isVisible():
            return

        if success:
            self._mode = self._STATE_READY
            self._progress.setVisible(False)
            rel = self._selected_release
            self._set_status(
                "updater.download_complete",
                f"v{rel.version} downloaded successfully.",
                version=rel.version,
            )
            # Prompt user before restarting
            self._prompt_install()
        else:
            # Go back to idle so user can retry
            self._mode = self._STATE_IDLE
            self._channel_combo.setEnabled(True)
            self._list_widget.setEnabled(True)
            self._progress.setVisible(False)
            self._rebuild_buttons_for_idle()
            self._download_btn.setEnabled(self._selected_release is not None)
            if not self._status_bar.text():
                self._show_random_tip()

    def _prompt_install(self) -> None:
        """Ask the user whether to install the downloaded update now.

        Yes → launch replacement script and quit.
        No  → return to idle (downloaded file stays, user can close).
        """

        rel = self._selected_release
        reply = QMessageBox.question(
            self,
            tr("updater.install_confirm_title", "Update Ready"),
            tr(
                "updater.install_confirm_msg",
                "Version <b>v{version}</b> has been downloaded and is ready "
                "to install.\n\n"
                "<b>Restart now to apply the update?</b>\n\n"
                "The application will close and restart automatically.",
            ).format(version=rel.version),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )

        if reply == QMessageBox.StandardButton.Yes:
            self._accept_update()
        else:
            # User chose not to restart — keep the downloaded file
            # and return to idle so they can close or try another version
            self._mode = self._STATE_IDLE
            self._channel_combo.setEnabled(True)
            self._list_widget.setEnabled(True)
            self._rebuild_buttons_for_idle()
            self._download_btn.setEnabled(True)
            self._set_status(
                "updater.install_deferred",
                f"v{rel.version} downloaded but not yet installed. "
                "Close the dialog when ready, or select another version.",
                version=rel.version,
            )

    def _accept_update(self) -> None:
        """User accepts the update — launch new version and quit."""
        from ...application.updater import launch_new_version

        if not self._install_thread or not self._install_thread.downloaded_path:
            return

        launch_new_version(self._install_thread.downloaded_path)
        self.accept()
        if self._pre_quit_callback:
            self._pre_quit_callback()
        self._quit_app()

    def reject(self) -> None:
        """Cancel any running download before closing."""
        self._disconnect_install_thread()
        super().reject()

    def _disconnect_install_thread(self) -> None:
        if self._install_thread is not None:
            try:
                self._install_thread.finished.disconnect(self._on_install_finished)
            except (RuntimeError, TypeError):
                pass
            try:
                self._install_thread.error.disconnect(self._on_install_error)
            except (RuntimeError, TypeError):
                pass
            try:
                self._install_thread.progress_pct.disconnect(self._on_progress)
            except (RuntimeError, TypeError):
                pass
            try:
                self._install_thread.status_msg.disconnect(self._status_bar.setText)
            except (RuntimeError, TypeError):
                pass
            if self._install_thread.isRunning():
                self._install_thread.cancel()

    @staticmethod
    def _quit_app() -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()


# ═══════════════════════════════════════════════════════════════════════════════
# Log Viewer Dialog
# ═══════════════════════════════════════════════════════════════════════════════


class LogViewerDialog(QDialog):
    """Non-modal dialog that shows captured updater logs in real time."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("updater.log_viewer_title", "Update Log"))
        self.setMinimumSize(620, 400)
        self.resize(700, 480)
        self._build_ui()
        self._refresh_logs()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        header = QLabel(
            f"<h3>{tr('updater.log_viewer_header', 'Update Activity Log')}</h3>"
        )
        layout.addWidget(header)

        hint = QLabel(
            tr(
                "updater.log_viewer_hint",
                "Detailed log of all network requests, API calls, and errors.",
            )
        )
        hint.setStyleSheet("QLabel { color: #888; font-size: 11px; }")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setStyleSheet(
            "QTextEdit { font-family: 'Consolas', 'Courier New', monospace; "
            "font-size: 11px; background: #1e1e1e; color: #d4d4d4; "
            "border: 1px solid #555; border-radius: 3px; }"
        )
        layout.addWidget(self._log_view, stretch=1)

        btn_layout = QHBoxLayout()

        self._auto_refresh_btn = QPushButton(
            tr("updater.log_auto_refresh", "Auto-Refresh")
        )
        self._auto_refresh_btn.setCheckable(True)
        self._auto_refresh_btn.setChecked(True)
        self._auto_refresh_btn.toggled.connect(self._on_auto_refresh_toggled)
        btn_layout.addWidget(self._auto_refresh_btn)

        refresh_btn = QPushButton(tr("updater.log_refresh", "Refresh"))
        refresh_btn.clicked.connect(self._refresh_logs)
        btn_layout.addWidget(refresh_btn)

        clear_btn = QPushButton(tr("updater.log_clear", "Clear"))
        clear_btn.clicked.connect(self._clear_logs)
        btn_layout.addWidget(clear_btn)

        btn_layout.addStretch()

        close_btn = QPushButton(tr("updater.close", "Close"))
        close_btn.clicked.connect(self.close)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

        # Auto-refresh timer
        from PySide6.QtCore import QTimer
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_logs)
        self._refresh_timer.start(2000)  # refresh every 2 seconds

    def _on_auto_refresh_toggled(self, checked: bool) -> None:
        if checked:
            self._refresh_timer.start(2000)
        else:
            self._refresh_timer.stop()

    def _refresh_logs(self) -> None:
        from ...application.updater import get_captured_logs

        logs = get_captured_logs()
        self._log_view.setPlainText("\n".join(logs))
        # Auto-scroll to bottom
        scrollbar = self._log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _clear_logs(self) -> None:
        from ...application.updater import clear_captured_logs

        clear_captured_logs()
        self._log_view.clear()

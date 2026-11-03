"""Update-check workflow for the main window."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QMainWindow, QMessageBox

from ...application import updater
from ...application.updater import (
    _get_token,
    _repo_owner_name,
    detect_channel,
    get_skipped_version,
    get_update_channel,
    is_auto_check_enabled,
    UpdateCheckResult,
)
from ...i18n import tr
from ..views.update_dialog import (
    NoUpdateDialog,
    UpdateAvailableDialog,
    UpdateCheckThread,
    VersionManagerDialog,
)


class UpdateCheckController:
    """Own update-check threads and update-related dialogs."""

    def __init__(
        self,
        parent: QMainWindow,
        pre_quit_callback: Callable[[], None],
    ) -> None:
        self._parent = parent
        self._pre_quit_callback = pre_quit_callback
        self._thread: UpdateCheckThread | None = None

    @property
    def running_version(self) -> str:
        return updater._running_version()

    def check_silent(self) -> None:
        if is_auto_check_enabled():
            self._run(silent=True)

    def check_manual(self) -> None:
        self.show_version_manager()

    def _run(self, *, silent: bool) -> None:
        try:
            owner, repo = _repo_owner_name()
        except RuntimeError:
            return

        token = _get_token()
        if not token:
            if not silent:
                QMessageBox.information(
                    self._parent,
                    tr(
                        "updater.no_token_title",
                        "Update Check Unavailable",
                    ),
                    tr(
                        "updater.no_token_msg",
                        "Update checking is not available in this build.\n\n"
                        "Please contact your administrator.",
                    ),
                )
            return

        channel = detect_channel() if silent else get_update_channel()
        self._thread = UpdateCheckThread(self._parent)
        self._thread.configure(owner, repo, token, channel=channel)
        self._thread.finished_with_result.connect(
            lambda result: self._on_result(result, silent)
        )
        self._thread.error_msg.connect(
            lambda message: self._on_error(message, silent)
        )
        self._thread.start()

    def _on_error(self, message: str, silent: bool) -> None:
        if not silent:
            QMessageBox.warning(
                self._parent,
                tr("updater.check_error_title", "Update Check Failed"),
                tr(
                    "updater.check_error_msg",
                    "Could not check for updates:\n\n{error}",
                ).format(error=message),
            )

    def _on_result(
        self,
        result: UpdateCheckResult | None,
        silent: bool,
    ) -> None:
        if result is None:
            return
        if not result.update_available:
            if not silent:
                NoUpdateDialog(result.current_version, self._parent).exec()
            return

        if silent and get_skipped_version() == result.latest_release.version:
            return

        UpdateAvailableDialog(
            result.current_version,
            result.latest_release,
            self._parent,
            pre_quit_callback=self._pre_quit_callback,
        ).exec()

    def show_version_manager(self) -> None:
        VersionManagerDialog(
            self.running_version,
            self._parent,
            pre_quit_callback=self._pre_quit_callback,
        ).exec()

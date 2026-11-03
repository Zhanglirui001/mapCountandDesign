"""Project file, merge, and data-fill commands for the main window."""

from __future__ import annotations

import tempfile
import os
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from ...application.data_fill import DataFillError
from ...application.project.merge import MergeService
from ...application.project_session import NegativeBoxPositionError
from ...file_integrity import IntegrityStatus, atomic_write_json
from ...i18n import tr
from ...models import Project
from ..viewmodels import AppViewModel
from ..views.data_fill_dialog import DataFillDialog
from ..views.error_dialog import ErrorDialog
from ..views.merge_window import MergeWindow


class ProjectFileController:
    """Handle GUI-facing project lifecycle and merge workflows."""

    def __init__(self, parent: QWidget, view_model: AppViewModel) -> None:
        self._parent = parent
        self._view_model = view_model

    def new_project(self) -> None:
        self._view_model.new_project()

    def open_project(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self._parent,
            tr("dialog.open_project_title", "Open Project"),
            "",
            tr("dialog.file_filter", "JSON Files (*.json);;All Files (*)"),
        )
        if not file_path:
            return
        try:
            try:
                integrity_status = self._view_model.open_project(file_path)
            except NegativeBoxPositionError as exc:
                reply = QMessageBox.question(
                    self._parent,
                    tr(
                        "box_position.load_negative_title",
                        "Negative Box Position Values",
                    ),
                    tr(
                        "box_position.load_negative_message",
                        "This project contains {count} Node(s) with a negative "
                        "Box Position. Box Position cannot be negative. If you "
                        "continue, those values will be set to 0. Continue?",
                    ).format(count=exc.count),
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                integrity_status = self._view_model.open_project(
                    file_path,
                    normalize_negative_box_positions=True,
                )
            if integrity_status is IntegrityStatus.LEGACY_UNSIGNED:
                QMessageBox.warning(
                    self._parent,
                    tr("integrity.legacy_title", "Unverified Project History"),
                    tr(
                        "integrity.legacy_message",
                        "This legacy file has no integrity hash. Existing history is read-only; new history starts from the opened state.",
                    ),
                )
            elif integrity_status is IntegrityStatus.MODIFIED:
                QMessageBox.warning(
                    self._parent,
                    tr("integrity.modified_title", "Project File Modified"),
                    tr(
                        "integrity.modified_message",
                        "The file content does not match its saved hash. The project was opened, but its previous history was discarded.",
                    ),
                )
        except Exception:
            ErrorDialog.show_exception(
                message=tr("error.open_project", "Failed to open project."),
                parent=self._parent,
            )

    def save_project(self) -> None:
        if self._view_model.current_file is None:
            self.save_project_as()
            return
        try:
            self._view_model.save_project()
            self._show_saved()
        except Exception:
            ErrorDialog.show_exception(
                message=tr("error.save_project", "Failed to save project."),
                parent=self._parent,
            )

    def save_project_as(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self._parent,
            tr("dialog.save_project_title", "Save Project"),
            "",
            tr("dialog.file_filter", "JSON Files (*.json);;All Files (*)"),
        )
        if not file_path:
            return
        if not file_path.endswith(".json"):
            file_path += ".json"
        try:
            self._view_model.save_project_as(file_path)
            self._show_saved()
        except Exception:
            ErrorDialog.show_exception(
                message=tr("error.save_project", "Failed to save project."),
                parent=self._parent,
            )

    def save_before_update(self) -> None:
        """Best-effort save or recovery snapshot before app replacement."""
        if self._view_model.current_file is not None:
            try:
                self._view_model.save_project()
            except Exception:
                pass
            return

        try:
            recovery = Path(tempfile.gettempdir()) / "count-and-design-update"
            recovery.mkdir(parents=True, exist_ok=True)
            recovery_file = recovery / f"pre_update_recovery-{os.getpid()}.json"
            atomic_write_json(
                recovery_file,
                self._view_model.build_recovery_payload(),
            )
        except Exception:
            pass

    def import_and_merge(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self._parent,
            tr("dialog.import_merge_title", "Select Project to Merge"),
            "",
            tr("dialog.file_filter", "JSON Files (*.json);;All Files (*)"),
        )
        if not file_path:
            return
        try:
            imported = Project.from_json(file_path)
            merge_service = MergeService(self._view_model.project, imported)
            merge_service.take_snapshot()
            window = MergeWindow(merge_service, self._parent)
            window.merge_finished.connect(self._view_model.refresh_all_tabs)
            window.exec()
        except Exception:
            ErrorDialog.show_exception(
                message=tr(
                    "error.import_merge",
                    "Failed to import and merge project.",
                ),
                parent=self._parent,
            )

    def open_data_fill(self) -> None:
        try:
            prepared = self._view_model.prepare_data_fill()
        except DataFillError as exc:
            messages = {
                "no_real_invs": tr(
                    "data_fill.error.no_invs",
                    "No real INV is available.",
                ),
                "no_sample_nodes": tr(
                    "data_fill.error.no_nodes",
                    "The real INVs contain no Nodes.",
                ),
            }
            QMessageBox.warning(
                self._parent,
                tr("data_fill.error_title", "Data Fill Failed"),
                messages.get(exc.code, str(exc)),
            )
            return
        DataFillDialog(self._view_model, prepared, self._parent).exec()

    def _show_saved(self) -> None:
        QMessageBox.information(
            self._parent,
            tr("dialog.saved_title", "Saved"),
            tr("dialog.saved_msg", "Project saved successfully"),
        )

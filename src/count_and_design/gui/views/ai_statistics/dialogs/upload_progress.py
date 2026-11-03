"""Upload progress dialog for AI statistics."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from .....i18n import tr


class UploadProgressDialog(QDialog):
    """Show upload progress and allow the user to cancel."""

    cancelled = Signal()

    def __init__(self, filename: str, parent=None) -> None:
        super().__init__(parent)
        self._filename = filename
        self._user_closed = False
        self.setWindowTitle(
            tr("ai_statistics.uploading.title", "Uploading PDF")
        )
        self.setModal(True)
        self.setMinimumWidth(480)
        self._setup_ui()
        self._apply_style()

    def set_progress(self, value: int) -> None:
        self._progress.setValue(max(0, min(100, value)))

    def close_without_cancel(self) -> None:
        self._user_closed = True
        self.close()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)

        self._title_label = QLabel(
            tr("ai_statistics.uploading.title", "Uploading PDF")
        )
        self._title_label.setObjectName("dialogTitle")
        root.addWidget(self._title_label)

        self._filename_label = QLabel(self._filename)
        self._filename_label.setObjectName("dialogFilename")
        self._filename_label.setWordWrap(True)
        root.addWidget(self._filename_label)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(8)
        progress_row.addWidget(self._progress, 1)
        self._cancel_button = QPushButton(
            tr("ai_statistics.uploading.cancel", "Cancel")
        )
        self._cancel_button.clicked.connect(self._emit_cancelled)
        progress_row.addWidget(self._cancel_button)
        root.addLayout(progress_row)

    def _emit_cancelled(self) -> None:
        self._user_closed = True
        self.cancelled.emit()
        self.reject()

    def closeEvent(self, event) -> None:
        if not self._user_closed:
            self.cancelled.emit()
        super().closeEvent(event)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog {
                background: #ffffff;
            }
            QLabel#dialogTitle {
                color: #243447;
                font-size: 18px;
                font-weight: 700;
            }
            QLabel#dialogFilename {
                color: #64748b;
                font-size: 13px;
            }
            QProgressBar {
                background: #e2e8f0;
                border: none;
                border-radius: 4px;
            }
            QProgressBar::chunk {
                background: #4676f2;
                border-radius: 4px;
            }
            QPushButton {
                min-height: 28px;
                padding: 0 14px;
                border: 1px solid #d9e0e7;
                border-radius: 3px;
                background: #ffffff;
                color: #334155;
            }
            """
        )

    def retranslate(self) -> None:
        self.setWindowTitle(
            tr("ai_statistics.uploading.title", "Uploading PDF")
        )
        self._title_label.setText(
            tr("ai_statistics.uploading.title", "Uploading PDF")
        )
        self._cancel_button.setText(
            tr("ai_statistics.uploading.cancel", "Cancel")
        )

"""Upload failure dialog for AI statistics."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .....i18n import tr
from ..helpers.pdf_upload import PdfUploadError

_ERROR_KEYS: dict[PdfUploadError, tuple[str, str]] = {
    PdfUploadError.UNSUPPORTED_FILE_TYPE: (
        "ai_statistics.upload_error.unsupported_file_type",
        "Unsupported file type. Please upload a PDF file.",
    ),
    PdfUploadError.PDF_ENCRYPTED: (
        "ai_statistics.upload_error.pdf_encrypted",
        "The PDF is encrypted or password protected.",
    ),
    PdfUploadError.FILE_TOO_LARGE: (
        "ai_statistics.upload_error.file_too_large",
        "The file exceeds the maximum upload size.",
    ),
    PdfUploadError.NO_READABLE_PAGES: (
        "ai_statistics.upload_error.no_readable_pages",
        "No readable pages were found in the PDF.",
    ),
    PdfUploadError.PAGE_RENDER_FAILED: (
        "ai_statistics.upload_error.page_render_failed",
        "Page rendering failed. The PDF may be corrupted.",
    ),
    PdfUploadError.UPLOAD_INTERRUPTED: (
        "ai_statistics.upload_error.upload_interrupted",
        "Upload was interrupted. Please try again.",
    ),
}


class UploadFailedDialog(QDialog):
    """Show one upload error with retry and cancel actions."""

    def __init__(
        self,
        error: PdfUploadError,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._error = error
        self._retry = False
        self.setWindowTitle(
            tr("ai_statistics.upload_failed.title", "Upload Failed")
        )
        self.setModal(True)
        self.setMinimumWidth(520)
        self._setup_ui()
        self._apply_style()

    @property
    def retry_requested(self) -> bool:
        return self._retry

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)

        title = QLabel(tr("ai_statistics.upload_failed.title", "Upload Failed"))
        title.setObjectName("dialogTitle")
        root.addWidget(title)

        intro = QLabel(
            tr(
                "ai_statistics.upload_failed.intro",
                "The file could not be uploaded. Please check the reason below.",
            )
        )
        intro.setObjectName("dialogIntro")
        intro.setWordWrap(True)
        root.addWidget(intro)

        error_box = QFrame()
        error_box.setObjectName("errorBox")
        error_layout = QVBoxLayout(error_box)
        error_layout.setContentsMargins(14, 12, 14, 12)
        error_layout.setSpacing(6)

        key, fallback = _ERROR_KEYS[self._error]
        self._error_label = QLabel(tr(key, fallback))
        self._error_label.setObjectName("errorPrimary")
        self._error_label.setWordWrap(True)
        error_layout.addWidget(self._error_label)

        hint = QLabel(
            tr(
                "ai_statistics.upload_failed.retry_hint",
                "Choose a valid PDF file and upload again.",
            )
        )
        hint.setObjectName("errorHint")
        hint.setWordWrap(True)
        error_layout.addWidget(hint)
        root.addWidget(error_box)

        actions = QHBoxLayout()
        actions.addStretch()
        self._cancel_button = QPushButton(
            tr("ai_statistics.upload_failed.cancel", "Cancel")
        )
        self._cancel_button.clicked.connect(self.reject)
        actions.addWidget(self._cancel_button)
        self._retry_button = QPushButton(
            tr(
                "ai_statistics.upload_failed.choose_another",
                "Choose Another File",
            )
        )
        self._retry_button.setObjectName("primaryButton")
        self._retry_button.clicked.connect(self._choose_another)
        actions.addWidget(self._retry_button)
        root.addLayout(actions)

    def _choose_another(self) -> None:
        self._retry = True
        self.accept()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog {
                background: #ffffff;
            }
            QLabel#dialogTitle {
                color: #c62828;
                font-size: 18px;
                font-weight: 700;
            }
            QLabel#dialogIntro {
                color: #475569;
                font-size: 13px;
            }
            QFrame#errorBox {
                background: #fff5f5;
                border: 1px solid #fecaca;
                border-radius: 4px;
            }
            QLabel#errorPrimary {
                color: #c62828;
                font-size: 13px;
            }
            QLabel#errorHint {
                color: #b45309;
                font-size: 12px;
            }
            QPushButton {
                min-height: 28px;
                padding: 0 14px;
                border: 1px solid #d9e0e7;
                border-radius: 3px;
                background: #ffffff;
                color: #334155;
            }
            QPushButton#primaryButton {
                background: #4676f2;
                color: #ffffff;
                border: 1px solid #3a66d8;
            }
            QPushButton#primaryButton:hover {
                background: #3a66d8;
            }
            """
        )

    def retranslate(self) -> None:
        self.setWindowTitle(
            tr("ai_statistics.upload_failed.title", "Upload Failed")
        )
        key, fallback = _ERROR_KEYS[self._error]
        self._error_label.setText(tr(key, fallback))
        self._cancel_button.setText(
            tr("ai_statistics.upload_failed.cancel", "Cancel")
        )
        self._retry_button.setText(
            tr(
                "ai_statistics.upload_failed.choose_another",
                "Choose Another File",
            )
        )

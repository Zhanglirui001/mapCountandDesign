"""Error dialog for displaying traceback information to the user."""

from __future__ import annotations

import traceback
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ...i18n import tr

if TYPE_CHECKING:
    pass


class ErrorDialog(QDialog):
    """A dialog that displays an error message with traceback and a copy button."""

    def __init__(
        self,
        message: str = "",
        detail: str = "",
        parent: QDialog | None = None,
    ) -> None:
        """Initialize the error dialog.

        Args:
            message: A short description of what went wrong.
            detail: The full traceback or error detail text.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self._message = message
        self._detail = detail
        self._setup_ui()

    def _setup_ui(self) -> None:
        """Build the dialog UI."""
        self.setWindowTitle(tr("error_dialog.title", "Error"))
        self.resize(700, 400)
        self.setMinimumSize(500, 300)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # -- Header / summary label -----------------------------------------
        header = QLabel(
            self._message or tr("error_dialog.default_message", "An unexpected error occurred.")
        )
        header.setWordWrap(True)
        header.setStyleSheet(
            "QLabel { font-weight: bold; font-size: 13px; color: #c0392b; }"
        )
        layout.addWidget(header)

        # -- Traceback viewer ------------------------------------------------
        traceback_label = QLabel(tr("error_dialog.detail_label", "Details:"))
        layout.addWidget(traceback_label)

        self._text_edit = QPlainTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setPlainText(self._detail)
        self._text_edit.setStyleSheet(
            "QPlainTextEdit {"
            "  font-family: 'Consolas', 'Courier New', monospace;"
            "  font-size: 12px;"
            "  background-color: #fdf2f2;"
            "  border: 1px solid #e0b4b4;"
            "  border-radius: 4px;"
            "}"
        )
        layout.addWidget(self._text_edit, 1)

        # -- Button row ------------------------------------------------------
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        copy_btn = QPushButton(tr("error_dialog.copy", "Copy to Clipboard"))
        copy_btn.clicked.connect(self._copy_to_clipboard)
        button_layout.addWidget(copy_btn)

        close_btn = QPushButton(tr("error_dialog.close", "Close"))
        close_btn.clicked.connect(self.close)
        close_btn.setDefault(True)
        button_layout.addWidget(close_btn)

        layout.addLayout(button_layout)

    def _copy_to_clipboard(self) -> None:
        """Copy the traceback detail to the system clipboard."""
        from PySide6.QtWidgets import QApplication

        text = self._text_edit.toPlainText()
        QApplication.clipboard().setText(text)

    # -- Static helpers -----------------------------------------------------

    @staticmethod
    def from_exception(
        message: str = "",
        parent: QDialog | None = None,
    ) -> ErrorDialog:
        """Create an ErrorDialog from the currently active exception.

        Uses ``sys.exc_info()`` to capture the current traceback.

        Args:
            message: Optional short description.
            parent: Optional parent widget.

        Returns:
            A ready-to-show ErrorDialog.
        """
        detail = "".join(traceback.format_exc())
        return ErrorDialog(message=message, detail=detail, parent=parent)

    @staticmethod
    def show_exception(
        message: str = "",
        parent: QDialog | None = None,
    ) -> ErrorDialog:
        """Convenience method: create from current exception and show immediately.

        Args:
            message: Optional short description.
            parent: Optional parent widget.

        Returns:
            The ErrorDialog instance (already shown as modal).
        """
        dlg = ErrorDialog.from_exception(message=message, parent=parent)
        dlg.exec()
        return dlg

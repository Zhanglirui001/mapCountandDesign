"""Upload success dialog for AI statistics."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .....i18n import tr


class UploadReadyDialog(QDialog):
    """Confirm that a PDF uploaded successfully and is ready for AI."""

    def __init__(
        self,
        filename: str,
        page_count: int,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._filename = filename
        self._page_count = page_count
        self.setWindowTitle(
            tr("ai_statistics.upload_ready.title", "PDF Ready")
        )
        self.setModal(True)
        self.setMinimumWidth(480)
        self._setup_ui()
        self._apply_style()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)

        self._title_label = QLabel(
            tr("ai_statistics.upload_ready.title", "PDF Ready")
        )
        self._title_label.setObjectName("dialogTitle")
        root.addWidget(self._title_label)

        info_box = QFrame()
        info_box.setObjectName("infoBox")
        info_layout = QVBoxLayout(info_box)
        info_layout.setContentsMargins(14, 12, 14, 12)
        info_layout.setSpacing(6)

        self._filename_label = QLabel(self._filename)
        self._filename_label.setObjectName("infoFilename")
        self._filename_label.setWordWrap(True)
        info_layout.addWidget(self._filename_label)

        self._page_count_label = QLabel(
            tr("ai_statistics.upload_ready.pages", "{count} pages").format(
                count=self._page_count
            )
        )
        self._page_count_label.setObjectName("infoMeta")
        info_layout.addWidget(self._page_count_label)

        self._message_label = QLabel(
            tr(
                "ai_statistics.upload_ready.message",
                "Ready to run AI model",
            )
        )
        self._message_label.setObjectName("infoMessage")
        self._message_label.setWordWrap(True)
        info_layout.addWidget(self._message_label)
        root.addWidget(info_box)

        actions = QHBoxLayout()
        actions.addStretch()
        self._ok_button = QPushButton(tr("common.ok", "OK"))
        self._ok_button.setObjectName("primaryButton")
        self._ok_button.clicked.connect(self.accept)
        actions.addWidget(self._ok_button)
        root.addLayout(actions)

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
            QFrame#infoBox {
                background: #f8fafc;
                border: 1px solid #dfe4ea;
                border-radius: 4px;
            }
            QLabel#infoFilename {
                color: #64748b;
                font-size: 13px;
            }
            QLabel#infoMeta {
                color: #64748b;
                font-size: 13px;
            }
            QLabel#infoMessage {
                color: #1f7a45;
                font-size: 13px;
                font-weight: 600;
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
            tr("ai_statistics.upload_ready.title", "PDF Ready")
        )
        self._title_label.setText(
            tr("ai_statistics.upload_ready.title", "PDF Ready")
        )
        self._page_count_label.setText(
            tr("ai_statistics.upload_ready.pages", "{count} pages").format(
                count=self._page_count
            )
        )
        self._message_label.setText(
            tr(
                "ai_statistics.upload_ready.message",
                "Ready to run AI model",
            )
        )
        self._ok_button.setText(tr("common.ok", "OK"))

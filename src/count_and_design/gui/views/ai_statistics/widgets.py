"""Shared widgets for AI statistics workflow pages."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ....i18n import tr


def panel_header() -> tuple[QFrame, QLabel, QLabel]:
    """Build a standard panel header and return its title/meta labels."""
    header = QFrame()
    header.setObjectName("panelHeader")
    header.setFixedHeight(34)
    layout = QHBoxLayout(header)
    layout.setContentsMargins(10, 0, 10, 0)
    title = QLabel()
    title.setObjectName("panelTitle")
    meta = QLabel()
    meta.setObjectName("panelMeta")
    layout.addWidget(title)
    layout.addStretch()
    layout.addWidget(meta)
    return header, title, meta


def empty_panel() -> tuple[QFrame, QLabel, QLabel, QLabel]:
    """Build a standard panel containing a centered empty-state label."""
    panel = QFrame()
    panel.setObjectName("panel")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    header, title, meta = panel_header()
    layout.addWidget(header)
    hint = QLabel()
    hint.setObjectName("emptyHint")
    hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(hint, 1)
    return panel, title, meta, hint


class WorkflowStagePage(QWidget):
    """Workflow page with a bottom-right confirm action."""

    confirm_clicked = Signal()

    def __init__(
        self,
        object_name: str,
        confirm_key: str,
        confirm_fallback: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self._confirm_key = confirm_key
        self._confirm_fallback = confirm_fallback
        self.confirm_button = QPushButton()
        self.confirm_button.setObjectName("confirmButton")
        self.confirm_button.clicked.connect(self.confirm_clicked.emit)

    def append_confirm_footer(self, root: QVBoxLayout) -> None:
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 8, 0, 0)
        footer.addStretch()
        footer.addWidget(self.confirm_button)
        root.addLayout(footer)

    def retranslate_confirm(self) -> None:
        self.confirm_button.setText(
            tr(self._confirm_key, self._confirm_fallback)
        )


class StagePlaceholder(QWidget):
    """Simple distinct placeholder used until a stage receives real results."""

    def __init__(self, object_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel, self.title_label, self.meta_label, self.hint_label = (
            empty_panel()
        )
        layout.addWidget(self.panel)

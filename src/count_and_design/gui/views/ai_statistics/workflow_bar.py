"""Workflow navigation bar for AI statistics."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QWidget,
)

from ....i18n import tr


class WorkflowBar(QFrame):
    """Display the four recognition stages and their statuses."""

    stage_clicked = Signal(int)

    STAGES = (
        ("ai_statistics.stage.page_classification", "Page Classification"),
        ("ai_statistics.stage.info_recognition", "Info Recognition"),
        ("ai_statistics.stage.element_recognition", "Element Recognition"),
        ("ai_statistics.stage.validation", "Validation"),
    )
    STATUS_VARIANTS = {
        "ai_statistics.status.waiting": "waiting",
        "ai_statistics.status.ready": "ready",
        "ai_statistics.status.result_ready": "result_ready",
        "ai_statistics.status.confirmed": "confirmed",
        "ai_statistics.status.needs_confirm": "needs_confirm",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("workflowCard")
        self._current_stage: int | None = None
        self._clickable_through: int | None = None
        self._status_keys = [
            ("ai_statistics.status.waiting", "waiting")
            for _stage in self.STAGES
        ]
        self.stage_titles: list[QLabel] = []
        self.stage_badges: list[QLabel] = []
        self.stage_cards: list[QFrame] = []
        self._setup_ui()
        self.retranslate()

    @property
    def current_stage(self) -> int | None:
        return self._current_stage

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)
        self.title_label = QLabel()
        self.title_label.setObjectName("sectionTitle")
        layout.addWidget(self.title_label)

        for index, _stage in enumerate(self.STAGES):
            if index:
                arrow = QLabel("→")
                arrow.setObjectName("muted")
                arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(arrow)

            card = QFrame()
            card.setObjectName("stageCard")
            card.setProperty("active", False)
            card.setProperty("clickable", False)
            card.installEventFilter(self)
            card.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(12, 7, 10, 7)
            title = QLabel()
            badge = QLabel()
            badge.setObjectName("statusBadge")
            card_layout.addWidget(title)
            card_layout.addStretch()
            card_layout.addWidget(badge)
            self.stage_cards.append(card)
            self.stage_titles.append(title)
            self.stage_badges.append(badge)
            layout.addWidget(card, 1)

    def set_current_stage(self, index: int | None) -> None:
        if index is not None and not 0 <= index < len(self.STAGES):
            raise ValueError(f"Invalid workflow stage index: {index}")
        self._current_stage = index
        self._apply_stage_highlight()

    def clear_current_stage(self) -> None:
        self.set_current_stage(None)

    def set_clickable_through(self, index: int | None) -> None:
        """Allow clicking stages ``0..index``; ``None`` disables all."""
        if index is not None and not 0 <= index < len(self.STAGES):
            raise ValueError(f"Invalid workflow stage index: {index}")
        self._clickable_through = index
        self._apply_stage_clickability()

    def _is_stage_clickable(self, index: int) -> bool:
        return (
            self._clickable_through is not None
            and index <= self._clickable_through
        )

    def _apply_stage_clickability(self) -> None:
        for card_index, card in enumerate(self.stage_cards):
            clickable = self._is_stage_clickable(card_index)
            card.setProperty("clickable", clickable)
            card.setCursor(
                Qt.CursorShape.PointingHandCursor
                if clickable
                else Qt.CursorShape.ArrowCursor
            )
            card.style().unpolish(card)
            card.style().polish(card)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                try:
                    index = self.stage_cards.index(obj)
                except ValueError:
                    return super().eventFilter(obj, event)
                if self._is_stage_clickable(index):
                    self.stage_clicked.emit(index)
                    return True
        return super().eventFilter(obj, event)

    def _apply_stage_highlight(self) -> None:
        for card_index, card in enumerate(self.stage_cards):
            active = (
                self._current_stage is not None
                and card_index == self._current_stage
            )
            card.setProperty("active", active)
            card.style().unpolish(card)
            card.style().polish(card)

    def set_status(
        self,
        index: int,
        translation_key: str,
        fallback: str,
    ) -> None:
        if not 0 <= index < len(self.STAGES):
            raise ValueError(f"Invalid workflow stage index: {index}")
        self._status_keys[index] = (translation_key, fallback)
        badge = self.stage_badges[index]
        badge.setText(tr(translation_key, fallback))
        self._apply_badge_style(badge, translation_key)

    def _apply_badge_style(self, badge: QLabel, translation_key: str) -> None:
        status = self.STATUS_VARIANTS.get(translation_key, "waiting")
        badge.setProperty("status", status)
        badge.style().unpolish(badge)
        badge.style().polish(badge)

    def retranslate(self) -> None:
        self.title_label.setText(tr("ai_statistics.workflow", "Workflow"))
        for label, (key, fallback) in zip(self.stage_titles, self.STAGES):
            label.setText(tr(key, fallback))
        for badge, (key, fallback) in zip(
            self.stage_badges, self._status_keys
        ):
            badge.setText(tr(key, fallback))
            self._apply_badge_style(badge, key)

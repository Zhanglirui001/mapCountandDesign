"""Reusable multi-selection widgets with delayed block reordering."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPoint, QRect, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QApplication, QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem

from ...i18n import tr


_ID_ROLE = Qt.ItemDataRole.UserRole
_TYPE_ROLE = Qt.ItemDataRole.UserRole + 1


class _OrderedSelectionMixin:
    """Mouse interaction shared by list and tree item views."""

    LONG_PRESS_MS = 300

    def _initialize_ordered_selection(self) -> None:
        self.setSelectionMode(self.SelectionMode.ExtendedSelection)
        self._selection_anchor_id: str | None = None
        self._internal_selection_change = False
        self._native_click_active = False
        self._press_item: Any | None = None
        self._press_pos = QPoint()
        self._press_modifiers = Qt.KeyboardModifier.NoModifier
        self._selected_at_press: tuple[str, ...] = ()
        self._ctrl_dragging = False
        self._ctrl_drag_scope: object | None = None
        self._last_ctrl_item: Any | None = None
        self._reorder_armed = False
        self._reordering = False
        self._moved_before_arm = False
        self._drop_item: Any | None = None
        self._drop_after = False
        self._drop_valid = False
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(self.LONG_PRESS_MS)
        self._long_press_timer.timeout.connect(self._arm_reorder)

    def apply_selected_ids(self, item_ids: tuple[str, ...]) -> None:
        wanted = set(item_ids)
        self.blockSignals(True)
        self.clearSelection()
        first = None
        for item in self._ordered_items():
            selected = self._item_id(item) in wanted
            item.setSelected(selected)
            if selected and first is None:
                first = item
        if first is not None:
            self.setCurrentItem(first)
            for item in self._ordered_items():
                if self._item_id(item) in wanted:
                    item.setSelected(True)
        self.blockSignals(False)
        if self._selection_anchor_id not in {self._item_id(item) for item in self._ordered_items()}:
            self._selection_anchor_id = self._item_id(first) if first is not None else None

    def selected_ids(self) -> tuple[str, ...]:
        return tuple(
            item_id
            for item in self._ordered_items()
            if item.isSelected() and (item_id := self._item_id(item)) is not None
        )

    @property
    def internal_selection_change(self) -> bool:
        return self._internal_selection_change

    def mousePressEvent(self, event: QMouseEvent) -> None:
        item = self.itemAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.RightButton:
            if item is not None and not item.isSelected():
                self._replace_selection((item,))
                self._selection_anchor_id = self._item_id(item)
                self._emit_selection()
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        if self._is_native_click_target(event.position().toPoint()):
            self._reset_pointer_state()
            self._native_click_active = True
            super().mousePressEvent(event)
            return

        self._reset_pointer_state()
        self._press_item = item
        self._press_pos = event.position().toPoint()
        self._press_modifiers = event.modifiers()
        self._selected_at_press = self.selected_ids()
        if item is not None and not (
            event.modifiers()
            & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        ):
            self._long_press_timer.start()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._native_click_active:
            super().mouseMoveEvent(event)
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton) or self._press_item is None:
            super().mouseMoveEvent(event)
            return
        pos = event.position().toPoint()
        if self._press_modifiers & Qt.KeyboardModifier.ControlModifier:
            if pos != self._press_pos:
                self._continue_ctrl_drag(pos)
            event.accept()
            return

        if self._press_modifiers & Qt.KeyboardModifier.ShiftModifier:
            event.accept()
            return

        distance = (pos - self._press_pos).manhattanLength()
        if not self._reorder_armed:
            if distance >= QApplication.startDragDistance():
                self._moved_before_arm = True
                self._long_press_timer.stop()
            event.accept()
            return

        if pos != self._press_pos:
            self._reordering = True
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            self._update_drop_target(pos)
            self.viewport().update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._native_click_active:
            self._native_click_active = False
            super().mouseReleaseEvent(event)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        self._long_press_timer.stop()
        item = self.itemAt(event.position().toPoint())
        if self._ctrl_dragging:
            self._emit_selection()
        elif self._reordering:
            if self._drop_valid:
                target_id = self._item_id(self._drop_item) if self._drop_item is not None else None
                self.reorder_requested.emit(target_id, self._drop_after)
        elif not self._moved_before_arm:
            if self._press_modifiers & Qt.KeyboardModifier.ShiftModifier:
                self._apply_shift_click(item)
            elif self._press_modifiers & Qt.KeyboardModifier.ControlModifier:
                self._apply_ctrl_click(item)
            else:
                self._apply_plain_click(item)
        self._reset_pointer_state()
        self.viewport().update()
        event.accept()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if not self._reorder_armed:
            return
        painter = QPainter(self.viewport())
        hint = tr("ordering.drag_to_reorder", "Drag to reorder")
        metrics = painter.fontMetrics()
        hint_width = metrics.horizontalAdvance(hint) + 18
        hint_height = metrics.height() + 8
        hint_rect = QRect(
            max(6, self.viewport().width() - hint_width - 8),
            8,
            hint_width,
            hint_height,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.palette().highlight())
        painter.drawRoundedRect(hint_rect, 5, 5)
        painter.setPen(self.palette().highlightedText().color())
        painter.drawText(hint_rect, Qt.AlignmentFlag.AlignCenter, hint)

        if not (self._reordering and self._drop_valid):
            return
        color = self.palette().color(self.foregroundRole())
        if color == QColor():
            color = QColor("#2f7ed8")
        pen = painter.pen()
        pen.setColor(color)
        pen.setWidth(2)
        painter.setPen(pen)
        if self._drop_item is None:
            y = max(1, self.viewport().height() - 2)
        else:
            rect = self.visualItemRect(self._drop_item)
            y = rect.bottom() if self._drop_after else rect.top()
        painter.drawLine(2, y, max(2, self.viewport().width() - 2), y)

    def _arm_reorder(self) -> None:
        if self._press_item is None or self._moved_before_arm:
            return
        if not self._press_item.isSelected():
            return
        self._reorder_armed = True
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        self.viewport().update()

    def _continue_ctrl_drag(self, pos: QPoint) -> None:
        if not self._ctrl_dragging:
            scope = self._selection_scope_for_gesture(self._press_item)
            if scope is None:
                return
            self._ctrl_dragging = True
            self._ctrl_drag_scope = scope
            self._toggle_item(self._press_item)
            self._last_ctrl_item = self._press_item
        current = self.itemAt(pos)
        if current is self._last_ctrl_item:
            return
        self._last_ctrl_item = current
        if current is not None and self._item_scope(current) == self._ctrl_drag_scope:
            self._toggle_item(current)

    def _apply_plain_click(self, item: Any | None) -> None:
        if item is None:
            return
        self._replace_selection((item,))
        self._selection_anchor_id = self._item_id(item)
        self._emit_selection()

    def _apply_ctrl_click(self, item: Any | None) -> None:
        if item is None or not self._can_extend_to(item):
            return
        self._internal_selection_change = True
        try:
            if not item.isSelected():
                item.setSelected(True)
            item.setSelected(True)
        finally:
            self._internal_selection_change = False
        self._selection_anchor_id = self._item_id(item)
        self._emit_selection()

    def _apply_shift_click(self, item: Any | None) -> None:
        if item is None or self._selection_anchor_id is None:
            return
        anchor = self._find_item(self._selection_anchor_id)
        if anchor is None or self._item_scope(anchor) != self._item_scope(item):
            return
        items = self._range_items(anchor, item)
        if not items:
            return
        self._replace_selection(tuple(items))
        self._emit_selection()

    def _selection_scope_for_gesture(self, start: Any) -> object | None:
        selected = [item for item in self._ordered_items() if item.isSelected()]
        if selected and self._item_scope(selected[0]) != self._item_scope(start):
            return None
        return self._item_scope(start)

    def _can_extend_to(self, item: Any) -> bool:
        selected = [entry for entry in self._ordered_items() if entry.isSelected()]
        return not selected or self._item_scope(selected[0]) == self._item_scope(item)

    def _update_drop_target(self, pos: QPoint) -> None:
        item = self.itemAt(pos)
        if item is None:
            self._drop_item = None
            self._drop_after = True
            self._drop_valid = True
            return
        if item.isSelected() or not self._can_drop_on(item):
            self._drop_valid = False
            return
        rect = self.visualItemRect(item)
        self._drop_item = item
        self._drop_after = pos.y() >= rect.center().y()
        self._drop_valid = True

    def _can_drop_on(self, item: Any) -> bool:
        selected = [entry for entry in self._ordered_items() if entry.isSelected()]
        return bool(selected) and self._item_scope(selected[0]) == self._item_scope(item)

    def _toggle_item(self, item: Any) -> None:
        item.setSelected(not item.isSelected())

    def _replace_selection(self, items: tuple[Any, ...]) -> None:
        self._internal_selection_change = True
        try:
            self.clearSelection()
            if not items:
                return
            self.setCurrentItem(items[0])
            for item in items:
                item.setSelected(True)
        finally:
            self._internal_selection_change = False

    def _emit_selection(self) -> None:
        self.selection_requested.emit(self.selected_ids())

    def _reset_pointer_state(self) -> None:
        self._long_press_timer.stop()
        self._press_item = None
        self._ctrl_dragging = False
        self._ctrl_drag_scope = None
        self._last_ctrl_item = None
        self._reorder_armed = False
        self._reordering = False
        self._moved_before_arm = False
        self._drop_item = None
        self._drop_after = False
        self._drop_valid = False
        self.viewport().unsetCursor()

    def _find_item(self, item_id: str) -> Any | None:
        return next(
            (item for item in self._ordered_items() if self._item_id(item) == item_id),
            None,
        )

    def _is_native_click_target(self, _pos: QPoint) -> bool:
        return False

    @staticmethod
    def _item_id(item: Any | None) -> str | None:
        return item.data(_ID_ROLE) if item is not None else None


class OrderedListWidget(_OrderedSelectionMixin, QListWidget):
    selection_requested = Signal(object)
    reorder_requested = Signal(object, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._initialize_ordered_selection()

    def _ordered_items(self) -> list[QListWidgetItem]:
        return [self.item(index) for index in range(self.count())]

    @staticmethod
    def _item_scope(_item: QListWidgetItem) -> str:
        return "list"

    def _range_items(
        self, anchor: QListWidgetItem, target: QListWidgetItem
    ) -> list[QListWidgetItem]:
        start, end = sorted((self.row(anchor), self.row(target)))
        return [self.item(index) for index in range(start, end + 1)]


class OrderedTreeWidget(_OrderedSelectionMixin, QTreeWidget):
    selection_requested = Signal(object)
    reorder_requested = Signal(object, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._initialize_ordered_selection()

    def _ordered_items(self) -> list[QTreeWidgetItem]:
        result: list[QTreeWidgetItem] = []

        def visit(item: QTreeWidgetItem) -> None:
            result.append(item)
            for index in range(item.childCount()):
                visit(item.child(index))

        for index in range(self.topLevelItemCount()):
            visit(self.topLevelItem(index))
        return result

    @staticmethod
    def _item_id(item: QTreeWidgetItem | None) -> str | None:
        return item.data(0, _ID_ROLE) if item is not None else None

    @staticmethod
    def _item_scope(item: QTreeWidgetItem) -> tuple[object, object]:
        parent = item.parent()
        return item.data(0, _TYPE_ROLE), id(parent) if parent is not None else "root"

    def _range_items(
        self, anchor: QTreeWidgetItem, target: QTreeWidgetItem
    ) -> list[QTreeWidgetItem]:
        parent = anchor.parent()
        if parent is None:
            siblings = [self.topLevelItem(index) for index in range(self.topLevelItemCount())]
        else:
            siblings = [parent.child(index) for index in range(parent.childCount())]
        try:
            start, end = sorted((siblings.index(anchor), siblings.index(target)))
        except ValueError:
            return []
        return siblings[start : end + 1]

    def _is_native_click_target(self, pos: QPoint) -> bool:
        """Let Qt handle a click on the tree's disclosure indicator."""

        item = self.itemAt(pos)
        if item is None or item.childCount() == 0:
            return False
        rect = self.visualItemRect(item)
        return rect.left() - self.indentation() <= pos.x() < rect.left()

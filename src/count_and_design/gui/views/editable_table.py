"""Table widget that opens cell editors on the first click."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLineEdit,
    QTableWidget,
    QWidget,
)


class ClickToEditTableWidget(QTableWidget):
    """Table whose editable cells behave like plain text fields.

    Qt's default triggers select a cell on the first click and only open
    its editor on a double click, with the whole text selected; placing the
    caret at one character then costs three clicks. This widget opens the
    editor on the first left click instead, puts the caret at the clicked
    character and forwards the rest of the gesture to the editor, so
    clicks, drags and double clicks match a QLineEdit. Ctrl/Shift clicks
    stay selection gestures and never start an edit.
    """

    def __init__(
        self,
        rows: int = 0,
        columns: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(rows, columns, parent)
        # Mouse editing is handled below; keep the keyboard triggers.
        self.setEditTriggers(
            QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self._drag_editor: QLineEdit | None = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        point = event.position().toPoint()
        # Selection updates and the commit of a previously open editor both
        # happen here. Committing may rebuild the table rows, so the index
        # must be resolved only after the base class is done.
        super().mousePressEvent(event)
        index = self.indexAt(point)
        if not self._starts_editing(event, index):
            return
        before = set(self.viewport().findChildren(QLineEdit))
        self.edit(index)
        if self.state() != QAbstractItemView.State.EditingState:
            return
        editor = self._new_editor(before)
        if editor is None:
            return
        self._drag_editor = editor
        local = editor.mapFrom(self.viewport(), point)
        # A fresh editor starts with its whole text selected; move the
        # caret to the clicked character instead.
        editor.setCursorPosition(editor.cursorPositionAt(local))
        self._forward_mouse_event(
            editor,
            event,
            QEvent.Type.MouseButtonPress,
            local,
        )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        editor = self._live_drag_editor()
        if editor is not None and event.buttons() & Qt.MouseButton.LeftButton:
            local = editor.mapFrom(self.viewport(), event.position().toPoint())
            self._forward_mouse_event(editor, event, QEvent.Type.MouseMove, local)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        editor = self._live_drag_editor()
        self._drag_editor = None
        if editor is not None:
            local = editor.mapFrom(self.viewport(), event.position().toPoint())
            self._forward_mouse_event(
                editor,
                event,
                QEvent.Type.MouseButtonRelease,
                local,
            )
        super().mouseReleaseEvent(event)

    def _starts_editing(self, event: QMouseEvent, index) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier
        ):
            return False
        if self.state() == QAbstractItemView.State.EditingState:
            return False
        return index.isValid() and bool(
            index.flags() & Qt.ItemFlag.ItemIsEditable
        )

    def _new_editor(self, existing: set[QLineEdit]) -> QLineEdit | None:
        """Return the editor just created, ignoring editors being replaced.

        The previous editor is hidden first and destroyed later, so looking
        up a child editor by type would keep returning the stale one.
        """
        for editor in self.viewport().findChildren(QLineEdit):
            if editor not in existing:
                return editor
        return None

    def _live_drag_editor(self) -> QLineEdit | None:
        """Return the editor handling the current drag, if it still exists.

        A table rebuild can destroy an editor while the mouse is still
        down; sending it events afterwards would crash.
        """

        editor = self._drag_editor
        if editor is None:
            return None
        try:
            editor.isVisible()
        except RuntimeError:
            self._drag_editor = None
            return None
        return editor

    @staticmethod
    def _forward_mouse_event(
        editor: QLineEdit,
        event: QMouseEvent,
        event_type: QEvent.Type,
        local_pos,
    ) -> None:
        forwarded = QMouseEvent(
            event_type,
            local_pos,
            editor.mapToGlobal(local_pos),
            event.button(),
            event.buttons(),
            event.modifiers(),
        )
        QApplication.sendEvent(editor, forwarded)

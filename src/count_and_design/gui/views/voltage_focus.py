"""Shared semantic field registration and persistent voltage-error highlighting."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QScrollArea,
    QWidget,
)


_ERROR_STYLE = "border: 2px solid #d93025; background-color: #fff2f0;"


class VoltageFocusWidget(QWidget):
    """A tab that can resolve semantic field keys to freshly rendered widgets."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._voltage_fields: dict[str, list[QWidget]] = {}
        self._voltage_focus_keys: tuple[str, ...] = ()
        self._voltage_primary_field = ""

    def begin_voltage_field_render(self) -> None:
        self._voltage_fields = {}

    def register_voltage_field(self, key: str, widget: QWidget) -> QWidget:
        self._voltage_fields.setdefault(key, []).append(widget)
        return widget

    def finish_voltage_field_render(self) -> None:
        if self._voltage_focus_keys:
            QTimer.singleShot(0, self._apply_voltage_focus)

    def focus_voltage_fields(self, keys: tuple[str, ...], primary: str = "") -> None:
        self.clear_voltage_focus(reset_request=False)
        self._voltage_focus_keys = keys or ("__object__",)
        self._voltage_primary_field = primary or self._voltage_focus_keys[0]
        self._apply_voltage_focus()

    def clear_voltage_focus(self, reset_request: bool = True) -> None:
        for widgets in self._voltage_fields.values():
            for widget in widgets:
                self._set_highlight(widget, False)
        if reset_request:
            self._voltage_focus_keys = ()
            self._voltage_primary_field = ""

    def _apply_voltage_focus(self) -> None:
        first: QWidget | None = None
        primary: QWidget | None = None
        for key in self._voltage_focus_keys:
            for widget in self._voltage_fields.get(key, ()):
                self._set_highlight(widget, True)
                first = first or widget
                if key == self._voltage_primary_field:
                    primary = primary or widget
        target = primary or first
        if target is None:
            for widget in self._voltage_fields.get("__object__", ()):
                self._set_highlight(widget, True)
                target = target or widget
        if target is None:
            return
        focus_target = self._editable_child(target)
        focus_target.setFocus(Qt.FocusReason.OtherFocusReason)
        scroll = target.parentWidget()
        while scroll is not None and not isinstance(scroll, QScrollArea):
            scroll = scroll.parentWidget()
        if isinstance(scroll, QScrollArea):
            scroll.ensureWidgetVisible(target, 24, 24)

    @staticmethod
    def _editable_child(widget: QWidget) -> QWidget:
        if isinstance(widget, (QLineEdit, QComboBox, QAbstractSpinBox, QAbstractButton)):
            return widget
        for cls in (QLineEdit, QComboBox, QAbstractSpinBox, QAbstractButton):
            child = widget.findChild(cls)
            if child is not None:
                return child
        return widget

    @classmethod
    def _set_highlight(cls, widget: QWidget, enabled: bool) -> None:
        targets: list[QWidget] = []
        if isinstance(widget, (QLineEdit, QComboBox, QAbstractSpinBox, QAbstractButton)):
            targets.append(widget)
        else:
            for cls_type in (QLineEdit, QComboBox, QAbstractSpinBox, QAbstractButton):
                targets.extend(widget.findChildren(cls_type))
            if not targets:
                targets.append(widget)
        for target in targets:
            original = target.property("voltageOriginalStyle")
            if enabled:
                if original is None:
                    target.setProperty("voltageOriginalStyle", target.styleSheet())
                target.setStyleSheet(f"{target.property('voltageOriginalStyle') or ''};{_ERROR_STYLE}")
            elif original is not None:
                target.setStyleSheet(str(original))
                target.setProperty("voltageOriginalStyle", None)

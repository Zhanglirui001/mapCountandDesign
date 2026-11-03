"""Tooltip (悬浮指引) manager for the application.

Provides a singleton TooltipManager that:
- Maintains an on/off state (default off).
- Allows any widget to register a tooltip i18n key.
- Installs a global event filter to show translated tooltips on hover
  when the feature is enabled.
"""

from __future__ import annotations

from typing import ClassVar

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QToolTip, QWidget

from ..i18n import tr


class TooltipManager(QObject):
    """Global manager for hover tooltips with i18n support."""

    enabled_changed = Signal(bool)

    _instance: ClassVar[TooltipManager | None] = None

    def __init__(self) -> None:
        if TooltipManager._instance is not None:
            raise RuntimeError("Use TooltipManager.instance()")
        super().__init__()
        self._enabled: bool = False
        self._registry: dict[int, str] = {}  # id(widget) -> i18n_key

    @classmethod
    def instance(cls) -> TooltipManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # -- public API -------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        if value == self._enabled:
            return
        self._enabled = value
        if value:
            self._install_filter()
        else:
            self._uninstall_filter()
        self.enabled_changed.emit(value)

    def register(self, widget: QWidget, i18n_key: str) -> None:
        """Register *widget* to show the translation of *i18n_key* on hover."""
        self._registry[id(widget)] = i18n_key
        # Ensure the filter is installed if tooltips are enabled
        if self._enabled:
            self._install_filter()

    def unregister(self, widget: QWidget) -> None:
        """Remove a previously registered widget."""
        self._registry.pop(id(widget), None)

    def clear(self) -> None:
        """Remove all registered widgets."""
        self._registry.clear()

    # -- internals --------------------------------------------------

    def _install_filter(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _uninstall_filter(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Enter:
            w = watched
            if isinstance(w, QWidget) and id(w) in self._registry:
                key = self._registry[id(w)]
                text = tr(key, "")
                if text:
                    # Show tooltip near the cursor
                    QToolTip.showText(QCursor.pos(), text, w)
        return super().eventFilter(watched, event)

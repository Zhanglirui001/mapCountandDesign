"""Navigate from voltage-drop issues to their owning editor fields."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QStatusBar, QTabWidget

from ...application.voltage_drop import VoltageDropIssue, format_voltage_drop_issue


class VoltageNavigationController:
    """Coordinate issue selection, tab routing, and field highlighting."""

    def __init__(
        self,
        tabs: QTabWidget,
        status_bar: QStatusBar,
        routes: dict[str, tuple[int, Any, Any]],
    ) -> None:
        self._tabs = tabs
        self._status_bar = status_bar
        self._routes = routes
        self.active_fingerprint: tuple[object, ...] | None = None
        self._active_view: Any | None = None

    def navigate(self, issue: VoltageDropIssue) -> None:
        source = issue.source
        if source is None or source.object_ref is None:
            self._show_issue(issue)
            return

        route = self._routes.get(source.object_kind)
        if route is None:
            self._show_issue(issue)
            return
        tab_index, view_model, view = route
        if view is None or not view_model.select_object(source.object_ref):
            self.clear()
            self._show_issue(issue)
            return

        self.clear()
        self.active_fingerprint = issue.fingerprint
        self._active_view = view
        self._tabs.setCurrentIndex(tab_index)
        QTimer.singleShot(
            0,
            lambda: view.focus_voltage_fields(
                source.field_keys,
                source.primary_field,
            ),
        )
        self._show_issue(issue)

    def on_issues_changed(self, issues: tuple[VoltageDropIssue, ...]) -> None:
        if self.active_fingerprint is None:
            return
        if not any(issue.fingerprint == self.active_fingerprint for issue in issues):
            self.clear()

    def clear(self) -> None:
        if self._active_view is not None:
            self._active_view.clear_voltage_focus()
        self._active_view = None
        self.active_fingerprint = None

    def _show_issue(self, issue: VoltageDropIssue) -> None:
        self._status_bar.showMessage(format_voltage_drop_issue(issue), 8000)

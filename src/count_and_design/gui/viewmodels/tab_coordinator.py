"""Coordination of project state across the application's tab view models."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ...application.catalog import ProductPresetService, PVPanelService
from ...application.version_manager import VersionManager
from ...models import Project
from .history_tab import HistoryTabViewModel


class TabCoordinator:
    """Bind a shared project and version manager to all tab view models."""

    def __init__(
        self,
        editable_tabs: Sequence[Any],
        history_tab: HistoryTabViewModel,
        count_tab: Any,
        pv_panel_service: PVPanelService,
        product_preset_service: ProductPresetService,
        emit_state: Callable[[], None],
        on_project_edited: Callable[[], None],
    ) -> None:
        self._editable_tabs = tuple(editable_tabs)
        self._history_tab = history_tab
        self._count_tab = count_tab
        self._pv_panel_service = pv_panel_service
        self._product_preset_service = product_preset_service
        self._emit_state = emit_state
        self._on_project_edited = on_project_edited

    def synchronize(
        self,
        project: Project,
        version_manager: VersionManager,
    ) -> None:
        self._product_preset_service.ensure_default_lynx_preset(project)
        self._product_preset_service.sync_product_presets_from_rackings(
            project
        )
        self._pv_panel_service.sync_from_product_presets(project)

        for tab in self._editable_tabs:
            tab._version_manager = version_manager
            tab._on_version_state_changed = (
                self._emit_state if tab is self._count_tab else self._on_project_edited
            )
            tab.set_project(project)

        self._history_tab.set_version_manager(version_manager)
        self._emit_state()

    def refresh_tab(self, index: int) -> None:
        tabs = (*self._editable_tabs, self._history_tab)
        if 0 <= index < len(tabs):
            tabs[index].refresh()

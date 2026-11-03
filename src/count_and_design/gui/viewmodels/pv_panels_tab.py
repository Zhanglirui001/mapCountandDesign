"""View model for named photovoltaic-panel properties."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ...application.catalog import PVPanelService
from ...application.version_manager import versionable
from ...i18n import tr
from ...models import PVPanelInfo, Project
from .states import ListItemState, PVPanelDetailState, PVPanelsTabState
from .property_clipboard import PropertyClipboard


class PVPanelsTabViewModel(QObject):
    state_changed = Signal(object)
    error_occurred = Signal(str, str)

    def __init__(self, service: PVPanelService | None = None) -> None:
        super().__init__()
        self._service = service or PVPanelService()
        self._project: Project | None = None
        self._selected_item_id: str | None = None
        self._item_lookup: dict[str, PVPanelInfo] = {}
        self._state = PVPanelsTabState()
        self._property_clipboard = PropertyClipboard()

    @property
    def state(self) -> PVPanelsTabState:
        return self._state

    def set_project(self, project: Project) -> None:
        self._project = project
        self._service.sync_from_product_presets(project)
        self.refresh()

    def refresh(self) -> None:
        if self._project is None:
            self._state = PVPanelsTabState()
            self.state_changed.emit(self._state)
            return
        self._service.sync_from_product_presets(self._project)
        used_names = self._service.get_used_panel_names(self._project)
        self._item_lookup = {self._key(panel): panel for panel in self._project.pv_panels}
        items = tuple(
            ListItemState(
                item_id=self._key(panel),
                label=(
                    panel.name
                    if panel.name in used_names
                    else f"{panel.name}{tr('pv_panels_tab.unused_suffix', ' (unused)')}"
                ),
                muted=panel.name not in used_names,
            )
            for panel in self._project.pv_panels
        )
        if self._selected_item_id not in self._item_lookup:
            self._selected_item_id = items[0].item_id if items else None
        panel = self._current()
        detail = PVPanelDetailState() if panel is None else PVPanelDetailState(
            name=panel.name,
            vmp=panel.vmp,
            imp=panel.imp,
            module_per_string=panel.module_per_string,
            system_voltage=panel.system_voltage,
        )
        self._state = PVPanelsTabState(items, self._selected_item_id, detail)
        self.state_changed.emit(self._state)

    def select_item(self, item_id: str | None) -> None:
        self._selected_item_id = item_id
        self.refresh()

    def select_object(self, panel: PVPanelInfo) -> bool:
        item_id = self._key(panel)
        self.refresh()
        if item_id not in self._item_lookup:
            return False
        self.select_item(item_id)
        return True

    def copy_selected(self) -> bool:
        """Copy every property except the selected panel's name."""
        panel = self._current()
        return panel is not None and self._property_clipboard.copy_from(panel)

    def paste_to_selected(self) -> bool:
        """Paste copied properties while preserving the target panel's name."""
        panel = self._current()
        if self._project is None or panel is None:
            return False
        pre = self._project.to_dict()
        if not self._property_clipboard.paste_into(panel):
            return False
        self._commit(pre, "version.paste_pv_panel_properties")
        self.refresh()
        return True

    def rename_panel(self, value: str) -> None:
        if self._project is None or self._current() is None:
            return
        if value.strip() == self._current().name:  # type: ignore[union-attr]
            return
        pre = self._project.to_dict()
        ok, message = self._service.rename(self._project, self._current(), value)  # type: ignore[arg-type]
        if not ok:
            self.error_occurred.emit(tr("pv_panels_tab.invalid_name", "Invalid panel name"), message)
            self.refresh()
            return
        self._commit(pre, "version.edit_pv_panel_name")
        self.refresh()

    def update_vmp(self, value: float) -> None:
        self._update_number("vmp", max(0.0, value))

    def update_imp(self, value: float) -> None:
        self._update_number("imp", max(0.0, value))

    def update_module_per_string(self, value: int) -> None:
        self._update_number("module_per_string", max(0, value))

    @versionable("version.clean_up_unused_pv_panels")
    def cleanup_unused(self) -> tuple[int, str]:
        if self._project is None:
            return 0, tr("pv_panels_tab.no_project_loaded", "No project loaded")
        removed = self._service.cleanup_unused(self._project)
        self.refresh()
        message = "" if removed else tr("pv_panels_tab.no_unused", "No unused PV panels to remove")
        return len(removed), message

    def count_unused(self) -> int:
        if self._project is None:
            return 0
        return self._service.count_unused(self._project)

    def _update_number(self, field: str, value: float | int) -> None:
        panel = self._current()
        if self._project is None or panel is None or getattr(panel, field) == value:
            return
        pre = self._project.to_dict()
        setattr(panel, field, value)
        self._commit(pre, "version.edit_pv_panel_properties")
        self.refresh()

    def _current(self) -> PVPanelInfo | None:
        return self._item_lookup.get(self._selected_item_id or "")

    def _commit(self, pre: dict, description: str) -> None:
        manager = getattr(self, "_version_manager", None)
        if manager is not None and self._project is not None:
            manager.commit(pre, self._project, description)

    @staticmethod
    def _key(panel: PVPanelInfo) -> str:
        return f"pv-panel:{id(panel)}"

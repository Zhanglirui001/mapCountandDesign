"""ViewModel for the Rackings tab."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from ...application.catalog import ProductPresetService, RackingsService
from ...application.ordering import (
    move_selected_items,
    optional_natural_text_key,
    sort_selected_slots,
)
from ...application.version_manager import versionable
from ...i18n import tr
from ...models import ProductPreset, Project, Rackings
from .states import ListItemState, RackingsDetailState, RackingsTabState
from .property_clipboard import PropertyClipboard

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _ordering_metadata(kind: str):
    def build(self, *args, **kwargs):
        selected = self._selected_rackings()
        return {
            "kind": kind,
            "scope": "rackings",
            "field": args[0] if kind == "sort" and args else kwargs.get("field", ""),
            "descending": bool(args[1] if kind == "sort" and len(args) > 1 else kwargs.get("descending", False)),
            "subjects": [item.name for item in selected],
        }
    return build


def _vm_gap_extender_name_for_index(index: int) -> str:
    """生成按字母顺序的 Gap Extender 名称: GA, GB, ..., GZ, GAA, GAB, ..., GZZ, GAAA, ..."""
    if index < 26:
        return f"G{_LETTERS[index]}"
    if index < 26 + 26 * 26:
        offset = index - 26
        first = offset // 26
        second = offset % 26
        return f"G{_LETTERS[first]}{_LETTERS[second]}"
    offset = index - (26 + 26 * 26)
    first = offset // (26 * 26)
    second = (offset // 26) % 26
    third = offset % 26
    return f"G{_LETTERS[first]}{_LETTERS[second]}{_LETTERS[third]}"


def _vm_gap_extender_index_from_name(name: str) -> int | None:
    """从 Gap Extender 名称反推出索引，若格式不正确返回 None。"""
    if not name or name[0] != "G":
        return None
    suffix = name[1:]
    if not suffix or not all(c in _LETTERS for c in suffix):
        return None
    if len(suffix) == 1:
        return _LETTERS.index(suffix)
    base = sum(26 ** k for k in range(1, len(suffix)))
    index = base
    for i, ch in enumerate(suffix):
        index += _LETTERS.index(ch) * (26 ** (len(suffix) - 1 - i))
    return index


def _vm_next_gap_extender_name(existing_names: set[str]) -> str:
    """基于已有名称生成下一个可用的 Gap Extender 名称。"""
    max_index = -1
    for name in existing_names:
        idx = _vm_gap_extender_index_from_name(name)
        if idx is not None and idx > max_index:
            max_index = idx
    return _vm_gap_extender_name_for_index(max_index + 1)


class RackingsTabViewModel(QObject):
    """Provides state and commands for the Rackings tab."""

    state_changed = Signal(object)

    def __init__(
        self,
        rackings_service: RackingsService | None = None,
        product_preset_service: ProductPresetService | None = None,
    ) -> None:
        super().__init__()
        self._rackings_service = rackings_service or RackingsService()
        self._product_preset_service = product_preset_service or ProductPresetService()
        self._project: Project | None = None
        self._selected_item_ids: list[str] = []
        self._selection_initialized = False
        self._item_lookup: dict[str, Rackings] = {}
        self._state = RackingsTabState()
        self._property_clipboard = PropertyClipboard()

    @property
    def state(self) -> RackingsTabState:
        return self._state

    def set_project(self, project: Project) -> None:
        self._project = project
        self._selection_initialized = False
        self.refresh()

    def refresh(self) -> None:
        if self._project is None:
            self._state = RackingsTabState()
            self.state_changed.emit(self._state)
            return
        used_names = self._rackings_service.get_used_rackings_names(self._project)
        self._item_lookup = {}
        items = []
        for rackings in self._project.rackings_list:
            item_id = self._key_for(rackings)
            self._item_lookup[item_id] = rackings
            muted = rackings.name not in used_names
            items.append(
                ListItemState(
                    item_id=item_id,
                    label=f"{rackings.name}{tr('rackings_tab.unused_suffix', ' (unused)')}" if muted else rackings.name,
                    muted=muted,
                )
            )
        selected = set(self._selected_item_ids)
        self._selected_item_ids = [item.item_id for item in items if item.item_id in selected]
        if not self._selected_item_ids and items and not self._selection_initialized:
            self._selected_item_ids = [items[0].item_id]
        self._selection_initialized = True
        selected_item_id = (
            self._selected_item_ids[0] if len(self._selected_item_ids) == 1 else None
        )
        self._state = RackingsTabState(
            items=tuple(items),
            selected_item_id=selected_item_id,
            selected_item_ids=tuple(self._selected_item_ids),
            detail=self._build_detail(),
        )
        self.state_changed.emit(self._state)

    def select_item(self, item_id: str | None) -> None:
        self.select_items((item_id,) if item_id is not None else ())

    def select_items(self, item_ids: tuple[str, ...] | list[str]) -> None:
        self._selection_initialized = True
        requested = set(item_ids)
        self._selected_item_ids = [
            item_id for item_id in self._item_lookup if item_id in requested
        ]
        self.refresh()

    @versionable(
        "version.reorder_rackings",
        metadata_factory=_ordering_metadata("reorder"),
    )
    def move_selected(self, target_item_id: str | None, after: bool = False) -> None:
        if self._project is None:
            return
        if target_item_id is not None and target_item_id not in self._item_lookup:
            return
        target = self._item_lookup.get(target_item_id or "")
        if move_selected_items(
            self._project.rackings_list,
            self._selected_rackings(),
            target,
            after=after,
        ):
            self.refresh()

    @versionable(
        "version.sort_rackings",
        metadata_factory=_ordering_metadata("sort"),
    )
    def sort_selected(self, field: str, descending: bool = False) -> None:
        if self._project is None or len(self._selected_item_ids) < 2:
            return
        if field == "name":
            key = lambda item: optional_natural_text_key(item.name)
        elif field == "branch_count":
            def key(item: Rackings) -> int:
                return sum(
                    preset.branch_count
                    for name in item.product_preset_names()
                    if (preset := self._project.get_product_preset(name)) is not None
                )
        else:
            return
        if sort_selected_slots(
            self._project.rackings_list,
            self._selected_rackings(),
            key,
            descending=descending,
        ):
            self.refresh()

    def select_object(self, rackings: Rackings) -> bool:
        item_id = self._key_for(rackings)
        self.refresh()
        if item_id not in self._item_lookup:
            return False
        self.select_item(item_id)
        return True

    def copy_selected(self) -> bool:
        """Copy every property except the selected racking's name."""
        rackings = self._current_rackings()
        return rackings is not None and self._property_clipboard.copy_from(rackings)

    def paste_to_selected(self) -> bool:
        """Paste copied properties while preserving the target racking's name."""
        rackings = self._current_rackings()
        if self._project is None or rackings is None:
            return False
        pre = self._project.to_dict()
        if not self._property_clipboard.paste_into(rackings):
            return False
        self._product_preset_service.sync_product_presets_from_rackings(self._project)
        self._push_version(pre, "version.paste_rackings_properties")
        self.refresh()
        return True

    def rename_rackings(self, new_name: str, merge_if_exists: bool = False) -> tuple[str, str]:
        if self._project is None:
            return "invalid", tr("rackings_tab.no_project_loaded", "No project loaded")
        rackings = self._current_rackings()
        if rackings is None:
            return "invalid", tr("rackings_tab.no_rackings_selected", "No rackings selected")
        pre = self._project.to_dict()
        result = self._rackings_service.rename_rackings(self._project, rackings, new_name, merge_if_exists)
        if result.status not in {"renamed", "merged"}:
            return result.status, result.message
        self._push_version(pre, "Rename rackings")
        self._product_preset_service.sync_product_presets_from_rackings(self._project)
        self.refresh()
        return result.status, result.message

    @versionable("version.edit_rackings_product_presets")
    def update_product_presets(self, product_presets_str: str) -> None:
        if self._project is None:
            return
        rackings = self._current_rackings()
        if rackings is None:
            return
        self._rackings_service.update_rackings_product_presets(self._project, rackings, product_presets_str)
        self._product_preset_service.sync_product_presets_from_rackings(self._project)
        self._state = replace(
            self._state,
            detail=replace(self._state.detail, product_presets=rackings.product_presets),
        )

    @versionable("version.edit_rackings_gap_extenders")
    def update_gap_extenders(self, gap_extenders: list[tuple[str, int]]) -> None:
        if self._project is None:
            return
        rackings = self._current_rackings()
        if rackings is None:
            return
        self._rackings_service.update_rackings_gap_extenders(self._project, rackings, gap_extenders)
        self._product_preset_service.sync_product_presets_from_rackings(self._project)
        self._state = replace(
            self._state,
            detail=replace(self._state.detail, gap_extenders=tuple((name, str(qty)) for name, qty in rackings.gap_extenders)),
        )

    @versionable("version.add_gap_extender")
    def create_and_add_gap_extender_to_racking(self) -> str:
        """Create a new Gap Extender ProductPreset and add it to the current racking.

        Generates the next available name (project-wide max + 1),
        creates the ProductPreset, appends it to the current racking's
        gap extender list, and refreshes the UI.

        Returns the newly created preset name.
        """
        if self._project is None:
            return ""
        rackings = self._current_rackings()
        if rackings is None:
            return ""
        # Collect all existing gap extender names from all rackings
        existing: set[str] = set()
        for r in self._project.rackings_list:
            for name, _qty in r.gap_extenders:
                if name:
                    existing.add(name)
        new_name = _vm_next_gap_extender_name(existing)
        # Create the ProductPreset
        new_preset = ProductPreset(name=new_name, product_type="Gap_Extender")
        self._project.add_product_preset(new_preset)
        # Add to the current racking
        rackings.gap_extenders.append((new_name, 0))
        # Refresh so the new name appears in both the dropdown and the list
        self.refresh()
        return new_name

    @versionable("version.clean_up_unused_rackings")
    def cleanup_unused(self) -> tuple[int, str]:
        if self._project is None:
            return 0, tr("rackings_tab.no_project_loaded", "No project loaded")
        removed = self._rackings_service.cleanup_unused_rackings(self._project)
        self.refresh()
        return len(removed), tr("rackings_tab.no_unused", "No unused rackings to remove") if not removed else ""

    def count_unused(self) -> int:
        if self._project is None:
            return 0
        used_names = self._rackings_service.get_used_rackings_names(self._project)
        return len([item for item in self._project.rackings_list if item.name not in used_names])

    def _build_detail(self) -> RackingsDetailState:
        rackings = self._current_rackings()
        if self._project is None or rackings is None:
            return RackingsDetailState()
        usage_count = 0
        for inv in self._project.invs:
            for node in inv.nodes:
                for row in node.rows:
                    for entry in row.racking_entries:
                        if entry.rackings == rackings.name:
                            usage_count += 1
        # Collect all gap extender names across all rackings in the project
        all_gap_extender_names: set[str] = set()
        for r in self._project.rackings_list:
            for name, _qty in r.gap_extenders:
                if name:
                    all_gap_extender_names.add(name)
        return RackingsDetailState(
            name=rackings.name,
            product_presets=rackings.product_presets,
            usage_count=usage_count,
            gap_extenders=tuple((name, str(qty)) for name, qty in rackings.gap_extenders),
            existing_gap_extender_names=tuple(sorted(all_gap_extender_names)),
        )

    def _current_rackings(self) -> Rackings | None:
        if len(self._selected_item_ids) != 1:
            return None
        return self._item_lookup.get(self._selected_item_ids[0])

    def _selected_rackings(self) -> list[Rackings]:
        selected = set(self._selected_item_ids)
        return [
            item
            for item in (self._project.rackings_list if self._project else [])
            if self._key_for(item) in selected
        ]

    def _push_version(self, pre_snapshot: dict, description: str) -> None:
        """Create a version node recording the current change, if wired up."""
        manager = getattr(self, "_version_manager", None)
        if manager is not None and self._project is not None:
            manager.commit(pre_snapshot, self._project, description)

    @staticmethod
    def _key_for(rackings: Rackings) -> str:
        return f"rackings:{id(rackings)}"

"""ViewModel for the Length Identifiers tab."""

from __future__ import annotations

import re

from PySide6.QtCore import QObject, Signal

from ...application.ordering import (
    move_selected_items,
    optional_natural_text_key,
    sort_selected_slots,
)
from ...application.version_manager import versionable
from ...i18n import tr
from ...models import Length, LengthIdentifier, Project
from ...models.length import DimensionError, LengthError
from .states import LengthIdentifiersDetailState, LengthIdentifiersTabState, LengthIdentifierItemState


def _ordering_metadata(kind: str):
    def build(self, *args, **kwargs):
        selected = self._selected_identifiers()
        return {
            "kind": kind,
            "scope": "length_identifiers",
            "field": args[0] if kind == "sort" and args else kwargs.get("field", ""),
            "descending": bool(args[1] if kind == "sort" and len(args) > 1 else kwargs.get("descending", False)),
            "subjects": [item.name for item in selected],
        }
    return build


class LengthIdentifiersTabViewModel(QObject):
    """Provides state and commands for the Length Identifiers tab."""

    state_changed = Signal(object)
    error_occurred = Signal(str, str)  # (title, message)

    def __init__(self) -> None:
        super().__init__()
        self._project: Project | None = None
        self._selected_item_ids: list[str] = []
        self._selection_initialized = False
        self._item_lookup: dict[str, LengthIdentifier] = {}
        self._state = LengthIdentifiersTabState()

    @property
    def state(self) -> LengthIdentifiersTabState:
        return self._state

    def set_project(self, project: Project) -> None:
        self._project = project
        self._selection_initialized = False
        self.refresh()

    def refresh(self) -> None:
        if self._project is None:
            self._state = LengthIdentifiersTabState()
            self.state_changed.emit(self._state)
            return

        self._item_lookup = {}
        items = []
        for identifier in self._project.length_identifiers:
            item_id = self._key_for(identifier)
            self._item_lookup[item_id] = identifier
            items.append(LengthIdentifierItemState(item_id=item_id, label=f"{identifier.name} = {identifier.length.length_str}"))

        selected = set(self._selected_item_ids)
        self._selected_item_ids = [item.item_id for item in items if item.item_id in selected]
        if not self._selected_item_ids and items and not self._selection_initialized:
            self._selected_item_ids = [items[0].item_id]
        self._selection_initialized = True
        selected_item_id = (
            self._selected_item_ids[0] if len(self._selected_item_ids) == 1 else None
        )

        self._state = LengthIdentifiersTabState(
            items=tuple(items),
            selected_item_id=selected_item_id,
            selected_item_ids=tuple(self._selected_item_ids),
            identifier_options=tuple(
                (identifier.name, identifier.length.length_str) for identifier in self._project.length_identifiers
            ),
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

    def select_object(self, identifier: LengthIdentifier) -> bool:
        item_id = self._key_for(identifier)
        self.refresh()
        if item_id not in self._item_lookup:
            return False
        self.select_item(item_id)
        return True

    _RESERVED_UNITS = {"mm", "ft", "inch", "in", "m"}

    @versionable("version.add_length_identifier")
    def add_identifier(self) -> None:
        if self._project is None:
            return
        used_names = {identifier.name for identifier in self._project.length_identifiers}
        next_name = next(
            (chr(code) for code in range(ord("A"), ord("Z") + 1)
             if chr(code) not in used_names and chr(code).lower() not in self._RESERVED_UNITS),
            "id_A",
        )
        # Fallback: find an unused name with pattern id_A, id_B, ...
        if next_name == "id_A" and next_name in used_names:
            for code in range(ord("A"), ord("Z") + 1):
                candidate = f"id_{chr(code)}"
                if candidate not in used_names:
                    next_name = candidate
                    break
        created = LengthIdentifier(
            name=next_name,
            length=Length("0mm", registry=self._project.identifier_registry),
        )
        self._project.add_length_identifier(created)
        self._selected_item_ids = [self._key_for(created)]
        self.refresh()

    @versionable(
        "version.reorder_length_identifiers",
        metadata_factory=_ordering_metadata("reorder"),
    )
    def move_selected(self, target_item_id: str | None, after: bool = False) -> None:
        if self._project is None or len(self._selected_item_ids) < 1:
            return
        if target_item_id is not None and target_item_id not in self._item_lookup:
            return
        selected = self._selected_identifiers()
        target = self._item_lookup.get(target_item_id or "")
        if move_selected_items(
            self._project.length_identifiers, selected, target, after=after
        ):
            self.refresh()

    @versionable(
        "version.sort_length_identifiers",
        metadata_factory=_ordering_metadata("sort"),
    )
    def sort_selected(self, field: str, descending: bool = False) -> None:
        if self._project is None or len(self._selected_item_ids) < 2:
            return
        if field == "name":
            key = lambda item: optional_natural_text_key(item.name)
        elif field == "length_mm":
            key = lambda item: item.length.length_mm
        else:
            return
        if sort_selected_slots(
            self._project.length_identifiers,
            self._selected_identifiers(),
            key,
            descending=descending,
        ):
            self.refresh()

    def update_name(self, value: str) -> tuple[str, str]:
        """Rename the selected identifier and update every project reference."""
        return self.rename_all(value)

    def rename_all(self, new_name: str) -> tuple[str, str]:
        """Rename the current length identifier and replace every reference
        to the old name across all :class:`Length` properties in the project.

        Returns:
            A ``(status, message)`` tuple where *status* is one of
            ``"renamed"``, ``"invalid"``, or ``"noop"``.
        """
        if self._project is None:
            return ("invalid", tr("rackings_tab.no_project_loaded", "No project loaded"))
        identifier = self._current_identifier()
        if identifier is None:
            return ("invalid", tr("length_identifiers_tab.no_selection", "No length identifier selected."))

        normalized = new_name.strip()
        if not re.fullmatch(r"[a-zA-Z_]+", normalized):
            return ("invalid", tr("length_identifiers_tab.invalid_name", "Name must consist of letters and underscores."))
        if normalized.lower() in self._RESERVED_UNITS:
            return ("invalid", tr("length_identifiers_tab.reserved_unit",
                                  "{name} is a reserved unit").format(name=normalized))

        old_name = identifier.name
        if normalized == old_name:
            return ("noop", "")

        if any(item is not identifier and item.name == normalized for item in self._project.length_identifiers):
            return ("invalid", tr("length_identifiers_tab.duplicate_name",
                                  "Identifier '{name}' already exists.").format(name=normalized))

        pre = self._project.to_dict()
        identifier.name = normalized
        self._project.replace_length_identifier_references(old_name, normalized)
        self._push_version(pre, "Rename length identifier")
        self.refresh()
        return ("renamed", "")

    def update_length(self, value: str) -> None:
        identifier = self._current_identifier()
        if self._project is None or identifier is None:
            return
        new_str = value.strip() or "0mm"
        if identifier.length.length_str == new_str:
            return  # 值未变化，跳过刷新

        # 检测循环依赖
        if self._project.would_create_length_identifier_cycle(
            identifier.name, new_str
        ):
            self.error_occurred.emit(
                tr("length_identifiers_tab.cycle_error_title",
                   "Circular Dependency"),
                tr("length_identifiers_tab.cycle_error_message",
                   "Cannot set '{name}' to '{value}': this would create a "
                   "circular dependency. An identifier must not depend on "
                   "itself, either directly or through other identifiers.")
                .format(name=identifier.name, value=new_str),
            )
            # 回退 UI 到当前值
            self.refresh()
            return

        try:
            pre = self._project.to_dict()
            identifier.length = Length(
                new_str, registry=self._project.identifier_registry
            )
            self._push_version(pre, "Edit length identifier")
        except LengthError as exc:
            self.error_occurred.emit(
                tr(f"length.error.{exc.code}_title",
                   tr("length_identifiers_tab.dimension_error_title", "Error")),
                tr(f"length.error.{exc.code}", str(exc)).format(**exc.params),
            )
            # 回退 UI 到当前值
            self.refresh()
            return

        self.refresh()

    @versionable("version.delete_length_identifier")
    def delete_selected(self) -> None:
        identifier = self._current_identifier()
        if self._project is None or identifier is None:
            return
        index = self._project.length_identifiers.index(identifier)
        self._project.remove_length_identifier(identifier)
        if self._project.length_identifiers:
            fallback_index = min(index, len(self._project.length_identifiers) - 1)
            self._selected_item_ids = [
                self._key_for(self._project.length_identifiers[fallback_index])
            ]
        else:
            self._selected_item_ids = []
        self.refresh()

    def _build_detail(self) -> LengthIdentifiersDetailState:
        identifier = self._current_identifier()
        if identifier is None:
            return LengthIdentifiersDetailState()
        return LengthIdentifiersDetailState(name=identifier.name, length=identifier.length.length_str)

    def _current_identifier(self) -> LengthIdentifier | None:
        if len(self._selected_item_ids) != 1:
            return None
        return self._item_lookup.get(self._selected_item_ids[0])

    def _selected_identifiers(self) -> list[LengthIdentifier]:
        selected = set(self._selected_item_ids)
        return [
            item
            for item in (self._project.length_identifiers if self._project else [])
            if self._key_for(item) in selected
        ]

    def _push_version(self, pre_snapshot: dict, description: str) -> None:
        """Create a version node recording the current change, if wired up."""
        manager = getattr(self, "_version_manager", None)
        if manager is not None and self._project is not None:
            manager.commit(pre_snapshot, self._project, description)

    @staticmethod
    def _key_for(identifier: LengthIdentifier) -> str:
        return f"length-identifier:{id(identifier)}"

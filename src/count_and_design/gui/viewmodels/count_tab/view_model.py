"""ViewModel for the Count tab."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import Event

from PySide6.QtCore import QObject, Signal

from ....application.catalog import ProductPresetService, RackingsService
from ....application.ordering import (
    move_selected_items,
    optional_natural_text_key,
    sort_selected_slots,
)
from ....application.project import ProjectStatsService, ProjectTreeService
from ....application.version_manager import versionable
from ....application.voltage_drop import VoltageDropService
from ....application.voltage_drop.models import VoltageDropReport
from ....i18n import tr
from ....models import Inv, Length, Node, Project, Row
from ....models.length import LengthError
from .editor import CountTabEditorBuilder
from .expansion import TreeExpansionController, tree_item_key
from ..states import (
    CountTabState,
    EditorState,
    TreeNodeState,
)


def _count_ordering_metadata(kind: str):
    def build(self, *args, **kwargs):
        context = self._selected_sibling_context()
        item_type, _collection, selected = context if context is not None else ("", (), ())
        return {
            "kind": kind,
            "scope": item_type,
            "field": args[0] if kind == "sort" and args else kwargs.get("field", ""),
            "descending": bool(args[1] if kind == "sort" and len(args) > 1 else kwargs.get("descending", False)),
            "subjects": [getattr(item, "name", str(item)) for item in selected],
        }
    return build


@dataclass(frozen=True)
class VoltageCalculationRequest:
    """Immutable input handed to the background voltage worker."""

    project_snapshot: Project
    revision: int


class CountTabViewModel(QObject):
    """Provides state and commands for the Count tab."""

    state_changed = Signal(object)
    results_changed = Signal(object)
    voltage_issues_changed = Signal(object)
    voltage_calculation_invalidated = Signal()
    expansion_changed = Signal(object)
    tree_item_changed = Signal(str, str)
    error_occurred = Signal(str, str)  # (title, message)

    def __init__(
        self,
        tree_service: ProjectTreeService | None = None,
        stats_service: ProjectStatsService | None = None,
        rackings_service: RackingsService | None = None,
        product_preset_service: ProductPresetService | None = None,
    ) -> None:
        super().__init__()
        self._tree_service = tree_service or ProjectTreeService()
        editor_stats_service = stats_service or ProjectStatsService()
        self._rackings_service = rackings_service or RackingsService()
        self._product_preset_service = product_preset_service or ProductPresetService()
        self._voltage_drop_service = VoltageDropService()
        self._voltage_report: VoltageDropReport | None = None
        self._expansion_controller = TreeExpansionController()
        self._editor_builder = CountTabEditorBuilder(
            editor_stats_service,
            self._product_preset_service,
        )
        self._project: Project | None = None
        self._clipboard: object | None = None
        self._selected_node_id: str | None = None
        self._selected_node_ids: list[str] = []
        self._expanded_ids: set[str] = set()
        self._expanded_ids_initialized: bool = False
        self._node_lookup: dict[str, tuple[str, object]] = {}
        self._project_revision = 0
        self._state = CountTabState()

    @property
    def state(self) -> CountTabState:
        return self._state

    @property
    def has_voltage_report(self) -> bool:
        return self._voltage_report is not None

    def set_project(self, project: Project) -> None:
        self.voltage_calculation_invalidated.emit()
        self._project = project
        self._project_revision += 1
        self._invalidate_results()
        self._expanded_ids_initialized = False
        self._product_preset_service.sync_product_presets_from_rackings(project)
        self._refresh_structure(select_object=project)

    def set_expanded_ids(self, node_ids: list[str]) -> None:
        self._expanded_ids = set(node_ids)

    def set_expanded(self, node_id: str, expanded: bool) -> None:
        if expanded:
            self._expanded_ids.add(node_id)
        else:
            self._expanded_ids.discard(node_id)

    def update_expanded_ids(self, node_ids: list[str]) -> None:
        """Directly update expanded IDs without triggering any refresh.

        Used by the view to synchronise the actual tree expansion state back
        after Qt may have auto-expanded ancestors during setCurrentItem.
        """
        self._expanded_ids = set(node_ids)

    def select_item(self, node_id: str | None) -> None:
        self.select_items((node_id,) if node_id is not None else ())

    def select_items(self, node_ids: tuple[str, ...] | list[str]) -> None:
        requested = [node_id for node_id in node_ids if node_id in self._node_lookup]
        if not requested:
            self._selected_node_ids = []
            self._selected_node_id = None
            self._refresh_editor()
            return

        current = next(
            (node_id for node_id in self._selected_node_ids if node_id in requested),
            None,
        )
        scope_source = current or requested[0]
        scope = self._selection_scope(scope_source)
        normalized = [
            node_id
            for node_id in self._node_lookup
            if node_id in requested and self._selection_scope(node_id) == scope
        ]
        self._selected_node_ids = normalized
        self._selected_node_id = normalized[0] if len(normalized) == 1 else None
        self._refresh_editor()

    @versionable(
        "version.reorder_count_items",
        metadata_factory=_count_ordering_metadata("reorder"),
    )
    def move_selected(self, target_node_id: str | None, after: bool = False) -> None:
        context = self._selected_sibling_context()
        if context is None:
            return
        _item_type, collection, selected = context
        if target_node_id is not None and target_node_id not in self._node_lookup:
            return
        target_lookup = self._node_lookup.get(target_node_id or "")
        target = target_lookup[1] if target_lookup is not None else None
        if target is not None and not any(target is item for item in collection):
            return
        if move_selected_items(collection, selected, target, after=after):
            self._invalidate_results()
            self._mark_project_changed()
            self._refresh_structure()

    @versionable(
        "version.sort_count_items",
        metadata_factory=_count_ordering_metadata("sort"),
    )
    def sort_selected(self, field: str, descending: bool = False) -> None:
        context = self._selected_sibling_context()
        if context is None or self._project is None:
            return
        item_type, collection, selected = context
        if field == "name" and item_type in {"inv", "node", "row"}:
            key = lambda item: optional_natural_text_key(item.name)
        elif field == "racking_pitch" and item_type == "inv":
            key = lambda item: (
                (item.racking_pitch or self._project.racking_pitch).length_mm
                if (item.racking_pitch or self._project.racking_pitch) is not None
                else None
            )
        elif field == "voltage_drop_percent" and item_type == "inv":
            if self._voltage_report is None:
                return
            key = lambda item: (
                level.voltage_drop_percent
                if (level := self._voltage_report.for_object(item)) is not None
                else None
            )
        elif field == "cluster_total_length" and item_type == "inv":
            key = lambda item: sum(
                node.cluster_length.length_mm
                for node in item.nodes
                if node.cluster_length is not None
            )
        else:
            return
        if sort_selected_slots(
            collection, selected, key, descending=descending
        ):
            self._invalidate_results()
            self._mark_project_changed()
            self._refresh_structure()

    @versionable("version.renumber_count_items")
    def renumber_selected(self) -> bool:
        context = self._selected_sibling_context()
        if context is None:
            return False
        item_type, collection, selected = context
        if item_type not in {"inv", "node", "row"} or len(selected) < 2:
            return False
        parsed: list[tuple[str, int, int]] = []
        for item in selected:
            match = re.fullmatch(r"(.*?)(\d+)", item.name)
            if match is None:
                self.error_occurred.emit(
                    tr("ordering.renumber_failed_title", "Renumber Failed"),
                    tr(
                        "ordering.renumber_requires_suffix",
                        "Every selected name must end with a number.",
                    ),
                )
                return False
            digits = match.group(2)
            parsed.append((match.group(1), int(digits), len(digits)))
        start = parsed[0][1]
        number_width = parsed[0][2]
        generated = [
            f"{prefix}{str(start + index).zfill(number_width)}"
            for index, (prefix, _number, _width) in enumerate(parsed)
        ]
        selected_ids = {id(item) for item in selected}
        unselected_names = {
            item.name for item in collection if id(item) not in selected_ids
        }
        if len(set(generated)) != len(generated) or any(
            name in unselected_names for name in generated
        ):
            self.error_occurred.emit(
                tr("ordering.renumber_failed_title", "Renumber Failed"),
                tr(
                    "ordering.renumber_name_conflict",
                    "Renumbering would create a duplicate sibling name.",
                ),
            )
            return False
        if all(item.name == name for item, name in zip(selected, generated)):
            return False
        for item, name in zip(selected, generated):
            item.name = name
        self._invalidate_results()
        self._mark_project_changed()
        self._refresh_structure()
        return True

    def selected_item_type(self) -> str | None:
        context = self._selected_sibling_context()
        return context[0] if context is not None else None

    def can_sort_voltage_drop(self) -> bool:
        return self.selected_item_type() == "inv" and self._voltage_report is not None

    def can_renumber_selected(self) -> bool:
        context = self._selected_sibling_context()
        if context is None:
            return False
        item_type, _collection, selected = context
        return (
            item_type in {"inv", "node", "row"}
            and len(selected) >= 2
            and all(re.fullmatch(r".*?\d+", item.name) is not None for item in selected)
        )

    @versionable("version.edit_project_properties")
    def update_project_field(self, field_name: str, value: str) -> None:
        if self._project is None:
            return
        if field_name == "name":
            self._project.name = value
        elif field_name == "address":
            self._project.address = value
        elif field_name == "client_name":
            self._project.client_name = value
        elif field_name == "project_type":
            self._project.project_type = value
        elif field_name == "racking_pitch":
            self._set_optional_length(self._project, "racking_pitch", value)
        elif field_name == "lynx_preset":
            self._project.lynx_preset = value if value else None
        self._mark_project_changed()
        if field_name == "name":
            self.tree_item_changed.emit(
                self._key_for(self._project),
                self._project.name,
            )
        self._recalculate_results()

    @versionable("version.edit_lynx_preset")
    def update_project_lynx_preset(self, value: str) -> None:
        """更新 project 的 LYNX preset 选择。"""
        self._set_lynx_preset("project", value)

    @versionable("version.create_lynx_preset")
    def create_and_select_project_lynx_preset(self) -> None:
        """创建新的 LYNX preset 并自动选中到 project。"""
        self._create_and_select_lynx_preset("project")

    @versionable("version.edit_inv_properties")
    def update_inv_field(self, field_name: str, value: str) -> None:
        current = self._current_lookup()
        if current is None or current[0] != "inv":
            return
        inv = current[1]
        if field_name == "name":
            inv.name = value
        elif field_name == "racking_pitch":
            self._set_optional_length(inv, "racking_pitch", value)
        self._mark_project_changed()
        if field_name == "name":
            self.tree_item_changed.emit(self._key_for(inv), inv.name)
        self._recalculate_results()

    @versionable("version.edit_inv_lynx_preset")
    def update_inv_lynx_preset(self, value: str) -> None:
        """更新 INV 的 LYNX preset 选择。"""
        self._set_lynx_preset("inv", value)

    @versionable("version.create_inv_lynx_preset")
    def create_and_select_inv_lynx_preset(self) -> None:
        """创建新的 LYNX preset 并自动选中到当前 INV。"""
        self._create_and_select_lynx_preset("inv")

    @versionable("version.edit_node_properties")
    def update_node_field(self, field_name: str, value: str) -> None:
        current = self._current_lookup()
        if current is None or current[0] != "node":
            return
        node = current[1]
        if field_name == "name":
            node.name = value
        elif field_name == "racking_pitch":
            self._set_optional_length(node, "racking_pitch", value)
        elif field_name == "cluster_length":
            self._set_optional_length(node, "cluster_length", value)
        elif field_name == "box_position":
            try:
                box_position = float(value)
            except ValueError:
                pass
            else:
                if box_position < 0:
                    node.box_position = 0.0
                    self._refresh_editor()
                    self.error_occurred.emit(
                        tr(
                            "box_position.negative_title",
                            "Invalid Box Position",
                        ),
                        tr(
                            "box_position.negative_message",
                            "Box Position cannot be negative and has been set to 0.",
                        ),
                    )
                else:
                    node.box_position = box_position
        self._mark_project_changed()
        if field_name == "name":
            self.tree_item_changed.emit(self._key_for(node), node.name)
        self._recalculate_results()

    @versionable("version.edit_node_lynx_preset")
    def update_node_lynx_preset(self, value: str) -> None:
        """更新 Node 的 LYNX preset 选择。"""
        self._set_lynx_preset("node", value)

    @versionable("version.create_node_lynx_preset")
    def create_and_select_node_lynx_preset(self) -> None:
        """创建新的 LYNX preset 并自动选中到当前 Node。"""
        self._create_and_select_lynx_preset("node")

    @versionable("version.edit_row_properties")
    def update_row_field(self, field_name: str, value: str) -> None:
        current = self._current_lookup()
        if current is None or current[0] != "row" or self._project is None:
            return
        row = current[1]
        if field_name == "name":
            row.name = value
        elif field_name.startswith("rackings_entry_name:"):
            # field_name == "rackings_entry_name:<index>"
            idx = int(field_name.split(":", 1)[1])
            if 0 <= idx < len(row.racking_entries):
                row.racking_entries[idx].rackings = value
                self._rackings_service.ensure_rackings_exists(self._project, value)
        elif field_name.startswith("rackings_entry_offset:"):
            idx = int(field_name.split(":", 1)[1])
            if 0 <= idx < len(row.racking_entries):
                row.racking_entries[idx].offset = (
                    self._parse_length(value)
                    or Length("0mm", registry=self._project.identifier_registry)
                )
        self._product_preset_service.sync_product_presets_from_rackings(self._project)
        self._mark_project_changed()
        if field_name == "name":
            self.tree_item_changed.emit(self._key_for(row), row.name)
        self._recalculate_results()

    @versionable("version.add_racking_entry")
    def add_racking_entry(self) -> None:
        """Add a new empty RackingEntry to the currently selected row."""
        current = self._current_lookup()
        if current is None or current[0] != "row":
            return
        row = current[1]
        from ....models.row import RackingEntry
        row.racking_entries.append(RackingEntry())
        self._mark_project_changed()
        self._refresh_editor(select_object=row)

    @versionable("version.remove_racking_entry")
    def remove_racking_entry(self, index: int) -> None:
        """Remove a RackingEntry at the given index from the currently selected row."""
        current = self._current_lookup()
        if current is None or current[0] != "row":
            return
        row = current[1]
        if 0 <= index < len(row.racking_entries) and len(row.racking_entries) > 1:
            del row.racking_entries[index]
        self._mark_project_changed()
        self._refresh_editor(select_object=row)

    @versionable("version.add_item")
    def create_child(self) -> None:
        current = self._current_lookup()
        if self._project is None or current is None:
            return
        created = self._tree_service.create_child(self._project, current[0], current[1])
        self._mark_project_changed()
        self._refresh_structure(select_object=created or current[1])

    def copy_selected(self) -> None:
        current = self._current_lookup()
        if current is None:
            return
        self._clipboard = self._tree_service.copy_item(current[0], current[1])

    @versionable("version.paste_item")
    def paste_to_selected(self) -> None:
        current = self._current_lookup()
        if self._project is None or current is None or self._clipboard is None:
            return
        created = self._tree_service.paste_item(
            self._project,
            current[0],
            current[1],
            self._clipboard,
        )
        if created is not None:
            self._expanded_ids.add(self._key_for(current[1]))
        self._mark_project_changed()
        self._refresh_structure(select_object=current[1])

    @versionable("version.delete_item")
    def delete_selected(self) -> None:
        current = self._current_lookup()
        if self._project is None or current is None:
            return
        fallback = self._tree_service.delete_item(self._project, current[0], current[1])
        self._mark_project_changed()
        self._refresh_structure(select_object=fallback or self._project)

    # ------------------------------------------------------------------
    # Alt+1 / Alt+2 / Alt+3  shortcut handlers
    # ------------------------------------------------------------------

    def expand_level(self, level: int) -> None:
        """Dispatch Alt+1/2/3 based on currently selected node type."""
        if self._project is None:
            return
        current = self._current_lookup()
        if current is None:
            return
        result = self._expansion_controller.expand(
            self._project,
            current[0],
            current[1],
            level,
            self._expanded_ids,
            self._selected_node_id or self._key_for(self._project),
        )
        self._expanded_ids = result.expanded_ids
        self._set_single_selection(result.selected_id)
        self._publish_expansion()

    def refresh(self, rebuild_tree: bool = True) -> None:
        """Refresh presentation state without implicitly calculating voltage."""
        if rebuild_tree:
            self._refresh_structure()
        else:
            self._refresh_editor()

    def project_changed(self) -> None:
        """Invalidate a running calculation after another tab edits the project."""
        self._mark_project_changed()
        self._refresh_editor()

    def create_voltage_calculation_request(
        self,
    ) -> VoltageCalculationRequest | None:
        if self._project is None:
            return None
        return VoltageCalculationRequest(
            project_snapshot=copy.deepcopy(self._project),
            revision=self._project_revision,
        )

    def calculate_voltage_request(
        self,
        request: VoltageCalculationRequest,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
        cancel_event: Event | None = None,
    ) -> VoltageDropReport:
        return self._voltage_drop_service.calculate(
            request.project_snapshot,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )

    def accept_voltage_report(
        self,
        request: VoltageCalculationRequest,
        report: VoltageDropReport,
    ) -> bool:
        """Accept a background report only when its input is still current."""
        if self._project is None or request.revision != self._project_revision:
            return False
        object_map = self._build_snapshot_object_map(
            request.project_snapshot,
            self._project,
        )
        report.rebind_object_references(object_map)
        self._voltage_report = report
        self.voltage_issues_changed.emit(tuple(report.issues))
        self._recalculate_results()
        return True

    def select_object(self, item: object) -> bool:
        """Select and reveal a project-tree object."""
        if self._project is None:
            return False
        node_id = self._key_for(item)
        if node_id not in self._node_lookup:
            self._refresh_structure(emit=False)
        if node_id not in self._node_lookup:
            return False
        current = self._node_lookup[node_id]
        item_type, item = current
        self._expanded_ids = self._expansion_controller.reveal(
            self._project,
            item_type,
            item,
            self._expanded_ids,
        )
        self._set_single_selection(node_id)
        self.expansion_changed.emit(frozenset(self._expanded_ids))
        self._refresh_editor()
        return True

    def _recalculate_results(self) -> None:
        """Rebuild and publish results from the last completed voltage report.

        Property editors call this instead of a full refresh so the active
        editor keeps keyboard focus. Voltage values intentionally remain from
        the last successful manual calculation.
        """
        if self._project is None:
            return
        editor = self._build_editor_state()
        if editor is None:
            return
        self._state = replace(self._state, editor=editor)
        self.results_changed.emit(editor.results)

    def _refresh_editor(
        self,
        select_id: str | None = None,
        select_object: object | None = None,
        emit: bool = True,
    ) -> None:
        if self._project is None:
            self._state = CountTabState()
            if emit:
                self.state_changed.emit(self._state)
            return

        if select_object is not None:
            self._set_single_selection(self._key_for(select_object))
        elif select_id is not None:
            self._set_single_selection(select_id)
        self._normalize_selected_node_ids()
        self._state = replace(
            self._state,
            selected_node_id=self._selected_node_id,
            selected_node_ids=tuple(self._selected_node_ids),
            editor=self._build_editor_state(),
        )
        if emit:
            self.state_changed.emit(self._state)

    def _refresh_structure(
        self,
        select_id: str | None = None,
        select_object: object | None = None,
        emit: bool = True,
    ) -> None:
        if self._project is None:
            self._state = CountTabState()
            if emit:
                self.state_changed.emit(self._state)
            return

        if not self._expanded_ids_initialized:
            self._expanded_ids = {self._key_for(self._project)}
            self._expanded_ids_initialized = True
        self._node_lookup = {}
        tree = (self._build_tree(self._project),)
        if select_object is not None:
            self._set_single_selection(self._key_for(select_object))
        elif select_id is not None:
            self._set_single_selection(select_id)
        self._normalize_selected_node_ids()
        self._state = CountTabState(
            tree=tree,
            selected_node_id=self._selected_node_id,
            selected_node_ids=tuple(self._selected_node_ids),
            editor=self._build_editor_state(),
        )
        if emit:
            self.state_changed.emit(self._state)

    def _mark_project_changed(self) -> None:
        self._project_revision += 1
        self.voltage_calculation_invalidated.emit()

    def _publish_expansion(self) -> None:
        self.expansion_changed.emit(frozenset(self._expanded_ids))
        self._refresh_editor()

    @staticmethod
    def _build_snapshot_object_map(
        snapshot: Project,
        project: Project,
    ) -> dict[int, object]:
        """Map snapshot model objects to the equivalent live model objects."""
        result: dict[int, object] = {
            id(snapshot): project,
            id(project): project,
        }

        def pair_lists(left: list[object], right: list[object]) -> None:
            for source, target in zip(left, right):
                result[id(source)] = target
                result[id(target)] = target

        pair_lists(snapshot.length_identifiers, project.length_identifiers)
        pair_lists(snapshot.rackings_list, project.rackings_list)
        pair_lists(snapshot.product_presets, project.product_presets)
        pair_lists(snapshot.pv_panels, project.pv_panels)
        pair_lists(snapshot.invs, project.invs)
        for snapshot_inv, live_inv in zip(snapshot.invs, project.invs):
            pair_lists(snapshot_inv.nodes, live_inv.nodes)
            for snapshot_node, live_node in zip(
                snapshot_inv.nodes,
                live_inv.nodes,
            ):
                pair_lists(snapshot_node.rows, live_node.rows)
        return result

    def _build_tree(self, project: Project) -> TreeNodeState:
        root_id = self._register("project", project)
        children = []
        for inv in project.invs:
            inv_id = self._register("inv", inv)
            node_children = []
            for node in inv.nodes:
                node_id = self._register("node", node)
                row_children = []
                for index, row in enumerate(node.rows):
                    row_id = self._register("row", row)
                    row_label = row.name or tr(
                        "count_tab.tree.default_row", "Row {index}"
                    ).format(index=index + 1)
                    row_children.append(
                        TreeNodeState(
                            node_id=row_id,
                            item_type="row",
                            label=row_label,
                            expanded=row_id in self._expanded_ids,
                        )
                    )
                node_children.append(
                    TreeNodeState(
                        node_id=node_id,
                        item_type="node",
                        label=node.name,
                        expanded=node_id in self._expanded_ids,
                        children=tuple(row_children),
                    )
                )
            children.append(
                TreeNodeState(
                    node_id=inv_id,
                    item_type="inv",
                    label=inv.name,
                    expanded=inv_id in self._expanded_ids,
                    children=tuple(node_children),
                )
            )
        return TreeNodeState(
            node_id=root_id,
            item_type="project",
            label=project.name or tr("count_tab.tree.untitled", "(Untitled)"),
            expanded=root_id in self._expanded_ids,
            children=tuple(children),
        )

    def _build_editor_state(self) -> EditorState | None:
        current = self._current_lookup()
        if self._project is None:
            return None
        return self._editor_builder.build(
            self._project,
            current,
            self._voltage_report,
        )

    def _invalidate_results(self) -> None:
        self._voltage_report = None

    def _set_optional_length(
        self,
        target: object,
        field_name: str,
        value: str,
    ) -> None:
        if not value.strip():
            setattr(target, field_name, None)
            return
        parsed = self._parse_length(value)
        if parsed is not None:
            setattr(target, field_name, parsed)

    def _lynx_target(self, item_type: str) -> object | None:
        if self._project is None:
            return None
        if item_type == "project":
            return self._project
        current = self._current_lookup()
        if current is None or current[0] != item_type:
            return None
        return current[1]

    def _set_lynx_preset(self, item_type: str, value: str) -> None:
        target = self._lynx_target(item_type)
        if target is None:
            return
        setattr(target, "lynx_preset", value if value else None)
        self._mark_project_changed()
        self._refresh_editor()

    def _create_and_select_lynx_preset(self, item_type: str) -> None:
        if self._project is None:
            return
        target = self._lynx_target(item_type)
        if target is None:
            return
        preset = self._product_preset_service.create_lynx_preset(
            self._project
        )
        setattr(target, "lynx_preset", preset.name)
        self._mark_project_changed()
        self._refresh_editor()

    def _parse_length(self, value: str) -> Length | None:
        if self._project is None or not value.strip():
            return None
        try:
            return Length(value.strip(), registry=self._project.identifier_registry)
        except LengthError as exc:
            self.error_occurred.emit(
                tr(f"length.error.{exc.code}_title",
                   tr("length_identifiers_tab.dimension_error_title", "Error")),
                tr(f"length.error.{exc.code}", str(exc)).format(**exc.params),
            )
            return None

    def _register(self, item_type: str, item: object) -> str:
        key = self._key_for(item)
        self._node_lookup[key] = (item_type, item)
        return key

    def _set_single_selection(self, node_id: str | None) -> None:
        self._selected_node_ids = [node_id] if node_id is not None else []
        self._selected_node_id = node_id

    def _normalize_selected_node_ids(self) -> None:
        requested = set(self._selected_node_ids)
        normalized = [
            node_id for node_id in self._node_lookup if node_id in requested
        ]
        if normalized:
            scope = self._selection_scope(normalized[0])
            normalized = [
                node_id
                for node_id in normalized
                if self._selection_scope(node_id) == scope
            ]
        self._selected_node_ids = normalized
        self._selected_node_id = normalized[0] if len(normalized) == 1 else None

    def _selection_scope(self, node_id: str) -> tuple[str, int] | None:
        if self._project is None:
            return None
        lookup = self._node_lookup.get(node_id)
        if lookup is None:
            return None
        item_type, item = lookup
        try:
            parent = self._tree_service.resolve_parent_context(
                self._project, item_type, item
            ).parent
        except ValueError:
            return None
        return item_type, id(parent)

    def _selected_sibling_context(
        self,
    ) -> tuple[str, list, list[object]] | None:
        if self._project is None or not self._selected_node_ids:
            return None
        lookups = [
            self._node_lookup[node_id]
            for node_id in self._selected_node_ids
            if node_id in self._node_lookup
        ]
        if len(lookups) != len(self._selected_node_ids) or not lookups:
            return None
        item_type, first = lookups[0]
        try:
            parent = self._tree_service.resolve_parent_context(
                self._project, item_type, first
            ).parent
        except ValueError:
            return None
        if any(
            other_type != item_type
            or self._tree_service.resolve_parent_context(
                self._project, other_type, item
            ).parent is not parent
            for other_type, item in lookups[1:]
        ):
            return None
        if item_type == "inv" and isinstance(parent, Project):
            collection = parent.invs
        elif item_type == "node" and isinstance(parent, Inv):
            collection = parent.nodes
        elif item_type == "row" and isinstance(parent, Node):
            collection = parent.rows
        else:
            return None
        selected_ids = {id(item) for _kind, item in lookups}
        selected = [item for item in collection if id(item) in selected_ids]
        return item_type, collection, selected

    @staticmethod
    def _key_for(item: object) -> str:
        return tree_item_key(item)

    def _current_lookup(self) -> tuple[str, object] | None:
        if len(self._selected_node_ids) != 1 or self._selected_node_id is None:
            return None
        return self._node_lookup.get(self._selected_node_id)

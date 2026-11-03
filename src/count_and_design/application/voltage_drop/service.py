from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import Event
from typing import TYPE_CHECKING, TypeVar

from ...models import Length, ProductPreset, Project, PVPanelInfo, Wire
from ...models.length import LengthError, get_length_field_names
from ..product_generation.lynx import _allocate_overmolds, _calculate_slot_positions
from ..product_generation.parts_factory import _resolve_product_presets
from .models import (
    LevelVoltageDrop,
    LineSegmentDrop,
    ProductVoltageDrop,
    VoltageDropIssue,
    VoltageDropIssueSource,
    VoltageDropReport,
)
from .resistance import get_resistance_ohms_per_kft

if TYPE_CHECKING:
    from ...models import Inv, Node, Row
    from ..product_generation.result import ProductGenerationResult


@dataclass
class _Context:
    system_voltage: float
    string_current: float
    segments: list[LineSegmentDrop]
    issues: list[VoltageDropIssue]
    cancel_event: Event | None = None


@dataclass(frozen=True)
class _LengthCandidate:
    label: str
    length: Length
    source: VoltageDropIssueSource


_T = TypeVar("_T")


class VoltageCalculationCancelled(RuntimeError):
    """Raised when a voltage-drop calculation is cooperatively cancelled."""


class _SourcedLengthError(LengthError):
    def __init__(
        self,
        original: LengthError,
        candidates: Iterable[_LengthCandidate],
    ) -> None:
        super().__init__(original.code, **original.params)
        self.candidates = tuple(candidates)


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


class VoltageDropService:
    """Calculate voltage drop using the same physical layout as product generation."""

    _ISSUE_PRIORITY = {
        "invalid_project_type": 0,
        "missing_rackings": 10,
        "missing_rackings_selection": 10,
        "missing_product_preset": 10,
        "no_eligible_products": 10,
        "missing_racking_pitch": 10,
        "missing_lynx_preset": 10,
        "missing_pv_panel": 10,
        "invalid_vmp": 10,
        "invalid_imp": 10,
        "invalid_module_count": 10,
        "invalid_length_expression": 10,
        "invalid_ibex_branches": 10,
        "branch_count_mismatch": 10,
        "missing_branch_coordinate": 10,
        "invalid_lynx_layout": 10,
        "negative_length": 20,
        "missing_wire": 20,
        "missing_resistance": 20,
        "missing_product_code": 30,
        "no_products": 40,
    }

    def calculate(
        self,
        project: Project,
        generation_result: ProductGenerationResult | None = None,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
        cancel_event: Event | None = None,
    ) -> VoltageDropReport:
        total_rows = sum(
            len(node.rows) for inv in project.invs for node in inv.nodes
        )
        total_nodes = sum(len(inv.nodes) for inv in project.invs)
        total_work = max(1, total_rows + total_nodes + len(project.invs) + 1)
        completed_work = 0

        def check_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise VoltageCalculationCancelled("Voltage-drop calculation cancelled.")

        def advance() -> None:
            nonlocal completed_work
            check_cancelled()
            completed_work += 1
            if progress_callback is not None:
                progress_callback(min(completed_work, total_work), total_work)

        check_cancelled()
        if progress_callback is not None:
            progress_callback(0, total_work)
        root_issues: list[VoltageDropIssue] = []
        if project.project_type not in {"IBEX", "LYNX"}:
            root_issues.append(
                VoltageDropIssue(
                    "invalid_project_type",
                    "Project type must be IBEX or LYNX.",
                    project.name,
                    source=self._source("project", project, "project_type"),
                )
            )

        # Context values are switched to the explicitly selected named panel
        # before each product calculation.
        ctx = _Context(0.0, 0.0, [], root_issues, cancel_event)
        products: list[ProductVoltageDrop] = []
        row_products: dict[int, list[ProductVoltageDrop]] = defaultdict(list)
        node_products: dict[int, list[ProductVoltageDrop]] = defaultdict(list)
        node_lynx: dict[int, ProductVoltageDrop] = {}
        node_layout_issues: dict[int, list[VoltageDropIssue]] = defaultdict(list)
        row_input_issues: dict[int, list[VoltageDropIssue]] = defaultdict(list)

        for inv_index, inv in enumerate(project.invs, 1):
            check_cancelled()
            inherited_preset_name = inv.lynx_preset if inv.lynx_preset is not None else project.lynx_preset
            inherited_pitch = inv.racking_pitch if inv.racking_pitch is not None else project.racking_pitch
            for node_index, node in enumerate(inv.nodes, 1):
                check_cancelled()
                preset_name = node.lynx_preset if node.lynx_preset is not None else inherited_preset_name
                pitch = node.racking_pitch if node.racking_pitch is not None else inherited_pitch
                node_path = f"{inv.name} / {node.name}"

                for row_index, row in enumerate(node.rows, 1):
                    check_cancelled()
                    row_name = row.name or f"Row{row_index}"
                    for entry_index, entry in enumerate(row.racking_entries, 1):
                        entry_field = f"racking_entries[{entry_index - 1}].rackings"
                        if not entry.rackings:
                            issue = VoltageDropIssue(
                                "missing_rackings_selection",
                                "A Rackings selection is required.",
                                f"{node_path} / {row_name}",
                                source=self._source("row", row, entry_field),
                            )
                            self._append_issue(ctx.issues, issue)
                            self._append_issue(row_input_issues[id(row)], issue)
                            continue
                        rackings = project.get_rackings(entry.rackings)
                        if rackings is None:
                            issue = VoltageDropIssue(
                                "missing_rackings",
                                f"Rackings '{entry.rackings}' was not found.",
                                f"{node_path} / {row_name}",
                                {"name": entry.rackings},
                                self._source("row", row, entry_field),
                            )
                            self._append_issue(ctx.issues, issue)
                            self._append_issue(row_input_issues[id(row)], issue)
                            continue

                        missing_names = [
                            name
                            for name in rackings.product_presets
                            if project.get_product_preset(name) is None
                        ]
                        if missing_names:
                            issue = VoltageDropIssue(
                                "missing_product_preset",
                                "Rackings references missing product definitions: {names}.",
                                f"{node_path} / {row_name}",
                                {"names": ", ".join(dict.fromkeys(missing_names))},
                                self._source("rackings", rackings, "product_presets"),
                            )
                            self._append_issue(ctx.issues, issue)
                            self._append_issue(row_input_issues[id(row)], issue)

                        resolved_presets = _resolve_product_presets(rackings, project)
                        eligible_presets = [
                            preset
                            for preset in resolved_presets
                            if preset.product_type in {"IBEX", "Homerun_Extender"}
                            and preset.branch_count > 0
                        ]
                        if not eligible_presets and not missing_names:
                            issue = VoltageDropIssue(
                                "no_eligible_products",
                                "Rackings has no IBEX or Homerun product definition for voltage-drop calculation.",
                                f"{node_path} / {row_name}",
                                source=self._source("rackings", rackings, "product_presets"),
                            )
                            self._append_issue(ctx.issues, issue)
                            self._append_issue(row_input_issues[id(row)], issue)
                        if pitch is None:
                            issue = VoltageDropIssue(
                                "missing_racking_pitch",
                                "Racking pitch is required for product layout.",
                                f"{node_path} / {row_name}",
                                source=self._pitch_source(project, inv, node),
                            )
                            self._append_issue(ctx.issues, issue)
                            self._append_issue(row_input_issues[id(row)], issue)
                            continue

                        extender = Length("0mm")
                        occurrence = 0
                        for preset in resolved_presets:
                            check_cancelled()
                            if preset.product_type not in {
                                "IBEX", "Homerun_Extender", "Placeholder"
                            }:
                                continue
                            if (
                                preset.product_type == "Placeholder"
                                or preset.branch_count == 0
                            ):
                                try:
                                    extender = self._length_op(
                                        lambda: extender + preset.product_total_length,
                                        (
                                            _LengthCandidate(
                                                f"ProductPreset[{preset.name}].product_total_length",
                                                preset.product_total_length,
                                                self._source(
                                                    "product_preset",
                                                    preset,
                                                    "product_total_length",
                                                ),
                                            ),
                                        ),
                                    )
                                except LengthError as exc:
                                    ctx.issues.append(
                                        self._length_issue(
                                            exc,
                                            f"{node_path} / {row_name} / {preset.name}",
                                            field=f"ProductPreset[{preset.name}].product_total_length",
                                            expression=preset.product_total_length.length_str,
                                            source=self._source(
                                                "product_preset", preset, "product_total_length"
                                            ),
                                        )
                                    )
                                continue
                            occurrence += 1
                            actual_type = (
                                "Homerun_Extender"
                                if preset.product_type == "Homerun_Extender" or preset.branch_count <= 1
                                else "IBEX"
                            )
                            instance_id = (
                                f"I{inv_index}.N{node_index}.R{row_index}.E{entry_index}.P{occurrence}"
                            )
                            product = ProductVoltageDrop(
                                instance_id=instance_id,
                                product_type=actual_type,
                                preset_name=preset.name,
                                inv_name=inv.name,
                                node_name=node.name,
                                row_name=row_name,
                                rackings_name=entry.rackings,
                                entry_index=entry_index,
                                string_count=preset.branch_count,
                                source_preset=preset,
                                source_node=node,
                                source_row=row,
                                source_rackings=rackings,
                                source_entry_index=entry_index - 1,
                                source_pitch=self._pitch_source(project, inv, node),
                            )
                            panel = self._configure_product_panel(ctx, project, preset, product)
                            if panel is not None:
                                try:
                                    rtr = abs(node.box_position - row_index)
                                    pitch_candidate = _LengthCandidate(
                                        f"{product.source_pitch.object_kind}.racking_pitch",
                                        pitch,
                                        product.source_pitch,
                                    )
                                    offset_candidate = _LengthCandidate(
                                        f"Row[{row_name}].racking_entries[{entry_index}].offset",
                                        entry.offset,
                                        self._source(
                                            "row",
                                            row,
                                            f"racking_entries[{entry_index - 1}].offset",
                                        ),
                                    )
                                    whip = self._length_op(
                                        lambda: pitch * rtr + entry.offset,
                                        (pitch_candidate, offset_candidate),
                                    )
                                    if project.project_type == "LYNX":
                                        whip = Length("0mm")
                                    if actual_type == "Homerun_Extender":
                                        self._calculate_homerun(ctx, product, preset, whip, extender)
                                    else:
                                        self._calculate_ibex(ctx, product, preset, whip, extender)
                                except LengthError as exc:
                                    pitch_field = (
                                        f"Node[{node.name}].racking_pitch"
                                        if node.racking_pitch is not None
                                        else f"Inv[{inv.name}].racking_pitch"
                                        if inv.racking_pitch is not None
                                        else "Project.racking_pitch"
                                    )
                                    candidates = [
                                        _LengthCandidate(
                                            pitch_field,
                                            pitch,
                                            self._pitch_source(project, inv, node),
                                        ),
                                        _LengthCandidate(
                                            f"Row[{row_name}].racking_entries[{entry_index}].offset",
                                            entry.offset,
                                            self._source(
                                                "row",
                                                row,
                                                f"racking_entries[{entry_index - 1}].offset",
                                            ),
                                        ),
                                    ]
                                    candidates.extend(self._preset_length_candidates(preset))
                                    issue = self._diagnose_length_issue(exc, product.path, candidates)
                                    self._add_existing_issue(ctx, product, issue)
                            products.append(product)
                            row_products[id(row)].append(product)
                            node_products[id(node)].append(product)
                            try:
                                extender = self._length_op(
                                    lambda: extender + preset.product_total_length,
                                    (
                                        _LengthCandidate(
                                            f"ProductPreset[{preset.name}].product_total_length",
                                            preset.product_total_length,
                                            self._source(
                                                "product_preset",
                                                preset,
                                                "product_total_length",
                                            ),
                                        ),
                                    ),
                                )
                            except LengthError as exc:
                                self._add_existing_issue(
                                    ctx,
                                    product,
                                    self._length_issue(
                                        exc,
                                        product.path,
                                        field=f"ProductPreset[{preset.name}].product_total_length",
                                        expression=preset.product_total_length.length_str,
                                        source=self._source(
                                            "product_preset", preset, "product_total_length"
                                        ),
                                    ),
                                )

                    advance()

                if project.project_type == "LYNX":
                    lynx_preset = project.get_product_preset(preset_name) if preset_name else None
                    if lynx_preset is None or lynx_preset.product_type != "LYNX":
                        issue = VoltageDropIssue(
                            "missing_lynx_preset",
                            "A valid LYNX preset is required.",
                            node_path,
                            source=self._lynx_preset_source(project, inv, node),
                        )
                        ctx.issues.append(issue)
                        node_layout_issues[id(node)].append(issue)
                    elif pitch is None:
                        issue = VoltageDropIssue(
                            "missing_racking_pitch",
                            "Racking pitch is required for LYNX layout.",
                            node_path,
                            source=self._pitch_source(project, inv, node),
                        )
                        ctx.issues.append(issue)
                        node_layout_issues[id(node)].append(issue)
                    else:
                        try:
                            lynx_product = self._calculate_lynx(
                                ctx,
                                project,
                                inv,
                                node,
                                lynx_preset,
                                pitch,
                                inv_index,
                                node_index,
                            )
                        except LengthError as exc:
                            lynx_product = ProductVoltageDrop(
                                instance_id=f"I{inv_index}.N{node_index}.LYNX",
                                product_type="LYNX",
                                preset_name=lynx_preset.name,
                                inv_name=inv.name,
                                node_name=node.name,
                                source_preset=lynx_preset,
                                source_node=node,
                                source_pitch=self._pitch_source(project, inv, node),
                            )
                            candidates = [
                                _LengthCandidate(
                                    f"Node[{node.name}].racking_pitch"
                                    if node.racking_pitch is not None
                                    else f"Inv[{inv.name}].racking_pitch"
                                    if inv.racking_pitch is not None
                                    else "Project.racking_pitch",
                                    pitch,
                                    self._pitch_source(project, inv, node),
                                ),
                            ]
                            if node.cluster_length is not None:
                                candidates.append(
                                    _LengthCandidate(
                                        f"Node[{node.name}].cluster_length",
                                        node.cluster_length,
                                        self._source("node", node, "cluster_length"),
                                    )
                                )
                            for candidate_row in node.rows:
                                for candidate_entry_index, candidate_entry in enumerate(
                                    candidate_row.racking_entries
                                ):
                                    candidates.append(
                                        _LengthCandidate(
                                            f"Row[{candidate_row.name}].racking_entries[{candidate_entry_index + 1}].offset",
                                            candidate_entry.offset,
                                            self._source(
                                                "row",
                                                candidate_row,
                                                f"racking_entries[{candidate_entry_index}].offset",
                                            ),
                                        )
                                    )
                            relevant_presets: list[ProductPreset] = [lynx_preset]
                            for candidate_row in node.rows:
                                for candidate_entry in candidate_row.racking_entries:
                                    candidate_rackings = project.get_rackings(
                                        candidate_entry.rackings
                                    )
                                    if candidate_rackings is not None:
                                        relevant_presets.extend(
                                            _resolve_product_presets(
                                                candidate_rackings, project
                                            )
                                        )
                            seen_presets: set[int] = set()
                            for candidate_preset in relevant_presets:
                                if id(candidate_preset) in seen_presets:
                                    continue
                                seen_presets.add(id(candidate_preset))
                                candidates.extend(
                                    self._preset_length_candidates(candidate_preset)
                                )
                            issue = self._diagnose_length_issue(exc, node_path, candidates)
                            self._add_existing_issue(ctx, lynx_product, issue)
                        products.append(lynx_product)
                        node_lynx[id(node)] = lynx_product
                advance()

        if generation_result is not None:
            self._attach_product_codes(products, generation_result)
            for product in products:
                if not product.positive_code or not product.negative_code:
                    self._product_issue(
                        ctx,
                        product,
                        "missing_product_code",
                        "The generated positive and negative product codes could not be paired.",
                    )

        levels: list[LevelVoltageDrop] = []
        levels_by_object_id: dict[int, LevelVoltageDrop] = {}
        inv_levels: list[LevelVoltageDrop] = []

        for inv in project.invs:
            check_cancelled()
            node_levels: list[LevelVoltageDrop] = []
            for node in inv.nodes:
                row_levels: list[LevelVoltageDrop] = []
                for row_index, row in enumerate(node.rows, 1):
                    row_name = row.name or f"Row{row_index}"
                    row_level = self._build_product_average_level(
                        "Row",
                        row_name,
                        f"{inv.name} / {node.name} / {row_name}",
                        row_products[id(row)],
                        source_object=row,
                        extra_issues=row_input_issues[id(row)],
                    )
                    row_levels.append(row_level)
                    levels.append(row_level)
                    levels_by_object_id[id(row)] = row_level

                source_products = node_products[id(node)]
                node_issue_list = list(node_layout_issues[id(node)])
                node_issue_list.extend(issue for level in row_levels for issue in level.issues)
                source_average = _mean(
                    product.voltage_drop
                    for product in source_products
                    if product.voltage_drop is not None
                )
                if not source_products:
                    node_issue_list.append(
                        VoltageDropIssue(
                            "no_products",
                            "No IBEX or Homerun Extender products are available.",
                            f"{inv.name} / {node.name}",
                            source=self._source("node", node, "__object__"),
                        )
                    )
                if len([p for p in source_products if p.voltage_drop is not None]) != len(source_products):
                    source_average = None
                source_percent = _mean(
                    product.voltage_drop_percent
                    for product in source_products
                    if product.voltage_drop_percent is not None
                )
                if len([p for p in source_products if p.voltage_drop_percent is not None]) != len(source_products):
                    source_percent = None

                node_value = source_average
                node_percent = source_percent
                lynx = node_lynx.get(id(node))
                if project.project_type == "LYNX":
                    if lynx is None or lynx.voltage_drop is None:
                        node_value = None
                        node_percent = None
                        if lynx is not None:
                            node_issue_list.extend(lynx.issues)
                    elif source_average is not None:
                        node_value = source_average + lynx.voltage_drop
                        node_percent = (
                            source_percent + lynx.voltage_drop_percent
                            if source_percent is not None and lynx.voltage_drop_percent is not None
                            else None
                        )

                node_level = self._make_level(
                    "Node",
                    node.name,
                    f"{inv.name} / {node.name}",
                    node_value,
                    product_count=len(source_products) + (1 if lynx else 0),
                    child_count=len(node.rows),
                    issues=node_issue_list,
                    percent_value=node_percent,
                )
                node_levels.append(node_level)
                levels.append(node_level)
                levels_by_object_id[id(node)] = node_level

            inv_issues = [issue for level in node_levels for issue in level.issues]
            inv_value = self._valid_level_mean(node_levels)
            inv_percent = self._valid_level_percent_mean(node_levels)
            inv_level = self._make_level(
                "INV",
                inv.name,
                inv.name,
                inv_value,
                product_count=sum(level.product_count for level in node_levels),
                child_count=len(node_levels),
                issues=inv_issues,
                percent_value=inv_percent,
            )
            inv_levels.append(inv_level)
            levels.append(inv_level)
            levels_by_object_id[id(inv)] = inv_level
            advance()

        project_issues = list(ctx.issues)
        project_issues.extend(issue for level in inv_levels for issue in level.issues)
        project_issues = self._deduplicate_issues(project_issues)
        project_value = self._valid_level_mean(inv_levels)
        project_percent = self._valid_level_percent_mean(inv_levels)
        project_level = self._make_level(
            "Project",
            project.name,
            project.name,
            project_value,
            product_count=len(products),
            child_count=len(inv_levels),
            issues=project_issues,
            percent_value=project_percent,
        )
        levels_by_object_id[id(project)] = project_level
        advance()
        ordered_levels = [project_level]
        for inv in project.invs:
            ordered_levels.append(levels_by_object_id[id(inv)])
            for node in inv.nodes:
                ordered_levels.append(levels_by_object_id[id(node)])
                for row in node.rows:
                    ordered_levels.append(levels_by_object_id[id(row)])

        return VoltageDropReport(
            project_level=project_level,
            levels=ordered_levels,
            products=products,
            segments=ctx.segments,
            issues=project_issues,
            _levels_by_object_id=levels_by_object_id,
            _products_by_object_id={
                **{key: list(value) for key, value in row_products.items()},
                **{key: list(value) for key, value in node_products.items()},
            },
        )

    def _calculate_ibex(
        self,
        ctx: _Context,
        product: ProductVoltageDrop,
        preset: ProductPreset,
        whip: Length,
        extender: Length,
    ) -> None:
        branches = preset.branch_lines
        if len(branches) < 2:
            self._product_issue(ctx, product, "invalid_ibex_branches", "IBEX requires at least two branches.")
            return
        if len(branches) != preset.branch_count:
            self._product_issue(
                ctx,
                product,
                "branch_count_mismatch",
                f"branch_count is {preset.branch_count}, but {len(branches)} branch coordinates are configured.",
                params={"expected": preset.branch_count, "actual": len(branches)},
            )
            return
        current = ctx.string_current
        total_current = preset.branch_count * current

        self._add_ibex_external(ctx, product, preset, whip, extender, total_current)

        # Standard branch drops exist at B1..B(N-1).
        for branch_index in range(len(branches) - 1):
            label = f"Branch {branch_index + 1}"
            self._add_segment(
                ctx, product, "POS", label, preset.branch_cu_wire,
                preset.branch_length_positive, current,
                length_fields=("branch_length_positive",),
            )
            self._add_segment(
                ctx, product, "NEG", label, preset.branch_cu_wire,
                preset.branch_length_negative, current,
                length_fields=("branch_length_negative",),
            )

        # Internal mainline ends at B(N-1); B(N-1)->BN belongs to the terminal branch.
        for index in range(len(branches) - 2):
            previous = branches[index]
            following = branches[index + 1]
            passing_current = (preset.branch_count - index - 1) * current
            self._add_internal_span(
                ctx,
                product,
                preset,
                previous.coordinate_positive,
                following.coordinate_positive,
                "POS",
                f"Internal Mainline B{index + 1}-B{index + 2}",
                passing_current,
                f"branch_lines[{index}].coordinate_positive",
                f"branch_lines[{index + 1}].coordinate_positive",
            )
            self._add_internal_span(
                ctx,
                product,
                preset,
                previous.coordinate_negative,
                following.coordinate_negative,
                "NEG",
                f"Internal Mainline B{index + 1}-B{index + 2}",
                passing_current,
                f"branch_lines[{index}].coordinate_negative",
                f"branch_lines[{index + 1}].coordinate_negative",
            )

        self._add_terminal_branch(ctx, product, preset, current)
        self._finalize_product(ctx, product)

    def _add_ibex_external(
        self,
        ctx: _Context,
        product: ProductVoltageDrop,
        preset: ProductPreset,
        whip: Length,
        extender: Length,
        current: float,
    ) -> None:
        first = preset.branch_lines[0]
        has_al = preset.external_al_wire.wiresize != "none"
        has_fuse = preset.external_fuse.current != "none"
        for polarity, coordinate in (
            ("POS", first.coordinate_positive),
            ("NEG", first.coordinate_negative),
        ):
            coordinate_field = (
                "branch_lines[0].coordinate_positive"
                if polarity == "POS"
                else "branch_lines[0].coordinate_negative"
            )
            coordinate_source = self._source(
                "product_preset", preset, coordinate_field
            )
            base_source = self._preset_negative_source(
                product,
                preset,
                (
                    "external_cu_length_to_row_head",
                    "external_extension_trench_length",
                ),
                include_layout=True,
            )
            mainline_fallback = self._source(
                "product_preset",
                preset,
                coordinate_field,
                "external_cu_length_to_row_head",
                "external_extension_trench_length",
                "external_fuse_suffix",
                "external_second_cual_suffix",
            )
            mainline_source = self._negative_operand_source(
                self._product_layout_candidates(product)
                + [
                    (coordinate, coordinate_source),
                    (
                        preset.external_cu_length_to_row_head,
                        self._source(
                            "product_preset", preset, "external_cu_length_to_row_head"
                        ),
                    ),
                    (
                        preset.external_extension_trench_length,
                        self._source(
                            "product_preset",
                            preset,
                            "external_extension_trench_length",
                        ),
                    ),
                    (
                        preset.external_fuse_suffix,
                        self._source("product_preset", preset, "external_fuse_suffix"),
                    ),
                    (
                        preset.external_second_cual_suffix,
                        self._source(
                            "product_preset", preset, "external_second_cual_suffix"
                        ),
                    ),
                ],
                mainline_fallback,
            )
            coordinate_candidate = _LengthCandidate(
                f"ProductPreset[{preset.name}].{coordinate_field}",
                coordinate,
                coordinate_source,
            )
            threshold_candidate = _LengthCandidate(
                f"ProductPreset[{preset.name}].external_al_threshold",
                preset.external_al_threshold,
                self._source("product_preset", preset, "external_al_threshold"),
            )
            need_al = has_al and self._length_op(
                lambda: coordinate + extender >= preset.external_al_threshold,
                (coordinate_candidate, threshold_candidate),
            )
            cu_head_candidate = _LengthCandidate(
                f"ProductPreset[{preset.name}].external_cu_length_to_row_head",
                preset.external_cu_length_to_row_head,
                self._source(
                    "product_preset", preset, "external_cu_length_to_row_head"
                ),
            )
            trench_candidate = _LengthCandidate(
                f"ProductPreset[{preset.name}].external_extension_trench_length",
                preset.external_extension_trench_length,
                self._source(
                    "product_preset", preset, "external_extension_trench_length"
                ),
            )
            base = self._length_op(
                lambda: whip
                + preset.external_cu_length_to_row_head
                + preset.external_extension_trench_length,
                (cu_head_candidate, trench_candidate),
            )
            if not need_al:
                if has_fuse and polarity == "POS":
                    self._add_segment(
                        ctx, product, polarity, "External Mainline",
                        preset.external_cu_wire, base, current,
                        length_source=base_source,
                    )
                    self._add_segment(
                        ctx,
                        product,
                        polarity,
                        "External Mainline",
                        preset.external_cu_wire,
                        self._length_op(
                            lambda: preset.external_fuse_suffix + extender + coordinate,
                            (
                                _LengthCandidate(
                                    f"ProductPreset[{preset.name}].external_fuse_suffix",
                                    preset.external_fuse_suffix,
                                    self._source(
                                        "product_preset", preset, "external_fuse_suffix"
                                    ),
                                ),
                                coordinate_candidate,
                            ),
                        ),
                        current,
                        length_source=mainline_source,
                    )
                else:
                    self._add_segment(
                        ctx,
                        product,
                        polarity,
                        "External Mainline",
                        preset.external_cu_wire,
                        self._length_op(
                            lambda: base + extender + coordinate,
                            (cu_head_candidate, coordinate_candidate),
                        ),
                        current,
                        length_source=mainline_source,
                    )
                continue

            if has_fuse and polarity == "POS":
                self._add_segment(
                    ctx, product, polarity, "External Mainline", preset.external_cu_wire,
                    base, current, length_source=base_source,
                )
                self._add_segment(
                    ctx, product, polarity, "External Mainline", preset.external_cu_wire,
                    preset.external_fuse_suffix, current,
                    length_fields=("external_fuse_suffix",),
                )
                al_length = self._length_op(
                    lambda: extender
                    + coordinate
                    - preset.external_second_cual_suffix,
                    (
                        coordinate_candidate,
                        _LengthCandidate(
                            f"ProductPreset[{preset.name}].external_second_cual_suffix",
                            preset.external_second_cual_suffix,
                            self._source(
                                "product_preset", preset, "external_second_cual_suffix"
                            ),
                        ),
                    ),
                )
            else:
                self._add_segment(
                    ctx, product, polarity, "External Mainline", preset.external_cu_wire,
                    base, current, length_source=base_source,
                )
                # Preserve the current product-generation split for the no-fuse case.
                al_length = (
                    extender
                    if not has_fuse
                    else self._length_op(
                        lambda: extender + coordinate,
                        (coordinate_candidate,),
                    )
                )
            self._add_segment(
                ctx, product, polarity, "External Mainline", preset.external_al_wire,
                al_length, current, length_source=mainline_source,
            )
            self._add_segment(
                ctx, product, polarity, "External Mainline", preset.external_cu_wire,
                preset.external_second_cual_suffix, current,
                length_fields=("external_second_cual_suffix",),
            )

    def _add_internal_span(
        self,
        ctx: _Context,
        product: ProductVoltageDrop,
        preset: ProductPreset,
        start: Length,
        end: Length,
        polarity: str,
        role: str,
        current: float,
        start_field: str,
        end_field: str,
    ) -> None:
        span_candidates = (
            _LengthCandidate(
                f"ProductPreset[{preset.name}].{start_field}",
                start,
                self._source("product_preset", preset, start_field),
            ),
            _LengthCandidate(
                f"ProductPreset[{preset.name}].{end_field}",
                end,
                self._source("product_preset", preset, end_field),
            ),
        )
        span = self._length_op(lambda: end - start, span_candidates)
        use_al = preset.internal_al_wire.wiresize != "none" and self._length_op(
            lambda: span >= preset.internal_al_threshold,
            span_candidates
            + (
                _LengthCandidate(
                    f"ProductPreset[{preset.name}].internal_al_threshold",
                    preset.internal_al_threshold,
                    self._source("product_preset", preset, "internal_al_threshold"),
                ),
            ),
        )
        if not use_al:
            self._add_segment(
                ctx, product, polarity, role, preset.internal_cu_wire, span, current,
                length_fields=(start_field, end_field),
            )
            return
        self._add_segment(
            ctx, product, polarity, role, preset.internal_cu_wire,
            preset.internal_first_cual_prefix, current,
            length_fields=("internal_first_cual_prefix",),
        )
        self._add_segment(
            ctx,
            product,
            polarity,
            role,
            preset.internal_al_wire,
            self._length_op(
                lambda: span
                - preset.internal_first_cual_prefix
                - preset.internal_second_cual_suffix,
                span_candidates
                + (
                    _LengthCandidate(
                        f"ProductPreset[{preset.name}].internal_first_cual_prefix",
                        preset.internal_first_cual_prefix,
                        self._source(
                            "product_preset", preset, "internal_first_cual_prefix"
                        ),
                    ),
                    _LengthCandidate(
                        f"ProductPreset[{preset.name}].internal_second_cual_suffix",
                        preset.internal_second_cual_suffix,
                        self._source(
                            "product_preset", preset, "internal_second_cual_suffix"
                        ),
                    ),
                ),
            ),
            current,
            length_fields=(
                start_field, end_field, "internal_first_cual_prefix",
                "internal_second_cual_suffix",
            ),
        )
        self._add_segment(
            ctx, product, polarity, role, preset.internal_cu_wire,
            preset.internal_second_cual_suffix, current,
            length_fields=("internal_second_cual_suffix",),
        )

    def _add_terminal_branch(
        self,
        ctx: _Context,
        product: ProductVoltageDrop,
        preset: ProductPreset,
        current: float,
    ) -> None:
        previous = preset.branch_lines[-2]
        last = preset.branch_lines[-1]
        positive_candidates = tuple(
            _LengthCandidate(
                f"ProductPreset[{preset.name}].{field_name}",
                length,
                self._source("product_preset", preset, field_name),
            )
            for field_name, length in (
                (
                    f"branch_lines[{len(preset.branch_lines) - 2}].coordinate_positive",
                    previous.coordinate_positive,
                ),
                (
                    f"branch_lines[{len(preset.branch_lines) - 1}].coordinate_positive",
                    last.coordinate_positive,
                ),
            )
        )
        negative_candidates = tuple(
            _LengthCandidate(
                f"ProductPreset[{preset.name}].{field_name}",
                length,
                self._source("product_preset", preset, field_name),
            )
            for field_name, length in (
                (
                    f"branch_lines[{len(preset.branch_lines) - 2}].coordinate_negative",
                    previous.coordinate_negative,
                ),
                (
                    f"branch_lines[{len(preset.branch_lines) - 1}].coordinate_negative",
                    last.coordinate_negative,
                ),
            )
        )
        positive_span = self._length_op(
            lambda: last.coordinate_positive - previous.coordinate_positive,
            positive_candidates,
        )
        negative_span = self._length_op(
            lambda: last.coordinate_negative - previous.coordinate_negative,
            negative_candidates,
        )
        use_al = (
            preset.effective_last_branch_al_wire.wiresize != "none"
            and self._length_op(
                lambda: positive_span >= preset.last_branch_al_threshold,
                positive_candidates
                + (
                    _LengthCandidate(
                        f"ProductPreset[{preset.name}].last_branch_al_threshold",
                        preset.last_branch_al_threshold,
                        self._source(
                            "product_preset", preset, "last_branch_al_threshold"
                        ),
                    ),
                ),
            )
        )
        last_cu = preset.effective_last_branch_cu_wire
        last_al = preset.effective_last_branch_al_wire
        last_fuse = preset.effective_last_branch_fuse
        for polarity, span, extra in (
            ("POS", positive_span, preset.branch_length_positive),
            ("NEG", negative_span, preset.branch_length_negative),
        ):
            coordinate_fields = (
                f"branch_lines[{len(preset.branch_lines) - 2}].coordinate_{'positive' if polarity == 'POS' else 'negative'}",
                f"branch_lines[{len(preset.branch_lines) - 1}].coordinate_{'positive' if polarity == 'POS' else 'negative'}",
            )
            total_fields = coordinate_fields + (
                ("branch_length_positive",) if polarity == "POS" else ("branch_length_negative",)
            )
            extra_field = (
                "branch_length_positive" if polarity == "POS" else "branch_length_negative"
            )
            total = (
                self._length_op(
                    lambda: span + extra,
                    (positive_candidates if polarity == "POS" else negative_candidates)
                    + (
                        _LengthCandidate(
                            f"ProductPreset[{preset.name}].{extra_field}",
                            extra,
                            self._source("product_preset", preset, extra_field),
                        ),
                    ),
                )
                if preset.last_branch_with_extra_length
                else span
            )
            if not use_al:
                self._add_segment(
                    ctx, product, polarity, "Terminal Branch", last_cu,
                    total, current, length_fields=total_fields,
                )
                continue
            before = preset.branch_first_cual_prefix
            if polarity == "POS" and last_fuse.current != "none":
                before = self._length_op(
                    lambda: before + preset.branch_fuse_prefix,
                    (
                        _LengthCandidate(
                            f"ProductPreset[{preset.name}].branch_first_cual_prefix",
                            preset.branch_first_cual_prefix,
                            self._source(
                                "product_preset", preset, "branch_first_cual_prefix"
                            ),
                        ),
                        _LengthCandidate(
                            f"ProductPreset[{preset.name}].branch_fuse_prefix",
                            preset.branch_fuse_prefix,
                            self._source("product_preset", preset, "branch_fuse_prefix"),
                        ),
                    ),
                )
            after = preset.branch_second_cual_suffix
            before_fields = ("branch_first_cual_prefix",)
            if polarity == "POS" and last_fuse.current != "none":
                before_fields += ("branch_fuse_prefix",)
            self._add_segment(
                ctx, product, polarity, "Terminal Branch", last_cu,
                before, current, length_fields=before_fields,
            )
            self._add_segment(
                ctx, product, polarity, "Terminal Branch", last_al,
                self._length_op(
                    lambda: total - before - after,
                    (
                        _LengthCandidate(
                            f"ProductPreset[{preset.name}].branch_first_cual_prefix",
                            preset.branch_first_cual_prefix,
                            self._source(
                                "product_preset", preset, "branch_first_cual_prefix"
                            ),
                        ),
                        _LengthCandidate(
                            f"ProductPreset[{preset.name}].branch_second_cual_suffix",
                            after,
                            self._source(
                                "product_preset", preset, "branch_second_cual_suffix"
                            ),
                        ),
                    ),
                ), current,
                length_fields=total_fields + before_fields + ("branch_second_cual_suffix",),
            )
            self._add_segment(
                ctx, product, polarity, "Terminal Branch", last_cu,
                after, current, length_fields=("branch_second_cual_suffix",),
            )

    def _calculate_homerun(
        self,
        ctx: _Context,
        product: ProductVoltageDrop,
        preset: ProductPreset,
        whip: Length,
        extender: Length,
    ) -> None:
        if not preset.branch_lines:
            self._product_issue(ctx, product, "missing_branch_coordinate", "Homerun Extender requires one branch coordinate.")
            return
        first = preset.branch_lines[0]
        prefix = self._length_op(
            lambda: whip
            + preset.external_cu_length_to_row_head
            + preset.external_extension_trench_length,
            (
                _LengthCandidate(
                    f"ProductPreset[{preset.name}].external_cu_length_to_row_head",
                    preset.external_cu_length_to_row_head,
                    self._source(
                        "product_preset", preset, "external_cu_length_to_row_head"
                    ),
                ),
                _LengthCandidate(
                    f"ProductPreset[{preset.name}].external_extension_trench_length",
                    preset.external_extension_trench_length,
                    self._source(
                        "product_preset", preset, "external_extension_trench_length"
                    ),
                ),
            ),
        )
        for polarity, coordinate in (
            ("POS", first.coordinate_positive),
            ("NEG", first.coordinate_negative),
        ):
            coordinate_field = (
                "branch_lines[0].coordinate_positive"
                if polarity == "POS"
                else "branch_lines[0].coordinate_negative"
            )
            prefix_source = self._preset_negative_source(
                product,
                preset,
                (
                    "external_cu_length_to_row_head",
                    "external_extension_trench_length",
                ),
                include_layout=True,
            )
            mainline_source = self._negative_operand_source(
                [
                    (
                        coordinate,
                        self._source("product_preset", preset, coordinate_field),
                    ),
                    (
                        preset.branch_second_cual_suffix,
                        self._source(
                            "product_preset", preset, "branch_second_cual_suffix"
                        ),
                    ),
                ],
                self._source(
                    "product_preset",
                    preset,
                    coordinate_field,
                    "branch_second_cual_suffix",
                ),
            )
            self._add_segment(
                ctx, product, polarity, "Homerun Whip", preset.external_cu_wire,
                prefix, ctx.string_current, length_source=prefix_source,
            )
            coordinate_candidate = _LengthCandidate(
                f"ProductPreset[{preset.name}].{coordinate_field}",
                coordinate,
                self._source("product_preset", preset, coordinate_field),
            )
            total = self._length_op(
                lambda: coordinate + extender,
                (coordinate_candidate,),
            )
            use_al = preset.external_al_wire.wiresize != "none" and self._length_op(
                lambda: total >= preset.external_al_threshold,
                (
                    coordinate_candidate,
                    _LengthCandidate(
                        f"ProductPreset[{preset.name}].external_al_threshold",
                        preset.external_al_threshold,
                        self._source(
                            "product_preset", preset, "external_al_threshold"
                        ),
                    ),
                ),
            )
            if use_al:
                self._add_segment(
                    ctx, product, polarity, "Homerun Mainline", preset.external_al_wire,
                    self._length_op(
                        lambda: total - preset.branch_second_cual_suffix,
                        (
                            coordinate_candidate,
                            _LengthCandidate(
                                f"ProductPreset[{preset.name}].branch_second_cual_suffix",
                                preset.branch_second_cual_suffix,
                                self._source(
                                    "product_preset", preset, "branch_second_cual_suffix"
                                ),
                            ),
                        ),
                    ), ctx.string_current,
                    length_source=mainline_source,
                )
                self._add_segment(
                    ctx, product, polarity, "Homerun Mainline", preset.external_cu_wire,
                    preset.branch_second_cual_suffix, ctx.string_current,
                    length_fields=("branch_second_cual_suffix",),
                )
            else:
                self._add_segment(
                    ctx, product, polarity, "Homerun Mainline", preset.external_cu_wire,
                    total, ctx.string_current, length_source=mainline_source,
                )
        self._finalize_product(ctx, product)

    def _calculate_lynx(
        self,
        ctx: _Context,
        project: Project,
        inv: Inv,
        node: Node,
        preset: ProductPreset,
        pitch: Length,
        inv_index: int,
        node_index: int,
    ) -> ProductVoltageDrop:
        product = ProductVoltageDrop(
            instance_id=f"I{inv_index}.N{node_index}.LYNX",
            product_type="LYNX",
            preset_name=preset.name,
            inv_name=inv.name,
            node_name=node.name,
            source_preset=preset,
            source_node=node,
            source_pitch=self._pitch_source(project, inv, node),
        )
        lynx_panel = self._configure_product_panel(ctx, project, preset, product)
        if lynx_panel is None:
            return product
        if preset.row_per_overmold <= 0:
            self._product_issue(
                ctx,
                product,
                "invalid_lynx_layout",
                "Row per Overmold must be greater than 0.",
                source=self._source("product_preset", preset, "row_per_overmold"),
            )
            return product
        cluster_wire = preset.cluster_wire
        slot_positions = _calculate_slot_positions(node, preset)
        try:
            allocations = _allocate_overmolds(node, slot_positions, preset, project)
        except ValueError as exc:
            self._product_issue(
                ctx,
                product,
                "invalid_lynx_layout",
                str(exc),
                source=self._lynx_layout_source(project, node),
            )
            return product

        branch_currents: list[float] = []
        allocation_currents: list[float] = []
        for slot_position, nearby_index, allocated_products in allocations:
            allocation_current = 0.0
            for source_preset, whip, row_position in allocated_products:
                source_panel = self._resolve_panel(project, source_preset)
                if source_panel is None or not self._validate_panel(ctx, product, source_panel, source_preset.pv_panel):
                    continue
                source_current = source_preset.branch_count * source_panel.imp
                allocation_current += source_current
                branch_currents.append(source_current)
                whip_source = self._lynx_whip_source(node, row_position, whip)
                copper_candidates = (
                    _LengthCandidate(
                        f"ProductPreset[{preset.name}].branch_length_cu",
                        preset.branch_length_cu,
                        self._source("product_preset", preset, "branch_length_cu"),
                    ),
                    _LengthCandidate(
                        "Racking pitch",
                        pitch,
                        product.source_pitch,
                    ),
                    _LengthCandidate(
                        f"ProductPreset[{preset.name}].nearby_overmold_gap",
                        preset.nearby_overmold_gap,
                        self._source(
                            "product_preset", preset, "nearby_overmold_gap"
                        ),
                    ),
                    _LengthCandidate("Row offset", whip, whip_source),
                )
                copper_length = self._length_op(
                    lambda: preset.branch_length_cu
                    + pitch * abs(slot_position - row_position)
                    + preset.nearby_overmold_gap * nearby_index
                    + whip,
                    copper_candidates,
                )
                copper_source = self._negative_operand_source(
                    [
                        (
                            preset.branch_length_cu,
                            self._source("product_preset", preset, "branch_length_cu"),
                        ),
                        (pitch, product.source_pitch),
                        (
                            preset.nearby_overmold_gap,
                            self._source(
                                "product_preset", preset, "nearby_overmold_gap"
                            ),
                        ),
                        (whip, whip_source),
                    ],
                    self._source(
                        "product_preset",
                        preset,
                        "branch_length_cu",
                        "nearby_overmold_gap",
                    ),
                )
                for polarity in ("POS", "NEG"):
                    self._add_segment(
                        ctx, product, polarity, "LYNX Branch", preset.branch_al_wire,
                        preset.branch_length_al, source_current,
                        length_fields=("branch_length_al",),
                    )
                    self._add_segment(
                        ctx, product, polarity, "LYNX Branch", preset.branch_cu_wire,
                        copper_length, source_current,
                        length_source=copper_source,
                    )
            allocation_currents.append(allocation_current)

        product.string_count = sum(p.branch_count for _, _, batch in allocations for p, _, _ in batch)
        total_current = sum(branch_currents)
        cluster_length = node.cluster_length if node.cluster_length is not None else Length("0mm")
        first_slot_length = (
            self._length_op(
                lambda: pitch * abs(slot_positions[0] - node.box_position),
                (_LengthCandidate("Racking pitch", pitch, product.source_pitch),),
            )
            if slot_positions
            else Length("0mm")
        )
        external_length = self._length_op(
            lambda: cluster_length + preset.buffer_length + first_slot_length,
            (
                _LengthCandidate(
                    f"Node[{node.name}].cluster_length",
                    cluster_length,
                    self._source("node", node, "cluster_length"),
                ),
                _LengthCandidate(
                    f"ProductPreset[{preset.name}].buffer_length",
                    preset.buffer_length,
                    self._source("product_preset", preset, "buffer_length"),
                ),
                _LengthCandidate("Racking pitch", pitch, product.source_pitch),
            ),
        )
        external_source = self._negative_operand_source(
            [
                (
                    cluster_length,
                    self._source("node", node, "cluster_length"),
                ),
                (
                    preset.buffer_length,
                    self._source("product_preset", preset, "buffer_length"),
                ),
                (pitch, product.source_pitch),
            ],
            self._source("product_preset", preset, "buffer_length"),
        )
        for polarity in ("POS", "NEG"):
            self._add_segment(
                ctx, product, polarity, "LYNX External Mainline", cluster_wire,
                external_length, total_current, length_source=external_source,
            )

        for index in range(1, len(allocations)):
            previous_slot = allocations[index - 1][0]
            current_slot = allocations[index][0]
            length = (
                preset.nearby_overmold_gap
                if previous_slot == current_slot
                else self._length_op(
                    lambda: pitch * (current_slot - previous_slot),
                    (_LengthCandidate("Racking pitch", pitch, product.source_pitch),),
                )
            )
            passing_current = sum(allocation_currents[index:])
            internal_source = (
                self._source("product_preset", preset, "nearby_overmold_gap")
                if previous_slot == current_slot
                else product.source_pitch
            )
            for polarity in ("POS", "NEG"):
                self._add_segment(
                    ctx, product, polarity, "LYNX Internal Mainline", cluster_wire,
                    length, passing_current, length_source=internal_source,
                )

        ctx.system_voltage = lynx_panel.system_voltage
        ctx.string_current = lynx_panel.imp
        self._finalize_product(ctx, product)
        return product

    def _add_segment(
        self,
        ctx: _Context,
        product: ProductVoltageDrop,
        polarity: str,
        role: str,
        wire: Wire,
        length: Length,
        current: float,
        length_fields: tuple[str, ...] = (),
        length_source: VoltageDropIssueSource | None = None,
    ) -> None:
        if ctx.cancel_event is not None and ctx.cancel_event.is_set():
            raise VoltageCalculationCancelled("Voltage-drop calculation cancelled.")
        resolved_length_source = length_source or self._source(
            "product_preset",
            product.source_preset,
            *(length_fields or self._default_length_fields(role, polarity)),
        )
        try:
            length_ft = length.to_ft()
        except LengthError as exc:
            issue = self._length_issue(
                exc,
                product.path,
                field=resolved_length_source.primary_field,
                expression=length.length_str,
                source=resolved_length_source,
            )
            self._add_existing_issue(ctx, product, issue)
            return
        if length_ft < -1e-9:
            self._product_issue(
                ctx,
                product,
                "negative_length",
                f"{role} has a negative length ({length_ft:.4f} ft).",
                params={"role": role, "length": f"{length_ft:.4f}"},
                source=resolved_length_source,
            )
            return
        if abs(length_ft) <= 1e-9 or abs(current) <= 1e-12:
            return
        if wire.wiresize == "none":
            self._product_issue(
                ctx,
                product,
                "missing_wire",
                f"{role} has no wire size configured.",
                params={"role": role},
                source=self._source(
                    "product_preset",
                    product.source_preset,
                    self._wire_field(product.source_preset, wire),
                ),
            )
            return
        size = wire.to_awg()
        resistance = get_resistance_ohms_per_kft(size, wire.material)
        voltage = None if resistance is None else current * (resistance / 1000.0) * length_ft
        segment = LineSegmentDrop(
            instance_id=product.instance_id,
            product_type=product.product_type,
            polarity=polarity,
            role=role,
            wire_size=size,
            material=wire.material,
            length_ft=length_ft,
            current_a=current,
            ohms_per_kft=resistance,
            voltage_drop=voltage,
            path=product.path,
        )
        ctx.segments.append(segment)
        if resistance is None:
            self._product_issue(
                ctx,
                product,
                "missing_resistance",
                f"Missing resistance for {size} {wire.material}.",
                params={"size": size, "material": wire.material},
                source=self._source(
                    "product_preset",
                    product.source_preset,
                    self._wire_field(product.source_preset, wire),
                ),
            )

    def _finalize_product(self, ctx: _Context, product: ProductVoltageDrop) -> None:
        own_segments = [segment for segment in ctx.segments if segment.instance_id == product.instance_id]
        if product.issues or any(segment.voltage_drop is None for segment in own_segments):
            return
        product.positive_drop = sum(
            segment.voltage_drop or 0.0 for segment in own_segments if segment.polarity == "POS"
        )
        product.negative_drop = sum(
            segment.voltage_drop or 0.0 for segment in own_segments if segment.polarity == "NEG"
        )
        # DC system voltage is reduced by both the positive outgoing conductor
        # and the negative return conductor, so the loop drop is their sum.
        product.voltage_drop = product.positive_drop + product.negative_drop
        product.voltage_drop_percent = (
            product.voltage_drop / ctx.system_voltage if ctx.system_voltage > 0 else None
        )
        branch_roles = {"Branch", "Terminal Branch", "LYNX Branch"}
        product.branch_drop = sum(
            segment.voltage_drop or 0.0
            for segment in own_segments
            if segment.role.startswith("Branch") or segment.role in branch_roles
        )
        product.mainline_drop = product.voltage_drop - product.branch_drop

    def _product_issue(
        self,
        ctx: _Context,
        product: ProductVoltageDrop,
        code: str,
        message: str,
        params: dict[str, object] | None = None,
        source: VoltageDropIssueSource | None = None,
    ) -> None:
        issue = VoltageDropIssue(
            code,
            message,
            product.path,
            params or {},
            source or self._default_product_source(product, code),
        )
        if not any(existing.fingerprint == issue.fingerprint for existing in product.issues):
            product.issues.append(issue)
        if not any(existing.fingerprint == issue.fingerprint for existing in ctx.issues):
            ctx.issues.append(issue)

    @staticmethod
    def _append_issue(issues: list[VoltageDropIssue], issue: VoltageDropIssue) -> None:
        if not any(existing.fingerprint == issue.fingerprint for existing in issues):
            issues.append(issue)

    @staticmethod
    def _source(
        object_kind: str,
        object_ref: object,
        *field_keys: str,
        primary_field: str = "",
    ) -> VoltageDropIssueSource:
        fields = tuple(field for field in field_keys if field)
        return VoltageDropIssueSource(
            object_kind,
            object_ref,
            fields,
            primary_field or (fields[0] if fields else ""),
        )

    @classmethod
    def _pitch_source(cls, project: Project, inv: Inv, node: Node) -> VoltageDropIssueSource:
        if node.racking_pitch is not None:
            return cls._source("node", node, "racking_pitch")
        if inv.racking_pitch is not None:
            return cls._source("inv", inv, "racking_pitch")
        if project.racking_pitch is not None:
            return cls._source("project", project, "racking_pitch")
        return cls._source("node", node, "racking_pitch")

    @classmethod
    def _lynx_preset_source(cls, project: Project, inv: Inv, node: Node) -> VoltageDropIssueSource:
        if node.lynx_preset is not None:
            return cls._source("node", node, "lynx_preset")
        if inv.lynx_preset is not None:
            return cls._source("inv", inv, "lynx_preset")
        if project.lynx_preset is not None:
            return cls._source("project", project, "lynx_preset")
        return cls._source("node", node, "lynx_preset")

    @staticmethod
    def _wire_field(preset: ProductPreset | None, wire: Wire) -> str:
        if preset is not None:
            for field_name in (
                "branch_cu_wire", "branch_al_wire",
                "last_branch_cu_wire", "last_branch_al_wire",
                "external_cu_wire",
                "external_al_wire", "internal_cu_wire", "internal_al_wire",
                "cluster_wire",
            ):
                if getattr(preset, field_name, None) is wire:
                    return field_name
        return "__object__"

    @classmethod
    def _negative_operand_source(
        cls,
        candidates: Iterable[tuple[Length, VoltageDropIssueSource]],
        fallback: VoltageDropIssueSource,
    ) -> VoltageDropIssueSource:
        negative: list[tuple[float, VoltageDropIssueSource]] = []
        for length, source in candidates:
            try:
                value = length.to_mm()
            except LengthError:
                continue
            if value < 0:
                negative.append((value, source))
        return min(negative, key=lambda item: item[0])[1] if negative else fallback

    @classmethod
    def _product_layout_candidates(
        cls,
        product: ProductVoltageDrop,
    ) -> list[tuple[Length, VoltageDropIssueSource]]:
        result: list[tuple[Length, VoltageDropIssueSource]] = []
        row = product.source_row
        entry_index = product.source_entry_index
        if row is not None and 0 <= entry_index < len(row.racking_entries):
            result.append(
                (
                    row.racking_entries[entry_index].offset,
                    cls._source("row", row, f"racking_entries[{entry_index}].offset"),
                )
            )
        pitch_source = product.source_pitch
        if pitch_source is not None:
            pitch = getattr(pitch_source.object_ref, "racking_pitch", None)
            if isinstance(pitch, Length):
                result.append((pitch, pitch_source))
        return result

    @classmethod
    def _lynx_whip_source(
        cls,
        node: Node,
        row_position: int,
        whip: Length,
    ) -> VoltageDropIssueSource:
        if 1 <= row_position <= len(node.rows):
            row = node.rows[row_position - 1]
            for entry_index, entry in enumerate(row.racking_entries):
                if entry.offset is whip:
                    return cls._source(
                        "row", row, f"racking_entries[{entry_index}].offset"
                    )
        return cls._source("node", node, "__object__")

    @classmethod
    def _preset_negative_source(
        cls,
        product: ProductVoltageDrop,
        preset: ProductPreset,
        fields: tuple[str, ...],
        include_layout: bool = False,
    ) -> VoltageDropIssueSource:
        fallback = cls._source("product_preset", preset, *fields)
        candidates = cls._product_layout_candidates(product) if include_layout else []
        candidates.extend(
            (value, cls._source("product_preset", preset, field_name))
            for field_name in fields
            if isinstance((value := getattr(preset, field_name, None)), Length)
        )
        return cls._negative_operand_source(candidates, fallback)

    @staticmethod
    def _default_length_fields(role: str, polarity: str) -> tuple[str, ...]:
        suffix = "positive" if polarity == "POS" else "negative"
        if role == "Branch":
            return (f"branch_length_{suffix}",)
        if role == "Terminal Branch":
            return ("branch_lines", f"branch_length_{suffix}")
        if role.startswith("Internal Mainline"):
            return ("branch_lines", "internal_first_cual_prefix", "internal_second_cual_suffix")
        if role.startswith("External"):
            return ("external_cu_length_to_row_head", "external_second_cual_suffix")
        if role.startswith("Homerun"):
            return ("branch_lines", "branch_second_cual_suffix")
        if role == "LYNX Branch":
            return ("branch_length_cu", "branch_length_al", "nearby_overmold_gap")
        if role.startswith("LYNX"):
            return ("buffer_length", "nearby_overmold_gap")
        return ("__object__",)

    @classmethod
    def _default_product_source(
        cls,
        product: ProductVoltageDrop,
        code: str,
    ) -> VoltageDropIssueSource | None:
        preset = product.source_preset
        if preset is None:
            return None
        fields = {
            "missing_product_code": ("__object__",),
            "invalid_ibex_branches": ("branch_count", "branch_lines"),
            "branch_count_mismatch": ("branch_count", "branch_lines"),
            "missing_branch_coordinate": ("branch_lines",),
            "missing_pv_panel": ("pv_panel",),
        }.get(code, ("__object__",))
        return cls._source("product_preset", preset, *fields)

    @classmethod
    def _lynx_layout_source(cls, project: Project, node: Node) -> VoltageDropIssueSource:
        for row in node.rows:
            counts: list[tuple[int, object, int]] = []
            for index, entry in enumerate(row.racking_entries):
                rackings = project.get_rackings(entry.rackings)
                count = len(_resolve_product_presets(rackings, project)) if rackings else 0
                counts.append((index, rackings, count))
            if sum(count for _, _, count in counts) <= 8:
                continue
            oversized = next(
                ((rackings, count) for _, rackings, count in counts if count > 8 and rackings),
                None,
            )
            if oversized is not None:
                return cls._source("rackings", oversized[0], "product_presets")
            fields = tuple(
                f"racking_entries[{index}].rackings"
                for index, _rackings, count in counts
                if count
            )
            return cls._source("row", row, *fields)
        return cls._source("node", node, "__object__")

    @staticmethod
    def _resolve_panel(project: Project, preset: ProductPreset) -> PVPanelInfo | None:
        if preset.pv_panel.strip():
            return project.get_pv_panel(preset.pv_panel)
        return None

    def _configure_product_panel(
        self,
        ctx: _Context,
        project: Project,
        preset: ProductPreset,
        product: ProductVoltageDrop,
    ) -> PVPanelInfo | None:
        panel = self._resolve_panel(project, preset)
        if panel is None:
            name = preset.pv_panel.strip()
            if name:
                self._product_issue(
                    ctx,
                    product,
                    "missing_pv_panel",
                    f"PV panel '{name}' was not found.",
                    params={"name": name},
                )
            else:
                self._product_issue(
                    ctx,
                    product,
                    "missing_pv_panel",
                    "A PV panel must be selected in the product preset.",
                    params={"selection_required": True},
                )
            return None
        product.pv_panel_name = panel.name or preset.pv_panel
        product.system_voltage = panel.system_voltage
        product.string_current = panel.imp
        if not self._validate_panel(ctx, product, panel, product.pv_panel_name):
            return None
        ctx.system_voltage = panel.system_voltage
        ctx.string_current = panel.imp
        return panel

    def _validate_panel(
        self,
        ctx: _Context,
        product: ProductVoltageDrop,
        panel: PVPanelInfo,
        name: str,
    ) -> bool:
        valid = True
        label = name or "selected PV panel"
        for code, message, ok in (
            ("invalid_vmp", f"Vmp for {label} must be greater than 0.", panel.vmp > 0),
            ("invalid_imp", f"Imp for {label} must be greater than 0.", panel.imp > 0),
            (
                "invalid_module_count",
                f"Modules per string for {label} must be greater than 0.",
                panel.module_per_string > 0,
            ),
        ):
            if not ok:
                valid = False
                self._product_issue(
                    ctx,
                    product,
                    code,
                    message,
                    params={"name": label},
                    source=self._source(
                        "pv_panel",
                        panel,
                        {
                            "invalid_vmp": "vmp",
                            "invalid_imp": "imp",
                            "invalid_module_count": "module_per_string",
                        }[code],
                    ),
                )
        return valid

    @classmethod
    def _preset_length_candidates(cls, preset: ProductPreset) -> list[_LengthCandidate]:
        candidates = [
            _LengthCandidate(
                f"ProductPreset[{preset.name}].{field_name}",
                getattr(preset, field_name),
                cls._source("product_preset", preset, field_name),
            )
            for field_name in get_length_field_names(preset)
            if isinstance(getattr(preset, field_name), Length)
        ]
        for branch_position, branch in enumerate(preset.branch_lines):
            candidates.extend(
                (
                    _LengthCandidate(
                        f"ProductPreset[{preset.name}].branch_lines[{branch_position}].coordinate_positive",
                        branch.coordinate_positive,
                        cls._source(
                            "product_preset",
                            preset,
                            f"branch_lines[{branch_position}].coordinate_positive",
                        ),
                    ),
                    _LengthCandidate(
                        f"ProductPreset[{preset.name}].branch_lines[{branch_position}].coordinate_negative",
                        branch.coordinate_negative,
                        cls._source(
                            "product_preset",
                            preset,
                            f"branch_lines[{branch_position}].coordinate_negative",
                        ),
                    ),
                )
            )
        return candidates

    @staticmethod
    def _length_op(
        operation: Callable[[], _T],
        candidates: Iterable[_LengthCandidate],
    ) -> _T:
        candidate_list = tuple(candidates)
        try:
            return operation()
        except LengthError as exc:
            raise _SourcedLengthError(exc, candidate_list) from exc

    @staticmethod
    def _same_length_error(left: LengthError, right: LengthError) -> bool:
        return left.code == right.code and left.params == right.params

    @classmethod
    def _diagnose_length_issue(
        cls,
        exc: LengthError,
        path: str,
        candidates: Iterable[_LengthCandidate],
    ) -> VoltageDropIssue:
        """Attach the exact model field and expression that reproduces *exc*."""
        candidate_list = list(
            exc.candidates if isinstance(exc, _SourcedLengthError) else candidates
        )
        identifier_candidates: list[_LengthCandidate] = []
        seen_identifiers: set[int] = set()

        def add_dependencies(length: Length) -> None:
            by_name = {identifier.name: identifier for identifier in length.identifiers}
            for name in Length.extract_identifier_refs(length.length_str):
                identifier = by_name.get(name)
                if identifier is None or id(identifier) in seen_identifiers:
                    continue
                seen_identifiers.add(id(identifier))
                add_dependencies(identifier.length)
                identifier_candidates.append(
                    _LengthCandidate(
                        f"LengthIdentifier[{identifier.name}].length",
                        identifier.length,
                        cls._source("length_identifier", identifier, "length"),
                    )
                )

        for candidate in candidate_list:
            add_dependencies(candidate.length)
        # Prefer the deepest editable definition over a parent expression
        # when a known identifier contains the actual malformed dependency.
        for candidate in identifier_candidates + candidate_list:
            try:
                candidate.length.to_mm()
            except LengthError as candidate_exc:
                if cls._same_length_error(exc, candidate_exc):
                    return cls._length_issue(
                        exc,
                        path,
                        field=candidate.label,
                        expression=candidate.length.length_str,
                        source=candidate.source,
                    )
        return cls._length_issue(
            exc,
            path,
            source=candidate_list[0].source if candidate_list else None,
        )

    @staticmethod
    def _length_issue(
        exc: LengthError,
        path: str,
        field: str = "",
        expression: str = "",
        source: VoltageDropIssueSource | None = None,
    ) -> VoltageDropIssue:
        if exc.code == "unknown_identifier":
            detail = str(exc.params.get("name", ""))
            message = f"Unknown length identifier: {detail}."
        else:
            detail = exc.code
            message = f"Invalid or unresolved length ({detail})."
        params: dict[str, object] = {"detail": detail}
        if field:
            params["field"] = field
        if expression:
            params["expression"] = expression
        return VoltageDropIssue(
            "invalid_length_expression",
            message,
            path,
            params,
            source,
        )

    @staticmethod
    def _add_existing_issue(
        ctx: _Context,
        product: ProductVoltageDrop,
        issue: VoltageDropIssue,
    ) -> None:
        if not any(existing.fingerprint == issue.fingerprint for existing in product.issues):
            product.issues.append(issue)
        if not any(existing.fingerprint == issue.fingerprint for existing in ctx.issues):
            ctx.issues.append(issue)

    def _build_product_average_level(
        self,
        level: str,
        name: str,
        path: str,
        products: list[ProductVoltageDrop],
        source_object: object | None = None,
        extra_issues: list[VoltageDropIssue] | None = None,
    ) -> LevelVoltageDrop:
        issues = list(extra_issues or [])
        issues.extend(issue for product in products for issue in product.issues)
        valid_values = [product.voltage_drop for product in products if product.voltage_drop is not None]
        valid_percents = [
            product.voltage_drop_percent
            for product in products
            if product.voltage_drop_percent is not None
        ]
        value = _mean(value for value in valid_values if value is not None)
        percent = _mean(value for value in valid_percents if value is not None)
        if not products:
            issues.append(
                VoltageDropIssue(
                    "no_products",
                    "No products are available.",
                    path,
                    source=(
                        self._source("row", source_object, "racking_entries")
                        if source_object is not None
                        else None
                    ),
                )
            )
            value = None
            percent = None
        elif len(valid_values) != len(products):
            value = None
        if len(valid_percents) != len(products):
            percent = None
        return self._make_level(
            level,
            name,
            path,
            value,
            product_count=len(products),
            issues=issues,
            percent_value=percent,
        )

    @staticmethod
    def _make_level(
        level: str,
        name: str,
        path: str,
        value: float | None,
        product_count: int = 0,
        child_count: int = 0,
        issues: list[VoltageDropIssue] | None = None,
        percent_value: float | None = None,
    ) -> LevelVoltageDrop:
        issue_list = VoltageDropService._deduplicate_issues(issues or [])
        return LevelVoltageDrop(
            level=level,
            name=name,
            path=path,
            voltage_drop=value,
            voltage_drop_percent=percent_value,
            product_count=product_count,
            child_count=child_count,
            issues=issue_list,
        )

    @staticmethod
    def _valid_level_mean(levels: list[LevelVoltageDrop]) -> float | None:
        if not levels or any(level.voltage_drop is None for level in levels):
            return None
        return _mean(level.voltage_drop for level in levels if level.voltage_drop is not None)

    @staticmethod
    def _valid_level_percent_mean(levels: list[LevelVoltageDrop]) -> float | None:
        if not levels or any(level.voltage_drop_percent is None for level in levels):
            return None
        return _mean(
            level.voltage_drop_percent
            for level in levels
            if level.voltage_drop_percent is not None
        )

    @staticmethod
    def _deduplicate_issues(issues: list[VoltageDropIssue]) -> list[VoltageDropIssue]:
        seen: set[tuple[object, ...]] = set()
        result: list[VoltageDropIssue] = []
        for issue in issues:
            key = (issue.path, issue.message, *issue.fingerprint)
            if key not in seen:
                seen.add(key)
                result.append(issue)
        return sorted(
            result,
            key=lambda issue: VoltageDropService._ISSUE_PRIORITY.get(issue.code, 20),
        )

    @staticmethod
    def _attach_product_codes(
        products: list[ProductVoltageDrop],
        generation_result: ProductGenerationResult,
    ) -> None:
        code_queues: dict[tuple[str, str, str, str, str, str, str], deque[str]] = defaultdict(deque)
        for generated in generation_result.all_products:
            if generated.product_type not in {"IBEX", "Homerun_Extender", "LYNX"}:
                continue
            key = (
                generated.product_type,
                generated.inv_name,
                generated.node_name,
                generated.row_name,
                generated.rackings_name,
                generated.preset_name,
                generated.polarity,
            )
            code_queues[key].append(generated.code)

        for product in products:
            for polarity, attr in (("POS", "positive_code"), ("NEG", "negative_code")):
                key = (
                    product.product_type,
                    product.inv_name,
                    product.node_name,
                    product.row_name,
                    product.rackings_name,
                    product.preset_name,
                    polarity,
                )
                if code_queues[key]:
                    setattr(product, attr, code_queues[key].popleft())

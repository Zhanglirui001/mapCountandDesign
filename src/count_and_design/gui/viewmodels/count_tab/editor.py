"""Pure state construction for the Count tab property editor."""

from __future__ import annotations

from typing import cast

from ....application.catalog import ProductPresetService
from ....application.project import ProjectStatsService, StatisticsOverview
from ....application.voltage_drop import format_voltage_drop_issue
from ....application.voltage_drop.models import LevelVoltageDrop, VoltageDropReport
from ....i18n import tr
from ....models import Inv, Node, Project, Row
from ..states import (
    BreakdownState,
    EditorState,
    MetricState,
    ReadOnlyResultsState,
    TableSectionState,
)


class CountTabEditorBuilder:
    """Map domain objects and calculated results to immutable view state."""

    def __init__(
        self,
        stats_service: ProjectStatsService,
        product_preset_service: ProductPresetService,
    ) -> None:
        self._stats_service = stats_service
        self._product_preset_service = product_preset_service

    def build(
        self,
        project: Project,
        current: tuple[str, object] | None,
        voltage_report: VoltageDropReport | None,
    ) -> EditorState | None:
        if current is None:
            return None
        item_type, item = current
        identifier_options = tuple(
            (identifier.name, identifier.length.length_str)
            for identifier in project.length_identifiers
        )
        rackings_options = tuple(
            rackings.name for rackings in project.rackings_list
        )
        lynx_options = tuple(
            preset.name
            for preset in self._product_preset_service.get_lynx_presets(project)
        )

        if item_type == "project":
            detail = TableSectionState(
                title=tr("count_tab.section.inv_details", "INV Details"),
                headers=(
                    tr("count_tab.header.inv", "INV"),
                    tr("count_tab.header.rackings_count", "Rackings Count"),
                    tr("count_tab.header.pitch", "Pitch"),
                    tr("count_tab.header.lynx_preset", "LYNX Preset"),
                    tr(
                        "count_tab.header.product_presets",
                        "ProductPresets",
                    ),
                    tr("voltage_drop.strings", "Strings"),
                    tr("voltage_drop.volts", "Voltage Drop (V)"),
                    tr("voltage_drop.percent", "Voltage Drop (%)"),
                ),
                rows=tuple(
                    self._stats_service.build_project_detail_rows(
                        project,
                        voltage_report,
                    )
                ),
            )
            return EditorState(
                kind="project",
                title=tr(
                    "count_tab.editor.project_properties",
                    "Project Properties",
                ),
                name=project.name,
                address=project.address,
                client_name=project.client_name,
                project_type=project.project_type,
                racking_pitch=self.format_length(project.racking_pitch),
                lynx_preset_name=project.lynx_preset or "",
                lynx_preset_options=lynx_options,
                identifier_options=identifier_options,
                results=self._build_results(
                    self._stats_service.build_project_overview(project),
                    (
                        voltage_report.for_object(project)
                        if voltage_report is not None
                        else None
                    ),
                    detail,
                    voltage_calculated=voltage_report is not None,
                ),
            )
        if item_type == "inv":
            inv = cast(Inv, item)
            detail = TableSectionState(
                title=tr("count_tab.section.node_details", "Node Details"),
                headers=(
                    tr("count_tab.header.node", "Node"),
                    tr("count_tab.header.rackings", "Rackings"),
                    tr("count_tab.header.pitch", "Pitch"),
                    tr("count_tab.header.lynx_preset", "LYNX Preset"),
                    tr("count_tab.header.cluster_length", "Cluster Length"),
                    tr("count_tab.header.box_position", "Box Position"),
                    tr(
                        "count_tab.header.product_presets",
                        "ProductPresets",
                    ),
                    tr("voltage_drop.strings", "Strings"),
                    tr("voltage_drop.volts", "Voltage Drop (V)"),
                    tr("voltage_drop.percent", "Voltage Drop (%)"),
                ),
                rows=tuple(
                    self._stats_service.build_inv_detail_rows(
                        project,
                        inv,
                        voltage_report,
                    )
                ),
            )
            return EditorState(
                kind="inv",
                title=tr(
                    "count_tab.editor.inv_properties",
                    "INV Properties",
                ),
                name=inv.name,
                racking_pitch=self.format_length(inv.racking_pitch),
                lynx_preset_name=inv.lynx_preset or "",
                lynx_preset_options=lynx_options,
                identifier_options=identifier_options,
                results=self._build_results(
                    self._stats_service.build_inv_overview(project, inv),
                    (
                        voltage_report.for_object(inv)
                        if voltage_report is not None
                        else None
                    ),
                    detail,
                    voltage_calculated=voltage_report is not None,
                ),
            )
        if item_type == "node":
            node = cast(Node, item)
            headers, rows = self._stats_service.build_node_detail_rows(
                project,
                node,
                voltage_report,
            )
            return EditorState(
                kind="node",
                title=tr(
                    "count_tab.editor.node_properties",
                    "Node Properties",
                ),
                name=node.name,
                racking_pitch=self.format_length(node.racking_pitch),
                lynx_preset_name=node.lynx_preset or "",
                lynx_preset_options=lynx_options,
                cluster_length=self.format_length(node.cluster_length),
                box_position=node.box_position,
                identifier_options=identifier_options,
                results=self._build_results(
                    self._stats_service.build_node_overview(project, node),
                    (
                        voltage_report.for_object(node)
                        if voltage_report is not None
                        else None
                    ),
                    TableSectionState(
                        title=tr(
                            "count_tab.section.row_details",
                            "Row Details",
                        ),
                        headers=headers,
                        rows=rows,
                    ),
                    voltage_calculated=voltage_report is not None,
                ),
            )
        if item_type == "row":
            row = cast(Row, item)
            row_name = row.name or tr(
                "count_tab.tree.default_row",
                "Row {index}",
            ).format(index=1)
            entries = tuple(
                (entry.rackings, self.format_length(entry.offset))
                for entry in row.racking_entries
            )
            return EditorState(
                kind="row",
                title=tr(
                    "count_tab.editor.row_properties",
                    "{name} Properties",
                ).format(name=row_name),
                name=row.name,
                racking_entries=entries,
                rackings_options=rackings_options,
                identifier_options=identifier_options,
                results=ReadOnlyResultsState(
                    voltage=self._build_voltage_metrics(
                        (
                            voltage_report.for_object(row)
                            if voltage_report is not None
                            else None
                        ),
                        calculated=voltage_report is not None,
                    ),
                    detail=self._build_product_voltage_section(
                        voltage_report,
                        row,
                    ),
                ),
            )
        return None

    @staticmethod
    def format_length(length: object | None) -> str:
        return getattr(length, "length_str", "") if length is not None else ""

    @staticmethod
    def _build_voltage_metrics(
        level: LevelVoltageDrop | None,
        *,
        calculated: bool = True,
    ) -> tuple[MetricState, ...]:
        if level is None or level.voltage_drop is None:
            drop = percent = "N/A"
            status = (
                format_voltage_drop_issue(level.issues[0])
                if level and level.issues
                else tr("voltage_drop.not_available", "Not available")
                if calculated
                else tr("voltage_drop.not_calculated", "Not calculated")
            )
            tone = "warning"
        else:
            drop = f"{level.voltage_drop:.3f}"
            percent = f"{level.voltage_drop_percent:.2%}"
            status = tr("voltage_drop.valid", "Valid")
            tone = "success"
        return (
            MetricState(tr("voltage_drop.volts", "Voltage Drop (V)"), drop),
            MetricState(
                tr("voltage_drop.percent", "Voltage Drop (%)"),
                percent,
            ),
            MetricState(
                tr("voltage_drop.status", "Calculation Status"),
                status,
                tone,
                level.issues[0] if level and level.issues else None,
            ),
        )

    def _build_results(
        self,
        overview: StatisticsOverview,
        level: LevelVoltageDrop | None,
        detail: TableSectionState,
        *,
        voltage_calculated: bool,
    ) -> ReadOnlyResultsState:
        return ReadOnlyResultsState(
            statistics=tuple(
                MetricState(label, value) for label, value in overview.metrics
            ),
            voltage=self._build_voltage_metrics(
                level,
                calculated=voltage_calculated,
            ),
            breakdowns=(
                BreakdownState(
                    tr("stats.rackings_count", "Rackings Count"),
                    overview.rackings,
                ),
                BreakdownState(
                    tr(
                        "stats.product_presets_count",
                        "ProductPresets Count",
                    ),
                    overview.product_presets,
                ),
                BreakdownState(
                    tr("stats.pv_panels_count", "PV Panels Count"),
                    overview.pv_panels,
                ),
                BreakdownState(
                    tr("stats.cluster_total_length", "Cluster Total Length"),
                    (
                        (
                            tr("stats.total", "Total"),
                            overview.cluster_total_length,
                        ),
                    )
                    if overview.cluster_total_length is not None
                    else (),
                ),
            ),
            detail=detail,
        )

    @staticmethod
    def _build_product_voltage_section(
        report: VoltageDropReport | None,
        row: Row,
    ) -> TableSectionState:
        rows = tuple(
            (
                product.product_type,
                product.preset_name,
                str(product.string_count),
                (
                    f"{product.positive_drop:.3f}"
                    if product.positive_drop is not None
                    else "N/A"
                ),
                (
                    f"{product.negative_drop:.3f}"
                    if product.negative_drop is not None
                    else "N/A"
                ),
                (
                    f"{product.voltage_drop:.3f}"
                    if product.voltage_drop is not None
                    else "N/A"
                ),
                (
                    f"{product.voltage_drop_percent:.2%}"
                    if product.voltage_drop_percent is not None
                    else "N/A"
                ),
            )
            for product in (
                report.products_for(row) if report is not None else []
            )
        )
        return TableSectionState(
            title=tr("voltage_drop.products", "Product Voltage Drop"),
            headers=(
                tr("voltage_drop.product_type", "Type"),
                tr("voltage_drop.preset", "Preset"),
                tr("voltage_drop.strings", "Strings"),
                tr("voltage_drop.positive", "Positive (V)"),
                tr("voltage_drop.negative", "Negative (V)"),
                tr("voltage_drop.average", "Average (V)"),
                tr("voltage_drop.percent", "Voltage Drop (%)"),
            ),
            rows=rows,
        )

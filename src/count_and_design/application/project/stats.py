"""Statistics and detail-table builders."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ...i18n import tr
from ...models import Inv, Node, Project
from ..voltage_drop.models import VoltageDropReport


@dataclass(frozen=True)
class StatisticsOverview:
    """Statistics grouped by their display meaning rather than table rows."""

    metrics: tuple[tuple[str, str], ...]
    rackings: tuple[tuple[str, str], ...]
    product_presets: tuple[tuple[str, str], ...]
    pv_panels: tuple[tuple[str, str], ...]
    cluster_total_length: str | None = None


class ProjectStatsService:
    """Builds statistics and detail table data for the UI."""

    @staticmethod
    def _format_length(length: object | None) -> str:
        return getattr(length, "length_str", "") if length is not None else ""

    @staticmethod
    def _format_wire(wire: object | None) -> str:
        return getattr(wire, "wiresize", "") if wire is not None else ""

    @staticmethod
    def _format_lynx_preset(preset_name: str | None, project: Project) -> str:
        if preset_name is None:
            return ""
        # 查找预设获取 cluster_wire 信息
        for pp in project.product_presets:
            if pp.name == preset_name and pp.product_type == "LYNX":
                return getattr(pp.cluster_wire, "wiresize", preset_name) or preset_name
        return preset_name

    def _count_rackings_and_product_presets(
        self,
        project: Project,
        rows: list[object],
    ) -> tuple[Counter[str], Counter[str]]:
        rackings_counter: Counter[str] = Counter()
        product_presets_counter: Counter[str] = Counter()
        for row in rows:
            for entry in row.racking_entries:
                name = entry.rackings
                if not name:
                    continue
                rackings_counter[name] += 1
                rackings = project.get_rackings(name)
                if rackings is not None:
                    for product in rackings.product_presets:
                        product_presets_counter[product] += 1
        return rackings_counter, product_presets_counter

    def _calculate_string_count(self, project: Project, rows: list[object]) -> int:
        """Calculate the total string count for a set of rows.

        For each row, for each racking entry, look up the Rackings object,
        enumerate its product presets, find each ProductPreset,
        and sum their branch_count values.
        """
        total = 0
        for row in rows:
            for entry in row.racking_entries:
                name = entry.rackings
                if not name:
                    continue
                rackings = project.get_rackings(name)
                if rackings is None:
                    continue
                for product_name in rackings.product_presets:
                    preset = project.get_product_preset(product_name)
                    if preset is not None:
                        total += preset.branch_count
        return total

    @staticmethod
    def _count_pv_panels(project: Project, rows: list[object]) -> Counter[str]:
        """Count installed modules by the PV panel selected on each preset."""
        panel_counter: Counter[str] = Counter()
        for row in rows:
            for entry in row.racking_entries:
                rackings = project.get_rackings(entry.rackings) if entry.rackings else None
                if rackings is None:
                    continue
                for preset_name in rackings.product_presets:
                    preset = project.get_product_preset(preset_name)
                    if preset is None or not preset.pv_panel.strip() or preset.branch_count <= 0:
                        continue
                    panel = project.get_pv_panel(preset.pv_panel)
                    if panel is None or panel.module_per_string <= 0:
                        continue
                    panel_counter[panel.name] += preset.branch_count * panel.module_per_string
        return panel_counter

    @staticmethod
    def _total_cluster_length(nodes: list[Node]) -> str:
        """Return the combined Cluster length in millimetres and feet."""
        total_mm = sum(
            node.cluster_length.to_mm()
            for node in nodes
            if node.cluster_length is not None
        )
        return f"{total_mm / 304.8:.3f}ft ({total_mm:.12g}mm)"

    @staticmethod
    def _overview(
        metrics: list[tuple[str, str]],
        rackings_counter: Counter[str],
        product_presets_counter: Counter[str],
        pv_panel_counter: Counter[str],
        cluster_total_length: str | None = None,
    ) -> StatisticsOverview:
        return StatisticsOverview(
            metrics=tuple(metrics),
            rackings=tuple((name, str(count)) for name, count in sorted(rackings_counter.items())),
            product_presets=tuple(
                (name, str(count)) for name, count in sorted(product_presets_counter.items())
            ),
            pv_panels=tuple(
                (name, str(count)) for name, count in sorted(pv_panel_counter.items())
            ),
            cluster_total_length=cluster_total_length,
        )

    @staticmethod
    def _flatten_overview(overview: StatisticsOverview) -> list[tuple[str, str]]:
        """Keep the legacy table-row API available for non-GUI callers."""
        rows = list(overview.metrics)
        rows.append((tr("stats.rackings_count", "Rackings Count"), ""))
        rows.extend((f"  {name}", count) for name, count in overview.rackings)
        rows.append((tr("stats.product_presets_count", "ProductPresets Count"), ""))
        rows.extend((f"  {name}", count) for name, count in overview.product_presets)
        rows.append((tr("stats.pv_panels_count", "PV Panels Count"), ""))
        rows.extend((f"  {name}", count) for name, count in overview.pv_panels)
        if overview.cluster_total_length is not None:
            rows.append(
                (
                    tr("stats.cluster_total_length", "Cluster Total Length"),
                    overview.cluster_total_length,
                )
            )
        return rows

    def build_project_overview(self, project: Project) -> StatisticsOverview:
        all_nodes = [node for inv in project.invs for node in inv.nodes]
        all_rows = [row for inv in project.invs for node in inv.nodes for row in node.rows]
        rackings_counter, product_presets_counter = self._count_rackings_and_product_presets(
            project, all_rows
        )
        return self._overview(
            [
                (tr("stats.inv_count", "INV Count"), str(len(project.invs))),
                (tr("stats.node_count", "Node Count"), str(sum(len(inv.nodes) for inv in project.invs))),
                (tr("stats.row_count", "Row Count"), str(len(all_rows))),
                (tr("stats.string_count", "String Count"), str(self._calculate_string_count(project, all_rows))),
            ],
            rackings_counter,
            product_presets_counter,
            self._count_pv_panels(project, all_rows),
            self._total_cluster_length(all_nodes),
        )

    def build_inv_overview(self, project: Project, inv: Inv) -> StatisticsOverview:
        all_nodes = list(inv.nodes)
        all_rows = [row for node in inv.nodes for row in node.rows]
        rackings_counter, product_presets_counter = self._count_rackings_and_product_presets(
            project, all_rows
        )
        return self._overview(
            [
                (tr("stats.node_count", "Node Count"), str(len(inv.nodes))),
                (tr("stats.row_count", "Row Count"), str(len(all_rows))),
                (tr("stats.string_count", "String Count"), str(self._calculate_string_count(project, all_rows))),
            ],
            rackings_counter,
            product_presets_counter,
            self._count_pv_panels(project, all_rows),
            self._total_cluster_length(all_nodes),
        )

    def build_node_overview(self, project: Project, node: Node) -> StatisticsOverview:
        all_rows = list(node.rows)
        rackings_counter, product_presets_counter = self._count_rackings_and_product_presets(
            project, all_rows
        )
        return self._overview(
            [
                (tr("stats.row_count", "Row Count"), str(len(all_rows))),
                (tr("stats.string_count", "String Count"), str(self._calculate_string_count(project, all_rows))),
            ],
            rackings_counter,
            product_presets_counter,
            self._count_pv_panels(project, all_rows),
        )

    def build_project_statistics(self, project: Project) -> list[tuple[str, str]]:
        overview = self.build_project_overview(project)
        return self._flatten_overview(overview)

    def build_inv_statistics(self, project: Project, inv: Inv) -> list[tuple[str, str]]:
        overview = self.build_inv_overview(project, inv)
        return self._flatten_overview(overview)

    def build_node_statistics(self, project: Project, node: Node) -> list[tuple[str, str]]:
        overview = self.build_node_overview(project, node)
        return self._flatten_overview(overview)

    @staticmethod
    def _format_drop(report: VoltageDropReport | None, item: object) -> tuple[str, str]:
        level = report.for_object(item) if report is not None else None
        if level is None or level.voltage_drop is None:
            return "N/A", "N/A"
        return f"{level.voltage_drop:.3f}", f"{level.voltage_drop_percent:.2%}"

    def build_project_detail_rows(self, project: Project, report: VoltageDropReport | None = None) -> list[tuple[str, ...]]:
        rows: list[tuple[str, ...]] = []
        for inv in project.invs:
            inv_rows = [row for node in inv.nodes for row in node.rows]
            rackings_counter, product_presets_counter = self._count_rackings_and_product_presets(project, inv_rows)
            pitch = self._format_length(inv.racking_pitch or project.racking_pitch)
            wire = self._format_lynx_preset(inv.lynx_preset or project.lynx_preset, project)
            drop, percent = self._format_drop(report, inv)
            rows.append(
                (
                    inv.name,
                    ", ".join(f"{name}({count})" for name, count in sorted(rackings_counter.items())),
                    pitch,
                    wire,
                    ", ".join(f"{name}({count})" for name, count in sorted(product_presets_counter.items())),
                    str(self._calculate_string_count(project, inv_rows)),
                    drop,
                    percent,
                )
            )
        return rows

    def build_inv_detail_rows(self, project: Project, inv: Inv, report: VoltageDropReport | None = None) -> list[tuple[str, ...]]:
        rows: list[tuple[str, ...]] = []
        for node in inv.nodes:
            rackings_counter, product_presets_counter = self._count_rackings_and_product_presets(project, list(node.rows))
            pitch = self._format_length(node.racking_pitch or inv.racking_pitch)
            wire = self._format_lynx_preset(node.lynx_preset or inv.lynx_preset, project)
            cluster_length = self._format_length(node.cluster_length)
            drop, percent = self._format_drop(report, node)
            rows.append(
                (
                    node.name,
                    ", ".join(f"{name}({count})" for name, count in sorted(rackings_counter.items())),
                    pitch,
                    wire,
                    cluster_length,
                    str(node.box_position),
                    ", ".join(f"{name}({count})" for name, count in sorted(product_presets_counter.items())),
                    str(self._calculate_string_count(project, list(node.rows))),
                    drop,
                    percent,
                )
            )
        return rows

    def build_node_detail_rows(
        self, project: Project, node: Node, report: VoltageDropReport | None = None
    ) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
        # Determine max number of racking entries across rows for consistent headers
        max_entries = 0
        for row in node.rows:
            max_entries = max(max_entries, len(row.racking_entries))
        if max_entries == 0:
            max_entries = 1  # at least one column

        headers: list[str] = [tr("stats.row", "Row")]
        for i in range(max_entries):
            headers.append(
                tr("stats.rackings_entry", "Rackings {n}").format(n=i + 1)
            )
        headers.append(tr("stats.product_presets", "ProductPresets"))
        headers.append(tr("voltage_drop.strings", "Strings"))
        headers.extend((tr("voltage_drop.volts", "Voltage Drop (V)"), tr("voltage_drop.percent", "Voltage Drop (%)")))

        data_rows: list[tuple[str, ...]] = []
        for index, row in enumerate(node.rows):
            racking_names: list[str] = []
            product_presets: list[str] = []
            for entry in row.racking_entries:
                name = (
                    f"{entry.rackings}({self._format_length(entry.offset)})"
                    if entry.rackings
                    else "-"
                )
                racking_names.append(name)
                if entry.rackings:
                    rackings = project.get_rackings(entry.rackings)
                    if rackings is not None:
                        product_presets.extend(list(rackings.product_presets))
            # Pad racking names to max_entries
            while len(racking_names) < max_entries:
                racking_names.append("-")
            row_data: list[str] = [str(index + 1)]
            row_data.extend(racking_names)
            row_data.append(" ".join(product_presets))
            row_data.append(str(self._calculate_string_count(project, [row])))
            drop, percent = self._format_drop(report, row)
            row_data.extend((drop, percent))
            data_rows.append(tuple(row_data))
        return tuple(headers), tuple(data_rows)

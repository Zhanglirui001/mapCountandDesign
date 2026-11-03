from __future__ import annotations

from collections.abc import Callable
from collections import defaultdict
from datetime import datetime
import os
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from ...models import Project
from .models import VoltageDropReport


_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
_HEADER_FONT = Font(color="FFFFFF", bold=True)
_TITLE_FILL = PatternFill("solid", fgColor="D9EAF7")
_ROW_BORDER = Border(bottom=Side(style="thin", color="D9E2F3"))


def _style_table(sheet, header_row: int, widths: list[float]) -> None:
    for cell in sheet[header_row]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    sheet.freeze_panes = f"A{header_row + 1}"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    for row in sheet.iter_rows(min_row=header_row + 1):
        for cell in row:
            cell.border = _ROW_BORDER
            cell.alignment = Alignment(vertical="center")
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width


def _apply_number_formats(sheet, columns: dict[int, str], start_row: int = 2) -> None:
    for column, number_format in columns.items():
        for row in range(start_row, sheet.max_row + 1):
            sheet.cell(row, column).number_format = number_format


def export_voltage_drop_to_xlsx(
    report: VoltageDropReport,
    project: Project,
    directory: str,
    progress_callback: Callable[[int, int], None] | None = None,
) -> str:
    """Export a validated voltage-drop report to a five-sheet workbook.

    ``progress_callback`` is called after each worksheet is fully populated and
    styled, with ``(completed_sheets, 5)``.
    """
    report.require_exportable()
    os.makedirs(directory, exist_ok=True)

    total_sheets = 5
    completed_sheets = 0

    def report_progress() -> None:
        nonlocal completed_sheets
        completed_sheets += 1
        if progress_callback is not None:
            progress_callback(completed_sheets, total_sheets)

    workbook = Workbook()
    overview = workbook.active
    overview.title = "Overview"
    overview.sheet_view.showGridLines = False
    overview["A1"] = "Voltage Drop Report"
    overview["A1"].font = Font(size=16, bold=True, color="1F4E78")
    overview.merge_cells("A1:D1")
    overview.append([])
    overview.append(["Project", project.name, "Project Type", project.project_type])
    overview.append(["PV Panel Types", len(project.pv_panels), "Configured Product Presets", len(project.product_presets)])
    overview.append(["Calculation Basis", "Per-preset selected PV panel", "System Voltage", "Varies by product"])
    overview.append(["Project Voltage Drop (V)", report.project_level.voltage_drop, "Project Voltage Drop (%)", report.project_level.voltage_drop_percent])
    overview.append(["INV Count", len(project.invs), "Product Instance Count", len(report.products)])
    overview.append(["Node Count", sum(len(inv.nodes) for inv in project.invs), "Line Segment Count", len(report.segments)])
    overview.append(["Row Count", sum(len(node.rows) for inv in project.invs for node in inv.nodes), "Status", "Valid"])
    for row in (3, 4, 5, 6, 7, 8, 9):
        overview.cell(row, 1).fill = _TITLE_FILL
        overview.cell(row, 1).font = Font(bold=True)
        overview.cell(row, 3).fill = _TITLE_FILL
        overview.cell(row, 3).font = Font(bold=True)
    overview["B6"].number_format = "0.000"
    overview["D6"].number_format = "0.00%"
    for column, width in zip(("A", "B", "C", "D"), (28, 22, 28, 22)):
        overview.column_dimensions[column].width = width
    report_progress()

    hierarchy = workbook.create_sheet("Hierarchy")
    hierarchy.append([
        "Level", "Name", "Path", "Product Count", "Child Count",
        "Voltage Drop (V)", "Voltage Drop (%)", "Status",
    ])
    for level in report.levels:
        hierarchy.append([
            level.level,
            level.name,
            level.path,
            level.product_count,
            level.child_count,
            level.voltage_drop,
            level.voltage_drop_percent,
            "Valid" if level.valid else "N/A",
        ])
    _style_table(hierarchy, 1, [14, 20, 46, 16, 14, 20, 20, 14])
    _apply_number_formats(hierarchy, {6: "0.000", 7: "0.00%"})
    report_progress()

    instances = workbook.create_sheet("Product Instances")
    instances.append([
        "Instance ID", "Type", "INV", "Node", "Row", "Racking Entry",
        "Rackings", "Preset", "Positive Code", "Negative Code", "Strings",
        "Positive Drop (V)", "Negative Drop (V)", "Loop Drop (V)",
        "Voltage Drop (%)", "Branch Drop (V)", "Mainline Drop (V)", "Status",
        "PV Panel", "System Voltage (V)", "String Current (A)",
    ])
    for product in report.products:
        instances.append([
            product.instance_id,
            product.product_type,
            product.inv_name,
            product.node_name,
            product.row_name,
            product.entry_index or None,
            product.rackings_name,
            product.preset_name,
            product.positive_code,
            product.negative_code,
            product.string_count,
            product.positive_drop,
            product.negative_drop,
            product.voltage_drop,
            product.voltage_drop_percent,
            product.branch_drop,
            product.mainline_drop,
            "Valid" if product.valid else "N/A",
            product.pv_panel_name,
            product.system_voltage,
            product.string_current,
        ])
    _style_table(instances, 1, [20, 20, 14, 14, 14, 16, 16, 12, 16, 16, 10, 18, 18, 18, 18, 18, 18, 12, 18, 18, 18])
    _apply_number_formats(instances, {12: "0.000", 13: "0.000", 14: "0.000", 15: "0.00%", 16: "0.000", 17: "0.000", 20: "0.000", 21: "0.000"})
    report_progress()

    summary = workbook.create_sheet("Product Summary")
    summary.append([
        "Type", "Preset", "PV Panel", "Positive Code", "Negative Code", "Quantity",
        "Total Strings", "Avg Positive (V)", "Avg Negative (V)",
        "Avg Drop (V)", "Min Drop (V)", "Max Drop (V)",
        "Avg Drop (%)", "Max Drop (%)",
    ])
    grouped: dict[tuple[str, str, str, str, str], list] = defaultdict(list)
    for product in report.products:
        grouped[(product.product_type, product.preset_name, product.pv_panel_name, product.positive_code, product.negative_code)].append(product)
    for key, group in grouped.items():
        drops = [product.voltage_drop for product in group if product.voltage_drop is not None]
        percents = [product.voltage_drop_percent for product in group if product.voltage_drop_percent is not None]
        positives = [product.positive_drop for product in group if product.positive_drop is not None]
        negatives = [product.negative_drop for product in group if product.negative_drop is not None]
        summary.append([
            *key,
            len(group),
            sum(product.string_count for product in group),
            sum(positives) / len(positives),
            sum(negatives) / len(negatives),
            sum(drops) / len(drops),
            min(drops),
            max(drops),
            sum(percents) / len(percents),
            max(percents),
        ])
    _style_table(summary, 1, [20, 12, 18, 16, 16, 12, 14, 18, 18, 16, 16, 16, 16, 16])
    _apply_number_formats(summary, {8: "0.000", 9: "0.000", 10: "0.000", 11: "0.000", 12: "0.000", 13: "0.00%", 14: "0.00%"})
    report_progress()

    segments = workbook.create_sheet("Line Segments")
    segments.append([
        "Instance ID", "Type", "Path", "Polarity", "Role", "Wire Size",
        "Material", "Length (ft)", "Current (A)", "Ohms/kft", "Voltage Drop (V)",
    ])
    for segment in report.segments:
        segments.append([
            segment.instance_id,
            segment.product_type,
            segment.path,
            segment.polarity,
            segment.role,
            segment.wire_size,
            segment.material,
            segment.length_ft,
            segment.current_a,
            segment.ohms_per_kft,
            segment.voltage_drop,
        ])
    _style_table(segments, 1, [20, 20, 42, 12, 28, 14, 12, 16, 16, 16, 18])
    _apply_number_formats(segments, {8: "0.0000", 9: "0.000", 10: "0.000000", 11: "0.000000"})
    report_progress()

    safe_name = re.sub(r'[<>:"/\\|?*]+', "_", project.name or "Project").strip(" .") or "Project"
    filename = f"{safe_name}压降信息{datetime.now().strftime('%m%d%Y')}.xlsx"
    path = os.path.join(directory, filename)
    workbook.save(path)
    return path

"""对应关系表导出 — 将 ProductGenerationResult 中的每个产品实例输出为独立 XLSX 文件。

导出三个工作表：
- IBEX+Homerun：按现有逻辑，显示所有字段
- Gap Extender：仅显示 rackings名称 / 产品preset名称 / 产品编号 / preset remarks，
  按显示属性去重合并，显示总数量
- LYNX：不显示 Row/RTR/offset/whip/rackings/extender/跨过的产品，
  但显示 Box position / Cluster Length / Buffer length / first slot length /
  外部主线总长 / Cluster线号 / row_per_overmold / Overmold个数 / 支线个数
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .result import ProductGenerationResult
    from ...models.project import Project


def export_correspondence_table(
    result: ProductGenerationResult,
    project: Project,
    directory: str,
    progress_callback: Callable[[int, int], None] | None = None,
) -> str:
    """将对应关系表导出为独立的 XLSX 文件。

    Args:
        result: 产品生成结果。
        project: 当前项目。
        directory: 输出目录路径。
        progress_callback: 每写入一行产品数据后调用，
            参数为 (已写入行数, 总产品数)。

    Returns:
        生成的 XLSX 文件完整路径。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()

    # ---- 分类产品 ----
    ibex_he_products = [
        p for p in result.all_products
        if p.product_type in ("IBEX", "Homerun_Extender")
    ]
    gap_products = [
        p for p in result.all_products
        if p.product_type == "Gap_Extender"
    ]
    lynx_products = [
        p for p in result.all_products
        if p.product_type == "LYNX"
    ]

    total = len(result.all_products)
    completed = 0

    header_font = Font(bold=True)
    center_align = Alignment(horizontal="center")
    today = datetime.now()

    # ================================================================
    # Sheet 1: IBEX + Homerun Extender（保持现有逻辑）
    # ================================================================
    ws1 = wb.active
    ws1.title = "IBEX+Homerun"

    ibex_headers = [
        "INV名称",
        "Node名称",
        "Row名称",
        "RTR个数",
        "racking_pitch",
        "offset长度",
        "offset长度值(ft)",
        "whip长度(ft)",
        "rackings名称",
        "产品preset名称",
        "产品编号",
        "extender长度(ft)",
        "跨过的产品",
        "preset remarks",
    ]
    ws1.append(ibex_headers)
    for col_idx in range(1, len(ibex_headers) + 1):
        cell = ws1.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.alignment = center_align

    for product in ibex_he_products:
        ws1.append([
            product.inv_name,
            product.node_name,
            product.row_name,
            product.rtr_count if product.rtr_count is not None else "",
            product.racking_pitch_str,
            product.offset_str,
            product.offset_ft if product.offset_ft is not None else "",
            product.whip_ft if product.whip_ft is not None else "",
            product.rackings_name,
            product.preset_name,
            product.code,
            product.extender_ft if product.extender_ft is not None else "",
            product.crossed_presets,
            product.preset_remarks,
        ])
        completed += 1
        if progress_callback is not None:
            progress_callback(completed, total)

    # 列宽
    for col_idx, width in enumerate(
        [14, 14, 14, 12, 18, 16, 18, 16, 16, 16, 14, 18, 22, 22], 1
    ):
        col_letter = ws1.cell(row=1, column=col_idx).column_letter
        ws1.column_dimensions[col_letter].width = width

    # ================================================================
    # Sheet 2: Gap Extender（去重合并，显示总数量）
    # ================================================================
    ws2 = wb.create_sheet("Gap Extender")

    gap_headers = [
        "rackings名称",
        "产品preset名称",
        "产品编号",
        "preset remarks",
        "总数量",
    ]
    ws2.append(gap_headers)
    for col_idx in range(1, len(gap_headers) + 1):
        cell = ws2.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.alignment = center_align

    # 按显示属性分组去重
    gap_groups: dict[tuple[str, str, str, str], int] = {}
    for product in gap_products:
        key = (
            product.rackings_name,
            product.preset_name,
            product.code,
            product.preset_remarks,
        )
        gap_groups[key] = gap_groups.get(key, 0) + 1

    for (rackings_name, preset_name, code, remarks), qty in gap_groups.items():
        ws2.append([rackings_name, preset_name, code, remarks, qty])
        completed += qty
        if progress_callback is not None:
            progress_callback(completed, total)

    for col_idx, width in enumerate([16, 16, 14, 22, 12], 1):
        col_letter = ws2.cell(row=1, column=col_idx).column_letter
        ws2.column_dimensions[col_letter].width = width

    # ================================================================
    # Sheet 3: LYNX
    # ================================================================
    ws3 = wb.create_sheet("LYNX")

    lynx_headers = [
        "INV名称",
        "Node名称",
        "Box position",
        "racking_pitch",
        "Cluster Length (ft)",
        "Buffer length (ft)",
        "first slot length (ft)",
        "外部主线总长 (ft)",
        "Cluster线号",
        "row_per_overmold",
        "Overmold个数",
        "支线个数",
        "产品preset名称",
        "产品编号",
        "preset remarks",
    ]
    ws3.append(lynx_headers)
    for col_idx in range(1, len(lynx_headers) + 1):
        cell = ws3.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.alignment = center_align

    for product in lynx_products:
        ws3.append([
            product.inv_name,
            product.node_name,
            product.box_position,
            product.racking_pitch_str,
            product.cluster_length_ft if product.cluster_length_ft else "",
            product.buffer_length_ft if product.buffer_length_ft else "",
            product.first_slot_length_ft if product.first_slot_length_ft else "",
            product.external_main_line_total_ft if product.external_main_line_total_ft else "",
            product.cluster_wire_info,
            product.row_per_overmold,
            product.overmold_count,
            product.lynx_branch_count,
            product.preset_name,
            product.code,
            product.preset_remarks,
        ])
        completed += 1
        if progress_callback is not None:
            progress_callback(completed, total)

    for col_idx, width in enumerate(
        [14, 14, 14, 18, 20, 20, 22, 22, 14, 18, 14, 12, 16, 14, 22], 1
    ):
        col_letter = ws3.cell(row=1, column=col_idx).column_letter
        ws3.column_dimensions[col_letter].width = width

    # ---- 生成文件名 ----
    safe_name = project.name if project.name else "Project"
    mmddyyyy = today.strftime("%m%d%Y")
    filename = f"{safe_name}对应关系表{mmddyyyy}.xlsx"
    output_path = os.path.join(directory, filename)

    wb.save(output_path)
    return output_path

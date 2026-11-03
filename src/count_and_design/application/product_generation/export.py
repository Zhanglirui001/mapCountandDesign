"""导出适配器 — 将 ProductGenerationResult 导出为 XLSX 文件。

导出逻辑从 ProductGenerationService 中移出，作为独立函数存在，
保持 Service 的纯粹性（不依赖 openpyxl 等呈现层库）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import os

from ...models.assembled_product import AssembledProduct
from ...models.project import Project
from .codegen import _get_digits
from .homerun_clustering import (
    HomerunExtenderClass,
    cluster_homerun_extender_products,
    homerun_total_wire_length,
    homerun_variable_wire_index,
    homerun_variable_wire_length,
)
from .lynx import (
    LynxCluster,
    _build_lynx_column,
    _build_lynx_product_description,
    cluster_lynx_products,
)


def _get_ibex_product_type(product: AssembledProduct) -> str:
    """根据是否使用铝线返回 IBEX 产品的 Type。

    使用了铝线 → "IBEX"，否则 → "Harness"。
    """
    return "IBEX" if product.uses_aluminum_wire else "Harness"


def _get_ibex_product_description(product: AssembledProduct) -> str:
    """根据是否使用铝线返回 IBEX 产品的 Product Description。

    未使用铝线时，将描述中的 "IBEX" 替换为 "Harness"。
    """
    if product.uses_aluminum_wire:
        return product.description
    return product.description.replace("IBEX", "Harness")


def _get_homerun_extender_product_type(product: AssembledProduct) -> str:
    """根据是否使用铝线返回 Homerun Extender 产品的 Type。

    判断方式与 IBEX 一致（依据 uses_aluminum_wire）：
    实际使用了铝线 → "ALEX"，否则 → "CUEX"。
    """
    return "ALEX" if product.uses_aluminum_wire else "CUEX"


def export_to_xlsx(
    result: ProductGenerationResult,
    project: Project,
    path: str,
    progress_callback: Callable[[int, int], None] | None = None,
) -> str:
    """将 parts 和 semi_parts 导出为 xlsx 文件。

    - "Project" 工作表：项目信息
    - "Project Parts Information" 工作表：项目部件信息模板
    - "Parts Reference" 工作表：parts 列表
    - "Semi Product" 工作表：semi_parts 列表
    - "Wire Length & Quantities" 工作表：线长与数量
    - "Products info" 工作表：产品信息
    - "LYNX Cluster Length&Quantities" 工作表：LYNX 簇长度与数量
    - 为每个 IBEX/LYNX 产品创建独立工作表

    工作簿文件名自动生成为：Project.name + "产品图纸" + MMDDYYYY + ".xlsx"

    若提供 progress_callback，则在每创建一个产品工作表后调用，
    参数为 (已创建工作表数, 总产品工作表数)。合并为同一类别的 LYNX
    产品只生成一个工作表，因此按类别计数。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()

    header_font = Font(bold=True)
    today = datetime.now()

    def _get_direction_no_by_index(parent: object | None, semi_index: int) -> int | str:
        """按父部件中 semi 子部件的出现顺序返回方向号。"""
        if parent is None or semi_index < 0:
            return ""

        direction_nos = getattr(parent, "direction_nos", [])
        if not isinstance(direction_nos, list):
            return ""

        return direction_nos[semi_index] if semi_index < len(direction_nos) else ""

    # =========================================================================
    # Project 工作表
    # =========================================================================
    ws_project = wb.active
    ws_project.title = "Project"

    # ---- 第 1 行表头 ----
    headers_project = [
        "Client Name",
        "Project Name",
        "Project Name(Label)",
        "Project Address",
        "Scale",
        "Date",
        "Designer",
        "Reviewer",
        "Approved by",
        "POS Color",
        "NEG Color",
        "Implementation Standards",
        "Project No.",
        "Heat Shrink Label",
        "Dust Cap Type",
        "Heated Hat",
        "Fuse Certification Standard",
    ]
    ws_project.append(headers_project)
    for col_idx, _header in enumerate(headers_project, 1):
        cell = ws_project.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    # ---- 第 2 行数据 ----
    date_str = today.strftime("%Y.%m.%d")
    ws_project.cell(row=2, column=1, value=project.client_name)
    ws_project.cell(row=2, column=2, value=project.name.upper() if project.name else "")
    ws_project.cell(row=2, column=3, value=project.name)
    ws_project.cell(row=2, column=4, value=project.address.upper() if project.address else "")
    ws_project.cell(row=2, column=5, value="N.T.S.")
    ws_project.cell(row=2, column=6, value=date_str)
    # 7=Designer, 8=Reviewer, 9=Approved by 留空
    ws_project.cell(row=2, column=10, value="Red")
    ws_project.cell(row=2, column=11, value="Black")
    ws_project.cell(row=2, column=12, value="UL 9703")
    ws_project.cell(row=2, column=13, value="24322")

    # ---- 第 3 行子表头 ----
    headers_sub = ["Type", "Bulk Packing Method", "Spare Parts Packing Method"]
    for col_idx, header in enumerate(headers_sub, 1):
        cell = ws_project.cell(row=3, column=col_idx, value=header)
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    # ---- 第 4 行起：包装方式数据 ----
    type_data = [
        ("Harness", "Reel", "Box"),
        ("LYNX", "Reel", "Box"),
        ("LYNX Plus", "Reel", "Box"),
        ("IBEX", "Reel", "Box"),
        ("IBEX Plus", "Reel", "Box"),
        ("CUEX", "Box", ""),
        ("ALEX", "Box", ""),
    ]
    for row_idx, (type_val, bulk_val, spare_val) in enumerate(type_data, 4):
        ws_project.cell(row=row_idx, column=1, value=type_val)
        ws_project.cell(row=row_idx, column=2, value=bulk_val)
        ws_project.cell(row=row_idx, column=3, value=spare_val)

    # ---- 第 2 行数据（续）：新增项目属性 ----
    ws_project.cell(row=2, column=14, value="no")
    ws_project.cell(row=2, column=15, value="Silicone")
    ws_project.cell(row=2, column=16, value="no")
    ws_project.cell(row=2, column=17, value="ul")

    # 调整列宽
    col_widths_project = [
        16, 18, 18, 22, 12, 14, 14, 14, 14, 12, 12, 18, 14, 18, 18, 14, 28,
    ]
    for col_idx, width in enumerate(col_widths_project, 1):
        col_letter = ws_project.cell(row=1, column=col_idx).column_letter
        ws_project.column_dimensions[col_letter].width = width

    # =========================================================================
    # Project Parts Information 工作表
    # =========================================================================
    ws_project_parts = wb.create_sheet("Project Parts Information")
    project_parts_rows = [
        ["LYNX Plus", "", "", "", "", ""],
        ["No.", "Code", "Material Name", "Price", "Qty", "Remark"],
        ["Harness", "", "", "", "", ""],
        ["No.", "Code", "Material Name", "Price", "Qty", "Remark"],
    ]
    for row in project_parts_rows:
        ws_project_parts.append(row)

    for row_idx in (2, 4):
        for cell in ws_project_parts[row_idx]:
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

    for row_idx in (1, 3):
        ws_project_parts.cell(row=row_idx, column=1).font = header_font

    col_widths_project_parts = [14, 18, 24, 14, 10, 24]
    for col_idx, width in enumerate(col_widths_project_parts, 1):
        col_letter = ws_project_parts.cell(row=1, column=col_idx).column_letter
        ws_project_parts.column_dimensions[col_letter].width = width

    # =========================================================================
    # Parts Reference 工作表
    # =========================================================================
    ws_parts = wb.create_sheet("Parts Reference")

    headers_parts = [
        "Parts No.",
        "CAD Parts No.",
        "CAD Parts Length(mm)",
        "Category",
        "Description",
        "Chinese Description",
        "Unit",
    ]
    ws_parts.append(headers_parts)

    for col_idx, header in enumerate(headers_parts, 1):
        cell = ws_parts.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    sorted_parts = sorted(result.parts, key=lambda p: (p.category, p.description, p.cad_parts_length))
    for part in sorted_parts:
        ws_parts.append([
            part.code,                    # A: Parts No.
            part.cad_parts_no,            # B: CAD Parts No.
            round(part.cad_parts_length, 4),        # C: CAD Parts Length(mm)
            part.category,                # D: Category
            part.description,             # E: Description
            "",                           # F: Chinese Description (N/A)
            part.unit,                    # G: Unit
        ])

    col_widths_parts = [18, 28, 22, 16, 55, 22, 10]
    for col_idx, width in enumerate(col_widths_parts, 1):
        ws_parts.column_dimensions[ws_parts.cell(row=1, column=col_idx).column_letter].width = width

    # =========================================================================
    # Semi Product 工作表
    # =========================================================================
    ws_semi = wb.create_sheet("Semi Product")

    headers_semi = [
        "Semi Product No.",
        "Parts No.",
        "Type",
        "Overmolding Detail(Up&Down)",
        "Direction No.",
        "Length(FT)",
        "Label Detail",
    ]
    ws_semi.append(headers_semi)

    for col_idx, header in enumerate(headers_semi, 1):
        cell = ws_semi.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    current_row = 2  # 从第2行开始（第1行是表头）
    for semi_part in result.semi_parts:
        if not semi_part.semi_list:
            # 没有子部件的半成品：仍然占一行
            ws_semi.append([
                semi_part.code,
                "",
                "Semi" if semi_part.semi_or_not else "",
                semi_part.overmolding_detail,
                _get_direction_no(None, semi_part),
                round(semi_part.length, 4) if semi_part.length else None,
                "",
            ])
            current_row += 1
            continue

        start_row = current_row
        semi_index = 0
        for sub_part in semi_part.semi_list:
            direction_no = ""
            if sub_part.semi_or_not:
                direction_no = _get_direction_no_by_index(semi_part, semi_index)
                semi_index += 1
            ws_semi.append([
                semi_part.code,                     # A: Semi Product No.
                sub_part.code,                      # B: Parts No.
                "Semi" if sub_part.semi_or_not else "",  # C: Type
                sub_part.overmolding_detail,        # D: Overmolding Detail
                direction_no,                       # E: Direction No.
                round(sub_part.length, 4) if sub_part.length else None,                    # F: Length(FT)
                "",                                 # G: Label Detail
            ])
            current_row += 1
        end_row = current_row - 1

        # 合并 A 列（Semi Product No.）
        if end_row > start_row:
            ws_semi.merge_cells(
                start_row=start_row, start_column=1,
                end_row=end_row, end_column=1,
            )
        # 设置合并单元格垂直居中
        ws_semi.cell(row=start_row, column=1).alignment = Alignment(
            vertical="center", horizontal="center"
        )

    col_widths_semi = [22, 18, 10, 28, 16, 14, 22]
    for col_idx, width in enumerate(col_widths_semi, 1):
        ws_semi.column_dimensions[ws_semi.cell(row=1, column=col_idx).column_letter].width = width

    # =========================================================================
    # Wire Length & Quantities 工作表
    # =========================================================================
    ws_wire = wb.create_sheet("Wire Length & Quantities")
    headers_wire = [
        "Extender/Whip",
        "Item",
        "Length(ft)",
        "Total Length(ft)",
        "Qty(pcs)",
        "Spare Part",
        "Product No.",
    ]
    ws_wire.append(headers_wire)
    for col_idx, _header in enumerate(headers_wire, 1):
        cell = ws_wire.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    col_widths_wire = [16, 14, 14, 18, 14, 14, 18]
    for col_idx, width in enumerate(col_widths_wire, 1):
        col_letter = ws_wire.cell(row=1, column=col_idx).column_letter
        ws_wire.column_dimensions[col_letter].width = width

    # =========================================================================
    # Products info 工作表
    # =========================================================================
    ws_products = wb.create_sheet("Products info")
    headers_products = [
        "Item Code",
        "Type",
        "Quantity",
        "Spare Part",
        "Product Description",
        "POS/NEG",
        "Product No.",
        "LYNX",
        "Branch Size",
        "Trunk Bus Size",
        "Overmold Gap",
    ]
    ws_products.append(headers_products)
    for col_idx, _header in enumerate(headers_products, 1):
        cell = ws_products.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    col_widths_products = [14, 10, 12, 12, 22, 12, 14, 10, 14, 16, 14]
    for col_idx, width in enumerate(col_widths_products, 1):
        col_letter = ws_products.cell(row=1, column=col_idx).column_letter
        ws_products.column_dimensions[col_letter].width = width

    # ---- 预先构建需要按类别导出的产品 ----
    lynx_clusters = cluster_lynx_products(result.lynx_products)
    homerun_classes = cluster_homerun_extender_products(
        result.homerun_extender_products
    )

    # ---- 填充产品数据 ----
    current_row = 2  # 从第2行开始（第1行是表头）

    # IBEX 产品
    for product in result.ibex_products:
        ws_products.cell(row=current_row, column=1, value=product.code)       # A: Item Code
        ws_products.cell(row=current_row, column=2, value=_get_ibex_product_type(product))  # B: Type
        ws_products.cell(row=current_row, column=3, value=product.quantity)   # C: Quantity
        # D: Spare Part (留空)
        ws_products.cell(row=current_row, column=5, value=_get_ibex_product_description(product))  # E: Product Description
        ws_products.cell(row=current_row, column=6, value=product.polarity)   # F: POS/NEG
        # G: Product No. (留空)
        current_row += 1

    # LYNX 产品（按类别汇总，每类一行）
    for cluster in lynx_clusters:
        ws_products.cell(row=current_row, column=1, value=cluster.class_name)       # A: Item Code
        ws_products.cell(row=current_row, column=2, value="LYNX Plus")              # B: Type
        # C: Quantity (留空)
        # D: Spare Part (留空)
        description = _build_lynx_product_description(cluster.total_branch_count, cluster.polarity)
        ws_products.cell(row=current_row, column=5, value=description)              # E: Product Description
        ws_products.cell(row=current_row, column=6, value=cluster.polarity)         # F: POS/NEG
        # G: Product No. (留空)
        # H: 空列（后面整体前移）
        ws_products.cell(row=current_row, column=8, value=_build_lynx_column(cluster.branch_counts))   # H: LYNX (原 I 列前移)
        ws_products.cell(row=current_row, column=9, value=cluster.first_branch_wire_digits)            # I: Branch Size (原 J 列前移)
        ws_products.cell(row=current_row, column=10, value=cluster.mainline_wire_digits)               # J: Trunk Bus Size (原 K 列前移)
        # K: Overmold Gap (留空，原 L 列前移)
        current_row += 1

    # Gap Extender 产品
    for product in result.gap_extender_products:
        ws_products.cell(row=current_row, column=1, value=product.code)       # A: Item Code
        ws_products.cell(row=current_row, column=2, value="CUEX")             # B: Type
        # C: Quantity (留空)
        # D: Spare Part (留空)
        ws_products.cell(row=current_row, column=5, value=product.description)  # E: Product Description
        ws_products.cell(row=current_row, column=6, value="NEG")              # F: POS/NEG
        # G: Product No. (留空)
        current_row += 1

    # Homerun Extender 产品（按类别汇总，每类一行）
    for product_class in homerun_classes:
        product = product_class.representative
        ws_products.cell(row=current_row, column=1, value=product_class.class_name)  # A: Item Code
        ws_products.cell(row=current_row, column=2, value=_get_homerun_extender_product_type(product))  # B: Type
        # C: Quantity (各变体数量在 Wire Length & Quantities 中列出)
        # D: Spare Part (留空)
        ws_products.cell(row=current_row, column=5, value=product.description)  # E: Product Description
        ws_products.cell(row=current_row, column=6, value=product.polarity)   # F: POS/NEG
        # G: Product No. (留空)
        current_row += 1

    # =========================================================================
    # LYNX Cluster Length&Quantities 工作表
    # =========================================================================
    ws_cluster = wb.create_sheet("LYNX Cluster Length&Quantities")
    headers_cluster = [
        "LYNX Cluster",
        "Item",
        "Label",
        "Length(ft)",
        "Qty(pcs)",
        "Spare Part",
        "Product No.",
        "Remark",
    ]
    ws_cluster.append(headers_cluster)
    for col_idx, _header in enumerate(headers_cluster, 1):
        cell = ws_cluster.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    col_widths_cluster = [16, 14, 14, 14, 12, 14, 18, 18]
    for col_idx, width in enumerate(col_widths_cluster, 1):
        col_letter = ws_cluster.cell(row=1, column=col_idx).column_letter
        ws_cluster.column_dimensions[col_letter].width = width

    # ---- 填充 LYNX Cluster 数据 ----
    # 按 class_name 排序，确保同一 cluster 的行相邻且顺序一致
    sorted_clusters = sorted(lynx_clusters, key=lambda c: c.class_name)
    current_row = 2  # 数据从第 2 行开始（第 1 行是表头）
    for cluster in sorted_clusters:
        # 同一 cluster 内按产品 code 排序，保证稳定输出
        sorted_products = sorted(cluster.products, key=lambda p: p.code)
        start_row = current_row
        for product in sorted_products:
            first_part_length_ft = round(product.parts[0].length, 4) if product.parts else 0.0
            ws_cluster.append([
                cluster.class_name,       # A: LYNX Cluster
                product.code,              # B: Item
                "",                        # C: Label
                first_part_length_ft,      # D: Length(ft)
                product.quantity,          # E: Qty(pcs)
                "",                        # F: Spare Part
                "",                        # G: Product No.
                "",                        # H: Remark
            ])
            current_row += 1
        end_row = current_row - 1

        # 同一 LYNX Cluster 值的行合并 A 列单元格
        if end_row > start_row:
            ws_cluster.merge_cells(
                start_row=start_row, start_column=1,
                end_row=end_row, end_column=1,
            )
            ws_cluster.cell(row=start_row, column=1).alignment = Alignment(
                vertical="center", horizontal="center"
            )

    # =========================================================================
    # 辅助函数：为产品创建工作表
    # =========================================================================
    def _add_product_sheet(
        product: AssembledProduct,
        product_type: str,
    ) -> None:
        """为单个产品创建工作表。

        Args:
            product: 组装后的产品。
            product_type: "IBEX" 或 "Homerun_Extender"。
        """
        # 根据铝线使用情况确定最终 Type 和 Description
        if product_type == "IBEX":
            final_type = _get_ibex_product_type(product)
            final_description = _get_ibex_product_description(product)
        else:
            final_type = _get_homerun_extender_product_type(product)
            final_description = product.description

        # 确保工作表名称唯一（31 字符 Excel 限制）
        base_name = product.code[:31]
        sheet_name = base_name
        counter = 1
        while sheet_name in wb.sheetnames:
            suffix = f"_{counter}"
            max_base = 31 - len(suffix)
            sheet_name = base_name[:max_base] + suffix
            counter += 1

        ws = wb.create_sheet(sheet_name)

        # ---- 第 1 行表头 ----
        row1_headers = [
            "Item Code",
            "Type",
            "Quantity",
            "Spare Part",
            "Product Description",
            "POS/NEG",
            "Labeling method",
            "Product No.",
            "Solaris Edition",
        ]
        for col_idx, header in enumerate(row1_headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # ---- 第 2 行数据 ----
        ws.cell(row=2, column=1, value=product.code)        # A: Item Code
        ws.cell(row=2, column=2, value=final_type)          # B: Type
        ws.cell(row=2, column=3, value=product.quantity)    # C: Quantity
        # D: Spare Part (留空)
        ws.cell(row=2, column=5, value=final_description)  # E: Product Description
        ws.cell(row=2, column=6, value=product.polarity)    # F: POS/NEG
        ws.cell(row=2, column=7, value="LA")                # G: Labeling method
        # H: Product No. (留空)
        ws.cell(row=2, column=9, value=product.pv_panel)  # I: downstream-compatible panel column

        # ---- 第 3 行表头 ----
        row3_headers = [
            "Parts No.",
            "Type",
            "Length(FT)",
            "Label Detail",
        ]
        for col_idx, header in enumerate(row3_headers, 1):
            cell = ws.cell(row=3, column=col_idx, value=header)
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # ---- 从第 4 行开始填充 parts ----
        current_row = 4
        for part in product.parts:
            ws.cell(row=current_row, column=1, value=part.code)            # A: Parts No.
            ws.cell(row=current_row, column=2,                           # B: Type
                    value="Semi" if part.semi_or_not else "")
            ws.cell(row=current_row, column=3, value=round(part.length, 4) if part.length else None)          # C: Length(FT)
            # D: Label Detail (留空)
            current_row += 1

        # 调整列宽
        col_widths = [18, 12, 14, 18]
        for col_idx, width in enumerate(col_widths, 1):
            ws.column_dimensions[
                ws.cell(row=3, column=col_idx).column_letter
            ].width = width

    def _add_gap_extender_sheet(
        ws_wire: object,
        wb: object,
        product: AssembledProduct,
        project: Project,
        result: ProductGenerationResult,
        header_font: object,
    ) -> None:
        """为 Gap Extender 产品创建工作表并填充 Wire Length & Quantities 行。

        Args:
            ws_wire: Wire Length & Quantities 工作表。
            wb: 工作簿。
            product: Gap Extender 产品。
            project: 项目（用于获取 preset 信息）。
            result: 生成结果。
            header_font: 表头字体样式。
        """
        from openpyxl.styles import Alignment

        # 获取产品对应的 preset
        preset = getattr(product, "_gap_extender_preset", None)
        if preset is None:
            return

        # ---- 在 Wire Length & Quantities 工作表中添加一行 ----
        length_ft = round(preset.length.to_ft(), 4) if preset.length else 0
        ws_wire.append([
            product.code,       # A: Extender/Whip
            1,                   # B: Item
            length_ft,          # C: Length(ft)
            "",                 # D: Total Length(ft)
            product.quantity,   # E: Qty(pcs)
            "",                 # F: Spare Part
            "",                 # G: Product No.
        ])

        # ---- 为 Gap Extender 创建独立工作表 ----
        base_name = product.code[:31]
        sheet_name = base_name
        counter = 1
        while sheet_name in wb.sheetnames:
            suffix = f"_{counter}"
            max_base = 31 - len(suffix)
            sheet_name = base_name[:max_base] + suffix
            counter += 1

        ws = wb.create_sheet(sheet_name)

        # ---- 第 1 行表头 ----
        row1_headers = [
            "Item Code",
            "Type",
            "Quantity",
            "Spare Part",
            "Product Description",
            "POS/NEG",
            "Labeling method",
            "Product No.",
            "Solaris Edition",
        ]
        for col_idx, header in enumerate(row1_headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # ---- 第 2 行数据 ----
        ws.cell(row=2, column=1, value=product.code)        # A: Item Code
        ws.cell(row=2, column=2, value="CUEX")             # B: Type = CUEX
        # C: Quantity (留空)
        # D: Spare Part (留空)
        ws.cell(row=2, column=5, value=product.description) # E: Product Description
        ws.cell(row=2, column=6, value="NEG")              # F: POS/NEG
        ws.cell(row=2, column=7, value="LA")               # G: Labeling method
        # H: Product No. (留空)
        ws.cell(row=2, column=9, value=product.pv_panel)  # I: downstream-compatible panel column

        # ---- 第 3 行表头 ----
        row3_headers = [
            "Parts No.",
            "Type",
            "Length(FT)",
            "Label Detail",
        ]
        for col_idx, header in enumerate(row3_headers, 1):
            cell = ws.cell(row=3, column=col_idx, value=header)
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # ---- 从第 4 行开始填充 parts ----
        current_row = 4
        for part in product.parts:
            ws.cell(row=current_row, column=1, value=part.code)  # A: Parts No.
            ws.cell(row=current_row, column=2,                   # B: Type
                    value="Semi" if part.semi_or_not else "")
            # C: Length(FT) — 线缆(Wire)部件填 "LENGTH"，其余留空
            if part.category == "Wire":
                ws.cell(row=current_row, column=3, value="LENGTH")
            else:
                ws.cell(row=current_row, column=3, value=None)
            # D: Label Detail — Label 部件填入标签明细文本
            ws.cell(row=current_row, column=4,
                    value=part.label_detail if part.category == "Label" else "")
            current_row += 1

        # 调整列宽
        col_widths = [18, 12, 14, 18]
        for col_idx, width in enumerate(col_widths, 1):
            ws.column_dimensions[
                ws.cell(row=3, column=col_idx).column_letter
            ].width = width

    def _add_homerun_class_sheet(
        product_class: HomerunExtenderClass,
    ) -> None:
        """填充 Homerun 类别明细，并以首个变体生成一个代表分表。"""
        product = product_class.representative

        # ---- Wire Length & Quantities：每个精确产品变体一行 ----
        start_row = ws_wire.max_row + 1
        for item_index, variant in enumerate(product_class.products, 1):
            ws_wire.append([
                product_class.class_name,
                item_index,
                homerun_variable_wire_length(variant),
                homerun_total_wire_length(variant),
                variant.quantity,
                "",
                "",
            ])
        end_row = ws_wire.max_row
        if end_row > start_row:
            ws_wire.merge_cells(
                start_row=start_row,
                start_column=1,
                end_row=end_row,
                end_column=1,
            )
        ws_wire.cell(row=start_row, column=1).alignment = Alignment(
            vertical="center",
            horizontal="center",
        )

        # ---- 类别代表分表 ----
        base_name = product_class.class_name[:31]
        sheet_name = base_name
        counter = 1
        while sheet_name in wb.sheetnames:
            suffix = f"_{counter}"
            max_base = 31 - len(suffix)
            sheet_name = base_name[:max_base] + suffix
            counter += 1

        ws = wb.create_sheet(sheet_name)
        row1_headers = [
            "Item Code",
            "Type",
            "Quantity",
            "Spare Part",
            "Product Description",
            "POS/NEG",
            "Labeling method",
            "Product No.",
            "Solaris Edition",
        ]
        for col_idx, header in enumerate(row1_headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        ws.cell(row=2, column=1, value=product_class.class_name)
        ws.cell(
            row=2,
            column=2,
            value=_get_homerun_extender_product_type(product),
        )
        # C: Quantity (类别包含多个长度变体，留空)
        # D: Spare Part (留空)
        ws.cell(row=2, column=5, value=product.description)
        ws.cell(row=2, column=6, value=product.polarity)
        ws.cell(row=2, column=7, value="LA")
        # H: Product No. (留空)
        ws.cell(row=2, column=9, value=product.pv_panel)

        row3_headers = ["Parts No.", "Type", "Length(FT)", "Label Detail"]
        for col_idx, header in enumerate(row3_headers, 1):
            cell = ws.cell(row=3, column=col_idx, value=header)
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        variable_wire_index = homerun_variable_wire_index(product)
        for row_offset, part in enumerate(product.parts, 4):
            part_index = row_offset - 4
            ws.cell(row=row_offset, column=1, value=part.code)
            ws.cell(
                row=row_offset,
                column=2,
                value="Semi" if part.semi_or_not else "",
            )
            if part_index == variable_wire_index:
                length_value: float | str | None = "LENGTH"
            else:
                length_value = round(part.length, 4) if part.length else None
            ws.cell(row=row_offset, column=3, value=length_value)

        col_widths = [18, 12, 14, 18]
        for col_idx, width in enumerate(col_widths, 1):
            ws.column_dimensions[
                ws.cell(row=3, column=col_idx).column_letter
            ].width = width

    # ---- 计算实际需要创建的产品工作表总数 ----
    total_product_sheets = (
        len(result.ibex_products)
        + len(lynx_clusters)
        + len(result.gap_extender_products)
        + len(homerun_classes)
    )
    completed = 0

    # ---- 为每个 IBEX 产品生成工作表 ----
    for product in result.ibex_products:
        _add_product_sheet(product, "IBEX")
        completed += 1
        if progress_callback is not None:
            progress_callback(completed, total_product_sheets)

    # ---- 为每个 LYNX 类别生成工作表 ----
    def _add_lynx_cluster_sheet(cluster: LynxCluster) -> None:
        """为 LYNX 类别创建工作表（以第一个产品为基准）。"""
        first_product = cluster.products[0]
        description = _build_lynx_product_description(cluster.total_branch_count, cluster.polarity)

        base_name = cluster.class_name[:31]
        sheet_name = base_name
        counter = 1
        while sheet_name in wb.sheetnames:
            suffix = f"_{counter}"
            max_base = 31 - len(suffix)
            sheet_name = base_name[:max_base] + suffix
            counter += 1

        ws = wb.create_sheet(sheet_name)

        # ---- 第 1 行表头 ----
        row1_headers = [
            "Item Code",
            "Type",
            "Quantity",
            "Spare Part",
            "Product Description",
            "POS/NEG",
            "Labeling method",
            "Product No.",
            "Solaris Edition",
        ]
        for col_idx, header in enumerate(row1_headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # ---- 第 2 行数据 ----
        ws.cell(row=2, column=1, value=cluster.class_name)   # A: Item Code
        ws.cell(row=2, column=2, value="LYNX Plus")          # B: Type
        # C: Quantity (留空)
        # D: Spare Part (留空)
        ws.cell(row=2, column=5, value=description)          # E: Product Description
        ws.cell(row=2, column=6, value=cluster.polarity)     # F: POS/NEG
        ws.cell(row=2, column=7, value="LA")                 # G: Labeling method
        # H: Product No. (留空)
        ws.cell(row=2, column=9, value=first_product.pv_panel)  # I: downstream-compatible panel column

        # ---- 第 3 行表头 ----
        row3_headers = [
            "Parts No.",
            "Type",
            "Length(FT)",
            "Label Detail",
        ]
        for col_idx, header in enumerate(row3_headers, 1):
            cell = ws.cell(row=3, column=col_idx, value=header)
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        # ---- 从第 4 行开始填充 parts ----
        current_row = 4
        for idx, part in enumerate(first_product.parts):
            ws.cell(row=current_row, column=1, value=part.code)            # A: Parts No.
            ws.cell(row=current_row, column=2,                           # B: Type
                    value="Semi" if part.semi_or_not else "")
            # C: Length(FT) — 第一个 part 填 "LENGTH"，其余正常填
            if idx == 0:
                ws.cell(row=current_row, column=3, value="LENGTH")
            else:
                ws.cell(row=current_row, column=3, value=round(part.length, 4) if part.length else None)
            # D: Label Detail (留空)
            current_row += 1

        # 调整列宽
        col_widths = [18, 12, 14, 18]
        for col_idx, width in enumerate(col_widths, 1):
            ws.column_dimensions[
                ws.cell(row=3, column=col_idx).column_letter
            ].width = width

    for cluster in lynx_clusters:
        _add_lynx_cluster_sheet(cluster)
        completed += 1
        if progress_callback is not None:
            progress_callback(completed, total_product_sheets)

    # ---- 为每个 Gap Extender 产品生成工作表 ----
    for product in result.gap_extender_products:
        _add_gap_extender_sheet(ws_wire, wb, product, project, result, header_font)
        completed += 1
        if progress_callback is not None:
            progress_callback(completed, total_product_sheets)

    # ---- 为每个 Homerun Extender 类别生成工作表及长度明细 ----
    for product_class in homerun_classes:
        _add_homerun_class_sheet(product_class)
        completed += 1
        if progress_callback is not None:
            progress_callback(completed, total_product_sheets)

    # =========================================================================
    # 生成文件名：Project.name + "产品图纸" + MMDDYYYY + ".xlsx"
    # =========================================================================
    safe_name = project.name if project.name else "Project"
    mmddyyyy = today.strftime("%m%d%Y")
    filename = f"{safe_name}产品图纸{mmddyyyy}.xlsx"
    output_path = os.path.join(os.path.dirname(path), filename)

    wb.save(output_path)

    return output_path

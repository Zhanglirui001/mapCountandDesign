"""部件 & 半成品工厂函数 — 创建各种类型的 Part。"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ...models.connector import Connector
from ...models.fuse import Fuse
from ...models.length import Length
from ...models.part import Part
from ...models.wire import Wire
from .codegen import _get_new_alpha_counter, _get_new_counter, _get_wire_prefix
from .dedup import _find_duplicate_part

if TYPE_CHECKING:
    from ...models import ProductPreset, Project, Rackings


def _resolve_product_presets(
    rackings: Rackings, project: Project
) -> list[ProductPreset]:
    """将 Rackings.product_presets 字符串解析为 ProductPreset 列表。"""
    from ...models import ProductPreset

    result: list[ProductPreset] = []
    for ch in rackings.product_presets:
        for preset in project.product_presets:
            if preset.name == ch:
                result.append(preset)
                break
    return result


# =============================================================================
# 部件生成函数 (Part Factories)
# =============================================================================


def _make_connect_part(
    parts: list[Part], gender: str, connector: Connector
) -> Part:
    """创建连接器部件。"""
    part = Part()
    part.category = "Connect"
    dust_cap = "[Dust Cap]" if connector.type == "MC4-EVO2A" else "(Dust Cap)"
    if gender == "M":
        part.cad_parts_no = "Connector Male"
        part.cad_parts_length = 63.5013
        part.description = (
            f"{connector.size.wiresize} {connector.type} "
            f"Male-{connector.manufacturer} {dust_cap}"
        )
    elif gender == "F":
        part.cad_parts_no = "Connector Female"
        part.cad_parts_length = 66.2969
        part.description = (
            f"{connector.size.wiresize} {connector.type} "
            f"Female-{connector.manufacturer} {dust_cap}"
        )
    part.unit = "pcs"

    existing = _find_duplicate_part(parts, part)
    if existing is not None:
        return existing
    counter = _get_new_counter(parts, "Connect", gender)
    part.code = gender + counter
    parts.append(part)
    return part


def _make_fuse_part(parts: list[Part], fuse: Fuse) -> Part:
    """创建保险丝部件。"""
    part = Part()
    part.category = "Fuse"
    part.cad_parts_no = "Fuse"
    part.cad_parts_length = 134
    part.description = f"{fuse.current} Fuse+Fuseholder - {fuse.manufacturer}"
    part.unit = "pcs"

    existing = _find_duplicate_part(parts, part)
    if existing is not None:
        return existing
    counter = _get_new_alpha_counter(parts, "fuse", "F")
    part.code = "F" + counter
    parts.append(part)
    return part


def _make_kit_part(parts: list[Part], kit: str) -> Part:
    """创建套件部件（如 T 型转接件）。"""
    part = Part()
    part.category = kit + " Kit"
    part.cad_parts_no = kit
    part.cad_parts_length = 84
    part.description = kit + " Kit"
    part.unit = "pcs"

    existing = _find_duplicate_part(parts, part)
    if existing is not None:
        return existing
    part.code = kit
    parts.append(part)
    return part


def _make_cu_al_part(parts: list[Part]) -> Part:
    """创建铜铝转接器部件。

    该部件为铜铝转换连接器，不依赖于具体导线规格。
    """
    part = Part()
    part.category = "AC"
    part.cad_parts_no = "Cu - Al"
    part.cad_parts_length = 135
    part.description = "Cu-Al Conversion"
    part.unit = "pcs"

    existing = _find_duplicate_part(parts, part)
    if existing is not None:
        return existing
    counter = _get_new_counter(parts, "AC", "G")
    part.code = "G" + counter
    parts.append(part)
    return part


def _make_overmold_part(parts: list[Part], branch_count: int) -> Part:
    """创建注塑件部件。"""
    part = Part()
    part.category = "OverMolding"
    if branch_count <= 4:
        part.cad_parts_no = "1-4"
        part.cad_parts_length = 147
    elif branch_count <= 6:
        part.cad_parts_no = "1-6"
        part.cad_parts_length = 160
    elif branch_count <= 8:
        part.cad_parts_no = "1-8"
        part.cad_parts_length = 160
    else:
        part.cad_parts_no = "1-8"
        part.cad_parts_length = 160
    part.description = "OverMolding"
    part.unit = "pcs"

    existing = _find_duplicate_part(parts, part)
    if existing is not None:
        return existing
    counter = _get_new_counter(parts, "OverMolding", "OM-", start_from=1)
    part.code = "OM-" + counter
    parts.append(part)
    return part


def _make_wire_part(
    parts: list[Part],
    wire: Wire,
    wire_type: str,
    color: str,
    length: Length,
    cad_parts_length_override: float | None = None,
) -> Part:
    """创建导线部件。

    wire_type: "LYNX_Branch_Al" / "LYNX_Cable" / "LYNX_Cable_External" /
        "Homerun_Extender" / "Gap_Extender" / "normal"
    color: "Red" / "Black"
    """
    part = Part()
    part.category = "Wire"
    part.unit = "ft"

    # 根据 wire_type + material + strands 动态分配字母前缀
    _prefix = _get_wire_prefix(wire_type, wire.material, wire.strands)
    # 纯字母部分（不含末尾连字符），用于 cad_parts_no
    _prefix_letter = _prefix.rstrip("-")
    counter = _get_new_counter(parts, part.category, _prefix)

    if color == "Red":
        polarity = "Pos "
    else:
        polarity = "Neg "

    if wire.strands != "none":
        part.description = (
            f"{wire.wiresize} {color} PV Wire 2KV "
            f"{wire.strands} Strands {wire.material}"
        )
    else:
        part.description = (
            f"{wire.wiresize} {color} PV Wire 2KV {wire.material}"
        )

    # 归一化到 6 位小数：消除长度算术中的浮点精度噪声
    # （如 535.0000000000001 vs 535.0），避免本应相同的产品因
    # 微小的浮点误差而无法合并。
    length_ft = round(length.to_ft(), 6)

    if wire_type == "LYNX_Branch_Al":
        is_special = cad_parts_length_override is not None
        if color == "Red":
            part.cad_parts_no = "E2" if is_special else "E1"
        else:
            part.cad_parts_no = "E4" if is_special else "E3"
        part.cad_parts_length = (
            cad_parts_length_override
            if cad_parts_length_override is not None
            else 103.8167
        )
        part.code = _prefix + counter + "L"
    elif wire_type == "LYNX_Cable":
        part.cad_parts_no = "Cable " + polarity
        part.cad_parts_length = 309.9424
        part.code = _prefix + counter
    elif wire_type == "LYNX_Cable_External":
        part.cad_parts_no = "Cable " + polarity + "large"
        part.cad_parts_length = 600
        part.code = _prefix + counter
    elif wire_type == "normal":
        if length_ft <= 10:
            part.cad_parts_no = "Wire " + polarity + "1-10ft"
            part.cad_parts_length = 120
        elif length_ft <= 35:
            part.cad_parts_no = "Wire " + polarity + "11-35ft"
            part.cad_parts_length = 140
        elif length_ft <= 70:
            part.cad_parts_no = "Wire " + polarity + "36-70ft"
            part.cad_parts_length = 190
        elif length_ft <= 150:
            part.cad_parts_no = "Wire " + polarity + "71-150ft"
            part.cad_parts_length = 300
        else:
            part.cad_parts_no = "Wire " + polarity + "151-250ft"
            part.cad_parts_length = 400
        part.code = _prefix + counter
    elif wire_type == "Gap_Extender":
        part.cad_parts_no = "extender-NEG"
        part.cad_parts_length = 440
        part.code = _prefix + counter + "W"  # 末尾加 W 标记
    elif wire_type == "Homerun_Extender":
        # Homerun_Extender 导线：EP-{n} (正极Red) / EN-{n} (负极Black)
        polarity_letter = "P" if color == "Red" else "N"
        part.cad_parts_no = f"extender-{'POS' if polarity_letter == 'P' else 'NEG'}"
        part.cad_parts_length = 440
        counter = _get_new_counter(parts, part.category, "E" + polarity_letter + "-")
        part.code = "E" + polarity_letter + "-" + counter

    part.length = length_ft  # store the actual cut length
    if wire_type == "LYNX_Branch_Al":
        existing = _find_duplicate_part(parts, part, frozenset({"cad_parts_no"}))
        if existing is not None:
            return existing
    else:
        # 按桶去重（跳过 length），保持 Parts Reference 简洁。
        # 若命中已有 Part，克隆一份并填入实际长度返回给产品使用，
        # 克隆体不加入 result.parts，避免产品间的长度相互干扰。
        existing = _find_duplicate_part(parts, part, frozenset({"length"}))
        if existing is not None:
            return replace(existing, length=length_ft)
    parts.append(part)
    return part


def _make_label_part(
    parts: list[Part],
    cad_parts_no: str,
    description: str = "Label",
    label_detail: str = "",
) -> Part:
    """创建标签部件。

    参考 IBEX 导线克隆模式：去重时跳过 label_detail，
    命中已有部件后克隆一份并填入实际标签明细返回，
    克隆体不加入 result.parts，避免产品间的 label_detail 相互干扰。

    不同 description 的标签视为不同部件（如 Left/Right 标签），
    在 Parts Reference 表中各自占一行。

    Args:
        parts: 全局部件列表。
        cad_parts_no: CAD 部件编号。
        description: 标签描述（填入 Parts Reference 表 E 列）。
        label_detail: 标签明细文本（填入产品工作表 D 列）。
    """
    part = Part()
    part.cad_parts_no = cad_parts_no
    part.description = description
    part.label_detail = label_detail
    part.category = "Label"
    part.unit = "pcs"

    # 按标签类型去重（跳过 label_detail），保持 Parts Reference 简洁。
    # description 作为区分字段参与去重（Left/Right 标签各自独立）。
    # 若命中已有 Part，克隆一份并填入实际标签明细返回给产品使用，
    # 克隆体不加入 result.parts，避免产品间的 label_detail 相互干扰。
    existing = _find_duplicate_part(parts, part, frozenset({"label_detail"}))
    if existing is not None:
        return replace(existing, label_detail=label_detail)
    counter = _get_new_counter(parts, "Label", "L-")
    part.code = "L-" + counter
    parts.append(part)
    return part

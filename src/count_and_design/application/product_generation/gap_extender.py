"""Gap Extender 产品族生成逻辑。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...models.assembled_product import AssembledProduct
from .codegen import _get_digits
from .parts_factory import (
    _make_connect_part,
    _make_label_part,
    _make_wire_part,
)

if TYPE_CHECKING:
    from ...models import Part, ProductPreset, Project


def _gap_extender_presets_equal(
    a: ProductPreset, b: ProductPreset,
) -> bool:
    """比较两个 Gap Extender ProductPreset 是否除 length 和 name 外完全相同。"""
    return (
        a.connector_male == b.connector_male
        and a.connector_female == b.connector_female
        and a.cu_wire == b.cu_wire
        and a.label == b.label
    )


def _add_gap_extender_product_with_dedup(
    products: list[AssembledProduct],
    candidate: AssembledProduct,
    preset: ProductPreset,
    quantity: int,
) -> None:
    """将 gap extender 产品加入列表，若存在除 code/quantity 外完全相同的产品，则给旧产品 quantity 累加。"""
    for existing in products:
        existing_preset = getattr(existing, "_gap_extender_preset", None)
        if existing_preset is not None and _gap_extender_presets_equal(existing_preset, preset):
            existing.quantity += quantity
            return
    candidate._gap_extender_preset = preset  # type: ignore[attr-defined]
    candidate.quantity = quantity
    products.append(candidate)


def _build_gap_extender_description(preset: ProductPreset) -> str:
    """根据 Gap Extender ProductPreset 构建产品描述。

    Args:
        preset: Gap Extender 产品预设。

    Returns:
        产品描述字符串。
    """
    wiresize_digits = _get_digits(preset.cu_wire.wiresize)
    strands_digits = _get_digits(preset.cu_wire.strands)
    return (
        f'{preset.label} - {wiresize_digits}Cu, Black, '
        f'{strands_digits}Strands, '
        f'{preset.connector_male.type} M/{preset.connector_female.type} F'
    )


def _make_gap_extender_product(
    parts: list[Part],
    preset: ProductPreset,
    project: Project,
) -> AssembledProduct:
    """生成单个 Gap Extender 产品。

    - 仅生成负极（NEG）产品：第一个连接器 Male（`connector_male`）、
      最后一个连接器 Female（`connector_female`）
    - 导线使用 Gap_Extender 变种（cad_parts_no=extender-NEG, cad_parts_length=440, code 末尾加 W）
    - 标签：固定 CAD 编号 "Harness Label Crosswise Left"，标签明细存入 label_detail

    Args:
        parts: 全局部件列表。
        preset: Gap Extender 产品预设。
        project: 当前项目（用于 label 前缀）。

    Returns:
        生成的 AssembledProduct（极性为 NEG）。
    """
    product = AssembledProduct(polarity="NEG")

    # 1. Male 连接器（第一个连接器）
    if preset.connector_male.type != "none":
        product.parts.append(
            _make_connect_part(parts, "M", preset.connector_male)
        )
    # 2. 标签：固定 CAD 编号，标签明细存入 label_detail
    label_text = project.name + "," + preset.label
    product.parts.append(
        _make_label_part(parts, "Harness Label Crosswise Left", "Label(Product Type Label)", label_text)
    )
    # 3. 导线（Gap_Extender 变种，黑色）
    product.parts.append(
        _make_wire_part(parts, preset.cu_wire, "Gap_Extender", "Black", preset.length)
    )
    # 4. Female 连接器（最后一个连接器）
    if preset.connector_female.type != "none":
        product.parts.append(
            _make_connect_part(parts, "F", preset.connector_female)
        )

    # 构建产品描述
    product.description = _build_gap_extender_description(preset)
    product.pv_panel = preset.pv_panel

    return product

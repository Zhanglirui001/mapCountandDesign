"""Homerun Extender 产品族生成逻辑。

Homerun Extender 是 IBEX 产品在 branch_count=1 时的特例，
拥有独立的外部主线生成逻辑。
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ...models.assembled_product import AssembledProduct
from ...models.length import Length
from ...models.part import Part
from .parts_factory import (
    _make_connect_part,
    _make_cu_al_part,
    _make_fuse_part,
    _make_label_part,
    _make_wire_part,
)

if TYPE_CHECKING:
    from ...models import ProductPreset


def _need_al_wire(length: Length, threshold: Length) -> bool:
    """判断是否需要铝线"""
    return length >= threshold


def _build_homerun_extender_description(
    preset: ProductPreset,
    polarity: str,
    has_al: bool,
    need_al: bool,
) -> str:
    """根据 ProductPreset 和极性构建 Homerun Extender 产品描述。

    Args:
        preset: 产品预设。
        polarity: 极性，"POS" 或 "NEG"。
        has_al: 预设是否配置了铝线（external_al_wire.wiresize != "none"）。
        need_al: 该极性是否需要使用铝线。

    Returns:
        产品描述字符串。
    """
    from .codegen import _get_digits

    description = ""

    if polarity == "POS":
        # 线材规格 + 颜色
        if has_al and need_al:
            description += (
                f"{_get_digits(preset.external_al_wire.wiresize):02d}"
                + "Al&"
                + f"{_get_digits(preset.external_cu_wire.wiresize):02d}"
                + "Cu,Red,"
            )
        else:
            description += (
                f"{_get_digits(preset.external_cu_wire.wiresize):02d}Cu,Red,"
            )

        # Fuse（仅正极）
        if preset.external_fuse.current != "none":
            description += preset.external_fuse.current + "A Fuse,"

        # Strands
        if has_al and need_al:
            description += (
                preset.external_al_wire.strands
                + "/"
                + preset.external_cu_wire.strands
                + "Strands,"
            )
        else:
            description += preset.external_cu_wire.strands + "Strands,"

        # 外部连接器
        if preset.external_connector.type != "none":
            description += preset.external_connector.type + "F/ "
        else:
            description += "Blunt Cut F/"
        # 支线连接器
        if preset.branch_connector.type != "none":
            description += preset.branch_connector.type + " M"
        else:
            description += "Blunt Cut M"
    else:
        # 线材规格 + 颜色
        if has_al and need_al:
            description += (
                f"{_get_digits(preset.external_al_wire.wiresize):02d}"
                + "Al&"
                + f"{_get_digits(preset.external_cu_wire.wiresize):02d}"
                + "Cu,Black,"
            )
        else:
            description += (
                f"{_get_digits(preset.external_cu_wire.wiresize):02d}Cu,Black,"
            )

        # Strands
        if has_al and need_al:
            description += (
                preset.external_al_wire.strands
                + "/"
                + preset.external_cu_wire.strands
                + "Strands,"
            )
        else:
            description += preset.external_cu_wire.strands + "Strands,"

        # 外部连接器
        if preset.external_connector.type != "none":
            description += preset.external_connector.type + "M/ "
        else:
            description += "Blunt Cut M/"
        # 支线连接器
        if preset.branch_connector.type != "none":
            description += preset.branch_connector.type + " F"
        else:
            description += "Blunt Cut F"

    return description


def _make_homerun_extender_product(
    parts: list[Part],
    preset: ProductPreset,
    whip_length: Length,
    extender_length: Length,
) -> tuple[AssembledProduct | None, AssembledProduct | None]:
    """构建 Homerun Extender 产品（正/负一对）。

    产品结构（cual=fuse=0/1 组合）：
    ```
    cual    fuse    product
    0       0       extender-cu(whip+mainline)-label1-label2-[connector]
    0       1       cu-label1-fuse-cu-label2-[connector]
    1       0       cu-label1-cual-al-cual-cu-label2-[connector]
    1       1       cu-label1-fuse-cual-al-cual-cu-label2-[connector]
    ```
    """
    pos_product = AssembledProduct(polarity="POS")
    neg_product = AssembledProduct(polarity="NEG")
    external_prefix_length = (
        whip_length
        + preset.external_cu_length_to_row_head
        + preset.external_extension_trench_length
    )
    positive_mainline_length = (
        preset.branch_lines[0].coordinate_positive + extender_length
    )
    negative_mainline_length = (
        preset.branch_lines[0].coordinate_negative + extender_length
    )
    has_external_fuse = preset.external_fuse.current != "none"
    has_al = preset.external_al_wire.wiresize != "none"
    need_al_pos = (
        len(preset.branch_lines) > 0
        and _need_al_wire(
            positive_mainline_length,
            preset.external_al_threshold,
        )
    )
    need_al_neg = (
        len(preset.branch_lines) > 0
        and _need_al_wire(
            negative_mainline_length,
            preset.external_al_threshold,
        )
    )
    use_prefix_for_fuse_suffix = (
        has_external_fuse
        and positive_mainline_length <= Length("0mm")
    )
    positive_prefix_length = external_prefix_length
    if use_prefix_for_fuse_suffix:
        positive_prefix_length = (
            positive_prefix_length - preset.external_fuse_suffix
        )
    positive_has_post_label_wire = (
        positive_mainline_length > Length("0mm")
        or (
            use_prefix_for_fuse_suffix
            and preset.external_fuse_suffix > Length("0mm")
        )
    )
    positive_prefix_wire_type = (
        "normal" if positive_has_post_label_wire else "Homerun_Extender"
    )
    negative_prefix_wire_type = (
        "normal"
        if negative_mainline_length > Length("0mm")
        else "Homerun_Extender"
    )
    positive_combined_length = external_prefix_length + positive_mainline_length
    negative_combined_length = external_prefix_length + negative_mainline_length
    merge_positive_copper_segments = (
        not has_external_fuse
        and positive_mainline_length > Length("0mm")
        and not (has_al and need_al_pos)
        and positive_combined_length > Length("0mm")
    )
    # 负极产品本身始终没有保险丝，因此只需判断是否实际存在铜铝转换。
    merge_negative_copper_segments = (
        negative_mainline_length > Length("0mm")
        and not (has_al and need_al_neg)
        and negative_combined_length > Length("0mm")
    )

    # 1. 外部连接器
    if preset.external_connector.type != "none":
        pos_product.parts.append(
            _make_connect_part(parts, "F", preset.external_connector)
        )
        neg_product.parts.append(
            _make_connect_part(parts, "M", preset.external_connector)
        )

    # 2. 外部铜线前缀：Whip + 到 row head 的铜线 + trench 延长。
    # 后两项无论是否存在 Whip 都必须计入。若左标签后完全没有主线 Wire，
    # 该 Whip-only 导线按 Homerun Extender 类型生成，使其获得 EP/EN 编号，
    # 供下游程序在代表分表中使用 LENGTH 写法。
    if merge_positive_copper_segments:
        pos_product.parts.append(
            _make_wire_part(
                parts,
                preset.external_cu_wire,
                "Homerun_Extender",
                "Red",
                positive_combined_length,
            )
        )
    elif positive_prefix_length > Length("0mm"):
        pos_product.parts.append(
            _make_wire_part(
                parts,
                preset.external_cu_wire,
                positive_prefix_wire_type,
                "Red",
                positive_prefix_length,
            )
        )
    if merge_negative_copper_segments:
        neg_product.parts.append(
            _make_wire_part(
                parts,
                preset.external_cu_wire,
                "Homerun_Extender",
                "Black",
                negative_combined_length,
            )
        )
    elif external_prefix_length > Length("0mm"):
        neg_product.parts.append(
            _make_wire_part(
                parts,
                preset.external_cu_wire,
                negative_prefix_wire_type,
                "Black",
                external_prefix_length,
            )
        )

    # 3. 左侧标签
    pos_product.parts.append(
        _make_label_part(
            parts,
            "Harness Label Crosswise Left",
            "Label(Product Type Label)",
        )
    )
    neg_product.parts.append(
        _make_label_part(
            parts,
            "Harness Label Crosswise Left",
            "Label(Product Type Label)",
        )
    )

    # 4. 外部保险丝（仅正极）
    if has_external_fuse:
        pos_product.parts.append(
            _make_fuse_part(parts, preset.external_fuse)
        )
        # 无下游主线 L 时，仍需保留保险丝后的铜线 F；为保持总长不变，
        # F 从保险丝前的外部铜线前缀 P 中扣除。
        if (
            use_prefix_for_fuse_suffix
            and preset.external_fuse_suffix > Length("0mm")
        ):
            pos_product.parts.append(
                _make_wire_part(
                    parts,
                    preset.external_cu_wire,
                    "Homerun_Extender",
                    "Red",
                    preset.external_fuse_suffix,
                )
            )

    # 5. 主线（铜线或铜铝混合）
    # coordinate + extender 为零或负数时，没有可生成的下游主线。
    # 同时跳过铜铝转换器，避免留下没有实际线段的转换结构。
    if (
        positive_mainline_length > Length("0mm")
        and not merge_positive_copper_segments
    ):
        if has_al and need_al_pos:
            pos_product.uses_aluminum_wire = True
            pos_product.parts.extend([
                _make_cu_al_part(parts),
                _make_wire_part(
                    parts, preset.external_al_wire, "Homerun_Extender", "Red",
                    positive_mainline_length - preset.branch_second_cual_suffix,
                ),
                _make_cu_al_part(parts),
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Red",
                    preset.branch_second_cual_suffix,
                ),
            ])
        else:
            pos_product.parts.append(
                _make_wire_part(
                    parts, preset.external_cu_wire, "Homerun_Extender", "Red",
                    positive_mainline_length,
                )
            )

    if (
        negative_mainline_length > Length("0mm")
        and not merge_negative_copper_segments
    ):
        if has_al and need_al_neg:
            neg_product.uses_aluminum_wire = True
            neg_product.parts.extend([
                _make_cu_al_part(parts),
                _make_wire_part(
                    parts, preset.external_al_wire, "Homerun_Extender", "Black",
                    negative_mainline_length - preset.branch_second_cual_suffix,
                ),
                _make_cu_al_part(parts),
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Black",
                    preset.branch_second_cual_suffix,
                ),
            ])
        else:
            neg_product.parts.append(
                _make_wire_part(
                    parts, preset.external_cu_wire, "Homerun_Extender", "Black",
                    negative_mainline_length,
                )
            )

    # 6. 第二个标签：始终紧邻支线连接器；没有支线连接器时就是最后一个部件。
    pos_product.parts.append(
        _make_label_part(
            parts,
            "Harness Label Crosswise Right",
            "Label(Circuit Label)",
        )
    )
    neg_product.parts.append(
        _make_label_part(
            parts,
            "Harness Label Crosswise Right",
            "Label(Circuit Label)",
        )
    )

    # 7. 支线连接器
    if preset.branch_connector.type != "none":
        branch_connector = replace(
            preset.branch_connector,
            size=replace(
                preset.branch_connector.size,
                wiresize=preset.external_cu_wire.wiresize,
            ),
        )
        pos_product.parts.append(
            _make_connect_part(parts, "M", branch_connector)
        )
        neg_product.parts.append(
            _make_connect_part(parts, "F", branch_connector)
        )

    # 产品描述
    pos_product.description = _build_homerun_extender_description(preset, "POS", has_al, need_al_pos)
    neg_product.description = _build_homerun_extender_description(preset, "NEG", has_al, need_al_neg)
    pos_product.pv_panel = preset.pv_panel
    neg_product.pv_panel = preset.pv_panel

    return pos_product, neg_product

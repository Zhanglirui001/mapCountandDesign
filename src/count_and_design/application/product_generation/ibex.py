"""IBEX 产品族生成逻辑。"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ...models.assembled_product import AssembledProduct
from ...models.length import Length
from ...models.part import Part
from .dedup import _find_duplicate_semi_part
from .codegen import _get_digits
from .parts_factory import (
    _make_connect_part,
    _make_cu_al_part,
    _make_fuse_part,
    _make_kit_part,
    _make_label_part,
    _make_wire_part,
)

if TYPE_CHECKING:
    from ...models import IBEXBranchCoordinate, ProductPreset



def _build_ibex_description(preset: ProductPreset, polarity: str) -> str:
    """根据 IBEX ProductPreset 和极性构建产品描述。

    Args:
        preset: IBEX 产品预设。
        polarity: 极性，"POS" 或 "NEG"。

    Returns:
        产品描述字符串。
    """
    if polarity == "POS":
        if preset.branch_fuse.current != "none" and preset.external_fuse.current != "none":
            return (
                str(preset.branch_count)
                + "-String IBEX "
                + str(_get_digits(preset.branch_fuse.current))
                + "&"
                + str(_get_digits(preset.external_fuse.current))
                + "A Inline Fuse Red"
            )
        elif preset.branch_fuse.current == "none" and preset.external_fuse.current != "none":
            return (
                str(preset.branch_count)
                + "-String IBEX "
                + str(_get_digits(preset.external_fuse.current))
                + "A Inline Fuse Red"
            )
        elif preset.branch_fuse.current != "none" and preset.external_fuse.current == "none":
            return (
                str(preset.branch_count)
                + "-String IBEX "
                + str(_get_digits(preset.branch_fuse.current))
                + "A Inline Fuse Red"
            )
        else:
            return (
                str(preset.branch_count)
                + "-String IBEX "
                + "Unfused Red"
            )

    else:
        return str(preset.branch_count) + "-String IBEX " + "Unfused Black"


# IBEX 支线半成品的 direction / overmolding_detail 映射

def _apply_ibex_branch_semi_attributes(semi_part: Part) -> None:
    """为 IBEX 支线半成品设置 direction_nos 与 overmolding_detail。

    - direction_nos：存储在父级（semi_part）上，按 semi_list 中 semi_or_not=True
      的子部件顺序排列。direction 表示子部件在父部件中的安装位置，
      属于父-子关系属性而非子部件自身属性，故存储在父级而非写入子部件。
    - overmolding_detail：设置给 semi_list 中的第一个部件（T Kit）

    规则：
    - 1 个 semi 子部件 → direction_nos=[1],     overmolding_detail="1&0"
    - 2 个 semi 子部件 → direction_nos=[1,2],   overmolding_detail="1&1"
    """
    semi_items = [p for p in semi_part.semi_list if p.semi_or_not]
    count = len(semi_items)

    if count == 0:
        return

    # direction_nos（存储在父级，按 semi 子部件顺序排列）
    if count == 1:
        semi_part.direction_nos = [1]
    elif count == 2:
        semi_part.direction_nos = [1, 2]

    # overmolding_detail（设置给 semi_list 中第一个部件，即 T Kit）
    # 使用 replace 克隆，避免修改被多个 semi 共享的 Part 对象
    _IBEX_ODM_MAP = {1: "1&0", 2: "1&1", 3: "2&1", 4: "2&2",
                     5: "4&1", 6: "4&2", 7: "4&3", 8: "4&4"}
    odm = _IBEX_ODM_MAP.get(count, "")
    if semi_part.semi_list and odm:
        semi_part.semi_list[0] = replace(semi_part.semi_list[0], overmolding_detail=odm)



def _make_ibex_branch_semi(
    semi_parts: list[Part],
    parts: list[Part],
    preset: ProductPreset,
) -> tuple[Part, Part]:
    """生成 IBEX 支线的正/负半成品。

    结构：
        外层 semi (pos_part / neg_part)
        ├── T Kit
        └── 内层 branch semi
            ├── Wire（正极含 fuse, 负极不含）
            ├── Fuse（仅正极）
            ├── Wire
            └── Connect
    """
    # ------------------------------------------------------------------
    # 内层 branch semi — 正极
    # ------------------------------------------------------------------
    pos_inner = Part()
    pos_inner.semi_or_not = True
    if preset.branch_fuse.current != "none":
        pos_inner.semi_list.extend([
            _make_wire_part(
                parts, preset.branch_cu_wire, "normal", "Red",
                preset.branch_fuse_prefix,
            ),
            _make_fuse_part(parts, preset.branch_fuse),
            _make_wire_part(
                parts, preset.branch_cu_wire, "normal", "Red",
                preset.branch_length_positive - preset.branch_fuse_prefix,
            ),
        ])
    else:
        pos_inner.semi_list.append(
            _make_wire_part(
                parts, preset.branch_cu_wire, "normal", "Red",
                preset.branch_length_positive,
            )
        )
    if preset.branch_connector.type != "none":
        pos_inner.semi_list.append(
            _make_connect_part(parts, "M", preset.branch_connector)
        )

    existing_pos_inner = _find_duplicate_semi_part(semi_parts, pos_inner)
    if existing_pos_inner is not None:
        pos_inner = existing_pos_inner
    else:
        semi_parts.append(pos_inner)

    # ------------------------------------------------------------------
    # 内层 branch semi — 负极
    # ------------------------------------------------------------------
    neg_inner = Part()
    neg_inner.semi_or_not = True
    neg_inner.semi_list.append(
        _make_wire_part(
            parts, preset.branch_cu_wire, "normal", "Black",
            preset.branch_length_negative,
        )
    )
    if preset.branch_connector.type != "none":
        neg_inner.semi_list.append(
            _make_connect_part(parts, "F", preset.branch_connector)
        )

    existing_neg_inner = _find_duplicate_semi_part(semi_parts, neg_inner)
    if existing_neg_inner is not None:
        neg_inner = existing_neg_inner
    else:
        semi_parts.append(neg_inner)

    # ------------------------------------------------------------------
    # 外层 semi
    # ------------------------------------------------------------------
    pos_part = Part()
    pos_part.semi_or_not = True
    pos_part.semi_list.append(_make_kit_part(parts, "T"))
    pos_part.semi_list.append(pos_inner)

    neg_part = Part()
    neg_part.semi_or_not = True
    neg_part.semi_list.append(_make_kit_part(parts, "T"))
    neg_part.semi_list.append(neg_inner)

    # 在去重前设置 direction / overmolding_detail
    _apply_ibex_branch_semi_attributes(pos_part)
    _apply_ibex_branch_semi_attributes(neg_part)

    # 去重：若已有相同的 semi part，复用已有
    existing_pos = _find_duplicate_semi_part(semi_parts, pos_part)
    if existing_pos is not None:
        pos_part = existing_pos
    else:
        semi_parts.append(pos_part)

    existing_neg = _find_duplicate_semi_part(semi_parts, neg_part)
    if existing_neg is not None:
        neg_part = existing_neg
    else:
        semi_parts.append(neg_part)

    return pos_part, neg_part


def _need_al_wire(len: Length, threshold: Length) -> bool:
    """判断是否需要铝线"""
    return len >= threshold


def _subtract_lengths(total: Length, *subtrahends: Length) -> Length:
    """按毫米计算长度差，并消除浮点运算产生的近零残差。"""
    result_mm = total.to_mm() - sum(value.to_mm() for value in subtrahends)
    if abs(result_mm) < 1e-9:
        result_mm = 0.0
    return Length(f"{result_mm:.12f}mm", registry=total.registry)


def _make_ibex_external(
    parts: list[Part],
    preset: ProductPreset,
    whip_length: Length,
    extender_length: Length,
    pos_product: AssembledProduct,
    neg_product: AssembledProduct,
) -> None:
    """生成 IBEX 外部主线部件。
    fuse    al_size  gap_extender	
    0       0        0        conn+cu+L+L
    0       0        1        conn+cu+L+L
    0       1        0        conn+cu+L+L
    0       1        1        conn+cu+L+L+G+E+G+cu
    1       0        0        conn+cu+L+L+fu+cu
    1       0        1        conn+cu+L+L+fu+cu
    1       1        0        conn+cu+L+L+fu+cu
    1       1        1        conn+cu+L+L+fu+cu+G+E+G+cu
    """
    external_prefix_length = (
        whip_length
        + preset.external_cu_length_to_row_head
        + preset.external_extension_trench_length
    )
    # 铜铝切换只判断 coordinate + extender；外部铜线前缀始终保持为铜。
    need_al_pos = (
        len(preset.branch_lines) > 0
        and _need_al_wire(
            preset.branch_lines[0].coordinate_positive + extender_length,
            preset.external_al_threshold,
        )
    )
    need_al_neg = (
        len(preset.branch_lines) > 0
        and _need_al_wire(
            preset.branch_lines[0].coordinate_negative + extender_length,
            preset.external_al_threshold,
        )
    )
    has_al_wire = preset.external_al_wire.wiresize != "none"
    has_fuse = preset.external_fuse.current != "none"
    has_connector = preset.external_connector.type != "none"

    # 连接器
    if has_connector:
        pos_product.parts.append(
            _make_connect_part(parts, "F", preset.external_connector)
        )
        neg_product.parts.append(
            _make_connect_part(parts, "M", preset.external_connector)
        )
    
    if not has_fuse:
        if not(has_al_wire and need_al_pos):
            pos_product.parts.extend([
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Red",
                    external_prefix_length + extender_length + preset.branch_lines[0].coordinate_positive,
                ),
                _make_label_part(parts, "Harness Label Crosswise Left", "Label(Product Type Label)"),
                _make_label_part(parts, "Harness Label Crosswise Right", "Label(Circuit Label)"),
            ])
        else:
            pos_product.uses_aluminum_wire = True
            pos_product.parts.extend([
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Red",
                    external_prefix_length,
                ),
                _make_label_part(parts, "Harness Label Crosswise Left", "Label(Product Type Label)"),
                _make_label_part(parts, "Harness Label Crosswise Right", "Label(Circuit Label)"),
                _make_cu_al_part(parts),
                _make_wire_part(
                    parts, preset.external_al_wire, "normal", "Red",
                    extender_length
                    + preset.branch_lines[0].coordinate_positive
                    - preset.external_second_cual_suffix,
                ),
                _make_cu_al_part(parts),
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Red",
                    preset.external_second_cual_suffix,
                ),
            ])

        if not(has_al_wire and need_al_neg):
            neg_product.parts.extend([
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Black",
                    external_prefix_length + extender_length + preset.branch_lines[0].coordinate_negative,
                ),
                _make_label_part(parts, "Harness Label Crosswise Left", "Label(Product Type Label)"),
                _make_label_part(parts, "Harness Label Crosswise Right", "Label(Circuit Label)"),
            ])
        else:
            neg_product.uses_aluminum_wire = True
            neg_product.parts.extend([
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Black",
                    external_prefix_length,
                ),
                _make_label_part(parts, "Harness Label Crosswise Left", "Label(Product Type Label)"),
                _make_label_part(parts, "Harness Label Crosswise Right", "Label(Circuit Label)"),
                _make_cu_al_part(parts),
                _make_wire_part(
                    parts, preset.external_al_wire, "normal", "Black",
                    extender_length
                    + preset.branch_lines[0].coordinate_negative
                    - preset.external_second_cual_suffix,
                ),
                _make_cu_al_part(parts),
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Black",
                    preset.external_second_cual_suffix,
                ),
            ])
    else:
        if not(has_al_wire and need_al_pos):
            pos_product.parts.extend([
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Red",
                    external_prefix_length
                ),
                _make_label_part(parts, "Harness Label Crosswise Left", "Label(Product Type Label)"),
                _make_label_part(parts, "Harness Label Crosswise Right", "Label(Circuit Label)"),
                _make_fuse_part(parts, preset.external_fuse),
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Red",
                    preset.external_fuse_suffix + extender_length + preset.branch_lines[0].coordinate_positive,
                ),
            ])
        else:
            pos_product.uses_aluminum_wire = True
            pos_product.parts.extend([
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Red",
                    external_prefix_length
                ),
                _make_label_part(parts, "Harness Label Crosswise Left", "Label(Product Type Label)"),
                _make_label_part(parts, "Harness Label Crosswise Right", "Label(Circuit Label)"),
                _make_fuse_part(parts, preset.external_fuse),
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Red",
                    preset.external_fuse_suffix,
                ),
                _make_cu_al_part(parts),
                _make_wire_part(
                    parts, preset.external_al_wire, "normal", "Red",
                    extender_length + preset.branch_lines[0].coordinate_positive - preset.external_second_cual_suffix,
                ),
                _make_cu_al_part(parts),
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Red",
                    preset.external_second_cual_suffix,
                ),
            ])

        if not(has_al_wire and need_al_neg):
            neg_product.parts.extend([
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Black",
                    external_prefix_length + extender_length + preset.branch_lines[0].coordinate_negative,
                ),
                _make_label_part(parts, "Harness Label Crosswise Left", "Label(Product Type Label)"),
                _make_label_part(parts, "Harness Label Crosswise Right", "Label(Circuit Label)"),
            ])
        else:
            neg_product.uses_aluminum_wire = True
            neg_product.parts.extend([
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Black",
                    external_prefix_length,
                ),
                _make_label_part(parts, "Harness Label Crosswise Left", "Label(Product Type Label)"),
                _make_label_part(parts, "Harness Label Crosswise Right", "Label(Circuit Label)"),
                _make_cu_al_part(parts),
                _make_wire_part(
                    parts, preset.external_al_wire, "normal", "Black",
                    _subtract_lengths(
                        extender_length
                        + preset.branch_lines[0].coordinate_negative,
                        preset.external_second_cual_suffix,
                    ),
                ),
                _make_cu_al_part(parts),
                _make_wire_part(
                    parts, preset.external_cu_wire, "normal", "Black",
                    preset.external_second_cual_suffix,
                ),
            ])

def _make_ibex_internal(
    parts: list[Part],
    preset: ProductPreset,
    pos_product: AssembledProduct,
    neg_product: AssembledProduct,
    branch_previous: IBEXBranchCoordinate,
    branch_this: IBEXBranchCoordinate,
) -> None:
    """生成 IBEX 内部主线（相邻支线之间的跨接线）。
    has_al      need_al
    0           0       cu
    0           1       cu
    1           0       cu
    1           1       cu+G+E+G+cu

    是否转接由内部主线阈值 internal_al_threshold 决定（相邻支线坐标跨度），
    与外部主线阈值 external_al_threshold 相互独立。
    """
    has_al = preset.internal_al_wire.wiresize != "none"
    need_al_pos = (
        len(preset.branch_lines) > 0
        and _need_al_wire(branch_this.coordinate_positive - branch_previous.coordinate_positive, preset.internal_al_threshold)
    )
    need_al_neg = (
        len(preset.branch_lines) > 0
        and _need_al_wire(branch_this.coordinate_negative - branch_previous.coordinate_negative, preset.internal_al_threshold)
    )
    
    if has_al and need_al_pos:
        pos_product.uses_aluminum_wire = True
        pos_product.parts.extend([
            _make_wire_part(
                parts, preset.internal_cu_wire, "normal", "Red",
                preset.internal_first_cual_prefix,
            ),
            _make_cu_al_part(parts),
            _make_wire_part(
                parts, preset.internal_al_wire, "normal", "Red",
                branch_this.coordinate_positive
                - branch_previous.coordinate_positive
                - preset.internal_first_cual_prefix
                - preset.internal_second_cual_suffix,
            ),
            _make_cu_al_part(parts),
            _make_wire_part(
                parts, preset.internal_cu_wire, "normal", "Red",
                preset.internal_second_cual_suffix,
            ),
        ])
    else:
        pos_product.parts.append(
            _make_wire_part(
                parts, preset.internal_cu_wire, "normal", "Red",
                branch_this.coordinate_positive
                - branch_previous.coordinate_positive,
            )
        )
        
    if has_al and need_al_neg:
        neg_product.uses_aluminum_wire = True
        neg_product.parts.extend([
            _make_wire_part(
                parts, preset.internal_cu_wire, "normal", "Black",
                preset.internal_first_cual_prefix,
            ),
            _make_cu_al_part(parts),
            _make_wire_part(
                parts, preset.internal_al_wire, "normal", "Black",
                branch_this.coordinate_negative
                - branch_previous.coordinate_negative
                - preset.internal_first_cual_prefix
                - preset.internal_second_cual_suffix,
            ),
            _make_cu_al_part(parts),
            _make_wire_part(
                parts, preset.internal_cu_wire, "normal", "Black",
                preset.internal_second_cual_suffix,
            ),
        ])
    else:
        neg_product.parts.append(
            _make_wire_part(
                parts, preset.internal_cu_wire, "normal", "Black",
                branch_this.coordinate_negative
                - branch_previous.coordinate_negative,
            )
        )


def _make_ibex_last_branch(
    parts: list[Part],
    preset: ProductPreset,
    pos_product: AssembledProduct,
    neg_product: AssembledProduct,
) -> None:
    """生成 IBEX 最后一条支线（末端支线）的末端部件。

    末端支线使用 preset 的 last_branch_* 属性（线号与保险丝）；
    未单独配置时回退到一般支线（branch_*）的对应属性。
    """
    last = preset.branch_lines[-1]
    second_last = preset.branch_lines[-2]
    last_cu = preset.effective_last_branch_cu_wire
    last_al = preset.effective_last_branch_al_wire
    last_fuse = preset.effective_last_branch_fuse
    positive_span = last.coordinate_positive - second_last.coordinate_positive
    negative_span = last.coordinate_negative - second_last.coordinate_negative
    has_last_al_wire = last_al.wiresize != "none"
    use_al_pos = has_last_al_wire and _need_al_wire(
        positive_span, preset.last_branch_al_threshold,
    )
    use_al_neg = has_last_al_wire and _need_al_wire(
        negative_span, preset.last_branch_al_threshold,
    )

    # 末端支线可以使用独立线号，因此连接器也必须使用实际末端铜线线号。
    last_connector = replace(
        preset.branch_connector,
        size=replace(
            preset.branch_connector.size,
            wiresize=last_cu.wiresize,
        ),
    )

    def _append_labels_and_connector(
        product: AssembledProduct, gender: str,
    ) -> None:
        product.parts.extend([
            _make_label_part(
                parts, "Harness Label Crosswise Left",
                "Label(Product Type Label)",
            ),
            _make_label_part(
                parts, "Harness Label Crosswise Right",
                "Label(Circuit Label)",
            ),
        ])
        if last_connector.type != "none":
            product.parts.append(
                _make_connect_part(parts, gender, last_connector)
            )

    positive_total = positive_span
    negative_total = negative_span
    if preset.last_branch_with_extra_length:
        positive_total = positive_total + preset.branch_length_positive
        negative_total = negative_total + preset.branch_length_negative

    if use_al_pos:
        pos_product.uses_aluminum_wire = True
        if last_fuse.current != "none":
            pos_product.parts.extend([
                _make_wire_part(
                    parts, last_cu, "normal", "Red",
                    preset.branch_fuse_prefix,
                ),
                _make_fuse_part(parts, last_fuse),
            ])
            positive_al_length = _subtract_lengths(
                positive_total,
                preset.branch_fuse_prefix,
                preset.branch_first_cual_prefix,
                preset.branch_second_cual_suffix,
            )
        else:
            # 未使用保险丝时不存在 fuse prefix，不应从末端总长中扣除。
            positive_al_length = _subtract_lengths(
                positive_total,
                preset.branch_first_cual_prefix,
                preset.branch_second_cual_suffix,
            )
        pos_product.parts.extend([
            _make_wire_part(
                parts, last_cu, "normal", "Red",
                preset.branch_first_cual_prefix,
            ),
            _make_cu_al_part(parts),
            _make_wire_part(
                parts, last_al, "normal", "Red", positive_al_length,
            ),
            _make_cu_al_part(parts),
            _make_wire_part(
                parts, last_cu, "normal", "Red",
                preset.branch_second_cual_suffix,
            ),
        ])
    else:
        if last_fuse.current != "none":
            pos_product.parts.extend([
                _make_wire_part(
                    parts, last_cu, "normal", "Red",
                    preset.branch_fuse_prefix,
                ),
                _make_fuse_part(parts, last_fuse),
                _make_wire_part(
                    parts, last_cu, "normal", "Red",
                    _subtract_lengths(
                        positive_total, preset.branch_fuse_prefix,
                    ),
                ),
            ])
        else:
            pos_product.parts.append(
                _make_wire_part(
                    parts, last_cu, "normal", "Red", positive_total,
                )
            )
    _append_labels_and_connector(pos_product, "M")

    if use_al_neg:
        neg_product.uses_aluminum_wire = True
        neg_product.parts.extend([
            _make_wire_part(
                parts, last_cu, "normal", "Black",
                preset.branch_first_cual_prefix,
            ),
            _make_cu_al_part(parts),
            _make_wire_part(
                parts, last_al, "normal", "Black",
                _subtract_lengths(
                    negative_total,
                    preset.branch_first_cual_prefix,
                    preset.branch_second_cual_suffix,
                ),
            ),
            _make_cu_al_part(parts),
            _make_wire_part(
                parts, last_cu, "normal", "Black",
                preset.branch_second_cual_suffix,
            ),
        ])
    else:
        neg_product.parts.append(
            _make_wire_part(
                parts, last_cu, "normal", "Black", negative_total,
            )
        )
    _append_labels_and_connector(neg_product, "F")


def _make_ibex_product(
    semi_parts: list[Part],
    parts: list[Part],
    preset: ProductPreset,
    whip_length: Length,
    extender_length: Length,
) -> tuple[AssembledProduct | None, AssembledProduct | None]:
    """构建 IBEX 产品（正/负一对）。"""
    pos_product = AssembledProduct(polarity="POS")
    neg_product = AssembledProduct(polarity="NEG")
    _make_ibex_external(
        parts, preset, whip_length, extender_length, pos_product, neg_product,
    )
    if len(preset.branch_lines) <= 1:
        return None, None
    for i in range(len(preset.branch_lines) - 1):
        pos_branch, neg_branch = _make_ibex_branch_semi(
            semi_parts, parts, preset,
        )
        pos_product.parts.append(pos_branch)
        neg_product.parts.append(neg_branch)
        # 最后一次迭代（倒数第二条→末条支线）的内部主线由 _make_ibex_last_branch 处理，此处跳过
        if i < len(preset.branch_lines) - 2:
            _make_ibex_internal(
                parts, preset, pos_product, neg_product,
                preset.branch_lines[i], preset.branch_lines[i + 1],
            )
    _make_ibex_last_branch(parts, preset, pos_product, neg_product)
    pos_product.description = _build_ibex_description(preset, "POS")
    neg_product.description = _build_ibex_description(preset, "NEG")
    pos_product.pv_panel = preset.pv_panel
    neg_product.pv_panel = preset.pv_panel
    return pos_product, neg_product


"""LYNX 产品族生成逻辑。"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ...models.assembled_product import AssembledProduct
from ...models.length import Length
from ...models.part import Part
from ...models.wire import Wire
from .dedup import _find_duplicate_semi_part
from .parts_factory import (
    _make_connect_part,
    _make_cu_al_part,
    _make_label_part,
    _make_overmold_part,
    _make_wire_part,
    _resolve_product_presets,
)

if TYPE_CHECKING:
    from ...models import Connector, Node, ProductPreset, Project, Row


# =============================================================================
# LYNX 产品归类
# =============================================================================


@dataclass
class LynxCluster:
    """LYNX 产品归类。"""

    class_name: str
    products: list[AssembledProduct]
    descriptor: str
    total_branch_count: int
    polarity: str
    branch_counts: list[int]
    first_branch_wire_digits: str
    mainline_wire_digits: str


def _extract_wire_size_digits(description: str) -> str:
    """从导线描述中提取线号数字部分。

    例如 "10AWG Red PV Wire 2KV ..." → "10"
         "500MCM Red PV Wire 2KV ..." → "500"
    """
    if not description:
        return ""
    first_token = description.split()[0]
    digits = "".join(ch for ch in first_token if ch.isdigit())
    return digits


def _get_overmold_branch_count(overmold_part: Part) -> int:
    """获取 overmold semi part 中的支线数量。

    overmold semi 结构：semi_list[0] = OverMolding part,
    semi_list[1:] = branch semis（每一条支线一个）。
    """
    if not overmold_part.semi_list:
        return 0
    # semi_list[0] 是 OverMolding，其余是 branch semi
    return len(overmold_part.semi_list) - 1


def _get_first_branch_cu_wire_size(product: AssembledProduct) -> str:
    """获取第一个 overmold 的第一条支线的铜线线号数字部分。

    遍历 parts 找到第一个 overmold semi，取其 semi_list[1]（第一条 branch semi），
    在其中查找描述以 "Cu" 结尾的 Wire 部件（铜线），提取线号数字。

    branch semi 结构：Al wire, Cu-Al, Cu wire, Connect。
    导线描述格式：{wiresize} {color} PV Wire 2KV {strands} Strands {material}
    铜线的 material="Cu"，铝线的 material="Al"，通过描述末单词区分。
    """
    for part in product.parts:
        if not part.semi_or_not or not part.semi_list:
            continue
        if part.semi_list[0].category == "OverMolding":
            if len(part.semi_list) > 1:
                branch_semi = part.semi_list[1]
                if branch_semi.semi_list:
                    for sub in branch_semi.semi_list:
                        if sub.category == "Wire" and sub.description:
                            # 描述末单词为材料：Cu=铜线，Al=铝线
                            last_word = sub.description.rstrip().rsplit(" ", 1)[-1]
                            if last_word == "Cu":
                                return _extract_wire_size_digits(sub.description)
            break
    return ""


def _format_length_ft(length_ft: float) -> str:
    """将英尺长度格式化为简洁字符串，去掉末尾多余的零。

    10.0 → "10", 16.5 → "16.5", 16.28 → "16.28"
    """
    rounded = round(length_ft, 4)
    if rounded == int(rounded):
        return f"{int(rounded)}ft"
    # 去掉末尾多余的零
    s = f"{rounded:.4f}".rstrip("0").rstrip(".")
    return f"{s}ft"


def _generate_lynx_descriptor(product: AssembledProduct) -> str:
    """为 LYNX 产品生成描述符。

    格式：{主线线径}-{overmold_1支线数}+{内部主线_1长度}-...-{overmold_n支线数}
    最后一个 overmold 后面没有主线长度。

    例：500MCM-2+16.5ft-4+16.5ft-2+16.5ft-2
    """
    if not product.parts:
        return ""

    # 主线线径：从第一个 part（外部主线）的描述中提取第一个 token（完整线号）
    first_desc = product.parts[0].description
    mainline_wire_size = first_desc.split()[0] if first_desc else ""

    parts_list = product.parts
    # parts 结构：
    # [0] 外部主线 wire
    # [1] overmold semi
    # [2] 内部主线 wire
    # [3] overmold semi
    # ...
    # 最后一个一定是 overmold semi

    result_parts: list[str] = [mainline_wire_size]

    i = 1
    while i < len(parts_list):
        part = parts_list[i]
        if part.semi_or_not and part.semi_list and part.semi_list[0].category == "OverMolding":
            branch_count = _get_overmold_branch_count(part)
            # 下一个 part 如果是内部主线 wire，格式为 -{count}+{length}
            if i + 1 < len(parts_list):
                next_part = parts_list[i + 1]
                if next_part.category == "Wire" and not next_part.semi_or_not:
                    length_ft = next_part.length
                    length_str = _format_length_ft(length_ft)
                    result_parts.append(f"{branch_count}+{length_str}")
                    i += 2
                    continue
            # 最后一个 overmold：只有 -{count}
            result_parts.append(str(branch_count))
        i += 1

    return "-".join(result_parts)


def _get_lynx_branch_counts(product: AssembledProduct) -> list[int]:
    """获取产品中每个 overmold 的支线数量列表。"""
    counts: list[int] = []
    for part in product.parts:
        if part.semi_or_not and part.semi_list and part.semi_list[0].category == "OverMolding":
            counts.append(_get_overmold_branch_count(part))
    return counts


def _lynx_parts_equal_except_first_length(a: AssembledProduct, b: AssembledProduct) -> bool:
    """比较两个 LYNX 产品是否除第一个 part 的 length 外完全相同。"""
    if len(a.parts) != len(b.parts):
        return False
    for idx, (pa, pb) in enumerate(zip(a.parts, b.parts)):
        if idx == 0:
            # 比较第一个 part 时跳过 length 字段
            if not _part_equal_except_field(pa, pb, "length"):
                return False
        else:
            if not _semi_parts_equal_for_cluster(pa, pb):
                return False
    return True


def _part_equal_except_field(a: Part, b: Part, skip_field: str) -> bool:
    """比较两个 Part 是否除指定字段外完全相同（浅层比较，不深入 semi_list）。"""
    scalar_fields = {
        "category", "cad_parts_no", "cad_parts_length",
        "description", "unit", "length", "semi_or_not",
        "overmolding_detail", "label_detail",
    }
    for field in scalar_fields:
        if field == skip_field:
            continue
        if getattr(a, field) != getattr(b, field):
            return False
    # direction_nos
    if a.direction_nos != b.direction_nos:
        return False
    # semi_list 深度比较
    if len(a.semi_list) != len(b.semi_list):
        return False
    for sub_a, sub_b in zip(a.semi_list, b.semi_list):
        if not _semi_parts_equal_for_cluster(sub_a, sub_b):
            return False
    return True


def _semi_parts_equal_for_cluster(a: Part, b: Part) -> bool:
    """深度比较两个 Part 除 code 外的所有字段是否相同（用于聚类）。"""
    scalar_fields = {
        "category", "cad_parts_no", "cad_parts_length",
        "description", "unit", "length", "semi_or_not",
        "overmolding_detail", "label_detail",
    }
    for field in scalar_fields:
        if getattr(a, field) != getattr(b, field):
            return False
    if a.direction_nos != b.direction_nos:
        return False
    if len(a.semi_list) != len(b.semi_list):
        return False
    for sub_a, sub_b in zip(a.semi_list, b.semi_list):
        if not _semi_parts_equal_for_cluster(sub_a, sub_b):
            return False
    return True


def _build_lynx_column(branch_counts: list[int]) -> str:
    """根据支线数量列表构建 LYNX 列字符串。

    将相邻且相同的数字用乘法表示，每节之间用加法连接。
    例如 [2, 2, 3, 3, 4] → "2*2+3*2+4*1"
    """
    if not branch_counts:
        return ""

    compressed: list[tuple[int, int]] = []  # (value, count)
    for count in branch_counts:
        if compressed and compressed[-1][0] == count:
            compressed[-1] = (count, compressed[-1][1] + 1)
        else:
            compressed.append((count, 1))

    return "+".join(f"{val}*{cnt}" for val, cnt in compressed)


def _build_lynx_product_description(total_branch_count: int, polarity: str) -> str:
    """构建 LYNX 产品描述。

    格式：{支线总数}-Input LYNX Plus Unfused {color}
    正极为 Red，负极为 Black。
    """
    color = "Red" if polarity == "POS" else "Black"
    return f"{total_branch_count}-Input LYNX Plus Unfused {color}"


def cluster_lynx_products(products: list[AssembledProduct]) -> list[LynxCluster]:
    """将 LYNX 产品归类。

    除第一个 part 的 length 属性外，所有 part 完全相同的产品归为同一类。
    类名为 {第一个产品的描述符}-Type{i}，避免与已有类名重复。
    """
    if not products:
        return []

    # 分组：使用 tuple 作为 key
    # key 基于除第一个 part length 外的所有 parts 信息
    unassigned = list(products)
    clusters: list[LynxCluster] = []
    used_class_names: set[str] = set()

    while unassigned:
        first = unassigned.pop(0)
        group = [first]

        # 找到所有与 first 同类（极性相同是隐含条件，因为 part 内容不同）
        remaining = []
        for p in unassigned:
            if p.polarity == first.polarity and _lynx_parts_equal_except_first_length(first, p):
                group.append(p)
            else:
                remaining.append(p)
        unassigned = remaining

        # 生成描述符和类名
        descriptor = _generate_lynx_descriptor(first)
        branch_counts = _get_lynx_branch_counts(first)
        total_branch_count = sum(branch_counts)

        # 确定类名，避免重复（正极 P- 前缀，负极 N- 前缀）
        polarity_prefix = "P" if first.polarity == "POS" else "N"
        type_idx = 1
        while True:
            class_name = f"{polarity_prefix}-{descriptor}-Type{type_idx}"
            if class_name not in used_class_names:
                break
            type_idx += 1
        used_class_names.add(class_name)

        clusters.append(LynxCluster(
            class_name=class_name,
            products=group,
            descriptor=descriptor,
            total_branch_count=total_branch_count,
            polarity=first.polarity,
            branch_counts=branch_counts,
            first_branch_wire_digits=_get_first_branch_cu_wire_size(first),
            mainline_wire_digits=_extract_wire_size_digits(first.parts[0].description) if first.parts else "",
        ))

    return clusters


# ---------------------------------------------------------------------------
# LYNX overmold 半成品的 direction / overmolding_detail 映射
# ---------------------------------------------------------------------------

# direction_nos 序列（按 semi 子部件数量）
_LYNX_DIRECTION_MAP: dict[int, list[int]] = {
    1: [1],
    2: [1, 2],
    3: [1, 2, 4],
    4: [1, 2, 3, 4],
    5: [1, 2, 3, 4, 6],
    6: [1, 2, 3, 4, 5, 6],
    7: [1, 2, 3, 4, 5, 7, 8],
    8: [1, 2, 3, 4, 5, 6, 7, 8],
}

# overmolding_detail（首个 semi 子部件）
_LYNX_OVERMOLDING_MAP: dict[int, str] = {
    1: "1&0",
    2: "1&1",
    3: "2&1",
    4: "2&2",
    5: "4&1",
    6: "4&2",
    7: "4&3",
    8: "4&4",
}


def _apply_lynx_overmold_semi_attributes(semi_part: Part) -> None:
    """为 LYNX 注塑半成品设置 direction_nos 与 overmolding_detail。

    - direction_nos：存储在父级（semi_part）上，按 semi_list 中 semi_or_not=True
      的子部件顺序排列。direction 表示子部件在父部件中的安装位置，
      属于父-子关系属性而非子部件自身属性，故存储在父级而非写入子部件。
    - overmolding_detail：设置给 semi_list 中的第一个部件（Overmold part）
    """
    semi_items = [p for p in semi_part.semi_list if p.semi_or_not]
    count = len(semi_items)

    if count == 0:
        return

    # direction_nos（存储在父级，按 semi 子部件顺序排列）
    directions = _LYNX_DIRECTION_MAP.get(
        count, list(range(1, count + 1))
    )
    semi_part.direction_nos = directions[:count]

    # overmolding_detail（设置给 semi_list 中第一个部件，即 Overmold part）
    # 使用 replace 克隆，避免修改被多个 semi 共享的 Part 对象
    odm = _LYNX_OVERMOLDING_MAP.get(count, "")
    if semi_part.semi_list:
        semi_part.semi_list[0] = replace(semi_part.semi_list[0], overmolding_detail=odm)


def _get_lynx_al_cad_parts_length(
    branch_position: int,
    overmold_branch_count: int,
) -> float:
    """根据支线在注塑件中的位置和注塑件类型确定铝线 CAD Parts Length。

    1-6 型注塑件（5-6 条支线）：位置 1、4 使用 115.7824
    1-8 型注塑件（7-8 条支线）：位置 1、4、5、8 使用 115.7824
    其余情况使用默认值 103.8167
    """
    if 5 <= overmold_branch_count <= 6:
        if branch_position in (1, 4):
            return 115.7824
    elif 7 <= overmold_branch_count <= 8:
        if branch_position in (1, 4, 5, 8):
            return 115.7824
    return 103.8167


def _make_lynx_branch_semi(
    semi_parts: list[Part],
    parts: list[Part],
    cu_length: Length,
    preset: ProductPreset,
    connector: Connector,
    branch_position: int = 0,
    overmold_branch_count: int = 0,
) -> tuple[Part, Part]:
    """生成 LYNX 支线的正/负半成品。

    branch_position: 支线在注塑件中的位置（1-based），用于确定铝线 CAD 长度。
    overmold_branch_count: 注塑件包含的支线总数。
    """
    al_cad_length = _get_lynx_al_cad_parts_length(
        branch_position, overmold_branch_count,
    )
    al_cad_override = al_cad_length if al_cad_length != 103.8167 else None
    pos_part = Part()
    pos_part.semi_or_not = True
    pos_part.semi_list.append(
        _make_wire_part(
            parts, preset.branch_al_wire, "LYNX_Branch_Al", "Red",
            preset.branch_length_al,
            cad_parts_length_override=al_cad_override,
        )
    )
    pos_part.semi_list.append(
        _make_cu_al_part(parts)
    )
    pos_part.semi_list.append(
        _make_wire_part(
            parts, preset.branch_cu_wire, "normal", "Red", cu_length,
        )
    )
    if connector.type != "none":
        pos_part.semi_list.append(_make_connect_part(parts, "M", connector))

    neg_part = Part()
    neg_part.semi_or_not = True
    neg_part.semi_list.append(
        _make_wire_part(
            parts, preset.branch_al_wire, "LYNX_Branch_Al", "Black",
            preset.branch_length_al,
            cad_parts_length_override=al_cad_override,
        )
    )
    neg_part.semi_list.append(
        _make_cu_al_part(parts)
    )
    neg_part.semi_list.append(
        _make_wire_part(
            parts, preset.branch_cu_wire, "normal", "Black", cu_length,
        )
    )
    if connector.type != "none":
        neg_part.semi_list.append(_make_connect_part(parts, "F", connector))

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


# ---------------------------------------------------------------------------
# Slot 计算与 Overmold 分配
# ---------------------------------------------------------------------------


def _calculate_slot_positions(node: Node, preset: ProductPreset) -> list[int]:
    """计算 slot 的 position 列表（1-based Row 编号）。

    第 i 个 slot 的位置为 floor(box_position) + 1 + (i-1) * row_per_overmold，
    position 必须 <= Row 的总数。若 tail_shortened 为 True 且最后一个 slot
    与最后一个 Row 对齐，则删除该 slot。
    """
    row_count = len(node.rows)
    if row_count == 0:
        return []

    row_per_overmold = preset.row_per_overmold
    box_pos = node.box_position

    positions: list[int] = []
    i = 1
    while True:
        pos = int(math.floor(box_pos)) + 1 + (i - 1) * row_per_overmold
        if pos > row_count:
            break
        positions.append(pos)
        i += 1

    # tail_shortened：最后一个 slot 与最后一个 row 对齐时删除
    # 但若只有一个 slot，则不删除（至少保留一个 slot）
    if (
        preset.tail_shortened
        and len(positions) > 1
        and positions[-1] == row_count
    ):
        positions.pop()

    return positions


def _resolve_row_products(
    row: Row,
    project: Project,
) -> list[ProductPreset]:
    """解析一个 Row 上所有需要 LYNX 支线的产品预设。

    Rackings.product_presets 字符串中每个字母对应一个 IBEX、Homerun Extender
    或只提供累计长度的 Placeholder。
    预设（Gap_Extender 不会出现在 product_presets 字符串中，因为其名称不是
    单个大写字母）。因此这里直接使用 _resolve_product_presets 即可。
    """
    result: list[ProductPreset] = []
    for entry in row.racking_entries:
        entry_rackings = project.get_rackings(entry.rackings)
        if entry_rackings is not None:
            result.extend(_resolve_product_presets(entry_rackings, project))
    return result


def _count_products_in_rows(
    node: Node,
    row_indices: list[int],  # 0-based
    project: Project,
) -> int:
    """计算给定 Row 列表中所有需要 LYNX 支线的产品总数。"""
    total = 0
    for ri in row_indices:
        total += len(_resolve_row_products(node.rows[ri], project))
    return total


def _get_row_whip_lengths(
    row: Row,
    project: Project,
) -> list[Length]:
    """获取 Row 上每个产品的 whip_length（对应 RackingEntry 的 offset）。

    返回的列表与 _resolve_row_products 的结果一一对应。
    """
    result: list[Length] = []
    for entry in row.racking_entries:
        entry_rackings = project.get_rackings(entry.rackings)
        if entry_rackings is not None:
            count = len(entry_rackings.product_presets)
            result.extend([entry.offset] * count)
    return result


def _allocate_overmolds(
    node: Node,
    slot_positions: list[int],
    preset: ProductPreset,
    project: Project,
) -> list[tuple[int, int, list[tuple[ProductPreset, Length, int]]]]:
    """分配 Overmold。

    返回一个扁平列表，每个元素为：
        (slot_position, nearby_index, [(product_preset, whip_length, row_position_1based)])

    其中 nearby_index 是该 overmold 在其 slot 中的序号（从 0 开始）。
    """
    row_count = len(node.rows)
    if row_count == 0 or not slot_positions:
        return []

    result: list[
        tuple[int, int, list[tuple[ProductPreset, Length, int]]]
    ] = []

    for slot_idx, slot_pos in enumerate(slot_positions):
        # 确定该 slot 负责的 Row 范围（1-based）
        if slot_idx == 0:
            # 第一个 slot：Row 1 到第二个 slot 前（若只有一个 slot，则到最后一个 Row）
            if len(slot_positions) > 1:
                end_row = slot_positions[1] - 1
            else:
                end_row = row_count
            start_row = 1
        elif slot_idx == len(slot_positions) - 1:
            # 最后一个 slot：从对齐的 Row 到最后一个 Row
            start_row = slot_pos
            end_row = row_count
        else:
            # 中间 slot：负责 row_per_overmold 个 Row
            start_row = slot_pos
            end_row = min(
                slot_pos + preset.row_per_overmold - 1,
                row_count,
            )

        # 收集该 slot 范围内所有 Row 的产品
        responsible_rows: list[int] = list(range(start_row, end_row + 1))
        row_0based_indices = [r - 1 for r in responsible_rows]

        # 按 Row 逐个累积产品，直到超过 8 则拆分
        nearby_index = 0
        current_batch: list[tuple[ProductPreset, Length, int]] = []
        current_product_count = 0

        for row_1based in responsible_rows:
            row_0based = row_1based - 1
            row = node.rows[row_0based]
            products = _resolve_row_products(row, project)
            whips = _get_row_whip_lengths(row, project)

            # 安全检查：单个 Row 产品数不应超过 8
            if len(products) > 8:
                raise ValueError(
                    f"Row {row_1based} in Node '{node.name}' has "
                    f"{len(products)} products, which exceeds the "
                    f"maximum of 8 per Overmold. "
                    f"Please review the rackings configuration."
                )

            row_products = list(zip(products, whips))

            if current_product_count + len(products) > 8 and current_batch:
                # 当前批次已满，保存并开启新批次
                result.append((slot_pos, nearby_index, current_batch))
                nearby_index += 1
                current_batch = []
                current_product_count = 0

            # 将当前 Row 的产品加入批次
            for prod, whip in row_products:
                current_batch.append((prod, whip, row_1based))
            current_product_count += len(products)

        # 保存最后一个批次
        if current_batch:
            result.append((slot_pos, nearby_index, current_batch))

    return result


# ---------------------------------------------------------------------------
# Overmold / 主线 生成
# ---------------------------------------------------------------------------


def _make_lynx_overmold_semi(
    semi_parts: list[Part],
    parts: list[Part],
    branch_specs: list[tuple[ProductPreset, Length]],
    lynx_preset: ProductPreset,
) -> tuple[Part, Part]:
    """生成 LYNX 注塑件半成品（含正/负极）。

    branch_specs: [(product_preset, cu_length), ...]
        每个元素描述一条支线：product_preset 提供 connector，
        cu_length 是已计算好的铜线长度。
    """
    branch_count = len(branch_specs)

    pos_part = Part()
    pos_part.semi_or_not = True
    pos_part.semi_list.append(_make_overmold_part(parts, branch_count))

    neg_part = Part()
    neg_part.semi_or_not = True
    neg_part.semi_list.append(_make_overmold_part(parts, branch_count))

    directions = _LYNX_DIRECTION_MAP.get(
        branch_count, list(range(1, branch_count + 1))
    )
    for idx, (product_preset, cu_length) in enumerate(branch_specs):
        direction = directions[idx]
        pos_branch, neg_branch = _make_lynx_branch_semi(
            semi_parts, parts, cu_length, lynx_preset,
            product_preset.external_connector,
            branch_position=direction,
            overmold_branch_count=branch_count,
        )
        pos_part.semi_list.append(pos_branch)
        neg_part.semi_list.append(neg_branch)

    # 在去重前设置 direction / overmolding_detail
    _apply_lynx_overmold_semi_attributes(pos_part)
    _apply_lynx_overmold_semi_attributes(neg_part)

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


def _make_lynx_external_main_line(
    parts: list[Part],
    cluster_wire: Wire,
    external_length: Length,
    pos_product: AssembledProduct,
    neg_product: AssembledProduct,
    *,
    has_cluster_extension: bool,
) -> None:
    """生成 LYNX 外部主线及其后的标签。"""
    pos_product.parts.append(
        _make_wire_part(
            parts, cluster_wire, "LYNX_Cable_External", "Red", external_length,
        )
    )
    neg_product.parts.append(
        _make_wire_part(
            parts, cluster_wire, "LYNX_Cable_External", "Black", external_length,
        )
    )

    _append_lynx_main_line_labels(parts, pos_product, neg_product)
    if has_cluster_extension:
        _append_lynx_main_line_labels(parts, pos_product, neg_product)


def _append_lynx_main_line_labels(
    parts: list[Part],
    pos_product: AssembledProduct,
    neg_product: AssembledProduct,
) -> None:
    """为 LYNX 正、负极产品各追加一组产品类型/回路标签。"""
    for product in (pos_product, neg_product):
        product.parts.extend([
            _make_label_part(
                parts,
                "Cable Label Up",
                "Label(Product Type Label)",
            ),
            _make_label_part(
                parts,
                "Cable Label Down",
                "Label(Circuit Label)",
            ),
        ])


def _make_lynx_internal_main_line(
    parts: list[Part],
    cluster_wire: Wire,
    length: Length,
    pos_product: AssembledProduct,
    neg_product: AssembledProduct,
) -> None:
    """生成 LYNX 内部主线。"""
    pos_product.parts.append(
        _make_wire_part(
            parts, cluster_wire, "LYNX_Cable", "Red", length,
        )
    )
    neg_product.parts.append(
        _make_wire_part(
            parts, cluster_wire, "LYNX_Cable", "Black", length,
        )
    )


# ---------------------------------------------------------------------------
# LYNX 对应关系表元数据填充
# ---------------------------------------------------------------------------


def _set_lynx_metadata(
    pos_product: AssembledProduct,
    neg_product: AssembledProduct,
    node: Node,
    preset: ProductPreset,
    cluster_wire: Wire,
    racking_pitch: Length,
    cluster_len: Length,
    buffer_len: Length,
    first_slot_len: Length,
    external_length: Length,
    overmold_count: int,
    lynx_branch_count: int,
) -> None:
    """为 LYNX 产品填充对应关系表元数据。"""
    for prod in (pos_product, neg_product):
        prod.product_type = "LYNX"
        prod.node_name = node.name
        prod.box_position = node.box_position
        prod.cluster_length_ft = round(cluster_len.to_ft(), 4)
        prod.buffer_length_ft = round(buffer_len.to_ft(), 4)
        prod.first_slot_length_ft = round(first_slot_len.to_ft(), 4)
        prod.external_main_line_total_ft = round(external_length.to_ft(), 4)
        prod.cluster_wire_info = cluster_wire.wiresize
        prod.row_per_overmold = preset.row_per_overmold
        prod.overmold_count = overmold_count
        prod.lynx_branch_count = lynx_branch_count
        prod.preset_name = preset.name
        prod.preset_remarks = preset.remarks
        prod.racking_pitch_str = racking_pitch.length_str
        prod.racking_pitch_ft = round(racking_pitch.to_ft(), 4)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def _make_lynx_product(
    semi_parts: list[Part],
    parts: list[Part],
    preset: ProductPreset,
    node: Node,
    racking_pitch: Length,
    cluster_wire: Wire,
    project: Project,
) -> tuple[AssembledProduct, AssembledProduct]:
    """构建 LYNX 产品（正/负一对）。

    新逻辑：
    1. 计算 Slot 位置
    2. 分配 Overmold（处理产品数量上限 8）
    3. 生成外部主线 = cluster_length + buffer_length + first_slot_length
    4. 为每个 Overmold 生成半成品（含动态铜线长度的支线）
    5. 相邻 Overmold 之间生成内部主线
    """
    pos_product = AssembledProduct(polarity="POS")
    neg_product = AssembledProduct(polarity="NEG")

    row_count = len(node.rows)
    if row_count == 0:
        _set_lynx_metadata(
            pos_product, neg_product, node, preset, cluster_wire,
            racking_pitch,
            cluster_len=Length("0mm"),
            buffer_len=preset.buffer_length,
            first_slot_len=Length("0mm"),
            external_length=Length("0mm"),
            overmold_count=0,
            lynx_branch_count=0,
        )
        return pos_product, neg_product

    # ---- 1. 计算 Slot 位置 ----
    slot_positions = _calculate_slot_positions(node, preset)

    # ---- 2. 分配 Overmold ----
    overmold_allocations = _allocate_overmolds(
        node, slot_positions, preset, project,
    )

    # ---- 3. 生成外部主线 ----
    cluster_len = node.cluster_length or Length("0mm")
    buffer_len = preset.buffer_length
    # first_slot_length = abs(first_slot.position - box_position) * racking_pitch
    if slot_positions:
        first_slot_pos = slot_positions[0]
        first_slot_length_val = abs(first_slot_pos - node.box_position)
    else:
        first_slot_length_val = 0.0
    # 将 first_slot_length_val（无单位的倍数）乘以 racking_pitch
    first_slot_len = racking_pitch * first_slot_length_val

    external_length = cluster_len + buffer_len + first_slot_len
    _make_lynx_external_main_line(
        parts, cluster_wire, external_length, pos_product, neg_product,
        has_cluster_extension=cluster_len.to_mm() != 0,
    )

    # ---- 4. 生成 Overmold 半成品 ----
    if not overmold_allocations:
        pos_product.pv_panel = preset.pv_panel
        neg_product.pv_panel = preset.pv_panel
        _set_lynx_metadata(
            pos_product, neg_product, node, preset, cluster_wire,
            racking_pitch, cluster_len, buffer_len,
            first_slot_len, external_length,
            overmold_count=0,
            lynx_branch_count=0,
        )
        return pos_product, neg_product

    pos_overmolds: list[Part] = []
    neg_overmolds: list[Part] = []

    for slot_pos, nearby_idx, products in overmold_allocations:
        # 为每个产品计算 cu_length
        branch_specs: list[tuple[ProductPreset, Length]] = []
        for product_preset, whip_length, row_pos in products:
            # cu_length = preset.branch_length_cu
            #   + abs(slot.position - row_position) * racking_pitch
            #   + nearby_idx * nearby_overmold_gap
            #   + whip_length
            delta_pos = abs(slot_pos - row_pos)
            cu_len = (
                preset.branch_length_cu
                + racking_pitch * delta_pos
                + preset.nearby_overmold_gap * nearby_idx
                + whip_length
            )
            branch_specs.append((product_preset, cu_len))

        pos_om, neg_om = _make_lynx_overmold_semi(
            semi_parts, parts, branch_specs, preset,
        )
        pos_overmolds.append(pos_om)
        neg_overmolds.append(neg_om)

    # ---- 5. 组装产品：Overmold 与内部主线交替 ----
    for i in range(len(overmold_allocations)):
        if i > 0:
            prev_slot_pos = overmold_allocations[i - 1][0]
            curr_slot_pos = overmold_allocations[i][0]

            if prev_slot_pos == curr_slot_pos:
                # 同一 Slot 内：使用 nearby_overmold_gap
                internal_length = preset.nearby_overmold_gap
            else:
                # 不同 Slot 间：整数倍 racking_pitch
                internal_length = racking_pitch * (curr_slot_pos - prev_slot_pos)

            _make_lynx_internal_main_line(
                parts, cluster_wire, internal_length,
                pos_product, neg_product,
            )

        # 最后一段主线与最后一个 Overmold 之间也需要一组标签。
        if i == len(overmold_allocations) - 1:
            _append_lynx_main_line_labels(parts, pos_product, neg_product)

        pos_product.parts.append(pos_overmolds[i])
        neg_product.parts.append(neg_overmolds[i])

    pos_product.pv_panel = preset.pv_panel
    neg_product.pv_panel = preset.pv_panel

    # ---- 填充 LYNX 对应关系表元数据 ----
    total_branch_count = sum(
        len(products) for _, _, products in overmold_allocations
    )
    _set_lynx_metadata(
        pos_product, neg_product, node, preset, cluster_wire,
        racking_pitch, cluster_len, buffer_len,
        first_slot_len, external_length,
        overmold_count=len(overmold_allocations),
        lynx_branch_count=total_branch_count,
    )

    return pos_product, neg_product

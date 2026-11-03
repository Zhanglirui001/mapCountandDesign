"""去重逻辑 — 查找和比较部件/产品的重复项。"""

from __future__ import annotations

from ...models.assembled_product import AssembledProduct
from ...models.part import Part


def _find_duplicate_part(
    parts: list[Part],
    candidate: Part,
    extra_skip_fields: frozenset[str] = frozenset(),
) -> Part | None:
    """在已有部件列表中查找是否存在除 code 及额外指定字段外完全相同的部件。

    Args:
        parts: 已有部件列表。
        candidate: 候选部件。
        extra_skip_fields: 需要额外排除的字段名称集合，默认不排除任何额外字段。

    Returns:
        若找到重复部件则返回该部件，否则返回 None。
    """
    skip_fields = {"code", "overmolding_detail", "direction_nos"} | extra_skip_fields
    candidate_attrs = {
        k: v for k, v in vars(candidate).items() if k not in skip_fields
    }
    for existing in parts:
        if existing.code == candidate.code:
            continue
        existing_attrs = {
            k: v for k, v in vars(existing).items() if k not in skip_fields
        }
        if existing_attrs == candidate_attrs:
            return existing
    return None


def _semi_parts_equal(a: Part, b: Part) -> bool:
    """深度比较两个 Part 除 code 外的所有字段是否相同（包括 semi_list）。

    长度字段按 6 位小数舍入后比较，消除浮点精度误差
    （如 535.0 与 535.0000000000001）导致的误判不相等。
    """
    scalar_fields = frozenset({
        "category", "cad_parts_no", "cad_parts_length",
        "description", "unit", "length", "semi_or_not",
    })
    for field in scalar_fields:
        if field == "length":
            # 长度是连续量，允许 1e-6 ft（约 0.0003 mm）以内的浮点噪声。
            if round(float(getattr(a, field)), 6) != round(float(getattr(b, field)), 6):
                return False
        elif getattr(a, field) != getattr(b, field):
            return False

    # 深度比较 semi_list
    if len(a.semi_list) != len(b.semi_list):
        return False
    for sub_a, sub_b in zip(a.semi_list, b.semi_list):
        if not _semi_parts_equal(sub_a, sub_b):
            return False
    return True


def _find_duplicate_semi_part(
    semi_parts: list[Part],
    candidate: Part,
) -> Part | None:
    """在已有 semi_parts 中查找除 code 外完全相同的 semi part（深度比较 semi_list）。

    Args:
        semi_parts: 已有半成品列表。
        candidate: 候选半成品。

    Returns:
        若找到重复半成品则返回该半成品，否则返回 None。
    """
    for existing in semi_parts:
        # 仅当双方 code 均非空且相等时才跳过（命名阶段前 code 均为空，不应跳过）
        if existing.code and existing.code == candidate.code:
            continue
        if _semi_parts_equal(existing, candidate):
            return existing
    return None


def _assembled_products_equal(a: AssembledProduct, b: AssembledProduct) -> bool:
    """比较两个 AssembledProduct 是否除 code 和 quantity 外完全相同。

    深度比较 polarity、preset_name（预设来源）和 parts 列表
    （parts 的比较排除 code 字段）。预设来源不同的产品即使部件
    完全相同也不视为重复，避免跨预设合并导致正负极产品数量失衡。
    """
    if a.polarity != b.polarity:
        return False
    if a.preset_name != b.preset_name:
        return False
    if len(a.parts) != len(b.parts):
        return False
    for pa, pb in zip(a.parts, b.parts):
        if not _semi_parts_equal(pa, pb):
            return False
    return True


def _add_assembled_product_with_dedup(
    products: list[AssembledProduct],
    candidate: AssembledProduct,
) -> None:
    """将产品加入列表，若已存在除 code 和 quantity 外完全相同的产品，则给旧产品 quantity+1。

    只有预设来源（preset_name）、极性及部件均相同的产品才会合并；
    不同预设生成的外观相同的产品各自保留为独立产品。
    """
    for existing in products:
        if _assembled_products_equal(existing, candidate):
            existing.quantity += 1
            return
    products.append(candidate)


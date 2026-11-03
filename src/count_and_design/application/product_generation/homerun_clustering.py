"""Homerun Extender 产品分类逻辑。

精确相同的产品仍由生成阶段去重并累计 quantity；本模块在此基础上，
进一步把仅“可变 Homerun Extender 线段长度”不同的产品归入同一导出类别。
"""

from __future__ import annotations

from dataclasses import dataclass

from ...models.assembled_product import AssembledProduct
from ...models.part import Part


@dataclass
class HomerunExtenderClass:
    """一类仅目标主线长度不同的 Homerun Extender 产品。"""

    class_name: str
    products: list[AssembledProduct]

    @property
    def representative(self) -> AssembledProduct:
        """用于生成类别分表的代表产品。"""
        return self.products[0]


def homerun_variable_wire_index(product: AssembledProduct) -> int | None:
    """返回 Homerun 分类及长度明细使用的 Wire 部件索引。

    目标段优先从左标签之后查找：

    - 纯铜方案：左标签后的第一段铜线；有保险丝时自然落在保险丝后。
    - 铜铝方案：左标签后的铝线，而不是转换前后的铜线。
    - 左标签后没有任何主线 Wire：回退到左标签前最后一根 Wire；它可能是
      Whip-only 线，也可能是已合并 ``Whip + 纯铜主线`` 的 EP/EN 线。
    - 前后都没有 Wire：返回 ``None``。
    """
    left_label_index = next(
        (
            index
            for index, part in enumerate(product.parts)
            if part.category == "Label"
            and part.cad_parts_no == "Harness Label Crosswise Left"
        ),
        None,
    )
    if left_label_index is None:
        return None

    candidate_indexes = [
        index
        for index in range(left_label_index + 1, len(product.parts))
        if product.parts[index].category == "Wire"
    ]
    if candidate_indexes and product.uses_aluminum_wire:
        for index in candidate_indexes:
            part = product.parts[index]
            if (
                part.cad_parts_no in {"extender-POS", "extender-NEG"}
                or part.description.rstrip().endswith(" Al")
            ):
                return index
        return None

    if candidate_indexes:
        return candidate_indexes[0]

    whip_indexes = [
        index
        for index in range(left_label_index)
        if product.parts[index].category == "Wire"
    ]
    return whip_indexes[-1] if whip_indexes else None


def homerun_variable_wire_length(product: AssembledProduct) -> float:
    """返回用于分类明细表 C 列的可变 EP/EN 线段长度（ft）。"""
    index = homerun_variable_wire_index(product)
    return 0.0 if index is None else round(product.parts[index].length, 4)


def homerun_total_wire_length(product: AssembledProduct) -> float:
    """返回产品中所有 Wire 部件的长度之和（ft，含嵌套半成品）。"""

    def _sum_wires(parts: list[Part]) -> float:
        total = 0.0
        for part in parts:
            if part.category == "Wire":
                total += part.length
            if part.semi_list:
                total += _sum_wires(part.semi_list)
        return total

    return round(_sum_wires(product.parts), 4)


def _variable_wire_parts_equal(a: Part, b: Part) -> bool:
    """比较目标 Wire，忽略长度及由长度区间派生的 CAD 字段。"""
    ignored = {"code", "length", "cad_parts_no", "cad_parts_length"}
    attrs_a = {key: value for key, value in vars(a).items() if key not in ignored}
    attrs_b = {key: value for key, value in vars(b).items() if key not in ignored}
    return attrs_a == attrs_b


def _fixed_parts_equal(a: Part, b: Part) -> bool:
    """深度比较非目标部件，仅忽略运行期分配的 code。"""
    ignored = {"code", "length", "semi_list"}
    attrs_a = {key: value for key, value in vars(a).items() if key not in ignored}
    attrs_b = {key: value for key, value in vars(b).items() if key not in ignored}
    if attrs_a != attrs_b:
        return False
    if round(a.length, 6) != round(b.length, 6):
        return False
    if len(a.semi_list) != len(b.semi_list):
        return False
    return all(
        _fixed_parts_equal(child_a, child_b)
        for child_a, child_b in zip(a.semi_list, b.semi_list)
    )


def homerun_products_same_class(
    a: AssembledProduct,
    b: AssembledProduct,
) -> bool:
    """判断两个 Homerun 产品是否只在目标主线长度上不同。"""
    if a.polarity != b.polarity:
        return False
    if a.preset_name != b.preset_name:
        return False
    if a.uses_aluminum_wire != b.uses_aluminum_wire:
        return False
    if len(a.parts) != len(b.parts):
        return False

    variable_a = homerun_variable_wire_index(a)
    variable_b = homerun_variable_wire_index(b)
    if variable_a != variable_b:
        return False

    for index, (part_a, part_b) in enumerate(zip(a.parts, b.parts)):
        if variable_a is not None and index == variable_a:
            if not _variable_wire_parts_equal(part_a, part_b):
                return False
        elif not _fixed_parts_equal(part_a, part_b):
            return False
    return True


def cluster_homerun_extender_products(
    products: list[AssembledProduct],
) -> list[HomerunExtenderClass]:
    """按首次出现顺序对 Homerun Extender 产品进行分类。"""
    classes: list[HomerunExtenderClass] = []
    for product in products:
        for product_class in classes:
            if homerun_products_same_class(product_class.representative, product):
                product_class.products.append(product)
                break
        else:
            classes.append(
                HomerunExtenderClass(
                    class_name=product.code,
                    products=[product],
                )
            )
    return classes

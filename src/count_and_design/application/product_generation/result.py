"""Product generation result data class."""

from __future__ import annotations

from dataclasses import dataclass, field

from ...models.assembled_product import AssembledProduct
from ...models.part import Part


@dataclass
class GeneratedPresetProducts:
    """Products generated for one preset position in a racking occurrence."""

    preset_index: int
    preset_name: str
    product_type: str
    positive: AssembledProduct | None = None
    negative: AssembledProduct | None = None


@dataclass
class GeneratedRackingInstance:
    """One concrete RackingEntry and the products generated from it."""

    inv_name: str
    node_name: str
    row_name: str
    entry_index: int
    rackings_name: str
    preset_names: tuple[str, ...] = ()
    preset_products: list[GeneratedPresetProducts] = field(default_factory=list)


class ProductGenerationResult:
    """产品生成结果。"""

    def __init__(self) -> None:
        self.semi_parts: list[Part] = []
        self.parts: list[Part] = []
        self.ibex_products: list[AssembledProduct] = []
        self.lynx_products: list[AssembledProduct] = []
        self.gap_extender_products: list[AssembledProduct] = []
        self.homerun_extender_products: list[AssembledProduct] = []
        # 对应关系表专用：保留每个产品实例（去重前），包含完整的 INV/Node/Row 元数据
        self.all_products: list[AssembledProduct] = []
        # 简图专用：保留每个实际 RackingEntry 与其产品对之间的稳定关联。
        self.racking_instances: list[GeneratedRackingInstance] = []

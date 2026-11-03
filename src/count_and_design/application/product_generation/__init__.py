"""产品生成模块 — 将 Project 转换为 IBEX/LYNX 产品及部件列表。

提供:
- ProductGenerationResult: 生成结果数据结构
- ProductGenerationService: 产品生成服务（核心业务逻辑）
- export_to_xlsx: 产品图纸导出适配器
- export_correspondence_table: 对应关系表导出
- export_schematics: 支架组合 SVG 简图导出
"""

from .correspondence import export_correspondence_table
from .export import export_to_xlsx
from .result import (
    GeneratedPresetProducts,
    GeneratedRackingInstance,
    ProductGenerationResult,
)
from .schematics import (
    SchematicExportResult,
    build_schematic_variants,
    export_schematics,
)
from .service import ProductGenerationService

__all__ = [
    "GeneratedPresetProducts",
    "GeneratedRackingInstance",
    "ProductGenerationResult",
    "ProductGenerationService",
    "SchematicExportResult",
    "build_schematic_variants",
    "export_to_xlsx",
    "export_correspondence_table",
    "export_schematics",
]

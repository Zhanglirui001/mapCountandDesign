"""Block-diagram table recognition for Agent3 (Element Recognition)."""

from __future__ import annotations

from .common import (
    STRATEGY_MINERU_CLAUDE,
    STRATEGY_YOLO_CLAUDE,
    STRATEGY_YOLO_GLM,
    BlockDiagramTableError,
    BlockDiagramTableResult,
    BlockDiagramTableRow,
    ImageInput,
    TableLocation,
    TableRegion,
    bedrock_image_transport_size,
    compact_table_markdown,
    compress_image_for_bedrock,
    html_table_to_plain_text,
    load_image_input,
    normalize_equipment_family,
    normalize_project_type,
    score_equipment_table_html,
    select_all_region_blocks,
    select_best_table_markdown,
    select_region_by_iou,
    select_table_bbox,
)
from .recognizer import BlockDiagramTableRecognizer

__all__ = [
    "BlockDiagramTableError",
    "BlockDiagramTableRecognizer",
    "BlockDiagramTableResult",
    "BlockDiagramTableRow",
    "ImageInput",
    "STRATEGY_MINERU_CLAUDE",
    "STRATEGY_YOLO_CLAUDE",
    "STRATEGY_YOLO_GLM",
    "TableLocation",
    "TableRegion",
    "_bedrock_image_transport_size",
    "_compact_table_markdown",
    "_compress_image_for_bedrock",
    "_html_table_to_plain_text",
    "_load_image_input",
    "_normalize_equipment_family",
    "_normalize_project_type",
    "_score_equipment_table_html",
    "_select_all_region_blocks",
    "_select_best_table_markdown",
    "_select_region_by_iou",
    "_select_table_bbox",
]

_bedrock_image_transport_size = bedrock_image_transport_size
_compact_table_markdown = compact_table_markdown
_compress_image_for_bedrock = compress_image_for_bedrock
_html_table_to_plain_text = html_table_to_plain_text
_load_image_input = load_image_input
_normalize_equipment_family = normalize_equipment_family
_normalize_project_type = normalize_project_type
_score_equipment_table_html = score_equipment_table_html
_select_all_region_blocks = select_all_region_blocks
_select_best_table_markdown = select_best_table_markdown
_select_region_by_iou = select_region_by_iou
_select_table_bbox = select_table_bbox

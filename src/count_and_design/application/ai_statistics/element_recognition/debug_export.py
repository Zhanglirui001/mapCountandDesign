"""JSON export helpers for inspecting Agent3 intermediate results."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from ...ai.types import BoundingBox, DetectionResult
from .block_diagram_table import BlockDiagramTableResult, TableLocation
from .contracts import ElementRecognitionRequest, ElementRecognitionResult


DEBUG_EXPORT_VERSION = "agent3-debug-v1"


def build_agent3_debug_payload(
    request: ElementRecognitionRequest | None,
    result: ElementRecognitionResult,
    page_images: Mapping[int, bytes] | None = None,
    *,
    include_page_images: bool = True,
) -> dict[str, Any]:
    """Build a self-contained, JSON-safe Agent3 diagnostic payload.

    Page PNGs are embedded as base64 by default so an exported file contains
    the exact model input. Callers may disable them when a compact report is
    preferred; byte length and SHA-256 remain available in either mode.
    """

    debug_pages = result.raw.get("debug_pages", ())
    pages_by_number = {
        int(page.get("page_number", 0)): page
        for page in debug_pages
        if isinstance(page, Mapping)
    }
    page_numbers = _page_numbers(request, result, pages_by_number)
    inputs = []
    for page_number in page_numbers:
        image_bytes = _page_image_bytes(request, page_images, page_number)
        fallback = pages_by_number.get(page_number, {}).get("input", {})
        inputs.append(
            _input_page_dict(
                page_number,
                image_bytes,
                fallback=fallback if isinstance(fallback, Mapping) else {},
                include_page_image=include_page_images,
            )
        )

    payload = {
        "schema_version": DEBUG_EXPORT_VERSION,
        "input_data": {
            **_request_metadata(request),
            "pages": inputs,
        },
        "table_recognition_results": [
            _category_entry(page, "table_recognition")
            for page in _ordered_debug_pages(debug_pages)
        ],
        "yolo_tracker_detection_results": [
            _category_entry(page, "yolo_tracker_detection")
            for page in _ordered_debug_pages(debug_pages)
        ],
        "yolo_box_detection_results": [
            _category_entry(page, "yolo_box_detection")
            for page in _ordered_debug_pages(debug_pages)
        ],
        "ocr_node_name_results": [
            _category_entry(page, "ocr_node_names")
            for page in _ordered_debug_pages(debug_pages)
        ],
        "claude_node_name_review_results": [
            _category_entry(page, "claude_node_name_review")
            for page in _ordered_debug_pages(debug_pages)
        ],
    }
    return json_safe(payload)


def input_image_debug_dict(page_number: int, image_bytes: bytes) -> dict[str, Any]:
    """Return compact metadata for one exact Agent3 page input."""

    width = height = 0
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            width, height = image.size
    except Exception:  # noqa: BLE001 - diagnostic metadata must not break Agent3
        pass
    return {
        "page_number": page_number,
        "media_type": "image/png",
        "byte_length": len(image_bytes),
        "sha256": hashlib.sha256(image_bytes).hexdigest(),
        "width": width,
        "height": height,
    }


def table_result_debug_dict(
    result: BlockDiagramTableResult | None,
    error: str | None = None,
    *,
    raw_llm: Mapping[str, Any] | None = None,
    location: TableLocation | None = None,
) -> dict[str, Any]:
    """Serialize one table-recognition entry for the debug payload.

    ``raw_llm`` carries the verbatim LLM response when recognition failed
    while parsing or validating it; on success the response is preserved
    inside ``result.raw_output`` next to the parsed ``raw_llm`` payload.
    ``location`` carries the located region when recognition failed after
    the locate phase, so the export still shows where the table was found.
    """
    if result is None:
        return {
            "result": None,
            "error": error,
            "raw_llm": json_safe(raw_llm) if raw_llm else None,
            "location": (
                {
                    "table_bbox": bbox_debug_dict(location.bbox),
                    "source": location.source,
                    "regions": [
                        {
                            "bbox": bbox_debug_dict(region.bbox),
                            "kind": region.kind,
                            "block_index": region.block_index,
                        }
                        for region in location.regions
                    ],
                    "raw_output": json_safe(dict(location.raw)),
                }
                if location is not None
                else None
            ),
        }
    return {
        "result": {
            "project_type": result.project_type,
            "equipment_family": result.equipment_family,
            "strategy": result.strategy,
            "string_info": [
                {"node": row.node, "string_number": row.string_number}
                for row in result.string_info
            ],
            "table_bbox": bbox_debug_dict(result.table_bbox),
            "cropped_image_path": (
                str(result.cropped_image_path)
                if result.cropped_image_path is not None
                else None
            ),
            "markdown": result.markdown,
            "raw_llm": json_safe(result.raw_llm),
            "raw_output": json_safe(result.raw_output),
        },
        "error": error,
    }


def detection_result_debug_dict(item: DetectionResult) -> dict[str, Any]:
    return {
        "label": item.label,
        "confidence": item.confidence,
        "class_id": item.class_id,
        "bbox": bbox_debug_dict(item.box),
        "source": item.source,
        "raw": json_safe(item.raw),
    }


def bbox_debug_dict(box: BoundingBox | None) -> dict[str, float] | None:
    if box is None:
        return None
    return {"x1": box.x1, "y1": box.y1, "x2": box.x2, "y2": box.y2}


def json_safe(value: Any) -> Any:
    """Recursively normalize provider payloads to values accepted by JSON."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    if is_dataclass(value):
        return json_safe(asdict(value))
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return json_safe(tolist())
        except Exception:  # noqa: BLE001 - diagnostic fallback
            pass
    return repr(value)


def _ordered_debug_pages(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    pages = [item for item in value if isinstance(item, Mapping)]
    return sorted(pages, key=lambda item: int(item.get("page_number", 0)))


def _category_entry(page: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = page.get(key)
    if key == "ocr_node_names" and value is None:
        value = []
    return {"page_number": int(page.get("page_number", 0)), "data": value}


def _page_numbers(
    request: ElementRecognitionRequest | None,
    result: ElementRecognitionResult,
    debug_pages: Mapping[int, Mapping[str, Any]],
) -> list[int]:
    numbers = {page.page_number for page in result.pages}
    numbers.update(debug_pages)
    if request is not None:
        numbers.update(page.page_number for page in request.pages)
    return sorted(numbers)


def _page_image_bytes(
    request: ElementRecognitionRequest | None,
    page_images: Mapping[int, bytes] | None,
    page_number: int,
) -> bytes:
    if page_images is not None:
        try:
            value = page_images.get(page_number, b"")
        except Exception:  # noqa: BLE001 - fall back to request bytes
            value = b""
        if isinstance(value, bytes) and value:
            return value
    if request is not None:
        for page in request.pages:
            if page.page_number == page_number:
                return page.png_bytes
    return b""


def _input_page_dict(
    page_number: int,
    image_bytes: bytes,
    *,
    fallback: Mapping[str, Any],
    include_page_image: bool,
) -> dict[str, Any]:
    item = dict(fallback)
    if image_bytes:
        item.update(input_image_debug_dict(page_number, image_bytes))
        if include_page_image:
            item["png_base64"] = base64.b64encode(image_bytes).decode("ascii")
    else:
        item.setdefault("page_number", page_number)
        item["png_base64"] = None
    if not include_page_image:
        item.pop("png_base64", None)
    return item


def _request_metadata(request: ElementRecognitionRequest | None) -> dict[str, Any]:
    if request is None:
        return {
            "project_name": "",
            "tracker_definitions": [],
            "expected_inv_names": [],
            "expected_inv_string_counts": [],
            "default_panel_orientation": "unknown",
            "inv_settings": [],
            "default_offset_snap_step_strings": 1.0,
        }
    return {
        "project_name": request.project_name,
        "tracker_definitions": [
            {
                "code": item.code,
                "strings_per_tracker": item.strings_per_tracker,
                "total_count": item.total_count,
                "panel_label": item.panel_label,
            }
            for item in request.tracker_definitions
        ],
        "expected_inv_names": list(request.expected_inv_names),
        "expected_inv_string_counts": [
            {
                "inv_name": item.inv_name,
                "total": item.total if item.total is not None else {},
                "columns": [
                    {
                        "label": column.label,
                        "count": (
                            column.count if column.count is not None else {}
                        ),
                        "column_index": column.column_index,
                    }
                    for column in item.columns
                ],
            }
            for item in request.expected_inv_string_counts
        ],
        "default_panel_orientation": request.default_panel_orientation.value,
        "inv_settings": [
            {
                "inv_name": item.inv_name,
                "panel_orientation": (
                    item.panel_orientation.value
                    if item.panel_orientation is not None
                    else None
                ),
                "offset_snap_step_strings": item.offset_snap_step_strings,
            }
            for item in request.inv_settings
        ],
        "default_offset_snap_step_strings": (
            request.default_offset_snap_step_strings
        ),
    }

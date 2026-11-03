"""Shared types and helpers for block-diagram table recognition.

Sections:
- Types: result dataclasses, strategy constants, errors
- Image I/O: load input, bbox conversion, Bedrock compression
- Table selection: header-semantic schedule table picking
- Markdown: compaction for table selection
- LLM output: JSON parsing, validation, result building
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from count_and_design.application.ai import BoundingBox, DetectionResult
from count_and_design.application.ai.llm.providers import (
    BedrockLlmProvider,
    OpenRouterLlmProvider,
)
from count_and_design.application.ai.llm.types import LlmServiceError
from count_and_design.application.config import BlockDiagramTableConfig
from count_and_design.application.image_transport import (
    ImageTransportError,
    bedrock_image_transport_size as _bedrock_image_transport_size,
    compress_image_for_bedrock as _compress_image_for_bedrock,
)

# --- Types -------------------------------------------------------------------

ImageInput = str | Path | bytes

STRATEGY_MINERU_CLAUDE = "mineru_claude"
STRATEGY_YOLO_CLAUDE = "yolo_claude"
STRATEGY_YOLO_GLM = "yolo_glm"


@dataclass(frozen=True)
class TableRegion:
    """One candidate table region, kept independent of the extraction step.

    ``kind`` is the source block's type when the region came from a MinerU
    layout block (``"table"``, ``"image"``, ``"text"``, ...), ``"yolo"`` when
    the region is the raw YOLO box, and ``"page"`` for the whole-page box. It
    defaults to ``""`` when the source is unknown.
    """

    bbox: BoundingBox
    kind: str = ""
    block_index: int | None = None


@dataclass(frozen=True)
class TableLocation:
    """A located table region, independent of the later LLM extraction.

    ``source`` is the locator that produced the region (``"yolo"`` or
    ``"mineru"``); ``raw`` carries locate diagnostics such as timings and
    confidence so they survive even when extraction fails.

    ``regions`` holds every region that will be cropped and sent to the
    extraction model. It always contains ``bbox`` as its first entry, so
    single-region callers can keep using ``bbox`` alone.
    """

    bbox: BoundingBox
    source: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)
    regions: tuple[TableRegion, ...] = ()

    def __post_init__(self) -> None:
        if not self.regions:
            object.__setattr__(
                self,
                "regions",
                (TableRegion(bbox=self.bbox, kind=self.source),),
            )


class BlockDiagramTableError(RuntimeError):
    """Raised when block-diagram table recognition fails.

    ``raw_llm`` optionally carries the verbatim LLM response (and the prompt
    that produced it) when the failure happened while parsing or validating
    an LLM response, so callers can persist it in debug exports.

    ``location`` carries the located table region(s) when the failure happened
    after the locate phase, so callers can still draw and export the regions
    the locator found.
    """

    def __init__(
        self,
        message: str,
        *,
        raw_llm: dict[str, Any] | None = None,
        location: TableLocation | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_llm = raw_llm
        self.location = location

    def with_location(self, location: TableLocation) -> "BlockDiagramTableError":
        """Return this error, or a copy that carries the located region."""

        if self.location is not None:
            return self
        return BlockDiagramTableError(
            str(self),
            raw_llm=self.raw_llm,
            location=location,
        )


@dataclass(frozen=True)
class BlockDiagramTableRow:
    """One parsed equipment row from the block-diagram table."""

    node: str
    string_number: int


@dataclass(frozen=True)
class BlockDiagramTableResult:
    """Structured output from the block-diagram table workflow."""

    project_type: str
    string_info: tuple[BlockDiagramTableRow, ...]
    equipment_family: str = ""
    strategy: str = ""
    table_bbox: BoundingBox | None = None
    cropped_image_path: Path | None = None
    markdown: str = ""
    raw_llm: dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    raw_output: dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_type": self.project_type,
            "equipment_family": self.equipment_family,
            "string_info": [
                {
                    "node": item.node,
                    "string_number": item.string_number,
                }
                for item in self.string_info
            ],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=indent,
        )


# --- Image I/O ---------------------------------------------------------------

def load_image_input(image: ImageInput) -> tuple[Path | None, bytes]:
    if isinstance(image, bytes):
        if not image:
            raise BlockDiagramTableError("Image bytes are empty.")
        return None, image
    path = Path(image).expanduser()
    if not path.exists():
        raise BlockDiagramTableError(f"Image file does not exist: {path}")
    data = path.read_bytes()
    if not data:
        raise BlockDiagramTableError(f"Image file is empty: {path}")
    return path, data


def parse_bbox(raw: Any) -> BoundingBox | None:
    if not isinstance(raw, Sequence) or len(raw) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(value) for value in raw)
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return BoundingBox(x1, y1, x2, y2)


def expand_bbox_uniformly(
    bbox: BoundingBox,
    *,
    expansion_ratio: float,
    max_width: float,
    max_height: float,
) -> BoundingBox:
    """Expand a box on all sides by a fraction of its own width and height."""

    if expansion_ratio <= 0.0 or max_width <= 0.0 or max_height <= 0.0:
        return bbox
    margin_x = max(0.0, bbox.x2 - bbox.x1) * expansion_ratio
    margin_y = max(0.0, bbox.y2 - bbox.y1) * expansion_ratio
    return BoundingBox(
        max(0.0, bbox.x1 - margin_x),
        max(0.0, bbox.y1 - margin_y),
        min(float(max_width), bbox.x2 + margin_x),
        min(float(max_height), bbox.y2 + margin_y),
    )


def bbox_to_pixels(
    bbox: BoundingBox,
    width: int,
    height: int,
) -> BoundingBox:
    values = (bbox.x1, bbox.y1, bbox.x2, bbox.y2)
    if max(values) <= 1.0:
        scale_x = width
        scale_y = height
    elif max(values) <= 1000.0:
        scale_x = width / 1000.0
        scale_y = height / 1000.0
    else:
        return bbox
    return BoundingBox(
        bbox.x1 * scale_x,
        bbox.y1 * scale_y,
        bbox.x2 * scale_x,
        bbox.y2 * scale_y,
    )


def select_yolo_table_bbox(
    detections: Sequence[DetectionResult],
    image_width: int,
    image_height: int,
    *,
    min_confidence: float,
) -> tuple[BoundingBox | None, float | None]:
    """Pick the highest-confidence YOLO table box when it clears ``min_confidence``.

    Returns a bbox normalized to 0..1 in xyxy form, or ``(None, best_confidence)``
    when no detection meets the threshold (caller should fall back to MinerU).
    """

    if not detections or image_width <= 0 or image_height <= 0:
        return None, None
    best = max(detections, key=lambda item: item.confidence)
    if best.confidence < min_confidence:
        return None, best.confidence
    box = best.box
    return (
        BoundingBox(
            box.x1 / image_width,
            box.y1 / image_height,
            box.x2 / image_width,
            box.y2 / image_height,
        ),
        best.confidence,
    )

def table_area(item: dict[str, Any]) -> float:
    bbox = parse_bbox(item.get("bbox"))
    if bbox is None:
        return 0.0
    return max(0.0, bbox.x2 - bbox.x1) * max(0.0, bbox.y2 - bbox.y1)


def bedrock_image_transport_size(image_bytes: bytes) -> int:
    """Size after base64 encoding, which Bedrock enforces against its transport cap."""
    return _bedrock_image_transport_size(image_bytes)


def compress_image_for_bedrock(
    image_bytes: bytes,
    *,
    max_transport_bytes: int | None = None,
) -> tuple[bytes, str]:
    limit = max_transport_bytes or BlockDiagramTableConfig().bedrock_max_transport_bytes
    try:
        return _compress_image_for_bedrock(
            image_bytes,
            max_transport_bytes=limit,
        )
    except ImageTransportError as exc:
        raise BlockDiagramTableError(str(exc)) from exc


# --- Table selection ---------------------------------------------------------

_EXCLUDED_TABLE_HEADER_TOKENS = (
    "LEGEND",
    "KEY PLAN",
    "INFORMATION",
    "REVISION",
    "REV",
    "DESCRIPTION",
    "STRING QTY",
    "MODULE QTY",
    "TRENCH NAME",
    "PROJECT BOUNDARY",
)


def select_table_bbox(
    content_list: Sequence[dict[str, Any]],
    markdown: str = "",
) -> BoundingBox:
    tables = [
        item
        for item in content_list
        if isinstance(item, dict) and item.get("type") == "table"
    ]
    html_tables = extract_html_tables(markdown)
    if html_tables and tables:
        best_index = select_best_table_index(html_tables)
        if (
            best_index is not None
            and best_index < len(tables)
            and score_equipment_table_html(html_tables[best_index]) > 0.0
        ):
            bbox = parse_bbox(tables[best_index].get("bbox"))
            if bbox is not None:
                return bbox
    if tables:
        best_item = max(tables, key=table_area)
        bbox = parse_bbox(best_item.get("bbox"))
        if bbox is None:
            raise BlockDiagramTableError(
                "MinerU table block did not include a usable bbox."
            )
        return bbox
    if markdown_contains_table(markdown):
        return BoundingBox(0.0, 0.0, 1.0, 1.0)
    raise BlockDiagramTableError(
        "MinerU did not locate any table in the image."
    )


def select_region_by_iou(
    content_list: Sequence[dict[str, Any]],
    reference: BoundingBox | None,
) -> list[TableRegion]:
    """Return the MinerU block that overlaps ``reference`` the most.

    Any block type is eligible (``table``, ``image``, ``text``, ...): the
    rejected YOLO box is treated as the authoritative pointer and the layout
    block it falls inside is the region to crop. Blocks without a usable bbox
    are ignored.
    """

    blocks = _layout_blocks(content_list)
    if not blocks:
        raise BlockDiagramTableError(
            "MinerU returned no layout blocks to match against the YOLO box."
        )
    if reference is None:
        return [first_region_with_bbox(blocks)]
    best: tuple[float, int, dict[str, Any], BoundingBox] | None = None
    for index, item in blocks:
        bbox = parse_bbox(item.get("bbox"))
        if bbox is None:
            continue
        score = intersection_over_union(reference, bbox)
        if best is None or score > best[0]:
            best = (score, index, item, bbox)
    if best is None:
        raise BlockDiagramTableError(
            "MinerU layout blocks did not include a usable bbox."
        )
    _, index, item, bbox = best
    return [TableRegion(bbox=bbox, kind=_block_kind(item), block_index=index)]


def first_region_with_bbox(
    blocks: Sequence[tuple[int, dict[str, Any]]],
) -> TableRegion:
    for index, item in blocks:
        bbox = parse_bbox(item.get("bbox"))
        if bbox is not None:
            return TableRegion(
                bbox=bbox,
                kind=_block_kind(item),
                block_index=index,
            )
    raise BlockDiagramTableError(
        "MinerU layout blocks did not include a usable bbox."
    )


def select_all_region_blocks(
    content_list: Sequence[dict[str, Any]],
) -> list[TableRegion]:
    """Return every MinerU block that carries a usable bbox, in layout order.

    Used when YOLO found nothing at all: no layout block is filtered out, so
    the extraction model receives the whole page as ordered crops.
    """

    regions = [
        TableRegion(bbox=bbox, kind=_block_kind(item), block_index=index)
        for index, item in _layout_blocks(content_list)
        for bbox in [parse_bbox(item.get("bbox"))]
        if bbox is not None
    ]
    if not regions:
        raise BlockDiagramTableError(
            "MinerU returned no layout blocks with a usable bbox."
        )
    return regions


def intersection_over_union(first: BoundingBox, second: BoundingBox) -> float:
    """Intersection over union of two boxes, in any shared coordinate space."""

    x1 = max(first.x1, second.x1)
    y1 = max(first.y1, second.y1)
    x2 = min(first.x2, second.x2)
    y2 = min(first.y2, second.y2)
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if intersection <= 0.0:
        return 0.0
    first_area = max(0.0, first.x2 - first.x1) * max(0.0, first.y2 - first.y1)
    second_area = max(0.0, second.x2 - second.x1) * max(0.0, second.y2 - second.y1)
    union = first_area + second_area - intersection
    if union <= 0.0:
        return 0.0
    return intersection / union


def scale_bbox_between_spaces(
    bbox: BoundingBox,
    *,
    source_width: float,
    source_height: float,
    target_width: float,
    target_height: float,
) -> BoundingBox:
    """Map a box from one page box into another (e.g. YOLO space to layout)."""

    if source_width <= 0.0 or source_height <= 0.0:
        return bbox
    scale_x = target_width / source_width
    scale_y = target_height / source_height
    return BoundingBox(
        bbox.x1 * scale_x,
        bbox.y1 * scale_y,
        bbox.x2 * scale_x,
        bbox.y2 * scale_y,
    )


def _layout_blocks(
    content_list: Sequence[dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    return [
        (index, item)
        for index, item in enumerate(content_list)
        if isinstance(item, dict)
    ]


def _block_kind(item: Mapping[str, Any]) -> str:
    kind = item.get("type")
    return str(kind) if kind else ""


def extract_html_tables(markdown: str) -> list[str]:
    return re.findall(r"<table\b.*?</table>", markdown, flags=re.I | re.S)


def select_best_table_index(html_tables: Sequence[str]) -> int | None:
    if not html_tables:
        return None
    scores = [score_equipment_table_html(table_html) for table_html in html_tables]
    best_score = max(scores)
    if best_score <= 0.0:
        return None
    return scores.index(best_score)


def select_best_table_markdown(markdown: str) -> str:
    html_tables = extract_html_tables(markdown)
    if not html_tables:
        return compact_table_markdown(markdown)
    best_index = select_best_table_index(html_tables)
    if best_index is None:
        return compact_table_markdown(markdown)
    return html_tables[best_index]


def score_equipment_table_html(table_html: str) -> float:
    grid = parse_html_table(table_html)
    if len(grid) < 2:
        return -1.0
    header_index = find_header_row_index(grid)
    if header_index is None:
        return -1.0
    headers = grid[header_index]
    if is_excluded_schedule_table(headers):
        return -1.0
    equipment_index = find_equipment_column_index(headers)
    string_index = find_string_count_column_index(headers)
    if equipment_index is None or string_index is None:
        return -1.0
    return 100.0


def parse_html_table(table_html: str) -> list[list[str]]:
    rows_html = re.findall(r"<tr\b.*?>(.*?)</tr>", table_html, flags=re.I | re.S)
    carry: dict[int, tuple[str, int]] = {}
    grid: list[list[str]] = []

    for row_html in rows_html:
        row: list[str] = []
        col = 0

        def fill_carried() -> None:
            nonlocal col
            while col in carry:
                value, remaining = carry[col]
                row.append(value)
                if remaining <= 1:
                    del carry[col]
                else:
                    carry[col] = (value, remaining - 1)
                col += 1

        fill_carried()
        for attrs, content in re.findall(
            r"<td\b([^>]*)>(.*?)</td>",
            row_html,
            flags=re.I | re.S,
        ):
            fill_carried()
            text = re.sub(r"<[^>]+>", " ", content)
            text = re.sub(r"\s+", " ", text).strip()
            rowspan_match = re.search(
                r'rowspan\s*=\s*["\']?(\d+)',
                attrs,
                flags=re.I,
            )
            rowspan = int(rowspan_match.group(1)) if rowspan_match else 1
            row.append(text)
            if rowspan > 1:
                carry[col] = (text, rowspan - 1)
            col += 1
        fill_carried()
        if row:
            grid.append(row)
    return grid


def find_header_row_index(grid: Sequence[Sequence[str]]) -> int | None:
    for index, row in enumerate(grid):
        if find_string_count_column_index(row) is not None:
            return index
    return None


def find_equipment_column_index(headers: Sequence[str]) -> int | None:
    for index, header in enumerate(headers):
        if is_equipment_header(header):
            return index
    return None


def find_string_count_column_index(headers: Sequence[str]) -> int | None:
    for index, header in enumerate(headers):
        if is_string_count_header(header):
            return index
    return None


def is_string_count_header(header: str) -> bool:
    upper = header.upper().strip()
    if upper in ("STRINGS", "# STRINGS", "STRING HARNESS"):
        return True
    if "STRING QTY" in upper or upper.endswith(" QTY"):
        return False
    if "STRING" not in upper:
        return False
    return any(
        token in upper
        for token in (
            "COMBINER",
            "LOAD BREAK",
            "COUNT",
            "NO. OF",
            "NO OF",
            "NUMBER OF",
        )
    )


def is_equipment_header(header: str) -> bool:
    upper = header.upper().strip()
    compact = re.sub(r"\s+", "", upper)
    if is_string_count_header(upper):
        return False
    if upper == "FROM" or compact == "FROM":
        return True
    if upper in ("TO", "TRENCH ID", "TRENCH NAME"):
        return False
    return any(
        token in upper or token in compact
        for token in (
            "LOAD BREAK DISCONNECT",
            "LOADBREAKDISCONNECT",
            "FROM COMBINER",
            "FROMCOMBINER",
            "COMBINER BOX",
            "COMBINERBOX",
            "COMBINER ID",
            "COMBINERID",
            "CBX",
            "LBD",
            "LYNX",
        )
    )


def is_excluded_schedule_table(headers: Sequence[str]) -> bool:
    header_text = " ".join(headers).upper()
    has_equipment = find_equipment_column_index(headers) is not None
    has_strings = find_string_count_column_index(headers) is not None
    if has_equipment and has_strings:
        return False
    return any(token in header_text for token in _EXCLUDED_TABLE_HEADER_TOKENS)


def markdown_contains_table(markdown: str) -> bool:
    if not markdown.strip():
        return False
    if "<table" in markdown.lower():
        return True
    lines = markdown.splitlines()
    for index, line in enumerate(lines[:-1]):
        if "|" not in line:
            continue
        if re.search(r"\|\s*-+", lines[index + 1]):
            return True
    return False


# --- Markdown ----------------------------------------------------------------

def compact_table_markdown(markdown: str) -> str:
    tables = re.findall(r"<table\b.*?</table>", markdown, flags=re.I | re.S)
    if tables:
        return "\n".join(tables)
    lines = markdown.splitlines()
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if "|" in line:
            current.append(line)
            continue
        if current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    pipe_tables = [
        block
        for block in blocks
        if re.search(r"\|\s*-+", block)
    ]
    if pipe_tables:
        return "\n\n".join(pipe_tables)
    return markdown.strip()


def html_table_to_plain_text(table_html: str) -> str:
    rows_html = re.findall(r"<tr\b.*?>(.*?)</tr>", table_html, flags=re.I | re.S)
    lines: list[str] = []
    for row_html in rows_html:
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, flags=re.I | re.S)
        values: list[str] = []
        for cell in cells:
            text = re.sub(r"<[^>]+>", " ", cell)
            text = re.sub(r"\s+", " ", text).strip()
            values.append(text)
        if any(values):
            lines.append(" | ".join(values))
    return "\n".join(lines)


# --- LLM output --------------------------------------------------------------

@dataclass(frozen=True)
class LlmExtraction:
    """Parsed structured payload plus the verbatim LLM output behind it."""

    payload: dict[str, Any]
    raw_output: dict[str, Any]


def llm_raw_output(completion: object) -> dict[str, Any]:
    """Capture the verbatim LLM response for diagnostic exports.

    ``completion`` is a :class:`LlmCompletionResult`; attribute access is
    defensive so diagnostic plumbing never breaks on provider mocks.
    """

    output: dict[str, Any] = {}
    content = getattr(completion, "content", "")
    if content:
        output["content"] = content
    for name in ("model", "finish_reason", "reasoning"):
        value = getattr(completion, name, "")
        if value:
            output[name] = value
    usage = getattr(completion, "usage", None)
    if usage:
        try:
            output["usage"] = dict(usage)
        except (TypeError, ValueError):
            output["usage"] = repr(usage)
    raw = getattr(completion, "raw", None)
    if raw:
        try:
            output["raw"] = dict(raw)
        except (TypeError, ValueError):
            output["raw"] = repr(raw)
    return output


_REQUIRED_TABLE_FIELDS = frozenset({"equipment_family", "string_info"})


def _collapse_doubled_json_braces(text: str) -> str:
    """Repair models that copy prompt-escaped braces as literal ``{{`` / ``}}``."""

    previous = None
    while previous != text:
        previous = text
        text = text.replace("{{", "{").replace("}}", "}")
    return text


def _is_structured_table_payload(value: Any) -> bool:
    return isinstance(value, dict) and _REQUIRED_TABLE_FIELDS.issubset(value)


def _decode_first_json_value(text: str) -> Any | None:
    """Decode a JSON object or array anywhere in ``text``.

    Responses may add prose before or after the JSON payload.
    Prefer a top-level object that already has the required table fields
    so a partial row object nested in broken JSON is not chosen first.
    Returns ``None`` when no complete JSON object or array exists.
    """

    decoder = json.JSONDecoder()
    fallback: Any | None = None
    for match in re.finditer(r"[\[{]", text):
        try:
            value, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if _is_structured_table_payload(value):
            return value
        if isinstance(value, list) and value and all(
            _is_structured_table_payload(item) for item in value
        ):
            return value
        if fallback is None:
            fallback = value
    return fallback


def _loads_json_payload(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = _collapse_doubled_json_braces(text)
        if repaired != text:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                payload = _decode_first_json_value(repaired)
                if payload is not None:
                    return payload
        payload = _decode_first_json_value(text)
        if payload is None:
            raise
        return payload


def parse_json_object(content: str) -> dict[str, Any]:
    """Parse one structured JSON object from an LLM response.

    Some models wrap every table row in its own object and return a
    top-level array instead of the single object the prompt asks for;
    array responses are merged into one payload. Fenced responses are
    extracted first; anything else is parsed by locating the first
    complete JSON value, so leading prose and trailing text do not break
    extraction. Doubled braces copied from escaped prompt examples are
    collapsed before parsing.
    """
    text = content.strip()
    if not text:
        raise BlockDiagramTableError("The LLM returned an empty response.")
    fenced = re.search(r"```(?:json)?\s*([\[{].*[\]}])\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        payload = _loads_json_payload(text)
    except json.JSONDecodeError as exc:
        raise BlockDiagramTableError(
            "The LLM response was not valid JSON."
        ) from exc
    if isinstance(payload, list):
        payload = merge_table_entries(payload)
    if not isinstance(payload, dict):
        raise BlockDiagramTableError(
            "The LLM response JSON must be an object."
        )
    return payload


def merge_table_entries(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge a top-level JSON array of table entries into one payload.

    Some models return one ``{equipment_family, string_info}`` object per
    schedule row; concatenate the string_info rows and require a consistent
    equipment_family across every entry.
    """
    if not entries:
        raise BlockDiagramTableError("The LLM returned an empty JSON array.")
    families: list[str] = []
    rows: list[Any] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise BlockDiagramTableError(
                f"The LLM JSON array item {index} must be an object."
            )
        missing = [
            name for name in ("equipment_family", "string_info")
            if name not in entry
        ]
        if missing:
            raise BlockDiagramTableError(
                "The LLM JSON array item is missing required fields: "
                + ", ".join(missing)
            )
        families.append(str(entry["equipment_family"]))
        info = entry["string_info"]
        if not isinstance(info, list):
            raise BlockDiagramTableError(
                f"The LLM JSON array item {index} string_info must be a list."
            )
        rows.extend(info)
    unique = {family.strip().upper() for family in families if family.strip()}
    if len(unique) > 1:
        raise BlockDiagramTableError(
            "The LLM returned mixed equipment families: "
            + ", ".join(sorted(unique))
            + "."
        )
    return {"equipment_family": families[0], "string_info": rows}


def validate_structured_table(payload: Mapping[str, Any]) -> None:
    required = ("equipment_family", "string_info")
    missing = [name for name in required if name not in payload]
    if missing:
        raise BlockDiagramTableError(
            "The LLM JSON is missing required fields: "
            + ", ".join(missing)
        )
    string_info = payload["string_info"]
    if not isinstance(string_info, list):
        raise BlockDiagramTableError(
            "The LLM JSON string_info must be a list."
        )
    for index, item in enumerate(string_info):
        if not isinstance(item, Mapping):
            raise BlockDiagramTableError(
                f"The LLM JSON string_info[{index}] must be an object."
            )
        if "node" not in item or "string_number" not in item:
            raise BlockDiagramTableError(
                "Each string_info item must include node and string_number."
            )


def normalize_equipment_family(raw: str) -> str:
    normalized = raw.strip().upper()
    if normalized in {"CBX", "LBD"}:
        return normalized
    if normalized in {"LBD", "LOAD BREAK DISCONNECT", "LOAD BREAK"} or (
        "LBD" in normalized or "LOAD BREAK" in normalized or "LYNX" in normalized
    ):
        return "LBD"
    if normalized in {"COMBINER", "COMBINER BOX"} or any(
        token in normalized for token in ("CBX", "COMBINER")
    ):
        return "CBX"
    raise BlockDiagramTableError(
        "The LLM JSON equipment_family must map to 'CBX' or 'LBD'."
    )


# Backward-compatible helper name for callers that imported the old symbol.
normalize_project_type = normalize_equipment_family


def extract_table_json_from_image(
    llm: BedrockLlmProvider | OpenRouterLlmProvider,
    image_bytes: bytes | Sequence[bytes],
    prompt: str,
    *,
    bedrock_max_transport_bytes: int | None = None,
    provider_name: str = "Claude",
) -> LlmExtraction:
    """Extract the schedule JSON from one image or an ordered set of images.

    ``image_bytes`` may be a sequence, in which case every crop is compressed
    and sent in a single request, in order. This is how the no-detection
    fallback hands the whole page to the model.
    """

    raw_images = (
        [image_bytes]
        if isinstance(image_bytes, (bytes, bytearray))
        else list(image_bytes)
    )
    if not raw_images:
        raise BlockDiagramTableError("No table image was provided.")
    prepared: list[tuple[bytes, str]] = [
        compress_image_for_bedrock(
            image,
            max_transport_bytes=bedrock_max_transport_bytes,
        )
        for image in raw_images
    ]
    try:
        completion = llm.complete_with_images(prompt, prepared)
    except LlmServiceError as exc:
        raise BlockDiagramTableError(
            f"{provider_name} table extraction failed: {exc}"
        ) from exc
    raw_output = llm_raw_output(completion)
    try:
        payload = parse_json_object(completion.content)
        validate_structured_table(payload)
    except BlockDiagramTableError as exc:
        raise BlockDiagramTableError(
            str(exc),
            raw_llm={**raw_output, "prompt": prompt},
        ) from exc
    return LlmExtraction(payload=payload, raw_output=raw_output)


def build_result(
    structured: Mapping[str, Any],
    *,
    strategy: str,
    table_bbox: BoundingBox,
    cropped_image_path: Path,
    markdown: str = "",
    raw_output: Mapping[str, Any] | None = None,
) -> BlockDiagramTableResult:
    rows = tuple(
        BlockDiagramTableRow(
            node=str(item["node"]),
            string_number=int(item["string_number"]),
        )
        for item in structured["string_info"]
        if isinstance(item, Mapping)
        and item.get("node") not in (None, "")
    )
    if not rows:
        raise BlockDiagramTableError(
            "The LLM did not extract any table rows."
        )
    equipment_family = normalize_equipment_family(
        str(structured["equipment_family"])
    )
    return BlockDiagramTableResult(
        project_type="IBEX" if equipment_family == "CBX" else "LYNX",
        equipment_family=equipment_family,
        string_info=rows,
        strategy=strategy,
        table_bbox=table_bbox,
        cropped_image_path=Path(cropped_image_path),
        markdown=markdown,
        raw_llm=dict(structured),
        raw_output=dict(raw_output) if raw_output else {},
    )

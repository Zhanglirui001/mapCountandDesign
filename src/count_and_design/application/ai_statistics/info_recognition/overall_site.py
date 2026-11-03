"""Agent2 extraction for overall-site layout pages.

YOLO first locates the target table bounding boxes. A plausible weak INV box
is included before MinerU is used as a result-driven fallback. Crops from
Agent1's high-resolution PNG are sent to a vision model for schema-constrained
extraction: GLM through OpenRouter by default (``*_glm`` strategies), or
Claude on Bedrock (``*_claude`` strategies).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
import hashlib
from io import BytesIO
import json
import logging
from math import ceil, floor
from queue import Empty, Queue
import re
from threading import Thread
import time
from time import perf_counter
from typing import Any

from PIL import Image, ImageOps

from count_and_design.application.ai import (
    BoundingBox,
    DetectionResult,
    LlmServiceError,
    MinerUParseResult,
    MinerUOcrService,
    OnnxRuntimeYoloDetector,
    OVERALL_SCHEDULE_MODEL_FILENAME,
    OVERALL_SCHEDULE_MODEL_TYPE,
    bundled_yolo_model_dir,
)
from count_and_design.application.ai.llm.providers import (
    BedrockLlmProvider,
    OpenRouterLlmProvider,
)
from count_and_design.application.ai.yolo.runtime import resolve_yolo_model_path
from count_and_design.application.config import (
    BedrockLlmConfig,
    OpenRouterLlmConfig,
    OverallSiteExtractionConfig,
)
from count_and_design.application.image_transport import (
    compress_image_for_bedrock,
)

logger = logging.getLogger(__name__)

EXTRACTION_SCOPE = "overall_site_tracker_fields_and_inv_summary"
STRATEGY_MINERU_CLAUDE = "mineru_claude"
STRATEGY_YOLO_CLAUDE = "yolo_claude"
STRATEGY_MINERU_GLM = "mineru_glm"
STRATEGY_YOLO_GLM = "yolo_glm"
_STRATEGIES = (
    STRATEGY_YOLO_GLM,
    STRATEGY_MINERU_GLM,
    STRATEGY_YOLO_CLAUDE,
    STRATEGY_MINERU_CLAUDE,
)
_OVERALL_YOLO_LABELS = frozenset({"inv_tree", "tracker_table"})
# Match the overall_schedule yolov26s model's 1280px input; boxes are
# mapped back to the original high-resolution page.
_YOLO_DETECT_MAX_SIDE = 1280
_INV_RECOVERY_IOU = 0.3
_MAX_INV_RECOVERY_TABLES = 3
_JSON_EXTRACTION_ATTEMPTS = 2
_JSON_RETRY_INSTRUCTION = """CORRECTION: Your previous response could not be parsed as a complete JSON object.
Return the entire response again as strict JSON only. Close every string, array,
and object; do not use markdown fences or add commentary."""
_LOGGABLE_JSON_KEYS = frozenset(
    {
        "pages",
        "page_number",
        "extraction_scope",
        "detected_module_column_count",
        "tracker_ocr_fields",
        "column_index",
        "module_model",
        "module_stc_dc_rating_w",
        "string_count",
        "num_str",
        "num_str_details",
        "inv_list",
        "inv_count",
        "warnings",
    }
)
SYSTEM_PROMPT = """You extract structured verification data from table images cropped from an overall site layout engineering drawing.
Return only valid JSON. Start with { and end with }.
Do not include thinking, analysis, markdown fences, or commentary."""

USER_PROMPT_TEMPLATE = """The attached image contains one or more table crops from a single overall_site_layout drawing page. A layout detector supplied only their coordinates; do all reading from the image. Extract two independent result groups:
1. Tracker OCR summary fields from the SYSTEMS SPECIFICATIONS table.
2. Equipment identifiers from the summary or equipment table used to build the INV tree.

Output contract:
- The information-recognition UI consumes tracker_ocr_fields, inv_list, and
  warnings directly.
- Do not return per-INV string-count columns or inv_rows. Extract only the
  identifier list for inv_list; ignore MODULE STRINGS / STRINGS columns to the
  right of each identifier.
- Agent4 deterministically converts this response to {"tracker_totals":[{"tracker_type":"<N>-string","power_counts":{"<rating>W":<integer>},"total":<integer>}],"grand_total":<integer>}. It derives those values from module_stc_dc_rating_w plus num_str_details.count. Therefore every visible physical module column and every numbered STR/STRING count cell must be preserved exactly once. Do not calculate or emit agent4 totals yourself.
- Use JSON null/[] for unreadable or absent values; never guess. Put a concise warning in warnings when a target table or required value is not visible.

Tracker OCR summary interpretation:
- MODULE MODEL is a row label. Every value cell in that row is a separate module/model column.
- For each module/model column, read values from the same column for:
  - MODULE STC DC RATING (W): module wattage. Add W suffix when the OCR value is a bare number.
  - STRING COUNT: total string count for that module/model column.
  - Num-STR: all rows whose label starts with a number followed by STR or
    STRING. The words after STR/STRING are project-specific descriptors and
    must not control whether the row is extracted. For example, treat 18-STR,
    3 STRING TRACKER, 4 STRING HARNESS COUNT, 4 STRING TABLE, 2 STR COUNT,
    and other "<number> STR/STRING ..." labels as equivalent tracker-type
    rows. The leading number is the tracker type; numeric cells in that row
    are its counts. Do not confuse these rows with the separate STRING COUNT
    total row, which does not start with a number. Normalize tracker types as
    "<number>-STR" in num_str and num_str_details. Exclude blank, null, --,
    -, and 0 values.
- Preserve every physical module/model column from left to right. Never merge
  columns, even when MODULE MODEL and MODULE STC DC RATING are identical;
  downstream code assigns a unique column_id to each list item.
- First count the physical value columns using MODULE MODEL, MODULE STC DC
  RATING (W), STRING COUNT, and the numbered STR/STRING rows as anchors.
  Return that count as detected_module_column_count. Then return exactly that
  many tracker_ocr_fields items, one for every physical column from left to
  right. Each item must include its 1-based column_index. Repeated model names
  are still separate columns when their cells occupy separate positions.
- Do not invent missing values. Use null or [] when not present.

INV summary interpretation:
- The identifiers used to build the INV tree are usually in the first
  (leftmost) column of a summary or equipment table. Treat that position as a
  strong hint, not an absolute requirement, and extract its body cells from
  top to bottom.
- Do not require a fixed column header. Depending on the design company, the
  header may be INV, PCS, PCS ID, INVERTER ID, another project-specific name,
  or may be unclear. Identify the column primarily from its body cells: it
  contains the ordered, distinct equipment identifiers, typically one per
  data row. A nonstandard header alone is never a reason to return an empty
  inv_list.
- Preserve values such as PCS-0001 and 01ED-PCS-0001; PCS in the header or
  identifier does not make it invalid. If several columns appear plausible,
  prefer the leftmost identifier-like column and add a concise warning only
  when the choice remains genuinely ambiguous.
- INV identifiers may be numeric or alphanumeric. Values such as 1101,
  11A01, INV1101, and INV11A01 are all valid.
- Preserve the exact identifier format recognized in each source cell. Do not
  add or remove an INV prefix, insert or remove spaces or separators, or
  change letter case. Only trim whitespace outside the cell value. For
  example, keep 11A01 as "11A01", INV11A01 as "INV11A01", and
  INV 11A01 as "INV 11A01".
- Preserve table order and remove duplicates.
- Do not include TOTAL or SUBTOTAL rows. Do not put tracker counts, block
  ratings, DC/AC ratios, or other non-string values into inv_list.
- Do not treat AREA, BLOCK, or ZONE map labels (for example AREA 1, BLOCK A,
  ZONE 3) as inv_list identifiers.
- Ignore per-INV string-count columns such as JINKO 590W MODULE STRINGS or
  FIRST SOLAR SERIES 6+ 460W STRINGS even when they are visible beside the
  identifier column.

Required JSON schema:
{{
  "extraction_scope": "overall_site_tracker_fields_and_inv_summary",
  "detected_module_column_count": number,
  "tracker_ocr_fields": [
    {{
      "column_index": number,
      "module_model": {{
        "label": "MODULE MODEL",
        "meaning": "board brand/model",
        "value": string|null,
        "source": string|null
      }},
      "module_stc_dc_rating_w": {{
        "label": "MODULE STC DC RATING (W)",
        "meaning": "module wattage",
        "value": string|null,
        "source": string|null
      }},
      "string_count": {{
        "label": "STRING COUNT",
        "meaning": "total string count",
        "value": string|null,
        "source": string|null
      }},
      "num_str": {{
        "label": "Num-STR",
        "meaning": "specific string types",
        "value": string|null,
        "source": string|null
      }},
      "num_str_details": [
        {{
          "num_str": string,
          "count": string|null,
          "source": string|null
        }}
      ]
    }}
  ],
  "inv_list": ["11A01", "11A02"],
  "inv_count": number,
  "warnings": [string]
}}

Example: if one SYSTEMS SPECIFICATIONS table has rows STRING COUNT=9767,
3 STRING TRACKER=2779, 2 STRING TRACKER=715, return string_count "9767",
num_str "3-STR, 2-STR", and details 3-STR:2779, 2-STR:715.

Example wording variant: if rows are 4 STRING HARNESS COUNT=1271 and
2 STRING HARNESS COUNT=91, return details 4-STR:1271 and 2-STR:91.
The same rule applies to 4 STRING TABLE and 2 STRING TABLE.

Read every crop in the attached image. Crops are ordered top-to-bottom by their position on the source drawing. Return only the required JSON object."""

BATCH_USER_PROMPT_SUFFIX = """

BATCH MODE OVERRIDE:
- Multiple images are attached. Each image is a crop montage for exactly one
  drawing page, in the order listed below.
- Read and return each page independently. Never move values between pages or
  merge repeated pages yourself.
- Return one top-level JSON object with a `pages` array. Every item must contain
  `page_number` plus all fields from the required single-page schema above.
- The page order and image order are:
{page_mapping}

Required batch envelope:
{{"pages": [{{"page_number": 17, "extraction_scope":
"overall_site_tracker_fields_and_inv_summary", "detected_module_column_count":
0, "tracker_ocr_fields": [], "inv_list": [], "inv_count": 0,
"warnings": []}}]}}

Return only the batch JSON object."""


class Agent2OverallSiteError(RuntimeError):
    """Raised when overall-site OCR or structured extraction fails."""


class Agent2Cancelled(Agent2OverallSiteError):
    """Raised when cooperative cancellation is requested for Agent2."""


Agent2CancellationCheck = Callable[[], bool]


def _raise_if_cancelled(
    is_cancelled: Agent2CancellationCheck | None,
) -> None:
    if is_cancelled is not None and is_cancelled():
        raise Agent2Cancelled("Information recognition was cancelled.")


def _run_cancellable(
    operation: Callable[[], Any],
    is_cancelled: Agent2CancellationCheck | None,
) -> Any:
    """Run a blocking provider call without trapping the Agent2 worker."""

    if is_cancelled is None:
        return operation()

    result_queue: Queue[tuple[bool, Any]] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            result_queue.put((True, operation()))
        except Exception as exc:
            result_queue.put((False, exc))

    Thread(target=invoke, name="agent2-provider-call", daemon=True).start()
    while True:
        _raise_if_cancelled(is_cancelled)
        try:
            succeeded, value = result_queue.get(timeout=0.05)
        except Empty:
            continue
        if succeeded:
            return value
        if isinstance(value, BaseException):
            raise value
        raise Agent2OverallSiteError("Agent2 provider call failed.")


@dataclass(frozen=True)
class TrackerFieldValue:
    label: str
    meaning: str
    value: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class NumStrDetail:
    num_str: str
    count: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class InvStringCount:
    column_index: int
    label: str
    count: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class InvOcrRow:
    identifier: str
    string_counts: tuple[InvStringCount, ...] = ()


@dataclass(frozen=True)
class TrackerOcrFields:
    module_model: TrackerFieldValue
    module_stc_dc_rating_w: TrackerFieldValue
    string_count: TrackerFieldValue
    num_str: TrackerFieldValue
    num_str_details: tuple[NumStrDetail, ...] = ()


@dataclass(frozen=True)
class OverallSiteExtraction:
    page_number: int
    extraction_scope: str = EXTRACTION_SCOPE
    tracker_ocr_fields: tuple[TrackerOcrFields, ...] = ()
    inv_list: tuple[str, ...] = ()
    inv_rows: tuple[InvOcrRow, ...] = ()
    inv_count: int = 0
    warnings: tuple[str, ...] = ()
    ocr_markdown: str = field(default="", repr=False, compare=False)
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass
class _PreparedOverallPage:
    page_number: int
    png_bytes: bytes
    decoded: Image.Image
    table_content: Sequence[dict[str, Any]]
    locate_meta: dict[str, Any]
    table_image: bytes
    table_bboxes: list[list[int]]
    table_bbox_labels: list[str]


def build_agent4_input(
    extractions: Sequence[OverallSiteExtraction],
) -> dict[str, Any]:
    """Build Agent4's tracker-total JSON payload from Agent2 OCR results.

    Every OCR module column receives a stable ID so duplicate power/model
    columns remain distinct. Tracker counts refer to those IDs. A missing
    value is represented by an empty JSON object and is never converted to
    zero.
    """

    grouped: dict[str, dict[str, Any]] = {}
    module_columns: list[dict[str, Any]] = []

    for extraction in extractions:
        for tracker in extraction.tracker_ocr_fields:
            column_id = f"module_{len(module_columns) + 1}"
            power = _normalize_power(tracker.module_stc_dc_rating_w.value)
            model = _optional_nonempty_string(tracker.module_model.value)
            string_count = _parse_count(tracker.string_count.value)
            module_column = {
                "column_id": column_id,
                "module_stc_dc_rating_w": power if power else {},
                "module_model": model if model else {},
                "string_count": (
                    string_count if string_count is not None else {}
                ),
                "tracker_count": {},
            }
            module_columns.append(module_column)

            details = list(tracker.num_str_details)
            if not details:
                details = [
                    NumStrDetail(num_str=item)
                    for item in _split_tracker_types(tracker.num_str.value)
                ]
            for detail in details:
                tracker_type = _normalize_tracker_type(detail.num_str)
                if not tracker_type:
                    continue
                row = grouped.setdefault(
                    tracker_type,
                    {
                        "tracker_type": tracker_type,
                        "module_counts": {},
                    },
                )
                count = _parse_count(detail.count)
                row["module_counts"][column_id] = (
                    count if count is not None else {}
                )

    column_ids = [column["column_id"] for column in module_columns]
    tracker_totals: list[dict[str, Any]] = []
    column_tracker_totals: dict[str, int] = {key: 0 for key in column_ids}
    column_has_tracker_count: set[str] = set()
    grand_total = 0
    has_grand_total = False
    for row in grouped.values():
        raw_counts = row["module_counts"]
        module_counts = {
            column_id: raw_counts.get(column_id, {})
            for column_id in column_ids
        }
        valid_counts = [
            value for value in module_counts.values() if isinstance(value, int)
        ]
        for column_id, value in module_counts.items():
            if isinstance(value, int):
                column_tracker_totals[column_id] += value
                column_has_tracker_count.add(column_id)
        total = sum(valid_counts) if valid_counts else {}
        if valid_counts:
            grand_total += sum(valid_counts)
            has_grand_total = True
        tracker_totals.append(
            {
                "tracker_type": row["tracker_type"],
                "module_counts": module_counts,
                "total": total,
            }
        )

    for column in module_columns:
        column_id = column["column_id"]
        if column_id in column_has_tracker_count:
            column["tracker_count"] = column_tracker_totals[column_id]

    valid_string_counts = [
        column["string_count"]
        for column in module_columns
        if isinstance(column["string_count"], int)
    ]
    return {
        "module_columns": module_columns,
        "string_count_total": (
            sum(valid_string_counts) if valid_string_counts else {}
        ),
        "tracker_totals": tracker_totals,
        "grand_total": grand_total if has_grand_total else {},
    }


def build_agent4_json(
    extractions: Sequence[OverallSiteExtraction],
    *,
    indent: int | None = 2,
) -> str:
    """Serialize :func:`build_agent4_input` as JSON for Agent4."""

    return json.dumps(
        build_agent4_input(extractions),
        ensure_ascii=False,
        indent=indent,
    )


def build_agent4_validation_input(
    extractions: Sequence[OverallSiteExtraction],
) -> dict[str, Any]:
    """Build the compact Agent2 payload consumed by Agent4 validation.

    The recognition UI keeps counts by physical module column so duplicate
    module columns remain reviewable. Agent4 instead consumes counts grouped
    by module power, so duplicate power columns are summed here.
    """

    review_payload = build_agent4_input(extractions)
    power_by_column = {
        column["column_id"]: column["module_stc_dc_rating_w"]
        for column in review_payload["module_columns"]
        if isinstance(column.get("module_stc_dc_rating_w"), str)
    }
    tracker_totals: list[dict[str, Any]] = []
    for tracker_total in review_payload["tracker_totals"]:
        power_counts: dict[str, int] = {}
        module_counts = tracker_total.get("module_counts", {})
        if isinstance(module_counts, Mapping):
            for column_id, count in module_counts.items():
                power = power_by_column.get(column_id)
                if power and isinstance(count, int):
                    power_counts[power] = power_counts.get(power, 0) + count
        tracker_totals.append(
            {
                "tracker_type": tracker_total["tracker_type"],
                "power_counts": power_counts,
                "total": tracker_total["total"],
            }
        )

    return {
        "tracker_totals": tracker_totals,
        "grand_total": review_payload["grand_total"],
        "inv_string_counts": _agent4_inv_string_counts(extractions),
    }


class Agent2OverallSiteRecognizer:
    """Locate overall-layout tables with YOLO/MinerU and read them with vision."""

    def __init__(
        self,
        mineru: MinerUOcrService | None = None,
        *,
        config: OverallSiteExtractionConfig | None = None,
        bedrock: BedrockLlmProvider | None = None,
        glm: OpenRouterLlmProvider | None = None,
        overall_schedule_detector: OnnxRuntimeYoloDetector | None = None,
    ) -> None:
        self.config = config or OverallSiteExtractionConfig.from_env()
        self.mineru = mineru or MinerUOcrService()
        self.bedrock = bedrock
        self.glm = glm
        self._overall_schedule_detector = overall_schedule_detector

    def _vision_llm(self) -> BedrockLlmProvider | OpenRouterLlmProvider:
        """Return the configured vision provider for table extraction."""

        if self.config.uses_glm:
            if self.glm is not None:
                return self.glm
            return OpenRouterLlmProvider(
                replace(
                    OpenRouterLlmConfig.from_env(),
                    model=self.config.openrouter_model,
                )
            )
        return self.bedrock or BedrockLlmProvider(BedrockLlmConfig.from_env())

    def recognize_page(
        self,
        page_number: int,
        png_bytes: bytes,
        *,
        on_progress: Callable[[int, int], None] | None = None,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> OverallSiteExtraction:
        _raise_if_cancelled(is_cancelled)
        if page_number <= 0:
            raise Agent2OverallSiteError("Page number must be positive.")
        if not png_bytes:
            raise Agent2OverallSiteError("Overall-site PNG is empty.")
        with Image.open(BytesIO(png_bytes)) as loaded:
            decoded = loaded.convert("RGB")
        table_content, locate_meta = self._locate_table_content(
            page_number,
            png_bytes,
            decoded=decoded,
            is_cancelled=is_cancelled,
        )
        _raise_if_cancelled(is_cancelled)
        if on_progress is not None:
            on_progress(1, 2)
        table_image, table_bboxes, table_bbox_labels = _crop_detected_tables(
            decoded,
            table_content,
        )
        _raise_if_cancelled(is_cancelled)
        payload = self._extract(table_image, is_cancelled=is_cancelled)
        payload = self._retry_incomplete_tracker_columns(
            table_image,
            payload,
            is_cancelled=is_cancelled,
        )
        (
            payload,
            locate_meta,
            table_bboxes,
            table_bbox_labels,
        ) = self._recover_missing_inv_tables(
            page_number,
            png_bytes,
            decoded,
            table_content,
            locate_meta,
            table_bboxes,
            table_bbox_labels,
            payload,
            is_cancelled=is_cancelled,
        )
        _raise_if_cancelled(is_cancelled)
        if on_progress is not None:
            on_progress(2, 2)
        extraction = _parse_extraction(page_number, "", payload)
        return replace(
            extraction,
            raw={
                **extraction.raw,
                "table_bboxes": table_bboxes,
                "table_bbox_labels": table_bbox_labels,
                "table_count": len(table_bboxes),
                **locate_meta,
                "vision_provider": self.config.vision_provider,
            },
        )

    def _locate_table_content(
        self,
        page_number: int,
        png_bytes: bytes,
        *,
        decoded: Image.Image | None = None,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> tuple[Sequence[dict[str, Any]], dict[str, Any]]:
        strategy = self.config.strategy.casefold()
        if strategy.startswith("mineru"):
            started = perf_counter()
            layout = self._run_mineru(
                page_number,
                png_bytes,
                is_cancelled=is_cancelled,
            )
            return layout.content_list, {
                "table_bbox_source": "mineru",
                "yolo_seconds": 0.0,
                "mineru_seconds": round(perf_counter() - started, 3),
                "yolo_confidence": None,
            }
        if not strategy.startswith("yolo"):
            raise Agent2OverallSiteError(
                f"Unknown Agent2 table strategy: {self.config.strategy!r}. "
                f"Expected one of: {', '.join(repr(name) for name in _STRATEGIES)}."
            )

        yolo_started = perf_counter()
        detections: Sequence[DetectionResult] = ()
        yolo_error = ""
        page_image = decoded
        try:
            if page_image is None:
                with Image.open(BytesIO(png_bytes)) as loaded:
                    page_image = loaded.convert("RGB")
            detector = self._schedule_detector()
            detect_image, scale_x, scale_y = _downscale_decoded(
                page_image,
                _yolo_detect_max_side(detector),
            )
            candidate_confidence = min(
                self.config.yolo_detection_confidence,
                self.config.yolo_weak_inv_confidence,
            )
            detections = _run_cancellable(
                lambda: detector.detect(
                    detect_image,
                    confidence=candidate_confidence,
                ),
                is_cancelled,
            )
            if scale_x != 1.0 or scale_y != 1.0:
                detections = _scale_detections(detections, scale_x, scale_y)
        except Agent2Cancelled:
            raise
        except Exception as exc:
            # A missing/corrupt local model must not remove the established
            # MinerU path from production.
            yolo_error = str(exc)
        yolo_seconds = round(perf_counter() - yolo_started, 3)
        image_width, image_height = (
            page_image.size if page_image is not None else (0, 0)
        )
        selected, best_confidence, weak_labels = _select_overall_yolo_tables(
            detections,
            min_confidence=self.config.yolo_fallback_confidence,
            weak_inv_confidence=self.config.yolo_weak_inv_confidence,
            image_width=image_width,
            image_height=image_height,
        )
        logger.info(
            "agent2.yolo page=%d seconds=%.3f detections=%d selected=%d "
            "best_confidence=%s error=%s",
            page_number,
            yolo_seconds,
            len(detections),
            len(selected),
            None if best_confidence is None else round(best_confidence, 3),
            yolo_error or "-",
        )
        if selected:
            if image_width <= 0:
                with Image.open(BytesIO(png_bytes)) as source:
                    image_width, image_height = source.size
            table_content = _detections_to_table_content(
                _expand_yolo_detections(
                    selected,
                    image_width=image_width,
                    image_height=image_height,
                    expansion_ratio=self.config.yolo_bbox_expansion_ratio,
                ),
                image_width=image_width,
                image_height=image_height,
            )
            return table_content, {
                "table_bbox_source": "yolo",
                "yolo_seconds": yolo_seconds,
                "mineru_seconds": 0.0,
                "yolo_confidence": best_confidence,
                "yolo_labels": [item.label for item in selected],
                "weak_yolo_labels": weak_labels,
                "yolo_bbox_expansion_ratio": self.config.yolo_bbox_expansion_ratio,
            }

        mineru_started = perf_counter()
        try:
            layout = self._run_mineru(
                page_number,
                png_bytes,
                is_cancelled=is_cancelled,
            )
        except Agent2Cancelled:
            raise
        except Exception as exc:
            confidence_detail = (
                "no known table detection"
                if best_confidence is None
                else f"best confidence {best_confidence:.1%}"
            )
            yolo_detail = f"; YOLO failed: {yolo_error}" if yolo_error else ""
            raise Agent2OverallSiteError(
                "YOLO did not provide an overall-layout table bounding box "
                f"above the configured {self.config.yolo_fallback_confidence:.1%} "
                f"threshold ({confidence_detail}){yolo_detail}; "
                f"MinerU fallback also failed: {exc}"
            ) from exc
        meta: dict[str, Any] = {
            "table_bbox_source": "mineru",
            "yolo_seconds": yolo_seconds,
            "mineru_seconds": round(perf_counter() - mineru_started, 3),
            "yolo_confidence": best_confidence,
        }
        if yolo_error:
            meta["yolo_error"] = yolo_error
        return layout.content_list, meta

    def _schedule_detector(self) -> OnnxRuntimeYoloDetector:
        if self._overall_schedule_detector is None:
            model_path = resolve_yolo_model_path(
                bundled_yolo_model_dir(),
                OVERALL_SCHEDULE_MODEL_TYPE,
                OVERALL_SCHEDULE_MODEL_FILENAME,
            )
            detector = OnnxRuntimeYoloDetector(
                model_path,
                confidence=self.config.yolo_detection_confidence,
                source=f"onnxruntime:{OVERALL_SCHEDULE_MODEL_TYPE}",
            )
            # Agent2 shares the GUI process with Qt. Saturating every CPU
            # during the first ONNX session makes the UI look frozen.
            detector.configure_parallelism(1, 4)
            detector.warmup()
            self._overall_schedule_detector = detector
        return self._overall_schedule_detector

    def _retry_incomplete_tracker_columns(
        self,
        table_image: bytes,
        payload: dict[str, Any],
        *,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> dict[str, Any]:
        issue = _tracker_column_completeness_issue(payload)
        if issue is None:
            return payload
        correction = (
            "CORRECTION REQUIRED: The previous JSON did not preserve every "
            f"physical module column. {issue} Re-read the OCR table column by "
            "column. Return detected_module_column_count and exactly one "
            "tracker_ocr_fields item per physical value column, with "
            "column_index values 1..N. Do not merge columns with repeated "
            "MODULE MODEL values.\n\nPrevious JSON:\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            retried = self._extract(
                table_image,
                correction=correction,
                is_cancelled=is_cancelled,
            )
        except Agent2Cancelled:
            raise
        except Agent2OverallSiteError:
            return _append_extraction_warning(payload, issue)
        retry_issue = _tracker_column_completeness_issue(retried)
        if retry_issue is not None:
            return _append_extraction_warning(retried, retry_issue)
        return retried

    def _retry_invalid_inv_selection(
        self,
        table_image: bytes,
        payload: dict[str, Any],
        *,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> dict[str, Any]:
        issue = _inv_selection_issue(payload)
        if issue is None:
            return payload
        correction = (
            "CORRECTION REQUIRED: The previous inv_list contains values that "
            f"are not equipment identifiers. {issue} Re-read the summary or "
            "equipment table. The identifier column is usually the first "
            "column, but its header may be INV, PCS, PCS ID, or another "
            "project-specific name. Choose it from the identifier-like body "
            "cells rather than requiring a particular header. PCS row "
            "identifiers such as PCS-0001 and 01ED-PCS-0001 are valid. Do not "
            "return per-INV string-count columns. Preserve tracker_ocr_fields "
            "as well.\n\n"
            "Previous JSON:\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            retried = self._extract(
                table_image,
                correction=correction,
                is_cancelled=is_cancelled,
            )
        except Agent2Cancelled:
            raise
        except Agent2OverallSiteError:
            return _append_inv_warning(payload, issue)
        # The retry is only authoritative for INV fields. Keeping the original
        # tracker payload prevents a focused correction from regressing an
        # already-valid SYSTEMS SPECIFICATIONS extraction.
        merged = {
            **payload,
            "inv_list": retried.get("inv_list", payload.get("inv_list")),
            "inv_count": retried.get("inv_count", payload.get("inv_count")),
            "warnings": _merge_warnings(payload, retried),
        }
        retry_issue = _inv_selection_issue(merged)
        if retry_issue is not None:
            return _append_inv_warning(merged, retry_issue)
        return merged

    def _recover_missing_inv_tables(
        self,
        page_number: int,
        png_bytes: bytes,
        decoded: Image.Image,
        table_content: Sequence[dict[str, Any]],
        locate_meta: dict[str, Any],
        table_bboxes: list[list[int]],
        table_bbox_labels: list[str],
        payload: dict[str, Any],
        *,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        list[list[int]],
        list[str],
    ]:
        """Use MinerU only after cheap YOLO/companion crops produced no INV."""

        if locate_meta.get("table_bbox_source") != "yolo":
            return payload, locate_meta, table_bboxes, table_bbox_labels
        if _normalize_inv_list(payload.get("inv_list")):
            return payload, locate_meta, table_bboxes, table_bbox_labels

        mineru_started = perf_counter()
        try:
            layout = self._run_mineru(
                page_number,
                png_bytes,
                is_cancelled=is_cancelled,
            )
        except Agent2Cancelled:
            raise
        except Exception as exc:
            logger.info(
                "agent2.inv_recovery page=%d skipped mineru_error=%s",
                page_number,
                exc,
            )
            return payload, {
                **locate_meta,
                "inv_recovery": "mineru_error",
            }, table_bboxes, table_bbox_labels

        candidates = _non_overlapping_table_items(
            table_content,
            layout.content_list,
            decoded.width,
            decoded.height,
        )
        candidates = [
            item
            for item in candidates
            if _looks_like_identifier_table_item(
                item,
                decoded.width,
                decoded.height,
            )
        ]
        extra = _rank_inv_recovery_items(
            candidates,
            table_content,
            decoded.width,
            decoded.height,
        )[:_MAX_INV_RECOVERY_TABLES]
        locate_meta = {
            **locate_meta,
            "mineru_seconds": round(perf_counter() - mineru_started, 3),
        }
        if not extra:
            return payload, {
                **locate_meta,
                "inv_recovery": "no_extra_table",
            }, table_bboxes, table_bbox_labels

        reliable_content = [
            item for item in table_content if not item.get("speculative")
        ]
        recovered_content = [*reliable_content, *extra]
        recovered_image, recovered_bboxes, recovered_labels = _crop_detected_tables(
            decoded,
            recovered_content,
        )
        correction = (
            "CORRECTION REQUIRED: The previous inv_list is empty. Additional "
            "table crops from the same drawing page are now included. Extract "
            "equipment identifiers for inv_list from those tables. Do not "
            "return AREA/BLOCK/ZONE map labels or per-INV string-count "
            "columns. Preserve the already-valid tracker_ocr_fields.\n\n"
            "Previous JSON:\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            recovered = self._extract(
                recovered_image,
                correction=correction,
                is_cancelled=is_cancelled,
            )
        except Agent2Cancelled:
            raise
        except Agent2OverallSiteError as exc:
            logger.info(
                "agent2.inv_recovery page=%d extra_tables=%d extract_error=%s",
                page_number,
                len(extra),
                exc,
            )
            return payload, {
                **locate_meta,
                "inv_recovery": "extract_error",
            }, table_bboxes, table_bbox_labels

        recovered_invs = _normalize_inv_list(recovered.get("inv_list"))
        if not recovered_invs:
            return payload, {
                **locate_meta,
                "inv_recovery": "empty",
            }, table_bboxes, table_bbox_labels
        merged = {
            **payload,
            "inv_list": list(recovered_invs),
            "inv_count": len(recovered_invs),
            "warnings": _merge_warnings(payload, recovered),
        }
        return merged, {
            **locate_meta,
            "table_bbox_source": "yolo+mineru",
            "inv_recovery": "recovered",
        }, recovered_bboxes, recovered_labels

    def recognize_pages(
        self,
        page_images: Mapping[int, bytes],
        *,
        on_progress: Callable[[int, int], None] | None = None,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> tuple[OverallSiteExtraction, ...]:
        page_numbers = sorted(page_images)
        if len(page_numbers) == 1:
            page_number = page_numbers[0]

            def report(step: int, _page_total: int) -> None:
                if on_progress is not None:
                    on_progress(step, 2)

            results = [
                self.recognize_page(
                    page_number,
                    page_images[page_number],
                    on_progress=report,
                    is_cancelled=is_cancelled,
                )
            ]
        else:
            prepared = self._prepare_all_overall_pages(
                page_images,
                on_progress=on_progress,
                is_cancelled=is_cancelled,
            )
            results = self._extract_prepared_pages(
                prepared,
                on_progress=on_progress,
                is_cancelled=is_cancelled,
            )
        _raise_if_cancelled(is_cancelled)
        return _consolidate_duplicate_overall_pages(
            results,
            page_images=page_images,
        )

    def _prepare_all_overall_pages(
        self,
        page_images: Mapping[int, bytes],
        *,
        on_progress: Callable[[int, int], None] | None = None,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> list[_PreparedOverallPage]:
        """Locate and crop every page before any vision extraction runs."""

        page_numbers = sorted(page_images)
        total = len(page_numbers) * 2
        prepared: list[_PreparedOverallPage] = []
        for index, page_number in enumerate(page_numbers):
            _raise_if_cancelled(is_cancelled)
            png_bytes = page_images[page_number]
            with Image.open(BytesIO(png_bytes)) as loaded:
                decoded = loaded.convert("RGB")
            table_content, locate_meta = self._locate_table_content(
                page_number,
                png_bytes,
                decoded=decoded,
                is_cancelled=is_cancelled,
            )
            table_image, table_bboxes, table_bbox_labels = _crop_detected_tables(
                decoded,
                table_content,
            )
            prepared.append(
                _PreparedOverallPage(
                    page_number=page_number,
                    png_bytes=png_bytes,
                    decoded=decoded,
                    table_content=table_content,
                    locate_meta=locate_meta,
                    table_image=table_image,
                    table_bboxes=table_bboxes,
                    table_bbox_labels=table_bbox_labels,
                )
            )
            if on_progress is not None:
                on_progress(index + 1, total)
        return prepared

    def _extract_prepared_pages(
        self,
        prepared: Sequence[_PreparedOverallPage],
        *,
        on_progress: Callable[[int, int], None] | None = None,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> list[OverallSiteExtraction]:
        """Extract prepared page crops after all table locations exist."""

        if not prepared:
            return []
        page_numbers = [item.page_number for item in prepared]
        total = len(page_numbers) * 2
        results: list[OverallSiteExtraction] = []
        if not self.config.batch_claude_enabled:
            for index, item in enumerate(prepared):
                results.append(
                    self._finish_prepared_page(
                        item,
                        is_cancelled=is_cancelled,
                    )
                )
                if on_progress is not None:
                    on_progress(len(page_numbers) + index + 1, total)
            return results

        batch_size = max(2, self.config.batch_claude_max_pages)
        for offset in range(0, len(prepared), batch_size):
            batch = list(prepared[offset : offset + batch_size])
            payloads = self._extract_prepared_batch_payloads(
                batch,
                is_cancelled=is_cancelled,
            )
            effective_pages = {item.page_number: item for item in batch}
            recovery_pages: list[_PreparedOverallPage] = []
            for item in batch:
                if _normalize_inv_list(payloads[item.page_number].get("inv_list")):
                    continue
                recovery = self._prepare_batch_inv_recovery(
                    item,
                    is_cancelled=is_cancelled,
                )
                if recovery is not None:
                    recovery_pages.append(recovery)
            if recovery_pages:
                recovered_payloads = self._extract_prepared_batch_payloads(
                    recovery_pages,
                    is_cancelled=is_cancelled,
                )
                for recovery in recovery_pages:
                    page_number = recovery.page_number
                    recovered = recovered_payloads[page_number]
                    recovered_invs = _normalize_inv_list(
                        recovered.get("inv_list")
                    )
                    if recovered_invs:
                        original = payloads[page_number]
                        payloads[page_number] = {
                            **original,
                            "inv_list": list(recovered_invs),
                            "inv_count": len(recovered_invs),
                            "warnings": _merge_warnings(original, recovered),
                        }
                        recovery.locate_meta.update(
                            {
                                "table_bbox_source": "yolo+mineru",
                                "inv_recovery": "batch_recovered",
                            }
                        )
                        effective_pages[page_number] = recovery
                    else:
                        effective_pages[page_number].locate_meta.update(
                            {
                                "mineru_seconds": recovery.locate_meta.get(
                                    "mineru_seconds", 0.0
                                ),
                                "inv_recovery": "batch_empty",
                            }
                        )
            batch_page_numbers = [item.page_number for item in batch]
            for original_item in batch:
                item = effective_pages[original_item.page_number]
                payload = payloads[item.page_number]
                issue = _tracker_column_completeness_issue(payload)
                if issue is not None:
                    payload = _append_extraction_warning(payload, issue)
                extraction = _parse_extraction(item.page_number, "", payload)
                results.append(
                    replace(
                        extraction,
                        raw={
                            **extraction.raw,
                            "table_bboxes": item.table_bboxes,
                            "table_bbox_labels": item.table_bbox_labels,
                            "table_count": len(item.table_bboxes),
                            **item.locate_meta,
                            "vision_provider": self.config.vision_provider,
                            "claude_batch_size": len(batch),
                            "claude_batch_page_numbers": batch_page_numbers,
                        },
                    )
                )
                if on_progress is not None:
                    on_progress(len(page_numbers) + len(results), total)
        return results

    def _extract_prepared_batch_payloads(
        self,
        prepared: Sequence[_PreparedOverallPage],
        *,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> dict[int, dict[str, Any]]:
        try:
            return self._extract_batch(
                prepared,
                is_cancelled=is_cancelled,
            )
        except Agent2Cancelled:
            raise
        except Exception as exc:
            if len(prepared) == 1:
                item = prepared[0]
                payload = self._extract(
                    item.table_image,
                    is_cancelled=is_cancelled,
                )
                payload = self._retry_incomplete_tracker_columns(
                    item.table_image,
                    payload,
                    is_cancelled=is_cancelled,
                )
                return {item.page_number: payload}
            logger.warning(
                "agent2.batch failed for %d prepared pages; "
                "retrying vision extraction per prepared crop without re-locating: %s",
                len(prepared),
                exc,
            )
            payloads: dict[int, dict[str, Any]] = {}
            for item in prepared:
                payload = self._extract(
                    item.table_image,
                    is_cancelled=is_cancelled,
                )
                payloads[item.page_number] = self._retry_incomplete_tracker_columns(
                    item.table_image,
                    payload,
                    is_cancelled=is_cancelled,
                )
            return payloads

    def _finish_prepared_page(
        self,
        item: _PreparedOverallPage,
        *,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> OverallSiteExtraction:
        payload = self._extract(item.table_image, is_cancelled=is_cancelled)
        payload = self._retry_incomplete_tracker_columns(
            item.table_image,
            payload,
            is_cancelled=is_cancelled,
        )
        (
            payload,
            locate_meta,
            table_bboxes,
            table_bbox_labels,
        ) = self._recover_missing_inv_tables(
            item.page_number,
            item.png_bytes,
            item.decoded,
            item.table_content,
            item.locate_meta,
            item.table_bboxes,
            item.table_bbox_labels,
            payload,
            is_cancelled=is_cancelled,
        )
        extraction = _parse_extraction(item.page_number, "", payload)
        return replace(
            extraction,
            raw={
                **extraction.raw,
                "table_bboxes": table_bboxes,
                "table_bbox_labels": table_bbox_labels,
                "table_count": len(table_bboxes),
                **locate_meta,
                "vision_provider": self.config.vision_provider,
            },
        )

    def _prepare_batch_inv_recovery(
        self,
        item: _PreparedOverallPage,
        *,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> _PreparedOverallPage | None:
        """Prepare MinerU recovery crops without issuing a vision request."""

        if item.locate_meta.get("table_bbox_source") != "yolo":
            return None
        mineru_started = perf_counter()
        try:
            layout = self._run_mineru(
                item.page_number,
                item.png_bytes,
                is_cancelled=is_cancelled,
            )
        except Agent2Cancelled:
            raise
        except Exception as exc:
            logger.info(
                "agent2.batch_inv_recovery page=%d mineru_error=%s",
                item.page_number,
                exc,
            )
            item.locate_meta["inv_recovery"] = "mineru_error"
            return None
        candidates = _non_overlapping_table_items(
            item.table_content,
            layout.content_list,
            item.decoded.width,
            item.decoded.height,
        )
        candidates = [
            candidate
            for candidate in candidates
            if _looks_like_identifier_table_item(
                candidate,
                item.decoded.width,
                item.decoded.height,
            )
        ]
        extra = _rank_inv_recovery_items(
            candidates,
            item.table_content,
            item.decoded.width,
            item.decoded.height,
        )[:_MAX_INV_RECOVERY_TABLES]
        mineru_seconds = round(perf_counter() - mineru_started, 3)
        if not extra:
            item.locate_meta.update(
                {
                    "mineru_seconds": mineru_seconds,
                    "inv_recovery": "no_extra_table",
                }
            )
            return None
        reliable_content = [
            content
            for content in item.table_content
            if not content.get("speculative")
        ]
        table_content = [*reliable_content, *extra]
        table_image, table_bboxes, table_bbox_labels = _crop_detected_tables(
            item.decoded,
            table_content,
        )
        return _PreparedOverallPage(
            page_number=item.page_number,
            png_bytes=item.png_bytes,
            decoded=item.decoded,
            table_content=table_content,
            locate_meta={
                **item.locate_meta,
                "mineru_seconds": mineru_seconds,
                "inv_recovery": "batch_candidate",
            },
            table_image=table_image,
            table_bboxes=table_bboxes,
            table_bbox_labels=table_bbox_labels,
        )

    def _extract_batch(
        self,
        prepared: Sequence[_PreparedOverallPage],
        *,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> dict[int, dict[str, Any]]:
        _raise_if_cancelled(is_cancelled)
        if not prepared:
            return {}
        page_mapping = "\n".join(
            f"- Image {index}: page {item.page_number}"
            for index, item in enumerate(prepared, start=1)
        )
        prompt = USER_PROMPT_TEMPLATE + BATCH_USER_PROMPT_SUFFIX.format(
            page_mapping=page_mapping
        )
        per_image_budget = max(
            256 * 1024,
            self.config.bedrock_max_transport_bytes // len(prepared),
        )
        images: list[tuple[bytes, str]] = []
        for item in prepared:
            try:
                images.append(
                    compress_image_for_bedrock(
                        item.table_image,
                        max_transport_bytes=per_image_budget,
                    )
                )
            except Exception as exc:
                raise Agent2OverallSiteError(
                    f"Agent2 batch image preparation failed: {exc}"
                ) from exc
        envelope: dict[str, Any] | None = None
        parse_error: Exception | None = None
        vision = self._vision_llm()
        provider_label = self.config.vision_provider_label
        for attempt in range(1, _JSON_EXTRACTION_ATTEMPTS + 1):
            attempt_prompt = prompt
            if attempt > 1:
                attempt_prompt = f"{prompt}\n\n{_JSON_RETRY_INSTRUCTION}"
            try:
                result = _run_cancellable(
                    lambda: vision.complete_with_images(
                        attempt_prompt,
                        images,
                        system_prompt=SYSTEM_PROMPT,
                        max_completion_tokens=self.config.bedrock_max_tokens,
                    ),
                    is_cancelled,
                )
            except LlmServiceError as exc:
                raise Agent2OverallSiteError(
                    f"{provider_label} batch table extraction failed: {exc}"
                ) from exc
            _raise_if_cancelled(is_cancelled)
            try:
                envelope = _parse_json_object(result.content)
                break
            except (TypeError, json.JSONDecodeError) as exc:
                parse_error = exc
                _log_invalid_json_response("batch", attempt, result, exc)
        if envelope is None:
            raise Agent2OverallSiteError(
                f"{provider_label} batch extraction output was not valid JSON "
                f"after {_JSON_EXTRACTION_ATTEMPTS} attempts."
            ) from parse_error
        pages = envelope.get("pages")
        if not isinstance(pages, list):
            raise Agent2OverallSiteError(
                f"{provider_label} batch extraction must contain a pages array."
            )
        payloads: dict[int, dict[str, Any]] = {}
        for payload in pages:
            if not isinstance(payload, dict):
                continue
            try:
                page_number = int(payload.get("page_number"))
            except (TypeError, ValueError):
                continue
            if page_number in payloads:
                raise Agent2OverallSiteError(
                    f"{provider_label} returned duplicate page {page_number}."
                )
            payloads[page_number] = payload
        expected = {item.page_number for item in prepared}
        if set(payloads) != expected:
            raise Agent2OverallSiteError(
                f"{provider_label} batch extraction returned unexpected pages: "
                f"expected {sorted(expected)}, received {sorted(payloads)}."
            )
        return payloads

    def _run_mineru(
        self,
        page_number: int,
        png_bytes: bytes,
        *,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> MinerUParseResult:
        attempts = max(1, self.config.mineru_max_attempts)
        retry_bytes = png_bytes
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            _raise_if_cancelled(is_cancelled)
            if attempt == 2:
                retry_bytes = _downscale_png(
                    png_bytes,
                    self.config.mineru_retry_max_dimension,
                )
            try:
                parsed = _run_cancellable(
                    lambda: self.mineru.parse_file(
                        retry_bytes,
                        filename=f"overall-page-{page_number}.png",
                        parse_method="ocr",
                        formula_enable=False,
                        table_enable=True,
                        image_analysis=False,
                        return_content_list=True,
                    ),
                    is_cancelled,
                )
                _raise_if_cancelled(is_cancelled)
                if any(
                    isinstance(item, Mapping)
                    and str(item.get("type", "")).lower() == "table"
                    and _parse_table_bbox(item.get("bbox")) is not None
                    for item in parsed.content_list
                ):
                    return parsed
                last_error = Agent2OverallSiteError(
                    "MinerU returned no table coordinates."
                )
            except Agent2Cancelled:
                raise
            except Exception as exc:
                last_error = exc
            if attempt < attempts:
                _sleep_with_cancellation(
                    min(float(attempt), 2.0),
                    is_cancelled,
                )
        raise Agent2OverallSiteError(
            f"MinerU table location failed after {attempts} attempt(s): {last_error}"
        ) from last_error

    def _extract(
        self,
        table_image: bytes,
        *,
        correction: str | None = None,
        is_cancelled: Agent2CancellationCheck | None = None,
    ) -> dict[str, Any]:
        _raise_if_cancelled(is_cancelled)
        prompt = USER_PROMPT_TEMPLATE
        if correction:
            prompt = f"{prompt}\n\n{correction}"
        try:
            image_bytes, mime_type = compress_image_for_bedrock(
                table_image,
                max_transport_bytes=self.config.bedrock_max_transport_bytes,
            )
        except Exception as exc:
            raise Agent2OverallSiteError(
                f"Agent2 table image preparation failed: {exc}"
            ) from exc
        parse_error: Exception | None = None
        vision = self._vision_llm()
        provider_label = self.config.vision_provider_label
        for attempt in range(1, _JSON_EXTRACTION_ATTEMPTS + 1):
            attempt_prompt = prompt
            if attempt > 1:
                attempt_prompt = f"{prompt}\n\n{_JSON_RETRY_INSTRUCTION}"
            try:
                result = _run_cancellable(
                    lambda: vision.complete_with_image(
                        attempt_prompt,
                        image_bytes,
                        mime_type=mime_type,
                        system_prompt=SYSTEM_PROMPT,
                        max_completion_tokens=self.config.bedrock_max_tokens,
                    ),
                    is_cancelled,
                )
            except LlmServiceError as exc:
                raise Agent2OverallSiteError(
                    f"{provider_label} table extraction failed: {exc}"
                ) from exc
            _raise_if_cancelled(is_cancelled)
            try:
                return _parse_json_object(result.content)
            except (TypeError, json.JSONDecodeError) as exc:
                parse_error = exc
                _log_invalid_json_response("single", attempt, result, exc)
        raise Agent2OverallSiteError(
            f"{provider_label} extraction output was not valid JSON after "
            f"{_JSON_EXTRACTION_ATTEMPTS} attempts."
        ) from parse_error


def _sleep_with_cancellation(
    delay_seconds: float,
    is_cancelled: Agent2CancellationCheck | None,
) -> None:
    """Sleep between retries while keeping cancellation latency low."""

    deadline = time.monotonic() + max(0.0, delay_seconds)
    while True:
        _raise_if_cancelled(is_cancelled)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.05))


def _log_invalid_json_response(
    scope: str,
    attempt: int,
    result: Any,
    error: Exception,
) -> None:
    usage = getattr(result, "usage", {})
    safe_usage = {
        key: usage[key]
        for key in ("inputTokens", "outputTokens", "totalTokens")
        if isinstance(usage, Mapping) and key in usage
    }
    logger.warning(
        "agent2.claude_invalid_json scope=%s attempt=%d/%d "
        "stopReason=%r usage=%s response=%s parse_error=%s",
        scope,
        attempt,
        _JSON_EXTRACTION_ATTEMPTS,
        str(getattr(result, "finish_reason", "") or ""),
        safe_usage,
        _redacted_response_summary(getattr(result, "content", "")),
        str(error),
    )


def _redacted_response_summary(content: Any) -> dict[str, Any]:
    """Describe malformed model output without logging extracted project data."""

    text = str(content or "")
    stripped = text.strip()
    keys = sorted(
        {
            match.group(1)
            for match in re.finditer(r'"([^"\\]+)"\s*:', text)
            if match.group(1) in _LOGGABLE_JSON_KEYS
        }
    )
    return {
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "starts_with_object": stripped.startswith("{"),
        "ends_with_object": stripped.endswith("}"),
        "has_code_fence": "```" in text,
        "object_delimiters": [text.count("{"), text.count("}")],
        "array_delimiters": [text.count("["), text.count("]")],
        "schema_keys": keys,
    }


def _parse_json_object(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise TypeError("Claude extraction JSON must be an object.")
    return payload


def _parse_table_bbox(raw: Any) -> tuple[float, float, float, float] | None:
    if (
        isinstance(raw, (str, bytes))
        or not isinstance(raw, Sequence)
        or len(raw) != 4
    ):
        return None
    try:
        x1, y1, x2, y2 = (float(value) for value in raw)
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _select_overall_yolo_tables(
    detections: Sequence[DetectionResult],
    *,
    min_confidence: float,
    weak_inv_confidence: float = 0.15,
    image_width: int = 0,
    image_height: int = 0,
) -> tuple[list[DetectionResult], float | None, list[str]]:
    """Select strong boxes and a plausible weak INV beside a strong tracker.

    The supplied model has separate ``inv_tree`` and ``tracker_table``
    classes. A detection above ``min_confidence`` can stand on its own. A weak
    INV is retained only when a strong tracker supplies supporting context.
    """

    known = [
        item
        for item in detections
        if item.label.strip().casefold() in _OVERALL_YOLO_LABELS
    ]
    if not known:
        return [], None, []
    best_confidence = max(item.confidence for item in known)
    best_by_label: dict[str, DetectionResult] = {}
    for detection in known:
        label = detection.label.strip().casefold()
        if label not in _OVERALL_YOLO_LABELS:
            continue
        current = best_by_label.get(label)
        if current is None or detection.confidence > current.confidence:
            best_by_label[label] = detection
    selected: list[DetectionResult] = [
        item
        for item in best_by_label.values()
        if item.confidence >= min_confidence
    ]
    if not selected:
        return [], best_confidence, []

    weak_labels: list[str] = []
    tracker = best_by_label.get("tracker_table")
    inv = best_by_label.get("inv_tree")
    tracker_is_strong = (
        tracker is not None and tracker.confidence >= min_confidence
    )
    inv_is_selected = any(
        item.label.strip().casefold() == "inv_tree" for item in selected
    )
    if (
        tracker_is_strong
        and not inv_is_selected
        and inv is not None
        and inv.confidence >= weak_inv_confidence
        and _is_plausible_weak_inv(
            inv,
            tracker,
            image_width=image_width,
            image_height=image_height,
        )
    ):
        selected.append(inv)
        weak_labels.append("inv_tree")
    selected.sort(key=lambda item: (item.box.y1, item.box.x1))
    return selected, best_confidence, weak_labels


def _is_plausible_weak_inv(
    inv: DetectionResult,
    tracker: DetectionResult,
    *,
    image_width: int,
    image_height: int,
) -> bool:
    """Reject tiny, duplicate, or spatially unrelated weak INV detections."""

    if image_width <= 0 or image_height <= 0:
        return True
    inv_width = max(0.0, inv.box.x2 - inv.box.x1)
    inv_height = max(0.0, inv.box.y2 - inv.box.y1)
    tracker_width = max(0.0, tracker.box.x2 - tracker.box.x1)
    tracker_height = max(0.0, tracker.box.y2 - tracker.box.y1)
    if min(inv_width, inv_height, tracker_width, tracker_height) <= 0:
        return False
    page_area = float(image_width * image_height)
    if inv_width * inv_height < page_area * 0.002:
        return False

    inv_box = (inv.box.x1, inv.box.y1, inv.box.x2, inv.box.y2)
    tracker_box = (
        tracker.box.x1,
        tracker.box.y1,
        tracker.box.x2,
        tracker.box.y2,
    )
    if _box_iou(inv_box, tracker_box) > 0.75:
        return False
    horizontal_overlap = max(
        0.0,
        min(inv.box.x2, tracker.box.x2) - max(inv.box.x1, tracker.box.x1),
    ) / min(inv_width, tracker_width)
    vertical_overlap = max(
        0.0,
        min(inv.box.y2, tracker.box.y2) - max(inv.box.y1, tracker.box.y1),
    ) / min(inv_height, tracker_height)
    horizontal_gap = max(
        0.0,
        max(inv.box.x1, tracker.box.x1) - min(inv.box.x2, tracker.box.x2),
    )
    vertical_gap = max(
        0.0,
        max(inv.box.y1, tracker.box.y1) - min(inv.box.y2, tracker.box.y2),
    )
    return (
        horizontal_overlap >= 0.15 and vertical_gap <= image_height * 0.35
    ) or (
        vertical_overlap >= 0.15 and horizontal_gap <= image_width * 0.35
    )


def _expand_yolo_detections(
    detections: Sequence[DetectionResult],
    *,
    image_width: int,
    image_height: int,
    expansion_ratio: float,
) -> list[DetectionResult]:
    """Expand every YOLO crop uniformly on all four sides.

    The margin is based on each detector-produced box, so every crop gains
    the configured fraction of its original width on the left and right and
    the same fraction of its original height on the top and bottom. The default
    is one percent. The expanded coordinates
    are also persisted as ``table_bboxes`` and therefore drive the Agent2 UI
    overlay, keeping the visualization identical to the actual vision crop.
    """

    expanded: list[DetectionResult] = []
    for item in detections:
        width = max(0.0, item.box.x2 - item.box.x1)
        height = max(0.0, item.box.y2 - item.box.y1)
        margin_x = width * expansion_ratio
        margin_y = height * expansion_ratio
        box = BoundingBox(
            max(0.0, item.box.x1 - margin_x),
            max(0.0, item.box.y1 - margin_y),
            min(float(image_width), item.box.x2 + margin_x),
            min(float(image_height), item.box.y2 + margin_y),
        )
        expanded.append(item if box == item.box else replace(item, box=box))
    return expanded


def _detections_to_table_content(
    detections: Sequence[DetectionResult],
    *,
    image_width: int,
    image_height: int,
) -> list[dict[str, Any]]:
    if image_width <= 0 or image_height <= 0:
        raise Agent2OverallSiteError("Overall-site image has invalid dimensions.")
    return [
        {
            "type": "table",
            "bbox": [
                detection.box.x1 / image_width,
                detection.box.y1 / image_height,
                detection.box.x2 / image_width,
                detection.box.y2 / image_height,
            ],
            "label": detection.label,
            "confidence": detection.confidence,
        }
        for detection in detections
    ]


def _bbox_to_pixel_box(
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    maximum = max(bbox)
    if maximum <= 1.0:
        scale_x, scale_y = float(width), float(height)
    elif maximum <= 1000.0:
        scale_x, scale_y = width / 1000.0, height / 1000.0
    else:
        scale_x = scale_y = 1.0
    padding_x = max(2, round(width * 0.003))
    padding_y = max(2, round(height * 0.003))
    return (
        max(0, floor(x1 * scale_x + 1e-6) - padding_x),
        max(0, floor(y1 * scale_y + 1e-6) - padding_y),
        min(width, ceil(x2 * scale_x - 1e-6) + padding_x),
        min(height, ceil(y2 * scale_y - 1e-6) + padding_y),
    )


def _box_iou(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    intersection_width = max(
        0.0,
        min(left[2], right[2]) - max(left[0], right[0]),
    )
    intersection_height = max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )
    intersection = intersection_width * intersection_height
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _non_overlapping_table_items(
    existing: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    width: int,
    height: int,
    *,
    iou_threshold: float = _INV_RECOVERY_IOU,
) -> list[dict[str, Any]]:
    """Return MinerU tables not already covered by reliable YOLO boxes."""

    covered: list[tuple[int, int, int, int]] = []
    for item in existing:
        if not isinstance(item, Mapping) or item.get("speculative"):
            continue
        parsed = _parse_table_bbox(item.get("bbox"))
        if parsed is not None:
            covered.append(_bbox_to_pixel_box(parsed, width, height))

    extras: list[dict[str, Any]] = []
    for item in candidates:
        if (
            not isinstance(item, Mapping)
            or str(item.get("type", "")).casefold() != "table"
        ):
            continue
        parsed = _parse_table_bbox(item.get("bbox"))
        if parsed is None:
            continue
        pixels = _bbox_to_pixel_box(parsed, width, height)
        if any(
            _box_iou(pixels, other) >= iou_threshold for other in covered
        ):
            continue
        extras.append(dict(item))
        covered.append(pixels)
    return extras


def _looks_like_identifier_table_item(
    item: Mapping[str, Any],
    width: int,
    height: int,
) -> bool:
    """Apply only broad sanity checks; INV tables may be wide or right-side."""

    parsed = _parse_table_bbox(item.get("bbox"))
    if parsed is None or width <= 0 or height <= 0:
        return False
    x1, y1, x2, y2 = _bbox_to_pixel_box(parsed, width, height)
    box_width = x2 - x1
    box_height = y2 - y1
    if box_width < width * 0.04 or box_height < height * 0.04:
        return False
    area_ratio = box_width * box_height / float(width * height)
    if area_ratio < 0.002 or area_ratio > 0.85:
        return False
    aspect_ratio = box_width / box_height
    return 0.12 <= aspect_ratio <= 15.0


def _rank_inv_recovery_items(
    candidates: Sequence[dict[str, Any]],
    existing: Sequence[Mapping[str, Any]],
    width: int,
    height: int,
) -> list[dict[str, Any]]:
    """Rank flexible MinerU candidates using tracker-relative soft signals."""

    tracker_boxes: list[tuple[int, int, int, int]] = []
    for item in existing:
        if (
            not isinstance(item, Mapping)
            or str(item.get("label", "")).casefold() != "tracker_table"
        ):
            continue
        parsed = _parse_table_bbox(item.get("bbox"))
        if parsed is not None:
            tracker_boxes.append(_bbox_to_pixel_box(parsed, width, height))

    def score(item: Mapping[str, Any]) -> float:
        parsed = _parse_table_bbox(item.get("bbox"))
        if parsed is None:
            return float("-inf")
        box = _bbox_to_pixel_box(parsed, width, height)
        box_width = box[2] - box[0]
        box_height = box[3] - box[1]
        value = min(1.0, box_width * box_height / (width * height * 0.1))
        if box_width >= box_height:
            value += 0.4
        if box[0] < width * 0.45 and box_height > box_width:
            value += 0.4
        for tracker in tracker_boxes:
            tracker_width = tracker[2] - tracker[0]
            overlap_x = max(
                0,
                min(box[2], tracker[2]) - max(box[0], tracker[0]),
            ) / max(1, min(box_width, tracker_width))
            vertical_gap = max(0, box[1] - tracker[3])
            if overlap_x >= 0.25:
                value += 1.5
            if box[1] >= tracker[1] and vertical_gap <= height * 0.35:
                value += 1.0
        return value

    return sorted(candidates, key=score, reverse=True)


def _crop_detected_tables(
    source: bytes | Image.Image,
    content_list: Sequence[dict[str, Any]],
) -> tuple[bytes, list[list[int]], list[str]]:
    """Crop every selected table bbox and stack crops without reading text."""

    labeled_boxes: list[
        tuple[tuple[float, float, float, float], str, bool]
    ] = []
    seen_boxes: set[tuple[float, float, float, float]] = set()
    for item in content_list:
        if (
            not isinstance(item, Mapping)
            or str(item.get("type", "")).lower() != "table"
        ):
            continue
        parsed = _parse_table_bbox(item.get("bbox"))
        if parsed is None or parsed in seen_boxes:
            continue
        seen_boxes.add(parsed)
        labeled_boxes.append(
            (
                parsed,
                str(item.get("label") or "table"),
                bool(item.get("speculative")),
            )
        )
    if not labeled_boxes:
        raise Agent2OverallSiteError("MinerU returned no usable table coordinates.")
    try:
        if isinstance(source, bytes):
            with Image.open(BytesIO(source)) as image:
                return _montage_table_crops(image.convert("RGB"), labeled_boxes)
        return _montage_table_crops(source, labeled_boxes)
    except Agent2OverallSiteError:
        raise
    except Exception as exc:
        raise Agent2OverallSiteError(
            f"Failed to crop MinerU table coordinates: {exc}"
        ) from exc


def _montage_table_crops(
    rgb: Image.Image,
    labeled_boxes: Sequence[
        tuple[tuple[float, float, float, float], str, bool]
    ],
) -> tuple[bytes, list[list[int]], list[str]]:
    ordered = sorted(
        (
            (
                _bbox_to_pixel_box(box, rgb.width, rgb.height),
                label,
                speculative,
            )
            for box, label, speculative in labeled_boxes
        ),
        key=lambda item: (item[0][1], item[0][0]),
    )
    pixel_boxes = [item[0] for item in ordered]
    labels = [item[1] for item in ordered]
    crops = [rgb.crop(box) for box in pixel_boxes]
    if len(crops) == 1:
        montage = crops[0]
    else:
        gap = max(12, round(max(rgb.size) * 0.002))
        montage_width = max(crop.width for crop in crops)
        montage_height = sum(crop.height for crop in crops) + gap * (
            len(crops) - 1
        )
        montage = Image.new("RGB", (montage_width, montage_height), "white")
        y = 0
        for crop in crops:
            montage.paste(crop, (0, y))
            y += crop.height + gap
    output = BytesIO()
    montage.save(output, format="PNG", compress_level=1)
    return output.getvalue(), [list(box) for box in pixel_boxes], labels


def _downscale_decoded(
    decoded: Image.Image,
    max_side: int,
) -> tuple[Image.Image, float, float]:
    longest = max(decoded.size)
    if max_side <= 0 or longest <= max_side:
        return decoded, 1.0, 1.0
    ratio = max_side / longest
    target = (
        max(1, round(decoded.width * ratio)),
        max(1, round(decoded.height * ratio)),
    )
    resized = decoded.resize(target, Image.Resampling.BOX)
    return resized, decoded.width / target[0], decoded.height / target[1]


def _yolo_detect_max_side(detector: object) -> int:
    input_size = getattr(detector, "_input_size", None)
    if (
        isinstance(input_size, tuple)
        and len(input_size) == 2
        and all(isinstance(value, int) and value > 0 for value in input_size)
    ):
        return max(_YOLO_DETECT_MAX_SIDE, max(input_size) * 2)
    return _YOLO_DETECT_MAX_SIDE


def _scale_detections(
    detections: Sequence[DetectionResult],
    scale_x: float,
    scale_y: float,
) -> tuple[DetectionResult, ...]:
    return tuple(
        replace(
            item,
            box=BoundingBox(
                item.box.x1 * scale_x,
                item.box.y1 * scale_y,
                item.box.x2 * scale_x,
                item.box.y2 * scale_y,
            ),
        )
        for item in detections
    )


def _downscale_png(png_bytes: bytes, max_dimension: int) -> bytes:
    if max_dimension <= 0:
        return png_bytes
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            if max(image.size) <= max_dimension:
                return png_bytes
            converted = image.convert("RGB")
            converted.thumbnail(
                (max_dimension, max_dimension),
                Image.Resampling.LANCZOS,
            )
            output = BytesIO()
            converted.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except Exception:
        return png_bytes


def _tracker_column_completeness_issue(
    payload: dict[str, Any],
) -> str | None:
    raw_trackers = payload.get("tracker_ocr_fields")
    if not isinstance(raw_trackers, list):
        return "tracker_ocr_fields is not a JSON array."
    actual = len(raw_trackers)
    declared = payload.get("detected_module_column_count")
    declared_count = (
        declared
        if isinstance(declared, int) and not isinstance(declared, bool)
        and declared >= 0
        else None
    )
    if declared_count is None:
        return "detected_module_column_count is missing or invalid."
    expected = declared_count
    if actual != expected:
        return f"Detected {expected} physical columns but returned {actual}."
    indices = [
        item.get("column_index") if isinstance(item, dict) else None
        for item in raw_trackers
    ]
    if indices != list(range(1, expected + 1)):
        return (
            "column_index values must be consecutive and ordered from 1 to "
            f"{expected}; received {indices}."
        )
    return None


def _append_extraction_warning(
    payload: dict[str, Any],
    issue: str,
) -> dict[str, Any]:
    warnings = payload.get("warnings")
    warning_list = list(warnings) if isinstance(warnings, list) else []
    message = f"Tracker column completeness check: {issue}"
    if message not in warning_list:
        warning_list.append(message)
    return {**payload, "warnings": warning_list}


def _inv_selection_issue(payload: dict[str, Any]) -> str | None:
    raw_invs = payload.get("inv_list")
    if not isinstance(raw_invs, list):
        return None
    invalid_values = [
        str(value).strip()
        for value in raw_invs
        if _inv_identity(value) is None
    ]
    if not invalid_values:
        return None
    preview = ", ".join(invalid_values[:3])
    return f"inv_list contains non-identifier values: {preview}."


def _append_inv_warning(
    payload: dict[str, Any],
    issue: str,
) -> dict[str, Any]:
    warnings = payload.get("warnings")
    warning_list = list(warnings) if isinstance(warnings, list) else []
    message = f"INV selection check: {issue} Invalid values were removed."
    if message not in warning_list:
        warning_list.append(message)
    return {**payload, "warnings": warning_list}


def _merge_warnings(*payloads: dict[str, Any]) -> list[str]:
    merged: list[str] = []
    for payload in payloads:
        warnings = payload.get("warnings")
        if not isinstance(warnings, list):
            continue
        for warning in warnings:
            if isinstance(warning, str) and warning not in merged:
                merged.append(warning)
    return merged


def _parse_extraction(
    page_number: int,
    markdown: str,
    payload: dict[str, Any],
) -> OverallSiteExtraction:
    if payload.get("extraction_scope") != EXTRACTION_SCOPE:
        raise Agent2OverallSiteError("Claude returned an invalid extraction_scope.")
    raw_trackers = payload.get("tracker_ocr_fields")
    if not isinstance(raw_trackers, list):
        raise Agent2OverallSiteError("tracker_ocr_fields must be a JSON array.")
    trackers = tuple(_parse_tracker(item) for item in raw_trackers)
    raw_invs = payload.get("inv_list")
    inv_list = _normalize_inv_list(raw_invs)
    warnings_raw = payload.get("warnings", [])
    warnings = (
        tuple(str(item) for item in warnings_raw if isinstance(item, str))
        if isinstance(warnings_raw, list)
        else ()
    )
    if isinstance(raw_invs, list) and raw_invs and not inv_list:
        filtered_warning = (
            "INV selection: candidate identifiers were rejected because "
            "they look like AREA/BLOCK/ZONE map labels or other "
            "non-equipment values."
        )
        if filtered_warning not in warnings:
            warnings = (*warnings, filtered_warning)
    return OverallSiteExtraction(
        page_number=page_number,
        tracker_ocr_fields=trackers,
        inv_list=inv_list,
        inv_rows=_inv_rows_from_list(inv_list),
        inv_count=len(inv_list),
        warnings=warnings,
        ocr_markdown=markdown,
        raw=payload,
    )


def _agent4_inv_string_counts(
    extractions: Sequence[OverallSiteExtraction],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for extraction in extractions:
        for row in extraction.inv_rows:
            columns: list[dict[str, Any]] = []
            total = 0
            has_total = False
            for item in row.string_counts:
                count = _parse_count(item.count)
                columns.append(
                    {
                        "column_index": item.column_index,
                        "label": item.label,
                        "count": count if count is not None else {},
                    }
                )
                if count is not None:
                    total += count
                    has_total = True
            items.append(
                {
                    "identifier": row.identifier,
                    "string_counts": columns,
                    "total": total if has_total else {},
                }
            )
    return items


def _inv_rows_from_list(inv_list: Sequence[str]) -> tuple[InvOcrRow, ...]:
    return tuple(InvOcrRow(identifier=inv) for inv in inv_list)


def _parse_inv_rows(value: Any, inv_list: Sequence[str]) -> tuple[InvOcrRow, ...]:
    raw_rows = value if isinstance(value, list) else []
    parsed: list[InvOcrRow] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("identifier") or "").strip()
        if _inv_identity(identifier) is None:
            continue
        counts_raw = item.get("string_counts", [])
        counts: list[InvStringCount] = []
        if isinstance(counts_raw, list):
            for index, count_item in enumerate(counts_raw, start=1):
                if not isinstance(count_item, dict):
                    continue
                label = str(count_item.get("label") or "").strip()
                if not label:
                    continue
                column_index = count_item.get("column_index", index)
                try:
                    column_index = int(column_index)
                except (TypeError, ValueError):
                    column_index = index
                counts.append(
                    InvStringCount(
                        column_index=column_index,
                        label=label,
                        count=_optional_string(count_item.get("count")),
                        source=_optional_string(count_item.get("source")),
                    )
                )
        parsed.append(
            InvOcrRow(identifier=identifier, string_counts=tuple(counts))
        )

    by_identity: dict[str, InvOcrRow] = {}
    for row in parsed:
        identity = _inv_identity(row.identifier)
        if identity is not None and identity not in by_identity:
            by_identity[identity] = row

    aligned: list[InvOcrRow] = []
    for identifier in inv_list:
        identity = _inv_identity(identifier)
        row = by_identity.get(identity) if identity else None
        if row is None:
            aligned.append(InvOcrRow(identifier=identifier))
        elif row.identifier != identifier:
            aligned.append(replace(row, identifier=identifier))
        else:
            aligned.append(row)
    return tuple(aligned)


def _merge_inv_rows(
    inv_list: Sequence[str],
    extractions: Sequence[OverallSiteExtraction],
) -> tuple[InvOcrRow, ...]:
    del extractions
    return _inv_rows_from_list(inv_list)


def _parse_tracker(payload: Any) -> TrackerOcrFields:
    if not isinstance(payload, dict):
        raise Agent2OverallSiteError(
            "Each tracker_ocr_fields item must be an object."
        )
    details_raw = payload.get("num_str_details", [])
    details: list[NumStrDetail] = []
    if isinstance(details_raw, list):
        for item in details_raw:
            if not isinstance(item, dict) or not item.get("num_str"):
                continue
            details.append(
                NumStrDetail(
                    num_str=str(item["num_str"]),
                    count=_optional_string(item.get("count")),
                    source=_optional_string(item.get("source")),
                )
            )
    if not details and isinstance(payload.get("num_str"), list):
        for item in payload["num_str"]:
            if not isinstance(item, dict) or not item.get("num_str"):
                continue
            details.append(
                NumStrDetail(
                    num_str=str(item["num_str"]),
                    count=_optional_string(item.get("count")),
                    source=_optional_string(item.get("source")),
                )
            )
    return TrackerOcrFields(
        module_model=_parse_field(payload, "module_model"),
        module_stc_dc_rating_w=_parse_field(
            payload,
            "module_stc_dc_rating_w",
        ),
        string_count=_parse_field(payload, "string_count"),
        num_str=_parse_field(payload, "num_str"),
        num_str_details=tuple(details),
    )


def _parse_field(payload: dict[str, Any], key: str) -> TrackerFieldValue:
    item = payload.get(key)
    if not isinstance(item, dict):
        return TrackerFieldValue(
            label={
                "module_model": "MODULE MODEL",
                "module_stc_dc_rating_w": "MODULE STC DC RATING (W)",
                "string_count": "STRING COUNT",
                "num_str": "Num-STR",
            }.get(key, key),
            meaning={
                "module_model": "board brand/model",
                "module_stc_dc_rating_w": "module wattage",
                "string_count": "total string count",
                "num_str": "specific string types",
            }.get(key, ""),
            value=_coerce_field_value(item, key=key),
        )
    return TrackerFieldValue(
        label=str(item.get("label") or ""),
        meaning=str(item.get("meaning") or ""),
        value=_optional_string(item.get("value")),
        source=_optional_string(item.get("source")),
    )


def _coerce_field_value(value: Any, *, key: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    if key != "num_str" or not isinstance(value, list):
        return None
    values: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("num_str") or item.get("value") or "").strip()
        else:
            text = str(item).strip()
        if text and text not in values:
            values.append(text)
    return ", ".join(values) or None


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_nonempty_string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _normalize_power(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", "", str(value)).upper()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)W?", text)
    return f"{match.group(1)}W" if match else ""


def _parse_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"\d+", text):
        return None
    return int(text)


def _split_tracker_types(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(
        item.strip()
        for item in re.split(r"[,;/\n]+", str(value))
        if item.strip()
    )


def _normalize_tracker_type(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return ""
    match = re.fullmatch(
        r"(\d+)\s*(?:-|\s)*(?:STR|STRING)(?:\s+.*)?",
        text,
        re.I,
    )
    return f"{match.group(1)}-string" if match else text


def _normalize_inv_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise Agent2OverallSiteError("inv_list must be a JSON array.")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        identity = _inv_identity(text)
        if identity is None:
            continue
        if identity not in seen:
            seen.add(identity)
            normalized.append(text)
    return tuple(normalized)


def _consolidate_duplicate_overall_pages(
    results: Sequence[OverallSiteExtraction],
    *,
    page_images: Mapping[int, bytes] | None = None,
) -> tuple[OverallSiteExtraction, ...]:
    """Collapse repeated visual versions of the same overall-site drawing.

    Duplicate detection is based on substantial INV-set overlap rather than
    shared tracker specifications, because separate site sheets commonly use
    the same module and tracker types. The best tracker extraction is retained
    and INV identifiers are merged without changing their recognized format.
    """

    fingerprints = {
        result.page_number: _layout_fingerprint(
            page_images.get(result.page_number, b"") if page_images else b""
        )
        for result in results
    }
    groups: list[list[OverallSiteExtraction]] = []
    for result in results:
        for group in groups:
            if any(
                _are_duplicate_overall_pages(
                    result,
                    item,
                    left_fingerprint=fingerprints[result.page_number],
                    right_fingerprint=fingerprints[item.page_number],
                )
                for item in group
            ):
                group.append(result)
                break
        else:
            groups.append([result])

    consolidated: list[OverallSiteExtraction] = []
    for group in groups:
        if len(group) == 1:
            consolidated.append(group[0])
            continue
        representative = max(
            group,
            key=lambda item: (
                _tracker_extraction_quality(item),
                len(item.inv_list),
                -item.page_number,
            ),
        )
        merged_invs: list[str] = []
        seen_inv_ids: set[str] = set()
        for item in (representative, *group):
            for inv in item.inv_list:
                identity = _inv_identity(inv)
                if identity is None or identity in seen_inv_ids:
                    continue
                seen_inv_ids.add(identity)
                merged_invs.append(inv)
        page_numbers = tuple(sorted(item.page_number for item in group))
        consolidated.append(
            replace(
                representative,
                inv_list=tuple(merged_invs),
                inv_rows=_merge_inv_rows(merged_invs, (representative, *group)),
                inv_count=len(merged_invs),
                raw={
                    **representative.raw,
                    "consolidated_page_numbers": page_numbers,
                },
            )
        )
    return tuple(consolidated)


def _are_duplicate_overall_pages(
    left: OverallSiteExtraction,
    right: OverallSiteExtraction,
    *,
    left_fingerprint: tuple[bool, ...] | None = None,
    right_fingerprint: tuple[bool, ...] | None = None,
) -> bool:
    left_ids = {item for inv in left.inv_list if (item := _inv_identity(inv))}
    right_ids = {item for inv in right.inv_list if (item := _inv_identity(inv))}
    if left_ids and right_ids:
        overlap = len(left_ids & right_ids)
        return (
            overlap / min(len(left_ids), len(right_ids)) >= 0.8
            and overlap / max(len(left_ids), len(right_ids)) >= 0.6
        )
    if _tracker_signature(left) != _tracker_signature(right):
        return False
    if not _tracker_signature(left):
        return False
    return _fingerprint_similarity(
        left_fingerprint,
        right_fingerprint,
    ) >= 0.82


def _tracker_signature(result: OverallSiteExtraction) -> tuple[object, ...]:
    return tuple(
        (
            _optional_nonempty_string(tracker.module_model.value).upper(),
            _normalize_power(tracker.module_stc_dc_rating_w.value),
            _parse_count(tracker.string_count.value),
            tuple(
                (
                    _normalize_tracker_type(detail.num_str),
                    _parse_count(detail.count),
                )
                for detail in tracker.num_str_details
            ),
        )
        for tracker in result.tracker_ocr_fields
    )


def _layout_fingerprint(png_bytes: bytes) -> tuple[bool, ...] | None:
    if not png_bytes:
        return None
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            gray = ImageOps.autocontrast(image.convert("L"))
            reduced = gray.resize((64, 64), Image.Resampling.LANCZOS)
            pixels = list(reduced.get_flattened_data())
            if max(pixels) - min(pixels) < 12:
                return None
            horizontal = [
                pixels[y * 64 + x] > pixels[y * 64 + x + 1]
                for y in range(64)
                for x in range(63)
            ]
            vertical = [
                pixels[y * 64 + x] > pixels[(y + 1) * 64 + x]
                for y in range(63)
                for x in range(64)
            ]
            return tuple(horizontal + vertical)
    except Exception:
        return None


def _fingerprint_similarity(
    left: tuple[bool, ...] | None,
    right: tuple[bool, ...] | None,
) -> float:
    if left is None or right is None or len(left) != len(right):
        return 0.0
    equal = sum(left_bit == right_bit for left_bit, right_bit in zip(left, right))
    return equal / len(left) if left else 0.0


def _tracker_extraction_quality(result: OverallSiteExtraction) -> int:
    score = 0
    for tracker in result.tracker_ocr_fields:
        score += sum(
            bool(field.value and field.value.strip())
            for field in (
                tracker.module_model,
                tracker.module_stc_dc_rating_w,
                tracker.string_count,
                tracker.num_str,
            )
        )
        score += sum(
            bool(detail.count and detail.count.strip())
            for detail in tracker.num_str_details
        )
    return score


def _inv_identity(value: Any) -> str | None:
    text = str(value).strip()
    if not text or not re.search(r"\d", text):
        return None
    if re.match(r"^(?:AREA|BLOCK|ZONE)\b", text, re.I):
        return None
    if re.match(r"^(?:GRAND\s+)?(?:SUB)?TOTAL\b", text, re.I):
        return None
    if re.fullmatch(r"[0-9A-Z][0-9A-Z\s_/#:.-]*", text, re.I) is None:
        return None
    without_inv_prefix = re.sub(
        r"^INV\s*[-:#]?\s*",
        "",
        text,
        flags=re.I,
    )
    return re.sub(r"\s+", "", without_inv_prefix).upper()

"""Agent3 orchestration for Array Layout element recognition."""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from collections.abc import Generator, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait
from dataclasses import replace
from io import BytesIO
from statistics import mean, median
from time import perf_counter
from threading import Lock, current_thread
from typing import Callable

from PIL import Image

from ...ai import (
    BoundingBox,
    DetectionResult,
    YoloClient,
)
from ...ai.llm.providers import QwenLlmProvider
from ...ai.yolo import BOX_MODEL_TYPE, TRACKER_MODEL_TYPE
from ...ordering import natural_text_key
from ..models import (
    DetectionAnnotation,
    InvDraft,
    NodeDraft,
    OcrField,
    RackingEntryDraft,
    RecognitionState,
    ReviewedTree,
    RowDraft,
    SourceReference,
    TrackerQuantity,
)
from .block_diagram_table import (
    BlockDiagramTableRecognizer,
    BlockDiagramTableResult,
    TableLocation,
)
from .block_diagram_table.common import bbox_to_pixels
from .contracts import (
    Agent3Cancelled,
    Agent3Error,
    Agent3ProgressUpdate,
    Agent3StepCallback,
    ArrayLayoutPageInput,
    ElementDetection,
    ElementPageResult,
    ElementRecognitionRequest,
    ElementRecognitionResult,
    InvRecognitionSettings,
    NodeStringTableRow,
    PanelOrientation,
    RecognitionIssue,
    RecognitionIssueSeverity,
    TrackerDefinition,
)
from .issues import IssueCode, create_issue
from .issue_review import node_name_issues, refresh_review_issues, with_issues
from .geometry import (
    TRACKER_LENGTH_TOLERANCE,
    TrackerLengthSample,
    axis_center,
    assign_boxes_to_nodes,
    assign_parent,
    center,
    check_tracker_lengths,
    classify_trackers,
    format_length,
    group_rows,
    interval_gap,
    long_side,
    polarity_for_side,
    short_side,
    snap_positive,
)
from .debug_export import (
    bbox_debug_dict,
    detection_result_debug_dict,
    input_image_debug_dict,
    table_result_debug_dict,
)
from .node_label import (
    NodeNameReviewCandidate,
    NodeNameReviewError,
    NodeNameReviewer,
    NodeNameRecognizer,
    _collect_node_crop_text,
    _equipment_signature,
    _extract_equipment_name,
    _extract_inv_prefix,
    _match_table_name,
    _node_ocr_raw,
    _normalize_name,
)
from .pipeline import submit_page
from .review import summarize_node_rows
from .inv_name_matching import best_agent2_inv_match, match_inv_names


ProgressCallback = Callable[[int, int], None]
CancellationCheck = Callable[[], bool]

logger = logging.getLogger(__name__)

# Network-bound Node-name/table requests run concurrently. The local Qwen
# deployment is sized for 32 parallel Node crops; AGENT3_IO_WORKERS remains an
# escape hatch for smaller deployments. Full-page table parses have a separate
# lane so they never head-of-line-block the small Qwen vision requests.
DEFAULT_IO_WORKERS = 32

# Page workers bound decoded RGB drawings and local preparation. Network
# waits suspend the page after its crops have been compressed and the full
# bitmap released, allowing later pages to use the same workers.
DEFAULT_PAGE_WORKERS = 2

# Detection runs on one shared downscaled copy: the bundled models export
# fixed square inputs (tracker 2560px, box 3072px), so a full 6000x9000
# bitmap only buys two expensive full-resolution numpy conversions per page
# before each model letterboxes it down again. Matching the largest exported
# input keeps detection information identical while removing those copies;
# detections are mapped back to page coordinates. Overrideable through
# AGENT3_YOLO_MAX_SIDE.
DEFAULT_DETECT_MAX_SIDE = 3072

# Claude receives the crop in its original drawing orientation only. Rotating
# a crop can change the visual reading order and was explicitly removed from
# the Agent3 contract.
DEFAULT_NODE_OCR_ORIENTATIONS = 1

# Agent3 should surface duplicate/ambiguous YOLO boxes well before the draft
# reaches validation. The comparison is deliberately stricter than the
# detector's own NMS setting: any surviving same-kind pair above 30% IoU is
# review-worthy, while a pair exactly at the boundary is accepted.
OVERLAP_ISSUE_IOU_THRESHOLD = 0.30


def _resolve_io_workers(io_workers: int | None) -> int:
    if io_workers is not None and io_workers > 0:
        return io_workers
    from count_and_design.application.env import env_int_or_none

    configured = env_int_or_none("AGENT3_IO_WORKERS", None)
    if configured is not None and configured > 0:
        return configured
    return DEFAULT_IO_WORKERS


def _resolve_detect_max_side(detect_max_side: int | None) -> int:
    if detect_max_side is not None and detect_max_side > 0:
        return detect_max_side
    from count_and_design.application.env import env_int_or_none

    configured = env_int_or_none("AGENT3_YOLO_MAX_SIDE", None)
    if configured is not None and configured > 0:
        return configured
    return DEFAULT_DETECT_MAX_SIDE


def _resolve_page_workers(page_workers: int | None) -> int:
    if page_workers is not None:
        return max(1, int(page_workers))
    from count_and_design.application.env import env_int_or_none

    configured = env_int_or_none("AGENT3_PAGE_WORKERS", None)
    if configured is not None and configured > 0:
        return configured
    return DEFAULT_PAGE_WORKERS


def _resolve_grayscale(grayscale: bool | None) -> bool:
    if grayscale is not None:
        return grayscale
    from count_and_design.application.config import Agent3ImageConfig

    return Agent3ImageConfig.from_env().grayscale_enabled


def _resolve_line_enhance(line_enhance: bool | None) -> bool:
    if line_enhance is not None:
        return line_enhance
    from count_and_design.application.config import Agent3ImageConfig

    return Agent3ImageConfig.from_env().line_enhance_enabled


def _resolve_node_review_enabled(node_name_review_enabled: bool | None) -> bool:
    """Resolve the page-level Node-name review switch (enabled by default)."""

    if node_name_review_enabled is not None:
        return bool(node_name_review_enabled)
    # Load the env file like every other config resolver does.  Reading
    # os.environ directly made the value depend on whether some earlier
    # config object happened to load .env first in the process.
    from count_and_design.application.env import env_bool, load_env_file

    load_env_file()
    return env_bool("AGENT3_NODE_REVIEW_ENABLED", True)


def prepare_agent3_page_image(
    loaded: Image.Image,
    *,
    grayscale: bool = False,
    line_enhance: bool = False,
) -> Image.Image:
    image = loaded.convert("RGB")
    if line_enhance:
        from .line_enhance import enhance_line_image

        return enhance_line_image(image)
    if grayscale:
        image = image.convert("L").convert("RGB")
    return image


def encode_agent3_page_png(
    image_bytes: bytes,
    *,
    max_side: int | None = None,
    grayscale: bool = False,
    line_enhance: bool = False,
) -> bytes:
    with Image.open(BytesIO(image_bytes)) as loaded:
        needs_resize = max_side is not None and max(loaded.size) > max_side
        if not grayscale and not line_enhance and not needs_resize:
            return image_bytes
        image = prepare_agent3_page_image(
            loaded,
            grayscale=grayscale,
            line_enhance=line_enhance,
        )
    if max_side is not None and max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, format="PNG")
    image.close()
    return output.getvalue()


def _report_step(
    callback: Agent3StepCallback | None,
    step: str,
    status: str,
    page_number: int | None = None,
    detail: str = "",
) -> None:
    if callback is not None:
        callback(
            Agent3ProgressUpdate(
                step=step,
                status=status,
                page_number=page_number,
                detail=detail,
                worker_name=current_thread().name,
            )
        )


def annotate_tracker_length_outliers(
    result: ElementRecognitionResult,
    definitions: tuple[TrackerDefinition, ...],
    tolerance: float = TRACKER_LENGTH_TOLERANCE,
) -> ElementRecognitionResult:
    """Warn on Trackers whose length contradicts their classification.

    Tracker classification runs per Node, but one string's length is an INV
    property: the same physical string length has to explain every Node's
    Trackers, so the check aggregates the whole merged INV. Each outlying
    Tracker detection carries the warning code, which lets the preview
    canvas draw that box in the issue color and the issue list navigate to
    it.
    """

    trackers_by_parent: dict[str, list[ElementDetection]] = {}
    page_by_entity: dict[str, int] = {}
    for page in result.pages:
        for detection in page.detections:
            if detection.kind != "Tracker" or not detection.parent_id:
                continue
            trackers_by_parent.setdefault(detection.parent_id, []).append(
                detection
            )
            page_by_entity[detection.entity_id] = detection.page_number
    issues = [
        issue for issue in result.all_issues
        if issue.code != IssueCode.TRACKER_LENGTH_OUTLIER.value
    ]
    for inv in result.reviewed_tree.invs:
        samples: list[TrackerLengthSample] = []
        for node in inv.nodes:
            trackers = trackers_by_parent.get(node.recognition_id, [])
            if not trackers or node.ocr_string_count <= 0:
                continue
            # Re-run the same classification the Node was built from. Its
            # inputs (Tracker order, OCR count, definitions) are stable, so
            # the assignment matches the reviewed Rows exactly.
            classification = classify_trackers(
                [tracker.bbox for tracker in trackers],
                node.ocr_string_count,
                definitions,
            )
            if classification is None:
                continue
            samples.extend(
                TrackerLengthSample(
                    tracker.entity_id,
                    tracker.bbox,
                    definitions[definition_index].strings_per_tracker,
                )
                for tracker, definition_index in zip(
                    trackers,
                    classification.definition_indexes,
                )
            )
        check = check_tracker_lengths(samples, tolerance)
        for outlier in check.outliers:
            sample = outlier.sample
            measured = long_side(sample.bbox)
            deviation = (
                abs(measured - outlier.expected_length) / outlier.expected_length
            )
            page_number = page_by_entity[sample.entity_id]
            issues.append(
                create_issue(
                    IssueCode.TRACKER_LENGTH_OUTLIER,
                    page_number=page_number,
                    entity_id=sample.entity_id,
                    bbox=sample.bbox,
                    tracker_id=sample.entity_id,
                    measured=measured,
                    deviation=deviation,
                    strings=sample.strings,
                    expected_length=outlier.expected_length,
                    inv_name=inv.name or '?',
                )
            )
    if tuple(issues) == result.all_issues:
        return result
    return with_issues(result, issues)


class Agent3ElementRecognizer:
    """Build a reviewable project tree from Array Layout pages."""

    def __init__(
        self,
        yolo: YoloClient | None = None,
        table_recognizer: BlockDiagramTableRecognizer | None = None,
        node_ocr: object | None = None,
        node_bedrock: object | None = None,
        *,
        node_qwen: QwenLlmProvider | None = None,
        node_name_reviewer: NodeNameReviewer | None = None,
        node_name_review_enabled: bool | None = None,
        io_workers: int | None = None,
        page_workers: int | None = None,
        detect_max_side: int | None = None,
        node_ocr_orientations: int | None = None,
        grayscale: bool | None = None,
        line_enhance: bool | None = None,
    ) -> None:
        self._yolo = yolo or YoloClient.from_defaults()
        self._table_recognizer = table_recognizer or BlockDiagramTableRecognizer.from_env()
        # ``node_ocr`` remains injectable for deterministic tests. The
        # ``node_bedrock`` alias preserves source compatibility for callers
        # that injected the old image provider; production always creates the
        # LAN Qwen provider.
        self._node_ocr = node_ocr
        self._node_qwen = node_qwen or node_bedrock
        # Node-name reading lives in ``node_label``; the injected providers
        # and the lazy Qwen initialization sit behind that facade.
        self._node_names = NodeNameRecognizer(
            node_ocr=self._node_ocr,
            qwen=self._node_qwen,
        )
        # Production runs always use the real table recognizer, so attach the
        # page-level Claude review automatically and reuse its Bedrock client
        # and transport limit when available. Tests/integrators that inject a
        # lightweight table fake remain network-free unless they explicitly
        # inject a reviewer too. ``AGENT3_NODE_REVIEW_ENABLED=false`` (or the
        # constructor argument) disables the review stage entirely, so the
        # preliminary per-crop readings can be measured on their own.
        self._node_review_enabled = _resolve_node_review_enabled(
            node_name_review_enabled
        )
        if not self._node_review_enabled:
            self._node_name_reviewer = None
        elif node_name_reviewer is not None:
            self._node_name_reviewer = node_name_reviewer
        elif isinstance(self._table_recognizer, BlockDiagramTableRecognizer):
            self._node_name_reviewer = NodeNameReviewer(
                bedrock=self._table_recognizer.bedrock,
                max_transport_bytes=(
                    self._table_recognizer.config.bedrock_max_transport_bytes
                ),
            )
        else:
            self._node_name_reviewer = None
        self._io_workers = _resolve_io_workers(io_workers)
        self._page_workers = _resolve_page_workers(page_workers)
        self._detect_max_side = _resolve_detect_max_side(detect_max_side)
        self._grayscale = _resolve_grayscale(grayscale)
        self._line_enhance = _resolve_line_enhance(line_enhance)
        self._node_ocr_orientations = 1
        self._node_ocr_angles = (0,)

    def warmup(self, page_count: int | None = None) -> None:
        """Preload the local YOLO runtime before the first recognition.

        The first inference pays torch/ultralytics imports plus ONNX Runtime
        session creation; running it ahead of (or in parallel with) the first
        page keeps that cold-start off the recognition critical path.
        """

        if page_count is not None and page_count > 0:
            configure = getattr(self._yolo, "configure_parallelism", None)
            if callable(configure):
                configure(2 * min(page_count, self._page_workers))
        self._yolo.warmup()

    def recognize(
        self,
        request: ElementRecognitionRequest,
        *,
        on_progress: ProgressCallback | None = None,
        on_step: Agent3StepCallback | None = None,
        is_cancelled: CancellationCheck | None = None,
    ) -> ElementRecognitionResult:
        _validate_request(request)
        return self.recognize_stream(
            request,
            request.pages,
            on_progress=on_progress,
            on_step=on_step,
            is_cancelled=is_cancelled,
        )

    def recognize_stream(
        self,
        request: ElementRecognitionRequest,
        rendered_pages: Iterable[ArrayLayoutPageInput],
        *,
        on_progress: ProgressCallback | None = None,
        on_step: Agent3StepCallback | None = None,
        is_cancelled: CancellationCheck | None = None,
    ) -> ElementRecognitionResult:
        """Recognize pages while an upstream renderer is still producing them."""

        _validate_request_metadata(request)
        settings = {_inv_key(item.inv_name): item for item in request.inv_settings}
        identifiers, definition_identifiers = _length_identifiers(
            request.tracker_definitions
        )
        total = len(request.pages) * 5
        completed = 0
        page_results: list[ElementPageResult] = []
        invs: list[InvDraft] = []
        all_issues: list[RecognitionIssue] = []
        families: list[str] = []
        debug_pages: list[dict[str, object]] = []
        progress_lock = Lock()

        def checkpoint(increment: bool = False) -> None:
            nonlocal completed
            if is_cancelled is not None and is_cancelled():
                raise Agent3Cancelled("Element recognition was cancelled.")
            if increment:
                with progress_lock:
                    completed += 1
                    if on_progress is not None:
                        on_progress(completed, total)

        ordered_pages = sorted(request.pages, key=lambda item: item.page_number)
        page_workers = min(len(ordered_pages), self._page_workers)
        configure_parallelism = getattr(self._yolo, "configure_parallelism", None)
        yolo_workers = 1
        yolo_threads = max(1, os.cpu_count() or 1)
        if callable(configure_parallelism):
            configured = configure_parallelism(2 * page_workers)
            if (
                isinstance(configured, tuple)
                and len(configured) == 2
            ):
                yolo_workers, yolo_threads = configured
        table_workers = min(
            len(ordered_pages),
            max(1, self._io_workers // 2),
        )
        ocr_workers = max(1, self._io_workers)
        logger.info(
            "agent3.start pages=%s steps=%d io_workers=%d table_workers=%d "
            "page_workers=%d "
            "ocr_workers=%d yolo_workers=%d yolo_threads=%d "
            "detect_max_side=%d node_ocr_orientations=%d "
            "node_review=%s trackers=%d expected_invs=%d",
            [page.page_number for page in ordered_pages],
            total,
            self._io_workers,
            table_workers,
            page_workers,
            ocr_workers,
            yolo_workers,
            yolo_threads,
            self._detect_max_side,
            self._node_ocr_orientations,
            "on" if self._node_review_enabled else "off",
            len(request.tracker_definitions),
            len(request.expected_inv_names),
        )
        # Page stages yield provider futures and resume on the bounded local
        # pool. Pages may finish out of order; aggregation
        # below restores the stable page order promised by the public result.
        table_pool = ThreadPoolExecutor(
            max_workers=table_workers,
            thread_name_prefix="agent3-table",
        )
        ocr_pool = ThreadPoolExecutor(
            max_workers=ocr_workers,
            thread_name_prefix="agent3-ocr",
        )
        page_pool = ThreadPoolExecutor(
            max_workers=page_workers,
            thread_name_prefix="agent3-page",
        )
        detection_pool = ThreadPoolExecutor(
            max_workers=2 * page_workers, thread_name_prefix="agent3-detect",
        )
        review_pool = ThreadPoolExecutor(
            max_workers=table_workers, thread_name_prefix="agent3-review",
        )
        expected_page_numbers = {page.page_number for page in ordered_pages}
        seen_page_numbers: set[int] = set()
        page_futures: dict[Future, int] = {}
        try:
            checkpoint()
            for page in rendered_pages:
                checkpoint()
                if page.page_number not in expected_page_numbers:
                    raise Agent3Error(
                        f"Unexpected rendered Array Layout page: {page.page_number}"
                    )
                if page.page_number in seen_page_numbers:
                    raise Agent3Error(
                        f"Duplicate rendered Array Layout page: {page.page_number}"
                    )
                if not page.png_bytes:
                    raise Agent3Error(
                        f"Rendered Array Layout page {page.page_number} is empty."
                    )
                # Optional grayscale for table OCR / Node crops. Line enhance is
                # applied later only to Tracker/Box YOLO detection.
                if self._grayscale:
                    page = replace(
                        page,
                        png_bytes=encode_agent3_page_png(
                            page.png_bytes,
                            grayscale=True,
                        ),
                    )
                seen_page_numbers.add(page.page_number)
                table_future = table_pool.submit(
                    self._recognize_table,
                    page,
                    on_step,
                )
                future = submit_page(
                    page_pool,
                    self._recognize_page,
                    page,
                    request.tracker_definitions,
                    definition_identifiers,
                    request.default_panel_orientation,
                    request.default_offset_snap_step_strings,
                    settings,
                    request.expected_inv_names,
                    checkpoint,
                    ocr_pool,
                    table_future,
                    on_step,
                    detection_pool,
                    review_pool,
                )
                page_futures[future] = page.page_number

            missing = expected_page_numbers - seen_page_numbers
            if missing:
                pages = ", ".join(str(item) for item in sorted(missing))
                raise Agent3Error(f"Agent3 renderer did not produce page(s): {pages}")

            completed_pages: list[
                tuple[ElementPageResult, InvDraft, str, dict[str, object]]
            ] = []
            for future in as_completed(page_futures):
                checkpoint()
                completed_pages.append(future.result())
            completed_pages.sort(key=lambda item: item[0].page_number)
            for page_output, inv, family, page_debug in completed_pages:
                page_results.append(page_output)
                invs.append(inv)
                debug_pages.append(page_debug)
                if family:
                    families.append(family)
        finally:
            for future in page_futures:
                future.cancel()
            # Running HTTP/inference calls cannot be force-cancelled. Keep the
            # Agent3 QThread alive until they finish so a retry cannot overlap
            # hidden work from the previous run and retain its page bitmaps.
            page_pool.shutdown(wait=True, cancel_futures=True)
            table_pool.shutdown(wait=True, cancel_futures=True)
            ocr_pool.shutdown(wait=True, cancel_futures=True)
            review_pool.shutdown(wait=True, cancel_futures=True)
            detection_pool.shutdown(wait=True, cancel_futures=True)

        _report_step(on_step, "finalizing", "running")
        family = families[0] if families else ""
        if any(item != family for item in families[1:]):
            all_issues.append(
                create_issue(
                    IssueCode.MIXED_EQUIPMENT_FAMILY,
                )
            )
        project_type = "IBEX" if family == "CBX" else "LYNX" if family == "LBD" else ""
        merged_invs: dict[str, InvDraft] = {}
        for page_result, inv in zip(page_results, invs):
            # Keep unresolved pages as separate unnamed INV branches. Named
            # pages for the same INV are intentionally merged in page order.
            merge_key = _inv_key(inv.name) or f"__page_{page_result.page_number}"
            existing = merged_invs.get(merge_key)
            if existing is None:
                merged_invs[merge_key] = inv
            else:
                merged_invs[merge_key] = replace(
                    existing,
                    nodes=(*existing.nodes, *inv.nodes),
                    issues=(*existing.issues, *inv.issues),
                )
        result = ElementRecognitionResult(
            reviewed_tree=ReviewedTree(
                project_name=request.project_name,
                invs=tuple(
                    replace(
                        inv,
                        nodes=tuple(
                            sorted(inv.nodes, key=lambda node: natural_text_key(node.name))
                        ),
                    )
                    for inv in merged_invs.values()
                ),
                project_type=project_type,
                length_identifiers=identifiers,
            ),
            pages=tuple(page_results),
            issues=tuple(all_issues),
            equipment_family=family,
            inv_name_matches=match_inv_names(
                request.expected_inv_names,
                (inv.name for inv in merged_invs.values()),
            ),
            raw={"debug_pages": debug_pages},
        )
        if not result.has_output:
            _report_step(
                on_step,
                "finalizing",
                "failed",
                detail="No Node draft was produced.",
            )
            raise Agent3Error("Agent3 could not produce a Node draft from any page.")
        result = annotate_tracker_length_outliers(
            result,
            request.tracker_definitions,
        )
        _report_step(on_step, "finalizing", "completed")
        return result

    def _recognize_page(
        self,
        page: ArrayLayoutPageInput,
        definitions: tuple[TrackerDefinition, ...],
        definition_identifiers: tuple[str, ...],
        default_orientation: PanelOrientation,
        default_snap_step: float,
        settings: Mapping[str, InvRecognitionSettings],
        expected_inv_names: tuple[str, ...],
        checkpoint: Callable[[bool], None],
        ocr_pool: ThreadPoolExecutor,
        table_future: Future,
        on_step: Agent3StepCallback | None,
        detection_pool: ThreadPoolExecutor,
        review_pool: ThreadPoolExecutor,
    ) -> Generator[tuple[Future, ...], None, tuple[ElementPageResult, InvDraft, str, dict[str, object]]]:
        checkpoint()
        started = perf_counter()
        page_issues: list[RecognitionIssue] = []
        _report_step(on_step, "preparing", "running", page.page_number)
        # Decode the high-resolution page exactly once. Table OCR and Node
        # crops reuse this unenhanced bitmap. Tracker/Box YOLO downscales
        # first, then optionally line-enhances the detection copy.
        with Image.open(BytesIO(page.png_bytes)) as loaded:
            decoded = prepare_agent3_page_image(loaded)
            width, height = decoded.size
        detect_image, scale_x, scale_y = _downscale_for_detection(
            decoded,
            self._detect_max_side,
        )
        if self._line_enhance:
            from .line_enhance import enhance_line_image

            enhanced = enhance_line_image(detect_image)
            if enhanced is not detect_image:
                if detect_image is not decoded:
                    detect_image.close()
                detect_image = enhanced
            logger.info(
                "agent3.detect_prepare page=%d line_enhance=true after_scale=%dx%d",
                page.page_number,
                detect_image.width,
                detect_image.height,
            )
        _report_step(on_step, "preparing", "completed", page.page_number)

        # The table lookup was already queued for every page before the loop;
        # its network round trip overlaps the local YOLO inference.

        try:
            def detect(model_type, step):
                _report_step(on_step, step, "running", page.page_number)
                try:
                    result = self._yolo.detect(detect_image, model_type=model_type)
                except Exception as exc:
                    _report_step(on_step, step, "failed", page.page_number, str(exc))
                    raise
                _report_step(on_step, step, "completed", page.page_number)
                return result

            # Independent models share the immutable downscaled input. The router
            # still enforces its CPU/session budget across all inference calls.
            tracker_future = detection_pool.submit(detect, TRACKER_MODEL_TYPE, "tracker_detection")
            box_future = detection_pool.submit(detect, BOX_MODEL_TYPE, "box_detection")
            tracker_detections = []
            box_detections = []
            tracker_detection_error = None
            box_detection_error = None
            try:
                tracker_detections = _rescale_detection_boxes(
                    tracker_future.result().detections, scale_x, scale_y,
                )
            except Exception as exc:
                tracker_detection_error = str(exc)
                page_issues.append(_page_issue(page, IssueCode.TRACKER_DETECTION_FAILED, str(exc)))
            try:
                box_detections = _rescale_detection_boxes(
                    box_future.result().detections, scale_x, scale_y,
                )
            except Exception as exc:
                box_detection_error = str(exc)
                page_issues.append(_page_issue(page, IssueCode.BOX_DETECTION_FAILED, str(exc)))
            checkpoint(True)
            checkpoint(True)

            nodes_raw = [item for item in tracker_detections if item.label.casefold() == "node"]
            trackers_raw = [item for item in tracker_detections if item.label.casefold() == "tracker"]
            boxes_raw = [item for item in box_detections if item.label.casefold() == "box"]
            nodes_raw.sort(key=_detection_order)
            trackers_raw.sort(key=_detection_order)
            boxes_raw.sort(key=_detection_order)

            detections: list[ElementDetection] = []
            node_ids: list[str] = []
            for index, item in enumerate(nodes_raw, start=1):
                entity_id = f"p{page.page_number}:node:{index}"
                node_ids.append(entity_id)
                detections.append(_element_detection(entity_id, "Node", page.page_number, item))
            for node_index, peers in _overlap_peers(nodes_raw).items():
                entity_id = node_ids[node_index]
                peer_ids = tuple(node_ids[peer_index] for peer_index, _iou in peers)
                max_iou = max(iou for _peer_index, iou in peers)
                page_issues.append(
                    create_issue(
                        IssueCode.OVERLAPPING_NODE_BOXES,
                        page_number=page.page_number,
                        entity_id=entity_id,
                        bbox=nodes_raw[node_index].box,
                        node_id=entity_id,
                        peers=', '.join(peer_ids),
                        max_iou=max_iou,
                        threshold=OVERLAP_ISSUE_IOU_THRESHOLD,
                    )
                )
            parent_boxes = list(zip(node_ids, (item.box for item in nodes_raw)))
            tracker_elements: list[ElementDetection] = []
            for index, item in enumerate(trackers_raw, start=1):
                entity_id = f"p{page.page_number}:tracker:{index}"
                parent_id = assign_parent(item.box, parent_boxes)
                element = _element_detection(entity_id, "Tracker", page.page_number, item, parent_id)
                tracker_elements.append(element)
                detections.append(element)
                if not parent_id:
                    page_issues.append(
                        create_issue(
                            IssueCode.UNASSIGNED_TRACKER,
                            page_number=page.page_number,
                            entity_id=entity_id,
                            bbox=item.box,
                        )
                    )
            node_boxes_by_id = dict(parent_boxes)
            tracker_overlap_entities: dict[
                str, list[tuple[str, str, float]]
            ] = {}
            for left_index, right_index, iou in _overlapping_pairs(trackers_raw):
                left = tracker_elements[left_index]
                right = tracker_elements[right_index]
                # An overlapping Tracker pair makes every owning Node ambiguous.
                # If a Tracker is unassigned, keep the issue visible on that
                # Tracker because there is no corresponding Node to highlight.
                affected_entities = {
                    left.parent_id or left.entity_id,
                    right.parent_id or right.entity_id,
                }
                for entity_id in affected_entities:
                    tracker_overlap_entities.setdefault(entity_id, []).append(
                        (left.entity_id, right.entity_id, iou)
                    )
            tracker_boxes_by_id = {
                item.entity_id: item.bbox for item in tracker_elements
            }
            for entity_id, overlaps in tracker_overlap_entities.items():
                pair_labels = ", ".join(
                    f"{left}/{right}" for left, right, _iou in overlaps
                )
                max_iou = max(iou for _left, _right, iou in overlaps)
                is_node = entity_id in node_boxes_by_id
                page_issues.append(
                    create_issue(
                        IssueCode.OVERLAPPING_TRACKER_BOXES,
                        page_number=page.page_number,
                        entity_id=entity_id,
                        bbox=node_boxes_by_id[entity_id] if is_node else tracker_boxes_by_id[entity_id],
                        pair_labels=pair_labels,
                        max_iou=max_iou,
                        target='Node' if is_node else 'unassigned Tracker',
                        target_id=entity_id,
                        threshold=OVERLAP_ISSUE_IOU_THRESHOLD,
                    )
                )
            box_elements: list[ElementDetection] = []
            box_parent_ids = assign_boxes_to_nodes(
                [item.box for item in boxes_raw],
                parent_boxes,
            )
            for index, (item, parent_id) in enumerate(
                zip(boxes_raw, box_parent_ids),
                start=1,
            ):
                entity_id = f"p{page.page_number}:box:{index}"
                element = _element_detection(entity_id, "Box", page.page_number, item, parent_id)
                box_elements.append(element)
                detections.append(element)
                if not parent_id:
                    page_issues.append(
                        create_issue(
                            IssueCode.UNASSIGNED_BOX,
                            page_number=page.page_number,
                            entity_id=entity_id,
                            bbox=item.box,
                        )
                    )

            # Only compressed crops survive a provider wait. Local page workers
            # can now prepare later pages without accumulating full RGB drawings.
            node_crops = [
                detection_pool.submit(self._node_names.encode_crop, decoded, item.box)
                for item in nodes_raw
            ]
            # PNG compression remains parallel, but never occupies HTTP slots.
            # Drain all encoders before releasing the shared drawing; individual
            # crop failures are consumed by the normal per-Node OCR fallback.
            wait(node_crops)
            detect_width, detect_height = detect_image.size
        finally:
            if detect_image is not decoded:
                detect_image.close()
            decoded.close()
        yield (table_future,)
        checkpoint()

        table: BlockDiagramTableResult | None = None
        table_error: str | None = None
        table_raw_llm: dict[str, object] | None = None
        table_location: TableLocation | None = None
        try:
            table = table_future.result()
        except Exception as exc:
            table_error = str(exc)
            # LLM parse/validation failures carry the verbatim response
            # (BlockDiagramTableError.raw_llm) so debug exports can show
            # exactly what the model returned.
            table_raw_llm = getattr(exc, "raw_llm", None)
            # A locate-phase success outlives an extraction failure: keep the
            # found region so the window can still draw it.
            table_location = getattr(exc, "location", None)
            page_issues.append(_page_issue(page, IssueCode.NODE_STRING_TABLE_FAILED, str(exc)))
        checkpoint(True)

        # Only the table's Node names go into every Node prompt, so Qwen can
        # return the canonical spelling already extracted for this page. String
        # counts stay out: they are unrelated to the label and used to be
        # echoed back as a plausible-looking wrong name.
        table_rows = tuple(
            NodeStringTableRow(item.node, item.string_number)
            for item in (table.string_info if table is not None else ())
        )
        _report_step(on_step, "qwen_node_names", "running", page.page_number)
        node_ocr_futures: list[list[tuple[int, Future]]] = [
            [
                (
                    0,
                    ocr_pool.submit(
                        self._recognize_prepared_node_crop,
                        node_crops[index - 1],
                        page.page_number,
                        index,
                        table_rows,
                    ),
                )
            ]
            for index, item in enumerate(nodes_raw, start=1)
        ]

        yield tuple(future for readings in node_ocr_futures for _angle, future in readings)
        checkpoint()
        node_crops.clear()
        table_by_name = {
            _normalize_name(item.node): item
            for item in (table.string_info if table is not None else ())
        }
        used_table_names: set[str] = set()
        node_names: list[tuple[str, str]] = []
        node_ocr_meta: list[tuple[int, float | None]] = []
        node_ocr_debug: list[dict[str, object]] = []
        for index, ((node_id, item), futures) in enumerate(
            zip(zip(node_ids, nodes_raw), node_ocr_futures),
            start=1,
        ):
            raw_text, angle, confidence, error, readings = _collect_node_crop_text(
                futures
            )
            if error is not None:
                page_issues.append(
                    create_issue(
                        IssueCode.NODE_NAME_OCR_FAILED,
                        page_number=page.page_number,
                        entity_id=node_id,
                        bbox=item.box,
                        detail=error,
                    )
                )
            matched = _match_table_name(raw_text, table_by_name, used_table_names)
            if matched:
                used_table_names.add(_normalize_name(matched))
            fallback_name = "" if matched else _extract_equipment_name(raw_text)
            # The reader may answer "No Match" for a crop that shows no listed
            # equipment name; that answer arrives as an empty reading. Keep the
            # numbered placeholder so the review pass and the manual editor can
            # still identify and resolve the Node, and say why the name is a
            # placeholder. A hard OCR failure is already reported above.
            no_match = error is None and not matched and not fallback_name
            name = matched or fallback_name or f"Node{index}"
            if no_match:
                page_issues.append(
                    create_issue(
                        IssueCode.NODE_NAME_NO_MATCH,
                        page_number=page.page_number,
                        entity_id=node_id,
                        bbox=item.box,
                        name=name,
                    )
                )
            node_names.append((name, raw_text))
            node_ocr_meta.append((angle, confidence))
            node_ocr_debug.append(
                {
                    "node_index": index,
                    "entity_id": node_id,
                    "node_bbox": bbox_debug_dict(item.box),
                    "readings": readings,
                    "selected": {
                        "text": raw_text,
                        "angle_deg": angle,
                        "confidence": confidence,
                    },
                    "matched_table_name": matched or None,
                    "no_match": no_match,
                    "preliminary_node_name": name,
                    "final_node_name": name,
                    "error": error,
                }
            )
        checkpoint(True)
        _report_step(on_step, "qwen_node_names", "completed", page.page_number)

        # Claude now performs the page-level consistency pass requested by
        # the workflow contract. The original full page is the visual input;
        # table rows and the stable number/name/bbox relation are JSON inside
        # the prompt. A malformed or failed response never discards usable
        # Qwen results: it is recorded as a review issue and falls back.
        node_review_debug: dict[str, object] = {
            "status": "skipped",
            "enabled": self._node_review_enabled,
            "reason": (
                "No Claude Node-name reviewer is configured."
                if self._node_review_enabled
                else "Node-name review is disabled (AGENT3_NODE_REVIEW_ENABLED=false)."
            ),
        }
        reviewed_inv_name: str | None = None
        reviewer = self._node_name_reviewer
        if reviewer is not None and nodes_raw:
            _report_step(
                on_step,
                "claude_node_review",
                "running",
                page.page_number,
            )
            try:
                review_future = review_pool.submit(
                    reviewer.review,
                    page.png_bytes,
                    table_rows,
                    tuple(
                        NodeNameReviewCandidate(
                            number=index,
                            recognized_name=node_names[index - 1][0],
                            bbox=item.box,
                            raw_ocr_text=node_names[index - 1][1],
                        )
                        for index, item in enumerate(nodes_raw, start=1)
                    ),
                    expected_inv_names,
                )
                yield (review_future,)
                checkpoint()
                review = review_future.result()
            except Agent3Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - page-level fallback
                message = str(exc)
                raw_llm = (
                    exc.raw_llm
                    if isinstance(exc, NodeNameReviewError)
                    else None
                )
                page_issues.append(
                    create_issue(
                        IssueCode.NODE_NAME_REVIEW_FAILED,
                        page_number=page.page_number,
                        detail=message,
                    )
                )
                node_review_debug = {
                    "status": "failed",
                    "error": message,
                    "raw_llm": raw_llm,
                }
                _report_step(
                    on_step,
                    "claude_node_review",
                    "failed",
                    page.page_number,
                    message,
                )
            else:
                node_names = [
                    (
                        review.corrections_by_number.get(index, old_name),
                        raw_text,
                    )
                    for index, (old_name, raw_text) in enumerate(
                        node_names,
                        start=1,
                    )
                ]
                reviewed_inv_name = getattr(review, "inv_name", None)
                for debug, (name, _raw_text) in zip(
                    node_ocr_debug,
                    node_names,
                ):
                    debug["final_node_name"] = name
                node_review_debug = {
                    "status": "completed",
                    "all_correct": bool(
                        getattr(review, "all_correct", False)
                    ),
                    "inv_name": reviewed_inv_name,
                    "inv_name_source": (
                        "claude" if reviewed_inv_name else "legacy_fallback"
                    ),
                    "prompt": review.prompt,
                    "corrected_node_names": [
                        {"number": number, "name": name}
                        for number, name in sorted(
                            review.corrections_by_number.items()
                        )
                    ],
                    "raw_output": review.raw_output,
                }
                _report_step(
                    on_step,
                    "claude_node_review",
                    "completed",
                    page.page_number,
                )

        page_issues.extend(node_name_issues(
            page.page_number,
            (
                (node_id, name, item.box)
                for (node_id, item), (name, _raw_text) in zip(
                    zip(node_ids, nodes_raw), node_names,
                )
            ),
            table_by_name,
        ))

        _report_step(on_step, "geometry", "running", page.page_number)
        if reviewed_inv_name:
            inv_name = reviewed_inv_name
            inv_candidates = (reviewed_inv_name,)
            inv_issue = None
        else:
            inv_name, inv_candidates, inv_issue = _infer_page_inv_name(
                tuple(
                    name if _extract_inv_prefix(name) else raw_text
                    for name, raw_text in node_names
                ),
                expected_inv_names,
                page.page_number,
            )
        if inv_issue is not None:
            page_issues.append(inv_issue)
        agent2_match = best_agent2_inv_match(inv_name, expected_inv_names)
        settings_name = (
            agent2_match.agent2_name if agent2_match is not None else inv_name
        )
        inv_settings = settings.get(_inv_key(settings_name))

        orientation = (
            inv_settings.panel_orientation
            if inv_settings is not None and inv_settings.panel_orientation is not None
            else default_orientation
        )
        snap_step = (
            inv_settings.offset_snap_step_strings
            if inv_settings is not None
            else default_snap_step
        )
        nodes: list[NodeDraft] = []
        for index, (node_id, node_detection) in enumerate(zip(node_ids, nodes_raw)):
            name, raw_text = node_names[index]
            angle, confidence = node_ocr_meta[index]
            node_trackers = [item for item in tracker_elements if item.parent_id == node_id]
            node_boxes = [item for item in box_elements if item.parent_id == node_id]
            normalized = _normalize_name(name)
            table_row = table_by_name.get(normalized)
            if table_row is not None:
                ocr_count = table_row.string_number
                has_ocr_string_count = True
            else:
                ocr_count = 0
                has_ocr_string_count = False
            node, node_issues = _build_node(
                page,
                node_id,
                name,
                raw_text,
                node_detection,
                node_trackers,
                node_boxes,
                ocr_count,
                definitions,
                definition_identifiers,
                orientation,
                snap_step,
                ocr_angle=angle,
                ocr_confidence=confidence,
                has_ocr_string_count=has_ocr_string_count,
            )
            nodes.append(node)
            page_issues.extend(node_issues)
        checkpoint(True)

        family = _equipment_family(table, tuple(item.name for item in nodes))
        inv_issue_codes = tuple(
            issue.code for issue in page_issues if issue.severity == RecognitionIssueSeverity.ERROR
        )
        inv = InvDraft(
            name=inv_name,
            page=page.page_number,
            nodes=tuple(nodes),
            source=SourceReference(
                page_number=page.page_number,
                source="agent3:node-name-majority",
                raw={
                    "inv_candidates": list(inv_candidates),
                    "matched_agent2_inv_name": (
                        agent2_match.agent2_name
                        if agent2_match is not None
                        else None
                    ),
                    "inv_name_similarity": (
                        agent2_match.similarity
                        if agent2_match is not None
                        else None
                    ),
                    "inv_name_source": (
                        "claude_node_review"
                        if reviewed_inv_name
                        else "node_name_format_fallback"
                    ),
                },
            ),
            state=RecognitionState.NEEDS_REVIEW,
            issues=inv_issue_codes,
            recognition_id=f"p{page.page_number}:inv",
            panel_orientation=orientation.value,
            offset_snap_step_strings=snap_step,
            inherits_panel_orientation=(
                inv_settings is None
                or inv_settings.panel_orientation is None
            ),
            inherits_offset_snap_step=(inv_settings is None),
        )
        issue_codes_by_entity: dict[str, list[str]] = {}
        for issue in page_issues:
            if issue.entity_id:
                issue_codes_by_entity.setdefault(issue.entity_id, []).append(
                    issue.code
                )
        detections = [
            replace(
                item,
                issues=tuple(issue_codes_by_entity.get(item.entity_id, ())),
            )
            for item in detections
        ]
        logger.info(
            "agent3.page page=%d seconds=%.2f trackers=%d boxes=%d nodes=%d "
            "issues=%d",
            page.page_number,
            perf_counter() - started,
            len(trackers_raw),
            len(boxes_raw),
            len(nodes_raw),
            len(page_issues),
        )
        _report_step(on_step, "geometry", "completed", page.page_number)
        located_bbox = (
            table.table_bbox
            if table is not None and table.table_bbox is not None
            else (table_location.bbox if table_location is not None else None)
        )
        located_regions = (
            tuple(
                bbox_to_pixels(region.bbox, width, height)
                for region in table_location.regions
            )
            if table_location is not None
            else ()
        )
        return (
            ElementPageResult(
                page_number=page.page_number,
                inv_name=inv_name,
                image_width=width,
                image_height=height,
                detections=tuple(detections),
                table_rows=table_rows,
                issues=tuple(page_issues),
                table_bbox=(
                    bbox_to_pixels(located_bbox, width, height)
                    if located_bbox is not None
                    else None
                ),
                table_bbox_source=_table_bbox_source(table, table_location),
                table_bboxes=located_regions,
            ),
            inv,
            family,
            {
                "page_number": page.page_number,
                "input": input_image_debug_dict(page.page_number, page.png_bytes),
                "table_recognition": table_result_debug_dict(
                    table,
                    table_error,
                    raw_llm=table_raw_llm,
                    location=table_location,
                ),
                "yolo_tracker_detection": {
                    "model_type": TRACKER_MODEL_TYPE,
                    "input_size": {
                        "width": detect_width,
                        "height": detect_height,
                    },
                    "coordinates": "original_page_pixels",
                    "scale_to_original_page": {
                        "x": scale_x,
                        "y": scale_y,
                    },
                    "detections": [
                        detection_result_debug_dict(item)
                        for item in tracker_detections
                    ],
                    "error": tracker_detection_error,
                },
                "yolo_box_detection": {
                    "model_type": BOX_MODEL_TYPE,
                    "input_size": {
                        "width": detect_width,
                        "height": detect_height,
                    },
                    "coordinates": "original_page_pixels",
                    "scale_to_original_page": {
                        "x": scale_x,
                        "y": scale_y,
                    },
                    "detections": [
                        detection_result_debug_dict(item)
                        for item in box_detections
                    ],
                    "error": box_detection_error,
                },
                "ocr_node_names": node_ocr_debug,
                "claude_node_name_review": node_review_debug,
            },
        )

    def _recognize_table(
        self,
        page: ArrayLayoutPageInput,
        on_step: Agent3StepCallback | None = None,
    ) -> BlockDiagramTableResult:
        _report_step(on_step, "table_recognition", "running", page.page_number)
        try:
            result = self._table_recognizer.recognize(page.png_bytes)
        except Exception as exc:
            _report_step(
                on_step,
                "table_recognition",
                "failed",
                page.page_number,
                str(exc),
            )
            raise
        _report_step(on_step, "table_recognition", "completed", page.page_number)
        return result

    def _recognize_prepared_node_crop(
        self,
        crop: Future,
        page_number: int,
        index: int,
        table_rows: tuple[NodeStringTableRow, ...],
    ) -> tuple[str, float | None]:
        return self._node_names.read_image(crop.result(), page_number, index, table_rows)

    def _recognize_node_crop(
        self,
        decoded: Image.Image,
        bbox: BoundingBox,
        page_number: int,
        index: int,
        table_rows: tuple[NodeStringTableRow, ...] = (),
    ) -> tuple[str, float | None]:
        """Read one Node label crop via :mod:`node_label`.

        Kept as a source-compatibility hook for callers that inject Node
        providers on the recognizer; the reading logic lives in
        ``node_label.recognizer``.
        """
        return self._node_names.read_crop(
            decoded,
            bbox,
            page_number,
            index,
            table_rows,
        )


def _downscale_for_detection(
    decoded: Image.Image,
    max_side: int,
) -> tuple[Image.Image, float, float]:
    """Return a shared detection bitmap plus x/y scales back to page pixels.

    Both bundled models letterbox their input to fixed exported square sizes
    (2560/3072px), so any source far above that carries no extra detection
    information while multiplying preprocess cost and peak memory.
    """

    longest = max(decoded.size)
    if max_side <= 0 or longest <= max_side:
        return decoded, 1.0, 1.0
    ratio = max_side / longest
    target = (
        max(1, round(decoded.width * ratio)),
        max(1, round(decoded.height * ratio)),
    )
    resized = decoded.resize(target, Image.Resampling.BILINEAR)
    return (
        resized,
        decoded.width / target[0],
        decoded.height / target[1],
    )


def _rescale_detection_boxes(
    items: list,
    scale_x: float,
    scale_y: float,
) -> list:
    """Map detections from the downscaled bitmap back to page coordinates."""

    if scale_x == 1.0 and scale_y == 1.0:
        return list(items)
    rescaled: list = []
    for item in items:
        box = item.box
        rescaled.append(
            replace(
                item,
                box=BoundingBox(
                    box.x1 * scale_x,
                    box.y1 * scale_y,
                    box.x2 * scale_x,
                    box.y2 * scale_y,
                ),
            )
        )
    return rescaled


def _build_node(
    page: ArrayLayoutPageInput,
    node_id: str,
    name: str,
    raw_text: str,
    node_detection,
    trackers: list[ElementDetection],
    boxes: list[ElementDetection],
    ocr_count: int,
    definitions: tuple[TrackerDefinition, ...],
    definition_identifiers: tuple[str, ...],
    orientation: PanelOrientation,
    snap_step: float,
    *,
    ocr_angle: int = 0,
    ocr_confidence: float | None = None,
    has_ocr_string_count: bool = True,
) -> tuple[NodeDraft, list[RecognitionIssue]]:
    issues: list[RecognitionIssue] = []
    tracker_boxes = [item.bbox for item in trackers]
    classification = classify_trackers(tracker_boxes, ocr_count, definitions)
    assigned = classification.definition_indexes if classification is not None else ()
    if trackers and classification is None:
        issues.append(
            create_issue(
                IssueCode.TRACKER_STRING_BALANCE_FAILED,
                page_number=page.page_number,
                entity_id=node_id,
                bbox=node_detection.box,
                ocr_count=ocr_count,
                name=name,
            )
        )
    if not trackers:
        issues.append(
            create_issue(
                IssueCode.NODE_HAS_NO_TRACKERS,
                page_number=page.page_number,
                entity_id=node_id,
                bbox=node_detection.box,
                name=name,
            )
        )
    if not boxes:
        issues.append(
            create_issue(
                IssueCode.NODE_HAS_NO_BOX,
                page_number=page.page_number,
                entity_id=node_id,
                bbox=node_detection.box,
                name=name,
            )
        )
    if len(boxes) > 1:
        issues.append(
            create_issue(
                IssueCode.NODE_HAS_MULTIPLE_BOXES,
                page_number=page.page_number,
                entity_id=node_id,
                bbox=node_detection.box,
                name=name,
            )
        )
    primary_box = (
        min(boxes, key=lambda item: _box_distance(item.bbox, node_detection.box))
        if boxes
        else None
    )
    u_axis, row_groups, pitch = group_rows(tracker_boxes)
    one_string_lengths = [
        long_side(box) / definitions[definition_index].strings_per_tracker
        for box, definition_index in zip(tracker_boxes, assigned)
        if definitions[definition_index].strings_per_tracker > 0
    ]
    one_string_px = mean(one_string_lengths) if one_string_lengths else 0.0
    rows: list[RowDraft] = []
    first_row_center = row_groups[0].v_center if row_groups else 0.0
    for group in row_groups:
        entries = _build_row_entries(
            group.tracker_indexes,
            trackers,
            assigned,
            definitions,
            definition_identifiers,
            primary_box,
            u_axis,
            one_string_px,
            orientation,
            snap_step,
        )
        row_box = _union_box([trackers[item].bbox for item in group.tracker_indexes])
        row_position = 1.0 + max(
            0.0,
            round((group.v_center - first_row_center) / max(pitch, 1e-6)),
        )
        position_text = f"{row_position:g}"
        rows.append(
            RowDraft(
                name=f"Row{position_text}",
                rackings=tuple(item.rackings for item in entries),
                racking_entries=entries,
                direction=u_axis,
                source=SourceReference(page_number=page.page_number, bbox=row_box, source="agent3:row-grouping"),
                state=RecognitionState.NEEDS_REVIEW,
                recognition_id=f"{node_id}:row:{group.index}",
            )
        )
    box_position = 0.0
    if primary_box is not None and row_groups:
        v_axis = "y" if u_axis == "x" else "x"
        if len(row_groups) == 1:
            # A single Row provides no measurable row pitch. ``group_rows``
            # returns 1 px as a neutral fallback, but treating that value as
            # a real pitch amplifies normal YOLO sub-pixel jitter into a false
            # half-row Box position. The only defensible automatic coordinate
            # is the sole known Row itself; flag visibly offset Boxes for
            # review instead of inventing a scale from pixels.
            box_position = 1.0
            group_boxes = [
                tracker_boxes[index]
                for index in row_groups[0].tracker_indexes
            ]
            if v_axis == "x":
                row_v1 = min(box.x1 for box in group_boxes)
                row_v2 = max(box.x2 for box in group_boxes)
            else:
                row_v1 = min(box.y1 for box in group_boxes)
                row_v2 = max(box.y2 for box in group_boxes)
            alignment_tolerance = max(
                1.0,
                median(short_side(box) for box in group_boxes) * 0.5,
            )
            box_v = axis_center(primary_box.bbox, v_axis)
            if not (
                row_v1 - alignment_tolerance
                <= box_v
                <= row_v2 + alignment_tolerance
            ):
                issues.append(
                    create_issue(
                        IssueCode.SINGLE_ROW_BOX_POSITION_AMBIGUOUS,
                        page_number=page.page_number,
                        entity_id=node_id,
                        bbox=primary_box.bbox,
                        name=name,
                    )
                )
        else:
            raw_position = 1.0 + (
                axis_center(primary_box.bbox, v_axis) - row_groups[0].v_center
            ) / max(pitch, 1e-6)
            box_position = snap_positive(max(0.0, raw_position), 0.5)
    counts = Counter(assigned)
    quantities = tuple(
        TrackerQuantity(definition.code, counts[index])
        for index, definition in enumerate(definitions)
        if counts[index]
    )
    annotations = [
        DetectionAnnotation(
            label=name,
            source=SourceReference(
                page_number=page.page_number,
                bbox=node_detection.box,
                confidence=node_detection.confidence,
                model_version="node_tracker_yolov8n.onnx",
                recognition_version="agent3-geometry-v1",
                source=node_detection.source,
                raw=_node_ocr_raw(raw_text, ocr_angle, ocr_confidence),
            ),
            state=RecognitionState.NEEDS_REVIEW,
            issues=tuple(issue.code for issue in issues),
        )
    ]
    for item in trackers + boxes:
        annotations.append(
            DetectionAnnotation(
                label=item.kind,
                source=SourceReference(
                    page_number=page.page_number,
                    bbox=item.bbox,
                    confidence=item.confidence,
                    model_version=("box_yolov8n.onnx" if item.kind == "Box" else "node_tracker_yolov8n.onnx"),
                    recognition_version="agent3-geometry-v1",
                    source=item.source,
                ),
                state=RecognitionState.NEEDS_REVIEW,
                issues=item.issues,
            )
        )
    node = NodeDraft(
            name=name,
            box_position=box_position,
            rows=tuple(rows),
            annotations=tuple(annotations),
            source=annotations[0].source,
            state=RecognitionState.NEEDS_REVIEW,
            issues=tuple(issue.code for issue in issues),
            ocr_string_count=ocr_count,
            has_ocr_string_count=has_ocr_string_count,
            tracker_counts=quantities,
            recognition_id=node_id,
            panel_orientation=orientation.value,
            offset_snap_step_strings=snap_step,
        )
    # ``tracker_counts`` are re-derived from the reviewed Rows, but
    # ``ocr_string_count`` must keep the table-recognition value. Agent4 uses
    # it as the reference for the structure total; overwriting it here would
    # make that comparison compare the derived total with itself.
    _, row_tracker_counts = summarize_node_rows(
        node,
        tracker_definitions=definitions,
    )
    return replace(node, tracker_counts=row_tracker_counts), issues


def _build_row_entries(
    tracker_indexes: tuple[int, ...],
    trackers: list[ElementDetection],
    assigned: tuple[int, ...],
    definitions: tuple[TrackerDefinition, ...],
    identifiers: tuple[str, ...],
    primary_box: ElementDetection | None,
    u_axis: str,
    one_string_px: float,
    orientation: PanelOrientation,
    snap_step: float,
) -> tuple[RackingEntryDraft, ...]:
    if not tracker_indexes:
        return ()
    if primary_box is None:
        groups = [list(tracker_indexes)]
    else:
        box_u = axis_center(primary_box.bbox, u_axis)
        negative = [index for index in tracker_indexes if axis_center(trackers[index].bbox, u_axis) < box_u]
        positive = [index for index in tracker_indexes if index not in negative]
        groups = [group for group in (negative, positive) if group]
    entries: list[RackingEntryDraft] = []
    for group in groups:
        if primary_box is not None:
            group.sort(key=lambda index: interval_gap(primary_box.bbox, trackers[index].bbox, u_axis))
        else:
            group.sort(key=lambda index: axis_center(trackers[index].bbox, u_axis))
        widths = [definitions[assigned[index]].strings_per_tracker for index in group if index < len(assigned)]
        base_name = "+".join(str(width) for width in widths)
        polarity = ""
        north_side: bool | None = None
        east_side: bool | None = None
        offset = "0mm"
        ratio: float | None = None
        identifier = ""
        if primary_box is not None and group:
            first = trackers[group[0]]
            delta_u = axis_center(first.bbox, u_axis) - axis_center(primary_box.bbox, u_axis)
            delta_y = center(first.bbox)[1] - center(primary_box.bbox)[1]
            delta_x = center(first.bbox)[0] - center(primary_box.bbox)[0]
            north_side = delta_y < 0 if abs(delta_y) > 1e-6 else delta_u < 0
            east_side = delta_x > 0 if abs(delta_x) > 1e-6 else delta_u > 0
            polarity = polarity_for_side(
                orientation,
                side_delta_u=delta_u,
                side_delta_y=delta_y,
                side_delta_x=delta_x,
            )
            if one_string_px > 0 and group[0] < len(assigned):
                ratio = interval_gap(primary_box.bbox, first.bbox, u_axis) / one_string_px
                coefficient = snap_positive(ratio, snap_step)
                identifier = identifiers[assigned[group[0]]]
                offset = format_length(coefficient, identifier)
        rackings = f"{base_name} {polarity}".strip()
        entries.append(
            RackingEntryDraft(
                rackings=rackings,
                length_str=offset,
                string_pattern=base_name,
                north_side=north_side,
                east_side=east_side,
                offset_ratio_strings=ratio,
                length_identifier=identifier,
            )
        )
    return tuple(entries)


def _validate_request(request: ElementRecognitionRequest) -> None:
    _validate_request_metadata(request)
    for page in request.pages:
        if not page.png_bytes:
            raise Agent3Error("Every Array Layout page requires page_number and PNG bytes.")


def _validate_request_metadata(request: ElementRecognitionRequest) -> None:
    """Validate request metadata while permitting stream-backed empty images."""

    if not request.pages:
        raise Agent3Error("Agent3 requires at least one Array Layout page.")
    page_numbers: set[int] = set()
    for page in request.pages:
        if page.page_number <= 0:
            raise Agent3Error("Every Array Layout page requires a positive page_number.")
        if page.page_number in page_numbers:
            raise Agent3Error(f"Duplicate Array Layout page input: {page.page_number}")
        page_numbers.add(page.page_number)
    if not request.tracker_definitions:
        raise Agent3Error("Agent3 requires tracker definitions from Agent2.")
    for item in request.tracker_definitions:
        if not item.code.strip() or item.strings_per_tracker <= 0:
            raise Agent3Error("Tracker definitions require code and positive strings_per_tracker.")
    if request.default_offset_snap_step_strings <= 0:
        raise Agent3Error("default_offset_snap_step_strings must be positive.")
    for item in request.inv_settings:
        if item.offset_snap_step_strings <= 0:
            raise Agent3Error("offset_snap_step_strings must be positive.")


def _length_identifiers(
    definitions: tuple[TrackerDefinition, ...],
) -> tuple[tuple[OcrField, ...], tuple[str, ...]]:
    labels: list[str] = []
    names: list[str] = []
    for definition in definitions:
        label = definition.panel_label.strip() or definition.code.strip()
        if label not in labels:
            labels.append(label)
        names.append(_identifier_name(labels.index(label)))
    fields = tuple(OcrField(name=_identifier_name(index), value=label) for index, label in enumerate(labels))
    return fields, tuple(names)


def _identifier_name(index: int) -> str:
    # Excel-like alphabetic suffix: a..z, aa..az, ...
    value = index + 1
    suffix = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        suffix = chr(ord("a") + remainder) + suffix
    return f"str_{suffix}"


def _inv_key(value: str) -> str:
    """INV identity shared by Agent2 and Agent3 spellings.

    Spacing, separators and letter case differ freely between the two
    sources, and a page may print the INV token that another source omits
    (``INV1201`` for the listed ``1201``). Only the characters identify the
    INV, so pages that spell one INV differently still merge into one
    branch and reach the same Agent2 settings entry.
    """

    key = re.sub(r"[^A-Z0-9]", "", value.upper())
    return key[3:] if key.startswith("INV") and len(key) > 3 else key


def _expected_inv_spellings(
    expected_inv_names: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    """Group Agent2 INV spellings by identity, preserving list order."""

    grouped: dict[str, list[str]] = {}
    for value in expected_inv_names:
        name = str(value).strip()
        identity = _inv_key(name)
        if identity:
            grouped.setdefault(identity, []).append(name)
    return {identity: tuple(names) for identity, names in grouped.items()}


def _listed_inv_identity(
    node_name: str,
    listed: Mapping[str, tuple[str, ...]],
) -> str:
    """Return the Agent2 INV identity a Node name carries, if any.

    The prefix before the equipment token preserves the page's own spelling
    and is tried first; the order-insensitive equipment signature backs it
    up, so a separator, the opposite token order or an equipment token this
    parser does not know cannot hide an INV that Agent2 already reviewed.
    Only exact identities count: a merely similar name must stay on the
    review path instead of silently joining a listed INV.
    """

    prefix = _extract_inv_prefix(node_name)
    if prefix:
        identity = _inv_key(prefix)
        if identity in listed:
            return identity
    signature = _equipment_signature(node_name)
    if signature is not None and signature[0]:
        identity = _inv_key(signature[0])
        if identity in listed:
            return identity
    return ""


def _top_voted_keys(counts: Counter[str]) -> tuple[str, ...]:
    """Return every key tied for the highest vote, in insertion order."""

    if not counts:
        return ()
    highest = max(counts.values())
    return tuple(key for key, count in counts.items() if count == highest)


def _most_complete_inv_name(names: tuple[str, ...]) -> str:
    """Return the fullest spelling among equivalent Agent2 INV names."""

    return max(names, key=lambda name: len(_normalize_name(name)))


def _infer_page_inv_name(
    node_names: tuple[str, ...],
    expected_inv_names: tuple[str, ...],
    page_number: int,
) -> tuple[str, tuple[str, ...], RecognitionIssue | None]:
    candidates = tuple(
        candidate
        for candidate in (_extract_inv_prefix(name) for name in node_names)
        if candidate
    )
    entity_id = f"p{page_number}:inv"

    # Agent2's list is the INV authority.  Vote on its identities before
    # trusting any local format, so the page's separators or token shape can
    # never turn an INV Agent2 already reviewed into an unnamed page.
    listed = _expected_inv_spellings(expected_inv_names)
    listed_votes: Counter[str] = Counter()
    for name in node_names:
        identity = _listed_inv_identity(name, listed)
        if identity:
            listed_votes[identity] += 1
    listed_winners = _top_voted_keys(listed_votes)
    if len(listed_winners) == 1:
        winner = listed_winners[0]
        # Keep the page's own spelling when it was parsed; otherwise the
        # reviewed list spelling is the only complete one available.
        spelling = next(
            (item for item in candidates if _inv_key(item) == winner),
            "",
        )
        return (
            spelling or _most_complete_inv_name(listed[winner]),
            candidates,
            None,
        )
    if listed_winners:
        return (
            "",
            candidates,
            create_issue(
                IssueCode.INV_NAME_AMBIGUOUS,
                page_number=page_number,
                entity_id=entity_id,
                candidates=', '.join((_most_complete_inv_name(listed[key]) for key in listed_winners)),
            ),
        )

    if not candidates:
        return (
            "",
            (),
            create_issue(
                IssueCode.INV_NAME_NOT_RECOGNIZED,
                page_number=page_number,
                entity_id=entity_id,
            ),
        )

    counts = Counter(_inv_key(item) for item in candidates)
    winners = _top_voted_keys(counts)
    if len(winners) != 1:
        return (
            "",
            candidates,
            create_issue(
                IssueCode.INV_NAME_AMBIGUOUS,
                page_number=page_number,
                entity_id=entity_id,
                candidates=', '.join(candidates),
            ),
        )

    inferred = next(item for item in candidates if _inv_key(item) == winners[0])
    matched = best_agent2_inv_match(inferred, expected_inv_names)
    if matched is not None:
        # Keep Agent3's own recognition for display.  The Agent2 spelling is
        # retained separately by the background match on the result.
        return inferred, candidates, None

    expected_text = ", ".join(expected_inv_names) or "<empty>"
    return (
        inferred,
        candidates,
        create_issue(
            IssueCode.INV_NAME_NOT_IN_AGENT2,
            page_number=page_number,
            entity_id=entity_id,
            inferred=inferred,
            expected_text=expected_text,
        ),
    )


def _equipment_family(table: BlockDiagramTableResult | None, names: tuple[str, ...]) -> str:
    if table is not None:
        if table.equipment_family in {"CBX", "LBD"}:
            return table.equipment_family
        value = table.project_type.upper()
        if value in {"CBX", "IBEX"}:
            return "CBX"
        if value in {"LBD", "LYNX"}:
            return "LBD"
    joined = " ".join(names).upper()
    if "CBX" in joined:
        return "CBX"
    if "LBD" in joined or "LYNX" in joined:
        return "LBD"
    return ""


def _detection_order(item) -> tuple[float, float]:
    x, y = center(item.box)
    return (y, x)


def _intersection_over_union(left: BoundingBox, right: BoundingBox) -> float:
    """Return the conventional intersection-over-union for two xyxy boxes."""

    intersection_width = max(0.0, min(left.x2, right.x2) - max(left.x1, right.x1))
    intersection_height = max(0.0, min(left.y2, right.y2) - max(left.y1, right.y1))
    intersection = intersection_width * intersection_height
    left_area = max(0.0, left.x2 - left.x1) * max(0.0, left.y2 - left.y1)
    right_area = max(0.0, right.x2 - right.x1) * max(0.0, right.y2 - right.y1)
    union = left_area + right_area - intersection
    return intersection / union if union > 0.0 else 0.0


def _overlapping_pairs(
    detections: Sequence[object],
    threshold: float = OVERLAP_ISSUE_IOU_THRESHOLD,
) -> tuple[tuple[int, int, float], ...]:
    """Return index pairs whose boxes overlap strictly above ``threshold``."""

    pairs: list[tuple[int, int, float]] = []
    for left_index in range(len(detections)):
        left_box = getattr(detections[left_index], "box")
        for right_index in range(left_index + 1, len(detections)):
            right_box = getattr(detections[right_index], "box")
            iou = _intersection_over_union(left_box, right_box)
            if iou > threshold:
                pairs.append((left_index, right_index, iou))
    return tuple(pairs)


def _overlap_peers(
    detections: Sequence[object],
) -> dict[int, list[tuple[int, float]]]:
    """Group overlapping pairs by detection so every affected box is marked."""

    peers: dict[int, list[tuple[int, float]]] = {}
    for left_index, right_index, iou in _overlapping_pairs(detections):
        peers.setdefault(left_index, []).append((right_index, iou))
        peers.setdefault(right_index, []).append((left_index, iou))
    return peers


def _element_detection(entity_id, kind, page_number, item, parent_id="") -> ElementDetection:
    return ElementDetection(
        entity_id=entity_id,
        kind=kind,
        confidence=item.confidence,
        bbox=item.box,
        page_number=page_number,
        parent_id=parent_id,
        label=item.label,
        source=item.source,
    )


def _page_issue(page, code: IssueCode, message: str) -> RecognitionIssue:
    return create_issue(
        code,
        detail=message,
        page_number=page.page_number,
        entity_id=f'p{page.page_number}:inv',
    )


def _box_distance(left: BoundingBox, right: BoundingBox) -> float:
    lx, ly = center(left)
    rx, ry = center(right)
    return (lx - rx) ** 2 + (ly - ry) ** 2


def _table_bbox_source(
    table: BlockDiagramTableResult | None,
    location: TableLocation | None = None,
) -> str:
    """Return the actual table locator used, including fallback locations."""

    if table is not None and table.table_bbox is not None:
        source = str(table.raw_output.get("table_bbox_source", "")).casefold()
        strategy = table.strategy
    elif location is not None:
        source = location.source.casefold()
        strategy = ""
    else:
        return ""
    if source in {"yolo", "mineru"}:
        return source
    if strategy.casefold().startswith("yolo"):
        return "yolo"
    if strategy.casefold().startswith("mineru"):
        return "mineru"
    return ""


def _union_box(boxes: list[BoundingBox]) -> BoundingBox | None:
    if not boxes:
        return None
    return BoundingBox(
        min(box.x1 for box in boxes),
        min(box.y1 for box in boxes),
        max(box.x2 for box in boxes),
        max(box.y2 for box in boxes),
    )


def replace_reviewed_tree(
    result: ElementRecognitionResult,
    tree: ReviewedTree,
) -> ElementRecognitionResult:
    """Return a result containing a user-reviewed tree without raw-data loss."""

    return refresh_review_issues(replace(result, reviewed_tree=tree), result)


def resolve_table_string_count(
    name: str,
    table_rows: Sequence[NodeStringTableRow],
) -> tuple[int, bool]:
    """Return the reviewed table's string count for one Node name.

    Renaming a Node in the structure tree must re-resolve its OCR reference
    from the name it actually carries; otherwise the tree keeps a count that
    belongs to another name. Unlike the recognition-time table matching,
    which tolerates OCR spelling variance, this lookup compares normalized
    names exactly: a typed name either is a reviewed table row or has no
    reference at all.
    """

    key = _normalize_name(name)
    if not key:
        return 0, False
    rows = [
        row for row in table_rows if _normalize_name(row.node) == key
    ]
    if len(rows) != 1:
        return 0, False
    return rows[0].string_number, True


def replace_table_rows_and_recalculate(
    result: ElementRecognitionResult,
    request: ElementRecognitionRequest,
    page_number: int,
    rows: Sequence[NodeStringTableRow],
    *,
    target_node_id: str | None = None,
) -> ElementRecognitionResult:
    """Apply reviewed table rows and rebuild derived Node geometry totals.

    Only Nodes that match a reviewed row are rebuilt. A Node without a row
    keeps its recognized OCR count and Row structure, and a reviewed count the
    detected Trackers cannot balance keeps the previously derived structure:
    applying table edits must never erase a Node down to an empty 0, or Agent4
    would report a lost value instead of the calculated-vs-OCR difference.

    A user-created Node (manual Add or paste) has no page anchor, so it is
    matched by name against the table being applied; that reviewed count is
    its only OCR reference and must reach Agent4's Node comparison.

    When ``target_node_id`` is supplied, only that recognized Node is rebuilt.
    A targeted rebuild also leaves the Node untouched when its edited name does
    not match a table row; this lets the properties panel fall back to its
    existing Row-based recalculation without erasing manual work.
    """

    page_result = next(
        (page for page in result.pages if page.page_number == page_number),
        None,
    )
    if page_result is None:
        raise ValueError(f"Unknown Agent3 page: {page_number}")
    reviewed_rows = tuple(rows)
    if any(not row.node.strip() or row.string_number < 0 for row in reviewed_rows):
        raise ValueError("Table rows require a Node name and non-negative strings.")

    table_by_name = {
        _normalize_name(row.node): row
        for row in reviewed_rows
        if _normalize_name(row.node)
    }
    identifiers, definition_identifiers = _length_identifiers(
        request.tracker_definitions
    )
    detections_by_id = {
        detection.entity_id: detection
        for detection in page_result.detections
    }
    used: set[str] = set()
    rebuilt_issues: dict[str, list[RecognitionIssue]] = {}

    def rebuild(node: NodeDraft) -> NodeDraft:
        if target_node_id is not None and node.recognition_id != target_node_id:
            return node
        # A user-created Node (manual Add or paste) carries no page anchor.
        # The reviewed table being applied is the page context the user is
        # editing, so an unanchored Node is resolved by name against it
        # instead of being skipped as a different page's Node.
        if node.source.page_number not in (None, page_number):
            return node
        detected = detections_by_id.get(node.recognition_id)
        if detected is None:
            # A manually added or pasted Node has no detection to rebuild its
            # geometry from, but the reviewed table still owns its OCR
            # reference. Update that reference so Agent4 validates the typed
            # structure instead of reporting the Node as lost data.
            matched = _match_table_name(node.name, table_by_name, used)
            if not matched:
                return node
            used.add(_normalize_name(matched))
            row = table_by_name.get(_normalize_name(matched))
            if row is None:
                return node
            return replace(
                node,
                name=matched,
                ocr_string_count=row.string_number,
                has_ocr_string_count=True,
            )
        matched = _match_table_name(node.name, table_by_name, used)
        if target_node_id is not None and not matched:
            return node
        canonical_name = matched or node.name
        if matched:
            used.add(_normalize_name(matched))
        row = table_by_name.get(_normalize_name(canonical_name))
        if row is None:
            # The reviewed table has no row for this Node. Rebuilding it with
            # an empty count would erase its OCR provenance and its derived
            # Rows; the table only overrides the Nodes it actually lists.
            return node
        string_count = row.string_number
        has_ocr_string_count = True
        tracker_detections = [
            item
            for item in page_result.detections
            if item.kind == "Tracker" and item.parent_id == node.recognition_id
        ]
        box_detections = [
            item
            for item in page_result.detections
            if item.kind == "Box" and item.parent_id == node.recognition_id
        ]
        raw_text = ""
        if node.annotations:
            raw = node.annotations[0].source.raw
            if isinstance(raw, Mapping):
                raw_text = str(raw.get("ocr_text", ""))
        orientation = PanelOrientation.from_value(
            node.panel_orientation or request.default_panel_orientation.value
        )
        rebuilt, node_issues = _build_node(
            next(page for page in request.pages if page.page_number == page_number),
            node.recognition_id,
            canonical_name,
            raw_text,
            DetectionResult(
                label=detected.label or "Node",
                confidence=detected.confidence,
                box=detected.bbox,
                source=detected.source,
            ),
            tracker_detections,
            box_detections,
            string_count,
            request.tracker_definitions,
            definition_identifiers,
            orientation,
            node.offset_snap_step_strings,
            has_ocr_string_count=has_ocr_string_count,
        )
        rebuilt_issues[node.recognition_id] = node_issues
        if not rebuilt.tracker_counts and node.tracker_counts:
            # The reviewed string count cannot be balanced by the detected
            # Trackers. Keep the previous Rows so the manual edit replaces
            # only the OCR reference and Agent4 compares that reference
            # against the recognized structure instead of showing 0.
            rebuilt = replace(
                rebuilt,
                rows=node.rows,
                tracker_counts=node.tracker_counts,
            )
        return replace(
            rebuilt,
            state=node.state,
            inherits_panel_orientation=node.inherits_panel_orientation,
            inherits_offset_snap_step=node.inherits_offset_snap_step,
        )

    tree = replace(
        result.reviewed_tree,
        length_identifiers=identifiers,
        invs=tuple(
            replace(inv, nodes=tuple(rebuild(node) for node in inv.nodes))
            for inv in result.reviewed_tree.invs
        ),
    )
    pages = tuple(
        replace(page, table_rows=reviewed_rows)
        if page.page_number == page_number
        else page
        for page in result.pages
    )
    updated = replace(result, reviewed_tree=tree, pages=pages)
    geometry_codes = {
        IssueCode.TRACKER_STRING_BALANCE_FAILED.value,
        IssueCode.NODE_HAS_NO_TRACKERS.value,
        IssueCode.NODE_HAS_NO_BOX.value,
        IssueCode.NODE_HAS_MULTIPLE_BOXES.value,
        IssueCode.SINGLE_ROW_BOX_POSITION_AMBIGUOUS.value,
    }
    issues = [
        issue for issue in updated.all_issues
        if not (issue.entity_id in rebuilt_issues and issue.code in geometry_codes)
    ]
    issues.extend(issue for group in rebuilt_issues.values() for issue in group)
    updated = with_issues(updated, issues)
    updated = refresh_review_issues(
        updated, result,
        tracker_definitions=request.tracker_definitions,
        expected_inv_names=request.expected_inv_names,
    )
    return annotate_tracker_length_outliers(updated, request.tracker_definitions)

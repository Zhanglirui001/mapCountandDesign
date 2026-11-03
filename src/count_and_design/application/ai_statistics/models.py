"""Typed contracts for the AI statistics workflow.

These objects describe the boundary between AI providers, application
workflow, and the PySide views.  They intentionally live outside the GUI so
the Agent1-4 implementations can be tested without Qt widgets.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
import logging
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any

from .info_recognition import OverallSiteExtraction

from ..ai.types import BoundingBox


logger = logging.getLogger(__name__)


class AIStatisticsStage(Enum):
    """Ordered stages in the PRD workflow."""

    PAGE_CLASSIFICATION = 0
    INFO_RECOGNITION = 1
    ELEMENT_RECOGNITION = 2
    VALIDATION = 3

    @property
    def index(self) -> int:
        return int(self.value)

    @classmethod
    def from_index(cls, index: int) -> "AIStatisticsStage":
        for stage in cls:
            if stage.index == index:
                return stage
        raise ValueError(f"Invalid AI statistics stage index: {index}")

    def next(self) -> "AIStatisticsStage | None":
        try:
            return self.from_index(self.index + 1)
        except ValueError:
            return None


class AIStatisticsStageStatus(Enum):
    """Status vocabulary shown by the workflow bar."""

    WAITING = "waiting"
    RUNNING = "running"
    RESULT_READY = "result_ready"
    NEEDS_CONFIRM = "needs_confirm"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class RecognitionState(Enum):
    """Review state attached to AI generated business objects."""

    AI_DRAFT = "ai_draft"
    NEEDS_REVIEW = "needs_review"
    USER_CONFIRMED = "user_confirmed"


class DrawingPageType(Enum):
    """Stable page categories produced and reviewed during Agent1."""

    ARRAY_LAYOUT = "Array Layout"
    OVERALL_LAYOUT = "Overall Layout"
    OTHER = "Other"


@dataclass(frozen=True)
class SourceReference:
    """Traceability metadata required for every AI-derived value."""

    page_number: int | None = None
    bbox: BoundingBox | None = None
    confidence: float | None = None
    model_version: str = ""
    recognition_version: str = ""
    source: str = ""
    raw: dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class PageClassification:
    """Agent1 output for one PDF page."""

    page_number: int
    page_type: str
    purpose: str = ""
    source: SourceReference = field(default_factory=SourceReference)
    state: RecognitionState = RecognitionState.AI_DRAFT


class LazyPageImages(Mapping[int, bytes]):
    """Render high-resolution PDF pages only when a consumer requests them."""

    def __init__(
        self,
        pdf_path: str | Path,
        page_count: int,
        *,
        width: int,
        height: int,
    ) -> None:
        self._pdf_path = Path(pdf_path)
        self._page_numbers = tuple(range(1, page_count + 1))
        self._width = width
        self._height = height
        self._cache: dict[int, bytes] = {}
        self._lock = Lock()

    def __len__(self) -> int:
        return len(self._page_numbers)

    def __iter__(self) -> Iterator[int]:
        return iter(self._page_numbers)

    def __contains__(self, page_number: object) -> bool:
        # Mapping's default membership test calls __getitem__, which would
        # silently trigger an expensive high-resolution render for every
        # checked page. Membership must stay a key-only lookup.
        try:
            return int(page_number) in self._page_numbers  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    def get(self, page_number: object, default=None):
        """Return cached-or-rendered bytes without Mapping's KeyError dance.

        Rendering happens only when callers explicitly fetch a page, never
        for availability probes.
        """

        try:
            key = int(page_number)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
        if key not in self._page_numbers:
            return default
        return self[key]

    def __getitem__(self, page_number: int) -> bytes:
        if page_number not in self._page_numbers:
            raise KeyError(page_number)
        with self._lock:
            cached = self._cache.get(page_number)
            if cached is not None:
                return cached
        from .pdf_rendering import process_memory_mb, render_high_resolution_page

        started = perf_counter()
        image = render_high_resolution_page(
            self._pdf_path,
            page_number - 1,
            width=self._width,
            height=self._height,
        )
        with self._lock:
            cached = self._cache.get(page_number)
            if cached is not None:
                return cached
            self._cache[page_number] = image
        working_mb, peak_mb = process_memory_mb()
        logger.info(
            "agent1.highres page=%d seconds=%.3f bytes=%d "
            "working_set_mb=%.1f peak_working_set_mb=%.1f",
            page_number,
            perf_counter() - started,
            len(image),
            working_mb,
            peak_mb,
        )
        return image

    @property
    def render_size(self) -> tuple[int, int]:
        return self._width, self._height

    @property
    def cached_page_count(self) -> int:
        return len(self._cache)

    def cached_items(self, page_numbers: set[int]) -> dict[int, bytes]:
        """Return already-rendered pages without triggering new renders."""

        with self._lock:
            return {
                page_number: image
                for page_number, image in self._cache.items()
                if page_number in page_numbers
            }

    def cache_page(self, page_number: int, image: bytes) -> None:
        """Store an externally rendered page for later GUI/agent reuse."""

        if page_number not in self._page_numbers:
            raise KeyError(page_number)
        if not image:
            raise ValueError("Cached page image cannot be empty.")
        with self._lock:
            self._cache[page_number] = image


@dataclass(frozen=True)
class PageClassificationResult:
    """Agent1 page split and page-type result."""

    pages: tuple[PageClassification, ...] = ()
    # Page number -> PNG bytes, downscaled from the bitmap Agent1 already
    # rendered so the review grid never has to rasterize a page again.
    thumbnails: dict[int, bytes] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    # Page number -> aspect-ratio-preserving high-resolution PNG bytes without
    # artificial canvas padding. Agent2 and Agent3 select from this in-memory
    # mapping after Agent1 classification/review.
    page_images: Mapping[int, bytes] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    @property
    def total_pages(self) -> int:
        return len(self.pages)

    def images_for_type(self, page_type: str) -> dict[int, bytes]:
        """Return high-resolution PNGs for the reviewed page type."""

        page_numbers = self.page_numbers_for_type(page_type)
        return {
            page_number: self.page_images[page_number]
            for page_number in sorted(page_numbers)
            if page_number in self.page_images
        }

    def page_numbers_for_type(self, page_type: str) -> set[int]:
        """Return matching page numbers without loading page image bytes."""

        return {
            page.page_number
            for page in self.pages
            if page.page_type == page_type
        }

    def has_page_type(self, page_type: str) -> bool:
        """Check classification metadata without triggering lazy rendering."""

        return any(page.page_type == page_type for page in self.pages)

    def available_page_numbers_for_type(self, page_type: str) -> set[int]:
        """Return matching page keys available from the image mapping."""

        return {
            page_number
            for page_number in self.page_numbers_for_type(page_type)
            if page_number in self.page_images
        }

    def has_images_for_type(self, page_type: str) -> bool:
        """Check image availability without reading lazy image values."""

        return bool(self.available_page_numbers_for_type(page_type))

    def cached_images_for_type(self, page_type: str) -> dict[int, bytes]:
        """Return only high-resolution images already present in memory."""

        page_numbers = self.page_numbers_for_type(page_type)
        if isinstance(self.page_images, LazyPageImages):
            return self.page_images.cached_items(page_numbers)
        return {
            page_number: image
            for page_number, image in self.page_images.items()
            if page_number in page_numbers
        }

    def cached_page_images(self) -> dict[int, bytes]:
        """Return every already-rendered page without triggering new work."""

        if isinstance(self.page_images, LazyPageImages):
            return self.page_images.cached_items(set(self.page_images))
        return dict(self.page_images)

    def thumbnail_images_for_type(self, page_type: str) -> dict[int, bytes]:
        """Return Agent1 thumbnails for a page type without high-res work."""

        return {
            page_number: self.thumbnails[page_number]
            for page_number in sorted(self.page_numbers_for_type(page_type))
            if page_number in self.thumbnails
        }


@dataclass(frozen=True)
class OcrField:
    """Field-level OCR value with source metadata."""

    name: str
    value: str
    source: SourceReference = field(default_factory=SourceReference)
    state: RecognitionState = RecognitionState.AI_DRAFT


@dataclass(frozen=True)
class TrackerOcrTotal:
    """Agent2 tracker total baseline row."""

    tracker_type: str
    total: int
    fields: tuple[OcrField, ...] = ()


@dataclass(frozen=True)
class TrackerQuantity:
    """Recognized tracker quantity for one tracker basis."""

    tracker_type: str
    tracker_count: int


@dataclass(frozen=True)
class DetectionAnnotation:
    """Canvas-ready detection attached to a business meaning."""

    label: str
    source: SourceReference
    state: RecognitionState = RecognitionState.AI_DRAFT
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class RackingEntryDraft:
    """Agent3 draft racking entry under a Row."""

    rackings: str = ""
    length_str: str = "0mm"
    string_pattern: str = ""
    north_side: bool | None = None
    east_side: bool | None = None
    offset_ratio_strings: float | None = None
    length_identifier: str = ""


@dataclass(frozen=True)
class RowDraft:
    """Agent3 draft row under a Node."""

    name: str
    rackings: tuple[str, ...] = ()
    racking_entries: tuple[RackingEntryDraft, ...] = ()
    direction: str = ""
    source: SourceReference = field(default_factory=SourceReference)
    state: RecognitionState = RecognitionState.AI_DRAFT
    issues: tuple[str, ...] = ()
    recognition_id: str = ""


@dataclass(frozen=True)
class NodeDraft:
    """Agent3 draft CBX/LBD node."""

    name: str
    box_position: float = 0.0
    rows: tuple[RowDraft, ...] = ()
    annotations: tuple[DetectionAnnotation, ...] = ()
    source: SourceReference = field(default_factory=SourceReference)
    state: RecognitionState = RecognitionState.AI_DRAFT
    issues: tuple[str, ...] = ()
    # Table-recognition provenance consumed by Agent4 as the OCR reference.
    # Never store the row-derived total here: Agent4 compares it against
    # ``tracker_counts``, and cloning the derived value makes every check pass.
    ocr_string_count: int = 0
    has_ocr_string_count: bool = False
    tracker_counts: tuple[TrackerQuantity, ...] = ()
    recognition_id: str = ""
    panel_orientation: str = ""
    offset_snap_step_strings: float = 1.0
    inherits_panel_orientation: bool = True
    inherits_offset_snap_step: bool = True


@dataclass(frozen=True)
class InvDraft:
    """Agent2/Agent3 draft INV branch."""

    name: str
    page: int | None = None
    nodes: tuple[NodeDraft, ...] = ()
    source: SourceReference = field(default_factory=SourceReference)
    state: RecognitionState = RecognitionState.AI_DRAFT
    issues: tuple[str, ...] = ()
    recognition_id: str = ""
    panel_orientation: str = ""
    offset_snap_step_strings: float = 1.0
    inherits_panel_orientation: bool = True
    inherits_offset_snap_step: bool = True


@dataclass(frozen=True)
class ReviewedTree:
    """Human-reviewed structure ready to import into Project."""

    project_name: str = ""
    invs: tuple[InvDraft, ...] = ()
    project_type: str = ""
    length_identifiers: tuple[OcrField, ...] = ()


@dataclass(frozen=True)
class ValidationDifference:
    """One Agent4 comparison row."""

    page_number: int | None
    inv_name: str
    node_name: str
    calculated_total: int
    ocr_total: int
    source: SourceReference = field(default_factory=SourceReference)

    @property
    def diff(self) -> int:
        return self.calculated_total - self.ocr_total


@dataclass(frozen=True)
class ValidationNodeComparison:
    """Agent4 calculated-vs-OCR comparison for one node."""

    inv_name: str
    node_name: str
    tracker_counts: tuple[TrackerQuantity, ...] = ()
    calculated_total: int = 0
    ocr_total: int = 0
    ocr_total_present: bool = True
    source: SourceReference = field(default_factory=SourceReference)
    # Agent3 entity link kept through validation so Agent4 can jump back to
    # the exact Node even when names are duplicated or were edited.
    recognition_id: str = ""

    @property
    def diff(self) -> int:
        return self.ocr_total - self.calculated_total

    @property
    def passed(self) -> bool:
        return self.ocr_total_present and self.diff == 0


@dataclass(frozen=True)
class ValidationInvComparison:
    """Agent4 calculated-vs-OCR comparison for one INV."""

    inv_name: str
    page: int | None = None
    node_count: int = 0
    tracker_counts: tuple[TrackerQuantity, ...] = ()
    calculated_total: int = 0
    ocr_total: int = 0
    nodes: tuple[ValidationNodeComparison, ...] = ()
    source: SourceReference = field(default_factory=SourceReference)
    recognition_id: str = ""

    @property
    def diff(self) -> int:
        return self.ocr_total - self.calculated_total

    @property
    def passed(self) -> bool:
        return self.diff == 0 and all(node.passed for node in self.nodes)


@dataclass(frozen=True)
class ValidationTrackerComparison:
    """Agent4 total validation for one tracker basis."""

    tracker_type: str
    structure_total: int
    ocr_total: int
    source: SourceReference = field(default_factory=SourceReference)

    @property
    def diff(self) -> int:
        return self.structure_total - self.ocr_total

    @property
    def passed(self) -> bool:
        return self.diff == 0


@dataclass(frozen=True)
class ValidationReport:
    """Agent4 validation result."""

    differences: tuple[ValidationDifference, ...] = ()
    source: SourceReference = field(default_factory=SourceReference)
    tracker_totals: tuple[TrackerOcrTotal, ...] = ()
    reviewed_tree: ReviewedTree | None = None
    inv_comparisons: tuple[ValidationInvComparison, ...] = ()
    tracker_comparisons: tuple[ValidationTrackerComparison, ...] = ()
    calculated_grand_total: int = 0
    ocr_grand_total: int = 0

    @property
    def passed(self) -> bool:
        return (
            all(item.diff == 0 for item in self.differences)
            and all(item.passed for item in self.inv_comparisons)
            and all(item.passed for item in self.tracker_comparisons)
        )


@dataclass(frozen=True)
class AIStatisticsProgressStep:
    """Aggregated progress for one named long-running pipeline step."""

    key: str
    completed: int = 0
    total: int = 0
    failed: int = 0
    active: int = 0


@dataclass(frozen=True)
class AIStatisticsActiveTask:
    """One Agent3 worker's currently running step and page."""

    worker_name: str
    step: str
    page_number: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class AIStatisticsSnapshot:
    """Serializable workflow state owned by the application layer."""

    pdf_path: Path | None = None
    page_count: int = 0
    selected_stage: AIStatisticsStage = AIStatisticsStage.PAGE_CLASSIFICATION
    active_stage: AIStatisticsStage | None = None
    furthest_stage: AIStatisticsStage | None = None
    stage_statuses: tuple[
        AIStatisticsStageStatus,
        AIStatisticsStageStatus,
        AIStatisticsStageStatus,
        AIStatisticsStageStatus,
    ] = (
        AIStatisticsStageStatus.WAITING,
        AIStatisticsStageStatus.WAITING,
        AIStatisticsStageStatus.WAITING,
        AIStatisticsStageStatus.WAITING,
    )
    page_classification: PageClassificationResult | None = None
    tracker_totals: tuple[TrackerOcrTotal, ...] = ()
    overall_site_extractions: tuple[OverallSiteExtraction, ...] = ()
    overall_site_orientation: str | None = None
    reviewed_tree: ReviewedTree | None = None
    validation_report: ValidationReport | None = None
    element_recognition_result: Any | None = None
    classification_progress_completed: int = 0
    classification_progress_total: int = 0
    element_progress_completed: int = 0
    element_progress_total: int = 0
    element_progress_steps: tuple[AIStatisticsProgressStep, ...] = ()
    element_progress_current_step: str = ""
    element_progress_page_number: int | None = None
    element_progress_detail: str = ""
    element_progress_active_tasks: tuple[AIStatisticsActiveTask, ...] = ()
    element_failure_context: dict[str, Any] = field(default_factory=dict)
    information_progress_completed: int = 0
    information_progress_total: int = 0
    error_message: str = ""

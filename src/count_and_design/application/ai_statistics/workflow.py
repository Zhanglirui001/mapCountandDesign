"""Stateful application workflow for AI Statistics."""

from __future__ import annotations

import logging
import multiprocessing
import threading
from collections.abc import Iterator, Mapping, MutableMapping
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from io import BytesIO
from pathlib import Path

from PIL import Image

from .models import (
    AIStatisticsActiveTask,
    AIStatisticsProgressStep,
    AIStatisticsSnapshot,
    AIStatisticsStage,
    AIStatisticsStageStatus,
    DrawingPageType,
    PageClassificationResult,
    LazyPageImages,
    RecognitionState,
    ReviewedTree,
    TrackerOcrTotal,
)
from ..config import DrawingClassificationConfig, OverallSiteExtractionConfig
from .pdf_rendering import (
    PAGE_RENDER_HEIGHT,
    PAGE_RENDER_WIDTH,
    initialize_pdf_render_worker,
    render_high_resolution_page,
    render_high_resolution_page_worker,
)
from .element_recognition import (
    AGENT3_PROGRESS_STEP_ORDER,
    Agent3Cancelled,
    Agent3ElementRecognizer,
    Agent3ProgressUpdate,
    Agent2ElementContract,
    ArrayLayoutPageInput,
    ElementRecognitionRequest,
    ElementRecognitionResult,
    InvExpectedStringCount,
    InvRecognitionSettings,
    PanelOrientation,
    TrackerDefinition,
    inv_names_from_overall_site,
    inv_string_counts_from_overall_site,
    match_inv_names,
)
from .element_recognition.process_runner import Agent3ProcessRunner
from .element_recognition.service import (
    _resolve_grayscale,
    encode_agent3_page_png,
)
from .info_recognition import (
    Agent2Cancelled,
    Agent2OverallSiteRecognizer,
    OverallSiteExtraction,
    build_agent4_validation_input,
)
from .info_recognition.overall_site import (
    STRATEGY_MINERU_CLAUDE,
    STRATEGY_MINERU_GLM,
)
from .page_classification import (
    Agent1Cancelled,
    Agent1NoRelevantDrawingPagesError,
    Agent1ProgressCallback,
    EmbeddingPageClassificationProvider,
    PageClassificationProvider,
)
from .validation import (
    Agent4ValidationService,
    element_review_to_validation_payload,
    reviewed_tree_to_validation_payload,
)


_OVERALL_SITE_PANEL_ORIENTATIONS = {
    "P-B": PanelOrientation.TOWARD_BOX,
    "N-B": PanelOrientation.AWAY_FROM_BOX,
    "P-N": PanelOrientation.POSITIVE_NORTH,
    "P-S": PanelOrientation.POSITIVE_SOUTH,
    "P-E": PanelOrientation.POSITIVE_EAST,
    "P-W": PanelOrientation.POSITIVE_WEST,
}

logger = logging.getLogger(__name__)
AGENT3_RENDER_MAX_SIDE = 6000
_RERUNNABLE_AGENT1_STATUSES = {
    AIStatisticsStageStatus.WAITING,
    AIStatisticsStageStatus.FAILED,
    AIStatisticsStageStatus.RESULT_READY,
    AIStatisticsStageStatus.NEEDS_CONFIRM,
    AIStatisticsStageStatus.CONFIRMED,
}
_RERUNNABLE_AGENT2_STATUSES = {
    AIStatisticsStageStatus.WAITING,
    AIStatisticsStageStatus.FAILED,
    AIStatisticsStageStatus.RESULT_READY,
    AIStatisticsStageStatus.NEEDS_CONFIRM,
    AIStatisticsStageStatus.CONFIRMED,
}


class AIStatisticsWorkflowError(RuntimeError):
    """Raised when the AI Statistics workflow receives an invalid command."""


class AIStatisticsWorkflow:
    """Own the Agent1-4 workflow state outside the PySide view layer."""

    def __init__(
        self,
        page_classification_provider: PageClassificationProvider | None = None,
        overall_site_recognizer: Agent2OverallSiteRecognizer | None = None,
        validation_service: Agent4ValidationService | None = None,
        element_recognizer: Agent3ElementRecognizer | None = None,
        *,
        auto_warm_element_runtime: bool = False,
    ) -> None:
        self._snapshot = AIStatisticsSnapshot()
        self._page_classification_provider = (
            page_classification_provider
            or EmbeddingPageClassificationProvider()
        )
        self._overall_site_recognizer = overall_site_recognizer
        self._validation_service = validation_service or Agent4ValidationService()
        self._element_recognizer = element_recognizer
        # Production Agent3 is isolated because its blocking HTTP and ONNX
        # calls cannot be interrupted safely inside the GUI process. Injected
        # recognizers remain in-process for tests and integrations.
        self._element_process_runner = (
            Agent3ProcessRunner() if element_recognizer is None else None
        )
        # Opt-in warmup applies only to explicitly injected in-process
        # recognizers. Production model initialization stays in the killable
        # process above.
        self._auto_warm_element_runtime = auto_warm_element_runtime
        self._element_warmup_started = False
        self._agent2_inv_names: tuple[str, ...] = ()
        self._agent2_inv_string_counts: tuple[InvExpectedStringCount, ...] = ()
        self._tracker_definitions: tuple[TrackerDefinition, ...] = ()
        self._default_panel_orientation = PanelOrientation.UNKNOWN
        self._default_offset_snap_step_strings = 1.0
        self._inv_recognition_settings: tuple[InvRecognitionSettings, ...] = ()

    @property
    def snapshot(self) -> AIStatisticsSnapshot:
        return self._snapshot

    @property
    def page_classification_provider(self) -> PageClassificationProvider:
        return self._page_classification_provider

    def clear_pdf(self) -> AIStatisticsSnapshot:
        self._snapshot = AIStatisticsSnapshot()
        self._agent2_inv_names = ()
        self._agent2_inv_string_counts = ()
        self._tracker_definitions = ()
        self._default_panel_orientation = PanelOrientation.UNKNOWN
        self._default_offset_snap_step_strings = 1.0
        self._inv_recognition_settings = ()
        return self._snapshot

    @property
    def element_recognition_request(self) -> ElementRecognitionRequest | None:
        try:
            return self._build_element_request()
        except AIStatisticsWorkflowError:
            return None

    @property
    def element_recognition_request_metadata(self) -> ElementRecognitionRequest | None:
        """Request metadata without touching any high-resolution page bytes.

        Safe to evaluate on the GUI thread for previews and enablement checks;
        materializing the actual PNGs happens in the recognition worker.
        """

        try:
            return self._build_element_request(materialize_images=False)
        except AIStatisticsWorkflowError:
            return None

    @property
    def array_layout_page_numbers(self) -> tuple[int, ...]:
        result = self._snapshot.page_classification
        if result is None:
            return ()
        return tuple(
            page.page_number
            for page in result.pages
            if page.page_type == DrawingPageType.ARRAY_LAYOUT.value
        )

    @property
    def agent2_inv_names(self) -> tuple[str, ...]:
        return self._agent2_inv_names

    @property
    def agent2_tracker_definitions(self) -> tuple[TrackerDefinition, ...]:
        return self._tracker_definitions

    def configure_element_recognition(
        self,
        tracker_definitions: tuple[TrackerDefinition, ...],
        default_panel_orientation: PanelOrientation = PanelOrientation.UNKNOWN,
        inv_settings: tuple[InvRecognitionSettings, ...] = (),
        default_offset_snap_step_strings: float = 1.0,
        expected_inv_names: tuple[str, ...] = (),
        expected_inv_string_counts: tuple[InvExpectedStringCount, ...] = (),
    ) -> AIStatisticsSnapshot:
        """Store the Agent2-to-Agent3 contract without importing Agent2 types."""

        if default_offset_snap_step_strings <= 0:
            raise AIStatisticsWorkflowError(
                "Project offset snap step must be positive."
            )
        self._agent2_inv_names = tuple(expected_inv_names)
        self._agent2_inv_string_counts = tuple(expected_inv_string_counts)
        self._tracker_definitions = tuple(tracker_definitions)
        self._default_panel_orientation = default_panel_orientation
        self._default_offset_snap_step_strings = (
            default_offset_snap_step_strings
        )
        self._inv_recognition_settings = tuple(inv_settings)
        statuses = _set_status(
            self._snapshot.stage_statuses,
            AIStatisticsStage.ELEMENT_RECOGNITION,
            AIStatisticsStageStatus.WAITING,
        )
        statuses = _set_status(
            statuses,
            AIStatisticsStage.VALIDATION,
            AIStatisticsStageStatus.WAITING,
        )
        self._snapshot = _replace_snapshot(
            self._snapshot,
            tracker_totals=tuple(
                TrackerOcrTotal(
                    tracker_type=item.code,
                    total=item.total_count,
                )
                for item in tracker_definitions
            ),
            element_recognition_result=None,
            reviewed_tree=None,
            validation_report=None,
            stage_statuses=statuses,
        )
        return self._snapshot

    def configure_agent2_contract(
        self,
        contract: Agent2ElementContract,
    ) -> AIStatisticsSnapshot:
        """Configure Agent3 through the isolated Agent2 adapter."""

        return self.configure_element_recognition(
            contract.tracker_definitions,
            contract.default_panel_orientation,
            contract.inv_settings,
            contract.default_offset_snap_step_strings,
            contract.inv_names,
            contract.inv_string_counts,
        )

    def update_project_recognition_settings(
        self,
        panel_orientation: PanelOrientation,
        offset_snap_step_strings: float,
    ) -> AIStatisticsSnapshot:
        if offset_snap_step_strings <= 0:
            raise AIStatisticsWorkflowError(
                "Project offset snap step must be positive."
            )
        self._default_panel_orientation = panel_orientation
        self._default_offset_snap_step_strings = offset_snap_step_strings
        return self._snapshot

    def update_inv_recognition_settings(
        self,
        inv_name: str,
        *,
        panel_orientation: PanelOrientation | None = None,
        offset_snap_step_strings: float = 1.0,
    ) -> AIStatisticsSnapshot:
        if offset_snap_step_strings <= 0:
            raise AIStatisticsWorkflowError("Offset snap step must be positive.")
        updated = [item for item in self._inv_recognition_settings if item.inv_name != inv_name]
        updated.append(
            InvRecognitionSettings(
                inv_name=inv_name,
                panel_orientation=panel_orientation,
                offset_snap_step_strings=offset_snap_step_strings,
            )
        )
        self._inv_recognition_settings = tuple(updated)
        return self._snapshot

    def load_pdf(self, path: str | Path, page_count: int) -> AIStatisticsSnapshot:
        if page_count <= 0:
            raise AIStatisticsWorkflowError("PDF page count must be positive.")
        self._snapshot = AIStatisticsSnapshot(
            pdf_path=Path(path),
            page_count=page_count,
        )
        return self._snapshot

    def set_current_stage(self, index: int) -> AIStatisticsSnapshot:
        stage = AIStatisticsStage.from_index(index)
        if not self.is_stage_clickable(stage):
            raise AIStatisticsWorkflowError(
                f"Stage is not available yet: {stage.name}"
            )
        self._snapshot = _replace_snapshot(
            self._snapshot,
            selected_stage=stage,
            active_stage=stage,
        )
        return self._snapshot

    def begin_page_classification(self) -> AIStatisticsSnapshot:
        snapshot = self._snapshot
        if snapshot.pdf_path is None:
            raise AIStatisticsWorkflowError("No PDF is loaded.")
        if snapshot.selected_stage != AIStatisticsStage.PAGE_CLASSIFICATION:
            raise AIStatisticsWorkflowError(
                "AI model can only be started from Page Classification."
            )
        if self.status_for(AIStatisticsStage.PAGE_CLASSIFICATION) not in (
            _RERUNNABLE_AGENT1_STATUSES
        ):
            raise AIStatisticsWorkflowError(
                "Page Classification has already started or produced a result."
            )
        if self.status_for(AIStatisticsStage.PAGE_CLASSIFICATION) not in {
            AIStatisticsStageStatus.WAITING,
            AIStatisticsStageStatus.FAILED,
        } and self._agent2_and_downstream_have_progress():
            self._invalidate_agent2_and_downstream()
            snapshot = self._snapshot
        self._snapshot = _replace_snapshot(
            snapshot,
            active_stage=AIStatisticsStage.PAGE_CLASSIFICATION,
            stage_statuses=_set_status(
                snapshot.stage_statuses,
                AIStatisticsStage.PAGE_CLASSIFICATION,
                AIStatisticsStageStatus.RUNNING,
            ),
            classification_progress_completed=0,
            classification_progress_total=snapshot.page_count,
            error_message="",
        )
        return self._snapshot

    def classify_page_document(
        self,
        *,
        on_progress: Agent1ProgressCallback | None = None,
        is_cancelled=None,
    ) -> PageClassificationResult:
        snapshot = self._snapshot
        if snapshot.pdf_path is None:
            raise AIStatisticsWorkflowError("No PDF is loaded.")
        if self.status_for(
            AIStatisticsStage.PAGE_CLASSIFICATION
        ) != AIStatisticsStageStatus.RUNNING:
            raise AIStatisticsWorkflowError(
                "Page Classification has not been started."
            )
        result = self._page_classification_provider.classify(
            snapshot.pdf_path,
            snapshot.page_count,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
        )
        if not any(
            result.has_page_type(page_type.value)
            for page_type in (
                DrawingPageType.ARRAY_LAYOUT,
                DrawingPageType.OVERALL_LAYOUT,
            )
        ):
            raise Agent1NoRelevantDrawingPagesError(
                "No Array Layout or Overall Layout pages were recognized. "
                "This PDF may not be a drawing for the current project. "
                "Please upload the correct project drawing PDF."
            )
        return result

    def update_page_classification_progress(
        self,
        completed: int,
        total: int,
    ) -> AIStatisticsSnapshot:
        if self.status_for(
            AIStatisticsStage.PAGE_CLASSIFICATION
        ) != AIStatisticsStageStatus.RUNNING:
            return self._snapshot
        self._snapshot = _replace_snapshot(
            self._snapshot,
            classification_progress_completed=max(0, min(completed, total)),
            classification_progress_total=max(0, total),
        )
        return self._snapshot

    def complete_page_classification(
        self,
        result: PageClassificationResult,
    ) -> AIStatisticsSnapshot:
        if result.total_pages != self._snapshot.page_count:
            raise AIStatisticsWorkflowError(
                "Page classifier result does not match PDF page count."
            )
        self._snapshot = _replace_snapshot(
            self._snapshot,
            furthest_stage=AIStatisticsStage.PAGE_CLASSIFICATION,
            stage_statuses=_set_status(
                self._snapshot.stage_statuses,
                AIStatisticsStage.PAGE_CLASSIFICATION,
                AIStatisticsStageStatus.RESULT_READY,
            ),
            classification_progress_completed=result.total_pages,
            classification_progress_total=result.total_pages,
            error_message="",
            page_classification=result,
        )
        return self._snapshot

    def fail_page_classification(self, message: str) -> AIStatisticsSnapshot:
        self._snapshot = _replace_snapshot(
            self._snapshot,
            stage_statuses=_set_status(
                self._snapshot.stage_statuses,
                AIStatisticsStage.PAGE_CLASSIFICATION,
                AIStatisticsStageStatus.FAILED,
            ),
            error_message=message,
        )
        return self._snapshot

    def cancel_page_classification(self) -> AIStatisticsSnapshot:
        self._snapshot = _replace_snapshot(
            self._snapshot,
            active_stage=None,
            stage_statuses=_set_status(
                self._snapshot.stage_statuses,
                AIStatisticsStage.PAGE_CLASSIFICATION,
                AIStatisticsStageStatus.WAITING,
            ),
            classification_progress_completed=0,
            classification_progress_total=0,
            error_message="",
        )
        return self._snapshot

    def run_page_classification(self) -> PageClassificationResult:
        """Synchronous compatibility entry point used by non-GUI callers."""

        self.begin_page_classification()
        try:
            result = self.classify_page_document(
                on_progress=self.update_page_classification_progress,
            )
        except Agent1Cancelled:
            self.cancel_page_classification()
            raise
        except Exception as exc:
            self.fail_page_classification(str(exc))
            raise
        self.complete_page_classification(result)
        return result

    def update_page_classification(
        self,
        page_number: int,
        page_type: str,
    ) -> AIStatisticsSnapshot:
        result = self._snapshot.page_classification
        if result is None:
            raise AIStatisticsWorkflowError("No page classification result.")
        matched = False
        pages = []
        for page in result.pages:
            if page.page_number != page_number:
                pages.append(page)
                continue
            matched = True
            pages.append(
                type(page)(
                    page_number=page.page_number,
                    page_type=page_type,
                    purpose=page.purpose,
                    source=page.source,
                    state=RecognitionState.USER_CONFIRMED,
                )
            )
        if not matched:
            raise AIStatisticsWorkflowError(
                f"Unknown PDF page number: {page_number}"
            )
        if self._agent2_and_downstream_have_progress():
            self._invalidate_agent2_and_downstream()
        self._snapshot = _replace_snapshot(
            self._snapshot,
            page_classification=PageClassificationResult(
                pages=tuple(pages),
                thumbnails=result.thumbnails,
                page_images=result.page_images,
            ),
        )
        return self._snapshot

    def begin_overall_site_recognition(self) -> AIStatisticsSnapshot:
        snapshot = self._snapshot
        if snapshot.page_classification is None:
            raise AIStatisticsWorkflowError(
                "Page Classification must finish before Agent2."
            )
        if snapshot.selected_stage != AIStatisticsStage.INFO_RECOGNITION:
            raise AIStatisticsWorkflowError(
                "Agent2 can only run from Information Recognition."
            )
        if self.status_for(AIStatisticsStage.INFO_RECOGNITION) not in (
            _RERUNNABLE_AGENT2_STATUSES
        ):
            raise AIStatisticsWorkflowError(
                "Information Recognition has already started or finished."
            )
        if self._agent3_and_downstream_have_progress():
            self._invalidate_agent3_and_downstream()
            snapshot = self._snapshot
        overall_page_numbers = (
            snapshot.page_classification.available_page_numbers_for_type(
                DrawingPageType.OVERALL_LAYOUT.value
            )
        )
        if not overall_page_numbers:
            raise AIStatisticsWorkflowError(
                "No reviewed Overall Layout page is available."
            )
        self._snapshot = _replace_snapshot(
            snapshot,
            active_stage=AIStatisticsStage.INFO_RECOGNITION,
            stage_statuses=_set_status(
                snapshot.stage_statuses,
                AIStatisticsStage.INFO_RECOGNITION,
                AIStatisticsStageStatus.RUNNING,
            ),
            error_message="",
            information_progress_completed=0,
            information_progress_total=len(overall_page_numbers) * 2,
            overall_site_orientation=None,
        )
        return self._snapshot

    def recognize_overall_site_pages(
        self,
        *,
        on_progress=None,
        is_cancelled=None,
    ) -> tuple[OverallSiteExtraction, ...]:
        snapshot = self._snapshot
        if self.status_for(
            AIStatisticsStage.INFO_RECOGNITION
        ) != AIStatisticsStageStatus.RUNNING:
            raise AIStatisticsWorkflowError("Agent2 has not been started.")
        assert snapshot.page_classification is not None
        page_numbers = tuple(
            sorted(
                snapshot.page_classification.page_numbers_for_type(
                    DrawingPageType.OVERALL_LAYOUT.value
                )
            )
        )
        images = self._materialize_overall_page_images(
            page_numbers,
            is_cancelled=is_cancelled,
        )
        recognizer = self._configure_overall_site_recognizer(
            retry=self.is_overall_site_retry_run(),
        )
        if on_progress is None and is_cancelled is None:
            return recognizer.recognize_pages(images)
        return recognizer.recognize_pages(
            images,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
        )

    def update_information_recognition_progress(
        self,
        completed: int,
        total: int,
    ) -> AIStatisticsSnapshot:
        if self.status_for(
            AIStatisticsStage.INFO_RECOGNITION
        ) != AIStatisticsStageStatus.RUNNING:
            return self._snapshot
        self._snapshot = _replace_snapshot(
            self._snapshot,
            information_progress_completed=max(
                0,
                min(completed, total),
            ),
            information_progress_total=max(0, total),
        )
        return self._snapshot

    def complete_overall_site_recognition(
        self,
        results: tuple[OverallSiteExtraction, ...],
    ) -> AIStatisticsSnapshot:
        if not results:
            raise AIStatisticsWorkflowError(
                "Agent2 returned no overall-site extraction."
            )
        try:
            adapted = Agent2ElementContract.from_overall_site(results)
        except (AttributeError, TypeError, ValueError):
            tracker_definitions: tuple[TrackerDefinition, ...] = ()
        else:
            tracker_definitions = adapted.tracker_definitions
        self._tracker_definitions = tracker_definitions
        self._agent2_inv_names = inv_names_from_overall_site(results)
        self._agent2_inv_string_counts = inv_string_counts_from_overall_site(
            results
        )
        statuses = _set_status(
            self._snapshot.stage_statuses,
            AIStatisticsStage.INFO_RECOGNITION,
            AIStatisticsStageStatus.NEEDS_CONFIRM,
        )
        statuses = _set_status(
            statuses,
            AIStatisticsStage.ELEMENT_RECOGNITION,
            AIStatisticsStageStatus.WAITING,
        )
        statuses = _set_status(
            statuses,
            AIStatisticsStage.VALIDATION,
            AIStatisticsStageStatus.WAITING,
        )
        self._snapshot = _replace_snapshot(
            self._snapshot,
            furthest_stage=_max_stage(
                self._snapshot.furthest_stage,
                AIStatisticsStage.INFO_RECOGNITION,
            ),
            stage_statuses=statuses,
            overall_site_extractions=results,
            tracker_totals=tuple(
                TrackerOcrTotal(
                    tracker_type=item.code,
                    total=item.total_count,
                )
                for item in tracker_definitions
            ),
            element_recognition_result=None,
            reviewed_tree=None,
            validation_report=None,
            information_progress_completed=(
                self._snapshot.information_progress_total
            ),
            error_message="",
        )
        return self._snapshot

    def update_overall_site_review(
        self,
        results: tuple[OverallSiteExtraction, ...],
        orientation: str,
    ) -> AIStatisticsSnapshot:
        """Persist user-edited Agent2 data and the required orientation."""

        panel_orientation = _OVERALL_SITE_PANEL_ORIENTATIONS.get(orientation)
        if panel_orientation is None:
            raise AIStatisticsWorkflowError(
                "A valid Agent2 connection orientation is required."
            )
        if not results:
            raise AIStatisticsWorkflowError(
                "Agent2 review must contain at least one extraction."
            )
        self._default_panel_orientation = panel_orientation
        self._agent2_inv_names = inv_names_from_overall_site(results)
        self._agent2_inv_string_counts = inv_string_counts_from_overall_site(
            results
        )
        element_result = self._snapshot.element_recognition_result
        if element_result is not None:
            element_result = self._refresh_inv_name_matches(element_result)
        statuses = self._snapshot.stage_statuses
        if self._snapshot.validation_report is not None:
            # Agent4 compares the structure against these edited counts. A
            # previously confirmed report is now stale and must not remain
            # downloadable/importable; confirming Agent3 runs it again.
            statuses = _set_status(
                statuses,
                AIStatisticsStage.VALIDATION,
                AIStatisticsStageStatus.WAITING,
            )
        self._snapshot = _replace_snapshot(
            self._snapshot,
            overall_site_extractions=results,
            overall_site_orientation=orientation,
            element_recognition_result=element_result,
            validation_report=None,
            stage_statuses=statuses,
        )
        return self._snapshot

    def fail_overall_site_recognition(self, message: str) -> AIStatisticsSnapshot:
        self._snapshot = _replace_snapshot(
            self._snapshot,
            stage_statuses=_set_status(
                self._snapshot.stage_statuses,
                AIStatisticsStage.INFO_RECOGNITION,
                AIStatisticsStageStatus.FAILED,
            ),
            error_message=message,
        )
        return self._snapshot

    def cancel_overall_site_recognition(self) -> AIStatisticsSnapshot:
        self._snapshot = _replace_snapshot(
            self._snapshot,
            active_stage=AIStatisticsStage.INFO_RECOGNITION,
            stage_statuses=_set_status(
                self._snapshot.stage_statuses,
                AIStatisticsStage.INFO_RECOGNITION,
                AIStatisticsStageStatus.WAITING,
            ),
            error_message="",
            information_progress_completed=0,
            information_progress_total=0,
        )
        return self._snapshot

    def run_overall_site_recognition(
        self,
    ) -> tuple[OverallSiteExtraction, ...]:
        """Synchronous Agent2 entry point for service and test callers."""

        self.begin_overall_site_recognition()
        try:
            results = self.recognize_overall_site_pages()
        except Exception as exc:
            self.fail_overall_site_recognition(str(exc))
            raise
        self.complete_overall_site_recognition(results)
        return results

    def confirm_current_stage(self) -> AIStatisticsSnapshot:
        snapshot = self._snapshot
        stage = snapshot.selected_stage
        if stage == AIStatisticsStage.VALIDATION:
            return snapshot
        if not self.is_confirm_enabled(stage):
            raise AIStatisticsWorkflowError(
                f"Stage cannot be confirmed yet: {stage.name}"
            )
        if stage == AIStatisticsStage.PAGE_CLASSIFICATION:
            if self._agent2_and_downstream_have_progress():
                self._invalidate_agent2_and_downstream()
                snapshot = self._snapshot
        next_stage = stage.next()
        if next_stage is None:
            return snapshot

        furthest = _max_stage(snapshot.furthest_stage, next_stage)
        statuses = _set_status(
            snapshot.stage_statuses,
            stage,
            AIStatisticsStageStatus.CONFIRMED,
        )
        changes: dict[str, object] = {
            "selected_stage": next_stage,
            "active_stage": next_stage,
            "furthest_stage": furthest,
            "stage_statuses": statuses,
        }
        if next_stage == AIStatisticsStage.VALIDATION:
            reviewed_tree = snapshot.reviewed_tree
            if reviewed_tree is None:
                raise AIStatisticsWorkflowError(
                    "Agent4 requires a reviewed Agent3 structure."
                )
            totals_payload = _build_agent4_totals_payload(snapshot)
            report = self._validation_service.validate(
                totals_payload,
                _agent4_structure_payload(
                    snapshot.element_recognition_result,
                    reviewed_tree,
                ),
            )
            changes.update(
                {
                    "stage_statuses": _set_status(
                        statuses,
                        AIStatisticsStage.VALIDATION,
                        AIStatisticsStageStatus.RESULT_READY,
                    ),
                    "tracker_totals": report.tracker_totals,
                    "reviewed_tree": report.reviewed_tree,
                    "validation_report": report,
                }
            )
        self._snapshot = _replace_snapshot(snapshot, **changes)
        return self._snapshot

    def begin_element_recognition(self) -> AIStatisticsSnapshot:
        if self._snapshot.selected_stage != AIStatisticsStage.ELEMENT_RECOGNITION:
            raise AIStatisticsWorkflowError(
                "Agent3 can only run from Element Recognition."
            )
        if self.status_for(AIStatisticsStage.ELEMENT_RECOGNITION) not in {
            AIStatisticsStageStatus.WAITING,
            AIStatisticsStageStatus.FAILED,
            AIStatisticsStageStatus.RESULT_READY,
            AIStatisticsStageStatus.NEEDS_CONFIRM,
            AIStatisticsStageStatus.CONFIRMED,
        }:
            raise AIStatisticsWorkflowError("Element Recognition is already running.")
        page_count = self._require_element_request_possible()
        # A production run owns a fresh killable process. Injected recognizers
        # keep the legacy optional in-process warmup behavior.
        if self._element_process_runner is not None:
            self._element_process_runner.reset()
        else:
            self._start_element_warmup(len(page_count))
        # A rerun replaces the structure a previous Agent4 report validated,
        # so that report must not survive the new run.
        statuses = _set_status(
            self._snapshot.stage_statuses,
            AIStatisticsStage.ELEMENT_RECOGNITION,
            AIStatisticsStageStatus.RUNNING,
        )
        statuses = _set_status(
            statuses,
            AIStatisticsStage.VALIDATION,
            AIStatisticsStageStatus.WAITING,
        )
        self._snapshot = _replace_snapshot(
            self._snapshot,
            active_stage=AIStatisticsStage.ELEMENT_RECOGNITION,
            stage_statuses=statuses,
            validation_report=None,
            element_progress_completed=0,
            element_progress_total=len(page_count) * 5,
            element_progress_steps=tuple(
                AIStatisticsProgressStep(
                    key=key,
                    total=(1 if key == "finalizing" else len(page_count)),
                )
                for key in AGENT3_PROGRESS_STEP_ORDER
            ),
            element_progress_current_step="",
            element_progress_page_number=None,
            element_progress_detail="",
            element_progress_active_tasks=(),
            error_message="",
        )
        return self._snapshot

    def recognize_element_pages(
        self,
        *,
        on_progress=None,
        on_step=None,
        is_cancelled=None,
    ) -> ElementRecognitionResult:
        if self.status_for(AIStatisticsStage.ELEMENT_RECOGNITION) != AIStatisticsStageStatus.RUNNING:
            raise AIStatisticsWorkflowError("Agent3 has not been started.")
        request = self._build_element_request(materialize_images=False)
        logger.info(
            "agent3.request pages=%s",
            [page.page_number for page in request.pages],
        )
        if self._element_process_runner is not None:
            snapshot = self._snapshot
            if snapshot.pdf_path is None or snapshot.page_classification is None:
                raise AIStatisticsWorkflowError(
                    "Agent3 rendering requires the source PDF."
                )
            page_numbers = {page.page_number for page in request.pages}
            images = snapshot.page_classification.page_images
            if isinstance(images, LazyPageImages):
                cached_images = images.cached_items(page_numbers)
            else:
                cached_images = {
                    page_number: images[page_number]
                    for page_number in page_numbers
                    if page_number in images
                }
            result, rendered_images = self._element_process_runner.run(
                request,
                snapshot.pdf_path,
                cached_images,
                on_progress=on_progress,
                on_step=on_step,
                is_cancelled=is_cancelled,
            )
            if isinstance(images, LazyPageImages):
                for page_number, image in rendered_images.items():
                    images.cache_page(page_number, image)
            elif isinstance(images, MutableMapping):
                images.update(rendered_images)
            return result

        recognizer = self._ensure_element_recognizer()
        recognize_stream = getattr(recognizer, "recognize_stream", None)
        if callable(recognize_stream):
            return recognize_stream(
                request,
                self._iter_element_page_inputs(
                    tuple(page.page_number for page in request.pages),
                    on_step=on_step,
                    is_cancelled=is_cancelled,
                ),
                on_progress=on_progress,
                on_step=on_step,
                is_cancelled=is_cancelled,
            )
        # Compatibility for injected recognizers that only implement the old
        # eager API (primarily application tests and third-party integrations).
        request = self._build_element_request()
        return recognizer.recognize(
            request,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
        )

    def cancel_element_execution(self) -> None:
        """Immediately terminate the production Agent3 process, if active."""

        if self._element_process_runner is not None:
            self._element_process_runner.cancel()

    def _iter_element_page_inputs(
        self,
        page_numbers: tuple[int, ...],
        *,
        on_step=None,
        is_cancelled=None,
    ) -> Iterator[ArrayLayoutPageInput]:
        """Yield selected Agent3 pages as soon as parallel renders finish."""

        snapshot = self._snapshot
        result = snapshot.page_classification
        if result is None or snapshot.pdf_path is None:
            raise AIStatisticsWorkflowError("Agent3 rendering requires the source PDF.")
        images = result.page_images
        render_processes = DrawingClassificationConfig.from_env().render_processes
        if render_processes <= 0:
            raise AIStatisticsWorkflowError("AGENT1_RENDER_PROCESSES must be positive.")

        grayscale = _resolve_grayscale(None)
        cached: list[ArrayLayoutPageInput] = []
        missing: list[int] = []
        for page_number in page_numbers:
            image = _cached_mapping_image(images, page_number)
            if image is None:
                missing.append(page_number)
                continue
            # UI keeps the color original. Table OCR stays unenhanced;
            # AGENT3_LINE_ENHANCE is applied only to Tracker/Box YOLO.
            display_image = encode_agent3_page_png(
                image,
                max_side=AGENT3_RENDER_MAX_SIDE,
            )
            if isinstance(images, LazyPageImages):
                images.cache_page(page_number, display_image)
            elif isinstance(images, MutableMapping):
                images[page_number] = display_image
            recognize_image = (
                encode_agent3_page_png(display_image, grayscale=True)
                if grayscale
                else display_image
            )
            cached.append(ArrayLayoutPageInput(page_number, recognize_image))

        for page in cached:
            if is_cancelled is not None and is_cancelled():
                raise Agent3Cancelled("Element recognition was cancelled.")
            if on_step is not None:
                on_step(
                    Agent3ProgressUpdate(
                        "rendering",
                        "running",
                        page.page_number,
                        "cached",
                        f"agent3-render-{page.page_number}",
                    )
                )
                on_step(
                    Agent3ProgressUpdate(
                        "rendering",
                        "completed",
                        page.page_number,
                        "cached",
                        f"agent3-render-{page.page_number}",
                    )
                )
            yield page
        if not missing:
            return

        if render_processes == 1:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent3-render")
            futures = {
                executor.submit(
                    render_high_resolution_page,
                    snapshot.pdf_path,
                    page_number - 1,
                    width=AGENT3_RENDER_MAX_SIDE,
                    height=AGENT3_RENDER_MAX_SIDE,
                ): page_number
                for page_number in missing
            }
        else:
            executor = ProcessPoolExecutor(
                max_workers=render_processes,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=initialize_pdf_render_worker,
                initargs=(str(snapshot.pdf_path),),
            )
            futures = {
                executor.submit(
                    render_high_resolution_page_worker,
                    page_number - 1,
                    AGENT3_RENDER_MAX_SIDE,
                    AGENT3_RENDER_MAX_SIDE,
                ): page_number
                for page_number in missing
            }
        if on_step is not None:
            for page_number in missing:
                on_step(
                    Agent3ProgressUpdate(
                        "rendering",
                        "running",
                        page_number,
                        worker_name=f"agent3-render-{page_number}",
                    )
                )
        try:
            for future in as_completed(futures):
                if is_cancelled is not None and is_cancelled():
                    raise Agent3Cancelled("Element recognition was cancelled.")
                page_number = futures[future]
                try:
                    rendered = future.result()
                except Exception as exc:
                    if on_step is not None:
                        on_step(
                            Agent3ProgressUpdate(
                                "rendering",
                                "failed",
                                page_number,
                                str(exc),
                                f"agent3-render-{page_number}",
                            )
                        )
                    raise
                image = rendered[1] if isinstance(rendered, tuple) else rendered
                display_image = encode_agent3_page_png(
                    image,
                    max_side=AGENT3_RENDER_MAX_SIDE,
                )
                if isinstance(images, LazyPageImages):
                    images.cache_page(page_number, display_image)
                logger.info(
                    "agent3.render page=%d max_side=%d bytes=%d",
                    page_number,
                    AGENT3_RENDER_MAX_SIDE,
                    len(display_image),
                )
                if on_step is not None:
                    on_step(
                        Agent3ProgressUpdate(
                            "rendering",
                            "completed",
                            page_number,
                            worker_name=f"agent3-render-{page_number}",
                        )
                    )
                recognize_image = (
                    encode_agent3_page_png(display_image, grayscale=True)
                    if grayscale
                    else display_image
                )
                yield ArrayLayoutPageInput(page_number, recognize_image)
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)

    def _agent2_and_downstream_have_progress(self) -> bool:
        snapshot = self._snapshot
        if snapshot.overall_site_extractions:
            return True
        info_status = self.status_for(AIStatisticsStage.INFO_RECOGNITION)
        if info_status not in {
            AIStatisticsStageStatus.WAITING,
            AIStatisticsStageStatus.RUNNING,
        }:
            return True
        if snapshot.element_recognition_result is not None:
            return True
        if snapshot.reviewed_tree is not None:
            return True
        if snapshot.validation_report is not None:
            return True
        return False

    def _invalidate_agent2_and_downstream(self) -> None:
        """Drop Agent2+ results after Agent1 input changes."""

        self._agent2_inv_names = ()
        self._agent2_inv_string_counts = ()
        self._tracker_definitions = ()
        self._default_panel_orientation = PanelOrientation.UNKNOWN
        snapshot = self._snapshot
        statuses = snapshot.stage_statuses
        for downstream_stage in (
            AIStatisticsStage.INFO_RECOGNITION,
            AIStatisticsStage.ELEMENT_RECOGNITION,
            AIStatisticsStage.VALIDATION,
        ):
            statuses = _set_status(
                statuses,
                downstream_stage,
                AIStatisticsStageStatus.WAITING,
            )
        furthest = snapshot.furthest_stage
        if (
            furthest is not None
            and furthest.index > AIStatisticsStage.INFO_RECOGNITION.index
        ):
            furthest = AIStatisticsStage.INFO_RECOGNITION
        self._snapshot = _replace_snapshot(
            snapshot,
            furthest_stage=furthest,
            stage_statuses=statuses,
            overall_site_extractions=(),
            overall_site_orientation=None,
            tracker_totals=(),
            element_recognition_result=None,
            reviewed_tree=None,
            validation_report=None,
            information_progress_completed=0,
            information_progress_total=0,
            element_progress_completed=0,
            element_progress_total=0,
            element_progress_steps=(),
            element_progress_current_step="",
            element_progress_page_number=None,
            element_progress_detail="",
            element_progress_active_tasks=(),
            error_message="",
        )

    def _agent3_and_downstream_have_progress(self) -> bool:
        snapshot = self._snapshot
        if snapshot.element_recognition_result is not None:
            return True
        if snapshot.reviewed_tree is not None:
            return True
        if snapshot.validation_report is not None:
            return True
        element_status = self.status_for(AIStatisticsStage.ELEMENT_RECOGNITION)
        if element_status not in {
            AIStatisticsStageStatus.WAITING,
            AIStatisticsStageStatus.RUNNING,
        }:
            return True
        validation_status = self.status_for(AIStatisticsStage.VALIDATION)
        if validation_status not in {
            AIStatisticsStageStatus.WAITING,
            AIStatisticsStageStatus.RUNNING,
        }:
            return True
        return False

    def _invalidate_agent3_and_downstream(self) -> None:
        """Drop Agent3+ results when Agent2 is rerun."""

        snapshot = self._snapshot
        statuses = snapshot.stage_statuses
        for downstream_stage in (
            AIStatisticsStage.ELEMENT_RECOGNITION,
            AIStatisticsStage.VALIDATION,
        ):
            statuses = _set_status(
                statuses,
                downstream_stage,
                AIStatisticsStageStatus.WAITING,
            )
        furthest = snapshot.furthest_stage
        if (
            furthest is not None
            and furthest.index > AIStatisticsStage.ELEMENT_RECOGNITION.index
        ):
            furthest = AIStatisticsStage.ELEMENT_RECOGNITION
        self._snapshot = _replace_snapshot(
            snapshot,
            furthest_stage=furthest,
            stage_statuses=statuses,
            element_recognition_result=None,
            reviewed_tree=None,
            validation_report=None,
            element_progress_completed=0,
            element_progress_total=0,
            element_progress_steps=(),
            element_progress_current_step="",
            element_progress_page_number=None,
            element_progress_detail="",
            element_progress_active_tasks=(),
            error_message="",
        )

    def is_overall_site_retry_run(self) -> bool:
        """Whether the next Agent2 run should retry with MinerU table location."""

        if self._snapshot.selected_stage != AIStatisticsStage.INFO_RECOGNITION:
            return False
        if self._snapshot.overall_site_extractions:
            return True
        return self.status_for(AIStatisticsStage.INFO_RECOGNITION) in {
            AIStatisticsStageStatus.NEEDS_CONFIRM,
            AIStatisticsStageStatus.RESULT_READY,
            AIStatisticsStageStatus.CONFIRMED,
            AIStatisticsStageStatus.FAILED,
        }

    def _configure_overall_site_recognizer(
        self,
        *,
        retry: bool,
    ) -> Agent2OverallSiteRecognizer:
        """Create or reuse Agent2 and apply the table-location strategy."""

        recognizer = self._ensure_overall_site_recognizer()
        base = OverallSiteExtractionConfig.from_env()
        # The retry only switches the locator (YOLO -> MinerU); it must keep the
        # configured vision provider, so a *_glm run never falls back to Claude.
        recognizer.config = (
            replace(
                base,
                strategy=(
                    STRATEGY_MINERU_GLM
                    if base.uses_glm
                    else STRATEGY_MINERU_CLAUDE
                ),
            )
            if retry
            else base
        )
        return recognizer

    def _ensure_overall_site_recognizer(self) -> Agent2OverallSiteRecognizer:
        """Create the Agent2 recognizer once and reuse it across runs.

        Reusing one instance keeps the overall-schedule YOLO session warm
        instead of paying ONNX Runtime startup again after cancel/retry.
        """

        if self._overall_site_recognizer is None:
            self._overall_site_recognizer = Agent2OverallSiteRecognizer()
        return self._overall_site_recognizer

    def _materialize_overall_page_images(
        self,
        page_numbers: tuple[int, ...],
        *,
        is_cancelled=None,
    ) -> dict[int, bytes]:
        """Load Overall Layout PNGs in a PDFium worker process.

        Agent2 shares the GUI process with Qt's PDF document. Opening the
        same file with pypdfium2 in that process can stall the first run
        until the user cancels. Worker processes isolate PDFium the same
        way Agent1/Agent3 already do.
        """

        snapshot = self._snapshot
        result = snapshot.page_classification
        if result is None or snapshot.pdf_path is None:
            raise AIStatisticsWorkflowError(
                "Agent2 rendering requires the source PDF."
            )
        images = result.page_images
        materialized: dict[int, bytes] = {}
        missing: list[int] = []
        for page_number in page_numbers:
            if is_cancelled is not None and is_cancelled():
                raise Agent2Cancelled("Information recognition was cancelled.")
            cached = _cached_mapping_image(images, page_number)
            if cached is None:
                missing.append(page_number)
                continue
            materialized[page_number] = cached
        if not missing:
            return materialized

        render_processes = DrawingClassificationConfig.from_env().render_processes
        if render_processes <= 0:
            raise AIStatisticsWorkflowError(
                "AGENT1_RENDER_PROCESSES must be positive."
            )
        width, height = PAGE_RENDER_WIDTH, PAGE_RENDER_HEIGHT
        if isinstance(images, LazyPageImages):
            width, height = images.render_size
        executor = ProcessPoolExecutor(
            max_workers=min(render_processes, len(missing)),
            mp_context=multiprocessing.get_context("spawn"),
            initializer=initialize_pdf_render_worker,
            initargs=(str(snapshot.pdf_path),),
        )
        futures = {
            executor.submit(
                render_high_resolution_page_worker,
                page_number - 1,
                width,
                height,
            ): page_number
            for page_number in missing
        }
        try:
            for future in as_completed(futures):
                if is_cancelled is not None and is_cancelled():
                    raise Agent2Cancelled(
                        "Information recognition was cancelled."
                    )
                page_number = futures[future]
                rendered = future.result()
                image = rendered[1] if isinstance(rendered, tuple) else rendered
                if isinstance(images, LazyPageImages):
                    images.cache_page(page_number, image)
                elif isinstance(images, MutableMapping):
                    images[page_number] = image
                materialized[page_number] = image
                logger.info(
                    "agent2.render page=%d bytes=%d",
                    page_number,
                    len(image),
                )
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
        return materialized

    def _ensure_element_recognizer(self) -> Agent3ElementRecognizer:
        """Create the Agent3 recognizer once and reuse it across runs.

        Reusing one instance keeps loaded YOLO runtimes warm between
        recognitions instead of paying model loading again per run.
        """

        if self._element_recognizer is None:
            self._element_recognizer = Agent3ElementRecognizer()
        return self._element_recognizer

    def _start_element_warmup(self, page_count: int) -> None:
        """Preload Agent3's local runtime on a daemon thread, best-effort."""

        if not self._auto_warm_element_runtime:
            return
        if self._element_warmup_started:
            return
        recognizer = self._ensure_element_recognizer()
        warmup = getattr(recognizer, "warmup", None)
        if warmup is None or not callable(warmup):
            return
        self._element_warmup_started = True

        def _warm() -> None:
            try:
                try:
                    warmup(page_count=page_count)
                except TypeError:
                    warmup()
            except Exception as exc:  # pragma: no cover - depends on runtime
                logger.warning("agent3.warmup failed: %s", exc)
            else:
                logger.info("agent3.warmup ready")

        threading.Thread(target=_warm, name="agent3-warmup", daemon=True).start()

    def update_element_recognition_progress(self, completed: int, total: int) -> AIStatisticsSnapshot:
        if self.status_for(AIStatisticsStage.ELEMENT_RECOGNITION) != AIStatisticsStageStatus.RUNNING:
            return self._snapshot
        self._snapshot = _replace_snapshot(
            self._snapshot,
            element_progress_completed=max(0, min(completed, total)),
            element_progress_total=max(0, total),
        )
        return self._snapshot

    def update_element_recognition_step(
        self,
        update: Agent3ProgressUpdate,
    ) -> AIStatisticsSnapshot:
        """Aggregate detailed Agent3 events without changing the legacy bar."""

        if self.status_for(AIStatisticsStage.ELEMENT_RECOGNITION) != AIStatisticsStageStatus.RUNNING:
            return self._snapshot
        steps = list(self._snapshot.element_progress_steps)
        active_tasks = list(self._snapshot.element_progress_active_tasks)
        for index, step in enumerate(steps):
            if step.key != update.step:
                continue
            if update.status == "running":
                steps[index] = AIStatisticsProgressStep(
                    key=step.key,
                    completed=step.completed,
                    total=step.total,
                    failed=step.failed,
                    active=step.active + 1,
                )
            elif update.status in {"completed", "failed"}:
                steps[index] = AIStatisticsProgressStep(
                    key=step.key,
                    completed=min(step.total, step.completed + 1),
                    total=step.total,
                    failed=step.failed + (1 if update.status == "failed" else 0),
                    active=max(0, step.active - 1),
                )
            break
        worker_name = update.worker_name or (
            f"{update.step}:{update.page_number}"
        )
        if update.status == "running":
            active_tasks = [
                task
                for task in active_tasks
                if task.worker_name != worker_name
            ]
            active_tasks.append(
                AIStatisticsActiveTask(
                    worker_name=worker_name,
                    step=update.step,
                    page_number=update.page_number,
                    detail=update.detail,
                )
            )
        elif update.status in {"completed", "failed"}:
            active_tasks = [
                task
                for task in active_tasks
                if not (
                    task.worker_name == worker_name
                    and task.step == update.step
                    and task.page_number == update.page_number
                )
            ]
        self._snapshot = _replace_snapshot(
            self._snapshot,
            element_progress_steps=tuple(steps),
            element_progress_current_step=update.step,
            element_progress_page_number=update.page_number,
            element_progress_detail=update.detail,
            element_progress_active_tasks=tuple(active_tasks),
        )
        return self._snapshot

    def complete_element_recognition(self, result: ElementRecognitionResult) -> AIStatisticsSnapshot:
        if not result.has_output:
            raise AIStatisticsWorkflowError("Agent3 returned no reviewable structure.")
        result = self._refresh_inv_name_matches(result)
        self._snapshot = _replace_snapshot(
            self._snapshot,
            furthest_stage=_max_stage(
                self._snapshot.furthest_stage,
                AIStatisticsStage.ELEMENT_RECOGNITION,
            ),
            stage_statuses=_set_status(
                self._snapshot.stage_statuses,
                AIStatisticsStage.ELEMENT_RECOGNITION,
                AIStatisticsStageStatus.RESULT_READY,
            ),
            element_recognition_result=result,
            reviewed_tree=result.reviewed_tree,
            element_progress_completed=self._snapshot.element_progress_total,
            element_progress_active_tasks=(),
            error_message="",
        )
        return self._snapshot

    def fail_element_recognition(self, message: str) -> AIStatisticsSnapshot:
        self._snapshot = _replace_snapshot(
            self._snapshot,
            stage_statuses=_set_status(
                self._snapshot.stage_statuses,
                AIStatisticsStage.ELEMENT_RECOGNITION,
                AIStatisticsStageStatus.FAILED,
            ),
            element_failure_context={
                "error": message,
                "step": self._snapshot.element_progress_current_step,
                "page_number": self._snapshot.element_progress_page_number,
                "detail": self._snapshot.element_progress_detail,
                "active_tasks": [asdict(t) for t in self._snapshot.element_progress_active_tasks],
            },
            element_progress_active_tasks=(),
            error_message=message,
        )
        return self._snapshot

    def cancel_element_recognition(self) -> AIStatisticsSnapshot:
        self._snapshot = _replace_snapshot(
            self._snapshot,
            stage_statuses=_set_status(
                self._snapshot.stage_statuses,
                AIStatisticsStage.ELEMENT_RECOGNITION,
                AIStatisticsStageStatus.WAITING,
            ),
            element_progress_completed=0,
            element_progress_total=0,
            element_progress_steps=(),
            element_progress_current_step="",
            element_progress_page_number=None,
            element_progress_detail="",
            element_progress_active_tasks=(),
            error_message="",
        )
        return self._snapshot

    def run_element_recognition(self) -> ElementRecognitionResult:
        self.begin_element_recognition()
        try:
            result = self.recognize_element_pages(
                on_progress=self.update_element_recognition_progress,
            )
        except Agent3Cancelled:
            self.cancel_element_recognition()
            raise
        except Exception as exc:
            self.fail_element_recognition(str(exc))
            raise
        self.complete_element_recognition(result)
        return result

    def replace_element_review(self, tree: ReviewedTree) -> AIStatisticsSnapshot:
        result = self._snapshot.element_recognition_result
        if result is None:
            raise AIStatisticsWorkflowError("No Agent3 result is available for review.")
        return self.replace_element_result(replace(result, reviewed_tree=tree))

    def validate_element_result(self, result: ElementRecognitionResult):
        """Calculate Agent4 comparisons for a candidate without advancing stages."""
        return self._validation_service.validate(
            _build_agent4_totals_payload(self._snapshot),
            _agent4_structure_payload(result, result.reviewed_tree),
        )

    def replace_element_result(
        self,
        updated: ElementRecognitionResult,
    ) -> AIStatisticsSnapshot:
        """Persist a reviewed Agent3 result, including edited table rows."""

        if self._snapshot.element_recognition_result is None:
            raise AIStatisticsWorkflowError("No Agent3 result is available for review.")
        from .element_recognition.issue_review import refresh_review_issues

        updated = refresh_review_issues(
            updated,
            self._snapshot.element_recognition_result,
            tracker_definitions=self._tracker_definitions,
            expected_inv_names=self.agent2_inv_names,
        )
        updated = self._refresh_inv_name_matches(updated)
        statuses = _set_status(
            self._snapshot.stage_statuses,
            AIStatisticsStage.ELEMENT_RECOGNITION,
            AIStatisticsStageStatus.NEEDS_CONFIRM,
        )
        statuses = _set_status(
            statuses,
            AIStatisticsStage.VALIDATION,
            AIStatisticsStageStatus.WAITING,
        )
        self._snapshot = _replace_snapshot(
            self._snapshot,
            element_recognition_result=updated,
            reviewed_tree=updated.reviewed_tree,
            validation_report=None,
            stage_statuses=statuses,
        )
        return self._snapshot

    def _refresh_inv_name_matches(
        self,
        result: ElementRecognitionResult,
    ) -> ElementRecognitionResult:
        """Keep hidden Agent2/Agent3 associations aligned with review edits."""

        return replace(
            result,
            inv_name_matches=match_inv_names(
                self.agent2_inv_names,
                (inv.name for inv in result.reviewed_tree.invs),
            ),
        )

    def _require_element_request_possible(self) -> tuple[int, ...]:
        """Validate Agent3 prerequisites without rendering any page image.

        Rendering high-resolution PNGs is expensive; it must happen in the
        recognition worker, never inside GUI-thread enablement checks.
        """

        result = self._snapshot.page_classification
        if result is None:
            raise AIStatisticsWorkflowError("Page Classification must finish before Agent3.")
        if not self._tracker_definitions:
            raise AIStatisticsWorkflowError("Agent2 did not provide tracker definitions.")
        page_numbers = self.array_layout_page_numbers
        if not page_numbers:
            raise AIStatisticsWorkflowError(
                "Agent1 did not provide any reviewed Array Layout pages."
            )
        return page_numbers

    def _build_element_request(
        self,
        *,
        materialize_images: bool = True,
    ) -> ElementRecognitionRequest:
        page_numbers = self._require_element_request_possible()
        result = self._snapshot.page_classification
        assert result is not None
        pages: list[ArrayLayoutPageInput] = []
        for page_number in page_numbers:
            if materialize_images:
                image = result.page_images.get(page_number)
                if image is None:
                    raise AIStatisticsWorkflowError(
                        f"Array Layout page {page_number} has no high-resolution PNG."
                    )
            else:
                image = b""
            pages.append(
                ArrayLayoutPageInput(
                    page_number=page_number,
                    png_bytes=image,
                )
            )
        return ElementRecognitionRequest(
            pages=tuple(pages),
            tracker_definitions=self._tracker_definitions,
            expected_inv_names=self.agent2_inv_names,
            expected_inv_string_counts=self._agent2_inv_string_counts,
            default_panel_orientation=self._default_panel_orientation,
            inv_settings=self._inv_recognition_settings,
            project_name=(self._snapshot.pdf_path.stem if self._snapshot.pdf_path else ""),
            default_offset_snap_step_strings=(
                self._default_offset_snap_step_strings
            ),
        )

    def status_for(self, stage: AIStatisticsStage) -> AIStatisticsStageStatus:
        return self._snapshot.stage_statuses[stage.index]

    def is_stage_clickable(self, stage: AIStatisticsStage) -> bool:
        furthest = self._snapshot.furthest_stage
        return furthest is not None and stage.index <= furthest.index

    def is_run_enabled(self) -> bool:
        if self._snapshot.selected_stage == AIStatisticsStage.ELEMENT_RECOGNITION:
            return self.is_element_recognition_run_enabled()
        return (
            self._snapshot.pdf_path is not None
            and self._snapshot.selected_stage
            == AIStatisticsStage.PAGE_CLASSIFICATION
            and self.status_for(AIStatisticsStage.PAGE_CLASSIFICATION)
            in _RERUNNABLE_AGENT1_STATUSES
        )

    def is_element_recognition_run_enabled(self) -> bool:
        if self._snapshot.selected_stage != AIStatisticsStage.ELEMENT_RECOGNITION:
            return False
        if self.status_for(AIStatisticsStage.ELEMENT_RECOGNITION) == AIStatisticsStageStatus.RUNNING:
            return False
        try:
            self._require_element_request_possible()
        except AIStatisticsWorkflowError:
            return False
        return True

    def is_overall_site_run_enabled(self) -> bool:
        result = self._snapshot.page_classification
        return (
            result is not None
            and self._snapshot.selected_stage
            == AIStatisticsStage.INFO_RECOGNITION
            and self.status_for(AIStatisticsStage.INFO_RECOGNITION)
            in _RERUNNABLE_AGENT2_STATUSES
            and result.has_images_for_type(DrawingPageType.OVERALL_LAYOUT.value)
        )

    def is_confirm_enabled(self, stage: AIStatisticsStage) -> bool:
        if self._snapshot.pdf_path is None:
            return False
        if stage != self._snapshot.selected_stage:
            return False
        if stage == AIStatisticsStage.PAGE_CLASSIFICATION:
            return self.status_for(stage) in {
                AIStatisticsStageStatus.RESULT_READY,
                AIStatisticsStageStatus.CONFIRMED,
            }
        if stage == AIStatisticsStage.ELEMENT_RECOGNITION:
            return self._snapshot.element_recognition_result is not None
        if stage == AIStatisticsStage.INFO_RECOGNITION:
            status_ready = self.status_for(stage) in {
                AIStatisticsStageStatus.RESULT_READY,
                AIStatisticsStageStatus.NEEDS_CONFIRM,
                AIStatisticsStageStatus.CONFIRMED,
            }
            result = self._snapshot.page_classification
            has_overall_image = bool(
                result
                and result.has_images_for_type(
                    DrawingPageType.OVERALL_LAYOUT.value
                )
            )
            return status_ready or not has_overall_image
        return False


def _cached_mapping_image(
    images: Mapping[int, bytes],
    page_number: int,
) -> bytes | None:
    """Read an existing cache entry without triggering LazyPageImages render."""

    if isinstance(images, LazyPageImages):
        return images.cached_items({page_number}).get(page_number)
    return images.get(page_number)


def _set_status(statuses, stage, status):
    updated = list(statuses)
    updated[stage.index] = status
    return tuple(updated)


def _max_stage(
    left: AIStatisticsStage | None,
    right: AIStatisticsStage,
) -> AIStatisticsStage:
    if left is None or right.index > left.index:
        return right
    return left


def _replace_snapshot(
    snapshot: AIStatisticsSnapshot,
    **changes: object,
) -> AIStatisticsSnapshot:
    data = {
        "pdf_path": snapshot.pdf_path,
        "page_count": snapshot.page_count,
        "selected_stage": snapshot.selected_stage,
        "active_stage": snapshot.active_stage,
        "furthest_stage": snapshot.furthest_stage,
        "stage_statuses": snapshot.stage_statuses,
        "page_classification": snapshot.page_classification,
        "tracker_totals": snapshot.tracker_totals,
        "overall_site_extractions": snapshot.overall_site_extractions,
        "overall_site_orientation": snapshot.overall_site_orientation,
        "reviewed_tree": snapshot.reviewed_tree,
        "validation_report": snapshot.validation_report,
        "element_recognition_result": snapshot.element_recognition_result,
        "classification_progress_completed": (
            snapshot.classification_progress_completed
        ),
        "classification_progress_total": snapshot.classification_progress_total,
        "element_progress_completed": snapshot.element_progress_completed,
        "element_progress_total": snapshot.element_progress_total,
        "element_progress_steps": snapshot.element_progress_steps,
        "element_progress_current_step": snapshot.element_progress_current_step,
        "element_progress_page_number": snapshot.element_progress_page_number,
        "element_progress_detail": snapshot.element_progress_detail,
        "element_progress_active_tasks": (
            snapshot.element_progress_active_tasks
        ),
        "information_progress_completed": (
            snapshot.information_progress_completed
        ),
        "information_progress_total": snapshot.information_progress_total,
        "error_message": snapshot.error_message,
    }
    data.update(changes)
    return AIStatisticsSnapshot(**data)


def _build_agent4_totals_payload(
    snapshot: AIStatisticsSnapshot,
) -> dict[str, object]:
    if snapshot.overall_site_extractions:
        return build_agent4_validation_input(snapshot.overall_site_extractions)
    return _tracker_totals_payload(snapshot)


def _agent4_structure_payload(
    element_result: ElementRecognitionResult | None,
    reviewed_tree: ReviewedTree,
) -> dict[str, object]:
    """Serialize the reviewed Agent3 result, reviewed table rows included.

    The reviewed tree alone carries only the Nodes the structure kept, which
    would show Agent4's Node Details as the intersection of table recognition
    and the YOLO + manual structure. The full result keeps every reviewed
    table row, so Agent4 can compare the union instead.
    """

    if element_result is not None:
        return element_review_to_validation_payload(element_result)
    return reviewed_tree_to_validation_payload(reviewed_tree)


def _tracker_totals_payload(snapshot: AIStatisticsSnapshot) -> dict[str, object]:
    if not snapshot.tracker_totals:
        raise AIStatisticsWorkflowError(
            "Agent4 requires Agent2 tracker totals."
        )
    rows = [
        {
            "tracker_type": item.tracker_type,
            "power_counts": {field.name: field.value for field in item.fields},
            "total": item.total,
        }
        for item in snapshot.tracker_totals
    ]
    return {
        "tracker_totals": rows,
        "grand_total": sum(item.total for item in snapshot.tracker_totals),
    }

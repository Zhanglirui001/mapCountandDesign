"""ViewModel for the AI Statistics tab."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ...application.recognition_queue import (
    RecognitionTicket,
    is_queue_busy,
    queue_directory,
)

from ...application.ai_statistics import (
    AIStatisticsActiveTask,
    AIStatisticsStage,
    AIStatisticsStageStatus,
    AIStatisticsProgressStep,
    AIStatisticsWorkflow,
    Agent1Cancelled,
    Agent1NoReadablePagesError,
    Agent1NoRelevantDrawingPagesError,
    Agent2Cancelled,
    Agent3Cancelled,
    ElementRecognitionResult,
    InvRecognitionSettings,
    OverallSiteExtraction,
    PageClassificationResult,
    PanelOrientation,
    ReviewedTree,
    TrackerDefinition,
)
from ...i18n import tr


@dataclass(frozen=True)
class StageStatusState:
    translation_key: str
    fallback: str


@dataclass(frozen=True)
class AIStatisticsViewState:
    recognition_queued: bool = False
    queue_position: int = 0
    pdf_path: Path | None = None
    page_count: int = 0
    current_stage: int = 0
    active_stage: int | None = None
    furthest_stage: int | None = None
    run_enabled: bool = False
    classification_running: bool = False
    element_running: bool = False
    information_recognition_running: bool = False
    progress_completed: int = 0
    progress_total: int = 0
    element_progress_steps: tuple[AIStatisticsProgressStep, ...] = ()
    element_progress_current_step: str = ""
    element_progress_page_number: int | None = None
    element_progress_detail: str = ""
    element_progress_active_tasks: tuple[AIStatisticsActiveTask, ...] = ()
    information_progress_completed: int = 0
    information_progress_total: int = 0
    error_message: str = ""
    page_classification: PageClassificationResult | None = None
    element_recognition_result: ElementRecognitionResult | None = None
    overall_site_extractions: tuple[OverallSiteExtraction, ...] = ()
    overall_site_orientation: str | None = None
    confirm_enabled: tuple[bool, bool, bool] = (False, False, False)
    info_recognition_retry: bool = False
    stage_statuses: tuple[
        StageStatusState,
        StageStatusState,
        StageStatusState,
        StageStatusState,
    ] = (
        StageStatusState("ai_statistics.status.waiting", "waiting"),
        StageStatusState("ai_statistics.status.waiting", "waiting"),
        StageStatusState("ai_statistics.status.waiting", "waiting"),
        StageStatusState("ai_statistics.status.waiting", "waiting"),
    )


_STATUS_LABELS: dict[AIStatisticsStageStatus, StageStatusState] = {
    AIStatisticsStageStatus.WAITING: StageStatusState(
        "ai_statistics.status.waiting", "waiting"
    ),
    AIStatisticsStageStatus.RUNNING: StageStatusState(
        "ai_statistics.status.running", "running"
    ),
    AIStatisticsStageStatus.RESULT_READY: StageStatusState(
        "ai_statistics.status.result_ready", "result ready"
    ),
    AIStatisticsStageStatus.NEEDS_CONFIRM: StageStatusState(
        "ai_statistics.status.needs_confirm", "needs confirm"
    ),
    AIStatisticsStageStatus.CONFIRMED: StageStatusState(
        "ai_statistics.status.confirmed", "confirmed"
    ),
    AIStatisticsStageStatus.FAILED: StageStatusState(
        "ai_statistics.status.failed", "failed"
    ),
}


class Agent1ClassificationThread(QThread):
    """Execute rendering and embedding away from the Qt GUI thread."""

    progress = Signal(int, int)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        workflow: AIStatisticsWorkflow,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._workflow = workflow

    def run(self) -> None:
        try:
            result = self._workflow.classify_page_document(
                on_progress=self.progress.emit,
                is_cancelled=self.isInterruptionRequested,
            )
        except Agent1Cancelled:
            self.cancelled.emit()
        except Agent1NoReadablePagesError:
            self.failed.emit(
                tr(
                    "ai_statistics.upload_error.no_readable_pages",
                    "No readable pages were found in the PDF.",
                )
            )
        except Agent1NoRelevantDrawingPagesError:
            self.failed.emit(
                tr(
                    "ai_statistics.agent1.no_relevant_drawing_pages",
                    "No Array Layout or Overall Layout pages were recognized. "
                    "This PDF may not be a drawing for the current project. "
                    "Please upload the correct project drawing PDF.",
                )
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            if self.isInterruptionRequested():
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)


class Agent3RecognitionThread(QThread):
    """Execute Agent3 providers and geometry away from the GUI thread."""

    progress = Signal(int, int)
    step_changed = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        workflow: AIStatisticsWorkflow,
        parent: QObject | None = None,
        admission: tuple[str, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._workflow = workflow
        self._admission = admission

    def run(self) -> None:
        try:
            result = self._workflow.recognize_element_pages(
                on_progress=self.progress.emit,
                on_step=self.step_changed.emit,
                is_cancelled=self.isInterruptionRequested,
                admission=self._admission,
            )
        except Agent3Cancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            if self.isInterruptionRequested():
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)


class Agent2InformationRecognitionThread(QThread):
    """Run MinerU table location and Claude vision off the GUI thread."""

    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    progress = Signal(int, int)

    def __init__(
        self,
        workflow: AIStatisticsWorkflow,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._workflow = workflow

    def run(self) -> None:
        try:
            result = self._workflow.recognize_overall_site_pages(
                on_progress=self.progress.emit,
                is_cancelled=self.isInterruptionRequested,
            )
        except Agent2Cancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            if self.isInterruptionRequested():
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)


class AIStatisticsViewModel(QObject):
    """Expose AI workflow state and background Agent1 commands."""

    state_changed = Signal(object)
    progress_changed = Signal(int, int)
    element_progress_changed = Signal()
    classification_finished = Signal(object)
    element_finished = Signal(object)
    information_recognition_finished = Signal(object)
    information_recognition_failed = Signal(str)
    operation_failed = Signal(str)
    operation_cancelled = Signal()

    def __init__(
        self,
        workflow: AIStatisticsWorkflow | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        # Application-owned workflows isolate Agent3 in a killable process.
        # The warmup flag remains relevant to explicitly injected runtimes.
        self._workflow = workflow or AIStatisticsWorkflow(
            auto_warm_element_runtime=True
        )
        self._agent1_thread: Agent1ClassificationThread | None = None
        self._agent3_thread: Agent3RecognitionThread | None = None
        self._agent2_thread: Agent2InformationRecognitionThread | None = None
        # Agent1/2 use window-local lanes. Memory-heavy Agent3 jobs share one
        # FIFO lane, retaining admission until their worker actually exits.
        self._recognition_directory = queue_directory() / "windows" / uuid4().hex
        self._recognition_ticket = None
        self._pending_stage = None
        self._recognition_stage = None
        self._queue_position = 0
        self.recognition_cancelled = False
        self._queue_timer = QTimer(self)
        self._queue_timer.setInterval(250)
        self._queue_timer.timeout.connect(self._poll_recognition_queue)
        self._state = self._build_state()

    @property
    def state(self) -> AIStatisticsViewState:
        return self._state

    @property
    def workflow(self) -> AIStatisticsWorkflow:
        return self._workflow

    @property
    def is_agent1_running(self) -> bool:
        return (
            self._agent1_thread is not None
            and self._agent1_thread.isRunning()
        )

    @property
    def is_agent2_running(self) -> bool:
        return (
            self._agent2_thread is not None
            and self._agent2_thread.isRunning()
        )

    @property
    def is_ai_running(self) -> bool:
        return (
            self._recognition_ticket is not None
            or self.is_agent1_running
            or self.is_agent2_running
            or self.is_agent3_running
        )

    def clear_pdf(self) -> None:
        if self.is_ai_running:
            raise RuntimeError("Cannot clear PDF while AI is running.")
        self._workflow.clear_pdf()
        self._publish()

    def load_pdf(self, path: str | Path, page_count: int) -> None:
        if self.is_ai_running:
            raise RuntimeError("Cannot load PDF while AI is running.")
        self._workflow.load_pdf(path, page_count)
        self._publish()

    def start_ai_model(self) -> bool:
        return self._request_recognition(self._workflow.snapshot.selected_stage)

    def start_element_recognition(self) -> bool:
        return self._request_recognition(AIStatisticsStage.ELEMENT_RECOGNITION)

    def _request_recognition(self, stage) -> bool:
        if self.is_ai_running:
            return False
        if stage != self._workflow.snapshot.selected_stage:
            raise ValueError("Select the requested recognition stage first")
        if not (self._workflow.is_run_enabled() or self._workflow.is_overall_site_run_enabled()):
            raise ValueError("The selected recognition stage is not ready to run")
        self._recognition_ticket = RecognitionTicket(self._admission_directory(stage))
        self.recognition_cancelled = False
        self._pending_stage = stage
        self._recognition_stage = stage
        self._queue_inputs = self._recognition_inputs()
        try:
            self._poll_recognition_queue(raise_errors=True)
        except Exception:
            self._release_recognition_slot()
            raise
        return True

    def _admission_directory(self, stage):
        if stage == AIStatisticsStage.ELEMENT_RECOGNITION:
            return self._recognition_directory.parent.parent / "agent3"
        return self._recognition_directory

    def _recognition_inputs(self):
        s = self._workflow.snapshot
        return (s.pdf_path, id(s.page_classification), id(s.overall_site_extractions), id(s.element_recognition_result),
                repr(self._workflow.element_recognition_request_metadata))

    def _poll_recognition_queue(self, *, raise_errors=False):
        if self._pending_stage is None:
            return
        stage = self._pending_stage
        try:
            acquired, position = self._recognition_ticket.poll()
            changed = position != self._queue_position
            self._queue_position = position
            if not acquired:
                self._queue_timer.start()
                if changed or not self._state.recognition_queued:
                    self._publish()
                return
            self._queue_timer.stop()
            if self._recognition_inputs() != self._queue_inputs:
                raise ValueError("Recognition inputs changed while queued; submit the stage again")
            if self._workflow.snapshot.selected_stage != stage:
                self._workflow.set_current_stage(stage.index)
            self._pending_stage = None
            if stage == AIStatisticsStage.ELEMENT_RECOGNITION:
                self._start_element_now()
            elif stage == AIStatisticsStage.INFO_RECOGNITION:
                self._start_information_recognition()
            else:
                self._start_classification_now()
        except sqlite3.OperationalError as exc:
            if is_queue_busy(exc) and self._pending_stage is not None:
                # Retain the FIFO ticket and retry when the short-lived DB lock
                # clears. Never stop scheduling or release a still-pending job.
                self._queue_timer.start()
                self._publish()
                return
            self._recognition_queue_failed(stage, exc, raise_errors)
        except Exception as exc:
            self._recognition_queue_failed(stage, exc, raise_errors)

    def _recognition_queue_failed(self, stage, exc, raise_errors):
        self._release_recognition_slot()
        if raise_errors:
            raise exc
        fail = {AIStatisticsStage.PAGE_CLASSIFICATION: self._workflow.fail_page_classification,
                AIStatisticsStage.INFO_RECOGNITION: self._workflow.fail_overall_site_recognition,
                AIStatisticsStage.ELEMENT_RECOGNITION: self._workflow.fail_element_recognition}[stage]
        fail(str(exc))
        self._publish()
        self.operation_failed.emit(str(exc))

    def _release_recognition_slot(self):
        self._queue_timer.stop()
        if self._recognition_ticket is not None:
            self._recognition_ticket.close()
        self._recognition_ticket = None
        self._pending_stage = None
        self._recognition_stage = None
        self._queue_position = 0

    def _start_classification_now(self) -> bool:
        self._workflow.begin_page_classification()
        self._publish()
        thread = Agent1ClassificationThread(self._workflow, self)
        thread.progress.connect(self._on_progress)
        thread.succeeded.connect(self._on_succeeded)
        thread.failed.connect(self._on_failed)
        thread.cancelled.connect(self._on_cancelled)
        thread.finished.connect(self._on_thread_finished)
        self._agent1_thread = thread
        thread.start()
        return True

    def _start_element_now(self) -> bool:
        self._workflow.begin_element_recognition()
        self._publish()
        thread = Agent3RecognitionThread(self._workflow, self,
                                         admission=self._recognition_ticket.worker_admission)
        thread.progress.connect(self._on_element_progress)
        thread.step_changed.connect(self._on_element_step)
        thread.succeeded.connect(self._on_element_succeeded)
        thread.failed.connect(self._on_element_failed)
        thread.cancelled.connect(self._on_element_cancelled)
        thread.finished.connect(self._on_element_thread_finished)
        self._agent3_thread = thread
        thread.start()
        return True

    def _start_information_recognition(self) -> bool:
        self._workflow.begin_overall_site_recognition()
        self._publish()
        thread = Agent2InformationRecognitionThread(self._workflow, self)
        thread.succeeded.connect(self._on_agent2_succeeded)
        thread.failed.connect(self._on_agent2_failed)
        thread.cancelled.connect(self._on_agent2_cancelled)
        thread.progress.connect(self._on_agent2_progress)
        thread.finished.connect(self._on_agent2_thread_finished)
        self._agent2_thread = thread
        thread.start()
        return True

    def cancel_ai_model(self) -> None:
        self.recognition_cancelled = True
        if self._pending_stage is not None:
            self._release_recognition_slot()
            self._publish()
            self.operation_cancelled.emit()
            return
        if self._agent1_thread is not None:
            self._agent1_thread.requestInterruption()
        if self._agent3_thread is not None:
            self._agent3_thread.requestInterruption()
            self._workflow.cancel_element_execution()
            # Reflect cancellation in the UI immediately. Late progress
            # events are ignored because the stage is no longer RUNNING.
            self._workflow.cancel_element_recognition()
            self._publish()
        if self._agent2_thread is not None:
            self._agent2_thread.requestInterruption()
            self._workflow.cancel_overall_site_recognition()
            self._publish()

    def configure_element_recognition(
        self,
        tracker_definitions: tuple[TrackerDefinition, ...],
        default_panel_orientation: PanelOrientation = PanelOrientation.UNKNOWN,
        inv_settings: tuple[InvRecognitionSettings, ...] = (),
        default_offset_snap_step_strings: float = 1.0,
        expected_inv_names: tuple[str, ...] = (),
    ) -> None:
        self._workflow.configure_element_recognition(
            tracker_definitions,
            default_panel_orientation,
            inv_settings,
            default_offset_snap_step_strings,
            expected_inv_names,
        )
        self._publish()

    def update_project_recognition_settings(
        self,
        orientation: PanelOrientation,
        snap_step: float,
    ) -> None:
        self._workflow.update_project_recognition_settings(
            orientation,
            snap_step,
        )
        self._publish()

    def update_inv_recognition_settings(
        self,
        inv_name: str,
        orientation: PanelOrientation | None,
        snap_step: float,
    ) -> None:
        self._workflow.update_inv_recognition_settings(
            inv_name,
            panel_orientation=orientation,
            offset_snap_step_strings=snap_step,
        )
        self._publish()

    def replace_element_review(self, tree: ReviewedTree) -> None:
        self._workflow.replace_element_review(tree)
        self._publish()

    def replace_element_result(self, result: ElementRecognitionResult) -> None:
        self._workflow.replace_element_result(result)
        self._publish()

    def run_ai_model(self) -> PageClassificationResult:
        """Synchronous compatibility entry point for non-GUI callers."""
        if self.is_ai_running:
            raise RuntimeError("A recognition task is already queued or running")
        ticket = RecognitionTicket(self._recognition_directory)
        try:
            if not ticket.poll()[0]:
                raise RuntimeError("Recognition is busy; use start_ai_model() to queue without blocking")
            result = self._workflow.run_page_classification()
            self._publish()
            return result
        finally:
            ticket.close()

    def confirm_current_stage(self) -> int:
        snapshot = self._workflow.confirm_current_stage()
        self._publish()
        return snapshot.selected_stage.index

    def set_current_stage(self, index: int) -> None:
        # Browsing available results does not change the stage held in the
        # queue. Admission restores that stage immediately before execution.
        self._workflow.set_current_stage(index)
        self._publish()

    def replace_overall_site_extractions(self, results) -> None:
        """Replace reviewed Agent2 input, rebuilding definitions and invalidating Agent3."""
        self._workflow.complete_overall_site_recognition(tuple(results))
        self._publish()

    def recover_element_result(self, result: ElementRecognitionResult) -> None:
        """Publish a recovery when Agent3 failed without a result."""
        if self._workflow.snapshot.element_recognition_result is not None:
            raise ValueError("Use replace_element_result for an existing result")
        self._workflow.complete_element_recognition(result)
        self._publish()

    def update_page_classification(
        self,
        page_number: int,
        page_type: str,
    ) -> None:
        self._workflow.update_page_classification(page_number, page_type)
        self._publish()

    def update_overall_site_review(
        self,
        results: tuple[OverallSiteExtraction, ...],
        orientation: str,
    ) -> None:
        self._workflow.update_overall_site_review(results, orientation)
        self._publish()

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        if self._pending_stage is not None:
            self.cancel_ai_model()
        running = [
            thread
            for thread in (
                self._agent1_thread,
                self._agent2_thread,
                self._agent3_thread,
            )
            if thread is not None and thread.isRunning()
        ]
        for thread in running:
            thread.requestInterruption()
        if self._agent3_thread in running:
            self._workflow.cancel_element_execution()
        stopped = all(thread.wait(timeout_ms) for thread in running)
        if stopped:
            self._release_recognition_slot()
        return stopped

    def _on_progress(self, completed: int, total: int) -> None:
        self._workflow.update_page_classification_progress(completed, total)
        self._publish()
        self.progress_changed.emit(completed, total)

    def _on_succeeded(self, result: PageClassificationResult) -> None:
        self._workflow.complete_page_classification(result)
        self._publish()
        self.classification_finished.emit(result)

    def _on_failed(self, message: str) -> None:
        self._workflow.fail_page_classification(message)
        self._publish()
        self.operation_failed.emit(message)

    def _on_cancelled(self) -> None:
        self._workflow.cancel_page_classification()
        self._publish()
        self.operation_cancelled.emit()

    def _on_thread_finished(self) -> None:
        thread = self._agent1_thread
        self._agent1_thread = None
        if thread is not None:
            thread.deleteLater()
        self._release_recognition_slot()
        self._publish()

    def _on_element_progress(self, completed: int, total: int) -> None:
        self._workflow.update_element_recognition_progress(completed, total)
        # Agent3 emits many progress events per page. Rebuilding every stage
        # view here repeatedly decodes large preview pixmaps; refresh only the
        # lightweight state consumed by the progress bar.
        self._state = self._build_state()
        self.element_progress_changed.emit()
        self.progress_changed.emit(completed, total)

    def _on_element_step(self, update: object) -> None:
        self._workflow.update_element_recognition_step(update)
        self._state = self._build_state()
        self.element_progress_changed.emit()

    def _on_element_succeeded(self, result: ElementRecognitionResult) -> None:
        self._workflow.complete_element_recognition(result)
        self._publish()
        self.element_finished.emit(result)

    def _on_element_failed(self, message: str) -> None:
        self._workflow.fail_element_recognition(message)
        self._publish()
        self.operation_failed.emit(message)

    def _on_element_cancelled(self) -> None:
        self._workflow.cancel_element_recognition()
        self._publish()
        self.operation_cancelled.emit()

    def _on_element_thread_finished(self) -> None:
        thread = self._agent3_thread
        self._agent3_thread = None
        if thread is not None:
            thread.deleteLater()
        self._release_recognition_slot()
        self._publish()

    def _on_agent2_succeeded(
        self,
        results: tuple[OverallSiteExtraction, ...],
    ) -> None:
        self._workflow.complete_overall_site_recognition(results)
        self._publish()
        self.information_recognition_finished.emit(results)

    def _on_agent2_progress(self, completed: int, total: int) -> None:
        self._workflow.update_information_recognition_progress(
            completed,
            total,
        )
        self._publish()
        self.progress_changed.emit(completed, total)

    def _on_agent2_failed(self, message: str) -> None:
        self._workflow.fail_overall_site_recognition(message)
        self._publish()
        self.information_recognition_failed.emit(message)

    def _on_agent2_cancelled(self) -> None:
        self._workflow.cancel_overall_site_recognition()
        self._publish()
        self.operation_cancelled.emit()

    def _on_agent2_thread_finished(self) -> None:
        thread = self._agent2_thread
        self._agent2_thread = None
        if thread is not None:
            thread.deleteLater()
        self._release_recognition_slot()
        self._publish()

    def _publish(self) -> None:
        self._state = self._build_state()
        self.state_changed.emit(self._state)

    def _build_state(self) -> AIStatisticsViewState:
        snapshot = self._workflow.snapshot
        element_selected = (
            snapshot.selected_stage == AIStatisticsStage.ELEMENT_RECOGNITION
        )
        return AIStatisticsViewState(
            recognition_queued=self._pending_stage is not None,
            queue_position=self._queue_position,
            pdf_path=snapshot.pdf_path,
            page_count=snapshot.page_count,
            current_stage=snapshot.selected_stage.index,
            active_stage=(
                snapshot.active_stage.index
                if snapshot.active_stage is not None
                else None
            ),
            furthest_stage=(
                snapshot.furthest_stage.index
                if snapshot.furthest_stage is not None
                else None
            ),
            run_enabled=(
                not self.is_ai_running and (
                self._workflow.is_run_enabled()
                or self._workflow.is_overall_site_run_enabled()
                )
            ),
            classification_running=(
                (self._recognition_ticket is not None and self._recognition_stage == AIStatisticsStage.PAGE_CLASSIFICATION) or
                self._workflow.status_for(
                    AIStatisticsStage.PAGE_CLASSIFICATION
                )
                == AIStatisticsStageStatus.RUNNING
            ),
            element_running=(
                (self._recognition_ticket is not None and self._recognition_stage == AIStatisticsStage.ELEMENT_RECOGNITION) or
                self._workflow.status_for(
                    AIStatisticsStage.ELEMENT_RECOGNITION
                )
                == AIStatisticsStageStatus.RUNNING
            ),
            information_recognition_running=(
                (self._recognition_ticket is not None and self._recognition_stage == AIStatisticsStage.INFO_RECOGNITION) or
                self._workflow.status_for(
                    AIStatisticsStage.INFO_RECOGNITION
                )
                == AIStatisticsStageStatus.RUNNING
            ),
            progress_completed=(
                snapshot.element_progress_completed
                if element_selected
                else snapshot.classification_progress_completed
            ),
            progress_total=(
                snapshot.element_progress_total
                if element_selected
                else snapshot.classification_progress_total
            ),
            element_progress_steps=snapshot.element_progress_steps,
            element_progress_current_step=(
                snapshot.element_progress_current_step
            ),
            element_progress_page_number=(
                snapshot.element_progress_page_number
            ),
            element_progress_detail=snapshot.element_progress_detail,
            element_progress_active_tasks=(
                snapshot.element_progress_active_tasks
            ),
            information_progress_completed=(
                snapshot.information_progress_completed
            ),
            information_progress_total=snapshot.information_progress_total,
            error_message=snapshot.error_message,
            page_classification=snapshot.page_classification,
            element_recognition_result=snapshot.element_recognition_result,
            overall_site_extractions=snapshot.overall_site_extractions,
            overall_site_orientation=snapshot.overall_site_orientation,
            confirm_enabled=(
                not self.is_ai_running and self._workflow.is_confirm_enabled(
                    AIStatisticsStage.PAGE_CLASSIFICATION
                ),
                not self.is_ai_running and self._workflow.is_confirm_enabled(
                    AIStatisticsStage.INFO_RECOGNITION
                ),
                not self.is_ai_running and self._workflow.is_confirm_enabled(
                    AIStatisticsStage.ELEMENT_RECOGNITION
                ),
            ),
            info_recognition_retry=(
                snapshot.selected_stage == AIStatisticsStage.INFO_RECOGNITION
                and self._workflow.is_overall_site_retry_run()
            ),
            stage_statuses=tuple(
                _STATUS_LABELS[status]
                for status in snapshot.stage_statuses
            ),
        )
    @property
    def is_agent3_running(self) -> bool:
        return (
            self._agent3_thread is not None
            and self._agent3_thread.isRunning()
        )

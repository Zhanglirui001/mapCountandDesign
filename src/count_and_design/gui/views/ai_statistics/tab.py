"""Container for the AI statistics workflow."""

from __future__ import annotations

from pathlib import Path
from time import monotonic

from PySide6.QtCore import QEventLoop, QThread, QTimer, Signal
from PySide6.QtGui import QCursor, QDragEnterEvent, QDropEvent
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ....application.ai_statistics.models import (
    ValidationInvComparison,
    ValidationNodeComparison,
)
from ....i18n import TranslationManager, tr
from ...viewmodels.ai_statistics import AIStatisticsViewModel
from .dialogs import (
    UploadFailedDialog,
    UploadProgressDialog,
    UploadReadyDialog,
)
from .pages import (
    ElementRecognitionPage,
    InfoRecognitionPage,
    PageClassificationPage,
    ValidationPage,
)
from .helpers.pdf_upload import PdfUploadError, load_pdf_document, validate_pdf_path
from .workflow_bar import WorkflowBar


class AIStatisticsTab(QWidget):
    """Own shared PDF state and coordinate the four workflow stage pages."""

    pdf_loaded = Signal(str)
    stage_changed = Signal(int)
    import_to_count_requested = Signal(dict)
    _FIRST_RUN_STAGE = 0
    _INFO_RECOGNITION_STAGE = 1
    _ELEMENT_RECOGNITION_STAGE = 2
    _UPLOAD_PROGRESS_STEP = 8
    _UPLOAD_TICK_MS = 40

    _STYLE = """
        AIStatisticsTab {
            background: #f5f7fa;
        }
        QFrame#workflowCard, QFrame#panel {
            background: #ffffff;
            border: 1px solid #dfe4ea;
            border-radius: 4px;
        }
        QFrame#stageCard {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 5px;
        }
        QFrame#stageCard[active="true"] {
            background: #f2f6ff;
            border: 1px solid #4676f2;
        }
        QFrame#stageCard[clickable="true"]:hover {
            background: #eef2f6;
            border-color: #94a3b8;
        }
        QLabel#pageTitle {
            color: #243447;
            font-size: 14px;
            font-weight: 600;
        }
        QLabel#sectionTitle, QLabel#panelTitle {
            color: #334155;
            font-weight: 600;
        }
        QLabel#validationSectionTitle {
            color: #243447;
            font-size: 14px;
            font-weight: 600;
        }
        QLabel#validationSubTitle {
            color: #334155;
            font-size: 13px;
            font-weight: 600;
            padding-top: 4px;
        }
        QLabel#muted, QLabel#panelMeta, QLabel#emptyHint {
            color: #94a3b8;
        }
        QLabel#emptyHint {
            font-size: 13px;
            font-weight: 600;
        }
        QLabel#statusBadge {
            color: #94a3b8;
            background: #eef2f6;
            border-radius: 8px;
            padding: 2px 7px;
            font-size: 10px;
        }
        QLabel#statusBadge[status="confirmed"] {
            color: #1f7a45;
            background: #e7f6ed;
        }
        QLabel#statusBadge[status="result_ready"] {
            color: #9a6700;
            background: #fff8e6;
        }
        QLabel#statusBadge[status="needs_confirm"] {
            color: #b45309;
            background: #fff4e5;
        }

        QFrame#panelHeader {
            background: #f8fafc;
            border: none;
            border-bottom: 1px solid #e2e8f0;
        }
        QSplitter#infoRecognitionSplitter::handle {
            background: transparent;
            width: 10px;
        }
        QSplitter#agent3MainSplitter::handle,
        QSplitter#agent3ReviewSplitter::handle {
            background: #eef2f6;
            width: 8px;
            height: 8px;
        }
        QSplitter#agent3MainSplitter::handle:hover,
        QSplitter#agent3ReviewSplitter::handle:hover {
            background: #cbd5e1;
        }
        QWidget#agent3PreviewToolbar {
            background: #f8fafc;
            border-bottom: 1px solid #e2e8f0;
        }
        QPushButton#agent3CompactButton,
        QPushButton#agent3IssueButton {
            min-height: 25px;
            min-width: 0;
            padding: 0 9px;
        }
        QPushButton#agent3CompactButton[attention="true"] {
            color: #9a6700;
            background: #fff8e6;
            border-color: #e7c45c;
        }
        QPushButton#agent3IssueButton {
            color: #b45309;
            background: #fff8e6;
            border-color: #f1d48a;
        }
        QTreeWidget#agent3StructureTree {
            border: 1px solid #dfe4ea;
            background: #ffffff;
        }
        QListWidget#agent3IssueList {
            color: #0f172a;
            background: #ffffff;
            border: 1px solid #dfe4ea;
            selection-color: #0f172a;
            selection-background-color: #dbeafe;
        }
        QListWidget#agent3IssueList::item:selected {
            color: #0f172a;
            background: #dbeafe;
        }
        QScrollArea#agent3PropertyScroll {
            border: none;
            background: #ffffff;
        }
        QLabel#agent3PropertyTitle {
            color: #334155;
            font-size: 13px;
            font-weight: 600;
        }
        QTabWidget#overallPageTabs::pane {
            border: none;
            background: #f8fafc;
        }
        QTabWidget#overallPageTabs QTabBar::tab {
            color: #64748b;
            background: #eef2f6;
            border: 1px solid #dfe4ea;
            padding: 5px 12px;
        }
        QTabWidget#overallPageTabs QTabBar::tab:selected {
            color: #245c64;
            background: #ffffff;
            border-bottom-color: #ffffff;
        }
        QWidget#previewZoomToolbar {
            background: #f8fafc;
            border-bottom: 1px solid #e2e8f0;
        }
        QPushButton#previewZoomButton {
            min-width: 28px;
            max-width: 28px;
            padding: 0;
            font-size: 15px;
            font-weight: 600;
        }
        QPushButton#previewFitButton,
        QPushButton#previewActualButton {
            min-width: 82px;
            padding: 0 8px;
        }
        QScrollArea#agent2ResultTree,
        QWidget#recognitionResultsContent {
            border: none;
            background: #f8fafc;
        }
        QFrame#recognitionCard {
            background: #ffffff;
            border: 1px solid #dfe4ea;
            border-radius: 6px;
        }
        QLabel#recognitionCardTitle {
            color: #243447;
            font-weight: 600;
        }
        QPushButton#trackerEditButton,
        QPushButton#trackerAddRowButton,
        QPushButton#trackerAddColumnButton,
        QPushButton#trackerSaveButton,
        QPushButton#trackerCancelButton {
            min-height: 24px;
            padding: 0 10px;
        }
        QPushButton#trackerSaveButton {
            color: #ffffff;
            background: #24736f;
            border-color: #24736f;
        }
        QPushButton#trackerSaveButton:hover {
            background: #1f6663;
        }
        QPushButton#trackerAddRowButton,
        QPushButton#trackerAddColumnButton {
            color: #2563eb;
            background: #eff6ff;
            border: 1px solid #93c5fd;
            border-radius: 11px;
            min-width: 22px;
            max-width: 22px;
            min-height: 22px;
            max-height: 22px;
            padding: 0;
            font-size: 14px;
        }
        QPushButton#trackerAddRowButton:hover,
        QPushButton#trackerAddColumnButton:hover {
            color: #ffffff;
            background: #2563eb;
        }
        QPushButton#trackerDeleteRowButton,
        QPushButton#trackerDeleteColumnButton {
            color: #b42318;
            background: #fff5f4;
            border: 1px solid #f5b7b1;
            border-radius: 11px;
            min-width: 22px;
            max-width: 22px;
            min-height: 22px;
            max-height: 22px;
            padding: 0;
            font-size: 14px;
        }
        QPushButton#trackerDeleteRowButton:hover,
        QPushButton#trackerDeleteColumnButton:hover {
            color: #ffffff;
            background: #b42318;
        }
        QLabel#reviewBadge {
            color: #a16207;
            background: #fff7d6;
            border-radius: 8px;
            padding: 2px 7px;
            font-size: 10px;
        }
        QFrame#agent2OrientationPanel {
            background: #ffffff;
            border: 1px solid #d7e2e8;
            border-left: 3px solid #2f7b78;
            border-radius: 8px;
        }
        QLabel#agent2OrientationTitle {
            color: #243447;
            font-weight: 600;
            font-size: 12px;
        }
        QLabel#agent2OrientationHelp {
            color: #718096;
            font-size: 10px;
        }
        QComboBox#agent2OrientationCombo {
            min-height: 36px;
            color: #334155;
            background: #ffffff;
            border: 1px solid #b9c9d3;
            border-radius: 6px;
            padding: 0 10px;
            font-size: 11px;
        }
        QComboBox#agent2OrientationCombo:hover {
            border-color: #5b918f;
        }
        QComboBox#agent2OrientationCombo:focus {
            border: 1px solid #2f7b78;
        }
        QComboBox#agent2OrientationCombo QAbstractItemView {
            color: #334155;
            background: #ffffff;
            border: 1px solid #b9c9d3;
            selection-color: #204f50;
            selection-background-color: #e7f3f2;
            padding: 4px;
        }
        QTableWidget#trackerOcrTable,
        QTableWidget#invEditTable {
            color: #334155;
            background: #ffffff;
            alternate-background-color: #f8fafc;
            border: 1px solid #dbe3eb;
            gridline-color: #dbe3eb;
            selection-background-color: transparent;
        }
        QTableWidget#trackerOcrTable::item,
        QTableWidget#invEditTable::item {
            padding: 4px 6px;
            border: none;
        }
        QTableWidget#trackerOcrTable QHeaderView::section,
        QTableWidget#invEditTable QHeaderView::section {
            color: #526273;
            background: #eaf0f5;
            border: none;
            border-right: 1px solid #dbe3eb;
            border-bottom: 1px solid #dbe3eb;
            padding: 5px 6px;
            font-size: 11px;
            font-weight: 600;
        }
        QLabel#resultDetailLabel, QLabel#resultEmpty {
            color: #7b8794;
        }
        QLabel#resultChip {
            color: #246b61;
            background: #e9f7f3;
            border: 1px solid #c5e8df;
            border-radius: 9px;
            padding: 2px 7px;
        }
        QLabel#invItem {
            color: #334155;
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 3px;
            padding: 6px 8px;
        }
        QLabel#warningItem {
            color: #9a6700;
            background: #fff9e8;
            padding: 5px;
        }
        QLabel#resultPageLabel {
            color: #64748b;
            font-weight: 600;
            padding: 2px 1px;
        }
        QPushButton {
            min-height: 25px;
            padding: 0 12px;
            border: 1px solid #d9e0e7;
            border-radius: 3px;
            background: #ffffff;
            color: #334155;
        }
        QPushButton:hover {
            background: #f8fafc;
            border-color: #b8c4d0;
        }
        QPushButton:disabled {
            color: #aeb8c4;
            background: #edf1f5;
            border-color: #e2e8f0;
        }
        QPushButton#confirmButton {
            min-width: 160px;
            background: #1f6f78;
            color: #ffffff;
            border: 1px solid #1a5c64;
        }
        QPushButton#confirmButton:hover {
            background: #26828c;
            border-color: #1a5c64;
        }
        QPushButton#confirmButton:disabled {
            color: #d7e4e6;
            background: #8aa9ad;
            border-color: #7a989c;
        }
        QPushButton#validationFilterButton {
            min-height: 23px;
            padding: 0 14px;
            border: 1px solid #d9e0e7;
            border-radius: 4px;
            background: #ffffff;
            color: #64748b;
            font-size: 12px;
            font-weight: 400;
        }
        QPushButton#validationFilterButton:hover {
            color: #334155;
            background: #f8fafc;
            border-color: #b8c4d0;
        }
        QPushButton#validationFilterButton:checked {
            color: #334155;
            background: #f2f6ff;
            border-color: #4676f2;
            font-weight: 400;
        }
        QPushButton#validationFilterButton[filterState="mismatch"] {
            color: #dc2626;
        }
        QPushButton#validationFilterButton[filterState="mismatch"]:checked {
            color: #dc2626;
            background: #fee2e2;
            border-color: #fecaca;
            font-weight: 400;
        }
        QPushButton#validationFilterButton[filterState="passed"] {
            color: #047857;
        }
        QPushButton#validationFilterButton[filterState="passed"]:checked {
            color: #047857;
            background: #ecfdf3;
            border-color: #bbf7d0;
            font-weight: 400;
        }
        QLineEdit#validationInvSearch {
            min-height: 25px;
            min-width: 210px;
            max-width: 260px;
            padding: 0 8px;
            border: 1px solid #d9e0e7;
            border-radius: 3px;
            background: #ffffff;
            color: #334155;
        }
        QLineEdit#validationInvSearch:focus {
            border-color: #4676f2;
            background: #ffffff;
        }
        QPushButton#validationDownloadButton {
            min-width: 88px;
            min-height: 25px;
            background: #4676f2;
            color: #ffffff;
            border: 1px solid #4676f2;
            border-radius: 3px;
            font-weight: 600;
        }
        QPushButton#validationDownloadButton:hover {
            background: #335fd7;
            border-color: #335fd7;
        }
        QPushButton#runAiModelButton {
            min-width: 98px;
            min-height: 25px;
            background: #4676f2;
            color: #ffffff;
            border: 1px solid #4676f2;
            border-radius: 3px;
            font-weight: 600;
        }
        QPushButton#runAiModelButton:hover {
            background: #335fd7;
            border-color: #335fd7;
        }
        QPushButton#runAiModelButton:disabled {
            background: #eef2f6;
            color: #94a3b8;
            border-color: #d9e0e7;
            font-weight: 400;
        }
        QPushButton#validationImportButton {
            min-width: 112px;
            min-height: 25px;
            background: #047857;
            color: #ffffff;
            border: 1px solid #047857;
            border-radius: 3px;
            font-weight: 600;
        }
        QPushButton#validationImportButton:hover {
            background: #065f46;
            border-color: #065f46;
        }
        QTableWidget {
            background: #ffffff;
            alternate-background-color: #f8fafc;
            border: 1px solid #dfe4ea;
            border-radius: 3px;
            color: #0f172a;
            gridline-color: #e2e8f0;
            selection-background-color: #dbeafe;
            selection-color: #0f172a;
        }
        QHeaderView::section {
            background: #eef2f6;
            color: #0f172a;
            border: 0;
            border-right: 1px solid #dfe4ea;
            border-bottom: 1px solid #dfe4ea;
            padding: 4px 8px;
            font-weight: 600;
        }
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        view_model: AIStatisticsViewModel | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("AIStatisticsTab")
        self.setAcceptDrops(True)
        self.setStyleSheet(self._STYLE)

        self._pdf_document = QPdfDocument(self)
        self._view_model = view_model or AIStatisticsViewModel(parent=self)
        self._view_model.state_changed.connect(self._render_view_state)
        self._view_model.element_progress_changed.connect(
            self._update_agent3_progress_bar
        )
        self._view_model.operation_failed.connect(self._show_agent1_error)
        self._view_model.information_recognition_failed.connect(
            self._show_agent2_error
        )
        self._pdf_path = self._view_model.state.pdf_path
        self._stage_ai_completed = [False] * 4
        self._furthest_stage = self._view_model.state.furthest_stage
        self._upload_in_progress = False
        self._upload_cancelled = False
        self._upload_progress = 0
        self._pending_upload_path: Path | None = None
        self._last_upload_success = False
        self._upload_dialog: UploadProgressDialog | None = None
        self._upload_timer = QTimer(self)
        self._upload_timer.timeout.connect(self._advance_upload_progress)
        self._agent3_elapsed_timer = QTimer(self)
        self._agent3_elapsed_timer.setInterval(1000)
        self._agent3_elapsed_timer.timeout.connect(
            self._update_agent3_progress_bar
        )
        self._agent3_started_at: float | None = None
        self._setup_ui()
        self._install_compatibility_aliases()
        TranslationManager.instance().language_changed.connect(
            lambda _locale: self._retranslate()
        )
        self._retranslate()

    @property
    def pdf_path(self) -> Path | None:
        return self._view_model.state.pdf_path

    @property
    def current_stage(self) -> int:
        return self._stage_stack.currentIndex()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 16)
        root.setSpacing(12)

        toolbar = QHBoxLayout()
        self._page_title = QLabel()
        self._page_title.setObjectName("pageTitle")
        toolbar.addWidget(self._page_title)
        toolbar.addStretch()
        self._upload_button = QPushButton()
        self._upload_button.clicked.connect(self._choose_pdf)
        toolbar.addWidget(self._upload_button)
        self._run_button = QPushButton()
        self._run_button.setObjectName("runAiModelButton")
        self._run_button.setEnabled(False)
        self._run_button.clicked.connect(self._run_ai_model)
        toolbar.addWidget(self._run_button)
        self._agent1_progress = QProgressBar()
        self._agent1_progress.setFixedWidth(150)
        self._agent1_progress.setTextVisible(True)
        self._agent1_progress.hide()
        toolbar.addWidget(self._agent1_progress)
        self._agent2_progress = QProgressBar()
        self._agent2_progress.setObjectName("agent2Progress")
        self._agent2_progress.setFixedWidth(150)
        self._agent2_progress.setTextVisible(True)
        self._agent2_progress.setFormat("Agent2 %v/%m")
        self._agent2_progress.hide()
        toolbar.addWidget(self._agent2_progress)
        self._agent3_progress = QProgressBar()
        self._agent3_progress.setMinimumWidth(320)
        self._agent3_progress.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self._agent3_progress.setTextVisible(True)
        self._agent3_progress.hide()
        self._cancel_ai_button = QPushButton()
        self._cancel_ai_button.clicked.connect(
            self._view_model.cancel_ai_model
        )
        self._cancel_ai_button.hide()
        toolbar.addWidget(self._cancel_ai_button)
        root.addLayout(toolbar)
        # Keep progress on its own row so the action buttons can never cover
        # or squeeze Agent3's detailed status text.
        root.addWidget(self._agent3_progress)

        self._workflow_bar = WorkflowBar()
        self._workflow_bar.stage_clicked.connect(self._on_workflow_stage_clicked)
        root.addWidget(self._workflow_bar)

        self.page_classification_page = PageClassificationPage(self._pdf_document)
        self.page_classification_page.classification_changed.connect(
            self._on_page_classification_changed
        )
        self.info_recognition_page = InfoRecognitionPage()
        self.info_recognition_page.review_requirement_changed.connect(
            self._update_stage_actions
        )
        self.element_recognition_page = ElementRecognitionPage()
        self.element_recognition_page.review_changed.connect(
            self._view_model.replace_element_review
        )
        self.element_recognition_page.table_review_changed.connect(
            self._view_model.replace_element_result
        )
        self.element_recognition_page.inv_settings_changed.connect(
            self._view_model.update_inv_recognition_settings
        )
        self.element_recognition_page.project_settings_changed.connect(
            self._view_model.update_project_recognition_settings
        )
        self.validation_page = ValidationPage()
        self.validation_page.import_to_count_requested.connect(
            self.import_to_count_requested.emit
        )
        self.validation_page.inv_activated.connect(self._open_inv_in_agent3)
        self.validation_page.node_activated.connect(self._open_node_in_agent3)
        self.pages = (
            self.page_classification_page,
            self.info_recognition_page,
            self.element_recognition_page,
            self.validation_page,
        )

        self._stage_stack = QStackedWidget()
        for page in self.pages:
            self._stage_stack.addWidget(page)
            if hasattr(page, "confirm_clicked"):
                page.confirm_clicked.connect(self._confirm_current_stage)
        root.addWidget(self._stage_stack, 1)
        self._update_stage_actions()

    def _update_stage_actions(self) -> None:
        """Enable Run/Confirm based on PDF load and the active workflow page."""
        if self._upload_in_progress:
            self._run_button.setEnabled(False)
            self._upload_button.setEnabled(False)
            for page in self.pages[:3]:
                page.confirm_button.setEnabled(False)
            return

        state = self._view_model.state
        ai_running = (
            state.classification_running
            or state.information_recognition_running
            or state.element_running
        )
        self._upload_button.setEnabled(not ai_running)
        self._run_button.setEnabled(state.run_enabled)
        if (
            state.current_stage == self._INFO_RECOGNITION_STAGE
            and state.info_recognition_retry
        ):
            self._run_button.setText(
                tr("ai_statistics.retry_button", "Retry AI Model")
            )
        else:
            self._run_button.setText(
                tr("ai_statistics.run_button", "Run AI Model")
            )
        self._agent1_progress.setVisible(
            state.classification_running
        )
        self._agent2_progress.setVisible(
            state.information_recognition_running
        )
        self._agent3_progress.setVisible(state.element_running)
        self._cancel_ai_button.setVisible(ai_running)
        if state.element_running:
            if self._agent3_started_at is None:
                self._agent3_started_at = monotonic()
                self._agent3_elapsed_timer.start()
            self._update_agent3_progress_bar()
        else:
            self._agent3_elapsed_timer.stop()
            self._agent3_started_at = None
            self._agent3_progress.setToolTip("")
        if state.classification_running:
            self._agent1_progress.setFormat("Agent1 %v/%m")
            total = max(1, state.progress_total)
            self._agent1_progress.setRange(0, total)
            self._agent1_progress.setValue(state.progress_completed)
        if state.information_recognition_running:
            total = max(1, state.information_progress_total)
            self._agent2_progress.setRange(0, total)
            self._agent2_progress.setValue(
                state.information_progress_completed
            )
        for index, page in enumerate(self.pages[:3]):
            enabled = state.confirm_enabled[index]
            if index == self._INFO_RECOGNITION_STAGE:
                enabled = enabled and (
                    not state.overall_site_extractions
                    or self.info_recognition_page.review_ready
                )
            page.confirm_button.setEnabled(enabled)

    def _update_agent3_progress_bar(self) -> None:
        state = self._view_model.state
        if not state.element_running:
            return
        labels = {
            "rendering": tr(
                "ai_statistics.agent3_progress.rendering",
                "Render",
            ),
            "preparing": tr(
                "ai_statistics.agent3_progress.preparing",
                "Prepare",
            ),
            "table_recognition": tr(
                "ai_statistics.agent3_progress.table_recognition",
                "Table Recognition",
            ),
            "tracker_detection": tr(
                "ai_statistics.agent3_progress.tracker_detection",
                "Node YOLO",
            ),
            "box_detection": tr(
                "ai_statistics.agent3_progress.box_detection",
                "Box YOLO",
            ),
            "qwen_node_names": tr(
                "ai_statistics.agent3_progress.qwen_node_names",
                "Node names",
            ),
            "claude_node_review": tr(
                "ai_statistics.agent3_progress.claude_node_review",
                "Node review",
            ),
            "geometry": tr(
                "ai_statistics.agent3_progress.geometry",
                "Geometry",
            ),
            "finalizing": tr(
                "ai_statistics.agent3_progress.finalizing",
                "Finalize",
            ),
        }
        if state.element_progress_steps:
            completed = sum(
                step.completed for step in state.element_progress_steps
            )
            total = sum(step.total for step in state.element_progress_steps)
        else:
            completed = state.progress_completed
            total = state.progress_total
        total = max(1, total)
        active_keys = {
            step.key for step in state.element_progress_steps if step.active > 0
        }
        display_priority = (
            "table_recognition",
            "qwen_node_names",
            "claude_node_review",
            "tracker_detection",
            "box_detection",
            "rendering",
            "preparing",
            "geometry",
            "finalizing",
        )
        active_labels = [
            labels[key] for key in display_priority if key in active_keys
        ]
        if active_labels:
            step_label = "+".join(active_labels[:2])
            if len(active_labels) > 2:
                step_label += f"+{len(active_labels) - 2}"
        else:
            step_label = labels.get(
                state.element_progress_current_step,
                tr("ai_statistics.agent3_progress.starting", "Starting"),
            )
        elapsed = self._format_elapsed(
            monotonic() - self._agent3_started_at
            if self._agent3_started_at is not None
            else 0
        )
        elapsed_text = tr(
            "ai_statistics.agent3_progress.elapsed",
            "Time {elapsed}",
        ).format(elapsed=elapsed)
        self._agent3_progress.setRange(0, total)
        self._agent3_progress.setValue(min(completed, total))
        self._agent3_progress.setFormat(
            f"Agent3 · {step_label} · {completed}/{total} · {elapsed_text}"
        )
        active_tasks = sorted(
            state.element_progress_active_tasks,
            key=lambda task: (
                task.worker_name,
                task.page_number if task.page_number is not None else 0,
                task.step,
            ),
        )
        tooltip_lines = [
            tr(
                "ai_statistics.agent3_progress.tooltip_title",
                "Currently processing",
            )
        ]
        for index, task in enumerate(active_tasks, start=1):
            page_text = (
                tr(
                    "ai_statistics.agent3_progress.page",
                    "Page {page}",
                ).format(page=task.page_number)
                if task.page_number is not None
                else tr(
                    "ai_statistics.agent3_progress.overall",
                    "Overall task",
                )
            )
            line = tr(
                "ai_statistics.agent3_progress.tooltip_task",
                "Thread {index}: {step} · {page}",
            ).format(
                index=index,
                step=labels.get(task.step, task.step),
                page=page_text,
            )
            if task.detail:
                line += f" · {task.detail}"
            tooltip_lines.append(line)
        if not active_tasks:
            tooltip_lines.append(
                labels.get(
                    state.element_progress_current_step,
                    tr(
                        "ai_statistics.agent3_progress.starting",
                        "Starting",
                    ),
                )
            )
        tooltip_text = "\n".join(tooltip_lines)
        self._agent3_progress.setToolTip(tooltip_text)
        # Updating QWidget.toolTip() alone does not repaint an already-open
        # native tooltip. Re-show it while the pointer remains over the bar so
        # every Agent3 progress event is reflected immediately.
        if self._agent3_progress.underMouse():
            QToolTip.showText(
                QCursor.pos(),
                tooltip_text,
                self._agent3_progress,
                self._agent3_progress.rect(),
            )

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _is_confirm_enabled(
        self,
        index: int,
        stage: int,
        has_pdf: bool,
    ) -> bool:
        if not has_pdf or index != stage:
            return False
        if index == self._FIRST_RUN_STAGE:
            return self._stage_ai_completed[index]
        return True

    def _install_compatibility_aliases(self) -> None:
        """Keep the original phase-one attributes stable for callers/tests."""
        self._canvas_stack = self.page_classification_page.canvas_stack
        self._canvas_meta = self.page_classification_page.canvas_meta
        self._stage_titles = self._workflow_bar.stage_titles
        self._stage_badges = self._workflow_bar.stage_badges

    def set_current_stage(self, index: int) -> None:
        """Show one of the four workflow pages."""
        if not 0 <= index < len(self.pages):
            raise ValueError(f"Invalid workflow stage index: {index}")
        previous = self.current_stage
        self._view_model.set_current_stage(index)
        self._render_view_state()
        if index != previous:
            self.stage_changed.emit(index)
        state = self._view_model.state
        if (
            index == self._ELEMENT_RECOGNITION_STAGE
            and state.run_enabled
            and state.element_recognition_result is None
        ):
            self._run_ai_model()

    def _sync_workflow_navigation(self) -> None:
        self._workflow_bar.set_clickable_through(
            self._view_model.state.furthest_stage
        )

    def _on_workflow_stage_clicked(self, index: int) -> None:
        furthest_stage = self._view_model.state.furthest_stage
        if furthest_stage is None or index > furthest_stage:
            return
        self.set_current_stage(index)

    def _open_inv_in_agent3(self, comparison: ValidationInvComparison) -> None:
        """Return to Agent3 showing the INV page Agent4 compared."""

        self.set_current_stage(self._ELEMENT_RECOGNITION_STAGE)
        self.element_recognition_page.reveal_inv(
            comparison.inv_name,
            comparison.page,
            comparison.recognition_id,
        )

    def _open_node_in_agent3(self, comparison: ValidationNodeComparison) -> None:
        """Return to Agent3 selecting the Node Agent4 compared.

        A table-only comparison row has no structure Node; its page number
        still opens the drawing that produced the unmatched table row.
        """

        self.set_current_stage(self._ELEMENT_RECOGNITION_STAGE)
        self.element_recognition_page.reveal_node(
            comparison.inv_name,
            comparison.node_name,
            comparison.recognition_id,
            comparison.source.page_number,
        )

    def set_stage_status(
        self,
        index: int,
        translation_key: str,
        fallback: str,
    ) -> None:
        self._workflow_bar.set_status(index, translation_key, fallback)

    def _render_view_state(self) -> None:
        state = self._view_model.state
        self._pdf_path = state.pdf_path
        self._furthest_stage = state.furthest_stage
        self._stage_ai_completed = [
            status.fallback in {"result ready", "confirmed"}
            for status in state.stage_statuses
        ]
        if self._stage_stack.currentIndex() != state.current_stage:
            self._stage_stack.setCurrentIndex(state.current_stage)
        if state.active_stage is None:
            self._workflow_bar.clear_current_stage()
        else:
            self._workflow_bar.set_current_stage(state.active_stage)
        self._sync_workflow_navigation()
        for index, status in enumerate(state.stage_statuses):
            self.set_stage_status(index, status.translation_key, status.fallback)
        if state.page_classification is not None:
            self.page_classification_page.show_result(
                state.page_classification,
                state.pdf_path,
            )
            # High-resolution PNGs belong on the Agent2 worker. The GUI
            # preview uses Agent1 thumbnails while recognition is running so
            # the first transition does not decode 6000px pixmaps.
            overall_images = self._overall_preview_images(state)
            self.info_recognition_page.show_results(
                overall_images,
                state.overall_site_extractions,
                running=state.information_recognition_running,
                status=state.stage_statuses[1].fallback,
                orientation=state.overall_site_orientation,
            )
        else:
            self.info_recognition_page.clear_results()
        # Metadata-only request: evaluating the full request here would render
        # every Array Layout page at high resolution on the GUI thread.
        self.element_recognition_page.set_request(
            self._view_model.workflow.element_recognition_request_metadata
        )
        self.element_recognition_page.set_result(
            state.element_recognition_result,
            (
                state.page_classification.cached_page_images()
                if state.page_classification is not None
                else {}
            ),
        )
        self.validation_page.set_pdf_name(
            state.pdf_path.name if state.pdf_path is not None else None
        )
        self.validation_page.set_report(
            self._view_model.workflow.snapshot.validation_report
        )
        self._update_page_title()
        self._update_stage_actions()

    @staticmethod
    def _overall_preview_images(state) -> dict[int, bytes]:
        classification = state.page_classification
        if classification is None:
            return {}
        if not state.information_recognition_running:
            cached = classification.cached_images_for_type("Overall Layout")
            if cached:
                return cached
        return classification.thumbnail_images_for_type("Overall Layout")

    def _on_page_classification_changed(
        self,
        page_number: int,
        page_type: str,
    ) -> None:
        self._view_model.update_page_classification(page_number, page_type)
        self._render_view_state()

    def _choose_pdf(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            tr("ai_statistics.upload_dialog", "Select Drawing PDF"),
            "",
            tr("ai_statistics.pdf_filter", "PDF Files (*.pdf)"),
        )
        if path:
            self.load_pdf(path)

    def _show_upload_error(self, error: PdfUploadError) -> None:
        dialog = UploadFailedDialog(error, self)
        if dialog.exec() == UploadFailedDialog.DialogCode.Accepted:
            if dialog.retry_requested:
                self._choose_pdf()

    def _show_upload_ready(self, filename: str, page_count: int) -> None:
        dialog = UploadReadyDialog(filename, page_count, self)
        dialog.exec()

    def _open_upload_dialog(self, filename: str) -> None:
        self._close_upload_dialog()
        self._upload_dialog = UploadProgressDialog(filename, self)
        self._upload_dialog.cancelled.connect(self._cancel_upload)
        self._upload_dialog.show()

    def _close_upload_dialog(self) -> None:
        if self._upload_dialog is None:
            return
        dialog = self._upload_dialog
        self._upload_dialog = None
        dialog.cancelled.disconnect(self._cancel_upload)
        dialog.close_without_cancel()
        dialog.deleteLater()

    def _reset_pdf_state(self) -> None:
        self._view_model.clear_pdf()
        self._pdf_document.close()
        self.page_classification_page.show_empty()
        self._render_view_state()

    def _cancel_upload(self) -> None:
        if not self._upload_in_progress:
            return
        self._upload_cancelled = True
        self._upload_timer.stop()
        self._close_upload_dialog()
        self._upload_in_progress = False
        self._pending_upload_path = None
        if self._pdf_path is None:
            self.page_classification_page.show_empty()
        else:
            self.page_classification_page.set_pdf_loaded(self._pdf_path.name)
        self._update_stage_actions()

    def _advance_upload_progress(self) -> None:
        if self._upload_cancelled or self._pending_upload_path is None:
            return
        self._upload_progress = min(
            90,
            self._upload_progress + self._UPLOAD_PROGRESS_STEP,
        )
        if self._upload_dialog is not None:
            self._upload_dialog.set_progress(self._upload_progress)
        if self._upload_progress >= 90:
            self._upload_timer.stop()
            self._complete_upload()

    def _complete_upload(self) -> None:
        if self._upload_cancelled or self._pending_upload_path is None:
            return

        pdf_path = self._pending_upload_path
        self._pdf_document.close()
        error, page_count = load_pdf_document(self._pdf_document, pdf_path)
        if error is not None:
            self._upload_in_progress = False
            self._pending_upload_path = None
            self._last_upload_success = False
            self._close_upload_dialog()
            self._reset_pdf_state()
            self._update_page_title()
            self._update_stage_actions()
            self._show_upload_error(error)
            return

        self._view_model.load_pdf(pdf_path, page_count)
        self.page_classification_page.set_pdf_loaded(pdf_path.name)
        self._render_view_state()
        self._upload_in_progress = False
        self._pending_upload_path = None
        self._last_upload_success = True
        self._close_upload_dialog()
        self._update_page_title()
        self._update_stage_actions()
        self._show_upload_ready(pdf_path.name, page_count)
        self.pdf_loaded.emit(str(pdf_path))

    def _start_upload(self, path: str | Path) -> bool:
        pdf_path = Path(path)
        pre_error = validate_pdf_path(pdf_path)
        if pre_error is not None:
            self._show_upload_error(pre_error)
            return False

        if self._upload_in_progress:
            self._cancel_upload()

        self._reset_pdf_state()
        self._upload_in_progress = True
        self._upload_cancelled = False
        self._upload_progress = 0
        self._pending_upload_path = pdf_path
        self._last_upload_success = False
        self._open_upload_dialog(pdf_path.name)
        self._update_stage_actions()
        self._upload_timer.start(self._UPLOAD_TICK_MS)
        return True

    def load_pdf(self, path: str | Path) -> bool:
        """Load *path* with uploading progress UI."""
        if not self._start_upload(path):
            return False
        # The upload progress is driven by the upload timer, so wait for it
        # while keeping the GUI responsive.  processEvents() returns as soon
        # as the queue is empty even with a deadline, hence the short sleep
        # between pumps; a bare loop would spin the GUI thread at 100% CPU.
        while self._upload_in_progress:
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
            QThread.msleep(5)
        return self._last_upload_success

    def load_pdf_for_tools(self, path: str | Path) -> None:
        """Load through the shared ViewModel without modal upload dialogs."""
        if self._upload_in_progress or self._view_model.is_ai_running:
            raise RuntimeError("An upload or recognition task is already running.")
        pdf_path = Path(path).resolve()
        error = validate_pdf_path(pdf_path)
        if error is not None:
            raise ValueError(str(error))
        document = QPdfDocument(self)
        error, page_count = load_pdf_document(document, pdf_path)
        if error is not None:
            document.deleteLater()
            raise ValueError(str(error))
        document.close()
        document.deleteLater()
        self._pdf_document.close()
        error, _ = load_pdf_document(self._pdf_document, pdf_path)
        if error is not None:
            raise ValueError(str(error))
        self._view_model.load_pdf(pdf_path, page_count)
        self.page_classification_page.set_pdf_loaded(pdf_path.name)
        self._render_view_state()
        self.pdf_loaded.emit(str(pdf_path))

    def _run_ai_model(self) -> None:
        """Run the AI implementation for the selected workflow stage."""
        state = self._view_model.state
        if not state.run_enabled or state.pdf_path is None:
            return
        if state.current_stage == self._FIRST_RUN_STAGE:
            self.page_classification_page.show_pdf(state.pdf_path.name)
        self._view_model.start_ai_model()

    def _show_agent1_error(self, message: str) -> None:
        if getattr(self, "_tool_session_active", False):
            return  # The workflow renders the error; the bridge reports it to Codex.
        QMessageBox.warning(
            self,
            tr("ai_statistics.ai_failed", "AI Recognition Failed"),
            message,
        )

    def _show_agent2_error(self, message: str) -> None:
        if getattr(self, "_tool_session_active", False):
            return
        QMessageBox.warning(
            self,
            tr(
                "ai_statistics.agent2_failed",
                "Information Recognition Failed",
            ),
            message,
        )

    def _confirm_current_stage(self) -> None:
        index = self.current_stage
        if index >= len(self.pages) - 1:
            return
        if (
            index == self._INFO_RECOGNITION_STAGE
            and self._view_model.state.overall_site_extractions
        ):
            orientation = self.info_recognition_page.selected_orientation
            if orientation is None:
                return
            self._view_model.update_overall_site_review(
                self.info_recognition_page.edited_extractions(),
                orientation,
            )
        next_stage = self._view_model.confirm_current_stage()
        if next_stage != index:
            self.stage_changed.emit(next_stage)
        if (
            next_stage
            in {
                self._INFO_RECOGNITION_STAGE,
                self._ELEMENT_RECOGNITION_STAGE,
            }
            and self._view_model.state.run_enabled
        ):
            self._run_ai_model()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if len(urls) == 1:
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        path = event.mimeData().urls()[0].toLocalFile()
        if self.load_pdf(path):
            event.acceptProposedAction()

    def _update_page_title(self) -> None:
        state = self._view_model.state
        if state.pdf_path is None:
            suffix = tr("ai_statistics.upload_button", "Upload PDF")
        elif state.active_stage is None:
            suffix = tr("ai_statistics.upload_button", "Upload PDF")
        else:
            key, fallback = WorkflowBar.STAGES[state.current_stage]
            suffix = tr(key, fallback)
        self._page_title.setText(
            f"{tr('tab.ai_statistics', 'AI Statistics')} / {suffix}"
        )

    def _retranslate(self) -> None:
        self._upload_button.setText(
            tr("ai_statistics.upload_button", "Upload PDF")
        )
        self._update_stage_actions()
        self._cancel_ai_button.setText(
            tr("ai_statistics.cancel_ai", "Cancel")
        )
        if self._view_model.state.element_running:
            self._update_agent3_progress_bar()
        self._workflow_bar.retranslate()
        for page in self.pages:
            page.retranslate()
        self._update_page_title()

    def closeEvent(self, event) -> None:
        if self._view_model.shutdown():
            event.accept()
        else:
            event.ignore()

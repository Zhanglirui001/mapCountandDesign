import json
import os
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QPainter, QPdfWriter, QWheelEvent
from PySide6.QtPdf import QPdfDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
)

from conftest import sample_agent2_extraction, sample_agent3_reviewed_tree
from count_and_design.application.ai import BoundingBox
from count_and_design.application.ai_statistics import (
    AIStatisticsStage,
    AIStatisticsStageStatus,
    AIStatisticsWorkflow,
    Agent1Cancelled,
    Agent1ProviderError,
    Agent2Cancelled,
    DrawingPageType,
    ElementDetection,
    ElementPageResult,
    ElementRecognitionResult,
    FakePageClassificationProvider,
    InvOcrRow,
    InvStringCount,
    NodeStringTableRow,
    NumStrDetail,
    OverallSiteExtraction,
    PageClassification,
    PageClassificationResult,
    TrackerFieldValue,
    TrackerOcrFields,
    Agent3ProgressUpdate,
    placeholder_thumbnail_png,
)
from count_and_design.i18n import TranslationManager
from count_and_design.application.ai_statistics.models import (
    AIStatisticsActiveTask,
    AIStatisticsProgressStep,
    InvDraft,
    NodeDraft,
    ReviewedTree,
    TrackerQuantity,
    ValidationInvComparison,
    ValidationNodeComparison,
    ValidationReport,
)
from count_and_design.gui.viewmodels.ai_statistics import AIStatisticsViewModel
from count_and_design.gui.views.ai_statistics.helpers.pdf_upload import (
    PdfUploadError,
    validate_pdf_path,
)
from count_and_design.gui.views.ai_statistics.pages.classification import (
    PageClassificationCard,
    PageClassificationPage,
    PagePreviewDialog,
    ResponsiveCardGrid,
)
from count_and_design.gui.views.ai_statistics.pages.info_recognition import (
    InfoRecognitionPage,
)
from count_and_design.gui.views.ai_statistics.pages.validation import (
    _HORIZONTAL_SCROLL_COLUMN_THRESHOLD,
    _TABLE_MIN_COLUMN_WIDTH,
    ValidationPage,
    default_agent4_export_filename,
)
from count_and_design.gui.views.ai_statistics_tab import AIStatisticsTab
from count_and_design.gui.views.main_window import MainWindow


@pytest.fixture(autouse=True)
def _fast_upload_animation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the timed upload animation that every ``load_pdf`` waits out.

    The animation is a UI affordance nobody asserts on, and waiting it out
    costs roughly half a second per call (12 ticks x 40 ms by default).
    """

    monkeypatch.setattr(AIStatisticsTab, "_UPLOAD_TICK_MS", 0)
    monkeypatch.setattr(AIStatisticsTab, "_UPLOAD_PROGRESS_STEP", 90)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _fake_tab() -> AIStatisticsTab:
    return AIStatisticsTab(
        view_model=AIStatisticsViewModel(
            AIStatisticsWorkflow(FakePageClassificationProvider())
        )
    )


def _wait_for_agent1(tab: AIStatisticsTab) -> None:
    for _attempt in range(200):
        QApplication.processEvents()
        if not tab._view_model.state.classification_running:
            return
        QTest.qWait(5)
    raise AssertionError("Agent1 background task did not finish.")


def _wait_for_agent2(tab: AIStatisticsTab) -> None:
    for _attempt in range(200):
        QApplication.processEvents()
        if not tab._view_model.state.information_recognition_running:
            return
        QTest.qWait(5)
    raise AssertionError("Agent2 background task did not finish.")


def _double_click_row(table: QTableWidget, row: int, column: int = 0) -> None:
    item = table.item(row, column)
    assert item is not None
    position = table.visualItemRect(item).center()
    # The offscreen platform only reports the double-click after the view has
    # already registered the row click.
    QTest.mouseClick(
        table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=position,
    )
    QTest.mouseDClick(
        table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=position,
    )


def test_empty_pdf_is_reported_as_having_no_readable_pages(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "empty.pdf"
    pdf_path.touch()

    assert validate_pdf_path(pdf_path) == PdfUploadError.NO_READABLE_PAGES


def test_agent3_progress_bar_shows_step_progress_and_elapsed_time() -> None:
    _app()
    tab = _fake_tab()
    tab._view_model._state = replace(
        tab._view_model.state,
        element_running=True,
        element_progress_steps=(
            AIStatisticsProgressStep("rendering", 1, 2, active=1),
            AIStatisticsProgressStep("table_recognition", 0, 2, active=1),
            AIStatisticsProgressStep("box_detection", 0, 2, active=1),
            AIStatisticsProgressStep("qwen_node_names", 0, 2),
        ),
        element_progress_current_step="box_detection",
        element_progress_page_number=2,
        element_progress_active_tasks=(
            AIStatisticsActiveTask(
                "agent3-table_0", "table_recognition", 3
            ),
            AIStatisticsActiveTask(
                "agent3-page_0", "box_detection", 2
            ),
            AIStatisticsActiveTask(
                "agent3-page_1", "tracker_detection", 5
            ),
        ),
    )

    tab.resize(900, 600)
    tab.show()
    QApplication.processEvents()
    tab._update_stage_actions()
    QApplication.processEvents()
    tab._agent3_started_at = time.monotonic() - 65
    tab._update_agent3_progress_bar()

    progress_format = tab._agent3_progress.format()
    assert tab._agent3_progress.width() > 700
    assert tab._agent3_progress.geometry().top() > max(
        tab._upload_button.geometry().bottom(),
        tab._run_button.geometry().bottom(),
    )
    assert "Table Recognition" in progress_format
    assert "Box" in progress_format
    assert "1/8" in progress_format
    assert "01:05" in progress_format
    tooltip = tab._agent3_progress.toolTip()
    assert "Thread 1" in tooltip
    assert "Table Recognition" in tooltip
    assert "Page 3" in tooltip
    assert "Thread 2" in tooltip
    assert "Box" in tooltip
    assert "Page 2" in tooltip
    assert "Thread 3" in tooltip
    assert "Node" in tooltip
    assert "Page 5" in tooltip
    assert not hasattr(tab, "_agent3_progress_panel")
    tab.close()


def test_agent3_progress_update_refreshes_visible_tooltip() -> None:
    _app()
    tab = _fake_tab()
    tab._view_model._state = replace(
        tab._view_model.state,
        element_running=True,
        element_progress_steps=(
            AIStatisticsProgressStep("rendering", 0, 2, active=1),
        ),
        element_progress_active_tasks=(
            AIStatisticsActiveTask("agent3-render", "rendering", 1),
        ),
    )

    with (
        patch.object(
            tab._agent3_progress,
            "underMouse",
            return_value=True,
        ),
        patch(
            "count_and_design.gui.views.ai_statistics.tab.QToolTip.showText"
        ) as show_tooltip,
    ):
        tab._update_agent3_progress_bar()
        tab._view_model._state = replace(
            tab._view_model.state,
            element_progress_active_tasks=(
                AIStatisticsActiveTask("agent3-render", "rendering", 2),
            ),
        )
        tab._view_model.element_progress_changed.emit()

    assert show_tooltip.call_count == 2
    assert "Page 1" in show_tooltip.call_args_list[0].args[1]
    assert "Page 2" in show_tooltip.call_args_list[1].args[1]
    tab.close()


def test_agent3_progress_events_do_not_publish_full_view_state() -> None:
    _app()
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    statuses = list(workflow.snapshot.stage_statuses)
    statuses[AIStatisticsStage.ELEMENT_RECOGNITION.index] = (
        AIStatisticsStageStatus.RUNNING
    )
    workflow._snapshot = replace(
        workflow.snapshot,
        selected_stage=AIStatisticsStage.ELEMENT_RECOGNITION,
        active_stage=AIStatisticsStage.ELEMENT_RECOGNITION,
        stage_statuses=tuple(statuses),
        element_progress_total=5,
        element_progress_steps=(
            AIStatisticsProgressStep("rendering", 0, 1),
        ),
    )
    view_model = AIStatisticsViewModel(workflow)
    state_events = []
    element_progress_events = []
    view_model.state_changed.connect(state_events.append)
    view_model.element_progress_changed.connect(
        lambda: element_progress_events.append(view_model.state)
    )

    view_model._on_element_step(
        Agent3ProgressUpdate(
            "rendering",
            "running",
            1,
            worker_name="agent3-render",
        )
    )
    assert view_model.state.element_progress_steps[0].active == 1
    assert len(view_model.state.element_progress_active_tasks) == 1
    assert view_model.state.element_progress_active_tasks[0].page_number == 1
    view_model._on_element_step(
        Agent3ProgressUpdate(
            "rendering",
            "completed",
            1,
            worker_name="agent3-render",
        )
    )
    view_model._on_element_progress(1, 5)

    assert state_events == []
    assert len(element_progress_events) == 3
    assert view_model.state.element_progress_steps[0].completed == 1
    assert view_model.state.element_progress_steps[0].active == 0
    assert view_model.state.element_progress_active_tasks == ()
    assert view_model.state.progress_completed == 1


def test_agent2_preview_reuses_pixmap_widgets_and_destroys_replaced_tabs() -> None:
    _app()
    tab = _fake_tab()
    page = tab.info_recognition_page
    first_png = placeholder_thumbnail_png(320)
    second_png = placeholder_thumbnail_png(240)

    page.show_results({1: first_png}, (), running=True, status="running")
    first_preview = page.preview_tabs.widget(0)
    for _ in range(12):
        page.show_results({1: first_png}, (), running=True, status="running")

    assert page.preview_tabs.count() == 1
    assert page.preview_tabs.widget(0) is first_preview

    page.show_results({1: second_png}, (), running=True, status="running")
    second_preview = page.preview_tabs.widget(0)
    assert second_preview is not first_preview
    assert first_preview.parent() is None

    page.clear_results()
    assert page.preview_tabs.count() == 0
    assert second_preview.parent() is None
    tab.close()


def test_page_classification_show_result_skips_rebuild_for_the_same_result() -> None:
    _app()
    page = PageClassificationPage(QPdfDocument())
    result = PageClassificationResult(
        pages=(
            PageClassification(
                page_number=1,
                page_type=DrawingPageType.OVERALL_LAYOUT.value,
            ),
        ),
        thumbnails={1: placeholder_thumbnail_png()},
    )
    page.show_result(result)
    first_group = page._group_layout.itemAt(0).widget()
    page.show_result(result)

    assert page._group_layout.itemAt(0).widget() is first_group
    page.close()


def _write_pdf(path: Path) -> None:
    writer = QPdfWriter(str(path))
    painter = QPainter(writer)
    painter.drawText(100, 100, "Count and Design")
    painter.end()


def test_ai_statistics_tab_starts_in_empty_state() -> None:
    _app()
    tab = _fake_tab()

    assert tab.pdf_path is None
    assert tab._canvas_stack.currentIndex() == 0
    assert not tab._run_button.isEnabled()
    assert not tab.page_classification_page.confirm_button.isEnabled()
    assert len(tab._stage_titles) == 4
    assert tab.current_stage == 0
    assert tab._workflow_bar.current_stage is None
    assert not tab._workflow_bar.stage_cards[0].property("active")

    tab.close()


def test_ai_statistics_tab_loads_pdf(tmp_path: Path) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = _fake_tab()

    with patch.object(tab, "_show_upload_ready") as show_ready:
        assert tab.load_pdf(pdf_path)
        show_ready.assert_called_once()

    assert tab.pdf_path == pdf_path
    assert (
        tab._canvas_stack.currentIndex()
        == tab.page_classification_page.CANVAS_EMPTY
    )
    assert tab._run_button.isEnabled()
    assert not tab.page_classification_page.confirm_button.isEnabled()
    assert tab._stage_badges[0].text() == "waiting"
    assert tab._workflow_bar.current_stage is None
    assert tab._canvas_meta.text() == "drawing.pdf"

    tab.close()


def test_run_ai_model_enables_confirm(tmp_path: Path) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = _fake_tab()
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)

    QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
    _wait_for_agent1(tab)

    assert tab._stage_badges[0].text() == "result ready"
    assert tab._workflow_bar.current_stage == 0
    assert tab._workflow_bar.stage_cards[0].property("active")
    assert tab._run_button.isEnabled()
    assert tab.page_classification_page.confirm_button.isEnabled()
    assert (
        tab._canvas_stack.currentIndex()
        == tab.page_classification_page.CANVAS_PREVIEW
    )

    tab.close()


def test_confirm_button_advances_workflow_stage(tmp_path: Path) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = _fake_tab()
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)

    QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
    _wait_for_agent1(tab)
    QTest.mouseClick(
        tab.page_classification_page.confirm_button,
        Qt.MouseButton.LeftButton,
    )

    assert tab.current_stage == 1
    assert tab._stage_badges[0].text() == "confirmed"
    assert tab._stage_badges[0].property("status") == "confirmed"
    assert not tab._run_button.isEnabled()
    assert tab.info_recognition_page.confirm_button.isEnabled()

    tab.close()


class OverallPageProvider:
    def classify(self, _pdf_path, _page_count, **_kwargs):
        png = placeholder_thumbnail_png()
        return PageClassificationResult(
            pages=(
                PageClassification(
                    page_number=1,
                    page_type=DrawingPageType.OVERALL_LAYOUT.value,
                ),
            ),
            thumbnails={1: png},
            page_images={1: png},
        )


def _agent2_result() -> OverallSiteExtraction:
    def field(label: str, meaning: str, value: str) -> TrackerFieldValue:
        return TrackerFieldValue(label, meaning, value, f"OCR: {value}")

    return OverallSiteExtraction(
        page_number=1,
        tracker_ocr_fields=(
            TrackerOcrFields(
                module_model=field(
                    "MODULE MODEL",
                    "board brand/model",
                    "ABC-620",
                ),
                module_stc_dc_rating_w=field(
                    "MODULE STC DC RATING (W)",
                    "module wattage",
                    "620W",
                ),
                string_count=field(
                    "STRING COUNT",
                    "total string count",
                    "9767",
                ),
                num_str=field(
                    "Num-STR",
                    "specific string types",
                    "3-STR, 2-STR",
                ),
                num_str_details=(
                    NumStrDetail("3-STR", "2779", "OCR row"),
                    NumStrDetail("2-STR", "715", "OCR row"),
                ),
            ),
        ),
        inv_list=("INV 1101", "INV 1102"),
        inv_rows=(
            InvOcrRow(
                identifier="INV 1101",
                string_counts=(
                    InvStringCount(1, "JINKO 590W MODULE STRINGS", "0"),
                    InvStringCount(2, "JINKO 595W MODULE STRINGS", "353"),
                ),
            ),
            InvOcrRow(
                identifier="INV 1102",
                string_counts=(
                    InvStringCount(1, "JINKO 590W MODULE STRINGS", "116"),
                    InvStringCount(2, "JINKO 595W MODULE STRINGS", "238"),
                ),
            ),
        ),
        inv_count=2,
    )


def test_agent2_ui_runs_and_displays_overall_results(tmp_path: Path) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    recognizer = Mock()
    recognizer.recognize_pages.return_value = (_agent2_result(),)
    tab = AIStatisticsTab(
        view_model=AIStatisticsViewModel(
            AIStatisticsWorkflow(OverallPageProvider(), recognizer)
        )
    )
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)

    QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
    _wait_for_agent1(tab)
    QTest.mouseClick(
        tab.page_classification_page.confirm_button,
        Qt.MouseButton.LeftButton,
    )

    assert tab.current_stage == 1
    _wait_for_agent2(tab)
    QApplication.processEvents()
    assert tab.info_recognition_page.review_title.text() == (
        "Information Recognition"
    )
    assert tab.info_recognition_page.orientation_title.text() == (
        "Panel installation direction"
    )
    assert tab.info_recognition_page.preview_tabs.count() == 1
    assert tab.info_recognition_page.preview_tabs.tabBar().isHidden()
    page_preview = tab.info_recognition_page.preview_tabs.widget(0)
    assert isinstance(page_preview, QScrollArea)
    assert page_preview.verticalScrollBarPolicy() == (
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert page_preview.horizontalScrollBarPolicy() == (
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    fitted_scale = page_preview.scale_factor
    viewport = page_preview.viewport()
    center = viewport.rect().center()
    wheel = QWheelEvent(
        QPointF(center),
        QPointF(viewport.mapToGlobal(center)),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(viewport, wheel)
    assert page_preview.scale_factor > fitted_scale
    assert page_preview.verticalScrollBarPolicy() == (
        Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=center)
    assert viewport.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    QTest.mouseMove(viewport, center + QPoint(-20, -20))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=center)
    assert viewport.cursor().shape() == Qt.CursorShape.OpenHandCursor
    QTest.mouseClick(
        tab.info_recognition_page.fit_button,
        Qt.MouseButton.LeftButton,
    )
    assert page_preview.fit_to_page
    assert page_preview.verticalScrollBarPolicy() == (
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )

    assert tab._stage_badges[1].text() == "needs confirm"
    assert tab._run_button.isEnabled()
    assert not tab.info_recognition_page.confirm_button.isEnabled()
    orientation_combo = tab.info_recognition_page.orientation_combo
    assert orientation_combo.itemText(1) == (
        "Positive to Box  ·  正极朝汇流箱 Box  (P-B)"
    )
    assert orientation_combo.itemText(2) == (
        "Negative to Box  ·  负极朝汇流箱 Box  (N-B)"
    )
    assert orientation_combo.itemText(3) == (
        "Positive to North  ·  正极朝北  (P-N)"
    )
    assert orientation_combo.itemText(4) == (
        "Positive to South  ·  正极朝南  (P-S)"
    )
    assert orientation_combo.itemText(5) == (
        "Positive to East  ·  正极朝东  (P-E)"
    )
    assert orientation_combo.itemText(6) == (
        "Positive to West  ·  正极朝西  (P-W)"
    )
    tab.info_recognition_page.orientation_combo.setCurrentIndex(1)
    assert tab.info_recognition_page.confirm_button.isEnabled()
    tree = tab.info_recognition_page.result_tree
    assert not tree.isHidden()
    cards = tree.findChildren(QFrame, "recognitionCard")
    assert len(cards) == 2
    card_text = [
        label.text()
        for card in cards
        for label in card.findChildren(QLabel)
    ]
    assert any("Tracker OCR Output" in text for text in card_text)
    assert any("INV Tree" in text for text in card_text)
    assert any("2. Tracker OCR Output" in text for text in card_text)
    assert any("3. INV Tree" in text for text in card_text)
    tracker_table = tree.findChild(QTableWidget, "trackerOcrTable")
    assert tracker_table is not None
    assert tracker_table.rowCount() == 5
    assert tracker_table.columnCount() == 3
    assert tracker_table.horizontalHeaderItem(0).text() == "Moudle Model"
    assert tracker_table.horizontalHeaderItem(1).text() == "ABC-620"
    assert tracker_table.horizontalHeaderItem(2).text() == "Total"
    assert tracker_table.item(0, 0).text() == "Module STC DC Rating"
    assert tracker_table.item(0, 1).text() == "620W"
    assert tracker_table.item(0, 2).text() == "/"
    assert tracker_table.item(1, 0).text() == "String Count"
    assert tracker_table.item(1, 1).text() == "9767"
    assert tracker_table.item(2, 0).text() == "3-STR"
    assert tracker_table.item(2, 1).text() == "2779"
    assert tracker_table.item(3, 0).text() == "2-STR"
    assert tracker_table.item(3, 1).text() == "715"
    assert tracker_table.item(4, 0).text() == "Tracker Count"
    assert tracker_table.item(4, 1).text() == "3,494"
    assert tracker_table.item(4, 2).text() == "3,494"
    assert not tracker_table.item(1, 1).flags() & Qt.ItemFlag.ItemIsEditable
    assert not tracker_table.item(4, 1).flags() & Qt.ItemFlag.ItemIsEditable
    edit_button = tree.findChild(QPushButton, "trackerEditButton")
    save_button = tree.findChild(QPushButton, "trackerSaveButton")
    assert edit_button is not None
    assert save_button is not None
    edit_button.click()
    assert tracker_table.item(1, 1).flags() & Qt.ItemFlag.ItemIsEditable
    tracker_table.horizontalHeaderItem(1).setText("EDITED-620")
    assert tracker_table.horizontalHeaderItem(1).text() == "EDITED-620"
    tracker_table.item(2, 1).setText("2800")
    assert tracker_table.item(4, 1).text() == "3,515"
    save_button.click()
    assert not tracker_table.item(1, 1).flags() & Qt.ItemFlag.ItemIsEditable
    inv_table = tree.findChild(QTableWidget, "invEditTable")
    assert inv_table is not None
    assert inv_table.columnCount() == 1
    inv_table.item(0, 0).setText("INV1199")
    edited_inv = tab.info_recognition_page.edited_extractions()[0]
    assert edited_inv.inv_list[0] == "INV1199"
    assert edited_inv.inv_rows[0].string_counts == ()
    assert tree.findChild(QTableWidget, "numStrTable") is None
    tree_text = []
    iterator = tree.invisibleRootItem()

    def collect(item):
        tree_text.extend(item.text(column) for column in range(3))
        for child_index in range(item.childCount()):
            collect(item.child(child_index))

    collect(iterator)
    assert "ABC-620" in tree_text
    assert "9767" in tree_text
    assert "INV 1101" in tree_text
    call = recognizer.recognize_pages.call_args
    assert call.args == ({1: placeholder_thumbnail_png()},)
    assert callable(call.kwargs["on_progress"])
    assert callable(call.kwargs["is_cancelled"])
    assert tab._view_model.state.information_progress_completed == 2
    assert tab._view_model.state.information_progress_total == 2
    assert tab._agent2_progress.format() == "Agent2 %v/%m"
    assert tab._agent2_progress.isHidden()
    QTest.mouseClick(
        tab.info_recognition_page.confirm_button,
        Qt.MouseButton.LeftButton,
    )
    snapshot = tab._view_model.workflow.snapshot
    assert snapshot.overall_site_orientation == "P-B"
    edited = snapshot.overall_site_extractions[0]
    assert edited.tracker_ocr_fields[0].module_model.value == "EDITED-620"
    assert edited.tracker_ocr_fields[0].num_str_details[0].count == "2800"
    assert edited.inv_list[0] == "INV1199"
    tab.close()


class SlowCancellableAgent2Recognizer:
    def recognize_pages(self, _images, *, on_progress=None, is_cancelled=None):
        for _attempt in range(200):
            if is_cancelled is not None and is_cancelled():
                raise Agent2Cancelled("cancelled")
            time.sleep(0.005)
        raise AssertionError("Agent2 recognizer should have been cancelled")


def test_agent2_background_task_can_be_cancelled() -> None:
    _app()
    tab = AIStatisticsTab(
        view_model=AIStatisticsViewModel(
            AIStatisticsWorkflow(
                OverallPageProvider(),
                SlowCancellableAgent2Recognizer(),
            )
        )
    )
    tab._view_model.load_pdf(Path("agent2-cancel-test.pdf"), 1)
    tab.show()

    QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
    _wait_for_agent1(tab)
    QTest.mouseClick(
        tab.page_classification_page.confirm_button,
        Qt.MouseButton.LeftButton,
    )
    QApplication.processEvents()
    assert tab._view_model.state.information_recognition_running
    assert tab._cancel_ai_button.isVisible()

    QTest.mouseClick(tab._cancel_ai_button, Qt.MouseButton.LeftButton)
    _wait_for_agent2(tab)
    QApplication.processEvents()

    assert tab._stage_badges[1].text() == "waiting"
    assert tab._run_button.isEnabled()
    assert not tab._agent2_progress.isVisible()
    tab.close()


def test_agent2_tracker_counts_reject_negative_and_decimal_values() -> None:
    _app()
    page = InfoRecognitionPage()
    page.show_results({}, (_agent2_result(),), status="result ready")
    table = page._tracker_table
    assert table is not None

    error_path = (
        "count_and_design.gui.views.ai_statistics.pages.info_recognition."
        "QMessageBox.critical"
    )
    with patch(error_path) as show_error:
        table.item(2, 1).setText("2,800")
        table.item(2, 1).setText("-12")
        assert table.item(2, 1).text() == "2,800"
        assert table.item(2, 2).text() == "2,800"
        assert table.item(4, 2).text() == "3,515"
        assert "Negative values" in show_error.call_args.args[2]
        assert "Previous value restored" in show_error.call_args.args[2]

        show_error.reset_mock()
        table.item(3, 1).setText("700")
        table.item(3, 1).setText("1.5")
        assert table.item(3, 1).text() == "700"
        assert table.item(3, 2).text() == "700"
        assert table.item(4, 2).text() == "3,500"
        assert "Decimals are not allowed" in show_error.call_args.args[2]
        assert "Previous value restored" in show_error.call_args.args[2]

    details = page.edited_extractions()[0].tracker_ocr_fields[0].num_str_details
    assert [(detail.num_str, detail.count) for detail in details] == [
        ("3-STR", "2,800"),
        ("2-STR", "700"),
    ]
    page.close()


def test_agent2_review_uses_chinese_labels_and_localized_status() -> None:
    _app()
    translations = TranslationManager.instance()
    previous_locale = translations.locale
    try:
        translations.locale = "zh_CN"
        page = InfoRecognitionPage()
        page.show_results({}, (_agent2_result(),), status="needs confirm")

        table = page._tracker_table
        assert table is not None
        assert page.orientation_title.text() == "组件安装方向"
        assert page.review_meta.text() == "待确认"
        assert table.horizontalHeaderItem(0).text() == "Moudle Model"
        assert page._tracker_edit_button is not None
        assert page._tracker_edit_button.text() == "编辑"
        assert page._tracker_save_button is not None
        assert page._tracker_save_button.text() == "保存表格"
        assert page._tracker_cancel_button is not None
        assert page._tracker_cancel_button.text() == "取消"

        table.item(0, 1).setText("777W")
        chinese_inv_table = page._inv_tables[0]
        assert chinese_inv_table is not None
        chinese_inv_table.item(0, 0).setText("INV-EDITED")

        translations.locale = "en_US"
        page.retranslate()
        table = page._tracker_table
        assert table is not None
        assert page.review_meta.text() == "needs confirm"
        assert table.horizontalHeaderItem(0).text() == "Moudle Model"
        assert table.horizontalHeaderItem(table.columnCount() - 1).text() == "Total"
        assert table.item(0, 0).text() == "Module STC DC Rating"
        assert table.item(0, 1).text() == "777W"
        assert table.item(1, 0).text() == "String Count"
        assert table.item(table.rowCount() - 1, 0).text() == "Tracker Count"
        card_titles = [
            label.text()
            for label in page.findChildren(QLabel, "recognitionCardTitle")
        ]
        assert "2. Tracker OCR Output" in card_titles
        assert "3. INV Tree" in card_titles
        inv_table = page._inv_tables[0]
        assert inv_table is not None
        assert inv_table.horizontalHeaderItem(0).text() == "INV Identifier"
        assert inv_table.item(0, 0).text() == "INV-EDITED"
        assert page._tracker_save_button.text() == "Save Table"
        assert page._tracker_cancel_button.text() == "Cancel"
    finally:
        translations.locale = previous_locale
        if "page" in locals():
            page.close()


def test_agent2_tracker_table_preserves_ocr_placeholders_and_edit_actions() -> None:
    _app()

    def field(label: str, value: str) -> TrackerFieldValue:
        return TrackerFieldValue(label, label, value, f"OCR: {value}")

    extraction = OverallSiteExtraction(
        page_number=1,
        tracker_ocr_fields=(
            TrackerOcrFields(
                module_model=field("MODULE MODEL", "BYD"),
                module_stc_dc_rating_w=field("MODULE STC", "650"),
                string_count=field("STRING COUNT", "---"),
                num_str=field("Num-STR", "3-STR, 2-STR"),
                num_str_details=(
                    NumStrDetail("3-STR", "---", "OCR row"),
                    NumStrDetail("2-STR", "-", "OCR row"),
                ),
            ),
        ),
    )
    page = InfoRecognitionPage()
    page.show_results({}, (extraction,), status="result ready")
    page.show()
    QApplication.processEvents()
    table = page._tracker_table
    assert table is not None
    assert table.horizontalHeaderItem(1).text() == "BYD"
    assert table.item(1, 1).text() == "---"
    assert table.item(2, 1).text() == "---"
    assert table.item(3, 1).text() == "-"
    assert table.item(4, 1).text() == "---"

    assert page._tracker_edit_button is not None
    assert page._tracker_add_button is not None
    assert page._tracker_add_column_button is not None
    assert page._tracker_delete_row_button is not None
    assert page._tracker_delete_column_button is not None
    assert page._tracker_save_button is not None
    assert page._tracker_cancel_button is not None
    page._tracker_edit_button.click()
    assert table._editing
    header = table.horizontalHeader()
    field_boundary = (
        header.sectionViewportPosition(0) + header.sectionSize(0)
    )
    table._show_column_insert_control(field_boundary)
    assert not page._tracker_add_column_button.isHidden()
    assert page._tracker_delete_column_button.isHidden()
    brand_center = (
        header.sectionViewportPosition(1) + header.sectionSize(1) // 2
    )
    table._show_column_insert_control(brand_center)
    assert page._tracker_add_column_button.isHidden()
    assert not page._tracker_delete_column_button.isHidden()
    first_tracker_boundary = table.visualRect(
        table.model().index(2, 0)
    ).bottom()
    table._show_row_controls(first_tracker_boundary)
    assert not page._tracker_add_button.isHidden()
    table.horizontalHeaderItem(1).setText("TRINA")
    assert table.horizontalHeaderItem(1).text() == "TRINA"
    page._tracker_add_button.click()
    assert table.rowCount() == 6
    assert table.item(3, 0).text() == "New type"
    new_row_rect = table.visualRect(table.model().index(3, 0))
    table._show_row_controls(new_row_rect.center().y())
    assert not page._tracker_delete_row_button.isHidden()
    page._tracker_delete_row_button.click()
    assert table.rowCount() == 5
    page._tracker_add_column_button.click()
    assert table.columnCount() == 4
    for column in range(table.columnCount() - 1):
        boundary = (
            header.sectionViewportPosition(column) + header.sectionSize(column)
        )
        table._show_column_insert_control(boundary)
        assert not page._tracker_add_column_button.isHidden()
    page._delete_tracker_row(2)
    page._delete_tracker_column(1)
    assert table.rowCount() == 4
    assert table.columnCount() == 3
    page._tracker_cancel_button.click()
    assert table.rowCount() == 5
    assert table.columnCount() == 3
    assert table.horizontalHeaderItem(1).text() == "BYD"
    assert table.item(2, 1).text() == "---"

    page._tracker_edit_button.click()
    table.horizontalHeaderItem(1).setText("TRINA")
    page._tracker_add_button.click()
    table.item(4, 0).setText("1-STR")
    table.item(4, 1).setText("12")
    page._tracker_add_column_button.click()
    table.item(0, 2).setText("335")
    table.horizontalHeaderItem(2).setText("RISEN")
    table.item(1, 2).setText("197")
    table.item(4, 2).setText("4")
    page._delete_tracker_row(3)
    page._delete_tracker_column(1)
    page._tracker_save_button.click()
    assert table.columnWidth(0) >= 150
    assert sum(
        table.columnWidth(column) for column in range(table.columnCount())
    ) >= table.viewport().width()
    assert table.horizontalScrollBarPolicy() == (
        Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    edited_trackers = page.edited_extractions()[0].tracker_ocr_fields
    edited = edited_trackers[0]
    assert edited.module_model.value == "RISEN"
    assert edited.module_stc_dc_rating_w.value == "335"
    assert edited.string_count.value == "197"
    assert [(detail.num_str, detail.count) for detail in edited.num_str_details] == [
        ("3-STR", "---"),
        ("1-STR", "4"),
    ]
    assert len(edited_trackers) == 1
    page.close()


def test_agent2_orientation_stays_at_top_and_survives_result_refresh() -> None:
    _app()
    page = InfoRecognitionPage()
    review_layout = page.orientation_panel.parentWidget().layout()
    assert review_layout.indexOf(page.orientation_panel) == 1
    assert review_layout.indexOf(page.review_hint) > 1

    page.orientation_combo.setCurrentIndex(2)
    selected = page.orientation_combo.currentData()
    page.show_results({}, (), running=True, status="running")
    assert page.orientation_combo.currentData() == selected

    page.show_results(
        {},
        (_agent2_result(),),
        running=False,
        status="result ready",
    )
    assert page.orientation_combo.currentData() == selected
    assert review_layout.indexOf(page.orientation_panel) == 1
    page.close()


def test_agent2_auto_run_failure_can_be_retried(tmp_path: Path) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    recognizer = Mock()
    recognizer.recognize_pages.side_effect = (
        RuntimeError("Qwen unavailable"),
        (_agent2_result(),),
    )
    tab = AIStatisticsTab(
        view_model=AIStatisticsViewModel(
            AIStatisticsWorkflow(OverallPageProvider(), recognizer)
        )
    )
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)

    with patch.object(tab, "_show_agent2_error") as show_error:
        QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
        _wait_for_agent1(tab)
        QTest.mouseClick(
            tab.page_classification_page.confirm_button,
            Qt.MouseButton.LeftButton,
        )
        _wait_for_agent2(tab)
        QApplication.processEvents()

    assert recognizer.recognize_pages.call_count == 1
    assert tab._stage_badges[1].text() == "failed"
    assert tab._run_button.isEnabled()
    show_error.assert_called_once_with("Qwen unavailable")

    QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
    _wait_for_agent2(tab)
    QApplication.processEvents()

    assert recognizer.recognize_pages.call_count == 2
    assert tab._stage_badges[1].text() == "needs confirm"
    assert not tab.info_recognition_page.confirm_button.isEnabled()
    tab.info_recognition_page.orientation_combo.setCurrentIndex(2)
    assert tab.info_recognition_page.confirm_button.isEnabled()
    tab.close()


def test_workflow_bar_navigates_to_previous_stage(tmp_path: Path) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = _fake_tab()
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)

    QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
    _wait_for_agent1(tab)
    QTest.mouseClick(
        tab.page_classification_page.confirm_button,
        Qt.MouseButton.LeftButton,
    )
    assert tab.current_stage == 1

    QTest.mouseClick(
        tab._workflow_bar.stage_cards[0],
        Qt.MouseButton.LeftButton,
    )

    assert tab.current_stage == 0
    assert tab._workflow_bar.stage_cards[0].property("active")
    assert tab.page_classification_page.confirm_button.isEnabled()

    tab.close()


def test_workflow_bar_not_clickable_before_run(tmp_path: Path) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = _fake_tab()
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)

    assert tab._workflow_bar.stage_cards[0].property("clickable") is False
    QTest.mouseClick(
        tab._workflow_bar.stage_cards[0],
        Qt.MouseButton.LeftButton,
    )
    assert tab._workflow_bar.current_stage is None

    tab.close()



def test_default_agent4_export_filename_uses_pdf_name_and_timestamp() -> None:
    fixed_now = datetime(2025, 9, 9, 11, 22, 33)
    assert default_agent4_export_filename(
        "drawing.pdf",
        now=fixed_now,
    ) == "drawing_20250909_112233.json"
    assert default_agent4_export_filename(
        None,
        now=fixed_now,
    ) == "agent4-final_20250909_112233.json"


def test_validation_page_renders_agent4_data(tmp_path: Path) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = _fake_tab()
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)

    QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
    _wait_for_agent1(tab)
    QTest.mouseClick(
        tab.page_classification_page.confirm_button,
        Qt.MouseButton.LeftButton,
    )
    tab._view_model.workflow.complete_overall_site_recognition(
        (sample_agent2_extraction(),)
    )
    tab._view_model._publish()
    tab._view_model.confirm_current_stage()
    tab._view_model.workflow.complete_element_recognition(
        ElementRecognitionResult(reviewed_tree=sample_agent3_reviewed_tree())
    )
    tab._view_model._publish()
    QTest.mouseClick(
        tab.element_recognition_page.confirm_button,
        Qt.MouseButton.LeftButton,
    )

    page = tab.validation_page
    report = tab._view_model.workflow.snapshot.validation_report

    def column_index(table, label: str) -> int:
        for column in range(table.columnCount()):
            if table.horizontalHeaderItem(column).text() == label:
                return column
        raise AssertionError(f"Missing table column: {label}")

    def assert_stretch_columns(table) -> None:
        header = table.horizontalHeader()
        for column in range(table.columnCount()):
            assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Stretch

    assert report is not None
    assert tab.current_stage == 3
    assert tab._stage_badges[3].text() == "result ready"
    assert page._ocr_table.rowCount() == len(report.tracker_totals)
    assert page._ocr_table.item(0, 1).text() == report.tracker_totals[0].fields[0].value
    ocr_total_item = page._ocr_table.item(0, page._ocr_table.columnCount() - 1)
    assert not ocr_total_item.font().bold()
    assert ocr_total_item.foreground().color().name() == "#1257f5"
    assert page._inv_table.rowCount() == len(report.inv_comparisons)
    assert page._tracker_table.rowCount() == len(report.tracker_comparisons)
    inv_headers = [
        page._inv_table.horizontalHeaderItem(column).text()
        for column in range(page._inv_table.columnCount())
    ]
    node_headers = [
        page._node_table.horizontalHeaderItem(column).text()
        for column in range(page._node_table.columnCount())
    ]
    assert "9-string total" in inv_headers
    assert "13-string total" in inv_headers
    assert "8-string total" not in inv_headers
    assert "8-string total" not in node_headers
    assert page._inv_table.item(0, 0).text() == "INV1"
    assert page._inv_table.item(0, column_index(page._inv_table, "Page")).text() == "P12"
    assert page._inv_table.item(0, column_index(page._inv_table, "Total String")).text() == "125"
    inv_diff_item = page._inv_table.item(
        0,
        column_index(page._inv_table, "Diff"),
    )
    assert inv_diff_item.text() == "+3"
    assert not inv_diff_item.font().bold()
    assert page._node_table.rowCount() == len(report.inv_comparisons[0].nodes)
    assert page._node_table.item(0, column_index(page._node_table, "Total String")).text() == "71"
    assert page._node_table.item(0, column_index(page._node_table, "Diff")).text() == "+3"
    assert page._filter_buttons["all"].isChecked()
    assert page._filter_buttons["mismatch"].text().startswith("\u2022 ")
    assert page._filter_buttons["passed"].text().startswith("\u2022 ")
    assert page._download_button.text() == "Download"
    assert page._download_button.isEnabled()
    assert page._import_button.text() == "Import to Count"
    assert page._import_button.isEnabled()
    assert page._inv_search.placeholderText() == "Search INV..."
    assert page._inv_search.isClearButtonEnabled()
    assert_stretch_columns(page._ocr_table)
    assert_stretch_columns(page._inv_table)
    assert_stretch_columns(page._node_table)
    assert_stretch_columns(page._tracker_table)
    for table in (page._ocr_table, page._inv_table, page._node_table, page._tracker_table):
        assert table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        for row in range(table.rowCount()):
            assert table.rowHeight(row) <= 28
    for button in page._filter_buttons.values():
        expected_width = button.fontMetrics().horizontalAdvance(button.text()) + 24
        assert button.minimumWidth() >= expected_width

    final_payload = page.final_export_payload()
    assert final_payload["invs"][0]["nodes"][0]["rows"][0]["racking_entries"][0] == {
        "rackings": "3N",
        "offset": {"length_str": "12str"},
    }
    target_json = tmp_path / "drawing_20250909_112233.json"
    fixed_now = datetime(2025, 9, 9, 11, 22, 33)
    with patch(
        "count_and_design.gui.views.ai_statistics.pages.validation."
        "QFileDialog.getSaveFileName",
        return_value=(str(target_json), "JSON Files (*.json)"),
    ) as save_dialog, patch(
        "count_and_design.gui.views.ai_statistics.pages.validation.datetime"
    ) as datetime_module:
        datetime_module.now.return_value = fixed_now
        datetime_module.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        with patch(
            "count_and_design.gui.views.ai_statistics.pages.validation."
            "QMessageBox.information",
        ) as info_message:
            QTest.mouseClick(page._download_button, Qt.MouseButton.LeftButton)
    assert save_dialog.call_args.args[2] == "drawing_20250909_112233.json"
    assert json.loads(target_json.read_text(encoding="utf-8")) == final_payload
    info_message.assert_called_once()

    imported_payloads = []
    page.import_to_count_requested.connect(imported_payloads.append)
    QTest.mouseClick(page._import_button, Qt.MouseButton.LeftButton)
    assert imported_payloads == [final_payload]

    QTest.mouseClick(page._filter_buttons["mismatch"], Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert page._filter_buttons["mismatch"].isChecked()
    assert page._inv_table.rowCount() == 1
    assert page._inv_table.item(0, 0).text() == "INV1"
    assert page._node_table.rowCount() == len(report.inv_comparisons[0].nodes)

    QTest.mouseClick(page._filter_buttons["passed"], Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert page._filter_buttons["passed"].isChecked()
    assert page._inv_table.rowCount() == 1
    assert page._inv_table.item(0, 0).text() == "INV2"
    assert page._node_table.item(0, column_index(page._node_table, "Diff")).text() == "0"

    QTest.mouseClick(page._filter_buttons["all"], Qt.MouseButton.LeftButton)
    QApplication.processEvents()

    assert page._filter_buttons["all"].isChecked()
    assert page._inv_table.rowCount() == len(report.inv_comparisons)

    page._inv_search.setText("INV2")
    QApplication.processEvents()

    assert page._inv_table.rowCount() == 1
    assert page._inv_table.item(0, 0).text() == "INV2"
    assert page._node_table.item(0, column_index(page._node_table, "Total String")).text() == "72"

    page._inv_search.clear()
    QApplication.processEvents()

    assert page._inv_table.rowCount() == len(report.inv_comparisons)

    inv_height = page._inv_table.height()
    node_height = page._node_table.height()
    inv_row_heights = [
        page._inv_table.rowHeight(row)
        for row in range(page._inv_table.rowCount())
    ]
    node_row_heights = [
        page._node_table.rowHeight(row)
        for row in range(page._node_table.rowCount())
    ]

    tab.set_current_stage(2)
    QApplication.processEvents()
    tab.set_current_stage(3)
    QApplication.processEvents()

    assert page._inv_table.height() == inv_height
    assert page._node_table.height() == node_height
    assert_stretch_columns(page._ocr_table)
    assert_stretch_columns(page._inv_table)
    assert_stretch_columns(page._node_table)
    assert_stretch_columns(page._tracker_table)
    assert [
        page._inv_table.rowHeight(row)
        for row in range(page._inv_table.rowCount())
    ] == inv_row_heights
    assert [
        page._node_table.rowHeight(row)
        for row in range(page._node_table.rowCount())
    ] == node_row_heights

    page._inv_table.selectRow(1)
    QApplication.processEvents()

    assert page._node_table.item(0, 0).text() == "Node1"
    assert page._node_table.item(0, column_index(page._node_table, "Total String")).text() == "72"
    assert page._node_table.item(0, column_index(page._node_table, "Diff")).text() == "0"

    tab.close()


def test_validation_tables_scroll_horizontally_after_column_threshold() -> None:
    _app()
    page = ValidationPage()
    tracker_types = tuple(
        f"{index}-string"
        for index in range(_HORIZONTAL_SCROLL_COLUMN_THRESHOLD - 4)
    )
    tracker_counts = tuple(
        TrackerQuantity(tracker_type=tracker_type, tracker_count=index + 1)
        for index, tracker_type in enumerate(tracker_types)
    )
    node = ValidationNodeComparison(
        inv_name="INV1",
        node_name="Node1",
        tracker_counts=tracker_counts,
        calculated_total=120,
        ocr_total=120,
    )
    inv = ValidationInvComparison(
        inv_name="INV1",
        node_count=1,
        tracker_counts=tracker_counts,
        calculated_total=120,
        ocr_total=120,
        nodes=(node,),
    )

    page.set_report(ValidationReport(inv_comparisons=(inv,)))
    QApplication.processEvents()

    for table in (page._inv_table, page._node_table):
        assert table.columnCount() > _HORIZONTAL_SCROLL_COLUMN_THRESHOLD
        assert table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        header = table.horizontalHeader()
        for column in range(table.columnCount()):
            assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
            assert table.columnWidth(column) >= _TABLE_MIN_COLUMN_WIDTH

    for table in (page._ocr_table, page._tracker_table):
        assert table.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff

    page.close()


def _agent4_navigation_result() -> ElementRecognitionResult:
    def page_result(
        page_number: int,
        inv_name: str,
        node_ids: tuple[str, ...],
    ) -> ElementPageResult:
        return ElementPageResult(
            page_number=page_number,
            inv_name=inv_name,
            image_width=100,
            image_height=100,
            detections=tuple(
                ElementDetection(
                    node_id,
                    "Node",
                    0.9,
                    BoundingBox(0, 0, 100, 100),
                    page_number,
                )
                for node_id in node_ids
            ),
        )

    return ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(
                InvDraft(
                    name="INV1",
                    page=12,
                    recognition_id="p12:inv",
                    nodes=(
                        NodeDraft(
                            name="Node1",
                            recognition_id="p12:node:1",
                            ocr_string_count=9,
                            has_ocr_string_count=True,
                            tracker_counts=(TrackerQuantity("9-string", 1),),
                        ),
                    ),
                ),
                InvDraft(
                    name="INV2",
                    page=13,
                    recognition_id="p13:inv",
                    nodes=(
                        NodeDraft(
                            name="Node1",
                            recognition_id="p13:node:1",
                            ocr_string_count=18,
                            has_ocr_string_count=True,
                            tracker_counts=(TrackerQuantity("9-string", 2),),
                        ),
                        # The duplicate name proves navigation relies on the
                        # Agent3 entity link instead of the first name match.
                        NodeDraft(
                            name="Node1",
                            recognition_id="p13:node:2",
                            ocr_string_count=27,
                            has_ocr_string_count=True,
                            tracker_counts=(TrackerQuantity("9-string", 3),),
                        ),
                    ),
                ),
            ),
        ),
        pages=(
            page_result(12, "INV1", ("p12:node:1",)),
            page_result(13, "INV2", ("p13:node:1", "p13:node:2")),
        ),
    )


def _drive_tab_to_agent4(
    tab: AIStatisticsTab,
    result: ElementRecognitionResult,
) -> None:
    workflow = tab._view_model.workflow
    workflow.load_pdf("drawing.pdf", page_count=2)
    workflow.run_page_classification()
    workflow.confirm_current_stage()
    workflow.complete_overall_site_recognition((sample_agent2_extraction(),))
    workflow.confirm_current_stage()
    workflow.complete_element_recognition(result)
    workflow.confirm_current_stage()
    tab._view_model._publish()
    QApplication.processEvents()


def test_validation_double_click_emits_row_comparisons() -> None:
    _app()
    page = ValidationPage()
    node = ValidationNodeComparison(
        inv_name="INV1",
        node_name="Node1",
        recognition_id="p12:node:1",
        calculated_total=120,
        ocr_total=120,
    )
    inv = ValidationInvComparison(
        inv_name="INV1",
        page=12,
        recognition_id="p12:inv",
        node_count=1,
        calculated_total=120,
        ocr_total=120,
        nodes=(node,),
    )
    page.set_report(ValidationReport(inv_comparisons=(inv,)))
    page.resize(900, 700)
    page.show()
    QApplication.processEvents()

    inv_rows: list[ValidationInvComparison] = []
    node_rows: list[ValidationNodeComparison] = []
    page.inv_activated.connect(inv_rows.append)
    page.node_activated.connect(node_rows.append)

    _double_click_row(page._inv_table, 0)
    QApplication.processEvents()
    _double_click_row(page._node_table, 0)
    QApplication.processEvents()

    assert inv_rows == [inv]
    assert node_rows == [node]
    page.close()


def test_agent4_double_click_returns_to_agent3_inv_page() -> None:
    _app()
    tab = _fake_tab()
    _drive_tab_to_agent4(tab, _agent4_navigation_result())

    assert tab.current_stage == 3
    agent3 = tab.element_recognition_page
    # Agent3 starts on page 12; activating INV2 must land on page 13.
    assert agent3.page_combo.currentData() == 12

    _double_click_row(tab.validation_page._inv_table, 1)
    QApplication.processEvents()

    assert tab.current_stage == 2
    assert agent3.page_combo.currentData() == 13
    assert agent3.structure_tree.currentItem().text(0) == "INV2"
    tab.close()


def test_agent4_double_click_returns_to_agent3_node() -> None:
    _app()
    tab = _fake_tab()
    _drive_tab_to_agent4(tab, _agent4_navigation_result())

    page = tab.validation_page
    assert page._inv_table.rowCount() == 2
    page._inv_table.selectRow(1)
    QApplication.processEvents()
    assert page._node_table.rowCount() == 2

    agent3 = tab.element_recognition_page
    inv2_item = agent3.structure_tree.topLevelItem(1)
    _double_click_row(page._node_table, 1)
    QApplication.processEvents()

    assert tab.current_stage == 2
    assert agent3.page_combo.currentData() == 13
    # Both INV2 Nodes share the name "Node1"; the entity link must pick the
    # activated row, not the first name match.
    assert agent3.structure_tree.currentItem() is inv2_item.child(1)
    tab.close()


def test_agent4_table_only_row_opens_source_page() -> None:
    _app()
    tab = _fake_tab()
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(
                InvDraft(
                    name="INV1",
                    page=12,
                    recognition_id="p12:inv",
                    nodes=(
                        NodeDraft(
                            name="CBX-01",
                            recognition_id="p12:node:1",
                            ocr_string_count=88,
                            has_ocr_string_count=True,
                            tracker_counts=(TrackerQuantity("11-string", 8),),
                        ),
                    ),
                ),
            ),
        ),
        pages=(
            ElementPageResult(
                page_number=12,
                inv_name="INV1",
                image_width=100,
                image_height=100,
                table_rows=(
                    NodeStringTableRow("CBX-01", 88),
                    NodeStringTableRow("CBX-02", 66),
                ),
            ),
        ),
    )
    _drive_tab_to_agent4(tab, result)

    page = tab.validation_page
    page._inv_table.selectRow(0)
    QApplication.processEvents()
    # Node Details shows the union: the structure Node plus the table row
    # YOLO missed, which has no structure Node to select in Agent3.
    assert page._node_table.rowCount() == 2
    assert page._node_table.item(1, 0).text() == "CBX-02"

    _double_click_row(page._node_table, 1)
    QApplication.processEvents()

    assert tab.current_stage == 2
    assert tab.element_recognition_page.page_combo.currentData() == 12
    tab.close()


def test_load_pdf_rejects_unsupported_file_type(tmp_path: Path) -> None:
    _app()
    text_path = tmp_path / "notes.txt"
    text_path.write_text("not a pdf", encoding="utf-8")
    tab = _fake_tab()

    with patch.object(tab, "_show_upload_error") as show_error:
        assert not tab.load_pdf(text_path)
        show_error.assert_called_once_with(PdfUploadError.UNSUPPORTED_FILE_TYPE)
    assert tab.pdf_path is None
    assert (
        tab._canvas_stack.currentIndex()
        == tab.page_classification_page.CANVAS_EMPTY
    )

    tab.close()


def test_upload_cancel_returns_to_empty_state(tmp_path: Path) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = _fake_tab()

    with patch.object(tab, "_open_upload_dialog"):
        assert tab._start_upload(pdf_path)
    tab._cancel_upload()

    assert tab.pdf_path is None
    assert (
        tab._canvas_stack.currentIndex()
        == tab.page_classification_page.CANVAS_EMPTY
    )
    assert tab._upload_button.isEnabled()
    assert not tab._run_button.isEnabled()

    tab.close()


def test_main_window_places_ai_statistics_first() -> None:
    _app()
    window = MainWindow()

    assert window._tabs.count() == 7
    assert window._tabs.widget(0) is window._ai_statistics_tab

    window.close()


def test_classification_edit_updates_group_counts(tmp_path: Path) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = _fake_tab()
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)

    QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
    _wait_for_agent1(tab)
    cards = tab.page_classification_page.findChildren(PageClassificationCard)
    assert len(cards) == 1
    assert not cards[0].findChild(QLabel, "pageThumbnail").pixmap().isNull()
    counts = tab.page_classification_page.counts_panel._value_labels
    assert counts[DrawingPageType.ARRAY_LAYOUT.value].text() == "1"
    assert counts[DrawingPageType.OTHER.value].text() == "0"

    cards[0].category_combo.setCurrentText(DrawingPageType.OTHER.value)
    QApplication.processEvents()

    assert counts[DrawingPageType.ARRAY_LAYOUT.value].text() == "0"
    assert counts[DrawingPageType.OTHER.value].text() == "1"
    result = tab._view_model.state.page_classification
    assert result.pages[0].page_type == DrawingPageType.OTHER.value
    cards = tab.page_classification_page.findChildren(PageClassificationCard)
    assert not cards[0].findChild(QLabel, "pageThumbnail").pixmap().isNull()

    tab.close()


def test_agent2_ui_shows_consolidated_pages_among_multiple_results() -> None:
    _app()
    translations = TranslationManager.instance()
    previous_locale = translations.locale
    page = InfoRecognitionPage()
    try:
        translations.locale = "en_US"
        merged = replace(
            _agent2_result(),
            page_number=19,
            raw={"consolidated_page_numbers": (17, 19)},
        )
        page.show_results(
            {},
            (
                merged,
                OverallSiteExtraction(page_number=18),
                OverallSiteExtraction(page_number=20),
            ),
            status="result ready",
        )

        labels = [
            label.text()
            for label in page.result_tree.findChildren(
                QLabel, "resultPageLabel"
            )
        ]
        assert labels == [
            "Merged pages: 17, 19",
            "Page 18",
            "Page 20",
        ]
    finally:
        translations.locale = previous_locale
        page.close()


def test_agent2_ui_lists_results_with_inv_data_first() -> None:
    _app()
    translations = TranslationManager.instance()
    previous_locale = translations.locale
    page = InfoRecognitionPage()
    try:
        translations.locale = "en_US"
        empty_merged = OverallSiteExtraction(
            page_number=40,
            raw={"consolidated_page_numbers": (40, 41)},
        )
        populated = replace(_agent2_result(), page_number=42)

        page.show_results(
            {},
            (empty_merged, populated),
            status="result ready",
        )

        assert [result.page_number for result in page._source_results] == [
            42,
            40,
        ]
        labels = [
            label.text()
            for label in page.result_tree.findChildren(
                QLabel, "resultPageLabel"
            )
        ]
        assert labels == ["Page 42", "Merged pages: 40, 41"]
    finally:
        translations.locale = previous_locale
        page.close()


def test_agent2_ui_hides_redundant_missing_data_warnings() -> None:
    _app()
    page = InfoRecognitionPage()
    warnings = (
        "SYSTEMS SPECIFICATIONS table is not visible in the provided crop; "
        "tracker_ocr_fields not extracted.",
        "No INV/PCS equipment identifier table visible in the provided crop.",
        "Tracker column completeness check: detected 2 columns but returned 1.",
    )
    result = OverallSiteExtraction(page_number=40, warnings=warnings)

    page.show_results({}, (result,), status="result ready")

    warning_labels = page.result_tree.findChildren(QLabel, "warningItem")
    assert [label.text() for label in warning_labels] == [
        "• Tracker column completeness check: detected 2 columns but returned 1."
    ]
    assert page._source_results[0].warnings == warnings
    page.close()


def test_classification_combo_wheel_scrolls_results_without_changing_value(
    tmp_path: Path,
) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = _fake_tab()
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)

    QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
    _wait_for_agent1(tab)
    tab.resize(900, 500)
    tab.show()
    tab.page_classification_page._groups_widget.setMinimumHeight(1200)
    QApplication.processEvents()

    page = tab.page_classification_page
    combo = page.findChildren(PageClassificationCard)[0].category_combo
    scroll_bar = page.results_scroll.verticalScrollBar()
    assert scroll_bar.maximum() > 0
    initial_category = combo.currentText()
    initial_scroll = scroll_bar.value()
    center = combo.rect().center()
    wheel = QWheelEvent(
        QPointF(center),
        QPointF(combo.mapToGlobal(center)),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )

    QApplication.sendEvent(combo, wheel)

    assert combo.currentText() == initial_category
    assert scroll_bar.value() > initial_scroll
    tab.close()


def test_classification_core_content_retranslates_to_chinese() -> None:
    _app()
    translations = TranslationManager.instance()
    previous_locale = translations.locale
    page = PageClassificationPage(QPdfDocument())
    try:
        page.show_result(
            PageClassificationResult(
                pages=(
                    PageClassification(
                        page_number=21,
                        page_type=DrawingPageType.ARRAY_LAYOUT.value,
                    ),
                )
            )
        )
        translations.locale = "zh_CN"
        page.retranslate()

        card = page.findChildren(PageClassificationCard)[0]
        assert page.canvas_title.text() == "页面分类预览"
        assert card.page_label.text() == "第 21 页"
        assert card.category_combo.currentText() == "Array Layout"
        assert card.category_combo.currentData() == "Array Layout"
        assert card.meta.text().startswith("置信度：")
        assert page.counts_panel.title_label.text() == "分类统计"
        assert (
            page.counts_panel._name_labels["Total Pages"].text()
            == "Total Pages"
        )
        assert (
            page.counts_panel._name_labels["Overall Layout"].text()
            == "Overall Layout"
        )
        assert page.counts_panel._name_labels["Other"].text() == "Other Pages"
        assert page.findChild(QLabel, "classificationGroupTitle").text() == (
            "Array Layout"
        )
    finally:
        translations.locale = previous_locale
        page.close()


class FailingAgent1Provider:
    def classify(self, *_args, **_kwargs):
        raise Agent1ProviderError("embedding endpoint failed")


class SlowCancellableAgent1Provider:
    def classify(
        self,
        _pdf_path,
        page_count,
        *,
        on_progress=None,
        is_cancelled=None,
    ):
        for index in range(200):
            if is_cancelled is not None and is_cancelled():
                raise Agent1Cancelled("cancelled")
            if on_progress is not None and index == 0:
                on_progress(0, page_count)
            time.sleep(0.005)
        raise AssertionError("Provider should have been cancelled")


def test_agent1_failure_is_shown_and_can_be_retried(tmp_path: Path) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = AIStatisticsTab(
        view_model=AIStatisticsViewModel(
            AIStatisticsWorkflow(FailingAgent1Provider())
        )
    )
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)
    with patch(
        "count_and_design.gui.views.ai_statistics.tab.QMessageBox.warning"
    ) as warning:
        QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
        _wait_for_agent1(tab)
        QApplication.processEvents()

    assert tab._stage_badges[0].text() == "failed"
    assert tab._run_button.isEnabled()
    assert tab._view_model.state.error_message == "embedding endpoint failed"
    warning.assert_called_once()
    tab.close()


def test_agent1_background_task_exposes_progress_and_cancel(
    tmp_path: Path,
) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = AIStatisticsTab(
        view_model=AIStatisticsViewModel(
            AIStatisticsWorkflow(SlowCancellableAgent1Provider())
        )
    )
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)
    tab.show()

    QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert tab._view_model.state.classification_running
    assert tab._agent1_progress.isVisible()
    assert tab._cancel_ai_button.isVisible()

    QTest.mouseClick(tab._cancel_ai_button, Qt.MouseButton.LeftButton)
    _wait_for_agent1(tab)
    QApplication.processEvents()

    assert tab._stage_badges[0].text() == "waiting"
    assert tab._run_button.isEnabled()
    assert not tab._agent1_progress.isVisible()
    tab.close()

def test_classification_grid_reflows_in_wide_window(tmp_path: Path) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = _fake_tab()
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)
    QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
    _wait_for_agent1(tab)

    tab.resize(1600, 900)
    tab.show()
    QTest.qWait(50)
    QApplication.processEvents()

    grids = tab.page_classification_page.findChildren(ResponsiveCardGrid)
    assert grids
    assert max(grid.column_count for grid in grids) >= 6
    tab.close()


def test_clicking_thumbnail_opens_maximized_zoomable_preview(
    tmp_path: Path,
) -> None:
    _app()
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    tab = _fake_tab()
    with patch.object(tab, "_show_upload_ready"):
        tab.load_pdf(pdf_path)
    QTest.mouseClick(tab._run_button, Qt.MouseButton.LeftButton)
    _wait_for_agent1(tab)

    result = tab._view_model.state.page_classification
    image = result.thumbnails[1]
    captured = []

    def capture_dialog(dialog):
        captured.append(dialog)
        initial_scale = dialog.scale_factor
        QTest.mouseClick(dialog._zoom_in, Qt.MouseButton.LeftButton)
        assert dialog.scale_factor > initial_scale
        return 0

    card = tab.page_classification_page.findChildren(
        PageClassificationCard
    )[0]
    thumbnail = card.findChild(QLabel, "pageThumbnail")
    with patch(
        "count_and_design.gui.views.ai_statistics.pages.classification."
        "render_pdf_page_with_thumbnail",
        return_value=(image, image),
    ), patch.object(PagePreviewDialog, "exec", capture_dialog):
        QTest.mouseClick(thumbnail, Qt.MouseButton.LeftButton)

    assert len(captured) == 1
    assert (
        captured[0].windowState()
        & Qt.WindowState.WindowMaximized
    )
    tab.close()

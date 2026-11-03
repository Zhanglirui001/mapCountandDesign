from __future__ import annotations

import os
from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsPixmapItem,
    QLineEdit,
    QMessageBox,
)

from count_and_design.application.ai import BoundingBox
from count_and_design.application.ai_statistics import (
    AIStatisticsStage,
    AIStatisticsStageStatus,
    AIStatisticsWorkflow,
    Agent2ElementContract,
    Agent3Cancelled,
    ArrayLayoutPageInput,
    ElementDetection,
    ElementPageResult,
    ElementRecognitionRequest,
    ElementRecognitionResult,
    FakePageClassificationProvider,
    InvDraft,
    InvExpectedStringCount,
    InvOcrRow,
    InvStringCount,
    InvStringCountColumn,
    NumStrDetail,
    NodeDraft,
    NodeStringTableRow,
    OverallSiteExtraction,
    PanelOrientation,
    RackingEntryDraft,
    RecognitionIssue,
    RecognitionIssueSeverity,
    ReviewedTree,
    RowDraft,
    SourceReference,
    TrackerDefinition,
    TrackerFieldValue,
    TrackerOcrFields,
    TrackerQuantity,
)
from count_and_design.gui.views.ai_statistics.pages import (
    element_recognition as element_recognition_module,
)
from count_and_design.gui.views.ai_statistics.pages.element_recognition import (
    DetectionCanvas,
    ElementRecognitionPage,
    IssueItemDelegate,
)
from count_and_design.i18n import TranslationManager


def _result() -> ElementRecognitionResult:
    node = NodeDraft(
        name="CBX01",
        recognition_id="p1:node:1",
        ocr_string_count=2,
        tracker_counts=(TrackerQuantity("2-string", 1),),
        rows=(RowDraft("Row1", recognition_id="p1:node:1:row:1"),),
    )
    issue = RecognitionIssue(
        "unassigned_tracker",
        "Tracker is outside every Node.",
        page_number=1,
        entity_id="p1:tracker:1",
    )
    return ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            project_name="draft",
            project_type="IBEX",
            invs=(
                InvDraft(
                    name="INV 1",
                    nodes=(node,),
                    recognition_id="p1:inv",
                ),
            ),
        ),
        equipment_family="CBX",
        pages=(
            ElementPageResult(
                page_number=1,
                inv_name="INV 1",
                image_width=100,
                image_height=100,
                detections=(
                    ElementDetection(
                        "p1:node:1",
                        "Node",
                        0.9,
                        BoundingBox(0, 0, 80, 80),
                        1,
                    ),
                    ElementDetection(
                        "p1:tracker:1",
                        "Tracker",
                        0.8,
                        BoundingBox(85, 85, 95, 95),
                        1,
                    ),
                ),
                issues=(issue,),
            ),
        ),
        issues=(issue,),
    )


class _FakeAgent3:
    def __init__(self, result: ElementRecognitionResult) -> None:
        self.result = result
        self.requests: list[ElementRecognitionRequest] = []

    def recognize(self, request, *, on_progress=None, is_cancelled=None):
        self.requests.append(request)
        if is_cancelled is not None and is_cancelled():
            raise Agent3Cancelled("cancelled")
        if on_progress is not None:
            on_progress(5, 5)
        return self.result


def _agent3_workflow(tmp_path) -> tuple[AIStatisticsWorkflow, _FakeAgent3]:
    fake = _FakeAgent3(_result())
    workflow = AIStatisticsWorkflow(
        FakePageClassificationProvider(),
        element_recognizer=fake,
    )
    workflow.load_pdf(tmp_path / "drawing.pdf", 1)
    classification = workflow.run_page_classification()
    workflow.complete_page_classification(
        replace(classification, page_images={1: b"agent1-high-resolution-png"})
    )
    workflow.confirm_current_stage()
    workflow.confirm_current_stage()
    workflow.configure_element_recognition(
        (TrackerDefinition("2-string", 2, total_count=1, panel_label="Panel A"),),
        PanelOrientation.POSITIVE_NORTH,
    )
    return workflow, fake


def test_workflow_assembles_unmapped_agent1_pages_and_invalidates_agent4(
    tmp_path,
) -> None:
    workflow, fake = _agent3_workflow(tmp_path)

    result = workflow.run_element_recognition()
    assert fake.requests[0].pages[0].page_number == 1
    assert fake.requests[0].pages[0].png_bytes
    assert workflow.status_for(AIStatisticsStage.ELEMENT_RECOGNITION) == (
        AIStatisticsStageStatus.RESULT_READY
    )
    workflow.confirm_current_stage()
    assert workflow.snapshot.validation_report is not None

    workflow.set_current_stage(AIStatisticsStage.ELEMENT_RECOGNITION.index)
    edited = replace(
        result.reviewed_tree,
        invs=(replace(result.reviewed_tree.invs[0], name="INV edited"),),
    )
    workflow.replace_element_review(edited)

    assert workflow.snapshot.validation_report is None
    assert workflow.snapshot.reviewed_tree.invs[0].name == "INV edited"
    assert workflow.status_for(AIStatisticsStage.ELEMENT_RECOGNITION) == (
        AIStatisticsStageStatus.NEEDS_CONFIRM
    )
    assert workflow.status_for(AIStatisticsStage.VALIDATION) == (
        AIStatisticsStageStatus.WAITING
    )


def test_workflow_allows_agent3_rerun_after_confirmation(tmp_path) -> None:
    """Regression: revisiting Agent2 and re-entering Agent3 must not crash.

    Confirming Agent3 marks the stage CONFIRMED; navigating back and
    confirming Agent2 auto-runs Agent3 again, which used to raise
    "Element Recognition is already running." even though no thread was
    active.
    """

    workflow, fake = _agent3_workflow(tmp_path)

    workflow.run_element_recognition()
    workflow.confirm_current_stage()
    assert workflow.status_for(AIStatisticsStage.ELEMENT_RECOGNITION) == (
        AIStatisticsStageStatus.CONFIRMED
    )
    assert workflow.snapshot.selected_stage == AIStatisticsStage.VALIDATION

    workflow.set_current_stage(AIStatisticsStage.INFO_RECOGNITION.index)
    workflow.confirm_current_stage()
    assert workflow.snapshot.selected_stage == (
        AIStatisticsStage.ELEMENT_RECOGNITION
    )

    workflow.run_element_recognition()
    assert len(fake.requests) == 2
    assert workflow.status_for(AIStatisticsStage.ELEMENT_RECOGNITION) == (
        AIStatisticsStageStatus.RESULT_READY
    )


def test_workflow_does_not_require_agent2_page_mapping(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", 1)
    classification = workflow.run_page_classification()
    workflow.complete_page_classification(
        replace(classification, page_images={1: b"agent1-high-resolution-png"})
    )
    workflow.confirm_current_stage()
    workflow.confirm_current_stage()
    workflow.configure_element_recognition(
        (TrackerDefinition("2-string", 2),),
    )

    assert workflow.is_element_recognition_run_enabled()
    workflow.begin_element_recognition()
    assert workflow.element_recognition_request.pages[0].page_number == 1


@pytest.mark.parametrize(
    ("overall_site_orientation", "expected"),
    (
        ("P-B", PanelOrientation.TOWARD_BOX),
        ("N-B", PanelOrientation.AWAY_FROM_BOX),
        ("P-N", PanelOrientation.POSITIVE_NORTH),
        ("P-S", PanelOrientation.POSITIVE_SOUTH),
        ("P-E", PanelOrientation.POSITIVE_EAST),
        ("P-W", PanelOrientation.POSITIVE_WEST),
    ),
)
def test_agent2_orientation_becomes_agent3_project_default(
    tmp_path,
    overall_site_orientation,
    expected,
) -> None:
    workflow, _fake = _agent3_workflow(tmp_path)

    workflow.update_overall_site_review(
        (OverallSiteExtraction(page_number=8),),
        overall_site_orientation,
    )

    assert workflow.element_recognition_request.default_panel_orientation == expected


def test_synthetic_agent2_adapter_preserves_inv_names_and_defaults() -> None:
    contract = Agent2ElementContract.from_mapping(
        {
            "inv_names": ["INV B", "INV A"],
            "default_panel_orientation": "positive_south",
            "default_offset_snap_step_strings": 0.5,
            "tracker_definitions": [
                {
                    "code": "2-string",
                    "strings_per_tracker": 2,
                    "total_count": 8,
                    "panel_label": "Panel A",
                }
            ],
        }
    )

    assert contract.inv_names == ("INV B", "INV A")
    assert contract.default_panel_orientation == PanelOrientation.POSITIVE_SOUTH
    assert contract.default_offset_snap_step_strings == 0.5
    assert contract.tracker_definitions[0].total_count == 8


def test_current_agent2_adapter_uses_inv_list_and_tracker_ocr() -> None:
    def field(value: str) -> TrackerFieldValue:
        return TrackerFieldValue(label="field", meaning="", value=value)

    extraction = OverallSiteExtraction(
        page_number=8,
        inv_list=("INV 101", "INV 102"),
        inv_rows=(
            InvOcrRow(
                identifier="INV 101",
                string_counts=(
                    InvStringCount(1, "JINKO 590W MODULE STRINGS", "353"),
                ),
            ),
            InvOcrRow(
                identifier="INV 102",
                string_counts=(
                    InvStringCount(1, "JINKO 590W MODULE STRINGS", "116"),
                ),
            ),
        ),
        tracker_ocr_fields=(
            TrackerOcrFields(
                module_model=field("Panel-X"),
                module_stc_dc_rating_w=field("605W"),
                string_count=field("100"),
                num_str=field("13-STR"),
                num_str_details=(NumStrDetail("13-STR", "7"),),
            ),
        ),
    )
    contract = Agent2ElementContract.from_overall_site(
        (extraction,),
    )

    assert contract.inv_names == ("INV 101", "INV 102")
    assert contract.tracker_definitions == (
        TrackerDefinition("13-string", 13, 7, "Panel-X"),
    )
    assert contract.inv_string_counts == (
        InvExpectedStringCount(
            "INV 101",
            (InvStringCountColumn("JINKO 590W MODULE STRINGS", 353, 1),),
        ),
        InvExpectedStringCount(
            "INV 102",
            (InvStringCountColumn("JINKO 590W MODULE STRINGS", 116, 1),),
        ),
    )
    assert contract.inv_string_counts[0].total == 353


def _png(width: int = 100, height: int = 100) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def shared_page(qapp: QApplication) -> ElementRecognitionPage:
    """One page for tests that only configure requests or read widget config.

    Constructing ``ElementRecognitionPage`` is the dominant per-test cost
    here.  Tests that never publish a review tree, and assert no layout
    geometry, can share one instance; stateful tests keep their own.
    """

    return ElementRecognitionPage()


def test_agent3_settings_lists_agent2_invs_without_page_mapping(
    shared_page: ElementRecognitionPage,
) -> None:
    page = shared_page
    changes = []
    page.inv_settings_changed.connect(
        lambda inv, orientation, snap: changes.append((inv, orientation, snap))
    )
    page.set_request(
        ElementRecognitionRequest(
            pages=(
                ArrayLayoutPageInput(3, _png()),
                ArrayLayoutPageInput(4, _png()),
            ),
            tracker_definitions=(TrackerDefinition("2-string", 2),),
            expected_inv_names=("INV 101", "INV 102"),
        )
    )

    assert page.mapping_table.rowCount() == 2
    assert page.mapping_table.item(0, 0).text() == "INV 101"
    assert page.mapping_table.item(1, 0).text() == "INV 102"
    first_orientation = page.mapping_table.cellWidget(0, 1)
    expected_texts = [
        "Positive to Box  ·  正极朝汇流箱 Box  (P-B)",
        "Negative to Box  ·  负极朝汇流箱 Box  (N-B)",
        "Positive to North  ·  正极朝北  (P-N)",
        "Positive to South  ·  正极朝南  (P-S)",
        "Positive to East  ·  正极朝东  (P-E)",
        "Positive to West  ·  正极朝西  (P-W)",
    ]
    for combo in (
        page.project_orientation,
        first_orientation,
        page.orientation_combo,
    ):
        assert [
            combo.itemText(index) for index in range(1, combo.count())
        ] == expected_texts
        assert [
            combo.itemData(index).value for index in range(1, combo.count())
        ] == ["P-B", "N-B", "P-N", "P-S", "P-E", "P-W"]
    first_orientation.setCurrentIndex(1)
    assert changes[-1][0] == "INV 101"


def test_agent3_review_page_shows_recognition_issues_and_editable_tree() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(1, b"fixture-free"),),
        tracker_definitions=(TrackerDefinition("2-string", 2),),
        expected_inv_names=("INV 1",),
    )
    page.set_request(request)
    assert page.mapping_table.rowCount() == 1
    assert page.mapping_table.item(0, 0).text() == "INV 1"

    page.set_result(_result(), {})
    assert page.structure_tree.topLevelItemCount() == 1
    assert page.structure_tree.topLevelItem(0).childCount() == 1
    assert page.issues.count() == 1
    assert len(page.canvas.scene().items()) == 2

    changes = []
    page.review_changed.connect(changes.append)
    page.structure_tree.setCurrentItem(page.structure_tree.topLevelItem(0))
    page._add_node()
    assert changes
    assert len(changes[-1].invs[0].nodes) == 2
    assert changes[-1].invs[0].nodes[-1].name == "CBX02"


def test_agent3_tree_row_label_shows_only_nonzero_offsets() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    result = _result()
    row = RowDraft(
        "Row1",
        racking_entries=(
            RackingEntryDraft("2 N", "0mm"),
            RackingEntryDraft("2 P", "1.5str"),
        ),
        recognition_id="p1:node:1:row:1",
    )
    node = replace(result.reviewed_tree.invs[0].nodes[0], rows=(row,))
    tree = replace(
        result.reviewed_tree,
        invs=(replace(result.reviewed_tree.invs[0], nodes=(node,)),),
    )

    page.set_result(replace(result, reviewed_tree=tree), {})

    row_item = page.structure_tree.topLevelItem(0).child(0).child(0)
    assert row_item.text(0) == "Row1 (1.5str)"

    page.structure_tree.setCurrentItem(row_item)
    page.racking_table.item(1, 1).setText("0str")
    assert page.structure_tree.currentItem().text(0) == "Row1"


def test_agent3_tree_row_offset_suffix_is_not_saved_as_its_name() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    result = _result()
    row = RowDraft(
        "Row1",
        racking_entries=(RackingEntryDraft("2 P", "1str"),),
        recognition_id="p1:node:1:row:1",
    )
    node = replace(result.reviewed_tree.invs[0].nodes[0], rows=(row,))
    tree = replace(
        result.reviewed_tree,
        invs=(replace(result.reviewed_tree.invs[0], nodes=(node,)),),
    )
    page.set_result(replace(result, reviewed_tree=tree), {})

    row_item = page.structure_tree.topLevelItem(0).child(0).child(0)
    row_item.setText(0, "Renamed Row (1str)")

    assert page._tree.invs[0].nodes[0].rows[0].name == "Renamed Row"


def test_agent3_issues_prioritize_selected_array_page_and_support_search() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    page.set_request(
        ElementRecognitionRequest(
            pages=(
                ArrayLayoutPageInput(1, b"page-one"),
                ArrayLayoutPageInput(2, b"page-two"),
            ),
            tracker_definitions=(TrackerDefinition("2-string", 2),),
        )
    )
    first_node = NodeDraft(
        "CBX01",
        source=SourceReference(page_number=1),
        recognition_id="p1:node:1",
    )
    second_node = NodeDraft(
        "CBX02",
        source=SourceReference(page_number=2),
        recognition_id="p2:node:1",
    )
    first_warning = RecognitionIssue(
        "first-page-warning",
        "First page warning",
        RecognitionIssueSeverity.WARNING,
        page_number=1,
    )
    first_error = RecognitionIssue(
        "first-page-error",
        "First page error",
        RecognitionIssueSeverity.ERROR,
        page_number=1,
    )
    second_error = RecognitionIssue(
        "second-page-code",
        "Second page error",
        RecognitionIssueSeverity.ERROR,
        page_number=2,
        entity_id="p2:node:1",
    )
    project_warning = RecognitionIssue(
        "project-warning",
        "Project-wide warning",
    )
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(
                InvDraft(
                    "INV 1",
                    nodes=(first_node,),
                    source=SourceReference(page_number=1),
                    recognition_id="p1:inv",
                ),
                InvDraft(
                    "INV 2",
                    nodes=(second_node,),
                    source=SourceReference(page_number=2),
                    recognition_id="p2:inv",
                ),
            )
        ),
        pages=(
            ElementPageResult(
                1,
                "INV 1",
                100,
                100,
                issues=(first_warning, first_error),
            ),
            ElementPageResult(2, "INV 2", 100, 100, issues=(second_error,)),
        ),
        issues=(first_warning, first_error, second_error, project_warning),
    )
    page.set_result(result)

    page.structure_tree.setCurrentItem(page.structure_tree.topLevelItem(0).child(0))
    issue_rows = [page.issues.item(index) for index in range(page.issues.count())]
    assert [item.data(Qt.ItemDataRole.UserRole + 1) for item in issue_rows] == [
        1,
        1,
        2,
        None,
    ]
    assert "First page error" in issue_rows[0].text()
    assert page.issues.count() == 4
    assert page.issues._divider_before_row == 2

    page.issue_search.setText("second-page-code")
    assert page.issues.count() == 4
    page.issue_search.returnPressed.emit()
    assert page.issues.count() == 1
    assert page.issues.item(0).data(Qt.ItemDataRole.UserRole + 1) == 2
    assert "4" in page.issues_button.text()

    page.issue_search.clear()
    assert page.issues.count() == 4
    page.issue_search.setText("project-warning")
    page.issue_search_button.click()
    assert page.issues.count() == 1
    assert "Project-wide warning" in page.issues.item(0).text()


def test_agent3_issues_can_be_muted_and_sink_below_active_issues() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    first_error = RecognitionIssue(
        "first-error",
        "First error",
        RecognitionIssueSeverity.ERROR,
        page_number=1,
    )
    first_warning = RecognitionIssue(
        "first-warning",
        "First warning",
        RecognitionIssueSeverity.WARNING,
        page_number=1,
    )
    second_warning = RecognitionIssue(
        "second-warning",
        "Second warning",
        RecognitionIssueSeverity.WARNING,
        page_number=2,
    )
    page.set_result(
        ElementRecognitionResult(
            reviewed_tree=ReviewedTree(),
            pages=(
                ElementPageResult(
                    1,
                    "INV 1",
                    100,
                    100,
                    issues=(first_error, first_warning),
                ),
                ElementPageResult(
                    2,
                    "INV 2",
                    100,
                    100,
                    issues=(second_warning,),
                ),
            ),
            issues=(first_error, first_warning, second_warning),
        )
    )
    key_role = Qt.ItemDataRole.UserRole + 3
    muted_role = Qt.ItemDataRole.UserRole + 4

    def _keys() -> list[object]:
        return [
            page.issues.item(index).data(key_role)
            for index in range(page.issues.count())
        ]

    assert _keys() == [
        ("first-error", 1, ""),
        ("first-warning", 1, ""),
        ("second-warning", 2, ""),
    ]
    assert "3" in page.issues_button.text()
    assert not page.issues_button.isHidden()

    page._set_issue_muted(("first-error", 1, ""), muted=True)

    assert _keys() == [
        ("first-warning", 1, ""),
        ("second-warning", 2, ""),
        ("first-error", 1, ""),
    ]
    assert [
        page.issues.item(index).data(muted_role)
        for index in range(page.issues.count())
    ] == [False, False, True]
    assert page.issues.item(2).toolTip().endswith("Ignored")
    assert "2" in page.issues_button.text()
    assert not page.issues_button.isHidden()

    # Wording changes do not create a new issue or lose the ignored state.
    current = page._result
    revised_error = replace(first_error, message="Updated error wording")
    page.set_result(replace(
        current,
        pages=(replace(current.pages[0], issues=(revised_error, first_warning)), current.pages[1]),
    ))
    assert page.issues.item(2).data(muted_role) is True
    assert "Updated error wording" in page.issues.item(2).text()
    assert "2" in page.issues_button.text()

    page._set_issue_muted(("first-error", 1, ""), muted=False)
    assert _keys() == [
        ("first-error", 1, ""),
        ("first-warning", 1, ""),
        ("second-warning", 2, ""),
    ]
    assert "3" in page.issues_button.text()

    page._set_issue_muted(("first-error", 1, ""), muted=True)
    page.set_result(None)
    assert page._muted_issue_keys == set()
    assert page.issues_button.isHidden()


def test_agent3_issue_context_menu_toggles_mute(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    issue = RecognitionIssue("orphan", "Orphan tracker", page_number=1)
    page.set_result(
        ElementRecognitionResult(
            reviewed_tree=ReviewedTree(),
            pages=(ElementPageResult(1, "INV 1", 100, 100, issues=(issue,)),),
            issues=(issue,),
        )
    )
    page.issues_dialog.show()
    app.processEvents()
    monkeypatch.setattr(
        element_recognition_module,
        "_exec_context_menu",
        lambda menu, position: menu.actions()[0],
    )
    muted_role = Qt.ItemDataRole.UserRole + 4

    page._show_issue_context_menu(
        page.issues.visualItemRect(page.issues.item(0)).center()
    )
    assert page.issues.item(0).data(muted_role) is True
    assert "0" in page.issues_button.text()
    # An all-muted list keeps the button: the dialog is the only place to
    # restore issues.
    assert not page.issues_button.isHidden()

    page._show_issue_context_menu(
        page.issues.visualItemRect(page.issues.item(0)).center()
    )
    assert page.issues.item(0).data(muted_role) is False
    assert "1" in page.issues_button.text()


def test_agent3_issue_click_keeps_selection_focus_and_readable_text() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    page.set_request(
        ElementRecognitionRequest(
            pages=(
                ArrayLayoutPageInput(1, b"page-one"),
                ArrayLayoutPageInput(2, b"page-two"),
            ),
            tracker_definitions=(TrackerDefinition("2-string", 2),),
        )
    )
    first_node = NodeDraft(
        "CBX01",
        source=SourceReference(page_number=1),
        recognition_id="p1:node:1",
    )
    second_node = NodeDraft(
        "CBX02",
        source=SourceReference(page_number=2),
        recognition_id="p2:node:1",
    )
    issue = RecognitionIssue(
        "second-page-error",
        "Second page error",
        RecognitionIssueSeverity.ERROR,
        page_number=2,
        entity_id="p2:node:1",
    )
    page.set_result(
        ElementRecognitionResult(
            reviewed_tree=ReviewedTree(
                invs=(
                    InvDraft(
                        "INV 1",
                        nodes=(first_node,),
                        source=SourceReference(page_number=1),
                        recognition_id="p1:inv",
                    ),
                    InvDraft(
                        "INV 2",
                        nodes=(second_node,),
                        source=SourceReference(page_number=2),
                        recognition_id="p2:inv",
                    ),
                )
            ),
            pages=(
                ElementPageResult(1, "INV 1", 100, 100),
                ElementPageResult(2, "INV 2", 100, 100, issues=(issue,)),
            ),
            issues=(issue,),
        )
    )
    page.show()
    page.issues_dialog.show()
    page.issues_dialog.activateWindow()
    app.processEvents()
    page.structure_tree.setCurrentItem(page.structure_tree.topLevelItem(0).child(0))
    selected_issue = page.issues.item(0)
    page.issues.setCurrentItem(selected_issue)

    page.issues.itemClicked.emit(selected_issue)
    app.processEvents()

    assert page.page_combo.currentData() == 2
    assert page.structure_tree.currentItem().data(
        0, Qt.ItemDataRole.UserRole + 1
    ) == "p2:node:1"
    assert page.issues.currentItem() is not None
    assert page.issues.currentItem().data(
        Qt.ItemDataRole.UserRole
    ) == "p2:node:1"
    assert page.issues.hasFocus()
    assert IssueItemDelegate._MESSAGE_COLOR.name() == "#0f172a"


def test_agent3_canvas_wheel_zooms_and_left_drag_pans() -> None:
    app = QApplication.instance() or QApplication([])
    canvas = DetectionCanvas()
    canvas.resize(240, 180)
    canvas.show()
    canvas.show_page(_png())
    app.processEvents()

    viewport = canvas.viewport()
    center = viewport.rect().center()
    fitted_scale = canvas.transform().m11()
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
    assert canvas.transform().m11() > fitted_scale

    canvas.scale(8, 8)
    app.processEvents()
    horizontal = canvas.horizontalScrollBar()
    vertical = canvas.verticalScrollBar()
    horizontal.setValue(horizontal.maximum() // 2)
    vertical.setValue(vertical.maximum() // 2)
    before_pan = (horizontal.value(), vertical.value())

    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=center)
    assert viewport.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    QTest.mouseMove(viewport, center + QPoint(-20, -20))
    QTest.mouseRelease(
        viewport,
        Qt.MouseButton.LeftButton,
        pos=center + QPoint(-20, -20),
    )
    assert (horizontal.value(), vertical.value()) != before_pan


def test_agent3_canvas_scrollbars_ignore_wheel_navigation() -> None:
    app = QApplication.instance() or QApplication([])
    canvas = DetectionCanvas()
    canvas.resize(240, 180)
    canvas.show()
    canvas.show_page(_png())
    canvas.scale(10, 10)
    app.processEvents()

    scrollbar = canvas.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    before = scrollbar.value()
    center = scrollbar.rect().center()
    wheel = QWheelEvent(
        QPointF(center),
        QPointF(scrollbar.mapToGlobal(center)),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(scrollbar, wheel)

    assert scrollbar.value() == before


def test_agent3_canvas_uses_solid_high_contrast_selection() -> None:
    app = QApplication.instance() or QApplication([])
    canvas = DetectionCanvas()
    canvas.show_page(_png(), _result().pages[0])

    canvas.select_entity("p1:node:1")
    selected = canvas.scene().selectedItems()[0]

    assert selected.zValue() == 1
    assert selected._highlight_pen.style() == Qt.PenStyle.SolidLine
    assert selected._highlight_pen.color().name() == "#00e5ff"
    assert selected._highlight_pen.width() == 6
    assert selected._highlight_brush.color().alpha() == 55

    canvas.select_entity("p1:tracker:1")
    assert selected.zValue() == 0
    assert canvas.scene().selectedItems()[0].zValue() == 1


def test_agent3_canvas_only_centers_the_same_selection_once() -> None:
    app = QApplication.instance() or QApplication([])
    canvas = DetectionCanvas()
    canvas.resize(240, 180)
    canvas.show()
    result = ElementPageResult(
        1,
        "INV 1",
        600,
        450,
        detections=(
            ElementDetection(
                "p1:node:1",
                "Node",
                0.9,
                BoundingBox(400, 300, 500, 400),
                1,
            ),
        ),
    )
    canvas.show_page(_png(600, 450), result)
    canvas.scale(4, 4)
    canvas.select_entity("p1:node:1")
    app.processEvents()

    horizontal = canvas.horizontalScrollBar()
    vertical = canvas.verticalScrollBar()
    horizontal.setValue(horizontal.minimum())
    vertical.setValue(vertical.minimum())
    panned_position = (horizontal.value(), vertical.value())

    canvas.select_entity("p1:node:1")

    assert (horizontal.value(), vertical.value()) == panned_position


def test_agent3_canvas_draws_table_bbox_with_recognition_source_label() -> None:
    app = QApplication.instance() or QApplication([])
    canvas = DetectionCanvas()
    page = ElementPageResult(
        1,
        "INV 1",
        100,
        100,
        table_bbox=BoundingBox(10, 20, 90, 80),
        table_bbox_source="yolo",
    )

    canvas.show_page(_png(), page)

    table_items = [
        item for item in canvas.scene().items() if item.data(1) == "table_detection"
    ]
    labels = [
        item
        for item in canvas.scene().items()
        if item.data(1) == "table_detection_label"
    ]
    assert len(table_items) == 1
    assert table_items[0].rect() == QRectF(10, 20, 80, 60)
    assert len(labels) == 1
    assert labels[0].data(2) == "yolo"
    assert "YOLO" in labels[0].text()
    assert labels[0].pos().x() == pytest.approx(
        table_items[0].rect().right() - labels[0].boundingRect().width() - 2.0
    )
    assert labels[0].pos().y() == table_items[0].rect().top() + 2.0
    assert not labels[0].font().bold()
    assert labels[0].pen().style() == Qt.PenStyle.NoPen
    assert labels[0].brush().color().name() == "#a855f7"


def test_agent3_add_child_shortcut_uses_count_style_sequence_names() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    page.set_result(_result(), {})

    inv_item = page.structure_tree.topLevelItem(0)
    page.structure_tree.setCurrentItem(inv_item)
    page._add_child()
    assert page._tree.invs[0].nodes[-1].name == "CBX02"

    page._add_child()
    assert page._tree.invs[0].nodes[-1].rows[-1].name == "Row1"

    page.structure_tree.setCurrentItem(
        page.structure_tree.topLevelItem(0).child(1)
    )
    page._add_child()
    assert page._tree.invs[0].nodes[-1].rows[-1].name == "Row2"


def test_agent3_added_row_exposes_editable_racking_entry() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    page.set_result(_result(), {})

    page.structure_tree.setCurrentItem(
        page.structure_tree.topLevelItem(0).child(0)
    )
    page._add_row()

    row = page._object_at(page._selected_path())
    assert len(row.racking_entries) == 1
    assert page.racking_table.rowCount() == 1
    assert (
        page.racking_table.item(0, 0).flags()
        & Qt.ItemFlag.ItemIsEditable
    )

    page.racking_table.item(0, 0).setText("2P")
    page.racking_table.item(0, 1).setText("12str")

    updated = page._object_at(page._selected_path())
    assert updated.rackings == ("2P",)
    assert updated.racking_entries[0].rackings == "2P"
    assert updated.racking_entries[0].length_str == "12str"


def _click_edit_page() -> ElementRecognitionPage:
    """Return a page whose Row is selected so its racking table is filled."""

    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    base = _result()
    node = replace(
        base.reviewed_tree.invs[0].nodes[0],
        rows=(
            RowDraft(
                "Row1",
                racking_entries=(RackingEntryDraft("2 N", "0mm"),),
                recognition_id="p1:node:1:row:1",
            ),
        ),
    )
    result = replace(
        base,
        reviewed_tree=replace(
            base.reviewed_tree,
            invs=(
                replace(
                    base.reviewed_tree.invs[0],
                    nodes=(node,),
                ),
            ),
        ),
        pages=(
            replace(
                base.pages[0],
                table_rows=(
                    NodeStringTableRow("CBX01", 2),
                    NodeStringTableRow("CBX02", 3),
                ),
            ),
        ),
    )
    page.resize(1200, 800)
    page.show()
    page.set_request(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(1, b""),),
            tracker_definitions=(),
        )
    )
    page.set_result(result, {})
    page.structure_tree.setCurrentItem(
        page.structure_tree.topLevelItem(0).child(0).child(0)
    )
    app.processEvents()
    assert page.racking_table.rowCount() == 1
    return page


def _visible_editor(table) -> QLineEdit | None:
    """Return the open editor, skipping editors that are being replaced."""

    return next(
        (
            editor
            for editor in table.viewport().findChildren(QLineEdit)
            if editor.isVisible()
        ),
        None,
    )


def _cell_edge_point(table, row: int, column: int, *, end: bool) -> QPoint:
    rect = table.visualRect(table.model().index(row, column))
    x = rect.right() - 1 if end else rect.left() + 1
    return QPoint(x, rect.center().y())


def test_agent3_tables_edit_on_first_click_at_the_clicked_character() -> None:
    app = QApplication.instance() or QApplication([])
    page = _click_edit_page()

    racking = page.racking_table
    QTest.mouseClick(
        racking.viewport(),
        Qt.MouseButton.LeftButton,
        pos=_cell_edge_point(racking, 0, 0, end=True),
    )
    app.processEvents()
    editor = _visible_editor(racking)
    assert editor is not None
    # One click opens the editor at the clicked character instead of
    # selecting everything, so the next keystroke appends rather than
    # replacing the whole value.
    assert editor.text() == "2 N"
    assert editor.cursorPosition() == len(editor.text())
    assert not editor.hasSelectedText()

    QTest.mouseClick(
        racking.viewport(),
        Qt.MouseButton.LeftButton,
        pos=_cell_edge_point(racking, 0, 0, end=False),
    )
    app.processEvents()
    editor = _visible_editor(racking)
    assert editor is not None
    assert editor.text() == "2 N"
    assert editor.cursorPosition() == 0
    assert not editor.hasSelectedText()

    page.table_button.setChecked(True)
    app.processEvents()
    review = page.table_review_table
    assert review.rowCount() == 2
    QTest.mouseClick(
        review.viewport(),
        Qt.MouseButton.LeftButton,
        pos=_cell_edge_point(review, 0, 0, end=True),
    )
    app.processEvents()
    editor = _visible_editor(review)
    assert editor is not None
    assert editor.text() == "CBX01"
    assert editor.cursorPosition() == len(editor.text())
    assert not editor.hasSelectedText()
    # The clicked row stays current so Delete still targets it.
    assert review.currentRow() == 0


def test_agent3_table_drag_selects_text_inside_the_open_editor() -> None:
    app = QApplication.instance() or QApplication([])
    page = _click_edit_page()
    racking = page.racking_table

    start = _cell_edge_point(racking, 0, 0, end=False)
    end = _cell_edge_point(racking, 0, 0, end=True)
    QTest.mousePress(racking.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(racking.viewport(), end)
    QTest.mouseRelease(racking.viewport(), Qt.MouseButton.LeftButton, pos=end)
    app.processEvents()
    editor = _visible_editor(racking)
    assert editor is not None
    # Dragging across the cell selects text like dragging in a text field.
    assert editor.hasSelectedText()
    assert (editor.selectionStart(), editor.selectionEnd()) == (
        0,
        len(editor.text()),
    )


def test_agent3_ctrl_click_extends_rows_without_starting_an_edit() -> None:
    app = QApplication.instance() or QApplication([])
    page = _click_edit_page()
    page.table_button.setChecked(True)
    app.processEvents()
    review = page.table_review_table

    QTest.mouseClick(
        review.viewport(),
        Qt.MouseButton.LeftButton,
        pos=_cell_edge_point(review, 0, 0, end=False),
    )
    app.processEvents()
    assert _visible_editor(review) is not None

    QTest.mouseClick(
        review.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        _cell_edge_point(review, 1, 0, end=False),
    )
    app.processEvents()
    assert _visible_editor(review) is None
    assert sorted({index.row() for index in review.selectedIndexes()}) == [0, 1]


def test_agent3_review_uses_resizable_three_pane_layout_and_single_column_tree() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    # Visibility flags settle during show(); processEvents() would only buy
    # an offscreen paint of a page whose geometry is never asserted.
    page.show()

    assert page.main_splitter.count() == 2
    assert page.review_splitter.count() == 3
    assert not page.table_review_panel.isVisible()
    assert page.main_splitter.childrenCollapsible()
    assert page.review_splitter.childrenCollapsible()
    assert page.structure_tree.columnCount() == 1

    page.set_result(_result(), {})
    inv_item = page.structure_tree.topLevelItem(0)
    node_item = inv_item.child(0)
    row_item = node_item.child(0)

    page.structure_tree.setCurrentItem(inv_item)
    assert page.properties_form.isRowVisible(page.orientation_combo)
    assert not page.properties_form.isRowVisible(page.node_strings)
    assert page.racking_table.isHidden()

    page.structure_tree.setCurrentItem(node_item)
    assert page.properties_form.isRowVisible(page.node_strings)
    assert page.properties_form.isRowVisible(page.tracker_counts)

    page.structure_tree.setCurrentItem(row_item)
    assert not page.properties_form.isRowVisible(page.orientation_combo)
    assert not page.racking_table.isHidden()


def test_agent3_preserves_page_tree_and_splitter_state_across_refresh() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    page.resize(1200, 800)
    page.show()
    page.set_result(_result(), {})
    app.processEvents()

    node_item = page.structure_tree.topLevelItem(0).child(0)
    node_item.setExpanded(True)
    page.structure_tree.setCurrentItem(node_item)
    page.main_splitter.setSizes([700, 420])
    page.review_splitter.setSizes([310, 250])
    app.processEvents()
    main_sizes = page.main_splitter.sizes()
    review_sizes = page.review_splitter.sizes()

    page.set_result(_result(), {})
    app.processEvents()

    assert page.main_splitter.sizes() == main_sizes
    assert page.review_splitter.sizes() == review_sizes
    assert page.structure_tree.currentItem().data(
        0, Qt.ItemDataRole.UserRole + 1
    ) == "p1:node:1"
    assert page.structure_tree.currentItem().isExpanded()


def test_agent3_previews_request_page_before_recognition(
    shared_page: ElementRecognitionPage,
) -> None:
    page = shared_page
    page.set_request(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(4, _png()),),
            tracker_definitions=(TrackerDefinition("2-string", 2),),
            expected_inv_names=("INV 4",),
        )
    )

    assert page.page_combo.currentData() == 4
    assert page.page_combo.currentText().endswith("Page 4")
    assert len(page.canvas.scene().items()) == 1


def test_agent3_request_never_requires_page_inv_selection(
    shared_page: ElementRecognitionPage,
) -> None:
    page = shared_page
    request = ElementRecognitionRequest(
        pages=(
            ArrayLayoutPageInput(3, _png()),
            ArrayLayoutPageInput(4, _png()),
        ),
        tracker_definitions=(TrackerDefinition("2-string", 2),),
        expected_inv_names=("INV 101", "INV 102"),
    )
    page.set_request(request)

    assert [item.page_number for item in request.pages] == [3, 4]
    assert page.page_combo.count() == 2
    assert page.mapping_table.rowCount() == 2


def test_agent3_tree_selection_switches_to_detection_page() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    second_node = NodeDraft(name="CBX02", recognition_id="p2:node:1")
    result = _result()
    result = replace(
        result,
        reviewed_tree=replace(
            result.reviewed_tree,
            invs=(
                *result.reviewed_tree.invs,
                InvDraft(
                    name="INV 2",
                    nodes=(second_node,),
                    recognition_id="p2:inv",
                ),
            ),
        ),
        pages=(
            *result.pages,
            ElementPageResult(
                page_number=2,
                inv_name="INV 2",
                image_width=100,
                image_height=100,
                detections=(
                    ElementDetection(
                        "p2:node:1",
                        "Node",
                        0.95,
                        BoundingBox(10, 10, 90, 90),
                        2,
                    ),
                ),
            ),
        ),
    )
    page.set_result(result, {1: _png(), 2: _png()})
    page.page_combo.setCurrentIndex(0)

    second_item = page.structure_tree.topLevelItem(1).child(0)
    page.structure_tree.setCurrentItem(second_item)

    assert page.page_combo.currentData() == 2
    assert page.canvas.scene().selectedItems()[0].data(0) == "p2:node:1"


def test_agent3_tree_shortcuts_cover_rename_delete_and_reordering(
    shared_page: ElementRecognitionPage,
) -> None:
    page = shared_page
    sequences = {shortcut.key().toString() for shortcut in page._shortcuts}

    assert {
        "F2",
        "Del",
        "Alt+F",
        "Alt+D",
        "Alt+C",
        "Alt+V",
        "Alt+1",
        "Alt+2",
        "Alt+3",
        "Alt+Up",
        "Alt+Down",
    } <= sequences


def test_agent3_tree_multi_selection_sorts_and_reorders_siblings() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    result = _result()
    result = replace(
        result,
        reviewed_tree=replace(
            result.reviewed_tree,
            invs=(
                replace(
                    result.reviewed_tree.invs[0],
                    nodes=(
                        NodeDraft("Node10"),
                        NodeDraft("Fixed"),
                        NodeDraft("Node2"),
                    ),
                ),
            ),
        ),
    )
    page.set_result(result, {})
    inv_item = page.structure_tree.topLevelItem(0)
    inv_item.child(0).setSelected(True)
    inv_item.child(2).setSelected(True)

    page._sort_selected()
    assert [node.name for node in page._tree.invs[0].nodes] == [
        "Node2",
        "Fixed",
        "Node10",
    ]

    page._reorder_selected((0, 1), after=True)
    assert [node.name for node in page._tree.invs[0].nodes] == [
        "Fixed",
        "Node2",
        "Node10",
    ]


def test_agent3_tree_copy_paste_and_level_expansion() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    page.set_result(_result(), {})
    inv_item = page.structure_tree.topLevelItem(0)
    node_item = inv_item.child(0)
    page.structure_tree.setCurrentItem(node_item)
    page._copy_selected()
    page.structure_tree.setCurrentItem(inv_item)
    page._paste_to_selected()

    assert len(page._tree.invs[0].nodes) == 2
    assert page._tree.invs[0].nodes[1].recognition_id == ""
    assert page._tree.invs[0].nodes[1].rows[0].recognition_id == ""

    inv_item = page.structure_tree.topLevelItem(0)
    node_item = inv_item.child(0)
    page._expand_to_level(3)
    assert inv_item.isExpanded()
    assert node_item.isExpanded()
    page._expand_to_level(1)
    assert not inv_item.isExpanded()
    assert not node_item.isExpanded()


def test_agent3_tree_actions_add_rename_move_and_delete(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    page.set_result(_result(), {})
    changes = []
    page.review_changed.connect(changes.append)

    inv_item = page.structure_tree.topLevelItem(0)
    page.structure_tree.setCurrentItem(inv_item)
    page._add_node()
    page._add_node()
    assert page._selected_path() == (0, 2)

    page._move_selected(-1)
    assert page._selected_path() == (0, 1)
    assert [node.name for node in changes[-1].invs[0].nodes] == [
        "CBX01",
        "CBX03",
        "CBX02",
    ]

    selected = page.structure_tree.currentItem()
    selected.setText(0, "Renamed Node")
    assert changes[-1].invs[0].nodes[1].name == "Renamed Node"

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    page._delete_selected()
    assert [node.name for node in changes[-1].invs[0].nodes] == [
        "CBX01",
        "CBX02",
    ]


def _canvas_pixmap_item(canvas: DetectionCanvas) -> QGraphicsPixmapItem | None:
    return next(
        (
            item
            for item in canvas.scene().items()
            if isinstance(item, QGraphicsPixmapItem)
        ),
        None,
    )


def test_agent3_canvas_skips_rebuilding_an_unchanged_page_payload() -> None:
    app = QApplication.instance() or QApplication([])
    canvas = DetectionCanvas()
    canvas.resize(240, 180)
    canvas.show()
    page = _result().pages[0]
    image = _png()
    canvas.show_page(image, page)
    canvas.scale(4, 4)
    canvas.select_entity("p1:node:1")
    pixmap_item = _canvas_pixmap_item(canvas)
    node_rect = canvas._rects["p1:node:1"]
    horizontal = canvas.horizontalScrollBar()
    vertical = canvas.verticalScrollBar()
    horizontal.setValue(horizontal.maximum() // 2)
    vertical.setValue(vertical.maximum() // 2)
    scale = canvas.transform().m11()
    viewport_position = (horizontal.value(), vertical.value())

    canvas.show_page(image, page)

    assert canvas.transform().m11() == scale
    assert (horizontal.value(), vertical.value()) == viewport_position
    assert _canvas_pixmap_item(canvas) is pixmap_item
    assert canvas._rects["p1:node:1"] is node_rect
    assert canvas.scene().selectedItems()[0].data(0) == "p1:node:1"

    # A changed payload still redraws the scene.
    canvas.show_page(image, replace(page, detections=page.detections[:0]))
    assert _canvas_pixmap_item(canvas) is not pixmap_item
    assert not canvas._rects


def test_agent3_canvas_redraws_table_labels_after_language_change() -> None:
    app = QApplication.instance() or QApplication([])
    translations = TranslationManager.instance()
    previous_locale = translations.locale
    try:
        translations.locale = "en_US"
        canvas = DetectionCanvas()
        image = _png()
        page = ElementPageResult(
            1,
            "INV 1",
            100,
            100,
            table_bbox=BoundingBox(10, 20, 90, 80),
            table_bbox_source="yolo",
        )
        canvas.show_page(image, page)
        before = [
            item.text()
            for item in canvas.scene().items()
            if item.data(1) == "table_detection_label"
        ]
        translations.locale = "zh_CN"

        canvas.show_page(image, page)

        after = [
            item.text()
            for item in canvas.scene().items()
            if item.data(1) == "table_detection_label"
        ]
        assert before and after
        assert before != after
    finally:
        translations.locale = previous_locale


def test_agent3_tree_publish_keeps_preview_and_clears_detached_selection() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    result = _result()
    image = _png()
    page.set_result(result, {1: image})
    page.structure_tree.setCurrentItem(
        page.structure_tree.topLevelItem(0).child(0)
    )
    assert page.canvas.scene().selectedItems()

    def publish(tree):
        page.set_result(replace(result, reviewed_tree=tree), {1: image})

    page.review_changed.connect(publish)
    page.canvas.scale(3, 3)
    pixmap_item = _canvas_pixmap_item(page.canvas)
    scale = page.canvas.transform().m11()

    page._add_node()

    # The workflow round-trip must not rebuild the preview nor reset zoom.
    assert _canvas_pixmap_item(page.canvas) is pixmap_item
    assert page.canvas.transform().m11() == scale
    # The added Node has no recognition link, so no stale box stays selected.
    assert (
        page.structure_tree.currentItem().data(
            0, Qt.ItemDataRole.UserRole + 1
        )
        == ""
    )
    assert not page.canvas.scene().selectedItems()


def test_agent3_clicking_pasted_detached_node_clears_canvas_selection() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    page.set_result(_result(), {1: _png()})
    page.structure_tree.setCurrentItem(
        page.structure_tree.topLevelItem(0).child(0)
    )
    assert page.canvas.scene().selectedItems()

    page._copy_selected()
    page.structure_tree.setCurrentItem(page.structure_tree.topLevelItem(0))
    page._paste_to_selected()
    pasted = page.structure_tree.currentItem()
    assert pasted.data(0, Qt.ItemDataRole.UserRole + 1) == ""

    page.structure_tree.setCurrentItem(
        page.structure_tree.topLevelItem(0).child(0)
    )
    assert page.canvas.scene().selectedItems()

    page.structure_tree.setCurrentItem(
        page.structure_tree.topLevelItem(0).child(1)
    )
    assert not page.canvas.scene().selectedItems()


def test_agent3_deleting_node_with_stale_widget_path_does_not_crash(
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    result = _result()
    second = NodeDraft("CBX02", recognition_id="p1:node:2")
    result = replace(
        result,
        reviewed_tree=replace(
            result.reviewed_tree,
            invs=(
                replace(
                    result.reviewed_tree.invs[0],
                    nodes=(*result.reviewed_tree.invs[0].nodes, second),
                ),
            ),
        ),
    )
    page.set_result(result, {1: _png()})

    # The workflow publish runs synchronously while the widget still holds the
    # deleted Node's path, so readers must tolerate a stale path.  PySide
    # swallows an exception raised inside a slot, so assert the publish ran to
    # completion instead of relying on the traceback reaching pytest.
    published = []

    def publish(tree):
        page.set_result(replace(page._result, reviewed_tree=tree), {1: _png()})
        published.append(tree)

    page.review_changed.connect(publish)
    page.structure_tree.setCurrentItem(
        page.structure_tree.topLevelItem(0).child(1)
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    page._delete_selected()

    assert published
    assert [node.name for node in page._tree.invs[0].nodes] == ["CBX01"]
    assert page.structure_tree.topLevelItem(0).childCount() == 1
    assert page.structure_tree.currentItem().data(
        0, Qt.ItemDataRole.UserRole + 1
    ) == "p1:inv"


def test_agent3_tree_edit_preserves_scroll_position() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    page.resize(900, 300)
    page.show()
    nodes = tuple(NodeDraft(f"CBX{index:03d}") for index in range(1, 41))
    page.set_result(
        ElementRecognitionResult(
            reviewed_tree=ReviewedTree(invs=(InvDraft("INV 1", nodes=nodes),))
        ),
        {},
    )
    page._expand_to_level(2)
    app.processEvents()
    middle = page.structure_tree.topLevelItem(0).child(20)
    page.structure_tree.setCurrentItem(middle)
    scrollbar = page.structure_tree.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(scrollbar.maximum() // 2)
    app.processEvents()
    before = scrollbar.value()

    middle.setText(0, "CBX-renamed")
    app.processEvents()

    assert page.structure_tree.currentItem().text(0) == "CBX-renamed"
    assert page._tree.invs[0].nodes[20].name == "CBX-renamed"
    assert scrollbar.value() == before

    # An externally refreshed result must also keep the viewport in place.
    refreshed = replace(page._result, reviewed_tree=replace(page._tree))
    page.set_result(refreshed, {})
    app.processEvents()

    assert page.structure_tree.topLevelItem(0).child(20).text(0) == "CBX-renamed"
    assert scrollbar.value() == before

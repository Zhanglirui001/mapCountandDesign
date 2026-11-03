from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest

from count_and_design.application.ai import BoundingBox
from count_and_design.application.ai_statistics import (
    AIStatisticsStage,
    AIStatisticsStageStatus,
    AIStatisticsWorkflow,
    STRATEGY_MINERU_CLAUDE,
    Agent1Cancelled,
    Agent1NoRelevantDrawingPagesError,
    Agent1ProviderError,
    Agent4ValidationService,
    DrawingPageType,
    ElementPageResult,
    ElementRecognitionResult,
    FakePageClassificationProvider,
    NodeStringTableRow,
    NumStrDetail,
    OverallSiteExtraction,
    PageClassification,
    PageClassificationResult,
    RecognitionState,
    SourceReference,
    TrackerFieldValue,
    TrackerOcrFields,
    ValidationDifference,
    ValidationReport,
    element_review_to_validation_payload,
    import_agent4_payload_to_project,
    reviewed_tree_to_validation_payload,
    validation_report_to_export_payload,
)
from count_and_design.application.ai_statistics.info_recognition.overall_site import (
    STRATEGY_MINERU_GLM,
)
from count_and_design.application.ai_statistics.models import (
    InvDraft,
    NodeDraft,
    ReviewedTree,
    TrackerQuantity,
)
from count_and_design.gui.viewmodels.ai_statistics import AIStatisticsViewModel
from conftest import (
    sample_agent2_extraction,
    sample_agent2_validation_payload,
    sample_agent3_reviewed_tree,
    sample_agent3_validation_payload,
)
from count_and_design.models import Project


def test_ai_statistics_workflow_owns_stage_transitions(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    pdf_path = tmp_path / "drawing.pdf"

    workflow.load_pdf(pdf_path, page_count=3)
    assert workflow.snapshot.pdf_path == pdf_path
    assert workflow.is_run_enabled()
    assert workflow.snapshot.active_stage is None

    result = workflow.run_page_classification()

    assert result.total_pages == 3
    assert result.pages[0].page_number == 1
    assert workflow.status_for(AIStatisticsStage.PAGE_CLASSIFICATION) == (
        AIStatisticsStageStatus.RESULT_READY
    )
    assert workflow.is_stage_clickable(AIStatisticsStage.PAGE_CLASSIFICATION)
    assert workflow.is_confirm_enabled(AIStatisticsStage.PAGE_CLASSIFICATION)

    snapshot = workflow.confirm_current_stage()

    assert snapshot.selected_stage == AIStatisticsStage.INFO_RECOGNITION
    assert snapshot.active_stage == AIStatisticsStage.INFO_RECOGNITION
    assert workflow.status_for(AIStatisticsStage.PAGE_CLASSIFICATION) == (
        AIStatisticsStageStatus.CONFIRMED
    )
    assert workflow.is_confirm_enabled(AIStatisticsStage.INFO_RECOGNITION)


def test_ai_statistics_contracts_keep_traceability_metadata() -> None:
    source = SourceReference(
        page_number=12,
        bbox=BoundingBox(1, 2, 30, 40),
        confidence=0.91,
        model_version="node-box-v1",
        recognition_version="agent3-draft",
        source="ultralytics:box",
    )
    report = ValidationReport(
        differences=(
            ValidationDifference(
                page_number=12,
                inv_name="INV1101",
                node_name="CBX01",
                calculated_total=71,
                ocr_total=74,
                source=source,
            ),
        )
    )

    assert report.differences[0].source.bbox.to_tuple() == (1, 2, 30, 40)
    assert report.differences[0].diff == -3
    assert not report.passed


def test_ai_statistics_viewmodel_presents_workflow_state(tmp_path) -> None:
    view_model = AIStatisticsViewModel(
        AIStatisticsWorkflow(FakePageClassificationProvider())
    )
    pdf_path = tmp_path / "drawing.pdf"

    assert not view_model.state.run_enabled

    view_model.load_pdf(pdf_path, 2)
    assert view_model.state.run_enabled
    assert view_model.state.confirm_enabled == (False, False, False)

    view_model.run_ai_model()
    assert view_model.state.stage_statuses[0].fallback == "result ready"
    assert view_model.state.confirm_enabled[0]
    assert view_model.state.furthest_stage == 0

    next_stage = view_model.confirm_current_stage()

    assert next_stage == 1
    assert view_model.state.current_stage == 1
    assert view_model.state.stage_statuses[0].fallback == "confirmed"
    assert view_model.state.confirm_enabled[1]


def test_fake_agent1_result_can_be_reviewed(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", page_count=4)

    result = workflow.run_page_classification()
    thumbnails = result.thumbnails
    page_images = result.page_images

    assert [page.page_type for page in result.pages] == [
        DrawingPageType.ARRAY_LAYOUT.value,
        DrawingPageType.ARRAY_LAYOUT.value,
        DrawingPageType.OVERALL_LAYOUT.value,
        DrawingPageType.ARRAY_LAYOUT.value,
    ]
    assert result.pages[0].source.model_version == "fake-agent1-v1"

    snapshot = workflow.update_page_classification(
        1,
        DrawingPageType.OTHER.value,
    )

    reviewed = snapshot.page_classification.pages[0]
    assert reviewed.page_type == DrawingPageType.OTHER.value
    assert reviewed.state == RecognitionState.USER_CONFIRMED
    assert snapshot.page_classification.thumbnails == thumbnails
    assert snapshot.page_classification.page_images == page_images


class FailingAgent1Provider:
    def classify(self, *_args, **_kwargs):
        raise Agent1ProviderError("embedding service unavailable")


class IrrelevantPdfAgent1Provider:
    def classify(self, *_args, **_kwargs):
        return PageClassificationResult(
            pages=(
                PageClassification(
                    page_number=1,
                    page_type=DrawingPageType.OTHER.value,
                    purpose="unknown",
                ),
            )
        )


def test_workflow_rejects_pdf_without_layout_pages(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(IrrelevantPdfAgent1Provider())
    workflow.load_pdf(tmp_path / "unrelated.pdf", 1)

    with pytest.raises(
        Agent1NoRelevantDrawingPagesError,
        match="may not be a drawing for the current project",
    ):
        workflow.run_page_classification()

    assert workflow.status_for(AIStatisticsStage.PAGE_CLASSIFICATION) == (
        AIStatisticsStageStatus.FAILED
    )
    assert workflow.snapshot.page_classification is None
    assert not workflow.is_confirm_enabled(
        AIStatisticsStage.PAGE_CLASSIFICATION
    )
    assert workflow.is_run_enabled()


def test_workflow_records_agent1_failure_and_allows_retry(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(FailingAgent1Provider())
    workflow.load_pdf(tmp_path / "drawing.pdf", 1)

    with pytest.raises(Agent1ProviderError):
        workflow.run_page_classification()

    assert workflow.status_for(AIStatisticsStage.PAGE_CLASSIFICATION) == (
        AIStatisticsStageStatus.FAILED
    )
    assert workflow.snapshot.error_message == "embedding service unavailable"
    assert workflow.is_run_enabled()


def test_workflow_resets_running_state_after_cancellation(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", 1)
    workflow.begin_page_classification()

    with pytest.raises(Agent1Cancelled):
        workflow.classify_page_document(is_cancelled=lambda: True)
    workflow.cancel_page_classification()

    assert workflow.status_for(AIStatisticsStage.PAGE_CLASSIFICATION) == (
        AIStatisticsStageStatus.WAITING
    )
    assert workflow.snapshot.classification_progress_total == 0
    assert workflow.is_run_enabled()

def test_agent4_validation_service_validates_agent2_and_agent3_payloads() -> None:
    tracker_totals_payload = sample_agent2_validation_payload()
    structure_payload = sample_agent3_validation_payload()
    reviewed_tree = sample_agent3_reviewed_tree()

    assert all(
        "tracker_strings" not in tracker_total
        for tracker_total in tracker_totals_payload["tracker_totals"]
    )
    report = Agent4ValidationService().validate(
        tracker_totals_payload,
        structure_payload,
    )

    assert report.reviewed_tree is not None
    assert report.reviewed_tree.project_type == "LYNX"
    first_total = tracker_totals_payload["tracker_totals"][0]
    first_power, first_count = next(iter(first_total["power_counts"].items()))
    assert report.tracker_totals[0].fields[0].name == first_power
    assert report.tracker_totals[0].fields[0].value == str(first_count)
    assert report.inv_comparisons[0].inv_name == "INV1"
    assert report.inv_comparisons[0].page == 12
    assert report.inv_comparisons[0].calculated_total == 125
    assert report.inv_comparisons[0].ocr_total == 128
    assert report.inv_comparisons[0].diff == 3
    assert report.inv_comparisons[1].calculated_total == 319
    assert report.inv_comparisons[1].diff == 0

    expected_structure_totals: dict[str, int] = {}
    for inv in reviewed_tree.invs:
        for node in inv.nodes:
            for tracker in node.tracker_counts:
                expected_structure_totals[tracker.tracker_type] = (
                    expected_structure_totals.get(tracker.tracker_type, 0)
                    + tracker.tracker_count
                )
    assert report.calculated_grand_total == sum(expected_structure_totals.values())
    assert report.ocr_grand_total == tracker_totals_payload["grand_total"]
    for comparison in report.tracker_comparisons:
        assert comparison.structure_total == expected_structure_totals.get(
            comparison.tracker_type,
            0,
        )

    payload = validation_report_to_export_payload(report)
    assert payload["project_type"] == "LYNX"
    assert "page" not in payload["invs"][0]
    assert payload["length_identifiers"] == [
        {"name": "str", "label": "Fs6a"},
        {"name": "str_a", "label": "BYD"},
    ]
    first_node = payload["invs"][0]["nodes"][0]
    assert "ocr_string_count" not in first_node
    assert "tracker_num" not in first_node
    first_entry = first_node["rows"][0]["racking_entries"][0]
    assert first_entry == {
        "rackings": "3N",
        "offset": {"length_str": "12str"},
    }

    project = Project()
    import_agent4_payload_to_project(project, payload)
    assert project.project_type == "LYNX"
    assert project.length_identifiers[0].name == "str"
    assert project.length_identifiers[0].label == "Fs6a"
    assert project.invs[0].name == "INV1"
    assert project.invs[0].nodes[0].box_position == 0.0
    assert project.invs[0].nodes[0].rows[0].racking_entries[0].rackings == "3N"
    assert (
        project.invs[0]
        .nodes[0]
        .rows[0]
        .racking_entries[0]
        .offset.length_str
        == "12str"
    )
    assert {rackings.name for rackings in project.rackings_list} >= {"3N", "3S"}
    assert not report.passed


def test_agent4_validation_marks_missing_layout_ocr_as_failed() -> None:
    tree = ReviewedTree(
        invs=(
            InvDraft(
                name="INV1101",
                page=1,
                nodes=(
                    NodeDraft(
                        name="INV1101-CBX-12",
                        ocr_string_count=96,
                        has_ocr_string_count=True,
                        tracker_counts=(TrackerQuantity("12-string", 8),),
                    ),
                    NodeDraft(
                        name="Node7",
                        ocr_string_count=0,
                        has_ocr_string_count=False,
                        tracker_counts=(),
                    ),
                ),
            ),
        ),
    )
    payload = reviewed_tree_to_validation_payload(tree)
    report = Agent4ValidationService().validate(
        {"tracker_totals": [], "grand_total": 96},
        payload,
    )

    node_comparison = report.inv_comparisons[0].nodes[1]
    assert node_comparison.node_name == "Node7"
    assert node_comparison.calculated_total == 0
    assert node_comparison.ocr_total == 0
    assert node_comparison.ocr_total_present is False
    assert not node_comparison.passed
    assert not report.inv_comparisons[0].passed


def test_agent4_report_keeps_agent3_entity_links_for_navigation() -> None:
    tree = ReviewedTree(
        invs=(
            InvDraft(
                name="INV1",
                page=12,
                recognition_id="p12:inv",
                nodes=(
                    NodeDraft(
                        name="Node1",
                        ocr_string_count=9,
                        has_ocr_string_count=True,
                        tracker_counts=(TrackerQuantity("9-string", 1),),
                        recognition_id="p12:node:1",
                    ),
                ),
            ),
        ),
    )

    report = Agent4ValidationService().validate(
        {"tracker_totals": [], "grand_total": 9},
        reviewed_tree_to_validation_payload(tree),
    )

    comparison = report.inv_comparisons[0]
    assert comparison.recognition_id == "p12:inv"
    assert comparison.nodes[0].recognition_id == "p12:node:1"
    # The Count import payload must stay free of Agent3 navigation links.
    payload = validation_report_to_export_payload(report)
    assert "recognition_id" not in payload["invs"][0]
    assert "recognition_id" not in payload["invs"][0]["nodes"][0]


def test_agent4_node_comparison_shows_union_of_table_rows_and_structure() -> None:
    tree = ReviewedTree(
        invs=(
            InvDraft(
                name="INV1101",
                page=13,
                nodes=(
                    NodeDraft(
                        name="INV1101-CBX-01",
                        ocr_string_count=88,
                        has_ocr_string_count=True,
                        tracker_counts=(TrackerQuantity("11-string", 8),),
                    ),
                ),
            ),
        ),
    )
    payload = reviewed_tree_to_validation_payload(tree)
    payload["table_rows"] = [
        {
            "inv_name": "INV1101",
            "page": 13,
            "node": "INV1101-CBX-01",
            "string_number": 88,
        },
        {
            "inv_name": "INV1101",
            "page": 13,
            "node": "INV1101-CBX-02",
            "string_number": 66,
        },
    ]

    report = Agent4ValidationService().validate(
        {"tracker_totals": [], "grand_total": 154},
        payload,
    )

    inv = report.inv_comparisons[0]
    # The matched row stays a single structure Node; the table row YOLO
    # missed is appended so Node Details shows the union, not the
    # intersection.
    assert [node.node_name for node in inv.nodes] == [
        "INV1101-CBX-01",
        "INV1101-CBX-02",
    ]
    table_only = inv.nodes[1]
    assert table_only.calculated_total == 0
    assert table_only.ocr_total == 66
    assert table_only.ocr_total_present is True
    assert table_only.diff == 66
    assert not table_only.passed
    assert table_only.source.page_number == 13
    assert inv.ocr_total == 154
    assert not inv.passed
    assert [difference.node_name for difference in report.differences] == [
        "INV1101-CBX-01",
        "INV1101-CBX-02",
    ]


def test_agent4_payload_attaches_table_rows_to_renamed_inv_branch() -> None:
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(
                InvDraft(
                    name="INV1101-FIXED",
                    page=13,
                    nodes=(
                        NodeDraft(
                            name="INV1101-CBX-01",
                            source=SourceReference(page_number=13),
                            recognition_id="p13:node:1",
                            ocr_string_count=88,
                            has_ocr_string_count=True,
                        ),
                    ),
                ),
            ),
        ),
        pages=(
            ElementPageResult(
                page_number=13,
                inv_name="INV1101",
                image_width=100,
                image_height=100,
                table_rows=(
                    NodeStringTableRow("INV1101-CBX-01", 88),
                    NodeStringTableRow("INV1101-CBX-02", 66),
                ),
            ),
        ),
    )

    payload = element_review_to_validation_payload(result)

    # The page keeps the recognition-time INV name; the Node page anchor
    # still binds its table rows to the renamed branch.
    assert all(
        row["inv_name"] == "INV1101-FIXED"
        for row in payload["table_rows"]
    )
    assert [row["node"] for row in payload["table_rows"]] == [
        "INV1101-CBX-01",
        "INV1101-CBX-02",
    ]


def test_agent4_payload_drops_table_rows_of_a_deleted_inv_branch() -> None:
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV2", page=14),),
        ),
        pages=(
            ElementPageResult(
                page_number=13,
                inv_name="INV1",
                image_width=100,
                image_height=100,
                table_rows=(NodeStringTableRow("INV1-CBX-01", 8),),
            ),
        ),
    )

    payload = element_review_to_validation_payload(result)

    # INV1's branch was deleted, so its page has no comparison row left to
    # attach the table OCR reference to.
    assert payload["table_rows"] == []


def test_workflow_passes_adapted_real_agent2_data_to_agent4() -> None:
    validation_service = Mock(wraps=Agent4ValidationService())
    workflow = AIStatisticsWorkflow(
        FakePageClassificationProvider(),
        validation_service=validation_service,
    )

    def field(label: str, value: str) -> TrackerFieldValue:
        return TrackerFieldValue(label=label, meaning="", value=value)

    extraction = OverallSiteExtraction(
        page_number=1,
        tracker_ocr_fields=(
            TrackerOcrFields(
                module_model=field("MODULE MODEL", "ABC"),
                module_stc_dc_rating_w=field("POWER", "400W"),
                string_count=field("STRING COUNT", "123"),
                num_str=field("Num-STR", "13-STR"),
                num_str_details=(NumStrDetail("13-STR", "7"),),
            ),
        ),
    )

    workflow.load_pdf("runtime-agent4-test.pdf", page_count=1)
    workflow.run_page_classification()
    workflow.confirm_current_stage()
    workflow.complete_overall_site_recognition((extraction,))
    workflow.confirm_current_stage()
    element_result = ElementRecognitionResult(
        reviewed_tree=sample_agent3_reviewed_tree()
    )
    workflow.complete_element_recognition(element_result)
    workflow.confirm_current_stage()

    validation_service.validate.assert_called_once_with(
        {
            "tracker_totals": [
                {
                    "tracker_type": "13-string",
                    "power_counts": {"400W": 7},
                    "total": 7,
                }
            ],
            "grand_total": 7,
            "inv_string_counts": [],
        },
        element_review_to_validation_payload(element_result),
    )


def test_workflow_agent4_keeps_table_only_nodes_from_agent3_pages() -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf("drawing.pdf", page_count=1)
    workflow.run_page_classification()
    workflow.confirm_current_stage()
    workflow.complete_overall_site_recognition((sample_agent2_extraction(),))
    workflow.confirm_current_stage()

    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(
                InvDraft(
                    name="INV1101",
                    page=13,
                    recognition_id="p13:inv",
                    nodes=(
                        NodeDraft(
                            name="INV1101-CBX-01",
                            source=SourceReference(page_number=13),
                            recognition_id="p13:node:1",
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
                page_number=13,
                inv_name="INV1101",
                image_width=100,
                image_height=100,
                table_rows=(
                    NodeStringTableRow("INV1101-CBX-01", 88),
                    NodeStringTableRow("INV1101-CBX-02", 66),
                ),
            ),
        ),
    )
    workflow.complete_element_recognition(result)
    snapshot = workflow.confirm_current_stage()

    report = snapshot.validation_report
    assert report is not None
    inv = report.inv_comparisons[0]
    assert [node.node_name for node in inv.nodes] == [
        "INV1101-CBX-01",
        "INV1101-CBX-02",
    ]
    assert inv.nodes[1].calculated_total == 0
    assert inv.nodes[1].ocr_total == 66
    assert not inv.nodes[1].passed
    assert not inv.passed


def test_ai_statistics_workflow_generates_agent4_report(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", page_count=1)
    workflow.run_page_classification()
    workflow.confirm_current_stage()
    workflow.complete_overall_site_recognition((sample_agent2_extraction(),))
    workflow.confirm_current_stage()
    workflow.complete_element_recognition(
        ElementRecognitionResult(reviewed_tree=sample_agent3_reviewed_tree())
    )
    snapshot = workflow.confirm_current_stage()

    assert snapshot.selected_stage == AIStatisticsStage.VALIDATION
    assert workflow.status_for(AIStatisticsStage.VALIDATION) == (
        AIStatisticsStageStatus.RESULT_READY
    )
    assert snapshot.validation_report is not None
    assert snapshot.validation_report.inv_comparisons[0].nodes[0].diff == 3


def test_agent2_review_edit_invalidates_stale_agent4_report(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", page_count=1)
    workflow.run_page_classification()
    workflow.confirm_current_stage()
    workflow.complete_overall_site_recognition((sample_agent2_extraction(),))
    workflow.confirm_current_stage()
    workflow.complete_element_recognition(
        ElementRecognitionResult(reviewed_tree=sample_agent3_reviewed_tree())
    )
    workflow.confirm_current_stage()
    assert workflow.snapshot.validation_report is not None
    assert workflow.status_for(AIStatisticsStage.VALIDATION) == (
        AIStatisticsStageStatus.RESULT_READY
    )

    workflow.set_current_stage(AIStatisticsStage.INFO_RECOGNITION.index)
    workflow.update_overall_site_review((sample_agent2_extraction(),), "P-B")

    # Agent4 compared the structure against the previous Agent2 totals; the
    # edited review must not leave that report downloadable.
    assert workflow.snapshot.validation_report is None
    assert workflow.status_for(AIStatisticsStage.VALIDATION) == (
        AIStatisticsStageStatus.WAITING
    )
    assert workflow.snapshot.element_recognition_result is not None


def test_agent3_rerun_invalidates_stale_agent4_report(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", page_count=1)
    workflow.run_page_classification()
    workflow.confirm_current_stage()
    workflow.complete_overall_site_recognition((sample_agent2_extraction(),))
    workflow.confirm_current_stage()
    workflow.complete_element_recognition(
        ElementRecognitionResult(reviewed_tree=sample_agent3_reviewed_tree())
    )
    workflow.confirm_current_stage()
    assert workflow.snapshot.validation_report is not None

    workflow.set_current_stage(AIStatisticsStage.ELEMENT_RECOGNITION.index)
    workflow.begin_element_recognition()

    # The new Agent3 run will replace the structure this report validated.
    assert workflow.snapshot.validation_report is None
    assert workflow.status_for(AIStatisticsStage.VALIDATION) == (
        AIStatisticsStageStatus.WAITING
    )
    assert workflow.snapshot.element_recognition_result is not None


def test_overall_site_retry_run_detects_previous_results(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", page_count=1)
    workflow.run_page_classification()
    workflow.confirm_current_stage()

    assert not workflow.is_overall_site_retry_run()

    workflow.complete_overall_site_recognition((sample_agent2_extraction(),))

    assert workflow.is_overall_site_retry_run()


def test_overall_site_retry_keeps_glm_vision_and_switches_locator(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying switches YOLO -> MinerU without dropping the GLM provider."""

    monkeypatch.setenv("AGENT2_OVERALL_TABLE_STRATEGY", "yolo_glm")
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", page_count=1)
    workflow.run_page_classification()
    workflow.confirm_current_stage()
    workflow.complete_overall_site_recognition((sample_agent2_extraction(),))

    recognizer = Mock()
    workflow._overall_site_recognizer = recognizer
    configured = workflow._configure_overall_site_recognizer(
        retry=workflow.is_overall_site_retry_run(),
    )

    assert configured is recognizer
    assert recognizer.config.strategy == STRATEGY_MINERU_GLM
    assert recognizer.config.uses_glm is True
    assert recognizer.config.vision_provider == "openrouter"


def test_overall_site_retry_uses_mineru_strategy(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT2_OVERALL_TABLE_STRATEGY", "yolo_claude")
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", page_count=1)
    workflow.run_page_classification()
    workflow.confirm_current_stage()
    workflow.complete_overall_site_recognition((sample_agent2_extraction(),))

    recognizer = Mock()
    workflow._overall_site_recognizer = recognizer
    configured = workflow._configure_overall_site_recognizer(
        retry=workflow.is_overall_site_retry_run(),
    )

    assert configured is recognizer
    assert recognizer.config.strategy == STRATEGY_MINERU_CLAUDE


def test_overall_site_not_retry_after_agent1_edit_and_reconfirm(
    tmp_path,
) -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", page_count=4)
    workflow.run_page_classification()
    workflow.confirm_current_stage()
    workflow.complete_overall_site_recognition((sample_agent2_extraction(),))

    assert workflow.is_overall_site_retry_run()

    workflow.set_current_stage(AIStatisticsStage.PAGE_CLASSIFICATION.index)
    workflow.update_page_classification(
        3,
        DrawingPageType.ARRAY_LAYOUT.value,
    )
    workflow.confirm_current_stage()

    assert not workflow.is_overall_site_retry_run()
    assert workflow.snapshot.overall_site_extractions == ()
    assert workflow.status_for(AIStatisticsStage.INFO_RECOGNITION) == (
        AIStatisticsStageStatus.WAITING
    )


def test_overall_site_still_retry_when_staying_on_agent2(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", page_count=4)
    workflow.run_page_classification()
    workflow.confirm_current_stage()
    workflow.complete_overall_site_recognition((sample_agent2_extraction(),))

    assert workflow.is_overall_site_retry_run()


def test_overall_site_rerun_enabled_after_agent3(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", page_count=4)
    classification = workflow.run_page_classification()
    workflow.complete_page_classification(
        replace(
            classification,
            page_images={3: b"overall-layout-page"},
        )
    )
    workflow.confirm_current_stage()
    workflow.complete_overall_site_recognition((sample_agent2_extraction(),))
    workflow.update_overall_site_review(
        (sample_agent2_extraction(),),
        "P-B",
    )
    workflow.confirm_current_stage()
    workflow.complete_element_recognition(
        ElementRecognitionResult(reviewed_tree=sample_agent3_reviewed_tree())
    )

    workflow.set_current_stage(AIStatisticsStage.INFO_RECOGNITION.index)

    assert workflow.is_overall_site_run_enabled()
    assert workflow.is_overall_site_retry_run()
    workflow.begin_overall_site_recognition()
    assert workflow.status_for(AIStatisticsStage.INFO_RECOGNITION) == (
        AIStatisticsStageStatus.RUNNING
    )
    assert workflow.snapshot.element_recognition_result is None


def test_page_classification_rerun_enabled_after_agent2(tmp_path) -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(tmp_path / "drawing.pdf", page_count=4)
    workflow.run_page_classification()
    workflow.confirm_current_stage()
    workflow.complete_overall_site_recognition((sample_agent2_extraction(),))

    workflow.set_current_stage(AIStatisticsStage.PAGE_CLASSIFICATION.index)

    assert workflow.is_run_enabled()
    workflow.begin_page_classification()
    assert workflow.status_for(AIStatisticsStage.PAGE_CLASSIFICATION) == (
        AIStatisticsStageStatus.RUNNING
    )
    assert workflow.snapshot.overall_site_extractions == ()

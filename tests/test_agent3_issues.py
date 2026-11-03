from dataclasses import replace

import pytest

from count_and_design.application.ai import BoundingBox
from count_and_design.application.ai_statistics.element_recognition.contracts import (
    ElementDetection,
    ElementPageResult,
    ElementRecognitionResult,
    NodeStringTableRow,
    RecognitionIssueSeverity,
)
from count_and_design.application.ai_statistics.element_recognition.issue_review import (
    refresh_review_issues,
    with_issues,
)
from count_and_design.application.ai_statistics.element_recognition.issues import (
    ISSUE_DEFINITIONS,
    IssueCode,
    create_issue,
)
from count_and_design.application.ai_statistics.models import (
    DetectionAnnotation,
    InvDraft,
    NodeDraft,
    RackingEntryDraft,
    ReviewedTree,
    RowDraft,
    SourceReference,
)


def _node(number, name="CBX01", strings=2):
    source = SourceReference(
        page_number=1, bbox=BoundingBox(number * 100, 0, number * 100 + 90, 90)
    )
    return NodeDraft(
        name=name,
        recognition_id=f"p1:node:{number}",
        source=source,
        annotations=(DetectionAnnotation(name, source),),
        rows=(
            RowDraft(
                "Row1", racking_entries=(RackingEntryDraft(rackings=f"{strings} P"),)
            ),
        ),
        ocr_string_count=2,
        has_ocr_string_count=True,
    )


def _result(*nodes, issues=()):
    return with_issues(
        ElementRecognitionResult(
            reviewed_tree=ReviewedTree(
                invs=(InvDraft("INV1", page=1, recognition_id="p1:inv", nodes=nodes),)
            ),
            pages=(
                ElementPageResult(
                    1,
                    "INV1",
                    400,
                    400,
                    table_rows=(
                        NodeStringTableRow("CBX01", 2),
                        NodeStringTableRow("CBX02", 2),
                    ),
                    detections=tuple(
                        ElementDetection(n.recognition_id, "Node", 1, n.source.bbox, 1)
                        for n in nodes
                    ),
                ),
            ),
        ),
        issues,
    )


def _edit(result, *nodes):
    return replace(
        result,
        reviewed_tree=replace(
            result.reviewed_tree,
            invs=(replace(result.reviewed_tree.invs[0], nodes=nodes),),
        ),
    )


def test_catalog_requires_registered_codes_and_template_context():
    assert set(ISSUE_DEFINITIONS) == set(IssueCode)
    with pytest.raises(KeyError):
        create_issue("typo")
    with pytest.raises(KeyError):
        create_issue(IssueCode.NODE_NAME_UNMATCHED)
    assert (
        create_issue(IssueCode.NODE_NAME_UNMATCHED, name="A").severity
        == RecognitionIssueSeverity.WARNING
    )
    assert (
        create_issue(IssueCode.NODE_NAME_REVIEW_FAILED, detail="offline").severity
        == RecognitionIssueSeverity.ERROR
    )


def test_all_issues_prefers_current_page_copy_and_stable_identity():
    old = create_issue(
        IssueCode.NODE_NAME_UNMATCHED, name="Old", page_number=1, entity_id="n1"
    )
    current = create_issue(
        IssueCode.NODE_NAME_UNMATCHED, name="New", page_number=1, entity_id="n1"
    )
    result = replace(_result(_node(1), issues=(current,)), issues=(old,))
    assert old.key == current.key
    assert result.all_issues == (current,)
    normalized = with_issues(result, result.all_issues)
    assert normalized.issues == ()
    assert normalized.to_dict()["pages"][0]["issues"][0]["message"] == current.message


def test_rename_clears_duplicate_and_placeholder_but_keeps_execution_failure():
    first, second = _node(1), _node(2)
    failures = [
        create_issue(
            IssueCode.DUPLICATE_NODE_NAME,
            name=n.name,
            entity_id=n.recognition_id,
            page_number=1,
        )
        for n in (first, second)
    ]
    failures += [
        create_issue(
            IssueCode.NODE_NAME_NO_MATCH,
            name=second.name,
            entity_id=second.recognition_id,
            page_number=1,
        ),
        create_issue(
            IssueCode.NODE_NAME_OCR_FAILED,
            detail="timeout",
            entity_id=second.recognition_id,
            page_number=1,
        ),
    ]
    previous = _result(first, second, issues=failures)
    updated = refresh_review_issues(
        _edit(previous, first, replace(second, name="CBX02")), previous
    )
    assert [issue.code for issue in updated.all_issues] == ["node_name_ocr_failed"]
    assert updated.pages[0].detections[0].issues == ()
    assert updated.reviewed_tree.invs[0].nodes[0].issues == ()
    assert updated.reviewed_tree.invs[0].nodes[1].issues == ("node_name_ocr_failed",)


def test_row_edits_add_and_clear_balance_issues_without_rewriting_ocr_reference():
    node = _node(1)
    previous = _result(node)
    changed = replace(node, rows=_node(1, strings=3).rows)
    invalid = refresh_review_issues(_edit(previous, changed), previous)
    assert [i.code for i in invalid.all_issues] == ["tracker_string_balance_failed", "table_node_not_detected"]
    repaired = refresh_review_issues(_edit(invalid, node), invalid)
    assert [i.code for i in repaired.all_issues] == ["table_node_not_detected"]
    assert repaired.all_issues[0].params["name"] == "CBX02"
    assert repaired.reviewed_tree.invs[0].nodes[0].ocr_string_count == 2
    empty = refresh_review_issues(_edit(repaired, replace(node, rows=())), repaired)
    assert [i.code for i in empty.all_issues] == ["node_has_no_trackers", "table_node_not_detected"]


def test_box_position_edit_only_resolves_position_ambiguity():
    node = _node(1)
    previous = _result(
        node,
        issues=[
            create_issue(
                code, name=node.name, entity_id=node.recognition_id, page_number=1
            )
            for code in (
                IssueCode.NODE_HAS_NO_BOX,
                IssueCode.SINGLE_ROW_BOX_POSITION_AMBIGUOUS,
            )
        ],
    )
    updated = refresh_review_issues(
        _edit(previous, replace(node, box_position=2)), previous
    )
    assert [i.code for i in updated.all_issues] == ["node_has_no_box", "table_node_not_detected"]


def test_deleted_node_loses_result_issues_but_execution_history_survives():
    node = _node(1)
    previous = _result(
        node,
        issues=[
            create_issue(
                IssueCode.NODE_HAS_NO_BOX,
                name=node.name,
                entity_id=node.recognition_id,
                page_number=1,
            ),
            create_issue(
                IssueCode.NODE_NAME_OCR_FAILED,
                detail="timeout",
                entity_id=node.recognition_id,
                page_number=1,
            ),
        ],
    )
    updated = refresh_review_issues(_edit(previous), previous)
    assert [i.code for i in updated.all_issues] == [
        "node_name_ocr_failed", "table_node_not_detected", "table_node_not_detected"]
    assert {i.params["name"] for i in updated.all_issues if i.code == "table_node_not_detected"} == {"CBX01", "CBX02"}


def test_node_issue_is_not_copied_to_tracker_annotation():
    node = _node(1)
    tracker_source = SourceReference(page_number=1, bbox=BoundingBox(110, 10, 130, 30))
    node = replace(
        node,
        annotations=(*node.annotations, DetectionAnnotation("Tracker", tracker_source)),
    )
    updated = _result(
        node,
        issues=[
            create_issue(
                IssueCode.NODE_HAS_NO_BOX,
                name=node.name,
                entity_id=node.recognition_id,
                page_number=1,
            )
        ],
    )
    annotations = updated.reviewed_tree.invs[0].nodes[0].annotations
    assert annotations[0].issues == ("node_has_no_box",)
    assert annotations[1].issues == ()


def test_inv_rename_refreshes_agent2_match_warning():
    previous = _result(
        _node(1),
        issues=[
            create_issue(
                IssueCode.INV_NAME_NOT_IN_AGENT2,
                inferred="INV1",
                expected_text="INV2",
                page_number=1,
                entity_id="p1:inv",
            )
        ],
    )
    updated = replace(
        previous,
        reviewed_tree=replace(
            previous.reviewed_tree,
            invs=(replace(previous.reviewed_tree.invs[0], name="INV2"),),
        ),
    )
    issues = refresh_review_issues(updated, previous, expected_inv_names=("INV2",)).all_issues
    assert [i.code for i in issues] == ["table_node_not_detected"]
    assert issues[0].params["name"] == "CBX02"

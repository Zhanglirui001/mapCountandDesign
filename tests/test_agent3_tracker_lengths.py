"""Tracker length anomaly checks for one INV scope."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from count_and_design.application.ai import BoundingBox
from count_and_design.application.ai_statistics import (
    ElementDetection,
    ElementPageResult,
    ElementRecognitionResult,
    InvDraft,
    NodeDraft,
    RecognitionIssueSeverity,
    ReviewedTree,
    TrackerDefinition,
    annotate_tracker_length_outliers,
)
from count_and_design.application.ai_statistics.element_recognition.geometry import (
    TrackerLengthSample,
    check_tracker_lengths,
)
from count_and_design.gui.views.ai_statistics.pages.element_recognition import (
    ElementRecognitionPage,
    _COLORS,
)


def _sample(entity_id: str, length: float, strings: int) -> TrackerLengthSample:
    return TrackerLengthSample(entity_id, BoundingBox(0, 0, length, 20), strings)


def _node(
    name: str,
    page_number: int,
    ocr_string_count: int,
    node_index: int,
) -> NodeDraft:
    return NodeDraft(
        name=name,
        ocr_string_count=ocr_string_count,
        has_ocr_string_count=True,
        recognition_id=f"p{page_number}:node:{node_index}",
    )


def _tracker(
    page_number: int,
    index: int,
    parent_id: str,
    length: float,
) -> ElementDetection:
    return ElementDetection(
        entity_id=f"p{page_number}:tracker:{index}",
        kind="Tracker",
        confidence=0.9,
        bbox=BoundingBox(0, 0, length, 20),
        page_number=page_number,
        parent_id=parent_id,
    )


def _page(page_number: int, trackers: tuple[ElementDetection, ...]) -> ElementPageResult:
    return ElementPageResult(
        page_number=page_number,
        inv_name="INV1",
        image_width=1000,
        image_height=1000,
        detections=trackers,
    )


def _result(
    nodes: tuple[NodeDraft, ...],
    pages: tuple[ElementPageResult, ...],
) -> ElementRecognitionResult:
    return ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1", page=pages[0].page_number, nodes=nodes),),
        ),
        pages=pages,
    )


DEFINITIONS = (
    TrackerDefinition("1-STR", 1),
    TrackerDefinition("2-STR", 2),
)


def test_single_string_trackers_measure_the_one_string_length() -> None:
    check = check_tracker_lengths(
        (
            _sample("p1:tracker:1", 100, 1),
            _sample("p1:tracker:2", 102, 1),
            _sample("p1:tracker:3", 200, 2),
            _sample("p1:tracker:4", 110, 2),
        )
    )

    assert check.one_string_length == pytest.approx(101.0)
    assert [outlier.sample.entity_id for outlier in check.outliers] == [
        "p1:tracker:4"
    ]
    assert check.outliers[0].expected_length == pytest.approx(202.0)


def test_multi_string_only_inv_falls_back_to_the_median_string_length() -> None:
    check = check_tracker_lengths(
        (
            _sample("p1:tracker:1", 200, 2),
            _sample("p1:tracker:2", 204, 2),
            _sample("p1:tracker:3", 300, 2),
        )
    )

    assert check.one_string_length == pytest.approx(102.0)
    assert [outlier.sample.entity_id for outlier in check.outliers] == [
        "p1:tracker:3"
    ]


def test_a_lone_classified_tracker_cannot_be_an_outlier() -> None:
    check = check_tracker_lengths((_sample("p1:tracker:1", 100, 3),))

    assert check.one_string_length == 0.0
    assert check.outliers == ()


def test_annotation_flags_the_tracker_that_cannot_match_its_strings() -> None:
    single = _node("CBX01", 1, 1, 1)
    short_pair = _node("CBX02", 1, 2, 2)
    full_pair = _node("CBX03", 1, 2, 3)
    page = _page(
        1,
        (
            _tracker(1, 1, single.recognition_id, 100.0),
            _tracker(1, 2, short_pair.recognition_id, 100.0),
            _tracker(1, 3, full_pair.recognition_id, 200.0),
        ),
    )
    annotated = annotate_tracker_length_outliers(
        _result((single, short_pair, full_pair), (page,)),
        DEFINITIONS,
    )

    issues = annotated.pages[0].issues
    assert [issue.code for issue in issues] == ["tracker_length_outlier"]
    assert issues[0].severity is RecognitionIssueSeverity.WARNING
    assert issues[0].entity_id == "p1:tracker:2"
    assert issues[0].page_number == 1
    assert issues[0].bbox == page.detections[1].bbox
    flagged = {
        detection.entity_id
        for detection in annotated.pages[0].detections
        if "tracker_length_outlier" in detection.issues
    }
    assert flagged == {"p1:tracker:2"}
    assert page.detections[1].issues == ()
    repeated = annotate_tracker_length_outliers(annotated, DEFINITIONS)
    assert repeated is annotated
    cleared = annotate_tracker_length_outliers(annotated, DEFINITIONS, tolerance=1.0)
    assert not cleared.all_issues
    assert all(not detection.issues for detection in cleared.pages[0].detections)


def test_annotation_keeps_a_result_without_outliers_untouched() -> None:
    node = _node("CBX01", 1, 2, 1)
    result = _result(
        (node,),
        (_page(1, (_tracker(1, 1, node.recognition_id, 200.0),)),),
    )

    assert annotate_tracker_length_outliers(result, DEFINITIONS) is result


def test_inv_one_string_length_spans_merged_pages() -> None:
    first = _node("CBX01", 1, 1, 1)
    second = _node("CBX02", 2, 2, 1)
    result = _result(
        (first, second),
        (
            _page(1, (_tracker(1, 1, first.recognition_id, 100.0),)),
            _page(2, (_tracker(2, 1, second.recognition_id, 100.0),)),
        ),
    )

    annotated = annotate_tracker_length_outliers(result, DEFINITIONS)

    assert annotated.pages[0].issues == ()
    assert [issue.entity_id for issue in annotated.pages[1].issues] == [
        "p2:tracker:1"
    ]


def test_preview_canvas_paints_the_outlier_tracker_in_the_issue_color() -> None:
    app = QApplication.instance() or QApplication([])
    single = _node("CBX01", 1, 1, 1)
    short_pair = _node("CBX02", 1, 2, 2)
    result = _result(
        (single, short_pair),
        (
            _page(
                1,
                (
                    _tracker(1, 1, single.recognition_id, 100.0),
                    _tracker(1, 2, short_pair.recognition_id, 100.0),
                ),
            ),
        ),
    )
    annotated = annotate_tracker_length_outliers(result, DEFINITIONS)
    page = ElementRecognitionPage()
    page.set_result(annotated)
    app.processEvents()

    outlier = page.canvas._rects["p1:tracker:2"]
    reference = page.canvas._rects["p1:tracker:1"]
    assert outlier.pen().color() == _COLORS["issue"]
    assert reference.pen().color() == _COLORS["tracker"]
    assert not page.issues_button.isHidden()
    assert page.issues.count() == 1
    assert (
        page.issues.item(0).data(Qt.ItemDataRole.UserRole) == "p1:tracker:2"
    )

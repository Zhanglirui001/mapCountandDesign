from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
import pytest

from count_and_design.application.ai import BoundingBox, DetectionResult
from count_and_design.application.ai_statistics import (
    Agent4ValidationService,
    reviewed_tree_to_validation_payload,
)
from count_and_design.application.ai_statistics.element_recognition import (
    Agent3Cancelled,
    Agent3ElementRecognizer,
    ArrayLayoutPageInput,
    ElementPageResult,
    ElementRecognitionRequest,
    ElementRecognitionResult,
    InvRecognitionSettings,
    NodeStringTableRow,
    PanelOrientation,
    TrackerDefinition,
    recalculate_node,
)
from count_and_design.application.ai_statistics.models import (
    InvDraft,
    NodeDraft,
    RackingEntryDraft,
    ReviewedTree,
    RowDraft,
    TrackerQuantity,
)
from count_and_design.application.ai_statistics.element_recognition.block_diagram_table import (
    BlockDiagramTableResult,
    BlockDiagramTableRow,
)
from count_and_design.application.ai_statistics.element_recognition.geometry import (
    assign_boxes_to_nodes,
    assign_parent,
    center_to_edge_distance,
    classify_trackers,
    polarity_for_side,
    snap_positive,
)
from count_and_design.application.ai_statistics.element_recognition.service import (
    _infer_page_inv_name,
    _intersection_over_union,
    _overlapping_pairs,
    replace_table_rows_and_recalculate,
)
from count_and_design.application.ordering import natural_text_key


def _box(x1, y1, x2, y2) -> BoundingBox:
    return BoundingBox(x1, y1, x2, y2)


def test_assign_parent_uses_nearest_center_for_nested_nodes() -> None:
    child = _box(40, 40, 60, 60)
    assert assign_parent(
        child,
        [("large", _box(0, 0, 100, 100)), ("small", _box(30, 30, 70, 70))],
    ) == "small"


def test_boxes_outside_nodes_fall_back_to_nearest_unpaired_node_edge() -> None:
    nodes = [
        ("left", _box(0, 0, 100, 100)),
        ("right", _box(200, 0, 300, 100)),
        ("bottom", _box(0, 200, 100, 300)),
    ]
    boxes = [
        _box(10, 10, 20, 20),
        _box(110, 40, 120, 60),
        _box(40, 160, 60, 170),
    ]

    assert center_to_edge_distance(boxes[1], nodes[0][1]) == 225.0
    assert assign_boxes_to_nodes(boxes, nodes) == (
        "left",
        "right",
        "bottom",
    )


def test_box_fallback_leaves_surplus_box_unassigned() -> None:
    assert assign_boxes_to_nodes(
        [_box(20, 20, 30, 30), _box(40, 40, 50, 50)],
        [("node", _box(0, 0, 10, 10))],
    ) == ("node", "")


def test_intersection_over_union_handles_overlap_and_degenerate_boxes() -> None:
    # 60 / (100 + 100 - 60) = 3/7, which is above Agent3's 30% threshold.
    assert _intersection_over_union(
        _box(0, 0, 10, 10), _box(4, 0, 14, 10)
    ) == pytest.approx(3 / 7)
    assert _intersection_over_union(
        _box(0, 0, 10, 10), _box(10, 0, 20, 10)
    ) == 0.0
    assert _intersection_over_union(
        _box(0, 0, 0, 10), _box(0, 0, 0, 10)
    ) == 0.0
    exactly_thirty_percent = [
        SimpleNamespace(box=_box(0, 0, 13, 10)),
        SimpleNamespace(box=_box(7, 0, 20, 10)),
    ]
    assert _overlapping_pairs(exactly_thirty_percent) == ()
    assert len(_overlapping_pairs(exactly_thirty_percent, threshold=0.299)) == 1


def test_string_balance_assigns_short_and_long_trackers() -> None:
    definitions = (
        TrackerDefinition("2-STR", 2),
        TrackerDefinition("3-STR", 3),
    )
    result = classify_trackers(
        [_box(0, 0, 60, 10), _box(0, 20, 90, 30)],
        5,
        definitions,
    )
    assert result is not None
    assert result.definition_indexes == (0, 1)
    assert result.counts == (1, 1)
    assert classify_trackers([_box(0, 0, 60, 10)], 7, definitions) is None


def test_string_balance_keeps_short_long_definition_detection_in_its_class() -> None:
    definitions = (
        TrackerDefinition("13-STR", 13),
        TrackerDefinition("9-STR", 9),
    )
    # Five 9-string and five 13-string Trackers where one 13-string box was
    # detected short. The OCR total still fixes the counts at 5/5, so the
    # outlier belongs to the 13-string definition instead of shifting the
    # partition to 6/4.
    result = classify_trackers(
        [_box(0, 0, 465, 10) for _ in range(5)]
        + [_box(0, 0, 562, 10)]
        + [_box(0, 0, 671, 10) for _ in range(4)],
        110,
        definitions,
    )
    assert result is not None
    assert result.counts == (5, 5)
    assert result.definition_indexes[:5] == (1, 1, 1, 1, 1)
    assert result.definition_indexes[5:] == (0, 0, 0, 0, 0)


def test_string_balance_rejects_a_total_no_counting_can_reach() -> None:
    definitions = (
        TrackerDefinition("13-STR", 13),
        TrackerDefinition("9-STR", 9),
    )
    boxes = [_box(0, 0, 465, 10) for _ in range(12)]
    assert classify_trackers(boxes, 99, definitions) is None


def test_orientation_and_positive_snapping_are_deterministic() -> None:
    # A bracket north of the Box faces the panel's negative pole when the
    # panel's positive pole points north, and vice versa for the south side.
    assert polarity_for_side(
        PanelOrientation.POSITIVE_NORTH,
        side_delta_u=-10,
        side_delta_y=0,
    ) == "N"
    assert polarity_for_side(
        PanelOrientation.POSITIVE_SOUTH,
        side_delta_u=-10,
        side_delta_y=0,
    ) == "P"
    assert polarity_for_side(
        PanelOrientation.POSITIVE_NORTH,
        side_delta_u=10,
        side_delta_y=0,
    ) == "P"
    assert polarity_for_side(
        PanelOrientation.POSITIVE_SOUTH,
        side_delta_u=10,
        side_delta_y=0,
    ) == "N"
    assert polarity_for_side(
        PanelOrientation.TOWARD_BOX,
        side_delta_u=10,
        side_delta_y=10,
    ) == "P"
    assert polarity_for_side(
        PanelOrientation.AWAY_FROM_BOX,
        side_delta_u=10,
        side_delta_y=10,
    ) == "N"
    assert polarity_for_side(
        PanelOrientation.POSITIVE_WEST,
        side_delta_u=-10,
        side_delta_x=-10,
        side_delta_y=0,
    ) == "N"
    assert polarity_for_side(
        PanelOrientation.POSITIVE_WEST,
        side_delta_u=10,
        side_delta_x=10,
        side_delta_y=0,
    ) == "P"
    assert polarity_for_side(
        PanelOrientation.POSITIVE_EAST,
        side_delta_u=-10,
        side_delta_x=-10,
        side_delta_y=0,
    ) == "P"
    assert polarity_for_side(
        PanelOrientation.POSITIVE_EAST,
        side_delta_u=10,
        side_delta_x=10,
        side_delta_y=0,
    ) == "N"
    assert snap_positive(1.49, 1.0) == 1.0
    assert snap_positive(1.5, 1.0) == 2.0


def test_agent3_node_names_use_case_insensitive_natural_order() -> None:
    names = ["CBX10", "cbx2", "CBX01"]
    assert sorted(names, key=natural_text_key) == ["CBX01", "cbx2", "CBX10"]


class _FakeYolo:
    def detect(self, _image, *, model_type, confidence=None):
        del confidence
        if model_type == "tracker":
            detections = [
                DetectionResult("Node", 0.95, _box(0, 0, 400, 300), source="fake:tracker"),
                DetectionResult("Tracker", 0.90, _box(60, 90, 120, 110), source="fake:tracker"),
                DetectionResult("Tracker", 0.91, _box(250, 90, 340, 110), source="fake:tracker"),
            ]
        else:
            detections = [
                DetectionResult("Box", 0.92, _box(190, 85, 210, 115), source="fake:box")
            ]
        return SimpleNamespace(detections=detections)


class _FakeTable:
    def recognize(self, _image):
        return BlockDiagramTableResult(
            project_type="IBEX",
            equipment_family="CBX",
            string_info=(BlockDiagramTableRow("CBX01", 5),),
            strategy="yolo_claude",
            table_bbox=BoundingBox(0.1, 0.2, 0.9, 0.8),
            raw_output={"table_bbox_source": "yolo"},
        )


class _FakeOcr:
    def parse_file(self, *_args, **_kwargs):
        return SimpleNamespace(markdown="INV 1001-CBX01")


class _SingleVerticalRowYolo:
    def __init__(self, *, box_x1: float = 9.3, box_x2: float = 25.3) -> None:
        self._box_x1 = box_x1
        self._box_x2 = box_x2

    def detect(self, _image, *, model_type, confidence=None):
        del confidence
        if model_type == "tracker":
            detections = [
                DetectionResult("Node", 0.95, _box(0, 0, 50, 1400), source="fake:tracker"),
                DetectionResult(
                    "Tracker",
                    0.90,
                    _box(10.0, 50.0, 26.0, 612.25),
                    source="fake:tracker",
                ),
                DetectionResult(
                    "Tracker",
                    0.91,
                    _box(9.9, 650.0, 25.9, 1212.1),
                    source="fake:tracker",
                ),
            ]
        else:
            detections = [
                DetectionResult(
                    "Box",
                    0.92,
                    _box(self._box_x1, 10, self._box_x2, 30),
                    source="fake:box",
                )
            ]
        return SimpleNamespace(detections=detections)


class _OverlappingYolo:
    def detect(self, _image, *, model_type, confidence=None):
        del confidence
        if model_type == "tracker":
            detections = [
                DetectionResult("Node", 0.95, _box(0, 0, 220, 200), source="fake:tracker"),
                DetectionResult("Node", 0.94, _box(100, 0, 320, 200), source="fake:tracker"),
                DetectionResult("Tracker", 0.90, _box(10, 50, 110, 70), source="fake:tracker"),
                DetectionResult("Tracker", 0.91, _box(50, 50, 150, 70), source="fake:tracker"),
            ]
        else:
            detections = [
                DetectionResult("Box", 0.92, _box(20, 80, 40, 100), source="fake:box")
            ]
        return SimpleNamespace(detections=detections)


class _MultipleBoxesYolo(_FakeYolo):
    def detect(self, image, *, model_type, confidence=None):
        result = super().detect(
            image,
            model_type=model_type,
            confidence=confidence,
        )
        if model_type == "box":
            result.detections.append(
                DetectionResult(
                    "Box",
                    0.91,
                    _box(215, 85, 235, 115),
                    source="fake:box",
                )
            )
        return result


class _OutsideBoxesYolo(_FakeYolo):
    def detect(self, _image, *, model_type, confidence=None):
        del confidence
        if model_type == "tracker":
            detections = [
                DetectionResult(
                    "Node",
                    0.95,
                    _box(0, 0, 100, 100),
                    source="fake:tracker",
                ),
                DetectionResult(
                    "Node",
                    0.94,
                    _box(200, 0, 300, 100),
                    source="fake:tracker",
                ),
            ]
        else:
            detections = [
                DetectionResult(
                    "Box",
                    0.92,
                    _box(10, 10, 20, 20),
                    source="fake:box",
                ),
                DetectionResult(
                    "Box",
                    0.91,
                    _box(110, 40, 120, 60),
                    source="fake:box",
                ),
            ]
        return SimpleNamespace(detections=detections)


class _EastWestRowsYolo:
    def detect(self, _image, *, model_type, confidence=None):
        del confidence
        if model_type == "tracker":
            # Deliberately return south, north, then middle. Row numbering must
            # use absolute page y instead of detector order.
            detections = [
                DetectionResult("Node", 0.95, _box(0, 0, 320, 300), source="fake:tracker"),
                DetectionResult("Tracker", 0.90, _box(20, 240, 120, 260), source="fake:tracker"),
                DetectionResult("Tracker", 0.90, _box(180, 240, 280, 260), source="fake:tracker"),
                DetectionResult("Tracker", 0.90, _box(20, 40, 120, 60), source="fake:tracker"),
                DetectionResult("Tracker", 0.90, _box(180, 40, 280, 60), source="fake:tracker"),
                DetectionResult("Tracker", 0.90, _box(20, 140, 120, 160), source="fake:tracker"),
                DetectionResult("Tracker", 0.90, _box(180, 140, 280, 160), source="fake:tracker"),
            ]
        else:
            detections = [
                DetectionResult("Box", 0.92, _box(140, 190, 160, 210), source="fake:box")
            ]
        return SimpleNamespace(detections=detections)


class _EastWestRowsTable:
    def recognize(self, _image):
        return BlockDiagramTableResult(
            project_type="IBEX",
            equipment_family="CBX",
            string_info=(BlockDiagramTableRow("CBX01", 6),),
        )


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (400, 300), "white").save(output, format="PNG")
    return output.getvalue()


def test_agent3_builds_reviewed_tree_and_traceable_page_result() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=_FakeYolo(),
        table_recognizer=_FakeTable(),
        node_ocr=_FakeOcr(),
    )
    progress = []
    result = recognizer.recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, _png()),),
            tracker_definitions=(
                TrackerDefinition("2-STR", 2, panel_label="Panel A"),
                TrackerDefinition("3-STR", 3, panel_label="Panel B"),
            ),
            expected_inv_names=("1001",),
            default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
        ),
        on_progress=lambda completed, total: progress.append((completed, total)),
    )

    assert result.equipment_family == "CBX"
    assert result.reviewed_tree.project_type == "IBEX"
    assert [item.name for item in result.reviewed_tree.length_identifiers] == [
        "str_a",
        "str_b",
    ]
    inv = result.reviewed_tree.invs[0]
    node = inv.nodes[0]
    assert inv.name == "INV 1001"
    assert (
        result.inv_name_matches[0].agent2_name,
        result.inv_name_matches[0].agent3_name,
    ) == ("1001", "INV 1001")
    assert inv.page == 7
    assert node.name == "CBX01"
    assert node.ocr_string_count == 5
    assert node.has_ocr_string_count is True
    assert [(item.tracker_type, item.tracker_count) for item in node.tracker_counts] == [
        ("2-STR", 1),
        ("3-STR", 1),
    ]
    assert [entry.rackings for entry in node.rows[0].racking_entries] == [
        "2 N",
        "3 P",
    ]
    assert [entry.length_str for entry in node.rows[0].racking_entries] == [
        "2str_a",
        "1str_b",
    ]
    assert node.box_position == 1.0
    assert len(result.pages[0].detections) == 4
    assert result.pages[0].table_rows == (NodeStringTableRow("CBX01", 5),)
    assert result.pages[0].table_bbox == BoundingBox(40, 60, 360, 240)
    assert result.pages[0].table_bbox_source == "yolo"
    assert progress[-1] == (5, 5)

    updated = replace_table_rows_and_recalculate(
        result,
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, b""),),
            tracker_definitions=(
                TrackerDefinition("2-STR", 2, panel_label="Panel A"),
                TrackerDefinition("3-STR", 3, panel_label="Panel B"),
            ),
            default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
        ),
        7,
        (NodeStringTableRow("CBX01", 4),),
    )
    recalculated = updated.reviewed_tree.invs[0].nodes[0]
    assert recalculated.ocr_string_count == 4
    assert recalculated.has_ocr_string_count is True
    assert recalculated.tracker_counts == (
        TrackerQuantity("2-STR", 2),
    )


def _recognize_single_vertical_row(yolo: _SingleVerticalRowYolo):
    return Agent3ElementRecognizer(
        yolo=yolo,
        table_recognizer=_FakeTable(),
        node_ocr=_FakeOcr(),
    ).recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, _png()),),
            tracker_definitions=(
                TrackerDefinition("2-STR", 2),
                TrackerDefinition("3-STR", 3),
            ),
            expected_inv_names=("1001",),
        )
    )


def test_agent3_keeps_edited_ocr_count_and_structure_when_trackers_cannot_balance_it() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=_FakeYolo(),
        table_recognizer=_FakeTable(),
        node_ocr=_FakeOcr(),
    )
    result = recognizer.recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, _png()),),
            tracker_definitions=(
                TrackerDefinition("2-STR", 2, panel_label="Panel A"),
                TrackerDefinition("3-STR", 3, panel_label="Panel B"),
            ),
            expected_inv_names=("1001",),
            default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
        )
    )
    original = result.reviewed_tree.invs[0].nodes[0]
    assert original.tracker_counts == (
        TrackerQuantity("2-STR", 1),
        TrackerQuantity("3-STR", 1),
    )

    # Reviewing the table to 9 strings cannot be balanced by the two detected
    # Trackers (max 3+3). The edited OCR reference must survive and the
    # recognized structure must not collapse to an empty 0, otherwise Agent4
    # would report a lost value instead of the calculated-vs-OCR difference.
    updated = replace_table_rows_and_recalculate(
        result,
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, b""),),
            tracker_definitions=(
                TrackerDefinition("2-STR", 2, panel_label="Panel A"),
                TrackerDefinition("3-STR", 3, panel_label="Panel B"),
            ),
            expected_inv_names=("1001",),
            default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
        ),
        7,
        (NodeStringTableRow("CBX01", 9),),
    )
    recalculated = updated.reviewed_tree.invs[0].nodes[0]

    assert recalculated.ocr_string_count == 9
    assert recalculated.has_ocr_string_count is True
    assert recalculated.tracker_counts == original.tracker_counts
    assert recalculated.rows == original.rows
    assert any(issue.code == "tracker_string_balance_failed" for issue in updated.all_issues)
    assert "tracker_string_balance_failed" in recalculated.issues

    repaired = replace_table_rows_and_recalculate(
        updated,
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, b""),),
            tracker_definitions=(TrackerDefinition("2-STR", 2), TrackerDefinition("3-STR", 3)),
        ),
        7,
        (NodeStringTableRow("CBX01", 5),),
    )
    assert not any(issue.code == "tracker_string_balance_failed" for issue in repaired.all_issues)
    assert "tracker_string_balance_failed" not in repaired.reviewed_tree.invs[0].nodes[0].issues
    assert all("tracker_string_balance_failed" not in item.issues for item in repaired.pages[0].detections)

    report = Agent4ValidationService().validate(
        {"tracker_totals": [], "grand_total": 5},
        reviewed_tree_to_validation_payload(updated.reviewed_tree),
    )
    comparison = report.inv_comparisons[0].nodes[0]
    assert comparison.calculated_total == 5
    assert comparison.ocr_total == 9
    assert comparison.diff == 4
    assert not comparison.passed


def test_agent3_table_apply_keeps_nodes_without_a_reviewed_row() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=_FakeYolo(),
        table_recognizer=_FakeTable(),
        node_ocr=_FakeOcr(),
    )
    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(7, _png()),),
        tracker_definitions=(
            TrackerDefinition("2-STR", 2, panel_label="Panel A"),
            TrackerDefinition("3-STR", 3, panel_label="Panel B"),
        ),
        expected_inv_names=("1001",),
        default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
    )
    result = recognizer.recognize(request)
    original = result.reviewed_tree.invs[0].nodes[0]

    # The reviewed table no longer lists this Node (its row was renamed or
    # never matched). Applying the table must keep the Node exactly as
    # recognized instead of erasing its OCR count and Row structure.
    updated = replace_table_rows_and_recalculate(
        result,
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, b""),),
            tracker_definitions=request.tracker_definitions,
            expected_inv_names=("1001",),
            default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
        ),
        7,
        (NodeStringTableRow("CBX99", 4),),
    )

    revised = updated.reviewed_tree.invs[0].nodes[0]
    assert replace(revised, issues=original.issues, annotations=original.annotations) == original
    assert "node_name_unmatched" in revised.issues
    assert any(issue.code == "node_name_unmatched" for issue in updated.all_issues)
    assert updated.pages[0].table_rows == (NodeStringTableRow("CBX99", 4),)


def test_agent3_table_apply_resolves_unanchored_user_node_by_name() -> None:
    # A Node the user created for a missed YOLO detection has no page anchor
    # (no detection, no page number). The reviewed table being applied is its
    # only OCR context, so the matching row must set its OCR count; otherwise
    # Agent4's Layout OCR cell stays empty.
    manual = NodeDraft(
        name="CBX07",
        rows=(
            RowDraft(
                "Row1",
                racking_entries=(RackingEntryDraft(rackings="2 P"),),
            ),
        ),
        tracker_counts=(TrackerQuantity("2-STR", 1),),
    )
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1", page=7, nodes=(manual,)),),
        ),
        pages=(ElementPageResult(7, "INV1", 100, 100),),
    )

    updated = replace_table_rows_and_recalculate(
        result,
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, b""),),
            tracker_definitions=(TrackerDefinition("2-STR", 2),),
            default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
        ),
        7,
        (NodeStringTableRow("CBX07", 54),),
    )

    node = updated.reviewed_tree.invs[0].nodes[0]
    assert node.name == "CBX07"
    assert node.ocr_string_count == 54
    assert node.has_ocr_string_count is True
    assert node.rows == manual.rows
    assert node.tracker_counts == manual.tracker_counts


def test_single_vertical_row_ignores_subpixel_box_alignment_jitter() -> None:
    result = _recognize_single_vertical_row(_SingleVerticalRowYolo())

    node = result.reviewed_tree.invs[0].nodes[0]
    assert [(row.name, row.direction) for row in node.rows] == [("Row1", "y")]
    assert node.box_position == 1.0
    assert not any(
        issue.code == "single_row_box_position_ambiguous"
        for issue in result.pages[0].issues
    )


def test_single_row_flags_box_offset_that_cannot_be_scaled_without_pitch() -> None:
    result = _recognize_single_vertical_row(
        _SingleVerticalRowYolo(box_x1=32.0, box_x2=48.0)
    )

    node = result.reviewed_tree.invs[0].nodes[0]
    assert node.box_position == 1.0
    assert any(
        issue.code == "single_row_box_position_ambiguous"
        and issue.entity_id == node.recognition_id
        for issue in result.pages[0].issues
    )


def test_east_west_trackers_number_rows_north_to_south_and_align_box() -> None:
    result = Agent3ElementRecognizer(
        yolo=_EastWestRowsYolo(),
        table_recognizer=_EastWestRowsTable(),
        node_ocr=_FakeOcr(),
    ).recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, _png()),),
            tracker_definitions=(TrackerDefinition("1-STR", 1),),
            expected_inv_names=("1001",),
            default_panel_orientation=PanelOrientation.POSITIVE_WEST,
        )
    )

    node = result.reviewed_tree.invs[0].nodes[0]
    assert [(row.name, row.direction) for row in node.rows] == [
        ("Row1", "x"),
        ("Row2", "x"),
        ("Row3", "x"),
    ]
    assert [row.source.bbox.y1 for row in node.rows] == [40, 140, 240]
    assert node.box_position == 2.5
    assert [
        [entry.rackings for entry in row.racking_entries]
        for row in node.rows
    ] == [["1 N", "1 P"]] * 3


def test_agent3_reports_each_internal_pipeline_step() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=_FakeYolo(),
        table_recognizer=_FakeTable(),
        node_ocr=_FakeOcr(),
    )
    updates = []

    recognizer.recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, _png()),),
            tracker_definitions=(
                TrackerDefinition("2-STR", 2),
                TrackerDefinition("3-STR", 3),
            ),
            default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
        ),
        on_step=updates.append,
    )

    assert {item.step for item in updates if item.status == "completed"} == {
        "preparing",
        "table_recognition",
        "tracker_detection",
        "box_detection",
        "qwen_node_names",
        "geometry",
        "finalizing",
    }


def test_agent3_reports_overlapping_yolo_boxes_and_marks_expected_nodes() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=_OverlappingYolo(),
        table_recognizer=_FakeTable(),
        node_ocr=_FakeOcr(),
    )
    result = recognizer.recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, _png()),),
            tracker_definitions=(
                TrackerDefinition("2-STR", 2),
                TrackerDefinition("3-STR", 3),
            ),
            expected_inv_names=("INV 1001",),
        )
    )

    page = result.pages[0]
    overlap_issues = [
        issue
        for issue in page.issues
        if issue.code in {"overlapping_node_boxes", "overlapping_tracker_boxes"}
    ]
    assert overlap_issues
    assert all(issue.severity.value == "error" for issue in overlap_issues)

    detections = {item.entity_id: item for item in page.detections}
    # Both overlapping Node boxes are red; the overlapping Tracker pair is
    # assigned to Node 1, so that Node (rather than the Tracker boxes) carries
    # the Tracker-overlap issue used by DetectionCanvas.
    assert "overlapping_node_boxes" in detections["p7:node:1"].issues
    assert "overlapping_node_boxes" in detections["p7:node:2"].issues
    assert "overlapping_tracker_boxes" in detections["p7:node:1"].issues
    assert "overlapping_tracker_boxes" not in detections["p7:tracker:1"].issues
    assert "overlapping_tracker_boxes" not in detections["p7:tracker:2"].issues


def test_agent3_marks_node_red_when_multiple_boxes_are_assigned() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=_MultipleBoxesYolo(),
        table_recognizer=_FakeTable(),
        node_ocr=_FakeOcr(),
    )
    result = recognizer.recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, _png()),),
            tracker_definitions=(
                TrackerDefinition("2-STR", 2),
                TrackerDefinition("3-STR", 3),
            ),
            expected_inv_names=("INV 1001",),
        )
    )

    issue = next(
        item
        for item in result.pages[0].issues
        if item.code == "node_has_multiple_boxes"
    )
    assert issue.severity.value == "error"
    assert issue.entity_id == "p7:node:1"

    detections = {
        item.entity_id: item for item in result.pages[0].detections
    }
    assert "node_has_multiple_boxes" in detections["p7:node:1"].issues
    assert "node_has_multiple_boxes" not in detections["p7:box:1"].issues
    assert "node_has_multiple_boxes" not in detections["p7:box:2"].issues


def test_agent3_assigns_outside_box_to_nearest_unpaired_node() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=_OutsideBoxesYolo(),
        table_recognizer=_FakeTable(),
        node_ocr=_FakeOcr(),
    )
    result = recognizer.recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(7, _png()),),
            tracker_definitions=(TrackerDefinition("2-STR", 2),),
        )
    )

    boxes = [
        item
        for item in result.pages[0].detections
        if item.kind == "Box"
    ]
    assert [item.parent_id for item in boxes] == [
        "p7:node:1",
        "p7:node:2",
    ]
    assert not any(
        issue.code == "unassigned_box"
        for issue in result.pages[0].issues
    )


def test_agent3_merges_multiple_pages_for_same_inv_in_stable_order() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=_FakeYolo(),
        table_recognizer=_FakeTable(),
        node_ocr=_FakeOcr(),
    )
    progress = []
    result = recognizer.recognize(
        ElementRecognitionRequest(
            pages=(
                ArrayLayoutPageInput(9, _png()),
                ArrayLayoutPageInput(3, _png()),
            ),
            tracker_definitions=(
                TrackerDefinition("2-STR", 2, panel_label="A"),
                TrackerDefinition("3-STR", 3, panel_label="B"),
            ),
            expected_inv_names=("INV 1001",),
            default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
        ),
        on_progress=lambda completed, total: progress.append((completed, total)),
    )

    assert [page.page_number for page in result.pages] == [3, 9]
    assert len(result.reviewed_tree.invs) == 1
    assert [node.recognition_id for node in result.reviewed_tree.invs[0].nodes] == [
        "p3:node:1",
        "p9:node:1",
    ]
    assert progress[-1] == (10, 10)


def test_agent3_merges_inv_pages_that_differ_only_by_their_inv_token() -> None:
    class _TwoStyleOcr:
        def parse_file(self, _image, *, filename, **_kwargs):
            # Page 3 prints the INV token, page 9 leaves it out; both pages
            # name the same INV and must not become two tree branches.
            page_number = int(
                filename.split("array-page-", 1)[1].split("-", 1)[0]
            )
            return SimpleNamespace(
                markdown=(
                    "INV 1001-CBX01"
                    if page_number == 3
                    else "1001-CBX01"
                )
            )

    recognizer = Agent3ElementRecognizer(
        yolo=_FakeYolo(),
        table_recognizer=_FakeTable(),
        node_ocr=_TwoStyleOcr(),
    )
    result = recognizer.recognize(
        ElementRecognitionRequest(
            pages=(
                ArrayLayoutPageInput(3, _png()),
                ArrayLayoutPageInput(9, _png()),
            ),
            tracker_definitions=(TrackerDefinition("2-STR", 2),),
            expected_inv_names=("1001",),
        )
    )

    assert [inv.name for inv in result.reviewed_tree.invs] == ["INV 1001"]
    assert len(result.reviewed_tree.invs[0].nodes) == 2


class _FailFirstTrackerYolo(_FakeYolo):
    def __init__(self) -> None:
        self.tracker_calls = 0

    def detect(self, image, *, model_type, confidence=None):
        if model_type == "tracker":
            self.tracker_calls += 1
            if self.tracker_calls == 1:
                raise RuntimeError("temporary detector error")
        return super().detect(image, model_type=model_type, confidence=confidence)


def test_agent3_preserves_partial_page_results_and_inv_overrides() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=_FailFirstTrackerYolo(),
        table_recognizer=_FakeTable(),
        node_ocr=_FakeOcr(),
    )
    result = recognizer.recognize(
        ElementRecognitionRequest(
            pages=(
                ArrayLayoutPageInput(1, _png()),
                ArrayLayoutPageInput(2, _png()),
            ),
            tracker_definitions=(
                TrackerDefinition("2-STR", 2, panel_label="A"),
                TrackerDefinition("3-STR", 3, panel_label="B"),
            ),
            expected_inv_names=("1001",),
            default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
            inv_settings=(
                InvRecognitionSettings(
                    "1001",
                    PanelOrientation.AWAY_FROM_BOX,
                    0.5,
                ),
            ),
        )
    )

    assert result.has_output
    assert any(issue.code == "tracker_detection_failed" for issue in result.pages[0].issues)
    recognized_inv = next(inv for inv in result.reviewed_tree.invs if inv.name)
    assert len(recognized_inv.nodes) == 1
    node = recognized_inv.nodes[0]
    assert node.panel_orientation == PanelOrientation.AWAY_FROM_BOX.value
    assert node.offset_snap_step_strings == 0.5
    assert all(
        entry.rackings.endswith(" N")
        for row in node.rows
        for entry in row.racking_entries
    )


def test_agent3_cancels_at_cooperative_checkpoint() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=_FakeYolo(),
        table_recognizer=_FakeTable(),
        node_ocr=_FakeOcr(),
    )
    with pytest.raises(Agent3Cancelled):
        recognizer.recognize(
            ElementRecognitionRequest(
                pages=(ArrayLayoutPageInput(1, _png()),),
                tracker_definitions=(TrackerDefinition("2-STR", 2),),
            ),
            is_cancelled=lambda: True,
        )


def test_page_inv_name_keeps_agent3_spelling_while_matching_agent2() -> None:
    inv_name, candidates, issue = _infer_page_inv_name(
        (
            "INV1201-CBX-01",
            "INV 1201_LBD_02",
            "INV1202-CBX-03",
        ),
        ("1201", "1202"),
        7,
    )

    assert inv_name == "INV 1201"
    assert candidates == ("INV 1201", "INV 1201", "INV 1202")
    assert issue is None


def test_page_inv_name_fuzzy_matches_the_closest_agent2_name() -> None:
    inv_name, _candidates, issue = _infer_page_inv_name(
        ("INV9001-CBX-01", "INV9001-CBX-02"),
        ("9001", "8001"),
        3,
    )

    assert inv_name == "INV 9001"
    assert issue is None


def test_page_inv_name_stays_unnamed_when_prefix_vote_is_tied() -> None:
    inv_name, _candidates, issue = _infer_page_inv_name(
        ("INV1001-CBX-01", "INV1002-CBX-02"),
        ("INV 1001", "INV 1002"),
        4,
    )

    assert inv_name == ""
    assert issue is not None
    assert issue.code == "inv_name_ambiguous"


def test_page_inv_name_keeps_page_spelling_for_dotted_separators() -> None:
    inv_name, candidates, issue = _infer_page_inv_name(
        ("INV1103-CBX.22", "INV1103-CBX.21", "INV1103-CBX.07"),
        ("INV1103",),
        16,
    )

    assert inv_name == "INV 1103"
    assert candidates == ("INV 1103", "INV 1103", "INV 1103")
    assert issue is None


def test_page_inv_name_matches_agent2_identity_without_known_token() -> None:
    # The drawing uses an equipment token this parser does not know, so no
    # prefix candidate exists.  The order-insensitive identifier still has
    # to resolve the listed INV; the reviewed list spelling is kept because
    # the page offers no complete one of its own.
    inv_name, candidates, issue = _infer_page_inv_name(
        ("INV1103-PCU.22", "INV1103-PCU.21"),
        ("INV1103",),
        16,
    )

    assert inv_name == "INV1103"
    assert candidates == ()
    assert issue is None


def test_page_inv_name_ignores_node_names_outside_the_agent2_list() -> None:
    inv_name, candidates, issue = _infer_page_inv_name(
        ("INV1103-CBX.01", "CBX9901-to-CBX9902", "INV1103-CBX.02"),
        ("INV1103",),
        16,
    )

    assert inv_name == "INV 1103"
    assert candidates == ("INV 1103", "INV 1103")
    assert issue is None


def test_recalculate_node_matches_polarity_for_side() -> None:
    node = NodeDraft(
        name="CBX01",
        rows=(
            RowDraft(
                name="Row1",
                racking_entries=(
                    RackingEntryDraft(string_pattern="8", north_side=True),
                    RackingEntryDraft(string_pattern="8", north_side=False),
                ),
            ),
        ),
    )
    north = recalculate_node(node, PanelOrientation.POSITIVE_NORTH, 1.0)
    south = recalculate_node(node, PanelOrientation.POSITIVE_SOUTH, 1.0)
    assert [entry.rackings for entry in north.rows[0].racking_entries] == [
        "8 N",
        "8 P",
    ]
    assert [entry.rackings for entry in south.rows[0].racking_entries] == [
        "8 P",
        "8 N",
    ]


def test_recalculate_node_matches_east_west_polarity_for_side() -> None:
    node = NodeDraft(
        name="CBX01",
        rows=(
            RowDraft(
                name="Row1",
                racking_entries=(
                    RackingEntryDraft(string_pattern="8", east_side=False),
                    RackingEntryDraft(string_pattern="8", east_side=True),
                ),
            ),
        ),
    )

    west = recalculate_node(node, PanelOrientation.POSITIVE_WEST, 1.0)
    east = recalculate_node(node, PanelOrientation.POSITIVE_EAST, 1.0)
    assert [entry.rackings for entry in west.rows[0].racking_entries] == [
        "8 N",
        "8 P",
    ]
    assert [entry.rackings for entry in east.rows[0].racking_entries] == [
        "8 P",
        "8 N",
    ]

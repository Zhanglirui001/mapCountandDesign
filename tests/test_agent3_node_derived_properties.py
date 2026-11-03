from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QTableWidgetItem

from count_and_design.application.ai import BoundingBox
from count_and_design.application.ai_statistics import (
    ArrayLayoutPageInput,
    ElementDetection,
    ElementPageResult,
    ElementRecognitionRequest,
    ElementRecognitionResult,
    InvDraft,
    NodeDraft,
    NodeStringTableRow,
    PanelOrientation,
    RackingEntryDraft,
    ReviewedTree,
    RowDraft,
    SourceReference,
    TrackerDefinition,
    TrackerQuantity,
    recalculate_node,
    reviewed_tree_to_validation_payload,
    summarize_node_rows,
)
from count_and_design.gui.views.ai_statistics.pages.element_recognition import (
    ElementRecognitionPage,
)


def _node() -> NodeDraft:
    return NodeDraft(
        name="CBX01",
        box_position=1.0,
        ocr_string_count=9,
        has_ocr_string_count=True,
        tracker_counts=(
            TrackerQuantity("2-STR", 99),
            TrackerQuantity("3-STR", 99),
        ),
        rows=(
            RowDraft(
                name="Row1",
                racking_entries=(
                    RackingEntryDraft(rackings="2+3 N", string_pattern="2+3"),
                    RackingEntryDraft(rackings="2 P", string_pattern="2"),
                ),
            ),
        ),
    )


def test_node_row_summary_derives_strings_and_tracker_counts() -> None:
    string_count, tracker_counts = summarize_node_rows(_node())

    assert string_count == 7
    assert tracker_counts == (
        TrackerQuantity("2-STR", 2),
        TrackerQuantity("3-STR", 1),
    )


def test_recalculate_node_keeps_ocr_provenance_and_derives_row_totals() -> None:
    updated = recalculate_node(_node(), PanelOrientation.TOWARD_BOX, 0.5)

    assert updated.ocr_string_count == 9
    assert updated.has_ocr_string_count is True
    assert updated.tracker_counts == (
        TrackerQuantity("2-STR", 2),
        TrackerQuantity("3-STR", 1),
    )


def test_node_properties_are_read_only_derived_fields_and_apply_uses_editors() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    page.set_result(
        ElementRecognitionResult(
            reviewed_tree=ReviewedTree(
                invs=(InvDraft(name="INV1", nodes=(_node(),)),),
            ),
        ),
        {},
    )
    node_item = page.structure_tree.topLevelItem(0).child(0)
    page.structure_tree.setCurrentItem(node_item)
    app.processEvents()

    assert not page.node_strings.isEnabled()
    assert not page.tracker_counts.isEnabled()
    assert page.node_strings.value() == 7
    assert page.tracker_counts.text() == "2-STR=2, 3-STR=1"
    strings_row, _ = page.properties_form.getWidgetPosition(page.node_strings)
    trackers_row, _ = page.properties_form.getWidgetPosition(page.tracker_counts)
    apply_row, _ = page.properties_form.getWidgetPosition(page.recalculate_button)
    assert strings_row < trackers_row < apply_row

    page.name_edit.setText("CBX02")
    page.box_position.setValue(2.5)
    page.orientation_combo.setCurrentIndex(
        page.orientation_combo.findData(PanelOrientation.TOWARD_BOX)
    )
    page.snap_step.setValue(0.5)
    changes = []
    page.review_changed.connect(changes.append)
    page._apply_settings()

    updated = changes[-1].invs[0].nodes[0]
    assert updated.name == "CBX02"
    assert updated.box_position == 2.5
    assert updated.panel_orientation == PanelOrientation.TOWARD_BOX.value
    assert updated.offset_snap_step_strings == 0.5
    assert updated.ocr_string_count == 9
    assert updated.has_ocr_string_count is True
    assert updated.tracker_counts == (
        TrackerQuantity("2-STR", 2),
        TrackerQuantity("3-STR", 1),
    )


def test_node_property_editing_finished_preserves_row_derived_fields() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    page.set_result(
        ElementRecognitionResult(
            reviewed_tree=ReviewedTree(
                invs=(InvDraft(name="INV1", nodes=(_node(),)),),
            ),
        ),
        {},
    )
    page.structure_tree.setCurrentItem(page.structure_tree.topLevelItem(0).child(0))
    app.processEvents()

    page.name_edit.setText("CBX02")
    page.box_position.setValue(2.5)
    changes = []
    page.review_changed.connect(changes.append)
    page.name_edit.editingFinished.emit()

    updated = changes[-1].invs[0].nodes[0]
    assert updated.name == "CBX02"
    assert updated.box_position == 2.5
    assert updated.ocr_string_count == 9
    assert updated.has_ocr_string_count is True
    assert updated.tracker_counts == (
        TrackerQuantity("2-STR", 2),
        TrackerQuantity("3-STR", 1),
    )


def test_node_apply_rematches_edited_name_and_rebuilds_only_that_node(
    monkeypatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(1, b""),),
        tracker_definitions=(TrackerDefinition("2-STR", 2),),
        default_panel_orientation=PanelOrientation.TOWARD_BOX,
    )
    original = NodeDraft(
        name="misread",
        recognition_id="p1:node:1",
        source=SourceReference(page_number=1, bbox=BoundingBox(0, 0, 100, 100)),
        rows=(
            RowDraft(
                "OldRow",
                racking_entries=(
                    RackingEntryDraft(rackings="3 P", string_pattern="3"),
                ),
            ),
        ),
    )
    sibling = NodeDraft(
        name="CBX03",
        recognition_id="p1:node:2",
        source=SourceReference(page_number=1, bbox=BoundingBox(0, 0, 100, 100)),
        rows=(
            RowDraft(
                "ManualRow",
                racking_entries=(
                    RackingEntryDraft(rackings="9 P", string_pattern="9"),
                ),
            ),
        ),
    )
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1", nodes=(original, sibling)),),
        ),
        pages=(
            ElementPageResult(
                page_number=1,
                inv_name="INV1",
                image_width=100,
                image_height=100,
                detections=(
                    ElementDetection(
                        "p1:node:1",
                        "Node",
                        0.9,
                        BoundingBox(0, 0, 100, 100),
                        1,
                    ),
                    ElementDetection(
                        "p1:tracker:1",
                        "Tracker",
                        0.9,
                        BoundingBox(10, 20, 40, 30),
                        1,
                        parent_id="p1:node:1",
                    ),
                    ElementDetection(
                        "p1:tracker:2",
                        "Tracker",
                        0.9,
                        BoundingBox(60, 20, 90, 30),
                        1,
                        parent_id="p1:node:1",
                    ),
                ),
                table_rows=(NodeStringTableRow("CBX02", 4),),
            ),
        ),
    )
    page.set_request(request)
    page.set_result(result, {})
    page.structure_tree.setCurrentItem(page.structure_tree.topLevelItem(0).child(0))
    app.processEvents()

    page.name_edit.setText("CBX02")
    page.box_position.setValue(2.5)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Node Apply must not show a confirmation dialog")
        ),
    )

    changes = []
    page.review_changed.connect(changes.append)
    page._apply_settings()

    updated = changes[-1].invs[0].nodes[0]
    assert updated.name == "CBX02"
    assert updated.box_position == 2.5
    assert updated.ocr_string_count == 4
    assert updated.tracker_counts == (TrackerQuantity("2-STR", 2),)
    assert [row.name for row in updated.rows] == ["Row1"]
    assert [entry.string_pattern for entry in updated.rows[0].racking_entries] == [
        "2+2"
    ]
    assert changes[-1].invs[0].nodes[1] == sibling


def test_table_apply_keeps_structure_when_edited_count_cannot_balance() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(1, b""),),
        tracker_definitions=(TrackerDefinition("2-STR", 2),),
        default_panel_orientation=PanelOrientation.TOWARD_BOX,
    )
    node = NodeDraft(
        name="CBX01",
        recognition_id="p1:node:1",
        source=SourceReference(page_number=1, bbox=BoundingBox(0, 0, 100, 100)),
        rows=(
            RowDraft(
                "Row1",
                racking_entries=(
                    RackingEntryDraft(rackings="2 P", string_pattern="2"),
                ),
            ),
        ),
        ocr_string_count=2,
        has_ocr_string_count=True,
        tracker_counts=(TrackerQuantity("2-STR", 1),),
    )
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1", page=1, nodes=(node,)),),
        ),
        pages=(
            ElementPageResult(
                page_number=1,
                inv_name="INV1",
                image_width=100,
                image_height=100,
                detections=(
                    ElementDetection(
                        "p1:node:1",
                        "Node",
                        0.9,
                        BoundingBox(0, 0, 100, 100),
                        1,
                    ),
                    ElementDetection(
                        "p1:tracker:1",
                        "Tracker",
                        0.9,
                        BoundingBox(10, 20, 40, 30),
                        1,
                        parent_id="p1:node:1",
                    ),
                ),
                table_rows=(NodeStringTableRow("CBX01", 2),),
            ),
        ),
    )
    page.set_request(request)
    page.set_result(result, {})
    app.processEvents()

    # 7 strings cannot be balanced by the single detected 2-STR Tracker. The
    # reviewed table count replaces the OCR reference while the recognized
    # structure survives, so Agent4 compares against it instead of showing 0.
    page.table_review_table.item(0, 1).setText("7")
    changes = []
    page.table_review_changed.connect(changes.append)
    page._apply_table_rows_and_recalculate()
    app.processEvents()

    updated = changes[-1].reviewed_tree.invs[0].nodes[0]
    assert updated.ocr_string_count == 7
    assert updated.has_ocr_string_count is True
    assert updated.tracker_counts == (TrackerQuantity("2-STR", 1),)
    assert page.table_review_table.item(0, 1).text() == "7"


def _rename_sync_page() -> ElementRecognitionPage:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(1, b""),),
        tracker_definitions=(TrackerDefinition("2-STR", 2),),
        default_panel_orientation=PanelOrientation.TOWARD_BOX,
    )
    node = NodeDraft(
        name="misread",
        recognition_id="p1:node:1",
        source=SourceReference(page_number=1, bbox=BoundingBox(0, 0, 100, 100)),
    )
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1", page=1, nodes=(node,)),),
        ),
        pages=(
            ElementPageResult(
                page_number=1,
                inv_name="INV1",
                image_width=100,
                image_height=100,
                detections=(
                    ElementDetection(
                        "p1:node:1",
                        "Node",
                        0.9,
                        BoundingBox(0, 0, 100, 100),
                        1,
                    ),
                ),
                table_rows=(NodeStringTableRow("CBX02", 4),),
            ),
        ),
    )
    page.set_request(request)
    page.set_result(result, {})
    app.processEvents()
    return page


def test_node_inline_rename_resolves_reviewed_table_string_count() -> None:
    app = QApplication.instance() or QApplication([])
    page = _rename_sync_page()
    changes = []
    page.review_changed.connect(changes.append)

    page.structure_tree.topLevelItem(0).child(0).setText(0, "CBX02")
    app.processEvents()

    updated = changes[-1].invs[0].nodes[0]
    assert updated.name == "CBX02"
    assert updated.ocr_string_count == 4
    assert updated.has_ocr_string_count is True


def test_node_property_rename_resolves_reviewed_table_string_count() -> None:
    app = QApplication.instance() or QApplication([])
    page = _rename_sync_page()
    changes = []
    page.review_changed.connect(changes.append)

    page.structure_tree.setCurrentItem(
        page.structure_tree.topLevelItem(0).child(0)
    )
    app.processEvents()
    page.name_edit.setText("CBX02")
    page.name_edit.editingFinished.emit()

    updated = changes[-1].invs[0].nodes[0]
    assert updated.name == "CBX02"
    assert updated.ocr_string_count == 4
    assert updated.has_ocr_string_count is True


def test_agent4_payload_gets_resolved_ocr_count_after_rename() -> None:
    app = QApplication.instance() or QApplication([])
    page = _rename_sync_page()
    changes = []
    page.review_changed.connect(changes.append)

    page.structure_tree.topLevelItem(0).child(0).setText(0, "CBX02")
    app.processEvents()

    payload = reviewed_tree_to_validation_payload(changes[-1])
    node_payload = payload["invs"][0]["nodes"][0]
    assert node_payload["name"] == "CBX02"
    assert node_payload["ocr_string_count"] == 4
    assert node_payload["has_ocr_string_count"] is True


def test_table_review_updates_ocr_reference_for_node_without_detection() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(1, b""),),
        tracker_definitions=(TrackerDefinition("2-STR", 2),),
        default_panel_orientation=PanelOrientation.TOWARD_BOX,
    )
    manual = NodeDraft(
        name="CBX09",
        source=SourceReference(page_number=1, bbox=BoundingBox(0, 0, 100, 100)),
        rows=(
            RowDraft(
                "Row1",
                racking_entries=(
                    RackingEntryDraft(rackings="2 P", string_pattern="2"),
                ),
            ),
        ),
        tracker_counts=(TrackerQuantity("2-STR", 1),),
    )
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1", page=1, nodes=(manual,)),),
        ),
        pages=(
            ElementPageResult(
                page_number=1,
                inv_name="INV1",
                image_width=100,
                image_height=100,
            ),
        ),
    )
    page.set_request(request)
    page.set_result(result, {})
    app.processEvents()
    changes = []
    page.table_review_changed.connect(changes.append)

    page.table_review_table.insertRow(0)
    page.table_review_table.setItem(0, 0, QTableWidgetItem("CBX09"))
    page.table_review_table.setItem(0, 1, QTableWidgetItem("7"))
    page._apply_table_rows_and_recalculate()

    updated = changes[-1].reviewed_tree.invs[0].nodes[0]
    assert updated.name == "CBX09"
    assert updated.ocr_string_count == 7
    assert updated.has_ocr_string_count is True
    assert updated.rows == manual.rows
    assert updated.tracker_counts == (TrackerQuantity("2-STR", 1),)


def test_manual_racking_widths_use_canonical_tracker_codes() -> None:
    node = NodeDraft(
        name="CBX01",
        tracker_counts=(TrackerQuantity("2-STR", 1),),
        rows=(
            RowDraft(
                "Row1",
                racking_entries=(
                    RackingEntryDraft(rackings="2+3 P", string_pattern="2+3"),
                ),
            ),
        ),
    )

    definitions = (
        TrackerDefinition("2-STR", 2),
        TrackerDefinition("3-STR", 3),
    )
    string_count, tracker_counts = summarize_node_rows(
        node,
        tracker_definitions=definitions,
    )

    assert string_count == 5
    assert tracker_counts == (
        TrackerQuantity("2-STR", 1),
        TrackerQuantity("3-STR", 1),
    )

    recalculated = recalculate_node(
        node,
        PanelOrientation.TOWARD_BOX,
        0.5,
        inherit_orientation=True,
        inherit_snap_step=True,
        tracker_definitions=definitions,
    )
    assert recalculated.tracker_counts == tracker_counts


def test_racking_edit_uses_canonical_tracker_code() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(1, b""),),
        tracker_definitions=(
            TrackerDefinition("2-STR", 2),
            TrackerDefinition("3-STR", 3),
        ),
        default_panel_orientation=PanelOrientation.TOWARD_BOX,
    )
    node = NodeDraft(
        name="CBX01",
        recognition_id="p1:node:1",
        source=SourceReference(page_number=1, bbox=BoundingBox(0, 0, 100, 100)),
        rows=(
            RowDraft(
                "Row1",
                racking_entries=(
                    RackingEntryDraft(rackings="2 P", string_pattern="2"),
                ),
            ),
        ),
        ocr_string_count=5,
        has_ocr_string_count=True,
        tracker_counts=(TrackerQuantity("2-STR", 1),),
    )
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1", page=1, nodes=(node,)),),
        ),
        pages=(
            ElementPageResult(
                page_number=1,
                inv_name="INV1",
                image_width=100,
                image_height=100,
                detections=(
                    ElementDetection(
                        "p1:node:1",
                        "Node",
                        0.9,
                        BoundingBox(0, 0, 100, 100),
                        1,
                    ),
                ),
                table_rows=(NodeStringTableRow("CBX01", 5),),
            ),
        ),
    )
    page.set_request(request)
    page.set_result(result, {})
    app.processEvents()
    changes = []
    page.review_changed.connect(changes.append)

    page.structure_tree.setCurrentItem(
        page.structure_tree.topLevelItem(0).child(0).child(0)
    )
    app.processEvents()
    page.racking_table.item(0, 0).setText("2+3 N")
    app.processEvents()

    updated = changes[-1].invs[0].nodes[0]
    assert updated.ocr_string_count == 5
    assert updated.tracker_counts == (
        TrackerQuantity("2-STR", 1),
        TrackerQuantity("3-STR", 1),
    )


def test_recalculate_node_adopts_manually_edited_racking_pattern() -> None:
    definitions = (
        TrackerDefinition("13-string", 13),
        TrackerDefinition("9-string", 9),
    )
    node = NodeDraft(
        name="CBX01",
        rows=(
            RowDraft(
                "Row1",
                racking_entries=(
                    RackingEntryDraft(
                        rackings="9 N",
                        string_pattern="13",
                        north_side=False,
                    ),
                ),
            ),
        ),
        tracker_counts=(TrackerQuantity("13-string", 1),),
    )

    updated = recalculate_node(
        node,
        PanelOrientation.POSITIVE_NORTH,
        1.0,
        tracker_definitions=definitions,
    )

    entry = updated.rows[0].racking_entries[0]
    assert entry.string_pattern == "9"
    assert entry.rackings == "9 N"
    assert entry.north_side is True
    assert updated.tracker_counts == (TrackerQuantity("9-string", 1),)


def test_recalculate_node_keeps_typed_polarity_for_every_orientation() -> None:
    def entry() -> RackingEntryDraft:
        return RackingEntryDraft(
            rackings="2 N",
            string_pattern="3",
            north_side=False,
        )

    for orientation in (
        PanelOrientation.POSITIVE_NORTH,
        PanelOrientation.POSITIVE_SOUTH,
        PanelOrientation.POSITIVE_EAST,
        PanelOrientation.POSITIVE_WEST,
    ):
        node = NodeDraft(
            name="CBX01",
            rows=(RowDraft("Row1", racking_entries=(entry(),)),),
        )
        updated = recalculate_node(node, orientation, 1.0)
        assert updated.rows[0].racking_entries[0].rackings == "2 N"


def test_node_apply_keeps_manually_edited_racking_pattern() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(1, b""),),
        tracker_definitions=(
            TrackerDefinition("13-string", 13),
            TrackerDefinition("9-string", 9),
        ),
        default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
    )
    node = NodeDraft(
        name="CBX01",
        recognition_id="p1:node:1",
        source=SourceReference(page_number=1, bbox=BoundingBox(0, 0, 100, 100)),
        rows=(
            RowDraft(
                "Row1",
                racking_entries=(
                    RackingEntryDraft(
                        rackings="13 P",
                        string_pattern="13",
                        north_side=False,
                    ),
                ),
            ),
        ),
        tracker_counts=(TrackerQuantity("13-string", 1),),
        panel_orientation=PanelOrientation.POSITIVE_NORTH.value,
    )
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1", page=1, nodes=(node,)),),
        ),
        pages=(
            ElementPageResult(
                page_number=1,
                inv_name="INV1",
                image_width=100,
                image_height=100,
                detections=(
                    ElementDetection(
                        "p1:node:1",
                        "Node",
                        0.9,
                        BoundingBox(0, 0, 100, 100),
                        1,
                    ),
                ),
            ),
        ),
    )
    page.set_request(request)
    page.set_result(result, {})
    app.processEvents()
    changes = []
    page.review_changed.connect(changes.append)

    page.structure_tree.setCurrentItem(
        page.structure_tree.topLevelItem(0).child(0).child(0)
    )
    app.processEvents()
    page.racking_table.item(0, 0).setText("9 N")
    app.processEvents()
    page.structure_tree.setCurrentItem(page.structure_tree.topLevelItem(0).child(0))
    app.processEvents()
    page._apply_settings()

    updated = changes[-1].invs[0].nodes[0]
    assert [item.rackings for item in updated.rows[0].racking_entries] == ["9 N"]
    assert updated.tracker_counts == (TrackerQuantity("9-string", 1),)


def test_unanchored_node_rename_resolves_unique_table_count() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(1, b""),),
        tracker_definitions=(TrackerDefinition("13-string", 13),),
        default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
    )
    detected = NodeDraft(
        name="CBX01",
        recognition_id="p1:node:1",
        source=SourceReference(page_number=1, bbox=BoundingBox(0, 0, 100, 100)),
        ocr_string_count=88,
        has_ocr_string_count=True,
    )
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1", page=1, nodes=(detected,)),),
        ),
        pages=(
            ElementPageResult(
                page_number=1,
                inv_name="INV1",
                image_width=100,
                image_height=100,
                table_rows=(
                    NodeStringTableRow("CBX01", 88),
                    NodeStringTableRow("CBX07", 54),
                ),
            ),
        ),
    )
    page.set_request(request)
    page.set_result(result, {})
    app.processEvents()
    changes = []
    page.review_changed.connect(changes.append)

    # The reviewed table lists CBX07 but YOLO missed its box; the user adds
    # the Node manually and types the table's spelling.
    page.structure_tree.setCurrentItem(page.structure_tree.topLevelItem(0))
    app.processEvents()
    page._add_node()
    app.processEvents()
    page.structure_tree.topLevelItem(0).child(1).setText(0, "CBX07")
    app.processEvents()

    added = changes[-1].invs[0].nodes[1]
    assert added.ocr_string_count == 54
    assert added.has_ocr_string_count is True


def test_unanchored_node_rename_requires_a_unique_table_name() -> None:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(1, b""), ArrayLayoutPageInput(2, b"")),
        tracker_definitions=(TrackerDefinition("13-string", 13),),
        default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
    )
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1", page=1, nodes=()),),
        ),
        pages=(
            ElementPageResult(
                page_number=1,
                inv_name="INV1",
                image_width=100,
                image_height=100,
                table_rows=(NodeStringTableRow("CBX01", 88),),
            ),
            ElementPageResult(
                page_number=2,
                inv_name="INV2",
                image_width=100,
                image_height=100,
                table_rows=(
                    NodeStringTableRow("CBX01", 42),
                    NodeStringTableRow("CBX09", 63),
                ),
            ),
        ),
    )
    page.set_request(request)
    page.set_result(result, {})
    app.processEvents()
    changes = []
    page.review_changed.connect(changes.append)

    page.structure_tree.setCurrentItem(page.structure_tree.topLevelItem(0))
    app.processEvents()
    page._add_node()
    app.processEvents()
    page.structure_tree.topLevelItem(0).child(0).setText(0, "CBX01")
    app.processEvents()

    # Two reviewed tables carry the same name, so no count is guessed.
    ambiguous = changes[-1].invs[0].nodes[0]
    assert ambiguous.ocr_string_count == 0
    assert ambiguous.has_ocr_string_count is False

    page.structure_tree.topLevelItem(0).child(0).setText(0, "CBX09")
    app.processEvents()
    unique = changes[-1].invs[0].nodes[0]
    assert unique.ocr_string_count == 63
    assert unique.has_ocr_string_count is True


def _paste_source_page() -> ElementRecognitionPage:
    app = QApplication.instance() or QApplication([])
    page = ElementRecognitionPage()
    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(1, b""),),
        tracker_definitions=(TrackerDefinition("2-STR", 2),),
        default_panel_orientation=PanelOrientation.TOWARD_BOX,
    )
    source = NodeDraft(
        name="CBX02",
        recognition_id="p1:node:1",
        source=SourceReference(page_number=1, bbox=BoundingBox(0, 0, 100, 100)),
        rows=(
            RowDraft(
                "Row1",
                racking_entries=(
                    RackingEntryDraft(rackings="2 P", string_pattern="2"),
                ),
            ),
        ),
        ocr_string_count=4,
        has_ocr_string_count=True,
        tracker_counts=(TrackerQuantity("2-STR", 1),),
    )
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1", page=1, nodes=(source,)),),
        ),
        pages=(
            ElementPageResult(
                page_number=1,
                inv_name="INV1",
                image_width=100,
                image_height=100,
                detections=(
                    ElementDetection(
                        "p1:node:1",
                        "Node",
                        0.9,
                        BoundingBox(0, 0, 100, 100),
                        1,
                    ),
                ),
                table_rows=(NodeStringTableRow("CBX02", 4),),
            ),
        ),
    )
    page.set_request(request)
    page.set_result(result, {})
    app.processEvents()
    return page


def test_pasted_node_drops_all_recognition_provenance() -> None:
    app = QApplication.instance() or QApplication([])
    page = _paste_source_page()
    changes = []
    page.review_changed.connect(changes.append)
    source = page._object_at((0, 0))

    page.structure_tree.setCurrentItem(page.structure_tree.topLevelItem(0).child(0))
    app.processEvents()
    page._copy_selected()
    page.structure_tree.setCurrentItem(page.structure_tree.topLevelItem(0))
    page._paste_to_selected()

    pasted = changes[-1].invs[0].nodes[1]
    assert pasted.name == "Node 2"
    assert pasted.recognition_id == ""
    assert pasted.source == SourceReference()
    assert pasted.annotations == ()
    assert pasted.ocr_string_count == 0
    assert pasted.has_ocr_string_count is False
    assert pasted.rows == source.rows
    assert changes[-1].invs[0].nodes[0] == source

    # A detached copy has no page anchor, so it behaves like a manually
    # added Node: renaming it to a name that matches exactly one reviewed
    # table row resolves that row's OCR count.
    page.structure_tree.setCurrentItem(
        page.structure_tree.topLevelItem(0).child(1)
    )
    app.processEvents()
    page.name_edit.setText("CBX02")
    page.name_edit.editingFinished.emit()
    assert changes[-1].invs[0].nodes[1].ocr_string_count == 4
    assert changes[-1].invs[0].nodes[1].has_ocr_string_count is True


def test_pasted_inv_drops_all_recognition_provenance() -> None:
    app = QApplication.instance() or QApplication([])
    page = _paste_source_page()
    changes = []
    page.review_changed.connect(changes.append)

    page.structure_tree.setCurrentItem(page.structure_tree.topLevelItem(0))
    app.processEvents()
    page._copy_selected()
    page._paste_to_selected()

    pasted_inv = changes[-1].invs[1]
    assert pasted_inv.name == "INV 2"
    assert pasted_inv.page is None
    assert pasted_inv.source == SourceReference()
    pasted_node = pasted_inv.nodes[0]
    assert pasted_node.name == "CBX02"
    assert pasted_node.recognition_id == ""
    assert pasted_node.source == SourceReference()
    assert pasted_node.annotations == ()
    assert pasted_node.ocr_string_count == 0
    assert pasted_node.has_ocr_string_count is False

"""Offline end-to-end tests: production geometry, Qt UI, loopback and MCP."""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PIL import Image
from PySide6.QtCore import QSizeF
from PySide6.QtGui import QPageSize, QPainter, QPdfWriter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from count_and_design.application.ai.types import BoundingBox
from count_and_design.application.ai_statistics import AIStatisticsWorkflow, FakePageClassificationProvider
from count_and_design.application.ai_statistics.models import (
    InvDraft, NodeDraft, PageClassification, PageClassificationResult,
    ReviewedTree, SourceReference,
)
from count_and_design.application.ai_statistics.element_recognition.contracts import (
    ElementDetection, ElementPageResult, ElementRecognitionResult, NodeStringTableRow, TrackerDefinition,
)
from count_and_design.gui.viewmodels.ai_statistics import AIStatisticsViewModel
from count_and_design.gui.views.ai_statistics.tab import AIStatisticsTab
from count_and_design.integration.corrections import correct_result
from count_and_design.integration.gui_bridge import GuiBridge
from count_and_design.integration.mcp_server import BridgeClient, create_server
from count_and_design.integration.review_session import ReviewSession


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def drawing(tmp_path, app):
    path = tmp_path / "drawing.pdf"
    writer = QPdfWriter(str(path))
    writer.setResolution(72)
    writer.setPageSize(QPageSize(QSizeF(1000, 1000), QPageSize.Unit.Point))
    painter = QPainter(writer)
    painter.drawRect(100, 100, 300, 25)
    painter.drawRect(100, 200, 300, 25)
    writer.newPage()
    painter.drawText(100, 100, "Overall Layout")
    painter.end()
    del writer
    return path


@pytest.fixture
def prepared(drawing, tmp_path):
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(drawing, 1)
    workflow.complete_page_classification(PageClassificationResult(
        pages=(PageClassification(1, "Array Layout"),),
    ))
    workflow.configure_element_recognition((TrackerDefinition("3-STR", 3, total_count=2),))
    node_box = BoundingBox(50, 50, 900, 900)
    node = NodeDraft("Node1", recognition_id="n1", source=SourceReference(page_number=1, bbox=node_box),
                     ocr_string_count=6, has_ocr_string_count=True)
    page = ElementPageResult(1, "INV1", 1000, 1000, detections=(
        ElementDetection("n1", "Node", .9, node_box, 1, label="Node1"),
        ElementDetection("t1", "Tracker", .9, BoundingBox(100, 100, 400, 125), 1, parent_id="n1"),
        ElementDetection("b1", "Box", .9, BoundingBox(80, 110, 90, 120), 1, parent_id="n1"),
    ), table_rows=(NodeStringTableRow("Node1", 6),))
    result = ElementRecognitionResult(ReviewedTree(invs=(InvDraft("INV1", nodes=(node,)),)), pages=(page,))
    workflow.complete_element_recognition(result)
    vm = AIStatisticsViewModel(workflow)
    session = ReviewSession(vm, tmp_path / "runtime")
    return workflow, vm, session


def addition():
    return {"op": "add_detection", "page_number": 1, "kind": "Tracker",
            "bbox": [.1, .2, .4, .225], "parent_id": "n1",
            "reason": "Second separate Tracker visible below the first"}


def test_inv_rename_does_not_rebuild_reviewed_rows(prepared):
    workflow, _, session = prepared
    request = workflow.element_recognition_request_metadata
    stable = correct_result(session.result(), request, [addition()])
    inv = stable.reviewed_tree.invs[0]
    reviewed = replace(inv.nodes[0], rows=(replace(inv.nodes[0].rows[0], name="Reviewed row"),
                                          *inv.nodes[0].rows[1:]))
    stable = replace(stable, reviewed_tree=replace(stable.reviewed_tree,
                     invs=(replace(inv, nodes=(reviewed,)),)))
    candidate = correct_result(stable, request, [{"op": "rename_inv", "page_number": 1,
        "name": "PCS-1", "reason": "Read page title"}])
    assert candidate.reviewed_tree.invs[0].nodes[0] == reviewed
    assert candidate.pages[0].detections == stable.pages[0].detections


def test_editing_one_node_preserves_untouched_node_on_same_page(prepared):
    from count_and_design.application.ai_statistics.models import RowDraft
    workflow, _, session = prepared
    before = session.result()
    inv = before.reviewed_tree.invs[0]
    other = replace(inv.nodes[0], recognition_id="n2", name="Node2",
                    rows=(RowDraft("Manually reviewed row", rackings=("3",)),))
    page = replace(before.pages[0], detections=(*before.pages[0].detections,
        ElementDetection("n2", "Node", .9, BoundingBox(500, 500, 900, 900), 1)))
    before = replace(before, pages=(page,), reviewed_tree=replace(before.reviewed_tree,
                     invs=(replace(inv, nodes=(*inv.nodes, other)),)))
    candidate = correct_result(before, workflow.element_recognition_request_metadata, [addition()])
    actual = candidate.reviewed_tree.invs[0].nodes[1]
    assert replace(actual, issues=other.issues) == other  # page-wide name diagnostics may refresh


def test_failed_string_allocation_is_not_reported_as_missing_trackers(prepared):
    workflow, _, session = prepared
    request = workflow.element_recognition_request_metadata
    stable = correct_result(session.result(), request, [addition()])
    candidate = correct_result(stable, request, [{"op": "replace_table_rows", "page_number": 1,
        "rows": [{"node": "Node1", "string_number": 7}],
        "reason": "Read conflicting schedule count"}])
    codes = {i.code for i in candidate.all_issues if i.entity_id == "n1"}
    assert "tracker_string_balance_failed" in codes
    assert "node_has_no_trackers" not in codes
    assert len([d for d in candidate.pages[0].detections if d.kind == "Tracker"]) == 2


def test_repair_receipt_reports_published_checks_and_unresolved_counts(prepared):
    workflow, _, session = prepared
    evidence = session.evidence(1)["metadata"]["evidence_id"]
    preview = session.preview(session.project_id, session.revision, [addition()], [evidence])
    assert "persistent_records" in preview["issue_changes"]
    receipt = session.apply(session.project_id, session.revision, preview["preview_id"], "verify-repair")
    assert receipt["preview_matches_published"]
    assert receipt["repair_checks"]["detected_trackers"] == 2
    assert receipt["repair_checks"]["derived_trackers"] == 2
    assert receipt["repair_checks"]["status"] == "needs_follow_up"  # unknown orientation
    assert not session.summary()["repair_complete"]
    assert "raw" not in session.issues()["diagnostics"]


def test_repair_checks_compare_rows_to_detections_and_cached_counts(prepared):
    from count_and_design.integration.verification import result_checks
    workflow, _, session = prepared
    request = workflow.element_recognition_request_metadata
    stable = correct_result(session.result(), request, [addition()])
    inv = stable.reviewed_tree.invs[0]
    node = replace(inv.nodes[0], rows=())  # stale quantities still say 2, but Rows are empty
    broken = replace(stable, reviewed_tree=replace(stable.reviewed_tree,
                     invs=(replace(inv, nodes=(node,)),)))
    checks = result_checks(broken, workflow.validate_element_result(broken), request)
    item = checks["node_count_mismatches"][0]
    assert (item["detected_trackers"], item["cached_trackers"], item["derived_trackers"]) == (2, 2, 0)
    assert "detection_structure_count_mismatch" in checks["blockers"]


def test_table_node_omission_is_listed_and_clears_only_after_node_added(prepared):
    from count_and_design.application.ai_statistics.element_recognition.issue_review import missing_node_issues
    from count_and_design.integration.verification import result_checks
    workflow, vm, session = prepared
    before = session.result()
    page = replace(before.pages[0], table_rows=(*before.pages[0].table_rows,
                                               NodeStringTableRow("MissingNode", 6)))
    before = replace(before, pages=(page,))
    vm.replace_element_result(before)
    missing = [i for i in session.issues()["items"] if i["code"] == "table_node_not_detected"]
    assert len(missing) == 1 and missing[0]["params"]["name"] == "MissingNode"
    assert missing[0]["bbox"] is None
    request = replace(workflow.element_recognition_request_metadata, expected_inv_names=("INV1",))
    checks = result_checks(before, workflow.validate_element_result(before), request)
    assert checks["missing_node_candidates"][0]["status"] == "needs_manual_confirmation"
    assert not checks["missing_node_candidates"][0]["counted_in_structure"]
    candidate = correct_result(before, request, [{"op": "add_detection", "page_number": 1,
        "kind": "Node", "name": "MissingNode", "inv_name": "INV1", "bbox": [.5, .5, .9, .9],
        "reason": "Confirmed node extent from original drawing"}])
    assert missing_node_issues(candidate) == []


def test_occluded_node_manual_item_persists_without_becoming_a_device(prepared):
    workflow, _, session = prepared
    before = session.result()
    request = workflow.element_recognition_request_metadata
    op = {"op": "mark_node_for_review", "page_number": 1, "name": "Occluded candidate",
          "reason": "Boundary and ownership obscured; human confirmation required"}
    candidate = correct_result(before, request, [op])
    assert len(candidate.reviewed_tree.invs[0].nodes) == len(before.reviewed_tree.invs[0].nodes)
    assert candidate.pages[0].detections == before.pages[0].detections
    notes = [i for i in candidate.all_issues if i.code == "manual_node_review_required"]
    assert len(notes) == 1 and notes[0].bbox is None
    session.vm.replace_element_result(candidate)
    summary = session.summary()
    handoff = json.loads(Path(summary["manual_review_path"]).read_text(encoding="utf-8"))
    assert handoff["manual_review_queue"][0]["name"] == "Occluded candidate"
    assert not handoff["manual_review_queue"][0]["counted_in_structure"]
    assert not summary["repair_complete"]
    later = correct_result(candidate, request, [addition()])
    assert any(i.key == notes[0].key for i in later.all_issues)
    resolved = correct_result(later, request, [{"op": "resolve_node_review", "page_number": 1,
        "entity_id": notes[0].entity_id, "reason": "Human verified it is drawing context, not a target Node"}])
    assert not any(i.code == "manual_node_review_required" for i in resolved.all_issues)


def test_missing_node_can_use_existing_page_inv_when_agent2_list_is_empty(prepared):
    workflow, _, session = prepared
    request = workflow.element_recognition_request_metadata
    assert request.expected_inv_names == ()
    op = {"op": "add_detection", "page_number": 1, "kind": "Node", "name": "Node2",
          "inv_name": "INV1", "bbox": [.5, .5, .9, .9], "reason": "Confirmed original Node under page INV"}
    candidate = correct_result(session.result(), request, [op])
    assert candidate.reviewed_tree.invs[0].nodes[-1].name == "Node2"
    with pytest.raises(ValueError, match="page-owned INV"):
        correct_result(session.result(), request, [{**op, "inv_name": "UnverifiedOtherINV"}])


@pytest.mark.parametrize("name", ["", "INV1"])
def test_corrections_preserve_separate_invs_with_duplicate_names(prepared, name):
    workflow, _, _ = prepared
    before = workflow.snapshot.element_recognition_result
    first = replace(before.reviewed_tree.invs[0], name=name, page=1, recognition_id="p1:inv")
    second_node = replace(first.nodes[0], recognition_id="n2",
                          source=replace(first.nodes[0].source, page_number=2))
    second = replace(first, page=2, recognition_id="p2:inv", nodes=(second_node,))
    before = replace(before, reviewed_tree=replace(before.reviewed_tree, invs=(first, second)),
                     pages=(*before.pages, replace(before.pages[0], page_number=2, inv_name=name,
                            detections=(ElementDetection("n2", "Node", .9, second_node.source.bbox, 2),))))
    candidate = correct_result(before, workflow.element_recognition_request_metadata, [addition()])
    assert len(candidate.reviewed_tree.invs) == 2
    assert [i.recognition_id for i in candidate.reviewed_tree.invs] == ["p1:inv", "p2:inv"]
    assert candidate.reviewed_tree.invs[0].nodes[0].tracker_counts[0].tracker_count == 2
    assert candidate.reviewed_tree.invs[1] == second
    assert candidate.pages[1] == before.pages[1]


def test_rename_inv_preserves_identity_and_updates_page(prepared):
    workflow, _, _ = prepared
    before = workflow.snapshot.element_recognition_result
    candidate = correct_result(before, workflow.element_recognition_request_metadata, [
        {"op": "rename_inv", "page_number": 1, "name": "01PCS-SKD-0001", "reason": "Read inverter table column"},
    ])
    assert candidate.reviewed_tree.invs[0].name == "01PCS-SKD-0001"
    assert candidate.pages[0].inv_name == "01PCS-SKD-0001"
    assert candidate.reviewed_tree.invs[0].recognition_id == before.reviewed_tree.invs[0].recognition_id


def test_delete_node_requires_children_to_be_reassigned(prepared):
    workflow, _, _ = prepared
    before = workflow.snapshot.element_recognition_result
    operation = {"op": "delete_node", "page_number": 1, "entity_id": "n1", "reason": "False node"}
    with pytest.raises(ValueError, match="children"):
        correct_result(before, workflow.element_recognition_request_metadata, [operation])
    candidate = correct_result(before, workflow.element_recognition_request_metadata, [
        {"op": "delete_detection", "page_number": 1, "entity_id": "t1", "reason": "False tracker"},
        {"op": "delete_detection", "page_number": 1, "entity_id": "b1", "reason": "False box"},
        operation,
    ])
    assert candidate.reviewed_tree.invs[0].nodes == ()
    assert candidate.pages[0].detections == ()
    assert before.reviewed_tree.invs[0].nodes[0].recognition_id == "n1"


def test_evidence_preview_apply_recalculates_and_publishes(prepared):
    workflow, vm, session = prepared
    notifications = []
    vm.state_changed.connect(notifications.append)
    evidence = session.evidence(1)
    metadata = evidence["metadata"]
    assert len(evidence["images"]) == 2
    assert Image.open(BytesIO(base64.b64decode(evidence["images"][0]["data"]))).size == (1000, 1000)
    assert metadata["page"]["detections"][1]["bbox"] == [.1, .1, .4, .125]
    before = workflow.snapshot.element_recognition_result
    preview = session.preview(session.project_id, session.revision, [addition()], [metadata["evidence_id"]])
    assert workflow.snapshot.element_recognition_result is before
    assert preview["changed_nodes"] == ["n1"]
    receipt = session.apply(session.project_id, session.revision, preview["preview_id"], "fix-1")
    node = workflow.snapshot.reviewed_tree.invs[0].nodes[0]
    assert node.tracker_counts[0].tracker_count == 2
    assert node.tracker_counts[0].tracker_type == "3-STR"
    assert node.has_ocr_string_count and node.ocr_string_count == 6
    assert len(node.rows) == 2
    assert notifications[-1].element_recognition_result is workflow.snapshot.element_recognition_result
    assert receipt["revision"] > metadata["revision"]
    record = json.loads(Path(receipt["audit_path"]).read_text(encoding="utf-8"))
    assert record["status"] == "applied"
    assert len(record["before"]["pages"][0]["detections"]) == 3
    assert len(record["after"]["pages"][0]["detections"]) == 4
    summary = session.summary()
    # Agent4 grand totals count Trackers; individual Node totals count strings.
    assert summary["validation"]["calculated_grand_total"] == 2
    assert summary["validation"]["inv_comparisons"][0]["nodes"][0]["calculated_total"] == 6
    assert Path(summary["result_path"]).exists()
    assert session.apply(session.project_id, metadata["revision"], preview["preview_id"], "fix-1") == receipt
    assert len(workflow.snapshot.element_recognition_result.pages[0].detections) == 4
    with pytest.raises(ValueError, match="already used"):
        session.apply(session.project_id, metadata["revision"], "different-preview", "fix-1")


def test_human_edit_invalidates_preview_and_evidence(prepared):
    workflow, vm, session = prepared
    evidence = session.evidence(1)["metadata"]
    preview = session.preview(session.project_id, session.revision, [addition()], [evidence["evidence_id"]])
    vm.replace_element_result(replace(workflow.snapshot.element_recognition_result, equipment_family="human-edit"))
    with pytest.raises(ValueError, match="Revision conflict"):
        session.apply(session.project_id, evidence["revision"], preview["preview_id"], "stale")
    with pytest.raises(ValueError, match="stale"):
        session.preview(session.project_id, session.revision, [addition()], [evidence["evidence_id"]])


@pytest.mark.parametrize("change", [
    {"bbox": [-.1, .2, .4, .3]}, {"bbox": [.1, .2, .1, .3]},
    {"bbox": [float("nan"), .2, .4, .3]}, {"parent_id": "missing"},
    {"reason": ""}, {"unexpected": "field"}, {"page_number": 2},
])
def test_invalid_correction_is_atomic(prepared, change):
    workflow, _, _ = prepared
    before = workflow.snapshot.element_recognition_result
    with pytest.raises(ValueError):
        correct_result(before, workflow.element_recognition_request_metadata, [addition(), {**addition(), **change}])
    assert workflow.snapshot.element_recognition_result is before
    assert len(before.pages[0].detections) == 3


def test_delete_move_rename_and_table_edits_share_geometry(prepared):
    workflow, _, _ = prepared
    candidate = correct_result(workflow.snapshot.element_recognition_result,
                               workflow.element_recognition_request_metadata, [
        addition(),
        {"op": "update_detection", "page_number": 1, "entity_id": "t1", "bbox": [.1, .15, .4, .175], "reason": "Refined position"},
        {"op": "rename_node", "page_number": 1, "entity_id": "n1", "name": "Node2", "reason": "Read original label"},
        {"op": "replace_table_rows", "page_number": 1, "rows": [{"node": "Node2", "string_number": 6}], "reason": "Read table label"},
        {"op": "delete_detection", "page_number": 1, "entity_id": "b1", "reason": "Symbol was a false positive"},
    ])
    node = candidate.reviewed_tree.invs[0].nodes[0]
    assert node.name == "Node2" and node.ocr_string_count == 6
    assert node.tracker_counts[0].tracker_count == 2
    assert "node_has_no_box" in {i.code for i in candidate.all_issues}
    assert next(d for d in candidate.pages[0].detections if d.entity_id == "t1").bbox.y1 == 150


def test_loopback_dispatch_and_gui_refresh(prepared, tmp_path, app):
    workflow, vm, _ = prepared
    window = QWidget()
    tab = AIStatisticsTab(view_model=vm)
    window._ai_statistics_tab = tab
    bridge = GuiBridge(window, tmp_path / "bridge.json")
    client = BridgeClient(tmp_path / "bridge.json")
    try:
        with ThreadPoolExecutor(1) as pool:
            def call(tool, **args):
                pending = pool.submit(client.call, tool, **args)
                for _ in range(600):
                    if pending.done():
                        return pending.result()
                    QTest.qWait(10)
                pytest.fail("Bridge request did not complete")
            context = call("get_project_context")
            evidence = call("get_review_evidence", page_number=1)["metadata"]
            preview = call("preview_corrections", project_id=context["project_id"], base_revision=evidence["revision"],
                           operations=[addition()], evidence_ids=[evidence["evidence_id"]])
            call("apply_corrections", project_id=context["project_id"], base_revision=evidence["revision"],
                 preview_id=preview["preview_id"], request_id="gui-fix")
            assert tab.element_recognition_page._result is workflow.snapshot.element_recognition_result
            assert workflow.snapshot.reviewed_tree.invs[0].nodes[0].tracker_counts[0].tracker_count == 2
    finally:
        bridge.close()
        tab.close()
        window.close()
    assert not (tmp_path / "bridge.json").exists()


def test_mcp_returns_native_images_and_tool_schemas():
    pytest.importorskip("mcp")
    fake = Mock()
    fake.call.return_value = {"metadata": {"evidence_id": "e1"}, "images": [
        {"path": "original.png", "data": base64.b64encode(b"png").decode(), "mimeType": "image/png"},
    ]}
    server = create_server(fake)

    async def verify():
        tools = await server.list_tools()
        assert len(tools) == 14
        assert "operations" in next(t for t in tools if t.name == "preview_corrections").inputSchema["properties"]
        import_schema = next(t for t in tools if t.name == "import_pdf").inputSchema
        assert import_schema["properties"]["replace_existing"]["default"] is False
        content = await server.call_tool("get_review_evidence", {"page_number": 1})
        # SDK may return (content, structuredContent) when it infers output schemas.
        if isinstance(content, tuple):
            content = content[0]
        assert [c.type for c in content] == ["text", "image"]
        local_pdf = str(Path.cwd() / "drawing.pdf")
        await server.call_tool("import_pdf", {"path": local_pdf})
        fake.call.assert_called_with("import_pdf", path=local_pdf, replace_existing=False)
        await server.call_tool("import_pdf", {"path": local_pdf, "replace_existing": True})
        fake.call.assert_called_with("import_pdf", path=local_pdf, replace_existing=True)
    asyncio.run(verify())


def test_mcp_stdio_handshake_and_tool_error(tmp_path):
    pytest.importorskip("mcp")
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def verify():
        env = {**os.environ, "COUNT_DESIGN_BRIDGE_FILE": str(tmp_path / "absent.json")}
        params = StdioServerParameters(command=sys.executable,
                                      args=["-m", "count_and_design.integration.mcp_server"], env=env)
        with anyio.fail_after(30):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    initialized = await client.initialize()
                    assert initialized.serverInfo.name == "count-and-design"
                    tools = await client.list_tools()
                    assert len(tools.tools) == 14
                    result = await client.call_tool("get_job_status", {})
                    assert result.isError
                    assert "start_session" in result.content[0].text
    asyncio.run(verify())


@pytest.mark.parametrize("same_path", [False, True])
def test_import_protects_manually_loaded_unprocessed_pdf(drawing, tmp_path, app, same_path):
    # Simulate a user importing before Codex connects, without running a stage.
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(drawing, 2)
    vm = AIStatisticsViewModel(workflow)
    tab = AIStatisticsTab(view_model=vm)
    window = QWidget()
    window._ai_statistics_tab = tab
    bridge = GuiBridge(window, tmp_path / "manual.json")
    other = drawing if same_path else tmp_path / "other.pdf"
    if not same_path:
        other.write_bytes(drawing.read_bytes())
    try:
        before = bridge.dispatch("start_session", {})
        snapshot = workflow.snapshot
        session = bridge.session
        with pytest.raises(ValueError, match="new_window=True"):
            bridge.dispatch("import_pdf", {"path": str(other)})
        assert workflow.snapshot is snapshot
        assert bridge.session is session
        assert bridge._status() == before
        assert snapshot.page_classification is None
        replaced = bridge.dispatch("import_pdf", {"path": str(other), "replace_existing": True})
        assert replaced["project_id"] != before["project_id"]
        assert workflow.snapshot.pdf_path == other
        assert replaced["page_count"] == 2
    finally:
        bridge.close()
        tab.close()
        window.close()


@pytest.mark.parametrize("stage", ["all", 0, 1, 2])
def test_pipeline_job_uses_real_viewmodel_workers(prepared, drawing, tmp_path, app, stage, monkeypatch):
    from conftest import sample_agent2_extraction
    from count_and_design.application.recognition_queue import RecognitionTicket
    monkeypatch.setenv("COUNT_DESIGN_QUEUE_DIR", str(tmp_path / "queue"))
    _, _, seed = prepared
    result = seed.result()
    stream = BytesIO()
    Image.new("RGB", (1000, 1000), "white").save(stream, format="PNG")
    image = stream.getvalue()
    classifications = PageClassificationResult(
        pages=(PageClassification(1, "Array Layout"), PageClassification(2, "Overall Layout")),
        page_images={1: image, 2: image}, thumbnails={1: image, 2: image},
    )
    provider = SimpleNamespace(classify=lambda *args, **kwargs: classifications)
    info = SimpleNamespace(recognize_pages=lambda *args, **kwargs: (sample_agent2_extraction(),))
    elements = SimpleNamespace(recognize=lambda *args, **kwargs: result)
    workflow = AIStatisticsWorkflow(provider, info, element_recognizer=elements)
    vm = AIStatisticsViewModel(workflow)
    tab = AIStatisticsTab(view_model=vm)
    window = QWidget()
    window._ai_statistics_tab = tab
    bridge = GuiBridge(window, tmp_path / "pipeline.json")
    blocker = None
    try:
        status = bridge.dispatch("import_pdf", {"path": str(drawing)})
        assert status["page_count"] == 2
        if stage in (1, 2):
            vm.run_ai_model()
            evidence = [bridge.session.evidence(n)["metadata"]["evidence_id"] for n in (1, 2)]
            bridge.session.confirm_stage_review(bridge.session.project_id, bridge.session.revision,
                                                 0, evidence, "Both original pages checked")
        if stage == 2:
            info_job = bridge.dispatch("start_recognition", {"stage": 1, "panel_orientation": "P-N"})["job_id"]
            for _ in range(600):
                QTest.qWait(10)
                if bridge.jobs[info_job]["status"] == "completed":
                    break
            assert bridge.jobs[info_job]["status"] == "completed"
            bridge.session.confirm_stage_review(bridge.session.project_id, bridge.session.revision,
                                                 1, [], "Overall extraction reviewed")
        blocker = RecognitionTicket(vm._admission_directory(vm.workflow.snapshot.selected_stage))
        started = bridge.dispatch("start_recognition", {"stage": stage, "panel_orientation": "P-N"})
        assert started["job"]["status"] == "queued"
        assert started["queue_position"] == 1
        blocker.close()
        job_id = started["job_id"]
        for _ in range(600):
            QTest.qWait(10)
            if bridge.jobs[job_id]["status"] not in {"queued", "running"}:
                break
        assert bridge.jobs[job_id]["status"] == "completed", bridge.jobs[job_id]
        if stage == "all":
            assert workflow.snapshot.page_classification is not None
            assert workflow.snapshot.element_recognition_result is None
            assert bridge.jobs[job_id]["review_required"]
            with pytest.raises(ValueError, match="requires confirm_stage_review"):
                bridge.dispatch("start_recognition", {"stage": 1})
        elif stage != 2:
            assert workflow.snapshot.element_recognition_result is None
        else:
            assert workflow.snapshot.element_recognition_result is not None
        if stage == 1:
            assert workflow.element_recognition_request_metadata.default_panel_orientation.value == "P-N"
    finally:
        if blocker is not None:
            blocker.close()
        assert vm.shutdown()
        bridge.close()
        tab.close()
        window.close()


def test_modified_pdf_rejects_old_results(prepared, drawing):
    _, _, session = prepared
    session.status()
    with drawing.open("ab") as stream:
        stream.write(b"\n% modified\n")
    assert session.status()["source_pdf_changed"]
    with pytest.raises(ValueError, match="Source PDF changed"):
        session.evidence(1)


def test_unanchored_manual_nodes_survive_unrelated_correction(prepared):
    workflow, _, session = prepared
    result = session.result()
    inv = result.reviewed_tree.invs[0]
    tree = replace(result.reviewed_tree, invs=(replace(inv, nodes=(*inv.nodes, NodeDraft("Manual1"), NodeDraft("Manual2"))),))
    candidate = correct_result(replace(result, reviewed_tree=tree),
                               workflow.element_recognition_request_metadata, [addition()])
    assert [n.name for n in candidate.reviewed_tree.invs[0].nodes] == ["Node1", "Manual1", "Manual2"]


def test_start_session_launches_and_reconnects_real_gui(tmp_path, drawing, monkeypatch):
    import subprocess
    import count_and_design.integration.mcp_server as module
    processes = []
    original = subprocess.Popen

    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    # Isolate the child's app-data/logging location from ordinary user data.
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    client = BridgeClient(tmp_path / "launched" / "bridge.json")
    try:
        started = client.start()
        assert started["page_count"] == 0
        imported = client.call("import_pdf", path=str(drawing))
        assert imported["page_count"] == 2
        assert client.start()["project_id"] == imported["project_id"]
        assert len(processes) == 1
    finally:
        # This test owns exactly the child processes recorded above.
        for process in processes:
            process.terminate()
            process.wait(timeout=15)


def test_multiple_real_windows_keep_projects_separate(tmp_path, drawing, monkeypatch):
    import subprocess
    import count_and_design.integration.mcp_server as module
    from count_and_design.application.recognition_queue import RecognitionTicket
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("COUNT_DESIGN_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COUNT_DESIGN_QUEUE_DIR", str(tmp_path / "queue"))
    monkeypatch.delenv("COUNT_DESIGN_BRIDGE_FILE", raising=False)
    processes = []
    original = subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    client = BridgeClient()
    try:
        a = client.start(new_window=True)
        imported = client.call("import_pdf", path=str(drawing))
        # A new Codex client auto-connects to the sole preloaded window, but
        # importing there must preserve the PDF even before recognition.
        newcomer = BridgeClient()
        assert newcomer.start()["project_id"] == imported["project_id"]
        with pytest.raises(ValueError, match="new_window=True"):
            newcomer.call("import_pdf", path=str(drawing))
        assert newcomer.call("get_job_status")["project_id"] == imported["project_id"]
        b = client.start(new_window=True)
        assert a["window_id"] != b["window_id"]
        assert b["page_count"] == 0
        assert len(client.list_sessions()["sessions"]) == 2
        assert client.start(selected_window_id=a["window_id"])["project_id"] == imported["project_id"]
        assert client.call("get_project_context")["project_id"] == imported["project_id"]
        client.start(selected_window_id=b["window_id"])
        imported_b = client.call("import_pdf", path=str(drawing))
        assert imported_b["project_id"] != imported["project_id"]
        client.start(selected_window_id=a["window_id"])
        assert client.call("get_project_context")["project_id"] == imported["project_id"]
        # An unselected client must never silently pick among multiple projects.
        with pytest.raises(ValueError, match="Multiple windows"):
            BridgeClient().start()
        assert len(processes) == 2
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=15)


def test_classification_review_scores_gaps_and_correction(drawing, tmp_path):
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(drawing, 2)
    workflow.complete_page_classification(PageClassificationResult(pages=(
        PageClassification(1, "Other", source=SourceReference(confidence=.49,
            raw={"min_score": .5, "category_scores": [{"category": "Overall Layout", "score": .49}]})),
        PageClassification(2, "Array Layout"),
    )))
    session = ReviewSession(AIStatisticsViewModel(workflow), tmp_path / "review")
    context = session.context()
    assert context["agent1_review"]["pages"][0]["near_threshold"]
    assert context["agent1_review"]["layout_pages"] == [2]
    assert session.issues()["total"] == 0
    evidence = session.evidence(1)["metadata"]
    preview = session.preview_stage_corrections(session.project_id, session.revision, 0,
        [{"page_number": 1, "page_type": "Overall Layout"}], [evidence["evidence_id"]],
        "Original page contains the overall site table")
    assert workflow.snapshot.page_classification.pages[0].page_type == "Other"
    session.apply(session.project_id, session.revision, preview["preview_id"], "classify-fix")
    assert workflow.snapshot.page_classification.pages[0].page_type == "Overall Layout"
    with pytest.raises(ValueError, match="stale"):
        session.confirm_stage_review(session.project_id, session.revision, 0,
                                     [evidence["evidence_id"]], "Old image")


def test_compact_context_preserves_triage_and_full_candidates(drawing, tmp_path):
    scores = [{"category": f"candidate-{n}", "score": n / 1000} for n in range(100)]
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(drawing, 2)
    workflow.complete_page_classification(PageClassificationResult(pages=(
        PageClassification(1, "Other", source=SourceReference(
            raw={"min_score": .1, "category_scores": scores})),
        PageClassification(2, "Array Layout"),
    )))
    session = ReviewSession(AIStatisticsViewModel(workflow), tmp_path / "review")
    compact = session.context(stage=0)
    full = session.context(detail="full", stage=0)
    assert "agent2_review" not in compact and "diagnostics" not in compact
    for key in ("page_classification", "project_id", "revision"):
        assert compact[key] == full[key]
    for key in ("review_pages", "suspicious_pages", "layout_gaps"):
        assert compact["agent1_review"][key] == full["agent1_review"][key]
    page = compact["agent1_review"]["pages"][0]
    assert page["near_threshold"] and page["score"] == .099
    assert [c["category"] for c in page["category_scores"]] == [
        "candidate-99", "candidate-98", "candidate-97"]
    assert len(full["agent1_review"]["pages"][0]["category_scores"]) == 100
    assert len(json.dumps(compact)) < len(json.dumps(full)) / 2
    assert "agent1_review" not in session.context(stage=1)
    assert "raw" not in session.context(stage=2)["diagnostics"]
    assert "raw" in session.context(detail="full", stage=2)["diagnostics"]
    with pytest.raises(ValueError):
        session.context(stage=True)
    with pytest.raises(ValueError):
        session.context(detail="unknown")


def test_classification_followup_reuses_evidence_and_receipt(drawing, tmp_path, monkeypatch):
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(drawing, 2)
    workflow.complete_page_classification(PageClassificationResult(pages=(
        PageClassification(1, "Other"), PageClassification(2, "Other"))))
    session = ReviewSession(AIStatisticsViewModel(workflow), tmp_path / "review")
    ids = [session.evidence(n)["metadata"]["evidence_id"] for n in (1, 2)]
    # A follow-up edit must not need to render already inspected originals again.
    monkeypatch.setattr("count_and_design.integration.stage_review.render_high_resolution_page",
                        Mock(side_effect=AssertionError("unexpected render")))
    context = session.context(stage=0)
    assert {e["evidence_id"] for e in context["available_evidence"]} == set(ids)
    changes = [{"page_number": n, "page_type": "Array Layout"} for n in (1, 2)]
    preview = session.preview_stage_corrections(session.project_id, session.revision, 0,
        changes, ids, "Previously inspected originals show arrays")
    receipt = session.apply(session.project_id, context["revision"], preview["preview_id"], "reuse")
    assert receipt["revision"] > context["revision"]
    assert receipt["applied_changes"] == changes
    assert receipt["classification_counts"] == {"Overall Layout": 0, "Array Layout": 2, "Other": 0}
    assert session.context(stage=0)["available_evidence"] == []
    assert session.apply(session.project_id, context["revision"], preview["preview_id"], "reuse") == receipt
    with pytest.raises(ValueError, match="stale"):
        session.preview_stage_corrections(session.project_id, receipt["revision"], 0,
            changes, ids, "Old evidence must still be rejected")


def test_stage_review_full_sequence_and_agent3_recovery(drawing, tmp_path):
    from conftest import sample_agent2_extraction
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(drawing, 2)
    workflow.complete_page_classification(PageClassificationResult(pages=(
        PageClassification(1, "Array Layout"), PageClassification(2, "Overall Layout"))))
    vm = AIStatisticsViewModel(workflow)
    session = ReviewSession(vm, tmp_path / "review")
    evidence = [session.evidence(n)["metadata"]["evidence_id"] for n in (1, 2)]
    session.confirm_stage_review(session.project_id, session.revision, 0, evidence, "Both pages checked")
    workflow.complete_overall_site_recognition((replace(sample_agent2_extraction(), page_number=2,
        inv_list=("INV1",), inv_count=1),))
    context = session.context()
    assert "0" in context["stage_reviews"]
    changes = context["agent2_review"]["extractions"]
    changes[0]["tracker_ocr_fields"][0]["num_str_details"][0]["count"] = "20"
    e2 = session.evidence(2)["metadata"]["evidence_id"]
    preview = session.preview_stage_corrections(session.project_id, session.revision, 1,
        changes, [e2], "Read Tracker quantity from original")
    assert preview["tracker_definitions"][0]["total_count"] == 20
    session.apply(session.project_id, session.revision, preview["preview_id"], "info-fix")
    assert workflow.agent2_tracker_definitions[0].total_count == 20
    assert workflow.snapshot.element_recognition_result is None
    e2 = session.evidence(2)["metadata"]["evidence_id"]
    session.confirm_stage_review(session.project_id, session.revision, 1, [e2], "Tracker and INV checked")
    workflow.fail_element_recognition("YOLO provider failed")
    assert session.context()["diagnostics"]["missing_result_pages"] == [1]
    assert session.context()["diagnostics"]["last_failure"]["error"] == "YOLO provider failed"
    e1 = session.evidence(1)["metadata"]
    op = {"op": "initialize_page", "page_number": 1, "inv_name": "INV1",
          "image_width": e1["image_width"], "image_height": e1["image_height"],
          "reason": "Recover original failed layout page"}
    node = {"op": "add_detection", "kind": "Node", "page_number": 1, "name": "INV1-1",
            "inv_name": "INV1", "bbox": [.05, .05, .9, .9], "reason": "Node boundary in original"}
    preview = session.preview(session.project_id, session.revision, [op, node], [e1["evidence_id"]])
    assert len(preview["changed_nodes"]) == 1
    session.apply(session.project_id, session.revision, preview["preview_id"], "recover-page")
    assert session.result().reviewed_tree.invs[0].nodes[0].name == "INV1-1"
    assert session.context()["diagnostics"]["missing_result_pages"] == []
    assert {"0", "1"} <= session.context()["stage_reviews"].keys()
    fresh = session.evidence(1)["metadata"]["evidence_id"]
    session.confirm_stage_review(session.project_id, session.revision, 2, [fresh],
                                  "Recovered Node; missing children remain documented in issues")
    assert "2" in session.context()["stage_reviews"]


def test_consolidated_overall_review_requires_all_original_pages(drawing, tmp_path):
    from conftest import sample_agent2_extraction
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(drawing, 2)
    workflow.complete_page_classification(PageClassificationResult(pages=(
        PageClassification(1, "Overall Layout"), PageClassification(2, "Overall Layout"))))
    workflow.complete_overall_site_recognition((replace(sample_agent2_extraction(),
        raw={"consolidated_page_numbers": [1, 2]}),))
    session = ReviewSession(AIStatisticsViewModel(workflow), tmp_path / "review")
    changes = session.context()["agent2_review"]["extractions"]
    e1 = session.evidence(1)["metadata"]["evidence_id"]
    with pytest.raises(ValueError, match="Every edited page"):
        session.preview_stage_corrections(session.project_id, session.revision, 1,
                                          changes, [e1], "Merged duplicate drawing checked")
    second = session.evidence(2)["metadata"]
    assert second["agent2"][0]["raw"]["consolidated_page_numbers"] == [1, 2]
    preview = session.preview_stage_corrections(session.project_id, session.revision, 1,
        changes, [e1, second["evidence_id"]], "Both duplicate originals checked")
    assert preview["tracker_definitions"]


def test_layout_gaps_and_below_threshold_pages_are_review_candidates(drawing, tmp_path):
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(drawing, 6)
    workflow.complete_page_classification(PageClassificationResult(pages=tuple(
        PageClassification(n, "Array Layout" if n in (2, 4) else "Other",
            source=SourceReference(confidence=.2, raw={"min_score": .21}))
        for n in range(1, 7))))
    review = ReviewSession(AIStatisticsViewModel(workflow), tmp_path / "review").classification_review()
    assert review["layout_gaps"] == [3]
    assert review["layout_pages"] == [2, 4]
    assert review["review_pages"] == [1, 2, 3, 4, 5, 6]


def test_failed_detection_context_serializes_for_http(prepared):
    from count_and_design.application.ai_statistics.element_recognition.contracts import (
        RecognitionIssue, RecognitionIssueSeverity,
    )
    workflow, vm, session = prepared
    result = workflow.snapshot.element_recognition_result
    issue = RecognitionIssue("tracker_detection_failed", "YOLO failed on page 1",
                             RecognitionIssueSeverity.ERROR, page_number=1)
    vm.replace_element_result(replace(result, issues=(issue,)))
    payload = json.loads(json.dumps(session.context()))
    assert payload["diagnostics"]["execution_issues"][0]["severity"] == "error"


def test_result_triage_does_not_require_images_or_include_normal_pages(drawing, tmp_path):
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.load_pdf(drawing, 2)
    workflow.complete_page_classification(PageClassificationResult(pages=(
        PageClassification(1, "Array Layout", source=SourceReference(confidence=.9, raw={"min_score": .5})),
        PageClassification(2, "Overall Layout", source=SourceReference(confidence=.9, raw={"min_score": .5})),
    )))
    session = ReviewSession(AIStatisticsViewModel(workflow), tmp_path / "review")
    assert session.context()["agent1_review"]["review_pages"] == []
    status = session.confirm_stage_review(session.project_id, session.revision, 0, [],
        "Read existing classification: Layout 1, Overall 2; no score or continuity suspects")
    assert status["stage_reviews"]["0"]["review_mode"] == "result_triage"
    assert status["stage_reviews"]["0"]["inspected_pages"] == []
    assert not session._evidence

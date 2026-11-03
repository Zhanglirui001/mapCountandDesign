from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from count_and_design.application.ai import BoundingBox, DetectionResult
from count_and_design.application.ai_statistics import (
    Agent3ElementRecognizer,
    ArrayLayoutPageInput,
    BlockDiagramTableResult,
    BlockDiagramTableRow,
    ElementRecognitionRequest,
    NodeNameReviewCandidate,
    NodeNameReviewError,
    NodeNameReviewer,
    NodeStringTableRow,
    TrackerDefinition,
    build_node_name_review_prompt,
    parse_node_name_review_response,
    parse_node_name_review_result,
)


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (400, 240), "white").save(output, format="PNG")
    return output.getvalue()


def test_claude_review_request_contains_full_page_table_and_numbered_boxes() -> None:
    page = _png()

    class _Claude:
        def __init__(self) -> None:
            self.calls = []

        def complete_with_image(self, prompt, image, **kwargs):
            self.calls.append((prompt, image, kwargs))
            return SimpleNamespace(
                content=(
                    '```json\n{"inv_name":"INV1001","corrections":['
                    '{"number":1,"name":"INV1001-CBX-01"},'
                    '{"number":2,"name":"INV1001-CBX-02"}]}\n```'
                ),
                model="claude-test",
                usage={"inputTokens": 10},
            )

    claude = _Claude()
    reviewer = NodeNameReviewer(claude)
    result = reviewer.review(
        page,
        (
            NodeStringTableRow("INV1001-CBX-10", 30),
            NodeStringTableRow("INV1001-CBX-2", 24),
            NodeStringTableRow("INV1001-CBX-01", 20),
        ),
        (
            NodeNameReviewCandidate(
                1,
                "INV1001-C8X-01",
                BoundingBox(10, 20, 190, 220),
            ),
            NodeNameReviewCandidate(
                2,
                "Node2",
                BoundingBox(210, 20, 390, 220),
            ),
        ),
        ("INV1001", "INV2002"),
    )

    assert claude.calls[0][1] == page
    prompt = claude.calls[0][0]
    assert '"node":"INV1001-CBX-01","string_number":20' in prompt
    table_json = prompt.split("Table Node-name/string-count JSON:\n", 1)[1].split(
        "\nTable rows unmatched by any preliminary Node name:",
        1,
    )[0]
    assert [row["node"] for row in json.loads(table_json)] == [
        "INV1001-CBX-01",
        "INV1001-CBX-2",
        "INV1001-CBX-10",
    ]
    unmatched_json = prompt.split(
        "Table rows unmatched by any preliminary Node name:\n",
        1,
    )[1].split("\nNumbered recognized Nodes and bounding boxes:", 1)[0]
    assert json.loads(unmatched_json) == ["INV1001-CBX-2", "INV1001-CBX-10"]
    assert 'Agent2 INV-name JSON:\n["INV1001","INV2002"]' in prompt
    assert '"number":1,"recognized_name":"INV1001-C8X-01"' in prompt
    assert '"bbox_pixels":{"x1":10' in prompt
    assert result.corrections_by_number == {
        1: "INV1001-CBX-01",
        2: "INV1001-CBX-02",
    }
    assert result.inv_name == "INV1001"
    assert result.all_correct is False
    assert result.raw_output["model"] == "claude-test"


@pytest.mark.parametrize(
    "content",
    (
        '{"corrections":[{"number":3,"name":"A"}]}',
        '{"corrections":[{"number":1,"name":"A"},'
        '{"number":1,"name":"B"}]}',
        '{"corrections":[{"number":1,"name":"A"},'
        '{"number":2,"name":""}]}',
    ),
)
def test_claude_review_parser_rejects_incomplete_or_ambiguous_mappings(
    content: str,
) -> None:
    with pytest.raises(NodeNameReviewError):
        parse_node_name_review_response(content, expected_numbers=(1, 2))


def test_review_prompt_defines_node_names_and_flags_connection_annotations() -> None:
    prompt = build_node_name_review_prompt(
        (
            NodeStringTableRow("INV1001-CBX-12", 30),
            NodeStringTableRow("INV1001-CBX-13", 24),
            NodeStringTableRow("INV1001-CBX-14", 20),
        ),
        (
            NodeNameReviewCandidate(
                1, "INV1001-CBX-12", BoundingBox(0, 0, 10, 10)
            ),
            NodeNameReviewCandidate(
                2, "CBX13-to-CBX14", BoundingBox(10, 0, 20, 10)
            ),
            NodeNameReviewCandidate(
                3, "INV1001-CBX-14", BoundingBox(20, 0, 30, 10)
            ),
        ),
        expected_inv_names=("INV1001",),
        image_width=100,
        image_height=100,
    )

    # The prompt must name the connection-annotation failure mode explicitly
    # and must state that the table is the only authority for Node names.
    assert '"CBX13-to-CBX14"' in prompt
    assert "wire labels, not equipment names" in prompt
    assert "only authority for Node names" in prompt
    # Format is relative: one page follows one format, shown by the table and
    # the names that already match it. Do not prescribe a fixed template.
    assert "share one format" in prompt
    assert "<INV>-<CBX|LBD>-<sequence>" not in prompt
    # The annotation "claims" neither CBX-14 nor CBX-13, so the row whose
    # Node carries it is reported as unmatched and must be paired by Claude.
    unmatched_json = prompt.split(
        "Table rows unmatched by any preliminary Node name:\n",
        1,
    )[1].split("\nNumbered recognized Nodes and bounding boxes:", 1)[0]
    assert json.loads(unmatched_json) == ["INV1001-CBX-13"]
    # The Agent2 list authorizes the INV, the page decides its spelling.
    assert "decides which INV this page belongs to" in prompt
    assert "most complete spelling this page shows" in prompt
    assert "INV1201, INV 1201 and 1201 are the same" in prompt
    assert "This rule governs inv_name only" in prompt
    # The compact escape hatch stays available but is constrained.
    assert "If and only if no Node needs a correction" in prompt
    assert "Return every supplied number exactly once" not in prompt


def test_claude_review_accepts_compact_all_correct_with_inv() -> None:
    parsed = parse_node_name_review_result(
        '{"inv_name":"inv-1001","corrections":"All correct"}',
        expected_numbers=(1, 2),
        current_names_by_number={1: "INV1001-CBX-01", 2: "INV1001-CBX-02"},
        expected_inv_names=("INV 1001", "INV 2002"),
    )

    assert parsed.all_correct
    assert parsed.inv_name == "INV 1001"
    assert parsed.corrections_by_number == {}


def test_bare_all_correct_marker_keeps_names_and_leaves_inv_for_fallback() -> None:
    parsed = parse_node_name_review_result(
        "```text\nAll correct\n```",
        expected_numbers=(1,),
        current_names_by_number={1: "INV1001-CBX-01"},
        expected_inv_names=("INV 1001",),
    )

    assert parsed.all_correct
    assert parsed.inv_name is None
    assert parsed.corrections_by_number == {}


def test_page_inv_spelling_keeps_the_full_page_form_of_a_listed_inv() -> None:
    parsed = parse_node_name_review_result(
        '{"inv_name":"INV1201","corrections":"All correct"}',
        expected_numbers=(1,),
        expected_inv_names=("1201", "1202"),
    )

    assert parsed.inv_name == "INV1201"
    assert parsed.all_correct


def test_page_inv_spelling_upgrades_to_a_more_complete_listed_form() -> None:
    parsed = parse_node_name_review_result(
        '{"inv_name":"1201","corrections":"All correct"}',
        expected_numbers=(1,),
        expected_inv_names=("INV 1201",),
    )

    assert parsed.inv_name == "INV 1201"


@pytest.mark.parametrize(
    "returned",
    ("INV1201A", "INV12011", "1201-2", "INVENTED"),
)
def test_page_inv_spelling_never_leaves_the_agent2_list(returned: str) -> None:
    parsed = parse_node_name_review_result(
        f'{{"inv_name":"{returned}","corrections":"All correct"}}',
        expected_numbers=(1,),
        expected_inv_names=("1201",),
    )

    assert parsed.inv_name is None
    assert parsed.all_correct


def test_unrecognized_claude_inv_is_ignored_without_losing_node_mapping() -> None:
    parsed = parse_node_name_review_result(
        '{"inv_name":"INVENTED","corrections":['
        '{"number":1,"name":"INV1001-CBX-01"}]}',
        expected_numbers=(1,),
        current_names_by_number={1: "wrong"},
        expected_inv_names=("INV1001",),
    )

    assert parsed.inv_name is None
    assert parsed.corrections_by_number == {1: "INV1001-CBX-01"}


def test_partial_correction_list_does_not_require_unchanged_nodes() -> None:
    parsed = parse_node_name_review_result(
        '{"inv_name":"INV1001","corrections":['
        '{"number":2,"name":"INV1001-CBX-02"}]}',
        expected_numbers=(1, 2, 3),
        expected_inv_names=("INV1001",),
    )

    assert not parsed.all_correct
    assert parsed.corrections_by_number == {2: "INV1001-CBX-02"}


def test_claude_corrected_mapping_becomes_downstream_node_input() -> None:
    class _Yolo:
        def detect(self, _image, *, model_type, confidence=None):
            del confidence
            if model_type == "tracker":
                return SimpleNamespace(
                    detections=[
                        DetectionResult(
                            "Node", 0.95, BoundingBox(0, 0, 190, 240)
                        ),
                        DetectionResult(
                            "Node", 0.94, BoundingBox(210, 0, 400, 240)
                        ),
                    ]
                )
            return SimpleNamespace(detections=[])

    class _Table:
        def recognize(self, _image):
            return BlockDiagramTableResult(
                project_type="IBEX",
                equipment_family="CBX",
                string_info=(
                    BlockDiagramTableRow("INV1001-CBX-01", 20),
                    BlockDiagramTableRow("INV1001-CBX-02", 24),
                ),
            )

    class _Ocr:
        def parse_file(self, _image, *, filename, **_kwargs):
            # Node 1 is already correct; Claude only needs to return Node 2.
            return SimpleNamespace(
                markdown=(
                    "INV1001-CBX-01" if "node-1" in filename else ""
                )
            )

    class _Reviewer:
        def __init__(self) -> None:
            self.candidates = ()

        def review(self, image, table_rows, candidates, expected_inv_names):
            del image, table_rows
            assert expected_inv_names == ("INV1001",)
            self.candidates = candidates
            return SimpleNamespace(
                corrections_by_number={2: "INV1001-CBX-02"},
                inv_name="INV1001",
                all_correct=False,
                prompt="review prompt",
                raw_output={"content": "ok"},
            )

    reviewer = _Reviewer()
    result = Agent3ElementRecognizer(
        yolo=_Yolo(),
        table_recognizer=_Table(),
        node_ocr=_Ocr(),
        node_name_reviewer=reviewer,
        node_name_review_enabled=True,
    ).recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(1, _png()),),
            tracker_definitions=(TrackerDefinition("2-STR", 2),),
            expected_inv_names=("INV1001",),
        )
    )

    nodes = result.reviewed_tree.invs[0].nodes
    assert result.reviewed_tree.invs[0].name == "INV1001"
    assert [node.name for node in nodes] == [
        "INV1001-CBX-01",
        "INV1001-CBX-02",
    ]
    assert [item.number for item in reviewer.candidates] == [1, 2]
    assert not any(
        issue.code == "node_name_unmatched" for issue in result.pages[0].issues
    )
    debug = result.raw["debug_pages"][0]["claude_node_name_review"]
    assert debug["corrected_node_names"] == [{
        "number": 2,
        "name": "INV1001-CBX-02",
    }]
    assert debug["inv_name"] == "INV1001"
    assert debug["inv_name_source"] == "claude"


def test_claude_review_failure_falls_back_and_is_traceable() -> None:
    class _Reviewer:
        def review(self, *_args):
            raise NodeNameReviewError(
                "invalid mapping",
                raw_llm={"content": "not-json"},
            )

    class _Yolo:
        def detect(self, _image, *, model_type, confidence=None):
            del model_type, confidence
            return SimpleNamespace(
                detections=[
                    DetectionResult("Node", 0.95, BoundingBox(0, 0, 400, 240))
                ]
            )

    class _Table:
        def recognize(self, _image):
            return BlockDiagramTableResult(
                project_type="IBEX",
                equipment_family="CBX",
                string_info=(BlockDiagramTableRow("INV1001-CBX-01", 20),),
            )

    class _Ocr:
        def parse_file(self, *_args, **_kwargs):
            return SimpleNamespace(markdown="INV1001-CBX-01")

    result = Agent3ElementRecognizer(
        yolo=_Yolo(),
        table_recognizer=_Table(),
        node_ocr=_Ocr(),
        node_name_reviewer=_Reviewer(),
        node_name_review_enabled=True,
    ).recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(1, _png()),),
            tracker_definitions=(TrackerDefinition("2-STR", 2),),
        )
    )

    assert result.reviewed_tree.invs[0].nodes[0].name == "INV1001-CBX-01"
    assert any(
        issue.code == "node_name_review_failed"
        for issue in result.pages[0].issues
    )
    debug = result.raw["debug_pages"][0]["claude_node_name_review"]
    assert debug["raw_llm"] == {"content": "not-json"}
    json.dumps(result.to_dict(), ensure_ascii=False)


def test_missing_claude_inv_uses_legacy_node_format_inference() -> None:
    class _Reviewer:
        def review(self, _image, _rows, _candidates, _expected_invs):
            return SimpleNamespace(
                corrections_by_number={},
                inv_name=None,
                all_correct=True,
                prompt="compact response without inv_name",
                raw_output={"content": "All correct"},
            )

    class _Yolo:
        def detect(self, _image, *, model_type, confidence=None):
            del model_type, confidence
            return SimpleNamespace(
                detections=[
                    DetectionResult("Node", 0.95, BoundingBox(0, 0, 400, 240))
                ]
            )

    class _Table:
        def recognize(self, _image):
            return BlockDiagramTableResult(
                project_type="IBEX",
                equipment_family="CBX",
                string_info=(BlockDiagramTableRow("INV1001-CBX-01", 20),),
            )

    class _Ocr:
        def parse_file(self, *_args, **_kwargs):
            return SimpleNamespace(markdown="INV1001-CBX-01")

    result = Agent3ElementRecognizer(
        yolo=_Yolo(),
        table_recognizer=_Table(),
        node_ocr=_Ocr(),
        node_name_reviewer=_Reviewer(),
        node_name_review_enabled=True,
    ).recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(1, _png()),),
            tracker_definitions=(TrackerDefinition("2-STR", 2),),
            expected_inv_names=("INV1001",),
        )
    )

    inv = result.reviewed_tree.invs[0]
    assert inv.name == "INV 1001"
    assert inv.source.raw["inv_name_source"] == "node_name_format_fallback"


def test_node_review_switch_disables_review_and_keeps_preliminary_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Reviewer:
        def __init__(self) -> None:
            self.calls = 0

        def review(self, *_args):
            self.calls += 1
            raise AssertionError("review must not run while disabled")

    class _Yolo:
        def detect(self, _image, *, model_type, confidence=None):
            del model_type, confidence
            return SimpleNamespace(
                detections=[
                    DetectionResult("Node", 0.95, BoundingBox(0, 0, 400, 240))
                ]
            )

    class _Table:
        def recognize(self, _image):
            return BlockDiagramTableResult(
                project_type="IBEX",
                equipment_family="CBX",
                string_info=(BlockDiagramTableRow("INV1001-CBX-77", 20),),
            )

    class _Ocr:
        def parse_file(self, *_args, **_kwargs):
            return SimpleNamespace(markdown="INV1001-CBX-01")

    reviewer = _Reviewer()
    monkeypatch.setenv("AGENT3_NODE_REVIEW_ENABLED", "false")
    result = Agent3ElementRecognizer(
        yolo=_Yolo(),
        table_recognizer=_Table(),
        node_ocr=_Ocr(),
        node_name_reviewer=reviewer,
    ).recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(1, _png()),),
            tracker_definitions=(TrackerDefinition("2-STR", 2),),
        )
    )

    assert reviewer.calls == 0
    debug = result.raw["debug_pages"][0]["claude_node_name_review"]
    assert debug["enabled"] is False
    assert debug["status"] == "skipped"
    assert "AGENT3_NODE_REVIEW_ENABLED" in str(debug["reason"])
    # The preliminary crop reading survives because the reviewer never ran.
    assert result.reviewed_tree.invs[0].nodes[0].name == "INV1001-CBX-01"
    json.dumps(result.to_dict(), ensure_ascii=False)

"""Regression tests for Agent3 name matching and OCR orientation voting.

Covers the Flat Fork drawing failure: plan labels print "-CBX- INV11A101 01"
(CBX first, rotated) while the DC cable schedule rows print
"INV11A101-CBX-01", so the previous directional substring match failed for
every node and the whole project tree collapsed into generic NodeN drafts.
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from count_and_design.application.ai import BoundingBox, DetectionResult
from count_and_design.application.ai_statistics.element_recognition import (
    Agent3ElementRecognizer,
    ArrayLayoutPageInput,
    BlockDiagramTableResult,
    ElementRecognitionRequest,
    NodeStringTableRow,
    TrackerDefinition,
)

from count_and_design.application.ai_statistics.element_recognition.block_diagram_table.common import (
    BlockDiagramTableRow,
)
from count_and_design.application.ai_statistics.element_recognition.node_label import (
    _clean_node_name_completion,
    _equipment_signature,
    _extract_inv_prefix,
    _is_no_match_completion,
    _match_table_name,
    _normalize_name,
    _ocr_text_quality,
    _one_edit_apart,
    _service_ocr_confidence,
)


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (400, 240), "white").save(output, format="PNG")
    return output.getvalue()


def _table(*rows: str) -> dict[str, BlockDiagramTableRow]:
    return {
        _normalize_name(name): BlockDiagramTableRow(name, 12 + index)
        for index, name in enumerate(rows)
    }


def test_qwen_node_prompt_sends_names_only_and_uses_original_crop() -> None:
    class _Qwen:
        def __init__(self) -> None:
            self.calls = []

        def complete_with_image(self, prompt, image, **kwargs):
            self.calls.append((prompt, image, kwargs))
            return SimpleNamespace(content='"INV11A101-CBX-01"')

    qwen = _Qwen()
    recognizer = Agent3ElementRecognizer(
        yolo=object(),
        table_recognizer=object(),
        node_qwen=qwen,
    )
    text, confidence = recognizer._recognize_node_crop(
        Image.new("RGB", (100, 40), "white"),
        BoundingBox(0, 0, 100, 40),
        1,
        1,
        (NodeStringTableRow("INV11A101-CBX-01", 45),),
    )

    assert text == "INV11A101-CBX-01"
    assert confidence is None
    assert '["INV11A101-CBX-01"]' in qwen.calls[0][0]
    # The string count is recognition-irrelevant and must not reach the model.
    assert "string_number" not in qwen.calls[0][0]
    assert "45" not in qwen.calls[0][0]
    assert "No Match" in qwen.calls[0][0]
    assert "original drawing orientation" in qwen.calls[0][0]
    assert "temperature" not in qwen.calls[0][2]
    assert "max_completion_tokens" not in qwen.calls[0][2]


def test_no_match_completion_becomes_an_empty_reading() -> None:
    assert _clean_node_name_completion("No Match") == ""
    assert _clean_node_name_completion('"no-match"') == ""
    assert _clean_node_name_completion("N/A") == ""
    assert _clean_node_name_completion("None") == ""
    assert _clean_node_name_completion("```text\nNo Match\n```") == ""
    assert _clean_node_name_completion("") == ""
    assert _clean_node_name_completion("INV11A101-CBX-01") == (
        "INV11A101-CBX-01"
    )


def test_no_match_detection_ignores_a_real_name() -> None:
    assert _is_no_match_completion(" No  Match. ") is True
    assert _is_no_match_completion("INV11A101-CBX-01") is False


def test_no_match_reading_keeps_a_placeholder_and_reports_it() -> None:
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

    class _QwenNoMatch:
        def complete_with_image(self, _prompt, _image, **_kwargs):
            return SimpleNamespace(content="No Match", model="fake", usage={})

    result = Agent3ElementRecognizer(
        yolo=_Yolo(),
        table_recognizer=_Table(),
        node_qwen=_QwenNoMatch(),
    ).recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(1, _png()),),
            tracker_definitions=(TrackerDefinition("2-STR", 2),),
        )
    )

    assert result.reviewed_tree.invs[0].nodes[0].name == "Node1"
    assert any(
        issue.code == "node_name_no_match" for issue in result.pages[0].issues
    )
    debug_nodes = result.raw["debug_pages"][0]["ocr_node_names"]
    assert debug_nodes[0]["no_match"] is True
    assert debug_nodes[0]["selected"]["text"] == ""


def test_cbx_first_label_matches_inv_first_schedule_row() -> None:
    table = _table("INV11A101-CBX-01", "INV11A101-CBX-02")

    assert _match_table_name("-CBX- INV11A101 01", table, set()) == (
        "INV11A101-CBX-01"
    )
    assert _match_table_name("-CBX- INV11A101 02", table, set()) == (
        "INV11A101-CBX-02"
    )


def test_exact_substring_match_still_wins() -> None:
    table = _table("INV1001-CBX01")

    assert (
        _match_table_name("INV 1001-CBX01", table, set()) == "INV1001-CBX01"
    )


def test_matched_rows_are_not_reused() -> None:
    table = _table("INV11A101-CBX-01")

    assert (
        _match_table_name(
            "-CBX- INV11A101 01",
            table,
            {_normalize_name("INV11A101-CBX-01")},
        )
        == ""
    )


def test_unknown_sequence_never_matches() -> None:
    table = _table("INV11A101-CBX-01", "INV11A101-CBX-02")

    assert _match_table_name("-CBX- INV11A101 09", table, set()) == ""


def test_ocr_inserted_character_is_tolerated_when_unambiguous() -> None:
    table = _table("INV11A101-CBX-11", "INV11A101-CBX-12")

    assert (
        _match_table_name("INV111A101 -CBX-11", table, set())
        == "INV11A101-CBX-11"
    )


def test_pure_numeric_inv_signature_matches() -> None:
    table = _table("INV1101-CBX-03")

    assert _match_table_name("CBX- 1101 03", table, set()) == "INV1101-CBX-03"


def test_equipment_signature_is_order_insensitive() -> None:
    cbx_first = _equipment_signature("-CBX- INV11A101 01")
    inv_first = _equipment_signature("INV11A101 -CBX-01")

    assert cbx_first == inv_first == ("11A101", "CBX", 1)


def test_one_edit_apart() -> None:
    assert _one_edit_apart("11A101", "111A101")
    assert _one_edit_apart("11A101", "1A101")
    assert _one_edit_apart("11A101", "11A102")
    assert not _one_edit_apart("11A101", "11B201")
    assert not _one_edit_apart("11A101", "111A1011")


def test_inv_prefix_supports_cbx_first_labels() -> None:
    assert _extract_inv_prefix("-CBX- INV11A101 01") == "INV 11A101"
    assert _extract_inv_prefix("INV1001-CBX-01") == "INV 1001"
    assert _extract_inv_prefix("-LBD- 12B105") == "INV 12B105"
    assert _extract_inv_prefix("Node1") == ""


def test_inv_prefix_accepts_any_equipment_token_separator() -> None:
    # Real drawings print CBX/LBD followed by whatever separator the CAD
    # template uses; the token must not disappear behind "." or "/".
    assert _extract_inv_prefix("INV1103-CBX.22") == "INV 1103"
    assert _extract_inv_prefix("INV1103-CBX/22") == "INV 1103"
    assert _extract_inv_prefix("-CBX. INV11A101 01") == "INV 11A101"


def test_ocr_quality_prefers_complete_names_over_noise() -> None:
    garbled = "-CBX-"
    merged = "01 -CBX- 03 FEEDER-11A1"
    complete = "INV11A101 -CBX-01"

    best = max((garbled, merged, complete), key=_ocr_text_quality)

    assert best == complete


class _Parsed:
    def __init__(self, raw: object) -> None:
        self.raw = raw


def test_service_confidence_is_honored_when_present() -> None:
    parsed = _Parsed({"results": [{"text": "x", "confidence": 0.87}]})

    assert _service_ocr_confidence(parsed) == 0.87


def test_service_confidence_is_none_without_field() -> None:
    parsed = _Parsed({"results": ["plain text"]})

    assert _service_ocr_confidence(parsed) is None

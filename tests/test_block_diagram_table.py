from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from count_and_design.application.ai import BoundingBox, DetectionResult
from count_and_design.application.ai.llm.types import LlmServiceError
from count_and_design.application.ai_statistics.element_recognition.block_diagram_table import (
    STRATEGY_MINERU_CLAUDE,
    STRATEGY_YOLO_CLAUDE,
    STRATEGY_YOLO_GLM,
    BlockDiagramTableError,
    BlockDiagramTableRecognizer,
    _bedrock_image_transport_size,
    _compact_table_markdown,
    _compress_image_for_bedrock,
    _html_table_to_plain_text,
    _normalize_equipment_family,
    _score_equipment_table_html,
    _select_all_region_blocks,
    _select_best_table_markdown,
    _select_region_by_iou,
    _select_table_bbox,
)
from count_and_design.application.config import (
    BlockDiagramTableConfig,
    OpenRouterLlmConfig,
)
from dataclasses import replace
from count_and_design.application.ai_statistics.element_recognition.block_diagram_table.common import (
    build_result,
    expand_bbox_uniformly,
    llm_raw_output,
    merge_table_entries,
    parse_json_object,
    select_yolo_table_bbox,
)


def _make_image(path: Path) -> None:
    Image.new("RGB", (1000, 800), color="white").save(path)


def _mineru_layout_response(*, bbox: list[int]) -> Mock:
    response = Mock(status_code=200, text="")
    response.json.return_value = {
        "md_content": "| Node | Strings |\n| --- | --- |\n| CBX01 | 12 |",
        "content_list": [
            {"type": "text", "text": "Title", "bbox": [0, 0, 1000, 100]},
            {"type": "table", "bbox": bbox},
        ],
    }
    return response


def _structured_payload() -> dict[str, object]:
    return {
        "equipment_family": "CBX",
        "string_info": [
            {"node": "CBX-12", "string_number": 45},
            {"node": "CBX-13", "string_number": 56},
        ],
    }


def test_compress_image_for_bedrock_keeps_small_png() -> None:
    small = Image.new("RGB", (100, 100), color="white")
    buffer = BytesIO()
    small.save(buffer, format="PNG")
    data = buffer.getvalue()
    compressed, mime_type = _compress_image_for_bedrock(data)
    assert compressed == data
    assert mime_type == "image/png"


def test_compress_image_for_bedrock_shrinks_large_png() -> None:
    # Noise does not compress in PNG, so this stays comfortably above the
    # Bedrock transport limit without paying for a huge bitmap.
    large = Image.effect_noise((1800, 1400), 64).convert("RGB")
    buffer = BytesIO()
    large.save(buffer, format="PNG")
    data = buffer.getvalue()
    assert _bedrock_image_transport_size(data) > 5 * 1024 * 1024
    compressed, mime_type = _compress_image_for_bedrock(data)
    assert _bedrock_image_transport_size(compressed) <= 5 * 1024 * 1024
    assert mime_type == "image/jpeg"


def test_bedrock_transport_size_exceeds_limit_before_raw_limit() -> None:
    raw = b"\0" * 4_201_849
    assert len(raw) < 5 * 1024 * 1024
    assert _bedrock_image_transport_size(raw) == 5_602_468


def test_normalize_equipment_family_maps_table_labels() -> None:
    assert _normalize_equipment_family("LBD") == "LBD"
    assert _normalize_equipment_family("LOAD BREAK DISCONNECT") == "LBD"
    assert _normalize_equipment_family("CBX") == "CBX"


def test_expand_bbox_uniformly_uses_box_dimensions() -> None:
    box = BoundingBox(100.0, 50.0, 700.0, 350.0)
    expanded = expand_bbox_uniformly(
        box,
        expansion_ratio=0.01,
        max_width=1000,
        max_height=400,
    )
    assert expanded == BoundingBox(94.0, 47.0, 706.0, 353.0)


def test_expand_bbox_uniformly_clamps_to_image_edges() -> None:
    box = BoundingBox(1.0, 2.0, 999.0, 398.0)
    expanded = expand_bbox_uniformly(
        box,
        expansion_ratio=0.01,
        max_width=1000,
        max_height=400,
    )
    assert expanded == BoundingBox(0.0, 0.0, 1000.0, 400.0)


def test_build_result_maps_lbd_equipment_family_to_lynx(tmp_path: Path) -> None:
    result = build_result(
        {
            "equipment_family": "LBD",
            "string_info": [{"node": "LBD-01", "string_number": 12}],
        },
        strategy=STRATEGY_MINERU_CLAUDE,
        table_bbox=BoundingBox(0, 0, 1, 1),
        cropped_image_path=tmp_path / "table.png",
    )

    assert result.equipment_family == "LBD"
    assert result.project_type == "LYNX"


def test_score_equipment_table_html_accepts_lbd_schedule_headers() -> None:
    table_html = """
    <table>
    <tr><td>LOAD BREAK DISCONNECT</td><td>TMEIC POWER MODULE</td><td>INSTALL TYPE</td><td>MODULE POWER (W)</td><td>STRING/LOAD BREAK DISCONNECT</td></tr>
    <tr><td>PCU-11B04-LBD-03</td><td>2</td><td>MESSENGER</td><td>470</td><td>112</td></tr>
    </table>
    """
    assert _score_equipment_table_html(table_html) > 0.0


def test_score_equipment_table_html_accepts_combinerbox_headers() -> None:
    table_html = """
    <table>
    <tr><td>INVERTER</td><td>COMBINERBOX</td><td>INSTALLTYPE</td><td>MODULEPOWER (W)</td><td>STRING /COMBINER</td><td>STRING /INVERTER</td></tr>
    <tr><td>INV2206-PM-02</td><td>INV2206-CBX-01</td><td>MESSENGER</td><td>400</td><td>20</td><td>92</td></tr>
    </table>
    """
    assert _score_equipment_table_html(table_html) > 0.0


def test_score_equipment_table_html_accepts_from_strings_schedule() -> None:
    table_html = """
    <table><tr><td>FROM</td><td>TO</td><td>STRINGS</td></tr>
    <tr><td>INV31A106_LBD_01</td><td>INV31A106</td><td>12</td></tr></table>
    """
    assert _score_equipment_table_html(table_html) > 0.0


def test_score_equipment_table_html_rejects_legend_table() -> None:
    table_html = (
        "<table><tr><td></td><td>PROJECT BOUNDARY</td>"
        "<td></td><td>INVERTER SKID</td></tr></table>"
    )
    assert _score_equipment_table_html(table_html) <= 0.0


def test_compact_table_markdown_keeps_html_tables_only() -> None:
    markdown = (
        "KEY MAP:\n![](images/foo.jpg)\n"
        "<table><tr><td>CBX-12</td><td>45</td></tr></table>\n"
        "notes"
    )
    assert _compact_table_markdown(markdown) == (
        "<table><tr><td>CBX-12</td><td>45</td></tr></table>"
    )


def test_select_table_bbox_prefers_schedule_table_by_headers() -> None:
    key_plan = {
        "type": "table",
        "bbox": [30, 27, 650, 577],
    }
    legend = {
        "type": "table",
        "bbox": [31, 602, 302, 970],
    }
    schedule = {
        "type": "table",
        "bbox": [305, 704, 869, 967],
    }
    markdown = """
    <table><tr><td>INV31B104_LBD_19</td><td>INV31A106_LBD_01</td></tr></table>
    <table><tr><td></td><td>PROJECT BOUNDARY</td></tr></table>
    <table><tr><td>FROM</td><td>TO</td><td>STRINGS</td></tr>
    <tr><td>INV31A106_LBD_01</td><td>INV31A106</td><td>12</td></tr>
    <tr><td>INV31A106_LBD_02</td><td>INV31A106</td><td>16</td></tr></table>
    """
    bbox = _select_table_bbox([key_plan, legend, schedule], markdown)
    assert bbox == BoundingBox(305.0, 704.0, 869.0, 967.0)


def test_select_all_region_blocks_keeps_every_block_in_layout_order() -> None:
    """No YOLO detection: nothing is filtered out, order is preserved."""

    content_list = [
        {"type": "text", "bbox": [0, 0, 100, 50]},
        {"type": "table", "bbox": [100, 60, 400, 200]},
        {"type": "image", "bbox": [420, 210, 900, 700]},
        {"type": "text", "text": "no bbox"},
    ]

    regions = _select_all_region_blocks(content_list)

    assert [region.kind for region in regions] == ["text", "table", "image"]
    assert [region.block_index for region in regions] == [0, 1, 2]
    assert regions[0].bbox == BoundingBox(0.0, 0.0, 100.0, 50.0)
    assert regions[2].bbox == BoundingBox(420.0, 210.0, 900.0, 700.0)


def test_select_all_region_blocks_rejects_bboxless_layout() -> None:
    with pytest.raises(BlockDiagramTableError, match="no layout blocks"):
        _select_all_region_blocks([{"type": "text", "text": "no bbox"}])


def test_select_region_by_iou_picks_overlapping_block_of_any_type() -> None:
    """Page-27 shape: the schedule lives in an ``image`` block, not a table."""

    content_list = [
        {"type": "table", "bbox": [757, 182, 875, 520]},
        {"type": "table", "bbox": [907, 666, 978, 831]},
        {"type": "image", "bbox": [339, 761, 820, 914]},
        {"type": "image", "bbox": [32, 46, 344, 710]},
    ]
    # The rejected YOLO box, expressed in the layout's 0..1000 space.
    reference = BoundingBox(418.9, 754.3, 793.6, 924.6)

    regions = _select_region_by_iou(content_list, reference)

    assert len(regions) == 1
    assert regions[0].kind == "image"
    assert regions[0].block_index == 2
    assert regions[0].bbox == BoundingBox(339.0, 761.0, 820.0, 914.0)


def test_select_region_by_iou_ignores_blocks_without_a_bbox() -> None:
    content_list = [
        {"type": "image", "bbox": None},
        {"type": "text", "bbox": [10, 10, 90, 90]},
    ]

    regions = _select_region_by_iou(
        content_list,
        BoundingBox(0.0, 0.0, 100.0, 100.0),
    )

    assert regions[0].kind == "text"
    assert regions[0].bbox == BoundingBox(10.0, 10.0, 90.0, 90.0)


def test_select_best_table_markdown_ignores_legend_table() -> None:
    markdown = """
    <table><tr><td></td><td>PROJECT BOUNDARY</td><td></td><td>INVERTER SKID</td></tr></table>
    <table><tr><td>FROM</td><td>TO</td><td>STRINGS</td></tr>
    <tr><td>INV12B108-LBD-01</td><td>INV12B108</td><td>20</td></tr></table>
    """
    best = _select_best_table_markdown(markdown)
    assert "FROM" in best
    assert "INV12B108-LBD-01" in best
    assert _score_equipment_table_html(best) > _score_equipment_table_html(
        "<table><tr><td></td><td>PROJECT BOUNDARY</td></tr></table>"
    )


def test_html_table_to_plain_text_joins_cells() -> None:
    html = "<table><tr><td>A</td><td>B</td></tr></table>"
    assert _html_table_to_plain_text(html) == "A | B"


def test_select_yolo_table_bbox_normalizes_high_confidence_box() -> None:
    detections = [
        DetectionResult(
            label="table",
            confidence=0.95,
            box=BoundingBox(100.0, 200.0, 900.0, 700.0),
        )
    ]
    bbox, confidence = select_yolo_table_bbox(
        detections,
        1000,
        800,
        min_confidence=0.9,
    )
    assert confidence == 0.95
    assert bbox == BoundingBox(0.1, 0.25, 0.9, 0.875)


def test_select_yolo_table_bbox_returns_none_below_fallback_threshold() -> None:
    detections = [
        DetectionResult(
            label="table",
            confidence=0.75,
            box=BoundingBox(100.0, 200.0, 900.0, 700.0),
        )
    ]
    bbox, confidence = select_yolo_table_bbox(
        detections,
        1000,
        800,
        min_confidence=0.9,
    )
    assert bbox is None
    assert confidence == 0.75


def test_block_diagram_table_recognizer_runs_yolo_claude_with_yolo_bbox(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "block.png"
    _make_image(image_path)
    bedrock = Mock()
    bedrock.complete_with_images.return_value = Mock(
        content=json.dumps(_structured_payload(), ensure_ascii=False)
    )
    schedule_detector = Mock()
    schedule_detector.detect.return_value = [
        DetectionResult(
            label="table",
            confidence=0.95,
            box=BoundingBox(100.0, 200.0, 900.0, 700.0),
        )
    ]
    recognizer = BlockDiagramTableRecognizer(
        config=replace(
            BlockDiagramTableConfig(),
            strategy=STRATEGY_YOLO_CLAUDE,
        ),
        bedrock=bedrock,
        temp_dir=tmp_path / "crops",
        block_schedule_detector=schedule_detector,
    )

    result = recognizer.recognize(image_path)

    schedule_detector.detect.assert_called_once()
    assert result.strategy == STRATEGY_YOLO_CLAUDE
    assert result.raw_output["table_bbox_source"] == "yolo"
    assert result.raw_output["yolo_confidence"] == 0.95
    assert result.raw_output["mineru_seconds"] == 0.0
    assert isinstance(result.raw_output["yolo_seconds"], float)
    assert isinstance(result.raw_output["claude_seconds"], float)
    assert isinstance(result.raw_output["total_seconds"], float)
    assert result.raw_output["yolo_bbox_expansion_ratio"] == pytest.approx(0.01)
    assert result.table_bbox.to_tuple() == pytest.approx(
        (0.092, 0.24375, 0.908, 0.88125)
    )
    assert result.raw_output["table_bboxes"][0] == pytest.approx(
        [0.092, 0.24375, 0.908, 0.88125]
    )
    # The persisted/visualized bbox is also the crop bbox.
    with Image.open(result.cropped_image_path) as cropped:
        assert cropped.size == (816, 510)
    bedrock.complete_with_images.assert_called_once()


def test_block_diagram_table_recognizer_runs_yolo_glm_with_yolo_bbox(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "block.png"
    _make_image(image_path)
    glm = Mock()
    glm.complete_with_images.return_value = Mock(
        content=json.dumps(_structured_payload(), ensure_ascii=False)
    )
    bedrock = Mock()
    schedule_detector = Mock()
    schedule_detector.detect.return_value = [
        DetectionResult(
            label="table",
            confidence=0.95,
            box=BoundingBox(100.0, 200.0, 900.0, 700.0),
        )
    ]
    recognizer = BlockDiagramTableRecognizer(
        config=replace(
            BlockDiagramTableConfig(),
            strategy=STRATEGY_YOLO_GLM,
        ),
        bedrock=bedrock,
        glm=glm,
        temp_dir=tmp_path / "crops",
        block_schedule_detector=schedule_detector,
    )

    result = recognizer.recognize(image_path)

    schedule_detector.detect.assert_called_once()
    assert result.strategy == STRATEGY_YOLO_GLM
    assert result.raw_output["table_bbox_source"] == "yolo"
    assert result.raw_output["yolo_confidence"] == 0.95
    assert result.raw_output["mineru_seconds"] == 0.0
    assert isinstance(result.raw_output["yolo_seconds"], float)
    assert isinstance(result.raw_output["glm_seconds"], float)
    assert "claude_seconds" not in result.raw_output
    assert isinstance(result.raw_output["total_seconds"], float)
    assert result.table_bbox.to_tuple() == pytest.approx(
        (0.092, 0.24375, 0.908, 0.88125)
    )
    glm.complete_with_images.assert_called_once()
    bedrock.complete_with_images.assert_not_called()


def test_block_diagram_table_recognizer_yolo_claude_falls_back_to_mineru(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "block.png"
    _make_image(image_path)
    bedrock = Mock()
    bedrock.complete_with_images.return_value = Mock(
        content=json.dumps(_structured_payload(), ensure_ascii=False)
    )
    schedule_detector = Mock()
    schedule_detector.detect.return_value = [
        DetectionResult(
            label="table",
            confidence=0.75,
            box=BoundingBox(50.0, 60.0, 950.0, 760.0),
        )
    ]
    recognizer = BlockDiagramTableRecognizer(
        config=replace(
            BlockDiagramTableConfig(),
            strategy=STRATEGY_YOLO_CLAUDE,
        ),
        bedrock=bedrock,
        temp_dir=tmp_path / "crops",
        block_schedule_detector=schedule_detector,
    )
    layout_response = _mineru_layout_response(bbox=[100, 200, 900, 700])

    with patch.object(
        recognizer._mineru,
        "parse_file",
        return_value=_parse_layout(layout_response),
    ) as mock_parse:
        result = recognizer.recognize(image_path)

    schedule_detector.detect.assert_called_once()
    mock_parse.assert_called_once()
    assert result.strategy == STRATEGY_YOLO_CLAUDE
    assert result.raw_output["table_bbox_source"] == "mineru"
    assert result.raw_output["yolo_confidence"] == 0.75
    assert result.raw_output["mineru_seconds"] >= 0.0
    assert result.raw_output["yolo_seconds"] >= 0.0
    assert result.raw_output["claude_seconds"] >= 0.0
    assert result.table_bbox == BoundingBox(100.0, 200.0, 900.0, 700.0)


def test_block_diagram_table_recognizer_without_yolo_detection_sends_every_block(
    tmp_path: Path,
) -> None:
    """No YOLO detection: every MinerU block is cropped and sent, unfiltered."""

    image_path = tmp_path / "block.png"
    _make_image(image_path)
    bedrock = Mock()
    bedrock.complete_with_images.return_value = Mock(
        content=json.dumps(_structured_payload(), ensure_ascii=False)
    )
    schedule_detector = Mock()
    schedule_detector.detect.return_value = []
    recognizer = BlockDiagramTableRecognizer(
        config=replace(
            BlockDiagramTableConfig(),
            strategy=STRATEGY_YOLO_CLAUDE,
        ),
        bedrock=bedrock,
        temp_dir=tmp_path / "crops",
        block_schedule_detector=schedule_detector,
    )
    layout_response = _mineru_layout_response(bbox=[100, 200, 900, 700])

    with patch.object(
        recognizer._mineru,
        "parse_file",
        return_value=_parse_layout(layout_response),
    ):
        result = recognizer.recognize(image_path)

    assert result.raw_output["table_bbox_source"] == "mineru"
    assert result.raw_output["yolo_confidence"] is None
    assert result.raw_output["region_selection"] == "all_mineru_blocks"
    # The text block and the table block both go to the model.
    assert result.raw_output["region_count"] == 2
    assert result.raw_output["table_region_kinds"] == ["text", "table"]
    assert result.raw_output["table_bboxes"] == [
        [0.0, 0.0, 1000.0, 100.0],
        [100.0, 200.0, 900.0, 700.0],
    ]
    sent = bedrock.complete_with_images.call_args.args[1]
    assert len(sent) == 2
    assert result.table_bbox == BoundingBox(0.0, 0.0, 1000.0, 100.0)


def test_block_diagram_table_recognizer_low_confidence_yolo_picks_overlapping_block(
    tmp_path: Path,
) -> None:
    """Below-threshold YOLO: the most overlapping layout block wins, any type.

    MinerU parsed fine here and only exposed a non-table block. The rejected
    YOLO box is the pointer, so that block is used even though it is ``text``.
    """

    image_path = tmp_path / "block.png"
    _make_image(image_path)
    bedrock = Mock()
    bedrock.complete_with_images.return_value = Mock(
        content=json.dumps(_structured_payload(), ensure_ascii=False)
    )
    schedule_detector = Mock()
    schedule_detector.detect.return_value = [
        DetectionResult(
            label="table",
            confidence=0.29,
            box=BoundingBox(100.0, 200.0, 900.0, 700.0),
        )
    ]
    recognizer = BlockDiagramTableRecognizer(
        config=replace(
            BlockDiagramTableConfig(),
            strategy=STRATEGY_YOLO_CLAUDE,
        ),
        bedrock=bedrock,
        temp_dir=tmp_path / "crops",
        block_schedule_detector=schedule_detector,
    )
    layout_response = Mock(status_code=200, text="")
    layout_response.json.return_value = {
        "md_content": "no table",
        "content_list": [{"type": "text", "text": "hello", "bbox": [0, 0, 10, 10]}],
    }

    with patch.object(
        recognizer._mineru,
        "parse_file",
        return_value=_parse_layout(layout_response),
    ) as mock_parse:
        result = recognizer.recognize(image_path)

    schedule_detector.detect.assert_called_once()
    mock_parse.assert_called_once()
    assert result.raw_output["table_bbox_source"] == "mineru"
    assert result.raw_output["yolo_confidence"] == 0.29
    assert result.raw_output["region_selection"] == "mineru_iou_with_yolo"
    assert result.raw_output["region_kind"] == "text"
    assert result.raw_output["table_region_kinds"] == ["text"]
    assert result.table_bbox == BoundingBox(0.0, 0.0, 10.0, 10.0)


def test_block_diagram_table_extraction_failure_keeps_located_region(
    tmp_path: Path,
) -> None:
    """A 503 during extraction must not discard the located table region."""

    image_path = tmp_path / "block.png"
    _make_image(image_path)
    bedrock = Mock()
    bedrock.complete_with_images.side_effect = LlmServiceError(
        "Bedrock server request returned HTTP 503: model overloaded"
    )
    schedule_detector = Mock()
    schedule_detector.detect.return_value = [
        DetectionResult(
            label="table",
            confidence=0.95,
            box=BoundingBox(100.0, 200.0, 900.0, 700.0),
        )
    ]
    recognizer = BlockDiagramTableRecognizer(
        config=replace(
            BlockDiagramTableConfig(),
            strategy=STRATEGY_YOLO_CLAUDE,
        ),
        bedrock=bedrock,
        temp_dir=tmp_path / "crops",
        block_schedule_detector=schedule_detector,
    )

    with pytest.raises(BlockDiagramTableError) as excinfo:
        recognizer.recognize(image_path)

    assert "HTTP 503" in str(excinfo.value)
    location = excinfo.value.location
    assert location is not None
    assert location.bbox.to_tuple() == pytest.approx(
        (0.092, 0.24375, 0.908, 0.88125)
    )
    assert location.source == "yolo"


def test_block_diagram_table_empty_extraction_keeps_located_region(
    tmp_path: Path,
) -> None:
    """Content failures after a successful locate carry the region too."""

    image_path = tmp_path / "block.png"
    _make_image(image_path)
    bedrock = Mock()
    bedrock.complete_with_images.return_value = Mock(
        content=json.dumps({"equipment_family": "CBX", "string_info": []})
    )
    schedule_detector = Mock()
    schedule_detector.detect.return_value = [
        DetectionResult(
            label="table",
            confidence=0.95,
            box=BoundingBox(100.0, 200.0, 900.0, 700.0),
        )
    ]
    recognizer = BlockDiagramTableRecognizer(
        config=replace(
            BlockDiagramTableConfig(),
            strategy=STRATEGY_YOLO_CLAUDE,
        ),
        bedrock=bedrock,
        temp_dir=tmp_path / "crops",
        block_schedule_detector=schedule_detector,
    )

    with pytest.raises(
        BlockDiagramTableError,
        match="did not extract any table rows",
    ) as excinfo:
        recognizer.recognize(image_path)

    assert excinfo.value.location is not None
    assert excinfo.value.location.bbox.to_tuple() == pytest.approx(
        (0.092, 0.24375, 0.908, 0.88125)
    )


def test_block_diagram_table_locate_failure_never_calls_claude(
    tmp_path: Path,
) -> None:
    """No located table means no Claude request at all."""

    image_path = tmp_path / "block.png"
    _make_image(image_path)
    bedrock = Mock()
    schedule_detector = Mock()
    schedule_detector.detect.return_value = []
    recognizer = BlockDiagramTableRecognizer(
        config=BlockDiagramTableConfig(),
        bedrock=bedrock,
        block_schedule_detector=schedule_detector,
    )

    with patch.object(
        recognizer._mineru,
        "parse_file",
        side_effect=BlockDiagramTableError(
            "MinerU did not locate any table in the image."
        ),
    ):
        with pytest.raises(BlockDiagramTableError) as excinfo:
            recognizer.recognize(image_path)

    bedrock.complete_with_images.assert_not_called()
    assert excinfo.value.location is None


def test_block_diagram_table_recognizer_rejects_unknown_strategy() -> None:
    recognizer = BlockDiagramTableRecognizer(
        config=replace(
            BlockDiagramTableConfig(),
            strategy="unknown",
        ),
    )
    with pytest.raises(BlockDiagramTableError, match="Unknown block-diagram table strategy"):
        recognizer.recognize(b"png")


def test_block_diagram_table_recognizer_runs_mineru_claude_strategy(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "block.png"
    _make_image(image_path)
    bedrock = Mock()
    bedrock.complete_with_images.return_value = Mock(
        content=json.dumps(_structured_payload(), ensure_ascii=False)
    )
    recognizer = BlockDiagramTableRecognizer(
        config=replace(
            BlockDiagramTableConfig(),
            strategy=STRATEGY_MINERU_CLAUDE,
        ),
        bedrock=bedrock,
        temp_dir=tmp_path / "crops",
    )
    layout_response = _mineru_layout_response(bbox=[100, 200, 900, 700])

    with patch.object(
        recognizer._mineru,
        "parse_file",
        return_value=_parse_layout(layout_response),
    ) as mock_parse:
        result = recognizer.recognize(image_path)

    assert mock_parse.call_count == 1
    assert result.strategy == STRATEGY_MINERU_CLAUDE
    assert result.project_type == "IBEX"
    assert result.equipment_family == "CBX"
    assert result.cropped_image_path.name.endswith("_table_full.png")
    assert result.markdown == ""
    bedrock.complete_with_images.assert_called_once()


def test_block_diagram_table_recognizer_falls_back_to_markdown_table(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "block.png"
    _make_image(image_path)
    bedrock = Mock()
    bedrock.complete_with_images.return_value = Mock(
        content=json.dumps(_structured_payload(), ensure_ascii=False)
    )
    recognizer = BlockDiagramTableRecognizer(
        config=replace(
            BlockDiagramTableConfig(),
            strategy=STRATEGY_MINERU_CLAUDE,
        ),
        bedrock=bedrock,
        temp_dir=tmp_path / "crops",
    )
    layout_response = Mock(status_code=200, text="")
    layout_response.json.return_value = {
        "md_content": "<table><tr><td>CBX-12</td><td>45</td></tr></table>",
        "content_list": [],
    }

    with patch.object(
        recognizer._mineru,
        "parse_file",
        return_value=_parse_layout(layout_response),
    ):
        result = recognizer.recognize(image_path)

    assert result.project_type == "IBEX"
    assert result.equipment_family == "CBX"
    assert result.table_bbox == BoundingBox(0.0, 0.0, 1.0, 1.0)


def test_block_diagram_table_recognizer_requires_table_bbox() -> None:
    bedrock = Mock()
    recognizer = BlockDiagramTableRecognizer(
        config=replace(
            BlockDiagramTableConfig(),
            strategy=STRATEGY_MINERU_CLAUDE,
        ),
        bedrock=bedrock,
    )
    response = Mock(status_code=200, text="")
    response.json.return_value = {
        "md_content": "no table",
        "content_list": [{"type": "text", "text": "hello", "bbox": [0, 0, 10, 10]}],
    }

    with patch.object(
        recognizer._mineru,
        "parse_file",
        return_value=_parse_layout(response),
    ):
        with pytest.raises(BlockDiagramTableError, match="did not locate any table"):
            recognizer.recognize(b"png")


def test_block_diagram_table_defaults_to_yolo_claude() -> None:
    config = BlockDiagramTableConfig()
    assert config.strategy == STRATEGY_YOLO_CLAUDE
    assert config.yolo_fallback_confidence == 0.8
    assert config.yolo_bbox_expansion_ratio == pytest.approx(0.01)


def test_block_diagram_table_openrouter_model_falls_back_to_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # An ambient OPENROUTER_MODEL (a developer's .env is loaded into os.environ
    # by other tests) would win over the file below: load_env_file protects
    # keys already present in the environment.
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("BLOCK_DIAGRAM_TABLE_OPENROUTER_MODEL", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPENROUTER_MODEL=openrouter/default-model\n",
        encoding="utf-8",
    )
    config = BlockDiagramTableConfig.from_env(env_path)
    assert config.openrouter_model == "openrouter/default-model"


def test_block_diagram_table_openrouter_model_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("BLOCK_DIAGRAM_TABLE_OPENROUTER_MODEL", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OPENROUTER_MODEL=openrouter/default-model",
                "BLOCK_DIAGRAM_TABLE_OPENROUTER_MODEL=openrouter/table-model",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = BlockDiagramTableConfig.from_env(env_path)
    assert config.openrouter_model == "openrouter/table-model"


def test_yolo_glm_vision_llm_uses_block_diagram_openrouter_model(
    tmp_path: Path,
) -> None:
    recognizer = BlockDiagramTableRecognizer(
        config=replace(
            BlockDiagramTableConfig(),
            strategy=STRATEGY_YOLO_GLM,
            openrouter_model="openrouter/table-model",
        ),
        temp_dir=tmp_path,
    )
    with patch(
        "count_and_design.application.ai_statistics.element_recognition."
        "block_diagram_table.recognizer.OpenRouterLlmConfig.from_env",
        return_value=OpenRouterLlmConfig(
            api_key="test-key",
            model="openrouter/default-model",
        ),
    ):
        provider = recognizer._vision_llm(STRATEGY_YOLO_GLM)

    assert provider.config.model == "openrouter/table-model"


def test_yolo_table_location_error_reports_yolo_and_fallback_context(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "block.png"
    _make_image(image_path)
    schedule_detector = Mock()
    schedule_detector.detect.return_value = []
    recognizer = BlockDiagramTableRecognizer(
        config=BlockDiagramTableConfig(),
        block_schedule_detector=schedule_detector,
    )

    with patch.object(
        recognizer._mineru,
        "parse_file",
        side_effect=BlockDiagramTableError("MinerU service unavailable"),
    ):
        with pytest.raises(
            BlockDiagramTableError,
            match=r"^YOLO detected no table; the MinerU fallback failed",
        ):
            recognizer.locate_table_bbox_yolo_or_mineru(
                image_path,
                image_path.read_bytes(),
            )


def test_yolo_low_confidence_falls_back_to_yolo_when_mineru_unavailable(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "block.png"
    _make_image(image_path)
    schedule_detector = Mock()
    schedule_detector.detect.return_value = [
        DetectionResult(
            label="table",
            confidence=0.75,
            box=BoundingBox(50.0, 60.0, 950.0, 760.0),
        )
    ]
    recognizer = BlockDiagramTableRecognizer(
        config=BlockDiagramTableConfig(),
        block_schedule_detector=schedule_detector,
    )

    with patch.object(
        recognizer._mineru,
        "parse_file",
        side_effect=BlockDiagramTableError("MinerU service unavailable"),
    ):
        regions, meta = recognizer.locate_table_bbox_yolo_or_mineru(
            image_path,
            image_path.read_bytes(),
        )

    assert meta["table_bbox_source"] == "yolo"
    assert meta["mineru_failed"] is True
    assert meta["yolo_confidence"] == 0.75
    assert meta["yolo_bbox_expansion_ratio"] == pytest.approx(0.01)
    assert [region.bbox for region in regions] == [
        BoundingBox(0.041, 0.06625, 0.959, 0.95875)
    ]


def _parse_layout(response: Mock):
    from count_and_design.application.ai.ocr.providers.mineru import _parse_result

    return _parse_result(response.json.return_value)


def _row(node: str, string_number: int) -> dict[str, object]:
    return {"node": node, "string_number": string_number}


def _array_response(entries: list[dict[str, object]]) -> str:
    return "```json\n" + json.dumps(entries, ensure_ascii=False) + "\n```"


def test_parse_json_object_merges_top_level_array() -> None:
    entries = [
        {"equipment_family": "CBX", "string_info": [_row("INV11A101-CBX-01", 12)]},
        {"equipment_family": "CBX", "string_info": [_row("INV11A101-CBX-02", 18)]},
        {"equipment_family": "CBX", "string_info": [_row("INV11A101-CBX-03", 18)]},
    ]

    payload = parse_json_object(_array_response(entries))

    assert payload["equipment_family"] == "CBX"
    assert payload["string_info"] == [
        {"node": "INV11A101-CBX-01", "string_number": 12},
        {"node": "INV11A101-CBX-02", "string_number": 18},
        {"node": "INV11A101-CBX-03", "string_number": 18},
    ]


def test_parse_json_object_merges_unfenced_array() -> None:
    entries = [
        {"equipment_family": "LBD", "string_info": [_row("INV11A101-LBD-01", 6)]},
        {"equipment_family": "LBD", "string_info": [_row("INV11A101-LBD-02", 8)]},
    ]
    payload = parse_json_object(json.dumps(entries))

    assert payload["equipment_family"] == "LBD"
    assert len(payload["string_info"]) == 2


def test_parse_json_object_rejects_mixed_equipment_families() -> None:
    entries = [
        {"equipment_family": "CBX", "string_info": [_row("A-CBX-01", 12)]},
        {"equipment_family": "LBD", "string_info": [_row("B-LBD-01", 8)]},
    ]

    with pytest.raises(BlockDiagramTableError, match="mixed equipment families"):
        parse_json_object(_array_response(entries))


def test_parse_json_object_empty_array_is_an_error() -> None:
    with pytest.raises(BlockDiagramTableError, match="empty JSON array"):
        parse_json_object(json.dumps([]))


def test_parse_json_object_single_object_fenced_still_works() -> None:
    content = (
        "```json\n"
        + json.dumps({"equipment_family": "CBX", "string_info": [_row("INV1101-CBX-12", 45)]})
        + "\n```"
    )
    payload = parse_json_object(content)

    assert payload["equipment_family"] == "CBX"
    assert payload["string_info"][0]["node"] == "INV1101-CBX-12"


def _real_array_entries() -> list[dict[str, object]]:
    string_numbers = [12, 18, 18, 6, 18, 18, 18, 18, 18, 15, 18, 18, 18, 18, 18, 18, 18, 18, 18, 18]
    return [
        {
            "equipment_family": "CBX",
            "string_info": [_row(f"INV11A101-CBX-{index:02d}", count)],
        }
        for index, count in enumerate(string_numbers, start=1)
    ]


def test_parse_json_object_parses_real_array_response() -> None:
    payload = parse_json_object(_array_response(_real_array_entries()))

    assert payload["equipment_family"] == "CBX"
    assert len(payload["string_info"]) == 20
    assert payload["string_info"][0] == {
        "node": "INV11A101-CBX-01",
        "string_number": 12,
    }
    assert payload["string_info"][3]["string_number"] == 6
    assert payload["string_info"][-1] == {
        "node": "INV11A101-CBX-20",
        "string_number": 18,
    }


def test_parse_json_object_tolerates_prose_before_bare_array() -> None:
    content = "Here is the extracted table:\n" + json.dumps(_real_array_entries())
    payload = parse_json_object(content)

    assert payload["equipment_family"] == "CBX"
    assert len(payload["string_info"]) == 20


def test_parse_json_object_tolerates_text_after_bare_array() -> None:
    content = (
        json.dumps(_real_array_entries())
        + "\nLet me know if you need anything else."
    )
    payload = parse_json_object(content)

    assert payload["equipment_family"] == "CBX"
    assert len(payload["string_info"]) == 20


def test_parse_json_object_tolerates_text_after_bare_object() -> None:
    content = json.dumps(_structured_payload()) + "\nHope this helps!"
    payload = parse_json_object(content)

    assert payload["equipment_family"] == "CBX"
    assert payload["string_info"][0]["node"] == "CBX-12"


def test_parse_json_object_still_rejects_prose_without_json() -> None:
    with pytest.raises(BlockDiagramTableError, match="not valid JSON"):
        parse_json_object("Sorry, I cannot produce JSON. Here is the table instead.")


def test_parse_json_object_repairs_doubled_braces_from_prompt_escaping() -> None:
    content = (
        '```json\n'
        '{"equipment_family":"CBX","string_info":['
        '{{"node":"INV1105-CBX-01","string_number":21}},'
        '{{"node":"INV1105-CBX-02","string_number":18}}'
        ']}\n'
        '```'
    )
    payload = parse_json_object(content)

    assert payload["equipment_family"] == "CBX"
    assert payload["string_info"] == [
        {"node": "INV1105-CBX-01", "string_number": 21},
        {"node": "INV1105-CBX-02", "string_number": 18},
    ]


def test_parse_json_object_prefers_full_table_over_nested_row_fragment() -> None:
    content = (
        'Prefix text {"node":"INV1105-CBX-01","string_number":21} '
        '{"equipment_family":"CBX","string_info":'
        '[{"node":"INV1105-CBX-02","string_number":18}]}'
    )
    payload = parse_json_object(content)

    assert payload["equipment_family"] == "CBX"
    assert payload["string_info"][0]["node"] == "INV1105-CBX-02"


def test_vision_prompt_uses_single_brace_json_example() -> None:
    from count_and_design.application.ai_statistics.element_recognition.block_diagram_table.prompts import (
        BLOCK_DIAGRAM_TABLE_VISION_PROMPT,
    )

    assert "{{" not in BLOCK_DIAGRAM_TABLE_VISION_PROMPT
    assert '"equipment_family":"CBX"' in BLOCK_DIAGRAM_TABLE_VISION_PROMPT
    assert '"string_info":[' in BLOCK_DIAGRAM_TABLE_VISION_PROMPT


def test_merge_table_entries_requires_mapping_items() -> None:
    with pytest.raises(BlockDiagramTableError, match="must be an object"):
        merge_table_entries([1, 2])


def test_llm_raw_output_skips_empty_fields() -> None:
    from count_and_design.application.ai.llm import LlmCompletionResult

    output = llm_raw_output(LlmCompletionResult(content="text"))

    assert output == {"content": "text"}

    output = llm_raw_output(LlmCompletionResult(
        content="",
        usage={"prompt_tokens": 5},
        raw={"nested": {"ok": True}},
    ))

    assert output == {"usage": {"prompt_tokens": 5}, "raw": {"nested": {"ok": True}}}

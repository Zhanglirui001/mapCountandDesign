from __future__ import annotations

from concurrent.futures import Future
from io import BytesIO
import json
from pathlib import Path
from threading import Event, Thread
from unittest.mock import ANY, Mock, patch

from PIL import Image, ImageDraw
import pytest

from count_and_design.application.ai import (
    BoundingBox,
    DetectionResult,
    LlmCompletionResult,
    MinerUParseResult,
)
from count_and_design.application.ai_statistics import (
    AIStatisticsStage,
    AIStatisticsStageStatus,
    AIStatisticsWorkflow,
    Agent2Cancelled,
    Agent2OverallSiteError,
    Agent2OverallSiteRecognizer,
    DrawingPageType,
    LazyPageImages,
    OverallSiteExtractionConfig,
    InvOcrRow,
    InvStringCount,
    NumStrDetail,
    OverallSiteExtraction,
    PageClassification,
    PageClassificationResult,
    TrackerFieldValue,
    TrackerOcrFields,
    build_agent4_input,
    build_agent4_json,
    build_agent4_validation_input,
)


def _claude_extraction() -> dict:
    return {
        "extraction_scope": (
            "overall_site_tracker_fields_and_inv_summary"
        ),
        "detected_module_column_count": 1,
        "tracker_ocr_fields": [
            {
                "column_index": 1,
                "module_model": {
                    "label": "MODULE MODEL",
                    "meaning": "board brand/model",
                    "value": "ABC-620",
                    "source": "| MODULE MODEL | ABC-620 |",
                },
                "module_stc_dc_rating_w": {
                    "label": "MODULE STC DC RATING (W)",
                    "meaning": "module wattage",
                    "value": "620W",
                    "source": "| MODULE STC DC RATING (W) | 620 |",
                },
                "string_count": {
                    "label": "STRING COUNT",
                    "meaning": "total string count",
                    "value": "9767",
                    "source": "| STRING COUNT | 9767 |",
                },
                "num_str": {
                    "label": "Num-STR",
                    "meaning": "specific string types",
                    "value": "3-STR, 2-STR",
                    "source": "tracker rows",
                },
                "num_str_details": [
                    {
                        "num_str": "3-STR",
                        "count": "2779",
                        "source": "| 3 STRING TRACKER | 2779 |",
                    },
                    {
                        "num_str": "2-STR",
                        "count": "715",
                        "source": "| 2 STRING TRACKER | 715 |",
                    },
                ],
            }
        ],
        "inv_list": ["INV1101", "inv 1102", "INV 1101", "TOTAL"],
        "inv_rows": [
            {
                "identifier": "INV1101",
                "string_counts": [
                    {
                        "column_index": 1,
                        "label": "JINKO 590W MODULE STRINGS",
                        "count": "0",
                        "source": "INV1101 row",
                    },
                    {
                        "column_index": 2,
                        "label": "JINKO 595W MODULE STRINGS",
                        "count": "353",
                        "source": "INV1101 row",
                    },
                ],
            },
            {
                "identifier": "inv 1102",
                "string_counts": [
                    {
                        "column_index": 1,
                        "label": "JINKO 590W MODULE STRINGS",
                        "count": "116",
                        "source": "inv 1102 row",
                    },
                    {
                        "column_index": 2,
                        "label": "JINKO 595W MODULE STRINGS",
                        "count": "238",
                        "source": "inv 1102 row",
                    },
                ],
            },
        ],
        "inv_count": 99,
        "warnings": [],
    }


def _response(payload: dict, *, nested: bool = False) -> Mock:
    response = Mock(status_code=200)
    response.text = ""
    text = json.dumps(payload)
    response.json.return_value = (
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": text}],
                }
            ]
        }
        if nested
        else {"output_text": text}
    )
    return response


def _png(width: int = 1000, height: int = 700) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


def _layout(*boxes: list[int], markdown: str = "ignored OCR text") -> MinerUParseResult:
    return MinerUParseResult(
        markdown=markdown,
        content_list=[{"type": "table", "bbox": box} for box in boxes],
    )


def _completion(payload: dict) -> LlmCompletionResult:
    return LlmCompletionResult(content=json.dumps(payload))


def _mineru_config(**kwargs) -> OverallSiteExtractionConfig:
    return OverallSiteExtractionConfig(strategy="mineru_claude", **kwargs)


def test_agent2_config_reads_its_own_bedrock_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT2_BEDROCK_MAX_TOKENS", "9876")

    config = OverallSiteExtractionConfig.from_env("missing-agent2.env")

    assert config.bedrock_max_tokens == 9876


def test_agent2_config_uses_stricter_yolo_acceptance_defaults() -> None:
    config = OverallSiteExtractionConfig()

    assert config.yolo_detection_confidence == pytest.approx(0.5)
    assert config.yolo_fallback_confidence == pytest.approx(0.9)
    assert config.yolo_weak_inv_confidence == pytest.approx(0.5)
    assert config.yolo_bbox_expansion_ratio == pytest.approx(0.01)


def test_agent2_defaults_to_glm_vision() -> None:
    """Agent2 reads tables with GLM-5.3-Flash through OpenRouter by default."""

    config = OverallSiteExtractionConfig()

    assert config.strategy == "yolo_glm"
    assert config.uses_glm is True
    assert config.vision_provider == "openrouter"
    assert config.vision_provider_label == "GLM"
    assert config.openrouter_model == "z-ai/glm-5.3-flash"


def test_agent2_claude_strategies_still_report_bedrock() -> None:
    for strategy in ("yolo_claude", "mineru_claude"):
        config = OverallSiteExtractionConfig(strategy=strategy)
        assert config.uses_glm is False
        assert config.vision_provider == "bedrock"
        assert config.vision_provider_label == "Claude"


def test_agent2_config_prefers_its_own_openrouter_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_MODEL", "shared/model")
    monkeypatch.setenv("AGENT2_OVERALL_TABLE_OPENROUTER_MODEL", "agent2/model")

    config = OverallSiteExtractionConfig.from_env("missing-agent2.env")

    assert config.openrouter_model == "agent2/model"


def test_agent2_config_falls_back_to_shared_openrouter_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_MODEL", "z-ai/glm-5.3-flash:nitro")
    monkeypatch.delenv("AGENT2_OVERALL_TABLE_OPENROUTER_MODEL", raising=False)

    config = OverallSiteExtractionConfig.from_env("missing-agent2.env")

    assert config.openrouter_model == "z-ai/glm-5.3-flash:nitro"


def test_agent2_uses_mineru_coordinates_then_claude_vision() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout(
        [100, 100, 900, 300],
        [50, 400, 450, 650],
    )
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(_claude_extraction())
    recognizer = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=bedrock
    )

    result = recognizer.recognize_page(7, _png())

    mineru.parse_file.assert_called_once_with(
        ANY,
        filename="overall-page-7.png",
        parse_method="ocr",
        formula_enable=False,
        table_enable=True,
        image_analysis=False,
        return_content_list=True,
    )
    call = bedrock.complete_with_image.call_args
    assert "SYSTEMS SPECIFICATIONS" in call.args[0]
    assert "strong hint, not an absolute requirement" in call.args[0]
    assert "header may be INV, PCS, PCS ID" in call.args[0]
    assert "Do not return per-INV string-count columns" in call.args[0]
    assert "FIRST SOLAR SERIES 6+ 460W STRINGS" in call.args[0]
    assert isinstance(call.args[1], bytes)
    assert call.kwargs["system_prompt"].startswith("You extract structured")
    assert "temperature" not in call.kwargs
    assert result.raw["table_count"] == 2
    assert result.raw["table_bboxes"] == [
        [97, 68, 903, 212],
        [47, 278, 453, 457],
    ]

    assert result.page_number == 7
    assert result.inv_list == ("INV1101", "inv 1102")
    assert result.inv_count == 2
    assert result.inv_rows == (
        InvOcrRow(identifier="INV1101"),
        InvOcrRow(identifier="inv 1102"),
    )
    tracker = result.tracker_ocr_fields[0]
    assert tracker.module_model.value == "ABC-620"
    assert tracker.string_count.value == "9767"
    assert tracker.num_str_details[1].count == "715"


def test_agent2_mineru_glm_reads_tables_with_openrouter() -> None:
    """mineru_glm locates with MinerU and reads with GLM, never Bedrock."""

    mineru = Mock()
    mineru.parse_file.return_value = _layout(
        [100, 100, 900, 300],
        [50, 400, 450, 650],
    )
    glm = Mock()
    glm.complete_with_image.return_value = _completion(_claude_extraction())
    bedrock = Mock()
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(strategy="mineru_glm"),
        bedrock=bedrock,
        glm=glm,
    )

    result = recognizer.recognize_page(7, _png())

    glm.complete_with_image.assert_called()
    bedrock.complete_with_image.assert_not_called()
    bedrock.complete_with_images.assert_not_called()
    assert result.raw["vision_provider"] == "openrouter"
    assert result.inv_list == ("INV1101", "inv 1102")


def test_agent2_default_strategy_uses_glm_not_bedrock() -> None:
    """A recognizer built with the shipped default routes to GLM."""

    mineru = Mock()
    mineru.parse_file.return_value = _layout([100, 100, 900, 300])
    glm = Mock()
    glm.complete_with_image.return_value = _completion(_claude_extraction())
    bedrock = Mock()
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(strategy="mineru_glm"),
        bedrock=bedrock,
        glm=glm,
    )

    assert recognizer._vision_llm() is glm

    recognizer.config = OverallSiteExtractionConfig(strategy="mineru_claude")

    assert recognizer._vision_llm() is bedrock


@pytest.mark.parametrize("label", ["inv_tree", "tracker_table"])
def test_agent2_uses_single_confident_yolo_table_without_mineru(label: str) -> None:
    mineru = Mock()
    detector = Mock()
    detector.detect.return_value = [
        DetectionResult(
            label=label,
            confidence=0.95,
            class_id=0,
            box=BoundingBox(100, 100, 900, 300),
            source="onnxruntime:overall_schedule",
        )
    ]
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(_claude_extraction())
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(strategy="yolo_claude"),
        bedrock=bedrock,
        overall_schedule_detector=detector,
    )

    result = recognizer.recognize_page(7, _png())

    detector.detect.assert_called_once_with(ANY, confidence=0.5)
    mineru.parse_file.assert_not_called()
    assert result.raw["table_bbox_source"] == "yolo"
    assert result.raw["yolo_confidence"] == pytest.approx(0.95)
    assert result.raw["yolo_labels"] == [label]
    if label == "tracker_table":
        assert result.raw["table_count"] == 1
        assert result.raw["table_bboxes"] == [[89, 96, 911, 304]]
        assert result.raw["table_bbox_labels"] == ["tracker_table"]
    else:
        assert result.raw["table_count"] == 1
        assert result.raw["table_bboxes"] == [[89, 96, 911, 304]]
    assert result.raw["yolo_bbox_expansion_ratio"] == pytest.approx(0.01)


def test_agent2_falls_back_to_mineru_for_low_confidence_yolo() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout([100, 100, 900, 300])
    detector = Mock()
    detector.detect.return_value = [
        DetectionResult(
            label="inv_tree",
            confidence=0.89,
            class_id=0,
            box=BoundingBox(100, 100, 900, 300),
            source="onnxruntime:overall_schedule",
        )
    ]
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(_claude_extraction())
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(
            strategy="yolo_claude",
            yolo_fallback_confidence=0.9,
        ),
        bedrock=bedrock,
        overall_schedule_detector=detector,
    )

    result = recognizer.recognize_page(7, _png())

    mineru.parse_file.assert_called_once()
    assert result.raw["table_bbox_source"] == "mineru"
    assert result.raw["yolo_confidence"] == pytest.approx(0.89)
    assert result.raw["table_count"] == 1


def test_agent2_keeps_plausible_weak_inv_beside_confident_tracker() -> None:
    mineru = Mock()
    detector = Mock()
    detector.detect.return_value = [
        DetectionResult(
            label="tracker_table",
            confidence=0.96,
            class_id=0,
            box=BoundingBox(100, 100, 900, 300),
            source="onnxruntime:overall_schedule",
        ),
        DetectionResult(
            label="inv_tree",
            confidence=0.6,
            class_id=1,
            box=BoundingBox(50, 400, 450, 650),
            source="onnxruntime:overall_schedule",
        ),
    ]
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(_claude_extraction())
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(strategy="yolo_claude"),
        bedrock=bedrock,
        overall_schedule_detector=detector,
    )

    result = recognizer.recognize_page(7, _png())

    mineru.parse_file.assert_not_called()
    assert result.raw["table_bbox_source"] == "yolo"
    assert result.raw["yolo_labels"] == ["tracker_table", "inv_tree"]
    assert result.raw["weak_yolo_labels"] == ["inv_tree"]
    assert result.raw["table_count"] == 2


def test_agent2_expands_narrow_inv_tree_uniformly_by_configured_ratio() -> None:
    mineru = Mock()
    detector = Mock()
    detector.detect.return_value = [
        DetectionResult(
            label="inv_tree",
            confidence=0.94,
            class_id=1,
            box=BoundingBox(80, 200, 160, 550),
            source="onnxruntime:overall_schedule",
        )
    ]
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(_claude_extraction())
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(strategy="yolo_claude"),
        bedrock=bedrock,
        overall_schedule_detector=detector,
    )

    result = recognizer.recognize_page(7, _png())

    mineru.parse_file.assert_not_called()
    assert result.raw["table_bboxes"] == [[76, 194, 164, 556]]


def test_agent2_ignores_legacy_inv_row_string_counts() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout([0, 0, 1000, 1000])
    payload = _claude_extraction()
    payload["inv_list"] = ["PCU-11B01", "PCU-11B02"]
    payload["inv_rows"] = [
        {
            "identifier": "PCU-11B01",
            "string_counts": [
                {
                    "column_index": 1,
                    "label": "FIRST SOLAR SERIES 6 PLUS 470W STRINGS",
                    "count": "1750",
                    "source": "PCU-11B01 row",
                }
            ],
        },
    ]
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(payload)

    result = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=bedrock
    ).recognize_page(1, _png())

    assert result.inv_list == ("PCU-11B01", "PCU-11B02")
    assert result.inv_rows == (
        InvOcrRow(identifier="PCU-11B01"),
        InvOcrRow(identifier="PCU-11B02"),
    )
    assert build_agent4_validation_input((result,))["inv_string_counts"] == [
        {
            "identifier": "PCU-11B01",
            "string_counts": [],
            "total": {},
        },
        {
            "identifier": "PCU-11B02",
            "string_counts": [],
            "total": {},
        },
    ]


def test_agent2_rejects_implausible_weak_inv_without_companion_roi() -> None:
    mineru = Mock()
    detector = Mock()
    detector.detect.return_value = [
        DetectionResult(
            label="tracker_table",
            confidence=0.96,
            class_id=0,
            box=BoundingBox(100, 100, 900, 300),
            source="onnxruntime:overall_schedule",
        ),
        DetectionResult(
            label="inv_tree",
            confidence=0.4,
            class_id=1,
            box=BoundingBox(990, 690, 999, 699),
            source="onnxruntime:overall_schedule",
        ),
    ]
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(_claude_extraction())
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(strategy="yolo_claude"),
        bedrock=bedrock,
        overall_schedule_detector=detector,
    )

    result = recognizer.recognize_page(7, _png())

    mineru.parse_file.assert_not_called()
    assert result.raw["yolo_labels"] == ["tracker_table"]
    assert result.raw["weak_yolo_labels"] == []
    assert result.raw["table_count"] == 1
    assert result.raw["table_bbox_labels"] == ["tracker_table"]


def test_agent2_recovers_inv_from_wide_right_side_mineru_table() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout(
        [400, 70, 900, 420],
        [400, 450, 900, 850],
    )
    detector = Mock()
    detector.detect.return_value = [
        DetectionResult(
            label="tracker_table",
            confidence=0.96,
            class_id=0,
            box=BoundingBox(400, 50, 900, 300),
            source="onnxruntime:overall_schedule",
        )
    ]
    first = _claude_extraction()
    first["inv_list"] = []
    first["inv_count"] = 0
    recovered = _claude_extraction()
    recovered["tracker_ocr_fields"] = []
    recovered["detected_module_column_count"] = 0
    recovered["inv_list"] = ["INV1101", "INV11A02"]
    bedrock = Mock()
    bedrock.complete_with_image.side_effect = [
        _completion(first),
        _completion(recovered),
    ]
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(strategy="yolo_claude"),
        bedrock=bedrock,
        overall_schedule_detector=detector,
    )

    result = recognizer.recognize_page(17, _png())

    mineru.parse_file.assert_called_once()
    assert bedrock.complete_with_image.call_count == 2
    assert result.raw["table_bbox_source"] == "yolo+mineru"
    assert result.raw["inv_recovery"] == "recovered"
    assert result.inv_list == ("INV1101", "INV11A02")
    assert result.tracker_ocr_fields[0].module_model.value == "ABC-620"
    assert result.raw["table_count"] == 2


def test_agent2_downscales_large_pages_before_yolo() -> None:
    mineru = Mock()
    seen: dict[str, tuple[int, int] | None] = {"size": None}

    def detect(image, confidence=0.25):
        seen["size"] = getattr(image, "size", None)
        return [
            DetectionResult(
                label="tracker_table",
                confidence=0.95,
                class_id=0,
                box=BoundingBox(10, 10, 100, 40),
                source="onnxruntime:overall_schedule",
            )
        ]

    detector = Mock()
    detector.detect.side_effect = detect
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(_claude_extraction())
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(strategy="yolo_claude"),
        bedrock=bedrock,
        overall_schedule_detector=detector,
    )

    recognizer.recognize_page(7, _png(3000, 2100))

    mineru.parse_file.assert_not_called()
    assert seen["size"] is not None
    assert max(seen["size"]) <= 1280


def test_agent2_rejects_non_json_claude_output_after_retry() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout([0, 0, 1000, 1000])
    bedrock = Mock()
    bedrock.complete_with_image.return_value = LlmCompletionResult(content="no json")
    recognizer = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=bedrock
    )

    with pytest.raises(Agent2OverallSiteError, match="not valid JSON"):
        recognizer.recognize_page(1, _png())

    assert bedrock.complete_with_image.call_count == 2


def test_agent2_retries_invalid_json_and_logs_redacted_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout([0, 0, 1000, 1000])
    secret_response = (
        '{"inv_list":["CONFIDENTIAL-INV-1101"],'
        '"inv_rows":['
    )
    bedrock = Mock()
    bedrock.complete_with_image.side_effect = [
        LlmCompletionResult(
            content=secret_response,
            finish_reason="max_tokens",
            usage={
                "inputTokens": 100,
                "outputTokens": 12000,
                "totalTokens": 12100,
            },
        ),
        _completion(_claude_extraction()),
    ]
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(
            strategy="mineru_claude",
            bedrock_max_tokens=12000,
        ),
        bedrock=bedrock,
    )

    with caplog.at_level("WARNING"):
        result = recognizer.recognize_page(1, _png())

    assert result.inv_list == ("INV1101", "inv 1102")
    assert bedrock.complete_with_image.call_count == 2
    calls = bedrock.complete_with_image.call_args_list
    assert all(call.kwargs["max_completion_tokens"] == 12000 for call in calls)
    assert "CORRECTION" in calls[1].args[0]
    assert "stopReason='max_tokens'" in caplog.text
    assert "outputTokens" in caplog.text
    assert "CONFIDENTIAL-INV-1101" not in caplog.text
    assert "schema_keys" in caplog.text


def test_agent2_accepts_fenced_claude_json() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout([0, 0, 1, 1])
    bedrock = Mock()
    bedrock.complete_with_image.return_value = LlmCompletionResult(
        content=f"```json\n{json.dumps(_claude_extraction())}\n```"
    )
    recognizer = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=bedrock
    )

    result = recognizer.recognize_page(1, _png())

    assert result.inv_list == ("INV1101", "inv 1102")
    bedrock.complete_with_image.assert_called_once()


def test_agent2_tolerates_scalar_and_list_tracker_fields() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout([0, 0, 1000, 1000])
    payload = _claude_extraction()
    tracker = payload["tracker_ocr_fields"][0]
    tracker["module_model"] = "BYD PHK-36"
    tracker["module_stc_dc_rating_w"] = 330
    tracker["string_count"] = "5266"
    tracker["num_str"] = [
        {"num_str": "4 STRING HARNESS COUNT", "count": "1271"},
        {"num_str": "2 STRING HARNESS COUNT", "count": "91"},
    ]
    tracker["num_str_details"] = []
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(payload)

    result = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=bedrock
    ).recognize_page(
        1,
        _png(),
    )

    parsed = result.tracker_ocr_fields[0]
    assert parsed.module_model.value == "BYD PHK-36"
    assert parsed.module_stc_dc_rating_w.value == "330"
    assert parsed.string_count.value == "5266"
    assert parsed.num_str.value == (
        "4 STRING HARNESS COUNT, 2 STRING HARNESS COUNT"
    )
    assert parsed.num_str_details == (
        NumStrDetail("4 STRING HARNESS COUNT", "1271"),
        NumStrDetail("2 STRING HARNESS COUNT", "91"),
    )
    tracker_payload = build_agent4_input((result,))
    assert [row["tracker_type"] for row in tracker_payload["tracker_totals"]] == [
        "4-string",
        "2-string",
    ]


def test_agent2_retries_when_claude_drops_a_physical_module_column() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout([0, 0, 1000, 1000])
    incomplete = _claude_extraction()
    incomplete["detected_module_column_count"] = 2
    complete = json.loads(json.dumps(incomplete))
    second = json.loads(json.dumps(complete["tracker_ocr_fields"][0]))
    second["column_index"] = 2
    second["module_stc_dc_rating_w"]["value"] = "540W"
    second["string_count"]["value"] = "2090"
    second["num_str_details"] = [
        {"num_str": "3-STR", "count": "530", "source": None},
        {"num_str": "2-STR", "count": "250", "source": None},
    ]
    complete["tracker_ocr_fields"].append(second)
    bedrock = Mock()
    bedrock.complete_with_image.side_effect = [
        _completion(incomplete),
        _completion(complete),
    ]

    result = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=bedrock
    ).recognize_page(
        1,
        _png(),
    )

    assert len(result.tracker_ocr_fields) == 2
    assert result.tracker_ocr_fields[1].module_stc_dc_rating_w.value == "540W"
    assert bedrock.complete_with_image.call_count == 2
    retry_prompt = bedrock.complete_with_image.call_args_list[1].args[0]
    assert "Detected 2 physical columns but returned 1" in retry_prompt
    assert "Do not merge columns with repeated MODULE MODEL values" in retry_prompt


def test_agent2_accepts_numeric_and_alphanumeric_inv_identifiers() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout([0, 0, 1000, 1000])
    payload = _claude_extraction()
    payload["inv_list"] = [
        "11A01",
        "INV11A02",
        "inv 11b01",
        "INV-12A03",
        "INV 1101",
        "INV11A01",
        "TOTAL",
    ]
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(payload)

    result = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=bedrock
    ).recognize_page(
        1,
        _png(),
    )

    assert result.inv_list == (
        "11A01",
        "INV11A02",
        "inv 11b01",
        "INV-12A03",
        "INV 1101",
    )
    assert result.inv_count == 5


def test_agent2_accepts_first_pcs_column_as_inv_tree_identifiers() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout([0, 0, 1000, 1000])
    incorrect = _claude_extraction()
    incorrect["inv_list"] = ["PCS-0001", "PCS-0002"]
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(incorrect)

    result = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=bedrock
    ).recognize_page(
        1,
        _png(),
    )

    assert result.inv_list == ("PCS-0001", "PCS-0002")
    assert result.inv_count == 2
    assert result.inv_rows == (
        InvOcrRow(identifier="PCS-0001"),
        InvOcrRow(identifier="PCS-0002"),
    )
    assert result.tracker_ocr_fields[0].module_model.value == "ABC-620"
    assert bedrock.complete_with_image.call_count == 1


def test_agent2_preserves_distinct_pcs_identifiers() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout([0, 0, 1000, 1000])
    incorrect = _claude_extraction()
    incorrect["inv_list"] = ["01ED-PCS-0001", "01ED-PCS-0002"]
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(incorrect)

    result = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=bedrock
    ).recognize_page(
        1,
        _png(),
    )

    assert result.inv_list == ("01ED-PCS-0001", "01ED-PCS-0002")
    assert result.inv_count == 2
    assert not any("Invalid PCS values were removed" in item for item in result.warnings)


def test_agent2_does_not_use_mineru_text_when_claude_omits_inv() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout(
        [0, 0, 1000, 1000],
        markdown="AREA 1 INV11A01 AREA 2 INV 11B02 INV11A01",
    )
    payload = _claude_extraction()
    payload["inv_list"] = []
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(payload)

    result = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=bedrock
    ).recognize_page(
        1,
        _png(),
    )

    assert result.inv_list == ()
    mineru.parse_file.assert_called_once()


def test_agent2_fails_when_mineru_returns_no_table_coordinates() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = MinerUParseResult(markdown="table text")
    recognizer = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=Mock()
    )

    # Stub the retry wait; these attempts must not cost real seconds.
    with patch(
        "count_and_design.application.ai_statistics.info_recognition."
        "overall_site._sleep_with_cancellation"
    ), pytest.raises(Agent2OverallSiteError, match="no table coordinates"):
        recognizer.recognize_page(1, _png())

    assert mineru.parse_file.call_count == 3


def _field(label: str, value: str | None) -> TrackerFieldValue:
    return TrackerFieldValue(label=label, meaning="", value=value)


def _tracker(model: str | None, count: str | None) -> TrackerOcrFields:
    return TrackerOcrFields(
        module_model=_field("MODULE MODEL", model),
        module_stc_dc_rating_w=_field("POWER", "660W" if model else None),
        string_count=_field("STRING COUNT", count),
        num_str=_field("Num-STR", "2-STR" if count else None),
        num_str_details=(
            (NumStrDetail("2-STR", "10"),) if count else ()
        ),
    )


def _overall_layout_png(*, colored: bool) -> bytes:
    image = Image.new("RGB", (640, 420), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((18, 18, 620, 400), outline="black", width=3)
    draw.rectangle(
        (35, 40, 235, 145),
        outline="black",
        fill="#d9f2ee" if colored else "#eeeeee",
        width=2,
    )
    for offset in range(0, 180, 30):
        draw.line((45 + offset, 45, 45 + offset, 140), fill="black", width=1)
    draw.rectangle(
        (285, 85, 555, 310),
        outline="black",
        fill="#b9e8de" if colored else "#dddddd",
        width=3,
    )
    draw.line((285, 195, 555, 195), fill="black", width=3)
    draw.line((420, 85, 420, 310), fill="black", width=3)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_recognize_pages_batches_multiple_page_images_into_one_claude_call() -> None:
    mineru = Mock()
    detector = Mock()
    detector.detect.return_value = [
        DetectionResult(
            label="tracker_table",
            confidence=0.96,
            class_id=0,
            box=BoundingBox(80, 40, 560, 210),
            source="onnxruntime:overall_schedule",
        ),
        DetectionResult(
            label="inv_tree",
            confidence=0.88,
            class_id=1,
            box=BoundingBox(80, 230, 560, 390),
            source="onnxruntime:overall_schedule",
        ),
    ]
    page_17 = _claude_extraction()
    page_17["page_number"] = 17
    page_17["inv_list"] = ["INV1701"]
    page_18 = _claude_extraction()
    page_18["page_number"] = 18
    page_18["inv_list"] = ["INV1801"]
    bedrock = Mock()
    bedrock.complete_with_images.return_value = _completion(
        {"pages": [page_17, page_18]}
    )
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(strategy="yolo_claude"),
        bedrock=bedrock,
        overall_schedule_detector=detector,
    )
    progress: list[tuple[int, int]] = []

    results = recognizer.recognize_pages(
        {
            17: _overall_layout_png(colored=False),
            18: _overall_layout_png(colored=True),
        },
        on_progress=lambda completed, total: progress.append(
            (completed, total)
        ),
    )

    bedrock.complete_with_images.assert_called_once()
    bedrock.complete_with_image.assert_not_called()
    call = bedrock.complete_with_images.call_args
    assert len(call.args[1]) == 2
    assert "Image 1: page 17" in call.args[0]
    assert "Image 2: page 18" in call.args[0]
    assert [result.page_number for result in results] == [17, 18]
    assert [result.inv_list for result in results] == [
        ("INV1701",),
        ("INV1801",),
    ]
    assert all(result.raw["claude_batch_size"] == 2 for result in results)
    assert progress == [(1, 4), (2, 4), (3, 4), (4, 4)]
    mineru.parse_file.assert_not_called()


def test_agent2_retries_invalid_batch_json_once() -> None:
    mineru = Mock()
    detector = Mock()
    detector.detect.return_value = [
        DetectionResult(
            label="tracker_table",
            confidence=0.96,
            class_id=0,
            box=BoundingBox(80, 40, 560, 210),
            source="onnxruntime:overall_schedule",
        )
    ]
    page_17 = _claude_extraction()
    page_17["page_number"] = 17
    page_17["inv_list"] = ["INV1701"]
    page_18 = _claude_extraction()
    page_18["page_number"] = 18
    page_18["inv_list"] = ["INV1801"]
    bedrock = Mock()
    bedrock.complete_with_images.side_effect = [
        LlmCompletionResult(
            content='{"pages":[',
            finish_reason="max_tokens",
        ),
        _completion({"pages": [page_17, page_18]}),
    ]
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(
            strategy="yolo_claude",
            bedrock_max_tokens=10000,
        ),
        bedrock=bedrock,
        overall_schedule_detector=detector,
    )

    results = recognizer.recognize_pages(
        {
            17: _overall_layout_png(colored=False),
            18: _overall_layout_png(colored=True),
        }
    )

    assert [result.page_number for result in results] == [17, 18]
    assert bedrock.complete_with_images.call_count == 2
    calls = bedrock.complete_with_images.call_args_list
    assert all(call.kwargs["max_completion_tokens"] == 10000 for call in calls)
    assert "CORRECTION" in calls[1].args[0]


def test_recognize_pages_runs_mineru_before_claude_batch() -> None:
    mineru = Mock()
    mineru.parse_file.return_value = _layout([100, 100, 900, 900])
    page_17 = _claude_extraction()
    page_17["page_number"] = 17
    page_17["inv_list"] = ["INV1701"]
    page_18 = _claude_extraction()
    page_18["page_number"] = 18
    page_18["inv_list"] = ["INV1801"]
    bedrock = Mock()
    order: list[str] = []

    def _mineru_parse(*_args, **_kwargs):
        order.append("mineru")
        return _layout([100, 100, 900, 900])

    def _claude_batch(*_args, **_kwargs):
        order.append("claude")
        return _completion({"pages": [page_17, page_18]})

    mineru.parse_file.side_effect = _mineru_parse
    bedrock.complete_with_images.side_effect = _claude_batch
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(strategy="mineru_claude"),
        bedrock=bedrock,
    )

    results = recognizer.recognize_pages(
        {
            17: _overall_layout_png(colored=False),
            18: _overall_layout_png(colored=True),
        }
    )

    assert mineru.parse_file.call_count == 2
    bedrock.complete_with_images.assert_called_once()
    bedrock.complete_with_image.assert_not_called()
    assert order == ["mineru", "mineru", "claude"]
    assert [result.page_number for result in results] == [17, 18]


def test_recognize_pages_consolidates_duplicate_visual_versions() -> None:
    page_12 = OverallSiteExtraction(
        page_number=12,
        tracker_ocr_fields=(_tracker(None, None),),
        inv_list=("INV 1101", "INV 1102", "INV 1103"),
        inv_count=3,
    )
    page_13 = OverallSiteExtraction(
        page_number=13,
        tracker_ocr_fields=(_tracker("TRINA", "100"),),
        inv_list=("INV1101", "INV1102", "INV1103", "INV1104"),
        inv_count=4,
    )
    recognizer = Agent2OverallSiteRecognizer(Mock(), bedrock=Mock())

    with patch.object(recognizer, "_prepare_all_overall_pages", return_value=[]):
        with patch.object(
            recognizer,
            "_extract_prepared_pages",
            return_value=[page_12, page_13],
        ):
            results = recognizer.recognize_pages({12: b"bw", 13: b"color"})

    assert len(results) == 1
    assert results[0].page_number == 13
    assert results[0].tracker_ocr_fields == page_13.tracker_ocr_fields
    assert results[0].inv_list == (
        "INV1101",
        "INV1102",
        "INV1103",
        "INV1104",
    )
    assert results[0].raw["consolidated_page_numbers"] == (12, 13)
    assert len(build_agent4_input(results)["module_columns"]) == 1


def test_recognize_pages_consolidates_duplicate_layouts_when_inv_is_empty() -> None:
    tracker = _tracker("JINKO", "13795")
    page_16 = OverallSiteExtraction(
        page_number=16,
        tracker_ocr_fields=(tracker,),
    )
    page_17 = OverallSiteExtraction(
        page_number=17,
        tracker_ocr_fields=(tracker,),
    )
    recognizer = Agent2OverallSiteRecognizer(Mock(), bedrock=Mock())

    layout_png = _overall_layout_png(colored=False)
    shared_fingerprint = (True, False) * 4032
    with patch(
        (
            "count_and_design.application.ai_statistics.info_recognition"
            ".overall_site._layout_fingerprint"
        ),
        return_value=shared_fingerprint,
    ):
        with patch.object(recognizer, "_prepare_all_overall_pages", return_value=[]):
            with patch.object(
                recognizer,
                "_extract_prepared_pages",
                return_value=[page_16, page_17],
            ):
                results = recognizer.recognize_pages(
                    {16: layout_png, 17: layout_png}
                )

    assert len(results) == 1
    assert results[0].raw["consolidated_page_numbers"] == (16, 17)
    assert len(build_agent4_input(results)["module_columns"]) == 1


def test_recognize_pages_keeps_distinct_overall_scopes_separate() -> None:
    page_12 = OverallSiteExtraction(
        page_number=12,
        inv_list=("INV1101", "INV1102"),
        inv_count=2,
    )
    page_13 = OverallSiteExtraction(
        page_number=13,
        inv_list=("INV1401", "INV1402"),
        inv_count=2,
    )
    recognizer = Agent2OverallSiteRecognizer(Mock(), bedrock=Mock())

    with patch.object(recognizer, "_prepare_all_overall_pages", return_value=[]):
        with patch.object(
            recognizer,
            "_extract_prepared_pages",
            return_value=[page_12, page_13],
        ):
            results = recognizer.recognize_pages({12: b"first", 13: b"second"})

    assert results == (page_12, page_13)


def test_build_agent4_input_preserves_duplicate_module_columns() -> None:
    extraction = OverallSiteExtraction(
        page_number=1,
        tracker_ocr_fields=(
            TrackerOcrFields(
                module_model=_field("MODULE MODEL", "First Solar Series 6+"),
                module_stc_dc_rating_w=_field("POWER", "460W"),
                string_count=_field("STRING COUNT", "21,344"),
                num_str=_field("Num-STR", "18-STR, 14-STR, 10-STR"),
                num_str_details=(
                    NumStrDetail("18-STR", "679"),
                    NumStrDetail("14-STR", "498"),
                    NumStrDetail("10-STR", "215"),
                ),
            ),
            TrackerOcrFields(
                module_model=_field("MODULE MODEL", "First Solar Series 6+"),
                module_stc_dc_rating_w=_field("POWER", "460W"),
                string_count=_field("STRING COUNT", "11616"),
                num_str=_field("Num-STR", "18-STR, 14-STR, 10-STR"),
                num_str_details=(
                    NumStrDetail("18-STR", "401"),
                    NumStrDetail("14-STR", "137"),
                    NumStrDetail("10-STR", "248"),
                ),
            ),
            TrackerOcrFields(
                module_model=_field("MODULE MODEL", "TRINA"),
                module_stc_dc_rating_w=_field("POWER", "395W"),
                string_count=_field("STRING COUNT", "533"),
                num_str=_field("Num-STR", "3-STR, 2-STR"),
                num_str_details=(
                    NumStrDetail("3-STR", "145"),
                    NumStrDetail("2-STR", "49"),
                ),
            ),
        ),
    )

    payload = build_agent4_input((extraction,))

    assert payload == {
        "module_columns": [
            {
                "column_id": "module_1",
                "module_stc_dc_rating_w": "460W",
                "module_model": "First Solar Series 6+",
                "string_count": 21344,
                "tracker_count": 1392,
            },
            {
                "column_id": "module_2",
                "module_stc_dc_rating_w": "460W",
                "module_model": "First Solar Series 6+",
                "string_count": 11616,
                "tracker_count": 786,
            },
            {
                "column_id": "module_3",
                "module_stc_dc_rating_w": "395W",
                "module_model": "TRINA",
                "string_count": 533,
                "tracker_count": 194,
            },
        ],
        "string_count_total": 33493,
        "tracker_totals": [
            {
                "tracker_type": "18-string",
                "module_counts": {
                    "module_1": 679,
                    "module_2": 401,
                    "module_3": {},
                },
                "total": 1080,
            },
            {
                "tracker_type": "14-string",
                "module_counts": {
                    "module_1": 498,
                    "module_2": 137,
                    "module_3": {},
                },
                "total": 635,
            },
            {
                "tracker_type": "10-string",
                "module_counts": {
                    "module_1": 215,
                    "module_2": 248,
                    "module_3": {},
                },
                "total": 463,
            },
            {
                "tracker_type": "3-string",
                "module_counts": {
                    "module_1": {},
                    "module_2": {},
                    "module_3": 145,
                },
                "total": 145,
            },
            {
                "tracker_type": "2-string",
                "module_counts": {
                    "module_1": {},
                    "module_2": {},
                    "module_3": 49,
                },
                "total": 49,
            },
        ],
        "grand_total": 2372,
    }
    assert json.loads(build_agent4_json((extraction,))) == payload

    assert build_agent4_validation_input((extraction,)) == {
        "tracker_totals": [
            {
                "tracker_type": "18-string",
                "power_counts": {"460W": 1080},
                "total": 1080,
            },
            {
                "tracker_type": "14-string",
                "power_counts": {"460W": 635},
                "total": 635,
            },
            {
                "tracker_type": "10-string",
                "power_counts": {"460W": 463},
                "total": 463,
            },
            {
                "tracker_type": "3-string",
                "power_counts": {"395W": 145},
                "total": 145,
            },
            {
                "tracker_type": "2-string",
                "power_counts": {"395W": 49},
                "total": 49,
            },
        ],
        "grand_total": 2372,
        "inv_string_counts": [],
    }


def test_build_agent4_input_uses_empty_objects_for_missing_values() -> None:
    extraction = OverallSiteExtraction(
        page_number=1,
        tracker_ocr_fields=(
            TrackerOcrFields(
                module_model=_field("MODULE MODEL", None),
                module_stc_dc_rating_w=_field("POWER", None),
                string_count=_field("STRING COUNT", None),
                num_str=_field("Num-STR", None),
            ),
        ),
    )

    assert build_agent4_input((extraction,)) == {
        "module_columns": [
            {
                "column_id": "module_1",
                "module_stc_dc_rating_w": {},
                "module_model": {},
                "string_count": {},
                "tracker_count": {},
            }
        ],
        "string_count_total": {},
        "tracker_totals": [],
        "grand_total": {},
    }


def test_build_agent4_input_normalizes_varied_string_type_rows() -> None:
    extraction = OverallSiteExtraction(
        page_number=1,
        tracker_ocr_fields=(
            TrackerOcrFields(
                module_model=_field("MODULE MODEL", "BYD PHK-36"),
                module_stc_dc_rating_w=_field("POWER", "330W"),
                string_count=_field("STRING COUNT", "5266"),
                num_str=_field(
                    "Num-STR",
                    "4 STRING TABLE, 2 STRING HARNESS COUNT",
                ),
                num_str_details=(
                    NumStrDetail("4 STRING TABLE", "1271"),
                    NumStrDetail("2 STRING HARNESS COUNT", "91"),
                ),
            ),
        ),
    )

    payload = build_agent4_input((extraction,))

    assert payload["tracker_totals"] == [
        {
            "tracker_type": "4-string",
            "module_counts": {"module_1": 1271},
            "total": 1271,
        },
        {
            "tracker_type": "2-string",
            "module_counts": {"module_1": 91},
            "total": 91,
        },
    ]
    assert payload["grand_total"] == 1362


def test_agent2_retries_mineru_and_reports_two_stage_progress() -> None:
    mineru = Mock()
    mineru.parse_file.side_effect = [
        ConnectionResetError(10054, "reset"),
        _layout([0, 0, 1000, 1000]),
    ]
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(_claude_extraction())
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=_mineru_config(mineru_max_attempts=3),
        bedrock=bedrock,
    )
    progress = []

    # Stub the retry wait; these attempts must not cost real seconds.
    with patch(
        "count_and_design.application.ai_statistics.info_recognition."
        "overall_site._sleep_with_cancellation"
    ):
        recognizer.recognize_page(
            1,
            _png(),
            on_progress=lambda completed, total: progress.append(
                (completed, total)
            ),
        )

    assert mineru.parse_file.call_count == 2
    assert progress == [(1, 2), (2, 2)]


def test_agent2_cancels_after_mineru_before_bedrock() -> None:
    cancelled = False
    mineru = Mock()

    def finish_mineru(*_args, **_kwargs):
        nonlocal cancelled
        cancelled = True
        return _layout([0, 0, 1000, 1000])

    mineru.parse_file.side_effect = finish_mineru
    bedrock = Mock()
    recognizer = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=bedrock
    )

    with pytest.raises(Agent2Cancelled):
        recognizer.recognize_page(
            1,
            _png(),
            is_cancelled=lambda: cancelled,
        )

    bedrock.complete_with_image.assert_not_called()


def test_agent2_cancel_does_not_wait_for_blocking_mineru_call() -> None:
    started = Event()
    release = Event()
    cancelled = Event()
    mineru = Mock()

    def blocking_mineru(*_args, **_kwargs):
        started.set()
        release.wait(1.0)
        return _layout([0, 0, 1000, 1000])

    def cancel_after_start() -> None:
        assert started.wait(1.0)
        cancelled.set()

    mineru.parse_file.side_effect = blocking_mineru
    recognizer = Agent2OverallSiteRecognizer(
        mineru, config=_mineru_config(), bedrock=Mock()
    )
    canceller = Thread(target=cancel_after_start)
    canceller.start()

    try:
        with pytest.raises(Agent2Cancelled):
            recognizer.recognize_page(
                1,
                _png(),
                is_cancelled=cancelled.is_set,
            )
    finally:
        release.set()
        canceller.join()


def test_agent2_cancels_during_mineru_retry_wait() -> None:
    cancelled = False
    mineru = Mock()
    mineru.parse_file.side_effect = ConnectionResetError(10054, "reset")
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=_mineru_config(mineru_max_attempts=3),
        bedrock=Mock(),
    )

    def request_cancel(*_args) -> None:
        nonlocal cancelled
        cancelled = True

    with patch(
        "count_and_design.application.ai_statistics.info_recognition."
        "overall_site._sleep_with_cancellation",
        side_effect=request_cancel,
    ), pytest.raises(Agent2Cancelled):
        recognizer.recognize_page(
            1,
            _png(),
            is_cancelled=lambda: cancelled,
        )

    assert mineru.parse_file.call_count == 1


def test_workflow_runs_agent2_only_on_reviewed_overall_pages(tmp_path) -> None:
    recognizer = Mock()
    recognizer.recognize_pages.return_value = (
        # A compact valid object is enough at the workflow boundary.
        Mock(page_number=2),
    )
    workflow = AIStatisticsWorkflow(overall_site_recognizer=recognizer)
    workflow.load_pdf(tmp_path / "drawing.pdf", 2)
    workflow.begin_page_classification()
    workflow.complete_page_classification(
        PageClassificationResult(
            pages=(
                PageClassification(
                    page_number=1,
                    page_type=DrawingPageType.ARRAY_LAYOUT.value,
                ),
                PageClassification(
                    page_number=2,
                    page_type=DrawingPageType.OVERALL_LAYOUT.value,
                ),
            ),
            page_images={1: b"array", 2: b"overall"},
        )
    )
    workflow.confirm_current_stage()

    results = workflow.run_overall_site_recognition()

    recognizer.recognize_pages.assert_called_once_with({2: b"overall"})
    assert results[0].page_number == 2
    assert workflow.snapshot.overall_site_extractions == results
    assert workflow.status_for(AIStatisticsStage.INFO_RECOGNITION) == (
        AIStatisticsStageStatus.NEEDS_CONFIRM
    )
    assert workflow.is_confirm_enabled(AIStatisticsStage.INFO_RECOGNITION)
    assert workflow.is_overall_site_run_enabled()

    workflow.begin_overall_site_recognition()

    assert workflow.status_for(AIStatisticsStage.INFO_RECOGNITION) == (
        AIStatisticsStageStatus.RUNNING
    )


class _ImmediateProcessPool:
    created: list[dict] = []

    def __init__(self, **kwargs):
        type(self).created.append(kwargs)

    def submit(self, _fn, page_index, _width, _height):
        future: Future = Future()
        future.set_result((page_index, f"rendered-{page_index + 1}".encode()))
        return future

    def shutdown(self, wait=True, cancel_futures=True):
        return None


def test_workflow_renders_uncached_overall_pages_outside_the_gui_process(
    tmp_path,
    monkeypatch,
) -> None:
    _ImmediateProcessPool.created = []
    monkeypatch.setattr(
        "count_and_design.application.ai_statistics.workflow.ProcessPoolExecutor",
        _ImmediateProcessPool,
    )

    def fail_in_process(*_args, **_kwargs):
        raise AssertionError(
            "Agent2 must not open PDFium in the GUI process"
        )

    monkeypatch.setattr(
        "count_and_design.application.ai_statistics.workflow."
        "render_high_resolution_page",
        fail_in_process,
    )
    monkeypatch.setattr(
        "count_and_design.application.ai_statistics.pdf_rendering."
        "render_high_resolution_page",
        fail_in_process,
    )

    recognizer = Mock()
    recognizer.recognize_pages.return_value = (Mock(page_number=2),)
    workflow = AIStatisticsWorkflow(overall_site_recognizer=recognizer)
    pdf_path = tmp_path / "drawing.pdf"
    pdf_path.write_bytes(b"%PDF")
    lazy = LazyPageImages(pdf_path, 2, width=600, height=900)
    workflow.load_pdf(pdf_path, 2)
    workflow.begin_page_classification()
    workflow.complete_page_classification(
        PageClassificationResult(
            pages=(
                PageClassification(
                    page_number=1,
                    page_type=DrawingPageType.ARRAY_LAYOUT.value,
                ),
                PageClassification(
                    page_number=2,
                    page_type=DrawingPageType.OVERALL_LAYOUT.value,
                ),
            ),
            page_images=lazy,
        )
    )
    workflow.confirm_current_stage()

    results = workflow.run_overall_site_recognition()

    recognizer.recognize_pages.assert_called_once_with({2: b"rendered-2"})
    assert results[0].page_number == 2
    assert lazy.cached_items({2}) == {2: b"rendered-2"}
    assert _ImmediateProcessPool.created
    assert _ImmediateProcessPool.created[0]["initializer"] is not None


def test_agent2_limits_onnx_threads_in_the_gui_process(monkeypatch) -> None:
    created: list[object] = []

    class _FakeDetector:
        def __init__(self, *_args, **_kwargs):
            self.parallelism = None
            created.append(self)

        def configure_parallelism(self, workers, intra_op_threads):
            self.parallelism = (workers, intra_op_threads)

        def warmup(self, *, image=None):
            del image

    monkeypatch.setattr(
        "count_and_design.application.ai_statistics.info_recognition."
        "overall_site.OnnxRuntimeYoloDetector",
        _FakeDetector,
    )
    monkeypatch.setattr(
        "count_and_design.application.ai_statistics.info_recognition."
        "overall_site.resolve_yolo_model_path",
        lambda *_args, **_kwargs: Path("overall.onnx"),
    )

    recognizer = Agent2OverallSiteRecognizer(
        mineru=Mock(),
        bedrock=Mock(),
    )
    detector = recognizer._schedule_detector()

    assert created == [detector]
    assert detector.parallelism == (1, 4)


def test_inv_identity_rejects_area_block_zone_labels() -> None:
    from count_and_design.application.ai_statistics.info_recognition.overall_site import (
        _inv_identity,
        _parse_extraction,
    )

    assert _inv_identity("AREA 1") is None
    assert _inv_identity("BLOCK A") is None
    assert _inv_identity("ZONE 3") is None
    assert _inv_identity("11A01") == "11A01"
    assert _inv_identity("PCU-0001") == "PCU-0001"

    payload = _claude_extraction()
    payload["inv_list"] = ["AREA 1", "AREA 2"]
    payload["inv_rows"] = [
        {"identifier": "AREA 1", "string_counts": []},
        {"identifier": "AREA 2", "string_counts": []},
    ]
    result = _parse_extraction(1, "", payload)

    assert result.inv_list == ()
    assert any("AREA/BLOCK/ZONE" in warning for warning in result.warnings)


def test_agent2_emits_table_bbox_labels() -> None:
    mineru = Mock()
    detector = Mock()
    detector.detect.return_value = [
        DetectionResult(
            label="tracker_table",
            confidence=0.95,
            class_id=0,
            box=BoundingBox(100, 100, 900, 300),
            source="onnxruntime:overall_schedule",
        )
    ]
    bedrock = Mock()
    bedrock.complete_with_image.return_value = _completion(_claude_extraction())
    recognizer = Agent2OverallSiteRecognizer(
        mineru,
        config=OverallSiteExtractionConfig(strategy="yolo_claude"),
        bedrock=bedrock,
        overall_schedule_detector=detector,
    )

    result = recognizer.recognize_page(7, _png())

    assert result.raw["table_bbox_labels"] == ["tracker_table"]


def test_table_bbox_overlays_from_result() -> None:
    from count_and_design.gui.views.ai_statistics.pages.info_recognition import (
        _table_bbox_overlays_from_result,
    )

    result = OverallSiteExtraction(
        page_number=22,
        raw={
            "table_bboxes": [[10, 20, 100, 200]],
            "table_bbox_labels": ["inv_tree"],
        },
    )

    overlays = _table_bbox_overlays_from_result(result)

    assert len(overlays) == 1
    assert overlays[0].bbox == (10, 20, 100, 200)
    assert overlays[0].label == "inv_tree"
    assert overlays[0].speculative is False

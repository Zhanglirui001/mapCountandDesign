from __future__ import annotations

import base64
import json
from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from count_and_design.application.ai import BoundingBox, DetectionResult
from count_and_design.application.ai_statistics import (
    Agent3ElementRecognizer,
    ArrayLayoutPageInput,
    BlockDiagramTableError,
    BlockDiagramTableResult,
    BlockDiagramTableRow,
    ElementRecognitionRequest,
    PanelOrientation,
    TableLocation,
    TrackerDefinition,
    build_agent3_debug_payload,
)


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (240, 160), "white").save(output, format="PNG")
    return output.getvalue()


def test_agent3_debug_export_contains_all_intermediate_results() -> None:
    image_bytes = _png()

    class _Yolo:
        def detect(self, _image, *, model_type, confidence=None):
            del confidence
            if model_type == "tracker":
                detections = [
                    DetectionResult(
                        "Node",
                        0.97,
                        BoundingBox(0, 0, 220, 150),
                        class_id=0,
                        source="tracker-model",
                    ),
                    DetectionResult(
                        "Tracker",
                        0.91,
                        BoundingBox(20, 20, 80, 60),
                        class_id=1,
                        raw={"tensor": (1, 2, 3)},
                    ),
                ]
            else:
                detections = [
                    DetectionResult(
                        "Box",
                        0.88,
                        BoundingBox(100, 70, 120, 90),
                        class_id=0,
                    )
                ]
            return SimpleNamespace(detections=detections)

    class _Table:
        def recognize(self, _image):
            return BlockDiagramTableResult(
                project_type="IBEX",
                equipment_family="CBX",
                strategy="mineru_claude",
                string_info=(BlockDiagramTableRow("INV1001-CBX-01", 2),),
                table_bbox=BoundingBox(1, 2, 30, 40),
                markdown="| FROM | STRINGS |",
                raw_llm={"answer": {"ok": True}},
                raw_output={
                    "content": "{\"equipment_family\": \"CBX\"}",
                    "model": "Qwen3.5-9B",
                },
            )

    class _Ocr:
        def parse_file(self, _image, *, filename, **_kwargs):
            return SimpleNamespace(
                markdown="INV1001-CBX-01",
                raw={"confidence": 0.93, "filename": filename},
            )

    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(7, image_bytes),),
        tracker_definitions=(TrackerDefinition("2-string", 2, 1, "Panel-A"),),
        expected_inv_names=("INV1001",),
        default_panel_orientation=PanelOrientation.POSITIVE_NORTH,
        project_name="Debug project",
    )
    result = Agent3ElementRecognizer(
        yolo=_Yolo(),
        table_recognizer=_Table(),
        node_ocr=_Ocr(),
        io_workers=4,
        node_ocr_orientations=2,
    ).recognize(request)

    payload = build_agent3_debug_payload(request, result)
    # The public contract must be directly writable with the stdlib JSON encoder.
    json.dumps(payload, ensure_ascii=False)

    assert payload["schema_version"] == "agent3-debug-v1"
    input_page = payload["input_data"]["pages"][0]
    assert input_page["page_number"] == 7
    assert input_page["byte_length"] == len(image_bytes)
    assert base64.b64decode(input_page["png_base64"]) == image_bytes

    table = payload["table_recognition_results"][0]["data"]["result"]
    assert table["strategy"] == "mineru_claude"
    assert table["markdown"] == "| FROM | STRINGS |"
    assert table["raw_llm"] == {"answer": {"ok": True}}
    assert table["raw_output"] == {
        "content": "{\"equipment_family\": \"CBX\"}",
        "model": "Qwen3.5-9B",
    }

    tracker_data = payload["yolo_tracker_detection_results"][0]["data"]
    assert [item["label"] for item in tracker_data["detections"]] == [
        "Node",
        "Tracker",
    ]
    assert tracker_data["detections"][1]["raw"] == {"tensor": [1, 2, 3]}

    box_data = payload["yolo_box_detection_results"][0]["data"]
    assert box_data["detections"][0]["label"] == "Box"

    ocr_data = payload["ocr_node_name_results"][0]["data"][0]
    assert ocr_data["final_node_name"] == "INV1001-CBX-01"
    assert ocr_data["selected"]["confidence"] == 0.93
    assert [item["angle_deg"] for item in ocr_data["readings"]] == [0]


def test_agent3_debug_export_can_omit_embedded_page_images() -> None:
    image_bytes = _png()
    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(1, image_bytes),),
        tracker_definitions=(TrackerDefinition("2-string", 2),),
    )
    result = SimpleNamespace(raw={}, pages=())

    payload = build_agent3_debug_payload(
        request,
        result,
        include_page_images=False,
    )

    input_page = payload["input_data"]["pages"][0]
    assert "png_base64" not in input_page
    assert input_page["byte_length"] == len(image_bytes)

def test_agent3_debug_export_keeps_located_region_on_table_failure() -> None:
    """A failed extraction still reports the region the locator found."""

    image_bytes = _png()

    class _Yolo:
        def detect(self, _image, *, model_type, confidence=None):
            del confidence
            if model_type == "tracker":
                return SimpleNamespace(detections=[
                    DetectionResult(
                        "Node",
                        0.97,
                        BoundingBox(0, 0, 220, 150),
                        class_id=0,
                        source="tracker-model",
                    ),
                ])
            return SimpleNamespace(detections=[])

    class _Table:
        def recognize(self, _image):
            raise BlockDiagramTableError(
                "Claude table extraction failed: Bedrock server request "
                "returned HTTP 503.",
                location=TableLocation(
                    bbox=BoundingBox(0.1, 0.25, 0.9, 0.75),
                    source="yolo",
                    raw={"yolo_confidence": 0.95, "yolo_seconds": 0.5},
                ),
            )

    class _Ocr:
        def parse_file(self, _image, *, filename, **_kwargs):
            return SimpleNamespace(markdown="", raw={})

    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(2, image_bytes),),
        tracker_definitions=(TrackerDefinition("2-string", 2),),
    )
    result = Agent3ElementRecognizer(
        yolo=_Yolo(),
        table_recognizer=_Table(),
        node_ocr=_Ocr(),
        io_workers=2,
        node_ocr_orientations=1,
    ).recognize(request)

    page = result.pages[0]
    assert page.table_bbox == BoundingBox(24.0, 40.0, 216.0, 120.0)
    assert page.table_bbox_source == "yolo"

    payload = build_agent3_debug_payload(request, result)
    json.dumps(payload, ensure_ascii=False)
    table = payload["table_recognition_results"][0]["data"]
    assert table["result"] is None
    assert table["location"] == {
        "table_bbox": {"x1": 0.1, "y1": 0.25, "x2": 0.9, "y2": 0.75},
        "source": "yolo",
        "regions": [
            {
                "bbox": {"x1": 0.1, "y1": 0.25, "x2": 0.9, "y2": 0.75},
                "kind": "yolo",
                "block_index": None,
            }
        ],
        "raw_output": {"yolo_confidence": 0.95, "yolo_seconds": 0.5},
    }


def test_agent3_debug_export_records_raw_llm_on_table_failure() -> None:
    """A failed LLM parse keeps the verbatim Qwen response in the export."""

    image_bytes = _png()

    class _Yolo:
        def detect(self, _image, *, model_type, confidence=None):
            del confidence
            if model_type == "tracker":
                return SimpleNamespace(detections=[
                    DetectionResult(
                        "Node",
                        0.97,
                        BoundingBox(0, 0, 220, 150),
                        class_id=0,
                        source="tracker-model",
                    ),
                ])
            return SimpleNamespace(detections=[])

    class _Table:
        def recognize(self, _image):
            raise BlockDiagramTableError(
                "The LLM response was not valid JSON.",
                raw_llm={
                    "content": "here is the table, sorry no json",
                    "model": "Qwen3.5-9B",
                    "prompt": "| FROM | STRINGS |",
                },
            )

    class _Ocr:
        def parse_file(self, _image, *, filename, **_kwargs):
            return SimpleNamespace(markdown="", raw={})

    request = ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(2, image_bytes),),
        tracker_definitions=(TrackerDefinition("2-string", 2),),
    )
    result = Agent3ElementRecognizer(
        yolo=_Yolo(),
        table_recognizer=_Table(),
        node_ocr=_Ocr(),
        io_workers=2,
        node_ocr_orientations=1,
    ).recognize(request)

    payload = build_agent3_debug_payload(request, result)
    json.dumps(payload, ensure_ascii=False)

    table = payload["table_recognition_results"][0]["data"]
    assert table["result"] is None
    assert table["error"] == "The LLM response was not valid JSON."
    assert table["raw_llm"] == {
        "content": "here is the table, sorry no json",
        "model": "Qwen3.5-9B",
        "prompt": "| FROM | STRINGS |",
    }

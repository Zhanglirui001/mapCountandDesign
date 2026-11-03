from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from count_and_design.application.ai import (
    BoundingBox,
    DetectionResult,
    YoloResult,
    draw_yolo_preview,
    save_yolo_preview,
)


def _result() -> YoloResult:
    return YoloResult(
        model_type="box",
        model_path="box.onnx",
        image="page.png",
        detections=[
            DetectionResult(
                label="Box",
                confidence=0.9,
                class_id=0,
                box=BoundingBox(10, 10, 50, 50),
                source="test",
            )
        ],
    )


def test_draw_and_save_yolo_preview(tmp_path: Path) -> None:
    image_path = tmp_path / "page.png"
    output_path = tmp_path / "previews" / "page.jpg"
    Image.new("RGB", (80, 60), "white").save(image_path)

    preview = draw_yolo_preview(image_path, _result())
    saved_path = save_yolo_preview(
        image_path,
        _result(),
        output_path,
    )

    assert preview.size == (80, 60)
    assert preview.getpixel((10, 10)) != (255, 255, 255)
    assert saved_path == output_path
    assert output_path.exists()


def test_draw_yolo_preview_accepts_image_bytes() -> None:
    buffer = BytesIO()
    Image.new("RGB", (80, 60), "white").save(buffer, format="PNG")

    preview = draw_yolo_preview(buffer.getvalue(), _result())

    assert preview.size == (80, 60)

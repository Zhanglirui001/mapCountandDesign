"""Structured and JSON-ready YOLO result types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..types import DetectionResult


def detection_result_to_dict(item: DetectionResult) -> dict[str, Any]:
    return {
        "label": item.label,
        "confidence": item.confidence,
        "class_id": item.class_id,
        "box": {
            "x1": item.box.x1,
            "y1": item.box.y1,
            "x2": item.box.x2,
            "y2": item.box.y2,
        },
        "source": item.source,
    }


@dataclass(frozen=True)
class YoloResult:
    """Normalized result for one image and one YOLO model type."""

    model_type: str
    model_path: str
    image: str
    detections: list[DetectionResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": self.model_type,
            "model_path": self.model_path,
            "image": self.image,
            "detections": [
                detection_result_to_dict(item)
                for item in self.detections
            ],
        }

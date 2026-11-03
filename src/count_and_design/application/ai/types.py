"""Shared value objects for AI model outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BoundingBox:
    """Pixel-space bounding box in xyxy format."""

    x1: float
    y1: float
    x2: float
    y2: float

    def to_tuple(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)


@dataclass(frozen=True)
class DetectionResult:
    """Normalized object detection result independent of a model backend."""

    label: str
    confidence: float
    box: BoundingBox
    class_id: int | None = None
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

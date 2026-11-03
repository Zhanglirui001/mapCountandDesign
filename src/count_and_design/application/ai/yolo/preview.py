"""Optional Pillow-based visualization for normalized YOLO results."""

from __future__ import annotations

from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from .result import YoloResult
from .runtime import ImageSource

if TYPE_CHECKING:
    from PIL import Image


Color = tuple[int, int, int]

DEFAULT_LABEL_COLORS: dict[str, Color] = {
    "Tracker": (230, 60, 60),
    "Node": (40, 120, 240),
    "Box": (30, 170, 90),
}
_FALLBACK_COLORS: tuple[Color, ...] = (
    (180, 80, 200),
    (230, 150, 40),
    (40, 170, 150),
    (120, 120, 230),
    (210, 90, 120),
)


class YoloPreviewError(RuntimeError):
    """Raised when a YOLO preview cannot be rendered."""


def draw_yolo_preview(
    image: ImageSource,
    result: YoloResult,
    *,
    label_colors: Mapping[str, Color] | None = None,
    line_width: int = 3,
) -> Image.Image:
    """Draw detections and return a Pillow RGB Image."""

    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover - dependency contract
        raise YoloPreviewError(
            "Pillow is required for YOLO previews. "
            "Install count-and-design[ai]."
        ) from exc

    if line_width <= 0:
        raise YoloPreviewError("Preview line width must be greater than zero.")

    try:
        source = BytesIO(image) if isinstance(image, bytes) else Path(image).expanduser()
        preview = Image.open(source).convert("RGB")
    except Exception as exc:
        raise YoloPreviewError(f"Could not open preview image: {exc}") from exc

    colors = dict(DEFAULT_LABEL_COLORS)
    colors.update(label_colors or {})
    draw = ImageDraw.Draw(preview)
    for detection in result.detections:
        color = colors.get(detection.label) or _color_for_label(detection.label)
        box = detection.box.to_tuple()
        draw.rectangle(box, outline=color, width=line_width)
        label = (
            f"{result.model_type}:{detection.label} "
            f"{detection.confidence:.2f}"
        )
        text_x = box[0]
        text_y = max(0.0, box[1] - 14)
        text_box = draw.textbbox((text_x, text_y), label)
        draw.rectangle(text_box, fill=color)
        draw.text((text_x, text_y), label, fill=(255, 255, 255))
    return preview


def save_yolo_preview(
    image: ImageSource,
    result: YoloResult,
    output_path: str | Path,
    *,
    label_colors: Mapping[str, Color] | None = None,
    line_width: int = 3,
) -> Path:
    """Render detections to an image file and return its path."""

    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    preview = draw_yolo_preview(
        image,
        result,
        label_colors=label_colors,
        line_width=line_width,
    )
    try:
        preview.save(path)
    except Exception as exc:
        raise YoloPreviewError(f"Could not save YOLO preview: {exc}") from exc
    return path


def _color_for_label(label: str) -> Color:
    index = sum(label.encode("utf-8")) % len(_FALLBACK_COLORS)
    return _FALLBACK_COLORS[index]

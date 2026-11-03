"""Line enhancement matching the labeling_workflow recognition preprocess.

Ports the stack used for YOLO training images (without hard black line press):

1. Neutralize pale colored fills while keeping local line contrast.
2. Multi-scale local dark-detail enhancement (no geometry warp).
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import cv2
import numpy as np
from PIL import Image


@dataclass(frozen=True)
class LineEnhanceParams:
    """Parameters aligned with labeling_workflow ``enhance_png_for_recognition``."""

    fill_gray: int = 205
    color_chroma_min: int = 8
    color_gray_min: int = 160
    color_gray_max: int = 250
    fill_detail_sigma: float = 2.2
    fill_detail_gain: float = 1.35
    detail_sigmas: tuple[float, ...] = (1.0, 2.2, 4.0)
    detail_gain: float = 0.9
    tracker_gray_min: int = 175
    tracker_gray_max: int = 220
    tracker_detail_gain: float = 1.05
    detail_max: int = 55
    very_dark_level: int = 70
    very_dark_gain: float = 0.2
    paper_white_level: int = 250
    paper_detail_max: int = 2


DEFAULT_LINE_ENHANCE_PARAMS = LineEnhanceParams()


def enhance_line_image(
    image: Image.Image,
    params: LineEnhanceParams = DEFAULT_LINE_ENHANCE_PARAMS,
) -> Image.Image:
    """Return an RGB image after recognition-style line enhancement."""

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    enhanced_bgr = enhance_bgr(bgr, params)
    enhanced_rgb = cv2.cvtColor(enhanced_bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(enhanced_rgb, mode="RGB")


def enhance_line_png_bytes(
    image_bytes: bytes,
    params: LineEnhanceParams = DEFAULT_LINE_ENHANCE_PARAMS,
) -> bytes:
    with Image.open(BytesIO(image_bytes)) as loaded:
        enhanced = enhance_line_image(loaded, params)
    output = BytesIO()
    enhanced.save(output, format="PNG")
    enhanced.close()
    return output.getvalue()


def enhance_bgr(
    img_bgr: np.ndarray,
    params: LineEnhanceParams = DEFAULT_LINE_ENHANCE_PARAMS,
) -> np.ndarray:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    neutral = _neutralize_colored_fills(img_bgr, gray, params)
    enhanced_gray = _enhance_local_dark_details(neutral, params)
    return cv2.cvtColor(enhanced_gray, cv2.COLOR_GRAY2BGR)


def _dark_detail(gray: np.ndarray, sigma: float) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=sigma, sigmaY=sigma)
    return np.maximum(blur.astype(np.int16) - gray.astype(np.int16), 0)


def _fill_line_detail(gray: np.ndarray, sigma: float) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=sigma, sigmaY=sigma)
    dark = blur.astype(np.int16) - gray.astype(np.int16)
    bright = gray.astype(np.int16) - blur.astype(np.int16)
    return np.maximum(np.maximum(dark, 0), np.maximum(bright, 0))


def _neutralize_colored_fills(
    img_bgr: np.ndarray,
    gray: np.ndarray,
    params: LineEnhanceParams,
) -> np.ndarray:
    b, g, r = cv2.split(img_bgr)
    max_ch = np.maximum(np.maximum(b, g), r)
    min_ch = np.minimum(np.minimum(b, g), r)
    chroma = cv2.subtract(max_ch, min_ch)

    color_mask = (
        (chroma >= params.color_chroma_min)
        & (gray >= params.color_gray_min)
        & (gray <= params.color_gray_max)
    )

    out = gray.copy()
    if not np.any(color_mask):
        return out

    fill_detail = _fill_line_detail(gray, params.fill_detail_sigma)
    mapped = (
        float(params.fill_gray)
        - params.fill_detail_gain * fill_detail.astype(np.float32)
    )
    mapped = np.clip(mapped, 0, 255).astype(np.uint8)
    out[color_mask] = mapped[color_mask]
    return out


def _enhance_local_dark_details(
    gray: np.ndarray,
    params: LineEnhanceParams,
) -> np.ndarray:
    detail = np.zeros_like(gray, dtype=np.int16)
    for sigma in params.detail_sigmas:
        detail = np.maximum(detail, _dark_detail(gray, sigma))
    detail = np.minimum(detail, params.detail_max)

    src = gray.astype(np.float32)
    gain = np.full(gray.shape, params.detail_gain, dtype=np.float32)
    tracker_fill = (gray >= params.tracker_gray_min) & (
        gray <= params.tracker_gray_max
    )
    gain[tracker_fill] = params.tracker_detail_gain
    gain[gray <= params.very_dark_level] = params.very_dark_gain

    out = src - gain * detail.astype(np.float32)
    out = np.clip(out, 0, 255).astype(np.uint8)

    paper = (gray >= params.paper_white_level) & (detail <= params.paper_detail_max)
    out[paper] = 255
    return out

"""Render Agent3 page images from a PDF with optional grayscale / line enhance.

Edit the configuration block below, then run:

    python test_scripts/render_agent3_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from count_and_design.application.ai_statistics.element_recognition.process_runner import (
    AGENT3_RENDER_MAX_SIDE,
)
from count_and_design.application.ai_statistics.element_recognition.service import (
    DEFAULT_DETECT_MAX_SIDE,
    encode_agent3_page_png,
    _downscale_for_detection,
    prepare_agent3_page_image,
)
from count_and_design.application.ai_statistics.element_recognition.line_enhance import (
    enhance_line_image,
)
from count_and_design.application.ai_statistics.pdf_rendering import (
    PdfRenderingError,
    pdf_page_count,
    render_high_resolution_page,
)
from io import BytesIO

from PIL import Image

# --- Configuration -----------------------------------------------------------

PDF_PATH = Path(
    r"C:\Users\Voltage\Downloads\历史项目数据\历史项目数据\funston.pdf"
)
# 1-based page numbers.
PAGES = (
    76,82,100,106
)
# Simple L-channel grayscale. Ignored when LINE_ENHANCE is True.
GRAYSCALE = False
# Recognition-style line enhance (same stack as labeling *_250dpi_enhanced).
LINE_ENHANCE = False
# When LINE_ENHANCE is True, choose enhance order relative to YOLO downscale:
#   "before" = enhance at render resolution, then downscale to DETECT_MAX_SIDE
#   "after"  = downscale to DETECT_MAX_SIDE first, then enhance (Agent3 default)
ENHANCE_ORDER = "before"
OUTPUT_DIR = Path("ocr_outputs/agent3_pages")
# Long-side cap after PDF render; Agent3 production uses 6000.
MAX_SIDE = AGENT3_RENDER_MAX_SIDE
# YOLO detection long-side cap; Agent3 production uses AGENT3_YOLO_MAX_SIDE.
DETECT_MAX_SIDE = DEFAULT_DETECT_MAX_SIDE

# -----------------------------------------------------------------------------


def _suffix(*, grayscale: bool, line_enhance: bool, enhance_order: str) -> str:
    if line_enhance:
        return f"_enhanced_{enhance_order}"
    if grayscale:
        return "_gray"
    return "_color"


def _encode_png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _process_page(
    rendered: bytes,
    *,
    grayscale: bool,
    line_enhance: bool,
    enhance_order: str,
    max_side: int,
    detect_max_side: int,
) -> bytes:
    if not line_enhance:
        return encode_agent3_page_png(
            rendered,
            max_side=max_side,
            grayscale=grayscale,
        )

    with Image.open(BytesIO(rendered)) as loaded:
        image = prepare_agent3_page_image(loaded)

    if max(image.size) > max_side > 0:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    order = enhance_order.strip().lower()
    if order == "before":
        enhanced = enhance_line_image(image)
        if enhanced is not image:
            image.close()
        detect_image, _sx, _sy = _downscale_for_detection(enhanced, detect_max_side)
        if detect_image is not enhanced:
            enhanced.close()
        final = _encode_png(detect_image)
        detect_image.close()
        return final

    if order == "after":
        detect_image, _sx, _sy = _downscale_for_detection(image, detect_max_side)
        if detect_image is not image:
            image.close()
        enhanced = enhance_line_image(detect_image)
        if enhanced is not detect_image:
            detect_image.close()
        final = _encode_png(enhanced)
        enhanced.close()
        return final

    raise ValueError(
        f"ENHANCE_ORDER must be 'before' or 'after', got {enhance_order!r}."
    )


def main() -> int:
    if not PDF_PATH.is_file():
        print(f"PDF not found: {PDF_PATH}", file=sys.stderr)
        return 1
    if MAX_SIDE <= 0:
        print("MAX_SIDE must be positive.", file=sys.stderr)
        return 1
    if DETECT_MAX_SIDE <= 0:
        print("DETECT_MAX_SIDE must be positive.", file=sys.stderr)
        return 1
    if not PAGES:
        print("PAGES must contain at least one page number.", file=sys.stderr)
        return 1
    if LINE_ENHANCE and ENHANCE_ORDER.strip().lower() not in {"before", "after"}:
        print(
            "ENHANCE_ORDER must be 'before' or 'after' when LINE_ENHANCE is True.",
            file=sys.stderr,
        )
        return 1

    pages = sorted({int(page) for page in PAGES})
    for page in pages:
        if page <= 0:
            print(f"Page numbers must be positive, got {page}.", file=sys.stderr)
            return 1

    try:
        total_pages = pdf_page_count(PDF_PATH)
    except PdfRenderingError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    invalid = [page for page in pages if page > total_pages]
    if invalid:
        joined = ", ".join(str(page) for page in invalid)
        print(
            f"Page(s) out of range for {PDF_PATH.name} "
            f"({total_pages} page(s) total): {joined}",
            file=sys.stderr,
        )
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_stem = PDF_PATH.stem
    suffix = _suffix(
        grayscale=GRAYSCALE,
        line_enhance=LINE_ENHANCE,
        enhance_order=ENHANCE_ORDER.strip().lower(),
    )

    print(
        f"PDF: {PDF_PATH.resolve()} "
        f"({total_pages} page(s), max_side={MAX_SIDE}, "
        f"detect_max_side={DETECT_MAX_SIDE}, grayscale={GRAYSCALE}, "
        f"line_enhance={LINE_ENHANCE}, enhance_order={ENHANCE_ORDER})"
    )

    for page_number in pages:
        print(f"Rendering page {page_number} ...", flush=True)
        try:
            rendered = render_high_resolution_page(
                PDF_PATH,
                page_number - 1,
                width=MAX_SIDE,
                height=MAX_SIDE,
            )
            final = _process_page(
                rendered,
                grayscale=GRAYSCALE,
                line_enhance=LINE_ENHANCE,
                enhance_order=ENHANCE_ORDER,
                max_side=MAX_SIDE,
                detect_max_side=DETECT_MAX_SIDE,
            )
        except (PdfRenderingError, ValueError) as exc:
            print(f"  FAIL page {page_number}: {exc}", file=sys.stderr)
            return 1

        output_path = OUTPUT_DIR / f"{pdf_stem}_p{page_number:03d}{suffix}.png"
        output_path.write_bytes(final)
        print(f"  -> {output_path.resolve()} ({len(final)} bytes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

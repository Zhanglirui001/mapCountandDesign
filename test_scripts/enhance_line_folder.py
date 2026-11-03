"""Batch line-enhance images with the same stack as render_agent3_page.py.

No resize: images keep their original pixel size so LabelMe JSON coordinates
stay valid. Sidecar .json files are copied as-is.

Edit the configuration block below, then run:

    python test_scripts/enhance_line_folder.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PIL import Image

from count_and_design.application.ai_statistics.element_recognition.line_enhance import (
    enhance_line_image,
)
from count_and_design.application.ai_statistics.element_recognition.service import (
    prepare_agent3_page_image,
)

# --- Configuration -----------------------------------------------------------

INPUT_DIR = Path(
    r"C:\Users\Voltage\Downloads\标注工作区\完整训练区\Tracker-Gray"
)
OUTPUT_DIR = Path(
    r"C:\Users\Voltage\Downloads\标注工作区\完整训练区\Tracker-Gray_enhanced"
)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
COPY_SIDECAR_JSON = True
SKIP_EXISTING = True

# -----------------------------------------------------------------------------


def _iter_images(root: Path) -> list[Path]:
    images = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    images.sort()
    return images


def _enhance_image(source: Path, destination: Path) -> tuple[int, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as loaded:
        image = prepare_agent3_page_image(loaded)
        size = image.size
        enhanced = enhance_line_image(image)
        if enhanced is not image:
            image.close()
    enhanced.save(destination, format="PNG")
    enhanced.close()
    return size


def _copy_sidecar_json(source_image: Path, destination_image: Path) -> Path | None:
    source_json = source_image.with_suffix(".json")
    if not source_json.is_file():
        return None
    destination_json = destination_image.with_suffix(".json")
    if SKIP_EXISTING and destination_json.is_file():
        return destination_json
    shutil.copy2(source_json, destination_json)
    return destination_json


def main() -> int:
    if not INPUT_DIR.is_dir():
        print(f"Input folder not found: {INPUT_DIR}", file=sys.stderr)
        return 1

    images = _iter_images(INPUT_DIR)
    if not images:
        print(f"No image files in {INPUT_DIR}", file=sys.stderr)
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(
        f"Input: {INPUT_DIR.resolve()} ({len(images)} image(s))\n"
        f"Output: {OUTPUT_DIR.resolve()} "
        f"(line enhance, no scale, skip_existing={SKIP_EXISTING})"
    )

    failed = 0
    skipped = 0
    enhanced_count = 0
    for index, source in enumerate(images, start=1):
        relative = source.relative_to(INPUT_DIR)
        destination = (OUTPUT_DIR / relative).with_suffix(".png")
        prefix = f"[{index}/{len(images)}] {relative}"

        if SKIP_EXISTING and destination.is_file() and destination.stat().st_size > 0:
            skipped += 1
            print(f"{prefix} skip (exists)")
            if COPY_SIDECAR_JSON:
                _copy_sidecar_json(source, destination)
            continue

        print(f"{prefix} enhance ...", flush=True)
        try:
            width, height = _enhance_image(source, destination)
            if COPY_SIDECAR_JSON:
                _copy_sidecar_json(source, destination)
        except Exception as exc:
            failed += 1
            print(f"  FAIL: {exc}", file=sys.stderr)
            continue

        enhanced_count += 1
        print(f"  -> {destination} ({width}x{height}, no scale)")

    print(
        f"Done. enhanced={enhanced_count}, skipped={skipped}, failed={failed}, "
        f"total={len(images)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

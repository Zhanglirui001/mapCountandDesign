"""Batch-test block_schedule YOLO table detection on a folder of PNGs."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from count_and_design.application.ai import (
    OnnxRuntimeYoloDetector,
    YoloResult,
    bundled_yolo_model_dir,
    detection_result_to_dict,
    save_yolo_preview,
)
from count_and_design.application.ai.yolo.runtime import (
    BLOCK_SCHEDULE_MODEL_FILENAME,
    BLOCK_SCHEDULE_MODEL_TYPE,
    resolve_yolo_model_path,
)

INPUT_DIR = Path(
    r"C:\Users\Voltage\Downloads\标注工作区\YOLO识别区"
)
OUTPUT_DIR = Path("detection_outputs/block_schedule_yolo")
DETECTION_CONFIDENCE = 0.25
FALLBACK_CONFIDENCE = 0.8


def main() -> int:
    if not INPUT_DIR.is_dir():
        print(f"Input folder not found: {INPUT_DIR}", file=sys.stderr)
        return 1

    images = sorted(INPUT_DIR.glob("*.png"))
    if not images:
        print(f"No PNG files in {INPUT_DIR}", file=sys.stderr)
        return 1

    model_path = resolve_yolo_model_path(
        bundled_yolo_model_dir(),
        BLOCK_SCHEDULE_MODEL_TYPE,
        BLOCK_SCHEDULE_MODEL_FILENAME,
    )
    detector = OnnxRuntimeYoloDetector(
        model_path,
        confidence=DETECTION_CONFIDENCE,
        source=f"onnxruntime:{BLOCK_SCHEDULE_MODEL_TYPE}",
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    summary_rows: list[dict[str, object]] = []
    for index, image_path in enumerate(images, start=1):
        detections = detector.detect(image_path, confidence=DETECTION_CONFIDENCE)
        best = max(detections, key=lambda item: item.confidence, default=None)
        result = YoloResult(
            model_type=BLOCK_SCHEDULE_MODEL_TYPE,
            model_path=str(model_path),
            image=str(image_path),
            detections=detections,
        )
        stem = image_path.stem
        json_path = OUTPUT_DIR / f"{stem}.json"
        preview_path = OUTPUT_DIR / f"{stem}_preview.jpg"
        json_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        save_yolo_preview(image_path, result, preview_path)

        row = {
            "image": image_path.name,
            "detection_count": len(detections),
            "best_label": best.label if best else "",
            "best_confidence": round(best.confidence, 4) if best else None,
            "would_use_yolo": bool(best and best.confidence >= FALLBACK_CONFIDENCE),
            "detections": [detection_result_to_dict(item) for item in detections],
        }
        summary_rows.append(row)
        status = (
            f"YOLO ({best.confidence:.3f})"
            if row["would_use_yolo"]
            else (
                f"MinerU fallback ({best.confidence:.3f})"
                if best
                else "MinerU fallback (no detection)"
            )
        )
        print(
            f"[{index}/{len(images)}] {image_path.name}: "
            f"{len(detections)} box(es), best={row['best_confidence']}, {status}"
        )

    summary_path = OUTPUT_DIR / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "input_dir": str(INPUT_DIR),
                "model_path": str(model_path),
                "detection_confidence": DETECTION_CONFIDENCE,
                "fallback_confidence": FALLBACK_CONFIDENCE,
                "pages": summary_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nSummary: {summary_path.resolve()}")
    print(f"Previews: {OUTPUT_DIR.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

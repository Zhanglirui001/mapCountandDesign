"""Batch-test block-diagram table recognition (YOLO/MinerU + Claude) on PNGs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from count_and_design.application.ai_statistics.element_recognition.block_diagram_table import (
    BlockDiagramTableError,
    BlockDiagramTableRecognizer,
)

DEFAULT_INPUT = Path(
    r"C:\Users\Voltage\Downloads\标注工作区\YOLO识别区"
)
DEFAULT_OUTPUT = Path("ocr_outputs/yolo_claude")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run yolo_claude / mineru_claude table recognition on a folder.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="Folder of block-diagram PNGs",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Directory for per-page JSON and summary",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help="Path to .env with Bedrock and strategy settings",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only the first N images (0 = all)",
    )
    parser.add_argument(
        "--pages",
        nargs="*",
        default=(),
        help="Optional page stems or filenames to run (e.g. p020 p025)",
    )
    args = parser.parse_args()

    if not args.input.is_dir():
        print(f"Input folder not found: {args.input}", file=sys.stderr)
        return 1

    images = sorted(args.input.glob("*.png"))
    if args.pages:
        tokens = {token.lower() for token in args.pages}
        images = [
            path
            for path in images
            if any(token in path.stem.lower() for token in tokens)
        ]
    if args.limit > 0:
        images = images[: args.limit]
    if not images:
        print(f"No matching PNG files in {args.input}", file=sys.stderr)
        return 1

    recognizer = BlockDiagramTableRecognizer.from_env(args.env)
    args.output.mkdir(parents=True, exist_ok=True)
    crop_dir = args.output / "crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    recognizer = BlockDiagramTableRecognizer(
        config=recognizer.config,
        mineru=recognizer.mineru,
        bedrock=recognizer.bedrock,
        temp_dir=crop_dir,
    )

    summary_rows: list[dict[str, object]] = []
    for index, image_path in enumerate(images, start=1):
        print(f"[{index}/{len(images)}] {image_path.name} ...", flush=True)
        try:
            result = recognizer.recognize(
                image_path,
            )
        except BlockDiagramTableError as exc:
            row = {
                "image": image_path.name,
                "error": str(exc),
                "raw_llm": getattr(exc, "raw_llm", None),
            }
            summary_rows.append(row)
            print(f"  FAIL: {exc}")
            continue

        payload = result.to_dict()
        payload["strategy"] = result.strategy
        payload["table_bbox"] = (
            {
                "x1": result.table_bbox.x1,
                "y1": result.table_bbox.y1,
                "x2": result.table_bbox.x2,
                "y2": result.table_bbox.y2,
            }
            if result.table_bbox is not None
            else None
        )
        payload["raw_output"] = dict(result.raw_output)
        payload["cropped_image_path"] = (
            str(result.cropped_image_path)
            if result.cropped_image_path is not None
            else None
        )

        json_path = args.output / f"{image_path.stem}.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        row_count = len(result.string_info)
        bbox_source = result.raw_output.get("table_bbox_source", "?")
        yolo_conf = result.raw_output.get("yolo_confidence")
        yolo_sec = result.raw_output.get("yolo_seconds")
        mineru_sec = result.raw_output.get("mineru_seconds")
        claude_sec = result.raw_output.get("claude_seconds")
        total_sec = result.raw_output.get("total_seconds")
        print(
            f"  OK strategy={result.strategy} "
            f"family={result.equipment_family} rows={row_count} "
            f"bbox={bbox_source} yolo={yolo_conf} "
            f"time(yolo={yolo_sec}s mineru={mineru_sec}s claude={claude_sec}s total={total_sec}s)"
        )
        summary_rows.append(
            {
                "image": image_path.name,
                "strategy": result.strategy,
                "equipment_family": result.equipment_family,
                "project_type": result.project_type,
                "row_count": row_count,
                "table_bbox_source": bbox_source,
                "yolo_confidence": yolo_conf,
                "yolo_seconds": yolo_sec,
                "mineru_seconds": mineru_sec,
                "claude_seconds": claude_sec,
                "total_seconds": total_sec,
                "string_info": payload["string_info"],
                "json_path": str(json_path),
                "cropped_image_path": payload["cropped_image_path"],
            }
        )

    summary_path = args.output / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "strategy": recognizer.config.strategy,
                "input_dir": str(args.input),
                "pages": summary_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    ok = sum(1 for row in summary_rows if "error" not in row)
    success_rows = [row for row in summary_rows if "error" not in row]
    if success_rows:
        def _avg(key: str) -> float:
            values = [float(row[key]) for row in success_rows if row.get(key) is not None]
            return round(sum(values) / len(values), 3) if values else 0.0

        print(
            "\nAverage timing (s): "
            f"yolo={_avg('yolo_seconds')} "
            f"mineru={_avg('mineru_seconds')} "
            f"claude={_avg('claude_seconds')} "
            f"total={_avg('total_seconds')}"
        )
    print(f"\nDone: {ok}/{len(images)} succeeded")
    print(f"Summary: {summary_path.resolve()}")
    return 0 if ok == len(images) else 1


if __name__ == "__main__":
    raise SystemExit(main())

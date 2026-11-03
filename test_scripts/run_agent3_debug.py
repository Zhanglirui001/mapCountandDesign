"""Run Agent3 against real services and write JSON plus boxed previews."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

from count_and_design.application.ai_statistics import (
    Agent3ElementRecognizer,
    ArrayLayoutPageInput,
    ElementRecognitionRequest,
    InvRecognitionSettings,
    PanelOrientation,
    TrackerDefinition,
)
from count_and_design.application.env import load_env_file


COLORS = {"node": "#2563eb", "tracker": "#059669", "box": "#d97706"}


def _page(value: str) -> tuple[int, Path]:
    parts = value.split(":", 1)
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("Expected PAGE:IMAGE_PATH")
    try:
        page_number = int(parts[0])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("PAGE must be an integer") from exc
    path = Path(parts[1]).expanduser()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"Image does not exist: {path}")
    return page_number, path


def _tracker(value: str) -> TrackerDefinition:
    parts = value.split(":", 3)
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(
            "Expected CODE:STRINGS[:TOTAL[:PANEL_LABEL]]"
        )
    try:
        strings = int(parts[1])
        total = int(parts[2]) if len(parts) >= 3 and parts[2] else 0
    except ValueError as exc:
        raise argparse.ArgumentTypeError("STRINGS and TOTAL must be integers") from exc
    label = parts[3] if len(parts) == 4 else parts[0]
    return TrackerDefinition(parts[0], strings, total, label)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--page",
        action="append",
        required=True,
        type=_page,
        help="Array Layout in PAGE:IMAGE_PATH form; repeatable.",
    )
    parser.add_argument(
        "--inv",
        action="append",
        default=[],
        help="Agent2 INV name used to validate recognized page ownership; repeatable.",
    )
    parser.add_argument(
        "--tracker",
        action="append",
        required=True,
        type=_tracker,
        help="Agent2 tracker definition; repeatable.",
    )
    parser.add_argument(
        "--orientation",
        choices=[item.value for item in PanelOrientation],
        default=PanelOrientation.UNKNOWN.value,
    )
    parser.add_argument("--offset-step", type=float, default=1.0)
    parser.add_argument("--project-name", default="Agent3 Debug")
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, default=Path("agent3_outputs"))
    return parser.parse_args()


def _save_preview(source: Path, page, destination: Path) -> None:
    image = Image.open(source).convert("RGB")
    draw = ImageDraw.Draw(image)
    for detection in page.detections:
        issue = bool(detection.issues) or any(
            item.entity_id == detection.entity_id for item in page.issues
        )
        color = "#dc2626" if issue else COLORS.get(detection.kind.lower(), "#7c3aed")
        bbox = detection.bbox.to_tuple()
        draw.rectangle(bbox, outline=color, width=4)
        draw.text(
            (bbox[0], max(0, bbox[1] - 14)),
            f"{detection.entity_id} {detection.confidence:.2f}",
            fill=color,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)


def main() -> None:
    args = _arguments()
    if args.offset_step <= 0:
        raise SystemExit("--offset-step must be positive")
    load_env_file(args.env)
    orientation = PanelOrientation(args.orientation)
    inputs = tuple(
        ArrayLayoutPageInput(number, path.read_bytes())
        for number, path in args.page
    )
    inv_names = tuple(dict.fromkeys(item.strip() for item in args.inv if item.strip()))
    request = ElementRecognitionRequest(
        pages=inputs,
        tracker_definitions=tuple(args.tracker),
        expected_inv_names=inv_names,
        default_panel_orientation=orientation,
        inv_settings=tuple(
            InvRecognitionSettings(inv_name, None, args.offset_step)
            for inv_name in inv_names
        ),
        project_name=args.project_name,
    )
    result = Agent3ElementRecognizer().recognize(
        request,
        on_progress=lambda completed, total: print(
            f"Agent3 progress: {completed}/{total}"
        ),
    )
    args.output.mkdir(parents=True, exist_ok=True)
    json_path = args.output / "agent3-result.json"
    json_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    source_by_page = {number: path for number, path in args.page}
    for page in result.pages:
        _save_preview(
            source_by_page[page.page_number],
            page,
            args.output / f"page-{page.page_number}-preview.png",
        )
    print(f"Result: {json_path.resolve()}")
    print(f"Issues: {len(result.all_issues)}")


if __name__ == "__main__":
    main()

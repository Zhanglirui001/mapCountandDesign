"""Run the real Agent2 MinerU -> table crops -> Bedrock Claude pipeline."""

from __future__ import annotations

import argparse
from io import BytesIO
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from count_and_design.application.ai.llm.providers import BedrockLlmProvider
from count_and_design.application.ai_statistics import (
    Agent2OverallSiteRecognizer,
    build_agent4_input,
    build_agent4_validation_input,
)
from count_and_design.application.config import BedrockLlmConfig


def _vision_smoke_image() -> bytes:
    image = Image.new("RGB", (640, 180), "white")
    ImageDraw.Draw(image).text(
        (30, 60),
        "BEDROCK VISION TEST 42",
        fill="black",
    )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _synthetic_overall_image() -> bytes:
    image = Image.new("RGB", (2200, 1500), "white")
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("arial.ttf", 42)
        cell_font = ImageFont.truetype("arial.ttf", 32)
    except OSError:
        title_font = ImageFont.load_default(size=42)
        cell_font = ImageFont.load_default(size=32)

    def table(
        x: int,
        y: int,
        widths: list[int],
        rows: list[list[str]],
        *,
        row_height: int,
    ) -> None:
        total_width = sum(widths)
        draw.rectangle(
            (x, y, x + total_width, y + row_height * len(rows)),
            outline="black",
            width=5,
        )
        for row_index, row in enumerate(rows):
            row_y = y + row_index * row_height
            if row_index:
                draw.line(
                    (x, row_y, x + total_width, row_y),
                    fill="black",
                    width=4,
                )
            column_x = x
            for column_index, value in enumerate(row):
                if column_index:
                    draw.line(
                        (column_x, row_y, column_x, row_y + row_height),
                        fill="black",
                        width=4,
                    )
                draw.text(
                    (column_x + 18, row_y + 32),
                    value,
                    fill="black",
                    font=cell_font,
                )
                column_x += widths[column_index]

    draw.text((100, 60), "SYSTEMS SPECIFICATIONS", fill="black", font=title_font)
    table(
        100,
        130,
        [900, 700],
        [
            ["FIELD", "MODULE 1"],
            ["MODULE MODEL", "ABC-620"],
            ["MODULE STC DC RATING (W)", "620"],
            ["STRING COUNT", "9767"],
            ["3 STRING TRACKER", "2779"],
            ["2 STRING TRACKER", "715"],
        ],
        row_height=120,
    )
    draw.text((100, 930), "SUMMARY TABLE", fill="black", font=title_font)
    table(
        100,
        1000,
        [700, 700],
        [
            ["INV", "BLOCK"],
            ["11A01", "A"],
            ["11A02", "B"],
            ["11A03", "C"],
        ],
        row_height=105,
    )
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _run_vision_smoke() -> None:
    provider = BedrockLlmProvider(BedrockLlmConfig.from_env())
    result = provider.complete_with_image(
        'Read the image and return only JSON: {"text":"<visible text>"}',
        _vision_smoke_image(),
        system_prompt="Return valid JSON only.",
    )
    print(result.content)
    print(
        json.dumps(
            {
                "model": result.model,
                "finish_reason": result.finish_reason,
                "usage": result.usage,
            },
            ensure_ascii=False,
        )
    )


def _run_agent2_bytes(image_bytes: bytes, page_number: int) -> None:
    result = Agent2OverallSiteRecognizer().recognize_page(
        page_number,
        image_bytes,
        on_progress=lambda completed, total: print(
            f"progress={completed}/{total}"
        ),
    )
    print(json.dumps(result.raw, ensure_ascii=False, indent=2))
    print("\nfrontend_payload:")
    print(json.dumps(build_agent4_input((result,)), ensure_ascii=False, indent=2))
    print("\nagent4_payload:")
    print(
        json.dumps(
            build_agent4_validation_input((result,)),
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "image",
        nargs="?",
        type=Path,
        help="High-resolution overall-layout PNG/JPEG. Omit for Bedrock smoke.",
    )
    parser.add_argument("--page-number", type=int, default=1)
    parser.add_argument(
        "--synthetic-agent2",
        action="store_true",
        help="Run the complete real pipeline against a generated table image.",
    )
    args = parser.parse_args()
    if args.synthetic_agent2:
        _run_agent2_bytes(_synthetic_overall_image(), args.page_number)
        return
    if args.image is None:
        _run_vision_smoke()
        return
    if not args.image.is_file():
        parser.error(f"image does not exist: {args.image}")
    _run_agent2_bytes(args.image.read_bytes(), args.page_number)


if __name__ == "__main__":
    main()

from pathlib import Path

from count_and_design.application.ai import MinerUOcrService


IMAGE_PATH = Path(
    r"test.png"
)

OUTPUT_PATH = Path("ocr_outputs") / f"{IMAGE_PATH.stem}.md"


service = MinerUOcrService()
result = service.parse_file(
    IMAGE_PATH,
    backend="pipeline",
    effort="high",
    parse_method="auto",
    extra_options={
        "start_page_id": 30,
        "end_page_id": 30,
    },
)

if not result.markdown:
    raise RuntimeError("MinerU没有返回Markdown内容。")

OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH.write_text(result.markdown, encoding="utf-8")

print(f"Markdown：{OUTPUT_PATH.resolve()}")
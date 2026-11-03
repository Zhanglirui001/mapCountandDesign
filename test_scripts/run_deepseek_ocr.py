import csv
from pathlib import Path

from count_and_design.application.ai.ocr.screenshot import DeepSeekOcrService


IMAGE_PATH = Path(
    r"test.png"
)
OUTPUT_PATH = Path("ocr_outputs") / f"{IMAGE_PATH.stem}.csv"


service = DeepSeekOcrService()
result = service.recognize(IMAGE_PATH.read_bytes())

print(result.text)

if not result.is_table:
    raise RuntimeError("DeepSeek没有返回表格内容。")

rows = [
    line.split("\t")
    for line in result.text.splitlines()
]

OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
with OUTPUT_PATH.open("w", newline="", encoding="utf-8-sig") as file:
    csv.writer(file).writerows(rows)

print(f"CSV：{OUTPUT_PATH.resolve()}")
print(f"行数：{len(rows)}")

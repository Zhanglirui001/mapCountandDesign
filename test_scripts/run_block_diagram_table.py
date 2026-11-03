from pathlib import Path

from count_and_design.application.ai_statistics.element_recognition import BlockDiagramTableRecognizer

IMAGE_PATH = Path(
    r"image.png"
)
OUTPUT_PATH = Path("ocr_outputs/claude") / f"{IMAGE_PATH.stem}_block_table.json"


recognizer = BlockDiagramTableRecognizer.from_env(".env")
result = recognizer.recognize(IMAGE_PATH)

OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH.write_text(result.to_json(), encoding="utf-8")

print(f"Strategy: {result.strategy}")
print(f"Project type: {result.project_type}")
print(f"Cropped image: {result.cropped_image_path}")
print(f"JSON: {OUTPUT_PATH.resolve()}")

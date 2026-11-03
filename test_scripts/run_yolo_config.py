import json
from pathlib import Path

from count_and_design.application.ai import (
    TRACKER_MODEL_TYPE,
    YoloClient,
    save_yolo_preview,
)


IMAGE_PATHS = [
    Path(
        r"test.png"
    ),
    Path(
        r"test.png"
    ),
]
MODEL_TYPE = TRACKER_MODEL_TYPE
OUTPUT_DIR = Path("detection_outputs")


client = YoloClient.from_defaults(
    confidence=0.2,
    device="cpu",
)

results = client.detect_images(
    IMAGE_PATHS,
    model_type=MODEL_TYPE,
    on_progress=lambda completed, total: print(
        f"检测进度：{completed}/{total}"
    ),
)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
for image_path, result in zip(IMAGE_PATHS, results, strict=True):
    output_stem = f"{image_path.stem}_{MODEL_TYPE}_yolo"
    json_path = OUTPUT_DIR / f"{output_stem}.json"
    preview_path = OUTPUT_DIR / f"{output_stem}_preview.jpg"

    json_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    save_yolo_preview(
        image_path,
        result,
        preview_path,
    )

    print(f"\n图片：{image_path.name}")
    print(f"JSON：{json_path.resolve()}")
    print(f"预览图：{preview_path.resolve()}")
    print(f"检测数量：{len(result.detections)}")

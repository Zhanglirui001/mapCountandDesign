"""Reproduce Agent3 input end-to-end with the real Flat Fork PDF."""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT = Path(r"C:\code\Count_and_Design")
sys.path.insert(0, str(PROJECT / "src"))

PDF = Path(r"C:\code\work\Flat Fork IFC Electrical.pdf")
OUT_DIR = Path(r"C:\code\Count_and_Design\test_scripts\_diag_out")


def banner(title: str) -> None:
    print("\n" + "=" * 12, title, "=" * 12, flush=True)


banner("0. Render page 2 exactly like LazyPageImages does")
from count_and_design.application.ai_statistics.pdf_rendering import (  # noqa: E402
    render_high_resolution_page,
)

OUT_DIR.mkdir(parents=True, exist_ok=True)
start = time.perf_counter()
png_bytes = render_high_resolution_page(PDF, 1, width=6000, height=9000)
out_path = OUT_DIR / "flat_fork_p2_highres.png"
out_path.write_bytes(png_bytes)
print(f"rendered in {time.perf_counter() - start:.2f}s bytes={len(png_bytes)} -> {out_path}", flush=True)

from io import BytesIO  # noqa: E402

from PIL import Image  # noqa: E402
import numpy as np  # noqa: E402

decoded = Image.open(BytesIO(png_bytes)).convert("RGB")
print("decoded size:", decoded.size, flush=True)
arr = np.asarray(decoded.resize((decoded.width // 8, decoded.height // 8)))
print(
    f"pixel stats: mean={float(arr.mean()):.1f} std={float(arr.std()):.1f} "
    f"min={int(arr.min())} max={int(arr.max())}",
    flush=True,
)

banner("1. Project detector path on this exact image")
from count_and_design.application.ai.yolo.runtime import (  # noqa: E402
    BOX_MODEL_TYPE,
    TRACKER_MODEL_TYPE,
    UltralyticsYoloDetector,
)
from count_and_design.application.ai_statistics.element_recognition.service import (  # noqa: E402
    _downscale_for_detection,
)

TRACKER = PROJECT / "src/count_and_design/resources/models/node_tracker_yolov8n.onnx"
BOX = PROJECT / "src/count_and_design/resources/models/box_yolov8n.onnx"

detect_image, scale_x, scale_y = _downscale_for_detection(decoded, 3072)
print(f"detect_image={detect_image.size} scales=({scale_x:.3f},{scale_y:.3f})", flush=True)

for name, model_path, model_type in (
    ("tracker", TRACKER, TRACKER_MODEL_TYPE),
    ("box", BOX, BOX_MODEL_TYPE),
):
    detector = UltralyticsYoloDetector(model_path, source="diag")
    start = time.perf_counter()
    try:
        results = detector.detect(detect_image)
        labels: dict[str, int] = {}
        for item in results:
            labels[item.label] = labels.get(item.label, 0) + 1
        print(
            f"[{name}] elapsed={time.perf_counter() - start:.2f}s "
            f"detections={len(results)} by_label={labels}",
            flush=True,
        )
        for item in results[:5]:
            print(
                f"   {item.label} {item.confidence:.3f} "
                f"({item.box.x1:.0f},{item.box.y1:.0f},{item.box.x2:.0f},{item.box.y2:.0f})"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[{name}] RAISED: {type(exc).__name__}: {exc}", flush=True)

banner("2. Raw ORT letterbox cross-check (tracker model)")
import onnxruntime as ort  # noqa: E402

session = ort.InferenceSession(str(TRACKER), providers=["CPUExecutionProvider"])
tensor_in = session.get_inputs()[0]
height, width = int(tensor_in.shape[2]), int(tensor_in.shape[3])
scale = min(width / decoded.width, height / decoded.height)
new_w, new_h = int(decoded.width * scale), int(decoded.height * scale)
resized = decoded.resize((new_w, new_h), Image.BILINEAR)
canvas = Image.new("RGB", (width, height), (114, 114, 114))
canvas.paste(resized, ((width - new_w) // 2, (height - new_h) // 2))
tensor = np.asarray(canvas, dtype=np.float32) / 255.0
tensor = tensor.transpose(2, 0, 1)[None]
raw = session.run(None, {tensor_in.name: tensor})[0]
preds = raw[0].T
scores = preds[:, 4:].max(axis=1)
mask = scores >= 0.25
classes_above = sorted(set(preds[mask][:, 4:].argmax(axis=1).tolist()))
print(
    f"candidates>=0.25 before NMS: {int(mask.sum())} classes={classes_above} "
    f"max_score={float(scores.max()):.4f}",
    flush=True,
)
for threshold in (0.10, 0.05):
    sub = preds[scores >= threshold][:, 4:].argmax(axis=1)
    print(f"  candidates>= {threshold}: {int((scores >= threshold).sum())}", flush=True)

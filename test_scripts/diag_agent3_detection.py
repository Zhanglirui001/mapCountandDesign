"""Diagnose Agent3 zero-detection issue: project path vs raw ORT path."""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT = Path(r"C:\code\Count_and_Design")
sys.path.insert(0, str(PROJECT / "src"))

IMG = Path(r"C:\code\model_test\Moss Decatur IFC Electrical_p071.png")
TRACKER = PROJECT / "src/count_and_design/resources/models/node_tracker_yolov8n.onnx"


def banner(title: str) -> None:
    print("\n" + "=" * 12, title, "=" * 12, flush=True)


banner("1. Project path: UltralyticsYoloDetector.detect(file path)")
from count_and_design.application.ai.yolo.runtime import (  # noqa: E402
    UltralyticsYoloDetector,
)

detector = UltralyticsYoloDetector(TRACKER, source="diag")
start = time.perf_counter()
try:
    results = detector.detect(IMG)
    print(
        f"elapsed={time.perf_counter() - start:.2f}s detections={len(results)}",
        flush=True,
    )
    for item in results[:8]:
        print(
            "  ",
            item.label,
            round(item.confidence, 3),
            tuple(
                round(value, 1)
                for value in (item.box.x1, item.box.y1, item.box.x2, item.box.y2)
            ),
        )
except Exception as exc:  # noqa: BLE001
    print(
        f"RAISED after {time.perf_counter() - start:.2f}s: "
        f"{type(exc).__name__}: {exc}",
        flush=True,
    )

banner("1b. Same detector, PIL input downscaled like Agent3 service")
from PIL import Image  # noqa: E402

from count_and_design.application.ai_statistics.element_recognition.service import (  # noqa: E402
    _downscale_for_detection,
)

decoded = Image.open(IMG).convert("RGB")
detect_image, scale_x, scale_y = _downscale_for_detection(decoded, 3072)
print(f"decoded={decoded.size} detect_image={detect_image.size} scales=({scale_x:.3f},{scale_y:.3f})", flush=True)
start = time.perf_counter()
try:
    results = detector.detect(detect_image)
    print(f"elapsed={time.perf_counter() - start:.2f}s detections={len(results)}", flush=True)
    for item in results[:8]:
        print(
            "  ",
            item.label,
            round(item.confidence, 3),
            tuple(
                round(value, 1)
                for value in (item.box.x1, item.box.y1, item.box.x2, item.box.y2)
            ),
        )
except Exception as exc:  # noqa: BLE001
    print(
        f"RAISED after {time.perf_counter() - start:.2f}s: "
        f"{type(exc).__name__}: {exc}",
        flush=True,
    )

banner("2. Which imgsz does ultralytics resolve by default?")
from ultralytics import YOLO  # noqa: E402

model = YOLO(str(TRACKER), task="detect")
print("overrides:", model.overrides, flush=True)
start = time.perf_counter()
try:
    output = model.predict(source=str(IMG), conf=0.25, verbose=False)
    result = output[0]
    predictor = getattr(model, "predictor", None)
    print(f"elapsed={time.perf_counter() - start:.2f}s boxes={len(result.boxes)}", flush=True)
    if predictor is not None:
        backend = predictor.model
        print("predictor.args.imgsz =", predictor.args.imgsz, flush=True)
        print("model.format =", getattr(backend, "format", None), flush=True)
        print("hasattr(backend,'imgsz') =", hasattr(backend, "imgsz"), flush=True)
        print("dynamic =", getattr(backend, "dynamic", "?"), flush=True)
except Exception as exc:  # noqa: BLE001
    print(
        f"default-imgsz predict RAISED after {time.perf_counter() - start:.2f}s: "
        f"{type(exc).__name__}: {exc}",
        flush=True,
    )

banner("3. Same YOLO instance, explicit imgsz=2560")
start = time.perf_counter()
try:
    output = model.predict(source=str(IMG), conf=0.25, imgsz=2560, verbose=False)
    result = output[0]
    print(
        f"elapsed={time.perf_counter() - start:.2f}s boxes={len(result.boxes)}",
        flush=True,
    )
    print("names sample:", dict(list(result.names.items())[:6]), flush=True)
    for box in list(result.boxes)[:6]:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        print(
            f"   cls={int(box.cls[0])} conf={float(box.conf[0]):.3f} "
            f"box=({x1:.1f},{y1:.1f},{x2:.1f},{y2:.1f})"
        )
except Exception as exc:  # noqa: BLE001
    print(
        f"imgsz=2560 predict RAISED after {time.perf_counter() - start:.2f}s: "
        f"{type(exc).__name__}: {exc}",
        flush=True,
    )

banner("4. Raw ORT letterbox path (standalone-script equivalent)")
import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402

session = ort.InferenceSession(str(TRACKER), providers=["CPUExecutionProvider"])
tensor_in = session.get_inputs()[0]
height, width = int(tensor_in.shape[2]), int(tensor_in.shape[3])
print("fixed input:", tensor_in.shape, "output:", session.get_outputs()[0].shape, flush=True)

image = Image.open(IMG).convert("RGB")
scale = min(width / image.width, height / image.height)
new_w, new_h = int(image.width * scale), int(image.height * scale)
resized = image.resize((new_w, new_h), Image.BILINEAR)
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

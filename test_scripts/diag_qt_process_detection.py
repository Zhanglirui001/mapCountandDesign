"""Run Agent3 detection inside a Qt-loaded process, mirroring the GUI."""

from __future__ import annotations

import sys
import time
import traceback
from io import BytesIO
from pathlib import Path

PROJECT = Path(r"C:\code\Count_and_Design")
sys.path.insert(0, str(PROJECT / "src"))

print("step 1: importing PySide6 exactly like main.py", flush=True)
from PySide6.QtCore import QStandardPaths  # noqa: E402,F401
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)
print("QApplication created:", app is not None, flush=True)

print("\nstep 2: rendering Flat Fork page 2 like LazyPageImages", flush=True)
from count_and_design.application.ai_statistics.pdf_rendering import (  # noqa: E402
    render_high_resolution_page,
)

png_bytes = render_high_resolution_page(
    Path(r"C:\code\work\Flat Fork IFC Electrical.pdf"),
    1,
    width=6000,
    height=9000,
)
print(f"rendered bytes={len(png_bytes)}", flush=True)

from PIL import Image  # noqa: E402

decoded = Image.open(BytesIO(png_bytes)).convert("RGB")

from count_and_design.application.ai_statistics.element_recognition.service import (  # noqa: E402
    _downscale_for_detection,
)
from count_and_design.application.ai.yolo.client import YoloClient  # noqa: E402

detect_image, scale_x, scale_y = _downscale_for_detection(decoded, 3072)
print(f"detect_image={detect_image.size}", flush=True)

print("\nstep 3: YoloClient.from_defaults() detects inside Qt process", flush=True)
client = YoloClient.from_defaults()
for model_type in ("tracker", "box"):
    start = time.perf_counter()
    try:
        result = client.detect(detect_image, model_type=model_type)
        labels: dict[str, int] = {}
        for item in result.detections:
            labels[item.label] = labels.get(item.label, 0) + 1
        print(
            f"[{model_type}] elapsed={time.perf_counter() - start:.2f}s "
            f"detections={len(result.detections)} by_label={labels} "
            f"model={result.model_path}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[{model_type}] RAISED after {time.perf_counter() - start:.2f}s: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        traceback.print_exc()

print("\nstep 4: warmup() path inside Qt process", flush=True)
try:
    client.warmup()
    print("warmup ok", flush=True)
except Exception as exc:  # noqa: BLE001
    print(f"warmup RAISED: {type(exc).__name__}: {exc}", flush=True)

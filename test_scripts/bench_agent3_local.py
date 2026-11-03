"""Benchmark Agent3's local pipeline: imports, YOLO warmup, steady-state, memory.

Run with the project venv:
    .venv\\Scripts\\python.exe test_scripts\\bench_agent3_local.py
"""

from __future__ import annotations

import ctypes
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "src" / "count_and_design" / "resources" / "models"


def working_set_mb() -> float:
    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb
    )
    return pmc.WorkingSetSize / (1024 * 1024)


def cpu_seconds() -> float:
    """Total process CPU time (user + kernel) in seconds."""
    ft_create = ctypes.c_longlong()
    ft_exit = ctypes.c_longlong()
    ft_kernel = ctypes.c_longlong()
    ft_user = ctypes.c_longlong()
    ctypes.windll.kernel32.GetProcessTimes(
        ctypes.windll.kernel32.GetCurrentProcess(),
        ctypes.byref(ft_create),
        ctypes.byref(ft_exit),
        ctypes.byref(ft_kernel),
        ctypes.byref(ft_user),
    )
    return (ft_kernel.value + ft_user.value) / 1e7


class Step:
    def __init__(self, label: str) -> None:
        self.label = label

    def __enter__(self):
        self.t0 = time.perf_counter()
        self.cpu0 = cpu_seconds()
        self.mem0 = working_set_mb()
        return self

    def __exit__(self, *exc):
        wall = time.perf_counter() - self.t0
        cpu = cpu_seconds() - self.cpu0
        mem = working_set_mb()
        pct = (cpu / wall * 100.0) if wall > 0 else 0.0
        print(
            f"{self.label:<44} wall={wall:8.3f}s  cpu={cpu:8.3f}s "
            f"({pct:5.1f}% of one core)  rss={mem:8.1f}MB (+{mem - self.mem0:7.1f})"
        )
        return False


def make_page_png(width: int = 5400, height: int = 8400) -> bytes:
    """Synthetic 'drawing' page: white background + many black lines."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for index in range(400):
        x = (index * 977) % (width - 20)
        y = (index * 1543) % (height - 20)
        draw.rectangle([x, y, x + 200, y + 60], outline="black", width=3)
        draw.line([x, y, x + 300, y + 400], fill="black", width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> int:
    print(f"python={sys.version.split()[0]}")
    with Step("baseline rss"):
        pass

    with Step("import torch"):
        import torch  # noqa: F401

    with Step("import ultralytics (YOLO)"):
        from ultralytics import YOLO  # noqa: F401

    print(f"torch.version.cuda={torch.version.cuda}")
    print(f"torch.cuda.is_available={torch.cuda.is_available()}")

    import onnxruntime

    print(f"onnxruntime providers={onnxruntime.get_available_providers()}")

    png_bytes = make_page_png()
    print(f"synthetic page PNG bytes={len(png_bytes):,}")

    from PIL import Image

    with Step("decode full page PNG -> RGB bitmap"):
        decoded = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    print(f"decoded size={decoded.size}")

    results = {}
    for name in ("node_tracker_yolov8n.onnx", "box_yolov8n.onnx"):
        path = MODELS / name
        with Step(f"YOLO({name}) model file load"):
            model = YOLO(str(path), task="detect")

        with Step(f"YOLO({name}) FIRST predict (warmup incl.)"):
            model.predict(decoded, conf=0.25, verbose=False)

        for i in range(2):
            with Step(f"YOLO({name}) steady predict #{i + 1}"):
                model.predict(decoded, conf=0.25, verbose=False)

        # What the service actually pays per call including result parsing.
        with Step(f"YOLO({name}) predict + parse detections"):
            res = model.predict(decoded, conf=0.25, verbose=False)
            count = 0
            for r in res:
                boxes = getattr(r, "boxes", None)
                if boxes is not None:
                    count += len(boxes.xyxy.tolist())
            results[name] = count
        print(f"   -> detections above conf: {results[name]}")

    # Two models back-to-back == what one Agent3 page does locally.
    tracker = YOLO(str(MODELS / "node_tracker_yolov8n.onnx"), task="detect")
    box = YOLO(str(MODELS / "box_yolov8n.onnx"), task="detect")
    tracker.predict(decoded, conf=0.25, verbose=False)
    box.predict(decoded, conf=0.25, verbose=False)
    with Step("one page local floor: decode already done + both predicts"):
        tracker.predict(decoded, conf=0.25, verbose=False)
        box.predict(decoded, conf=0.25, verbose=False)

    with Step("re-decode full page (per-page cost in service)"):
        Image.open(io.BytesIO(png_bytes)).convert("RGB")

    print("\nfinal rss:", round(working_set_mb(), 1), "MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

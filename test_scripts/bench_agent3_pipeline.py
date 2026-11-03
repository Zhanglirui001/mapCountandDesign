"""Validate Agent3 optimizations: pipelined wall-time and downscaled detection.

Part 1 drives the REAL Agent3ElementRecognizer with sleep-based fakes to show
cross-page table overlap.
Part 2 benchmarks real YOLO steady-state predict cost on a full-resolution
page versus the shared downscaled copy, plus warmup().
"""

from __future__ import annotations

import io
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402

from count_and_design.application.ai.types import BoundingBox, DetectionResult  # noqa: E402
from count_and_design.application.ai_statistics.element_recognition.service import (  # noqa: E402
    Agent3ElementRecognizer,
)
from count_and_design.application.ai_statistics.element_recognition.contracts import (  # noqa: E402
    ArrayLayoutPageInput,
    ElementRecognitionRequest,
    TrackerDefinition,
)


def working_set_mb() -> float:
    import ctypes

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
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    psapi = ctypes.WinDLL("psapi")
    k32 = ctypes.WinDLL("kernel32")
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
    handle = k32.GetCurrentProcess()
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    handle = k32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
        return -1.0
    return pmc.WorkingSetSize / (1024 * 1024)


def tiny_png(width: int = 480, height: int = 320) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


class _InstantYolo:
    def detect(self, _image, *, model_type, confidence=None):
        del confidence, model_type
        return SimpleNamespace_detections()


def SimpleNamespace_detections():
    from types import SimpleNamespace

    return SimpleNamespace(
        detections=[DetectionResult("Node", 0.95, BoundingBox(0, 0, 180, 300))]
    )


def part1_pipeline(pages: int = 8, table_seconds: float = 0.5) -> None:
    from types import SimpleNamespace

    class _SleepTable:
        def __init__(self) -> None:
            self.calls = 0
            self.max_concurrent = 0
            self._active = 0
            self._lock = threading.Lock()

        def recognize(self, _image):
            with self._lock:
                self.calls += 1
                self._active += 1
                self.max_concurrent = max(self.max_concurrent, self._active)
            time.sleep(table_seconds)
            with self._lock:
                self._active -= 1
            return None

    class _FakeYolo:
        def detect(self, _image, *, model_type, confidence=None):
            del confidence, model_type
            return SimpleNamespace(
                detections=[DetectionResult("Node", 0.95, BoundingBox(0, 0, 180, 300))]
            )

    class _FastOcr:
        def parse_file(self, *_a, **_k):
            time.sleep(0.02)
            return SimpleNamespace(markdown="INV 1001-CBX01")

    table = _SleepTable()
    recognizer = Agent3ElementRecognizer(
        yolo=_FakeYolo(),
        table_recognizer=table,
        node_ocr=_FastOcr(),
        io_workers=8,
        detect_max_side=1920,
    )
    request = ElementRecognitionRequest(
        pages=tuple(ArrayLayoutPageInput(i + 1, tiny_png()) for i in range(pages)),
        tracker_definitions=(TrackerDefinition("2-string", 2),),
        expected_inv_names=("INV 1001",),
    )
    started = time.perf_counter()
    result = recognizer.recognize(request)
    wall = time.perf_counter() - started
    serial_estimate = pages * (table_seconds + 0.02)
    print(
        f"[pipeline] pages={pages} table_latency={table_seconds}s "
        f"wall={wall:.2f}s (serial would be ~{serial_estimate:.2f}s) "
        f"max_table_concurrency={table.max_concurrent} "
        f"nodes={len(result.reviewed_tree.invs[0].nodes)}"
    )


def part2_downscale(reps: int = 3) -> None:
    from ultralytics import YOLO

    models_dir = ROOT / "src" / "count_and_design" / "resources" / "models"
    png_bytes = make_page_png()
    decoded = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    variants = []
    for side in (1920, 3072):
        variants.append(
            (
                f"{side}px",
                decoded.resize(
                    (side, int(side * decoded.height / decoded.width)),
                    Image.Resampling.BILINEAR,
                ),
            )
        )

    tracker = YOLO(str(models_dir / "node_tracker_yolov8n.onnx"), task="detect")
    box = YOLO(str(models_dir / "box_yolov8n.onnx"), task="detect")

    # Absorb cold-start exactly like the new warmup path does.
    t0 = time.perf_counter()
    tracker.predict(Image.new("RGB", (640, 640), "white"), conf=0.25, verbose=False)
    box.predict(Image.new("RGB", (640, 640), "white"), conf=0.25, verbose=False)
    print(f"[warmup] dummy-predict cold-start absorbed in {time.perf_counter() - t0:.2f}s")

    for label, image in (("full-res", decoded), *variants):
        tracker_times, box_times = [], []
        for _ in range(reps):
            t0 = time.perf_counter()
            tracker.predict(image, conf=0.25, verbose=False)
            tracker_times.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            box.predict(image, conf=0.25, verbose=False)
            box_times.append(time.perf_counter() - t0)
        print(
            f"[detect] {label:<9} per-page(both models) "
            f"median={statistics.median(tracker_times) + statistics.median(box_times):.3f}s "
            f"tracker_med={statistics.median(tracker_times):.3f}s "
            f"box_med={statistics.median(box_times):.3f}s"
        )

    print(f"[memory] rss after real-model section={working_set_mb():.0f}MB")


def make_page_png(width: int = 5400, height: int = 8400) -> bytes:
    from PIL import ImageDraw

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for index in range(120):
        x = (index * 977) % (width - 20)
        y = (index * 1543) % (height - 20)
        draw.rectangle([x, y, x + 200, y + 60], outline="black", width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


if __name__ == "__main__":
    print(f"rss baseline={working_set_mb():.0f}MB")
    part1_pipeline()
    part1_pipeline(pages=19, table_seconds=0.5)
    if "--with-models" in sys.argv:
        part2_downscale()
    print("done")

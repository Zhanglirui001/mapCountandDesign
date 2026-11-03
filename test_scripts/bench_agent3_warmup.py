"""Focused benchmark: import/warmup costs, RSS growth, CPU utilization."""

from __future__ import annotations

import ctypes
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "src" / "count_and_design" / "resources" / "models"


class Mem:
    def __init__(self) -> None:
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

        self._pmc_type = PMC
        self._pmc = PMC()
        self._pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        self._handle = k32.GetCurrentProcess()
        self._get = psapi.GetProcessMemoryInfo
        self._get.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
        self._get.restype = ctypes.c_int

    def ws_mb(self) -> float:
        if not self._get(self._handle, ctypes.byref(self._pmc), self._pmc.cb):
            return -1.0
        return self._pmc.WorkingSetSize / (1024 * 1024)

    def peak_mb(self) -> float:
        if not self._get(self._handle, ctypes.byref(self._pmc), self._pmc.cb):
            return -1.0
        return self._pmc.PeakWorkingSetSize / (1024 * 1024)


def report(label: str, t0: float, cpu0: float, mem0: float) -> None:
    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0
    mem = MEM.ws_mb()
    pct = (cpu / wall * 100.0) if wall > 0 else 0.0
    print(
        f"{label:<46} wall={wall:7.3f}s cpu={cpu:7.3f}s ({pct:5.1f}% core) "
        f"rss={mem:7.1f}MB (+{mem - mem0:7.1f}) peak={MEM.peak_mb():7.1f}MB"
    )


def step(label: str) -> None:
    global _T0, _C0, _M0
    report(f"[end] {PREV[0]}", _T0, _C0, _M0)
    _T0, _C0, _M0 = time.perf_counter(), time.process_time(), MEM.ws_mb()
    PREV[0] = label


MEM = Mem()
_T0 = _C0 = _M0 = 0.0
PREV = ["start"]
_T0, _C0, _M0 = time.perf_counter(), time.process_time(), MEM.ws_mb()
print(f"baseline rss={_M0:.1f}MB")


def make_page_png(width: int = 5400, height: int = 8400) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for index in range(120):
        x = (index * 977) % (width - 20)
        y = (index * 1543) % (height - 20)
        draw.rectangle([x, y, x + 200, y + 60], outline="black", width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


step("import PIL")
from PIL import Image

png_bytes = make_page_png()
step("make 5400x8400 synthetic page PNG")
decoded = Image.open(io.BytesIO(png_bytes)).convert("RGB")
step("decode page PNG")

import torch  # noqa: F401

step("import torch")
import ultralytics  # noqa: F401
from ultralytics import YOLO

step("import ultralytics")

tracker = YOLO(str(MODELS / "node_tracker_yolov8n.onnx"), task="detect")
step("YOLO(tracker) constructor (lazy, no session)")
tracker.predict(decoded, conf=0.25, verbose=False)
step("YOLO(tracker) FIRST predict (session+warmup)")
tracker.predict(decoded, conf=0.25, verbose=False)
step("YOLO(tracker) second predict")

box = YOLO(str(MODELS / "box_yolov8n.onnx"), task="detect")
box.predict(decoded, conf=0.25, verbose=False)
step("YOLO(box) FIRST predict")
box.predict(decoded, conf=0.25, verbose=False)
step("YOLO(box) second predict")

# Half-resolution variant: what the network actually sees anyway.
small = decoded.copy()
small.thumbnail((1920, 1920))
step("downscale page to 1920px")
tracker.predict(small, conf=0.25, verbose=False)
step("YOLO(tracker) predict on 1920px copy")
box.predict(small, conf=0.25, verbose=False)
step("YOLO(box) predict on 1920px copy")

report("[end] last step", _T0, _C0, _M0)
print("done")

"""PDFium-backed rendering used by Agent1 and its GUI previews."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import sys
from threading import Lock
from time import perf_counter

import pypdfium2 as pdfium
from PIL import Image


# PDFium is not thread-safe, even for distinct documents. This lock protects
# callers in one process. Process workers each get an independent lock and
# PDFium address space, so they can safely render in parallel.
_RENDER_LOCK = Lock()
_WORKER_DOCUMENT: pdfium.PdfDocument | None = None

THUMBNAIL_WIDTH = 320
PAGE_RENDER_WIDTH = 6000
PAGE_RENDER_HEIGHT = 9000
CLASSIFICATION_MAX_SIZE = 1080
CLASSIFICATION_HEIGHT = 720


@dataclass(frozen=True)
class RenderMetrics:
    """Timing and process-memory data for one rendered page."""

    pdfium_seconds: float
    encode_seconds: float
    total_seconds: float
    working_set_mb: float
    peak_working_set_mb: float


@dataclass(frozen=True)
class RenderedClassificationPage:
    """Small Agent1 assets safe to return through process IPC."""

    page_index: int
    classification_png: bytes
    thumbnail_png: bytes
    width: int
    height: int
    metrics: RenderMetrics


@dataclass(frozen=True)
class RenderedPdfPage:
    """Compatibility result without the removed fixed-canvas legacy PNG."""

    page_png: bytes
    classification_png: bytes
    thumbnail_png: bytes
    metrics: RenderMetrics


class PdfRenderingError(RuntimeError):
    """Raised when PDFium cannot open or render a drawing page."""


def pdf_page_count(path: str | Path) -> int:
    try:
        with _RENDER_LOCK:
            document = pdfium.PdfDocument(str(path))
            count = len(document)
            document.close()
        return count
    except Exception as exc:
        raise PdfRenderingError(f"Unable to open PDF: {exc}") from exc


def initialize_pdf_render_worker(path: str) -> None:
    """Open one document per spawned renderer process."""

    global _WORKER_DOCUMENT
    if _WORKER_DOCUMENT is not None:
        _WORKER_DOCUMENT.close()
    _WORKER_DOCUMENT = pdfium.PdfDocument(path)


def render_classification_page_worker(
    page_index: int,
    classification_max_size: int = CLASSIFICATION_MAX_SIZE,
    thumbnail_width: int = THUMBNAIL_WIDTH,
) -> RenderedClassificationPage:
    """ProcessPool entry point; return only compact Agent1 assets."""

    if _WORKER_DOCUMENT is None:
        raise PdfRenderingError("PDF render worker was not initialized.")
    return _render_classification_from_document(
        _WORKER_DOCUMENT,
        page_index,
        classification_max_size=classification_max_size,
        thumbnail_width=thumbnail_width,
    )


def render_high_resolution_page_worker(
    page_index: int,
    width: int = PAGE_RENDER_WIDTH,
    height: int = PAGE_RENDER_HEIGHT,
) -> tuple[int, bytes]:
    """ProcessPool entry point for an aspect-ratio-preserving PNG.

    Agent1 and Agent3 share the same process initializer so every worker owns
    one PDFium document and address space. Returning the page index keeps
    out-of-order ``as_completed`` consumers trivial.
    """

    if _WORKER_DOCUMENT is None:
        raise PdfRenderingError("PDF render worker was not initialized.")
    if width <= 0 or height <= 0:
        raise ValueError("Render width and height must be positive.")
    image = _render_page_image(_WORKER_DOCUMENT, page_index, width, height)
    output = BytesIO()
    image.save(output, format="PNG")
    image.close()
    return page_index, output.getvalue()


def render_classification_page(
    path: str | Path,
    page_index: int,
    *,
    classification_max_size: int = CLASSIFICATION_MAX_SIZE,
    thumbnail_width: int = THUMBNAIL_WIDTH,
) -> RenderedClassificationPage:
    """Render directly at Agent1 resolution in the current process."""

    try:
        with _RENDER_LOCK:
            document = pdfium.PdfDocument(str(path))
            try:
                return _render_classification_from_document(
                    document,
                    page_index,
                    classification_max_size=classification_max_size,
                    thumbnail_width=thumbnail_width,
                )
            finally:
                document.close()
    except PdfRenderingError:
        raise
    except Exception as exc:
        raise PdfRenderingError(
            f"Unable to render PDF page {page_index + 1}: {exc}"
        ) from exc


def render_high_resolution_page(
    path: str | Path,
    page_index: int,
    *,
    width: int = PAGE_RENDER_WIDTH,
    height: int = PAGE_RENDER_HEIGHT,
) -> bytes:
    """Render one aspect-ratio-preserving high-resolution PNG on demand."""

    if width <= 0 or height <= 0:
        raise ValueError("Render width and height must be positive.")
    try:
        with _RENDER_LOCK:
            document = pdfium.PdfDocument(str(path))
            try:
                image = _render_page_image(document, page_index, width, height)
            finally:
                document.close()
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except PdfRenderingError:
        raise
    except Exception as exc:
        raise PdfRenderingError(
            f"Unable to render PDF page {page_index + 1}: {exc}"
        ) from exc


def render_pdf_page_with_thumbnail(
    path: str | Path,
    page_index: int,
    *,
    width: int = PAGE_RENDER_WIDTH,
    height: int = PAGE_RENDER_HEIGHT,
    thumbnail_width: int = THUMBNAIL_WIDTH,
) -> tuple[bytes, bytes]:
    """Render one high-resolution page and a preview thumbnail."""

    rendered = render_pdf_page_assets(
        path,
        page_index,
        width=width,
        height=height,
        thumbnail_width=thumbnail_width,
    )
    return rendered.page_png, rendered.thumbnail_png


def render_pdf_page_assets(
    path: str | Path,
    page_index: int,
    *,
    width: int = PAGE_RENDER_WIDTH,
    height: int = PAGE_RENDER_HEIGHT,
    classification_max_size: int = CLASSIFICATION_MAX_SIZE,
    thumbnail_width: int = THUMBNAIL_WIDTH,
) -> RenderedPdfPage:
    """Derive all assets for regression and compatibility callers.

    Agent1 no longer calls this function. The fixed-size white canvas has
    been removed; only the native-aspect high-resolution PNG is retained.
    """

    started = perf_counter()
    page_png = render_high_resolution_page(
        path,
        page_index,
        width=width,
        height=height,
    )
    high_res_done = perf_counter()
    with Image.open(BytesIO(page_png)) as source:
        classification = source.copy()
    classification = _classification_canvas(classification, classification_max_size)
    classification_output = BytesIO()
    classification.save(classification_output, format="PNG")
    thumbnail = classification.copy()
    thumbnail.thumbnail((thumbnail_width, thumbnail_width))
    thumbnail_output = BytesIO()
    thumbnail.save(thumbnail_output, format="PNG")
    finished = perf_counter()
    working, peak = process_memory_mb()
    return RenderedPdfPage(
        page_png=page_png,
        classification_png=classification_output.getvalue(),
        thumbnail_png=thumbnail_output.getvalue(),
        metrics=RenderMetrics(
            pdfium_seconds=high_res_done - started,
            encode_seconds=finished - high_res_done,
            total_seconds=finished - started,
            working_set_mb=working,
            peak_working_set_mb=peak,
        ),
    )


def placeholder_thumbnail_png(width: int = THUMBNAIL_WIDTH) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, width), (226, 232, 240)).save(output, format="PNG")
    return output.getvalue()


def process_memory_mb() -> tuple[float, float]:
    """Return current and peak process working set when available."""

    if sys.platform == "win32":
        return _windows_process_memory_mb()
    try:
        import resource

        peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        peak /= 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
        return 0.0, peak
    except Exception:
        return 0.0, 0.0


def _render_classification_from_document(
    document: pdfium.PdfDocument,
    page_index: int,
    *,
    classification_max_size: int,
    thumbnail_width: int,
) -> RenderedClassificationPage:
    if classification_max_size <= 0 or thumbnail_width <= 0:
        raise ValueError("Derived PNG sizes must be positive.")
    started = perf_counter()
    image = _render_page_image(
        document,
        page_index,
        classification_max_size,
        round(classification_max_size * CLASSIFICATION_HEIGHT / CLASSIFICATION_MAX_SIZE),
    )
    pdfium_done = perf_counter()
    image = _classification_canvas(image, classification_max_size)
    classification_output = BytesIO()
    image.save(classification_output, format="PNG")
    thumbnail = image.copy()
    thumbnail.thumbnail((thumbnail_width, thumbnail_width))
    thumbnail_output = BytesIO()
    thumbnail.save(thumbnail_output, format="PNG")
    finished = perf_counter()
    working, peak = process_memory_mb()
    return RenderedClassificationPage(
        page_index=page_index,
        classification_png=classification_output.getvalue(),
        thumbnail_png=thumbnail_output.getvalue(),
        width=image.width,
        height=image.height,
        metrics=RenderMetrics(
            pdfium_seconds=pdfium_done - started,
            encode_seconds=finished - pdfium_done,
            total_seconds=finished - started,
            working_set_mb=working,
            peak_working_set_mb=peak,
        ),
    )


def _classification_canvas(image: Image.Image, width: int) -> Image.Image:
    """Fit the full page on the reference library's 1080 by 720 white canvas."""
    if width <= 0:
        raise ValueError("Classification width must be positive.")
    height = max(1, round(width * CLASSIFICATION_HEIGHT / CLASSIFICATION_MAX_SIZE))
    image.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), "white")
    canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
    image.close()
    return canvas


def _render_page_image(
    document: pdfium.PdfDocument,
    page_index: int,
    width: int,
    height: int,
) -> Image.Image:
    if not 0 <= page_index < len(document):
        raise PdfRenderingError(f"Invalid PDF page index: {page_index}")
    page = document[page_index]
    try:
        page_width, page_height = page.get_size()
        scale = min(
            width / max(page_width, 1.0),
            height / max(page_height, 1.0),
        )
        bitmap = page.render(scale=scale)
        try:
            return bitmap.to_pil()
        finally:
            bitmap.close()
    finally:
        page.close()


def _windows_process_memory_mb() -> tuple[float, float]:
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    get_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    get_memory_info.restype = wintypes.BOOL
    if not get_memory_info(
        get_current_process(), ctypes.byref(counters), counters.cb
    ):
        return 0.0, 0.0
    scale = float(2**20)
    return (
        counters.WorkingSetSize / scale,
        counters.PeakWorkingSetSize / scale,
    )

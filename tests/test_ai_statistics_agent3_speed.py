"""Regression tests for Agent3 page-selection observability and speed.

Covers the fixes for two reported problems:
1. Entering Element Recognition rendered every selected Array Layout page at
   high resolution on the GUI thread (Mapping membership/get traps), which
   looked like "the whole document is being processed".
2. Inference was slow with low CPU because each Node OCR re-decoded the full
   page PNG and awaited every network round trip sequentially.
"""

from __future__ import annotations

import threading
import time
from io import BytesIO
from types import SimpleNamespace

import pytest

from PIL import Image

from count_and_design.application.ai.types import DetectionResult, BoundingBox
from count_and_design.application.ai_statistics.element_recognition import (
    Agent3ElementRecognizer,
    ArrayLayoutPageInput,
    ElementPageResult,
    ElementRecognitionRequest,
    TrackerDefinition,
)
from count_and_design.application.ai_statistics.models import (
    InvDraft,
    LazyPageImages,
    NodeDraft,
)


def _tiny_png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (480, 320), "white").save(output, format="PNG")
    return output.getvalue()


class _RecordingLazyImages(LazyPageImages):
    """Lazy mapping that records every high-resolution render."""

    def __init__(self, page_count: int) -> None:
        super().__init__(
            "unused.pdf",
            page_count,
            width=100,
            height=100,
        )
        self.rendered: list[int] = []

    def __getitem__(self, page_number: int) -> bytes:
        self.rendered.append(page_number)
        return _tiny_png()


def test_lazy_page_images_membership_does_not_render() -> None:
    lazy = _RecordingLazyImages(12)

    assert 3 in lazy
    assert 99 not in lazy
    assert "7" in lazy  # type: ignore[operator]
    assert lazy.rendered == []

    # Availability probes used by the workflow stay render-free.
    assert lazy.cached_page_count == 0


def test_lazy_page_images_get_renders_only_requested_page() -> None:
    lazy = _RecordingLazyImages(12)

    assert lazy.get(5) == _tiny_png()
    assert lazy.get(99) is None
    assert lazy.get("bad") is None  # type: ignore[argument]
    assert lazy.rendered == [5]


def test_element_request_metadata_never_touches_page_images(tmp_path) -> None:
    from count_and_design.application.ai_statistics import (
        AIStatisticsStage,
        AIStatisticsStageStatus,
        AIStatisticsWorkflow,
        PageClassification,
        PageClassificationResult,
        DrawingPageType,
    )

    workflow = AIStatisticsWorkflow()
    workflow.load_pdf(tmp_path / "drawing.pdf", 4)
    lazy = _RecordingLazyImages(4)
    pages = tuple(
        PageClassification(
            page_number=number,
            page_type=(
                DrawingPageType.ARRAY_LAYOUT.value
                if number in {2, 4}
                else DrawingPageType.OTHER.value
            ),
        )
        for number in range(1, 5)
    )
    workflow._snapshot = type(workflow._snapshot)(
        pdf_path=tmp_path / "drawing.pdf",
        page_count=4,
        furthest_stage=AIStatisticsStage.ELEMENT_RECOGNITION,
        selected_stage=AIStatisticsStage.ELEMENT_RECOGNITION,
        stage_statuses=tuple(
            status if index != 2 else AIStatisticsStageStatus.WAITING
            for index, status in enumerate(
                [AIStatisticsStageStatus.CONFIRMED] * 4
            )
        ),
        page_classification=PageClassificationResult(pages=pages, page_images=lazy),
    )
    workflow.configure_element_recognition((TrackerDefinition("2-string", 2),))

    metadata = workflow.element_recognition_request_metadata
    assert metadata is not None
    assert [page.page_number for page in metadata.pages] == [2, 4]
    assert all(page.png_bytes == b"" for page in metadata.pages)
    # GUI-thread enablement checks must stay render-free too.
    assert workflow.is_element_recognition_run_enabled()
    assert lazy.rendered == []


def test_agent3_node_ocr_requests_run_concurrently(monkeypatch) -> None:
    # Two Nodes are read concurrently, each in its original orientation only.
    barrier = threading.Barrier(2, timeout=10)
    encoding_barrier = threading.Barrier(2, timeout=5)
    from count_and_design.application.ai_statistics.element_recognition.node_label import NodeNameRecognizer
    original_encode = NodeNameRecognizer.encode_crop

    def encode(decoded, bbox):
        encoding_barrier.wait()
        return original_encode(decoded, bbox)

    monkeypatch.setattr(NodeNameRecognizer, "encode_crop", staticmethod(encode))

    class _FakeYolo:
        def detect(self, _image, *, model_type, confidence=None):
            del confidence
            detections = [
                DetectionResult("Node", 0.95, BoundingBox(0, 0, 180, 300)),
                DetectionResult("Node", 0.94, BoundingBox(220, 0, 400, 300)),
            ]
            if model_type != "tracker":
                detections.append(
                    DetectionResult("Box", 0.92, BoundingBox(190, 85, 210, 115))
                )
            return SimpleNamespace(detections=detections)

    class _ParallelOcr:
        """OCR fake that only succeeds when requests overlap in pairs."""

        def __init__(self) -> None:
            self.calls = 0
            self.lock = threading.Lock()

        def parse_file(self, *_args, **_kwargs):
            with self.lock:
                self.calls += 1
            barrier.wait()
            return SimpleNamespace(markdown="INV 1001-CBX01")

    class _NoTable:
        def recognize(self, _image):
            return None

    ocr = _ParallelOcr()
    recognizer = Agent3ElementRecognizer(
        yolo=_FakeYolo(),
        table_recognizer=_NoTable(),
        node_ocr=ocr,
        io_workers=2,
    )
    result = recognizer.recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(1, _tiny_png()),),
            tracker_definitions=(TrackerDefinition("2-string", 2),),
            expected_inv_names=("INV 1001",),
        )
    )
    assert ocr.calls == 2
    node = result.reviewed_tree.invs[0].nodes[0]
    # Without a table result the OCR text itself names the node.
    assert "CBX01" in node.name


def test_agent3_logs_requested_pages(caplog) -> None:
    class _FakeYolo:
        def detect(self, _image, *, model_type, confidence=None):
            del confidence
            detections = [
                DetectionResult("Node", 0.95, BoundingBox(0, 0, 180, 300)),
            ]
            if model_type != "tracker":
                detections.append(
                    DetectionResult("Box", 0.92, BoundingBox(190, 85, 210, 115))
                )
            return SimpleNamespace(detections=detections)

    class _NoTable:
        def recognize(self, _image):
            return None

    class _EmptyOcr:
        def parse_file(self, *_args, **_kwargs):
            return SimpleNamespace(markdown="")

    import logging

    from count_and_design.application.ai_statistics.element_recognition.service import (
        logger as agent3_logger,
    )

    recognizer = Agent3ElementRecognizer(
        yolo=_FakeYolo(),
        table_recognizer=_NoTable(),
        node_ocr=_EmptyOcr(),
        io_workers=2,
    )
    with caplog.at_level(logging.INFO, logger=agent3_logger.name):
        recognizer.recognize(
            ElementRecognitionRequest(
                pages=(
                    ArrayLayoutPageInput(9, _tiny_png()),
                    ArrayLayoutPageInput(3, _tiny_png()),
                ),
                tracker_definitions=(TrackerDefinition("2-string", 2),),
            )
        )

    start_records = [
        record for record in caplog.records if record.getMessage().startswith("agent3.start")
    ]
    assert start_records, "Agent3 must log the exact page scope of a run"
    assert "pages=[3, 9]" in start_records[0].getMessage()


def test_agent3_downscales_detection_input_and_rescales_boxes() -> None:
    """Both models share one downscaled bitmap; boxes map back to the page."""

    import threading

    class _SizeRecordingYolo:
        def __init__(self) -> None:
            self.sizes: list[tuple[int, int]] = []
            self.lock = threading.Lock()

        def detect(self, image, *, model_type, confidence=None):
            del confidence, model_type
            with self.lock:
                self.sizes.append((image.width, image.height))
            # Fixed box in whatever coordinate space the model received.
            return SimpleNamespace(
                detections=[DetectionResult("Node", 0.9, BoundingBox(10, 10, 50, 50))]
            )

    class _NoTable:
        def recognize(self, _image):
            return None

    class _EmptyOcr:
        def parse_file(self, *_args, **_kwargs):
            return SimpleNamespace(markdown="")

    yolo = _SizeRecordingYolo()
    page_png = BytesIO()
    Image.new("RGB", (800, 600), "white").save(page_png, format="PNG")
    recognizer = Agent3ElementRecognizer(
        yolo=yolo,
        table_recognizer=_NoTable(),
        node_ocr=_EmptyOcr(),
        detect_max_side=100,
    )
    result = recognizer.recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(1, page_png.getvalue()),),
            tracker_definitions=(TrackerDefinition("2-string", 2),),
        )
    )

    # Both models ran on one shared 100x75 copy of the 800x600 page...
    assert yolo.sizes == [(100, 75), (100, 75)]
    # ...and the reported detection is back in original page coordinates.
    box = result.pages[0].detections[0].bbox
    assert (box.x1, box.y1, box.x2, box.y2) == (80.0, 80.0, 400.0, 400.0)
    assert (result.pages[0].image_width, result.pages[0].image_height) == (800, 600)


def test_agent3_table_requests_run_concurrently_across_pages() -> None:
    """Full-page table parses are queued up front, not one-per-page serial."""

    import threading

    pages = (1, 2, 3)
    barrier = threading.Barrier(len(pages), timeout=15)

    class _BlockingTable:
        def recognize(self, _image):
            barrier.wait()
            return None

    class _FakeYolo:
        def detect(self, _image, *, model_type, confidence=None):
            del confidence, model_type
            return SimpleNamespace(
                detections=[DetectionResult("Node", 0.95, BoundingBox(0, 0, 180, 300))]
            )

    class _EmptyOcr:
        def parse_file(self, *_args, **_kwargs):
            return SimpleNamespace(markdown="")

    recognizer = Agent3ElementRecognizer(
        yolo=_FakeYolo(),
        table_recognizer=_BlockingTable(),
        node_ocr=_EmptyOcr(),
        io_workers=8,
    )
    result = recognizer.recognize(
        ElementRecognitionRequest(
            pages=tuple(ArrayLayoutPageInput(number, _tiny_png()) for number in pages),
            tracker_definitions=(TrackerDefinition("2-string", 2),),
        )
    )

    assert not any(
        issue.code == "node_string_table_failed" for issue in result.all_issues
    ), "every page's table request must have run concurrently"


def test_agent3_node_ocr_skips_formula_and_image_analysis() -> None:
    """Node-name crops disable MinerU features they never need."""

    captured: dict = {}

    class _FakeYolo:
        def detect(self, _image, *, model_type, confidence=None):
            del confidence, model_type
            return SimpleNamespace(
                detections=[DetectionResult("Node", 0.95, BoundingBox(0, 0, 180, 300))]
            )

    class _NoTable:
        def recognize(self, _image):
            return None

    class _CapturingOcr:
        def parse_file(self, *_args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(markdown="INV 1001-CBX01")

    recognizer = Agent3ElementRecognizer(
        yolo=_FakeYolo(),
        table_recognizer=_NoTable(),
        node_ocr=_CapturingOcr(),
    )
    recognizer.recognize(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(1, _tiny_png()),),
            tracker_definitions=(TrackerDefinition("2-string", 2),),
        )
    )

    assert captured["formula_enable"] is False
    assert captured["image_analysis"] is False
    assert captured["table_enable"] is False


def test_agent3_default_node_names_use_qwen_in_original_orientation() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=object(),
        table_recognizer=object(),
    )

    assert recognizer._node_ocr is None
    assert recognizer._node_qwen is None
    assert recognizer._node_ocr_angles == (0,)


def test_agent3_defaults_to_32_node_qwen_workers(monkeypatch) -> None:
    monkeypatch.delenv("AGENT3_IO_WORKERS", raising=False)
    recognizer = Agent3ElementRecognizer(
        yolo=object(),
        table_recognizer=object(),
    )

    assert recognizer._io_workers == 32


def test_agent3_grayscale_converts_color_to_luminance() -> None:
    from count_and_design.application.ai_statistics.element_recognition.service import (
        encode_agent3_page_png,
    )

    output = BytesIO()
    Image.new("RGB", (2, 2), (255, 0, 0)).save(output, format="PNG")
    png_bytes = output.getvalue()

    color = encode_agent3_page_png(png_bytes, grayscale=False)
    gray = encode_agent3_page_png(png_bytes, grayscale=True)

    with Image.open(BytesIO(color)) as color_image:
        assert color_image.getpixel((0, 0)) == (255, 0, 0)
    with Image.open(BytesIO(gray)) as gray_image:
        pixel = gray_image.getpixel((0, 0))
        assert pixel[0] == pixel[1] == pixel[2] == 76


def test_agent3_grayscale_enabled_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AGENT3_GRAYSCALE_IMAGES", "true")
    recognizer = Agent3ElementRecognizer(
        yolo=object(),
        table_recognizer=object(),
    )

    assert recognizer._grayscale is True


def test_agent3_grayscale_enabled_via_constructor() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=object(),
        table_recognizer=object(),
        grayscale=True,
    )

    assert recognizer._grayscale is True


def test_agent3_line_enhance_darkens_colored_fill_lines() -> None:
    from count_and_design.application.ai_statistics.element_recognition.service import (
        encode_agent3_page_png,
    )

    output = BytesIO()
    Image.new("RGB", (48, 48), (180, 210, 240)).save(output, format="PNG")
    # Draw a darker vertical line through the pale blue fill.
    image = Image.open(BytesIO(output.getvalue())).convert("RGB")
    for y in range(48):
        image.putpixel((24, y), (40, 60, 90))
    colored = BytesIO()
    image.save(colored, format="PNG")
    png_bytes = colored.getvalue()
    image.close()

    enhanced = encode_agent3_page_png(png_bytes, line_enhance=True)
    with Image.open(BytesIO(enhanced)) as result:
        center = result.getpixel((24, 24))
        fill = result.getpixel((8, 8))
        assert center[0] == center[1] == center[2]
        assert fill[0] == fill[1] == fill[2]
        assert center[0] < fill[0]


def test_agent3_line_enhance_enabled_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AGENT3_LINE_ENHANCE", "true")
    recognizer = Agent3ElementRecognizer(
        yolo=object(),
        table_recognizer=object(),
    )

    assert recognizer._line_enhance is True


def test_agent3_line_enhance_enabled_via_constructor() -> None:
    recognizer = Agent3ElementRecognizer(
        yolo=object(),
        table_recognizer=object(),
        line_enhance=True,
    )

    assert recognizer._line_enhance is True


def test_agent3_display_stays_color_while_recognition_is_grayscale() -> None:
    """UI keeps the color original; recognition work copy is grayscale."""

    from count_and_design.application.ai_statistics.element_recognition.service import (
        encode_agent3_page_png,
    )

    output = BytesIO()
    Image.new("RGB", (32, 24), (180, 210, 240)).save(output, format="PNG")
    png_bytes = output.getvalue()

    display = encode_agent3_page_png(png_bytes, grayscale=False)
    recognize = encode_agent3_page_png(display, grayscale=True)
    with Image.open(BytesIO(display)) as image:
        assert image.getpixel((0, 0)) == (180, 210, 240)
    with Image.open(BytesIO(recognize)) as image:
        pixel = image.getpixel((0, 0))
        assert pixel[0] == pixel[1] == pixel[2]


def test_agent3_bounds_full_page_workers(monkeypatch, tmp_path) -> None:
    # Constructor config loading must not restore a developer's .env override
    # after delenv; this test checks the code default, not local settings.
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "unused.env"))
    monkeypatch.delenv("AGENT3_PAGE_WORKERS", raising=False)
    active = 0
    max_active = 0
    lock = threading.Lock()

    class _BoundedRecognizer(Agent3ElementRecognizer):
        def _recognize_table(self, _page, _on_step=None):
            return None

        def _recognize_page(self, page, *_args):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return (
                ElementPageResult(
                    page_number=page.page_number,
                    inv_name=f"INV {page.page_number}",
                    image_width=480,
                    image_height=320,
                ),
                InvDraft(
                    name=f"INV {page.page_number}",
                    nodes=(NodeDraft(name=f"Node {page.page_number}"),),
                ),
                "",
                {},
            )

    class _ParallelismYolo:
        def __init__(self) -> None:
            self.configured: list[int] = []

        def configure_parallelism(self, page_count: int):
            self.configured.append(page_count)
            return (page_count, 1)

        def warmup(self) -> None:
            pass

    yolo = _ParallelismYolo()
    recognizer = _BoundedRecognizer(
        yolo=yolo,
        table_recognizer=object(),
        node_ocr=object(),
    )
    pages = tuple(
        ArrayLayoutPageInput(page_number, _tiny_png())
        for page_number in range(1, 7)
    )
    recognizer.warmup(page_count=len(pages))
    recognizer.recognize(
        ElementRecognitionRequest(
            pages=pages,
            tracker_definitions=(TrackerDefinition("2-string", 2),),
        )
    )

    assert recognizer._page_workers == 2
    assert max_active == 2
    assert yolo.configured == [4, 4]  # Two independent models per active page.


def test_agent3_cancel_waits_for_started_provider_tasks() -> None:
    table_started = threading.Event()
    release_table = threading.Event()
    cancelled = threading.Event()
    errors = []

    class _BlockingTable:
        def recognize(self, _image):
            table_started.set()
            assert release_table.wait(10)
            return None

    class _NoDetections:
        def detect(self, _image, *, model_type, confidence=None):
            del model_type, confidence
            return SimpleNamespace(detections=[])

    recognizer = Agent3ElementRecognizer(
        yolo=_NoDetections(),
        table_recognizer=_BlockingTable(),
        node_ocr=object(),
        page_workers=1,
    )

    def run() -> None:
        try:
            recognizer.recognize(
                ElementRecognitionRequest(
                    pages=(ArrayLayoutPageInput(1, _tiny_png()),),
                    tracker_definitions=(TrackerDefinition("2-string", 2),),
                ),
                is_cancelled=cancelled.is_set,
            )
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert table_started.wait(10)
    cancelled.set()
    time.sleep(0.05)

    assert worker.is_alive(), "Agent3 returned while a provider task was still running"
    release_table.set()
    worker.join(10)
    assert not worker.is_alive()
    assert errors and errors[0].__class__.__name__ == "Agent3Cancelled"


def test_agent3_stream_starts_first_page_before_renderer_finishes() -> None:
    """Rendered pages enter detection immediately and overlap later-page work."""

    first_tracker_started = threading.Event()
    second_tracker_started = threading.Event()
    box_finished: set[int] = set()
    progress: list[tuple[int, int]] = []

    def page_png(page_number: int) -> bytes:
        output = BytesIO()
        Image.new("RGB", (480, 320), (page_number, 0, 0)).save(output, format="PNG")
        return output.getvalue()

    class _ParallelYolo:
        def detect(self, image, *, model_type, confidence=None):
            del confidence
            page_number = image.getpixel((0, 0))[0]
            if model_type == "tracker":
                if page_number == 1:
                    first_tracker_started.set()
                else:
                    second_tracker_started.set()
                detections = [
                    DetectionResult("Node", 0.95, BoundingBox(0, 0, 180, 300))
                ]
            else:
                box_finished.add(page_number)
                detections = [
                    DetectionResult("Box", 0.92, BoundingBox(80, 80, 100, 100))
                ]
            return SimpleNamespace(detections=detections)

    class _MinerU:
        def parse_file(self, _image, *, filename, **_kwargs):
            page_number = 1 if "page-1-" in filename else 2
            assert page_number in box_finished, "Box detection must precede Node OCR"
            if page_number == 1:
                assert second_tracker_started.wait(10), (
                    "page 2 YOLO must overlap page 1's OCR wait"
                )
            return SimpleNamespace(markdown=f"INV 100{page_number}-CBX01")

    class _NoTable:
        def recognize(self, _image):
            return None

    def rendered_pages():
        yield ArrayLayoutPageInput(1, page_png(1))
        assert first_tracker_started.wait(10), (
            "page 1 detection must start before the renderer yields page 2"
        )
        yield ArrayLayoutPageInput(2, page_png(2))

    recognizer = Agent3ElementRecognizer(
        yolo=_ParallelYolo(),
        table_recognizer=_NoTable(),
        node_ocr=_MinerU(),
        io_workers=4,
        node_ocr_orientations=1,
    )
    result = recognizer.recognize_stream(
        ElementRecognitionRequest(
            pages=(ArrayLayoutPageInput(1, b""), ArrayLayoutPageInput(2, b"")),
            tracker_definitions=(TrackerDefinition("2-string", 2),),
        ),
        rendered_pages(),
        on_progress=lambda completed, total: progress.append((completed, total)),
    )

    assert [page.page_number for page in result.pages] == [1, 2]
    assert progress == [(value, 10) for value in range(1, 11)]


@pytest.mark.parametrize("blocked_stage", ["table", "ocr", "review"])
def test_provider_wait_releases_page_worker(blocked_stage, monkeypatch):
    """Even one page worker can feed later pages while an earlier provider waits."""
    last_page_detected = threading.Event()
    provider_started = threading.Event()
    failures = []
    decoded_pages = []
    from count_and_design.application.ai_statistics.element_recognition.node_label import NodeNameRecognizer
    original_encode = NodeNameRecognizer.encode_crop

    def encode(decoded, bbox):
        decoded_pages.append(decoded)
        return original_encode(decoded, bbox)

    monkeypatch.setattr(NodeNameRecognizer, "encode_crop", staticmethod(encode))

    def wait_for_later_page():
        if blocked_stage != "table":
            assert decoded_pages
            for decoded in decoded_pages:
                with pytest.raises(ValueError, match="closed image"):
                    decoded.getpixel((0, 0))
        provider_started.set()
        if not last_page_detected.wait(5):
            failures.append(blocked_stage)

    def page_png(number):
        output = BytesIO()
        Image.new("RGB", (480, 320), (number, 0, 0)).save(output, format="PNG")
        return output.getvalue()

    class Yolo:
        def detect(self, image, *, model_type):
            if image.getpixel((0, 0))[0] == 3:
                last_page_detected.set()
            return SimpleNamespace(detections=[
                DetectionResult("Node", .95, BoundingBox(0, 0, 180, 300))
            ])

    class Table:
        def recognize(self, image):
            with Image.open(BytesIO(image)) as loaded:
                first = loaded.getpixel((0, 0))[0] == 1
            if first and blocked_stage == "table":
                wait_for_later_page()
            return None

    class Ocr:
        def parse_file(self, image, *, filename, **kwargs):
            if "page-1-" in filename and blocked_stage == "ocr":
                wait_for_later_page()
            return SimpleNamespace(markdown="INV 1001-CBX01")

    class Reviewer:
        def review(self, image, *args):
            with Image.open(BytesIO(image)) as loaded:
                first = loaded.getpixel((0, 0))[0] == 1
            if first and blocked_stage == "review":
                wait_for_later_page()
            return SimpleNamespace(corrections_by_number={}, prompt="", raw_output="")

    recognizer = Agent3ElementRecognizer(
        yolo=Yolo(), table_recognizer=Table(), node_ocr=Ocr(),
        node_name_reviewer=Reviewer(), node_name_review_enabled=True,
        page_workers=1, io_workers=6,
    )
    def rendered_pages():
        yield ArrayLayoutPageInput(1, page_png(1))
        assert provider_started.wait(5), "Page 1 must reach the provider during rendering"
        yield ArrayLayoutPageInput(2, page_png(2))
        yield ArrayLayoutPageInput(3, page_png(3))

    result = recognizer.recognize_stream(ElementRecognitionRequest(
        pages=tuple(ArrayLayoutPageInput(n, b"") for n in (1, 2, 3)),
        tracker_definitions=(TrackerDefinition("2-string", 2),),
    ), rendered_pages())
    assert not failures, f"{blocked_stage} wait blocked later-page detection"
    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert not any(issue.code.endswith("_failed") for issue in result.all_issues)


def test_same_page_models_overlap():
    barrier = threading.Barrier(2, timeout=5)

    class Yolo:
        def detect(self, image, *, model_type):
            barrier.wait()
            return SimpleNamespace(detections=[
                DetectionResult("Node", .95, BoundingBox(0, 0, 180, 300))
            ])

    class Table:
        def recognize(self, image):
            return None

    class Ocr:
        def parse_file(self, *args, **kwargs):
            return SimpleNamespace(markdown="INV 1001-CBX01")

    result = Agent3ElementRecognizer(
        yolo=Yolo(), table_recognizer=Table(), node_ocr=Ocr(), page_workers=1,
    ).recognize(ElementRecognitionRequest(
        pages=(ArrayLayoutPageInput(1, _tiny_png()),),
        tracker_definitions=(TrackerDefinition("2-string", 2),),
    ))
    assert not any(issue.code.endswith("detection_failed") for issue in result.all_issues)

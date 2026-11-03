"""Provider boundary and implementations for Agent1 page classification."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from io import BytesIO
import logging
from multiprocessing import get_context
from pathlib import Path
from time import perf_counter
from typing import Protocol

from PIL import Image

from count_and_design.application.config import DrawingClassificationConfig

from ..ai.embedding import EmbeddingClient, EmbeddingResult, create_embedding_client
from .category_vectors import (
    CategoryVectors,
    DrawingPageMatch,
    bundled_category_index,
    match_drawing_page,
)
from .models import (
    DrawingPageType,
    LazyPageImages,
    PageClassification,
    PageClassificationResult,
    SourceReference,
)
from .pdf_rendering import (
    CLASSIFICATION_MAX_SIZE,
    PAGE_RENDER_HEIGHT,
    PAGE_RENDER_WIDTH,
    RenderedClassificationPage,
    initialize_pdf_render_worker,
    pdf_page_count,
    placeholder_thumbnail_png,
    process_memory_mb,
    render_classification_page,
    render_classification_page_worker,
)


Agent1ProgressCallback = Callable[[int, int], None]
Agent1CancellationCheck = Callable[[], bool]


logger = logging.getLogger(__name__)


class Agent1ProviderError(RuntimeError):
    """Raised when Agent1 cannot render or classify a PDF."""


class Agent1NoRelevantDrawingPagesError(Agent1ProviderError):
    """Raised when Agent1 finds neither array nor overall layout pages."""


class Agent1NoReadablePagesError(Agent1ProviderError):
    """Raised when every rendered PDF page is visually blank."""


class Agent1Cancelled(Agent1ProviderError):
    """Raised cooperatively when the user cancels Agent1."""


class PageClassificationProvider(Protocol):
    """Classify every page of one drawing PDF."""

    def classify(
        self,
        pdf_path: Path,
        page_count: int,
        *,
        on_progress: Agent1ProgressCallback | None = None,
        is_cancelled: Agent1CancellationCheck | None = None,
    ) -> PageClassificationResult: ...


class FakePageClassificationProvider:
    """Return deterministic demo data for tests and offline UI checks."""

    def classify(
        self,
        pdf_path: Path,
        page_count: int,
        *,
        on_progress: Agent1ProgressCallback | None = None,
        is_cancelled: Agent1CancellationCheck | None = None,
    ) -> PageClassificationResult:
        pages: list[PageClassification] = []
        thumbnails: dict[int, bytes] = {}
        for page_number in range(1, page_count + 1):
            if is_cancelled is not None and is_cancelled():
                raise Agent1Cancelled("Page classification was cancelled.")
            if page_number in {3, 8}:
                page_type = DrawingPageType.OVERALL_LAYOUT.value
                confidence = 0.92
            elif page_number % 11 == 0:
                page_type = DrawingPageType.OTHER.value
                confidence = 0.76
            else:
                page_type = DrawingPageType.ARRAY_LAYOUT.value
                confidence = 0.89
            # A fake provider must not touch the filesystem, so the review
            # grid gets a blank thumbnail rather than a rendered page.
            thumbnails[page_number] = placeholder_thumbnail_png()
            pages.append(
                PageClassification(
                    page_number=page_number,
                    page_type=page_type,
                    purpose=f"Mock classification for {pdf_path.name}",
                    source=SourceReference(
                        page_number=page_number,
                        confidence=confidence,
                        model_version="fake-agent1-v1",
                        recognition_version="demo-v1",
                        source="fake:page-classification",
                    ),
                )
            )
            if on_progress is not None:
                on_progress(page_number, page_count)
        return PageClassificationResult(
            pages=tuple(pages),
            thumbnails=thumbnails,
        )


class EmbeddingPageClassificationProvider:
    """Real Agent1 using PDFium rendering and the packaged vector library."""

    def __init__(
        self,
        client: EmbeddingClient | None = None,
        *,
        library: Mapping[str, CategoryVectors] | None = None,
        min_score: float | None = None,
        render_processes: int | None = None,
        embedding_concurrency: int | None = None,
        max_in_flight: int | None = None,
        classification_max_size: int = CLASSIFICATION_MAX_SIZE,
        # Deprecated compatibility alias for embedding_concurrency.
        max_workers: int | None = None,
        render_width: int = PAGE_RENDER_WIDTH,
        render_height: int = PAGE_RENDER_HEIGHT,
    ) -> None:
        self._client = client
        self._library = library
        self._min_score = min_score
        self._render_processes = render_processes
        self._embedding_concurrency = embedding_concurrency
        self._max_in_flight = max_in_flight
        self._classification_max_size = classification_max_size
        self._max_workers = max_workers
        self._render_width = render_width
        self._render_height = render_height

    def classify(
        self,
        pdf_path: Path,
        page_count: int,
        *,
        on_progress: Agent1ProgressCallback | None = None,
        is_cancelled: Agent1CancellationCheck | None = None,
    ) -> PageClassificationResult:
        try:
            actual_page_count = pdf_page_count(pdf_path)
            if actual_page_count != page_count:
                raise Agent1ProviderError(
                    "PDF page count changed after upload."
                )
            client = self._client or create_embedding_client()
            library = self._library or bundled_category_index()
            classification_config = DrawingClassificationConfig.from_env()
            min_score = (
                self._min_score
                if self._min_score is not None
                else classification_config.min_score
            )
            render_processes = (
                self._render_processes
                if self._render_processes is not None
                else classification_config.render_processes
            )
            embedding_concurrency = (
                self._embedding_concurrency
                if self._embedding_concurrency is not None
                else self._max_workers
                if self._max_workers is not None
                else classification_config.embedding_concurrency
            )
            max_in_flight = (
                self._max_in_flight
                if self._max_in_flight is not None
                else classification_config.max_in_flight
            )
            if min(render_processes, embedding_concurrency, max_in_flight) <= 0:
                raise Agent1ProviderError(
                    "Agent1 concurrency settings must be greater than zero."
                )
            return self._classify_pages(
                pdf_path,
                page_count,
                client=client,
                library=library,
                min_score=min_score,
                render_processes=render_processes,
                embedding_concurrency=embedding_concurrency,
                max_in_flight=max_in_flight,
                on_progress=on_progress,
                is_cancelled=is_cancelled,
            )
        except (Agent1Cancelled, Agent1ProviderError):
            raise
        except Exception as exc:
            raise Agent1ProviderError(str(exc)) from exc

    def _classify_pages(
        self,
        pdf_path: Path,
        page_count: int,
        *,
        client: EmbeddingClient,
        library: Mapping[str, CategoryVectors],
        min_score: float,
        render_processes: int,
        embedding_concurrency: int,
        max_in_flight: int,
        on_progress: Agent1ProgressCallback | None,
        is_cancelled: Agent1CancellationCheck | None,
    ) -> PageClassificationResult:
        """Bounded multi-process render and threaded embedding pipeline."""

        pipeline_started = perf_counter()
        pages: list[PageClassification | None] = [None] * page_count
        thumbnails: dict[int, bytes] = {}
        completed = 0
        render_seconds: list[float] = []
        embedding_seconds: list[float] = []
        blank_page_count = 0
        render_limit = min(max_in_flight, max(1, render_processes * 2))

        if render_processes == 1:
            render_executor = ThreadPoolExecutor(max_workers=1)

            def submit_render(page_index: int) -> Future[RenderedClassificationPage]:
                return render_executor.submit(
                    render_classification_page,
                    pdf_path,
                    page_index,
                    classification_max_size=self._classification_max_size,
                )
        else:
            render_executor = ProcessPoolExecutor(
                max_workers=render_processes,
                mp_context=get_context("spawn"),
                initializer=initialize_pdf_render_worker,
                initargs=(str(pdf_path),),
            )

            def submit_render(page_index: int) -> Future[RenderedClassificationPage]:
                return render_executor.submit(
                    render_classification_page_worker,
                    page_index,
                    self._classification_max_size,
                )

        embedding_executor = ThreadPoolExecutor(
            max_workers=embedding_concurrency
        )
        render_futures: dict[Future[RenderedClassificationPage], int] = {}
        embedding_futures: dict[
            Future[tuple[EmbeddingResult, float]], RenderedClassificationPage
        ] = {}
        next_page_index = 0
        try:
            while (
                next_page_index < page_count
                or render_futures
                or embedding_futures
            ):
                if is_cancelled is not None and is_cancelled():
                    raise Agent1Cancelled("Page classification was cancelled.")

                while (
                    next_page_index < page_count
                    and len(render_futures) < render_limit
                    and len(render_futures) + len(embedding_futures)
                    < max_in_flight
                ):
                    future = submit_render(next_page_index)
                    render_futures[future] = next_page_index
                    next_page_index += 1

                pending = set(render_futures) | set(embedding_futures)
                if not pending:
                    continue
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    if future in render_futures:
                        page_index = render_futures.pop(future)
                        rendered = future.result()
                        render_seconds.append(rendered.metrics.total_seconds)
                        logger.info(
                            "agent1.render page=%d pdfium_ms=%.1f "
                            "encode_ms=%.1f total_ms=%.1f "
                            "classification_bytes=%d thumbnail_bytes=%d "
                            "working_set_mb=%.1f peak_working_set_mb=%.1f",
                            page_index + 1,
                            rendered.metrics.pdfium_seconds * 1000.0,
                            rendered.metrics.encode_seconds * 1000.0,
                            rendered.metrics.total_seconds * 1000.0,
                            len(rendered.classification_png),
                            len(rendered.thumbnail_png),
                            rendered.metrics.working_set_mb,
                            rendered.metrics.peak_working_set_mb,
                        )
                        if _is_visually_blank(rendered.classification_png):
                            page_number = rendered.page_index + 1
                            pages[rendered.page_index] = _build_blank_page(
                                page_number
                            )
                            thumbnails[page_number] = rendered.thumbnail_png
                            blank_page_count += 1
                            completed += 1
                            logger.info(
                                "agent1.blank_page page=%d completed=%d "
                                "total=%d",
                                page_number,
                                completed,
                                page_count,
                            )
                            if on_progress is not None:
                                on_progress(completed, page_count)
                            continue
                        embed_future = embedding_executor.submit(
                            _embed_timed,
                            client,
                            rendered.classification_png,
                        )
                        embedding_futures[embed_future] = rendered
                        continue

                    rendered = embedding_futures.pop(future)
                    embedding, elapsed = future.result()
                    embedding_seconds.append(elapsed)
                    match = match_drawing_page(
                        embedding.embedding,
                        library,
                        min_score=min_score,
                    )
                    page_number = rendered.page_index + 1
                    pages[rendered.page_index] = _build_page(
                        page_number,
                        match,
                        embedding.model,
                        min_score,
                    )
                    thumbnails[page_number] = rendered.thumbnail_png
                    completed += 1
                    logger.info(
                        "agent1.embedding page=%d seconds=%.3f "
                        "input_bytes=%d completed=%d total=%d",
                        page_number,
                        elapsed,
                        len(rendered.classification_png),
                        completed,
                        page_count,
                    )
                    if on_progress is not None:
                        on_progress(completed, page_count)
        finally:
            for future in render_futures:
                future.cancel()
            for future in embedding_futures:
                future.cancel()
            render_executor.shutdown(cancel_futures=True)
            embedding_executor.shutdown(cancel_futures=True)

        if blank_page_count == page_count:
            raise Agent1NoReadablePagesError(
                "No readable pages were found in the PDF."
            )

        working_mb, peak_mb = process_memory_mb()
        elapsed = perf_counter() - pipeline_started
        logger.info(
            "agent1.complete pages=%d seconds=%.3f render_processes=%d "
            "embedding_concurrency=%d max_in_flight=%d "
            "avg_render_ms=%.1f avg_embedding_ms=%.1f "
            "thumbnail_bytes=%d working_set_mb=%.1f peak_working_set_mb=%.1f",
            page_count,
            elapsed,
            render_processes,
            embedding_concurrency,
            max_in_flight,
            _average(render_seconds) * 1000.0,
            _average(embedding_seconds) * 1000.0,
            sum(len(image) for image in thumbnails.values()),
            working_mb,
            peak_mb,
        )
        return PageClassificationResult(
            pages=tuple(page for page in pages if page is not None),
            thumbnails=thumbnails,
            page_images=LazyPageImages(
                pdf_path,
                page_count,
                width=self._render_width,
                height=self._render_height,
            ),
        )


def _embed_timed(
    client: EmbeddingClient,
    classification_png: bytes,
) -> tuple[EmbeddingResult, float]:
    started = perf_counter()
    # Empty prompt is required because the packaged category vectors were
    # generated in that same embedding space.
    result = client.embed_image(classification_png, mime_type="image/png")
    return result, perf_counter() - started


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _is_visually_blank(png: bytes, *, white_threshold: int = 250) -> bool:
    """Return whether a rendered page contains no meaningful visible pixels."""

    with Image.open(BytesIO(png)) as image:
        with image.convert("RGB") as rgb_image:
            extrema = rgb_image.getextrema()
    return all(channel_min >= white_threshold for channel_min, _ in extrema)


def _build_blank_page(page_number: int) -> PageClassification:
    return PageClassification(
        page_number=page_number,
        page_type=DrawingPageType.OTHER.value,
        purpose="blank_page",
        source=SourceReference(
            page_number=page_number,
            confidence=1.0,
            recognition_version="agent1-blank-page-v1",
            source="pdfium:blank-page-detection",
        ),
    )


def _build_page(
    page_number: int,
    match: DrawingPageMatch,
    model_version: str,
    min_score: float,
) -> PageClassification:
    return PageClassification(
        page_number=page_number,
        page_type=match.page_type,
        purpose=match.category,
        source=SourceReference(
            page_number=page_number,
            confidence=max(0.0, min(1.0, match.score)),
            model_version=model_version,
            recognition_version="agent1-cosine-v4-1080x720",
            source="embedding:category-vectors",
            raw={
                "matched_category": match.category,
                "min_score": min_score,
                "category_scores": [
                    {
                        "category": score.category,
                        "score": score.score,
                        "score_to_average": score.score_to_average,
                        "best_sample_score": score.best_sample_score,
                    }
                    for score in match.scores
                ],
            },
        ),
    )

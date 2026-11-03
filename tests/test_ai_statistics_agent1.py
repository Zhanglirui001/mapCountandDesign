from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PIL import Image
from PySide6.QtGui import QPainter, QPdfWriter
from PySide6.QtWidgets import QApplication

from count_and_design.application.ai.embedding import (
    EmbeddingClient,
    EmbeddingResult,
)
from count_and_design.application.ai_statistics import (
    UNKNOWN_CATEGORY,
    Agent1Cancelled,
    Agent1NoReadablePagesError,
    Agent1ProviderError,
    CategoryVectors,
    DrawingPageType,
    EmbeddingPageClassificationProvider,
    LazyPageImages,
    PAGE_RENDER_HEIGHT,
    PAGE_RENDER_WIDTH,
    PageClassification,
    PageClassificationResult,
    bundled_category_index,
    match_drawing_page,
    render_classification_page,
    score_categories,
    render_pdf_page_assets,
)
from count_and_design.application.config import DrawingClassificationConfig
from count_and_design.application.ai_statistics.pdf_rendering import (
    CLASSIFICATION_MAX_SIZE,
    CLASSIFICATION_HEIGHT,
    initialize_pdf_render_worker,
    render_high_resolution_page_worker,
)


def _library() -> dict[str, CategoryVectors]:
    """Three categories where only array_layout holds more than one image."""

    return {
        "array_layout": CategoryVectors(
            name="array_layout",
            average=np.array([1.0, 0.0, 0.0]),
            # The winning sample is off-prototype, so best_sample_score
            # beats score_to_average for this category.
            samples=np.array([[1.0, 0.0, 0.0], [0.6, 0.8, 0.0]]),
        ),
        "overall_site_layout": CategoryVectors(
            name="overall_site_layout",
            average=np.array([0.0, 1.0, 0.0]),
            samples=np.array([[0.0, 1.0, 0.0]]),
        ),
        "trench_details": CategoryVectors(
            name="trench_details",
            average=np.array([0.0, 0.0, 1.0]),
            samples=np.array([[0.0, 0.0, 1.0]]),
        ),
    }


class VectorEmbeddingProvider:
    """Return a fixed page vector so classification is deterministic."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    def embed_text(self, text: str) -> EmbeddingResult:
        raise AssertionError("Agent1 must not embed text.")

    def embed_texts(self, texts) -> list[EmbeddingResult]:
        raise AssertionError("Agent1 must not embed text.")

    def embed_image(
        self,
        image,
        *,
        prompt=None,
        mime_type=None,
    ) -> EmbeddingResult:
        assert isinstance(image, bytes)
        assert image.startswith(b"\x89PNG")
        assert mime_type == "image/png"
        return EmbeddingResult(self._vector, model="image-test")


def _write_pdf(path: Path, page_count: int = 1) -> None:
    QApplication.instance() or QApplication([])
    writer = QPdfWriter(str(path))
    painter = QPainter(writer)
    for index in range(page_count):
        if index:
            writer.newPage()
        painter.drawText(100, 100, f"Array layout {index}")
    painter.end()


def _write_blank_pdf(path: Path, page_count: int = 1) -> None:
    QApplication.instance() or QApplication([])
    writer = QPdfWriter(str(path))
    painter = QPainter(writer)
    for index in range(1, page_count):
        writer.newPage()
    painter.end()


def _test_provider(*args, **kwargs) -> EmbeddingPageClassificationProvider:
    kwargs.setdefault("render_processes", 1)
    kwargs.setdefault("classification_max_size", 160)
    return EmbeddingPageClassificationProvider(
        *args,
        render_width=600,
        render_height=900,
        **kwargs,
    )


def test_agent1_high_resolution_defaults_are_6000_by_9000() -> None:
    assert (PAGE_RENDER_WIDTH, PAGE_RENDER_HEIGHT) == (6000, 9000)


def test_classification_uses_reference_canvas_in_direct_and_worker_render(tmp_path: Path) -> None:
    from count_and_design.application.ai_statistics.pdf_rendering import (
        render_classification_page_worker,
    )

    pdf_path = tmp_path / "portrait.pdf"
    _write_pdf(pdf_path)
    assert (CLASSIFICATION_MAX_SIZE, CLASSIFICATION_HEIGHT) == (1080, 720)
    initialize_pdf_render_worker(str(pdf_path))
    for rendered in (render_classification_page(pdf_path, 0),
                     render_classification_page_worker(0)):
        assert (rendered.width, rendered.height) == (1080, 720)
        with Image.open(BytesIO(rendered.classification_png)) as image:
            assert image.size == (1080, 720)
            # A portrait page is centered with white side margins, without stretching.
            assert image.getpixel((0, 360)) == (255, 255, 255)
            assert image.getpixel((1079, 360)) == (255, 255, 255)
            assert np.asarray(image).min() < 255


def test_shared_render_worker_supports_agent3_6000_long_side(tmp_path: Path) -> None:
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    initialize_pdf_render_worker(str(pdf_path))

    page_index, png = render_high_resolution_page_worker(0, 6000, 6000)

    assert page_index == 0
    with Image.open(BytesIO(png)) as image:
        assert max(image.size) == 6000
        assert image.width != image.height


def test_agent1_concurrency_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("AGENT1_MAX_WORKERS", "14")
    monkeypatch.setenv("AGENT1_RENDER_PROCESSES", "4")
    monkeypatch.setenv("AGENT1_EMBEDDING_CONCURRENCY", "8")
    monkeypatch.setenv("AGENT1_MAX_IN_FLIGHT", "12")

    config = DrawingClassificationConfig.from_env()

    assert config.max_workers == 14
    assert config.render_processes == 4
    assert config.embedding_concurrency == 8
    assert config.max_in_flight == 12


def test_agent1_default_classification_threshold_is_0675() -> None:
    assert DrawingClassificationConfig().min_score == 0.675


def test_pdf_page_assets_have_no_legacy_fixed_canvas(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)

    rendered = render_pdf_page_assets(
        pdf_path,
        0,
        width=600,
        height=900,
        classification_max_size=160,
        thumbnail_width=32,
    )

    with Image.open(BytesIO(rendered.page_png)) as image:
        assert image.width == 600 or image.height == 900
        assert image.size != (600, 900)
    with Image.open(BytesIO(rendered.classification_png)) as image:
        assert image.size == (160, 107)
    with Image.open(BytesIO(rendered.thumbnail_png)) as image:
        assert max(image.size) == 32


def test_direct_classification_render_is_consistent_with_highres_downsample(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    baseline = render_pdf_page_assets(
        pdf_path,
        0,
        width=600,
        height=900,
        classification_max_size=160,
    )
    direct = render_classification_page(
        pdf_path,
        0,
        classification_max_size=160,
    )

    with Image.open(BytesIO(direct.classification_png)) as new_image:
        normalized_new = new_image.convert("RGB")
        new_pixels = np.asarray(normalized_new, dtype=np.int16)
    with Image.open(BytesIO(baseline.classification_png)) as old_image:
        assert abs(old_image.width - normalized_new.width) <= 1
        assert abs(old_image.height - normalized_new.height) <= 1
        normalized_old = old_image.convert("RGB").resize(normalized_new.size)
        old_pixels = np.asarray(normalized_old, dtype=np.int16)

    assert float(np.abs(old_pixels - new_pixels).mean()) < 3.0


def test_lazy_page_type_checks_do_not_render_and_load_only_matching_pages(
    monkeypatch,
) -> None:
    rendered_indices = []

    def fake_highres(_path, page_index, **_kwargs):
        rendered_indices.append(page_index)
        return b"png"

    monkeypatch.setattr(
        "count_and_design.application.ai_statistics.pdf_rendering."
        "render_high_resolution_page",
        fake_highres,
    )
    lazy_images = LazyPageImages("unused.pdf", 2, width=600, height=900)
    result = PageClassificationResult(
        pages=(
            PageClassification(1, DrawingPageType.ARRAY_LAYOUT.value),
            PageClassification(2, DrawingPageType.OVERALL_LAYOUT.value),
        ),
        page_images=lazy_images,
    )

    assert result.has_page_type(DrawingPageType.OVERALL_LAYOUT.value)
    assert result.cached_images_for_type(
        DrawingPageType.OVERALL_LAYOUT.value
    ) == {}
    assert rendered_indices == []

    assert result.images_for_type(DrawingPageType.OVERALL_LAYOUT.value) == {
        2: b"png"
    }
    assert rendered_indices == [1]


def test_lazy_page_images_do_not_hold_lock_during_render(monkeypatch) -> None:
    lazy_images = LazyPageImages("unused.pdf", 1, width=600, height=900)
    seen: dict[str, dict[int, bytes]] = {}

    def fake_highres(_path, page_index, **_kwargs):
        seen["during_render"] = lazy_images.cached_items({page_index + 1})
        return b"png"

    monkeypatch.setattr(
        "count_and_design.application.ai_statistics.pdf_rendering."
        "render_high_resolution_page",
        fake_highres,
    )

    assert lazy_images[1] == b"png"
    assert seen["during_render"] == {}
    assert lazy_images.cached_items({1}) == {1: b"png"}
    assert lazy_images.render_size == (600, 900)


def test_category_score_combines_average_and_best_sample() -> None:
    scores = {item.category: item for item in score_categories([0.6, 0.8, 0.0], _library())}

    array = scores["array_layout"]
    assert array.score_to_average == 0.6
    assert array.best_sample_score == 1.0
    assert array.score == 1.0

    overall = scores["overall_site_layout"]
    assert overall.score_to_average == overall.best_sample_score == 0.8


def test_single_image_category_scores_are_identical() -> None:
    scores = {item.category: item for item in score_categories([0.0, 0.0, 1.0], _library())}

    trench = scores["trench_details"]
    assert trench.score_to_average == trench.best_sample_score == 1.0


def test_scores_are_sorted_by_combined_score() -> None:
    scores = score_categories([0.6, 0.8, 0.0], _library())

    assert scores[0].category == "array_layout"
    assert [item.score for item in scores] == sorted(
        (item.score for item in scores),
        reverse=True,
    )


def test_match_folds_unmapped_category_into_other() -> None:
    match = match_drawing_page([0.0, 0.0, 1.0], _library(), min_score=0.5)

    assert match.category == "trench_details"
    assert match.page_type == DrawingPageType.OTHER.value
    assert not match.is_unknown


def test_match_maps_the_two_front_end_categories() -> None:
    array = match_drawing_page([1.0, 0.0, 0.0], _library(), min_score=0.5)
    overall = match_drawing_page([0.0, 1.0, 0.0], _library(), min_score=0.5)

    assert array.page_type == DrawingPageType.ARRAY_LAYOUT.value
    assert overall.page_type == DrawingPageType.OVERALL_LAYOUT.value


def test_match_below_threshold_is_unknown_and_lands_in_other() -> None:
    # Sits between array_layout and trench_details, matching neither well.
    match = match_drawing_page([1.0, 0.0, 1.0], _library(), min_score=0.8)

    assert match.is_unknown
    assert match.category == UNKNOWN_CATEGORY
    assert match.page_type == DrawingPageType.OTHER.value
    assert round(match.score, 6) == round(2.0**-0.5, 6)


def test_packaged_category_index_loads() -> None:
    library = bundled_category_index()

    assert len(library) == 88
    assert library["REVAMP+array_layout"].sample_count == 19
    assert library["REVAMP+overall_site_layout"].sample_count == 2
    assert library["EVS+ARRAY LAYOUT"].sample_count == 702
    assert library["REVAMP+summary table"].sample_count == 4
    assert library["EVS+DC SUMMARY"].sample_count == 1
    assert library["EVS+MV FEEDER CROSSING TABLE"].sample_count == 1
    assert sum(category.sample_count for category in library.values()) == 2526
    for category in library.values():
        assert category.average.shape == (2048,)
        assert category.samples.shape[1] == 2048
        assert np.allclose(np.linalg.norm(category.average), 1.0)
        assert np.allclose(np.linalg.norm(category.samples, axis=1), 1.0)


def test_real_agent1_uses_pdfium_png_and_category_vectors(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    provider = _test_provider(
        EmbeddingClient(provider=VectorEmbeddingProvider([0.6, 0.8, 0.0])),
        library=_library(),
        min_score=0.5,
    )
    progress = []

    result = provider.classify(
        pdf_path,
        1,
        on_progress=lambda completed, total: progress.append(
            (completed, total)
        ),
    )

    assert progress == [(1, 1)]
    page = result.pages[0]
    assert page.page_type == DrawingPageType.ARRAY_LAYOUT.value
    assert page.purpose == "array_layout"
    assert page.source.model_version == "image-test"
    assert page.source.confidence == 1.0
    assert page.source.raw["matched_category"] == "array_layout"
    assert page.source.raw["min_score"] == 0.5
    assert len(page.source.raw["category_scores"]) == 3
    assert result.thumbnails[1].startswith(b"\x89PNG")
    assert isinstance(result.page_images, LazyPageImages)
    assert result.page_images.cached_page_count == 0
    assert result.page_images[1].startswith(b"\x89PNG")
    assert result.page_images.cached_page_count == 1
    with Image.open(BytesIO(result.page_images[1])) as image:
        assert image.size != (600, 900)
        assert image.width <= 600
        assert image.height <= 900
    assert result.images_for_type(DrawingPageType.ARRAY_LAYOUT.value) == {
        1: result.page_images[1]
    }


def test_real_agent1_rejects_structurally_valid_blank_pdf(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "blank.pdf"
    _write_blank_pdf(pdf_path, page_count=2)
    provider = _test_provider(
        EmbeddingClient(provider=VectorEmbeddingProvider([0.6, 0.8, 0.0])),
        library=_library(),
        min_score=0.5,
    )
    progress = []

    with pytest.raises(
        Agent1NoReadablePagesError,
        match="No readable pages",
    ):
        provider.classify(
            pdf_path,
            2,
            on_progress=lambda completed, total: progress.append(
                (completed, total)
            ),
        )

    assert progress == [(1, 2), (2, 2)]


def test_concurrent_classify_keeps_page_order_and_thumbnails(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path, page_count=5)
    provider = _test_provider(
        EmbeddingClient(provider=VectorEmbeddingProvider([0.6, 0.8, 0.0])),
        library=_library(),
        min_score=0.5,
        max_workers=4,
    )
    progress = []

    result = provider.classify(
        pdf_path,
        5,
        on_progress=lambda completed, total: progress.append(completed),
    )

    assert [page.page_number for page in result.pages] == [1, 2, 3, 4, 5]
    assert sorted(progress) == [1, 2, 3, 4, 5]
    assert sorted(result.thumbnails) == [1, 2, 3, 4, 5]
    assert sorted(result.page_images) == [1, 2, 3, 4, 5]
    for data in result.thumbnails.values():
        assert data.startswith(b"\x89PNG")


def test_multiprocess_render_keeps_page_order(tmp_path: Path) -> None:
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path, page_count=3)
    provider = _test_provider(
        EmbeddingClient(provider=VectorEmbeddingProvider([0.6, 0.8, 0.0])),
        library=_library(),
        min_score=0.5,
        render_processes=2,
        embedding_concurrency=2,
        max_in_flight=4,
    )

    result = provider.classify(pdf_path, 3)

    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert sorted(result.thumbnails) == [1, 2, 3]


def test_concurrent_classify_propagates_cancellation(tmp_path: Path) -> None:
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path, page_count=4)
    provider = _test_provider(
        EmbeddingClient(provider=VectorEmbeddingProvider([0.6, 0.8, 0.0])),
        library=_library(),
        min_score=0.5,
        max_workers=2,
    )

    with pytest.raises(Agent1Cancelled):
        provider.classify(pdf_path, 4, is_cancelled=lambda: True)


def test_classify_rejects_a_page_count_that_no_longer_matches(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path, page_count=2)
    provider = _test_provider(
        EmbeddingClient(provider=VectorEmbeddingProvider([0.6, 0.8, 0.0])),
        library=_library(),
        min_score=0.5,
    )

    with pytest.raises(Agent1ProviderError, match="page count changed"):
        provider.classify(pdf_path, 7)


def test_real_agent1_records_unknown_below_threshold(tmp_path: Path) -> None:
    pdf_path = tmp_path / "drawing.pdf"
    _write_pdf(pdf_path)
    provider = _test_provider(
        EmbeddingClient(provider=VectorEmbeddingProvider([1.0, 0.0, 1.0])),
        library=_library(),
        min_score=0.8,
    )

    page = provider.classify(pdf_path, 1).pages[0]

    assert page.page_type == DrawingPageType.OTHER.value
    assert page.source.raw["matched_category"] == UNKNOWN_CATEGORY

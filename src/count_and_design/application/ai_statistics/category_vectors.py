"""Packaged drawing category vector library and cosine scoring for Agent1."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from .models import DrawingPageType


CATEGORY_INDEX_FILENAME = "category_index.sqlite"
UNKNOWN_CATEGORY = "unknown"

# Layout and summary drawing categories map to dedicated
# page types in the review UI; everything else, including a page that
# fails the score threshold, is folded into Other.
_PAGE_TYPE_BY_CATEGORY = {
    "array_layout": DrawingPageType.ARRAY_LAYOUT,
    "overall_site_layout": DrawingPageType.OVERALL_LAYOUT,
    "dc_collection_system_summary": DrawingPageType.OVERALL_LAYOUT,
    "summary_table": DrawingPageType.OVERALL_LAYOUT,
}


class CategoryVectorError(RuntimeError):
    """Raised when the packaged category vector library cannot be loaded."""


@dataclass(frozen=True, eq=False)
class CategoryVectors:
    """L2-normalized prototype and per-image vectors for one source category."""

    name: str
    average: np.ndarray
    samples: np.ndarray
    page_type: str | None = None

    @property
    def sample_count(self) -> int:
        return int(self.samples.shape[0])


@dataclass(frozen=True)
class CategoryScore:
    """Both cosine scores for one category plus the combined value."""

    category: str
    score: float
    score_to_average: float
    best_sample_score: float


@dataclass(frozen=True)
class DrawingPageMatch:
    """Winning category, folded page type, and every category score."""

    category: str
    page_type: str
    score: float
    scores: tuple[CategoryScore, ...]

    @property
    def is_unknown(self) -> bool:
        return self.category == UNKNOWN_CATEGORY


def bundled_category_vector_dir() -> Path:
    """Return the packaged directory holding the category vector database."""

    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            return (
                Path(bundle_root)
                / "count_and_design"
                / "resources"
                / "embeddings"
            )
    return Path(__file__).resolve().parents[2] / "resources" / "embeddings"


def load_category_index(
    path: str | Path | None = None,
) -> dict[str, CategoryVectors]:
    """Load the category vector library, L2-normalizing every vector."""

    resolved = (
        Path(path)
        if path is not None
        else bundled_category_vector_dir() / CATEGORY_INDEX_FILENAME
    )
    if resolved.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
        return _load_sqlite_index(resolved)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CategoryVectorError(
            f"Unable to read category vectors: {exc}"
        ) from exc
    except ValueError as exc:
        raise CategoryVectorError(
            f"Category vector file is not valid JSON: {resolved}"
        ) from exc

    categories = payload.get("categories") if isinstance(payload, dict) else None
    if not isinstance(categories, dict) or not categories:
        raise CategoryVectorError(
            f"Category vector file has no categories: {resolved}"
        )

    library: dict[str, CategoryVectors] = {}
    for name, entry in categories.items():
        if not isinstance(entry, dict):
            raise CategoryVectorError(f"Category entry is not an object: {name}")
        images = entry.get("images")
        if not isinstance(images, dict) or not images:
            raise CategoryVectorError(f"Category has no reference images: {name}")
        samples = _normalize_rows(
            np.asarray(
                [image["embedding"] for image in images.values()],
                dtype=np.float64,
            ),
            name,
        )
        average = _normalize_rows(
            np.asarray([entry["average_embedding"]], dtype=np.float64),
            name,
        )[0]
        if average.shape[0] != samples.shape[1]:
            raise CategoryVectorError(
                f"Category vector dimensions are inconsistent: {name}"
            )
        library[name] = CategoryVectors(
            name=name,
            average=average,
            samples=samples,
        )
    return library


def _load_sqlite_index(path: Path) -> dict[str, CategoryVectors]:
    """Read a SQLite library without creating or modifying its resource file."""
    library: dict[str, CategoryVectors] = {}
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise CategoryVectorError("Unsupported category database schema.")
            for category_id, name, page_type, dimensions, dtype, average_blob in db.execute(
                "SELECT id, name, page_type, dimensions, dtype, average_embedding "
                "FROM categories ORDER BY id"
            ):
                rows = db.execute("SELECT embedding FROM samples WHERE category_id = ? ORDER BY id",
                                  (category_id,)).fetchall()
                if not rows:
                    continue
                if (dtype != "float64-le" or average_blob is None
                        or len(average_blob) != dimensions * 8
                        or any(len(row[0]) != dimensions * 8 for row in rows)):
                    raise CategoryVectorError(f"Malformed stored vectors: {name}")
                if page_type not in {item.value for item in DrawingPageType}:
                    raise CategoryVectorError(f"Unknown page type: {page_type}")
                average = _normalize_rows(
                    np.frombuffer(average_blob, dtype="<f8").reshape(1, dimensions), name)[0]
                samples = _normalize_rows(np.stack(
                    [np.frombuffer(row[0], dtype="<f8") for row in rows]), name)
                library[name] = CategoryVectors(name, average, samples, page_type)
        finally:
            db.close()
    except (sqlite3.Error, ValueError) as exc:
        raise CategoryVectorError(f"Unable to load category database: {path}: {exc}") from exc
    if not library:
        raise CategoryVectorError(f"Category database has no reference samples: {path}")
    return library


@lru_cache(maxsize=1)
def bundled_category_index() -> dict[str, CategoryVectors]:
    """Load and cache the packaged category vector library."""

    return load_category_index()


def score_categories(
    page_embedding: np.ndarray | list[float],
    library: Mapping[str, CategoryVectors],
) -> tuple[CategoryScore, ...]:
    """Score one page against every category, highest combined score first."""

    if not library:
        raise CategoryVectorError("The category vector library is empty.")
    vector = np.asarray(page_embedding, dtype=np.float64)
    if vector.ndim != 1 or vector.size == 0:
        raise CategoryVectorError("Page embedding must be a non-empty vector.")
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        raise CategoryVectorError("Page embedding must not be a zero vector.")
    vector = vector / norm

    scores: list[CategoryScore] = []
    for name, category in library.items():
        if category.average.shape[0] != vector.shape[0]:
            raise CategoryVectorError(
                f"Page embedding has {vector.shape[0]} dimensions but "
                f"category '{name}' has {category.average.shape[0]}."
            )
        score_to_average = float(category.average @ vector)
        best_sample_score = float(np.max(category.samples @ vector))
        scores.append(
            CategoryScore(
                category=name,
                score=max(score_to_average, best_sample_score),
                score_to_average=score_to_average,
                best_sample_score=best_sample_score,
            )
        )
    return tuple(sorted(scores, key=lambda item: item.score, reverse=True))


def match_drawing_page(
    page_embedding: np.ndarray | list[float],
    library: Mapping[str, CategoryVectors],
    *,
    min_score: float,
) -> DrawingPageMatch:
    """Pick the best category, falling back to unknown below the threshold."""

    scores = score_categories(page_embedding, library)
    best = scores[0]
    if best.score < min_score:
        return DrawingPageMatch(
            category=UNKNOWN_CATEGORY,
            page_type=DrawingPageType.OTHER.value,
            score=best.score,
            scores=scores,
        )
    category = library[best.category]
    page_type = category.page_type or _PAGE_TYPE_BY_CATEGORY.get(
        best.category.split("+", 1)[-1].lower().replace(" ", "_"), DrawingPageType.OTHER).value
    return DrawingPageMatch(
        category=best.category,
        page_type=page_type,
        score=best.score,
        scores=scores,
    )


def _normalize_rows(matrix: np.ndarray, name: str) -> np.ndarray:
    if matrix.ndim != 2 or matrix.size == 0 or not np.isfinite(matrix).all():
        raise CategoryVectorError(f"Category vectors are malformed: {name}")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if not np.all(norms > 0.0):
        raise CategoryVectorError(f"Category contains a zero vector: {name}")
    return matrix / norms

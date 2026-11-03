"""SQLite storage for drawing reference vectors, with transactional sample edits."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from .models import DrawingPageType


SCHEMA_VERSION = 1


def vector_blob(values, dimension: int | None = None) -> tuple[bytes, int]:
    vector = np.asarray(values, dtype="<f8")
    if (vector.ndim != 1 or vector.size == 0
            or not np.isfinite(vector).all() or not np.any(vector)
            or (dimension is not None and vector.size != dimension)):
        raise ValueError("Expected a finite, nonzero vector with matching dimensions.")
    return vector.tobytes(), int(vector.size)


class CategoryVectorStore:
    """An editable store. Callers reload their in-memory library after edits."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY,
                    project_type TEXT NOT NULL CHECK(project_type IN ('REVAMP', 'EVS')),
                    source_type TEXT NOT NULL,
                    name TEXT NOT NULL UNIQUE,
                    page_type TEXT NOT NULL,
                    dimensions INTEGER NOT NULL CHECK(dimensions > 0),
                    dtype TEXT NOT NULL DEFAULT 'float64-le' CHECK(dtype = 'float64-le'),
                    average_embedding BLOB,
                    embedding_provider TEXT,
                    source_path TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(project_type, source_type),
                    CHECK(name = project_type || '+' || source_type),
                    CHECK(average_embedding IS NULL OR length(average_embedding) = dimensions * 8)
                );
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY,
                    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                    image_name TEXT NOT NULL,
                    image_hash TEXT,
                    embedding BLOB NOT NULL,
                    UNIQUE(category_id, image_name)
                );
                CREATE INDEX IF NOT EXISTS samples_category ON samples(category_id);
                PRAGMA user_version = 1;
            """)

    def import_category(self, project_type: str, source_type: str, entry: dict,
                        *, source_path: str, metadata: dict | None = None) -> None:
        average, dimension = vector_blob(entry["average_embedding"])
        images = entry["images"]
        if not images:
            raise ValueError(f"Category has no samples: {source_type}")
        canonical = source_type.lower().replace(" ", "_")
        page_type = {"array_layout": DrawingPageType.ARRAY_LAYOUT.value,
                     "overall_site_layout": DrawingPageType.OVERALL_LAYOUT.value,
                     "dc_collection_system_summary": DrawingPageType.OVERALL_LAYOUT.value,
                     "summary_table": DrawingPageType.OVERALL_LAYOUT.value}.get(
                         canonical, DrawingPageType.OTHER.value)
        with self.connection() as db:
            cursor = db.execute("""
                INSERT INTO categories
                (project_type, source_type, name, page_type, dimensions,
                 average_embedding, embedding_provider, source_path, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (project_type, source_type, f"{project_type}+{source_type}",
                  page_type, dimension, average, entry.get("embedding_provider"),
                  source_path, json.dumps(metadata or {}, ensure_ascii=False)))
            for name, image in images.items():
                blob, _ = vector_blob(image["embedding"], dimension)
                db.execute("""INSERT INTO samples
                    (category_id, image_name, image_hash, embedding) VALUES (?, ?, ?, ?)""",
                           (cursor.lastrowid, name, image.get("hash"), blob))

    def upsert_sample(self, category: str, image_name: str, embedding,
                      *, image_hash: str | None = None) -> None:
        with self.connection() as db:
            row = db.execute("SELECT id, dimensions FROM categories WHERE name = ?",
                             (category,)).fetchone()
            if row is None:
                raise KeyError(category)
            blob, _ = vector_blob(embedding, row[1])
            db.execute("""INSERT INTO samples (category_id, image_name, image_hash, embedding)
                VALUES (?, ?, ?, ?) ON CONFLICT(category_id, image_name)
                DO UPDATE SET image_hash = excluded.image_hash, embedding = excluded.embedding
            """, (row[0], image_name, image_hash, blob))
            self._refresh_average(db, row[0])

    def delete_sample(self, category: str, image_name: str) -> None:
        with self.connection() as db:
            row = db.execute("SELECT id FROM categories WHERE name = ?", (category,)).fetchone()
            if row is None:
                raise KeyError(category)
            db.execute("DELETE FROM samples WHERE category_id = ? AND image_name = ?",
                       (row[0], image_name))
            self._refresh_average(db, row[0])

    def delete_category(self, category: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM categories WHERE name = ?", (category,))

    @staticmethod
    def _refresh_average(db, category_id: int) -> None:
        rows = db.execute("SELECT embedding FROM samples WHERE category_id = ? ORDER BY id",
                          (category_id,)).fetchall()
        average = None
        if rows:
            average, _ = vector_blob(np.mean(
                [np.frombuffer(row[0], dtype="<f8") for row in rows], axis=0))
        db.execute("UPDATE categories SET average_embedding = ? WHERE id = ?",
                   (average, category_id))

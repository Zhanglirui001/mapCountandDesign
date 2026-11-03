from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from count_and_design.application.ai_statistics.category_vectors import (
    CategoryVectorError,
    bundled_category_index,
    bundled_category_vector_dir,
    load_category_index,
    match_drawing_page,
    score_categories,
)
from count_and_design.application.ai_statistics.models import DrawingPageType
from count_and_design.application.ai_statistics.vector_store import CategoryVectorStore


@pytest.fixture
def store(tmp_path):
    store = CategoryVectorStore(tmp_path / "references.sqlite")
    store.initialize()
    store.import_category("REVAMP", "array_layout", {
        "average_embedding": [1.0, 0.0],
        "images": {"one.png": {"hash": "abc", "embedding": [1.0, 0.0]}},
    }, source_path="revamp.json")
    store.import_category("EVS", "ARRAY LAYOUT", {
        "average_embedding": [0.0, 1.0],
        "images": {"two.png": {"hash": "def", "embedding": [0.0, 1.0]}},
        "embedding_provider": "test-provider",
    }, source_path="evs/hash_map.json")
    return store


def test_project_namespaces_and_page_mapping(store):
    library = load_category_index(store.path)
    assert set(library) == {"REVAMP+array_layout", "EVS+ARRAY LAYOUT"}
    for vector, name in [([1, 0], "REVAMP+array_layout"), ([0, 1], "EVS+ARRAY LAYOUT")]:
        match = match_drawing_page(vector, library, min_score=0.675)
        assert match.category == name
        assert match.page_type == DrawingPageType.ARRAY_LAYOUT.value
    with store.connection() as db:
        assert db.execute("SELECT image_hash FROM samples ORDER BY id").fetchall() == [
            ("abc",), ("def",)]


def test_edits_recompute_average_and_empty_categories_are_skipped(store):
    store.upsert_sample("REVAMP+array_layout", "second.png", [0.0, 1.0])
    library = load_category_index(store.path)
    assert library["REVAMP+array_layout"].sample_count == 2
    np.testing.assert_allclose(library["REVAMP+array_layout"].average,
                               np.array([1, 1]) / np.sqrt(2))
    store.upsert_sample("REVAMP+array_layout", "second.png", [1.0, 1.0])
    assert load_category_index(store.path)["REVAMP+array_layout"].sample_count == 2
    store.delete_sample("REVAMP+array_layout", "second.png")
    np.testing.assert_array_equal(load_category_index(store.path)["REVAMP+array_layout"].average,
                                   [1, 0])
    store.delete_sample("REVAMP+array_layout", "one.png")
    assert set(load_category_index(store.path)) == {"EVS+ARRAY LAYOUT"}
    store.delete_category("EVS+ARRAY LAYOUT")
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM samples").fetchone()[0] == 0


@pytest.mark.parametrize("vector", [[1, 2, 3], [0, 0], [float("nan"), 1]])
def test_invalid_edit_does_not_change_existing_sample(store, vector):
    with pytest.raises(ValueError):
        store.upsert_sample("REVAMP+array_layout", "one.png", vector)
    np.testing.assert_array_equal(load_category_index(store.path)["REVAMP+array_layout"].samples,
                                   [[1, 0]])


def test_import_rolls_back_category_and_samples_on_invalid_vector(store):
    with pytest.raises(ValueError):
        store.import_category("EVS", "bad", {
            "average_embedding": [1, 0],
            "images": {"valid": {"embedding": [1, 0]}, "bad": {"embedding": [1]}},
        }, source_path="bad.json")
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM categories").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM samples").fetchone()[0] == 2


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(CategoryVectorError):
        load_category_index(path)
    assert not path.exists()


def test_revamp_migration_preserves_vectors_and_scores():
    legacy = load_category_index(bundled_category_vector_dir() / "category_index.json")
    merged = bundled_category_index()
    migrated = {name: merged[f"REVAMP+{name}"] for name in legacy}
    for name, original in legacy.items():
        np.testing.assert_array_equal(original.average, migrated[name].average)
        np.testing.assert_array_equal(original.samples, migrated[name].samples)
    query = legacy["array_layout"].samples[0]
    assert score_categories(query, legacy) == score_categories(query, migrated)


def test_packaged_database_integrity_and_provenance():
    path = bundled_category_vector_dir() / "category_index.sqlite"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT DISTINCT dimensions, dtype FROM categories").fetchall() == [
            (2048, "float64-le")]
        rows = db.execute("SELECT project_type, embedding_provider, metadata_json FROM categories "
                          "WHERE source_type != 'summary table'").fetchall()
        assert all(provider == "internal:./models/Qwen3-VL-Embedding-2B|rs1080"
                   for project, provider, _ in rows if project == "EVS")
        assert all(provider is None for project, provider, _ in rows if project == "REVAMP")
        assert all(isinstance(json.loads(metadata), dict) for _, _, metadata in rows)


@pytest.mark.parametrize("project, source_type", [
    ("EVS", "DC COLLECTION SYSTEM SUMMARY"), ("REVAMP", "summary table")])
def test_summary_categories_import_as_overall_layout(store, project, source_type):
    store.import_category(project, source_type, {
        "average_embedding": [1.0, 1.0],
        "images": {"summary.png": {"embedding": [1.0, 1.0]}},
    }, source_path="summary")
    library = load_category_index(store.path)
    match = match_drawing_page([1.0, 1.0], library, min_score=0.675)
    assert match.category == f"{project}+{source_type}"
    assert match.page_type == DrawingPageType.OVERALL_LAYOUT.value


def test_packaged_summary_vectors_and_mapping():
    path = bundled_category_vector_dir() / "category_index.sqlite"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        category_id, page_type, average, provider, metadata = db.execute(
            "SELECT id, page_type, average_embedding, embedding_provider, metadata_json "
            "FROM categories WHERE name = 'REVAMP+summary table'").fetchone()
        rows = db.execute("SELECT embedding, image_hash FROM samples WHERE category_id = ? ORDER BY id",
                          (category_id,)).fetchall()
        assert len(rows) == 4
        assert all(len(image_hash) == 64 for _, image_hash in rows)
        expected = np.mean([np.frombuffer(blob, dtype="<f8") for blob, _ in rows], axis=0)
        np.testing.assert_array_equal(np.frombuffer(average, dtype="<f8"), expected)
        assert page_type == DrawingPageType.OVERALL_LAYOUT.value
        assert provider == "internal:./models/Qwen3-VL-Embedding-2B"
        assert json.loads(metadata)["user_prompt"] == ""
        assert db.execute("SELECT page_type FROM categories WHERE "
                          "source_type = 'DC COLLECTION SYSTEM SUMMARY'").fetchone() == (page_type,)
    library = load_category_index(path)
    match = match_drawing_page(expected, library, min_score=0.675)
    assert match.category == "REVAMP+summary table"
    assert match.page_type == DrawingPageType.OVERALL_LAYOUT.value

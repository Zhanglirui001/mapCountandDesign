"""Embed the PNGs directly inside a directory and add a SQLite category."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from count_and_design.application.ai.embedding import EmbeddingClient
from count_and_design.application.ai_statistics.vector_store import CategoryVectorStore, vector_blob


def add_category(database: Path, images_dir: Path, project_type: str, source_type: str,
                 *, env_path: Path | None = None, client: EmbeddingClient | None = None,
                 output_json: Path | None = None) -> dict:
    project_type = project_type.upper()
    name = f"{project_type}+{source_type}"
    if output_json is not None and output_json.exists():
        raise FileExistsError(output_json)
    paths = sorted(p for p in images_dir.iterdir() if p.is_file() and p.suffix.lower() == ".png")
    if not paths:
        raise ValueError(f"No PNG files found directly inside {images_dir}")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("SELECT 1 FROM categories WHERE name = ?", (name,)).fetchone():
            raise ValueError(f"Category already exists: {name}")
        dimensions = [row[0] for row in db.execute("SELECT DISTINCT dimensions FROM categories")]
        if len(dimensions) != 1:
            raise ValueError("Expected one reference vector dimension in the database")
    client = client or EmbeddingClient.from_env(env_path)
    model = client.resolve_model()
    image_bytes = [p.read_bytes() for p in paths]
    image_sizes = {}
    for path, data in zip(paths, image_bytes):
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image_sizes[path.name] = list(image.size)
    print(f"Embedding {len(paths)} PNGs with {model}; user prompt is empty.", flush=True)
    results = client.embed_images(image_bytes, prompt="", mime_type="image/png", max_concurrent=4,
                                  on_progress=lambda done, total: print(
                                      f"Embedding complete: {done}/{total}", flush=True))
    if len(results) != len(paths):
        raise ValueError("Embedding result count does not match the source images")
    result_models = {result.model or model for result in results}
    if len(result_models) != 1:
        raise ValueError("Embedding results came from different models")
    actual_model = result_models.pop()
    for result in results:
        vector_blob(result.embedding, dimensions[0])
    average = np.mean([result.embedding for result in results], axis=0)
    vector_blob(average, dimensions[0])
    entry = {
        "average_embedding": average.tolist(),
        "embedding_provider": f"internal:{actual_model}",
        "images": {
            path.name: {"hash": hashlib.sha256(data).hexdigest(), "embedding": result.embedding}
            for path, data, result in zip(paths, image_bytes, results)
        },
    }
    metadata = {
        "model": actual_model,
        "system_prompt": client.config.system_prompt,
        "user_prompt": "",
        "preprocessing": "source PNG bytes unchanged",
        "image_sizes": image_sizes,
        "hash_algorithm": "sha256",
        "average_method": "arithmetic mean of raw sample embeddings",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if output_json is not None:
        # Save model results before import so they remain available if SQLite is locked.
        with output_json.open("x", encoding="utf-8") as stream:
            json.dump({**entry, **metadata}, stream, ensure_ascii=False, indent=2,
                      allow_nan=False)
            stream.write("\n")
    CategoryVectorStore(database).import_category(
        project_type, source_type, entry, source_path=str(images_dir.resolve()), metadata=metadata)
    return {"category": name, "samples": len(paths), "dimensions": dimensions[0], "model": actual_model}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--project-type", choices=["REVAMP", "EVS"], required=True)
    parser.add_argument("--source-type", required=True)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--output-json", type=Path,
                        help="Save sample vectors, category mean, and provenance as JSON")
    args = parser.parse_args()
    print(json.dumps(add_category(args.database, args.images, args.project_type, args.source_type,
                                  env_path=args.env, output_json=args.output_json), indent=2))

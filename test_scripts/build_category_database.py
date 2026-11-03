"""Build a portable SQLite reference library from REVAMP and EVS JSON exports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from count_and_design.application.ai_statistics.vector_store import CategoryVectorStore


def build_database(revamp: Path, evs: Path, output: Path) -> dict:
    paths = sorted(evs.glob("*/hash_map.json"))
    if not paths:
        raise ValueError(f"No EVS hash_map.json files found in {evs}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_suffix(".building.sqlite")
    if staging.exists():
        raise FileExistsError(staging)
    store = CategoryVectorStore(staging)
    try:
        store.initialize()
        payload = json.loads(revamp.read_text(encoding="utf-8"))
        metadata = {k: v for k, v in payload.items() if k != "categories"}
        for name, entry in payload["categories"].items():
            store.import_category("REVAMP", name, entry,
                                  source_path=str(revamp), metadata=metadata)
        for path in paths:
            entry = json.loads(path.read_text(encoding="utf-8"))
            metadata = {k: v for k, v in entry.items()
                        if k not in {"images", "average_embedding"}}
            store.import_category("EVS", path.parent.name, entry,
                                  source_path=str(path), metadata=metadata)
        with store.connection() as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
            if len(db.execute("SELECT DISTINCT dimensions FROM categories").fetchall()) != 1:
                raise ValueError("Reference libraries have incompatible dimensions")
            summary = {
                project: {"categories": categories, "samples": samples}
                for project, categories, samples in db.execute("""
                    SELECT c.project_type, count(DISTINCT c.id), count(s.id)
                    FROM categories c JOIN samples s ON s.category_id = c.id
                    GROUP BY c.project_type
                """)
            }
        staging.replace(output)
        return summary
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revamp", type=Path, required=True)
    parser.add_argument("--evs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_database(args.revamp, args.evs, args.output), indent=2))

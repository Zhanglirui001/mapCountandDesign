"""Project persistence service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..version_manager import VersionManager
from ...file_integrity import (
    IntegrityStatus,
    attach_file_integrity,
    atomic_write_json,
    verify_file_integrity,
)
from ...models import Project


@dataclass(frozen=True)
class LoadedProject:
    project: Project
    path: Path
    version_tree: dict[str, Any] | None
    integrity_status: IntegrityStatus
    negative_box_position_count: int = 0

    def __iter__(self):
        """Keep the historical ``project, path = open_project(...)`` API working."""
        yield self.project
        yield self.path


class ProjectPersistenceService:
    """Handles project lifecycle and file persistence."""

    def new_project(self) -> Project:
        return Project(name="New Project")

    def open_project(self, path: str | Path) -> LoadedProject:
        file_path = Path(path)
        data = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Project file root must be a JSON object")
        negative_box_position_count = sum(
            1
            for inv in data.get("invs", [])
            if isinstance(inv, dict)
            for node in inv.get("nodes", [])
            if isinstance(node, dict)
            and _is_negative_number(node.get("box_position", 0.0))
        )
        return LoadedProject(
            project=Project.from_dict(data),
            path=file_path,
            version_tree=(
                data.get("version_tree")
                if isinstance(data.get("version_tree"), dict) else None
            ),
            integrity_status=verify_file_integrity(data),
            negative_box_position_count=negative_box_position_count,
        )

    def build_payload(
        self,
        project: Project,
        version_manager: VersionManager | None = None,
    ) -> dict[str, Any]:
        data = project.to_dict()
        if version_manager is not None:
            data["version_tree"] = version_manager.to_dict()
        return attach_file_integrity(data)

    def save_project(
        self,
        project: Project,
        path: str | Path,
        version_manager: VersionManager | None = None,
    ) -> Path:
        file_path = Path(path)
        return atomic_write_json(
            file_path,
            self.build_payload(project, version_manager),
        )

    def load_version_tree(self, path: str | Path) -> dict[str, Any] | None:
        """Extract the version tree from a saved project file, if present."""
        file_path = Path(path)
        with open(file_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("version_tree")


def _is_negative_number(value: object) -> bool:
    try:
        return float(value) < 0
    except (TypeError, ValueError):
        return False

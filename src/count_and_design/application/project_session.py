"""Stateful application session for a project and its version history."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..models import Inv, Project
from ..file_integrity import IntegrityStatus
from .core.persistence import ProjectPersistenceService
from .version_manager import VersionManager


class NegativeBoxPositionError(ValueError):
    """Raised before opening a project that needs Box Position repair."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(f"Project contains {count} negative Box Position value(s)")


class ProjectSession:
    """Own the current project, file path, persistence, and version tree."""

    def __init__(
        self,
        translate: Callable[[str, str], str],
        persistence: ProjectPersistenceService | None = None,
    ) -> None:
        self._translate = translate
        self._persistence = persistence or ProjectPersistenceService()
        self._project = self._persistence.new_project()
        self._current_file: Path | None = None
        self._version_manager = VersionManager(translate)

    @property
    def project(self) -> Project:
        return self._project

    @property
    def current_file(self) -> Path | None:
        return self._current_file

    @property
    def version_manager(self) -> VersionManager:
        return self._version_manager

    def replace_project(self, project: Project) -> None:
        """Replace the in-memory project without changing file/history state."""
        self._project = project

    def replace_version_manager(self, version_manager: VersionManager) -> None:
        """Replace version history when loading or restoring a session."""
        self._version_manager = version_manager

    def new_project(self) -> None:
        self._project = self._persistence.new_project()
        self._current_file = None
        self._version_manager.clear()

    def open_project(
        self,
        path: str,
        *,
        normalize_negative_box_positions: bool = False,
    ) -> IntegrityStatus:
        loaded = self._persistence.open_project(path)
        if (
            loaded.negative_box_position_count > 0
            and not normalize_negative_box_positions
        ):
            raise NegativeBoxPositionError(loaded.negative_box_position_count)
        self._project = loaded.project
        self._current_file = loaded.path
        if loaded.integrity_status is IntegrityStatus.MODIFIED:
            self._version_manager = VersionManager(self._translate)
        elif loaded.version_tree:
            self._version_manager = VersionManager.from_dict(
                loaded.version_tree,
                self._translate,
                lock_legacy=int(loaded.version_tree.get("history_format", 1)) < 2,
            )
            if loaded.integrity_status is IntegrityStatus.LEGACY_UNSIGNED:
                self._version_manager.lock_existing_history()
        else:
            self._version_manager = VersionManager(self._translate)
        return loaded.integrity_status

    def save_project(self) -> None:
        if self._current_file is None:
            raise ValueError("No save path selected")
        self._current_file = self._persistence.save_project(
            self._project,
            self._current_file,
            self._version_manager,
        )

    def save_project_as(self, path: str) -> None:
        self._current_file = self._persistence.save_project(
            self._project,
            path,
            self._version_manager,
        )

    def checkout_node(self, node_id: str) -> bool:
        if not self._version_manager.can_navigate_to(node_id):
            return False
        self._project = self._version_manager.checkout(node_id, self._project)
        return True

    def add_inv(self, name: str) -> None:
        pre = self._project.to_dict()
        self._project.invs.append(Inv(name=name))
        self._version_manager.commit(
            pre,
            self._project,
            "version.add_inv",
        )

    def commit(self, previous: dict[str, Any], description: str) -> None:
        self._version_manager.commit(previous, self._project, description)

    def build_recovery_payload(self) -> dict[str, Any]:
        return self._persistence.build_payload(
            self._project,
            self._version_manager,
        )

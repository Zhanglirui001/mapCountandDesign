"""State-changing workflow around the data-fill solver."""

from __future__ import annotations

import copy
from collections.abc import Callable

from ...models import Project
from ..version_manager import VersionManager
from .service import (
    DataFillConstraints,
    DataFillPrepared,
    DataFillResult,
    DataFillService,
    FAKE_INV_NAME,
)


class DataFillWorkflow:
    """Prepare, solve, and apply aggregate data-fill operations."""

    def __init__(self, service: DataFillService | None = None) -> None:
        self._service = service or DataFillService()

    def prepare(self, project: Project) -> DataFillPrepared:
        return self._service.prepare(project)

    def solve(
        self,
        project: Project,
        constraints: DataFillConstraints,
        progress_callback: Callable[[float], None] | None = None,
    ) -> DataFillResult:
        return self._service.solve(
            project,
            constraints,
            progress_callback=progress_callback,
        )

    def cancel(self) -> None:
        self._service.cancel()

    def apply(
        self,
        project: Project,
        version_manager: VersionManager,
        result: DataFillResult,
    ) -> None:
        pre = project.to_dict()
        project.data_fill_targets = copy.deepcopy(list(result.constraints.targets))
        project.invs = [inv for inv in project.invs if inv.name != FAKE_INV_NAME]
        fake = self._service.build_fake_inv(result)
        if fake is not None:
            project.invs.append(fake)
        project.rebind_length_identifiers()
        version_manager.commit(pre, project, "version.fill_data")

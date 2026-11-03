"""Application-level view model."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QObject, Signal

from ...application.ai_statistics import import_agent4_payload_to_project
from ...application.export_workflow import ExportWorkflow
from ...application.product_generation import (
    ProductGenerationResult,
    SchematicExportResult,
)
from ...application.project_session import ProjectSession
from ...application.catalog import ProductPresetService, PVPanelService, RackingsService
from ...application.data_fill import (
    DataFillConstraints,
    DataFillPrepared,
    DataFillResult,
    DataFillWorkflow,
)
from ...application.project import (
    ProjectStatsService,
    ProjectTreeService,
    ValidationService,
)
from ...application.ai.ocr.screenshot import DeepSeekOcrService
from ...application.version_manager import VersionManager
from ... import __version__
from ...i18n import tr
from ...models import Project
from ...file_integrity import IntegrityStatus
from .count_tab import CountTabViewModel
from .history_tab import HistoryTabViewModel
from .length_identifiers_tab import LengthIdentifiersTabViewModel
from .product_presets_tab import ProductPresetsTabViewModel
from .pv_panels_tab import PVPanelsTabViewModel
from .rackings_tab import RackingsTabViewModel
from .states import AppState
from .tab_coordinator import TabCoordinator


class AppViewModel(QObject):
    """Coordinates the main window and tab view models."""

    state_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        validation_service = ValidationService()
        product_preset_service = ProductPresetService()
        pv_panel_service = PVPanelService()
        rackings_service = RackingsService(validation_service)
        tree_service = ProjectTreeService()
        stats_service = ProjectStatsService()
        self.ocr_service = DeepSeekOcrService()

        self._session = ProjectSession(tr)
        self._data_fill_workflow = DataFillWorkflow()
        self._export_workflow = ExportWorkflow()
        self.count_tab = CountTabViewModel(
            tree_service,
            stats_service,
            rackings_service,
            product_preset_service,
        )
        self.length_identifiers_tab = LengthIdentifiersTabViewModel()
        self.rackings_tab = RackingsTabViewModel(rackings_service, product_preset_service)
        self.product_presets_tab = ProductPresetsTabViewModel(product_preset_service, pv_panel_service)
        self.pv_panels_tab = PVPanelsTabViewModel(pv_panel_service)
        self.history_tab = HistoryTabViewModel()
        self._state = AppState()
        self._tab_coordinator = TabCoordinator(
            (
                self.count_tab,
                self.length_identifiers_tab,
                self.rackings_tab,
                self.product_presets_tab,
                self.pv_panels_tab,
            ),
            self.history_tab,
            self.count_tab,
            pv_panel_service,
            product_preset_service,
            self._emit_state,
            self._on_project_edited,
        )
        self._push_project()

    @property
    def state(self) -> AppState:
        return self._state

    @property
    def version_manager(self) -> VersionManager:
        return self._session.version_manager

    @property
    def project(self) -> Project:
        return self._session.project

    @property
    def current_file(self) -> str | None:
        path = self._session.current_file
        return str(path) if path is not None else None

    # Compatibility shims for existing tests and extensions. New callers
    # should use ``project`` / ``version_manager`` and synchronize_project().
    @property
    def _project(self) -> Project:
        return self._session.project

    @_project.setter
    def _project(self, project: Project) -> None:
        self._session.replace_project(project)

    @property
    def _version_manager(self) -> VersionManager:
        return self._session.version_manager

    @_version_manager.setter
    def _version_manager(self, version_manager: VersionManager) -> None:
        self._session.replace_version_manager(version_manager)

    # -- version navigation ----------------------------------------------

    def checkout_node(self, node_id: str) -> None:
        """Navigate to a specific version node in the operation tree.

        If *node_id* is not a direct child of the current node, this
        creates a fork (new branch) when the next operation is committed.
        """
        if self._session.checkout_node(node_id):
            self._push_project()

    # -- file operations ------------------------------------------------

    def new_project(self) -> None:
        self._session.new_project()
        self._push_project()

    def open_project(
        self,
        path: str,
        *,
        normalize_negative_box_positions: bool = False,
    ) -> IntegrityStatus:
        integrity_status = self._session.open_project(
            path,
            normalize_negative_box_positions=normalize_negative_box_positions,
        )
        self._push_project()
        return integrity_status

    def save_project(self) -> None:
        if self._session.current_file is None:
            raise ValueError(tr("app.no_save_path", "No save path selected"))
        self._session.save_project()
        self._emit_state()

    def save_project_as(self, path: str) -> None:
        self._session.save_project_as(path)
        self._emit_state()

    def add_inv(self, name: str) -> None:
        self._session.add_inv(name)
        self._push_project()

    def import_ai_statistics_payload(self, payload: Mapping[str, Any]) -> None:
        pre = self.project.to_dict()
        import_agent4_payload_to_project(self.project, payload)
        self._session.commit(pre, "version.import_ai_statistics")
        self._push_project()
        self._emit_state()

    # -- aggregate data fill -------------------------------------------

    def prepare_data_fill(self) -> DataFillPrepared:
        return self._data_fill_workflow.prepare(self.project)

    def solve_data_fill(
        self,
        constraints: DataFillConstraints,
        progress_callback: Callable[[float], None] | None = None,
    ) -> DataFillResult:
        return self._data_fill_workflow.solve(
            self.project,
            constraints,
            progress_callback=progress_callback,
        )

    def cancel_data_fill(self) -> None:
        self._data_fill_workflow.cancel()

    def apply_data_fill(self, result: DataFillResult) -> None:
        self._data_fill_workflow.apply(
            self.project,
            self.version_manager,
            result,
        )
        self._push_project()

    def generate_products(
        self,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> ProductGenerationResult:
        return self._export_workflow.generate_products(
            self.project,
            progress_callback=progress_callback,
        )

    def export_products_to_xlsx(
        self,
        result: ProductGenerationResult,
        path: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        return self._export_workflow.export_products_to_xlsx(
            result,
            self.project,
            path,
            progress_callback=progress_callback,
        )

    def export_correspondence_table(
        self,
        result: ProductGenerationResult,
        directory: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        return self._export_workflow.export_correspondence_table(
            result,
            self.project,
            directory,
            progress_callback=progress_callback,
        )

    def export_voltage_drop(
        self,
        result: ProductGenerationResult,
        directory: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        return self._export_workflow.export_voltage_drop(
            result,
            self.project,
            directory,
            progress_callback=progress_callback,
        )

    def export_schematics(
        self,
        result: ProductGenerationResult,
        directory: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> SchematicExportResult:
        return self._export_workflow.export_schematics(
            result,
            self.project,
            directory,
            progress_callback=progress_callback,
        )

    def on_tab_changed(self, index: int) -> None:
        self._tab_coordinator.refresh_tab(index)

    def refresh_all_tabs(self) -> None:
        """Refresh all tabs after a merge operation."""
        pre = self.project.to_dict()
        self.project.rebind_length_identifiers()
        self._session.commit(pre, "version.merge")
        self._push_project()

    def synchronize_project(self) -> None:
        """Synchronize the current project with all tab view models."""
        self._push_project()

    def refresh_window_state(self) -> None:
        """Rebuild translated window state without changing the project."""
        self._emit_state()

    def build_recovery_payload(self) -> dict[str, Any]:
        return self._session.build_recovery_payload()

    def _push_project(self) -> None:
        self._tab_coordinator.synchronize(self.project, self.version_manager)

    def _on_project_edited(self) -> None:
        """Keep voltage diagnostics synchronized with edits made in any tab."""
        self.count_tab.project_changed()
        self._emit_state()

    def _emit_state(self) -> None:
        title = f"{tr('app.title', 'Count and Design')} v{__version__}"
        if self.project.name:
            title = f"{title} - {self.project.name}"
        self._state = AppState(
            title=title,
            current_file=self.current_file,
        )
        self.state_changed.emit(self._state)

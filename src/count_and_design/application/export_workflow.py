"""Application workflow for product generation and report exports."""

from __future__ import annotations

from collections.abc import Callable

from ..models import Project
from .product_generation import (
    ProductGenerationResult,
    ProductGenerationService,
    SchematicExportResult,
    export_correspondence_table,
    export_schematics,
    export_to_xlsx,
)
from .voltage_drop import VoltageDropService, export_voltage_drop_to_xlsx


class ExportWorkflow:
    """Coordinate read-only product generation and export use cases."""

    def __init__(
        self,
        product_generation: ProductGenerationService | None = None,
        voltage_drop: VoltageDropService | None = None,
    ) -> None:
        self._product_generation = product_generation or ProductGenerationService()
        self._voltage_drop = voltage_drop or VoltageDropService()

    def generate_products(
        self,
        project: Project,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> ProductGenerationResult:
        return self._product_generation.build_products(
            project,
            progress_callback=progress_callback,
        )

    def export_products_to_xlsx(
        self,
        result: ProductGenerationResult,
        project: Project,
        path: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        return export_to_xlsx(
            result,
            project,
            path,
            progress_callback=progress_callback,
        )

    def export_correspondence_table(
        self,
        result: ProductGenerationResult,
        project: Project,
        directory: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        return export_correspondence_table(
            result,
            project,
            directory,
            progress_callback=progress_callback,
        )

    def export_voltage_drop(
        self,
        result: ProductGenerationResult,
        project: Project,
        directory: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        report = self._voltage_drop.calculate(project, result)
        return export_voltage_drop_to_xlsx(
            report,
            project,
            directory,
            progress_callback=progress_callback,
        )

    def export_schematics(
        self,
        result: ProductGenerationResult,
        project: Project,
        directory: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> SchematicExportResult:
        return export_schematics(
            result,
            project,
            directory,
            progress_callback=progress_callback,
        )

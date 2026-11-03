"""GUI orchestration for product generation and report exports."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
    QStatusBar,
    QWidget,
)

from ...application.voltage_drop import VoltageDropValidationError
from ...i18n import tr
from ..viewmodels import AppViewModel
from ..views.error_dialog import ErrorDialog


class ProductExportController:
    """Run the multi-stage product export flow while updating the GUI."""

    def __init__(
        self,
        parent: QWidget,
        status_bar: QStatusBar,
        view_model: AppViewModel,
    ) -> None:
        self._parent = parent
        self._status_bar = status_bar
        self._view_model = view_model

    def run(self) -> None:
        self._status_bar.showMessage(
            tr("dialog.generating_msg", "Generating products, please wait...")
        )
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()

        def on_generate_progress(current: int, total: int) -> None:
            self._show_progress(
                "dialog.generating_progress",
                "Generating products, please wait... ({current}/{total})",
                current,
                total,
            )

        try:
            result = self._view_model.generate_products(
                progress_callback=on_generate_progress,
            )
        except Exception:
            self._restore_cursor()
            ErrorDialog.show_exception(
                message=tr(
                    "error.generate_products",
                    "Failed to generate products.",
                ),
                parent=self._parent,
            )
            return

        QApplication.restoreOverrideCursor()
        total_parts = len(result.parts) + len(result.semi_parts)
        self._status_bar.showMessage(
            tr(
                "dialog.generated_status",
                "Generated {ibex} IBEX, {lynx} LYNX, {he} Homerun Extender, "
                "{ge} Gap Extender products ({parts} parts)",
            ).format(
                ibex=len(result.ibex_products),
                lynx=len(result.lynx_products),
                he=len(result.homerun_extender_products),
                ge=len(result.gap_extender_products),
                parts=total_parts,
            ),
            8000,
        )

        directory = QFileDialog.getExistingDirectory(
            self._parent,
            tr("dialog.export_dir_title", "Select Export Directory"),
            "",
        )
        if not directory:
            return

        self._status_bar.showMessage(
            tr(
                "dialog.exporting_msg",
                "Exporting products to Excel, please wait...",
            )
        )
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        base = os.path.join(directory, "generated_products")
        os.makedirs(base, exist_ok=True)

        try:
            xlsx_path = self._view_model.export_products_to_xlsx(
                result,
                os.path.join(base, "parts_and_semi_parts.xlsx"),
                progress_callback=lambda current, total: self._show_progress(
                    "dialog.exporting_progress",
                    "Exporting products to Excel, please wait... ({current}/{total})",
                    current,
                    total,
                ),
            )
            correspondence_path = self._view_model.export_correspondence_table(
                result,
                base,
                progress_callback=lambda current, total: self._show_progress(
                    "dialog.exporting_correspondence_progress",
                    "Exporting correspondence table, please wait... "
                    "({current}/{total})",
                    current,
                    total,
                ),
            )
        except Exception:
            self._restore_cursor()
            ErrorDialog.show_exception(
                message=tr(
                    "error.export_products",
                    "Failed to export products.",
                ),
                parent=self._parent,
            )
            return

        schematic_result = None
        schematic_error = ""
        try:
            schematic_result = self._view_model.export_schematics(
                result,
                os.path.join(base, "schematics"),
                progress_callback=lambda current, total: self._show_progress(
                    "dialog.exporting_schematics_progress",
                    "Generating SVG schematics, please wait... "
                    "({current}/{total})",
                    current,
                    total,
                ),
            )
        except Exception as exc:
            schematic_error = str(exc)

        voltage_path = None
        voltage_error = ""
        try:
            self._show_progress(
                "dialog.exporting_voltage_drop_progress",
                "Exporting voltage drop report, please wait... ({current}/{total})",
                0,
                5,
            )
            voltage_path = self._view_model.export_voltage_drop(
                result,
                base,
                progress_callback=lambda current, total: self._show_progress(
                    "dialog.exporting_voltage_drop_progress",
                    "Exporting voltage drop report, please wait... "
                    "({current}/{total})",
                    current,
                    total,
                ),
            )
        except VoltageDropValidationError as exc:
            voltage_error = str(exc)
        except Exception as exc:
            voltage_error = str(exc)

        self._restore_cursor()
        message = tr(
            "dialog.exported_msg",
            "Products exported successfully to:\n{path}\n\n"
            "- {xlsx_name} (product drawing sheets)\n"
            "- {corr_name} (correspondence table)\n"
            "- ibex_products: {ip} products\n"
            "- lynx_products: {lp} products",
        ).format(
            path=base,
            xlsx_name=os.path.basename(xlsx_path),
            corr_name=os.path.basename(correspondence_path),
            ip=len(result.ibex_products),
            lp=len(result.lynx_products),
        )
        if voltage_path:
            message += tr(
                "dialog.voltage_drop_exported",
                "\n- {name} (voltage drop report)",
            ).format(name=os.path.basename(voltage_path))
        if schematic_result is not None:
            message += tr(
                "dialog.schematics_exported",
                "\n- schematics ({count} SVG variant(s))",
            ).format(count=schematic_result.variant_count)
        QMessageBox.information(
            self._parent,
            tr("dialog.exported_title", "Export Successful"),
            message,
        )
        if voltage_error:
            QMessageBox.warning(
                self._parent,
                tr(
                    "dialog.voltage_drop_not_exported_title",
                    "Voltage Drop Report Not Exported",
                ),
                tr(
                    "dialog.voltage_drop_not_exported",
                    "The product files were exported, but the voltage drop "
                    "report was not created:\n{detail}",
                ).format(detail=voltage_error),
            )
        if schematic_error:
            QMessageBox.warning(
                self._parent,
                tr(
                    "dialog.schematics_not_exported_title",
                    "SVG Schematics Not Exported",
                ),
                tr(
                    "dialog.schematics_not_exported",
                    "The product files were exported, but SVG schematics "
                    "were not created:\n{detail}",
                ).format(detail=schematic_error),
            )
        elif schematic_result is not None and schematic_result.warnings:
            shown = schematic_result.warnings[:10]
            warning_text = "\n".join(f"- {warning}" for warning in shown)
            remaining = len(schematic_result.warnings) - len(shown)
            if remaining > 0:
                warning_text += tr(
                    "dialog.schematics_more_warnings",
                    "\n... and {count} more warning(s).",
                ).format(count=remaining)
            QMessageBox.warning(
                self._parent,
                tr(
                    "dialog.schematics_warnings_title",
                    "SVG Schematics Warnings",
                ),
                tr(
                    "dialog.schematics_warnings",
                    "SVG schematics were created with the following "
                    "warnings:\n{detail}",
                ).format(detail=warning_text),
            )

    def _show_progress(
        self,
        key: str,
        fallback: str,
        current: int,
        total: int,
    ) -> None:
        self._status_bar.showMessage(
            tr(key, fallback).format(current=current, total=total)
        )
        QApplication.processEvents()

    def _restore_cursor(self) -> None:
        QApplication.restoreOverrideCursor()
        self._status_bar.clearMessage()

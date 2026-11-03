"""Screenshot capture and OCR workflow for the main window."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QBuffer, QIODevice, QTimer
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QStatusBar

from ...i18n import tr
from ..views.screenshot_ocr import OcrWorkerThread, ScreenshotRegionOverlay


class ScreenshotOcrController:
    """Own screenshot overlay and OCR worker lifecycles."""

    def __init__(
        self,
        parent: QMainWindow,
        status_bar: QStatusBar,
        action: QAction,
        ocr_service: Any,
    ) -> None:
        self._parent = parent
        self._status_bar = status_bar
        self._action = action
        self._ocr_service = ocr_service
        self._overlay: ScreenshotRegionOverlay | None = None
        self._thread: OcrWorkerThread | None = None

    def set_action(self, action: QAction) -> None:
        self._action = action

    def start(self) -> None:
        self._action.setEnabled(False)
        self._status_bar.showMessage(
            tr("count_tab.ocr_select_region", "Select a screen region...")
        )
        self._parent.hide()
        QTimer.singleShot(200, self._show_overlay)

    def _show_overlay(self) -> None:
        try:
            self._overlay = ScreenshotRegionOverlay()
        except Exception as exc:
            self._restore_window()
            self._set_ready(tr("count_tab.ocr_failed", "OCR failed."))
            QMessageBox.warning(
                self._parent,
                tr("count_tab.ocr_failed_title", "OCR Failed"),
                str(exc),
            )
            return
        self._overlay.region_selected.connect(self._on_region_selected)
        self._overlay.cancelled.connect(self._on_cancelled)
        self._overlay.show()

    def _on_cancelled(self) -> None:
        self._restore_window()
        self._overlay = None
        self._set_ready(tr("count_tab.ocr_cancelled", "OCR cancelled."))

    def _on_region_selected(self, pixmap: QPixmap) -> None:
        self._restore_window()
        self._overlay = None
        image_bytes = self._pixmap_to_png_bytes(pixmap)
        if not image_bytes:
            self._set_ready(
                tr(
                    "count_tab.ocr_empty_selection",
                    "No screenshot was captured.",
                )
            )
            return
        self._status_bar.showMessage(
            tr("count_tab.ocr_running", "Running OCR...")
        )
        self._thread = OcrWorkerThread(
            self._ocr_service,
            image_bytes,
            self._parent,
        )
        self._thread.finished_with_result.connect(self._on_finished)
        self._thread.error_msg.connect(self._on_failed)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def _on_finished(self, result: Any) -> None:
        QApplication.clipboard().setText(result.text)
        if result.is_table:
            message = tr(
                "count_tab.ocr_table_copied",
                "Table copied as TSV. Paste it directly into Excel.",
            )
        else:
            message = tr(
                "count_tab.ocr_text_copied",
                "Text copied to clipboard.",
            )
        self._set_ready(message)
        QMessageBox.information(
            self._parent,
            tr("count_tab.ocr_done_title", "OCR Complete"),
            message,
        )

    def _on_failed(self, message: str) -> None:
        self._set_ready(tr("count_tab.ocr_failed", "OCR failed."))
        QMessageBox.warning(
            self._parent,
            tr("count_tab.ocr_failed_title", "OCR Failed"),
            message,
        )

    def _cleanup_thread(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None

    def _restore_window(self) -> None:
        self._parent.show()
        self._parent.raise_()
        self._parent.activateWindow()

    def _set_ready(self, status: str) -> None:
        self._action.setEnabled(True)
        self._status_bar.showMessage(status, 5000)

    @staticmethod
    def _pixmap_to_png_bytes(pixmap: QPixmap) -> bytes:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not pixmap.save(buffer, "PNG"):
            return b""
        return bytes(buffer.data())

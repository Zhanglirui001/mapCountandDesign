"""Screenshot selection and OCR worker helpers."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ...application.ai.ocr.screenshot import DeepSeekOcrService, OcrResult


class ScreenshotRegionOverlay(QWidget):
    """Fullscreen overlay for selecting a screenshot region."""

    region_selected = Signal(QPixmap)
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        screens = QGuiApplication.screens()
        if not screens:
            raise RuntimeError("No screen is available for screenshot capture.")
        self._screen_geometry = self._virtual_desktop_geometry(screens)
        self._screenshot = self._capture_all_screens(screens)
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setWindowState(Qt.WindowState.WindowFullScreen)
        self.setGeometry(self._screen_geometry)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

    @staticmethod
    def _virtual_desktop_geometry(screens) -> QRect:
        """Return the bounding rectangle that encompasses all screens."""
        rect = screens[0].geometry()
        for s in screens[1:]:
            rect = rect.united(s.geometry())
        return rect

    @staticmethod
    def _capture_all_screens(screens) -> QPixmap:
        """Grab each screen and stitch the captures into one pixmap."""
        virtual_rect = ScreenshotRegionOverlay._virtual_desktop_geometry(screens)
        composite = QPixmap(virtual_rect.size())
        composite.fill(Qt.GlobalColor.black)
        painter = QPainter(composite)
        origin = virtual_rect.topLeft()
        for s in screens:
            offset = s.geometry().topLeft() - origin
            painter.drawPixmap(offset, s.grabWindow(0))
        painter.end()
        return composite

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._origin = event.position().toPoint()
        self._current = self._origin
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._origin is None:
            return
        self._current = event.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        self._current = event.position().toPoint()
        rect = self._selection_rect()
        self._origin = None
        self._current = None
        if rect.width() < 5 or rect.height() < 5:
            self.cancelled.emit()
            self.close()
            return
        cropped = self._screenshot.copy(rect)
        self.region_selected.emit(cropped)
        self.close()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self._screenshot)
        painter.fillRect(self.rect(), QBrush(QColor(0, 0, 0, 120)))
        rect = self._selection_rect()
        if not rect.isNull():
            painter.drawPixmap(rect, self._screenshot, rect)
            painter.setPen(QPen(QColor(74, 144, 217), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(0, 0, -1, -1))

    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        self.activateWindow()
        self.raise_()
        self.setFocus()

    def _selection_rect(self) -> QRect:
        if self._origin is None or self._current is None:
            return QRect()
        return QRect(self._origin, self._current).normalized().intersected(self.rect())


class OcrWorkerThread(QThread):
    """Runs OCR recognition in a background thread."""

    finished_with_result = Signal(object)
    error_msg = Signal(str)

    def __init__(self, service: DeepSeekOcrService, image_bytes: bytes, parent=None) -> None:
        super().__init__(parent)
        self._service = service
        self._image_bytes = image_bytes

    def run(self) -> None:
        try:
            result: OcrResult = self._service.recognize(self._image_bytes)
        except Exception as exc:
            self.error_msg.emit(str(exc))
            return
        self.finished_with_result.emit(result)

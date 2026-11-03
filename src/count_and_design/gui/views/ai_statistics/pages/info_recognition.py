"""Info-recognition stage view."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from dataclasses import dataclass, replace

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHeaderView,
    QHBoxLayout, QLabel, QMessageBox, QScrollArea, QSizePolicy, QPushButton,
    QInputDialog,
    QSplitter,
    QTableWidget,
    QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from .....application.ai_statistics import (
    InvOcrRow,
    NumStrDetail,
    OverallSiteExtraction,
    TrackerOcrFields,
)
from .....i18n import tr
from ..widgets import WorkflowStagePage, empty_panel
from .orientation_options import (
    PANEL_ORIENTATION_OPTIONS,
    panel_orientation_text,
)


_LAST_VALID_COUNT_ROLE = Qt.ItemDataRole.UserRole

_TABLE_OVERLAY_COLORS = {
    "tracker_table": "#a855f7",
    "inv_tree": "#f97316",
    "inv_companion_roi": "#eab308",
    "table": "#22c55e",
}


@dataclass(frozen=True)
class _TableBBoxOverlay:
    bbox: tuple[int, int, int, int]
    label: str
    speculative: bool = False


def _table_bbox_overlays_from_result(
    result: OverallSiteExtraction,
) -> tuple[_TableBBoxOverlay, ...]:
    raw = result.raw
    bboxes = raw.get("table_bboxes")
    if not isinstance(bboxes, list):
        return ()
    labels = raw.get("table_bbox_labels")
    label_items = labels if isinstance(labels, list) else []
    overlays: list[_TableBBoxOverlay] = []
    for index, bbox in enumerate(bboxes):
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        try:
            parsed = tuple(int(value) for value in bbox)
        except (TypeError, ValueError):
            continue
        label = (
            str(label_items[index])
            if index < len(label_items)
            else "table"
        )
        normalized = label.strip().casefold()
        overlays.append(
            _TableBBoxOverlay(
                bbox=parsed,
                label=label,
                speculative=normalized == "inv_companion_roi",
            )
        )
    return tuple(overlays)


class _PageImageScrollArea(QScrollArea):
    """Show one drawing at native preview size or fitted to the viewport."""

    def __init__(self, pixmap: QPixmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source_pixmap = pixmap
        self._table_overlays: tuple[_TableBBoxOverlay, ...] = ()
        self._fit_to_page = False
        self._pan_origin = None
        self._pan_values = (0, 0)
        self._scale = min(
            1.0,
            1100 / max(1, pixmap.width()),
            1400 / max(1, pixmap.height()),
        )
        self.image_label = QLabel()
        self.image_label.setObjectName("overallPageImage")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWidget(self.image_label)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.viewport().installEventFilter(self)
        self.image_label.installEventFilter(self)
        self.set_fit_to_page(False)

    @property
    def fit_to_page(self) -> bool:
        return self._fit_to_page

    @property
    def scale_factor(self) -> float:
        return self._scale

    def set_fit_to_page(self, enabled: bool) -> None:
        self._fit_to_page = enabled
        policy = (Qt.ScrollBarPolicy.ScrollBarAlwaysOff if enabled
                  else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(policy)
        self.setVerticalScrollBarPolicy(policy)
        self.setWidgetResizable(enabled)
        self.viewport().setCursor(
            Qt.CursorShape.ArrowCursor
            if enabled
            else Qt.CursorShape.OpenHandCursor
        )
        self._refresh_pixmap()

    def zoom(self, factor: float) -> None:
        if self._fit_to_page:
            self.set_fit_to_page(False)
        self._scale = max(0.1, min(8.0, self._scale * factor))
        self._refresh_pixmap()

    def zoom_at(self, factor: float, position) -> None:
        """Zoom while keeping the drawing point under the cursor stable."""
        old_scale = self._scale
        old_x = self.horizontalScrollBar().value()
        old_y = self.verticalScrollBar().value()
        self.zoom(factor)
        applied = self._scale / max(0.001, old_scale)
        self.horizontalScrollBar().setValue(
            int((old_x + position.x()) * applied - position.x())
        )
        self.verticalScrollBar().setValue(
            int((old_y + position.y()) * applied - position.y())
        )

    def actual_size(self) -> None:
        self.set_fit_to_page(False)
        self._scale = 1.0
        self._refresh_pixmap()

    def set_table_overlays(
        self,
        overlays: Sequence[_TableBBoxOverlay],
    ) -> None:
        self._table_overlays = tuple(overlays)
        self._refresh_pixmap()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if event.type() == QEvent.Type.Wheel:
            point = event.position().toPoint()
            if watched is self.image_label:
                point = self.image_label.mapTo(self.viewport(), point)
            self.zoom_at(
                1.2 if event.angleDelta().y() > 0 else 1 / 1.2,
                point,
            )
            event.accept()
            return True
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
            and not self._fit_to_page
        ):
            self._pan_origin = event.globalPosition().toPoint()
            self._pan_values = (
                self.horizontalScrollBar().value(),
                self.verticalScrollBar().value(),
            )
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return True
        if (
            event.type() == QEvent.Type.MouseMove
            and self._pan_origin is not None
        ):
            delta = event.globalPosition().toPoint() - self._pan_origin
            self.horizontalScrollBar().setValue(self._pan_values[0] - delta.x())
            self.verticalScrollBar().setValue(self._pan_values[1] - delta.y())
            event.accept()
            return True
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and self._pan_origin is not None
        ):
            self._pan_origin = None
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if self._fit_to_page:
            self._refresh_pixmap()

    def _refresh_pixmap(self) -> None:
        if self._source_pixmap.isNull():
            return
        if self._fit_to_page:
            target = self.viewport().size()
            target.setWidth(max(1, target.width() - 20))
            target.setHeight(max(1, target.height() - 20))
            pixmap = self._source_pixmap.scaled(
                target, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._scale = pixmap.width() / max(1, self._source_pixmap.width())
            self.image_label.setMinimumSize(1, 1)
        else:
            size = self._source_pixmap.size() * self._scale
            pixmap = self._source_pixmap.scaled(
                size, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.image_label.resize(pixmap.size())
        if self._table_overlays:
            pixmap = self._draw_table_overlays(pixmap)
        self.image_label.setPixmap(pixmap)

    def _draw_table_overlays(self, pixmap: QPixmap) -> QPixmap:
        if pixmap.isNull() or not self._table_overlays:
            return pixmap
        annotated = QPixmap(pixmap)
        painter = QPainter(annotated)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        scale = self._scale
        for overlay in self._table_overlays:
            x1, y1, x2, y2 = overlay.bbox
            rect_x = int(round(x1 * scale))
            rect_y = int(round(y1 * scale))
            rect_w = max(1, int(round((x2 - x1) * scale)))
            rect_h = max(1, int(round((y2 - y1) * scale)))
            color_key = overlay.label.strip().casefold()
            color = _TABLE_OVERLAY_COLORS.get(color_key, "#38bdf8")
            pen = QPen(color)
            pen.setWidth(max(2, int(round(3 * scale))))
            if overlay.speculative:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(rect_x, rect_y, rect_w, rect_h)
            label_text = overlay.label.replace("_", " ")
            font = painter.font()
            font.setPixelSize(max(10, int(round(12 * scale))))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QPen(color))
            painter.drawText(rect_x + 4, rect_y + font.pixelSize() + 2, label_text)
        painter.end()
        return annotated


class _RecognitionResultsPane(QScrollArea):
    """PRD-style result cards with the former tree's read-only data API."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content.setObjectName("recognitionResultsContent")
        self.cards_layout = QVBoxLayout(self.content)
        self.cards_layout.setContentsMargins(10, 10, 10, 10)
        self.cards_layout.setSpacing(10)
        self.cards_layout.addStretch()
        self.setWidget(self.content)
        self._tree_model = QTreeWidget()

    def clear(self) -> None:
        self._tree_model.clear()
        while self.cards_layout.count() > 1:
            item = self.cards_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def addTopLevelItem(self, item: QTreeWidgetItem) -> None:  # noqa: N802
        self._tree_model.addTopLevelItem(item)

    def invisibleRootItem(self) -> QTreeWidgetItem:  # noqa: N802
        return self._tree_model.invisibleRootItem()

    def setHeaderLabels(self, labels: list[str]) -> None:  # noqa: N802
        self._tree_model.setHeaderLabels(labels)

    def expandToDepth(self, depth: int) -> None:  # noqa: N802
        self._tree_model.expandToDepth(depth)

    def resizeColumnToContents(self, column: int) -> None:  # noqa: N802
        self._tree_model.resizeColumnToContents(column)

    def add_card(self, widget: QWidget) -> None:
        self.cards_layout.insertWidget(self.cards_layout.count() - 1, widget)


class _TrackerReviewTable(QTableWidget):
    """Editable review table with contextual row/column insertion controls."""

    add_column_requested = Signal(int)
    add_row_requested = Signal(int)
    delete_column_requested = Signal(int)
    delete_row_requested = Signal(int)
    header_edit_requested = Signal(int)

    def __init__(self, rows: int, columns: int) -> None:
        super().__init__(rows, columns)
        self._editing = False
        self._laying_out_columns = False
        self._column_insert_index = max(1, columns - 1)
        self._row_insert_index = max(2, rows - 1)
        self._delete_column_index = -1
        self._delete_row_index = -1
        self.add_column_button = QPushButton("+", self)
        self.add_column_button.setObjectName("trackerAddColumnButton")
        self.add_column_button.setToolTip("Add Column")
        self.add_column_button.hide()
        self.add_column_button.clicked.connect(
            lambda: self.add_column_requested.emit(self._column_insert_index)
        )
        self.add_row_button = QPushButton("+", self)
        self.add_row_button.setObjectName("trackerAddRowButton")
        self.add_row_button.setToolTip("Add Row")
        self.add_row_button.hide()
        self.add_row_button.clicked.connect(
            lambda: self.add_row_requested.emit(self._row_insert_index)
        )
        self.delete_column_button = QPushButton("×", self)
        self.delete_column_button.setObjectName("trackerDeleteColumnButton")
        self.delete_column_button.setToolTip("Delete Column")
        self.delete_column_button.hide()
        self.delete_column_button.clicked.connect(
            lambda: self.delete_column_requested.emit(
                self._delete_column_index
            )
        )
        self.delete_row_button = QPushButton("×", self)
        self.delete_row_button.setObjectName("trackerDeleteRowButton")
        self.delete_row_button.setToolTip("Delete Row")
        self.delete_row_button.hide()
        self.delete_row_button.clicked.connect(
            lambda: self.delete_row_requested.emit(self._delete_row_index)
        )
        self.horizontalHeader().setMouseTracking(True)
        self.horizontalHeader().setSectionsClickable(True)
        self.horizontalHeader().viewport().setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.horizontalHeader().viewport().installEventFilter(self)
        self.viewport().installEventFilter(self)
        self.horizontalHeader().sectionDoubleClicked.connect(
            self._request_header_edit
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.apply_responsive_layout()

    def apply_responsive_layout(self) -> None:
        """Keep labels readable, fill spare width, and scroll when required."""

        if self._laying_out_columns or self.columnCount() < 2:
            return
        self._laying_out_columns = True
        try:
            header = self.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            header.setStretchLastSection(False)
            last_column = self.columnCount() - 1
            widths = [
                max(150, min(220, self.sizeHintForColumn(0) + 16)),
                *(
                    max(78, min(130, self.sizeHintForColumn(column) + 16))
                    for column in range(1, last_column)
                ),
                max(
                    88,
                    min(120, self.sizeHintForColumn(last_column) + 16),
                ),
            ]
            available = max(1, self.viewport().width())
            preferred_width = sum(widths)
            if preferred_width < available:
                flexible_columns = max(1, len(widths) - 1)
                extra, remainder = divmod(
                    available - preferred_width,
                    flexible_columns,
                )
                for column in range(1, len(widths)):
                    widths[column] += extra
                    if remainder:
                        widths[column] += 1
                        remainder -= 1
            for column, width in enumerate(widths):
                self.setColumnWidth(column, width)
            needs_scroll = sum(widths) > max(1, self.width() - 4)
            self.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            scrollbar_height = (
                self.horizontalScrollBar().sizeHint().height()
                if needs_scroll
                else 0
            )
            self.setFixedHeight(
                30 + 28 * self.rowCount() + 2 + scrollbar_height
            )
        finally:
            self._laying_out_columns = False

    def set_review_editing(self, editing: bool) -> None:
        self._editing = editing
        self._column_insert_index = max(1, self.columnCount() - 1)
        self._row_insert_index = max(2, self.rowCount() - 1)
        self.add_column_button.hide()
        self.add_row_button.hide()
        self.delete_column_button.hide()
        self.delete_row_button.hide()

    def _request_header_edit(self, column: int) -> None:
        if self._editing and 0 < column < self.columnCount() - 1:
            self.header_edit_requested.emit(column)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if not self._editing or event.type() != QEvent.Type.MouseMove:
            return super().eventFilter(watched, event)
        if watched is self.horizontalHeader().viewport():
            self.add_row_button.hide()
            self.delete_row_button.hide()
            self._show_column_insert_control(int(event.position().x()))
        elif watched is self.viewport():
            self.add_column_button.hide()
            self.delete_column_button.hide()
            self._show_row_controls(int(event.position().y()))
        return super().eventFilter(watched, event)

    def _show_column_insert_control(self, x: int) -> None:
        header = self.horizontalHeader()
        nearest: tuple[int, int] | None = None
        for column in range(self.columnCount() - 1):
            boundary = (
                header.sectionViewportPosition(column)
                + header.sectionSize(column)
            )
            distance = abs(x - boundary)
            trigger_width = min(
                14,
                max(5, header.sectionSize(column) // 4),
            )
            if (
                distance <= trigger_width
                and (nearest is None or distance < nearest[0])
            ):
                nearest = (distance, column + 1)
        if nearest is not None:
            self.delete_column_button.hide()
            self._column_insert_index = min(
                nearest[1], self.columnCount() - 1
            )
            self.add_column_button.adjustSize()
            boundary = (
                header.sectionViewportPosition(nearest[1] - 1)
                + header.sectionSize(nearest[1] - 1)
            )
            x_pos = max(
                0,
                min(
                    self.width() - self.add_column_button.width(),
                    boundary - self.add_column_button.width() // 2,
                ),
            )
            y_pos = max(
                0,
                header.geometry().center().y()
                - self.add_column_button.height() // 2,
            )
            self.add_column_button.move(x_pos, y_pos)
            self.add_column_button.raise_()
            self.add_column_button.show()
            return
        self.add_column_button.hide()
        hovered_column = header.logicalIndexAt(x)
        if not 0 < hovered_column < self.columnCount() - 1:
            self.delete_column_button.hide()
            return
        self._delete_column_index = hovered_column
        self.delete_column_button.setFixedSize(22, 22)
        section_left = header.sectionViewportPosition(hovered_column)
        section_width = header.sectionSize(hovered_column)
        self.delete_column_button.move(
            max(0, section_left + section_width - 26),
            max(0, header.geometry().center().y() - 11),
        )
        self.delete_column_button.raise_()
        self.delete_column_button.show()

    def _show_row_controls(self, y: int) -> None:
        hovered_row = self.rowAt(y)
        last_row = self.rowCount() - 1
        if 2 <= hovered_row < last_row:
            self._delete_row_index = hovered_row
            self.delete_row_button.setFixedSize(22, 22)
            rect = self.visualRect(self.model().index(hovered_row, 0))
            self.delete_row_button.move(
                max(0, self.width() - self.delete_row_button.width() - 8),
                max(
                    self.horizontalHeader().height(),
                    self.viewport().geometry().top()
                    + rect.center().y()
                    - self.delete_row_button.height() // 2,
                ),
            )
            self.delete_row_button.raise_()
            self.delete_row_button.show()
        else:
            self.delete_row_button.hide()

        nearest: tuple[int, int] | None = None
        for row in range(self.rowCount()):
            rect = self.visualRect(self.model().index(row, 0))
            distance = abs(y - rect.bottom())
            if distance <= 8 and (nearest is None or distance < nearest[0]):
                nearest = (distance, row + 1)
        if nearest is None:
            self.add_row_button.hide()
            return
        self._row_insert_index = max(2, min(nearest[1], last_row))
        self.add_row_button.adjustSize()
        boundary = self.visualRect(
            self.model().index(min(nearest[1] - 1, last_row), 0)
        ).bottom()
        self.add_row_button.move(
            max(0, (self.width() - self.add_row_button.width()) // 2),
            max(
                self.horizontalHeader().height(),
                self.viewport().geometry().top()
                + boundary
                - self.add_row_button.height() // 2,
            ),
        )
        self.add_row_button.raise_()
        self.add_row_button.show()


def _agent4_cell(value: object) -> str:
    if isinstance(value, dict):
        return "---"
    if isinstance(value, int):
        return f"{value:,}"
    text = str(value).strip() if value is not None else ""
    return text or "---"


def _editable_text(value: str) -> str | None:
    text = value.strip()
    return text or None


def _editable_count(value: str) -> int | None:
    text = value.strip().replace(",", "")
    return int(text) if text.isdigit() else None


def _invalid_count_kind(value: str) -> str | None:
    """Return the specifically rejected numeric form, if present."""

    text = value.strip().replace(",", "")
    if re.fullmatch(r"-(?:\d+(?:\.\d*)?|\.\d+)", text):
        return "negative"
    if re.fullmatch(r"\+?(?:\d+\.\d*|\.\d+)", text):
        return "decimal"
    return None


def _visible_extraction_warnings(
    result: OverallSiteExtraction,
) -> tuple[str, ...]:
    """Hide missing-data prose already represented by result-card empties."""

    visible: list[str] = []
    for warning in result.warnings:
        normalized = " ".join(warning.casefold().split())
        reports_absence = any(
            marker in normalized
            for marker in (
                "not visible",
                "not found",
                "not extracted",
                "no visible",
            )
        ) or normalized.startswith("no ")
        missing_inv = (
            not result.inv_list
            and reports_absence
            and ("inv" in normalized or "pcs" in normalized)
        )
        missing_tracker = (
            not result.tracker_ocr_fields
            and reports_absence
            and (
                "systems specifications" in normalized
                or "tracker_ocr_fields" in normalized
                or "tracker table" in normalized
            )
        )
        if not missing_inv and not missing_tracker:
            visible.append(warning)
    return tuple(visible)


def _tracker_key(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    match = re.fullmatch(
        r"(\d+)\s*(?:-|\s)*(?:STR|STRING)(?:\s+.*)?",
        text,
        re.I,
    )
    normalized = f"{match.group(1)}-string" if match else text
    return re.sub(r"[^0-9a-z]", "", normalized.lower()).replace(
        "string",
        "str",
    )


def _source_tracker_types(
    results: Sequence[OverallSiteExtraction],
) -> list[str]:
    """Return first-seen OCR row labels without Agent4 normalization."""

    labels: list[str] = []
    seen: set[str] = set()
    for result in results:
        for tracker in result.tracker_ocr_fields:
            candidates = [detail.num_str for detail in tracker.num_str_details]
            if not candidates and tracker.num_str.value:
                candidates = re.split(r"[,;\n]+", tracker.num_str.value)
            for candidate in candidates:
                label = candidate.strip()
                key = _tracker_key(label)
                if label and key and key not in seen:
                    seen.add(key)
                    labels.append(label)
    return labels


def _inv_review_table_content(
    result: OverallSiteExtraction,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    headers = (
        tr("ai_statistics.info_recognition.inv_identifier", "INV Identifier"),
    )
    return headers, tuple((inv,) for inv in result.inv_list)


def _normalize_inv_editor_value(value: str) -> str:
    return value.strip()


def _localized_status(status: str) -> str:
    """Translate workflow status values passed to the review panel."""

    normalized = re.sub(r"[\s-]+", "_", status.strip().lower())
    fallbacks = {
        "waiting": "waiting",
        "running": "running",
        "ready": "ready",
        "result_ready": "result ready",
        "needs_confirm": "needs confirm",
        "confirmed": "confirmed",
        "failed": "failed",
    }
    fallback = fallbacks.get(normalized)
    if fallback is None:
        return status
    return tr(f"ai_statistics.status.{normalized}", fallback)


def _result_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    object_name: str,
    total_row: bool = False,
    editable: bool = False,
    contextual_insert: bool = False,
) -> QTableWidget:
    table = (
        _TrackerReviewTable(len(rows), len(headers))
        if contextual_insert
        else QTableWidget(len(rows), len(headers))
    )
    table.setObjectName(object_name)
    table.setHorizontalHeaderLabels(list(headers))
    table.verticalHeader().hide()
    table.setEditTriggers(
        QAbstractItemView.EditTrigger.DoubleClicked
        | QAbstractItemView.EditTrigger.EditKeyPressed
        if editable
        else QAbstractItemView.EditTrigger.NoEditTriggers
    )
    table.setSelectionMode(
        QAbstractItemView.SelectionMode.SingleSelection
        if editable
        else QAbstractItemView.SelectionMode.NoSelection
    )
    if not editable:
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    table.setAlternatingRowColors(True)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.horizontalHeader().setMinimumSectionSize(54)
    table.horizontalHeader().setFixedHeight(30)
    table.verticalHeader().setDefaultSectionSize(28)
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            item = QTableWidgetItem(value)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            if total_row and row_index == len(rows) - 1:
                font = QFont(item.font())
                font.setBold(True)
                item.setFont(font)
            table.setItem(row_index, column, item)
    for row in range(table.rowCount()):
        table.setRowHeight(row, 28)
    table.setFixedHeight(30 + 28 * table.rowCount() + 2)
    if isinstance(table, _TrackerReviewTable):
        table.apply_responsive_layout()
    return table


class InfoRecognitionPage(WorkflowStagePage):
    """Display overall pages and review Agent2's structured extraction."""

    review_requirement_changed = Signal()

    ORIENTATION_OPTIONS = PANEL_ORIENTATION_OPTIONS

    def __init__(self, parent=None) -> None:
        super().__init__("InfoRecognitionPage", "ai_statistics.info_recognition.confirm",
                         "Confirm Information", parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("infoRecognitionSplitter")
        splitter.setChildrenCollapsible(False)

        preview, self.preview_title, self.preview_meta, self.preview_hint = empty_panel()
        self.preview_tabs = QTabWidget()
        self.preview_tabs.setObjectName("overallPageTabs")
        self.preview_tabs.setDocumentMode(True)
        self.preview_tabs.hide()
        self.preview_toolbar = QWidget()
        self.preview_toolbar.setObjectName("previewZoomToolbar")
        zoom_layout = QHBoxLayout(self.preview_toolbar)
        zoom_layout.setContentsMargins(8, 5, 8, 5)
        zoom_layout.setSpacing(5)
        zoom_layout.addStretch()
        self.zoom_out_button = QPushButton("−")
        self.zoom_out_button.setObjectName("previewZoomButton")
        self.fit_button = QPushButton(
            tr("ai_statistics.preview_fit", "Fit to Window")
        )
        self.fit_button.setObjectName("previewFitButton")
        self.actual_size_button = QPushButton(
            tr("ai_statistics.preview_actual", "Actual Size")
        )
        self.actual_size_button.setObjectName("previewActualButton")
        self.zoom_in_button = QPushButton("+")
        self.zoom_in_button.setObjectName("previewZoomButton")
        for button in (
            self.zoom_out_button,
            self.fit_button,
            self.actual_size_button,
            self.zoom_in_button,
        ):
            zoom_layout.addWidget(button)
        self.zoom_out_button.clicked.connect(lambda: self._zoom_current(1 / 1.2))
        self.zoom_in_button.clicked.connect(lambda: self._zoom_current(1.2))
        self.fit_button.clicked.connect(self._fit_current)
        self.actual_size_button.clicked.connect(self._actual_size_current)
        self.preview_toolbar.hide()
        preview.layout().addWidget(self.preview_toolbar)
        preview.layout().addWidget(self.preview_tabs, 1)

        review, self.review_title, self.review_meta, self.review_hint = empty_panel()
        self.orientation_panel = QFrame()
        self.orientation_panel.setObjectName("agent2OrientationPanel")
        orientation_layout = QVBoxLayout(self.orientation_panel)
        orientation_layout.setContentsMargins(14, 11, 14, 12)
        orientation_layout.setSpacing(6)
        self.orientation_title = QLabel()
        self.orientation_title.setObjectName("agent2OrientationTitle")
        orientation_layout.addWidget(self.orientation_title)
        self.orientation_help = QLabel()
        self.orientation_help.setObjectName("agent2OrientationHelp")
        self.orientation_help.setWordWrap(True)
        orientation_layout.addWidget(self.orientation_help)
        self.orientation_combo = QComboBox()
        self.orientation_combo.setObjectName("agent2OrientationCombo")
        self.orientation_combo.setMaxVisibleItems(
            len(self.ORIENTATION_OPTIONS) + 1
        )
        self.orientation_combo.addItem("", None)
        for code, _english, _chinese in self.ORIENTATION_OPTIONS:
            self.orientation_combo.addItem(panel_orientation_text(code), code)
        self.orientation_combo.currentIndexChanged.connect(
            lambda _index: self.review_requirement_changed.emit()
        )
        orientation_layout.addWidget(self.orientation_combo)
        # Keep this directly below the panel header. The stretchable empty
        # hint must stay underneath it so running/result state changes cannot
        # move the user's required choice between the top and bottom.
        review.layout().insertWidget(1, self.orientation_panel)
        self.result_tree = _RecognitionResultsPane()
        self.result_tree.setObjectName("agent2ResultTree")
        self.result_tree.hide()
        review.layout().addWidget(self.result_tree, 1)

        self._source_results: tuple[OverallSiteExtraction, ...] = ()
        self._tracker_table: QTableWidget | None = None
        self._module_locations: list[tuple[int, int]] = []
        self._tracker_types: list[str] = []
        self._tracker_edit_snapshot: tuple[tuple[str, ...], ...] | None = None
        self._tracker_type_snapshot: tuple[str, ...] = ()
        self._tracker_header_snapshot: tuple[str, ...] = ()
        self._module_location_snapshot: tuple[tuple[int, int], ...] = ()
        self._tracker_edit_button: QPushButton | None = None
        self._tracker_add_button: QPushButton | None = None
        self._tracker_add_column_button: QPushButton | None = None
        self._tracker_delete_row_button: QPushButton | None = None
        self._tracker_delete_column_button: QPushButton | None = None
        self._tracker_save_button: QPushButton | None = None
        self._tracker_cancel_button: QPushButton | None = None
        self._inv_tables: list[QTableWidget | None] = []
        self._preview_signature: tuple[tuple[int, int, int], ...] = ()
        self._preview_page_numbers: list[int] = []
        self._page_bbox_overlays: dict[int, tuple[_TableBBoxOverlay, ...]] = {}
        self._review_status = "waiting"
        self._review_running = False

        splitter.addWidget(preview)
        splitter.addWidget(review)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([720, 480])
        root.addWidget(splitter, 1)
        self.append_confirm_footer(root)
        self.retranslate()

    def show_results(self, page_images: Mapping[int, bytes],
                     results: Sequence[OverallSiteExtraction], *,
                     running: bool = False, status: str = "waiting",
                     orientation: str | None = None) -> None:
        self._review_status = status
        self._review_running = running
        self._show_page_images(page_images)
        self.review_meta.setText(_localized_status(status))
        if orientation:
            index = self.orientation_combo.findData(orientation)
            if index >= 0:
                self.orientation_combo.setCurrentIndex(index)
        if running:
            self.review_hint.setText(tr("ai_statistics.info_recognition.running",
                                        "MinerU table location and Claude vision are running…"))
            self.review_hint.show()
            self.result_tree.hide()
        elif results:
            result_tuple = tuple(
                sorted(
                    results,
                    key=lambda result: (
                        not bool(result.inv_list),
                        result.page_number,
                    ),
                )
            )
            if result_tuple != self._source_results:
                self._show_extractions(result_tuple)
        else:
            self.result_tree.clear()
            self.result_tree.hide()
            key = ("ai_statistics.info_recognition.failed" if status == "failed"
                   else "ai_statistics.info_recognition.empty_review")
            fallback = ("Recognition failed. Use Run AI Model to retry." if status == "failed"
                        else "Run information recognition to review Tracker and INV data.")
            self.review_hint.setText(tr(key, fallback))
            self.review_hint.show()

    def clear_results(self) -> None:
        self._review_status = "waiting"
        self._review_running = False
        self._clear_page_images()
        self.preview_tabs.hide()
        self.preview_toolbar.hide()
        self.preview_hint.show()
        self.result_tree.clear()
        self.result_tree.hide()
        self.review_hint.show()
        self._source_results = ()
        self._tracker_table = None
        self._module_locations = []
        self._tracker_types = []
        self._tracker_edit_snapshot = None
        self._tracker_type_snapshot = ()
        self._tracker_header_snapshot = ()
        self._module_location_snapshot = ()
        self._tracker_edit_button = None
        self._tracker_add_button = None
        self._tracker_add_column_button = None
        self._tracker_delete_row_button = None
        self._tracker_delete_column_button = None
        self._tracker_save_button = None
        self._tracker_cancel_button = None
        self._inv_tables = []
        self.orientation_combo.setCurrentIndex(0)

    @property
    def selected_orientation(self) -> str | None:
        value = self.orientation_combo.currentData()
        return str(value) if value else None

    @property
    def review_ready(self) -> bool:
        return bool(self._source_results and self.selected_orientation)

    def _show_page_images(self, page_images: Mapping[int, bytes]) -> None:
        signature = tuple(
            (
                page_number,
                len(page_images[page_number]),
                id(page_images[page_number]),
            )
            for page_number in sorted(page_images)
        )
        if signature == self._preview_signature:
            return

        self._clear_page_images()
        page_areas = []
        for page_number in sorted(page_images):
            pixmap = QPixmap()
            if not pixmap.loadFromData(page_images[page_number], "PNG"):
                continue
            scroll = _PageImageScrollArea(pixmap)
            page_areas.append(scroll)
            self._preview_page_numbers.append(page_number)
            self.preview_tabs.addTab(
                scroll,
                tr("ai_statistics.page_number", "Page {number}").format(
                    number=page_number
                ),
            )
        self._preview_signature = signature
        has_pages = self.preview_tabs.count() > 0
        single_page = self.preview_tabs.count() == 1
        self.preview_tabs.tabBar().setVisible(not single_page)
        for page_area in page_areas:
            page_area.set_fit_to_page(single_page)
        self.preview_tabs.setVisible(has_pages)
        self.preview_toolbar.setVisible(has_pages)
        self.preview_hint.setVisible(not has_pages)
        if has_pages:
            self.preview_meta.setText(tr("ai_statistics.fit_page", "Fit to page"))
        self._sync_preview_overlays()

    def _clear_page_images(self) -> None:
        """Destroy removed tab pages so their native QPixmaps are released."""

        while self.preview_tabs.count():
            widget = self.preview_tabs.widget(0)
            self.preview_tabs.removeTab(0)
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._preview_signature = ()
        self._preview_page_numbers = []
        self._page_bbox_overlays = {}

    def _build_page_bbox_overlay_map(
        self,
        results: Sequence[OverallSiteExtraction],
    ) -> dict[int, tuple[_TableBBoxOverlay, ...]]:
        overlays: dict[int, tuple[_TableBBoxOverlay, ...]] = {}
        for result in results:
            page_overlays = _table_bbox_overlays_from_result(result)
            page_numbers = result.raw.get("consolidated_page_numbers")
            if isinstance(page_numbers, (list, tuple)):
                for page_number in page_numbers:
                    try:
                        overlays[int(page_number)] = page_overlays
                    except (TypeError, ValueError):
                        continue
            else:
                overlays[result.page_number] = page_overlays
        return overlays

    def _sync_preview_overlays(self) -> None:
        for index, page_number in enumerate(self._preview_page_numbers):
            widget = self.preview_tabs.widget(index)
            if not isinstance(widget, _PageImageScrollArea):
                continue
            widget.set_table_overlays(
                self._page_bbox_overlays.get(page_number, ())
            )

    def _current_page_area(self) -> _PageImageScrollArea | None:
        widget = self.preview_tabs.currentWidget()
        return widget if isinstance(widget, _PageImageScrollArea) else None

    def _zoom_current(self, factor: float) -> None:
        page_area = self._current_page_area()
        if page_area is not None:
            page_area.zoom(factor)

    def _fit_current(self) -> None:
        page_area = self._current_page_area()
        if page_area is not None:
            page_area.set_fit_to_page(True)

    def _actual_size_current(self) -> None:
        page_area = self._current_page_area()
        if page_area is not None:
            page_area.actual_size()

    def _make_card(self, number: int, title: str, badge: str | None = None):
        card = QFrame()
        card.setObjectName("recognitionCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        title_label = QLabel(f"{number}. {title}")
        title_label.setObjectName("recognitionCardTitle")
        header.addWidget(title_label)
        header.addStretch()
        if badge:
            badge_label = QLabel(badge)
            badge_label.setObjectName("reviewBadge")
            header.addWidget(badge_label)
        layout.addLayout(header)
        return card, layout

    def _tracker_card(
        self,
        results: Sequence[OverallSiteExtraction],
        number: int,
    ) -> QFrame:
        card, layout = self._make_card(
            number, tr("ai_statistics.info_recognition.tracker_output", "Tracker OCR Output"),
            tr("ai_statistics.info_recognition.review_badge", "needs review"))
        trackers = [
            tracker
            for result in results
            for tracker in result.tracker_ocr_fields
        ]
        if trackers:
            headers = (
                tr(
                    "ai_statistics.info_recognition.module_model_header",
                    "Moudle Model",
                ),
                *(
                    _agent4_cell(tracker.module_model.value)
                    for tracker in trackers
                ),
                tr("ai_statistics.info_recognition.total", "Total"),
            )
            rows = [
                (
                    tr(
                        "ai_statistics.info_recognition.module_stc_dc_rating",
                        "Module STC DC Rating",
                    ),
                    *(
                        _agent4_cell(tracker.module_stc_dc_rating_w.value)
                        for tracker in trackers
                    ),
                    "/",
                ),
                (
                    tr(
                        "ai_statistics.info_recognition.string_count",
                        "String Count",
                    ),
                    *(
                        _agent4_cell(tracker.string_count.value)
                        for tracker in trackers
                    ),
                    "---",
                ),
            ]
            for tracker_type in self._tracker_types:
                rows.append(
                    (
                        tracker_type,
                        *(
                            _agent4_cell(
                                next(
                                    (
                                        detail.count
                                        for detail in tracker.num_str_details
                                        if _tracker_key(detail.num_str)
                                        == _tracker_key(tracker_type)
                                    ),
                                    None,
                                )
                            )
                            for tracker in trackers
                        ),
                        "---",
                    )
                )
            rows.append(
                (
                    tr(
                        "ai_statistics.info_recognition.tracker_count",
                        "Tracker Count",
                    ),
                    *("---" for _tracker in trackers),
                    "---",
                )
            )
            table = _result_table(
                headers,
                rows,
                object_name="trackerOcrTable",
                total_row=True,
                editable=False,
                contextual_insert=True,
            )
            assert isinstance(table, _TrackerReviewTable)
            self._tracker_table = table
            table.itemChanged.connect(self._on_tracker_item_changed)
            table.add_row_requested.connect(self._add_tracker_row)
            table.add_column_requested.connect(self._add_tracker_column)
            table.delete_row_requested.connect(self._delete_tracker_row)
            table.delete_column_requested.connect(
                self._delete_tracker_column
            )
            table.header_edit_requested.connect(self._edit_tracker_header)
            self._refresh_tracker_totals(table.item(0, 0))

            actions = QHBoxLayout()
            actions.addStretch()
            self._tracker_edit_button = QPushButton(
                tr("ai_statistics.info_recognition.edit", "Edit")
            )
            self._tracker_edit_button.setObjectName("trackerEditButton")
            self._tracker_add_button = table.add_row_button
            self._tracker_add_column_button = table.add_column_button
            self._tracker_delete_row_button = table.delete_row_button
            self._tracker_delete_column_button = table.delete_column_button
            self._tracker_save_button = QPushButton(
                tr("ai_statistics.info_recognition.save", "Save Table")
            )
            self._tracker_save_button.setObjectName("trackerSaveButton")
            self._tracker_cancel_button = QPushButton(
                tr("ai_statistics.info_recognition.cancel", "Cancel")
            )
            self._tracker_cancel_button.setObjectName("trackerCancelButton")
            actions.addWidget(self._tracker_edit_button)
            self._tracker_edit_button.clicked.connect(self._begin_tracker_edit)
            self._tracker_save_button.clicked.connect(self._save_tracker_edit)
            self._tracker_cancel_button.clicked.connect(self._cancel_tracker_edit)
            layout.addLayout(actions)
            layout.addWidget(table)
            save_actions = QHBoxLayout()
            save_actions.addStretch()
            save_actions.addWidget(self._tracker_save_button)
            save_actions.addWidget(self._tracker_cancel_button)
            layout.addLayout(save_actions)
            self._set_tracker_editing(False)
        else:
            empty = QLabel(tr("ai_statistics.info_recognition.no_tracker",
                              "No tracker data recognized"))
            empty.setObjectName("resultEmpty")
            layout.addWidget(empty)

        return card

    def _set_tracker_editing(self, editing: bool) -> None:
        table = self._tracker_table
        if table is None:
            return
        table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            if editing
            else QAbstractItemView.EditTrigger.NoEditTriggers
        )
        table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
            if editing
            else QAbstractItemView.SelectionMode.NoSelection
        )
        table.setFocusPolicy(
            Qt.FocusPolicy.StrongFocus if editing else Qt.FocusPolicy.NoFocus
        )
        if isinstance(table, _TrackerReviewTable):
            table.set_review_editing(editing)
        last_row = table.rowCount() - 1
        last_column = table.columnCount() - 1
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is None:
                    continue
                can_edit = editing and (
                    (row < 2 and 0 < column < last_column)
                    or (2 <= row < last_row and column < last_column)
                )
                flags = item.flags()
                item.setFlags(
                    flags | Qt.ItemFlag.ItemIsEditable
                    if can_edit
                    else flags & ~Qt.ItemFlag.ItemIsEditable
                )
                if column == 0 or row == last_row:
                    font = QFont(item.font())
                    font.setBold(True)
                    item.setFont(font)
        for row in range(1, last_row):
            for column in range(1, last_column):
                item = table.item(row, column)
                item.setData(_LAST_VALID_COUNT_ROLE, item.text())
        if self._tracker_edit_button is not None:
            self._tracker_edit_button.setVisible(not editing)
        for button in (
            self._tracker_save_button,
            self._tracker_cancel_button,
        ):
            if button is not None:
                button.setVisible(editing)
        if isinstance(table, _TrackerReviewTable):
            table.apply_responsive_layout()

    def _begin_tracker_edit(self) -> None:
        table = self._tracker_table
        if table is None:
            return
        self._tracker_edit_snapshot = tuple(
            tuple(table.item(row, column).text() for column in range(table.columnCount()))
            for row in range(table.rowCount())
        )
        self._tracker_type_snapshot = tuple(self._tracker_types)
        self._tracker_header_snapshot = tuple(
            table.horizontalHeaderItem(column).text()
            for column in range(table.columnCount())
        )
        self._module_location_snapshot = tuple(self._module_locations)
        self._set_tracker_editing(True)

    def _add_tracker_row(self, insert_row: int) -> None:
        table = self._tracker_table
        if table is None or self._tracker_edit_snapshot is None:
            return
        row = max(2, min(insert_row, table.rowCount() - 1))
        table.blockSignals(True)
        try:
            table.insertRow(row)
            for column in range(table.columnCount()):
                text = (
                    tr("ai_statistics.info_recognition.new_tracker_type", "New type")
                    if column == 0
                    else "---"
                )
                table.setItem(row, column, QTableWidgetItem(text))
            table.setRowHeight(row, 28)
            table.setFixedHeight(30 + 28 * table.rowCount() + 2)
            self._tracker_types.insert(
                row - 2,
                table.item(row, 0).text(),
            )
        finally:
            table.blockSignals(False)
        self._set_tracker_editing(True)
        table.setCurrentItem(table.item(row, 0))
        table.editItem(table.item(row, 0))

    def _delete_tracker_row(self, row: int) -> None:
        table = self._tracker_table
        if table is None or self._tracker_edit_snapshot is None:
            return
        if not 2 <= row < table.rowCount() - 1:
            return
        table.blockSignals(True)
        try:
            table.removeRow(row)
            self._tracker_types.pop(row - 2)
            table.setFixedHeight(30 + 28 * table.rowCount() + 2)
        finally:
            table.blockSignals(False)
        self._refresh_tracker_totals(table.item(0, 0))
        self._set_tracker_editing(True)

    def _add_tracker_column(self, insert_column: int) -> None:
        table = self._tracker_table
        if table is None or self._tracker_edit_snapshot is None:
            return
        total_column = table.columnCount() - 1
        column = max(1, min(insert_column, total_column))
        table.blockSignals(True)
        try:
            table.insertColumn(column)
            table.setHorizontalHeaderItem(
                column,
                QTableWidgetItem(
                    tr(
                        "ai_statistics.info_recognition.new_brand",
                        "New brand",
                    )
                ),
            )
            for row in range(table.rowCount()):
                table.setItem(row, column, QTableWidgetItem("---"))
            location_index = column - 1
            if self._module_locations:
                neighbor_index = min(
                    location_index,
                    len(self._module_locations) - 1,
                )
                target_result = self._module_locations[neighbor_index][0]
            else:
                target_result = 0
            self._module_locations.insert(
                location_index,
                (target_result, -1),
            )
        finally:
            table.blockSignals(False)
        self._set_tracker_editing(True)

    def _delete_tracker_column(self, column: int) -> None:
        table = self._tracker_table
        if table is None or self._tracker_edit_snapshot is None:
            return
        if not 0 < column < table.columnCount() - 1:
            return
        table.blockSignals(True)
        try:
            table.removeColumn(column)
            self._module_locations.pop(column - 1)
        finally:
            table.blockSignals(False)
        self._refresh_tracker_totals(table.item(0, 0))
        self._set_tracker_editing(True)

    def _edit_tracker_header(self, column: int) -> None:
        table = self._tracker_table
        if table is None:
            return
        header = table.horizontalHeaderItem(column)
        value, accepted = QInputDialog.getText(
            self,
            tr("ai_statistics.info_recognition.edit_brand", "Edit brand"),
            tr("ai_statistics.info_recognition.brand", "Brand"),
            text=header.text(),
        )
        if accepted:
            header.setText(value.strip() or "---")
            if isinstance(table, _TrackerReviewTable):
                table.apply_responsive_layout()

    def _save_tracker_edit(self) -> None:
        table = self._tracker_table
        if table is None or self._tracker_edit_snapshot is None:
            return
        table.blockSignals(True)
        try:
            last_row = table.rowCount() - 1
            last_column = table.columnCount() - 1
            for row in range(last_row):
                editable_columns = (
                    range(1, last_column) if row < 2 else range(last_column)
                )
                for column in editable_columns:
                    item = table.item(row, column)
                    if not item.text().strip():
                        item.setText("---")
            self._tracker_types = [
                table.item(row, 0).text().strip() or "---"
                for row in range(2, last_row)
            ]
            for column in range(1, last_column):
                table.horizontalHeaderItem(column).setText(
                    table.horizontalHeaderItem(column).text().strip() or "---"
                )
        finally:
            table.blockSignals(False)
        self._refresh_tracker_totals(table.item(0, 0))
        self._tracker_edit_snapshot = None
        self._tracker_type_snapshot = ()
        self._tracker_header_snapshot = ()
        self._module_location_snapshot = ()
        self._set_tracker_editing(False)

    def _cancel_tracker_edit(self) -> None:
        table = self._tracker_table
        snapshot = self._tracker_edit_snapshot
        if table is None or snapshot is None:
            return
        table.blockSignals(True)
        try:
            table.setColumnCount(len(self._tracker_header_snapshot))
            for column, header in enumerate(self._tracker_header_snapshot):
                table.setHorizontalHeaderItem(
                    column,
                    QTableWidgetItem(header),
                )
            table.setRowCount(len(snapshot))
            for row, values in enumerate(snapshot):
                for column, value in enumerate(values):
                    item = table.item(row, column)
                    if item is None:
                        item = QTableWidgetItem()
                        table.setItem(row, column, item)
                    item.setText(value)
                table.setRowHeight(row, 28)
            table.setFixedHeight(30 + 28 * table.rowCount() + 2)
            self._tracker_types = list(self._tracker_type_snapshot)
            self._module_locations = list(self._module_location_snapshot)
        finally:
            table.blockSignals(False)
        self._tracker_edit_snapshot = None
        self._tracker_type_snapshot = ()
        self._tracker_header_snapshot = ()
        self._module_location_snapshot = ()
        self._set_tracker_editing(False)

    def _on_tracker_item_changed(self, item: QTableWidgetItem) -> None:
        table = self._tracker_table
        if table is None:
            return
        is_count_cell = (
            1 <= item.row() < table.rowCount() - 1
            and 1 <= item.column() < table.columnCount() - 1
        )
        invalid_kind = (
            _invalid_count_kind(item.text()) if is_count_cell else None
        )
        if invalid_kind is not None:
            previous_value = item.data(_LAST_VALID_COUNT_ROLE)
            table.blockSignals(True)
            try:
                item.setText(
                    str(previous_value) if previous_value is not None else ""
                )
            finally:
                table.blockSignals(False)
            self._refresh_tracker_totals(item)
            message_key = (
                "ai_statistics.info_recognition.negative_count"
                if invalid_kind == "negative"
                else "ai_statistics.info_recognition.decimal_count"
            )
            fallback = (
                "Negative values are not allowed. Previous value restored."
                if invalid_kind == "negative"
                else "Decimals are not allowed. Previous value restored."
            )
            QMessageBox.critical(
                self,
                tr(
                    "ai_statistics.info_recognition.invalid_count_title",
                    "Input Error",
                ),
                tr(message_key, fallback),
            )
            return
        if is_count_cell:
            table.blockSignals(True)
            try:
                item.setData(_LAST_VALID_COUNT_ROLE, item.text())
            finally:
                table.blockSignals(False)
        self._refresh_tracker_totals(item)

    def _inv_card(self, result: OverallSiteExtraction, number: int) -> QFrame:
        count = tr("ai_statistics.info_recognition.inv_count", "{count} items").format(
            count=result.inv_count)
        card, layout = self._make_card(
            number, tr("ai_statistics.info_recognition.inv_tree", "INV Tree"), count)
        if result.inv_list:
            headers, rows = _inv_review_table_content(result)
            table = _result_table(
                headers,
                rows,
                object_name="invEditTable",
                editable=True,
            )
            table.setToolTip(
                tr(
                    "ai_statistics.info_recognition.edit_tip",
                    "Double-click a cell to edit it.",
                )
            )
            self._inv_tables.append(table)
            layout.addWidget(table)
        else:
            self._inv_tables.append(None)
            empty = QLabel(tr("ai_statistics.info_recognition.no_inv",
                              "No INV identifiers recognized"))
            empty.setObjectName("resultEmpty")
            layout.addWidget(empty)
        return card

    def _warning_card(self, warnings: Sequence[str], number: int) -> QFrame:
        card, layout = self._make_card(
            number, tr("ai_statistics.info_recognition.warnings", "Warnings"),
            str(len(warnings)))
        for warning in warnings:
            label = QLabel(f"• {warning}")
            label.setObjectName("warningItem")
            label.setWordWrap(True)
            layout.addWidget(label)
        return card

    def _show_extractions(self, results: Sequence[OverallSiteExtraction]) -> None:
        ordered_results = tuple(results)
        self._page_bbox_overlays = self._build_page_bbox_overlay_map(
            ordered_results
        )
        self._sync_preview_overlays()
        self.result_tree.clear()
        self._source_results = ordered_results
        self._tracker_table = None
        self._tracker_edit_snapshot = None
        self._tracker_type_snapshot = ()
        self._tracker_header_snapshot = ()
        self._module_location_snapshot = ()
        self._tracker_edit_button = None
        self._tracker_add_button = None
        self._tracker_add_column_button = None
        self._tracker_delete_row_button = None
        self._tracker_delete_column_button = None
        self._tracker_save_button = None
        self._tracker_cancel_button = None
        self._inv_tables = []
        self._module_locations = [
            (result_index, tracker_index)
            for result_index, result in enumerate(ordered_results)
            for tracker_index, _tracker in enumerate(result.tracker_ocr_fields)
        ]
        self._tracker_types = _source_tracker_types(ordered_results)
        self.result_tree.add_card(self._tracker_card(ordered_results, 2))
        card_number = 3
        for result in ordered_results:
            visible_warnings = _visible_extraction_warnings(result)
            page = QTreeWidgetItem([f"Page {result.page_number}", "", ""])
            self.result_tree.addTopLevelItem(page)
            trackers = QTreeWidgetItem(
                page, ["Tracker parameters", str(len(result.tracker_ocr_fields)), ""])
            for index, tracker in enumerate(result.tracker_ocr_fields, 1):
                tracker_item = QTreeWidgetItem(
                    trackers, [f"Tracker {index}",
                               tracker.module_model.value or "Unknown model", ""])
                for field in (tracker.module_model, tracker.module_stc_dc_rating_w,
                              tracker.string_count, tracker.num_str):
                    QTreeWidgetItem(tracker_item,
                                    [field.label, field.value or "—", field.source or ""])
                if tracker.num_str_details:
                    details = QTreeWidgetItem(tracker_item, ["Num-STR details", "", ""])
                    for detail in tracker.num_str_details:
                        QTreeWidgetItem(details, [detail.num_str, detail.count or "—",
                                                  detail.source or ""])
            invs = QTreeWidgetItem(page, ["INV summary", str(result.inv_count), ""])
            for inv in result.inv_list:
                QTreeWidgetItem(invs, [inv, "", ""])
            if visible_warnings:
                warning_items = QTreeWidgetItem(
                    page, ["Warnings", str(len(visible_warnings)), ""])
                for warning in visible_warnings:
                    QTreeWidgetItem(warning_items, [warning, "", ""])

            consolidated_pages = result.raw.get("consolidated_page_numbers")
            if isinstance(consolidated_pages, (list, tuple)):
                page_text = ", ".join(str(page) for page in consolidated_pages)
                page_label = QLabel(
                    tr(
                        "ai_statistics.info_recognition.consolidated_pages",
                        "Merged pages: {pages}",
                    ).format(pages=page_text)
                )
                page_label.setObjectName("resultPageLabel")
                self.result_tree.add_card(page_label)
            elif len(ordered_results) > 1:
                page_label = QLabel(
                    tr(
                        "ai_statistics.info_recognition.page",
                        "Page {page}",
                    ).format(page=result.page_number)
                )
                page_label.setObjectName("resultPageLabel")
                self.result_tree.add_card(page_label)
            self.result_tree.add_card(self._inv_card(result, card_number))
            card_number += 1
            if visible_warnings:
                self.result_tree.add_card(
                    self._warning_card(visible_warnings, card_number)
                )
                card_number += 1
        self.result_tree.expandToDepth(2)
        self.result_tree.resizeColumnToContents(0)
        self.result_tree.resizeColumnToContents(1)
        self.review_hint.hide()
        self.result_tree.show()

    def _refresh_tracker_totals(self, _item: QTableWidgetItem) -> None:
        table = self._tracker_table
        if table is None:
            return
        table.blockSignals(True)
        try:
            module_columns = range(1, table.columnCount() - 1)
            total_column = table.columnCount() - 1
            tracker_rows = range(2, table.rowCount() - 1)
            string_counts = [
                _editable_count(table.item(1, column).text())
                for column in module_columns
            ]
            valid_string_counts = [
                value for value in string_counts if value is not None
            ]
            table.item(1, total_column).setText(
                f"{sum(valid_string_counts):,}"
                if valid_string_counts
                else "---"
            )
            grand_total = 0
            has_grand_total = False
            for row in tracker_rows:
                counts = [
                    _editable_count(table.item(row, column).text())
                    for column in module_columns
                ]
                valid_counts = [value for value in counts if value is not None]
                table.item(row, total_column).setText(
                    f"{sum(valid_counts):,}" if valid_counts else "---"
                )
                if valid_counts:
                    grand_total += sum(valid_counts)
                    has_grand_total = True
            tracker_count_row = table.rowCount() - 1
            for column in module_columns:
                counts = [
                    _editable_count(table.item(row, column).text())
                    for row in tracker_rows
                ]
                valid_counts = [value for value in counts if value is not None]
                table.item(tracker_count_row, column).setText(
                    f"{sum(valid_counts):,}" if valid_counts else "---"
                )
            table.item(tracker_count_row, total_column).setText(
                f"{grand_total:,}" if has_grand_total else "---"
            )
        finally:
            table.blockSignals(False)

    def edited_extractions(self) -> tuple[OverallSiteExtraction, ...]:
        """Rebuild Agent2 results from the editable review widgets."""

        if not self._source_results:
            return self._source_results
        table = self._tracker_table
        trackers_by_result: list[list[TrackerOcrFields]] = [
            [] for _result in self._source_results
        ]
        if table is not None:
            for column, (result_index, tracker_index) in enumerate(
                self._module_locations, start=1
            ):
                is_new_tracker = tracker_index < 0
                if is_new_tracker:
                    template = next(
                        tracker
                        for result in self._source_results
                        for tracker in result.tracker_ocr_fields
                    )
                    tracker = TrackerOcrFields(
                        module_model=replace(
                            template.module_model, value=None, source=None
                        ),
                        module_stc_dc_rating_w=replace(
                            template.module_stc_dc_rating_w,
                            value=None,
                            source=None,
                        ),
                        string_count=replace(
                            template.string_count, value=None, source=None
                        ),
                        num_str=replace(
                            template.num_str, value=None, source=None
                        ),
                        num_str_details=(),
                    )
                else:
                    tracker = self._source_results[result_index].tracker_ocr_fields[
                        tracker_index
                    ]
                details: list[NumStrDetail] = []
                for offset, tracker_type in enumerate(self._tracker_types):
                    count = _editable_text(
                        table.item(2 + offset, column).text()
                    )
                    if not count:
                        continue
                    source = next(
                        (
                            detail.source
                            for detail in tracker.num_str_details
                            if _tracker_key(detail.num_str)
                            == _tracker_key(tracker_type)
                        ),
                        None,
                    )
                    details.append(NumStrDetail(tracker_type, count, source))
                edited_tracker = TrackerOcrFields(
                    module_model=replace(
                        tracker.module_model,
                        value=_editable_text(
                            table.horizontalHeaderItem(column).text()
                        ),
                    ),
                    module_stc_dc_rating_w=replace(
                        tracker.module_stc_dc_rating_w,
                        value=_editable_text(table.item(0, column).text()),
                    ),
                    string_count=replace(
                        tracker.string_count,
                        value=_editable_text(table.item(1, column).text()),
                    ),
                    num_str=replace(
                        tracker.num_str,
                        value=(
                            ", ".join(detail.num_str for detail in details)
                            or None
                        ),
                    ),
                    num_str_details=tuple(details),
                )
                trackers_by_result[result_index].append(edited_tracker)

        edited: list[OverallSiteExtraction] = []
        for result_index, result in enumerate(self._source_results):
            inv_table = self._inv_tables[result_index]
            invs = result.inv_list
            inv_rows = result.inv_rows
            if inv_table is not None:
                seen: set[str] = set()
                edited_invs: list[str] = []
                for row in range(inv_table.rowCount()):
                    inv = _normalize_inv_editor_value(
                        inv_table.item(row, 0).text()
                    )
                    if not inv or inv in seen:
                        continue
                    seen.add(inv)
                    edited_invs.append(inv)
                invs = tuple(edited_invs)
                inv_rows = tuple(
                    InvOcrRow(identifier=inv) for inv in edited_invs
                )
            edited.append(
                replace(
                    result,
                    tracker_ocr_fields=tuple(trackers_by_result[result_index]),
                    inv_list=invs,
                    inv_rows=inv_rows,
                    inv_count=len(invs),
                )
            )
        return tuple(edited)

    def retranslate(self) -> None:
        for index, page_number in enumerate(self._preview_page_numbers):
            self.preview_tabs.setTabText(
                index,
                tr("ai_statistics.page_number", "Page {number}").format(
                    number=page_number
                ),
            )
        interaction_tip = tr(
            "ai_statistics.preview_interaction_tip",
            "Mouse wheel to zoom; drag to inspect details",
        )
        self.zoom_out_button.setToolTip(interaction_tip)
        self.zoom_in_button.setToolTip(interaction_tip)
        self.preview_tabs.setToolTip(interaction_tip)
        self.fit_button.setText(
            tr("ai_statistics.preview_fit", "Fit to Window")
        )
        self.actual_size_button.setText(
            tr("ai_statistics.preview_actual", "Actual Size")
        )
        self.preview_title.setText(tr("ai_statistics.info_recognition.preview",
                                      "Overall Layout Page"))
        self.preview_meta.setText(tr("ai_statistics.fit_page", "Fit to page"))
        self.preview_hint.setText(tr("ai_statistics.info_recognition.empty_preview",
                                     "Classification results will select the layout page."))
        self.review_title.setText(tr("ai_statistics.info_recognition.review",
                                     "Information Recognition"))
        self.orientation_title.setText(
            tr(
                "ai_statistics.info_recognition.orientation_required",
                "Panel installation direction",
            )
        )
        self.orientation_help.setText(
            tr(
                "ai_statistics.info_recognition.orientation_help",
                "Choose the module polarity direction used across the "
                "overall layout.",
            )
        )
        self.orientation_combo.setItemText(
            0,
            tr(
                "ai_statistics.info_recognition.orientation_placeholder",
                "Select an orientation…",
            ),
        )
        if self._tracker_edit_button is not None:
            self._tracker_edit_button.setText(
                tr("ai_statistics.info_recognition.edit", "Edit")
            )
        if self._tracker_add_button is not None:
            self._tracker_add_button.setText("+")
            self._tracker_add_button.setToolTip(
                tr("ai_statistics.info_recognition.add_row", "Add Row")
            )
        if self._tracker_add_column_button is not None:
            self._tracker_add_column_button.setText("+")
            self._tracker_add_column_button.setToolTip(
                tr(
                    "ai_statistics.info_recognition.add_column", "Add Column"
                )
            )
        if self._tracker_delete_row_button is not None:
            self._tracker_delete_row_button.setText("×")
            self._tracker_delete_row_button.setToolTip(
                tr("ai_statistics.info_recognition.delete_row", "Delete Row")
            )
        if self._tracker_delete_column_button is not None:
            self._tracker_delete_column_button.setText("×")
            self._tracker_delete_column_button.setToolTip(
                tr(
                    "ai_statistics.info_recognition.delete_column",
                    "Delete Column",
                )
            )
        if self._tracker_save_button is not None:
            self._tracker_save_button.setText(
                tr("ai_statistics.info_recognition.save", "Save Table")
            )
        if self._tracker_cancel_button is not None:
            self._tracker_cancel_button.setText(
                tr("ai_statistics.info_recognition.cancel", "Cancel")
            )
        self.result_tree.setHeaderLabels([
            tr("ai_statistics.info_recognition.field", "Field"),
            tr("ai_statistics.info_recognition.value", "Value"),
            tr("ai_statistics.info_recognition.source", "OCR source"),
        ])
        self.review_meta.setText(_localized_status(self._review_status))
        # isVisible() is false when an ancestor (for example the inactive
        # stacked page) is hidden. isHidden() reflects this pane's own state,
        # which is what distinguishes populated results from running/empty.
        if not self.result_tree.isHidden() and self._source_results:
            # Result cards and table labels are created dynamically. Rebuild
            # them on a locale change, while carrying across any current cell
            # edits instead of reverting to the original OCR values.
            was_editing = self._tracker_edit_snapshot is not None
            edited_results = self.edited_extractions()
            scroll_value = self.result_tree.verticalScrollBar().value()
            self._show_extractions(edited_results)
            if was_editing:
                self._begin_tracker_edit()
            self.result_tree.verticalScrollBar().setValue(scroll_value)
        elif self._review_running:
            self.review_hint.setText(
                tr(
                    "ai_statistics.info_recognition.running",
                    "MinerU table location and Claude vision are running…",
                )
            )
        else:
            key = (
                "ai_statistics.info_recognition.failed"
                if self._review_status == "failed"
                else "ai_statistics.info_recognition.empty_review"
            )
            fallback = (
                "Recognition failed. Use Run AI Model to retry."
                if self._review_status == "failed"
                else "Run information recognition to review Tracker and INV data."
            )
            self.review_hint.setText(tr(key, fallback))
        self.retranslate_confirm()

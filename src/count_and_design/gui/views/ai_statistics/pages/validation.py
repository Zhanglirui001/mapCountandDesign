"""Validation stage view."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLineEdit,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .....application.ai_statistics.models import (
    TrackerOcrTotal,
    TrackerQuantity,
    ValidationInvComparison,
    ValidationNodeComparison,
    ValidationReport,
    ValidationTrackerComparison,
)
from .....application.ai_statistics.validation import validation_report_to_export_payload
from .....i18n import tr
from ..widgets import empty_panel


_TABLE_ROW_HEIGHT = 28
_TABLE_MIN_COLUMN_WIDTH = 112
_HORIZONTAL_SCROLL_COLUMN_THRESHOLD = 9
_FILTER_ALL = "all"
_FILTER_MISMATCH = "mismatch"
_FILTER_PASSED = "passed"


class ValidationPage(QWidget):
    """Compare OCR totals with the recognized project structure."""

    import_to_count_requested = Signal(dict)
    # Double-clicking a comparison row asks the tab to reopen the matching
    # Agent3 page (INV) or Agent3 Node so a mismatch can be fixed in place.
    inv_activated = Signal(object)
    node_activated = Signal(object)

    _EMPTY_INDEX = 0
    _REPORT_INDEX = 1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ValidationPage")
        self._report: ValidationReport | None = None
        self._pdf_name: str | None = None
        self._selected_inv_name = ""
        self._inv_filter = _FILTER_ALL
        self._inv_query = ""
        self._visible_inv_comparisons: tuple[ValidationInvComparison, ...] = ()
        self._visible_node_comparisons: tuple[ValidationNodeComparison, ...] = ()
        self._filter_buttons: dict[str, QPushButton] = {}
        self._setup_ui()
        self.retranslate()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget()
        root.addWidget(self._stack, 1)

        panel, self.title_label, self.meta_label, self.hint_label = empty_panel()
        self._stack.addWidget(panel)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(12)
        scroll.setWidget(content)
        self._stack.addWidget(scroll)

        report_panel = _section_frame()
        self._report_layout = QVBoxLayout(report_panel)
        self._report_layout.setContentsMargins(12, 12, 12, 12)
        self._report_layout.setSpacing(14)
        self._build_action_row()
        self._build_ocr_section()
        self._build_inv_section()
        self._build_tracker_section()
        self._content_layout.addWidget(report_panel)
        self._content_layout.addStretch()

    def _build_action_row(self) -> None:
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addStretch()
        self._download_button = QPushButton()
        self._download_button.setObjectName("validationDownloadButton")
        self._download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._download_button.setEnabled(False)
        self._download_button.clicked.connect(self._download_final_json)
        actions.addWidget(self._download_button)
        self._import_button = QPushButton()
        self._import_button.setObjectName("validationImportButton")
        self._import_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._import_button.setEnabled(False)
        self._import_button.clicked.connect(self._import_to_count)
        actions.addWidget(self._import_button)
        self._report_layout.addLayout(actions)

    def _build_ocr_section(self) -> None:
        section = QWidget()
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        header = _section_header()
        self._ocr_title = _section_title()
        self._ocr_badge = _badge_label()
        header.addWidget(self._ocr_title)
        header.addWidget(self._ocr_badge)
        header.addStretch()
        layout.addLayout(header)
        self._ocr_table = _table("agent2OcrTable")
        layout.addWidget(self._ocr_table)
        self._report_layout.addWidget(section)

    def _build_inv_section(self) -> None:
        section = QWidget()
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        header = _section_header()
        self._inv_title = _section_title()
        header.addWidget(self._inv_title)
        header.addStretch()
        layout.addLayout(header)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        for filter_key in (_FILTER_ALL, _FILTER_MISMATCH, _FILTER_PASSED):
            button = QPushButton()
            button.setObjectName("validationFilterButton")
            button.setProperty("filterState", filter_key)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, key=filter_key: self._set_inv_filter(key)
            )
            self._filter_buttons[filter_key] = button
            controls.addWidget(button)
        controls.addStretch()
        self._inv_search = QLineEdit()
        self._inv_search.setObjectName("validationInvSearch")
        self._inv_search.setClearButtonEnabled(True)
        self._inv_search.textChanged.connect(self._set_inv_search)
        controls.addWidget(self._inv_search)
        layout.addLayout(controls)

        self._inv_table = _table("invComparisonTable")
        self._inv_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._inv_table.itemSelectionChanged.connect(self._sync_node_details)
        self._inv_table.itemDoubleClicked.connect(self._emit_inv_activated)
        layout.addWidget(self._inv_table)

        self._node_title = QLabel()
        self._node_title.setObjectName("validationSubTitle")
        layout.addWidget(self._node_title)
        self._node_table = _table("nodeDetailsTable")
        self._node_table.itemDoubleClicked.connect(self._emit_node_activated)
        layout.addWidget(self._node_table)
        self._report_layout.addWidget(section)

    def _build_tracker_section(self) -> None:
        section = QWidget()
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        header = _section_header()
        self._tracker_title = _section_title()
        header.addWidget(self._tracker_title)
        header.addStretch()
        layout.addLayout(header)
        self._tracker_table = _table("trackerValidationTable")
        layout.addWidget(self._tracker_table)
        self._report_layout.addWidget(section)

    def set_pdf_name(self, filename: str | None) -> None:
        self._pdf_name = filename

    def set_report(self, report: ValidationReport | None) -> None:
        self._report = report
        if report is None:
            self._stack.setCurrentIndex(self._EMPTY_INDEX)
            self._update_action_buttons()
            return
        if not self._selected_inv_name and report.inv_comparisons:
            self._selected_inv_name = report.inv_comparisons[0].inv_name
        self._populate_report()
        self._stack.setCurrentIndex(self._REPORT_INDEX)
        self._update_action_buttons()

    def retranslate(self) -> None:
        self.title_label.setText(
            tr("ai_statistics.validation.title", "Validation Results")
        )
        self.meta_label.setText(tr("ai_statistics.status.waiting", "waiting"))
        self.hint_label.setText(
            tr(
                "ai_statistics.validation.empty",
                "Complete the previous stages to calculate validation results.",
            )
        )
        self._ocr_title.setText(
            tr("ai_statistics.validation.ocr_output", "1. Agent2 OCR Output")
        )
        self._ocr_badge.setText(
            tr("ai_statistics.validation.confirmed_baseline", "confirmed baseline")
        )
        self._download_button.setText(
            tr("ai_statistics.validation.download", "Download")
        )
        self._import_button.setText(
            tr("ai_statistics.validation.import_to_count", "Import to Count")
        )
        self._inv_title.setText(
            tr(
                "ai_statistics.validation.inv_comparison",
                "2. INV / Node Auto Calculation Comparison",
            )
        )
        self._node_title.setText(
            tr("ai_statistics.validation.node_details", "Node Details")
        )
        self._inv_search.setPlaceholderText(
            tr("ai_statistics.validation.search_inv", "Search INV...")
        )
        self._tracker_title.setText(
            tr(
                "ai_statistics.validation.tracker_total",
                "3. Tracker Total Validation",
            )
        )
        if self._report is not None:
            self._populate_report()

    def final_export_payload(self) -> dict[str, object]:
        if self._report is None:
            return {
                "project_type": "",
                "length_identifiers": [],
                "invs": [],
            }
        return validation_report_to_export_payload(self._report)

    def _download_final_json(self) -> None:
        if self._report is None:
            return
        file_path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            tr("ai_statistics.validation.download_title", "Download Final JSON"),
            default_agent4_export_filename(self._pdf_name),
            tr("ai_statistics.validation.json_filter", "JSON Files (*.json)"),
        )
        if not file_path:
            return
        target = Path(file_path)
        if target.suffix.lower() != ".json":
            target = target.with_suffix(".json")
        try:
            target.write_text(
                json.dumps(self.final_export_payload(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            QMessageBox.warning(
                self,
                tr("ai_statistics.validation.download_failed", "Download Failed"),
                str(exc),
            )
            return
        QMessageBox.information(
            self,
            tr("ai_statistics.validation.downloaded", "Downloaded"),
            tr(
                "ai_statistics.validation.downloaded_message",
                "Final JSON saved to:\n{path}",
            ).format(path=str(target)),
        )

    def _import_to_count(self) -> None:
        if self._report is None:
            return
        self.import_to_count_requested.emit(self.final_export_payload())

    def _update_action_buttons(self) -> None:
        enabled = self._report is not None
        self._download_button.setEnabled(enabled)
        self._import_button.setEnabled(enabled)

    def _populate_report(self) -> None:
        if self._report is None:
            return
        tracker_types = _tracker_types(self._report)
        self._populate_ocr_table(self._report.tracker_totals)
        self._populate_inv_table(self._filtered_inv_comparisons(), tracker_types)
        self._populate_tracker_table(self._report.tracker_comparisons)
        self._update_summary()

    def _populate_ocr_table(self, rows: tuple[TrackerOcrTotal, ...]) -> None:
        power_columns = _power_columns(rows)
        headers = [
            tr("ai_statistics.validation.col.tracker_basis", "Tracker Total Basis"),
            *power_columns,
            tr("ai_statistics.validation.col.ocr_total", "OCR Total"),
        ]
        self._ocr_table.setColumnCount(len(headers))
        self._ocr_table.setHorizontalHeaderLabels(headers)
        self._ocr_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = {field.name: field.value for field in row.fields}
            _set_item(self._ocr_table, row_index, 0, row.tracker_type)
            for column_index, power in enumerate(power_columns, start=1):
                _set_item(self._ocr_table, row_index, column_index, values.get(power, "0"))
            _set_item(
                self._ocr_table,
                row_index,
                len(headers) - 1,
                str(row.total),
                text_color="#1257f5",
            )
        _fit_table(self._ocr_table)

    def _populate_inv_table(
        self,
        rows: tuple[ValidationInvComparison, ...],
        tracker_types: tuple[str, ...],
    ) -> None:
        headers = [
            tr("ai_statistics.validation.col.inv", "INV"),
            tr("ai_statistics.validation.col.page", "Page"),
            tr("ai_statistics.validation.col.nodes", "Nodes"),
            *(f"{tracker_type} total" for tracker_type in tracker_types),
            tr("ai_statistics.validation.col.total_string", "Total String"),
            tr("ai_statistics.validation.col.layout_ocr", "Layout OCR"),
            tr("ai_statistics.validation.col.diff", "Diff"),
            tr("ai_statistics.validation.col.status", "Status"),
        ]
        self._visible_inv_comparisons = rows
        self._inv_table.blockSignals(True)
        self._inv_table.setColumnCount(len(headers))
        self._inv_table.setHorizontalHeaderLabels(headers)
        self._inv_table.setRowCount(len(rows))
        selected_row = 0
        for row_index, row in enumerate(rows):
            if row.inv_name == self._selected_inv_name:
                selected_row = row_index
            _set_item(self._inv_table, row_index, 0, row.inv_name)
            _set_item(
                self._inv_table,
                row_index,
                1,
                f"P{row.page}" if row.page is not None else "",
            )
            _set_item(self._inv_table, row_index, 2, str(row.node_count))
            next_column = 3
            for tracker_type in tracker_types:
                _set_item(
                    self._inv_table,
                    row_index,
                    next_column,
                    str(_count_for_type(row.tracker_counts, tracker_type)),
                )
                next_column += 1
            _set_item(self._inv_table, row_index, next_column, str(row.calculated_total))
            _set_item(self._inv_table, row_index, next_column + 1, str(row.ocr_total))
            _set_diff_item(self._inv_table, row_index, next_column + 2, row.diff)
            _set_status_item(self._inv_table, row_index, next_column + 3, row.passed)
        if rows:
            self._selected_inv_name = rows[selected_row].inv_name
            self._inv_table.selectRow(selected_row)
        else:
            self._selected_inv_name = ""
            self._inv_table.clearSelection()
        self._inv_table.blockSignals(False)
        _fit_table(self._inv_table, max_visible_rows=6)
        self._sync_node_details()

    def _sync_node_details(self) -> None:
        if self._report is None:
            return
        current_row = self._inv_table.currentRow()
        if current_row >= 0 and self._inv_table.item(current_row, 0) is not None:
            self._selected_inv_name = self._inv_table.item(current_row, 0).text()
        selected = next(
            (
                row
                for row in self._visible_inv_comparisons
                if row.inv_name == self._selected_inv_name
            ),
            self._visible_inv_comparisons[0]
            if self._visible_inv_comparisons
            else None,
        )
        tracker_types = _tracker_types(self._report)
        nodes = selected.nodes if selected is not None else ()
        self._visible_node_comparisons = nodes
        self._populate_node_table(nodes, tracker_types)

    def _populate_node_table(
        self,
        rows: tuple[ValidationNodeComparison, ...],
        tracker_types: tuple[str, ...],
    ) -> None:
        headers = [
            tr("ai_statistics.validation.col.node", "Node"),
            *(f"{tracker_type} total" for tracker_type in tracker_types),
            tr("ai_statistics.validation.col.total_string", "Total String"),
            tr("ai_statistics.validation.col.layout_ocr", "Layout OCR"),
            tr("ai_statistics.validation.col.diff", "Diff"),
            tr("ai_statistics.validation.col.status", "Status"),
        ]
        self._node_table.setColumnCount(len(headers))
        self._node_table.setHorizontalHeaderLabels(headers)
        self._node_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            _set_item(self._node_table, row_index, 0, row.node_name)
            next_column = 1
            for tracker_type in tracker_types:
                _set_item(
                    self._node_table,
                    row_index,
                    next_column,
                    str(_count_for_type(row.tracker_counts, tracker_type)),
                )
                next_column += 1
            _set_item(self._node_table, row_index, next_column, str(row.calculated_total))
            layout_ocr = str(row.ocr_total) if row.ocr_total_present else ""
            _set_item(self._node_table, row_index, next_column + 1, layout_ocr)
            if row.ocr_total_present:
                _set_diff_item(self._node_table, row_index, next_column + 2, row.diff)
            else:
                _set_item(self._node_table, row_index, next_column + 2, "")
            _set_status_item(self._node_table, row_index, next_column + 3, row.passed)
        _fit_table(self._node_table, max_visible_rows=7)

    def _populate_tracker_table(
        self,
        rows: tuple[ValidationTrackerComparison, ...],
    ) -> None:
        display_rows = [
            (
                row.tracker_type,
                row.structure_total,
                row.ocr_total,
                row.diff,
                row.passed,
            )
            for row in rows
        ]
        headers = [
            tr("ai_statistics.validation.col.basis", "Basis"),
            tr("ai_statistics.validation.col.structure_total", "Structure Total"),
            tr("ai_statistics.validation.col.ocr_total", "OCR Total"),
            tr("ai_statistics.validation.col.diff", "Diff"),
            tr("ai_statistics.validation.col.status", "Status"),
        ]
        self._tracker_table.setColumnCount(len(headers))
        self._tracker_table.setHorizontalHeaderLabels(headers)
        self._tracker_table.setRowCount(len(display_rows))
        for row_index, row in enumerate(display_rows):
            basis, structure_total, ocr_total, diff, passed = row
            _set_item(self._tracker_table, row_index, 0, basis)
            _set_item(self._tracker_table, row_index, 1, str(structure_total))
            _set_item(self._tracker_table, row_index, 2, str(ocr_total))
            _set_diff_item(self._tracker_table, row_index, 3, diff)
            _set_status_item(self._tracker_table, row_index, 4, passed)
        _fit_table(self._tracker_table)

    def _emit_inv_activated(self, item: QTableWidgetItem) -> None:
        row = item.row()
        if 0 <= row < len(self._visible_inv_comparisons):
            self.inv_activated.emit(self._visible_inv_comparisons[row])

    def _emit_node_activated(self, item: QTableWidgetItem) -> None:
        row = item.row()
        if 0 <= row < len(self._visible_node_comparisons):
            self.node_activated.emit(self._visible_node_comparisons[row])

    def _set_inv_filter(self, filter_key: str) -> None:
        if filter_key == self._inv_filter:
            self._update_summary()
            return
        self._inv_filter = filter_key
        if self._report is not None:
            self._populate_report()

    def _set_inv_search(self, text: str) -> None:
        self._inv_query = text.strip().casefold()
        if self._report is not None:
            self._populate_report()

    def _filtered_inv_comparisons(self) -> tuple[ValidationInvComparison, ...]:
        if self._report is None:
            return ()
        rows = self._report.inv_comparisons
        if self._inv_filter == _FILTER_MISMATCH:
            rows = tuple(row for row in rows if not row.passed)
        elif self._inv_filter == _FILTER_PASSED:
            rows = tuple(row for row in rows if row.passed)
        if self._inv_query:
            rows = tuple(
                row for row in rows if self._inv_query in row.inv_name.casefold()
            )
        return rows

    def _update_summary(self) -> None:
        if self._report is None:
            counts = {
                _FILTER_ALL: 0,
                _FILTER_MISMATCH: 0,
                _FILTER_PASSED: 0,
            }
        else:
            mismatch_count = sum(
                1 for row in self._report.inv_comparisons if not row.passed
            )
            passed_count = sum(1 for row in self._report.inv_comparisons if row.passed)
            counts = {
                _FILTER_ALL: len(self._report.inv_comparisons),
                _FILTER_MISMATCH: mismatch_count,
                _FILTER_PASSED: passed_count,
            }
        labels = {
            _FILTER_ALL: tr(
                "ai_statistics.validation.filter.all",
                "All {count}",
            ),
            _FILTER_MISMATCH: tr(
                "ai_statistics.validation.filter.mismatch",
                "\u2022 Mismatch {count}",
            ),
            _FILTER_PASSED: tr(
                "ai_statistics.validation.filter.passed",
                "\u2022 Passed {count}",
            ),
        }
        for filter_key, button in self._filter_buttons.items():
            text = labels[filter_key].format(count=counts[filter_key])
            button.setText(text)
            button.setMinimumWidth(button.fontMetrics().horizontalAdvance(text) + 24)
            button.setChecked(filter_key == self._inv_filter)
            button.style().unpolish(button)
            button.style().polish(button)


def default_agent4_export_filename(
    pdf_name: str | None,
    *,
    now: datetime | None = None,
) -> str:
    """Build the default Agent4 export filename from the imported PDF name."""

    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    if pdf_name:
        safe_stem = re.sub(
            r'[<>:"/\\|?*]+',
            "_",
            Path(pdf_name).stem,
        ).strip(" .") or "agent4-final"
        return f"{safe_stem}_{timestamp}.json"
    return f"agent4-final_{timestamp}.json"


def _section_frame() -> QFrame:
    frame = QFrame()
    frame.setObjectName("panel")
    return frame


def _section_header() -> QHBoxLayout:
    header = QHBoxLayout()
    header.setContentsMargins(0, 0, 0, 0)
    header.setSpacing(8)
    return header


def _section_title() -> QLabel:
    label = QLabel()
    label.setObjectName("validationSectionTitle")
    return label


def _badge_label() -> QLabel:
    label = QLabel()
    label.setObjectName("statusBadge")
    label.setProperty("status", "confirmed")
    return label


class _ContainedWheelTable(QTableWidget):
    """Keep wheel scrolling inside the table while it has scrollable rows."""

    def wheelEvent(self, event) -> None:  # noqa: ANN001
        scroll_bar = self.verticalScrollBar()
        delta = event.angleDelta().y()
        can_scroll_up = delta > 0 and scroll_bar.value() > scroll_bar.minimum()
        can_scroll_down = delta < 0 and scroll_bar.value() < scroll_bar.maximum()
        if can_scroll_up or can_scroll_down:
            super().wheelEvent(event)
            event.accept()
            return
        super().wheelEvent(event)


def _table(object_name: str) -> QTableWidget:
    table = _ContainedWheelTable()
    table.setObjectName(object_name)
    table.setAlternatingRowColors(True)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setFocusPolicy(Qt.FocusPolicy.WheelFocus)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(_TABLE_ROW_HEIGHT)
    table.verticalHeader().setMinimumSectionSize(_TABLE_ROW_HEIGHT)
    table.horizontalHeader().setMinimumSectionSize(_TABLE_MIN_COLUMN_WIDTH)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
    table.setShowGrid(True)
    table.setWordWrap(False)
    return table


def _set_item(
    table: QTableWidget,
    row: int,
    column: int,
    text: str,
    *,
    align_left: bool = False,
    emphasis: bool = False,
    text_color: str | None = None,
) -> None:
    item = QTableWidgetItem(text)
    alignment = Qt.AlignmentFlag.AlignVCenter
    alignment |= Qt.AlignmentFlag.AlignLeft if align_left else Qt.AlignmentFlag.AlignCenter
    item.setTextAlignment(alignment)
    if emphasis:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setForeground(QColor("#1257f5"))
    elif text_color:
        item.setForeground(QColor(text_color))
    table.setItem(row, column, item)


def _set_diff_item(table: QTableWidget, row: int, column: int, value: int) -> None:
    text = f"+{value}" if value > 0 else str(value)
    _set_item(table, row, column, text)
    item = table.item(row, column)
    if item is None:
        return
    if value == 0:
        item.setForeground(QColor("#047857"))
        item.setBackground(QColor("#ecfdf3"))
    else:
        item.setForeground(QColor("#dc2626"))
        item.setBackground(QColor("#fee2e2"))


def _set_status_item(table: QTableWidget, row: int, column: int, passed: bool) -> None:
    text = tr("ai_statistics.validation.passed", "Passed") if passed else tr(
        "ai_statistics.validation.mismatch",
        "Mismatch",
    )
    _set_item(table, row, column, text)
    item = table.item(row, column)
    if item is None:
        return
    if passed:
        item.setForeground(QColor("#047857"))
        item.setBackground(QColor("#ecfdf3"))
    else:
        item.setForeground(QColor("#dc2626"))
        item.setBackground(QColor("#fee2e2"))


def _fit_table(table: QTableWidget, max_visible_rows: int | None = None) -> None:
    header = table.horizontalHeader()
    use_horizontal_scroll = table.columnCount() > _HORIZONTAL_SCROLL_COLUMN_THRESHOLD
    scroll_policy = (
        Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        if use_horizontal_scroll
        else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    table.setHorizontalScrollBarPolicy(scroll_policy)
    resize_mode = (
        QHeaderView.ResizeMode.Interactive
        if use_horizontal_scroll
        else QHeaderView.ResizeMode.Stretch
    )
    for column in range(table.columnCount()):
        header.setSectionResizeMode(column, resize_mode)
        if use_horizontal_scroll:
            table.setColumnWidth(column, _TABLE_MIN_COLUMN_WIDTH)
    row_count = table.rowCount()
    for row in range(row_count):
        table.setRowHeight(row, _TABLE_ROW_HEIGHT)
    visible_rows = row_count
    if max_visible_rows is not None:
        visible_rows = min(row_count, max_visible_rows)
    visible_rows = max(visible_rows, 1)
    horizontal_scroll_height = (
        table.horizontalScrollBar().sizeHint().height()
        if use_horizontal_scroll
        else 0
    )
    height = (
        table.horizontalHeader().height()
        + visible_rows * _TABLE_ROW_HEIGHT
        + table.frameWidth() * 2
        + horizontal_scroll_height
    )
    table.setMinimumHeight(height)
    table.setMaximumHeight(height if max_visible_rows is not None else height + 24)


def _power_columns(rows: tuple[TrackerOcrTotal, ...]) -> tuple[str, ...]:
    columns: list[str] = []
    for row in rows:
        for field in row.fields:
            if field.name not in columns:
                columns.append(field.name)
    return tuple(columns)


def _tracker_types(report: ValidationReport) -> tuple[str, ...]:
    tracker_types: list[str] = []
    if report.reviewed_tree is not None:
        for inv in report.reviewed_tree.invs:
            for node in inv.nodes:
                for quantity in node.tracker_counts:
                    if quantity.tracker_count <= 0:
                        continue
                    if quantity.tracker_type not in tracker_types:
                        tracker_types.append(quantity.tracker_type)
    if tracker_types:
        return tuple(tracker_types)
    for inv in report.inv_comparisons:
        for quantity in inv.tracker_counts:
            if quantity.tracker_count <= 0:
                continue
            if quantity.tracker_type not in tracker_types:
                tracker_types.append(quantity.tracker_type)
    return tuple(tracker_types)


def _count_for_type(
    quantities: tuple[TrackerQuantity, ...],
    tracker_type: str,
) -> int:
    return next(
        (
            quantity.tracker_count
            for quantity in quantities
            if quantity.tracker_type == tracker_type
        ),
        0,
    )

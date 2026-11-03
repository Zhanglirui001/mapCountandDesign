"""Count tab view."""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QThread,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDoubleSpinBox,
    QFrame,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...i18n import tr, TranslationManager
from ...application.voltage_drop import (
    VoltageCalculationCancelled,
)
from ..tooltips import TooltipManager
from ..viewmodels.count_tab import (
    CountTabViewModel,
    VoltageCalculationRequest,
)
from ..viewmodels.states import (
    BreakdownState,
    CountTabState,
    EditorState,
    MetricState,
    ReadOnlyResultsState,
    TableSectionState,
    TreeNodeState,
)
from .ordered_selection import OrderedTreeWidget
from .widgets import LengthEdit, LynxPresetEdit, ProjectTypeEdit, RackingsEdit
from .voltage_focus import VoltageFocusWidget


class ResultsTableModel(QAbstractTableModel):
    """Lazy model for large, read-only statistics tables."""

    def __init__(self, section: TableSectionState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._headers = section.headers
        self._rows = section.rows

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._headers)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        value = self._rows[index.row()][index.column()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return value
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self._headers)
        ):
            return self._headers[section]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


class CopyableResultsTable(QTableView):
    """Read-only table that copies the selected rectangle as TSV."""

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            indexes = self.selectedIndexes()
            if indexes:
                cells = {(index.row(), index.column()): index.data() or "" for index in indexes}
                rows = range(min(index.row() for index in indexes), max(index.row() for index in indexes) + 1)
                columns = range(
                    min(index.column() for index in indexes),
                    max(index.column() for index in indexes) + 1,
                )
                text = "\n".join(
                    "\t".join(str(cells.get((row, column), "")) for column in columns)
                    for row in rows
                )
                QApplication.clipboard().setText(text)
                event.accept()
                return
        super().keyPressEvent(event)


class VoltageDropWorker(QThread):
    """Calculate voltage drop on an isolated project snapshot."""

    result_ready = Signal(object, object)
    error_occurred = Signal(object)
    cancelled = Signal()
    progress_changed = Signal(int, int)

    def __init__(
        self,
        view_model: CountTabViewModel,
        request: VoltageCalculationRequest,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._view_model = view_model
        self._request = request
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        last_emit_time = 0.0
        last_percent = -1

        def report_progress(completed: int, total: int) -> None:
            nonlocal last_emit_time, last_percent
            percent = int(completed * 100 / max(total, 1))
            now = time.monotonic()
            if percent == last_percent:
                return
            if completed < total and percent > 0 and now - last_emit_time < 0.05:
                return
            last_emit_time = now
            last_percent = percent
            self.progress_changed.emit(completed, total)

        try:
            report = self._view_model.calculate_voltage_request(
                self._request,
                progress_callback=report_progress,
                cancel_event=self._cancel_event,
            )
        except VoltageCalculationCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.error_occurred.emit(exc)
        else:
            if self._cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.result_ready.emit(self._request, report)


class MetricGrid(QWidget):
    """Responsive grid for compact result metrics."""

    metric_activated = Signal(object)

    def __init__(
        self,
        metrics: tuple[MetricState, ...],
        max_columns: int = 3,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("metricGrid")
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setHorizontalSpacing(10)
        self._layout.setVerticalSpacing(10)
        self._cards = [self._make_card(metric) for metric in metrics]
        self._max_columns = max_columns
        self._columns = 0
        self._relayout(min(max_columns, 3))

    def _make_card(self, metric: MetricState) -> QFrame:
        card = QFrame()
        card.setObjectName("metricCard")
        card.setProperty("tone", metric.tone)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 10)
        layout.setSpacing(3)

        label = QLabel(metric.label)
        label.setObjectName("metricLabel")
        label.setWordWrap(True)
        value = QLabel(metric.value)
        value.setObjectName("metricValue")
        value.setWordWrap(True)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        layout.addWidget(value)
        if metric.activation is not None:
            locate_button = QPushButton(
                tr("voltage_drop.locate_error", "Locate Error")
            )
            locate_button.setObjectName("voltageLocateButton")
            locate_button.setToolTip(metric.value)
            locate_button.clicked.connect(
                lambda _checked=False, issue=metric.activation: self.metric_activated.emit(issue)
            )
            layout.addWidget(locate_button, alignment=Qt.AlignmentFlag.AlignRight)
        return card

    def _relayout(self, columns: int) -> None:
        if columns == self._columns:
            return
        while self._layout.count():
            self._layout.takeAt(0)
        self._columns = columns
        for index, card in enumerate(self._cards):
            self._layout.addWidget(card, index // columns, index % columns)
        for column in range(columns):
            self._layout.setColumnStretch(column, 1)

    def resizeEvent(self, event) -> None:
        width = event.size().width()
        columns = (
            self._max_columns
            if width >= 720
            else min(self._max_columns, 2)
            if width >= 400
            else 1
        )
        self._relayout(columns)
        super().resizeEvent(event)


class CountTab(VoltageFocusWidget):
    """View widget for the Count tab."""

    voltage_issue_activated = Signal(object)

    def __init__(self, view_model: CountTabViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view_model = view_model
        self._tooltips = TooltipManager.instance()
        self._item_lookup: dict[str, QTreeWidgetItem] = {}
        self._suppress_count: int = 0
        self._rendered_tree: object | None = None
        self._voltage_worker: VoltageDropWorker | None = None
        self._voltage_button: QPushButton | None = None
        self._voltage_progress: QProgressBar | None = None
        self._voltage_progress_completed = 0
        self._voltage_progress_total = 1
        self._voltage_cancelling = False
        self._setup_ui()
        self._setup_shortcuts()
        self._view_model.state_changed.connect(self._render)
        self._view_model.results_changed.connect(self._render_results_update)
        self._view_model.expansion_changed.connect(self._apply_expansion)
        self._view_model.tree_item_changed.connect(self._update_tree_item)
        self._view_model.voltage_calculation_invalidated.connect(
            self._on_voltage_calculation_invalidated
        )
        self._view_model.error_occurred.connect(self._show_error)
        TranslationManager.instance().language_changed.connect(
            self._on_language_changed
        )
        self._render(self._view_model.state)
        self._render_voltage_controls()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self._tree = OrderedTreeWidget()
        self._tree.setHeaderLabel(tr("count_tab.tree_header", "Project Structure"))
        self._tree.currentItemChanged.connect(self._on_current_item_changed)
        self._tree.selection_requested.connect(self._view_model.select_items)
        self._tree.reorder_requested.connect(self._view_model.move_selected)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_tree_context_menu)
        self._tree.itemExpanded.connect(self._sync_expansion_state)
        self._tree.itemCollapsed.connect(self._sync_expansion_state)
        self._tooltips.register(self._tree, "count_tab.tooltip.tree")
        left_layout.addWidget(self._tree)

        self._editor_container = QWidget()
        right_layout = QVBoxLayout(self._editor_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self._properties_container = QWidget()
        self._properties_layout = QVBoxLayout(self._properties_container)
        self._properties_layout.setContentsMargins(0, 0, 0, 0)
        self._properties_layout.setSpacing(0)
        right_layout.addWidget(self._properties_container)

        self._results_scroll = QScrollArea()
        self._results_scroll.setObjectName("countResultsScroll")
        self._results_scroll.setWidgetResizable(True)
        self._results_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._results_scroll.setMinimumHeight(180)
        self._results_container = QWidget()
        self._results_container.setObjectName("countResultsContainer")
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setContentsMargins(12, 10, 12, 12)
        self._results_layout.setSpacing(12)
        self._results_scroll.setWidget(self._results_container)
        right_layout.addWidget(self._results_scroll, 1)

        self._editor_container.setStyleSheet(
            "QWidget#countResultsContainer { background: palette(base); }"
            "QLabel#resultsHeading { font-size: 16px; font-weight: 600; }"
            "QFrame#metricCard { background: palette(alternate-base); border: 1px solid palette(midlight); border-radius: 6px; }"
            "QFrame#metricCard[tone=success] { border-left: 4px solid #4f8a61; }"
            "QFrame#metricCard[tone=warning] { border-left: 4px solid #c58a28; }"
            "QLabel#metricLabel { color: palette(mid); font-size: 11px; }"
            "QLabel#metricValue { font-size: 15px; font-weight: 600; }"
            "QFrame#breakdownCard { background: palette(alternate-base); border: 1px solid palette(midlight); border-radius: 6px; }"
            "QLabel#sectionHeading { font-size: 13px; font-weight: 600; }"
            "QLabel#breakdownCount { font-weight: 600; }"
            "QLabel#emptyResults { color: palette(mid); padding: 12px 0; }"
            "QTableView#resultsTable { border: 1px solid palette(midlight); border-radius: 4px; gridline-color: palette(midlight); }"
        )

        splitter.addWidget(left)
        splitter.addWidget(self._editor_container)
        splitter.setSizes([300, 600])
        layout.addWidget(splitter)

    def _render_voltage_controls(self) -> None:
        if self._voltage_button is None or self._voltage_progress is None:
            return
        running = self._voltage_worker is not None
        self._voltage_progress.setRange(0, max(1, self._voltage_progress_total))
        self._voltage_progress.setValue(
            min(self._voltage_progress_completed, max(1, self._voltage_progress_total))
        )
        self._voltage_progress.setVisible(running)
        if running:
            self._voltage_button.setText(
                tr("voltage_drop.cancel_calculation", "Cancel calculation")
            )
            self._voltage_button.setEnabled(not self._voltage_cancelling)
        else:
            self._voltage_button.setText(
                tr("voltage_drop.calculate", "Calculate voltage drop")
            )
            self._voltage_button.setEnabled(True)

    def _on_voltage_button_clicked(self) -> None:
        if self._voltage_worker is not None:
            self._cancel_voltage_calculation()
            return
        request = self._view_model.create_voltage_calculation_request()
        if request is None:
            return
        worker = VoltageDropWorker(self._view_model, request, self)
        self._voltage_worker = worker
        self._voltage_cancelling = False
        self._voltage_progress_completed = 0
        self._voltage_progress_total = 1
        worker.progress_changed.connect(self._on_voltage_progress)
        worker.result_ready.connect(self._on_voltage_result)
        worker.error_occurred.connect(self._on_voltage_error)
        worker.finished.connect(self._on_voltage_worker_finished)
        self._render_voltage_controls()
        worker.start()

    def _on_voltage_progress(self, completed: int, total: int) -> None:
        self._voltage_progress_completed = completed
        self._voltage_progress_total = max(1, total)
        self._render_voltage_controls()

    def _on_voltage_result(
        self,
        request: VoltageCalculationRequest,
        report: object,
    ) -> None:
        self._view_model.accept_voltage_report(request, report)

    def _on_voltage_error(self, exc: Exception) -> None:
        QMessageBox.warning(
            self,
            tr("voltage_drop.calculation_failed", "Calculation failed"),
            str(exc),
        )

    def _on_voltage_worker_finished(self) -> None:
        worker = self._voltage_worker
        self._voltage_worker = None
        self._voltage_cancelling = False
        self._render_voltage_controls()
        if worker is not None:
            worker.deleteLater()

    def _cancel_voltage_calculation(self) -> None:
        if self._voltage_worker is None:
            return
        self._voltage_cancelling = True
        self._voltage_worker.cancel()
        self._render_voltage_controls()

    def _on_voltage_calculation_invalidated(self) -> None:
        if self._voltage_worker is not None:
            self._cancel_voltage_calculation()

    def shutdown_voltage_calculation(self, timeout_ms: int = 2000) -> bool:
        worker = self._voltage_worker
        if worker is None or not worker.isRunning():
            return True
        worker.cancel()
        return worker.wait(timeout_ms)

    def closeEvent(self, event) -> None:
        if not self.shutdown_voltage_calculation():
            event.ignore()
            return
        super().closeEvent(event)

    def _on_language_changed(self, _locale: str) -> None:
        self._view_model.refresh(rebuild_tree=True)
        self._render_voltage_controls()

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Alt+F"), self).activated.connect(self._view_model.create_child)
        QShortcut(QKeySequence("Alt+D"), self).activated.connect(self._view_model.delete_selected)
        QShortcut(QKeySequence("Alt+C"), self).activated.connect(self._view_model.copy_selected)
        QShortcut(QKeySequence("Alt+V"), self).activated.connect(self._view_model.paste_to_selected)
        QShortcut(QKeySequence("Alt+1"), self).activated.connect(lambda: self._view_model.expand_level(1))
        QShortcut(QKeySequence("Alt+2"), self).activated.connect(lambda: self._view_model.expand_level(2))
        QShortcut(QKeySequence("Alt+3"), self).activated.connect(lambda: self._view_model.expand_level(3))

    def _render(self, state: CountTabState) -> None:
        self._tree.setHeaderLabel(tr("count_tab.tree_header", "Project Structure"))
        if state.tree is not self._rendered_tree:
            self._render_tree(state)
            self._rendered_tree = state.tree
        else:
            self._sync_tree_selection(state.selected_node_ids)
        self._render_editor(state.editor)
        self._render_voltage_controls()

    def _render_tree(self, state: CountTabState) -> None:
        self._suppress_count += 1
        self._tree.blockSignals(True)
        self._tree.clear()
        self._item_lookup = {}
        for node in state.tree:
            self._add_tree_item(None, node)
        self._tree.blockSignals(False)
        self._tree.apply_selected_ids(state.selected_node_ids)
        self._suppress_count -= 1

    def _sync_tree_selection(self, node_ids: tuple[str, ...]) -> None:
        if self._tree.selected_ids() == node_ids:
            return
        self._suppress_count += 1
        self._tree.apply_selected_ids(node_ids)
        self._suppress_count -= 1

    def _add_tree_item(self, parent: QTreeWidgetItem | None, node: TreeNodeState) -> None:
        item = QTreeWidgetItem(parent or self._tree)
        item.setText(0, node.label)
        item.setData(0, Qt.ItemDataRole.UserRole, node.node_id)
        item.setData(0, Qt.ItemDataRole.UserRole + 1, node.item_type)
        item.setExpanded(node.expanded)
        self._item_lookup[node.node_id] = item
        for child in node.children:
            self._add_tree_item(item, child)

    def _render_editor(self, editor: EditorState | None) -> None:
        self.begin_voltage_field_render()
        self._clear_layout(self._properties_layout)
        self._voltage_button = None
        self._voltage_progress = None
        self._clear_layout(self._results_layout)
        if editor is None:
            self.finish_voltage_field_render()
            return
        if editor.kind == "project":
            self._render_project_editor(editor)
        elif editor.kind == "inv":
            self._render_inv_editor(editor)
        elif editor.kind == "node":
            self._render_node_editor(editor)
        elif editor.kind == "row":
            self._render_row_editor(editor)
        self._render_results(editor.results)
        self.finish_voltage_field_render()
        QTimer.singleShot(0, lambda: self._results_scroll.verticalScrollBar().setValue(0))

    def _render_results_update(self, results: ReadOnlyResultsState) -> None:
        """Update calculated results while leaving the active editor intact."""
        scroll_bar = self._results_scroll.verticalScrollBar()
        scroll_position = scroll_bar.value()
        self._voltage_button = None
        self._voltage_progress = None
        self._clear_layout(self._results_layout)
        self._render_results(results)
        QTimer.singleShot(0, lambda: scroll_bar.setValue(scroll_position))

    # -- Project editor ------------------------------------------------

    def _render_project_editor(self, editor: EditorState) -> None:
        group = QGroupBox(editor.title)
        self.register_voltage_field("__object__", group)
        form = QFormLayout(group)
        self._bind_line_edit(form, tr("count_tab.form.name", "Name:"), editor.name,
                             lambda value: self._view_model.update_project_field("name", value),
                             "count_tab.tooltip.project_name")
        self._bind_line_edit(form, tr("count_tab.form.address", "Address:"), editor.address,
                             lambda value: self._view_model.update_project_field("address", value),
                             "count_tab.tooltip.address")
        self._bind_line_edit(form, tr("count_tab.form.client_name", "Client Name:"), editor.client_name,
                             lambda value: self._view_model.update_project_field("client_name", value),
                             "count_tab.tooltip.client_name")
        project_type = ProjectTypeEdit()
        project_type.set_value(editor.project_type)
        project_type.currentTextChanged.connect(
            lambda value: self._view_model.update_project_field("project_type", value))
        self._tooltips.register(project_type, "count_tab.tooltip.project_type")
        self.register_voltage_field("project_type", project_type)
        form.addRow(tr("count_tab.form.project_type", "Project Type:"), project_type)
        pitch_edit = self._make_length_edit(editor.racking_pitch, editor.identifier_options,
                                           lambda value: self._view_model.update_project_field("racking_pitch", value),
                                           "count_tab.tooltip.racking_pitch")
        self.register_voltage_field("racking_pitch", pitch_edit)
        form.addRow(tr("count_tab.form.racking_pitch", "Racking Pitch:"), pitch_edit)
        preset_edit = self._make_lynx_preset_edit(editor.lynx_preset_name, editor.lynx_preset_options,
                                                self._view_model.update_project_lynx_preset,
                                                self._view_model.create_and_select_project_lynx_preset,
                                                "count_tab.tooltip.lynx_preset")
        self.register_voltage_field("lynx_preset", preset_edit)
        form.addRow(tr("count_tab.form.lynx_preset", "LYNX Preset:"), preset_edit)
        self._properties_layout.addWidget(group)

    # -- INV editor ---------------------------------------------------

    def _render_inv_editor(self, editor: EditorState) -> None:
        group = QGroupBox(editor.title)
        self.register_voltage_field("__object__", group)
        form = QFormLayout(group)
        self._bind_line_edit(form, tr("count_tab.form.name", "Name:"), editor.name,
                             lambda value: self._view_model.update_inv_field("name", value),
                             "count_tab.tooltip.project_name")
        pitch_edit = self._make_length_edit(editor.racking_pitch, editor.identifier_options,
                                           lambda value: self._view_model.update_inv_field("racking_pitch", value),
                                           "count_tab.tooltip.racking_pitch")
        self.register_voltage_field("racking_pitch", pitch_edit)
        form.addRow(tr("count_tab.form.racking_pitch", "Racking Pitch:"), pitch_edit)
        preset_edit = self._make_lynx_preset_edit(editor.lynx_preset_name, editor.lynx_preset_options,
                                                self._view_model.update_inv_lynx_preset,
                                                self._view_model.create_and_select_inv_lynx_preset,
                                                "count_tab.tooltip.lynx_preset")
        self.register_voltage_field("lynx_preset", preset_edit)
        form.addRow(tr("count_tab.form.lynx_preset", "LYNX Preset:"), preset_edit)
        self._properties_layout.addWidget(group)

    # -- Node editor --------------------------------------------------

    def _render_node_editor(self, editor: EditorState) -> None:
        group = QGroupBox(editor.title)
        self.register_voltage_field("__object__", group)
        form = QFormLayout(group)
        self._bind_line_edit(form, tr("count_tab.form.name", "Name:"), editor.name,
                             lambda value: self._view_model.update_node_field("name", value),
                             "count_tab.tooltip.project_name")
        pitch_edit = self._make_length_edit(editor.racking_pitch, editor.identifier_options,
                                           lambda value: self._view_model.update_node_field("racking_pitch", value),
                                           "count_tab.tooltip.racking_pitch")
        self.register_voltage_field("racking_pitch", pitch_edit)
        form.addRow(tr("count_tab.form.racking_pitch", "Racking Pitch:"), pitch_edit)
        preset_edit = self._make_lynx_preset_edit(editor.lynx_preset_name, editor.lynx_preset_options,
                                                self._view_model.update_node_lynx_preset,
                                                self._view_model.create_and_select_node_lynx_preset,
                                                "count_tab.tooltip.lynx_preset")
        self.register_voltage_field("lynx_preset", preset_edit)
        form.addRow(tr("count_tab.form.lynx_preset", "LYNX Preset:"), preset_edit)
        cluster_edit = self._make_length_edit(editor.cluster_length, editor.identifier_options,
                                           lambda value: self._view_model.update_node_field("cluster_length", value),
                                           "count_tab.tooltip.cluster_length")
        self.register_voltage_field("cluster_length", cluster_edit)
        form.addRow(tr("count_tab.form.cluster_length", "Cluster Length:"), cluster_edit)
        self._bind_float_edit(form, tr("count_tab.form.box_position", "Box Position:"), editor.box_position,
                              lambda value: self._view_model.update_node_field("box_position", value),
                              "count_tab.tooltip.box_position")
        self._properties_layout.addWidget(group)

    # -- Row editor ---------------------------------------------------

    def _render_row_editor(self, editor: EditorState) -> None:
        group = QGroupBox(editor.title)
        self.register_voltage_field("__object__", group)
        self.register_voltage_field("racking_entries", group)
        form = QFormLayout(group)
        self._bind_line_edit(form, tr("count_tab.form.name", "Name:"), editor.name,
                             lambda value: self._view_model.update_row_field("name", value),
                             "count_tab.tooltip.project_name")

        # Racking entries list
        for i, (rackings_val, offset_val) in enumerate(editor.racking_entries):
            entry_group = QGroupBox(
                tr("count_tab.form.racking_entry", "Racking Entry {n}").format(n=i + 1)
            )
            entry_form = QFormLayout(entry_group)

            rack_edit = RackingsEdit()
            rack_edit.set_rackings_names(list(editor.rackings_options))
            rack_edit.set_value(rackings_val)
            idx = i
            rack_edit.value_changed.connect(
                lambda idx=idx, widget=rack_edit: self._view_model.update_row_field(
                    f"rackings_entry_name:{idx}", widget.get_value()
                )
            )
            self._tooltips.register(rack_edit, "count_tab.tooltip.racking_entry_name")
            self.register_voltage_field(f"racking_entries[{i}].rackings", rack_edit)
            entry_form.addRow(tr("count_tab.form.rackings", "Rackings:"), rack_edit)

            len_edit = self._make_length_edit(
                offset_val, editor.identifier_options,
                lambda value, idx=idx: self._view_model.update_row_field(
                    f"rackings_entry_offset:{idx}", value
                ),
                "count_tab.tooltip.racking_entry_offset"
            )
            entry_form.addRow(tr("count_tab.form.offset", "Offset:"), len_edit)
            self.register_voltage_field(f"racking_entries[{i}].offset", len_edit)

            # Remove button (only show if more than 1 entry)
            if len(editor.racking_entries) > 1:
                remove_btn = QPushButton(
                    tr("count_tab.form.remove_entry", "Remove Entry")
                )
                remove_btn.clicked.connect(
                    lambda checked, idx=idx: self._view_model.remove_racking_entry(idx)
                )
                entry_form.addRow("", remove_btn)

            form.addRow(entry_group)

        # Add entry button
        add_btn = QPushButton(tr("count_tab.form.add_racking_entry", "+ Add Racking Entry"))
        add_btn.clicked.connect(lambda: self._view_model.add_racking_entry())
        self._tooltips.register(add_btn, "count_tab.tooltip.add_racking_entry")
        form.addRow(add_btn)

        self._properties_layout.addWidget(group)

    # -- Shared helpers -----------------------------------------------

    def _render_results(self, results: ReadOnlyResultsState) -> None:
        heading = QLabel(tr("count_tab.results.overview", "Overview"))
        heading.setObjectName("resultsHeading")
        self._results_layout.addWidget(heading)

        if results.statistics:
            statistics_heading = QLabel(tr("count_tab.section.statistics", "Statistics"))
            statistics_heading.setObjectName("sectionHeading")
            self._results_layout.addWidget(statistics_heading)
            self._results_layout.addWidget(
                MetricGrid(results.statistics, max_columns=4)
            )

        if results.voltage:
            voltage_header = QWidget()
            voltage_header_layout = QHBoxLayout(voltage_header)
            voltage_header_layout.setContentsMargins(0, 0, 0, 0)
            voltage_header_layout.setSpacing(10)
            voltage_heading = QLabel(tr("voltage_drop.section", "Voltage Drop"))
            voltage_heading.setObjectName("sectionHeading")
            self._voltage_progress = QProgressBar()
            self._voltage_progress.setMinimumWidth(150)
            self._voltage_button = QPushButton()
            self._voltage_button.clicked.connect(self._on_voltage_button_clicked)
            voltage_header_layout.addWidget(voltage_heading)
            voltage_header_layout.addWidget(self._voltage_progress, 1)
            voltage_header_layout.addWidget(self._voltage_button)
            self._results_layout.addWidget(voltage_header)
            self._render_voltage_controls()
            voltage_grid = MetricGrid(results.voltage, max_columns=3)
            voltage_grid.metric_activated.connect(self.voltage_issue_activated)
            self._results_layout.addWidget(voltage_grid)

        populated_breakdowns = tuple(item for item in results.breakdowns if item.items)
        if populated_breakdowns:
            breakdowns = QWidget()
            layout = QGridLayout(breakdowns)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setHorizontalSpacing(10)
            layout.setVerticalSpacing(10)
            for index, breakdown in enumerate(populated_breakdowns):
                layout.addWidget(self._make_breakdown_card(breakdown), index // 2, index % 2)
            layout.setColumnStretch(0, 1)
            layout.setColumnStretch(1, 1)
            self._results_layout.addWidget(breakdowns)

        if results.detail is not None:
            self._render_detail(results.detail)
        self._results_layout.addStretch()

    @staticmethod
    def _make_breakdown_card(breakdown: BreakdownState) -> QFrame:
        card = QFrame()
        card.setObjectName("breakdownCard")
        layout = QGridLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(5)
        heading = QLabel(breakdown.title)
        heading.setObjectName("sectionHeading")
        layout.addWidget(heading, 0, 0, 1, 2)
        for row, (name, count) in enumerate(breakdown.items, start=1):
            name_label = QLabel(name)
            name_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            count_label = QLabel(count)
            count_label.setObjectName("breakdownCount")
            count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            count_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(name_label, row, 0)
            layout.addWidget(count_label, row, 1)
        layout.setColumnStretch(0, 1)
        return card

    def _render_detail(self, section: TableSectionState) -> None:
        heading = QLabel(section.title)
        heading.setObjectName("sectionHeading")
        self._results_layout.addWidget(heading)
        if not section.rows:
            empty = QLabel(tr("count_tab.results.empty", "No data"))
            empty.setObjectName("emptyResults")
            self._results_layout.addWidget(empty)
            return

        table = CopyableResultsTable()
        table.setObjectName("resultsTable")
        table.setToolTip(
            tr(
                "count_tab.results.copy_hint",
                "Select cells and press Ctrl+C to copy.",
            )
        )
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.setModel(ResultsTableModel(section, table))

        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setResizeContentsPrecision(100)
        table.resizeColumnsToContents()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        row_height = table.verticalHeader().defaultSectionSize()
        table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        height = (
            header.sizeHint().height()
            + row_height * len(section.rows)
            + table.horizontalScrollBar().sizeHint().height()
            + 6
        )
        table.setFixedHeight(max(90, height))
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._results_layout.addWidget(table)

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()

    def _bind_line_edit(self, form: QFormLayout, label: str, value: str, handler, tooltip_key: str = "") -> None:
        line_edit = QLineEdit(value)
        line_edit.editingFinished.connect(lambda: handler(line_edit.text()))
        form.addRow(label, line_edit)
        if tooltip_key:
            self._tooltips.register(line_edit, tooltip_key)

    def _bind_float_edit(self, form: QFormLayout, label: str, value: float, handler, tooltip_key: str = "") -> None:
        spinbox = QDoubleSpinBox()
        spinbox.setRange(-99999.0, 99999.0)
        spinbox.setDecimals(3)
        spinbox.setValue(value)
        spinbox.valueChanged.connect(lambda v: handler(str(v)))
        form.addRow(label, spinbox)
        if tooltip_key:
            self._tooltips.register(spinbox, tooltip_key)

    def _make_length_edit(self, value: str, identifier_options: tuple[tuple[str, str], ...], handler,
                          tooltip_key: str = "") -> LengthEdit:
        widget = LengthEdit()
        widget.set_identifier_options(list(identifier_options))
        widget.set_text(value)
        widget.value_changed.connect(lambda: handler(widget.get_text()))
        if tooltip_key:
            self._tooltips.register(widget, tooltip_key)
        return widget

    def _make_lynx_preset_edit(self, current_name: str, options: tuple[str, ...], handler, create_handler,
                               tooltip_key: str = "") -> LynxPresetEdit:
        widget = LynxPresetEdit()
        widget.set_options(list(options))
        widget.set_value(current_name)
        widget.value_changed.connect(lambda val: handler(val))
        widget.create_new_pressed.connect(create_handler)
        if tooltip_key:
            self._tooltips.register(widget, tooltip_key)
        return widget

    def _on_current_item_changed(
        self,
        current: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if self._suppress_count > 0 or self._tree.internal_selection_change:
            return
        if current is None:
            return
        node_id = current.data(0, Qt.ItemDataRole.UserRole)
        if node_id is None:
            return
        self._view_model.select_item(node_id)

    def _show_tree_context_menu(self, pos) -> None:
        if self._tree.itemAt(pos) is None:
            return
        state = self._view_model.state
        selected_type = self._view_model.selected_item_type()
        enough = len(state.selected_node_ids) >= 2
        menu = QMenu(self)
        sort_menu = menu.addMenu(tr("ordering.sort", "Sort"))
        sort_menu.setEnabled(enough and selected_type is not None)
        if selected_type in {"inv", "node", "row"}:
            self._add_tree_sort_field(
                sort_menu, tr("ordering.name", "Name"), "name"
            )
        if selected_type == "inv":
            self._add_tree_sort_field(
                sort_menu,
                tr("ordering.racking_pitch", "Racking Pitch"),
                "racking_pitch",
            )
            self._add_tree_sort_field(
                sort_menu,
                tr("ordering.voltage_drop_percent", "Voltage Drop %"),
                "voltage_drop_percent",
                enabled=self._view_model.can_sort_voltage_drop(),
            )
            self._add_tree_sort_field(
                sort_menu,
                tr("ordering.cluster_total_length", "Cluster Total Length"),
                "cluster_total_length",
            )
        renumber = menu.addAction(tr("ordering.renumber", "Renumber"))
        renumber.setEnabled(self._view_model.can_renumber_selected())
        renumber.triggered.connect(self._view_model.renumber_selected)
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _add_tree_sort_field(
        self, menu: QMenu, label: str, field: str, *, enabled: bool = True
    ) -> None:
        field_menu = menu.addMenu(label)
        field_menu.setEnabled(enabled)
        ascending = field_menu.addAction(tr("ordering.ascending", "Ascending"))
        descending = field_menu.addAction(tr("ordering.descending", "Descending"))
        ascending.triggered.connect(
            lambda _checked=False, value=field: self._view_model.sort_selected(value, False)
        )
        descending.triggered.connect(
            lambda _checked=False, value=field: self._view_model.sort_selected(value, True)
        )

    def _sync_expansion_state(self, item: QTreeWidgetItem) -> None:
        if self._suppress_count > 0:
            return
        node_id = item.data(0, Qt.ItemDataRole.UserRole)
        if node_id is not None:
            self._view_model.set_expanded(node_id, item.isExpanded())

    def _apply_expansion(self, expanded_ids: frozenset[str]) -> None:
        self._suppress_count += 1
        self._tree.blockSignals(True)
        for node_id, item in self._item_lookup.items():
            expanded = node_id in expanded_ids
            if item.isExpanded() != expanded:
                item.setExpanded(expanded)
        self._tree.blockSignals(False)
        self._suppress_count -= 1

    def _update_tree_item(self, node_id: str, label: str) -> None:
        item = self._item_lookup.get(node_id)
        if item is not None:
            item.setText(0, label)

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

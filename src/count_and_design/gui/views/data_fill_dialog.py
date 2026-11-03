"""Dialog for solving and applying ordered aggregate project targets."""

from __future__ import annotations

import copy
import re
import uuid

from PySide6.QtCore import (
    QAbstractItemModel,
    QEvent,
    QModelIndex,
    QRect,
    Qt,
    QThread,
    Signal,
    QStringListModel,
    QTimer,
)
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
    QShowEvent,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QApplication,
    QCompleter,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...application.data_fill import (
    DataFillConstraints,
    DataFillError,
    DataFillFormulaError,
    DataFillPrepared,
    DataFillResult,
    DataFillService,
    format_data_fill_formula,
    parse_data_fill_formula,
    validate_formula_references,
)
from ...i18n import tr
from ...models import DataFillTarget, QuantityRef
from ..viewmodels.app import AppViewModel


def _escape_formula_name(name: str) -> str:
    return name.replace("\\", "\\\\").replace("]", "\\]")


class TargetTableWidget(QTableWidget):
    """Target table whose selected row is marked by one continuous outline."""

    _SELECTION_BORDER_WIDTH = 2

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        selected = self.selectionModel().selectedRows()
        if not selected or self.columnCount() == 0:
            return
        row = selected[0].row()
        first = self.visualRect(self.model().index(row, 0))
        last = self.visualRect(self.model().index(row, self.columnCount() - 1))
        outline = first.united(last).adjusted(1, 1, -2, -2)
        painter = QPainter(self.viewport())
        painter.setPen(QPen(
            self.palette().color(QPalette.ColorRole.Text),
            self._SELECTION_BORDER_WIDTH,
        ))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(outline)


class SelectionNeutralItemDelegate(QStyledItemDelegate):
    """Render selected cells normally; the table paints the row outline."""

    def initStyleOption(
        self, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        super().initStyleOption(option, index)
        option.state &= ~QStyle.StateFlag.State_Selected
        option.state &= ~QStyle.StateFlag.State_HasFocus


class EnabledCheckDelegate(SelectionNeutralItemDelegate):
    """Draw a stable high-contrast checkbox independent of platform colors."""

    _CHECK_SIZE = 16

    @staticmethod
    def _is_checked(index: QModelIndex) -> bool:
        value = index.data(Qt.ItemDataRole.CheckStateRole)
        try:
            return Qt.CheckState(value) == Qt.CheckState.Checked
        except (TypeError, ValueError):
            return False

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        cell_option = QStyleOptionViewItem(option)
        self.initStyleOption(cell_option, index)
        cell_option.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        cell_option.text = ""
        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem,
            cell_option,
            painter,
            widget,
        )

        rect = QRect(
            option.rect.center().x() - self._CHECK_SIZE // 2,
            option.rect.center().y() - self._CHECK_SIZE // 2,
            self._CHECK_SIZE,
            self._CHECK_SIZE,
        )
        checked = self._is_checked(index)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#606060"), 1))
        painter.setBrush(QColor("#303030") if checked else QColor("white"))
        painter.drawRoundedRect(rect, 3, 3)
        if checked:
            pen = QPen(QColor("white"), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(rect.left() + 3, rect.center().y(), rect.left() + 7, rect.bottom() - 3)
            painter.drawLine(rect.left() + 7, rect.bottom() - 3, rect.right() - 2, rect.top() + 4)
        painter.restore()

    def editorEvent(
        self,
        event: QEvent,
        model: QAbstractItemModel,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> bool:
        flags = index.flags()
        if not (
            flags & Qt.ItemFlag.ItemIsEnabled
            and flags & Qt.ItemFlag.ItemIsUserCheckable
        ):
            return False
        toggle = False
        if isinstance(event, QMouseEvent):
            toggle = (
                event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and option.rect.contains(event.position().toPoint())
            )
        elif isinstance(event, QKeyEvent):
            toggle = (
                event.type() == QEvent.Type.KeyPress
                and event.key() in {Qt.Key.Key_Space, Qt.Key.Key_Select}
            )
        if not toggle:
            return False
        checked = self._is_checked(index)
        return model.setData(
            index,
            Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked,
            Qt.ItemDataRole.CheckStateRole,
        )


class FormulaTextEdit(QPlainTextEdit):
    """Multi-line formula editor with context-aware quantity completion."""

    editing_finished = Signal()

    _TOKEN_START = frozenset("=+-*(")

    def __init__(
        self,
        racking_names: tuple[str, ...],
        product_names: tuple[str, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._racking_names = racking_names
        self._product_names = product_names
        self._inserting_completion = False
        self._completion_model = QStringListModel(self)
        self._completer = QCompleter(self._completion_model, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setWidget(self)
        self._completer.popup().setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._completer.popup().setTextElideMode(Qt.TextElideMode.ElideNone)
        self._completer.activated.connect(self._insert_completion)
        self.textChanged.connect(self._update_completions)
        self.setTabChangesFocus(True)
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        popup = self._completer.popup()
        watched_widget = watched if isinstance(watched, QWidget) else None
        belongs_to_editor = watched is self or (
            watched_widget is not None and self.isAncestorOf(watched_widget)
        )
        belongs_to_popup = watched is popup or (
            watched_widget is not None and popup.isAncestorOf(watched_widget)
        )
        if (
            isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Tab
            and popup.isVisible()
            and (belongs_to_editor or belongs_to_popup)
        ):
            if event.type() == QEvent.Type.ShortcutOverride:
                event.accept()
                return True
            if (
                event.type() == QEvent.Type.KeyPress
                and self._accept_current_completion()
            ):
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, value: str) -> None:
        self.setPlainText(value)

    def cursorPosition(self) -> int:
        return self.textCursor().position()

    def setCursorPosition(self, position: int) -> None:
        cursor = self.textCursor()
        cursor.setPosition(position)
        self.setTextCursor(cursor)

    def hasSelectedText(self) -> bool:
        return self.textCursor().hasSelection()

    def selectionStart(self) -> int:
        cursor = self.textCursor()
        return min(cursor.anchor(), cursor.position())

    def completer(self) -> QCompleter:
        return self._completer

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            self._completer.popup().isVisible()
            and event.key() == Qt.Key.Key_Tab
        ):
            self._accept_current_completion()
            return
        if (
            self._completer.popup().isVisible()
            and event.key() in {
                Qt.Key.Key_Enter,
                Qt.Key.Key_Return,
                Qt.Key.Key_Escape,
                Qt.Key.Key_Tab,
                Qt.Key.Key_Backtab,
            }
        ):
            event.ignore()
            return
        if (
            event.key() in {Qt.Key.Key_Enter, Qt.Key.Key_Return}
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.editing_finished.emit()
            return
        typed = event.text()
        cursor = self.cursorPosition()
        if typed == "[" and cursor > 0 and self.text()[cursor - 1] == "[":
            return
        if typed == "]" and cursor < len(self.text()) and self.text()[cursor] == "]":
            self.setCursorPosition(cursor + 1)
            return
        if typed.lower() in {"r", "p"} and self._at_token_start():
            cursor = self.selectionStart() if self.hasSelectedText() else cursor
            if cursor >= len(self.text()) or self.text()[cursor] != "[":
                prefix = typed.upper()
                self.textCursor().insertText(f"{prefix}[]")
                self.setCursorPosition(cursor + 2)
                self._show_name_completions(prefix, "")
                return
        super().keyPressEvent(event)

    def _at_token_start(self) -> bool:
        cursor = self.selectionStart() if self.hasSelectedText() else self.cursorPosition()
        before = self.text()[:cursor]
        if not before:
            return True
        previous = before[-1]
        return previous.isspace() or previous in self._TOKEN_START

    def _completion_context(self) -> tuple[str, int, int, str] | None:
        text = self.text()
        cursor = self.cursorPosition()
        before = text[:cursor]
        bracket_match = re.search(r"(?i)(?:^|[=+\-*()\s])([RP])\[([^\]]*)$", before)
        if bracket_match:
            prefix = bracket_match.group(1).upper()
            name_start = bracket_match.start(2)
            closing = self._find_closing_bracket(text, cursor)
            name_end = closing if closing is not None else cursor
            return prefix, name_start, name_end, bracket_match.group(2)

        keyword_match = re.search(r"(?i)(?:^|[=+\-*()\s])([A-Z_]*)$", before)
        if not keyword_match or not keyword_match.group(1):
            return None
        token = keyword_match.group(1)
        return "K", keyword_match.start(1), cursor, token

    @staticmethod
    def _find_closing_bracket(text: str, start: int) -> int | None:
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "]":
                return index
        return None

    def _show_name_completions(self, prefix: str, typed_name: str) -> None:
        names = self._racking_names if prefix == "R" else self._product_names
        self._completion_model.setStringList(list(names))
        self._completer.setCompletionPrefix(typed_name)
        if names:
            self._show_completer()

    def _update_completions(self, *_args: object) -> None:
        if self._inserting_completion:
            return
        context = self._completion_context()
        if context is None:
            self._completer.popup().hide()
            return
        prefix, _start, _end, typed = context
        if prefix in {"R", "P"}:
            self._show_name_completions(prefix, typed)
            return
        keywords = [item for item in ("NODE", "STRINGS") if item.startswith(typed.upper())]
        self._completion_model.setStringList(keywords)
        self._completer.setCompletionPrefix(typed)
        if keywords:
            self._show_completer()
        else:
            self._completer.popup().hide()

    def _show_completer(self) -> None:
        popup = self._completer.popup()
        rect = self.cursorRect()
        popup_width = max(
            280,
            self.viewport().width(),
            popup.sizeHintForColumn(0)
            + popup.verticalScrollBar().sizeHint().width()
            + 32,
        )
        popup.setMinimumWidth(popup_width)
        rect.setWidth(popup_width)
        self._completer.complete(rect)

    def _insert_completion(self, completion: str) -> None:
        context = self._completion_context()
        if context is None:
            self._completer.popup().hide()
            return
        prefix, start, end, _typed = context
        replacement = completion if prefix == "K" else _escape_formula_name(completion)
        cursor = self.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self._inserting_completion = True
        try:
            cursor.insertText(replacement)
            position = cursor.position()
            text = self.toPlainText()
            if prefix in {"R", "P"}:
                if position < len(text) and text[position] == "]":
                    cursor.setPosition(position + 1)
                else:
                    cursor.insertText("]")
                tail = self.toPlainText()[cursor.position():].lstrip()
                if not tail.startswith(("+", "-", "*", ")")):
                    cursor.insertText(" + ")
            self.setTextCursor(cursor)
        finally:
            self._inserting_completion = False
        self._completer.popup().hide()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _accept_current_completion(self) -> bool:
        popup = self._completer.popup()
        current = popup.currentIndex()
        if not current.isValid() and self._completer.completionModel().rowCount() > 0:
            current = self._completer.completionModel().index(0, 0)
        if not current.isValid():
            return False
        completion = current.data(self._completer.completionRole())
        self._insert_completion(str(completion))
        return True


class FormulaItemDelegate(SelectionNeutralItemDelegate):
    def __init__(
        self,
        racking_names: tuple[str, ...],
        product_names: tuple[str, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._racking_names = racking_names
        self._product_names = product_names

    def createEditor(
        self,
        parent: QWidget,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> QWidget:
        editor = FormulaTextEdit(self._racking_names, self._product_names, parent)
        editor.editing_finished.connect(lambda: self._finish_editing(editor))
        editor.document().blockCountChanged.connect(
            lambda count: self._resize_editor_row(index.row(), count, editor)
        )
        QTimer.singleShot(
            0,
            lambda: self._resize_editor_row(
                index.row(), editor.document().blockCount(), editor
            ),
        )
        return editor

    def displayText(self, value: object, locale: object) -> str:
        return str(value).replace("\r", "").replace("\n", " ")

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:
        if isinstance(editor, FormulaTextEdit):
            editor.setPlainText(str(index.data(Qt.ItemDataRole.EditRole) or ""))
            return
        super().setEditorData(editor, index)

    def setModelData(
        self, editor: QWidget, model: QAbstractItemModel, index: QModelIndex
    ) -> None:
        if isinstance(editor, FormulaTextEdit):
            model.setData(index, editor.toPlainText(), Qt.ItemDataRole.EditRole)
            return
        super().setModelData(editor, model, index)

    def _finish_editing(self, editor: FormulaTextEdit) -> None:
        table = self.parent()
        row = table.currentRow() if isinstance(table, QTableWidget) else -1
        self.commitData.emit(editor)
        self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)
        if isinstance(table, QTableWidget) and row >= 0:
            QTimer.singleShot(0, lambda: table.resizeRowToContents(row))

    def destroyEditor(self, editor: QWidget, index: QModelIndex) -> None:
        table = self.parent()
        super().destroyEditor(editor, index)
        if isinstance(table, QTableWidget):
            QTimer.singleShot(0, lambda: table.resizeRowToContents(index.row()))

    def _resize_editor_row(
        self, row: int, block_count: int, editor: FormulaTextEdit
    ) -> None:
        table = self.parent()
        if not isinstance(table, QTableWidget):
            return
        line_height = editor.fontMetrics().lineSpacing()
        height = max(72, min(200, block_count * line_height + 18))
        table.setRowHeight(row, height)


class DataFillWorker(QThread):
    result_ready = Signal(object)
    error_occurred = Signal(object)
    progress_changed = Signal(float)

    def __init__(
        self,
        view_model: AppViewModel,
        constraints: DataFillConstraints,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._view_model = view_model
        self._constraints = constraints

    def run(self) -> None:
        try:
            result = self._view_model.solve_data_fill(
                self._constraints, progress_callback=self.progress_changed.emit
            )
        except Exception as exc:
            self.error_occurred.emit(exc)
        else:
            self.result_ready.emit(result)


class DataFillDialog(QDialog):
    """Edit ordered targets, preview the best solution and apply it."""

    _ENABLED = 0
    _NAME = 1
    _FORMULA = 2
    _CURRENT = 3
    _TARGET = 4
    _TARGET_LIMIT = 2_000_000_000
    _BASE_BACKGROUND_ROLE = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(
        self,
        view_model: AppViewModel,
        prepared: DataFillPrepared,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._view_model = view_model
        self._prepared = prepared
        self._targets = copy.deepcopy(list(prepared.targets))
        self._formula_text = {
            item.target_id: format_data_fill_formula(item.terms) for item in self._targets
        }
        self._result: DataFillResult | None = None
        self._worker: DataFillWorker | None = None
        self._building_table = False
        self._initial_widths_applied = False
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle(tr("data_fill.title", "Fill Project Data"))
        self.resize(1040, 720)
        self.setMinimumSize(820, 560)
        root = QVBoxLayout(self)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_targets_tab(), tr("data_fill.targets", "Targets"))
        self._tabs.addTab(self._build_results_tab(), tr("data_fill.results", "Results"))
        root.addWidget(self._tabs, 1)

        self._status = QLabel(tr("data_fill.not_calculated", "Not calculated"))
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        buttons = QHBoxLayout()
        self._cancel_solve = QPushButton(tr("data_fill.cancel_solve", "Cancel Solve"))
        self._cancel_solve.setEnabled(False)
        self._cancel_solve.clicked.connect(self._cancel_running_solve)
        buttons.addWidget(self._cancel_solve)
        buttons.addStretch()
        self._calculate = QPushButton(tr("data_fill.calculate", "Calculate"))
        self._calculate.clicked.connect(self._start_calculation)
        buttons.addWidget(self._calculate)
        self._apply = QPushButton(tr("data_fill.apply", "Apply"))
        self._apply.setEnabled(False)
        self._apply.clicked.connect(self._apply_result)
        buttons.addWidget(self._apply)
        self._close_button = QPushButton(tr("data_fill.close", "Close"))
        self._close_button.clicked.connect(self.reject)
        buttons.addWidget(self._close_button)
        root.addLayout(buttons)
        self._validate_table()

    def _build_targets_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        self._add_target = QPushButton(tr("data_fill.add_target", "Add"))
        self._delete_target = QPushButton(tr("data_fill.delete_target", "Delete"))
        self._move_up = QPushButton(tr("data_fill.move_up", "Move Up"))
        self._move_down = QPushButton(tr("data_fill.move_down", "Move Down"))
        self._add_target.clicked.connect(self._add_custom_target)
        self._delete_target.clicked.connect(self._delete_selected_target)
        self._move_up.clicked.connect(lambda: self._move_selected_target(-1))
        self._move_down.clicked.connect(lambda: self._move_selected_target(1))
        for button in (self._add_target, self._delete_target, self._move_up, self._move_down):
            toolbar.addWidget(button)
        toolbar.addStretch()
        editable_hint = QLabel(tr(
            "data_fill.editable_hint",
            "Black text is editable; gray text is read-only.",
        ))
        editable_hint.setStyleSheet("color: palette(mid);")
        toolbar.addWidget(editable_hint)
        layout.addLayout(toolbar)

        self._target_table = TargetTableWidget(0, 5)
        self._target_table.setObjectName("dataFillTargetTable")
        self._apply_target_selection_style()
        self._target_table.setHorizontalHeaderLabels((
            tr("data_fill.enable", "Enable"),
            tr("data_fill.name", "Name"),
            tr("data_fill.expression", "Expression"),
            tr("data_fill.current", "Current"),
            tr("data_fill.target", "Target"),
        ))
        vertical_header = self._target_table.verticalHeader()
        vertical_header.setVisible(False)
        vertical_header.setMinimumSectionSize(28)
        vertical_header.setDefaultSectionSize(28)
        self._target_table.setWordWrap(False)
        self._target_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        header = self._target_table.horizontalHeader()
        header.setMinimumSectionSize(40)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._target_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._target_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._target_table.setItemDelegate(
            SelectionNeutralItemDelegate(self._target_table)
        )
        self._target_table.setItemDelegateForColumn(
            self._ENABLED,
            EnabledCheckDelegate(self._target_table),
        )
        self._target_table.setItemDelegateForColumn(
            self._FORMULA,
            FormulaItemDelegate(
                self._prepared.racking_names,
                self._prepared.product_names,
                self._target_table,
            ),
        )
        self._target_table.itemChanged.connect(self._on_target_changed)
        self._target_table.itemSelectionChanged.connect(self._update_target_buttons)
        layout.addWidget(self._target_table, 1)
        self._rebuild_target_table()
        return page

    def _build_results_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel(tr("data_fill.target_results", "Target Results")))
        self._constraint_results = QTableWidget(0, 7)
        self._constraint_results.setHorizontalHeaderLabels((
            tr("data_fill.name", "Name"),
            tr("data_fill.expression", "Expression"),
            tr("data_fill.current", "Current"),
            tr("data_fill.target", "Target"),
            tr("data_fill.solved", "Solved"),
            tr("data_fill.abs_deviation", "Abs. Deviation"),
            tr("data_fill.rel_deviation", "Rel. Deviation"),
        ))
        self._configure_readonly_table(self._constraint_results)
        layout.addWidget(self._constraint_results, 1)

        layout.addWidget(QLabel(tr("data_fill.template_result", "Node Template Results")))
        self._template_results = QTableWidget(0, 4)
        self._template_results.setHorizontalHeaderLabels((
            tr("data_fill.samples", "Samples"),
            tr("data_fill.sample_count", "Sample Count"),
            tr("data_fill.sample_ratio", "Sample Ratio"),
            tr("data_fill.added_count", "Added Count"),
        ))
        self._configure_readonly_table(self._template_results)
        layout.addWidget(self._template_results, 1)
        return page

    @staticmethod
    def _configure_readonly_table(table: QTableWidget) -> None:
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

    def _apply_target_selection_style(self) -> None:
        self._target_table.setStyleSheet("""
            QTableWidget#dataFillTargetTable {
                outline: 0;
                background-color: white;
                alternate-background-color: white;
                selection-background-color: white;
                selection-color: black;
            }
            QTableWidget#dataFillTargetTable::item:selected {
                background-color: white;
                border: none;
            }
        """)

    def _set_item_editability(self, item: QTableWidgetItem, editable: bool) -> None:
        background = QColor("white")
        item.setData(self._BASE_BACKGROUND_ROLE, background)
        item.setBackground(background)
        item.setForeground(QColor("black") if editable else QColor("#808080"))

    def _readonly_item(self, value: str) -> QTableWidgetItem:
        item = QTableWidgetItem(value)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._set_item_editability(item, False)
        return item

    def _display_label(self, target: DataFillTarget) -> str:
        ref = target.builtin_ref
        if ref is None:
            return target.label
        if ref.kind == "nodes":
            return tr("data_fill.nodes", "Nodes")
        if ref.kind == "strings":
            return tr("data_fill.strings", "Strings")
        if ref.kind == "racking":
            return tr("data_fill.racking_named", "Racking: {name}").format(name=ref.name)
        return tr("data_fill.product_named", "Product: {name}").format(name=ref.name)

    def _rebuild_target_table(self, selected_row: int | None = None) -> None:
        self._building_table = True
        try:
            self._target_table.setRowCount(len(self._targets))
            for row, target in enumerate(self._targets):
                enabled = QTableWidgetItem()
                enabled.setFlags(
                    (enabled.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    & ~Qt.ItemFlag.ItemIsEditable
                )
                enabled.setCheckState(
                    Qt.CheckState.Checked if target.enabled else Qt.CheckState.Unchecked
                )
                self._set_item_editability(enabled, True)
                self._target_table.setItem(row, self._ENABLED, enabled)

                name = QTableWidgetItem(self._display_label(target))
                formula = QTableWidgetItem(self._formula_text.get(
                    target.target_id, format_data_fill_formula(target.terms)
                ))
                if target.is_builtin:
                    name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    formula.setFlags(formula.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._set_item_editability(name, not target.is_builtin)
                self._set_item_editability(formula, not target.is_builtin)
                self._target_table.setItem(row, self._NAME, name)
                self._target_table.setItem(row, self._FORMULA, formula)
                current = DataFillService.target_value(self._prepared.current, target)
                self._target_table.setItem(row, self._CURRENT, self._readonly_item(str(current)))
                target_item = QTableWidgetItem(str(target.target))
                self._set_item_editability(target_item, True)
                self._target_table.setItem(row, self._TARGET, target_item)
            if selected_row is not None and 0 <= selected_row < len(self._targets):
                self._target_table.selectRow(selected_row)
        finally:
            self._building_table = False
        self._target_table.resizeRowsToContents()
        self._update_target_buttons()
        if hasattr(self, "_calculate"):
            self._validate_table()

    def _add_custom_target(self) -> None:
        number = 1
        existing = {item.label for item in self._targets}
        while True:
            label = tr("data_fill.custom_target", "Target {number}").format(number=number)
            if label not in existing:
                break
            number += 1
        target = DataFillTarget(
            target_id=f"custom:{uuid.uuid4()}",
            label=label,
            terms=[],
            target=0,
            enabled=True,
        )
        self._targets.insert(0, target)
        self._formula_text[target.target_id] = ""
        row = 0
        self._rebuild_target_table(row)
        self._invalidate_result()
        self._target_table.setCurrentCell(row, self._NAME)
        self._target_table.editItem(self._target_table.item(row, self._NAME))

    def _selected_row(self) -> int:
        rows = self._target_table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _delete_selected_target(self) -> None:
        row = self._selected_row()
        if row < 0 or self._targets[row].is_builtin:
            return
        target = self._targets.pop(row)
        self._formula_text.pop(target.target_id, None)
        self._rebuild_target_table(min(row, len(self._targets) - 1))
        self._invalidate_result()

    def _move_selected_target(self, delta: int) -> None:
        row = self._selected_row()
        target_row = row + delta
        if row < 0 or not 0 <= target_row < len(self._targets):
            return
        self._sync_table_values()
        self._targets[row], self._targets[target_row] = self._targets[target_row], self._targets[row]
        self._rebuild_target_table(target_row)
        self._invalidate_result()

    def _update_target_buttons(self) -> None:
        if not hasattr(self, "_delete_target"):
            return
        row = self._selected_row()
        valid = 0 <= row < len(self._targets)
        self._delete_target.setEnabled(valid and not self._targets[row].is_builtin)
        self._move_up.setEnabled(valid and row > 0)
        self._move_down.setEnabled(valid and row < len(self._targets) - 1)

    def _on_target_changed(self, item: QTableWidgetItem) -> None:
        if self._building_table:
            return
        self._sync_table_values()
        self._invalidate_result()
        self._target_table.resizeRowToContents(item.row())

    def _sync_table_values(self) -> None:
        for row, target in enumerate(self._targets):
            enabled = self._target_table.item(row, self._ENABLED)
            target.enabled = enabled.checkState() == Qt.CheckState.Checked
            target.label = self._target_table.item(row, self._NAME).text().strip()
            if not target.is_builtin:
                self._formula_text[target.target_id] = self._target_table.item(row, self._FORMULA).text()
            try:
                target.target = int(self._target_table.item(row, self._TARGET).text().strip())
            except ValueError:
                pass

    def _set_error(self, item: QTableWidgetItem, message: str | None) -> None:
        if message:
            item.setBackground(QColor("#fff0f0"))
            item.setToolTip(message)
        else:
            background = item.data(self._BASE_BACKGROUND_ROLE)
            item.setBackground(background if isinstance(background, QColor) else QBrush())
            if item.column() == self._FORMULA and "\n" in item.text():
                item.setToolTip(item.text())
            else:
                item.setToolTip("")

    def _formula_error_message(self, exc: DataFillFormulaError) -> str:
        messages = {
            "formula_empty": tr("data_fill.error.formula_empty", "Expression cannot be empty."),
            "formula_syntax": tr("data_fill.error.formula_syntax", "Invalid expression syntax."),
            "formula_nonlinear": tr("data_fill.error.formula_nonlinear", "Only integer linear expressions are supported."),
            "formula_invalid_coefficient": tr("data_fill.error.formula_coefficient", "Expression coefficients must be valid integers."),
            "formula_unknown_reference": tr("data_fill.error.formula_reference", "Expression contains an unknown quantity reference."),
        }
        message = messages.get(exc.code, str(exc))
        return f"{message} ({exc.token})" if exc.token else message

    def _validate_table(self) -> bool:
        if self._building_table or not hasattr(self, "_calculate"):
            return False
        signals_were_blocked = self._target_table.blockSignals(True)
        self._sync_table_values()
        valid = True
        labels: dict[str, list[int]] = {}
        rackings = set(self._prepared.racking_names)
        products = set(self._prepared.product_names)
        for row, target in enumerate(self._targets):
            name_item = self._target_table.item(row, self._NAME)
            formula_item = self._target_table.item(row, self._FORMULA)
            target_item = self._target_table.item(row, self._TARGET)
            label = self._display_label(target).strip()
            labels.setdefault(label, []).append(row)
            self._set_error(name_item, None)
            self._set_error(formula_item, None)
            self._set_error(target_item, None)
            if not label:
                self._set_error(name_item, tr("data_fill.error.empty_name", "Target name cannot be empty."))
                valid = False
            if not target.is_builtin:
                try:
                    terms = parse_data_fill_formula(self._formula_text[target.target_id])
                    validate_formula_references(terms, rackings, products)
                    target.terms = terms
                    current = DataFillService.target_value(self._prepared.current, target)
                    self._target_table.item(row, self._CURRENT).setText(str(current))
                except DataFillFormulaError as exc:
                    self._set_error(formula_item, self._formula_error_message(exc))
                    valid = False
            try:
                value = int(target_item.text().strip())
                if not -self._TARGET_LIMIT <= value <= self._TARGET_LIMIT:
                    raise ValueError
                target.target = value
            except ValueError:
                self._set_error(target_item, tr("data_fill.error.target_range", "Target must be a signed 32-bit integer."))
                valid = False
        for label, rows in labels.items():
            if label and len(rows) > 1:
                valid = False
                for row in rows:
                    self._set_error(
                        self._target_table.item(row, self._NAME),
                        tr("data_fill.error.duplicate_name", "Target names must be unique."),
                    )
        enabled = any(item.enabled for item in self._targets)
        solving = self._worker is not None
        self._target_table.blockSignals(signals_were_blocked)
        self._calculate.setEnabled(valid and enabled and not solving)
        return valid and enabled

    def _invalidate_result(self, *_args: object) -> None:
        if self._worker is not None:
            return
        self._result = None
        if hasattr(self, "_apply"):
            self._apply.setEnabled(False)
            self._status.setText(tr("data_fill.not_calculated", "Not calculated"))
        self._validate_table()

    def _collect_constraints(self) -> DataFillConstraints:
        if not self._validate_table():
            raise DataFillError("invalid_targets")
        return DataFillConstraints(tuple(copy.deepcopy(self._targets)))

    def _start_calculation(self) -> None:
        try:
            constraints = self._collect_constraints()
            constraints.validate()
        except DataFillError as exc:
            self._show_data_fill_error(exc)
            return
        self._result = None
        self._apply.setEnabled(False)
        self._set_solving(True)
        self._status.setText(tr("data_fill.solving", "Calculating the best result..."))
        worker = DataFillWorker(self._view_model, constraints, self)
        self._worker = worker
        worker.result_ready.connect(self._on_result)
        worker.error_occurred.connect(self._on_error)
        worker.progress_changed.connect(self._on_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.start()

    def _on_progress(self, similarity: float) -> None:
        self._status.setText(tr(
            "data_fill.solving_similarity",
            "Calculating... Current similarity: {similarity:.1f}%",
        ).format(similarity=similarity))

    def _cancel_running_solve(self) -> None:
        if self._worker is None:
            return
        self._status.setText(tr("data_fill.cancelling", "Cancelling..."))
        self._view_model.cancel_data_fill()

    def _on_result(self, result: DataFillResult) -> None:
        self._result = result
        self._render_result(result)
        self._apply.setEnabled(True)
        self._tabs.setCurrentIndex(1)

    def _on_error(self, exc: Exception) -> None:
        if isinstance(exc, DataFillError) and exc.code == "cancelled":
            self._status.setText(tr("data_fill.cancelled", "Calculation cancelled."))
            return
        if isinstance(exc, DataFillError):
            self._show_data_fill_error(exc)
        else:
            QMessageBox.warning(
                self,
                tr("data_fill.error_title", "Data Fill Failed"),
                str(exc),
            )

    def _on_worker_finished(self) -> None:
        worker = self._worker
        self._worker = None
        self._set_solving(False)
        if worker is not None:
            worker.deleteLater()

    def _set_solving(self, solving: bool) -> None:
        self._target_table.setEnabled(not solving)
        for button in (self._add_target, self._delete_target, self._move_up, self._move_down):
            button.setEnabled(not solving)
        self._cancel_solve.setEnabled(solving)
        self._close_button.setEnabled(not solving)
        if solving:
            self._calculate.setEnabled(False)
        else:
            self._validate_table()
            self._update_target_buttons()

    def _render_result(self, result: DataFillResult) -> None:
        target_by_id = {item.target_id: item for item in result.constraints.targets}
        self._constraint_results.setRowCount(len(result.constraint_results))
        for row, item in enumerate(result.constraint_results):
            target = target_by_id.get(item.target_id)
            label = self._display_label(target) if target is not None else item.label
            values = (
                label,
                item.formula,
                str(item.current),
                str(item.target),
                str(item.actual),
                str(item.absolute_deviation),
                f"{item.relative_deviation:.2%}",
            )
            for column, value in enumerate(values):
                self._constraint_results.setItem(row, column, QTableWidgetItem(value))

        self._template_results.setRowCount(len(result.template_results))
        for row, item in enumerate(result.template_results):
            sample_names = ", ".join(item.sample_names[:3])
            if len(item.sample_names) > 3:
                sample_names += tr("data_fill.more_samples", " … (+{count})").format(
                    count=len(item.sample_names) - 3
                )
            values = (
                sample_names,
                str(item.sample_count),
                f"{item.sample_ratio:.2%}",
                str(item.added_count),
            )
            for column, value in enumerate(values):
                self._template_results.setItem(row, column, QTableWidgetItem(value))

        status = (
            tr("data_fill.exact", "Exact result")
            if result.exact
            else tr("data_fill.closest", "Closest result; one or more targets cannot be exact")
        )
        if result.timed_out:
            status += " " + tr("data_fill.timed_out", "(time limit reached)")
        if result.used_fallback:
            status += " " + tr("data_fill.fallback", "(fallback search used)")
        status += " " + tr("data_fill.added_nodes", "Added Nodes: {count}").format(
            count=result.added_node_count
        )
        self._status.setText(status)

    def _apply_initial_target_widths(self) -> None:
        if self._initial_widths_applied:
            return
        available = max(self._target_table.viewport().width(), 1)
        shares = (0.07, 0.20, 0.43, 0.15, 0.15)
        minimums = (56, 140, 280, 90, 90)
        widths = [max(minimum, int(available * share)) for minimum, share in zip(minimums, shares)]
        overflow = sum(widths) - available
        for column in (self._FORMULA, self._NAME, self._TARGET, self._CURRENT):
            if overflow <= 0:
                break
            reducible = widths[column] - minimums[column]
            reduction = min(reducible, overflow)
            widths[column] -= reduction
            overflow -= reduction
        if overflow <= 0 and sum(widths) < available:
            widths[self._FORMULA] += available - sum(widths)
        for column, width in enumerate(widths):
            self._target_table.setColumnWidth(column, width)
        self._target_table.resizeRowsToContents()
        self._initial_widths_applied = True

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._apply_target_selection_style()
        if not self._initial_widths_applied:
            QTimer.singleShot(0, self._apply_initial_target_widths)

    def _show_data_fill_error(self, exc: DataFillError) -> None:
        messages = {
            "no_constraints": tr("data_fill.error.no_constraints", "Select at least one target."),
            "invalid_targets": tr("data_fill.error.invalid_targets", "Fix invalid target rows before calculating."),
            "target_empty_name": tr("data_fill.error.empty_name", "Target name cannot be empty."),
            "target_duplicate_name": tr("data_fill.error.duplicate_name", "Target names must be unique."),
            "target_out_of_range": tr("data_fill.error.target_range", "Target must be a signed 32-bit integer."),
            "no_real_invs": tr("data_fill.error.no_invs", "No real INV is available."),
            "no_sample_nodes": tr("data_fill.error.no_nodes", "The real INVs contain no Nodes."),
            "solver_no_result": tr("data_fill.error.no_result", "No solver result was available."),
        }
        QMessageBox.warning(
            self,
            tr("data_fill.error_title", "Data Fill Failed"),
            messages.get(exc.code, str(exc)),
        )

    def _apply_result(self) -> None:
        if self._result is None:
            return
        self._view_model.apply_data_fill(self._result)
        self.accept()

    def reject(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._cancel_running_solve()
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._view_model.cancel_data_fill()
            if not self._worker.wait(2000):
                event.ignore()
                return
        super().closeEvent(event)

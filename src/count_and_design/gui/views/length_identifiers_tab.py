"""Length identifiers tab view."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...i18n import tr, TranslationManager
from ..tooltips import TooltipManager
from ..viewmodels.length_identifiers_tab import LengthIdentifiersTabViewModel
from ..viewmodels.states import LengthIdentifiersTabState
from .widgets import LengthEdit
from .ordered_selection import OrderedListWidget
from .voltage_focus import VoltageFocusWidget


class LengthIdentifiersTab(VoltageFocusWidget):
    """View widget for length identifier management."""

    def __init__(self, view_model: LengthIdentifiersTabViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view_model = view_model
        self._tooltips = TooltipManager.instance()
        self._item_lookup: dict[str, QListWidgetItem] = {}
        self._list_label: QLabel | None = None
        # 缓存的编辑器 widget，用于避免不必要的销毁/重建
        self._name_edit: QLineEdit | None = None
        self._length_edit: LengthEdit | None = None
        self._name_label: QLabel | None = None
        self._editor_group: QGroupBox | None = None
        self._delete_button: QPushButton | None = None
        self._current_editor_item_id: str | None = None
        self._rendering: bool = False
        self._setup_ui()
        self._view_model.state_changed.connect(self._render)
        self._view_model.error_occurred.connect(self._show_error)
        TranslationManager.instance().language_changed.connect(
            lambda _: self._view_model.refresh()
        )
        self._render(self._view_model.state)

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self._list_label = QLabel(tr("length_identifiers_tab.list_label", "Length Identifiers:"))
        left_layout.addWidget(self._list_label)
        self._list = OrderedListWidget()
        self._list.currentItemChanged.connect(self._on_current_item_changed)
        self._list.selection_requested.connect(self._on_selection_requested)
        self._list.reorder_requested.connect(self._view_model.move_selected)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        left_layout.addWidget(self._list)
        self._add_button = QPushButton(tr("length_identifiers_tab.add_btn", "Add Identifier"))
        self._add_button.clicked.connect(self._view_model.add_identifier)
        self._tooltips.register(self._add_button, "length_identifiers_tab.tooltip.add_btn")
        left_layout.addWidget(self._add_button)

        self._editor = QWidget()
        self._editor_layout = QVBoxLayout(self._editor)
        self._editor_layout.setContentsMargins(0, 0, 0, 0)

        splitter.addWidget(left)
        splitter.addWidget(self._editor)
        splitter.setSizes([300, 500])
        layout.addWidget(splitter)

    def _render(self, state: LengthIdentifiersTabState) -> None:
        self._rendering = True
        self.begin_voltage_field_render()

        if self._list_label:
            self._list_label.setText(tr("length_identifiers_tab.list_label", "Length Identifiers:"))
        if self._add_button:
            self._add_button.setText(tr("length_identifiers_tab.add_btn", "Add Identifier"))

        # 更新左侧列表（使用 blockSignals 避免触发选中变化）
        self._list.blockSignals(True)
        self._list.clear()
        self._item_lookup = {}
        for item_state in state.items:
            item = QListWidgetItem(item_state.label)
            item.setData(Qt.ItemDataRole.UserRole, item_state.item_id)
            self._list.addItem(item)
            self._item_lookup[item_state.item_id] = item
        self._list.blockSignals(False)
        self._list.apply_selected_ids(state.selected_item_ids)

        # 更新右侧编辑器
        self._render_editor(state)

        self._rendering = False
        self.finish_voltage_field_render()

    def _render_editor(self, state: LengthIdentifiersTabState) -> None:
        """渲染右侧编辑器，尽可能复用已有 widget 以避免焦点问题。

        当用户在一个输入框编辑后点击另一个输入框时，editingFinished 信号
        会触发 update_length → defer_refresh → _render。如果此
        时销毁并重建所有 widget，目标输入框会丢失点击事件。

        解决方案：检查选中项是否变化。若未变化（仅值刷新），直接更新已有
        widget 的文本；若选中项变化，则安全地重建整个编辑器区域。
        """
        if state.selected_item_id is None:
            self._clear_editor()
            if not state.selected_item_ids:
                self._editor_layout.addWidget(QLabel(
                    tr("length_identifiers_tab.no_selection", "No length identifier selected.")))
            self._editor_layout.addStretch()
            return

        # 当选中项未变且编辑器已存在时，只更新值
        if (self._name_edit is not None
                and self._length_edit is not None
                and self._current_editor_item_id == state.selected_item_id):
            self.register_voltage_field("__object__", self._editor_group or self._length_edit)
            self.register_voltage_field("length", self._length_edit)
            # 更新 identifier_options（可能因其他标识符变化而改变）
            self._length_edit.set_identifier_options(list(state.identifier_options))
            # 刷新 i18n 字符串（切换语言时此路径也会被走到）
            self._refresh_i18n()
            # 局部更新文本 —— 仅在字段未被用户编辑时才覆盖，
            # 避免丢弃用户尚未提交的输入（例如改名后立即编辑长度时触发 refresh）。
            if self._name_edit.text() != state.detail.name and not self._name_edit.hasFocus():
                self._name_edit.blockSignals(True)
                self._name_edit.setText(state.detail.name)
                self._name_edit.blockSignals(False)
            if self._length_edit.get_text() != state.detail.length:
                # LengthEdit 内部 QLineEdit 若持有焦点则不覆盖，避免丢弃用户尚未提交的输入
                inner_line = self._length_edit.findChild(QLineEdit)
                if inner_line is None or not inner_line.hasFocus():
                    self._length_edit.set_text(state.detail.length)
            return

        # 选中项变化或首次渲染 → 重建编辑器
        self._clear_editor()
        self._build_editor_widgets(state)
        self._current_editor_item_id = state.selected_item_id

    def _clear_editor(self) -> None:
        """清除编辑器区域并清空缓存引用。"""
        while self._editor_layout.count():
            child = self._editor_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._name_edit = None
        self._length_edit = None
        self._name_label = None
        self._editor_group = None
        self._delete_button = None
        self._current_editor_item_id = None

    def _build_editor_widgets(self, state: LengthIdentifiersTabState) -> None:
        """从零构建编辑器 widget。"""
        group = QGroupBox(tr("length_identifiers_tab.group_title", "Length Identifier"))
        form = QFormLayout(group)

        self._name_edit = QLineEdit(state.detail.name)
        self._name_edit.setPlaceholderText(tr("length_identifiers_tab.name_placeholder",
                                              "e.g., length_A, my_identifier"))
        self._tooltips.register(self._name_edit, "length_identifiers_tab.tooltip.name_field")
        self._name_edit.editingFinished.connect(self._on_name_edited)

        self._name_label = QLabel(tr("length_identifiers_tab.form.name", "Name:"))
        form.addRow(self._name_label, self._name_edit)

        self._length_edit = LengthEdit()
        self.register_voltage_field("__object__", group)
        self.register_voltage_field("length", self._length_edit)
        self._length_edit.set_identifier_options(list(state.identifier_options))
        self._length_edit.set_text(state.detail.length)
        # 捕获当前 widget 引用到闭包
        _length_edit = self._length_edit
        self._length_edit.value_changed.connect(
            lambda: self._view_model.update_length(_length_edit.get_text()))
        self._tooltips.register(self._length_edit, "length_identifiers_tab.tooltip.length_field")
        form.addRow(tr("length_identifiers_tab.form.length", "Length:"), self._length_edit)

        self._editor_group = group
        self._editor_layout.addWidget(group)

        self._delete_button = QPushButton(tr("length_identifiers_tab.delete_btn", "Delete Identifier"))
        self._delete_button.clicked.connect(self._view_model.delete_selected)
        self._tooltips.register(self._delete_button, "length_identifiers_tab.tooltip.delete_btn")
        self._editor_layout.addWidget(self._delete_button)
        self._editor_layout.addStretch()

    def _refresh_i18n(self) -> None:
        """刷新编辑器内所有控件的 i18n 文本（用于原地更新路径，避免切换语言后文字不更新）。"""
        if self._editor_group is not None:
            self._editor_group.setTitle(tr("length_identifiers_tab.group_title", "Length Identifier"))
        if self._name_edit is not None:
            self._name_edit.setPlaceholderText(
                tr("length_identifiers_tab.name_placeholder", "e.g., length_A, my_identifier"))
        if self._name_label is not None:
            self._name_label.setText(tr("length_identifiers_tab.form.name", "Name:"))
        if self._delete_button is not None:
            self._delete_button.setText(tr("length_identifiers_tab.delete_btn", "Delete Identifier"))
        # 更新表单标签
        if self._editor_group is not None:
            form_layout = self._editor_group.layout()
            if form_layout is not None and isinstance(form_layout, QFormLayout):
                if self._length_edit is not None:
                    length_label = form_layout.labelForField(self._length_edit)
                    if length_label is not None:
                        length_label.setText(tr("length_identifiers_tab.form.length", "Length:"))

    def _on_name_edited(self) -> None:
        """Commit a name edit and update all references automatically."""
        if self._name_edit is None:
            return
        new_name = self._name_edit.text()
        status, message = self._view_model.update_name(new_name)
        if status == "invalid":
            QMessageBox.warning(
                self,
                tr("length_identifiers_tab.invalid_name_title", "Invalid Name"),
                message,
            )
            self._view_model.refresh()

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def _on_selection_requested(self, item_ids: tuple[str, ...]) -> None:
        if self._rendering:
            return
        self._view_model.select_items(item_ids)

    def _on_current_item_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if self._rendering or self._list.internal_selection_change:
            return
        item_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        self._view_model.select_item(item_id)

    def _show_context_menu(self, pos) -> None:
        if self._list.itemAt(pos) is None:
            return
        menu = QMenu(self)
        sort_menu = menu.addMenu(tr("ordering.sort", "Sort"))
        enabled = len(self._view_model.state.selected_item_ids) >= 2
        sort_menu.setEnabled(enabled)
        self._add_sort_field(sort_menu, tr("ordering.name", "Name"), "name")
        self._add_sort_field(
            sort_menu,
            tr("ordering.length_mm", "Length (mm)"),
            "length_mm",
        )
        menu.exec(self._list.viewport().mapToGlobal(pos))

    def _add_sort_field(self, menu: QMenu, label: str, field: str) -> None:
        field_menu = menu.addMenu(label)
        ascending = field_menu.addAction(tr("ordering.ascending", "Ascending"))
        descending = field_menu.addAction(tr("ordering.descending", "Descending"))
        ascending.triggered.connect(
            lambda _checked=False, value=field: self._view_model.sort_selected(value, False)
        )
        descending.triggered.connect(
            lambda _checked=False, value=field: self._view_model.sort_selected(value, True)
        )

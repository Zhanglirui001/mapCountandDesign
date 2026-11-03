"""Rackings tab view."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
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
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...i18n import tr, TranslationManager
from ..tooltips import TooltipManager
from ..viewmodels.rackings_tab import RackingsTabViewModel
from ..viewmodels.states import RackingsTabState
from .widgets import ProductPresetsEdit, disable_combo_scroll
from .ordered_selection import OrderedListWidget
from .voltage_focus import VoltageFocusWidget


class RackingsTab(VoltageFocusWidget):
    """View widget for rackings management."""

    def __init__(self, view_model: RackingsTabViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view_model = view_model
        self._tooltips = TooltipManager.instance()
        self._item_lookup: dict[str, QListWidgetItem] = {}
        self._list_label: QLabel | None = None
        self._suppress_gap_extender_save: bool = False
        self._setup_ui()
        self._view_model.state_changed.connect(self._render)
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
        self._list_label = QLabel(tr("rackings_tab.list_label", "Rackings in Project:"))
        left_layout.addWidget(self._list_label)
        self._list = OrderedListWidget()
        self._list.currentItemChanged.connect(self._on_current_item_changed)
        self._list.selection_requested.connect(self._on_selection_requested)
        self._list.reorder_requested.connect(self._view_model.move_selected)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        QShortcut(QKeySequence.StandardKey.Copy, self._list).activated.connect(
            self._view_model.copy_selected
        )
        QShortcut(QKeySequence.StandardKey.Paste, self._list).activated.connect(
            self._view_model.paste_to_selected
        )
        left_layout.addWidget(self._list)
        self._cleanup_btn = QPushButton(tr("rackings_tab.cleanup_btn", "Clean Up Unused"))
        self._cleanup_btn.clicked.connect(self._on_cleanup)
        self._tooltips.register(self._cleanup_btn, "rackings_tab.tooltip.cleanup_btn")
        left_layout.addWidget(self._cleanup_btn)

        self._editor = QWidget()
        self._editor_layout = QVBoxLayout(self._editor)
        self._editor_layout.setContentsMargins(0, 0, 0, 0)

        splitter.addWidget(left)
        splitter.addWidget(self._editor)
        splitter.setSizes([300, 500])
        layout.addWidget(splitter)

    def _render(self, state: RackingsTabState) -> None:
        self.begin_voltage_field_render()
        # Update list label
        if self._list_label:
            self._list_label.setText(tr("rackings_tab.list_label", "Rackings in Project:"))
        self._cleanup_btn.setText(tr("rackings_tab.cleanup_btn", "Clean Up Unused"))

        self._list.blockSignals(True)
        self._list.clear()
        self._item_lookup = {}
        for item_state in state.items:
            item = QListWidgetItem(item_state.label)
            if item_state.muted:
                item.setForeground(Qt.GlobalColor.gray)
            item.setData(Qt.ItemDataRole.UserRole, item_state.item_id)
            self._list.addItem(item)
            self._item_lookup[item_state.item_id] = item
        self._list.blockSignals(False)
        self._list.apply_selected_ids(state.selected_item_ids)

        while self._editor_layout.count():
            child = self._editor_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        if state.selected_item_id is None:
            self.finish_voltage_field_render()
            return

        name_group = QGroupBox(tr("rackings_tab.name_group", "Rackings Name"))
        name_layout = QVBoxLayout(name_group)
        form = QFormLayout()
        self._name_edit = QLineEdit(state.detail.name)
        self._tooltips.register(self._name_edit, "rackings_tab.tooltip.name_field")
        form.addRow(tr("rackings_tab.form.name", "Name:"), self._name_edit)
        name_layout.addLayout(form)
        rename_btn = QPushButton(tr("rackings_tab.rename_all_btn", "Rename All"))
        rename_btn.clicked.connect(self._on_rename)
        self._tooltips.register(rename_btn, "rackings_tab.tooltip.rename_all_btn")
        name_layout.addWidget(rename_btn)
        self._editor_layout.addWidget(name_group)

        product_presets_group = QGroupBox(tr("rackings_tab.product_presets_group", "ProductPresets"))
        self.register_voltage_field("__object__", product_presets_group)
        product_presets_layout = QVBoxLayout(product_presets_group)
        product_presets_layout.addWidget(QLabel(tr("rackings_tab.product_presets_hint",
                                            "Enter uppercase letters for product_presets:")))
        self._product_presets_edit = ProductPresetsEdit()
        self._product_presets_edit.set_value(state.detail.product_presets)
        _ppe = self._product_presets_edit
        self._product_presets_edit.value_changed.connect(
            lambda: self._view_model.update_product_presets(_ppe.get_value()))
        self._tooltips.register(self._product_presets_edit, "rackings_tab.tooltip.product_presets_edit")
        self.register_voltage_field("product_presets", self._product_presets_edit)
        product_presets_layout.addWidget(self._product_presets_edit)
        self._editor_layout.addWidget(product_presets_group)

        # Gap Extenders group
        gap_extenders_group = QGroupBox(tr("rackings_tab.extenders_group", "Gap Extenders"))
        gap_extenders_layout = QVBoxLayout(gap_extenders_group)

        # Container for gap extender rows (already-added gap extenders for this racking)
        self._gap_extender_rows_widget = QWidget()
        self._gap_extender_rows_layout = QVBoxLayout(self._gap_extender_rows_widget)
        self._gap_extender_rows_layout.setContentsMargins(0, 0, 0, 0)
        gap_extenders_layout.addWidget(self._gap_extender_rows_widget)

        # Populate gap extender rows from state (suppress auto-save during render)
        self._suppress_gap_extender_save = True
        try:
            self._populate_gap_extender_rows(state.detail.gap_extenders)
        finally:
            self._suppress_gap_extender_save = False

        # Selector row below the list: [Dropdown of gap extenders] [+]
        selector_row = QWidget()
        selector_layout = QHBoxLayout(selector_row)
        selector_layout.setContentsMargins(0, 0, 0, 0)

        self._gap_extender_combo = disable_combo_scroll(QComboBox())
        self._gap_extender_combo.setEditable(False)
        self._gap_extender_combo.setMinimumWidth(80)
        existing_sorted = sorted(state.detail.existing_gap_extender_names)
        self._gap_extender_combo.blockSignals(True)
        for name in existing_sorted:
            self._gap_extender_combo.addItem(name)
        self._gap_extender_combo.setCurrentIndex(-1)  # no selection initially
        self._gap_extender_combo.blockSignals(False)
        self._gap_extender_combo.currentTextChanged.connect(self._on_gap_extender_combo_changed)
        self._tooltips.register(self._gap_extender_combo, "rackings_tab.tooltip.extender_selector")
        selector_layout.addWidget(self._gap_extender_combo, 1)

        create_btn = QPushButton("+")
        create_btn.setFixedWidth(30)
        create_btn.setToolTip(tr("rackings_tab.create_extender_tooltip", "Create New Gap Extender"))
        create_btn.clicked.connect(self._on_create_new_gap_extender)
        self._tooltips.register(create_btn, "rackings_tab.tooltip.create_extender_btn")
        selector_layout.addWidget(create_btn)

        gap_extenders_layout.addWidget(selector_row)

        self._editor_layout.addWidget(gap_extenders_group)

        info_group = QGroupBox(tr("rackings_tab.info_group", "Information"))
        info_layout = QVBoxLayout(info_group)
        info_layout.addWidget(QLabel(
            tr("rackings_tab.usage_count", "Used in {count} row position(s)")
            .format(count=state.detail.usage_count)))
        self._editor_layout.addWidget(info_group)
        self._editor_layout.addStretch()
        self.finish_voltage_field_render()

    def _populate_gap_extender_rows(
        self,
        gap_extenders: tuple[tuple[str, str], ...],
    ) -> None:
        """Rebuild gap extender row widgets from state data.

        Each row shows: [name label] [qty spin] [delete button].
        The name is read-only — it was chosen via the selector dropdown above.

        Args:
            gap_extenders: Gap Extender data from state, used to restore name and quantity.
        """
        # Clear existing rows
        while self._gap_extender_rows_layout.count():
            child = self._gap_extender_rows_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        for name, qty_str in gap_extenders:
            self._add_gap_extender_row(name, qty_str)

    def _add_gap_extender_row(
        self,
        name: str,
        qty: str = "0",
    ) -> None:
        """Add a single gap extender row with a read-only name label, quantity spin, and delete button.

        Args:
            name: Gap Extender name (e.g., "GA").
            qty: Quantity as a string.
        """
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)

        name_label = QLabel(name)
        name_label.setMinimumWidth(40)

        qty_spin = QSpinBox()
        qty_spin.setRange(0, 9999)
        qty_spin.setValue(int(qty) if qty.isdigit() else 0)

        delete_btn = QPushButton(tr("rackings_tab.delete_extender", "X"))
        delete_btn.setFixedWidth(30)
        delete_btn.clicked.connect(lambda: self._on_remove_gap_extender_row(row_widget))

        row_layout.addWidget(QLabel(tr("rackings_tab.extender_name_label", "Name:")))
        row_layout.addWidget(name_label, 1)
        row_layout.addWidget(QLabel(tr("rackings_tab.extender_qty_label", "Qty:")))
        row_layout.addWidget(qty_spin)
        row_layout.addWidget(delete_btn)

        # Connect signals to auto-save
        qty_spin.valueChanged.connect(lambda _: self._on_gap_extender_changed())
        self._tooltips.register(qty_spin, "rackings_tab.tooltip.extender_qty")
        self._tooltips.register(delete_btn, "rackings_tab.tooltip.delete_extender_btn")

        self._gap_extender_rows_layout.addWidget(row_widget)

    def _on_create_new_gap_extender(self) -> None:
        """Create a new gap extender type and add it to the current racking.

        The view model generates the next available name, creates the ProductPreset,
        adds it to the racking, and calls refresh() which rebuilds the UI.
        """
        self._view_model.create_and_add_gap_extender_to_racking()

    def _on_gap_extender_combo_changed(self, text: str) -> None:
        """When user picks a gap extender from the dropdown, add it to the racking.

        Does nothing during _render (suppressed) or if text is empty.
        After adding, resets the combo to no-selection so the same item
        can be picked again.
        """
        if self._suppress_gap_extender_save or not text:
            return
        # Check if this gap extender is already in the racking
        for i in range(self._gap_extender_rows_layout.count()):
            item = self._gap_extender_rows_layout.itemAt(i)
            if item and item.widget():
                labels = item.widget().findChildren(QLabel)
                if len(labels) >= 2 and labels[1].text() == text:
                    # Already exists – reset combo and return
                    self._gap_extender_combo.blockSignals(True)
                    self._gap_extender_combo.setCurrentIndex(-1)
                    self._gap_extender_combo.blockSignals(False)
                    return

        self._add_gap_extender_row(text, "0")
        self._save_gap_extenders()
        # Reset combo to no-selection so the same name can be picked again
        self._gap_extender_combo.blockSignals(True)
        self._gap_extender_combo.setCurrentIndex(-1)
        self._gap_extender_combo.blockSignals(False)

    def _on_remove_gap_extender_row(self, row_widget: QWidget) -> None:
        """Remove a specific gap extender row."""
        self._gap_extender_rows_layout.removeWidget(row_widget)
        row_widget.deleteLater()
        self._on_gap_extender_changed()

    def _collect_gap_extenders(self) -> list[tuple[str, int]]:
        """Collect all gap extender rows into (name, qty) list."""
        gap_extenders: list[tuple[str, int]] = []
        for i in range(self._gap_extender_rows_layout.count()):
            item = self._gap_extender_rows_layout.itemAt(i)
            if item and item.widget():
                row = item.widget()
                labels = row.findChildren(QLabel)
                spins = row.findChildren(QSpinBox)
                # labels[0] is "Name:" label, labels[1] is the name value
                name = labels[1].text() if len(labels) >= 2 else ""
                qty = spins[0].value() if spins else 0
                gap_extenders.append((name, qty))
        return gap_extenders

    def _save_gap_extenders(self) -> None:
        """Push current gap extender state to the view model."""
        gap_extenders = self._collect_gap_extenders()
        self._view_model.update_gap_extenders(gap_extenders)

    def _on_gap_extender_changed(self) -> None:
        """Collect all gap extender rows and push to viewmodel."""
        if self._suppress_gap_extender_save:
            return
        self._save_gap_extenders()

    def _on_selection_requested(self, item_ids: tuple[str, ...]) -> None:
        self._view_model.select_items(item_ids)

    def _on_current_item_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if self._list.internal_selection_change:
            return
        item_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        self._view_model.select_item(item_id)

    def _show_context_menu(self, pos) -> None:
        if self._list.itemAt(pos) is None:
            return
        menu = QMenu(self)
        sort_menu = menu.addMenu(tr("ordering.sort", "Sort"))
        sort_menu.setEnabled(len(self._view_model.state.selected_item_ids) >= 2)
        self._add_sort_field(sort_menu, tr("ordering.name", "Name"), "name")
        self._add_sort_field(
            sort_menu,
            tr("ordering.branch_count_sum", "Branch Count Sum"),
            "branch_count",
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

    def _on_rename(self) -> None:
        new_name = self._name_edit.text()
        status, message = self._view_model.rename_rackings(new_name)
        if status == "needs_confirmation":
            reply = QMessageBox.question(
                self,
                tr("rackings_tab.duplicate_name", "Duplicate Name"),
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._view_model.rename_rackings(new_name, merge_if_exists=True)
        elif status == "invalid":
            QMessageBox.warning(
                self,
                tr("rackings_tab.invalid_name", "Invalid Name"),
                message,
            )

    def _on_cleanup(self) -> None:
        count = self._view_model.count_unused()
        if count == 0:
            QMessageBox.information(
                self,
                tr("rackings_tab.cleanup_title", "Cleanup"),
                tr("rackings_tab.no_unused", "No unused rackings to remove"),
            )
            return
        reply = QMessageBox.question(
            self,
            tr("rackings_tab.confirm_cleanup_title", "Confirm Cleanup"),
            tr("rackings_tab.confirm_cleanup", "Remove {count} unused rackings?").format(count=count),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._view_model.cleanup_unused()

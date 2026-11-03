"""Editor for named photovoltaic-panel electrical properties."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox,
    QSplitter, QVBoxLayout, QWidget,
)

from ...i18n import TranslationManager, tr
from ..viewmodels.pv_panels_tab import PVPanelsTabViewModel
from ..viewmodels.states import PVPanelsTabState
from .voltage_focus import VoltageFocusWidget


class PVPanelsTab(VoltageFocusWidget):
    def __init__(self, view_model: PVPanelsTabViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view_model = view_model
        self._rendering = False
        layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self._list_label = QLabel()
        left_layout.addWidget(self._list_label)
        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_selected)
        QShortcut(QKeySequence.StandardKey.Copy, self._list).activated.connect(
            self._view_model.copy_selected
        )
        QShortcut(QKeySequence.StandardKey.Paste, self._list).activated.connect(
            self._view_model.paste_to_selected
        )
        left_layout.addWidget(self._list)
        self._cleanup = QPushButton()
        self._cleanup.clicked.connect(self._on_cleanup)
        left_layout.addWidget(self._cleanup)
        self._editor = QWidget()
        self._editor_layout = QVBoxLayout(self._editor)
        splitter.addWidget(left)
        splitter.addWidget(self._editor)
        splitter.setSizes([300, 600])
        layout.addWidget(splitter)
        view_model.state_changed.connect(self._render)
        view_model.error_occurred.connect(lambda title, msg: QMessageBox.warning(self, title, msg))
        TranslationManager.instance().language_changed.connect(lambda _: view_model.refresh())
        self._render(view_model.state)

    def _render(self, state: PVPanelsTabState) -> None:
        self._rendering = True
        self.begin_voltage_field_render()
        self._list_label.setText(tr("pv_panels_tab.list_label", "PV Panels:"))
        self._cleanup.setText(tr("pv_panels_tab.cleanup_btn", "Clean Up Unused"))
        self._list.blockSignals(True)
        self._list.clear()
        selected_item = None
        for item_state in state.items:
            item = QListWidgetItem(item_state.label)
            if item_state.muted:
                item.setForeground(Qt.GlobalColor.gray)
            item.setData(Qt.ItemDataRole.UserRole, item_state.item_id)
            self._list.addItem(item)
            if item_state.item_id == state.selected_item_id:
                selected_item = item
        self._list.setCurrentItem(selected_item)
        self._list.blockSignals(False)
        while self._editor_layout.count():
            child = self._editor_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        if not state.selected_item_id:
            self._editor_layout.addWidget(QLabel(tr("pv_panels_tab.no_selection", "No PV panel selected.")))
            self._editor_layout.addStretch()
            self._rendering = False
            self.finish_voltage_field_render()
            return
        group = QGroupBox(tr("pv_panels_tab.properties", "PV Panel Properties"))
        self.register_voltage_field("__object__", group)
        form = QFormLayout(group)
        name = QLineEdit(state.detail.name)
        name.editingFinished.connect(lambda: self._view_model.rename_panel(name.text()))
        form.addRow(tr("pv_panels_tab.name", "Name / Brand:"), name)
        vmp = self._double_spin(state.detail.vmp)
        self.register_voltage_field("vmp", vmp)
        vmp.editingFinished.connect(lambda: self._view_model.update_vmp(vmp.value()))
        form.addRow(tr("voltage_drop.vmp", "Module Vmp (V):"), vmp)
        imp = self._double_spin(state.detail.imp)
        self.register_voltage_field("imp", imp)
        imp.editingFinished.connect(lambda: self._view_model.update_imp(imp.value()))
        form.addRow(tr("voltage_drop.imp", "Module Imp (A):"), imp)
        count = QSpinBox()
        count.setRange(0, 10000)
        count.setValue(state.detail.module_per_string)
        self.register_voltage_field("module_per_string", count)
        count.editingFinished.connect(lambda: self._view_model.update_module_per_string(count.value()))
        form.addRow(tr("voltage_drop.modules_per_string", "Modules per String:"), count)
        system = QLabel(f"{state.detail.system_voltage:.3f}")
        system.setObjectName("systemVoltageValue")
        system.setStyleSheet("color: gray;")
        system.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        form.addRow(tr("voltage_drop.system_voltage", "System Voltage (V):"), system)
        self._editor_layout.addWidget(group)
        self._editor_layout.addStretch()
        self._rendering = False
        self.finish_voltage_field_render()

    @staticmethod
    def _double_spin(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 100000.0)
        spin.setDecimals(4)
        spin.setValue(value)
        return spin

    def _on_selected(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        if not self._rendering:
            self._view_model.select_item(current.data(Qt.ItemDataRole.UserRole) if current else None)

    def _on_cleanup(self) -> None:
        count = self._view_model.count_unused()
        if count == 0:
            QMessageBox.information(
                self,
                tr("pv_panels_tab.cleanup_title", "Cleanup"),
                tr("pv_panels_tab.no_unused", "No unused PV panels to remove"),
            )
            return
        reply = QMessageBox.question(
            self,
            tr("pv_panels_tab.confirm_cleanup_title", "Confirm Cleanup"),
            tr(
                "pv_panels_tab.confirm_cleanup",
                "Remove {count} unused PV panels?",
            ).format(count=count),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._view_model.cleanup_unused()

"""ProductPresets tab view."""


from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
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
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...i18n import tr, TranslationManager
from ..tooltips import TooltipManager
from ...models.connector import VALID_CONNECTOR_TYPES
from ...models.fuse import sorted_fuse_currents
from ..viewmodels.product_presets_tab import ProductPresetsTabViewModel
from ..viewmodels.states import ProductPresetDetailState, ProductPresetsTabState
from .widgets import WireEdit, disable_combo_scroll
from .ordered_selection import OrderedListWidget
from .voltage_focus import VoltageFocusWidget


class ProductPresetsTab(VoltageFocusWidget):
    """View widget for product presets."""

    def __init__(self, view_model: ProductPresetsTabViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view_model = view_model
        self._tooltips = TooltipManager.instance()
        self._item_lookup: dict[str, QListWidgetItem] = {}
        self._rendering = False
        self._list_label: QLabel | None = None
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

        # -- Left: list --
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self._list_label = QLabel(tr("product_presets_tab.list_label", "Product Presets in Project:"))
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
        self._cleanup_btn = QPushButton(tr("product_presets_tab.cleanup_btn", "Clean Up Unused"))
        self._cleanup_btn.clicked.connect(self._on_cleanup)
        self._tooltips.register(self._cleanup_btn, "product_presets_tab.tooltip.cleanup_btn")
        left_layout.addWidget(self._cleanup_btn)

        # -- Right: detail in scroll area --
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._detail_widget = QWidget()
        self._detail_layout = QVBoxLayout(self._detail_widget)
        self._scroll.setWidget(self._detail_widget)

        splitter.addWidget(left)
        splitter.addWidget(self._scroll)
        splitter.setSizes([300, 500])
        layout.addWidget(splitter)

    # ----------------------------------------------------------------
    #  Render
    # ----------------------------------------------------------------

    def _render(self, state: ProductPresetsTabState) -> None:
        self._rendering = True
        self.begin_voltage_field_render()

        # Update list label
        if self._list_label:
            self._list_label.setText(tr("product_presets_tab.list_label", "Product Presets in Project:"))
        self._cleanup_btn.setText(tr("product_presets_tab.cleanup_btn", "Clean Up Unused"))

        # Left list
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

        # Right detail
        if len(state.selected_item_ids) > 1:
            while self._detail_layout.count():
                child = self._detail_layout.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()
            self._detail_layout.addStretch()
        else:
            self._render_detail(state.detail_title, state.detail)

        self._rendering = False
        self.finish_voltage_field_render()

    def _render_detail(self, title: str, detail: ProductPresetDetailState) -> None:
        # Clear previous widgets
        while self._detail_layout.count():
            child = self._detail_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        if not detail.name:
            self._detail_layout.addWidget(QLabel(tr("product_presets_tab.no_selection", "No product preset selected")))
            self._detail_layout.addStretch()
            return

        is_ibex = detail.product_type in ("IBEX", "Homerun_Extender")
        is_placeholder = detail.product_type == "Placeholder"
        is_lynx = detail.product_type == "LYNX"
        is_gap_extender = detail.product_type == "Gap_Extender"

        # -- Core group --
        core_group = QGroupBox(title)  # title is already localized by the viewmodel
        self.register_voltage_field("__object__", core_group)
        core_form = QFormLayout(core_group)

        # 使用 QLabel 显示只读信息，但设定最小高度与 QLineEdit 一致
        # 保证行高均匀，又不会误导用户尝试编辑
        _input_height = QLineEdit().sizeHint().height()

        name_label = QLabel(detail.name)
        name_label.setMinimumHeight(_input_height)
        name_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self._tooltips.register(name_label, "product_presets_tab.tooltip.name")
        core_form.addRow(tr("product_presets_tab.form.name", "Name:"), name_label)

        type_display = QLineEdit(detail.product_type)
        type_display.setObjectName("productTypeDisplay")
        type_display.setReadOnly(True)
        type_display.setMinimumHeight(_input_height)
        self.register_voltage_field("product_type", type_display)
        self._tooltips.register(
            type_display, "product_presets_tab.tooltip.product_type"
        )
        core_form.addRow(
            tr("product_presets_tab.form.product_type", "Product Type:"),
            type_display,
        )

        remarks_edit = QLineEdit(detail.remarks)
        remarks_edit.editingFinished.connect(
            lambda e=remarks_edit: self._view_model.update_remarks(e.text())
        )
        self._tooltips.register(remarks_edit, "product_presets_tab.tooltip.remarks")
        core_form.addRow(tr("product_presets_tab.form.remarks", "Remarks:"), remarks_edit)

        pv_panel_edit = disable_combo_scroll(QComboBox())
        pv_panel_edit.setObjectName("pvPanelCombo")
        pv_panel_edit.setEditable(True)
        pv_panel_edit.addItems(detail.pv_panel_options)
        pv_panel_edit.setCurrentText(detail.pv_panel)
        pv_panel_edit.lineEdit().editingFinished.connect(
            lambda e=pv_panel_edit: self._commit_pv_panel(e)
        )
        pv_panel_edit.activated.connect(
            lambda _index, e=pv_panel_edit: self._commit_pv_panel(e)
        )
        pv_panel_edit.setEnabled(not is_gap_extender)
        self.register_voltage_field("pv_panel", pv_panel_edit)
        self._tooltips.register(pv_panel_edit, "product_presets_tab.tooltip.pv_panel")
        core_form.addRow(tr("product_presets_tab.form.pv_panel", "PV Panel:"), pv_panel_edit)

        self._detail_layout.addWidget(core_group)

        # Placeholder only carries its branch count and cumulative length.
        if is_placeholder:
            placeholder_group = QGroupBox(
                tr("product_presets_tab.placeholder_group", "Placeholder Configuration")
            )
            placeholder_form = QFormLayout(placeholder_group)
            branch_edit = QLineEdit(detail.branch_count)
            self.register_voltage_field("branch_count", branch_edit)
            branch_edit.editingFinished.connect(
                lambda e=branch_edit: self._view_model.update_branch_count(e.text())
            )
            placeholder_form.addRow(
                tr("product_presets_tab.form.branch_count", "Branch Count:"), branch_edit
            )

            ptl_edit = QLineEdit(detail.product_total_length)
            self.register_voltage_field("product_total_length", ptl_edit)
            ptl_edit.editingFinished.connect(
                lambda e=ptl_edit: self._view_model.update_product_total_length(e.text())
            )
            ptl_row = QWidget()
            ptl_row_layout = QHBoxLayout(ptl_row)
            ptl_row_layout.setContentsMargins(0, 0, 0, 0)
            ptl_row_layout.addWidget(ptl_edit)
            ptl_label = QLabel(detail.length_displays.get("product_total_length", ""))
            ptl_label.setStyleSheet("color: gray; font-size: 10px;")
            ptl_row_layout.addWidget(ptl_label)
            placeholder_form.addRow(
                tr("product_presets_tab.form.product_total_length", "Product Total Length:"),
                ptl_row,
            )
            self._detail_layout.addWidget(placeholder_group)

        # -- IBEX group --
        if is_ibex:
            ibex_group = QGroupBox(tr("product_presets_tab.ibex_group", "IBEX Configuration"))
            ibex_form = QFormLayout(ibex_group)

            branch_edit = QLineEdit(detail.branch_count)
            self.register_voltage_field("branch_count", branch_edit)
            branch_edit.editingFinished.connect(
                lambda e=branch_edit: self._view_model.update_branch_count(e.text())
            )
            self._tooltips.register(branch_edit, "product_presets_tab.tooltip.branch_count")
            ibex_form.addRow(tr("product_presets_tab.form.branch_count", "Branch Count:"), branch_edit)

            # Product Total Length
            ptl_display = detail.length_displays.get("product_total_length", "")
            ptl_row = QWidget()
            ptl_row_layout = QHBoxLayout(ptl_row)
            ptl_row_layout.setContentsMargins(0, 0, 0, 0)
            ptl_edit = QLineEdit(detail.product_total_length)
            self.register_voltage_field("product_total_length", ptl_edit)
            ptl_edit.editingFinished.connect(
                lambda e=ptl_edit: self._view_model.update_product_total_length(e.text())
            )
            self._tooltips.register(ptl_edit, "product_presets_tab.tooltip.product_total_length")
            ptl_row_layout.addWidget(ptl_edit)
            ptl_label = QLabel(ptl_display)
            ptl_label.setStyleSheet("color: gray; font-size: 10px;")
            ptl_row_layout.addWidget(ptl_label)
            ibex_form.addRow(tr("product_presets_tab.form.product_total_length", "Product Total Length:"), ptl_row)

            # Branch Connector - type, manufacturer
            ibex_form.addRow(
                tr("product_presets_tab.form.branch_conn_wire", "Branch Connector:"),
                self._make_connector_row(
                    detail.branch_connector_type, self._view_model.update_branch_connector_type,
                    detail.branch_connector_manufacturer, self._view_model.update_branch_connector_manufacturer,
                )
            )

            # Branch Fuse - current and manufacturer on same row
            ibex_form.addRow(
                tr("product_presets_tab.form.branch_fuse_current", "Branch Fuse:"),
                self._make_fuse_row(
                    detail.branch_fuse_current, self._view_model.update_branch_fuse_current,
                    detail.branch_fuse_manufacturer, self._view_model.update_branch_fuse_manufacturer,
                )
            )

            # Wires
            ibex_form.addRow(tr("product_presets_tab.form.branch_cu_wire", "Branch Cu Wire:"), self._make_wire_edit(
                detail.branch_cu_wire, detail.branch_cu_strands,
                self._view_model.update_branch_cu_wire, self._view_model.update_branch_cu_strands,
                material="Cu"))
            ibex_form.addRow(tr("product_presets_tab.form.branch_al_wire", "Branch Al Wire:"), self._make_wire_edit(
                detail.branch_al_wire, detail.branch_al_strands,
                self._view_model.update_branch_al_wire, self._view_model.update_branch_al_strands,
                material="Al"))

            # Last Branch Fuse - current and manufacturer on same row
            ibex_form.addRow(
                tr("product_presets_tab.form.last_branch_fuse_current", "Last Branch Fuse:"),
                self._make_fuse_row(
                    detail.last_branch_fuse_current, self._view_model.update_last_branch_fuse_current,
                    detail.last_branch_fuse_manufacturer, self._view_model.update_last_branch_fuse_manufacturer,
                )
            )

            # Last Branch Wires
            ibex_form.addRow(tr("product_presets_tab.form.last_branch_cu_wire", "Last Branch Cu Wire:"), self._make_wire_edit(
                detail.last_branch_cu_wire, detail.last_branch_cu_strands,
                self._view_model.update_last_branch_cu_wire, self._view_model.update_last_branch_cu_strands,
                material="Cu"))
            ibex_form.addRow(tr("product_presets_tab.form.last_branch_al_wire", "Last Branch Al Wire:"), self._make_wire_edit(
                detail.last_branch_al_wire, detail.last_branch_al_strands,
                self._view_model.update_last_branch_al_wire, self._view_model.update_last_branch_al_strands,
                material="Al"))

            ibex_form.addRow(tr("product_presets_tab.form.external_cu_wire", "External Cu Wire:"), self._make_wire_edit(
                detail.external_cu_wire, detail.external_cu_strands,
                self._view_model.update_external_cu_wire, self._view_model.update_external_cu_strands,
                material="Cu"))
            ibex_form.addRow(tr("product_presets_tab.form.external_al_wire", "External Al Wire:"), self._make_wire_edit(
                detail.external_al_wire, detail.external_al_strands,
                self._view_model.update_external_al_wire, self._view_model.update_external_al_strands,
                material="Al"))

            # External Fuse - current and manufacturer on same row
            ibex_form.addRow(
                tr("product_presets_tab.form.external_fuse_current", "External Fuse:"),
                self._make_fuse_row(
                    detail.external_fuse_current, self._view_model.update_external_fuse_current,
                    detail.external_fuse_manufacturer, self._view_model.update_external_fuse_manufacturer,
                )
            )

            # External Connector - type, manufacturer
            ibex_form.addRow(
                tr("product_presets_tab.form.external_conn_wire", "External Connector:"),
                self._make_connector_row(
                    detail.external_connector_type, self._view_model.update_external_connector_type,
                    detail.external_connector_manufacturer, self._view_model.update_external_connector_manufacturer,
                )
            )

            # Internal Wires
            ibex_form.addRow(tr("product_presets_tab.form.internal_cu_wire", "Internal Cu Wire:"), self._make_wire_edit(
                detail.internal_cu_wire, detail.internal_cu_strands,
                self._view_model.update_internal_cu_wire, self._view_model.update_internal_cu_strands,
                material="Cu"))
            ibex_form.addRow(tr("product_presets_tab.form.internal_al_wire", "Internal Al Wire:"), self._make_wire_edit(
                detail.internal_al_wire, detail.internal_al_strands,
                self._view_model.update_internal_al_wire, self._view_model.update_internal_al_strands,
                material="Al"))

            self._detail_layout.addWidget(ibex_group)

            # IBEX default params
            params_group = QGroupBox(tr("product_presets_tab.ibex_defaults_group", "IBEX Default Lengths"))
            params_form = QFormLayout(params_group)

            def _len_row(label: str, value: str, handler, display_key: str = "") -> None:
                edit = QLineEdit(value)
                if display_key:
                    self.register_voltage_field(display_key, edit)
                edit.editingFinished.connect(lambda e=edit, h=handler: h(e.text()))
                display_text = detail.length_displays.get(display_key, "")
                display_label = QLabel(display_text)
                display_label.setStyleSheet("color: gray; font-size: 10px;")
                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.addWidget(edit)
                row_layout.addWidget(display_label)
                params_form.addRow(label, row_widget)

            _len_row(tr("product_presets_tab.form.branch_length_positive", "Branch Length (+):"), detail.branch_length_positive,
                     self._view_model.update_branch_length_positive, display_key="branch_length_positive")
            _len_row(tr("product_presets_tab.form.branch_length_negative", "Branch Length (-):"), detail.branch_length_negative,
                     self._view_model.update_branch_length_negative, display_key="branch_length_negative")
            _len_row(tr("product_presets_tab.form.branch_fuse_prefix", "Branch Fuse Prefix:"), detail.branch_fuse_prefix,
                     self._view_model.update_branch_fuse_prefix, display_key="branch_fuse_prefix")
            _len_row(tr("product_presets_tab.form.first_cual_prefix", "Terminal Branch Cu/Al to Previous T-mold:"), detail.branch_first_cual_prefix,
                     self._view_model.update_branch_first_cual_prefix, display_key="branch_first_cual_prefix")
            _len_row(tr("product_presets_tab.form.second_cual_suffix", "Terminal Branch Cu/Al to Connector:"), detail.branch_second_cual_suffix,
                     self._view_model.update_branch_second_cual_suffix, display_key="branch_second_cual_suffix")
            _len_row(tr("product_presets_tab.form.ext_fuse_suffix", "Ext Fuse Suffix:"), detail.external_fuse_suffix,
                     self._view_model.update_external_fuse_suffix, display_key="external_fuse_suffix")
            _len_row(tr("product_presets_tab.form.ext_cu_to_row_head", "Ext Cu to Row Head:"),
                     detail.external_cu_length_to_row_head, self._view_model.update_external_cu_length_to_row_head,
                     display_key="external_cu_length_to_row_head")
            _len_row(tr("product_presets_tab.form.ext_second_cual_suffix", "Ext 2nd Cu/Al Suffix:"),
                     detail.external_second_cual_suffix, self._view_model.update_external_second_cual_suffix,
                     display_key="external_second_cual_suffix")
            _len_row(tr("product_presets_tab.form.ext_trench_length", "Ext Trench Length:"),
                     detail.external_extension_trench_length, self._view_model.update_external_extension_trench_length,
                     display_key="external_extension_trench_length")
            _len_row(tr("product_presets_tab.form.ext_al_threshold", "Ext Al Threshold:"),
                     detail.external_al_threshold, self._view_model.update_external_al_threshold,
                     display_key="external_al_threshold")
            _len_row(tr("product_presets_tab.form.last_branch_al_threshold", "Last Branch Al Threshold:"),
                     detail.last_branch_al_threshold, self._view_model.update_last_branch_al_threshold,
                     display_key="last_branch_al_threshold")
            _len_row(tr("product_presets_tab.form.int_al_threshold", "Int Al Threshold:"),
                     detail.internal_al_threshold, self._view_model.update_internal_al_threshold,
                     display_key="internal_al_threshold")
            _len_row(tr("product_presets_tab.form.int_first_cual_prefix", "Int 1st Cu/Al Prefix:"),
                     detail.internal_first_cual_prefix, self._view_model.update_internal_first_cual_prefix,
                     display_key="internal_first_cual_prefix")
            _len_row(tr("product_presets_tab.form.int_second_cual_suffix", "Int 2nd Cu/Al Suffix:"),
                     detail.internal_second_cual_suffix, self._view_model.update_internal_second_cual_suffix,
                     display_key="internal_second_cual_suffix")

            extra_cb = QCheckBox()
            extra_cb.setChecked(detail.last_branch_with_extra_length)
            extra_cb.toggled.connect(self._view_model.update_last_branch_with_extra_length)
            params_form.addRow(tr("product_presets_tab.form.last_branch_extra", "Last Branch Extra:"), extra_cb)
            self._detail_layout.addWidget(params_group)

            # -- Branch Coordination group (only when branch_count > 0) --
            try:
                bc = int(detail.branch_count)
            except ValueError:
                bc = 0
            if bc > 0:
                coord_group = QGroupBox(tr("product_presets_tab.branch_coordination_group", "Branch Coordination"))
                self.register_voltage_field("branch_lines", coord_group)
                coord_form = QFormLayout(coord_group)
                for branch_position, bl_entry in enumerate(detail.branch_lines):
                    idx_str, pos_val, neg_val = bl_entry
                    pos_edit = QLineEdit(pos_val)
                    neg_edit = QLineEdit(neg_val)
                    idx_int = int(idx_str)
                    self.register_voltage_field(
                        f"branch_lines[{branch_position}].coordinate_positive", pos_edit
                    )
                    self.register_voltage_field(
                        f"branch_lines[{branch_position}].coordinate_negative", neg_edit
                    )
                    pos_edit.editingFinished.connect(
                        lambda e=pos_edit, i=idx_int: self._view_model.update_branch_coordinate_positive(i, e.text())
                    )
                    neg_edit.editingFinished.connect(
                        lambda e=neg_edit, i=idx_int: self._view_model.update_branch_coordinate_negative(i, e.text())
                    )
                    # Add conversion display labels for branch coordinates
                    pos_display = detail.length_displays.get(f"branch_coord_{idx_int}_pos", "")
                    neg_display = detail.length_displays.get(f"branch_coord_{idx_int}_neg", "")
                    pos_label = QLabel(pos_display)
                    pos_label.setStyleSheet("color: gray; font-size: 10px;")
                    neg_label = QLabel(neg_display)
                    neg_label.setStyleSheet("color: gray; font-size: 10px;")
                    row_label = tr(
                        "product_presets_tab.form.branch_coord_row",
                        "Branch {index} (±)"
                    ).format(index=idx_str)
                    row_widget = QWidget()
                    row_layout = QHBoxLayout(row_widget)
                    row_layout.setContentsMargins(0, 0, 0, 0)
                    row_layout.addWidget(QLabel("+"))
                    row_layout.addWidget(pos_edit)
                    row_layout.addWidget(pos_label)
                    row_layout.addWidget(QLabel("-"))
                    row_layout.addWidget(neg_edit)
                    row_layout.addWidget(neg_label)
                    coord_form.addRow(row_label, row_widget)
                self._detail_layout.addWidget(coord_group)

        # -- Gap Extender group --
        if is_gap_extender:
            gap_extender_group = QGroupBox(tr("product_presets_tab.extender_group", "Gap Extender Configuration"))
            gap_extender_form = QFormLayout(gap_extender_group)

            # Connector Male
            gap_extender_form.addRow(
                tr("product_presets_tab.form.connector_male", "Connector Male:"),
                self._make_connector_row(
                    detail.connector_male_type, self._view_model.update_gap_extender_connector_male_type,
                    detail.connector_male_manufacturer, self._view_model.update_gap_extender_connector_male_manufacturer,
                )
            )

            # Connector Female
            gap_extender_form.addRow(
                tr("product_presets_tab.form.connector_female", "Connector Female:"),
                self._make_connector_row(
                    detail.connector_female_type, self._view_model.update_gap_extender_connector_female_type,
                    detail.connector_female_manufacturer, self._view_model.update_gap_extender_connector_female_manufacturer,
                )
            )

            # Cu Wire (size + strands)
            gap_extender_form.addRow(tr("product_presets_tab.form.cu_wire", "Cu Wire:"), self._make_wire_edit(
                detail.cu_wire, detail.cu_wire_strands,
                self._view_model.update_gap_extender_cu_wire, self._view_model.update_gap_extender_cu_strands,
                material="Cu"))

            # Label
            label_combo = disable_combo_scroll(QComboBox())
            label_options = ["", "Motor Gap Extender", "BHA Gap Extender"]
            label_combo.addItems(label_options)
            if detail.label in label_options:
                label_combo.setCurrentText(detail.label)
            else:
                label_combo.setCurrentText("")
            label_combo.currentTextChanged.connect(self._view_model.update_gap_extender_label)
            gap_extender_form.addRow(tr("product_presets_tab.form.label", "Label:"), label_combo)

            # Length
            el_display = detail.length_displays.get("length", "")
            el_row = QWidget()
            el_row_layout = QHBoxLayout(el_row)
            el_row_layout.setContentsMargins(0, 0, 0, 0)
            el_edit = QLineEdit(detail.length)
            el_edit.editingFinished.connect(
                lambda e=el_edit: self._view_model.update_gap_extender_length(e.text())
            )
            el_row_layout.addWidget(el_edit)
            el_label = QLabel(el_display)
            el_label.setStyleSheet("color: gray; font-size: 10px;")
            el_row_layout.addWidget(el_label)
            gap_extender_form.addRow(tr("product_presets_tab.form.length", "Length:"), el_row)

            self._detail_layout.addWidget(gap_extender_group)

        # -- LYNX group --
        elif is_lynx:
            lynx_group = QGroupBox(tr("product_presets_tab.lynx_group", "LYNX Configuration"))
            lynx_form = QFormLayout(lynx_group)

            lynx_form.addRow(tr("product_presets_tab.form.cluster_wire", "Cluster Wire:"),
                             self._make_wire_edit(
                                 detail.cluster_wire, detail.cluster_wire_strands,
                                 self._view_model.update_cluster_wire,
                                 self._view_model.update_cluster_wire_strands,
                                 material="Al", strands_readonly=True))

            # Branch Cu Length with conversion display
            bcl_display = detail.length_displays.get("branch_length_cu", "")
            bcl_row = QWidget()
            bcl_row_layout = QHBoxLayout(bcl_row)
            bcl_row_layout.setContentsMargins(0, 0, 0, 0)
            bcl_edit = QLineEdit(detail.branch_length_cu)
            self.register_voltage_field("branch_length_cu", bcl_edit)
            bcl_edit.editingFinished.connect(
                lambda e=bcl_edit: self._view_model.update_branch_length_cu(e.text()))
            bcl_row_layout.addWidget(bcl_edit)
            bcl_label = QLabel(bcl_display)
            bcl_label.setStyleSheet("color: gray; font-size: 10px;")
            bcl_row_layout.addWidget(bcl_label)
            lynx_form.addRow(tr("product_presets_tab.form.branch_cu_length", "Branch Cu Length:"), bcl_row)

            # Branch Al Length with conversion display
            bal_display = detail.length_displays.get("branch_length_al", "")
            bal_row = QWidget()
            bal_row_layout = QHBoxLayout(bal_row)
            bal_row_layout.setContentsMargins(0, 0, 0, 0)
            bal_edit = QLineEdit(detail.branch_length_al)
            self.register_voltage_field("branch_length_al", bal_edit)
            bal_edit.editingFinished.connect(
                lambda e=bal_edit: self._view_model.update_branch_length_al(e.text()))
            bal_row_layout.addWidget(bal_edit)
            bal_label = QLabel(bal_display)
            bal_label.setStyleSheet("color: gray; font-size: 10px;")
            bal_row_layout.addWidget(bal_label)
            lynx_form.addRow(tr("product_presets_tab.form.branch_al_length", "Branch Al Length:"), bal_row)

            lynx_form.addRow(tr("product_presets_tab.form.branch_cu_wire", "Branch Cu Wire:"), self._make_wire_edit(
                detail.branch_cu_wire, detail.branch_cu_strands,
                self._view_model.update_branch_cu_wire, self._view_model.update_branch_cu_strands,
                material="Cu"))
            lynx_form.addRow(tr("product_presets_tab.form.branch_al_wire", "Branch Al Wire:"), self._make_wire_edit(
                detail.branch_al_wire, detail.branch_al_strands,
                self._view_model.update_branch_al_wire, self._view_model.update_branch_al_strands,
                material="Al", strands_readonly=True))

            # Row per Overmold
            rpo_edit = QLineEdit(detail.row_per_overmold)
            self.register_voltage_field("row_per_overmold", rpo_edit)
            rpo_edit.editingFinished.connect(
                lambda e=rpo_edit: self._view_model.update_row_per_overmold(e.text()))
            lynx_form.addRow(
                tr("product_presets_tab.form.row_per_overmold", "Row per Overmold:"),
                rpo_edit)

            # Tail Shortened
            ts_check = QCheckBox()
            ts_check.setChecked(detail.tail_shortened)
            ts_check.toggled.connect(self._view_model.update_tail_shortened)
            lynx_form.addRow(
                tr("product_presets_tab.form.tail_shortened", "Tail Shortened:"),
                ts_check)

            # Nearby Overmold Gap with conversion display
            nog_display = detail.length_displays.get("nearby_overmold_gap", "")
            nog_row = QWidget()
            nog_row_layout = QHBoxLayout(nog_row)
            nog_row_layout.setContentsMargins(0, 0, 0, 0)
            nog_edit = QLineEdit(detail.nearby_overmold_gap)
            self.register_voltage_field("nearby_overmold_gap", nog_edit)
            nog_edit.editingFinished.connect(
                lambda e=nog_edit: self._view_model.update_nearby_overmold_gap(e.text()))
            nog_row_layout.addWidget(nog_edit)
            nog_label = QLabel(nog_display)
            nog_label.setStyleSheet("color: gray; font-size: 10px;")
            nog_row_layout.addWidget(nog_label)
            lynx_form.addRow(
                tr("product_presets_tab.form.nearby_overmold_gap", "Nearby Overmold Gap:"),
                nog_row)

            # Buffer Length with conversion display
            bfl_display = detail.length_displays.get("buffer_length", "")
            bfl_row = QWidget()
            bfl_row_layout = QHBoxLayout(bfl_row)
            bfl_row_layout.setContentsMargins(0, 0, 0, 0)
            bfl_edit = QLineEdit(detail.buffer_length)
            self.register_voltage_field("buffer_length", bfl_edit)
            bfl_edit.editingFinished.connect(
                lambda e=bfl_edit: self._view_model.update_buffer_length(e.text()))
            bfl_row_layout.addWidget(bfl_edit)
            bfl_label = QLabel(bfl_display)
            bfl_label.setStyleSheet("color: gray; font-size: 10px;")
            bfl_row_layout.addWidget(bfl_label)
            lynx_form.addRow(
                tr("product_presets_tab.form.buffer_length", "Buffer Length:"),
                bfl_row)

            self._detail_layout.addWidget(lynx_group)

        self._detail_layout.addStretch()

    # -- Widget factory helpers --

    @staticmethod
    def _make_combo(options: set[str] | tuple[str, ...], current: str,
                    handler) -> QComboBox:
        """Create a QComboBox with sorted options and a change handler."""
        combo = disable_combo_scroll(QComboBox())
        sorted_opts = sorted(options, key=lambda s: ("none" != s.lower(), s))
        combo.addItems(sorted_opts)
        if current in options:
            combo.setCurrentText(current)
        combo.currentTextChanged.connect(handler)
        return combo

    def _make_wire_edit(self, size: str, strands: str = "none",
                        size_handler=None, strands_handler=None,
                        material: str = "Cu",
                        strands_readonly: bool = False) -> WireEdit:
        """Create a WireEdit pre-filled with wire size and strands.

        Material is set per-field by the caller (defaults to "Cu").
        When strands_readonly is True, the strands combo is replaced by a
        read-only label (auto-inferred from wire size).
        """
        widget = WireEdit()
        widget.material = material
        widget.size_combo.blockSignals(True)
        widget.strands_combo.blockSignals(True)
        widget.set_value(size, strands)
        widget.size_combo.blockSignals(False)
        widget.strands_combo.blockSignals(False)
        widget.strands_readonly = strands_readonly
        if size_handler:
            widget.size_combo.currentTextChanged.connect(size_handler)
        if strands_handler:
            widget.strands_combo.currentTextChanged.connect(strands_handler)
        if size_handler is not None:
            handler_name = getattr(size_handler, "__name__", "")
            if handler_name.startswith("update_"):
                self.register_voltage_field(handler_name.removeprefix("update_"), widget)
        return widget

    @staticmethod
    def _make_connector_row(
        conn_type: str, type_handler,
        manufacturer: str, manufacturer_handler=None,
    ) -> QWidget:
        """Create a connector type combo row (manufacturer is auto-set internally)."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Type combo
        type_combo = disable_combo_scroll(QComboBox())
        sorted_types = sorted(VALID_CONNECTOR_TYPES, key=lambda s: ("none" != s.lower(), s))
        type_combo.addItems(sorted_types)
        if conn_type in VALID_CONNECTOR_TYPES:
            type_combo.setCurrentText(conn_type)
        type_combo.currentTextChanged.connect(type_handler)
        layout.addWidget(QLabel(tr("product_presets_tab.conn_type_label", "Type:")))
        layout.addWidget(type_combo)
        
        # Manufacturer label (read-only, auto-derived from type)
        manu_label = QLabel(manufacturer)
        manu_label.setStyleSheet("color: gray; padding-left: 8px;")
        layout.addWidget(QLabel(tr("product_presets_tab.conn_manu_label", "Mfr:")))
        layout.addWidget(manu_label)
        
        # Keep a reference so callers can update the label if needed
        widget._manu_label = manu_label  # type: ignore[attr-defined]
        
        layout.addStretch()
        return widget

    @staticmethod
    def _make_fuse_row(
        current: str, current_handler,
        manufacturer: str, manufacturer_handler=None,
    ) -> QWidget:
        """Create a combined horizontal row for fuse: current combo + manufacturer label.

        Manufacturer is auto-derived from current (single supplier: Adler) and
        displayed as a read-only label, mirroring the connector row design.
        """
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Current combo (sorted numerically, not alphabetically)
        cur_combo = disable_combo_scroll(QComboBox())
        cur_combo.addItems(sorted_fuse_currents())
        if current:
            cur_combo.setCurrentText(current)
        cur_combo.currentTextChanged.connect(current_handler)
        layout.addWidget(QLabel(tr("product_presets_tab.fuse_current_label", "Current:")))
        layout.addWidget(cur_combo)
        
        # Manufacturer label (read-only, auto-derived from current)
        manu_label = QLabel(manufacturer)
        manu_label.setStyleSheet("color: gray; padding-left: 8px;")
        layout.addWidget(QLabel(tr("product_presets_tab.fuse_manu_label", "Mfr:")))
        layout.addWidget(manu_label)
        
        # Keep a reference so callers can update the label if needed
        widget._manu_label = manu_label  # type: ignore[attr-defined]
        
        layout.addStretch()
        return widget

    # ----------------------------------------------------------------
    #  Events
    # ----------------------------------------------------------------

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
        self._view_model.select_product(item_id)

    def _show_context_menu(self, pos) -> None:
        if self._list.itemAt(pos) is None:
            return
        menu = QMenu(self)
        sort_menu = menu.addMenu(tr("ordering.sort", "Sort"))
        sort_menu.setEnabled(len(self._view_model.state.selected_item_ids) >= 2)
        for label, field in (
            (tr("ordering.name", "Name"), "name"),
            (tr("ordering.remarks", "Remarks"), "remarks"),
            (tr("ordering.pv_panel", "PV Panel"), "pv_panel"),
            (tr("ordering.branch_count", "Branch Count"), "branch_count"),
        ):
            self._add_sort_field(sort_menu, label, field)
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

    def _commit_pv_panel(self, combo: QComboBox) -> None:
        """Commit without rebuilding the editor while its popup is active."""
        normalized = self._view_model.update_pv_panel(combo.currentText())
        if normalized and combo.findText(normalized) < 0:
            combo.addItem(normalized)
        if combo.currentText() != normalized:
            combo.setCurrentText(normalized)

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def _on_cleanup(self) -> None:
        count = self._view_model.count_unused()
        if count == 0:
            QMessageBox.information(
                self,
                tr("product_presets_tab.cleanup_title", "Cleanup"),
                tr("product_presets_tab.no_unused", "No unused presets to remove"),
            )
            return
        reply = QMessageBox.question(
            self,
            tr("product_presets_tab.confirm_cleanup_title", "Confirm Cleanup"),
            tr("product_presets_tab.confirm_cleanup", "Remove {count} unused presets?").format(count=count),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._view_model.cleanup_unused()

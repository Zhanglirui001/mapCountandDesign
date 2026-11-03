import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
)

from count_and_design.gui.views.count_tab import CountTab
from count_and_design.gui.views.length_identifiers_tab import LengthIdentifiersTab
from count_and_design.gui.views.product_presets_tab import ProductPresetsTab
from count_and_design.gui.views.pv_panels_tab import PVPanelsTab
from count_and_design.gui.views.rackings_tab import RackingsTab
from count_and_design.gui.views.widgets import LengthEdit, ProjectTypeEdit
from count_and_design.gui.viewmodels import AppViewModel
from count_and_design.models import (
    IBEXBranchCoordinate,
    Inv,
    Length,
    LengthIdentifier,
    Node,
    ProductPreset,
    Project,
    PVPanelInfo,
    RackingEntry,
    Rackings,
    Row,
)


class ViewModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_count_tab_selection_builds_editor_state(self) -> None:
        app_vm = AppViewModel()
        app_vm.add_inv("INV1")
        count_vm = app_vm.count_tab
        root = count_vm.state.tree[0]
        inv_node = root.children[0]
        count_vm.select_item(inv_node.node_id)
        self.assertEqual(count_vm.state.editor.kind, "inv")
        self.assertEqual(count_vm.state.editor.name, "INV1")

    def test_app_view_model_exposes_public_project_session_boundary(self) -> None:
        app_vm = AppViewModel()
        self.assertIs(app_vm.project, app_vm._project)
        self.assertIsNone(app_vm.current_file)

        payload = app_vm.build_recovery_payload()

        self.assertEqual(payload["name"], app_vm.project.name)
        self.assertIn("version_tree", payload)

    def test_project_type_default_is_stored_and_legacy_empty_value_is_migrated(self) -> None:
        self.assertEqual(Project().project_type, "IBEX")
        self.assertEqual(Project.from_dict({"project_type": ""}).project_type, "IBEX")
        app_vm = AppViewModel()
        self.assertEqual(app_vm._project.project_type, "IBEX")
        self.assertEqual(app_vm.count_tab.state.editor.project_type, "IBEX")

    def test_rackings_created_product_resolves_existing_length_identifier(self) -> None:
        app_vm = AppViewModel()
        project = app_vm.project
        project.length_identifiers.append(
            LengthIdentifier(name="Motor", length=Length("3ft"))
        )
        project.rebind_length_identifiers()
        project.rackings_list.append(Rackings(name="R", product_presets=""))
        app_vm._push_project()

        rackings_item = app_vm.rackings_tab.state.items[0]
        app_vm.rackings_tab.select_item(rackings_item.item_id)
        app_vm.rackings_tab.update_product_presets("M")
        preset = project.get_product_preset("M")
        self.assertIsNotNone(preset)
        assert preset is not None
        self.assertIs(
            preset.product_total_length.registry,
            project.identifier_registry,
        )

        self.assertTrue(app_vm.product_presets_tab.select_object(preset))
        app_vm.product_presets_tab.update_product_total_length("1*Motor")
        app_vm.product_presets_tab.update_branch_coordinate_positive(1, "1Motor")
        app_vm.product_presets_tab.refresh()

        self.assertAlmostEqual(preset.product_total_length.to_ft(), 3.0)
        self.assertAlmostEqual(
            preset.branch_lines[0].coordinate_positive.to_ft(), 3.0
        )
        project.length_identifiers[0].length.set_length_str("5ft")
        self.assertAlmostEqual(preset.product_total_length.to_ft(), 5.0)
        self.assertAlmostEqual(
            preset.branch_lines[0].coordinate_positive.to_ft(), 5.0
        )
        self.assertEqual(
            app_vm.product_presets_tab.state.detail.length_displays[
                "product_total_length"
            ],
            "= 3.00 ft (914.4 mm)",
        )

    def test_branch_count_automatically_selects_product_family(self) -> None:
        app_vm = AppViewModel()
        preset = ProductPreset(name="Z", branch_count=2)
        app_vm.project.product_presets.append(preset)
        vm = app_vm.product_presets_tab
        vm.refresh()
        self.assertTrue(vm.select_object(preset))

        vm.update_branch_count("1")
        self.assertEqual(preset.product_type, "Homerun_Extender")
        self.assertEqual(len(preset.branch_lines), 1)

        vm.update_branch_count("0")
        self.assertEqual(preset.product_type, "Placeholder")
        self.assertEqual(preset.branch_lines, [])

        vm.update_branch_count("3")
        self.assertEqual(preset.product_type, "IBEX")
        self.assertEqual(len(preset.branch_lines), 3)

        tab = ProductPresetsTab(vm)
        self.assertFalse(
            any(
                "Homerun_Extender"
                in [combo.itemText(i) for i in range(combo.count())]
                for combo in tab.findChildren(QComboBox)
            )
        )
        type_display = tab.findChild(QLineEdit, "productTypeDisplay")
        self.assertIsNotNone(type_display)
        assert type_display is not None
        self.assertTrue(type_display.isReadOnly())
        self.assertEqual(type_display.text(), "IBEX")

    def test_internal_al_threshold_is_editable_and_exposed_in_state(self) -> None:
        app_vm = AppViewModel()
        preset = ProductPreset(name="Z", branch_count=3)
        app_vm.project.product_presets.append(preset)
        vm = app_vm.product_presets_tab
        vm.refresh()
        self.assertTrue(vm.select_object(preset))

        self.assertEqual(vm.state.detail.internal_al_threshold, "100ft")

        vm.update_internal_al_threshold("25ft")
        QApplication.processEvents()

        self.assertAlmostEqual(preset.internal_al_threshold.to_ft(), 25.0)
        self.assertEqual(vm.state.detail.internal_al_threshold, "25ft")
        self.assertEqual(
            vm.state.detail.length_displays["internal_al_threshold"],
            "= 25.00 ft (7620.0 mm)",
        )

    def test_rejected_length_input_reports_error_and_reverts_editor(self) -> None:
        """Regression: rejected length input must not linger in the editor.

        Entering a dimension-invalid value (e.g. bare number '0') shows an
        error dialog and keeps the model's previous value; the tab state is
        refreshed so input boxes are reverted to that value instead of
        displaying the rejected text.
        """
        app_vm = AppViewModel()
        project = app_vm.project
        preset = ProductPreset(name="Z", branch_count=1)
        project.product_presets.append(preset)

        vm = app_vm.product_presets_tab
        vm.refresh()
        self.assertTrue(vm.select_object(preset))

        negative = preset.branch_lines[0].coordinate_negative
        negative.set_length_str("2ft")

        errors: list[tuple[str, str]] = []
        vm.error_occurred.connect(lambda title, message: errors.append((title, message)))
        state_updates: list[int] = []
        vm.state_changed.connect(lambda _state: state_updates.append(1))

        vm.update_branch_coordinate_negative(1, "0")  # bare number → dim=0

        # Error dialog would have been shown for the dimension error.
        self.assertEqual(len(errors), 1)
        # The model keeps its previous valid value.
        self.assertEqual(negative.length_str, "2ft")
        # A revert refresh must be scheduled and run: the editor fields are
        # rebuilt so the rejected text does not linger in the input box.
        updates_at_rejection = len(state_updates)
        QApplication.processEvents()
        self.assertGreater(len(state_updates), updates_at_rejection)
        # Rebuilt editor state carries the reverted text.
        branch_entries = vm.state.detail.branch_lines
        self.assertEqual(branch_entries[0], ("1", "0mm", "2ft"))

    def test_project_type_editor_never_masks_an_invalid_model_value(self) -> None:
        editor = ProjectTypeEdit()
        editor.set_value("")
        self.assertEqual(editor.currentIndex(), -1)
        self.assertEqual(editor.currentText(), "")
        editor.set_value("IBEX")
        self.assertEqual(editor.currentText(), "IBEX")

    def test_count_tab_results_are_semantic_and_hide_empty_breakdowns(self) -> None:
        app_vm = AppViewModel()
        results = app_vm.count_tab.state.editor.results

        self.assertTrue(results.statistics)
        self.assertTrue(results.voltage)
        self.assertEqual(results.metrics, results.statistics + results.voltage)
        self.assertEqual(results.breakdowns[0].items, ())
        self.assertEqual(results.breakdowns[1].items, ())
        self.assertEqual(results.breakdowns[2].items, ())
        self.assertIsNotNone(results.detail)
        self.assertEqual(results.detail.rows, ())

    def test_count_tab_overview_exposes_pv_panel_counts(self) -> None:
        app_vm = AppViewModel()
        app_vm._project.product_presets.append(
            ProductPreset(name="A", pv_panel="Panel X", branch_count=2)
        )
        app_vm._project.pv_panels = [
            PVPanelInfo(name="Panel X", module_per_string=24)
        ]
        app_vm._project.rackings_list = [Rackings(name="Rack", product_presets="A")]
        inv = Inv(name="INV1")
        node = Node(name="Node1")
        node.rows.append(Row(name="Row1", racking_entries=[RackingEntry(rackings="Rack")]))
        inv.nodes.append(node)
        app_vm._project.invs.append(inv)
        app_vm._push_project()

        panel_breakdown = app_vm.count_tab.state.editor.results.breakdowns[2]
        self.assertEqual(panel_breakdown.items, (("Panel X", "48"),))

    def test_project_and_inv_overviews_expose_cluster_total_length(self) -> None:
        app_vm = AppViewModel()
        inv = Inv(
            name="INV1",
            nodes=[
                Node(name="Node1", cluster_length=Length("1ft")),
                Node(name="Node2", cluster_length=Length("2m")),
            ],
        )
        app_vm._project.invs.append(inv)
        app_vm._push_project()

        project_breakdown = app_vm.count_tab.state.editor.results.breakdowns[3]
        self.assertEqual(
            project_breakdown.items,
            (("Total", "7.562ft (2304.8mm)"),),
        )

        inv_id = app_vm.count_tab.state.tree[0].children[0].node_id
        app_vm.count_tab.select_item(inv_id)
        inv_breakdown = app_vm.count_tab.state.editor.results.breakdowns[3]
        self.assertEqual(
            inv_breakdown.items,
            (("Total", "7.562ft (2304.8mm)"),),
        )

        node_id = app_vm.count_tab.state.tree[0].children[0].children[0].node_id
        app_vm.count_tab.select_item(node_id)
        self.assertEqual(
            app_vm.count_tab.state.editor.results.breakdowns[3].items,
            (),
        )

    def test_overview_detail_headers_include_child_string_count(self) -> None:
        app_vm = AppViewModel()
        inv = Inv(name="INV1")
        node = Node(name="Node1")
        node.rows.append(Row(name="Row1"))
        inv.nodes.append(node)
        app_vm._project.invs.append(inv)
        app_vm._push_project()
        count_vm = app_vm.count_tab

        self.assertIn("Strings", count_vm.state.editor.results.detail.headers)

        inv_id = count_vm.state.tree[0].children[0].node_id
        count_vm.select_item(inv_id)
        self.assertIn("Strings", count_vm.state.editor.results.detail.headers)

        node_id = count_vm.state.tree[0].children[0].children[0].node_id
        count_vm.select_item(node_id)
        self.assertIn("Strings", count_vm.state.editor.results.detail.headers)

    def test_node_detail_table_exposes_box_position(self) -> None:
        app_vm = AppViewModel()
        inv = Inv(name="INV1")
        inv.nodes.append(Node(name="Node1", box_position=3.5))
        app_vm._project.invs.append(inv)
        app_vm._push_project()
        inv_id = app_vm.count_tab.state.tree[0].children[0].node_id

        app_vm.count_tab.select_item(inv_id)

        detail = app_vm.count_tab.state.editor.results.detail
        column = detail.headers.index("Box Position")
        self.assertEqual(detail.rows[0][column], "3.5")

    def test_negative_box_position_is_reported_and_reset_to_zero(self) -> None:
        app_vm = AppViewModel()
        node = Node(name="Node1", box_position=2)
        app_vm.project.invs.append(Inv(name="INV1", nodes=[node]))
        app_vm._push_project()
        node_id = app_vm.count_tab.state.tree[0].children[0].children[0].node_id
        app_vm.count_tab.select_item(node_id)
        errors: list[tuple[str, str]] = []
        app_vm.count_tab.error_occurred.connect(
            lambda title, message: errors.append((title, message))
        )

        app_vm.count_tab.update_node_field("box_position", "-3")

        self.assertEqual(node.box_position, 0.0)
        self.assertEqual(app_vm.count_tab.state.editor.box_position, 0.0)
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot be negative", errors[0][1])

    def test_row_results_do_not_include_sibling_row_details(self) -> None:
        app_vm = AppViewModel()
        inv = Inv(name="INV1")
        node = Node(name="Node1")
        node.rows.extend((Row(name="Row1"), Row(name="Row2")))
        inv.nodes.append(node)
        app_vm._project.invs.append(inv)
        app_vm._push_project()
        row_node = app_vm.count_tab.state.tree[0].children[0].children[0].children[0]

        app_vm.count_tab.select_item(row_node.node_id)

        results = app_vm.count_tab.state.editor.results
        self.assertEqual(results.breakdowns, ())
        self.assertEqual(results.detail.title, "Product Voltage Drop")
        self.assertEqual(results.detail.rows, ())

    def test_voltage_status_activation_carries_semantic_row_target(self) -> None:
        app_vm = AppViewModel()
        row = Row(name="Broken Row", racking_entries=[RackingEntry(rackings="Missing")])
        node = Node(name="Node1", rows=[row])
        app_vm._project.invs.append(Inv(name="INV1", nodes=[node]))
        app_vm._push_project()
        request = app_vm.count_tab.create_voltage_calculation_request()
        self.assertIsNotNone(request)
        report = app_vm.count_tab.calculate_voltage_request(request)
        self.assertTrue(app_vm.count_tab.accept_voltage_report(request, report))

        status = app_vm.count_tab.state.editor.results.voltage[2]
        missing = status.activation
        self.assertIsNotNone(missing)
        self.assertEqual(missing.path, "INV1 / Node1 / Broken Row")
        self.assertEqual(missing.source.object_kind, "row")
        self.assertIs(missing.source.object_ref, row)
        self.assertEqual(missing.source.primary_field, "racking_entries[0].rackings")

        widget = CountTab(app_vm.count_tab)
        widget.show()
        self.app.processEvents()
        activated: list[object] = []
        widget.voltage_issue_activated.connect(activated.append)
        locate_button = widget.findChild(QPushButton, "voltageLocateButton")
        self.assertIsNotNone(locate_button)
        QTest.mouseDClick(locate_button.parentWidget(), Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(activated, [])
        QTest.mouseClick(locate_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(activated, [missing])

    def test_branch_focus_keys_use_list_position_not_business_index(self) -> None:
        app_vm = AppViewModel()
        preset = ProductPreset(
            name="A",
            branch_count=2,
            branch_lines=[
                IBEXBranchCoordinate(3, Length("0mm"), Length("0mm")),
                IBEXBranchCoordinate(7, Length("1Bad"), Length("1mm")),
            ],
        )
        app_vm._project.product_presets = [preset]
        app_vm._push_project()
        app_vm.product_presets_tab.select_object(preset)
        widget = ProductPresetsTab(app_vm.product_presets_tab)
        widget.show()
        self.app.processEvents()

        self.assertIn("branch_lines[0].coordinate_positive", widget._voltage_fields)
        self.assertIn("branch_lines[1].coordinate_positive", widget._voltage_fields)
        self.assertNotIn("branch_lines[6].coordinate_positive", widget._voltage_fields)

    def test_count_tab_expansion_persists(self) -> None:
        app_vm = AppViewModel()
        app_vm.add_inv("INV1")
        count_vm = app_vm.count_tab
        root = count_vm.state.tree[0]
        count_vm.set_expanded_ids([root.node_id])
        count_vm.refresh()
        self.assertTrue(count_vm.state.tree[0].expanded)

    def test_selection_and_expansion_reuse_existing_tree_items(self) -> None:
        app_vm = AppViewModel()
        inv = Inv(name="INV1", nodes=[Node(name="Node1", rows=[Row(name="Row1")])])
        app_vm._project.invs.append(inv)
        app_vm._push_project()
        count_vm = app_vm.count_tab
        widget = CountTab(count_vm)
        widget.show()
        self.app.processEvents()
        tree_state = count_vm.state.tree
        node_id = tree_state[0].children[0].children[0].node_id
        existing_item = widget._item_lookup[node_id]

        with patch.object(
            count_vm._voltage_drop_service,
            "calculate",
            wraps=count_vm._voltage_drop_service.calculate,
        ) as calculate:
            count_vm.select_item(node_id)
            existing_item.setExpanded(True)
            self.app.processEvents()

        self.assertIs(count_vm.state.tree, tree_state)
        self.assertIs(widget._item_lookup[node_id], existing_item)
        self.assertEqual(calculate.call_count, 0)

    def test_manual_voltage_report_is_rebound_and_preserved_after_edits(self) -> None:
        app_vm = AppViewModel()
        row = Row(name="Broken", racking_entries=[RackingEntry(rackings="Missing")])
        node = Node(name="Node1", rows=[row])
        app_vm._project.invs.append(Inv(name="INV1", nodes=[node]))
        app_vm._push_project()
        count_vm = app_vm.count_tab
        request = count_vm.create_voltage_calculation_request()
        self.assertIsNotNone(request)
        report = count_vm.calculate_voltage_request(request)
        snapshot_issue_ref = report.issues[0].source.object_ref
        self.assertIsNot(snapshot_issue_ref, row)
        self.assertTrue(count_vm.accept_voltage_report(request, report))
        self.assertIs(report.issues[0].source.object_ref, row)
        previous_voltage = count_vm.state.editor.results.voltage

        count_vm.update_project_field("address", "Changed")

        self.assertEqual(count_vm.state.editor.results.voltage, previous_voltage)
        self.assertFalse(count_vm.accept_voltage_report(request, report))

    def test_voltage_button_runs_background_calculation(self) -> None:
        app_vm = AppViewModel()
        widget = CountTab(app_vm.count_tab)
        widget.show()

        QTest.mouseClick(widget._voltage_button, Qt.MouseButton.LeftButton)
        for _ in range(100):
            self.app.processEvents()
            if widget._voltage_worker is None:
                break
            QTest.qWait(5)

        self.assertIsNone(widget._voltage_worker)
        self.assertTrue(app_vm.count_tab.has_voltage_report)
        self.assertFalse(widget._voltage_progress.isVisible())
        self.assertEqual(widget._voltage_button.text(), "Calculate voltage drop")
        voltage_header = widget._voltage_button.parentWidget()
        self.assertIs(widget._voltage_progress.parentWidget(), voltage_header)
        self.assertEqual(
            [label.text() for label in voltage_header.findChildren(QLabel)],
            ["Voltage Drop"],
        )
        self.assertEqual(voltage_header.layout().indexOf(widget._voltage_progress), 1)
        self.assertEqual(voltage_header.layout().indexOf(widget._voltage_button), 2)

    def test_field_updates_do_not_emit_full_editor_refresh(self) -> None:
        app_vm = AppViewModel()
        count_vm = app_vm.count_tab
        events: list[object] = []
        result_events: list[object] = []
        count_vm.state_changed.connect(events.append)
        count_vm.results_changed.connect(result_events.append)
        count_vm.update_project_field("name", "Renamed")
        self.assertEqual(events, [])
        self.assertEqual(result_events, [count_vm.state.editor.results])
        self.assertEqual(app_vm._project.name, "Renamed")

    def test_project_structure_interactions_do_not_implicitly_calculate_voltage_drop(self) -> None:
        app_vm = AppViewModel()
        inv = Inv(name="INV1")
        node = Node(name="Node1")
        row = Row(name="Row1")
        node.rows.append(row)
        inv.nodes.append(node)
        app_vm._project.invs.append(inv)
        app_vm._push_project()
        count_vm = app_vm.count_tab

        with patch.object(
            count_vm._voltage_drop_service,
            "calculate",
            wraps=count_vm._voltage_drop_service.calculate,
        ) as calculate:
            count_vm.update_project_field("address", "New address")
            self.assertEqual(calculate.call_count, 0)

            inv_id = count_vm.state.tree[0].children[0].node_id
            count_vm.select_item(inv_id)
            count_vm.update_inv_field("racking_pitch", "1m")
            self.assertEqual(calculate.call_count, 0)

            node_id = count_vm.state.tree[0].children[0].children[0].node_id
            count_vm.select_item(node_id)
            count_vm.update_node_field("box_position", "2")
            self.assertEqual(calculate.call_count, 0)

            row_id = count_vm.state.tree[0].children[0].children[0].children[0].node_id
            count_vm.select_item(row_id)
            count_vm.update_row_field("rackings_entry_offset:0", "1m")
            self.assertEqual(calculate.call_count, 0)

    def test_switching_back_to_project_structure_does_not_calculate_voltage_drop(self) -> None:
        app_vm = AppViewModel()
        count_vm = app_vm.count_tab

        app_vm.on_tab_changed(1)
        with patch.object(
            count_vm._voltage_drop_service,
            "calculate",
            wraps=count_vm._voltage_drop_service.calculate,
        ) as calculate:
            app_vm.on_tab_changed(0)

        self.assertEqual(calculate.call_count, 0)

    def test_tab_refreshes_through_app_view_model(self) -> None:
        app_vm = AppViewModel()
        app_vm.add_inv("INV1")
        app_vm.on_tab_changed(1)
        self.assertEqual(app_vm.length_identifiers_tab.state.items, ())
        app_vm.on_tab_changed(2)
        self.assertEqual(app_vm.rackings_tab.state.items, ())
        app_vm.on_tab_changed(3)
        # 新项目自动创建默认 LYNX preset "LA"（未引用，显示为 unused）
        self.assertEqual(len(app_vm.product_presets_tab.state.items), 1)
        self.assertTrue(app_vm.product_presets_tab.state.items[0].label.startswith("LA"))
        self.assertTrue(app_vm.product_presets_tab.state.items[0].muted)

    def test_product_preset_list_displays_name_and_remarks(self) -> None:
        app_vm = AppViewModel()
        app_vm._project.rackings_list.append(Rackings(name="R1", product_presets="AB"))
        app_vm._project.product_presets.extend([
            ProductPreset(name="A", remarks="2 String North"),
            ProductPreset(name="B", remarks="   "),
        ])

        app_vm.product_presets_tab.refresh()

        labels = {item.label for item in app_vm.product_presets_tab.state.items}
        self.assertIn("A (2 String North)", labels)
        self.assertIn("B", labels)

    def test_pv_panel_combo_stays_open_and_commits_typed_name(self) -> None:
        app_vm = AppViewModel()
        app_vm._project.pv_panels.append(PVPanelInfo(name="Existing"))
        app_vm.product_presets_tab.refresh()
        widget = ProductPresetsTab(app_vm.product_presets_tab)
        widget.show()
        self.app.processEvents()
        combo = widget.findChild(QComboBox, "pvPanelCombo")
        self.assertIsNotNone(combo)

        combo.showPopup()
        combo.lineEdit().editingFinished.emit()
        self.app.processEvents()
        self.assertTrue(combo.view().isVisible())
        combo.hidePopup()

        existing_index = combo.findText("Existing")
        combo.setCurrentIndex(existing_index)
        combo.activated.emit(existing_index)
        selected = app_vm.product_presets_tab._current_preset()
        self.assertEqual(selected.pv_panel, "Existing")
        self.assertEqual(len(app_vm._project.pv_panels), 1)

        combo.setEditText("  Brand X  ")
        combo.lineEdit().editingFinished.emit()

        selected = app_vm.product_presets_tab._current_preset()
        self.assertEqual(selected.pv_panel, "Brand X")
        self.assertEqual(
            [panel.name for panel in app_vm._project.pv_panels],
            ["Existing", "Brand X"],
        )
        self.assertGreaterEqual(combo.findText("Brand X"), 0)

        combo.lineEdit().editingFinished.emit()
        self.assertEqual(len(app_vm._project.pv_panels), 2)

    def test_pv_panel_tab_marks_unused_and_system_voltage_is_selectable_text(self) -> None:
        app_vm = AppViewModel()
        app_vm._project.product_presets.append(ProductPreset(name="A", pv_panel="Used"))
        app_vm._project.pv_panels.extend(
            [
                PVPanelInfo(vmp=40.0, module_per_string=20, name="Used"),
                PVPanelInfo(name="Unused"),
            ]
        )
        app_vm.pv_panels_tab.refresh()

        items = app_vm.pv_panels_tab.state.items
        self.assertFalse(items[0].muted)
        self.assertTrue(items[1].muted)
        self.assertIn("unused", items[1].label.lower())

        widget = PVPanelsTab(app_vm.pv_panels_tab)
        widget.show()
        self.app.processEvents()
        self.assertEqual(
            widget._list.item(1).foreground().color(),
            QColor(Qt.GlobalColor.gray),
        )
        voltage = widget.findChild(QLabel, "systemVoltageValue")
        self.assertEqual(voltage.text(), "800.000")
        flags = voltage.textInteractionFlags()
        self.assertTrue(flags & Qt.TextInteractionFlag.TextSelectableByMouse)
        self.assertTrue(flags & Qt.TextInteractionFlag.TextSelectableByKeyboard)

        app_vm.pv_panels_tab.cleanup_unused()
        self.assertEqual([panel.name for panel in app_vm._project.pv_panels], ["Used"])

    def test_count_tab_keeps_paste_target_selected_for_repeated_paste(self) -> None:
        app_vm = AppViewModel()
        app_vm.add_inv("INV1")
        count_vm = app_vm.count_tab
        inv = app_vm._project.invs[0]
        count_vm._tree_service.create_child(app_vm._project, "inv", inv)
        count_vm.refresh()
        inv_node_id = count_vm.state.tree[0].children[0].node_id
        source_node_id = count_vm.state.tree[0].children[0].children[0].node_id
        count_vm.select_item(source_node_id)
        count_vm.copy_selected()
        count_vm.select_item(inv_node_id)
        count_vm.paste_to_selected()
        count_vm.paste_to_selected()
        self.assertEqual(len(app_vm._project.invs[0].nodes), 3)
        self.assertEqual(count_vm.state.selected_node_id, inv_node_id)

    def test_count_tab_expands_parent_after_pasting_node(self) -> None:
        app_vm = AppViewModel()
        app_vm.add_inv("INV1")
        count_vm = app_vm.count_tab
        inv = app_vm._project.invs[0]
        count_vm._tree_service.create_child(app_vm._project, "inv", inv)
        count_vm.refresh()
        inv_state = count_vm.state.tree[0].children[0]
        source_node_id = inv_state.children[0].node_id

        count_vm.select_item(source_node_id)
        count_vm.copy_selected()
        count_vm.select_item(inv_state.node_id)
        count_vm.set_expanded(inv_state.node_id, False)
        count_vm.paste_to_selected()

        refreshed_inv = count_vm.state.tree[0].children[0]
        self.assertEqual(len(refreshed_inv.children), 2)
        self.assertTrue(refreshed_inv.expanded)

    def test_rackings_product_presets_update_does_not_emit_full_refresh(self) -> None:
        app_vm = AppViewModel()
        app_vm._project.rackings_list.append(Rackings(name="ABC", product_presets="A"))
        app_vm.rackings_tab.refresh()
        item_id = app_vm.rackings_tab.state.items[0].item_id
        app_vm.rackings_tab.select_item(item_id)
        events: list[object] = []
        app_vm.rackings_tab.state_changed.connect(events.append)
        app_vm.rackings_tab.update_product_presets("AB")
        self.assertEqual(events, [])
        self.assertEqual(app_vm._project.rackings_list[0].product_presets, "AB")
        self.assertEqual(app_vm.rackings_tab.state.detail.product_presets, "AB")

    def test_duplicate_rackings_rename_waits_for_confirmation_without_refresh(self) -> None:
        app_vm = AppViewModel()
        inv = Inv(name="INV1")
        node = Node(name="Node1")
        node.rows.append(Row(name="Row1", racking_entries=[RackingEntry(rackings="AAA"), RackingEntry(rackings="BBB")]))
        inv.nodes.append(node)
        app_vm._project.invs.append(inv)
        app_vm._project.rackings_list.extend(
            [Rackings(name="AAA", product_presets="A"), Rackings(name="BBB", product_presets="B")]
        )
        app_vm.rackings_tab.refresh()
        item_id = app_vm.rackings_tab.state.items[0].item_id
        app_vm.rackings_tab.select_item(item_id)
        events: list[object] = []
        app_vm.rackings_tab.state_changed.connect(events.append)

        status, message = app_vm.rackings_tab.rename_rackings("BBB")

        self.assertEqual(status, "needs_confirmation")
        self.assertIn("already exists", message)
        self.assertEqual(events, [])
        self.assertEqual([item.name for item in app_vm._project.rackings_list], ["AAA", "BBB"])
        self.assertEqual(app_vm.rackings_tab.state.detail.name, "AAA")

    def test_rackings_tab_confirmed_merge_uses_typed_name(self) -> None:
        app_vm = AppViewModel()
        inv = Inv(name="INV1")
        node = Node(name="Node1")
        node.rows.append(Row(name="Row1", racking_entries=[RackingEntry(rackings="AAA"), RackingEntry(rackings="BBB")]))
        inv.nodes.append(node)
        app_vm._project.invs.append(inv)
        app_vm._project.rackings_list.extend(
            [Rackings(name="AAA", product_presets="A"), Rackings(name="BBB", product_presets="B")]
        )
        app_vm.rackings_tab.refresh()
        item_id = app_vm.rackings_tab.state.items[0].item_id
        app_vm.rackings_tab.select_item(item_id)
        widget = RackingsTab(app_vm.rackings_tab)
        widget._name_edit.setText("BBB")

        with patch(
            "count_and_design.gui.views.rackings_tab.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            widget._on_rename()

        self.assertEqual([item.name for item in app_vm._project.rackings_list], ["BBB"])
        row = app_vm._project.invs[0].nodes[0].rows[0]
        self.assertEqual(row.racking_entries[0].rackings, "BBB")
        self.assertEqual(row.racking_entries[1].rackings, "BBB")

    def test_statistics_tables_are_read_only(self) -> None:
        app_vm = AppViewModel()
        app_vm.add_inv("INV1")
        widget = CountTab(app_vm.count_tab)
        read_only_tables = widget.findChildren(QTableView)
        self.assertTrue(read_only_tables)
        for table in read_only_tables:
            self.assertEqual(table.editTriggers(), table.EditTrigger.NoEditTriggers)
            self.assertEqual(
                table.selectionMode(), table.SelectionMode.ExtendedSelection
            )
            self.assertFalse(table.verticalHeader().isVisible())
            self.assertEqual(
                table.verticalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )

    def test_long_statistics_table_expands_without_nested_vertical_scroll(self) -> None:
        app_vm = AppViewModel()
        for index in range(12):
            app_vm.add_inv(f"INV{index + 1}")
        widget = CountTab(app_vm.count_tab)
        widget.resize(1000, 600)
        widget.show()
        self.app.processEvents()
        table = widget.findChild(QTableView, "resultsTable")

        expected_rows_height = (
            table.verticalHeader().defaultSectionSize() * table.model().rowCount()
        )
        self.assertEqual(table.verticalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assertGreaterEqual(table.height(), expected_rows_height + table.horizontalHeader().height())

    def test_statistics_table_columns_can_be_resized_manually(self) -> None:
        app_vm = AppViewModel()
        app_vm.add_inv("INV1")
        widget = CountTab(app_vm.count_tab)
        table = widget.findChild(QTableView, "resultsTable")

        header = table.horizontalHeader()
        for column in range(table.model().columnCount()):
            self.assertEqual(
                header.sectionResizeMode(column),
                QHeaderView.ResizeMode.Interactive,
            )

    def test_statistics_table_copies_selected_row_as_tsv(self) -> None:
        app_vm = AppViewModel()
        app_vm.add_inv("INV1")
        widget = CountTab(app_vm.count_tab)
        widget.show()
        self.app.processEvents()
        table = widget.findChild(QTableView, "resultsTable")
        self.assertIsNotNone(table)
        table.setFocus()
        table.selectRow(0)
        QApplication.clipboard().clear()

        QTest.keyClick(table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

        copied = QApplication.clipboard().text()
        self.assertTrue(copied.startswith("INV1\t"))
        self.assertEqual(copied.count("\t"), table.model().columnCount() - 1)

    def test_large_statistics_table_uses_a_lazy_model(self) -> None:
        app_vm = AppViewModel()
        app_vm._project.invs = [Inv(name=f"INV{index}") for index in range(1000)]
        app_vm._push_project()

        widget = CountTab(app_vm.count_tab)
        table = widget.findChild(QTableView, "resultsTable")

        self.assertIsNotNone(table)
        self.assertEqual(table.model().rowCount(), 1000)
        self.assertEqual(table.model().columnCount(), 8)

    def test_length_identifiers_tab_exposes_project_identifiers(self) -> None:
        app_vm = AppViewModel()

        app_vm.length_identifiers_tab.add_identifier()
        app_vm.length_identifiers_tab.rename_all("S")
        app_vm.length_identifiers_tab.update_length("1200mm")

        detail = app_vm.length_identifiers_tab.state.detail
        self.assertEqual(detail.name, "S")
        self.assertEqual(detail.length, "1200mm")

    def test_length_identifier_name_edit_automatically_renames_references(self) -> None:
        app_vm = AppViewModel()
        app_vm.length_identifiers_tab.add_identifier()
        app_vm.length_identifiers_tab.update_length("100mm")
        app_vm.count_tab.update_project_field("racking_pitch", "2A")
        widget = LengthIdentifiersTab(app_vm.length_identifiers_tab)

        widget._name_edit.setText("S")
        widget._name_edit.editingFinished.emit()

        self.assertEqual(app_vm._project.length_identifiers[0].name, "S")
        self.assertEqual(app_vm._project.racking_pitch.length_str, "2S")
        self.assertFalse(any(
            button.text() == "Rename All"
            for button in widget.findChildren(QPushButton)
        ))

    def test_length_edit_shows_trailing_operator_error_inline(self) -> None:
        widget = LengthEdit()
        emitted: list[bool] = []
        widget.value_changed.connect(lambda: emitted.append(True))
        widget._line_edit.setText("100mm+")

        widget._line_edit.editingFinished.emit()

        self.assertIn("error", widget._label.text().lower())
        self.assertEqual(emitted, [])

    def test_invalid_identifier_length_does_not_open_error_dialog(self) -> None:
        app_vm = AppViewModel()
        app_vm.length_identifiers_tab.add_identifier()
        app_vm.length_identifiers_tab.update_length("100mm")
        widget = LengthIdentifiersTab(app_vm.length_identifiers_tab)
        widget._length_edit._line_edit.setText("100mm+")

        with patch(
            "count_and_design.gui.views.length_identifiers_tab.QMessageBox.warning"
        ) as warning:
            widget._length_edit._line_edit.editingFinished.emit()

        warning.assert_not_called()
        self.assertIn("error", widget._length_edit._label.text().lower())
        self.assertEqual(
            app_vm._project.length_identifiers[0].length.length_str, "100mm"
        )

    def test_length_identifier_update_rebinds_project_lengths(self) -> None:
        app_vm = AppViewModel()
        app_vm.length_identifiers_tab.add_identifier()
        app_vm.length_identifiers_tab.rename_all("S")
        app_vm.length_identifiers_tab.update_length("100mm")
        app_vm.count_tab.update_project_field("racking_pitch", "2S3inch")

        app_vm.length_identifiers_tab.update_length("150mm")

        self.assertAlmostEqual(app_vm._project.racking_pitch.to_mm(), 2 * 150 + 3 * 25.4)

    def test_length_identifiers_tab_delete_updates_selection(self) -> None:
        app_vm = AppViewModel()
        app_vm.length_identifiers_tab.add_identifier()
        app_vm.length_identifiers_tab.rename_all("S")
        app_vm.length_identifiers_tab.add_identifier()
        app_vm.length_identifiers_tab.rename_all("R")

        first_item = app_vm.length_identifiers_tab.state.items[0].item_id
        app_vm.length_identifiers_tab.select_item(first_item)
        app_vm.length_identifiers_tab.delete_selected()

        self.assertEqual([item.name for item in app_vm._project.length_identifiers], ["R"])
        self.assertEqual(app_vm.length_identifiers_tab.state.detail.name, "R")


if __name__ == "__main__":
    unittest.main()

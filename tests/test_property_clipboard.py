from __future__ import annotations

import os
import unittest
from copy import deepcopy
from dataclasses import dataclass, field, fields

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QListWidget

from count_and_design.gui.viewmodels import AppViewModel
from count_and_design.gui.viewmodels.property_clipboard import PropertyClipboard
from count_and_design.gui.views.product_presets_tab import ProductPresetsTab
from count_and_design.gui.views.pv_panels_tab import PVPanelsTab
from count_and_design.gui.views.rackings_tab import RackingsTab
from count_and_design.models import ProductPreset, PVPanelInfo, Rackings


class PropertyClipboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _send_shortcut(
        self,
        widget,
        source_id: str,
        target_id: str,
    ) -> None:
        widget.show()
        self.app.processEvents()
        item_list: QListWidget = widget._list

        def row_for(item_id: str) -> int:
            for row in range(item_list.count()):
                if item_list.item(row).data(Qt.ItemDataRole.UserRole) == item_id:
                    return row
            self.fail(f"List item not found: {item_id}")

        item_list.setCurrentRow(row_for(source_id))
        item_list.setFocus()
        QTest.keyClick(item_list, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        item_list.setCurrentRow(row_for(target_id))
        QTest.keyClick(item_list, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        widget.close()

    def test_rackings_shortcuts_copy_all_properties_except_name(self) -> None:
        app_vm = AppViewModel()
        source = Rackings("Source", product_presets="AB", gap_extenders=[("GA", 2)])
        target = Rackings("Target", product_presets="C", gap_extenders=[])
        app_vm._project.rackings_list = [source, target]
        app_vm.rackings_tab.refresh()

        self._send_shortcut(
            RackingsTab(app_vm.rackings_tab),
            app_vm.rackings_tab._key_for(source),
            app_vm.rackings_tab._key_for(target),
        )

        self.assertEqual(target.name, "Target")
        self.assertEqual(target.product_presets, "AB")
        self.assertEqual(target.gap_extenders, [("GA", 2)])
        self.assertIsNot(target.gap_extenders, source.gap_extenders)

    def test_product_preset_shortcuts_deep_copy_every_dataclass_field(self) -> None:
        app_vm = AppViewModel()
        source = ProductPreset(name="A", remarks="source", branch_count=2)
        source.branch_cu_wire.strands = "19"
        target = ProductPreset(name="B", remarks="target")
        app_vm._project.product_presets.extend([source, target])
        app_vm.product_presets_tab.refresh()
        expected = {
            model_field.name: deepcopy(getattr(source, model_field.name))
            for model_field in fields(source)
            if model_field.name != "name"
        }

        self._send_shortcut(
            ProductPresetsTab(app_vm.product_presets_tab),
            app_vm.product_presets_tab._key_for(source),
            app_vm.product_presets_tab._key_for(target),
        )

        self.assertEqual(target.name, "B")
        for field_name, value in expected.items():
            self.assertEqual(getattr(target, field_name), value, field_name)
        self.assertIsNot(target.branch_lines, source.branch_lines)
        self.assertIsNot(target.branch_cu_wire, source.branch_cu_wire)

    def test_pv_panel_shortcuts_copy_properties_and_preserve_name(self) -> None:
        app_vm = AppViewModel()
        source = PVPanelInfo(vmp=41.5, imp=13.2, module_per_string=24, name="Source")
        target = PVPanelInfo(vmp=1.0, imp=2.0, module_per_string=3, name="Target")
        app_vm._project.pv_panels = [source, target]
        app_vm.pv_panels_tab.refresh()

        self._send_shortcut(
            PVPanelsTab(app_vm.pv_panels_tab),
            app_vm.pv_panels_tab._key(source),
            app_vm.pv_panels_tab._key(target),
        )

        self.assertEqual(target, PVPanelInfo(41.5, 13.2, 24, "Target"))

    def test_new_dataclass_fields_are_discovered_without_a_copy_list(self) -> None:
        @dataclass
        class FutureModel:
            name: str
            future_property: list[int] = field(default_factory=list)

        source = FutureModel("Source", [1, 2, 3])
        target = FutureModel("Target", [9])
        clipboard = PropertyClipboard()

        self.assertTrue(clipboard.copy_from(source))
        source.future_property.append(4)
        self.assertTrue(clipboard.paste_into(target))

        self.assertEqual(target.name, "Target")
        self.assertEqual(target.future_property, [1, 2, 3])
        self.assertIsNot(target.future_property, source.future_property)


if __name__ == "__main__":
    unittest.main()

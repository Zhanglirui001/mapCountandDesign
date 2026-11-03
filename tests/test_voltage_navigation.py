import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

from count_and_design.gui.views.main_window import MainWindow
from count_and_design.models import (
    IBEXBranchCoordinate,
    Inv,
    Length,
    Node,
    ProductPreset,
    PVPanelInfo,
    RackingEntry,
    Rackings,
    Row,
    Wire,
)


def _calculate_voltage(window: MainWindow) -> None:
    count_tab = window._view_model.count_tab
    request = count_tab.create_voltage_calculation_request()
    assert request is not None
    report = count_tab.calculate_voltage_request(request)
    assert count_tab.accept_voltage_report(request, report)


def test_locate_button_navigates_highlights_and_clears_after_fix() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    project = window._view_model._project
    preset = ProductPreset(
        name="D",
        product_type="IBEX",
        branch_count=3,
        branch_lines=[
            IBEXBranchCoordinate(1, Length("0mm"), Length("0mm")),
            IBEXBranchCoordinate(2, Length("1mm"), Length("1mm")),
            IBEXBranchCoordinate(3, Length("1SMotor"), Length("2mm")),
        ],
        pv_panel="Panel",
        branch_cu_wire=Wire("12AWG", "Cu"),
        external_cu_wire=Wire("12AWG", "Cu"),
        internal_cu_wire=Wire("12AWG", "Cu"),
    )
    project.racking_pitch = Length("0mm")
    project.product_presets.append(preset)
    project.pv_panels.append(PVPanelInfo(40, 10, 20, "Panel"))
    project.rackings_list.append(Rackings("R", "D"))
    project.invs.append(
        Inv(
            "INV1",
            nodes=[
                Node(
                    "Node1",
                    rows=[Row("Row1", [RackingEntry("R", Length("0mm"))])],
                )
            ],
        )
    )
    window._view_model._push_project()
    _calculate_voltage(window)
    window.show()
    app.processEvents()

    locate_button = window._count_tab.findChild(QPushButton, "voltageLocateButton")
    assert locate_button is not None
    QTest.mouseClick(locate_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    app.processEvents()

    field_key = "branch_lines[2].coordinate_positive"
    assert window._tabs.currentIndex() == 4
    assert window._view_model.product_presets_tab.state.detail.name == "D"
    assert field_key in window._product_presets_tab._voltage_fields
    target = window._product_presets_tab._voltage_fields[field_key][0]
    assert "#d93025" in target.styleSheet()

    window._view_model.product_presets_tab.update_branch_coordinate_positive(3, "1mm")
    _calculate_voltage(window)
    app.processEvents()
    app.processEvents()
    assert window._active_voltage_fingerprint is None
    assert window._product_presets_tab._voltage_focus_keys == ()
    window.close()


def test_missing_product_definition_navigates_to_rackings_products() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    project = window._view_model._project
    rackings = Rackings("Broken", "")
    project.racking_pitch = Length("1m")
    project.rackings_list.append(rackings)
    project.invs.append(
        Inv(
            "INV1",
            nodes=[
                Node(
                    "Node1",
                    rows=[Row("Row1", [RackingEntry("Broken", Length("0mm"))])],
                )
            ],
        )
    )
    window._view_model._push_project()
    _calculate_voltage(window)
    window.show()
    app.processEvents()

    issue = window._view_model.count_tab.state.editor.results.voltage[2].activation
    assert issue.code == "no_eligible_products"
    locate_button = window._count_tab.findChild(QPushButton, "voltageLocateButton")
    QTest.mouseClick(locate_button, Qt.MouseButton.LeftButton)
    app.processEvents()

    assert window._tabs.currentIndex() == 3
    assert window._view_model.rackings_tab.state.detail.name == "Broken"
    target = window._rackings_tab._voltage_fields["product_presets"][0]
    assert "#d93025" in target.findChild(QLineEdit).styleSheet()
    window.close()

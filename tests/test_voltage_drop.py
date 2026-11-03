import tempfile
from threading import Event

from openpyxl import load_workbook
import pytest

from count_and_design.application.product_generation import ProductGenerationService
from count_and_design.application.product_generation.result import ProductGenerationResult
from count_and_design.application.catalog import PVPanelService
from count_and_design.application.voltage_drop import (
    VoltageCalculationCancelled,
    VoltageDropService,
    VoltageDropValidationError,
    format_voltage_drop_issue,
    load_resistance_table,
)
from count_and_design.application.voltage_drop.export import export_voltage_drop_to_xlsx
from count_and_design.application.voltage_drop.resistance import normalize_wire_size
from count_and_design.i18n import TranslationManager
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
    Wire,
)


def _cu(size: str = "12AWG") -> Wire:
    return Wire(size, "Cu")


def _homerun(name: str, positive_ft: float, negative_ft: float | None = None) -> ProductPreset:
    negative_ft = positive_ft if negative_ft is None else negative_ft
    return ProductPreset(
        name=name,
        product_type="Homerun_Extender",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                1,
                Length(f"{positive_ft}ft"),
                Length(f"{negative_ft}ft"),
            )
        ],
        external_cu_wire=_cu(),
        external_cu_length_to_row_head=Length("0ft"),
        product_total_length=Length("0ft"),
    )


def _project_with_rows(presets: list[ProductPreset], rackings: list[Rackings], rows: list[Row], project_type: str = "IBEX") -> Project:
    for preset in presets:
        if preset.product_type in {"IBEX", "Homerun_Extender", "LYNX"} and not preset.pv_panel:
            preset.pv_panel = "Test Panel"
    return Project(
        name="Voltage Test",
        project_type=project_type,
        pv_panels=[PVPanelInfo(vmp=40.0, imp=10.0, module_per_string=20, name="Test Panel")],
        racking_pitch=Length("0ft"),
        product_presets=presets,
        rackings_list=rackings,
        invs=[Inv("INV1", nodes=[Node("Node1", box_position=1, rows=rows)])],
    )


def test_resistance_json_contains_only_supported_intersection() -> None:
    table = load_resistance_table()
    assert len(table) == 31
    assert table[("12AWG", "Cu")] == 2.075931
    assert ("12AWG", "Al") not in table
    assert ("250MCM", "Cu") not in table
    assert normalize_wire_size("250 kcmil") == "250MCM"


def test_named_pv_panel_round_trip_and_legacy_global_panel_is_ignored() -> None:
    project = Project(name="P", pv_panels=[PVPanelInfo(41.5, 13.2, 24, "Brand")])
    loaded = Project.from_dict(project.to_dict())
    assert loaded.pv_panels == project.pv_panels
    old_data = project.to_dict()
    old_data["pv_panels"] = []
    old_data["pv_panel_info"] = PVPanelInfo(99.0, 99.0, 99).to_dict()
    old_loaded = Project.from_dict(old_data)
    assert old_loaded.pv_panels == []
    assert not hasattr(old_loaded, "pv_panel_info")


def test_product_preset_json_uses_only_pv_panel_field() -> None:
    project = Project(product_presets=[ProductPreset(name="A", pv_panel="Brand")])

    data = project.to_dict()

    preset_data = data["product_presets"][0]  # type: ignore[index]
    assert preset_data["pv_panel"] == "Brand"  # type: ignore[index]
    assert "solaris_edition" not in preset_data
    assert Project.from_dict(data).product_presets[0].pv_panel == "Brand"

    preset_data.pop("pv_panel")  # type: ignore[union-attr]
    preset_data["solaris_edition"] = "Legacy"  # type: ignore[index]
    assert Project.from_dict(data).product_presets[0].pv_panel == ""


def test_legacy_global_values_cannot_enable_voltage_calculation() -> None:
    project = _project_with_rows(
        [_homerun("A", 1)],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    data = project.to_dict()
    data["pv_panels"] = []
    data["pv_panel_info"] = PVPanelInfo(1234.0, 35.0, 26).to_dict()
    data["product_presets"][0]["pv_panel"] = ""  # type: ignore[index]
    loaded = Project.from_dict(data)
    report = VoltageDropService().calculate(loaded)
    assert report.products[0].voltage_drop is None
    assert any(issue.code == "missing_pv_panel" for issue in report.issues)


def test_named_pv_panels_round_trip_and_sync_from_brand() -> None:
    preset = _homerun("A", 1)
    preset.pv_panel = "Brand X"
    project = Project(
        product_presets=[preset],
    )
    PVPanelService().sync_from_product_presets(project)
    assert project.pv_panels == [PVPanelInfo(name="Brand X")]
    loaded = Project.from_dict(project.to_dict())
    assert loaded.pv_panels == project.pv_panels


def test_voltage_calculation_reports_monotonic_progress() -> None:
    project = _project_with_rows(
        [_homerun("A", 1)],
        [Rackings("R", "A")],
        [
            Row("Row1", [RackingEntry("R", Length("0ft"))]),
            Row("Row2", [RackingEntry("R", Length("0ft"))]),
        ],
    )
    progress: list[tuple[int, int]] = []

    VoltageDropService().calculate(project, progress_callback=lambda done, total: progress.append((done, total)))

    assert progress[0][0] == 0
    assert progress[-1][0] == progress[-1][1]
    assert progress[-1][1] > 0
    assert [done for done, _ in progress] == sorted(done for done, _ in progress)
    assert len({total for _, total in progress}) == 1


def test_voltage_calculation_can_be_cancelled_without_partial_report() -> None:
    project = _project_with_rows(
        [_homerun("A", 1)],
        [Rackings("R", "A")],
        [
            Row("Row1", [RackingEntry("R", Length("0ft"))]),
            Row("Row2", [RackingEntry("R", Length("0ft"))]),
        ],
    )
    cancel_event = Event()

    def cancel_after_first_unit(done: int, _total: int) -> None:
        if done >= 1:
            cancel_event.set()

    with pytest.raises(VoltageCalculationCancelled):
        VoltageDropService().calculate(
            project,
            progress_callback=cancel_after_first_unit,
            cancel_event=cancel_event,
        )


def test_voltage_drop_uses_panel_selected_by_product_preset() -> None:
    preset = _homerun("A", 1)
    preset.pv_panel = "High Current"
    project = _project_with_rows(
        [preset],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    project.pv_panels = [PVPanelInfo(50.0, 15.0, 20, "High Current")]
    report = VoltageDropService().calculate(project)
    product = report.products[0]
    expected = 2 * 15.0 * (2.075931 / 1000)
    assert product.pv_panel_name == "High Current"
    assert product.string_current == 15.0
    assert product.system_voltage == 1000.0
    assert product.voltage_drop == pytest.approx(expected)
    assert product.voltage_drop_percent == pytest.approx(expected / 1000.0)


def test_zero_node_racking_pitch_overrides_inv_and_project() -> None:
    preset = _homerun("A", 1)
    project = _project_with_rows(
        [preset],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    project.racking_pitch = Length("10ft")
    project.invs[0].racking_pitch = Length("5ft")
    project.invs[0].nodes[0].racking_pitch = Length("0ft")
    project.invs[0].nodes[0].box_position = 0
    report = VoltageDropService().calculate(project)
    lengths = [segment.length_ft for segment in report.segments]
    assert lengths == [1.0, 1.0]


def test_unresolved_length_is_reported_without_crashing() -> None:
    preset = _homerun("A", 1)
    preset.branch_lines[0].coordinate_positive = Length("1S")
    project = _project_with_rows(
        [preset],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    report = VoltageDropService().calculate(project)
    assert report.products[0].voltage_drop is None
    assert any(
        issue.code == "invalid_length_expression" and "S" in issue.message
        for issue in report.issues
    )
    issue = next(issue for issue in report.issues if issue.code == "invalid_length_expression")
    assert issue.path == "INV1 / Node1 / Row1 / A"
    assert issue.params["field"] == "ProductPreset[A].branch_lines[0].coordinate_positive"
    assert issue.params["expression"] == "1S"
    assert issue.source.object_kind == "product_preset"
    assert issue.source.object_ref is preset
    assert issue.source.field_keys == ("branch_lines[0].coordinate_positive",)
    with pytest.raises(VoltageDropValidationError) as error:
        report.require_exportable()
    assert "ProductPreset[A].branch_lines[0].coordinate_positive" in str(error.value)
    assert "'1S'" in str(error.value)


def test_product_voltage_drop_adds_positive_and_negative_loop_drop() -> None:
    preset = _homerun("A", 1, 3)
    project = _project_with_rows(
        [preset],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    report = VoltageDropService().calculate(project)
    product = report.products[0]
    resistance = 2.075931 / 1000
    assert product.positive_drop == pytest.approx(10 * resistance * 1)
    assert product.negative_drop == pytest.approx(10 * resistance * 3)
    assert product.voltage_drop == pytest.approx(10 * resistance * 4)


def test_ibex_last_coordinate_span_is_terminal_branch_not_internal_mainline() -> None:
    preset = ProductPreset(
        name="A",
        branch_count=3,
        branch_lines=[
            IBEXBranchCoordinate(1, Length("0ft"), Length("0ft")),
            IBEXBranchCoordinate(2, Length("10ft"), Length("10ft")),
            IBEXBranchCoordinate(3, Length("30ft"), Length("30ft")),
        ],
        branch_cu_wire=_cu("12AWG"),
        external_cu_wire=_cu("12AWG"),
        internal_cu_wire=_cu("10AWG"),
        branch_length_positive=Length("0ft"),
        branch_length_negative=Length("0ft"),
        external_cu_length_to_row_head=Length("0ft"),
    )
    project = _project_with_rows(
        [preset],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    report = VoltageDropService().calculate(project)
    positive = [s for s in report.segments if s.polarity == "POS"]
    internal = [s for s in positive if s.role.startswith("Internal Mainline")]
    terminal = [s for s in positive if s.role == "Terminal Branch"]
    assert [(s.length_ft, s.current_a, s.wire_size) for s in internal] == [(10.0, 20.0, "10AWG")]
    assert [(s.length_ft, s.current_a, s.wire_size) for s in terminal] == [(20.0, 10.0, "12AWG")]
    all_terminal = [s for s in report.segments if s.role == "Terminal Branch"]
    assert report.products[0].branch_drop == pytest.approx(
        sum(s.voltage_drop or 0.0 for s in all_terminal)
    )


def test_node_averages_all_product_instances_not_row_averages() -> None:
    preset_a = _homerun("A", 1)
    preset_b = _homerun("B", 3)
    project = _project_with_rows(
        [preset_a, preset_b],
        [Rackings("RA", "A"), Rackings("RB", "BBB")],
        [
            Row("Row1", [RackingEntry("RA", Length("0ft"))]),
            Row("Row2", [RackingEntry("RB", Length("0ft"))]),
        ],
    )
    report = VoltageDropService().calculate(project)
    node = project.invs[0].nodes[0]
    row1, row2 = node.rows
    unit_drop = 10 * (2.075931 / 1000)
    assert report.for_object(row1).voltage_drop == pytest.approx(2 * unit_drop)
    assert report.for_object(row2).voltage_drop == pytest.approx(6 * unit_drop)
    assert report.for_object(node).voltage_drop == pytest.approx(5 * unit_drop)


def test_placeholder_and_external_prefix_lengths_feed_homerun_voltage_drop() -> None:
    placeholder = ProductPreset(
        name="A", branch_count=0, product_total_length=Length("7ft")
    )
    homerun = _homerun("B", 10)
    homerun.external_cu_length_to_row_head = Length("3ft")
    homerun.external_extension_trench_length = Length("2ft")
    project = _project_with_rows(
        [placeholder, homerun],
        [Rackings("R", "AB")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )

    report = VoltageDropService().calculate(project)

    assert [product.preset_name for product in report.products] == ["B"]
    product = report.products[0]
    for polarity in ("POS", "NEG"):
        segments = [
            segment
            for segment in report.segments
            if segment.instance_id == product.instance_id
            and segment.polarity == polarity
        ]
        assert [(segment.role, segment.length_ft) for segment in segments] == [
            ("Homerun Whip", 5.0),
            ("Homerun Mainline", 17.0),
        ]


def test_homerun_voltage_drop_keeps_external_prefix_copper() -> None:
    homerun = _homerun("A", 0)
    homerun.external_cu_wire = _cu("8AWG")
    homerun.external_al_wire = Wire("6AWG", "Al")
    homerun.external_al_threshold = Length("100ft")
    homerun.external_cu_length_to_row_head = Length("17ft")
    homerun.external_extension_trench_length = Length("14ft")
    project = _project_with_rows(
        [homerun],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("70ft"))])],
    )

    report = VoltageDropService().calculate(project)

    mainline = [
        segment
        for segment in report.segments
        if segment.role.startswith("Homerun")
    ]
    assert mainline
    assert all(segment.material == "Cu" for segment in mainline)
    assert all(segment.length_ft >= 0 for segment in mainline)


def test_lynx_node_adds_source_product_average_and_lynx_average() -> None:
    source = _homerun("A", 0)
    lynx = ProductPreset(
        name="LA",
        product_type="LYNX",
        cluster_wire=_cu("10AWG"),
        branch_cu_wire=_cu("12AWG"),
        branch_al_wire=Wire("6AWG", "Al"),
        branch_length_cu=Length("0ft"),
        branch_length_al=Length("0ft"),
        buffer_length=Length("0ft"),
        row_per_overmold=1,
    )
    project = _project_with_rows(
        [source, lynx],
        [Rackings("R", "A")],
        [
            Row("Row1", [RackingEntry("R", Length("0ft"))]),
            Row("Row2", [RackingEntry("R", Length("0ft"))]),
        ],
        project_type="LYNX",
    )
    project.racking_pitch = Length("10ft")
    project.lynx_preset = "LA"
    project.invs[0].nodes[0].box_position = 0
    report = VoltageDropService().calculate(project)
    lynx_product = next(product for product in report.products if product.product_type == "LYNX")
    expected = 10 * (1.300078 / 1000) * 30
    assert lynx_product.positive_drop == pytest.approx(expected)
    assert lynx_product.negative_drop == pytest.approx(expected)
    assert lynx_product.voltage_drop == pytest.approx(2 * expected)
    assert report.for_object(project.invs[0].nodes[0]).voltage_drop == pytest.approx(2 * expected)


def test_export_contains_auditable_voltage_drop_sheets() -> None:
    preset = _homerun("A", 2)
    project = _project_with_rows(
        [preset],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    generation_result = ProductGenerationService().build_products(project)
    report = VoltageDropService().calculate(project, generation_result)
    with tempfile.TemporaryDirectory() as directory:
        progress: list[tuple[int, int]] = []
        path = export_voltage_drop_to_xlsx(
            report,
            project,
            directory,
            progress_callback=lambda current, total: progress.append((current, total)),
        )
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            assert workbook.sheetnames == [
                "Overview",
                "Hierarchy",
                "Product Instances",
                "Product Summary",
                "Line Segments",
            ]
            instance_row = next(workbook["Product Instances"].iter_rows(min_row=2, values_only=True))
            assert instance_row[8]
            assert instance_row[9]
            assert isinstance(instance_row[13], float)
        finally:
            workbook.close()
        assert progress == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]


def test_missing_resistance_blocks_voltage_drop_export() -> None:
    preset = _homerun("A", 2)
    preset.external_al_wire = Wire("12AWG", "Al")
    preset.external_al_threshold = Length("0ft")
    preset.branch_second_cual_suffix = Length("0ft")
    project = _project_with_rows(
        [preset],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    report = VoltageDropService().calculate(project)
    assert any(issue.code == "missing_resistance" for issue in report.issues)
    issue = next(issue for issue in report.issues if issue.code == "missing_resistance")
    assert issue.source.object_ref is preset
    assert issue.source.primary_field == "external_al_wire"
    translations = TranslationManager.instance()
    previous_locale = translations.locale
    try:
        translations.locale = "zh_CN"
        assert format_voltage_drop_issue(issue) == "缺少 12AWG Al 导线的电阻率数据。"
    finally:
        translations.locale = previous_locale
    with pytest.raises(VoltageDropValidationError):
        report.require_exportable()


def test_structural_and_preset_issues_have_actionable_sources() -> None:
    valid = _project_with_rows(
        [_homerun("A", 1)],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )

    project = Project.from_dict(valid.to_dict())
    project.project_type = ""
    issue = next(i for i in VoltageDropService().calculate(project).issues if i.code == "invalid_project_type")
    assert (issue.source.object_kind, issue.source.primary_field) == ("project", "project_type")

    project = Project.from_dict(valid.to_dict())
    project.invs[0].nodes[0].rows[0].racking_entries[0].rackings = "Missing"
    issue = next(i for i in VoltageDropService().calculate(project).issues if i.code == "missing_rackings")
    assert issue.source.object_ref is project.invs[0].nodes[0].rows[0]
    assert issue.source.primary_field == "racking_entries[0].rackings"

    project = Project.from_dict(valid.to_dict())
    project.racking_pitch = None
    issue = next(i for i in VoltageDropService().calculate(project).issues if i.code == "missing_racking_pitch")
    assert (issue.source.object_kind, issue.source.primary_field) == ("node", "racking_pitch")

    project = Project.from_dict(valid.to_dict())
    project.product_presets[0].branch_lines = []
    issue = next(i for i in VoltageDropService().calculate(project).issues if i.code == "missing_branch_coordinate")
    assert (issue.source.object_kind, issue.source.primary_field) == ("product_preset", "branch_lines")

    project = Project.from_dict(valid.to_dict())
    project.product_presets[0].product_type = "IBEX"
    project.product_presets[0].branch_count = 2
    issue = next(i for i in VoltageDropService().calculate(project).issues if i.code == "invalid_ibex_branches")
    assert issue.source.field_keys == ("branch_count", "branch_lines")

    project = Project.from_dict(valid.to_dict())
    preset = project.product_presets[0]
    preset.product_type = "IBEX"
    preset.branch_count = 3
    preset.branch_lines.append(
        IBEXBranchCoordinate(2, Length("2ft"), Length("2ft"))
    )
    issue = next(i for i in VoltageDropService().calculate(project).issues if i.code == "branch_count_mismatch")
    assert issue.source.primary_field == "branch_count"

    project = Project.from_dict(valid.to_dict())
    project.product_presets[0].pv_panel = ""
    issue = next(i for i in VoltageDropService().calculate(project).issues if i.code == "missing_pv_panel")
    assert issue.source.primary_field == "pv_panel"

    project = Project.from_dict(valid.to_dict())
    project.project_type = "LYNX"
    project.lynx_preset = "Missing"
    issue = next(i for i in VoltageDropService().calculate(project).issues if i.code == "missing_lynx_preset")
    assert issue.source.primary_field == "lynx_preset"

    empty = Project(
        name="Empty",
        project_type="IBEX",
        invs=[Inv("INV1", nodes=[Node("Node1", rows=[Row("Row1")])])],
    )
    issue = next(i for i in VoltageDropService().calculate(empty).issues if i.code == "no_products")
    assert issue.source.object_kind in {"node", "row"}


def test_electrical_issues_point_to_wire_panel_and_length_inputs() -> None:
    project = _project_with_rows(
        [_homerun("A", -1)],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    negative = next(i for i in VoltageDropService().calculate(project).issues if i.code == "negative_length")
    assert negative.source.object_ref is project.product_presets[0]
    assert negative.source.primary_field == "branch_lines[0].coordinate_positive"

    project = _project_with_rows(
        [_homerun("A", 1)],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    project.product_presets[0].external_cu_wire = Wire("none", "Cu")
    missing_wire = next(i for i in VoltageDropService().calculate(project).issues if i.code == "missing_wire")
    assert missing_wire.source.primary_field == "external_cu_wire"

    project = _project_with_rows(
        [_homerun("A", 1)],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    panel = project.pv_panels[0]
    panel.vmp = panel.imp = 0
    panel.module_per_string = 0
    report = VoltageDropService().calculate(project)
    expected = {
        "invalid_vmp": "vmp",
        "invalid_imp": "imp",
        "invalid_module_count": "module_per_string",
    }
    for code, field_name in expected.items():
        issue = next(i for i in report.issues if i.code == code)
        assert issue.source.object_ref is panel
        assert issue.source.primary_field == field_name


def test_invalid_identifier_definition_targets_the_identifier_editor() -> None:
    identifier = LengthIdentifier("S", Length("1Missing"))
    preset = _homerun("A", 1)
    preset.branch_lines[0].coordinate_positive = Length("1S")
    project = _project_with_rows(
        [preset],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    project.length_identifiers = [identifier]
    project.rebind_length_identifiers()

    issue = next(
        i
        for i in VoltageDropService().calculate(project).issues
        if i.code == "invalid_length_expression"
    )
    assert issue.params["detail"] == "Missing"
    assert issue.source.object_kind == "length_identifier"
    assert issue.source.object_ref is identifier
    assert issue.source.primary_field == "length"


def test_lynx_layout_guard_and_missing_code_have_sources() -> None:
    source = _homerun("A", 1)
    lynx = ProductPreset(
        name="LA",
        product_type="LYNX",
        pv_panel="Test Panel",
        cluster_wire=_cu("10AWG"),
        branch_cu_wire=_cu(),
        branch_al_wire=Wire("6AWG", "Al"),
        row_per_overmold=0,
    )
    project = _project_with_rows(
        [source, lynx],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
        project_type="LYNX",
    )
    project.lynx_preset = "LA"
    project.invs[0].nodes[0].box_position = 0
    issue = next(i for i in VoltageDropService().calculate(project).issues if i.code == "invalid_lynx_layout")
    assert issue.source.object_ref is lynx
    assert issue.source.primary_field == "row_per_overmold"

    project = _project_with_rows(
        [_homerun("A", 1)],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    issue = next(
        i
        for i in VoltageDropService().calculate(project, ProductGenerationResult()).issues
        if i.code == "missing_product_code"
    )
    assert issue.source.object_ref is project.product_presets[0]
    assert issue.source.primary_field == "__object__"


def test_missing_rackings_product_definition_targets_rackings_and_propagates() -> None:
    broken_rackings = Rackings("Broken", "Z")
    broken_row = Row("Broken Row", [RackingEntry("Broken", Length("0ft"))])
    project = _project_with_rows([], [broken_rackings], [broken_row])

    report = VoltageDropService().calculate(project)
    issue = next(i for i in report.issues if i.code == "missing_product_preset")

    assert issue.params["names"] == "Z"
    assert issue.source.object_ref is broken_rackings
    assert issue.source.primary_field == "product_presets"
    assert any(i.code == "missing_product_preset" for i in report.for_object(broken_row).issues)
    assert any(
        i.code == "missing_product_preset"
        for i in report.for_object(project.invs[0].nodes[0]).issues
    )
    assert not report.project_level.valid


def test_broken_row_is_not_hidden_by_valid_sibling_row() -> None:
    preset = _homerun("A", 1)
    good = Rackings("Good", "A")
    broken = Rackings("Broken", "Z")
    good_row = Row("Good Row", [RackingEntry("Good", Length("0ft"))])
    broken_row = Row("Broken Row", [RackingEntry("Broken", Length("0ft"))])
    project = _project_with_rows([preset], [good, broken], [good_row, broken_row])

    report = VoltageDropService().calculate(project)

    assert report.for_object(good_row).voltage_drop is not None
    assert any(i.code == "missing_product_preset" for i in report.issues)
    assert not report.project_level.valid


def test_empty_and_ineligible_rackings_have_deepest_actionable_sources() -> None:
    empty_row = Row("Empty", [RackingEntry("", Length("0ft"))])
    project = _project_with_rows([], [], [empty_row])
    issue = VoltageDropService().calculate(project).issues[0]
    assert issue.code == "missing_rackings_selection"
    assert issue.source.object_ref is empty_row
    assert issue.source.primary_field == "racking_entries[0].rackings"

    gap = ProductPreset(name="A", product_type="Gap_Extender")
    rackings = Rackings("Only Gap", "A")
    project = _project_with_rows(
        [gap],
        [rackings],
        [Row("Row1", [RackingEntry("Only Gap", Length("0ft"))])],
    )
    issue = VoltageDropService().calculate(project).issues[0]
    assert issue.code == "no_eligible_products"
    assert issue.source.object_ref is rackings
    assert issue.source.primary_field == "product_presets"


def test_negative_length_sources_are_bound_to_the_actual_segment_operands() -> None:
    wire = _cu()
    source = ProductPreset(
        name="A",
        product_type="IBEX",
        branch_count=2,
        branch_lines=[
            IBEXBranchCoordinate(1, Length("0m"), Length("0m")),
            IBEXBranchCoordinate(2, Length("1m"), Length("1m")),
        ],
        branch_cu_wire=wire,
        external_cu_wire=wire,
        internal_cu_wire=wire,
        branch_length_positive=Length("-1m"),
        branch_length_negative=Length("1m"),
    )
    lynx = ProductPreset(
        name="LA",
        product_type="LYNX",
        cluster_wire=wire,
        branch_cu_wire=wire,
        branch_al_wire=Wire("6AWG", "Al"),
        branch_length_cu=Length("0m"),
        branch_length_al=Length("0m"),
        nearby_overmold_gap=Length("0m"),
        row_per_overmold=1,
    )
    project = _project_with_rows(
        [source, lynx],
        [Rackings("R", "A")],
        [Row("Row1", [RackingEntry("R", Length("-1m"))])],
        project_type="LYNX",
    )
    project.lynx_preset = "LA"
    project.invs[0].nodes[0].box_position = 0
    project.rebind_length_identifiers()

    issues = [
        i for i in VoltageDropService().calculate(project).issues
        if i.code == "negative_length"
    ]
    branch = next(i for i in issues if i.params["role"] == "Branch 1")
    lynx_branch = next(i for i in issues if i.params["role"] == "LYNX Branch")
    assert branch.source.object_ref is source
    assert branch.source.primary_field == "branch_length_positive"
    assert lynx_branch.source.object_kind == "row"
    assert lynx_branch.source.primary_field == "racking_entries[0].offset"


def test_lynx_length_parse_error_keeps_row_offset_source() -> None:
    source = _homerun("A", 0)
    lynx = ProductPreset(
        name="LA",
        product_type="LYNX",
        cluster_wire=_cu("10AWG"),
        branch_cu_wire=_cu(),
        branch_al_wire=Wire("6AWG", "Al"),
        row_per_overmold=1,
    )
    row = Row("Row1", [RackingEntry("R", Length("1Bad"))])
    project = _project_with_rows(
        [source, lynx], [Rackings("R", "A")], [row], project_type="LYNX"
    )
    project.lynx_preset = "LA"
    project.rebind_length_identifiers()

    issues = [
        i for i in VoltageDropService().calculate(project).issues
        if i.code == "invalid_length_expression"
    ]
    assert issues
    assert all(issue.source.object_ref is row for issue in issues)
    assert all(issue.source.primary_field == "racking_entries[0].offset" for issue in issues)

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from count_and_design.application.product_generation import (
    ProductGenerationService,
    build_schematic_variants,
    export_schematics,
)
from count_and_design.models import (
    Connector,
    Fuse,
    Inv,
    Length,
    Node,
    ProductPreset,
    Project,
    PVPanelInfo,
    RackingEntry,
    Rackings,
    Row,
    Wire,
)


SVG = {"svg": "http://www.w3.org/2000/svg"}


def _preset(
    name: str = "A",
    branch_count: int = 3,
    panel_name: str = "Panel X",
) -> ProductPreset:
    connector = Connector(Wire("12AWG", "Cu"), "MC4", "Staubli")
    preset = ProductPreset(
        name=name,
        branch_count=branch_count,
        pv_panel=panel_name,
        product_total_length=Length("20ft"),
        branch_cu_wire=Wire("12AWG", "Cu"),
        branch_al_wire=Wire("6AWG", "Al"),
        external_cu_wire=Wire("12AWG", "Cu"),
        external_al_wire=Wire("6AWG", "Al"),
        internal_cu_wire=Wire("12AWG", "Cu"),
        internal_al_wire=Wire("6AWG", "Al"),
        branch_connector=connector,
        external_connector=connector,
        branch_fuse=Fuse("15A", "Adler"),
        external_fuse=Fuse("15A", "Adler"),
        external_al_threshold=Length("2ft"),
        last_branch_al_threshold=Length("2ft"),
    )
    coordinates = [
        ("5ft", "3ft"),   # POS is at the tail.
        ("2ft", "7ft"),   # NEG is at the tail.
        ("10ft", "10ft"), # Equal: both are at the head.
    ]
    for branch, (positive, negative) in zip(
        preset.branch_lines, coordinates
    ):
        branch.coordinate_positive = Length(positive)
        branch.coordinate_negative = Length(negative)
    return preset


def _project(
    *,
    module_count: int = 12,
    entries: list[RackingEntry] | None = None,
    rackings_name: str = "Rack A",
    preset: ProductPreset | None = None,
) -> Project:
    product_preset = preset or _preset()
    project = Project(
        name="Schematic Demo",
        project_type="IBEX",
        racking_pitch=Length("10ft"),
        product_presets=[product_preset],
        pv_panels=[
            PVPanelInfo(
                name="Panel X",
                module_per_string=module_count,
                vmp=40,
                imp=10,
            )
        ],
        rackings_list=[
            Rackings(name=rackings_name, product_presets=product_preset.name),
            Rackings(name="Unused", product_presets=product_preset.name),
        ],
    )
    row = Row(
        name="Row 1",
        racking_entries=entries
        or [RackingEntry(rackings=rackings_name, offset=Length("0ft"))],
    )
    project.invs = [
        Inv(
            name="INV 1",
            nodes=[Node(name="Node 1", box_position=1, rows=[row])],
        )
    ]
    return project


def _class_elements(root: ET.Element, class_name: str) -> list[ET.Element]:
    return [
        element
        for element in root.iter()
        if class_name in element.attrib.get("class", "").split()
    ]


def test_product_generation_records_concrete_racking_products() -> None:
    project = _project()
    result = ProductGenerationService().build_products(project)

    assert len(result.racking_instances) == 1
    instance = result.racking_instances[0]
    assert instance.rackings_name == "Rack A"
    assert instance.preset_names == ("A",)
    assert len(instance.preset_products) == 1
    assert instance.preset_products[0].positive is not None
    assert instance.preset_products[0].negative is not None


def test_terminal_orientation_uses_each_branch_coordinates() -> None:
    project = _project()
    result = ProductGenerationService().build_products(project)
    variant = build_schematic_variants(result, project)[0]
    strings = variant.sections[0].strings

    assert (strings[0].positive_terminal, strings[0].negative_terminal) == (
        "tail",
        "head",
    )
    assert (strings[1].positive_terminal, strings[1].negative_terminal) == (
        "head",
        "tail",
    )
    assert (strings[2].positive_terminal, strings[2].negative_terminal) == (
        "head",
        "head",
    )


def test_module_count_30_is_expanded_and_31_is_collapsed() -> None:
    for module_count, expected_panels in ((30, 90), (31, 24)):
        project = _project(module_count=module_count)
        result = ProductGenerationService().build_products(project)
        with tempfile.TemporaryDirectory() as directory:
            exported = export_schematics(result, project, directory)
            root = ET.parse(exported.paths[0]).getroot()
            assert len(_class_elements(root, "panel")) == expected_panels
            first_panel = _class_elements(root, "panel")[0]
            assert float(first_panel.attrib["width"]) / float(
                first_panel.attrib["height"]
            ) == 2.0
            strings = _class_elements(root, "pv-string")
            assert all(
                item.attrib["data-module-count"] == str(module_count)
                for item in strings
            )
            if module_count == 31:
                assert len(_class_elements(root, "panel-ellipsis")) == 3
                assert "31" in "".join(root.itertext())


def test_svg_contains_required_product_glyphs_and_wire_metadata() -> None:
    project = _project()
    result = ProductGenerationService().build_products(project)
    with tempfile.TemporaryDirectory() as directory:
        exported = export_schematics(result, project, directory)
        root = ET.parse(exported.paths[0]).getroot()

        assert _class_elements(root, "tee")
        assert _class_elements(root, "male")
        assert _class_elements(root, "female")
        assert _class_elements(root, "fuse")
        assert _class_elements(root, "cu-al")
        assert _class_elements(root, "pos")
        assert _class_elements(root, "neg")
        wires = _class_elements(root, "wire")
        assert wires
        assert all("data-description" in wire.attrib for wire in wires)
        assert all("data-length-ft" in wire.attrib for wire in wires)
        assert "ft" in "".join(root.itertext())
        assert not [
            line
            for line in root.findall(".//svg:line", SVG)
            if "connection" in line.attrib.get("class", "").split()
        ]
        guides = _class_elements(root, "connection-guide")
        assert guides
        assert all(len(guide.findall("svg:line", SVG)) == 2 for guide in guides)


def test_variants_deduplicate_visible_instances_but_keep_length_changes() -> None:
    entries = [
        RackingEntry(rackings="Rack A", offset=Length("0ft")),
        RackingEntry(rackings="Rack A", offset=Length("0ft")),
        RackingEntry(rackings="Rack A", offset=Length("1ft")),
    ]
    project = _project(entries=entries)
    result = ProductGenerationService().build_products(project)
    variants = build_schematic_variants(result, project)

    assert len(result.racking_instances) == 3
    assert len(variants) == 2
    assert len(variants[0].sources) == 2
    assert len(variants[1].sources) == 1
    assert {variant.rackings_name for variant in variants} == {"Rack A"}


def test_multi_preset_racking_includes_ibex_and_homerun_sections() -> None:
    ibex = _preset(name="A", branch_count=2)
    homerun = _preset(name="B", branch_count=1)
    project = Project(
        name="Combined",
        project_type="IBEX",
        racking_pitch=Length("10ft"),
        product_presets=[ibex, homerun],
        pv_panels=[PVPanelInfo(name="Panel X", module_per_string=10)],
        rackings_list=[Rackings(name="Combined Rack", product_presets="AB")],
        invs=[
            Inv(
                name="INV",
                nodes=[
                    Node(
                        name="Node",
                        rows=[
                            Row(
                                name="Row",
                                racking_entries=[
                                    RackingEntry(rackings="Combined Rack")
                                ],
                            )
                        ],
                    )
                ],
            )
        ],
    )
    result = ProductGenerationService().build_products(project)
    variants = build_schematic_variants(result, project)

    assert len(variants) == 1
    assert [section.preset_name for section in variants[0].sections] == [
        "A",
        "B",
    ]
    assert [section.product_type for section in variants[0].sections] == [
        "IBEX",
        "Homerun_Extender",
    ]
    assert [len(section.strings) for section in variants[0].sections] == [2, 1]
    assert variants[0].sections[1].crossed_presets == "A"
    assert variants[0].sections[1].crossed_length_ft == 20.0

    with tempfile.TemporaryDirectory() as directory:
        exported = export_schematics(result, project, directory)
        root = ET.parse(exported.paths[0]).getroot()
        harnesses = _class_elements(root, "harness")
        later = [
            harness
            for harness in harnesses
            if harness.attrib.get("data-preset") == "B"
        ]
        assert len(later) == 2
        assert all(
            harness.attrib["data-crossed-presets"] == "A"
            and harness.attrib["data-crossed-length-ft"] == "20"
            for harness in later
        )
        assert len(
            {harness.attrib["data-external-origin-x"] for harness in harnesses}
        ) == 1


def test_placeholder_is_hidden_but_accumulates_into_following_homerun() -> None:
    placeholder = ProductPreset(
        name="A", branch_count=0, product_total_length=Length("7ft")
    )
    homerun = _preset(name="B", branch_count=1)
    project = Project(
        name="Placeholder",
        project_type="IBEX",
        racking_pitch=Length("0ft"),
        product_presets=[placeholder, homerun],
        pv_panels=[PVPanelInfo(name="Panel X", module_per_string=10)],
        rackings_list=[Rackings(name="Rack", product_presets="AB")],
        invs=[
            Inv(
                name="INV",
                nodes=[
                    Node(
                        name="Node",
                        box_position=1,
                        rows=[
                            Row(
                                name="Row",
                                racking_entries=[RackingEntry(rackings="Rack")],
                            )
                        ],
                    )
                ],
            )
        ],
    )

    result = ProductGenerationService().build_products(project)
    variants = build_schematic_variants(result, project)

    assert placeholder.product_type == "Placeholder"
    assert len(result.homerun_extender_products) == 2
    assert not result.ibex_products
    assert [section.preset_name for section in variants[0].sections] == ["B"]
    assert variants[0].sections[0].crossed_presets == "A"
    assert variants[0].sections[0].crossed_length_ft == 7.0


def test_zero_first_coordinate_reserves_external_component_space() -> None:
    preset = _preset(branch_count=2)
    preset.branch_lines[0].coordinate_positive = Length("0ft")
    preset.branch_lines[0].coordinate_negative = Length("1ft")
    project = _project(preset=preset)
    result = ProductGenerationService().build_products(project)

    with tempfile.TemporaryDirectory() as directory:
        exported = export_schematics(result, project, directory)
        root = ET.parse(exported.paths[0]).getroot()
        positive_harness = next(
            harness
            for harness in _class_elements(root, "harness")
            if "pos" in harness.attrib.get("class", "").split()
        )
        origin = float(positive_harness.attrib["data-external-origin-x"])
        first_terminal = min(
            float(terminal.attrib["cx"])
            for terminal in _class_elements(positive_harness, "terminal")
        )
        assert first_terminal - origin >= 200.0


def test_schematic_draws_strings_horizontally_on_landscape_canvas() -> None:
    project = _project()
    result = ProductGenerationService().build_products(project)

    with tempfile.TemporaryDirectory() as directory:
        exported = export_schematics(result, project, directory)
        root = ET.parse(exported.paths[0]).getroot()
        view_box = [float(value) for value in root.attrib["viewBox"].split()]
        panels = _class_elements(root, "panel")

        assert view_box[2] > view_box[3]
        assert len({panel.attrib["y"] for panel in panels[:12]}) == 1
        assert len({panel.attrib["x"] for panel in panels[:12]}) == 12


def test_missing_panel_produces_placeholder_and_warning() -> None:
    preset = _preset(panel_name="Missing Panel")
    project = _project(preset=preset)
    result = ProductGenerationService().build_products(project)

    with tempfile.TemporaryDirectory() as directory:
        exported = export_schematics(result, project, directory)
        root = ET.parse(exported.paths[0]).getroot()
        assert exported.warnings
        assert len(_class_elements(root, "panel-placeholder")) == 3
        assert _class_elements(root, "warning")


def test_used_racking_without_generated_products_gets_warning_svg() -> None:
    preset = _preset(branch_count=0)
    project = _project(preset=preset)
    project.racking_pitch = None
    result = ProductGenerationService().build_products(project)

    with tempfile.TemporaryDirectory() as directory:
        exported = export_schematics(result, project, directory)
        root = ET.parse(exported.paths[0]).getroot()
        assert exported.variant_count == 1
        assert exported.warnings
        assert _class_elements(root, "warning-line")


def test_only_used_rackings_export_and_managed_cleanup_is_safe() -> None:
    project = _project(rackings_name='Rack <A>:"/\\|?*')
    result = ProductGenerationService().build_products(project)
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory)
        unrelated = output / "keep.svg"
        unrelated.write_text("<svg/>", encoding="utf-8")
        stale = output / "old-generated.svg"
        stale.write_text("<svg/>", encoding="utf-8")
        (output / ".schematics-manifest.json").write_text(
            json.dumps({"version": 1, "files": [stale.name]}),
            encoding="utf-8",
        )

        exported = export_schematics(result, project, directory)

        assert exported.variant_count == 1
        assert "Unused" not in Path(exported.paths[0]).name
        assert not any(char in Path(exported.paths[0]).name for char in '<>:"/\\|?*')
        assert unrelated.exists()
        assert not stale.exists()
        root = ET.parse(exported.paths[0]).getroot()
        title = root.find("svg:title", SVG)
        assert title is not None
        assert title.text == 'Rack <A>:"/\\|?*'


def test_generated_svg_is_valid_for_qt_renderer() -> None:
    app = QApplication.instance() or QApplication([])
    project = _project()
    result = ProductGenerationService().build_products(project)
    with tempfile.TemporaryDirectory() as directory:
        exported = export_schematics(result, project, directory)
        renderer = QSvgRenderer(exported.paths[0])
        assert renderer.isValid()
        assert renderer.defaultSize().width() > 0
        assert renderer.defaultSize().height() > 0
    assert app is not None

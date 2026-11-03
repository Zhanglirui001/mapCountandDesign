import tempfile

import pytest
from openpyxl import load_workbook

from count_and_design.application.product_generation import ProductGenerationResult, ProductGenerationService
from count_and_design.application.product_generation.dedup import (
    _add_assembled_product_with_dedup,
    _assembled_products_equal,
)
from count_and_design.application.product_generation.export import export_to_xlsx
from count_and_design.application.product_generation.homerun_clustering import (
    cluster_homerun_extender_products,
    homerun_total_wire_length,
    homerun_variable_wire_index,
    homerun_variable_wire_length,
)
from count_and_design.application.product_generation.homerun_extender import (
    _make_homerun_extender_product,
)
from count_and_design.application.product_generation.ibex import _make_ibex_external
from count_and_design.application.product_generation.parts_factory import _make_wire_part
from count_and_design.models import (
    AssembledProduct,
    Connector,
    Fuse,
    IBEXBranchCoordinate,
    Inv,
    Length,
    Node,
    Part,
    ProductPreset,
    Project,
    RackingEntry,
    Rackings,
    Row,
    Wire,
)


def _project(project_type: str) -> Project:
    project = Project(
        name="Demo",
        project_type=project_type,
        racking_pitch=Length("10ft"),
        product_presets=[
            ProductPreset(
                name="A",
                product_total_length=Length("5ft"),
                remarks="Product A",
            ),
            ProductPreset(
                name="B",
                product_total_length=Length("7ft"),
                remarks="Product B",
            ),
        ],
        rackings_list=[
            Rackings(name="NW", product_presets="AB"),
            Rackings(name="SE", product_presets="A"),
        ],
    )
    inv = Inv(name="INV1")
    node = Node(name="Node1", box_position=3)
    inv.nodes.append(node)
    project.invs.append(inv)
    return project


def test_products_are_exported_to_workbook() -> None:
    project = _project("IBEX")
    project.invs[0].nodes[0].rows.append(
        Row(name="Row1", racking_entries=[RackingEntry(rackings="NW", offset=Length("1ft"))])
    )
    result = ProductGenerationService().build_products(project)

    with tempfile.TemporaryDirectory() as directory:
        product_path = export_to_xlsx(result, project, f"{directory}/products.xlsx")

        product_wb = load_workbook(product_path, read_only=True)
        try:
            assert "Project" in product_wb.sheetnames
        finally:
            product_wb.close()


def test_export_includes_fuse_certification_and_project_parts_information() -> None:
    project = Project(name="Export Metadata")

    with tempfile.TemporaryDirectory() as directory:
        product_path = export_to_xlsx(
            ProductGenerationResult(), project, f"{directory}/products.xlsx"
        )

        product_wb = load_workbook(product_path, read_only=True)
        try:
            project_sheet = product_wb["Project"]
            assert project_sheet["Q1"].value == "Fuse Certification Standard"
            assert project_sheet["Q2"].value == "ul"

            project_parts_sheet = product_wb["Project Parts Information"]
            assert list(
                project_parts_sheet.iter_rows(
                    min_row=1,
                    max_row=4,
                    min_col=1,
                    max_col=6,
                    values_only=True,
                )
            ) == [
                ("LYNX Plus", None, None, None, None, None),
                ("No.", "Code", "Material Name", "Price", "Qty", "Remark"),
                ("Harness", None, None, None, None, None),
                ("No.", "Code", "Material Name", "Price", "Qty", "Remark"),
            ]
        finally:
            product_wb.close()


@pytest.mark.parametrize(
    ("cluster_length", "front_label_pair_count"),
    [
        pytest.param(Length("0ft"), 1, id="zero-cluster-length"),
        pytest.param(Length("5ft"), 2, id="nonzero-cluster-length"),
    ],
)
def test_lynx_main_line_labels_follow_cluster_length_rules(
    cluster_length: Length,
    front_label_pair_count: int,
) -> None:
    ibex_preset = ProductPreset(
        name="A",
        product_type="IBEX",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                index=1,
                coordinate_positive=Length("10ft"),
                coordinate_negative=Length("10ft"),
            )
        ],
        branch_connector=Connector(
            size=Wire("10AWG", "Cu"),
            type="MC4",
            manufacturer="Staubli",
        ),
    )
    lynx_preset = ProductPreset(
        name="LA",
        product_type="LYNX",
        cluster_wire=Wire("500MCM", "Al"),
        branch_cu_wire=Wire("10AWG", "Cu"),
        branch_al_wire=Wire("none", "Al"),
        external_connector=Connector(
            size=Wire("10AWG", "Cu"),
            type="MC4",
            manufacturer="Staubli",
        ),
    )
    project = Project(
        name="LYNX labels",
        project_type="LYNX",
        racking_pitch=Length("10ft"),
        lynx_preset="LA",
        product_presets=[ibex_preset, lynx_preset],
        rackings_list=[Rackings(name="NW", product_presets="A")],
    )
    node = Node(
        name="Node1",
        box_position=0,
        cluster_length=cluster_length,
    )
    for row_number in (1, 2):
        node.rows.append(
            Row(
                name=f"Row{row_number}",
                racking_entries=[
                    RackingEntry(rackings="NW", offset=Length("1ft"))
                ],
            )
        )
    project.invs.append(Inv(name="INV1", nodes=[node]))

    result = ProductGenerationService().build_products(project)

    assert len(result.lynx_products) == 2
    for product in result.lynx_products:
        assert product.parts[0].category == "Wire"

        first_overmold_index = 1 + front_label_pair_count * 2
        assert product.parts[first_overmold_index].semi_or_not
        assert product.parts[first_overmold_index + 1].category == "Wire"
        assert product.parts[-1].semi_or_not

        label_positions = list(range(1, first_overmold_index))
        label_positions.extend([len(product.parts) - 3, len(product.parts) - 2])
        assert len(label_positions) == (front_label_pair_count + 1) * 2

        for index in label_positions[::2]:
            label_pair = product.parts[index:index + 2]
            assert [part.cad_parts_no for part in label_pair] == [
                "Cable Label Up",
                "Cable Label Down",
            ]
            assert [part.description for part in label_pair] == [
                "Label(Product Type Label)",
                "Label(Circuit Label)",
            ]
            assert all(part.category == "Label" for part in label_pair)


# ---------------------------------------------------------------------------
# 合并去重：预设来源（preset_name）必须相同
# ---------------------------------------------------------------------------


def _wire_part(length: float, color: str = "Red") -> Part:
    polarity_word = "Pos" if color == "Red" else "Neg"
    return Part(
        category="Wire",
        cad_parts_no=f"Wire {polarity_word} 11-35ft",
        cad_parts_length=140.0,
        description=f"8AWG {color} PV Wire 2KV 66 Strands Cu",
        unit="ft",
        length=length,
    )


def _assembled_product(preset_name: str, polarity: str = "POS") -> AssembledProduct:
    product = AssembledProduct(polarity=polarity, preset_name=preset_name)
    product.parts.append(
        _wire_part(31.0, "Red" if polarity == "POS" else "Black")
    )
    return product


def test_assembled_products_equal_requires_same_preset() -> None:
    same_preset_a = _assembled_product("A")
    same_preset_b = _assembled_product("A")
    assert _assembled_products_equal(same_preset_a, same_preset_b)

    other_preset = _assembled_product("B")
    assert not _assembled_products_equal(same_preset_a, other_preset)

    other_polarity = _assembled_product("A", polarity="NEG")
    assert not _assembled_products_equal(same_preset_a, other_polarity)


def test_dedup_merges_only_within_same_preset() -> None:
    products: list[AssembledProduct] = []
    _add_assembled_product_with_dedup(products, _assembled_product("A"))
    # 同一预设、结构相同 → 合并（quantity 累加）
    _add_assembled_product_with_dedup(products, _assembled_product("A"))
    # 不同预设、结构相同 → 不合并，各自保留
    _add_assembled_product_with_dedup(products, _assembled_product("B"))
    _add_assembled_product_with_dedup(products, _assembled_product("B"))

    assert len(products) == 2
    assert {p.preset_name for p in products} == {"A", "B"}
    assert all(p.quantity == 2 for p in products)


# ---------------------------------------------------------------------------
# 浮点精度误差：长度噪声不应阻止产品合并
# ---------------------------------------------------------------------------


def test_assembled_products_equal_tolerates_float_noise() -> None:
    exact = _assembled_product("A")
    exact.parts[0].length = 535.0
    noisy = _assembled_product("A")
    noisy.parts[0].length = 535.0000000000001

    assert _assembled_products_equal(exact, noisy)

    # 超出浮点噪声范围的真实长度差异仍应判为不同产品
    different = _assembled_product("A")
    different.parts[0].length = 535.5
    assert not _assembled_products_equal(exact, different)


def test_dedup_merges_products_with_float_noise_lengths() -> None:
    products: list[AssembledProduct] = []
    first = _assembled_product("A")
    first.parts[0].length = 535.0
    second = _assembled_product("A")
    second.parts[0].length = 535.0000000000001

    _add_assembled_product_with_dedup(products, first)
    _add_assembled_product_with_dedup(products, second)

    assert len(products) == 1
    assert products[0].quantity == 2


def test_wire_part_length_is_normalized_against_float_noise() -> None:
    noisy = _make_wire_part(
        [], Wire("8AWG", "Cu"), "normal", "Red",
        Length("163068.00000000003mm"),
    )
    exact = _make_wire_part(
        [], Wire("8AWG", "Cu"), "normal", "Red", Length("163068mm"),
    )
    assert noisy.length == 535.0
    assert exact.length == 535.0


def test_homerun_extender_wire_uses_extender_cad_parts_number() -> None:
    parts: list[Part] = []
    positive = _make_wire_part(
        parts,
        Wire("6AWG", "Al", "7"),
        "Homerun_Extender",
        "Red",
        Length("20ft"),
    )
    negative = _make_wire_part(
        parts,
        Wire("6AWG", "Al", "7"),
        "Homerun_Extender",
        "Black",
        Length("20ft"),
    )

    assert positive.code.startswith("EP-")
    assert positive.cad_parts_no == "extender-POS"
    assert negative.code.startswith("EN-")
    assert negative.cad_parts_no == "extender-NEG"


def _identical_ibex_presets() -> tuple[ProductPreset, ProductPreset]:
    def make(name: str) -> ProductPreset:
        return ProductPreset(
            name=name,
            product_type="IBEX",
            branch_count=3,
            branch_lines=[
                IBEXBranchCoordinate(
                    index=i,
                    coordinate_positive=Length("10ft"),
                    coordinate_negative=Length("10ft"),
                )
                for i in range(1, 4)
            ],
            branch_connector=Connector(Wire("10AWG", "Cu"), "MC4", "Staubli"),
            external_connector=Connector(Wire("10AWG", "Cu"), "MC4", "Staubli"),
            branch_cu_wire=Wire("10AWG", "Cu"),
            external_cu_wire=Wire("10AWG", "Cu"),
        )

    return make("A"), make("B")


def test_ibex_products_from_different_presets_are_not_merged() -> None:
    preset_a, preset_b = _identical_ibex_presets()
    project = Project(
        name="Demo",
        project_type="IBEX",
        racking_pitch=Length("10ft"),
        product_presets=[preset_a, preset_b],
        rackings_list=[
            Rackings(name="RA", product_presets="A"),
            Rackings(name="RB", product_presets="B"),
        ],
    )
    inv = Inv(name="INV1")
    node = Node(name="Node1", box_position=3)
    inv.nodes.append(node)
    project.invs.append(inv)
    node.rows.append(
        Row(
            name="Row1",
            racking_entries=[
                RackingEntry(rackings="RA", offset=Length("1ft")),
                RackingEntry(rackings="RB", offset=Length("1ft")),
            ],
        )
    )

    result = ProductGenerationService().build_products(project)

    # 两个预设生成的结构完全相同的产品必须各自保留（A-POS/A-NEG/B-POS/B-NEG）
    assert len(result.ibex_products) == 4
    assert {p.preset_name for p in result.ibex_products} == {"A", "B"}
    assert all(p.quantity == 1 for p in result.ibex_products)

    # 正极产品结构确实完全相同，仅预设来源不同
    pos_a = next(
        p for p in result.ibex_products
        if p.preset_name == "A" and p.polarity == "POS"
    )
    pos_b = next(
        p for p in result.ibex_products
        if p.preset_name == "B" and p.polarity == "POS"
    )
    assert [p.category for p in pos_a.parts] == [p.category for p in pos_b.parts]
    assert pos_a.description == pos_b.description


def test_ibex_products_from_same_preset_still_merge() -> None:
    (preset_a, _preset_b) = _identical_ibex_presets()
    project = Project(
        name="Demo",
        project_type="IBEX",
        racking_pitch=Length("10ft"),
        product_presets=[preset_a],
        rackings_list=[Rackings(name="RA", product_presets="A")],
    )
    inv = Inv(name="INV1")
    node = Node(name="Node1", box_position=3)
    inv.nodes.append(node)
    project.invs.append(inv)
    node.rows.append(
        Row(
            name="Row1",
            racking_entries=[
                RackingEntry(rackings="RA", offset=Length("1ft")),
                RackingEntry(rackings="RA", offset=Length("1ft")),
            ],
        )
    )

    result = ProductGenerationService().build_products(project)

    # 同一预设的两次相同出现仍合并为一条，quantity 累加
    assert len(result.ibex_products) == 2  # POS + NEG
    assert {p.polarity for p in result.ibex_products} == {"POS", "NEG"}
    assert all(p.quantity == 2 for p in result.ibex_products)


def test_all_product_sheets_keep_legacy_header_with_pv_panel_values() -> None:
    project = Project(name="Panel Export")
    result = ProductGenerationResult()
    result.ibex_products.append(
        AssembledProduct(code="IBEX-TEST", polarity="POS", pv_panel="IBEX Panel")
    )
    result.homerun_extender_products.append(
        AssembledProduct(code="HOME-TEST", polarity="NEG", pv_panel="Homerun Panel")
    )
    result.lynx_products.append(
        AssembledProduct(code="LYNX-TEST", polarity="POS", pv_panel="LYNX Panel")
    )
    gap_product = AssembledProduct(
        code="GAP-TEST", polarity="NEG", pv_panel="Gap Panel"
    )
    gap_product._gap_extender_preset = ProductPreset(  # type: ignore[attr-defined]
        name="GA", product_type="Gap_Extender"
    )
    result.gap_extender_products.append(gap_product)

    serialized = result.ibex_products[0].to_dict()
    assert serialized["pv_panel"] == "IBEX Panel"
    assert "solaris_edition" not in serialized
    assert AssembledProduct.from_dict(serialized).pv_panel == "IBEX Panel"

    with tempfile.TemporaryDirectory() as directory:
        progress: list[tuple[int, int]] = []
        product_path = export_to_xlsx(
            result,
            project,
            f"{directory}/products.xlsx",
            progress_callback=lambda current, total: progress.append((current, total)),
        )
        workbook = load_workbook(product_path, read_only=True)
        try:
            product_sheets = [
                sheet
                for sheet in workbook.worksheets
                if sheet.cell(row=1, column=9).value == "Solaris Edition"
            ]
            assert {sheet.cell(row=2, column=9).value for sheet in product_sheets} == {
                "IBEX Panel",
                "LYNX Panel",
                "Gap Panel",
                "Homerun Panel",
            }
        finally:
            workbook.close()

        assert progress == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_homerun_extender_class_sheet_type_and_quantity_is_blank() -> None:
    """Homerun 类别行/分表留空数量；Type 仍按铝线使用情况区分。"""
    project = Project(name="Homerun Export")
    result = ProductGenerationResult()
    cu_product = AssembledProduct(code="HOME-CU", polarity="NEG", quantity=3)
    al_product = AssembledProduct(code="HOME-AL", polarity="POS", quantity=5)
    al_product.uses_aluminum_wire = True
    result.homerun_extender_products.extend([cu_product, al_product])

    with tempfile.TemporaryDirectory() as directory:
        product_path = export_to_xlsx(result, project, f"{directory}/products.xlsx")
        workbook = load_workbook(product_path, read_only=True)
        try:
            # Products info 主表按类别列出；各变体数量改在长度明细表中记录。
            products_info = workbook["Products info"]
            info_rows = {
                products_info.cell(row=row, column=1).value: (
                    products_info.cell(row=row, column=2).value,
                    products_info.cell(row=row, column=3).value,
                )
                for row in range(2, products_info.max_row + 1)
                if products_info.cell(row=row, column=1).value
            }
            assert info_rows["HOME-CU"] == ("CUEX", None)
            assert info_rows["HOME-AL"] == ("ALEX", None)

            # 类别分表：类型按铝线使用情况判定，数量留空。
            cu_sheet = workbook["HOME-CU"]
            assert cu_sheet.cell(row=2, column=2).value == "CUEX"
            assert cu_sheet.cell(row=2, column=3).value is None

            al_sheet = workbook["HOME-AL"]
            assert al_sheet.cell(row=2, column=2).value == "ALEX"
            assert al_sheet.cell(row=2, column=3).value is None

            wire_sheet = workbook["Wire Length & Quantities"]
            assert wire_sheet.cell(row=2, column=5).value == 3
            assert wire_sheet.cell(row=3, column=5).value == 5
        finally:
            workbook.close()


def _homerun_cu_variant(
    target_length: float,
    *,
    quantity: int,
    preset_name: str = "A",
) -> AssembledProduct:
    product = AssembledProduct(
        code="EA",
        polarity="POS",
        quantity=quantity,
        preset_name=preset_name,
        product_type="Homerun_Extender",
        description="Homerun Extender",
    )
    product.parts = [
        Part(
            code="B-1",
            category="Wire",
            cad_parts_no="Wire Pos 1-10ft",
            cad_parts_length=120,
            description="10AWG Red PV Wire 2KV 75 Strands Cu",
            unit="ft",
            length=10,
        ),
        Part(
            code="L-1",
            category="Label",
            cad_parts_no="Harness Label Crosswise Left",
            description="Label(Product Type Label)",
            unit="pcs",
        ),
        Part(
            code="B-2",
            category="Wire",
            cad_parts_no=(
                "Wire Pos 1-10ft" if target_length <= 10 else "Wire Pos 11-35ft"
            ),
            cad_parts_length=120 if target_length <= 10 else 140,
            description="10AWG Red PV Wire 2KV 75 Strands Cu",
            unit="ft",
            length=target_length,
        ),
        Part(
            code="L-2",
            category="Label",
            cad_parts_no="Harness Label Crosswise Right",
            description="Label(Circuit Label)",
            unit="pcs",
        ),
    ]
    return product


def test_homerun_classes_ignore_only_target_mainline_length() -> None:
    short = _homerun_cu_variant(1, quantity=2)
    long = _homerun_cu_variant(21, quantity=3)
    other_preset = _homerun_cu_variant(21, quantity=4, preset_name="B")

    classes = cluster_homerun_extender_products([short, long, other_preset])

    assert [len(item.products) for item in classes] == [2, 1]
    assert homerun_variable_wire_length(short) == 1
    assert homerun_variable_wire_length(long) == 21
    assert homerun_total_wire_length(short) == 11
    assert homerun_total_wire_length(long) == 31


def test_homerun_variable_length_targets_post_fuse_copper_wire() -> None:
    preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                index=1,
                coordinate_positive=Length("20ft"),
                coordinate_negative=Length("20ft"),
            )
        ],
        external_cu_wire=Wire("10AWG", "Cu", "75"),
        external_al_wire=Wire("none", "Al", "none"),
        external_fuse=Fuse("15A", "Adler"),
        external_cu_length_to_row_head=Length("10ft"),
    )

    product, _negative = _make_homerun_extender_product(
        [], preset, Length("0ft"), Length("0ft")
    )
    assert product is not None

    assert homerun_variable_wire_length(product) == 20
    assert homerun_total_wire_length(product) == 30
    variable_index = homerun_variable_wire_index(product)
    assert variable_index is not None
    variable_part = product.parts[variable_index]
    assert variable_part.code.startswith("EP-")
    assert variable_part.cad_parts_no == "extender-POS"


def test_homerun_pure_copper_mainlines_use_extender_wire_codes() -> None:
    preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                index=1,
                coordinate_positive=Length("20ft"),
                coordinate_negative=Length("21ft"),
            )
        ],
        external_cu_wire=Wire("10AWG", "Cu", "75"),
        external_al_wire=Wire("none", "Al", "none"),
        external_cu_length_to_row_head=Length("10ft"),
    )

    positive, negative = _make_homerun_extender_product(
        [], preset, Length("0ft"), Length("0ft")
    )

    for product, expected_prefix, expected_cad_parts_no in (
        (positive, "EP-", "extender-POS"),
        (negative, "EN-", "extender-NEG"),
    ):
        assert product is not None
        variable_index = homerun_variable_wire_index(product)
        assert variable_index is not None
        variable_part = product.parts[variable_index]
        assert variable_part.code.startswith(expected_prefix)
        assert variable_part.cad_parts_no == expected_cad_parts_no


def test_homerun_unfused_pure_copper_merges_whip_and_mainline_before_left_label() -> None:
    connector = Connector(Wire("8AWG", "Cu"), "MC4", "Staubli")
    preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                index=1,
                coordinate_positive=Length("70ft"),
                coordinate_negative=Length("70ft"),
            )
        ],
        branch_connector=connector,
        external_connector=connector,
        external_cu_wire=Wire("8AWG", "Cu"),
        external_al_wire=Wire("none", "Al"),
        external_cu_length_to_row_head=Length("17ft"),
        external_extension_trench_length=Length("14ft"),
    )

    positive, negative = _make_homerun_extender_product(
        [], preset, Length("0ft"), Length("0ft")
    )

    for product, expected_prefix, expected_cad_parts_no in (
        (positive, "EP-", "extender-POS"),
        (negative, "EN-", "extender-NEG"),
    ):
        assert product is not None
        assert [part.category for part in product.parts] == [
            "Connect",
            "Wire",
            "Label",
            "Label",
            "Connect",
        ]
        combined_wire = product.parts[1]
        assert combined_wire.length == pytest.approx(101.0)
        assert combined_wire.code.startswith(expected_prefix)
        assert combined_wire.cad_parts_no == expected_cad_parts_no
        assert product.parts[2].cad_parts_no == "Harness Label Crosswise Left"
        assert homerun_variable_wire_index(product) == 1
        assert homerun_variable_wire_length(product) == pytest.approx(101.0)


def test_homerun_negative_pure_copper_merges_even_when_positive_has_fuse() -> None:
    preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                index=1,
                coordinate_positive=Length("20ft"),
                coordinate_negative=Length("21ft"),
            )
        ],
        external_cu_wire=Wire("10AWG", "Cu", "75"),
        external_al_wire=Wire("none", "Al", "none"),
        external_fuse=Fuse("15A", "Adler"),
        external_cu_length_to_row_head=Length("10ft"),
    )

    positive, negative = _make_homerun_extender_product(
        [], preset, Length("0ft"), Length("0ft")
    )
    assert positive is not None
    assert negative is not None

    positive_wires = [part for part in positive.parts if part.category == "Wire"]
    assert [part.length for part in positive_wires] == pytest.approx([10.0, 20.0])
    assert positive_wires[0].code.startswith("B-")
    assert positive_wires[1].code.startswith("EP-")

    negative_wires = [part for part in negative.parts if part.category == "Wire"]
    assert len(negative_wires) == 1
    assert negative_wires[0].length == pytest.approx(31.0)
    assert negative_wires[0].code.startswith("EN-")
    left_label_index = next(
        index
        for index, part in enumerate(negative.parts)
        if part.cad_parts_no == "Harness Label Crosswise Left"
    )
    assert negative.parts.index(negative_wires[0]) < left_label_index


def test_homerun_variable_length_targets_aluminum_between_converters() -> None:
    preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                index=1,
                coordinate_positive=Length("20ft"),
                coordinate_negative=Length("20ft"),
            )
        ],
        external_cu_wire=Wire("10AWG", "Cu", "75"),
        external_al_wire=Wire("6AWG", "Al", "7"),
        external_al_threshold=Length("1ft"),
        branch_second_cual_suffix=Length("3ft"),
        external_cu_length_to_row_head=Length("10ft"),
    )

    product, _negative = _make_homerun_extender_product(
        [], preset, Length("0ft"), Length("0ft")
    )
    assert product is not None

    assert homerun_variable_wire_length(product) == 17
    assert homerun_total_wire_length(product) == 30
    variable_index = homerun_variable_wire_index(product)
    assert variable_index is not None
    variable_part = product.parts[variable_index]
    assert variable_part.code.startswith("EP-")
    assert variable_part.cad_parts_no == "extender-POS"


def test_homerun_variable_length_falls_back_to_whip_without_mainline() -> None:
    first = _homerun_cu_variant(1, quantity=2)
    second = _homerun_cu_variant(1, quantity=3)
    first.parts.pop(2)
    second.parts.pop(2)
    second.parts[0].length = 20
    second.parts[0].cad_parts_no = "Wire Pos 11-35ft"
    second.parts[0].cad_parts_length = 140

    classes = cluster_homerun_extender_products([first, second])

    assert len(classes) == 1
    assert homerun_variable_wire_length(first) == 10
    assert homerun_variable_wire_length(second) == 20
    assert homerun_total_wire_length(first) == 10
    assert homerun_total_wire_length(second) == 20


def test_homerun_whip_fallback_is_length_placeholder_in_class_sheet() -> None:
    project = Project(name="Homerun Whip Fallback")
    result = ProductGenerationResult()
    product = _homerun_cu_variant(1, quantity=2)
    product.parts.pop(2)
    result.homerun_extender_products.append(product)

    with tempfile.TemporaryDirectory() as directory:
        product_path = export_to_xlsx(
            result,
            project,
            f"{directory}/products.xlsx",
        )
        workbook = load_workbook(product_path, read_only=True)
        try:
            wire_sheet = workbook["Wire Length & Quantities"]
            assert wire_sheet["C2"].value == 10
            assert wire_sheet["D2"].value == 10

            class_sheet = workbook["EA"]
            assert class_sheet["C4"].value == "LENGTH"
        finally:
            workbook.close()


def test_homerun_class_export_writes_variants_and_one_representative_sheet() -> None:
    project = Project(name="Homerun Classes")
    result = ProductGenerationResult()
    result.homerun_extender_products.extend([
        _homerun_cu_variant(1, quantity=2),
        _homerun_cu_variant(21, quantity=3),
    ])

    with tempfile.TemporaryDirectory() as directory:
        progress: list[tuple[int, int]] = []
        product_path = export_to_xlsx(
            result,
            project,
            f"{directory}/products.xlsx",
            progress_callback=lambda current, total: progress.append((current, total)),
        )
        workbook = load_workbook(product_path)
        try:
            wire_sheet = workbook["Wire Length & Quantities"]
            assert "A2:A3" in {str(cell_range) for cell_range in wire_sheet.merged_cells.ranges}
            assert [wire_sheet.cell(row=row, column=1).value for row in (2, 3)] == ["EA", None]
            assert [wire_sheet.cell(row=row, column=2).value for row in (2, 3)] == [1, 2]
            assert [wire_sheet.cell(row=row, column=3).value for row in (2, 3)] == [1, 21]
            assert [wire_sheet.cell(row=row, column=4).value for row in (2, 3)] == [11, 31]
            assert [wire_sheet.cell(row=row, column=5).value for row in (2, 3)] == [2, 3]
            assert [wire_sheet.cell(row=row, column=6).value for row in (2, 3)] == [None, None]
            assert [wire_sheet.cell(row=row, column=7).value for row in (2, 3)] == [None, None]

            class_sheet = workbook["EA"]
            assert class_sheet["A2"].value == "EA"
            assert class_sheet["C2"].value is None
            assert class_sheet["C4"].value == 10
            assert class_sheet["C6"].value == "LENGTH"
            assert workbook.sheetnames.count("EA") == 1
        finally:
            workbook.close()

    assert progress == [(1, 1)]


def test_product_generation_assigns_one_code_per_homerun_length_class() -> None:
    placeholder = ProductPreset(
        name="X",
        branch_count=0,
        product_total_length=Length("20ft"),
    )
    homerun = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                index=1,
                coordinate_positive=Length("1ft"),
                coordinate_negative=Length("2ft"),
            )
        ],
        external_cu_wire=Wire("10AWG", "Cu", "75"),
        external_al_wire=Wire("none", "Al", "none"),
    )
    project = Project(
        name="Homerun Naming",
        racking_pitch=Length("10ft"),
        product_presets=[placeholder, homerun],
        rackings_list=[
            Rackings(name="Direct", product_presets="A"),
            Rackings(name="Extended", product_presets="XA"),
        ],
    )
    inv = Inv(name="INV1")
    node = Node(name="Node1", box_position=1)
    node.rows.append(
        Row(
            name="Row1",
            racking_entries=[
                RackingEntry(rackings="Direct"),
                RackingEntry(rackings="Extended"),
            ],
        )
    )
    inv.nodes.append(node)
    project.invs.append(inv)

    result = ProductGenerationService().build_products(project)
    classes = cluster_homerun_extender_products(result.homerun_extender_products)

    assert len(result.homerun_extender_products) == 4
    assert len(classes) == 2  # POS 与 NEG 各一类
    assert [item.class_name for item in classes] == ["EA", "EB"]
    assert [len(item.products) for item in classes] == [2, 2]
    assert {product.code for product in result.all_products} == {"EA", "EB"}


def test_clustered_lynx_sheets_are_included_in_export_progress() -> None:
    project = Project(name="LYNX Progress")
    result = ProductGenerationResult()
    result.lynx_products.extend([
        AssembledProduct(code="LYNX-1", polarity="POS"),
        AssembledProduct(code="LYNX-2", polarity="POS"),
    ])
    progress: list[tuple[int, int]] = []

    with tempfile.TemporaryDirectory() as directory:
        export_to_xlsx(
            result,
            project,
            f"{directory}/products.xlsx",
            progress_callback=lambda current, total: progress.append((current, total)),
        )

    assert progress == [(1, 1)]


def test_unfused_ibex_external_aluminum_includes_first_branch_coordinate() -> None:
    preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                index=1,
                coordinate_positive=Length("120ft"),
                coordinate_negative=Length("130ft"),
            )
        ],
        external_cu_wire=Wire("8AWG", "Cu", "66"),
        external_al_wire=Wire("6AWG", "Al", "63"),
        external_al_threshold=Length("100ft"),
        external_second_cual_suffix=Length("1ft"),
        external_extension_trench_length=Length("2ft"),
    )
    positive = AssembledProduct(polarity="POS")
    negative = AssembledProduct(polarity="NEG")

    _make_ibex_external(
        [],
        preset,
        whip_length=Length("0ft"),
        extender_length=Length("0ft"),
        pos_product=positive,
        neg_product=negative,
    )

    positive_aluminum = next(
        part
        for part in positive.parts
        if part.category == "Wire" and "Strands Al" in part.description
    )
    negative_aluminum = next(
        part
        for part in negative.parts
        if part.category == "Wire" and "Strands Al" in part.description
    )

    assert positive_aluminum.length == pytest.approx(119.0)
    assert negative_aluminum.length == pytest.approx(129.0)
    assert sum(
        part.length for part in positive.parts if part.category == "Wire"
    ) == pytest.approx(125.0)
    assert sum(
        part.length for part in negative.parts if part.category == "Wire"
    ) == pytest.approx(135.0)


def test_ibex_external_copper_prefix_does_not_trigger_aluminum_switch() -> None:
    preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                index=1,
                coordinate_positive=Length("0ft"),
                coordinate_negative=Length("0ft"),
            )
        ],
        external_cu_wire=Wire("8AWG", "Cu", "66"),
        external_al_wire=Wire("6AWG", "Al", "63"),
        external_al_threshold=Length("100ft"),
        external_second_cual_suffix=Length("1ft"),
        external_cu_length_to_row_head=Length("17ft"),
        external_extension_trench_length=Length("14ft"),
    )
    positive = AssembledProduct(polarity="POS")
    negative = AssembledProduct(polarity="NEG")

    _make_ibex_external(
        [],
        preset,
        whip_length=Length("70ft"),
        extender_length=Length("0ft"),
        pos_product=positive,
        neg_product=negative,
    )

    for product in (positive, negative):
        wires = [part for part in product.parts if part.category == "Wire"]
        assert not product.uses_aluminum_wire
        assert all(part.description.endswith("Cu") for part in wires)
        assert [part.length for part in wires] == pytest.approx([101.0])


def test_fused_ibex_external_negative_aluminum_preserves_total_length() -> None:
    preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                index=1,
                coordinate_positive=Length("120ft"),
                coordinate_negative=Length("130ft"),
            )
        ],
        external_cu_wire=Wire("8AWG", "Cu", "66"),
        external_al_wire=Wire("6AWG", "Al", "63"),
        external_fuse=Fuse("20A", "Adler"),
        external_al_threshold=Length("100ft"),
        external_second_cual_suffix=Length("1ft"),
        external_cu_length_to_row_head=Length("3ft"),
        external_extension_trench_length=Length("2ft"),
    )
    positive = AssembledProduct(polarity="POS")
    negative = AssembledProduct(polarity="NEG")

    _make_ibex_external(
        [], preset, Length("0ft"), Length("0ft"), positive, negative,
    )

    negative_wires = [
        part for part in negative.parts if part.category == "Wire"
    ]
    negative_aluminum = next(
        part for part in negative_wires if part.description.endswith("Al")
    )
    assert negative_aluminum.length == pytest.approx(129.0)
    assert sum(part.length for part in negative_wires) == pytest.approx(135.0)


@pytest.mark.parametrize("use_aluminum", [False, True])
@pytest.mark.parametrize("has_fuse", [False, True])
@pytest.mark.parametrize("has_branch_connector", [False, True])
def test_homerun_always_places_second_label_before_branch_connector(
    use_aluminum: bool,
    has_fuse: bool,
    has_branch_connector: bool,
) -> None:
    none_connector = Connector(Wire("none", "Cu"), "none", "none")
    branch_connector = (
        Connector(Wire("8AWG", "Cu"), "MC4", "Staubli")
        if has_branch_connector
        else none_connector
    )
    preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(1, Length("20ft"), Length("22ft"))
        ],
        branch_connector=branch_connector,
        external_cu_wire=Wire("8AWG", "Cu"),
        external_al_wire=Wire("6AWG", "Al"),
        external_al_threshold=Length("10ft" if use_aluminum else "100ft"),
        external_fuse=Fuse("20A" if has_fuse else "none", "Adler"),
        external_cu_length_to_row_head=Length("0ft"),
        external_extension_trench_length=Length("0ft"),
        branch_second_cual_suffix=Length("2ft"),
    )

    positive, negative = _make_homerun_extender_product(
        [], preset, Length("0ft"), Length("0ft")
    )

    for product in (positive, negative):
        assert product is not None
        labels = [part for part in product.parts if part.category == "Label"]
        assert [part.description for part in labels] == [
            "Label(Product Type Label)",
            "Label(Circuit Label)",
        ]
        second_label_index = product.parts.index(labels[1])
        if has_branch_connector:
            assert second_label_index == len(product.parts) - 2
            assert product.parts[-1].category == "Connect"
        else:
            assert second_label_index == len(product.parts) - 1

    if use_aluminum:
        assert negative is not None
        negative_aluminum = next(
            part
            for part in negative.parts
            if part.category == "Wire" and part.description.endswith("Al")
        )
        # NEG Homerun: L = 22ft, S = 2ft, so the aluminum segment is L - S.
        assert negative_aluminum.length == pytest.approx(20.0)


@pytest.mark.parametrize("mainline_length", ["0ft", "-1ft"])
def test_homerun_omits_nonpositive_mainline_parts(
    mainline_length: str,
) -> None:
    preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                1,
                Length(mainline_length),
                Length(mainline_length),
            )
        ],
        branch_connector=Connector(Wire("8AWG", "Cu"), "MC4", "Staubli"),
        external_cu_wire=Wire("8AWG", "Cu"),
        external_al_wire=Wire("6AWG", "Al"),
        # A zero threshold verifies that the nonpositive-length guard takes
        # precedence over the otherwise true aluminum-switch condition.
        external_al_threshold=Length("0ft"),
        external_cu_length_to_row_head=Length("17ft"),
        external_extension_trench_length=Length("14ft"),
        branch_second_cual_suffix=Length("2ft"),
    )

    positive, negative = _make_homerun_extender_product(
        [], preset, Length("0ft"), Length("0ft")
    )

    for product, expected_code, expected_cad_parts_no in (
        (positive, "EP-", "extender-POS"),
        (negative, "EN-", "extender-NEG"),
    ):
        assert product is not None
        wires = [part for part in product.parts if part.category == "Wire"]
        assert [part.length for part in wires] == pytest.approx([31.0])
        assert wires[0].code.startswith(expected_code)
        assert wires[0].cad_parts_no == expected_cad_parts_no
        assert not any(part.category == "AC" for part in product.parts)
        assert not product.uses_aluminum_wire
        assert [part.category for part in product.parts[-2:]] == [
            "Label",
            "Connect",
        ]


def test_homerun_without_mainline_moves_fuse_suffix_out_of_prefix() -> None:
    preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(1, Length("0ft"), Length("0ft"))
        ],
        branch_connector=Connector(Wire("8AWG", "Cu"), "MC4", "Staubli"),
        external_cu_wire=Wire("8AWG", "Cu"),
        external_al_wire=Wire("6AWG", "Al"),
        external_al_threshold=Length("0ft"),
        external_fuse=Fuse("20A", "Adler"),
        external_fuse_suffix=Length("3ft"),
        external_cu_length_to_row_head=Length("17ft"),
        external_extension_trench_length=Length("14ft"),
    )

    positive, negative = _make_homerun_extender_product(
        [], preset, Length("0ft"), Length("0ft")
    )

    assert positive is not None
    assert negative is not None
    positive_wires = [
        part for part in positive.parts if part.category == "Wire"
    ]
    negative_wires = [
        part for part in negative.parts if part.category == "Wire"
    ]
    assert [part.length for part in positive_wires] == pytest.approx([28.0, 3.0])
    assert [part.length for part in negative_wires] == pytest.approx([31.0])
    assert positive_wires[1].code.startswith("EP-")
    assert positive_wires[1].cad_parts_no == "extender-POS"
    assert negative_wires[0].code.startswith("EN-")
    assert negative_wires[0].cad_parts_no == "extender-NEG"
    assert sum(part.length for part in positive_wires) == pytest.approx(31.0)

    fuse_index = next(
        index
        for index, part in enumerate(positive.parts)
        if part.category == "Fuse"
    )
    assert positive.parts[fuse_index + 1].category == "Wire"
    assert positive.parts[fuse_index + 1].length == pytest.approx(3.0)
    assert positive.parts[fuse_index + 2].description == "Label(Circuit Label)"
    assert not any(part.category == "AC" for part in positive.parts)

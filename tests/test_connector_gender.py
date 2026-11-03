"""连接器公母属性规则回归测试。

规则（详见 DATA_TYPES.md「连接器公母属性规则」）：
1. IBEX/Homerun 正极产品：支线连接器（含末端支线）为 Male，外部主线连接器为 Female
2. IBEX/Homerun 负极产品：支线连接器（含末端支线）为 Female，外部主线连接器为 Male
3. LYNX 产品：支线连接器与相连 IBEX 同极性产品的外部主线连接器相反（正极 Male、负极 Female）
5. Gap Extender（仅生成负极产品）：第一个连接器为 Male，最后一个连接器为 Female
"""

import pytest

from count_and_design.application.product_generation import ProductGenerationService
from count_and_design.application.product_generation.parts_factory import (
    _make_connect_part,
)
from count_and_design.models import (
    Connector,
    IBEXBranchCoordinate,
    Inv,
    Length,
    Node,
    ProductPreset,
    Project,
    Rackings,
    RackingEntry,
    Row,
    Wire,
)


def _connector() -> Connector:
    return Connector(size=Wire("10AWG", "Cu"), type="MC4", manufacturer="Staubli")


@pytest.mark.parametrize("gender, gender_text", [("M", "Male"), ("F", "Female")])
def test_mc4_evo2a_connector_uses_square_brackets_for_dust_cap(
    gender: str, gender_text: str
) -> None:
    connector = Connector(
        size=Wire("10AWG", "Cu"),
        type="MC4-EVO2A",
        manufacturer="Staubli",
    )

    part = _make_connect_part([], gender, connector)

    assert part.description == (
        f"10AWG MC4-EVO2A {gender_text}-Staubli [Dust Cap]"
    )


def _walk_connectors(parts: list) -> list:
    """递归收集部件树中的所有连接器部件（含 semi_list 嵌套）。"""
    out = []
    for part in parts:
        if part.category == "Connect":
            out.append(part)
        out += _walk_connectors(getattr(part, "semi_list", []))
    return out


def _project_with_presets(
    project_type: str,
    presets: list[ProductPreset],
    racking_presets: str = "A",
    gap_extenders: list[tuple[str, int]] | None = None,
) -> Project:
    project = Project(
        name="Demo",
        project_type=project_type,
        racking_pitch=Length("10ft"),
        product_presets=presets,
        rackings_list=[
            Rackings(
                name="NW",
                product_presets=racking_presets,
                gap_extenders=gap_extenders or [],
            )
        ],
    )
    inv = Inv(name="INV1")
    node = Node(name="Node1", box_position=3)
    inv.nodes.append(node)
    project.invs.append(inv)
    node.rows.append(
        Row(name="Row1", racking_entries=[RackingEntry(rackings="NW", offset=Length("1ft"))])
    )
    return project


def _ibex_preset(branch_count: int = 3) -> ProductPreset:
    return ProductPreset(
        name="A",
        product_type="IBEX",
        branch_count=branch_count,
        branch_lines=[
            IBEXBranchCoordinate(
                index=i,
                coordinate_positive=Length("10ft"),
                coordinate_negative=Length("10ft"),
            )
            for i in range(1, branch_count + 1)
        ],
        branch_connector=_connector(),
        external_connector=_connector(),
        branch_cu_wire=Wire("10AWG", "Cu"),
        external_cu_wire=Wire("10AWG", "Cu"),
    )


def test_ibex_branch_connector_gender_is_opposite_to_external() -> None:
    """规则 1/2：IBEX 支线连接器（含末端支线）与外部主线连接器公母相反，
    正极支线 Male/外部 Female，负极支线 Female/外部 Male。"""
    result = ProductGenerationService().build_products(
        _project_with_presets("IBEX", [_ibex_preset()])
    )
    assert len(result.ibex_products) == 2

    for product in result.ibex_products:
        top_connectors = [p for p in product.parts if p.category == "Connect"]
        # 顶层第一个 Connect 部件即外部主线连接器（_make_ibex_external 最先添加）
        external = top_connectors[0]
        expected_external = "Connector Female" if product.polarity == "POS" else "Connector Male"
        expected_branch = "Connector Male" if product.polarity == "POS" else "Connector Female"

        assert external.cad_parts_no == expected_external
        others = [p for p in _walk_connectors(product.parts) if p is not external]
        assert others, "产品应包含支线连接器"
        assert all(p.cad_parts_no == expected_branch for p in others)


def test_gap_extender_is_neg_only_with_male_first_female_last() -> None:
    """规则 5：Gap Extender 仅生成负极产品，第一个连接器 Male、最后一个 Female。"""
    ge_preset = ProductPreset(
        name="GA",
        product_type="Gap_Extender",
        connector_male=_connector(),
        connector_female=_connector(),
        cu_wire=Wire("10AWG", "Cu"),
        length=Length("10ft"),
    )
    result = ProductGenerationService().build_products(
        _project_with_presets("IBEX", [ge_preset], racking_presets="", gap_extenders=[("GA", 1)])
    )
    assert len(result.gap_extender_products) == 1
    product = result.gap_extender_products[0]
    assert product.polarity == "NEG"
    connectors = [p for p in product.parts if p.category == "Connect"]
    assert len(connectors) == 2
    assert connectors[0].cad_parts_no == "Connector Male"
    assert connectors[-1].cad_parts_no == "Connector Female"


def test_homerun_connector_gender_matches_ibex_rule() -> None:
    """规则 1/2：Homerun Extender 正极支线 Male/外部 Female，负极支线 Female/外部 Male。"""
    homerun_preset = ProductPreset(
        name="A",
        product_type="Homerun_Extender",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(
                index=1,
                coordinate_positive=Length("10ft"),
                coordinate_negative=Length("10ft"),
            )
        ],
        branch_connector=_connector(),
        external_connector=_connector(),
        branch_cu_wire=Wire("10AWG", "Cu"),
        external_cu_wire=Wire("10AWG", "Cu"),
    )
    result = ProductGenerationService().build_products(
        _project_with_presets("IBEX", [homerun_preset])
    )
    assert len(result.homerun_extender_products) == 2

    for product in result.homerun_extender_products:
        top_connectors = [p for p in product.parts if p.category == "Connect"]
        # 顶层第一个 Connect 部件即外部主线连接器（_make_homerun_extender_product 最先添加）
        external = top_connectors[0]
        expected_external = "Connector Female" if product.polarity == "POS" else "Connector Male"
        expected_branch = "Connector Male" if product.polarity == "POS" else "Connector Female"

        assert external.cad_parts_no == expected_external
        others = [p for p in top_connectors if p is not external]
        assert others, "产品应包含支线连接器"
        assert all(p.cad_parts_no == expected_branch for p in others)
        # 描述文本也应体现外部/支线公母（如 "MC4F/ MC4 M"）
        assert product.description.endswith("F/ MC4 M") if product.polarity == "POS" \
            else product.description.endswith("M/ MC4 F")


def test_homerun_branch_connector_uses_external_copper_wire_size() -> None:
    homerun_preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(1, Length("10ft"), Length("10ft"))
        ],
        branch_connector=_connector(),
        branch_cu_wire=Wire("12AWG", "Cu"),
        external_cu_wire=Wire("8AWG", "Cu"),
    )

    result = ProductGenerationService().build_products(
        _project_with_presets("IBEX", [homerun_preset])
    )

    for product in result.homerun_extender_products:
        branch = next(
            part
            for part in reversed(product.parts)
            if part.category == "Connect"
        )
        assert branch.description.startswith("8AWG MC4")


def test_homerun_external_prefix_does_not_trigger_aluminum_switch() -> None:
    homerun_preset = ProductPreset(
        name="A",
        branch_count=1,
        branch_lines=[
            IBEXBranchCoordinate(1, Length("10ft"), Length("10ft"))
        ],
        external_cu_wire=Wire("10AWG", "Cu"),
        external_al_wire=Wire("6AWG", "Al"),
        external_al_threshold=Length("30ft"),
        external_cu_length_to_row_head=Length("3ft"),
        external_extension_trench_length=Length("2ft"),
        branch_second_cual_suffix=Length("2ft"),
    )

    result = ProductGenerationService().build_products(
        _project_with_presets("IBEX", [homerun_preset])
    )

    for product in result.homerun_extender_products:
        wires = [part for part in product.parts if part.category == "Wire"]
        # Helper layout: 20ft pitch distance + 1ft offset, plus 3ft row-head
        # and 2ft trench. The 26ft prefix remains excluded from the aluminum
        # threshold; coordinate + extender is only 10ft, below 30ft. Because
        # this product has neither a fuse nor Cu-Al conversion, the 26ft
        # prefix and 10ft mainline are emitted as one EP/EN copper wire.
        assert not product.uses_aluminum_wire
        assert [wire.description.rsplit(" ", 1)[-1] for wire in wires] == ["Cu"]
        assert [wire.length for wire in wires] == [36.0]
        expected_prefix = "EP-" if product.polarity == "POS" else "EN-"
        assert wires[0].code.startswith(expected_prefix)


def test_lynx_branch_connector_opposite_to_ibex_external() -> None:
    """规则 3：LYNX 支线连接器与相连 IBEX 同极性产品的外部主线连接器相反。"""
    lynx_preset = ProductPreset(
        name="LA",
        product_type="LYNX",
        branch_cu_wire=Wire("10AWG", "Cu"),
        branch_al_wire=Wire("none", "Al"),
        external_connector=_connector(),
    )
    # LYNX preset 通过 Project → Inv → Node 继承链引用；
    # box_position=0 使 slot 覆盖 Row1，从而生成含支线连接器的注塑件
    project = _project_with_presets("LYNX", [_ibex_preset(), lynx_preset])
    project.lynx_preset = "LA"
    project.invs[0].nodes[0].box_position = 0
    result = ProductGenerationService().build_products(project)
    assert len(result.lynx_products) == 2

    for product in result.lynx_products:
        connectors = _walk_connectors(product.parts)
        assert connectors, "LYNX 产品应包含支线连接器"
        expected = "Connector Male" if product.polarity == "POS" else "Connector Female"
        assert all(p.cad_parts_no == expected for p in connectors)

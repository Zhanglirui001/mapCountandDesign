"""末端支线（Last Branch）专属属性测试。

覆盖：
- ProductPreset 新增字段的序列化往返与回退（effective_*）行为；
- 产品生成（产品表/图纸的部件列表）中末端支线使用 last_branch_* 属性；
- 压降计算中 Terminal Branch 段使用 last_branch_* 属性。
"""

import pytest

from count_and_design.application.product_generation import ProductGenerationService
from count_and_design.application.product_generation.ibex import _make_ibex_last_branch
from count_and_design.application.voltage_drop import VoltageDropService
from count_and_design.models import (
    AssembledProduct,
    Connector,
    Fuse,
    IBEXBranchCoordinate,
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


def _cu(size: str = "12AWG") -> Wire:
    return Wire(size, "Cu")


def _project_with_rows(
    presets: list[ProductPreset], rows: list[Row]
) -> Project:
    for preset in presets:
        if preset.product_type in {"IBEX", "Homerun_Extender", "LYNX"} and not preset.pv_panel:
            preset.pv_panel = "Test Panel"
    return Project(
        name="Last Branch Test",
        project_type="IBEX",
        pv_panels=[PVPanelInfo(vmp=40.0, imp=10.0, module_per_string=20, name="Test Panel")],
        racking_pitch=Length("0ft"),
        product_presets=presets,
        rackings_list=[Rackings("R", "".join(p.name for p in presets))],
        invs=[Inv("INV1", nodes=[Node("Node1", box_position=1, rows=rows)])],
    )


def _three_branch_preset(**overrides) -> ProductPreset:
    preset = ProductPreset(
        name="A",
        branch_count=3,
        branch_lines=[
            IBEXBranchCoordinate(1, Length("0ft"), Length("0ft")),
            IBEXBranchCoordinate(2, Length("10ft"), Length("10ft")),
            IBEXBranchCoordinate(3, Length("30ft"), Length("30ft")),
        ],
        branch_cu_wire=_cu(),
        external_cu_wire=_cu(),
        internal_cu_wire=_cu("10AWG"),
        branch_length_positive=Length("0ft"),
        branch_length_negative=Length("0ft"),
        external_cu_length_to_row_head=Length("0ft"),
    )
    for key, value in overrides.items():
        setattr(preset, key, value)
    return preset


# ---------------------------------------------------------------------------
# 模型层
# ---------------------------------------------------------------------------


def test_last_branch_fields_round_trip_through_dict() -> None:
    preset = _three_branch_preset(
        last_branch_cu_wire=Wire("10AWG", "Cu", "66"),
        last_branch_al_wire=Wire("6AWG", "Al", "63"),
        last_branch_fuse=Fuse("20A", "Adler"),
    )
    loaded = ProductPreset.from_dict(preset.to_dict())
    assert loaded.last_branch_cu_wire == preset.last_branch_cu_wire
    assert loaded.last_branch_al_wire == preset.last_branch_al_wire
    assert loaded.last_branch_fuse == preset.last_branch_fuse


def test_last_branch_fields_default_to_none_and_fallback_to_branch() -> None:
    preset = _three_branch_preset()
    # 未配置时回退到一般支线
    assert preset.effective_last_branch_cu_wire is preset.branch_cu_wire
    assert preset.effective_last_branch_al_wire is preset.branch_al_wire
    assert preset.effective_last_branch_fuse is preset.branch_fuse
    # 配置后使用末端支线专属属性
    preset.last_branch_cu_wire = Wire("10AWG", "Cu")
    preset.last_branch_fuse = Fuse("20A", "Adler")
    assert preset.effective_last_branch_cu_wire is preset.last_branch_cu_wire
    assert preset.effective_last_branch_fuse is preset.last_branch_fuse


def test_legacy_preset_dict_without_last_branch_fields_loads() -> None:
    data = _three_branch_preset().to_dict()
    data.pop("last_branch_cu_wire")
    data.pop("last_branch_al_wire")
    data.pop("last_branch_fuse")
    preset = ProductPreset.from_dict(data)
    assert preset.last_branch_cu_wire.wiresize == "none"
    assert preset.last_branch_al_wire.wiresize == "none"
    assert preset.last_branch_fuse.current == "none"
    assert preset.effective_last_branch_cu_wire is preset.branch_cu_wire


# ---------------------------------------------------------------------------
# 产品生成（产品表 / 图纸部件列表）
# ---------------------------------------------------------------------------


def _flatten_parts(parts) -> list:
    """递归展开半成品（semi_list）以收集所有部件。"""
    flattened: list = []
    for part in parts:
        flattened.append(part)
        if part.semi_list:
            flattened.extend(_flatten_parts(part.semi_list))
    return flattened


def _generated_ibex_parts(preset: ProductPreset):
    project = _project_with_rows(
        [preset],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    result = ProductGenerationService().build_products(project)
    assert len(result.ibex_products) == 2  # POS + NEG
    pos = next(p for p in result.ibex_products if p.polarity == "POS")
    return _flatten_parts(pos.parts)


def test_generation_uses_last_branch_wires_and_fuse_when_configured() -> None:
    preset = _three_branch_preset(
        branch_fuse=Fuse("15A", "Adler"),
        last_branch_cu_wire=Wire("8AWG", "Cu"),
        last_branch_fuse=Fuse("20A", "Adler"),
    )
    parts = _generated_ibex_parts(preset)

    # 末端支线使用专属铜线（一般支线/外部主线仍为 12AWG）
    assert any("8AWG" in part.description for part in parts)
    assert any("12AWG" in part.description for part in parts)
    # 末端支线保险丝与一般支线保险丝同时存在
    descriptions = {part.description for part in parts}
    assert "20A Fuse+Fuseholder - Adler" in descriptions
    assert "15A Fuse+Fuseholder - Adler" in descriptions


def test_generation_falls_back_to_branch_wires_and_fuse() -> None:
    preset = _three_branch_preset(branch_fuse=Fuse("15A", "Adler"))
    parts = _generated_ibex_parts(preset)

    descriptions = {part.description for part in parts}
    assert not any("8AWG" in description for description in descriptions)
    assert "15A Fuse+Fuseholder - Adler" in descriptions
    assert not any("20A" in description for description in descriptions)


def test_generation_uses_last_branch_al_wire_when_configured() -> None:
    preset = _three_branch_preset(
        last_branch_al_wire=Wire("6AWG", "Al"),
        last_branch_cu_wire=Wire("8AWG", "Cu"),
        last_branch_al_threshold=Length("2ft"),
    )
    parts = _generated_ibex_parts(preset)
    descriptions = {part.description for part in parts}
    assert any("6AWG" in description and "Al" in description for description in descriptions)
    assert any("8AWG" in description for description in descriptions)


def test_none_fuse_and_connector_parts_are_omitted() -> None:
    parts = _generated_ibex_parts(_three_branch_preset())

    assert not any(part.category == "Fuse" for part in parts)
    assert not any(part.category == "Connect" for part in parts)


def test_last_branch_aluminum_is_decided_per_polarity() -> None:
    preset = _three_branch_preset(
        branch_lines=[
            IBEXBranchCoordinate(1, Length("0ft"), Length("0ft")),
            IBEXBranchCoordinate(2, Length("10ft"), Length("10ft")),
            IBEXBranchCoordinate(3, Length("15ft"), Length("30ft")),
        ],
        last_branch_al_wire=Wire("6AWG", "Al"),
        last_branch_al_threshold=Length("10ft"),
        branch_first_cual_prefix=Length("2ft"),
        branch_second_cual_suffix=Length("2ft"),
    )
    positive = AssembledProduct(polarity="POS")
    negative = AssembledProduct(polarity="NEG")

    _make_ibex_last_branch([], preset, positive, negative)

    assert not positive.uses_aluminum_wire
    assert negative.uses_aluminum_wire
    assert all(
        not (part.category == "Wire" and part.description.endswith("Al"))
        for part in positive.parts
    )
    assert any(
        part.category == "Wire" and part.description.endswith("Al")
        for part in negative.parts
    )


def test_unfused_positive_aluminum_last_branch_keeps_full_length() -> None:
    preset = _three_branch_preset(
        branch_lines=[
            IBEXBranchCoordinate(1, Length("0ft"), Length("0ft")),
            IBEXBranchCoordinate(2, Length("10ft"), Length("10ft")),
            IBEXBranchCoordinate(3, Length("30ft"), Length("30ft")),
        ],
        last_branch_al_wire=Wire("6AWG", "Al"),
        last_branch_al_threshold=Length("10ft"),
        branch_fuse_prefix=Length("3ft"),
        branch_first_cual_prefix=Length("2ft"),
        branch_second_cual_suffix=Length("2ft"),
    )
    positive = AssembledProduct(polarity="POS")
    negative = AssembledProduct(polarity="NEG")

    _make_ibex_last_branch([], preset, positive, negative)

    positive_wire_length = sum(
        part.length for part in positive.parts if part.category == "Wire"
    )
    assert positive_wire_length == pytest.approx(20.0)


def test_last_branch_connector_uses_last_branch_copper_size() -> None:
    preset = _three_branch_preset(
        branch_connector=Connector(
            size=Wire("12AWG", "Cu"),
            type="MC4",
            manufacturer="Staubli",
        ),
        last_branch_cu_wire=Wire("8AWG", "Cu"),
    )
    positive = AssembledProduct(polarity="POS")
    negative = AssembledProduct(polarity="NEG")

    _make_ibex_last_branch([], preset, positive, negative)

    connectors = [
        part
        for product in (positive, negative)
        for part in product.parts
        if part.category == "Connect"
    ]
    assert len(connectors) == 2
    assert all(part.description.startswith("8AWG MC4") for part in connectors)


# ---------------------------------------------------------------------------
# 压降计算
# ---------------------------------------------------------------------------


def test_voltage_drop_terminal_branch_uses_last_branch_cu_wire() -> None:
    preset = _three_branch_preset(
        branch_length_positive=Length("1ft"),
        branch_length_negative=Length("1ft"),
        last_branch_cu_wire=Wire("8AWG", "Cu"),
        last_branch_fuse=Fuse("20A", "Adler"),
    )
    project = _project_with_rows(
        [preset],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    report = VoltageDropService().calculate(project)
    terminal = [s for s in report.segments if s.role == "Terminal Branch"]
    assert terminal, "expected Terminal Branch segments"
    assert {s.wire_size for s in terminal} == {"8AWG"}
    # 一般支线仍使用 branch_cu_wire（12AWG）
    branches = [s for s in report.segments if s.role.startswith("Branch") and s.role != "Terminal Branch"]
    assert {s.wire_size for s in branches} == {"12AWG"}


def test_voltage_drop_terminal_branch_falls_back_to_branch_wire() -> None:
    preset = _three_branch_preset()
    project = _project_with_rows(
        [preset],
        [Row("Row1", [RackingEntry("R", Length("0ft"))])],
    )
    report = VoltageDropService().calculate(project)
    terminal = [s for s in report.segments if s.role == "Terminal Branch"]
    assert terminal
    assert {s.wire_size for s in terminal} == {"12AWG"}
    assert report.products[0].voltage_drop is not None

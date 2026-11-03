"""内部主线（Internal Mainline）铜铝转接阈值 internal_al_threshold 测试。

覆盖：
- ProductPreset 新字段的默认值、序列化往返，以及旧存档（缺少该键）回退到
  该预设 external_al_threshold 的迁移行为；
- 产品生成中内部主线是否采用铜铝转接由 internal_al_threshold 独立决定，
  不再受 external_al_threshold 影响（反之亦然）；
- 压降计算中 Internal Mainline 段使用同一阈值，与产品生成保持一致。
"""

import pytest

from count_and_design.application.product_generation import ProductGenerationService
from count_and_design.application.product_generation.ibex import (
    _make_ibex_external,
    _make_ibex_internal,
)
from count_and_design.application.voltage_drop import VoltageDropService
from count_and_design.models import (
    AssembledProduct,
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


def _three_branch_preset(**overrides) -> ProductPreset:
    """三条支线的 IBEX 预设：内部主线 B1->B2 的正负跨度为 10ft。"""
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
        internal_al_wire=Wire("6AWG", "Al"),
        branch_length_positive=Length("0ft"),
        branch_length_negative=Length("0ft"),
        external_cu_length_to_row_head=Length("0ft"),
    )
    for key, value in overrides.items():
        setattr(preset, key, value)
    return preset


def _has_al_wire(product: AssembledProduct) -> bool:
    return any(
        part.category == "Wire" and part.description.endswith("Al")
        for part in product.parts
    )


def _flatten_parts(parts) -> list:
    """递归展开半成品（semi_list）以收集所有部件。"""
    flattened: list = []
    for part in parts:
        flattened.append(part)
        if part.semi_list:
            flattened.extend(_flatten_parts(part.semi_list))
    return flattened


# ---------------------------------------------------------------------------
# 模型层
# ---------------------------------------------------------------------------


def test_internal_al_threshold_defaults_to_100ft() -> None:
    assert ProductPreset(name="A").internal_al_threshold == Length("100ft")


def test_internal_al_threshold_round_trips_through_dict() -> None:
    preset = _three_branch_preset(internal_al_threshold=Length("25ft"))
    loaded = ProductPreset.from_dict(preset.to_dict())
    assert loaded.internal_al_threshold == Length("25ft")


def test_explicit_internal_al_threshold_is_not_overridden_by_external() -> None:
    preset = _three_branch_preset(
        external_al_threshold=Length("1ft"),
        internal_al_threshold=Length("50ft"),
    )
    loaded = ProductPreset.from_dict(preset.to_dict())
    assert loaded.external_al_threshold == Length("1ft")
    assert loaded.internal_al_threshold == Length("50ft")


def test_legacy_preset_dict_falls_back_to_external_al_threshold() -> None:
    preset = _three_branch_preset(external_al_threshold=Length("30ft"))
    data = preset.to_dict()
    data.pop("internal_al_threshold")

    loaded = ProductPreset.from_dict(data)

    assert loaded.external_al_threshold == Length("30ft")
    # 旧存档没有该键：保持"内部主线复用外部阈值"的升级前行为。
    assert loaded.internal_al_threshold == Length("30ft")


def test_legacy_fallback_clones_threshold_instead_of_sharing_instance() -> None:
    preset = _three_branch_preset(external_al_threshold=Length("30ft"))
    data = preset.to_dict()
    data.pop("internal_al_threshold")

    loaded = ProductPreset.from_dict(data)
    assert loaded.internal_al_threshold is not loaded.external_al_threshold

    # Length 是可变对象：编辑内部阈值不得改写外部阈值。
    loaded.internal_al_threshold.set_length_str("5ft")
    assert loaded.external_al_threshold == Length("30ft")
    assert loaded.internal_al_threshold == Length("5ft")


def test_legacy_preset_dict_without_any_threshold_uses_defaults() -> None:
    data = _three_branch_preset().to_dict()
    data.pop("internal_al_threshold")
    data.pop("external_al_threshold")

    loaded = ProductPreset.from_dict(data)

    assert loaded.external_al_threshold == Length("100ft")
    assert loaded.internal_al_threshold == Length("100ft")


def test_legacy_project_file_keeps_previous_internal_mainline_behavior() -> None:
    """旧项目文件（无 internal_al_threshold 键）加载后内部主线输出不变。

    升级前内部主线复用 external_al_threshold；预设只把外部阈值设为 1ft 时，
    旧文件必须在迁移后仍然生成铝制内部主线（末端支线铝线未配置，因此产品中
    出现的铝线只能来自内部主线）。
    """
    preset = _three_branch_preset(external_al_threshold=Length("1ft"))
    project = _project_with_rows(
        [preset], [Row("Row1", [RackingEntry("R", Length("0ft"))])]
    )
    data = project.to_dict()
    data["product_presets"][0].pop("internal_al_threshold")

    loaded = Project.from_dict(data)
    assert loaded.product_presets[0].internal_al_threshold == Length("1ft")

    result = ProductGenerationService().build_products(loaded)
    positive = next(p for p in result.ibex_products if p.polarity == "POS")
    assert any(
        part.category == "Wire" and part.description.endswith("Al")
        for part in _flatten_parts(positive.parts)
    )


# ---------------------------------------------------------------------------
# 产品生成：内部主线
# ---------------------------------------------------------------------------


def _internal_parts(preset: ProductPreset) -> tuple[AssembledProduct, AssembledProduct]:
    positive = AssembledProduct(polarity="POS")
    negative = AssembledProduct(polarity="NEG")
    _make_ibex_internal(
        [], preset, positive, negative, preset.branch_lines[0], preset.branch_lines[1]
    )
    return positive, negative


def test_internal_mainline_uses_aluminum_at_or_above_threshold() -> None:
    preset = _three_branch_preset(internal_al_threshold=Length("10ft"))

    positive, negative = _internal_parts(preset)

    for product in (positive, negative):
        assert product.uses_aluminum_wire
        assert _has_al_wire(product)
        # 铜铝转接器成对出现
        assert sum(part.category == "AC" for part in product.parts) == 2


def test_internal_mainline_stays_copper_below_threshold() -> None:
    preset = _three_branch_preset(internal_al_threshold=Length("11ft"))

    positive, negative = _internal_parts(preset)

    for product in (positive, negative):
        assert not product.uses_aluminum_wire
        assert not _has_al_wire(product)
        assert not any(part.category == "AC" for part in product.parts)
        wires = [part for part in product.parts if part.category == "Wire"]
        assert [part.length for part in wires] == [pytest.approx(10.0)]


def test_external_threshold_does_not_affect_internal_mainline() -> None:
    """内部主线只认 internal_al_threshold：外部阈值很小也不转接。"""
    preset = _three_branch_preset(
        external_al_threshold=Length("1ft"),
        internal_al_threshold=Length("100ft"),
    )

    positive, negative = _internal_parts(preset)

    for product in (positive, negative):
        assert not product.uses_aluminum_wire
        assert not _has_al_wire(product)


def test_internal_threshold_does_not_affect_external_mainline() -> None:
    """反向独立性：内部阈值很小，外部主线仍按 external_al_threshold 保持纯铜。"""
    preset = _three_branch_preset(
        external_al_threshold=Length("100ft"),
        internal_al_threshold=Length("0ft"),
        external_al_wire=Wire("6AWG", "Al"),
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

    for product in (positive, negative):
        assert not product.uses_aluminum_wire
        assert not _has_al_wire(product)


def test_internal_mainline_aluminum_is_decided_per_polarity() -> None:
    preset = _three_branch_preset(
        branch_lines=[
            IBEXBranchCoordinate(1, Length("0ft"), Length("0ft")),
            IBEXBranchCoordinate(2, Length("10ft"), Length("30ft")),
            IBEXBranchCoordinate(3, Length("40ft"), Length("50ft")),
        ],
        internal_al_threshold=Length("20ft"),
    )
    positive = AssembledProduct(polarity="POS")
    negative = AssembledProduct(polarity="NEG")

    _make_ibex_internal(
        [], preset, positive, negative, preset.branch_lines[0], preset.branch_lines[1]
    )

    assert not positive.uses_aluminum_wire
    assert negative.uses_aluminum_wire
    assert not _has_al_wire(positive)
    assert _has_al_wire(negative)


# ---------------------------------------------------------------------------
# 压降计算
# ---------------------------------------------------------------------------


def _project_with_rows(presets: list[ProductPreset], rows: list[Row]) -> Project:
    for preset in presets:
        if (
            preset.product_type in {"IBEX", "Homerun_Extender", "LYNX"}
            and not preset.pv_panel
        ):
            preset.pv_panel = "Test Panel"
    return Project(
        name="Internal Al Threshold Test",
        project_type="IBEX",
        pv_panels=[
            PVPanelInfo(vmp=40.0, imp=10.0, module_per_string=20, name="Test Panel")
        ],
        racking_pitch=Length("0ft"),
        product_presets=presets,
        rackings_list=[Rackings("R", "".join(p.name for p in presets))],
        invs=[Inv("INV1", nodes=[Node("Node1", box_position=1, rows=rows)])],
    )


def _segments_by_role(preset: ProductPreset, role_prefix: str):
    project = _project_with_rows(
        [preset], [Row("Row1", [RackingEntry("R", Length("0ft"))])]
    )
    report = VoltageDropService().calculate(project)
    assert not [issue for issue in report.issues if issue.code == "missing_resistance"]
    return [s for s in report.segments if s.role.startswith(role_prefix)]


def test_voltage_drop_internal_mainline_uses_internal_threshold() -> None:
    preset = _three_branch_preset(internal_al_threshold=Length("10ft"))

    segments = _segments_by_role(preset, "Internal Mainline")

    assert segments
    assert {segment.material for segment in segments} == {"Cu", "Al"}
    assert {
        segment.wire_size for segment in segments if segment.material == "Al"
    } == {"6AWG"}


def test_voltage_drop_internal_mainline_ignores_external_threshold() -> None:
    preset = _three_branch_preset(
        external_al_threshold=Length("1ft"),
        internal_al_threshold=Length("100ft"),
    )

    segments = _segments_by_role(preset, "Internal Mainline")

    assert segments
    assert {segment.material for segment in segments} == {"Cu"}


def test_voltage_drop_external_mainline_ignores_internal_threshold() -> None:
    preset = _three_branch_preset(
        external_al_wire=Wire("6AWG", "Al"),
        external_al_threshold=Length("100ft"),
        internal_al_threshold=Length("0ft"),
        # 非零外部前缀，确保外部主线确实产生压降段。
        external_cu_length_to_row_head=Length("5ft"),
    )

    internal = _segments_by_role(preset, "Internal Mainline")
    external = _segments_by_role(preset, "External Mainline")

    assert internal and external
    assert {segment.material for segment in internal} == {"Cu", "Al"}
    assert {segment.material for segment in external} == {"Cu"}

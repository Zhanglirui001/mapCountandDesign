"""Quick validation of all new product-related models."""
from count_and_design.models import (
    Connector, Fuse, IBEXBranch, IBEXBranchCoordinate, IBEXProduct,
    LYNXBranch, LYNXProduct, Overmold, ProductPreset,
    Length, Wire,
)
from count_and_design.models.connector import CONNECTOR_MANUFACTURER_MAP


NEW_CONNECTOR_MANUFACTURERS = {
    "TT02": "Chuangda",
    "T6-2C": "Tlian",
    "PV-SY02": "Risen",
    "TS4": "Trina",
    "TS4-PLUS-3": "Trina",
    "PV-RY01": "Runergy",
    "H4-Plus": "Amphenol",
    "PV-TS02": "Tiansheng",
    "PV-RY02": "Runergy",
    "PV5": "TE connectivity",
    "Z4S": "Zerun",
}


def test_connector():
    c = Connector(size=Wire("4mm2", "Cu"), type="MC4", manufacturer="Staubli")
    d = c.to_dict()
    c2 = Connector.from_dict(d)
    assert c.size.wiresize == c2.size.wiresize
    assert c.type == c2.type
    assert c.manufacturer == c2.manufacturer
    print("  Connector: OK")


def test_new_connector_manufacturers():
    assert {
        connector_type: CONNECTOR_MANUFACTURER_MAP[connector_type]
        for connector_type in NEW_CONNECTOR_MANUFACTURERS
    } == NEW_CONNECTOR_MANUFACTURERS

    for connector_type, manufacturer in NEW_CONNECTOR_MANUFACTURERS.items():
        connector = Connector(
            size=Wire("4mm2", "Cu"),
            type=connector_type,
            manufacturer="none",
        )
        assert connector.type == connector_type
        assert connector.manufacturer == manufacturer


def test_fuse():
    f = Fuse(current="15A", manufacturer="Adler")
    d = f.to_dict()
    f2 = Fuse.from_dict(d)
    assert f.current == f2.current
    assert f.manufacturer == f2.manufacturer
    print("  Fuse: OK")


def test_ibex_branch_coordinate():
    ibc = IBEXBranchCoordinate(
        index=1,
        coordinate_positive=Length("5ft"),
        coordinate_negative=Length("3ft"),
    )
    d = ibc.to_dict()
    ibc2 = IBEXBranchCoordinate.from_dict(d)
    assert ibc.index == ibc2.index
    print("  IBEXBranchCoordinate: OK")


def test_ibex_branch():
    ib = IBEXBranch(index=1, prefix_length=Length("2ft"))
    d = ib.to_dict()
    ib2 = IBEXBranch.from_dict(d)
    assert ib.index == ib2.index
    print("  IBEXBranch: OK")


def test_lynx_branch():
    lb = LYNXBranch(
        location=1,
        cu_wire=Wire("4mm2", "Cu"),
        al_wire=Wire("6mm2", "Al"),
    )
    d = lb.to_dict()
    lb2 = LYNXBranch.from_dict(d)
    assert lb.location == lb2.location
    assert lb.cu_length.to_mm() == lb2.cu_length.to_mm()
    print("  LYNXBranch: OK")


def test_overmold():
    lb = LYNXBranch(location=1, cu_wire=Wire("4mm2", "Cu"), al_wire=Wire("6mm2", "Al"))
    om = Overmold(branch_lines=[lb], prefix_length=Length("1ft"))
    d = om.to_dict()
    om2 = Overmold.from_dict(d)
    assert len(om2.branch_lines) == 1
    print("  Overmold: OK")


def test_lynx_product():
    lb = LYNXBranch(location=1, cu_wire=Wire("4mm2", "Cu"), al_wire=Wire("6mm2", "Al"))
    om = Overmold(branch_lines=[lb], prefix_length=Length("1ft"))
    lp = LYNXProduct(
        product_code="LYNX-001",
        mainline_wire=Wire("10mm2", "Cu"),
        overmolds=[om],
    )
    d = lp.to_dict()
    lp2 = LYNXProduct.from_dict(d)
    assert lp.product_code == lp2.product_code
    print("  LYNXProduct: OK")


def test_ibex_product():
    ib = IBEXBranch(index=1, prefix_length=Length("2ft"))
    iep = IBEXProduct(
        product_preset="MyPreset",
        product_code="IBEX-001",
        polarity="POS",
        extender_length=Length("10ft"),
        whip_length=Length("3ft"),
        branches=[ib],
    )
    d = iep.to_dict()
    iep2 = IBEXProduct.from_dict(d)
    assert iep.product_preset == iep2.product_preset
    assert iep.polarity == iep2.polarity
    print("  IBEXProduct: OK")


def test_product_preset_ibex():
    pp = ProductPreset(
        name="T",
        product_type="IBEX",
        branch_count=2,
        branch_connector=Connector(Wire("4mm2", "Cu"), "MC4", "Staubli"),
        branch_fuse=Fuse("15A", "Adler"),
        branch_cu_wire=Wire("4mm2", "Cu"),
        branch_al_wire=Wire("6mm2", "Al"),
        external_cu_wire=Wire("10mm2", "Cu"),
        external_al_wire=Wire("16mm2", "Al"),
        external_fuse=Fuse("30A", "Adler"),
        external_connector=Connector(Wire("10mm2", "Cu"), "MC4", "Staubli"),
        internal_cu_wire=Wire("4mm2", "Cu"),
        internal_al_wire=Wire("6mm2", "Al"),
        product_total_length=Length("100ft"),
    )
    d = pp.to_dict()
    pp2 = ProductPreset.from_dict(d)
    assert pp2.product_type == "IBEX"
    assert pp2.name == "T"
    print("  ProductPreset (IBEX): OK")


def test_product_preset_lynx():
    lb = LYNXBranch(location=1, cu_wire=Wire("4mm2", "Cu"), al_wire=Wire("6mm2", "Al"))
    om = Overmold(branch_lines=[lb], prefix_length=Length("1ft"))
    pp = ProductPreset(
        name="LA",
        product_type="LYNX",
        branch_cu_wire=Wire("4mm2", "Cu"),
        branch_al_wire=Wire("6mm2", "Al"),
        cluster_wire=Wire("240mm2", "Al"),
    )
    d = pp.to_dict()
    pp3 = ProductPreset.from_dict(d)
    assert pp3.product_type == "LYNX"
    assert pp3.name == "LA"
    print("  ProductPreset (LYNX): OK")


def test_validation():
    """Test that validation catches invalid values."""
    try:
        Connector(size=Wire("4mm2", "Cu"), type="INVALID", manufacturer="Staubli")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    try:
        Fuse(current="999A", manufacturer="Adler")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    try:
        ProductPreset(name="X", product_type="INVALID")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    # IBEX name must be single uppercase letter
    try:
        ProductPreset(name="Bad")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    # LYNX name must match r"L[A-Z]+"
    try:
        ProductPreset(name="L", product_type="LYNX")
        assert False, "Should have raised ValueError for LYNX name 'L'"
    except ValueError:
        pass

    try:
        ProductPreset(name="LX", product_type="LYNX")
    except ValueError:
        assert False, "Should not raise ValueError for LYNX name 'LX'"

    # IBEX / LYNX now have default values for all required fields,
    # so creating with only name should succeed (no longer raises ValueError)

    print("  Validation: OK")


if __name__ == "__main__":
    print("Testing new product models...")
    test_connector()
    test_fuse()
    test_ibex_branch_coordinate()
    test_ibex_branch()
    test_lynx_branch()
    test_overmold()
    test_lynx_product()
    test_ibex_product()
    test_product_preset_ibex()
    test_product_preset_lynx()
    test_validation()
    print()
    print("All tests passed!")

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from types import SimpleNamespace
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QListWidgetItem, QTreeWidgetItem

from count_and_design.application.ordering import (
    move_selected_items,
    natural_text_key,
    sort_selected_slots,
)
from count_and_design.gui.viewmodels import AppViewModel
from count_and_design.gui.views.ordered_selection import OrderedListWidget, OrderedTreeWidget
from count_and_design.models import (
    Inv,
    Length,
    LengthIdentifier,
    Node,
    ProductPreset,
    Project,
    Rackings,
    Row,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_natural_text_and_selected_slot_sorting_keep_unselected_fixed() -> None:
    values = ["INV10", "fixed-a", "INV2", "fixed-b", "INV1"]
    selected = [values[0], values[2], values[4]]

    changed = sort_selected_slots(
        values, selected, natural_text_key, descending=False
    )

    assert changed
    assert values == ["INV1", "fixed-a", "INV2", "fixed-b", "INV10"]


def test_missing_values_remain_last_when_descending() -> None:
    class Entry:
        def __init__(self, value):
            self.value = value

    entries = [Entry(None), Entry(1), Entry(3), Entry(None), Entry(2)]
    sort_selected_slots(entries, entries, lambda item: item.value, descending=True)
    assert [entry.value for entry in entries] == [3, 2, 1, None, None]


def test_move_selected_items_preserves_block_order_and_adjusts_boundary() -> None:
    values = [object() for _ in range(6)]
    original = list(values)
    assert move_selected_items(values, [values[1], values[3]], values[5])
    assert values == [original[0], original[2], original[4], original[1], original[3], original[5]]


def test_flat_viewmodels_sort_their_real_project_collections() -> None:
    app = AppViewModel()
    project = app.project
    project.length_identifiers = [
        LengthIdentifier("C", Length("10mm")),
        LengthIdentifier("B", Length("2mm")),
        LengthIdentifier("A", Length("1mm")),
    ]
    project.rackings_list = [
        Rackings("R10", "A"),
        Rackings("R2", "AB"),
        Rackings("R1", "B"),
    ]
    project.product_presets = [
        ProductPreset("A", branch_count=1, remarks="z"),
        ProductPreset("LA", product_type="LYNX", remarks="m"),
        ProductPreset("B", branch_count=3, remarks="a"),
    ]
    app._push_project()

    length_vm = app.length_identifiers_tab
    length_vm.select_items(tuple(length_vm._item_lookup))
    length_vm.sort_selected("name")
    assert [item.name for item in project.length_identifiers] == ["A", "B", "C"]

    rackings_vm = app.rackings_tab
    rackings_vm.select_items(tuple(rackings_vm._item_lookup))
    rackings_vm.sort_selected("branch_count", descending=True)
    assert [item.name for item in project.rackings_list] == ["R2", "R1", "R10"]

    presets_vm = app.product_presets_tab
    assert [item.name for item in presets_vm._item_lookup.values()] == ["A", "LA", "B"]
    presets_vm.select_items(tuple(presets_vm._item_lookup))
    presets_vm.sort_selected("remarks")
    assert [item.name for item in project.product_presets] == ["B", "LA", "A"]


def test_count_selection_scope_sort_move_and_renumber_preserve_subtrees() -> None:
    node_a = Node("Node10", rows=[Row("East08"), Row("West12")])
    node_b = Node("Node2", rows=[Row("Row99")])
    node_c = Node("Node1", rows=[Row("Row1")])
    other_node = Node("Other1", rows=[Row("OtherRow1")])
    inv = Inv("INV1", nodes=[node_a, node_b, node_c])
    other_inv = Inv("INV2", nodes=[other_node])
    app = AppViewModel()
    app._project = Project(name="P", invs=[inv, other_inv])
    app._push_project()
    vm = app.count_tab

    vm.select_items((vm._key_for(node_a), vm._key_for(other_node)))
    assert vm.state.selected_node_ids == (vm._key_for(node_a),)

    vm.select_items(tuple(vm._key_for(item) for item in inv.nodes))
    vm.sort_selected("name")
    assert [item.name for item in inv.nodes] == ["Node1", "Node2", "Node10"]
    assert node_a.rows[0].name == "East08"

    vm.select_items((vm._key_for(node_a), vm._key_for(node_b)))
    vm.move_selected(vm._key_for(node_c), after=False)
    assert inv.nodes == [node_b, node_a, node_c]
    assert node_a.rows[0].name == "East08"

    vm.select_items((vm._key_for(node_a.rows[0]), vm._key_for(node_a.rows[1])))
    assert vm.renumber_selected()
    assert [row.name for row in node_a.rows] == ["East08", "West09"]


def test_count_inv_numeric_sort_keys_use_inheritance_and_invalidate_voltage() -> None:
    inv_a = Inv("INV1", racking_pitch=Length("30mm"), nodes=[Node("A", cluster_length=Length("2m"))])
    inv_b = Inv("INV2", nodes=[Node("B", cluster_length=Length("500mm"))])
    inv_c = Inv("INV3", racking_pitch=Length("10mm"), nodes=[Node("C")])
    project = Project(name="P", racking_pitch=Length("20mm"), invs=[inv_a, inv_b, inv_c])
    app = AppViewModel()
    app._project = project
    app._push_project()
    vm = app.count_tab

    all_ids = tuple(vm._key_for(item) for item in project.invs)
    vm.select_items(all_ids)
    vm.sort_selected("racking_pitch")
    assert project.invs == [inv_c, inv_b, inv_a]

    vm.select_items(tuple(vm._key_for(item) for item in project.invs))
    vm.sort_selected("cluster_total_length", descending=True)
    assert project.invs == [inv_a, inv_b, inv_c]

    values = {id(inv_a): 0.02, id(inv_b): 0.03, id(inv_c): 0.01}

    class Report:
        def for_object(self, item):
            return SimpleNamespace(voltage_drop_percent=values[id(item)])

    vm._voltage_report = Report()
    vm.select_items(tuple(vm._key_for(item) for item in project.invs))
    assert vm.can_sort_voltage_drop()
    vm.sort_selected("voltage_drop_percent", descending=True)
    assert project.invs == [inv_b, inv_a, inv_c]
    assert vm._voltage_report is None


def test_versioned_sorts_round_trip_nested_collections() -> None:
    project = Project(
        name="P",
        length_identifiers=[
            LengthIdentifier("C", Length("3mm")),
            LengthIdentifier("A", Length("1mm")),
            LengthIdentifier("B", Length("2mm")),
        ],
        rackings_list=[
            Rackings("R3", gap_extenders=[("E1", 1), ("E2", 2), ("E3", 3)]),
            Rackings("R1", gap_extenders=[]),
            Rackings("R2", gap_extenders=[("E4", 1)]),
        ],
        product_presets=[
            ProductPreset("A", remarks="c", branch_count=3),
            ProductPreset("B", remarks="a", branch_count=1),
            ProductPreset("C", remarks="b", branch_count=2),
        ],
        invs=[
            Inv("INV3", nodes=[
                Node("Node3", rows=[Row("Row3"), Row("Row1"), Row("Row2")]),
                Node("Node1", rows=[]),
                Node("Node2", rows=[Row("Only")]),
            ]),
            Inv("INV1", nodes=[]),
            Inv("INV2", nodes=[Node("Only")]),
        ],
    )
    app = AppViewModel()
    app._project = project
    app._push_project()

    def round_trip(action) -> None:
        before = app.project.to_dict()
        parent_id = app.version_manager.current_id
        action()
        after = app.project.to_dict()
        child_id = app.version_manager.current_id
        assert child_id != parent_id
        app.checkout_node(parent_id)
        assert app.project.to_dict() == before
        app.checkout_node(child_id)
        assert app.project.to_dict() == after

    def sort_invs() -> None:
        vm = app.count_tab
        vm.select_items(tuple(vm._key_for(item) for item in app.project.invs))
        vm.sort_selected("name")

    def sort_nodes() -> None:
        vm = app.count_tab
        inv = next(item for item in app.project.invs if item.name == "INV3")
        vm.select_items(tuple(vm._key_for(item) for item in inv.nodes))
        vm.sort_selected("name")

    def sort_rows() -> None:
        vm = app.count_tab
        inv = next(item for item in app.project.invs if item.name == "INV3")
        node = next(item for item in inv.nodes if item.name == "Node3")
        vm.select_items(tuple(vm._key_for(item) for item in node.rows))
        vm.sort_selected("name")

    round_trip(sort_invs)
    round_trip(sort_nodes)
    round_trip(sort_rows)

    for vm, collection, field in (
        (app.length_identifiers_tab, lambda: app.project.length_identifiers, "name"),
        (app.rackings_tab, lambda: app.project.rackings_list, "name"),
        (
            app.product_presets_tab,
            lambda: [item for item in app.project.product_presets if item.name in {"A", "B", "C"}],
            "remarks",
        ),
    ):
        def action(vm=vm, collection=collection, field=field) -> None:
            vm.select_items(tuple(vm._key_for(item) for item in collection()))
            vm.sort_selected(field)
        round_trip(action)

    current = app.version_manager.get_node(app.version_manager.current_id)
    assert current is not None
    assert current.metadata["kind"] == "sort"
    assert current.metadata["subjects"] == ["A", "B", "C"]
    assert all(name in current.summary for name in ("A", "B", "C"))


def test_count_renumber_rejects_conflicts_atomically() -> None:
    first = Row("East08")
    second = Row("West20")
    conflict = Row("West09")
    node = Node("Node1", rows=[first, second, conflict])
    app = AppViewModel()
    app._project = Project(name="P", invs=[Inv("INV1", nodes=[node])])
    app._push_project()
    vm = app.count_tab
    vm.select_items((vm._key_for(first), vm._key_for(second)))

    assert not vm.renumber_selected()
    assert [row.name for row in node.rows] == ["East08", "West20", "West09"]


def test_count_renumber_uses_first_items_zero_padding_policy() -> None:
    rows = [Row("Row1"), Row("Row09"), Row("Row010")]
    node = Node("Node1", rows=rows)
    app = AppViewModel()
    app._project = Project(name="P", invs=[Inv("INV1", nodes=[node])])
    app._push_project()
    vm = app.count_tab
    vm.select_items(tuple(vm._key_for(row) for row in rows))

    assert vm.renumber_selected()
    assert [row.name for row in rows] == ["Row1", "Row2", "Row3"]

    rows[0].name, rows[1].name, rows[2].name = "Row01", "Row7", "Row100"
    vm.refresh()
    vm.select_items(tuple(vm._key_for(row) for row in rows))
    assert vm.renumber_selected()
    assert [row.name for row in rows] == ["Row01", "Row02", "Row03"]


def _make_ordered_list() -> OrderedListWidget:
    widget = OrderedListWidget()
    widget.resize(240, 180)
    for index in range(3):
        item = QListWidgetItem(f"Item {index}")
        item.setData(Qt.ItemDataRole.UserRole, f"id-{index}")
        widget.addItem(item)
    widget.show()
    QApplication.processEvents()
    return widget


def _wait_for_reorder_armed(widget) -> None:
    """Wait for the delayed block reorder instead of trusting ``qWait``.

    Reordering arms with a 300 ms long-press timer; under parallel test
    execution that timer can fire late, so poll the widget's own flag rather
    than assuming a fixed wait has been enough.
    """

    for _attempt in range(200):
        QApplication.processEvents()
        if widget._reorder_armed:
            return
        QTest.qWait(10)
    raise AssertionError("Long-press reorder did not arm.")


def test_ctrl_drag_selects_immediately_without_long_press() -> None:
    widget = _make_ordered_list()
    emitted: list[tuple[str, ...]] = []
    widget.selection_requested.connect(emitted.append)
    first = widget.visualItemRect(widget.item(0)).center()
    second = widget.visualItemRect(widget.item(1)).center()

    QTest.mousePress(
        widget.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        first,
    )
    QTest.mouseMove(widget.viewport(), second)
    QTest.mouseRelease(
        widget.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        second,
    )

    assert emitted[-1] == ("id-0", "id-1")
    widget.close()


def test_ctrl_click_only_adds_to_existing_selection() -> None:
    widget = _make_ordered_list()
    emitted: list[tuple[str, ...]] = []
    widget.selection_requested.connect(emitted.append)
    first = widget.visualItemRect(widget.item(0)).center()
    second = widget.visualItemRect(widget.item(1)).center()

    QTest.mouseClick(widget.viewport(), Qt.MouseButton.LeftButton, pos=first)
    QTest.mouseClick(
        widget.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        second,
    )

    assert emitted[-1] == ("id-0", "id-1")
    widget.close()


def test_plain_reorder_requires_300_ms_stationary_hold() -> None:
    widget = _make_ordered_list()
    widget.apply_selected_ids(("id-0",))
    emitted: list[tuple[object, bool]] = []
    widget.reorder_requested.connect(lambda target, after: emitted.append((target, after)))
    first = widget.visualItemRect(widget.item(0)).center()
    third_rect = widget.visualItemRect(widget.item(2))
    third_bottom = QPoint(third_rect.center().x(), third_rect.bottom() - 1)

    QTest.mousePress(widget.viewport(), Qt.MouseButton.LeftButton, pos=first)
    QTest.qWait(100)
    QTest.mouseMove(widget.viewport(), third_bottom)
    QTest.mouseRelease(widget.viewport(), Qt.MouseButton.LeftButton, pos=third_bottom)
    assert emitted == []

    widget.apply_selected_ids(("id-0",))
    QTest.mousePress(widget.viewport(), Qt.MouseButton.LeftButton, pos=first)
    _wait_for_reorder_armed(widget)
    assert widget.viewport().cursor().shape() == Qt.CursorShape.OpenHandCursor
    QTest.mouseMove(widget.viewport(), third_bottom)
    assert widget.viewport().cursor().shape() == Qt.CursorShape.ClosedHandCursor
    QTest.mouseRelease(widget.viewport(), Qt.MouseButton.LeftButton, pos=third_bottom)
    assert emitted == [("id-2", True)]
    assert widget.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
    widget.close()


def test_tree_disclosure_indicator_keeps_native_single_click_expand() -> None:
    widget = OrderedTreeWidget()
    widget.resize(240, 180)
    root = QTreeWidgetItem(widget, ["Root"])
    QTreeWidgetItem(root, ["Child"])
    widget.show()
    QApplication.processEvents()
    rect = widget.visualItemRect(root)
    disclosure = QPoint(
        rect.left() - widget.indentation() // 2,
        rect.center().y(),
    )

    assert not root.isExpanded()
    QTest.mouseClick(widget.viewport(), Qt.MouseButton.LeftButton, pos=disclosure)
    assert root.isExpanded()
    QTest.mouseClick(widget.viewport(), Qt.MouseButton.LeftButton, pos=disclosure)
    assert not root.isExpanded()
    widget.close()


def test_agent3_tree_supports_tuple_ids_and_long_press_drag() -> None:
    app = QApplication.instance() or QApplication([])
    tree = OrderedTreeWidget()
    tree.resize(260, 200)
    root = QTreeWidgetItem(tree, ["INV"])
    root.setData(0, Qt.ItemDataRole.UserRole, (0,))
    root.setData(0, Qt.ItemDataRole.UserRole + 1, "inv")
    for index in range(3):
        item = QTreeWidgetItem(root, [f"Node{index + 1}"])
        item.setData(0, Qt.ItemDataRole.UserRole, (0, index))
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "node")
    root.setExpanded(True)
    tree.show()
    app.processEvents()

    requested = []
    tree.selection_requested.connect(requested.append)
    first = tree.visualItemRect(root.child(0)).center()
    second = tree.visualItemRect(root.child(1)).center()
    QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=first)
    QTest.mouseClick(
        tree.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        second,
    )
    assert requested[-1] == ((0, 0), (0, 1))

    tree.apply_selected_ids(((0, 0), (0, 1)))
    drops = []
    tree.reorder_requested.connect(
        lambda target, after: drops.append((target, after))
    )
    third_rect = tree.visualItemRect(root.child(2))
    third_bottom = QPoint(third_rect.center().x(), third_rect.bottom() - 1)
    QTest.mousePress(tree.viewport(), Qt.MouseButton.LeftButton, pos=first)
    _wait_for_reorder_armed(tree)
    QTest.mouseMove(tree.viewport(), third_bottom)
    QTest.mouseRelease(
        tree.viewport(), Qt.MouseButton.LeftButton, pos=third_bottom
    )

    assert drops == [((0, 2), True)]
    tree.close()

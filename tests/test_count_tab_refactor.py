import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication
from unittest.mock import patch

from count_and_design.gui.viewmodels import AppViewModel
from count_and_design.models import Inv, Node, Project, Row


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def hierarchy() -> tuple[AppViewModel, dict[str, object]]:
    app = AppViewModel()
    row1 = Row("Row1")
    row2 = Row("Row2")
    node1 = Node("Node1", rows=[row1])
    node2 = Node("Node2", rows=[row2])
    inv1 = Inv("INV1", nodes=[node1])
    inv2 = Inv("INV2", nodes=[node2])
    project = Project(name="Project", invs=[inv1, inv2])
    app._project = project
    app._push_project()
    return app, {
        "project": project,
        "inv": inv1,
        "node": node1,
        "row": row1,
        "other_inv": inv2,
        "other_node": node2,
    }


@pytest.mark.parametrize(
    ("selected", "level", "expected_selected", "expected_expanded"),
    [
        ("project", 1, "project", ()),
        ("project", 2, "project", ("project", "inv", "other_inv")),
        (
            "project",
            3,
            "project",
            ("project", "inv", "other_inv", "node", "other_node"),
        ),
        ("inv", 1, "project", ()),
        ("inv", 2, "inv", ("project", "inv")),
        ("inv", 3, "inv", ("project", "inv", "node")),
        ("node", 1, "project", ()),
        ("node", 2, "inv", ("project",)),
        ("node", 3, "node", ("project", "inv", "node")),
        ("row", 1, "project", ()),
        ("row", 2, "inv", ("project",)),
        ("row", 3, "node", ("project", "inv")),
    ],
)
def test_expand_level_characterization(
    hierarchy: tuple[AppViewModel, dict[str, object]],
    selected: str,
    level: int,
    expected_selected: str,
    expected_expanded: tuple[str, ...],
) -> None:
    _app, objects = hierarchy
    view_model = _app.count_tab
    assert view_model.select_object(objects[selected])

    view_model.expand_level(level)

    assert view_model.state.selected_node_id == view_model._key_for(
        objects[expected_selected]
    )
    assert view_model._expanded_ids == {
        view_model._key_for(objects[name]) for name in expected_expanded
    }


@pytest.mark.parametrize(
    ("target_name", "method_name"),
    [
        ("project", "update_project_lynx_preset"),
        ("inv", "update_inv_lynx_preset"),
        ("node", "update_node_lynx_preset"),
    ],
)
def test_lynx_selection_is_versioned(
    hierarchy: tuple[AppViewModel, dict[str, object]],
    target_name: str,
    method_name: str,
) -> None:
    app, objects = hierarchy
    view_model = app.count_tab
    assert view_model.select_object(objects[target_name])
    before = app.version_manager.current_id

    getattr(view_model, method_name)("LA")

    assert app.version_manager.current_id != before
    assert getattr(objects[target_name], "lynx_preset") == "LA"


@pytest.mark.parametrize(
    ("target_name", "method_name"),
    [
        ("project", "create_and_select_project_lynx_preset"),
        ("inv", "create_and_select_inv_lynx_preset"),
        ("node", "create_and_select_node_lynx_preset"),
    ],
)
def test_lynx_creation_is_versioned(
    hierarchy: tuple[AppViewModel, dict[str, object]],
    target_name: str,
    method_name: str,
) -> None:
    app, objects = hierarchy
    view_model = app.count_tab
    assert view_model.select_object(objects[target_name])
    before = app.version_manager.current_id

    getattr(view_model, method_name)()

    assert app.version_manager.current_id != before
    assert getattr(objects[target_name], "lynx_preset").startswith("L")


def test_racking_entry_add_remove_and_restore(
    hierarchy: tuple[AppViewModel, dict[str, object]],
) -> None:
    app, objects = hierarchy
    view_model = app.count_tab
    assert view_model.select_object(objects["row"])

    view_model.add_racking_entry()
    add_version = app.version_manager.current_id
    assert len(objects["row"].racking_entries) == 2

    view_model.remove_racking_entry(1)
    assert len(objects["row"].racking_entries) == 1

    app.checkout_node(add_version)
    restored_row = app.project.invs[0].nodes[0].rows[0]
    assert len(restored_row.racking_entries) == 2


def test_select_object_reveals_all_ancestors(
    hierarchy: tuple[AppViewModel, dict[str, object]],
) -> None:
    app, objects = hierarchy
    view_model = app.count_tab
    view_model.set_expanded_ids([])

    assert view_model.select_object(objects["row"])

    assert view_model.state.selected_node_id == view_model._key_for(objects["row"])
    assert view_model._expanded_ids == {
        view_model._key_for(objects["project"]),
        view_model._key_for(objects["inv"]),
        view_model._key_for(objects["node"]),
    }


def test_selection_and_expansion_reuse_voltage_report(
    hierarchy: tuple[AppViewModel, dict[str, object]],
) -> None:
    app, objects = hierarchy
    view_model = app.count_tab

    with patch.object(
        view_model._voltage_drop_service,
        "calculate",
        wraps=view_model._voltage_drop_service.calculate,
    ) as calculate:
        request = view_model.create_voltage_calculation_request()
        assert request is not None
        report = view_model.calculate_voltage_request(request)
        assert view_model.accept_voltage_report(request, report)
        assert calculate.call_count == 1

        view_model.select_object(objects["inv"])
        view_model.expand_level(2)
        view_model.select_object(objects["row"])
        assert calculate.call_count == 1

        view_model.update_row_field("rackings_entry_offset:0", "1m")
        assert calculate.call_count == 1

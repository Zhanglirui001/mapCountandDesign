import json

import pytest

from count_and_design.application.project_session import (
    NegativeBoxPositionError,
    ProjectSession,
)
from count_and_design.application.version_manager import VersionManager
from count_and_design.file_integrity import (
    IntegrityStatus,
    attach_file_integrity,
    verify_file_integrity,
)
from count_and_design.models import Inv, Node, Project


def _tr(_key: str, fallback: str) -> str:
    return fallback


def test_saved_payload_hash_covers_project_and_history(tmp_path) -> None:
    session = ProjectSession(_tr)
    session.add_inv("INV1")
    path = tmp_path / "project.json"
    session.save_project_as(str(path))
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 2
    assert verify_file_integrity(payload) is IntegrityStatus.VERIFIED

    project_tamper = json.loads(json.dumps(payload))
    project_tamper["name"] = "external change"
    assert verify_file_integrity(project_tamper) is IntegrityStatus.MODIFIED

    history_tamper = json.loads(json.dumps(payload))
    history_tamper["version_tree"]["current_id"] = "external change"
    assert verify_file_integrity(history_tamper) is IntegrityStatus.MODIFIED


def test_integrity_ignores_json_key_order_and_formatting() -> None:
    payload = attach_file_integrity(Project(name="P").to_dict())
    reordered = dict(reversed(list(payload.items())))
    reparsed = json.loads(json.dumps(reordered, indent=8, ensure_ascii=False))
    assert verify_file_integrity(reparsed) is IntegrityStatus.VERIFIED


def test_v2_without_hash_is_modified() -> None:
    assert verify_file_integrity(Project(name="P").to_dict()) is IntegrityStatus.MODIFIED


def test_modified_file_opens_current_project_and_discards_history(tmp_path) -> None:
    session = ProjectSession(_tr)
    session.add_inv("INV1")
    path = tmp_path / "project.json"
    session.save_project_as(str(path))

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["name"] = "Externally edited"
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = ProjectSession(_tr)
    status = restored.open_project(str(path))
    assert status is IntegrityStatus.MODIFIED
    assert restored.project.name == "Externally edited"
    assert restored.version_manager.current_id == "_root"
    assert len(restored.version_manager.get_tree()[0]["children"]) == 0


def test_negative_box_positions_require_confirmation_and_load_as_zero(
    tmp_path,
) -> None:
    project = Project(
        name="Needs repair",
        invs=[Inv("INV1", nodes=[Node("N1"), Node("N2")])],
    )
    payload = project.to_dict()
    payload["invs"][0]["nodes"][0]["box_position"] = -1
    payload["invs"][0]["nodes"][1]["box_position"] = -2.5
    path = tmp_path / "negative-box-position.json"
    path.write_text(
        json.dumps(attach_file_integrity(payload)),
        encoding="utf-8",
    )

    session = ProjectSession(_tr)
    previous_project = session.project
    with pytest.raises(NegativeBoxPositionError) as raised:
        session.open_project(str(path))

    assert raised.value.count == 2
    assert session.project is previous_project
    assert session.current_file is None

    status = session.open_project(
        str(path),
        normalize_negative_box_positions=True,
    )
    assert status is IntegrityStatus.VERIFIED
    assert [
        node.box_position for node in session.project.invs[0].nodes
    ] == [0.0, 0.0]


def test_unsigned_legacy_history_is_read_only_across_resave(tmp_path) -> None:
    project = Project(name="Legacy")
    manager = VersionManager()
    pre = project.to_dict()
    project.invs.append(Inv("INV1"))
    manager.commit(pre, project, "add")
    boundary_id = manager.current_id

    payload = project.to_dict()
    payload["schema_version"] = 1
    payload["version_tree"] = manager.to_dict()
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    session = ProjectSession(_tr)
    assert session.open_project(str(path)) is IntegrityStatus.LEGACY_UNSIGNED
    assert not session.version_manager.can_navigate_to("_root")
    assert not session.version_manager.get_node(boundary_id).locked

    session.add_inv("INV2")
    new_id = session.version_manager.current_id
    assert session.checkout_node(boundary_id)
    assert session.checkout_node(new_id)
    session.save_project()

    reopened = ProjectSession(_tr)
    assert reopened.open_project(str(path)) is IntegrityStatus.VERIFIED
    assert reopened.version_manager.get_node("_root").locked
    assert reopened.version_manager.can_navigate_to(boundary_id)

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from count_and_design.application.export_workflow import ExportWorkflow
from count_and_design.application.project_session import ProjectSession
from count_and_design.application.data_fill import DataFillService, DataFillWorkflow
from count_and_design.application.version_manager import VersionManager
from count_and_design.models import Inv, Project


def _tr(_key: str, fallback: str) -> str:
    return fallback


def test_project_session_owns_file_and_version_state(tmp_path: Path) -> None:
    session = ProjectSession(_tr)
    session.add_inv("INV1")
    target = tmp_path / "project.json"

    session.save_project_as(str(target))

    assert session.current_file == target
    assert session.project.invs[0].name == "INV1"
    payload = session.build_recovery_payload()
    assert payload["invs"][0]["name"] == "INV1"
    assert "version_tree" in payload

    restored = ProjectSession(_tr)
    restored.open_project(str(target))
    assert restored.project.invs[0].name == "INV1"
    assert restored.version_manager.current_id != "_root"


def test_export_workflow_delegates_without_mutating_project() -> None:
    generator = Mock()
    voltage_drop = Mock()
    workflow = ExportWorkflow(generator, voltage_drop)
    project = Project(name="Example")
    result = object()
    generator.build_products.return_value = result

    generated = workflow.generate_products(project)

    assert generated is result
    generator.build_products.assert_called_once_with(
        project,
        progress_callback=None,
    )
    assert project.name == "Example"


def test_export_workflow_coordinates_voltage_report() -> None:
    project = Project()
    result = object()
    report = object()
    voltage_drop = Mock()
    voltage_drop.calculate.return_value = report
    workflow = ExportWorkflow(Mock(), voltage_drop)

    with patch(
        "count_and_design.application.export_workflow.export_voltage_drop_to_xlsx",
        return_value="report.xlsx",
    ) as export:
        path = workflow.export_voltage_drop(result, project, "output")

    assert path == "report.xlsx"
    voltage_drop.calculate.assert_called_once_with(project, result)
    export.assert_called_once_with(
        report,
        project,
        "output",
        progress_callback=None,
    )


def test_data_fill_workflow_applies_one_versioned_change() -> None:
    service = Mock(spec=DataFillService)
    service.build_fake_inv.return_value = Inv(name="INV_fake")
    workflow = DataFillWorkflow(service)
    project = Project(invs=[Inv(name="INV1"), Inv(name="INV_fake")])
    version_manager = Mock(spec=VersionManager)
    target = SimpleNamespace(name="target")
    result = SimpleNamespace(
        constraints=SimpleNamespace(targets=(target,)),
    )

    workflow.apply(project, version_manager, result)

    assert [inv.name for inv in project.invs] == ["INV1", "INV_fake"]
    assert project.data_fill_targets[0] is not target
    version_manager.commit.assert_called_once()
    assert version_manager.commit.call_args.args[2] == "version.fill_data"

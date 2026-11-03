import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QStatusBar

from count_and_design.gui.controllers.ocr_capture import ScreenshotOcrController
from count_and_design.gui.controllers.product_export import ProductExportController
from count_and_design.gui.controllers.project_file import ProjectFileController
from count_and_design.gui.controllers.update_check import UpdateCheckController
from count_and_design.gui.viewmodels import AppViewModel
from count_and_design.application.project_session import NegativeBoxPositionError
from count_and_design.file_integrity import IntegrityStatus, verify_file_integrity
from count_and_design.models import Project


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_project_file_controller_writes_unsaved_recovery(
    tmp_path: Path,
) -> None:
    _app()
    parent = QMainWindow()
    view_model = AppViewModel()
    view_model.add_inv("INV1")
    controller = ProjectFileController(parent, view_model)

    with patch(
        "count_and_design.gui.controllers.project_file.tempfile.gettempdir",
        return_value=str(tmp_path),
    ):
        controller.save_before_update()

    recovery = (
        tmp_path
        / "count-and-design-update"
        / f"pre_update_recovery-{os.getpid()}.json"
    )
    payload = json.loads(recovery.read_text(encoding="utf-8"))
    assert payload["invs"][0]["name"] == "INV1"
    assert "version_tree" in payload
    assert verify_file_integrity(payload) is IntegrityStatus.VERIFIED
    parent.close()


def test_project_file_controller_warns_when_modified_history_is_reset() -> None:
    _app()
    parent = QMainWindow()
    view_model = Mock()
    view_model.open_project.return_value = IntegrityStatus.MODIFIED
    controller = ProjectFileController(parent, view_model)

    with (
        patch(
            "count_and_design.gui.controllers.project_file.QFileDialog.getOpenFileName",
            return_value=("project.json", ""),
        ),
        patch(
            "count_and_design.gui.controllers.project_file.QMessageBox.warning"
        ) as warning,
    ):
        controller.open_project()

    view_model.open_project.assert_called_once_with("project.json")
    warning.assert_called_once()
    parent.close()


def test_project_file_controller_can_cancel_negative_box_position_load() -> None:
    _app()
    parent = QMainWindow()
    view_model = Mock()
    view_model.open_project.side_effect = NegativeBoxPositionError(3)
    controller = ProjectFileController(parent, view_model)

    with (
        patch(
            "count_and_design.gui.controllers.project_file.QFileDialog.getOpenFileName",
            return_value=("project.json", ""),
        ),
        patch(
            "count_and_design.gui.controllers.project_file.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ) as question,
    ):
        controller.open_project()

    view_model.open_project.assert_called_once_with("project.json")
    assert "3" in question.call_args.args[2]
    parent.close()


def test_project_file_controller_confirms_and_normalizes_negative_box_positions() -> None:
    _app()
    parent = QMainWindow()
    view_model = Mock()
    view_model.open_project.side_effect = [
        NegativeBoxPositionError(2),
        IntegrityStatus.VERIFIED,
    ]
    controller = ProjectFileController(parent, view_model)

    with (
        patch(
            "count_and_design.gui.controllers.project_file.QFileDialog.getOpenFileName",
            return_value=("project.json", ""),
        ),
        patch(
            "count_and_design.gui.controllers.project_file.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ),
    ):
        controller.open_project()

    assert view_model.open_project.call_args_list == [
        call("project.json"),
        call("project.json", normalize_negative_box_positions=True),
    ]
    parent.close()


def test_project_file_controller_saves_existing_project() -> None:
    _app()
    parent = QMainWindow()
    view_model = Mock()
    view_model.current_file = "project.json"
    controller = ProjectFileController(parent, view_model)

    with patch(
        "count_and_design.gui.controllers.project_file."
        "QMessageBox.information"
    ):
        controller.save_project()

    view_model.save_project.assert_called_once_with()
    parent.close()


def test_project_file_controller_connects_merge_refresh(
    tmp_path: Path,
) -> None:
    _app()
    parent = QMainWindow()
    source = tmp_path / "source.json"
    Project(name="Imported").to_json(source)
    view_model = Mock()
    view_model.project = Project(name="Current")
    controller = ProjectFileController(parent, view_model)
    merge_window = Mock()

    with (
        patch(
            "count_and_design.gui.controllers.project_file."
            "QFileDialog.getOpenFileName",
            return_value=(str(source), ""),
        ),
        patch(
            "count_and_design.gui.controllers.project_file.MergeWindow",
            return_value=merge_window,
        ),
    ):
        controller.import_and_merge()

    merge_window.merge_finished.connect.assert_called_once_with(
        view_model.refresh_all_tabs
    )
    merge_window.exec.assert_called_once_with()
    parent.close()


def test_product_export_controller_runs_all_export_stages(
    tmp_path: Path,
) -> None:
    _app()
    parent = QMainWindow()
    status = QStatusBar(parent)
    result = SimpleNamespace(
        parts=[],
        semi_parts=[],
        ibex_products=[],
        lynx_products=[],
        homerun_extender_products=[],
        gap_extender_products=[],
    )
    view_model = Mock()
    view_model.generate_products.return_value = result
    view_model.export_products_to_xlsx.return_value = str(tmp_path / "parts.xlsx")
    view_model.export_correspondence_table.return_value = str(tmp_path / "corr.xlsx")
    view_model.export_schematics.return_value = SimpleNamespace(
        variant_count=1,
        warnings=(),
    )
    view_model.export_voltage_drop.return_value = str(tmp_path / "voltage.xlsx")
    controller = ProductExportController(parent, status, view_model)

    with (
        patch(
            "count_and_design.gui.controllers.product_export."
            "QFileDialog.getExistingDirectory",
            return_value=str(tmp_path),
        ),
        patch(
            "count_and_design.gui.controllers.product_export."
            "QMessageBox.information"
        ),
        patch(
            "count_and_design.gui.controllers.product_export."
            "QApplication.setOverrideCursor"
        ),
        patch(
            "count_and_design.gui.controllers.product_export."
            "QApplication.restoreOverrideCursor"
        ),
        patch(
            "count_and_design.gui.controllers.product_export."
            "QApplication.processEvents"
        ),
    ):
        controller.run()

    view_model.generate_products.assert_called_once()
    view_model.export_products_to_xlsx.assert_called_once()
    view_model.export_correspondence_table.assert_called_once()
    view_model.export_schematics.assert_called_once()
    view_model.export_voltage_drop.assert_called_once()
    parent.close()


def test_update_controller_skips_disabled_automatic_check() -> None:
    _app()
    parent = QMainWindow()
    controller = UpdateCheckController(parent, Mock())

    with (
        patch(
            "count_and_design.gui.controllers.update_check."
            "is_auto_check_enabled",
            return_value=False,
        ),
        patch.object(controller, "_run") as run,
    ):
        controller.check_silent()

    run.assert_not_called()
    parent.close()


def test_update_controller_reports_manual_check_error() -> None:
    _app()
    parent = QMainWindow()
    controller = UpdateCheckController(parent, Mock())

    with patch(
        "count_and_design.gui.controllers.update_check.QMessageBox.warning"
    ) as warning:
        controller._on_error("network", silent=False)

    warning.assert_called_once()
    parent.close()


def test_ocr_controller_disables_action_before_capture() -> None:
    _app()
    parent = QMainWindow()
    status = QStatusBar(parent)
    action = QAction("OCR", parent)
    controller = ScreenshotOcrController(parent, status, action, Mock())

    with patch(
        "count_and_design.gui.controllers.ocr_capture.QTimer.singleShot"
    ) as single_shot:
        controller.start()

    assert not action.isEnabled()
    single_shot.assert_called_once()
    parent.close()


def test_ocr_controller_reports_success_and_failure() -> None:
    _app()
    parent = QMainWindow()
    status = QStatusBar(parent)
    action = QAction("OCR", parent)
    controller = ScreenshotOcrController(parent, status, action, Mock())

    with (
        patch(
            "count_and_design.gui.controllers.ocr_capture."
            "QMessageBox.information"
        ) as information,
        patch(
            "count_and_design.gui.controllers.ocr_capture."
            "QMessageBox.warning"
        ) as warning,
    ):
        controller._on_finished(SimpleNamespace(text="ok", is_table=False))
        controller._on_failed("failed")

    assert action.isEnabled()
    information.assert_called_once()
    warning.assert_called_once()
    parent.close()

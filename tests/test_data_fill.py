import copy
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHeaderView, QLineEdit

from count_and_design.application.catalog import RackingsService
from count_and_design.application.data_fill import (
    DataFillConstraints,
    DataFillError,
    DataFillFormulaError,
    DataFillService,
    format_data_fill_formula,
    parse_data_fill_formula,
)
from count_and_design.gui.viewmodels import AppViewModel
from count_and_design.gui.views.data_fill_dialog import (
    DataFillDialog,
    EnabledCheckDelegate,
    FormulaItemDelegate,
    FormulaTextEdit,
    TargetTableWidget,
)
from count_and_design.models import (
    DataFillTarget,
    Inv,
    Length,
    Node,
    ProductPreset,
    Project,
    RackingEntry,
    Rackings,
    Row,
)


def _node(name: str, racking: str, row_name: str = "Row1") -> Node:
    return Node(
        name=name,
        rows=[Row(name=row_name, racking_entries=[RackingEntry(rackings=racking)])],
    )


def _project() -> Project:
    project = Project(
        name="Fill Test",
        project_type="IBEX",
        racking_pitch=Length("5mm"),
        rackings_list=[
            Rackings(name="RA", product_presets="A"),
            Rackings(name="RB", product_presets="B"),
        ],
        product_presets=[
            ProductPreset(name="A", branch_count=2),
            ProductPreset(name="B", branch_count=3),
        ],
    )
    inv = Inv(name="INV1", racking_pitch=Length("10mm"))
    inv.nodes = [_node("Node1", "RA"), _node("Node2", "RA", "Different row name")]
    project.invs = [inv]
    project.rebind_length_identifiers()
    return project


def _constraints(project: Project, ordered: list[tuple[str, int]]) -> DataFillConstraints:
    targets = copy.deepcopy(list(DataFillService().prepare(project).targets))
    by_label = {target.label: target for target in targets}
    selected: list[DataFillTarget] = []
    selected_ids: set[str] = set()
    for label, value in ordered:
        target = by_label[label]
        target.enabled = True
        target.target = value
        selected.append(target)
        selected_ids.add(target.target_id)
    selected.extend(target for target in targets if target.target_id not in selected_ids)
    return DataFillConstraints(tuple(selected))


class DataFillFormulaTests(unittest.TestCase):
    def test_formula_expands_parentheses_and_combines_terms(self) -> None:
        terms = parse_data_fill_formula("= 2*(R[RA] + P[A]) - R[RA]")
        self.assertEqual(format_data_fill_formula(terms), "R[RA] + 2*P[A]")

    def test_formula_supports_negative_terms_and_escaped_names(self) -> None:
        terms = parse_data_fill_formula(r"R[A\]B] - 2*P[C\\D]")
        self.assertEqual(format_data_fill_formula(terms), r"R[A\]B] - 2*P[C\\D]")

    def test_formula_treats_line_breaks_as_whitespace(self) -> None:
        terms = parse_data_fill_formula("R[RA] +\n2*P[A]")
        self.assertEqual(format_data_fill_formula(terms), "R[RA] + 2*P[A]")

    def test_formula_rejects_unsupported_constructs(self) -> None:
        cases = {
            "": "formula_empty",
            "R[RA]*P[A]": "formula_nonlinear",
            "R[RA]/2": "formula_nonlinear",
            "1.5*R[RA]": "formula_invalid_coefficient",
            "UNKNOWN": "formula_unknown_reference",
            "R[RA]+1": "formula_syntax",
        }
        for expression, code in cases.items():
            with self.subTest(expression=expression):
                with self.assertRaises(DataFillFormulaError) as ctx:
                    parse_data_fill_formula(expression)
                self.assertEqual(ctx.exception.code, code)


class DataFillServiceTests(unittest.TestCase):
    def test_prepare_uses_all_real_invs_excludes_fake_and_builds_targets(self) -> None:
        project = _project()
        second = Inv(name="INV2", racking_pitch=Length("10mm"), nodes=[_node("N3", "RA")])
        fake = Inv(name="INV_fake", nodes=[_node("Old fake", "RB")])
        project.invs.extend((second, fake))

        prepared = DataFillService().prepare(project)

        self.assertEqual(prepared.current.node_count, 3)
        self.assertEqual(prepared.current.string_count, 6)
        self.assertEqual(prepared.current.rackings, {"RA": 3})
        self.assertEqual(len(prepared.templates), 1)
        self.assertEqual(prepared.templates[0].sample_count, 3)
        self.assertEqual(
            [target.label for target in prepared.targets],
            ["NODE", "STRINGS", "R[RA]", "R[RB]", "P[A]", "P[B]"],
        )

    def test_effective_inheritance_is_part_of_template_identity(self) -> None:
        project = _project()
        project.invs[0].nodes = [_node("N1", "RA")]
        project.invs.append(Inv(name="INV2", racking_pitch=Length("20mm"), nodes=[_node("N2", "RA")]))

        prepared = DataFillService().prepare(project)

        self.assertEqual(len(prepared.templates), 2)
        pitches = {item.node.racking_pitch.length_str for item in prepared.templates}
        self.assertEqual(pitches, {"10mm", "20mm"})

    def test_exact_solution_preserves_sample_proportion(self) -> None:
        project = _project()
        result = DataFillService(max_time_seconds=2).solve(
            project,
            _constraints(project, [("NODE", 4), ("STRINGS", 8), ("R[RA]", 4), ("P[A]", 4)]),
        )
        self.assertTrue(result.exact)
        self.assertEqual(result.added_node_count, 2)
        self.assertEqual(result.actual.node_count, 4)

    def test_row_order_defines_strict_priority(self) -> None:
        project = _project()
        service = DataFillService(max_time_seconds=2)
        node_first = service.solve(project, _constraints(project, [("NODE", 3), ("STRINGS", 100)]))
        strings_first = service.solve(project, _constraints(project, [("STRINGS", 100), ("NODE", 3)]))
        self.assertEqual(node_first.actual.node_count, 3)
        self.assertEqual(strings_first.actual.string_count, 100)
        self.assertGreater(strings_first.actual.node_count, 3)

    def test_fallback_score_uses_the_same_row_priority(self) -> None:
        project = _project()
        service = DataFillService(max_time_seconds=2)
        prepared = service.prepare(project)
        node_first = _constraints(project, [("NODE", 3), ("STRINGS", 100)])
        strings_first = _constraints(project, [("STRINGS", 100), ("NODE", 3)])
        node_counts = service._fallback_solution(prepared, node_first)
        string_counts = service._fallback_solution(prepared, strings_first)
        self.assertEqual(sum(node_counts), 1)
        self.assertEqual(sum(string_counts), 48)

    def test_composite_target_compiles_current_and_node_contributions(self) -> None:
        project = _project()
        terms = parse_data_fill_formula("R[RA] + R[RB] + P[A] + 3*P[B]")
        custom = DataFillTarget("custom:1", "Combined", terms, target=8, enabled=True)
        result = DataFillService(max_time_seconds=2).solve(
            project, DataFillConstraints((custom,))
        )
        row = result.constraint_results[0]
        self.assertEqual(row.current, 4)
        self.assertEqual(row.actual, 8)
        self.assertEqual(result.added_node_count, 2)

    def test_negative_coefficients_are_supported(self) -> None:
        project = _project()
        project.invs[0].nodes = [_node("A", "RA"), _node("B", "RB")]
        custom = DataFillTarget(
            "custom:balance",
            "Balance",
            parse_data_fill_formula("R[RA] - 2*R[RB]"),
            target=1,
            enabled=True,
        )
        result = DataFillService(max_time_seconds=2).solve(
            project, DataFillConstraints((custom,))
        )
        self.assertTrue(result.exact)
        self.assertEqual(result.constraint_results[0].actual, 1)

    def test_similarity_is_equal_weighted_and_handles_zero_and_negative_targets(self) -> None:
        project = _project()
        service = DataFillService()
        prepared = service.prepare(project)

        node = DataFillTarget(
            "custom:node", "Node", parse_data_fill_formula("NODE"), 4, True
        )
        strings = DataFillTarget(
            "custom:strings", "Strings", parse_data_fill_formula("STRINGS"), 4, True
        )
        similarity = service.solution_similarity(
            prepared, DataFillConstraints((node, strings)), [0]
        )
        self.assertAlmostEqual(similarity, 75.0)

        zero = DataFillTarget(
            "custom:zero", "Zero", parse_data_fill_formula("NODE"), 0, True
        )
        self.assertEqual(
            service.solution_similarity(prepared, DataFillConstraints((zero,)), [0]),
            0.0,
        )
        negative = DataFillTarget(
            "custom:negative", "Negative", parse_data_fill_formula("-NODE"), -2, True
        )
        self.assertEqual(
            service.solution_similarity(prepared, DataFillConstraints((negative,)), [0]),
            100.0,
        )

    def test_solve_reports_bounded_initial_and_final_similarity(self) -> None:
        project = _project()
        progress: list[float] = []
        result = DataFillService(max_time_seconds=2).solve(
            project,
            _constraints(project, [("NODE", 3)]),
            progress_callback=progress.append,
        )
        self.assertTrue(result.exact)
        self.assertGreaterEqual(len(progress), 2)
        self.assertTrue(all(0.0 <= item <= 100.0 for item in progress))
        self.assertAlmostEqual(progress[0], 100.0 * 2 / 3)
        self.assertEqual(progress[-1], 100.0)

    def test_fallback_reports_candidate_improvements(self) -> None:
        project = _project()
        service = DataFillService()
        prepared = service.prepare(project)
        constraints = _constraints(project, [("NODE", 5)])
        candidates: list[list[int]] = []
        counts = service._fallback_solution(
            prepared,
            constraints,
            progress_callback=lambda value: candidates.append(list(value)),
        )
        self.assertTrue(candidates)
        self.assertEqual(candidates[-1], counts)

    def test_target_below_existing_never_deletes_real_nodes(self) -> None:
        project = _project()
        result = DataFillService(max_time_seconds=2).solve(
            project, _constraints(project, [("NODE", 1), ("STRINGS", 1)])
        )
        self.assertEqual(result.added_node_count, 0)
        self.assertEqual(result.actual.node_count, 2)

    def test_build_fake_inv_deep_copies_and_materializes_inheritance(self) -> None:
        project = _project()
        source = project.invs[0].nodes[0]
        service = DataFillService(max_time_seconds=2)
        result = service.solve(project, _constraints(project, [("NODE", 3)]))
        fake = service.build_fake_inv(result)
        self.assertIsNotNone(fake)
        self.assertEqual(fake.nodes[0].racking_pitch.length_str, "10mm")
        fake.nodes[0].rows[0].racking_entries[0].rackings = "RB"
        self.assertEqual(source.rows[0].racking_entries[0].rackings, "RA")

    def test_validation_errors_are_structured(self) -> None:
        with self.assertRaises(DataFillError) as ctx:
            DataFillConstraints().validate()
        self.assertEqual(ctx.exception.code, "no_constraints")
        with self.assertRaises(DataFillError) as ctx:
            DataFillService().prepare(Project(name="Empty"))
        self.assertEqual(ctx.exception.code, "no_real_invs")

    def test_targets_round_trip_with_project(self) -> None:
        project = _project()
        project.data_fill_targets = [
            DataFillTarget(
                "custom:stored", "Stored", parse_data_fill_formula("R[RA] + 2*P[A]"),
                target=-5, enabled=True,
            )
        ]
        loaded = Project.from_dict(project.to_dict())
        self.assertEqual(loaded.data_fill_targets[0].to_dict(), project.data_fill_targets[0].to_dict())

    def test_racking_rename_updates_custom_and_merges_builtin_targets(self) -> None:
        project = _project()
        targets = list(DataFillService().prepare(project).targets)
        targets[2].enabled = True
        targets[2].target = 10
        targets[3].enabled = True
        targets[3].target = 20
        targets.append(DataFillTarget(
            "custom:r", "Both", parse_data_fill_formula("R[RA] + 2*R[RB]"), 1, True
        ))
        project.data_fill_targets = targets
        service = RackingsService()
        service.rename_rackings(project, project.rackings_list[0], "RB", merge_if_exists=True)
        builtins = [t for t in project.data_fill_targets if t.builtin_ref and t.builtin_ref.kind == "racking"]
        self.assertEqual(len(builtins), 1)
        self.assertEqual(builtins[0].target, 30)
        self.assertEqual(format_data_fill_formula(project.data_fill_targets[-1].terms), "3*R[RB]")


class DataFillViewModelTests(unittest.TestCase):
    def test_apply_replaces_fake_persists_targets_and_records_one_version(self) -> None:
        app = AppViewModel()
        app._project = _project()
        app._project.invs.append(Inv(name="INV_fake", nodes=[_node("Old", "RB")]))
        app._push_project()
        previous_version = app.version_manager.current_id
        constraints = _constraints(app._project, [("NODE", 3)])
        result = app.solve_data_fill(constraints)
        app.apply_data_fill(result)
        self.assertEqual(len([inv for inv in app._project.invs if inv.name == "INV_fake"]), 1)
        self.assertEqual(len(app._project.data_fill_targets), len(constraints.targets))
        self.assertNotEqual(app.version_manager.current_id, previous_version)

    def test_zero_addition_removes_old_fake(self) -> None:
        app = AppViewModel()
        app._project = _project()
        app._project.invs.append(Inv(name="INV_fake", nodes=[_node("Old", "RB")]))
        app._push_project()
        result = app.solve_data_fill(_constraints(app._project, [("NODE", 2)]))
        app.apply_data_fill(result)
        self.assertEqual([inv.name for inv in app._project.invs], ["INV1"])


class DataFillDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self) -> tuple[AppViewModel, DataFillDialog]:
        app = AppViewModel()
        app._project = _project()
        app._push_project()
        return app, DataFillDialog(app, app.prepare_data_fill())

    def test_builtin_formula_is_readonly_and_edit_invalidates_preview(self) -> None:
        app, dialog = self._dialog()
        formula = dialog._target_table.item(0, dialog._FORMULA)
        self.assertFalse(formula.flags() & Qt.ItemFlag.ItemIsEditable)
        dialog._target_table.item(0, dialog._ENABLED).setCheckState(Qt.CheckState.Checked)
        constraints = dialog._collect_constraints()
        constraints.targets[0].target = 3
        result = app.solve_data_fill(constraints)
        dialog._on_result(result)
        self.assertTrue(dialog._apply.isEnabled())
        dialog._target_table.item(0, dialog._TARGET).setText("4")
        self.assertFalse(dialog._apply.isEnabled())
        dialog.close()

    def test_custom_formula_inline_validation_and_reordering(self) -> None:
        _app, dialog = self._dialog()
        dialog._add_custom_target()
        row = 0
        self.assertEqual(dialog._selected_row(), 0)
        self.assertEqual(dialog._target_table.currentColumn(), dialog._NAME)
        name_editor = dialog._target_table.findChild(QLineEdit)
        self.assertIsNotNone(name_editor)
        self.assertTrue(dialog._targets[0].enabled)
        self.assertEqual(
            dialog._target_table.item(0, dialog._ENABLED).checkState(),
            Qt.CheckState.Checked,
        )
        dialog._target_table.item(row, dialog._NAME).setText("Combined")
        dialog._target_table.item(row, dialog._FORMULA).setText("R[RA] +\n2*P[A]")
        self.assertTrue(dialog._calculate.isEnabled())
        target_id = dialog._targets[row].target_id
        constraints = dialog._collect_constraints()
        self.assertIn("\n", dialog._formula_text[target_id])
        self.assertEqual(
            format_data_fill_formula(constraints.targets[0].terms),
            "R[RA] + 2*P[A]",
        )
        dialog._target_table.selectRow(row)
        dialog._move_selected_target(1)
        self.assertEqual(dialog._targets[row + 1].target_id, target_id)
        dialog._target_table.item(row + 1, dialog._FORMULA).setText("R[missing]")
        self.assertFalse(dialog._calculate.isEnabled())
        self.assertTrue(dialog._target_table.item(row + 1, dialog._FORMULA).toolTip())
        dialog.close()

    def test_formula_editor_pairs_references_and_supports_manual_typing(self) -> None:
        editor = FormulaTextEdit(("RA", "R]B"), ("A",), None)
        editor.show()
        editor.setFocus()

        QTest.keyClicks(editor, "R")
        self.assertEqual(editor.text(), "R[]")
        self.assertEqual(editor.cursorPosition(), 2)
        editor._insert_completion("R]B")
        self.assertEqual(editor.text(), "R[R\\]B] + ")

        editor.clear()
        QTest.keyClicks(editor, "P")
        self.assertEqual(editor.text(), "P[]")
        self.assertEqual(editor.cursorPosition(), 2)

        editor.clear()
        QTest.keyClicks(editor, "R[RA]")
        self.assertEqual(editor.text(), "R[RA]")

        editor.setText("R[")
        editor.setCursorPosition(2)
        QTest.keyClicks(editor, "r")
        self.assertEqual(editor.text(), "R[r")

        editor.setText("R[RA] +")
        editor.setCursorPosition(len(editor.text()))
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClicks(editor, "P[A]")
        self.assertEqual(editor.text(), "R[RA] +\nP[A]")
        editor.close()

    def test_formula_editor_offers_contextual_names_and_keywords(self) -> None:
        editor = FormulaTextEdit(("RA", "RB"), ("A", "B"), None)
        editor.show()
        editor.setFocus()
        QTest.keyClicks(editor, "R")
        model = editor.completer().completionModel()
        self.assertEqual(
            [model.index(row, 0).data() for row in range(model.rowCount())],
            ["RA", "RB"],
        )

        editor.clear()
        QTest.keyClicks(editor, "N")
        model = editor.completer().completionModel()
        self.assertEqual(
            [model.index(row, 0).data() for row in range(model.rowCount())],
            ["NODE"],
        )
        editor._insert_completion("NODE")
        self.assertEqual(editor.text(), "NODE")

        editor.clear()
        QTest.keyClicks(editor, "R")
        self.app.processEvents()
        popup = editor.completer().popup()
        popup.setCurrentIndex(editor.completer().completionModel().index(1, 0))
        self.assertEqual(
            popup.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.assertGreaterEqual(popup.minimumWidth(), editor.viewport().width())
        QTest.keyClick(popup.viewport(), Qt.Key.Key_Tab)
        self.assertEqual(editor.text(), "R[RB] + ")
        self.assertEqual(editor.cursorPosition(), len(editor.text()))
        self.assertFalse(popup.isVisible())

        editor.clear()
        QTest.keyClicks(editor, "R")
        self.app.processEvents()
        popup.setCurrentIndex(editor.completer().completionModel().index(0, 0))
        editor.completer().activated.emit("RA")
        self.assertEqual(editor.text(), "R[RA] + ")
        self.assertEqual(editor.cursorPosition(), len(editor.text()))
        self.assertFalse(popup.isVisible())

        editor.clear()
        QTest.keyClicks(editor, "P")
        editor.completer().activated.emit("A")
        self.assertEqual(editor.text(), "P[A] + ")
        self.assertEqual(editor.cursorPosition(), len(editor.text()))
        self.assertFalse(editor.completer().popup().isVisible())
        editor.close()

    def test_formula_delegate_commits_and_displays_multiple_lines(self) -> None:
        _app, dialog = self._dialog()
        dialog.show()
        dialog._add_custom_target()
        self.app.processEvents()
        name_editor = dialog._target_table.findChild(QLineEdit)
        self.assertIsNotNone(name_editor)
        name_editor.setText("Combined")
        QTest.keyClick(name_editor, Qt.Key.Key_Tab)
        self.app.processEvents()
        editor = dialog._target_table.findChild(FormulaTextEdit)
        self.assertIsNotNone(editor)
        QTest.keyClicks(editor, "R")
        self.app.processEvents()
        popup = editor.completer().popup()
        popup.setCurrentIndex(editor.completer().completionModel().index(0, 0))
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        self.app.processEvents()
        self.assertIs(dialog._target_table.findChild(FormulaTextEdit), editor)
        self.assertTrue(editor.isVisible())
        self.assertEqual(editor.text(), "R[RA] + ")
        self.assertEqual(editor.cursorPosition(), len(editor.text()))
        editor.clear()
        editor.setPlainText("R[RA] +\nP[A]")
        self.app.processEvents()
        self.assertGreater(
            dialog._target_table.rowHeight(0), dialog._target_table.rowHeight(1)
        )
        QTest.keyClick(
            editor,
            Qt.Key.Key_Return,
            Qt.KeyboardModifier.ControlModifier,
        )
        self.app.processEvents()
        self.assertEqual(
            dialog._target_table.item(0, dialog._FORMULA).text(),
            "R[RA] +\nP[A]",
        )
        self.assertLessEqual(
            dialog._target_table.rowHeight(0),
            dialog._target_table.rowHeight(1) + 2,
        )
        delegate = dialog._target_table.itemDelegateForColumn(dialog._FORMULA)
        self.assertIsInstance(delegate, FormulaItemDelegate)
        self.assertEqual(delegate.displayText("R[RA] +\nP[A]", None), "R[RA] + P[A]")
        dialog.close()

    def test_target_columns_are_interactive_and_template_id_is_hidden(self) -> None:
        app, dialog = self._dialog()
        dialog._apply_initial_target_widths()
        self.assertEqual(dialog._target_table.columnCount(), 5)
        self.assertIsInstance(dialog._target_table, TargetTableWidget)
        self.assertEqual(dialog._target_table._SELECTION_BORDER_WIDTH, 2)
        enabled_delegate = dialog._target_table.itemDelegateForColumn(dialog._ENABLED)
        self.assertIsInstance(enabled_delegate, EnabledCheckDelegate)
        self.assertEqual(dialog._CURRENT, 3)
        self.assertEqual(dialog._TARGET, 4)
        header = dialog._target_table.horizontalHeader()
        for column in range(dialog._target_table.columnCount()):
            self.assertEqual(
                header.sectionResizeMode(column), QHeaderView.ResizeMode.Interactive
            )
        self.assertGreaterEqual(dialog._target_table.rowHeight(0), 28)
        self.assertGreater(
            dialog._target_table.columnWidth(dialog._FORMULA),
            dialog._target_table.columnWidth(dialog._NAME),
        )
        original = dialog._target_table.columnWidth(dialog._NAME)
        dialog._target_table.setColumnWidth(dialog._NAME, original + 20)
        self.assertEqual(dialog._target_table.columnWidth(dialog._NAME), original + 20)
        self.assertIn("border: none", dialog._target_table.styleSheet())
        self.assertNotIn("palette(midlight)", dialog._target_table.styleSheet())
        self.assertEqual(
            dialog._target_table.item(0, dialog._CURRENT).background().color(),
            dialog._target_table.item(0, dialog._TARGET).background().color(),
        )
        self.assertEqual(
            dialog._target_table.item(0, dialog._TARGET).foreground().color(),
            QColor("black"),
        )
        self.assertEqual(
            dialog._target_table.item(0, dialog._CURRENT).foreground().color(),
            QColor("#808080"),
        )

        dialog.show()
        self.app.processEvents()
        enabled_item = dialog._target_table.item(0, dialog._ENABLED)
        initial_state = enabled_item.checkState()
        rect = dialog._target_table.visualItemRect(enabled_item)
        QTest.mouseClick(
            dialog._target_table.viewport(),
            Qt.MouseButton.LeftButton,
            pos=rect.center(),
        )
        self.assertNotEqual(enabled_item.checkState(), initial_state)
        self.assertTrue(enabled_delegate._is_checked(
            dialog._target_table.indexFromItem(enabled_item)
        ))

        constraints = _constraints(app._project, [("NODE", 3)])
        result = app.solve_data_fill(constraints)
        dialog._on_result(result)
        self.assertEqual(dialog._template_results.columnCount(), 4)
        self.assertNotEqual(dialog._template_results.item(0, 0).text(), "T1")
        dialog._on_progress(87.65)
        self.assertIn("87.7%", dialog._status.text())
        dialog._on_result(result)
        self.assertNotIn("87.7%", dialog._status.text())
        dialog.close()

    def test_closing_without_apply_does_not_persist_targets(self) -> None:
        app, dialog = self._dialog()
        dialog._add_custom_target()
        dialog.close()
        self.assertEqual(app._project.data_fill_targets, [])


if __name__ == "__main__":
    unittest.main()

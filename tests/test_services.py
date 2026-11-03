import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from count_and_design.application.ai.ocr.screenshot import (
    DeepSeekOcrService,
    OcrServiceError,
)
from count_and_design.application.catalog import (
    ProductPresetService,
    PVPanelService,
    RackingsService,
)
from count_and_design.application.core import ProjectPersistenceService
from count_and_design.application.project import ProjectStatsService, ProjectTreeService
from count_and_design.models import (
    IdentifierRegistry, Inv, Length, LengthIdentifier, Node, ProductPreset, Project, PVPanelInfo,
    RackingEntry, Rackings, Row,
)
from count_and_design.models.length import LengthError


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Project(name="Demo")
        inv = Inv(name="INV1")
        node = Node(name="Node1")
        row = Row(name="Row1", racking_entries=[RackingEntry(rackings="ABC"), RackingEntry(rackings="XYZ")])
        node.rows.append(row)
        inv.nodes.append(node)
        self.project.invs.append(inv)
        self.project.rackings_list.extend([Rackings(name="ABC", product_presets="AB"), Rackings(name="XYZ", product_presets="C")])

    def test_persistence_round_trip(self) -> None:
        service = ProjectPersistenceService()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.json"
            service.save_project(self.project, path)
            loaded, loaded_path = service.open_project(path)
        self.assertEqual(loaded_path.name, "demo.json")
        self.assertEqual(loaded.to_dict(), self.project.to_dict())

    def test_tree_copy_paste_sequence(self) -> None:
        service = ProjectTreeService()
        clipboard = service.copy_item("row", self.project.invs[0].nodes[0].rows[0])
        created = service.paste_item(self.project, "node", self.project.invs[0].nodes[0], clipboard)
        self.assertIsNotNone(created)
        self.assertEqual(self.project.invs[0].nodes[0].rows[-1].name, "Row2")

    def test_stats_match_expected_counts(self) -> None:
        rows = ProjectStatsService().build_project_statistics(self.project)
        self.assertIn(("INV Count", "1"), rows)
        self.assertIn(("Node Count", "1"), rows)
        self.assertIn(("Row Count", "1"), rows)
        self.assertIn(("  ABC", "1"), rows)
        self.assertIn(("  XYZ", "1"), rows)
        self.assertIn(("  A", "1"), rows)
        self.assertIn(("  C", "1"), rows)

    def test_stats_overview_separates_metrics_and_breakdowns(self) -> None:
        overview = ProjectStatsService().build_project_overview(self.project)

        self.assertIn(("INV Count", "1"), overview.metrics)
        self.assertIn(("String Count", "0"), overview.metrics)
        self.assertEqual(overview.rackings, (("ABC", "1"), ("XYZ", "1")))
        self.assertEqual(overview.product_presets, (("A", "1"), ("B", "1"), ("C", "1")))
        self.assertEqual(overview.pv_panels, ())

    def test_stats_overview_counts_modules_by_pv_panel_type_at_every_level(self) -> None:
        self.project.product_presets = [
            ProductPreset(name="A", branch_count=2, pv_panel="Panel X"),
            ProductPreset(name="B", branch_count=3, pv_panel="Panel X"),
            ProductPreset(name="C", branch_count=4, pv_panel="Panel Y"),
        ]
        self.project.pv_panels = [
            PVPanelInfo(name="Panel X", module_per_string=20),
            PVPanelInfo(name="Panel Y", module_per_string=25),
        ]
        service = ProjectStatsService()
        inv = self.project.invs[0]
        node = inv.nodes[0]

        expected = (("Panel X", "100"), ("Panel Y", "100"))
        self.assertEqual(service.build_project_overview(self.project).pv_panels, expected)
        self.assertEqual(service.build_inv_overview(self.project, inv).pv_panels, expected)
        self.assertEqual(service.build_node_overview(self.project, node).pv_panels, expected)
        self.assertIn(("  Panel X", "100"), service.build_project_statistics(self.project))

    def test_stats_overview_totals_node_cluster_lengths_for_project_and_inv(self) -> None:
        inv = self.project.invs[0]
        inv.nodes[0].cluster_length = Length("1ft")
        inv.nodes.append(Node(name="Node2", cluster_length=Length("2m")))
        self.project.invs.append(
            Inv(name="INV2", nodes=[Node(name="Node3", cluster_length=Length("3inch"))])
        )
        service = ProjectStatsService()

        self.assertEqual(
            service.build_project_overview(self.project).cluster_total_length,
            "7.812ft (2381mm)",
        )
        self.assertEqual(
            service.build_inv_overview(self.project, inv).cluster_total_length,
            "7.562ft (2304.8mm)",
        )
        self.assertIsNone(
            service.build_node_overview(self.project, inv.nodes[0]).cluster_total_length
        )
        self.assertIn(
            ("Cluster Total Length", "7.812ft (2381mm)"),
            service.build_project_statistics(self.project),
        )

    def test_empty_stats_overview_has_no_breakdown_placeholders(self) -> None:
        overview = ProjectStatsService().build_project_overview(Project(name="Empty"))

        self.assertIn(("INV Count", "0"), overview.metrics)
        self.assertEqual(overview.rackings, ())
        self.assertEqual(overview.product_presets, ())
        self.assertEqual(overview.pv_panels, ())
        self.assertEqual(overview.cluster_total_length, "0.000ft (0mm)")

    def test_detail_rows_include_each_child_string_count(self) -> None:
        self.project.product_presets = [
            ProductPreset(name="A", branch_count=2),
            ProductPreset(name="B", branch_count=3),
            ProductPreset(name="C", branch_count=4),
        ]
        service = ProjectStatsService()
        inv = self.project.invs[0]
        node = inv.nodes[0]

        project_rows = service.build_project_detail_rows(self.project)
        inv_rows = service.build_inv_detail_rows(self.project, inv)
        node_headers, node_rows = service.build_node_detail_rows(self.project, node)

        self.assertEqual(project_rows[0][-3], "9")
        self.assertEqual(inv_rows[0][-3], "9")
        self.assertEqual(node_headers[-3], "Strings")
        self.assertEqual(node_rows[0][-3], "9")

    def test_node_detail_rows_include_racking_offsets(self) -> None:
        node = self.project.invs[0].nodes[0]
        node.rows[0].racking_entries[0].offset = Length("1ft")
        node.rows[0].racking_entries[1].offset = Length("2m+3inch")

        _headers, rows = ProjectStatsService().build_node_detail_rows(
            self.project, node
        )

        self.assertEqual(rows[0][1:3], ("ABC(1ft)", "XYZ(2m+3inch)"))

    def test_node_detail_rows_include_box_position(self) -> None:
        node = self.project.invs[0].nodes[0]
        node.box_position = 2.5

        inv_rows = ProjectStatsService().build_inv_detail_rows(
            self.project, self.project.invs[0]
        )

        self.assertEqual(inv_rows[0][5], "2.5")

    def test_rackings_rename_and_cleanup(self) -> None:
        service = RackingsService()
        result = service.rename_rackings(self.project, self.project.rackings_list[0], "DEF")
        self.assertEqual(result.status, "renamed")
        self.assertEqual(self.project.invs[0].nodes[0].rows[0].racking_entries[0].rackings, "DEF")
        self.project.rackings_list.append(Rackings(name="UNUSED", product_presets=""))
        removed = service.cleanup_unused_rackings(self.project)
        self.assertEqual([item.name for item in removed], ["UNUSED"])

    def test_pv_panel_service_creates_reuses_and_cleans_only_unused_panels(self) -> None:
        service = PVPanelService()
        self.project.product_presets = [ProductPreset(name="A", pv_panel="Used")]
        used = service.ensure_exists(self.project, "  Used  ")
        duplicate = service.ensure_exists(self.project, "Used")
        unused = service.ensure_exists(self.project, "Unused")

        self.assertIs(used, duplicate)
        self.assertIsNone(service.ensure_exists(self.project, "   "))
        self.assertEqual(service.get_used_panel_names(self.project), {"Used"})
        self.assertEqual(service.count_unused(self.project), 1)

        removed = service.cleanup_unused(self.project)

        self.assertEqual(removed, [unused])
        self.assertEqual(self.project.pv_panels, [PVPanelInfo(name="Used")])

    def test_rackings_rename_allows_arbitrary_string_names(self) -> None:
        service = RackingsService()
        result = service.rename_rackings(self.project, self.project.rackings_list[0], "Rack 01-alpha_beta")
        self.assertEqual(result.status, "renamed")
        self.assertEqual(self.project.rackings_list[0].name, "Rack 01-alpha_beta")
        self.assertEqual(self.project.invs[0].nodes[0].rows[0].racking_entries[0].rackings, "Rack 01-alpha_beta")

    def test_rackings_rename_trims_name_without_forcing_uppercase(self) -> None:
        service = RackingsService()
        result = service.rename_rackings(self.project, self.project.rackings_list[0], "  rack-a  ")
        self.assertEqual(result.status, "renamed")
        self.assertEqual(self.project.rackings_list[0].name, "rack-a")

    def test_rackings_rename_requires_confirmation_before_merge(self) -> None:
        service = RackingsService()
        result = service.rename_rackings(self.project, self.project.rackings_list[0], "XYZ")
        self.assertEqual(result.status, "needs_confirmation")
        self.assertEqual(len(self.project.rackings_list), 2)

        merged = service.rename_rackings(self.project, self.project.rackings_list[0], "XYZ", merge_if_exists=True)
        self.assertEqual(merged.status, "merged")
        self.assertEqual(len(self.project.rackings_list), 1)
        self.assertEqual(self.project.rackings_list[0].name, "XYZ")
        row = self.project.invs[0].nodes[0].rows[0]
        self.assertEqual(row.racking_entries[0].rackings, "XYZ")
        self.assertEqual(row.racking_entries[1].rackings, "XYZ")

    def test_rackings_model_allows_arbitrary_string_name(self) -> None:
        rackings = Rackings(name="rack 01-alpha_beta", product_presets="AB")
        self.assertEqual(rackings.name, "rack 01-alpha_beta")

    def test_product_service_sync_and_filter(self) -> None:
        service = ProductPresetService()
        product_presets = service.get_display_product_presets(self.project)
        self.assertEqual([product.name for product in product_presets], ["A", "B", "C"])
        self.assertEqual([product.name for product in self.project.product_presets], ["A", "B", "C"])

    def test_length_expression_supports_identifier_sections(self) -> None:
        identifiers = [
            LengthIdentifier(name="S", length=Length("100mm")),
            LengthIdentifier(name="R", length=Length("200mm")),
        ]
        for identifier in identifiers:
            identifier.length.set_identifiers(identifiers)

        length = Length("1S2R3.3inch", identifiers=identifiers)

        self.assertAlmostEqual(length.to_mm(), 100 + 2 * 200 + 3.3 * 25.4)

    def test_identifier_registry_exposes_latest_identifier_values(self) -> None:
        identifier = LengthIdentifier(name="Motor", length=Length("3ft"))
        registry = IdentifierRegistry([identifier])
        identifier.length.set_registry(registry)
        length = Length("1Motor", registry=registry)

        self.assertAlmostEqual(length.to_ft(), 3.0)
        identifier.length.set_length_str("5ft")
        self.assertAlmostEqual(length.to_ft(), 5.0)

        replacement = LengthIdentifier(name="Motor", length=Length("7ft"))
        registry.replace_identifiers([replacement])
        replacement.length.set_registry(registry)
        self.assertAlmostEqual(length.to_ft(), 7.0)

    def test_project_registry_tracks_add_remove_and_round_trip(self) -> None:
        project = Project()
        preset = ProductPreset(name="M", branch_count=0)
        project.add_product_preset(preset)
        preset.product_total_length.set_length_str("1Motor")

        motor = LengthIdentifier(name="Motor", length=Length("3ft"))
        project.add_length_identifier(motor)
        self.assertAlmostEqual(preset.product_total_length.to_ft(), 3.0)
        self.assertIs(
            preset.product_total_length.registry,
            project.identifier_registry,
        )

        loaded = Project.from_dict(project.to_dict())
        loaded_preset = loaded.get_product_preset("M")
        assert loaded_preset is not None
        self.assertNotIn("identifier_registry", project.to_dict())
        self.assertIs(
            loaded_preset.product_total_length.registry,
            loaded.identifier_registry,
        )
        self.assertAlmostEqual(loaded_preset.product_total_length.to_ft(), 3.0)

        loaded.remove_length_identifier(loaded.length_identifiers[0])
        with self.assertRaises(LengthError) as raised:
            loaded_preset.product_total_length.to_mm()
        self.assertEqual(raised.exception.code, "unknown_identifier")

    def test_length_expression_trailing_operator_is_structured_error(self) -> None:
        for expression in ("100mm+", "100mm-", "100mm*", "100mm/"):
            with self.subTest(expression=expression):
                with self.assertRaises(LengthError) as raised:
                    Length(expression)
                self.assertEqual(raised.exception.code, "evaluation_failed")

    def test_length_expression_rejects_bare_number(self) -> None:
        """Bare numbers are scalars (dim=0); a length expression must resolve to dim=1."""
        identifiers = [LengthIdentifier(name="S", length=Length("100mm"))]
        identifiers[0].length.set_identifiers(identifiers)

        # Bare number alone → dim=0 → DimensionError
        with self.assertRaises(Exception):
            Length("5", identifiers=identifiers)

        # 1S2 → 1S (dim=1) + 2 (dim=0) → implicit addition fails
        with self.assertRaises(Exception):
            Length("1S2", identifiers=identifiers)

        # Explicit: 1S + 2mm works fine
        length = Length("1S+2mm", identifiers=identifiers)
        self.assertAlmostEqual(length.to_mm(), 102.0)

    def test_set_length_str_rejected_input_keeps_original_value(self) -> None:
        """Regression: a failed set_length_str must not corrupt the object.

        Previously length_str was assigned before validation, so a rejected
        value (e.g. bare number '0') stayed in the model with a stale
        length_mm and was later persisted by to_dict(), producing project
        files that fail to load with a dimension error.
        """
        length = Length("3ft")

        with self.assertRaises(LengthError) as raised:
            length.set_length_str("0")  # bare number → dim=0 → dimension error
        self.assertEqual(raised.exception.code, "dimension_error")

        # The invalid expression must not persist on the object or its payload.
        self.assertEqual(length.length_str, "3ft")
        self.assertAlmostEqual(length.length_mm, 3 * 304.8)
        self.assertEqual(
            length.to_dict(),
            {"length_str": "3ft", "length_mm": 3 * 304.8},
        )

        # Tolerant path still works: identifier references may be entered
        # before the identifier exists (resolved lazily).
        length.set_length_str("2S")
        self.assertEqual(length.length_str, "2S")
        self.assertTrue(length._needs_identifier_resolution)

    def test_extract_identifier_refs(self) -> None:
        self.assertEqual(
            Length.extract_identifier_refs("12A + 2B + 5mm"),
            {"A", "B"},
        )
        self.assertEqual(
            Length.extract_identifier_refs("100mm"),
            set(),
        )
        self.assertEqual(
            Length.extract_identifier_refs("1S2R3.3inch"),
            {"S", "R"},
        )
        self.assertEqual(
            Length.extract_identifier_refs("A - B * (C + D)"),
            {"A", "B", "C", "D"},
        )
        # reserved units are not identifiers
        self.assertEqual(
            Length.extract_identifier_refs("5m + 3ft + 2inch"),
            set(),
        )

    def test_cycle_detection_direct_self_reference(self) -> None:
        """B = 12A + 2B should be detected as a cycle."""
        project = Project(name="Test")
        project.length_identifiers = [
            LengthIdentifier(name="A", length=Length("100mm")),
            LengthIdentifier(name="B", length=Length("2A")),
        ]
        project.rebind_length_identifiers()
        # B currently depends on A (no cycle)
        self.assertFalse(
            project.would_create_length_identifier_cycle("B", "3A")
        )
        # B = 12A + 2B → self-reference
        self.assertTrue(
            project.would_create_length_identifier_cycle("B", "12A + 2B")
        )
        # B = 2B → direct self-reference
        self.assertTrue(
            project.would_create_length_identifier_cycle("B", "2B")
        )

    def test_cycle_detection_indirect_mutual(self) -> None:
        """A = B + 1mm, B = A + 2mm should be detected as a cycle."""
        project = Project(name="Test")
        project.length_identifiers = [
            LengthIdentifier(name="A", length=Length("100mm")),
            LengthIdentifier(name="B", length=Length("200mm")),
        ]
        project.rebind_length_identifiers()
        # Set A = B + 1mm (A depends on B, no cycle yet)
        project.length_identifiers[0].length = Length(
            "B + 1mm", identifiers=project.length_identifiers
        )
        project.rebind_length_identifiers()
        # Now B = A + 2mm would create A→B→A cycle
        self.assertTrue(
            project.would_create_length_identifier_cycle("B", "A + 2mm")
        )
        # But B = 300mm is fine
        self.assertFalse(
            project.would_create_length_identifier_cycle("B", "300mm")
        )

    def test_cycle_detection_three_node_cycle(self) -> None:
        """A→B, B→C, C→A should be detected."""
        project = Project(name="Test")
        project.length_identifiers = [
            LengthIdentifier(name="A", length=Length("B")),
            LengthIdentifier(name="B", length=Length("C")),
            LengthIdentifier(name="C", length=Length("100mm")),
        ]
        project.rebind_length_identifiers()
        # A→B→C (no cycle yet)
        self.assertFalse(
            project.would_create_length_identifier_cycle("A", "B")
        )
        # C = A would close the loop: A→B→C→A
        self.assertTrue(
            project.would_create_length_identifier_cycle("C", "A")
        )

    def test_cycle_detection_no_false_positive(self) -> None:
        """DAG without cycles should not be flagged."""
        project = Project(name="Test")
        project.length_identifiers = [
            LengthIdentifier(name="A", length=Length("100mm")),
            LengthIdentifier(name="B", length=Length("2A")),
            LengthIdentifier(name="C", length=Length("A + B")),
        ]
        project.rebind_length_identifiers()
        # All of these are fine (DAG)
        self.assertFalse(
            project.would_create_length_identifier_cycle("B", "2A + 10")
        )
        self.assertFalse(
            project.would_create_length_identifier_cycle("C", "A + 2B")
        )
        self.assertFalse(
            project.would_create_length_identifier_cycle("A", "200mm")
        )

    def test_cycle_detection_unknown_reference_ignored(self) -> None:
        """References to identifiers not in the list should not cause false cycles."""
        project = Project(name="Test")
        project.length_identifiers = [
            LengthIdentifier(name="A", length=Length("100mm")),
        ]
        project.rebind_length_identifiers()
        # A references X which doesn't exist — no cycle
        self.assertFalse(
            project.would_create_length_identifier_cycle("A", "X + 10")
        )

    @staticmethod
    def _chat_response(content: str, status_code: int = 200):
        response = Mock(status_code=status_code, text="")
        response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": content}}]
        }
        return response

    @staticmethod
    def _ocr_service():
        # An explicit model skips the /v1/models discovery request.
        return DeepSeekOcrService(
            base_url="http://ocr.test/v1",
            model="deepseek-ai/DeepSeek-OCR",
        )

    def test_ocr_service_parses_text_response(self) -> None:
        service = self._ocr_service()
        response = self._chat_response("hello world")

        with patch(
            "count_and_design.application.ai.ocr.providers.screenshot.requests.post",
            return_value=response,
        ):
            result = service.recognize(b"png")

        self.assertFalse(result.is_table)
        self.assertEqual(result.text, "hello world")

    def test_ocr_service_sends_chat_completions_request(self) -> None:
        service = self._ocr_service()
        response = self._chat_response("hello")

        with patch(
            "count_and_design.application.ai.ocr.providers.screenshot.requests.post",
            return_value=response,
        ) as mock_post:
            service.recognize(b"png")

        url = mock_post.call_args.args[0]
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(url, "http://ocr.test/v1/chat/completions")
        self.assertEqual(body["model"], "deepseek-ai/DeepSeek-OCR")
        image_part = body["messages"][0]["content"][0]
        self.assertEqual(image_part["type"], "image_url")
        self.assertTrue(
            image_part["image_url"]["url"].startswith("data:image/png;base64,")
        )

    @patch.dict("os.environ", {"COUNT_AND_DESIGN_OCR_MODEL": ""}, clear=False)
    def test_ocr_service_discovers_model_when_unset(self) -> None:
        service = DeepSeekOcrService(base_url="http://ocr.test/v1")
        models_response = Mock(status_code=200, text="")
        models_response.json.return_value = {"data": [{"id": "auto-model"}]}
        chat_response = self._chat_response("hello")

        with patch(
            "count_and_design.application.ai.ocr.providers.screenshot.requests.get",
            return_value=models_response,
        ) as mock_get, patch(
            "count_and_design.application.ai.ocr.providers.screenshot.requests.post",
            return_value=chat_response,
        ) as mock_post:
            service.recognize(b"png")

        self.assertEqual(mock_get.call_args.args[0], "http://ocr.test/v1/models")
        self.assertEqual(mock_post.call_args.kwargs["json"]["model"], "auto-model")

    def test_ocr_service_converts_html_table_to_tsv(self) -> None:
        service = self._ocr_service()
        html = (
            "<table><tr><td>A</td><td>B</td></tr>"
            "<tr><td>1</td><td>2</td></tr></table>"
        )
        response = self._chat_response(html)

        with patch(
            "count_and_design.application.ai.ocr.providers.screenshot.requests.post",
            return_value=response,
        ):
            result = service.recognize(b"png")

        self.assertTrue(result.is_table)
        self.assertEqual(result.text, "A\tB\n1\t2")

    def test_ocr_service_converts_markdown_table_to_tsv(self) -> None:
        service = self._ocr_service()
        response = self._chat_response("| A | B |\n| --- | --- |\n| 1 | 2 |")

        with patch(
            "count_and_design.application.ai.ocr.providers.screenshot.requests.post",
            return_value=response,
        ):
            result = service.recognize(b"png")

        self.assertTrue(result.is_table)
        self.assertEqual(result.text, "A\tB\n1\t2")

    def test_ocr_service_strips_special_tokens(self) -> None:
        service = self._ocr_service()
        response = self._chat_response("hello world<|end▁of▁sentence|>")

        with patch(
            "count_and_design.application.ai.ocr.providers.screenshot.requests.post",
            return_value=response,
        ):
            result = service.recognize(b"png")

        self.assertFalse(result.is_table)
        self.assertEqual(result.text, "hello world")

    def test_ocr_service_wraps_network_errors(self) -> None:
        service = self._ocr_service()

        with patch(
            "count_and_design.application.ai.ocr.providers.screenshot.requests.post",
            side_effect=requests.RequestException("boom"),
        ):
            with self.assertRaises(OcrServiceError):
                service.recognize(b"png")

    def test_ocr_service_raises_for_http_error(self) -> None:
        service = self._ocr_service()
        response = Mock(status_code=500, text="server error")

        with patch(
            "count_and_design.application.ai.ocr.providers.screenshot.requests.post",
            return_value=response,
        ):
            with self.assertRaises(OcrServiceError):
                service.recognize(b"png")


if __name__ == "__main__":
    unittest.main()

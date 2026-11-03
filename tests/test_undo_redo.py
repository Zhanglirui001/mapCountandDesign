"""Comprehensive tests for the version management system.

Covers:
1. VersionManager core unit tests (commit, checkout, tree structure, diffs)
2. @versionable decorator behaviour
3. AppViewModel version navigation integration
4. CountTabViewModel @versionable methods
5. LengthIdentifiersTabViewModel versioning
6. RackingsTabViewModel versioning
7. ProductPresetsTabViewModel versioning
8. Branching (fork) and multi-step sequences
9. Serialization round-trip
10. Edge cases
"""

import os
import unittest
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from count_and_design.application.version_manager import (
    VersionManager,
    versionable,
    _compute_diff,
    _apply_diff,
    _invert_diff,
    _compute_patch,
    _apply_patch,
)
from count_and_design.gui.viewmodels import AppViewModel
from count_and_design.models import (
    Inv,
    Length,
    LengthIdentifier,
    Node,
    ProductPreset,
    Project,
    RackingEntry,
    Rackings,
    Row,
)


# ============================================================================
# 1. Diff Utilities
# ============================================================================


class DiffUtilityTests(unittest.TestCase):
    """Test the low-level diff / apply / invert functions."""

    def test_simple_field_change(self) -> None:
        old = {"name": "Test", "address": ""}
        new = {"name": "Renamed", "address": ""}
        diff = _compute_diff(old, new)
        self.assertEqual(diff, {"name": "Renamed"})

        _apply_diff(old, diff)
        self.assertEqual(old, new)

    def test_nested_change(self) -> None:
        old = {"name": "Test", "invs": [{"name": "INV1"}]}
        new = {"name": "Test", "invs": [{"name": "INV2"}]}
        diff = _compute_diff(old, new)
        self.assertEqual(diff, {"invs.0.name": "INV2"})

        _apply_diff(old, diff)
        self.assertEqual(old, new)

    def test_list_append(self) -> None:
        old = {"invs": [{"name": "INV1"}]}
        new = {"invs": [{"name": "INV1"}, {"name": "INV2"}]}
        diff = _compute_diff(old, new)
        self.assertEqual(diff, {"invs.1": {"name": "INV2"}})

        _apply_diff(old, diff)
        self.assertEqual(old, new)

    def test_list_remove(self) -> None:
        old = {"invs": [{"name": "INV1"}, {"name": "INV2"}]}
        new = {"invs": [{"name": "INV1"}]}
        diff = _compute_diff(old, new)
        self.assertIn("invs.1", diff)
        self.assertIsNone(diff["invs.1"])

        _apply_diff(old, diff)
        self.assertEqual(old, new)

    def test_invert_diff(self) -> None:
        old = {"name": "A", "value": 1}
        new = {"name": "B", "value": 2}
        fwd = _compute_diff(old, new)
        rev = _invert_diff(fwd, old)
        self.assertEqual(rev, {"name": "A", "value": 1})

    def test_roundtrip_apply_invert(self) -> None:
        old = {"name": "A", "invs": [{"name": "X"}]}
        new = {"name": "B", "invs": [{"name": "X"}, {"name": "Y"}]}
        fwd = _compute_diff(old, new)
        rev = _invert_diff(fwd, old)

        # Apply forward, then reverse
        data = dict(old)
        _apply_diff(data, fwd)
        self.assertEqual(data, new)
        _apply_diff(data, rev)
        self.assertEqual(data, old)

    def test_typed_patch_replaces_reordered_list_atomically(self) -> None:
        old = {"nodes": [
            {"name": "Node3", "rows": [{"name": "A"}, {"name": "B"}, {"name": "C"}]},
            {"name": "Node1", "rows": []},
            {"name": "Node2", "rows": [{"name": "X"}]},
        ]}
        new = {"nodes": [old["nodes"][1], old["nodes"][2], old["nodes"][0]]}
        forward = _compute_patch(old, new)
        reverse = _compute_patch(new, old)

        self.assertEqual(len(forward), 1)
        self.assertEqual(forward[0].op, "replace")
        self.assertEqual(forward[0].path, ("nodes",))
        restored = deepcopy(old)
        _apply_patch(restored, forward)
        self.assertEqual(restored, new)
        _apply_patch(restored, reverse)
        self.assertEqual(restored, old)

    def test_typed_patch_distinguishes_none_from_remove(self) -> None:
        old = {"optional": "value", "removed": 1}
        new = {"optional": None}
        patch = _compute_patch(old, new)
        self.assertEqual({op.op for op in patch}, {"replace", "remove"})
        restored = deepcopy(old)
        _apply_patch(restored, patch)
        self.assertEqual(restored, new)


# ============================================================================
# 2. VersionManager Core Tests
# ============================================================================


class VersionManagerCoreTests(unittest.TestCase):
    """Test VersionManager with bare Project objects."""

    def setUp(self) -> None:
        self.vm = VersionManager()
        self.project = Project(name="Test")

    # -- commit --------------------------------------------------------------

    def test_commit_creates_node(self) -> None:
        pre = self.project.to_dict()
        self.project.name = "Renamed"
        node_id = self.vm.commit(pre, self.project, "rename")
        self.assertIsNotNone(node_id)
        self.assertEqual(self.vm.current_id, node_id)

    def test_commit_updates_tree(self) -> None:
        pre = self.project.to_dict()
        self.project.name = "Renamed"
        self.vm.commit(pre, self.project, "rename")
        tree = self.vm.get_tree()
        self.assertEqual(len(tree[0]["children"]), 1)
        self.assertEqual(tree[0]["children"][0]["description"], "rename")
        self.assertTrue(tree[0]["children"][0]["is_current"])

    def test_multiple_commits_form_chain(self) -> None:
        pre = self.project.to_dict()
        self.project.name = "A"
        self.vm.commit(pre, self.project, "step 1")

        pre = self.project.to_dict()
        self.project.name = "B"
        self.vm.commit(pre, self.project, "step 2")

        tree = self.vm.get_tree()
        child = tree[0]["children"][0]
        self.assertEqual(child["description"], "step 1")
        self.assertFalse(child["is_current"])
        self.assertEqual(len(child["children"]), 1)
        self.assertEqual(child["children"][0]["description"], "step 2")
        self.assertTrue(child["children"][0]["is_current"])

    # -- checkout ------------------------------------------------------------

    def test_checkout_to_ancestor(self) -> None:
        pre = self.project.to_dict()
        self.project.name = "A"
        self.vm.commit(pre, self.project, "step 1")

        pre = self.project.to_dict()
        self.project.name = "B"
        step2_id = self.vm.commit(pre, self.project, "step 2")

        # Navigate back to root
        restored = self.vm.checkout("_root", self.project)
        self.assertEqual(restored.name, "Test")
        self.assertEqual(self.vm.current_id, "_root")

    def test_checkout_to_intermediate(self) -> None:
        pre = self.project.to_dict()
        self.project.name = "A"
        step1_id = self.vm.commit(pre, self.project, "step 1")

        pre = self.project.to_dict()
        self.project.name = "B"
        self.vm.commit(pre, self.project, "step 2")

        restored = self.vm.checkout(step1_id, self.project)
        self.assertEqual(restored.name, "A")
        self.assertEqual(self.vm.current_id, step1_id)

    def test_checkout_same_node_noop(self) -> None:
        pre = self.project.to_dict()
        self.project.name = "A"
        self.vm.commit(pre, self.project, "step 1")
        self.assertFalse(self.vm.can_navigate_to(self.vm.current_id))

    def test_checkout_preserves_complex_structure(self) -> None:
        inv = Inv(name="INV1")
        node = Node(name="Node1")
        node.rows.append(Row(name="Row1"))
        inv.nodes.append(node)
        self.project.invs.append(inv)

        pre = self.project.to_dict()
        self.project.invs[0].name = "INV2"
        self.vm.commit(pre, self.project, "rename INV")

        restored = self.vm.checkout("_root", self.project)
        self.assertEqual(restored.invs[0].name, "INV1")
        self.assertEqual(len(restored.invs[0].nodes), 1)

    # -- branching -----------------------------------------------------------

    def test_branch_creates_fork(self) -> None:
        pre = self.project.to_dict()
        self.project.name = "A"
        step1_id = self.vm.commit(pre, self.project, "step 1")

        pre = self.project.to_dict()
        self.project.name = "B"
        self.vm.commit(pre, self.project, "step 2 -> B")

        # Go back to step 1 and commit a different change
        restored = self.vm.checkout(step1_id, self.project)
        pre = restored.to_dict()
        restored.name = "C"
        self.vm.commit(pre, restored, "step 2 -> C")

        tree = self.vm.get_tree()
        children = tree[0]["children"][0]["children"]
        self.assertEqual(len(children), 2)
        descriptions = {c["description"] for c in children}
        self.assertEqual(descriptions, {"step 2 -> B", "step 2 -> C"})

    # -- clear ---------------------------------------------------------------

    def test_clear_resets_everything(self) -> None:
        pre = self.project.to_dict()
        self.project.name = "A"
        self.vm.commit(pre, self.project, "step 1")
        self.vm.clear()
        tree = self.vm.get_tree()
        self.assertEqual(len(tree[0]["children"]), 0)
        self.assertEqual(self.vm.current_id, "_root")

    # -- serialization -------------------------------------------------------

    def test_to_dict_and_from_dict(self) -> None:
        pre = self.project.to_dict()
        self.project.name = "A"
        self.vm.commit(pre, self.project, "step 1")

        data = self.vm.to_dict()
        vm2 = VersionManager.from_dict(data)
        self.assertEqual(vm2.current_id, self.vm.current_id)
        self.assertEqual(len(vm2._nodes), len(self.vm._nodes))

        # Verify tree is the same
        tree1 = self.vm.get_tree()
        tree2 = vm2.get_tree()
        self.assertEqual(len(tree1[0]["children"]), len(tree2[0]["children"]))

    def test_serialization_roundtrip_with_checkout(self) -> None:
        pre = self.project.to_dict()
        self.project.name = "A"
        step1_id = self.vm.commit(pre, self.project, "step 1")

        pre = self.project.to_dict()
        self.project.name = "B"
        self.vm.commit(pre, self.project, "step 2")

        data = self.vm.to_dict()
        vm2 = VersionManager.from_dict(data)

        # Checkout should work on deserialized VM
        restored = vm2.checkout(step1_id, self.project)
        self.assertEqual(restored.name, "A")

    # -- get_path_to_root ----------------------------------------------------

    def test_get_path_to_root(self) -> None:
        pre = self.project.to_dict()
        self.project.name = "A"
        step1_id = self.vm.commit(pre, self.project, "step 1")

        pre = self.project.to_dict()
        self.project.name = "B"
        step2_id = self.vm.commit(pre, self.project, "step 2")

        path = self.vm.get_path_to_root(step2_id)
        self.assertEqual(path, [step2_id, step1_id, "_root"])


# ============================================================================
# 3. @versionable Decorator Tests
# ============================================================================


class VersionableDecoratorTests(unittest.TestCase):
    """Test the @versionable decorator behaviour."""

    def test_decorator_creates_version_node(self) -> None:
        vm = VersionManager()
        project = Project(name="Test")

        class FakeVM:
            _version_manager = vm
            _project = project

            @versionable("rename")
            def rename(self, new_name: str) -> None:
                self._project.name = new_name

        fvm = FakeVM()
        original_name = fvm._project.name
        fvm.rename("Renamed")

        tree = vm.get_tree()
        self.assertEqual(len(tree[0]["children"]), 1)
        self.assertEqual(tree[0]["children"][0]["description"], "rename")

        # Checkout should restore
        restored = vm.checkout("_root", project)
        self.assertEqual(restored.name, original_name)

    def test_decorator_noop_when_no_manager(self) -> None:
        project = Project(name="Test")

        class FakeVM:
            _project = project

            @versionable("rename")
            def rename(self, new_name: str) -> None:
                self._project.name = new_name

        fvm = FakeVM()
        fvm.rename("Renamed")  # Should not crash

    def test_decorator_noop_when_no_project(self) -> None:
        vm = VersionManager()

        class FakeVM:
            _version_manager = vm

            @versionable("rename")
            def rename(self, new_name: str) -> None:
                pass

        fvm = FakeVM()
        fvm.rename("Renamed")
        tree = vm.get_tree()
        self.assertEqual(len(tree[0]["children"]), 0)

    def test_decorator_preserves_return_value(self) -> None:
        vm = VersionManager()
        project = Project(name="Test")

        class FakeVM:
            _version_manager = vm
            _project = project

            @versionable("doit")
            def doit(self) -> str:
                return "result"

        fvm = FakeVM()
        self.assertEqual(fvm.doit(), "result")

    def test_decorator_preserves_args_and_kwargs(self) -> None:
        vm = VersionManager()
        project = Project(name="Test")

        class FakeVM:
            _version_manager = vm
            _project = project

            @versionable("update")
            def update(self, a: int, b: int = 10) -> int:
                return a + b

        fvm = FakeVM()
        self.assertEqual(fvm.update(5), 15)
        self.assertEqual(fvm.update(5, 20), 25)


# ============================================================================
# 4. AppViewModel Version Navigation
# ============================================================================


class AppViewModelVersionTests(unittest.TestCase):
    """Test version navigation through AppViewModel."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.app_vm = AppViewModel()

    def test_initial_state_root_only(self) -> None:
        tree = self.app_vm.version_manager.get_tree()
        self.assertEqual(len(tree[0]["children"]), 0)

    def test_add_inv_creates_version(self) -> None:
        self.app_vm.add_inv("INV1")
        tree = self.app_vm.version_manager.get_tree()
        self.assertEqual(len(tree[0]["children"]), 1)
        self.assertEqual(len(self.app_vm._project.invs), 1)

    def test_checkout_reverts_add_inv(self) -> None:
        self.app_vm.add_inv("INV1")
        self.assertEqual(len(self.app_vm._project.invs), 1)
        self.app_vm.checkout_node("_root")
        self.assertEqual(len(self.app_vm._project.invs), 0)

    def test_checkout_to_child_and_back(self) -> None:
        self.app_vm.add_inv("INV1")
        tree = self.app_vm.version_manager.get_tree()
        child_id = tree[0]["children"][0]["id"]

        # Add another INV
        self.app_vm.add_inv("INV2")
        self.assertEqual(len(self.app_vm._project.invs), 2)

        # Checkout back to first child
        self.app_vm.checkout_node(child_id)
        self.assertEqual(len(self.app_vm._project.invs), 1)
        self.assertEqual(self.app_vm._project.invs[0].name, "INV1")

    def test_branching_after_checkout(self) -> None:
        self.app_vm.add_inv("INV1")
        tree = self.app_vm.version_manager.get_tree()
        child_id = tree[0]["children"][0]["id"]

        self.app_vm.add_inv("INV2")
        self.assertEqual(len(self.app_vm._project.invs), 2)

        # Checkout to first child, then add INV3 (creates fork)
        self.app_vm.checkout_node(child_id)
        self.app_vm.add_inv("INV3")
        self.assertEqual(len(self.app_vm._project.invs), 2)
        self.assertEqual(self.app_vm._project.invs[1].name, "INV3")

        # Tree should now have two branches
        tree = self.app_vm.version_manager.get_tree()
        children = tree[0]["children"][0]["children"]
        self.assertEqual(len(children), 2)

    def test_file_operations_reset_version_tree(self) -> None:
        self.app_vm.add_inv("INV1")
        tree = self.app_vm.version_manager.get_tree()
        self.assertEqual(len(tree[0]["children"]), 1)
        self.app_vm.new_project()
        tree = self.app_vm.version_manager.get_tree()
        self.assertEqual(len(tree[0]["children"]), 0)

    def test_checkout_invalid_node_does_nothing(self) -> None:
        self.app_vm.add_inv("INV1")
        original_count = len(self.app_vm._project.invs)
        self.app_vm.checkout_node("nonexistent")
        self.assertEqual(len(self.app_vm._project.invs), original_count)


# ============================================================================
# 5. CountTabViewModel @versionable Methods
# ============================================================================


class CountTabVersionTests(unittest.TestCase):
    """Test that @versionable methods create version nodes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.app_vm = AppViewModel()
        self.count_vm = self.app_vm.count_tab
        self.vm = self.app_vm.version_manager

    def _add_inv_with_node(self) -> None:
        self.app_vm.add_inv("INV1")
        inv = self.app_vm._project.invs[0]
        node = self.count_vm._tree_service.create_child(self.app_vm._project, "inv", inv)
        self.count_vm._tree_service.create_child(self.app_vm._project, "node", node)
        self.count_vm.refresh()

    def _version_count(self) -> int:
        return len(self.vm._nodes)

    # -- update_project_field ------------------------------------------------

    def test_update_project_name_versioned(self) -> None:
        initial_count = self._version_count()
        self.count_vm.update_project_field("name", "Renamed")
        self.assertGreater(self._version_count(), initial_count)

    def test_update_project_name_restorable(self) -> None:
        original_name = self.app_vm._project.name
        self.count_vm.update_project_field("name", "Renamed")
        self.assertEqual(self.app_vm._project.name, "Renamed")
        self.app_vm.checkout_node("_root")
        self.assertEqual(self.app_vm._project.name, original_name)

    def test_update_inv_name_versioned(self) -> None:
        self._add_inv_with_node()
        inv_node_id = self.count_vm.state.tree[0].children[0].node_id
        self.count_vm.select_item(inv_node_id)
        initial_count = self._version_count()
        self.count_vm.update_inv_field("name", "INV_Renamed")
        self.assertGreater(self._version_count(), initial_count)
        self.assertEqual(self.app_vm._project.invs[0].name, "INV_Renamed")

    def test_update_node_name_versioned(self) -> None:
        self._add_inv_with_node()
        node_id = self.count_vm.state.tree[0].children[0].children[0].node_id
        self.count_vm.select_item(node_id)
        initial_count = self._version_count()
        self.count_vm.update_node_field("name", "Node_Renamed")
        self.assertGreater(self._version_count(), initial_count)

    def test_create_child_versioned(self) -> None:
        self._add_inv_with_node()
        inv_node_id = self.count_vm.state.tree[0].children[0].node_id
        self.count_vm.select_item(inv_node_id)
        node_count_before = len(self.app_vm._project.invs[0].nodes)
        initial_count = self._version_count()
        self.count_vm.create_child()
        self.assertEqual(len(self.app_vm._project.invs[0].nodes), node_count_before + 1)
        self.assertGreater(self._version_count(), initial_count)

    def test_delete_selected_versioned(self) -> None:
        self._add_inv_with_node()
        self.app_vm._project.invs[0].nodes[0].rows.append(Row(name="Row2"))
        self.count_vm.refresh()
        row_id = self.count_vm.state.tree[0].children[0].children[0].children[0].node_id
        self.count_vm.select_item(row_id)
        initial_count = self._version_count()
        self.count_vm.delete_selected()
        self.assertGreater(self._version_count(), initial_count)


# ============================================================================
# 6. LengthIdentifiersTabViewModel Versioning
# ============================================================================


class LengthIdentifiersTabVersionTests(unittest.TestCase):
    """Test versioning for length identifier operations."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.app_vm = AppViewModel()
        self.vm_len = self.app_vm.length_identifiers_tab
        self.vm = self.app_vm.version_manager

    def test_add_identifier_creates_version(self) -> None:
        initial_count = len(self.vm._nodes)
        self.vm_len.add_identifier()
        self.assertGreater(len(self.vm._nodes), initial_count)

    def test_add_identifier_restorable(self) -> None:
        self.vm_len.add_identifier()
        self.assertEqual(len(self.app_vm._project.length_identifiers), 1)
        self.app_vm.checkout_node("_root")
        self.assertEqual(len(self.app_vm._project.length_identifiers), 0)

    def test_delete_identifier_creates_version(self) -> None:
        self.vm_len.add_identifier()
        self.vm_len.select_item(self.vm_len.state.items[0].item_id)
        initial_count = len(self.vm._nodes)
        self.vm_len.delete_selected()
        self.assertGreater(len(self.vm._nodes), initial_count)

    def test_rename_identifier_creates_version(self) -> None:
        self.vm_len.add_identifier()
        self.vm_len.select_item(self.vm_len.state.items[0].item_id)
        initial_count = len(self.vm._nodes)
        self.vm_len.rename_all("Z")
        self.assertGreater(len(self.vm._nodes), initial_count)

    def test_update_length_creates_version(self) -> None:
        self.vm_len.add_identifier()
        self.vm_len.select_item(self.vm_len.state.items[0].item_id)
        initial_count = len(self.vm._nodes)
        self.vm_len.update_length("150mm")
        self.assertGreater(len(self.vm._nodes), initial_count)

    def test_cycle_blocked_no_version_created(self) -> None:
        """When a cycle is detected, no version node should be created
        because _push_version is called after the cycle check passes."""
        self.vm_len.add_identifier()
        self.vm_len.rename_all("A")
        self.vm_len.add_identifier()
        self.vm_len.select_item(self.vm_len.state.items[1].item_id)
        self.vm_len.rename_all("B")
        # Set A = B
        self.vm_len.select_item(self.vm_len.state.items[0].item_id)
        self.vm_len.update_length("B")
        # Try to set B = A (cycle!)
        self.vm_len.select_item(self.vm_len.state.items[1].item_id)
        node_count_before = len(self.vm._nodes)
        self.vm_len.update_length("A")  # Should fail - error_occurred emitted
        # No new node should have been created
        self.assertEqual(len(self.vm._nodes), node_count_before)


# ============================================================================
# 7. RackingsTabViewModel Versioning
# ============================================================================


class RackingsTabVersionTests(unittest.TestCase):
    """Test versioning for rackings operations."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.app_vm = AppViewModel()
        self.app_vm._project.rackings_list.append(
            Rackings(name="ABC", product_presets="A")
        )
        self.app_vm.rackings_tab.refresh()
        self.vm_rack = self.app_vm.rackings_tab
        self.vm = self.app_vm.version_manager

    def test_rename_rackings_versioned(self) -> None:
        self.vm_rack.select_item(self.vm_rack.state.items[0].item_id)
        initial_count = len(self.vm._nodes)
        self.vm_rack.rename_rackings("DEF")
        self.assertGreater(len(self.vm._nodes), initial_count)

    def test_update_product_presets_versioned(self) -> None:
        self.vm_rack.select_item(self.vm_rack.state.items[0].item_id)
        initial_count = len(self.vm._nodes)
        self.vm_rack.update_product_presets("AB")
        self.assertGreater(len(self.vm._nodes), initial_count)

    def test_cleanup_unused_versioned(self) -> None:
        self.app_vm.add_inv("INV1")
        inv = self.app_vm._project.invs[0]
        node = Node(name="Node1")
        node.rows.append(Row(name="Row1", racking_entries=[RackingEntry(rackings="ABC")]))
        inv.nodes.append(node)
        self.app_vm._project.rackings_list.append(
            Rackings(name="UNUSED", product_presets="")
        )
        self.vm_rack.refresh()
        initial_count = len(self.vm._nodes)
        self.vm_rack.cleanup_unused()
        self.assertGreater(len(self.vm._nodes), initial_count)

    def test_update_gap_extenders_versioned(self) -> None:
        self.vm_rack.select_item(self.vm_rack.state.items[0].item_id)
        initial_count = len(self.vm._nodes)
        self.vm_rack.update_gap_extenders([("GA", 2)])
        self.assertGreater(len(self.vm._nodes), initial_count)


# ============================================================================
# 8. ProductPresetsTabViewModel Versioning
# ============================================================================


class ProductPresetsTabVersionTests(unittest.TestCase):
    """Test versioning for product preset operations."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.app_vm = AppViewModel()
        self.vm_pp = self.app_vm.product_presets_tab
        self.vm = self.app_vm.version_manager
        self.vm_pp.refresh()
        if self.vm_pp.state.items:
            self.vm_pp.select_product(self.vm_pp.state.items[0].item_id)

    def test_update_product_type_versioned(self) -> None:
        if not self.vm_pp.state.items:
            self.skipTest("No presets available")
        initial_count = len(self.vm._nodes)
        self.vm_pp.update_product_type("IBEX")
        self.assertGreater(len(self.vm._nodes), initial_count)

    def test_update_remarks_versioned(self) -> None:
        if not self.vm_pp.state.items:
            self.skipTest("No presets available")
        initial_count = len(self.vm._nodes)
        self.vm_pp.update_remarks("Test remark")
        self.assertGreater(len(self.vm._nodes), initial_count)

    def test_update_branch_count_versioned(self) -> None:
        if not self.vm_pp.state.items:
            self.skipTest("No presets available")
        initial_count = len(self.vm._nodes)
        self.vm_pp.update_branch_count("3")
        self.assertGreater(len(self.vm._nodes), initial_count)

    def test_update_branch_cu_wire_versioned(self) -> None:
        if not self.vm_pp.state.items:
            self.skipTest("No presets available")
        initial_count = len(self.vm._nodes)
        self.vm_pp.update_branch_cu_wire("4mm2")
        self.assertGreater(len(self.vm._nodes), initial_count)

    def test_update_cluster_wire_versioned(self) -> None:
        if not self.vm_pp.state.items:
            self.skipTest("No presets available")
        initial_count = len(self.vm._nodes)
        self.vm_pp.update_cluster_wire("240mm2")
        self.assertGreater(len(self.vm._nodes), initial_count)

    def test_cleanup_unused_versioned(self) -> None:
        pp = ProductPreset(name="Z", product_type="IBEX")
        self.app_vm._project.product_presets.append(pp)
        self.vm_pp.refresh()
        initial_count = len(self.vm._nodes)
        self.vm_pp.cleanup_unused()
        self.assertGreater(len(self.vm._nodes), initial_count)


# ============================================================================
# 9. Multi-Step and Branching Sequences
# ============================================================================


class MultiStepVersionTests(unittest.TestCase):
    """Test complex multi-step and branching scenarios."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.app_vm = AppViewModel()
        self.vm = self.app_vm.version_manager

    def test_cross_tab_version_sequence(self) -> None:
        """Operations across different tabs all create version nodes."""
        self.app_vm.add_inv("INV1")
        self.app_vm.length_identifiers_tab.add_identifier()
        self.vm_len = self.app_vm.length_identifiers_tab
        self.vm_len.select_item(self.vm_len.state.items[0].item_id)
        self.vm_len.update_length("100mm")
        self.app_vm._project.rackings_list.append(Rackings(name="ABC"))
        self.app_vm.rackings_tab.refresh()
        self.app_vm.rackings_tab.select_item(
            self.app_vm.rackings_tab.state.items[0].item_id
        )
        self.app_vm.rackings_tab.update_product_presets("AB")

        tree = self.vm.get_tree()
        # Walk the chain
        node = tree[0]
        steps = 0
        while node["children"]:
            node = node["children"][0]
            steps += 1
        self.assertGreaterEqual(steps, 3)

    def test_checkout_preserves_data_integrity(self) -> None:
        """After checkout, the project should match the target state."""
        self.app_vm.add_inv("INV1")
        inv = self.app_vm._project.invs[0]
        node = Node(name="Node1")
        node.rows.append(Row(name="Row1"))
        inv.nodes.append(node)

        # Record the ID after first INV
        tree = self.vm.get_tree()
        first_commit_id = tree[0]["children"][0]["id"]

        self.app_vm.add_inv("INV2")
        self.assertEqual(len(self.app_vm._project.invs), 2)

        # Checkout back to first commit
        self.app_vm.checkout_node(first_commit_id)
        self.assertEqual(len(self.app_vm._project.invs), 1)
        self.assertEqual(self.app_vm._project.invs[0].name, "INV1")

    def test_branching_with_multiple_checkouts(self) -> None:
        """Create a tree with multiple branches."""
        self.app_vm.add_inv("INV1")
        tree = self.vm.get_tree()
        after_inv1_id = tree[0]["children"][0]["id"]

        # Branch 1: add INV2
        self.app_vm.add_inv("INV2")
        b1_id = self.vm.current_id

        # Go back and branch: add INV3
        self.app_vm.checkout_node(after_inv1_id)
        self.app_vm.add_inv("INV3")
        b2_id = self.vm.current_id

        # Both branches should exist
        tree = self.vm.get_tree()
        children = tree[0]["children"][0]["children"]
        self.assertEqual(len(children), 2)

        # Checkout to branch 1
        self.app_vm.checkout_node(b1_id)
        self.assertEqual(len(self.app_vm._project.invs), 2)
        self.assertEqual(self.app_vm._project.invs[1].name, "INV2")

        # Checkout to branch 2
        self.app_vm.checkout_node(b2_id)
        self.assertEqual(len(self.app_vm._project.invs), 2)
        self.assertEqual(self.app_vm._project.invs[1].name, "INV3")

    def test_repeated_edits_each_create_version(self) -> None:
        """Each edit on the same field creates a new version node."""
        self.app_vm.add_inv("INV1")
        inv_node_id = self.app_vm.count_tab.state.tree[0].children[0].node_id
        self.app_vm.count_tab.select_item(inv_node_id)

        self.app_vm.count_tab.update_inv_field("name", "A")
        self.app_vm.count_tab.update_inv_field("name", "B")
        self.app_vm.count_tab.update_inv_field("name", "C")

        tree = self.vm.get_tree()
        # Walk down to count steps
        node = tree[0]
        steps = 0
        while node["children"]:
            node = node["children"][0]
            steps += 1
        self.assertEqual(steps, 4)  # add_inv + 3 renames


if __name__ == "__main__":
    unittest.main()

"""Merge service – handles merging two projects step by step."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Callable

from ...models import (
    Inv,
    LengthIdentifier,
    ProductPreset,
    Project,
    Rackings,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MergeConflict:
    """A single name-conflict between current and imported items."""

    name: str
    current_item: object
    imported_item: object
    resolution: str = ""  # "keep_current" | "keep_imported" | "rename_current" | "rename_imported" | "keep_both_renamed"
    rename_current_to: str = ""
    rename_imported_to: str = ""


@dataclass
class MergeStepState:
    step_index: int  # 0=length_identifiers, 1=product_presets, 2=rackings_list, 3=invs
    step_label: str
    conflicts: list[MergeConflict] = field(default_factory=list)
    non_conflict_count: int = 0
    selected_conflict_index: int = 0


MergeStep = int
STEP_LENGTH_IDENTIFIERS = 0
STEP_PRODUCT_PRESETS = 1
STEP_RACKINGS_LIST = 2
STEP_INVS = 3
STEP_COUNT = 4




# ---------------------------------------------------------------------------
# MergeService
# ---------------------------------------------------------------------------


class MergeService:
    """Orchestrates a step-by-step merge of an imported project into the
    current project.

    Usage::

        svc = MergeService(current_project, imported_project)
        svc.take_snapshot()

        # Step 1
        conflicts = svc.build_conflicts(STEP_LENGTH_IDENTIFIERS)
        # ... user resolves all conflicts ...
        svc.apply_auto_merge(STEP_LENGTH_IDENTIFIERS)

        # Step 2 … etc.

        # If user abandons:
        svc.revert()
    """

    def __init__(self, current: Project, imported: Project) -> None:
        """*current* is left untouched; all mutations happen on a deep copy.

        Call :meth:`finalise` to write the merged result back to *current*.
        """
        self._original = current
        self._current = copy.deepcopy(current)
        self._imported = imported

    # -- Snapshot / revert ------------------------------------------------

    def take_snapshot(self) -> None:
        """No-op kept for backward compatibility.

        The service already works on a deep copy so an explicit snapshot is
        unnecessary.
        """

    def revert(self) -> None:
        """No-op kept for backward compatibility.

        Discarding the copy is handled by simply letting it go out of scope.
        """

    # -- Conflict detection -----------------------------------------------

    def build_conflicts(self, step: MergeStep) -> list[MergeConflict]:
        """Return the list of name-conflicts for *step*."""
        if step == STEP_LENGTH_IDENTIFIERS:
            return self._conflicts_for(
                self._current.length_identifiers,
                self._imported.length_identifiers,
            )
        elif step == STEP_PRODUCT_PRESETS:
            return self._conflicts_for(
                self._current.product_presets,
                self._imported.product_presets,
            )
        elif step == STEP_RACKINGS_LIST:
            return self._conflicts_for(
                self._current.rackings_list,
                self._imported.rackings_list,
            )
        else:  # STEP_INVS
            return self._conflicts_for(
                self._current.invs,
                self._imported.invs,
            )

    def non_conflict_count(self, step: MergeStep) -> int:
        """How many imported items of *step* can be auto-merged?"""
        if step == STEP_LENGTH_IDENTIFIERS:
            return self._non_conflict_count(
                self._current.length_identifiers,
                self._imported.length_identifiers,
            )
        elif step == STEP_PRODUCT_PRESETS:
            return self._non_conflict_count(
                self._current.product_presets,
                self._imported.product_presets,
            )
        elif step == STEP_RACKINGS_LIST:
            return self._non_conflict_count(
                self._current.rackings_list,
                self._imported.rackings_list,
            )
        else:
            return self._non_conflict_count(
                self._current.invs,
                self._imported.invs,
            )

    def has_any_conflicts(self, step: MergeStep) -> bool:
        return len(self.build_conflicts(step)) > 0

    # -- Non-conflict items -----------------------------------------------

    def get_non_conflict_items(self, step: MergeStep) -> list:
        """Return the list of imported items that have no name conflict."""
        if step == STEP_LENGTH_IDENTIFIERS:
            current_names = {li.name for li in self._current.length_identifiers}
            return [copy.deepcopy(li) for li in self._imported.length_identifiers
                    if li.name not in current_names]
        elif step == STEP_PRODUCT_PRESETS:
            current_names = {pp.name for pp in self._current.product_presets}
            return [copy.deepcopy(pp) for pp in self._imported.product_presets
                    if pp.name not in current_names]
        elif step == STEP_RACKINGS_LIST:
            current_names = {r.name for r in self._current.rackings_list}
            return [copy.deepcopy(r) for r in self._imported.rackings_list
                    if r.name not in current_names]
        else:  # STEP_INVS
            current_names = {inv.name for inv in self._current.invs}
            return [copy.deepcopy(inv) for inv in self._imported.invs
                    if inv.name not in current_names]

    def apply_auto_merge_items(self, step: MergeStep, names: set[str]) -> None:
        """Merge only the specified non-conflicting imported items."""
        if step == STEP_LENGTH_IDENTIFIERS:
            current_names = {li.name for li in self._current.length_identifiers}
            for li in self._imported.length_identifiers:
                if li.name in names and li.name not in current_names:
                    self._current.add_length_identifier(copy.deepcopy(li))
        elif step == STEP_PRODUCT_PRESETS:
            current_names = {pp.name for pp in self._current.product_presets}
            for pp in self._imported.product_presets:
                if pp.name in names and pp.name not in current_names:
                    self._current.add_product_preset(copy.deepcopy(pp))
        elif step == STEP_RACKINGS_LIST:
            current_names = {r.name for r in self._current.rackings_list}
            for r in self._imported.rackings_list:
                if r.name in names and r.name not in current_names:
                    self._current.rackings_list.append(copy.deepcopy(r))
        else:
            current_names = {inv.name for inv in self._current.invs}
            for inv in self._imported.invs:
                if inv.name in names and inv.name not in current_names:
                    self._current.invs.append(copy.deepcopy(inv))

    def resolve_remaining_conflicts_as_keep_current(self, step: MergeStep, conflicts: list[MergeConflict]) -> None:
        """Resolve all unresolved conflicts by keeping the current version."""
        for c in conflicts:
            if not c.resolution:
                c.resolution = "keep_current"
                # No actual mutation needed – current already in place

    # -- Resolve a single conflict ----------------------------------------

    def resolve_conflict(self, step: MergeStep, conflict: MergeConflict) -> None:
        """Apply *conflict.resolution* for a single conflict.

        This is called after the user has chosen a resolution for one
        conflicting pair.  The changes are applied **to the current project**
        (or the imported items are transferred).

        For rename operations, the "all references" renaming logic is
        triggered in the appropriate project.
        """
        if step == STEP_LENGTH_IDENTIFIERS:
            self._resolve_length_identifier(conflict)
        elif step == STEP_PRODUCT_PRESETS:
            self._resolve_product_preset(conflict)
        elif step == STEP_RACKINGS_LIST:
            self._resolve_rackings(conflict)
        else:
            self._resolve_inv(conflict)

    # -- Apply auto-merge (non-conflicting items) -------------------------

    def apply_auto_merge(self, step: MergeStep) -> None:
        """Merge all non-conflicting imported items into the current project."""
        if step == STEP_LENGTH_IDENTIFIERS:
            self._auto_merge_length_identifiers()
        elif step == STEP_PRODUCT_PRESETS:
            self._auto_merge_product_presets()
        elif step == STEP_RACKINGS_LIST:
            self._auto_merge_rackings()
        else:
            self._auto_merge_invs()

    # -- Finalise ---------------------------------------------------------

    def finalise(self) -> None:
        """Called after all steps; write merged result back to the original."""
        self._current.rebind_length_identifiers()
        # Sync product presets from rackings to ensure consistency
        from ..catalog.product_presets import ProductPresetService
        from ..catalog.pv_panels import PVPanelService
        ProductPresetService().sync_product_presets_from_rackings(self._current)
        existing_panel_names = {panel.name for panel in self._current.pv_panels}
        for panel in self._imported.pv_panels:
            if panel.name not in existing_panel_names:
                self._current.pv_panels.append(copy.deepcopy(panel))
                existing_panel_names.add(panel.name)
        PVPanelService().sync_from_product_presets(self._current)

        # Publish the merged copy back to the original project
        self._original.length_identifiers = self._current.length_identifiers
        self._original.product_presets = self._current.product_presets
        self._original.pv_panels = self._current.pv_panels
        self._original.rackings_list = self._current.rackings_list
        self._original.invs = self._current.invs
        self._original.rebind_length_identifiers()

    # ====================================================================
    # Private helpers – conflict detection
    # ====================================================================

    @staticmethod
    def _conflicts_for(
        current_list: list, imported_list: list
    ) -> list[MergeConflict]:
        current_map = {item.name: item for item in current_list}
        imported_map = {item.name: item for item in imported_list}
        common = set(current_map) & set(imported_map)
        return [
            MergeConflict(
                name=name,
                current_item=current_map[name],
                imported_item=imported_map[name],
            )
            for name in sorted(common)
        ]

    @staticmethod
    def _non_conflict_count(
        current_list: list, imported_list: list
    ) -> int:
        current_names = {item.name for item in current_list}
        return sum(1 for item in imported_list if item.name not in current_names)

    # ====================================================================
    # Private helpers – auto-merge
    # ====================================================================

    def _auto_merge_length_identifiers(self) -> None:
        current_names = {li.name for li in self._current.length_identifiers}
        for li in self._imported.length_identifiers:
            if li.name not in current_names:
                self._current.add_length_identifier(copy.deepcopy(li))

    def _auto_merge_product_presets(self) -> None:
        current_names = {pp.name for pp in self._current.product_presets}
        for pp in self._imported.product_presets:
            if pp.name not in current_names:
                self._current.add_product_preset(copy.deepcopy(pp))

    def _auto_merge_rackings(self) -> None:
        current_names = {r.name for r in self._current.rackings_list}
        for r in self._imported.rackings_list:
            if r.name not in current_names:
                self._current.rackings_list.append(copy.deepcopy(r))

    def _auto_merge_invs(self) -> None:
        current_names = {inv.name for inv in self._current.invs}
        for inv in self._imported.invs:
            if inv.name not in current_names:
                self._current.invs.append(copy.deepcopy(inv))

    # ====================================================================
    # Private helpers – conflict resolution
    # ====================================================================

    # -- length_identifiers -----------------------------------------------

    def _resolve_length_identifier(self, c: MergeConflict) -> None:
        cur: LengthIdentifier = c.current_item
        imp: LengthIdentifier = c.imported_item

        if c.resolution == "keep_current":
            # Nothing to do – current already in place, imported discarded
            return

        if c.resolution == "keep_imported":
            # Replace current with imported
            imported = copy.deepcopy(imp)
            assert self._current.identifier_registry is not None
            imported.length.set_registry(self._current.identifier_registry)
            self._replace_in_list(self._current.length_identifiers, cur, imported)
            return

        if c.resolution == "rename_current":
            new_name = c.rename_current_to
            self._current.replace_length_identifier_references(cur.name, new_name)
            cur.name = new_name
            # Now also add the imported item (since it no longer conflicts)
            self._current.add_length_identifier(copy.deepcopy(imp))
            return

        if c.resolution == "rename_imported":
            new_name = c.rename_imported_to
            old_name = imp.name
            # Update references inside the imported project (ProductPresets,
            # Rackings offsets, etc. may reference this identifier).
            self._imported.replace_length_identifier_references(old_name, new_name)
            imp_clone = copy.deepcopy(imp)
            imp_clone.name = new_name
            self._current.add_length_identifier(imp_clone)
            return

        if c.resolution == "keep_both_renamed":
            # Rename current
            if c.rename_current_to:
                self._current.replace_length_identifier_references(cur.name, c.rename_current_to)
                cur.name = c.rename_current_to
            # Rename imported and add
            if c.rename_imported_to:
                self._imported.replace_length_identifier_references(imp.name, c.rename_imported_to)
            imp_clone = copy.deepcopy(imp)
            if c.rename_imported_to:
                imp_clone.name = c.rename_imported_to
            self._current.add_length_identifier(imp_clone)
            return

    # -- product_presets --------------------------------------------------

    def _resolve_product_preset(self, c: MergeConflict) -> None:
        cur: ProductPreset = c.current_item
        imp: ProductPreset = c.imported_item

        if c.resolution == "keep_current":
            return

        if c.resolution == "keep_imported":
            imported = copy.deepcopy(imp)
            self._current.bind_product_preset(imported)
            self._replace_in_list(self._current.product_presets, cur, imported)
            return

        if c.resolution == "rename_current":
            new_name = c.rename_current_to
            self._rename_product_preset_references(
                self._current, cur.name, new_name
            )
            cur.name = new_name
            self._current.add_product_preset(copy.deepcopy(imp))
            return

        if c.resolution == "rename_imported":
            new_name = c.rename_imported_to
            # Update references in the imported project's rackings
            old_name = imp.name
            self._rename_product_preset_references(
                self._imported, old_name, new_name
            )
            imp_clone = copy.deepcopy(imp)
            imp_clone.name = new_name
            self._current.add_product_preset(imp_clone)
            return

        if c.resolution == "keep_both_renamed":
            if c.rename_current_to:
                self._rename_product_preset_references(
                    self._current, cur.name, c.rename_current_to
                )
                cur.name = c.rename_current_to
            imp_clone = copy.deepcopy(imp)
            if c.rename_imported_to:
                old_imp_name = imp.name
                self._rename_product_preset_references(
                    self._imported, old_imp_name, c.rename_imported_to
                )
                imp_clone.name = c.rename_imported_to
            self._current.add_product_preset(imp_clone)
            return

    @staticmethod
    def _rename_product_preset_references(
        project: Project, old_name: str, new_name: str
    ) -> None:
        """Replace *old_name* with *new_name* in every Rackings.product_presets
        string of *project*."""
        for r in project.rackings_list:
            if old_name in r.product_presets:
                r.product_presets = r.product_presets.replace(old_name, new_name)

    # -- rackings_list ----------------------------------------------------

    def _resolve_rackings(self, c: MergeConflict) -> None:
        cur: Rackings = c.current_item
        imp: Rackings = c.imported_item

        if c.resolution == "keep_current":
            return

        if c.resolution == "keep_imported":
            self._replace_in_list(self._current.rackings_list, cur, copy.deepcopy(imp))
            return

        if c.resolution == "rename_current":
            new_name = c.rename_current_to
            self._rename_rackings_references(self._current, cur.name, new_name)
            cur.name = new_name
            self._current.rackings_list.append(copy.deepcopy(imp))
            return

        if c.resolution == "rename_imported":
            new_name = c.rename_imported_to
            imp_clone = copy.deepcopy(imp)
            imp_clone.name = new_name
            self._current.rackings_list.append(imp_clone)
            return

        if c.resolution == "keep_both_renamed":
            if c.rename_current_to:
                self._rename_rackings_references(
                    self._current, cur.name, c.rename_current_to
                )
                cur.name = c.rename_current_to
            imp_clone = copy.deepcopy(imp)
            if c.rename_imported_to:
                imp_clone.name = c.rename_imported_to
            self._current.rackings_list.append(imp_clone)
            return

    @staticmethod
    def _rename_rackings_references(
        project: Project, old_name: str, new_name: str
    ) -> None:
        for inv in project.invs:
            for node in inv.nodes:
                for row in node.rows:
                    for entry in row.racking_entries:
                        if entry.rackings == old_name:
                            entry.rackings = new_name

    # -- invs -------------------------------------------------------------

    def _resolve_inv(self, c: MergeConflict) -> None:
        cur: Inv = c.current_item
        imp: Inv = c.imported_item

        if c.resolution == "keep_current":
            return

        if c.resolution == "keep_imported":
            self._replace_in_list(self._current.invs, cur, copy.deepcopy(imp))
            return

        if c.resolution == "rename_current":
            cur.name = c.rename_current_to
            self._current.invs.append(copy.deepcopy(imp))
            return

        if c.resolution == "rename_imported":
            imp_clone = copy.deepcopy(imp)
            imp_clone.name = c.rename_imported_to
            self._current.invs.append(imp_clone)
            return

        if c.resolution == "keep_both_renamed":
            if c.rename_current_to:
                cur.name = c.rename_current_to
            imp_clone = copy.deepcopy(imp)
            if c.rename_imported_to:
                imp_clone.name = c.rename_imported_to
            self._current.invs.append(imp_clone)
            return

    # -- Utility ----------------------------------------------------------

    @staticmethod
    def _replace_in_list(lst: list, old_item: object, new_item: object) -> None:
        idx = lst.index(old_item)
        lst[idx] = new_item

    # -- Name availability helpers ----------------------------------------

    def is_name_available_for_step(
        self, step: MergeStep, name: str, exclude_current: str | None = None
    ) -> bool:
        """Check whether *name* can be used for a rename in *step*."""
        if step == STEP_LENGTH_IDENTIFIERS:
            existing = {li.name for li in self._current.length_identifiers}
        elif step == STEP_PRODUCT_PRESETS:
            existing = {pp.name for pp in self._current.product_presets}
        elif step == STEP_RACKINGS_LIST:
            existing = {r.name for r in self._current.rackings_list}
        else:
            existing = {inv.name for inv in self._current.invs}

        if exclude_current:
            existing.discard(exclude_current)
        # Also check imported items that haven't been merged yet (for this step)
        # Actually, since we add items to current as we resolve conflicts,
        # checking current names is sufficient.
        return name not in existing

    @staticmethod
    def step_label(step: MergeStep) -> str:
        from ...i18n import t

        labels = [
            t("merge.step.length_identifiers", "Length Identifiers"),
            t("merge.step.product_presets", "Product Presets"),
            t("merge.step.rackings_list", "Rackings List"),
            t("merge.step.invs", "INVs"),
        ]
        return labels[step]

"""Rackings-related business logic."""

from __future__ import annotations

from dataclasses import dataclass

from ...i18n import tr
from ...models import (
    DataFillTarget,
    DataFillTerm,
    ProductPreset,
    Project,
    QuantityRef,
    Rackings,
)
from ..project.validation import ValidationService


@dataclass(frozen=True)
class RackingsRenameResult:
    """Result of a rename operation."""

    status: str
    message: str = ""
    requires_confirmation: bool = False


class RackingsService:
    """Operations for rackings definitions and row references."""

    def __init__(self, validation_service: ValidationService | None = None) -> None:
        self._validation = validation_service or ValidationService()

    def get_used_rackings_names(self, project: Project) -> set[str]:
        used: set[str] = set()
        for inv in project.invs:
            for node in inv.nodes:
                for row in node.rows:
                    for entry in row.racking_entries:
                        if entry.rackings:
                            used.add(entry.rackings)
        return used

    def ensure_rackings_exists(self, project: Project, name: str) -> None:
        if not name:
            return
        if project.get_rackings(name) is None:
            project.rackings_list.append(Rackings(name=name, product_presets=""))

    def replace_rackings_references(self, project: Project, old_name: str, new_name: str) -> None:
        for inv in project.invs:
            for node in inv.nodes:
                for row in node.rows:
                    for entry in row.racking_entries:
                        if entry.rackings == old_name:
                            entry.rackings = new_name
        old_ref = QuantityRef("racking", old_name)
        new_ref = QuantityRef("racking", new_name)
        for target in project.data_fill_targets:
            combined: dict[QuantityRef, int] = {}
            order: list[QuantityRef] = []
            for term in target.terms:
                ref = new_ref if term.ref == old_ref else term.ref
                if ref not in combined:
                    order.append(ref)
                    combined[ref] = 0
                combined[ref] += term.coefficient
            target.terms = [
                DataFillTerm(ref, combined[ref]) for ref in order if combined[ref]
            ]
            if target.builtin_ref == old_ref:
                target.builtin_ref = new_ref

        # A merge can leave two built-in rows referring to the same racking.
        # Keep the earlier priority, add their targets and preserve either enable.
        seen: dict[QuantityRef, DataFillTarget] = {}
        merged_targets: list[DataFillTarget] = []
        for target in project.data_fill_targets:
            ref = target.builtin_ref
            if ref is None or ref not in seen:
                merged_targets.append(target)
                if ref is not None:
                    seen[ref] = target
                continue
            existing = seen[ref]
            existing.target += target.target
            existing.enabled = existing.enabled or target.enabled
        project.data_fill_targets = merged_targets

    def rename_rackings(
        self,
        project: Project,
        rackings: Rackings,
        new_name: str,
        merge_if_exists: bool = False,
    ) -> RackingsRenameResult:
        normalized = self._validation.normalize_rackings_name(new_name)
        error = self._validation.validate_rackings_name(normalized)
        if error is not None:
            return RackingsRenameResult(status="invalid", message=error)

        old_name = rackings.name
        if normalized == old_name:
            return RackingsRenameResult(status="noop")

        duplicate = next(
            (item for item in project.rackings_list if item.name == normalized and item is not rackings),
            None,
        )
        if duplicate is not None and not merge_if_exists:
            return RackingsRenameResult(
                status="needs_confirmation",
                message=tr("rackings_tab.merge_confirm", "Rackings '{name}' already exists.\nDo you want to merge?").format(name=normalized),
                requires_confirmation=True,
            )

        if duplicate is not None:
            self.replace_rackings_references(project, old_name, normalized)
            project.rackings_list.remove(rackings)
            return RackingsRenameResult(status="merged")

        rackings.name = normalized
        self.replace_rackings_references(project, old_name, normalized)
        return RackingsRenameResult(status="renamed")

    def update_rackings_product_presets(self, project: Project, rackings: Rackings, product_presets_str: str) -> None:
        rackings.product_presets = product_presets_str
        existing_names = {product.name for product in project.product_presets}
        for char in product_presets_str:
            if char not in existing_names:
                project.add_product_preset(ProductPreset(name=char))
                existing_names.add(char)

    def update_rackings_gap_extenders(self, project: Project, rackings: Rackings, gap_extenders: list[tuple[str, int]]) -> None:
        """更新支架的 gap_extenders 列表，并同步创建 Gap Extender 预设。

        对于每个 gap extender 名称：
        - 如果不存在同名 preset，则创建 Gap_Extender 类型的 ProductPreset
        - 如果已存在同名但非 Gap_Extender 类型的 preset，替换为 Gap_Extender 类型
          （这处理了旧数据中 gap extender 与 IBEX preset 重名的情况）
        - 如果已存在同名且已是 Gap_Extender 类型，则跳过
        """
        rackings.gap_extenders = gap_extenders

        # 构建名称到 preset 的映射
        name_to_preset: dict[str, ProductPreset] = {
            product.name: product for product in project.product_presets
        }

        for name, _qty in gap_extenders:
            if not name:
                continue
            existing = name_to_preset.get(name)
            if existing is None:
                # 不存在，创建新的 Gap Extender preset
                new_preset = ProductPreset(name=name, product_type="Gap_Extender")
                project.add_product_preset(new_preset)
                name_to_preset[name] = new_preset
            elif existing.product_type != "Gap_Extender":
                # 名称已被非 Gap_Extender 类型占用（例如旧数据中与 IBEX 重名）
                # 替换为 Gap_Extender 类型的 preset
                project.product_presets.remove(existing)
                new_preset = ProductPreset(name=name, product_type="Gap_Extender")
                project.add_product_preset(new_preset)
                name_to_preset[name] = new_preset

    def cleanup_unused_rackings(self, project: Project) -> list[Rackings]:
        used_names = self.get_used_rackings_names(project)
        to_remove = [item for item in project.rackings_list if item.name not in used_names]
        for item in to_remove:
            project.rackings_list.remove(item)
        return to_remove

"""Product-related business logic."""

from __future__ import annotations

from ...models import ProductPreset, Project


class ProductPresetService:
    """Encapsulates product derivation and synchronization."""

    def get_referenced_product_preset_names(self, project: Project) -> set[str]:
        names: set[str] = set()
        for rackings in project.rackings_list:
            names.update(rackings.product_preset_names())
        return names

    def get_referenced_gap_extender_names(self, project: Project) -> set[str]:
        """获取所有被 Rackings 引用的 Gap Extender 名称"""
        names: set[str] = set()
        for rackings in project.rackings_list:
            names.update(rackings.gap_extender_names())
        return names

    def get_referenced_lynx_preset_names(self, project: Project) -> set[str]:
        """获取所有被 project/inv/node 引用的 LYNX preset 名称"""
        names: set[str] = set()
        # Project 级引用
        if project.lynx_preset is not None:
            names.add(project.lynx_preset)
        # Inv 级引用
        for inv in project.invs:
            if inv.lynx_preset is not None:
                names.add(inv.lynx_preset)
            # Node 级引用
            for node in inv.nodes:
                if node.lynx_preset is not None:
                    names.add(node.lynx_preset)
        return names

    def sync_product_presets_from_rackings(self, project: Project) -> None:
        """同步产品预设：添加缺失的 IBEX 和 Gap Extender 预设。

        - 从 Rackings 中收集所有被引用的 IBEX 和 Gap Extender 预设名称
        - 添加尚未存在的预设
        - 不再自动删除未引用的预设（改为由用户手动清理）
        """
        referenced_ibex_names = self.get_referenced_product_preset_names(project)
        referenced_gap_extender_names = self.get_referenced_gap_extender_names(project)

        existing_names = {product.name for product in project.product_presets}

        # 添加缺失的 IBEX 预设
        for name in sorted(referenced_ibex_names):
            if name not in existing_names:
                project.add_product_preset(ProductPreset(name=name))
                existing_names.add(name)

        # 添加缺失的 Gap Extender 预设
        for name in sorted(referenced_gap_extender_names):
            if name not in existing_names:
                project.add_product_preset(
                    ProductPreset(name=name, product_type="Gap_Extender")
                )
                existing_names.add(name)

    def get_display_product_presets(self, project: Project) -> list[ProductPreset]:
        """获取所有 IBEX 产品预设（包括未引用的）。"""
        self.sync_product_presets_from_rackings(project)
        return [
            product
            for product in project.product_presets
            if product.product_type in ("IBEX", "Homerun_Extender", "Placeholder")
        ]

    def get_gap_extender_presets(self, project: Project) -> list[ProductPreset]:
        """获取项目中所有的 Gap Extender preset"""
        return [pp for pp in project.product_presets if pp.product_type == "Gap_Extender"]

    def get_lynx_presets(self, project: Project) -> list[ProductPreset]:
        """获取项目中所有的 LYNX preset"""
        return [pp for pp in project.product_presets if pp.product_type == "LYNX"]

    def get_next_lynx_preset_name(self, project: Project) -> str:
        """生成下一个可用的 LYNX preset 名称（LA, LB, LC, ...）"""
        existing_names = {
            pp.name for pp in project.product_presets
            if pp.product_type == "LYNX"
        }
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            name = f"L{letter}"
            if name not in existing_names:
                return name
        # 如果 26 个字母都用完了，继续 LAA, LAB, ...
        suffix = 0
        while True:
            name = f"L{suffix:02d}"
            if name not in existing_names:
                return name
            suffix += 1

    def create_lynx_preset(self, project: Project, auto_select_name: str | None = None) -> ProductPreset:
        """创建一个新的 LYNX preset，并添加到 project 中"""
        name = auto_select_name or self.get_next_lynx_preset_name(project)
        preset = ProductPreset(name=name, product_type="LYNX")
        project.add_product_preset(preset)
        return preset

    def ensure_default_lynx_preset(self, project: Project) -> None:
        """确保项目中有默认的 LYNX preset（名称为 LA）"""
        has_lynx = any(pp.product_type == "LYNX" for pp in project.product_presets)
        if not has_lynx:
            self.create_lynx_preset(project, "LA")

    def get_all_referenced_names(self, project: Project) -> set[str]:
        """获取所有被引用的 preset 名称（IBEX + Gap Extender + LYNX）。"""
        names: set[str] = set()
        names.update(self.get_referenced_product_preset_names(project))
        names.update(self.get_referenced_gap_extender_names(project))
        names.update(self.get_referenced_lynx_preset_names(project))
        return names

    def cleanup_unreferenced_lynx_presets(self, project: Project) -> None:
        """移除没有被任何节点引用的 LYNX preset（保留以供兼容，建议使用 cleanup_unreferenced_presets）。"""
        referenced_names = self.get_referenced_lynx_preset_names(project)
        project.product_presets = [
            pp for pp in project.product_presets
            if pp.product_type != "LYNX" or pp.name in referenced_names
        ]

    def cleanup_unreferenced_presets(self, project: Project) -> list[str]:
        """移除所有未被引用的 preset（IBEX、Gap Extender 和 LYNX），返回被移除的 preset 名称列表。"""
        referenced_names = self.get_all_referenced_names(project)
        to_remove: list[str] = []
        kept: list[ProductPreset] = []
        for pp in project.product_presets:
            if pp.name in referenced_names:
                kept.append(pp)
            else:
                to_remove.append(pp.name)
        project.product_presets = kept
        return to_remove

    def count_unreferenced_presets(self, project: Project) -> int:
        """统计未被引用的 preset 数量。"""
        referenced_names = self.get_all_referenced_names(project)
        return sum(1 for pp in project.product_presets if pp.name not in referenced_names)

    def get_lynx_preset_by_name(self, project: Project, name: str) -> ProductPreset | None:
        """按名称查找 LYNX preset"""
        for pp in project.product_presets:
            if pp.name == name and pp.product_type == "LYNX":
                return pp
        return None

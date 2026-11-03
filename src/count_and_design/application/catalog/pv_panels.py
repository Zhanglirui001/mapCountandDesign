"""Named photovoltaic-panel synchronization and editing."""

from __future__ import annotations

from ...models import PVPanelInfo, Project


class PVPanelService:
    """Keep panel objects synchronized with product-preset panel brands."""

    def get_used_panel_names(self, project: Project) -> set[str]:
        """Return normalized panel names referenced by any product preset."""
        return {
            preset.pv_panel.strip()
            for preset in project.product_presets
            if preset.pv_panel.strip()
        }

    def sync_from_product_presets(self, project: Project) -> None:
        names = self.get_used_panel_names(project)
        existing = {panel.name for panel in project.pv_panels}
        missing = sorted(names - existing)
        for name in missing:
            project.pv_panels.append(PVPanelInfo(name=name))

    def ensure_exists(self, project: Project, name: str) -> PVPanelInfo | None:
        normalized = name.strip()
        if not normalized:
            return None
        panel = project.get_pv_panel(normalized)
        if panel is None:
            panel = PVPanelInfo(name=normalized)
            project.pv_panels.append(panel)
        return panel

    def rename(self, project: Project, panel: PVPanelInfo, name: str) -> tuple[bool, str]:
        normalized = name.strip()
        if not normalized:
            return False, "Panel name cannot be empty."
        duplicate = project.get_pv_panel(normalized)
        if duplicate is not None and duplicate is not panel:
            return False, f"PV panel '{normalized}' already exists."
        old_name = panel.name
        panel.name = normalized
        for preset in project.product_presets:
            if preset.pv_panel == old_name:
                preset.pv_panel = normalized
        return True, ""

    def cleanup_unused(self, project: Project) -> list[PVPanelInfo]:
        """Remove and return every panel that is not referenced by a preset."""
        used_names = self.get_used_panel_names(project)
        removed = [panel for panel in project.pv_panels if panel.name not in used_names]
        project.pv_panels = [panel for panel in project.pv_panels if panel.name in used_names]
        return removed

    def count_unused(self, project: Project) -> int:
        used_names = self.get_used_panel_names(project)
        return sum(panel.name not in used_names for panel in project.pv_panels)

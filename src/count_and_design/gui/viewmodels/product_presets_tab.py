"""ViewModel for the ProductPresets tab."""

from __future__ import annotations

import re
from dataclasses import replace

from PySide6.QtCore import QObject, QTimer, Signal

from ...application.catalog import ProductPresetService, PVPanelService
from ...application.ordering import (
    move_selected_items,
    optional_natural_text_key,
    sort_selected_slots,
)
from ...application.version_manager import versionable
from ...i18n import tr
from ...models import IBEXBranchCoordinate, ProductPreset, Project
from ...models.product_preset import product_type_for_branch_count
from ...models.connector import CONNECTOR_MANUFACTURER_MAP
from ...models.fuse import FUSE_MANUFACTURER_MAP
from ...models.length import Length, LengthError
from ...models.wire import AL_STRANDS_DEFAULTS, AL_STRANDS_DEFAULT, REVERSE_WIRE_SIZE_MAP
from .states import ListItemState, ProductPresetDetailState, ProductPresetsTabState
from .property_clipboard import PropertyClipboard


def _ordering_metadata(kind: str):
    def build(self, *args, **kwargs):
        selected = self._selected_presets()
        return {
            "kind": kind,
            "scope": "product_presets",
            "field": args[0] if kind == "sort" and args else kwargs.get("field", ""),
            "descending": bool(args[1] if kind == "sort" and len(args) > 1 else kwargs.get("descending", False)),
            "subjects": [item.name for item in selected],
        }
    return build

class ProductPresetsTabViewModel(QObject):
    """Provides state and commands for the ProductPresets tab."""

    state_changed = Signal(object)
    error_occurred = Signal(str, str)  # (title, message)

    def __init__(
        self,
        product_preset_service: ProductPresetService | None = None,
        pv_panel_service: PVPanelService | None = None,
    ) -> None:
        super().__init__()
        self._product_preset_service = product_preset_service or ProductPresetService()
        self._pv_panel_service = pv_panel_service or PVPanelService()
        self._project: Project | None = None
        self._selected_item_ids: list[str] = []
        self._selection_initialized = False
        self._item_lookup: dict[str, ProductPreset] = {}
        self._state = ProductPresetsTabState()
        self._property_clipboard = PropertyClipboard()

    @property
    def state(self) -> ProductPresetsTabState:
        return self._state

    def set_project(self, project: Project) -> None:
        self._project = project
        self._selection_initialized = False
        self._pv_panel_service.sync_from_product_presets(project)
        self.refresh()

    def _defer_refresh(self) -> None:
        """延迟刷新以避免在焦点切换过程中销毁 widget。

        当 editingFinished 信号在焦点离开时触发，若立即调用 refresh() 会
        销毁并重建编辑器区域的所有 widget，导致目标 widget 来不及接收焦点。
        """
        QTimer.singleShot(0, self.refresh)

    def refresh(self) -> None:
        if self._project is None:
            self._state = ProductPresetsTabState()
            self.state_changed.emit(self._state)
            return
        # Ensure referenced presets exist, then preserve the project's real
        # order instead of regrouping entries by product type.
        self._product_preset_service.sync_product_presets_from_rackings(self._project)
        supported_types = {
            "IBEX", "Homerun_Extender", "Placeholder", "LYNX", "Gap_Extender"
        }
        all_presets = [
            preset
            for preset in self._project.product_presets
            if preset.product_type in supported_types
        ]

        # 确定哪些 preset 被引用
        referenced_names = self._product_preset_service.get_all_referenced_names(self._project)
        
        self._item_lookup = {}
        items = []
        for product in all_presets:
            item_id = self._key_for(product)
            self._item_lookup[item_id] = product
            muted = product.name not in referenced_names
            remarks = product.remarks.strip()
            label = f"{product.name} ({remarks})" if remarks else product.name
            items.append(ListItemState(
                item_id=item_id,
                label=f"{label}{tr('product_presets_tab.unused_suffix', ' (unused)')}" if muted else label,
                muted=muted,
            ))
        selected_ids = set(self._selected_item_ids)
        self._selected_item_ids = [
            item.item_id for item in items if item.item_id in selected_ids
        ]
        if not self._selected_item_ids and items and not self._selection_initialized:
            self._selected_item_ids = [items[0].item_id]
        self._selection_initialized = True
        selected_item_id = (
            self._selected_item_ids[0] if len(self._selected_item_ids) == 1 else None
        )
        selected = self._item_lookup.get(selected_item_id or "")
        detail_title = tr("product_presets_tab.detail_title_with_name", "Product Preset {name}").format(name=selected.name) if selected is not None else tr("product_presets_tab.detail_title", "Product Preset Properties")
        detail = self._build_detail(selected)
        detail = replace(
            detail,
            pv_panel_options=tuple(panel.name for panel in self._project.pv_panels),
        )
        self._state = ProductPresetsTabState(
            items=tuple(items),
            selected_item_id=selected_item_id,
            selected_item_ids=tuple(self._selected_item_ids),
            detail_title=detail_title,
            detail=detail,
        )
        self.state_changed.emit(self._state)

    def select_product(self, item_id: str | None) -> None:
        self.select_items((item_id,) if item_id is not None else ())

    def select_items(self, item_ids: tuple[str, ...] | list[str]) -> None:
        self._selection_initialized = True
        requested = set(item_ids)
        self._selected_item_ids = [
            item_id for item_id in self._item_lookup if item_id in requested
        ]
        self.refresh()

    @versionable(
        "version.reorder_product_presets",
        metadata_factory=_ordering_metadata("reorder"),
    )
    def move_selected(self, target_item_id: str | None, after: bool = False) -> None:
        if self._project is None:
            return
        if target_item_id is not None and target_item_id not in self._item_lookup:
            return
        target = self._item_lookup.get(target_item_id or "")
        if move_selected_items(
            self._project.product_presets,
            self._selected_presets(),
            target,
            after=after,
        ):
            self.refresh()

    @versionable(
        "version.sort_product_presets",
        metadata_factory=_ordering_metadata("sort"),
    )
    def sort_selected(self, field: str, descending: bool = False) -> None:
        if self._project is None or len(self._selected_item_ids) < 2:
            return
        if field == "name":
            key = lambda item: optional_natural_text_key(item.name)
        elif field == "remarks":
            key = lambda item: optional_natural_text_key(item.remarks)
        elif field == "pv_panel":
            key = lambda item: optional_natural_text_key(item.pv_panel)
        elif field == "branch_count":
            key = lambda item: item.branch_count
        else:
            return
        if sort_selected_slots(
            self._project.product_presets,
            self._selected_presets(),
            key,
            descending=descending,
        ):
            self.refresh()

    def select_object(self, product: ProductPreset) -> bool:
        item_id = self._key_for(product)
        self.refresh()
        if item_id not in self._item_lookup:
            return False
        self.select_product(item_id)
        return True

    def copy_selected(self) -> bool:
        """Copy every property except the selected preset's name."""
        preset = self._current_preset()
        return preset is not None and self._property_clipboard.copy_from(preset)

    def paste_to_selected(self) -> bool:
        """Paste copied properties while preserving the target preset's name."""
        preset = self._current_preset()
        if self._project is None or preset is None:
            return False
        pre = self._project.to_dict()
        if not self._property_clipboard.paste_into(preset):
            return False
        self._project.bind_product_preset(preset)
        self._pv_panel_service.ensure_exists(self._project, preset.pv_panel)
        manager = getattr(self, "_version_manager", None)
        if manager is not None:
            manager.commit(pre, self._project, "version.paste_product_preset_properties")
        self.refresh()
        return True

    # -- Property update handlers --

    @versionable("version.edit_product_type")
    def update_product_type(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if value in {"IBEX", "Homerun_Extender", "Placeholder"}:
            preset.product_type = product_type_for_branch_count(preset.branch_count)
        else:
            preset.product_type = value
        self._defer_refresh()

    @versionable("version.edit_remarks")
    def update_remarks(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.remarks = value
        self._defer_refresh()

    @versionable("version.edit_pv_panel")
    def update_pv_panel(self, value: str) -> str:
        preset = self._current_preset()
        if preset is None:
            return ""
        normalized = value.strip()
        preset.pv_panel = normalized
        if self._project is not None:
            self._pv_panel_service.ensure_exists(self._project, normalized)
        options = list(self._state.detail.pv_panel_options)
        if normalized and normalized not in options:
            options.append(normalized)
        self._state = replace(
            self._state,
            detail=replace(
                self._state.detail,
                pv_panel=normalized,
                pv_panel_options=tuple(options),
            ),
        )
        return normalized

    @versionable("version.edit_branch_count")
    def update_branch_count(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        try:
            new_count = int(value)
        except ValueError:
            return
        if new_count < 0:
            return
        old_count = preset.branch_count
        preset.branch_count = new_count
        if preset.product_type in {"IBEX", "Homerun_Extender", "Placeholder"}:
            preset.product_type = product_type_for_branch_count(new_count)
        # Sync branch_lines list to match branch_count
        if new_count > old_count:
            for i in range(old_count + 1, new_count + 1):
                branch = (
                    IBEXBranchCoordinate(index=i, coordinate_positive=Length("0mm"), coordinate_negative=Length("0mm"))
                )
                if self._project is not None:
                    self._project.add_branch_coordinate(preset, branch)
                else:
                    preset.branch_lines.append(branch)
        elif new_count < old_count:
            del preset.branch_lines[new_count:]
        # Re-index existing entries
        for idx, bl in enumerate(preset.branch_lines, start=1):
            bl.index = idx
        self._defer_refresh()

    # -- Connector update handlers --

    @versionable("version.edit_branch_connector_type")
    def update_branch_connector_type(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.branch_connector.type = value
        preset.branch_connector.manufacturer = CONNECTOR_MANUFACTURER_MAP.get(value, "none")
        self._defer_refresh()

    @versionable("version.edit_branch_connector_manufacturer")
    def update_branch_connector_manufacturer(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.branch_connector.manufacturer = value
        self._defer_refresh()

    @versionable("version.edit_branch_connector_size")
    def update_branch_connector_size(self, value: str) -> None:
        """Update connector wire size and auto-sync to branch_cu_wire."""
        preset = self._current_preset()
        if preset is None:
            return
        preset.branch_connector.size.wiresize = value
        preset.branch_connector.size.material = "Cu"
        if value != "none":
            preset.branch_cu_wire.wiresize = value
        self._defer_refresh()

    def update_branch_connector_material(self, value: str) -> None:
        """Keep for backward compatibility - no longer connected from UI."""
        pass

    def update_branch_connector_strands(self, value: str) -> None:
        """Keep for backward compatibility - no longer connected from UI."""
        pass

    @versionable("version.edit_external_connector_type")
    def update_external_connector_type(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.external_connector.type = value
        preset.external_connector.manufacturer = CONNECTOR_MANUFACTURER_MAP.get(value, "none")
        self._defer_refresh()

    @versionable("version.edit_external_connector_manufacturer")
    def update_external_connector_manufacturer(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.external_connector.manufacturer = value
        self._defer_refresh()

    @versionable("version.edit_external_connector_size")
    def update_external_connector_size(self, value: str) -> None:
        """Update connector wire size and auto-sync to external_cu_wire."""
        preset = self._current_preset()
        if preset is None:
            return
        preset.external_connector.size.wiresize = value
        preset.external_connector.size.material = "Cu"
        if value != "none":
            preset.external_cu_wire.wiresize = value
        self._defer_refresh()

    def update_external_connector_material(self, value: str) -> None:
        """Keep for backward compatibility - no longer connected from UI."""
        pass

    def update_external_connector_strands(self, value: str) -> None:
        """Keep for backward compatibility - no longer connected from UI."""
        pass

    # -- Fuse update handlers --

    @versionable("version.edit_branch_fuse_current")
    def update_branch_fuse_current(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.branch_fuse.current = value
        preset.branch_fuse.manufacturer = FUSE_MANUFACTURER_MAP.get(value, "none")
        self._defer_refresh()

    @versionable("version.edit_branch_fuse_manufacturer")
    def update_branch_fuse_manufacturer(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.branch_fuse.manufacturer = value
        self._defer_refresh()

    @versionable("version.edit_last_branch_fuse_current")
    def update_last_branch_fuse_current(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.last_branch_fuse.current = value
        preset.last_branch_fuse.manufacturer = FUSE_MANUFACTURER_MAP.get(value, "none")
        self._defer_refresh()

    @versionable("version.edit_last_branch_fuse_manufacturer")
    def update_last_branch_fuse_manufacturer(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.last_branch_fuse.manufacturer = value
        self._defer_refresh()

    @versionable("version.edit_external_fuse_current")
    def update_external_fuse_current(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.external_fuse.current = value
        preset.external_fuse.manufacturer = FUSE_MANUFACTURER_MAP.get(value, "none")
        self._defer_refresh()

    @versionable("version.edit_external_fuse_manufacturer")
    def update_external_fuse_manufacturer(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.external_fuse.manufacturer = value
        self._defer_refresh()

    # -- Wire update handlers (material set directly) --

    @versionable("version.edit_branch_cu_wire")
    def update_branch_cu_wire(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.branch_cu_wire.wiresize = value
        preset.branch_cu_wire.material = "Cu"
        preset._sync_connector_sizes()
        self._defer_refresh()

    def update_branch_cu_material(self, value: str) -> None:
        """Keep for backward compatibility."""
        pass

    @versionable("version.edit_branch_al_wire")
    def update_branch_al_wire(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.branch_al_wire.wiresize = value
        preset.branch_al_wire.material = "Al"
        self._defer_refresh()

    def update_branch_al_material(self, value: str) -> None:
        """Keep for backward compatibility."""
        pass

    @versionable("version.edit_last_branch_cu_wire")
    def update_last_branch_cu_wire(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.last_branch_cu_wire.wiresize = value
        preset.last_branch_cu_wire.material = "Cu"
        self._defer_refresh()

    @versionable("version.edit_last_branch_cu_strands")
    def update_last_branch_cu_strands(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.last_branch_cu_wire.strands = value
        self._defer_refresh()

    @versionable("version.edit_last_branch_al_wire")
    def update_last_branch_al_wire(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.last_branch_al_wire.wiresize = value
        preset.last_branch_al_wire.material = "Al"
        self._defer_refresh()

    @versionable("version.edit_last_branch_al_strands")
    def update_last_branch_al_strands(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.last_branch_al_wire.strands = value
        self._defer_refresh()

    @versionable("version.edit_external_cu_wire")
    def update_external_cu_wire(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.external_cu_wire.wiresize = value
        preset.external_cu_wire.material = "Cu"
        preset._sync_connector_sizes()
        self._defer_refresh()

    def update_external_cu_material(self, value: str) -> None:
        """Keep for backward compatibility."""
        pass

    @versionable("version.edit_external_al_wire")
    def update_external_al_wire(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.external_al_wire.wiresize = value
        preset.external_al_wire.material = "Al"
        self._defer_refresh()

    def update_external_al_material(self, value: str) -> None:
        """Keep for backward compatibility."""
        pass

    @versionable("version.edit_internal_cu_wire")
    def update_internal_cu_wire(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.internal_cu_wire.wiresize = value
        preset.internal_cu_wire.material = "Cu"
        self._defer_refresh()

    def update_internal_cu_material(self, value: str) -> None:
        """Keep for backward compatibility."""
        pass

    @versionable("version.edit_internal_al_wire")
    def update_internal_al_wire(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.internal_al_wire.wiresize = value
        preset.internal_al_wire.material = "Al"
        self._defer_refresh()

    def update_internal_al_material(self, value: str) -> None:
        """Keep for backward compatibility."""
        pass

    # -- Strands update handlers (keep for backward compatibility) --

    @versionable("version.edit_branch_cu_strands")
    def update_branch_cu_strands(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.branch_cu_wire.strands = value
        self._defer_refresh()

    @versionable("version.edit_branch_al_strands")
    def update_branch_al_strands(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.branch_al_wire.strands = value
        self._defer_refresh()

    @versionable("version.edit_external_cu_strands")
    def update_external_cu_strands(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.external_cu_wire.strands = value
        self._defer_refresh()

    @versionable("version.edit_external_al_strands")
    def update_external_al_strands(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.external_al_wire.strands = value
        self._defer_refresh()

    @versionable("version.edit_internal_cu_strands")
    def update_internal_cu_strands(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.internal_cu_wire.strands = value
        self._defer_refresh()

    @versionable("version.edit_internal_al_strands")
    def update_internal_al_strands(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.internal_al_wire.strands = value
        self._defer_refresh()

    def update_branch_connector_strands(self, value: str) -> None:
        """Keep for backward compatibility."""
        pass

    def update_external_connector_strands(self, value: str) -> None:
        """Keep for backward compatibility."""
        pass

    @versionable("version.edit_cluster_wire_strands")
    def update_cluster_wire_strands(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.cluster_wire.strands = value
        self._defer_refresh()

    # -- Length update handlers --

    @versionable("version.edit_product_total_length")
    def update_product_total_length(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.product_total_length, value):
            return
        self._defer_refresh()

    # -- Branch coordinate update handlers --

    @versionable("version.edit_branch_coordinate_positive")
    def update_branch_coordinate_positive(self, index: int, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        for bl in preset.branch_lines:
            if bl.index == index:
                if not self._safe_set_length(bl.coordinate_positive, value):
                    return
                break
        self._check_branch_distance_linkage(index, value, preset)
        self._defer_refresh()

    @versionable("version.edit_branch_coordinate_negative")
    def update_branch_coordinate_negative(self, index: int, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        for bl in preset.branch_lines:
            if bl.index == index:
                if not self._safe_set_length(bl.coordinate_negative, value):
                    return
                break
        self._check_branch_distance_linkage(index, value, preset)
        self._defer_refresh()

    # -- Branch distance linkage helpers --

    @staticmethod
    def _is_length_identifier_format(value: str) -> bool:
        """检查是否为长度标识符格式（如"1S", "1S2R", "1S2M3R"等）。

        与旧项目（Typical_calculator）中的定义保持一致：
        数字+字母，可重复多次。
        """
        if not value or not str(value).strip():
            return False
        pattern = r'^\d+[A-Za-z]+(\d+[A-Za-z]+)*$'
        return bool(re.match(pattern, str(value).strip()))

    def _check_branch_distance_linkage(
        self, branch_index: int, value: str, preset: ProductPreset
    ) -> None:
        """检查并处理支线坐标联动逻辑。

        当输入值符合长度标识符格式时，比较当前支线的正负坐标中
        第一个数字的大小：
        - 正 > 负：从下一支线开始，负坐标 = 上一正坐标，正坐标 = 负坐标首个数字+1
        - 负 > 正：从下一支线开始，正坐标 = 上一负坐标，负坐标 = 正坐标首个数字+1
        """
        if not self._is_length_identifier_format(value):
            return

        # 找到当前支线
        current_bl = None
        for bl in preset.branch_lines:
            if bl.index == branch_index:
                current_bl = bl
                break
        if current_bl is None:
            return

        pos_str = current_bl.coordinate_positive.length_str
        neg_str = current_bl.coordinate_negative.length_str

        # 提取第一个数字进行比较
        try:
            pos_match = re.search(r'\d+', str(pos_str))
            neg_match = re.search(r'\d+', str(neg_str))
            if not pos_match or not neg_match:
                return
            pos_num = int(pos_match.group())
            neg_num = int(neg_match.group())
        except (ValueError, AttributeError):
            return

        if pos_num > neg_num:
            # 正大于负：从下一个支线开始，负坐标 = 当前正坐标，正坐标 = 负坐标首个数字+1
            next_index = branch_index + 1
            if self._get_branch_by_index(preset, next_index) is not None:
                self._recursive_update_branch_coordinate(
                    preset, next_index, "negative", pos_str, True
                )
        elif neg_num > pos_num:
            # 负大于正：从下一个支线开始，正坐标 = 当前负坐标，负坐标 = 正坐标首个数字+1
            next_index = branch_index + 1
            if self._get_branch_by_index(preset, next_index) is not None:
                self._recursive_update_branch_coordinate(
                    preset, next_index, "positive", neg_str, False
                )

    @staticmethod
    def _get_branch_by_index(preset: ProductPreset, index: int) -> IBEXBranchCoordinate | None:
        """根据索引获取支线坐标对象。"""
        for bl in preset.branch_lines:
            if bl.index == index:
                return bl
        return None

    def _recursive_update_branch_coordinate(
        self,
        preset: ProductPreset,
        branch_index: int,
        coordinate_type: str,
        value: str,
        is_positive_greater: bool,
    ) -> None:
        """递归更新支线坐标。

        Args:
            preset: 产品预设
            branch_index: 当前支线索引
            coordinate_type: 要更新的坐标类型 ("positive" 或 "negative")
            value: 要设置的值
            is_positive_greater: 是否是正大于负的情况
        """
        bl = self._get_branch_by_index(preset, branch_index)
        if bl is None:
            return

        if coordinate_type == "negative":
            # 更新当前支线的负坐标 = 上一支线的正坐标
            if not self._safe_set_length(bl.coordinate_negative, value):
                return
            # 构建新的正坐标 = 负坐标首个数字 + 1
            match = re.search(r'(\d+)', str(value))
            if match:
                first_number = int(match.group(1))
                new_number = first_number + 1
                new_positive_value = re.sub(
                    r'\d+', str(new_number), str(value), count=1
                )
                if not self._safe_set_length(bl.coordinate_positive, new_positive_value):
                    return
                # 递归处理下一个支线
                next_index = branch_index + 1
                if self._get_branch_by_index(preset, next_index) is not None:
                    self._recursive_update_branch_coordinate(
                        preset, next_index, "negative",
                        new_positive_value, is_positive_greater
                    )
        elif coordinate_type == "positive":
            # 更新当前支线的正坐标 = 上一支线的负坐标
            if not self._safe_set_length(bl.coordinate_positive, value):
                return
            # 构建新的负坐标 = 正坐标首个数字 + 1
            match = re.search(r'(\d+)', str(value))
            if match:
                first_number = int(match.group(1))
                new_number = first_number + 1
                new_negative_value = re.sub(
                    r'\d+', str(new_number), str(value), count=1
                )
                if not self._safe_set_length(bl.coordinate_negative, new_negative_value):
                    return
                # 递归处理下一个支线
                next_index = branch_index + 1
                if self._get_branch_by_index(preset, next_index) is not None:
                    self._recursive_update_branch_coordinate(
                        preset, next_index, "positive",
                        new_negative_value, is_positive_greater
                    )

    @versionable("version.edit_branch_length_positive")
    def update_branch_length_positive(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.branch_length_positive, value):
            return
        self._defer_refresh()

    @versionable("version.edit_branch_length_negative")
    def update_branch_length_negative(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.branch_length_negative, value):
            return
        self._defer_refresh()

    @versionable("version.edit_last_branch_with_extra_length")
    def update_last_branch_with_extra_length(self, value: bool) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.last_branch_with_extra_length = value
        self._defer_refresh()

    @versionable("version.edit_branch_fuse_prefix")
    def update_branch_fuse_prefix(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.branch_fuse_prefix, value):
            return
        self._defer_refresh()

    @versionable("version.edit_branch_first_cual_prefix")
    def update_branch_first_cual_prefix(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.branch_first_cual_prefix, value):
            return
        self._defer_refresh()

    @versionable("version.edit_branch_second_cual_suffix")
    def update_branch_second_cual_suffix(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.branch_second_cual_suffix, value):
            return
        self._defer_refresh()

    @versionable("version.edit_external_fuse_suffix")
    def update_external_fuse_suffix(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.external_fuse_suffix, value):
            return
        self._defer_refresh()

    @versionable("version.edit_external_cu_length_to_row_head")
    def update_external_cu_length_to_row_head(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.external_cu_length_to_row_head, value):
            return
        self._defer_refresh()

    @versionable("version.edit_external_second_cual_suffix")
    def update_external_second_cual_suffix(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.external_second_cual_suffix, value):
            return
        self._defer_refresh()

    @versionable("version.edit_external_extension_trench_length")
    def update_external_extension_trench_length(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.external_extension_trench_length, value):
            return
        self._defer_refresh()

    @versionable("version.edit_external_al_threshold")
    def update_external_al_threshold(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.external_al_threshold, value):
            return
        self._defer_refresh()

    @versionable("version.edit_last_branch_al_threshold")
    def update_last_branch_al_threshold(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.last_branch_al_threshold, value):
            return
        self._defer_refresh()

    @versionable("version.edit_internal_al_threshold")
    def update_internal_al_threshold(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.internal_al_threshold, value):
            return
        self._defer_refresh()

    @versionable("version.edit_internal_first_cual_prefix")
    def update_internal_first_cual_prefix(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.internal_first_cual_prefix, value):
            return
        self._defer_refresh()

    @versionable("version.edit_internal_second_cual_suffix")
    def update_internal_second_cual_suffix(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.internal_second_cual_suffix, value):
            return
        self._defer_refresh()

    # -- LYNX update handlers --

    @versionable("version.edit_cluster_wire")
    def update_cluster_wire(self, value: str) -> None:
        """Update cluster wire size, auto-set material to Al and infer strands."""
        preset = self._current_preset()
        if preset is None:
            return
        preset.cluster_wire.wiresize = value
        preset.cluster_wire.material = "Al"
        preset.cluster_wire.strands = self._infer_al_strands(value)
        self._defer_refresh()

    def update_cluster_wire_material(self, value: str) -> None:
        """Keep for backward compatibility."""
        pass

    @versionable("version.edit_branch_length_cu")
    def update_branch_length_cu(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.branch_length_cu, value):
            return
        self._defer_refresh()

    @versionable("version.edit_branch_length_al")
    def update_branch_length_al(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.branch_length_al, value):
            return
        self._defer_refresh()

    @versionable("version.edit_row_per_overmold")
    def update_row_per_overmold(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        try:
            new_val = int(value)
        except ValueError:
            return
        if new_val < 1:
            return
        preset.row_per_overmold = new_val
        self._defer_refresh()

    @versionable("version.edit_tail_shortened")
    def update_tail_shortened(self, value: bool) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.tail_shortened = value
        self._defer_refresh()

    @versionable("version.edit_nearby_overmold_gap")
    def update_nearby_overmold_gap(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.nearby_overmold_gap, value):
            return
        self._defer_refresh()

    @versionable("version.edit_buffer_length")
    def update_buffer_length(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.buffer_length, value):
            return
        self._defer_refresh()

    # -- Gap Extender update handlers --

    @versionable("version.edit_gap_extender_connector_male_type")
    def update_gap_extender_connector_male_type(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.connector_male.type = value
        preset.connector_male.manufacturer = CONNECTOR_MANUFACTURER_MAP.get(value, "none")
        self._defer_refresh()

    @versionable("version.edit_gap_extender_connector_male_manufacturer")
    def update_gap_extender_connector_male_manufacturer(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.connector_male.manufacturer = value
        self._defer_refresh()

    @versionable("version.edit_gap_extender_connector_male_size")
    def update_gap_extender_connector_male_size(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.connector_male.size.wiresize = value
        preset.connector_male.size.material = "Cu"
        if value != "none":
            preset.cu_wire.wiresize = value
        self._defer_refresh()

    @versionable("version.edit_gap_extender_connector_female_type")
    def update_gap_extender_connector_female_type(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.connector_female.type = value
        preset.connector_female.manufacturer = CONNECTOR_MANUFACTURER_MAP.get(value, "none")
        self._defer_refresh()

    @versionable("version.edit_gap_extender_connector_female_manufacturer")
    def update_gap_extender_connector_female_manufacturer(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.connector_female.manufacturer = value
        self._defer_refresh()

    @versionable("version.edit_gap_extender_connector_female_size")
    def update_gap_extender_connector_female_size(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.connector_female.size.wiresize = value
        preset.connector_female.size.material = "Cu"
        if value != "none":
            preset.cu_wire.wiresize = value
        self._defer_refresh()

    @versionable("version.edit_gap_extender_cu_wire")
    def update_gap_extender_cu_wire(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.cu_wire.wiresize = value
        preset.cu_wire.material = "Cu"
        preset._sync_connector_sizes()
        self._defer_refresh()

    @versionable("version.edit_gap_extender_cu_strands")
    def update_gap_extender_cu_strands(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.cu_wire.strands = value
        self._defer_refresh()

    @versionable("version.edit_gap_extender_length")
    def update_gap_extender_length(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        if not self._safe_set_length(preset.length, value):
            return
        self._defer_refresh()

    @versionable("version.edit_gap_extender_label")
    def update_gap_extender_label(self, value: str) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        preset.label = value
        self._defer_refresh()

    # -- Cleanup --

    @versionable("version.clean_up_unused_presets")
    def cleanup_unused(self) -> tuple[int, str]:
        if self._project is None:
            return 0, tr("product_presets_tab.no_project_loaded", "No project loaded")
        removed = self._product_preset_service.cleanup_unreferenced_presets(self._project)
        self.refresh()
        return len(removed), tr("product_presets_tab.no_unused", "No unused presets to remove") if not removed else ""

    def count_unused(self) -> int:
        if self._project is None:
            return 0
        return self._product_preset_service.count_unreferenced_presets(self._project)

    # -- Helpers --

    def _safe_set_length(self, length: Length, value: str) -> bool:
        """Safely set a length value, emitting error_occurred on failure.

        Returns True if the value was set successfully, False otherwise.
        On failure the input is rejected: the model keeps its previous
        value, and a deferred refresh reverts the editor fields so the
        rejected text does not linger in the input box.
        """
        try:
            length.set_length_str(value)
            return True
        except LengthError as exc:
            self.error_occurred.emit(
                tr(f"length.error.{exc.code}_title",
                   tr("length_identifiers_tab.dimension_error_title", "Error")),
                tr(f"length.error.{exc.code}", str(exc)).format(**exc.params),
            )
            # 拒绝非法输入后回退 UI 到当前有效值（与长度标识符页一致）。
            self._defer_refresh()
            return False

    def _current_preset(self) -> ProductPreset | None:
        if len(self._selected_item_ids) != 1:
            return None
        return self._item_lookup.get(self._selected_item_ids[0])

    def _selected_presets(self) -> list[ProductPreset]:
        selected = set(self._selected_item_ids)
        return [
            item
            for item in (self._project.product_presets if self._project else [])
            if self._key_for(item) in selected
        ]

    @staticmethod
    def _infer_al_strands(size: str) -> str:
        """Infer default strands for an aluminum wire of the given size."""
        if size == "none":
            return "none"
        if size in AL_STRANDS_DEFAULTS:
            return AL_STRANDS_DEFAULTS[size]
        awg = REVERSE_WIRE_SIZE_MAP.get(size, size)
        return AL_STRANDS_DEFAULTS.get(awg, AL_STRANDS_DEFAULT)

    @staticmethod
    def _build_detail(preset: ProductPreset | None) -> ProductPresetDetailState:
        if preset is None:
            return ProductPresetDetailState()

        def _fmt_len(length) -> str:
            return getattr(length, "length_str", "")

        def _fmt_len_display(length) -> str:
            """Format a Length's converted value for display label."""
            if length is None or not isinstance(length, Length):
                return ""
            try:
                mm = length.to_mm()
                ft = length.to_ft()
                return f"= {ft:.2f} ft ({mm:.1f} mm)"
            except ValueError:
                return "(needs identifier)"

        def _fmt_wire(wire) -> tuple[str, str, str]:
            if wire is None:
                return ("", "Cu", "none")
            return (wire.wiresize, wire.material, wire.strands)

        def _fmt_connector(conn) -> tuple[str, str, str]:
            """Format connector: returns (type, manufacturer, wire_size)."""
            if conn is None:
                return ("", "", "")
            size = conn.size
            if size is None:
                return (conn.type, conn.manufacturer, "")
            return (conn.type, conn.manufacturer, size.wiresize)

        def _fmt_fuse(f) -> tuple[str, str]:
            if f is None:
                return ("", "")
            return (getattr(f, "current", ""), getattr(f, "manufacturer", ""))

        bc_type, bc_manu, bc_size = _fmt_connector(preset.branch_connector)
        ec_type, ec_manu, ec_size = _fmt_connector(preset.external_connector)
        bf_cur, bf_manu = _fmt_fuse(preset.branch_fuse)
        ef_cur, ef_manu = _fmt_fuse(preset.external_fuse)
        lbf_cur, lbf_manu = _fmt_fuse(preset.last_branch_fuse)
        bcu_size, bcu_mat, bcu_strands = _fmt_wire(preset.branch_cu_wire)
        balu_size, balu_mat, balu_strands = _fmt_wire(preset.branch_al_wire)
        lbcu_size, lbcu_mat, lbcu_strands = _fmt_wire(preset.last_branch_cu_wire)
        lbal_size, lbal_mat, lbal_strands = _fmt_wire(preset.last_branch_al_wire)
        ecu_size, ecu_mat, ecu_strands = _fmt_wire(preset.external_cu_wire)
        ealu_size, ealu_mat, ealu_strands = _fmt_wire(preset.external_al_wire)
        icu_size, icu_mat, icu_strands = _fmt_wire(preset.internal_cu_wire)
        ialu_size, ialu_mat, ialu_strands = _fmt_wire(preset.internal_al_wire)
        cd_size, cd_mat, cd_strands = _fmt_wire(preset.cluster_wire)

        # Gap Extender fields
        cm_type, cm_manu, cm_size = _fmt_connector(preset.connector_male)
        cf_type, cf_manu, cf_size = _fmt_connector(preset.connector_female)
        ewire_size, ewire_mat, ewire_strands = _fmt_wire(preset.cu_wire)

        branch_lines_entries: list[tuple[str, str, str]] = []
        for bl in preset.branch_lines:
            branch_lines_entries.append((
                str(bl.index),
                getattr(bl.coordinate_positive, "length_str", ""),
                getattr(bl.coordinate_negative, "length_str", ""),
            ))

        # Build length conversion display info
        length_displays: dict[str, str] = {}
        _len_field_names = [
            "product_total_length",
            "branch_length_positive",
            "branch_length_negative",
            "branch_fuse_prefix",
            "branch_first_cual_prefix",
            "branch_second_cual_suffix",
            "external_fuse_suffix",
            "external_cu_length_to_row_head",
            "external_second_cual_suffix",
            "external_al_threshold",
            "last_branch_al_threshold",
            "internal_al_threshold",
            "external_extension_trench_length",
            "internal_first_cual_prefix",
            "internal_second_cual_suffix",
            "branch_length_cu",
            "branch_length_al",
            "nearby_overmold_gap",
            "buffer_length",
            "length",  # Gap Extender
        ]
        for field_name in _len_field_names:
            length_val = getattr(preset, field_name, None)
            length_displays[field_name] = _fmt_len_display(length_val)

        # Add branch line coordinate displays
        for bl in preset.branch_lines:
            pos_key = f"branch_coord_{bl.index}_pos"
            neg_key = f"branch_coord_{bl.index}_neg"
            length_displays[pos_key] = _fmt_len_display(bl.coordinate_positive)
            length_displays[neg_key] = _fmt_len_display(bl.coordinate_negative)

        return ProductPresetDetailState(
            name=preset.name,
            product_type=preset.product_type,
            remarks=preset.remarks,
            pv_panel=preset.pv_panel,
            branch_count=str(preset.branch_count),
            # Connectors (material/strands kept in model but no longer exposed in UI)
            branch_connector_type=bc_type,
            branch_connector_manufacturer=bc_manu,
            branch_connector_size=bc_size,
            branch_connector_material="Cu",
            branch_connector_strands="none",
            external_connector_type=ec_type,
            external_connector_manufacturer=ec_manu,
            external_connector_size=ec_size,
            external_connector_material="Cu",
            external_connector_strands="none",
            # Fuses
            branch_fuse_current=bf_cur,
            branch_fuse_manufacturer=bf_manu,
            external_fuse_current=ef_cur,
            external_fuse_manufacturer=ef_manu,
            # Last branch (末端支线)
            last_branch_fuse_current=lbf_cur,
            last_branch_fuse_manufacturer=lbf_manu,
            last_branch_cu_wire=lbcu_size,
            last_branch_cu_material=lbcu_mat,
            last_branch_cu_strands=lbcu_strands,
            last_branch_al_wire=lbal_size,
            last_branch_al_material=lbal_mat,
            last_branch_al_strands=lbal_strands,
            # Wires
            branch_cu_wire=bcu_size,
            branch_cu_material=bcu_mat,
            branch_cu_strands=bcu_strands,
            branch_al_wire=balu_size,
            branch_al_material=balu_mat,
            branch_al_strands=balu_strands,
            external_cu_wire=ecu_size,
            external_cu_material=ecu_mat,
            external_cu_strands=ecu_strands,
            external_al_wire=ealu_size,
            external_al_material=ealu_mat,
            external_al_strands=ealu_strands,
            internal_cu_wire=icu_size,
            internal_cu_material=icu_mat,
            internal_cu_strands=icu_strands,
            internal_al_wire=ialu_size,
            internal_al_material=ialu_mat,
            internal_al_strands=ialu_strands,
            # Lengths
            product_total_length=_fmt_len(preset.product_total_length),
            branch_length_positive=_fmt_len(preset.branch_length_positive),
            branch_length_negative=_fmt_len(preset.branch_length_negative),
            last_branch_with_extra_length=preset.last_branch_with_extra_length,
            branch_fuse_prefix=_fmt_len(preset.branch_fuse_prefix),
            branch_first_cual_prefix=_fmt_len(preset.branch_first_cual_prefix),
            branch_second_cual_suffix=_fmt_len(preset.branch_second_cual_suffix),
            external_fuse_suffix=_fmt_len(preset.external_fuse_suffix),
            external_cu_length_to_row_head=_fmt_len(preset.external_cu_length_to_row_head),
            external_second_cual_suffix=_fmt_len(preset.external_second_cual_suffix),
            external_al_threshold=_fmt_len(preset.external_al_threshold),
            last_branch_al_threshold=_fmt_len(preset.last_branch_al_threshold),
            internal_al_threshold=_fmt_len(preset.internal_al_threshold),
            external_extension_trench_length=_fmt_len(preset.external_extension_trench_length),
            internal_first_cual_prefix=_fmt_len(preset.internal_first_cual_prefix),
            internal_second_cual_suffix=_fmt_len(preset.internal_second_cual_suffix),
            # Branch Coordination
            branch_lines=tuple(branch_lines_entries),
            # LYNX
            cluster_wire=cd_size,
            cluster_wire_material=cd_mat,
            cluster_wire_strands=cd_strands,
            branch_length_cu=_fmt_len(preset.branch_length_cu),
            branch_length_al=_fmt_len(preset.branch_length_al),
            row_per_overmold=str(preset.row_per_overmold),
            tail_shortened=preset.tail_shortened,
            nearby_overmold_gap=_fmt_len(preset.nearby_overmold_gap),
            buffer_length=_fmt_len(preset.buffer_length),
            # Gap Extender
            connector_male_type=cm_type,
            connector_male_manufacturer=cm_manu,
            connector_male_size=cm_size,
            connector_female_type=cf_type,
            connector_female_manufacturer=cf_manu,
            connector_female_size=cf_size,
            label=preset.label,
            cu_wire=ewire_size,
            cu_wire_strands=ewire_strands,
            length=_fmt_len(preset.length),
            length_displays=length_displays,
        )

    @staticmethod
    def _key_for(product: ProductPreset) -> str:
        return f"product:{id(product)}"

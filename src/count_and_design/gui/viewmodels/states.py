"""State objects used by Qt views."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TreeNodeState:
    node_id: str
    item_type: str
    label: str
    expanded: bool = True
    children: tuple["TreeNodeState", ...] = ()


@dataclass(frozen=True)
class TableSectionState:
    title: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class MetricState:
    """A compact, read-only value shown in the count-tab overview."""

    label: str
    value: str
    tone: str = "neutral"
    activation: object | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class BreakdownState:
    """A named collection of counts, such as rackings or product presets."""

    title: str
    items: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ReadOnlyResultsState:
    """Semantic read-only results rendered below the editable properties."""

    statistics: tuple[MetricState, ...] = ()
    voltage: tuple[MetricState, ...] = ()
    breakdowns: tuple[BreakdownState, ...] = ()
    detail: TableSectionState | None = None

    @property
    def metrics(self) -> tuple[MetricState, ...]:
        """Combined metrics retained for callers that do not need presentation groups."""
        return self.statistics + self.voltage


@dataclass(frozen=True)
class EditorState:
    kind: str
    title: str
    name: str = ""
    address: str = ""
    client_name: str = ""
    project_type: str = ""
    racking_pitch: str = ""
    lynx_preset_name: str = ""
    lynx_preset_options: tuple[str, ...] = ()
    cluster_length: str = ""
    box_position: float = 0.0
    racking_entries: tuple[tuple[str, str], ...] = ()  # ((rackings_name, offset), ...)
    rackings_options: tuple[str, ...] = ()
    identifier_options: tuple[tuple[str, str], ...] = ()
    results: ReadOnlyResultsState = field(default_factory=ReadOnlyResultsState)


@dataclass(frozen=True)
class CountTabState:
    tree: tuple[TreeNodeState, ...] = ()
    selected_node_id: str | None = None
    selected_node_ids: tuple[str, ...] = ()
    editor: EditorState | None = None


@dataclass(frozen=True)
class ListItemState:
    item_id: str
    label: str
    muted: bool = False


@dataclass(frozen=True)
class RackingsDetailState:
    name: str = ""
    product_presets: str = ""
    usage_count: int = 0
    gap_extenders: tuple[tuple[str, str], ...] = ()  # (name, quantity)
    existing_gap_extender_names: tuple[str, ...] = ()  # all gap extender names across the project


@dataclass(frozen=True)
class RackingsTabState:
    items: tuple[ListItemState, ...] = ()
    selected_item_id: str | None = None
    selected_item_ids: tuple[str, ...] = ()
    detail: RackingsDetailState = field(default_factory=RackingsDetailState)


@dataclass(frozen=True)
class ProductPresetDetailState:
    """Detail state for a ProductPreset editor in the ProductPresets tab."""

    # Core
    name: str = ""
    product_type: str = "IBEX"
    remarks: str = ""
    pv_panel: str = ""
    pv_panel_options: tuple[str, ...] = ()
    # IBEX
    branch_count: str = "1"
    # -- Connector sub-fields --
    branch_connector_type: str = ""
    branch_connector_manufacturer: str = ""
    branch_connector_size: str = ""
    branch_connector_material: str = "Cu"
    branch_connector_strands: str = "none"
    external_connector_type: str = ""
    external_connector_manufacturer: str = ""
    external_connector_size: str = ""
    external_connector_material: str = "Cu"
    external_connector_strands: str = "none"
    # -- Fuse sub-fields --
    branch_fuse_current: str = ""
    branch_fuse_manufacturer: str = ""
    external_fuse_current: str = ""
    external_fuse_manufacturer: str = ""
    # -- Last branch (末端支线) sub-fields --
    last_branch_fuse_current: str = ""
    last_branch_fuse_manufacturer: str = ""
    last_branch_cu_wire: str = ""
    last_branch_cu_material: str = "Cu"
    last_branch_cu_strands: str = "none"
    last_branch_al_wire: str = ""
    last_branch_al_material: str = "Al"
    last_branch_al_strands: str = "none"
    # -- Wire fields (size + material + strands) --
    branch_cu_wire: str = ""
    branch_cu_material: str = "Cu"
    branch_cu_strands: str = "none"
    branch_al_wire: str = ""
    branch_al_material: str = "Al"
    branch_al_strands: str = "none"
    external_cu_wire: str = ""
    external_cu_material: str = "Cu"
    external_cu_strands: str = "none"
    external_al_wire: str = ""
    external_al_material: str = "Al"
    external_al_strands: str = "none"
    internal_cu_wire: str = ""
    internal_cu_material: str = "Cu"
    internal_cu_strands: str = "none"
    internal_al_wire: str = ""
    internal_al_material: str = "Al"
    internal_al_strands: str = "none"
    # -- Length fields --
    product_total_length: str = ""
    # External al threshold
    external_al_threshold: str = ""
    # Last branch al threshold
    last_branch_al_threshold: str = ""
    # Internal al threshold
    internal_al_threshold: str = ""
    # IBEX default params
    branch_length_positive: str = ""
    branch_length_negative: str = ""
    last_branch_with_extra_length: bool = False
    branch_fuse_prefix: str = ""
    branch_first_cual_prefix: str = ""
    branch_second_cual_suffix: str = ""
    external_fuse_suffix: str = ""
    external_cu_length_to_row_head: str = ""
    external_second_cual_suffix: str = ""
    external_extension_trench_length: str = ""
    internal_first_cual_prefix: str = ""
    internal_second_cual_suffix: str = ""
    # Branch Coordination (IBEX)
    branch_lines: tuple[tuple[str, str, str], ...] = ()  # (index, positive, negative)
    # Length conversion display info: maps field names to converted display strings (e.g., "≈ 304.8 mm")
    length_displays: dict[str, str] = field(default_factory=dict)
    # LYNX
    cluster_wire: str = ""
    cluster_wire_material: str = "Al"
    cluster_wire_strands: str = "none"
    branch_length_cu: str = ""
    branch_length_al: str = ""
    row_per_overmold: str = "1"
    tail_shortened: bool = False
    nearby_overmold_gap: str = ""
    buffer_length: str = ""
    # Gap Extender
    connector_male_type: str = ""
    connector_male_manufacturer: str = ""
    connector_male_size: str = ""
    connector_female_type: str = ""
    connector_female_manufacturer: str = ""
    connector_female_size: str = ""
    label: str = ""
    cu_wire: str = ""
    cu_wire_strands: str = "none"
    length: str = ""


@dataclass(frozen=True)
class ProductPresetsTabState:
    items: tuple[ListItemState, ...] = ()
    selected_item_id: str | None = None
    selected_item_ids: tuple[str, ...] = ()
    detail_title: str = "Product Preset Properties"
    detail: ProductPresetDetailState = field(default_factory=ProductPresetDetailState)


@dataclass(frozen=True)
class PVPanelDetailState:
    name: str = ""
    vmp: float = 0.0
    imp: float = 0.0
    module_per_string: int = 0
    system_voltage: float = 0.0


@dataclass(frozen=True)
class PVPanelsTabState:
    items: tuple[ListItemState, ...] = ()
    selected_item_id: str | None = None
    detail: PVPanelDetailState = field(default_factory=PVPanelDetailState)


@dataclass(frozen=True)
class LengthIdentifierItemState:
    item_id: str
    label: str


@dataclass(frozen=True)
class LengthIdentifiersDetailState:
    name: str = ""
    length: str = ""


@dataclass(frozen=True)
class LengthIdentifiersTabState:
    items: tuple[LengthIdentifierItemState, ...] = ()
    selected_item_id: str | None = None
    selected_item_ids: tuple[str, ...] = ()
    identifier_options: tuple[tuple[str, str], ...] = ()
    detail: LengthIdentifiersDetailState = field(default_factory=LengthIdentifiersDetailState)


@dataclass(frozen=True)
class HistoryTreeNodeState:
    """A single node in the operation history tree for display."""
    node_id: str
    description: str
    summary: str = ""
    timestamp: float = 0.0
    is_current: bool = False
    is_locked: bool = False
    is_fold_row: bool = False
    fold_count: int = 0
    fold_start_index: int = 0
    children: tuple["HistoryTreeNodeState", ...] = ()


@dataclass(frozen=True)
class HistoryTabState:
    """State for the History tab."""
    tree: tuple[HistoryTreeNodeState, ...] = ()
    current_node_id: str = ""
    current_description: str = ""
    path_to_current: tuple[str, ...] = ()
    search_target_node_id: str = ""


@dataclass(frozen=True)
class AppState:
    title: str = "Count and Design"
    current_file: str | None = None

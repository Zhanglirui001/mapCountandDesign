from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from ...i18n import tr


@dataclass(frozen=True)
class VoltageDropIssueSource:
    """Semantic model location that can be resolved by any presentation layer."""

    object_kind: str
    object_ref: Any = field(compare=False, repr=False)
    field_keys: tuple[str, ...] = ()
    primary_field: str = ""


@dataclass(frozen=True)
class VoltageDropIssue:
    code: str
    message: str
    path: str = ""
    params: dict[str, object] = field(default_factory=dict)
    source: VoltageDropIssueSource | None = field(default=None, compare=False)

    @property
    def fingerprint(self) -> tuple[object, ...]:
        source = self.source
        return (
            self.code,
            id(source.object_ref) if source is not None else None,
            source.object_kind if source is not None else "",
            source.field_keys if source is not None else (),
            source.primary_field if source is not None else "",
            str(self.params.get("detail", "")),
            str(self.params.get("role", "")),
            str(self.params.get("expression", "")),
        )


def format_voltage_drop_issue(issue: VoltageDropIssue) -> str:
    """Return an issue message in the currently selected UI language."""
    key = f"voltage_drop.issue.{issue.code}"
    if issue.code == "missing_pv_panel" and issue.params.get("selection_required"):
        key = "voltage_drop.issue.missing_pv_panel_selection"
    template = tr(key, issue.message)
    try:
        return template.format(**issue.params)
    except (KeyError, ValueError):
        return issue.message


def format_voltage_drop_issue_location(issue: VoltageDropIssue) -> str:
    """Return a compact, auditable location including field and input value."""
    parts = [issue.path] if issue.path else []
    field_name = str(issue.params.get("field", ""))
    expression = str(issue.params.get("expression", ""))
    if field_name:
        parts.append(field_name)
    if expression:
        parts.append(f"{expression!r}")
    return " > ".join(parts)


@dataclass
class LineSegmentDrop:
    instance_id: str
    product_type: str
    polarity: str
    role: str
    wire_size: str
    material: str
    length_ft: float
    current_a: float
    ohms_per_kft: float | None
    voltage_drop: float | None
    path: str


@dataclass
class ProductVoltageDrop:
    instance_id: str
    product_type: str
    preset_name: str
    inv_name: str
    node_name: str
    row_name: str = ""
    rackings_name: str = ""
    entry_index: int = 0
    string_count: int = 0
    pv_panel_name: str = ""
    system_voltage: float = 0.0
    string_current: float = 0.0
    positive_code: str = ""
    negative_code: str = ""
    positive_drop: float | None = None
    negative_drop: float | None = None
    voltage_drop: float | None = None
    voltage_drop_percent: float | None = None
    branch_drop: float | None = None
    mainline_drop: float | None = None
    issues: list[VoltageDropIssue] = field(default_factory=list)
    source_preset: Any = field(default=None, compare=False, repr=False)
    source_node: Any = field(default=None, compare=False, repr=False)
    source_row: Any = field(default=None, compare=False, repr=False)
    source_rackings: Any = field(default=None, compare=False, repr=False)
    source_entry_index: int = field(default=-1, compare=False, repr=False)
    source_pitch: VoltageDropIssueSource | None = field(default=None, compare=False, repr=False)

    @property
    def valid(self) -> bool:
        return self.voltage_drop is not None and not self.issues

    @property
    def path(self) -> str:
        parts = [self.inv_name, self.node_name]
        if self.row_name:
            parts.append(self.row_name)
        parts.append(self.preset_name or self.product_type)
        return " / ".join(part for part in parts if part)


@dataclass
class LevelVoltageDrop:
    level: str
    name: str
    path: str
    voltage_drop: float | None
    voltage_drop_percent: float | None
    product_count: int = 0
    child_count: int = 0
    issues: list[VoltageDropIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.voltage_drop is not None and not self.issues


@dataclass
class VoltageDropReport:
    project_level: LevelVoltageDrop
    levels: list[LevelVoltageDrop] = field(default_factory=list)
    products: list[ProductVoltageDrop] = field(default_factory=list)
    segments: list[LineSegmentDrop] = field(default_factory=list)
    issues: list[VoltageDropIssue] = field(default_factory=list)
    _levels_by_object_id: dict[int, LevelVoltageDrop] = field(default_factory=dict, repr=False)
    _products_by_object_id: dict[int, list[ProductVoltageDrop]] = field(default_factory=dict, repr=False)

    def for_object(self, item: object) -> LevelVoltageDrop | None:
        return self._levels_by_object_id.get(id(item))

    def products_for(self, item: object) -> list[ProductVoltageDrop]:
        return list(self._products_by_object_id.get(id(item), []))

    def rebind_object_references(self, object_map: Mapping[int, object]) -> None:
        """Rebind report references from a calculation snapshot to live objects.

        ``object_map`` is keyed by ``id(snapshot_object)``.  The calculation
        report is otherwise self-contained, so rebinding can happen atomically
        on the GUI thread after a background calculation completes.
        """
        issue_cache: dict[int, VoltageDropIssue] = {}

        def rebind_source(
            source: VoltageDropIssueSource | None,
        ) -> VoltageDropIssueSource | None:
            if source is None:
                return None
            target = object_map.get(id(source.object_ref))
            return replace(source, object_ref=target) if target is not None else source

        def rebind_issue(issue: VoltageDropIssue) -> VoltageDropIssue:
            cached = issue_cache.get(id(issue))
            if cached is not None:
                return cached
            rebound = replace(issue, source=rebind_source(issue.source))
            issue_cache[id(issue)] = rebound
            return rebound

        self.issues = [rebind_issue(issue) for issue in self.issues]
        seen_levels: set[int] = set()
        for level in [self.project_level, *self.levels]:
            if id(level) in seen_levels:
                continue
            seen_levels.add(id(level))
            level.issues = [rebind_issue(issue) for issue in level.issues]

        for product in self.products:
            product.issues = [rebind_issue(issue) for issue in product.issues]
            for field_name in (
                "source_preset",
                "source_node",
                "source_row",
                "source_rackings",
            ):
                source_object = getattr(product, field_name)
                target = object_map.get(id(source_object))
                if target is not None:
                    setattr(product, field_name, target)
            product.source_pitch = rebind_source(product.source_pitch)

        self._levels_by_object_id = {
            id(target): level
            for source_id, level in self._levels_by_object_id.items()
            if (target := object_map.get(source_id)) is not None
        }
        self._products_by_object_id = {
            id(target): products
            for source_id, products in self._products_by_object_id.items()
            if (target := object_map.get(source_id)) is not None
        }

    def require_exportable(self) -> None:
        if self.issues or not self.project_level.valid:
            raise VoltageDropValidationError(self.issues or self.project_level.issues)


class VoltageDropValidationError(ValueError):
    def __init__(self, issues: list[VoltageDropIssue]) -> None:
        self.issues = issues
        detail = "\n".join(
            f"- {format_voltage_drop_issue_location(issue)}: {format_voltage_drop_issue(issue)}"
            if format_voltage_drop_issue_location(issue)
            else f"- {format_voltage_drop_issue(issue)}"
            for issue in issues
        )
        super().__init__(
            detail
            or tr(
                "voltage_drop.error.not_exportable",
                "Voltage drop report is not exportable.",
            )
        )

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from ..file_integrity import (
    IntegrityStatus,
    attach_file_integrity,
    atomic_write_json,
    verify_file_integrity,
)

from .inv import Inv
from .identifier_registry import IdentifierRegistry
from .length import Length, _LENGTH_TOKEN_RE, get_length_field_names
from .length_identifier import LengthIdentifier
from .product_preset import ProductPreset
from .pv_panel import PVPanelInfo
from .rackings import Rackings
from .wire import Wire
from .data_fill import DataFillTarget


@dataclass
class Project:
    name: str = ""
    address: str = ""
    client_name: str = ""
    # Keep the model default aligned with the project-type editor.  Previously
    # the editor displayed IBEX while a new Project still stored an empty
    # string, so the first voltage-drop calculation failed until the user
    # manually changed the combo box.
    project_type: str = "IBEX"
    length_identifiers: list[LengthIdentifier] = field(default_factory=list)
    rackings_list: list[Rackings] = field(default_factory=list)
    product_presets: list[ProductPreset] = field(default_factory=list)
    racking_pitch: Length | None = None
    cluster_wire: Wire | None = None
    lynx_preset: str | None = None
    pv_panels: list[PVPanelInfo] = field(default_factory=list)
    invs: list[Inv] = field(default_factory=list)
    data_fill_targets: list[DataFillTarget] = field(default_factory=list)
    identifier_registry: IdentifierRegistry | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.project_type and self.project_type not in {"IBEX", "LYNX"}:
            raise ValueError("project_type must be IBEX or LYNX.")
        if self.identifier_registry is None:
            self.identifier_registry = IdentifierRegistry(self.length_identifiers)
        self._bind_length_identifiers()

    def _bind_length_identifiers(self) -> None:
        assert self.identifier_registry is not None
        self.identifier_registry.replace_identifiers(self.length_identifiers)
        for length in self._iter_lengths():
            if length is not None:
                length.set_registry(self.identifier_registry)

    def rebind_length_identifiers(self) -> None:
        """Reattach an externally replaced object graph to this registry.

        Normal identifier edits and additions do not require this method;
        it remains for project loading, merge publication, and legacy callers
        that replace model lists directly.
        """
        self._bind_length_identifiers()

    def add_length_identifier(
        self, identifier: LengthIdentifier
    ) -> LengthIdentifier:
        assert self.identifier_registry is not None
        identifier.length.set_registry(self.identifier_registry)
        self.length_identifiers.append(identifier)
        return identifier

    def remove_length_identifier(self, identifier: LengthIdentifier) -> None:
        self.length_identifiers.remove(identifier)

    def bind_product_preset(self, preset: ProductPreset) -> None:
        """Attach every Length owned by *preset* to this project registry."""
        assert self.identifier_registry is not None
        for field_name in get_length_field_names(preset):
            length = getattr(preset, field_name)
            if length is not None:
                length.set_registry(self.identifier_registry)
        for branch in preset.branch_lines:
            self.bind_branch_coordinate(branch)

    def add_product_preset(self, preset: ProductPreset) -> ProductPreset:
        self.bind_product_preset(preset)
        self.product_presets.append(preset)
        return preset

    def bind_branch_coordinate(self, branch: object) -> None:
        """Attach the Length fields of an IBEX branch coordinate."""
        assert self.identifier_registry is not None
        for field_name in get_length_field_names(branch):
            length = getattr(branch, field_name)
            if length is not None:
                length.set_registry(self.identifier_registry)

    def add_branch_coordinate(
        self, preset: ProductPreset, branch: object
    ) -> None:
        self.bind_branch_coordinate(branch)
        preset.branch_lines.append(branch)  # type: ignore[arg-type]

    def _iter_lengths(self) -> list[Length | None]:
        lengths: list[Length | None] = [self.racking_pitch]
        # 遍历标识符自身的 length 表达式（其他标识符可能引用它们）
        for identifier in self.length_identifiers:
            lengths.append(identifier.length)
        # 遍历 invs/nodes/rows 中的长度字段
        for inv in self.invs:
            lengths.append(inv.racking_pitch)
            for node in inv.nodes:
                lengths.extend([node.racking_pitch, node.cluster_length])
                for row in node.rows:
                    for entry in row.racking_entries:
                        lengths.append(entry.offset)
        # 遍历产品预设中的所有长度字段（自动发现，无需手动维护列表）
        for pp in self.product_presets:
            for field_name in get_length_field_names(pp):
                length_val = getattr(pp, field_name)
                if length_val is not None:
                    lengths.append(length_val)
            # 支线坐标中的 Length 字段（由 IBEXBranchCoordinate 自行处理）
            for bl in pp.branch_lines:
                for field_name in get_length_field_names(bl):
                    length_val = getattr(bl, field_name)
                    if length_val is not None:
                        lengths.append(length_val)
        return lengths

    def to_dict(self) -> dict[str, object]:
        from .. import __version__

        return {
            "schema_version": 2,
            "app_version": __version__,
            "name": self.name,
            "address": self.address,
            "client_name": self.client_name,
            "project_type": self.project_type,
            "length_identifiers": [item.to_dict() for item in self.length_identifiers],
            "rackings_list": [item.to_dict() for item in self.rackings_list],
            "product_presets": [item.to_dict() for item in self.product_presets],
            "racking_pitch": self.racking_pitch.to_dict() if self.racking_pitch else None,
            "cluster_wire": self.cluster_wire.to_dict() if self.cluster_wire else None,
            "lynx_preset": self.lynx_preset,
            "pv_panels": [item.to_dict() for item in self.pv_panels],
            "invs": [item.to_dict() for item in self.invs],
            "data_fill_targets": [item.to_dict() for item in self.data_fill_targets],
        }

    def to_json(self, path: str | Path) -> None:
        atomic_write_json(path, attach_file_integrity(self.to_dict()))

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Project":
        schema_version = data.get("schema_version")
        if schema_version is not None and schema_version not in {1, 2}:
            raise ValueError(
                f"Unsupported project schema version: {schema_version}. "
                "Please upgrade Count and Design to open this file."
            )
        identifiers = [
            LengthIdentifier.from_dict(item)  # type: ignore[arg-type]
            for item in data.get("length_identifiers", [])
        ]
        registry = IdentifierRegistry(identifiers)
        # 在构造 Project 之前先解析 identifier 自身的 length 表达式，
        # 否则后续 ProductPreset/IBEXBranchCoordinate 等解析时引用的
        # identifier.length 可能仍处于未解析状态（_needs_identifier_resolution=True）。
        for ident in identifiers:
            ident.length.set_registry(registry)
        project = cls(
            name=str(data.get("name", "")),
            address=str(data.get("address", "")),
            client_name=str(data.get("client_name", "")),
            # Migrate saves produced while the UI showed IBEX without writing
            # that visual default into the model.
            project_type=str(data.get("project_type") or "IBEX"),
            length_identifiers=identifiers,
            identifier_registry=registry,
            rackings_list=[
                Rackings.from_dict(item)  # type: ignore[arg-type]
                for item in data.get("rackings_list", [])
            ],
            product_presets=[
                ProductPreset.from_dict(item, identifiers)  # type: ignore[arg-type]
                for item in data.get("product_presets", [])
            ],
            racking_pitch=Length.from_dict(data.get("racking_pitch"), identifiers),  # type: ignore[arg-type]
            cluster_wire=Wire.from_dict(data.get("cluster_wire")),  # type: ignore[arg-type]
            lynx_preset=str(data["lynx_preset"]) if data.get("lynx_preset") else None,
            pv_panels=[
                PVPanelInfo.from_dict(item)  # type: ignore[arg-type]
                for item in data.get("pv_panels", [])
            ],
            invs=[
                Inv.from_dict(item, identifiers)  # type: ignore[arg-type]
                for item in data.get("invs", [])
            ],
            data_fill_targets=[
                DataFillTarget.from_dict(item)  # type: ignore[arg-type]
                for item in data.get("data_fill_targets", [])
            ],
        )
        return project

    @classmethod
    def from_json(cls, path: str | Path) -> "Project":
        source = Path(path)
        data = json.loads(source.read_text(encoding="utf-8"))
        if verify_file_integrity(data) is IntegrityStatus.MODIFIED:
            raise ValueError("Project file integrity verification failed")
        return cls.from_dict(data)
    def swap(self, index1: int, index2: int) -> None:
        self.invs[index1], self.invs[index2] = self.invs[index2], self.invs[index1]

    def would_create_length_identifier_cycle(
        self, changed_name: str, new_length_str: str
    ) -> bool:
        """Check whether setting *changed_name* to *new_length_str* would
        introduce a circular dependency among length identifiers.

        Builds a dependency graph from all identifiers (using the proposed
        expression for *changed_name*) and runs a DFS colour-based cycle
        detection starting from *changed_name*.
        """
        # Build adjacency: each identifier → set of identifiers it references
        deps: dict[str, set[str]] = {}
        for ident in self.length_identifiers:
            if ident.name == changed_name:
                deps[ident.name] = Length.extract_identifier_refs(new_length_str)
            else:
                deps[ident.name] = Length.extract_identifier_refs(
                    ident.length.length_str
                )

        # Three-colour DFS (WHITE = unvisited, GRAY = in current path, BLACK = done)
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {name: WHITE for name in deps}

        def dfs(node: str) -> bool:
            color[node] = GRAY
            for neighbour in deps.get(node, set()):
                if neighbour not in color:
                    # References an identifier that hasn't been defined yet —
                    # cannot form a cycle involving known identifiers.
                    continue
                if color[neighbour] == GRAY:
                    return True  # back edge → cycle
                if color[neighbour] == WHITE:
                    if dfs(neighbour):
                        return True
            color[node] = BLACK
            return False

        return dfs(changed_name)

    def replace_length_identifier_references(self, old_name: str, new_name: str) -> None:
        """Replace all occurrences of *old_name* with *new_name* in every
        :class:`Length` expression found in the project (including nested
        objects such as product presets and their branch coordinates).

        The replacement operates on the token level using the same tokeniser
        that the length-expression parser uses, so that short identifier
        names (e.g. ``A``) are not accidentally replaced inside longer
        identifier tokens (e.g. ``AB``).
        """
        for length in self._iter_lengths():
            if length is not None:
                tokens = _LENGTH_TOKEN_RE.findall(length.length_str)
                new_tokens = [new_name if t == old_name else t for t in tokens]
                length.length_str = "".join(new_tokens)
                length._recalculate()

    def get_rackings(self, name: str) -> Rackings | None:
        for r in self.rackings_list:
            if r.name == name:
                return r
        return None

    def get_product_preset(self, name: str) -> ProductPreset | None:
        for pp in self.product_presets:
            if pp.name == name:
                return pp
        return None

    def get_pv_panel(self, name: str) -> PVPanelInfo | None:
        normalized = name.strip()
        for panel in self.pv_panels:
            if panel.name == normalized:
                return panel
        return None

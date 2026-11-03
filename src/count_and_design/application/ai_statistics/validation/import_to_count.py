"""Import Agent4 final JSON payloads into Count projects."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ....models import (
    Inv,
    Length,
    LengthIdentifier,
    Node,
    Project,
    RackingEntry,
    Rackings,
    Row,
)


def import_agent4_payload_to_project(
    project: Project,
    payload: Mapping[str, Any],
) -> None:
    """Replace the Count tree with the final Agent4 structure payload."""

    project_type = str(payload.get("project_type") or project.project_type)
    if project_type in {"IBEX", "LYNX"}:
        project.project_type = project_type
    project.length_identifiers = _merge_length_identifiers(
        project.length_identifiers,
        payload.get("length_identifiers", ()),
    )
    project.invs = [
        _parse_inv(item, project.length_identifiers)
        for item in _sequence(payload.get("invs", ()))
        if isinstance(item, Mapping)
    ]
    _ensure_rackings(project)
    project.rebind_length_identifiers()


def _merge_length_identifiers(
    existing: list[LengthIdentifier],
    payload_items: object,
) -> list[LengthIdentifier]:
    by_name = {identifier.name: identifier for identifier in existing}
    merged: list[LengthIdentifier] = []
    used: set[str] = set()
    for item in _sequence(payload_items):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", "")).strip()
        if not name or name in used:
            continue
        label = str(item.get("label", ""))
        identifier = by_name.get(name)
        if identifier is None:
            identifier = LengthIdentifier(name=name, length=Length("0mm"), label=label)
        else:
            identifier.label = label
        merged.append(identifier)
        used.add(name)
    merged.extend(identifier for identifier in existing if identifier.name not in used)
    return merged


def _parse_inv(data: Mapping[str, Any], identifiers: list[LengthIdentifier]) -> Inv:
    return Inv(
        name=str(data.get("name", "")),
        nodes=[
            _parse_node(item, identifiers)
            for item in _sequence(data.get("nodes", ()))
            if isinstance(item, Mapping)
        ],
    )


def _parse_node(data: Mapping[str, Any], identifiers: list[LengthIdentifier]) -> Node:
    return Node(
        name=str(data.get("name", "")),
        box_position=float(data.get("box_position", 0.0) or 0.0),
        rows=[
            _parse_row(item, identifiers)
            for item in _sequence(data.get("rows", ()))
            if isinstance(item, Mapping)
        ],
    )


def _parse_row(data: Mapping[str, Any], identifiers: list[LengthIdentifier]) -> Row:
    entries = [
        _parse_racking_entry(item, identifiers)
        for item in _sequence(data.get("racking_entries", ()))
        if isinstance(item, Mapping)
    ]
    return Row(name=str(data.get("name", "")), racking_entries=entries or [RackingEntry()])


def _parse_racking_entry(
    data: Mapping[str, Any],
    identifiers: list[LengthIdentifier],
) -> RackingEntry:
    offset = data.get("offset", {})
    length_str = "0mm"
    if isinstance(offset, Mapping):
        length_str = str(offset.get("length_str", "0mm") or "0mm")
    return RackingEntry(
        rackings=str(data.get("rackings", "")),
        offset=Length(length_str, identifiers=identifiers),
    )


def _ensure_rackings(project: Project) -> None:
    existing = {rackings.name for rackings in project.rackings_list}
    for inv in project.invs:
        for node in inv.nodes:
            for row in node.rows:
                for entry in row.racking_entries:
                    if entry.rackings and entry.rackings not in existing:
                        project.rackings_list.append(Rackings(entry.rackings))
                        existing.add(entry.rackings)


def _sequence(value: object) -> Sequence[Any]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    return value

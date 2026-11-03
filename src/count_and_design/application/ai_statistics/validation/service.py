"""Agent4 validation calculation for AI Statistics."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..models import (
    InvDraft,
    NodeDraft,
    OcrField,
    RackingEntryDraft,
    ReviewedTree,
    RowDraft,
    SourceReference,
    TrackerOcrTotal,
    TrackerQuantity,
    ValidationDifference,
    ValidationInvComparison,
    ValidationNodeComparison,
    ValidationReport,
    ValidationTrackerComparison,
)

if TYPE_CHECKING:
    from ..element_recognition.contracts import ElementRecognitionResult


class Agent4ValidationError(ValueError):
    """Raised when Agent4 receives malformed validation input."""


class Agent4ValidationService:
    """Build validation reports from Agent2 and Agent3 JSON payloads."""

    def validate(
        self,
        tracker_totals_payload: Mapping[str, Any],
        structure_payload: Mapping[str, Any],
    ) -> ValidationReport:
        tracker_totals = _parse_tracker_totals(tracker_totals_payload)
        reviewed_tree = _parse_reviewed_tree(structure_payload)
        table_rows_by_inv = _table_rows_by_inv(structure_payload)
        tracker_types = _collect_tracker_types(tracker_totals, reviewed_tree)

        inv_comparisons: list[ValidationInvComparison] = []
        legacy_differences: list[ValidationDifference] = []
        structure_tracker_totals: Counter[str] = Counter()

        for inv in reviewed_tree.invs:
            inv_tracker_counts: Counter[str] = Counter()
            node_comparisons: list[ValidationNodeComparison] = []
            inv_calculated_total = 0
            inv_ocr_total = 0
            for node in inv.nodes:
                node_tracker_counts = _tracker_counter(node.tracker_counts)
                calculated_total = _calculated_string_total(node_tracker_counts)
                ocr_total = node.ocr_string_count
                ocr_total_present = node.has_ocr_string_count
                inv_tracker_counts.update(node_tracker_counts)
                structure_tracker_totals.update(node_tracker_counts)
                inv_calculated_total += calculated_total
                if ocr_total_present:
                    inv_ocr_total += ocr_total
                comparison = ValidationNodeComparison(
                    inv_name=inv.name,
                    node_name=node.name,
                    tracker_counts=_quantities_for_types(
                        tracker_types,
                        node_tracker_counts,
                    ),
                    calculated_total=calculated_total,
                    ocr_total=ocr_total,
                    ocr_total_present=ocr_total_present,
                    recognition_id=node.recognition_id,
                )
                node_comparisons.append(comparison)
                legacy_differences.append(
                    ValidationDifference(
                        page_number=inv.page,
                        inv_name=inv.name,
                        node_name=node.name,
                        calculated_total=calculated_total,
                        ocr_total=ocr_total,
                    )
                )

            # Node Details must show the union of the reviewed table and the
            # reviewed structure. A table row that no Node consumed (YOLO
            # missed it or review deleted the Node) has no structure total,
            # so it would otherwise disappear from Agent4's comparison.
            consumed_names = {_name_key(node.name) for node in inv.nodes}
            for table_row in table_rows_by_inv.get(inv.name, ()):
                if _name_key(table_row.node) in consumed_names:
                    continue
                comparison = ValidationNodeComparison(
                    inv_name=inv.name,
                    node_name=table_row.node,
                    tracker_counts=_quantities_for_types(
                        tracker_types,
                        Counter(),
                    ),
                    calculated_total=0,
                    ocr_total=table_row.string_number,
                    ocr_total_present=True,
                    source=SourceReference(
                        page_number=table_row.page,
                        source="agent3:table-row",
                    ),
                )
                node_comparisons.append(comparison)
                inv_ocr_total += table_row.string_number
                legacy_differences.append(
                    ValidationDifference(
                        page_number=table_row.page,
                        inv_name=inv.name,
                        node_name=table_row.node,
                        calculated_total=0,
                        ocr_total=table_row.string_number,
                    )
                )

            inv_comparisons.append(
                ValidationInvComparison(
                    inv_name=inv.name,
                    page=inv.page,
                    node_count=len(inv.nodes),
                    tracker_counts=_quantities_for_types(
                        tracker_types,
                        inv_tracker_counts,
                    ),
                    calculated_total=inv_calculated_total,
                    ocr_total=inv_ocr_total,
                    nodes=tuple(node_comparisons),
                    recognition_id=inv.recognition_id,
                )
            )

        ocr_by_type = {item.tracker_type: item.total for item in tracker_totals}
        tracker_comparisons = tuple(
            ValidationTrackerComparison(
                tracker_type=tracker_type,
                structure_total=structure_tracker_totals[tracker_type],
                ocr_total=ocr_by_type.get(tracker_type, 0),
            )
            for tracker_type in tracker_types
        )
        ocr_grand_total = _to_int(
            tracker_totals_payload.get(
                "grand_total",
                sum(item.total for item in tracker_totals),
            ),
            "grand_total",
        )

        return ValidationReport(
            differences=tuple(legacy_differences),
            source=SourceReference(source="agent4:validation"),
            tracker_totals=tracker_totals,
            reviewed_tree=reviewed_tree,
            inv_comparisons=tuple(inv_comparisons),
            tracker_comparisons=tracker_comparisons,
            calculated_grand_total=sum(structure_tracker_totals.values()),
            ocr_grand_total=ocr_grand_total,
        )


def validation_report_to_export_payload(report: ValidationReport) -> dict[str, Any]:
    """Return the final Agent4 JSON payload used by Download/Import."""

    if report.reviewed_tree is None:
        return {
            "project_type": "",
            "length_identifiers": [],
            "invs": [],
        }
    return reviewed_tree_to_export_payload(report.reviewed_tree)


def reviewed_tree_to_export_payload(tree: ReviewedTree) -> dict[str, Any]:
    """Serialize reviewed Agent3 structure to the Count import JSON shape."""

    return {
        "project_type": tree.project_type,
        "length_identifiers": [
            {
                "name": identifier.name,
                "label": identifier.value,
            }
            for identifier in tree.length_identifiers
        ],
        "invs": [
            {
                "name": inv.name,
                "nodes": [
                    {
                        "name": node.name,
                        "box_position": node.box_position,
                        "rows": [
                            {
                                "name": row.name,
                                "racking_entries": [
                                    {
                                        "rackings": entry.rackings,
                                        "offset": {
                                            "length_str": entry.length_str or "0mm",
                                        },
                                    }
                                    for entry in _export_row_entries(row)
                                ],
                            }
                            for row in node.rows
                        ],
                    }
                    for node in inv.nodes
                ],
            }
            for inv in tree.invs
        ],
    }


def reviewed_tree_to_validation_payload(tree: ReviewedTree) -> dict[str, Any]:
    """Serialize the reviewed Agent3 tree including Agent4-only totals.

    ``recognition_id`` values are carried through so every Agent4 row keeps
    the entity link needed to navigate back to its Agent3 page and Node.
    """

    payload = reviewed_tree_to_export_payload(tree)
    for inv_payload, inv in zip(payload["invs"], tree.invs):
        inv_payload["page"] = inv.page
        inv_payload["recognition_id"] = inv.recognition_id
        for node_payload, node in zip(inv_payload["nodes"], inv.nodes):
            node_payload["recognition_id"] = node.recognition_id
            node_payload["ocr_string_count"] = node.ocr_string_count
            node_payload["has_ocr_string_count"] = node.has_ocr_string_count
            node_payload["tracker_num"] = [
                {
                    "tracker_type": quantity.tracker_type,
                    "tracker_count": quantity.tracker_count,
                }
                for quantity in node.tracker_counts
            ]
    return payload


def element_review_to_validation_payload(
    result: ElementRecognitionResult,
) -> dict[str, Any]:
    """Serialize a reviewed Agent3 result for Agent4, table rows included.

    ``reviewed_tree_to_validation_payload`` alone carries only the Nodes the
    reviewed structure kept, so it hides every reviewed table row that no
    Node consumed. Attaching each page's reviewed table rows to the INV
    branch that owns the page lets Agent4 compare the union of table
    recognition and reviewed structure instead of their intersection.
    """

    payload = reviewed_tree_to_validation_payload(result.reviewed_tree)
    payload["table_rows"] = _agent4_table_rows(result)
    return payload


def _agent4_table_rows(result: ElementRecognitionResult) -> list[dict[str, Any]]:
    inv_name_by_page = _inv_name_by_page(result.reviewed_tree)
    rows: list[dict[str, Any]] = []
    for page in result.pages:
        # A Node's page anchor survives INV renames; the page's own name is
        # only a fallback for a page whose Nodes were all deleted.
        inv_name = inv_name_by_page.get(page.page_number) or _inv_name_for_page(
            result.reviewed_tree,
            page.inv_name,
        )
        if not inv_name:
            # The INV branch that owned this page was deleted; there is no
            # comparison row left to attach the table OCR reference to.
            continue
        for row in page.table_rows:
            node = row.node.strip()
            if not node:
                continue
            rows.append(
                {
                    "inv_name": inv_name,
                    "page": page.page_number,
                    "node": node,
                    "string_number": row.string_number,
                }
            )
    return rows


def _inv_name_by_page(tree: ReviewedTree) -> dict[int, str]:
    """Map every page a reviewed INV owns to that INV's current name."""

    names: dict[int, str] = {}
    for inv in tree.invs:
        if inv.page is not None:
            names.setdefault(inv.page, inv.name)
        for node in inv.nodes:
            if node.source.page_number is not None:
                names.setdefault(node.source.page_number, inv.name)
    return names


def _inv_name_for_page(tree: ReviewedTree, page_inv_name: str) -> str:
    """Resolve a page's reviewed INV name after the branch was renamed.

    Agent3 writes the page's INV name at recognition time and never rewrites
    it on rename, so this lookup is only for pages that lost every Node.
    """

    key = _name_key(page_inv_name)
    if not key:
        return ""
    for inv in tree.invs:
        if _name_key(inv.name) == key:
            return inv.name
    return ""


def _export_row_entries(row: RowDraft) -> tuple[RackingEntryDraft, ...]:
    if row.racking_entries:
        return row.racking_entries
    return tuple(
        RackingEntryDraft(rackings=racking, length_str="0mm")
        for racking in row.rackings
    )


@dataclass(frozen=True)
class _ReviewedTableRow:
    """One reviewed table row carried into the Agent4 comparison."""

    page: int | None
    node: str
    string_number: int


def _table_rows_by_inv(
    payload: Mapping[str, Any],
) -> dict[str, tuple[_ReviewedTableRow, ...]]:
    grouped: dict[str, list[_ReviewedTableRow]] = {}
    rows = _sequence(payload.get("table_rows", ()), "table_rows")
    for index, item in enumerate(rows):
        data = _mapping(item, f"table_rows[{index}]")
        inv_name = str(data.get("inv_name", "")).strip()
        node = str(data.get("node", "")).strip()
        if not inv_name or not node:
            raise Agent4ValidationError(
                f"table_rows[{index}] requires inv_name and node"
            )
        grouped.setdefault(inv_name, []).append(
            _ReviewedTableRow(
                page=_optional_int(
                    data.get("page"),
                    f"table_rows[{index}].page",
                ),
                node=node,
                string_number=_to_int(
                    data.get("string_number", 0),
                    f"table_rows[{index}].string_number",
                ),
            )
        )
    return {name: tuple(items) for name, items in grouped.items()}


def _parse_tracker_totals(payload: Mapping[str, Any]) -> tuple[TrackerOcrTotal, ...]:
    rows = _sequence(payload.get("tracker_totals", ()), "tracker_totals")
    parsed: list[TrackerOcrTotal] = []
    for index, row in enumerate(rows):
        data = _mapping(row, f"tracker_totals[{index}]")
        tracker_type = str(data.get("tracker_type", "")).strip()
        if not tracker_type:
            raise Agent4ValidationError(f"tracker_totals[{index}].tracker_type is required")
        power_counts = _mapping(data.get("power_counts", {}), "power_counts")
        fields = tuple(
            OcrField(name=str(name), value=str(_to_int(value, str(name))))
            for name, value in power_counts.items()
        )
        total = _to_int(
            data.get("total", sum(_to_int(field.value, field.name) for field in fields)),
            f"tracker_totals[{index}].total",
        )
        parsed.append(
            TrackerOcrTotal(
                tracker_type=tracker_type,
                total=total,
                fields=fields,
            )
        )
    return tuple(parsed)


def _parse_reviewed_tree(payload: Mapping[str, Any]) -> ReviewedTree:
    length_identifiers = tuple(
        OcrField(
            name=str(_mapping(item, "length_identifiers[]").get("name", "")),
            value=str(_mapping(item, "length_identifiers[]").get("label", "")),
        )
        for item in _sequence(payload.get("length_identifiers", ()), "length_identifiers")
    )
    invs = []
    for inv_index, inv_item in enumerate(_sequence(payload.get("invs", ()), "invs")):
        inv_data = _mapping(inv_item, f"invs[{inv_index}]")
        nodes = []
        for node_index, node_item in enumerate(_sequence(inv_data.get("nodes", ()), "nodes")):
            node_data = _mapping(node_item, f"nodes[{node_index}]")
            rows = []
            for row_item in _sequence(node_data.get("rows", ()), "rows"):
                row_data = _mapping(row_item, "rows[]")
                entries: list[RackingEntryDraft] = []
                for entry_index, entry_item in enumerate(
                    _sequence(row_data.get("racking_entries", ()), "racking_entries")
                ):
                    entry_data = _mapping(
                        entry_item,
                        f"racking_entries[{entry_index}]",
                    )
                    offset_data = _mapping(
                        entry_data.get("offset", {}),
                        f"racking_entries[{entry_index}].offset",
                    )
                    entries.append(
                        RackingEntryDraft(
                            rackings=str(entry_data.get("rackings", "")),
                            length_str=str(offset_data.get("length_str", "0mm")),
                        )
                    )
                rows.append(
                    RowDraft(
                        name=str(row_data.get("name", "")),
                        rackings=tuple(entry.rackings for entry in entries),
                        racking_entries=tuple(entries),
                    )
                )
            tracker_counts = tuple(
                TrackerQuantity(
                    tracker_type=str(
                        _mapping(item, "tracker_num[]").get("tracker_type", "")
                    ),
                    tracker_count=_to_int(
                        _mapping(item, "tracker_num[]").get("tracker_count", 0),
                        "tracker_count",
                    ),
                )
                for item in _sequence(node_data.get("tracker_num", ()), "tracker_num")
            )
            nodes.append(
                NodeDraft(
                    name=str(node_data.get("name", "")),
                    box_position=float(node_data.get("box_position", 0.0)),
                    rows=tuple(rows),
                    ocr_string_count=_to_int(
                        node_data.get("ocr_string_count", 0),
                        "ocr_string_count",
                    ),
                    has_ocr_string_count=_parse_has_ocr_string_count(node_data),
                    tracker_counts=tracker_counts,
                    recognition_id=str(node_data.get("recognition_id", "")),
                )
            )
        invs.append(
            InvDraft(
                name=str(inv_data.get("name", "")),
                page=_optional_int(inv_data.get("page"), f"invs[{inv_index}].page"),
                nodes=tuple(nodes),
                recognition_id=str(inv_data.get("recognition_id", "")),
            )
        )
    return ReviewedTree(
        invs=tuple(invs),
        project_type=str(payload.get("project_type", "")),
        length_identifiers=length_identifiers,
    )


def _collect_tracker_types(
    tracker_totals: tuple[TrackerOcrTotal, ...],
    reviewed_tree: ReviewedTree,
) -> tuple[str, ...]:
    ordered: list[str] = []
    for row in tracker_totals:
        _append_unique(ordered, row.tracker_type)
    for inv in reviewed_tree.invs:
        for node in inv.nodes:
            for quantity in node.tracker_counts:
                _append_unique(ordered, quantity.tracker_type)
    return tuple(ordered)


def _append_unique(items: list[str], item: str) -> None:
    if item and item not in items:
        items.append(item)


def _name_key(value: str) -> str:
    """Fold case and separators so table and structure names compare equal."""

    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def _tracker_counter(quantities: tuple[TrackerQuantity, ...]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for quantity in quantities:
        counter[quantity.tracker_type] += quantity.tracker_count
    return counter


def _quantities_for_types(
    tracker_types: tuple[str, ...],
    counts: Counter[str],
) -> tuple[TrackerQuantity, ...]:
    return tuple(
        TrackerQuantity(tracker_type=tracker_type, tracker_count=counts[tracker_type])
        for tracker_type in tracker_types
    )


def _calculated_string_total(counts: Counter[str]) -> int:
    return sum(_tracker_width(tracker_type) * count for tracker_type, count in counts.items())


def _tracker_width(tracker_type: str) -> int:
    match = re.search(r"\d+", tracker_type)
    if match is None:
        raise Agent4ValidationError(f"Tracker type has no numeric string count: {tracker_type}")
    return int(match.group(0))


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Agent4ValidationError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise Agent4ValidationError(f"{label} must be a list")
    return value


def _to_int(value: Any, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise Agent4ValidationError(f"{label} must be an integer") from exc


def _optional_int(value: Any, label: str) -> int | None:
    if value in (None, ""):
        return None
    return _to_int(value, label)


def _parse_has_ocr_string_count(node_data: Mapping[str, Any]) -> bool:
    if "has_ocr_string_count" in node_data:
        return bool(node_data["has_ocr_string_count"])
    return "ocr_string_count" in node_data

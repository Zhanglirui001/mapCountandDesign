"""Refresh issues affected by manual review without calling AI providers.

Detection geometry remains evidence from the last recognition. Editing a Box
position does not manufacture a missing Box detection or resolve overlapping
boxes. Table/geometry rebuilds replace their own issues in service.py.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import replace

from ...ai.types import BoundingBox
from .contracts import ElementRecognitionResult, RecognitionIssue, TrackerDefinition
from .inv_name_matching import best_agent2_inv_match
from .issues import IssueCode, create_issue, is_execution_issue
from .node_label import _normalize_name
from .review import summarize_node_rows


def node_name_issues(
    page_number: int,
    nodes: Iterable[tuple[str, str, BoundingBox | None]],
    table_names: Iterable[str],
) -> list[RecognitionIssue]:
    """The same final-name checks for recognition and subsequent edits."""

    nodes = tuple(nodes)
    counts = Counter(_normalize_name(name) for _entity, name, _box in nodes)
    table_labels = {_normalize_name(name): name for name in table_names if _normalize_name(name)}
    table_names = set(table_labels)
    issues = []
    for entity_id, name, bbox in nodes:
        normalized = _normalize_name(name)
        context = {
            "page_number": page_number,
            "entity_id": entity_id,
            "bbox": bbox,
            "name": name,
        }
        if normalized not in table_names:
            issues.append(create_issue(IssueCode.NODE_NAME_UNMATCHED, **context))
        if normalized and counts[normalized] > 1:
            issues.append(create_issue(IssueCode.DUPLICATE_NODE_NAME, **context))
    for normalized in sorted(table_names - counts.keys()):
        issues.append(create_issue(
            IssueCode.TABLE_NODE_NOT_DETECTED, page_number=page_number,
            entity_id=f"p{page_number}:table_node:{normalized}", name=table_labels[normalized],
        ))
    return issues


def missing_node_issues(result: ElementRecognitionResult) -> list[RecognitionIssue]:
    """Reverse table-to-Node coverage, including legacy recognition results."""
    return [issue for page in result.pages for issue in node_name_issues(
        page.page_number,
        ((n.recognition_id, n.name, n.source.bbox) for inv in result.reviewed_tree.invs
         for n in inv.nodes if n.source.page_number == page.page_number),
        (r.node for r in page.table_rows),
    ) if issue.code == IssueCode.TABLE_NODE_NOT_DETECTED.value]


def with_issues(
    result: ElementRecognitionResult,
    issues: tuple[RecognitionIssue, ...] | list[RecognitionIssue],
) -> ElementRecognitionResult:
    """Store each issue once and synchronize canvas/tree issue references."""

    unique = {issue.key: issue for issue in issues}
    by_entity: dict[str, list[str]] = {}
    for issue in unique.values():
        if issue.entity_id:
            by_entity.setdefault(issue.entity_id, []).append(issue.code)
    known = {code.value for code in IssueCode}

    def codes(entity_id: str, existing: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                [code for code in existing if code not in known]
                + by_entity.get(entity_id, [])
            )
        )

    pages = tuple(
        replace(
            page,
            issues=tuple(
                i for i in unique.values() if i.page_number == page.page_number
            ),
            detections=tuple(
                replace(detection, issues=codes(detection.entity_id, detection.issues))
                for detection in page.detections
            ),
        )
        for page in result.pages
    )
    detection_ids = {
        (detection.page_number, detection.bbox, detection.kind): detection.entity_id
        for page in pages
        for detection in page.detections
    }

    def annotation_codes(node, annotation) -> tuple[str, ...]:
        if annotation.source.bbox == node.source.bbox:
            entity_id = node.recognition_id
        else:
            entity_id = detection_ids.get(
                (
                    annotation.source.page_number,
                    annotation.source.bbox,
                    annotation.label,
                ),
                "",
            )
        return codes(entity_id, annotation.issues)

    invs = []
    for inv in result.reviewed_tree.invs:
        nodes = tuple(
            replace(
                node,
                issues=codes(node.recognition_id, node.issues),
                annotations=tuple(
                    replace(annotation, issues=annotation_codes(node, annotation))
                    for annotation in node.annotations
                ),
            )
            for node in inv.nodes
        )
        page_numbers = {node.source.page_number for node in nodes}
        page_numbers.update((inv.page, inv.source.page_number))
        page_numbers.discard(None)
        inv_codes = codes(inv.recognition_id, inv.issues)
        inv_codes = tuple(
            dict.fromkeys(
                (
                    *inv_codes,
                    *(
                        i.code
                        for i in unique.values()
                        if i.page_number in page_numbers and i.severity.value == "error"
                    ),
                )
            )
        )
        invs.append(replace(inv, nodes=nodes, issues=inv_codes))
    page_numbers = {page.page_number for page in pages}
    return replace(
        result,
        pages=pages,
        # Keep unanchored legacy issues visible even when their page is absent.
        issues=tuple(i for i in unique.values() if i.page_number not in page_numbers),
        reviewed_tree=replace(result.reviewed_tree, invs=tuple(invs)),
    )


def refresh_review_issues(
    result: ElementRecognitionResult,
    previous: ElementRecognitionResult,
    *,
    tracker_definitions: tuple[TrackerDefinition, ...] = (),
    expected_inv_names: tuple[str, ...] | None = None,
) -> ElementRecognitionResult:
    """Recheck only edited inputs, preserving unrelated recognition evidence."""

    old_nodes = {
        node.recognition_id: node
        for inv in previous.reviewed_tree.invs
        for node in inv.nodes
        if node.recognition_id
    }
    nodes = {
        node.recognition_id: node
        for inv in result.reviewed_tree.invs
        for node in inv.nodes
        if node.recognition_id
    }
    old_pages = {page.page_number: page for page in previous.pages}
    issues = list(result.all_issues)
    removed = old_nodes.keys() - nodes.keys()
    removed_entities = set(removed)
    for page in result.pages:
        removed_entities.update(
            d.entity_id for d in page.detections if d.parent_id in removed
        )
    issues = [
        i
        for i in issues
        if i.entity_id not in removed_entities or is_execution_issue(i)
    ]

    def discard(entity_ids: set[str], *codes: IssueCode) -> None:
        values = {code.value for code in codes}
        issues[:] = [
            i for i in issues if not (i.entity_id in entity_ids and i.code in values)
        ]

    for page in result.pages:
        page_nodes = [
            node
            for node in nodes.values()
            if node.source.page_number == page.page_number
        ]
        old_page = old_pages.get(page.page_number)
        table_changed = old_page is not None and old_page.table_rows != page.table_rows
        old_page_nodes = [
            node
            for node in old_nodes.values()
            if node.source.page_number == page.page_number
        ]
        names_changed = {(n.recognition_id, n.name) for n in page_nodes} != {
            (n.recognition_id, n.name) for n in old_page_nodes
        }
        if names_changed or table_changed:
            ids = {node.recognition_id for node in page_nodes}
            discard(ids, IssueCode.NODE_NAME_UNMATCHED, IssueCode.DUPLICATE_NODE_NAME)
            table_names = {_normalize_name(row.node) for row in page.table_rows}
            issues.extend(
                node_name_issues(
                    page.page_number,
                    (
                        (node.recognition_id, node.name, node.source.bbox)
                        for node in page_nodes
                    ),
                    table_names,
                )
            )
            for node in page_nodes:
                normalized = _normalize_name(node.name)
                old = old_nodes.get(node.recognition_id)
                if (
                    old is not None and old.name != node.name
                ) or normalized in table_names:
                    discard({node.recognition_id}, IssueCode.NODE_NAME_NO_MATCH)

    tracker_parent_ids = {d.parent_id for page in result.pages for d in page.detections
                          if d.kind == "Tracker"}
    stale_missing_trackers = {i.entity_id for i in issues
                             if i.code == IssueCode.NODE_HAS_NO_TRACKERS.value} & tracker_parent_ids
    for entity_id, node in nodes.items():
        old = old_nodes.get(entity_id)
        if old is None:
            continue
        context = {
            "page_number": node.source.page_number,
            "entity_id": entity_id,
            "bbox": node.source.bbox,
        }
        if entity_id in stale_missing_trackers or old.rows != node.rows or (
            old.ocr_string_count,
            old.has_ocr_string_count,
        ) != (node.ocr_string_count, node.has_ocr_string_count):
            discard(
                {entity_id},
                IssueCode.TRACKER_STRING_BALANCE_FAILED,
                IssueCode.NODE_HAS_NO_TRACKERS,
            )
            total, quantities = summarize_node_rows(
                node, tracker_definitions=tracker_definitions
            )
            if not quantities:
                # Empty derived Rows can mean string allocation failed, even
                # when the source detections still contain assigned Trackers.
                detected_trackers = entity_id in tracker_parent_ids
                issues.append(
                    create_issue(
                        IssueCode.TRACKER_STRING_BALANCE_FAILED if detected_trackers
                        else IssueCode.NODE_HAS_NO_TRACKERS,
                        name=node.name, **({"ocr_count": node.ocr_string_count} if detected_trackers else {}),
                        **context
                    )
                )
            elif node.has_ocr_string_count and total != node.ocr_string_count:
                issues.append(
                    create_issue(
                        IssueCode.TRACKER_STRING_BALANCE_FAILED,
                        name=node.name,
                        ocr_count=node.ocr_string_count,
                        **context,
                    )
                )
        if old.box_position != node.box_position:
            discard({entity_id}, IssueCode.SINGLE_ROW_BOX_POSITION_AMBIGUOUS)

    old_invs = {
        inv.recognition_id: inv
        for inv in previous.reviewed_tree.invs
        if inv.recognition_id
    }
    for inv in result.reviewed_tree.invs:
        old = old_invs.get(inv.recognition_id)
        if old is None or old.name == inv.name:
            continue
        # Merged INVs may span pages; their warnings keep per-page identities.
        page_numbers = {n.source.page_number for n in inv.nodes}
        page_numbers.update((inv.page, inv.source.page_number))
        page_numbers.discard(None)
        ids = {inv.recognition_id, *(f"p{number}:inv" for number in page_numbers)}
        if inv.name.strip():
            discard(
                ids, IssueCode.INV_NAME_AMBIGUOUS, IssueCode.INV_NAME_NOT_RECOGNIZED
            )
        if expected_inv_names is not None:
            discard(ids, IssueCode.INV_NAME_NOT_IN_AGENT2)
            if (
                inv.name.strip()
                and best_agent2_inv_match(inv.name, expected_inv_names) is None
            ):
                issues.append(
                    create_issue(
                        IssueCode.INV_NAME_NOT_IN_AGENT2,
                        inferred=inv.name,
                        expected_text=", ".join(expected_inv_names) or "<empty>",
                        page_number=inv.page or inv.source.page_number,
                        entity_id=inv.recognition_id,
                    )
                )
    issues = [i for i in issues if i.code != IssueCode.TABLE_NODE_NOT_DETECTED.value]
    issues.extend(missing_node_issues(result))
    return with_issues(result, issues)

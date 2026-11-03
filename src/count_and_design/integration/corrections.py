"""Validated semantic edits; all derived geometry uses the production builder."""

from dataclasses import replace
import math
from uuid import uuid4

from ..application.ai.types import BoundingBox, DetectionResult
from ..application.ai_statistics.models import NodeDraft, InvDraft, SourceReference
from ..application.ai_statistics.element_recognition.contracts import (
    ArrayLayoutPageInput, ElementDetection, NodeStringTableRow, PanelOrientation,
)
from ..application.ai_statistics.element_recognition.service import (
    _build_node, _length_identifiers, _intersection_over_union,
    annotate_tracker_length_outliers, resolve_table_string_count, OVERLAP_ISSUE_IOU_THRESHOLD,
)
from ..application.ai_statistics.element_recognition.issue_review import (
    refresh_review_issues, node_name_issues, with_issues,
)
from ..application.ai_statistics.element_recognition.issues import (
    IssueCode, create_issue, is_execution_issue,
)
from ..application.ai_statistics.element_recognition.node_label import _normalize_name


def pixel_box(values, page):
    """External coordinates are normalized xyxy on the rendered PDF page."""
    if not isinstance(values, list) or len(values) != 4:
        raise ValueError("bbox must contain four normalized xyxy numbers")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
        raise ValueError("bbox coordinates must be numbers")
    x1, y1, x2, y2 = values
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in values):
        raise ValueError("bbox coordinates must be finite and in [0, 1]")
    if x1 >= x2 or y1 >= y2:
        raise ValueError("bbox must have positive width and height")
    return BoundingBox(x1 * page.image_width, y1 * page.image_height,
                       x2 * page.image_width, y2 * page.image_height)


def correct_result(result, request, operations):
    """Return a candidate without changing workflow state or calling AI providers.

    Supports evidence-backed page recovery, Node/Tracker/Box insertion,
    detection movement/reparenting, Node naming and table replacement.
    """
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise ValueError("Supply 1..100 correction operations")
    pages = {p.page_number: p for p in result.pages}
    nodes = {n.recognition_id: n for inv in result.reviewed_tree.invs for n in inv.nodes
             if n.recognition_id}
    original_nodes = dict(nodes)
    original_pages = dict(pages)
    changed_pages = set()
    manual_notes = {i.entity_id: i for i in result.all_issues
                    if i.code == IssueCode.MANUAL_NODE_REVIEW_REQUIRED.value}
    # Names are editable labels, not identities. In particular, separate pages
    # commonly start with the same empty INV name.
    invs = list(result.reviewed_tree.invs)
    inv_nodes = [list(inv.nodes) for inv in invs]
    allowed = {
        "mark_node_for_review": {"op", "page_number", "name", "bbox", "reason"},
        "resolve_node_review": {"op", "page_number", "entity_id", "reason"},
        "initialize_page": {"op", "page_number", "inv_name", "image_width", "image_height", "reason"},
        "add_detection": {"op", "page_number", "kind", "bbox", "parent_id", "name", "inv_name", "reason"},
        "update_detection": {"op", "page_number", "entity_id", "bbox", "parent_id", "reason"},
        "delete_detection": {"op", "page_number", "entity_id", "reason"},
        "rename_node": {"op", "page_number", "entity_id", "name", "reason"},
        "delete_node": {"op", "page_number", "entity_id", "reason"},
        "rename_inv": {"op", "page_number", "name", "reason"},
        "replace_table_rows": {"op", "page_number", "rows", "reason"},
    }
    for op in operations:
        if not isinstance(op, dict) or op.get("op") not in allowed:
            raise ValueError("Unknown correction operation")
        action = op["op"]
        if set(op) - allowed[action]:
            raise ValueError("Unknown fields in correction operation")
        if not isinstance(op.get("reason"), str) or not op["reason"].strip():
            raise ValueError("Each correction requires a reason grounded in evidence")
        number = op.get("page_number")
        if action == "initialize_page":
            from ..application.ai_statistics.element_recognition.contracts import ElementPageResult
            if type(number) is not int or number not in {p.page_number for p in request.pages} or number in pages:
                raise ValueError("Initialize only a missing layout page")
            if op.get("inv_name") not in request.expected_inv_names:
                raise ValueError("Initialize requires an expected INV name")
            if any(type(op.get(k)) is not int or not 1 <= op[k] <= 20000 for k in ("image_width", "image_height")):
                raise ValueError("Supply original evidence image dimensions")
            pages[number] = ElementPageResult(number, op["inv_name"], op["image_width"], op["image_height"])
            changed_pages.add(number)
            continue
        if type(number) is not int or number not in pages:
            raise ValueError("Unknown page_number (page numbers start at 1)")
        page = pages[number]
        if action == "mark_node_for_review":
            name = op.get("name")
            if not isinstance(name, str) or not _normalize_name(name):
                raise ValueError("Supply a candidate name/label for manual review")
            key = f"p{number}:manual_node:{_normalize_name(name)}"
            manual_notes[key] = create_issue(
                IssueCode.MANUAL_NODE_REVIEW_REQUIRED, page_number=number,
                entity_id=key, name=name.strip(), detail=op["reason"],
                bbox=pixel_box(op["bbox"], page) if op.get("bbox") is not None else None,
            )
            continue
        if action == "resolve_node_review":
            note = manual_notes.get(op.get("entity_id"))
            if note is None or note.page_number != number:
                raise ValueError("Unknown manual Node review item on this page")
            del manual_notes[note.entity_id]
            continue
        detections = list(page.detections)
        if action == "rename_inv":
            name = op.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("INV name cannot be empty")
            matches = [index for index, inv in enumerate(invs)
                       if number in {inv.page, inv.source.page_number,
                                     *(n.source.page_number for n in inv_nodes[index])}]
            if len(matches) != 1:
                raise ValueError("Page must identify exactly one INV")
            index = matches[0]
            if any(n.source.page_number != number for n in inv_nodes[index]):
                raise ValueError("Cannot rename a merged INV from only one page")
            invs[index] = replace(invs[index], name=name.strip())
            page = replace(page, inv_name=name.strip())
        elif action == "delete_node":
            node = nodes.get(op.get("entity_id"))
            if node is None or node.source.page_number != number:
                raise ValueError("Unknown Node on this page")
            if any(d.parent_id == node.recognition_id for d in detections):
                raise ValueError("Reparent or delete all children before deleting a Node")
            detections = [d for d in detections if d.entity_id != node.recognition_id]
            del nodes[node.recognition_id]
            inv_nodes = [[n for n in members if n.recognition_id != node.recognition_id]
                         for members in inv_nodes]
        elif action == "replace_table_rows":
            rows = op.get("rows")
            if not isinstance(rows, list):
                raise ValueError("rows must be a list")
            names = set()
            converted = []
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"node", "string_number"}:
                    raise ValueError("Each table row requires node and string_number")
                name, count = row["node"], row["string_number"]
                if not isinstance(name, str) or not name.strip() or type(count) is not int or count < 0:
                    raise ValueError("Table rows require a name and non-negative integer count")
                key = "".join(name.split()).casefold()
                if key in names:
                    raise ValueError("Duplicate table row name")
                names.add(key)
                converted.append(NodeStringTableRow(name.strip(), count))
            page = replace(page, table_rows=tuple(converted))
        elif action == "rename_node":
            node = nodes.get(op.get("entity_id"))
            name = op.get("name")
            if node is None or node.source.page_number != number:
                raise ValueError("Unknown Node on this page")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Node name cannot be empty")
            nodes[node.recognition_id] = replace(node, name=name.strip())
            detections = [replace(d, label=name.strip()) if d.entity_id == node.recognition_id else d
                          for d in detections]
        else:
            if action == "add_detection":
                if op.get("kind") not in {"Node", "Tracker", "Box"}:
                    raise ValueError("Only Node, Tracker and Box can be added")
                detection = ElementDetection(
                    entity_id=f"p{number}:codex:{uuid4().hex}", kind=op["kind"],
                    confidence=1.0, bbox=pixel_box(op.get("bbox"), page),
                    page_number=number, parent_id=op.get("parent_id", ""),
                    source="codex:review", label=op["kind"],
                )
                detections.append(detection)
                if detection.kind == "Node":
                    name, inv_name = op.get("name"), op.get("inv_name", page.inv_name)
                    page_owned_inv = bool(inv_name and inv_name == page.inv_name and any(
                        inv.name == inv_name and number in {
                            inv.page, inv.source.page_number,
                            *(n.source.page_number for n in inv_nodes[i])}
                        for i, inv in enumerate(invs)))
                    if (not isinstance(name, str) or not name.strip()
                            or not (inv_name in request.expected_inv_names or page_owned_inv)):
                        raise ValueError("Node requires name and an expected or existing page-owned INV")
                    node = NodeDraft(name.strip(), recognition_id=detection.entity_id,
                                     source=SourceReference(page_number=number, bbox=detection.bbox))
                    nodes[node.recognition_id] = node
                    matches = [i for i, inv in enumerate(invs) if inv.name == inv_name]
                    if len(matches) > 1:
                        matches = [i for i in matches if number in {
                            invs[i].page, invs[i].source.page_number,
                            *(n.source.page_number for n in inv_nodes[i])}]
                    if len(matches) > 1:
                        raise ValueError("Ambiguous INV for new Node")
                    if matches:
                        inv_nodes[matches[0]].append(node)
                    else:
                        invs.append(InvDraft(inv_name, page=number))
                        inv_nodes.append([node])
                    detections[-1] = replace(detection, label=name.strip())
            else:
                detection = next((d for d in detections if d.entity_id == op.get("entity_id")), None)
                if detection is None:
                    raise ValueError("Unknown detection on this page")
                if action == "delete_detection":
                    if detection.kind not in {"Tracker", "Box"}:
                        raise ValueError("Only Tracker and Box can be deleted")
                    detections.remove(detection)
                else:
                    if not ({"bbox", "parent_id"} & op.keys()):
                        raise ValueError("Update requires bbox or parent_id")
                    if detection.kind == "Node" and "parent_id" in op:
                        raise ValueError("Node parent cannot be changed")
                    updated = replace(
                        detection, bbox=pixel_box(op["bbox"], page) if "bbox" in op else detection.bbox,
                        parent_id=op.get("parent_id", detection.parent_id), source="codex:review",
                    )
                    detections[detections.index(detection)] = updated
                    detection = updated
            if action != "delete_detection" and detection.kind in {"Tracker", "Box"}:
                parent = nodes.get(detection.parent_id)
                if parent is None or parent.source.page_number != number:
                    raise ValueError("Tracker/Box requires a Node parent on the same page")
        pages[number] = replace(page, detections=tuple(detections))
        changed_pages.add(number)

    # Rebuild only Nodes whose geometry inputs or matched table count changed.
    # INV naming and unrelated edits must preserve reviewed Rows on this page.
    rebuild_ids = set()
    def children(page, entity_id):
        return [(d.entity_id, d.kind, d.bbox, d.parent_id) for d in page.detections
                if d.parent_id == entity_id]

    for number in changed_pages:
        page, old_page = pages[number], original_pages.get(number)
        for node in nodes.values():
            if node.source.page_number != number:
                continue
            old = original_nodes.get(node.recognition_id)
            detected = next((d for d in page.detections if d.entity_id == node.recognition_id), None)
            prior = next((d for d in old_page.detections if d.entity_id == node.recognition_id), None) if old_page else None
            if (old is None or old_page is None or prior is None or detected is None
                    or prior.bbox != detected.bbox
                    or children(page, node.recognition_id) != children(old_page, node.recognition_id)
                    or resolve_table_string_count(node.name, page.table_rows) !=
                       resolve_table_string_count(old.name, old_page.table_rows)):
                rebuild_ids.add(node.recognition_id)
    deleted_ids = original_nodes.keys() - nodes.keys()

    # Replace only issues that this rebuild can derive. INV/name-inference
    # warnings and provider history must not disappear after an unrelated edit.
    rebuilt_codes = {
        IssueCode.UNASSIGNED_TRACKER.value, IssueCode.UNASSIGNED_BOX.value,
        IssueCode.OVERLAPPING_NODE_BOXES.value, IssueCode.OVERLAPPING_TRACKER_BOXES.value,
        IssueCode.TRACKER_LENGTH_OUTLIER.value, IssueCode.TRACKER_STRING_BALANCE_FAILED.value,
        IssueCode.NODE_HAS_NO_TRACKERS.value, IssueCode.NODE_HAS_NO_BOX.value,
        IssueCode.NODE_HAS_MULTIPLE_BOXES.value, IssueCode.SINGLE_ROW_BOX_POSITION_AMBIGUOUS.value,
        IssueCode.NODE_NAME_UNMATCHED.value, IssueCode.DUPLICATE_NODE_NAME.value,
    }
    page_codes = {IssueCode.UNASSIGNED_TRACKER.value, IssueCode.UNASSIGNED_BOX.value,
                  IssueCode.OVERLAPPING_NODE_BOXES.value, IssueCode.OVERLAPPING_TRACKER_BOXES.value,
                  IssueCode.NODE_NAME_UNMATCHED.value, IssueCode.DUPLICATE_NODE_NAME.value}
    page_codes.add(IssueCode.TABLE_NODE_NOT_DETECTED.value)
    issues = [i for i in result.all_issues if is_execution_issue(i) or not (
        i.entity_id in deleted_ids or
        (i.page_number in changed_pages and i.code in page_codes) or
        (i.entity_id in rebuild_ids and i.code in rebuilt_codes))]
    issues = [i for i in issues if i.code != IssueCode.MANUAL_NODE_REVIEW_REQUIRED.value]
    issues.extend(manual_notes.values())
    _, identifiers = _length_identifiers(request.tracker_definitions)
    for number in changed_pages:
        page = pages[number]
        page_nodes = [n for n in nodes.values() if n.source.page_number == number]
        for node in page_nodes:
            if node.recognition_id not in rebuild_ids:
                continue
            detected = next((d for d in page.detections if d.entity_id == node.recognition_id), None)
            if detected is None:
                raise ValueError("Cannot rebuild a Node without its source detection")
            count, present = resolve_table_string_count(node.name, page.table_rows)
            if not present:
                # An absent/ambiguous table row must never inherit another name's count.
                count = 0
            rebuilt, node_issues = _build_node(
                ArrayLayoutPageInput(number, b""), node.recognition_id, node.name,
                str(node.source.raw.get("ocr_text", "")),
                DetectionResult(detected.label, detected.confidence, detected.bbox, source=detected.source),
                [d for d in page.detections if d.kind == "Tracker" and d.parent_id == node.recognition_id],
                [d for d in page.detections if d.kind == "Box" and d.parent_id == node.recognition_id],
                count, request.tracker_definitions, identifiers,
                PanelOrientation.from_value(node.panel_orientation or request.default_panel_orientation.value),
                node.offset_snap_step_strings, has_ocr_string_count=present,
            )
            nodes[node.recognition_id] = replace(
                rebuilt, state=node.state,
                inherits_panel_orientation=node.inherits_panel_orientation,
                inherits_offset_snap_step=node.inherits_offset_snap_step,
            )
            issues.extend(node_issues)
        issues.extend(node_name_issues(number,
                      [(n.recognition_id, n.name, n.source.bbox) for n in page_nodes],
                      [r.node for r in page.table_rows]))
        for kind, code in (("Tracker", IssueCode.UNASSIGNED_TRACKER), ("Box", IssueCode.UNASSIGNED_BOX)):
            for d in page.detections:
                if d.kind == kind and not d.parent_id:
                    issues.append(create_issue(code, page_number=number, entity_id=d.entity_id, bbox=d.bbox))
        for kind, code in (("Node", IssueCode.OVERLAPPING_NODE_BOXES), ("Tracker", IssueCode.OVERLAPPING_TRACKER_BOXES)):
            group = [d for d in page.detections if d.kind == kind]
            for index, left in enumerate(group):
                for right in group[index + 1:]:
                    iou = _intersection_over_union(left.bbox, right.bbox)
                    if iou <= OVERLAP_ISSUE_IOU_THRESHOLD:
                        continue
                    for d, peer in ((left, right), (right, left)):
                        params = ({"node_id": d.entity_id, "peers": peer.entity_id} if kind == "Node" else
                                  {"pair_labels": f"{left.entity_id}/{right.entity_id}", "target": kind, "target_id": d.entity_id})
                        issues.append(create_issue(code, page_number=number, entity_id=d.entity_id,
                                      bbox=d.bbox, threshold=OVERLAP_ISSUE_IOU_THRESHOLD, max_iou=iou, **params))
    tree = replace(result.reviewed_tree, invs=tuple(
        replace(inv, nodes=tuple(nodes.get(n.recognition_id, n) for n in members))
        for inv, members in zip(invs, inv_nodes)))
    candidate = replace(result, pages=tuple(pages[n] for n in sorted(pages)), reviewed_tree=tree)
    candidate = refresh_review_issues(with_issues(candidate, issues), result,
                                     tracker_definitions=request.tracker_definitions,
                                     expected_inv_names=request.expected_inv_names)
    return annotate_tracker_length_outliers(candidate, request.tracker_definitions)

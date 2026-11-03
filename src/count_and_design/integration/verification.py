"""Deterministic correction checks; issue counts alone are not acceptance."""

from collections import Counter
from ..application.ai_statistics.element_recognition.review import summarize_node_rows
from ..application.ai_statistics.element_recognition.issue_review import missing_node_issues


def current_issues(result):
    issues = {i.key: i for i in result.all_issues if i.code != "table_node_not_detected"}
    issues.update((i.key, i) for i in missing_node_issues(result))
    return tuple(issues.values())


def result_checks(result, report, request):
    nodes = {n.recognition_id: n for inv in result.reviewed_tree.invs for n in inv.nodes
             if n.recognition_id}
    assigned = Counter(d.parent_id for p in result.pages for d in p.detections if d.kind == "Tracker")
    mismatches = []
    derived_total = 0
    for key, node in nodes.items():
        _, quantities = summarize_node_rows(node, tracker_definitions=request.tracker_definitions)
        derived = sum(q.tracker_count for q in quantities)
        cached = sum(q.tracker_count for q in node.tracker_counts)
        derived_total += derived
        if assigned[key] != derived or cached != derived:
            mismatches.append({"page_number": node.source.page_number, "entity_id": key,
                               "name": node.name, "detected_trackers": assigned[key],
                               "derived_trackers": derived,
                               "cached_trackers": cached,
                               "row_count": len(node.rows),
                               "row_entry_count": sum(len(r.racking_entries) for r in node.rows)})
    errors = [i for i in current_issues(result) if i.severity.value == "error"]
    causes = Counter(i.code for i in current_issues(result))
    missing_nodes = [{"page_number": i.page_number, "candidate_id": i.entity_id,
                      "name": i.params["name"], "status": "needs_manual_confirmation",
                      "reason": "table_entry_without_matching_node",
                      "bbox": None, "counted_in_structure": False}
                     for i in missing_node_issues(result)]
    blockers = []
    if not report.passed:
        blockers.append("validation_failed")
    if errors:
        blockers.append("remaining_error_issues")
    if mismatches:
        blockers.append("detection_structure_count_mismatch")
    unassigned = sum(count for key, count in assigned.items() if key not in nodes)
    if unassigned:
        blockers.append("unassigned_tracker_detections")
    if request.default_panel_orientation.value == "unknown":
        blockers.append("panel_orientation_unknown")
    if causes["inv_name_not_in_agent2"]:
        blockers.append("inv_reference_incomplete_or_mismatched")
    if missing_nodes:
        blockers.append("unresolved_missing_node_candidates")
    manual = [{"page_number": i.page_number, "candidate_id": i.entity_id,
               "name": i.params.get("name"), "reason": i.params.get("detail"),
               "status": "needs_manual_confirmation", "counted_in_structure": False,
               "bbox_pixels": ({k: getattr(i.bbox, k) for k in ("x1", "y1", "x2", "y2")}
                               if i.bbox else None)}
              for i in result.all_issues if i.code == "manual_node_review_required"]
    if manual:
        blockers.append("pending_manual_node_review")
    return {"status": "needs_follow_up" if blockers else "checks_passed",
            "blockers": blockers, "validation_passed": report.passed,
            "detected_trackers": sum(assigned.values()),
            "derived_trackers": derived_total, "unassigned_trackers": unassigned,
            "node_count_mismatches": mismatches, "issue_counts_by_code": dict(causes),
            "remaining_error_count": len(errors),
            "missing_node_candidates": missing_nodes,
            "manual_review_queue": manual,
            "scope": "Structural/count checks only; not proof of visual correctness."}


def issue_changes(before, after):
    old = {i.key: i for i in current_issues(before)}
    new = {i.key: i for i in current_issues(after)}
    def describe(keys, items):
        return [{"code": items[k].code, "page_number": items[k].page_number,
                 "entity_id": items[k].entity_id, "severity": items[k].severity.value}
                for k in sorted(keys, key=str)]
    return {"cleared_records": describe(old.keys() - new.keys(), old),
            "persistent_records": describe(old.keys() & new.keys(), new),
            "introduced_records": describe(new.keys() - old.keys(), new),
            "introduced_error_count": sum(new[k].severity.value == "error" for k in new.keys() - old.keys())}

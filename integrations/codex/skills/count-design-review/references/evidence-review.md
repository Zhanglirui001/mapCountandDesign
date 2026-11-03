# Evidence-based drawing correction

Use this procedure when the user requests verification and correction of recognition issues. A request to explain the method or list suspects does not by itself authorize changing the drawing result.

## Establish what each source proves

Group reported issues by page and likely shared cause before fetching crops. Retain a compact evidence ledger: project/revision, page, crop coverage, evidence ID, affected entity IDs, observed source details, proposed operation and unresolved doubt. Read original and overlay together: the original establishes drawing content; the overlay establishes which detection or parent assignment is wrong. OCR text and PDF text extraction help locate labels but are not independent confirmation of the image.

Fetch only enough crop coverage to resolve the particular doubt. A label crop can prove a name, but cannot prove a distant tracker's geometry. Map PDF coordinates through its rotation and renderer before proposing normalized boxes; do not reuse unconverted PDF text coordinates. After a rerun, obtain current evidence for the proposed edits even when the same source PDF remains unchanged.

## Choose a correction supported by the drawing

- Missing INV names: inspect the relevant title block or an unambiguous equipment label; cross-check the overall map/list when available. Do not infer names solely from page sequence. A verified INV list does not establish per-INV string counts.
- Truncated Node boundary or unassigned Tracker: check the complete row or connected group, its label and table row. Extend the boundary only to the visible group and explicitly assign the affected detection to its verified parent.
- Suspected false Tracker: inspect panel segmentation, symbol meaning and nearby installed/unused patterns. Grey fill, white fill or low confidence alone is insufficient; drawing conventions differ. Delete only when the source establishes a non-equipment mark, duplicate, or equipment excluded by the project's counting scope.
- Multiple Boxes: distinguish duplicate detections of one symbol from distinct physical symbols. Confidence can select between duplicate boxes only after the original establishes that there is one symbol.
- False overlapping Node: establish that it is a partial duplicate with no independent group identity. Reparent valid children before `delete_node`; absence of a recognized label alone does not prove a false Node.
- Missing Box: search the relevant group boundary/label area. If the symbol or location remains ambiguous, retain the issue. Never invent geometry from a count discrepancy.
- Table versus graphic mismatch: verify both the printed table and visible tracker group, using the project's actual Tracker/string definitions. If both readings are clear but conflict, preserve the source discrepancy instead of rewriting one to match the other.

These are decision criteria, not automatic rules for a particular drawing style. Do not generalize project-specific identifiers, page numbers, colors or quantities into the skill.

## Preview protects both correctness and completeness

Submit compatible operations together with current evidence. Inspect remaining issues and numerical comparisons, then check:

1. INV branches and result pages are retained except for explicitly intended changes. Empty or repeated INV names are not unique identities.
2. The set of removed Node recognition IDs equals the intended Node deletions; unrelated IDs and page membership remain intact. Check parent links for reassignments as well as names and totals.
3. Changed Nodes and count deltas are explainable by the proposed operations. A dramatic issue reduction with disappearing branches is a failed preview.

If these checks fail, do not apply. Retain the original result and identify the tool defect. Source-code repair or restarting a live project requires authorization covering that work; an ordinary drawing correction request does not automatically authorize it. Repair tests should cover the demonstrated failure and preservation of unrelated branches, then use fresh project evidence after restart.

## Finish with an auditable, qualified result

Apply only a sound preview, retain its receipt/audit path, and record the result review at the returned revision. Report actual changes, issues resolved/remaining, original pages and identifiers for unresolved cases, and the saved result path.

Separate three comparisons: detected structure versus Node-table strings; summed Node-table strings versus overall strings; and calculated Tracker quantities versus overall Tracker quantities. Their units and failure causes differ. A failed balance may leave a Node's calculated count at zero; disclose this instead of treating zero as physical absence. Unknown orientation or mixed source models remain explicit limitations. A completed review gate means triage finished, not that all validation passed or all original pages were visually checked.

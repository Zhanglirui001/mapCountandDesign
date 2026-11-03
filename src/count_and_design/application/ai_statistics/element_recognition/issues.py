"""Agent3 issue catalog. Severity describes a result; it never aborts a run.

Keep triggering predicates beside the business operation that owns them.
Provider failures are execution history; result issues may be replaced after
review. Fatal failures continue to use Agent3Error and the workflow boundary.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from ...ai.types import BoundingBox
from .contracts import RecognitionIssue, RecognitionIssueSeverity


class IssueCode(Enum):
    UNASSIGNED_TRACKER = "unassigned_tracker"
    UNASSIGNED_BOX = "unassigned_box"
    NODE_NAME_NO_MATCH = "node_name_no_match"
    NODE_NAME_UNMATCHED = "node_name_unmatched"
    TABLE_NODE_NOT_DETECTED = "table_node_not_detected"
    MANUAL_NODE_REVIEW_REQUIRED = "manual_node_review_required"
    INV_NAME_AMBIGUOUS = "inv_name_ambiguous"
    INV_NAME_NOT_RECOGNIZED = "inv_name_not_recognized"
    INV_NAME_NOT_IN_AGENT2 = "inv_name_not_in_agent2"
    SINGLE_ROW_BOX_POSITION_AMBIGUOUS = "single_row_box_position_ambiguous"
    TRACKER_LENGTH_OUTLIER = "tracker_length_outlier"
    TRACKER_DETECTION_FAILED = "tracker_detection_failed"
    BOX_DETECTION_FAILED = "box_detection_failed"
    OVERLAPPING_NODE_BOXES = "overlapping_node_boxes"
    OVERLAPPING_TRACKER_BOXES = "overlapping_tracker_boxes"
    NODE_STRING_TABLE_FAILED = "node_string_table_failed"
    NODE_NAME_OCR_FAILED = "node_name_ocr_failed"
    NODE_NAME_REVIEW_FAILED = "node_name_review_failed"
    DUPLICATE_NODE_NAME = "duplicate_node_name"
    TRACKER_STRING_BALANCE_FAILED = "tracker_string_balance_failed"
    NODE_HAS_NO_TRACKERS = "node_has_no_trackers"
    NODE_HAS_NO_BOX = "node_has_no_box"
    NODE_HAS_MULTIPLE_BOXES = "node_has_multiple_boxes"
    MIXED_EQUIPMENT_FAMILY = "mixed_equipment_family"


class IssueLifetime(Enum):
    EXECUTION = "execution"
    RESULT = "result"


@dataclass(frozen=True)
class IssueDefinition:
    severity: RecognitionIssueSeverity
    template: str
    lifetime: IssueLifetime = IssueLifetime.RESULT


_WARNING = RecognitionIssueSeverity.WARNING
_ERROR = RecognitionIssueSeverity.ERROR
_EXECUTION = IssueLifetime.EXECUTION

# Templates also document what each code means. Codes and severities stay
# stable across wording changes; callers supply context, never a severity.
ISSUE_DEFINITIONS = MappingProxyType(
    {
        IssueCode.UNASSIGNED_TRACKER: IssueDefinition(
            _WARNING, "Tracker center is outside every detected Node."
        ),
        IssueCode.UNASSIGNED_BOX: IssueDefinition(
            _WARNING, "Box center is outside every detected Node."
        ),
        IssueCode.NODE_NAME_NO_MATCH: IssueDefinition(
            _WARNING,
            "The Node reader returned no matching name; the Node keeps the placeholder name '{name}' for manual review.",
        ),
        IssueCode.NODE_NAME_UNMATCHED: IssueDefinition(
            _WARNING, "Could not match the final Node name to the string table: {name}"
        ),
        IssueCode.TABLE_NODE_NOT_DETECTED: IssueDefinition(
            _WARNING,
            "String table lists '{name}', but no recognized Node matches it. Possible missed/occluded Node or table/name error; locate in original drawing or retain for manual review.",
        ),
        IssueCode.MANUAL_NODE_REVIEW_REQUIRED: IssueDefinition(
            _WARNING, "Node candidate '{name}' needs manual confirmation: {detail}",
        ),
        IssueCode.INV_NAME_AMBIGUOUS: IssueDefinition(
            _WARNING, "Node-name INV candidates have no unique majority: {candidates}"
        ),
        IssueCode.INV_NAME_NOT_RECOGNIZED: IssueDefinition(
            _WARNING,
            "No Node name contained an INV prefix before CBX/LBD, and none carried an Agent2 INV; the page remains under an unnamed INV.",
        ),
        IssueCode.INV_NAME_NOT_IN_AGENT2: IssueDefinition(
            _WARNING,
            "Recognized INV '{inferred}' is absent from Agent2's INV list: {expected_text}.",
        ),
        IssueCode.SINGLE_ROW_BOX_POSITION_AMBIGUOUS: IssueDefinition(
            _WARNING,
            "Box position for {name} cannot be measured from a single Row; Row1 is used and requires review.",
        ),
        IssueCode.TRACKER_LENGTH_OUTLIER: IssueDefinition(
            _WARNING,
            "Tracker {tracker_id} long side {measured:.0f}px deviates {deviation:.0%} from the expected {strings}-string length {expected_length:.0f}px in INV {inv_name}; review the detection.",
        ),
        IssueCode.NODE_HAS_MULTIPLE_BOXES: IssueDefinition(
            _WARNING, "Multiple Boxes were assigned to {name}; the nearest is used."
        ),
        IssueCode.TRACKER_DETECTION_FAILED: IssueDefinition(
            _ERROR, "{detail}", _EXECUTION
        ),
        IssueCode.BOX_DETECTION_FAILED: IssueDefinition(_ERROR, "{detail}", _EXECUTION),
        IssueCode.OVERLAPPING_NODE_BOXES: IssueDefinition(
            _ERROR,
            "Node box {node_id} overlaps {peers} above {threshold:.0%} IoU (maximum {max_iou:.1%}).",
        ),
        IssueCode.OVERLAPPING_TRACKER_BOXES: IssueDefinition(
            _ERROR,
            "Tracker boxes {pair_labels} overlap above {threshold:.0%} IoU (maximum {max_iou:.1%}); review {target} {target_id}.",
        ),
        IssueCode.NODE_STRING_TABLE_FAILED: IssueDefinition(
            _ERROR, "{detail}", _EXECUTION
        ),
        IssueCode.NODE_NAME_OCR_FAILED: IssueDefinition(_ERROR, "{detail}", _EXECUTION),
        IssueCode.NODE_NAME_REVIEW_FAILED: IssueDefinition(
            _ERROR, "{detail}", _EXECUTION
        ),
        IssueCode.DUPLICATE_NODE_NAME: IssueDefinition(
            _ERROR,
            "Final Node name '{name}' is used by multiple detected Nodes after review.",
        ),
        IssueCode.TRACKER_STRING_BALANCE_FAILED: IssueDefinition(
            _ERROR,
            "Tracker lengths cannot balance OCR string count {ocr_count} for {name}.",
        ),
        IssueCode.NODE_HAS_NO_TRACKERS: IssueDefinition(
            _ERROR, "No Tracker was assigned to {name}."
        ),
        IssueCode.NODE_HAS_NO_BOX: IssueDefinition(
            _ERROR, "No Box was assigned to {name}."
        ),
        IssueCode.MIXED_EQUIPMENT_FAMILY: IssueDefinition(
            _ERROR, "Array Layout pages disagree on CBX/LBD equipment family."
        ),
    }
)


def create_issue(
    code: IssueCode,
    *,
    page_number: int | None = None,
    entity_id: str = "",
    bbox: BoundingBox | None = None,
    **parameters: object,
) -> RecognitionIssue:
    """Build a catalogued issue; missing codes/format parameters fail loudly."""

    definition = ISSUE_DEFINITIONS[code]
    return RecognitionIssue(
        code=code.value,
        message=definition.template.format(**parameters),
        severity=definition.severity,
        page_number=page_number,
        entity_id=entity_id,
        bbox=bbox,
        params=dict(parameters),
    )


def format_issue_message(
    issue: RecognitionIssue,
    translate: Callable[[str, str], str],
) -> str:
    """Translate at display time; keep worker results and debug text stable.

    Unknown/legacy issues and incomplete translation templates retain their
    original message. Do not format the raw fallback: provider details may
    contain braces or JSON. Plain dictionaries remain process-pickleable.
    """

    prefix = "ai_statistics.element_recognition.issue"
    template = translate(f"{prefix}.{issue.code}", "")
    if not template:
        return issue.message
    parameters = dict(issue.params)
    if issue.code == IssueCode.OVERLAPPING_TRACKER_BOXES.value:
        target = parameters.get("target")
        if target == "unassigned Tracker":
            parameters["target"] = translate(
                f"{prefix}.value.unassigned_tracker",
                "unassigned Tracker",
            )
    if parameters.get("expected_text") == "<empty>":
        parameters["expected_text"] = translate(f"{prefix}.value.empty", "<empty>")
    try:
        return template.format(**parameters)
    except (KeyError, ValueError, IndexError, TypeError):
        return issue.message


def is_execution_issue(issue: RecognitionIssue) -> bool:
    """Unknown external/legacy codes are preserved rather than discarded."""

    try:
        definition = ISSUE_DEFINITIONS[IssueCode(issue.code)]
    except ValueError:
        return True
    return definition.lifetime == IssueLifetime.EXECUTION

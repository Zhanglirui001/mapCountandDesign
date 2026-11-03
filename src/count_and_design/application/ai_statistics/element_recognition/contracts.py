"""Stable contracts for Agent3 element recognition.

The core recognizer depends on these DTOs rather than importing Agent2's
implementation types.  The workflow is responsible for adapting reviewed
Agent1 images and Agent2 metadata into :class:`ElementRecognitionRequest`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Callable
from typing import Any

from ...ai.types import BoundingBox
from ..models import ReviewedTree
from .inv_name_matching import InvNameMatch


class PanelOrientation(Enum):
    """Panel orientation using the canonical values defined by Agent2."""

    UNKNOWN = "unknown"
    POSITIVE_NORTH = "P-N"
    POSITIVE_SOUTH = "P-S"
    POSITIVE_EAST = "P-E"
    POSITIVE_WEST = "P-W"
    TOWARD_BOX = "P-B"
    AWAY_FROM_BOX = "N-B"

    @classmethod
    def from_value(cls, value: object) -> "PanelOrientation":
        """Read canonical Agent2 codes and legacy Agent3 stored values."""

        normalized = str(value).strip()
        aliases = {
            "positive_north": cls.POSITIVE_NORTH,
            "positive_south": cls.POSITIVE_SOUTH,
            "positive_east": cls.POSITIVE_EAST,
            "positive_west": cls.POSITIVE_WEST,
            "toward_box": cls.TOWARD_BOX,
            "away_from_box": cls.AWAY_FROM_BOX,
        }
        if normalized.casefold() in aliases:
            return aliases[normalized.casefold()]
        for item in cls:
            if item.value.casefold() == normalized.casefold():
                return item
        raise ValueError(f"Unknown panel orientation: {value}")


class RecognitionIssueSeverity(Enum):
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class RecognitionIssue:
    code: str
    message: str
    severity: RecognitionIssueSeverity = RecognitionIssueSeverity.WARNING
    page_number: int | None = None
    entity_id: str = ""
    bbox: BoundingBox | None = None
    params: dict[str, object] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, int | None, str]:
        """Stable identity across wording, severity and geometry changes."""

        return (self.code, self.page_number, self.entity_id)


@dataclass(frozen=True)
class TrackerDefinition:
    """One tracker basis extracted by Agent2."""

    code: str
    strings_per_tracker: int
    total_count: int = 0
    panel_label: str = ""


@dataclass(frozen=True)
class InvRecognitionSettings:
    inv_name: str
    panel_orientation: PanelOrientation | None = None
    offset_snap_step_strings: float = 1.0


@dataclass(frozen=True)
class InvStringCountColumn:
    """One overall-site string-count column for a single INV/PCS/PCU."""

    label: str
    count: int | None = None
    column_index: int = 0


@dataclass(frozen=True)
class InvExpectedStringCount:
    """Per-INV string counts from Agent2, forwarded without changing later logic."""

    inv_name: str
    columns: tuple[InvStringCountColumn, ...] = ()

    @property
    def total(self) -> int | None:
        values = [item.count for item in self.columns if item.count is not None]
        return sum(values) if values else None


@dataclass(frozen=True)
class ArrayLayoutPageInput:
    page_number: int
    png_bytes: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class ElementRecognitionRequest:
    pages: tuple[ArrayLayoutPageInput, ...]
    tracker_definitions: tuple[TrackerDefinition, ...]
    # Authoritative names extracted by Agent2. Agent3 derives each page's INV
    # from its recognized Node names and uses this list only for background
    # fuzzy association and settings; neither Agent's display name is replaced.
    expected_inv_names: tuple[str, ...] = ()
    expected_inv_string_counts: tuple[InvExpectedStringCount, ...] = ()
    default_panel_orientation: PanelOrientation = PanelOrientation.UNKNOWN
    inv_settings: tuple[InvRecognitionSettings, ...] = ()
    project_name: str = ""
    default_offset_snap_step_strings: float = 1.0


@dataclass(frozen=True)
class ElementDetection:
    entity_id: str
    kind: str
    confidence: float
    bbox: BoundingBox
    page_number: int
    parent_id: str = ""
    label: str = ""
    source: str = ""
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeStringTableRow:
    """One user-reviewable Node/string-count row extracted from a page."""

    node: str
    string_number: int


@dataclass(frozen=True)
class ElementPageResult:
    page_number: int
    inv_name: str
    image_width: int
    image_height: int
    detections: tuple[ElementDetection, ...] = ()
    table_rows: tuple[NodeStringTableRow, ...] = ()
    issues: tuple[RecognitionIssue, ...] = ()
    table_bbox: BoundingBox | None = None
    table_bbox_source: str = ""
    table_bboxes: tuple[BoundingBox, ...] = ()


@dataclass(frozen=True)
class ElementRecognitionResult:
    reviewed_tree: ReviewedTree
    pages: tuple[ElementPageResult, ...] = ()
    issues: tuple[RecognitionIssue, ...] = ()
    equipment_family: str = ""
    recognition_version: str = "agent3-geometry-v1"
    inv_name_matches: tuple[InvNameMatch, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def has_output(self) -> bool:
        return any(inv.nodes for inv in self.reviewed_tree.invs)

    @property
    def all_issues(self) -> tuple[RecognitionIssue, ...]:
        """One read path for project/page issues, including legacy duplicates.

        New results store page issues only on pages. Prefer the page's current
        copy when reading older results that also copied it to the root.
        """

        unique = {issue.key: issue for issue in self.issues}
        for page in self.pages:
            unique.update((issue.key, issue) for issue in page.issues)
        return tuple(unique.values())

    def to_dict(self) -> dict[str, Any]:
        """Serialize the review result, including trace data but not images."""

        from ..validation.service import reviewed_tree_to_validation_payload

        return {
            "reviewed_tree": reviewed_tree_to_validation_payload(
                self.reviewed_tree
            ),
            "equipment_family": self.equipment_family,
            "recognition_version": self.recognition_version,
            "inv_name_matches": [
                {
                    "agent2_name": item.agent2_name,
                    "agent3_name": item.agent3_name,
                    "similarity": item.similarity,
                }
                for item in self.inv_name_matches
            ],
            "pages": [
                {
                    "page_number": page.page_number,
                    "inv_name": page.inv_name,
                    "image_width": page.image_width,
                    "image_height": page.image_height,
                    "detections": [
                        {
                            "entity_id": item.entity_id,
                            "kind": item.kind,
                            "confidence": item.confidence,
                            "bbox": _bbox_dict(item.bbox),
                            "parent_id": item.parent_id,
                            "label": item.label,
                            "source": item.source,
                            "issues": list(item.issues),
                        }
                        for item in page.detections
                    ],
                    "table_rows": [
                        {
                            "node": row.node,
                            "string_number": row.string_number,
                        }
                        for row in page.table_rows
                    ],
                    "table_bbox": (
                        _bbox_dict(page.table_bbox)
                        if page.table_bbox is not None
                        else None
                    ),
                    "table_bboxes": [
                        _bbox_dict(box) for box in page.table_bboxes
                    ],
                    "table_bbox_source": page.table_bbox_source,
                    "issues": [_issue_dict(issue) for issue in page.issues],
                }
                for page in self.pages
            ],
            "issues": [_issue_dict(issue) for issue in self.issues],
            "trace": [
                {
                    "inv": inv.name,
                    "node": node.name,
                    "recognition_id": node.recognition_id,
                    "annotations": [
                        {
                            "label": annotation.label,
                            "page_number": annotation.source.page_number,
                            "bbox": (
                                _bbox_dict(annotation.source.bbox)
                                if annotation.source.bbox is not None
                                else None
                            ),
                            "confidence": annotation.source.confidence,
                            "model_version": annotation.source.model_version,
                            "recognition_version": (
                                annotation.source.recognition_version
                            ),
                            "source": annotation.source.source,
                            "raw": annotation.source.raw,
                            "issues": list(annotation.issues),
                        }
                        for annotation in node.annotations
                    ],
                }
                for inv in self.reviewed_tree.invs
                for node in inv.nodes
            ],
            "raw": self.raw,
        }


def _bbox_dict(box: BoundingBox) -> dict[str, float]:
    return {"x1": box.x1, "y1": box.y1, "x2": box.x2, "y2": box.y2}


def _issue_dict(issue: RecognitionIssue) -> dict[str, Any]:
    return {
        "code": issue.code,
        "message": issue.message,
        "params": dict(issue.params),
        "severity": issue.severity.value,
        "page_number": issue.page_number,
        "entity_id": issue.entity_id,
        "bbox": _bbox_dict(issue.bbox) if issue.bbox is not None else None,
    }


AGENT3_PROGRESS_STEP_ORDER = (
    "rendering",
    "preparing",
    "table_recognition",
    "tracker_detection",
    "box_detection",
    "qwen_node_names",
    "claude_node_review",
    "geometry",
    "finalizing",
)


@dataclass(frozen=True)
class Agent3ProgressUpdate:
    """One observable Agent3 pipeline transition.

    ``status`` is one of ``running``, ``completed`` or ``failed``.  Page
    stages carry a page number; the final aggregation stage does not.
    ``worker_name`` identifies the worker so concurrent active tasks can be
    displayed independently.
    """

    step: str
    status: str
    page_number: int | None = None
    detail: str = ""
    worker_name: str = ""


Agent3ProgressCallback = Callable[[int, int], None]
Agent3StepCallback = Callable[[Agent3ProgressUpdate], None]


class Agent3Error(RuntimeError):
    """Raised when Agent3 cannot produce any usable draft."""


class Agent3Cancelled(Agent3Error):
    """Raised when cooperative cancellation is requested."""

"""Deterministic geometry helpers used by Agent3."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import floor
from statistics import mean, median

from ...ai.types import BoundingBox
from .contracts import PanelOrientation, TrackerDefinition


def center(box: BoundingBox) -> tuple[float, float]:
    return ((box.x1 + box.x2) / 2.0, (box.y1 + box.y2) / 2.0)


def contains_center(container: BoundingBox, item: BoundingBox) -> bool:
    x, y = center(item)
    return container.x1 <= x <= container.x2 and container.y1 <= y <= container.y2


def center_distance(left: BoundingBox, right: BoundingBox) -> float:
    lx, ly = center(left)
    rx, ry = center(right)
    return (lx - rx) ** 2 + (ly - ry) ** 2


def assign_parent(box: BoundingBox, parents: list[tuple[str, BoundingBox]]) -> str:
    matches = [(entity_id, parent) for entity_id, parent in parents if contains_center(parent, box)]
    if not matches:
        return ""
    return min(
        matches,
        key=lambda item: (
            center_distance(item[1], box),
            (item[1].x2 - item[1].x1) * (item[1].y2 - item[1].y1),
        ),
    )[0]


def center_to_edge_distance(left: BoundingBox, right: BoundingBox) -> float:
    """Return the squared distance from ``left``'s center to ``right``'s edge."""

    x, y = center(left)
    if right.x1 <= x <= right.x2 and right.y1 <= y <= right.y2:
        return min(
            (x - right.x1) ** 2,
            (right.x2 - x) ** 2,
            (y - right.y1) ** 2,
            (right.y2 - y) ** 2,
        )
    dx = max(right.x1 - x, 0.0, x - right.x2)
    dy = max(right.y1 - y, 0.0, y - right.y2)
    return dx * dx + dy * dy


def assign_boxes_to_nodes(
    boxes: list[BoundingBox],
    nodes: list[tuple[str, BoundingBox]],
) -> tuple[str, ...]:
    """Assign Boxes by containment, then by the nearest unused Node edge.

    Direct center-in-Node matches take precedence. Each Box whose center is
    outside every Node then claims the closest Node that has not already been
    paired with a Box. Boxes are processed in their supplied, deterministic
    detection order; surplus Boxes remain unassigned.
    """

    assignments = [assign_parent(box, nodes) for box in boxes]
    paired_nodes = {entity_id for entity_id in assignments if entity_id}
    for index, box in enumerate(boxes):
        if assignments[index]:
            continue
        candidates = [
            (entity_id, node_box, node_index)
            for node_index, (entity_id, node_box) in enumerate(nodes)
            if entity_id not in paired_nodes
        ]
        if not candidates:
            break
        entity_id, _node_box, _node_index = min(
            candidates,
            key=lambda item: (
                center_to_edge_distance(box, item[1]),
                item[2],
            ),
        )
        assignments[index] = entity_id
        paired_nodes.add(entity_id)
    return tuple(assignments)


def long_side(box: BoundingBox) -> float:
    return max(box.x2 - box.x1, box.y2 - box.y1)


def short_side(box: BoundingBox) -> float:
    return min(box.x2 - box.x1, box.y2 - box.y1)


@dataclass(frozen=True)
class TrackerClassification:
    definition_indexes: tuple[int, ...]
    counts: tuple[int, ...]
    score: float


def _feasible_counts(
    strings: tuple[int, ...],
    box_count: int,
    string_total: int,
) -> tuple[tuple[int, ...], ...]:
    """Return every per-class box count matching boxes and total strings."""

    if not strings:
        return ()
    solutions: list[tuple[int, ...]] = []

    def walk(
        index: int,
        remaining_boxes: int,
        remaining_strings: int,
        prefix: tuple[int, ...],
    ) -> None:
        strings_per_box = strings[index]
        if index == len(strings) - 1:
            if remaining_strings == remaining_boxes * strings_per_box:
                solutions.append(prefix + (remaining_boxes,))
            return
        for count in range(remaining_boxes + 1):
            leftover = remaining_strings - count * strings_per_box
            if leftover < 0:
                break
            walk(index + 1, remaining_boxes - count, leftover, prefix + (count,))

    walk(0, box_count, string_total, ())
    return tuple(solutions)


def _squared_deviation(values: list[float]) -> float:
    total = sum(values)
    return max(
        0.0,
        sum(value * value for value in values) - total * total / len(values),
    )


def classify_trackers(
    boxes: list[BoundingBox],
    ocr_string_count: int,
    definitions: tuple[TrackerDefinition, ...],
) -> TrackerClassification | None:
    """Classify tracker long sides and require an exact OCR string balance.

    Counts are resolved from the OCR total first, so a single unreliably
    short detection cannot change how many boxes each definition receives;
    the boxes are then partitioned in ascending length order.
    """

    if not boxes or ocr_string_count <= 0:
        return None
    definition_indexes_by_strings: dict[int, list[int]] = {}
    for index, definition in enumerate(definitions):
        if definition.strings_per_tracker > 0:
            definition_indexes_by_strings.setdefault(
                definition.strings_per_tracker, []
            ).append(index)
    if not definition_indexes_by_strings:
        return None

    string_counts = tuple(sorted(definition_indexes_by_strings))
    lengths = [long_side(box) for box in boxes]
    order = sorted(range(len(boxes)), key=lambda index: (lengths[index], index))
    total_variance = _squared_deviation(lengths)
    candidates: list[TrackerClassification] = []
    for counts in _feasible_counts(string_counts, len(boxes), ocr_string_count):
        assigned = [0] * len(boxes)
        cost = 0.0
        position = 0
        for strings, count in zip(string_counts, counts):
            block = order[position : position + count]
            position += count
            if count:
                cost += _squared_deviation([lengths[index] for index in block])
            definition_index = definition_indexes_by_strings[strings][0]
            for index in block:
                assigned[index] = definition_index
        candidates.append(
            TrackerClassification(
                tuple(assigned),
                tuple(assigned.count(index) for index in range(len(definitions))),
                cost / total_variance if total_variance > 0 else 0.0,
            )
        )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            len(set(item.definition_indexes)),
            item.score,
            tuple(
                definitions[index].strings_per_tracker
                for index in item.definition_indexes
            ),
        ),
    )


@dataclass(frozen=True)
class RowGroup:
    index: int
    tracker_indexes: tuple[int, ...]
    v_center: float


@dataclass(frozen=True)
class TrackerLengthSample:
    """One classified Tracker measured by the INV-wide length check."""

    entity_id: str
    bbox: BoundingBox
    strings: int


@dataclass(frozen=True)
class TrackerLengthOutlier:
    """A Tracker whose long side cannot belong to its string count."""

    sample: TrackerLengthSample
    expected_length: float


@dataclass(frozen=True)
class TrackerLengthCheck:
    one_string_length: float
    outliers: tuple[TrackerLengthOutlier, ...]


# Relative distance from ``strings * one_string_length`` that still passes.
# YOLO box jitter stays well below this, while a Tracker covering half of its
# classified strings (or a merged pair classified as a single string) clears
# it.
TRACKER_LENGTH_TOLERANCE = 0.3


def check_tracker_lengths(
    samples: Sequence[TrackerLengthSample],
    tolerance: float = TRACKER_LENGTH_TOLERANCE,
) -> TrackerLengthCheck:
    """Flag Trackers whose long side contradicts their string classification.

    Within one INV every classified Tracker must fit the same one-string
    length: an n-string Tracker is ``n`` times that length. The reference is
    the mean long side of the INV's single-string Trackers, because they
    carry no multiplication error; an INV without single-string Trackers
    falls back to the median ``long_side / strings`` of every classified
    Tracker so the check stays available for multi-string-only equipment.
    """

    classified = [sample for sample in samples if sample.strings > 0]
    if len(classified) < 2:
        return TrackerLengthCheck(0.0, ())
    single_lengths = [
        long_side(sample.bbox) for sample in classified if sample.strings == 1
    ]
    one_string_length = (
        mean(single_lengths)
        if single_lengths
        else median(long_side(sample.bbox) / sample.strings for sample in classified)
    )
    if one_string_length <= 0:
        return TrackerLengthCheck(0.0, ())
    outliers = tuple(
        TrackerLengthOutlier(sample, one_string_length * sample.strings)
        for sample in classified
        if abs(long_side(sample.bbox) - one_string_length * sample.strings)
        > tolerance * one_string_length * sample.strings
    )
    return TrackerLengthCheck(one_string_length, outliers)


def group_rows(boxes: list[BoundingBox]) -> tuple[str, tuple[RowGroup, ...], float]:
    """Group Trackers into Rows using their detected long/short axes.

    ``u_axis`` follows the Tracker long edge and the perpendicular coordinate
    orders Rows.  Consequently north-south Trackers produce west-to-east Rows,
    while east-west Trackers produce north-to-south Rows in absolute page
    coordinates (smallest y first).
    """

    if not boxes:
        return "x", (), 1.0
    horizontal = sum((box.x2 - box.x1) >= (box.y2 - box.y1) for box in boxes)
    u_axis = "x" if horizontal >= len(boxes) / 2 else "y"
    v_values = [center(box)[1 if u_axis == "x" else 0] for box in boxes]
    threshold = max(1.0, median(short_side(box) for box in boxes) * 0.5)
    ordered = sorted(range(len(boxes)), key=lambda index: (v_values[index], center(boxes[index])[0]))
    raw_groups: list[list[int]] = []
    for index in ordered:
        if not raw_groups:
            raw_groups.append([index])
            continue
        current_mean = sum(v_values[item] for item in raw_groups[-1]) / len(raw_groups[-1])
        if abs(v_values[index] - current_mean) <= threshold:
            raw_groups[-1].append(index)
        else:
            raw_groups.append([index])
    centers = [sum(v_values[item] for item in group) / len(group) for group in raw_groups]
    pitch = approximate_pitch(centers, threshold)
    groups = tuple(
        RowGroup(index=index + 1, tracker_indexes=tuple(group), v_center=centers[index])
        for index, group in enumerate(raw_groups)
    )
    return u_axis, groups, pitch


def approximate_pitch(centers: list[float], tolerance: float) -> float:
    gaps = [right - left for left, right in zip(centers, centers[1:]) if right > left]
    if not gaps:
        return 1.0
    smallest = min(gaps)
    candidates = [smallest / divisor for divisor in range(1, 9)]
    valid = []
    for candidate in candidates:
        residual = max(abs(gap / candidate - round(gap / candidate)) * candidate for gap in gaps)
        if residual <= tolerance:
            valid.append(candidate)
    return max(valid) if valid else median(gaps)


def axis_center(box: BoundingBox, axis: str) -> float:
    x, y = center(box)
    return x if axis == "x" else y


def interval_gap(left: BoundingBox, right: BoundingBox, axis: str) -> float:
    if axis == "x":
        a1, a2, b1, b2 = left.x1, left.x2, right.x1, right.x2
    else:
        a1, a2, b1, b2 = left.y1, left.y2, right.y1, right.y2
    if a2 < b1:
        return b1 - a2
    if b2 < a1:
        return a1 - b2
    return 0.0


def snap_positive(value: float, step: float) -> float:
    if step <= 0:
        raise ValueError("Snap step must be positive.")
    return floor(value / step + 0.5) * step


def format_length(coefficient: float, identifier: str) -> str:
    if coefficient <= 0 or not identifier:
        return "0mm"
    text = f"{coefficient:.6f}".rstrip("0").rstrip(".")
    return f"{text}{identifier}"


def polarity_for_side(
    orientation: PanelOrientation,
    *,
    side_delta_u: float,
    side_delta_y: float,
    side_delta_x: float | None = None,
) -> str:
    """Return the bracket polarity facing the combiner box for one side.

    The bracket sits between the Box and its panel string, so its polarity is
    the pole of the string at the Box end. With "positive toward north", a
    bracket north of the Box faces the panel's negative pole and is "N"; a
    bracket south of the Box faces its positive pole and is "P". "Positive
    toward south" mirrors that mapping. East/west orientations apply the same
    rule on the page x axis.
    """
    if orientation == PanelOrientation.TOWARD_BOX:
        return "P"
    if orientation == PanelOrientation.AWAY_FROM_BOX:
        return "N"
    if orientation in {
        PanelOrientation.POSITIVE_NORTH,
        PanelOrientation.POSITIVE_SOUTH,
    }:
        north_side = (
            side_delta_y < 0
            if abs(side_delta_y) > 1e-6
            else side_delta_u < 0
        )
    else:
        north_side = False
    if orientation == PanelOrientation.POSITIVE_NORTH:
        return "N" if north_side else "P"
    if orientation == PanelOrientation.POSITIVE_SOUTH:
        return "P" if north_side else "N"
    east_side = (
        side_delta_x > 0
        if side_delta_x is not None and abs(side_delta_x) > 1e-6
        else side_delta_u > 0
    )
    if orientation == PanelOrientation.POSITIVE_EAST:
        return "N" if east_side else "P"
    if orientation == PanelOrientation.POSITIVE_WEST:
        return "P" if east_side else "N"
    return ""

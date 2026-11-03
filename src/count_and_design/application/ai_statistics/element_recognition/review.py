"""Pure helpers for editing and locally recalculating an Agent3 draft."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace

from ..models import NodeDraft, RackingEntryDraft, TrackerQuantity
from .contracts import PanelOrientation, TrackerDefinition
from .geometry import format_length, snap_positive


_RACKING_WIDTH_PATTERN = re.compile(r"\d+")
_RACKING_POLARITY_PATTERN = re.compile(r"([PN])\s*$", re.IGNORECASE)


def summarize_node_rows(
    node: NodeDraft,
    *,
    tracker_definitions: Sequence[TrackerDefinition] = (),
) -> tuple[int, tuple[TrackerQuantity, ...]]:
    """Derive total strings and per-type Tracker counts from a Node's Rows.

    ``tracker_definitions`` names the project's canonical Tracker codes. A
    manually edited Row may introduce a width the recognized structure never
    had; mapping it through the definitions keeps the Agent2 spelling (for
    example ``3-STR``) instead of a synthetic ``3-string``, so Agent4 can
    compare the structure per Tracker type.
    """

    widths: Counter[int] = Counter()
    for row in node.rows:
        patterns = (
            tuple(entry.rackings or entry.string_pattern for entry in row.racking_entries)
            if row.racking_entries
            else row.rackings
        )
        for pattern in patterns:
            widths.update(
                width
                for token in _RACKING_WIDTH_PATTERN.findall(pattern)
                if (width := int(token)) > 0
            )

    type_by_width: dict[int, str] = {}
    ordered_widths: list[int] = []
    for definition in tracker_definitions:
        width = int(definition.strings_per_tracker)
        if width <= 0 or not definition.code or width in type_by_width:
            continue
        type_by_width[width] = definition.code
        ordered_widths.append(width)
    for quantity in node.tracker_counts:
        match = _RACKING_WIDTH_PATTERN.search(quantity.tracker_type)
        if match is None:
            continue
        width = int(match.group(0))
        if width not in type_by_width:
            type_by_width[width] = quantity.tracker_type
            ordered_widths.append(width)
    ordered_widths.extend(sorted(width for width in widths if width not in type_by_width))

    quantities = tuple(
        TrackerQuantity(
            tracker_type=type_by_width.get(width, f"{width}-string"),
            tracker_count=widths[width],
        )
        for width in ordered_widths
        if widths[width]
    )
    string_count = sum(width * count for width, count in widths.items())
    return string_count, quantities


def recalculate_node(
    node: NodeDraft,
    orientation: PanelOrientation,
    snap_step: float,
    *,
    inherit_orientation: bool = False,
    inherit_snap_step: bool = False,
    tracker_definitions: Sequence[TrackerDefinition] = (),
) -> NodeDraft:
    """Rebuild derived P/N and offsets without rerunning providers."""

    if snap_step <= 0:
        raise ValueError("Offset snap step must be positive.")
    rows = []
    for row in node.rows:
        entries = tuple(
            _recalculate_entry(entry, orientation, snap_step)
            for entry in row.racking_entries
        )
        rows.append(
            replace(
                row,
                racking_entries=entries,
                rackings=tuple(entry.rackings for entry in entries),
            )
        )
    updated = replace(
        node,
        rows=tuple(rows),
        panel_orientation=orientation.value,
        offset_snap_step_strings=snap_step,
        inherits_panel_orientation=inherit_orientation,
        inherits_offset_snap_step=inherit_snap_step,
    )
    # Only the row-derived Tracker totals change; ``ocr_string_count`` records
    # the table-recognition value that Agent4 validates against.
    _, tracker_counts = summarize_node_rows(
        updated,
        tracker_definitions=tracker_definitions,
    )
    return replace(updated, tracker_counts=tracker_counts)


def _recalculate_entry(
    entry: RackingEntryDraft,
    orientation: PanelOrientation,
    snap_step: float,
) -> RackingEntryDraft:
    pattern = entry.string_pattern
    north_side = entry.north_side
    east_side = entry.east_side
    typed_pattern, typed_polarity = _split_rackings(entry.rackings)
    if typed_pattern and typed_pattern != pattern:
        # A manual edit rewrites only ``rackings``. Adopting the typed widths
        # here keeps a later recalculation (Node/INV/project Apply) from
        # restoring the recognized pattern and erasing the corrected string
        # total Agent4 derives from it.
        pattern = typed_pattern
        sides = _sides_for_polarity(orientation, typed_polarity)
        if sides is not None:
            north_side, east_side = sides
    if not pattern:
        return entry
    polarity = _polarity_for_sides(orientation, north_side, east_side)
    length_str = entry.length_str
    if entry.offset_ratio_strings is not None:
        length_str = format_length(
            snap_positive(entry.offset_ratio_strings, snap_step),
            entry.length_identifier,
        )
    return replace(
        entry,
        rackings=f"{pattern} {polarity}".strip(),
        string_pattern=pattern,
        north_side=north_side,
        east_side=east_side,
        length_str=length_str,
    )


def _split_rackings(text: str) -> tuple[str, str]:
    """Split a racking string into its width pattern and P/N polarity.

    The recognized pattern (``string_pattern``) and the displayed racking
    text (``rackings``) are normally two views of the same value. A user may
    type the widths directly into the racking editor, so the typed text has
    to be parseable back into the structured fields; otherwise a later
    recalculation would silently restore the recognized widths.
    """

    value = text or ""
    pattern = "+".join(_RACKING_WIDTH_PATTERN.findall(value))
    match = _RACKING_POLARITY_PATTERN.search(value)
    return pattern, match.group(1).upper() if match else ""


def _polarity_for_sides(
    orientation: PanelOrientation,
    north_side: bool | None,
    east_side: bool | None,
) -> str:
    """Return the racking polarity one side faces under the orientation."""

    if orientation == PanelOrientation.TOWARD_BOX:
        return "P"
    if orientation == PanelOrientation.AWAY_FROM_BOX:
        return "N"
    if (
        orientation
        in {
            PanelOrientation.POSITIVE_EAST,
            PanelOrientation.POSITIVE_WEST,
        }
        and east_side is not None
    ):
        positive_east = orientation == PanelOrientation.POSITIVE_EAST
        if positive_east:
            return "N" if east_side else "P"
        return "P" if east_side else "N"
    if north_side is not None:
        positive_north = orientation == PanelOrientation.POSITIVE_NORTH
        positive_south = orientation == PanelOrientation.POSITIVE_SOUTH
        if positive_north:
            return "N" if north_side else "P"
        if positive_south:
            return "P" if north_side else "N"
    return ""


def _sides_for_polarity(
    orientation: PanelOrientation,
    polarity: str,
) -> tuple[bool | None, bool | None] | None:
    """Return side flags that reproduce *polarity* under *orientation*.

    ``None`` means the orientation decides the polarity itself (positive
    toward/away from the box, or unknown direction), so the recognized side
    flags stay untouched.
    """

    if polarity not in {"P", "N"}:
        return None
    if orientation == PanelOrientation.POSITIVE_NORTH:
        return (polarity == "N", None)
    if orientation == PanelOrientation.POSITIVE_SOUTH:
        return (polarity == "P", None)
    if orientation == PanelOrientation.POSITIVE_EAST:
        return (None, polarity == "N")
    if orientation == PanelOrientation.POSITIVE_WEST:
        return (None, polarity == "P")
    return None

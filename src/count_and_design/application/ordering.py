"""Shared ordering helpers for user-managed project collections."""

from __future__ import annotations

from collections.abc import Callable, Iterable, MutableSequence
import re
from typing import TypeVar


T = TypeVar("T")
K = TypeVar("K")

_NATURAL_PART_RE = re.compile(r"(\d+)")


def natural_text_key(value: object) -> tuple[object, ...]:
    """Return a case-insensitive key whose digit runs compare numerically."""

    text = str(value).strip().casefold()
    return tuple(
        int(part) if part.isdigit() else part
        for part in _NATURAL_PART_RE.split(text)
    )


def optional_natural_text_key(value: object) -> tuple[object, ...] | None:
    """Return a natural key, treating blank text as a missing value."""

    return natural_text_key(value) if str(value).strip() else None


def move_selected_items(
    collection: MutableSequence[T],
    selected: Iterable[T],
    target: T | None,
    *,
    after: bool = False,
) -> bool:
    """Move selected objects as one stable block before/after *target*.

    Identity, rather than equality, is used throughout because project model
    dataclasses may compare equal while still representing distinct rows.
    ``target=None`` means the end of the collection.
    """

    selected_ids = {id(item) for item in selected}
    if not selected_ids:
        return False
    indexed_selected = [
        (index, item)
        for index, item in enumerate(collection)
        if id(item) in selected_ids
    ]
    if not indexed_selected:
        return False
    if target is not None and id(target) in selected_ids:
        return False

    if target is None:
        boundary = len(collection)
    else:
        target_index = next(
            (index for index, item in enumerate(collection) if item is target),
            None,
        )
        if target_index is None:
            return False
        boundary = target_index + (1 if after else 0)

    removed_before_boundary = sum(
        1 for index, _item in indexed_selected if index < boundary
    )
    insertion_index = boundary - removed_before_boundary
    moving = [item for _index, item in indexed_selected]
    remaining = [item for item in collection if id(item) not in selected_ids]
    reordered = remaining[:insertion_index] + moving + remaining[insertion_index:]
    if all(left is right for left, right in zip(collection, reordered)):
        return False
    collection[:] = reordered
    return True


def sort_selected_slots(
    collection: MutableSequence[T],
    selected: Iterable[T],
    key: Callable[[T], K | None],
    *,
    descending: bool = False,
) -> bool:
    """Sort selected objects back into their existing slots.

    ``None`` keys are always placed after non-missing values, independently
    of direction. Python's stable sort preserves the order of equal keys.
    """

    selected_ids = {id(item) for item in selected}
    positions = [
        index for index, item in enumerate(collection) if id(item) in selected_ids
    ]
    if len(positions) < 2:
        return False
    current = [collection[index] for index in positions]
    present = [item for item in current if key(item) is not None]
    missing = [item for item in current if key(item) is None]
    ordered = sorted(present, key=key, reverse=descending) + missing
    if all(left is right for left, right in zip(current, ordered)):
        return False
    for index, item in zip(positions, ordered):
        collection[index] = item
    return True

"""Fuzzy, display-safe matching between Agent2 and Agent3 INV names."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass(frozen=True)
class InvNameMatch:
    """One background association without changing either displayed name."""

    agent2_name: str
    agent3_name: str
    similarity: float


def inv_name_similarity(left: str, right: str) -> float:
    """Return generic character similarity without assuming a naming scheme."""

    left_key = _comparison_key(left)
    right_key = _comparison_key(right)
    if not left_key or not right_key:
        return 0.0
    return SequenceMatcher(None, left_key, right_key, autojunk=False).ratio()


def match_inv_names(
    agent2_names: Iterable[str],
    agent3_names: Iterable[str],
) -> tuple[InvNameMatch, ...]:
    """Pair unique names one-to-one, taking highest similarities first.

    Returned names retain their exact input spellings, so the association can
    be used internally without canonicalizing either Agent's displayed text.
    """

    left = _unique_names(agent2_names)
    right = _unique_names(agent3_names)
    ranked = sorted(
        (
            (inv_name_similarity(agent2, agent3), left_index, right_index)
            for left_index, agent2 in enumerate(left)
            for right_index, agent3 in enumerate(right)
        ),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    used_left: set[int] = set()
    used_right: set[int] = set()
    matches: list[InvNameMatch] = []
    for similarity, left_index, right_index in ranked:
        if similarity <= 0.0:
            break
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        matches.append(
            InvNameMatch(
                agent2_name=left[left_index],
                agent3_name=right[right_index],
                similarity=similarity,
            )
        )
        if len(matches) == min(len(left), len(right)):
            break
    return tuple(matches)


def best_agent2_inv_match(
    agent3_name: str,
    agent2_names: Iterable[str],
) -> InvNameMatch | None:
    """Return the most similar Agent2 name for one Agent3 display name."""

    matches = match_inv_names(agent2_names, (agent3_name,))
    return matches[0] if matches else None


def _comparison_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _unique_names(values: Iterable[str]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = str(value).strip()
        key = _comparison_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return tuple(names)

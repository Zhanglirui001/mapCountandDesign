from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from .length import Length


@dataclass
class RackingEntry:
    """A rackings name paired with its offset, forming one entry in a Row's racking list."""

    rackings: str = ""
    offset: Length = field(default_factory=Length)

    def to_dict(self) -> dict[str, object]:
        return {
            "rackings": self.rackings,
            "offset": self.offset.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, object], identifiers: list[object] | None = None
    ) -> "RackingEntry":
        r = data.get("rackings", "")
        if isinstance(r, dict):
            r = str(r.get("name", ""))
        else:
            r = str(r) if r else ""
        return cls(
            rackings=r,
            offset=Length.from_dict(data.get("offset"), identifiers),  # type: ignore[arg-type]
        )

    def clone(self) -> "RackingEntry":
        return copy.deepcopy(self)


@dataclass
class Row:
    name: str = ""
    racking_entries: list[RackingEntry] = field(
        default_factory=lambda: [RackingEntry()]
    )

    # ------------------------------------------------------------------
    # serialization (backward-compatible with old NW/SE format)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "racking_entries": [e.to_dict() for e in self.racking_entries],
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, object], identifiers: list[object] | None = None
    ) -> "Row":
        entries_data = data.get("racking_entries")
        if entries_data is not None and isinstance(entries_data, list):
            # New format: list of entries
            entries = [
                RackingEntry.from_dict(item, identifiers)  # type: ignore[arg-type]
                for item in entries_data
            ]
        else:
            # Migrate old format where rackings were separate NW/SE fields
            entries = _migrate_old_format(data, identifiers)

        return cls(
            name=str(data.get("name", "")),
            racking_entries=entries if entries else [RackingEntry()],
        )

    @staticmethod
    def sequence_name(previous_name: str | None) -> str:
        if not previous_name:
            return "Row1"
        match = re.search(r"(\d+)$", previous_name)
        if not match:
            return "Row1"
        num_str = match.group(1)
        prefix = previous_name[: match.start()]
        new_num = int(num_str) + 1
        new_num_str = str(new_num).zfill(len(num_str))
        return prefix + new_num_str

    @classmethod
    def new(cls, previous_name: str | None = None) -> "Row":
        return cls(name=cls.sequence_name(previous_name))

    def clone(self) -> "Row":
        return copy.deepcopy(self)


def _migrate_old_format(
    data: dict[str, object], identifiers: list[object] | None
) -> list[RackingEntry]:
    """Convert old rackings_NW / rackings_SE fields into a list of RackingEntry."""

    def _parse_name(raw: object) -> str:
        if isinstance(raw, dict):
            return str(raw.get("name", ""))
        return str(raw) if raw else ""

    entries: list[RackingEntry] = []

    nw_raw = data.get("rackings_NW", "")
    nw_name = _parse_name(nw_raw) if nw_raw else ""
    se_raw = data.get("rackings_SE", "")
    se_name = _parse_name(se_raw) if se_raw else ""

    if nw_name:
        entries.append(
            RackingEntry(
                rackings=nw_name,
                offset=Length.from_dict(data.get("offset_NW"), identifiers),  # type: ignore[arg-type]
            )
        )
    if se_name:
        entries.append(
            RackingEntry(
                rackings=se_name,
                offset=Length.from_dict(data.get("offset_SE"), identifiers),  # type: ignore[arg-type]
            )
        )

    if not entries:
        entries.append(RackingEntry())

    return entries

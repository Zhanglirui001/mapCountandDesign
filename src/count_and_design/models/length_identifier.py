from __future__ import annotations

from dataclasses import dataclass
import re

from .length import Length, LengthError


@dataclass
class LengthIdentifier:
    name: str
    length: Length
    label: str = ""

    _RESERVED_UNITS = {"mm", "ft", "inch", "in", "m"}

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-zA-Z_]+", self.name):
            raise LengthError("identifier_invalid_name")
        if self.name.lower() in self._RESERVED_UNITS:
            raise LengthError("identifier_reserved_unit", name=self.name)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "length": self.length.to_dict(),
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "LengthIdentifier":
        return cls(
            name=str(data["name"]),
            length=Length.from_dict(data.get("length")),  # type: ignore[arg-type]
            label=str(data.get("label", "")),
        )

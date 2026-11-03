"""Part model — represents a single part / semi-part in a generated product."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Part:
    """部件 / 半成品"""

    code: str = ""
    category: str = ""
    cad_parts_no: str = ""
    cad_parts_length: float = 0.0
    description: str = ""
    unit: str = ""
    overmolding_detail: str = ""
    label_detail: str = ""
    direction_nos: list[int] = field(default_factory=list)
    length: float = 0.0
    semi_or_not: bool = False
    semi_list: list[Part] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "category": self.category,
            "cad_parts_no": self.cad_parts_no,
            "cad_parts_length": self.cad_parts_length,
            "description": self.description,
            "unit": self.unit,
            "overmolding_detail": self.overmolding_detail,
            "label_detail": self.label_detail,
            "direction_nos": self.direction_nos,
            "length": self.length,
            "semi_or_not": self.semi_or_not,
            "semi_list": [p.to_dict() for p in self.semi_list],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Part:
        raw = data.get("direction_nos", [])
        direction_nos: list[int] = list(raw) if isinstance(raw, (list, tuple)) else []
        return cls(
            code=str(data.get("code", "")),
            category=str(data.get("category", "")),
            cad_parts_no=str(data.get("cad_parts_no", "")),
            cad_parts_length=float(data.get("cad_parts_length", 0.0)),
            description=str(data.get("description", "")),
            unit=str(data.get("unit", "")),
            overmolding_detail=str(data.get("overmolding_detail", "")),
            label_detail=str(data.get("label_detail", "")),
            direction_nos=direction_nos,
            length=float(data.get("length", 0.0)),
            semi_or_not=bool(data.get("semi_or_not", False)),
            semi_list=[Part.from_dict(p) for p in data.get("semi_list", [])],  # type: ignore[arg-type]
        )

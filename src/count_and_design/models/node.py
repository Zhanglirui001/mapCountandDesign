from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from .length import Length
from .row import Row
from .wire import Wire



@dataclass
class Node:
    name: str
    racking_pitch: Length | None = None
    cluster_wire: Wire | None = None
    lynx_preset: str | None = None
    cluster_length: Length | None = None
    box_position: float = 0.0
    rows: list[Row] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Box Position is a non-negative layout offset and cannot represent
        # additional cable length.  Keep the model invariant in one place so
        # project loading and version-history checkout cannot reintroduce
        # legacy negative values.
        if self.box_position < 0:
            self.box_position = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "racking_pitch": self.racking_pitch.to_dict() if self.racking_pitch else None,
            "cluster_wire": self.cluster_wire.to_dict() if self.cluster_wire else None,
            "lynx_preset": self.lynx_preset,
            "cluster_length": self.cluster_length.to_dict() if self.cluster_length else None,
            "box_position": self.box_position,
            "rows": [row.to_dict() for row in self.rows],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object], identifiers: list[object] | None = None) -> "Node":
        return cls(
            name=str(data["name"]),
            racking_pitch=Length.from_dict(data.get("racking_pitch"), identifiers),  # type: ignore[arg-type]
            cluster_wire=Wire.from_dict(data.get("cluster_wire")),  # type: ignore[arg-type]
            lynx_preset=str(data["lynx_preset"]) if data.get("lynx_preset") else None,
            cluster_length=Length.from_dict(data.get("cluster_length"), identifiers),  # type: ignore[arg-type]
            box_position=float(data.get("box_position", 0.0)),
            rows=[
                Row.from_dict(item, identifiers)  # type: ignore[arg-type]
                for item in data.get("rows", [])
            ],
        )

    @staticmethod
    def sequence_name(previous_name: str | None) -> str:
        if not previous_name:
            return "Node1"
        match = re.search(r"(\d+)$", previous_name)
        if not match:
            return "Node1"
        num_str = match.group(1)
        prefix = previous_name[: match.start()]
        new_num = int(num_str) + 1
        new_num_str = str(new_num).zfill(len(num_str))
        return prefix + new_num_str

    @classmethod
    def new(cls, previous_name: str | None = None) -> "Node":
        return cls(name=cls.sequence_name(previous_name))

    def clone(self) -> "Node":
        return copy.deepcopy(self)

    def swap(self, index1: int, index2: int) -> None:
        self.rows[index1], self.rows[index2] = self.rows[index2], self.rows[index1]

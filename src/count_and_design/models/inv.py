from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from .length import Length
from .node import Node
from .wire import Wire


@dataclass
class Inv:
    name: str
    racking_pitch: Length | None = None
    cluster_wire: Wire | None = None
    lynx_preset: str | None = None
    nodes: list[Node] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "racking_pitch": self.racking_pitch.to_dict() if self.racking_pitch else None,
            "cluster_wire": self.cluster_wire.to_dict() if self.cluster_wire else None,
            "lynx_preset": self.lynx_preset,
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object], identifiers: list[object] | None = None) -> "Inv":
        return cls(
            name=str(data["name"]),
            racking_pitch=Length.from_dict(data.get("racking_pitch"), identifiers),  # type: ignore[arg-type]
            cluster_wire=Wire.from_dict(data.get("cluster_wire")),  # type: ignore[arg-type]
            lynx_preset=str(data["lynx_preset"]) if data.get("lynx_preset") else None,
            nodes=[
                Node.from_dict(item, identifiers)  # type: ignore[arg-type]
                for item in data.get("nodes", [])
            ],
        )

    @staticmethod
    def sequence_name(previous_name: str | None) -> str:
        if not previous_name:
            return "INV1"
        match = re.search(r"(\d+)$", previous_name)
        if not match:
            return "INV1"
        num_str = match.group(1)
        prefix = previous_name[: match.start()]
        new_num = int(num_str) + 1
        new_num_str = str(new_num).zfill(len(num_str))
        return prefix + new_num_str

    @classmethod
    def new(cls, previous_name: str | None = None) -> "Inv":
        return cls(name=cls.sequence_name(previous_name))

    def clone(self) -> "Inv":
        return copy.deepcopy(self)

    def swap(self, index1: int, index2: int) -> None:
        self.nodes[index1], self.nodes[index2] = self.nodes[index2], self.nodes[index1]

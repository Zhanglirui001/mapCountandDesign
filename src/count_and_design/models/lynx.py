from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .length import Length
from .wire import Wire

if TYPE_CHECKING:
    pass


@dataclass
class LYNXBranch:
    """LYNX 支线，表示注塑件中的一条支线。"""

    location: int
    cu_wire: Wire
    al_wire: Wire
    cu_length: Length = field(default_factory=lambda: Length("11ft"))
    al_length: Length = field(default_factory=lambda: Length("1ft"))

    def __post_init__(self) -> None:
        if not isinstance(self.cu_length, Length):
            self.cu_length = Length(str(self.cu_length))
        if not isinstance(self.al_length, Length):
            self.al_length = Length(str(self.al_length))

    def to_dict(self) -> dict[str, object]:
        return {
            "location": self.location,
            "cu_wire": self.cu_wire.to_dict(),
            "al_wire": self.al_wire.to_dict(),
            "cu_length": self.cu_length.to_dict(),
            "al_length": self.al_length.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, object],
        identifiers: list | None = None,
    ) -> LYNXBranch:
        return cls(
            location=int(data["location"]),
            cu_wire=Wire.from_dict(data["cu_wire"]),  # type: ignore[arg-type]
            al_wire=Wire.from_dict(data["al_wire"]),  # type: ignore[arg-type]
            cu_length=Length.from_dict(
                data.get("cu_length"), identifiers  # type: ignore[arg-type]
            ) or Length("11ft"),
            al_length=Length.from_dict(
                data.get("al_length"), identifiers  # type: ignore[arg-type]
            ) or Length("1ft"),
        )


@dataclass
class Slot:
    """LYNX 主线上的虚拟槽位。"""

    position: int
    overmolds: list[Overmold] = field(default_factory=list)


@dataclass
class Overmold:
    """LYNX 注塑件，包含多条 LYNX 支线和一个前置长度。"""

    branch_lines: list[LYNXBranch] = field(default_factory=list)
    prefix_length: Length = field(default_factory=lambda: Length("0mm"))
    responsible_rows: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.prefix_length, Length):
            self.prefix_length = Length(str(self.prefix_length))

    def to_dict(self) -> dict[str, object]:
        return {
            "branch_lines": [b.to_dict() for b in self.branch_lines],
            "prefix_length": self.prefix_length.to_dict(),
            "responsible_rows": list(self.responsible_rows),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, object],
        identifiers: list | None = None,
    ) -> Overmold:
        return cls(
            branch_lines=[
                LYNXBranch.from_dict(b, identifiers)  # type: ignore[arg-type]
                for b in data.get("branch_lines", [])
            ],
            prefix_length=Length.from_dict(
                data.get("prefix_length"), identifiers  # type: ignore[arg-type]
            ) or Length("0mm"),
            responsible_rows=[
                int(r) for r in data.get("responsible_rows", [])
            ],
        )


@dataclass
class LYNXProduct:
    """LYNX 产品，包含产品代号、主线线号和注塑件列表。"""

    product_code: str
    mainline_wire: Wire
    overmolds: list[Overmold] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "product_code": self.product_code,
            "mainline_wire": self.mainline_wire.to_dict(),
            "overmolds": [o.to_dict() for o in self.overmolds],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, object],
        identifiers: list | None = None,
    ) -> LYNXProduct:
        return cls(
            product_code=str(data["product_code"]),
            mainline_wire=Wire.from_dict(data["mainline_wire"]),  # type: ignore[arg-type]
            overmolds=[
                Overmold.from_dict(o, identifiers)  # type: ignore[arg-type]
                for o in data.get("overmolds", [])
            ],
        )

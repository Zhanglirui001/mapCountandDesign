from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PVPanelInfo:
    """Electrical inputs for one photovoltaic module/string."""

    vmp: float = 0.0
    imp: float = 0.0
    module_per_string: int = 0
    name: str = ""

    @property
    def system_voltage(self) -> float:
        if self.vmp <= 0 or self.module_per_string <= 0:
            return 0.0
        return self.vmp * self.module_per_string

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "vmp": self.vmp,
            "imp": self.imp,
            "module_per_string": self.module_per_string,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> "PVPanelInfo":
        if not data:
            return cls()
        return cls(
            vmp=float(data.get("vmp", 0.0)),
            imp=float(data.get("imp", 0.0)),
            module_per_string=int(data.get("module_per_string", 0)),
            name=str(data.get("name", "")),
        )

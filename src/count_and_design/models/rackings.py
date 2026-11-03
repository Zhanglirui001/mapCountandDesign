from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass
class Rackings:
    name: str
    product_presets: str = ""
    gap_extenders: list[tuple[str, int]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.gap_extenders is None:
            self.gap_extenders = []
        if self.product_presets and not re.fullmatch(r"[A-Z]+", self.product_presets):
            raise ValueError("Rackings product presets must be uppercase letters.")

    def product_preset_names(self) -> list[str]:
        return list(self.product_presets)

    def gap_extender_names(self) -> list[str]:
        return [name for name, _qty in self.gap_extenders]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "product_presets": self.product_presets,
            "gap_extenders": [{"name": name, "quantity": qty} for name, qty in self.gap_extenders],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> Rackings | None:
        if data is None:
            return None
        extenders_raw = data.get("gap_extenders", data.get("extenders", []))
        extenders: list[tuple[str, int]] = []
        if isinstance(extenders_raw, list):
            for item in extenders_raw:
                if isinstance(item, dict):
                    extenders.append((str(item.get("name", "")), int(item.get("quantity", 0))))
                elif isinstance(item, list) and len(item) == 2:
                    extenders.append((str(item[0]), int(item[1])))
        return cls(
            name=str(data["name"]),
            product_presets=str(data.get("product_presets", "")),
            gap_extenders=extenders,
        )

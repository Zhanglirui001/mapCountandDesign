from __future__ import annotations

from dataclasses import dataclass


WIRE_SIZE_MAP = {
    "14AWG": "2.5mm2",
    "12AWG": "4mm2",
    "10AWG": "6mm2",
    "8AWG": "10mm2",
    "6AWG": "16mm2",
    "4AWG": "25mm2",
    "2AWG": "35mm2",
    "1AWG": "42mm2",
    "1/0AWG": "50mm2",
    "2/0AWG": "70mm2",
    "3/0AWG": "95mm2",
    "4/0AWG": "120mm2",
    "300MCM": "150mm2",
    "350MCM": "185mm2",
    "400MCM": "200mm2",
    "500MCM": "240mm2",
    "600MCM": "300mm2",
    "750MCM": "400mm2",
    "900MCM": "500mm2",
    "1000MCM": "630mm2",
}

REVERSE_WIRE_SIZE_MAP = {metric: awg for awg, metric in WIRE_SIZE_MAP.items()}
VALID_MATERIALS = {"Cu", "Al"}
VALID_STRANDS = {"none", "7", "48", "63", "66", "75", "105", "106", "161", "210", "266"}

# Default strands values by wire size for copper wires.
# Only applies when a concrete wire size is selected.
CU_STRANDS_DEFAULTS: dict[str, str] = {
    "12AWG": "48",
    "8AWG": "66",
    "10AWG": "75",
    "6AWG": "7",
}

# Default strands values by wire size for aluminum wires.
# Only applies when a concrete wire size is selected.
AL_STRANDS_DEFAULTS: dict[str, str] = {
    "6AWG": "63",
    "4AWG": "105",
    "2AWG": "161",
    "1AWG": "210",
    "1/0AWG": "266",
}

# Fallback strands value for aluminum wires when no size-specific default exists.
AL_STRANDS_DEFAULT: str = "7"


@dataclass
class Wire:
    wiresize: str
    material: str
    strands: str = "none"

    def __post_init__(self) -> None:
        self.wiresize = self._normalize_size(self.wiresize)
        if self.material not in VALID_MATERIALS:
            raise ValueError(f"Unsupported wire material: {self.material}")
        if self.strands not in VALID_STRANDS:
            raise ValueError(
                f"Unsupported strands value: {self.strands}. "
                f"Must be one of {sorted(VALID_STRANDS, key=lambda s: (s == 'none', s))}"
            )


    @staticmethod
    def _normalize_size(value: str) -> str:
        normalized = value.strip().lower()
        if normalized == "none":
            return "none"
        # Build case-insensitive lookup maps
        lower_wire_map = {k.lower(): k for k in WIRE_SIZE_MAP}
        lower_reverse_map = {k.lower(): k for k in REVERSE_WIRE_SIZE_MAP}
        if normalized in lower_wire_map:
            return lower_wire_map[normalized]
        if normalized in lower_reverse_map:
            return lower_reverse_map[normalized]
        raise ValueError(f"Unsupported wire size: {value}")

    def to_metric(self) -> str:
        if self.wiresize == "none":
            return "none"
        if self.wiresize in REVERSE_WIRE_SIZE_MAP:
            return self.wiresize
        return WIRE_SIZE_MAP[self.wiresize]

    def to_awg(self) -> str:
        if self.wiresize == "none":
            return "none"
        if self.wiresize in WIRE_SIZE_MAP:
            return self.wiresize
        return REVERSE_WIRE_SIZE_MAP[self.wiresize]

    def to_dict(self) -> dict[str, str]:
        return {
            "wiresize": self.wiresize,
            "material": self.material,
            "strands": self.strands,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> Wire | None:
        if data is None:
            return None
        return cls(
            wiresize=str(data["wiresize"]),
            material=str(data["material"]),
            strands=str(data["strands"]),
        )

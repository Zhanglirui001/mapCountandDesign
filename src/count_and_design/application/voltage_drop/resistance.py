from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re


_DATA_PATH = Path(__file__).with_name("data") / "resistance.json"


def normalize_wire_size(value: str) -> str:
    normalized = re.sub(r"\s+", "", value).upper().replace("KCMIL", "MCM")
    return normalized


@lru_cache(maxsize=1)
def load_resistance_table() -> dict[tuple[str, str], float]:
    data = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported resistance table schema version.")
    if data.get("unit") != "ohm_per_kft":
        raise ValueError("Resistance table unit must be ohm_per_kft.")

    result: dict[tuple[str, str], float] = {}
    for raw in data.get("entries", []):
        size = normalize_wire_size(str(raw["wire_size"]))
        material = str(raw["material"])
        value = float(raw["ohms_per_kft"])
        if material not in {"Cu", "Al"}:
            raise ValueError(f"Unsupported resistance material: {material}")
        if value <= 0:
            raise ValueError(f"Resistance must be positive for {size} {material}.")
        key = (size, material)
        if key in result:
            raise ValueError(f"Duplicate resistance entry: {size} {material}")
        result[key] = value
    return result


def get_resistance_ohms_per_kft(wire_size: str, material: str) -> float | None:
    return load_resistance_table().get((normalize_wire_size(wire_size), material))

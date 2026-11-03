from __future__ import annotations

import re
from dataclasses import dataclass


VALID_FUSE_CURRENTS = {
    "4A", "5A", "10A", "15A", "20A", "25A", "30A", "32A",
    "35A", "40A", "45A", "50A", "55A", "60A", "65A", "80A", "none",
}

# 电流 → 制造商绑定表（唯一供应商为 Adler，程序内部自动设置，用户不可选）
FUSE_MANUFACTURER_MAP: dict[str, str] = {
    c: "Adler" if c != "none" else "none"
    for c in VALID_FUSE_CURRENTS
}

VALID_FUSE_MANUFACTURERS = set(FUSE_MANUFACTURER_MAP.values())


def _fuse_current_sort_key(current: str) -> tuple[int, str]:
    """按电流数值排序的键函数。

    解析 "4A" → (4, "A")，"none" 排在最后。
    """
    if current == "none":
        return (9999, "")
    match = re.match(r"(\d+)(\D*)", current)
    if match:
        return (int(match.group(1)), match.group(2))
    return (0, current)


def sorted_fuse_currents() -> list[str]:
    """返回按电流数值排序的电流列表。"""
    return sorted(VALID_FUSE_CURRENTS, key=_fuse_current_sort_key)


@dataclass
class Fuse:
    current: str
    manufacturer: str

    def __post_init__(self) -> None:
        if self.current not in VALID_FUSE_CURRENTS:
            raise ValueError(
                f"Invalid fuse current: {self.current}. "
                f"Must be one of {sorted_fuse_currents()}"
            )
        # 根据电流自动设置制造商（忽略传入的 manufacturer 值）
        expected_mfr = FUSE_MANUFACTURER_MAP.get(self.current, "none")
        if self.manufacturer != expected_mfr:
            self.manufacturer = expected_mfr

    def to_dict(self) -> dict[str, str]:
        return {
            "current": self.current,
            "manufacturer": self.manufacturer,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> Fuse | None:
        if data is None:
            return None
        return cls(
            current=str(data["current"]),
            manufacturer=str(data["manufacturer"]),
        )

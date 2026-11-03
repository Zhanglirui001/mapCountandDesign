from __future__ import annotations

from dataclasses import dataclass

from .wire import Wire


# 连接器类型 → 生产厂商 绑定表（程序内部自动设置，用户不可选）
CONNECTOR_MANUFACTURER_MAP: dict[str, str] = {
    "MC4": "Staubli",
    "S418": "Bizlink",
    "MC4-EVO2A": "Staubli",
    "T6": "Tlian",
    "QC4.10": "QC Solar",
    "PV-ZH202B": "Zhonghuan",
    "JK03M2": "Jinko",
    "TS4 PLUS": "Trina",
    "PV-JM608": "Jiaming",
    "PV4-S": "TE connectivity",
    "RY01": "Runergy",
    "XT101.1": "Xietong",
    "PV-LR5": "Longi",
    "TT02": "Chuangda",
    "T6-2C": "Tlian",
    "PV-SY02": "Risen",
    "TS4": "Trina",
    "TS4-PLUS-3": "Trina",
    "PV-RY01": "Runergy",
    "H4-Plus": "Amphenol",
    "PV-TS02": "Tiansheng",
    "PV-RY02": "Runergy",
    "PV5": "TE connectivity",
    "Z4S": "Zerun",
    "none": "none",
}

VALID_CONNECTOR_TYPES = set(CONNECTOR_MANUFACTURER_MAP.keys())

VALID_MANUFACTURERS = set(CONNECTOR_MANUFACTURER_MAP.values())


@dataclass
class Connector:
    size: Wire
    type: str
    manufacturer: str

    def __post_init__(self) -> None:
        if self.type not in VALID_CONNECTOR_TYPES:
            raise ValueError(
                f"Invalid connector type: {self.type}. "
                f"Must be one of {sorted(VALID_CONNECTOR_TYPES)}"
            )
        # 根据类型自动设置厂商（忽略传入的 manufacturer 值）
        expected_mfr = CONNECTOR_MANUFACTURER_MAP.get(self.type, "none")
        if self.manufacturer != expected_mfr:
            self.manufacturer = expected_mfr

    def to_dict(self) -> dict[str, object]:
        return {
            "size": self.size.to_dict(),
            "type": self.type,
            "manufacturer": self.manufacturer,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> Connector | None:
        if data is None:
            return None
        return cls(
            size=Wire.from_dict(data["size"]),  # type: ignore[arg-type]
            type=str(data["type"]),
            manufacturer=str(data["manufacturer"]),
        )

"""Assembled product model — the result of product generation."""

from __future__ import annotations

from dataclasses import dataclass, field

from .part import Part


@dataclass
class AssembledProduct:
    """组装后的产品（正极或负极），包含部件列表"""

    code: str = ""
    polarity: str = ""  # "POS" / "NEG"
    quantity: int = 1
    description: str = ""
    pv_panel: str = ""
    uses_aluminum_wire: bool = False
    parts: list[Part] = field(default_factory=list)

    # ---- 对应关系表元数据 ----
    product_type: str = ""  # Generated types; Placeholder never produces one.
    inv_name: str = ""
    node_name: str = ""
    row_name: str = ""
    rtr_count: float = 0.0
    racking_pitch_str: str = ""  # racking_pitch 字面量
    racking_pitch_ft: float = 0.0  # racking_pitch 换算为 ft
    offset_str: str = ""  # offset 字面量
    offset_ft: float = 0.0  # offset 换算为 ft
    whip_ft: float = 0.0  # whip 长度 (ft)
    rackings_name: str = ""
    preset_name: str = ""
    extender_ft: float = 0.0  # extender 长度 (ft)
    crossed_presets: str = ""  # 跨过的产品
    preset_remarks: str = ""

    # ---- LYNX 对应关系表专用字段 ----
    box_position: float = 0.0
    cluster_length_ft: float = 0.0
    buffer_length_ft: float = 0.0
    first_slot_length_ft: float = 0.0
    external_main_line_total_ft: float = 0.0
    cluster_wire_info: str = ""
    row_per_overmold: int = 0
    overmold_count: int = 0
    lynx_branch_count: int = 0


    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "polarity": self.polarity,
            "quantity": self.quantity,
            "description": self.description,
            "pv_panel": self.pv_panel,
            "uses_aluminum_wire": self.uses_aluminum_wire,
            "parts": [p.to_dict() for p in self.parts],
            # 对应关系表元数据
            "product_type": self.product_type,
            "inv_name": self.inv_name,
            "node_name": self.node_name,
            "row_name": self.row_name,
            "rtr_count": self.rtr_count,
            "racking_pitch_str": self.racking_pitch_str,
            "racking_pitch_ft": self.racking_pitch_ft,
            "offset_str": self.offset_str,
            "offset_ft": self.offset_ft,
            "whip_ft": self.whip_ft,
            "rackings_name": self.rackings_name,
            "preset_name": self.preset_name,
            "extender_ft": self.extender_ft,
            "crossed_presets": self.crossed_presets,
            "preset_remarks": self.preset_remarks,
            # LYNX 对应关系表专用字段
            "box_position": self.box_position,
            "cluster_length_ft": self.cluster_length_ft,
            "buffer_length_ft": self.buffer_length_ft,
            "first_slot_length_ft": self.first_slot_length_ft,
            "external_main_line_total_ft": self.external_main_line_total_ft,
            "cluster_wire_info": self.cluster_wire_info,
            "row_per_overmold": self.row_per_overmold,
            "overmold_count": self.overmold_count,
            "lynx_branch_count": self.lynx_branch_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AssembledProduct:
        return cls(
            code=str(data.get("code", "")),
            polarity=str(data.get("polarity", "")),
            quantity=int(data.get("quantity", 1)),
            description=str(data.get("description", "")),
            pv_panel=str(data.get("pv_panel", "")),
            uses_aluminum_wire=bool(data.get("uses_aluminum_wire", False)),
            parts=[Part.from_dict(p) for p in data.get("parts", [])],  # type: ignore[arg-type]
            # 对应关系表元数据
            product_type=str(data.get("product_type", "")),
            inv_name=str(data.get("inv_name", "")),
            node_name=str(data.get("node_name", "")),
            row_name=str(data.get("row_name", "")),
            rtr_count=float(data.get("rtr_count", 0.0)),
            racking_pitch_str=str(data.get("racking_pitch_str", "")),
            racking_pitch_ft=float(data.get("racking_pitch_ft", 0.0)),
            offset_str=str(data.get("offset_str", "")),
            offset_ft=float(data.get("offset_ft", 0.0)),
            whip_ft=float(data.get("whip_ft", 0.0)),
            rackings_name=str(data.get("rackings_name", "")),
            preset_name=str(data.get("preset_name", "")),
            extender_ft=float(data.get("extender_ft", 0.0)),
            crossed_presets=str(data.get("crossed_presets", "")),
            preset_remarks=str(data.get("preset_remarks", "")),
            # LYNX 对应关系表专用字段
            box_position=float(data.get("box_position", 0.0)),
            cluster_length_ft=float(data.get("cluster_length_ft", 0.0)),
            buffer_length_ft=float(data.get("buffer_length_ft", 0.0)),
            first_slot_length_ft=float(data.get("first_slot_length_ft", 0.0)),
            external_main_line_total_ft=float(data.get("external_main_line_total_ft", 0.0)),
            cluster_wire_info=str(data.get("cluster_wire_info", "")),
            row_per_overmold=int(data.get("row_per_overmold", 0)),
            overmold_count=int(data.get("overmold_count", 0)),
            lynx_branch_count=int(data.get("lynx_branch_count", 0)),
        )

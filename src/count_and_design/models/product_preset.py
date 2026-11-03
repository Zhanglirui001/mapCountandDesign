from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from .connector import Connector
from .fuse import Fuse
from .ibex import IBEXBranchCoordinate
from .length import Length, get_length_field_names
from .wire import Wire

VALID_PRODUCT_TYPES = {
    "IBEX",
    "LYNX",
    "Gap_Extender",
    "Homerun_Extender",
    "Placeholder",
}
BRANCH_COUNT_PRODUCT_TYPES = {"IBEX", "Homerun_Extender", "Placeholder"}


def product_type_for_branch_count(branch_count: int) -> str:
    """Return the product family implied by a branch count."""
    if branch_count == 0:
        return "Placeholder"
    if branch_count == 1:
        return "Homerun_Extender"
    if branch_count > 1:
        return "IBEX"
    raise ValueError("branch_count must be greater than or equal to zero.")

# Length 默认值工厂（partial 预绑定，供 dataclass field 的 default_factory 使用）
_L0 = partial(Length, "0mm")
_L1 = partial(Length, "1ft")
_L2 = partial(Length, "2ft")
_L3 = partial(Length, "3ft")
_L4 = partial(Length, "4ft")
_L11 = partial(Length, "11ft")
_L100 = partial(Length, "100ft")

# 默认 "none" 组件工厂（供自动创建 ProductPreset 时使用）
def _default_connector() -> Connector:
    return Connector(size=Wire("none", "Cu"), type="none", manufacturer="none")

def _default_fuse() -> Fuse:
    return Fuse(current="none", manufacturer="none")

def _default_cu_wire() -> Wire:
    return Wire("none", "Cu")

def _default_al_wire() -> Wire:
    return Wire("none", "Al")


def _clone_length(source: Length, identifiers: list | None = None) -> Length:
    """构造一个独立的 Length 副本。

    Length 是可变的（界面编辑会就地改写同一个实例），因此需要回退或复制
    某个 Length 字段时必须新建实例，不能共享对象。
    """
    clone = Length.from_dict(source.to_dict(), identifiers)  # type: ignore[arg-type]
    return clone if clone is not None else Length("0mm")


@dataclass
class ProductPreset:
    """产品预设，包含 IBEX 或 LYNX 类型产品的公共参数与特有参数。

    通过 product_type 字段区分类型：
    - "IBEX": 使用 IBEX 相关字段（branch_count, branch_lines, product_total_length 等）
    - "LYNX": 使用 LYNX 相关字段（branch_length_cu/al 等）
    """

    # ---- 公共字段 ----
    name: str
    product_type: str = "IBEX"
    remarks: str = ""
    pv_panel: str = ""
    branch_cu_wire: Wire = field(default_factory=_default_cu_wire)
    branch_al_wire: Wire = field(default_factory=_default_al_wire)

    # ---- IBEX 特有字段 ----
    branch_count: int = 1
    branch_lines: list[IBEXBranchCoordinate] = field(default_factory=list)
    product_total_length: Length = field(default_factory=_L0)
    branch_connector: Connector = field(default_factory=_default_connector)
    branch_fuse: Fuse = field(default_factory=_default_fuse)
    # 末端支线（最后一条支线）特有线号与保险丝。
    # 未单独配置（"none"）时回退到一般支线的对应属性（见 effective_* 属性）。
    last_branch_cu_wire: Wire = field(default_factory=_default_cu_wire)
    last_branch_al_wire: Wire = field(default_factory=_default_al_wire)
    last_branch_fuse: Fuse = field(default_factory=_default_fuse)
    external_cu_wire: Wire = field(default_factory=_default_cu_wire)
    external_al_wire: Wire = field(default_factory=_default_al_wire)
    external_fuse: Fuse = field(default_factory=_default_fuse)
    external_connector: Connector = field(default_factory=_default_connector)
    internal_cu_wire: Wire = field(default_factory=_default_cu_wire)
    internal_al_wire: Wire = field(default_factory=_default_al_wire)

    # 外部主线铜铝转接阈值
    external_al_threshold: Length = field(default_factory=_L100)
    # 末端支线铜铝转接阈值
    last_branch_al_threshold: Length = field(default_factory=_L100)
    # 内部主线（相邻支线之间的跨接线）铜铝转接阈值
    internal_al_threshold: Length = field(default_factory=_L100)

    # IBEX 默认参数
    branch_length_positive: Length = field(default_factory=_L4)
    branch_length_negative: Length = field(default_factory=_L4)
    last_branch_with_extra_length: bool = False
    branch_fuse_prefix: Length = field(default_factory=_L1)
    branch_first_cual_prefix: Length = field(default_factory=_L2)
    branch_second_cual_suffix: Length = field(default_factory=_L2)
    external_fuse_suffix: Length = field(default_factory=_L1)
    external_cu_length_to_row_head: Length = field(default_factory=_L3)
    external_second_cual_suffix: Length = field(default_factory=_L1)
    external_extension_trench_length: Length = field(default_factory=_L0)
    internal_first_cual_prefix: Length = field(default_factory=_L1)
    internal_second_cual_suffix: Length = field(default_factory=_L1)

    # ---- LYNX 特有字段 ----
    branch_length_cu: Length = field(default_factory=_L11)
    branch_length_al: Length = field(default_factory=_L1)
    cluster_wire: Wire = field(default_factory=_default_al_wire)
    row_per_overmold: int = 1
    tail_shortened: bool = False
    nearby_overmold_gap: Length = field(default_factory=lambda: Length("3ft"))
    buffer_length: Length = field(default_factory=_L0)

    # ---- Gap Extender 特有字段 ----
    connector_male: Connector = field(default_factory=_default_connector)
    connector_female: Connector = field(default_factory=_default_connector)
    cu_wire: Wire = field(default_factory=_default_cu_wire)
    length: Length = field(default_factory=_L0)
    label: str = ""

    def __post_init__(self) -> None:
        # 兼容旧数据：将 "Extender" 标准化为 "Gap_Extender"
        if self.product_type == "Extender":
            self.product_type = "Gap_Extender"

        # 校验 name：
        # - IBEX 类型：必须是单个大写字母（与 Rackings.product_presets 中的引用方式一致）
        # - LYNX 类型：必须是 "L" + 若干个大写字母（如 "LA", "LB", "LABC"）
        if self.product_type == "LYNX":
            if not re.fullmatch(r"L[A-Z]+", self.name):
                raise ValueError(
                    "LYNX ProductPreset name must start with 'L' followed by uppercase letters (e.g., LA, LB)."
                )
        elif self.product_type == "Gap_Extender":
            if not self.name or not re.fullmatch(r".+", self.name):
                raise ValueError(
                    f"{self.product_type} ProductPreset name must not be empty."
                )
        elif self.product_type in {"Homerun_Extender", "Placeholder"}:
            # Homerun_Extender / Placeholder 复用 IBEX 的单字母引用规则。
            if not re.fullmatch(r"[A-Z]", self.name):
                raise ValueError(
                    f"{self.product_type} ProductPreset name must be a single uppercase letter."
                )
        else:
            if not re.fullmatch(r"[A-Z]", self.name):
                raise ValueError("ProductPreset name must be a single uppercase letter for IBEX type.")

        # 校验 product_type
        if self.product_type not in VALID_PRODUCT_TYPES:
            raise ValueError(
                f"Invalid product_type: {self.product_type}. "
                f"Must be one of {sorted(VALID_PRODUCT_TYPES)}"
            )

        # IBEX-family presets derive their type from branch_count.  This also
        # normalizes legacy files where a zero/one-branch preset was stored as
        # IBEX before Placeholder/Homerun became explicit types.
        if self.product_type in BRANCH_COUNT_PRODUCT_TYPES:
            self.product_type = product_type_for_branch_count(self.branch_count)

        # 确保 Length 类型字段均为 Length 实例（自动发现，无需手动维护列表）
        for attr in get_length_field_names(self):
            value = getattr(self, attr)
            if value is not None and not isinstance(value, Length):
                setattr(self, attr, Length(str(value)))

        # Auto-populate branch_lines if branch_count > 0 and list is empty
        if self.branch_count > 0 and not self.branch_lines:
            self.branch_lines = [
                IBEXBranchCoordinate(index=i, coordinate_positive=Length("0mm"), coordinate_negative=Length("0mm"))
                for i in range(1, self.branch_count + 1)
            ]

        # Auto-sync connector wire sizes with their connected wires.
        # IBEX: branch_connector ← branch_cu_wire, external_connector ← external_cu_wire
        # Gap Extender: connector_male / connector_female ← cu_wire
        self._sync_connector_sizes()

    def _sync_connector_sizes(self) -> None:
        """将连接器的线号与其所连接导线的线号自动同步。

        - IBEX 支线连接器 ← branch_cu_wire
        - IBEX 外部连接器 ← external_cu_wire
        - Gap Extender 公头/母头连接器 ← cu_wire

        导线为 "none" 时连接器也跟随为 "none"。
        """
        # IBEX: 支线连接器随支线铜线
        self.branch_connector.size.wiresize = self.branch_cu_wire.wiresize
        # IBEX: 外部连接器随外部铜线
        self.external_connector.size.wiresize = self.external_cu_wire.wiresize
        # Gap Extender: 公母头连接器随铜线
        self.connector_male.size.wiresize = self.cu_wire.wiresize
        self.connector_female.size.wiresize = self.cu_wire.wiresize

    # ---- 末端支线有效属性（未单独配置时回退到一般支线）----

    @property
    def effective_last_branch_cu_wire(self) -> Wire:
        """末端支线铜线：未单独配置（"none"）时回退到一般支线铜线。"""
        if self.last_branch_cu_wire.wiresize != "none":
            return self.last_branch_cu_wire
        return self.branch_cu_wire

    @property
    def effective_last_branch_al_wire(self) -> Wire:
        """末端支线铝线：未单独配置（"none"）时回退到一般支线铝线。"""
        if self.last_branch_al_wire.wiresize != "none":
            return self.last_branch_al_wire
        return self.branch_al_wire

    @property
    def effective_last_branch_fuse(self) -> Fuse:
        """末端支线保险丝：未单独配置（"none"）时回退到一般支线保险丝。"""
        if self.last_branch_fuse.current != "none":
            return self.last_branch_fuse
        return self.branch_fuse

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "product_type": self.product_type,
            "remarks": self.remarks,
            "pv_panel": self.pv_panel,
        }

        # IBEX fields
        result["branch_count"] = self.branch_count
        result["branch_lines"] = [bl.to_dict() for bl in self.branch_lines]
        result["product_total_length"] = self.product_total_length.to_dict()
        result["branch_connector"] = self.branch_connector.to_dict()
        result["branch_fuse"] = self.branch_fuse.to_dict()
        result["branch_cu_wire"] = self.branch_cu_wire.to_dict()
        result["branch_al_wire"] = self.branch_al_wire.to_dict()
        result["last_branch_cu_wire"] = self.last_branch_cu_wire.to_dict()
        result["last_branch_al_wire"] = self.last_branch_al_wire.to_dict()
        result["last_branch_fuse"] = self.last_branch_fuse.to_dict()
        result["external_cu_wire"] = self.external_cu_wire.to_dict()
        result["external_al_wire"] = self.external_al_wire.to_dict()
        result["external_fuse"] = self.external_fuse.to_dict()
        result["external_connector"] = self.external_connector.to_dict()
        result["internal_cu_wire"] = self.internal_cu_wire.to_dict()
        result["internal_al_wire"] = self.internal_al_wire.to_dict()

        # Copper/aluminum transition thresholds
        result["external_al_threshold"] = self.external_al_threshold.to_dict()
        result["last_branch_al_threshold"] = self.last_branch_al_threshold.to_dict()
        result["internal_al_threshold"] = self.internal_al_threshold.to_dict()

        # IBEX default params
        result["branch_length_positive"] = self.branch_length_positive.to_dict()
        result["branch_length_negative"] = self.branch_length_negative.to_dict()
        result["last_branch_with_extra_length"] = self.last_branch_with_extra_length
        result["branch_fuse_prefix"] = self.branch_fuse_prefix.to_dict()
        result["branch_first_cual_prefix"] = self.branch_first_cual_prefix.to_dict()
        result["branch_second_cual_suffix"] = self.branch_second_cual_suffix.to_dict()
        result["external_fuse_suffix"] = self.external_fuse_suffix.to_dict()
        result["external_cu_length_to_row_head"] = (
            self.external_cu_length_to_row_head.to_dict()
        )
        result["external_second_cual_suffix"] = (
            self.external_second_cual_suffix.to_dict()
        )
        result["external_extension_trench_length"] = (
            self.external_extension_trench_length.to_dict()
        )
        result["internal_first_cual_prefix"] = (
            self.internal_first_cual_prefix.to_dict()
        )
        result["internal_second_cual_suffix"] = (
            self.internal_second_cual_suffix.to_dict()
        )

        # LYNX fields
        result["branch_length_cu"] = self.branch_length_cu.to_dict()
        result["branch_length_al"] = self.branch_length_al.to_dict()
        result["cluster_wire"] = self.cluster_wire.to_dict()
        result["row_per_overmold"] = self.row_per_overmold
        result["tail_shortened"] = self.tail_shortened
        result["nearby_overmold_gap"] = self.nearby_overmold_gap.to_dict()
        result["buffer_length"] = self.buffer_length.to_dict()

        # Gap Extender fields
        result["connector_male"] = self.connector_male.to_dict()
        result["connector_female"] = self.connector_female.to_dict()
        result["cu_wire"] = self.cu_wire.to_dict()
        result["length"] = self.length.to_dict()
        result["label"] = self.label

        return result

    @classmethod
    def from_dict(
        cls,
        data: dict[str, object],
        identifiers: list | None = None,
    ) -> ProductPreset:
        def _len(key: str, default_factory: callable = _L0) -> Length:
            return Length.from_dict(
                data.get(key), identifiers  # type: ignore[arg-type]
            ) or default_factory()

        def _wire(key: str, default: Wire | None = None) -> Wire:
            result = Wire.from_dict(data.get(key))  # type: ignore[arg-type]
            return result if result is not None else (default or _default_cu_wire())

        def _connector(key: str) -> Connector:
            result = Connector.from_dict(data.get(key))  # type: ignore[arg-type]
            return result if result is not None else _default_connector()

        def _fuse(key: str) -> Fuse:
            result = Fuse.from_dict(data.get(key))  # type: ignore[arg-type]
            return result if result is not None else _default_fuse()

        external_al_threshold = _len("external_al_threshold", _L100)
        # 旧存档没有 internal_al_threshold 键：回退到该预设的 external_al_threshold，
        # 保持升级前"内部主线复用外部阈值"的产品输出不变。
        internal_al_threshold = _len(
            "internal_al_threshold",
            lambda: _clone_length(external_al_threshold, identifiers),
        )

        return cls(
            name=str(data["name"]),
            product_type=str(data.get("product_type", "IBEX")),
            remarks=str(data.get("remarks", "")),
            pv_panel=str(data.get("pv_panel", "")),
            # IBEX fields
            branch_count=int(data.get("branch_count", 1)),
            branch_lines=[
                IBEXBranchCoordinate.from_dict(bl, identifiers)  # type: ignore[arg-type]
                for bl in data.get("branch_lines", [])
            ],
            product_total_length=_len("product_total_length", _L0),
            branch_connector=_connector("branch_connector"),
            branch_fuse=_fuse("branch_fuse"),
            branch_cu_wire=_wire("branch_cu_wire"),
            branch_al_wire=_wire("branch_al_wire"),
            last_branch_cu_wire=_wire("last_branch_cu_wire"),
            last_branch_al_wire=_wire("last_branch_al_wire"),
            last_branch_fuse=_fuse("last_branch_fuse"),
            external_cu_wire=_wire("external_cu_wire"),
            external_al_wire=_wire("external_al_wire"),
            external_fuse=_fuse("external_fuse"),
            external_connector=_connector("external_connector"),
            internal_cu_wire=_wire("internal_cu_wire"),
            internal_al_wire=_wire("internal_al_wire"),
            # IBEX default params
            branch_length_positive=_len("branch_length_positive", _L4),
            branch_length_negative=_len("branch_length_negative", _L4),
            last_branch_with_extra_length=bool(
                data.get("last_branch_with_extra_length", False)
            ),
            branch_fuse_prefix=_len("branch_fuse_prefix", _L1),
            branch_first_cual_prefix=_len("branch_first_cual_prefix", _L2),
            branch_second_cual_suffix=_len("branch_second_cual_suffix", _L2),
            external_fuse_suffix=_len("external_fuse_suffix", _L1),
            external_cu_length_to_row_head=_len(
                "external_cu_length_to_row_head", _L3
            ),
            external_second_cual_suffix=_len(
                "external_second_cual_suffix", _L1
            ),
            external_al_threshold=external_al_threshold,
            last_branch_al_threshold=_len("last_branch_al_threshold", _L100),
            internal_al_threshold=internal_al_threshold,
            external_extension_trench_length=_len(
                "external_extension_trench_length", _L0
            ),
            internal_first_cual_prefix=_len(
                "internal_first_cual_prefix", _L1
            ),
            internal_second_cual_suffix=_len(
                "internal_second_cual_suffix", _L1
            ),
            # LYNX fields
            branch_length_cu=_len("branch_length_cu", _L11),
            branch_length_al=_len("branch_length_al", _L1),
            cluster_wire=_wire("cluster_wire", _default_al_wire()),
            row_per_overmold=int(data.get("row_per_overmold", 1)),
            tail_shortened=bool(data.get("tail_shortened", False)),
            nearby_overmold_gap=_len("nearby_overmold_gap", lambda: Length("3ft")),
            buffer_length=_len("buffer_length", _L0),
            # Gap Extender fields
            label=str(data.get("label", "")),
            connector_male=_connector("connector_male"),
            connector_female=_connector("connector_female"),
            cu_wire=_wire("cu_wire"),
            length=_len("length", _L0),
        )

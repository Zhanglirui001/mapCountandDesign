from __future__ import annotations

from dataclasses import dataclass, field

from .length import Length


@dataclass
class IBEXBranchCoordinate:
    """IBEX 支线坐标，以第一个铜铝转接器为参考起点。"""

    index: int = 1
    coordinate_positive: Length = field(default_factory=lambda: Length("0mm"))
    coordinate_negative: Length = field(default_factory=lambda: Length("0mm"))

    def __post_init__(self) -> None:
        if not isinstance(self.coordinate_positive, Length):
            self.coordinate_positive = Length(str(self.coordinate_positive))
        if not isinstance(self.coordinate_negative, Length):
            self.coordinate_negative = Length(str(self.coordinate_negative))

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "coordinate_positive": self.coordinate_positive.to_dict(),
            "coordinate_negative": self.coordinate_negative.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, object],
        identifiers: list | None = None,
    ) -> IBEXBranchCoordinate:
        return cls(
            index=int(data["index"]),
            coordinate_positive=Length.from_dict(
                data.get("coordinate_positive"), identifiers  # type: ignore[arg-type]
            ) or Length("0mm"),
            coordinate_negative=Length.from_dict(
                data.get("coordinate_negative"), identifiers  # type: ignore[arg-type]
            ) or Length("0mm"),
        )


@dataclass
class IBEXBranch:
    """IBEX 支线，表示一条支线的前置长度。"""

    index: int = 0
    prefix_length: Length = field(default_factory=lambda: Length("0mm"))

    def __post_init__(self) -> None:
        if not isinstance(self.prefix_length, Length):
            self.prefix_length = Length(str(self.prefix_length))

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "prefix_length": self.prefix_length.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, object],
        identifiers: list | None = None,
    ) -> IBEXBranch:
        return cls(
            index=int(data["index"]),
            prefix_length=Length.from_dict(
                data.get("prefix_length"), identifiers  # type: ignore[arg-type]
            ) or Length("0mm"),
        )


@dataclass
class IBEXProduct:
    """IBEX 产品，引用一个 ProductPreset 并补充具体参数。"""

    product_preset: str
    product_code: str = ""
    polarity: str = "POS"
    extender_length: Length = field(default_factory=lambda: Length("0mm"))
    whip_length: Length = field(default_factory=lambda: Length("0mm"))
    branches: list[IBEXBranch] = field(default_factory=list)

    VALID_POLARITIES = {"POS", "NEG"}

    def __post_init__(self) -> None:
        if self.polarity not in self.VALID_POLARITIES:
            raise ValueError(
                f"Invalid polarity: {self.polarity}. Must be POS or NEG."
            )
        if not isinstance(self.extender_length, Length):
            self.extender_length = Length(str(self.extender_length))
        if not isinstance(self.whip_length, Length):
            self.whip_length = Length(str(self.whip_length))

    def to_dict(self) -> dict[str, object]:
        return {
            "product_preset": self.product_preset,
            "product_code": self.product_code,
            "polarity": self.polarity,
            "extender_length": self.extender_length.to_dict(),
            "whip_length": self.whip_length.to_dict(),
            "branches": [b.to_dict() for b in self.branches],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, object],
        identifiers: list | None = None,
    ) -> IBEXProduct:
        return cls(
            product_preset=str(data["product_preset"]),
            product_code=str(data.get("product_code", "")),
            polarity=str(data.get("polarity", "POS")),
            extender_length=Length.from_dict(
                data.get("extender_length", data.get("extender_length")), identifiers  # type: ignore[arg-type]
            ) or Length("0mm"),
            whip_length=Length.from_dict(
                data.get("whip_length"), identifiers  # type: ignore[arg-type]
            ) or Length("0mm"),
            branches=[
                IBEXBranch.from_dict(b, identifiers)  # type: ignore[arg-type]
                for b in data.get("branches", [])
            ],
        )

from __future__ import annotations

from dataclasses import dataclass, field


_QUANTITY_KINDS = {"nodes", "strings", "racking", "product"}


@dataclass(frozen=True)
class QuantityRef:
    """Reference to one primitive project quantity."""

    kind: str
    name: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _QUANTITY_KINDS:
            raise ValueError(f"Unsupported quantity kind: {self.kind}")
        if self.kind in {"nodes", "strings"} and self.name:
            raise ValueError(f"{self.kind} quantities cannot have a name")
        if self.kind in {"racking", "product"} and not self.name:
            raise ValueError(f"{self.kind} quantities require a name")

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "name": self.name}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "QuantityRef":
        return cls(kind=str(data.get("kind", "")), name=str(data.get("name", "")))


@dataclass(frozen=True)
class DataFillTerm:
    """One non-zero coefficient in a sparse data-fill formula."""

    ref: QuantityRef
    coefficient: int

    def __post_init__(self) -> None:
        if isinstance(self.coefficient, bool) or not isinstance(self.coefficient, int):
            raise ValueError("Data-fill coefficients must be integers")
        if self.coefficient == 0:
            raise ValueError("Data-fill coefficients cannot be zero")

    def to_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref.to_dict(),
            "coefficient": self.coefficient,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DataFillTerm":
        ref_data = data.get("ref", {})
        if not isinstance(ref_data, dict):
            raise ValueError("Invalid data-fill term reference")
        return cls(
            ref=QuantityRef.from_dict(ref_data),
            coefficient=int(data.get("coefficient", 0)),
        )


@dataclass
class DataFillTarget:
    """An ordered soft equality target used by project data fill."""

    target_id: str
    label: str
    terms: list[DataFillTerm] = field(default_factory=list)
    target: int = 0
    enabled: bool = False
    builtin_ref: QuantityRef | None = None

    @property
    def is_builtin(self) -> bool:
        return self.builtin_ref is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.target_id,
            "label": self.label,
            "terms": [term.to_dict() for term in self.terms],
            "target": self.target,
            "enabled": self.enabled,
            "builtin_ref": self.builtin_ref.to_dict() if self.builtin_ref else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DataFillTarget":
        terms_data = data.get("terms", [])
        builtin_data = data.get("builtin_ref")
        return cls(
            target_id=str(data.get("id", "")),
            label=str(data.get("label", "")),
            terms=[
                DataFillTerm.from_dict(item)
                for item in terms_data
                if isinstance(item, dict)
            ] if isinstance(terms_data, list) else [],
            target=int(data.get("target", 0)),
            enabled=bool(data.get("enabled", False)),
            builtin_ref=(
                QuantityRef.from_dict(builtin_data)
                if isinstance(builtin_data, dict)
                else None
            ),
        )

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
import re
from typing import TYPE_CHECKING, NamedTuple, Union, get_args, get_origin, get_type_hints

from .identifier_registry import IdentifierRegistry

if TYPE_CHECKING:
    from .length_identifier import LengthIdentifier


_LENGTH_TOKEN_RE = re.compile(r"[a-zA-Z_]+|\d+(?:\.\d+)?|[+\-*/()]")
_RESERVED_UNITS = {"mm", "m", "ft", "inch", "in"}
_NUMBER_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?$")


class LengthError(ValueError):
    """Structured exception for length-expression errors.

    Carries a machine-readable *code* and optional *params* so that the
    ViewModel layer can map the error to an i18n key without the model
    depending on the i18n module.
    """

    def __init__(self, code: str, **params: object) -> None:
        self.code = code
        self.params: dict[str, object] = params
        super().__init__(code)


class DimensionError(LengthError):
    """Raised when a length expression evaluates to a non-linear dimension."""


class _DimValue(NamedTuple):
    value: float
    dim: int  # length dimension: 0=scalar, 1=length(mm), 2=area(mm²), ...


def _is_length_type(field_type: object) -> bool:
    """检查类型注解是否为 Length 或 Length | None。

    支持字符串注解（由 ``from __future__ import annotations`` 产生）。
    """
    # Handle string annotations
    if isinstance(field_type, str):
        # Simple cases: "Length", "Length | None"
        stripped = field_type.strip()
        if stripped == "Length":
            return True
        # "Length | None" or "Optional[Length]" etc.
        if "Length" in stripped and "None" in stripped:
            return True
        return False
    origin = get_origin(field_type)
    if origin is None:
        return field_type is Length
    if origin is Union:
        args = get_args(field_type)
        return Length in args and type(None) in args
    return False


def get_length_field_names(cls_or_instance) -> list[str]:
    """返回 dataclass 中所有 Length 类型字段的名称列表。

    可传入 dataclass 类或其实例。通过 dataclasses.fields() 自动发现，
    无需手动维护字段列表。

    支持 :py:mod:`dataclasses` 与 ``from __future__ import annotations``
    同时使用时的字符串注解。
    """
    if not is_dataclass(cls_or_instance):
        return []
    # Resolve string annotations via get_type_hints
    cls = cls_or_instance if isinstance(cls_or_instance, type) else type(cls_or_instance)
    try:
        hints = get_type_hints(cls, include_extras=False)
    except Exception:
        hints = {}
    result: list[str] = []
    for f in fields(cls_or_instance):
        field_type = hints.get(f.name, f.type)
        if _is_length_type(field_type):
            result.append(f.name)
    return result


def _ensure_length(value: Length | str | int | float | None) -> Length | None:
    if value is None or isinstance(value, Length):
        return value
    return Length(str(value))


@dataclass
class Length:
    length_str: str = "0mm"
    identifiers: list["LengthIdentifier"] = field(default_factory=list, repr=False)
    registry: IdentifierRegistry | None = field(default=None, repr=False, compare=False)
    _length_mm: float = field(init=False, repr=False)
    _needs_identifier_resolution: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        if self.registry is None:
            self.registry = IdentifierRegistry(self.identifiers)
        else:
            # Keep the legacy public attribute as a compatibility view.  New
            # code resolves through registry, which remains stable even when
            # the project's identifier list changes.
            self.identifiers = self.registry.identifiers
        self.length_str = self.length_str.strip() or "0mm"
        self._recalculate()

    @property
    def length_mm(self) -> float:
        return self._length_mm

    @length_mm.setter
    def length_mm(self, value: float) -> None:
        self._length_mm = float(value)
        self.length_str = f"{self._length_mm}mm"

    def set_identifiers(self, identifiers: list["LengthIdentifier"]) -> None:
        """Compatibility adapter for callers not yet using a registry."""
        self.set_registry(IdentifierRegistry(identifiers))

    def set_registry(self, registry: IdentifierRegistry) -> None:
        self.registry = registry
        self.identifiers = registry.identifiers
        self._recalculate()

    def set_length_str(self, value: str) -> None:
        stripped = value.strip() or "0mm"
        original = self.length_str
        self.length_str = stripped
        try:
            self._recalculate()
        except Exception:
            # 校验失败（如量纲错误）时不允许留下半更新的状态：
            # 回滚表达式，保持原值与原 _length_mm 一致，再向调用方抛错。
            self.length_str = original
            raise

    def _recalculate(self) -> None:
        try:
            self._length_mm = self._str_to_mm(self.length_str)
            self._needs_identifier_resolution = False
        except DimensionError:
            raise
        except ValueError:
            # 仅当表达式本身不可能通过后续设置 identifiers 来修复时才报错。
            # 如果包含标识符引用（无论 identifiers 是否已注入），
            # 静默保持 _needs_identifier_resolution=True，由 to_mm() 按需触发重试。
            if not self._contains_potential_identifier():
                raise
            self._length_mm = 0.0
            self._needs_identifier_resolution = True

    def _contains_potential_identifier(self) -> bool:
        """Check if the expression contains any identifier-like tokens (not units)."""
        compact = self.length_str.replace(" ", "")
        tokens = _LENGTH_TOKEN_RE.findall(compact)
        return any(
            re.fullmatch(r"[a-zA-Z_]+", t) and t.lower() not in _RESERVED_UNITS
            for t in tokens
        )

    def _str_to_mm(self, expression: str) -> float:
        compact = expression.replace(" ", "")
        if not compact:
            raise LengthError("empty_expression")

        tokens = _LENGTH_TOKEN_RE.findall(compact)
        if "".join(tokens) != compact:
            raise LengthError("unsupported_expression", expression=expression)
        result, next_index = self._parse_expression(tokens, 0)
        if next_index != len(tokens):
            raise LengthError("evaluation_failed", expression=expression)
        if result.dim != 1:
            raise DimensionError("dimension_error", dim=result.dim)
        return result.value

    # ── four-layer recursive-descent parser ──────────────────────
    # Layer 3 (lowest):  _parse_expression   +  -
    # Layer 2:           _parse_term          *  /
    # Layer 1:           _parse_implicit_sum  consecutive atoms (5S3R)
    # Layer 0 (highest): _parse_atom          num+id, id, num+unit, (expr), -atom

    def _parse_expression(
        self, tokens: list[str], index: int, stop_at: str | None = None
    ) -> tuple[_DimValue, int]:
        """Layer 3: explicit + and - (lowest precedence)."""
        total, index = self._parse_term(tokens, index)

        while index < len(tokens):
            token = tokens[index]
            if stop_at is not None and token == stop_at:
                return total, index + 1
            if token == "+":
                index += 1
                right, index = self._parse_term(tokens, index)
                if total.dim != right.dim:
                    raise DimensionError(
                        "dimension_mismatch", dim1=total.dim, dim2=right.dim
                    )
                total = _DimValue(total.value + right.value, total.dim)
            elif token == "-":
                index += 1
                right, index = self._parse_term(tokens, index)
                if total.dim != right.dim:
                    raise DimensionError(
                        "dimension_mismatch", dim1=total.dim, dim2=right.dim
                    )
                total = _DimValue(total.value - right.value, total.dim)
            else:
                break

        if stop_at is not None:
            raise LengthError("missing_parenthesis")
        return total, index

    def _parse_term(self, tokens: list[str], index: int) -> tuple[_DimValue, int]:
        """Layer 2: explicit * and / (medium precedence)."""
        total, index = self._parse_implicit_sum(tokens, index)

        while index < len(tokens):
            token = tokens[index]
            if token == "*":
                index += 1
                right, index = self._parse_implicit_sum(tokens, index)
                total = _DimValue(total.value * right.value, total.dim + right.dim)
            elif token == "/":
                index += 1
                right, index = self._parse_implicit_sum(tokens, index)
                total = _DimValue(total.value / right.value, total.dim - right.dim)
            else:
                break

        return total, index

    def _parse_implicit_sum(
        self, tokens: list[str], index: int
    ) -> tuple[_DimValue, int]:
        """Layer 1: consecutive atoms are implicitly added (e.g. 5S3R → 5*S + 3*R)."""
        total, index = self._parse_atom(tokens, index)

        while index < len(tokens) and self._can_start_atom(tokens[index]):
            value, index = self._parse_atom(tokens, index)
            if total.dim != value.dim:
                raise DimensionError(
                    "dimension_mismatch_implicit", dim1=total.dim, dim2=value.dim
                )
            total = _DimValue(total.value + value.value, total.dim)

        return total, index

    def _can_start_atom(self, token: str) -> bool:
        """Return True if *token* can begin a new atomic value."""
        if token == "(":
            return True
        if _NUMBER_TOKEN_RE.fullmatch(token):
            return True
        if re.fullmatch(r"[a-zA-Z_]+", token):
            return token.lower() not in _RESERVED_UNITS
        return False

    def _parse_atom(self, tokens: list[str], index: int) -> tuple[_DimValue, int]:
        """Layer 0: atomic value – unary minus, parentheses, identifiers, numbers."""
        if index >= len(tokens):
            raise LengthError("evaluation_failed", expression=self.length_str)
        token = tokens[index]

        # unary minus
        if token == "-":
            result, index = self._parse_atom(tokens, index + 1)
            return _DimValue(-result.value, result.dim), index

        # parenthesized sub-expression
        if token == "(":
            return self._parse_expression(tokens, index + 1, stop_at=")")

        # bare identifier
        if re.fullmatch(r"[a-zA-Z_]+", token):
            if token.lower() in _RESERVED_UNITS:
                raise LengthError(
                    "reserved_unit_as_identifier", token=token
                )
            return _DimValue(self._identifier_to_mm(token), 1), index + 1

        # number [ unit | identifier ]?
        if _NUMBER_TOKEN_RE.fullmatch(token):
            value = float(token)
            next_index = index + 1
            if next_index < len(tokens):
                qualifier = tokens[next_index]
                if re.fullmatch(r"[a-zA-Z_]+", qualifier):
                    if qualifier.lower() in _RESERVED_UNITS:
                        return (
                            _DimValue(
                                self._convert_value_to_mm(value, qualifier), 1
                            ),
                            next_index + 1,
                        )
                    return (
                        _DimValue(
                            value * self._identifier_to_mm(qualifier), 1
                        ),
                        next_index + 1,
                    )
            return _DimValue(value, 0), next_index

        raise LengthError("invalid_token", token=token)

    def _identifier_to_mm(self, name: str) -> float:
        assert self.registry is not None
        identifier = self.registry.find(name)
        if identifier is not None:
            return identifier.length.to_mm()
        raise LengthError("unknown_identifier", name=name)

    @staticmethod
    def extract_identifier_refs(length_str: str) -> set[str]:
        """Extract all identifier names referenced in a length expression.

        Returns only tokens that look like identifiers (letters/underscores)
        and are not reserved unit names.
        """
        tokens = _LENGTH_TOKEN_RE.findall(length_str.replace(" ", ""))
        return {
            t
            for t in tokens
            if re.fullmatch(r"[a-zA-Z_]+", t) and t.lower() not in _RESERVED_UNITS
        }

    @staticmethod
    def _convert_value_to_mm(value: float, unit: str) -> float:
        if unit == "mm":
            return value
        if unit == "m":
            return value * 1000.0
        if unit == "ft":
            return value * 304.8
        if unit in {"inch", "in"}:
            return value * 25.4
        raise LengthError("unsupported_unit", unit=unit)

    def to_mm(self) -> float:
        # Identifier expressions are evaluated on access so changes made in
        # the shared registry are visible without rebinding every Length.
        if self._needs_identifier_resolution or self._contains_potential_identifier():
            # Retry directly so callers receive the precise cause (for
            # example unknown_identifier) instead of a generic placeholder.
            self._length_mm = self._str_to_mm(self.length_str)
            self._needs_identifier_resolution = False
        return self._length_mm

    def to_m(self) -> float:
        return self.to_mm() / 1000.0

    def to_ft(self) -> float:
        return self.to_mm() / 304.8

    def to_inch(self) -> float:
        return self.to_mm() / 25.4

    def to_dict(self) -> dict[str, float | str]:
        try:
            current_mm = self.to_mm()
        except ValueError:
            # Preserve unresolved expressions in project files.  They may
            # become valid when an identifier is added later.
            current_mm = self._length_mm
        return {
            "length_str": self.length_str,
            "length_mm": current_mm,
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, object] | None,
        identifiers: list["LengthIdentifier"] | None = None,
        registry: IdentifierRegistry | None = None,
    ) -> Length | None:
        if data is None:
            return None
        length = cls(
            length_str=str(data.get("length_str", "0mm")),
            identifiers=identifiers or [],
            registry=registry,
        )
        if "length_mm" in data and data.get("length_str") in {None, ""}:
            length.length_mm = float(data["length_mm"])
        return length

    def __add__(self, other: Length | str | int | float) -> Length:
        right = _ensure_length(other)
        return Length(f"{self.to_mm() + right.to_mm()}mm", registry=self.registry)

    def __sub__(self, other: Length | str | int | float) -> Length:
        right = _ensure_length(other)
        return Length(f"{self.to_mm() - right.to_mm()}mm", registry=self.registry)

    def __mul__(self, scalar: int | float) -> Length:
        return Length(f"{self.to_mm() * float(scalar)}mm", registry=self.registry)

    def __rmul__(self, scalar: int | float) -> Length:
        return Length(f"{self.to_mm() * float(scalar)}mm", registry=self.registry)

    def __truediv__(self, scalar: int | float) -> Length:
        return Length(f"{self.to_mm() / float(scalar)}mm", registry=self.registry)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Length | str | int | float):
            return NotImplemented
        right = _ensure_length(other)
        if right is None:
            return NotImplemented
        return self.to_mm() == right.to_mm()

    def __ne__(self, other: object) -> bool:
        if not isinstance(other, Length | str | int | float):
            return NotImplemented
        right = _ensure_length(other)
        if right is None:
            return NotImplemented
        return self.to_mm() != right.to_mm()

    def __lt__(self, other: Length | str | int | float) -> bool:
        right = _ensure_length(other)
        return self.to_mm() < right.to_mm()  # type: ignore[return-value]

    def __le__(self, other: Length | str | int | float) -> bool:
        right = _ensure_length(other)
        return self.to_mm() <= right.to_mm()  # type: ignore[return-value]

    def __gt__(self, other: Length | str | int | float) -> bool:
        right = _ensure_length(other)
        return self.to_mm() > right.to_mm()  # type: ignore[return-value]

    def __ge__(self, other: Length | str | int | float) -> bool:
        right = _ensure_length(other)
        return self.to_mm() >= right.to_mm()  # type: ignore[return-value]

    def __repr__(self) -> str:
        return f"Length(length_str={self.length_str!r}, length_mm={self._length_mm!r})"

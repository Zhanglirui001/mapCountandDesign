from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from ...models import DataFillTerm, QuantityRef


MAX_COEFFICIENT = 2_000_000_000


class DataFillFormulaError(ValueError):
    """Formula error with a stable code suitable for GUI translation."""

    def __init__(self, code: str, *, token: str = "") -> None:
        self.code = code
        self.token = token
        super().__init__(code)


@dataclass
class _LinearExpression:
    coefficients: OrderedDict[QuantityRef, int] = field(default_factory=OrderedDict)
    constant: int = 0

    @property
    def has_quantities(self) -> bool:
        return any(self.coefficients.values())

    def scaled(self, scalar: int) -> "_LinearExpression":
        _check_integer(scalar)
        result = _LinearExpression(constant=self.constant * scalar)
        _check_integer(result.constant)
        for ref, coefficient in self.coefficients.items():
            value = coefficient * scalar
            _check_integer(value)
            result.coefficients[ref] = value
        return result

    def add(self, other: "_LinearExpression", sign: int = 1) -> "_LinearExpression":
        result = _LinearExpression(OrderedDict(self.coefficients), self.constant)
        result.constant += sign * other.constant
        _check_integer(result.constant)
        for ref, coefficient in other.coefficients.items():
            value = result.coefficients.get(ref, 0) + sign * coefficient
            _check_integer(value)
            result.coefficients[ref] = value
        return result


def _check_integer(value: int) -> None:
    if abs(value) > MAX_COEFFICIENT:
        raise DataFillFormulaError("formula_invalid_coefficient")


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source.replace("×", "*")
        self.index = 0

    def parse(self) -> list[DataFillTerm]:
        self._skip_space()
        if self._accept("="):
            self._skip_space()
        if self.index >= len(self.source):
            raise DataFillFormulaError("formula_empty")
        expression = self._parse_sum()
        self._skip_space()
        if self.index != len(self.source):
            if self.source[self.index] in "/.^":
                raise DataFillFormulaError("formula_nonlinear")
            raise DataFillFormulaError("formula_syntax", token=self.source[self.index])
        if expression.constant != 0:
            raise DataFillFormulaError("formula_syntax", token=str(expression.constant))
        terms = [
            DataFillTerm(ref, coefficient)
            for ref, coefficient in expression.coefficients.items()
            if coefficient != 0
        ]
        if not terms:
            raise DataFillFormulaError("formula_empty")
        return terms

    def _parse_sum(self) -> _LinearExpression:
        left = self._parse_product()
        while True:
            self._skip_space()
            if self._accept("+"):
                left = left.add(self._parse_product())
            elif self._accept("-"):
                left = left.add(self._parse_product(), -1)
            else:
                return left

    def _parse_product(self) -> _LinearExpression:
        left = self._parse_unary()
        while True:
            self._skip_space()
            if not self._accept("*"):
                return left
            right = self._parse_unary()
            if left.has_quantities and right.has_quantities:
                raise DataFillFormulaError("formula_nonlinear")
            if not left.has_quantities and not right.has_quantities:
                left = _LinearExpression(constant=left.constant * right.constant)
                _check_integer(left.constant)
            elif left.has_quantities:
                left = left.scaled(right.constant)
            else:
                left = right.scaled(left.constant)

    def _parse_unary(self) -> _LinearExpression:
        self._skip_space()
        if self._accept("+"):
            return self._parse_unary()
        if self._accept("-"):
            return self._parse_unary().scaled(-1)
        return self._parse_atom()

    def _parse_atom(self) -> _LinearExpression:
        self._skip_space()
        if self.index >= len(self.source):
            raise DataFillFormulaError("formula_syntax")
        if self._accept("("):
            value = self._parse_sum()
            self._skip_space()
            if not self._accept(")"):
                raise DataFillFormulaError("formula_syntax", token=")")
            return value
        char = self.source[self.index]
        if char.isdigit():
            start = self.index
            while self.index < len(self.source) and self.source[self.index].isdigit():
                self.index += 1
            if self.index < len(self.source) and self.source[self.index] == ".":
                raise DataFillFormulaError("formula_invalid_coefficient")
            value = int(self.source[start:self.index])
            _check_integer(value)
            return _LinearExpression(constant=value)
        if char.isalpha() or char == "_":
            return self._parse_reference()
        raise DataFillFormulaError("formula_syntax", token=char)

    def _parse_reference(self) -> _LinearExpression:
        start = self.index
        while self.index < len(self.source) and (
            self.source[self.index].isalnum() or self.source[self.index] == "_"
        ):
            self.index += 1
        token = self.source[start:self.index]
        keyword = token.upper()
        if keyword == "NODE":
            ref = QuantityRef("nodes")
        elif keyword == "STRINGS":
            ref = QuantityRef("strings")
        elif keyword in {"R", "P"}:
            self._skip_space()
            if not self._accept("["):
                raise DataFillFormulaError("formula_syntax", token=token)
            name = self._parse_bracket_name()
            ref = QuantityRef("racking" if keyword == "R" else "product", name)
        else:
            raise DataFillFormulaError("formula_unknown_reference", token=token)
        result = _LinearExpression()
        result.coefficients[ref] = 1
        return result

    def _parse_bracket_name(self) -> str:
        chars: list[str] = []
        while self.index < len(self.source):
            char = self.source[self.index]
            self.index += 1
            if char == "]":
                if not chars:
                    raise DataFillFormulaError("formula_unknown_reference")
                return "".join(chars)
            if char == "\\":
                if self.index >= len(self.source) or self.source[self.index] not in {"\\", "]"}:
                    raise DataFillFormulaError("formula_syntax", token="\\")
                chars.append(self.source[self.index])
                self.index += 1
            else:
                chars.append(char)
        raise DataFillFormulaError("formula_syntax", token="]")

    def _skip_space(self) -> None:
        while self.index < len(self.source) and self.source[self.index].isspace():
            self.index += 1

    def _accept(self, token: str) -> bool:
        if self.source.startswith(token, self.index):
            self.index += len(token)
            return True
        return False


def parse_data_fill_formula(expression: str) -> list[DataFillTerm]:
    return _Parser(expression).parse()


def _escape_name(name: str) -> str:
    return name.replace("\\", "\\\\").replace("]", "\\]")


def format_quantity_ref(ref: QuantityRef) -> str:
    if ref.kind == "nodes":
        return "NODE"
    if ref.kind == "strings":
        return "STRINGS"
    prefix = "R" if ref.kind == "racking" else "P"
    return f"{prefix}[{_escape_name(ref.name)}]"


def format_data_fill_formula(terms: list[DataFillTerm] | tuple[DataFillTerm, ...]) -> str:
    parts: list[str] = []
    for index, term in enumerate(terms):
        coefficient = term.coefficient
        sign = "-" if coefficient < 0 else "+"
        absolute = abs(coefficient)
        body = format_quantity_ref(term.ref)
        if absolute != 1:
            body = f"{absolute}*{body}"
        if index == 0:
            parts.append(f"-{body}" if coefficient < 0 else body)
        else:
            parts.append(f" {sign} {body}")
    return "".join(parts)


def validate_formula_references(
    terms: list[DataFillTerm] | tuple[DataFillTerm, ...],
    racking_names: set[str],
    product_names: set[str],
) -> None:
    for term in terms:
        ref = term.ref
        if ref.kind == "racking" and ref.name not in racking_names:
            raise DataFillFormulaError("formula_unknown_reference", token=format_quantity_ref(ref))
        if ref.kind == "product" and ref.name not in product_names:
            raise DataFillFormulaError("formula_unknown_reference", token=format_quantity_ref(ref))

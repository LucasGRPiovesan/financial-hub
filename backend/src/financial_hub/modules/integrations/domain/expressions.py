"""The mapping language of ``manifest.v1`` (ADR-0008): declarative, deterministic, bounded.

A **selector** is a JSON pointer string (RFC 6901, ``"/order/id"``; ``""`` is the whole document)
or one expression object from a closed whitelist:

- ``{"op": "literal", "value": ...}``: a string, an integer, a boolean or ``null``;
- ``{"op": "pointer", "pointer": "/a/0/b"}``: the same as the bare pointer string;
- ``{"op": "multiply", "of": <selector>, "by": "<decimal>"}``: a decimal times a constant;
- ``{"op": "coalesce", "of": [<selector>, ...]}``: the first operand present and not ``null``;
- ``{"op": "to_decimal", "of": <selector>}``: an integer or a decimal string, as a decimal.

There is no eval, no regular expression over input, no loop and no recursion over the data:
evaluation visits each node of the expression once, and an expression has at most
``MAX_DEPTH`` levels and ``MAX_NODES`` nodes, so its cost is bounded by the expression itself.
Numbers are ``int`` or ``Decimal`` (never float) with at most ``DECIMAL_INTEGER_DIGITS`` integer
digits and ``DECIMAL_SCALE`` decimals, the precision metering stores.
"""

import re
from decimal import Context, Decimal, InvalidOperation
from typing import Annotated, Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

MAX_DEPTH: Final = 4
MAX_NODES: Final = 16
MAX_COALESCE: Final = 8
MAX_POINTER_LENGTH: Final = 256
MAX_POINTER_TOKENS: Final = 16
MAX_LITERAL_LENGTH: Final = 256
DECIMAL_INTEGER_DIGITS: Final = 18
DECIMAL_SCALE: Final = 9
MAX_INTEGER: Final = 10**DECIMAL_INTEGER_DIGITS - 1

# Both patterns have no nested quantifier (linear time); input is also length-checked first.
POINTER_PATTERN: Final = r"^(/([^~/]|~[01])*)*$"
DECIMAL_PATTERN: Final = r"^-?(0|[1-9][0-9]{0,17})(\.[0-9]{1,9})?$"
_DECIMAL_RE: Final = re.compile(DECIMAL_PATTERN)
_MAX_DECIMAL_TEXT: Final = 1 + DECIMAL_INTEGER_DIGITS + 1 + DECIMAL_SCALE
_INTEGER_LIMIT: Final = Decimal(10) ** DECIMAL_INTEGER_DIGITS
_CONTEXT: Final = Context(prec=2 * (DECIMAL_INTEGER_DIGITS + DECIMAL_SCALE) + 2)

Pointer = Annotated[
    str,
    Field(
        max_length=MAX_POINTER_LENGTH,
        pattern=POINTER_PATTERN,
        description='A JSON pointer (RFC 6901) into the business event payload, e.g. "/order/id".',
    ),
]
DecimalText = Annotated[
    str,
    Field(
        max_length=_MAX_DECIMAL_TEXT,
        pattern=DECIMAL_PATTERN,
        description="A decimal as a string (at most 18 integer digits and 9 decimals).",
    ),
]


class EvaluationError(ValueError):
    """The payload does not give the selector what it needs (a type or a bound)."""


class _Missing:
    """A pointer that names nothing in the document (distinct from a JSON ``null``)."""

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"


MISSING: Final = _Missing()


class _Expression(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class LiteralExpression(_Expression):
    """A constant."""

    op: Literal["literal"]
    value: (
        Annotated[str, Field(max_length=MAX_LITERAL_LENGTH)]
        | Annotated[int, Field(ge=-MAX_INTEGER, le=MAX_INTEGER)]
        | bool
        | None
    )


class PointerExpression(_Expression):
    """The value at ``pointer`` (absent when the payload has nothing there)."""

    op: Literal["pointer"]
    pointer: Pointer


class MultiplyExpression(_Expression):
    """``of`` (an integer or a decimal string) times the constant ``by``, exactly."""

    op: Literal["multiply"]
    of: Selector
    by: DecimalText


class CoalesceExpression(_Expression):
    """The first operand that is present and not ``null``; absent when none is."""

    op: Literal["coalesce"]
    of: list[Selector] = Field(min_length=1, max_length=MAX_COALESCE)


class ToDecimalExpression(_Expression):
    """``of`` (an integer or a decimal string) as a decimal."""

    op: Literal["to_decimal"]
    of: Selector


type Expression = Annotated[
    LiteralExpression
    | PointerExpression
    | MultiplyExpression
    | CoalesceExpression
    | ToDecimalExpression,
    Field(discriminator="op"),
]
type Selector = Pointer | Expression


def check_bounds(selector: Selector) -> None:
    """``ValueError`` when the expression is deeper than ``MAX_DEPTH`` or has too many nodes."""
    nodes = 0
    pending: list[tuple[Selector, int]] = [(selector, 1)]
    while pending:  # bounded: every node is visited once and the walk stops at MAX_NODES
        current, depth = pending.pop()
        nodes += 1
        if depth > MAX_DEPTH:
            raise ValueError(f"expressions nest at most {MAX_DEPTH} levels")
        if nodes > MAX_NODES:
            raise ValueError(f"expressions have at most {MAX_NODES} nodes")
        if isinstance(current, str):
            if current.count("/") > MAX_POINTER_TOKENS:
                raise ValueError(f"pointers have at most {MAX_POINTER_TOKENS} tokens")
        elif isinstance(current, PointerExpression):
            if current.pointer.count("/") > MAX_POINTER_TOKENS:
                raise ValueError(f"pointers have at most {MAX_POINTER_TOKENS} tokens")
        elif isinstance(current, MultiplyExpression | ToDecimalExpression):
            pending.append((current.of, depth + 1))
        elif isinstance(current, CoalesceExpression):
            pending.extend((operand, depth + 1) for operand in current.of)


def pointers_of(selector: Selector) -> list[str]:
    """Every JSON pointer the selector reads (used to redact personal data before storage)."""
    found: list[str] = []
    pending: list[Selector] = [selector]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            found.append(current)
        elif isinstance(current, PointerExpression):
            found.append(current.pointer)
        elif isinstance(current, MultiplyExpression | ToDecimalExpression):
            pending.append(current.of)
        elif isinstance(current, CoalesceExpression):
            pending.extend(current.of)
    return found


def pointer_tokens(pointer: str) -> list[str]:
    """The unescaped reference tokens of a pointer (``~1`` is ``/``, ``~0`` is ``~``)."""
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("a JSON pointer starts with '/'")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def _array_index(token: str, length: int) -> int | None:
    if not (token.isascii() and token.isdigit()) or len(token) > 9:
        return None
    if len(token) > 1 and token.startswith("0"):
        return None
    index = int(token)
    return index if index < length else None


def resolve_pointer(document: Any, pointer: str) -> Any:
    """The value at ``pointer`` in ``document``, or :data:`MISSING`."""
    current: object = document
    for token in pointer_tokens(pointer):
        if isinstance(current, dict):
            members = cast(dict[str, object], current)
            if token not in members:
                return MISSING
            current = members[token]
        elif isinstance(current, list):
            items = cast(list[object], current)
            index = _array_index(token, len(items))
            if index is None:
                return MISSING
            current = items[index]
        else:
            return MISSING
    return current


def as_decimal(value: object) -> Decimal:
    """An integer or a decimal string (never a float or a boolean) as a bounded ``Decimal``."""
    if isinstance(value, bool):
        raise EvaluationError("expected an integer or a decimal string, got a boolean")
    if isinstance(value, int):
        if abs(value) > MAX_INTEGER:
            raise EvaluationError(f"integers have at most {DECIMAL_INTEGER_DIGITS} digits")
        return Decimal(value)
    if isinstance(value, Decimal):
        return _bounded(value)
    if isinstance(value, str):
        if len(value) > _MAX_DECIMAL_TEXT or _DECIMAL_RE.fullmatch(value) is None:
            raise EvaluationError(
                "expected a decimal string with at most 18 integer digits and 9 decimals"
            )
        return Decimal(value)
    if isinstance(value, float):
        raise EvaluationError("floats are not accepted: send an integer or a decimal string")
    raise EvaluationError(f"expected an integer or a decimal string, got {type(value).__name__}")


def _bounded(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise EvaluationError("decimals must be finite")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -DECIMAL_SCALE:
        normalized = value.normalize(_CONTEXT)
        exponent = normalized.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -DECIMAL_SCALE:
            raise EvaluationError(f"decimals have at most {DECIMAL_SCALE} decimal places")
        value = normalized
    if value.copy_abs() >= _INTEGER_LIMIT:
        raise EvaluationError(f"decimals have at most {DECIMAL_INTEGER_DIGITS} integer digits")
    return value


def plain_decimal(value: Decimal) -> str:
    """Fixed-point text without trailing zeros (``Decimal('1.50')`` -> ``"1.5"``)."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def evaluate(selector: Selector, document: Any) -> Any:
    """The selector's value over ``document``: JSON values, ``Decimal`` or :data:`MISSING`.

    Raises :class:`EvaluationError` when an operand has the wrong type or exceeds a bound.
    """
    if isinstance(selector, str):
        return resolve_pointer(document, selector)
    if isinstance(selector, LiteralExpression):
        return selector.value
    if isinstance(selector, PointerExpression):
        return resolve_pointer(document, selector.pointer)
    if isinstance(selector, CoalesceExpression):
        for operand in selector.of:
            value = evaluate(operand, document)
            if value is not MISSING and value is not None:
                return value
        return MISSING
    operand = evaluate(selector.of, document)
    if operand is MISSING or operand is None:
        return operand
    if isinstance(selector, ToDecimalExpression):
        return as_decimal(operand)
    try:
        product = _CONTEXT.multiply(as_decimal(operand), Decimal(selector.by))
    except InvalidOperation:  # pragma: no cover - both operands are bounded
        raise EvaluationError("the product is not a valid decimal") from None
    return _bounded(product)


for _model in (MultiplyExpression, CoalesceExpression, ToDecimalExpression):
    _model.model_rebuild()

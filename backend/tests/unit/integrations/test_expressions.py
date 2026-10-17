"""The mapping language: whitelisted operators, exact decimals, bounded cost (property tests)."""

from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from financial_hub.modules.integrations.domain import (
    MISSING,
    EvaluationError,
    Selector,
    as_decimal,
    check_bounds,
    evaluate,
    plain_decimal,
    pointers_of,
)
from financial_hub.modules.integrations.domain.expressions import MAX_DEPTH, MAX_NODES

SELECTOR: TypeAdapter[Any] = TypeAdapter(Selector)


def parse(raw: Any) -> Any:
    selector = SELECTOR.validate_python(raw, strict=True)
    check_bounds(selector)
    return selector


DOCUMENT = {"a": {"b": [10, "2.5", None]}, "x~y": 1, "s/t": 2, "f": True}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", DOCUMENT),
        ("/a/b/0", 10),
        ("/a/b/1", "2.5"),
        ("/a/b/9", MISSING),
        ("/x~0y", 1),
        ("/s~1t", 2),
        ("/nope", MISSING),
        ({"op": "pointer", "pointer": "/a/b/0"}, 10),
        ({"op": "literal", "value": "fixed"}, "fixed"),
        ({"op": "literal", "value": None}, None),
        ({"op": "coalesce", "of": ["/nope", "/a/b/2", "/a/b/1"]}, "2.5"),
        ({"op": "coalesce", "of": ["/nope"]}, MISSING),
        ({"op": "to_decimal", "of": "/a/b/1"}, Decimal("2.5")),
        ({"op": "multiply", "of": "/a/b/1", "by": "0.4"}, Decimal("1.00")),
        ({"op": "multiply", "of": "/nope", "by": "2"}, MISSING),
    ],
)
def test_evaluate(raw: Any, expected: Any) -> None:
    assert evaluate(parse(raw), DOCUMENT) == expected


@pytest.mark.parametrize(
    "raw",
    [
        {"op": "eval", "of": "/a"},
        {"op": "regex", "of": "/a", "pattern": "(a+)+$"},
        {"op": "literal", "value": 1.5},
        {"op": "multiply", "of": "/a", "by": 2},
        {"op": "multiply", "of": "/a", "by": "1e3"},
        {"op": "coalesce", "of": []},
        {"op": "pointer", "pointer": "no-slash"},
        "/bad~2escape",
    ],
)
def test_only_whitelisted_operators_and_exact_operands(raw: Any) -> None:
    with pytest.raises(ValidationError):
        parse(raw)


def nested(depth: int) -> Any:
    raw: Any = "/a"
    for _ in range(depth):
        raw = {"op": "to_decimal", "of": raw}
    return raw


def test_depth_and_size_are_bounded() -> None:
    parse(nested(MAX_DEPTH - 1))  # the leaf pointer is a level too
    with pytest.raises(ValueError, match="nest"):
        parse(nested(MAX_DEPTH))
    wide = {"op": "coalesce", "of": [{"op": "coalesce", "of": ["/a"] * 8}] * 8}
    with pytest.raises(ValueError, match=f"{MAX_NODES}|nodes"):
        parse(wide)


@pytest.mark.parametrize("value", [1.5, True, [1], {"a": 1}, "1e3", "NaN", "1." + "0" * 10])
def test_as_decimal_refuses_floats_and_non_numbers(value: Any) -> None:
    with pytest.raises(EvaluationError):
        as_decimal(value)


def test_evaluation_errors_on_wrong_operand_types() -> None:
    with pytest.raises(EvaluationError):
        evaluate(parse({"op": "to_decimal", "of": "/f"}), DOCUMENT)
    with pytest.raises(EvaluationError):
        evaluate(parse({"op": "multiply", "of": "/a", "by": "2"}), DOCUMENT)


decimals = st.decimals(
    min_value=Decimal("-999999999"), max_value=Decimal("999999999"), places=4, allow_nan=False
)
factors = st.decimals(min_value=Decimal("-1000"), max_value=Decimal("1000"), places=4)


@given(value=decimals, factor=factors)
def test_multiply_is_exact(value: Decimal, factor: Decimal) -> None:
    result = evaluate(
        parse({"op": "multiply", "of": "/v", "by": plain_decimal(factor)}),
        {"v": plain_decimal(value)},
    )
    assert isinstance(result, Decimal)
    assert result == value * factor


@given(value=decimals)
def test_to_decimal_round_trips_through_plain_text(value: Decimal) -> None:
    text = plain_decimal(value)
    assert as_decimal(text) == value
    assert "E" not in text and "e" not in text


@given(st.integers(min_value=-(10**18) + 1, max_value=10**18 - 1))
def test_integers_within_bounds_are_exact(value: int) -> None:
    assert evaluate(parse({"op": "to_decimal", "of": "/v"}), {"v": value}) == Decimal(value)


@given(st.integers(min_value=10**18, max_value=10**30))
def test_integers_out_of_bounds_are_refused(value: int) -> None:
    with pytest.raises(EvaluationError):
        evaluate(parse({"op": "to_decimal", "of": "/v"}), {"v": value})


json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-1000, 1000) | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(min_size=1, max_size=4), children, max_size=4)
    ),
    max_leaves=20,
)


@given(document=json_values, pointer=st.lists(st.text(max_size=4), max_size=5))
def test_pointer_never_raises_and_is_deterministic(document: Any, pointer: list[str]) -> None:
    text = "".join("/" + token.replace("~", "~0").replace("/", "~1") for token in pointer)
    selector = parse(text)
    assert evaluate(selector, document) == evaluate(selector, document)
    assert pointers_of(selector) == [text]


@given(document=json_values)
def test_coalesce_returns_the_first_present_operand(document: Any) -> None:
    selector = parse({"op": "coalesce", "of": ["/missing-key", {"op": "literal", "value": 7}]})
    expected = 7
    if isinstance(document, dict) and document.get("missing-key") is not None:
        expected = document["missing-key"]
    assert evaluate(selector, document) == expected

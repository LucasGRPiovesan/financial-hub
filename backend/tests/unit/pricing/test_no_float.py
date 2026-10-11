"""ADR-0002: no ``float`` anywhere in the pricing module (calls, annotations or literals)."""

import io
import tokenize
from pathlib import Path

import financial_hub.modules.pricing as pricing

SOURCES = sorted(Path(pricing.__file__).parent.rglob("*.py"))


def float_tokens(source: str) -> list[str]:
    found: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NAME and token.string == "float":
            found.append(f"{token.start[0]}: name 'float'")
        number = token.string.lower()
        if (
            token.type == tokenize.NUMBER
            and not number.startswith("0x")
            and any(mark in number for mark in (".", "e", "j"))
        ):
            found.append(f"{token.start[0]}: literal {token.string}")
    return found


def test_scanner_catches_floats() -> None:
    assert float_tokens("x = float(y)\n")
    assert float_tokens("x = 1.5\n")
    assert float_tokens("x = 1e3\n")
    assert not float_tokens("x = Decimal('1.5')  # float in a comment\n")


def test_pricing_sources_contain_no_float() -> None:
    assert len(SOURCES) >= 8
    offenders = {
        str(path): found for path in SOURCES if (found := float_tokens(path.read_text("utf-8")))
    }
    assert offenders == {}

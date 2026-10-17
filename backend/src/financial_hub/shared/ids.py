"""TypeID-style identifiers: ``<prefix>_<26 chars base32 Crockford of a UUIDv7>`` (ADR-0001).

Encoding follows the TypeID spec (v0.3): the 128-bit UUID is left-padded with two zero bits to 130
bits and written as 26 lowercase Crockford base32 characters, so the first character is ``0``-``7``.
The database stores the native ``uuid``; the prefix is added and checked at the API boundary.
"""

import uuid
from enum import StrEnum
from typing import Final

ALPHABET: Final = "0123456789abcdefghjkmnpqrstvwxyz"
SUFFIX_LENGTH: Final = 26
_DECODE: Final[dict[str, int]] = {char: value for value, char in enumerate(ALPHABET)}


class IdPrefix(StrEnum):
    """Registry of entity prefixes. Adding one is cheap; renaming one breaks every stored id."""

    CUSTOMER = "cus"  # a project-private customer identity (what API callers see)
    FINANCIAL_CUSTOMER = "fcus"  # the universal customer; never leaves the platform
    CUSTOMER_IDENTITY = "cid"
    PROJECT = "prj"
    API_KEY = "key"
    PROVIDER_ACCOUNT = "pva"
    ACCOUNT = "acc"
    JOURNAL_ENTRY = "je"
    POSTING = "pst"
    INVOICE = "inv"
    SUBSCRIPTION = "sub"
    CHARGE = "chg"
    PAYMENT_INTENT = "pi"
    PROVIDER_PAYMENT = "pp"
    REFUND = "rf"
    PROVIDER_EVENT = "pev"
    EVENT = "evt"
    DELIVERY = "dlv"
    WEBHOOK_ENDPOINT = "whe"
    MANIFEST_VERSION = "mfv"
    PAYEE = "pay"
    RECONCILIATION_RUN = "rec"
    RECONCILIATION_BREAK = "rbk"
    RECONCILIATION_ADJUSTMENT = "adj"
    OPERATOR = "opr"
    SESSION = "ses"
    AUDIT_RECORD = "aud"


class InvalidIdError(ValueError):
    """The value is not a well-formed id with the expected prefix."""


def _prefix(prefix: IdPrefix | str) -> IdPrefix:
    try:
        return IdPrefix(prefix)
    except ValueError:
        raise InvalidIdError(f"unregistered id prefix: {prefix!r}") from None


def encode_uuid(value: uuid.UUID) -> str:
    """Encode a UUID as the 26-character TypeID suffix."""
    number = value.int
    chars = [ALPHABET[(number >> (5 * shift)) & 0x1F] for shift in range(SUFFIX_LENGTH - 1, -1, -1)]
    return "".join(chars)


def decode_suffix(suffix: str) -> uuid.UUID:
    """Decode a 26-character TypeID suffix back to its UUID."""
    if len(suffix) != SUFFIX_LENGTH:
        raise InvalidIdError(f"id suffix must have {SUFFIX_LENGTH} characters")
    if suffix[0] > "7":
        raise InvalidIdError("id suffix overflows 128 bits")
    number = 0
    for char in suffix:
        digit = _DECODE.get(char)
        if digit is None:
            raise InvalidIdError(f"invalid character {char!r} in id suffix")
        number = (number << 5) | digit
    return uuid.UUID(int=number)


def format_id(prefix: IdPrefix | str, value: uuid.UUID) -> str:
    """Render a stored UUID with its entity prefix (``inv_01j9...``)."""
    return f"{_prefix(prefix).value}_{encode_uuid(value)}"


def new_uuid() -> uuid.UUID:
    """A new time-ordered UUIDv7 (RFC 9562; monotonic within the process)."""
    return uuid.uuid7()


def new_id(prefix: IdPrefix | str) -> str:
    """A new prefixed id for a registered entity type."""
    return format_id(prefix, new_uuid())


def parse_id(value: str, expected_prefix: IdPrefix | str) -> uuid.UUID:
    """Validate ``value`` against ``expected_prefix`` and return the UUID it encodes."""
    expected = _prefix(expected_prefix)
    if not isinstance(value, str):  # pyright: ignore[reportUnnecessaryIsInstance] - runtime guard
        raise InvalidIdError(f"id must be str, got {type(value).__name__}")
    prefix, separator, suffix = value.rpartition("_")
    if not separator:
        raise InvalidIdError(f"id has no prefix, expected {expected.value}_")
    if prefix != expected.value:
        raise InvalidIdError(f"expected a {expected.value}_ id, got prefix {prefix!r}")
    return decode_suffix(suffix)

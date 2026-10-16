"""CPF/CNPJ documents: validation, normalisation, masking and the keys derived from them.

Pure functions. The plaintext digits exist only inside a request, the sealing step and (for
reveal) the infrastructure vault; everything stored or displayed is derived here:

- ``document_hash``: HMAC-SHA256 under a key derived from the API-key pepper (never a bare hash:
  an 11-digit CPF is trivially enumerable, so the pepper is what makes a database leak useless).
- ``document_aad``: the AES-GCM associated data, binding a ciphertext to its universal customer.
- ``mask_document``: what an API response may show (last two digits).
"""

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from financial_hub.shared.ids import IdPrefix, format_id

CPF_LENGTH: Final = 11
CNPJ_LENGTH: Final = 14
HASH_KEY_CONTEXT: Final = b"financial-hub/customer-document-hash/v1"
_FORMATTING: Final = re.compile(r"[.\-/\s]")
_CNPJ_WEIGHTS: Final = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


class CustomerKind(StrEnum):
    INDIVIDUAL = "individual"
    COMPANY = "company"


@dataclass(frozen=True, slots=True)
class Document:
    """A valid CPF (``individual``) or CNPJ (``company``), as digits only."""

    digits: str

    @property
    def kind(self) -> CustomerKind:
        return CustomerKind.INDIVIDUAL if len(self.digits) == CPF_LENGTH else CustomerKind.COMPANY

    @property
    def last4(self) -> str:
        return self.digits[-4:]

    def __repr__(self) -> str:
        return f"Document(kind={self.kind.value}, last4={self.last4})"


def _cpf_digit(digits: list[int]) -> int:
    weight = len(digits) + 1
    total = sum(digit * (weight - index) for index, digit in enumerate(digits))
    return total * 10 % 11 % 10


def _cnpj_digit(digits: list[int], weights: tuple[int, ...]) -> int:
    remainder = sum(digit * weight for digit, weight in zip(digits, weights, strict=True)) % 11
    return 0 if remainder < 2 else 11 - remainder


def cpf_is_valid(digits: str) -> bool:
    if len(digits) != CPF_LENGTH or len(set(digits)) == 1:
        return False
    numbers = [int(char) for char in digits]
    first = _cpf_digit(numbers[:9])
    return numbers[9] == first and numbers[10] == _cpf_digit([*numbers[:9], first])


def cnpj_is_valid(digits: str) -> bool:
    if len(digits) != CNPJ_LENGTH or len(set(digits)) == 1:
        return False
    numbers = [int(char) for char in digits]
    first = _cnpj_digit(numbers[:12], _CNPJ_WEIGHTS)
    second = _cnpj_digit([*numbers[:12], first], (6, *_CNPJ_WEIGHTS))
    return numbers[12] == first and numbers[13] == second


def parse_document(raw: str) -> Document:
    """Normalise to digits and check length and check digits; ``ValueError`` otherwise.

    The message never repeats the input.
    """
    digits = _FORMATTING.sub("", raw)
    if not digits.isascii() or not digits.isdigit():
        raise ValueError("document must contain only digits (CPF or CNPJ)")
    if len(digits) == CPF_LENGTH:
        if not cpf_is_valid(digits):
            raise ValueError("invalid CPF check digits")
    elif len(digits) == CNPJ_LENGTH:
        if not cnpj_is_valid(digits):
            raise ValueError("invalid CNPJ check digits")
    else:
        raise ValueError("document must have 11 (CPF) or 14 (CNPJ) digits")
    return Document(digits)


def mask_document(kind: CustomerKind, last4: str) -> str:
    """``***.***.***-34`` (CPF) or ``**.***.***/****-34`` (CNPJ): only the last two digits."""
    tail = last4[-2:]
    return f"***.***.***-{tail}" if kind is CustomerKind.INDIVIDUAL else f"**.***.***/****-{tail}"


def derive_hash_key(pepper: bytes) -> bytes:
    """Domain-separated subkey of the API-key pepper, so the two uses never share a raw key."""
    return hmac.new(pepper, HASH_KEY_CONTEXT, hashlib.sha256).digest()


def document_hash(hash_key: bytes, document: Document) -> bytes:
    return hmac.new(hash_key, document.digits.encode("ascii"), hashlib.sha256).digest()


def document_aad(financial_customer_id: UUID) -> bytes:
    """AES-GCM associated data ``customer|<fcus id>``: a ciphertext copied elsewhere never opens."""
    return f"customer|{format_id(IdPrefix.FINANCIAL_CUSTOMER, financial_customer_id)}".encode()

from uuid import UUID

import pytest

from financial_hub.modules.customers.domain import (
    CustomerKind,
    cnpj_is_valid,
    cpf_is_valid,
    derive_hash_key,
    document_aad,
    document_hash,
    mask_document,
    parse_document,
)
from tests.unit.customers.generators import (
    format_cnpj,
    format_cpf,
    make_cnpj,
    make_cpf,
    with_bad_check_digit,
)

KNOWN_CPF = "52998224725"  # a published, structurally valid test CPF
KNOWN_CNPJ = "11222333000181"  # a published, structurally valid test CNPJ


def test_known_valid_numbers() -> None:
    assert cpf_is_valid(KNOWN_CPF)
    assert cnpj_is_valid(KNOWN_CNPJ)


@pytest.mark.parametrize("seed", range(25))
def test_generated_numbers_validate(seed: int) -> None:
    assert parse_document(make_cpf(seed)).kind is CustomerKind.INDIVIDUAL
    assert parse_document(make_cnpj(seed)).kind is CustomerKind.COMPANY


def test_formatted_input_is_normalised_to_digits() -> None:
    cpf, cnpj = make_cpf(1), make_cnpj(1)

    assert parse_document(format_cpf(cpf)).digits == cpf
    assert parse_document(f"  {format_cnpj(cnpj)} ").digits == cnpj


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (with_bad_check_digit(make_cpf(2)), "invalid CPF"),
        (with_bad_check_digit(make_cnpj(2)), "invalid CNPJ"),
        ("11111111111", "invalid CPF"),
        ("00000000000000", "invalid CNPJ"),
        ("1234567890", "11 .* or 14"),
        ("123456789012", "11 .* or 14"),
        ("5299822472a", "only digits"),
        ("", "only digits"),
        ("٥٢٩٩٨٢٢٤٧٢٥", "only digits"),  # non-ASCII digits are not accepted
    ],
)
def test_invalid_documents_are_rejected_without_echoing_them(value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message) as raised:
        parse_document(value)

    assert value.strip() == "" or value not in str(raised.value)


def test_masking_shows_only_the_last_two_digits() -> None:
    assert mask_document(CustomerKind.INDIVIDUAL, "4725") == "***.***.***-25"
    assert mask_document(CustomerKind.COMPANY, "0181") == "**.***.***/****-81"


def test_repr_never_contains_the_number() -> None:
    document = parse_document(KNOWN_CPF)

    assert KNOWN_CPF not in repr(document)
    assert document.last4 == "4725"


def test_hash_is_keyed_deterministic_and_normalisation_independent() -> None:
    key = derive_hash_key(b"fake-pepper-one")
    other = derive_hash_key(b"fake-pepper-two")
    plain, pretty = parse_document(KNOWN_CPF), parse_document(format_cpf(KNOWN_CPF))

    assert document_hash(key, plain) == document_hash(key, pretty)
    assert len(document_hash(key, plain)) == 32
    assert document_hash(key, plain) != document_hash(other, plain)
    assert document_hash(key, plain) != document_hash(key, parse_document(make_cpf(9)))
    assert key != b"fake-pepper-one"


def test_aad_binds_the_universal_customer() -> None:
    first, second = UUID(int=1), UUID(int=2)

    assert document_aad(first).startswith(b"customer|fcus_")
    assert document_aad(first) != document_aad(second)

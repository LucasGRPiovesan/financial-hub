import uuid

import pytest

from financial_hub.shared.ids import (
    ALPHABET,
    SUFFIX_LENGTH,
    IdPrefix,
    InvalidIdError,
    decode_suffix,
    encode_uuid,
    format_id,
    new_id,
    new_uuid,
    parse_id,
)

EXPECTED_PREFIXES = {
    "cus", "cid", "prj", "key", "acc", "je", "pst", "inv", "chg", "pi", "pp", "pev", "evt", "dlv",
    "whe", "mfv", "pay", "rec", "opr", "ses", "aud",
}  # fmt: skip


def test_registry_has_every_prefix_from_the_spec() -> None:
    assert {prefix.value for prefix in IdPrefix} == EXPECTED_PREFIXES


@pytest.mark.parametrize("prefix", list(IdPrefix))
def test_round_trip_for_every_prefix(prefix: IdPrefix) -> None:
    value = new_uuid()
    rendered = format_id(prefix, value)

    assert rendered.startswith(f"{prefix.value}_")
    assert parse_id(rendered, prefix) == value
    assert parse_id(rendered, prefix.value) == value


def test_new_id_shape() -> None:
    rendered = new_id(IdPrefix.INVOICE)
    prefix, _, suffix = rendered.partition("_")

    assert prefix == "inv"
    assert len(suffix) == SUFFIX_LENGTH
    assert set(suffix) <= set(ALPHABET)
    assert suffix[0] <= "7"


def test_new_uuid_is_version_7() -> None:
    assert new_uuid().version == 7


def test_ids_are_time_ordered() -> None:
    ids = [new_id(IdPrefix.EVENT) for _ in range(500)]

    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_matches_typeid_spec_vectors() -> None:
    # Test vectors from the TypeID spec (jetify-com/typeid, spec/valid.yml).
    assert encode_uuid(uuid.UUID(int=0)) == "0" * 26
    assert encode_uuid(uuid.UUID(int=1)) == "0" * 25 + "1"
    assert encode_uuid(uuid.UUID(int=2**128 - 1)) == "7zzzzzzzzzzzzzzzzzzzzzzzzz"
    assert (
        encode_uuid(uuid.UUID("01890a5d-ac96-774b-bcce-b302099a8057"))
        == "01h455vb4pex5vsknk084sn02q"
    )
    assert decode_suffix("7zzzzzzzzzzzzzzzzzzzzzzzzz") == uuid.UUID(int=2**128 - 1)


def test_prefix_mismatch_raises() -> None:
    rendered = new_id(IdPrefix.CUSTOMER)

    with pytest.raises(InvalidIdError, match="expected a inv_ id"):
        parse_id(rendered, IdPrefix.INVOICE)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("01h455vb4pex5vsknk084sn02q", "no prefix"),
        ("inv_01h455vb4pex5vsknk084sn02", "26 characters"),
        ("inv_01h455vb4pex5vsknk084sn02qq", "26 characters"),
        ("inv_81h455vb4pex5vsknk084sn02q", "overflows"),
        ("inv_01h455vb4pex5vsknk084sn0iq", "invalid character"),
        ("inv_01H455VB4PEX5VSKNK084SN02Q", "invalid character"),
        ("inv_", "26 characters"),
        ("INV_01h455vb4pex5vsknk084sn02q", "prefix"),
    ],
)
def test_malformed_ids_raise(value: str, message: str) -> None:
    with pytest.raises(InvalidIdError, match=message):
        parse_id(value, IdPrefix.INVOICE)


def test_non_str_id_raises() -> None:
    with pytest.raises(InvalidIdError):
        parse_id(123, IdPrefix.INVOICE)  # pyright: ignore[reportArgumentType]


def test_unregistered_prefix_raises() -> None:
    with pytest.raises(InvalidIdError, match="unregistered"):
        new_id("foo")
    with pytest.raises(InvalidIdError, match="unregistered"):
        parse_id("foo_01h455vb4pex5vsknk084sn02q", "foo")


def test_invalid_id_error_is_value_error() -> None:
    assert issubclass(InvalidIdError, ValueError)

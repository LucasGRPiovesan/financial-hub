import pytest

from financial_hub.modules.customers.application.api import customer_account_owner
from financial_hub.modules.customers.domain import (
    normalize_email,
    normalize_phone,
    validate_external_ref,
    validate_metadata,
    validate_name,
)
from financial_hub.shared.ids import new_uuid


@pytest.mark.parametrize("ref", ["a", "user-1", "u_1.2:3@x+y=z~", "A" * 128])
def test_valid_external_refs(ref: str) -> None:
    assert validate_external_ref(ref) == ref


@pytest.mark.parametrize("ref", ["", " a", "-a", "a b", "a/b", "A" * 129, "á"])
def test_invalid_external_refs(ref: str) -> None:
    with pytest.raises(ValueError, match="external_ref"):
        validate_external_ref(ref)


def test_name_is_stripped_and_bounded() -> None:
    assert validate_name("  Maria  ") == "Maria"
    for bad in ("", "   ", "x" * 201):
        with pytest.raises(ValueError, match="name"):
            validate_name(bad)


def test_email_is_lowercased_and_checked() -> None:
    assert normalize_email(" Maria@Example.COM ") == "maria@example.com"
    for bad in ("nope", "a@b", "a b@c.d", "x" * 250 + "@a.bc"):
        with pytest.raises(ValueError, match="email"):
            normalize_email(bad)


def test_phone_is_normalised() -> None:
    assert normalize_phone("+55 (11) 91234-5678") == "+5511912345678"
    for bad in ("123", "abc12345678", "1" * 16, "++5511912345678"):
        with pytest.raises(ValueError, match="phone"):
            normalize_phone(bad)


def test_metadata_bounds() -> None:
    assert validate_metadata({"plan": "pro", "n": 3}) == {"plan": "pro", "n": 3}
    with pytest.raises(ValueError, match="at most 50 keys"):
        validate_metadata({f"k{i}": 1 for i in range(51)})
    with pytest.raises(ValueError, match="keys must have"):
        validate_metadata({"": 1})
    with pytest.raises(ValueError, match="keys must have"):
        validate_metadata({"k" * 41: 1})
    with pytest.raises(ValueError, match="at most 4096 bytes"):
        validate_metadata({"a": "x" * 4100})
    with pytest.raises(ValueError, match="serialisable"):
        validate_metadata({"a": object()})


def test_two_projects_identities_of_the_same_person_are_distinct_account_owners() -> None:
    # The universal customer id is not even an input: ownership is per project identity.
    project_a, project_b = new_uuid(), new_uuid()
    identity_a, identity_b = new_uuid(), new_uuid()

    owner_a = customer_account_owner(project_a, identity_a)
    owner_b = customer_account_owner(project_b, identity_b)

    assert owner_a != owner_b
    assert owner_a.owner_id != owner_b.owner_id
    assert owner_a.owner_id.startswith("cus_")
    assert customer_account_owner(project_a, identity_a) == owner_a
    # The same identity id under another project is still another owner (project is part of it).
    assert customer_account_owner(project_b, identity_a) != owner_a

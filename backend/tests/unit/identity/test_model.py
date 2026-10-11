from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from financial_hub.modules.identity.domain import (
    Operator,
    OperatorStatus,
    PasswordPolicyError,
    Session,
    SessionPolicy,
    is_valid_email,
    mask_email,
    normalize_email,
    validate_display_name,
    validate_new_password,
)

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
POLICY = SessionPolicy(
    idle_timeout=timedelta(minutes=30),
    absolute_timeout=timedelta(hours=12),
    touch_interval=timedelta(seconds=60),
)


def session(**changes: object) -> Session:
    idle, absolute = POLICY.expiries(T0)
    values: dict[str, object] = {
        "id": b"\x00" * 32,
        "operator_id": uuid4(),
        "csrf_token_hash": b"\x01" * 32,
        "created_at": T0,
        "last_seen_at": T0,
        "idle_expires_at": idle,
        "absolute_expires_at": absolute,
        "revoked_at": None,
    }
    values.update(changes)
    return Session(**values)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("length", [12, 64, 128])
def test_password_lengths_within_policy_pass(length: int) -> None:
    assert validate_new_password("x" * length) == "x" * length


@pytest.mark.parametrize("length", [0, 11, 129])
def test_password_lengths_outside_policy_fail(length: int) -> None:
    with pytest.raises(PasswordPolicyError, match="12 to 128"):
        validate_new_password("x" * length)


def test_emails_are_normalized_and_loosely_validated() -> None:
    assert normalize_email("  Ana@Example.COM ") == "ana@example.com"
    assert is_valid_email("ana@example.com")
    for bad in ("ana", "ana@example", "a b@example.com", "a@@example.com", "a" * 250 + "@x.io"):
        assert not is_valid_email(bad)


def test_mask_email_keeps_only_the_first_character_and_domain() -> None:
    assert mask_email("ana@example.com") == "a***@example.com"
    assert mask_email("no-at-sign") == "***"


def test_display_name_is_trimmed_and_bounded() -> None:
    assert validate_display_name("  Ana  ") == "Ana"
    for bad in ("", "   ", "x" * 101):
        with pytest.raises(ValueError, match="display name"):
            validate_display_name(bad)


def test_expiries_cap_idle_by_absolute() -> None:
    assert POLICY.expiries(T0) == (T0 + timedelta(minutes=30), T0 + timedelta(hours=12))
    short = SessionPolicy(timedelta(hours=2), timedelta(hours=1), timedelta(0))
    assert short.expiries(T0) == (T0 + timedelta(hours=1), T0 + timedelta(hours=1))


def test_session_activity_window() -> None:
    record = session()
    assert record.is_active(T0)
    assert record.is_active(T0 + timedelta(minutes=29, seconds=59))
    assert not record.is_active(T0 + timedelta(minutes=30))
    assert not session(revoked_at=T0).is_active(T0)
    late = session(idle_expires_at=T0 + timedelta(hours=13))
    assert not late.is_active(T0 + timedelta(hours=12))


def test_touch_is_throttled_and_capped_by_absolute_expiry() -> None:
    record = session()
    assert record.touch(T0 + timedelta(seconds=59), POLICY) is None
    assert record.touch(T0 + timedelta(seconds=60), POLICY) == T0 + timedelta(minutes=31)
    near_end = T0 + timedelta(hours=11, minutes=50)
    assert record.touch(near_end, POLICY) == T0 + timedelta(hours=12)


def test_operator_lock() -> None:
    operator = Operator(
        id=uuid4(),
        email="a@example.com",
        display_name="A",
        password_hash="$argon2id$...",
        status=OperatorStatus.ACTIVE,
        failed_login_count=5,
        locked_until=T0 + timedelta(minutes=15),
        password_changed_at=T0,
        created_at=T0,
    )
    assert operator.is_locked(T0)
    assert not operator.is_locked(T0 + timedelta(minutes=15))

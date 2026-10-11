from datetime import UTC, datetime, timedelta

from financial_hub.modules.identity.domain import (
    ThrottlePolicy,
    ThrottleScope,
    ThrottleState,
    throttle_key,
)

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
POLICY = ThrottlePolicy(limit=3, window=timedelta(minutes=15), block=timedelta(minutes=10))


def record(state: ThrottleState, times: int, at: datetime) -> ThrottleState:
    for _ in range(times):
        state = state.record(at, POLICY)
    return state


def test_blocks_when_the_limit_is_reached_inside_the_window() -> None:
    state = record(ThrottleState.fresh(T0), 2, T0)
    assert state.blocked_until is None
    assert state.retry_after(T0) is None

    state = state.record(T0 + timedelta(minutes=1), POLICY)
    assert state.blocked_until == T0 + timedelta(minutes=11)
    assert state.retry_after(T0 + timedelta(minutes=1)) == 600
    assert state.retry_after(T0 + timedelta(minutes=10, seconds=59.5)) == 1  # rounded up
    assert state.retry_after(T0 + timedelta(minutes=11)) is None


def test_a_new_window_restarts_the_count() -> None:
    state = record(ThrottleState.fresh(T0), 2, T0)
    later = T0 + timedelta(minutes=15)
    state = state.record(later, POLICY)
    assert (state.window_started_at, state.attempts, state.blocked_until) == (later, 1, None)


def test_recording_after_a_block_starts_over() -> None:
    blocked = record(ThrottleState.fresh(T0), 3, T0)
    after = T0 + timedelta(minutes=20)
    state = blocked.record(after, POLICY)
    assert (state.attempts, state.blocked_until) == (1, None)


def test_keys_are_scoped_digests() -> None:
    email = throttle_key(ThrottleScope.EMAIL, "a@example.com")
    assert len(email) == 32
    assert b"example" not in email
    assert email != throttle_key(ThrottleScope.IP, "a@example.com")
    assert email == throttle_key(ThrottleScope.EMAIL, "a@example.com")

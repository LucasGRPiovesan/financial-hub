"""Split rounding (FINANCIAL_MODEL s4 R3/R8/R9): parts sum to the total, never negative."""

from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from financial_hub.modules.ledger.domain.accounts import PayeeId, ProviderAccountId
from financial_hub.modules.ledger.domain.model import Direction
from financial_hub.modules.ledger.domain.recipes import (
    DisputeOpened,
    RecipeContext,
    RefundRequested,
    SplitAllocation,
    SplitRule,
    SplitShare,
    build_r8_internal,
    build_r9_opened,
)
from financial_hub.shared.money import BRL, Money
from tests.integration.ledger.support import AT

CTX = RecipeContext(UUID(int=100), AT)
PA = ProviderAccountId(UUID(int=2))

weights = st.integers(min_value=0, max_value=10_000)
amounts = st.integers(min_value=0, max_value=10**9)


@st.composite
def split_rules(draw: st.DrawFn) -> SplitRule:
    payee_weights = draw(st.lists(weights, min_size=0, max_size=5))
    platform = draw(weights)
    if sum(payee_weights) + platform == 0:
        platform = 1
    return SplitRule(
        tuple(SplitShare(PayeeId(UUID(int=i + 1)), w) for i, w in enumerate(payee_weights)),
        platform,
    )


@st.composite
def allocations(draw: st.DrawFn) -> SplitAllocation:
    rule = draw(split_rules())
    return rule.allocate(Money(draw(st.integers(min_value=1, max_value=10**7)), BRL))


def parts(allocation: SplitAllocation) -> list[int]:
    return allocation.weights()


@given(split_rules(), amounts)
def test_split_parts_sum_to_basis_and_are_never_negative(rule: SplitRule, units: int) -> None:
    allocation = rule.allocate(Money(units, BRL))
    assert allocation.total == Money(units, BRL)
    assert all(part >= 0 for part in parts(allocation))
    # A zero weight never receives anything.
    for share, payee in zip(rule.payees, allocation.payees, strict=True):
        if share.weight == 0:
            assert payee.amount.is_zero()


@given(allocations(), amounts)
def test_reallocation_sums_and_is_never_negative(original: SplitAllocation, units: int) -> None:
    again = original.reallocate(Money(units, BRL))
    assert again.total == Money(units, BRL)
    assert all(part >= 0 for part in parts(again))
    # Reallocating the original total reproduces the original parts exactly.
    assert original.reallocate(original.total) == original


@given(allocations(), st.lists(st.integers(min_value=1, max_value=10**6), max_size=6))
def test_cumulative_refund_differences_never_drift(
    original: SplitAllocation, refunds: list[int]
) -> None:
    """Per-refund differences of cumulative allocations add up to the cumulative allocation."""
    gross = original.total.minor_units
    refunded = 0
    totals = [0] * len(parts(original))
    for refund in refunds:
        amount = min(refund, gross - refunded)
        if amount == 0:
            break
        before = parts(original.reallocate(Money(refunded, BRL)))
        refunded += amount
        after = parts(original.reallocate(Money(refunded, BRL)))
        diffs = [a - b for a, b in zip(after, before, strict=True)]
        assert sum(diffs) == amount
        totals = [t + d for t, d in zip(totals, diffs, strict=True)]
    assert totals == parts(original.reallocate(Money(refunded, BRL)))
    if refunded == gross:
        assert totals == parts(original)


@given(
    allocations(),
    st.integers(min_value=1, max_value=10**6),
    st.integers(min_value=0, max_value=10**6),
    st.booleans(),
)
def test_r8_internal_entry_is_always_balanced(
    original: SplitAllocation, amount: int, already: int, settled: bool
) -> None:
    gross = original.total.minor_units
    amount = min(amount, gross)
    already = min(already, gross - amount)
    request = RefundRequested(
        refund_id=UUID(int=9),
        payment_id=UUID(int=8),
        provider_account_id=PA,
        amount=Money(amount, BRL),
        gross=Money(gross, BRL),
        already_refunded=Money(already, BRL),
        settled=settled,
        original=original,
    )
    (draft,) = build_r8_internal(CTX, request, {})
    debits = sum(p.amount.minor_units for p in draft.postings if p.direction is Direction.DEBIT)
    credits = sum(p.amount.minor_units for p in draft.postings if p.direction is Direction.CREDIT)
    assert debits == credits
    assert all(p.amount.is_positive() for p in draft.postings)


@given(allocations(), st.integers(min_value=1, max_value=10**7))
def test_r9_dispute_shares_sum_to_disputed_amount(original: SplitAllocation, amount: int) -> None:
    opened = DisputeOpened(UUID(int=7), PA, Money(amount, BRL), original)
    shares = opened.shares()
    assert shares.total == Money(amount, BRL)
    assert all(part >= 0 for part in parts(shares))
    (draft,) = build_r9_opened(CTX, opened, {})
    debits = sum(p.amount.minor_units for p in draft.postings if p.direction is Direction.DEBIT)
    credits = sum(p.amount.minor_units for p in draft.postings if p.direction is Direction.CREDIT)
    assert debits == credits == amount

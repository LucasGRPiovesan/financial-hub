from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from financial_hub.modules.ledger.domain import (
    Account,
    AccountSpec,
    AccountType,
    Balance,
    BalanceDelta,
    Direction,
    EntryDraft,
    EntryStatus,
    JournalEntry,
    NormalBalance,
    Posting,
    PostingDraft,
    TrialBalance,
    balance_deltas,
    metadata_to_json,
    transition_factors,
)
from financial_hub.shared.errors import Conflict, ValidationFailed
from financial_hub.shared.money import BRL, USD, Currency, Money

AT = datetime(2026, 10, 10, 12, tzinfo=UTC)
CASH = AccountSpec("platform:cash", AccountType.ASSET)
REVENUE = AccountSpec("platform:revenue", AccountType.INCOME)
WALLET = AccountSpec("customer:wallet", AccountType.LIABILITY, no_overdraft=True)


def leg(
    spec: AccountSpec, direction: Direction, units: int, currency: Currency = BRL
) -> PostingDraft:
    return PostingDraft(spec, direction, Money(units, currency))


def draft(**overrides: object) -> EntryDraft:
    values: dict[str, object] = {
        "project_id": uuid4(),
        "idempotency_key": "payment:pp_1:captured",
        "recipe": "R1",
        "postings": [leg(CASH, Direction.DEBIT, 100), leg(REVENUE, Direction.CREDIT, 100)],
        "effective_at": AT,
    }
    values.update(overrides)
    created = EntryDraft.create(**values)  # pyright: ignore[reportArgumentType]
    assert created is not None
    return created


# --- accounts and balances (FINANCIAL_MODEL s2) -----------------------------------------------


@pytest.mark.parametrize(
    ("account_type", "side"),
    [
        (AccountType.ASSET, NormalBalance.DEBIT),
        (AccountType.EXPENSE, NormalBalance.DEBIT),
        (AccountType.LIABILITY, NormalBalance.CREDIT),
        (AccountType.INCOME, NormalBalance.CREDIT),
        (AccountType.EQUITY, NormalBalance.CREDIT),
    ],
)
def test_normal_balance_derives_from_type(account_type: AccountType, side: NormalBalance) -> None:
    assert account_type.normal_balance is side
    assert AccountSpec("x", account_type).normal_balance is side


def test_credit_normal_balance_and_available() -> None:
    balance = Balance(
        BRL, NormalBalance.CREDIT, posted_dr=30, posted_cr=100, pending_dr=20, pending_cr=50
    )
    assert balance.posted == Money(70, BRL)
    # Pending inflows (pending_cr) never increase available; pending outflows reduce it.
    assert balance.available == Money(50, BRL)


def test_debit_normal_balance_and_available() -> None:
    balance = Balance(
        BRL, NormalBalance.DEBIT, posted_dr=100, posted_cr=30, pending_dr=50, pending_cr=20
    )
    assert balance.posted == Money(70, BRL)
    assert balance.available == Money(50, BRL)


def test_balance_apply_and_delta_sum() -> None:
    delta = BalanceDelta(posted_dr=1, pending_cr=2) + BalanceDelta(posted_cr=3, pending_dr=4)
    assert delta == BalanceDelta(1, 3, 4, 2)
    assert Balance(BRL, NormalBalance.DEBIT).apply(delta) == Balance(
        BRL, NormalBalance.DEBIT, 1, 3, 4, 2
    )


def test_balance_rejects_non_int() -> None:
    with pytest.raises(TypeError):
        Balance(BRL, NormalBalance.DEBIT, posted_dr=1.0)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        Balance(BRL, NormalBalance.DEBIT, posted_dr=True)


@pytest.mark.parametrize("code", ["", "has space", "tab\t", "é", "x" * 201, "line\n"])
def test_account_code_is_validated(code: str) -> None:
    with pytest.raises(ValidationFailed):
        AccountSpec(code, AccountType.ASSET)


def test_account_spec_types_are_checked() -> None:
    with pytest.raises(TypeError):
        AccountSpec("x", "asset")  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        AccountSpec("x", AccountType.ASSET, no_overdraft=1)  # pyright: ignore[reportArgumentType]


def test_account_consistency() -> None:
    balance = Balance(BRL, NormalBalance.CREDIT, posted_cr=5)
    account = Account(uuid4(), uuid4(), WALLET, BRL, balance)
    assert (account.code, account.type, account.normal_balance, account.no_overdraft) == (
        "customer:wallet",
        AccountType.LIABILITY,
        NormalBalance.CREDIT,
        True,
    )
    with pytest.raises(ValueError, match="currency"):
        Account(uuid4(), uuid4(), WALLET, USD, balance)
    with pytest.raises(ValueError, match="normal side"):
        Account(uuid4(), uuid4(), CASH, BRL, balance)


# --- entry factory (FINANCIAL_MODEL s1) -------------------------------------------------------


def test_entry_factory_builds_balanced_posted_entry() -> None:
    entry = draft()
    assert entry.status is EntryStatus.POSTED
    assert len(entry.postings) == 2
    assert draft(pending=True).status is EntryStatus.PENDING


def test_zero_legs_are_dropped() -> None:
    entry = draft(
        postings=[
            leg(CASH, Direction.DEBIT, 100),
            leg(WALLET, Direction.CREDIT, 0),
            leg(REVENUE, Direction.CREDIT, 100),
        ]
    )
    assert [p.account for p in entry.postings] == [CASH, REVENUE]


def test_all_zero_legs_create_nothing() -> None:
    created = EntryDraft.create(
        project_id=uuid4(),
        idempotency_key="k",
        recipe="R1",
        postings=[leg(CASH, Direction.DEBIT, 0), leg(REVENUE, Direction.CREDIT, 0)],
        effective_at=AT,
    )
    assert created is None


def test_single_non_zero_leg_is_unbalanced_not_dropped() -> None:
    with pytest.raises(ValidationFailed, match="not balanced"):
        draft(postings=[leg(CASH, Direction.DEBIT, 100), leg(REVENUE, Direction.CREDIT, 0)])


def test_fewer_than_two_postings_rejected() -> None:
    with pytest.raises(ValidationFailed, match="at least 2"):
        EntryDraft(uuid4(), "k", "R1", (), AT)


def test_unbalanced_per_currency_rejected() -> None:
    with pytest.raises(ValidationFailed, match="BRL, USD"):
        draft(postings=[leg(CASH, Direction.DEBIT, 100), leg(REVENUE, Direction.CREDIT, 100, USD)])


def test_multi_currency_entry_balanced_per_currency() -> None:
    entry = draft(
        postings=[
            leg(CASH, Direction.DEBIT, 100),
            leg(REVENUE, Direction.CREDIT, 100),
            leg(CASH, Direction.DEBIT, 7, USD),
            leg(REVENUE, Direction.CREDIT, 7, USD),
        ]
    )
    assert {p.key for p in entry.postings} == {
        ("platform:cash", "BRL"),
        ("platform:revenue", "BRL"),
        ("platform:cash", "USD"),
        ("platform:revenue", "USD"),
    }


def test_negative_leg_rejected() -> None:
    with pytest.raises(ValidationFailed):
        leg(CASH, Direction.DEBIT, -1)


def test_direct_construction_rejects_zero_amount() -> None:
    with pytest.raises(ValidationFailed, match="> 0"):
        EntryDraft(
            uuid4(),
            "k",
            "R1",
            (leg(CASH, Direction.DEBIT, 0), leg(REVENUE, Direction.CREDIT, 0)),
            AT,
        )


def test_posting_draft_types_are_checked() -> None:
    with pytest.raises(TypeError):
        PostingDraft(CASH, "debit", Money(1, BRL))  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        PostingDraft(CASH, Direction.DEBIT, 1)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "overrides",
    [
        {"idempotency_key": ""},
        {"idempotency_key": " padded"},
        {"idempotency_key": "x" * 256},
        {"recipe": ""},
        {"recipe": "x" * 65},
        {"effective_at": datetime(2026, 10, 10)},  # naive on purpose
        {"metadata": {"rate": 1.5}},
        {"metadata": {"nested": [{"bad": 0.1}]}},
        {"metadata": {1: "non-string key"}},
        {"metadata": {"obj": object()}},
    ],
)
def test_entry_fields_are_validated(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationFailed):
        draft(**overrides)


def test_entry_rejects_bad_types_and_status() -> None:
    legs = (leg(CASH, Direction.DEBIT, 1), leg(REVENUE, Direction.CREDIT, 1))
    with pytest.raises(TypeError):
        EntryDraft("not-a-uuid", "k", "R1", legs, AT)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        EntryDraft(uuid4(), "k", "R1", legs, "2026-10-10")  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationFailed, match="pending or posted"):
        EntryDraft(uuid4(), "k", "R1", legs, AT, status=EntryStatus.ARCHIVED)
    with pytest.raises(ValidationFailed, match="JSON object"):
        EntryDraft(uuid4(), "k", "R1", legs, AT, metadata=["x"])  # pyright: ignore[reportArgumentType]


def test_effective_at_is_normalized_to_utc() -> None:
    entry = draft(effective_at=datetime(2026, 10, 10, 9, tzinfo=timezone(timedelta(hours=-3))))
    assert entry.effective_at == AT
    assert entry.effective_at.tzinfo is UTC


def test_metadata_is_frozen_and_round_trips() -> None:
    entry = draft(metadata={"invoice": "inv_1", "lines": [1, {"a": None, "ok": True}]})
    assert metadata_to_json(entry.metadata) == {
        "invoice": "inv_1",
        "lines": [1, {"a": None, "ok": True}],
    }
    with pytest.raises(TypeError):
        entry.metadata["x"] = 1  # pyright: ignore[reportIndexIssue]


def test_payload_hash_covers_financial_content_only() -> None:
    project = uuid4()
    base = draft(project_id=project)
    assert base.payload_hash == draft(project_id=project).payload_hash
    assert len(base.payload_hash) == 64
    # Delivery data does not change the fingerprint: a redelivery replays.
    assert (
        draft(
            project_id=project, correlation_id=uuid4(), causation_id=uuid4(), metadata={"a": "b"}
        ).payload_hash
        == base.payload_hash
    )
    # Leg order does not matter either.
    reordered = draft(
        project_id=project,
        postings=[leg(REVENUE, Direction.CREDIT, 100), leg(CASH, Direction.DEBIT, 100)],
    )
    assert reordered.payload_hash == base.payload_hash
    for changed in (
        draft(project_id=uuid4()),
        draft(project_id=project, recipe="R2"),
        draft(project_id=project, pending=True),
        draft(project_id=project, effective_at=AT + timedelta(seconds=1)),
        draft(project_id=project, reverses_entry_id=uuid4()),
        draft(
            project_id=project,
            postings=[leg(CASH, Direction.DEBIT, 101), leg(REVENUE, Direction.CREDIT, 101)],
        ),
    ):
        assert changed.payload_hash != base.payload_hash


# --- stored entries and transitions -----------------------------------------------------------


def stored(status: EntryStatus) -> JournalEntry:
    postings = (
        Posting(uuid4(), uuid4(), Direction.DEBIT, Money(5, BRL)),
        Posting(uuid4(), uuid4(), Direction.CREDIT, Money(5, BRL)),
    )
    return JournalEntry(uuid4(), uuid4(), status, "k", "0" * 64, "R1", AT, postings)


def test_transitions() -> None:
    assert stored(EntryStatus.PENDING).transition_to(EntryStatus.POSTED) is True
    assert stored(EntryStatus.PENDING).transition_to(EntryStatus.ARCHIVED) is True
    assert stored(EntryStatus.POSTED).transition_to(EntryStatus.POSTED) is False
    for source, target in [
        (EntryStatus.POSTED, EntryStatus.ARCHIVED),
        (EntryStatus.POSTED, EntryStatus.PENDING),
        (EntryStatus.ARCHIVED, EntryStatus.POSTED),
        (EntryStatus.PENDING, EntryStatus.PENDING),
    ]:
        if source is target:
            assert stored(source).transition_to(target) is False
        else:
            with pytest.raises(Conflict):
                stored(source).transition_to(target)


def test_stored_entry_must_be_balanced() -> None:
    with pytest.raises(ValidationFailed):
        JournalEntry(
            uuid4(),
            uuid4(),
            EntryStatus.POSTED,
            "k",
            "0" * 64,
            "R1",
            AT,
            (Posting(uuid4(), uuid4(), Direction.DEBIT, Money(5, BRL)),),
        )


@pytest.mark.parametrize(
    ("source", "target", "factors"),
    [
        (None, EntryStatus.PENDING, (1, 0)),
        (None, EntryStatus.POSTED, (0, 1)),
        (EntryStatus.PENDING, EntryStatus.POSTED, (-1, 1)),
        (EntryStatus.PENDING, EntryStatus.ARCHIVED, (-1, 0)),
        (EntryStatus.POSTED, EntryStatus.POSTED, (0, 0)),
    ],
)
def test_transition_factors(
    source: EntryStatus | None, target: EntryStatus, factors: tuple[int, int]
) -> None:
    assert transition_factors(source, target) == factors


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (None, EntryStatus.ARCHIVED),
        (EntryStatus.POSTED, EntryStatus.ARCHIVED),
        (EntryStatus.ARCHIVED, EntryStatus.POSTED),
    ],
)
def test_invalid_transition_factors(source: EntryStatus | None, target: EntryStatus) -> None:
    with pytest.raises(Conflict):
        transition_factors(source, target)


def test_balance_deltas_per_transition() -> None:
    a, b = uuid4(), uuid4()
    legs = [
        (a, Direction.DEBIT, Money(10, BRL)),
        (b, Direction.CREDIT, Money(10, BRL)),
        (a, Direction.CREDIT, Money(3, BRL)),
        (b, Direction.DEBIT, Money(3, BRL)),
    ]
    assert balance_deltas(legs, None, EntryStatus.PENDING) == {
        a: BalanceDelta(pending_dr=10, pending_cr=3),
        b: BalanceDelta(pending_dr=3, pending_cr=10),
    }
    assert balance_deltas(legs, EntryStatus.PENDING, EntryStatus.POSTED) == {
        a: BalanceDelta(posted_dr=10, posted_cr=3, pending_dr=-10, pending_cr=-3),
        b: BalanceDelta(posted_dr=3, posted_cr=10, pending_dr=-3, pending_cr=-10),
    }


def test_trial_balance() -> None:
    assert TrialBalance(BRL, 10, 10, 5, 5).is_balanced
    unbalanced = TrialBalance(BRL, 10, 9, 5, 6)
    assert (unbalanced.posted_net, unbalanced.pending_net, unbalanced.is_balanced) == (1, -1, False)


def test_direction_opposite() -> None:
    assert Direction.DEBIT.opposite() is Direction.CREDIT
    assert Direction.CREDIT.opposite() is Direction.DEBIT

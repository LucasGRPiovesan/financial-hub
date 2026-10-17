"""Transaction matching and pair comparison (pure, no I/O)."""

from dataclasses import replace
from uuid import uuid4

from financial_hub.modules.reconciliation.domain.matcher import (
    ExternalRecord,
    InternalRecord,
    compare,
    match,
    missing_external,
    missing_internal,
)


def hub(n: int = 1, **changes: object) -> InternalRecord:
    record = InternalRecord(
        intent_id=uuid4(),
        external_reference=f"pi_{n}",
        provider_payment_id=f"pay_{n}",
        currency="BRL",
        amount=1000,
        capture_status="captured",
        settlement_status="settled",
        dispute_status="none",
        booked_net=901,
        refunded=0,
    )
    return replace(record, **changes)  # type: ignore[arg-type]


def provider(n: int = 1, **changes: object) -> ExternalRecord:
    record = ExternalRecord(
        provider_payment_id=f"pay_{n}",
        external_reference=f"pi_{n}",
        currency="BRL",
        amount=1000,
        capture_status="captured",
        settlement_status="settled",
        dispute_status="none",
        net_amount=901,
        refunded=0,
    )
    return replace(record, **changes)  # type: ignore[arg-type]


def kinds_of(hub_record: InternalRecord, provider_record: ExternalRecord) -> list[str]:
    return [draft.kind.value for draft in compare(hub_record, provider_record)]


def test_match_pairs_by_provider_id_first() -> None:
    result = match([hub(1), hub(2)], [provider(2), provider(1)])

    assert [(h.provider_payment_id, p.provider_payment_id) for h, p in result.pairs] == [
        ("pay_2", "pay_2"),
        ("pay_1", "pay_1"),
    ]
    assert result.internal_only == [] and result.external_only == []


def test_match_falls_back_to_the_external_reference() -> None:
    lost_create = provider(1, provider_payment_id="pay_other")
    result = match([hub(1)], [lost_create])

    assert [(h.external_reference, p.provider_payment_id) for h, p in result.pairs] == [
        ("pi_1", "pay_other")
    ]


def test_a_hub_payment_is_matched_at_most_once() -> None:
    duplicate = provider(1, provider_payment_id="pay_dup")  # same reference, other id
    result = match([hub(1)], [provider(1), duplicate])

    assert len(result.pairs) == 1
    assert result.external_only == [duplicate]


def test_leftovers_on_each_side() -> None:
    result = match([hub(1), hub(2)], [provider(1), provider(3, external_reference=None)])

    assert [record.external_reference for record in result.internal_only] == ["pi_2"]
    assert [record.provider_payment_id for record in result.external_only] == ["pay_3"]


def test_an_agreeing_pair_has_no_breaks() -> None:
    assert compare(hub(), provider()) == []


def test_amount_mismatch_counts_refunds_and_currency() -> None:
    [amount] = compare(hub(), provider(amount=1200))
    [refund] = compare(hub(), provider(refunded=300))
    [currency] = compare(hub(), provider(currency="USD"))

    assert (amount.kind.value, amount.difference) == ("amount_mismatch", 200)
    assert (refund.kind.value, refund.difference) == ("amount_mismatch", -300)
    assert currency.kind.value == "amount_mismatch"
    assert amount.internal is not None and amount.internal["amount_minor"] == 1000
    assert amount.external is not None and amount.external["amount_minor"] == 1200


def test_status_mismatch_covers_capture_settlement_and_dispute() -> None:
    assert kinds_of(hub(capture_status="pending", booked_net=None), provider()) == [
        "status_mismatch"
    ]
    assert kinds_of(hub(settlement_status="none"), provider()) == ["status_mismatch"]
    assert kinds_of(hub(), provider(dispute_status="open")) == ["status_mismatch"]


def test_fee_mismatch_only_between_two_captured_nets() -> None:
    [fee] = compare(hub(), provider(net_amount=880))

    assert (fee.kind.value, fee.difference) == ("fee_mismatch", -21)
    assert kinds_of(hub(booked_net=None), provider(net_amount=880)) == []
    assert kinds_of(hub(), provider(net_amount=None)) == []


def test_several_differences_are_separate_breaks() -> None:
    assert kinds_of(hub(), provider(amount=1200, dispute_status="open", net_amount=1100)) == [
        "amount_mismatch",
        "status_mismatch",
        "fee_mismatch",
    ]


def test_missing_breaks_carry_one_side_and_a_signed_difference() -> None:
    unknown = missing_internal(provider(9, refunded=100))
    lost = missing_external(hub(8, refunded=250))

    assert (unknown.kind.value, unknown.internal, unknown.difference) == (
        "missing_internal",
        None,
        900,
    )
    assert unknown.subject == {"provider_payment_id": "pay_9"}
    assert (lost.kind.value, lost.external, lost.difference) == ("missing_external", None, -750)
    assert lost.subject == {"provider_payment_id": "pay_8", "payment_intent_id": "pi_8"}


def test_fingerprints_are_stable_per_payment_and_kind() -> None:
    first = compare(hub(), provider(amount=1200))[0]
    again = compare(hub(), provider(amount=1300))[0]
    other = compare(hub(2), provider(2, amount=1200))[0]

    assert first.fingerprint == again.fingerprint == "amount_mismatch:pay_1"
    assert other.fingerprint != first.fingerprint
    assert missing_internal(provider()).fingerprint != missing_external(hub()).fingerprint

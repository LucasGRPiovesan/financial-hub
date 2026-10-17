"""Transaction matching (ADR-0013 s2): hub payments vs. the provider's listing, 1:1.

1. by provider payment id (the hub stores it at creation);
2. what is left, by ``externalReference`` = the hub's ``pi_`` id (a create whose answer was lost).

A matched pair is compared field by field; each difference is its own break. The leftovers are
*candidates*: the application confirms them (``missing_external`` by fetching the payment,
``missing_internal`` by an all-time hub lookup) before opening a break, because the window only
scopes the listing, not the match.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from financial_hub.modules.reconciliation.domain.model import BreakDraft, BreakKind, fingerprint

CAPTURED = "captured"


@dataclass(frozen=True, slots=True)
class InternalRecord:
    intent_id: UUID
    external_reference: str
    provider_payment_id: str
    currency: str
    amount: int
    capture_status: str
    settlement_status: str
    dispute_status: str
    booked_net: int | None
    refunded: int

    def side(self) -> dict[str, object]:
        return {
            "payment_intent_id": self.external_reference,
            "provider_payment_id": self.provider_payment_id,
            "currency": self.currency,
            "amount_minor": self.amount,
            "refunded_minor": self.refunded,
            "net_minor": self.booked_net,
            "capture_status": self.capture_status,
            "settlement_status": self.settlement_status,
            "dispute_status": self.dispute_status,
        }


@dataclass(frozen=True, slots=True)
class ExternalRecord:
    provider_payment_id: str
    external_reference: str | None
    currency: str
    amount: int
    capture_status: str
    settlement_status: str
    dispute_status: str
    net_amount: int | None
    refunded: int

    def side(self) -> dict[str, object]:
        return {
            "provider_payment_id": self.provider_payment_id,
            "external_reference": self.external_reference,
            "currency": self.currency,
            "amount_minor": self.amount,
            "refunded_minor": self.refunded,
            "net_minor": self.net_amount,
            "capture_status": self.capture_status,
            "settlement_status": self.settlement_status,
            "dispute_status": self.dispute_status,
        }


@dataclass(frozen=True, slots=True)
class MatchResult:
    pairs: list[tuple[InternalRecord, ExternalRecord]]
    internal_only: list[InternalRecord]
    external_only: list[ExternalRecord]


def match(internal: Sequence[InternalRecord], external: Sequence[ExternalRecord]) -> MatchResult:
    by_provider_id = {record.provider_payment_id: record for record in internal}
    by_reference = {record.external_reference: record for record in internal}
    used: set[str] = set()
    pairs: list[tuple[InternalRecord, ExternalRecord]] = []
    unmatched: list[ExternalRecord] = []
    for item in external:
        hub = by_provider_id.get(item.provider_payment_id)
        if hub is not None and hub.external_reference not in used:
            used.add(hub.external_reference)
            pairs.append((hub, item))
        else:
            unmatched.append(item)
    external_only: list[ExternalRecord] = []
    for item in unmatched:
        hub = None if item.external_reference is None else by_reference.get(item.external_reference)
        if hub is not None and hub.external_reference not in used:
            used.add(hub.external_reference)
            pairs.append((hub, item))
        else:
            external_only.append(item)
    internal_only = [record for record in internal if record.external_reference not in used]
    return MatchResult(pairs, internal_only, external_only)


def _subject(hub: InternalRecord | None, provider_payment_id: str) -> dict[str, str]:
    subject = {"provider_payment_id": provider_payment_id}
    if hub is not None:
        subject["payment_intent_id"] = hub.external_reference
    return subject


def compare(hub: InternalRecord, provider: ExternalRecord) -> list[BreakDraft]:
    """The breaks of one matched pair (none when both sides agree)."""

    def pair_break(kind: BreakKind, difference: int | None = None) -> BreakDraft:
        pp_id = provider.provider_payment_id
        return BreakDraft(
            kind,
            fingerprint(kind, pp_id),
            subject=_subject(hub, pp_id),
            internal=hub.side(),
            external=provider.side(),
            currency=hub.currency,
            difference=difference,
            payment_intent_id=hub.intent_id,
            provider_payment_id=pp_id,
        )

    drafts: list[BreakDraft] = []
    if (
        hub.currency != provider.currency
        or hub.amount != provider.amount
        or hub.refunded != provider.refunded
    ):
        difference = (provider.amount - provider.refunded) - (hub.amount - hub.refunded)
        drafts.append(pair_break(BreakKind.AMOUNT_MISMATCH, difference))
    if (hub.capture_status, hub.settlement_status, hub.dispute_status) != (
        provider.capture_status,
        provider.settlement_status,
        provider.dispute_status,
    ):
        drafts.append(pair_break(BreakKind.STATUS_MISMATCH))
    if (
        hub.capture_status == CAPTURED
        and provider.capture_status == CAPTURED
        and hub.booked_net is not None
        and provider.net_amount is not None
        and hub.booked_net != provider.net_amount
    ):
        drafts.append(pair_break(BreakKind.FEE_MISMATCH, provider.net_amount - hub.booked_net))
    return drafts


def missing_internal(provider: ExternalRecord) -> BreakDraft:
    """A provider payment the hub does not know (created at the provider, or a lost create)."""
    return BreakDraft(
        BreakKind.MISSING_INTERNAL,
        fingerprint(BreakKind.MISSING_INTERNAL, provider.provider_payment_id),
        subject=_subject(None, provider.provider_payment_id),
        internal=None,
        external=provider.side(),
        currency=provider.currency,
        difference=provider.amount - provider.refunded,
        provider_payment_id=provider.provider_payment_id,
    )


def missing_external(hub: InternalRecord) -> BreakDraft:
    """A hub payment the provider says does not exist."""
    return BreakDraft(
        BreakKind.MISSING_EXTERNAL,
        fingerprint(BreakKind.MISSING_EXTERNAL, hub.provider_payment_id),
        subject=_subject(hub, hub.provider_payment_id),
        internal=hub.side(),
        external=None,
        currency=hub.currency,
        difference=-(hub.amount - hub.refunded),
        payment_intent_id=hub.intent_id,
        provider_payment_id=hub.provider_payment_id,
    )


__all__ = [
    "CAPTURED",
    "ExternalRecord",
    "InternalRecord",
    "MatchResult",
    "compare",
    "match",
    "missing_external",
    "missing_internal",
]

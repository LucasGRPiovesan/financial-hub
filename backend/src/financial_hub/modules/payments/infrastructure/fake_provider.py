"""The ``fake`` payment provider: deterministic, in memory, scriptable. Development and tests only.

It honours the :class:`PaymentProvider` port like a real adapter would, including the failure
modes the external-call protocol must survive (ADR-0012 s3):

- ``SUCCEED`` / ``SUCCEED_CAPTURED``: the payment is created (pending, or already paid);
- ``REJECT``: definitive refusal, nothing is created;
- ``RETRYABLE``: the provider says it did not process the request (nothing is created);
- ``TIMEOUT_BEFORE_COMMIT``: the call fails as uncertain and nothing was created;
- ``TIMEOUT_AFTER_COMMIT``: the payment *is* created, then the call fails as uncertain;
- a second create with an existing external reference raises :class:`ProviderDuplicate`.

Behaviours are consumed in order from :meth:`FakePaymentProvider.script` (then ``default``).
Ids are sequential (``fake_pay_000001``, or ``fake_pay_<namespace>_000001``: provider ids are
unique per provider in the database, so a long-lived process or a test names its own namespace).
:meth:`hold` pauses the next create *after* it took effect, to interleave a late response with a
resolver in tests. No money moves and no network is used; the registry exposes it only when
settings allow it.
"""

import asyncio
from collections import deque
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final

from financial_hub.modules.payments.domain import (
    FAKE_PROVIDER,
    CaptureStatus,
    CreatePaymentRequest,
    DisputeStatus,
    ProviderCustomer,
    ProviderCustomerRequest,
    ProviderDuplicate,
    ProviderPaymentSnapshot,
    ProviderRefundSnapshot,
    ProviderRejected,
    ProviderRetryable,
    ProviderUncertain,
    RefundRequest,
    RefundState,
    SettlementStatus,
)
from financial_hub.shared.money import Money

RAW_STATUS: Final = {
    CaptureStatus.PENDING: "PENDING",
    CaptureStatus.CAPTURED: "RECEIVED",
    CaptureStatus.FAILED: "OVERDUE",
    CaptureStatus.CANCELED: "DELETED",
}


class FakeBehavior(StrEnum):
    SUCCEED = "succeed"
    SUCCEED_CAPTURED = "succeed_captured"
    REJECT = "reject"
    RETRYABLE = "retryable"
    TIMEOUT_BEFORE_COMMIT = "timeout_before_commit"
    TIMEOUT_AFTER_COMMIT = "timeout_after_commit"


class FakeRefundBehavior(StrEnum):
    DONE = "done"
    SUBMITTED = "submitted"
    REJECT = "reject"
    RETRYABLE = "retryable"
    TIMEOUT_AFTER_COMMIT = "timeout_after_commit"


@dataclass(slots=True)
class _Payment:
    snapshot: ProviderPaymentSnapshot
    refunds: list[ProviderRefundSnapshot] = field(default_factory=list[ProviderRefundSnapshot])

    def current(self) -> ProviderPaymentSnapshot:
        return replace(self.snapshot, refunds=tuple(self.refunds))


class FakePaymentProvider:
    """One in-memory provider "account". Not thread-safe; one event loop at a time."""

    def __init__(
        self,
        *,
        default: FakeBehavior = FakeBehavior.SUCCEED,
        default_refund: FakeRefundBehavior = FakeRefundBehavior.DONE,
        namespace: str = "",
    ) -> None:
        self.namespace = namespace
        self.default = default
        self.default_refund = default_refund
        self.lookup_unavailable = False
        self.calls: list[str] = []
        self._script: deque[FakeBehavior] = deque()
        self._refund_script: deque[FakeRefundBehavior] = deque()
        self._payments: dict[str, _Payment] = {}
        self._by_reference: dict[str, str] = {}
        self._customers: dict[str, str] = {}
        self._sequence = 0
        self._hold: asyncio.Event | None = None
        self.held = asyncio.Event()

    @property
    def name(self) -> str:
        return FAKE_PROVIDER

    # --- scripting ------------------------------------------------------------------------------

    def script(self, *behaviors: FakeBehavior) -> None:
        self._script.extend(behaviors)

    def script_refunds(self, *behaviors: FakeRefundBehavior) -> None:
        self._refund_script.extend(behaviors)

    def hold(self) -> asyncio.Event:
        """Pause the next create after it took effect, until the returned event is set."""
        self._hold = asyncio.Event()
        self.held = asyncio.Event()
        return self._hold

    def _next_id(self, kind: str) -> str:
        self._sequence += 1
        prefix = f"fake_{kind}_{self.namespace}_" if self.namespace else f"fake_{kind}_"
        return f"{prefix}{self._sequence:06d}"

    # --- provider-side changes (what a webhook would announce) ----------------------------------

    def _set(self, provider_payment_id: str, **changes: object) -> ProviderPaymentSnapshot:
        record = self._payments[provider_payment_id]
        record.snapshot = replace(record.snapshot, **changes)  # pyright: ignore[reportArgumentType]
        return record.current()

    def capture(self, provider_payment_id: str, *, fee: int = 0) -> ProviderPaymentSnapshot:
        amount = self._payments[provider_payment_id].snapshot.amount.minor_units
        return self._set(
            provider_payment_id,
            capture_status=CaptureStatus.CAPTURED,
            raw_status=RAW_STATUS[CaptureStatus.CAPTURED],
            net_amount=amount - fee,
        )

    def fail(self, provider_payment_id: str) -> ProviderPaymentSnapshot:
        return self._set(
            provider_payment_id,
            capture_status=CaptureStatus.FAILED,
            raw_status=RAW_STATUS[CaptureStatus.FAILED],
        )

    def settle(self, provider_payment_id: str) -> ProviderPaymentSnapshot:
        return self._set(provider_payment_id, settlement_status=SettlementStatus.SETTLED)

    def dispute(self, provider_payment_id: str, status: DisputeStatus) -> ProviderPaymentSnapshot:
        return self._set(provider_payment_id, dispute_status=status)

    def refund_externally(self, provider_payment_id: str, amount: Money) -> ProviderRefundSnapshot:
        """A refund made at the provider (dashboard), with no hub reference."""
        refund = ProviderRefundSnapshot(
            provider_refund_id=self._next_id("ref"),
            external_reference=None,
            amount=amount,
            status=RefundState.DONE,
        )
        self._payments[provider_payment_id].refunds.append(refund)
        return refund

    def find(self, external_reference: str) -> ProviderPaymentSnapshot | None:
        provider_id = self._by_reference.get(external_reference)
        return None if provider_id is None else self._payments[provider_id].current()

    @property
    def payment_count(self) -> int:
        return len(self._payments)

    # --- the port -------------------------------------------------------------------------------

    async def create_customer(self, request: ProviderCustomerRequest) -> ProviderCustomer:
        self.calls.append("create_customer")
        provider_id = self._customers.get(request.external_reference)
        if provider_id is None:
            provider_id = self._customers[request.external_reference] = self._next_id("cus")
        return ProviderCustomer(provider_customer_id=provider_id)

    async def create_payment(self, request: CreatePaymentRequest) -> ProviderPaymentSnapshot:
        self.calls.append("create_payment")
        behavior = self._script.popleft() if self._script else self.default
        if request.external_reference in self._by_reference:
            raise ProviderDuplicate("duplicate_external_reference")
        if behavior is FakeBehavior.REJECT:
            raise ProviderRejected("invalid_request", "the fake provider rejected the payment")
        if behavior is FakeBehavior.RETRYABLE:
            raise ProviderRetryable("rate_limited")
        if behavior is FakeBehavior.TIMEOUT_BEFORE_COMMIT:
            raise ProviderUncertain("timeout")
        captured = behavior is FakeBehavior.SUCCEED_CAPTURED
        capture = CaptureStatus.CAPTURED if captured else CaptureStatus.PENDING
        provider_id = self._next_id("pay")
        snapshot = ProviderPaymentSnapshot(
            provider_payment_id=provider_id,
            external_reference=request.external_reference,
            raw_status=RAW_STATUS[capture],
            amount=request.amount,
            capture_status=capture,
            net_amount=request.amount.minor_units if captured else None,
        )
        self._payments[provider_id] = _Payment(snapshot)
        self._by_reference[request.external_reference] = provider_id
        if self._hold is not None:
            hold, self._hold = self._hold, None
            self.held.set()
            await hold.wait()
        if behavior is FakeBehavior.TIMEOUT_AFTER_COMMIT:
            raise ProviderUncertain("timeout")
        return snapshot

    async def get_payment(self, provider_payment_id: str) -> ProviderPaymentSnapshot:
        self.calls.append("get_payment")
        record = self._payments.get(provider_payment_id)
        if record is None:
            raise ProviderRejected("not_found")
        return record.current()

    async def lookup_by_external_reference(
        self, external_reference: str
    ) -> ProviderPaymentSnapshot | None:
        self.calls.append("lookup_by_external_reference")
        if self.lookup_unavailable:
            raise ProviderUncertain("unavailable")
        return self.find(external_reference)

    async def refund(self, request: RefundRequest) -> ProviderRefundSnapshot:
        self.calls.append("refund")
        behavior = self._refund_script.popleft() if self._refund_script else self.default_refund
        record = self._payments.get(request.provider_payment_id)
        if record is None:
            raise ProviderRejected("not_found")
        if any(r.external_reference == request.external_reference for r in record.refunds):
            raise ProviderDuplicate("duplicate_external_reference")
        if behavior is FakeRefundBehavior.REJECT:
            raise ProviderRejected("refund_not_allowed")
        if behavior is FakeRefundBehavior.RETRYABLE:
            raise ProviderRetryable("rate_limited")
        submitted = behavior is FakeRefundBehavior.SUBMITTED
        status = RefundState.SUBMITTED if submitted else RefundState.DONE
        refund = ProviderRefundSnapshot(
            provider_refund_id=self._next_id("ref"),
            external_reference=request.external_reference,
            amount=request.amount,
            status=status,
        )
        record.refunds.append(refund)
        if behavior is FakeRefundBehavior.TIMEOUT_AFTER_COMMIT:
            raise ProviderUncertain("timeout")
        return refund

    def complete_refund(self, provider_payment_id: str, provider_refund_id: str) -> None:
        refunds = self._payments[provider_payment_id].refunds
        for index, refund in enumerate(refunds):
            if refund.provider_refund_id == provider_refund_id:
                refunds[index] = replace(refund, status=RefundState.DONE)

    async def cancel(self, provider_payment_id: str) -> ProviderPaymentSnapshot:
        self.calls.append("cancel")
        record = self._payments.get(provider_payment_id)
        if record is None:
            raise ProviderRejected("not_found")
        if record.snapshot.capture_status is not CaptureStatus.PENDING:
            raise ProviderRejected("not_cancelable")
        return self._set(
            provider_payment_id,
            capture_status=CaptureStatus.CANCELED,
            raw_status=RAW_STATUS[CaptureStatus.CANCELED],
        )

"""The payment provider port: what an adapter (Asaas in PRO-41, the fake here) must implement.

Pure types only. Requests and responses are typed value objects with :class:`Money` amounts; the
adapter maps the provider's own status vocabulary to the orthogonal state fields (ADR-0012 s1) and
keeps the raw status for the receipt. Errors say what the hub may assume about the call:

- :class:`ProviderRejected`: definitive refusal, nothing was created (validation, unknown id...);
- :class:`ProviderRetryable`: the provider says it did not process the request (rate limited,
  temporarily unavailable). The hub still never re-sends a *create* blindly: it looks up first;
- :class:`ProviderUncertain`: the request may or may not have taken effect (timeout, 5xx, broken
  connection). Only :meth:`PaymentProvider.lookup_by_external_reference` can tell;
- :class:`ProviderDuplicate`: the provider already holds an object with this external reference
  (an earlier attempt succeeded): resolve it by lookup.

Every object the hub creates carries ``external_reference`` = the hub's own id (``pi_`` / ``rf_``),
which is what makes the lookup possible.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from financial_hub.modules.payments.domain.model import PaymentMethod
from financial_hub.modules.payments.domain.refunds import RefundState
from financial_hub.modules.payments.domain.state import (
    CaptureStatus,
    DisputeStatus,
    SettlementStatus,
)
from financial_hub.shared.money import Money


class ProviderCredentials:
    """Decrypted provider credentials, passed in by the caller. Never printed or logged."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def __repr__(self) -> str:
        return "ProviderCredentials(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class ProviderCustomerRequest:
    external_reference: str  # the hub's cus_ id
    name: str
    email: str | None = None
    phone: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderCustomer:
    provider_customer_id: str


@dataclass(frozen=True, slots=True)
class CreatePaymentRequest:
    external_reference: str  # the hub's pi_ id
    amount: Money
    method: PaymentMethod
    due_date: date
    customer_reference: str  # the hub's cus_ id
    provider_customer_id: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class RefundRequest:
    provider_payment_id: str
    external_reference: str  # the hub's rf_ id
    amount: Money
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderRefundSnapshot:
    provider_refund_id: str
    external_reference: str | None
    amount: Money
    status: RefundState


@dataclass(frozen=True, slots=True)
class ProviderPaymentSnapshot:
    """A provider payment as fetched (never a webhook body, ADR-0012 s2)."""

    provider_payment_id: str
    external_reference: str | None
    raw_status: str
    amount: Money
    capture_status: CaptureStatus
    settlement_status: SettlementStatus = SettlementStatus.NONE
    dispute_status: DisputeStatus = DisputeStatus.NONE
    net_amount: int | None = None
    refunds: tuple[ProviderRefundSnapshot, ...] = field(default=())


class ProviderError(Exception):
    """Base of the port's errors. ``code`` is a short machine code, never provider PII."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(detail or code)
        self.code = code


class ProviderRejected(ProviderError):
    """Definitive refusal: nothing was created or changed."""


class ProviderRetryable(ProviderError):
    """The provider did not process the request; the hub looks up before any new attempt."""


class ProviderUncertain(ProviderError):
    """The outcome is unknown (timeout, 5xx, broken connection)."""


class ProviderDuplicate(ProviderUncertain):
    """An object with this external reference already exists at the provider."""


class ProviderUnavailable(ProviderError):
    """No adapter is registered for the provider, or it cannot be built (e.g. no credentials)."""


class PaymentProvider(Protocol):
    """One provider account's client. Implementations must not log credentials or PII."""

    @property
    def name(self) -> str: ...

    async def create_customer(self, request: ProviderCustomerRequest) -> ProviderCustomer: ...

    async def create_payment(self, request: CreatePaymentRequest) -> ProviderPaymentSnapshot: ...

    async def get_payment(self, provider_payment_id: str) -> ProviderPaymentSnapshot: ...

    async def lookup_by_external_reference(
        self, external_reference: str
    ) -> ProviderPaymentSnapshot | None:
        """The payment created with ``external_reference``; ``None`` is a *confirmed* absence."""
        ...

    async def refund(self, request: RefundRequest) -> ProviderRefundSnapshot: ...

    async def cancel(self, provider_payment_id: str) -> ProviderPaymentSnapshot: ...

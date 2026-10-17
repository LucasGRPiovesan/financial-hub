"""Plans, subscriptions and invoices (docs/DOMAIN.md "Billing"). Amounts are integer minor units.

Invoice lifecycle: ``draft -> finalized -> paid | void | uncollectible``, ``draft -> void`` and
``uncollectible -> paid | void``; ``paid`` and ``void`` are final (the database enforces the same
moves). Subscription status only moves forward: ``trialing -> active -> canceled``.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from financial_hub.modules.billing.domain.periods import Period

BILLING_INTERVAL: Final = "month"
PAYMENT_METHODS: Final = frozenset({"pix", "boleto", "card"})
DEFAULT_PAYMENT_METHOD: Final = "pix"
DEFAULT_DAYS_UNTIL_DUE: Final = 7
MAX_DAYS_UNTIL_DUE: Final = 60
MAX_TRIAL_DAYS: Final = 365
MAX_PLAN_PRICES: Final = 20


class SubscriptionStatus(StrEnum):
    TRIALING = "trialing"
    ACTIVE = "active"
    CANCELED = "canceled"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    FINALIZED = "finalized"
    PAID = "paid"
    VOID = "void"
    UNCOLLECTIBLE = "uncollectible"


class LineKind(StrEnum):
    FIXED = "fixed"
    USAGE = "usage"


_MOVES: Final[Mapping[InvoiceStatus, frozenset[InvoiceStatus]]] = {
    InvoiceStatus.DRAFT: frozenset({InvoiceStatus.FINALIZED, InvoiceStatus.VOID}),
    InvoiceStatus.FINALIZED: frozenset(
        {InvoiceStatus.PAID, InvoiceStatus.VOID, InvoiceStatus.UNCOLLECTIBLE}
    ),
    InvoiceStatus.UNCOLLECTIBLE: frozenset({InvoiceStatus.PAID, InvoiceStatus.VOID}),
    InvoiceStatus.PAID: frozenset(),
    InvoiceStatus.VOID: frozenset(),
}


def can_move(current: InvoiceStatus, target: InvoiceStatus) -> bool:
    """Whether the invoice lifecycle allows ``current -> target``."""
    return target in _MOVES[current]


@dataclass(frozen=True, slots=True)
class InvoiceAmounts:
    """``amount_due = total - credits_applied``; ``credits_applied`` is the W5.4 wallet seam."""

    total: int
    credits_applied: int
    amount_due: int


def invoice_amounts(line_amounts: Iterable[int], *, credits_applied: int = 0) -> InvoiceAmounts:
    """Integer totals of an invoice from its line amounts (minor units, each ``>= 0``)."""
    amounts = list(line_amounts)
    for amount in (*amounts, credits_applied):
        if type(amount) is not int:
            raise TypeError(f"amounts are int minor units, got {type(amount).__name__}")
        if amount < 0:
            raise ValueError("invoice amounts must be >= 0")
    total = sum(amounts)
    if credits_applied > total:
        raise ValueError("credits_applied cannot exceed the invoice total")
    return InvoiceAmounts(total, credits_applied, total - credits_applied)


@dataclass(frozen=True, slots=True)
class Plan:
    id: UUID
    project_id: UUID
    code: str
    name: str
    currency: str
    price_ids: tuple[UUID, ...]
    created_at: datetime
    billing_interval: str = BILLING_INTERVAL


@dataclass(frozen=True, slots=True)
class Subscription:
    id: UUID
    project_id: UUID
    customer_id: UUID
    plan_id: UUID
    status: SubscriptionStatus
    anchor_day: int
    started_at: datetime
    trial_end: datetime | None
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    canceled_at: datetime | None
    ended_at: datetime | None
    payment_method: str
    days_until_due: int
    created_at: datetime
    updated_at: datetime

    @property
    def current_period(self) -> Period:
        return Period(self.current_period_start, self.current_period_end)

    @property
    def billing_start(self) -> datetime:
        """When paid service starts: usage before it (the trial) is never billed."""
        return self.trial_end or self.started_at


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    """One rated price. ``quantity`` is the billed usage (``None`` for fixed lines)."""

    id: UUID
    position: int
    kind: LineKind
    price_id: UUID
    price_code: str
    price_version_id: UUID
    price_version: int
    model: str
    metric_code: str | None
    quantity: Decimal | None
    amount: int
    rounding: str
    details: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class Invoice:
    id: UUID
    project_id: UUID
    subscription_id: UUID
    customer_id: UUID
    currency: str
    status: InvoiceStatus
    period_start: datetime
    period_end: datetime
    total: int
    credits_applied: int
    amount_due: int
    payment_intent_id: UUID | None
    due_date: date | None
    finalized_at: datetime | None
    paid_at: datetime | None
    voided_at: datetime | None
    created_at: datetime
    updated_at: datetime
    lines: tuple[InvoiceLine, ...] = ()

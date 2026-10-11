"""Seeds a project with a mixed recipe flow through ``apply_recipes`` (no HTTP, no raw SQL)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from financial_hub.modules.ledger.application import (
    CustomerId,
    DisputeOpened,
    DisputeWon,
    FeeBearer,
    InvoiceFinalized,
    PayeeAmount,
    PayeeId,
    PaymentCaptured,
    PaymentSettled,
    ProviderAccountId,
    RecipeContext,
    RecipeInput,
    RefundCompleted,
    RefundOutcome,
    RefundRequested,
    SplitAllocated,
    SplitAllocation,
    SplitRule,
    SplitShare,
    apply_recipes,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.ids import IdPrefix, format_id, parse_id
from financial_hub.shared.money import BRL, Money
from tests.integration.identity.support import Api, Operator

CUSTOMER = CustomerId(UUID(int=1))
PROVIDER_ACCOUNT = ProviderAccountId(UUID(int=2))
PAYEE = PayeeId(UUID(int=3))
CORRELATION = UUID(int=99)

T1 = datetime(2026, 3, 1, 10, tzinfo=UTC)  # R1 invoice finalized
T2 = datetime(2026, 3, 2, 10, tzinfo=UTC)  # R2 + R3 capture and split
T3 = datetime(2026, 3, 3, 10, tzinfo=UTC)  # R4 settlement
T4 = datetime(2026, 3, 4, 10, tzinfo=UTC)  # R9 dispute opened, then won (reversal)
T5 = datetime(2026, 3, 5, 10, tzinfo=UTC)  # R8 refund requested (pending), still open


def brl(units: int) -> Money:
    return Money(units, BRL)


@dataclass(frozen=True)
class Seeded:
    owner: Operator
    project_id: str

    @property
    def uuid(self) -> UUID:
        return parse_id(self.project_id, IdPrefix.PROJECT)

    @property
    def base(self) -> str:
        return f"/v1/projects/{self.project_id}/ledger"


async def _apply(api: Api, project_id: UUID, at: datetime, *inputs: RecipeInput) -> None:
    context = RecipeContext(project_id, at, correlation_id=CORRELATION)
    async with UnitOfWork(api.factory) as uow:
        await apply_recipes(uow, inputs, context=context)


async def seed_project(api: Api, project_id: str, owner: Operator) -> Seeded:
    """R1 -> R2/R3 -> R4 golden flow (R$200, fee 1.99, 75/25), an R9 dispute opened and won
    (reversal link) and an R8 internal-split refund left pending (two-phase)."""
    seeded = Seeded(owner, project_id)
    pid = seeded.uuid
    invoice, payment, refund, dispute = (UUID(int=n) for n in (10, 11, 12, 13))
    parts = SplitAllocation((PayeeAmount(PAYEE, brl(14851)),), brl(5149))
    await _apply(api, pid, T1, InvoiceFinalized(invoice, CUSTOMER, brl(20000), brl(0)))
    await _apply(
        api,
        pid,
        T2,
        PaymentCaptured(payment, CUSTOMER, PROVIDER_ACCOUNT, brl(20000), brl(20000), brl(199)),
        SplitAllocated(
            payment,
            brl(20000),
            brl(19801),
            FeeBearer.PROPORTIONAL,
            SplitRule((SplitShare(PAYEE, 75),), 25),
        ),
    )
    await _apply(
        api,
        pid,
        T3,
        PaymentSettled(payment, PROVIDER_ACCOUNT, brl(19801), (PayeeAmount(PAYEE, brl(14851)),)),
    )
    await _apply(api, pid, T4, DisputeOpened(dispute, PROVIDER_ACCOUNT, brl(5000), parts))
    await _apply(api, pid, T4, DisputeWon(dispute))
    await _apply(
        api,
        pid,
        T5,
        RefundRequested(
            refund_id=refund,
            payment_id=payment,
            provider_account_id=PROVIDER_ACCOUNT,
            amount=brl(20000),
            gross=brl(20000),
            already_refunded=brl(0),
            settled=True,
            original=parts,
        ),
    )
    return seeded


async def complete_refund(api: Api, seeded: Seeded) -> None:
    """Post the pending R8 entry (``RefundCompleted DONE``)."""
    await _apply(api, seeded.uuid, T5, RefundCompleted(UUID(int=12), RefundOutcome.DONE))


def customer_id() -> str:
    return format_id(IdPrefix.CUSTOMER, CUSTOMER)


def payee_id() -> str:
    return format_id(IdPrefix.PAYEE, PAYEE)


def provider_account_id() -> str:
    return format_id(IdPrefix.PROVIDER_ACCOUNT, PROVIDER_ACCOUNT)

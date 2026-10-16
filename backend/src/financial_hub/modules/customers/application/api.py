"""Public API of customers for other modules (ARCHITECTURE s3): import only from here.

Ledger and wallet code identify a customer's money by :func:`customer_account_owner`, always the
*project-scoped identity*, never the universal customer: the same person in two projects owns two
unrelated wallets (ADR-0011). Provider customer mappings are stored here and used by provider
adapters later; nothing in this module calls a provider.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.customers.infrastructure import repository
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import Conflict, NotFound
from financial_hub.shared.ids import IdPrefix, format_id

__all__ = [
    "CustomerAccountOwner",
    "customer_account_owner",
    "find_identity_by_provider_customer",
    "get_provider_customer",
    "link_provider_customer",
]


@dataclass(frozen=True, slots=True)
class CustomerAccountOwner:
    """The owner of a customer's ledger accounts (``customer_credits:<identity uuid>``)."""

    project_id: UUID
    identity_id: UUID

    @property
    def owner_id(self) -> str:
        """The ``cus_`` id used as ``owner_id`` in ledger queries."""
        return format_id(IdPrefix.CUSTOMER, self.identity_id)


def customer_account_owner(project_id: UUID, identity_id: UUID) -> CustomerAccountOwner:
    return CustomerAccountOwner(project_id=project_id, identity_id=identity_id)


async def link_provider_customer(
    session: AsyncSession,
    clock: Clock,
    *,
    project_id: UUID,
    identity_id: UUID,
    provider_account_id: UUID,
    provider_customer_id: str,
) -> None:
    """Record that ``identity`` is ``provider_customer_id`` on a provider account of its project.

    Idempotent for the same mapping. ``NotFound`` when the identity is not in the project or the
    account is not one of the project's; ``Conflict`` when the identity already maps to another
    provider customer on that account, or the provider customer belongs to another identity.
    """
    if not await repository.identity_in_project(session, project_id, identity_id):
        raise NotFound("Customer not found.")
    try:
        async with session.begin_nested():
            inserted = await repository.insert_provider_customer(
                session,
                identity_id=identity_id,
                provider_account_id=provider_account_id,
                provider_customer_id=provider_customer_id,
                now=clock.now(),
            )
    except IntegrityError:
        # The trigger (account in another project) or the foreign key (unknown account).
        raise NotFound("Provider account not found.") from None
    if inserted:
        return
    current = await repository.get_provider_customer(session, identity_id, provider_account_id)
    if current != provider_customer_id:
        raise Conflict("The provider customer mapping already exists with a different value.")


async def get_provider_customer(
    session: AsyncSession, *, project_id: UUID, identity_id: UUID, provider_account_id: UUID
) -> str | None:
    if not await repository.identity_in_project(session, project_id, identity_id):
        return None
    return await repository.get_provider_customer(session, identity_id, provider_account_id)


async def find_identity_by_provider_customer(
    session: AsyncSession,
    *,
    project_id: UUID,
    provider_account_id: UUID,
    provider_customer_id: str,
) -> UUID | None:
    return await repository.find_identity_by_provider_customer(
        session, project_id, provider_account_id, provider_customer_id
    )

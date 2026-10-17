"""Ledger reads for reconciliation (ADR-0013): internal consistency and provider-side balances.

Read-only and lock-free, inside the caller's :class:`UnitOfWork`. Amounts are integer minor units.
"""

from collections.abc import Collection
from uuid import UUID

from financial_hub.modules.ledger.domain.accounts import (
    CODE_SEPARATOR,
    CUSTOMER_RECEIVABLE,
    PROVIDER_BALANCE,
    PROVIDER_BALANCE_HELD,
    SALES_CLEARING,
)
from financial_hub.modules.ledger.infrastructure import repository as repo
from financial_hub.modules.ledger.infrastructure.reconciliation_queries import (
    AccountDrift,
    Columns,
    CurrencyTrialBalance,
    Residue,
    drifted_accounts,
    residues,
    trial_balances,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.money import Currency

__all__ = [
    "AccountDrift",
    "Columns",
    "CurrencyTrialBalance",
    "LedgerReconciliationReader",
    "Residue",
]


class LedgerReconciliationReader:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def drifted_accounts(self, project_id: UUID) -> list[AccountDrift]:
        """Accounts whose materialized columns differ from the sums of their postings."""
        return await drifted_accounts(self._uow.session, project_id)

    async def trial_balances(self, project_id: UUID) -> list[CurrencyTrialBalance]:
        """One trial balance per currency the project has accounts in."""
        return await trial_balances(self._uow.session, project_id)

    async def clearing_residues(
        self, project_id: UUID, correlation_ids: Collection[UUID]
    ) -> list[Residue]:
        """Non-zero nets on ``sales_clearing`` / ``customer_receivable:*`` per correlation.

        For a fully paid standalone intent (R1+R2+R3 under ``correlation_id = intent.id``) both
        nets must be zero; anything returned here is a ``clearing_not_zero`` break.
        """
        found = await residues(
            self._uow.session,
            project_id,
            correlation_ids,
            exact_code=SALES_CLEARING.code(),
            code_prefix=f"{CUSTOMER_RECEIVABLE.name}{CODE_SEPARATOR}",
        )
        return [residue for residue in found if residue.net_debit != 0]

    async def provider_balance(
        self, project_id: UUID, provider_account_id: UUID, currency: Currency
    ) -> tuple[int, int]:
        """Posted ``(available, held)`` of ``provider_balance[_held]:{pa}``; 0 if never booked."""
        values: list[int] = []
        for template in (PROVIDER_BALANCE, PROVIDER_BALANCE_HELD):
            code = template.code(provider_account_id)
            account = await repo.find_account(self._uow.session, project_id, code, currency)
            values.append(0 if account is None else account.balance.posted.minor_units)
        return values[0], values[1]

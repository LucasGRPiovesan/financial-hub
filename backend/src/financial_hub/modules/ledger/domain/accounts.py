"""Chart of accounts (FINANCIAL_MODEL s3): typed templates, provisioned lazily per project/currency.

A template names an account family (``customer_receivable``) with its type, normal side, flags
and owner kind. :meth:`AccountTemplate.spec` turns it into the concrete :class:`AccountSpec` of
one owner (``customer_receivable:<customer uuid>``). The project comes from the entry and the
currency from the posting amount, so the stored identity is ``(project, code, currency)``: the
same customer in two projects always gets two distinct, independent accounts.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NewType
from uuid import UUID

from financial_hub.modules.ledger.domain.model import AccountSpec, AccountType, NormalBalance

CustomerId = NewType("CustomerId", UUID)
"""Opaque customer reference (owner of receivable, wallet and unapplied-cash accounts)."""
PayeeId = NewType("PayeeId", UUID)
"""Opaque payee reference (seller/professional of the project)."""
ProviderAccountId = NewType("ProviderAccountId", UUID)
"""Opaque reference of the project's account at the payment provider."""

CODE_SEPARATOR: Final = ":"


class OwnerKind(StrEnum):
    """Who an account belongs to inside the project; ``PROJECT`` means the platform itself."""

    PROJECT = "project"
    CUSTOMER = "customer"
    PAYEE = "payee"
    PROVIDER_ACCOUNT = "provider_account"


@dataclass(frozen=True, slots=True)
class AccountTemplate:
    name: str
    type: AccountType
    owner: OwnerKind
    no_overdraft: bool = False

    @property
    def normal_balance(self) -> NormalBalance:
        return self.type.normal_balance

    def code(self, owner_id: UUID | None = None) -> str:
        self._check_owner(owner_id)
        if owner_id is None:
            return self.name
        return f"{self.name}{CODE_SEPARATOR}{owner_id}"

    def spec(self, owner_id: UUID | None = None) -> AccountSpec:
        """Concrete account of ``owner_id`` (``None`` for project-level accounts)."""
        return AccountSpec(
            code=self.code(owner_id),
            type=self.type,
            no_overdraft=self.no_overdraft,
            customer_id=owner_id if self.owner is OwnerKind.CUSTOMER else None,
            payee_id=owner_id if self.owner is OwnerKind.PAYEE else None,
            provider_account_id=owner_id if self.owner is OwnerKind.PROVIDER_ACCOUNT else None,
        )

    def _check_owner(self, owner_id: UUID | None) -> None:
        if self.owner is OwnerKind.PROJECT:
            if owner_id is not None:
                raise ValueError(f"{self.name} is a project account and takes no owner")
        elif not isinstance(owner_id, UUID):
            raise TypeError(f"{self.name} needs a {self.owner} id (UUID)")


# --- FINANCIAL_MODEL s3, in table order ----------------------------------------------------------

CUSTOMER_RECEIVABLE: Final = AccountTemplate(
    "customer_receivable", AccountType.ASSET, OwnerKind.CUSTOMER
)
CUSTOMER_CREDITS: Final = AccountTemplate(
    "customer_credits", AccountType.LIABILITY, OwnerKind.CUSTOMER, no_overdraft=True
)
CUSTOMER_UNAPPLIED_CASH: Final = AccountTemplate(
    "customer_unapplied_cash", AccountType.LIABILITY, OwnerKind.CUSTOMER
)
SALES_CLEARING: Final = AccountTemplate("sales_clearing", AccountType.LIABILITY, OwnerKind.PROJECT)
PROVIDER_RECEIVABLE: Final = AccountTemplate(
    "provider_receivable", AccountType.ASSET, OwnerKind.PROVIDER_ACCOUNT
)
PROVIDER_BALANCE: Final = AccountTemplate(
    "provider_balance", AccountType.ASSET, OwnerKind.PROVIDER_ACCOUNT
)
PROVIDER_BALANCE_HELD: Final = AccountTemplate(
    "provider_balance_held", AccountType.ASSET, OwnerKind.PROVIDER_ACCOUNT
)
PAYEE_PAYABLE_PENDING: Final = AccountTemplate(
    "payee_payable_pending", AccountType.LIABILITY, OwnerKind.PAYEE
)
PAYEE_PAYABLE_AVAILABLE: Final = AccountTemplate(
    "payee_payable_available", AccountType.LIABILITY, OwnerKind.PAYEE, no_overdraft=True
)
PAYEE_PAYABLE_HELD: Final = AccountTemplate(
    "payee_payable_held", AccountType.LIABILITY, OwnerKind.PAYEE
)
PAYEE_RECEIVABLE: Final = AccountTemplate("payee_receivable", AccountType.ASSET, OwnerKind.PAYEE)
PLATFORM_REVENUE: Final = AccountTemplate("platform_revenue", AccountType.INCOME, OwnerKind.PROJECT)
OTHER_INCOME: Final = AccountTemplate("other_income", AccountType.INCOME, OwnerKind.PROJECT)
DISCOUNTS_GIVEN: Final = AccountTemplate("discounts_given", AccountType.EXPENSE, OwnerKind.PROJECT)
PROVIDER_FEES: Final = AccountTemplate("provider_fees", AccountType.EXPENSE, OwnerKind.PROJECT)
CHARGEBACK_LOSSES: Final = AccountTemplate(
    "chargeback_losses", AccountType.EXPENSE, OwnerKind.PROJECT
)
OPENING_BALANCE: Final = AccountTemplate("opening_balance", AccountType.EQUITY, OwnerKind.PROJECT)

CHART_OF_ACCOUNTS: Final[tuple[AccountTemplate, ...]] = (
    CUSTOMER_RECEIVABLE,
    CUSTOMER_CREDITS,
    CUSTOMER_UNAPPLIED_CASH,
    SALES_CLEARING,
    PROVIDER_RECEIVABLE,
    PROVIDER_BALANCE,
    PROVIDER_BALANCE_HELD,
    PAYEE_PAYABLE_PENDING,
    PAYEE_PAYABLE_AVAILABLE,
    PAYEE_PAYABLE_HELD,
    PAYEE_RECEIVABLE,
    PLATFORM_REVENUE,
    OTHER_INCOME,
    DISCOUNTS_GIVEN,
    PROVIDER_FEES,
    CHARGEBACK_LOSSES,
    OPENING_BALANCE,
)

_BY_NAME: Final[dict[str, AccountTemplate]] = {t.name: t for t in CHART_OF_ACCOUNTS}


def template_of(code: str) -> AccountTemplate:
    """Template of a concrete account code (``payee_receivable:<uuid>`` -> ``PAYEE_RECEIVABLE``)."""
    name, _, owner = code.partition(CODE_SEPARATOR)
    template = _BY_NAME.get(name)
    if template is None:
        raise ValueError(f"{code!r} is not in the chart of accounts")
    if template.code(UUID(owner) if owner else None) != code:
        raise ValueError(f"{code!r} is not a canonical {template.name} code")
    return template

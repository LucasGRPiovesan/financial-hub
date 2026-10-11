from uuid import UUID

import pytest

from financial_hub.modules.ledger.domain.accounts import (
    CHART_OF_ACCOUNTS,
    CUSTOMER_CREDITS,
    CUSTOMER_RECEIVABLE,
    PAYEE_PAYABLE_AVAILABLE,
    PLATFORM_REVENUE,
    PROVIDER_BALANCE,
    AccountTemplate,
    OwnerKind,
    template_of,
)
from financial_hub.modules.ledger.domain.model import AccountSpec, AccountType, NormalBalance

OWNER = UUID("00000000-0000-0000-0000-00000000000a")

# FINANCIAL_MODEL s3, in table order: (name, type, owner, no_overdraft)
S3 = [
    ("customer_receivable", AccountType.ASSET, OwnerKind.CUSTOMER, False),
    ("customer_credits", AccountType.LIABILITY, OwnerKind.CUSTOMER, True),
    ("customer_unapplied_cash", AccountType.LIABILITY, OwnerKind.CUSTOMER, False),
    ("sales_clearing", AccountType.LIABILITY, OwnerKind.PROJECT, False),
    ("provider_receivable", AccountType.ASSET, OwnerKind.PROVIDER_ACCOUNT, False),
    ("provider_balance", AccountType.ASSET, OwnerKind.PROVIDER_ACCOUNT, False),
    ("provider_balance_held", AccountType.ASSET, OwnerKind.PROVIDER_ACCOUNT, False),
    ("payee_payable_pending", AccountType.LIABILITY, OwnerKind.PAYEE, False),
    ("payee_payable_available", AccountType.LIABILITY, OwnerKind.PAYEE, True),
    ("payee_payable_held", AccountType.LIABILITY, OwnerKind.PAYEE, False),
    ("payee_receivable", AccountType.ASSET, OwnerKind.PAYEE, False),
    ("platform_revenue", AccountType.INCOME, OwnerKind.PROJECT, False),
    ("other_income", AccountType.INCOME, OwnerKind.PROJECT, False),
    ("discounts_given", AccountType.EXPENSE, OwnerKind.PROJECT, False),
    ("provider_fees", AccountType.EXPENSE, OwnerKind.PROJECT, False),
    ("chargeback_losses", AccountType.EXPENSE, OwnerKind.PROJECT, False),
    ("opening_balance", AccountType.EQUITY, OwnerKind.PROJECT, False),
]


def test_chart_matches_financial_model_s3() -> None:
    assert [(t.name, t.type, t.owner, t.no_overdraft) for t in CHART_OF_ACCOUNTS] == S3


def test_normal_balance_follows_type() -> None:
    assert CUSTOMER_RECEIVABLE.normal_balance is NormalBalance.DEBIT
    assert CUSTOMER_CREDITS.normal_balance is NormalBalance.CREDIT


def test_owned_spec_carries_owner_reference() -> None:
    assert CUSTOMER_CREDITS.spec(OWNER) == AccountSpec(
        f"customer_credits:{OWNER}", AccountType.LIABILITY, no_overdraft=True, customer_id=OWNER
    )
    assert PAYEE_PAYABLE_AVAILABLE.spec(OWNER).payee_id == OWNER
    assert PROVIDER_BALANCE.spec(OWNER).provider_account_id == OWNER
    assert PLATFORM_REVENUE.spec() == AccountSpec("platform_revenue", AccountType.INCOME)


def test_owner_kind_is_enforced() -> None:
    with pytest.raises(ValueError, match="takes no owner"):
        PLATFORM_REVENUE.code(OWNER)
    with pytest.raises(TypeError, match="needs a customer id"):
        CUSTOMER_RECEIVABLE.code()
    with pytest.raises(TypeError):
        CUSTOMER_RECEIVABLE.code(str(OWNER))  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("template", CHART_OF_ACCOUNTS)
def test_template_of_round_trips(template: AccountTemplate) -> None:
    code = template.code() if template.owner is OwnerKind.PROJECT else template.code(OWNER)
    assert template_of(code) is template


@pytest.mark.parametrize(
    "code",
    [
        "cash",
        "platform_revenue:" + str(OWNER),
        "customer_receivable",
        "customer_receivable:" + str(OWNER).upper(),
    ],
)
def test_template_of_rejects_unknown_or_non_canonical(code: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        template_of(code)

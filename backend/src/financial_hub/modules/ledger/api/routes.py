"""``/v1/projects/{project_id}/ledger``: read-only ledger API (PRO-36).

Every route is GET and requires ``ledger:read`` (``require()`` turns non-members into 404,
ADR-0011). Amounts are integer minor units next to their ISO 4217 code
(``{"amount_minor": 1990, "currency": "BRL"}``): never a float. Ids carry their TypeID prefix
(``acc_``, ``je_``, ``pst_``, ``cus_``...). Each handler runs one read-only Unit of Work.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from financial_hub.modules.identity.application.api import Permission
from financial_hub.modules.ledger.application.queries import (
    AccountBalanceAsOfView,
    AccountType,
    AccountView,
    EntryStatus,
    EntryView,
    LedgerQueryService,
    TrialBalanceView,
)
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope, require
from financial_hub.shared.pagination import DEFAULT_LIMIT, MAX_LIMIT, Page, PageParams

router = APIRouter(prefix="/v1/projects/{project_id}/ledger", tags=["ledger"])

CurrentScope = Annotated[ProjectScope, require(Permission.LEDGER_READ)]


def _params(
    cursor: Annotated[str | None, Query(description="Opaque cursor from `next_cursor`.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Page size.")] = DEFAULT_LIMIT,
) -> PageParams:
    return PageParams(cursor=cursor, limit=limit)


Pagination = Annotated[PageParams, Depends(_params)]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MoneyOut(_Out):
    amount_minor: int = Field(description="Integer minor units of the currency (no float).")
    currency: str = Field(description="ISO 4217 code, e.g. BRL.")


class BalanceOut(_Out):
    posted: MoneyOut = Field(description="Posted balance on the account's normal side.")
    pending: MoneyOut = Field(description="Net of open two-phase holds on the normal side.")
    available: MoneyOut = Field(description="Posted minus pending outflows.")


class AccountOut(_Out):
    id: str
    code: str
    type: AccountType
    normal_balance: str
    currency: str
    no_overdraft: bool
    customer_id: str | None
    payee_id: str | None
    provider_account_id: str | None
    balance: BalanceOut
    created_at: datetime


class AccountBalanceAsOfOut(_Out):
    account_id: str
    currency: str
    as_of: datetime
    posted: MoneyOut = Field(description="Posted balance on the account's normal side at as_of.")
    debits: MoneyOut
    credits: MoneyOut


class PostingOut(_Out):
    id: str
    account_id: str
    account_code: str
    direction: str
    amount: MoneyOut


class EntryOut(_Out):
    id: str
    status: EntryStatus
    recipe: str
    idempotency_key: str
    effective_at: datetime
    created_at: datetime
    reverses_entry_id: str | None
    reversed_by_entry_id: str | None
    correlation_id: str | None = Field(
        description="Plain UUID; the chain is in the events explorer."
    )
    causation_id: str | None = Field(description="Parent event id (evt_).")
    metadata: dict[str, object]


class EntryDetailOut(EntryOut):
    postings: list[PostingOut]


class TrialBalanceLineOut(_Out):
    currency: str
    type: AccountType
    code: str = Field(description="Account family (code before ':'), e.g. customer_receivable.")
    debits: int = Field(description="Posted debits in minor units.")
    credits: int = Field(description="Posted credits in minor units.")


class TrialBalanceTotalOut(_Out):
    currency: str
    debits: int
    credits: int
    balanced: bool


class TrialBalanceOut(_Out):
    as_of: datetime | None
    balanced: bool = Field(description="True when debits equal credits in every currency.")
    totals: list[TrialBalanceTotalOut]
    lines: list[TrialBalanceLineOut]


def _uow(request: Request) -> UnitOfWork:
    return UnitOfWork(request.app.state.session_factory)


def _page[V, O: BaseModel](page: Page[V], model: type[O]) -> Page[O]:
    return Page[model](  # pyright: ignore[reportInvalidTypeArguments]
        data=[model.model_validate(item) for item in page.data],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get(
    "/accounts",
    summary="List ledger accounts",
    description=(
        "Accounts of the project with their current balance, oldest first (cursor pagination, "
        "stable order). `owner_id` matches the customer (`cus_`), payee (`pay_`) or provider "
        "account (`pva_`) the account belongs to."
    ),
)
async def list_accounts(
    scope: CurrentScope,
    params: Pagination,
    request: Request,
    code: Annotated[str | None, Query(description="Exact account code.")] = None,
    type: Annotated[AccountType | None, Query(description="Account type.")] = None,
    owner_id: Annotated[str | None, Query(description="cus_, pay_ or pva_ id.")] = None,
    currency: Annotated[str | None, Query(description="ISO 4217 code.")] = None,
) -> Page[AccountOut]:
    async with _uow(request) as uow:
        page = await LedgerQueryService(uow).list_accounts(
            scope.project_id,
            params,
            code=code,
            account_type=type,
            owner_id=owner_id,
            currency=currency,
        )
    return _page(page, AccountOut)


@router.get(
    "/accounts/{account_id}",
    summary="Get a ledger account",
    description="One account with its current posted, pending and available balance.",
)
async def get_account(scope: CurrentScope, account_id: str, request: Request) -> AccountOut:
    async with _uow(request) as uow:
        view: AccountView = await LedgerQueryService(uow).get_account(scope.project_id, account_id)
    return AccountOut.model_validate(view)


@router.get(
    "/accounts/{account_id}/balance",
    summary="Account balance as of a point in time",
    description=(
        "Recomputed from the postings of entries effective at or before `as_of` (RFC 3339 with "
        "offset). Only entries that are `posted` count: pending (two-phase) and archived entries "
        "are excluded, so holds never move this balance. Entry status history is not stored, so "
        "an entry that is posted now counts at its effective time."
    ),
)
async def get_account_balance(
    scope: CurrentScope,
    account_id: str,
    as_of: Annotated[datetime, Query(description="RFC 3339 timestamp with a UTC offset.")],
    request: Request,
) -> AccountBalanceAsOfOut:
    async with _uow(request) as uow:
        view: AccountBalanceAsOfView = await LedgerQueryService(uow).balance_as_of(
            scope.project_id, account_id, as_of
        )
    return AccountBalanceAsOfOut.model_validate(view)


@router.get(
    "/entries",
    summary="List journal entries",
    description=(
        "Journal entries, newest first (cursor pagination). `account_id` keeps entries with a "
        "posting on that account; `effective_from`/`effective_to` bound `effective_at` "
        "(inclusive)."
    ),
)
async def list_entries(
    scope: CurrentScope,
    params: Pagination,
    request: Request,
    account_id: Annotated[str | None, Query(description="acc_ id.")] = None,
    recipe: Annotated[str | None, Query(description="Recipe code, e.g. R2.")] = None,
    status: Annotated[EntryStatus | None, Query()] = None,
    correlation_id: Annotated[str | None, Query(description="Correlation UUID.")] = None,
    effective_from: Annotated[datetime | None, Query()] = None,
    effective_to: Annotated[datetime | None, Query()] = None,
) -> Page[EntryOut]:
    async with _uow(request) as uow:
        page = await LedgerQueryService(uow).list_entries(
            scope.project_id,
            params,
            account_id=account_id,
            recipe=recipe,
            status=status,
            correlation_id=correlation_id,
            effective_from=effective_from,
            effective_to=effective_to,
        )
    return _page(page, EntryOut)


@router.get(
    "/entries/{entry_id}",
    summary="Get a journal entry",
    description=(
        "One entry with its postings, metadata, idempotency key, status, reversal links and the "
        "correlation/causation ids (the event chain itself is in the events explorer)."
    ),
)
async def get_entry(scope: CurrentScope, entry_id: str, request: Request) -> EntryDetailOut:
    async with _uow(request) as uow:
        view: EntryView = await LedgerQueryService(uow).get_entry(scope.project_id, entry_id)
    return EntryDetailOut.model_validate(view)


@router.get(
    "/trial-balance",
    summary="Trial balance",
    description=(
        "Posted debits and credits per currency, account type and account family. Without "
        "`as_of` the current materialized balances are summed; with it the totals are recomputed "
        "from posted entries effective at or before `as_of`. `balanced` is true when debits "
        "equal credits in every currency."
    ),
)
async def get_trial_balance(
    scope: CurrentScope,
    request: Request,
    currency: Annotated[str | None, Query(description="ISO 4217 code; default all.")] = None,
    as_of: Annotated[datetime | None, Query(description="RFC 3339 with offset.")] = None,
) -> TrialBalanceOut:
    async with _uow(request) as uow:
        view: TrialBalanceView = await LedgerQueryService(uow).trial_balance(
            scope.project_id, currency=currency, as_of=as_of
        )
    return TrialBalanceOut.model_validate(view)

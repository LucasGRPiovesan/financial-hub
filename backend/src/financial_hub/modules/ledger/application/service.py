"""Ledger use cases: the only way other modules write to the ledger (ADR-0003).

Every method runs inside the caller's active :class:`UnitOfWork` (one transaction). Writes follow
the lock protocol of ADR-0003 s5: business aggregate first (``aggregate_lock``), then the union of
all accounts of the unit of work, provisioned and locked **once** in account-id order. A second
locking write in the same unit of work is refused because it could lock out of order.
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.ledger.domain import (
    Account,
    AccountSpec,
    Balance,
    BalanceDelta,
    EntryDraft,
    EntryStatus,
    JournalEntry,
    JsonValue,
    Posting,
    PostingDraft,
    TrialBalance,
    balance_deltas,
    metadata_to_json,
)
from financial_hub.modules.ledger.infrastructure import repository as repo
from financial_hub.platform.db import UnitOfWork
from financial_hub.shared.errors import (
    Conflict,
    IdempotencyKeyReused,
    InsufficientFunds,
    NotFound,
    ValidationFailed,
)
from financial_hub.shared.ids import new_uuid
from financial_hub.shared.money import Currency

type AggregateLock = Callable[[], Awaitable[None]]
"""Locks the business aggregate row (``FOR UPDATE``) before any ledger account (ADR-0003 s5)."""

_WRITE_MARK = "financial_hub.ledger.write_locked"


class LedgerService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    # --- writes --------------------------------------------------------------------------------

    async def post_entries(
        self, entries: Sequence[EntryDraft], *, aggregate_lock: AggregateLock | None = None
    ) -> list[JournalEntry]:
        """Write every entry of the unit of work atomically; one result per input, same order.

        Idempotent per ``(project, idempotency_key)``: a replay with the same payload hash returns
        the stored entry (and changes nothing); another payload raises ``IdempotencyKeyReused``.
        ``no_overdraft`` accounts are checked against ``available`` after all deltas, under lock.
        """
        if not entries:
            return []
        project_id = self._single_project(entries)
        unique = self._dedupe(entries)
        session = self._begin_write()
        if aggregate_lock is not None:
            await aggregate_lock()

        accounts, ids = await self._provision_and_lock(project_id, unique.values())

        existing = await repo.entry_ids_by_key(session, project_id, unique)
        stored: dict[str, UUID] = {}
        new: list[EntryDraft] = []
        for key, draft in sorted(unique.items()):
            if key in existing:
                stored[key] = self._replayed(draft, *existing[key])
            else:
                new.append(draft)
        await self._check_reversals(new)

        legs = {
            draft.idempotency_key: [
                Posting(new_uuid(), ids[p.key], p.direction, p.amount) for p in draft.postings
            ]
            for draft in new
        }
        deltas: dict[UUID, BalanceDelta] = {}
        for draft in new:
            for account_id, delta in balance_deltas(
                ((p.account_id, p.direction, p.amount) for p in legs[draft.idempotency_key]),
                None,
                draft.status,
            ).items():
                deltas[account_id] = deltas.get(account_id, BalanceDelta()) + delta
        self._check_no_overdraft(accounts, deltas)

        for draft in new:
            entry_id = new_uuid()
            inserted = await repo.insert_entry(
                session,
                entry_id=entry_id,
                project_id=project_id,
                status=draft.status,
                idempotency_key=draft.idempotency_key,
                payload_hash=draft.payload_hash,
                recipe=draft.recipe,
                effective_at=draft.effective_at,
                reverses_entry_id=draft.reverses_entry_id,
                correlation_id=draft.correlation_id,
                causation_id=draft.causation_id,
                metadata=metadata_to_json(draft.metadata),
            )
            if not inserted:
                # Unreachable while writers of one key lock the same accounts; kept as a guard.
                raced = await repo.entry_ids_by_key(session, project_id, [draft.idempotency_key])
                stored[draft.idempotency_key] = self._replayed(draft, *raced[draft.idempotency_key])
                continue
            await repo.insert_postings(
                session,
                entry_id=entry_id,
                project_id=project_id,
                postings=legs[draft.idempotency_key],
            )
            await repo.apply(session, entry_id)
            stored[draft.idempotency_key] = entry_id

        loaded = await repo.load_entries(session, project_id, stored.values())
        return [loaded[stored[draft.idempotency_key]] for draft in entries]

    async def create_pending(
        self, draft: EntryDraft, *, aggregate_lock: AggregateLock | None = None
    ) -> JournalEntry:
        """Two-phase hold: the entry moves only the pending columns until posted or voided."""
        if draft.status is not EntryStatus.PENDING:
            raise ValidationFailed(
                "create_pending needs a pending draft (EntryDraft.create(pending=True))"
            )
        (entry,) = await self.post_entries([draft], aggregate_lock=aggregate_lock)
        return entry

    async def post_pending(
        self, project_id: UUID, entry_id: UUID, *, aggregate_lock: AggregateLock | None = None
    ) -> JournalEntry:
        """``pending -> posted``; idempotent when already posted."""
        return await self._transition(project_id, entry_id, EntryStatus.POSTED, aggregate_lock)

    async def void_pending(
        self, project_id: UUID, entry_id: UUID, *, aggregate_lock: AggregateLock | None = None
    ) -> JournalEntry:
        """``pending -> archived`` (releases the hold); idempotent when already archived."""
        return await self._transition(project_id, entry_id, EntryStatus.ARCHIVED, aggregate_lock)

    async def reverse(
        self,
        project_id: UUID,
        entry_id: UUID,
        idempotency_key: str,
        *,
        effective_at: datetime,
        recipe: str = "reversal",
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
        aggregate_lock: AggregateLock | None = None,
    ) -> JournalEntry:
        """Post the mirror of a posted entry (``reverses_entry_id``); an entry is reversed once.

        Pending entries are voided, not reversed. ``effective_at`` is explicit so that a replay
        with the same key hashes the same.
        """
        session = self._uow.session
        original = await self.get_entry(project_id, entry_id)
        if original.status is not EntryStatus.POSTED:
            raise Conflict(f"only posted entries can be reversed; entry is {original.status}")
        specs = await repo.get_accounts(
            session, project_id, (p.account_id for p in original.postings)
        )
        draft = EntryDraft.create(
            project_id=project_id,
            idempotency_key=idempotency_key,
            recipe=recipe,
            postings=[
                PostingDraft(specs[p.account_id].spec, p.direction.opposite(), p.amount)
                for p in original.postings
            ],
            effective_at=effective_at,
            reverses_entry_id=entry_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            metadata=metadata,
        )
        if draft is None:  # pragma: no cover - stored postings are > 0
            raise Conflict("entry has nothing to reverse")
        (entry,) = await self.post_entries([draft], aggregate_lock=aggregate_lock)
        return entry

    async def provision_accounts(
        self, project_id: UUID, accounts: Iterable[tuple[AccountSpec, Currency]]
    ) -> dict[tuple[str, str], Account]:
        """Create missing accounts (no lock, no balance change); existing ones must match."""
        session = self._uow.session
        specs = self._merge_specs(((spec.code, currency.code), spec) for spec, currency in accounts)
        await repo.insert_missing_accounts(session, project_id, specs)
        ids = await repo.account_ids(session, project_id, specs)
        found = await repo.get_accounts(session, project_id, ids.values())
        result = {key: found[ids[key]] for key in specs}
        self._check_specs(specs, result)
        return result

    # --- reads ---------------------------------------------------------------------------------

    async def get_entry(self, project_id: UUID, entry_id: UUID) -> JournalEntry:
        entry = (await repo.load_entries(self._uow.session, project_id, [entry_id])).get(entry_id)
        if entry is None:
            raise NotFound(f"journal entry {entry_id} not found")
        return entry

    async def get_account(self, project_id: UUID, account_id: UUID) -> Account:
        account = await repo.get_account(self._uow.session, project_id, account_id)
        if account is None:
            raise NotFound(f"ledger account {account_id} not found")
        return account

    async def find_account(self, project_id: UUID, code: str, currency: Currency) -> Account | None:
        return await repo.find_account(self._uow.session, project_id, code, currency)

    async def get_balance(self, project_id: UUID, account_id: UUID) -> Balance:
        """Materialized balance (O(1), FINANCIAL_MODEL s2)."""
        return (await self.get_account(project_id, account_id)).balance

    async def recompute_balance_from_postings(self, project_id: UUID, account_id: UUID) -> Balance:
        """Balance rebuilt from postings; equals :meth:`get_balance` unless the ledger drifted."""
        account = await self.get_account(project_id, account_id)
        return await repo.recompute_balance(self._uow.session, account)

    async def trial_balance(self, project_id: UUID, currency: Currency) -> TrialBalance:
        """Sum of all balance columns of a project in one currency; ``is_balanced`` must hold."""
        return await repo.trial_balance(self._uow.session, project_id, currency)

    # --- internals -----------------------------------------------------------------------------

    def _begin_write(self) -> AsyncSession:
        session = self._uow.session
        if session.info.get(_WRITE_MARK):
            raise RuntimeError(
                "ledger accounts are already locked in this unit of work: collect every entry "
                "and call post_entries once (ADR-0003 s5)"
            )
        session.info[_WRITE_MARK] = True
        return session

    @staticmethod
    def _single_project(entries: Sequence[EntryDraft]) -> UUID:
        projects = {draft.project_id for draft in entries}
        if len(projects) != 1:
            raise ValidationFailed("all entries of one ledger write belong to one project")
        return next(iter(projects))

    @staticmethod
    def _dedupe(entries: Sequence[EntryDraft]) -> dict[str, EntryDraft]:
        unique: dict[str, EntryDraft] = {}
        for draft in entries:
            seen = unique.setdefault(draft.idempotency_key, draft)
            if seen.payload_hash != draft.payload_hash:
                raise IdempotencyKeyReused(
                    f"idempotency key {draft.idempotency_key!r} used twice with different payloads"
                )
        return unique

    @staticmethod
    def _merge_specs(
        items: Iterable[tuple[repo.AccountKey, AccountSpec]],
    ) -> dict[repo.AccountKey, AccountSpec]:
        specs: dict[repo.AccountKey, AccountSpec] = {}
        for key, spec in items:
            if specs.setdefault(key, spec) != spec:
                raise ValidationFailed(f"account {key[0]} ({key[1]}) has conflicting definitions")
        return specs

    @staticmethod
    def _check_specs(
        specs: Mapping[repo.AccountKey, AccountSpec], accounts: Mapping[repo.AccountKey, Account]
    ) -> None:
        for key, spec in specs.items():
            if accounts[key].spec != spec:
                raise Conflict(
                    f"account {key[0]} ({key[1]}) exists with a different definition "
                    f"({accounts[key].spec}), requested {spec}"
                )

    async def _provision_and_lock(
        self, project_id: UUID, drafts: Iterable[EntryDraft]
    ) -> tuple[dict[UUID, Account], dict[repo.AccountKey, UUID]]:
        session = self._uow.session
        specs = self._merge_specs((p.key, p.account) for d in drafts for p in d.postings)
        await repo.insert_missing_accounts(session, project_id, specs)
        ids = await repo.account_ids(session, project_id, specs)
        locked = await repo.lock_accounts(session, ids.values())
        self._check_specs(specs, {key: locked[account_id] for key, account_id in ids.items()})
        return locked, ids

    @staticmethod
    def _replayed(draft: EntryDraft, entry_id: UUID, payload_hash: str) -> UUID:
        if payload_hash != draft.payload_hash:
            raise IdempotencyKeyReused(
                f"idempotency key {draft.idempotency_key!r} was already used with another payload"
            )
        return entry_id

    async def _check_reversals(self, drafts: Sequence[EntryDraft]) -> None:
        targets = [d.reverses_entry_id for d in drafts if d.reverses_entry_id is not None]
        if len(targets) != len(set(targets)):
            raise Conflict("an entry is reversed at most once")
        for target in targets:
            # Only posted entries are reversed; a pending hold is voided (ADR-0003).
            if await repo.entry_status(self._uow.session, target) is not EntryStatus.POSTED:
                raise Conflict(f"journal entry {target} is not posted and cannot be reversed")
            if await repo.reversal_of(self._uow.session, target) is not None:
                raise Conflict(f"journal entry {target} is already reversed")

    @staticmethod
    def _check_no_overdraft(
        accounts: Mapping[UUID, Account], deltas: Mapping[UUID, BalanceDelta]
    ) -> None:
        for account_id, delta in sorted(deltas.items()):
            account = accounts[account_id]
            if not account.no_overdraft:
                continue
            available = account.balance.apply(delta).available
            if available.is_negative():
                raise InsufficientFunds(
                    f"account {account.code} ({account.currency}) would have available {available}"
                )

    async def _transition(
        self,
        project_id: UUID,
        entry_id: UUID,
        target: EntryStatus,
        aggregate_lock: AggregateLock | None,
    ) -> JournalEntry:
        entry = await self.get_entry(project_id, entry_id)
        session = self._begin_write()
        if aggregate_lock is not None:
            await aggregate_lock()
        accounts = await repo.lock_accounts(session, (p.account_id for p in entry.postings))
        # Re-read under the account locks: a concurrent transition has committed by now.
        status = await repo.entry_status(session, entry_id)
        current = replace(entry, status=status or entry.status)
        if not current.transition_to(target):
            return current
        self._check_no_overdraft(
            accounts,
            balance_deltas(
                ((p.account_id, p.direction, p.amount) for p in entry.postings),
                current.status,
                target,
            ),
        )
        if not await repo.set_status(session, entry_id, target):  # pragma: no cover - locked
            raise Conflict(f"journal entry {entry_id} changed concurrently")
        await repo.apply(session, entry_id)
        return replace(current, status=target)

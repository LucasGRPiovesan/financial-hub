"""Identity persistence: plain SQL on the caller's session (the Unit of Work owns the transaction).

Runs as ``fh_app`` (no DELETE anywhere: sessions are revoked, throttle rows are reset). Times that
drive behaviour (expiry, lockout, throttle windows) come from the caller's ``Clock`` as parameters;
``updated_at`` audit columns use the database clock. Session ids and throttle keys are SHA-256
digests computed by the caller: raw tokens, emails or addresses never reach these tables as keys.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from financial_hub.modules.identity.domain import (
    Operator,
    OperatorStatus,
    Role,
    Session,
    ThrottleScope,
    ThrottleState,
)

_SELECT_OPERATOR = """
    SELECT id, email, display_name, password_hash, status, failed_login_count, locked_until,
           password_changed_at, created_at
    FROM public.operators
"""

_OPERATOR_BY_EMAIL = text(_SELECT_OPERATOR + " WHERE email = :email")
_OPERATOR_BY_ID = text(_SELECT_OPERATOR + " WHERE id = :id")
_OPERATOR_BY_ID_FOR_UPDATE = text(_SELECT_OPERATOR + " WHERE id = :id FOR UPDATE")
_INSERT_OPERATOR = text(
    """
    INSERT INTO public.operators (id, email, display_name, password_hash, status, created_at,
                                  password_changed_at)
    VALUES (:id, :email, :display_name, :password_hash, 'active', :created_at, :created_at)
    ON CONFLICT (email) DO NOTHING
    RETURNING id
    """
)
_SET_LOGIN_STATE = text(
    """
    UPDATE public.operators
    SET failed_login_count = :failed_login_count, locked_until = :locked_until, updated_at = now()
    WHERE id = :id
    """
)
_REHASH_PASSWORD = text(
    """
    UPDATE public.operators SET password_hash = :new_hash, updated_at = now()
    WHERE id = :id AND password_hash = :old_hash
    """
)
_CHANGE_PASSWORD = text(
    """
    UPDATE public.operators
    SET password_hash = :password_hash, password_changed_at = :changed_at, updated_at = now()
    WHERE id = :id
    """
)

_ENSURE_THROTTLE = text(
    """
    INSERT INTO public.login_throttle (scope, key_hash, window_started_at, attempts, updated_at)
    VALUES (:scope, :key_hash, :now, 0, :now)
    ON CONFLICT (scope, key_hash) DO NOTHING
    """
)
_THROTTLE = text(
    """
    SELECT window_started_at, attempts, blocked_until FROM public.login_throttle
    WHERE scope = :scope AND key_hash = :key_hash
    """
)
_THROTTLE_FOR_UPDATE = text(
    """
    SELECT window_started_at, attempts, blocked_until FROM public.login_throttle
    WHERE scope = :scope AND key_hash = :key_hash
    FOR UPDATE
    """
)
_SAVE_THROTTLE = text(
    """
    UPDATE public.login_throttle
    SET window_started_at = :window_started_at, attempts = :attempts,
        blocked_until = :blocked_until, updated_at = :now
    WHERE scope = :scope AND key_hash = :key_hash
    """
)

_INSERT_SESSION = text(
    """
    INSERT INTO public.sessions (id, operator_id, csrf_token_hash, created_at, last_seen_at,
                                 idle_expires_at, absolute_expires_at, user_agent, ip)
    VALUES (:id, :operator_id, :csrf_token_hash, :created_at, :created_at, :idle_expires_at,
            :absolute_expires_at, :user_agent, CAST(:ip AS inet))
    """
)
_SESSION_BY_ID = text(
    """
    SELECT id, operator_id, csrf_token_hash, created_at, last_seen_at, idle_expires_at,
           absolute_expires_at, revoked_at
    FROM public.sessions WHERE id = :id
    """
)
_TOUCH_SESSION = text(
    """
    UPDATE public.sessions SET last_seen_at = :now, idle_expires_at = :idle_expires_at
    WHERE id = :id AND revoked_at IS NULL AND last_seen_at < :now
    """
)
_REVOKE_SESSION = text(
    "UPDATE public.sessions SET revoked_at = :now WHERE id = :id AND revoked_at IS NULL"
)
_REVOKE_OTHER_SESSIONS = text(
    """
    UPDATE public.sessions SET revoked_at = :now
    WHERE operator_id = :operator_id AND id <> :keep_id AND revoked_at IS NULL
    """
)

_MEMBERSHIPS = text(
    "SELECT project_id, role FROM public.project_memberships WHERE operator_id = :operator_id"
)
_INSERT_MEMBERSHIP = text(
    """
    INSERT INTO public.project_memberships (operator_id, project_id, role)
    VALUES (:operator_id, :project_id, :role)
    """
)


_PROJECT_MEMBERS_SQL = """
    SELECT m.operator_id, o.email, o.display_name, m.role, m.created_at
    FROM public.project_memberships m
    JOIN public.operators o ON o.id = m.operator_id
    WHERE m.project_id = :project_id
    ORDER BY m.created_at, m.operator_id
"""
_PROJECT_MEMBERS = text(_PROJECT_MEMBERS_SQL)
# FOR UPDATE OF m: serialises concurrent member changes of one project (last-owner invariant).
_PROJECT_MEMBERS_FOR_UPDATE = text(_PROJECT_MEMBERS_SQL + " FOR UPDATE OF m")
_SET_MEMBERSHIP_ROLE = text(
    """
    UPDATE public.project_memberships SET role = :role
    WHERE operator_id = :operator_id AND project_id = :project_id
    """
)
_DELETE_MEMBERSHIP = text(
    "DELETE FROM public.project_memberships WHERE operator_id = :operator_id "
    "AND project_id = :project_id"
)
_ACTIVE_DOMAIN_OPERATORS = text(
    _SELECT_OPERATOR + " WHERE status = 'active' AND lower(split_part(email, '@', 2)) = :domain "
    "ORDER BY email LIMIT :limit"
)
_MEMBERSHIPS_OF = text(
    "SELECT operator_id, project_id, role FROM public.project_memberships "
    "WHERE operator_id = ANY(:operator_ids)"
)


def _operator(row: RowMapping) -> Operator:
    return Operator(
        id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        password_hash=row["password_hash"],
        status=OperatorStatus(row["status"]),
        failed_login_count=row["failed_login_count"],
        locked_until=row["locked_until"],
        password_changed_at=row["password_changed_at"],
        created_at=row["created_at"],
    )


def _session(row: RowMapping) -> Session:
    return Session(
        id=bytes(row["id"]),
        operator_id=row["operator_id"],
        csrf_token_hash=bytes(row["csrf_token_hash"]),
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        idle_expires_at=row["idle_expires_at"],
        absolute_expires_at=row["absolute_expires_at"],
        revoked_at=row["revoked_at"],
    )


def _throttle(row: RowMapping) -> ThrottleState:
    return ThrottleState(
        window_started_at=row["window_started_at"],
        attempts=row["attempts"],
        blocked_until=row["blocked_until"],
    )


def _rowcount(result: Any) -> int:
    return int(result.rowcount)


# --- operators -----------------------------------------------------------------------------------


async def get_operator_by_email(session: AsyncSession, email: str) -> Operator | None:
    row = (await session.execute(_OPERATOR_BY_EMAIL, {"email": email})).mappings().first()
    return None if row is None else _operator(row)


async def get_operator(
    session: AsyncSession, operator_id: UUID, *, for_update: bool = False
) -> Operator | None:
    query = _OPERATOR_BY_ID_FOR_UPDATE if for_update else _OPERATOR_BY_ID
    row = (await session.execute(query, {"id": operator_id})).mappings().first()
    return None if row is None else _operator(row)


async def insert_operator(
    session: AsyncSession,
    *,
    operator_id: UUID,
    email: str,
    display_name: str,
    password_hash: str,
    created_at: datetime,
) -> bool:
    """``False`` when the email is already taken (nothing is written)."""
    result = await session.execute(
        _INSERT_OPERATOR,
        {
            "id": operator_id,
            "email": email,
            "display_name": display_name,
            "password_hash": password_hash,
            "created_at": created_at,
        },
    )
    return result.first() is not None


async def set_login_state(
    session: AsyncSession,
    operator_id: UUID,
    *,
    failed_login_count: int,
    locked_until: datetime | None,
) -> None:
    await session.execute(
        _SET_LOGIN_STATE,
        {"id": operator_id, "failed_login_count": failed_login_count, "locked_until": locked_until},
    )


async def rehash_password(
    session: AsyncSession, operator_id: UUID, *, old_hash: str, new_hash: str
) -> None:
    """Replace the hash only if it is still ``old_hash`` (a concurrent change wins)."""
    await session.execute(
        _REHASH_PASSWORD, {"id": operator_id, "old_hash": old_hash, "new_hash": new_hash}
    )


async def change_password(
    session: AsyncSession, operator_id: UUID, *, password_hash: str, changed_at: datetime
) -> None:
    await session.execute(
        _CHANGE_PASSWORD,
        {"id": operator_id, "password_hash": password_hash, "changed_at": changed_at},
    )


# --- login throttle ------------------------------------------------------------------------------


async def get_throttle(
    session: AsyncSession, scope: ThrottleScope, key_hash: bytes
) -> ThrottleState | None:
    params = {"scope": scope.value, "key_hash": key_hash}
    row = (await session.execute(_THROTTLE, params)).mappings().first()
    return None if row is None else _throttle(row)


async def lock_throttle(
    session: AsyncSession, scope: ThrottleScope, key_hash: bytes, now: datetime
) -> ThrottleState:
    """Create the counter if missing and lock it for this transaction (serializes attempts)."""
    params = {"scope": scope.value, "key_hash": key_hash}
    await session.execute(_ENSURE_THROTTLE, {**params, "now": now})
    row = (await session.execute(_THROTTLE_FOR_UPDATE, params)).mappings().one()
    return _throttle(row)


async def save_throttle(
    session: AsyncSession,
    scope: ThrottleScope,
    key_hash: bytes,
    state: ThrottleState,
    now: datetime,
) -> None:
    await session.execute(
        _SAVE_THROTTLE,
        {
            "scope": scope.value,
            "key_hash": key_hash,
            "window_started_at": state.window_started_at,
            "attempts": state.attempts,
            "blocked_until": state.blocked_until,
            "now": now,
        },
    )


# --- sessions ------------------------------------------------------------------------------------


async def insert_session(
    session: AsyncSession, record: Session, *, user_agent: str | None, ip: str | None
) -> None:
    await session.execute(
        _INSERT_SESSION,
        {
            "id": record.id,
            "operator_id": record.operator_id,
            "csrf_token_hash": record.csrf_token_hash,
            "created_at": record.created_at,
            "idle_expires_at": record.idle_expires_at,
            "absolute_expires_at": record.absolute_expires_at,
            "user_agent": user_agent,
            "ip": ip,
        },
    )


async def get_session(session: AsyncSession, session_id: bytes) -> Session | None:
    row = (await session.execute(_SESSION_BY_ID, {"id": session_id})).mappings().first()
    return None if row is None else _session(row)


async def touch_session(
    session: AsyncSession, session_id: bytes, *, now: datetime, idle_expires_at: datetime
) -> None:
    await session.execute(
        _TOUCH_SESSION, {"id": session_id, "now": now, "idle_expires_at": idle_expires_at}
    )


async def revoke_session(session: AsyncSession, session_id: bytes, now: datetime) -> bool:
    result = await session.execute(_REVOKE_SESSION, {"id": session_id, "now": now})
    return _rowcount(result) == 1


async def revoke_other_sessions(
    session: AsyncSession, operator_id: UUID, *, keep_id: bytes, now: datetime
) -> int:
    result = await session.execute(
        _REVOKE_OTHER_SESSIONS, {"operator_id": operator_id, "keep_id": keep_id, "now": now}
    )
    return _rowcount(result)


# --- memberships ---------------------------------------------------------------------------------


async def memberships(session: AsyncSession, operator_id: UUID) -> Mapping[UUID, Role]:
    rows = (await session.execute(_MEMBERSHIPS, {"operator_id": operator_id})).mappings()
    return {row["project_id"]: Role(row["role"]) for row in rows}


async def insert_membership(
    session: AsyncSession, *, operator_id: UUID, project_id: UUID, role: Role
) -> None:
    await session.execute(
        _INSERT_MEMBERSHIP,
        {"operator_id": operator_id, "project_id": project_id, "role": role.value},
    )


@dataclass(frozen=True, slots=True)
class MemberRow:
    operator_id: UUID
    email: str
    display_name: str
    role: Role
    added_at: datetime


@dataclass(frozen=True, slots=True)
class OperatorMembership:
    operator_id: UUID
    project_id: UUID
    role: Role


def _member(row: RowMapping) -> MemberRow:
    return MemberRow(
        operator_id=row["operator_id"],
        email=row["email"],
        display_name=row["display_name"],
        role=Role(row["role"]),
        added_at=row["created_at"],
    )


async def project_members(
    session: AsyncSession, project_id: UUID, *, for_update: bool = False
) -> list[MemberRow]:
    """Members of a project, oldest first (``for_update`` locks the membership rows)."""
    query = _PROJECT_MEMBERS_FOR_UPDATE if for_update else _PROJECT_MEMBERS
    rows = (await session.execute(query, {"project_id": project_id})).mappings()
    return [_member(row) for row in rows]


async def set_membership_role(
    session: AsyncSession, *, operator_id: UUID, project_id: UUID, role: Role
) -> None:
    await session.execute(
        _SET_MEMBERSHIP_ROLE,
        {"operator_id": operator_id, "project_id": project_id, "role": role.value},
    )


async def delete_membership(session: AsyncSession, *, operator_id: UUID, project_id: UUID) -> None:
    await session.execute(
        _DELETE_MEMBERSHIP, {"operator_id": operator_id, "project_id": project_id}
    )


async def list_active_operators_in_domain(
    session: AsyncSession, *, domain: str, limit: int
) -> list[Operator]:
    rows = (
        await session.execute(_ACTIVE_DOMAIN_OPERATORS, {"domain": domain, "limit": limit})
    ).mappings()
    return [_operator(row) for row in rows]


async def memberships_of(
    session: AsyncSession, operator_ids: Collection[UUID]
) -> list[OperatorMembership]:
    if not operator_ids:
        return []
    rows = (await session.execute(_MEMBERSHIPS_OF, {"operator_ids": list(operator_ids)})).mappings()
    return [
        OperatorMembership(
            operator_id=row["operator_id"], project_id=row["project_id"], role=Role(row["role"])
        )
        for row in rows
    ]

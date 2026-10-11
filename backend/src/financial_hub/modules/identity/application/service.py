"""Operator authentication use cases: login, session validation, logout, password change.

Security properties (docs/SECURITY.md, task 1.1):

- **No user enumeration.** Unknown email, wrong password, disabled or locked operator all give the
  same ``Unauthorized("Invalid email or password.")`` after the same Argon2 work (unknown emails
  verify against a dummy hash). The per-email throttle counts failures whether or not an operator
  owns the email, so a 429 reveals nothing either.
- **Throttling.** Every attempt counts against the client IP; failures count against the email.
  A blocked key gets ``RateLimited`` with ``Retry-After`` *before* any password check. Failure
  state is committed in its own transaction before the error is raised, so it survives the
  rollback a raised error would cause inside a Unit of Work.
- **Session fixation.** Every login mints a new random token and revokes the session of any token
  the client presented. Tokens are stored only as SHA-256 digests.
- **Expiry.** A session dies at ``idle_expires_at`` (sliding, written at most once per touch
  interval) or ``absolute_expires_at``, whichever comes first, or when revoked (logout, password
  change of another session).
- **CSRF.** Cookie-authenticated unsafe methods must echo the session's CSRF token in
  ``X-CSRF-Token``; checked in constant time.

Argon2 runs in a worker thread so it never blocks the event loop.
"""

import asyncio
import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_hub.modules.identity.application import tokens
from financial_hub.modules.identity.domain import (
    USER_AGENT_MAX_LENGTH,
    Operator,
    OperatorStatus,
    PasswordHasher,
    PasswordPolicyError,
    Role,
    Session,
    SessionPolicy,
    ThrottlePolicy,
    ThrottleScope,
    ThrottleState,
    is_valid_email,
    mask_email,
    normalize_email,
    permissions_for,
    throttle_key,
    validate_display_name,
    validate_new_password,
)
from financial_hub.modules.identity.infrastructure import repository
from financial_hub.modules.identity.infrastructure.passwords import Argon2PasswordHasher
from financial_hub.modules.projects.application.api import project_names
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.platform.settings import Settings
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import (
    Conflict,
    FieldError,
    Forbidden,
    RateLimited,
    Unauthorized,
    ValidationFailed,
)
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid

logger = structlog.get_logger()

OPERATOR_PRINCIPAL: Final = "operator"
INVALID_CREDENTIALS: Final = "Invalid email or password."
TOO_MANY_ATTEMPTS: Final = "Too many login attempts. Try again later."
UNKNOWN_IP: Final = "unknown"


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    session: SessionPolicy
    email_throttle: ThrottlePolicy
    ip_throttle: ThrottlePolicy

    @classmethod
    def from_settings(cls, settings: Settings) -> IdentityConfig:
        return cls(
            session=SessionPolicy(
                idle_timeout=timedelta(minutes=settings.session_idle_timeout_minutes),
                absolute_timeout=timedelta(hours=settings.session_absolute_timeout_hours),
                touch_interval=timedelta(seconds=settings.session_touch_interval_seconds),
            ),
            email_throttle=ThrottlePolicy(
                limit=settings.login_max_failures,
                window=timedelta(minutes=settings.login_failure_window_minutes),
                block=timedelta(minutes=settings.login_lockout_minutes),
            ),
            ip_throttle=ThrottlePolicy(
                limit=settings.login_ip_max_attempts,
                window=timedelta(minutes=settings.login_ip_window_minutes),
                block=timedelta(minutes=settings.login_ip_window_minutes),
            ),
        )


@dataclass(frozen=True, slots=True)
class OperatorView:
    id: str
    email: str
    display_name: str


@dataclass(frozen=True, slots=True)
class MembershipView:
    project_id: str
    project_name: str
    role: str


@dataclass(frozen=True, slots=True)
class LoginResult:
    operator: OperatorView
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class MeView:
    operator: OperatorView
    memberships: list[MembershipView]
    csrf_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    """An operator authenticated by a session cookie (implements ``Principal``)."""

    operator_id: UUID
    session_id: bytes = field(repr=False)
    csrf_token: str = field(repr=False)
    memberships: Mapping[UUID, Role]

    @property
    def kind(self) -> str:
        return OPERATOR_PRINCIPAL

    @property
    def subject_id(self) -> str:
        return format_id(IdPrefix.OPERATOR, self.operator_id)

    @property
    def project_ids(self) -> frozenset[UUID]:
        return frozenset(self.memberships)

    def scope_for(self, project_id: UUID) -> ProjectScope | None:
        role = self.memberships.get(project_id)
        if role is None:
            return None
        return ProjectScope(
            project_id=project_id,
            principal_kind=OPERATOR_PRINCIPAL,
            principal_id=self.subject_id,
            permissions=frozenset(str(permission) for permission in permissions_for(role)),
        )


def _operator_view(operator: Operator) -> OperatorView:
    return OperatorView(
        id=format_id(IdPrefix.OPERATOR, operator.id),
        email=operator.email,
        display_name=operator.display_name,
    )


def _client_ip(ip: str | None) -> str | None:
    """A storable (``inet``) address, or ``None``."""
    if ip is None:
        return None
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def _user_agent(user_agent: str | None) -> str | None:
    return user_agent[:USER_AGENT_MAX_LENGTH] if user_agent else None


def _invalid(pointer: str, detail: str, code: str = "invalid") -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.",
        errors=[FieldError(pointer=pointer, detail=detail, code=code)],
    )


class AuthService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        hasher: PasswordHasher,
        config: IdentityConfig,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self._hasher = hasher
        self._config = config

    def _uow(self) -> UnitOfWork:
        return UnitOfWork(self._factory)

    async def _verify(self, password_hash: str, password: str) -> bool:
        return await asyncio.to_thread(self._hasher.verify, password_hash, password)

    # --- login ---------------------------------------------------------------------------------

    async def login(
        self,
        *,
        email: str,
        password: str,
        ip: str | None,
        user_agent: str | None,
        presented_token: str | None = None,
    ) -> LoginResult:
        now = self._clock.now()
        normalized = normalize_email(email)
        address = _client_ip(ip)
        ip_key = throttle_key(ThrottleScope.IP, address or UNKNOWN_IP)
        email_key = throttle_key(ThrottleScope.EMAIL, normalized)

        # 1. Throttle gate (committed: the attempt counts even when it fails later).
        retry_after: int | None = None
        async with self._uow() as uow:
            session = uow.session
            ip_state = await repository.lock_throttle(session, ThrottleScope.IP, ip_key, now)
            retry_after = ip_state.retry_after(now)
            if retry_after is None:
                ip_state = ip_state.record(now, self._config.ip_throttle)
                await repository.save_throttle(session, ThrottleScope.IP, ip_key, ip_state, now)
                email_state = await repository.get_throttle(session, ThrottleScope.EMAIL, email_key)
                if email_state is not None:
                    retry_after = email_state.retry_after(now)
            operator = (
                await repository.get_operator_by_email(session, normalized)
                if retry_after is None
                else None
            )
        if retry_after is not None:
            logger.info("auth.login_failed", email=mask_email(normalized), reason="throttled")
            raise RateLimited(TOO_MANY_ATTEMPTS, retry_after_seconds=retry_after)

        # 2. Verify (always: the dummy hash keeps unknown emails on the same timing).
        candidate_hash = operator.password_hash if operator is not None else self._hasher.dummy_hash
        verified = await self._verify(candidate_hash, password)
        if (
            operator is None
            or not verified
            or operator.status is not OperatorStatus.ACTIVE
            or operator.is_locked(now)
        ):
            await self._record_failure(normalized, email_key, operator, now)
            reason = "unknown_email" if operator is None else "rejected"
            logger.info("auth.login_failed", email=mask_email(normalized), reason=reason)
            raise Unauthorized(INVALID_CREDENTIALS)

        new_hash = (
            await asyncio.to_thread(self._hasher.hash, password)
            if self._hasher.needs_rehash(operator.password_hash)
            else None
        )

        # 3. Success: reset counters, rotate the session.
        token, raw = tokens.new_session_token()
        csrf_token = tokens.csrf_token_for(raw)
        idle_expires_at, absolute_expires_at = self._config.session.expiries(now)
        record = Session(
            id=tokens.session_id(raw),
            operator_id=operator.id,
            csrf_token_hash=tokens.csrf_token_hash(csrf_token),
            created_at=now,
            last_seen_at=now,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
            revoked_at=None,
        )
        async with self._uow() as uow:
            session = uow.session
            current = await repository.get_operator(session, operator.id, for_update=True)
            if current is None or current.password_hash != operator.password_hash:
                current = None  # password changed concurrently: the verified one is stale
            else:
                email_state = await repository.lock_throttle(
                    session, ThrottleScope.EMAIL, email_key, now
                )
                if email_state.attempts or email_state.blocked_until is not None:
                    await repository.save_throttle(
                        session, ThrottleScope.EMAIL, email_key, ThrottleState.fresh(now), now
                    )
                if current.failed_login_count or current.locked_until is not None:
                    await repository.set_login_state(
                        session, current.id, failed_login_count=0, locked_until=None
                    )
                if new_hash is not None:
                    await repository.rehash_password(
                        session, current.id, old_hash=current.password_hash, new_hash=new_hash
                    )
                old = tokens.parse_session_token(presented_token)
                if old is not None:
                    await repository.revoke_session(session, tokens.session_id(old), now)
                await repository.insert_session(
                    session, record, user_agent=_user_agent(user_agent), ip=address
                )
        if current is None:
            logger.info("auth.login_failed", email=mask_email(normalized), reason="stale")
            raise Unauthorized(INVALID_CREDENTIALS)

        operator_view = _operator_view(operator)
        logger.info("auth.login_succeeded", operator_id=operator_view.id, rehashed=bool(new_hash))
        return LoginResult(
            operator=operator_view,
            session_token=token,
            csrf_token=csrf_token,
            expires_at=absolute_expires_at,
        )

    async def _record_failure(
        self, email: str, email_key: bytes, operator: Operator | None, now: datetime
    ) -> None:
        """Count one failure for the email (and its operator, in lockstep with the block)."""
        async with self._uow() as uow:
            session = uow.session
            state = await repository.lock_throttle(session, ThrottleScope.EMAIL, email_key, now)
            state = state.record(now, self._config.email_throttle)
            await repository.save_throttle(session, ThrottleScope.EMAIL, email_key, state, now)
            if operator is not None:
                current = await repository.get_operator(session, operator.id, for_update=True)
                if current is not None:
                    locked_until = (
                        state.blocked_until
                        if state.blocked_until is not None
                        else current.locked_until
                    )
                    await repository.set_login_state(
                        session,
                        current.id,
                        failed_login_count=current.failed_login_count + 1,
                        locked_until=locked_until,
                    )
        if state.blocked_until is not None:
            logger.warning("auth.login_locked", email=mask_email(email))

    # --- sessions ------------------------------------------------------------------------------

    async def authenticate(
        self, session_token: str | None, *, csrf_token: str | None, require_csrf: bool
    ) -> OperatorPrincipal | None:
        """The operator behind a session cookie, or ``None`` when it is absent/invalid/expired.

        Raises ``Forbidden`` when ``require_csrf`` and the CSRF token is missing or wrong.
        """
        raw = tokens.parse_session_token(session_token)
        if raw is None:
            return None
        now = self._clock.now()
        sid = tokens.session_id(raw)
        async with self._uow() as uow:
            session = uow.session
            record = await repository.get_session(session, sid)
            if record is None or not record.is_active(now):
                return None
            operator = await repository.get_operator(session, record.operator_id)
            if operator is None or operator.status is not OperatorStatus.ACTIVE:
                return None
            if require_csrf and not tokens.csrf_matches(record.csrf_token_hash, csrf_token):
                raise Forbidden("Missing or invalid CSRF token.")
            idle_expires_at = record.touch(now, self._config.session)
            if idle_expires_at is not None:
                await repository.touch_session(
                    session, sid, now=now, idle_expires_at=idle_expires_at
                )
            memberships = await repository.memberships(session, operator.id)
        return OperatorPrincipal(
            operator_id=operator.id,
            session_id=sid,
            csrf_token=tokens.csrf_token_for(raw),
            memberships=memberships,
        )

    async def logout(self, principal: OperatorPrincipal) -> None:
        async with self._uow() as uow:
            await repository.revoke_session(uow.session, principal.session_id, self._clock.now())
        logger.info("auth.logout", operator_id=principal.subject_id)

    async def me(self, principal: OperatorPrincipal) -> MeView:
        async with self._uow() as uow:
            session = uow.session
            operator = await repository.get_operator(session, principal.operator_id)
            if operator is None:  # pragma: no cover - operators are never deleted
                raise Unauthorized("Authentication required.")
            names = await project_names(session, principal.project_ids)
        memberships = sorted(
            (
                MembershipView(
                    project_id=format_id(IdPrefix.PROJECT, project_id),
                    project_name=names.get(project_id, ""),
                    role=role.value,
                )
                for project_id, role in principal.memberships.items()
            ),
            key=lambda membership: (membership.project_name.casefold(), membership.project_id),
        )
        return MeView(
            operator=_operator_view(operator),
            memberships=memberships,
            csrf_token=principal.csrf_token,
        )

    # --- password change -----------------------------------------------------------------------

    async def change_password(
        self, principal: OperatorPrincipal, *, current_password: str, new_password: str
    ) -> None:
        """Replace the password and revoke every other session of the operator."""
        try:
            validate_new_password(new_password)
        except PasswordPolicyError as error:
            raise _invalid("/new_password", str(error), "password_policy") from None
        now = self._clock.now()
        async with self._uow() as uow:
            operator = await repository.get_operator(uow.session, principal.operator_id)
            if operator is None:  # pragma: no cover - operators are never deleted
                raise Unauthorized("Authentication required.")
            email_key = throttle_key(ThrottleScope.EMAIL, operator.email)
            email_state = await repository.get_throttle(uow.session, ThrottleScope.EMAIL, email_key)
        retry_after = email_state.retry_after(now) if email_state is not None else None
        if retry_after is not None:
            raise RateLimited(TOO_MANY_ATTEMPTS, retry_after_seconds=retry_after)
        if not await self._verify(operator.password_hash, current_password):
            await self._record_failure(operator.email, email_key, operator, now)
            logger.info("auth.password_change_failed", operator_id=principal.subject_id)
            raise _invalid("/current_password", "incorrect password", "incorrect")

        new_hash = await asyncio.to_thread(self._hasher.hash, new_password)
        async with self._uow() as uow:
            session = uow.session
            current = await repository.get_operator(session, operator.id, for_update=True)
            if current is None or current.password_hash != operator.password_hash:
                raise Conflict("The password was changed concurrently; sign in again.")
            await repository.change_password(
                session, current.id, password_hash=new_hash, changed_at=now
            )
            revoked = await repository.revoke_other_sessions(
                session, current.id, keep_id=principal.session_id, now=now
            )
        logger.info(
            "auth.password_changed", operator_id=principal.subject_id, revoked_sessions=revoked
        )


async def create_operator(
    uow: UnitOfWork,
    *,
    hasher: PasswordHasher,
    clock: Clock,
    email: str,
    display_name: str,
    password: str,
) -> OperatorView:
    """Bootstrap/administrative creation of an operator inside the caller's Unit of Work."""
    normalized = normalize_email(email)
    if not is_valid_email(normalized):
        raise _invalid("/email", "not a valid email address")
    try:
        name = validate_display_name(display_name)
    except ValueError as error:
        raise _invalid("/display_name", str(error)) from None
    try:
        validate_new_password(password)
    except PasswordPolicyError as error:
        raise _invalid("/password", str(error), "password_policy") from None
    password_hash = await asyncio.to_thread(hasher.hash, password)
    operator_id = new_uuid()
    created = await repository.insert_operator(
        uow.session,
        operator_id=operator_id,
        email=normalized,
        display_name=name,
        password_hash=password_hash,
        created_at=clock.now(),
    )
    if not created:
        raise Conflict(f"An operator with email {normalized} already exists.")
    return OperatorView(
        id=format_id(IdPrefix.OPERATOR, operator_id), email=normalized, display_name=name
    )


def default_password_hasher() -> PasswordHasher:
    """Argon2id with the OWASP baseline parameters (see ``infrastructure.passwords``)."""
    return Argon2PasswordHasher()

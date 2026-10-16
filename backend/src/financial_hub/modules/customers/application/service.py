"""Customer use cases: upsert with per-project identities, reads, anonymization.

Design (docs/DOMAIN.md, docs/SECURITY.md):

- One universal ``financial_customers`` record per legal person, recognised across projects by
  ``document_hash`` (HMAC-SHA256 of the digits under a key derived from the API-key pepper). The
  document itself is AES-256-GCM sealed (AAD ``customer|<fcus id>``); this module can seal but not
  open it (import-linter contract 5).
- Each project sees only its own ``customer_identities`` row (``cus_`` id); the universal id and
  the person's other projects never leave this module.
- Concurrency: an advisory lock per ``(project, external_ref)`` serialises same-ref upserts and one
  per document hash serialises same-document upserts; ``ON CONFLICT`` clauses are the backstop.
- No personal data (name, email, phone, document, external_ref) is logged or put in an audit diff;
  events carry ids, the project's own external_ref and field names only.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from financial_hub.modules.audit.application import (
    REDACTED,
    Actor,
    AuditContext,
    AuditRecorder,
    AuditTarget,
    changed_fields,
)
from financial_hub.modules.customers.application import events as customer_events
from financial_hub.modules.customers.domain import (
    CustomerIdentity,
    CustomerKind,
    Document,
    derive_hash_key,
    document_aad,
    document_hash,
    mask_document,
    normalize_email,
    normalize_phone,
    parse_document,
    validate_external_ref,
    validate_metadata,
    validate_name,
)
from financial_hub.modules.customers.infrastructure import repository
from financial_hub.modules.events.application import EventDraft, EventPublisher
from financial_hub.platform.crypto.keyring import Keyring
from financial_hub.platform.crypto.seal import seal
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import Conflict, FieldError, Forbidden, NotFound, ValidationFailed
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid, parse_id
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page

logger = structlog.get_logger()

_EVENT_TYPES = customer_events  # importing the module registers the event types
_PII_FIELDS = ("name", "kind", "email", "phone", "metadata", "document")


@dataclass(frozen=True, slots=True)
class CustomerView:
    """What an API caller sees: the project's own identity. No universal id, no document."""

    id: str
    project_id: str
    external_ref: str
    kind: str
    name: str
    email: str | None
    phone: str | None
    metadata: dict[str, Any]
    document_masked: str | None
    anonymized: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UpsertResult:
    customer: CustomerView
    created: bool


def _view(identity: CustomerIdentity) -> CustomerView:
    masked = (
        None
        if identity.document_last4 is None
        else mask_document(identity.kind, identity.document_last4)
    )
    return CustomerView(
        id=format_id(IdPrefix.CUSTOMER, identity.id),
        project_id=format_id(IdPrefix.PROJECT, identity.project_id),
        external_ref=identity.external_ref,
        kind=identity.kind.value,
        name=identity.name,
        email=identity.email,
        phone=identity.phone,
        metadata=identity.metadata,
        document_masked=masked,
        anonymized=identity.anonymized_at is not None,
        created_at=identity.created_at,
        updated_at=identity.updated_at,
    )


def _invalid(pointer: str, error: ValueError) -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.",
        errors=[FieldError(pointer=pointer, detail=str(error), code="invalid")],
    )


class CustomerAnonymized(Conflict):
    """The identity was anonymized; its personal data is gone and cannot be written again (409)."""

    slug = "customer-anonymized"
    title = "Customer anonymized"


class DocumentChangeNotAllowed(Conflict):
    """The identity is already tied to a different document (409)."""

    slug = "customer-document-immutable"
    title = "Customer document cannot be changed"


class CustomerService:
    """Every method runs inside the caller's active :class:`UnitOfWork`."""

    def __init__(
        self,
        uow: UnitOfWork,
        *,
        clock: Clock,
        keyring: Keyring,
        pepper: bytes,
        audit: AuditContext | None = None,
        publisher: EventPublisher | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._keyring = keyring
        self._hash_key = derive_hash_key(pepper)
        self._audit = audit or AuditContext.system()
        self._publisher = publisher or EventPublisher(clock=clock, notifier=None)

    async def _record(
        self, scope: ProjectScope, action: str, identity_id: UUID, diff: dict[str, Any]
    ) -> None:
        await AuditRecorder.record(
            self._uow.session,
            actor=self._audit.acting(Actor.of_principal(scope.principal_kind, scope.principal_id)),
            action=action,
            target=AuditTarget("customer", format_id(IdPrefix.CUSTOMER, identity_id)),
            diff=diff,
            project_id=scope.project_id,
        )

    async def _publish(
        self, project_id: UUID, event_type: str, identity_id: UUID, **data: Any
    ) -> None:
        customer_id = format_id(IdPrefix.CUSTOMER, identity_id)
        await self._publisher.publish(
            self._uow,
            EventDraft(
                type=event_type,
                data={"customer_id": customer_id, **data},
                project_id=project_id,
                subject=customer_id,
            ),
        )

    def _seal_document(self, customer_id: UUID, document: Document) -> Any:
        return seal(self._keyring, document.digits.encode("ascii"), aad=document_aad(customer_id))

    async def _universal_for(
        self,
        *,
        kind: CustomerKind,
        name: str,
        email: str | None,
        phone: str | None,
        document: Document | None,
        digest: bytes | None,
        now: datetime,
    ) -> UUID:
        """The universal customer for a new identity: the document's owner, or a fresh record."""
        customer_id = new_uuid()
        if document is None or digest is None:
            return await repository.insert_universal(
                self._uow.session,
                customer_id=customer_id,
                kind=kind,
                name=name,
                email=email,
                phone=phone,
                document_hash=None,
                sealed=None,
                document_last4=None,
                now=now,
            )
        return await repository.insert_universal(
            self._uow.session,
            customer_id=customer_id,
            kind=kind,
            name=name,
            email=email,
            phone=phone,
            document_hash=digest,
            sealed=self._seal_document(customer_id, document),
            document_last4=document.last4,
            now=now,
        )

    async def upsert(
        self,
        scope: ProjectScope,
        external_ref: str,
        *,
        name: str,
        kind: str,
        email: str | None,
        phone: str | None,
        document: str | None,
        metadata: dict[str, Any],
    ) -> UpsertResult:
        try:
            ref = validate_external_ref(external_ref)
        except ValueError as error:
            raise _invalid("/external_ref", error) from None
        fields = self._validate(
            name=name, kind=kind, email=email, phone=phone, document=document, metadata=metadata
        )
        session = self._uow.session
        project_id = scope.project_id
        await repository.advisory_lock(session, f"customer-ref|{project_id}|{ref}")
        parsed, digest = fields.document, fields.digest(self._hash_key)
        if digest is not None:
            hex_digest = hashlib.sha256(digest).hexdigest()
            await repository.advisory_lock(session, f"customer-document|{hex_digest}")
        existing = await repository.get_identity_by_ref(session, project_id, ref)
        now = self._clock.now()
        if existing is None:
            return await self._create(scope, ref, fields, parsed, digest, now)
        return await self._update(scope, existing, fields, parsed, digest, now)

    @staticmethod
    def _validate(
        *,
        name: str,
        kind: str,
        email: str | None,
        phone: str | None,
        document: str | None,
        metadata: dict[str, Any],
    ) -> _Fields:
        try:
            clean_name = validate_name(name)
        except ValueError as error:
            raise _invalid("/name", error) from None
        try:
            clean_kind = CustomerKind(kind)
        except ValueError:
            raise _invalid("/kind", ValueError("kind must be individual or company")) from None
        try:
            clean_email = None if email is None else normalize_email(email)
        except ValueError as error:
            raise _invalid("/email", error) from None
        try:
            clean_phone = None if phone is None else normalize_phone(phone)
        except ValueError as error:
            raise _invalid("/phone", error) from None
        try:
            clean_metadata = validate_metadata(metadata)
        except ValueError as error:
            raise _invalid("/metadata", error) from None
        parsed: Document | None = None
        if document is not None:
            try:
                parsed = parse_document(document)
            except ValueError as error:
                raise _invalid("/document", error) from None
            if parsed.kind is not clean_kind:
                raise _invalid(
                    "/kind", ValueError("kind does not match the document (CPF: individual)")
                )
        return _Fields(clean_name, clean_kind, clean_email, clean_phone, clean_metadata, parsed)

    async def _create(
        self,
        scope: ProjectScope,
        ref: str,
        fields: _Fields,
        parsed: Document | None,
        digest: bytes | None,
        now: datetime,
    ) -> UpsertResult:
        session = self._uow.session
        universal_id = await self._universal_for(
            kind=fields.kind,
            name=fields.name,
            email=fields.email,
            phone=fields.phone,
            document=parsed,
            digest=digest,
            now=now,
        )
        identity_id = new_uuid()
        inserted = await repository.insert_identity(
            session,
            identity_id=identity_id,
            project_id=scope.project_id,
            customer_id=universal_id,
            external_ref=ref,
            kind=fields.kind,
            name=fields.name,
            email=fields.email,
            phone=fields.phone,
            metadata=fields.metadata,
            now=now,
        )
        if not inserted:  # pragma: no cover - the advisory lock makes this unreachable
            raise Conflict("A concurrent request created this customer; retry.")
        await self._record(
            scope,
            "customer.created",
            identity_id,
            changed_fields(None, fields.redacted()),
        )
        await self._publish(scope.project_id, "customer.created", identity_id, external_ref=ref)
        logger.info(
            "customer.created",
            customer_id=format_id(IdPrefix.CUSTOMER, identity_id),
            project_id=format_id(IdPrefix.PROJECT, scope.project_id),
            created_by=scope.principal_id,
        )
        created = await repository.get_identity(session, scope.project_id, identity_id)
        assert created is not None  # just inserted in this transaction  # noqa: S101
        return UpsertResult(_view(created), created=True)

    async def _update(
        self,
        scope: ProjectScope,
        existing: CustomerIdentity,
        fields: _Fields,
        parsed: Document | None,
        digest: bytes | None,
        now: datetime,
    ) -> UpsertResult:
        session = self._uow.session
        if existing.anonymized_at is not None:
            raise CustomerAnonymized("The customer was anonymized and cannot be updated.")
        if existing.document_last4 is not None and fields.kind is not existing.kind:
            raise _invalid("/kind", ValueError("kind cannot change once a document is on file"))
        universal_id = existing.financial_customer_id
        document_changed = False
        orphan: UUID | None = None
        if parsed is not None and digest is not None:
            owner = await repository.find_universal_by_hash(session, digest)
            if owner == universal_id:
                pass  # same document already on file
            elif existing.document_last4 is not None:
                raise DocumentChangeNotAllowed(
                    "This customer is already tied to a different document."
                )
            elif owner is not None:
                orphan, universal_id, document_changed = universal_id, owner, True
            else:
                attached = await repository.attach_document(
                    session,
                    universal_id,
                    document_hash=digest,
                    sealed=self._seal_document(universal_id, parsed),
                    document_last4=parsed.last4,
                    now=now,
                )
                if not attached:  # pragma: no cover - guarded by document_last4 above
                    raise DocumentChangeNotAllowed(
                        "This customer is already tied to a different document."
                    )
                document_changed = True
        changed = [
            field
            for field, differs in (
                ("name", fields.name != existing.name),
                ("kind", fields.kind is not existing.kind),
                ("email", fields.email != existing.email),
                ("phone", fields.phone != existing.phone),
                ("metadata", fields.metadata != existing.metadata),
                ("document", document_changed),
            )
            if differs
        ]
        if changed:
            await repository.update_identity(
                session,
                scope.project_id,
                existing.id,
                customer_id=universal_id,
                kind=fields.kind,
                name=fields.name,
                email=fields.email,
                phone=fields.phone,
                metadata=fields.metadata,
                now=now,
            )
            if orphan is not None:
                await self._retire_orphan(orphan, now)
            await self._record(
                scope,
                "customer.updated",
                existing.id,
                {
                    "before": {field: REDACTED for field in changed},
                    "after": {field: REDACTED for field in changed},
                },
            )
            await self._publish(
                scope.project_id,
                "customer.updated",
                existing.id,
                external_ref=existing.external_ref,
                changed=changed,
            )
            logger.info(
                "customer.updated",
                customer_id=format_id(IdPrefix.CUSTOMER, existing.id),
                project_id=format_id(IdPrefix.PROJECT, scope.project_id),
                changed=changed,
                updated_by=scope.principal_id,
            )
        current = await repository.get_identity(session, scope.project_id, existing.id)
        assert current is not None  # read earlier in this transaction  # noqa: S101
        return UpsertResult(_view(current), created=False)

    async def _retire_orphan(self, universal_id: UUID, now: datetime) -> None:
        """A document-less universal record nobody points to any more carries nothing: wipe it."""
        session = self._uow.session
        await repository.lock_universal(session, universal_id)
        if await repository.count_live_identities(session, universal_id) == 0:
            await repository.anonymize_universal(session, universal_id, now=now)

    async def get(self, project_id: UUID, customer_id: str) -> CustomerView:
        return _view(await self._existing(project_id, customer_id))

    async def get_by_ref(self, project_id: UUID, external_ref: str) -> CustomerView:
        identity = await repository.get_identity_by_ref(self._uow.session, project_id, external_ref)
        if identity is None:
            raise NotFound("Customer not found.")
        return _view(identity)

    async def _existing(self, project_id: UUID, customer_id: str) -> CustomerIdentity:
        try:
            parsed = parse_id(customer_id, IdPrefix.CUSTOMER)
        except ValueError:
            raise NotFound("Customer not found.") from None
        identity = await repository.get_identity(self._uow.session, project_id, parsed)
        if identity is None:
            raise NotFound("Customer not found.")
        return identity

    async def list_customers(
        self,
        project_id: UUID,
        params: PageParams,
        *,
        external_ref: str | None = None,
        email: str | None = None,
    ) -> Page[CustomerView]:
        clean_email: str | None = None
        if email is not None:
            try:
                clean_email = normalize_email(email)
            except ValueError as error:
                raise _invalid("/email", error) from None
        rows = await repository.list_identities(
            self._uow.session,
            project_id,
            external_ref=external_ref,
            email=clean_email,
            cursor=params.decoded_cursor(),
            limit=params.limit + 1,
        )
        return build_page(
            rows, params.limit, _view, lambda row: Cursor(created_at=row.created_at, id=row.id)
        )

    async def anonymize(self, scope: ProjectScope, customer_id: str) -> CustomerView:
        """Erase this project's personal data; the universal record too when nobody else uses it.

        Operator-only. Idempotent. Ledger entries are untouched (they reference the identity id).
        """
        if scope.principal_kind != "operator":
            raise Forbidden("Only operators can anonymize customers.")
        session = self._uow.session
        existing = await self._existing(scope.project_id, customer_id)
        if existing.anonymized_at is not None:
            return _view(existing)
        now = self._clock.now()
        universal_id = existing.financial_customer_id
        await repository.lock_universal(session, universal_id)
        await repository.anonymize_identity(session, scope.project_id, existing.id, now=now)
        wiped = (
            await repository.count_live_identities(session, universal_id, excluding=existing.id)
            == 0
        )
        if wiped:
            await repository.anonymize_universal(session, universal_id, now=now)
        await self._record(
            scope,
            "customer.anonymized",
            existing.id,
            {"universal_record_wiped": wiped},
        )
        await self._publish(
            scope.project_id,
            "customer.anonymized",
            existing.id,
            external_ref=existing.external_ref,
        )
        logger.info(
            "customer.anonymized",
            customer_id=format_id(IdPrefix.CUSTOMER, existing.id),
            project_id=format_id(IdPrefix.PROJECT, scope.project_id),
            universal_record_wiped=wiped,
            anonymized_by=scope.principal_id,
        )
        current = await repository.get_identity(session, scope.project_id, existing.id)
        assert current is not None  # noqa: S101
        return _view(current)


@dataclass(frozen=True, slots=True)
class _Fields:
    name: str
    kind: CustomerKind
    email: str | None
    phone: str | None
    metadata: dict[str, Any]
    document: Document | None

    def digest(self, hash_key: bytes) -> bytes | None:
        return None if self.document is None else document_hash(hash_key, self.document)

    def redacted(self) -> dict[str, Any]:
        """Which fields were provided, never their values."""
        provided = {
            "name": True,
            "kind": True,
            "email": self.email is not None,
            "phone": self.phone is not None,
            "metadata": bool(self.metadata),
            "document": self.document is not None,
        }
        return {field: REDACTED for field in _PII_FIELDS if provided[field]}

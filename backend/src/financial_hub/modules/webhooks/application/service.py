"""Webhook endpoint use cases: create, read, update, disable, rotate the secret, test, redeliver.

Each endpoint owns one ``event_subscriptions`` row of kind ``webhook``; filters and the enabled
flag are mirrored to it in the same transaction, so fan-out (ADR-0005) sees exactly what the
endpoint says. The signing secret is generated here, sealed with the platform keyring and
returned once (create/rotate); it never enters an audit diff, a log line or another response.
"""

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
from financial_hub.modules.events.application import (
    EventDraft,
    EventPublisher,
    create_webhook_subscription,
    replay_delivery,
    set_subscription_enabled,
    set_subscription_event_types,
    webhook_delivery_endpoint,
)
from financial_hub.modules.webhooks.application.events import WEBHOOK_TEST, WebhookTestPayload
from financial_hub.modules.webhooks.domain import (
    MAX_DESCRIPTION_LENGTH,
    MAX_EVENT_TYPES,
    SECRET_OVERLAP,
    InvalidWebhookUrlError,
    TargetPolicy,
    WebhookEndpoint,
    generate_secret,
    secret_aad,
    validate_url,
)
from financial_hub.modules.webhooks.infrastructure import repository
from financial_hub.platform.crypto.keyring import Keyring
from financial_hub.platform.crypto.seal import SealedSecret, seal
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import Conflict, FieldError, NotFound, ValidationFailed
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page

logger = structlog.get_logger()

_UNSET: Any = object()


@dataclass(frozen=True, slots=True)
class EndpointView:
    id: str
    project_id: str
    url: str
    description: str | None
    event_types: list[str]
    enabled: bool
    secret_rotation_ends_at: datetime | None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None


@dataclass(frozen=True, slots=True)
class EndpointWithSecret:
    """Create/rotate result: the only time the plaintext secret leaves the service."""

    endpoint: EndpointView
    secret: str


@dataclass(frozen=True, slots=True)
class AttemptView:
    id: UUID
    delivery_id: str
    attempt: int
    outcome: str
    status_code: int | None
    latency_ms: int
    error: str | None
    attempted_at: datetime


def endpoint_public_id(endpoint_id: UUID) -> str:
    return format_id(IdPrefix.WEBHOOK_ENDPOINT, endpoint_id)


def _view(endpoint: WebhookEndpoint, now: datetime) -> EndpointView:
    return EndpointView(
        id=endpoint_public_id(endpoint.id),
        project_id=format_id(IdPrefix.PROJECT, endpoint.project_id),
        url=endpoint.url,
        description=endpoint.description,
        event_types=list(endpoint.event_types),
        enabled=endpoint.enabled,
        secret_rotation_ends_at=endpoint.previous_expires_at if endpoint.overlapping(now) else None,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
        disabled_at=endpoint.disabled_at,
    )


def _audit_fields(endpoint: WebhookEndpoint) -> dict[str, Any]:
    return {
        "url": endpoint.url,
        "description": endpoint.description,
        "event_types": list(endpoint.event_types),
        "enabled": endpoint.enabled,
    }


def _clean_description(description: str | None) -> str | None:
    if description is None:
        return None
    cleaned = description.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_DESCRIPTION_LENGTH:
        raise ValidationFailed(
            "description is too long",
            errors=[
                FieldError(
                    pointer="/description",
                    detail=f"at most {MAX_DESCRIPTION_LENGTH} characters",
                    code="too_long",
                )
            ],
        )
    return cleaned


def _clean_event_types(event_types: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(event_types))
    if not 1 <= len(cleaned) <= MAX_EVENT_TYPES:
        raise ValidationFailed(
            "event_types must name 1..50 filters",
            errors=[
                FieldError(pointer="/event_types", detail="1..50 filters", code="length"),
            ],
        )
    return cleaned


class WebhookEndpointService:
    def __init__(
        self,
        uow: UnitOfWork,
        *,
        clock: Clock,
        keyring: Keyring,
        policy: TargetPolicy,
        audit: AuditContext | None = None,
        publisher: EventPublisher | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._keyring = keyring
        self._policy = policy
        self._audit = audit or AuditContext.system()
        self._publisher = publisher or EventPublisher(clock=clock, notifier=None)

    # --- helpers -------------------------------------------------------------------------------

    def _validate_url(self, url: str) -> str:
        cleaned = url.strip()
        try:
            validate_url(cleaned, self._policy)
        except InvalidWebhookUrlError as exc:
            raise ValidationFailed(
                "invalid webhook url",
                errors=[FieldError(pointer="/url", detail=str(exc), code="invalid_url")],
            ) from None
        return cleaned

    def _seal(self, endpoint_id: UUID, secret: str) -> SealedSecret:
        return seal(self._keyring, secret.encode("ascii"), aad=secret_aad(endpoint_id))

    async def _record(
        self, scope: ProjectScope, action: str, endpoint_id: UUID, diff: dict[str, Any]
    ) -> None:
        await AuditRecorder.record(
            self._uow.session,
            actor=self._audit.acting(Actor.of_principal(scope.principal_kind, scope.principal_id)),
            action=action,
            target=AuditTarget("webhook_endpoint", endpoint_public_id(endpoint_id)),
            diff=diff,
            project_id=scope.project_id,
        )

    async def _require(
        self, project_id: UUID, endpoint_id: UUID, *, for_update: bool = False
    ) -> WebhookEndpoint:
        endpoint = await repository.get_endpoint(
            self._uow.session, project_id, endpoint_id, for_update=for_update
        )
        if endpoint is None:
            raise NotFound("webhook endpoint not found.")
        return endpoint

    def _log(self, event: str, endpoint: WebhookEndpoint, scope: ProjectScope) -> None:
        logger.info(
            event,
            endpoint_id=endpoint_public_id(endpoint.id),
            project_id=format_id(IdPrefix.PROJECT, endpoint.project_id),
            actor=scope.principal_id,
        )

    # --- commands ------------------------------------------------------------------------------

    async def create(
        self,
        scope: ProjectScope,
        *,
        url: str,
        description: str | None,
        event_types: list[str],
    ) -> EndpointWithSecret:
        cleaned_url = self._validate_url(url)
        cleaned_description = _clean_description(description)
        filters = _clean_event_types(event_types)
        endpoint_id = new_uuid()
        subscription_id = await create_webhook_subscription(
            self._uow, project_id=scope.project_id, endpoint_id=endpoint_id, event_types=filters
        )
        secret = generate_secret()
        now = self._clock.now()
        endpoint = await repository.insert_endpoint(
            self._uow.session,
            endpoint_id=endpoint_id,
            project_id=scope.project_id,
            subscription_id=subscription_id,
            url=cleaned_url,
            description=cleaned_description,
            event_types=filters,
            sealed=self._seal(endpoint_id, secret),
            now=now,
        )
        await self._record(
            scope,
            "webhook_endpoint.created",
            endpoint.id,
            changed_fields(None, {**_audit_fields(endpoint), "secret": REDACTED}),
        )
        self._log("webhook_endpoint.created", endpoint, scope)
        return EndpointWithSecret(_view(endpoint, now), secret)

    async def update(
        self,
        scope: ProjectScope,
        endpoint_id: UUID,
        *,
        url: str = _UNSET,
        description: str | None = _UNSET,
        event_types: list[str] = _UNSET,
        enabled: bool = _UNSET,
    ) -> EndpointView:
        endpoint = await self._require(scope.project_id, endpoint_id, for_update=True)
        new_url = endpoint.url if url is _UNSET else self._validate_url(url)
        new_description = (
            endpoint.description if description is _UNSET else _clean_description(description)
        )
        new_types = (
            list(endpoint.event_types) if event_types is _UNSET else _clean_event_types(event_types)
        )
        new_enabled = endpoint.enabled if enabled is _UNSET else enabled
        if new_types != list(endpoint.event_types):
            await set_subscription_event_types(self._uow, endpoint.subscription_id, new_types)
        if new_enabled != endpoint.enabled:
            await set_subscription_enabled(self._uow, endpoint.subscription_id, new_enabled)
        now = self._clock.now()
        updated = await repository.update_endpoint(
            self._uow.session,
            endpoint,
            url=new_url,
            description=new_description,
            event_types=new_types,
            enabled=new_enabled,
            now=now,
        )
        diff = changed_fields(_audit_fields(endpoint), _audit_fields(updated))
        if diff.get("after"):
            action = (
                "webhook_endpoint.disabled"
                if endpoint.enabled and not updated.enabled
                else "webhook_endpoint.updated"
            )
            await self._record(scope, action, updated.id, diff)
            self._log(action, updated, scope)
        return _view(updated, now)

    async def disable(self, scope: ProjectScope, endpoint_id: UUID) -> EndpointView:
        return await self.update(scope, endpoint_id, enabled=False)

    async def rotate_secret(self, scope: ProjectScope, endpoint_id: UUID) -> EndpointWithSecret:
        """New secret now; the old one keeps signing (second signature) for 24 h."""
        endpoint = await self._require(scope.project_id, endpoint_id, for_update=True)
        secret = generate_secret()
        now = self._clock.now()
        rotated = await repository.rotate_secret(
            self._uow.session,
            endpoint.id,
            sealed=self._seal(endpoint.id, secret),
            previous_expires_at=now + SECRET_OVERLAP,
            now=now,
        )
        await self._record(
            scope,
            "webhook_endpoint.secret_rotated",
            rotated.id,
            {
                "before": {"secret": REDACTED},
                "after": {"secret": REDACTED},
                "previous_secret_expires_at": rotated.previous_expires_at,
            },
        )
        self._log("webhook_endpoint.secret_rotated", rotated, scope)
        return EndpointWithSecret(_view(rotated, now), secret)

    async def send_test(self, scope: ProjectScope, endpoint_id: UUID) -> str:
        """Publish a ``webhook.test`` event delivered to this endpoint only; returns its id."""
        endpoint = await self._require(scope.project_id, endpoint_id, for_update=True)
        if not endpoint.enabled:
            raise Conflict("the webhook endpoint is disabled.")
        public_id = endpoint_public_id(endpoint.id)
        event = await self._publisher.publish(
            self._uow,
            EventDraft(
                type=WEBHOOK_TEST,
                data=WebhookTestPayload(endpoint_id=public_id, message="Test event"),
                project_id=scope.project_id,
                subject=public_id,
            ),
            only_subscription=endpoint.subscription_id,
        )
        self._log("webhook_endpoint.test_sent", endpoint, scope)
        return event.public_id

    async def retry_delivery(
        self, scope: ProjectScope, *, event_id: UUID, delivery_id: UUID
    ) -> None:
        """Manual redelivery of a ``dead`` or ``succeeded`` webhook delivery of the event."""
        endpoint_id = await webhook_delivery_endpoint(
            self._uow, project_id=scope.project_id, event_id=event_id, delivery_id=delivery_id
        )
        if endpoint_id is None:
            raise NotFound("webhook delivery not found.")
        await replay_delivery(
            self._uow, project_id=scope.project_id, delivery_id=delivery_id, clock=self._clock
        )
        logger.info(
            "webhook_delivery.retry_requested",
            delivery_id=format_id(IdPrefix.DELIVERY, delivery_id),
            endpoint_id=endpoint_public_id(endpoint_id),
            actor=scope.principal_id,
        )

    # --- queries -------------------------------------------------------------------------------

    async def get(self, project_id: UUID, endpoint_id: UUID) -> EndpointView:
        return _view(await self._require(project_id, endpoint_id), self._clock.now())

    async def list_endpoints(self, project_id: UUID, params: PageParams) -> Page[EndpointView]:
        rows = await repository.list_endpoints(
            self._uow.session, project_id, cursor=params.decoded_cursor(), limit=params.limit
        )
        now = self._clock.now()
        return build_page(
            rows,
            params.limit,
            lambda endpoint: _view(endpoint, now),
            lambda endpoint: Cursor(endpoint.created_at, endpoint.id),
        )

    async def list_attempts(
        self, project_id: UUID, endpoint_id: UUID, *, limit: int
    ) -> list[AttemptView]:
        await self._require(project_id, endpoint_id)
        records = await repository.list_attempts(
            self._uow.session, project_id, endpoint_id, limit=limit
        )
        return [
            AttemptView(
                id=record.id,
                delivery_id=format_id(IdPrefix.DELIVERY, record.delivery_id),
                attempt=record.attempt,
                outcome=record.outcome,
                status_code=record.status_code,
                latency_ms=record.latency_ms,
                error=record.error,
                attempted_at=record.attempted_at,
            )
            for record in records
        ]

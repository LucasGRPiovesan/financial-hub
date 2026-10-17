"""Integration manifests: versions, review, activation, business event ingestion, dry runs.

Lifecycle (ADR-0008, domain ``lifecycle``): a draft is created and edited, *validated* (shape,
cross-field rules and references: metrics and prices exist), *approved* against an exact
``content_hash`` (``integrations:approve``; four-eyes when the diff against the active version is
money-sensitive), then *activated*. Activation runs in one transaction: it supersedes the active
version, creates the metrics the manifest declares as ``create`` (metering api) and its webhook
endpoints (webhooks api), appends an activation row, audits and publishes ``manifest.activated``.
Endpoint secrets are returned to the activating operator only; they are never stored here.

Ingestion (``POST /v1/business-events``) validates the payload against the active manifest, maps
it to usage events and records them through metering (idempotent: the usage transaction ids derive
from the business event's key), then stores the business event with personal data redacted.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from financial_hub.modules.audit.application import (
    Actor,
    AuditContext,
    AuditRecorder,
    AuditTarget,
)
from financial_hub.modules.events.application import EventDraft, EventPublisher
from financial_hub.modules.integrations.application import events as integration_events
from financial_hub.modules.integrations.application.config import IntegrationsSettings
from financial_hub.modules.integrations.domain import (
    Activation,
    Allocation,
    BusinessEventRecord,
    ExistingMetric,
    FourEyesRequired,
    ManifestDiff,
    ManifestStatus,
    ManifestV1,
    ManifestVersion,
    NewMetric,
    UsageDraft,
    allocate_splits,
    as_decimal,
    canonical_json,
    check_idempotency_key,
    check_transition,
    content_hash,
    diff_manifests,
    find_float,
    four_eyes_required,
    map_usage,
    parse_manifest,
    redact,
    validate_payload,
)
from financial_hub.modules.integrations.domain.expressions import EvaluationError
from financial_hub.modules.integrations.domain.manifest import MAX_CONTENT_BYTES
from financial_hub.modules.integrations.infrastructure import repository
from financial_hub.modules.metering.application.api import (
    MetricDefinition,
    UsageItem,
    define_metric,
    metric_definition,
    record_usage,
)
from financial_hub.modules.pricing.application.api import (
    ChargeLine,
    catalog_entries_by_code,
    price_lines,
)
from financial_hub.modules.webhooks.application.api import TargetPolicy, create_endpoint
from financial_hub.platform.crypto.keyring import Keyring
from financial_hub.platform.db import UnitOfWork
from financial_hub.platform.http.auth import ProjectScope
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import (
    Conflict,
    DomainError,
    FieldError,
    Forbidden,
    NotFound,
    ValidationFailed,
)
from financial_hub.shared.ids import IdPrefix, format_id, new_uuid
from financial_hub.shared.money import Money
from financial_hub.shared.pagination import Cursor, Page, PageParams, build_page

_EVENT_TYPES = integration_events  # importing the module registers the event types
OPERATOR_PRINCIPAL: Final = "operator"
MAX_PAYLOAD_BYTES: Final = 65_536
MAX_REASON_LENGTH: Final = 1000
ACTIVATION_HISTORY: Final = 50


class NoActiveManifest(Conflict):
    slug = "no-active-manifest"
    title = "The project has no active manifest"


class ManifestHashMismatch(Conflict):
    slug = "manifest-hash-mismatch"
    title = "The manifest content differs from the reviewed one"


class ManifestMetricConflict(Conflict):
    slug = "manifest-metric-conflict"
    title = "A metric the manifest creates already exists with another definition"


class BusinessEventConflict(Conflict):
    slug = "business-event-conflict"
    title = "Idempotency key reused with a different business event"


@dataclass(frozen=True, slots=True)
class EndpointSecret:
    """A webhook endpoint created by an activation and its signing secret (shown once)."""

    endpoint_id: str
    url: str
    secret: str


@dataclass(frozen=True, slots=True)
class ActivationResult:
    manifest: ManifestVersion
    activation: Activation
    secrets: list[EndpointSecret]


@dataclass(frozen=True, slots=True)
class SimulatedCharge:
    """The charge lines one price rates for one would-be usage event (in isolation)."""

    transaction_id: str
    metric: str
    price: str
    price_version: int
    lines: tuple[ChargeLine, ...]
    amount: Money


@dataclass(frozen=True, slots=True)
class Simulation:
    version: int
    event: str
    usage: list[UsageDraft]
    charges: list[SimulatedCharge]
    allocations: dict[str, list[Allocation]]  # currency code -> payees then platform
    notes: list[str] = field(default_factory=list[str])


@dataclass(frozen=True, slots=True)
class Ingestion:
    record: BusinessEventRecord
    created: bool


def manifest_public_id(manifest_id: UUID) -> str:
    return format_id(IdPrefix.MANIFEST_VERSION, manifest_id)


def _invalid(pointer: str, detail: str, code: str = "invalid") -> ValidationFailed:
    return ValidationFailed(
        "The request contains invalid fields.",
        errors=[FieldError(pointer=pointer, detail=detail, code=code)],
    )


def _operator(scope: ProjectScope, action: str) -> str:
    """Approvals are four-eyes between *people*: only operator sessions approve or activate."""
    if scope.principal_kind != OPERATOR_PRINCIPAL:
        raise Forbidden(f"Manifest versions are {action} by operators only.")
    return scope.principal_id


def _check_content(content: object) -> dict[str, Any]:
    if not isinstance(content, dict):
        raise _invalid("/content", "the manifest must be a JSON object", "type")
    try:
        size = len(canonical_json(content).encode())  # pyright: ignore[reportUnknownArgumentType]
    except TypeError, ValueError:
        raise _invalid("/content", "the manifest must be plain JSON", "type") from None
    if size > MAX_CONTENT_BYTES:
        raise _invalid("/content", f"at most {MAX_CONTENT_BYTES} bytes", "too_large")
    try:
        float_at = find_float(content)
    except ValueError as error:
        raise _invalid("/content", str(error), "too_deep") from None
    if float_at is not None:
        raise _invalid(f"/content{float_at}", "floats are not accepted: use a decimal string")
    return content  # pyright: ignore[reportUnknownVariableType]


def _endpoint_fingerprint(url: str, event_types: Sequence[str]) -> str:
    text = url + "\n" + "\n".join(sorted(set(event_types)))
    return hashlib.sha256(text.encode()).hexdigest()


def _prefixed(error: ValidationFailed, prefix: str) -> ValidationFailed:
    return ValidationFailed(
        error.detail,
        errors=[
            FieldError(f"{prefix}{item.pointer}", item.detail, item.code) for item in error.errors
        ],
    )


class ManifestService:
    """Every method runs inside the caller's active :class:`UnitOfWork`."""

    def __init__(
        self,
        uow: UnitOfWork,
        *,
        clock: Clock,
        audit: AuditContext | None = None,
        publisher: EventPublisher | None = None,
        settings: IntegrationsSettings | None = None,
        keyring: Keyring | None = None,
        webhook_policy: TargetPolicy | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._audit = audit or AuditContext.system()
        self._publisher = publisher or EventPublisher(clock=clock, notifier=None)
        self._settings = settings or IntegrationsSettings()
        self._keyring = keyring
        self._webhook_policy = webhook_policy

    # -- helpers ---------------------------------------------------------------------------

    async def _record(
        self, scope: ProjectScope, action: str, manifest: ManifestVersion, diff: dict[str, Any]
    ) -> None:
        await AuditRecorder.record(
            self._uow.session,
            actor=self._audit.acting(Actor.of_principal(scope.principal_kind, scope.principal_id)),
            action=action,
            target=AuditTarget("integration_manifest", manifest_public_id(manifest.id)),
            diff={"version": manifest.version, **diff},
            project_id=scope.project_id,
        )

    async def _require(
        self, project_id: UUID, version: int, *, for_update: bool = False
    ) -> ManifestVersion:
        manifest = await repository.get_manifest(
            self._uow.session, project_id, version, for_update=for_update
        )
        if manifest is None:
            raise NotFound("Manifest version not found.")
        return manifest

    async def _diff_against_active(self, manifest: ManifestVersion) -> ManifestDiff:
        """What ``manifest`` changes relative to the active version (empty base when none)."""
        active = await repository.get_active(self._uow.session, manifest.project_id)
        base = None if active is None else parse_manifest(active.content)
        return diff_manifests(
            base,
            parse_manifest(manifest.content),
            base_version=None if active is None else active.version,
        )

    async def _check_references(self, project_id: UUID, manifest: ManifestV1) -> None:
        """Metrics and prices the manifest names exist (``ValidationFailed`` with pointers)."""
        session = self._uow.session
        errors: list[FieldError] = []
        fields: dict[str, str | None] = {}
        for index, spec in enumerate(manifest.metrics):
            found = await metric_definition(session, project_id, spec.code)
            pointer = f"/metrics/{index}"
            if isinstance(spec, ExistingMetric):
                if found is None:
                    errors.append(FieldError(f"{pointer}/code", "metric not found", "not_found"))
                elif not found.active:
                    errors.append(FieldError(f"{pointer}/code", "metric is archived", "archived"))
                else:
                    fields[spec.code] = found.field
            elif found is not None and not _same_definition(found, spec):
                errors.append(
                    FieldError(
                        pointer,
                        f"metric {spec.code} exists with another aggregation or field",
                        "conflict",
                    )
                )
        for index, spec in enumerate(manifest.business_events):
            for position, mapping in enumerate(spec.usage):
                if mapping.metric in fields and (
                    (fields[mapping.metric] is None) != (mapping.quantity is None)
                ):
                    errors.append(
                        FieldError(
                            f"/business_events/{index}/usage/{position}/quantity",
                            "a count metric takes no quantity"
                            if fields[mapping.metric] is None
                            else f"quantity is required: metric {mapping.metric} reads a field",
                            "quantity",
                        )
                    )
        codes = [price.code for price in manifest.prices]
        known = {
            entry.price.code for entry in await catalog_entries_by_code(session, project_id, codes)
        }
        for index, code in enumerate(codes):
            if code not in known:
                errors.append(FieldError(f"/prices/{index}/code", "price not found", "not_found"))
        if errors:
            raise ValidationFailed("The manifest references unknown entities.", errors=errors[:50])

    # -- drafts ----------------------------------------------------------------------------

    async def create(self, scope: ProjectScope, content: object) -> ManifestVersion:
        clean = _check_content(content)
        session = self._uow.session
        await repository.lock_project(session, scope.project_id)
        now = self._clock.now()
        manifest = ManifestVersion(
            id=new_uuid(),
            project_id=scope.project_id,
            version=await repository.next_version(session, scope.project_id),
            status=ManifestStatus.DRAFT,
            content=clean,
            content_hash=content_hash(clean),
            created_by=scope.principal_id,
            created_at=now,
            updated_at=now,
        )
        await repository.insert_manifest(session, manifest)
        await self._record(
            scope, "manifest.created", manifest, {"content_hash": manifest.content_hash}
        )
        return manifest

    async def replace(self, scope: ProjectScope, version: int, content: object) -> ManifestVersion:
        """Replace a draft's content (validated versions are immutable)."""
        clean = _check_content(content)
        current = await self._require(scope.project_id, version, for_update=True)
        if current.status is not ManifestStatus.DRAFT:
            raise Conflict(f"Manifest version {version} is {current.status}: content is immutable.")
        updated = await repository.update_manifest(
            self._uow.session,
            scope.project_id,
            version,
            content=clean,
            content_hash=content_hash(clean),
            updated_at=self._clock.now(),
        )
        await self._record(
            scope,
            "manifest.updated",
            updated,
            {"content_hash": {"from": current.content_hash, "to": updated.content_hash}},
        )
        return updated

    async def get(self, project_id: UUID, version: int) -> ManifestVersion:
        return await self._require(project_id, version)

    async def active(self, project_id: UUID) -> ManifestVersion | None:
        return await repository.get_active(self._uow.session, project_id)

    async def list(self, project_id: UUID, params: PageParams) -> Page[ManifestVersion]:
        rows = await repository.list_manifests(
            self._uow.session, project_id, cursor=params.decoded_cursor(), limit=params.limit + 1
        )
        return build_page(
            rows, params.limit, lambda row: row, lambda row: Cursor(row.created_at, row.id)
        )

    async def activations(self, project_id: UUID) -> list[Activation]:
        return list(
            await repository.list_activations(
                self._uow.session, project_id, limit=ACTIVATION_HISTORY
            )
        )

    # -- review ----------------------------------------------------------------------------

    async def validate(self, scope: ProjectScope, version: int) -> ManifestVersion:
        current = await self._require(scope.project_id, version, for_update=True)
        check_transition(current.status, ManifestStatus.VALIDATED)
        manifest = parse_manifest(current.content)
        await self._check_references(scope.project_id, manifest)
        now = self._clock.now()
        updated = await repository.update_manifest(
            self._uow.session,
            scope.project_id,
            version,
            status=ManifestStatus.VALIDATED.value,
            validated_at=now,
            updated_at=now,
        )
        await self._record(scope, "manifest.validated", updated, {"status": "validated"})
        return updated

    async def diff(self, project_id: UUID, version: int) -> ManifestDiff:
        return await self._diff_against_active(await self._require(project_id, version))

    async def approve(
        self, scope: ProjectScope, version: int, *, content_hash: str
    ) -> ManifestVersion:
        """Approve exactly ``content_hash``; four-eyes when the change is money-sensitive."""
        approver = _operator(scope, "approved")
        await repository.lock_project(self._uow.session, scope.project_id)
        current = await self._require(scope.project_id, version, for_update=True)
        check_transition(current.status, ManifestStatus.APPROVED)
        if content_hash != current.content_hash:
            raise ManifestHashMismatch(
                f"Manifest version {version} has content hash {current.content_hash}."
            )
        diff = await self._diff_against_active(current)
        required = four_eyes_required(
            money_sensitive=diff.money_sensitive, always=self._settings.four_eyes_always
        )
        if required and approver == current.created_by:
            raise FourEyesRequired(
                "This version changes "
                + (", ".join(diff.sensitive_sections) or "the manifest")
                + ": a different operator must approve it."
            )
        now = self._clock.now()
        updated = await repository.update_manifest(
            self._uow.session,
            scope.project_id,
            version,
            status=ManifestStatus.APPROVED.value,
            approved_by=approver,
            approved_at=now,
            four_eyes=approver != current.created_by,
            approval_base_version=diff.base_version,
            approval_diff=diff.as_json(),
            updated_at=now,
        )
        await self._record(
            scope,
            "manifest.approved",
            updated,
            {
                "content_hash": updated.content_hash,
                "four_eyes": updated.four_eyes,
                "base_version": diff.base_version,
                "money_sensitive": diff.money_sensitive,
                "sensitive_sections": list(diff.sensitive_sections),
            },
        )
        return updated

    async def reject(self, scope: ProjectScope, version: int, *, reason: str) -> ManifestVersion:
        rejecter = _operator(scope, "rejected")
        cleaned = reason.strip()
        if not 1 <= len(cleaned) <= MAX_REASON_LENGTH:
            raise _invalid("/reason", f"1..{MAX_REASON_LENGTH} characters")
        current = await self._require(scope.project_id, version, for_update=True)
        check_transition(current.status, ManifestStatus.REJECTED)
        now = self._clock.now()
        updated = await repository.update_manifest(
            self._uow.session,
            scope.project_id,
            version,
            status=ManifestStatus.REJECTED.value,
            rejected_by=rejecter,
            rejected_at=now,
            rejection_reason=cleaned,
            updated_at=now,
        )
        await self._record(scope, "manifest.rejected", updated, {"reason": cleaned})
        return updated

    # -- activation ------------------------------------------------------------------------

    async def activate(self, scope: ProjectScope, version: int) -> ActivationResult:
        """Make ``version`` the active manifest (an ``approved`` one, or a ``superseded`` one for a
        rollback) and materialize its metrics and webhook endpoints, atomically."""
        operator = _operator(scope, "activated")
        session = self._uow.session
        await repository.lock_project(session, scope.project_id)
        target = await self._require(scope.project_id, version, for_update=True)
        check_transition(target.status, ManifestStatus.ACTIVE)
        manifest = parse_manifest(target.content)
        previous = await repository.get_active(session, scope.project_id, for_update=True)
        diff = diff_manifests(
            None if previous is None else parse_manifest(previous.content),
            manifest,
            base_version=None if previous is None else previous.version,
        )
        required = four_eyes_required(
            money_sensitive=diff.money_sensitive, always=self._settings.four_eyes_always
        )
        if required and not target.four_eyes:
            raise FourEyesRequired(
                f"Activating version {version} changes "
                + (", ".join(diff.sensitive_sections) or "the manifest")
                + " but it was approved by its author: approve a new version with a second "
                "operator."
            )
        await self._check_references(scope.project_id, manifest)
        now = self._clock.now()
        metrics = await self._ensure_metrics(scope, manifest)
        if previous is not None:
            await repository.update_manifest(
                session,
                scope.project_id,
                previous.version,
                status=ManifestStatus.SUPERSEDED.value,
                superseded_at=now,
                updated_at=now,
            )
        activated = await repository.update_manifest(
            session,
            scope.project_id,
            version,
            status=ManifestStatus.ACTIVE.value,
            activated_at=now,
            updated_at=now,
        )
        endpoints, secrets = await self._ensure_endpoints(scope, manifest, version, now)
        activation = Activation(
            id=new_uuid(),
            project_id=scope.project_id,
            version=version,
            previous_version=None if previous is None else previous.version,
            activated_by=operator,
            activated_at=now,
            materialized={
                "metrics": metrics,
                "webhook_endpoints": endpoints,
                "webhook_endpoints_created": [secret.endpoint_id for secret in secrets],
            },
        )
        await repository.insert_activation(session, activation)
        await self._record(
            scope,
            "manifest.activated",
            activated,
            {
                "previous_version": activation.previous_version,
                "content_hash": activated.content_hash,
                "rollback": target.status is ManifestStatus.SUPERSEDED,
                "materialized": activation.materialized,
            },
        )
        await self._publisher.publish(
            self._uow,
            EventDraft(
                type=integration_events.MANIFEST_ACTIVATED.name,
                data=integration_events.ManifestActivatedPayload(
                    version=version,
                    previous_version=activation.previous_version,
                    content_hash=activated.content_hash,
                    activated_by=operator,
                ),
                project_id=scope.project_id,
                subject=manifest_public_id(activated.id),
            ),
        )
        return ActivationResult(manifest=activated, activation=activation, secrets=secrets)

    async def _ensure_metrics(self, scope: ProjectScope, manifest: ManifestV1) -> dict[str, str]:
        """Metric code -> ``existing`` | ``reused`` | ``created`` (creating the missing ones)."""
        outcome: dict[str, str] = {}
        for spec in manifest.metrics:
            if isinstance(spec, ExistingMetric):
                outcome[spec.code] = "existing"
                continue
            found = await metric_definition(self._uow.session, scope.project_id, spec.code)
            if found is not None:
                if not _same_definition(found, spec):
                    raise ManifestMetricConflict(
                        f"Metric {spec.code} exists with another aggregation or field."
                    )
                outcome[spec.code] = "reused"
                continue
            await define_metric(
                self._uow,
                scope,
                clock=self._clock,
                audit=self._audit,
                code=spec.code,
                name=spec.name,
                description=spec.description,
                aggregation=spec.aggregation,
                field=spec.field,
            )
            outcome[spec.code] = "created"
        return outcome

    async def _ensure_endpoints(
        self, scope: ProjectScope, manifest: ManifestV1, version: int, now: datetime
    ) -> tuple[list[str], list[EndpointSecret]]:
        """Create the declared endpoints not created by an earlier activation; secrets of the new
        ones are returned (only) to the caller."""
        if not manifest.webhooks:
            return [], []
        if self._keyring is None or self._webhook_policy is None:  # pragma: no cover - wiring
            raise RuntimeError("activating a manifest with webhooks needs a keyring and a policy")
        session = self._uow.session
        known = await repository.materialized_endpoints(session, scope.project_id)
        endpoint_ids: list[str] = []
        secrets: list[EndpointSecret] = []
        for index, spec in enumerate(manifest.webhooks):
            fingerprint = _endpoint_fingerprint(spec.url, spec.event_types)
            if fingerprint in known:
                endpoint_ids.append(known[fingerprint])
                continue
            try:
                created = await create_endpoint(
                    self._uow,
                    scope,
                    clock=self._clock,
                    keyring=self._keyring,
                    policy=self._webhook_policy,
                    audit=self._audit,
                    url=spec.url,
                    description=spec.description,
                    event_types=list(spec.event_types),
                )
            except ValidationFailed as error:
                raise _prefixed(error, f"/webhooks/{index}") from None
            await repository.insert_materialized_endpoint(
                session,
                row_id=new_uuid(),
                project_id=scope.project_id,
                fingerprint=fingerprint,
                endpoint_id=created.endpoint.id,
                url=created.endpoint.url,
                event_types=list(created.endpoint.event_types),
                version=version,
                now=now,
            )
            known[fingerprint] = created.endpoint.id
            endpoint_ids.append(created.endpoint.id)
            secrets.append(
                EndpointSecret(created.endpoint.id, created.endpoint.url, created.secret)
            )
        return endpoint_ids, secrets

    # -- dry run ---------------------------------------------------------------------------

    async def simulate(
        self,
        project_id: UUID,
        version: int,
        *,
        name: str,
        payload: object,
        idempotency_key: str,
    ) -> Simulation:
        """What ingesting ``payload`` under ``version`` would record and charge. Reads only."""
        current = await self._require(project_id, version)
        manifest = parse_manifest(current.content)
        spec, clean = _event_spec(manifest, name, payload)
        check_idempotency_key(idempotency_key)
        errors = validate_payload(spec.payload_schema, clean, "/payload")
        if errors:
            raise ValidationFailed("The payload does not match the event's schema.", errors=errors)
        session = self._uow.session
        fields = await _metric_fields(session, project_id, manifest, spec)
        drafts = map_usage(
            manifest, spec, clean, idempotency_key=idempotency_key, metric_fields=fields
        )
        notes: list[str] = []
        entries = await catalog_entries_by_code(
            session, project_id, [price.code for price in manifest.prices]
        )
        now = self._clock.now()
        charges: list[SimulatedCharge] = []
        for draft in drafts:
            quantity = _quantity(draft, fields[draft.metric])
            if quantity is None:
                notes.append(f"{draft.metric}: unique_count usage is not rated per event")
                continue
            for entry in entries:
                if entry.metric_code != draft.metric:
                    continue
                try:
                    price_version = entry.price.version_at(draft.timestamp or now)
                    lines = price_lines(price_version.model, quantity)
                except (DomainError, TypeError, ValueError) as error:
                    notes.append(f"{entry.price.code}: not rated ({error})")
                    continue
                amount = sum((line.amount for line in lines), Money.zero(price_version.currency))
                charges.append(
                    SimulatedCharge(
                        transaction_id=draft.transaction_id,
                        metric=draft.metric,
                        price=entry.price.code,
                        price_version=price_version.version,
                        lines=lines,
                        amount=amount,
                    )
                )
        for entry in entries:
            if entry.metric_code is None:
                notes.append(f"{entry.price.code}: not usage-based, billed per period")
        totals: dict[str, Money] = {}
        for charge in charges:
            code = charge.amount.currency.code
            totals[code] = totals[code] + charge.amount if code in totals else charge.amount
        allocations = {
            code: allocate_splits(total, manifest.splits) for code, total in sorted(totals.items())
        }
        return Simulation(
            version=version,
            event=name,
            usage=drafts,
            charges=charges,
            allocations=allocations,
            notes=notes,
        )


def _same_definition(found: MetricDefinition, spec: NewMetric) -> bool:
    return found.aggregation.value == spec.aggregation and found.field == spec.field


def _quantity(draft: UsageDraft, field_name: str | None) -> Decimal | None:
    """The quantity one usage event contributes (``count``: 1; ``unique_count``: not rated)."""
    if field_name is None:
        return Decimal(1)
    value = draft.properties[field_name]
    try:
        return as_decimal(value)
    except EvaluationError:
        return None


def _event_spec(manifest: ManifestV1, name: str, payload: object) -> tuple[Any, dict[str, Any]]:
    spec = manifest.event(name)
    if spec is None:
        raise _invalid("/name", f"the active manifest declares no business event {name}", "unknown")
    if not isinstance(payload, dict):
        raise _invalid("/payload", "the payload must be a JSON object", "type")
    try:
        size = len(canonical_json(payload).encode())  # pyright: ignore[reportUnknownArgumentType]
    except TypeError, ValueError:
        raise _invalid("/payload", "the payload must be plain JSON", "type") from None
    if size > MAX_PAYLOAD_BYTES:
        raise _invalid("/payload", f"at most {MAX_PAYLOAD_BYTES} bytes", "too_large")
    try:
        float_at = find_float(payload)
    except ValueError as error:
        raise _invalid("/payload", str(error), "too_deep") from None
    if float_at is not None:
        raise _invalid(f"/payload{float_at}", "floats are not accepted: use a decimal string")
    return spec, payload  # pyright: ignore[reportUnknownVariableType]


async def _metric_fields(
    session: Any, project_id: UUID, manifest: ManifestV1, spec: Any
) -> dict[str, str | None]:
    """Metric code -> the property it reads, for the metrics ``spec`` maps to."""
    fields: dict[str, str | None] = {}
    for mapping in spec.usage:
        declared = manifest.metric(mapping.metric)
        if isinstance(declared, NewMetric):
            fields[mapping.metric] = declared.field
            continue
        found = await metric_definition(session, project_id, mapping.metric)
        if found is not None:
            fields[mapping.metric] = found.field
    return fields


class BusinessEventService:
    """``POST /v1/business-events``: runs inside the caller's active :class:`UnitOfWork`."""

    def __init__(
        self, uow: UnitOfWork, *, clock: Clock, publisher: EventPublisher | None = None
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._publisher = publisher or EventPublisher(clock=clock, notifier=None)

    async def ingest(
        self, project_id: UUID, *, name: str, payload: object, idempotency_key: str
    ) -> Ingestion:
        """Record one business event: 200 with the original on a retry with the same payload,
        ``BusinessEventConflict`` (409) on a different one, ``NoActiveManifest`` (409) without an
        active manifest, ``ValidationFailed`` (422) for an unknown name or an invalid payload."""
        session = self._uow.session
        check_idempotency_key(idempotency_key)
        active = await repository.get_active(session, project_id)
        if active is None:
            raise NoActiveManifest("Activate a manifest before sending business events.")
        manifest = parse_manifest(active.content)
        spec, clean = _event_spec(manifest, name, payload)
        payload_hash = content_hash(clean)

        existing = await repository.get_business_event(session, project_id, name, idempotency_key)
        if existing is not None:
            return self._duplicate(existing, payload_hash)

        errors = validate_payload(spec.payload_schema, clean, "/payload")
        if errors:
            raise ValidationFailed("The payload does not match the event's schema.", errors=errors)
        fields = await _metric_fields(session, project_id, manifest, spec)
        drafts = map_usage(
            manifest, spec, clean, idempotency_key=idempotency_key, metric_fields=fields
        )
        outcome: list[dict[str, Any]] = []
        for index, draft in enumerate(drafts):
            try:
                ingested = await record_usage(
                    self._uow,
                    project_id,
                    UsageItem(
                        transaction_id=draft.transaction_id,
                        metric=draft.metric,
                        properties=draft.properties,
                        timestamp=draft.timestamp,
                        customer_external_ref=draft.customer_external_ref,
                    ),
                    clock=self._clock,
                    publisher=self._publisher,
                )
            except ValidationFailed as error:
                raise _prefixed(error, f"/usage/{index}") from None
            outcome.append(
                {
                    "transaction_id": ingested.usage.transaction_id,
                    "metric": ingested.usage.metric,
                    "customer_id": ingested.usage.customer_id,
                    "value": ingested.usage.value,
                    "created": ingested.created,
                }
            )
        record = BusinessEventRecord(
            id=new_uuid(),
            project_id=project_id,
            name=name,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            payload=redact(manifest, clean),
            manifest_version=active.version,
            outcome={"usage": outcome},
            received_at=self._clock.now(),
        )
        if not await repository.insert_business_event(session, record):
            winner = await repository.get_business_event(session, project_id, name, idempotency_key)
            if winner is None:  # pragma: no cover - rows are never deleted
                raise BusinessEventConflict("The business event is being recorded; retry.")
            return self._duplicate(winner, payload_hash)
        await self._publisher.publish(
            self._uow,
            EventDraft(
                type=integration_events.BUSINESS_EVENT_RECORDED.name,
                data=integration_events.BusinessEventRecordedPayload(
                    name=name,
                    idempotency_key=idempotency_key,
                    manifest_version=active.version,
                    usage_transaction_ids=[draft.transaction_id for draft in drafts],
                ),
                project_id=project_id,
                subject=idempotency_key,
            ),
        )
        return Ingestion(record=record, created=True)

    @staticmethod
    def _duplicate(stored: BusinessEventRecord, payload_hash: str) -> Ingestion:
        if stored.payload_hash != payload_hash:
            raise BusinessEventConflict(
                f"Business event {stored.name} with idempotency key {stored.idempotency_key} was "
                "already recorded with a different payload."
            )
        return Ingestion(record=stored, created=False)

    async def list(self, project_id: UUID, params: PageParams) -> Page[BusinessEventRecord]:
        rows = await repository.list_business_events(
            self._uow.session, project_id, cursor=params.decoded_cursor(), limit=params.limit + 1
        )
        return build_page(
            rows, params.limit, lambda row: row, lambda row: Cursor(row.received_at, row.id)
        )


__all__ = [
    "ActivationResult",
    "BusinessEventConflict",
    "BusinessEventService",
    "EndpointSecret",
    "Ingestion",
    "ManifestHashMismatch",
    "ManifestMetricConflict",
    "ManifestService",
    "NoActiveManifest",
    "SimulatedCharge",
    "Simulation",
]

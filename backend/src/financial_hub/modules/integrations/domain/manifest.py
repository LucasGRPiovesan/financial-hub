"""``manifest.v1``: how a product integrates with the hub, as data (ADR-0008, INTEGRATIONS.md s1).

The pydantic models below *are* the schema: :func:`manifest_json_schema` generates the published
JSON Schema (draft 2020-12, ``docs/schemas/manifest.v1.json``) from them, and :func:`parse_manifest`
validates a document with them plus the cross-field rules a JSON Schema cannot express (unique
keys, split total, metric references, expression and payload-schema bounds). Every model is strict
(no coercion, so no float sneaks in as an integer) and closed (``additionalProperties: false``).

Money never appears as a float: splits are integer basis points, constants are decimal strings.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from financial_hub.modules.integrations.domain.expressions import Selector, check_bounds
from financial_hub.modules.integrations.domain.payload_schema import (
    PayloadSchema,
    check_schema_bounds,
)
from financial_hub.shared.errors import FieldError, ValidationFailed

SCHEMA_VERSION: Final = "manifest.v1"
SCHEMA_ID: Final = "https://financial-hub.dev/schemas/manifest.v1.json"
DRAFT_2020_12: Final = "https://json-schema.org/draft/2020-12/schema"
MAX_CONTENT_BYTES: Final = 262_144
BASIS_POINTS_TOTAL: Final = 10_000
MAX_ERRORS: Final = 50

# Same rules as the modules that own these codes (metering, pricing, events); repeated here as
# schema patterns (pydantic's regex engine is linear-time) because the manifest is self-describing.
METRIC_CODE_PATTERN: Final = r"^[a-z][a-z0-9_]{0,62}$"
METRIC_FIELD_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
PRICE_CODE_PATTERN: Final = r"^[a-z0-9][a-z0-9_.-]{0,99}$"
EVENT_NAME_PATTERN: Final = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
EVENT_FILTER_PATTERN: Final = r"^(\*|[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*(\.\*)?)$"
PRODUCT_CODE_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,62}$"
PAYEE_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"

MetricCode = Annotated[str, Field(pattern=METRIC_CODE_PATTERN, max_length=63)]
Text = Annotated[str, Field(min_length=1, max_length=200)]
LongText = Annotated[str, Field(min_length=1, max_length=1000)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Product(_Section):
    """The product this manifest integrates."""

    code: str = Field(pattern=PRODUCT_CODE_PATTERN, max_length=63)
    name: Text
    description: LongText | None = None


class CustomerMapping(_Section):
    """Where a business event payload carries the customer's identity.

    ``external_id`` is matched against the customer's ``external_ref`` in the hub. The other
    selectors declare where personal data sits: those values are redacted before the payload is
    stored, and are not used to create customers (customers are created through the API first).
    """

    external_id: Selector
    name: Selector | None = None
    document: Selector | None = None
    email: Selector | None = None


class UsageMapping(_Section):
    """One usage event to record when the business event happens.

    ``quantity`` is the value the metric's ``field`` reads (absent for ``count`` metrics);
    ``customer`` defaults to ``customers.external_id``; ``idempotency_key`` to the business event's
    own key; ``timestamp`` (an RFC 3339 instant) to the time of receipt.
    """

    metric: MetricCode
    quantity: Selector | None = None
    customer: Selector | None = None
    idempotency_key: Selector | None = None
    timestamp: Selector | None = None


class BusinessEventSpec(_Section):
    """A business event the product sends to ``POST /v1/business-events``."""

    name: str = Field(pattern=EVENT_NAME_PATTERN, max_length=100)
    description: LongText | None = None
    payload_schema: PayloadSchema
    usage: list[UsageMapping] = Field(min_length=1, max_length=8)


class ExistingMetric(_Section):
    """A billable metric that already exists in the project."""

    source: Literal["existing"]
    code: MetricCode


class NewMetric(_Section):
    """A billable metric created (if missing) when the manifest is activated."""

    source: Literal["create"]
    code: MetricCode
    name: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    aggregation: Literal["sum", "count", "max", "unique_count"]
    field: Annotated[str, Field(pattern=METRIC_FIELD_PATTERN, max_length=64)] | None = None


type MetricSpec = Annotated[ExistingMetric | NewMetric, Field(discriminator="source")]


class PriceRef(_Section):
    """A price of the project's catalog, by code (validated against the catalog)."""

    code: str = Field(pattern=PRICE_CODE_PATTERN, max_length=100)


class Split(_Section):
    """A payee's share of each charge in basis points (1 bp = 0.01%); the platform keeps the rest"""

    payee: str = Field(pattern=PAYEE_PATTERN, max_length=100)
    basis_points: int = Field(ge=1, le=BASIS_POINTS_TOTAL)
    description: Text | None = None


class WalletPolicy(_Section):
    """Placeholder until wallets exist: ``disabled`` or ``wallet_first`` (debit a wallet first)."""

    mode: Literal["disabled", "wallet_first"] = "disabled"


class WebhookSpec(_Section):
    """An outbound webhook endpoint created when the manifest is activated."""

    url: str = Field(min_length=8, max_length=2048)
    event_types: list[Annotated[str, Field(pattern=EVENT_FILTER_PATTERN, max_length=100)]] = Field(
        min_length=1, max_length=50
    )
    description: Text | None = None


class ManifestV1(_Section):
    """An integration manifest (``manifest.v1``)."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        title="manifest.v1",
        json_schema_extra={
            "description": (
                "Integration manifest of a Financial Hub project (ADR-0008). Cross-field rules "
                "checked by the hub: business event names, metric codes, price codes, split "
                "payees and webhook urls are unique; split basis points sum to at most 10000; "
                "every usage metric is declared in `metrics`; selectors nest at most 4 levels; "
                "payload schemas describe an object and nest at most 6 levels. Floats are never "
                "accepted: use integers or decimal strings."
            )
        },
    )

    schema_version: Literal["manifest.v1"]
    description: LongText | None = None
    product: Product
    customers: CustomerMapping
    business_events: list[BusinessEventSpec] = Field(min_length=1, max_length=32)
    metrics: list[MetricSpec] = Field(default_factory=list[MetricSpec], max_length=32)
    prices: list[PriceRef] = Field(default_factory=list[PriceRef], max_length=32)
    splits: list[Split] = Field(default_factory=list[Split], max_length=16)
    wallet: WalletPolicy = Field(default_factory=WalletPolicy)
    webhooks: list[WebhookSpec] = Field(default_factory=list[WebhookSpec], max_length=10)

    def event(self, name: str) -> BusinessEventSpec | None:
        return next((spec for spec in self.business_events if spec.name == name), None)

    def metric(self, code: str) -> ExistingMetric | NewMetric | None:
        return next((spec for spec in self.metrics if spec.code == code), None)


def manifest_json_schema() -> dict[str, Any]:
    """The published JSON Schema of ``manifest.v1`` (draft 2020-12, deterministic key order)."""
    generated = ManifestV1.model_json_schema(by_alias=True, mode="validation")
    return {"$schema": DRAFT_2020_12, "$id": SCHEMA_ID, **generated}


def canonical_json(content: Mapping[str, Any]) -> str:
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(content: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON: what an approval is bound to."""
    return hashlib.sha256(canonical_json(content).encode()).hexdigest()


def _pointer(location: Iterable[int | str]) -> str:
    parts = [str(part).replace("~", "~0").replace("/", "~1") for part in location]
    return "".join(f"/{part}" for part in parts)


def _invalid(errors: list[FieldError]) -> ValidationFailed:
    return ValidationFailed("The manifest is not a valid manifest.v1.", errors=errors[:MAX_ERRORS])


def parse_manifest(content: object) -> ManifestV1:
    """Validate a ``manifest.v1`` document (shape and cross-field rules); ``ValidationFailed``
    lists every problem with a JSON pointer into the document."""
    if not isinstance(content, dict):
        raise _invalid([FieldError("", "the manifest must be a JSON object", "type")])
    try:
        size = len(canonical_json(content).encode())  # pyright: ignore[reportUnknownArgumentType]
    except TypeError, ValueError:
        raise _invalid([FieldError("", "the manifest must be plain JSON", "type")]) from None
    if size > MAX_CONTENT_BYTES:
        raise _invalid([FieldError("", f"at most {MAX_CONTENT_BYTES} bytes", "too_large")])
    try:
        manifest = ManifestV1.model_validate(content)
    except ValidationError as error:
        raise _invalid(
            [
                FieldError(_pointer(_strip_tags(item["loc"])), item["msg"], item["type"])
                for item in error.errors(include_url=False)
            ]
        ) from None
    errors = cross_check(manifest)
    if errors:
        raise _invalid(errors)
    return manifest


_UNION_TAGS: Final = frozenset(
    {"literal", "pointer", "multiply", "coalesce", "to_decimal", "existing", "create"}
)


def _is_label(part: int | str, previous: int | str | None) -> bool:
    if not isinstance(part, str) or previous == "properties":  # a payload property is a key
        return False
    return part in _UNION_TAGS or "[" in part or part.startswith("constrained-") or part == "str"


def _strip_tags(location: tuple[int | str, ...]) -> list[int | str]:
    """Drop pydantic's union labels (``tagged-union[...]``, ``multiply``...) from a location."""
    kept: list[int | str] = []
    previous: int | str | None = None
    for part in location:
        if not _is_label(part, previous):
            kept.append(part)
        previous = part
    return kept


def _duplicates(values: Iterable[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


def cross_check(manifest: ManifestV1) -> list[FieldError]:
    """The rules of ``manifest.v1`` that a JSON Schema cannot express."""
    errors: list[FieldError] = []
    for section, keys in (
        ("business_events", [spec.name for spec in manifest.business_events]),
        ("metrics", [spec.code for spec in manifest.metrics]),
        ("prices", [spec.code for spec in manifest.prices]),
        ("splits", [spec.payee for spec in manifest.splits]),
        ("webhooks", [spec.url for spec in manifest.webhooks]),
    ):
        for key in _duplicates(keys):
            errors.append(FieldError(f"/{section}", f"{key!r} appears more than once", "duplicate"))
    total = sum(split.basis_points for split in manifest.splits)
    if total > BASIS_POINTS_TOTAL:
        errors.append(
            FieldError("/splits", f"basis points sum to {total}, at most 10000", "split_total")
        )
    for index, metric in enumerate(manifest.metrics):
        if isinstance(metric, NewMetric):
            if metric.aggregation == "count" and metric.field is not None:
                errors.append(
                    FieldError(f"/metrics/{index}/field", "count reads no field", "field")
                )
            if metric.aggregation != "count" and metric.field is None:
                errors.append(
                    FieldError(f"/metrics/{index}/field", "required unless count", "required")
                )
    _check_selectors(manifest, errors)
    for index, spec in enumerate(manifest.business_events):
        base = f"/business_events/{index}"
        try:
            check_schema_bounds(spec.payload_schema)
        except ValueError as error:
            errors.append(FieldError(f"{base}/payload_schema", str(error), "bounds"))
        for metric in _duplicates(mapping.metric for mapping in spec.usage):
            errors.append(FieldError(f"{base}/usage", f"metric {metric} mapped twice", "duplicate"))
        for position, mapping in enumerate(spec.usage):
            declared = manifest.metric(mapping.metric)
            if declared is None:
                errors.append(
                    FieldError(
                        f"{base}/usage/{position}/metric",
                        f"metric {mapping.metric} is not declared in /metrics",
                        "undeclared",
                    )
                )
            elif isinstance(declared, NewMetric) and (
                (declared.field is None) != (mapping.quantity is None)
            ):
                detail = (
                    "a count metric takes no quantity"
                    if declared.field is None
                    else f"quantity is required: metric {declared.code} reads a field"
                )
                errors.append(FieldError(f"{base}/usage/{position}/quantity", detail, "quantity"))
    return errors


def _check_selectors(manifest: ManifestV1, errors: list[FieldError]) -> None:
    located: list[tuple[str, Selector | None]] = [
        ("/customers/external_id", manifest.customers.external_id),
        ("/customers/name", manifest.customers.name),
        ("/customers/document", manifest.customers.document),
        ("/customers/email", manifest.customers.email),
    ]
    for index, spec in enumerate(manifest.business_events):
        for position, mapping in enumerate(spec.usage):
            base = f"/business_events/{index}/usage/{position}"
            located += [
                (f"{base}/quantity", mapping.quantity),
                (f"{base}/customer", mapping.customer),
                (f"{base}/idempotency_key", mapping.idempotency_key),
                (f"{base}/timestamp", mapping.timestamp),
            ]
    for pointer, selector in located:
        if selector is None:
            continue
        try:
            check_bounds(selector)
        except ValueError as error:
            errors.append(FieldError(pointer, str(error), "bounds"))

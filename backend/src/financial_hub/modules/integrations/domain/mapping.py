"""Business event -> usage events and charge -> split allocation, as pure functions.

:func:`map_usage` evaluates a business event's usage mappings over its payload and returns the
usage events metering would record; it never touches storage, so ingestion and the dry run
(``simulate``) share it. :func:`allocate_splits` divides a charge between the payees and the
platform with the largest-remainder method (FINANCIAL_MODEL R3: parts sum exactly to the total,
the platform listed last).
"""

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, cast
from uuid import UUID

from financial_hub.modules.integrations.domain.expressions import (
    MISSING,
    EvaluationError,
    Selector,
    evaluate,
    plain_decimal,
    pointer_tokens,
    pointers_of,
)
from financial_hub.modules.integrations.domain.manifest import (
    BASIS_POINTS_TOTAL,
    BusinessEventSpec,
    ManifestV1,
    Split,
)
from financial_hub.shared.errors import FieldError, ValidationFailed
from financial_hub.shared.money import Money, allocate

IDEMPOTENCY_KEY_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:@+=~-]{0,63}$"
_IDEMPOTENCY_KEY_RE: Final = re.compile(IDEMPOTENCY_KEY_PATTERN)
MAX_CUSTOMER_REF: Final = 128
MAX_TIMESTAMP_TEXT: Final = 64
REDACTED: Final = "[redacted]"
PLATFORM: Final = "platform"


@dataclass(frozen=True, slots=True)
class UsageDraft:
    """A usage event as metering will receive it (``transaction_id`` derives from the key)."""

    transaction_id: str
    metric: str
    customer_external_ref: str
    properties: dict[str, Any]
    timestamp: datetime | None


@dataclass(frozen=True, slots=True)
class BusinessEventRecord:
    """A stored business event: ``payload`` is redacted, ``payload_hash`` covers the original."""

    id: UUID
    project_id: UUID
    name: str
    idempotency_key: str
    payload_hash: str
    payload: dict[str, Any]
    manifest_version: int
    outcome: dict[str, Any]
    received_at: datetime


@dataclass(frozen=True, slots=True)
class Allocation:
    payee: str
    basis_points: int
    amount: Money


def check_idempotency_key(value: str, pointer: str = "/idempotency_key") -> str:
    if len(value) > 64 or _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
        raise _invalid(
            pointer, "1-64 characters: letters, digits and . _ : @ + = ~ - (no leading symbol)"
        )
    return value


def _invalid(pointer: str, detail: str, code: str = "invalid") -> ValidationFailed:
    return ValidationFailed(
        "The business event cannot be mapped.",
        errors=[FieldError(pointer=pointer, detail=detail, code=code)],
    )


def _value(selector: Selector, payload: Any, pointer: str) -> Any:
    try:
        return evaluate(selector, payload)
    except EvaluationError as error:
        raise _invalid(pointer, str(error), "mapping") from None


def _text(selector: Selector, payload: Any, pointer: str, *, limit: int, what: str) -> str:
    value = _value(selector, payload, pointer)
    if value is MISSING or value is None:
        raise _invalid(pointer, f"the payload gives no {what}", "missing")
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise _invalid(pointer, f"the {what} must be a string or an integer", "mapping")
    text = str(value)
    if not 1 <= len(text) <= limit:
        raise _invalid(pointer, f"the {what} must have 1..{limit} characters", "mapping")
    return text


def _timestamp(selector: Selector, payload: Any, pointer: str) -> datetime | None:
    value = _value(selector, payload, pointer)
    if value is MISSING or value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_TIMESTAMP_TEXT:
        raise _invalid(pointer, "the timestamp must be an RFC 3339 string", "mapping")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid(pointer, "the timestamp must be an RFC 3339 string", "mapping") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid(pointer, "the timestamp must include a timezone offset", "mapping")
    return parsed


def _quantity(selector: Selector, payload: Any, pointer: str) -> Any:
    value = _value(selector, payload, pointer)
    if value is MISSING or value is None:
        raise _invalid(pointer, "the payload gives no quantity", "missing")
    if isinstance(value, Decimal):
        return plain_decimal(value)
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise _invalid(pointer, "the quantity must be an integer or a string", "mapping")
    return value


def map_usage(
    manifest: ManifestV1,
    spec: BusinessEventSpec,
    payload: Any,
    *,
    idempotency_key: str,
    metric_fields: Mapping[str, str | None],
) -> list[UsageDraft]:
    """The usage events ``payload`` produces; ``ValidationFailed`` (pointers into the mapping).

    ``metric_fields`` maps each metric code to the property its definition reads (``None`` for
    ``count``). The transaction id of each usage event is ``<key>:<metric>``, where ``<key>`` is
    the mapping's idempotency selector or the business event's own key: a retried business event
    always derives the same usage events.
    """
    index = manifest.business_events.index(spec)
    drafts: list[UsageDraft] = []
    for position, mapping in enumerate(spec.usage):
        base = f"/manifest/business_events/{index}/usage/{position}"
        key = idempotency_key
        if mapping.idempotency_key is not None:
            key = _text(
                mapping.idempotency_key,
                payload,
                f"{base}/idempotency_key",
                limit=64,
                what="idempotency key",
            )
            check_idempotency_key(key, f"{base}/idempotency_key")
        customer = _text(
            mapping.customer or manifest.customers.external_id,
            payload,
            f"{base}/customer",
            limit=MAX_CUSTOMER_REF,
            what="customer reference",
        )
        if mapping.metric not in metric_fields:
            raise _invalid(f"{base}/metric", f"metric {mapping.metric} not found", "not_found")
        field = metric_fields[mapping.metric]
        properties: dict[str, Any] = {}
        if field is not None:
            if mapping.quantity is None:
                raise _invalid(f"{base}/quantity", "the metric reads a field: map a quantity")
            properties[field] = _quantity(mapping.quantity, payload, f"{base}/quantity")
        timestamp = (
            None
            if mapping.timestamp is None
            else _timestamp(mapping.timestamp, payload, f"{base}/timestamp")
        )
        drafts.append(
            UsageDraft(
                transaction_id=f"{key}:{mapping.metric}",
                metric=mapping.metric,
                customer_external_ref=customer,
                properties=properties,
                timestamp=timestamp,
            )
        )
    return drafts


def redact(manifest: ManifestV1, payload: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``payload`` without the personal data the manifest points at (name, document,
    email): what is stored. Only the declared pointers are touched."""
    redacted = copy.deepcopy(payload)
    selectors = (manifest.customers.name, manifest.customers.document, manifest.customers.email)
    for selector in selectors:
        if selector is None:
            continue
        for pointer in pointers_of(selector):
            _replace(redacted, pointer_tokens(pointer))
    return redacted


def _replace(document: Any, tokens: Sequence[str]) -> None:
    if not tokens:
        return
    parent: object = document
    for token in tokens[:-1]:
        if isinstance(parent, dict) and token in parent:
            parent = cast(dict[str, object], parent)[token]
        elif isinstance(parent, list) and token.isascii() and token.isdigit():
            items = cast(list[object], parent)
            if int(token) >= len(items):
                return
            parent = items[int(token)]
        else:
            return
    last = tokens[-1]
    if isinstance(parent, dict) and last in parent:
        cast(dict[str, object], parent)[last] = REDACTED
    elif isinstance(parent, list) and last.isascii() and last.isdigit():
        items = cast(list[object], parent)
        if int(last) < len(items):
            items[int(last)] = REDACTED


def allocate_splits(total: Money, splits: Sequence[Split]) -> list[Allocation]:
    """Each payee's share of ``total`` and, last, the platform's remainder (largest remainder)."""
    payees = sum(split.basis_points for split in splits)
    if payees > BASIS_POINTS_TOTAL:
        raise ValueError("split basis points exceed 10000")
    weights = [split.basis_points for split in splits] + [BASIS_POINTS_TOTAL - payees]
    amounts = allocate(total, weights)
    parts = [
        Allocation(payee=split.payee, basis_points=split.basis_points, amount=amount)
        for split, amount in zip(splits, amounts, strict=False)
    ]
    parts.append(Allocation(PLATFORM, BASIS_POINTS_TOTAL - payees, amounts[-1]))
    return parts

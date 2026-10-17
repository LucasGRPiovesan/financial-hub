"""The Asaas Sandbox adapter: :class:`PaymentProvider` over the Asaas v3 REST API (PRO-41).

Facts come from ``docs/research/asaas-sandbox.md``; the protocol it must survive is ADR-0012 s3.

**No idempotency header exists** at Asaas, so the adapter never makes a money-moving call it
cannot recognise afterwards: every payment carries ``externalReference`` = the hub's ``pi_`` id
(refunds carry their ``rf_`` id in the refund ``description``, the only free text a refund has) and
the adapter looks that reference up *before* creating (``ProviderDuplicate`` if it exists).
Asaas does not enforce ``externalReference`` uniqueness (research s1), so a lookup that returns more
than one match is ambiguous: it raises :class:`ProviderUncertain` and logs a warning with ids only.

Outcome classification (what the hub may assume, provider.py):

- no request left the machine (connect error/timeout, pool timeout), HTTP 429, or a *read* that
  failed or got a 5xx -> :class:`ProviderRetryable`;
- a definitive 4xx (code from the body, never its free-text message) -> :class:`ProviderRejected`;
- a *write* (POST/DELETE) that may have reached Asaas but has no usable answer (timeout or broken
  connection after sending, 5xx, unreadable body) -> :class:`ProviderUncertain`: the resolver
  converges through :meth:`lookup_by_external_reference`; a create is never retried blindly.

Secrets: the API key lives only in the ``access_token`` request header, built per call. It is not in
URLs (so not in OpenTelemetry spans), not in exception messages (errors carry a short code and are
raised ``from None``), not in ``repr`` and never logged; debug output redacts the header.

Money: Asaas speaks decimal reais as JSON numbers. Requests are encoded from integer minor units by
exact decimal arithmetic and spliced into the JSON text (a ``float`` is never created); responses
are parsed with ``parse_float=Decimal`` and rejected if they carry more than two decimals.
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any, Final, cast
from uuid import UUID

import httpx
import structlog

from financial_hub.modules.payments.domain import (
    CaptureStatus,
    CreatePaymentRequest,
    PaymentMethod,
    PaymentProvider,
    ProviderBalance,
    ProviderCredentials,
    ProviderCustomer,
    ProviderCustomerRequest,
    ProviderDuplicate,
    ProviderPaymentSnapshot,
    ProviderRefundSnapshot,
    ProviderRejected,
    ProviderRetryable,
    ProviderUnavailable,
    ProviderUncertain,
    RefundRequest,
    RefundState,
)
from financial_hub.modules.payments.domain.model import EXTERNAL_REF_PATTERN
from financial_hub.modules.payments.infrastructure import asaas_status
from financial_hub.shared.money import BRL, Money

logger = structlog.get_logger()

PROVIDER_NAME: Final = "asaas"
SANDBOX_BASE_URL: Final = "https://api-sandbox.asaas.com/v3"
SANDBOX_ENVIRONMENT: Final = "sandbox"
USER_AGENT: Final = "financial-hub/0.1 (asaas-sandbox-adapter)"
API_KEY_HEADER: Final = "access_token"
TIMEOUT: Final = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)

# A sanity cap in minor units (R$ 10 billion): anything above is a bug or a hostile response.
MAX_MINOR_UNITS: Final = 10**12
LOOKUP_PAGE_SIZE: Final = 10
LIST_PAGE_SIZE: Final = 100
LIST_MAX_PAGES: Final = 500
REDACTED: Final = "[REDACTED]"

BILLING_TYPES: Final[Mapping[PaymentMethod, str]] = {
    PaymentMethod.PIX: "PIX",
    PaymentMethod.BOLETO: "BOLETO",
    # CARD is deliberately absent: a CREDIT_CARD charge needs the payer on Asaas' invoice page (the
    # port has no field for its URL) or card data the hub must never hold, and the research does
    # not confirm either flow for the sandbox. It is refused with a clear code instead.
}
_ID: Final = re.compile(r"[A-Za-z0-9_]{1,64}")
_ERROR_CODE: Final = re.compile(r"[A-Za-z0-9_.-]{1,60}")
_STATUS_NAME: Final = re.compile(r"[A-Z_]{1,50}")
_DIGITS: Final = re.compile(r"\D")


class _InvalidResponse(Exception):
    """A 2xx body that is not what the API documents (turned into ``ProviderUncertain``)."""


# --- money --------------------------------------------------------------------------------------


def to_asaas_value(amount: Money) -> str:
    """Integer minor units -> the JSON number text Asaas expects (``12345 -> "123.45"``)."""
    if amount.currency != BRL:
        raise ProviderRejected("currency_not_supported")
    if not 0 < amount.minor_units <= MAX_MINOR_UNITS:
        raise ProviderRejected("invalid_amount")
    return format(amount.to_decimal(), "f")


def from_asaas_value(value: object) -> Money:
    """A decimal-reais JSON value -> :class:`Money`; more than two decimals is refused.

    The value must have been parsed with ``parse_float=Decimal``; a ``float`` or ``str`` is
    refused (so a drifted parser fails loudly instead of rounding money).
    """
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise _InvalidResponse("amount is not a decimal number")
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0 or amount > Decimal(MAX_MINOR_UNITS) / 100:
        raise _InvalidResponse("amount out of range")
    try:
        return Money.parse(format(amount, "f"), BRL)
    except ValueError:
        raise _InvalidResponse("amount has more than two decimals") from None


def _signed_asaas_value(value: object) -> Money:
    """Like :func:`from_asaas_value` but a negative amount is valid (an account balance)."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise _InvalidResponse("amount is not a decimal number")
    amount = Decimal(value)
    if not amount.is_finite():
        raise _InvalidResponse("amount out of range")
    magnitude = from_asaas_value(abs(amount))
    return Money(-magnitude.minor_units, BRL) if amount < 0 else magnitude


def _json_text(fields: Mapping[str, object], decimals: Mapping[str, str]) -> bytes:
    """JSON object text with ``decimals`` spliced in as raw numbers (no float is ever involved)."""
    text = json.dumps(dict(fields), separators=(",", ":"), ensure_ascii=False)
    if decimals:
        raw = ",".join(f'"{name}":{number}' for name, number in decimals.items())
        text = f"{text[:-1]},{raw}}}" if fields else f"{{{raw}}}"
    return text.encode()


# --- redaction -----------------------------------------------------------------------------------


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Headers safe to log: the API key (and any authorization-like header) is masked."""
    return {
        name: REDACTED if name.lower() in {API_KEY_HEADER, "authorization"} else value
        for name, value in headers.items()
    }


# --- response parsing ----------------------------------------------------------------------------


def _obj(value: object) -> dict[str, Any] | None:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _items(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _text(payload: Mapping[str, Any], name: str, *, required: bool = False) -> str | None:
    value = payload.get(name)
    if isinstance(value, str) and value:
        return value
    if required:
        raise _InvalidResponse(f"missing {name}")
    return None


def _refund_snapshot(
    payment_id: str, item: Mapping[str, Any], index: int
) -> ProviderRefundSnapshot:
    raw = str(item.get("status", ""))
    state = asaas_status.refund_state(raw)
    if state is None:
        logger.warning("payments.asaas.unknown_refund_status", status=_safe_status(raw))
        state = RefundState.SUBMITTED  # keeps holding the money until a known status arrives
    description = _text(item, "description")
    reference = (
        description.strip()
        if description is not None and EXTERNAL_REF_PATTERN.fullmatch(description.strip())
        else None
    )
    # A refund has no id of its own in the documented fields: derive a stable one.
    provider_id = (
        _text(item, "id")
        or _text(item, "endToEndIdentifier")
        or f"{payment_id}:{item.get('dateCreated') or index}:{index}"
    )
    return ProviderRefundSnapshot(
        provider_refund_id=provider_id[:100],
        external_reference=reference,
        amount=from_asaas_value(item.get("value")),
        status=state,
    )


def _safe_status(raw: str) -> str:
    return raw if _STATUS_NAME.fullmatch(raw) else "<invalid>"


def payment_snapshot(payload: Mapping[str, Any]) -> ProviderPaymentSnapshot:
    """A fetched payment object as a snapshot (raises ``_InvalidResponse`` if it is malformed)."""
    payment_id = _text(payload, "id", required=True)
    raw_status = _text(payload, "status", required=True)
    if payment_id is None or raw_status is None:  # pragma: no cover - required=True raised
        raise _InvalidResponse("missing id or status")
    amount = from_asaas_value(payload.get("value"))
    effect = asaas_status.status_effect(raw_status)
    if effect is None:
        logger.warning(
            "payments.asaas.unknown_status",
            status=_safe_status(raw_status),
            provider_payment_id=payment_id,
        )
        effect = asaas_status.UNKNOWN
    if payload.get("deleted") is True:
        effect, raw_status = asaas_status.DELETED, asaas_status.DELETED_RAW_STATUS
    net: int | None = None
    if payload.get("netValue") is not None:
        candidate = from_asaas_value(payload["netValue"]).minor_units
        if 0 <= candidate <= amount.minor_units:
            net = candidate
        else:
            logger.warning("payments.asaas.net_value_out_of_range", provider_payment_id=payment_id)
    refunds = tuple(
        _refund_snapshot(payment_id, item, index)
        for index, raw in enumerate(_items(payload.get("refunds")))
        if (item := _obj(raw)) is not None
    )
    return ProviderPaymentSnapshot(
        provider_payment_id=payment_id,
        external_reference=_text(payload, "externalReference"),
        raw_status=raw_status[:100],
        amount=amount,
        capture_status=effect.capture,
        settlement_status=effect.settlement,
        dispute_status=effect.dispute,
        net_amount=net,
        refunds=refunds,
    )


def _error_code(response: httpx.Response) -> str:
    """The first Asaas error code of a 4xx body; its free-text description is never used."""
    try:
        body = _obj(json.loads(response.content))
        first = _obj(_items(None if body is None else body.get("errors"))[0])
        code = None if first is None else first.get("code")
    except ValueError, IndexError:
        return "invalid_request"
    if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
        return code
    return "invalid_request"


# --- the adapter ---------------------------------------------------------------------------------


class AsaasPaymentProvider:
    """One Asaas account's client (the API key is held privately and only ever sent as a header)."""

    __slots__ = ("_api_key", "_base_url", "_transport")

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = SANDBOX_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._transport = transport

    def __repr__(self) -> str:
        return "AsaasPaymentProvider(<redacted>)"

    __str__ = __repr__

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    # --- transport ------------------------------------------------------------------------------

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        body: bytes | None = None,
        write: bool = False,
    ) -> dict[str, Any]:
        """One HTTP call -> the JSON object of a 2xx answer, or the typed error it maps to."""
        headers = {
            API_KEY_HEADER: self._api_key,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        logger.debug(
            "payments.asaas.request", method=method, path=path, headers=redact_headers(headers)
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                transport=self._transport,
                timeout=TIMEOUT,
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method, path, params=params, content=body, headers=headers
                )
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout:
            # nothing reached Asaas: safe for the caller to look up and try again
            raise ProviderRetryable("connect_error") from None
        except httpx.TimeoutException:
            raise _after_send(write, "timeout") from None
        except httpx.TransportError:
            raise _after_send(write, "connection_error") from None
        logger.debug(
            "payments.asaas.response", method=method, path=path, status=response.status_code
        )
        return self._answer(response, write=write)

    @staticmethod
    def _answer(response: httpx.Response, *, write: bool) -> dict[str, Any]:
        status = response.status_code
        if 200 <= status < 300:
            try:
                parsed = _obj(json.loads(response.content, parse_float=Decimal))
            except ValueError:
                parsed = None
            if parsed is None:
                raise _after_send(write, "invalid_response", read_is_retryable=False)
            return parsed
        if status == 429:
            raise ProviderRetryable("rate_limited")
        if status == 401:
            raise ProviderRejected("invalid_credentials")
        if status == 403:
            raise ProviderRejected("forbidden")
        if status == 404:
            raise ProviderRejected("not_found")
        if 400 <= status < 500:
            raise ProviderRejected(_error_code(response))
        raise _after_send(write, "provider_error" if status >= 500 else "unexpected_status")

    # --- lookups --------------------------------------------------------------------------------

    async def _find(self, path: str, external_reference: str, kind: str) -> list[dict[str, Any]]:
        """Objects of ``path`` whose externalReference is exactly ``external_reference``.

        Asaas' filter is not documented to be exact or even honoured (research s9.3), so the page
        is re-checked client-side and an inconclusive page is never read as "absent".
        """
        if not external_reference:
            raise ProviderRejected("invalid_external_reference")
        data = await self._call(
            "GET",
            path,
            params={"externalReference": external_reference, "limit": LOOKUP_PAGE_SIZE},
        )
        matches = [
            item
            for raw in _items(data.get("data"))
            if (item := _obj(raw)) is not None
            and item.get("externalReference") == external_reference
        ]
        if len(matches) > 1:
            logger.warning(
                "payments.asaas.ambiguous_external_reference",
                kind=kind,
                external_reference=external_reference,
                provider_ids=[str(item.get("id")) for item in matches],
            )
            raise ProviderUncertain("ambiguous_external_reference")
        if data.get("hasMore") is True:
            raise ProviderUncertain("lookup_inconclusive")
        return matches

    async def lookup_by_external_reference(
        self, external_reference: str
    ) -> ProviderPaymentSnapshot | None:
        matches = await self._find("payments", external_reference, "payment")
        return _snapshot(matches[0]) if matches else None

    async def get_payment(self, provider_payment_id: str) -> ProviderPaymentSnapshot:
        return _snapshot(await self._get_payment_object(provider_payment_id))

    async def _get_payment_object(self, provider_payment_id: str) -> dict[str, Any]:
        return await self._call("GET", f"payments/{_checked_id(provider_payment_id)}")

    # --- reconciliation reads (ADR-0013) --------------------------------------------------------

    async def list_payments(
        self, created_from: date, created_to: date
    ) -> list[ProviderPaymentSnapshot]:
        """``GET /payments`` filtered by ``dateCreated`` (Asaas' calendar), every page or an error.

        A page that makes no progress, or more than :data:`LIST_MAX_PAGES` pages, is
        :class:`ProviderUncertain`: a partial listing would read as ``missing_external`` breaks.
        """
        if created_to < created_from:
            raise ProviderRejected("invalid_window")
        found: dict[str, ProviderPaymentSnapshot] = {}
        offset = 0
        for _ in range(LIST_MAX_PAGES):
            data = await self._call(
                "GET",
                "payments",
                params={
                    "dateCreated[ge]": _iso(created_from),
                    "dateCreated[le]": _iso(created_to),
                    "limit": LIST_PAGE_SIZE,
                    "offset": offset,
                },
            )
            page = [item for raw in _items(data.get("data")) if (item := _obj(raw)) is not None]
            for item in page:
                snapshot = _snapshot(item)
                found[snapshot.provider_payment_id] = snapshot
            if data.get("hasMore") is not True:
                return list(found.values())
            if not page:
                raise ProviderUncertain("listing_stalled")
            offset += len(page)
        raise ProviderUncertain("listing_too_long")

    async def get_balance(self) -> ProviderBalance:
        """``GET /finance/balance``: the account's available balance (may be negative)."""
        data = await self._call("GET", "finance/balance")
        try:
            return ProviderBalance(_signed_asaas_value(data.get("balance")))
        except _InvalidResponse:
            raise ProviderUncertain("invalid_response") from None

    # --- customers ------------------------------------------------------------------------------

    async def create_customer(
        self, request: ProviderCustomerRequest, *, cpf_cnpj: str | None = None
    ) -> ProviderCustomer:
        """Find the customer by ``externalReference`` first; create it only if absent.

        ``cpf_cnpj`` is the identity's decrypted document, passed by the service right before the
        call (W4.4); it only travels in the request body and is never logged. Without one Asaas
        refuses the create with its error code. The lookup first is what makes a retry after an
        unknown outcome safe (no duplicate customer). Notifications are disabled: Asaas would
        otherwise e-mail/SMS the customer.
        """
        found = await self._find("customers", request.external_reference, "customer")
        if found:
            return _customer(found[0])
        fields: dict[str, object] = {
            "name": request.name,
            "externalReference": request.external_reference,
            "notificationDisabled": True,
        }
        if request.email:
            fields["email"] = request.email
        phone = _national_phone(request.phone)
        if phone is not None:
            fields["mobilePhone"] = phone
        if cpf_cnpj:
            fields["cpfCnpj"] = cpf_cnpj
        data = await self._call("POST", "customers", body=_json_text(fields, {}), write=True)
        return _customer(data)

    async def _customer_for(self, customer_reference: str) -> str:
        found = await self._find("customers", customer_reference, "customer")
        if not found:
            raise ProviderRejected("customer_not_provisioned")
        return _customer(found[0]).provider_customer_id

    # --- payments -------------------------------------------------------------------------------

    async def create_payment(self, request: CreatePaymentRequest) -> ProviderPaymentSnapshot:
        billing = BILLING_TYPES.get(request.method)
        if billing is None:
            raise ProviderRejected("method_not_supported")
        value = to_asaas_value(request.amount)
        # Look before writing: Asaas has no idempotency key, so a hit means an earlier attempt won.
        if await self._find("payments", request.external_reference, "payment"):
            raise ProviderDuplicate("duplicate_external_reference")
        customer = request.provider_customer_id or await self._customer_for(
            request.customer_reference
        )
        fields: dict[str, object] = {
            "customer": _checked_id(customer),
            "billingType": billing,
            "dueDate": _iso(request.due_date),
            "externalReference": request.external_reference,
        }
        if request.description:
            fields["description"] = request.description
        data = await self._call(
            "POST", "payments", body=_json_text(fields, {"value": value}), write=True
        )
        return _snapshot(data)

    async def refund(self, request: RefundRequest) -> ProviderRefundSnapshot:
        """Refund ``request.amount``; our ``rf_`` id travels in the refund ``description``."""
        payment_id = _checked_id(request.provider_payment_id)
        value = to_asaas_value(request.amount)
        before = _snapshot(await self._get_payment_object(payment_id))
        if any(r.external_reference == request.external_reference for r in before.refunds):
            raise ProviderDuplicate("duplicate_external_reference")
        data = await self._call(
            "POST",
            f"payments/{payment_id}/refund",
            body=_json_text({"description": request.external_reference}, {"value": value}),
            write=True,
        )
        for refund in _snapshot(data).refunds:
            if refund.external_reference == request.external_reference:
                return refund
        # The refund may well exist; only the payment's refunds[] can tell (the resolver reads it).
        raise ProviderUncertain("refund_not_confirmed")

    async def cancel(self, provider_payment_id: str) -> ProviderPaymentSnapshot:
        """Delete a charge that is still unpaid; anything else is refused."""
        payment_id = _checked_id(provider_payment_id)
        current = _snapshot(await self._get_payment_object(payment_id))
        if current.capture_status is CaptureStatus.CANCELED:
            return current
        if current.capture_status is not CaptureStatus.PENDING:
            raise ProviderRejected("not_cancelable")
        data = await self._call("DELETE", f"payments/{payment_id}", write=True)
        if data.get("deleted") is not True:
            raise ProviderRejected("not_cancelable")
        return replace(
            current,
            capture_status=asaas_status.DELETED.capture,
            raw_status=asaas_status.DELETED_RAW_STATUS,
        )


# --- helpers -------------------------------------------------------------------------------------


def _after_send(write: bool, code: str, *, read_is_retryable: bool = True) -> Exception:
    """The error for a failure that may follow a sent request: uncertain for writes."""
    if write or not read_is_retryable:
        return ProviderUncertain(code)
    return ProviderRetryable(code if code != "provider_error" else "provider_unavailable")


def _checked_id(value: str) -> str:
    """An Asaas id for a URL path or body: letters, digits and underscores only."""
    if not _ID.fullmatch(value):
        raise ProviderRejected("invalid_id")
    return value


def _iso(value: date) -> str:
    return value.isoformat()


def _snapshot(payload: Mapping[str, Any]) -> ProviderPaymentSnapshot:
    try:
        return payment_snapshot(payload)
    except _InvalidResponse:
        raise ProviderUncertain("invalid_response") from None


def _national_phone(phone: str | None) -> str | None:
    """A Brazilian number as Asaas takes it (DDD + number, 10 or 11 digits), else ``None``.

    Customers store phones in E.164 (``+5511987654321``): the country code is dropped.
    """
    if not phone:
        return None
    digits = _DIGITS.sub("", phone)
    if phone.lstrip().startswith("+"):
        if not digits.startswith("55"):
            return None
        digits = digits[2:]
    return digits if len(digits) in (10, 11) else None


def _customer(payload: Mapping[str, Any]) -> ProviderCustomer:
    try:
        return ProviderCustomer(provider_customer_id=_text(payload, "id", required=True) or "")
    except _InvalidResponse:
        raise ProviderUncertain("invalid_response") from None


def asaas_factory(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> Callable[[UUID, ProviderCredentials], PaymentProvider]:
    """The registry factory for ``asaas`` accounts (``transport`` is for tests).

    Only the sandbox is wired: credentials of a ``production`` account are refused with
    ``environment_not_supported`` rather than silently sent to the sandbox. The caller (the
    credentials loader) states the account environment next to the secrets.
    """

    def build(_account_id: UUID, credentials: ProviderCredentials) -> PaymentProvider:
        api_key = credentials.get("api_key")
        if not api_key:
            raise ProviderUnavailable("credentials_unavailable")
        if credentials.get("environment") != SANDBOX_ENVIRONMENT:
            raise ProviderUnavailable("environment_not_supported")
        return AsaasPaymentProvider(api_key, transport=transport)

    return build

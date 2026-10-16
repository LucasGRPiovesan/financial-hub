"""Inbound provider webhooks (W4.4, docs/INTEGRATIONS.md s2): public, authenticated by token.

``POST /v1/webhooks/asaas/{provider_account_id}`` takes Asaas notifications for one provider
account. There is no session, API key or CSRF check: the ``asaas-access-token`` header must equal
the account's ``webhook_token`` credential, and an unknown account and a wrong token get the same
401. The body is capped before it is read, stored raw, deduplicated by the Asaas event id and
processed by the worker, so the answer is a fast 200 (a duplicate also gets 200 and does nothing).
"""

from typing import Annotated, Final

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from financial_hub.modules.payments.application.config import PaymentsSettings
from financial_hub.modules.payments.application.webhooks import ASAAS_PROVIDER, WebhookInbox
from financial_hub.platform.http.auth import public
from financial_hub.shared.errors import ValidationFailed

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

TOKEN_HEADER: Final = "asaas-access-token"  # noqa: S105 - a header name, not a secret


class WebhookAck(BaseModel):
    received: bool = True


def _settings(request: Request) -> PaymentsSettings:
    settings: PaymentsSettings | None = getattr(request.app.state, "payments_settings", None)
    if settings is None:
        settings = PaymentsSettings()
        request.app.state.payments_settings = settings
    return settings


def _too_large(limit: int) -> HTTPException:
    return HTTPException(
        status.HTTP_413_CONTENT_TOO_LARGE, detail=f"The body must be at most {limit} bytes."
    )


async def _capped_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            too_large = int(declared) > limit
        except ValueError:
            raise ValidationFailed("Invalid Content-Length.") from None
        if too_large:
            raise _too_large(limit)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _too_large(limit)
    return bytes(body)


def _inbox(request: Request) -> WebhookInbox:
    state = request.app.state
    return WebhookInbox(
        state.session_factory,
        clock=state.clock,
        # set by the composition root (main.py); without it every request is refused
        credentials=getattr(state, "payment_credentials", None),
    )


@router.post(
    "/asaas/{provider_account_id}",
    dependencies=[public()],
    responses={401: {"description": "Unknown account or wrong token (same answer)."}},
)
async def receive_asaas_webhook(
    provider_account_id: str,
    request: Request,
    token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
) -> WebhookAck:
    """Store one Asaas notification for processing (duplicates are acknowledged and dropped)."""
    body = await _capped_body(request, _settings(request).webhook_max_body_bytes)
    await _inbox(request).receive(ASAAS_PROVIDER, provider_account_id, token, body)
    return WebhookAck()

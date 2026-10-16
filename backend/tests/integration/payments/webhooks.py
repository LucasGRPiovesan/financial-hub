"""Send Asaas webhook fixtures to the hub (W4.4, docs/INTEGRATIONS.md s2).

In tests, :func:`post_webhook` goes through the ASGI app. Against a running hub (local or behind a
tunnel) the module is also a small CLI that posts the fixture for one payment, with the token read
from the environment so it never appears in the shell history::

    export FH_WEBHOOK_TOKEN=...   # the account's webhook_token credential
    docker compose run --rm -e FH_WEBHOOK_TOKEN api python -m tests.integration.payments.webhooks \
        --url http://api:8000 --account pa_... --payment pay_... --reference pi_...
"""

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx

from tests.integration.identity.support import Api

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN_HEADER = "asaas-access-token"


def notification(
    payment_id: str,
    reference: str | None = None,
    *,
    event: str = "PAYMENT_RECEIVED",
    event_id: str | None = None,
    **payment: Any,
) -> dict[str, Any]:
    """The fixture notification for one payment, with a fresh event id unless one is given."""
    body: dict[str, Any] = json.loads((FIXTURES / "asaas_payment_received.json").read_text())
    body["id"] = event_id or f"evt_{secrets.token_hex(16)}&{secrets.randbelow(10**9)}"
    body["event"] = event
    body["payment"].update(id=payment_id, externalReference=reference, **payment)
    return body


def encoded(body: dict[str, Any] | bytes | str) -> bytes:
    if isinstance(body, bytes):
        return body
    return (body if isinstance(body, str) else json.dumps(body)).encode()


async def post_webhook(
    api: Api,
    account_id: str,
    token: str | None,
    body: dict[str, Any] | bytes | str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    sent = {"content-type": "application/json", **(headers or {})}
    if token is not None:
        sent[TOKEN_HEADER] = token
    async with api.client() as client:
        return await client.post(
            f"/v1/webhooks/asaas/{account_id}", content=encoded(body), headers=sent
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post an Asaas webhook fixture to the hub.")
    parser.add_argument("--url", default="http://localhost:8000", help="the hub's base URL")
    parser.add_argument("--account", required=True, help="provider account id (pa_...)")
    parser.add_argument("--payment", required=True, help="Asaas payment id (pay_...)")
    parser.add_argument("--reference", help="the hub's payment id (pi_...)")
    parser.add_argument("--event", default="PAYMENT_RECEIVED")
    args = parser.parse_args(argv)
    token = os.environ.get("FH_WEBHOOK_TOKEN")
    if not token:
        print("FH_WEBHOOK_TOKEN is not set", file=sys.stderr)
        return 2
    body = notification(args.payment, args.reference, event=args.event)
    response = httpx.post(
        f"{args.url.rstrip('/')}/v1/webhooks/asaas/{args.account}",
        content=encoded(body),
        headers={"content-type": "application/json", TOKEN_HEADER: token},
        timeout=10,
    )
    print(response.status_code, response.text)
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())

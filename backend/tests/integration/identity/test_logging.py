"""Nothing secret reaches the logs: passwords, session tokens, CSRF tokens, cookies, full emails."""

import json
from typing import Any

import pytest

from tests.integration.identity.support import PASSWORD, Api, csrf, login, unique_email

NEW_PASSWORD = "a brand new passphrase"
WRONG_PASSWORD = "wrong password guess"


def events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for record in caplog.records:
        try:
            parsed.append(json.loads(record.getMessage()))
        except ValueError:
            continue
    return parsed


async def test_auth_flow_logs_events_without_secrets(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    operator = await api.operator(email=unique_email("logcheck"))
    unknown = unique_email("ghost")
    async with api.client() as client:
        await login(client, operator, password=WRONG_PASSWORD)
        await client.post("/v1/auth/login", json={"email": unknown, "password": WRONG_PASSWORD})
        response = await login(client, operator)
        csrf_token = response.json()["csrf_token"]
        cookie = client.cookies["fh_session"]
        await client.get("/v1/auth/me")
        await client.post(
            "/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=csrf(csrf_token),
        )
        await client.post("/v1/auth/logout", headers=csrf(csrf_token))

    text = caplog.text + "\n".join(record.getMessage() for record in caplog.records)
    for secret in (
        PASSWORD,
        NEW_PASSWORD,
        WRONG_PASSWORD,
        cookie,
        csrf_token,
        operator.email,
        unknown,
    ):
        assert secret not in text, f"{secret!r} leaked into the logs"

    logged = events(caplog)
    names = [event["event"] for event in logged]
    assert names.count("auth.login_failed") == 2
    assert "auth.login_succeeded" in names
    assert "auth.password_changed" in names
    assert "auth.logout" in names
    failed = [event for event in logged if event["event"] == "auth.login_failed"]
    assert {event["email"] for event in failed} == {"l***@example.test", "g***@example.test"}
    assert {event["reason"] for event in failed} == {"rejected", "unknown_email"}
    succeeded = next(event for event in logged if event["event"] == "auth.login_succeeded")
    assert succeeded["operator_id"] == operator.id

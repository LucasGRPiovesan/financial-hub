"""Project API keys as an authentication scheme, and the wiring of this module's collaborators.

Precedence (docs/SECURITY.md): ``Authorization: Bearer fh_...`` is checked **before** the operator
session cookie. A request carrying a ``fh_`` bearer token is judged on that token alone: if it is
invalid the request gets 401 even when a valid session cookie is also present (explicit
credentials are never silently replaced by ambient ones). Requests without a ``fh_`` bearer token
fall through to the session authenticator. Bearer authentication is not ambient (browsers never
attach it on their own), so it needs no CSRF token.
"""

from datetime import timedelta

from fastapi import FastAPI, Request

from financial_hub.modules.projects.application.api_keys import (
    BEARER_KEY_PREFIX,
    ApiKeyAuthService,
)
from financial_hub.platform.crypto.keyring import Keyring
from financial_hub.platform.http.auth import Principal, add_authenticator
from financial_hub.platform.settings import Settings

AUTHORIZATION_HEADER = "Authorization"
BEARER_SCHEME = "bearer"


class ApiKeyAuthenticator:
    """``Authorization: Bearer fh_<env>_<prefix>_<secret>``."""

    async def authenticate(self, request: Request) -> Principal | None:
        header = request.headers.get(AUTHORIZATION_HEADER)
        if header is None:
            return None
        scheme, _, token = header.partition(" ")
        token = token.strip()
        if scheme.lower() != BEARER_SCHEME or not token.startswith(BEARER_KEY_PREFIX):
            return None
        state = request.app.state
        service = ApiKeyAuthService(
            state.session_factory,
            clock=state.clock,
            pepper=state.api_key_pepper,
            environment=state.api_key_environment,
            touch_interval=state.api_key_touch_interval,
        )
        return await service.authenticate(token)


def configure_projects(app: FastAPI, settings: Settings) -> None:
    """Store the module's secrets/config on ``app.state`` and plug API keys in ahead of sessions."""
    app.state.api_key_pepper = settings.api_key_pepper.get_secret_value().encode()
    app.state.api_key_environment = settings.api_key_environment
    app.state.api_key_touch_interval = timedelta(seconds=settings.api_key_touch_interval_seconds)
    app.state.keyring = Keyring.from_settings(settings)
    add_authenticator(app, ApiKeyAuthenticator(), first=True)

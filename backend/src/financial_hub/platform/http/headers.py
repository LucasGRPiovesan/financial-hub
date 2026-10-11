"""Security headers on every HTTP response (docs/SECURITY.md, Browser row). Pure ASGI."""

from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS: Final = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    # The API is never framed; the SPA's own CSP (default-src 'self') is served by the web tier.
    ("Content-Security-Policy", "frame-ancestors 'none'"),
    ("X-Frame-Options", "DENY"),
)
NO_STORE_PATH_PREFIXES: Final = ("/v1/auth/",)


class SecurityHeadersMiddleware:
    """Adds the baseline headers (keeping any a route set itself) and forces ``no-store`` on
    authentication responses, which carry session cookies and CSRF tokens."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        no_store = path.startswith(NO_STORE_PATH_PREFIXES)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    headers.setdefault(name, value)
                if no_store:
                    headers["Cache-Control"] = "no-store"
                    headers["Pragma"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_headers)

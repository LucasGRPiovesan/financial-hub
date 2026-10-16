"""Who is acting and from where: the request facts every audit record carries."""

import ipaddress
from dataclasses import dataclass, replace
from typing import Any

from starlette.requests import Request

from financial_hub.modules.audit.domain import USER_AGENT_MAX_LENGTH, Actor


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Actor plus correlation/request id, client ip and user agent (all optional off-HTTP)."""

    actor: Actor
    correlation_id: str | None = None
    request_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None

    @classmethod
    def system(cls) -> AuditContext:
        """Non-HTTP paths (CLI, workers): the system acts, with no request facts."""
        return cls(Actor.system())

    def acting(self, actor: Actor) -> AuditContext:
        """The same request facts with a different actor (login learns the actor late)."""
        return replace(self, actor=actor)


def _storable_ip(host: str | None) -> str | None:
    if host is None:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def request_audit_context(request: Request) -> AuditContext:
    """FastAPI dependency: request facts with the ``system`` actor.

    Handlers bind the real actor (``context.acting(...)``) once they know the principal.
    """
    state: Any = request.state
    user_agent = request.headers.get("user-agent")
    return AuditContext(
        actor=Actor.system(),
        correlation_id=getattr(state, "correlation_id", None),
        request_id=getattr(state, "request_id", None),
        ip=_storable_ip(request.client.host if request.client else None),
        user_agent=user_agent[:USER_AGENT_MAX_LENGTH] if user_agent else None,
    )

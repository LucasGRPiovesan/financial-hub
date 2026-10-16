"""``/v1/auth``: operator login, logout, current operator and password change (contract of 9.1).

The session token travels only in the ``fh_session`` cookie (HttpOnly, SameSite=Lax, Secure
outside dev, Path=/). Unsafe methods authenticated by that cookie must send ``X-CSRF-Token``
(returned by login and ``/me``). Login is the one unauthenticated POST: FastAPI only parses
``application/json`` bodies, which a cross-site HTML form cannot send without a CORS preflight,
so a forged form post fails validation before any credential is checked.
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from pydantic import BaseModel, Field

from financial_hub.modules.audit.application import AuditContext, request_audit_context
from financial_hub.modules.identity.application.service import (
    AuthService,
    IdentityConfig,
    MeView,
    OperatorPrincipal,
    OperatorView,
    PasswordHasher,
    default_password_hasher,
)
from financial_hub.platform.http.auth import Principal, authenticated, install_authenticators
from financial_hub.platform.http.auth import public as public_access
from financial_hub.platform.http.protection.rate_limit import AuthAttempt
from financial_hub.platform.settings import Settings
from financial_hub.shared.clock import Clock
from financial_hub.shared.errors import Forbidden

SESSION_COOKIE: Final = "fh_session"
CSRF_HEADER: Final = "X-CSRF-Token"
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
# Generous caps: real limits are enforced by the policy; these only bound request size.
_EMAIL_MAX: Final = 320
_PASSWORD_MAX: Final = 1024


# --- wiring --------------------------------------------------------------------------------------


def configure_identity(
    app: FastAPI,
    settings: Settings,
    *,
    clock: Clock,
    hasher: PasswordHasher | None = None,
) -> None:
    """Store identity's collaborators on ``app.state`` and plug the session authenticator in."""
    app.state.identity_config = IdentityConfig.from_settings(settings)
    app.state.password_hasher = hasher or default_password_hasher()
    app.state.identity_clock = clock
    app.state.cookies_secure = settings.cookies_secure
    install_authenticators(app, SessionAuthenticator())


def auth_service(request: Request) -> AuthService:
    state = request.app.state
    return AuthService(
        state.session_factory,
        clock=state.identity_clock,
        hasher=state.password_hasher,
        config=state.identity_config,
    )


class SessionAuthenticator:
    """Operator session cookie; enforces CSRF on unsafe methods."""

    async def authenticate(self, request: Request) -> Principal | None:
        token = request.cookies.get(SESSION_COOKIE)
        if token is None:
            return None
        return await auth_service(request).authenticate(
            token,
            csrf_token=request.headers.get(CSRF_HEADER),
            require_csrf=request.method not in SAFE_METHODS,
        )


def _operator_principal(principal: Principal) -> OperatorPrincipal:
    if not isinstance(principal, OperatorPrincipal):
        raise Forbidden("This endpoint requires an operator session.")
    return principal


def _set_session_cookie(request: Request, response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        path="/",
        secure=request.app.state.cookies_secure,
        httponly=True,
        samesite="lax",
    )


def _clear_session_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=request.app.state.cookies_secure,
        httponly=True,
        samesite="lax",
    )


# --- schemas -------------------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=_EMAIL_MAX)
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=_PASSWORD_MAX)
    new_password: str = Field(min_length=1, max_length=_PASSWORD_MAX)


class OperatorOut(BaseModel):
    id: str
    email: str
    display_name: str

    @classmethod
    def of(cls, view: OperatorView) -> OperatorOut:
        return cls(id=view.id, email=view.email, display_name=view.display_name)


class LoginResponse(BaseModel):
    operator: OperatorOut
    csrf_token: str


class MembershipOut(BaseModel):
    project_id: str
    project_name: str
    role: str


class MeResponse(BaseModel):
    operator: OperatorOut
    memberships: list[MembershipOut]
    csrf_token: str

    @classmethod
    def of(cls, view: MeView) -> MeResponse:
        return cls(
            operator=OperatorOut.of(view.operator),
            memberships=[
                MembershipOut(
                    project_id=item.project_id, project_name=item.project_name, role=item.role
                )
                for item in view.memberships
            ],
            csrf_token=view.csrf_token,
        )


# --- routes --------------------------------------------------------------------------------------

router = APIRouter(prefix="/v1/auth", tags=["auth"])

Service = Annotated[AuthService, Depends(auth_service)]
CurrentPrincipal = Annotated[Principal, authenticated()]
AuditCtx = Annotated[AuditContext, Depends(request_audit_context)]


@router.post("/login", dependencies=[public_access()])
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    service: Service,
    audit: AuditCtx,
    _attempt: AuthAttempt,
) -> LoginResponse:
    result = await service.login(
        email=body.email,
        password=body.password,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        presented_token=request.cookies.get(SESSION_COOKIE),
        audit=audit,
    )
    config: IdentityConfig = request.app.state.identity_config
    max_age = int(config.session.absolute_timeout.total_seconds())
    _set_session_cookie(request, response, result.session_token, max_age)
    return LoginResponse(operator=OperatorOut.of(result.operator), csrf_token=result.csrf_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    principal: CurrentPrincipal, request: Request, service: Service, audit: AuditCtx
) -> Response:
    await service.logout(_operator_principal(principal), audit)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookie(request, response)
    return response


@router.get("/me")
async def me(principal: CurrentPrincipal, service: Service) -> MeResponse:
    return MeResponse.of(await service.me(_operator_principal(principal)))


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChangeRequest, principal: CurrentPrincipal, service: Service, audit: AuditCtx
) -> Response:
    await service.change_password(
        _operator_principal(principal),
        current_password=body.current_password,
        new_password=body.new_password,
        audit=audit,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

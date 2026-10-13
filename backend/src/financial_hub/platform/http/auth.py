"""Route access declarations and the principal abstraction shared by every authentication scheme.

Every route declares exactly how it may be reached, through one FastAPI dependency:

- ``public()``: no authentication (health checks, login);
- ``authenticated()``: any authenticated principal (operator-level routes); returns the
  ``Principal``;
- ``require(permission)``: a permission inside the project named by the ``project_id`` path
  parameter (TypeID ``prj_``); returns the ``ProjectScope`` repositories require (ADR-0011).
  Principals that cannot see the project get 404 (no project enumeration); visible projects
  without the permission get 403.

Authentication schemes plug in as ``Authenticator`` objects (``install_authenticators`` /
``add_authenticator``): the operator session cookie and project API keys (``Authorization:
Bearer fh_...``, checked first). The first one that recognises the request wins.
``undeclared_routes`` lists routes without a declaration; ``create_app`` refuses to start with any
and a test enumerates ``effective_routes`` too.
"""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.routing import BaseRoute

from financial_hub.shared.errors import Forbidden, NotFound, Unauthorized
from financial_hub.shared.ids import IdPrefix, InvalidIdError, parse_id

PROJECT_PATH_PARAM: Final = "project_id"
ACCESS_ATTRIBUTE: Final = "__fh_access__"

type AccessKind = Literal["public", "authenticated", "permission"]


@dataclass(frozen=True, slots=True)
class ProjectScope:
    """What one principal may do inside one project. Repositories take it instead of a bare id."""

    project_id: UUID
    principal_kind: str
    principal_id: str
    permissions: frozenset[str]

    def allows(self, permission: str) -> bool:
        return permission in self.permissions


class Principal(Protocol):
    """An authenticated caller (operator session, API key...)."""

    @property
    def kind(self) -> str:
        """Scheme name, e.g. ``operator`` or ``api_key``."""
        ...

    @property
    def subject_id(self) -> str:
        """Public prefixed id of the caller (``opr_...``, ``key_...``)."""
        ...

    @property
    def project_ids(self) -> frozenset[UUID]:
        """Projects the caller can see at all."""
        ...

    def scope_for(self, project_id: UUID) -> ProjectScope | None:
        """The caller's scope in ``project_id``; ``None`` when the project is not visible."""
        ...


class Authenticator(Protocol):
    async def authenticate(self, request: Request) -> Principal | None:
        """``None`` when the request carries no credentials of this scheme.

        Raise ``Unauthorized``/``Forbidden`` for credentials that are present but unacceptable
        in a way the caller must hear about (e.g. a missing CSRF token on an unsafe method).
        """
        ...


@dataclass(frozen=True, slots=True)
class AccessDeclaration:
    kind: AccessKind
    permission: str | None = None


def install_authenticators(app: FastAPI, *authenticators: Authenticator) -> None:
    app.state.authenticators = tuple(authenticators)


def add_authenticator(app: FastAPI, authenticator: Authenticator, *, first: bool = False) -> None:
    """Plug one more scheme into the chain: last by default, ``first=True`` to take precedence.

    Precedence matters when a request carries credentials of several schemes: the first scheme
    that recognises the request decides (and may reject it) without consulting the others.
    """
    current: tuple[Authenticator, ...] = getattr(app.state, "authenticators", ())
    app.state.authenticators = (authenticator, *current) if first else (*current, authenticator)


def _declare[F: Callable[..., Any]](function: F, declaration: AccessDeclaration) -> F:
    setattr(function, ACCESS_ATTRIBUTE, declaration)
    return function


async def current_principal(request: Request) -> Principal:
    """Authenticate once per request (cached on ``request.state``)."""
    cached: Principal | None = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    authenticators: Sequence[Authenticator] = getattr(request.app.state, "authenticators", ())
    for authenticator in authenticators:
        principal = await authenticator.authenticate(request)
        if principal is not None:
            request.state.principal = principal
            return principal
    raise Unauthorized("Authentication required.")


def public() -> Any:
    """Declare a route reachable without authentication."""

    async def public_access() -> None:
        return None

    return Depends(_declare(public_access, AccessDeclaration("public")))


def authenticated() -> Any:
    """Declare an operator-level route: any authenticated principal; injects the ``Principal``."""

    async def authenticated_access(request: Request) -> Principal:
        return await current_principal(request)

    return Depends(_declare(authenticated_access, AccessDeclaration("authenticated")))


def require(permission: str) -> Any:
    """Declare a project-scoped route needing ``permission``; injects the ``ProjectScope``."""

    async def permission_access(request: Request) -> ProjectScope:
        principal = await current_principal(request)
        raw = request.path_params.get(PROJECT_PATH_PARAM)
        try:
            project_id = parse_id(str(raw), IdPrefix.PROJECT)
        except InvalidIdError:
            raise NotFound("Project not found.") from None
        scope = principal.scope_for(project_id)
        if scope is None:
            raise NotFound("Project not found.")
        if not scope.allows(permission):
            raise Forbidden(f"Missing permission {permission}.")
        return scope

    return Depends(_declare(permission_access, AccessDeclaration("permission", str(permission))))


def _walk(dependant: Dependant) -> Iterator[Dependant]:
    for child in dependant.dependencies:
        yield child
        yield from _walk(child)


@dataclass(frozen=True, slots=True)
class EffectiveRoute:
    """A route as the app serves it: full path and the dependency tree that actually runs."""

    route: BaseRoute
    path: str
    path_format: str
    methods: frozenset[str]
    dependant: Dependant | None

    @property
    def label(self) -> str:
        return f"{','.join(sorted(self.methods))} {self.path}".strip()


def effective_routes(app: FastAPI) -> Iterator[EffectiveRoute]:
    """Every served route, including those of included routers (resolved lazily since FastAPI
    0.14x: ``app.routes`` holds the included router, not copies of its routes). Path and
    dependencies include the ``include_router`` prefix and dependencies.
    """
    for context in iter_route_contexts(app.routes):
        original = context.original_route
        path = context.path or getattr(original, "path", repr(original))
        dependant: Dependant | None = None
        if isinstance(original, APIRoute):
            effective: Any = context.dependant
            dependant = effective if isinstance(effective, Dependant) else original.dependant
        yield EffectiveRoute(
            route=original,
            path=path,
            path_format=context.path_format or path,
            methods=frozenset(context.methods or ()),
            dependant=dependant,
        )


def route_access(route: APIRoute | EffectiveRoute) -> list[AccessDeclaration]:
    """Every access declaration found in the route's dependency tree."""
    found: list[AccessDeclaration] = []
    if route.dependant is None:
        return found
    for dependant in _walk(route.dependant):
        declaration = getattr(dependant.call, ACCESS_ATTRIBUTE, None)
        if isinstance(declaration, AccessDeclaration):
            found.append(declaration)
    return found


def _framework_paths(app: FastAPI) -> set[str]:
    """OpenAPI schema and docs pages FastAPI registers itself (public by construction)."""
    paths = {app.openapi_url, app.docs_url, app.redoc_url}
    if app.docs_url:
        paths.add(app.swagger_ui_oauth2_redirect_url)
    return {path for path in paths if path}


def undeclared_routes(app: FastAPI) -> list[str]:
    """Routes without exactly one access declaration (or misdeclared), as ``"METHODS path"``."""
    problems: list[str] = []
    framework = _framework_paths(app)
    for route in effective_routes(app):
        if route.dependant is None:
            if route.path not in framework:
                problems.append(f"{route.path}: not an API route, cannot declare access")
            continue
        label = route.label
        declarations = route_access(route)
        if len(declarations) != 1:
            problems.append(f"{label}: {len(declarations)} access declarations (need exactly 1)")
        elif (
            declarations[0].kind == "permission"
            and f"{{{PROJECT_PATH_PARAM}}}" not in route.path_format
        ):
            problems.append(f"{label}: require() needs a {{{PROJECT_PATH_PARAM}}} path parameter")
    return problems


def verify_route_declarations(app: FastAPI) -> None:
    problems = undeclared_routes(app)
    if problems:
        raise RuntimeError("routes without a valid access declaration: " + "; ".join(problems))

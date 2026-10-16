from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.audit.api.routes import router as audit_router
from financial_hub.modules.customers.api.routes import router as customers_router
from financial_hub.modules.events.api.explorer import router as events_router
from financial_hub.modules.events.api.stream import router as stream_router
from financial_hub.modules.events.application.realtime import build_broker
from financial_hub.modules.identity.api.routes import configure_identity
from financial_hub.modules.identity.api.routes import demo_router as demo_auth_router
from financial_hub.modules.identity.api.routes import router as auth_router
from financial_hub.modules.identity.application.service import PasswordHasher
from financial_hub.modules.ledger.api.routes import router as ledger_router
from financial_hub.modules.metering.api.routes import metrics_router, usage_router
from financial_hub.modules.payments.api.routes import router as payments_router
from financial_hub.modules.payments.api.webhooks import router as provider_webhooks_router
from financial_hub.modules.payments.application.api import (
    build_registry,
    customers_contact_loader,
    projects_credentials_loader,
)
from financial_hub.modules.projects.api.api_keys import router as api_keys_router
from financial_hub.modules.projects.api.auth import configure_projects
from financial_hub.modules.projects.api.members import router as members_router
from financial_hub.modules.projects.api.providers import catalog_router as provider_catalog_router
from financial_hub.modules.projects.api.providers import router as provider_accounts_router
from financial_hub.modules.projects.api.routes import router as projects_router
from financial_hub.modules.webhooks.api.routes import retry_router as webhook_retry_router
from financial_hub.modules.webhooks.api.routes import router as webhooks_router
from financial_hub.modules.webhooks.application.api import target_policy as webhook_target_policy
from financial_hub.platform.db import create_engine, create_session_factory
from financial_hub.platform.http.auth import public, verify_route_declarations
from financial_hub.platform.http.headers import SecurityHeadersMiddleware
from financial_hub.platform.http.middleware import CorrelationIdMiddleware
from financial_hub.platform.http.problems import Problem, register_exception_handlers
from financial_hub.platform.http.protection import configure_protection
from financial_hub.platform.logging import configure_logging
from financial_hub.platform.settings import Settings, get_settings
from financial_hub.platform.telemetry import (
    Telemetry,
    configure_telemetry,
    instrument_app,
    instrument_engine,
)
from financial_hub.shared.clock import Clock, SystemClock

logger = structlog.get_logger()


def _lifespan(
    settings: Settings, telemetry: Telemetry | None
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        engine = create_engine(settings)
        if telemetry is not None:
            instrument_engine(engine, telemetry)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        broker = build_broker(engine, clock=app.state.clock)
        app.state.realtime = broker
        await broker.start()
        logger.info("api.started", env=settings.app_env, telemetry=telemetry is not None)
        try:
            yield
        finally:
            await broker.stop()
            await engine.dispose()
            logger.info("api.stopped")
            if telemetry is not None:
                telemetry.tracer_provider.force_flush()
                telemetry.logger_provider.force_flush()

    return lifespan


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    password_hasher: PasswordHasher | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    telemetry = configure_telemetry(settings)

    app = FastAPI(
        title="Financial Hub",
        lifespan=_lifespan(settings, telemetry),
        responses={"default": {"model": Problem, "description": "RFC 9457 problem details"}},
    )
    register_exception_handlers(app)
    configure_protection(app, settings)  # idempotency + rate limits, inside the correlation id
    app.add_middleware(CorrelationIdMiddleware)
    # Outermost: also covers the 500 responses CorrelationIdMiddleware renders itself.
    app.add_middleware(SecurityHeadersMiddleware)
    app.state.settings = settings
    app.state.clock = clock or SystemClock()
    configure_identity(app, settings, clock=app.state.clock, hasher=password_hasher)
    app.include_router(auth_router)
    app.include_router(demo_auth_router)
    if settings.demo_login:
        logger.warning("demo_login.enabled", detail="any visitor can open any operator session")
    configure_projects(app, settings)
    # Payments never decrypts: it is handed a loader that the projects module backs (contract 5).
    app.state.payment_providers = build_registry(settings)
    app.state.payment_credentials = projects_credentials_loader(app.state.keyring)
    # Customer provisioning (W4.4): customers decrypts the CPF/CNPJ, payments only passes it on.
    app.state.payment_contacts = customers_contact_loader(app.state.keyring)
    app.state.webhook_policy = webhook_target_policy(settings)
    app.include_router(projects_router)
    app.include_router(api_keys_router)
    app.include_router(members_router)
    app.include_router(provider_accounts_router)
    app.include_router(provider_catalog_router)
    app.include_router(customers_router)
    app.include_router(payments_router)
    app.include_router(provider_webhooks_router)
    app.include_router(metrics_router)
    app.include_router(usage_router)
    app.include_router(stream_router)
    app.include_router(events_router)
    app.include_router(webhooks_router)
    app.include_router(webhook_retry_router)
    app.include_router(ledger_router)
    app.include_router(audit_router)

    @app.get("/health/live", dependencies=[public()])
    async def live() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "live"}

    @app.get("/health/ready", dependencies=[public()])
    async def ready(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        engine: AsyncEngine = request.app.state.engine
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError) as exc:
            logger.warning("health.ready.database_unavailable", error=type(exc).__name__)
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "checks": {"database": "error"}},
            )
        return JSONResponse(content={"status": "ready", "checks": {"database": "ok"}})

    if telemetry is not None:
        instrument_app(app, telemetry)
    # Fail fast: every route must declare public(), authenticated() or require(...).
    verify_route_declarations(app)
    return app


app = create_app()

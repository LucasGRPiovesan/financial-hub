from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.modules.identity.api.routes import configure_identity
from financial_hub.modules.identity.api.routes import router as auth_router
from financial_hub.modules.identity.application.service import PasswordHasher
from financial_hub.modules.projects.api.routes import router as projects_router
from financial_hub.platform.db import create_engine, create_session_factory
from financial_hub.platform.http.auth import public, verify_route_declarations
from financial_hub.platform.http.headers import SecurityHeadersMiddleware
from financial_hub.platform.http.middleware import CorrelationIdMiddleware
from financial_hub.platform.http.problems import Problem, register_exception_handlers
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
        logger.info("api.started", env=settings.app_env, telemetry=telemetry is not None)
        try:
            yield
        finally:
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
    app.add_middleware(CorrelationIdMiddleware)
    # Outermost: also covers the 500 responses CorrelationIdMiddleware renders itself.
    app.add_middleware(SecurityHeadersMiddleware)
    app.state.settings = settings
    app.state.clock = clock or SystemClock()
    configure_identity(app, settings, clock=app.state.clock, hasher=password_hasher)
    app.include_router(auth_router)
    app.include_router(projects_router)

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

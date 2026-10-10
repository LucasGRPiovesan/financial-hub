from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from financial_hub.platform.db import create_engine
from financial_hub.platform.logging import configure_logging
from financial_hub.platform.settings import get_settings

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings)
    app.state.engine = engine
    logger.info("api.started", env=settings.app_env)
    try:
        yield
    finally:
        await engine.dispose()
        logger.info("api.stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="Financial Hub", lifespan=lifespan)

    @app.get("/health/live")
    async def live() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "live"}

    @app.get("/health/ready")
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

    return app


app = create_app()

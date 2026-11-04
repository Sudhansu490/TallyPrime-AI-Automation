"""FastAPI application factory.

Run with: uvicorn tally_automation.api.main:build_app --factory --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tally_automation import __version__
from tally_automation.api.routes import health
from tally_automation.config import Environment, Settings, get_settings
from tally_automation.db.session import create_db_engine, create_session_factory
from tally_automation.logging_config import configure_logging

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_directories()
        engine = create_db_engine(settings.database_url)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        logger.info(
            "api started",
            extra={"environment": settings.app_env.value, "tally_write_enabled": settings.tally_write_enabled},
        )
        try:
            yield
        finally:
            engine.dispose()
            logger.info("api stopped")

    is_production = settings.app_env is Environment.PRODUCTION
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        # Interactive docs are off in production to reduce exposed surface.
        docs_url=None if is_production else "/docs",
        redoc_url=None,
        openapi_url=None if is_production else "/openapi.json",
    )
    app.include_router(health.router, prefix="/api/v1")
    return app


def build_app() -> FastAPI:
    """Entry point for `uvicorn --factory`: configures logging, then builds the app."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)
    return create_app(settings)

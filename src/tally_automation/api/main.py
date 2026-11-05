"""FastAPI application factory.

One process serves the JSON API (``/api/v1/...``), the HTML pages and the
static frontend assets, all from the same origin.

Production: ``python -m tally_automation`` (see ``tally_automation.server``).
Development with auto-reload:
``uvicorn tally_automation.api.main:build_app --factory --host 127.0.0.1 --port 8000 --reload``
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from tally_automation import __version__
from tally_automation.api.middleware import install_origin_check, install_security_headers
from tally_automation.api.routes import documents, health
from tally_automation.config import Environment, Settings, get_settings
from tally_automation.db.session import create_db_engine, create_session_factory
from tally_automation.documents.service import DocumentIngestor
from tally_automation.frontend import routes as frontend_routes
from tally_automation.frontend.routes import STATIC_DIR
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
        app.state.ingestor = DocumentIngestor(settings, app.state.session_factory)
        logger.info(
            "app started",
            extra={"environment": settings.app_env.value, "tally_write_enabled": settings.tally_write_enabled},
        )
        try:
            yield
        finally:
            engine.dispose()
            logger.info("app stopped")

    is_production = settings.app_env is Environment.PRODUCTION
    docs_url = None if is_production else "/docs"
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        # Interactive docs are off in production to reduce exposed surface.
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=None if is_production else "/openapi.json",
    )
    # No CORSMiddleware on purpose: the browser only ever calls this same origin.
    # Middleware added later runs earlier, so the origin check's 403 still gets headers.
    install_origin_check(app)
    install_security_headers(app, docs_path=docs_url)
    # Reject requests whose Host header is not ours (DNS-rebinding protection).
    # Added last so it runs first.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(frontend_routes.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def build_app() -> FastAPI:
    """Entry point for `uvicorn --factory`: configures logging, then builds the app."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)
    return create_app(settings)

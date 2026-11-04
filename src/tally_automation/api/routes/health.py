"""Liveness and readiness reporting."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from tally_automation import __version__
from tally_automation.api.deps import EngineDep, SettingsDep
from tally_automation.db.session import check_database

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class ComponentStatus(BaseModel):
    status: Literal["ok", "error", "not_configured"]
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: ComponentStatus
    tally_write_enabled: bool


@router.get("/health", response_model=HealthResponse)
def health(settings: SettingsDep, engine: EngineDep, response: Response) -> HealthResponse:
    try:
        check_database(engine)
        database = ComponentStatus(status="ok")
    except SQLAlchemyError as exc:
        logger.error("database health check failed", extra={"error_type": type(exc).__name__})
        # Do not leak connection strings or driver messages to clients.
        database = ComponentStatus(status="error", detail="database unavailable")

    overall: Literal["ok", "degraded"] = "ok" if database.status == "ok" else "degraded"
    if overall != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status=overall,
        version=__version__,
        environment=settings.app_env.value,
        database=database,
        tally_write_enabled=settings.tally_write_enabled,
    )

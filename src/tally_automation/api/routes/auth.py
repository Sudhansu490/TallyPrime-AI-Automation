"""Login, logout, the current user, and changing one's own password (Phase 9)."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from tally_automation.api.deps import SettingsDep
from tally_automation.auth.deps import ROLE_LABELS, CurrentUserDep
from tally_automation.auth.middleware import SESSION_COOKIE
from tally_automation.auth.service import (
    AuthError, authenticate, change_own_password, end_session, start_session,
)
from tally_automation.db.models import AppUser, UserRole

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class MeOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: UserRole
    role_label: str
    can_review: bool
    can_approve: bool
    last_login_at: datetime | None


def _me(user: AppUser) -> MeOut:
    return MeOut(id=user.id, username=user.username, display_name=user.display_name, role=user.role,
                 role_label=ROLE_LABELS[user.role], can_review=user.role.at_least(UserRole.REVIEWER),
                 can_approve=user.role.at_least(UserRole.APPROVER), last_login_at=user.last_login_at)


def _error(exc: AuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.message, "code": exc.code})


def _set_cookie(response: JSONResponse, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="strict", path="/",
        # HTTPS is not used inside the tailnet (WireGuard already encrypts the link);
        # the flag is set automatically if the app is ever served over HTTPS.
        secure=request.url.scheme == "https",
    )


@router.post("/login", response_model=MeOut, responses={401: {}, 429: {}})
def login(body: LoginIn, request: Request, settings: SettingsDep) -> JSONResponse:
    factory = request.app.state.session_factory
    client = request.client.host if request.client else None
    session: Session = factory()
    try:
        try:
            user = authenticate(session, settings, body.username, body.password)
        except AuthError as exc:
            session.commit()          # keep the failed-login counter / lockout
            logger.info("login refused", extra={"reason": exc.code, "client": client})
            return _error(exc)
        end_session(session, request.cookies.get(SESSION_COOKIE))   # no session fixation
        token = start_session(session, settings, user, client_address=client,
                              user_agent=request.headers.get("user-agent"))
        session.commit()
        out = _me(user)
    finally:
        session.close()
    logger.info("login", extra={"user_id": out.id, "client": client})
    response = JSONResponse(content=out.model_dump(mode="json"))
    _set_cookie(response, request, token)
    return response


@router.post("/logout", status_code=204)
def logout(request: Request, user: CurrentUserDep) -> Response:
    session: Session = request.app.state.session_factory()
    try:
        end_session(session, request.cookies.get(SESSION_COOKIE))
        session.commit()
    finally:
        session.close()
    logger.info("logout", extra={"user_id": user.id})
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me", response_model=MeOut)
def me(request: Request, user: CurrentUserDep) -> MeOut:
    session: Session = request.app.state.session_factory()
    try:
        record = session.get(AppUser, user.id)
        assert record is not None
        return _me(record)
    finally:
        session.close()


@router.post("/password", response_model=MeOut, responses={401: {}, 422: {}})
def change_password(body: PasswordIn, request: Request, user: CurrentUserDep, settings: SettingsDep) -> JSONResponse:
    """Change one's own password. Every session of the user ends; this browser gets a new one."""
    session: Session = request.app.state.session_factory()
    client = request.client.host if request.client else None
    try:
        try:
            record = change_own_password(session, settings, user.id, body.current_password, body.new_password)
        except AuthError as exc:
            session.rollback()
            return _error(exc)
        token = start_session(session, settings, record, client_address=client,
                              user_agent=request.headers.get("user-agent"))
        session.commit()
        out = _me(record)
    finally:
        session.close()
    logger.info("password changed", extra={"user_id": user.id})
    response = JSONResponse(content=out.model_dump(mode="json"))
    _set_cookie(response, request, token)
    return response

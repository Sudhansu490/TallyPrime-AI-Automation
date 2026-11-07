"""Logging test clients in without the (deliberately slow) password hash. SYNTHETIC accounts only."""

from __future__ import annotations

from functools import lru_cache

from fastapi.testclient import TestClient
from sqlalchemy import select

from tally_automation.auth.middleware import SESSION_COOKIE
from tally_automation.auth.service import start_session
from tally_automation.db.models import AppUser, UserRole, UserSession
from tally_automation.security import hash_password, token_digest

PASSWORD = "synthetic-Pass-2026!"


@lru_cache
def password_hash() -> str:
    return hash_password(PASSWORD)


def ensure_user(client: TestClient, username: str, role: UserRole) -> AppUser:
    factory = client.app.state.session_factory
    with factory.begin() as session:
        user = session.scalar(select(AppUser).where(AppUser.username == username))
        if user is None:
            user = AppUser(username=username, display_name=f"Synthetic {role.value}", role=role,
                           password_hash=password_hash(), is_active=True)
            session.add(user)
            session.flush()
        session.expunge(user)
        return user


def login_as(client: TestClient, role: UserRole = UserRole.ADMIN, username: str | None = None) -> AppUser:
    """Give ``client`` a session for a synthetic user with ``role`` (cookie + CSRF header)."""
    user = ensure_user(client, username or f"synthetic-{role.value}", role)
    factory = client.app.state.session_factory
    settings = client.app.state.settings
    with factory.begin() as session:
        token = start_session(session, settings, session.get(AppUser, user.id), client_address="testclient",
                              user_agent="pytest")
        csrf = session.scalar(select(UserSession.csrf_token).where(UserSession.token_sha256 == token_digest(token)))
    set_token(client, token)
    client.headers["X-CSRF-Token"] = csrf
    return user


def set_token(client: TestClient, token: str) -> None:
    """Replace the session cookie (as the server would set it, for host 127.0.0.1)."""
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token, domain="127.0.0.1", path="/")


def get_token(client: TestClient) -> str | None:
    return next((c.value for c in client.cookies.jar if c.name == SESSION_COOKIE), None)


def logout(client: TestClient) -> None:
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)

"""Users, logins and server-side sessions.

* Passwords are hashed with scrypt (``tally_automation.security``).
* A login creates a session row holding the SHA-256 of a random 256-bit token;
  the token itself lives only in the browser's HttpOnly cookie. Each session
  also has its own random CSRF token.
* Sessions end after ``SESSION_IDLE_MINUTES`` without activity, after
  ``SESSION_MAX_HOURS`` in total, at logout, and when the password changes.
* ``LOGIN_MAX_FAILURES`` consecutive wrong passwords lock the account for
  ``LOGIN_LOCKOUT_MINUTES``. Unknown usernames take as long as known ones.
* Accounts are created and managed from the command line on Computer A
  (``python -m tally_automation users …``); there is no self-registration.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from tally_automation.config import Settings
from tally_automation.db.base import utc_now
from tally_automation.db.models import AppUser, UserRole, UserSession
from tally_automation.security import (
    burn_time, hash_password, needs_rehash, new_token, token_digest, verify_password,
)

logger = logging.getLogger(__name__)
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
LAST_SEEN_RESOLUTION = timedelta(seconds=60)


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    display_name: str
    role: UserRole
    session_id: int
    csrf_token: str

    def can(self, role: UserRole) -> bool:
        return self.role.at_least(role)


# ---- Passwords and accounts ------------------------------------------------------------------

def check_password_policy(password: str, settings: Settings, username: str | None = None) -> None:
    problems = []
    if len(password) < settings.password_min_length:
        problems.append(f"at least {settings.password_min_length} characters")
    if username and username.lower() in password.lower():
        problems.append("must not contain the username")
    if len(set(password)) < 5:
        problems.append("at least 5 different characters")
    if problems:
        raise AuthError("weak_password", "Password rejected: " + "; ".join(problems) + ".", 422)


def normalise_username(username: str) -> str:
    return username.strip().lower()


def create_user(session: Session, settings: Settings, *, username: str, display_name: str, password: str,
                role: UserRole) -> AppUser:
    username = normalise_username(username)
    if not USERNAME.match(username):
        raise AuthError("invalid_username", "Usernames are 2–64 lower-case letters, digits, '.', '_' or '-'.", 422)
    if session.scalar(select(AppUser).where(AppUser.username == username)) is not None:
        raise AuthError("username_taken", f"The username '{username}' already exists.", 409)
    check_password_policy(password, settings, username)
    user = AppUser(username=username, display_name=display_name.strip() or username, role=role,
                   password_hash=hash_password(password), is_active=True, password_changed_at=utc_now())
    session.add(user)
    session.flush()
    logger.info("user created", extra={"user_id": user.id, "role": role.value})
    return user


def set_password(session: Session, settings: Settings, user: AppUser, password: str) -> None:
    """Set a new password and end every session of that user."""
    check_password_policy(password, settings, user.username)
    user.password_hash = hash_password(password)
    user.password_changed_at = utc_now()
    user.failed_logins, user.locked_until = 0, None
    end_user_sessions(session, user.id)


# ---- Login --------------------------------------------------------------------------------------

def authenticate(session: Session, settings: Settings, username: str, password: str,
                 *, now: datetime | None = None) -> AppUser:
    now = now or utc_now()
    user = session.scalar(select(AppUser).where(AppUser.username == normalise_username(username)))
    if user is None:
        burn_time(password)
        raise AuthError("invalid_credentials", "Wrong username or password.", 401)
    if user.locked_until is not None and user.locked_until > now:
        burn_time(password)
        minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
        raise AuthError("locked", f"Too many failed logins. Try again in about {minutes} minute(s), "
                                  "or ask an administrator to reset the password.", 429)
    if not verify_password(password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= settings.login_max_failures:
            user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
            user.failed_logins = 0
            logger.warning("account locked after failed logins", extra={"user_id": user.id})
        raise AuthError("invalid_credentials", "Wrong username or password.", 401)
    if not user.is_active:
        raise AuthError("invalid_credentials", "Wrong username or password.", 401)
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


def start_session(session: Session, settings: Settings, user: AppUser, *, client_address: str | None,
                  user_agent: str | None) -> str:
    """Create a session; returns the token for the cookie (never stored in clear)."""
    token = new_token()
    now = utc_now()
    session.add(UserSession(
        token_sha256=token_digest(token), user_id=user.id, csrf_token=new_token(), client_address=client_address,
        user_agent=(user_agent or "")[:255] or None, created_at=now, last_seen_at=now,
        expires_at=now + timedelta(hours=settings.session_max_hours),
    ))
    session.flush()
    return token


def resolve_session(session: Session, settings: Settings, token: str | None, *,
                    now: datetime | None = None) -> CurrentUser | None:
    """The logged-in user for a cookie token, or None. Expired sessions are ended."""
    if not token or len(token) > 128:
        return None
    now = now or utc_now()
    row = session.execute(
        select(UserSession, AppUser).join(AppUser, AppUser.id == UserSession.user_id)
        .where(UserSession.token_sha256 == token_digest(token), UserSession.ended_at.is_(None))
    ).first()
    if row is None:
        return None
    user_session, user = row
    idle_limit = user_session.last_seen_at + timedelta(minutes=settings.session_idle_minutes)
    if not user.is_active or now >= user_session.expires_at or now >= idle_limit:
        user_session.ended_at = now
        return None
    if now - user_session.last_seen_at >= LAST_SEEN_RESOLUTION:
        user_session.last_seen_at = now
    return CurrentUser(user.id, user.username, user.display_name, user.role, user_session.id, user_session.csrf_token)


def end_session(session: Session, token: str | None) -> None:
    if token:
        session.execute(update(UserSession).where(
            UserSession.token_sha256 == token_digest(token), UserSession.ended_at.is_(None),
        ).values(ended_at=utc_now()))


def end_user_sessions(session: Session, user_id: int) -> int:
    return session.execute(update(UserSession).where(
        UserSession.user_id == user_id, UserSession.ended_at.is_(None),
    ).values(ended_at=utc_now())).rowcount


def change_own_password(session: Session, settings: Settings, user_id: int, current: str, new: str) -> AppUser:
    user = session.get(AppUser, user_id)
    if user is None or not verify_password(current, user.password_hash):
        raise AuthError("invalid_credentials", "The current password is wrong.", 401)
    if current == new:
        raise AuthError("weak_password", "The new password must differ from the current one.", 422)
    set_password(session, settings, user, new)
    return user


def user_names(session: Session, ids) -> dict[int, str]:
    """user id → "Display Name (username)" for audit displays."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    return {u.id: f"{u.display_name} ({u.username})"
            for u in session.scalars(select(AppUser).where(AppUser.id.in_(wanted)))}


def user_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(AppUser).where(AppUser.is_active.is_(True))) or 0

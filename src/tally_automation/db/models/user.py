"""Application users and their server-side login sessions (Phase 9).

Roles are cumulative: viewer < reviewer < approver < admin.

* viewer   – read everything.
* reviewer – upload, correct extracted values, decide duplicates, retrieve
  TallyPrime masters, generate vouchers.
* approver – additionally approve / reject vouchers and import them.
* admin    – everything; user accounts are managed from the command line.

A session row holds only the SHA-256 of the random token kept in the browser's
HttpOnly cookie, plus that session's CSRF token.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from tally_automation.db.base import Base, TimestampMixin, UtcDateTime, utc_now


class UserRole(StrEnum):
    VIEWER = "viewer"        # read-only access
    REVIEWER = "reviewer"    # upload, edit, decide duplicates, generate vouchers
    APPROVER = "approver"    # also approve vouchers and import them into TallyPrime
    ADMIN = "admin"          # everything

    @property
    def rank(self) -> int:
        return list(UserRole).index(self)

    def at_least(self, other: UserRole) -> bool:
        return self.rank >= other.rank


class AppUser(TimestampMixin, Base):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=False, create_constraint=True,
             values_callable=lambda roles: [r.value for r in roles], length=16),
        default=UserRole.VIEWER,
    )
    is_active: Mapped[bool] = mapped_column(default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    # Consecutive failed logins; reaching LOGIN_MAX_FAILURES locks the account until locked_until.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime())
    password_changed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())

    def __repr__(self) -> str:
        return f"AppUser(id={self.id!r}, username={self.username!r}, role={self.role.value!r})"


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="RESTRICT"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    client_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())       # absolute limit
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime())  # logout, expiry or password change

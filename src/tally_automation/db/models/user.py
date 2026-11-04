"""Application users. Roles gate review, approval and Tally import actions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from tally_automation.db.base import Base, TimestampMixin


class UserRole(StrEnum):
    VIEWER = "viewer"        # read-only access
    REVIEWER = "reviewer"    # may edit extracted data
    APPROVER = "approver"    # may approve vouchers for import
    ADMIN = "admin"          # user management and Tally import


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
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"AppUser(id={self.id!r}, username={self.username!r}, role={self.role.value!r})"

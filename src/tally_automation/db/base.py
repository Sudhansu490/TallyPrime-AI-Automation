"""Declarative base, constraint naming convention and shared column mixins."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from typing import Any

from sqlalchemy import DateTime, Dialect, Enum, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

# Deterministic constraint names are required for Alembic batch migrations on SQLite.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes, even on SQLite.

    SQLite stores no offset, so values would load as naive datetimes and reach
    the browser without a timezone (then be misread as local time). This type
    stores UTC and always returns UTC-aware values. Naive input is refused
    because its timezone would be a guess.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime refused; pass a timezone-aware value")
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def str_enum(enum_cls: type[StrEnum], name: str, length: int = 32) -> Enum:
    """Portable enum column: VARCHAR + CHECK constraint, storing the enum *values*."""
    return Enum(
        enum_cls, name=name, native_enum=False, create_constraint=True,
        values_callable=lambda members: [m.value for m in members], length=length,
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.current_timestamp(),
    )

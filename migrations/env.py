"""Alembic migration environment."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from tally_automation.config import get_settings
from tally_automation.db import models  # noqa: F401  (registers all tables)
from tally_automation.db.base import Base, DecimalText, UtcDateTime
from tally_automation.db.session import create_db_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    # UtcDateTime is a Python-side wrapper; the column itself is a plain DateTime.
    if type_ == "type" and isinstance(obj, UtcDateTime):
        return "sa.DateTime(timezone=True)"
    # DecimalText is a Python-side wrapper over a text column.
    if type_ == "type" and isinstance(obj, DecimalText):
        return "sa.String(length=40)"
    return False


def _database_url() -> str:
    # Tests pass an explicit URL; otherwise use application settings.
    return config.attributes.get("database_url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        render_item=_render_item,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    if not config.attributes.get("database_url"):
        # The default SQLite file lives under data/, which may not exist yet.
        get_settings().ensure_directories()
    engine = create_db_engine(_database_url())
    is_sqlite = engine.dialect.name == "sqlite"
    try:
        with engine.connect() as connection:
            if is_sqlite:
                # Batch mode rebuilds a table by copy, drop and rename. With foreign
                # keys enforced, dropping a referenced table fails (or would cascade).
                # The pragma only works outside a transaction, so it is set here and
                # integrity is verified explicitly before the migration commits.
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                connection.commit()
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                # SQLite cannot ALTER most constraints; batch mode recreates tables.
                render_as_batch=True,
                compare_type=True,
                render_item=_render_item,
            )
            with context.begin_transaction():
                context.run_migrations()
                if is_sqlite:
                    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                    if violations:
                        raise RuntimeError(f"migration left foreign key violations: {violations[:5]}")
            if is_sqlite:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""Alembic migration environment."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from tally_automation.config import get_settings
from tally_automation.db import models  # noqa: F401  (registers all tables)
from tally_automation.db.base import Base
from tally_automation.db.session import create_db_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    # Tests pass an explicit URL; otherwise use application settings.
    return config.attributes.get("database_url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    if not config.attributes.get("database_url"):
        # The default SQLite file lives under data/, which may not exist yet.
        get_settings().ensure_directories()
    engine = create_db_engine(_database_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                # SQLite cannot ALTER most constraints; batch mode recreates tables.
                render_as_batch=True,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tally_automation.db.base import Base
from tally_automation.db.models import AppUser, UserRole
from tally_automation.db.session import create_session_factory, transaction
from tally_automation.security import hash_password


def make_user(username: str = "reviewer1", **overrides: object) -> AppUser:
    values = {
        "username": username,
        "display_name": "Synthetic Test User",
        "password_hash": hash_password("synthetic-password"),
    } | overrides
    return AppUser(**values)


def test_migration_creates_expected_tables(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names())
    assert {"app_users", "alembic_version"} <= tables


def test_models_match_migrations(migrated_engine: Engine) -> None:
    """Fails if a model was changed without generating a migration."""
    with migrated_engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_downgrade_and_upgrade_round_trip(alembic_config: Config, migrated_engine: Engine) -> None:
    command.downgrade(alembic_config, "base")
    assert "app_users" not in inspect(migrated_engine).get_table_names()
    command.upgrade(alembic_config, "head")
    assert "app_users" in inspect(migrated_engine).get_table_names()


def test_sqlite_pragmas_enabled(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_user_defaults_and_timestamps(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)
    with transaction(factory) as session:
        session.add(make_user())
    with factory() as session:
        user = session.query(AppUser).one()
        assert user.role is UserRole.VIEWER
        assert user.is_active is True
        assert user.created_at is not None


def test_username_is_unique(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)
    with transaction(factory) as session:
        session.add(make_user("dup"))
    with pytest.raises(IntegrityError):
        with transaction(factory) as session:
            session.add(make_user("dup"))


def test_invalid_role_rejected_by_database(migrated_engine: Engine) -> None:
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO app_users (username, display_name, password_hash, role, is_active) "
                "VALUES ('x', 'x', 'x', 'superuser', 1)"
            ))


def test_transaction_rolls_back_on_error(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)
    with pytest.raises(RuntimeError):
        with transaction(factory) as session:
            session.add(make_user("rolled-back"))
            session.flush()
            raise RuntimeError("synthetic failure")
    with factory() as session:
        assert session.query(AppUser).count() == 0

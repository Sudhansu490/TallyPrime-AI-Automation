import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from datetime import UTC, datetime, timedelta, timezone

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
    assert {"app_users", "document_files", "documents", "processing_jobs", "alembic_version"} <= tables


def test_models_match_migrations(migrated_engine: Engine) -> None:
    """Fails if a model was changed without generating a migration."""
    with migrated_engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_downgrade_and_upgrade_round_trip(alembic_config: Config, migrated_engine: Engine) -> None:
    command.downgrade(alembic_config, "base")
    assert inspect(migrated_engine).get_table_names() == ["alembic_version"]
    command.upgrade(alembic_config, "head")
    assert {"app_users", "documents"} <= set(inspect(migrated_engine).get_table_names())


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


def test_timestamps_round_trip_as_utc_aware(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)
    ist = timezone(timedelta(hours=5, minutes=30))
    with transaction(factory) as session:
        session.add(make_user("tz", last_login_at=datetime(2026, 11, 4, 17, 55, tzinfo=ist)))
    with factory() as session:
        user = session.query(AppUser).one()
        assert user.last_login_at == datetime(2026, 11, 4, 12, 25, tzinfo=UTC)
        assert user.last_login_at.tzinfo is UTC
        assert user.created_at.tzinfo is UTC  # server/Python default also comes back aware


def test_naive_datetime_refused(migrated_engine: Engine) -> None:
    factory = create_session_factory(migrated_engine)
    with pytest.raises(Exception, match="naive datetime"):
        with transaction(factory) as session:
            session.add(make_user("naive", last_login_at=datetime(2026, 11, 4, 12, 0)))

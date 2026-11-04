"""Shared fixtures. Every test gets an isolated temporary database and data dir."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tally_automation.api.main import create_app
from tally_automation.config import PROJECT_ROOT, Environment, Settings
from tally_automation.db.session import create_db_engine


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # _env_file=None keeps a developer's real .env out of the test run.
    return Settings(
        _env_file=None,
        app_env=Environment.TEST,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
    )


@pytest.fixture
def alembic_config(settings: Settings) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture
def migrated_engine(settings: Settings, alembic_config: Config) -> Iterator[Engine]:
    command.upgrade(alembic_config, "head")
    engine = create_db_engine(settings.database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def client(settings: Settings, migrated_engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client

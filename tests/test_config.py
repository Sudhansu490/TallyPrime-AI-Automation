from pathlib import Path

import pytest
from pydantic import ValidationError

from tally_automation.config import PROJECT_ROOT, Settings


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_defaults_are_safe() -> None:
    settings = make()
    assert settings.api_host == "127.0.0.1"
    assert settings.tally_write_enabled is False
    assert settings.tally_url is None
    assert settings.max_upload_size_bytes == 20 * 1024 * 1024


def test_relative_paths_resolve_against_project_root() -> None:
    settings = make(data_dir="data", database_url="sqlite:///data/app.db")
    assert settings.data_dir == (PROJECT_ROOT / "data").resolve()
    db_path = Path(settings.database_url.removeprefix("sqlite:///"))
    assert db_path == (PROJECT_ROOT / "data" / "app.db").resolve()


def test_blank_optional_values_become_none() -> None:
    settings = make(tally_host="  ", llm_api_key="", secret_key="")
    assert settings.tally_host is None
    assert settings.llm_api_key is None
    assert settings.secret_key is None


def test_secrets_are_not_exposed_in_repr() -> None:
    settings = make(llm_api_key="sk-synthetic-test-key", secret_key="synthetic-secret")
    text = repr(settings)
    assert "sk-synthetic-test-key" not in text
    assert "synthetic-secret" not in text
    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == "sk-synthetic-test-key"


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALLY_HOST", "tally-host.example.ts.net")
    monkeypatch.setenv("TALLY_PORT", "9001")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    settings = make()
    assert settings.tally_url == "http://tally-host.example.ts.net:9001"
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    "overrides",
    [{"tally_port": 0}, {"api_port": 70000}, {"log_level": "verbose"}, {"max_upload_size_mb": 0}],
)
def test_invalid_values_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make(**overrides)


def test_ensure_directories_creates_data_tree(tmp_path: Path) -> None:
    settings = make(data_dir=tmp_path / "data")
    settings.ensure_directories()
    assert settings.upload_dir.is_dir()
    assert settings.log_dir.is_dir()

"""Application configuration loaded from environment variables and `.env`."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Tally Automation"
    app_env: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    secret_key: SecretStr | None = None

    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/tally_automation.db"
    max_upload_size_mb: int = Field(default=20, gt=0, le=100)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_base_url: str = "http://127.0.0.1:8000"

    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_timeout_seconds: float = Field(default=60, gt=0)

    tally_host: str | None = None
    tally_port: int = Field(default=9000, ge=1, le=65535)
    tally_timeout_seconds: float = Field(default=15, gt=0)
    tally_company: str | None = None
    tally_write_enabled: bool = False

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {value}")
        return value

    @field_validator(
        "secret_key", "llm_provider", "llm_model", "llm_api_key",
        "llm_base_url", "tally_host", "tally_company",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        # `.env.example` leaves optional values blank; treat them as unset.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("data_dir")
    @classmethod
    def _absolute_data_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator("database_url")
    @classmethod
    def _absolute_sqlite_path(cls, value: str) -> str:
        url = make_url(value)
        if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
            return value
        path = Path(url.database)
        if path.is_absolute():
            return value
        # Built by hand: render_as_string() would percent-encode "E:\" on Windows.
        absolute = (PROJECT_ROOT / path).resolve().as_posix()
        query = "&".join(f"{k}={v}" for k, v in url.query.items())
        return f"{url.drivername}:///{absolute}" + (f"?{query}" if query else "")

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def tally_url(self) -> str | None:
        if not self.tally_host:
            return None
        return f"http://{self.tally_host}:{self.tally_port}"

    def ensure_directories(self) -> None:
        for directory in (self.data_dir, self.upload_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()

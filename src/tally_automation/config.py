"""Application configuration loaded from environment variables and `.env`."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
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

    # Interfaces the single app process listens on. Each must be loopback or a
    # Tailscale address; see `tally_automation.server.validate_bind_host`.
    bind_hosts: Annotated[list[str], NoDecode] = ["127.0.0.1"]
    app_port: int = Field(default=8000, ge=1, le=65535)
    # Host-header names accepted in addition to loopback names and bind_hosts,
    # e.g. Computer A's MagicDNS name. Blocks DNS-rebinding attacks.
    extra_allowed_hosts: Annotated[list[str], NoDecode] = []

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

    @field_validator("bind_hosts", "extra_allowed_hosts", mode="before")
    @classmethod
    def _split_comma_list(cls, value: object) -> object:
        # Environment values are comma-separated: BIND_HOSTS=127.0.0.1,100.x.y.z
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("bind_hosts")
    @classmethod
    def _at_least_one_bind_host(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("bind_hosts must contain at least one address")
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
    def allowed_hosts(self) -> list[str]:
        names = ["127.0.0.1", "localhost", "[::1]", *self.bind_hosts, *self.extra_allowed_hosts]
        # IPv6 literals appear bracketed in the Host header.
        names = [f"[{n}]" if ":" in n and not n.startswith("[") else n for n in names]
        return list(dict.fromkeys(names))

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

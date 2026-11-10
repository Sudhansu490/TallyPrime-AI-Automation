"""Application configuration loaded from environment variables and `.env`."""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

from tally_automation.network import TAILSCALE_IPV4, TAILSCALE_IPV6

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
    # false: log only to data/logs/app.log (rotated), not to the console (supervised runs).
    log_to_console: bool = True
    secret_key: SecretStr | None = None

    data_dir: Path = Path("data")
    database_url: str = "sqlite:///data/tally_automation.db"
    max_upload_size_mb: int = Field(default=20, gt=0, le=100)
    max_pdf_pages: int = Field(default=200, gt=0, le=2000)
    # Guards against decompression bombs (tiny file, enormous bitmap).
    max_image_megapixels: int = Field(default=50, gt=0, le=200)

    # Background processing (Phase 3). One worker thread, one job at a time:
    # OCR is CPU-heavy and accuracy matters more than throughput.
    processing_worker_enabled: bool = True
    processing_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    # A PDF page with fewer letters/digits than this in its embedded text is OCR'd.
    ocr_min_text_chars: int = Field(default=25, ge=1, le=10_000)
    # Resolution used to render PDF pages for OCR.
    ocr_render_dpi: int = Field(default=300, ge=100, le=600)
    # Longest side PaddleOCR's text detector works at. Its default (960) shrinks
    # an A4 page so much that small invoice text is lost.
    ocr_det_max_side: int = Field(default=2000, ge=960, le=4000)
    # OCR lines below this confidence are flagged for human review.
    ocr_low_confidence: float = Field(default=0.85, gt=0, lt=1)

    # Interfaces the single app process listens on. Each must be loopback or a
    # Tailscale address; see `tally_automation.server.validate_bind_host`.
    bind_hosts: Annotated[list[str], NoDecode] = ["127.0.0.1"]
    app_port: int = Field(default=8000, ge=1, le=65535)
    # Host-header names accepted in addition to loopback names and bind_hosts,
    # e.g. Computer A's MagicDNS name. Blocks DNS-rebinding attacks.
    extra_allowed_hosts: Annotated[list[str], NoDecode] = []
    # Client addresses allowed to talk to the app at all (Phase 9). Default:
    # this computer and any device on the tailnet; narrow it to specific
    # Tailscale IPs (e.g. 127.0.0.1/32,100.x.y.z/32) to admit only those devices.
    allowed_client_networks: Annotated[list[str], NoDecode] = [
        "127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48",
    ]

    # Login and sessions (Phase 9). Sessions are server-side; the browser only
    # holds a random token in an HttpOnly, SameSite=Strict cookie.
    session_idle_minutes: int = Field(default=60, ge=5, le=24 * 60)
    session_max_hours: int = Field(default=12, ge=1, le=7 * 24)
    login_max_failures: int = Field(default=5, ge=3, le=20)
    login_lockout_minutes: int = Field(default=15, ge=1, le=24 * 60)
    password_min_length: int = Field(default=12, ge=10, le=128)
    # When true, a voucher cannot be approved by the person who generated it.
    require_separate_approver: bool = False

    # Backups (Phase 10): `python -m tally_automation backup`. Blank = DATA_DIR/backups;
    # better another drive or disk. The newest BACKUP_KEEP database copies are kept.
    backup_dir_setting: Path | None = Field(default=None, alias="BACKUP_DIR")
    backup_keep: int = Field(default=14, ge=1, le=365)

    # AI extraction (Phase 4). Unset provider = extraction disabled: documents
    # stop at text_extracted and nothing leaves Computer A.
    llm_provider: str | None = None
    llm_model: str | None = None          # blank = the provider's default model
    # Used while the main model is over its DAILY limit (each model has its own
    # quota). Blank = no fallback. Groq: openai/gpt-oss-20b (less accurate).
    llm_fallback_model: str | None = None
    llm_api_key: SecretStr | None = None
    # A second Groq account's key: used only after both models are over their daily limit on the first.
    llm_api_key_secondary: SecretStr | None = None
    llm_base_url: str | None = None
    llm_timeout_seconds: float = Field(default=60, gt=0)
    llm_reasoning_effort: str = "medium"  # gpt-oss models: low | medium | high
    llm_max_retries: int = Field(default=3, ge=0, le=10)
    llm_max_output_tokens: int = Field(default=32_000, ge=1_000, le=65_000)
    # Pages sent per LLM call. Long statements are split so output never truncates.
    llm_pages_per_call: int = Field(default=3, ge=1, le=20)
    # Fields the model reports below this confidence are flagged for review.
    llm_low_confidence: float = Field(default=0.8, gt=0, lt=1)

    # TallyPrime (Phase 6: read-only). The host must be loopback or a Tailscale
    # address; see `tally_automation.tally.client.validate_tally_host`.
    tally_host: str | None = None
    tally_port: int = Field(default=9000, ge=1, le=65535)
    # Connection checks and small requests.
    tally_timeout_seconds: float = Field(default=15, gt=0)
    # Master exports (a company can have thousands of ledgers).
    tally_export_timeout_seconds: float = Field(default=120, gt=0)
    tally_max_response_mb: int = Field(default=100, ge=1, le=1000)
    # Company selected by default on the TallyPrime page (must be loaded in Tally).
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
        "secret_key", "llm_provider", "llm_model", "llm_fallback_model", "llm_api_key", "llm_api_key_secondary",
        "llm_base_url", "tally_host", "tally_company",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        # `.env.example` leaves optional values blank; treat them as unset.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("llm_provider")
    @classmethod
    def _known_provider(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if value not in {"groq"}:
            raise ValueError(f"unsupported LLM_PROVIDER: {value!r} (supported: groq)")
        return value

    @field_validator("llm_reasoning_effort")
    @classmethod
    def _valid_effort(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"low", "medium", "high"}:
            raise ValueError("LLM_REASONING_EFFORT must be low, medium or high")
        return value

    @field_validator("bind_hosts", "extra_allowed_hosts", "allowed_client_networks", mode="before")
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

    @field_validator("allowed_client_networks")
    @classmethod
    def _private_networks_only(cls, value: list[str]) -> list[str]:
        for item in value:
            network = ipaddress.ip_network(item, strict=False)
            inside = network.is_loopback or any(
                network.subnet_of(allowed) for allowed in (TAILSCALE_IPV4, TAILSCALE_IPV6) if allowed.version == network.version
            )
            if not inside:
                raise ValueError(f"allowed_client_networks: {item} is neither loopback nor inside the Tailscale ranges")
        if not value:
            raise ValueError("allowed_client_networks must not be empty")
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
    def backup_dir(self) -> Path:
        if self.backup_dir_setting is None:
            return self.data_dir / "backups"
        path = self.backup_dir_setting
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

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

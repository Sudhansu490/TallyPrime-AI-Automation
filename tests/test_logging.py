import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from tally_automation.logging_config import REDACTED, configure_logging


@pytest.fixture
def log_file(tmp_path: Path) -> Iterator[Path]:
    configure_logging("DEBUG", tmp_path)
    yield tmp_path / "app.log"
    # Detach the file handler so the temp dir can be removed on Windows.
    configure_logging("WARNING")


def read_last_entry(path: Path) -> dict:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])


def test_logs_are_json_with_extra_fields(log_file: Path) -> None:
    logging.getLogger("test").info("document received", extra={"document_id": 42})
    entry = read_last_entry(log_file)
    assert entry["message"] == "document received"
    assert entry["level"] == "INFO"
    assert entry["document_id"] == 42


@pytest.mark.parametrize(
    "field", ["password", "api_key", "llm_api_key", "token", "account_number", "authorization"]
)
def test_sensitive_fields_are_redacted(log_file: Path, field: str) -> None:
    logging.getLogger("test").info("event", extra={field: "synthetic-sensitive-value"})
    entry = read_last_entry(log_file)
    assert entry[field] == REDACTED
    assert "synthetic-sensitive-value" not in log_file.read_text(encoding="utf-8")


def test_exceptions_are_serialised(log_file: Path) -> None:
    try:
        raise RuntimeError("synthetic failure")
    except RuntimeError:
        logging.getLogger("test").exception("processing failed")
    entry = read_last_entry(log_file)
    assert "RuntimeError: synthetic failure" in entry["exception"]

"""Structured JSON logging with redaction of sensitive fields."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REDACTED = "***REDACTED***"

# Any `extra=` field whose name matches is masked before it is written.
_SENSITIVE_KEY = re.compile(
    r"pass(word)?|secret|token|api[_-]?key|authorization|cookie|account[_-]?(no|number)|ifsc",
    re.IGNORECASE,
)

# Attributes every LogRecord has; anything else came from `extra=`.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key in _extra_fields(record):
            if _SENSITIVE_KEY.search(key):
                setattr(record, key, REDACTED)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """Configure root logging. Safe to call more than once."""
    formatter = JsonFormatter()
    redactor = RedactingFilter()

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(redactor)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)

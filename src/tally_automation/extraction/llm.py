"""LLM provider layer: one small interface, one adapter per provider.

The adapter only sends text and returns the raw JSON string the model
produced. Parsing and schema validation happen in ``service``, so no provider
can bypass them.

Error handling:

* transient failures (timeouts, connection errors, 5xx) are retried with
  exponential backoff;
* rate limits (429) wait for the provider's ``retry-after`` before retrying,
  because small accounts have low per-minute token limits;
* a DAILY limit (tokens or requests per day) cannot be waited out: with a
  fallback model configured, requests switch to it at once and stay there
  until the provider says the main model is available again; without one (or
  when the fallback is also exhausted) the request fails immediately;
* a reply the provider itself rejects as not matching the schema is reported
  as ``invalid_output`` so the service can retry it once like any malformed
  reply;
* error messages never include the provider's echo of the generated output,
  which would copy document content into logs and the database.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from tally_automation.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_MODELS = {
    # Largest Groq model with strict JSON-schema output.
    "groq": "openai/gpt-oss-120b",
}
MAX_RATE_LIMIT_WAIT_SECONDS = 90.0
# How long the main model is skipped after a daily-limit error that names no time.
DEFAULT_DAILY_BLOCK_SECONDS = 3600.0
_DAILY_LIMIT = re.compile(r"per day|\((?:TPD|RPD)\)", re.IGNORECASE)
_TRY_AGAIN = re.compile(r"try again in\s+((?:\d+(?:\.\d+)?(?:ms|[hms])\s*)+)", re.IGNORECASE)

ErrorKind = Literal["transient", "invalid_output", "fatal"]


class LlmError(Exception):
    def __init__(self, message: str, *, kind: ErrorKind = "fatal") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind

    @property
    def transient(self) -> bool:
        return self.kind == "transient"


@dataclass(frozen=True)
class LlmResponse:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str | None


class LlmClient(Protocol):
    provider: str
    model: str

    def complete_json(
        self, system: str, user: str, schema_name: str, schema: dict[str, Any],
    ) -> LlmResponse: ...


def _error_detail(exc: Any, limit: int = 300) -> tuple[str | None, str]:
    """(code, message) from a provider error body, without 'failed_generation'."""
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    message = str(error.get("message") or type(exc).__name__)
    # Groq appends the generated JSON to some messages; keep only the explanation.
    return error.get("code"), message[:limit]


def _retry_after(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    value = headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _is_daily_limit(message: str) -> bool:
    return bool(_DAILY_LIMIT.search(message))


def _try_again_seconds(message: str) -> float | None:
    """Seconds from Groq's 'Please try again in 1h2m3.5s', if present."""
    match = _TRY_AGAIN.search(message)
    if not match:
        return None
    units = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    return sum(float(n) * units[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|[hms])", match.group(1)))


class GroqClient:
    provider = "groq"

    def __init__(
        self, *, api_key: str, model: str, timeout: float, reasoning_effort: str,
        max_output_tokens: int, max_retries: int, base_url: str | None = None,
        fallback_model: str | None = None, sleep: Any = time.sleep, clock: Any = time.monotonic,
    ) -> None:
        from groq import Groq

        self.model = model
        self.fallback_model = fallback_model if fallback_model and fallback_model != model else None
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        # The main model is skipped until this clock value after a daily-limit error.
        self._main_blocked_until: float | None = None
        # SDK retries are disabled so this class owns the retry policy and its logs.
        self._client = Groq(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)

    def complete_json(
        self, system: str, user: str, schema_name: str, schema: dict[str, Any],
    ) -> LlmResponse:
        import groq

        attempt = 0
        while True:
            attempt += 1
            wait: float | None = None
            model = self._current_model()
            try:
                response = self._client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    response_format={"type": "json_schema", "json_schema": {
                        "name": schema_name, "strict": True, "schema": schema,
                    }},
                    reasoning_effort=self.reasoning_effort,
                    max_completion_tokens=self.max_output_tokens,
                    temperature=0,
                )
            except groq.RateLimitError as exc:
                _, message = _error_detail(exc)
                full_message = _error_detail(exc, limit=2000)[1]
                if _is_daily_limit(full_message):
                    if model == self.model and self.fallback_model:
                        # Waiting cannot help: switch now. Not counted as a retry.
                        block = _try_again_seconds(full_message) or _retry_after(exc) or DEFAULT_DAILY_BLOCK_SECONDS
                        self._main_blocked_until = self._clock() + block
                        logger.warning("daily LLM limit reached; using the fallback model",
                                       extra={"model": model, "fallback_model": self.fallback_model,
                                              "blocked_seconds": round(block)})
                        attempt -= 1
                        continue
                    raise LlmError(
                        f"Groq daily limit reached for {model}; try again later"
                        + ("" if self.fallback_model else " or set LLM_FALLBACK_MODEL")
                        + f". {message}",
                        kind="transient",
                    ) from None
                error = LlmError(f"Groq rate limit: {message}", kind="transient")
                wait = _retry_after(exc)
            except (groq.APITimeoutError, groq.APIConnectionError, groq.InternalServerError) as exc:
                error = LlmError(f"{type(exc).__name__}: {_error_detail(exc)[1]}", kind="transient")
            except groq.APIStatusError as exc:
                code, message = _error_detail(exc)
                if code == "json_validate_failed":
                    raise LlmError(f"Groq rejected the reply as not matching the schema: {message}",
                                   kind="invalid_output") from None
                # Bad request, authentication, request too large: not retryable.
                raise LlmError(f"Groq rejected the request (HTTP {exc.status_code}): {message}") from None
            else:
                choice = response.choices[0]
                usage = response.usage
                return LlmResponse(
                    content=choice.message.content or "",
                    model=response.model,
                    prompt_tokens=usage.prompt_tokens if usage else 0,
                    completion_tokens=usage.completion_tokens if usage else 0,
                    finish_reason=choice.finish_reason,
                )
            if attempt > self.max_retries:
                raise LlmError(f"Groq unavailable after {attempt} attempts: {error.message}", kind="transient")
            delay = min(max(wait or 0.0, float(2 ** attempt)), MAX_RATE_LIMIT_WAIT_SECONDS)
            logger.warning("LLM call failed; retrying",
                           extra={"attempt": attempt, "delay_seconds": delay, "error": error.message})
            self._sleep(delay)

    def _current_model(self) -> str:
        """The main model, or the fallback while the main one is over its daily limit."""
        if self._main_blocked_until is not None:
            if self.fallback_model and self._clock() < self._main_blocked_until:
                return self.fallback_model
            self._main_blocked_until = None
            logger.info("trying the main LLM model again", extra={"model": self.model})
        return self.model


def create_llm_client(settings: Settings) -> LlmClient | None:
    """The configured client, or None when extraction is not configured."""
    if settings.llm_provider is None:
        return None
    if settings.llm_api_key is None:
        raise ValueError("LLM_PROVIDER is set but LLM_API_KEY is empty")
    model = settings.llm_model or DEFAULT_MODELS[settings.llm_provider]
    if settings.llm_provider == "groq":
        return GroqClient(
            api_key=settings.llm_api_key.get_secret_value(), model=model,
            timeout=settings.llm_timeout_seconds, reasoning_effort=settings.llm_reasoning_effort,
            max_output_tokens=settings.llm_max_output_tokens, max_retries=settings.llm_max_retries,
            base_url=settings.llm_base_url, fallback_model=settings.llm_fallback_model,
        )
    raise ValueError(f"unsupported LLM provider {settings.llm_provider}")  # pragma: no cover

"""LLM provider layer: one small interface, one adapter per provider.

The adapter only sends text and returns the raw JSON string the model
produced. Parsing and schema validation happen in ``service``, so no provider
can bypass them.

Error handling:

* transient failures (timeouts, connection errors, 5xx) are retried with
  exponential backoff;
* rate limits (429) wait for the provider's ``retry-after`` before retrying,
  because small accounts have low per-minute token limits;
* a reply the provider itself rejects as not matching the schema is reported
  as ``invalid_output`` so the service can retry it once like any malformed
  reply;
* error messages never include the provider's echo of the generated output,
  which would copy document content into logs and the database.
"""

from __future__ import annotations

import logging
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


def _error_detail(exc: Any) -> tuple[str | None, str]:
    """(code, message) from a provider error body, without 'failed_generation'."""
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    message = str(error.get("message") or type(exc).__name__)
    # Groq appends the generated JSON to some messages; keep only the explanation.
    return error.get("code"), message[:300]


def _retry_after(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    value = headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class GroqClient:
    provider = "groq"

    def __init__(
        self, *, api_key: str, model: str, timeout: float, reasoning_effort: str,
        max_output_tokens: int, max_retries: int, base_url: str | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        from groq import Groq

        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.max_retries = max_retries
        self._sleep = sleep
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
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
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
            base_url=settings.llm_base_url,
        )
    raise ValueError(f"unsupported LLM provider {settings.llm_provider}")  # pragma: no cover

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

import json
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
# Per-minute limits (Groq's free plan: a few thousand tokens per minute): wait for the window to pass
# instead of failing. A long statement needs several such waits, so they are not counted as retries.
PER_MINUTE_WINDOW_SECONDS = 61.0
MAX_PER_MINUTE_WAITS = 10
# Groq counts part of the answer allowance (max_completion_tokens) against the per-minute limit. Once a
# request is refused as too large, the allowance is fitted to the limit Groq names: limit minus the
# request's estimated size, never below MIN_OUTPUT_TOKENS.
MIN_OUTPUT_TOKENS = 1500
TOKEN_MARGIN = 300
CHARS_PER_TOKEN = 2          # measured on statement text (digits, codes): about 2 characters per token
# How long the main model is skipped after a daily-limit error that names no time.
DEFAULT_DAILY_BLOCK_SECONDS = 3600.0
_DAILY_LIMIT = re.compile(r"per day|\((?:TPD|RPD)\)", re.IGNORECASE)
_CUT_OFF = re.compile(r"max completion tokens|max_completion_tokens|truncated", re.IGNORECASE)
_LIMIT = re.compile(r"Limit (\d+)")
_PER_MINUTE_LIMIT = re.compile(r"per minute|\((?:TPM|RPM)\)", re.IGNORECASE)
_TRY_AGAIN = re.compile(r"try again in\s+((?:\d+(?:\.\d+)?(?:ms|[hms])\s*)+)", re.IGNORECASE)

# "too_large": the request is bigger than the provider allows per minute; send less text per request.
# "cut_off": the reply ran out of answer allowance before it was complete; send less text per request.
ErrorKind = Literal["transient", "invalid_output", "too_large", "cut_off", "fatal"]


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
        secondary_api_key: str | None = None,
    ) -> None:
        from groq import Groq

        self.model = model
        self.fallback_model = fallback_model if fallback_model and fallback_model != model else None
        self.reasoning_effort = reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        # Per-minute token limit learned from a "request too large" refusal, per (key, model).
        self._minute_limit: dict[tuple[int, str], int] = {}
        # Per key: the main model is skipped until this clock value after a daily-limit error.
        self._main_blocked: dict[int, float] = {}
        # Per key: the whole key (both models) is skipped until this clock value.
        self._key_blocked: dict[int, float] = {}
        # SDK retries are disabled so this class owns the retry policy and its logs.
        # Key 0 is LLM_API_KEY; key 1, if set, LLM_API_KEY_SECONDARY (another Groq account).
        keys = [api_key] + ([secondary_api_key] if secondary_api_key and secondary_api_key != api_key else [])
        self._clients = [Groq(api_key=k, base_url=base_url, timeout=timeout, max_retries=0) for k in keys]
        self._key = 0

    @property
    def _client(self) -> Any:
        return self._clients[self._key]

    @_client.setter
    def _client(self, value: Any) -> None:
        self._clients[self._key] = value

    def complete_json(
        self, system: str, user: str, schema_name: str, schema: dict[str, Any],
    ) -> LlmResponse:
        import groq

        attempt = 0
        minute_waits = 0
        waited_for_size = False
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
                    max_completion_tokens=self._output_budget(model, system, user, schema),
                    temperature=0,
                )
            except groq.RateLimitError as exc:
                _, message = _error_detail(exc)
                full_message = _error_detail(exc, limit=2000)[1]
                if _is_daily_limit(full_message):
                    block = _try_again_seconds(full_message) or _retry_after(exc) or DEFAULT_DAILY_BLOCK_SECONDS
                    if model == self.model and self.fallback_model:
                        # Waiting cannot help: switch now. Not counted as a retry.
                        self._main_blocked[self._key] = self._clock() + block
                        logger.warning("daily LLM limit reached; using the fallback model",
                                       extra={"model": model, "fallback_model": self.fallback_model,
                                              "blocked_seconds": round(block), "api_key": self._key_label()})
                        attempt -= 1
                        continue
                    other = self._other_key()
                    if other is not None:
                        # Both models of this key are over their daily limit: use the other Groq account's key
                        # until one of them is free again.
                        free_at = min(self._main_blocked.get(self._key, float("inf")), self._clock() + block)
                        self._key_blocked[self._key] = free_at
                        logger.warning("daily LLM limits reached on this API key; using the other key",
                                       extra={"api_key": self._key_label(), "next_api_key": self._key_label(other),
                                              "blocked_seconds": round(free_at - self._clock())})
                        self._key = other
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
                if _PER_MINUTE_LIMIT.search(full_message) and minute_waits < MAX_PER_MINUTE_WAITS:
                    # The per-minute allowance is used up: wait for it, without using up a retry.
                    minute_waits += 1
                    attempt -= 1
                    delay = min(max(wait or _try_again_seconds(full_message) or 0.0, 1.0), PER_MINUTE_WINDOW_SECONDS)
                    logger.warning("LLM per-minute limit reached; waiting", extra={
                        "model": model, "delay_seconds": round(delay, 1), "wait": minute_waits})
                    self._sleep(delay)
                    continue
            except (groq.APITimeoutError, groq.APIConnectionError, groq.InternalServerError) as exc:
                error = LlmError(f"{type(exc).__name__}: {_error_detail(exc)[1]}", kind="transient")
            except groq.APIStatusError as exc:
                code, message = _error_detail(exc)
                if code == "json_validate_failed" and _CUT_OFF.search(_error_detail(exc, limit=2000)[1]):
                    # Groq reports a reply cut short by max_completion_tokens as a schema failure.
                    raise LlmError(f"Groq cut the reply off at the answer allowance: {message}", kind="cut_off") from None
                if code == "json_validate_failed":
                    raise LlmError(f"Groq rejected the reply as not matching the schema: {message}",
                                   kind="invalid_output") from None
                full = _error_detail(exc, limit=2000)[1]
                if exc.status_code == 413 and _PER_MINUTE_LIMIT.search(full):
                    limit = _LIMIT.search(full)
                    if limit and self._minute_limit.get((self._key, model)) != int(limit.group(1)):
                        # First refusal: fit the answer allowance to the limit and send again at once.
                        self._minute_limit[(self._key, model)] = int(limit.group(1))
                        attempt -= 1
                        logger.warning("LLM request too large; fitting the answer allowance to the per-minute limit",
                                       extra={"model": model, "limit": self._minute_limit[(self._key, model)],
                                              "max_output_tokens": self._output_budget(model, system, user, schema)})
                        continue
                    if minute_waits < MAX_PER_MINUTE_WAITS and not waited_for_size:
                        # Often the tokens just used still count: after a full minute it may fit.
                        waited_for_size = True
                        minute_waits += 1
                        attempt -= 1
                        logger.warning("LLM request too large for the per-minute limit; waiting a minute",
                                       extra={"model": model, "delay_seconds": PER_MINUTE_WINDOW_SECONDS})
                        self._sleep(PER_MINUTE_WINDOW_SECONDS)
                        continue
                    raise LlmError(f"Groq rejected the request as too large for its per-minute limit: {message}",
                                   kind="too_large") from None
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

    def _output_budget(self, model: str, system: str, user: str, schema: dict[str, Any]) -> int:
        """The answer allowance: the configured one, or (once Groq has named its per-minute limit) what
        fits under that limit next to this request."""
        limit = self._minute_limit.get((self._key, model))
        if limit is None:
            return self.max_output_tokens
        prompt = (len(system) + len(user) + len(json.dumps(schema))) // CHARS_PER_TOKEN
        return min(self.max_output_tokens, max(MIN_OUTPUT_TOKENS, limit - prompt - TOKEN_MARGIN))

    def _key_label(self, key: int | None = None) -> str:
        return "primary" if (self._key if key is None else key) == 0 else "secondary"

    def _other_key(self) -> int | None:
        """Another API key that is not itself over its daily limits, if any."""
        now = self._clock()
        for key in range(len(self._clients)):
            if key != self._key and now >= self._key_blocked.get(key, 0.0):
                return key
        return None

    def _current_model(self) -> str:
        """The main model, or the fallback while the main one is over its daily limit; on the primary key
        whenever it has a model free again."""
        now = self._clock()
        if self._key != 0 and now >= self._key_blocked.get(0, 0.0):
            self._key = 0
            logger.info("using the primary LLM API key again")
        until = self._main_blocked.get(self._key)
        if until is not None:
            if self.fallback_model and now < until:
                return self.fallback_model
            del self._main_blocked[self._key]
            logger.info("trying the main LLM model again", extra={"model": self.model, "api_key": self._key_label()})
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
            secondary_api_key=(settings.llm_api_key_secondary.get_secret_value()
                               if settings.llm_api_key_secondary is not None else None),
        )
    raise ValueError(f"unsupported LLM provider {settings.llm_provider}")  # pragma: no cover

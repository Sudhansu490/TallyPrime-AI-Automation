"""Groq adapter error handling, with the HTTP layer faked. No network access."""

from types import SimpleNamespace

import groq
import httpx
import pytest

from tally_automation.config import Settings
from tally_automation.extraction.llm import GroqClient, LlmError, create_llm_client

REQUEST = httpx.Request("POST", "https://api.groq.test/openai/v1/chat/completions")
DOCUMENT_ECHO = "SYNTHETIC SECRET DOCUMENT CONTENT"


def status_error(cls, status: int, body: dict, headers: dict | None = None):
    response = httpx.Response(status, headers=headers or {}, request=REQUEST)
    return cls(f"Error code: {status} - {body}", response=response, body=body)


def ok_response(content: str = "{}"):
    return SimpleNamespace(
        model="openai/gpt-oss-120b",
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def client_with(outcomes, max_retries: int = 3):
    sleeps: list[float] = []
    client = GroqClient(api_key="synthetic-test-key", model="m", timeout=5, reasoning_effort="low",
                        max_output_tokens=1000, max_retries=max_retries, sleep=sleeps.append)
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client, calls, sleeps


def test_success_sends_strict_schema() -> None:
    client, calls, _ = client_with([ok_response('{"a": 1}')])
    response = client.complete_json("sys", "user", "name", {"type": "object"})
    assert response.content == '{"a": 1}' and response.prompt_tokens == 10
    request = calls[0]
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["temperature"] == 0 and request["reasoning_effort"] == "low"


def test_rate_limit_waits_for_retry_after() -> None:
    limited = status_error(groq.RateLimitError, 429, {"error": {"message": "rate limit", "code": "rate_limit_exceeded"}},
                           {"retry-after": "17"})
    client, calls, sleeps = client_with([limited, ok_response()])
    client.complete_json("s", "u", "n", {})
    assert sleeps == [17.0] and len(calls) == 2


def test_rate_limit_wait_is_capped_and_retries_exhaust() -> None:
    limited = status_error(groq.RateLimitError, 429, {"error": {"message": "rate limit"}}, {"retry-after": "600"})
    client, calls, sleeps = client_with([limited, limited, limited], max_retries=2)
    with pytest.raises(LlmError) as excinfo:
        client.complete_json("s", "u", "n", {})
    assert excinfo.value.kind == "transient" and "after 3 attempts" in excinfo.value.message
    assert sleeps == [90.0, 90.0]


def test_schema_rejection_is_invalid_output_without_document_echo() -> None:
    body = {"error": {"code": "json_validate_failed", "message": "Generated JSON does not match the expected schema.",
                      "failed_generation": DOCUMENT_ECHO}}
    client, calls, sleeps = client_with([status_error(groq.BadRequestError, 400, body)])
    with pytest.raises(LlmError) as excinfo:
        client.complete_json("s", "u", "n", {})
    assert excinfo.value.kind == "invalid_output"
    assert DOCUMENT_ECHO not in excinfo.value.message and sleeps == [] and len(calls) == 1


def test_other_client_errors_are_fatal_and_not_retried() -> None:
    body = {"error": {"message": "Invalid API Key", "code": "invalid_api_key"}}
    client, calls, _ = client_with([status_error(groq.AuthenticationError, 401, body)])
    with pytest.raises(LlmError) as excinfo:
        client.complete_json("s", "u", "n", {})
    assert excinfo.value.kind == "fatal" and "HTTP 401" in excinfo.value.message and len(calls) == 1


def test_server_errors_retry_with_backoff() -> None:
    server = status_error(groq.InternalServerError, 503, {"error": {"message": "overloaded"}})
    client, _, sleeps = client_with([server, server, ok_response()])
    client.complete_json("s", "u", "n", {})
    assert sleeps == [2.0, 4.0]


def test_create_llm_client_configuration() -> None:
    assert create_llm_client(Settings(_env_file=None)) is None
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        create_llm_client(Settings(_env_file=None, llm_provider="groq"))
    client = create_llm_client(Settings(_env_file=None, llm_provider="GROQ", llm_api_key="synthetic-test-key"))
    assert (client.provider, client.model) == ("groq", "openai/gpt-oss-120b")
    with pytest.raises(ValueError, match="unsupported LLM_PROVIDER"):
        Settings(_env_file=None, llm_provider="someother")


DAILY = (
    "Rate limit reached for model `{model}` in organization `org_synthetic` service tier `on_demand` "
    "on tokens per day (TPD): Limit 200000, Used 199000, Requested 8871. Please try again in 1h2m3.5s. "
    "Need more tokens? Upgrade to Dev Tier today."
)


def daily_limit(model: str = "main-model"):
    return status_error(groq.RateLimitError, 429,
                        {"error": {"message": DAILY.format(model=model), "code": "rate_limit_exceeded"}},
                        {"retry-after": "3723"})


def fallback_client(outcomes):
    now = [1000.0]
    client, calls, sleeps = client_with(outcomes)
    client.model, client.fallback_model = "main-model", "fallback-model"
    client._clock = lambda: now[0]
    return client, calls, sleeps, now


def test_daily_limit_switches_to_fallback_at_once_and_stays_there() -> None:
    client, calls, sleeps, now = fallback_client([daily_limit(), ok_response(), ok_response(), ok_response()])
    client.complete_json("s", "u", "n", {})
    assert [c["model"] for c in calls] == ["main-model", "fallback-model"] and sleeps == []
    now[0] += 3600  # still inside the 1h2m3.5s the provider asked for
    client.complete_json("s", "u", "n", {})
    assert calls[-1]["model"] == "fallback-model"
    now[0] += 200  # past it: the main model is tried again
    client.complete_json("s", "u", "n", {})
    assert calls[-1]["model"] == "main-model"


def test_daily_limit_on_fallback_fails_without_waiting() -> None:
    client, calls, sleeps, _ = fallback_client([daily_limit(), daily_limit("fallback-model")])
    with pytest.raises(LlmError) as excinfo:
        client.complete_json("s", "u", "n", {})
    assert "daily limit reached for fallback-model" in excinfo.value.message
    assert len(calls) == 2 and sleeps == []


def test_daily_limit_without_fallback_fails_without_waiting() -> None:
    client, calls, sleeps = client_with([daily_limit()])
    with pytest.raises(LlmError) as excinfo:
        client.complete_json("s", "u", "n", {})
    assert "set LLM_FALLBACK_MODEL" in excinfo.value.message and len(calls) == 1 and sleeps == []


def test_per_minute_limit_still_waits_on_the_main_model() -> None:
    minute = status_error(groq.RateLimitError, 429, {"error": {
        "message": "Rate limit reached on tokens per minute (TPM): Limit 8000, Requested 8871. Please try again in 6.6s."
    }}, {"retry-after": "7"})
    client, calls, sleeps, _ = fallback_client([minute, ok_response()])
    client.complete_json("s", "u", "n", {})
    assert [c["model"] for c in calls] == ["main-model", "main-model"] and sleeps == [7.0]


def test_fallback_model_is_configured_from_settings() -> None:
    settings = Settings(_env_file=None, llm_provider="groq", llm_api_key="synthetic-test-key",
                        llm_fallback_model="openai/gpt-oss-20b")
    assert create_llm_client(settings).fallback_model == "openai/gpt-oss-20b"
    same = Settings(_env_file=None, llm_provider="groq", llm_api_key="synthetic-test-key",
                    llm_fallback_model="openai/gpt-oss-120b")
    assert create_llm_client(same).fallback_model is None
    blank = Settings(_env_file=None, llm_provider="groq", llm_api_key="synthetic-test-key", llm_fallback_model=" ")
    assert create_llm_client(blank).fallback_model is None


TPM_MESSAGE = ("Rate limit reached for model `m` in organization `org_synthetic` service tier `on_demand` on tokens "
               "per minute (TPM): Limit 8000, Used 7000, Requested 4000. Please try again in 22.5s.")
TOO_LARGE_MESSAGE = ("Request too large for model `m` in organization `org_synthetic` service tier `on_demand` on "
                     "tokens per minute (TPM): Limit 8000, Requested 15557, please reduce your message size and try again.")


def test_per_minute_waits_do_not_use_up_retries() -> None:
    minute = status_error(groq.RateLimitError, 429, {"error": {"message": TPM_MESSAGE}}, {"retry-after": "23"})
    # More per-minute waits than retries: a long statement needs several, and they all succeed in the end.
    client, calls, sleeps = client_with([minute, minute, minute, ok_response()], max_retries=1)
    client.complete_json("s", "u", "n", {})
    assert len(calls) == 4 and sleeps == [23.0, 23.0, 23.0]


def test_too_large_request_waits_a_minute_once_then_is_too_large() -> None:
    too_large = status_error(groq.APIStatusError, 413, {"error": {"message": TOO_LARGE_MESSAGE}})
    client, calls, sleeps = client_with([too_large, ok_response()])
    client.complete_json("s", "u", "n", {})                     # fits after the minute's usage has passed
    assert len(calls) == 2 and sleeps == [61.0]

    client, calls, sleeps = client_with([too_large, too_large])
    with pytest.raises(LlmError) as excinfo:
        client.complete_json("s", "u", "n", {})
    assert excinfo.value.kind == "too_large" and "per-minute limit" in excinfo.value.message
    assert len(calls) == 2 and sleeps == [61.0]


def test_other_413_errors_stay_fatal() -> None:
    too_big = status_error(groq.APIStatusError, 413, {"error": {"message": "Request Entity Too Large"}})
    client, calls, sleeps = client_with([too_big])
    with pytest.raises(LlmError) as excinfo:
        client.complete_json("s", "u", "n", {})
    assert excinfo.value.kind == "fatal" and sleeps == [] and len(calls) == 1

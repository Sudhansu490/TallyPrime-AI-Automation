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

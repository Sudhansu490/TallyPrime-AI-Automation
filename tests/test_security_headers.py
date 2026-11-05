"""Security headers, Host-header allow-list and absence of CORS."""

import pytest
from fastapi.testclient import TestClient

from tally_automation.api.main import create_app
from tally_automation.api.middleware import CONTENT_SECURITY_POLICY
from tally_automation.config import Settings


@pytest.mark.parametrize("path", ["/", "/api/v1/health", "/static/css/app.css", "/api/v1/missing"])
def test_security_headers_on_every_response(client: TestClient, path: str) -> None:
    headers = client.get(path).headers
    assert headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"


def test_csp_forbids_inline_and_external_scripts() -> None:
    directives = dict(d.split(" ", 1) for d in CONTENT_SECURITY_POLICY.split("; "))
    assert directives["script-src"] == "'self'"
    assert directives["style-src"] == "'self'"
    assert directives["object-src"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert "unsafe" not in CONTENT_SECURITY_POLICY
    assert "http" not in CONTENT_SECURITY_POLICY


def test_pages_and_api_are_not_cached(client: TestClient) -> None:
    assert client.get("/").headers["Cache-Control"] == "no-store"
    assert client.get("/api/v1/health").headers["Cache-Control"] == "no-store"
    # Not versioned: always revalidated, so an upgrade never leaves stale JavaScript.
    assert client.get("/static/js/api.js").headers["Cache-Control"] == "no-cache"


def test_relaxed_csp_only_on_docs(client: TestClient) -> None:
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert docs.headers["Content-Security-Policy"] != CONTENT_SECURITY_POLICY
    assert client.get("/openapi.json").headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


def test_no_cors_headers_for_foreign_origin(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers
    preflight = client.options(
        "/api/v1/health",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.parametrize("host", ["evil.example", "192.168.1.51", "attacker.ts.net"])
def test_unknown_host_header_is_rejected(client: TestClient, host: str) -> None:
    response = client.get("/api/v1/health", headers={"Host": host})
    assert response.status_code == 400


def test_configured_tailscale_host_is_accepted(settings: Settings, migrated_engine) -> None:
    tailnet = settings.model_copy(update={
        "bind_hosts": ["127.0.0.1", "100.95.0.1"],
        "extra_allowed_hosts": ["computer-a.example.ts.net"],
    })
    with TestClient(create_app(tailnet), base_url="http://127.0.0.1") as client:
        for host in ("100.95.0.1:8000", "computer-a.example.ts.net", "localhost:8000"):
            assert client.get("/api/v1/health", headers={"Host": host}).status_code == 200

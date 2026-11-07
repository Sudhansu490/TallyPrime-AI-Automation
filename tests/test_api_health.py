from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from auth_helpers import login_as
from tally_automation import __version__
from tally_automation.api.main import create_app
from tally_automation.config import Environment, Settings


def test_health_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["environment"] == "test"
    assert body["database"] == {"status": "ok", "detail": None}
    assert body["tally_configured"] is False
    assert body["tally_write_enabled"] is False


def test_health_reports_database_failure_without_leaking_details(
    client: TestClient, monkeypatch
) -> None:
    def broken(_engine: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("synthetic: /secret/path.db locked"))

    monkeypatch.setattr("tally_automation.api.routes.health.check_database", broken)
    response = client.get("/api/v1/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"]["status"] == "error"
    assert "secret" not in response.text


def test_docs_disabled_in_production(settings: Settings, migrated_engine) -> None:
    production = settings.model_copy(update={"app_env": Environment.PRODUCTION})
    # A real client address: production does not admit the test client's placeholder host.
    with TestClient(create_app(production), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        login_as(client)
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/api/v1/health").status_code == 200


def test_unknown_route_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/does-not-exist").status_code == 404

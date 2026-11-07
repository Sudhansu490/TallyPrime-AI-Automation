"""Login, sessions, CSRF, roles and the client allow-list (Phase 9). SYNTHETIC accounts only."""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from auth_helpers import PASSWORD, ensure_user, get_token, login_as, logout, set_token
from tally_automation.api.main import create_app
from tally_automation.auth.middleware import SESSION_COOKIE
from tally_automation.cli import main as cli
from tally_automation.config import Settings
from tally_automation.db.base import utc_now
from tally_automation.db.models import AppUser, UserRole, UserSession
from tally_automation.frontend.routes import safe_next
from tally_automation.security import token_digest


@pytest.fixture
def anon(client: TestClient) -> TestClient:
    logout(client)
    return client


def login(client: TestClient, username: str, password: str = PASSWORD):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


# ---- Gate ---------------------------------------------------------------------------------------

def test_everything_needs_a_login_except_the_login_itself(anon: TestClient) -> None:
    for path in ("/api/v1/health", "/api/v1/documents", "/api/v1/vouchers", "/api/v1/tally/companies"):
        response = anon.get(path)
        assert response.status_code == 401 and response.json()["code"] == "not_authenticated"
    page = anon.get("/review?document=3", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login?next=%2Freview%3Fdocument%3D3"
    assert anon.get("/login").status_code == 200
    assert anon.get("/static/css/app.css").status_code == 200
    assert anon.get("/api/v1/health/live").json() == {"status": "ok"}
    upload = anon.post("/api/v1/documents", files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")})
    assert upload.status_code == 401


def test_login_sets_a_strict_httponly_cookie_and_stores_only_a_hash(anon: TestClient) -> None:
    ensure_user(anon, "synthetic-reviewer", UserRole.REVIEWER)
    response = login(anon, "Synthetic-Reviewer ")          # usernames are case-insensitive, trimmed
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "reviewer" and response.json()["can_approve"] is False
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
    token = get_token(anon)
    with anon.app.state.session_factory() as session:
        row = session.scalar(select(UserSession).where(UserSession.token_sha256 == token_digest(token)))
        assert row is not None and token not in (row.token_sha256, row.csrf_token)
    me = anon.get("/api/v1/auth/me").json()
    assert me["username"] == "synthetic-reviewer"
    page = anon.get("/").text
    assert f'<meta name="csrf-token" content="{row.csrf_token}">' in page and "Log out" in page


def test_wrong_passwords_are_generic_and_lock_the_account(anon: TestClient) -> None:
    ensure_user(anon, "synthetic-viewer", UserRole.VIEWER)
    unknown = login(anon, "nobody-here", "whatever-password")
    wrong = login(anon, "synthetic-viewer", "wrong-password-1")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json() == {"detail": "Wrong username or password.", "code": "invalid_credentials"}
    for _ in range(4):
        login(anon, "synthetic-viewer", "wrong-password-1")
    locked = login(anon, "synthetic-viewer")                # right password, but locked now
    assert locked.status_code == 429 and locked.json()["code"] == "locked"
    with anon.app.state.session_factory.begin() as session:
        user = session.scalar(select(AppUser).where(AppUser.username == "synthetic-viewer"))
        user.locked_until = None
    assert login(anon, "synthetic-viewer").status_code == 200


def test_deactivated_users_cannot_log_in(anon: TestClient) -> None:
    ensure_user(anon, "synthetic-gone", UserRole.ADMIN)
    with anon.app.state.session_factory.begin() as session:
        session.scalar(select(AppUser).where(AppUser.username == "synthetic-gone")).is_active = False
    assert login(anon, "synthetic-gone").status_code == 401


# ---- Sessions ------------------------------------------------------------------------------------

def test_logout_ends_the_session(client: TestClient) -> None:
    token = get_token(client)
    assert client.post("/api/v1/auth/logout").status_code == 204
    set_token(client, token)               # an old cookie must not work again
    assert client.get("/api/v1/auth/me").status_code == 401


@pytest.mark.parametrize("field", ["last_seen_at", "expires_at"])
def test_idle_and_absolute_expiry(client: TestClient, field: str) -> None:
    token = get_token(client)
    with client.app.state.session_factory.begin() as session:
        row = session.scalar(select(UserSession).where(UserSession.token_sha256 == token_digest(token)))
        setattr(row, field, utc_now() - timedelta(hours=13))
    assert client.get("/api/v1/auth/me").status_code == 401
    with client.app.state.session_factory() as session:
        assert session.scalar(select(UserSession).where(UserSession.token_sha256 == token_digest(token))).ended_at


def test_password_change_ends_other_sessions(client: TestClient) -> None:
    user = login_as(client, UserRole.APPROVER)
    old_token = get_token(client)
    other = login_as(client, UserRole.APPROVER)          # a second session of the same user (another browser)
    assert other.id == user.id
    second_token = get_token(client)
    weak = client.post("/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": "short"})
    assert weak.status_code == 422 and weak.json()["code"] == "weak_password"
    wrong = client.post("/api/v1/auth/password", json={"current_password": "nope", "new_password": "Another-Synthetic-9"})
    assert wrong.status_code == 401
    ok = client.post("/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": "Another-Synthetic-9"})
    assert ok.status_code == 200
    new_token = get_token(client)
    assert new_token not in (old_token, second_token)
    for stale in (old_token, second_token):
        set_token(client, stale)
        assert client.get("/api/v1/auth/me").status_code == 401
    set_token(client, new_token)
    assert client.get("/api/v1/auth/me").status_code == 200


# ---- CSRF ----------------------------------------------------------------------------------------

def test_state_changes_need_the_sessions_csrf_token(client: TestClient) -> None:
    good = client.headers.pop("X-CSRF-Token")
    missing = client.post("/api/v1/documents/1/validate")
    assert missing.status_code == 403 and missing.json()["code"] == "csrf_failed"
    wrong = client.post("/api/v1/documents/1/validate", headers={"X-CSRF-Token": "x" * 43})
    assert wrong.status_code == 403
    assert client.post("/api/v1/documents/1/validate", headers={"X-CSRF-Token": good}).status_code == 404  # passed the gate


# ---- Roles ---------------------------------------------------------------------------------------

def test_viewers_read_but_cannot_change_anything(client: TestClient) -> None:
    login_as(client, UserRole.VIEWER)
    assert client.get("/api/v1/documents").status_code == 200
    attempts = [
        client.post("/api/v1/documents", files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")},
                    data={"document_type": "purchase_invoice"}),
        client.post("/api/v1/documents/1/reprocess"),
        client.post("/api/v1/documents/1/validate"),
        client.patch("/api/v1/extractions/invoice/1/fields/invoice_number", json={"value": "X"}),
        client.post("/api/v1/duplicates/1/review", json={"decision": "duplicate"}),
        client.post("/api/v1/tally/sync", json={"company": "SYNTHETIC"}),
        client.post("/api/v1/documents/1/vouchers", json={"company_guid": "x"}),
    ]
    for response in attempts:
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "forbidden" and "reviewer" in response.json()["detail"]


# ---- Network -------------------------------------------------------------------------------------

@pytest.mark.parametrize(("address", "allowed"), [
    ("127.0.0.1", True), ("100.101.102.103", True), ("192.168.1.20", False), ("8.8.8.8", False),
])
def test_only_allowed_client_addresses_get_in(settings: Settings, migrated_engine: Engine, address: str, allowed: bool) -> None:
    with TestClient(create_app(settings), base_url="http://127.0.0.1", client=(address, 50000)) as c:
        response = c.get("/api/v1/health/live")
        assert (response.status_code == 200) is allowed
        if not allowed:
            assert response.json()["code"] == "client_not_allowed"
            assert "Content-Security-Policy" in response.headers


def test_narrowed_allow_list(settings: Settings, migrated_engine: Engine) -> None:
    narrow = settings.model_copy(update={"allowed_client_networks": ["127.0.0.1/32", "100.101.102.103/32"]})
    with TestClient(create_app(narrow), base_url="http://127.0.0.1", client=("100.101.102.104", 50000)) as c:
        assert c.get("/api/v1/health/live").status_code == 403
    with pytest.raises(ValueError):
        Settings(_env_file=None, allowed_client_networks="10.0.0.0/8")


def test_redirect_after_login_stays_on_this_site(client: TestClient) -> None:
    assert safe_next("/vouchers?show=all") == "/vouchers?show=all"
    for bad in ("//evil.example", "https://evil.example", "/\\evil.example", "", None):
        assert safe_next(bad) == "/"
    response = client.get("/login?next=//evil.example", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"


# ---- Command line --------------------------------------------------------------------------------

def test_cli_manages_users(settings: Settings, migrated_engine: Engine, monkeypatch, capsys) -> None:
    monkeypatch.setattr("tally_automation.cli.get_settings", lambda: settings)
    monkeypatch.setattr("sys.stdin", io.StringIO("Synthetic-Admin-2026\n"))
    assert cli(["users", "create", "Synthetic-Boss", "--role", "admin", "--password-stdin"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert cli(["users", "create", "synthetic-two", "--role", "viewer", "--password-stdin"]) == 2
    assert "at least 12 characters" in capsys.readouterr().err
    assert cli(["users", "set-role", "synthetic-boss", "approver"]) == 0
    assert cli(["users", "deactivate", "synthetic-boss"]) == 0
    cli(["users", "list"])
    listing = capsys.readouterr().out
    assert "synthetic-boss" in listing and "approver" in listing and "deactivated" in listing
    assert "Synthetic-Admin-2026" not in listing


def test_account_and_login_pages(client: TestClient) -> None:
    page = client.get("/account")
    assert page.status_code == 200 and 'src="/static/js/pages/account.js' in page.text and "Change password" in page.text
    logout(client)
    login_page = client.get("/login")
    assert login_page.status_code == 200 and 'autocomplete="current-password"' in login_page.text
    assert 'class="nav"' not in login_page.text and 'content=""' in login_page.text   # no menu, no CSRF token

"""Request gates: client allow-list, login, and CSRF.

Order for every request (outermost first): trusted Host header → security
headers → **client allow-list** → cross-site origin check → **login + CSRF** →
route.

* Client allow-list: the connecting address must be in
  ``ALLOWED_CLIENT_NETWORKS`` (loopback and the tailnet by default; narrow it to
  specific Tailscale device IPs). Uvicorn runs with ``proxy_headers=False``,
  so the address cannot be spoofed with ``X-Forwarded-For``.
* Login: every page and API path needs a valid session, except the login page,
  the login endpoint, static assets and a bare liveness probe. Pages redirect
  to ``/login``; the API answers ``401``.
* CSRF: every POST/PUT/PATCH/DELETE under ``/api/`` (except the login itself)
  must carry the session's CSRF token in ``X-CSRF-Token``. Pages get the token
  in a ``<meta>`` tag; it is never stored in browser storage. Together with the
  SameSite=Strict cookie and the Origin check this stops cross-site requests.
"""

from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Awaitable, Callable
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from tally_automation.api.middleware import UNSAFE_METHODS
from tally_automation.auth.service import CurrentUser, resolve_session
from tally_automation.config import Settings
from tally_automation.db.session import transaction

SESSION_COOKIE = "ta_session"
CSRF_HEADER = "x-csrf-token"
PUBLIC_PATHS = frozenset({"/login", "/api/v1/auth/login", "/api/v1/health/live", "/favicon.ico"})
PUBLIC_PREFIXES = ("/static/",)


def client_allowed(host: str | None, networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return any(address.version == n.version and address in n for n in networks)


def install_client_allowlist(app: FastAPI, settings: Settings, *, allow_test_client: bool = False) -> None:
    networks = [ipaddress.ip_network(n, strict=False) for n in settings.allowed_client_networks]

    @app.middleware("http")
    async def allowlist(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        host = request.client.host if request.client else None
        if not client_allowed(host, networks) and not (allow_test_client and host == "testclient"):
            return JSONResponse(status_code=403, content={
                "detail": "This device is not allowed to use the application.", "code": "client_not_allowed"})
        return await call_next(request)


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def install_auth(app: FastAPI, *, docs_paths: tuple[str, ...] = ()) -> None:
    @app.middleware("http")
    async def authenticate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request.state.user = None
        path = request.url.path
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            settings = request.app.state.settings

            def lookup() -> CurrentUser | None:
                with transaction(request.app.state.session_factory) as tx:
                    return resolve_session(tx, settings, token)

            request.state.user = await run_in_threadpool(lookup)
        user: CurrentUser | None = request.state.user
        if _is_public(path):
            return await call_next(request)
        if user is None:
            if path.startswith("/api/") or path in docs_paths:
                response: Response = JSONResponse(status_code=401, content={
                    "detail": "Please log in.", "code": "not_authenticated"})
            else:
                target = path + (f"?{request.url.query}" if request.url.query else "")
                response = RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
            if token:
                response.delete_cookie(SESSION_COOKIE, path="/")
            return response
        if request.method in UNSAFE_METHODS and path.startswith("/api/"):
            sent = request.headers.get(CSRF_HEADER, "")
            if not sent or not hmac.compare_digest(sent, user.csrf_token):
                return JSONResponse(status_code=403, content={
                    "detail": "The page's security token is missing or out of date. Reload the page and try again.",
                    "code": "csrf_failed"})
        return await call_next(request)

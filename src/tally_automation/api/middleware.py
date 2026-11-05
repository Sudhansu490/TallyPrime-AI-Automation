"""HTTP security middleware: response headers and cross-site request rejection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# No inline scripts or styles, no third-party origins. The frontend is entirely
# same-origin, so 'self' covers scripts, styles, images and fetch() calls.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

# FastAPI's Swagger UI loads its assets from a CDN and uses an inline script.
# It is served only outside production, and only this path gets the looser policy.
DOCS_CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "style-src 'self' https://cdn.jsdelivr.net",
    "img-src 'self' data: https://fastapi.tiangolo.com",
    "object-src 'none'",
    "frame-ancestors 'none'",
])

STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def is_cross_site(request: Request) -> bool:
    """True if a browser tells us an unsafe request came from another origin.

    Uses Fetch Metadata (Sec-Fetch-Site) and the Origin header. Non-browser
    clients send neither and are not affected. This blocks a malicious web page
    from submitting forms or fetch() calls to this app; per-session CSRF tokens
    are added with authentication in Phase 9.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    if origin is None:
        return False
    if origin == "null":
        return True
    return urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()


def install_origin_check(app: FastAPI) -> None:
    @app.middleware("http")
    async def reject_cross_site(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method in UNSAFE_METHODS and is_cross_site(request):
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-site request rejected.", "code": "cross_site_request"},
            )
        return await call_next(request)


def install_security_headers(app: FastAPI, *, docs_path: str | None) -> None:
    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        csp = DOCS_CONTENT_SECURITY_POLICY if docs_path and path == docs_path else CONTENT_SECURITY_POLICY
        response.headers["Content-Security-Policy"] = csp
        for name, value in STATIC_HEADERS.items():
            response.headers[name] = value
        # Pages and API responses may carry financial data: never cache them.
        # Versioned static assets may be cached by the browser.
        if not path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

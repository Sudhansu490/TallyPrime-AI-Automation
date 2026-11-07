"""HTML page routes.

Pages are thin shells: Jinja2 renders the layout and navigation only. All data
is loaded by the page's JavaScript module from the JSON API, and all business
logic and validation stays on the backend.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from tally_automation import __version__

FRONTEND_DIR = Path(__file__).resolve().parent
STATIC_DIR = FRONTEND_DIR / "static"
TEMPLATES_DIR = FRONTEND_DIR / "templates"

# On Windows, mimetypes reads the registry, which can map .js to text/plain.
# With X-Content-Type-Options: nosniff the browser would then refuse to run
# the ES modules, so pin the types explicitly.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# Jinja2Templates enables HTML autoescaping for .html templates.
templates = Jinja2Templates(directory=TEMPLATES_DIR)
router = APIRouter(include_in_schema=False)


@dataclass(frozen=True)
class NavItem:
    label: str
    path: str
    phase: int
    available: bool


# Pages that do not exist yet are shown disabled, labelled with their phase.
NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem("Dashboard", "/", 1, True),
    NavItem("Upload", "/upload", 2, True),
    NavItem("Processing", "/processing", 2, True),
    NavItem("Review", "/review", 4, True),
    NavItem("Duplicates", "/duplicates", 5, True),
    NavItem("TallyPrime", "/tally", 6, True),
    NavItem("Generate", "/generate", 7, True),
    NavItem("Vouchers", "/vouchers", 8, True),
)


def _render(request: Request, template: str, *, title: str, page_script: str) -> HTMLResponse:
    settings = request.app.state.settings
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(
        request,
        template,
        {
            "app_name": settings.app_name,
            "version": __version__,
            "title": title,
            "nav_items": NAV_ITEMS,
            "current_path": request.url.path,
            "page_script": page_script,
            # Logged-in user for the header; the CSRF token goes into a <meta> tag
            # for api.js (never into browser storage).
            "user": user,
            "csrf_token": user.csrf_token if user else "",
        },
    )


def safe_next(value: str | None) -> str:
    """Only same-site relative paths: never redirect to another site after login."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


@router.get("/login", response_class=HTMLResponse)
def login(request: Request, next: str | None = None) -> HTMLResponse:  # noqa: A002 - query parameter name
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return _render(request, "login.html", title="Log in", page_script="pages/login.js")


@router.get("/account", response_class=HTMLResponse)
def account(request: Request) -> HTMLResponse:
    return _render(request, "account.html", title="Account", page_script="pages/account.js")


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return _render(request, "dashboard.html", title="Dashboard", page_script="pages/dashboard.js")


@router.get("/upload", response_class=HTMLResponse)
def upload(request: Request) -> HTMLResponse:
    return _render(request, "upload.html", title="Upload documents", page_script="pages/upload.js")


@router.get("/processing", response_class=HTMLResponse)
def processing(request: Request) -> HTMLResponse:
    return _render(request, "processing.html", title="Document processing", page_script="pages/processing.js")


@router.get("/review", response_class=HTMLResponse)
def review(request: Request) -> HTMLResponse:
    return _render(request, "review.html", title="Extracted data review", page_script="pages/review.js")


@router.get("/duplicates", response_class=HTMLResponse)
def duplicates(request: Request) -> HTMLResponse:
    return _render(request, "duplicates.html", title="Duplicate management", page_script="pages/duplicates.js")


@router.get("/tally", response_class=HTMLResponse)
def tally(request: Request) -> HTMLResponse:
    return _render(request, "tally.html", title="TallyPrime integration", page_script="pages/tally.js")


@router.get("/vouchers", response_class=HTMLResponse)
def vouchers(request: Request) -> HTMLResponse:
    return _render(request, "vouchers.html", title="Voucher review", page_script="pages/vouchers.js")


@router.get("/generate", response_class=HTMLResponse)
def generate(request: Request) -> HTMLResponse:
    return _render(request, "generate.html", title="Voucher generation", page_script="pages/generate.js")

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
from fastapi.responses import HTMLResponse
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
    NavItem("TallyPrime", "/tally", 6, False),
    NavItem("Vouchers", "/vouchers", 8, False),
)


def _render(request: Request, template: str, *, title: str, page_script: str) -> HTMLResponse:
    settings = request.app.state.settings
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
        },
    )


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

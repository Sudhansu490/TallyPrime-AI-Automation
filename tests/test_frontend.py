"""Page routes, static file serving and static checks of the frontend source."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tally_automation import __version__
from tally_automation.frontend.routes import NAV_ITEMS, STATIC_DIR, TEMPLATES_DIR


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline_script_text: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._in_script = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_script_text.append(data)


def parse(html: str) -> _TagCollector:
    collector = _TagCollector()
    collector.feed(html)
    return collector


# ---- Page routes -----------------------------------------------------------

def test_dashboard_page_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "<title>Dashboard · Tally Automation</title>" in html
    assert 'id="health-grid"' in html
    assert f"v{__version__}" in html


PAGES = [
    ("/", "Dashboard", "pages/dashboard.js"),
    ("/upload", "Upload documents", "pages/upload.js"),
    ("/processing", "Document processing", "pages/processing.js"),
]


@pytest.mark.parametrize(("path", "title", "script"), PAGES)
def test_pages_load_only_local_module_scripts_and_styles(
    client: TestClient, path: str, title: str, script: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert f"<title>{title} · Tally Automation</title>" in response.text
    page = parse(response.text)
    assert page.inline_script_text == []
    scripts = [attrs for tag, attrs in page.tags if tag == "script"]
    assert scripts == [{"type": "module", "src": f"/static/js/{script}?v={__version__}"}]
    stylesheets = [attrs["href"] for tag, attrs in page.tags if tag == "link" and attrs.get("rel") == "stylesheet"]
    assert stylesheets == [f"/static/css/app.css?v={__version__}"]
    # No inline styles or inline event handlers.
    for _tag, attrs in page.tags:
        assert "style" not in attrs
        assert not any(name.startswith("on") for name in attrs)
    assert client.get(f"/static/js/{script}").status_code == 200


@pytest.mark.parametrize(("path", "title", "script"), PAGES)
def test_current_page_highlighted(client: TestClient, path: str, title: str, script: str) -> None:
    assert f'href="{path}" aria-current="page"' in client.get(path).text


def test_navigation_marks_current_page_and_unbuilt_pages(client: TestClient) -> None:
    html = client.get("/").text
    assert '<a href="/" aria-current="page">Dashboard</a>' in html
    for item in NAV_ITEMS:
        if not item.available:
            assert f'href="{item.path}"' not in html
            assert f"Available from Phase {item.phase}" in html


def test_unbuilt_pages_are_not_served(client: TestClient) -> None:
    for item in NAV_ITEMS:
        if not item.available:
            assert client.get(item.path).status_code == 404


# ---- Static files ----------------------------------------------------------

@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/css/app.css", "text/css"),
        ("/static/js/api.js", "text/javascript"),
        ("/static/js/ui.js", "text/javascript"),
        ("/static/js/pages/dashboard.js", "text/javascript"),
        ("/static/js/pages/upload.js", "text/javascript"),
        ("/static/js/pages/processing.js", "text/javascript"),
        ("/static/favicon.svg", "image/svg+xml"),
    ],
)
def test_static_assets_are_served(client: TestClient, path: str, content_type: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)


@pytest.mark.parametrize(
    "path",
    [
        "/static/missing.js",
        "/static/../config.py",
        "/static/%2e%2e/config.py",
        "/static/..%2f..%2fpyproject.toml",
        "/static/js/..%5c..%5croutes.py",
    ],
)
def test_static_rejects_missing_and_traversal_paths(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert "import" not in response.text  # no Python source leaked


# ---- Source rules (cheap guards for the XSS / no-CDN policy) ---------------

JS_FILES = sorted(STATIC_DIR.rglob("*.js"))
TEMPLATE_FILES = sorted(TEMPLATES_DIR.rglob("*.html"))
UNSAFE_DOM = re.compile(r"\.(innerHTML|outerHTML)\s*[+]?=|insertAdjacentHTML\s*\(|document\.write(ln)?\s*\(|\beval\s*\(|new\s+Function\s*\(")
EXTERNAL_URL = re.compile(r"""["'(]\s*(https?:)?//""")


def test_frontend_files_exist() -> None:
    assert JS_FILES and TEMPLATE_FILES


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_javascript_avoids_unsafe_dom_apis(path: Path) -> None:
    assert not UNSAFE_DOM.search(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_javascript_has_no_storage_of_credentials(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert "localStorage" not in source
    assert "sessionStorage" not in source


@pytest.mark.parametrize("path", [*JS_FILES, *TEMPLATE_FILES, *STATIC_DIR.rglob("*.css")], ids=lambda p: p.name)
def test_no_external_asset_urls(path: Path) -> None:
    assert not EXTERNAL_URL.search(path.read_text(encoding="utf-8"))

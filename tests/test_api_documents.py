"""Document API endpoints, including size limits and cross-site protection."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from synthetic_files import make_image, make_pdf
from auth_helpers import login_as
from tally_automation.api.main import create_app
from tally_automation.config import Settings


def upload(client: TestClient, data: bytes, name: str = "invoice.pdf",
           document_type: str = "purchase_invoice", **kwargs):
    return client.post(
        "/api/v1/documents",
        files={"file": (name, data, "application/octet-stream")},
        data={"document_type": document_type},
        **kwargs,
    )


def test_upload_limits(client: TestClient) -> None:
    body = client.get("/api/v1/documents/upload-limits").json()
    assert body["max_size_bytes"] == 20 * 1024 * 1024
    assert body["allowed_extensions"]["bank_statement"] == [".pdf"]
    assert set(body["allowed_extensions"]["purchase_invoice"]) == {".pdf", ".jpg", ".jpeg", ".png"}


def test_upload_accepts_and_queues(client: TestClient) -> None:
    response = upload(client, make_pdf(pages=2))
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "queued"
    assert body["document_type"] == "purchase_invoice"
    assert body["file"]["page_count"] == 2
    assert body["file"]["media_type"] == "application/pdf"
    assert len(body["file"]["sha256"]) == 64
    assert body["duplicate"] is None
    # Timestamps carry an explicit UTC offset so browsers convert them correctly.
    assert body["uploaded_at"].endswith(("+00:00", "Z"))
    job = body["latest_job"]
    assert job["created_at"].endswith(("+00:00", "Z"))
    assert job["status"] == "queued"
    assert set(job["stages"].values()) == {"pending"}


def test_duplicate_upload_explains_evidence(client: TestClient) -> None:
    data = make_image("JPEG")
    first = upload(client, data, "scan.jpg").json()
    second = upload(client, data, "scan (1).jpg")
    assert second.status_code == 201
    body = second.json()
    assert body["status"] == "duplicate"
    assert body["latest_job"] is None
    evidence = body["duplicate"]
    assert evidence["level"] == "exact_file"
    assert "SHA-256" in evidence["description"]
    assert evidence["duplicate_of"]["id"] == first["id"]
    assert evidence["duplicate_of"]["original_filename"] == "scan.jpg"


@pytest.mark.parametrize(
    ("data", "name", "document_type", "code"),
    [
        (make_pdf(), "invoice.exe", "purchase_invoice", "unsupported_extension"),
        (make_image("PNG"), "statement.png", "bank_statement", "type_not_allowed_for_document"),
        (make_image("PNG"), "invoice.pdf", "purchase_invoice", "extension_mismatch"),
        (b"not a pdf at all", "invoice.pdf", "purchase_invoice", "unrecognised_content"),
        (b"", "invoice.pdf", "purchase_invoice", "empty_file"),
    ],
)
def test_upload_rejections(client: TestClient, data: bytes, name: str, document_type: str, code: str) -> None:
    response = upload(client, data, name, document_type)
    assert response.status_code == 422
    assert response.json()["code"] == code
    assert response.json()["detail"]
    assert client.get("/api/v1/documents").json()["total"] == 0


def test_invalid_document_type(client: TestClient) -> None:
    response = upload(client, make_pdf(), document_type="sales_invoice")
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_document_type"


def test_missing_file(client: TestClient) -> None:
    response = client.post("/api/v1/documents", data={"document_type": "purchase_invoice"},
                           files={"other": ("x.txt", b"x")})
    assert response.status_code in (400, 422)


def test_non_multipart_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/documents", json={"document_type": "purchase_invoice"})
    assert response.status_code == 400
    assert response.json()["code"] == "not_multipart"


def test_chunked_upload_without_length_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/documents",
        content=iter([b"--x\r\n", b"data\r\n"]),
        headers={"Content-Type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 411


@pytest.fixture
def small_limit_client(settings: Settings, migrated_engine: Engine) -> Iterator[TestClient]:
    limited = settings.model_copy(update={"max_upload_size_mb": 1})
    with TestClient(create_app(limited), base_url="http://127.0.0.1") as client:
        login_as(client)
        yield client


def test_oversized_declared_length_rejected_before_reading(small_limit_client: TestClient) -> None:
    response = upload(small_limit_client, b"%PDF-" + b"0" * (2 * 1024 * 1024))
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"


def test_oversized_file_within_multipart_overhead_rejected(small_limit_client: TestClient) -> None:
    # Body fits under limit + overhead, but the file itself exceeds the limit.
    response = upload(small_limit_client, b"%PDF-" + b"0" * (1024 * 1024 + 10))
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"


# ---- Listing, detail, stats ----------------------------------------------------

def test_list_filter_and_paginate(client: TestClient) -> None:
    pdfs = [make_pdf(f"SYNTHETIC {n}") for n in range(3)]
    for n, data in enumerate(pdfs):
        upload(client, data, f"inv{n}.pdf")
    upload(client, make_pdf("SYNTHETIC STATEMENT"), "stmt.pdf", "bank_statement")
    upload(client, pdfs[0], "again.pdf")  # same bytes: duplicate

    everything = client.get("/api/v1/documents").json()
    assert everything["total"] == 5
    assert [d["id"] for d in everything["items"]] == [5, 4, 3, 2, 1]  # newest first

    page = client.get("/api/v1/documents", params={"limit": 2, "offset": 2}).json()
    assert [d["id"] for d in page["items"]] == [3, 2]

    statements = client.get("/api/v1/documents", params={"document_type": "bank_statement"}).json()
    assert [d["original_filename"] for d in statements["items"]] == ["stmt.pdf"]

    duplicates = client.get("/api/v1/documents", params={"status": "duplicate"}).json()
    assert [d["original_filename"] for d in duplicates["items"]] == ["again.pdf"]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"status": "bogus"}])
def test_list_rejects_bad_parameters(client: TestClient, params: dict) -> None:
    assert client.get("/api/v1/documents", params=params).status_code == 422


def test_get_document_and_404(client: TestClient) -> None:
    created = upload(client, make_pdf()).json()
    assert client.get(f"/api/v1/documents/{created['id']}").json() == created
    assert client.get("/api/v1/documents/999").status_code == 404


def test_stats(client: TestClient) -> None:
    assert client.get("/api/v1/documents/stats").json() == {
        "total": 0, "queued": 0, "processing": 0, "text_extracted": 0, "processed": 0, "failed": 0,
        "duplicates": 0, "possible_duplicates": 0, "validation_failed": 0, "ready_for_vouchers": 0,
        "pending_approvals": 0,
    }
    data = make_pdf()
    upload(client, data)
    upload(client, data)
    stats = client.get("/api/v1/documents/stats").json()
    assert (stats["total"], stats["queued"], stats["duplicates"]) == (2, 1, 1)
    assert stats["pending_approvals"] == 0  # ready vouchers waiting for approval (Phase 8)


# ---- Cross-site protection -----------------------------------------------------

@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://127.0.0.1:9999"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_cross_site_uploads_rejected(client: TestClient, headers: dict) -> None:
    response = upload(client, make_pdf(), headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "cross_site_request"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/api/v1/documents").json()["total"] == 0


def test_same_origin_upload_allowed(client: TestClient) -> None:
    response = upload(client, make_pdf(), headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 201


def test_cross_site_get_is_not_blocked(client: TestClient) -> None:
    # Safe methods change nothing; the browser's same-origin policy keeps responses private.
    assert client.get("/api/v1/documents/stats", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200

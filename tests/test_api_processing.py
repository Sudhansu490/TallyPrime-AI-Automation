"""Extracted-text and reprocess endpoints. SYNTHETIC data only."""

from fastapi.testclient import TestClient

from synthetic_files import make_pdf, make_scanned_pdf
from tally_automation.processing.worker import ProcessingWorker
from test_processing_worker import FakeOcrEngine


def upload(client: TestClient, data: bytes, name: str = "synthetic.pdf") -> dict:
    response = client.post(
        "/api/v1/documents", files={"file": (name, data, "application/pdf")},
        data={"document_type": "purchase_invoice"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def run_worker(client: TestClient, engine: FakeOcrEngine | None = None) -> None:
    state = client.app.state
    ProcessingWorker(state.settings, state.session_factory, engine or FakeOcrEngine()).run_once()


def test_text_endpoint_returns_pages_and_evidence(client: TestClient) -> None:
    created = upload(client, make_scanned_pdf())
    assert client.get(f"/api/v1/documents/{created['id']}/text").json()["detail"] == (
        "No extracted text yet for this document."
    )
    run_worker(client)

    document = client.get(f"/api/v1/documents/{created['id']}").json()
    assert document["status"] == "text_extracted"
    assert document["latest_job"]["stages"] == {
        "text_extraction": "succeeded", "ocr": "succeeded", "ai_extraction": "pending", "validation": "pending",
    }
    body = client.get(f"/api/v1/documents/{created['id']}/text").json()
    assert body["attempt"] == 1 and body["low_confidence_threshold"] == 0.85
    [page] = body["pages"]
    assert page["text_source"] == "ocr" and page["ocr_engine"] == "fake-ocr"
    assert page["text"].startswith("SYNTHETIC TEST INVOICE")
    assert page["ocr_lines"][0]["low_confidence"] is True
    assert page["ocr_lines"][1]["low_confidence"] is False
    assert client.get("/api/v1/documents/stats").json()["text_extracted"] == 1


def test_text_endpoint_404_for_unknown_document(client: TestClient) -> None:
    assert client.get("/api/v1/documents/999/text").json()["detail"] == "Document not found."


def test_reprocess_failed_document(client: TestClient) -> None:
    created = upload(client, make_scanned_pdf())
    run_worker(client, FakeOcrEngine(error="synthetic outage"))
    failed = client.get(f"/api/v1/documents/{created['id']}").json()
    assert failed["status"] == "failed"
    assert (failed["latest_job"]["error_stage"], failed["latest_job"]["error_message"]) == (
        "ocr", "Page 1: synthetic outage",
    )

    response = client.post(f"/api/v1/documents/{created['id']}/reprocess")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert (body["latest_job"]["attempt"], body["latest_job"]["status"]) == (2, "queued")


def test_reprocess_refuses_non_failed_and_unknown(client: TestClient) -> None:
    created = upload(client, make_pdf())
    response = client.post(f"/api/v1/documents/{created['id']}/reprocess")
    assert response.status_code == 409 and response.json()["code"] == "not_requeueable"
    response = client.post("/api/v1/documents/999/reprocess")
    assert response.status_code == 404 and response.json()["code"] == "not_found"


def test_reprocess_rejects_cross_site_requests(client: TestClient) -> None:
    created = upload(client, make_scanned_pdf())
    run_worker(client, FakeOcrEngine(error="synthetic outage"))
    response = client.post(
        f"/api/v1/documents/{created['id']}/reprocess", headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert client.get(f"/api/v1/documents/{created['id']}").json()["status"] == "failed"


def test_app_starts_worker_only_when_enabled(settings, migrated_engine) -> None:
    from tally_automation.api.main import create_app
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as test_client:
        assert test_client.app.state.worker is None  # disabled in test settings
    settings.processing_worker_enabled = True
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as test_client:
        worker = test_client.app.state.worker
        assert worker is not None and worker._thread.is_alive()
    assert worker._thread is None  # stopped on shutdown

"""AI stage in the worker, the extraction/review API and audited edits. SYNTHETIC data only."""

from __future__ import annotations

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from extraction_fakes import (
    INVOICE_TEXT, STATEMENT_PAGE_1, STATEMENT_PAGE_2, SUPPLIER_GSTIN, FakeLlm, field, invoice_reply,
    statement_page1_reply, statement_page2_reply,
)
from tally_automation.db.models import (
    BankStatement, BankTransaction, Document, DocumentPage, DocumentStatus, ExtractionRun, FieldEdit, Invoice,
    JobStatus, ProcessingJob, StageStatus,
)
from tally_automation.extraction.llm import LlmError
from tally_automation.processing.worker import ProcessingWorker
from test_processing_worker import FakeOcrEngine


def text_pdf(*pages: str) -> bytes:
    with fitz.open() as pdf:
        for text in pages:
            page = pdf.new_page()
            for index, line in enumerate(text.splitlines()):
                page.insert_text((40, 60 + index * 16), line, fontsize=9)
        return pdf.tobytes()


def upload(client: TestClient, data: bytes, document_type: str = "purchase_invoice") -> int:
    response = client.post(
        "/api/v1/documents", files={"file": ("synthetic.pdf", data, "application/pdf")},
        data={"document_type": document_type},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def process(client: TestClient, llm: FakeLlm | None, pages_per_call: int = 3) -> None:
    state = client.app.state
    state.settings.llm_pages_per_call = pages_per_call
    ProcessingWorker(state.settings, state.session_factory, FakeOcrEngine(), llm).run_once()


@pytest.fixture
def invoice_id(client: TestClient) -> int:
    document_id = upload(client, text_pdf(INVOICE_TEXT))
    process(client, FakeLlm(invoice_reply()))
    return document_id


def test_worker_runs_ai_stage_and_stores_entities(client: TestClient, invoice_id: int) -> None:
    document = client.get(f"/api/v1/documents/{invoice_id}").json()
    assert document["status"] == "processed"
    assert document["latest_job"]["stages"] == {
        "text_extraction": "succeeded", "ocr": "skipped", "ai_extraction": "succeeded", "validation": "succeeded",
    }
    with client.app.state.session_factory() as session:
        invoice = session.scalars(select(Invoice)).one()
        assert (invoice.supplier_gstin, invoice.invoice_number) == (SUPPLIER_GSTIN, "SYN-0001")
        assert str(invoice.invoice_date) == "2026-04-01" and str(invoice.grand_total) == "1180.00"
        assert len(invoice.line_items) == 1
        run = session.scalars(select(ExtractionRun)).one()
        assert (run.provider, run.llm_calls, len(run.raw_responses)) == ("fake", 1, 1)
    assert client.get("/api/v1/documents/stats").json()["processed"] == 1


def test_without_llm_document_stops_at_text_extracted(client: TestClient) -> None:
    document_id = upload(client, text_pdf(INVOICE_TEXT))
    process(client, None)
    document = client.get(f"/api/v1/documents/{document_id}").json()
    assert document["status"] == "text_extracted"
    assert document["latest_job"]["stages"]["ai_extraction"] == "pending"
    assert client.get(f"/api/v1/documents/{document_id}/extraction").status_code == 404
    # It can be re-run once an LLM is configured.
    assert client.post(f"/api/v1/documents/{document_id}/reprocess").json()["status"] == "queued"
    process(client, FakeLlm(invoice_reply()))
    assert client.get(f"/api/v1/documents/{document_id}").json()["status"] == "processed"


def test_ai_failure_keeps_page_text(client: TestClient) -> None:
    document_id = upload(client, text_pdf(INVOICE_TEXT))
    process(client, FakeLlm(LlmError("synthetic outage")))
    document = client.get(f"/api/v1/documents/{document_id}").json()
    job = document["latest_job"]
    assert document["status"] == "failed"
    assert (job["error_stage"], job["stages"]["ai_extraction"]) == ("ai_extraction", "failed")
    assert job["error_message"] == "LLM request failed: synthetic outage"
    assert job["stages"]["text_extraction"] == "succeeded"
    pages = client.get(f"/api/v1/documents/{document_id}/text").json()["pages"]
    assert "SYN-0001" in pages[0]["text"]


def test_extraction_endpoint(client: TestClient, invoice_id: int) -> None:
    body = client.get(f"/api/v1/documents/{invoice_id}/extraction").json()
    assert body["document_type"] == "purchase_invoice" and body["run"]["attempt"] == 1
    fields = {f["name"]: f for f in body["data"]["fields"]}
    total = fields["grand_total"]
    assert (total["raw"], total["value"], total["status"], total["method"]) == ("1,180.00", "1180.00", "valid", "extracted")
    assert total["evidence"] == "Grand Total 1,180.00" and total["page"] == 1
    assert fields["due_date"]["status"] == "missing"
    [row] = body["data"]["rows"]
    assert row["number"] == 1 and {f["name"]: f["value"] for f in row["fields"]}["igst_amount"] == "180.00"
    assert body["data"]["row_columns"][0] == {"name": "description", "label": "Description", "type": "text"}
    assert body["summary"]["invalid"] == 0 and body["summary"]["fields"] == 24 + 15


def test_extraction_404s(client: TestClient) -> None:
    assert client.get("/api/v1/documents/999/extraction").json()["detail"] == "Document not found."


def test_edit_field_is_reparsed_audited_and_synced(client: TestClient, invoice_id: int) -> None:
    body = client.get(f"/api/v1/documents/{invoice_id}/extraction").json()
    invoice_id_db = body["data"]["id"]
    url = f"/api/v1/extractions/invoice/{invoice_id_db}/fields/grand_total"

    response = client.patch(url, json={"value": "1,190.00", "reason": "synthetic correction"})
    assert response.status_code == 200
    edited = response.json()["field"]
    assert (edited["raw"], edited["value"], edited["method"], edited["status"]) == ("1,190.00", "1190.00", "edited", "valid")
    assert edited["original"]["raw"] == "1,180.00" and edited["original"]["evidence"] == "Grand Total 1,180.00"

    # Unparsable input is stored as typed but marked invalid, and the key column is cleared.
    edited = client.patch(url, json={"value": "1.190.00"}).json()["field"]
    assert (edited["value"], edited["status"]) == (None, "invalid")
    assert edited["original"]["raw"] == "1,180.00"  # still the first extracted value
    with client.app.state.session_factory() as session:
        assert session.get(Invoice, invoice_id_db).grand_total is None
        edits = session.scalars(select(FieldEdit).order_by(FieldEdit.id)).all()
        assert [(e.old_raw, e.new_raw) for e in edits] == [("1,180.00", "1,190.00"), ("1,190.00", "1.190.00")]
        assert edits[0].client_address == "testclient" and edits[0].edited_by_user_id is not None

    history = client.get(f"/api/v1/extractions/invoice/{invoice_id_db}/edits").json()
    assert [h["reason"] for h in history] == ["synthetic correction", None]

    summary = client.get(f"/api/v1/documents/{invoice_id}/extraction").json()["summary"]
    assert summary["edited"] == 1 and summary["invalid"] == 1


def test_clearing_a_required_field_is_flagged(client: TestClient, invoice_id: int) -> None:
    entity_id = client.get(f"/api/v1/documents/{invoice_id}/extraction").json()["data"]["id"]
    edited = client.patch(f"/api/v1/extractions/invoice/{entity_id}/fields/invoice_number", json={"value": "  "}).json()
    assert edited["field"]["status"] == "missing" and "required_missing" in edited["field"]["flags"]


def test_edit_errors(client: TestClient, invoice_id: int) -> None:
    body = client.get(f"/api/v1/documents/{invoice_id}/extraction").json()
    entity_id, row_id = body["data"]["id"], body["data"]["rows"][0]["id"]
    base = "/api/v1/extractions"
    assert client.patch(f"{base}/invoice/{entity_id}/fields/grand_total", json={"value": "1,180.00"}).status_code == 409
    assert client.patch(f"{base}/invoice/{entity_id}/fields/bogus", json={"value": "1"}).status_code == 404
    assert client.patch(f"{base}/invoice/999/fields/grand_total", json={"value": "1"}).status_code == 404
    assert client.patch(f"{base}/nonsense/1/fields/x", json={"value": "1"}).status_code == 422
    assert client.patch(f"{base}/invoice/{entity_id}/fields/grand_total", json={"value": "x" * 2001}).status_code == 422
    # Line items are edited through their own entity.
    row = client.patch(f"{base}/invoice_line_item/{row_id}/fields/quantity", json={"value": "12"}).json()
    assert row["field"]["value"] == "12"


def test_edits_reject_cross_site_requests(client: TestClient, invoice_id: int) -> None:
    entity_id = client.get(f"/api/v1/documents/{invoice_id}/extraction").json()["data"]["id"]
    response = client.patch(
        f"/api/v1/extractions/invoice/{entity_id}/fields/grand_total", json={"value": "1.00"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_bank_statement_flow_masks_account_number(client: TestClient) -> None:
    document_id = upload(client, text_pdf(STATEMENT_PAGE_1, STATEMENT_PAGE_2), "bank_statement")
    process(client, FakeLlm(statement_page1_reply(), statement_page2_reply()), pages_per_call=1)
    body = client.get(f"/api/v1/documents/{document_id}/extraction").json()
    assert body["document_type"] == "bank_statement" and body["run"]["llm_calls"] == 2
    fields = {f["name"]: f for f in body["data"]["fields"]}
    account = fields["account_number"]
    assert account["value"] == account["raw"] == "XXXXXXXX9012"
    assert "123456789012" not in client.get(f"/api/v1/documents/{document_id}/extraction").text
    assert account["evidence"] == "Account No: XXXXXXXX9012 IFSC: SYNB0001234"
    assert [r["number"] for r in body["data"]["rows"]] == [1, 2]

    with client.app.state.session_factory() as session:
        statement = session.scalars(select(BankStatement)).one()
        assert statement.account_number == "123456789012"  # full value kept server-side for matching
        debit, credit = session.scalars(select(BankTransaction).order_by(BankTransaction.sequence)).all()
        assert (str(debit.debit), debit.credit, str(debit.balance)) == ("5000.00", None, "5000.00")
        assert str(credit.credit) == "2500.00" and str(credit.txn_date) == "2026-04-05"

    statement_id = body["data"]["id"]
    edited = client.patch(
        f"/api/v1/extractions/bank_statement/{statement_id}/fields/account_number", json={"value": "123456789099"},
    ).json()["field"]
    assert edited["raw"] == "XXXXXXXX9099" and edited["original"]["raw"] == "XXXXXXXX9012"
    history = client.get(f"/api/v1/extractions/bank_statement/{statement_id}/edits").json()
    assert (history[0]["old_raw"], history[0]["new_raw"]) == ("XXXXXXXX9012", "XXXXXXXX9099")
    assert fields["ifsc"]["evidence"] == "Account No: XXXXXXXX9012 IFSC: SYNB0001234"
    # Neither the original nor the edited full number appears anywhere in any response.
    for url in (f"/api/v1/documents/{document_id}/extraction", f"/api/v1/extractions/bank_statement/{statement_id}/edits"):
        text = client.get(url).text
        assert "123456789012" not in text and "123456789099" not in text
    ifsc_edit = client.patch(f"/api/v1/extractions/bank_statement/{statement_id}/fields/ifsc", json={"value": "SYNB0001235"})
    assert "123456789012" not in ifsc_edit.text and "123456789099" not in ifsc_edit.text


def test_hallucinated_value_is_invalid_end_to_end(client: TestClient) -> None:
    document_id = upload(client, text_pdf(INVOICE_TEXT))
    process(client, FakeLlm(invoice_reply(header={"due_date": field("15/04/2026", "Due: 15/04/2026")})))
    fields = {f["name"]: f for f in client.get(f"/api/v1/documents/{document_id}/extraction").json()["data"]["fields"]}
    assert fields["due_date"]["status"] == "invalid"
    assert "value_not_in_document" in fields["due_date"]["flags"]


def test_health_reports_llm_configuration(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()
    assert (body["llm_provider"], body["llm_model"]) == (None, None)

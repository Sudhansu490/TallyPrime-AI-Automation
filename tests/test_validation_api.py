"""Validation and duplicate management through the worker and API. SYNTHETIC data only."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from extraction_fakes import (
    INVOICE_TEXT, STATEMENT_PAGE_1, STATEMENT_PAGE_2, FakeLlm, field, invoice_reply, reply,
    statement_page1_reply, statement_page2_reply,
)
from synthetic_files import make_pdf
from tally_automation.db.models import BankTransaction, DocumentType, DuplicateReview, StageStatus
from test_extraction_api import process, text_pdf, upload


def validation(client: TestClient, document_id: int) -> dict:
    response = client.get(f"/api/v1/documents/{document_id}/validation")
    assert response.status_code == 200, response.text
    return response.json()


def processed_invoice(client: TestClient, extra_line: str = "", **reply_overrides) -> int:
    document_id = upload(client, text_pdf(INVOICE_TEXT + extra_line))
    process(client, FakeLlm(invoice_reply(**reply_overrides)))
    return document_id


def test_clean_invoice_passes_and_is_eligible(client: TestClient) -> None:
    document_id = processed_invoice(client)
    body = validation(client, document_id)
    assert (body["outcome"], body["error_count"], body["warning_count"]) == ("passed", 0, 0)
    assert body["eligibility"] == {"eligible": True, "reasons": [], "excluded_transaction_ids": []}
    grand_total = next(c for c in body["checks"] if c["code"] == "grand_total")
    assert grand_total["passed"] and grand_total["calculated"] == "1180.00"
    listed = client.get("/api/v1/documents").json()["items"][0]
    assert listed["validation"] == {"outcome": "passed", "error_count": 0, "warning_count": 0, "open_duplicates": 0}
    assert client.get("/api/v1/documents/stats").json()["ready_for_vouchers"] == 1


def test_arithmetic_error_blocks_until_corrected(client: TestClient) -> None:
    text = INVOICE_TEXT.replace("Grand Total 1,180.00", "Grand Total 1,280.00")
    document_id = upload(client, text_pdf(text))
    process(client, FakeLlm(invoice_reply(header={"grand_total": field("1,280.00", "Grand Total 1,280.00")})))
    body = validation(client, document_id)
    assert body["outcome"] == "failed" and not body["eligibility"]["eligible"]
    bad = next(c for c in body["checks"] if c["code"] == "grand_total")
    assert (bad["calculated"], bad["printed"], bad["difference"]) == ("1180.00", "1280.00", "100.00")
    assert client.get("/api/v1/documents/stats").json()["validation_failed"] == 1

    # A reviewer finds the transcription was right and the supplier's invoice is wrong:
    # nothing is "corrected" automatically; editing re-runs validation.
    invoice_id = client.get(f"/api/v1/documents/{document_id}/extraction").json()["data"]["id"]
    edited = client.patch(f"/api/v1/extractions/invoice/{invoice_id}/fields/grand_total",
                          json={"value": "1,180.00", "reason": "test"}).json()
    assert edited["validation"] == {"outcome": "passed", "error_count": 0, "warning_count": 0}
    assert validation(client, document_id)["eligibility"]["eligible"]


def test_same_invoice_in_another_pdf_is_flagged_and_reviewed(client: TestClient) -> None:
    first = processed_invoice(client)
    second = processed_invoice(client, "\nSYNTHETIC COPY RECEIVED BY EMAIL")
    matches = client.get("/api/v1/duplicates").json()["matches"]
    assert len(matches) == 1
    match = matches[0]
    assert (match["kind"], match["rule"], match["strength"], match["status"]) == (
        "invoice", "same_invoice_number", "probable", "open")
    assert (match["document"]["id"], match["other_document"]["id"]) == (second, first)  # later upload is the candidate
    assert match["record"] == match["other_record"] == "Invoice SYN-0001"
    assert all(e["match"] == "same" for e in match["evidence"] if e["field"] != "supplier_name")

    for doc in (first, second):
        e = validation(client, doc)["eligibility"]
        assert not e["eligible"] and "1 possible duplicate(s) have not been reviewed." in e["reasons"]
    assert client.get("/api/v1/documents/stats").json()["possible_duplicates"] == 2

    decided = client.post(f"/api/v1/duplicates/{match['id']}/review",
                          json={"decision": "duplicate", "note": "same invoice emailed twice"}).json()
    assert decided["status"] == "duplicate" and decided["resolved_at"]
    assert decided["reviews"][0]["decision"] == "duplicate" and decided["reviews"][0]["client_address"]
    assert validation(client, first)["eligibility"]["eligible"]
    later = validation(client, second)["eligibility"]
    assert not later["eligible"] and later["reasons"] == [f"Confirmed duplicate of document #{first}."]

    again = client.post(f"/api/v1/duplicates/{match['id']}/review", json={"decision": "not_duplicate"})
    assert again.status_code == 409 and again.json()["code"] == "already_resolved"
    reopened = client.post(f"/api/v1/duplicates/{match['id']}/review", json={"decision": "reopened"}).json()
    assert reopened["status"] == "open" and reopened["resolved_at"] is None
    client.post(f"/api/v1/duplicates/{match['id']}/review", json={"decision": "not_duplicate"})
    assert validation(client, second)["eligibility"]["eligible"]
    with client.app.state.session_factory() as session:
        assert [r.decision.value for r in session.scalars(select(DuplicateReview).order_by(DuplicateReview.id))] == [
            "duplicate", "reopened", "not_duplicate"]
    assert client.get("/api/v1/duplicates?status=resolved").json()["counts"]["not_duplicate"] == 1


def test_ocr_variant_of_invoice_number_is_flagged(client: TestClient) -> None:
    processed_invoice(client)
    text = INVOICE_TEXT.replace("Invoice No: SYN-0001", "Invoice No: SYN-O0O1")
    upload(client, text_pdf(text))
    process(client, FakeLlm(invoice_reply(header={"invoice_number": field("SYN-O0O1", "Invoice No: SYN-O0O1")})))
    match = client.get("/api/v1/duplicates").json()["matches"][0]
    assert (match["rule"], match["strength"]) == ("invoice_number_variant", "probable")


def test_correcting_the_number_removes_an_unreviewed_match(client: TestClient) -> None:
    processed_invoice(client)
    second = processed_invoice(client, "\nSYNTHETIC SECOND")
    assert client.get("/api/v1/duplicates").json()["counts"]["open"] == 1
    invoice_id = client.get(f"/api/v1/documents/{second}/extraction").json()["data"]["id"]
    for name, value in (("invoice_number", "SYN-0002"), ("invoice_date", "02/04/2026")):
        client.patch(f"/api/v1/extractions/invoice/{invoice_id}/fields/{name}", json={"value": value})
    assert client.get("/api/v1/duplicates").json()["counts"]["open"] == 0


def test_overlapping_statements_flag_the_shared_transaction_without_leaking_the_account(client: TestClient) -> None:
    first = upload(client, text_pdf(STATEMENT_PAGE_1, STATEMENT_PAGE_2), "bank_statement")
    process(client, FakeLlm(statement_page1_reply(), statement_page2_reply()), pages_per_call=1)
    assert validation(client, first)["outcome"] == "passed"

    # A second statement of the same account repeating the 05/04 transaction.
    page = "SYNTHETIC BANK - TEST STATEMENT - NOT REAL\nAccount No: 123456789012\n" \
           "Opening Balance 5,000.00 Cr\n05/04/2026 SYNTHETIC SALARY CREDIT 2,500.00 7,500.00 Cr"
    second = upload(client, text_pdf(page), "bank_statement")
    process(client, FakeLlm(reply(DocumentType.BANK_STATEMENT, {
        "account_number": field("123456789012", "Account No: 123456789012"),
        "opening_balance": field("5,000.00 Cr", "Opening Balance 5,000.00 Cr"),
    }, [{"row_text": "05/04/2026 SYNTHETIC SALARY CREDIT 2,500.00 7,500.00 Cr", "txn_date": "05/04/2026",
         "description": "SYNTHETIC SALARY CREDIT", "credit": "2,500.00", "balance": "7,500.00 Cr"}])))
    response = client.get("/api/v1/duplicates")
    assert "123456789012" not in response.text
    match = response.json()["matches"][0]
    assert (match["kind"], match["rule"]) == ("bank_transaction", "same_fingerprint")
    assert match["record"] == "Transaction 1 (p. 1)" and match["other_record"] == "Transaction 2 (p. 2)"
    with client.app.state.session_factory() as session:
        assert all(t.fingerprint for t in session.scalars(select(BankTransaction)))

    bulk = client.post("/api/v1/duplicates/review", json={"match_ids": [match["id"]], "decision": "duplicate"})
    assert bulk.status_code == 200 and bulk.json()[0]["status"] == "duplicate"
    e = validation(client, second)["eligibility"]
    assert e["eligible"] and len(e["excluded_transaction_ids"]) == 1   # left out, statement still usable
    assert "123456789012" not in client.get(f"/api/v1/documents/{second}/validation").text


def test_swapped_column_is_caught_on_statement(client: TestClient) -> None:
    document_id = upload(client, text_pdf(STATEMENT_PAGE_1, STATEMENT_PAGE_2), "bank_statement")
    page1 = statement_page1_reply().replace('"debit": "5,000.00", "credit": null', '"debit": null, "credit": "5,000.00"')
    assert page1 != statement_page1_reply()
    process(client, FakeLlm(page1, statement_page2_reply()), pages_per_call=1)
    errors = [c for c in validation(client, document_id)["checks"] if not c["passed"] and c["severity"] == "error"]
    assert any("swapped" in c["message"] and c["row"] == 1 for c in errors)


def test_validate_endpoint_and_errors(client: TestClient) -> None:
    document_id = processed_invoice(client)
    rerun = client.post(f"/api/v1/documents/{document_id}/validate")
    assert rerun.status_code == 200 and rerun.json()["outcome"] == "passed"
    assert client.post("/api/v1/documents/999/validate").status_code == 404
    pending = upload(client, make_pdf("SYNTHETIC PENDING"))
    response = client.post(f"/api/v1/documents/{pending}/validate")
    assert response.status_code == 409 and response.json()["code"] == "no_extraction"
    assert client.get(f"/api/v1/documents/{pending}/validation").status_code == 404
    assert client.post("/api/v1/duplicates/999/review", json={"decision": "duplicate"}).status_code == 404
    assert client.post("/api/v1/duplicates/review", json={"match_ids": [], "decision": "duplicate"}).status_code == 422


def test_validation_failure_keeps_the_extraction(client: TestClient, monkeypatch) -> None:
    import tally_automation.processing.worker as worker_module

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic validation bug")

    monkeypatch.setattr(worker_module, "validate_document", broken)
    document_id = processed_invoice(client)
    document = client.get(f"/api/v1/documents/{document_id}").json()
    assert document["status"] == "processed"
    assert document["latest_job"]["stages"]["validation"] == StageStatus.FAILED.value
    assert client.get(f"/api/v1/documents/{document_id}/extraction").status_code == 200
    monkeypatch.undo()
    assert client.post(f"/api/v1/documents/{document_id}/validate").status_code == 200
    assert client.get(f"/api/v1/documents/{document_id}").json()["latest_job"]["stages"]["validation"] == "succeeded"


def test_exact_file_duplicates_are_listed(client: TestClient) -> None:
    data = make_pdf("SYNTHETIC SAME BYTES")
    original = upload(client, data)
    copy = upload(client, data)
    body = client.get("/api/v1/duplicates").json()
    assert body["counts"]["exact_files"] == 1
    assert (body["exact_files"][0]["document"]["id"], body["exact_files"][0]["duplicate_of"]["id"]) == (copy, original)


def test_decisions_reject_cross_site_requests(client: TestClient) -> None:
    response = client.post("/api/v1/duplicates/review", json={"match_ids": [1], "decision": "duplicate"},
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_bulk_eligibility_matches_the_per_document_rules(client: TestClient) -> None:
    from tally_automation.validation.service import eligibility, eligible_document_ids

    def check() -> set[int]:
        with client.app.state.session_factory() as session:
            single = {d for d in documents if eligibility(session, d).eligible}
            assert eligible_document_ids(session, documents) == single
            return single

    first = processed_invoice(client)
    second = processed_invoice(client, "\nSYNTHETIC SECOND COPY")
    text = INVOICE_TEXT.replace("Grand Total 1,180.00", "Grand Total 1,280.00").replace("SYN-0001", "SYN-0099")
    failed = upload(client, text_pdf(text))
    process(client, FakeLlm(invoice_reply(header={"invoice_number": field("SYN-0099"),
                                                  "grand_total": field("1,280.00", "Grand Total 1,280.00")})))
    documents = [first, second, failed]
    assert check() == set()                        # open duplicate + failed validation
    match = client.get("/api/v1/duplicates").json()["matches"][0]
    client.post(f"/api/v1/duplicates/{match['id']}/review", json={"decision": "duplicate"})
    assert check() == {first}
    client.post(f"/api/v1/duplicates/{match['id']}/review", json={"decision": "reopened"})
    client.post(f"/api/v1/duplicates/{match['id']}/review", json={"decision": "not_duplicate"})
    assert check() == {first, second}
    assert client.get("/api/v1/documents/stats").json()["ready_for_vouchers"] == 2

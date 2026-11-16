"""Bank reconciliation API, with SYNTHETIC statements and SYNTHETIC TallyPrime masters."""

from __future__ import annotations

import hashlib
import itertools

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from auth_helpers import login_as
from extraction_fakes import STATEMENT_PAGE_1, STATEMENT_PAGE_2, FakeLlm, statement_page1_reply, statement_page2_reply
from tally_fakes import COMPANY, COMPANY_GUID, FakeTally
from test_extraction_api import process, text_pdf, upload
from tally_automation.db.models import (
    BankStatement, BankTransaction, Document, DocumentFile, DocumentStatus, DocumentType, ExtractionRun, JobStatus,
    MediaType, ProcessingJob, ReconciliationAccount, ReconciliationEvent, UserRole, ValidationOutcome,
    ValidationResult,
)

ACCOUNT = "123456789012"
_counter = itertools.count(1)


def f(value: str | None) -> dict:
    return {"raw": value, "value": value, "status": "valid" if value is not None else "missing"}


def seed_statement(client: TestClient, frm: str, to: str, opening: str | None, closing: str | None,
                   rows=(), *, account: str | None = ACCOUNT, outcome: ValidationOutcome = ValidationOutcome.PASSED) -> int:
    """Insert one processed bank statement straight into the database (no AI). Returns the document id."""
    n = next(_counter)
    with client.app.state.session_factory.begin() as s:
        file = DocumentFile(sha256=hashlib.sha256(f"synthetic-{n}".encode()).hexdigest(), size_bytes=1,
                            media_type=MediaType.PDF, storage_path=f"synthetic/{n}.pdf", page_count=1)
        document = Document(file=file, document_type=DocumentType.BANK_STATEMENT,
                            original_filename=f"synthetic-{n}.pdf", status=DocumentStatus.PROCESSED)
        job = ProcessingJob(document=document, attempt=1, status=JobStatus.SUCCEEDED)
        s.add_all([file, document, job])
        s.flush()
        run = ExtractionRun(job_id=job.id, document_id=document.id, document_type=DocumentType.BANK_STATEMENT,
                            provider="fake", model="fake", prompt_version="test", llm_calls=0, prompt_tokens=0,
                            completion_tokens=0, seconds=0.0, document_issues=[], raw_responses=[])
        s.add(run)
        s.flush()
        header = {"account_number": f(account), "bank_name": f("Synthetic Bank"), "period_from": f(frm),
                  "period_to": f(to), "opening_balance": f(opening), "closing_balance": f(closing)}
        statement = BankStatement(extraction_run_id=run.id, document_id=document.id, fields=header,
                                  account_number=account)
        s.add(statement)
        s.flush()
        for seq, (d, dr, cr, bal) in enumerate(rows, start=1):
            s.add(BankTransaction(bank_statement_id=statement.id, sequence=seq, confidence=1.0,
                                  fields={"txn_date": f(d), "debit": f(dr), "credit": f(cr), "balance": f(bal)}))
        errors = 1 if outcome is ValidationOutcome.FAILED else 0
        s.add(ValidationResult(extraction_run_id=run.id, document_id=document.id, rules_version="test",
                               outcome=outcome, error_count=errors, warning_count=0, checks=[]))
        return document.id


@pytest.fixture
def three_months(client: TestClient) -> str:
    """April and June 2026 statements (May missing), June's opening off by 25.00. Returns the account key."""
    seed_statement(client, "2026-04-01", "2026-04-30", "1000.00", "1300.00",
                   [("2026-04-05", None, "500.00", "1500.00"), ("2026-04-20", "200.00", None, "1300.00")])
    seed_statement(client, "2026-06-01", "2026-06-30", "1325.00", "1325.00")
    accounts = client.get("/api/v1/reconciliation/accounts").json()["accounts"]
    assert len(accounts) == 1
    return accounts[0]["key"]


@pytest.fixture
def tally(client: TestClient) -> FakeTally:
    fake = FakeTally()
    client.app.state.settings.tally_host = "127.0.0.1"
    client.app.state.tally_transport = fake.transport
    assert client.post("/api/v1/tally/sync", json={"company": COMPANY}).status_code == 200
    fake.requests.clear()
    return fake


def test_accounts_are_listed_masked_and_addressed_by_an_opaque_key(client: TestClient, three_months: str) -> None:
    response = client.get("/api/v1/reconciliation/accounts")
    account = response.json()["accounts"][0]
    assert ACCOUNT not in response.text and account["account_number"] == "XXXXXXXX9012"
    assert ACCOUNT not in three_months and len(three_months) == 64
    assert (account["statement_count"], account["first_day"], account["last_day"]) == (2, "2026-04-01", "2026-06-30")
    detail = client.get(f"/api/v1/reconciliation/accounts/{three_months}")
    assert detail.status_code == 200 and ACCOUNT not in detail.text
    assert [s["period_from"] for s in detail.json()["statements"]] == ["2026-04-01", "2026-06-01"]
    assert client.get(f"/api/v1/reconciliation/accounts/{'0' * 64}").status_code == 404


def test_monthly_view_finds_the_missing_month_and_the_opening_mismatch(client: TestClient, three_months: str) -> None:
    body = client.get(f"/api/v1/reconciliation/accounts/{three_months}/monthly").json()
    assert (body["from"], body["to"]) == ("2026-04", "2026-06")
    april, may, june = body["periods"]
    assert april["status"] == "matched" and april["calculated_closing"] == "1300.00" and april["difference"] == "0.00"
    assert may["status"] == "missing" and may["opening"]["value"] is None and may["closing"]["value"] is None
    assert june["previous"]["status"] == "unknown"          # May is missing, so June cannot be compared with it
    assert body["summary"]["missing"] == 1 and body["summary"]["matched"] == 2

    # Once May's statement is there, June's opening is compared with May's closing: the 25.00 jump shows.
    seed_statement(client, "2026-05-01", "2026-05-31", "1300.00", "1300.00")
    june = client.get(f"/api/v1/reconciliation/accounts/{three_months}/monthly?from=2026-06&to=2026-06").json()["periods"][0]
    assert june["previous"]["status"] == "mismatch" and june["previous"]["difference"] == "25.00"


def test_yearly_view_and_coverage(client: TestClient, three_months: str) -> None:
    body = client.get(f"/api/v1/reconciliation/accounts/{three_months}/yearly?fy_from=2025-26&fy_to=2026-27").json()
    assert [p["label"] for p in body["periods"]] == ["2025-26", "2026-27"]
    assert body["periods"][0]["status"] == "missing"
    fy = body["periods"][1]
    assert fy["status"] == "partial" and "2026-05" in fy["missing_months"] and fy["opening"]["value"] == "1000.00"
    assert fy["closing"]["value"] is None                  # no statement for March 2027

    cover = client.get(f"/api/v1/reconciliation/accounts/{three_months}/coverage").json()
    assert cover["missing_months"] == ["2026-05"]
    assert cover["missing"] == [{"from": "2026-05-01", "to": "2026-05-31", "days": 31}]
    assert cover["partial_years"][0]["year"] == "2026-27"


@pytest.mark.parametrize("query", ["monthly?from=2026-13&to=2026-14", "monthly?from=2026-06&to=2026-04",
                                   "yearly?fy_from=2026-28&fy_to=2026-27", "coverage?from=2026-06-01&to=2026-04-01"])
def test_bad_ranges_are_refused(client: TestClient, three_months: str, query: str) -> None:
    assert client.get(f"/api/v1/reconciliation/accounts/{three_months}/{query}").status_code == 422


def test_statements_without_an_account_number_or_period_are_reported_not_guessed(client: TestClient) -> None:
    seed_statement(client, "2026-04-01", "2026-04-30", "1.00", "1.00", account=None)
    unplaced = seed_statement(client, None, None, "1.00", "1.00", account="999988887777")
    body = client.get("/api/v1/reconciliation/accounts").json()
    assert len(body["unassigned"]) == 1
    key = body["accounts"][0]["key"]
    assert body["accounts"][0]["unusable_count"] == 1
    cover = client.get(f"/api/v1/reconciliation/accounts/{key}/coverage").json()
    assert cover["from"] is None and cover["unusable_statements"][0]["document_id"] == unplaced
    assert client.get(f"/api/v1/reconciliation/accounts/{key}/monthly").json()["periods"] == []


def test_failed_validation_is_shown_as_unverified(client: TestClient) -> None:
    seed_statement(client, "2026-04-01", "2026-04-30", "1.00", "1.00", outcome=ValidationOutcome.FAILED)
    account = client.get("/api/v1/reconciliation/accounts").json()["accounts"][0]
    assert account["unverified_count"] == 1
    april = client.get(f"/api/v1/reconciliation/accounts/{account['key']}/monthly").json()["periods"][0]
    assert april["unverified"] is True


def test_a_processed_statement_is_reconciled_from_its_extraction(client: TestClient) -> None:
    upload(client, text_pdf(STATEMENT_PAGE_1, STATEMENT_PAGE_2), "bank_statement")
    process(client, FakeLlm(statement_page1_reply(), statement_page2_reply()), pages_per_call=1)
    account = client.get("/api/v1/reconciliation/accounts").json()["accounts"][0]
    april = client.get(f"/api/v1/reconciliation/accounts/{account['key']}/monthly").json()["periods"][0]
    assert april["label"] == "2026-04"
    assert (april["opening"]["value"], april["closing"]["value"]) == ("10000.00", "7500.00")
    assert april["calculated_closing"] == "7500.00" and april["status"] == "matched"


# ---- Ledger link ---------------------------------------------------------------------------------

def test_linking_a_bank_ledger_is_audited_and_sends_nothing_to_tally(
    client: TestClient, three_months: str, tally: FakeTally,
) -> None:
    ledgers = client.get(f"/api/v1/reconciliation/companies/{COMPANY_GUID}/bank-ledgers").json()
    bank = next(l for l in ledgers if l["name"] == "Synthetic Bank")
    detail = client.get(f"/api/v1/reconciliation/accounts/{three_months}").json()
    assert detail["suggestions"][0]["ledger_guid"] == bank["guid"]

    url = f"/api/v1/reconciliation/accounts/{three_months}/ledger"
    linked = client.post(url, json={"company_guid": COMPANY_GUID, "ledger_guid": bank["guid"], "note": "checked"})
    assert linked.status_code == 200, linked.text
    assert linked.json()["ledger"]["ledger_name"] == "Synthetic Bank"
    assert client.post(url, json={"company_guid": COMPANY_GUID, "ledger_guid": "l-Synthetic Supplier Pvt Ltd"}).status_code == 409
    assert client.post(url, json={"ledger_guid": None}).json()["ledger"] is None
    assert client.post(url, json={"ledger_guid": None}).status_code == 409        # nothing left to unlink

    events = client.get(f"/api/v1/reconciliation/accounts/{three_months}").json()["events"]
    assert [e["kind"] for e in events] == ["ledger_unlinked", "ledger_linked"]
    assert events[1]["note"] == "checked" and events[1]["user"]
    with client.app.state.session_factory() as s:
        stored = s.scalar(select(ReconciliationAccount))
        assert stored.account_number == ACCOUNT and stored.ledger_guid is None
        assert len(s.scalars(select(ReconciliationEvent)).all()) == 2
    assert tally.requests == []


def test_viewers_can_read_but_not_link(client: TestClient, three_months: str, tally: FakeTally) -> None:
    login_as(client, UserRole.VIEWER)
    assert client.get(f"/api/v1/reconciliation/accounts/{three_months}/monthly").status_code == 200
    response = client.post(f"/api/v1/reconciliation/accounts/{three_months}/ledger",
                           json={"company_guid": COMPANY_GUID, "ledger_guid": "l-Synthetic Bank"})
    assert response.status_code == 403


def test_ledger_list_needs_retrieved_masters(client: TestClient) -> None:
    assert client.get("/api/v1/reconciliation/companies/unknown/bank-ledgers").status_code == 409

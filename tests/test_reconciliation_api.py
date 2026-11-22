"""Bank reconciliation API, with SYNTHETIC statements and SYNTHETIC TallyPrime masters."""

from __future__ import annotations

import hashlib
import io
import itertools

import openpyxl

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from auth_helpers import login_as
from extraction_fakes import STATEMENT_PAGE_1, STATEMENT_PAGE_2, FakeLlm, statement_page1_reply, statement_page2_reply
from tally_fakes import COMPANY, COMPANY_GUID, FakeTally
from test_extraction_api import process, text_pdf, upload
from tally_automation.db.models import (
    BankStatement, BankTransaction, Document, DocumentFile, DocumentStatus, DocumentType, ExtractionRun, JobStatus,
    MediaType, ProcessingJob, ReconciliationAccount, ReconciliationEvent, ReconciliationRun, UserRole,
    ValidationOutcome, ValidationResult,
)
from tally_automation.tally.xmlio import assert_read_only

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


# ---- Transactions with TallyPrime, saved runs, decisions, reports --------------------------------------------

def voucher(n: int, day: str, kind: str, bank_amount: str, narration: str = "SYNTHETIC") -> dict:
    other = "-" + bank_amount if not bank_amount.startswith("-") else bank_amount[1:]
    return {"date": day, "type": kind, "narration": narration, "guid": f"synthetic-v{n}", "master_id": str(n),
            "number": str(n), "entries": [("Synthetic Bank", bank_amount), ("Synthetic Supplier Pvt Ltd", other)]}


@pytest.fixture
def april(client: TestClient, tally: FakeTally) -> str:
    """April 2026 statement, its TallyPrime side, and the account linked to the bank ledger. Returns the key.

    Statement: opening 1000, +500 (5th), −200 (20th), −5.61 twice (25th), closing 1288.78.
    TallyPrime: +500 (5th), −200 (21st), −5.61 once (25th), −1000 (28th, not on the statement);
    ledger 1000.00 (debit) on 31 March, 294.39 on 30 April.
    """
    seed_statement(client, "2026-04-01", "2026-04-30", "1000.00", "1288.78", [
        ("2026-04-05", None, "500.00", "1500.00"), ("2026-04-20", "200.00", None, "1300.00"),
        ("2026-04-25", "5.61", None, "1294.39"), ("2026-04-25", "5.61", None, "1288.78")])
    tally.vouchers += [voucher(1, "20260405", "Receipt", "-500.00"), voucher(2, "20260421", "Payment", "200.00"),
                       voucher(3, "20260425", "Payment", "5.61"), voucher(4, "20260428", "Payment", "1000.00"),
                       voucher(5, "20260410", "Journal", "0")]
    tally.ledger_balances.update({"20260331": "-1000.00", "20260430": "-294.39"})
    key = client.get("/api/v1/reconciliation/accounts").json()["accounts"][0]["key"]
    assert client.post(f"/api/v1/reconciliation/accounts/{key}/ledger",
                       json={"company_guid": COMPANY_GUID, "ledger_guid": "l-Synthetic Bank"}).status_code == 200
    tally.requests.clear()
    return key


def compare(client: TestClient, key: str, **extra) -> dict:
    response = client.post(f"/api/v1/reconciliation/accounts/{key}/runs",
                           json={"kind": "transactions", "date_from": "2026-04-01", "date_to": "2026-04-30", **extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_transactions_are_compared_with_tally_read_only(client: TestClient, april: str, tally: FakeTally) -> None:
    run = compare(client, april)
    r = run["result"]
    assert (r["bank_transactions"], r["tally_entries"]) == (4, 4)
    assert r["counts"]["matched"] == 2 and r["counts"]["awaiting_approval"] == 1
    assert (r["counts"]["bank_only"], r["counts"]["tally_only"]) == (1, 1)
    st = r["statement"]
    assert (st["tally_opening"], st["tally_closing"], st["bank_opening"], st["bank_closing"]) == (
        "1000.00", "294.39", "1000.00", "1288.78")
    assert st["expected_bank_closing"] == "1288.78" and st["unexplained"] == "0.00"
    # Only export (read) requests reached TallyPrime: vouchers and two ledger balances.
    assert tally.imports == 0 and tally.requests
    for request in tally.requests:
        assert_read_only(request.content.decode())
    assert ACCOUNT not in client.get(f"/api/v1/reconciliation/runs/{run['id']}").text


def test_decisions_are_audited_and_carry_over_to_the_next_comparison(client: TestClient, april: str) -> None:
    run = compare(client, april)
    uncertain = next(m for m in run["result"]["matches"] if m["needs_approval"])
    exact = next(m for m in run["result"]["matches"] if m["status"] == "matched")
    url = f"/api/v1/reconciliation/runs/{run['id']}/matches"

    assert client.post(f"{url}/{exact['signature']}", json={"decision": "approved"}).status_code == 422
    approved = client.post(f"{url}/{uncertain['signature']}", json={"decision": "approved", "note": "same cheque"})
    assert approved.status_code == 200 and approved.json()["result"]["counts"]["approved"] == 1
    assert client.post(f"{url}/{uncertain['signature']}", json={"decision": "approved"}).status_code == 409

    again = compare(client, april)                      # a later comparison of the same entries
    carried = next(m for m in again["result"]["matches"] if m["signature"] == uncertain["signature"])
    assert carried["decision"]["decision"] == "approved" and carried["decision"]["note"] == "same cheque"

    rejected = client.post(f"/api/v1/reconciliation/runs/{again['id']}/matches/{uncertain['signature']}",
                           json={"decision": "rejected"}).json()["result"]
    assert rejected["counts"]["bank_only"] == 2 and rejected["statement"]["unexplained"] == "0.00"
    assert client.post(f"{url}/{uncertain['signature']}", json={"decision": "reopened"}).status_code == 200
    assert client.post(f"{url}/{uncertain['signature']}", json={"decision": "reopened"}).status_code == 409
    assert client.post(f"{url}/{'f' * 40}", json={"decision": "rejected"}).status_code == 404

    kinds = [e["kind"] for e in client.get(f"/api/v1/reconciliation/accounts/{april}").json()["events"]]
    assert kinds[:4] == ["match_reopened", "match_rejected", "run_saved", "match_approved"]
    # The saved comparison itself never changes.
    with client.app.state.session_factory() as s:
        stored = s.get(ReconciliationRun, run["id"]).result
        assert next(m for m in stored["matches"] if m["signature"] == uncertain["signature"]).get("decision") is None


def test_reports_download_and_are_recorded(client: TestClient, april: str) -> None:
    run = compare(client, april)
    xlsx = client.get(f"/api/v1/reconciliation/runs/{run['id']}/report.xlsx")
    assert xlsx.status_code == 200 and xlsx.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert 'attachment; filename="bank-reconciliation-' in xlsx.headers["content-disposition"]
    workbook = openpyxl.load_workbook(io.BytesIO(xlsx.content))
    assert "Matches" in workbook.sheetnames
    assert ACCOUNT not in str([[c.value for c in row] for ws in workbook for row in ws.iter_rows()])
    pdf = client.get(f"/api/v1/reconciliation/runs/{run['id']}/report.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert client.get(f"/api/v1/reconciliation/runs/{run['id']}/report.csv").status_code == 422
    kinds = [e["kind"] for e in client.get(f"/api/v1/reconciliation/accounts/{april}").json()["events"]]
    assert kinds[:2] == ["report_downloaded", "report_downloaded"]

    login_as(client, UserRole.VIEWER)
    assert client.get(f"/api/v1/reconciliation/runs/{run['id']}/report.pdf").status_code == 200
    assert client.post(f"/api/v1/reconciliation/accounts/{april}/runs",
                       json={"kind": "monthly", "month_from": "2026-04", "month_to": "2026-04"}).status_code == 403


def test_balance_runs_are_saved_and_marked_out_of_date_after_a_correction(client: TestClient, three_months: str) -> None:
    url = f"/api/v1/reconciliation/accounts/{three_months}/runs"
    monthly = client.post(url, json={"kind": "monthly", "month_from": "2026-04", "month_to": "2026-06"}).json()
    assert monthly["label"] == "Monthly 2026-04 to 2026-06" and not monthly["stale"]
    assert [p["status"] for p in monthly["result"]["periods"]] == ["matched", "missing", "matched"]
    yearly = client.post(url, json={"kind": "yearly", "fy_from": "2026-27", "fy_to": "2026-27"}).json()
    assert yearly["result"]["coverage"]["missing_months"][0] == "2026-05"
    report = client.get(f"/api/v1/reconciliation/runs/{monthly['id']}/report.xlsx")
    sheets = openpyxl.load_workbook(io.BytesIO(report.content)).sheetnames
    assert sheets == ["Summary", "Months", "Missing statements", "Statements used"]

    with client.app.state.session_factory.begin() as s:          # a reviewer corrects a closing balance
        statement = s.scalars(select(BankStatement).order_by(BankStatement.id)).first()
        statement.fields = {**statement.fields, "closing_balance": f("1301.00")}
    assert client.get(f"/api/v1/reconciliation/runs/{monthly['id']}").json()["stale"] is True
    runs = client.get(url).json()
    assert [r["kind"] for r in runs] == ["yearly", "monthly"] and all(r["stale"] for r in runs)
    assert client.post(url, json={"kind": "monthly", "month_from": "2026-06", "month_to": "2026-04"}).status_code == 422


def test_comparison_needs_a_linked_ledger_and_a_reachable_tally(client: TestClient, three_months: str, tally: FakeTally) -> None:
    url = f"/api/v1/reconciliation/accounts/{three_months}/runs"
    body = {"kind": "transactions", "date_from": "2026-04-01", "date_to": "2026-04-30"}
    assert client.post(url, json=body).json()["code"] == "not_linked"
    client.post(f"/api/v1/reconciliation/accounts/{three_months}/ledger",
                json={"company_guid": COMPANY_GUID, "ledger_guid": "l-Synthetic Bank"})
    assert client.post(url, json={**body, "date_to": "2027-05-01"}).status_code == 422          # over a year
    tally.fail = "connect"
    response = client.post(url, json=body)
    assert response.status_code == 502 and response.json()["code"] == "tally_error"
    assert client.get(url).json() == []                                                        # nothing saved

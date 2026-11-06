"""TallyPrime integration API (read-only). SYNTHETIC data through a fake Tally transport."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tally_fakes import BANK_ACCOUNT, COMPANY, COMPANY_GUID, PARTY_GSTIN, FakeTally
from tally_fakes import _envelope as envelope
from tally_fakes import _ledger as ledger
from tally_automation.db.models import SyncStatus, TallyLedger, TallySyncRun


@pytest.fixture
def fake(client: TestClient) -> FakeTally:
    fake = FakeTally()
    client.app.state.settings.tally_host = "127.0.0.1"
    client.app.state.tally_transport = fake.transport
    return fake


def sync(client: TestClient, company: str = COMPANY):
    return client.post("/api/v1/tally/sync", json={"company": company})


def test_status_when_not_configured(client: TestClient) -> None:
    body = client.get("/api/v1/tally/status").json()
    assert (body["configured"], body["reachable"], body["read_only"], body["write_enabled"]) == (False, False, True, False)
    assert "not configured" in body["message"]
    assert sync(client).json()["code"] == "not_configured"


def test_status_lists_loaded_companies(client: TestClient, fake: FakeTally) -> None:
    body = client.get("/api/v1/tally/status").json()
    assert (body["reachable"], body["server_running"]) == (True, True)
    assert body["loaded_companies"] == [
        {"name": COMPANY, "guid": COMPANY_GUID, "books_from": "2025-04-01", "state": "Maharashtra"}]


@pytest.mark.parametrize(("fail", "reachable"), [("connect", False), ("timeout", False)])
def test_status_reports_unreachable_tally_without_failing(client: TestClient, fail: str, reachable: bool) -> None:
    client.app.state.settings.tally_host = "127.0.0.1"
    client.app.state.tally_transport = FakeTally(fail=fail).transport
    response = client.get("/api/v1/tally/status")
    assert response.status_code == 200 and response.json()["reachable"] is reachable


def test_public_tally_host_is_refused(client: TestClient) -> None:
    client.app.state.settings.tally_host = "8.8.8.8"
    body = client.get("/api/v1/tally/status").json()
    assert body["configured"] and not body["reachable"] and "refused" in body["message"]


def test_sync_stores_snapshot_and_masks_bank_accounts(client: TestClient, fake: FakeTally) -> None:
    run = sync(client)
    assert run.status_code == 200, run.text
    body = run.json()
    assert (body["status"], body["group_count"], body["ledger_count"], body["voucher_type_count"]) == ("succeeded", 6, 6, 3)
    assert body["removed_characters"] >= 4 and body["company_guid"] == COMPANY_GUID

    companies = client.get("/api/v1/tally/companies").json()
    assert [(c["name"], c["ledger_count"]) for c in companies] == [(COMPANY, 6)]
    base = f"/api/v1/tally/companies/{COMPANY_GUID}"
    ledgers = client.get(f"{base}/ledgers").json()
    assert ledgers["total"] == 6
    bank = client.get(f"{base}/ledgers?kind=bank").json()["items"]
    assert [b["bank_account_number"] for b in bank] == ["XXXXXXXX9012"]
    assert BANK_ACCOUNT not in client.get(f"{base}/ledgers").text
    with client.app.state.session_factory() as session:   # full number kept server-side for matching
        assert session.scalar(select(TallyLedger.bank_account_number).where(TallyLedger.ifsc.is_not(None))) == BANK_ACCOUNT

    invalid = client.get(f"{base}/ledgers?kind=invalid_gstin").json()["items"]
    assert [l["name"] for l in invalid] == ["Synthetic Typo Vendor"]
    assert client.get(f"{base}/ledgers?q={PARTY_GSTIN}").json()["items"][0]["gstin_valid"] is True
    assert [l["name"] for l in client.get(f"{base}/ledgers?kind=gst_duty").json()["items"]] == ["Input CGST 9%"]
    assert client.get(f"{base}/ledgers?group=Current%20Liabilities").json()["total"] == 3
    assert len(client.get(f"{base}/groups").json()) == 6
    types = client.get(f"{base}/voucher-types").json()
    assert {(v["name"], v["is_active"]) for v in types} >= {("Old Purchase", False), ("Purchase", True)}


def test_resync_replaces_the_snapshot(client: TestClient, fake: FakeTally) -> None:
    sync(client)
    fake.overrides["Ledger"] = envelope(ledger("Only Ledger Now", "Sundry Creditors"))
    second = sync(client).json()
    assert second["ledger_count"] == 1
    names = [l["name"] for l in client.get(f"/api/v1/tally/companies/{COMPANY_GUID}/ledgers").json()["items"]]
    assert names == ["Only Ledger Now"]


def test_failed_sync_keeps_the_previous_snapshot(client: TestClient, fake: FakeTally) -> None:
    sync(client)
    fake.overrides["VoucherType"] = "<RESPONSE>Unknown Request, cannot be processed</RESPONSE>"
    failed = sync(client)
    assert failed.status_code == 502 and failed.json()["code"] == "tally_error"
    assert client.get(f"/api/v1/tally/companies/{COMPANY_GUID}/ledgers").json()["total"] == 6
    runs = client.get("/api/v1/tally/sync-runs").json()
    assert [r["status"] for r in runs] == ["failed", "succeeded"]
    assert "Unknown Request" in runs[0]["error_message"]


def test_company_must_be_loaded(client: TestClient, fake: FakeTally) -> None:
    response = sync(client, "SYNTHETIC OTHER COMPANY")
    assert response.status_code == 409 and response.json()["code"] == "company_not_loaded"
    assert COMPANY in response.json()["detail"]
    fake.loaded = False
    assert sync(client).json()["code"] == "company_not_loaded"


def test_unknown_company_in_listing_endpoints(client: TestClient) -> None:
    for path in ("ledgers", "groups", "voucher-types"):
        assert client.get(f"/api/v1/tally/companies/nope/{path}").status_code == 404


def test_every_request_to_tally_is_a_read(client: TestClient, fake: FakeTally) -> None:
    client.get("/api/v1/tally/status")
    sync(client)
    assert fake.requests
    for request in fake.requests:
        assert request.method in {"GET", "POST"}
        if request.method == "POST":
            assert b"<TALLYREQUEST>Export</TALLYREQUEST>" in request.content and b"Import" not in request.content


def test_interrupted_sync_is_marked_failed_at_startup(client: TestClient) -> None:
    from tally_automation.tally.service import recover_interrupted_syncs

    factory = client.app.state.session_factory
    with factory.begin() as session:
        session.add(TallySyncRun(company_name=COMPANY, host="127.0.0.1", port=9000, status=SyncStatus.RUNNING, warnings=[]))
    assert recover_interrupted_syncs(factory) == 1
    assert client.get("/api/v1/tally/sync-runs").json()[0]["status"] == "failed"


def test_sync_rejects_cross_site_requests(client: TestClient, fake: FakeTally) -> None:
    response = client.post("/api/v1/tally/sync", json={"company": COMPANY}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403 and not fake.requests

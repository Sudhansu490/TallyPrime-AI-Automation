"""End-to-end: the whole workflow through the HTTP API, with each role doing its part.

Upload → text extraction → AI extraction (fake LLM) → validation → duplicate
check → TallyPrime masters (fake TallyPrime) → voucher generation → approval →
import → verification, plus the dashboard figures and the logs. SYNTHETIC
documents and a SYNTHETIC TallyPrime only.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from auth_helpers import login_as
from extraction_fakes import (
    INVOICE_TEXT, STATEMENT_PAGE_1, STATEMENT_PAGE_2, SUPPLIER_GSTIN, FakeLlm, field, invoice_reply,
    statement_page1_reply, statement_page2_reply,
)
from tally_fakes import BANK_ACCOUNT, COMPANY, COMPANY_GUID, LEDGERS, FakeTally
from tally_automation.db.models import UserRole
from test_extraction_api import process, text_pdf, upload
from test_voucher_api import EXTRA_LEDGERS, VOUCHER_TYPES, guid

API = "/api/v1"


def ok(response, status: int = 200) -> dict | list:
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture
def tally(client: TestClient) -> FakeTally:
    fake = FakeTally()
    fake.overrides["Ledger"] = LEDGERS.replace("   </COLLECTION>", EXTRA_LEDGERS + "   </COLLECTION>")
    fake.overrides["VoucherType"] = VOUCHER_TYPES
    client.app.state.settings.tally_host = "127.0.0.1"
    client.app.state.tally_transport = fake.transport
    return fake


def test_full_workflow_with_roles(client: TestClient, tally: FakeTally, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    # ---- Reviewer: upload and process ----------------------------------------------------------
    reviewer = login_as(client, UserRole.REVIEWER)
    invoice_doc = upload(client, text_pdf(INVOICE_TEXT))
    process(client, FakeLlm(invoice_reply(header={
        "total_taxable_value": field("1,000.00"), "total_igst": field("180.00"),
    })))
    statement_doc = upload(client, text_pdf(STATEMENT_PAGE_1, STATEMENT_PAGE_2), "bank_statement")
    process(client, FakeLlm(statement_page1_reply(), statement_page2_reply()), pages_per_call=1)
    documents = {d["id"]: d for d in ok(client.get(f"{API}/documents"))["items"]}
    assert documents[invoice_doc]["status"] == documents[statement_doc]["status"] == "processed"
    assert documents[invoice_doc]["validation"]["outcome"] == "passed"

    # A correction on the Review page is audited with the reviewer's name.
    invoice_id = ok(client.get(f"{API}/documents/{invoice_doc}/extraction"))["data"]["id"]
    ok(client.patch(f"{API}/extractions/invoice/{invoice_id}/fields/purchase_order_number",
                    json={"value": "PO-SYN-1", "reason": "end-to-end test"}))
    [edit] = ok(client.get(f"{API}/extractions/invoice/{invoice_id}/edits"))
    assert edit["edited_by"].startswith("Synthetic reviewer")
    assert ok(client.get(f"{API}/duplicates"))["matches"] == []

    # ---- Reviewer: TallyPrime masters and vouchers ------------------------------------------------
    ok(client.post(f"{API}/tally/sync", json={"company": COMPANY}))
    [purchase] = ok(client.post(f"{API}/documents/{invoice_doc}/vouchers", json={
        "company_guid": COMPANY_GUID,
        "invoice": {"voucher_type": "Purchase", "party": guid("Synthetic Supplier Pvt Ltd"),
                    "purchase": {"18": guid("Synthetic Office Supplies")}, "duty": {"IGST": guid("Input IGST 18%")}},
    }))
    setup = ok(client.get(f"{API}/documents/{statement_doc}/voucher-setup"))
    out, into = setup["statement"]["transactions"]
    bank_vouchers = ok(client.post(f"{API}/documents/{statement_doc}/vouchers", json={
        "company_guid": COMPANY_GUID,
        "statement": {"bank": guid("Synthetic Bank"),
                      "voucher_types": {"payment": "Payment", "receipt": "Receipt", "contra": "Contra"},
                      "transactions": [{"transaction_id": out["id"], "ledger": guid("Synthetic Supplier Pvt Ltd")},
                                       {"transaction_id": into["id"], "ledger": guid("Synthetic Cash")}]},
    }))
    vouchers = [purchase, *bank_vouchers]
    assert [(v["voucher_kind"], v["status"]) for v in vouchers] == [
        ("purchase", "ready"), ("payment", "ready"), ("contra", "ready")]
    assert all(v["generated_by"].startswith("Synthetic reviewer") for v in vouchers)
    assert client.post(f"{API}/vouchers/{purchase['id']}/approve", json={}).status_code == 403   # not an approver

    # ---- Viewer: reads, changes nothing --------------------------------------------------------------
    login_as(client, UserRole.VIEWER)
    assert len(ok(client.get(f"{API}/vouchers?approval_status=pending"))) == 3
    assert client.post(f"{API}/documents/{invoice_doc}/validate").status_code == 403

    # ---- Approver: approve and import -------------------------------------------------------------------
    approver = login_as(client, UserRole.APPROVER)
    assert ok(client.get(f"{API}/documents/stats"))["pending_approvals"] == 3
    for v in vouchers:
        approved = ok(client.post(f"{API}/vouchers/{v['id']}/approve", json={"acknowledge_warnings": True}))
        assert approved["approval_status"] == "approved"
    assert ok(client.post(f"{API}/vouchers/{purchase['id']}/import", json={"confirm": True}), 409)["code"] == "write_disabled"
    client.app.state.settings.tally_write_enabled = True
    tally.requests.clear()
    for v in vouchers:
        attempt = ok(client.post(f"{API}/vouchers/{v['id']}/import", json={"confirm": True}))
        assert (attempt["status"], attempt["verified"]) == ("created", True), attempt
        assert attempt["imported_by"].startswith("Synthetic approver")
    assert tally.imports == 3 and len(tally.vouchers) == 3
    assert {v["marker"] for v in vouchers} == {m for t in tally.vouchers for m in [t["narration"].split("[")[-1].rstrip("]")]}
    assert ok(client.post(f"{API}/vouchers/{purchase['id']}/import", json={"confirm": True}), 409)["code"] == "already_imported"

    history = ok(client.get(f"{API}/vouchers/{purchase['id']}/history"))
    assert history["approvals"][0]["decided_by_user_id"] == approver.id
    assert history["imports"][0]["company_name"] == COMPANY

    # ---- Dashboard ------------------------------------------------------------------------------------------
    stats = ok(client.get(f"{API}/documents/stats"))
    assert (stats["processed"], stats["failed"], stats["pending_approvals"]) == (2, 0, 0)
    assert ok(client.get(f"{API}/vouchers/stats")) == {
        "awaiting_approval": 0, "approved_not_imported": 0, "imported": 3, "import_failed": 0,
        "import_unclear": 0, "blocked": 0,
    }

    # ---- Logs: no account numbers, GSTINs, party or ledger names, passwords or tokens ---------------------------
    logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for secret in (BANK_ACCOUNT, SUPPLIER_GSTIN, "Synthetic Supplier", "Synthetic Office Supplies", "NEFT SYNTHETIC VENDOR",
                   client.headers["X-CSRF-Token"]):
        assert secret not in logged, secret
    assert reviewer.id != approver.id

"""Extraction service with a fake LLM. SYNTHETIC data only."""

import json

import pytest

from extraction_fakes import (
    BUYER_GSTIN, SUPPLIER_GSTIN, FakeLlm, field, invoice_pages, invoice_reply, statement_page1_reply,
    statement_page2_reply, statement_pages,
)
from tally_automation.db.models import DocumentType
from tally_automation.extraction.llm import LlmError
from tally_automation.extraction.parsing import Status
from tally_automation.extraction.prompts import PROMPT_VERSION
from tally_automation.extraction.schemas import llm_json_schema
from tally_automation.extraction.service import ExtractionError, PageInput, extract_document

INVOICE = DocumentType.PURCHASE_INVOICE
STATEMENT = DocumentType.BANK_STATEMENT


def run(pages, document_type, llm, pages_per_call=3):
    return extract_document(pages, document_type, llm, pages_per_call=pages_per_call, low_confidence=0.8)


def test_llm_schema_is_strict_everywhere() -> None:
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert sorted(node["required"]) == sorted(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    for document_type in DocumentType:
        walk(llm_json_schema(document_type))


def test_invoice_happy_path() -> None:
    llm = FakeLlm(invoice_reply())
    result = run(invoice_pages(), INVOICE, llm)
    h = result.header
    assert h["supplier_gstin"].value == SUPPLIER_GSTIN and h["supplier_gstin"].status is Status.VALID
    assert h["buyer_gstin"].value == BUYER_GSTIN
    assert (h["grand_total"].raw, h["grand_total"].value) == ("1,180.00", "1180.00")
    assert h["invoice_date"].value == "2026-04-01" and h["invoice_date"].method == "extracted"
    assert h["due_date"].status is Status.MISSING and h["due_date"].confidence is None
    assert h["total_igst"].status is Status.MISSING  # not printed separately: stays null, never summed
    [row] = result.rows
    assert row.fields["igst_rate"].value == "18" and row.fields["taxable_amount"].value == "1000.00"
    assert row.fields["discount"].status is Status.MISSING
    assert result.document_check_issues == []
    assert (result.calls, result.prompt_version, result.provider) == (1, PROMPT_VERSION, "fake")
    request = llm.requests[0]
    assert "=== Page 1 ===" in request["user"] and "never" in request["system"].lower()
    assert request["schema"]["properties"]["invoice"]["required"][0] == "supplier_name"


def test_value_not_in_document_is_invalid() -> None:
    reply = invoice_reply(header={"grand_total": field("1,280.00", "Grand Total 1,280.00")})
    result = run(invoice_pages(), INVOICE, FakeLlm(reply))
    total = result.header["grand_total"]
    assert total.status is Status.INVALID
    assert {"value_not_in_document", "evidence_not_found"} <= set(total.flags)
    assert total.value == "1280.00"  # parsed, but marked unusable; never silently replaced


def test_evidence_must_contain_value() -> None:
    reply = invoice_reply(header={"invoice_number": field("SYN-0001", "Synthetic Widget 8471")})
    number = run(invoice_pages(), INVOICE, FakeLlm(reply)).header["invoice_number"]
    assert number.status is Status.WARNING and "value_not_in_evidence" in number.flags


def test_low_confidence_flagged() -> None:
    reply = invoice_reply(header={"invoice_number": field("SYN-0001", "Invoice No: SYN-0001", confidence=0.5)})
    number = run(invoice_pages(), INVOICE, FakeLlm(reply)).header["invoice_number"]
    assert number.status is Status.WARNING and "low_confidence" in number.flags


def test_low_ocr_confidence_line_flagged() -> None:
    pages = [PageInput(1, invoice_pages()[0].text, from_ocr=True,
                       low_confidence_ocr_lines=("Grand Total 1,180.00",))]
    total = run(pages, INVOICE, FakeLlm(invoice_reply())).header["grand_total"]
    assert "low_ocr_confidence" in total.flags and total.status is Status.WARNING


def test_ambiguous_amount_kept_raw_and_invalid() -> None:
    pages = [PageInput(1, invoice_pages()[0].text.replace("Grand Total 1,180.00", "Grand Total 1.180.00"))]
    reply = invoice_reply(header={"grand_total": field("1.180.00", "Grand Total 1.180.00")})
    total = run(pages, INVOICE, FakeLlm(reply)).header["grand_total"]
    assert (total.raw, total.value, total.status) == ("1.180.00", None, Status.INVALID)
    assert "format_invalid" in total.flags


def test_required_missing_flagged() -> None:
    reply = invoice_reply(header={"invoice_number": field(None)})
    number = run(invoice_pages(), INVOICE, FakeLlm(reply)).header["invoice_number"]
    assert number.status is Status.MISSING and "required_missing" in number.flags


def test_wrong_document_type_reported() -> None:
    reply = invoice_reply(expected=False, reason="This is a bank statement.")
    result = run(invoice_pages(), INVOICE, FakeLlm(reply))
    assert result.document_check_issues == ["Pages 1-1: This is a bank statement."]


def test_statement_batches_are_merged_in_order() -> None:
    llm = FakeLlm(statement_page1_reply(), statement_page2_reply())
    result = run(statement_pages(), STATEMENT, llm, pages_per_call=1)
    assert result.calls == 2 and "page 2 of 2" in llm.requests[1]["user"]
    h = result.header
    assert h["account_number"].value == "123456789012"
    assert h["opening_balance"].value == "10000.00" and h["closing_balance"].value == "7500.00"
    assert h["closing_balance"].page == 2
    assert "conflict" not in h["bank_name"].flags  # same value on both pages
    debit, credit = result.rows
    assert (debit.fields["debit"].value, debit.fields["credit"].status) == ("5000.00", Status.MISSING)
    assert (credit.fields["credit"].value, credit.page) == ("2500.00", 2)
    assert credit.fields["txn_date"].value == "2026-04-05"


def test_conflicting_values_across_batches_flagged() -> None:
    page2 = json.loads(statement_page2_reply())
    page2["statement"]["ifsc"] = field("SYNB0009999", "SYNTHETIC BANK - page 2", page=2)
    pages = [PageInput(1, statement_pages()[0].text), PageInput(2, statement_pages()[1].text + "\nIFSC SYNB0009999")]
    result = run(pages, STATEMENT, FakeLlm(statement_page1_reply(), json.dumps(page2)), pages_per_call=1)
    ifsc = result.header["ifsc"]
    assert ifsc.value == "SYNB0001234" and ifsc.alternatives == ["SYNB0009999"]
    assert "conflict" in ifsc.flags and ifsc.status is Status.WARNING


def test_invalid_json_retried_once_then_accepted() -> None:
    llm = FakeLlm("{not json", invoice_reply())
    result = run(invoice_pages(), INVOICE, llm)
    assert result.calls == 2 and result.header["invoice_number"].value == "SYN-0001"


@pytest.mark.parametrize("bad", [
    "{not json",
    json.dumps({"document_check": {"is_expected_type": True, "reason": None}}),  # missing keys
    invoice_reply()[:-1] + ', "extra": 1}',  # unexpected key
    invoice_reply().replace('"confidence": 0.97', '"confidence": 7', 1),  # out of range
])
def test_invalid_replies_are_never_accepted(bad: str) -> None:
    with pytest.raises(ExtractionError, match="did not match the required schema"):
        run(invoice_pages(), INVOICE, FakeLlm(bad, bad))


def test_truncated_output_fails() -> None:
    with pytest.raises(ExtractionError, match="cut off"):
        run(invoice_pages(), INVOICE, FakeLlm(invoice_reply(), finish_reason="length"))


def test_llm_errors_fail_cleanly() -> None:
    with pytest.raises(ExtractionError, match="LLM request failed: synthetic outage"):
        run(invoice_pages(), INVOICE, FakeLlm(LlmError("synthetic outage", kind="transient")))


def test_no_text_fails() -> None:
    with pytest.raises(ExtractionError, match="no text"):
        run([PageInput(1, "   ")], INVOICE, FakeLlm())


def test_provider_schema_rejection_is_retried_once() -> None:
    rejected = LlmError("Groq rejected the reply as not matching the schema", kind="invalid_output")
    result = run(invoice_pages(), INVOICE, FakeLlm(rejected, invoice_reply()))
    assert result.calls == 2 and result.header["grand_total"].value == "1180.00"
    with pytest.raises(ExtractionError, match="did not match the required schema"):
        run(invoice_pages(), INVOICE, FakeLlm(rejected, rejected))


def test_rows_array_is_described_for_the_model() -> None:
    schema = llm_json_schema(INVOICE)
    assert schema["properties"]["line_items"]["description"].startswith("JSON array")
    assert "line item" in schema["properties"]["line_items"]["description"]


def empty_statement_reply() -> str:
    body = json.loads(statement_page1_reply())
    body["transactions"] = []
    return json.dumps(body)


def test_empty_transaction_list_is_retried_once_when_text_has_dated_amounts() -> None:
    llm = FakeLlm(empty_statement_reply(), statement_page1_reply())
    result = run(statement_pages()[:1], STATEMENT, llm)
    assert result.calls == 2 and len(result.rows) == 1
    assert "Note: this text has 1 line(s) containing both a date and an amount" in llm.requests[1]["user"]
    assert result.document_check_issues == []


def test_empty_transaction_list_twice_is_reported_not_retried_forever() -> None:
    llm = FakeLlm(empty_statement_reply(), empty_statement_reply())
    result = run(statement_pages()[:1], STATEMENT, llm)
    assert result.calls == 2 and result.rows == []
    assert result.document_check_issues == ["No transactions were found."]


def test_empty_retry_keeps_the_malformed_reply_retry() -> None:
    llm = FakeLlm(empty_statement_reply(), "{not json", statement_page1_reply())
    result = run(statement_pages()[:1], STATEMENT, llm)
    assert result.calls == 3 and len(result.rows) == 1


def test_statement_without_dated_amounts_is_not_retried() -> None:
    pages = [PageInput(1, "SYNTHETIC BANK - TEST STATEMENT - NOT REAL\nNo transactions in this period")]
    llm = FakeLlm(empty_statement_reply())
    result = run(pages, STATEMENT, llm)
    assert result.calls == 1 and result.rows == []


def test_fallback_model_use_is_reported_for_review() -> None:
    llm = FakeLlm(invoice_reply())
    llm.model = "openai/gpt-oss-120b"
    llm.reply_model = "openai/gpt-oss-20b"
    result = run(invoice_pages(), INVOICE, llm)
    assert result.model == "openai/gpt-oss-20b"
    assert any("fallback model openai/gpt-oss-20b" in issue for issue in result.document_check_issues)


def test_main_model_use_is_not_reported() -> None:
    result = run(invoice_pages(), INVOICE, FakeLlm(invoice_reply()))
    assert result.model == "fake-model" and result.document_check_issues == []

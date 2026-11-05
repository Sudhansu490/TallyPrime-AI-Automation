"""Structured extraction: LLM transcription → schema validation → parsing → evidence checks.

Safeguards, in order:

1. The LLM answers under a strict JSON schema; the reply is then validated
   again with Pydantic. Invalid JSON or a schema violation is retried once and
   otherwise fails the document. Nothing unvalidated is ever stored as data.
2. Truncated output (token limit) fails instead of silently losing rows.
3. Every value is parsed in code (``parsing``); ambiguous text stays unparsed.
4. Every value and its evidence quote must actually occur in the document
   text. A value that cannot be found there is marked invalid, because it may
   have been invented.
5. Self-reported low confidence, low OCR confidence on the quoted line, and
   disagreement between page batches are flagged for the reviewer.

The LLM never sees ledgers, never decides mappings and never posts anything.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from tally_automation.db.models import DocumentType
from tally_automation.extraction.llm import LlmClient, LlmError
from tally_automation.extraction.parsing import Parsed, Status, ValueType, parse_value
from tally_automation.extraction.prompts import PROMPT_VERSION, system_prompt, user_message
from tally_automation.extraction.schemas import SCHEMAS, FieldSpec, llm_json_schema, response_model

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = {
    DocumentType.PURCHASE_INVOICE: ("supplier_name", "invoice_number", "invoice_date", "grand_total"),
    DocumentType.BANK_STATEMENT: ("account_number",),
}
_SEVERITY = {Status.VALID: 0, Status.MISSING: 0, Status.WARNING: 1, Status.INVALID: 2}


class ExtractionError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class PageInput:
    page_number: int
    text: str
    from_ocr: bool = False
    low_confidence_ocr_lines: tuple[str, ...] = ()


@dataclass
class FieldResult:
    """One field: the model's transcription plus everything checked about it."""

    name: str
    value_type: ValueType
    raw: str | None
    value: str | None
    status: Status
    method: str = "extracted"               # extracted | calculated | edited
    flags: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    evidence: str | None = None
    page: int | None = None
    confidence: float | None = None
    alternatives: list[str] = field(default_factory=list)

    def to_meta(self) -> dict[str, Any]:
        return {
            "type": self.value_type.value, "raw": self.raw, "value": self.value, "status": self.status.value,
            "method": self.method, "flags": self.flags, "messages": self.messages, "evidence": self.evidence,
            "page": self.page, "confidence": self.confidence, "alternatives": self.alternatives,
        }


@dataclass
class RowResult:
    row_text: str | None
    page: int | None
    confidence: float
    fields: dict[str, FieldResult]


@dataclass
class ExtractionResult:
    document_type: DocumentType
    header: dict[str, FieldResult]
    rows: list[RowResult]
    document_check_issues: list[str]
    provider: str
    model: str
    prompt_version: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    raw_responses: list[str]


# ---- Text matching -------------------------------------------------------------------

def _alnum(text: str) -> str:
    """Letters and digits only, lower-case: robust to line breaks and spacing,
    still exact about every character that carries meaning."""
    return re.sub(r"[^0-9a-z]", "", text.lower())


class _DocumentText:
    def __init__(self, pages: list[PageInput]) -> None:
        self.pages = {p.page_number: p for p in pages}
        self.all = _alnum("\n".join(p.text for p in pages))

    def contains(self, text: str) -> bool:
        needle = _alnum(text)
        return bool(needle) and needle in self.all

    def low_ocr_overlap(self, page: int | None, quote: str | None) -> bool:
        if page is None or quote is None or page not in self.pages:
            return False
        quote_key = _alnum(quote)
        for line in self.pages[page].low_confidence_ocr_lines:
            line_key = _alnum(line)
            if len(line_key) >= 3 and (line_key in quote_key or (len(quote_key) >= 3 and quote_key in line_key)):
                return True
        return False


# ---- Field checks ---------------------------------------------------------------------

def _raise_status(result: FieldResult, status: Status) -> None:
    if _SEVERITY[status] > _SEVERITY[result.status]:
        result.status = status


def build_field(
    spec: FieldSpec, raw: str | None, *, evidence: str | None, page: int | None, confidence: float | None,
    document: _DocumentText, low_confidence: float, evidence_is_row: bool = False,
) -> FieldResult:
    raw = raw.strip() if isinstance(raw, str) and raw.strip() else None
    parsed: Parsed = parse_value(raw, spec.value_type)
    result = FieldResult(
        name=spec.name, value_type=spec.value_type, raw=raw, value=parsed.value, status=parsed.status,
        messages=list(parsed.messages), evidence=evidence, page=page, confidence=confidence,
    )
    if parsed.status is Status.INVALID:
        result.flags.append("format_invalid")
    if raw is None:
        return result

    if not document.contains(raw):
        result.flags.append("value_not_in_document")
        result.messages.append("This value does not appear in the document text; it may have been invented.")
        _raise_status(result, Status.INVALID)
    if evidence is None:
        result.flags.append("no_evidence")
        _raise_status(result, Status.WARNING)
    else:
        if not document.contains(evidence):
            result.flags.append("evidence_not_found")
            result.messages.append("The quoted evidence was not found in the document text.")
            _raise_status(result, Status.WARNING)
        if _alnum(raw) not in _alnum(evidence):
            result.flags.append("value_not_in_evidence")
            label = "row" if evidence_is_row else "quoted evidence"
            result.messages.append(f"The value is not part of its {label}.")
            _raise_status(result, Status.WARNING)
    if confidence is not None and confidence < low_confidence:
        result.flags.append("low_confidence")
        result.messages.append(f"Model confidence {confidence:.0%}.")
        _raise_status(result, Status.WARNING)
    if document.low_ocr_overlap(page, evidence):
        result.flags.append("low_ocr_confidence")
        result.messages.append("The source text came from an OCR line with low confidence.")
        _raise_status(result, Status.WARNING)
    return result


# ---- Orchestration --------------------------------------------------------------------

def _batches(pages: list[PageInput], size: int) -> list[list[PageInput]]:
    usable = [p for p in pages if p.text.strip()]
    return [usable[i:i + size] for i in range(0, len(usable), size)]


def _call(
    client: LlmClient, document_type: DocumentType, batch: list[PageInput], total_pages: int,
    model_cls: type[BaseModel], schema: dict[str, Any], record: dict[str, Any],
) -> BaseModel:
    system = system_prompt(document_type)
    user = user_message([(p.page_number, p.text) for p in batch], total_pages)
    last_problem = ""
    for _ in range(2):  # one retry for a malformed reply; transport retries live in the client
        try:
            response = client.complete_json(system, user, f"{document_type.value}_extraction", schema)
        except LlmError as exc:
            if exc.kind == "invalid_output":
                # The provider itself found the reply off-schema: same as a malformed reply.
                record["calls"] += 1
                last_problem = exc.message
                logger.warning("LLM reply rejected by provider schema check")
                continue
            raise ExtractionError(f"LLM request failed: {exc.message}") from exc
        record["calls"] += 1
        record["prompt_tokens"] += response.prompt_tokens
        record["completion_tokens"] += response.completion_tokens
        record["raw"].append(response.content)
        record["model"] = response.model
        if response.finish_reason == "length":
            raise ExtractionError(
                "The LLM output was cut off at the token limit, so rows could be missing. "
                "Lower LLM_PAGES_PER_CALL or raise LLM_MAX_OUTPUT_TOKENS."
            )
        try:
            return model_cls.model_validate_json(response.content)
        except ValidationError as exc:
            last_problem = f"{exc.error_count()} schema error(s), first: {exc.errors()[0]['msg']}"
            logger.warning("LLM reply failed validation", extra={"problem": last_problem})
    raise ExtractionError(f"The LLM reply did not match the required schema ({last_problem}).")


def extract_document(
    pages: list[PageInput], document_type: DocumentType, client: LlmClient,
    *, pages_per_call: int, low_confidence: float,
) -> ExtractionResult:
    schema_def = SCHEMAS[document_type]
    model_cls = response_model(document_type)
    json_schema = llm_json_schema(document_type)
    document = _DocumentText(pages)
    batches = _batches(pages, pages_per_call)
    if not batches:
        raise ExtractionError("There is no text to extract from.")

    started = time.perf_counter()
    record: dict[str, Any] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "raw": [], "model": client.model}
    replies = [
        _call(client, document_type, batch, len(pages), model_cls, json_schema, record) for batch in batches
    ]

    issues = [
        f"Pages {batch[0].page_number}-{batch[-1].page_number}: {reply.document_check.reason or 'not the expected document type'}"
        for batch, reply in zip(batches, replies) if not reply.document_check.is_expected_type
    ]

    header: dict[str, FieldResult] = {}
    for spec in schema_def.header_fields:
        candidates = [
            getattr(getattr(reply, schema_def.header_key), spec.name) for reply in replies
        ]
        present = [c for c in candidates if c.value is not None and c.value.strip()]
        chosen = present[0] if present else candidates[0]
        result = build_field(
            spec, chosen.value, evidence=chosen.evidence, page=chosen.page,
            confidence=chosen.confidence if present else None,
            document=document, low_confidence=low_confidence,
        )
        others = {
            c.value.strip() for c in present[1:]
            if parse_value(c.value, spec.value_type).value != result.value
        }
        if others:
            result.alternatives = sorted(others)
            result.flags.append("conflict")
            result.messages.append("Different pages show different values: " + ", ".join(result.alternatives) + ".")
            _raise_status(result, Status.WARNING)
        if spec.name in REQUIRED_FIELDS[document_type] and result.raw is None:
            result.flags.append("required_missing")
            result.messages.append("Required field not found in the document.")
        header[spec.name] = result

    rows: list[RowResult] = []
    for reply in replies:
        for row in getattr(reply, schema_def.rows_key):
            data = row.model_dump()
            rows.append(RowResult(
                row_text=row.row_text, page=row.page, confidence=row.confidence,
                fields={
                    spec.name: build_field(
                        spec, data[spec.name], evidence=row.row_text, page=row.page,
                        confidence=row.confidence, document=document, low_confidence=low_confidence,
                        evidence_is_row=True,
                    )
                    for spec in schema_def.row_fields
                },
            ))

    if document_type is DocumentType.BANK_STATEMENT and not rows:
        issues.append("No transactions were found.")

    return ExtractionResult(
        document_type=document_type, header=header, rows=rows, document_check_issues=issues,
        provider=client.provider, model=record["model"], prompt_version=PROMPT_VERSION,
        calls=record["calls"], prompt_tokens=record["prompt_tokens"],
        completion_tokens=record["completion_tokens"], seconds=round(time.perf_counter() - started, 2),
        raw_responses=record["raw"],
    )

"""Field catalogues and the JSON schema the LLM must answer with.

The catalogue is the single source of truth: it drives the LLM schema, the
Pydantic validation of the response, parsing, the database columns and the
review page labels.

Header fields are returned as ``{value, evidence, page, confidence}`` objects.
Table rows (line items, bank transactions) carry one ``row_text`` quote,
``page`` and ``confidence`` per row plus plain string values, which keeps the
output small enough for statements with hundreds of transactions.

Every value is a *string as printed* (or null). The model never converts
numbers or dates; ``parsing`` does, deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from tally_automation.db.models import DocumentType
from tally_automation.extraction.parsing import ValueType as T


@dataclass(frozen=True)
class FieldSpec:
    name: str
    value_type: T
    label: str
    hint: str = ""            # extra instruction for the model, kept short


INVOICE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("supplier_name", T.TEXT, "Supplier name", "seller / 'from' party"),
    FieldSpec("supplier_gstin", T.GSTIN, "Supplier GSTIN"),
    FieldSpec("supplier_address", T.TEXT, "Supplier address"),
    FieldSpec("buyer_name", T.TEXT, "Buyer name", "'bill to' party"),
    FieldSpec("buyer_gstin", T.GSTIN, "Buyer GSTIN"),
    FieldSpec("invoice_number", T.TEXT, "Invoice number"),
    FieldSpec("invoice_date", T.DATE, "Invoice date"),
    FieldSpec("due_date", T.DATE, "Due date"),
    FieldSpec("purchase_order_number", T.TEXT, "PO number"),
    FieldSpec("place_of_supply", T.TEXT, "Place of supply"),
    FieldSpec("state", T.TEXT, "State", "state named for place of supply or buyer"),
    FieldSpec("state_code", T.STATE_CODE, "State code", "2-digit GST state code printed on the invoice"),
    FieldSpec("invoice_type", T.TEXT, "Invoice type", "e.g. 'Tax Invoice', 'Bill of Supply', as printed"),
    FieldSpec("subtotal", T.AMOUNT, "Subtotal"),
    FieldSpec("total_discount", T.AMOUNT, "Total discount"),
    FieldSpec("total_taxable_value", T.AMOUNT, "Total taxable value"),
    FieldSpec("total_cgst", T.AMOUNT, "Total CGST"),
    FieldSpec("total_sgst", T.AMOUNT, "Total SGST/UTGST"),
    FieldSpec("total_igst", T.AMOUNT, "Total IGST"),
    FieldSpec("total_cess", T.AMOUNT, "Total cess"),
    FieldSpec("round_off", T.AMOUNT, "Round-off", "keep the sign as printed"),
    FieldSpec("grand_total", T.AMOUNT, "Grand total"),
    FieldSpec("amount_payable", T.AMOUNT, "Amount payable", "balance due, if printed separately"),
    FieldSpec("amount_in_words", T.TEXT, "Amount in words"),
)

LINE_ITEM_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("description", T.TEXT, "Description"),
    FieldSpec("hsn_sac", T.HSN_SAC, "HSN/SAC"),
    FieldSpec("quantity", T.QUANTITY, "Qty", "number only"),
    FieldSpec("unit", T.TEXT, "Unit", "e.g. Nos, Kg"),
    FieldSpec("rate", T.AMOUNT, "Rate", "price per unit"),
    FieldSpec("discount", T.AMOUNT, "Discount", "amount, not percentage"),
    FieldSpec("taxable_amount", T.AMOUNT, "Taxable"),
    FieldSpec("cgst_rate", T.PERCENT, "CGST %"),
    FieldSpec("cgst_amount", T.AMOUNT, "CGST"),
    FieldSpec("sgst_rate", T.PERCENT, "SGST %"),
    FieldSpec("sgst_amount", T.AMOUNT, "SGST"),
    FieldSpec("igst_rate", T.PERCENT, "IGST %"),
    FieldSpec("igst_amount", T.AMOUNT, "IGST"),
    FieldSpec("cess_amount", T.AMOUNT, "Cess"),
    FieldSpec("line_total", T.AMOUNT, "Line total"),
)

STATEMENT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("bank_name", T.TEXT, "Bank"),
    FieldSpec("account_holder", T.TEXT, "Account holder"),
    FieldSpec("account_number", T.ACCOUNT_NUMBER, "Account number"),
    FieldSpec("ifsc", T.IFSC, "IFSC"),
    FieldSpec("period_from", T.DATE, "Period from"),
    FieldSpec("period_to", T.DATE, "Period to"),
    FieldSpec("opening_balance", T.BALANCE, "Opening balance", "include any Dr/Cr marker"),
    FieldSpec("closing_balance", T.BALANCE, "Closing balance", "include any Dr/Cr marker"),
)

TRANSACTION_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("txn_date", T.DATE, "Date", "transaction / posting date"),
    FieldSpec("value_date", T.DATE, "Value date"),
    FieldSpec("description", T.TEXT, "Description", "complete narration, exactly as printed"),
    FieldSpec("reference_number", T.TEXT, "Reference", "UTR / ref no."),
    FieldSpec("cheque_number", T.TEXT, "Cheque no."),
    FieldSpec("debit", T.AMOUNT, "Debit", "withdrawal amount"),
    FieldSpec("credit", T.AMOUNT, "Credit", "deposit amount"),
    FieldSpec("balance", T.BALANCE, "Balance", "include any Dr/Cr marker"),
)


@dataclass(frozen=True)
class DocumentSchema:
    document_type: DocumentType
    header_key: str
    rows_key: str
    header_fields: tuple[FieldSpec, ...]
    row_fields: tuple[FieldSpec, ...]


SCHEMAS = {
    DocumentType.PURCHASE_INVOICE: DocumentSchema(
        DocumentType.PURCHASE_INVOICE, "invoice", "line_items", INVOICE_FIELDS, LINE_ITEM_FIELDS,
    ),
    DocumentType.BANK_STATEMENT: DocumentSchema(
        DocumentType.BANK_STATEMENT, "statement", "transactions", STATEMENT_FIELDS, TRANSACTION_FIELDS,
    ),
}


# ---- Response models (validation) ---------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FieldOut(_Strict):
    value: str | None
    evidence: str | None
    page: int | None
    confidence: float = Field(ge=0, le=1)


class DocumentCheck(_Strict):
    is_expected_type: bool
    reason: str | None


def _header_model(schema: DocumentSchema) -> type[BaseModel]:
    return create_model(  # type: ignore[call-overload]
        f"{schema.header_key.title()}Header", __base__=_Strict,
        **{spec.name: (FieldOut, ...) for spec in schema.header_fields},
    )


def _row_model(schema: DocumentSchema) -> type[BaseModel]:
    return create_model(  # type: ignore[call-overload]
        f"{schema.rows_key.title().replace('_', '')}Row", __base__=_Strict,
        row_text=(str | None, ...), page=(int | None, ...), confidence=(float, Field(ge=0, le=1)),
        **{spec.name: (str | None, ...) for spec in schema.row_fields},
    )


def response_model(document_type: DocumentType) -> type[BaseModel]:
    schema = SCHEMAS[document_type]
    return create_model(  # type: ignore[call-overload]
        f"{schema.header_key.title()}Extraction", __base__=_Strict,
        document_check=(DocumentCheck, ...),
        **{schema.header_key: (_header_model(schema), ...), schema.rows_key: (list[_row_model(schema)], ...)},
    )


# ---- JSON schema for the LLM (strict mode) -------------------------------------------

def _nullable(json_type: str, description: str = "") -> dict[str, Any]:
    node: dict[str, Any] = {"anyOf": [{"type": json_type}, {"type": "null"}]}
    if description:
        node["description"] = description
    return node


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    # Strict mode: every property required, nothing extra.
    return {"type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties}


def _describe(spec: FieldSpec) -> str:
    return f"{spec.label}" + (f" ({spec.hint})" if spec.hint else "")


def llm_json_schema(document_type: DocumentType) -> dict[str, Any]:
    schema = SCHEMAS[document_type]
    field_def = _object({
        "value": _nullable("string", "exactly as printed; null if not in the document"),
        "evidence": _nullable("string", "verbatim quote from the document containing the value"),
        "page": _nullable("integer", "page number the evidence is on"),
        "confidence": {"type": "number", "description": "0-1: how sure the transcription is right"},
    })
    header = _object({
        spec.name: {"$ref": "#/$defs/field", "description": _describe(spec)} for spec in schema.header_fields
    })
    row = _object({
        "row_text": _nullable("string", "the whole row, verbatim, as one quote"),
        "page": _nullable("integer"),
        "confidence": {"type": "number"},
        **{spec.name: _nullable("string", _describe(spec)) for spec in schema.row_fields},
    })
    root = _object({
        "document_check": _object({
            "is_expected_type": {"type": "boolean"},
            "reason": _nullable("string", "why not, if is_expected_type is false"),
        }),
        schema.header_key: header,
        # The description matters: without it the model was seen answering
        # {"items": [...]} (copying the schema keyword) instead of a plain array.
        schema.rows_key: {
            "type": "array", "items": row,
            "description": f"JSON array of row objects, one per {schema.rows_key[:-1].replace('_', ' ')} row",
        },
    })
    root["$defs"] = {"field": field_def}
    return root

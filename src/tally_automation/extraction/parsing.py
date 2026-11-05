"""Deterministic parsing and format checks for extracted values.

The LLM only transcribes text as printed. Turning that text into an amount,
a date or a GSTIN happens here, in plain code, with explicit rules:

* nothing ambiguous is guessed: "1.180.00" or "1.180,00" is rejected, not
  "corrected" to 1180.00;
* every interpretation that is an assumption (e.g. day-first dates, two-digit
  years, "Dr" meaning negative) is reported in the message;
* invalid values are kept as raw text for the reviewer and never dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class ValueType(StrEnum):
    TEXT = "text"
    AMOUNT = "amount"          # money, 2+ decimals kept exactly
    BALANCE = "balance"        # running/opening/closing balance: may carry Dr/Cr
    QUANTITY = "quantity"
    PERCENT = "percent"
    DATE = "date"
    GSTIN = "gstin"
    STATE_CODE = "state_code"
    HSN_SAC = "hsn_sac"
    IFSC = "ifsc"
    ACCOUNT_NUMBER = "account_number"


class Status(StrEnum):
    VALID = "valid"
    WARNING = "warning"        # usable, but a person should look at it
    INVALID = "invalid"        # cannot be used as is
    MISSING = "missing"        # not present in the document (null)


@dataclass
class Parsed:
    value: str | None                      # canonical text: Decimal str, ISO date, upper-case code
    status: Status
    messages: list[str] = field(default_factory=list)


# ---- Numbers -------------------------------------------------------------------

_CURRENCY = re.compile(r"^(₹|rs\.?|inr)\s*|\s*(₹|rs\.?|inr)$", re.IGNORECASE)
_DR_CR = re.compile(r"\s*\b(dr|cr)\.?$", re.IGNORECASE)
_PLAIN_NUMBER = re.compile(r"^\d+(\.\d+)?$")
_GROUPED_NUMBER = re.compile(r"^\d{1,3}(,\d{2,3})+(\.\d+)?$")


def _valid_grouping(integer_part: str) -> bool:
    groups = integer_part.split(",")
    head, rest = groups[0], groups[1:]
    if not 1 <= len(head) <= 3:
        return False
    western = all(len(g) == 3 for g in rest)
    # Indian: last group of 3, the others of 2 (1,23,45,678).
    indian = len(rest[-1]) == 3 and all(len(g) == 2 for g in rest[:-1]) and len(head) <= 2
    return western or indian


def parse_decimal(raw: str, *, allow_dr_cr: bool = False) -> Parsed:
    text = raw.strip().replace("−", "-")  # Unicode minus
    messages: list[str] = []
    negative = False

    dr_cr = _DR_CR.search(text)
    if dr_cr:
        if not allow_dr_cr:
            return Parsed(None, Status.INVALID, [f"Unexpected '{dr_cr.group(1)}' suffix in an amount."])
        text = text[: dr_cr.start()].strip()
        if dr_cr.group(1).lower() == "dr":
            negative = True
            messages.append("'Dr' read as a debit (negative) balance.")
    text = _CURRENCY.sub("", text).strip()
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1].strip()
        messages.append("Parentheses read as a negative amount.")
    if text.startswith("-"):
        negative, text = True, text[1:].strip()
    elif text.startswith("+"):
        text = text[1:].strip()
    text = text.replace(" ", "")

    if _PLAIN_NUMBER.match(text):
        pass
    elif _GROUPED_NUMBER.match(text) and _valid_grouping(text.split(".")[0]):
        text = text.replace(",", "")
    else:
        return Parsed(None, Status.INVALID, [f"'{raw}' is not an unambiguous number; it was not interpreted."])
    try:
        number = Decimal(text)
    except InvalidOperation:
        return Parsed(None, Status.INVALID, [f"'{raw}' is not a number."])
    if negative:
        number = -number
    return Parsed(str(number), Status.VALID, messages)


def parse_amount(raw: str, *, allow_dr_cr: bool = False) -> Parsed:
    if "%" in raw:
        return Parsed(None, Status.INVALID, ["A percentage was given where an amount is expected."])
    return parse_decimal(raw, allow_dr_cr=allow_dr_cr)


def parse_percent(raw: str) -> Parsed:
    parsed = parse_decimal(raw.replace("%", "").strip())
    if parsed.value is not None and not Decimal(0) <= Decimal(parsed.value) <= Decimal(100):
        return Parsed(parsed.value, Status.INVALID, ["A rate must be between 0 and 100 %."])
    return parsed


def parse_quantity(raw: str) -> Parsed:
    parsed = parse_decimal(raw)
    if parsed.value is not None and Decimal(parsed.value) < 0:
        parsed.status, parsed.messages = Status.WARNING, [*parsed.messages, "Negative quantity."]
    return parsed


# ---- Dates ---------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_NUMERIC_DATE = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2}|\d{4})$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_NAMED_DMY = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]*([a-z]{3,9})\.?[\s\-/.,]*(\d{2}|\d{4})$", re.IGNORECASE)
_NAMED_MDY = re.compile(r"^([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$", re.IGNORECASE)


def _year(text: str, messages: list[str]) -> int:
    if len(text) == 2:
        messages.append(f"Two-digit year '{text}' read as 20{text}.")
        return 2000 + int(text)
    return int(text)


def _month(name: str) -> int | None:
    return _MONTHS.get(name[:3].lower())


def parse_date(raw: str) -> Parsed:
    text = raw.strip()
    messages: list[str] = []
    try:
        if match := _ISO_DATE.match(text):
            value = date(int(match[1]), int(match[2]), int(match[3]))
        elif match := _NUMERIC_DATE.match(text):
            day, month = int(match[1]), int(match[2])
            value = date(_year(match[3], messages), month, day)
            if day <= 12 and month <= 12 and day != month:
                messages.append("Read as day/month/year (Indian format).")
        elif match := _NAMED_DMY.match(text):
            month = _month(match[2])
            if month is None:
                raise ValueError
            value = date(_year(match[3], messages), month, int(match[1]))
        elif match := _NAMED_MDY.match(text):
            month = _month(match[1])
            if month is None:
                raise ValueError
            value = date(int(match[3]), month, int(match[2]))
        else:
            return Parsed(None, Status.INVALID, [f"'{raw}' is not a recognised date format."])
    except ValueError:
        return Parsed(None, Status.INVALID, [f"'{raw}' is not a valid calendar date."])
    status = Status.VALID
    if not 2000 <= value.year <= datetime.now().year + 1:
        status = Status.WARNING
        messages.append(f"Unusual year {value.year}.")
    return Parsed(value.isoformat(), status, messages)


# ---- Indian identifiers -------------------------------------------------------

# GST state codes (CBIC). 97 = Other Territory, 99 = Centre Jurisdiction.
VALID_STATE_CODES = frozenset({f"{n:02d}" for n in range(1, 39)} | {"97", "99"})
_GSTIN = re.compile(r"^(\d{2})([A-Z]{5}\d{4}[A-Z])([1-9A-Z])Z([0-9A-Z])$")
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_checksum(first14: str) -> str:
    """GSTIN check character (mod-36 Luhn variant used by GSTN)."""
    total = 0
    for index, char in enumerate(first14):
        product = _GSTIN_CHARS.index(char) * (2 if index % 2 else 1)
        total += product // 36 + product % 36
    return _GSTIN_CHARS[(36 - total % 36) % 36]


def parse_gstin(raw: str) -> Parsed:
    text = re.sub(r"\s", "", raw).upper()
    match = _GSTIN.match(text)
    if not match:
        return Parsed(text or None, Status.INVALID, ["Not a 15-character GSTIN (state code, PAN, entity, 'Z', check)."])
    if match[1] not in VALID_STATE_CODES:
        return Parsed(text, Status.INVALID, [f"'{match[1]}' is not a GST state code."])
    expected = gstin_checksum(text[:14])
    if text[14] != expected:
        return Parsed(text, Status.INVALID, ["GSTIN check character does not match; possibly misread."])
    return Parsed(text, Status.VALID)


def parse_state_code(raw: str) -> Parsed:
    text = raw.strip()
    if text.isdigit() and len(text) == 1:
        text = "0" + text
    if text not in VALID_STATE_CODES:
        return Parsed(text or None, Status.INVALID, [f"'{raw}' is not a GST state code."])
    return Parsed(text, Status.VALID)


def parse_hsn_sac(raw: str) -> Parsed:
    text = re.sub(r"\s", "", raw)
    if not text.isdigit():
        return Parsed(text, Status.INVALID, ["HSN/SAC codes contain digits only."])
    if len(text) not in (4, 6, 8):
        return Parsed(text, Status.WARNING, [f"HSN/SAC codes are usually 4, 6 or 8 digits (got {len(text)})."])
    return Parsed(text, Status.VALID)


_IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def parse_ifsc(raw: str) -> Parsed:
    text = re.sub(r"\s", "", raw).upper()
    if not _IFSC.match(text):
        return Parsed(text or None, Status.INVALID, ["IFSC is 4 letters, '0', then 6 letters/digits."])
    return Parsed(text, Status.VALID)


def parse_account_number(raw: str) -> Parsed:
    text = re.sub(r"[\s\-]", "", raw).upper()
    if re.fullmatch(r"[X*]+\d{3,}", text):
        return Parsed(text, Status.WARNING, ["The statement shows this account number partly masked."])
    if not re.fullmatch(r"\d{6,20}", text):
        return Parsed(text or None, Status.INVALID, ["An account number is 6–20 digits."])
    return Parsed(text, Status.VALID)


def mask_account_number(value: str | None) -> str | None:
    if not value:
        return value
    compact = re.sub(r"\s", "", value)
    return "X" * max(len(compact) - 4, 0) + compact[-4:]


def parse_value(raw: str | None, value_type: ValueType) -> Parsed:
    if raw is None or not raw.strip():
        return Parsed(None, Status.MISSING)
    match value_type:
        case ValueType.TEXT:
            return Parsed(raw.strip(), Status.VALID)
        case ValueType.AMOUNT:
            return parse_amount(raw)
        case ValueType.BALANCE:
            return parse_amount(raw, allow_dr_cr=True)
        case ValueType.QUANTITY:
            return parse_quantity(raw)
        case ValueType.PERCENT:
            return parse_percent(raw)
        case ValueType.DATE:
            return parse_date(raw)
        case ValueType.GSTIN:
            return parse_gstin(raw)
        case ValueType.STATE_CODE:
            return parse_state_code(raw)
        case ValueType.HSN_SAC:
            return parse_hsn_sac(raw)
        case ValueType.IFSC:
            return parse_ifsc(raw)
        case ValueType.ACCOUNT_NUMBER:
            return parse_account_number(raw)
    raise ValueError(f"unknown value type {value_type}")

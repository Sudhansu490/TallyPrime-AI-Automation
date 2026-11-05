"""Masking of bank account numbers in API output.

Full account numbers stay in the database (needed for matching); every API
response shows them masked, including where they appear inside free text
such as evidence quotes, printed rows or transaction descriptions.
"""

from __future__ import annotations

import re
from typing import Any

from tally_automation.db.models import BankStatement
from tally_automation.extraction.parsing import mask_account_number


def mask_in(text: str | None, secrets: tuple[str, ...]) -> str | None:
    """Mask every occurrence of any of the account numbers in free text."""
    if not text:
        return text
    for secret in secrets:
        digits = re.sub(r"\D", "", secret)
        if len(digits) < 5:
            continue
        # Allow spaces or hyphens between the digits as printed.
        pattern = r"[\s\-]?".join(map(re.escape, digits))
        text = re.sub(pattern, mask_account_number(digits) or "", text)
    return text


def account_secrets(fields: dict[str, Any] | None) -> tuple[str, ...]:
    """Every account number value a statement's fields hold (current and original)."""
    meta = (fields or {}).get("account_number") or {}
    candidates = [meta.get("raw"), meta.get("value"), *(meta.get("alternatives") or [])]
    if meta.get("original"):
        candidates += [meta["original"].get("raw"), meta["original"].get("value")]
    return tuple(dict.fromkeys(c for c in candidates if c))


def statement_secrets(statement: BankStatement) -> tuple[str, ...]:
    extra = (statement.account_number,) if statement.account_number else ()
    return tuple(dict.fromkeys(account_secrets(statement.fields) + extra))

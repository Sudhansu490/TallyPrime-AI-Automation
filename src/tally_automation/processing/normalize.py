"""Text normalisation for extracted and OCR'd text.

Only *layout* is normalised: Unicode form, whitespace, line endings and
invisible control characters. Characters that carry meaning (digits, ₹, %,
punctuation, case) are never changed, removed or "corrected". The raw text is
always stored alongside, so normalisation can be audited.
"""

from __future__ import annotations

import re
import unicodedata

# Horizontal whitespace variants (NBSP, thin space, ideographic space, tabs ...).
_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")
_MANY_BLANK_LINES = re.compile(r"\n{3,}")
# Zero-width characters and the BOM: invisible, and they break matching.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


def normalize_text(raw: str) -> str:
    """Return ``raw`` with consistent Unicode, whitespace and line breaks."""
    # NFKC folds compatibility forms (full-width digits, ligatures like "ﬁ",
    # NBSP) into their plain equivalents. It keeps "₹" and all digits.
    text = unicodedata.normalize("NFKC", raw).translate(_INVISIBLE)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    text = "".join(
        ch for ch in text
        if ch == "\n" or ch == "\t" or not unicodedata.category(ch).startswith("C")
    )
    lines = [_HORIZONTAL_SPACE.sub(" ", line).strip() for line in text.split("\n")]
    return _MANY_BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def meaningful_char_count(text: str) -> int:
    """Number of letters and digits; used to decide whether text is usable."""
    return sum(1 for ch in text if ch.isalnum())

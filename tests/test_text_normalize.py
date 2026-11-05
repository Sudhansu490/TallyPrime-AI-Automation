from tally_automation.processing.normalize import meaningful_char_count, normalize_text


def test_whitespace_and_line_endings_are_normalised() -> None:
    raw = "  Invoice\u00a0No:\tSYN-1  \r\n\r\n\r\n\r\nTotal   1,180.00 \f next"
    assert normalize_text(raw) == "Invoice No: SYN-1\n\nTotal 1,180.00\nnext"


def test_meaningful_characters_are_never_changed() -> None:
    raw = "₹1,23,456.70 CGST 9% (Rs.) GSTIN 27AAAAA0000A1Z5 – o/s"
    assert normalize_text(raw) == raw


def test_compatibility_forms_are_folded() -> None:
    # Full-width digits and the "fi" ligature become their plain forms.
    assert normalize_text("１２３ ﬁle") == "123 file"


def test_invisible_and_control_characters_removed() -> None:
    assert normalize_text("\ufeffGST\u200bIN\x00 27") == "GSTIN 27"


def test_meaningful_char_count_counts_letters_and_digits() -> None:
    assert meaningful_char_count("₹ 1,180.00 - Total") == 11
    assert meaningful_char_count(" \n.,- ") == 0

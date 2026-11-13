"""Deterministic parsing of transcribed values. Nothing ambiguous may be guessed."""

import pytest

from tally_automation.extraction.parsing import (
    Status, ValueType, gstin_checksum, mask_account_number, parse_amount, parse_date, parse_gstin,
    parse_ifsc, parse_value,
)

SYNTHETIC_GSTIN = "27AAAAA0000A1Z" + gstin_checksum("27AAAAA0000A1Z")


@pytest.mark.parametrize(("raw", "expected"), [
    ("1180.00", "1180.00"), ("1,180.00", "1180.00"), ("1,23,456.70", "123456.70"),
    ("123,456.70", "123456.70"), ("₹ 1,180.00", "1180.00"), ("Rs. 99", "99"), ("INR 5.5", "5.5"),
    ("-0.40", "-0.40"), ("(12.00)", "-12.00"), ("0.10", "0.10"),
])
def test_amounts_parse_exactly(raw: str, expected: str) -> None:
    parsed = parse_amount(raw)
    assert (parsed.value, parsed.status) == (expected, Status.VALID)


@pytest.mark.parametrize("raw", ["1.180.00", "1.180,00", "1,18,0.00", "12,34", "abc", "1,180.00 Dr", "10%"])
def test_ambiguous_or_wrong_amounts_are_not_interpreted(raw: str) -> None:
    parsed = parse_amount(raw)
    assert parsed.value is None and parsed.status is Status.INVALID


def test_balances_accept_dr_cr_and_say_so() -> None:
    debit = parse_value("1,000.00 Dr", ValueType.BALANCE)
    assert debit.value == "-1000.00" and "negative" in debit.messages[0]
    assert parse_value("1,000.00 Cr", ValueType.BALANCE).value == "1000.00"


@pytest.mark.parametrize(("raw", "expected"), [
    ("01/04/2026", "2026-04-01"), ("1-4-2026", "2026-04-01"), ("2026-04-01", "2026-04-01"),
    ("01-Apr-2026", "2026-04-01"), ("1st April 2026", "2026-04-01"), ("April 1, 2026", "2026-04-01"),
    ("13.04.26", "2026-04-13"),
])
def test_dates(raw: str, expected: str) -> None:
    assert parse_date(raw).value == expected


def test_day_first_assumption_is_reported() -> None:
    parsed = parse_date("01/04/2026")
    assert "day/month/year" in " ".join(parsed.messages)
    assert parse_date("13/04/2026").messages == []  # unambiguous


@pytest.mark.parametrize("raw", ["31/02/2026", "2026/13/01", "next tuesday", "04/2026"])
def test_invalid_dates(raw: str) -> None:
    assert parse_date(raw).status is Status.INVALID


def test_gstin_checksum_matches_published_example() -> None:
    # The sample GSTIN used in GSTN's own documentation (not a business record).
    assert gstin_checksum("27AAPFU0939F1Z") == "V"


def test_gstin_checksum_and_format() -> None:
    assert parse_gstin(SYNTHETIC_GSTIN).status is Status.VALID
    assert parse_gstin(SYNTHETIC_GSTIN.lower()).value == SYNTHETIC_GSTIN
    tampered = SYNTHETIC_GSTIN[:5] + ("B" if SYNTHETIC_GSTIN[5] != "B" else "C") + SYNTHETIC_GSTIN[6:]
    result = parse_gstin(tampered)
    assert result.status is Status.INVALID and result.value == tampered  # kept, never "fixed"
    assert parse_gstin("40AAAAA0000A1Z5").status is Status.INVALID  # no state 40
    assert parse_gstin("27AAAAA0000A1X5").status is Status.INVALID  # 14th char must be Z


def test_identifiers() -> None:
    assert parse_ifsc("sbin0001234").value == "SBIN0001234"
    assert parse_ifsc("SBIN1001234").status is Status.INVALID
    assert parse_value("7", ValueType.STATE_CODE).value == "07"
    assert parse_value("45", ValueType.STATE_CODE).status is Status.INVALID
    assert parse_value("84713", ValueType.HSN_SAC).status is Status.WARNING
    assert parse_value("XXXXXX1234", ValueType.ACCOUNT_NUMBER).status is Status.WARNING
    assert mask_account_number("123456789012") == "XXXXXXXX9012"


def test_missing_values() -> None:
    for value_type in ValueType:
        assert parse_value(None, value_type).status is Status.MISSING
        assert parse_value("   ", value_type).status is Status.MISSING


def test_dr_or_cr_written_straight_after_a_balance() -> None:
    from tally_automation.extraction.parsing import ValueType, parse_value
    assert parse_value("2,74,91,693.76Dr", ValueType.BALANCE).value == "-27491693.76"
    assert parse_value("3,31,21,831.11Cr", ValueType.BALANCE).value == "33121831.11"
    assert parse_value("1,000.00 Dr.", ValueType.BALANCE).value == "-1000.00"


def test_account_numbers_masked_in_the_middle() -> None:
    from tally_automation.extraction.parsing import Status, ValueType, account_matches, parse_value
    for shown in ("261XXXXXXXXX176", "XXXXXXXXXX0074"):
        assert parse_value(shown, ValueType.ACCOUNT_NUMBER).status is Status.WARNING
    assert parse_value("12XX", ValueType.ACCOUNT_NUMBER).status is Status.INVALID
    assert account_matches("261000000000176", "261XXXXXXXXX176")
    assert not account_matches("261000000000177", "261XXXXXXXXX176")      # a visible digit differs
    assert not account_matches("2610000000000176", "261XXXXXXXXX176")     # a different length
    assert account_matches("1234567890074", "XXXXXXXXXX0074") and account_matches("123456", "123456")

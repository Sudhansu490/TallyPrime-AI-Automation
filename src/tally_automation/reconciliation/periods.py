"""Calendar months and Indian financial years (1 April – 31 March)."""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterator
from datetime import date, timedelta

_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_FY = re.compile(r"^(\d{4})-(\d{2})$")

Month = tuple[int, int]   # (year, month)


def parse_month(value: str) -> Month:
    """'2025-04' → (2025, 4)."""
    match = _MONTH.match(value.strip())
    if not match:
        raise ValueError(f"'{value}' is not a month in the form YYYY-MM.")
    return int(match.group(1)), int(match.group(2))


def month_label(month: Month) -> str:
    return f"{month[0]:04d}-{month[1]:02d}"


def month_of(day: date) -> Month:
    return day.year, day.month


def month_bounds(month: Month) -> tuple[date, date]:
    year, number = month
    return date(year, number, 1), date(year, number, calendar.monthrange(year, number)[1])


def next_month(month: Month) -> Month:
    year, number = month
    return (year + 1, 1) if number == 12 else (year, number + 1)


def months_between(first: Month, last: Month) -> Iterator[Month]:
    """Every month from ``first`` to ``last``, both included."""
    month = first
    while month <= last:
        yield month
        month = next_month(month)


def fy_start_year(day: date) -> int:
    """The calendar year in which ``day``'s financial year begins: 15 Jan 2026 → 2025 (FY 2025-26)."""
    return day.year if day.month >= 4 else day.year - 1


def fy_label(start_year: int) -> str:
    """2025 → '2025-26'."""
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def parse_fy(value: str) -> int:
    """'2025-26' → 2025. The second part must be the following year."""
    match = _FY.match(value.strip())
    if not match or (int(match.group(1)) + 1) % 100 != int(match.group(2)):
        raise ValueError(f"'{value}' is not a financial year in the form 2025-26.")
    return int(match.group(1))


def fy_bounds(start_year: int) -> tuple[date, date]:
    """1 April of ``start_year`` to 31 March of the next year."""
    return date(start_year, 4, 1), date(start_year + 1, 3, 31)


def fy_months(start_year: int) -> list[Month]:
    """April to March of the financial year."""
    return list(months_between((start_year, 4), (start_year + 1, 3)))


def day_before(day: date) -> date:
    return day - timedelta(days=1)


def day_after(day: date) -> date:
    return day + timedelta(days=1)

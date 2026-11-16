"""Missing statements: days, months and financial years of an account that no statement covers.

A missing period is reported as missing. It is never filled with a zero (or any other) balance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from tally_automation.reconciliation.balances import Timeline, covered_ranges, gaps
from tally_automation.reconciliation.periods import (
    fy_bounds, fy_label, fy_start_year, month_bounds, month_label, month_of, months_between,
)


@dataclass
class Coverage:
    start: date | None
    end: date | None
    covered: list[tuple[date, date]]
    missing: list[tuple[date, date]]
    missing_months: list[str]
    partial_months: list[dict[str, Any]]
    missing_years: list[str]
    partial_years: list[dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        def ranges(items: list[tuple[date, date]]) -> list[dict[str, str]]:
            return [{"from": a.isoformat(), "to": b.isoformat(), "days": (b - a).days + 1} for a, b in items]
        return {
            "from": self.start.isoformat() if self.start else None, "to": self.end.isoformat() if self.end else None,
            "covered": ranges(self.covered), "missing": ranges(self.missing),
            "missing_months": self.missing_months, "partial_months": self.partial_months,
            "missing_years": self.missing_years, "partial_years": self.partial_years,
        }


def _days(items: list[tuple[date, date]]) -> int:
    return sum((b - a).days + 1 for a, b in items)


def coverage(timeline: Timeline, start: date | None = None, end: date | None = None) -> Coverage:
    """Coverage between ``start`` and ``end`` (default: the account's first to last statement day).
    Months and financial years are judged in full, also where the range cuts through them."""
    start = start or timeline.first_day
    end = end or timeline.last_day
    if start is None or end is None:
        return Coverage(None, None, [], [], [], [], [], [])
    if start > end:
        raise ValueError("The start date is after the end date.")

    missing_months, partial_months = [], []
    for month in months_between(month_of(start), month_of(end)):
        a, b = month_bounds(month)
        missing = gaps(timeline, a, b)
        if _days(missing) == (b - a).days + 1:
            missing_months.append(month_label(month))
        elif missing:
            partial_months.append({"month": month_label(month), "missing_days": _days(missing),
                                   "missing": [{"from": x.isoformat(), "to": y.isoformat()} for x, y in missing]})
    missing_years, partial_years = [], []
    for year in range(fy_start_year(start), fy_start_year(end) + 1):
        a, b = fy_bounds(year)
        missing = gaps(timeline, a, b)
        if _days(missing) == (b - a).days + 1:
            missing_years.append(fy_label(year))
        elif missing:
            partial_years.append({"year": fy_label(year), "missing_days": _days(missing),
                                  "missing": [{"from": x.isoformat(), "to": y.isoformat()} for x, y in missing]})
    return Coverage(start, end, covered_ranges(timeline, start, end), gaps(timeline, start, end),
                    missing_months, partial_months, missing_years, partial_years)

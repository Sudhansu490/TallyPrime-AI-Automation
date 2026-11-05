"""Layout-preserving text: place words on a character grid by their x position.

Plain text extraction joins the cells of a table row with single spaces, so a
value in a "Deposit" column becomes indistinguishable from one in a
"Withdrawal" column. Bank statements and invoice tables depend on that
distinction. Rendering each word at a column proportional to its x
coordinate keeps the columns aligned under their headers, for the LLM and for
the reviewer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


def _char_width(boxes: Sequence[Box]) -> float:
    widths = [(b.x1 - b.x0) / len(b.text) for b in boxes if b.text and b.x1 > b.x0]
    return median(widths) if widths else 1.0


def _rows(boxes: Sequence[Box], tolerance: float) -> list[list[Box]]:
    """Group boxes whose vertical centres are close into rows, top to bottom."""
    ordered = sorted(boxes, key=lambda b: ((b.y0 + b.y1) / 2, b.x0))
    rows: list[list[Box]] = []
    for box in ordered:
        centre = (box.y0 + box.y1) / 2
        if rows:
            anchor = rows[-1][0]
            height = max(anchor.y1 - anchor.y0, 1.0)
            if abs(centre - (anchor.y0 + anchor.y1) / 2) <= height * tolerance:
                rows[-1].append(box)
                continue
        rows.append([box])
    return rows


# A horizontal gap narrower than this many characters is an ordinary word space.
WORD_GAP_CHARS = 2.0


def render_layout(boxes: Sequence[Box], *, row_tolerance: float = 0.5) -> str:
    """Text with words at grid columns ~ x / character width; one output line per row.

    Words separated by a normal space stay separated by one space, so prose is
    unchanged; wider gaps (table columns) are padded so each cell starts at its
    proportional column, with at least two spaces before it.
    """
    boxes = [b for b in boxes if b.text.strip()]
    if not boxes:
        return ""
    char_width = _char_width(boxes)
    origin = min(b.x0 for b in boxes)
    lines: list[str] = []
    for row in _rows(boxes, row_tolerance):
        line = ""
        previous: Box | None = None
        for box in sorted(row, key=lambda b: b.x0):
            column = round((box.x0 - origin) / char_width)
            if previous is None:
                spaces = column
            elif (box.x0 - previous.x1) / char_width < WORD_GAP_CHARS:
                spaces = 1
            else:
                spaces = max(column - len(line), 2)
            line += " " * spaces + box.text.strip()
            previous = box
        lines.append(line.rstrip())
    return "\n".join(lines)

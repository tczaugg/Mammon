"""TXF v042 export for a custom report (SRD compartment J).

TXF is a FORMAT this module can emit, not the thing the report model is designed
around. That ordering is deliberate and is why the writer lives HERE rather than
inside ``custom.py``: the report definition knows nothing about tax forms, and
the tax-form metadata it does carry (``txf_refnum``, ``txf_copy``,
``txf_format``) arrives on the ITEM from a year-definition file, never from a
category. A category has no opinion about a tax form.

What the writer will and will not emit:

* **Only items with a non-NULL ``txf_refnum``.** A tithing report exports
  nothing at all -- correctly, and without raising. A tax report seeded from a
  definition file exports its refnum-bearing lines and silently skips the rest,
  because "skip" is the right answer for a subtotal line that no form asks for.
* **A ``COMPUTED`` item NEVER emits**, even in the impossible case that someone
  puts a refnum on one. TXF has exactly one aggregation concept -- summary over
  detail, which aggregates TRANSACTIONS into one form line -- and no field in
  any of its seven record formats holds another refnum. A computed total is a
  line referencing lines, which the format cannot express; emitting its number
  as if it were an independent form line would double-count it against the
  parts it was computed from.
* **One summary record (``T`` = ``S``) per item** -- except an item broken down
  by tag, which emits one record per tag value on its own ``C`` copy and does
  NOT also emit its total. Detail records (``T`` = ``D``) are optional in the
  spec and are not phase-one work.

Conversions happen at the export BOUNDARY and nowhere else. Everything upstream
is signed integer cents and ISO ``YYYY-MM-DD``; a TXF file wants a plain decimal
string with no ``$`` and no comma, and ``MM/DD/YYYY``. Doing that translation
here -- in the last function before the bytes hit the disk -- is what keeps the
domain layer free of display formats. Rounding at the cents boundary is
``ROUND_HALF_UP`` like everywhere else, though at this point it is a no-op: the
number is already an integer count of cents.

Records are separated by ``^`` on its own line and lines end CRLF, which is what
the consuming tax programs expect; the file is written with ``newline=""`` so
the platform does not helpfully turn that into CRCRLF on Windows.

No Qt, no dialogs: :func:`export_txf_to` takes a path and writes a file. The UI
decides whether to offer the menu entry (a report of ``kind='tax'`` with at
least one refnum-bearing item); this module just answers what the file says.
"""

from __future__ import annotations

import datetime as _dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from mammon.reports import custom

TXF_VERSION = "V042"
TXF_PROGRAM = "Mammon"

_EOL = "\r\n"
_SEPARATOR = "^"


def format_amount(cents: int) -> str:
    """Signed cents as a plain decimal string: ``-1234`` -> ``-12.34``.

    No currency symbol, no thousands separator, always two decimal places -- a
    TXF amount is a number for another program to parse, not something a person
    reads."""
    value = (Decimal(int(cents)) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{value:f}"


def format_date(iso: str) -> str:
    """ISO ``YYYY-MM-DD`` -> TXF ``MM/DD/YYYY``. The ONLY place a stored date
    changes shape on the way out."""
    day = _dt.date.fromisoformat(iso)
    return f"{day.month:02d}/{day.day:02d}/{day.year:04d}"


def txf_records(conn, report_id: int,
                start: Optional[str] = None, end: Optional[str] = None,
                *, today: Optional[_dt.date] = None) -> list[dict]:
    """The exportable items of one evaluated report, in report order.

    Returns plain dicts (``refnum``, ``copy``, ``line``, ``amount``, ``name``,
    ``label``, ``txf_format``) rather than formatted text, so a caller can count
    or inspect what WOULD be exported without writing a file -- which is also
    how the UI decides whether the TXF menu entry exists.

    ``line`` numbers the records within each ``(refnum, copy)`` pair from 1. Two
    W-2s are two items sharing a refnum with different copies, so the pair is
    the right key: numbering globally would renumber employer two every time an
    unrelated line was added above it.

    An item BROKEN DOWN BY TAG exports its sub-rows INSTEAD OF its total -- one
    record per tag on the copy that sub-row owns, which is exactly TXF's
    mechanism for a second and third Schedule E property. Exporting the total as
    well would bill the same money twice. A sub-row with nothing in it is left
    out (an untagged remainder of zero is not a form line); the copy numbers come
    from the evaluation, so dropping it shifts nothing."""
    evaluation = custom.evaluate(conn, report_id, start, end, today=today)
    by_id = {item.id: item for item in custom.list_items(conn, report_id)}
    broken = {row.item_id for row in evaluation.rows if row.is_breakdown}
    seen: dict[tuple[int, int], int] = {}
    out: list[dict] = []
    for row in evaluation.rows:
        item = by_id.get(row.item_id)
        if item is None or item.txf_refnum is None:
            continue
        if item.kind == "COMPUTED":
            continue
        if row.is_breakdown:
            if row.no_data:
                continue
        elif row.item_id in broken:
            continue                     # its sub-rows carry the money
        copy = row.txf_copy if row.txf_copy is not None else (item.txf_copy or 1)
        key = (int(item.txf_refnum), int(copy))
        seen[key] = seen.get(key, 0) + 1
        out.append({
            "refnum": key[0],
            "copy": key[1],
            "line": seen[key],
            "amount": int(row.amount),
            "name": item.name,
            "label": row.label if row.is_breakdown else item.display_label,
            "txf_format": item.txf_format,
        })
    return out


def txf_text(conn, report_id: int,
             start: Optional[str] = None, end: Optional[str] = None,
             *, today: Optional[_dt.date] = None,
             export_date: Optional[str] = None,
             program: str = TXF_PROGRAM) -> str:
    """The whole TXF file as text.

    ``export_date`` is the date stamped in the header (today when omitted); it
    is a parameter so a test can assert the bytes without owning the clock."""
    records = txf_records(conn, report_id, start, end, today=today)
    stamp = export_date or (today or _dt.date.today()).isoformat()
    lines = [TXF_VERSION, f"A{program}", f"D{format_date(stamp)}", _SEPARATOR]
    for rec in records:
        lines.extend([
            "TS",                              # summary, not detail
            f"N{rec['refnum']}",
            f"C{rec['copy']}",
            f"L{rec['line']}",
            f"${format_amount(rec['amount'])}",
            _SEPARATOR,
        ])
    return _EOL.join(lines) + _EOL


def export_txf_to(conn, report_id: int, path,
                  start: Optional[str] = None, end: Optional[str] = None,
                  *, today: Optional[_dt.date] = None,
                  export_date: Optional[str] = None,
                  program: str = TXF_PROGRAM) -> int:
    """Write the TXF file and return how many ITEM records it holds.

    A return of 0 means a well-formed file with a header and no items -- which
    is the honest answer for a report carrying no refnums, and better than
    refusing to write: the user asked for the export, and an empty one tells him
    what is wrong faster than an error box he has to interpret."""
    text = txf_text(conn, report_id, start, end, today=today,
                    export_date=export_date, program=program)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return len(txf_records(conn, report_id, start, end, today=today))

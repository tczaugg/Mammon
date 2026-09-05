"""mammon.reports.spending -- spending itemized by category (SRD 5.9, priority).

The first and highest-priority Mammon report: how much went OUT, grouped by
category, over a selectable period. It sits directly on the ``transactions`` /
``splits`` / ``categories`` tables (via mammon.db) and mirrors mammon.ledger's
conventions -- money is signed integer cents, and a transfer is any transaction
whose ``transfer_account_id`` is set.

Definitions (locked with the task brief):
- SPENDING is money OUT: a NEGATIVE ledger amount. It is reported as a POSITIVE
  magnitude (cents). Income / refunds (positive amounts) are simply not part of
  this report -- they are NOT netted against spending, so a category's parent
  total is always exactly the sum of its children's totals. (Net cash-flow and
  income-vs-expense reports come later per SRD 5.9.)
- TRANSFERS ARE EXCLUDED. Moving money between your own accounts is not
  spending, so any transaction with a ``transfer_account_id`` is skipped.
- SPLITS ARE HONORED. If a transaction is split, each split line is attributed
  to its own category (and the transaction's own amount is ignored, so nothing
  is double-counted); an un-split transaction is attributed to its own category.
- HIERARCHY. Child categories roll UP under their parent: a category's
  ``total_cents`` includes all descendants, while ``own_cents`` is just the
  spending booked directly on it. The returned tree also exposes the leaf
  breakdown as nested ``children``.
- UNCATEGORIZED spending (no category) is reported as a top-level pseudo-row.

The result is a plain data structure (dataclasses) so a GUI, a CSV writer, or
the bundled :func:`format_spending_report` text renderer can all consume it
without re-querying.
"""
from __future__ import annotations

import calendar
import datetime as _dt
from dataclasses import dataclass, field
from typing import Iterable, Optional

_UNCATEGORIZED = "Uncategorized"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class CategoryReportRow:
    """One category node in the spending tree. ``total_cents`` includes this
    category's own spending plus every descendant's; ``own_cents`` is only what
    was booked directly on it. Both are positive magnitudes in cents."""

    category_id: Optional[int]        # None for the Uncategorized pseudo-row
    name: str                         # leaf name ("Fuel")
    path: str                         # full path ("Auto & Transport:Fuel")
    depth: int                        # 0 at top level, +1 per level down
    own_cents: int
    total_cents: int
    children: list["CategoryReportRow"] = field(default_factory=list)


@dataclass
class SpendingReport:
    start: str                        # ISO YYYY-MM-DD, inclusive
    end: str                          # ISO YYYY-MM-DD, inclusive
    account_ids: Optional[list[int]]  # None == all accounts
    rows: list[CategoryReportRow]     # top-level rows, each with nested children
    total_cents: int                  # grand total spending magnitude (cents)

    def flat(self) -> list[CategoryReportRow]:
        """Depth-first flattening (parents before children) for CSV / table
        rendering. Order matches :func:`format_spending_report`."""
        out: list[CategoryReportRow] = []

        def walk(nodes: list[CategoryReportRow]) -> None:
            for n in nodes:
                out.append(n)
                walk(n.children)

        walk(self.rows)
        return out


# ---------------------------------------------------------------------------
# Period helper: month / quarter / year -> (start, end)
# ---------------------------------------------------------------------------
def period_range(period: str, year: int, *, month: Optional[int] = None,
                 quarter: Optional[int] = None) -> tuple[str, str]:
    """Return the inclusive ``(start, end)`` ISO dates for a named period.

    - ``period="month"``   requires ``month`` (1-12).
    - ``period="quarter"`` requires ``quarter`` (1-4).
    - ``period="year"``    uses ``year`` alone.

    For a fully custom range, skip this helper and pass your own start/end
    straight to :func:`spending_by_category`.
    """
    p = (period or "").strip().lower()
    if p == "year":
        return f"{year:04d}-01-01", f"{year:04d}-12-31"
    if p == "month":
        if month is None or not 1 <= month <= 12:
            raise ValueError("month period requires month in 1..12")
        last = calendar.monthrange(year, month)[1]
        return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"
    if p == "quarter":
        if quarter is None or not 1 <= quarter <= 4:
            raise ValueError("quarter period requires quarter in 1..4")
        start_month = (quarter - 1) * 3 + 1
        end_month = start_month + 2
        last = calendar.monthrange(year, end_month)[1]
        return (f"{year:04d}-{start_month:02d}-01",
                f"{year:04d}-{end_month:02d}-{last:02d}")
    raise ValueError(f"unknown period {period!r}; use month|quarter|year")


def _years_back(today: _dt.date, n: int) -> _dt.date:
    """``today`` minus ``n`` calendar years, stepping a Feb-29 anniversary back to
    Feb 28 in a non-leap year -- the guard the rolling-year windows share."""
    try:
        return today.replace(year=today.year - n)
    except ValueError:            # today is Feb 29; step back to Feb 28
        return today.replace(year=today.year - n, day=28)


def preset_range(preset: str, today: _dt.date) -> tuple[str, str]:
    """Return the inclusive ``(start, end)`` ISO dates for a UI period preset,
    computed relative to ``today`` (a :class:`datetime.date`).

    Rolling presets (the unified report period dropdown, §5.9b):

    - ``"last_7_days"``    -- the 7 days ending today (today-6 .. today).
    - ``"last_30_days"``   -- the 30 days ending today (today-29 .. today).
    - ``"last_12_months"`` -- one year ago (same day) through today.
    - ``"last_3_years"``   -- three years ago (same day) through today.
    - ``"last_5_years"``   -- five years ago (same day) through today.
    - ``"last_10_years"``  -- ten years ago (same day) through today.
    - ``"this_quarter"``   -- the calendar quarter ``today`` falls in.
    - ``"last_quarter"``   -- the previous calendar quarter (crossing years).

    Calendar presets (kept for the customize-dialog fallback and callers that
    still ask for them by name):

    - ``"this_month"`` -- first..last day of ``today``'s month.
    - ``"last_month"`` -- the whole previous calendar month (crossing years).
    - ``"this_year"``  -- Jan 1..Dec 31 of ``today``'s year.
    - ``"last_year"``  -- Jan 1..Dec 31 of the previous year.
    - ``"ytd"``        -- Jan 1 of ``today``'s year through ``today`` inclusive.

    ``"custom"`` and ``"earliest"`` are intentionally NOT handled here: a custom
    range is whatever the user picks in the customize dialog, and "earliest to
    date" needs the ledger's own bounds -- both are resolved in the UI
    (``ui/report_filters.resolve_period``), not from ``today`` alone.
    """
    p = (preset or "").strip().lower()
    y, m = today.year, today.month
    iso = today.strftime("%Y-%m-%d")
    if p == "last_7_days":
        return (today - _dt.timedelta(days=6)).strftime("%Y-%m-%d"), iso
    if p == "last_30_days":
        return (today - _dt.timedelta(days=29)).strftime("%Y-%m-%d"), iso
    if p == "last_12_months":
        return _years_back(today, 1).strftime("%Y-%m-%d"), iso
    if p == "last_3_years":
        return _years_back(today, 3).strftime("%Y-%m-%d"), iso
    if p == "last_5_years":
        return _years_back(today, 5).strftime("%Y-%m-%d"), iso
    if p == "last_10_years":
        return _years_back(today, 10).strftime("%Y-%m-%d"), iso
    if p == "this_quarter":
        return period_range("quarter", y, quarter=(m - 1) // 3 + 1)
    if p == "last_quarter":
        q = (m - 1) // 3 + 1
        return (period_range("quarter", y - 1, quarter=4) if q == 1
                else period_range("quarter", y, quarter=q - 1))
    if p == "this_month":
        return period_range("month", y, month=m)
    if p == "last_month":
        ly, lm = (y - 1, 12) if m == 1 else (y, m - 1)
        return period_range("month", ly, month=lm)
    if p == "this_year":
        return period_range("year", y)
    if p == "last_year":
        return period_range("year", y - 1)
    if p == "ytd":
        return period_range("year", y)[0], iso
    raise ValueError(
        f"unknown preset {preset!r}; use last_7_days|last_30_days|"
        "last_12_months|last_3_years|last_5_years|last_10_years|this_quarter|"
        "last_quarter|this_month|last_month|this_year|last_year|ytd")


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def spending_by_category(conn, start: str, end: str,
                         account_ids: Optional[Iterable[int]] = None) -> SpendingReport:
    """Spending grouped by category over ``[start, end]`` (inclusive ISO dates).

    ``account_ids`` restricts the report to those accounts; ``None`` includes
    every account. Passing an explicit empty iterable selects no accounts (an
    empty report), which is the logical -- if rarely useful -- result.

    Returns a :class:`SpendingReport` whose top-level ``rows`` carry rolled-up
    ``total_cents`` and nested ``children`` for the leaf breakdown.
    """
    _validate_date(start)
    _validate_date(end)
    acct_list = None if account_ids is None else [int(a) for a in account_ids]

    own = _aggregate_own_spending(conn, start, end, acct_list)
    cats = _load_categories(conn)
    rows, total = _build_tree(own, cats)
    return SpendingReport(start=start, end=end, account_ids=acct_list,
                          rows=rows, total_cents=total)


def _aggregate_own_spending(conn, start: str, end: str,
                            acct_list: Optional[list[int]]) -> dict[Optional[int], int]:
    """Sum spending (positive magnitude cents) directly attributable to each
    category id (None = uncategorized), honoring splits and excluding transfers.
    """
    where = ["date >= ?", "date <= ?", "transfer_account_id IS NULL"]
    params: list = [start, end]
    if acct_list is not None:
        if not acct_list:
            return {}                       # no accounts selected -> nothing
        marks = ",".join("?" for _ in acct_list)
        where.append(f"account_id IN ({marks})")
        params.extend(acct_list)
    sql = "SELECT id, category_id, amount FROM transactions WHERE " + " AND ".join(where)
    txns = conn.execute(sql, params).fetchall()
    if not txns:
        return {}

    # Which of these transactions are split? Pull their split lines in one query.
    ids = [t["id"] for t in txns]
    splits: dict[int, list] = {}
    for chunk_start in range(0, len(ids), 500):     # keep the IN() list bounded
        chunk = ids[chunk_start:chunk_start + 500]
        marks = ",".join("?" for _ in chunk)
        for s in conn.execute(
            f"SELECT transaction_id, category_id, amount FROM splits "
            f"WHERE transaction_id IN ({marks})", chunk,
        ).fetchall():
            splits.setdefault(s["transaction_id"], []).append(s)

    totals: dict[Optional[int], int] = {}
    for t in txns:
        lines = splits.get(t["id"])
        if lines:                                   # split txn: use the split lines
            for s in lines:
                if s["amount"] < 0:
                    _add(totals, s["category_id"], -s["amount"])
        elif t["amount"] < 0:                       # plain txn: use its own amount
            _add(totals, t["category_id"], -t["amount"])
    return totals


def _add(d: dict, key, cents: int) -> None:
    d[key] = d.get(key, 0) + cents


def _load_categories(conn) -> dict[int, dict]:
    """id -> {'id','name','parent_id'} for every category (one query)."""
    out: dict[int, dict] = {}
    for r in conn.execute("SELECT id, name, parent_id FROM categories").fetchall():
        out[r["id"]] = {"id": r["id"], "name": r["name"], "parent_id": r["parent_id"]}
    return out


def _path_of(cat_id: int, cats: dict[int, dict]) -> str:
    parts: list[str] = []
    cid: Optional[int] = cat_id
    seen: set[int] = set()
    while cid is not None and cid in cats and cid not in seen:
        seen.add(cid)
        parts.append(cats[cid]["name"])
        cid = cats[cid]["parent_id"]
    return ":".join(reversed(parts))


def _build_tree(own: dict[Optional[int], int],
                cats: dict[int, dict]) -> tuple[list[CategoryReportRow], int]:
    # children index over real categories
    children_of: dict[Optional[int], list[int]] = {}
    for cid, meta in cats.items():
        children_of.setdefault(meta["parent_id"], []).append(cid)

    def build(cid: int, depth: int) -> CategoryReportRow:
        own_cents = own.get(cid, 0)
        kids: list[CategoryReportRow] = []
        for child_id in children_of.get(cid, []):
            child = build(child_id, depth + 1)
            if child.total_cents > 0:               # prune empty branches
                kids.append(child)
        kids.sort(key=lambda r: (-r.total_cents, r.path.lower()))
        total = own_cents + sum(k.total_cents for k in kids)
        return CategoryReportRow(
            category_id=cid, name=cats[cid]["name"], path=_path_of(cid, cats),
            depth=depth, own_cents=own_cents, total_cents=total, children=kids,
        )

    rows: list[CategoryReportRow] = []
    for cid in children_of.get(None, []):           # top-level real categories
        node = build(cid, 0)
        if node.total_cents > 0:
            rows.append(node)

    # Uncategorized spending (category_id NULL) as a top-level pseudo-row.
    unc = own.get(None, 0)
    if unc > 0:
        rows.append(CategoryReportRow(
            category_id=None, name=_UNCATEGORIZED, path=_UNCATEGORIZED,
            depth=0, own_cents=unc, total_cents=unc, children=[],
        ))

    rows.sort(key=lambda r: (-r.total_cents, r.path.lower()))
    grand_total = sum(r.total_cents for r in rows)
    return rows, grand_total


# ---------------------------------------------------------------------------
# Plain-text renderer
# ---------------------------------------------------------------------------
def format_spending_report(report: SpendingReport, *, width: int = 48) -> str:
    """Render a spending report as an indented, right-aligned text table. The
    amount column shows each category's rolled-up ``total_cents`` as dollars."""
    flat = report.flat()
    label_col = max(
        [len("Spending by Category")]
        + [2 * r.depth + len(r.name) for r in flat]
        + [len("Total")]
    )
    amt_col = max(len(_fmt_dollars(report.total_cents)),
                  *([len(_fmt_dollars(r.total_cents)) for r in flat] or [0]))
    pad = max(width, label_col + 2 + amt_col)

    def line(label: str, cents: int) -> str:
        amt = _fmt_dollars(cents)
        return f"{label}{amt.rjust(pad - len(label))}"

    rule = "-" * pad
    title = f"Spending by Category  {report.start} to {report.end}"
    lines = [title, rule]
    if not flat:
        lines.append("(no spending in this period)")
    for r in flat:
        lines.append(line("  " * r.depth + r.name, r.total_cents))
    lines.append(rule)
    lines.append(line("Total", report.total_cents))
    return "\n".join(lines)


def _fmt_dollars(cents: int) -> str:
    """Integer-cents -> '1,234.56' (no float; keeps full precision)."""
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}{c // 100:,}.{c % 100:02d}"


def _validate_date(date: str) -> None:
    try:
        _dt.date.fromisoformat(date)
    except (TypeError, ValueError):
        raise ValueError(f"date must be ISO 'YYYY-MM-DD', got {date!r}")

"""mammon.reports.charts -- chart-ready data for Mammon's report visualizations.

These functions turn the ledger into small, plain data structures that a
charting layer (matplotlib here, but equally an HTML export or QtChart) can
render without any knowledge of SQL or of matplotlib. Keeping the number
crunching HERE -- and out of the GUI -- means the pie/line inputs are
unit-testable headless, exactly like the text reports in this package.

Two charts (the user's request, P2f):
- a SPENDING PIE by top-level category over a period, and
- NET WORTH OVER TIME sampled across a date range.

A third, SPENDING PER PERIOD, totals money-out into calendar buckets so a bar
chart can show the trend; it is the data behind the home page's spending chart.
Its value axis is deliberately auto-scaled (never zero-based) by the rendering
layer so a few-hundred-dollar swing stays visible against thousand-dollar
monthly totals -- but that is a display concern; here we only produce the sums.

Money stays signed integer cents throughout; the caller formats for display.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from mammon import ledger
from mammon.reports._lines import resolve_accounts, signed_lines
from mammon.reports.flows import bucket_of, buckets_in
from mammon.reports.spending import spending_by_category

_OTHER = "Other"


# ---------------------------------------------------------------------------
# Spending pie
# ---------------------------------------------------------------------------
@dataclass
class PieSlice:
    label: str
    cents: int          # positive magnitude
    fraction: float     # cents / total, in 0..1


@dataclass
class SpendingPie:
    start: str
    end: str
    slices: list  # list[PieSlice], largest first (Other, if any, sorts last)
    total_cents: int

    def is_empty(self) -> bool:
        return self.total_cents <= 0 or not self.slices


def spending_pie(conn, start, end, account_ids=None, max_slices=8) -> SpendingPie:
    """Top-level spending categories as pie slices over ``[start, end]``.

    Built on :func:`mammon.reports.spending.spending_by_category`, so it inherits
    the same locked semantics: money OUT only (negative amounts, reported as
    positive magnitudes), transfers excluded, splits honored, an Uncategorized
    bucket. Only the ROLLED-UP top-level categories become slices.

    To keep a pie legible with dozens of categories, the largest
    ``max_slices - 1`` slices are kept and the remaining long tail is collapsed
    into a single ``"Other"`` slice. Slices are ordered largest first; ``Other``
    (when present) always sorts last.
    """
    if max_slices < 2:
        raise ValueError("max_slices must be >= 2")
    from mammon.category_types import INCOME, classify_categories
    report = spending_by_category(conn, start, end, account_ids)
    types = classify_categories(conn)
    # EXPENSE categories only: an income-type category that happens to have a
    # negative row (a clawback, a mis-signed correction) is NOT spending, so it
    # is dropped. Uncategorized money-out (category_id is None) has no type and
    # is kept -- it is genuinely money that went out.
    rows = [r for r in report.rows if r.total_cents > 0
            and (r.category_id is None or types.get(r.category_id) != INCOME)]
    total = sum(r.total_cents for r in rows)
    if total <= 0 or not rows:
        return SpendingPie(start=start, end=end, slices=[], total_cents=0)

    if len(rows) > max_slices:
        keep, tail = rows[:max_slices - 1], rows[max_slices - 1:]
    else:
        keep, tail = rows, []
    slices = [PieSlice(r.name, r.total_cents, r.total_cents / total) for r in keep]
    if tail:
        other = sum(r.total_cents for r in tail)
        slices.append(PieSlice(_OTHER, other, other / total))
    return SpendingPie(start=start, end=end, slices=slices, total_cents=total)


def income_pie(conn, start, end, account_ids=None, max_slices=8) -> SpendingPie:
    """Top-level INCOME categories as pie slices over ``[start, end]`` -- the
    mirror image of :func:`spending_pie`.

    Money IN (positive amounts) is summed per category, restricted to categories
    classified INCOME by :mod:`mammon.category_types`, then rolled up to the
    top-level ancestor for the slice. Transfers are excluded and splits honored
    exactly as the spending report does. The tail beyond ``max_slices - 1`` slices
    collapses into a single ``"Other"`` slice. Returns the same :class:`SpendingPie`
    payload (a generic category-pie: labels, magnitudes, fractions) that the
    spending pie does, so one canvas renders both.
    """
    if max_slices < 2:
        raise ValueError("max_slices must be >= 2")
    from mammon.reports.spending import _validate_date
    _validate_date(start)
    _validate_date(end)
    acct_list = None if account_ids is None else [int(a) for a in account_ids]
    by_top, cats = _income_by_top_level(conn, start, end, acct_list)

    total = sum(by_top.values())
    if total <= 0 or not by_top:
        return SpendingPie(start=start, end=end, slices=[], total_cents=0)

    rows = sorted(by_top.items(),
                  key=lambda kv: (-kv[1], cats[kv[0]]["name"].lower()))
    if len(rows) > max_slices:
        keep, tail = rows[:max_slices - 1], rows[max_slices - 1:]
    else:
        keep, tail = rows, []
    slices = [PieSlice(cats[cid]["name"], cents, cents / total)
              for cid, cents in keep]
    if tail:
        other = sum(cents for _cid, cents in tail)
        slices.append(PieSlice(_OTHER, other, other / total))
    return SpendingPie(start=start, end=end, slices=slices, total_cents=total)


def _income_by_top_level(conn, start, end, acct_list):
    """``({top_level_category_id: positive cents}, categories)`` of INCOME money
    over the window. The single place the "money IN, income-type categories only,
    rolled up to the top-level ancestor, transfers excluded, splits honored" rule
    lives -- shared by :func:`income_pie` (which then collapses a max-slices tail
    into ``Other``) and :func:`income_category_rows` (which hands the raw
    top-level rows to a canvas that does its OWN 10%-``Other`` rollup and
    drill-down). Keeping the crunch in one place stops the two paths drifting."""
    from mammon.category_types import INCOME, classify_categories
    from mammon.reports.spending import _load_categories
    per_cat = _aggregate_money_in(conn, start, end, acct_list)
    cats = _load_categories(conn)
    types = classify_categories(conn)
    by_top: dict[int, int] = {}
    for cid, cents in per_cat.items():
        if types.get(cid) != INCOME:            # income-type categories only
            continue
        top = _top_level_id(cid, cats)
        if top is None or top not in cats:
            continue
        by_top[top] = by_top.get(top, 0) + cents
    return by_top, cats


def income_category_rows(conn, start, end, account_ids=None) -> list:
    """Top-level INCOME categories as raw ``[(name, cents)]`` over ``[start,
    end]``, largest first (ties by lowercased name) -- the UNGROUPED input for a
    pie that rolls up its own long tail and drills into it
    (:class:`mammon.ui.charts.SlicesPieCanvas`, via
    :func:`mammon.ui.charts.group_small_slices`).

    Same crunching as :func:`income_pie` (money IN, income-type categories only,
    rolled to the top-level ancestor, transfers excluded, splits honored) but
    WITHOUT the max-slices collapse: the canvas needs every top-level category as
    its own row so its ``Other`` wedge can break back out into real components on
    a click. Money stays integer cents; the caller formats for display.
    """
    from mammon.reports.spending import _validate_date
    _validate_date(start)
    _validate_date(end)
    acct_list = None if account_ids is None else [int(a) for a in account_ids]
    by_top, cats = _income_by_top_level(conn, start, end, acct_list)
    rows = sorted(by_top.items(),
                  key=lambda kv: (-kv[1], cats[kv[0]]["name"].lower()))
    return [(cats[cid]["name"], cents) for cid, cents in rows]


def spending_category_rows(conn, start, end, account_ids=None) -> list:
    """Top-level EXPENSE categories as raw ``[(name, cents)]`` over ``[start,
    end]``, largest first (ties by lowercased name) -- the UNGROUPED input for a
    pie that rolls up its own long tail and drills into it
    (:class:`mammon.ui.charts.SlicesPieCanvas`, via
    :func:`mammon.ui.charts.group_small_slices`).

    The spending counterpart to :func:`income_category_rows`, and the same
    money-OUT, expense-only semantics as :func:`spending_pie` (money OUT only,
    reported as positive magnitudes; transfers excluded; splits honored; an
    income-type category with a negative row dropped; Uncategorized money-out
    kept) -- but WITHOUT the max-slices collapse, so the canvas owns the
    ``Other`` rollup and can break it back out into real categories on a click.
    Keeping the crunch on top of ``spending_by_category`` (not a second query)
    stops the pie and the by-category report drifting. Money stays integer
    cents; the caller formats for display.
    """
    from mammon.category_types import INCOME, classify_categories
    report = spending_by_category(conn, start, end, account_ids)
    types = classify_categories(conn)
    rows = [r for r in report.rows if r.total_cents > 0
            and (r.category_id is None or types.get(r.category_id) != INCOME)]
    rows.sort(key=lambda r: (-r.total_cents, r.name.lower()))
    return [(r.name, r.total_cents) for r in rows]


def _top_level_id(cat_id, cats):
    """Walk parents up to the top-level ancestor id (cycle-safe)."""
    cid = cat_id
    seen: set = set()
    while (cid is not None and cid in cats
           and cats[cid]["parent_id"] is not None and cid not in seen):
        seen.add(cid)
        cid = cats[cid]["parent_id"]
    return cid


def _aggregate_money_in(conn, start, end, acct_list):
    """``category_id -> positive cents`` (money IN) over the window, honoring
    splits and excluding transfers. Uncategorized rows are dropped."""
    where = ["date >= ?", "date <= ?", "transfer_account_id IS NULL"]
    params: list = [start, end]
    if acct_list is not None:
        if not acct_list:
            return {}
        marks = ",".join("?" for _ in acct_list)
        where.append(f"account_id IN ({marks})")
        params.extend(acct_list)
    sql = "SELECT id, category_id, amount FROM transactions WHERE " + " AND ".join(where)
    txns = conn.execute(sql, params).fetchall()
    if not txns:
        return {}

    ids = [t["id"] for t in txns]
    splits: dict[int, list] = {}
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start:chunk_start + 500]
        marks = ",".join("?" for _ in chunk)
        for s in conn.execute(
            f"SELECT transaction_id, category_id, amount FROM splits "
            f"WHERE transaction_id IN ({marks})", chunk,
        ).fetchall():
            splits.setdefault(s["transaction_id"], []).append(s)

    totals: dict[int, int] = {}
    for t in txns:
        lines = splits.get(t["id"])
        if lines:
            for s in lines:
                if s["amount"] > 0 and s["category_id"] is not None:
                    totals[s["category_id"]] = totals.get(s["category_id"], 0) + s["amount"]
        elif t["amount"] > 0 and t["category_id"] is not None:
            totals[t["category_id"]] = totals.get(t["category_id"], 0) + t["amount"]
    return totals


# ---------------------------------------------------------------------------
# Net worth over time
# ---------------------------------------------------------------------------
@dataclass
class NetWorthPoint:
    date: str           # ISO YYYY-MM-DD
    cents: int          # net worth as of end-of-day on that date


@dataclass
class NetWorthSeries:
    start: str
    end: str
    points: list        # list[NetWorthPoint] in ascending date order

    def is_empty(self) -> bool:
        return not self.points


def _subtree_category_ids(conn, names) -> set:
    """Every category id under the given TOP-LEVEL category names, inclusive.

    The filter bar lists top-level names (it mirrors what the spending report
    groups by), but money posts to leaves, so "Vacation" has to reach
    ``Vacation:Airfare`` too or excluding it would barely change anything."""
    wanted = {str(n).strip().lower() for n in names if str(n).strip()}
    if not wanted:
        return set()
    rows = conn.execute("SELECT id, name, parent_id FROM categories").fetchall()
    children: dict = {}
    for r in rows:
        children.setdefault(r["parent_id"], []).append(int(r["id"]))
    out: set = set()
    stack = [int(r["id"]) for r in rows
             if r["parent_id"] is None and str(r["name"]).strip().lower() in wanted]
    while stack:
        cid = stack.pop()
        if cid in out:
            continue
        out.add(cid)
        stack.extend(children.get(cid, []))
    return out


def _category_flow_through(conn, as_of, category_ids, account_ids=None) -> int:
    """Signed cents posted to ``category_ids`` on or before ``as_of``.

    Counts plain rows AND split lines, because a split's parent transaction
    carries a NULL category -- missing them would leave half of a categorized
    purchase in the total. Scheduled placeholder rows are excluded: they are
    Mammon's own pre-entries for money that has not moved.
    """
    if not category_ids:
        return 0
    marks = ",".join("?" * len(category_ids))
    params: list = list(category_ids)
    acct_sql = ""
    if account_ids is not None:
        if not account_ids:
            return 0
        acct_sql = " AND t.account_id IN (%s)" % ",".join("?" * len(account_ids))
    plain = (
        "SELECT COALESCE(SUM(t.amount), 0) FROM transactions t "
        "WHERE t.category_id IN (%s) AND t.date<=? AND t.scheduled=0%s"
        % (marks, acct_sql))
    split = (
        "SELECT COALESCE(SUM(s.amount), 0) FROM splits s "
        "JOIN transactions t ON t.id = s.transaction_id "
        "WHERE s.category_id IN (%s) AND t.date<=? AND t.scheduled=0%s"
        % (marks, acct_sql))
    args = params + [as_of] + (list(account_ids) if account_ids is not None else [])
    return int(conn.execute(plain, args).fetchone()[0]) + \
        int(conn.execute(split, args).fetchone()[0])


def net_worth_series(conn, start=None, end=None, points=24, *,
                     account_ids=None, include_hidden: bool = False,
                     exclude_categories=None) -> NetWorthSeries:
    """Net worth sampled at up to ``points`` evenly-spaced dates over a range.

    ``start``/``end`` default to the ledger's transaction date bounds, so a bare
    call charts the whole history. Each sample is :func:`mammon.ledger.net_worth`
    AS-OF that date (transfers net to zero). Sample dates are de-duplicated, so a
    range shorter than ``points`` days yields one point per day. Returns an empty
    series when there are no transactions.

    ``account_ids`` charts a subset -- "how did the retirement accounts grow".

    ``include_hidden`` defaults to False, matching :func:`ledger.net_worth` so
    the chart and the account bar never disagree by default. Turning it on is
    the interesting case for a GROWTH curve specifically: an account zeroed
    before it was hidden contributes nothing to today's total but held money for
    years, and leaving it out makes decades of saving look like a recent
    windfall. It is an opt-in rather than the default because hiding is also how
    a user excludes an account whose records are incomplete, and that balance
    should not reappear in a total unasked.

    ``exclude_categories`` (top-level category NAMES) draws a COUNTERFACTUAL
    line: net worth as if that spending had never happened. Each sample has the
    cumulative flow posted to those categories subtracted, so unticking
    "Vacation" lifts the curve by everything vacations ever cost, compounding
    forward. It is not a subset of net worth -- a balance carries no category --
    it is a what-if, and worth labelling as one wherever it is plotted.

    Transfers are untouched by it, having no category at all, so moving money
    between your own accounts never registers as spending here.
    """
    if points < 2:
        raise ValueError("points must be >= 2")
    lo, hi = ledger.transaction_date_bounds(conn)
    start = start or lo
    end = end or hi
    if not start or not end:
        return NetWorthSeries(start=start or "", end=end or "", points=[])

    d0 = _dt.date.fromisoformat(start)
    d1 = _dt.date.fromisoformat(end)
    if d1 < d0:
        d0, d1 = d1, d0
    span = (d1 - d0).days
    if span == 0:
        dates = [d0]
    else:
        n = min(points, span + 1)
        dates = []
        for i in range(n):
            off = round(i * span / (n - 1))
            d = d0 + _dt.timedelta(days=off)
            if not dates or dates[-1] != d:
                dates.append(d)

    drop = _subtree_category_ids(conn, exclude_categories or ())
    pts = []
    for d in dates:
        iso = d.isoformat()
        cents = ledger.net_worth(conn, as_of=iso, account_ids=account_ids,
                                 include_hidden=include_hidden)
        if drop:
            # Undo the flow rather than re-deriving balances: the excluded
            # spending is money that left the account, so adding it back is
            # exactly "as if it had never happened".
            cents -= _category_flow_through(conn, iso, drop, account_ids)
        pts.append(NetWorthPoint(iso, cents))
    return NetWorthSeries(start=d0.isoformat(), end=d1.isoformat(), points=pts)


# ---------------------------------------------------------------------------
# Spending per period (bar chart)
# ---------------------------------------------------------------------------
@dataclass
class PeriodSpending:
    key: str            # bucket key: "2026-03" (month), "2026-Q1", "2026"
    label: str          # display label, e.g. "Mar 2026"
    cents: int          # positive MAGNITUDE of money out in this bucket
    income_cents: int = 0   # positive MAGNITUDE of money in this bucket


@dataclass
class SpendingByPeriod:
    start: str
    end: str
    bucket: str                 # "month" | "quarter" | "year"
    periods: list               # list[PeriodSpending], chronological

    def is_empty(self) -> bool:
        # Empty only when NO money moved either way: an income-only month still
        # has a green bar worth drawing, so it is not "empty".
        return not any(p.cents or p.income_cents for p in self.periods)

    def total_cents(self) -> int:
        return sum(p.cents for p in self.periods)

    def total_income_cents(self) -> int:
        return sum(p.income_cents for p in self.periods)


def _period_label(key: str, bucket: str) -> str:
    """Human label for a bucket key -- 'Mar 2026', 'Q1 2026', '2026'."""
    if bucket == "month":
        return _dt.date.fromisoformat(f"{key}-01").strftime("%b %Y")
    if bucket == "quarter":
        year, q = key.split("-Q")
        return f"Q{q} {year}"
    return key                                  # year (or anything unlabelled)


def spending_by_period(conn, start, end, *, bucket="month", account_ids=None,
                       include_scheduled=False) -> SpendingByPeriod:
    """Money OUT and money IN per period bucket over ``[start, end]``, zero-filled.

    Each bucket carries both the spending magnitude (``cents``) and the income
    magnitude (``income_cents``), so the home chart can draw grouped
    spending/income bars from one pass.

    Spending is the magnitude of every money-OUT line (negative signed cents,
    reported here as a POSITIVE magnitude) summed per calendar bucket; income is
    the same for money-IN (positive) lines. It
    inherits the shared line semantics from :mod:`mammon.reports._lines`:
    transfers are excluded (moving your own money between accounts is not
    spending), splits are honored line by line, and scheduled placeholder rows
    are excluded unless ``include_scheduled`` is set. Every bucket the range
    spans is present even when nothing happened in it, so a bar chart shows the
    quiet months as gaps rather than dropping them.

    Money stays integer cents; the caller formats for display. ``bucket`` is
    ``month`` (default), ``quarter`` or ``year``. ``account_ids`` charts a
    subset; ``None`` covers every (non-hidden) account.
    """
    keys = buckets_in(start, end, bucket)
    acct_list = resolve_accounts(conn, account_ids)
    totals = {k: 0 for k in keys}
    income = {k: 0 for k in keys}
    for ln in signed_lines(conn, start, end, acct_list,
                           transfers="exclude",
                           include_scheduled=include_scheduled):
        b = bucket_of(ln.date, bucket)
        if b not in totals:                     # always present; cheap guard
            continue
        if ln.amount < 0:
            totals[b] -= ln.amount              # accumulate positive magnitude
        elif ln.amount > 0:
            income[b] += ln.amount              # money in, positive magnitude
    periods = [PeriodSpending(k, _period_label(k, bucket), totals[k], income[k])
               for k in keys]
    return SpendingByPeriod(start=start, end=end, bucket=bucket, periods=periods)

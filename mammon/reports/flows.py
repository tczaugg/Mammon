"""mammon.reports.flows -- income vs. expense by period, cash flow, period
comparison and averages (SRD 5.9; parity roadmap item 4).

These are the aggregations Quicken ships as windows, built as pure functions
first so an LLM tool (the MCP server) and a report window compose the same
deterministic numbers. Every amount is SIGNED integer cents in the ledger's
convention -- negative is money out -- so an income row is positive, an expense
row negative, and ``net = income + expense``. A refund lands on its expense
category and shrinks that category's outflow rather than counting as income;
that is what a person means by "what did groceries cost this month".

Categories are classified income/expense the way the rest of the app does
(:mod:`mammon.category_types`: by the sign of the category's net over the
whole ledger), so the same category never flips sections between reports.
Uncategorized money is one pseudo-row classified by its own net in the window.

Buckets: ``"month"`` (``2026-01``), ``"quarter"`` (``2026-Q1``), ``"year"``
(``2026``) or ``"total"`` (one bucket). A range always yields every bucket it
spans, activity or not, so a table has a column for the quiet month too.
"""
from __future__ import annotations

import calendar
import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional

from mammon import category_types, ledger
from mammon.category_types import EXPENSE, INCOME
from mammon.reports._lines import (category_paths, resolve_accounts,
                                   signed_lines, validate_date)

UNCATEGORIZED = "Uncategorized"
BUCKETS = ("month", "quarter", "year", "total")


# ---------------------------------------------------------------------------
# bucket helpers
# ---------------------------------------------------------------------------
def bucket_of(date: str, bucket: str) -> str:
    """The bucket key an ISO date falls in."""
    if bucket == "month":
        return date[:7]
    if bucket == "quarter":
        return f"{date[:4]}-Q{(int(date[5:7]) - 1) // 3 + 1}"
    if bucket == "year":
        return date[:4]
    if bucket == "total":
        return "total"
    raise ValueError(f"unknown bucket {bucket!r}; use month|quarter|year|total")


def buckets_in(start: str, end: str, bucket: str) -> list[str]:
    """Every bucket key from ``start`` through ``end``, in order, whether or not
    anything happened in it."""
    validate_date(start)
    validate_date(end)
    if bucket == "total":
        return ["total"]
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}; use month|quarter|year|total")
    d0 = _dt.date.fromisoformat(start)
    d1 = _dt.date.fromisoformat(end)
    if d1 < d0:
        d0, d1 = d1, d0
    keys: list[str] = []
    y, m = d0.year, d0.month
    while (y, m) <= (d1.year, d1.month):
        key = bucket_of(f"{y:04d}-{m:02d}-01", bucket)
        if not keys or keys[-1] != key:
            keys.append(key)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return keys


def bucket_end(key: str, bucket: str, end: str) -> str:
    """The last day of a bucket, clamped to the report's ``end``."""
    if bucket == "total":
        return end
    if bucket == "year":
        last = f"{key}-12-31"
    elif bucket == "quarter":
        y, q = key.split("-Q")
        m = int(q) * 3
        last = f"{y}-{m:02d}-{calendar.monthrange(int(y), m)[1]:02d}"
    else:
        y, m = int(key[:4]), int(key[5:7])
        last = f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
    return min(last, end)


def _round_div(cents: int, n: int) -> int:
    if n <= 0:
        return 0
    return int((Decimal(cents) / Decimal(n)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# income vs. expense by period
# ---------------------------------------------------------------------------
@dataclass
class FlowRow:
    """One category's signed cents per bucket. ``top`` is the top-level name,
    for callers that want to roll a hierarchy up themselves."""

    category_id: Optional[int]
    path: str
    top: str
    type: str                      # income | expense
    by_bucket: dict[str, int]
    total: int


@dataclass
class IncomeExpenseReport:
    start: str
    end: str
    bucket: str
    buckets: list[str]
    account_ids: list[int]
    income: list[FlowRow]          # largest first
    expense: list[FlowRow]         # most negative first
    income_by_bucket: dict[str, int] = field(default_factory=dict)
    expense_by_bucket: dict[str, int] = field(default_factory=dict)
    net_by_bucket: dict[str, int] = field(default_factory=dict)
    total_income: int = 0
    total_expense: int = 0
    net: int = 0

    def rows(self) -> list[FlowRow]:
        return self.income + self.expense


def income_expense(conn, start: str, end: str, *, bucket: str = "month",
                   account_ids: Optional[Iterable[int]] = None,
                   include_hidden: bool = False,
                   include_scheduled: bool = False) -> IncomeExpenseReport:
    """Every category's signed net per bucket over ``[start, end]``, income
    rows then expense rows, with per-bucket and grand totals. Transfers are
    excluded (they are neither); a split contributes its lines."""
    keys = buckets_in(start, end, bucket)
    acct_list = resolve_accounts(conn, account_ids, include_hidden)
    lines = signed_lines(conn, start, end, acct_list, include_scheduled=include_scheduled)
    paths = category_paths(conn)
    types = category_types.classify_categories(conn)
    agg: dict[Optional[int], dict[str, int]] = {}
    for ln in lines:
        b = bucket_of(ln.date, bucket)
        d = agg.setdefault(ln.category_id, {})
        d[b] = d.get(b, 0) + ln.amount
    rows: list[FlowRow] = []
    for cid, byb in agg.items():
        total = sum(byb.values())
        if cid is None:
            path = top = UNCATEGORIZED
            typ = INCOME if total > 0 else EXPENSE
        else:
            path = paths.get(cid, f"#{cid}")
            top = path.split(":")[0]
            typ = types.get(cid, EXPENSE)
        rows.append(FlowRow(cid, path, top, typ, {k: byb.get(k, 0) for k in keys}, total))
    income = sorted((r for r in rows if r.type == INCOME),
                    key=lambda r: (-r.total, r.path.lower()))
    expense = sorted((r for r in rows if r.type == EXPENSE),
                     key=lambda r: (r.total, r.path.lower()))
    rep = IncomeExpenseReport(start=start, end=end, bucket=bucket, buckets=keys,
                              account_ids=acct_list, income=income, expense=expense)
    for k in keys:
        rep.income_by_bucket[k] = sum(r.by_bucket.get(k, 0) for r in income)
        rep.expense_by_bucket[k] = sum(r.by_bucket.get(k, 0) for r in expense)
        rep.net_by_bucket[k] = rep.income_by_bucket[k] + rep.expense_by_bucket[k]
    rep.total_income = sum(r.total for r in income)
    rep.total_expense = sum(r.total for r in expense)
    rep.net = rep.total_income + rep.total_expense
    return rep


# ---------------------------------------------------------------------------
# cash flow (income vs. expense over one window, plus transfers out of the set)
# ---------------------------------------------------------------------------
@dataclass
class TransferRow:
    """Net movement between the selected accounts and one account outside
    them. Negative = money left the selected accounts."""

    account_id: int
    name: str
    cents: int


@dataclass
class CashFlowReport:
    start: str
    end: str
    account_ids: list[int]
    income: list[FlowRow]
    expense: list[FlowRow]
    transfers: list[TransferRow]
    total_income: int
    total_expense: int
    net_transfers: int
    net: int                       # income + expense + transfers


def cash_flow(conn, start: str, end: str, *,
              account_ids: Optional[Iterable[int]] = None,
              include_hidden: bool = False,
              include_scheduled: bool = False) -> CashFlowReport:
    """Quicken's Cash Flow: inflows by category, outflows by category, and --
    when the report covers a SUBSET of accounts -- the transfers to and from
    accounts outside that subset, which really did move money out of or into
    it. Over every account the transfers section is empty by construction, so
    ``net`` is simply what the ledger gained or lost."""
    ie = income_expense(conn, start, end, bucket="total", account_ids=account_ids,
                        include_hidden=include_hidden, include_scheduled=include_scheduled)
    acct_list = ie.account_ids
    by_acct: dict[int, int] = {}
    for ln in signed_lines(conn, start, end, acct_list, transfers="external",
                           include_scheduled=include_scheduled):
        if ln.transfer_account_id is None:
            continue
        by_acct[ln.transfer_account_id] = by_acct.get(ln.transfer_account_id, 0) + ln.amount
    transfers: list[TransferRow] = []
    for aid, cents in by_acct.items():
        acct = ledger.get_account(conn, aid)
        transfers.append(TransferRow(aid, acct["name"] if acct else f"#{aid}", cents))
    transfers.sort(key=lambda r: (r.cents, r.name.lower()))
    net_transfers = sum(r.cents for r in transfers)
    return CashFlowReport(
        start=start, end=end, account_ids=acct_list, income=ie.income,
        expense=ie.expense, transfers=transfers, total_income=ie.total_income,
        total_expense=ie.total_expense, net_transfers=net_transfers,
        net=ie.total_income + ie.total_expense + net_transfers)


# ---------------------------------------------------------------------------
# period comparison
# ---------------------------------------------------------------------------
@dataclass
class ComparisonRow:
    category_id: Optional[int]
    path: str
    type: str
    a_cents: int
    b_cents: int
    delta: int                     # b - a
    pct: Optional[float]           # change from a to b, in percent; None when a is 0


@dataclass
class ComparisonTotal:
    label: str                     # income | expense | net
    a_cents: int
    b_cents: int
    delta: int
    pct: Optional[float]


@dataclass
class ComparisonReport:
    a: tuple[str, str]
    b: tuple[str, str]
    account_ids: list[int]
    rows: list[ComparisonRow]      # income first, then expense, by |delta| desc
    totals: list[ComparisonTotal]


def _pct(a: int, b: int) -> Optional[float]:
    if a == 0:
        return None
    return round((b - a) / abs(a) * 100.0, 1)


def compare_periods(conn, a_start: str, a_end: str, b_start: str, b_end: str, *,
                    account_ids: Optional[Iterable[int]] = None,
                    include_hidden: bool = False,
                    include_scheduled: bool = False) -> ComparisonReport:
    """Every category's signed net in period A against period B ("this quarter
    versus the same quarter last year"), with the change in cents and percent.
    A category active in only one period appears with 0 in the other."""
    ra = income_expense(conn, a_start, a_end, bucket="total", account_ids=account_ids,
                        include_hidden=include_hidden, include_scheduled=include_scheduled)
    rb = income_expense(conn, b_start, b_end, bucket="total", account_ids=account_ids,
                        include_hidden=include_hidden, include_scheduled=include_scheduled)
    seen: dict[tuple, dict] = {}
    for rep, key in ((ra, "a"), (rb, "b")):
        for r in rep.rows():
            slot = seen.setdefault((r.path, r.category_id), {
                "category_id": r.category_id, "path": r.path, "type": r.type, "a": 0, "b": 0})
            slot[key] = r.total
            slot["type"] = r.type
    rows = [ComparisonRow(s["category_id"], s["path"], s["type"], s["a"], s["b"],
                          s["b"] - s["a"], _pct(s["a"], s["b"])) for s in seen.values()]
    rows.sort(key=lambda r: (0 if r.type == INCOME else 1, -abs(r.delta), r.path.lower()))
    totals = []
    for label, av, bv in (("income", ra.total_income, rb.total_income),
                          ("expense", ra.total_expense, rb.total_expense),
                          ("net", ra.net, rb.net)):
        totals.append(ComparisonTotal(label, av, bv, bv - av, _pct(av, bv)))
    return ComparisonReport(a=(a_start, a_end), b=(b_start, b_end),
                            account_ids=ra.account_ids, rows=rows, totals=totals)


# ---------------------------------------------------------------------------
# averages per bucket
# ---------------------------------------------------------------------------
@dataclass
class AverageRow:
    category_id: Optional[int]
    path: str
    type: str
    total: int
    average: int                   # per bucket, ROUND_HALF_UP


@dataclass
class AveragesReport:
    start: str
    end: str
    bucket: str
    n_buckets: int
    account_ids: list[int]
    rows: list[AverageRow]
    average_income: int
    average_expense: int
    average_net: int


def category_averages(conn, start: str, end: str, *, bucket: str = "month",
                      account_ids: Optional[Iterable[int]] = None,
                      include_hidden: bool = False,
                      include_scheduled: bool = False) -> AveragesReport:
    """Each category's average signed net per bucket over the range. The
    divisor is the number of buckets the range SPANS, quiet ones included --
    a bill paid in four of twelve months averages a third of its size, which
    is the number a budget wants."""
    if bucket == "total":
        raise ValueError("averages need month|quarter|year buckets")
    ie = income_expense(conn, start, end, bucket=bucket, account_ids=account_ids,
                        include_hidden=include_hidden, include_scheduled=include_scheduled)
    n = len(ie.buckets)
    rows = [AverageRow(r.category_id, r.path, r.type, r.total, _round_div(r.total, n))
            for r in ie.rows()]
    return AveragesReport(start=start, end=end, bucket=bucket, n_buckets=n,
                          account_ids=ie.account_ids, rows=rows,
                          average_income=_round_div(ie.total_income, n),
                          average_expense=_round_div(ie.total_expense, n),
                          average_net=_round_div(ie.net, n))

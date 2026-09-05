"""mammon.reports.budget -- multi-period budget-vs-actual roll-ups (SRD 5.9;
parity roadmap item 6).

:mod:`mammon.budgets` compares one budget against one month
(:func:`mammon.budgets.budget_vs_actual`). A plan, though, is read across a
span -- "how did the quarter go", "where do I stand year-to-date" -- so this
report layer stacks that single-period primitive across a range of months and
sums it, staying pure and read-only like every other module in
:mod:`mammon.reports`.

Two properties are load-bearing and inherited straight from the domain layer,
not re-implemented here:

* **Actuals are never stored.** Every actual comes from calling
  :func:`mammon.budgets.budget_vs_actual` per month, which derives spending
  read-only from the ledger. This module never touches transaction rows and is
  never a second write path -- it only adds cents that another function already
  computed.
* **Money is signed integer cents**, actuals a positive magnitude (money out),
  ``remaining = carried_in + budgeted - actual`` (negative = overspent) --
  identical to :class:`mammon.budgets.BudgetActualRow`, so summing across
  periods is just integer addition with no rounding and no unit change. The
  per-period primitive already consumes each rollover line's carry, so a
  year-to-date span picks up the remainder carried across the January boundary
  for free; a category's range ``carried_in`` is its opening balance (the carry
  into the first period it appears in), recorded once so it never double-counts.

The report returns three views of the same numbers: ``category_totals`` (each
category summed over the whole span), ``period_totals`` (each month's own
budgeted/actual/remaining), and the grand totals -- plus ``by_period`` keeping
the untouched per-month category rows for a caller that wants the detail.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from mammon import budgets
# NB: import the ``budgets`` *module* object, never a name from it. This module
# is pulled in through ``mammon.reports.__init__`` while ``mammon.budgets`` is
# still mid-import (budgets -> reports.spending -> reports.__init__ -> here), so
# ``budgets.BudgetActualRow`` is not defined yet at import time. The module
# object exists in ``sys.modules`` and its attributes are only ever read at call
# time, so binding the module is safe; ``from mammon.budgets import X`` is not.


@dataclass
class BudgetCategoryTotal:
    """One category's planned-vs-spent summed across every period in the range.

    ``actual_cents`` is a positive magnitude (money out). ``carried_in_cents`` is
    the remainder rolled *into the first period of the range* for a rollover line
    -- the opening envelope balance, which may predate the range (e.g. December
    carrying into a year-to-date span). ``remaining_cents`` is
    ``carried_in_cents + budgeted_cents - actual_cents``: the envelope balance at
    the end of the range (negative = overspent). For a non-rollover category
    ``carried_in_cents`` is 0 and this stays ``budgeted - actual``.
    """
    category_id: int
    category_name: str
    budgeted_cents: int
    actual_cents: int
    remaining_cents: int
    carried_in_cents: int = 0


@dataclass
class BudgetPeriodTotal:
    """One period's grand totals across all of its categories.

    ``carried_in_cents`` sums each category's carry into this month;
    ``remaining_cents`` is ``budgeted_cents + carried_in_cents - actual_cents``.
    """
    period: str                # ISO 'YYYY-MM'
    budgeted_cents: int
    actual_cents: int
    remaining_cents: int
    carried_in_cents: int = 0


@dataclass
class BudgetRangeReport:
    """A budget compared against actuals over a contiguous span of months.

    ``periods`` are the ISO ``'YYYY-MM'`` months in order. ``category_totals``
    sum each category over the whole span; ``period_totals`` give each month's
    own totals; ``total_*`` are the grand totals; ``by_period`` maps each month
    to its raw :class:`mammon.budgets.BudgetActualRow` list for callers wanting
    the per-month detail.
    """
    budget_id: int
    budget_name: str
    periods: list[str]
    category_totals: list[BudgetCategoryTotal]
    period_totals: list[BudgetPeriodTotal]
    total_budgeted_cents: int
    total_actual_cents: int
    total_remaining_cents: int
    total_carried_in_cents: int = 0
    by_period: dict[str, list["budgets.BudgetActualRow"]] = field(default_factory=dict)


def _months(start_period: str, end_period: str) -> list[str]:
    """Every ISO ``'YYYY-MM'`` month from ``start_period`` to ``end_period``
    inclusive. Raises if either is malformed or the end precedes the start."""
    sy, sm = budgets._split_period(start_period)
    ey, em = budgets._split_period(end_period)
    if (ey, em) < (sy, sm):
        raise ValueError(
            f"end period {end_period!r} precedes start {start_period!r}")
    out: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def budget_vs_actual_range(conn, budget_id: int, start_period: str,
                           end_period: str, *,
                           include_unbudgeted: bool = True) -> BudgetRangeReport:
    """Budget vs actual for every month from ``start_period`` to ``end_period``.

    Both bounds are ISO ``'YYYY-MM'`` months and the range is inclusive. Each
    month is evaluated with :func:`mammon.budgets.budget_vs_actual` (so
    transfers are excluded and splits honoured exactly as elsewhere), then
    summed per category and per period. A category that appears in only some
    months contributes 0 to the others, so its totals still line up. Category
    totals are ordered by name; period totals follow the calendar.

    With ``include_unbudgeted`` (the default) a category spent in a month with
    no line for it is still counted, with ``budgeted_cents == 0`` for that
    month -- surfacing spending that escaped the plan.
    """
    budget = budgets.get_budget(conn, budget_id)
    if budget is None:
        raise ValueError(f"no budget with id {budget_id}")

    periods = _months(start_period, end_period)

    by_period: dict[str, list[BudgetActualRow]] = {}
    period_totals: list[BudgetPeriodTotal] = []
    # category_id -> {name, budgeted, actual}, accumulated across periods.
    acc: dict[int, dict] = {}

    for period in periods:
        rows = budgets.budget_vs_actual(
            conn, budget_id, period, include_unbudgeted=include_unbudgeted)
        by_period[period] = rows
        p_budgeted = p_actual = p_carried = 0
        for r in rows:
            slot = acc.get(r.category_id)
            if slot is None:
                # The carry into the FIRST period a category appears in is its
                # opening envelope balance for the whole range (it may predate
                # the range). Later periods' carry telescopes through the
                # budgeted/actual sums, so recording it once avoids double count.
                slot = acc[r.category_id] = {
                    "name": r.category_name, "budgeted": 0, "actual": 0,
                    "carried_in": r.carried_in_cents}
            if not slot["name"] and r.category_name:
                slot["name"] = r.category_name
            slot["budgeted"] += r.budgeted_cents
            slot["actual"] += r.actual_cents
            p_budgeted += r.budgeted_cents
            p_actual += r.actual_cents
            p_carried += r.carried_in_cents
        period_totals.append(BudgetPeriodTotal(
            period=period, budgeted_cents=p_budgeted, actual_cents=p_actual,
            remaining_cents=p_budgeted + p_carried - p_actual,
            carried_in_cents=p_carried))

    category_totals = [
        BudgetCategoryTotal(
            category_id=cid, category_name=slot["name"],
            budgeted_cents=slot["budgeted"], actual_cents=slot["actual"],
            remaining_cents=slot["carried_in"] + slot["budgeted"] - slot["actual"],
            carried_in_cents=slot["carried_in"])
        for cid, slot in acc.items()
    ]
    category_totals.sort(key=lambda r: (r.category_name.lower(), r.category_id))

    total_budgeted = sum(slot["budgeted"] for slot in acc.values())
    total_actual = sum(slot["actual"] for slot in acc.values())
    total_carried = sum(slot["carried_in"] for slot in acc.values())

    return BudgetRangeReport(
        budget_id=budget_id, budget_name=budget.name, periods=periods,
        category_totals=category_totals, period_totals=period_totals,
        total_budgeted_cents=total_budgeted, total_actual_cents=total_actual,
        total_remaining_cents=total_carried + total_budgeted - total_actual,
        total_carried_in_cents=total_carried,
        by_period=by_period)


def budget_vs_actual_ytd(conn, budget_id: int, year: int, *,
                         through_month: int = 12,
                         include_unbudgeted: bool = True) -> BudgetRangeReport:
    """Year-to-date roll-up: January through ``through_month`` of ``year``.

    A thin wrapper over :func:`budget_vs_actual_range` that pins the span to one
    calendar year. ``through_month`` (1..12, default December) caps the last
    month included, so a mid-year "where do I stand" asks for
    ``through_month`` = the current month.
    """
    y = int(year)
    if not 1 <= int(through_month) <= 12:
        raise ValueError(f"through_month must be 1..12, got {through_month!r}")
    start = f"{y:04d}-01"
    end = f"{y:04d}-{int(through_month):02d}"
    return budget_vs_actual_range(
        conn, budget_id, start, end, include_unbudgeted=include_unbudgeted)

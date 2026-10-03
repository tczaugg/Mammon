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

import datetime as _dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

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
    #: A budget GROUP (a named pot) rather than a category; ``category_id`` is
    #: then :func:`mammon.budgets.group_key` of it, and ``group_id`` the group.
    is_group: bool = False
    group_id: Optional[int] = None


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
    transfers are excluded and splits honored exactly as elsewhere), then
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
                    "carried_in": r.carried_in_cents,
                    "is_group": r.is_group, "group_id": r.group_id}
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
            carried_in_cents=slot["carried_in"], is_group=slot["is_group"],
            group_id=slot["group_id"] if slot["is_group"] else None)
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


@dataclass
class GoalRow:
    """One savings goal as the budget reads it for a single month (SRD 5.12g).

    ``planned_cents`` is what the goal says it will contribute this month.
    ``funded_this_month_cents`` is what actually went in -- and it comes from
    the goal's own funding query, NOT from the budget's spending actuals.
    Funding a goal is a transfer, and every spending aggregation in this
    codebase excludes transfers on purpose; reading it out of actuals would
    have meant inverting that rule for one caller.

    Every money field is a positive magnitude in cents.
    """

    goal_id: int
    name: str
    target_cents: int
    funded_cents: int
    remaining_cents: int
    planned_cents: int
    funded_this_month_cents: int
    required_cents: int
    shortfall_cents: int
    months_left: Optional[int]
    projected_month: Optional[str]
    state: str
    target_date: Optional[str] = None
    account_id: Optional[int] = None
    category_id: Optional[int] = None

    @property
    def variance_cents(self) -> int:
        """This month's funding less the plan; negative means underfunded."""
        return self.funded_this_month_cents - self.planned_cents


@dataclass
class GoalTable:
    """Every goal attached to one budget, for one ISO ``YYYY-MM`` period."""

    budget_id: Optional[int]
    period: str
    rows: list[GoalRow] = field(default_factory=list)

    @property
    def total_planned_cents(self) -> int:
        return sum(r.planned_cents for r in self.rows)

    @property
    def total_funded_this_month_cents(self) -> int:
        return sum(r.funded_this_month_cents for r in self.rows)

    @property
    def total_target_cents(self) -> int:
        return sum(r.target_cents for r in self.rows)

    @property
    def total_funded_cents(self) -> int:
        return sum(r.funded_cents for r in self.rows)


def goal_table(conn, budget_id: Optional[int] = None, period: Optional[str] = None,
               *, include_archived: bool = False,
               as_of: Optional[str] = None) -> GoalTable:
    """Read-only goal roll-up for one month: plan, progress and pace.

    ``budget_id`` ``None`` means every goal, attached to a budget or not, which
    is what the Save pane shows before anyone has wired a goal into a plan.
    Progress is measured as of the last day of ``period`` unless ``as_of``
    overrides it, so looking at a past month reports where the goal stood THEN
    rather than where it stands now.

    Pure: this returns plain data structures and writes nothing, like every
    other function in :mod:`mammon.reports`.
    """
    from mammon import goals as _goals

    per = period or (as_of or _dt.date.today().isoformat())[:7]
    year, month = budgets._split_period(per)
    _, month_end = budgets.month_bounds(year, month)
    when = as_of or month_end

    rows: list[GoalRow] = []
    for goal in _goals.list_goals(conn, include_archived=include_archived,
                                  budget_id=budget_id):
        progress = _goals.goal_progress(conn, goal.id, as_of=when)
        rows.append(GoalRow(
            goal_id=goal.id,
            name=goal.name,
            target_cents=progress.target_cents,
            funded_cents=progress.funded_cents,
            remaining_cents=progress.remaining_cents,
            planned_cents=progress.monthly_cents,
            funded_this_month_cents=_goals.month_funding(conn, goal.id, per),
            required_cents=progress.required_cents,
            shortfall_cents=progress.shortfall_cents,
            months_left=progress.months_left,
            projected_month=progress.projected_month,
            state=progress.state,
            target_date=goal.target_date,
            account_id=goal.account_id,
            category_id=goal.category_id))
    return GoalTable(budget_id=budget_id, period=per, rows=rows)


# ---------------------------------------------------------------------------
# The retirement seam: what the budget implies for retirement spending
# ---------------------------------------------------------------------------
# One figure crosses from the budget to the Retirement Planner: an annual
# spending LEVEL in base-year cents. It is MEASURED (a plan the user wrote, or
# twelve months of the ledger), never indexed and never inflated, which is
# why it can live here instead of in :mod:`mammon.retirement`: the planner owns
# every figure that moves with law or annual indexing, and this module must not
# hold a single one of them. The seam runs ONE direction -- nothing here reads
# the planner, and the planner never writes a budget row.
#
# The words below classify a category by its PATH, because no schema column ties
# a category to a loan or to work: ``categories`` carries an id and a path and
# nothing else. Keyword matching is therefore the only evidence available, and
# it is presented as evidence -- every candidate line states why it was picked,
# and the user confirms or clears it before anything is subtracted.
_BASIS_MONTHS = 12

#: Escrow, property tax and insurance are NEVER candidates, even though a path
#: like 'Home:Mortgage Escrow' contains 'mortgage': the house still costs those
#: after the note is paid off. Checked FIRST so the mortgage words cannot claim
#: them.
_ESCROW_WORDS = ("escrow", "property tax", "real estate tax", "homeowner",
                 "home insurance", "hazard insurance", "flood insurance",
                 "pmi", "mortgage insurance", "hoa", "assessment")

#: Money set aside rather than spent.
_SAVING_WORDS = ("saving", "savings", "contribution", "contributions",
                 "401k", "403b", "457", "ira", "roth", "hsa contribution",
                 "emergency fund", "sinking fund", "college fund", "529")

_MORTGAGE_WORDS = ("mortgage", "home loan", "principal and interest")

_DEBT_WORDS = ("loan", "auto payment", "car payment", "note payment",
               "card payment", "debt payment", "debt service", "payoff")

#: Tax the household never pays out of retirement spending, because the planner
#: computes its own tax on top of the level this seam hands over.
_WITHHELD_WORDS = ("payroll tax", "income tax", "tax withheld", "withholding",
                   "withheld", "fica", "federal tax", "state tax",
                   "social security tax", "medicare tax", "self-employment tax")

_WORK_WORDS = ("commute", "commuting", "parking", "work lunch", "work clothes",
               "dry cleaning", "union dues", "professional dues",
               "professional fees", "continuing education", "job ")

_HEALTH_WORDS = ("health insurance", "medical insurance", "health premium",
                 "dental insurance", "vision insurance", "cobra")


@dataclass
class ExcludedLine:
    """One line of the subtraction table: a name, its cents, and its reason.

    ``reason`` is the point of the row -- it says what evidence picked the line
    (a keyword in the path, a goal link, an amortization schedule that ends
    before the retirement year), so the user can disagree with it. It states
    facts and cites them; it never recommends.

    ``key`` is a stable string (``'cat:12'``, ``'acct:5'``) that a caller passes
    back in ``exclude_keys`` to confirm or clear the line, so a dialog toggling
    rows re-derives no money -- it asks this module again.

    ``default_on`` records whether the line is subtracted when nobody has
    confirmed anything. Debt service that clears first, savings contributions
    and withheld tax default on; work-related spending and health-insurance
    premiums are OFFERED and default off.
    """
    key: str
    category_name: str
    cents: int
    reason: str
    category_id: Optional[int] = None
    account_id: Optional[int] = None
    default_on: bool = True


@dataclass
class RetirementSpendingBasis:
    """What a budget implies the household spends in a year, ready to hand over.

    ``annual_cents`` is the figure that crosses the seam: base-year dollars,
    **not inflated**, measured in ``basis_year``. The planner applies its own
    increase rate to it, so inflating here would compound twice.

    The table the user sees is the arithmetic: ``total_cents`` (the window's whole
    plan), each line of ``excluded`` with its cents and its reason,
    ``included_cents`` as the remainder, and ``annual_cents`` -- equal to
    ``included_cents`` when a full twelve months were observed, scaled up to a
    year otherwise. ``offered`` holds the candidates that were NOT subtracted and
    await the user's confirmation.

    ``source`` is ``'budget'`` when the user has actually set a plan and
    ``'trailing12'`` when the basis falls back to twelve months of measured
    spending. ``coverage_pct`` is the budget's category coverage over the window
    (0-100), the honesty check on a plan that covers only part of the money;
    measured spending is 100 by definition. ``note`` is the one-line provenance
    string, computed here and never hardcoded, so whatever the planner shows
    beside the field describes the numbers actually used.

    ``retirement_year`` is the year the debt lines were actually compared
    against, ``None`` if none was available. It is reported back because it may
    have been RESOLVED rather than passed - the household's own stated retirement
    year, read from :mod:`mammon.retirement`, is used when a caller gives none -
    and a figure a debt line was subtracted on must be visible to the user who is
    about to accept the subtraction.
    """
    annual_cents: int
    basis_year: int
    source: str                      # 'budget' | 'trailing12'
    budget_id: Optional[int]
    months_observed: int
    total_cents: int
    included_cents: int
    excluded: list[ExcludedLine] = field(default_factory=list)
    offered: list[ExcludedLine] = field(default_factory=list)
    coverage_pct: Decimal = Decimal("0.0")
    periods: list[str] = field(default_factory=list)
    note: str = ""
    retirement_year: Optional[int] = None
    #: Payroll deductions measured from the window's paychecks and ADDED to a
    #: budget basis (SRD 5.12i): the budget is planned from take-home pay, so
    #: withholding and premiums are not lines in it, but they are part of what
    #: the household spends and the planner must see them. Inside
    #: ``total_cents``; 0 for measured spending, which already includes them.
    deductions_cents: int = 0

    @property
    def start_period(self) -> str:
        """First ISO ``'YYYY-MM'`` month of the basis window."""
        return self.periods[0] if self.periods else ""

    @property
    def end_period(self) -> str:
        """Last ISO ``'YYYY-MM'`` month of the basis window."""
        return self.periods[-1] if self.periods else ""


def _trailing_periods(end_period: str, months: int = _BASIS_MONTHS) -> list[str]:
    """The ``months`` ISO ``'YYYY-MM'`` periods ending at ``end_period``."""
    year, month = budgets._split_period(end_period)
    out: list[str] = []
    for _ in range(months):
        out.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month < 1:
            year, month = year - 1, 12
    return list(reversed(out))


def _norm(text: str) -> str:
    """A category path lowercased with separators turned into spaces, so a
    keyword can be matched without caring how the path is punctuated."""
    out = []
    for ch in (text or "").lower():
        out.append(ch if (ch.isalnum() or ch == "-") else " ")
    return " ".join("".join(out).split())


def _has_word(text: str, words) -> Optional[str]:
    """The first of ``words`` appearing in the normalized ``text``, or ``None``.

    Single words match on a word boundary ('ira' must not match 'spiral');
    phrases match as substrings of the normalized path.
    """
    padded = f" {text} "
    for word in words:
        needle = _norm(word)
        if not needle:
            continue
        if " " in needle or "-" in needle:
            if needle in text:
                return word
        elif f" {needle} " in padded:
            return word
    return None


def _loan_payoff_years(conn) -> dict[int, tuple[str, Optional[int]]]:
    """``account_id -> (account name, payoff year)`` for every loan in the file.

    Loan-ness is a ``loan_params`` row on an ordinary liability account, so this
    walks the accounts and asks :mod:`mammon.loans` for the schedule. The payoff
    year is the year of the schedule's last row; ``None`` when the loan's
    parameters are too incomplete to date a schedule (no origination date, say),
    which is reported as unknown rather than guessed.
    """
    from mammon import ledger as _ledger   # lazy: neither imports this module
    from mammon import loans as _loans

    out: dict[int, tuple[str, Optional[int]]] = {}
    for acct in _ledger.list_accounts(conn, include_closed=True,
                                      include_hidden=True):
        aid = int(acct["id"])
        if _loans.get_loan_params(conn, aid) is None:
            continue
        year: Optional[int] = None
        try:
            schedule = _loans.amortization_schedule(conn, aid)
        except (LookupError, ValueError):
            schedule = []
        if schedule:
            try:
                year = int(schedule[-1].date[:4])
            except (TypeError, ValueError):
                year = None
        out[aid] = (acct["name"] or "", year)
    return out


def _payoff_for(path: str, payoffs: dict[int, tuple[str, Optional[int]]]
                ) -> tuple[Optional[int], Optional[str], Optional[int]]:
    """Match a category path to a loan account by name overlap.

    Returns ``(account_id, account_name, payoff_year)``, all ``None`` when no
    loan account looks like this category. The match is deliberately weak
    evidence -- it scores the distinctive words the two names share -- and it is
    only ever used to WRITE A REASON the user then confirms.
    """
    stop = {"loan", "payment", "payments", "expense", "expenses", "the", "and",
            "of", "my", "our", "account", "principal", "interest"}
    want = {w for w in _norm(path).split() if w not in stop and len(w) > 2}
    best: tuple[int, Optional[int], Optional[str], Optional[int]] = (0, None, None, None)
    for aid, (name, year) in sorted(payoffs.items()):
        have = {w for w in _norm(name).split() if w not in stop and len(w) > 2}
        score = len(want & have)
        if score > best[0]:
            best = (score, aid, name, year)
    if best[0] <= 0:
        return None, None, None
    return best[1], best[2], best[3]


def _classify(path: str, cents: int, category_id: int,
              goal_categories: dict[int, str],
              payoffs: dict[int, tuple[str, Optional[int]]],
              retirement_year: Optional[int]) -> Optional[ExcludedLine]:
    """One category's candidacy for exclusion, or ``None`` to leave it in.

    First match wins, and the order matters: escrow is checked before the
    mortgage words so a path containing both stays in the basis, and the goal
    link is checked before the saving words because a linked goal is stronger
    evidence than a keyword.
    """
    text = _norm(path)
    key = f"cat:{category_id}"

    if _has_word(text, _ESCROW_WORDS):
        # Not a candidate at all: the house costs escrow, property tax and
        # insurance after the note is paid off, so this money stays in.
        return None

    goal_name = goal_categories.get(category_id)
    if goal_name:
        return ExcludedLine(
            key=key, category_name=path, cents=cents, category_id=category_id,
            reason=(f"Funds the savings goal {goal_name!r}: money set aside, "
                    f"not spent."),
            default_on=True)

    if _has_word(text, _SAVING_WORDS):
        return ExcludedLine(
            key=key, category_name=path, cents=cents, category_id=category_id,
            reason="Savings or retirement contribution: money set aside, not spent.",
            default_on=True)

    if _has_word(text, _WITHHELD_WORDS):
        return ExcludedLine(
            key=key, category_name=path, cents=cents, category_id=category_id,
            reason=("Payroll or income tax withheld: the planner computes tax "
                    "itself on top of this level, so counting it here counts it "
                    "twice."),
            default_on=True)

    mortgage = _has_word(text, _MORTGAGE_WORDS)
    debt = mortgage or _has_word(text, _DEBT_WORDS)
    if debt:
        label = ("Mortgage principal and interest" if mortgage
                 else "Debt service")
        aid, name, payoff = _payoff_for(path, payoffs)
        if payoff is not None and retirement_year is not None and payoff < retirement_year:
            return ExcludedLine(
                key=key, category_name=path, cents=cents, category_id=category_id,
                account_id=aid,
                reason=(f"{label}: the amortization schedule for {name!r} ends "
                        f"in {payoff}, before retirement in {retirement_year}. "
                        f"Escrow, property tax and insurance are not part of "
                        f"this line."),
                default_on=True)
        if payoff is not None and retirement_year is not None:
            return ExcludedLine(
                key=key, category_name=path, cents=cents, category_id=category_id,
                account_id=aid,
                reason=(f"{label}: the amortization schedule for {name!r} runs "
                        f"to {payoff}, which is not before retirement in "
                        f"{retirement_year}, so it is offered rather than "
                        f"subtracted."),
                default_on=False)
        if payoff is not None:
            return ExcludedLine(
                key=key, category_name=path, cents=cents, category_id=category_id,
                account_id=aid,
                reason=(f"{label}: {name!r} pays off in {payoff}; no retirement "
                        f"year was given to compare it against, so it is offered "
                        f"rather than subtracted."),
                default_on=False)
        if aid is not None:
            return ExcludedLine(
                key=key, category_name=path, cents=cents, category_id=category_id,
                account_id=aid,
                reason=(f"{label}: matches the loan account {name!r}, whose "
                        f"parameters do not date a schedule, so its payoff year "
                        f"is unknown and it is offered rather than subtracted."),
                default_on=False)
        return ExcludedLine(
            key=key, category_name=path, cents=cents, category_id=category_id,
            reason=(f"{label} by the words in the category path; no loan "
                    f"account in this file matches it, so its payoff date is "
                    f"unknown and it is offered rather than subtracted."),
            default_on=False)

    if _has_word(text, _HEALTH_WORDS):
        return ExcludedLine(
            key=key, category_name=path, cents=cents, category_id=category_id,
            reason=("Health insurance premium: what a household pays before and "
                    "after Medicare differ, so this is offered only and never "
                    "subtracted unless you confirm it."),
            default_on=False)

    if _has_word(text, _WORK_WORDS):
        return ExcludedLine(
            key=key, category_name=path, cents=cents, category_id=category_id,
            reason=("Work-related spending by the words in the category path: "
                    "offered only, until you confirm it ends with the job."),
            default_on=False)

    return None


def _dollars(cents: int) -> str:
    """``123456`` -> ``'1,234.56'``: integer cents to a decimal dollar string
    for a provenance sentence, never a float."""
    sign = "-" if int(cents) < 0 else ""
    whole, part = divmod(abs(int(cents)), 100)
    return f"{sign}{whole:,}.{part:02d}"


def _basis_note(source: str, budget_name: str, periods: list[str],
                months_observed: int, coverage_pct: Decimal,
                excluded: list[ExcludedLine], basis_year: int, *,
                deductions_cents: int = 0) -> str:
    """The one-line provenance string, computed from the numbers actually used."""
    where = (f"budget {budget_name!r}" if source == "budget"
             else "measured spending")
    window = (f"{months_observed} month" + ("" if months_observed == 1 else "s")
              + (f" ending {periods[-1]}" if periods else ""))
    parts = [f"Basis: {where}", window]
    if source == "budget":
        parts.append(f"{coverage_pct} percent category coverage")
        if deductions_cents:
            parts.append(f"plus {_dollars(deductions_cents)} in payroll deductions "
                         f"measured from paychecks")
    if excluded:
        names = "; ".join(line.category_name for line in excluded)
        parts.append(f"less {len(excluded)} "
                     + ("exclusion" if len(excluded) == 1 else "exclusions")
                     + f" ({names})")
    else:
        parts.append("no exclusions")
    return (", ".join(parts) + f". Base-year {basis_year} dollars; the planner "
            f"inflates at its own rate.")


def _household_retirement_year(conn) -> Optional[int]:
    """The earliest retirement year the household has stated, or ``None``.

    ASKED, never re-derived: :mod:`mammon.retirement` owns the figure (it is the
    year a person reaches their planned claim age) and this module reads it
    through that module's own function, so there is no second definition of when
    the household retires. The earliest is taken because it is the first year the
    household's spending pattern changes, and because it is the conservative
    comparison for a debt: a loan running past it is offered rather than
    subtracted.

    Nothing indexed crosses -- a household's own stated year moves with nobody's
    legislation -- and a caller that passes a year explicitly never reaches here.
    """
    from mammon import retirement as _retirement
    years = []
    for person in _retirement.list_people(conn):
        year = _retirement.retirement_year(person)
        if year:
            years.append(int(year))
    return min(years) if years else None


def retirement_spending_basis(conn, budget_id: Optional[int] = None,
                              as_of: Optional[str] = None, *,
                              retirement_year: Optional[int] = None,
                              exclude_keys: Optional[Iterable[str]] = None
                              ) -> RetirementSpendingBasis:
    """What the budget implies the household spends in a year, with its reasons.

    Twelve months ending at ``as_of``'s month (today's, by default) are summed,
    each candidate line is classified, the confirmed ones are subtracted, and the
    remainder is annualized if fewer than twelve months were actually observed.
    The result is BASE-YEAR cents -- see :class:`RetirementSpendingBasis`.

    ``budget_id`` names the plan to read; ``None`` reads the ACTIVE budget, so
    that a caller with no budget in view never has to guess which plan the
    household means. No active budget, or a budget with no lines in the window,
    falls back to twelve months of measured spending out of the household's
    spending accounts (``source == 'trailing12'``).

    ``retirement_year`` is evidence, not a setting: it is what a debt line's
    payoff year is compared against. Passed as ``None`` it is resolved from the
    household's own stated retirement year (see
    :func:`_household_retirement_year`), because a debt that clears first is meant
    to be computed rather than asserted; whichever year was used comes back on the
    result. If the household has stated none either, a debt line is offered rather
    than subtracted, and says so.

    ``exclude_keys`` is the user's confirmation. ``None`` means "use the
    defaults" (savings and goal contributions, withheld tax, and debt that clears
    before retirement); a set means "subtract exactly these", so a dialog toggling
    rows asks this function again instead of doing its own arithmetic.

    Pure and read-only, like every other function in :mod:`mammon.reports`: this
    reads a plan and a ledger and returns plain data structures. It writes
    nothing, and nothing in it moves with law or annual indexing -- those figures
    live in :mod:`mammon.retirement` alone.
    """
    from mammon import goals as _goals
    from mammon import ledger as _ledger
    from mammon.reports import spending as _spending

    end_period = (as_of or _dt.date.today().isoformat())[:7]
    periods = _trailing_periods(end_period)
    basis_year = budgets._split_period(periods[-1])[0]
    confirmed = None if exclude_keys is None else set(exclude_keys)
    if retirement_year is None:
        retirement_year = _household_retirement_year(conn)

    if budget_id:
        budget = budgets.get_budget(conn, budget_id)
    else:
        active = budgets.list_budgets(conn, include_inactive=False)
        budget = active[0] if active else None
    paths = {int(c["id"]): (c["path"] or "") for c in
             _ledger.list_categories(conn, include_hidden=True)}

    # --- the window's plan or measured spending, per category ---------------
    per_category: dict[int, int] = {}
    saving_rows: list[tuple[int, int]] = []       # (account_id, cents)
    months_observed = 0
    source = "trailing12"

    if budget is not None:
        # A GROUP's lines are lines: summed into the window under the group's
        # key and classified by the group's name, so a pot called "Taxes" is
        # offered as withheld tax exactly as a category called Taxes would be.
        for g in budgets.list_groups(conn, budget.id):
            paths[g.key] = g.name
        for period in periods:
            month_cents = 0
            for line in budgets.get_lines(conn, budget.id, period=period):
                if not line.amount_cents:
                    continue
                per_category[line.category_id] = (
                    per_category.get(line.category_id, 0) + line.amount_cents)
                month_cents += line.amount_cents
            for gl in budgets.get_group_lines(conn, budget.id, period=period):
                if not gl.amount_cents:
                    continue
                key = budgets.group_key(gl.group_id)
                per_category[key] = per_category.get(key, 0) + gl.amount_cents
                month_cents += gl.amount_cents
            for sl in budgets.get_saving_lines(conn, budget.id, period=period):
                if not sl.amount_cents:
                    continue
                saving_rows.append((sl.account_id, sl.amount_cents))
                month_cents += sl.amount_cents
            if month_cents:
                months_observed += 1
        if months_observed:
            source = "budget"
        else:
            per_category.clear()
            saving_rows.clear()

    deductions_cents = 0
    if source == "budget":
        # The budget is take-home (SRD 5.12): a paycheck's withholding and
        # premium legs are not lines in it, yet they are what the household
        # spends on tax and health cover, so they come back here from the
        # ledger -- measured facts on paychecks, classified exactly like a
        # category line of the same name would be.
        year, month = budgets._split_period(periods[0])
        w_start, _ = budgets.month_bounds(year, month)
        year, month = budgets._split_period(periods[-1])
        _, w_end = budgets.month_bounds(year, month)
        for cid, cents in budgets.payroll_deductions(conn, w_start, w_end).items():
            if cents:
                per_category[cid] = per_category.get(cid, 0) + cents
                deductions_cents += cents

    if source == "trailing12":
        account_ids = budgets.spending_account_ids(conn)
        for period in periods:
            year, month = budgets._split_period(period)
            start, end = budgets.month_bounds(year, month)
            report = _spending.spending_by_category(conn, start, end,
                                                    account_ids=account_ids)
            month_cents = 0
            for cid, cents in _flatten_spending(report.rows):
                if not cents:
                    continue
                per_category[cid] = per_category.get(cid, 0) + cents
                month_cents += cents
            if month_cents:
                months_observed += 1

    total_cents = sum(per_category.values()) + sum(c for _, c in saving_rows)

    # --- classify every line, then apply the confirmed subtractions ---------
    goal_categories: dict[int, str] = {}
    saving_accounts: dict[int, str] = {}
    if budget is not None:
        for goal in _goals.list_goals(conn, budget_id=budget.id):
            if goal.category_id:
                goal_categories[int(goal.category_id)] = goal.name
            if goal.account_id:
                saving_accounts[int(goal.account_id)] = goal.name
    payoffs = _loan_payoff_years(conn)

    candidates: list[ExcludedLine] = []
    for cid, cents in per_category.items():
        line = _classify(paths.get(cid, f"category {cid}"), cents, cid,
                         goal_categories, payoffs, retirement_year)
        if line is not None:
            candidates.append(line)

    by_account: dict[int, int] = {}
    for aid, cents in saving_rows:
        by_account[aid] = by_account.get(aid, 0) + cents
    accounts = {int(a["id"]): (a["name"] or "")
                for a in _ledger.list_accounts(conn, include_closed=True,
                                               include_hidden=True)}
    for aid, cents in sorted(by_account.items()):
        name = accounts.get(aid, f"account {aid}")
        goal_name = saving_accounts.get(aid)
        reason = ("Savings contribution to " + repr(name) +
                  (f" for the goal {goal_name!r}" if goal_name else "") +
                  ": money set aside, not spent.")
        candidates.append(ExcludedLine(
            key=f"acct:{aid}", category_name=f"Saving: {name}", cents=cents,
            reason=reason, account_id=aid, default_on=True))

    candidates.sort(key=lambda r: (-r.cents, r.category_name.lower()))
    if confirmed is None:
        keep = {c.key for c in candidates if c.default_on}
    else:
        keep = {c.key for c in candidates if c.key in confirmed}
    excluded = [c for c in candidates if c.key in keep]
    offered = [c for c in candidates if c.key not in keep]

    included_cents = total_cents - sum(c.cents for c in excluded)
    if months_observed and months_observed < _BASIS_MONTHS:
        annual_cents = int((Decimal(included_cents) * _BASIS_MONTHS
                            / Decimal(months_observed)
                            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    else:
        annual_cents = included_cents

    if source == "budget":
        year, month = budgets._split_period(periods[0])
        window_start, _ = budgets.month_bounds(year, month)
        year, month = budgets._split_period(periods[-1])
        _, window_end = budgets.month_bounds(year, month)
        days = ((_dt.date.fromisoformat(window_end)
                 - _dt.date.fromisoformat(window_start)).days + 1)
        coverage = budgets.budget_coverage(conn, budget.id, as_of=window_end,
                                          days=days)
    else:
        # Measured spending is its own coverage: every dollar counted came from
        # the ledger, so there is no unbudgeted remainder to warn about.
        coverage = Decimal("100.0")

    return RetirementSpendingBasis(
        annual_cents=annual_cents, basis_year=basis_year, source=source,
        budget_id=(budget.id if budget is not None and source == "budget"
                   else None),
        months_observed=months_observed, total_cents=total_cents,
        included_cents=included_cents, excluded=excluded, offered=offered,
        coverage_pct=coverage, periods=periods,
        retirement_year=retirement_year,
        deductions_cents=deductions_cents,
        note=_basis_note(source, budget.name if budget is not None else "",
                         periods, months_observed, coverage, excluded,
                         basis_year, deductions_cents=deductions_cents))


def _flatten_spending(rows) -> list[tuple[int, int]]:
    """``(category_id, own_cents)`` for every node of a spending tree.

    Own cents only, so a parent and its children never double-count the same
    money.
    """
    out: list[tuple[int, int]] = []
    for row in rows:
        out.append((row.category_id, row.own_cents))
        if getattr(row, "children", None):
            out.extend(_flatten_spending(row.children))
    return out

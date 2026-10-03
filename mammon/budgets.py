"""Budgets: the UI-free domain layer for named per-category monthly targets,
now with rollover carry-over consumed.

A *budget* is a named envelope set the user can toggle ``active`` without
deleting; its *lines* pin one target ``amount_cents`` per ``(category, period)``
where ``period`` is an ISO ``'YYYY-MM'`` month. Keeping the target per-month
(rather than a single annual figure) lets a plan bend around known lumpy months
without inventing a row per day, and the ``UNIQUE(budget_id, category_id,
period)`` key makes :func:`set_line` a true upsert -- editing a target rewrites
the one row instead of accreting duplicates.

The load-bearing rule this module honors: **actuals are never stored.** A
budget only ever records intent; what was *actually* spent is derived read-only
from the ledger by reusing :func:`mammon.reports.spending.spending_by_category`,
so there is exactly one definition of "spending" (transfers excluded, splits
honored, gross outflow as a positive magnitude) and no second write path into
transaction rows -- ``mammon.ledger`` stays the sole writer, per the codebase
invariant. That is why :func:`budget_vs_actual` takes a connection and computes
on the fly rather than caching a total anywhere.

All money is signed integer cents, matching the rest of the app. A line's
``rollover`` flag is now *consumed*: for such a line the net remainder of every
prior rollover period (``budgeted - actual``, summed) is carried into this
period's available amount, so an underspend flows forward as extra headroom and
an overspend eats into the next period. The carry is compared chronologically by
the ISO ``'YYYY-MM'`` period string, which sorts as text in calendar order, so
it crosses the December -> January boundary with no reset -- the year-boundary
case Quicken keeps getting wrong. It is exact integer-cent addition (no division,
so the app's ROUND_HALF_UP cents rule is honored trivially); a ``rollover = 0``
line is unaffected and its ``remaining`` stays ``budgeted - actual``.

On top of the lines sits the per-category *intent* the Budget Planner's Set tab
needs (SRD 5.12b), in ``budget_category_settings``: which of the three buckets a
category is (``fixed`` | ``flex`` | ``nonmonthly``), whether its remainder rolls
forward (``none`` | ``positive`` | ``both``), and, for a non-monthly expense, the
yearly total its twelve monthly lines are spread from. It is keyed by
``(budget_id, category_id)`` and not by a column on ``categories`` because the
intent is per-BUDGET: two scenarios may legitimately bucket the same category
differently, which one column on the category could never express.

The bucket decides whether a line is GRADED. ``flex`` and ``nonmonthly`` are
discretionary: Track reads them as free, committed out or over. ``fixed`` is
non-discretionary -- withholding, a premium, rent -- and is carried at its known
amount so the plan's totals, its coverage and the retirement basis are complete,
but it is never called "over": there is nothing the user is meant to do about a
three-paycheck month. A fixed line that comes from a schedule is written as the
occurrences landing in each month times the amount per occurrence
(:func:`scheduled_amounts`), never the annual total over twelve, so it reads
"as planned" in every month until the pay actually changes.

A **group** (``budget_groups`` + ``budget_group_lines``, schema v111) is a named
pot inside one budget: "Food" over Groceries and Dining, so that cutting one
cannot be replaced by growing the other and still meet the plan. The pot holds
the budget and the members hold the actuals -- a group's budgeted amount is its
own lines (plus any line a member still carries), its actual and committed are
the sums of its members' own figures, and its carry is the ordinary recursion
run over those sums. Joining a group folds the member's existing lines into the
group's, period by period, so nothing is ever distributed by guesswork. A group
row travels through :class:`BudgetActualRow` with ``category_id = -group_id`` and
``is_group = True``; the negative key lets a group share a dictionary with
categories without colliding, and readers test the flag, never the sign.
:func:`budget_vs_actual` leaves member rows OUT unless asked, so every caller
that sums the rows it gets -- the range report, the MCP surface, the outlook,
the burn-down, the retirement basis -- partitions the money exactly without
knowing the rule.

Three more rules hold here and are worth stating because each of them replaces a
tempting shortcut:

* **The spread is exact.** ``annual_cents`` is divided into twelve monthly lines
  by :func:`spread_annual`, which hands the remainder cents out one per month to
  the earliest months, so the twelve lines sum to the annual total to the cent.
  Dividing and rounding each month independently loses (or invents) up to eleven
  cents a year, and a budget that does not add up is a budget the user stops
  trusting.
* **Proposals are not writes.** :func:`seed_from_history` *reads* the ledger and
  *returns* :class:`SeedProposal` rows; nothing is stored until the user accepts,
  at which point :func:`apply_proposals` funnels every amount through
  :func:`set_line`. A seeder that wrote as it computed would make "let me look at
  what it suggests" a destructive act.
* **Saving is budgeted, but never as spending.** A transfer to a savings,
  investment or loan account gets a target per DESTINATION ACCOUNT in
  ``budget_saving_lines`` and is measured, net, by
  :mod:`mammon.reports.saving` (SRD 5.12d). None of it reaches
  :func:`budget_vs_actual`, so a spending total can never include the 401(k).
* **The carry is computed, never stored** -- see ``budget_carry_overrides``, whose
  only job is to let a carry *start again*: an explicit, dated, note-carrying
  row that terminates the recursion. Storing only the override is what keeps the
  computed carry from drifting away from the ledger.
"""
from __future__ import annotations

import calendar
import datetime as _dt
import sqlite3
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional, Sequence

# NB: mammon.reports.spending is imported *lazily* inside budget_vs_actual, not
# here. Importing it at module load pulls in mammon.reports' package __init__,
# which eagerly imports mammon.reports.budget, which imports BudgetActualRow
# back from this module -- a circular import that explodes whenever mammon.budgets
# (or the Budgets UI) is the first thing imported in a process. Deferring the
# import to call time breaks the cycle: by then this module is fully defined.


#: The kinds of budget line. ``fixed`` is a known repeated amount the
#: household cannot move by spending less (rent, a premium paid from checking,
#: a car payment) and is carried, not graded; ``flex`` (the page says
#: "Variable") a per-month target the user tunes (groceries); ``nonmonthly`` an
#: expense that arrives a few times a year and is funded a twelfth at a time.
#: ``flex`` and ``nonmonthly`` are DISCRETIONARY: the page grades them.
#: ``income`` marks a line on an income category as the plan's take-home pay:
#: its actual is money received, not spent, and :func:`budget_vs_actual` leaves
#: it out of the expense rows unless asked.
BUCKETS = ("fixed", "flex", "nonmonthly", "income")

#: The buckets Track grades (free / committed out / over). A ``fixed`` line is
#: reported against its plan and never called over.
DISCRETIONARY_BUCKETS = ("flex", "nonmonthly")

#: How many months a budget spans. The Set tab shows exactly this many columns.
PLAN_MONTHS = 12


def is_discretionary(bucket: str) -> bool:
    """Whether a line of ``bucket`` is graded. An unknown or blank bucket (a
    group whose members disagree) reads as discretionary, the default."""
    return bucket not in ("fixed", "income")


def group_key(group_id: int) -> int:
    """The ``category_id`` a group row travels under: ``-group_id``.

    Negative so a group can share a ``category_id``-keyed dictionary with real
    categories without a collision; readers tell the two apart by
    :attr:`BudgetActualRow.is_group`, never by the sign."""
    return -int(group_id)

#: How a category's remainder moves to the next month. ``positive`` carries an
#: underspend forward but forgives an overspend; ``both`` carries either way.
ROLLOVER_MODES = ("none", "positive", "both")

#: Account kinds whose transactions are household spending. A brokerage fee is a
#: portfolio event, not a budget item, so investment and loan accounts are out.
SPENDING_ACCOUNT_TYPES = ("checking", "savings", "credit", "cash")

#: Sentinel for "this keyword argument was not passed", used where ``None`` is
#: itself a meaningful value to store (``group_id``, ``end_period``, ``note``).
_UNSET = object()


@dataclass
class Budget:
    """A named, toggleable budget (envelope set).

    ``start_period``, ``end_period`` and ``note`` are appended with defaults, so
    every caller written before they existed still constructs a valid Budget.
    ``start_period`` is the defined first month of the plan -- the floor the
    rollover recursion needs to stop at; ``end_period`` is ``None`` for an
    open-ended budget; ``note`` is what a scenario is FOR.
    """
    id: int
    name: str
    active: bool
    start_period: Optional[str] = None
    end_period: Optional[str] = None
    note: Optional[str] = None


@dataclass
class BudgetLine:
    """One target amount for a (category, period) within a budget."""
    id: int
    budget_id: int
    category_id: int
    period: str            # ISO 'YYYY-MM'
    amount_cents: int      # signed cents
    rollover: bool


@dataclass
class BudgetActualRow:
    """One category's planned-vs-spent comparison for a single period.

    ``actual_cents`` is a positive magnitude (money out), matching
    :mod:`mammon.reports.spending`. ``carried_in_cents`` is the net remainder
    rolled over from prior periods for a ``rollover`` line (positive = an unspent
    surplus that raises this period's available amount, negative = a prior
    overspend that lowers it); it is 0 for a line that does not roll over and for
    an unbudgeted category. ``remaining_cents`` is
    ``budgeted_cents + carried_in_cents - actual_cents`` -- the envelope's
    available minus what was spent -- so a negative value still means overspent.
    ``budgeted_cents`` is 0 for a category with spending but no budget line.

    ``committed_cents`` is money this period is already promised to but has not
    paid yet: the scheduled occurrences landing in the period minus the part of
    them a real row already represents (SRD 5.12c). It is a positive magnitude
    like ``actual_cents``, and the two never overlap -- the actuals query always
    runs with scheduled placeholder rows EXCLUDED and the commitment is computed
    separately, so entering a scheduled payment moves cents from
    ``committed_cents`` into ``actual_cents`` and leaves their sum alone. That
    invariant is what keeps a reminder from being counted twice.

    ``remaining_cents`` deliberately does NOT learn about commitments -- it stays
    "available minus spent" for every caller that already reads it.
    ``uncommitted_cents`` is ``remaining_cents - committed_cents``: what is
    genuinely free to spend, negative once the commitments outrun the envelope.
    ``bucket``, ``rollover_mode`` and ``group_id`` carry the category's intent
    (:class:`BudgetCategorySettings`) so a table can render a row without a
    second lookup, and ``carry_overridden`` is True when ``carried_in_cents``
    came from a hand-set override rather than the recursion.

    A GROUP row (``is_group``) is a named pot: ``category_id`` is
    :func:`group_key` of the group (negative), ``category_name`` the group's
    name, ``group_id`` its id, and its money is the sum over its members. A
    MEMBER row (``group_id`` set, ``is_group`` False) is one of those members,
    returned only when the caller asks for members; its figures are already
    inside its group's row, so a reader summing rows must skip it -- the
    default row set has none, which is what makes the default safe to add up.

    Every field after ``carried_in_cents`` is defaulted: callers and tests that
    construct a row positionally from the original five keep working.
    """
    category_id: int
    category_name: str
    budgeted_cents: int
    actual_cents: int
    remaining_cents: int
    carried_in_cents: int = 0
    committed_cents: int = 0
    uncommitted_cents: int = 0
    bucket: str = "flex"
    rollover_mode: str = "none"
    group_id: Optional[int] = None
    carry_overridden: bool = False
    is_group: bool = False

    @property
    def is_member(self) -> bool:
        """A category reported under a group (its money is in the group row)."""
        return self.group_id is not None and not self.is_group

    @property
    def is_income(self) -> bool:
        """An income line: ``actual_cents`` is take-home RECEIVED, and
        ``remaining_cents`` is what the plan still expects to arrive."""
        return self.bucket == "income"

    @property
    def discretionary(self) -> bool:
        """Whether Track grades this row (see :func:`is_discretionary`)."""
        return is_discretionary(self.bucket)


@dataclass
class BudgetCategorySettings:
    """One category's *intent* within one budget.

    ``bucket`` picks how the line is meant to be read and edited; ``rollover_mode``
    says whether its remainder moves forward; ``annual_cents`` is meaningful only
    for the ``nonmonthly`` bucket, where it is the yearly total the twelve monthly
    lines are spread from. ``group_id`` names the :class:`BudgetGroup` of this
    budget the category is a member of, or ``None``; a member's money is reported
    inside its group's row (see :func:`set_member_group`).
    """
    budget_id: int
    category_id: int
    bucket: str = "flex"
    rollover_mode: str = "none"
    annual_cents: int = 0
    group_id: Optional[int] = None

    @property
    def rollover(self) -> bool:
        """The legacy two-valued flag ``budget_lines.rollover`` still stores."""
        return self.rollover_mode != "none"


@dataclass
class BudgetGroup:
    """A named pot inside one budget (schema v111): "Food" over Groceries and
    Dining. It has its own monthly lines (``budget_group_lines``), a bucket and a
    rollover mode like a category, and a member set held on the members' own
    settings rows (``budget_category_settings.group_id``). It exists only in its
    budget: a scenario copies it, and it never appears in a category picker.
    """
    id: int
    budget_id: int
    name: str
    bucket: str = "flex"
    rollover_mode: str = "none"
    annual_cents: int = 0
    #: A PAYEE line (schema v113): the text a payee must contain, case
    #: insensitively. The line's actual is then every matching payment out of a
    #: spending account counted WHOLE -- a mortgage payment with its principal,
    #: interest and escrow legs as one figure -- and those payments' category
    #: legs are taken out of the category actuals so nothing is counted twice
    #: (:func:`payee_payments`). ``None`` for an ordinary group of categories.
    payee_match: Optional[str] = None

    @property
    def key(self) -> int:
        """The ``category_id`` this group's row travels under (:func:`group_key`)."""
        return group_key(self.id)

    @property
    def by_payee(self) -> bool:
        return bool(self.payee_match)


@dataclass(frozen=True)
class GroupLine:
    """One target amount for a (group, period) within a budget."""
    budget_id: int
    group_id: int
    period: str            # ISO 'YYYY-MM'
    amount_cents: int


@dataclass
class CarryOverride:
    """A hand-set carry-in that terminates the rollover recursion at ``period``.

    The carry is otherwise recomputed from the lines and the ledger every time it
    is asked for, so this row is the *only* way to say "this envelope starts again
    from here". It keeps ``note`` and ``set_at`` because a number that silently
    overrides a computation has to be able to explain itself later.
    """
    budget_id: int
    category_id: int
    period: str            # ISO 'YYYY-MM'
    amount_cents: int      # the carry-in, as set by hand
    note: Optional[str]
    set_at: str            # ISO date


def _split_period(period: str) -> tuple[int, int]:
    """Parse an ISO ``'YYYY-MM'`` month into ``(year, month)`` or raise."""
    p = (period or "").strip()
    try:
        year_s, month_s = p.split("-")
        year, month = int(year_s), int(month_s)
    except (ValueError, AttributeError):
        raise ValueError(f"period must be 'YYYY-MM', got {period!r}")
    if len(year_s) != 4 or not 1 <= month <= 12:
        raise ValueError(f"period must be 'YYYY-MM', got {period!r}")
    return year, month


def shift_period(period: str, months: int) -> str:
    """Return the ISO ``'YYYY-MM'`` month ``months`` away from ``period``.

    Done in absolute month-index arithmetic (``year * 12 + month - 1``) rather
    than by adding to a date, so a shift out of a 31-day month cannot land on a
    day that does not exist, and a negative shift crosses January backwards
    without a special case.
    """
    year, month = _split_period(period)
    idx = year * 12 + (month - 1) + int(months)
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def period_sequence(start_period: str, n_months: int) -> list[str]:
    """Return ``n_months`` consecutive ISO months beginning at ``start_period``."""
    if int(n_months) <= 0:
        return []
    return [shift_period(start_period, i) for i in range(int(n_months))]


def month_bounds(year: int, month: int) -> tuple[str, str]:
    """Return the inclusive ISO ``(first, last)`` dates of one calendar month."""
    last = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"


def period_bounds(period: str) -> tuple[str, str]:
    """Return the inclusive ISO ``(first, last)`` dates of a ``'YYYY-MM'`` month."""
    return month_bounds(*_split_period(period))


def trailing_months(today: _dt.date, count: int) -> list[tuple[str, str, str]]:
    """The ``count`` COMPLETE months before ``today``, oldest first.

    Each entry is ``(period, first_date, last_date)``. The month containing
    ``today`` is deliberately excluded: a partial month drags every average that
    includes it, and a quietly wrong average is worse than a missing one.
    """
    if int(count) <= 0:
        return []
    end = f"{today.year:04d}-{today.month:02d}"
    months = []
    for back in range(int(count), 0, -1):
        period = shift_period(end, -back)
        first, last = period_bounds(period)
        months.append((period, first, last))
    return months


def mean_cents(total_cents: int, months: int) -> int:
    """``total_cents / months`` as integer cents, ROUND_HALF_UP, never a float.

    One division at the cents boundary, matching the app-wide money rule; ties go
    away from zero, so -15/2 is -8 and not -7. A zero month count has no mean and
    yields 0 rather than raising, because an empty window is a legitimate state of
    a young ledger and not a programming error.
    """
    if int(months) <= 0:
        return 0
    return int((Decimal(int(total_cents)) / Decimal(int(months))).quantize(
        Decimal(1), rounding=ROUND_HALF_UP))


def spending_account_ids(conn: sqlite3.Connection) -> list[int]:
    """Ids of the accounts whose activity counts as household spending.

    Closed accounts are INCLUDED: money spent last year out of an account closed
    since was still spending, and dropping it would silently lower every average
    that reaches back past the closure.
    """
    from mammon import ledger  # lazy: ledger imports nothing from here

    return [a["id"] for a in ledger.list_accounts(conn, include_closed=True)
            if (a["type"] or "") in SPENDING_ACCOUNT_TYPES]


# ---- budgets (CRUD) ---------------------------------------------------------
#: Spelled out once: the columns of a Budget, in the dataclass's field order.
_BUDGET_COLUMNS = ("SELECT id, name, active, start_period, end_period, note "
                   "FROM budgets")


def _budget_row(r) -> Budget:
    return Budget(id=r["id"], name=r["name"], active=bool(r["active"]),
                  start_period=r["start_period"], end_period=r["end_period"],
                  note=r["note"])


def create_budget(conn: sqlite3.Connection, name: str, active: bool = True) -> int:
    """Create a budget and return its new id."""
    cur = conn.execute(
        "INSERT INTO budgets (name, active) VALUES (?, ?)",
        (name, 1 if active else 0),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_budgets(conn: sqlite3.Connection, *,
                 include_inactive: bool = True) -> list[Budget]:
    """Return every budget (or only the active ones), ordered by name then id."""
    sql = _BUDGET_COLUMNS
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY name COLLATE NOCASE, id"
    return [_budget_row(r) for r in conn.execute(sql).fetchall()]


def get_budget(conn: sqlite3.Connection, budget_id: int) -> Optional[Budget]:
    """Return one budget by id, or ``None`` if it does not exist."""
    r = conn.execute(_BUDGET_COLUMNS + " WHERE id = ?", (budget_id,)).fetchone()
    if r is None:
        return None
    return _budget_row(r)


def set_budget_period(conn: sqlite3.Connection, budget_id: int,
                      start_period: Optional[str],
                      end_period: object = _UNSET) -> None:
    """Set a budget's first (and optionally last) month.

    ``start_period`` is what stops the rollover recursion walking backwards
    forever, so it is worth setting even on an open-ended plan. Both are validated
    as ISO months here rather than at read time: a malformed period stored once
    would break every later carry computation with no clue where it came from.
    ``end_period`` left unpassed keeps whatever is there; passing ``None``
    explicitly clears it (open-ended).
    """
    if start_period is not None:
        _split_period(start_period)
    if end_period is not _UNSET:
        if end_period is not None:
            _split_period(str(end_period))
            if start_period is not None and str(end_period) < start_period:
                raise ValueError(
                    f"end_period {end_period!r} precedes start_period {start_period!r}")
        conn.execute("UPDATE budgets SET start_period = ?, end_period = ? WHERE id = ?",
                     (start_period, end_period, budget_id))
    else:
        conn.execute("UPDATE budgets SET start_period = ? WHERE id = ?",
                     (start_period, budget_id))
    conn.commit()


def set_budget_note(conn: sqlite3.Connection, budget_id: int,
                    note: Optional[str]) -> None:
    """Set the free-text note that says what a scenario is FOR."""
    conn.execute("UPDATE budgets SET note = ? WHERE id = ?", (note, budget_id))
    conn.commit()


def rename_budget(conn: sqlite3.Connection, budget_id: int, name: str) -> None:
    """Rename a budget."""
    conn.execute("UPDATE budgets SET name = ? WHERE id = ?", (name, budget_id))
    conn.commit()


def set_active(conn: sqlite3.Connection, budget_id: int, active: bool) -> None:
    """Toggle a budget's ``active`` flag without deleting it."""
    conn.execute(
        "UPDATE budgets SET active = ? WHERE id = ?",
        (1 if active else 0, budget_id),
    )
    conn.commit()


def set_only_active(conn: sqlite3.Connection, budget_id: int) -> None:
    """Make ``budget_id`` the one active budget, deactivating every other.

    Scenarios are ordinary budgets distinguished only by ``active``, so exactly
    one may be active at a time -- two active envelope sets would leave "what is
    my budget for March" with two answers. Done in one UPDATE rather than a
    deactivate-then-activate pair so no moment exists with nothing active.
    """
    conn.execute("UPDATE budgets SET active = CASE WHEN id = ? THEN 1 ELSE 0 END",
                 (budget_id,))
    conn.commit()


def delete_budget(conn: sqlite3.Connection, budget_id: int) -> None:
    """Delete a budget, its lines, settings, overrides and saving targets.

    Everything is removed explicitly first so the delete is correct even on a
    connection whose ``foreign_keys`` pragma is off; the schema's
    ``ON DELETE CASCADE`` is a backstop, not the sole guarantee.
    """
    conn.execute("DELETE FROM budget_lines WHERE budget_id = ?", (budget_id,))
    conn.execute("DELETE FROM budget_category_settings WHERE budget_id = ?",
                 (budget_id,))
    conn.execute("DELETE FROM budget_carry_overrides WHERE budget_id = ?",
                 (budget_id,))
    conn.execute("DELETE FROM budget_saving_lines WHERE budget_id = ?",
                 (budget_id,))
    conn.execute("DELETE FROM budget_group_lines WHERE budget_id = ?",
                 (budget_id,))
    conn.execute("DELETE FROM budget_groups WHERE budget_id = ?", (budget_id,))
    conn.execute("DELETE FROM budget_line_order WHERE budget_id = ?", (budget_id,))
    conn.execute("DELETE FROM budget_other_lines WHERE budget_id = ?", (budget_id,))
    conn.execute("DELETE FROM budget_accounts WHERE budget_id = ?", (budget_id,))
    conn.execute("DELETE FROM budget_excluded_categories WHERE budget_id = ?",
                 (budget_id,))
    conn.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))
    conn.commit()


# ---- budget lines -----------------------------------------------------------
def _stored_rollover(conn: sqlite3.Connection, budget_id: int,
                     category_id: int) -> bool:
    """The legacy boolean equivalent of a category's stored ``rollover_mode``."""
    r = conn.execute(
        "SELECT rollover_mode FROM budget_category_settings "
        "WHERE budget_id = ? AND category_id = ?",
        (budget_id, category_id),
    ).fetchone()
    return bool(r is not None and r["rollover_mode"] != "none")


def set_line(conn: sqlite3.Connection, budget_id: int, category_id: int,
             period: str, amount_cents: int,
             rollover: Optional[bool] = None) -> int:
    """Upsert the target for one ``(budget, category, period)`` and return its id.

    Re-setting an existing line overwrites its ``amount_cents`` and ``rollover``
    rather than inserting a duplicate -- the ``UNIQUE(budget_id, category_id,
    period)`` constraint is the conflict target.

    ``rollover`` is the LEGACY two-valued flag, superseded by the three-valued
    ``budget_category_settings.rollover_mode`` but kept in sync for one release so
    the older readers (:func:`_carry_in`, :func:`budget_vs_actual`, the reports
    layer) keep working unchanged. Passing it explicitly still forces the flag;
    leaving it ``None`` -- the normal case now -- derives it from the category's
    stored mode, which is ``False`` when no settings row exists. That way a caller
    who never heard of buckets writes the same row it always did.
    """
    _split_period(period)  # validate format early
    if rollover is None:
        rollover = _stored_rollover(conn, budget_id, category_id)
    conn.execute(
        """
        INSERT INTO budget_lines (budget_id, category_id, period, amount_cents, rollover)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(budget_id, category_id, period)
        DO UPDATE SET amount_cents = excluded.amount_cents,
                      rollover     = excluded.rollover
        """,
        (budget_id, category_id, period, int(amount_cents), 1 if rollover else 0),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM budget_lines "
        "WHERE budget_id = ? AND category_id = ? AND period = ?",
        (budget_id, category_id, period),
    ).fetchone()
    return int(row["id"])


def get_lines(conn: sqlite3.Connection, budget_id: int, *,
              period: Optional[str] = None) -> list[BudgetLine]:
    """Return a budget's lines, optionally filtered to one ``'YYYY-MM'`` period.

    Ordered by period then category id for stable output.
    """
    sql = "SELECT id, budget_id, category_id, period, amount_cents, rollover " \
          "FROM budget_lines WHERE budget_id = ?"
    params: list = [budget_id]
    if period is not None:
        sql += " AND period = ?"
        params.append(period)
    sql += " ORDER BY period, category_id"
    return [
        BudgetLine(
            id=r["id"], budget_id=r["budget_id"], category_id=r["category_id"],
            period=r["period"], amount_cents=r["amount_cents"],
            rollover=bool(r["rollover"]),
        )
        for r in conn.execute(sql, params).fetchall()
    ]


def delete_line(conn: sqlite3.Connection, line_id: int) -> None:
    """Delete a single budget line by id."""
    conn.execute("DELETE FROM budget_lines WHERE id = ?", (line_id,))
    conn.commit()


# ---- budgeted vs actual -----------------------------------------------------
def _month_actuals(conn: sqlite3.Connection, period: str, *,
                   account_ids: Optional[Iterable[int]] = None,
                   include_scheduled: bool = False,
                   take_home: bool = True) -> dict[int, int]:
    """``category_id -> own (directly-booked) spending`` for one ``'YYYY-MM'``.

    ``take_home`` (the default here, unlike the report it reads) leaves out the
    deduction legs of a paycheck: the budget is planned from take-home pay, so
    withholding and premiums decided at open enrollment are not spending it
    grades, and never appear in "Everything else".

    A positive magnitude in cents, derived read-only from the ledger via
    :func:`mammon.reports.spending.spending_by_category`, so transfers are
    excluded and splits honored exactly as in every other spending view. Using
    ``own_cents`` (not a subtree roll-up) keeps money from being counted twice
    when both a parent and its child carry a budget line. The ``spending``
    import is deferred to call time to break the package import cycle.

    ``account_ids`` defaults to ``None``, meaning every account -- what
    :func:`budget_vs_actual` has always compared against. The seed passes the
    spending accounts only, so a brokerage fee never becomes a household target.

    ``include_scheduled`` is the same optional dimension every report module
    carries (``reports/_lines.py``): False, the default, counts only rows the
    user actually has, leaving pending pre-entries out. Budget *actuals* always
    ask for the default -- a reminder is not spending, and folding it in here is
    exactly the double count :func:`month_committed` exists to avoid. The
    committed figure is derived from the DIFFERENCE between the two calls, which
    is why the keyword lives here rather than in a second query.
    """
    from mammon.reports.spending import period_range, spending_by_category

    year, month = _split_period(period)
    start, end = period_range("month", year, month=month)
    report = spending_by_category(conn, start, end, account_ids=account_ids,
                                  include_scheduled=include_scheduled,
                                  take_home=take_home)
    return {
        row.category_id: row.own_cents
        for row in report.flat()
        if row.category_id is not None
    }


def month_income(conn: sqlite3.Connection, period: str, *,
                 account_ids: Optional[Iterable[int]] = None) -> dict[int, int]:
    """``income category id -> take-home received`` for one ``'YYYY-MM'``.

    Take-home is the NET amount of every positive, non-transfer, posted
    transaction in the spending accounts: a paycheck entered as one deposit
    counts as it stands, and a paycheck split into gross, withholding and a
    retirement leg counts as its net, which is what reached the household. The
    deposit is attributed to its income leg's category -- the transaction's own
    category, or for a split the category of its largest positive leg -- so a
    budget's "Net pay" line can cover Salary and find every paycheck. Positive
    magnitudes, integer cents.
    """
    start, end = period_bounds(period)
    ids = (spending_account_ids(conn) if account_ids is None
           else [int(a) for a in account_ids])
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    txns = conn.execute(
        f"SELECT id, category_id, amount FROM transactions "          # noqa: S608
        f"WHERE date >= ? AND date <= ? AND transfer_account_id IS NULL "
        f"AND scheduled = 0 AND amount > 0 AND account_id IN ({marks})",
        [start, end, *ids]).fetchall()
    if not txns:
        return {}
    by_txn: dict[int, list] = {}
    all_ids = [int(t["id"]) for t in txns]
    for i in range(0, len(all_ids), 400):
        chunk = all_ids[i:i + 400]
        for sp in conn.execute(
                "SELECT transaction_id, category_id, amount FROM splits "
                "WHERE transaction_id IN (%s)" % ",".join("?" for _ in chunk),
                chunk).fetchall():
            by_txn.setdefault(int(sp["transaction_id"]), []).append(sp)
    out: dict[int, int] = {}
    for t in txns:
        legs = by_txn.get(int(t["id"]))
        cid = t["category_id"]
        if legs:
            best = max((sp for sp in legs if sp["category_id"] is not None
                        and int(sp["amount"]) > 0),
                       key=lambda sp: int(sp["amount"]), default=None)
            if best is not None:
                cid = best["category_id"]
        if cid is None:
            continue
        out[int(cid)] = out.get(int(cid), 0) + int(t["amount"])
    return out


def payroll_deductions(conn: sqlite3.Connection, start: str, end: str, *,
                       account_ids: Optional[Iterable[int]] = None
                       ) -> dict[int, int]:
    """``category id -> cents withheld`` between ``start`` and ``end``: the
    negative category legs of positive-net transactions in the spending
    accounts -- exactly the money :func:`_month_actuals` leaves out at
    take-home. The retirement basis adds these back from the ledger, because a
    premium is part of what the household spends even though the budget never
    planned it (SRD 5.12i). Positive magnitudes."""
    ids = (spending_account_ids(conn) if account_ids is None
           else [int(a) for a in account_ids])
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT s.category_id AS category_id, s.amount AS amount "   # noqa: S608
        f"FROM splits s JOIN transactions t ON t.id = s.transaction_id "
        f"WHERE t.date >= ? AND t.date <= ? AND t.transfer_account_id IS NULL "
        f"AND t.scheduled = 0 AND t.amount > 0 AND t.account_id IN ({marks}) "
        f"AND s.transfer_account_id IS NULL AND s.category_id IS NOT NULL "
        f"AND s.amount < 0", [start, end, *ids]).fetchall()
    out: dict[int, int] = {}
    for r in rows:
        out[int(r["category_id"])] = out.get(int(r["category_id"]), 0) - int(r["amount"])
    return out


# ---- committed spend: scheduled, not yet paid (SRD 5.12c) -------------------
def _occurrence_categories(conn: sqlite3.Connection, defn: dict) -> dict[int, int]:
    """``category_id -> positive magnitude`` for ONE occurrence of ``defn``.

    The definition's split template (:func:`mammon.scheduled.get_scheduled_splits`)
    when it has one, else its own amount against its own category. Money IN and
    transfer legs contribute nothing: a commitment is spending you have promised,
    and moving your own money between accounts is not spending -- the same
    exclusion the actuals side makes.
    """
    from mammon import scheduled as _scheduled

    out: dict[int, int] = {}
    if defn.get("transfer_account_id") is not None:
        return out
    lines = _scheduled.get_scheduled_splits(conn, int(defn["id"]))
    if lines:
        for s in lines:
            if s["transfer_account_id"] is not None or s["category_id"] is None:
                continue
            if s["amount"] < 0:
                out[int(s["category_id"])] = (out.get(int(s["category_id"]), 0)
                                              - int(s["amount"]))
        return out
    cid, amount = defn.get("category_id"), int(defn["amount"])
    if cid is not None and amount < 0:
        out[int(cid)] = -amount
    return out


def month_committed(conn: sqlite3.Connection, period: str, *,
                    account_ids: Optional[Iterable[int]] = None,
                    entered: Optional[dict[int, int]] = None) -> dict[int, int]:
    """``category_id -> committed (scheduled but unpaid) spending`` for a month.

    Committed spend is the third state the Track tab shows beside actual and
    free (SRD 5.12c): money this month is already promised to. It is

    1. every pending pre-entry already sitting in the register for the month --
       the difference between the actuals with and without scheduled rows, so it
       reuses ONE definition of spending rather than inventing a second; plus
    2. every scheduled occurrence landing in the month that no row represents
       yet, taken from :func:`mammon.scheduled.occurrences` over the active
       manual definitions.

    An occurrence counts as already represented when a transaction on the same
    account carries exactly its signed amount within
    :data:`mammon.scheduled.PLACEHOLDER_MATCH_WINDOW_DAYS` days of it -- pending
    or entered, since a pending one was already counted in (1) and an entered one
    is in ``actual_cents``. Each candidate row is consumed once, so two identical
    occurrences need two rows to both fall out of the commitment.

    That is the whole invariant: paying a scheduled bill through
    :mod:`mammon.ledger` moves cents from committed into actual and leaves their
    SUM unchanged. Quicken double counts a reminder here; this does not.

    ``account_ids`` restricts both halves, matching :func:`_month_actuals`; a
    definition on an account outside the set is ignored, as its spending would
    be too. ``entered`` lets a caller that has already computed the month's
    actuals for the same accounts hand them in rather than paying for the query
    twice. All integer cents, positive magnitudes.
    """
    from mammon import scheduled as _scheduled

    year, month = _split_period(period)
    start, end = month_bounds(year, month)
    acct_set = None if account_ids is None else {int(a) for a in account_ids}
    if acct_set is not None and not acct_set:
        return {}

    # (1) pre-entries already in the register: actuals WITH scheduled minus WITHOUT.
    if entered is None:
        entered = _month_actuals(conn, period, account_ids=account_ids)
    with_pending = _month_actuals(conn, period, account_ids=account_ids,
                                  include_scheduled=True)
    committed: dict[int, int] = {}
    for cid, cents in with_pending.items():
        delta = cents - entered.get(cid, 0)
        if delta > 0:
            committed[cid] = delta

    # (2) occurrences with nothing behind them yet. A definition counts when it
    # has an EXPENSE LEG, whatever its own sign: a scheduled paycheck's
    # withholding is money this month is promised to (SRD 5.12, 2026-09).
    window = _scheduled.PLACEHOLDER_MATCH_WINDOW_DAYS
    defs = _expense_definitions(conn, account_ids=acct_set)
    if not defs:
        return committed

    # Candidate rows once, for the month plus the match window on either side.
    rows = conn.execute(
        "SELECT id, account_id, date, amount FROM transactions "
        "WHERE date >= ? AND date <= ? AND transfer_account_id IS NULL "
        "ORDER BY date, id",
        (_shift_days(start, -window), _shift_days(end, window))).fetchall()
    by_key: dict[tuple[int, int], list[str]] = {}
    for r in rows:
        by_key.setdefault((int(r["account_id"]), int(r["amount"])), []).append(r["date"])

    for defn, per_occurrence in defs:
        dates = _scheduled.occurrences(defn["next_date"], defn["frequency"], start, end)
        if not dates:
            continue
        key = (int(defn["account_id"]), int(defn["amount"]))
        for when in dates:
            if _claim_row(by_key.get(key), when, window):
                continue                    # a real row already stands for it
            for cid, cents in per_occurrence.items():
                committed[cid] = committed.get(cid, 0) + cents
    return committed


def _shift_days(iso: str, days: int) -> str:
    return (_dt.date.fromisoformat(iso) + _dt.timedelta(days=days)).isoformat()


def _claim_row(dates: Optional[list[str]], when: str, window: int) -> bool:
    """Consume the candidate row closest to ``when`` within ``window`` days.

    Consuming rather than merely matching is what stops one payment from
    cancelling two occurrences of the same bill.
    """
    if not dates:
        return False
    target = _dt.date.fromisoformat(when)
    best, best_diff = None, None
    for i, d in enumerate(dates):
        diff = abs((_dt.date.fromisoformat(d) - target).days)
        if diff <= window and (best_diff is None or diff < best_diff):
            best, best_diff = i, diff
    if best is None:
        return False
    dates.pop(best)
    return True


def _carry_in(conn: sqlite3.Connection, budget_id: int, period: str,
              rollover_cats: set[int], *,
              account_ids: Optional[Iterable[int]] = None) -> dict[int, int]:
    """Net remainder carried into ``period`` for each category in ``rollover_cats``.

    The recursion (SRD 5.12b) walks month by month rather than summing prior
    months independently, because each step needs the balance the previous step
    produced::

        carry_in(P) = override(P)                       if one is set
                    = 0                                 if P <= the budget floor
                    = 0                                 if the prior month's line
                                                        does not roll over
                    = carry_in(P-1)                      if the prior month has no
                                                        line for the category
                    = carry_in(P-1) + budgeted(P-1) - actual(P-1)   otherwise,
                                                        clamped at 0 in
                                                        ``positive`` mode

    Three shapes are load-bearing. A stored **override** terminates the walk --
    it is the only way to say "this envelope starts again here", which is why the
    carry itself is never stored. A **gap month** passes the balance through
    instead of resetting it: a month with no line is a month with no target, and
    counting its spending against zero would invent a deficit. A **non-rollover
    prior line** stops the carry, per category per month, so unchecking rollover
    for one month has the effect the user expects. The mode comes from
    ``budget_category_settings.rollover_mode`` when a row exists and from the
    legacy ``budget_lines.rollover`` flag when it does not; the two are kept in
    sync by :func:`set_line`, :func:`set_settings` and :func:`clear_settings`, so
    they cannot disagree.

    The floor is the budget's ``start_period`` when it has one, else the earliest
    month the budget mentions at all -- without it the walk would recurse back
    through empty months forever.

    ISO ``'YYYY-MM'`` strings sort as text in calendar order, so stepping back a
    month crosses the December -> January boundary with no reset. All integer
    cents; the carry is pure addition, so no rounding is possible. Each month's
    actuals are fetched once and memoized for the whole walk.

    A GROUP walks the same recursion under its :func:`group_key`: its line for a
    month is the group's own line plus any line a member still carries, its
    actual is the sum of its members' own actuals, and its mode is the group's.
    A group has no override (the override table is keyed by category), so a pot
    is reset by editing its lines.
    """
    if not rollover_cats:
        return {}

    groups = {g.key: g for g in list_groups(conn, budget_id) if g.key in rollover_cats}
    members = group_members(conn, budget_id) if groups else {}
    member_key = {cid: group_key(gid) for gid, cids in members.items()
                  for cid in cids if group_key(gid) in groups}

    # Every line the budget holds, by period -> key -> (amount, rollover). A
    # member's line lands on its GROUP's key, added to whatever the group holds.
    lines: dict[str, dict[int, tuple[int, bool]]] = {}

    def add_line(p: str, key: int, cents: int, rollover: bool) -> None:
        have = lines.setdefault(p, {}).get(key)
        if have is None:
            lines[p][key] = (cents, rollover)
        else:
            lines[p][key] = (have[0] + cents, have[1] or rollover)

    for r in conn.execute(
        "SELECT category_id, period, amount_cents, rollover FROM budget_lines "
        "WHERE budget_id = ? AND period < ? ORDER BY period", (budget_id, period),
    ).fetchall():
        cid = int(r["category_id"])
        if cid in member_key:
            gkey = member_key[cid]
            add_line(r["period"], gkey, int(r["amount_cents"]),
                     groups[gkey].rollover_mode != "none")
        elif cid in rollover_cats:
            add_line(r["period"], cid, int(r["amount_cents"]), bool(r["rollover"]))
    if groups:
        for r in conn.execute(
            "SELECT group_id, period, amount_cents FROM budget_group_lines "
            "WHERE budget_id = ? AND period < ? ORDER BY period",
            (budget_id, period),
        ).fetchall():
            gkey = group_key(int(r["group_id"]))
            if gkey in groups:
                add_line(r["period"], gkey, int(r["amount_cents"]),
                         groups[gkey].rollover_mode != "none")

    overrides: dict[tuple[int, str], int] = {
        (o.category_id, o.period): o.amount_cents
        for o in get_carry_overrides(conn, budget_id)
        if o.category_id in rollover_cats
    }

    settings = dict(get_settings(conn, budget_id))
    for gkey, g in groups.items():
        settings[gkey] = BudgetCategorySettings(
            budget_id=budget_id, category_id=gkey, bucket=g.bucket,
            rollover_mode=g.rollover_mode, annual_cents=g.annual_cents)
    floor = _carry_floor(conn, budget_id, lines, overrides)
    actuals_cache: dict[str, dict[int, int]] = {}

    payee_cache: dict[tuple[str, int], int] = {}

    def actual(p: str, key: int) -> int:
        if p not in actuals_cache:
            actuals_cache[p] = _month_actuals(conn, p, account_ids=account_ids)
        if key in groups and groups[key].by_payee:
            if (p, key) not in payee_cache:
                start, end = period_bounds(p)
                payee_cache[(p, key)] = payee_payments(
                    conn, start, end, groups[key].payee_match,
                    account_ids=account_ids).whole_cents
            return payee_cache[(p, key)]
        if key in groups:
            month = actuals_cache[p]
            return sum(month.get(cid, 0) for cid in members.get(-key, []))
        return actuals_cache[p].get(key, 0)

    memo: dict[tuple[int, str], int] = {}

    def carry_of(cid: int, p: str) -> int:
        key = (cid, p)
        if key in memo:
            return memo[key]
        if key in overrides:
            memo[key] = overrides[key]
            return memo[key]
        if floor is None or p <= floor:
            memo[key] = 0
            return 0
        prev = shift_period(p, -1)
        prev_line = lines.get(prev, {}).get(cid)
        if prev_line is None:                       # a gap month: pass through
            memo[key] = carry_of(cid, prev)
            return memo[key]
        budgeted, prev_rollover = prev_line
        mode = _resolved_rollover_mode(settings.get(cid), prev_rollover)
        if mode == "none":
            memo[key] = 0
            return 0
        balance = carry_of(cid, prev) + budgeted - actual(prev, cid)
        if mode == "positive" and balance < 0:
            balance = 0
        memo[key] = balance
        return balance

    return {cid: carry_of(cid, period) for cid in rollover_cats}


def _resolved_rollover_mode(setting: Optional[BudgetCategorySettings],
                            prev_rollover: bool) -> str:
    """The rollover mode one step of the recursion should use.

    Absent settings mean the legacy flag is all there is, and a flag that is set
    has always meant "carry the remainder either way" -- so it reads as ``both``.
    A prior line with the flag off stops the carry whatever the stored mode says,
    which is how a single month can be excluded without changing the category's
    intent.
    """
    if setting is None:
        return "both" if prev_rollover else "none"
    if setting.rollover_mode == "none" or not prev_rollover:
        return "none"
    return setting.rollover_mode


def _carry_floor(conn: sqlite3.Connection, budget_id: int,
                 lines: dict[str, dict[int, tuple[int, bool]]],
                 overrides: dict[tuple[int, str], int]) -> Optional[str]:
    """The first month the carry recursion may walk back to.

    The budget's declared ``start_period`` when it has one; otherwise the
    earliest month it mentions, which for a budget created before start periods
    existed is the same practical answer. ``None`` (nothing to walk) short-circuits
    the recursion to 0.
    """
    budget = get_budget(conn, budget_id)
    if budget is not None and budget.start_period:
        return budget.start_period
    known = list(lines) + [p for _cid, p in overrides]
    return min(known) if known else None


def budget_vs_actual(conn: sqlite3.Connection, budget_id: int, period: str, *,
                     include_unbudgeted: bool = True,
                     include_members: bool = False,
                     include_income: bool = False) -> list[BudgetActualRow]:
    """Compare each budgeted category's target against actual spending in ``period``.

    ``period`` is an ISO ``'YYYY-MM'`` month. Actuals are derived read-only from
    the ledger via :func:`mammon.reports.spending.spending_by_category` over that
    whole month, so transfers are excluded and splits are honored exactly as in
    every other spending view; a category's actual is the spending booked
    *directly* to it (``own_cents``), which keeps money from being counted twice
    when both a parent and its child carry a line.

    A line marked ``rollover`` carries the net remainder of its prior rollover
    periods into ``carried_in_cents`` (see :func:`_carry_in`), so
    ``remaining_cents = budgeted + carried_in - actual`` reflects the running
    envelope rather than the month in isolation. A non-rollover line carries 0.

    ``committed_cents`` is what the month is already promised to but has not paid
    (:func:`month_committed`), and ``uncommitted_cents`` is what is left after
    those promises. ``remaining_cents`` is deliberately untouched by them, so
    every existing caller keeps the number it has always read. The commitment is
    always computed rather than hidden behind a flag: a row reporting
    ``committed_cents == 0`` because the caller did not ask would be
    indistinguishable from a month with nothing scheduled.

    With ``include_unbudgeted`` (the default) a category that was spent but has
    no line for this period is still returned, with ``budgeted_cents == 0`` --
    surfacing spending that escaped the plan. Rows are ordered by category name.

    Every GROUP of the budget is returned as one row (``is_group``): its
    budgeted amount is its own line plus any line a member still carries, its
    actual and committed are the sums over its members, and its carry is the
    recursion run over those sums. The members themselves are LEFT OUT unless
    ``include_members`` is set -- then each follows with its own actual and
    committed, ``budgeted_cents`` its own line if it still has one, no carry, and
    ``group_id`` set. The default row set therefore partitions the month's money
    exactly, and a caller that adds up what it gets (the range report, the MCP
    surface, the outlook) needs no rule about members; the Budget page asks for
    them and knows not to sum them twice.

    INCOME lines (a settings row with bucket ``income``) are likewise left out
    unless ``include_income`` is set: they are not spending, and a reader that
    summed them into an expense total would be wrong. Asked for, each comes
    back with ``budgeted_cents`` the planned take-home, ``actual_cents`` the
    take-home received (:func:`month_income`) and ``remaining_cents`` what the
    plan still expects to arrive; ``is_income`` names them.
    """
    scope = budget_account_ids(conn, budget_id)
    excluded = excluded_categories(conn, budget_id)
    actual_by_cat = _month_actuals(conn, period, account_ids=scope)
    committed_by_cat = month_committed(conn, period, account_ids=scope,
                                       entered=actual_by_cat)
    _drop_excluded(actual_by_cat, excluded)
    _drop_excluded(committed_by_cat, excluded)
    settings = get_settings(conn, budget_id)
    income_cats = {cid for cid, st in settings.items() if st.bucket == "income"}

    line_objs = get_lines(conn, budget_id, period=period)
    lines = {ln.category_id: ln.amount_cents for ln in line_objs}
    groups = list_groups(conn, budget_id)
    # A payee line claims its payments whole; their category legs leave the
    # category actuals (and commitments) so the money is counted once.
    claims = payee_claims(conn, budget_id, period)
    _subtract_legs(actual_by_cat, claims.values())
    payee_promised: dict[int, int] = {}
    for g in groups:
        if g.by_payee:
            whole, legs = payee_committed(conn, period, g.payee_match,
                                          account_ids=scope)
            payee_promised[g.id] = whole
            _subtract_legs(committed_by_cat, [PayeePayments(legs=legs)])
    members = group_members(conn, budget_id)
    member_of = {cid: gid for gid, cids in members.items() for cid in cids}
    glines = {gl.group_id: gl.amount_cents
              for gl in get_group_lines(conn, budget_id, period=period)}

    rollover_cats = {ln.category_id for ln in line_objs
                     if ln.rollover and ln.category_id not in member_of
                     and ln.category_id not in income_cats}
    rollover_cats |= {g.key for g in groups if g.rollover_mode != "none"}
    carried = _carry_in(conn, budget_id, period, rollover_cats, account_ids=scope)
    overridden = {o.category_id for o in
                  get_carry_overrides(conn, budget_id, period=period)}

    if include_unbudgeted:
        cat_ids = set(lines) | {cid for cid, cents in actual_by_cat.items() if cents > 0}
    else:
        cat_ids = set(lines)
    cat_ids -= set(member_of)
    cat_ids -= income_cats
    cat_ids -= excluded

    names = {
        r["id"]: r["name"]
        for r in conn.execute("SELECT id, name FROM categories").fetchall()
    }

    def category_row(cid: int, *, carried_in: int, group_id: Optional[int]
                     ) -> BudgetActualRow:
        budgeted = lines.get(cid, 0)
        actual = actual_by_cat.get(cid, 0)
        committed = committed_by_cat.get(cid, 0)
        remaining = budgeted + carried_in - actual
        setting = settings.get(cid)
        return BudgetActualRow(
            category_id=cid,
            category_name=names.get(cid, ""),
            budgeted_cents=budgeted,
            actual_cents=actual,
            remaining_cents=remaining,
            carried_in_cents=carried_in,
            committed_cents=committed,
            uncommitted_cents=remaining - committed,
            bucket=setting.bucket if setting is not None else "flex",
            rollover_mode=(setting.rollover_mode if setting is not None
                           else ("both" if cid in rollover_cats else "none")),
            group_id=group_id,
            carry_overridden=cid in overridden,
        )

    rows: list[BudgetActualRow] = []
    for cid in cat_ids:
        rows.append(category_row(cid, carried_in=carried.get(cid, 0),
                                 group_id=None))
    for g in groups:
        cids = members.get(g.id, [])
        budgeted = glines.get(g.id, 0) + sum(lines.get(c, 0) for c in cids)
        if g.by_payee:
            actual = claims.get(g.id, PayeePayments()).whole_cents
            committed = payee_promised.get(g.id, 0)
        else:
            actual = sum(actual_by_cat.get(c, 0) for c in cids)
            committed = sum(committed_by_cat.get(c, 0) for c in cids)
        carried_in = carried.get(g.key, 0)
        remaining = budgeted + carried_in - actual
        rows.append(BudgetActualRow(
            category_id=g.key, category_name=g.name,
            budgeted_cents=budgeted, actual_cents=actual,
            remaining_cents=remaining, carried_in_cents=carried_in,
            committed_cents=committed, uncommitted_cents=remaining - committed,
            bucket=g.bucket, rollover_mode=g.rollover_mode, group_id=g.id,
            carry_overridden=False, is_group=True))
        if include_members:
            for cid in cids:
                rows.append(category_row(cid, carried_in=0, group_id=g.id))
    if include_income and income_cats:
        received = month_income(conn, period, account_ids=scope)
        for cid in sorted(income_cats):
            budgeted = lines.get(cid, 0)
            actual = received.get(cid, 0)
            rows.append(BudgetActualRow(
                category_id=cid, category_name=names.get(cid, ""),
                budgeted_cents=budgeted, actual_cents=actual,
                remaining_cents=budgeted - actual, carried_in_cents=0,
                committed_cents=0, uncommitted_cents=budgeted - actual,
                bucket="income", rollover_mode="none"))
    rows.sort(key=lambda r: (r.category_name.lower(), r.category_id))
    return rows


# ---- payee lines: a payment counted whole (SRD 5.12) -------------------------
@dataclass(frozen=True)
class PayeePayments:
    """The payments one payee line claims in a month: ``whole_cents`` is their
    total as the money left the account (principal, interest and escrow of a
    mortgage payment as one figure); ``legs`` is ``category id -> cents`` of the
    category legs those payments carry, which the category actuals give up so
    the money is counted once; ``payments`` is ``(date, payee, cents, txn_id,
    account_id)`` for a drill-down."""
    whole_cents: int = 0
    legs: dict = None  # type: ignore[assignment]
    payments: tuple = ()
    txn_ids: tuple = ()

    def __post_init__(self):
        if self.legs is None:
            object.__setattr__(self, "legs", {})


def payee_payments(conn: sqlite3.Connection, start: str, end: str, match: str, *,
                   account_ids: Optional[Iterable[int]] = None) -> PayeePayments:
    """Every payment out of a spending account between ``start`` and ``end``
    whose payee CONTAINS ``match`` (case-insensitively), counted whole.

    "Out of a spending account" means money leaving the household's cash: a
    categorized payment, a split payment whatever its legs, or a transfer whose
    other side is NOT a spending account (the principal of a mortgage paid as a
    plain transfer to the loan). A transfer between two spending accounts (a
    card payment from checking) is not a payment, because the card's purchases
    were the spending. Posted rows only; a user who wants the scheduled payment
    counted turns on "Count scheduled bills as spent".
    """
    text = (match or "").strip().lower()
    ids = (spending_account_ids(conn) if account_ids is None
           else [int(a) for a in account_ids])
    if not text or not ids:
        return PayeePayments()
    spending = set(ids)
    marks = ",".join("?" for _ in ids)
    txns = conn.execute(
        f"SELECT id, date, payee, amount, category_id, transfer_account_id, "  # noqa: S608
        f"account_id FROM transactions WHERE date >= ? AND date <= ? AND scheduled = 0 "
        f"AND amount < 0 AND account_id IN ({marks}) "
        f"AND lower(coalesce(payee, '')) LIKE ? ESCAPE '\\'",
        [start, end, *ids, "%" + _like_escape(text) + "%"]).fetchall()
    hits = [t for t in txns if t["transfer_account_id"] is None
            or int(t["transfer_account_id"]) not in spending]
    if not hits:
        return PayeePayments()
    legs: dict[int, int] = {}
    by_txn: dict[int, list] = {}
    all_ids = [int(t["id"]) for t in hits]
    for i in range(0, len(all_ids), 400):
        chunk = all_ids[i:i + 400]
        for sp in conn.execute(
                "SELECT transaction_id, category_id, amount, transfer_account_id "
                "FROM splits WHERE transaction_id IN (%s)" % ",".join("?" for _ in chunk),
                chunk).fetchall():
            by_txn.setdefault(int(sp["transaction_id"]), []).append(sp)
    payments = []
    for t in hits:
        lines = by_txn.get(int(t["id"]))
        if lines:
            for sp in lines:
                if sp["transfer_account_id"] is None and sp["category_id"] is not None \
                        and int(sp["amount"]) < 0:
                    cid = int(sp["category_id"])
                    legs[cid] = legs.get(cid, 0) - int(sp["amount"])
        elif t["category_id"] is not None and t["transfer_account_id"] is None:
            cid = int(t["category_id"])
            legs[cid] = legs.get(cid, 0) - int(t["amount"])
        payments.append((t["date"], t["payee"] or "", -int(t["amount"]), int(t["id"]),
                         int(t["account_id"])))
    payments.sort()
    return PayeePayments(whole_cents=sum(pm[2] for pm in payments), legs=legs,
                         payments=tuple(payments), txn_ids=tuple(all_ids))


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def payee_claims(conn: sqlite3.Connection, budget_id: int,
                 period: str) -> dict[int, PayeePayments]:
    """``group id -> PayeePayments`` for every payee line of the budget in the
    month. A payment matched by two payee lines is claimed by the first (by
    name), so the money is still counted once."""
    start, end = period_bounds(period)
    scope = budget_account_ids(conn, budget_id)
    out: dict[int, PayeePayments] = {}
    taken: set[int] = set()
    for g in list_groups(conn, budget_id):
        if not g.by_payee:
            continue
        found = payee_payments(conn, start, end, g.payee_match, account_ids=scope)
        if taken:
            keep = [pm for pm in found.payments if pm[3] not in taken]
            if len(keep) != len(found.payments):
                ids = {pm[3] for pm in keep}
                found = payee_payments_subset(conn, found, ids)
        taken.update(found.txn_ids)
        out[g.id] = found
    return out


def payee_payments_subset(conn: sqlite3.Connection, found: PayeePayments,
                          keep_ids: set[int]) -> PayeePayments:
    """``found`` narrowed to the transactions in ``keep_ids`` (recomputed, so
    the legs stay exact)."""
    if not keep_ids:
        return PayeePayments()
    payments = tuple(pm for pm in found.payments if pm[3] in keep_ids)
    legs: dict[int, int] = {}
    marks = ",".join("?" for _ in keep_ids)
    ids = sorted(keep_ids)
    for sp in conn.execute(
            f"SELECT transaction_id, category_id, amount, transfer_account_id "  # noqa: S608
            f"FROM splits WHERE transaction_id IN ({marks})", ids).fetchall():
        if sp["transfer_account_id"] is None and sp["category_id"] is not None \
                and int(sp["amount"]) < 0:
            legs[int(sp["category_id"])] = legs.get(int(sp["category_id"]), 0) \
                - int(sp["amount"])
    split_ids = {int(r[0]) for r in conn.execute(
        f"SELECT DISTINCT transaction_id FROM splits "  # noqa: S608
        f"WHERE transaction_id IN ({marks})", ids).fetchall()}
    for t in conn.execute(
            f"SELECT id, category_id, amount, transfer_account_id FROM transactions "  # noqa: S608
            f"WHERE id IN ({marks})", ids).fetchall():
        if int(t["id"]) not in split_ids and t["category_id"] is not None \
                and t["transfer_account_id"] is None:
            legs[int(t["category_id"])] = legs.get(int(t["category_id"]), 0) \
                - int(t["amount"])
    return PayeePayments(whole_cents=sum(pm[2] for pm in payments), legs=legs,
                         payments=payments, txn_ids=tuple(ids))


def _subtract_legs(actuals: dict[int, int], claims: Iterable[PayeePayments]) -> None:
    """Take the claimed payments' category legs out of ``actuals`` in place,
    never below zero."""
    for found in claims:
        for cid, cents in found.legs.items():
            if cid in actuals:
                actuals[cid] = max(0, actuals[cid] - cents)
                if actuals[cid] == 0:
                    del actuals[cid]


def payee_history(conn: sqlite3.Connection, match: str,
                  periods: Sequence[str]) -> list[int]:
    """The whole payments matching ``match`` per month of ``periods``, oldest
    first -- what the Add a line dialog proposes a payee line's amount from."""
    out = []
    for p in periods:
        start, end = period_bounds(p)
        out.append(payee_payments(conn, start, end, match).whole_cents)
    return out


def payee_committed(conn: sqlite3.Connection, period: str, match: str, *,
                    account_ids: Optional[Iterable[int]] = None
                    ) -> tuple[int, dict[int, int]]:
    """``(whole cents, category legs)`` the active schedules whose payee contains
    ``match`` have promised this month and no row stands for yet -- the payee
    line's share of :func:`month_committed`, with the legs the category
    commitments give up."""
    from mammon import scheduled as _scheduled

    text = (match or "").strip().lower()
    if not text:
        return 0, {}
    start, end = period_bounds(period)
    accounts = set(spending_account_ids(conn) if account_ids is None
                   else [int(a) for a in account_ids])
    window = _scheduled.PLACEHOLDER_MATCH_WINDOW_DAYS
    defs = [d for d in _scheduled.list_scheduled(conn, active_only=True)
            if int(d["amount"]) < 0 and int(d["account_id"]) in accounts
            and text in (d.get("payee") or "").lower()
            and (d.get("transfer_account_id") is None
                 or int(d["transfer_account_id"]) not in accounts)]
    if not defs:
        return 0, {}
    rows = conn.execute(
        "SELECT id, account_id, date, amount FROM transactions "
        "WHERE date >= ? AND date <= ? ORDER BY date, id",
        (_shift_days(start, -window), _shift_days(end, window))).fetchall()
    by_key: dict[tuple[int, int], list[str]] = {}
    for r in rows:
        by_key.setdefault((int(r["account_id"]), int(r["amount"])), []).append(r["date"])
    whole = 0
    legs: dict[int, int] = {}
    for d in defs:
        per = _occurrence_categories(conn, d)
        key = (int(d["account_id"]), int(d["amount"]))
        for when in _scheduled.occurrences(d["next_date"], d["frequency"], start, end):
            if _claim_row(by_key.get(key), when, window):
                continue
            whole += -int(d["amount"])
            for cid, cents in per.items():
                legs[cid] = legs.get(cid, 0) + cents
    return whole, legs


# ---- everything else: spending the plan has no line for ---------------------
@dataclass(frozen=True)
class OtherSpending:
    """The month's spending outside the plan: every category (and the
    uncategorized remainder) with spending but no line, no group and no income
    role. ``by_category`` is ``(category id or None, path, cents)``, largest
    first. ``planned_cents`` is the optional amount the user gave the
    "Everything else" line (``budget_other_lines``), 0 when none."""
    period: str
    actual_cents: int
    planned_cents: int = 0
    by_category: tuple = ()

    @property
    def category_ids(self) -> tuple:
        return tuple(cid for cid, _p, _c in self.by_category if cid is not None)


def other_spending(conn: sqlite3.Connection, budget_id: int,
                   period: str) -> OtherSpending:
    """What this month spent that the plan has no line for (SRD 5.12, the
    one-page budget's last line). At take-home, like every budget actual;
    uncategorized spending is included, because a beginner's ledger has plenty
    of it and leaving it out would make the plan look more complete than it
    is."""
    from mammon.reports.spending import spending_by_category

    start, end = period_bounds(period)
    report = spending_by_category(conn, start, end,
                                  account_ids=budget_account_ids(conn, budget_id),
                                  take_home=True)
    settings = get_settings(conn, budget_id)
    covered = {ln.category_id for ln in get_lines(conn, budget_id, period=period)}
    covered |= set(member_groups(conn, budget_id))
    covered |= {cid for cid, st in settings.items() if st.bucket == "income"}
    covered |= set(settings)                      # configured = on the plan
    covered |= excluded_categories(conn, budget_id)   # left out on purpose
    own = {row.category_id: int(row.own_cents) for row in report.flat()}
    claimed: dict = {}
    for found in payee_claims(conn, budget_id, period).values():
        for cid, cents in found.legs.items():
            claimed[cid] = claimed.get(cid, 0) + cents
    rows = []
    for row in report.flat():
        cents = own.get(row.category_id, 0) - claimed.get(row.category_id, 0)
        if cents <= 0 or row.category_id in covered:
            continue
        rows.append((row.category_id, row.path or row.name or "Uncategorized",
                     cents))
    rows.sort(key=lambda r: (-r[2], r[1].lower()))
    planned = conn.execute(
        "SELECT amount_cents FROM budget_other_lines WHERE budget_id = ? "
        "AND period = ?", (budget_id, period)).fetchone()
    return OtherSpending(period=period, actual_cents=sum(r[2] for r in rows),
                         planned_cents=int(planned["amount_cents"]) if planned else 0,
                         by_category=tuple(rows))


def set_other_line(conn: sqlite3.Connection, budget_id: int, period: str,
                   amount_cents: int) -> None:
    """The planned amount for "Everything else" in one month."""
    _split_period(period)
    conn.execute(
        "INSERT INTO budget_other_lines (budget_id, period, amount_cents) "
        "VALUES (?, ?, ?) ON CONFLICT(budget_id, period) "
        "DO UPDATE SET amount_cents = excluded.amount_cents",
        (budget_id, period, int(amount_cents)))
    conn.commit()


def get_other_lines(conn: sqlite3.Connection, budget_id: int) -> dict[str, int]:
    """``period -> planned cents`` for "Everything else" across the budget."""
    return {r["period"]: int(r["amount_cents"]) for r in conn.execute(
        "SELECT period, amount_cents FROM budget_other_lines WHERE budget_id = ? "
        "ORDER BY period", (budget_id,)).fetchall()}


def clear_other_line(conn: sqlite3.Connection, budget_id: int, period: str) -> None:
    conn.execute("DELETE FROM budget_other_lines WHERE budget_id = ? AND period = ?",
                 (budget_id, period))
    conn.commit()


# ---- line order: the user's priority list -----------------------------------
#: The kinds a line-order row may name.
LINE_KINDS = ("category", "group", "account")


def line_order(conn: sqlite3.Connection, budget_id: int) -> dict[tuple[str, int], int]:
    """``(kind, ident) -> position`` for every line the user has placed."""
    return {(r["kind"], int(r["ident"])): int(r["position"]) for r in conn.execute(
        "SELECT kind, ident, position FROM budget_line_order WHERE budget_id = ? "
        "ORDER BY position", (budget_id,)).fetchall()}


def set_line_order(conn: sqlite3.Connection, budget_id: int,
                   ordered: Sequence[tuple[str, int]]) -> None:
    """Record the user's order of lines, first to last. Replaces the whole
    list, so a line left out simply falls back to the default order."""
    for kind, _ident in ordered:
        if kind not in LINE_KINDS:
            raise ValueError(f"unknown line kind {kind!r}; expected one of {LINE_KINDS}")
    conn.execute("DELETE FROM budget_line_order WHERE budget_id = ?", (budget_id,))
    for position, (kind, ident) in enumerate(ordered):
        conn.execute(
            "INSERT INTO budget_line_order (budget_id, kind, ident, position) "
            "VALUES (?, ?, ?, ?)", (budget_id, kind, int(ident), position))
    conn.commit()


# ---- per-category settings (bucket, rollover mode, annual total) ------------
def get_settings(conn: sqlite3.Connection,
                 budget_id: int) -> dict[int, BudgetCategorySettings]:
    """Every stored per-category setting for a budget, keyed by category id.

    Categories with no row are simply absent: the default (flex, no rollover, no
    annual total) is a *shape*, not a row, so a fresh budget stores nothing until
    the user expresses an intent.
    """
    return {
        r["category_id"]: BudgetCategorySettings(
            budget_id=r["budget_id"], category_id=r["category_id"],
            bucket=r["bucket"], rollover_mode=r["rollover_mode"],
            annual_cents=r["annual_cents"], group_id=r["group_id"])
        for r in conn.execute(
            "SELECT budget_id, category_id, bucket, rollover_mode, annual_cents, "
            "group_id FROM budget_category_settings WHERE budget_id = ? "
            "ORDER BY category_id", (budget_id,)).fetchall()
    }


def get_setting(conn: sqlite3.Connection, budget_id: int,
                category_id: int) -> BudgetCategorySettings:
    """One category's settings, or the defaults if it has never been configured.

    Never returns ``None``: every category has a bucket as far as the Set tab is
    concerned, and an absent row means "flex, no rollover". Callers that need to
    know whether a row exists should look in :func:`get_settings`.
    """
    r = conn.execute(
        "SELECT budget_id, category_id, bucket, rollover_mode, annual_cents, "
        "group_id FROM budget_category_settings "
        "WHERE budget_id = ? AND category_id = ?",
        (budget_id, category_id)).fetchone()
    if r is None:
        return BudgetCategorySettings(budget_id=budget_id, category_id=category_id)
    return BudgetCategorySettings(
        budget_id=r["budget_id"], category_id=r["category_id"], bucket=r["bucket"],
        rollover_mode=r["rollover_mode"], annual_cents=r["annual_cents"],
        group_id=r["group_id"])


def set_settings(conn: sqlite3.Connection, budget_id: int, category_id: int, *,
                 bucket: object = _UNSET, rollover_mode: object = _UNSET,
                 annual_cents: object = _UNSET,
                 group_id: object = _UNSET) -> BudgetCategorySettings:
    """Upsert one category's bucket / rollover mode / annual total / group.

    Every argument is optional and an unpassed one keeps its stored value, so the
    Set tab's per-cell editors each write only what they changed instead of
    round-tripping a whole row (two editors on the same row would otherwise race,
    and the loser's edit would vanish).

    The legacy ``budget_lines.rollover`` flag is rewritten for this category's
    existing lines so the older readers stay correct: ``rollover_mode != 'none'``
    is the boolean. That duplication is deliberate and temporary -- one release,
    per the design -- and lives here rather than in the readers so there is
    exactly one place where the two representations can disagree.

    ``group_id`` must name a group OF THIS BUDGET (or ``None``). This is the raw
    write: it does not fold the category's lines into the group, which is what
    :func:`set_member_group` -- the path the Set tab takes -- is for.
    """
    current = get_setting(conn, budget_id, category_id)
    new_bucket = current.bucket if bucket is _UNSET else str(bucket)
    new_mode = (current.rollover_mode if rollover_mode is _UNSET
                else str(rollover_mode))
    new_annual = (current.annual_cents if annual_cents is _UNSET
                  else int(annual_cents))  # type: ignore[arg-type]
    new_group = current.group_id if group_id is _UNSET else group_id
    if new_bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {new_bucket!r}; expected one of {BUCKETS}")
    if new_mode not in ROLLOVER_MODES:
        raise ValueError(
            f"unknown rollover mode {new_mode!r}; expected one of {ROLLOVER_MODES}")
    if new_group is not None:
        g = get_group(conn, int(new_group))  # type: ignore[arg-type]
        if g is None or g.budget_id != budget_id:
            raise ValueError(f"group {new_group!r} is not a group of budget "
                             f"{budget_id}")
    conn.execute(
        """
        INSERT INTO budget_category_settings
            (budget_id, category_id, bucket, rollover_mode, annual_cents, group_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(budget_id, category_id)
        DO UPDATE SET bucket        = excluded.bucket,
                      rollover_mode = excluded.rollover_mode,
                      annual_cents  = excluded.annual_cents,
                      group_id      = excluded.group_id
        """,
        (budget_id, category_id, new_bucket, new_mode, new_annual,
         None if new_group is None else int(new_group)),  # type: ignore[arg-type]
    )
    conn.execute(
        "UPDATE budget_lines SET rollover = ? WHERE budget_id = ? AND category_id = ?",
        (0 if new_mode == "none" else 1, budget_id, category_id),
    )
    conn.commit()
    return BudgetCategorySettings(
        budget_id=budget_id, category_id=category_id, bucket=new_bucket,
        rollover_mode=new_mode, annual_cents=new_annual,
        group_id=None if new_group is None else int(new_group))  # type: ignore[arg-type]


def clear_settings(conn: sqlite3.Connection, budget_id: int,
                   category_id: int) -> None:
    """Drop a category's settings row, returning it to the flex/no-rollover default.

    The legacy line flag is cleared with it, so no line is left claiming a
    rollover that nothing describes any more.
    """
    conn.execute(
        "DELETE FROM budget_category_settings WHERE budget_id = ? AND category_id = ?",
        (budget_id, category_id))
    conn.execute(
        "UPDATE budget_lines SET rollover = 0 WHERE budget_id = ? AND category_id = ?",
        (budget_id, category_id))
    conn.commit()


# ---- scope: the accounts a budget watches and the categories it leaves out ----
def budget_account_ids(conn: sqlite3.Connection, budget_id: int) -> list[int]:
    """The spending accounts this budget covers (schema v114).

    The household's spending accounts (:func:`spending_account_ids`), narrowed
    to the ones chosen for the budget when any are stored; with none stored,
    every spending account. A business checking account beside the household's
    is the case: its spending is real but is not the household budget's, and a
    plan that counted it would show business costs in "Everything else".
    """
    chosen = {int(r["account_id"]) for r in conn.execute(
        "SELECT account_id FROM budget_accounts WHERE budget_id = ?",
        (budget_id,)).fetchall()}
    all_spending = spending_account_ids(conn)
    if not chosen:
        return all_spending
    return [a for a in all_spending if a in chosen]


def set_budget_accounts(conn: sqlite3.Connection, budget_id: int,
                        account_ids: Optional[Iterable[int]]) -> None:
    """Choose the accounts a budget covers; ``None`` or an empty set means every
    spending account (the stored rows are cleared)."""
    conn.execute("DELETE FROM budget_accounts WHERE budget_id = ?", (budget_id,))
    for aid in sorted({int(a) for a in (account_ids or ())}):
        conn.execute("INSERT INTO budget_accounts (budget_id, account_id) VALUES (?, ?)",
                     (budget_id, aid))
    conn.commit()


def excluded_categories(conn: sqlite3.Connection, budget_id: int) -> set[int]:
    """Categories the budget leaves out entirely (schema v114): not offered as
    lines, not counted in "Everything else", not proposed. Business categories
    in a household ledger are the case."""
    return {int(r["category_id"]) for r in conn.execute(
        "SELECT category_id FROM budget_excluded_categories WHERE budget_id = ?",
        (budget_id,)).fetchall()}


def set_excluded_categories(conn: sqlite3.Connection, budget_id: int,
                            category_ids: Iterable[int]) -> None:
    """Replace the budget's excluded-category set."""
    conn.execute("DELETE FROM budget_excluded_categories WHERE budget_id = ?",
                 (budget_id,))
    for cid in sorted({int(c) for c in category_ids}):
        conn.execute("INSERT INTO budget_excluded_categories (budget_id, category_id) "
                     "VALUES (?, ?)", (budget_id, cid))
    conn.commit()


def _drop_excluded(actuals: dict[int, int], excluded: set[int]) -> None:
    for cid in list(actuals):
        if cid in excluded:
            del actuals[cid]


# ---- groups: a named pot inside one budget (schema v111) --------------------
_GROUP_COLUMNS = ("SELECT id, budget_id, name, bucket, rollover_mode, annual_cents, "
                  "payee_match FROM budget_groups")


def _group_row(r) -> BudgetGroup:
    return BudgetGroup(id=int(r["id"]), budget_id=int(r["budget_id"]),
                       name=r["name"], bucket=r["bucket"],
                       rollover_mode=r["rollover_mode"],
                       annual_cents=int(r["annual_cents"]),
                       payee_match=r["payee_match"] or None)


def create_group(conn: sqlite3.Connection, budget_id: int, name: str, *,
                 bucket: str = "flex", rollover_mode: str = "none",
                 payee_match: Optional[str] = None) -> int:
    """Create a group in ``budget_id`` and return its id.

    The name must be non-blank and unique within the budget (case-insensitive),
    because it is the label the pot is tracked and reported under: two pots
    called Food are two rows nobody can tell apart. With ``payee_match`` the
    group is a PAYEE line (see :class:`BudgetGroup`) and takes no members.
    """
    clean = (name or "").strip()
    if not clean:
        raise ValueError("a group needs a name")
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}; expected one of {BUCKETS}")
    if rollover_mode not in ROLLOVER_MODES:
        raise ValueError(f"unknown rollover mode {rollover_mode!r}; expected one "
                         f"of {ROLLOVER_MODES}")
    if any(g.name.lower() == clean.lower() for g in list_groups(conn, budget_id)):
        raise ValueError(f"budget {budget_id} already has a group named {clean!r}")
    match = (payee_match or "").strip() or None
    cur = conn.execute(
        "INSERT INTO budget_groups (budget_id, name, bucket, rollover_mode, "
        "payee_match) VALUES (?, ?, ?, ?, ?)",
        (budget_id, clean, bucket, rollover_mode, match))
    conn.commit()
    return int(cur.lastrowid)


def list_groups(conn: sqlite3.Connection, budget_id: int) -> list[BudgetGroup]:
    """A budget's groups, by name then id."""
    return [_group_row(r) for r in conn.execute(
        _GROUP_COLUMNS + " WHERE budget_id = ? ORDER BY name COLLATE NOCASE, id",
        (budget_id,)).fetchall()]


def get_group(conn: sqlite3.Connection, group_id: int) -> Optional[BudgetGroup]:
    """One group by id, or ``None``."""
    r = conn.execute(_GROUP_COLUMNS + " WHERE id = ?", (int(group_id),)).fetchone()
    return None if r is None else _group_row(r)


def update_group(conn: sqlite3.Connection, group_id: int, *,
                 name: object = _UNSET, bucket: object = _UNSET,
                 rollover_mode: object = _UNSET,
                 annual_cents: object = _UNSET,
                 payee_match: object = _UNSET) -> BudgetGroup:
    """Change a group's name, bucket, rollover mode, annual total or payee
    match; an unpassed argument keeps its stored value."""
    g = get_group(conn, group_id)
    if g is None:
        raise KeyError(f"no group {group_id}")
    new_name = g.name if name is _UNSET else str(name).strip()
    new_bucket = g.bucket if bucket is _UNSET else str(bucket)
    new_mode = g.rollover_mode if rollover_mode is _UNSET else str(rollover_mode)
    new_annual = (g.annual_cents if annual_cents is _UNSET
                  else int(annual_cents))  # type: ignore[arg-type]
    new_match = (g.payee_match if payee_match is _UNSET
                 else ((str(payee_match).strip() or None)  # type: ignore[arg-type]
                       if payee_match is not None else None))
    if not new_name:
        raise ValueError("a group needs a name")
    if new_bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {new_bucket!r}; expected one of {BUCKETS}")
    if new_mode not in ROLLOVER_MODES:
        raise ValueError(f"unknown rollover mode {new_mode!r}; expected one of "
                         f"{ROLLOVER_MODES}")
    if any(o.id != g.id and o.name.lower() == new_name.lower()
           for o in list_groups(conn, g.budget_id)):
        raise ValueError(f"budget {g.budget_id} already has a group named "
                         f"{new_name!r}")
    conn.execute(
        "UPDATE budget_groups SET name = ?, bucket = ?, rollover_mode = ?, "
        "annual_cents = ?, payee_match = ? WHERE id = ?",
        (new_name, new_bucket, new_mode, new_annual, new_match, g.id))
    conn.commit()
    return get_group(conn, g.id)  # type: ignore[return-value]


def delete_group(conn: sqlite3.Connection, group_id: int) -> None:
    """Delete a group and its lines; its members are RELEASED (their settings
    rows lose the pointer), not deleted. Their own lines were folded into the
    group's when they joined and are not restored: the pot's money was one
    number, and splitting it back out would be the guesswork joining avoided.
    """
    conn.execute("UPDATE budget_category_settings SET group_id = NULL "
                 "WHERE group_id = ?", (int(group_id),))
    conn.execute("DELETE FROM budget_group_lines WHERE group_id = ?", (int(group_id),))
    conn.execute("DELETE FROM budget_groups WHERE id = ?", (int(group_id),))
    conn.commit()


def group_members(conn: sqlite3.Connection,
                  budget_id: int) -> dict[int, list[int]]:
    """``group id -> [member category ids]`` for every group of the budget,
    groups with no members included (as empty lists), members by category id."""
    out: dict[int, list[int]] = {g.id: [] for g in list_groups(conn, budget_id)}
    for r in conn.execute(
            "SELECT category_id, group_id FROM budget_category_settings "
            "WHERE budget_id = ? AND group_id IS NOT NULL "
            "ORDER BY category_id", (budget_id,)).fetchall():
        out.setdefault(int(r["group_id"]), []).append(int(r["category_id"]))
    return out


def member_groups(conn: sqlite3.Connection, budget_id: int) -> dict[int, int]:
    """``member category id -> its group id`` over the budget."""
    return {cid: gid for gid, cids in group_members(conn, budget_id).items()
            for cid in cids}


def set_member_group(conn: sqlite3.Connection, budget_id: int, category_id: int,
                     group_id: Optional[int]) -> None:
    """Put a category into a group, or (``None``) take it out of whichever one
    holds it.

    JOINING FOLDS THE LINES: every month's line the category holds is added into
    the group's line for that month and removed from the category, so the pot is
    typed once and "Food 600" is never split back out by guesswork. The
    category's carry overrides stay where they are: nothing reads them while it
    is a member, and they resume if it leaves. Leaving clears the pointer and
    nothing else -- the money stays in the pot it was folded into. A category
    is in at most one group per budget, so joining a second group is a move.
    """
    if group_id is not None:
        g = get_group(conn, int(group_id))
        if g is None or g.budget_id != budget_id:
            raise ValueError(f"group {group_id!r} is not a group of budget "
                             f"{budget_id}")
        for ln in get_lines(conn, budget_id):
            if ln.category_id != int(category_id):
                continue
            have = conn.execute(
                "SELECT amount_cents FROM budget_group_lines "
                "WHERE budget_id = ? AND group_id = ? AND period = ?",
                (budget_id, g.id, ln.period)).fetchone()
            set_group_line(conn, budget_id, g.id, ln.period,
                           (int(have["amount_cents"]) if have else 0)
                           + ln.amount_cents)
        conn.execute("DELETE FROM budget_lines WHERE budget_id = ? AND "
                     "category_id = ?", (budget_id, int(category_id)))
    set_settings(conn, budget_id, int(category_id),
                 group_id=None if group_id is None else int(group_id))


def set_group_members(conn: sqlite3.Connection, group_id: int,
                      category_ids: Iterable[int]) -> None:
    """Make ``category_ids`` exactly the group's members: joins the new ones
    (folding their lines) and releases the rest."""
    g = get_group(conn, group_id)
    if g is None:
        raise KeyError(f"no group {group_id}")
    wanted = {int(c) for c in category_ids}
    current = set(group_members(conn, g.budget_id).get(g.id, []))
    for cid in sorted(current - wanted):
        set_member_group(conn, g.budget_id, cid, None)
    for cid in sorted(wanted - current):
        set_member_group(conn, g.budget_id, cid, g.id)


def set_group_line(conn: sqlite3.Connection, budget_id: int, group_id: int,
                   period: str, amount_cents: int) -> None:
    """Upsert one month's target for a group."""
    _split_period(period)
    conn.execute(
        """
        INSERT INTO budget_group_lines (budget_id, group_id, period, amount_cents)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(budget_id, group_id, period)
        DO UPDATE SET amount_cents = excluded.amount_cents
        """,
        (budget_id, int(group_id), period, int(amount_cents)))
    conn.commit()


def get_group_lines(conn: sqlite3.Connection, budget_id: int, *,
                    period: Optional[str] = None) -> list[GroupLine]:
    """A budget's group lines, optionally for one month, by period then group."""
    sql = ("SELECT budget_id, group_id, period, amount_cents "
           "FROM budget_group_lines WHERE budget_id = ?")
    params: list = [budget_id]
    if period is not None:
        sql += " AND period = ?"
        params.append(period)
    sql += " ORDER BY period, group_id"
    return [GroupLine(budget_id=int(r["budget_id"]), group_id=int(r["group_id"]),
                      period=r["period"], amount_cents=int(r["amount_cents"]))
            for r in conn.execute(sql, params).fetchall()]


def clear_group_line(conn: sqlite3.Connection, budget_id: int, group_id: int,
                     period: str) -> None:
    """Remove one month's target for a group (an emptied cell)."""
    conn.execute("DELETE FROM budget_group_lines "
                 "WHERE budget_id = ? AND group_id = ? AND period = ?",
                 (budget_id, int(group_id), period))
    conn.commit()


# ---- non-monthly spreading --------------------------------------------------
def spread_annual(annual_cents: int, n_months: int = 12) -> list[int]:
    """Split a yearly total into ``n_months`` monthly set-asides that SUM EXACTLY.

    A yearly 1,000.00 is not twelve times 83.33 -- that is 999.96, and a plan that
    quietly loses four cents a year is a plan the user cannot reconcile against
    the bill. So the remainder is distributed a cent at a time over the earliest
    months: 100000 cents becomes four 8,334s followed by eight 8,333s. Integer
    floor division keeps the remainder in ``[0, n)`` for a negative total too, so
    the sum identity holds in both directions.
    """
    n = int(n_months)
    if n <= 0:
        return []
    total = int(annual_cents)
    base = total // n
    rem = total - base * n
    return [base + (1 if i < rem else 0) for i in range(n)]


def apply_nonmonthly(conn: sqlite3.Connection, budget_id: int, category_id: int,
                     annual_cents: int, *, start_period: str,
                     rollover_mode: str = "both", months: int = 12) -> list[int]:
    """Store a yearly total for a category and write its spread monthly lines.

    Returns the amounts written, oldest month first, so a caller can show the
    user the exact schedule it just created. ``rollover_mode`` defaults to
    ``"both"`` because a non-monthly envelope is pointless without carry: the
    whole point is that eleven months of set-aside are still there when the bill
    lands.
    """
    _split_period(start_period)
    amounts = spread_annual(annual_cents, months)
    set_settings(conn, budget_id, category_id, bucket="nonmonthly",
                 rollover_mode=rollover_mode, annual_cents=int(annual_cents))
    for period, cents in zip(period_sequence(start_period, months), amounts):
        set_line(conn, budget_id, category_id, period, cents)
    return amounts


def apply_nonmonthly_group(conn: sqlite3.Connection, group_id: int,
                           annual_cents: int, *, start_period: str,
                           rollover_mode: str = "both",
                           months: int = 12) -> list[int]:
    """:func:`apply_nonmonthly` for a GROUP: store the yearly total on the group
    and write its spread as group lines."""
    _split_period(start_period)
    g = get_group(conn, group_id)
    if g is None:
        raise KeyError(f"no group {group_id}")
    amounts = spread_annual(annual_cents, months)
    update_group(conn, g.id, bucket="nonmonthly", rollover_mode=rollover_mode,
                 annual_cents=int(annual_cents))
    for period, cents in zip(period_sequence(start_period, months), amounts):
        set_group_line(conn, g.budget_id, g.id, period, cents)
    return amounts


# ---- carry overrides --------------------------------------------------------
def set_carry_override(conn: sqlite3.Connection, budget_id: int, category_id: int,
                       period: str, amount_cents: int, *,
                       note: Optional[str] = None,
                       set_at: Optional[str] = None) -> None:
    """Pin the carry-in for one ``(category, period)`` by hand.

    An override exists because the computed carry is only ever as good as the
    history behind it: when a user starts budgeting mid-year, or forgives an
    overspend, the recursion's answer is arithmetically right and practically
    wrong. Storing the override -- rather than editing the past -- keeps the
    history intact and leaves an audit trail (``note``, ``set_at``).
    """
    _split_period(period)
    stamp = set_at or _dt.date.today().isoformat()
    conn.execute(
        """
        INSERT INTO budget_carry_overrides
            (budget_id, category_id, period, amount_cents, note, set_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(budget_id, category_id, period)
        DO UPDATE SET amount_cents = excluded.amount_cents,
                      note         = excluded.note,
                      set_at       = excluded.set_at
        """,
        (budget_id, category_id, period, int(amount_cents), note, stamp),
    )
    conn.commit()


def get_carry_overrides(conn: sqlite3.Connection, budget_id: int, *,
                        category_id: Optional[int] = None,
                        period: Optional[str] = None) -> list[CarryOverride]:
    """A budget's carry overrides, optionally narrowed to one category or month."""
    sql = ("SELECT budget_id, category_id, period, amount_cents, note, set_at "
           "FROM budget_carry_overrides WHERE budget_id = ?")
    params: list = [budget_id]
    if category_id is not None:
        sql += " AND category_id = ?"
        params.append(int(category_id))
    if period is not None:
        sql += " AND period = ?"
        params.append(period)
    sql += " ORDER BY period, category_id"
    return [
        CarryOverride(budget_id=r["budget_id"], category_id=r["category_id"],
                      period=r["period"], amount_cents=r["amount_cents"],
                      note=r["note"], set_at=r["set_at"])
        for r in conn.execute(sql, params).fetchall()
    ]


def clear_carry_override(conn: sqlite3.Connection, budget_id: int,
                         category_id: int, period: str) -> None:
    """Remove one carry override, handing the month back to the computation."""
    conn.execute(
        "DELETE FROM budget_carry_overrides "
        "WHERE budget_id = ? AND category_id = ? AND period = ?",
        (budget_id, category_id, period))
    conn.commit()


# ---- scenarios --------------------------------------------------------------
def copy_budget(conn: sqlite3.Connection, src_id: int, name: str, *,
                shift_months: int = 0, active: bool = False) -> int:
    """Duplicate a budget -- lines, settings, overrides, saving targets and note --
    and return the id.

    A scenario ("what if we cut dining by a third?") is an ordinary budget, so no
    new table is needed; this is the whole of scenario support. ``shift_months``
    moves every period, which is what makes last year's plan usable as next
    year's draft: a twelve-month shift lands each line in the same calendar month.

    The copy is created INACTIVE by default. Exactly one budget is active at a
    time, and a duplicate that silently became the live plan would change what
    Track reports without the user asking for it; call :func:`set_only_active`
    when the scenario is meant to take over.
    """
    src = get_budget(conn, src_id)
    if src is None:
        raise KeyError(f"no budget {src_id}")
    shift = int(shift_months)
    new_id = create_budget(conn, name, active=active)
    conn.execute(
        "UPDATE budgets SET start_period = ?, end_period = ?, note = ? WHERE id = ?",
        (shift_period(src.start_period, shift) if src.start_period else None,
         shift_period(src.end_period, shift) if src.end_period else None,
         src.note, new_id))
    for ln in get_lines(conn, src_id):
        conn.execute(
            "INSERT INTO budget_lines "
            "(budget_id, category_id, period, amount_cents, rollover) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_id, ln.category_id, shift_period(ln.period, shift),
             ln.amount_cents, 1 if ln.rollover else 0))
    # Groups first, so the copied settings can point at the COPIED groups: a
    # scenario's Food pot is its own row, never the source budget's.
    group_map: dict[int, int] = {}
    for g in list_groups(conn, src_id):
        cur = conn.execute(
            "INSERT INTO budget_groups (budget_id, name, bucket, rollover_mode, "
            "annual_cents, payee_match) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id, g.name, g.bucket, g.rollover_mode, g.annual_cents,
             g.payee_match))
        group_map[g.id] = int(cur.lastrowid)
    for gl in get_group_lines(conn, src_id):
        conn.execute(
            "INSERT INTO budget_group_lines (budget_id, group_id, period, "
            "amount_cents) VALUES (?, ?, ?, ?)",
            (new_id, group_map[gl.group_id], shift_period(gl.period, shift),
             gl.amount_cents))
    for st in get_settings(conn, src_id).values():
        conn.execute(
            "INSERT INTO budget_category_settings "
            "(budget_id, category_id, bucket, rollover_mode, annual_cents, group_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id, st.category_id, st.bucket, st.rollover_mode, st.annual_cents,
             group_map.get(st.group_id) if st.group_id is not None else None))
    for ov in get_carry_overrides(conn, src_id):
        conn.execute(
            "INSERT INTO budget_carry_overrides "
            "(budget_id, category_id, period, amount_cents, note, set_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id, ov.category_id, shift_period(ov.period, shift),
             ov.amount_cents, ov.note, ov.set_at))
    for sl in get_saving_lines(conn, src_id):
        conn.execute(
            "INSERT INTO budget_saving_lines "
            "(budget_id, account_id, period, amount_cents) VALUES (?, ?, ?, ?)",
            (new_id, sl.account_id, shift_period(sl.period, shift),
             sl.amount_cents))
    for period, cents in get_other_lines(conn, src_id).items():
        conn.execute(
            "INSERT INTO budget_other_lines (budget_id, period, amount_cents) "
            "VALUES (?, ?, ?)", (new_id, shift_period(period, shift), cents))
    for (kind, ident), position in line_order(conn, src_id).items():
        conn.execute(
            "INSERT INTO budget_line_order (budget_id, kind, ident, position) "
            "VALUES (?, ?, ?, ?)",
            (new_id, kind, group_map.get(ident, ident) if kind == "group" else ident,
             position))
    for r in conn.execute("SELECT account_id FROM budget_accounts WHERE budget_id = ?",
                          (src_id,)).fetchall():
        conn.execute("INSERT INTO budget_accounts (budget_id, account_id) VALUES (?, ?)",
                     (new_id, int(r["account_id"])))
    for cid in sorted(excluded_categories(conn, src_id)):
        conn.execute("INSERT INTO budget_excluded_categories (budget_id, category_id) "
                     "VALUES (?, ?)", (new_id, cid))
    conn.commit()
    return new_id


# ---- seeding a budget from history -----------------------------------------
#: The bases a proposal may rest on. ``scheduled`` is not requestable: it is what
#: a category gets when a recurring definition already states the amount.
SEED_BASES = ("mean", "median", "same_month")

#: Frequencies that recur LESS often than monthly, and so belong in a
#: non-monthly envelope even when a schedule states the amount exactly.
_SUBMONTHLY = ("quarterly", "semiannual", "annual", "yearly")

#: Occurrences per year, per scheduled frequency, for the annual total.
_PER_YEAR = {"weekly": 52, "biweekly": 26, "fortnightly": 26, "semimonthly": 24,
             "monthly": 12, "quarterly": 4, "semiannual": 2, "annual": 1,
             "yearly": 1}


@dataclass(frozen=True)
class SeedProposal:
    """One proposed budget amount, with the evidence behind it.

    A proposal is a READ: nothing here has been written, and the user's Accept is
    what turns it into lines (see :func:`apply_proposals`). Carrying the evidence
    -- the basis, the months seen, the per-month samples -- is what lets the page
    explain itself and offer an alternative without going back to the database.
    """
    category_id: int
    category_name: str
    bucket: str                 # "fixed" | "flex" | "nonmonthly"
    basis: str                  # "scheduled" | "mean" | "median" | "same_month"
    amount_cents: int           # the monthly target (a set-aside, if nonmonthly)
    annual_cents: int = 0       # nonmonthly (or scheduled): the yearly total
    months_seen: int = 0        # months of the window with any spending
    spread_cents: int = 0       # highest month minus lowest, the volatility measure
    volatile: bool = False
    sample_cents: tuple = ()    # the window's per-month spend, oldest first


def trimmed_median(samples: Sequence[int]) -> int:
    """Median of ``samples`` with the single highest and lowest month dropped.

    The trim is the point: one holiday month or one insurance payment otherwise
    drags a mean into a target the user will miss all year. Fewer than three
    samples have nothing to trim, so the plain median is returned. An even count
    averages the two middle values ROUND_HALF_UP at the cents boundary, never a
    float.
    """
    vals = sorted(int(s) for s in samples)
    if not vals:
        return 0
    if len(vals) > 2:
        vals = vals[1:-1]
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return mean_cents(vals[mid - 1] + vals[mid], 2)


def _category_paths(conn: sqlite3.Connection) -> dict[int, str]:
    from mammon import ledger  # lazy: ledger imports nothing from here

    return {c["id"]: c["path"]
            for c in ledger.list_categories(conn, include_hidden=True)}


def _expense_definitions(conn: sqlite3.Connection, *,
                         account_ids: Optional[Iterable[int]] = None
                         ) -> list[tuple[dict, dict[int, int]]]:
    """``(definition, category_id -> cents per occurrence)`` for every ACTIVE
    manual schedule that spends anything.

    A BILL is a definition whose own amount is negative. A plain bill
    contributes its own category; one with a split template contributes each
    negative category leg; transfers contribute nothing. A scheduled PAYCHECK is
    money in and contributes nothing here even when its template carries
    withholding legs: the budget is planned from take-home pay, and a deduction
    is not spending the household does (:func:`_month_actuals`). Its net amount
    is what :func:`scheduled_income` reads.
    """
    from mammon import scheduled as _scheduled

    acct_set = None if account_ids is None else {int(a) for a in account_ids}
    out: list[tuple[dict, dict[int, int]]] = []
    for d in _scheduled.list_scheduled(conn, active_only=True):
        if acct_set is not None and int(d["account_id"]) not in acct_set:
            continue
        if int(d["amount"]) >= 0:
            continue
        legs = _occurrence_categories(conn, d)
        if legs:
            out.append((d, legs))
    return out


def _income_definitions(conn: sqlite3.Connection) -> list[tuple[dict, int, int]]:
    """``(definition, income category id, net cents per occurrence)`` for every
    active scheduled deposit on a spending account: a positive own amount, no
    transfer. The category is the definition's own, or for a split template the
    category of its largest positive leg -- the same attribution
    :func:`month_income` makes for the rows that later post."""
    from mammon import scheduled as _scheduled

    accounts = set(spending_account_ids(conn))
    out = []
    for d in _scheduled.list_scheduled(conn, active_only=True):
        if (d.get("transfer_account_id") is not None or int(d["amount"]) <= 0
                or int(d["account_id"]) not in accounts):
            continue
        cid = d.get("category_id")
        legs = _scheduled.get_scheduled_splits(conn, int(d["id"]))
        best = max((sp for sp in legs if sp["category_id"] is not None
                    and int(sp["amount"]) > 0),
                   key=lambda sp: int(sp["amount"]), default=None)
        if best is not None:
            cid = best["category_id"]
        if cid is None:
            continue
        out.append((d, int(cid), int(d["amount"])))
    return out


def bill_schedule_summary(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    """``category id -> [{payee, frequency, per_occurrence_cents, next_date}]``
    over the active scheduled bills, for a page proposing a line: a category a
    schedule charges is proposed at the schedule's amount and cadence."""
    out: dict[int, list[dict]] = {}
    for d, legs in _expense_definitions(conn):
        for cid, cents in legs.items():
            out.setdefault(int(cid), []).append({
                "payee": d.get("payee") or "",
                "frequency": (d.get("frequency") or "monthly").lower(),
                "per_occurrence_cents": int(cents),
                "next_date": str(d["next_date"])})
    return out


def income_schedule_summary(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    """``income category id -> [{payee, frequency, net_cents, next_date}]``
    over the active scheduled deposits (:func:`_income_definitions`)."""
    out: dict[int, list[dict]] = {}
    for d, cid, net in _income_definitions(conn):
        out.setdefault(cid, []).append({
            "payee": d.get("payee") or "",
            "frequency": (d.get("frequency") or "monthly").lower(),
            "net_cents": int(net), "next_date": str(d["next_date"])})
    return out


def scheduled_income(conn: sqlite3.Connection, periods: Sequence[str]
                     ) -> dict[int, dict[str, int]]:
    """``income category id -> {period: take-home cents}`` from the active
    scheduled deposits, occurrence by occurrence over ``periods`` -- the income
    counterpart of :func:`scheduled_amounts`, and the reason a Net pay line
    plans two paychecks in most months and three in some."""
    out: dict[int, dict[str, int]] = {}
    if not periods:
        return out
    first, _ = period_bounds(min(periods))
    _, last = period_bounds(max(periods))
    wanted = set(periods)
    for d, cid, net in _income_definitions(conn):
        for when in _schedule_dates(d, first, last):
            p = when[:7]
            if p in wanted:
                months = out.setdefault(cid, {})
                months[p] = months.get(p, 0) + net
    return out


def _scheduled_annual(conn: sqlite3.Connection) -> dict[int, tuple[int, str]]:
    """``category_id -> (annual cents, frequency)`` over the active schedules'
    expense legs (:func:`_expense_definitions`).

    Several schedules on one category add up, and the coarsest frequency wins
    as the category's cadence -- a category with both a monthly and an annual
    charge is still one that needs a yearly envelope. A transfer is not
    spending at all (``reports.spending`` excludes it, so budgeting one would
    compare a target against a permanent zero) and never reaches this.
    """
    out: dict[int, tuple[int, str]] = {}
    for d, legs in _expense_definitions(conn):
        freq = (d.get("frequency") or "monthly").lower()
        per_year = _PER_YEAR.get(freq, 12)
        for cid, cents in legs.items():
            annual = int(cents) * per_year
            prev = out.get(cid)
            if prev is None:
                out[cid] = (annual, freq)
            else:
                coarser = freq if freq in _SUBMONTHLY else prev[1]
                out[cid] = (prev[0] + annual, coarser)
    return out


def _schedule_dates(defn: dict, start: str, end: str) -> list[str]:
    """Every due date of ``defn`` in ``[start, end]``, whichever side of its
    ``next_date`` the window lies on.

    :func:`mammon.scheduled.occurrences` walks FORWARD from the next due date,
    which is all a reminder needs. A plan is written for months that may precede
    that date -- a budget started in June for January -- so this steps the
    cadence backwards first (the inverse of ``advance_date``: minus the days,
    minus the months, or the semimonthly predecessor) until it is before the
    window, then walks forward. Bounded, like the forward walk.
    """
    from mammon import scheduled as _scheduled

    freq = (defn.get("frequency") or "monthly").lower()
    due = str(defn["next_date"])
    guard = 0
    while due >= start and guard < 600:
        due = _step_back(due, freq)
        guard += 1
    return _scheduled.occurrences(due, freq, start, end)


def _step_back(iso: str, frequency: str) -> str:
    """The due date one ``frequency`` BEFORE ``iso``."""
    from mammon import scheduled as _scheduled

    f = frequency.lower()
    if f in _scheduled._MONTHLY:
        return _scheduled._add_months(iso, -_scheduled._MONTHLY[f])
    if f == _scheduled._SEMIMONTHLY:
        return _scheduled._semimonthly_back(iso)
    days = _scheduled._DAILY.get(f, 14)
    return (_dt.date.fromisoformat(iso) - _dt.timedelta(days=days)).isoformat()


def scheduled_amounts(conn: sqlite3.Connection, periods: Sequence[str], *,
                      account_ids: Optional[Iterable[int]] = None
                      ) -> dict[int, dict[str, int]]:
    """``category_id -> {period: cents}``: what the active schedules will charge
    each category in each of ``periods``, occurrence by occurrence.

    This is the arithmetic a fixed line is written from. A biweekly deduction
    lands twice in most months and three times in two of them, and a line that
    says so reads "as planned" all year; the annual total over twelve reads over
    in the three-paycheck months and under in the rest, which was the reported
    defect. Positive magnitudes, integer cents, no division anywhere.
    """
    out: dict[int, dict[str, int]] = {}
    if not periods:
        return out
    first, _ = period_bounds(min(periods))
    _, last = period_bounds(max(periods))
    wanted = set(periods)
    for d, legs in _expense_definitions(conn, account_ids=account_ids):
        for when in _schedule_dates(d, first, last):
            p = when[:7]
            if p not in wanted:
                continue
            for cid, cents in legs.items():
                months = out.setdefault(int(cid), {})
                months[p] = months.get(p, 0) + int(cents)
    return out


def schedule_summary(conn: sqlite3.Connection, category_id: int,
                     periods: Sequence[str]) -> list[dict]:
    """The definitions behind :func:`scheduled_amounts` for one category: a
    list of ``{payee, frequency, per_occurrence_cents, occurrences}`` over
    ``periods``, for a page that wants to say what a scheduled fill rests on."""
    if not periods:
        return []
    first, _ = period_bounds(min(periods))
    _, last = period_bounds(max(periods))
    wanted = set(periods)
    out = []
    for d, legs in _expense_definitions(conn):
        cents = legs.get(int(category_id))
        if not cents:
            continue
        n = sum(1 for when in _schedule_dates(d, first, last) if when[:7] in wanted)
        out.append({"payee": d.get("payee") or "",
                    "frequency": (d.get("frequency") or "monthly").lower(),
                    "per_occurrence_cents": int(cents), "occurrences": n})
    return out


def seed_from_history(conn: sqlite3.Connection, *, months: int = 12,
                      end_period: Optional[str] = None, basis: str = "mean",
                      account_ids: Optional[Iterable[int]] = None,
                      today: Optional[_dt.date] = None) -> list[SeedProposal]:
    """Propose a budget amount per category from the trailing ``months`` of history.

    READS ONLY. The window ends at ``end_period`` (default: the last COMPLETE
    month before ``today``, because a partial month drags every average it touches
    low) and is measured over the household spending accounts, so a brokerage fee
    never becomes a grocery target. Each category's samples are its own,
    directly-booked spend per month -- never a subtree roll-up, which would let a
    parent and a child both claim the same money.

    Three shapes come out, and which one a category gets is evidence, not a
    preference:

    * a category with an active recurring bill takes the SCHEDULE's amount
      (``basis="scheduled"``); history cannot beat a definition that states the
      number. A cadence coarser than monthly makes it non-monthly anyway -- the
      amount is known, but it is not due every month.
    * a category seen in at most a third of the window, but at least once, is
      non-monthly: its yearly total goes to ``annual_cents`` and the monthly
      figure is the first slice of :func:`spread_annual`. Averaging a twice-yearly
      insurance bill into twelve equal months is not wrong, but calling it a
      monthly target is.
    * everything else is flex, on the requested ``basis``.

    A category whose spread exceeds its own mean is flagged ``volatile``: the page
    shows the trimmed median beside the mean and lets the user pick. Mammon does
    not choose for them, because which one is right depends on whether the outlier
    was a one-off, and only the user knows that.
    """
    if basis not in SEED_BASES:
        raise ValueError(f"unknown basis {basis!r}; expected one of {SEED_BASES}")
    n_months = int(months)
    if n_months <= 0:
        return []
    if end_period is None:
        ref = today or _dt.date.today()
        end_period = shift_period(f"{ref.year:04d}-{ref.month:02d}", -1)
    else:
        _split_period(end_period)
    periods = period_sequence(shift_period(end_period, -(n_months - 1)), n_months)
    if account_ids is None:
        account_ids = spending_account_ids(conn)
    ids = list(account_ids)

    monthly = [_month_actuals(conn, p, account_ids=ids) for p in periods]
    scheduled_by_cat = _scheduled_annual(conn)
    paths = _category_paths(conn)

    seen_ids = {cid for m in monthly for cid in m}
    proposals: list[SeedProposal] = []
    for cid in sorted(seen_ids | set(scheduled_by_cat)):
        samples = tuple(m.get(cid, 0) for m in monthly)
        total = sum(samples)
        months_seen = sum(1 for s in samples if s)
        spread = (max(samples) - min(samples)) if samples else 0
        mean = mean_cents(total, n_months)

        sched = scheduled_by_cat.get(cid)
        if sched is not None:
            annual, freq = sched
            if freq in _SUBMONTHLY:
                bucket, amount = "nonmonthly", spread_annual(annual, 12)[0]
            else:
                bucket, amount = "fixed", mean_cents(annual, 12)
            used_basis = "scheduled"
        elif months_seen and months_seen <= n_months // 3:
            annual = mean_cents(total * 12, n_months)
            bucket, amount = "nonmonthly", spread_annual(annual, 12)[0]
            used_basis = "mean"
        else:
            annual, bucket, used_basis = 0, "flex", basis
            if basis == "median":
                amount = trimmed_median(samples)
            elif basis == "same_month":
                amount = _same_month_sample(periods, samples, end_period, mean)
            else:
                amount = mean

        proposals.append(SeedProposal(
            category_id=cid, category_name=paths.get(cid, ""), bucket=bucket,
            basis=used_basis, amount_cents=amount, annual_cents=annual,
            months_seen=months_seen, spread_cents=spread,
            volatile=spread > mean, sample_cents=samples))
    proposals.sort(key=lambda p: (p.category_name.lower(), p.category_id))
    return proposals


def _same_month_sample(periods: Sequence[str], samples: Sequence[int],
                       end_period: str, fallback: int) -> int:
    """The sample from the calendar month the plan is about to start in.

    "Same month last year" is the right basis for spending that is seasonal but
    not rare -- heating, school supplies -- where a mean flattens the very shape
    the user is budgeting for. If the window does not reach that calendar month,
    the mean stands in rather than a zero: a missing sample is not evidence of no
    spending.
    """
    target_month = _split_period(shift_period(end_period, 1))[1]
    for period, sample in zip(periods, samples):
        if _split_period(period)[1] == target_month:
            return int(sample)
    return fallback


def _round_to_dollar(cents: int) -> int:
    """``cents`` rounded to the nearest whole dollar, ROUND_HALF_UP, in cents."""
    return int((Decimal(int(cents)) / 100).quantize(
        Decimal(1), rounding=ROUND_HALF_UP)) * 100


def apply_proposals(conn: sqlite3.Connection, budget_id: int,
                    proposals: Iterable[SeedProposal], *, start_period: str,
                    months: int = 12, round_to_dollar: bool = False) -> int:
    """Write accepted proposals as lines and settings; return the line count.

    This is the ONLY thing that turns a proposal into stored state, and it runs
    only on an explicit Accept -- seeding must never write behind the user's back,
    because a budget they did not agree to is one they will not trust.

    A non-monthly proposal gets its twelve spread amounts, repeating if more than
    a year is requested; a fixed proposal from a SCHEDULE gets each month's
    occurrences times the amount per occurrence (:func:`scheduled_amounts`), so
    a biweekly deduction lands two or three times as the calendar says; every
    other proposal gets the same figure each month. ``round_to_dollar`` applies
    per line with NO remainder redistribution: round numbers are the point,
    and re-adding the pennies somewhere to make the total come out would undo
    exactly that.

    A proposal for a category that is a MEMBER of one of the budget's groups is
    folded into the group's lines instead (its setting row keeps its group), so
    seeding a plan with a Food pot proposes Food as the sum of its members.
    """
    _split_period(start_period)
    periods = period_sequence(start_period, months)
    member_of = member_groups(conn, budget_id)
    by_schedule: Optional[dict[int, dict[str, int]]] = None
    group_add: dict[int, dict[str, int]] = {}
    written = 0
    for p in proposals:
        if p.bucket == "nonmonthly":
            spread = spread_annual(p.annual_cents, 12)
            amounts = [spread[i % 12] for i in range(len(periods))]
        elif p.bucket == "fixed" and p.basis == "scheduled":
            if by_schedule is None:
                by_schedule = scheduled_amounts(conn, periods)
            months_of = by_schedule.get(p.category_id, {})
            amounts = [months_of.get(period, 0) for period in periods]
        else:
            amounts = [p.amount_cents] * len(periods)
        if round_to_dollar:
            amounts = [_round_to_dollar(a) for a in amounts]
        gid = member_of.get(p.category_id)
        if gid is not None:
            slot = group_add.setdefault(gid, {})
            for period, cents in zip(periods, amounts):
                slot[period] = slot.get(period, 0) + cents
            continue
        mode = "both" if p.bucket == "nonmonthly" else "none"
        set_settings(conn, budget_id, p.category_id, bucket=p.bucket,
                     rollover_mode=mode, annual_cents=p.annual_cents)
        for period, cents in zip(periods, amounts):
            set_line(conn, budget_id, p.category_id, period, cents)
            written += 1
    for gid, slot in group_add.items():
        for period, cents in slot.items():
            set_group_line(conn, budget_id, gid, period, cents)
            written += 1
    return written


def fill_line_from_schedule(conn: sqlite3.Connection, budget_id: int,
                            category_id: int, *,
                            months: int = PLAN_MONTHS) -> dict[str, int]:
    """Write a category's twelve months from its active schedules and mark it
    ``fixed``: each month gets the occurrences landing in it times the amount
    per occurrence (:func:`scheduled_amounts`). Returns ``{period: cents}`` as
    written, months with no occurrence included as 0 so the row is complete.
    A category no schedule touches raises, because there is nothing to fill
    from and a silent row of zeros would look like a plan.
    """
    budget = get_budget(conn, budget_id)
    if budget is None or not budget.start_period:
        raise ValueError(f"budget {budget_id} has no start month")
    periods = plan_periods(budget.start_period, months)
    months_of = scheduled_amounts(conn, periods).get(int(category_id))
    if not months_of:
        raise ValueError(f"no active schedule charges category {category_id}")
    set_settings(conn, budget_id, int(category_id), bucket="fixed",
                 rollover_mode="none")
    out: dict[str, int] = {}
    for period in periods:
        cents = months_of.get(period, 0)
        set_line(conn, budget_id, int(category_id), period, cents)
        out[period] = cents
    return out


# ---- what the household did, for proposing a line (SRD 5.12) ----------------
@dataclass(frozen=True)
class HistorySamples:
    """The last complete months' figures the Budget page proposes a line from:
    ``spending`` is ``category id -> per-month own spending at take-home``,
    ``income`` is ``category id -> per-month take-home received``, ``saving``
    is ``account id -> per-month net saved``; every list is oldest first over
    ``periods``. Read-only, like everything the page proposes from."""
    periods: tuple
    spending: dict
    income: dict
    saving: dict

    @staticmethod
    def summary(samples: Sequence[int]) -> dict:
        """``total``, ``mean``, ``high``, ``high_index``, ``median`` and
        ``volatile`` (the spread exceeds the mean) for one line's samples."""
        vals = [int(v) for v in samples]
        n = max(len(vals), 1)
        total = sum(vals)
        mean = mean_cents(total, n)
        high = max(vals) if vals else 0
        return {"total": total, "mean": mean, "high": high,
                "high_index": vals.index(high) if vals else 0,
                "median": trimmed_median(vals) if vals else 0,
                "volatile": bool(vals) and (high - min(vals)) > mean > 0}


def trailing_samples(conn: sqlite3.Connection, *, today: Optional[_dt.date] = None,
                     months: int = 12,
                     account_ids: Optional[Iterable[int]] = None) -> HistorySamples:
    """The last ``months`` COMPLETE months of spending, income and saving, per
    category or account (the current month is left out: a partial month drags
    every average it touches), over ``account_ids`` or every spending
    account."""
    day = today or _dt.date.today()
    window = trailing_months(day, months)
    periods = tuple(p for p, _s, _e in window)
    ids = None if account_ids is None else [int(a) for a in account_ids]
    spend_by_month = [_month_actuals(conn, p, account_ids=ids) for p in periods]
    income_by_month = [month_income(conn, p, account_ids=ids) for p in periods]
    saving_by_month = [month_saving(conn, p) for p in periods]

    def pivot(by_month: list[dict]) -> dict:
        keys = {k for m in by_month for k in m}
        return {k: [m.get(k, 0) for m in by_month] for k in keys}

    return HistorySamples(periods=periods, spending=pivot(spend_by_month),
                          income=pivot(income_by_month),
                          saving=pivot(saving_by_month))


# ---- the twelve-month plan (SRD 5.12) ----------------------------------------
#: How often a line recurs within the plan, as the step between months. "once"
#: is a single month; "bi-monthly" is every OTHER month, not twice a month.
FREQUENCIES = {"monthly": 1, "bi-monthly": 2, "quarterly": 3,
               "semi-annually": 6, "once": None}


def plan_periods(start_period: str, months: int = PLAN_MONTHS) -> list[str]:
    """The ISO months a budget starting at ``start_period`` covers."""
    return period_sequence(start_period, months)


#: Frequencies FINER than a month, which take a first DATE rather than a first
#: month: a biweekly paycheck lands twice in most months and three times in two
#: of them, and which two depends on the payday, not on the cadence. Values are
#: the step in days; the names are ``mammon.scheduled``'s so one advance rule
#: serves both.
DATED_FREQUENCIES = {"weekly": 7, "biweekly": 14}

#: Occurrences per year of a dated frequency, for proposing a per-occurrence
#: amount from a year of history.
DATED_PER_YEAR = {"weekly": 52, "biweekly": 26}


def dated_frequency_counts(start_period: str, first_date: str, frequency: str,
                           months: int = PLAN_MONTHS) -> dict[str, int]:
    """``period -> occurrences`` of a line recurring every ``frequency`` from
    ``first_date`` over the plan starting ``start_period``.

    The walk is :func:`mammon.scheduled.occurrences`: forward from
    ``first_date`` in steps of the cadence, so a first date before the plan
    anchors the grid and the plan's own months take what lands in them, and a
    first date inside the plan leaves the earlier months empty (nothing lands
    before the first occurrence). Only months with at least one occurrence are
    keys, so a caller writes exactly the months the calendar fills.
    """
    from mammon import scheduled as _scheduled

    if frequency not in DATED_FREQUENCIES:
        raise ValueError(f"unknown dated frequency {frequency!r}; expected one of "
                         f"{tuple(DATED_FREQUENCIES)}")
    _dt.date.fromisoformat(first_date)           # validate early
    window = plan_periods(start_period, months)
    first, _ = period_bounds(window[0])
    _, last = period_bounds(window[-1])
    if first_date > last:
        raise ValueError(f"{first_date!r} is after the plan ending {window[-1]!r}")
    counts: dict[str, int] = {}
    for when in _scheduled.occurrences(first_date, frequency, first, last):
        counts[when[:7]] = counts.get(when[:7], 0) + 1
    return counts


def frequency_periods(start_period: str, first_period: str, frequency: str,
                      months: int = PLAN_MONTHS) -> list[str]:
    """The months of the plan a line recurring at ``frequency`` from
    ``first_period`` lands in. Nothing lands before ``first_period`` and nothing
    wraps past the end of the plan: a quarterly bill first due in the plan's
    second month is due in months 2, 5, 8 and 11, never also in month 1."""
    if frequency not in FREQUENCIES:
        raise ValueError(f"unknown frequency {frequency!r}; expected one of "
                         f"{tuple(FREQUENCIES)}")
    window = plan_periods(start_period, months)
    if first_period not in window:
        raise ValueError(f"{first_period!r} is not in the plan starting "
                         f"{start_period!r}")
    step = FREQUENCIES[frequency]
    first = window.index(first_period)
    if step is None:
        return [first_period]
    return window[first::step]


def default_budget_name(conn: sqlite3.Connection, start_period: str) -> str:
    """``new-budget-9-26`` for a plan starting September 2026, with ``-2``,
    ``-3`` ... appended when that name is taken. A placeholder the user is
    expected to overwrite, so it only has to be unique and say when."""
    year, month = _split_period(start_period)
    base = f"new-budget-{month}-{year % 100:02d}"
    taken = {b.name for b in list_budgets(conn)}
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def new_budget(conn: sqlite3.Connection, start_period: str, *,
               copy_from: Optional[int] = None,
               months: int = PLAN_MONTHS) -> int:
    """Create a twelve-month budget starting at ``start_period`` and return its
    id. It is EMPTY unless ``copy_from`` names a budget, in which case that
    budget's lines, settings, overrides and saving targets come with it,
    re-dated so each amount keeps its calendar month (see
    :func:`move_budget_start`). It is active only when no other budget is."""
    _split_period(start_period)
    name = default_budget_name(conn, start_period)
    any_active = any(b.active for b in list_budgets(conn))
    if copy_from is None:
        bid = create_budget(conn, name, active=not any_active)
        set_budget_period(conn, bid, start_period,
                          shift_period(start_period, months - 1))
        return bid
    bid = copy_budget(conn, copy_from, name, active=not any_active)
    move_budget_start(conn, bid, start_period, months=months)
    return bid


def _wrapped(period: str, window: list[str]) -> str:
    """The month of ``window`` with the same calendar month as ``period``."""
    month = _split_period(period)[1]
    return next(p for p in window if _split_period(p)[1] == month)


def move_budget_start(conn: sqlite3.Connection, budget_id: int,
                      start_period: str, *, months: int = PLAN_MONTHS) -> None:
    """Move a budget to start at ``start_period``, WRAPPING its months around.

    Every amount keeps its calendar month: moving a plan from Sep 2026 - Aug 2027
    to Jan 2027 - Dec 2027 moves the November 2026 figure to November 2027 and
    leaves the March 2027 one where it is. That is what re-dating a plan means
    to a person - "the same budget, for next year" - and it is how a copied 2027
    budget becomes the 2028 one by changing only its start.

    Lines, saving targets and carry overrides all move. A budget that somehow
    holds two lines for one calendar month (one spanning more than a year from
    before plans were fixed at twelve months) keeps the one that was inside its
    old window, so nothing the user could see is replaced by something they
    could not."""
    _split_period(start_period)
    budget = get_budget(conn, budget_id)
    if budget is None:
        raise KeyError(f"no budget {budget_id}")
    new_window = plan_periods(start_period, months)
    old_window = set(plan_periods(budget.start_period, months)
                     if budget.start_period else [])

    def remap(table: str, key: str) -> None:
        extra = {"budget_lines": ", rollover",
                 "budget_carry_overrides": ", note, set_at"}.get(table, "")
        full = conn.execute(
            f"SELECT {key}, period, amount_cents{extra} FROM {table} "  # noqa: S608
            f"WHERE budget_id = ?", (budget_id,)).fetchall()
        # Inside-the-old-window rows are written LAST so they win a collision.
        ordered = sorted(full, key=lambda r: r["period"] in old_window)
        conn.execute(f"DELETE FROM {table} WHERE budget_id = ?",  # noqa: S608
                     (budget_id,))
        kept: dict = {}
        for r in ordered:
            kept[(r[key], _wrapped(r["period"], new_window))] = r
        for (k, period), r in kept.items():
            cols = ["budget_id", key, "period", "amount_cents"]
            vals = [budget_id, k, period, r["amount_cents"]]
            if table == "budget_lines":
                cols.append("rollover")
                vals.append(r["rollover"])
            elif table == "budget_carry_overrides":
                cols += ["note", "set_at"]
                vals += [r["note"], r["set_at"]]
            conn.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) "  # noqa: S608
                f"VALUES ({', '.join('?' for _ in cols)})", vals)

    remap("budget_lines", "category_id")
    remap("budget_saving_lines", "account_id")
    remap("budget_carry_overrides", "category_id")
    remap("budget_group_lines", "group_id")
    others = get_other_lines(conn, budget_id)
    conn.execute("DELETE FROM budget_other_lines WHERE budget_id = ?", (budget_id,))
    kept_other: dict[str, int] = {}
    for period in sorted(others, key=lambda p: p in old_window):
        kept_other[_wrapped(period, new_window)] = others[period]
    for period, cents in kept_other.items():
        conn.execute("INSERT INTO budget_other_lines (budget_id, period, amount_cents) "
                     "VALUES (?, ?, ?)", (budget_id, period, cents))
    conn.execute("UPDATE budgets SET start_period = ?, end_period = ? WHERE id = ?",
                 (start_period, new_window[-1], budget_id))
    conn.commit()


def fill_line(conn: sqlite3.Connection, budget_id: int, amount_cents: int, *,
              frequency: str, first_period: Optional[str] = None,
              first_date: Optional[str] = None,
              category_id: Optional[int] = None,
              account_id: Optional[int] = None,
              group_id: Optional[int] = None) -> list[str]:
    """Write a line's months for a category or (``account_id``) a saving target
    and return the months written. The plan's other months are left as they
    are.

    A STEP frequency (:data:`FREQUENCIES`) puts ``amount_cents`` into every
    month it lands on from ``first_period``. A DATED frequency
    (:data:`DATED_FREQUENCIES`) takes ``first_date`` and ``amount_cents`` PER
    OCCURRENCE, and writes each month the occurrences landing in it times that
    amount (:func:`dated_frequency_counts`) -- so a biweekly 100.00 from a
    payday lands as 200.00 in ten months and 300.00 in two, which is what the
    register will show and the reason the annual average was wrong.
    """
    targets = [t for t in (category_id, account_id, group_id) if t is not None]
    if len(targets) != 1:
        raise ValueError("pass exactly one of category_id, account_id and group_id")
    budget = get_budget(conn, budget_id)
    if budget is None or not budget.start_period:
        raise ValueError(f"budget {budget_id} has no start month")
    if frequency in DATED_FREQUENCIES:
        if not first_date:
            raise ValueError(f"{frequency} needs a first date")
        counts = dated_frequency_counts(budget.start_period, first_date, frequency)
        amounts = {p: int(amount_cents) * n for p, n in counts.items()}
    else:
        if not first_period:
            raise ValueError(f"{frequency} needs a first month")
        amounts = {p: int(amount_cents) for p in
                   frequency_periods(budget.start_period, first_period, frequency)}
    for period, cents in amounts.items():
        if category_id is not None:
            set_line(conn, budget_id, int(category_id), period, cents)
        elif group_id is not None:
            set_group_line(conn, budget_id, int(group_id), period, cents)
        else:
            set_saving_line(conn, budget_id, int(account_id), period, cents)
    return sorted(amounts)


def clear_line(conn: sqlite3.Connection, budget_id: int, category_id: int,
               period: str) -> None:
    """Remove one month's target for one category (an emptied cell)."""
    conn.execute("DELETE FROM budget_lines "
                 "WHERE budget_id = ? AND category_id = ? AND period = ?",
                 (budget_id, int(category_id), period))
    conn.commit()


def remove_category(conn: sqlite3.Connection, budget_id: int,
                    category_id: int) -> None:
    """Take a category out of a budget entirely: its lines, its settings and
    its carry overrides. The category itself, and its history, are untouched."""
    for table in ("budget_lines", "budget_category_settings",
                  "budget_carry_overrides"):
        conn.execute(f"DELETE FROM {table} "  # noqa: S608
                     f"WHERE budget_id = ? AND category_id = ?",
                     (budget_id, int(category_id)))
    conn.commit()


def remove_saving_account(conn: sqlite3.Connection, budget_id: int,
                          account_id: int) -> None:
    """Take a savings goal or debt pay-down account out of a budget."""
    conn.execute("DELETE FROM budget_saving_lines "
                 "WHERE budget_id = ? AND account_id = ?",
                 (budget_id, int(account_id)))
    conn.commit()


# ---- saving and debt pay-down (SRD 5.12d) ------------------------------------
# Money moved from a cash-flow account into a savings, investment, retirement or
# loan account is not spending, but it is planned like spending: "put 900.00 a
# month into the 401(k)". It is targeted per DESTINATION ACCOUNT, in its own
# table, and measured by mammon.reports.saving -- net, so a withdrawal lowers
# the month's saving. It has no bucket, rollover or carry: saving more one month
# did not overspend the next, and an envelope that carried a shortfall forward
# would be telling the user they owe their own 401(k).
@dataclass(frozen=True)
class SavingLine:
    """One saving target for a (destination account, period) within a budget."""
    budget_id: int
    account_id: int
    period: str            # ISO 'YYYY-MM'
    amount_cents: int      # net cents to move into the account that month


@dataclass
class SavingActualRow:
    """One destination account's planned-vs-saved comparison for a month.

    Every figure is net money INTO the account, positive = saved. It reads the
    opposite way from a spending row on purpose: ``remaining_cents`` is
    ``budgeted - actual``, so a positive remainder is saving still to do and a
    negative one is saving ahead of plan -- never an overspend.
    ``committed_cents`` is what an active schedule will move in this month that
    the register does not hold yet, and ``uncommitted_cents`` is what is still to
    do after it (``remaining - committed``)."""
    account_id: int
    account_name: str
    account_type: str
    budgeted_cents: int
    actual_cents: int
    remaining_cents: int
    committed_cents: int = 0
    uncommitted_cents: int = 0


@dataclass(frozen=True)
class SavingProposal:
    """One proposed monthly saving target, with the evidence behind it -- the
    saving counterpart of :class:`SeedProposal`, and just as much a READ."""
    account_id: int
    account_name: str
    basis: str                  # "mean" | "median" | "same_month"
    amount_cents: int           # the monthly target
    months_seen: int = 0
    spread_cents: int = 0
    volatile: bool = False
    sample_cents: tuple = ()    # the window's per-month net saving, oldest first


def set_saving_line(conn: sqlite3.Connection, budget_id: int, account_id: int,
                    period: str, amount_cents: int) -> None:
    """Upsert the saving target for one ``(budget, account, period)``."""
    _split_period(period)
    conn.execute(
        """
        INSERT INTO budget_saving_lines (budget_id, account_id, period, amount_cents)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(budget_id, account_id, period)
        DO UPDATE SET amount_cents = excluded.amount_cents
        """,
        (budget_id, int(account_id), period, int(amount_cents)),
    )
    conn.commit()


def get_saving_lines(conn: sqlite3.Connection, budget_id: int, *,
                     period: Optional[str] = None) -> list[SavingLine]:
    """A budget's saving targets, optionally for one month, by period then
    account."""
    sql = ("SELECT budget_id, account_id, period, amount_cents "
           "FROM budget_saving_lines WHERE budget_id = ?")
    params: list = [budget_id]
    if period is not None:
        sql += " AND period = ?"
        params.append(period)
    sql += " ORDER BY period, account_id"
    return [SavingLine(budget_id=r["budget_id"], account_id=r["account_id"],
                       period=r["period"], amount_cents=r["amount_cents"])
            for r in conn.execute(sql, params).fetchall()]


def delete_saving_line(conn: sqlite3.Connection, budget_id: int,
                       account_id: int, period: str) -> None:
    """Remove one month's saving target for one account."""
    conn.execute("DELETE FROM budget_saving_lines "
                 "WHERE budget_id = ? AND account_id = ? AND period = ?",
                 (budget_id, int(account_id), period))
    conn.commit()


def month_saving(conn: sqlite3.Connection, period: str, *,
                 include_scheduled: bool = False) -> dict[int, int]:
    """``destination account id -> net cents saved`` for one ``'YYYY-MM'``,
    straight from :func:`mammon.reports.saving.saving_by_account` (imported
    lazily, like the spending report, to stay clear of the package cycle)."""
    from mammon.reports.saving import saving_by_account

    start, end = period_bounds(period)
    return saving_by_account(conn, start, end,
                             include_scheduled=include_scheduled)


def month_extra_principal(conn: sqlite3.Connection, period: str,
                          account_ids: Iterable[int], *,
                          include_scheduled: bool = False) -> dict[int, int]:
    """``debt account id -> extra principal paid`` in one ``'YYYY-MM'``: what a
    pay-down line measures (SRD 5.12h). A pay-down line is a FIXED EXTRA
    PRINCIPAL payment on top of the regular one, which is budgeted where it is
    paid -- a mortgage as one line by payee -- so its principal leg is not the
    pay-down line's (:func:`mammon.loans.extra_principal_paid`)."""
    from mammon import loans

    start, end = period_bounds(period)
    return {int(a): loans.extra_principal_paid(conn, int(a), start, end,
                                               include_scheduled=include_scheduled)
            for a in account_ids}


def _occurrence_saving(conn: sqlite3.Connection, defn: dict,
                       cash_flow: dict[int, bool]) -> dict[int, int]:
    """``destination account id -> net cents`` ONE occurrence of ``defn`` saves.

    The definition's own transfer when it is a plain transfer, else the transfer
    legs of its split template -- a paycheck definition saves through its 401(k)
    leg even though the paycheck itself is money in. Only a definition on a
    cash-flow account can save, and only into an account that is not one."""
    from mammon import scheduled as _scheduled

    out: dict[int, int] = {}
    if not cash_flow.get(int(defn["account_id"]), False):
        return out
    dest = defn.get("transfer_account_id")
    if dest is not None:
        if not cash_flow.get(int(dest), False):
            out[int(dest)] = -int(defn["amount"])
        return out
    for s in _scheduled.get_scheduled_splits(conn, int(defn["id"])):
        dest = s["transfer_account_id"]
        if dest is not None and not cash_flow.get(int(dest), False):
            out[int(dest)] = out.get(int(dest), 0) - int(s["amount"])
    return out


def month_saving_committed(conn: sqlite3.Connection, period: str, *,
                           entered: Optional[dict[int, int]] = None
                           ) -> dict[int, int]:
    """``destination account id -> saving a schedule has promised this month and
    the register does not hold yet``.

    The same two halves, and the same consume-once matching, as
    :func:`month_committed`: pending pre-entries (saving WITH scheduled rows minus
    WITHOUT) plus each active definition's occurrences in the month that no row
    on the same account for the same signed amount stands for. Unlike spending,
    the candidate rows include transfers and a definition may be money IN -- a
    paycheck is exactly the definition that carries a 401(k) leg -- so neither is
    filtered out here. Only positive commitments are reported: a scheduled
    withdrawal is not a promise to save."""
    from mammon import scheduled as _scheduled
    from mammon.reports.saving import _cash_flow_types

    start, end = period_bounds(period)
    if entered is None:
        entered = month_saving(conn, period)
    with_pending = month_saving(conn, period, include_scheduled=True)
    committed: dict[int, int] = {}
    for acct, cents in with_pending.items():
        delta = cents - entered.get(acct, 0)
        if delta > 0:
            committed[acct] = delta

    cash_flow = _cash_flow_types(conn)
    defs = []
    for d in _scheduled.list_scheduled(conn, active_only=True):
        per = _occurrence_saving(conn, d, cash_flow)
        if any(c > 0 for c in per.values()):
            defs.append((d, per))
    if not defs:
        return committed

    window = _scheduled.PLACEHOLDER_MATCH_WINDOW_DAYS
    rows = conn.execute(
        "SELECT account_id, date, amount FROM transactions "
        "WHERE date >= ? AND date <= ? ORDER BY date, id",
        (_shift_days(start, -window), _shift_days(end, window))).fetchall()
    by_key: dict[tuple[int, int], list[str]] = {}
    for r in rows:
        by_key.setdefault((int(r["account_id"]), int(r["amount"])), []).append(r["date"])

    for defn, per in defs:
        key = (int(defn["account_id"]), int(defn["amount"]))
        for when in _scheduled.occurrences(defn["next_date"], defn["frequency"],
                                           start, end):
            if _claim_row(by_key.get(key), when, window):
                continue                    # a real row already stands for it
            for acct, cents in per.items():
                if cents > 0:
                    committed[acct] = committed.get(acct, 0) + cents
    return committed


def _account_meta(conn: sqlite3.Connection) -> dict[int, tuple[str, str, bool]]:
    """``account id -> (name, type, closed)`` over every account."""
    from mammon import ledger  # lazy: ledger imports nothing from here

    return {int(a["id"]): (a["name"], a["type"] or "", bool(a["closed_flag"]))
            for a in ledger.list_accounts(conn, include_closed=True,
                                          include_hidden=True)}


def saving_vs_actual(conn: sqlite3.Connection, budget_id: int, period: str, *,
                     include_unbudgeted: bool = True) -> list[SavingActualRow]:
    """Each destination account's saving target against what was saved in
    ``period``, ordered by account name.

    With ``include_unbudgeted`` (the default) an account that received saving
    but has no target this month still appears, with ``budgeted_cents == 0`` --
    the saving counterpart of spending that escaped the plan."""
    actual = month_saving(conn, period)
    committed = month_saving_committed(conn, period, entered=actual)
    lines = {ln.account_id: ln.amount_cents
             for ln in get_saving_lines(conn, budget_id, period=period)}
    ids = set(lines)
    if include_unbudgeted:
        ids |= {a for a, cents in actual.items() if cents}
        ids |= {a for a, cents in committed.items() if cents}
    meta = _account_meta(conn)
    rows = []
    for acct in ids:
        name, kind, _closed = meta.get(acct, ("", "", False))
        budgeted = lines.get(acct, 0)
        got = actual.get(acct, 0)
        remaining = budgeted - got
        promised = committed.get(acct, 0)
        rows.append(SavingActualRow(
            account_id=acct, account_name=name, account_type=kind,
            budgeted_cents=budgeted, actual_cents=got,
            remaining_cents=remaining, committed_cents=promised,
            uncommitted_cents=remaining - promised))
    rows.sort(key=lambda r: (r.account_name.lower(), r.account_id))
    return rows


def seed_saving_from_history(conn: sqlite3.Connection, *, months: int = 12,
                             end_period: Optional[str] = None,
                             basis: str = "mean",
                             today: Optional[_dt.date] = None
                             ) -> list[SavingProposal]:
    """Propose a monthly saving target per destination account from the trailing
    ``months`` complete months. READS ONLY, like :func:`seed_from_history`, over
    the same window and on the same ``basis``.

    Only accounts whose proposal comes out positive are offered: a net outflow
    is not something to aim for. A CLOSED destination is skipped too -- a loan
    paid off during the window had real pay-down, but it can take no more."""
    if basis not in SEED_BASES:
        raise ValueError(f"unknown basis {basis!r}; expected one of {SEED_BASES}")
    n_months = int(months)
    if n_months <= 0:
        return []
    if end_period is None:
        ref = today or _dt.date.today()
        end_period = shift_period(f"{ref.year:04d}-{ref.month:02d}", -1)
    else:
        _split_period(end_period)
    periods = period_sequence(shift_period(end_period, -(n_months - 1)), n_months)
    monthly = [month_saving(conn, p) for p in periods]
    meta = _account_meta(conn)

    proposals: list[SavingProposal] = []
    for acct in sorted({a for m in monthly for a in m}):
        name, _kind, closed = meta.get(acct, ("", "", False))
        if closed:
            continue
        samples = tuple(m.get(acct, 0) for m in monthly)
        mean = mean_cents(sum(samples), n_months)
        if basis == "median":
            amount = trimmed_median(samples)
        elif basis == "same_month":
            amount = _same_month_sample(periods, samples, end_period, mean)
        else:
            amount = mean
        if amount <= 0:
            continue
        spread = max(samples) - min(samples)
        proposals.append(SavingProposal(
            account_id=acct, account_name=name, basis=basis, amount_cents=amount,
            months_seen=sum(1 for s in samples if s), spread_cents=spread,
            volatile=spread > mean, sample_cents=samples))
    proposals.sort(key=lambda p: (p.account_name.lower(), p.account_id))
    return proposals


def apply_saving_proposals(conn: sqlite3.Connection, budget_id: int,
                           proposals: Iterable[SavingProposal], *,
                           start_period: str, months: int = 12,
                           round_to_dollar: bool = False) -> int:
    """Write accepted saving proposals as monthly targets; return the count.
    The same figure lands in every month of the span, rounded per line when
    asked, exactly as :func:`apply_proposals` treats a flex category."""
    _split_period(start_period)
    periods = period_sequence(start_period, months)
    written = 0
    for p in proposals:
        cents = _round_to_dollar(p.amount_cents) if round_to_dollar else p.amount_cents
        for period in periods:
            set_saving_line(conn, budget_id, p.account_id, period, cents)
            written += 1
    return written


# ---- category maintenance (called from ledger's category operations) --------
def merge_category_state(conn: sqlite3.Connection, from_id: int,
                         to_id: int) -> None:
    """Fold the losing category's budget state into the winner's. Does NOT commit.

    Called from :func:`mammon.ledger._merge_budget_lines`, which is the one place
    ledger code reaches into budget tables; keeping the SQL here keeps this module
    the only writer of them. The caller owns the transaction, so this function
    deliberately leaves the commit to it -- a merge that committed halfway would
    leave a category deleted and its envelope still pointing at it.

    Settings: the TARGET's ``bucket`` and ``rollover_mode`` win, because the
    surviving category is the one the user keeps working with, while
    ``annual_cents`` ADDS -- two half-year insurance envelopes merged are one
    full-year envelope. Overrides add for the same period, for the same reason.
    Group membership: the target keeps its own group when it has one, else it
    takes the loser's, so a pot never silently loses the money that was
    charged to it.
    """
    for r in conn.execute(
        "SELECT budget_id, bucket, rollover_mode, annual_cents, group_id "
        "FROM budget_category_settings WHERE category_id = ?", (from_id,)
    ).fetchall():
        tgt = conn.execute(
            "SELECT bucket, rollover_mode, annual_cents, group_id "
            "FROM budget_category_settings WHERE budget_id = ? AND category_id = ?",
            (r["budget_id"], to_id)).fetchone()
        if tgt is None:
            bucket, mode = r["bucket"], r["rollover_mode"]
            annual, group = r["annual_cents"], r["group_id"]
        else:
            bucket, mode = tgt["bucket"], tgt["rollover_mode"]
            annual = tgt["annual_cents"] + r["annual_cents"]
            group = tgt["group_id"] if tgt["group_id"] is not None else r["group_id"]
        conn.execute(
            """
            INSERT INTO budget_category_settings
                (budget_id, category_id, bucket, rollover_mode, annual_cents, group_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(budget_id, category_id)
            DO UPDATE SET bucket        = excluded.bucket,
                          rollover_mode = excluded.rollover_mode,
                          annual_cents  = excluded.annual_cents,
                          group_id      = excluded.group_id
            """,
            (r["budget_id"], to_id, bucket, mode, annual, group))
    conn.execute("DELETE FROM budget_category_settings WHERE category_id = ?",
                 (from_id,))

    for r in conn.execute(
        "SELECT budget_id, period, amount_cents, note, set_at "
        "FROM budget_carry_overrides WHERE category_id = ?", (from_id,)
    ).fetchall():
        conn.execute(
            """
            INSERT INTO budget_carry_overrides
                (budget_id, category_id, period, amount_cents, note, set_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(budget_id, category_id, period)
            DO UPDATE SET amount_cents = amount_cents + excluded.amount_cents
            """,
            (r["budget_id"], to_id, r["period"], r["amount_cents"], r["note"],
             r["set_at"]))
    conn.execute("DELETE FROM budget_carry_overrides WHERE category_id = ?",
                 (from_id,))


def forget_category(conn: sqlite3.Connection, category_id: int) -> None:
    """Drop every trace of a category from the budget tables. Does NOT commit.

    Called from :func:`mammon.ledger.delete_category`. The schema's
    ``ON DELETE CASCADE`` would usually do this, but only on a connection whose
    ``foreign_keys`` pragma happens to be on; doing it explicitly makes "a deleted
    category leaves no budget row behind" true unconditionally, which is what the
    orphan test asserts.
    """
    conn.execute("DELETE FROM budget_category_settings WHERE category_id = ?",
                 (category_id,))
    conn.execute("DELETE FROM budget_carry_overrides WHERE category_id = ?",
                 (category_id,))


# ---- burn-down: the Financial Calendar's budget mode (SRD 5.10e, 5.12e) -----
#: Below this percentage of recent spending covered by budget lines, the
#: calendar flags the figure: a burn-down that only watches half the money is
#: reassuring for the wrong reason, and the user should know before trusting it.
COVERAGE_FLOOR = Decimal("60")

#: How many days back :func:`budget_coverage` looks. A quarter is long enough to
#: include a non-monthly category that fires once and short enough to reflect a
#: budget the user reworked recently.
COVERAGE_DAYS = 90


@dataclass(frozen=True)
class BurnDownExpense:
    """One expense the burn-down subtracts, kept at line resolution.

    A SPLIT contributes one of these per non-transfer leg, because each leg
    belongs to its own envelope -- which is also why ``amount_cents`` is the
    leg's magnitude and not the transaction's. ``source`` is ``entered`` (a row
    the register holds), ``scheduled`` (a pre-entry or an occurrence nothing
    stands for yet) or ``predicted`` (a learned recurrence).

    ``over_cents`` is how far past its allowance the expense leaves its
    CATEGORY: 0 while the envelope still has room, and positive for the expense
    that crosses the line AND every later expense in that category that month.
    That is the rule the calendar's mark follows -- one mark per offending
    expense, so a day with two blown categories carries two marks, each naming
    its own -- and computing it here rather than in the UI keeps the arithmetic
    in the domain layer where the cents live.
    """
    date: str
    payee: str
    amount_cents: int
    category_id: int
    category_name: str
    source: str
    over_cents: int = 0
    txn_id: Optional[int] = None

    @property
    def over(self) -> bool:
        """True when this expense is past its category's limit (it is marked)."""
        return self.over_cents > 0


@dataclass(frozen=True)
class BurnDownDay:
    """One calendar day of the burn-down.

    ``spent_cents``, ``committed_cents`` and ``predicted_cents`` are that day's
    outflow by source, positive magnitudes that never overlap: an entered row
    claims the scheduled occurrence behind it, and a scheduled occurrence
    suppresses the prediction that would duplicate it. ``remaining_cents`` is the
    allowance minus the cumulative outflow through this day and is ALLOWED TO GO
    NEGATIVE -- a negative tail is the point of the view, not an error.
    """
    date: str
    spent_cents: int
    committed_cents: int
    predicted_cents: int
    remaining_cents: int
    over_categories: tuple[str, ...] = ()
    expenses: tuple[BurnDownExpense, ...] = ()

    @property
    def outflow_cents(self) -> int:
        return self.spent_cents + self.committed_cents + self.predicted_cents


@dataclass(frozen=True)
class CategoryStatus:
    """One budget ITEM's month: what it was allowed and what is left of it.

    The burn-down's day figures are the household total; this is the same month
    read one envelope at a time, which is what a per-item picture needs. The
    numbers come from the same event pass, so an item's remaining can never
    disagree with the total it contributes to.

    ``spent_cents`` is entered rows only and ``committed_cents`` is scheduled
    occurrences plus (when the caller asked for them) predictions -- positive
    magnitudes that never overlap, exactly as on :class:`BurnDownDay`.
    ``remaining_cents`` is ALLOWED TO GO NEGATIVE: an envelope past its limit is
    the thing a reader is looking for, not an error to clamp away.
    """
    category_id: int
    category_name: str
    allowance_cents: int
    spent_cents: int = 0
    committed_cents: int = 0

    @property
    def charged_cents(self) -> int:
        """Everything this month puts against the envelope, by any source."""
        return self.spent_cents + self.committed_cents

    @property
    def remaining_cents(self) -> int:
        """What is left of the envelope; negative once it is overspent."""
        return self.allowance_cents - self.charged_cents

    @property
    def over_cents(self) -> int:
        """How far past its limit the envelope is, or 0 while it still fits."""
        return max(0, -self.remaining_cents)

    @property
    def over(self) -> bool:
        return self.remaining_cents < 0


@dataclass(frozen=True)
class BurnDown:
    """A month of budget burn-down: what the allowance was and how it drained.

    **Income is ignored on purpose.** The burn-down answers "is the plan holding"
    and a paycheck is not an answer to that, so this view can look healthy while
    the cash floor breaks. It therefore never replaces the projected-balance
    calendar; it is a second mode beside it, and the summary keeps showing the
    projected low so the two questions stay visible at once.

    ``allowance_cents`` is the sum over budgeted categories of
    ``budgeted + carried_in`` for the period, taken from the same lines and the
    same carry recursion the Track tab reads, so the two can never disagree --
    rollover headroom shows up in the burn-down for free. The model is PER
    CATEGORY underneath: ``days`` carries every expense with the envelope it hit,
    even though a day cell shows one household figure. ``per_category`` exposes
    that underneath directly, one :class:`CategoryStatus` per BUDGET ITEM -- the
    budget's whole item set, the same in every month (see
    :func:`budget_item_category_ids`) -- so a caller drawing a bar per envelope
    does not re-derive the arithmetic and no envelope disappears as the reader
    pages months.
    """
    period: str
    budget_id: int
    allowance_cents: int
    days: tuple[BurnDownDay, ...]
    coverage_pct: Decimal = Decimal("100.0")
    low_coverage: bool = False
    #: Every budget item -- a FIXED set in a FIXED order, identical in every month
    #: of the budget -- see :func:`month_category_status`.
    per_category: tuple[CategoryStatus, ...] = ()

    @property
    def spent_cents(self) -> int:
        return sum(d.spent_cents for d in self.days)

    @property
    def committed_cents(self) -> int:
        return sum(d.committed_cents for d in self.days)

    @property
    def predicted_cents(self) -> int:
        return sum(d.predicted_cents for d in self.days)

    @property
    def remaining_cents(self) -> int:
        """What is left at the end of the month (negative once overspent)."""
        return self.days[-1].remaining_cents if self.days else self.allowance_cents


def _expense_lines(conn: sqlite3.Connection, start: str, end: str, *,
                   account_ids: Optional[Iterable[int]] = None) -> list[dict]:
    """Expenses between ``start`` and ``end`` at LINE resolution, dated.

    The aggregate views ask :mod:`mammon.reports.spending` for a category total;
    the burn-down needs the same money with its date, payee and transaction id
    still attached, and that module exposes no per-line function. So this
    reproduces :func:`mammon.reports.spending._aggregate_own_spending`'s rules
    EXACTLY -- the same WHERE clause (no transfers, the scheduled flag kept so
    the caller can tell a pre-entry from a real row), the same split-leg
    attribution, the same negative-amounts-only sign rule -- and
    ``test_burn_down_lines_sum_to_the_month_actuals`` pins the two together. A
    looser extractor here (``reports._lines``, say, which keeps the legs of a
    split whose PARENT is a transfer) would make the calendar and the Track tab
    report different spending for the same month, which is worse than either
    rule being wrong.

    Returns dicts with ``date``, ``txn_id``, ``payee``, ``category_id``,
    ``cents`` (a positive magnitude) and ``scheduled`` (1 for a pre-entry),
    ordered by date then transaction id.
    """
    where = ["date >= ?", "date <= ?", "transfer_account_id IS NULL"]
    params: list = [start, end]
    if account_ids is not None:
        ids = [int(a) for a in account_ids]
        if not ids:
            return []
        where.append("account_id IN (%s)" % ",".join("?" for _ in ids))
        params.extend(ids)
    txns = conn.execute(
        "SELECT id, date, payee, amount, category_id, scheduled FROM transactions "
        "WHERE " + " AND ".join(where) + " ORDER BY date, id", params).fetchall()
    if not txns:
        return []

    splits: dict[int, list] = {}
    ids_all = [int(t["id"]) for t in txns]
    for i in range(0, len(ids_all), 400):      # keep clear of SQLite's variable cap
        chunk = ids_all[i:i + 400]
        for s in conn.execute(
                "SELECT transaction_id, category_id, amount, transfer_account_id "
                "FROM splits WHERE transaction_id IN (%s)"
                % ",".join("?" for _ in chunk), chunk).fetchall():
            splits.setdefault(int(s["transaction_id"]), []).append(s)

    out: list[dict] = []
    for t in txns:
        base = {"date": t["date"], "txn_id": int(t["id"]),
                "payee": t["payee"] or "", "scheduled": int(t["scheduled"] or 0)}
        lines = splits.get(int(t["id"]))
        if lines:
            if int(t["amount"]) > 0:
                # A paycheck's deduction legs: not spending at take-home, the
                # same rule _month_actuals applies (the two are pinned together).
                continue
            for s in lines:
                if s["transfer_account_id"] is not None or s["category_id"] is None:
                    continue
                if int(s["amount"]) < 0:
                    out.append(dict(base, category_id=int(s["category_id"]),
                                    cents=-int(s["amount"])))
        elif int(t["amount"]) < 0 and t["category_id"] is not None:
            out.append(dict(base, category_id=int(t["category_id"]),
                            cents=-int(t["amount"])))
    return out


def _committed_events(conn: sqlite3.Connection, start: str, end: str, *,
                      account_ids: Optional[Iterable[int]] = None) -> list[dict]:
    """Scheduled occurrences in the range that no register row stands for yet.

    The day-resolved form of part (2) of :func:`month_committed`: same active
    manual definitions, same :func:`mammon.scheduled.occurrences` expansion, same
    one-candidate-row-per-occurrence claim through :func:`_claim_row`, so the
    two agree category by category over a month (pinned by
    ``test_committed_events_match_month_committed``). It is a second function
    rather than a refactor of ``month_committed`` because that one's ``entered``
    short-cut and its aggregate contract are read by the Track tab, and a
    calendar mode is not a reason to disturb them.

    Part (1) of ``month_committed`` -- the pre-entries already sitting in the
    register -- is NOT reproduced here: those rows come back from
    :func:`_expense_lines` with ``scheduled = 1`` and their own dates, which is
    the same money by a shorter route.

    Returns dicts with ``date``, ``account_id``, ``payee``, ``amount`` (the
    definition's signed amount, for matching a prediction against) and ``legs``
    (``category_id -> positive cents``).
    """
    from mammon import scheduled as _scheduled

    acct_set = None if account_ids is None else {int(a) for a in account_ids}
    if acct_set is not None and not acct_set:
        return []
    window = _scheduled.PLACEHOLDER_MATCH_WINDOW_DAYS
    defs = _expense_definitions(conn, account_ids=acct_set)
    if not defs:
        return []

    rows = conn.execute(
        "SELECT id, account_id, date, amount FROM transactions "
        "WHERE date >= ? AND date <= ? AND transfer_account_id IS NULL "
        "ORDER BY date, id",
        (_shift_days(start, -window), _shift_days(end, window))).fetchall()
    by_key: dict[tuple[int, int], list[str]] = {}
    for r in rows:
        by_key.setdefault((int(r["account_id"]), int(r["amount"])), []).append(r["date"])

    out: list[dict] = []
    for defn, legs in defs:
        dates = _scheduled.occurrences(defn["next_date"], defn["frequency"], start, end)
        if not dates:
            continue
        key = (int(defn["account_id"]), int(defn["amount"]))
        for when in dates:
            if _claim_row(by_key.get(key), when, window):
                continue                    # a real row already stands for it
            out.append({"date": when, "account_id": int(defn["account_id"]),
                        "payee": defn.get("payee") or "", "amount": int(defn["amount"]),
                        "legs": dict(legs)})
    out.sort(key=lambda e: (e["date"], e["payee"].lower()))
    return out


def _predicted_events(conn: sqlite3.Connection, start: str, end: str, today: str, *,
                      account_ids: Optional[Iterable[int]] = None,
                      committed: Sequence[dict] = ()) -> list[dict]:
    """Learned recurrences due in the range, netted against everything real.

    Predictions start at ``today`` (the past is what actually happened, not what
    was expected) and drop out three ways, in the order the calendar already
    uses: a row the register holds (:func:`mammon.predictions.is_entered`), a
    scheduled occurrence on the same account within the entered window carrying
    the same payee key or the same amount, and anything whose category no line
    covers. A :class:`mammon.predictions.Prediction` knows its ``category_id``,
    which is why the burn-down can charge one to an envelope at all --
    ``projection.ProjectedEvent`` cannot.
    """
    from mammon import predictions as _pred
    from mammon import scheduled as _scheduled

    first = max(start, today)
    if first > end:
        return []
    ids = None if account_ids is None else [int(a) for a in account_ids]
    preds = [p for p in _pred.predict_recurring(conn, today, account_ids=ids)
             if int(p.amount) < 0 and p.category_id is not None]
    if not preds:
        return []
    entered = _pred.entered_dates(conn, sorted({p.account_id for p in preds}),
                                 first, end)
    marks = [(int(c["account_id"]), _dt.date.fromisoformat(c["date"]),
              _pred.payee_key(c["payee"]), int(c["amount"])) for c in committed]

    out: list[dict] = []
    for p in preds:
        for due in _scheduled.occurrences(p.next_date, p.frequency, first, end):
            if _pred.is_entered(entered, p.account_id, p.key, due):
                continue
            if _covered_by_scheduled(marks, p.account_id, due, p.key, int(p.amount)):
                continue
            out.append({"date": due, "payee": p.payee, "legs": {int(p.category_id): -int(p.amount)}})
    out.sort(key=lambda e: (e["date"], e["payee"].lower()))
    return out


def _covered_by_scheduled(marks: Sequence[tuple], account_id: int, due: str,
                          key: str, amount: int) -> bool:
    """Does a scheduled occurrence already stand for this prediction?

    The same test :mod:`mammon.projection` makes: same account, within the
    entered window, and either the same payee key or the same signed amount.
    """
    from mammon import predictions as _pred

    when = _dt.date.fromisoformat(due)
    for m_acct, m_date, m_key, m_amount in marks:
        if m_acct != int(account_id):
            continue
        if abs((m_date - when).days) > _pred.ENTERED_WINDOW_DAYS:
            continue
        if (m_key and m_key == key) or m_amount == amount:
            return True
    return False


def budget_for_period(conn: sqlite3.Connection, period: str) -> Optional[Budget]:
    """The active budget the calendar should burn down for ``period``.

    A household usually keeps one active budget, but it may keep a scenario or
    a rolled-forward successor beside it, so pick in order: an active budget
    whose defined span covers the month AND has a line in it, then one whose
    span covers the month, then any active budget with a line in it, then the
    first active budget. Returns ``None`` when nothing is active, which is how
    the UI knows to explain that there is no budget rather than draw an empty
    month.
    """
    best = None
    for b in list_budgets(conn, include_inactive=False):
        covers = ((b.start_period is None or b.start_period <= period)
                  and (b.end_period is None or b.end_period >= period))
        lines = bool(get_lines(conn, b.id, period=period))
        rank = (0 if (covers and lines) else 1 if covers else 2 if lines else 3)
        if best is None or rank < best[0]:
            best = (rank, b)
    return None if best is None else best[1]


def burn_down(conn: sqlite3.Connection, budget_id: int, period: str, *,
              include_predictions: bool = True,
              account_ids: Optional[Iterable[int]] = None,
              today: Optional[str] = None) -> BurnDown:
    """The month's budget drawn down day by day (SRD 5.10e).

    Starts the month at the allowance -- every budgeted category's target plus
    whatever its rollover carried in -- and subtracts expenses day by day:
    entered rows for the days that have happened, scheduled occurrences and
    (with ``include_predictions``) learned recurrences for the rest of the month.
    **Income is never added back**, so the figure answers "is the plan holding",
    not "will the account clear".

    Only budgeted categories participate. Spending outside the plan is not
    subtracted from an envelope it has no claim on; :func:`budget_coverage` is
    how the user learns how much of their money that leaves unwatched. A budget
    item with no line for THIS month is such a case for the day figures -- its
    spend drains nothing -- yet it still appears in ``per_category`` with a zero
    allowance, because the item set on screen is the budget's and must not change
    from month to month (:func:`budget_item_category_ids`).

    Nothing is double counted. A scheduled occurrence a register row already
    represents is claimed by that row (:func:`_committed_events`), and a
    prediction a scheduled occurrence already represents is dropped
    (:func:`_predicted_events`) -- entered beats scheduled beats predicted,
    the precedence the balance calendar uses.

    A transfer is not spending, here as everywhere: money moved to savings never
    reaches an envelope because the extraction excludes transfer rows and
    transfer legs, and the principal share of a debt payment is exactly such a
    leg -- so a split mortgage payment burns its interest and escrow and leaves
    its principal alone, while an unsplit one is a categoryless transfer and
    burns nothing. The exclusion is inherited rather than re-stated, which is
    why the burn-down and the Track tab cannot drift apart.

    ``account_ids`` narrows the SPEND side only -- entered rows, scheduled
    occurrences and predictions alike -- and defaults to every account. The
    ALLOWANCE is deliberately NOT narrowed with it: a budget is a household
    plan, so the month opens at the full budgeted figure however few accounts
    are being watched, and the per-category over-spend marks are judged against
    that same full figure. The calendar's budget mode passes the accounts its
    slots select (the user's ruling; it once forced every account), which means
    a narrowed burn-down answers "what have THESE accounts done to the plan" --
    a real question, but one whose ``remaining_cents`` is not money left over.
    A caller that narrows the accounts owes the user that sentence; the
    calendar prints it in its summary.
    """
    year, month = _split_period(period)
    start, end = month_bounds(year, month)
    today = today or _dt.date.today().isoformat()
    if account_ids is None:
        # The budget's own scope, unless the calendar narrowed the spend side.
        account_ids = budget_account_ids(conn, budget_id)

    allowance = month_allowance(conn, budget_id, period)
    allowance_cents = sum(allowance.values())
    # The ITEM SET is the budget's, not the month's: every category the budget
    # plans for in any period keeps its place every month (SRD 5.10e). The
    # ALLOWANCE stays this month's lines, so the plan total is unchanged; an item
    # with no line this month is charged against a zero allowance and reads as an
    # overrun instead of disappearing from the grid.
    items = set(budget_item_category_ids(conn, budget_id)) | set(allowance)
    names = {int(r["id"]): r["name"]
             for r in conn.execute("SELECT id, name FROM categories").fetchall()}
    # A group is one item under its key, and a member's expense is charged to
    # the pot: "Food" draws one bar, drained by groceries and dining alike.
    names.update({g.key: g.name for g in list_groups(conn, budget_id)})
    charge_to = {cid: group_key(gid)
                 for cid, gid in member_groups(conn, budget_id).items()}
    # A payee line's payments are charged WHOLE to its key, legs dropped, so the
    # calendar's bar drains by the same figure the page shows.
    claimed_txn: dict[int, int] = {}
    whole_events: list[tuple] = []
    for gid, found in payee_claims(conn, budget_id, period).items():
        for date, payee, cents, txn_id, _acct in found.payments:
            claimed_txn[txn_id] = group_key(gid)
            if group_key(gid) in items:
                whole_events.append((date, 0, "entered", payee,
                                     {group_key(gid): cents}, txn_id))

    def regroup(legs: dict) -> dict:
        out: dict[int, int] = {}
        for c, v in legs.items():
            key = charge_to.get(c, c)
            if key in items:
                out[key] = out.get(key, 0) + v
        return out

    # Entered rows and pre-entries, then unbacked occurrences, then predictions.
    events: list[tuple[str, int, str, str, dict, Optional[int]]] = []
    for ln in _expense_lines(conn, start, end, account_ids=account_ids):
        if ln["txn_id"] in claimed_txn:
            continue                        # charged whole, below
        legs = regroup({ln["category_id"]: ln["cents"]})
        if not legs:
            continue
        src = "scheduled" if ln["scheduled"] else "entered"
        rank = 1 if ln["scheduled"] else 0
        events.append((ln["date"], rank, src, ln["payee"], legs, ln["txn_id"]))
    events.extend(whole_events)
    committed = _committed_events(conn, start, end, account_ids=account_ids)
    for ev in committed:
        legs = regroup(ev["legs"])
        if legs:
            events.append((ev["date"], 1, "scheduled", ev["payee"], legs, None))
    if include_predictions:
        for ev in _predicted_events(conn, start, end, today,
                                    account_ids=account_ids, committed=committed):
            legs = regroup(ev["legs"])
            if legs:
                events.append((ev["date"], 2, "predicted", ev["payee"], legs, None))
    events.sort(key=lambda e: (e[0], e[1], e[3].lower()))

    # One pass in date order: each envelope's running total decides the marks.
    # The same pass splits each envelope's charges by source, which is all
    # per_category is: the marks and the per-item bars therefore cannot disagree.
    running: dict[int, int] = {}
    entered_by_cat: dict[int, int] = {}
    future_by_cat: dict[int, int] = {}
    by_day: dict[str, list[BurnDownExpense]] = {}
    for when, _rank, src, payee, legs, txn_id in events:
        for cid in sorted(legs):
            cents = legs[cid]
            bucket = entered_by_cat if src == "entered" else future_by_cat
            bucket[cid] = bucket.get(cid, 0) + cents
            if cid not in allowance:
                # A budget item with NO line this month has no envelope here to
                # drain: its spend is outside this month's plan, so it moves
                # neither a day cell nor the remaining line, and the month's
                # spent_cents keeps summing the plan's own categories. The item's
                # own bar still shows the money, as an overrun of a zero
                # allowance -- which is exactly what it is.
                continue
            running[cid] = running.get(cid, 0) + cents
            over = running[cid] - allowance[cid]
            by_day.setdefault(when, []).append(BurnDownExpense(
                date=when, payee=payee, amount_cents=cents, category_id=cid,
                category_name=names.get(cid, ""), source=src,
                over_cents=over if over > 0 else 0, txn_id=txn_id))

    days: list[BurnDownDay] = []
    remaining = allowance_cents
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        when = "%04d-%02d-%02d" % (year, month, day)
        todays = by_day.get(when, [])
        spent = sum(e.amount_cents for e in todays if e.source == "entered")
        commit = sum(e.amount_cents for e in todays if e.source == "scheduled")
        predicted = sum(e.amount_cents for e in todays if e.source == "predicted")
        remaining -= spent + commit + predicted
        over_names = []
        for e in todays:
            if e.over_cents > 0 and e.category_name not in over_names:
                over_names.append(e.category_name)
        days.append(BurnDownDay(
            date=when, spent_cents=spent, committed_cents=commit,
            predicted_cents=predicted, remaining_cents=remaining,
            over_categories=tuple(sorted(over_names)), expenses=tuple(todays)))

    per_category = _order_category_status(
        (CategoryStatus(category_id=cid, category_name=names.get(cid, ""),
                        allowance_cents=allowance.get(cid, 0),
                        spent_cents=entered_by_cat.get(cid, 0),
                        committed_cents=future_by_cat.get(cid, 0))
         for cid in items),
        _category_paths(conn))

    pct = budget_coverage(conn, budget_id, as_of=today)
    return BurnDown(period=period, budget_id=budget_id,
                    allowance_cents=allowance_cents, days=tuple(days),
                    coverage_pct=pct, low_coverage=pct < COVERAGE_FLOOR,
                    per_category=per_category)


def _order_category_status(items: Iterable[CategoryStatus],
                           paths: dict[int, str]) -> tuple[CategoryStatus, ...]:
    """Budget items in a FIXED place that no month's spending can move.

    BY DISPLAY PATH, case-insensitively, then ``category_id`` to break a tie --
    the ordering the Budget Planner's Set tab already uses for its category rows
    (the first Budget Planner's Set tab, since replaced) and the one
    :func:`mammon.ledger.list_categories` hands the register's category picker.
    Matching it means the bars, the Set tab and every picker present the plan in
    one order, so an envelope is in the same relative position wherever it is
    read. A path sorts a child directly under its parent ("Auto & Transport"
    then "Auto & Transport:Fuel") because the parent's path is a prefix of it.

    This REPLACED a worst-first key (ascending ``remaining_cents``): that order
    depended on the month's money, so a category jumped columns as soon as its
    spending changed and the user could not watch one envelope across months --
    the reported defect. A caller that genuinely wants trouble first sorts a
    local copy; the canonical order stays month-independent. MEMBERSHIP is
    month-independent too -- the caller hands in the budget's whole item set
    (:func:`budget_item_category_ids`), so the same cells appear in the same
    sequence in every month of the budget.
    """
    return tuple(sorted(items, key=lambda s: (
        paths.get(s.category_id, s.category_name).lower(), s.category_id)))


def month_category_status(conn: sqlite3.Connection, budget_id: int, period: str, *,
                          include_predictions: bool = True,
                          account_ids: Optional[Iterable[int]] = None,
                          today: Optional[str] = None) -> tuple[CategoryStatus, ...]:
    """Each budget item's month: allowance, charges, and what is LEFT.

    The per-item reading of the same month :func:`burn_down` totals up, for a
    caller that wants one figure per envelope rather than one per day -- the
    calendar's budget mode draws a bar from each of these. It delegates rather
    than re-deriving, so the bars, the day cells and the month summary are three
    views of one arithmetic and cannot drift.

    ``account_ids`` narrows the SPEND side only and the allowance stays the whole
    household's, exactly as in :func:`burn_down`; a caller that narrows owes the
    user that sentence, because "left" then means "left after these accounts".

    The SET and the ORDER are both FIXED and month-independent. Every item of the
    budget appears in every month of it (:func:`budget_item_category_ids`),
    ordered by category display path -- the Set tab's and the category picker's
    order (see :func:`_order_category_status`) -- so an envelope keeps its place
    as the reader pages from month to month and never disappears on the way. It
    is NOT ordered by what is left. An item with no ``budget_lines`` row for this
    period comes back with ``allowance_cents`` 0 and its real charges, so it
    reads as an overrun rather than vanishing; no amount is invented and no other
    month's amount is carried in. The month's own plan total
    (:attr:`BurnDown.allowance_cents`) is unaffected by those zero items.
    """
    return burn_down(conn, budget_id, period,
                     include_predictions=include_predictions,
                     account_ids=account_ids, today=today).per_category


def _coverage_window(conn: sqlite3.Connection, budget_id: int,
                     as_of: Optional[str], days: int) -> tuple[int, int, list[tuple[str, int]]]:
    """``(covered_cents, total_cents, unbudgeted)`` over the trailing window.

    ``unbudgeted`` is ``(category name, cents)`` for the categories the budget
    has no line for, largest first -- what the low-coverage tooltip names.
    """
    from mammon.reports.spending import spending_by_category

    end = as_of or _dt.date.today().isoformat()
    start = _shift_days(end, -(int(days) - 1))
    report = spending_by_category(conn, start, end,
                                  account_ids=budget_account_ids(conn, budget_id),
                                  take_home=True)
    excluded = excluded_categories(conn, budget_id)
    budgeted = {ln.category_id for ln in get_lines(conn, budget_id)}
    # A member is covered when its GROUP has a line: the pot is its envelope.
    with_lines = {gl.group_id for gl in get_group_lines(conn, budget_id)}
    budgeted |= {cid for cid, gid in member_groups(conn, budget_id).items()
                 if gid in with_lines}
    claimed: dict = {}
    for g in list_groups(conn, budget_id):
        if g.by_payee:
            for cid, cents in payee_payments(conn, start, end, g.payee_match).legs.items():
                claimed[cid] = claimed.get(cid, 0) + cents
    covered = total = 0
    gaps: list[tuple[str, int]] = []
    for row in report.flat():
        if row.category_id is None or row.own_cents <= 0:
            continue
        if row.category_id in excluded:
            continue                      # not the budget's money at all
        total += row.own_cents
        taken = min(row.own_cents, claimed.get(row.category_id, 0))
        covered += taken                  # a payee line's payment is budgeted
        rest = row.own_cents - taken
        if row.category_id in budgeted:
            covered += rest
        elif rest > 0:
            gaps.append((row.path or row.name, rest))
    gaps.sort(key=lambda g: (-g[1], g[0].lower()))
    return covered, total, gaps


def budget_coverage(conn: sqlite3.Connection, budget_id: int,
                    as_of: Optional[str] = None,
                    days: int = COVERAGE_DAYS) -> Decimal:
    """Percent of the last ``days`` of spending that falls in budgeted categories.

    The honesty check on the burn-down: an envelope set covering a third of the
    money produces a calendar that looks calm because most of the spending never
    touches it. Shown beside the mode toggle as "Covers 68% of recent spending",
    flagged below :data:`COVERAGE_FLOOR`.

    A Decimal to one place, rounded ROUND_HALF_UP like every other figure the
    user sees. A window with no spending at all is 100 percent: nothing escaped
    the plan, which is vacuously true and reads better than a zero that suggests
    the plan is broken.
    """
    covered, total, _gaps = _coverage_window(conn, budget_id, as_of, days)
    if total <= 0:
        return Decimal("100.0")
    pct = (Decimal(covered) * 100 / Decimal(total))
    return pct.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def coverage_gaps(conn: sqlite3.Connection, budget_id: int, *,
                  as_of: Optional[str] = None, days: int = COVERAGE_DAYS,
                  limit: int = 5) -> list[tuple[str, int]]:
    """The biggest unbudgeted categories in the coverage window, largest first.

    ``(category path, cents)``. Naming them is the difference between "your
    budget covers 41% of your spending" and knowing which envelope to add.
    """
    _covered, _total, gaps = _coverage_window(conn, budget_id, as_of, days)
    return gaps[:max(0, int(limit))]


# ---- the cash floor: the month that balances and still breaks (SRD 5.12f) ----
# A budget is a monthly abstraction and an overdraft is a daily fact. The gap
# between those two is where a household that planned correctly still pays a fee:
# income beats planned spending for the month, and the rent still clears before
# the paycheck lands. The burn-down (:func:`burn_down`) cannot see this by
# construction -- it ignores income on purpose -- so the floor is a second,
# income-aware reading of the SAME month, taken from the projection the balance
# calendar already computes rather than from anything new.

#: Default cash cushion, in cents. Zero: the app must not invent a comfort level
#: the household never chose, and zero still catches every real overdraft. The
#: user raises it through ``ui/prefs.cash_cushion_cents``; nothing is stored in
#: the database, because a cushion is a preference and not a fact about the
#: ledger.
DEFAULT_CUSHION_CENTS = 0


def month_allowance(conn: sqlite3.Connection, budget_id: int, period: str, *,
                    account_ids: Optional[Iterable[int]] = None) -> dict[int, int]:
    """``category_id -> the month's plan``: its target plus what rollover carried in.

    One definition of "what this month is allowed to spend", shared by the
    burn-down and the cash floor, so the two cannot disagree about the size of
    the plan they are each judging.

    Keyed by THIS MONTH's ``budget_lines`` rows only -- a category the user never
    budgeted for this period is absent, not zero -- because the callers sum these
    values into the month's plan total. Which items the budget DISPLAYS is a
    different question, answered by :func:`budget_item_category_ids`.

    A GROUP with a line this month is one item under its :func:`group_key`, at
    its own line plus any line a member still carries plus its carry; the members
    themselves are not keys here, because their spending is charged to the pot.
    """
    if account_ids is None:
        account_ids = budget_account_ids(conn, budget_id)
    income_cats = {cid for cid, st in get_settings(conn, budget_id).items()
                   if st.bucket == "income"}
    line_objs = [ln for ln in get_lines(conn, budget_id, period=period)
                 if ln.category_id not in income_cats]
    groups = {g.id: g for g in list_groups(conn, budget_id)}
    member_of = member_groups(conn, budget_id) if groups else {}
    glines = {gl.group_id: gl.amount_cents
              for gl in get_group_lines(conn, budget_id, period=period)}
    rollover_cats = {ln.category_id for ln in line_objs
                     if ln.rollover and ln.category_id not in member_of}
    rollover_cats |= {g.key for g in groups.values() if g.rollover_mode != "none"}
    carried = _carry_in(conn, budget_id, period, rollover_cats,
                        account_ids=account_ids)
    out: dict[int, int] = {}
    for ln in line_objs:
        gid = member_of.get(ln.category_id)
        if gid is None:
            out[ln.category_id] = ln.amount_cents + carried.get(ln.category_id, 0)
        else:
            glines[gid] = glines.get(gid, 0) + ln.amount_cents
    for gid, cents in glines.items():
        key = group_key(gid)
        out[key] = cents + carried.get(key, 0)
    return out


def budget_item_category_ids(conn: sqlite3.Connection,
                             budget_id: int) -> tuple[int, ...]:
    """The budget's ITEM SET: every category it plans for, in ANY period.

    The union of every category with a ``budget_lines`` row in any period of the
    budget and every category the user has configured in
    ``budget_category_settings``. Deliberately NOT per month, which is the whole
    point: a budget item holds one place all year, so the set the calendar's bar
    grid draws is the BUDGET's and not the month's. A category budgeted in
    January no longer vanishes from February just because no February line was
    ever written for it -- the reported defect; lines are one row per (budget,
    category, period) with no inheritance across months.

    This is a MEMBERSHIP question only, kept apart from :func:`month_allowance`
    on purpose. That function answers "what is this month allowed to spend", and
    the plan total and the cash floor sum its values; widening it would grow
    every month's total by the months a category is not budgeted for. A
    month-missing item therefore shows an allowance of 0 (see :func:`burn_down`)
    -- never an invented amount, and never January's amount carried into
    February.

    A GROUP is one item, under its :func:`group_key`; its members are not
    items of their own, because their spending is charged to the pot.
    """
    rows = conn.execute(
        "SELECT DISTINCT category_id FROM budget_lines WHERE budget_id = ? "
        "UNION "
        "SELECT category_id FROM budget_category_settings WHERE budget_id = ?",
        (budget_id, budget_id)).fetchall()
    settings = get_settings(conn, budget_id)
    member_of = member_groups(conn, budget_id)
    items = {int(r[0]) for r in rows} - set(member_of)
    items -= {cid for cid, st in settings.items() if st.bucket == "income"}
    items |= {g.key for g in list_groups(conn, budget_id)}
    return tuple(sorted(items))


def planned_spending_cents(conn: sqlite3.Connection, budget_id: int, period: str, *,
                           account_ids: Optional[Iterable[int]] = None) -> int:
    """The month's whole plan as one positive magnitude of cents."""
    return sum(month_allowance(conn, budget_id, period,
                               account_ids=account_ids).values())


def floor_account_ids(conn: sqlite3.Connection) -> list[int]:
    """The accounts a cash floor is about: OPEN, visible spending accounts.

    Deliberately not :func:`spending_account_ids`, which includes closed accounts
    because money spent out of one was still spending. A floor is a FORWARD
    question and a closed account's balance is not money that can pay the rent,
    so counting it would cushion the projection with cash that is not there.
    """
    from mammon import ledger  # lazy: ledger imports nothing from here

    return [a["id"] for a in ledger.list_accounts(conn, include_closed=False,
                                                  include_hidden=False)
            if (a["type"] or "") in SPENDING_ACCOUNT_TYPES]


@dataclass(frozen=True)
class FloorCheck:
    """What a projected balance does against a cushion over one span of days.

    ``low_cents``/``low_date`` are :func:`mammon.projection.project`'s ``low`` and
    ``low_date`` NARROWED to the span asked about. ``project`` seeds its low with
    the OPENING balance, dated the day before ``start``, so a span every day of
    which sits above its opening would otherwise report a date outside itself --
    and a flag on June whose date is in May is not a flag anybody can act on.
    """
    start: str
    end: str
    cushion_cents: int
    low_cents: int
    low_date: str
    breaks: bool

    @property
    def shortfall_cents(self) -> int:
        """How far below the cushion the low point goes; 0 when it does not."""
        return max(0, self.cushion_cents - self.low_cents)


def _floor_from_days(days: Sequence, start: str, end: str, cushion_cents: int, *,
                     opening: int = 0,
                     extra: Sequence[tuple[str, int]] = ()) -> FloorCheck:
    """The lowest end-of-day balance among ``days``, with ``extra`` applied.

    ``extra`` is ``(ISO date, signed cents)`` for money that is NOT in the ledger
    yet -- a contribution or an extra payment a plan is about to propose. Each
    entry shifts every day from its own date onward, which is what lets a
    proposal be calendar-tested before it is committed rather than recommended
    and regretted.
    """
    deltas = [(str(when), int(cents)) for when, cents in extra]
    low: Optional[int] = None
    low_date = end
    for day in days:
        shift = sum(cents for when, cents in deltas if when <= day.date)
        balance = day.balance + shift
        if low is None or balance < low:
            low, low_date = balance, day.date
    if low is None:                      # an empty span: nothing to stand on
        low = int(opening) + sum(cents for _when, cents in deltas)
        low_date = end
    return FloorCheck(start=start, end=end, cushion_cents=int(cushion_cents),
                      low_cents=low, low_date=low_date,
                      breaks=low < int(cushion_cents))


def check_floor(conn: sqlite3.Connection, start: str, end: str, *,
                cushion_cents: int = DEFAULT_CUSHION_CENTS,
                account_ids: Optional[Iterable[int]] = None,
                include_predictions: bool = True, today: Optional[str] = None,
                extra: Sequence[tuple[str, int]] = ()) -> FloorCheck:
    """Does the projected balance stay at or above ``cushion_cents``, and if not, when?

    The reusable seam. Called with no ``extra`` it answers "does this span break
    the floor"; called with ``extra`` it answers "would it break the floor if we
    also did THIS", which is how a proposed savings contribution or a proposed
    extra principal payment gets tested against the calendar before anything is
    written. Nothing here writes, and ``extra`` never reaches the database.

    ``account_ids`` defaults to :func:`floor_account_ids`. A floor is a household
    question, so it is summed over every spending account rather than narrowed to
    one: the rent leaves checking, but the card it was almost paid with is part of
    the same wallet.
    """
    from mammon import projection as _projection  # lazy: Qt-free, but heavy

    ids = ([int(a) for a in account_ids] if account_ids is not None
           else floor_account_ids(conn))
    proj = _projection.project(conn, ids, start, end,
                               include_predictions=include_predictions, today=today)
    return _floor_from_days(proj.days, proj.start, proj.end, cushion_cents,
                            opening=proj.opening, extra=extra)


@dataclass(frozen=True)
class MonthFloor:
    """One budgeted month judged twice: on paper, and day by day.

    ``planned_cents`` is the month's whole plan (:func:`planned_spending_cents`)
    and ``income_cents`` is every inflow the projection sees inside the month --
    entered for the days that have happened, scheduled and predicted for the rest
    -- so the pair is the "on paper" verdict the household would give. The floor
    figures are the daily one.
    """
    period: str
    budget_id: int
    planned_cents: int
    income_cents: int
    cushion_cents: int
    low_cents: int
    low_date: str
    breaks: bool

    @property
    def surplus_cents(self) -> int:
        """Income minus the plan: positive when the month balances."""
        return self.income_cents - self.planned_cents

    @property
    def balances(self) -> bool:
        """True when the month's income covers the month's plan."""
        return self.surplus_cents >= 0

    @property
    def shortfall_cents(self) -> int:
        """How far below the cushion the month dips; 0 when it does not."""
        return max(0, self.cushion_cents - self.low_cents)

    @property
    def flagged(self) -> bool:
        """THE flag: the month balances on paper and still breaks the floor.

        A month that does not balance is a plan error the Track tab already
        reports category by category; this flag is reserved for the one the
        arithmetic cannot show, where the amounts are right and the ORDER is
        wrong.
        """
        return self.balances and self.breaks


def month_floors(conn: sqlite3.Connection, budget_id: int, periods: Iterable[str], *,
                 cushion_cents: int = DEFAULT_CUSHION_CENTS,
                 account_ids: Optional[Iterable[int]] = None,
                 include_predictions: bool = True,
                 today: Optional[str] = None) -> dict[str, MonthFloor]:
    """A :class:`MonthFloor` for each ISO ``'YYYY-MM'`` in ``periods``, by period.

    ONE projection spans every month asked about and is then sliced by month.
    :func:`mammon.projection.project` walks the ledger, every schedule and every
    learned recurrence once per call, so a twelve-month outlook calling it twelve
    times would pay for that walk twelve times over for an identical answer.
    """
    months = sorted({str(p) for p in periods})
    if not months:
        return {}
    span_start, _ = month_bounds(*_split_period(months[0]))
    _, span_end = month_bounds(*_split_period(months[-1]))
    from mammon import projection as _projection  # lazy: Qt-free, but heavy

    ids = ([int(a) for a in account_ids] if account_ids is not None
           else floor_account_ids(conn))
    proj = _projection.project(conn, ids, span_start, span_end,
                               include_predictions=include_predictions, today=today)
    by_month: dict[str, list] = {}
    for day in proj.days:
        by_month.setdefault(day.date[:7], []).append(day)
    out: dict[str, MonthFloor] = {}
    for period in months:
        start, end = month_bounds(*_split_period(period))
        days = by_month.get(period, [])
        check = _floor_from_days(days, start, end, cushion_cents,
                                 opening=proj.opening)
        income = sum(e.amount for d in days for e in d.events if e.amount > 0)
        out[period] = MonthFloor(
            period=period, budget_id=int(budget_id),
            planned_cents=planned_spending_cents(conn, budget_id, period,
                                                 account_ids=account_ids),
            income_cents=income, cushion_cents=int(cushion_cents),
            low_cents=check.low_cents, low_date=check.low_date,
            breaks=check.breaks)
    return out


def month_floor(conn: sqlite3.Connection, budget_id: int, period: str, *,
                cushion_cents: int = DEFAULT_CUSHION_CENTS,
                account_ids: Optional[Iterable[int]] = None,
                include_predictions: bool = True,
                today: Optional[str] = None) -> MonthFloor:
    """One month's :class:`MonthFloor` (see :func:`month_floors`)."""
    return month_floors(conn, budget_id, [period], cushion_cents=cushion_cents,
                        account_ids=account_ids,
                        include_predictions=include_predictions,
                        today=today)[period]

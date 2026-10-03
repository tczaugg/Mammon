"""Savings goals: a named target, what is funded toward it, and what it takes.

Why this is its own module rather than more of :mod:`mammon.budgets`
--------------------------------------------------------------------
A budget answers "how much may I spend on groceries in March". A goal answers
"when do I have the down payment". Those are different questions over different
time bases: a budget line is per month and resets, a goal is cumulative and
spans years. Folding goals into ``budgets.py`` would have meant a second,
period-less code path threaded through every budget query, and the two would
have drifted. This module sits beside ``budgets`` and above ``ledger``, is
UI-free, and holds no Qt, no formatting and no prose the user reads.

Two funding modes, because households keep savings both ways
------------------------------------------------------------
**Account-backed** (``account_id`` set) is the common case: a whole account IS
the goal, so ``funded = account_balance(as_of) - baseline_cents``. The baseline
is the part of that balance the goal does not get to claim -- the thousand that
was already sitting there before the goal existed. A partial unique index in the
schema (``idx_savings_goals_account``) allows at most ONE live goal per account,
because two goals reading the same balance would each claim the same dollars and
the two progress readouts would sum to twice the money.

**Allocated** (``account_id`` NULL) is for a household that saves for three
things in one account, or saves inside an account the goal does not own. Here
funding is the sum of :func:`allocate` rows, each of which names an existing
transaction. Those rows ANNOTATE a transaction; they never create, edit or
delete one. That is deliberate and load-bearing: ``mammon.ledger`` stays the
only writer of transaction rows, literally, with no exception carved for goals.
A contribution is a positive allocation and a withdrawal a negative one, so a
raided goal reads as less funded rather than as untouched.

Signs
-----
``target_cents`` and ``funded_cents`` are POSITIVE MAGNITUDES on the way out, no
matter which side of the ledger the backing account sits on. A goal backed by a
liability-signed account (``credit``, ``liability``) stores a negative balance,
so the magnitude conversion happens here, in one place -- otherwise a perfectly
healthy goal renders as "-2,500 of 10,000 saved". Funding floors at zero: an
account below its own baseline is a goal with nothing in it, not a negative one.

Ceiling division, and why the schedule is not just repetition
--------------------------------------------------------------
``required_cents`` is ``ceil(remaining / months_left)``. Rounding down would
leave a goal short of its own target -- three months of 2,500.00 at 833.33 is
2,499.99, and the household arrives a penny under the number it was told. The
ceiling overshoots instead, and :func:`contribution_plan` gives back the actual
month-by-month schedule that sums to the target EXACTLY by letting the last
month absorb the difference: 833.34, 833.34, 833.32.

States, not advice
------------------
:class:`GoalProgress` reports one of three plain states -- ``ON_PACE``,
``BEHIND`` (with the monthly shortfall named) and ``NO_DEADLINE`` (whose readout
is the projected month) -- and stops there. It does not tell anyone to spend
less, and it does not rank goals against each other. The tool computes and
cites; the household decides.

Where a goal touches the budget
-------------------------------
:func:`apply_to_budget` writes the goal's monthly contribution as ONE ORDINARY
budget row per month, through ``budgets``, which remains the sole writer of
budget tables. There is no new kind of line. Which table depends on how the goal
was set up: a goal with a ``category_id`` writes a ``budget_lines`` row through
``budgets.set_line`` (the user named a category for it), and an account-backed
goal without one writes a ``budget_saving_lines`` row through
``budgets.set_saving_line`` (SRD 5.12d already targets money by destination
account, which is exactly what an account-backed goal is). The design document
names only the first; the second exists because the Set tab shipped the
account-keyed table first and a savings goal is the natural producer of one.

The *actual* against such a line comes from :func:`month_funding`, never from
the budget layer's spending actuals. Funding a goal is a TRANSFER, and every
spending aggregation in this codebase excludes transfers on purpose (see
``reports/_lines.py``). Reading goal funding out of spending actuals would have
required inverting that rule for one caller; instead the goal answers for its
own money.

Interaction with the register: none. Nothing here writes a transaction.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from mammon import budgets, ledger
# The driver's OWN IntegrityError, not sqlite3's. Under the SQLCipher build the
# two hierarchies are disjoint, so `except sqlite3.IntegrityError` silently stops
# catching and the account-collision below escapes as a driver error instead of
# the ValueError this module promises. See mammon/sqldriver.py.
from mammon.sqldriver import IntegrityError

__all__ = [
    "ON_PACE", "BEHIND", "NO_DEADLINE", "STATES",
    "Goal", "GoalProgress", "TxnCandidate",
    "create_goal", "update_goal", "archive_goal", "delete_goal",
    "get_goal", "list_goals", "goal_progress", "contribution_plan",
    "allocate", "unallocate", "allocations", "suggest_allocations",
    "month_funding", "apply_to_budget", "check_contribution",
]


# The three states a goal can be in. Plain labels: each is a fact about the
# arithmetic, and none of them is a recommendation.
ON_PACE = "on pace"
BEHIND = "behind"
NO_DEADLINE = "no deadline"
STATES = (ON_PACE, BEHIND, NO_DEADLINE)

# Account types whose stored balance is negative when the household owes money.
# Used only for the magnitude conversion; see the module docstring.
LIABILITY_TYPES = ("credit", "liability")

_GOAL_COLUMNS = (
    "id, name, target_cents, target_date, account_id, baseline_cents, "
    "baseline_date, budget_id, category_id, monthly_cents, priority, note, "
    "archived, created_at"
)


# --------------------------------------------------------------------------
# validation helpers


def _validate_date(date: str) -> str:
    """Return ``date`` unchanged if it is an ISO ``YYYY-MM-DD`` calendar date."""
    try:
        _dt.date.fromisoformat(str(date))
    except (TypeError, ValueError):
        raise ValueError(f"date must be ISO YYYY-MM-DD, got {date!r}") from None
    return str(date)


def _opt_date(date: Optional[str]) -> Optional[str]:
    return None if date is None or date == "" else _validate_date(date)


def _today(today: Optional[str] = None) -> str:
    return _validate_date(today) if today else _dt.date.today().isoformat()


def _period_of(date: str) -> str:
    """The ISO ``YYYY-MM`` period a calendar date falls in."""
    return _validate_date(date)[:7]


def _ceil_div(numerator: int, denominator: int) -> int:
    """Ceiling of ``numerator / denominator`` for non-negative integer cents."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    n = int(numerator)
    if n <= 0:
        return 0
    return -(-n // int(denominator))


def _months_between(from_period: str, to_period: str) -> int:
    """Whole months from one ISO period to another; negative if it is behind."""
    fy, fm = budgets._split_period(from_period)
    ty, tm = budgets._split_period(to_period)
    return (ty - fy) * 12 + (tm - fm)


def _account_sign(conn: sqlite3.Connection, account_id: int) -> int:
    """+1 for an account whose balance grows positive, -1 for a liability."""
    row = conn.execute(
        "SELECT type FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
    if row is None:
        raise KeyError(f"no account with id {account_id}")
    return -1 if (row[0] or "") in LIABILITY_TYPES else 1


# --------------------------------------------------------------------------
# rows


@dataclass(frozen=True)
class Goal:
    """One row of ``savings_goals``, as stored.

    ``target_cents`` is a positive magnitude. ``account_id`` set means the goal
    is account-backed; ``None`` means it is funded by allocations.
    """

    id: int
    name: str
    target_cents: int
    target_date: Optional[str]
    account_id: Optional[int]
    baseline_cents: int
    baseline_date: Optional[str]
    budget_id: Optional[int]
    category_id: Optional[int]
    monthly_cents: int
    priority: int
    note: Optional[str]
    archived: bool
    created_at: str

    @property
    def account_backed(self) -> bool:
        return self.account_id is not None


@dataclass(frozen=True)
class GoalProgress:
    """What a goal has, what it still needs, and whether the plan gets there.

    Every money field is a positive magnitude in cents. ``schedule`` is the
    month-by-month plan that sums to ``remaining_cents`` exactly; it is empty
    for a goal with no deadline or nothing left to fund.
    """

    goal_id: int
    name: str
    as_of: str
    period: str
    target_cents: int
    funded_cents: int
    remaining_cents: int
    monthly_cents: int
    months_left: Optional[int]
    required_cents: int
    schedule: tuple[int, ...]
    projected_month: Optional[str]
    state: str
    target_date: Optional[str] = None

    @property
    def shortfall_cents(self) -> int:
        """Cents per month the plan is short; zero unless the state is BEHIND."""
        return max(0, self.required_cents - self.monthly_cents)

    @property
    def complete(self) -> bool:
        return self.remaining_cents == 0

    @property
    def percent(self) -> float:
        """Funded share of the target, 0.0-1.0. Display only; never money."""
        if self.target_cents <= 0:
            return 1.0
        return min(1.0, self.funded_cents / self.target_cents)


@dataclass(frozen=True)
class TxnCandidate:
    """A transaction :func:`suggest_allocations` thinks belongs to a goal.

    ``allocated_cents`` is what this goal already claims from it, so a caller
    can show "already counted" rather than offering the same row twice.
    """

    txn_id: int
    date: str
    account_id: int
    account_name: str
    payee: Optional[str]
    amount_cents: int
    allocated_cents: int = 0


def _goal_from_row(row: Sequence[Any]) -> Goal:
    return Goal(
        id=int(row[0]),
        name=row[1],
        target_cents=int(row[2]),
        target_date=row[3],
        account_id=None if row[4] is None else int(row[4]),
        baseline_cents=int(row[5]),
        baseline_date=row[6],
        budget_id=None if row[7] is None else int(row[7]),
        category_id=None if row[8] is None else int(row[8]),
        monthly_cents=int(row[9]),
        priority=int(row[10]),
        note=row[11],
        archived=bool(row[12]),
        created_at=row[13],
    )


# --------------------------------------------------------------------------
# create / read / update / delete


def create_goal(conn: sqlite3.Connection, name: str, target_cents: int, *,
                target_date: Optional[str] = None,
                account_id: Optional[int] = None,
                baseline_cents: int = 0,
                baseline_date: Optional[str] = None,
                budget_id: Optional[int] = None,
                category_id: Optional[int] = None,
                monthly_cents: int = 0,
                priority: int = 0,
                note: Optional[str] = None,
                created_at: Optional[str] = None) -> int:
    """Create one savings goal and return its id.

    ``target_cents`` is a positive magnitude and must be greater than zero -- a
    goal of nothing is a data-entry slip, not a goal. Passing an ``account_id``
    already claimed by another live goal raises :class:`ValueError` rather than
    surfacing the index's ``IntegrityError``, because the collision is a
    meaningful thing to tell the user about.
    """
    label = (name or "").strip()
    if not label:
        raise ValueError("a goal needs a name")
    target = int(target_cents)
    if target <= 0:
        raise ValueError(f"target_cents must be positive, got {target_cents!r}")
    if int(monthly_cents) < 0:
        raise ValueError("monthly_cents may not be negative")
    target_date = _opt_date(target_date)
    baseline_date = _opt_date(baseline_date)
    created = _opt_date(created_at) or _dt.date.today().isoformat()
    if account_id is not None:
        _account_sign(conn, account_id)          # existence check
    try:
        cur = conn.execute(
            "INSERT INTO savings_goals (name, target_cents, target_date, "
            "account_id, baseline_cents, baseline_date, budget_id, "
            "category_id, monthly_cents, priority, note, archived, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (label, target, target_date,
             None if account_id is None else int(account_id),
             int(baseline_cents), baseline_date,
             None if budget_id is None else int(budget_id),
             None if category_id is None else int(category_id),
             int(monthly_cents), int(priority), note, created))
    except IntegrityError as exc:
        if account_id is not None and "savings_goals" in str(exc):
            raise ValueError(
                f"account {account_id} already backs a live savings goal; "
                "archive that goal or use allocations instead") from exc
        raise
    conn.commit()
    return int(cur.lastrowid)


_UPDATABLE = ("name", "target_cents", "target_date", "account_id",
              "baseline_cents", "baseline_date", "budget_id", "category_id",
              "monthly_cents", "priority", "note")


def update_goal(conn: sqlite3.Connection, goal_id: int, **fields: Any) -> None:
    """Update named columns of one goal. Unknown names raise.

    ``archived`` is not updatable here on purpose: use :func:`archive_goal`, so
    the one place that frees an account's claim is easy to find.
    """
    if not fields:
        return
    bad = [k for k in fields if k not in _UPDATABLE]
    if bad:
        raise ValueError(f"not updatable: {', '.join(sorted(bad))}")
    get_goal(conn, goal_id)                      # existence check
    values: list[Any] = []
    for key in fields:
        val = fields[key]
        if key == "name":
            val = (val or "").strip()
            if not val:
                raise ValueError("a goal needs a name")
        elif key == "target_cents":
            val = int(val)
            if val <= 0:
                raise ValueError("target_cents must be positive")
        elif key == "monthly_cents":
            val = int(val)
            if val < 0:
                raise ValueError("monthly_cents may not be negative")
        elif key in ("target_date", "baseline_date"):
            val = _opt_date(val)
        elif key in ("baseline_cents", "priority"):
            val = int(val)
        elif key in ("account_id", "budget_id", "category_id"):
            val = None if val is None else int(val)
            if key == "account_id" and val is not None:
                _account_sign(conn, val)
        values.append(val)
    assign = ", ".join(f"{k} = ?" for k in fields)
    try:
        conn.execute(f"UPDATE savings_goals SET {assign} WHERE id = ?",
                     (*values, int(goal_id)))
    except IntegrityError as exc:
        raise ValueError(
            "that account already backs a live savings goal") from exc
    conn.commit()


def archive_goal(conn: sqlite3.Connection, goal_id: int,
                 archived: bool = True) -> None:
    """Archive (or un-archive) a goal.

    Archiving frees the goal's account for a new goal, which is why the unique
    index is partial. Un-archiving can therefore fail if something else took
    the account meanwhile; that raises :class:`ValueError`.
    """
    get_goal(conn, goal_id)
    try:
        conn.execute("UPDATE savings_goals SET archived = ? WHERE id = ?",
                     (1 if archived else 0, int(goal_id)))
    except IntegrityError as exc:
        raise ValueError(
            "that account already backs a live savings goal") from exc
    conn.commit()


def delete_goal(conn: sqlite3.Connection, goal_id: int) -> None:
    """Delete a goal and its allocation rows. Transactions are untouched."""
    conn.execute("DELETE FROM savings_goal_allocations WHERE goal_id = ?",
                 (int(goal_id),))
    conn.execute("DELETE FROM savings_goals WHERE id = ?", (int(goal_id),))
    conn.commit()


def get_goal(conn: sqlite3.Connection, goal_id: int) -> Goal:
    """Return one goal. Raises :class:`KeyError` if it does not exist."""
    row = conn.execute(
        f"SELECT {_GOAL_COLUMNS} FROM savings_goals WHERE id = ?",
        (int(goal_id),)).fetchone()
    if row is None:
        raise KeyError(f"no savings goal with id {goal_id}")
    return _goal_from_row(row)


def list_goals(conn: sqlite3.Connection, *, include_archived: bool = False,
               budget_id: Optional[int] = None) -> list[Goal]:
    """Goals in display order: priority ascending, then name, then id.

    Lower ``priority`` sorts first, so a household that numbers its goals 1, 2,
    3 gets them in that order without anyone having to remember a convention.
    """
    where = [] if include_archived else ["archived = 0"]
    params: list[Any] = []
    if budget_id is not None:
        where.append("budget_id = ?")
        params.append(int(budget_id))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT {_GOAL_COLUMNS} FROM savings_goals{clause} "
        "ORDER BY priority, name, id", params).fetchall()
    return [_goal_from_row(r) for r in rows]


# --------------------------------------------------------------------------
# funding and progress


def _allocated_total(conn: sqlite3.Connection, goal_id: int,
                     as_of: Optional[str] = None) -> int:
    sql = ("SELECT COALESCE(SUM(a.amount_cents), 0) "
           "FROM savings_goal_allocations a "
           "JOIN transactions t ON t.id = a.txn_id WHERE a.goal_id = ?")
    params: list[Any] = [int(goal_id)]
    if as_of:
        sql += " AND t.date <= ?"
        params.append(_validate_date(as_of))
    return int(conn.execute(sql, params).fetchone()[0])


def funded_cents(conn: sqlite3.Connection, goal: Goal | int,
                 as_of: Optional[str] = None) -> int:
    """Positive cents funded toward a goal as of a date (default: today).

    Account-backed: the account's balance less the baseline, sign-corrected and
    floored at zero. Allocated: the sum of allocations on transactions dated on
    or before ``as_of``, likewise floored at zero.
    """
    g = goal if isinstance(goal, Goal) else get_goal(conn, goal)
    when = _today(as_of)
    if g.account_id is None:
        return max(0, _allocated_total(conn, g.id, when))
    sign = _account_sign(conn, g.account_id)
    balance = ledger.account_balance(conn, g.account_id, when)
    return max(0, sign * balance - int(g.baseline_cents))


def contribution_plan(remaining_cents: int, months: int) -> list[int]:
    """The month-by-month schedule that sums to ``remaining_cents`` exactly.

    Every month but the last is ``ceil(remaining / months)``; the last absorbs
    the difference. That is why 2,500.00 over three months is 833.34, 833.34,
    833.32 and never three times 833.33, which lands a penny short of the
    target the household was promised.
    """
    n = int(months)
    if n <= 0:
        raise ValueError("months must be positive")
    remaining = int(remaining_cents)
    if remaining <= 0:
        return [0] * n
    level = _ceil_div(remaining, n)
    plan = [level] * (n - 1)
    plan.append(remaining - level * (n - 1))
    return plan


def goal_progress(conn: sqlite3.Connection, goal_id: int,
                  as_of: Optional[str] = None) -> GoalProgress:
    """Where a goal stands, and whether its planned contribution gets there.

    ``months_left`` counts the current period THROUGH the target's month,
    inclusive: a deadline at the end of March is still a March you can fund in.
    It never goes below one, so an overdue goal reports what it would take to
    finish this month rather than dividing by zero.
    """
    g = get_goal(conn, goal_id)
    when = _today(as_of)
    period = _period_of(when)
    funded = funded_cents(conn, g, when)
    remaining = max(0, int(g.target_cents) - funded)
    monthly = int(g.monthly_cents)

    months_left: Optional[int] = None
    required = 0
    schedule: tuple[int, ...] = ()
    if g.target_date:
        months_left = max(1, _months_between(period, g.target_date[:7]) + 1)
        required = _ceil_div(remaining, months_left)
        schedule = tuple(contribution_plan(remaining, months_left))

    projected: Optional[str] = None
    if remaining == 0:
        projected = period
    elif monthly > 0:
        projected = budgets.shift_period(
            period, _ceil_div(remaining, monthly) - 1)

    if g.target_date is None:
        state = NO_DEADLINE
    elif monthly >= required:
        state = ON_PACE
    else:
        state = BEHIND

    return GoalProgress(
        goal_id=g.id, name=g.name, as_of=when, period=period,
        target_cents=int(g.target_cents), funded_cents=funded,
        remaining_cents=remaining, monthly_cents=monthly,
        months_left=months_left, required_cents=required, schedule=schedule,
        projected_month=projected, state=state, target_date=g.target_date)


def month_funding(conn: sqlite3.Connection, goal_id: int, period: str) -> int:
    """Cents funded toward a goal DURING one ISO ``YYYY-MM`` period.

    This is the goal's own answer, and the only correct one: funding a goal is
    a transfer, and every spending aggregation in this codebase excludes
    transfers by design. Account-backed goals take the difference between the
    balance at the end of the month and the balance the day before it started;
    allocated goals sum the allocations whose transaction falls in the month.
    """
    g = get_goal(conn, goal_id)
    year, month = budgets._split_period(period)
    start, end = budgets.month_bounds(year, month)
    if g.account_id is None:
        row = conn.execute(
            "SELECT COALESCE(SUM(a.amount_cents), 0) "
            "FROM savings_goal_allocations a "
            "JOIN transactions t ON t.id = a.txn_id "
            "WHERE a.goal_id = ? AND t.date >= ? AND t.date <= ?",
            (g.id, start, end)).fetchone()
        return int(row[0])
    sign = _account_sign(conn, g.account_id)
    before = (_dt.date.fromisoformat(start) - _dt.timedelta(days=1)).isoformat()
    opening = ledger.account_balance(conn, g.account_id, before)
    closing = ledger.account_balance(conn, g.account_id, end)
    return sign * (closing - opening)


# --------------------------------------------------------------------------
# allocations -- annotations on transactions, never writes of them


def allocate(conn: sqlite3.Connection, goal_id: int, txn_id: int,
             amount_cents: int) -> None:
    """Credit ``amount_cents`` of one existing transaction to a goal.

    This writes a row in ``savings_goal_allocations`` and NOTHING ELSE. The
    transaction is not created, edited, moved or re-categorized -- ``ledger``
    remains the only writer of transaction rows. A positive amount is a
    contribution and a negative one a withdrawal; zero removes the allocation,
    which is the same thing the user means by clearing the field.
    """
    g = get_goal(conn, goal_id)
    if g.account_id is not None:
        raise ValueError(
            f"goal {g.id} is account-backed; its funding is that account's "
            "balance, so allocations would count the same money twice")
    row = conn.execute("SELECT id FROM transactions WHERE id = ?",
                       (int(txn_id),)).fetchone()
    if row is None:
        raise KeyError(f"no transaction with id {txn_id}")
    cents = int(amount_cents)
    if cents == 0:
        unallocate(conn, goal_id, txn_id)
        return
    conn.execute(
        "INSERT INTO savings_goal_allocations (goal_id, txn_id, amount_cents) "
        "VALUES (?,?,?) ON CONFLICT(goal_id, txn_id) "
        "DO UPDATE SET amount_cents = excluded.amount_cents",
        (g.id, int(txn_id), cents))
    conn.commit()


def unallocate(conn: sqlite3.Connection, goal_id: int, txn_id: int) -> None:
    """Drop one allocation. The transaction itself is untouched."""
    conn.execute(
        "DELETE FROM savings_goal_allocations WHERE goal_id = ? AND txn_id = ?",
        (int(goal_id), int(txn_id)))
    conn.commit()


def allocations(conn: sqlite3.Connection, goal_id: int) -> list[TxnCandidate]:
    """Every transaction currently credited to a goal, newest date last."""
    rows = conn.execute(
        "SELECT t.id, t.date, t.account_id, a.name, t.payee, t.amount, "
        "       g.amount_cents "
        "FROM savings_goal_allocations g "
        "JOIN transactions t ON t.id = g.txn_id "
        "JOIN accounts a ON a.id = t.account_id "
        "WHERE g.goal_id = ? ORDER BY t.date, t.id", (int(goal_id),)
    ).fetchall()
    return [TxnCandidate(int(r[0]), r[1], int(r[2]), r[3], r[4], int(r[5]),
                         int(r[6])) for r in rows]


def suggest_allocations(conn: sqlite3.Connection, goal_id: int,
                        start: str, end: str,
                        limit: int = 200) -> list[TxnCandidate]:
    """Transactions in a window that look like money moved toward this goal.

    A candidate is the RECEIVING leg of a transfer whose other side is a
    cash-flow account -- the shape of "I moved money out of checking into
    something that is not spending". Legs already claimed by a different goal
    are left out, so two goals cannot be offered the same dollars; legs already
    claimed by THIS goal come back with ``allocated_cents`` set, so the caller
    can show them as counted rather than offering them twice.

    This suggests. It never writes: the caller passes what it wants to keep to
    :func:`allocate`.
    """
    g = get_goal(conn, goal_id)
    _validate_date(start)
    _validate_date(end)
    from mammon.reports import saving as _saving

    cash_ids = _saving.cash_flow_account_ids(conn)
    if not cash_ids:
        return []
    marks = ",".join("?" for _ in cash_ids)

    # Bind order follows the SQL text below exactly: the two LEFT JOINs first,
    # then the account scope, then the window, then the transfer-source set,
    # then the limit.
    params: list[Any] = [g.id, g.id]
    if g.account_id is None:
        scope = f"t.account_id NOT IN ({marks})"
        params.extend(cash_ids)
    else:
        scope = "t.account_id = ?"
        params.append(int(g.account_id))
    params.extend([start, end])
    params.extend(cash_ids)
    params.append(int(limit))

    rows = conn.execute(
        "SELECT t.id, t.date, t.account_id, a.name, t.payee, t.amount, "
        "       COALESCE(mine.amount_cents, 0) "
        "FROM transactions t "
        "JOIN accounts a ON a.id = t.account_id "
        "LEFT JOIN savings_goal_allocations mine "
        "       ON mine.txn_id = t.id AND mine.goal_id = ? "
        "LEFT JOIN savings_goal_allocations other "
        "       ON other.txn_id = t.id AND other.goal_id <> ? "
        f"WHERE {scope} AND t.date >= ? AND t.date <= ? AND t.amount > 0 "
        "  AND t.transfer_account_id IS NOT NULL "
        f"  AND t.transfer_account_id IN ({marks}) "
        "  AND other.txn_id IS NULL "
        "ORDER BY t.date, t.id LIMIT ?", params).fetchall()
    return [TxnCandidate(int(r[0]), r[1], int(r[2]), r[3], r[4], int(r[5]),
                         int(r[6])) for r in rows]


# --------------------------------------------------------------------------
# the budget, and the cash floor


def apply_to_budget(conn: sqlite3.Connection, goal_id: int,
                    periods: Iterable[str]) -> int:
    """Write the goal's monthly contribution into its budget, one row a month.

    Returns how many months were written. Writing goes through ``budgets``,
    which stays the sole writer of budget tables: a goal with a ``category_id``
    writes an ordinary ``budget_lines`` row, and an account-backed goal without
    one writes a ``budget_saving_lines`` row keyed by its destination account
    (SRD 5.12d). A goal with neither, or with no ``budget_id``, raises -- there
    is nowhere for the number to land, and silently doing nothing would read as
    a bug in the budget rather than a goal that was never wired up.
    """
    g = get_goal(conn, goal_id)
    if g.budget_id is None:
        raise ValueError(f"goal {g.id} is not attached to a budget")
    if g.category_id is None and g.account_id is None:
        raise ValueError(
            f"goal {g.id} names neither a category nor an account, so its "
            "contribution has nowhere to land in the budget")
    written = 0
    for period in periods:
        budgets._split_period(period)
        if g.category_id is not None:
            budgets.set_line(conn, g.budget_id, g.category_id, period,
                             int(g.monthly_cents))
        else:
            budgets.set_saving_line(conn, g.budget_id, int(g.account_id),
                                    period, int(g.monthly_cents))
        written += 1
    return written


def check_contribution(conn: sqlite3.Connection, goal_id: int, date: str,
                       amount_cents: int, *,
                       cushion_cents: int = budgets.DEFAULT_CUSHION_CENTS,
                       start: Optional[str] = None, end: Optional[str] = None,
                       include_predictions: bool = True,
                       today: Optional[str] = None) -> "budgets.FloorCheck":
    """Would moving ``amount_cents`` into this goal on ``date`` break the floor?

    Asked BEFORE anything is written, through the one cash-floor seam
    (:func:`budgets.check_floor`) rather than a second copy of the arithmetic --
    the projection, the cushion and the "which accounts count as cash" question
    all have exactly one answer in this codebase and this is not the place to
    give them a second.

    The account set is the CASH-FLOW accounts (checking, credit, cash) minus the
    goal's own backing account. That subtraction is the whole point: the default
    floor set includes ``savings``, so a checking-to-savings contribution nets
    to zero inside it and every contribution would look free. Against cash flow
    alone, money leaving for the goal is money that has left.

    Returns the :class:`budgets.FloorCheck` unchanged, so ``breaks``,
    ``low_cents``, ``low_date`` and ``shortfall_cents`` read the same here as
    everywhere else. It decides nothing; the caller shows the number.
    """
    g = get_goal(conn, goal_id)
    _validate_date(date)
    from mammon.reports import saving as _saving

    ids = [i for i in _saving.cash_flow_account_ids(conn)
           if g.account_id is None or i != int(g.account_id)]
    if start is None or end is None:
        year, month = budgets._split_period(date[:7])
        bounds = budgets.month_bounds(year, month)
        start = start or bounds[0]
        end = end or bounds[1]
    return budgets.check_floor(
        conn, start, end, cushion_cents=int(cushion_cents), account_ids=ids,
        include_predictions=include_predictions, today=today,
        extra=((date, -abs(int(amount_cents))),))


def goal_for_account(conn: sqlite3.Connection, account_id: int, *,
                     budget_id: Optional[int] = None) -> Optional[Goal]:
    """The live goal backed by ``account_id``, preferring this budget's, or
    ``None``. A save-into line on the Budget page carries its goal this way
    (SRD 5.12g): the goal is a property of the line, found by the account the
    line saves into, so the page needs no goals table of its own."""
    candidates = [g for g in list_goals(conn) if g.account_id == int(account_id)]
    if not candidates:
        return None
    if budget_id is not None:
        for g in candidates:
            if g.budget_id == int(budget_id):
                return g
    return candidates[0]

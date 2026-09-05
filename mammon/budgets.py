"""Budgets: the UI-free domain layer for named per-category monthly targets,
now with rollover carry-over consumed.

A *budget* is a named envelope set the user can toggle ``active`` without
deleting; its *lines* pin one target ``amount_cents`` per ``(category, period)``
where ``period`` is an ISO ``'YYYY-MM'`` month. Keeping the target per-month
(rather than a single annual figure) lets a plan bend around known lumpy months
without inventing a row per day, and the ``UNIQUE(budget_id, category_id,
period)`` key makes :func:`set_line` a true upsert -- editing a target rewrites
the one row instead of accreting duplicates.

The load-bearing rule this module honours: **actuals are never stored.** A
budget only ever records intent; what was *actually* spent is derived read-only
from the ledger by reusing :func:`mammon.reports.spending.spending_by_category`,
so there is exactly one definition of "spending" (transfers excluded, splits
honoured, gross outflow as a positive magnitude) and no second write path into
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
so the app's ROUND_HALF_UP cents rule is honoured trivially); a ``rollover = 0``
line is unaffected and its ``remaining`` stays ``budgeted - actual``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

# NB: mammon.reports.spending is imported *lazily* inside budget_vs_actual, not
# here. Importing it at module load pulls in mammon.reports' package __init__,
# which eagerly imports mammon.reports.budget, which imports BudgetActualRow
# back from this module -- a circular import that explodes whenever mammon.budgets
# (or the Budgets UI) is the first thing imported in a process. Deferring the
# import to call time breaks the cycle: by then this module is fully defined.


@dataclass
class Budget:
    """A named, toggleable budget (envelope set)."""
    id: int
    name: str
    active: bool


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
    """
    category_id: int
    category_name: str
    budgeted_cents: int
    actual_cents: int
    remaining_cents: int
    carried_in_cents: int = 0


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


# ---- budgets (CRUD) ---------------------------------------------------------
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
    sql = "SELECT id, name, active FROM budgets"
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY name COLLATE NOCASE, id"
    return [
        Budget(id=r["id"], name=r["name"], active=bool(r["active"]))
        for r in conn.execute(sql).fetchall()
    ]


def get_budget(conn: sqlite3.Connection, budget_id: int) -> Optional[Budget]:
    """Return one budget by id, or ``None`` if it does not exist."""
    r = conn.execute(
        "SELECT id, name, active FROM budgets WHERE id = ?", (budget_id,)
    ).fetchone()
    if r is None:
        return None
    return Budget(id=r["id"], name=r["name"], active=bool(r["active"]))


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


def delete_budget(conn: sqlite3.Connection, budget_id: int) -> None:
    """Delete a budget and all of its lines.

    Lines are removed explicitly first so the delete is correct even on a
    connection whose ``foreign_keys`` pragma is off; the schema's
    ``ON DELETE CASCADE`` is a backstop, not the sole guarantee.
    """
    conn.execute("DELETE FROM budget_lines WHERE budget_id = ?", (budget_id,))
    conn.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))
    conn.commit()


# ---- budget lines -----------------------------------------------------------
def set_line(conn: sqlite3.Connection, budget_id: int, category_id: int,
             period: str, amount_cents: int, rollover: bool = False) -> int:
    """Upsert the target for one ``(budget, category, period)`` and return its id.

    Re-setting an existing line overwrites its ``amount_cents`` and ``rollover``
    rather than inserting a duplicate -- the ``UNIQUE(budget_id, category_id,
    period)`` constraint is the conflict target.
    """
    _split_period(period)  # validate format early
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
def _month_actuals(conn: sqlite3.Connection, period: str) -> dict[int, int]:
    """``category_id -> own (directly-booked) spending`` for one ``'YYYY-MM'``.

    A positive magnitude in cents, derived read-only from the ledger via
    :func:`mammon.reports.spending.spending_by_category`, so transfers are
    excluded and splits honoured exactly as in every other spending view. Using
    ``own_cents`` (not a subtree roll-up) keeps money from being counted twice
    when both a parent and its child carry a budget line. The ``spending``
    import is deferred to call time to break the package import cycle.
    """
    from mammon.reports.spending import period_range, spending_by_category

    year, month = _split_period(period)
    start, end = period_range("month", year, month=month)
    report = spending_by_category(conn, start, end)
    return {
        row.category_id: row.own_cents
        for row in report.flat()
        if row.category_id is not None
    }


def _carry_in(conn: sqlite3.Connection, budget_id: int, period: str,
              rollover_cats: set[int]) -> dict[int, int]:
    """Net remainder carried into ``period`` for each category in ``rollover_cats``.

    Only a category whose *current* line has ``rollover`` set carries anything;
    for such a category the carry is the sum, over every earlier period that also
    has a ``rollover`` line for it, of ``budgeted - actual`` (actual being that
    month's directly-booked spend, computed exactly as for the current period).
    A positive result is an unspent surplus; a negative result is a prior
    overspend that reduces this period's available amount.

    The scan runs over ISO ``'YYYY-MM'`` strings, which sort as text in calendar
    order, so ``period < ?`` means "an earlier month" and the sum crosses the
    December -> January boundary without a reset. All integer cents; the carry is
    pure addition, so no rounding is possible.
    """
    if not rollover_cats:
        return {}

    # Prior rollover lines for the wanted categories, grouped by their month.
    prior: dict[str, dict[int, int]] = {}
    for r in conn.execute(
        "SELECT category_id, period, amount_cents FROM budget_lines "
        "WHERE budget_id = ? AND rollover = 1 AND period < ? "
        "ORDER BY period",
        (budget_id, period),
    ).fetchall():
        cid = r["category_id"]
        if cid in rollover_cats:
            prior.setdefault(r["period"], {})[cid] = r["amount_cents"]

    carry: dict[int, int] = {cid: 0 for cid in rollover_cats}
    for prior_period, budgeted_by_cat in prior.items():
        actual_by_cat = _month_actuals(conn, prior_period)
        for cid, budgeted in budgeted_by_cat.items():
            carry[cid] += budgeted - actual_by_cat.get(cid, 0)
    return carry


def budget_vs_actual(conn: sqlite3.Connection, budget_id: int, period: str, *,
                     include_unbudgeted: bool = True) -> list[BudgetActualRow]:
    """Compare each budgeted category's target against actual spending in ``period``.

    ``period`` is an ISO ``'YYYY-MM'`` month. Actuals are derived read-only from
    the ledger via :func:`mammon.reports.spending.spending_by_category` over that
    whole month, so transfers are excluded and splits are honoured exactly as in
    every other spending view; a category's actual is the spending booked
    *directly* to it (``own_cents``), which keeps money from being counted twice
    when both a parent and its child carry a line.

    A line marked ``rollover`` carries the net remainder of its prior rollover
    periods into ``carried_in_cents`` (see :func:`_carry_in`), so
    ``remaining_cents = budgeted + carried_in - actual`` reflects the running
    envelope rather than the month in isolation. A non-rollover line carries 0.

    With ``include_unbudgeted`` (the default) a category that was spent but has
    no line for this period is still returned, with ``budgeted_cents == 0`` --
    surfacing spending that escaped the plan. Rows are ordered by category name.
    """
    actual_by_cat = _month_actuals(conn, period)

    line_objs = get_lines(conn, budget_id, period=period)
    lines = {ln.category_id: ln.amount_cents for ln in line_objs}
    rollover_cats = {ln.category_id for ln in line_objs if ln.rollover}
    carried = _carry_in(conn, budget_id, period, rollover_cats)

    if include_unbudgeted:
        cat_ids = set(lines) | {cid for cid, cents in actual_by_cat.items() if cents > 0}
    else:
        cat_ids = set(lines)

    names = {
        r["id"]: r["name"]
        for r in conn.execute("SELECT id, name FROM categories").fetchall()
    }

    rows: list[BudgetActualRow] = []
    for cid in cat_ids:
        budgeted = lines.get(cid, 0)
        actual = actual_by_cat.get(cid, 0)
        carried_in = carried.get(cid, 0)
        rows.append(BudgetActualRow(
            category_id=cid,
            category_name=names.get(cid, ""),
            budgeted_cents=budgeted,
            actual_cents=actual,
            remaining_cents=budgeted + carried_in - actual,
            carried_in_cents=carried_in,
        ))
    rows.sort(key=lambda r: (r.category_name.lower(), r.category_id))
    return rows

"""Rollover carry-over for budgets: the ``rollover`` flag is now *consumed*.

A ``rollover`` line carries its net remainder (``budgeted - actual``, summed over
prior rollover periods) into the next period's available amount, so an underspend
becomes extra headroom and an overspend eats into the following month. The carry
is compared chronologically by the ISO ``'YYYY-MM'`` period string, so it crosses
the December -> January boundary with no reset -- the exact year-boundary case
Quicken keeps getting wrong. A ``rollover = 0`` line is unaffected.

These tests pin: underspend carry-forward, overspend carry-forward, multi-period
accumulation across the year boundary, per-line gating (a non-rollover prior does
not contribute), that ``rollover = 0`` leaves ``remaining == budgeted - actual``,
and the range/YTD report roll-up. All money is signed integer cents. Synthetic
data only.
"""
from __future__ import annotations

import pytest

from mammon import budgets, db, ledger
from mammon.reports.budget import budget_vs_actual_range, budget_vs_actual_ytd
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "rollover.db")
    yield c
    c.close()


@pytest.fixture
def cats(conn):
    return {
        "groceries": ledger.resolve_category(conn, "Groceries"),
        "dining": ledger.resolve_category(conn, "Dining"),
    }


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Checking", "checking")


def _row(rows, category_id):
    for r in rows:
        if r.category_id == category_id:
            return r
    raise AssertionError(f"category {category_id} not present in {rows!r}")


# ---- single-line carry ------------------------------------------------------
def test_underspend_carries_forward(conn, cats, acct):
    """Jan under budget -> the surplus raises Feb's available amount."""
    g = cats["groceries"]
    budgets_id = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, budgets_id, g, "2026-01", 300_00, rollover=True)
    budgets.set_line(conn, budgets_id, g, "2026-02", 300_00, rollover=True)

    ledger.add_transaction(conn, acct, "2026-01-10", -250_00, category_id=g)  # under by 50
    ledger.add_transaction(conn, acct, "2026-02-10", -260_00, category_id=g)

    jan = _row(budgets.budget_vs_actual(conn, budgets_id, "2026-01"), g)
    assert jan.carried_in_cents == 0            # nothing precedes January
    assert jan.actual_cents == 250_00
    assert jan.remaining_cents == 50_00         # 300 - 250

    feb = _row(budgets.budget_vs_actual(conn, budgets_id, "2026-02"), g)
    assert feb.carried_in_cents == 50_00        # the January surplus
    assert feb.budgeted_cents == 300_00
    assert feb.actual_cents == 260_00
    assert feb.remaining_cents == 90_00         # 300 + 50 - 260


def test_overspend_carries_forward(conn, cats, acct):
    """Jan over budget -> the deficit lowers Feb's available amount."""
    g = cats["groceries"]
    budgets_id = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, budgets_id, g, "2026-01", 300_00, rollover=True)
    budgets.set_line(conn, budgets_id, g, "2026-02", 300_00, rollover=True)

    ledger.add_transaction(conn, acct, "2026-01-10", -350_00, category_id=g)  # over by 50
    ledger.add_transaction(conn, acct, "2026-02-10", -100_00, category_id=g)

    jan = _row(budgets.budget_vs_actual(conn, budgets_id, "2026-01"), g)
    assert jan.remaining_cents == -50_00        # overspent

    feb = _row(budgets.budget_vs_actual(conn, budgets_id, "2026-02"), g)
    assert feb.carried_in_cents == -50_00       # the January deficit
    assert feb.remaining_cents == 150_00        # 300 + (-50) - 100


def test_rollover_zero_is_unaffected(conn, cats, acct):
    """A line that does not roll over keeps ``remaining == budgeted - actual`` and
    never carries a prior remainder, even after a large underspend."""
    g = cats["groceries"]
    budgets_id = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, budgets_id, g, "2026-01", 300_00)                # rollover=False
    budgets.set_line(conn, budgets_id, g, "2026-02", 300_00)

    ledger.add_transaction(conn, acct, "2026-01-10", -250_00, category_id=g)  # 50 unspent
    ledger.add_transaction(conn, acct, "2026-02-10", -260_00, category_id=g)

    feb = _row(budgets.budget_vs_actual(conn, budgets_id, "2026-02"), g)
    assert feb.carried_in_cents == 0
    assert feb.remaining_cents == 40_00         # 300 - 260, no carry
    assert feb.remaining_cents == feb.budgeted_cents - feb.actual_cents


# ---- accumulation and boundaries -------------------------------------------
def test_carry_accumulates_across_year_boundary(conn, cats, acct):
    """Dec 2025 -> Jan 2026 -> Feb 2026 with no January reset; carry compounds."""
    g = cats["groceries"]
    budgets_id = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, budgets_id, g, "2025-12", 100_00, rollover=True)
    budgets.set_line(conn, budgets_id, g, "2026-01", 100_00, rollover=True)
    budgets.set_line(conn, budgets_id, g, "2026-02", 100_00, rollover=True)

    ledger.add_transaction(conn, acct, "2025-12-10", -60_00, category_id=g)   # +40 -> Jan
    ledger.add_transaction(conn, acct, "2026-01-10", -70_00, category_id=g)   # +30 net -> +70 -> Feb
    ledger.add_transaction(conn, acct, "2026-02-10", -50_00, category_id=g)

    jan = _row(budgets.budget_vs_actual(conn, budgets_id, "2026-01"), g)
    assert jan.carried_in_cents == 40_00        # Dec surplus crossed the year boundary
    assert jan.remaining_cents == 70_00         # 100 + 40 - 70

    feb = _row(budgets.budget_vs_actual(conn, budgets_id, "2026-02"), g)
    assert feb.carried_in_cents == 70_00        # Dec + Jan surpluses compounded
    assert feb.remaining_cents == 120_00        # 100 + 70 - 50


def test_non_rollover_prior_does_not_contribute(conn, cats, acct):
    """Per-line gating: a prior month whose line does NOT roll over adds nothing to
    a later rollover month's carry."""
    g = cats["groceries"]
    budgets_id = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, budgets_id, g, "2026-01", 300_00)                  # rollover=False
    budgets.set_line(conn, budgets_id, g, "2026-02", 300_00, rollover=True)

    ledger.add_transaction(conn, acct, "2026-01-10", -250_00, category_id=g)  # 50 unspent, but no roll
    ledger.add_transaction(conn, acct, "2026-02-10", -260_00, category_id=g)

    feb = _row(budgets.budget_vs_actual(conn, budgets_id, "2026-02"), g)
    assert feb.carried_in_cents == 0            # January opted out of rollover
    assert feb.remaining_cents == 40_00         # 300 + 0 - 260


def test_carry_is_per_category(conn, cats, acct):
    """One category's rollover must not bleed into another's carry."""
    g, d = cats["groceries"], cats["dining"]
    budgets_id = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, budgets_id, g, "2026-01", 300_00, rollover=True)
    budgets.set_line(conn, budgets_id, g, "2026-02", 300_00, rollover=True)
    budgets.set_line(conn, budgets_id, d, "2026-02", 80_00, rollover=True)

    ledger.add_transaction(conn, acct, "2026-01-10", -250_00, category_id=g)  # groceries +50
    # dining has no January line -> no opening balance in February

    feb_rows = budgets.budget_vs_actual(conn, budgets_id, "2026-02")
    assert _row(feb_rows, g).carried_in_cents == 50_00
    assert _row(feb_rows, d).carried_in_cents == 0


# ---- report roll-up ---------------------------------------------------------
def test_range_report_opening_balance_and_remaining(conn, cats, acct):
    """The range report's category total carries the opening balance (a remainder
    from before the span) once, and ends at the envelope's closing balance."""
    g = cats["groceries"]
    budgets_id = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, budgets_id, g, "2025-12", 100_00, rollover=True)   # before the span
    budgets.set_line(conn, budgets_id, g, "2026-01", 100_00, rollover=True)
    budgets.set_line(conn, budgets_id, g, "2026-02", 100_00, rollover=True)

    ledger.add_transaction(conn, acct, "2025-12-10", -60_00, category_id=g)   # +40 opening
    ledger.add_transaction(conn, acct, "2026-01-10", -70_00, category_id=g)
    ledger.add_transaction(conn, acct, "2026-02-10", -50_00, category_id=g)

    rep = budget_vs_actual_range(conn, budgets_id, "2026-01", "2026-02")
    cat = _row(rep.category_totals, g)
    assert cat.budgeted_cents == 200_00          # Jan + Feb
    assert cat.actual_cents == 120_00            # 70 + 50
    assert cat.carried_in_cents == 40_00         # opening balance from Dec 2025
    assert cat.remaining_cents == 120_00         # 40 + 200 - 120 == envelope end after Feb

    per = {t.period: t for t in rep.period_totals}
    assert per["2026-01"].carried_in_cents == 40_00
    assert per["2026-01"].remaining_cents == 70_00     # 100 + 40 - 70
    assert per["2026-02"].carried_in_cents == 70_00
    assert per["2026-02"].remaining_cents == 120_00    # 100 + 70 - 50

    assert rep.total_carried_in_cents == 40_00
    assert rep.total_remaining_cents == 120_00         # 40 + 200 - 120


def test_ytd_report_carries_prior_year_remainder(conn, cats, acct):
    """A year-to-date roll-up picks up December's remainder across the boundary."""
    g = cats["groceries"]
    budgets_id = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, budgets_id, g, "2025-12", 100_00, rollover=True)
    budgets.set_line(conn, budgets_id, g, "2026-01", 100_00, rollover=True)

    ledger.add_transaction(conn, acct, "2025-12-10", -60_00, category_id=g)   # +40 into 2026
    ledger.add_transaction(conn, acct, "2026-01-10", -90_00, category_id=g)

    ytd = budget_vs_actual_ytd(conn, budgets_id, 2026, through_month=1)
    cat = _row(ytd.category_totals, g)
    assert cat.carried_in_cents == 40_00
    assert cat.remaining_cents == 50_00          # 40 + 100 - 90

# The carried column's UI projection was asserted here against the old Budgets
# dialog. That dialog is gone (SRD 5.12); the Budget Planner's Track tab owes
# the equivalent coverage when it arrives.

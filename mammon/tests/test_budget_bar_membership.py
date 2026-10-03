"""Every budget item appears in EVERY month of the budget (SRD 5.10e, 5.12e).

Defect, after the fixed ordering landed: the order was consistent, but not
every item showed in every month; every item should. The cause was membership, not order: a
``budget_lines`` row is one row per (budget, category, period) with no
inheritance, so a category the user gave a January line and no February line was
simply absent from February's plan and its bar disappeared.

The fix makes the bar set the BUDGET's item set
(:func:`mammon.budgets.budget_item_category_ids`) -- the union over every period,
plus configured categories -- identical in every month. These tests pin the whole
life-cycle: the domain's set and sequence, the zero allowance a month-missing item
shows (with its REAL spend, so it reads as an overrun), the month plan total that
must NOT grow because of those zero items, and the bars on screen in both months.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from mammon import budgets, ledger
from mammon.tests import fresh_db
from mammon.ui import prefs

TODAY = "2026-03-20"                 # after both months: nothing is predicted
MONTH_A = "2026-01"                  # lines for all three items
MONTH_B = "2026-02"                  # a line for Groceries ONLY


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    """Never touch the developer's own QSettings: the panel reads and writes
    preferences (account slots, chart mode) as it is built."""
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    prefs.set_auto_enter_on_launch(False)
    yield
    prefs.set_auto_enter_on_launch(True)


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "bar_membership.db")
    yield c
    c.close()


@pytest.fixture
def lopsided_budget(conn):
    """One plan whose two months were budgeted UNEVENLY.

    January has a line for all three envelopes; February has one for Groceries
    alone -- yet February really does spend on Fuel and Dining, which is what
    makes their absence visible rather than harmless.
    """
    acct = ledger.create_account(conn, "Everyday", "checking",
                                 opening_balance=10_000_00)
    cat = {"Fuel": ledger.resolve_category(conn, "Auto & Transport:Fuel"),
           "Dining": ledger.resolve_category(conn, "Dining"),
           "Groceries": ledger.resolve_category(conn, "Groceries")}
    b = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, b, cat["Fuel"], MONTH_A, 100_00)
    budgets.set_line(conn, b, cat["Dining"], MONTH_A, 80_00)
    budgets.set_line(conn, b, cat["Groceries"], MONTH_A, 200_00)
    budgets.set_line(conn, b, cat["Groceries"], MONTH_B, 200_00)

    # January, inside its lines.
    ledger.add_transaction(conn, acct, "2026-01-07", -20_00, payee="Station",
                           category_id=cat["Fuel"])
    ledger.add_transaction(conn, acct, "2026-01-08", -60_00, payee="Market",
                           category_id=cat["Groceries"])
    # February: the two UNBUDGETED envelopes are spent anyway.
    ledger.add_transaction(conn, acct, "2026-02-05", -30_00, payee="Station",
                           category_id=cat["Fuel"])
    ledger.add_transaction(conn, acct, "2026-02-06", -45_00, payee="Cafe",
                           category_id=cat["Dining"])
    ledger.add_transaction(conn, acct, "2026-02-09", -50_00, payee="Market",
                           category_id=cat["Groceries"])
    return {"acct": acct, "budget": b, "cat": cat}


def _status(conn, budget_id, period, acct):
    return budgets.month_category_status(conn, budget_id, period,
                                         include_predictions=False,
                                         account_ids=[acct], today=TODAY)


def _burn(conn, budget_id, period, acct):
    return budgets.burn_down(conn, budget_id, period, include_predictions=False,
                             account_ids=[acct], today=TODAY)


def test_the_item_set_is_the_budgets_and_is_identical_in_both_months(
        conn, lopsided_budget):
    """All three envelopes come back for BOTH months, in the identical sequence,
    even though February has a line for only one of them."""
    acct, b, cat = (lopsided_budget["acct"], lopsided_budget["budget"],
                    lopsided_budget["cat"])
    a = _status(conn, b, MONTH_A, acct)
    bb = _status(conn, b, MONTH_B, acct)

    want_ids = tuple(sorted(cat.values()))
    assert budgets.budget_item_category_ids(conn, b) == want_ids
    assert set(s.category_id for s in a) == set(want_ids)
    assert set(s.category_id for s in bb) == set(want_ids)

    # Same set AND same sequence -- the display-path order, unchanged.
    assert [s.category_id for s in a] == [s.category_id for s in bb]
    assert [s.category_name for s in bb] == ["Fuel", "Dining", "Groceries"]

    # A category the user merely CONFIGURED, with no line in any period, is a
    # budget item too and joins the set in every month.
    tips = ledger.resolve_category(conn, "Zz Charity")
    budgets.set_settings(conn, b, tips, bucket="flex")
    assert tips in budgets.budget_item_category_ids(conn, b)
    for period in (MONTH_A, MONTH_B):
        assert tips in [s.category_id for s in _status(conn, b, period, acct)]


def test_a_month_missing_item_shows_a_zero_allowance_and_its_real_spend(
        conn, lopsided_budget):
    """No invented allowance and nothing carried over from January: February's
    Fuel and Dining are zero-allowance items whose own spending makes them read
    as overruns."""
    acct, b = lopsided_budget["acct"], lopsided_budget["budget"]
    by_name = {s.category_name: s for s in _status(conn, b, MONTH_B, acct)}

    fuel, dining = by_name["Fuel"], by_name["Dining"]
    assert fuel.allowance_cents == 0            # NOT January's 100_00
    assert dining.allowance_cents == 0          # NOT January's 80_00
    assert (fuel.spent_cents, fuel.committed_cents) == (30_00, 0)
    assert (dining.spent_cents, dining.committed_cents) == (45_00, 0)
    assert fuel.remaining_cents == -30_00 and fuel.over_cents == 30_00
    assert dining.remaining_cents == -45_00 and dining.over_cents == 45_00
    assert fuel.over and dining.over

    # The one real line is untouched by any of that.
    groceries = by_name["Groceries"]
    assert (groceries.allowance_cents, groceries.spent_cents) == (200_00, 50_00)
    assert groceries.remaining_cents == 150_00 and not groceries.over

    # January's own figures did not move either.
    jan = {s.category_name: s for s in _status(conn, b, MONTH_A, acct)}
    assert jan["Fuel"].allowance_cents == 100_00
    assert jan["Fuel"].spent_cents == 20_00


def test_the_months_plan_total_counts_only_that_months_lines(
        conn, lopsided_budget):
    """Widening the ITEM SET must not widen the PLAN. February's allowance is the
    single Groceries line, and the zero items add neither allowance nor drained
    cents to the day figures."""
    acct, b = lopsided_budget["acct"], lopsided_budget["budget"]

    feb = _burn(conn, b, MONTH_B, acct)
    assert feb.allowance_cents == 200_00                 # not 380_00
    assert budgets.planned_spending_cents(conn, b, MONTH_B,
                                          account_ids=[acct]) == 200_00
    assert set(budgets.month_allowance(conn, b, MONTH_B)) == \
        {lopsided_budget["cat"]["Groceries"]}

    # The day figures stay the plan's: only the Groceries charge drains February,
    # so remaining ends at the allowance less that charge.
    assert feb.spent_cents == 50_00
    assert feb.days[-1].remaining_cents == 150_00
    assert sum(s.allowance_cents for s in feb.per_category) == feb.allowance_cents

    jan = _burn(conn, b, MONTH_A, acct)
    assert jan.allowance_cents == 380_00
    assert jan.spent_cents == 80_00


def test_the_grid_draws_every_item_in_both_months_with_the_overrun_outside(
        qapp, conn, lopsided_budget):
    """End to end through the calendar in budget mode: three bars in each month,
    in one sequence, and February's zero-allowance Fuel paints its overrun in the
    gutter OUTSIDE an empty bar instead of vanishing."""
    from mammon.ui.budget_bars import BudgetBarGrid, BudgetItemBar
    from mammon.ui.projection_dialogs import CalendarPanel

    acct, b = lopsided_budget["acct"], lopsided_budget["budget"]
    seen = []
    for month in (1, 2):
        dlg = CalendarPanel(conn, year=2026, month=month, today=TODAY,
                            account_id=acct)
        dlg.include_predictions.setChecked(False)
        dlg.budget_mode.setChecked(True)
        grid = dlg.chart_widget
        assert isinstance(grid, BudgetBarGrid)

        bars = grid.bars()
        names = [bar.status.category_name for bar in bars]
        assert names == [bar.status.category_name
                         for bar in grid.widget().findChildren(BudgetItemBar)]
        assert names == [s.category_name for s in
                         _status(conn, b, "2026-%02d" % month, acct)]
        seen.append(names)

        if month == 2:
            by_name = {bar.status.category_name: bar for bar in bars}
            fuel = by_name["Fuel"]
            assert fuel.status.allowance_cents == 0
            g = fuel.bar_geometry(width=180)
            # Nothing inside the bar (no scale to draw against), red outside it.
            assert g.fill_width == 0.0
            assert g.over_width > 0 and g.over_x < g.bar_x
            assert g.over_clipped
            # A budgeted item that is INSIDE its line draws no overrun at all.
            assert by_name["Groceries"].bar_geometry(width=180).over_width == 0.0
        dlg.deleteLater()

    assert seen[0] == seen[1] == ["Fuel", "Dining", "Groceries"]

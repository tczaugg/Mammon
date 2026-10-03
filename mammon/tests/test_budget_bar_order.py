"""The calendar's budget bars keep each category in a FIXED place (SRD 5.10e).

Defect: the order of the budget item bars changed from month to month. Each
category belongs in a fixed location, so that scrolling month-to-month one can
watch a single budget item and see how it behaved. The cause was a worst-first canonical order in
:func:`mammon.budgets.burn_down` -- ascending ``remaining_cents`` -- which is a
function of the month's money, so a bar moved as soon as its spending changed.

These tests pin the replacement END TO END: the domain returns the same sequence
for two months whose spending would sort differently, that sequence is the
category display path order (the Budget Planner Set tab's and the category
picker's order), and the on-screen bars are in exactly that sequence.
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

TODAY = "2026-03-15"                     # after both months, so nothing predicts


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
    c = fresh_db(tmp_path / "bar_order.db")
    yield c
    c.close()


@pytest.fixture
def two_months(conn):
    """One plan, three envelopes, two months that a worst-first order would
    shuffle: January blows Groceries (last by path) and leaves Fuel nearly full,
    February blows Fuel (FIRST by path, as 'Auto & Transport:Fuel') and leaves
    Groceries untouched.
    """
    acct = ledger.create_account(conn, "Everyday", "checking",
                                 opening_balance=10_000_00)
    cat = {"Fuel": ledger.resolve_category(conn, "Auto & Transport:Fuel"),
           "Dining": ledger.resolve_category(conn, "Dining"),
           "Groceries": ledger.resolve_category(conn, "Groceries")}
    b = budgets.create_budget(conn, "Household")
    for period in ("2026-01", "2026-02"):
        for cid in cat.values():
            budgets.set_line(conn, b, cid, period, 100_00)

    # January: Groceries overspent, Fuel barely touched.
    ledger.add_transaction(conn, acct, "2026-01-05", -200_00, payee="Market",
                           category_id=cat["Groceries"])
    ledger.add_transaction(conn, acct, "2026-01-06", -10_00, payee="Station",
                           category_id=cat["Fuel"])
    # February: Fuel overspent, Groceries untouched.
    ledger.add_transaction(conn, acct, "2026-02-05", -250_00, payee="Station",
                           category_id=cat["Fuel"])
    ledger.add_transaction(conn, acct, "2026-02-06", -50_00, payee="Cafe",
                           category_id=cat["Dining"])
    return {"acct": acct, "budget": b, "cat": cat}


def _status(conn, budget_id, period, acct):
    return budgets.month_category_status(conn, budget_id, period,
                                         include_predictions=False,
                                         account_ids=[acct], today=TODAY)


def test_domain_order_is_identical_across_months_and_follows_the_category_path(
        conn, two_months):
    """The canonical per-category sequence is a function of the CATEGORY TREE,
    not of the month's money: two months with opposite trouble come back in the
    same order, the one `ledger.list_categories` (the picker and the Set tab)
    uses."""
    acct, b = two_months["acct"], two_months["budget"]
    jan = _status(conn, b, "2026-01", acct)
    feb = _status(conn, b, "2026-02", acct)

    # The months really do differ in their money, so a money-based order WOULD
    # have shuffled them -- this is the defect the fixed order removes.
    jan_worst = sorted(jan, key=lambda s: s.remaining_cents)
    feb_worst = sorted(feb, key=lambda s: s.remaining_cents)
    assert [s.category_name for s in jan_worst] != \
           [s.category_name for s in feb_worst]

    # The fixed key: category display path, case-insensitive, then id.
    paths = {c["id"]: c["path"] for c in ledger.list_categories(conn)}
    budgeted = [cid for cid in (s.category_id for s in jan)]
    want = [paths[cid] for cid in sorted(budgeted,
                                         key=lambda c: (paths[c].lower(), c))]
    assert want == ["Auto & Transport:Fuel", "Dining", "Groceries"]

    assert [s.category_id for s in jan] == [s.category_id for s in feb]
    assert [paths[s.category_id] for s in jan] == want
    assert [s.category_name for s in jan] == ["Fuel", "Dining", "Groceries"]

    # Same rows, same order, whichever entry point asked.
    assert budgets.burn_down(conn, b, "2026-02", include_predictions=False,
                             account_ids=[acct],
                             today=TODAY).per_category == feb


def test_the_drawn_bars_sit_in_the_same_order_in_both_months(
        qapp, conn, two_months):
    """On screen, not only in the domain: with budget mode on, the bar widgets of
    two different months are in one identical sequence, and it is the domain's --
    the grid does not re-sort what it was handed."""
    from mammon.ui.budget_bars import BudgetBarGrid, BudgetItemBar
    from mammon.ui.projection_dialogs import CalendarPanel

    acct = two_months["acct"]
    seen = []
    for month in (1, 2):
        dlg = CalendarPanel(conn, year=2026, month=month, today=TODAY,
                            account_id=acct)
        dlg.include_predictions.setChecked(False)
        dlg.budget_mode.setChecked(True)
        grid = dlg.chart_widget
        assert isinstance(grid, BudgetBarGrid)

        names = [bar.status.category_name for bar in grid.bars()]
        # The widget CHILDREN, in the order Qt holds them, say the same thing.
        children = [bar.status.category_name
                    for bar in grid.widget().findChildren(BudgetItemBar)]
        assert children == names

        want = [s.category_name for s in
                _status(conn, two_months["budget"], "2026-%02d" % month, acct)]
        assert names == want                      # no re-sorting in the view
        seen.append(names)
        dlg.deleteLater()

    assert seen[0] == seen[1] == ["Fuel", "Dining", "Groceries"]


def test_narrow_and_wide_grids_hold_one_sequence(qapp, conn, two_months):
    """The column count may follow the width; the sequence may not. A one-column
    grid and a four-column grid list the items in the same order."""
    from mammon.ui.budget_bars import BudgetBarGrid

    items = _status(conn, two_months["budget"], "2026-01", two_months["acct"])
    narrow = BudgetBarGrid(items, columns=1)
    wide = BudgetBarGrid(items, columns=4)
    assert [b.status.category_id for b in narrow.bars()] == \
           [b.status.category_id for b in wide.bars()] == \
           [s.category_id for s in items]

"""Regression: every report's customization popup offers the pickers it can
actually honor, and each picker really filters (SRD 5.9c).

The user's report was a matrix of holes: "the category customization is missing
for Itemize by Category, Income by Category, Cash Flow, Income vs Expense, and
Transactions. The account customization is missing for Account Balances." The
case behind it is a restriction with its subtree intact -- "restrict the
categories to Church and expand to see the subcategories and their spending" --
and a two-sided one, "Landlord spending and Rent income, for a complete summary".

Two halves are pinned here, because a picker can fail either way:

* the control EXISTS and is really shown in the gear popup (a spec-level
  ``show_categories=False`` or ``show_accounts=False`` silently removes it), and
* the selection changes the money, matching the pure reports-layer function
  called directly with the same restriction. A visible check-list that filters
  nothing is the bug, not the fix, so every narrowing test asserts against the
  aggregation rather than against itself.

Itemize's own pair lives in ``test_itemize_report.py``; this module covers the
other four report-window reports plus the Income by Category pie, which is not a
ReportSpec at all but a dialog built by ``MainWindow._income_chart_dialog``.

Synthetic data only -- invented institution and payee names, no PII. Money stays
signed integer cents; nothing here does money arithmetic the reports layer has
not already done.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QGroupBox

from mammon import db, ledger, reports
from mammon.ui.report_window import (
    ACCOUNT_BALANCES_SPEC,
    CASH_FLOW_SPEC,
    INCOME_EXPENSE_SPEC,
    ITEMIZE_SPEC,
    TRANSACTIONS_SPEC,
    ReportWindow,
)
from mammon.tests import fresh_db

JAN = ("2026-01-01", "2026-01-31")


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "pickers.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    """Two checking accounts, one savings, and a month of activity spanning a
    sub-categorized expense (Auto & Transport:Fuel / :Parking), a flat expense,
    income, and a transfer between accounts -- enough for every picker under
    test to have something to drop."""
    north = ledger.create_account(conn, "Bank North Checking", "checking")
    south = ledger.create_account(conn, "Bank South Checking", "checking")
    savings = ledger.create_account(conn, "Bank North Savings", "savings")
    fuel = ledger.resolve_category(conn, "Auto & Transport:Fuel")
    parking = ledger.resolve_category(conn, "Auto & Transport:Parking")
    groceries = ledger.resolve_category(conn, "Groceries")
    salary = ledger.resolve_category(conn, "Salary")

    ledger.add_transaction(conn, north, "2026-01-05", -100_00,
                           category_id=fuel, payee="Fuel Stop")
    ledger.add_transaction(conn, north, "2026-01-10", -20_00,
                           category_id=parking, payee="City Meter")
    ledger.add_transaction(conn, north, "2026-01-15", -80_00,
                           category_id=groceries, payee="Corner Market")
    ledger.add_transaction(conn, south, "2026-01-16", -25_00,
                           category_id=groceries, payee="Corner Market")
    ledger.add_transaction(conn, north, "2026-01-28", 2000_00,
                           category_id=salary, payee="ANON Employer")
    ledger.create_transfer(conn, north, savings, "2026-01-29", 500_00,
                           payee="Transfer to savings")
    return {"north": north, "south": south, "savings": savings}


def _check_only(lst, names):
    """Tick exactly ``names`` in a check-list widget, untick the rest."""
    for i in range(lst.count()):
        item = lst.item(i)
        item.setCheckState(Qt.Checked if item.text() in names else Qt.Unchecked)


def _labels(win):
    return [r.label for r in win._rows]


def _amount(win, label):
    """The signed cents the window is rendering on the row called ``label``."""
    return next(r.amount for r in win._rows if r.label == label)


def _january(win):
    win.filters.set_range(*JAN)
    win.refresh()


# -- half one: the control exists and is really shown -------------------------
# Nothing here calls exec_(): a modal dialog blocks forever under the offscreen
# platform. show() realizes the same widget tree, which is what isVisible()
# needs to mean anything.

@pytest.mark.parametrize("spec", [ITEMIZE_SPEC, CASH_FLOW_SPEC,
                                  INCOME_EXPENSE_SPEC, TRANSACTIONS_SPEC],
                         ids=lambda s: s.title)
def test_category_reports_offer_a_visible_category_picker(qapp, conn, seeded, spec):
    win = ReportWindow(conn, spec=spec)
    try:
        assert spec.show_categories is True, \
            f"{spec.title} consumes selected_categories() -- it must offer the list"
        lst = win.filters.category_list
        assert lst is not None, f"{spec.title} must show a category check-list"
        # Sourced from the ledger's top level, so income categories are offered
        # too -- an expenses-only source would silently drop Salary.
        listed = [lst.item(i).text() for i in range(lst.count())]
        assert listed == [c["name"] for c in ledger.category_children(conn, None)]
        assert {"Auto & Transport", "Groceries", "Salary"} <= set(listed)
        # Everything starts checked, which the bar reports as "no filter".
        assert win.filters.selected_categories() is None

        win.customize_dialog.show()
        try:
            assert lst.isVisible(), \
                f"{spec.title}'s category list must be visible in the gear popup"
            titles = [g.title() for g in win.customize_dialog.findChildren(QGroupBox)]
            assert "Categories" in titles
        finally:
            win.customize_dialog.hide()
    finally:
        win.close()


def test_account_balances_offers_a_visible_account_picker(qapp, conn, seeded):
    """Account Balances hid the account list because its aggregation took no
    account filter. It takes one now, so the picker comes back."""
    win = ReportWindow(conn, spec=ACCOUNT_BALANCES_SPEC)
    try:
        assert ACCOUNT_BALANCES_SPEC.show_accounts is True
        lst = win.filters.account_list
        assert lst is not None, "Account Balances must offer an account check-list"
        assert lst.count() >= 3

        win.customize_dialog.show()
        try:
            assert lst.isVisible()
            titles = [g.title() for g in win.customize_dialog.findChildren(QGroupBox)]
            assert "Accounts" in titles
        finally:
            win.customize_dialog.hide()
    finally:
        win.close()


def test_reports_that_cannot_honor_a_category_pick_do_not_offer_one(qapp, conn,
                                                                   seeded):
    """The other direction of the same rule: Account Balances does not group by
    category, so it must NOT grow a check-list that could only filter nothing."""
    win = ReportWindow(conn, spec=ACCOUNT_BALANCES_SPEC)
    try:
        assert ACCOUNT_BALANCES_SPEC.show_categories is False
        assert win.filters.category_list is None
    finally:
        win.close()


# -- half two: the selection really moves the money ---------------------------

def test_cash_flow_category_pick_narrows_expenses_and_keeps_subcategories(
        qapp, conn, seeded):
    win = ReportWindow(conn, spec=CASH_FLOW_SPEC)
    try:
        _january(win)
        assert "Groceries" in _labels(win)
        assert _amount(win, "Total Expense") == -225_00

        _check_only(win.filters.category_list, {"Auto & Transport"})
        assert win.filters.selected_categories() == {"Auto & Transport"}
        win.filters.apply_button.click()

        # The picked top level brings its whole subtree: each sub-category keeps
        # its own row, which is the "expand to see the subcategories" case.
        assert "Auto & Transport:Fuel" in _labels(win)
        assert "Auto & Transport:Parking" in _labels(win)
        assert "Groceries" not in _labels(win)
        assert "Salary" not in _labels(win)
        assert _amount(win, "Total Expense") == -120_00
        assert _amount(win, "Total Income") == 0

        # The reports layer, not the UI, did the filtering.
        pure = reports.cash_flow(conn, *JAN, top_level_names=["Auto & Transport"])
        assert pure.total_expense == -120_00
        assert _amount(win, "Net Cash Flow") == pure.net
    finally:
        win.close()


def test_cash_flow_category_pick_leaves_the_transfers_section_alone(
        qapp, conn, seeded):
    """A transfer carries no category, so a category pick has nothing to say
    about it; silencing the section would leave Net claiming money stayed put
    when it moved."""
    win = ReportWindow(conn, spec=CASH_FLOW_SPEC)
    try:
        _january(win)
        # Restrict to ONE account so a transfer really does cross the boundary.
        win.filters.clear_accounts()
        for i in range(win.filters.account_list.count()):
            item = win.filters.account_list.item(i)
            if item.text() == "Bank North Checking":
                item.setCheckState(Qt.Checked)
        _check_only(win.filters.category_list, {"Auto & Transport"})
        win.filters.apply_button.click()

        assert "Bank North Savings" in _labels(win)
        assert _amount(win, "Net Transfers") == -500_00
        pure = reports.cash_flow(conn, *JAN, account_ids=[seeded["north"]],
                                 top_level_names=["Auto & Transport"])
        assert _amount(win, "Net Cash Flow") == pure.net
    finally:
        win.close()


def test_income_expense_pick_spanning_income_and_expense_keeps_both_sides(
        qapp, conn, seeded):
    """The rental-summary case: one income and one expense category picked
    together must leave both sides standing, with Net recomputed over what is
    left rather than echoing the unfiltered report."""
    win = ReportWindow(conn, spec=INCOME_EXPENSE_SPEC)
    try:
        _january(win)
        assert _amount(win, "Net Income") == 1775_00

        _check_only(win.filters.category_list, {"Salary", "Groceries"})
        win.filters.apply_button.click()

        assert "Salary" in _labels(win)
        assert "Groceries" in _labels(win)
        assert "Auto & Transport:Fuel" not in _labels(win)
        assert _amount(win, "Total Income") == 2000_00
        assert _amount(win, "Total Expense") == -105_00

        pure = reports.income_expense(conn, *JAN, bucket="total",
                                      top_level_names=["Salary", "Groceries"])
        assert _amount(win, "Net Income") == pure.net == 1895_00
    finally:
        win.close()


def test_transactions_category_pick_lists_only_that_subtree(qapp, conn, seeded):
    win = ReportWindow(conn, spec=TRANSACTIONS_SPEC)
    try:
        _january(win)
        # 5 categorized entries plus BOTH legs of the transfer, since the
        # listing spans every account.
        assert _amount(win, "7 transactions") == -225_00 + 2000_00

        _check_only(win.filters.category_list, {"Auto & Transport"})
        win.filters.apply_button.click()

        payees = [r.cells[1] for r in win._rows if r.cells and r.cells[0] != "Total"]
        assert set(payees) == {"Fuel Stop", "City Meter"}   # Fuel AND Parking
        assert _amount(win, "2 transactions") == -120_00

        pure = reports.transactions(conn, *JAN,
                                    top_level_names=["Auto & Transport"])
        assert pure.count == 2 and pure.total_cents == -120_00
    finally:
        win.close()


def test_account_balances_account_pick_drops_the_other_account(qapp, conn,
                                                               seeded):
    """Restricting to one of two accounts must drop the other's balance row --
    and the Net Worth total must follow, or the table and its total disagree."""
    win = ReportWindow(conn, spec=ACCOUNT_BALANCES_SPEC)
    try:
        win.filters.set_range(*JAN)
        win.refresh()
        assert "Bank South Checking" in _labels(win)

        win.filters.clear_accounts()
        for i in range(win.filters.account_list.count()):
            item = win.filters.account_list.item(i)
            if item.text() == "Bank North Checking":
                item.setCheckState(Qt.Checked)
        assert win.filters.selected_account_ids() == [seeded["north"]]
        win.filters.apply_button.click()

        assert _labels(win) == ["Bank North Checking", "Net Worth"]
        assert "Bank South Checking" not in _labels(win)

        pure = reports.account_balances(conn, JAN[1],
                                        account_ids=[seeded["north"]])
        assert len(pure.rows) == 1
        assert _amount(win, "Net Worth") == pure.total == 1300_00
    finally:
        win.close()


def test_account_balances_unfiltered_still_values_every_account(qapp, conn,
                                                               seeded):
    """None means all: the new parameter must not change today's behavior."""
    everything = reports.account_balances(conn, JAN[1])
    assert {r.name for r in everything.rows} == {
        "Bank North Checking", "Bank South Checking", "Bank North Savings"}
    assert everything.total == sum(r.cents for r in everything.rows)


# -- the Income by Category pie ----------------------------------------------
# Not a ReportSpec: MainWindow._income_chart_dialog builds its own dialog around
# the shared CustomizeDialog, the way the spending pie does. It passed
# categories=None, so its gear popup had no category list at all. Driving the
# dialog itself would need exec_() (which blocks forever offscreen), so this
# pins the two pieces the dialog is assembled from: the income-side name source
# and the by-name filter applied to the pie's rows.

def test_income_chart_category_source_is_income_not_expense(qapp, conn, seeded):
    """The pie asks the ONE shared picker for the INCOME scope. There is no
    income-specific name source any more (``MainWindow._income_report_categories``
    and its expense twin are gone): the kind is a parameter, so the income and
    expense lists are the same code answering different questions."""
    from mammon.ui.report_filters import (CATEGORY_KIND_EXPENSE,
                                          CATEGORY_KIND_INCOME,
                                          category_picker_names)

    names = category_picker_names(conn, CATEGORY_KIND_INCOME)
    assert names == ["Salary"], "the income pie must list INCOME categories"
    # The expense scope is a different answer -- offering it to the income pie
    # would give the user a check-list that ticks nothing the pie could show.
    expense = category_picker_names(conn, CATEGORY_KIND_EXPENSE)
    assert "Salary" not in expense
    assert {"Auto & Transport", "Groceries"} <= set(expense)


def test_income_chart_rows_filter_by_the_checked_names(qapp, conn, seeded):
    """The pie's refresh() narrows its rows by the checked names -- the same
    by-name filter the spending pie uses (no SQL, no money math in the UI)."""
    ledger.add_transaction(conn, seeded["north"], "2026-01-20", 300_00,
                           category_id=ledger.resolve_category(conn, "Interest"),
                           payee="Bank North")
    rows = reports.income_category_rows(conn, *JAN)
    assert {n for n, _c in rows} == {"Salary", "Interest"}

    sel = {"Interest"}
    narrowed = [(n, c) for n, c in rows if n in sel]
    assert narrowed == [("Interest", 300_00)]


# A ``_FakeWindow`` stub used to stand in for MainWindow so its two private
# category-name helpers could be called without opening the whole app. Both
# helpers are gone -- the picker is a module-level function over a connection --
# so nothing needs to impersonate a window any more.

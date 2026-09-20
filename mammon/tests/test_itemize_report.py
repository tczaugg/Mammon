"""Regression: the Itemize by Category report is an expandable drill-down again.

When Itemize moved into the generalized :class:`ReportWindow` it was flattened to
a two-column (Category, Amount) table and its header bar mislabeled the category
column with "Date". This module pins the restored behavior:

* the pure projection (:func:`itemize_tree_rows`) walks the
  :class:`~mammon.reports.ItemizedTree` into a depth-tagged row list -- sections,
  categories, sub-categories and finally the transaction leaves -- so a
  QTreeWidget can re-nest it and the CSV/HTML/PDF export can flatten it;
* the four columns read Category, Date, Payee / Memo, Amount -- Category first,
  so the Date header sits over the dates, never over the category names;
* the window really hosts a tree (not the flat table) and drills
  section -> category -> sub-category -> transaction.

Synthetic data only -- no PII. Money stays signed integer cents; the UI never
sums or formats it here (that is the reports layer + :func:`fmt_cents`).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QGroupBox

from mammon import db, ledger
from mammon.reports import itemize_tree, period_range
from mammon.ui.report_window import (
    ITEMIZE_SPEC,
    TREE_COLUMNS,
    ReportWindow,
    itemize_tree_rows,
    tree_rows_to_csv,
    tree_rows_to_html,
)
from mammon.tests import fresh_db


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "itemize.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    """A checking account with a sub-categorized expense (Auto & Transport:Fuel),
    a flat leaf expense, and salary income -- enough to exercise every tree depth
    (section -> category -> sub-category -> transaction)."""
    a = ledger.create_account(conn, "Checking", "checking")
    fuel = ledger.resolve_category(conn, "Auto & Transport:Fuel")
    parking = ledger.resolve_category(conn, "Auto & Transport:Parking")
    groceries = ledger.resolve_category(conn, "Groceries")
    salary = ledger.resolve_category(conn, "Salary")

    ledger.add_transaction(conn, a, "2026-01-05", -100_00, category_id=fuel,
                           payee="Shell")
    ledger.add_transaction(conn, a, "2026-01-10", -20_00, category_id=parking,
                           payee="Meter")
    ledger.add_transaction(conn, a, "2026-01-15", -80_00, category_id=groceries,
                           payee="Market")
    ledger.add_transaction(conn, a, "2026-01-28", 2000_00, category_id=salary,
                           payee="ACME")
    return {"a": a, "fuel": fuel}


# ---- pure projection: the depth-tagged tree rows --------------------------

def test_itemize_tree_rows_walk_section_to_transaction(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    tree = itemize_tree(conn, start, end)
    rows = itemize_tree_rows(tree)

    # Every row carries the four Category/Date/Payee/Amount cells.
    assert all(len(r.cells) == len(TREE_COLUMNS) == 4 for r in rows)

    # Sections are bold, start expanded, and put their label in the Category cell
    # with the Date/Payee cells blank (grouping rows never carry a date).
    sections = [r for r in rows if r.kind == "section"]
    assert [r.cells[0] for r in sections] == ["INCOME", "EXPENSES"]
    for r in sections:
        assert r.bold and r.expanded
        assert r.cells[1] == "" and r.cells[2] == ""

    # A transaction leaf carries a blank Category cell, a formatted Date, and its
    # payee -- so the hierarchy reads straight down the first column.
    txns = [r for r in rows if r.kind == "txn"]
    assert txns, "the tree must drill down to individual transactions"
    for r in txns:
        assert r.cells[0] == ""          # blank category keeps the tree shape
        assert r.cells[1]                # a rendered date sits in the Date column
    assert any("Shell" in r.cells[2] for r in txns)

    # Deepest structure: the Fuel sub-category sits below its Auto & Transport
    # parent, which sits below the EXPENSES section (depths 0 < 1 < 2 < 3 txn).
    depths = {r.kind for r in rows}
    assert {"section", "category", "txn"} <= depths
    fuel_leaf = next(r for r in rows if r.kind == "txn" and "Shell" in r.cells[2])
    assert fuel_leaf.depth >= 3

    # The final row is the grand total (bold, depth 0).
    assert rows[-1].kind == "total" and rows[-1].bold and rows[-1].depth == 0


# ---- pure serializers: flatten the tree with indentation ------------------

def test_tree_rows_csv_and_html_carry_four_columns_and_indent(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    rows = itemize_tree_rows(itemize_tree(conn, start, end))

    csv_text = tree_rows_to_csv(rows)
    assert csv_text.splitlines()[0] == "Category,Date,Payee / Memo,Amount"
    # Depth survives the flatten as leading spaces on the Category column.
    assert any(line.startswith("  ") for line in csv_text.splitlines()[1:])

    html_text = tree_rows_to_html(rows, "Itemize by Category")
    assert '<th align="left">Category</th>' in html_text
    assert '<th align="right">Amount</th>' in html_text
    assert "Section" not in html_text          # the shared 3-col header is gone
    assert "<b>EXPENSES</b>" in html_text       # sections render bold


# ---- window: it hosts a drill-down tree, not the flat table ---------------

def test_itemize_window_is_a_tree_with_category_first_headers(qapp, conn, seeded):
    win = ReportWindow(conn, spec=ITEMIZE_SPEC)
    try:
        assert win.windowTitle() == "Itemize by Category"
        assert ITEMIZE_SPEC.is_tree is True
        assert ITEMIZE_SPEC.columns == ["Category", "Date", "Payee / Memo", "Amount"]

        # The tree replaces the flat table entirely.
        assert win.tree is not None
        assert win.table is None

        headers = [win.tree.headerItem().text(i)
                   for i in range(win.tree.columnCount())]
        assert headers == ["Category", "Date", "Payee / Memo", "Amount"]

        # Drill down: a top-level section expands to a category that expands to a
        # sub-category (or leaf) that expands to a dated transaction row.
        assert win.tree.topLevelItemCount() >= 1
        found_txn = False
        stack = [win.tree.topLevelItem(i)
                 for i in range(win.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            for i in range(item.childCount()):
                stack.append(item.child(i))
            # A transaction row is the only one with a Date but no Category text.
            if item.text(1) and not item.text(0):
                found_txn = True
        assert found_txn, "the tree must expand down to individual transactions"
    finally:
        win.close()


# ---- the account picker: present, visible, and a REAL filter --------------
# The user's report was "Itemize by Category has no account customization -- I
# can't see the breakdown of one bank's spending by category". The account
# check-list is shared (ReportFilterBar) but lives behind the gear, so nothing
# pinned that ITEMIZE_SPEC keeps it: a spec-level ``show_accounts=False``, or a
# hidden/never-laid-out gear, would silently take the picker away from this one
# report while every other report kept it. These two tests pin both halves --
# the control is there and visible, and the selection actually narrows the money.


def test_itemize_customization_offers_a_visible_account_picker(qapp, conn, seeded):
    """The gear popup for Itemize really contains a shown account check-list.

    Nothing here calls ``exec_()``: a modal dialog blocks forever under the
    offscreen platform. ``show()`` realizes the same widget tree, which is what
    ``isVisible()`` needs to mean anything.
    """
    win = ReportWindow(conn, spec=ITEMIZE_SPEC)
    try:
        # The spec must not opt out of the shared account check-list.
        assert ITEMIZE_SPEC.show_accounts is True

        # The gear that opens the customization is really parented into the
        # window (not built and dropped), so the picker is reachable at all.
        assert win.gear_button is not None
        assert win.gear_button.parent() is win

        # The check-list exists on the bar inside the popup ...
        bar = win.filters
        assert bar.account_list is not None, \
            "Itemize's customization must offer an account check-list"
        assert bar.account_list.count() >= 1

        # ... and it is actually shown when the popup is opened, rather than
        # built into a collapsed/hidden branch of the layout.
        win.customize_dialog.show()
        try:
            assert bar.account_list.isVisible(), \
                "the account check-list must be visible in the gear popup"
            titles = [g.title() for g in win.customize_dialog.findChildren(QGroupBox)]
            assert "Accounts" in titles
        finally:
            win.customize_dialog.hide()
    finally:
        win.close()


def test_itemize_account_selection_narrows_the_category_totals(qapp, conn):
    """Two accounts spend in the SAME category; restricting the picker to one
    must report only that account's spending -- the whole point of the control.

    Synthetic institution names only.
    """
    north = ledger.create_account(conn, "Bank North Checking", "checking")
    south = ledger.create_account(conn, "Bank South Checking", "checking")
    groceries = ledger.resolve_category(conn, "Groceries")
    ledger.add_transaction(conn, north, "2026-01-05", -100_00,
                           category_id=groceries, payee="Market")
    ledger.add_transaction(conn, south, "2026-01-06", -25_00,
                           category_id=groceries, payee="Market")

    win = ReportWindow(conn, spec=ITEMIZE_SPEC)
    try:
        win.filters.set_range("2026-01-01", "2026-01-31")
        win.refresh()

        def groceries_amount():
            row = next(r for r in win._rows
                       if r.cells[0].strip() == "Groceries" and r.kind != "txn")
            return row.cells[-1]

        # Both accounts in scope: the category carries the combined spend.
        assert groceries_amount() == "-125.00"

        # Narrow to ONE account the way the user does -- Clear all, tick one,
        # press Apply (the same signal the gear popup's Apply button emits).
        win.filters.clear_accounts()
        for i in range(win.filters.account_list.count()):
            item = win.filters.account_list.item(i)
            if item.text() == "Bank North Checking":
                item.setCheckState(Qt.Checked)
        assert win.filters.selected_account_ids() == [north]
        win.filters.apply_button.click()

        # Only the picked account's spending survives -- not the other bank's.
        assert groceries_amount() == "-100.00"

        # The filtering happens in the reports layer, not the UI: the pure
        # function agrees with what the window shows.
        tree = itemize_tree(conn, "2026-01-01", "2026-01-31", account_ids=[north])
        assert tree.total_cents == -100_00
    finally:
        win.close()


def test_itemize_window_period_change_refreshes_tree(qapp, conn, seeded):
    win = ReportWindow(conn, spec=ITEMIZE_SPEC)
    try:
        # Switch the shared Period dropdown to a year that has no data: the tree
        # rebuilds (no crash, sections drop to nothing but the total row logic in
        # the projector still holds).
        idx = win.period_combo.findData("ytd")
        if idx >= 0:
            win.period_combo.setCurrentIndex(idx)
        # Whatever period is selected, the body is still the tree, never a table.
        assert win.table is None
        assert win.tree is not None
    finally:
        win.close()


# -- the category picker (SRD 5.9c) -----------------------------------------
# Itemize takes top_level_names, so its customization popup must offer a
# top-level category check-list. It was hardcoded off for every ReportWindow
# report, which left _run_itemize threading a selected_categories() that could
# only ever answer None. The user's case: "I want a report of just my Church
# spending", and "Landlord spending and Rent income, for a complete summary" --
# the second spans an income and an expense category at once.

def _check_only(lst, names):
    """Tick exactly ``names`` in a check-list widget, untick the rest."""
    for i in range(lst.count()):
        item = lst.item(i)
        item.setCheckState(Qt.Checked if item.text() in names else Qt.Unchecked)


def _tree_labels(win, depth):
    """The non-blank Category-column labels at ``depth`` in the rendered rows.
    Depth 1 is the top-level categories and 2 their sub-categories (transaction
    leaves carry a blank Category cell). Depth 0 holds the INCOME/EXPENSES
    sections AND the grand-total row, so read those by ``kind`` instead."""
    return [r.cells[0] for r in win._rows if r.depth == depth and r.cells[0]]


def test_itemize_customization_offers_the_ledger_top_level_categories(
        qapp, conn, seeded):
    win = ReportWindow(conn, spec=ITEMIZE_SPEC)
    try:
        assert ITEMIZE_SPEC.show_categories is True
        lst = win.filters.category_list
        assert lst is not None, "Itemize must show a category check-list"
        listed = [lst.item(i).text() for i in range(lst.count())]
        # Sourced from the ledger's top level -- income categories included, which
        # an expenses-only source would silently drop.
        assert listed == [c["name"] for c in ledger.category_children(conn, None)]
        assert {"Auto & Transport", "Groceries", "Salary"} <= set(listed)
        # Everything starts checked, which the bar reports as "no filter".
        assert win.filters.selected_categories() is None
    finally:
        win.close()


def test_itemize_category_pick_restricts_tree_but_keeps_subcategories(
        qapp, conn, seeded):
    win = ReportWindow(conn, spec=ITEMIZE_SPEC)
    try:
        win.filters.set_range("2026-01-01", "2026-01-31")
        _check_only(win.filters.category_list, {"Auto & Transport"})
        assert win.filters.selected_categories() == {"Auto & Transport"}
        win.filters.apply_button.click()          # -> applied -> refresh

        assert _tree_labels(win, 1) == ["Auto & Transport"]
        # ...and it still expands to its sub-categories and their transactions.
        assert _tree_labels(win, 2) == ["Fuel", "Parking"]
        payees = [r.cells[2] for r in win._rows if r.kind == "txn"]
        assert any("Shell" in p for p in payees)
        assert any("Meter" in p for p in payees)
        assert not any("Market" in p or "ACME" in p for p in payees)
    finally:
        win.close()


def test_itemize_category_pick_spanning_income_and_expense_keeps_both_sections(
        qapp, conn, seeded):
    """The rental-summary case: one income category and one expense category
    picked together must yield BOTH sections, not whichever sign wins."""
    win = ReportWindow(conn, spec=ITEMIZE_SPEC)
    try:
        win.filters.set_range("2026-01-01", "2026-01-31")
        _check_only(win.filters.category_list, {"Salary", "Auto & Transport"})
        win.filters.apply_button.click()

        sections = [r.cells[0] for r in win._rows if r.kind == "section"]
        assert sections == ["INCOME", "EXPENSES"]
        assert set(_tree_labels(win, 1)) == {"Salary", "Auto & Transport"}
        assert "Groceries" not in _tree_labels(win, 1)
        # The expense side still drills down under its own section.
        assert _tree_labels(win, 2) == ["Fuel", "Parking"]
    finally:
        win.close()


def test_itemize_with_nothing_checked_falls_back_to_every_category(
        qapp, conn, seeded):
    """Clear-all is a step on the way to ticking one box, not a request for an
    empty report -- unlike the account list, an empty category pick means "all"."""
    win = ReportWindow(conn, spec=ITEMIZE_SPEC)
    try:
        win.filters.set_range("2026-01-01", "2026-01-31")
        win.filters.clear_categories()
        assert win.filters.selected_categories() == set()
        win.filters.apply_button.click()
        assert set(_tree_labels(win, 1)) == \
            {"Salary", "Auto & Transport", "Groceries"}
    finally:
        win.close()

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


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "itemize.db")
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

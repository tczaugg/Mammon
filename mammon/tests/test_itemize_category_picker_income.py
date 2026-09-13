"""Regression: Itemize by Category's customization picker offers INCOME too.

The report renders an INCOME section and an EXPENSES section, so its category
check-list has to offer both kinds of top-level category; an expense-only list
cannot express "show me just my salary" or "everything except the rent I
collect". The defect it guards against is sourcing the list from
``MainWindow._report_categories`` (derived from ``reports.spending_by_category``,
which sums money OUT only, so every income category is silently missing) instead
of the domain-layer top-level reader ``ledger.category_children(conn, None)``.

The picker the user actually sees is reached by the Reports > "Itemize by
Category…" menu action -> ``MainWindow._itemize_window`` -> ``_open_report_window
(ITEMIZE_SPEC)`` -> ``ReportWindow`` -> ``CustomizeDialog`` -> ``ReportFilterBar``,
so these tests walk that same wiring rather than calling the report function
directly. Nothing here ``exec_()``s a dialog: under the offscreen platform that
would block forever (see CLAUDE.md's headless-modal hazard) -- the check-list is
inspected on the live bar, and made visible with show()/hide() only.

Synthetic data only -- no PII. Money stays signed integer cents.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt

from mammon import db, ledger
from mammon.ui.report_window import ITEMIZE_SPEC, ReportWindow


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "itemize_picker.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    """One income top-level category and one expense top-level category, each with
    a transaction inside the default report period, plus a hidden category that
    must stay out of the picker."""
    acct = ledger.create_account(conn, "Checking", "checking")
    salary = ledger.resolve_category(conn, "Salary")
    groceries = ledger.resolve_category(conn, "Groceries")
    retired = ledger.resolve_category(conn, "Retired Category")
    conn.execute("UPDATE categories SET hidden=1 WHERE id=?", (retired,))
    conn.commit()

    ledger.add_transaction(conn, acct, "2026-01-05", 5000_00, category_id=salary,
                           payee="Employer")
    ledger.add_transaction(conn, acct, "2026-01-06", -125_00, category_id=groceries,
                           payee="Market")
    return {"acct": acct, "salary": salary, "groceries": groceries}


def _window(conn):
    """The window the menu action opens, built exactly as the app builds it."""
    return ReportWindow(conn, spec=ITEMIZE_SPEC)


def _labels(check_list):
    return [check_list.item(i).text() for i in range(check_list.count())]


def _check_only(win, name):
    """Tick exactly one category and re-run the report, as Apply does."""
    lst = win.filters.category_list
    for i in range(lst.count()):
        item = lst.item(i)
        item.setCheckState(Qt.Checked if item.text() == name else Qt.Unchecked)
    win.refresh()
    return win._rows


def _section(rows, label):
    """(section row, its descendant rows) for the named section, or (None, [])."""
    for i, r in enumerate(rows):
        if r.kind == "section" and r.cells[0] == label:
            body = []
            for later in rows[i + 1:]:
                if later.kind in ("section", "total"):
                    break
                body.append(later)
            return r, body
    return None, []


# ---- the menu action really lands on this window/spec ----------------------

def test_menu_action_opens_the_itemize_spec_window():
    """Reports > "Itemize by Category…" calls _itemize_window, which opens the
    generic report window on ITEMIZE_SPEC -- so the picker under test is the one
    the user sees."""
    from mammon.ui.widgets import MainWindow

    opened = []

    class _Stub:
        _open_report_window = lambda self, spec: opened.append(spec)  # noqa: E731

    MainWindow._itemize_window(_Stub())
    assert opened == [ITEMIZE_SPEC]
    assert ITEMIZE_SPEC.show_categories, "no category list would be built at all"


# ---- 1. the picker lists income AND expense categories ---------------------

def test_picker_lists_income_and_expense_categories(qapp, conn, seeded):
    win = _window(conn)
    lst = win.filters.category_list
    assert lst is not None, "Itemize must offer a category check-list"

    labels = _labels(lst)
    assert "Salary" in labels, "income categories are missing from the picker"
    assert "Groceries" in labels
    # Hidden categories stay out, as before.
    assert "Retired Category" not in labels
    # Everything starts ticked, which the bar reports as "no filter" (all).
    assert all(lst.item(i).checkState() == Qt.Checked for i in range(lst.count()))
    assert win.filters.selected_categories() is None

    # The list is really on screen in the customization popup (never exec_() it:
    # that blocks forever offscreen).
    win.customize_dialog.show()
    assert lst.isVisible()
    win.customize_dialog.hide()


def test_picker_is_not_the_money_out_only_list(qapp, conn, seeded):
    """The known bad source (spending-only names) cannot produce this list."""
    from mammon import reports

    spending_names = {r.name for r
                      in reports.spending_by_category(conn, "2026-01-01",
                                                      "2026-12-31").rows}
    # Hold the window: dropping the last Python reference deletes the C++ widget
    # and _labels then raises "wrapped C/C++ object has been deleted".
    win = _window(conn)
    labels = set(_labels(win.filters.category_list))
    assert "Salary" in labels - spending_names


# ---- 2. ticking either kind really filters its section ---------------------

def test_income_only_selection_filters_the_report(qapp, conn, seeded):
    win = _window(conn)
    rows = _check_only(win, "Salary")

    assert win.filters.selected_categories() == {"Salary"}
    income, body = _section(rows, "INCOME")
    assert income is not None and income.cells[-1] == "5,000.00"
    assert any(r.kind == "category" and r.cells[0] == "Salary" for r in body)
    expenses, _ = _section(rows, "EXPENSES")
    assert expenses is None, "an income-only pick must leave no expense section"
    assert not any(r.cells[0] == "Groceries" for r in rows)


def test_expense_only_selection_filters_the_report(qapp, conn, seeded):
    win = _window(conn)
    rows = _check_only(win, "Groceries")

    assert win.filters.selected_categories() == {"Groceries"}
    expenses, body = _section(rows, "EXPENSES")
    assert expenses is not None and expenses.cells[-1] == "-125.00"
    assert any(r.kind == "category" and r.cells[0] == "Groceries" for r in body)
    income, _ = _section(rows, "INCOME")
    assert income is None, "an expense-only pick must leave no income section"
    assert not any(r.cells[0] == "Salary" for r in rows)


def test_all_ticked_shows_both_sections(qapp, conn, seeded):
    win = _window(conn)
    rows = win._rows
    assert _section(rows, "INCOME")[0] is not None
    assert _section(rows, "EXPENSES")[0] is not None

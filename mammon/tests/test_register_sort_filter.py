"""Register sorting and filtering (parity roadmap, item 2, stage 1).

The register model keeps the ledger-ordered rows and shows the view a
PROJECTION of them -- sorted by the clicked column, narrowed by the filter bar
-- through one indirection (``RegisterModel._view``) that every row-indexed
method reads. Three properties are locked in:

* an edit made through a sorted or filtered row reaches the transaction the
  user is looking at, not the one that used to sit at that index;
* the Balance column keeps each row's date-ordered running value whatever the
  sort (sorting by payee never recomputes a balance -- Quicken's behavior);
* the blank quick-entry row stays last and keeps working while sorted or
  filtered, and the filter reports what it hid.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QCoreApplication, Qt
from PyQt5.QtWidgets import QApplication

from mammon import db, ledger
from mammon.ui.models import RegisterFilter, RegisterModel
from mammon.ui.widgets import RegisterWidget
from mammon.tests import fresh_db

R = RegisterModel


def _settle():
    """Turn the event loop once, so the blank row's deferred reload actually runs.

    A blank-row commit writes to the database IMMEDIATELY but pushes the model
    RESET to the next event-loop turn: it runs inside a delegate's setModelData
    while Qt is destroying the editor, and resetting there frees the editor the
    frame still holds (see ``RegisterModel._write`` and the modal/heap note in
    CLAUDE.md). So the DB is correct the instant setData returns while the
    model's cached rows -- what ``txn_at``/``view_counts`` read -- are one turn
    behind. A running app turns the loop constantly; a test has to do it by
    hand, exactly as ``test_ui._settle`` does.
    """
    for _ in range(3):
        QCoreApplication.processEvents()



@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "sortfilter.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    rent = ledger.resolve_category(conn, "Housing:Rent")
    salary = ledger.resolve_category(conn, "Income:Salary")
    ledger.add_transaction(conn, chk, "2026-01-05", -500_00, payee="Rent Co", num="101",
                           category_id=rent, cleared=1)
    ledger.add_transaction(conn, chk, "2026-01-10", 2000_00, payee="Paycheck",
                           category_id=salary, cleared=1, reconciled=1)
    ledger.add_transaction(conn, chk, "2026-01-12", -4_50, payee="Cafe", memo="latte")
    ledger.add_transaction(conn, chk, "2026-01-20", -520_00, payee="Rent Co", num="102",
                           category_id=rent)
    ledger.add_transaction(conn, chk, "2026-02-01", -30_00, payee="Bookshop", num="EFT",
                           tag="gift")
    return chk


def _payees(m):
    return [m.txn_at(i)["payee"] for i in range(m.rowCount() - 1)]


def _balances_by_id(conn, chk):
    return {r["id"]: r["balance"] for r in ledger.register_rows(conn, chk)}


# ---------------------------------------------------------------------------
# sorting
# ---------------------------------------------------------------------------
def test_default_projection_is_ledger_order(qapp, conn, account):
    m = RegisterModel(conn, account)
    assert _payees(m) == ["Rent Co", "Paycheck", "Cafe", "Rent Co", "Bookshop"]
    assert m.sort_state() == (R.DATE, Qt.AscendingOrder)
    assert m.is_blank_row(m.rowCount() - 1)


def test_sort_by_payee_reorders_and_keeps_each_rows_balance(qapp, conn, account):
    m = RegisterModel(conn, account)
    ledger_bal = _balances_by_id(conn, account)
    m.set_sort(R.PAYEE, Qt.AscendingOrder)
    assert _payees(m) == ["Bookshop", "Cafe", "Paycheck", "Rent Co", "Rent Co"]
    # Ties (the two Rent Co rows) keep ledger order; balances travel with rows.
    assert m.txn_at(3)["date"] == "2026-01-05" and m.txn_at(4)["date"] == "2026-01-20"
    for i in range(5):
        r = m.txn_at(i)
        assert r["balance"] == ledger_bal[r["id"]]
    assert m.is_blank_row(m.rowCount() - 1)          # blank row still last
    m.set_sort(R.PAYEE, Qt.DescendingOrder)
    assert _payees(m)[:2] == ["Rent Co", "Rent Co"]


def test_sort_by_date_descending_is_the_exact_reverse(qapp, conn, account):
    m = RegisterModel(conn, account)
    m.set_sort(R.DATE, Qt.DescendingOrder)
    assert _payees(m) == ["Bookshop", "Rent Co", "Cafe", "Paycheck", "Rent Co"]
    m.set_sort(R.DATE, Qt.AscendingOrder)
    assert _payees(m) == ["Rent Co", "Paycheck", "Cafe", "Rent Co", "Bookshop"]


def test_sort_num_is_numeric_then_text_then_blank(qapp, conn, account):
    m = RegisterModel(conn, account)
    m.set_sort(R.NUM, Qt.AscendingOrder)
    assert [m.txn_at(i)["num"] or "" for i in range(5)] == ["101", "102", "EFT", "", ""]


def test_sort_money_and_clr_columns(qapp, conn, account):
    m = RegisterModel(conn, account)
    m.set_sort(R.PAYMENT, Qt.DescendingOrder)
    assert _payees(m)[:2] == ["Rent Co", "Rent Co"] and m.txn_at(0)["amount"] == -520_00
    m.set_sort(R.DEPOSIT, Qt.DescendingOrder)
    assert _payees(m)[0] == "Paycheck"
    m.set_sort(R.CLR, Qt.DescendingOrder)
    assert _payees(m)[:2] == ["Paycheck", "Rent Co"]     # R, then c, then blank
    m.set_sort(R.BALANCE, Qt.AscendingOrder)
    assert m.txn_at(0)["balance"] == 500_00              # after the first rent


def test_edit_through_a_sorted_row_reaches_the_row_shown(qapp, conn, account):
    m = RegisterModel(conn, account)
    m.set_sort(R.PAYEE, Qt.AscendingOrder)
    assert m.txn_at(0)["payee"] == "Bookshop"
    assert m.setData(m.index(0, R.MEMO), "novels", Qt.EditRole)
    assert conn.execute("SELECT memo FROM transactions WHERE payee='Bookshop'"
                        ).fetchone()["memo"] == "novels"
    # The sort survives the write's reload, and row_for_txn follows it.
    assert _payees(m)[0] == "Bookshop"
    tid = conn.execute("SELECT id FROM transactions WHERE payee='Cafe'").fetchone()["id"]
    assert m.row_for_txn(tid) == 1


def test_blank_row_entry_works_while_sorted(qapp, conn, account):
    m = RegisterModel(conn, account)
    m.set_sort(R.PAYEE, Qt.DescendingOrder)
    b = m.rowCount() - 1
    m.setData(m.index(b, R.DATE), "2026-02-10", Qt.EditRole)
    m.setData(m.index(b, R.PAYEE), "Zoo", Qt.EditRole)
    m.setData(m.index(b, R.PAYMENT), "12.00", Qt.EditRole)
    _settle()                                            # the reload is deferred
    assert _payees(m)[0] == "Zoo"                        # lands in sorted position
    assert m.is_blank_row(m.rowCount() - 1)
    assert ledger.account_balance(conn, account) == 1000_00 - 500_00 + 2000_00 - 4_50 - 520_00 - 30_00 - 12_00


# ---------------------------------------------------------------------------
# filtering
# ---------------------------------------------------------------------------
def test_filter_matching_rules():
    row = {"payee": "Rent Co", "memo": "", "num": "101", "tag": "", "category_label":
           "Housing:Rent", "amount": -520_00, "date": "2026-01-20", "cleared": 1,
           "reconciled": 0}
    assert RegisterFilter().is_empty()
    assert RegisterFilter(text="rent").matches(row)
    assert RegisterFilter(text="housing").matches(row)          # category label
    assert RegisterFilter(text="520").matches(row)              # the amount text
    assert not RegisterFilter(text="latte").matches(row)
    assert RegisterFilter(date_from="2026-01-20", date_to="2026-01-20").matches(row)
    assert not RegisterFilter(date_to="2026-01-19").matches(row)
    assert RegisterFilter(amount_min=500_00, amount_max=600_00).matches(row)
    assert not RegisterFilter(amount_max=100_00).matches(row)
    assert RegisterFilter(clr="cleared").matches(row)
    assert not RegisterFilter(clr="reconciled").matches(row)
    assert not RegisterFilter(clr="uncleared").matches(row)


def test_model_filter_narrows_rows_keeps_blank_row_and_counts(qapp, conn, account):
    m = RegisterModel(conn, account)
    m.set_filter(RegisterFilter(text="rent co"))
    assert _payees(m) == ["Rent Co", "Rent Co"]
    assert m.view_counts() == (2, 5)
    assert m.is_blank_row(m.rowCount() - 1)
    # Sort and filter compose.
    m.set_sort(R.DATE, Qt.DescendingOrder)
    assert [m.txn_at(i)["date"] for i in range(2)] == ["2026-01-20", "2026-01-05"]
    m.set_filter(RegisterFilter(clr="uncleared"))
    assert sorted(_payees(m)) == ["Bookshop", "Cafe", "Rent Co"]
    # An empty filter is no filter.
    m.set_filter(RegisterFilter())
    assert m.filter_state() is None and m.view_counts() == (5, 5)


def test_edit_and_entry_through_a_filtered_view(qapp, conn, account):
    m = RegisterModel(conn, account)
    m.set_filter(RegisterFilter(text="cafe"))
    assert m.rowCount() == 2                              # one match + blank row
    assert m.setData(m.index(0, R.TAG), "coffee", Qt.EditRole)
    assert conn.execute("SELECT tag FROM transactions WHERE payee='Cafe'"
                        ).fetchone()["tag"] == "coffee"
    # A new row that does not match the filter is recorded but not shown.
    b = m.rowCount() - 1
    m.setData(m.index(b, R.DATE), "2026-02-11", Qt.EditRole)
    m.setData(m.index(b, R.PAYEE), "Grocer", Qt.EditRole)
    m.setData(m.index(b, R.PAYMENT), "40.00", Qt.EditRole)
    _settle()                                            # the reload is deferred
    assert m.view_counts() == (1, 6)


# ---------------------------------------------------------------------------
# the widget: header clicks and the filter bar
# ---------------------------------------------------------------------------
def test_header_indicator_sorts_and_keeps_the_selected_transaction(qapp, conn, account):
    w = RegisterWidget(conn, account)
    m = w.model
    cafe = conn.execute("SELECT id FROM transactions WHERE payee='Cafe'").fetchone()["id"]
    w.view.selectRow(m.row_for_txn(cafe))
    w.view.setCurrentIndex(m.index(m.row_for_txn(cafe), R.PAYEE))
    hh = w.view.horizontalHeader()
    hh.setSortIndicator(R.PAYEE, Qt.AscendingOrder)     # what a header click does
    assert m.sort_state() == (R.PAYEE, Qt.AscendingOrder)
    assert _payees(m)[0] == "Bookshop"
    assert w.view.currentIndex().row() == m.row_for_txn(cafe) == 1
    w.deleteLater()


def test_filter_bar_narrows_reports_and_clears_on_hide(qapp, conn, account):
    w = RegisterWidget(conn, account)
    m = w.model
    assert w.filter_bar.isHidden() and not w.act_filter.isChecked()
    w.set_filter_visible(True)
    assert w.act_filter.isChecked()
    w.filter_text.setText("rent")
    assert m.view_counts() == (2, 5)
    assert w.filter_count.text() == "Showing 2 of 5"
    w.filter_clr.setCurrentIndex(w.filter_clr.findData("uncleared"))
    assert m.view_counts() == (1, 5)
    assert m.txn_at(0)["date"] == "2026-01-20"
    w.filter_min.setText("600")
    w.filter_min.editingFinished.emit()
    assert m.view_counts() == (0, 5) and m.rowCount() == 1   # only the blank row
    w._clear_filter()
    assert m.filter_state() is None and w.filter_count.text() == ""
    w.filter_text.setText("cafe")
    assert m.view_counts() == (1, 5)
    # Hiding the bar (gear menu unticked) clears the filter with it.
    w.act_filter.setChecked(False)
    assert w.filter_bar.isHidden()
    assert m.filter_state() is None and m.view_counts() == (5, 5)
    assert w.filter_text.text() == ""
    w.deleteLater()

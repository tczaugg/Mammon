"""Feature parity between the INVESTMENT register and the cash register
(parity roadmap item 2 remainder / upgrade_priorities #5).

The investment register must offer the same register TOOLKIT the cash register
does -- click-to-sort, a filter bar that narrows the shown rows, a persisted
column chooser, multi-row batch editing, find-and-replace and Quicken's Void --
while keeping the CONTENT differences a security ledger genuinely needs (Share
Bal / Price / Inv Amt columns, an Action verb, no cash-only Payee/Category
fields). Every write still funnels through :mod:`mammon.investments` (the sole
writer of ``investment_transactions``) or :mod:`mammon.ledger`; the register
never grows a second write path.

This is a life-cycle test over the real widget + model + domain code:

  1. SORT -- ``set_sort`` re-projects the shown rows (Decimal quantities, the
     money columns, dates), and Date descending is the exact reverse;
  2. FILTER -- an :class:`InvestmentFilter` (and the widget's filter bar) narrows
     the visible set and reports what it hid, leaving the underlying rows intact;
  3. BATCH EDIT -- a multi-row selection sets the memo on several rows at once,
     THROUGH ``investments.update_investment_fields`` (no cash ``transactions``
     row is ever written);
  4. VOID -- one row (and a batch) is taken out of the money AND the share math
     via ``investments.void_investment``, stays as a **VOID** record, and is
     idempotent;
  5. COLUMN CHOOSER -- hiding a column persists through ``ui/prefs`` (QSettings,
     its own scope) and re-applies to a freshly built register;
  6. FIND & REPLACE -- a memo substitution rewrites matching rows through the
     investment write path.

Offscreen Qt; synthetic data only (no real security, holding or amount).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest
from PyQt5.QtCore import QItemSelectionModel, Qt
from PyQt5.QtWidgets import QApplication

import mammon.ui.widgets as W
from mammon import db, investments, ledger
from mammon.ui.models import InvestmentFilter, InvestmentRegisterModel
from mammon.ui.widgets import InvestmentRegisterWidget
from mammon.tests import fresh_db

M = InvestmentRegisterModel


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    # The column chooser persists through QSettings; keep it out of the real
    # profile so the test neither reads nor writes a developer's own prefs.
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "inv_parity.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)


def _buy(conn, a, date, sym, qty, price, amount, memo=None):
    return investments.record_investment(
        conn, a, date, "Buy", symbol=sym, quantity=qty, price=price,
        amount=amount, memo=memo)


def _syms(m):
    """The security/symbol of each shown row, in view order."""
    return [m.txn_at(i)["symbol"] for i in range(m.rowCount())]


def _col(name: str) -> int:
    return M.HEADERS.index(name)


# ---------------------------------------------------------------------------
# 1. SORT -- the header click re-projects the model's shown rows
# ---------------------------------------------------------------------------
def test_sort_reorders_rows(qapp, conn, acct):
    # Dates ascending, but quantities deliberately out of that order.
    _buy(conn, acct, "2026-01-05", "AAA", "30", "10.00", -300_00)
    _buy(conn, acct, "2026-02-05", "BBB", "10", "20.00", -200_00)
    _buy(conn, acct, "2026-03-05", "CCC", "20", "15.00", -300_00)
    reg = InvestmentRegisterWidget(conn, acct)
    try:
        m = reg.model
        # Default projection is application (date) order.
        assert m.sort_state() == (M.DATE, Qt.AscendingOrder)
        assert _syms(m) == ["AAA", "BBB", "CCC"]

        # Sort by Quantity ascending -> 10, 20, 30 -> BBB, CCC, AAA.
        reg._on_sort_changed(M.QUANTITY, Qt.AscendingOrder)
        assert _syms(m) == ["BBB", "CCC", "AAA"]
        assert [Decimal(m.txn_at(i)["quantity"]) for i in range(3)] == [
            Decimal(10), Decimal(20), Decimal(30)]

        # Sort by Security (symbol) descending.
        reg._on_sort_changed(M.SECURITY, Qt.DescendingOrder)
        assert _syms(m) == ["CCC", "BBB", "AAA"]

        # Date descending is the exact reverse of the ledger order.
        reg._on_sort_changed(M.DATE, Qt.DescendingOrder)
        assert _syms(m) == ["CCC", "BBB", "AAA"]
        reg._on_sort_changed(M.DATE, Qt.AscendingOrder)
        assert _syms(m) == ["AAA", "BBB", "CCC"]
    finally:
        reg.deleteLater()


# ---------------------------------------------------------------------------
# 2. FILTER -- narrows the shown rows, leaving the stored rows intact
# ---------------------------------------------------------------------------
def test_filter_narrows_the_visible_set(qapp, conn, acct):
    _buy(conn, acct, "2026-01-05", "AAA", "30", "10.00", -300_00)
    _buy(conn, acct, "2026-02-05", "BBB", "10", "20.00", -200_00)
    _buy(conn, acct, "2026-03-05", "CCC", "20", "15.00", -300_00)
    reg = InvestmentRegisterWidget(conn, acct)
    try:
        m = reg.model
        assert m.rowCount() == 3

        # Text filter over the symbol column, driven through the widget's bar.
        reg.set_filter_visible(True)
        reg.filter_text.setText("BBB")     # textChanged -> _apply_filter
        assert m.view_counts() == (1, 3)
        assert _syms(m) == ["BBB"]
        # Only the VIEW narrowed; the stored history is untouched.
        assert len(investments.list_investment_txns(conn, acct)) == 3

        # Clearing the bar restores every row and drops the filter.
        reg.set_filter_visible(False)
        assert m.filter_state() is None
        assert m.rowCount() == 3

        # An amount-range filter, at the model level (the bar feeds this).
        m.set_filter(InvestmentFilter(amount_min=250_00))
        assert sorted(_syms(m)) == ["AAA", "CCC"]   # the two 300.00 buys
        m.set_filter(None)
        assert m.rowCount() == 3
    finally:
        reg.deleteLater()


# ---------------------------------------------------------------------------
# 3. BATCH EDIT -- a multi-row memo change writes through investments.py
# ---------------------------------------------------------------------------
def test_batch_edit_writes_through_investments(qapp, conn, acct, monkeypatch):
    t1 = _buy(conn, acct, "2026-01-05", "AAA", "30", "10.00", -300_00)
    t2 = _buy(conn, acct, "2026-02-05", "BBB", "10", "20.00", -200_00)
    t3 = _buy(conn, acct, "2026-03-05", "CCC", "20", "15.00", -300_00)
    reg = InvestmentRegisterWidget(conn, acct)
    try:
        m = reg.model
        # Multi-select rows 0 and 1 and confirm the widget sees both.
        sm = reg.view.selectionModel()
        for row in (0, 1):
            sm.select(m.index(row, 0),
                      QItemSelectionModel.Select | QItemSelectionModel.Rows)
        assert reg._selected_rows() == [0, 1]

        # The batch edit's text comes through the overridable seam.
        monkeypatch.setattr(reg, "_ask_batch_text",
                            lambda *a, **k: "audited 2026")
        reg._batch_memo(reg._selected_rows())
        assert reg.last_batch == ("Changed memo on", 2, 0)
    finally:
        reg.deleteLater()

    # The memo landed on exactly the two selected investment rows, in the
    # investment_transactions table -- and NOTHING was written to the cash
    # `transactions` table (no second write path).
    assert investments.get_investment_txn(conn, t1)["memo"] == "audited 2026"
    assert investments.get_investment_txn(conn, t2)["memo"] == "audited 2026"
    assert investments.get_investment_txn(conn, t3)["memo"] is None
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 4. VOID -- take the row out of the money and the share math, keep the record
# ---------------------------------------------------------------------------
def test_void_marks_a_row_void(qapp, conn, acct, monkeypatch):
    tid = _buy(conn, acct, "2026-01-05", "AAA", "10", "100.00", -1000_00,
               memo="opening lot")
    reg = InvestmentRegisterWidget(conn, acct)
    try:
        # The confirm dialog is answered Yes through the patched seam.
        monkeypatch.setattr(W.QMessageBox, "question",
                            lambda *a, **k: W.QMessageBox.Yes)
        reg._void_row(0)

        # The row is voided in the store: zero money, no share movement, and the
        # **VOID** mark on the memo (with the original amount noted).
        row = investments.get_investment_txn(conn, tid)
        assert investments.is_void_investment(row)
        assert row["amount"] == 0
        assert row["quantity"] is None
        assert "was 1,000.00" in row["memo"]

        # The register surfaces the void on the Action cell (no payee to stamp).
        m = reg.model
        assert m.data(m.index(0, _col("Action"))).startswith(ledger.VOID_PREFIX)

        # It is out of the share math: the 10-share lot no longer counts.
        investments.rebuild_holdings(conn, acct)
        held = investments.get_holding(conn, acct, "AAA")
        assert held is None or Decimal(held["quantity"]) == 0

        # Void is idempotent -- a second void changes nothing.
        assert investments.void_investment(conn, tid) is False
    finally:
        reg.deleteLater()


def test_batch_void(qapp, conn, acct):
    t1 = _buy(conn, acct, "2026-01-05", "AAA", "30", "10.00", -300_00)
    t2 = _buy(conn, acct, "2026-02-05", "BBB", "10", "20.00", -200_00)
    reg = InvestmentRegisterWidget(conn, acct)
    try:
        changed, skipped = reg.model.batch_void([t1, t2])
        assert (changed, skipped) == (2, 0)
    finally:
        reg.deleteLater()
    assert investments.is_void_investment(investments.get_investment_txn(conn, t1))
    assert investments.is_void_investment(investments.get_investment_txn(conn, t2))


# ---------------------------------------------------------------------------
# 5. COLUMN CHOOSER -- persists through ui/prefs (QSettings), its own scope
# ---------------------------------------------------------------------------
def test_column_chooser_persists_via_prefs(qapp, conn, acct):
    from mammon.ui import prefs
    _buy(conn, acct, "2026-01-05", "AAA", "10", "100.00", -1000_00)
    reg = InvestmentRegisterWidget(conn, acct)
    try:
        assert not reg.view.isColumnHidden(M.PRICE)
        reg.set_column_hidden(M.PRICE, True)
        assert reg.view.isColumnHidden(M.PRICE)
        # Persisted under the investment register's OWN scope...
        assert "Price" in prefs.hidden_columns(scope="investment_register")
        # ...and never bleeds into the cash register's chooser.
        assert "Price" not in prefs.hidden_columns()   # scope="register"
    finally:
        reg.deleteLater()

    # A freshly built register applies the persisted choice on open.
    reg2 = InvestmentRegisterWidget(conn, acct)
    try:
        assert reg2.view.isColumnHidden(M.PRICE)
        reg2.set_column_hidden(M.PRICE, False)         # show it again
        assert not reg2.view.isColumnHidden(M.PRICE)
        assert "Price" not in prefs.hidden_columns(scope="investment_register")
    finally:
        reg2.deleteLater()


# ---------------------------------------------------------------------------
# 6. FIND & REPLACE -- a memo substitution rewrites rows through investments.py
# ---------------------------------------------------------------------------
def test_find_replace_memo_through_investments(qapp, conn, acct, monkeypatch):
    t1 = _buy(conn, acct, "2026-01-05", "AAA", "10", "100.00", -1000_00,
              memo="fee reimbursed")
    t2 = _buy(conn, acct, "2026-02-05", "BBB", "5", "50.00", -250_00,
              memo="advisory fee")
    t3 = _buy(conn, acct, "2026-03-05", "CCC", "1", "10.00", -10_00,
              memo="dividend")
    reg = InvestmentRegisterWidget(conn, acct)
    try:
        monkeypatch.setattr(reg, "_ask_find_replace",
                            lambda: ("memo", "fee", "charge"))
        # The completion notice is a modal; route it through the seam tests
        # patch so it never blocks under the offscreen platform (CLAUDE.md's
        # headless-modal hazard).
        monkeypatch.setattr(reg, "_notify", lambda *a, **k: None)
        reg.find_replace()
    finally:
        reg.deleteLater()
    assert investments.get_investment_txn(conn, t1)["memo"] == "charge reimbursed"
    assert investments.get_investment_txn(conn, t2)["memo"] == "advisory charge"
    assert investments.get_investment_txn(conn, t3)["memo"] == "dividend"

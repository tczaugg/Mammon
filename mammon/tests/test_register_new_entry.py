"""Regression: entering a NEW transaction on the register's blank last line must
COMMIT, not hang and then vanish.

The bug: the blank quick-entry row auto-commits the moment a date and a money
column are both present (RegisterModel._set_blank). That commit ran
``add_from_values`` -> ``reload()`` -- a full beginResetModel/endResetModel --
SYNCHRONOUSLY, inside whichever delegate's ``setModelData`` had just been called.
Qt calls setModelData BEFORE it destroys the cell editor, so resetting the model
there invalidates the very index the view still holds and is about to tear down.
Qt then dereferences freed internals: the app hangs, then the row disappears
instead of being recorded -- a hard access violation (0xc0000374) with no Python
traceback. It is the exact hazard ``RegisterModel._write`` already defers reloads
to avoid, and the reason the Enter/closeEditor blank-row commits defer too.

The fix keeps ``mammon.ledger`` the sole writer and writes the row synchronously
(a fresh connection sees it at once), but defers only the model RESET a turn --
so these tests assert both halves: the ledger holds the transaction immediately,
while the model reset is pushed to the next event-loop turn rather than run under
a live editor.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QCoreApplication, Qt
from PyQt5.QtWidgets import QApplication, QStyleOptionViewItem, QWidget

from mammon import db, ledger
from mammon.ui.delegates import MoneyDelegate
from mammon.ui.models import RegisterModel


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "new_entry.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=100_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return chk, sav


def _settle():
    """Turn the event loop once, so the deferred model reset actually runs. A
    live app turns the loop constantly; a test has to do it by hand (see the same
    helper in test_ui / test_quickfill)."""
    QCoreApplication.processEvents()


def _money_editor(delegate, model, row, col):
    """Open an inline money editor exactly as the view does, so setModelData runs
    in the real editor-teardown context that triggered the bug."""
    parent = QWidget()
    idx = model.index(row, col)
    editor = delegate.createEditor(parent, QStyleOptionViewItem(), idx)
    editor._parent_ref = parent          # keep the parent alive through the editor
    delegate.setEditorData(editor, idx)
    return editor, idx


def test_new_blank_row_entry_commits_through_ledger(qapp, conn, accounts):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    blank = m.rowCount() - 1
    resets = []
    m.modelReset.connect(lambda: resets.append(1))

    m.setData(m.index(blank, RegisterModel.DATE), "2026-05-01", Qt.EditRole)
    m.setData(m.index(blank, RegisterModel.PAYEE), "Grocery Store", Qt.EditRole)
    # Typing the amount is what completes and commits the new row.
    m.setData(m.index(blank, RegisterModel.PAYMENT), "42.00", Qt.EditRole)

    # The ledger -- the single writer -- has the transaction immediately, in
    # signed integer cents (money out is negative).
    rows = ledger.register_rows(conn, chk)
    assert any(r["payee"] == "Grocery Store" and r["amount"] == -42_00 for r in rows)
    assert ledger.account_balance(conn, chk) == 100_00 - 42_00
    # But the model reset did NOT run inside setData's editor teardown...
    assert resets == []
    # ...and the quick-entry buffer is cleared, so a racing Enter cannot re-insert.
    assert m.blank_values() == {}

    _settle()                             # the turn a running app would take
    assert resets == [1]                  # the view refreshes exactly once
    assert m.txn_at(0)["payee"] == "Grocery Store"
    assert m.is_blank_row(m.rowCount() - 1)   # a fresh blank row is ready


def test_new_entry_commit_survives_editor_teardown(qapp, conn, accounts):
    """Drive the commit through MoneyDelegate.setModelData -- the actual path Qt
    takes while destroying the cell editor -- and confirm it records the row
    rather than hanging and dropping it."""
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    blank = m.rowCount() - 1
    resets = []
    m.modelReset.connect(lambda: resets.append(1))

    m.setData(m.index(blank, RegisterModel.DATE), "2026-06-01", Qt.EditRole)
    m.setData(m.index(blank, RegisterModel.PAYEE), "Hardware Store", Qt.EditRole)

    delegate = MoneyDelegate()
    editor, idx = _money_editor(delegate, m, blank, RegisterModel.PAYMENT)
    editor.setText("18.50")
    delegate.setModelData(editor, m, idx)     # commits inside editor teardown

    # Written through the ledger at once; the reset waits for the editor to die.
    row = conn.execute(
        "SELECT amount FROM transactions WHERE account_id=? AND date='2026-06-01'",
        (chk,)).fetchone()
    assert row["amount"] == -18_50
    assert resets == []

    _settle()
    assert resets == [1]
    assert m.txn_at(0)["payee"] == "Hardware Store"
    # No duplicate row was written by the deferred turn.
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id=? AND date='2026-06-01'",
        (chk,)).fetchone()[0] == 1


def test_pre_entered_amount_alone_does_not_commit_the_blank_row(qapp, conn, accounts):
    """A date with no money typed is not a complete row: nothing is written and
    the blank row stays open (guards against the fix committing too eagerly)."""
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    blank = m.rowCount() - 1
    before = ledger.account_balance(conn, chk)

    m.setData(m.index(blank, RegisterModel.DATE), "2026-07-01", Qt.EditRole)
    m.setData(m.index(blank, RegisterModel.PAYEE), "Nobody", Qt.EditRole)
    _settle()

    assert ledger.account_balance(conn, chk) == before
    assert m.is_blank_row(m.rowCount() - 1)
    assert m.blank_values().get("date") == "2026-07-01"   # buffer intact, not committed

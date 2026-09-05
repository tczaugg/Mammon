"""Feature: a NEW transaction can be SPLIT during entry, just like editing a
pending transaction -- not only assigned a single category.

The register's blank quick-entry row auto-commits a plain, single-category
transaction the moment a date and a typed amount are both present
(RegisterModel._set_blank). But a QuickFilled recurring payee (a paycheck, a
mortgage bill) reaches the blank row with its amount PRE-ENTERED (it waits in
_auto, so it never trips that auto-commit) -- exactly the transaction a user
wants to break across several categories. Before this change the Split gesture
was a silent no-op on the blank row, so the only way to split a new transaction
was to commit it single-category first and then reopen the split editor.

The fix mirrors how a pending review row is already split (RegisterWidget.
_split_row): commit the in-progress row through mammon.ledger FIRST -- the sole
writer -- then open the very same SplitDialog on the row it created, Copy-from-
previous-<payee> button and all. No second write path: the transaction is born
via ledger.add_transaction and its split lines are written via ledger.set_splits.

These tests drive the real RegisterModel / SplitDialog offscreen and never
exec_() a modal (which would block under the offscreen platform); the widget
test monkeypatches SplitDialog to a non-blocking fake, the seam existing split
tests already use.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication, QDialog

from mammon import db, ledger
from mammon.ui import widgets
from mammon.ui.models import RegisterModel


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "new_split.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return chk, sav


def _split_after_lines(dlg, lines):
    """Replace the split dialog's seeded lines with (category, dollars, memo)
    tuples, then persist through the ledger. Returns apply_split's result."""
    for e in list(dlg._lines):
        dlg._remove_line(e)
    for cat, dollars, memo in lines:
        dlg.add_line(cat, dollars, memo)
    return dlg.apply_split()


def test_new_transaction_entered_with_multiline_split_persists_via_ledger(
        qapp, conn, accounts):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    # An in-progress NEW transaction on the blank row: a paycheck whose amount
    # QuickFill pre-entered (lives in _auto, so it has NOT auto-committed as a
    # single-category row) -- the moment the user reaches for Split.
    m._new = {"date": "2026-05-01", "payee": "Employer Payroll"}
    m._auto = {"deposit": "1000.00"}

    txn_id = m.commit_blank_returning_id()
    assert txn_id and txn_id > 0                      # committed via the ledger
    row = m.row_for_txn(txn_id)
    assert row is not None and row >= 0

    dlg = widgets.SplitDialog(m, row)
    # Gross salary in, taxes withheld out -- nets to the $1000 deposit.
    assert _split_after_lines(dlg, [
        ("Income:Salary", 1200.00, "gross"),
        ("Taxes", -200.00, "withholding"),
    ])

    splits = ledger.get_splits(conn, txn_id)
    assert len(splits) == 2
    # Split lines are signed integer cents and sum to the transaction total.
    assert sorted(s["amount"] for s in splits) == [-200_00, 1200_00]
    assert sum(s["amount"] for s in splits) == 1000_00
    assert int(ledger.get_transaction(conn, txn_id)["amount"]) == 1000_00
    # The register now renders it as a split, not a lone category.
    assert ledger.category_display(
        conn, ledger.get_transaction(conn, txn_id)) == ledger.SPLIT_LABEL


def test_new_transaction_split_offers_copy_from_previous_payee(qapp, conn, accounts):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    # A prior split for this exact payee -- the breakdown Copy-from-previous
    # should re-enter in one click.
    prev = ledger.add_transaction(conn, chk, "2026-04-01", 1000_00,
                                  payee="Employer Payroll")
    ledger.set_splits(conn, prev, [
        {"category_id": ledger.resolve_category(conn, "Income:Salary"),
         "amount": 1200_00, "memo": None},
        {"category_id": ledger.resolve_category(conn, "Taxes"),
         "amount": -200_00, "memo": None},
    ])
    m.reload()

    # This month's paycheck, still in-progress on the blank row.
    m._new = {"date": "2026-05-01", "payee": "Employer Payroll"}
    m._auto = {"deposit": "1000.00"}
    txn_id = m.commit_blank_returning_id()
    assert txn_id and txn_id > 0

    dlg = widgets.SplitDialog(m, m.row_for_txn(txn_id))
    assert dlg._prior_split                           # the button is offered
    dlg._copy_previous_split()                        # one-click re-entry
    assert dlg.apply_split()

    splits = ledger.get_splits(conn, txn_id)
    assert sorted(s["amount"] for s in splits) == [-200_00, 1200_00]
    assert sum(s["amount"] for s in splits) == 1000_00


def test_split_gesture_on_blank_row_materialises_then_opens_editor(
        qapp, conn, accounts, monkeypatch):
    chk, _ = accounts
    reg = widgets.RegisterWidget(conn, chk)
    # In-progress NEW transaction on the blank row (QuickFilled amount).
    reg.model._new = {"date": "2026-05-01", "payee": "Employer Payroll"}
    reg.model._auto = {"deposit": "1000.00"}
    blank = reg.model.rowCount() - 1
    assert reg.model.is_blank_row(blank)

    opened = {}

    class _FakeSplit:
        def __init__(self, model, row, parent=None):
            opened["txn"] = model.txn_at(row)

        def exec_(self):
            return QDialog.Rejected

    monkeypatch.setattr(widgets, "SplitDialog", _FakeSplit)
    before = len(ledger.register_rows(conn, chk))
    reg._split_row(blank)                             # the toolbar Split gesture

    # The blank row was committed into a REAL transaction and the split editor
    # opened on it -- previously the blank row was a silent no-op.
    assert opened.get("txn") is not None
    assert opened["txn"]["payee"] == "Employer Payroll"
    assert len(ledger.register_rows(conn, chk)) == before + 1


def test_split_gesture_on_empty_blank_row_is_still_a_noop(
        qapp, conn, accounts, monkeypatch):
    """Guard the fix from over-firing: an empty blank row (no date, no amount)
    has nothing to commit, so Split stays a no-op and never opens the editor."""
    chk, _ = accounts
    reg = widgets.RegisterWidget(conn, chk)
    blank = reg.model.rowCount() - 1

    opened = {"n": 0}

    class _FakeSplit:
        def __init__(self, model, row, parent=None):
            opened["n"] += 1

        def exec_(self):
            return QDialog.Rejected

    monkeypatch.setattr(widgets, "SplitDialog", _FakeSplit)
    before = len(ledger.register_rows(conn, chk))
    reg._split_row(blank)                             # no crash, no dialog

    assert opened["n"] == 0
    assert len(ledger.register_rows(conn, chk)) == before

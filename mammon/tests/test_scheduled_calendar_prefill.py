"""The finance calendar's Add-scheduled dialog PREFILLS and visibly INDICATES
the category -- or the full split -- a payee will inherit from its most recent
real transaction, so the learned split (task 6107f483) is no longer invisible
behind a lone default category.

The domain behavior (a definition reproduces its stored split on every
pre-entry, through ledger.set_splits) is already covered by test_scheduled_split;
this file covers the UI GAP: on the calendar's Schedule-<payee> editor the
inherited category/split must be surfaced when the payee is entered, and what the
dialog shows must be exactly what the write path (schedule_prediction, via
ledger.previous_split_for_payee) will actually enter. Read-only lookups only;
no second transaction writer is introduced.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

from mammon import categorize, db, ledger
from mammon.ui.scheduled_payments_dialog import ScheduledPaymentEditor
from mammon.tests import fresh_db


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "prefill.db")
    yield c
    c.close()


def _world(conn):
    """A checking account with a recurring SPLIT paycheck ("ANON Payroll":
    +3000 salary, -600 tax, netting +2400) and a plain single-category payee
    ("ANON Gym" -> Health). Synthetic names per the anonymization convention."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    salary = ledger.resolve_category(conn, "Salary")
    tax = ledger.resolve_category(conn, "Taxes")
    health = ledger.resolve_category(conn, "Health")
    for date in ("2026-06-15", "2026-07-15", "2026-08-15"):
        tid = ledger.add_transaction(conn, chk, date, 2400_00, payee="ANON Payroll")
        ledger.set_splits(conn, tid, [
            {"category_id": salary, "amount": 3000_00, "memo": "gross"},
            {"category_id": tax, "amount": -600_00, "memo": "withholding"},
        ])
    ledger.add_transaction(conn, chk, "2026-08-20", -50_00, payee="ANON Gym",
                           category_id=health)
    return {"chk": chk, "salary": salary, "tax": tax, "health": health}


# ---------------------------------------------------------------------------
# The read-only domain lookup the dialog uses
# ---------------------------------------------------------------------------
def test_inherited_entry_returns_the_split(conn):
    w = _world(conn)
    info = categorize.inherited_entry_for_payee(conn, "ANON Payroll")
    assert [(s["category_id"], s["amount"]) for s in info["splits"]] == \
        [(w["salary"], 3000_00), (w["tax"], -600_00)]
    # A split carries its own categories, so the single-category answer is empty.
    assert info["category_id"] is None and info["category_label"] == ""


def test_inherited_entry_returns_single_category_when_not_split(conn):
    _world(conn)
    info = categorize.inherited_entry_for_payee(conn, "ANON Gym")
    assert info["splits"] == []
    assert info["category_label"] == "Health"


def test_inherited_entry_empty_for_unknown_payee(conn):
    _world(conn)
    info = categorize.inherited_entry_for_payee(conn, "ANON Never Seen")
    assert info == {"category_id": None, "category_label": "", "splits": []}


def test_inherited_split_matches_what_the_write_path_enters(conn):
    """What the dialog shows must equal what schedule_prediction actually learns
    and reproduces -- both read ledger.previous_split_for_payee, so they cannot
    drift apart."""
    _world(conn)
    shown = categorize.inherited_entry_for_payee(conn, "ANON Payroll")["splits"]
    applied = ledger.previous_split_for_payee(conn, "ANON Payroll")
    assert [(s["category_id"], s["amount"]) for s in shown] == \
        [(s["category_id"], s["amount"]) for s in applied]


# ---------------------------------------------------------------------------
# The dialog: prefill + visible indication
# ---------------------------------------------------------------------------
def test_dialog_indicates_inherited_split(qapp, conn):
    w = _world(conn)
    # The editor as the calendar opens it: pre-filled from a prediction whose
    # single dominant category is shown, with the split learning enabled.
    entry = {"account_id": w["chk"], "payee": "ANON Payroll", "amount": 2400_00,
             "frequency": "monthly", "next_date": "2026-09-15",
             "category_id": w["salary"], "category_label": "Salary"}
    dlg = ScheduledPaymentEditor(conn, entry=entry, learn_splits=True)
    try:
        # The split is surfaced (not left to be applied invisibly)...
        assert [(s["category_id"], s["amount"]) for s in dlg._inherited_splits] == \
            [(w["salary"], 3000_00), (w["tax"], -600_00)]
        hint = dlg.inherit_hint.text()
        assert "ANON Payroll" in hint
        assert "Salary" in hint and "Taxes" in hint
        assert "$3,000.00" in hint and "-$600.00" in hint
        # ...and the lone Category field is disabled: the split overrides it.
        assert not dlg.category.isEnabled()
    finally:
        dlg.deleteLater()


def test_dialog_prefills_single_category_for_known_payee(qapp, conn):
    w = _world(conn)
    # Manager-style Add (no prediction, no split learning): the user types a
    # known payee and tabs out (editingFinished -> _prefill_from_payee).
    dlg = ScheduledPaymentEditor(conn)
    try:
        idx = dlg.account.findData(w["chk"])
        if idx >= 0:
            dlg.account.setCurrentIndex(idx)
        dlg.payee.setText("ANON Gym")
        dlg._prefill_from_payee()
        assert dlg.category.currentText() == "Health"
        assert "ANON Gym" in dlg.inherit_hint.text()
        assert dlg._inherited_splits == []
        assert dlg.category.isEnabled()
    finally:
        dlg.deleteLater()


def test_dialog_does_not_clobber_a_category_the_user_typed(qapp, conn):
    _world(conn)
    dlg = ScheduledPaymentEditor(conn)
    try:
        dlg.category.setEditText("Groceries")
        dlg.payee.setText("ANON Gym")
        dlg._prefill_from_payee()
        # The user's own choice wins; no misleading provenance note is shown.
        assert dlg.category.currentText() == "Groceries"
        assert dlg.inherit_hint.text() == ""
    finally:
        dlg.deleteLater()


def test_manager_add_does_not_claim_a_split_it_will_not_enter(qapp, conn):
    """Without learn_splits (the plain manager Add stores no split template), the
    dialog must NOT promise a split -- claiming one would be a lie."""
    _world(conn)
    dlg = ScheduledPaymentEditor(conn)          # learn_splits defaults False
    try:
        dlg.payee.setText("ANON Payroll")
        dlg._prefill_from_payee()
        assert dlg._inherited_splits == []
        assert "split" not in dlg.inherit_hint.text().lower()
        assert dlg.category.isEnabled()
    finally:
        dlg.deleteLater()


def test_switching_to_transfer_drops_the_inherited_split(qapp, conn):
    w = _world(conn)
    # The transfer target must exist before the dialog builds its account combos.
    other = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    entry = {"account_id": w["chk"], "payee": "ANON Payroll", "amount": 2400_00,
             "frequency": "monthly", "next_date": "2026-09-15",
             "category_id": w["salary"], "category_label": "Salary"}
    dlg = ScheduledPaymentEditor(conn, entry=entry, learn_splits=True)
    try:
        assert dlg._inherited_splits                # split surfaced first
        # Pick a transfer target: a transfer definition carries neither category
        # nor split, so the hint clears.
        dlg.transfer.setCurrentIndex(dlg.transfer.findData(other))
        assert dlg._inherited_splits == []
        assert dlg.inherit_hint.text() == ""
    finally:
        dlg.deleteLater()

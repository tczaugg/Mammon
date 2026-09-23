"""Regression: the per-account audit log of changes to RECONCILED transactions
(migration 63, ``reconciled_change_log``).

A reconciled row should almost never change; when one does it can quietly throw
off the next reconcile of that account. ``mammon.ledger`` -- the sole writer of
transaction rows -- records every edit and deletion of an already-reconciled
transaction so the change can be traced afterwards. These tests pin the
life-cycle: an edit logs one row per changed field with the right old/new values
scoped to the right account, a deletion logs the values it lost, reconciling a
row is itself not a "change" to a reconciled row, and touching an UN-reconciled
transaction logs nothing at all.
"""
from __future__ import annotations

import pytest

from mammon import db, ledger
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    checking = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    savings = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return checking, savings


def _by_field(entries):
    return {e["field"]: e for e in entries}


def test_lifecycle_edit_and_delete_are_logged(conn, accounts):
    checking, savings = accounts

    # Create a transaction and reconcile it. Reconciling is a 0 -> 1 transition,
    # not a change to an already-reconciled row, so it must log nothing.
    txn = ledger.add_transaction(
        conn, checking, "2026-01-05", -50_00, payee="Safeway")
    ledger.update_transaction(conn, txn, reconciled=1)
    assert ledger.reconciled_change_log(conn, checking) == []

    # Edit the reconciled row's amount AND date in one call -> one row per field.
    ledger.update_transaction(conn, txn, amount=-60_00, date="2026-02-01")
    edits = _by_field(ledger.reconciled_change_log(conn, checking))
    assert set(edits) == {"amount", "date"}

    assert edits["amount"]["operation"] == "edit"
    assert edits["amount"]["transaction_id"] == txn
    assert edits["amount"]["old_value"] == "-5000"
    assert edits["amount"]["new_value"] == "-6000"

    assert edits["date"]["operation"] == "edit"
    assert edits["date"]["old_value"] == "2026-01-05"
    assert edits["date"]["new_value"] == "2026-02-01"

    # Every entry carries a timestamp.
    assert all(e["changed_at"] for e in ledger.reconciled_change_log(conn, checking))

    # A second reconciled transaction, then deleted -> a 'delete' entry per
    # surviving value field, with the lost value in old_value and new_value NULL.
    txn2 = ledger.add_transaction(
        conn, checking, "2026-03-01", -120_00, payee="Rent")
    ledger.update_transaction(conn, txn2, reconciled=1)
    ledger.delete_transaction(conn, txn2)

    deletes = [e for e in ledger.reconciled_change_log(conn, checking)
               if e["operation"] == "delete"]
    assert deletes, "a reconciled deletion must be recorded"
    assert all(e["transaction_id"] == txn2 for e in deletes)
    assert all(e["new_value"] is None for e in deletes)
    dfields = _by_field(deletes)
    assert dfields["amount"]["old_value"] == "-12000"
    assert dfields["date"]["old_value"] == "2026-03-01"
    assert dfields["payee"]["old_value"] == "Rent"

    # The whole trail is scoped to the account it happened in.
    assert ledger.reconciled_change_log(conn, savings) == []


def test_editing_an_unreconciled_transaction_logs_nothing(conn, accounts):
    checking, _ = accounts
    txn = ledger.add_transaction(
        conn, checking, "2026-01-05", -50_00, payee="Safeway")
    # Never reconciled -- edit and delete freely; the log stays empty.
    ledger.update_transaction(conn, txn, amount=-75_00, date="2026-01-09",
                              payee="Costco", memo="groceries")
    assert ledger.reconciled_change_log(conn, checking) == []

    txn2 = ledger.add_transaction(conn, checking, "2026-02-01", -10_00)
    ledger.delete_transaction(conn, txn2)
    assert ledger.reconciled_change_log(conn, checking) == []


def test_un_reconciling_a_reconciled_row_is_logged(conn, accounts):
    checking, _ = accounts
    txn = ledger.add_transaction(conn, checking, "2026-01-05", -50_00)
    ledger.update_transaction(conn, txn, reconciled=1)
    # Flipping reconciled 1 -> 0 on a reconciled row is exactly the quiet change
    # this log exists to catch.
    ledger.update_transaction(conn, txn, reconciled=0)
    entries = ledger.reconciled_change_log(conn, checking)
    assert len(entries) == 1
    assert entries[0]["field"] == "reconciled"
    assert entries[0]["old_value"] == "1"
    assert entries[0]["new_value"] == "0"


def test_no_op_edit_of_reconciled_row_logs_nothing(conn, accounts):
    checking, _ = accounts
    txn = ledger.add_transaction(conn, checking, "2026-01-05", -50_00,
                                 payee="Safeway")
    ledger.update_transaction(conn, txn, reconciled=1)
    # Re-setting a field to its current value changes nothing, so nothing logs.
    ledger.update_transaction(conn, txn, amount=-50_00, payee="Safeway")
    assert ledger.reconciled_change_log(conn, checking) == []


def test_deleting_a_reconciled_transfer_logs_both_reconciled_legs(conn, accounts):
    checking, savings = accounts
    pair = ledger.create_transfer(
        conn, checking, savings, "2026-01-05", 40_00, reconciled=1)
    # create_transfer returns the two leg ids; delete one, both go.
    leg = pair[0] if isinstance(pair, (list, tuple)) else pair
    ledger.delete_transaction(conn, leg)
    # Each account's reconciled leg is audited in its own account's log.
    assert any(e["operation"] == "delete"
               for e in ledger.reconciled_change_log(conn, checking))
    assert any(e["operation"] == "delete"
               for e in ledger.reconciled_change_log(conn, savings))

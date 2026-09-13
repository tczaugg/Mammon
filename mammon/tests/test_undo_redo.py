"""Undo/Redo for the account register.

Two layers are exercised:

* the domain manager (``mammon.undo.UndoManager``) driven directly against the
  ledger -- the manager is where the inverse-operation logic lives, and it holds
  no Qt, so these tests are fast and precise about add / edit / delete / transfer
  / split, id-churn across recreate, and redo-stack semantics; and
* the real ``RegisterModel`` write chokepoints (``add_from_values`` /
  ``delete_row`` / ``undo`` / ``redo``) under the offscreen Qt platform, proving
  the model records and replays through the ledger with no second write path.

All data is synthetic (no PII).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger, undo


# ---- fixtures --------------------------------------------------------------
@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    checking = ledger.create_account(conn, "Checking", "checking",
                                     opening_balance=100_00)
    savings = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return checking, savings


@pytest.fixture
def mgr(conn):
    return undo.UndoManager(conn)


def _rows(conn, account_id):
    """Transactions in one account, oldest first (read-only, for assertions)."""
    return conn.execute(
        "SELECT * FROM transactions WHERE account_id=? ORDER BY date, id",
        (account_id,),
    ).fetchall()


def _count(conn, account_id):
    return len(_rows(conn, account_id))


# ---- domain manager: add ---------------------------------------------------
def test_undo_redo_add(conn, accounts, mgr):
    checking, _ = accounts
    tid = ledger.add_transaction(conn, checking, "2026-01-05", -25_00,
                                 payee="Safeway", memo="groceries")
    mgr.record_add([tid])

    assert _count(conn, checking) == 1
    assert mgr.can_undo() and not mgr.can_redo()
    assert mgr.undo_label() == "Add transaction"

    assert mgr.undo() is True
    assert _count(conn, checking) == 0
    assert not mgr.can_undo() and mgr.can_redo()
    assert mgr.redo_label() == "Add transaction"

    assert mgr.redo() is True
    rows = _rows(conn, checking)
    assert len(rows) == 1
    assert rows[0]["amount"] == -25_00
    assert rows[0]["payee"] == "Safeway"
    assert rows[0]["memo"] == "groceries"
    assert ledger.account_balance(conn, checking) == 100_00 - 25_00


# ---- domain manager: edit --------------------------------------------------
def test_undo_redo_edit(conn, accounts, mgr):
    checking, _ = accounts
    tid = ledger.add_transaction(conn, checking, "2026-01-05", -25_00,
                                 payee="Safeway", memo="groceries")

    before = mgr.capture(tid)
    ledger.update_transaction(conn, tid, amount=-30_00, payee="Costco")
    mgr.record_edit(tid, before)
    assert mgr.undo_label() == "Edit transaction"

    assert mgr.undo() is True
    row = ledger.get_transaction(conn, tid)
    assert row["amount"] == -25_00
    assert row["payee"] == "Safeway"
    assert row["memo"] == "groceries"          # untouched field restored intact

    assert mgr.redo() is True
    row = ledger.get_transaction(conn, tid)
    assert row["amount"] == -30_00
    assert row["payee"] == "Costco"


def test_edit_that_changes_nothing_records_no_step(conn, accounts, mgr):
    checking, _ = accounts
    tid = ledger.add_transaction(conn, checking, "2026-01-05", -25_00, payee="A")
    before = mgr.capture(tid)
    ledger.update_transaction(conn, tid, payee="A")   # same value -> no change
    mgr.record_edit(tid, before)
    assert not mgr.can_undo()


# ---- domain manager: delete ------------------------------------------------
def test_undo_redo_delete(conn, accounts, mgr):
    checking, _ = accounts
    tid = ledger.add_transaction(conn, checking, "2026-01-05", -25_00,
                                 payee="Safeway", memo="x")
    snaps = mgr.capture_many([tid])            # snapshot BEFORE the delete
    ledger.delete_transaction(conn, tid)
    mgr.push_delete(snaps)

    assert _count(conn, checking) == 0
    assert mgr.undo_label() == "Delete transaction"

    assert mgr.undo() is True                  # recreate
    rows = _rows(conn, checking)
    assert len(rows) == 1
    assert rows[0]["amount"] == -25_00
    assert rows[0]["payee"] == "Safeway"
    assert rows[0]["memo"] == "x"

    assert mgr.redo() is True                  # delete again
    assert _count(conn, checking) == 0


def test_undo_delete_restores_splits(conn, accounts, mgr):
    checking, _ = accounts
    food = ledger.resolve_category(conn, "Food")
    gas = ledger.resolve_category(conn, "Auto:Gas")
    tid = ledger.add_transaction(conn, checking, "2026-01-05", -50_00, payee="Costco")
    ledger.set_splits(conn, tid, [
        {"category_id": food, "amount": -30_00, "memo": None},
        {"category_id": gas, "amount": -20_00, "memo": None},
    ])
    snaps = mgr.capture_many([tid])
    ledger.delete_transaction(conn, tid)
    mgr.push_delete(snaps)

    assert mgr.undo() is True
    rows = _rows(conn, checking)
    assert len(rows) == 1
    new_id = rows[0]["id"]
    assert ledger.has_splits(conn, new_id)
    assert sorted(s["amount"] for s in ledger.get_splits(conn, new_id)) == \
        [-30_00, -20_00]


# ---- domain manager: transfer (both mirror legs together) ------------------
def test_undo_redo_transfer_create_inverts_both_sides(conn, accounts, mgr):
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(conn, checking, savings, "2026-01-05",
                                            40_00, payee="Move")
    mgr.record_add([from_id, to_id], transfer=True)

    assert mgr.undo_label() == "Add transfer"
    assert _count(conn, checking) == 1 and _count(conn, savings) == 1

    assert mgr.undo() is True                  # both legs vanish together
    assert _count(conn, checking) == 0 and _count(conn, savings) == 0

    assert mgr.redo() is True                  # both legs return together
    crows, srows = _rows(conn, checking), _rows(conn, savings)
    assert len(crows) == 1 and len(srows) == 1
    assert crows[0]["amount"] == -40_00
    assert srows[0]["amount"] == 40_00
    assert crows[0]["transfer_account_id"] == savings
    assert srows[0]["transfer_account_id"] == checking
    assert crows[0]["transfer_pair_id"] == srows[0]["id"]
    assert srows[0]["transfer_pair_id"] == crows[0]["id"]


def test_undo_redo_transfer_delete_recreates_both(conn, accounts, mgr):
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(conn, checking, savings, "2026-01-05",
                                            40_00, payee="Move")
    # The register's delete_row snapshots BOTH legs, then one delete removes both.
    snaps = mgr.capture_many([from_id, to_id])
    ledger.delete_transaction(conn, from_id)
    mgr.push_delete(snaps, transfer=True)

    assert _count(conn, checking) == 0 and _count(conn, savings) == 0
    assert mgr.undo_label() == "Delete transfer"

    assert mgr.undo() is True                  # both legs recreated + relinked
    crows, srows = _rows(conn, checking), _rows(conn, savings)
    assert len(crows) == 1 and len(srows) == 1
    assert crows[0]["amount"] == -40_00 and srows[0]["amount"] == 40_00
    assert crows[0]["transfer_pair_id"] == srows[0]["id"]

    assert mgr.redo() is True                  # both deleted again
    assert _count(conn, checking) == 0 and _count(conn, savings) == 0


def test_undo_redo_transfer_edit_syncs_both_legs(conn, accounts, mgr):
    checking, savings = accounts
    from_id, to_id = ledger.create_transfer(conn, checking, savings, "2026-01-05",
                                            40_00, payee="Move")
    before = mgr.capture(from_id)
    ledger.update_transaction(conn, from_id, amount=-55_00, date="2026-02-01")
    mgr.record_edit(from_id, before)
    assert ledger.get_transaction(conn, from_id)["amount"] == -55_00
    assert ledger.get_transaction(conn, to_id)["amount"] == 55_00

    assert mgr.undo() is True                  # BOTH legs snap back
    assert ledger.get_transaction(conn, from_id)["amount"] == -40_00
    assert ledger.get_transaction(conn, to_id)["amount"] == 40_00
    assert ledger.get_transaction(conn, from_id)["date"] == "2026-01-05"
    assert ledger.get_transaction(conn, to_id)["date"] == "2026-01-05"

    assert mgr.redo() is True
    assert ledger.get_transaction(conn, from_id)["amount"] == -55_00
    assert ledger.get_transaction(conn, to_id)["amount"] == 55_00


# ---- domain manager: split -------------------------------------------------
def test_undo_redo_split(conn, accounts, mgr):
    checking, _ = accounts
    food = ledger.resolve_category(conn, "Food")
    gas = ledger.resolve_category(conn, "Auto:Gas")
    tid = ledger.add_transaction(conn, checking, "2026-01-05", -50_00, payee="Costco")

    before = mgr.capture(tid)
    ledger.set_splits(conn, tid, [
        {"category_id": food, "amount": -30_00, "memo": None},
        {"category_id": gas, "amount": -20_00, "memo": None},
    ])
    mgr.record_edit(tid, before, label="Edit splits")
    assert mgr.undo_label() == "Edit splits"
    assert ledger.has_splits(conn, tid)

    assert mgr.undo() is True
    assert not ledger.has_splits(conn, tid)

    assert mgr.redo() is True
    assert ledger.has_splits(conn, tid)
    assert sorted(s["amount"] for s in ledger.get_splits(conn, tid)) == \
        [-30_00, -20_00]


# ---- redo-after-undo consistency, and id churn -----------------------------
def test_redo_after_undo_consistency(conn, accounts, mgr):
    checking, _ = accounts
    tid = ledger.add_transaction(conn, checking, "2026-01-05", -25_00, payee="Safeway")
    mgr.record_add([tid])

    # Round-trip several times: each redo allocates a NEW row id, but the content
    # and the running balance must come back identically every time.
    for _ in range(3):
        assert mgr.undo() is True
        assert _count(conn, checking) == 0
        assert mgr.redo() is True
        rows = _rows(conn, checking)
        assert len(rows) == 1
        assert rows[0]["amount"] == -25_00
        assert rows[0]["payee"] == "Safeway"
    assert ledger.account_balance(conn, checking) == 75_00


def test_edit_survives_add_undo_redo_id_churn(conn, accounts, mgr):
    # add A, edit A, then undo both and redo both. A surviving higher-id row
    # keeps SQLite from re-issuing A's old rowid, so the redo of the ADD gives A
    # a genuinely FRESH id; the redo of the EDIT must still find it (through the
    # manager's id remap) and re-apply. This is the case a naive stack breaks on.
    checking, _ = accounts
    tid = ledger.add_transaction(conn, checking, "2026-01-05", -25_00, payee="Safeway")
    survivor = ledger.add_transaction(conn, checking, "2026-01-06", -1_00, payee="Keep")
    mgr.record_add([tid])
    before = mgr.capture(tid)
    ledger.update_transaction(conn, tid, payee="Costco", amount=-30_00)
    mgr.record_edit(tid, before)

    assert mgr.undo() is True       # undo edit
    assert mgr.undo() is True       # undo add (deletes A; `survivor` keeps max id)
    assert _count(conn, checking) == 1

    assert mgr.redo() is True       # redo add -> A comes back under a NEW id
    assert mgr.redo() is True       # redo edit -> resolves the new id, re-applies
    new = [r for r in _rows(conn, checking) if r["id"] != survivor]
    assert len(new) == 1
    assert new[0]["id"] != tid      # proof the row was genuinely recreated
    assert new[0]["payee"] == "Costco"
    assert new[0]["amount"] == -30_00


# ---- a new edit clears the redo stack --------------------------------------
def test_new_edit_clears_redo_stack(conn, accounts, mgr):
    checking, _ = accounts
    tid = ledger.add_transaction(conn, checking, "2026-01-05", -25_00, payee="A")
    before1 = mgr.capture(tid)
    ledger.update_transaction(conn, tid, payee="B")
    mgr.record_edit(tid, before1)

    assert mgr.undo() is True
    assert mgr.can_redo()               # the edit is now redoable

    # A brand-new edit must throw the redo stack away.
    before2 = mgr.capture(tid)
    ledger.update_transaction(conn, tid, memo="note")
    mgr.record_edit(tid, before2)
    assert not mgr.can_redo()
    assert mgr.can_undo()


def test_convert_to_transfer_is_a_barrier(conn, accounts, mgr):
    # A convert has no clean ledger inverse, so it is not pushed as an undo step;
    # it drops the redo stack instead of recording something it cannot reverse.
    checking, savings = accounts
    tid = ledger.add_transaction(conn, checking, "2026-01-05", -25_00, payee="A")
    mgr.record_add([tid])
    assert mgr.undo()                   # redo stack now holds the add
    assert mgr.redo()                   # and back
    before = mgr.capture(tid)
    ledger.convert_to_transfer(conn, tid, savings)
    mgr.record_edit(tid, before)        # structural change -> barrier
    assert not mgr.can_redo()


# ---- through the real RegisterModel chokepoints ----------------------------
@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _register_model(conn, account_id):
    from mammon.ui.models import RegisterModel
    return RegisterModel(conn, account_id)


def test_model_add_undo_redo(qapp, conn, accounts):
    checking, _ = accounts
    m = _register_model(conn, checking)
    assert not m.can_undo()

    m.add_from_values({"date": "2026-01-05", "payee": "Safeway", "payment": "25.00"})
    assert m.can_undo()
    assert m.undo_label() == "Add transaction"
    assert _count(conn, checking) == 1

    assert m.undo() is True
    assert _count(conn, checking) == 0
    assert not m.can_undo() and m.can_redo()

    assert m.redo() is True
    rows = _rows(conn, checking)
    assert len(rows) == 1
    assert rows[0]["payee"] == "Safeway"
    assert rows[0]["amount"] == -25_00


def test_model_delete_undo_redo(qapp, conn, accounts):
    checking, _ = accounts
    ledger.add_transaction(conn, checking, "2026-01-05", -25_00, payee="Safeway")
    m = _register_model(conn, checking)

    assert m.delete_row(0) is True             # row 0 is the only txn
    assert _count(conn, checking) == 0
    assert m.undo_label() == "Delete transaction"

    assert m.undo() is True
    rows = _rows(conn, checking)
    assert len(rows) == 1 and rows[0]["payee"] == "Safeway"

    assert m.redo() is True
    assert _count(conn, checking) == 0


def test_model_transfer_undo_redo_both_sides(qapp, conn, accounts):
    checking, savings = accounts
    m = _register_model(conn, checking)
    m.add_from_values({"date": "2026-03-01", "payee": "Move",
                       "category": "[Savings]", "payment": "300.00"})
    assert m.undo_label() == "Add transfer"
    assert _count(conn, checking) == 1 and _count(conn, savings) == 1

    assert m.undo() is True
    assert _count(conn, checking) == 0 and _count(conn, savings) == 0

    assert m.redo() is True
    crows, srows = _rows(conn, checking), _rows(conn, savings)
    assert len(crows) == 1 and len(srows) == 1
    assert crows[0]["amount"] == -300_00
    assert srows[0]["amount"] == 300_00
    c = ledger.get_transaction(conn, crows[0]["id"])
    s = ledger.get_transaction(conn, srows[0]["id"])
    assert c["transfer_pair_id"] == s["id"]
    assert s["transfer_pair_id"] == c["id"]

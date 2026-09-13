"""USER BUG (2026-09-11): 'I got the message, "a transfer can't be split" when I
tried to split a transfer from my Coinbase account to one of my checking
accounts. It needs to be split because I sold for 859.70 in the coinbase account
but only received 843.09. The rest needs categorized as a transaction fee.'

A transfer is one transaction whose money can move more than one way, so the
transfer is now a property of one split LINE rather than of the whole ROW: the
line carries what actually landed in the other account (843.09) and the
remaining lines take ordinary categories (16.61 of fee). The invariant these
tests pin down is that the counter-account row equals the transfer LINE, not the
row total, through every step of the life cycle -- create, split, re-split,
undo/redo and delete -- with no orphaned mirror left behind.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger, undo


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "split.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    """A crypto/exchange account and the checking account the sale lands in.
    Both open at zero so every balance below is the transactions alone."""
    coinbase = ledger.create_account(conn, "Coinbase", "crypto", opening_balance=0)
    checking = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    return coinbase, checking


@pytest.fixture
def fee_cat(conn):
    return ledger.resolve_category(conn, "Bank Charge:Transaction Fee")


def _sale(conn, coinbase, checking):
    """The transfer as the user first records it: the whole 859.70 moves."""
    return ledger.create_transfer(conn, coinbase, checking, "2026-03-04",
                                  859_70, payee="Coinbase")


def _leg_pairs(conn, txn_id):
    """The mirror ids the split lines point at, in line order. ledger.get_splits
    does not surface splits.transfer_pair_id, and that linkage is exactly what
    these tests are about."""
    return [r["transfer_pair_id"] for r in conn.execute(
        "SELECT transfer_pair_id FROM splits WHERE transaction_id=? ORDER BY id",
        (txn_id,)).fetchall()]


def _split_it(conn, txn_id, checking, fee_cat, landed=843_09, fee=16_61):
    ledger.set_splits(conn, txn_id, [
        {"transfer_account_id": checking, "amount": -landed},
        {"category_id": fee_cat, "amount": -fee, "memo": "transaction fee"},
    ])


# ---- the ledger life cycle --------------------------------------------------
def test_transfer_splits_into_a_transfer_line_plus_a_fee(conn, accounts, fee_cat):
    coinbase, checking = accounts
    sale, mirror = _sale(conn, coinbase, checking)
    _split_it(conn, sale, checking, fee_cat)

    row = ledger.get_transaction(conn, sale)
    assert row["amount"] == -859_70                  # the row still totals the sale
    assert row["transfer_account_id"] is None        # the transfer moved to a line
    assert row["transfer_pair_id"] is None
    assert ledger.category_display(conn, row) == ledger.SPLIT_LABEL
    assert ledger.uncategorized_split_amount(conn, sale) == 0

    legs = [(l["category_label"], l["amount"]) for l in ledger.get_splits(conn, sale)]
    assert legs == [("[Checking]", -843_09),
                    ("Bank Charge:Transaction Fee", -16_61)]

    # The other side is the SAME row, re-amounted to the transfer LINE.
    other = ledger.get_transaction(conn, mirror)
    assert other["id"] == mirror
    assert other["amount"] == 843_09
    assert other["account_id"] == checking
    assert other["transfer_account_id"] == coinbase
    # A split leg's mirror is one-sided: the split row remembers it.
    assert other["transfer_pair_id"] is None
    assert _leg_pairs(conn, sale) == [mirror, None]

    assert ledger.account_balance(conn, checking) == 843_09
    assert ledger.account_balance(conn, coinbase) == -859_70
    # Exactly one row in checking -- nothing duplicated, nothing orphaned.
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                        (checking,)).fetchone()["c"] == 1


def test_adopted_mirror_keeps_its_reconcile_status(conn, accounts, fee_cat):
    """The counter row is adopted, not recreated, so a leg the user already
    reconciled against the checking statement is not silently dropped back to
    uncleared by splitting the other side."""
    coinbase, checking = accounts
    sale, mirror = _sale(conn, coinbase, checking)
    ledger.update_transaction(conn, mirror, cleared=1, reconciled=1)
    _split_it(conn, sale, checking, fee_cat)
    other = ledger.get_transaction(conn, mirror)
    assert (other["cleared"], other["reconciled"]) == (1, 1)
    assert other["amount"] == 843_09


def test_splitting_a_transfer_needs_a_line_back_to_the_counter_account(
        conn, accounts, fee_cat):
    coinbase, checking = accounts
    sale, mirror = _sale(conn, coinbase, checking)
    with pytest.raises(ValueError) as exc:
        ledger.set_splits(conn, sale, [
            {"category_id": fee_cat, "amount": -16_61},
            {"category_id": None, "amount": -843_09},
        ])
    assert "Checking" in str(exc.value)
    # Refused cleanly: the transfer is untouched on both sides.
    row = ledger.get_transaction(conn, sale)
    assert row["transfer_account_id"] == checking
    assert row["transfer_pair_id"] == mirror
    assert not ledger.has_splits(conn, sale)
    assert ledger.get_transaction(conn, mirror)["amount"] == 859_70


def test_editing_the_split_keeps_both_sides_consistent(conn, accounts, fee_cat):
    """Re-splitting an already-split transfer (the fee turned out to be 19.70)
    must move the counter-account row with it and leave no second deposit."""
    coinbase, checking = accounts
    sale, _mirror = _sale(conn, coinbase, checking)
    _split_it(conn, sale, checking, fee_cat)
    _split_it(conn, sale, checking, fee_cat, landed=840_00, fee=19_70)

    legs = ledger.get_splits(conn, sale)
    assert [(l["category_label"], l["amount"]) for l in legs] == [
        ("[Checking]", -840_00), ("Bank Charge:Transaction Fee", -19_70)]
    assert ledger.account_balance(conn, checking) == 840_00
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                        (checking,)).fetchone()["c"] == 1
    pair = _leg_pairs(conn, sale)[0]
    assert ledger.get_transaction(conn, pair)["amount"] == 840_00


def test_deleting_the_split_transfer_takes_the_mirror_with_it(conn, accounts, fee_cat):
    coinbase, checking = accounts
    sale, mirror = _sale(conn, coinbase, checking)
    _split_it(conn, sale, checking, fee_cat)
    ledger.delete_transaction(conn, sale)
    assert ledger.get_transaction(conn, sale) is None
    assert ledger.get_transaction(conn, mirror) is None
    assert ledger.account_balance(conn, checking) == 0
    assert conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"] == 0


def test_clear_splits_can_hand_the_transfer_back_to_the_row(conn, accounts, fee_cat):
    coinbase, checking = accounts
    sale, mirror = _sale(conn, coinbase, checking)
    _split_it(conn, sale, checking, fee_cat)
    ledger.clear_splits(conn, sale, restore_transfer_to=checking)

    row = ledger.get_transaction(conn, sale)
    assert row["transfer_account_id"] == checking
    assert row["transfer_pair_id"] == mirror        # the same mirror, re-linked
    assert not ledger.has_splits(conn, sale)
    other = ledger.get_transaction(conn, mirror)
    assert other["amount"] == 859_70                # back to the whole row total
    assert other["transfer_pair_id"] == sale
    assert ledger.account_balance(conn, checking) == 859_70


def test_clear_splits_without_restore_still_drops_the_legs(conn, accounts, fee_cat):
    """The default path is unchanged for every other caller (a loan payment's
    principal leg to [Mortgage] must NOT be promoted into a whole-row transfer)."""
    coinbase, checking = accounts
    sale, mirror = _sale(conn, coinbase, checking)
    _split_it(conn, sale, checking, fee_cat)
    ledger.clear_splits(conn, sale)
    row = ledger.get_transaction(conn, sale)
    assert row["transfer_account_id"] is None
    assert ledger.get_transaction(conn, mirror) is None
    assert ledger.account_balance(conn, checking) == 0


def test_splitting_a_one_sided_transfer_leg_fabricates_no_mirror(conn, accounts,
                                                                 fee_cat):
    """A one-sided transfer leg (an asymmetric imported row, or the mirror of
    someone else's split leg: transfer_account_id set, transfer_pair_id NULL)
    splits into a one-sided LEG. Inventing a counter row here would post money
    into the other account that never existed."""
    coinbase, checking = accounts
    txn = ledger.add_transaction(conn, coinbase, "2026-03-04", -859_70,
                                 payee="Coinbase")
    conn.execute("UPDATE transactions SET transfer_account_id=? WHERE id=?",
                 (checking, txn))
    conn.commit()
    _split_it(conn, txn, checking, fee_cat)
    legs = ledger.get_splits(conn, txn)
    assert [(l["category_label"], l["amount"]) for l in legs] == [
        ("[Checking]", -843_09), ("Bank Charge:Transaction Fee", -16_61)]
    assert _leg_pairs(conn, txn) == [None, None]
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                        (checking,)).fetchone()["c"] == 0


# ---- undo / redo ------------------------------------------------------------
def test_undo_restores_the_whole_transfer_then_redo_splits_again(conn, accounts,
                                                                 fee_cat):
    coinbase, checking = accounts
    sale, mirror = _sale(conn, coinbase, checking)
    mgr = undo.UndoManager(conn)
    before = mgr.capture(sale)
    _split_it(conn, sale, checking, fee_cat)
    mgr.record_edit(sale, before, label="Edit splits")
    # Moving a transfer into a split line is invertible, so it IS an undo step
    # (not a structural barrier).
    assert mgr.can_undo()

    assert mgr.undo() is True
    row = ledger.get_transaction(conn, sale)
    assert row["transfer_account_id"] == checking
    assert row["transfer_pair_id"] == mirror
    assert not ledger.has_splits(conn, sale)
    assert ledger.get_transaction(conn, mirror)["amount"] == 859_70
    assert ledger.account_balance(conn, checking) == 859_70
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                        (checking,)).fetchone()["c"] == 1

    assert mgr.redo() is True
    row = ledger.get_transaction(conn, sale)
    assert row["transfer_account_id"] is None
    legs = [(l["category_label"], l["amount"]) for l in ledger.get_splits(conn, sale)]
    assert legs == [("[Checking]", -843_09),
                    ("Bank Charge:Transaction Fee", -16_61)]
    assert ledger.account_balance(conn, checking) == 843_09
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                        (checking,)).fetchone()["c"] == 1


def test_undo_of_a_deleted_split_transfer_leaves_no_orphan(conn, accounts, fee_cat):
    coinbase, checking = accounts
    sale, _mirror = _sale(conn, coinbase, checking)
    _split_it(conn, sale, checking, fee_cat)
    mgr = undo.UndoManager(conn)
    snaps = mgr.capture_many([sale])
    ledger.delete_transaction(conn, sale)
    mgr.push_delete(snaps)
    assert mgr.undo() is True
    new_id = mgr.resolve(sale)
    row = ledger.get_transaction(conn, new_id)
    assert row["amount"] == -859_70
    legs = [(l["category_label"], l["amount"]) for l in ledger.get_splits(conn, new_id)]
    assert legs == [("[Checking]", -843_09),
                    ("Bank Charge:Transaction Fee", -16_61)]
    assert ledger.account_balance(conn, checking) == 843_09
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=?",
                        (checking,)).fetchone()["c"] == 1


# ---- the register path the user actually takes ------------------------------
@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_register_splits_a_transfer_end_to_end(qapp, conn, accounts, fee_cat,
                                               monkeypatch):
    """The whole user gesture: open the split on a transfer row (it must not be
    refused), correct the seeded [Checking] line down to what landed, categorize
    the fee, save."""
    from mammon.ui import widgets
    from mammon.ui.widgets import RegisterWidget, SplitDialog
    from PyQt5.QtWidgets import QDialog

    coinbase, checking = accounts
    sale, mirror = _sale(conn, coinbase, checking)

    w = RegisterWidget(conn, coinbase)
    infos: list = []
    monkeypatch.setattr(widgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: infos.append(a)))
    monkeypatch.setattr(widgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: infos.append(a)))
    monkeypatch.setattr(SplitDialog, "exec_", lambda self: QDialog.Rejected)
    w._split_row(w.model.row_for_txn(sale))
    assert infos == []                          # opened, not "A transfer cannot be split."

    dlg = SplitDialog(w.model, w.model.row_for_txn(sale))
    # Line 1 is seeded with the transfer itself at the full amount, so the user
    # only has to correct it down to what actually arrived.
    assert dlg._lines[0]["cat"].currentText() == "[Checking]"
    assert round(dlg._lines[0]["amount"].value(), 2) == -859.70
    dlg._lines[0]["amount"].setValue(-843.09)
    for entry in list(dlg._lines[1:]):
        dlg._remove_line(entry)
    dlg.add_line("Bank Charge:Transaction Fee", -16.61, "transaction fee")
    dlg._update_remainder()
    assert dlg.remainder_cents() == 0
    assert dlg.apply_split() is True
    assert infos == []

    row = ledger.get_transaction(conn, sale)
    assert row["amount"] == -859_70
    assert ledger.category_display(conn, row) == ledger.SPLIT_LABEL
    legs = [(l["category_label"], l["amount"]) for l in ledger.get_splits(conn, sale)]
    assert legs == [("[Checking]", -843_09),
                    ("Bank Charge:Transaction Fee", -16_61)]
    other = ledger.get_transaction(conn, mirror)
    assert other["amount"] == 843_09 and other["transfer_account_id"] == coinbase
    assert ledger.account_balance(conn, checking) == 843_09

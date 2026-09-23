"""Register column chooser, batch edits, void, and find-and-replace (parity
roadmap, item 2, stages 2-4).

Locked in here:

* the column chooser is a preference layered UNDER what the layout hides, so a
  loan register's dropped Num/Tag and two-line mode's collapsed
  Category/Memo/Tag are never un-hidden by it;
* a batch edit resolves its transaction ids BEFORE writing (rows shift on
  reload), skips what it cannot change (a split's category, a transfer's
  category, a reconciled row's cleared flag) and reports the skips;
* Void keeps the row as a record: amount zero on both legs of a transfer,
  the **VOID** mark on the payee, the original amount noted in the memo;
* Replace sets the WHOLE field (Quicken's semantics), skips transfers and
  splits for a category, and tells the main window to reload registers.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtWidgets import QApplication, QMessageBox

from mammon import categorize, db, ledger
from mammon.ui import prefs, widgets
from mammon.ui.models import RegisterModel
from mammon.ui.widgets import RegisterWidget, SearchDialog
from mammon.tests import fresh_db

R = RegisterModel


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    prefs.set_hidden_columns([])
    yield
    prefs.set_hidden_columns([])


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "batch.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return chk, sav


@pytest.fixture
def rows(conn, accounts):
    """Five checking rows: two plain, one reconciled, one transfer, one split."""
    chk, sav = accounts
    rent = ledger.resolve_category(conn, "Housing:Rent")
    a = ledger.add_transaction(conn, chk, "2026-01-05", -500_00, payee="Rent Co",
                               category_id=rent)
    b = ledger.add_transaction(conn, chk, "2026-01-06", -20_00, payee="Cafe", memo="latte")
    c = ledger.add_transaction(conn, chk, "2026-01-07", -30_00, payee="Books",
                               cleared=1, reconciled=1)
    d, _mirror = ledger.create_transfer(conn, chk, sav, "2026-01-08", 100_00, payee="Stash")
    e = ledger.add_transaction(conn, chk, "2026-01-09", -50_00, payee="Big Box")
    g = ledger.resolve_category(conn, "Groceries")
    h = ledger.resolve_category(conn, "Household")
    ledger.set_splits(conn, e, [(g, -30_00, ""), (h, -20_00, "")])
    return {"rent": a, "cafe": b, "books": c, "xfer": d, "split": e, "rent_cat": rent}


def _txn(conn, tid):
    return ledger.get_transaction(conn, tid)


# ---------------------------------------------------------------------------
# column chooser
# ---------------------------------------------------------------------------
def test_hidden_columns_preference_round_trips():
    assert prefs.hidden_columns() == []
    prefs.set_hidden_columns(["Tag", "Balance", "Tag", " "])
    assert prefs.hidden_columns() == ["Tag", "Balance"]


def test_column_chooser_hides_and_shows_and_persists(qapp, conn, accounts, rows):
    chk, _ = accounts
    w = RegisterWidget(conn, chk)
    assert not w.view.isColumnHidden(R.TAG)
    w.set_column_hidden(R.TAG, True)
    w.set_column_hidden(R.BALANCE, True)
    assert w.view.isColumnHidden(R.TAG) and w.view.isColumnHidden(R.BALANCE)
    assert w.user_hidden_columns() == {R.TAG, R.BALANCE}
    # Not a hideable column: ignored.
    w.set_column_hidden(R.PAYEE, True)
    assert not w.view.isColumnHidden(R.PAYEE)
    # A second register picks the preference up.
    w2 = RegisterWidget(conn, chk)
    assert w2.view.isColumnHidden(R.TAG) and w2.view.isColumnHidden(R.BALANCE)
    assert not w2.view.isColumnHidden(R.MEMO)
    w2.set_column_hidden(R.TAG, False)
    assert not w2.view.isColumnHidden(R.TAG)
    assert prefs.hidden_columns() == ["Balance"]
    w.deleteLater()
    w2.deleteLater()


def test_layout_hiding_is_layered_over_the_chooser(qapp, conn, accounts, rows):
    chk, _ = accounts
    w = RegisterWidget(conn, chk)
    w.set_view_mode("two")
    for col in (R.CATEGORY, R.MEMO, R.TAG):
        assert w.view.isColumnHidden(col)
    w.set_column_hidden(R.MEMO, False)          # the chooser cannot un-collapse it
    assert w.view.isColumnHidden(R.MEMO)
    w.set_view_mode("one")
    assert not w.view.isColumnHidden(R.MEMO)
    w.deleteLater()


# ---------------------------------------------------------------------------
# batch edits through the model
# ---------------------------------------------------------------------------
def test_batch_category_skips_transfer_and_split_and_teaches(qapp, conn, accounts, rows):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    ids = [rows["rent"], rows["cafe"], rows["xfer"], rows["split"]]
    changed, skipped = m.batch_set_category(ids, "Dining:Coffee")
    assert (changed, skipped) == (2, 2)
    coffee = ledger.resolve_category(conn, "Dining:Coffee")
    assert _txn(conn, rows["rent"])["category_id"] == coffee
    assert _txn(conn, rows["cafe"])["category_id"] == coffee
    assert _txn(conn, rows["xfer"])["transfer_account_id"] is not None     # untouched
    assert ledger.has_splits(conn, rows["split"])                             # untouched
    assert categorize.suggest_category(conn, "Cafe") == coffee               # taught


def test_batch_category_can_make_transfers(qapp, conn, accounts, rows):
    chk, sav = accounts
    m = RegisterModel(conn, chk)
    m.category_choices()
    changed, skipped = m.batch_set_category([rows["cafe"], rows["split"]], "[Savings]")
    assert (changed, skipped) == (1, 1)
    t = _txn(conn, rows["cafe"])
    assert t["transfer_account_id"] == sav and t["transfer_pair_id"] is not None


def test_batch_field_and_cleared(qapp, conn, accounts, rows):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    ids = [rows["rent"], rows["cafe"], rows["books"]]
    assert m.batch_set_field(ids, "tag", "2026") == (3, 0)
    assert all(_txn(conn, t)["tag"] == "2026" for t in ids)
    assert m.batch_set_field(ids, "memo", "") == (3, 0)
    assert _txn(conn, rows["cafe"])["memo"] is None
    # Cleared: the reconciled row is skipped, an already-cleared row too.
    assert m.batch_set_cleared(ids, True) == (2, 1)
    assert _txn(conn, rows["rent"])["cleared"] == 1
    assert _txn(conn, rows["books"])["reconciled"] == 1
    assert m.batch_set_cleared(ids, True) == (0, 3)
    assert m.batch_set_cleared(ids, False) == (2, 1)
    with pytest.raises(ValueError):
        m.batch_set_field(ids, "amount", "1")


def test_batch_delete_and_transaction_ids_resolve_before_writing(qapp, conn, accounts, rows):
    chk, sav = accounts
    m = RegisterModel(conn, chk)
    display_rows = [m.row_for_txn(rows["cafe"]), m.row_for_txn(rows["xfer"])]
    ids = m.txn_ids_at(display_rows + [m.rowCount() - 1])     # the blank row is dropped
    assert ids == [rows["cafe"], rows["xfer"]]
    assert m.batch_delete(ids) == (2, 0)
    assert _txn(conn, rows["cafe"]) is None
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?",
                        (sav,)).fetchone()[0] == 0                 # mirror went too
    assert m.rowCount() == 3 + 1


# ---------------------------------------------------------------------------
# void
# ---------------------------------------------------------------------------
def test_void_zeroes_marks_and_notes_the_amount(conn, accounts, rows):
    chk, sav = accounts
    assert ledger.void_transaction(conn, rows["cafe"]) is True
    t = _txn(conn, rows["cafe"])
    assert t["amount"] == 0 and t["payee"] == "**VOID** Cafe"
    assert t["memo"] == "latte (voided; was 20.00)"
    assert ledger.is_voided(t)
    assert ledger.void_transaction(conn, rows["cafe"]) is False    # idempotent
    # A transfer voids on both legs; a split loses its lines first.
    assert ledger.void_transaction(conn, rows["xfer"]) is True
    legs = conn.execute("SELECT amount, payee FROM transactions WHERE payee LIKE '**VOID**%' "
                        "AND account_id IN (?, ?) AND date='2026-01-08'", (chk, sav)).fetchall()
    assert len(legs) == 2 and all(r["amount"] == 0 for r in legs)
    assert ledger.void_transaction(conn, rows["split"]) is True
    assert not ledger.has_splits(conn, rows["split"])
    assert _txn(conn, rows["split"])["amount"] == 0
    assert ledger.account_balance(conn, chk) == 1000_00 - 500_00 - 30_00


def test_void_from_the_register_confirms_first(qapp, conn, accounts, rows, monkeypatch):
    chk, _ = accounts
    w = RegisterWidget(conn, chk)
    row = w.model.row_for_txn(rows["cafe"])
    monkeypatch.setattr(widgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.No))
    w._void_row(row)
    assert _txn(conn, rows["cafe"])["amount"] == -20_00
    monkeypatch.setattr(widgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    w._void_row(row)
    assert _txn(conn, rows["cafe"])["amount"] == 0
    w.deleteLater()


# ---------------------------------------------------------------------------
# batch edits through the widget's seams
# ---------------------------------------------------------------------------
def test_widget_batch_actions_use_the_selection_and_report_skips(qapp, conn, accounts, rows,
                                                                 monkeypatch):
    chk, _ = accounts
    w = RegisterWidget(conn, chk)
    m = w.model
    notices = []
    monkeypatch.setattr(w, "_notify", lambda title, text: notices.append(text))
    monkeypatch.setattr(w, "_ask_batch_category", lambda title: "Dining:Coffee")
    monkeypatch.setattr(w, "_ask_batch_text", lambda title, label: "batch memo")
    monkeypatch.setattr(widgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    sm = w.view.selectionModel()
    for tid in (rows["rent"], rows["cafe"], rows["xfer"]):
        sm.select(m.index(m.row_for_txn(tid), 0), sm.Select | sm.Rows)
    sel = w._selected_rows()
    assert len(sel) == 3
    w._batch_category(sel)
    assert w.last_batch == ("Recategorized", 2, 1)
    assert notices and "1 skipped" in notices[-1]
    w._batch_field(sel, "memo", "memo")
    assert w.last_batch == ("Changed memo on", 3, 0)
    assert _txn(conn, rows["xfer"])["memo"] == "batch memo"
    w._batch_cleared(sel, True)
    assert w.last_batch == ("Marked cleared", 3, 0)
    w._void_rows(sel)
    assert w.last_batch == ("Voided", 3, 0)
    assert _txn(conn, rows["rent"])["amount"] == 0
    # The multi-row Delete button deletes the selection (both transfer legs).
    # Every batch write reloads the model, which clears the selection, so the
    # user (and this test) re-selects before the next act.
    for tid in (rows["rent"], rows["cafe"], rows["xfer"]):
        sm.select(m.index(m.row_for_txn(tid), 0), sm.Select | sm.Rows)
    sel = w._selected_rows()
    assert len(sel) == 3
    w.on_delete()
    assert w.last_batch == ("Deleted", 3, 0)
    assert _txn(conn, rows["rent"]) is None
    w.deleteLater()


# ---------------------------------------------------------------------------
# find and replace
# ---------------------------------------------------------------------------
def test_replace_field_semantics(conn, accounts, rows):
    ids = [rows["rent"], rows["cafe"], rows["xfer"], rows["split"]]
    assert ledger.replace_field(conn, ids, "payee", "Vendor") == (4, 0)
    assert _txn(conn, rows["xfer"])["payee"] == "Vendor"
    mirror = conn.execute("SELECT payee FROM transactions WHERE transfer_pair_id=?",
                          (rows["xfer"],)).fetchone()
    assert mirror["payee"] == "Vendor"                       # mirrored to the other leg
    assert ledger.replace_field(conn, ids, "category", "Misc") == (2, 2)
    misc = ledger.resolve_category(conn, "Misc")
    assert _txn(conn, rows["rent"])["category_id"] == misc
    assert ledger.replace_field(conn, ids, "memo", "") == (4, 0)
    assert _txn(conn, rows["cafe"])["memo"] is None
    with pytest.raises(ValueError):
        ledger.replace_field(conn, ids, "amount", "0")


def test_search_dialog_replace_selected_and_all(qapp, conn, accounts, rows, monkeypatch):
    chk, _ = accounts
    dlg = SearchDialog(conn, default_account_id=None)
    monkeypatch.setattr(widgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    fired = []
    dlg.changed.connect(lambda: fired.append(True))
    dlg.query.setText("Cafe")
    hits = dlg.run_search()
    assert len(hits) == 1
    # Nothing highlighted: Replace Selected refuses politely.
    dlg.replace_with.setText("Coffee Shop")
    assert dlg.replace(all_results=False) == (0, 0)
    assert "select a result" in dlg.status.text()
    dlg.view.selectRow(0)
    assert dlg.replace(all_results=False) == (1, 0)
    assert _txn(conn, rows["cafe"])["payee"] == "Coffee Shop"
    assert fired == [True]
    # Replace All over a broader search, category form, skips the transfer/split.
    dlg.query.setText("2026-01")
    hits = dlg.run_search()
    assert len(hits) >= 5
    dlg.replace_field.setCurrentIndex(dlg.replace_field.findData("category"))
    dlg.replace_with.setText("Misc")
    changed, skipped = dlg.replace(all_results=True)
    assert changed >= 3 and skipped >= 2
    assert "skipped" in dlg.status.text()
    misc = ledger.resolve_category(conn, "Misc")
    assert _txn(conn, rows["rent"])["category_id"] == misc
    assert categorize.suggest_category(conn, "Rent Co") == misc      # taught
    dlg.deleteLater()

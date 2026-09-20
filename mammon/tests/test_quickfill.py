"""QuickFill (parity roadmap, item 1): the register completes a payee from its
own history and pre-enters that payee's last category, memo, tag and amount
into a NEW transaction -- Quicken's memorized-payee behaviour, derived from the
ledger itself rather than a separately maintained list.

Three things are locked in here and were each wrong before:

* the payee cell had NO completer at all (``ledger.list_payees`` read a table
  nothing writes, and nothing called it);
* the blank row commits the moment a date and an amount are both present, so a
  pre-entered amount must live apart from what the user typed or the row would
  record itself the instant a known payee was entered;
* Enter is the gesture that records a pre-entered row, and it must work from
  inside ANY cell editor -- including the category combo, which swallows the
  key before the view sees it -- so the hook is the delegate's close hint.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent, Qt
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import (QAbstractItemDelegate, QApplication, QLineEdit,
                             QStyleOptionViewItem)

from mammon import categorize, db, ledger
from mammon.ui.delegates import (PayeeCompleter, _accept_active_completion,
                                 payee_completions)
from mammon.ui.models import RegisterModel, fmt_cents
from mammon.ui.widgets import RegisterWidget, TransactionDialog
from mammon.tests import fresh_db


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "quickfill.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=100_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return chk, sav


@pytest.fixture
def history(conn, accounts):
    """A small register with a recurring payee whose latest checking row is the
    one QuickFill should recall (newer than the older checking row, older than
    a row for the same payee in ANOTHER account)."""
    chk, sav = accounts
    rent = ledger.resolve_category(conn, "Housing:Rent")
    salary = ledger.resolve_category(conn, "Income:Salary")
    ledger.add_transaction(conn, chk, "2026-01-01", -500_00, payee="Rent Co",
                           memo="January rent", tag="home", category_id=rent)
    ledger.add_transaction(conn, chk, "2026-02-01", -520_00, payee="Rent Co",
                           memo="February rent", tag="home", category_id=rent)
    ledger.add_transaction(conn, sav, "2026-03-01", -999_00, payee="Rent Co",
                           memo="from savings", category_id=rent)
    ledger.add_transaction(conn, chk, "2026-01-15", 2000_00, payee="Paycheck",
                           category_id=salary)
    return {"rent": rent, "salary": salary}


def _blank(m):
    return m.rowCount() - 1


def _txt(m, row, col):
    return m.data(m.index(row, col), Qt.DisplayRole) or ""


# ---------------------------------------------------------------------------
# domain: what history remembers for a payee
# ---------------------------------------------------------------------------
def test_last_transaction_prefers_own_account_then_recency(conn, accounts, history):
    chk, sav = accounts
    own = ledger.last_transaction_for_payee(conn, "Rent Co", chk)
    assert own["amount"] == -520_00 and own["memo"] == "February rent"
    # From the other account's point of view its own (newest) row wins.
    theirs = ledger.last_transaction_for_payee(conn, "Rent Co", sav)
    assert theirs["amount"] == -999_00
    # With no account preference, plain recency decides.
    assert ledger.last_transaction_for_payee(conn, "Rent Co")["amount"] == -999_00
    assert ledger.last_transaction_for_payee(conn, "Nobody", chk) is None
    assert ledger.last_transaction_for_payee(conn, "", chk) is None


def test_last_transaction_matches_case_and_edges_insensitively(conn, accounts, history):
    chk, _ = accounts
    assert ledger.last_transaction_for_payee(conn, "  rent co ", chk)["amount"] == -520_00


def test_last_transaction_skips_scheduled_placeholders(conn, accounts, history):
    chk, _ = accounts
    tid = ledger.add_transaction(conn, chk, "2026-04-01", -530_00, payee="Rent Co",
                                 memo="pre-entered")
    conn.execute("UPDATE transactions SET scheduled=1 WHERE id=?", (tid,))
    conn.commit()
    # The placeholder is Mammon's guess, not the user's act: still February.
    assert ledger.last_transaction_for_payee(conn, "Rent Co", chk)["memo"] == "February rent"


def test_list_payees_most_recent_first_without_blanks(conn, accounts, history):
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-05-01", -5_00, payee="   ")
    ledger.add_transaction(conn, chk, "2026-05-02", -5_00)
    assert ledger.list_payees(conn) == ["Rent Co", "Paycheck"]


def test_quickfill_recalls_shape_and_honours_a_user_override(conn, accounts, history):
    chk, _ = accounts
    fill = categorize.quickfill(conn, "Rent Co", chk)
    assert fill == {"amount": -520_00, "memo": "February rent", "tag": "home",
                    "category": "Housing:Rent"}
    # A category the user set for this payee outranks what the last row carried.
    other = ledger.resolve_category(conn, "Housing:Lease")
    categorize.record_user_categorization(conn, "Rent Co", other)
    assert categorize.quickfill(conn, "Rent Co", chk)["category"] == "Housing:Lease"
    assert categorize.quickfill(conn, "Nobody", chk) == {}


def test_quickfill_copies_a_transfer_but_not_a_split(conn, accounts, history):
    chk, sav = accounts
    ledger.create_transfer(conn, chk, sav, "2026-06-01", 250_00, payee="Stash")
    fill = categorize.quickfill(conn, "Stash", chk)
    assert fill["category"] == "[Savings]" and fill["amount"] == -250_00

    tid = ledger.add_transaction(conn, chk, "2026-06-02", -100_00, payee="Big Box")
    a = ledger.resolve_category(conn, "Groceries")
    b = ledger.resolve_category(conn, "Household")
    ledger.set_splits(conn, tid, [(a, -60_00, ""), (b, -40_00, "")])
    fill = categorize.quickfill(conn, "Big Box", chk)
    assert fill["amount"] == -100_00 and fill["category"] == ""   # split: user's call


# ---------------------------------------------------------------------------
# the register model: pre-enter, never overwrite, never self-commit
# ---------------------------------------------------------------------------
def test_blank_row_payee_pre_enters_but_does_not_commit(qapp, conn, accounts, history):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    rows_before, bal_before = m.rowCount(), ledger.account_balance(conn, chk)
    b = _blank(m)
    m.setData(m.index(b, RegisterModel.DATE), "2026-03-01", Qt.EditRole)
    m.setData(m.index(b, RegisterModel.PAYEE), "rent co", Qt.EditRole)
    assert _txt(m, b, RegisterModel.CATEGORY) == "Housing:Rent"
    assert _txt(m, b, RegisterModel.MEMO) == "February rent"
    assert _txt(m, b, RegisterModel.TAG) == "home"
    assert _txt(m, b, RegisterModel.PAYMENT) == fmt_cents(520_00)
    assert _txt(m, b, RegisterModel.DEPOSIT) == ""
    # Date + a pre-entered amount is NOT "date + money typed": nothing recorded.
    assert m.rowCount() == rows_before
    assert ledger.account_balance(conn, chk) == bal_before


def test_typed_fields_win_over_quickfill(qapp, conn, accounts, history):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    b = _blank(m)
    m.setData(m.index(b, RegisterModel.CATEGORY), "Utilities", Qt.EditRole)
    m.setData(m.index(b, RegisterModel.PAYEE), "Rent Co", Qt.EditRole)
    assert _txt(m, b, RegisterModel.CATEGORY) == "Utilities"
    assert _txt(m, b, RegisterModel.MEMO) == "February rent"   # untyped: filled
    # Clearing a pre-entered field keeps it cleared.
    m.setData(m.index(b, RegisterModel.MEMO), "", Qt.EditRole)
    assert _txt(m, b, RegisterModel.MEMO) == ""
    # A typed DEPOSIT drops the pre-entered payment so the two cannot net.
    m.setData(m.index(b, RegisterModel.DEPOSIT), "12.00", Qt.EditRole)
    assert m.blank_values().get("payment", "") == ""
    assert m.blank_values()["deposit"] == "12.00"


def test_typed_amount_commits_with_the_pre_entered_fields(qapp, conn, accounts, history):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    b = _blank(m)
    m.setData(m.index(b, RegisterModel.DATE), "2026-03-01", Qt.EditRole)
    m.setData(m.index(b, RegisterModel.PAYEE), "Rent Co", Qt.EditRole)
    m.setData(m.index(b, RegisterModel.PAYMENT), "525.00", Qt.EditRole)
    row = conn.execute(
        "SELECT * FROM transactions WHERE account_id=? AND date='2026-03-01'", (chk,)
    ).fetchone()
    assert row["amount"] == -525_00
    assert row["category_id"] == history["rent"]
    assert row["memo"] == "February rent" and row["tag"] == "home"
    assert m.blank_values() == {}                    # buffer cleared by the reload


def test_commit_blank_records_the_pre_entered_amount(qapp, conn, accounts, history):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    b = _blank(m)
    assert m.commit_blank() is False                 # nothing typed: not ready
    m.setData(m.index(b, RegisterModel.PAYEE), "Rent Co", Qt.EditRole)
    assert m.commit_blank() is False                 # no date yet
    m.setData(m.index(b, RegisterModel.DATE), "2026-03-01", Qt.EditRole)
    assert m.commit_blank() is True
    row = conn.execute(
        "SELECT amount, memo FROM transactions WHERE account_id=? AND date='2026-03-01'",
        (chk,)).fetchone()
    assert row["amount"] == -520_00 and row["memo"] == "February rent"
    assert m.commit_blank() is False                 # already recorded: no duplicate


def test_changing_the_payee_reguesses_but_keeps_typed_fields(qapp, conn, accounts, history):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    b = _blank(m)
    m.setData(m.index(b, RegisterModel.PAYEE), "Rent Co", Qt.EditRole)
    m.setData(m.index(b, RegisterModel.MEMO), "my memo", Qt.EditRole)
    m.setData(m.index(b, RegisterModel.PAYEE), "Paycheck", Qt.EditRole)
    assert _txt(m, b, RegisterModel.CATEGORY) == "Income:Salary"
    assert _txt(m, b, RegisterModel.DEPOSIT) == fmt_cents(2000_00)
    assert _txt(m, b, RegisterModel.PAYMENT) == ""    # the old guess is gone
    assert _txt(m, b, RegisterModel.MEMO) == "my memo"
    m.setData(m.index(b, RegisterModel.PAYEE), "Nobody", Qt.EditRole)
    assert _txt(m, b, RegisterModel.CATEGORY) == ""
    assert _txt(m, b, RegisterModel.MEMO) == "my memo"


def test_pre_entered_transfer_commits_as_a_mirrored_transfer(qapp, conn, accounts, history):
    chk, sav = accounts
    ledger.create_transfer(conn, chk, sav, "2026-06-01", 250_00, payee="Stash")
    m = RegisterModel(conn, chk)
    b = _blank(m)
    m.setData(m.index(b, RegisterModel.DATE), "2026-07-01", Qt.EditRole)
    m.setData(m.index(b, RegisterModel.PAYEE), "Stash", Qt.EditRole)
    assert _txt(m, b, RegisterModel.CATEGORY) == "[Savings]"
    assert m.commit_blank() is True
    legs = conn.execute(
        "SELECT account_id, amount FROM transactions WHERE date='2026-07-01' "
        "ORDER BY account_id").fetchall()
    assert [(r["account_id"], r["amount"]) for r in legs] == [(chk, -250_00), (sav, 250_00)]


def test_payee_choices_are_cached_per_reload(qapp, conn, accounts, history):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    assert m.payee_choices() == ["Rent Co", "Paycheck"]
    ledger.add_transaction(conn, chk, "2026-08-01", -1_00, payee="Newcomer")
    assert m.payee_choices() == ["Rent Co", "Paycheck"]   # stale until reload
    m.reload()
    assert m.payee_choices()[0] == "Newcomer"


# ---------------------------------------------------------------------------
# the payee editor and its completer
# ---------------------------------------------------------------------------
def test_payee_completions_rank_prefix_then_substring_in_recency_order():
    choices = ["Costco Gas", "COSTCO WHSE #0001", "Safeway", "Coast Dental"]
    assert payee_completions("cos", choices) == ["Costco Gas", "COSTCO WHSE #0001"]
    assert payee_completions("way", choices) == ["Safeway"]
    assert payee_completions("co", choices) == [
        "Costco Gas", "COSTCO WHSE #0001", "Coast Dental"]
    assert payee_completions("", choices) == []
    assert payee_completions("zzz", choices) == []


def test_payee_editor_completes_from_history_and_accepts_on_tab(qapp, conn, accounts, history):
    chk, _ = accounts
    w = RegisterWidget(conn, chk)
    idx = w.model.index(_blank(w.model), RegisterModel.PAYEE)
    editor = w.payee_delegate.createEditor(w.view, QStyleOptionViewItem(), idx)
    assert isinstance(editor, QLineEdit)
    completer = editor.completer()
    assert isinstance(completer, PayeeCompleter)
    completer.setCompletionPrefix("ren")
    assert completer.currentCompletion() == "Rent Co"
    # What the Tab-out path does: accept the completion that extends the typed text.
    editor.setText("ren")
    _accept_active_completion(editor)
    assert editor.text() == "Rent Co"
    # A substring-only match is offered in the popup but never silently accepted.
    editor.setText("check")
    completer.setCompletionPrefix("check")
    assert completer.currentCompletion() == "Paycheck"
    _accept_active_completion(editor)
    assert editor.text() == "check"
    w.deleteLater()


def test_enter_in_a_cell_editor_records_the_pre_entered_row(qapp, conn, accounts, history):
    chk, _ = accounts
    w = RegisterWidget(conn, chk)
    m = w.model
    b = _blank(m)
    m.setData(m.index(b, RegisterModel.DATE), "2026-03-01", Qt.EditRole)
    m.setData(m.index(b, RegisterModel.PAYEE), "Rent Co", Qt.EditRole)
    w.view.setCurrentIndex(m.index(b, RegisterModel.CATEGORY))
    w.view.selectRow(b)
    # A Tab-out (EditNextItem) is not the gesture...
    w._on_editor_closed(None, QAbstractItemDelegate.EditNextItem)
    QCoreApplication.processEvents()
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id=? AND date='2026-03-01'",
        (chk,)).fetchone()[0] == 0
    # ...Enter is, whatever editor it came from (the close hint says so).
    w._on_editor_closed(None, QAbstractItemDelegate.SubmitModelCache)
    QCoreApplication.processEvents()
    row = conn.execute(
        "SELECT amount, category_id FROM transactions "
        "WHERE account_id=? AND date='2026-03-01'", (chk,)).fetchone()
    assert row["amount"] == -520_00 and row["category_id"] == history["rent"]
    w.deleteLater()


def test_enter_on_the_view_records_the_pre_entered_row(qapp, conn, accounts, history):
    chk, _ = accounts
    w = RegisterWidget(conn, chk)
    m = w.model
    b = _blank(m)
    m.setData(m.index(b, RegisterModel.DATE), "2026-03-02", Qt.EditRole)
    m.setData(m.index(b, RegisterModel.PAYEE), "Paycheck", Qt.EditRole)
    w.view.setCurrentIndex(m.index(b, RegisterModel.PAYEE))
    w.view.selectRow(b)
    QApplication.sendEvent(
        w.view, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    QCoreApplication.processEvents()
    row = conn.execute(
        "SELECT amount FROM transactions WHERE date='2026-03-02'").fetchone()
    assert row["amount"] == 2000_00
    # A second Enter on the now-empty blank row is a harmless no-op.
    QApplication.sendEvent(
        w.view, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    QCoreApplication.processEvents()
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE date='2026-03-02'"
                        ).fetchone()[0] == 1
    w.deleteLater()


# ---------------------------------------------------------------------------
# the full-field dialog gets the same treatment
# ---------------------------------------------------------------------------
def test_transaction_dialog_quickfills_a_new_transaction_only(qapp, conn, accounts, history):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    dlg = TransactionDialog(m)
    assert isinstance(dlg.payee.completer(), PayeeCompleter)
    dlg.memo.setText("typed first")
    dlg.payee.setText("Rent Co")
    dlg.payee.editingFinished.emit()
    assert dlg.category.currentText() == "Housing:Rent"
    assert dlg.memo.text() == "typed first"             # never overwritten
    assert dlg.tag.text() == "home"
    assert dlg.payment.text() == fmt_cents(520_00) and dlg.deposit.text() == ""
    dlg.deleteLater()

    # Editing an EXISTING row: the payee field completes but fills nothing.
    edit = TransactionDialog(m, row=0)
    before = (edit.category.currentText(), edit.payment.text(), edit.memo.text())
    edit.payee.setText("Paycheck")
    edit.payee.editingFinished.emit()
    assert (edit.category.currentText(), edit.payment.text(), edit.memo.text()) == before
    edit.deleteLater()

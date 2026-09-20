"""Behavioural parity between the CRYPTO register and the cash register.

The crypto register must FEEL like the cash register -- a per-row context menu
with Edit/Delete, a blank quick-entry row at the bottom that records a brand-new
transaction, single-click editing with the same Tab/click focus behaviour, and a
review list that auto-renames the payee from learned corrections -- while keeping
the CONTENT differences a coin register genuinely needs (coin quantities as
Decimal text rather than USD cents; on a wallet, no price / amount / cash-balance
column, and a fee paid IN THE COIN).

This is a full life-cycle test, exercising the real register/model/review code:

  1. the BLANK entry row records a new wallet event through ``mammon.crypto``
     (the sole writer of ``crypto_*``) and it renders in the coin columns;
  2. the per-row CONTEXT MENU offers Edit and Delete and both work (Edit through
     the real :class:`CryptoTransactionDialog` -> ``crypto.update_event``, Delete
     -> ``crypto.delete_event``);
  3. field NAVIGATION matches the cash register -- identical edit triggers, a
     single click opens the editor, the coin/text cells use the same
     focus-select editor the cash register's Payee/Memo cells use;
  4. a crypto REVIEW row auto-renames the payee through the same ``rename_tree``
     the cash review uses (learned only from genuine corrections).

Offscreen Qt; synthetic ANON data only -- placeholder 0x1111.../0x2222...
addresses, no real wallet address, hash or amount appears here.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QAbstractItemView, QDialog, QMessageBox,
                             QStyleOptionViewItem)

import mammon.ui.widgets as W
from mammon import crypto, db, import_review, ledger, rename_tree
from mammon.ui.delegates import (DateDelegate, FocusSelectDelegate,
                                 _FocusSelectLineEdit)
from mammon.ui.models import CryptoRegisterModel, RegisterModel
from mammon.ui.widgets import (CryptoRegisterWidget, CryptoTransactionDialog,
                               RegisterWidget)
from mammon.tests import fresh_db

WALLET_ADDR = "0x1111111111111111111111111111111111111111"
COUNTERPARTY = "0x2222222222222222222222222222222222222222"
RECIPIENT = "0x3333333333333333333333333333333333333333"


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    # review_visibility()/prefs read QSettings; keep it out of the real profile.
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "parity.db")
    yield c
    c.close()


@pytest.fixture
def wallet(conn):
    aid = crypto.create_account(conn, "Paper Wallet",
                                kind=crypto.CRYPTO_KIND_WALLET)
    conn.execute("UPDATE accounts SET account_number=? WHERE id=?",
                 (WALLET_ADDR, aid))
    conn.commit()
    return aid


# ---- small helpers over a CryptoRegisterModel ---------------------------
def _cols(m):
    return [m.headerData(c, Qt.Horizontal) for c in range(m.columnCount())]


def _set(m, row, name, value):
    cols = _cols(m)
    return m.setData(m.index(row, cols.index(name)), value)


def _cell(m, row, name):
    cols = _cols(m)
    return m.data(m.index(row, cols.index(name)))


def _example_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM rename_examples WHERE kind='payee'").fetchone()[0]


def _crypto_mapped(txh, *, direction="in", memo=""):
    """A crypto MappedRow the way the importer builds one off a chain export."""
    rec = SimpleNamespace(
        date="2024-03-01", direction=direction,
        from_addr=COUNTERPARTY, to_addr=RECIPIENT, symbol="ETH", quantity="2",
        price="", fee_symbol="", fee_quantity="", memo=memo, tx_hash=txh)
    return import_review.mapped_from_crypto_record(rec)


# ---------------------------------------------------------------------------
# 1. the blank quick-entry row records a new wallet event through mammon.crypto
# ---------------------------------------------------------------------------
def test_blank_row_records_new_wallet_transactions_through_crypto(qapp, conn, wallet):
    """The cash register's manual-entry gesture -- a blank row at the bottom --
    now exists on the crypto register and posts a real coin-native event through
    mammon.crypto, never the cash `transactions` table."""
    reg = CryptoRegisterWidget(conn, wallet)
    try:
        m = reg.model
        # A blank quick-entry row sits at the bottom, exactly as in the cash one.
        blank = m.blank_row()
        assert m.is_blank_row(blank)
        assert blank == m.rowCount() - 1

        # RECEIVE 2 ETH from a counterparty -- committed through the model.
        assert _set(m, blank, "Date", "2024-03-01")
        assert _set(m, blank, "Coin / Wallet", "ETH")
        assert _set(m, blank, "Payee", COUNTERPARTY)
        assert _set(m, blank, "Coin In", "2")
        assert m.commit_blank() is True

        # SEND 0.5 ETH -- committed through the WIDGET's Enter path helper, so the
        # register's own commit route is exercised too.
        blank = m.blank_row()
        assert _set(m, blank, "Date", "2024-03-02")
        assert _set(m, blank, "Coin / Wallet", "ETH")
        assert _set(m, blank, "Payee", RECIPIENT)
        assert _set(m, blank, "Coin Out", "0.5")
        reg._commit_blank_row()
    finally:
        reg.deleteLater()

    rows = conn.execute(
        "SELECT action, symbol, quantity, payee FROM crypto_transactions "
        "WHERE account_id=? ORDER BY date", (wallet,)).fetchall()
    assert [r["action"] for r in rows] == ["RECEIVE", "SEND"]
    assert Decimal(rows[0]["quantity"]) == Decimal("2")
    # A send is stored as a negative (coin OUT), the crypto layer's convention.
    assert Decimal(rows[1]["quantity"]) == Decimal("-0.5")
    assert rows[0]["payee"] == COUNTERPARTY and rows[1]["payee"] == RECIPIENT
    # A wallet has NO fiat sleeve, so nothing landed in the cash `transactions`.
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    # The register renders them in the coin IN/OUT columns -- the content
    # difference a wallet requires -- not a signed USD amount column.
    m2 = CryptoRegisterModel(conn, wallet)
    assert Decimal(_cell(m2, 0, "Coin In")) == Decimal("2")
    assert _cell(m2, 0, "Coin Out") == ""
    assert Decimal(_cell(m2, 1, "Coin Out")) == Decimal("0.5")
    assert _cell(m2, 1, "Coin In") == ""


def test_blank_row_is_not_ready_until_it_has_a_coin_and_a_quantity(qapp, conn, wallet):
    """A half-typed multi-field crypto event must not post itself the way a
    two-field cash row can: commit_blank does nothing until the row is complete."""
    m = CryptoRegisterModel(conn, wallet)
    _set(m, m.blank_row(), "Date", "2024-03-01")
    assert m.commit_blank() is False                # date alone -> not ready
    _set(m, m.blank_row(), "Coin / Wallet", "ETH")
    assert m.commit_blank() is False                # still no quantity
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 2. the per-row context menu offers Edit and Delete, and both work
# ---------------------------------------------------------------------------
def test_context_menu_offers_edit_and_delete_and_both_work(qapp, conn, wallet,
                                                           monkeypatch):
    """The reported gap: the crypto register had no context menu at all. It now
    offers New / Edit / Delete like the cash register, and Edit/Delete route
    through mammon.crypto (update_event / delete_event)."""
    crypto.record_wallet_credit(conn, wallet, "2024-03-01", "ETH", "2",
                                payee=COUNTERPARTY, action="RECEIVE",
                                memo="original memo")
    crypto.rebuild_holdings(conn, wallet)
    reg = CryptoRegisterWidget(conn, wallet)
    try:
        m = reg.model
        txn_id = int(m.txn_at(0)["id"])

        # (a) the menu lists Edit and Delete on a real row.
        labels = []

        class _FakeMenu:
            def __init__(self, *a, **k):
                pass

            def addAction(self, text):
                labels.append(text)
                return SimpleNamespace(setEnabled=lambda *_: None)

            def addSeparator(self):
                pass

            def exec_(self, *a, **k):
                return None

        monkeypatch.setattr(W, "QMenu", _FakeMenu)
        reg.view.indexAt = lambda pos: m.index(0, 0)
        reg._context_menu(reg.view.rect().center())
        assert "Edit…" in labels
        assert "Delete" in labels
        assert "New…" in labels

        # (b) Edit through the REAL dialog -> crypto.update_event. Only exec_ is
        #     replaced (to avoid a modal); __init__ and values() are the real ones.
        class _AutoEdit(CryptoTransactionDialog):
            def exec_(self):
                self.memo_edit.setText("edited via dialog")
                self.qty_edit.setText("3")
                return QDialog.Accepted

        monkeypatch.setattr(W, "CryptoTransactionDialog", _AutoEdit)
        reg._edit_row(0)
        edited = conn.execute(
            "SELECT quantity, memo FROM crypto_transactions WHERE id=?",
            (txn_id,)).fetchone()
        assert Decimal(edited["quantity"]) == Decimal("3")
        assert edited["memo"] == "edited via dialog"

        # (c) Delete -> crypto.delete_event, after the confirm dialog.
        monkeypatch.setattr(W.QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Yes))
        reg._delete_row(0)
        assert conn.execute(
            "SELECT COUNT(*) FROM crypto_transactions WHERE account_id=?",
            (wallet,)).fetchone()[0] == 0
    finally:
        reg.deleteLater()


# ---------------------------------------------------------------------------
# 3. field navigation matches the cash register (tabs and clicks)
# ---------------------------------------------------------------------------
def test_field_navigation_matches_the_cash_register(qapp, conn, wallet):
    """The reported gap: the fields did not behave like the cash register's for
    tabs and clicks. Edit triggers are now identical, a single click opens the
    editor, and the coin/text cells use the same focus-select editor (Tab
    replaces, click appends) the cash register's Payee/Memo cells use."""
    cash_aid = ledger.create_account(conn, "Checking", "checking",
                                     opening_balance=0)
    cash = RegisterWidget(conn, cash_aid)
    reg = CryptoRegisterWidget(conn, wallet)
    try:
        # (a) IDENTICAL edit triggers -- keyboard only, no DoubleClicked (the
        #     crypto register used to require a double click to edit at all).
        assert reg.view.editTriggers() == cash.view.editTriggers()
        assert not (reg.view.editTriggers() & QAbstractItemView.DoubleClicked)

        m = reg.model
        cols = _cols(m)
        ci = cols.index("Coin In")

        # (b) The coin-quantity/text cells use the SAME focus-select editor the
        #     cash register's Payee/Memo cells use, and Date uses the calendar
        #     editor -- so Tab and click behave here exactly as they do there.
        assert isinstance(reg.view.itemDelegateForColumn(ci), FocusSelectDelegate)
        editor = reg.view.itemDelegateForColumn(ci).createEditor(
            reg.view.viewport(), QStyleOptionViewItem(),
            m.index(m.blank_row(), ci))
        assert isinstance(editor, _FocusSelectLineEdit)
        editor.deleteLater()
        assert isinstance(
            reg.view.itemDelegateForColumn(cols.index("Date")), DateDelegate)
        # The cash register uses the very same DateDelegate on its own Date cell.
        assert isinstance(
            cash.view.itemDelegateForColumn(RegisterModel.DATE), DateDelegate)

        # (c) A SINGLE click routes to view.edit() on an EDITABLE cell, and does
        #     NOTHING on a read-only derived cell -- the same rule the cash
        #     register applies (you cannot type into a running balance). The spy
        #     records the routing WITHOUT opening a live editor, so no editor is
        #     left mid-teardown (the documented setModelData heap hazard).
        opened = []
        reg.view.edit = lambda index, *a, **k: (opened.append(index.column()), True)[1]
        reg._on_cell_clicked(m.index(m.blank_row(), ci))
        assert ci in opened

        opened.clear()
        cb = cols.index("Coin Bal")
        assert not (m.flags(m.index(m.blank_row(), cb)) & Qt.ItemIsEditable)
        reg._on_cell_clicked(m.index(m.blank_row(), cb))
        assert opened == []
    finally:
        reg.deleteLater()
        cash.deleteLater()


# ---------------------------------------------------------------------------
# 4. a crypto review row auto-renames the payee through the same rename tree
# ---------------------------------------------------------------------------
def test_crypto_review_row_auto_renames_payee_like_the_cash_review(qapp, conn,
                                                                   wallet):
    """The reported gap: no automatic renaming of the payee in the crypto review
    list. It now runs the SAME rename tree the cash review uses -- the payee is
    auto-filled from learned corrections, learned only from genuine corrections,
    and the honest address stands until the user teaches it a name."""
    # Before any correction, the pending review line shows the raw address -- the
    # honest default, exactly as the cash review shows the bank's own text.
    m = CryptoRegisterModel(conn, wallet)
    m.set_pending(SimpleNamespace(mapped=_crypto_mapped("0xa")))
    assert _cell(m, m.pending_row(), "Payee") == COUNTERPARTY
    m.clear_pending()

    # The user renames the counterparty to "Coinbase" on two accepted rows -- the
    # SAME correction-driven learning the cash review does (>= MIN_FILL sightings).
    before = _example_count(conn)
    import_review.save_new(conn, wallet, _crypto_mapped("0xa"), payee="Coinbase")
    import_review.save_new(conn, wallet, _crypto_mapped("0xb"), payee="Coinbase")
    assert _example_count(conn) == before + 2       # both corrections learned

    # A NEW review row for the same address now auto-fills the friendly name in
    # the pending register line -- the renaming the crypto register lacked.
    assert import_review.predict_crypto_payee(conn, _crypto_mapped("0xc")) == "Coinbase"
    m2 = CryptoRegisterModel(conn, wallet)
    m2.set_pending(SimpleNamespace(mapped=_crypto_mapped("0xc")))
    assert _cell(m2, m2.pending_row(), "Payee") == "Coinbase"

    # It is literally the SAME rename tree the cash review consults (kind=payee):
    # a bare suggest() over the address returns the learned name.
    assert rename_tree.suggest(conn, COUNTERPARTY).payee == "Coinbase"

    # Accepting an address left as its own payee teaches nothing -- an address is
    # not its own rename -- so learning stays scoped to genuine corrections, the
    # same discipline the cash review keeps.
    count = _example_count(conn)
    import_review.save_new(conn, wallet, _crypto_mapped("0xd"))   # payee unchanged
    assert _example_count(conn) == count

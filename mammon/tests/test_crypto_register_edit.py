"""Every field a crypto row legitimately has is editable -- inline AND in the
Edit dialog -- and a mis-recorded DIRECTION can be flipped.

The reported bug: "I'm unable to edit many fields in the crypto register and
can't change SEND to RECIEVE even in the edit dialog. This should be possible."
Two gaps sat behind it:

  1. a POSTED crypto row was inline-editable in only a handful of cells (payee,
     memo, action, transfer, and an exchange's amount); date, coin, quantity,
     price and fee were read-only, so an import typo could not be fixed; and
  2. the Action list on a posted row was scoped to its CURRENT direction, so a
     SEND could never become a RECEIVE even through the Edit dialog.

This is a life-cycle test over the real register model, delegates and Edit
dialog. It edits every editable field and asserts the change PERSISTS through
:mod:`mammon.crypto` (the sole writer of ``crypto_*``), and it flips a row's
direction SEND <-> RECEIVE through both the inline Action cell and the Edit
dialog, asserting that the coin IN/OUT side and the payee's From/To meaning flip
with it. The only cells that stay read-only are the DERIVED running balances.

Offscreen Qt; synthetic ANON data only -- placeholder 0x1111.../0x2222...
addresses, no real wallet address, hash or amount appears here.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest
from PyQt5.QtCore import QDate, Qt

from mammon import crypto, db
from mammon.ui.models import CryptoRegisterModel
from mammon.ui.widgets import CryptoTransactionDialog

COUNTERPARTY = "0x2222222222222222222222222222222222222222"
RECIPIENT = "0x3333333333333333333333333333333333333333"


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "edit.db")
    yield c
    c.close()


@pytest.fixture
def wallet(conn):
    return crypto.create_account(conn, "Paper Wallet",
                                 kind=crypto.CRYPTO_KIND_WALLET)


@pytest.fixture
def exchange(conn):
    return crypto.create_account(conn, "Brokerage",
                                 kind=crypto.CRYPTO_KIND_EXCHANGE)


# ---- small helpers over a CryptoRegisterModel ---------------------------
def _idx(m, row, key):
    return m.index(row, m.column_index(key))


def _edit(m, row, key, value):
    """Edit one cell exactly as a delegate would -- assert it is editable, write
    through setData, then refresh the model the way the deferred reload does."""
    idx = _idx(m, row, key)
    assert m.flags(idx) & Qt.ItemIsEditable, f"column {key} should be editable"
    assert m.setData(idx, value, Qt.EditRole) is True
    m.reload()


def _not_editable(m, row, key):
    ci = m.column_index(key)
    if ci < 0:
        return                      # column absent in this account's layout
    assert not (m.flags(m.index(row, ci)) & Qt.ItemIsEditable)


def _cell(m, row, key):
    return m.data(_idx(m, row, key))


def _cell_tip(m, row, key):
    return m.data(_idx(m, row, key), Qt.ToolTipRole)


# ---------------------------------------------------------------------------
# 1. every field of a WALLET row is inline-editable and persists via crypto.py
# ---------------------------------------------------------------------------
def test_every_wallet_field_edits_inline_and_persists(qapp, conn, wallet):
    txn_id = crypto.record_wallet_credit(
        conn, wallet, "2024-03-01", "ETH", "2",
        payee=COUNTERPARTY, action="RECEIVE", memo="original")
    crypto.rebuild_holdings(conn, wallet)
    m = CryptoRegisterModel(conn, wallet)
    M = CryptoRegisterModel

    # The derived running balance, and the empty Coin OUT side of this credit
    # row, are the ONLY read-only cells -- everything else is correctable.
    _not_editable(m, 0, M.COIN_BAL)
    _not_editable(m, 0, M.COIN_OUT)

    _edit(m, 0, M.DATE, QDate(2024, 4, 15))
    _edit(m, 0, M.COIN, "weth")                 # upper-cased on the way in
    _edit(m, 0, M.PAYEE, "Alice")
    _edit(m, 0, M.MEMO, "a gift")
    _edit(m, 0, M.COIN_IN, "3.5")               # magnitude of the active side
    _edit(m, 0, M.FEE, "0.001 ETH")
    _edit(m, 0, M.ACTION, "REWARD")             # same direction, no flip

    row = crypto.get_event(conn, txn_id)
    assert row["date"] == "2024-04-15"
    assert row["symbol"] == "WETH"
    assert row["payee"] == "Alice"
    assert row["memo"] == "a gift"
    assert Decimal(row["quantity"]) == Decimal("3.5")   # still a credit (+)
    assert row["fee_symbol"] == "ETH"
    assert Decimal(row["fee_quantity"]) == Decimal("0.001")
    assert row["action"] == "REWARD"

    # The transfer LINK is a field too: naming another account links the row.
    cold = crypto.create_account(conn, "Cold Wallet",
                                 kind=crypto.CRYPTO_KIND_WALLET)
    _edit(m, 0, M.TRANSFER, "[Cold Wallet]")
    row = crypto.get_event(conn, txn_id)
    assert row["transfer_account_id"] == cold
    assert row["action"] == "TRANSFER_IN"       # a positive leg links as IN


# ---------------------------------------------------------------------------
# 2. every field of an EXCHANGE row is inline-editable and persists
# ---------------------------------------------------------------------------
def test_every_exchange_field_edits_inline_and_persists(qapp, conn, exchange):
    txn_id = crypto.record_buy(
        conn, exchange, "2024-01-01", "ETH", "2", 400000, price="2000",
        memo="original")
    crypto.rebuild_holdings(conn, exchange)
    m = CryptoRegisterModel(conn, exchange)
    M = CryptoRegisterModel

    # Only the two derived running balances are read-only on an exchange row.
    _not_editable(m, 0, M.COIN_BAL)
    _not_editable(m, 0, M.CASH_BAL)

    _edit(m, 0, M.DATE, QDate(2024, 2, 2))
    _edit(m, 0, M.COIN, "weth")
    _edit(m, 0, M.PAYEE, "Coinbase")
    _edit(m, 0, M.MEMO, "bought the dip")
    _edit(m, 0, M.QUANTITY, "3")                # magnitude; sign is the action's
    _edit(m, 0, M.PRICE, "2100")
    _edit(m, 0, M.AMOUNT, "6300")
    _edit(m, 0, M.FEE, "0.5 WETH")

    row = crypto.get_event(conn, txn_id)
    assert row["date"] == "2024-02-02"
    assert row["symbol"] == "WETH"
    assert row["payee"] == "Coinbase"
    assert row["memo"] == "bought the dip"
    assert Decimal(row["quantity"]) == Decimal("3")     # a buy stays positive
    assert Decimal(row["price"]) == Decimal("2100")
    assert row["amount"] == -630000                     # a buy is money OUT
    assert row["fee_symbol"] == "WETH"
    assert Decimal(row["fee_quantity"]) == Decimal("0.5")

    # Flipping the action reverses BOTH signed numbers: a buy (+coin, -cash)
    # becomes a sell (-coin, +cash), so an action-only edit is never left
    # inconsistent with the quantity and amount cells.
    _edit(m, 0, M.ACTION, "SELL")
    row = crypto.get_event(conn, txn_id)
    assert row["action"] == "SELL"
    assert Decimal(row["quantity"]) == Decimal("-3")
    assert row["amount"] == 630000


# ---------------------------------------------------------------------------
# 3. SEND <-> RECEIVE flips through the Edit DIALOG, coin side and payee source
#    following the direction
# ---------------------------------------------------------------------------
def _flip_via_dialog(qapp, m, row, new_action):
    """Drive the REAL CryptoTransactionDialog: only the action changes, then the
    row is written back through the model (crypto.update_event, the sole writer).
    exec_() is never called, so nothing blocks under the offscreen platform."""
    txn = m.txn_at(row)
    dlg = CryptoTransactionDialog(m, dict(txn), m.actions_for_row(row))
    try:
        i = dlg.action_combo.findText(new_action)
        assert i >= 0, f"{new_action} must be offered on a posted row"
        dlg.action_combo.setCurrentIndex(i)
        assert m.apply_edit(int(txn["id"]), dlg.values()) is True
    finally:
        dlg.deleteLater()


def test_flip_send_to_receive_and_back_via_dialog(qapp, conn, wallet):
    txn_id = crypto.record_wallet_debit(
        conn, wallet, "2024-03-02", "ETH", "0.5",
        payee=RECIPIENT, action="SEND")
    crypto.rebuild_holdings(conn, wallet)
    m = CryptoRegisterModel(conn, wallet)
    M = CryptoRegisterModel

    # Before: a SEND shows in Coin OUT, and its payee is the recipient (a "To").
    assert Decimal(_cell(m, 0, M.COIN_OUT)) == Decimal("0.5")
    assert _cell(m, 0, M.COIN_IN) == ""
    assert crypto.payee_role("SEND") == "to"
    assert _cell_tip(m, 0, M.PAYEE) == f"To: {RECIPIENT}"

    _flip_via_dialog(qapp, m, 0, "RECEIVE")

    row = crypto.get_event(conn, txn_id)
    assert row["action"] == "RECEIVE"
    assert Decimal(row["quantity"]) == Decimal("0.5")   # sign flipped to a credit
    assert row["payee"] == RECIPIENT                    # same counterparty string
    assert crypto.payee_role(row["action"]) == "from"   # now the sender (a "From")
    # The register renders the coin on the IN side now, and the OUT side empty.
    assert Decimal(_cell(m, 0, M.COIN_IN)) == Decimal("0.5")
    assert _cell(m, 0, M.COIN_OUT) == ""
    assert _cell_tip(m, 0, M.PAYEE) == f"From: {RECIPIENT}"

    # And back again -- the flip is reversible with no residue.
    _flip_via_dialog(qapp, m, 0, "SEND")
    row = crypto.get_event(conn, txn_id)
    assert row["action"] == "SEND"
    assert Decimal(row["quantity"]) == Decimal("-0.5")
    assert crypto.payee_role(row["action"]) == "to"
    assert Decimal(_cell(m, 0, M.COIN_OUT)) == Decimal("0.5")
    assert _cell(m, 0, M.COIN_IN) == ""
    assert _cell_tip(m, 0, M.PAYEE) == f"To: {RECIPIENT}"


# ---------------------------------------------------------------------------
# 4. the same flip works INLINE, through the Action cell's ChoiceDelegate path
# ---------------------------------------------------------------------------
def test_flip_send_to_receive_inline(qapp, conn, wallet):
    txn_id = crypto.record_wallet_debit(
        conn, wallet, "2024-03-02", "ETH", "0.75",
        payee=RECIPIENT, action="SEND")
    crypto.rebuild_holdings(conn, wallet)
    m = CryptoRegisterModel(conn, wallet)
    M = CryptoRegisterModel

    # RECEIVE must be offered on a SEND row now (it never was before the fix).
    assert "RECEIVE" in m.actions_for_row(0)

    _edit(m, 0, M.ACTION, "RECEIVE")
    row = crypto.get_event(conn, txn_id)
    assert row["action"] == "RECEIVE"
    assert Decimal(row["quantity"]) == Decimal("0.75")  # re-signed to a credit
    assert Decimal(_cell(m, 0, M.COIN_IN)) == Decimal("0.75")
    assert _cell(m, 0, M.COIN_OUT) == ""


# ---------------------------------------------------------------------------
# 4b. a bare cash movement flips DEPOSIT <-> WITHDRAW, its amount following
# ---------------------------------------------------------------------------
def test_flip_deposit_to_withdraw_reverses_the_amount(qapp, conn, exchange):
    txn_id = crypto.record_cash(conn, exchange, "2024-01-01", 100000,
                                action="DEPOSIT")
    crypto.rebuild_holdings(conn, exchange)
    m = CryptoRegisterModel(conn, exchange)
    M = CryptoRegisterModel

    assert crypto.get_event(conn, txn_id)["amount"] == 100000    # deposit: in
    assert "WITHDRAW" in m.actions_for_row(0)
    _edit(m, 0, M.ACTION, "WITHDRAW")
    row = crypto.get_event(conn, txn_id)
    assert row["action"] == "WITHDRAW"
    assert row["amount"] == -100000                              # now money out

    # And editing the amount on the withdrawal keeps it negative.
    _edit(m, 0, M.AMOUNT, "250")
    assert crypto.get_event(conn, txn_id)["amount"] == -25000


# ---------------------------------------------------------------------------
# 5. the Edit dialog reaches EVERY field, including the transfer link
# ---------------------------------------------------------------------------
def test_edit_dialog_writes_every_field_including_transfer(qapp, conn, wallet):
    txn_id = crypto.record_wallet_credit(
        conn, wallet, "2024-03-01", "ETH", "2",
        payee=COUNTERPARTY, action="RECEIVE", memo="original")
    crypto.rebuild_holdings(conn, wallet)
    cold = crypto.create_account(conn, "Cold Wallet",
                                 kind=crypto.CRYPTO_KIND_WALLET)
    m = CryptoRegisterModel(conn, wallet)

    txn = m.txn_at(0)
    dlg = CryptoTransactionDialog(m, dict(txn), m.actions_for_row(0))
    try:
        dlg.date_edit.setDate(QDate(2024, 5, 6))
        dlg.coin_edit.setText("BTC")
        dlg.qty_edit.setText("1.25")
        dlg.fee_edit.setText("0.002 ETH")
        dlg.payee_edit.setText("Bob")
        dlg.memo_edit.setText("edited via dialog")
        dlg.transfer_edit.setText("[Cold Wallet]")
        assert m.apply_edit(int(txn["id"]), dlg.values()) is True
    finally:
        dlg.deleteLater()

    row = crypto.get_event(conn, txn_id)
    assert row["date"] == "2024-05-06"
    assert row["symbol"] == "BTC"
    assert Decimal(row["quantity"]) == Decimal("1.25")
    assert row["fee_symbol"] == "ETH"
    assert Decimal(row["fee_quantity"]) == Decimal("0.002")
    assert row["payee"] == "Bob"
    assert row["memo"] == "edited via dialog"
    assert row["transfer_account_id"] == cold      # the link gesture applied too

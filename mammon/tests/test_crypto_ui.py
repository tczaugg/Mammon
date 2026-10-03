"""Headless UI tests for the crypto register + holdings views (the final crypto
phase). These exercise, under the offscreen Qt platform (no display needed):

  * that a ``type='crypto'`` account is GROUPED with investments in the sidebar
    grouping table (``ledger.INVESTMENT_LIKE_TYPES`` is the single source of
    truth the ``_BAR_GROUPS`` "Investing" section reads);
  * that :class:`CryptoHoldingsDialog` values a wallet's coins + cash to the same
    number the account list shows (``crypto.display_balance``);
  * that :class:`CryptoRegisterModel` renders the crypto event taxonomy -- a buy,
    a swap pair (the two ``swap_group_id`` legs shown as one paired trade), a
    wallet transfer (the mirror model, shown as ``[Other Wallet]``), and gas as a
    same-coin ``fee_*`` leg.

The UI stays a THIN projection: every number here is read through
``mammon.crypto`` / ``mammon.ledger``; the widgets hold no SQL and no money math.
Fixtures use synthetic ANON data only (no real wallet address, no PII).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest

from mammon import crypto, db, ledger
from mammon.crypto import Quote
from mammon.ui import widgets
from mammon.ui.models import (
    CryptoRegisterModel, fmt_cents, fmt_money, fmt_qty,
)
from mammon.ui.widgets import CryptoHoldingsDialog, CryptoRegisterWidget

from PyQt5.QtCore import Qt
from mammon.tests import fresh_db


# --------------------------------------------------------------------------
# fixtures (mirror the phase-3 crypto fixture trio)
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "crypto_ui.db")
    yield c
    c.close()


@pytest.fixture
def wallet(conn):
    # Fund a cash sleeve so a buy nets against real fiat.
    return crypto.create_account(conn, "Hot Wallet", opening_balance=1_000_000_00)


class _FakeSource:
    """The injected quote seam: given BARE coin symbols, returns Quotes already
    keyed by the '{SYM}-USD' pair -- exactly what CryptoQuoteSource yields."""
    source_name = "fake"

    def __init__(self, quotes):
        self._quotes = quotes

    def get_quotes(self, symbols):
        return self._quotes


def _cell(model, row, col):
    """One cell, addressed by column KEY.

    The keys are not positions. A wallet and an exchange show DIFFERENT columns,
    so ``model.column_index`` is the supported way to locate one -- the constants
    happened to equal their positions in the exchange layout until a Transfer
    column was added after Payee, and treating that coincidence as an API is what
    made these tests read the wrong cells."""
    return model.data(model.index(row, model.column_index(col)), Qt.DisplayRole)


def _row_where(model, **match):
    """Index of the first row whose stored event matches every field."""
    for i in range(model.rowCount()):
        t = model.txn_at(i)
        if all((t[k] == v) for k, v in match.items()):
            return i
    raise AssertionError(f"no register row matching {match}")


# --------------------------------------------------------------------------
# (1) grouping: crypto lives under the investments section
# --------------------------------------------------------------------------
def _group_for_type(acct_type):
    for title, types in widgets._BAR_GROUPS:
        if acct_type in types:
            return title
    return "Other"


def test_crypto_groups_under_investments(qapp):
    # The constant is the single source of truth for "is this investment-like".
    assert "crypto" in ledger.INVESTMENT_LIKE_TYPES
    # The sidebar grouping table (the one grouping site) places a crypto wallet
    # in the SAME "Investing" section as an equity brokerage.
    assert _group_for_type("crypto") == "Investing"
    assert _group_for_type("crypto") == _group_for_type("investment")


# --------------------------------------------------------------------------
# (2) holdings view: coin quantity + current valuation
# --------------------------------------------------------------------------
def test_crypto_holdings_view_values_coins_and_cash(qapp, conn, wallet):
    crypto.record_buy(conn, wallet, "2024-01-05", "ETH", "2", 4_000_00)
    crypto.rebuild_holdings(conn, wallet)
    crypto.fetch_quotes(
        conn, ["ETH"],
        source=_FakeSource([Quote("ETH-USD", "2024-06-01", "2500.00", "fake")]))

    dlg = CryptoHoldingsDialog(conn, wallet)

    # One priced coin position, valued through the shared {SYM}-USD price path.
    assert [h.symbol for h in dlg._held] == ["ETH"]
    eth = dlg._held[0]
    assert eth.quantity == Decimal("2")
    assert eth.market_value == 2 * 2500 * 100          # 2 ETH * $2500 -> cents
    assert eth.gain == eth.market_value - 4_000_00

    # The footer total is the SAME number the accounts list shows for this
    # account (crypto.display_balance), so the two cannot drift.
    assert dlg.valuation.total == crypto.display_balance(conn, wallet)
    assert fmt_money(dlg.valuation.total) in dlg.total_label.text()

    # The coin row renders quantity + market value; the cash sleeve is the last
    # row and carries the fiat balance.
    assert _hcell(dlg, 0, dlg.QUANTITY) == fmt_qty("2")
    assert _hcell(dlg, 0, dlg.MARKET) == fmt_cents(eth.market_value)
    cash_row = len(dlg._held)
    assert _hcell(dlg, cash_row, dlg.SYMBOL) == "Cash"
    assert _hcell(dlg, cash_row, dlg.MARKET) == fmt_cents(dlg.valuation.cash)


def _hcell(dlg, row, col):
    item = dlg.table.item(row, col)
    return item.text() if item is not None else None


def test_crypto_holdings_view_leaves_unpriced_coin_blank(qapp, conn, wallet):
    # No quote recorded -> the coin is unpriced: blank Price/Market/Gain, matching
    # the investments holdings window.
    crypto.record_buy(conn, wallet, "2024-01-05", "BTC", "1", 10_000_00)
    crypto.rebuild_holdings(conn, wallet)

    dlg = CryptoHoldingsDialog(conn, wallet)
    assert dlg._held[0].price is None
    assert _hcell(dlg, 0, dlg.PRICE) == ""
    assert _hcell(dlg, 0, dlg.MARKET) == ""
    assert _hcell(dlg, 0, dlg.GAIN) == ""


# --------------------------------------------------------------------------
# (3) register view: the crypto event taxonomy
# --------------------------------------------------------------------------
@pytest.fixture
def busy_wallet(conn, wallet):
    """A wallet exercising a buy, a swap pair, a wallet transfer and a gas fee."""
    crypto.record_buy(conn, wallet, "2024-01-05", "BTC", "1", 10_000_00)
    out_id, in_id = crypto.record_swap(
        conn, wallet, "2024-02-01", "BTC", "1", "ETH", "20", 12_000_00)
    cold = crypto.create_account(conn, "Cold Wallet")
    crypto.record_wallet_transfer(conn, wallet, cold, "2024-03-01", "ETH", "5")
    crypto.record_send(conn, wallet, "2024-04-01", "ETH", "1", 2_500_00,
                       fee_symbol="ETH", fee_quantity="0.01", fee_amount=25_00)
    return wallet, cold, out_id, in_id


def test_register_renders_buy(qapp, conn, busy_wallet):
    wallet, *_ = busy_wallet
    m = CryptoRegisterModel(conn, wallet)
    r = _row_where(m, action="BUY")
    assert _cell(m, r, m.ACTION) == "BUY"
    assert _cell(m, r, m.COIN) == "BTC"
    assert _cell(m, r, m.QUANTITY) == fmt_qty("1")
    assert _cell(m, r, m.COIN_BAL) == fmt_qty("1")
    # Fiat leaves the cash sleeve on a buy (amount<0).
    assert _cell(m, r, m.AMOUNT) == fmt_cents(-10_000_00)


def test_register_renders_swap_pair_as_paired(qapp, conn, busy_wallet):
    wallet, _cold, out_id, in_id = busy_wallet
    m = CryptoRegisterModel(conn, wallet)
    out_row = _row_where(m, action="SWAP_OUT")
    in_row = _row_where(m, action="SWAP_IN")
    # Both legs render the same OUT->IN pair label, so they read as one trade.
    assert _cell(m, out_row, m.COIN) == "BTC->ETH"
    assert _cell(m, in_row, m.COIN) == "BTC->ETH"
    # ...and share a swap_group_id (set to the OUT leg's id).
    assert m.txn_at(out_row)["swap_group_id"] == out_id
    assert m.txn_at(in_row)["swap_group_id"] == out_id
    # The OUT leg's quantity is stored signed (negative).
    assert _cell(m, out_row, m.QUANTITY) == fmt_qty("-1")


def test_register_renders_wallet_transfer_as_mirror(qapp, conn, busy_wallet):
    wallet, cold, *_ = busy_wallet
    m = CryptoRegisterModel(conn, wallet)
    out_row = _row_where(m, action="TRANSFER_OUT")
    # The other wallet is named in its OWN column, as a cash register names a
    # transfer account in the field after the payee. It used to be rendered in
    # the COIN column instead, which meant a transfer row could not say which
    # coin moved -- and left the register with no transfer field at all.
    assert _cell(m, out_row, m.TRANSFER) == "[Cold Wallet]"
    assert _cell(m, out_row, m.COIN) == "ETH"
    # The mirror leg lives in the OTHER account and points back here.
    mc = CryptoRegisterModel(conn, cold)
    in_row = _row_where(mc, action="TRANSFER_IN")
    assert _cell(mc, in_row, mc.TRANSFER) == "[Hot Wallet]"
    assert _cell(mc, in_row, mc.COIN) == "ETH"


def test_register_renders_gas_as_same_coin_fee_leg(qapp, conn, busy_wallet):
    wallet, *_ = busy_wallet
    m = CryptoRegisterModel(conn, wallet)
    send_row = _row_where(m, action="SEND")
    assert _cell(m, send_row, m.FEE) == "0.01 ETH"
    # The running coin balance folds in BOTH the send and its same-coin gas, so
    # the last ETH balance ties to the rebuilt holdings.
    holdings = {h["symbol"]: h for h in crypto.rebuild_holdings(conn, wallet)}
    assert _cell(m, send_row, m.COIN_BAL) == fmt_qty(holdings["ETH"]["quantity"])
    assert _cell(m, send_row, m.COIN_BAL) == fmt_qty("13.99")


# --------------------------------------------------------------------------
# the register WIDGET builds and is API-compatible for MainWindow's stack
# --------------------------------------------------------------------------
def test_crypto_register_widget_builds(qapp, conn, busy_wallet):
    wallet, *_ = busy_wallet
    w = CryptoRegisterWidget(conn, wallet)
    assert isinstance(w.model, CryptoRegisterModel)
    # buy + swap-out + swap-in + transfer-out + send (the transfer-IN leg lives
    # in the cold wallet, not here), PLUS the trailing blank quick-entry row that
    # brings manual entry to parity with the cash register.
    assert w.model.rowCount() == 6
    assert w.model.is_blank_row(w.model.rowCount() - 1)
    # No editor is open on first build (the blank row exists but is not editing).
    assert w.has_open_editor() is False
    # The valuation header reads through the crypto domain layer.
    assert "Total:" in w.valuation_label.text()
    assert w.header.text() == "Hot Wallet"
    # API-compatible no-ops the stack relies on.
    w.set_view_mode("two")
    w.apply_display_prefs("one")

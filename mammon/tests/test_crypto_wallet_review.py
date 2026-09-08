"""A crypto WALLET (a single paper-wallet address) is coin-native end to end.

This is the regression for the reported defect: an on-chain by-address export
imported into a wallet came out as CASH. It reached the generic delimited
importer, which knows only date/payee/amount columns, so it inferred the block
number as the money column, reviewed the rows with a cash header set, and the
register showed Price, Amount and Cash Bal -- three columns that have no meaning
for an address where no dollar ever moves. What a wallet actually has is a coin
quantity in or out (the export's ``Value_IN`` / ``Value_OUT``), the counterparty
ADDRESS as the payee (``From`` on an increase, ``To`` on a decrease), and a
network fee paid IN THE COIN.

So the four surfaces are asserted together, because fixing any one of them alone
still leaves the wallet reading wrong:

  1. IMPORT routes to the coin-native parser, never the cash one -- no block
     number reaches an amount, and nothing is written until the row is accepted;
  2. the REVIEW pane renders coin columns (Coin In / Coin Out / Fee), not cash;
  3. ACCEPT posts through ``mammon.crypto``'s wallet writers into
     ``crypto_transactions`` with the coin, the counterparty and a COIN fee leg,
     and no fiat at all;
  4. the REGISTER drops Price / Amount / Cash Bal for a wallet and keeps them for
     an EXCHANGE, which really does hold a cash sleeve.

Offscreen Qt; synthetic ANON data only -- the fixture's addresses are obvious
0x1111.../0x2222... placeholders and no real wallet address, hash or amount
appears here.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal
from pathlib import Path

import pytest

from mammon import crypto, db, import_review
from mammon.ui import import_review_widget as irw
from mammon.ui.models import CryptoRegisterModel
from mammon.ui.widgets import CryptoHoldingsDialog, CryptoRegisterWidget, MainWindow

FIXTURE = Path(__file__).parent / "fixtures" / "etherscan_eth_2020.csv"

WALLET_ADDR = "0x1111111111111111111111111111111111111111"
COUNTERPARTY = "0x2222222222222222222222222222222222222222"
RECIPIENT = "0x3333333333333333333333333333333333333333"


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    # review_visibility() reads QSettings; keep it out of the real profile.
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "wallet.db")
    yield c
    c.close()


@pytest.fixture
def wallet(conn):
    aid = crypto.create_account(conn, "Paper Wallet",
                                kind=crypto.CRYPTO_KIND_WALLET)
    # The address is what makes gas attribution and own-wallet detection possible.
    conn.execute("UPDATE accounts SET account_number=? WHERE id=?",
                 (WALLET_ADDR, aid))
    conn.commit()
    return aid


@pytest.fixture
def exchange(conn):
    return crypto.create_account(conn, "Exchange",
                                 kind=crypto.CRYPTO_KIND_EXCHANGE)


def _import(win, account_id, path=FIXTURE):
    """Drive the real per-account file-import chokepoint, reporting suppressed."""
    from mammon import ledger
    acct = ledger.get_account(win.conn, account_id)
    return win._ingest_file_via_review(account_id, acct, str(path), report=False)


# ---------------------------------------------------------------------------
# 1. import routes coin-native and writes nothing yet
# ---------------------------------------------------------------------------
def test_wallet_import_produces_coin_native_review_rows(qapp, conn, wallet):
    win = MainWindow(conn)
    try:
        res = _import(win, wallet)
    finally:
        win.close()
    assert res["parsed"] == 3 and res["inserted"] == 3
    entries = import_review.load_pending(conn, wallet)
    assert len(entries) == 3
    assert all(e.mapped.is_crypto for e in entries)
    # NOTHING is in the ledger yet: review is still the gate.
    assert conn.execute(
        "SELECT COUNT(*) FROM crypto_transactions WHERE account_id=?",
        (wallet,)).fetchone()[0] == 0

    by_hash = {e.mapped.tx_hash[-1]: e.mapped for e in entries}
    # The two receives: quantity from Value_IN, payee from From, no fee (gas is
    # the sender's, and the sender is the counterparty on an inbound row).
    for tail in ("1", "2"):
        m = by_hash[tail]
        assert m.action == "RECEIVE"
        assert m.symbol == "ETH"
        assert Decimal(m.quantity) == Decimal("2")
        assert m.payee == COUNTERPARTY
        assert m.fee_quantity == ""
    # The send: quantity from Value_OUT, payee from To, gas as a COIN fee.
    out = by_hash["3"]
    assert out.action == "SEND"
    assert Decimal(out.quantity) == Decimal("1")
    assert out.payee == RECIPIENT
    assert out.fee_symbol == "ETH" and Decimal(out.fee_quantity) == Decimal("0.002")

    # No fiat anywhere -- and in particular the block number (1000001..3) never
    # became an amount, which is the defect this test exists for.
    amounts = [r[0] for r in conn.execute(
        "SELECT amount FROM review_items WHERE account_id=?", (wallet,))]
    assert amounts == [0, 0, 0]
    prices = [r[0] for r in conn.execute(
        "SELECT price FROM review_items WHERE account_id=?", (wallet,))]
    assert prices == [None, None, None]


def test_reimporting_the_same_export_adds_nothing(qapp, conn, wallet):
    """``tx_hash`` is a globally unique on-chain key, so a re-import is a no-op --
    it must not stack a second pending copy of every row."""
    win = MainWindow(conn)
    try:
        _import(win, wallet)
        again = _import(win, wallet)
    finally:
        win.close()
    assert again["parsed"] == 0 and again["inserted"] == 0
    assert len(import_review.load_pending(conn, wallet)) == 3


# ---------------------------------------------------------------------------
# 2. the review pane renders coin columns
# ---------------------------------------------------------------------------
def test_review_pane_uses_coin_columns_not_cash(qapp, conn, wallet):
    win = MainWindow(conn)
    try:
        _import(win, wallet)
    finally:
        win.close()
    reg = CryptoRegisterWidget(conn, wallet)
    try:
        panel = reg.review_panel
        assert panel.is_crypto_wallet
        headers = [panel.table.horizontalHeaderItem(c).text()
                   for c in range(panel.table.columnCount())]
        assert headers == irw._CRYPTO_HEADERS
        # The cash shape's money columns are gone; the coin ones are there.
        assert "Amount" not in headers and "Num" not in headers
        assert {"Coin", "Coin In", "Coin Out", "Fee"} <= set(headers)

        reg.show_review(import_review.load_pending(conn, wallet))
        rows = {}
        for r in range(panel.table.rowCount()):
            def cell(c):
                it = panel.table.item(r, c)
                return it.text() if it is not None else ""
            rows[cell(irw.C_PAYEE)] = (cell(irw.C_COIN), cell(irw.C_IN),
                                       cell(irw.C_OUT), cell(irw.C_FEE))
        # A receive fills Coin In and leaves Coin Out and Fee blank...
        assert rows[COUNTERPARTY] == ("ETH", "2", "", "")
        # ...and a send fills Coin Out and carries the gas, in ETH.
        assert rows[RECIPIENT] == ("ETH", "", "1", "0.002 ETH")
    finally:
        reg.deleteLater()


# ---------------------------------------------------------------------------
# 3. accept posts coin-native, through the crypto writers
# ---------------------------------------------------------------------------
def test_accepting_review_rows_posts_coin_native(qapp, conn, wallet):
    win = MainWindow(conn)
    try:
        _import(win, wallet)
    finally:
        win.close()
    assert import_review.accept_all(conn, wallet) == 3

    rows = list(conn.execute(
        "SELECT action, symbol, quantity, payee, amount, price, basis, "
        "       fee_symbol, fee_quantity, fee_amount, tx_hash "
        "FROM crypto_transactions WHERE account_id=? ORDER BY date", (wallet,)))
    assert len(rows) == 3
    for r in rows:
        # No fiat leg on ANY wallet row: no amount, no price, no basis, and the
        # fee is never a USD figure.
        assert r["amount"] is None and r["price"] is None and r["basis"] is None
        assert r["fee_amount"] is None
        assert r["symbol"] == "ETH"
        assert r["tx_hash"]
    assert [r["action"] for r in rows] == ["RECEIVE", "RECEIVE", "SEND"]
    assert [r["payee"] for r in rows] == [COUNTERPARTY, COUNTERPARTY, RECIPIENT]
    # The send is stored signed-negative with a coin fee leg.
    send = rows[-1]
    assert Decimal(send["quantity"]) == Decimal("-1")
    assert send["fee_symbol"] == "ETH"
    assert Decimal(send["fee_quantity"]) == Decimal("0.002")

    # Per-token holdings: 2 + 2 - 1 - 0.002 gas.
    held = {h.symbol: h.quantity for h in crypto.holdings(conn, wallet)}
    assert Decimal(held["ETH"]) == Decimal("2.998")
    # A wallet has no cash sleeve, and accepting coin rows must not invent one.
    assert crypto.crypto_cash(conn, wallet) == 0


# ---------------------------------------------------------------------------
# 4. the register's columns follow the account KIND
# ---------------------------------------------------------------------------
def test_wallet_register_drops_price_amount_and_cash_bal(qapp, conn, wallet):
    win = MainWindow(conn)
    try:
        _import(win, wallet)
    finally:
        win.close()
    import_review.accept_all(conn, wallet)

    m = CryptoRegisterModel(conn, wallet)
    assert m.is_wallet
    headers = [m.headerData(c, 1) for c in range(m.columnCount())]
    assert "Price" not in headers
    assert "Amount" not in headers
    assert "Cash Bal" not in headers
    assert {"Coin In", "Coin Out", "Coin Bal", "Fee", "Payee"} <= set(headers)
    for key in (CryptoRegisterModel.PRICE, CryptoRegisterModel.AMOUNT,
                CryptoRegisterModel.CASH_BAL):
        assert m.column_index(key) == -1

    def cell(row, key):
        return m.data(m.index(row, m.column_index(key)))

    # Rows are in application order: receive, receive, send.
    assert cell(0, CryptoRegisterModel.COIN_IN) == "2"
    assert cell(0, CryptoRegisterModel.COIN_OUT) == ""
    assert cell(0, CryptoRegisterModel.PAYEE) == COUNTERPARTY
    assert cell(2, CryptoRegisterModel.COIN_OUT) == "1"
    assert cell(2, CryptoRegisterModel.COIN_IN) == ""
    assert cell(2, CryptoRegisterModel.PAYEE) == RECIPIENT
    assert cell(2, CryptoRegisterModel.FEE) == "0.002 ETH"
    # The running coin balance still nets the same-coin gas leg.
    assert Decimal(cell(2, CryptoRegisterModel.COIN_BAL)) == Decimal("2.998")


def test_exchange_register_keeps_the_fiat_columns(qapp, conn, exchange):
    """An exchange account is custodial -- it holds a cash sleeve and trades coin
    for dollars -- so its rows really do carry a price, an amount and a running
    cash balance. Dropping them for a wallet must not drop them here."""
    m = CryptoRegisterModel(conn, exchange)
    assert not m.is_wallet
    headers = [m.headerData(c, 1) for c in range(m.columnCount())]
    assert headers == CryptoRegisterModel.HEADERS
    assert {"Price", "Amount", "Cash Bal"} <= set(headers)
    # The class constants still read as positions for the exchange layout.
    assert m.column_index(CryptoRegisterModel.AMOUNT) == CryptoRegisterModel.AMOUNT


def test_wallet_surfaces_show_no_cash(qapp, conn, wallet):
    """A paper-wallet address has no fiat sleeve, so neither the register header
    nor the holdings window may report a Cash figure for it."""
    win = MainWindow(conn)
    try:
        _import(win, wallet)
    finally:
        win.close()
    import_review.accept_all(conn, wallet)

    reg = CryptoRegisterWidget(conn, wallet)
    try:
        assert "Cash" not in reg.valuation_label.text()
        assert "Coins" in reg.valuation_label.text()
    finally:
        reg.deleteLater()
    dlg = CryptoHoldingsDialog(conn, wallet)
    try:
        assert "Cash" not in dlg.total_label.text()
        symbols = [dlg.table.item(r, dlg.SYMBOL).text()
                   for r in range(dlg.table.rowCount())]
        assert symbols == ["ETH"]
    finally:
        dlg.deleteLater()

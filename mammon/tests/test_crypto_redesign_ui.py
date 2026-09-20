"""Wiring the redesigned (multi-token, security-style) crypto domain into the UI.

This is the phase-2 lifecycle test: it creates BOTH crypto account kinds THROUGH
the real New Account dialog (so the wallet/exchange choice actually flows to
``crypto.create_account(kind=)``), imports a synthetic Etherscan by-address CSV
onto the wallet through the corrected coin-native importer, and asserts the two
UI-facing shapes the redesign adds:

  1. per-token holdings on the wallet -- each coin (ETH, LINK) a distinct
     position with its own quantity, valued in USD only at the net-worth layer;
     no fiat leg rides any wallet row;
  2. the per-asset net-worth breakdown -- one line per coin/currency with a
     NATIVE quantity and a USD-converted value (USD only at the net-worth layer),
     and a grand total that never double-counts a crypto holding (it equals
     ``fx.total_in_currency``).

Synthetic data only: every address below is an obvious ANON placeholder, reusing
the gitignored-shape fixture ``fixtures/etherscan_eth_2020.csv`` (no real wallet
address, hash or amount appears here).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal
from pathlib import Path

import pytest

from mammon import crypto, db, fx, investments, ledger
from mammon.importers import crypto_core
from mammon.tests import fresh_db

FIXTURE = Path(__file__).parent / "fixtures" / "etherscan_eth_2020.csv"

# The fixture's wallet is 0x1111...; it receives 2 ETH twice from COUNTERPARTY and
# sends 1 ETH to RECIPIENT (with 0.002 ETH gas).
COUNTERPARTY = "0x2222222222222222222222222222222222222222"
RECIPIENT = "0x3333333333333333333333333333333333333333"


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mammon.db")
    yield c
    c.close()


def _make_account_via_dialog(conn, *, name, type_, kind=None, opening=0.0):
    """Drive the exact path on_new_account uses: build the New Account dialog, set
    its fields, and turn its values() into an account through the shared
    create_account_from_values helper (which routes a crypto type to
    crypto.create_account so crypto_kind/asset_class are set)."""
    from mammon.ui import widgets
    dlg = widgets.NewAccountDialog()
    try:
        dlg.name.setText(name)
        i = dlg.type.findText(type_)
        assert i >= 0
        dlg.type.setCurrentIndex(i)
        if kind is not None:
            j = dlg.crypto_kind.findData(kind)
            assert j >= 0, f"dialog offers no crypto kind {kind!r}"
            dlg.crypto_kind.setCurrentIndex(j)
        dlg.opening.setValue(opening)
        v = dlg.values()
        aid = widgets.create_account_from_values(conn, v)
    finally:
        dlg.deleteLater()
    return aid


def test_dialog_offers_both_crypto_kinds(qapp):
    """The dialog must offer BOTH kinds, each carrying its CRYPTO_KIND_* constant
    as item data so values() is label-agnostic."""
    from mammon.ui import widgets
    dlg = widgets.NewAccountDialog()
    try:
        kinds = {dlg.crypto_kind.itemData(i) for i in range(dlg.crypto_kind.count())}
        assert kinds == {crypto.CRYPTO_KIND_WALLET, crypto.CRYPTO_KIND_EXCHANGE}
    finally:
        dlg.deleteLater()


def test_wallet_and_exchange_lifecycle(qapp, conn):
    from PyQt5.QtCore import Qt
    from mammon.ui.models import CryptoRegisterModel, NetWorthByAssetModel

    # --- create both kinds through the dialog -----------------------------
    wallet = _make_account_via_dialog(
        conn, name="Paper Wallet", type_="crypto", kind=crypto.CRYPTO_KIND_WALLET)
    exchange = _make_account_via_dialog(
        conn, name="Exchange", type_="crypto", kind=crypto.CRYPTO_KIND_EXCHANGE,
        opening=1000.0)
    assert crypto.is_wallet_account(ledger.get_account(conn, wallet))
    assert crypto.is_exchange_account(ledger.get_account(conn, exchange))

    # --- coin-native Etherscan import onto the wallet ---------------------
    result = crypto_core.import_etherscan_file(conn, FIXTURE, wallet)
    assert result.imported == 3
    events = crypto.list_events(conn, wallet)
    assert [e["action"] for e in events] == ["RECEIVE", "RECEIVE", "SEND"]
    # No fiat leg on any wallet row: price/amount/basis all NULL, and
    # the cash sleeve stays 0 (a wallet has no cash).
    for e in events:
        assert e["amount"] is None and e["price"] is None and e["basis"] is None
    assert crypto.crypto_cash(conn, wallet) == 0
    # The on-chain counterparty rode payee: From on a credit, To on the debit.
    recv0, _recv1, send = events
    assert recv0["payee"] == COUNTERPARTY
    assert send["payee"] == RECIPIENT
    # Gas is a coin-native leg, NEVER a USD amount.
    assert send["fee_symbol"] == "ETH" and send["fee_quantity"] == "0.002"
    assert send["fee_amount"] is None

    # --- a second token, so holdings are genuinely per-token --------------
    crypto.record_wallet_credit(conn, wallet, "2020-07-01", "LINK", "60",
                                payee=COUNTERPARTY)
    crypto.rebuild_holdings(conn, wallet)
    # 2 + 2 - 1 - 0.002 gas = 2.998 ETH; LINK is a distinct position, never merged.
    assert crypto.get_holding(conn, wallet, "ETH")["quantity"] == "2.998"
    assert crypto.get_holding(conn, wallet, "LINK")["quantity"] == "60"

    # --- the register surfaces the payee (From/To) column -----------------
    m = CryptoRegisterModel(conn, wallet)
    headers = [m.headerData(c, Qt.Horizontal) for c in range(m.columnCount())]
    assert "Payee" in headers
    payee_col = headers.index("Payee")
    send_row = next(i for i in range(m.rowCount())
                    if m.txn_at(i)["action"] == "SEND")
    assert m.data(m.index(send_row, payee_col), Qt.DisplayRole) == RECIPIENT

    # --- USD only at the net-worth layer: seed the {SYM}-USD price series --
    investments.record_price(conn, "ETH-USD", "2020-12-31", "2000")
    investments.record_price(conn, "LINK-USD", "2020-12-31", "25")

    bd = fx.net_worth_by_asset(conn)
    by = {ln.asset: ln for ln in bd.lines}
    # Per-coin NATIVE quantity row.
    assert by["ETH"].quantity == Decimal("2.998")
    assert by["LINK"].quantity == Decimal("60")
    assert by["ETH"].is_coin and by["LINK"].is_coin
    # Per-coin USD-converted row: 2.998 * $2000 and 60 * $25.
    assert by["ETH"].usd_cents == 599600
    assert by["LINK"].usd_cents == 150000
    # The exchange's fiat cash sleeve is a currency line, not a coin.
    assert not by["USD"].is_coin
    assert by["USD"].native_cents == 100000
    assert by["USD"].usd_cents == 100000
    # The grand total sums every line AND matches the account bar's own figure --
    # the crypto holdings are counted exactly once (no double count).
    assert bd.total_usd_cents == 599600 + 150000 + 100000
    assert bd.total_usd_cents == fx.total_in_currency(conn, "USD")

    # --- the same breakdown, projected into the UI model ------------------
    nwm = NetWorthByAssetModel(conn)
    cols = [nwm.headerData(c, Qt.Horizontal) for c in range(nwm.columnCount())]
    assert cols == ["ETH", "LINK", "USD", "Total"]
    vrows = [nwm.headerData(r, Qt.Vertical) for r in range(nwm.rowCount())]
    assert vrows == ["Native", "USD"]
    assert nwm.total_cents() == bd.total_usd_cents

    # --- and the accounts overview wires the breakdown table in -----------
    from mammon.ui.widgets import AccountsWidget
    w = AccountsWidget(conn)
    try:
        assert w.asset_model.columnCount() == 4        # ETH, LINK, USD, Total
        assert w.asset_model.total_cents() == bd.total_usd_cents
    finally:
        w.deleteLater()

"""Renaming a coin, and prices filed under the bare symbol instead of the pair.

User-reported, 2026-09-15: "I did merge ETH-USD in to ETH when I saw that both
existed", then "The securities table won't let me click on ETH to rename it and
the crypto accounts don't have a Rename Security like the investment accounts."
A coin's prices live under ``SYM-USD`` so a coin and a stock of the same ticker
cannot collide; filed under the bare symbol they are invisible to every
valuation and the holding reads as worth nothing. Synthetic data only.
"""
from __future__ import annotations

import os
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import crypto, db, investments, ledger
from mammon.tests import fresh_db


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "coins.db")
    yield c
    c.close()


@pytest.fixture
def wallet(conn):
    aid = crypto.create_account(conn, "ANON Wallet", kind=crypto.CRYPTO_KIND_WALLET)
    crypto.record_wallet_credit(conn, aid, "2025-01-02", "ANC", "10")
    crypto.rebuild_holdings(conn, aid)
    return aid


def test_prices_under_the_bare_symbol_are_found_and_moved(conn, wallet):
    investments.record_prices(conn, [("ANC", "2026-09-10", "2478.23", "yfinance"),
                                     ("ANC", "2026-09-11", "2444.89", "yfinance")])
    assert crypto.display_balance(conn, wallet) == 0          # the coin values at nothing
    assert crypto.misfiled_prices(conn, wallet) == {"ANC": 2}
    assert crypto.adopt_misfiled_prices(conn, "ANC") == 2
    assert crypto.misfiled_prices(conn, wallet) == {}
    assert crypto.latest_price(conn, "ANC") == Decimal("2444.89")
    assert crypto.display_balance(conn, wallet) == 10 * 2444_89


def test_a_real_security_of_the_same_ticker_is_left_alone(conn, wallet):
    """'ETH' was a stock ticker before it was Ethereum: prices belonging to a
    security must never be taken for a coin's."""
    stock = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    investments.record_investment(conn, stock, "2020-01-02", "Buy", symbol="ANC",
                                  quantity="10", price="30", amount=-300_00)
    investments.record_prices(conn, [("ANC", "2026-09-11", "31", "yfinance")])
    assert crypto.misfiled_prices(conn, wallet) == {}
    assert crypto.adopt_misfiled_prices(conn, "") == 0


def test_a_price_already_filed_correctly_wins(conn, wallet):
    investments.record_prices(conn, [("ANC", "2026-09-11", "2444.89", "yfinance"),
                                     ("ANC-USD", "2026-09-11", "2450", "yfinance")])
    crypto.adopt_misfiled_prices(conn, "ANC")
    assert crypto.latest_price(conn, "ANC") == Decimal("2450")
    assert conn.execute("SELECT COUNT(*) FROM price_history WHERE symbol='ANC'").fetchone()[0] == 0


def test_renaming_a_coin_moves_its_events_and_its_price_series(conn, wallet):
    investments.record_prices(conn, [("ANC-USD", "2026-09-11", "2444.89", "yfinance")])
    assert crypto.apply_coin_renames(conn, wallet, [("ANC", "ANC2")]) == 1
    assert [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM crypto_transactions WHERE account_id=?", (wallet,))] == ["ANC2"]
    assert crypto.latest_price(conn, "ANC2") == Decimal("2444.89")
    assert crypto.display_balance(conn, wallet) == 10 * 2444_89


def test_the_crypto_register_renames_and_repairs(qapp, conn, wallet):
    from mammon.ui.widgets import CryptoRegisterWidget
    investments.record_prices(conn, [("ANC", "2026-09-11", "100", "yfinance")])
    reg = CryptoRegisterWidget(conn, wallet)
    reg._say = lambda *a, **k: None          # no modal in a headless test
    assert reg.act_fix_prices.isVisible() or reg._misfiled == {"ANC": 1}
    assert reg.fix_coin_prices(confirmed=True) == 1
    assert reg._misfiled == {}
    assert reg.coins_held() == ["ANC"]
    assert reg.rename_coin(("ANC", "ANC2")) == 1
    assert reg.coins_held() == ["ANC2"]
    assert crypto.latest_price(conn, "ANC2") == Decimal("100")
    reg.close()


def test_the_menu_action_itself_repairs_the_prices(qapp, conn, wallet):
    """Triggering the ACTION, not calling the method: QAction.triggered hands a
    `checked` bool to a slot that can take an argument, and fix_coin_prices read
    it as "the user said no" -- the menu item did nothing at all (reported)."""
    from mammon.ui.widgets import CryptoRegisterWidget
    investments.record_prices(conn, [("ANC", "2026-09-11", "100", "yfinance")])
    reg = CryptoRegisterWidget(conn, wallet)
    reg._say = lambda *a, **k: None
    reg._confirm_fix = True
    reg.fix_coin_prices = (lambda confirmed=None, _f=reg.fix_coin_prices:
                           _f(True if confirmed is None else confirmed))
    reg.act_fix_prices.trigger()
    assert crypto.misfiled_prices(conn, wallet) == {}
    assert crypto.display_balance(conn, wallet) == 10 * 100_00
    reg.close()

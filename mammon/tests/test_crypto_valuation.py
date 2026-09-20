"""A crypto EXCHANGE holds COINS, not USD cash -- so its cash figure must match
the register everywhere, and the register was already right.

The reported defect: an ETH exchange whose register Cash Bal read $0 (correct)
reported $78,508.45 of CASH in the holdings view and account list, and a total
of coin_value + $78,508.45 -- nearly double its real worth. The cash figure and
the register disagreed.

Root cause: two different cash formulas.
  * ``crypto.register_rows`` computes the sleeve as ``opening_balance`` + the
    crypto events whose fiat settles HERE (BUY/SELL/DEPOSIT/WITHDRAW). It never
    reads the ordinary ``transactions`` table.
  * ``crypto.account_valuation`` computed cash as
    ``ledger.account_balance`` (opening + ALL ordinary transactions) +
    ``crypto_cash`` (which summed every non-NULL ``amount``, X-twins included).

They agreed only when the sole ordinary rows were an X-twin's mirror leg (which
cancels the X-twin's ``amount``). A crypto account carrying an ordinary row that
is NOT such a leg -- an imported deposit, a stray transfer -- had that balance
counted as cash the exchange does not hold. This pins the fixed behavior:
``account_valuation`` reads the sleeve the register shows and nothing else.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import ROUND_HALF_UP, Decimal

import pytest

from mammon import crypto, db, investments, ledger
from mammon.tests import fresh_db

ETH_QTY = "27.176547"
ETH_PRICE = 2000                 # dollars per ETH -> a deterministic coin value
PHANTOM_CENTS = 7850845          # $78,508.45 -- the exact shape the user reported


def _coin_cents(qty: str, price_dollars) -> int:
    return int((Decimal(qty) * Decimal(price_dollars) * 100)
               .to_integral_value(rounding=ROUND_HALF_UP))


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "crypto_valuation.db")
    yield c
    c.close()


def _exchange_coins_only(conn) -> int:
    """A crypto EXCHANGE holding only ETH (coin-native, no fiat leg) with a ZERO
    cash sleeve, but carrying a stray ORDINARY ``transactions`` row -- the shape
    that produced the phantom-cash bug. Returns the account id."""
    ex = crypto.create_account(conn, "Coinbase", kind=crypto.CRYPTO_KIND_EXCHANGE)
    crypto.record_wallet_credit(conn, ex, "2021-01-01", "ETH", ETH_QTY,
                                action="RECEIVE")
    # An ordinary transaction on the crypto account (an imported deposit, an old
    # transfer leg): NOT a crypto sleeve event, so the register never counts it.
    ledger.add_transaction(conn, ex, "2021-02-01", PHANTOM_CENTS, payee="import")
    crypto.rebuild_holdings(conn, ex)
    return ex


def test_crypto_exchange_cash_is_zero_not_the_phantom(conn):
    ex = _exchange_coins_only(conn)

    # The register's Cash Bal column -- the figure the user confirmed correct.
    reg = crypto.register_rows(conn, ex)
    assert reg[-1]["cash_bal"] == 0

    # The stray ordinary balance really is on the account -- this is the trap...
    assert ledger.account_balance(conn, ex) == PHANTOM_CENTS
    # ...but the sleeve and the valuation cash ignore it. The account holds coins,
    # not USD cash: its cash is zero, not the coin value and not the phantom.
    assert crypto.crypto_cash(conn, ex) == 0
    val = crypto.account_valuation(conn, ex, prices={"ETH": ETH_PRICE})
    assert val.cash == 0
    assert val.cash != PHANTOM_CENTS


def test_crypto_exchange_total_is_coin_value_alone(conn):
    ex = _exchange_coins_only(conn)
    coin = _coin_cents(ETH_QTY, ETH_PRICE)

    val = crypto.account_valuation(conn, ex, prices={"ETH": ETH_PRICE})
    assert val.securities == coin
    assert val.total == coin                     # no phantom cash added
    assert val.total != coin + PHANTOM_CENTS      # the wrong number the user saw
    # display_balance is what the holdings view / account list actually render.
    assert crypto.display_balance(conn, ex, prices={"ETH": ETH_PRICE}) == coin


def test_account_list_net_worth_excludes_phantom_crypto_cash(conn):
    ex = _exchange_coins_only(conn)
    ledger.create_account(conn, "Checking", "checking", opening_balance=100000)
    coin = _coin_cents(ETH_QTY, ETH_PRICE)

    # investments.net_worth (which ledger.net_worth delegates to for a one-currency
    # ledger) sums display_balance per account -- exactly what the account list
    # totals. The crypto account contributes its coin value, never the phantom.
    nw = investments.net_worth(conn, prices={"ETH": ETH_PRICE})
    assert nw == 100000 + coin
    assert nw != 100000 + coin + PHANTOM_CENTS


def test_register_and_valuation_cash_never_diverge(conn):
    """The register and the valuation must report the SAME cash for a crypto
    account, across a plain trade left in the sleeve, a SELLX whose proceeds went
    to a bank, and a stray ordinary row. Locking this keeps a future 'cleanup'
    from reintroducing the two-formula split that caused the phantom."""
    ex = crypto.create_account(conn, "Coinbase", kind=crypto.CRYPTO_KIND_EXCHANGE)
    bank = ledger.create_account(conn, "Checking", "checking")
    crypto.record_cash(conn, ex, "2021-01-01", 500000)           # +$5000 deposit
    crypto.record_buy(conn, ex, "2021-01-02", "ETH", 1, 300000)   # -$3000 buy
    sell = crypto.record_sell(conn, ex, "2021-02-01", "ETH", "0.5", 180000)
    crypto.link_as_transfer(conn, sell, bank)   # -> SELLX: the proceeds left here
    ledger.add_transaction(conn, ex, "2021-03-01", 4242, payee="stray")
    crypto.rebuild_holdings(conn, ex)

    reg = crypto.register_rows(conn, ex)
    reg_cash = reg[-1]["cash_bal"]
    val_cash = crypto.account_valuation(conn, ex, prices={"ETH": 0}).cash
    assert reg_cash == val_cash
    # deposit(+500000) - buy(300000) = 200000; the SELLX proceeds are in the bank,
    # and the stray ordinary +4242 belongs to neither path.
    assert val_cash == 200000
    assert ledger.account_balance(conn, bank) == 180000

"""Realized P/L for short positions, and a sale's commission counted once.

Two defects made the Holdings window's Previously Held tab disagree with the cash
a closed position actually produced, badly so for an option-writing history:

* A trade was applied by its ACTION alone. Covering a short (``CvrShrt``, or the
  ``Buy`` Quicken uses as often) realized nothing, a ``Buy`` against a short
  stacked a long lot on top of it, and ``ShtSell`` against a long mixed a credit
  into the long's cost. An option writer's entire history is ``ShtSell`` and
  ``CvrShrt``, so every written contract showed $0.
* A sale's amount is the NET cash received, but the replay subtracted the
  commission from it again.

The property that pins both: once every position is flat, a symbol's realized
P/L equals the net cash of its rows, to the cent. All data is synthetic.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, investments, ledger
from mammon.tests import fresh_db

STOCK = "ACME"
CALL45 = "ACME 260417C00045000"
PUT40 = "ACME 260417P00040000"
PUT35 = "ACME 260417P00035000"


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "short.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)


def _rec(conn, acct, date, action, symbol, qty, price=None, amount=0, commission=None):
    return investments.record_investment(conn, acct, date, action, symbol=symbol,
                                         quantity=qty, price=price, amount=amount,
                                         commission=commission)


def _pos(conn, acct, symbol):
    return investments._replay_positions(conn, acct, use_snapshots=False)[symbol]


def _net_cash(conn, acct, symbol):
    return sum(investments._cash_effect(r["action"], r["amount"]) for r in conn.execute(
        "SELECT action, amount FROM investment_transactions WHERE account_id=? AND symbol=?",
        (acct, symbol)))


def test_a_written_option_that_expires_realizes_the_whole_premium(conn, acct):
    _rec(conn, acct, "2026-01-05", "ShtSell", CALL45, "500", "4.10", 2_045_00, 5_00)
    _rec(conn, acct, "2026-04-17", "CvrShrt", CALL45, "500")
    pos = _pos(conn, acct, CALL45)
    assert (pos.qty, pos.cost) == (0, 0)
    assert pos.realized == 2_045_00
    assert [g.term for g in pos.gains] == ["short"]


def test_a_written_option_bought_back_realizes_premium_less_cost(conn, acct):
    _rec(conn, acct, "2026-01-05", "ShtSell", PUT40, "300", "2.00", 595_00, 5_00)
    _rec(conn, acct, "2026-02-05", "CvrShrt", PUT40, "100", "1.00", 105_00, 5_00)
    pos = _pos(conn, acct, PUT40)
    assert pos.qty == -200
    assert pos.realized == 198_33 - 105_00          # a third of the credit, less the cover
    assert pos.cost == -(595_00 - 198_33)           # the remaining credit


def test_a_buy_that_covers_a_short_realizes_it_and_the_rest_opens_a_long(conn, acct):
    _rec(conn, acct, "2026-01-05", "ShtSell", STOCK, "10", "50", 500_00)
    _rec(conn, acct, "2026-04-01", "Buy", STOCK, "25", "40", 1_000_00)
    pos = _pos(conn, acct, STOCK)
    assert pos.realized == 500_00 - 400_00          # credit less 10/25 of the cash spent
    assert (pos.qty, pos.cost) == (15, 600_00)
    assert [(l.qty, l.cost) for l in pos.lots] == [(15, 600_00)]


def test_shtsell_against_a_long_sells_the_long_first(conn, acct):
    _rec(conn, acct, "2026-01-05", "Buy", STOCK, "10", "20", 200_00)
    _rec(conn, acct, "2026-02-05", "ShtSell", STOCK, "15", "30", 450_00)
    pos = _pos(conn, acct, STOCK)
    assert pos.realized == 300_00 - 200_00          # the 10 held, sold
    assert (pos.qty, pos.cost) == (-5, -150_00)     # the other 5 opened a short


def test_a_sale_with_nothing_held_opens_a_short_instead_of_booking_its_proceeds(conn, acct):
    _rec(conn, acct, "2026-01-05", "Sell", STOCK, "10", "30", 300_00)
    pos = _pos(conn, acct, STOCK)
    assert pos.realized == 0
    assert (pos.qty, pos.cost) == (-10, -300_00)


def test_a_sales_commission_is_charged_once(conn, acct):
    _rec(conn, acct, "2026-01-05", "Buy", STOCK, "10", "10", 101_00, 1_00)
    _rec(conn, acct, "2026-02-05", "Sell", STOCK, "10", "12", 119_00, 1_00)
    pos = _pos(conn, acct, STOCK)
    assert pos.realized == 18_00 == _net_cash(conn, acct, STOCK)


def test_a_sale_with_no_amount_still_nets_its_commission(conn, acct):
    _rec(conn, acct, "2026-01-05", "Buy", STOCK, "10", "10", 100_00)
    _rec(conn, acct, "2026-02-05", "Sell", STOCK, "10", "12", None, 1_00)
    assert _pos(conn, acct, STOCK).realized == 119_00 - 100_00


def test_a_long_only_history_is_unchanged(conn, acct):
    _rec(conn, acct, "2026-01-05", "Buy", STOCK, "10", "10", 100_00)
    _rec(conn, acct, "2026-02-05", "Buy", STOCK, "10", "20", 200_00)
    _rec(conn, acct, "2026-03-05", "Sell", STOCK, "5", "30", 150_00)
    pos = _pos(conn, acct, STOCK)
    assert (pos.qty, pos.cost) == (15, 225_00)       # average cost 15.00
    assert pos.realized == 150_00 - 75_00


def _assigned_and_exercised(conn, acct):
    """Calls written and assigned, the short stock covered with CvrShrt AND Buy,
    puts written and assigned, long puts exercised, and the last long sold with
    ShtSell -- the Quicken shape throughout."""
    rows = [
        ("2026-01-05", "ShtSell", CALL45, "500", "4.10", 2_045_00, 5_00),
        ("2026-01-05", "ShtSell", PUT40, "300", "2.00", 595_00, 5_00),
        ("2026-01-05", "Buy", PUT35, "200", "0.50", 105_00, 5_00),
        ("2026-02-10", "ShtSell", STOCK, "500", "45", 22_499_00, 1_00),
        ("2026-02-10", "CvrShrt", CALL45, "500", None, 0, None),
        ("2026-02-11", "CvrShrt", STOCK, "300", "51.25", 15_380_00, 5_00),
        ("2026-02-11", "Buy", STOCK, "200", "51.25", 10_255_00, 5_00),
        ("2026-03-20", "Buy", STOCK, "300", "40", 12_000_00, None),
        ("2026-03-20", "CvrShrt", PUT40, "300", None, 0, None),
        ("2026-03-20", "Sell", STOCK, "200", "35", 7_000_00, None),
        ("2026-03-20", "Sell", PUT35, "200", None, 0, None),
        ("2026-04-01", "ShtSell", STOCK, "100", "38", 3_795_00, 5_00),
    ]
    for date, action, symbol, qty, price, amount, commission in rows:
        _rec(conn, acct, date, action, symbol, qty, price, amount, commission)


def test_every_closed_symbol_realizes_exactly_its_net_cash(conn, acct):
    _assigned_and_exercised(conn, acct)
    investments.rebuild_holdings(conn, acct)
    closed = {p.symbol: p for p in investments.closed_positions(conn, acct)}
    assert set(closed) == {STOCK, CALL45, PUT40, PUT35}
    for symbol, p in closed.items():
        assert p.realized_pl == _net_cash(conn, acct, symbol), symbol
    assert sum(p.realized_pl for p in closed.values()) == sum(
        _net_cash(conn, acct, s) for s in closed)


def test_year_end_snapshots_agree_with_a_replay_from_inception(conn, acct):
    _rec(conn, acct, "2025-11-03", "ShtSell", CALL45, "500", "4.10", 2_045_00, 5_00)
    _rec(conn, acct, "2025-12-15", "ShtSell", STOCK, "100", "50", 4_999_00, 1_00)
    _rec(conn, acct, "2026-01-16", "CvrShrt", CALL45, "300", "1.00", 305_00, 5_00)
    _rec(conn, acct, "2026-02-02", "Buy", STOCK, "150", "40", 6_000_00)
    investments.rebuild_holdings(conn, acct)
    seeded = investments._replay_positions(conn, acct, use_snapshots=True)
    oracle = investments._replay_positions(conn, acct, use_snapshots=False)
    for symbol in (STOCK, CALL45):
        a, b = seeded[symbol], oracle[symbol]
        assert (a.qty, a.cost, a.realized) == (b.qty, b.cost, b.realized), symbol
    assert oracle[STOCK].realized == 4_999_00 - 4_000_00
    assert oracle[STOCK].qty == Decimal(50)

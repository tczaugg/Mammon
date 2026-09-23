"""The share side of an option exercise or assignment is not price history.

When a contract is exercised or assigned, the shares change hands at the STRIKE
(or at strike +/- premium once the premium is rolled into the basis, SRD 5.8e-7),
not at what the stock traded for that day. Learning a price from that row writes
a close no share ever traded at: a stock trading near 51 would chart a close of
45 on the day a 45 call was assigned, and valuations on that day would follow.

These cover both shapes a delivery arrives in (Mammon's Exercise/Assign pair,
and the broker/Quicken pair of a strike-priced share row plus an unpriced option
close), the import path end to end, and the two things the guard must NOT do:
touch an ordinary trade that merely happens to be at a strike, or reach a
contract nobody has classified as an option (SRD 5.8e-2).

All data is synthetic -- invented tickers, figures and dates.
"""
from __future__ import annotations

import pytest

from mammon import db, importers, instruments, investments, ledger, securities
from mammon.tests import fresh_db

STOCK = "ACME"
CALL45 = "ACME  260417C00045000"         # strike 45, call
PUT60 = "ACME  260417P00060000"          # strike 60, put
DAY = "2026-03-20"


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "delivery.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)


def _option(conn, symbol, strike, right, classify=True):
    conn.execute("INSERT OR IGNORE INTO securities(symbol, name) VALUES (?,?)",
                 (symbol, symbol))
    conn.commit()
    if classify:
        securities.set_kinds(conn, [dict(
            symbol=symbol, kind=instruments.Kind.OPTION.value, kind_source="user",
            multiplier="100", underlying=STOCK, expiration="2026-04-17",
            strike=strike, option_right=right)])


def _price(conn, symbol, date):
    row = conn.execute("SELECT close_price FROM price_history WHERE symbol=? AND date=?",
                       (symbol, date)).fetchone()
    return None if row is None else row[0]


def _quicken_assignment(conn, acct, date=DAY, shares="500"):
    """The pair as Quicken and a broker's OFX write it: shares at the strike,
    and the contract closed with no price and no amount."""
    investments.record_investment(conn, acct, date, "ShtSell", symbol=STOCK,
                                  quantity=shares, price="45", amount=2_249_900,
                                  commission=100)
    investments.record_investment(conn, acct, date, "CvrShrt", symbol=CALL45,
                                  quantity=shares)


def test_a_strike_priced_share_leg_is_not_learned_as_a_price(conn, acct):
    _option(conn, CALL45, "45", "C")
    _quicken_assignment(conn, acct)
    investments.learn_prices_from_transactions(conn, acct)
    assert _price(conn, STOCK, DAY) is None


def test_the_days_real_trade_still_supplies_the_price(conn, acct):
    """The assignment was booked first, so without the guard its strike would
    win the day under DO-NOTHING precedence."""
    _option(conn, CALL45, "45", "C")
    _quicken_assignment(conn, acct)
    investments.record_investment(conn, acct, DAY, "CvrShrt", symbol=STOCK,
                                  quantity="500", price="51.25", amount=-2_562_500)
    investments.learn_prices_from_transactions(conn, acct)
    assert _price(conn, STOCK, DAY) == "51.25"


def test_contracts_times_multiplier_matches_the_share_count(conn, acct):
    """Quicken counts an option's quantity in shares; a feed or Mammon's own
    writer counts contracts. Both are the same delivery."""
    _option(conn, PUT60, "60", "P")
    investments.record_investment(conn, acct, "2026-04-10", "Buy", symbol=STOCK,
                                  quantity="300", price="60", amount=-1_800_000)
    investments.record_investment(conn, acct, "2026-04-10", "CvrShrt", symbol=PUT60,
                                  quantity="3")
    investments.learn_prices_from_transactions(conn, acct)
    assert _price(conn, STOCK, "2026-04-10") is None


def test_mammons_own_exercise_pair_is_not_learned_either(conn, acct):
    """record_option_exercise writes the share leg with no price and an amount
    of strike x shares + premium; the derived quotient is not a market price."""
    _option(conn, CALL45, "45", "C")
    investments.record_investment(conn, acct, "2026-02-02", "Buy to Open",
                                  symbol=CALL45, quantity="2", amount=-1_000_00)
    investments.record_option_exercise(conn, acct, DAY, CALL45)
    investments.learn_prices_from_transactions(conn, acct)
    assert _price(conn, STOCK, DAY) is None


def test_the_accept_path_is_guarded_too(conn, acct):
    """Accepting a row learns from that one row (txn_id scope); the option close
    on the same day must still be seen."""
    _option(conn, CALL45, "45", "C")
    _quicken_assignment(conn, acct)
    share_id = conn.execute("SELECT id FROM investment_transactions WHERE symbol=?",
                            (STOCK,)).fetchone()[0]
    assert investments.learn_prices_from_transactions(conn, txn_id=share_id) == 0
    assert _price(conn, STOCK, DAY) is None


def test_a_trade_at_a_strike_with_no_option_close_keeps_its_price(conn, acct):
    """A limit order filled at exactly a strike price is still a trade at market.
    A round price alone is not a delivery."""
    _option(conn, CALL45, "45", "C")
    investments.record_investment(conn, acct, "2026-04-01", "Buy", symbol=STOCK,
                                  quantity="100", price="45", amount=-450_000)
    investments.learn_prices_from_transactions(conn, acct)
    assert _price(conn, STOCK, "2026-04-01") == "45"


def test_an_unclassified_contract_changes_nothing(conn, acct):
    """No option rule reaches a security nobody has classified (SRD 5.8e-2): an
    unclassified ledger learns exactly what it learned before."""
    _option(conn, CALL45, "45", "C", classify=False)
    _quicken_assignment(conn, acct)
    investments.learn_prices_from_transactions(conn, acct)
    assert _price(conn, STOCK, DAY) == "45"


def test_a_quicken_export_imports_without_the_strike_as_a_price(conn, tmp_path):
    """End to end through the QIF importer, which learns prices only after its
    !Type:Security block has classified the contract as an option. The stock is
    stored under its NAME ("ACME INC") and the contract's underlying under the
    TICKER ("ACME"); the guard has to join them through ``securities.ticker``.
    The contract is spelled as Quicken writes it, with the standard symbol's
    padded root ("ACME  260417C00045000"): the security list and the trades must
    still land as one security."""
    qif = (
        "!Type:Security\nNACME  260417C00045000 ACME 17APR26 45.0 C\nSACME  260417C00045000\n"
        "TOption\n^\n"
        "NACME INC\nSACME\nTStock\n^\n"
        "!Type:Invst\n"
        "D3/20'26\nNShtSell\nYACME  260417C00045000 ACME 17APR26 45.0 C\nQ500\n"
        "I4.10\nT2,050.00\n^\n"
        "D3/20'26\nNShtSell\nYACME INC\nI45\nQ500\nU22,499.00\nT22,499.00\nO1.00\n^\n"
        "D3/20'26\nNCvrShrt\nYACME  260417C00045000 ACME 17APR26 45.0 C\nQ500\n^\n"
    )
    path = tmp_path / "broker.qif"
    path.write_text(qif, encoding="utf-8")
    importers.import_file(conn, str(path), account="Brokerage", account_type="investment")
    stock_rows = conn.execute(
        "SELECT symbol, price FROM investment_transactions WHERE price='45'").fetchall()
    assert stock_rows, "the share leg should have imported at the strike"
    for symbol, _ in stock_rows:
        assert _price(conn, symbol, "2026-03-20") is None


def test_a_later_file_repeating_the_price_list_does_not_restore_the_strike(conn, tmp_path):
    """Quicken's yearly exports each carry the WHOLE price list. The next year's
    file trades only in another account, yet re-states the assignment day's
    strike close; the account that holds the delivery must be checked again."""
    security = (
        "!Type:Security\nNACME  260417C00045000 ACME 17APR26 45.0 C\nSACME  260417C00045000\n"
        "TOption\n^\n"
        "NACME INC\nSACME\nTStock\n^\n"
    )
    prices = '!Type:Prices\n"ACME",45.000,"3/20\'26"\n^\n'
    first = tmp_path / "2026.qif"
    first.write_text(
        security + "!Type:Invst\n"
        "D3/20'26\nNShtSell\nYACME  260417C00045000 ACME 17APR26 45.0 C\nQ500\n"
        "I4.10\nT2,050.00\n^\n"
        "D3/20'26\nNShtSell\nYACME INC\nI45\nQ500\nT22,499.00\nO1.00\n^\n"
        "D3/20'26\nNCvrShrt\nYACME  260417C00045000 ACME 17APR26 45.0 C\nQ500\n^\n"
        + prices, encoding="utf-8")
    importers.import_file(conn, str(first), account="Brokerage", account_type="investment")
    assert _price(conn, "ACME INC", DAY) is None

    second = tmp_path / "2027.qif"
    second.write_text(
        security + "!Type:Invst\n"
        "D1/5'27\nNBuy\nYACME INC\nI50\nQ10\nT500.00\n^\n" + prices, encoding="utf-8")
    importers.import_file(conn, str(second), account="Other Brokerage",
                          account_type="investment")
    assert _price(conn, "ACME INC", DAY) is None

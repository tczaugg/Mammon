"""An option contract is a DIFFERENT INSTRUMENT from its underlying (SRD 5.8e-2d).

These are the domain-layer regressions for the four ways the old, kind-blind
code fused the two: it folded a contract into the stock's alias identity, summed
its contract count into the stock's share count, offered to "adjust" that count
with invented shares, and valued ten contracts at ten dollars' worth of premium
instead of a thousand's. The last one is the net-worth error.

Every test carries its NULL-kind twin. That is the point of the file as much as
the option behavior is: ``securities.kind`` NULL means UNCLASSIFIED, never
"equity", so an unclassified forty-year ledger has to take exactly the code path
it took before any of this landed. If a guard here starts firing on a NULL-kind
row, these twins are what says so.

All data is synthetic -- invented tickers on an invented issuer.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, instruments, investments, ledger, securities
from mammon.tests import fresh_db

# One synthetic issuer and two OSI contracts on it. ACME is not a real ticker.
STOCK = "ACME"
CALL = "ACME  260116C00050000"
PUT = "ACME  260116P00045000"
EXPIRY = "2026-01-16"
TRADE = "2025-11-03"
AFTER_EXPIRY = "2026-02-02"


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "options.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Brokerage", "investment",
                                 opening_balance=0)


def _security(conn, symbol, kind=None, **terms):
    """One synthetic securities row, classified through the public writer.

    ``kind=None`` leaves the row UNCLASSIFIED, which is what every security in
    an un-backfilled ledger looks like and what the twin tests assert on."""
    conn.execute("INSERT OR IGNORE INTO securities(symbol, name) VALUES (?,?)",
                 (symbol, symbol))
    conn.commit()
    if kind is not None:
        securities.set_kinds(conn, [dict(symbol=symbol, kind=kind,
                                         kind_source="user", **terms)])


def _option(conn, symbol, multiplier="100", expiration=EXPIRY, strike="50",
            right="C"):
    _security(conn, symbol, kind=instruments.Kind.OPTION.value,
              multiplier=multiplier, underlying=STOCK, expiration=expiration,
              strike=strike, option_right=right)


def _trade(conn, account_id, action, symbol, qty, price, mult="1",
           date=TRADE):
    """One trade whose cash amount is the HONEST qty x price x multiplier, so
    cost basis is right and only the VALUATION side is under test."""
    cash = (Decimal(qty) * Decimal(price) * Decimal(mult) * 100
            ).to_integral_value()
    sign = 1 if action.lower() in ("shtsell", "sell") else -1
    investments.record_investment(conn, account_id, date, action, symbol=symbol,
                                  quantity=qty, price=price,
                                  amount=sign * int(cash))
    investments.rebuild_holdings(conn, account_id)


def _reconcile(conn, account_id, symbol, statement_date, stated):
    """A reconciliation with every row through ``statement_date`` ticked, which
    is what the dialog holds once the user has matched the statement's lines.
    Only CLEARED rows count toward a computed balance, so an untouched summary
    would read zero here and say nothing about instrument scoping."""
    s = investments.share_reconcile_summary(conn, account_id, symbol,
                                            statement_date, stated)
    for row in s["uncleared_rows"]:
        investments.set_investment_cleared(conn, row["id"])
    return investments.share_reconcile_summary(conn, account_id, symbol,
                                               statement_date, stated)


def _value(conn, account_id, symbol, price, as_of=None):
    """Market value in cents of ``symbol`` at an injected price."""
    values = investments.holding_values(conn, account_id, as_of=as_of,
                                        prices={symbol: price})
    return next(v.market_value for v in values if v.symbol == symbol)


# ---------------------------------------------------------------------------
# (a) a stock and a contract on it are two positions, reconciled separately
# ---------------------------------------------------------------------------
def test_stock_and_option_are_two_independent_positions(conn, acct):
    _security(conn, STOCK)                      # unclassified, as a real one is
    _option(conn, CALL)
    _trade(conn, acct, "Buy", STOCK, "100", "50.00")
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")

    held = {h["symbol"]: Decimal(h["quantity"])
            for h in investments.list_holdings(conn, acct)}
    assert held == {STOCK: Decimal(100), CALL: Decimal(10)}


def test_three_instruments_on_one_underlying_stay_three(conn, acct):
    """The doc's case: a stock and two contracts on it are THREE positions with
    three independent price series."""
    _security(conn, STOCK)
    _option(conn, CALL, strike="50", right="C")
    _option(conn, PUT, strike="45", right="P")
    _trade(conn, acct, "Buy", STOCK, "100", "50.00")
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")
    _trade(conn, acct, "Buy", PUT, "3", "1.75", mult="100")

    investments.record_price(conn, STOCK, TRADE, "50.00")
    investments.record_price(conn, CALL, TRADE, "4.20")
    investments.record_price(conn, PUT, TRADE, "1.75")

    values = {v.symbol: (v.quantity, v.price, v.market_value)
              for v in investments.holding_values(conn, acct)}
    assert set(values) == {STOCK, CALL, PUT}
    assert values[STOCK] == (Decimal(100), Decimal("50.00"), 5000_00)
    assert values[CALL] == (Decimal(10), Decimal("4.20"), 4200_00)
    assert values[PUT] == (Decimal(3), Decimal("1.75"), 525_00)


def test_option_contracts_never_enter_the_stocks_share_identity(conn, acct):
    _security(conn, STOCK)
    _option(conn, CALL)
    assert investments.share_identity(conn, STOCK) == [STOCK]
    assert investments.share_identity(conn, CALL) == [CALL]


def test_reconciliations_are_scoped_to_one_instrument(conn, acct):
    _security(conn, STOCK)
    _option(conn, CALL)
    _trade(conn, acct, "Buy", STOCK, "100", "50.00")
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")

    shares = _reconcile(conn, acct, STOCK, "2025-11-30", "100")
    contracts = _reconcile(conn, acct, CALL, "2025-11-30", "10")
    # 100 shares and 10 contracts, each balancing on its own statement line --
    # not 110 of anything.
    assert shares["computed_ending_qty"] == Decimal(100)
    assert shares["difference"] == Decimal(0)
    assert contracts["computed_ending_qty"] == Decimal(10)
    assert contracts["difference"] == Decimal(0)
    assert contracts["kind"] == instruments.Kind.OPTION.value
    assert contracts["adjustment_allowed"] is False
    assert shares["adjustment_allowed"] is True


def test_a_hand_written_alias_cannot_fuse_a_contract_onto_its_stock(conn, acct):
    """``add_alias`` refuses this; a row written before that guard existed (or by
    hand) must still not merge the two identities. The alias goes in AFTER the
    trades because the position replay refuses such a set outright (see the next
    test) -- everything below it is the read side, which has to stay correct
    even while that bad row sits in the table."""
    _security(conn, STOCK)
    _option(conn, CALL)
    _trade(conn, acct, "Buy", STOCK, "100", "50.00")
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")
    conn.execute("INSERT INTO security_aliases(alias_symbol, canonical_symbol) "
                 "VALUES (?,?)", (CALL, STOCK))
    conn.commit()

    assert investments.share_identity(conn, STOCK) == [STOCK]
    assert investments.share_identity(conn, CALL) == [CALL]
    shares = _reconcile(conn, acct, STOCK, "2025-11-30", "100")
    contracts = _reconcile(conn, acct, CALL, "2025-11-30", "10")
    assert shares["symbol"] == STOCK
    assert shares["computed_ending_qty"] == Decimal(100)
    assert contracts["symbol"] == CALL
    assert contracts["computed_ending_qty"] == Decimal(10)
    # The stock does not inherit the contract's terms through the alias set.
    assert investments.option_terms(conn, STOCK) is None
    assert investments.contract_multiplier(conn, STOCK) == Decimal(1)


def test_folding_a_contract_into_an_alias_is_refused_loudly(conn, acct):
    """The replay's belt-and-braces: if such an alias set ever reaches the
    position fold, it raises rather than silently summing a contract count into
    a share count. Loud is the point -- a wrong holding would be invisible."""
    _security(conn, STOCK)
    _option(conn, CALL)
    _trade(conn, acct, "Buy", STOCK, "100", "50.00")
    conn.execute("INSERT INTO security_aliases(alias_symbol, canonical_symbol) "
                 "VALUES (?,?)", (CALL, STOCK))
    conn.commit()
    investments.record_investment(conn, acct, TRADE, "Buy", symbol=CALL,
                                  quantity="10", price="4.20", amount=-4200_00)
    with pytest.raises(ValueError, match="option"):
        investments.compute_holdings(conn, acct)


# ---------------------------------------------------------------------------
# (b) valuation: quantity x premium x multiplier, and a short signs negative
# ---------------------------------------------------------------------------
def test_long_option_values_at_quantity_times_premium_times_multiplier(conn, acct):
    _option(conn, CALL, multiplier="100")
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")
    # 10 contracts x $4.20 x 100 = $4,200, not $42.
    assert _value(conn, acct, CALL, "4.20") == 4200_00


def test_a_mini_contracts_multiplier_is_read_not_assumed(conn, acct):
    """Ten is as real a multiplier as a hundred: the column is read, never
    defaulted to the usual contract."""
    _option(conn, CALL, multiplier="10")
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="10")
    assert _value(conn, acct, CALL, "4.20") == 420_00


def test_short_option_values_as_a_liability(conn, acct):
    """A written call is an obligation. Its market value SUBTRACTS from net
    worth -- the sign follows the quantity and nothing takes an absolute
    value."""
    _option(conn, CALL, multiplier="100")
    _trade(conn, acct, "ShtSell", CALL, "5", "3.00", mult="100")

    holdings = {h["symbol"]: Decimal(h["quantity"])
                for h in investments.list_holdings(conn, acct)}
    assert holdings[CALL] == Decimal(-5)
    assert _value(conn, acct, CALL, "3.50") == -1750_00


def test_short_option_subtracts_from_the_accounts_market_value(conn, acct):
    """The same thing one layer up: the rollup the net-worth figure reads."""
    _security(conn, STOCK)
    _option(conn, CALL, multiplier="100")
    _trade(conn, acct, "Buy", STOCK, "100", "50.00")
    _trade(conn, acct, "ShtSell", CALL, "5", "3.00", mult="100")
    investments.record_price(conn, STOCK, TRADE, "50.00")
    investments.record_price(conn, CALL, TRADE, "3.50")

    total = sum(v.market_value for v in investments.holding_values(conn, acct))
    assert total == 5000_00 - 1750_00


def test_option_position_value_matches_the_per_security_view(conn, acct):
    """holding_values and security_positions must not disagree about a
    contract, or the Holdings window and the register footer would differ."""
    _option(conn, CALL, multiplier="100")
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")
    pos = next(p for p in investments.security_positions(
        conn, acct, prices={CALL: "4.20"}) if p.symbol == CALL)
    assert pos.market_value == 4200_00
    assert _value(conn, acct, CALL, "4.20") == pos.market_value


def test_historical_valuation_also_multiplies(conn, acct):
    """holding_values_at is the third site a multiplier could be forgotten at."""
    _option(conn, CALL, multiplier="100")
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")
    investments.record_price(conn, CALL, TRADE, "4.20")
    at = investments.holding_values_at(conn, acct, as_of="2025-11-30")
    assert at[CALL] == 4200_00


# ---------------------------------------------------------------------------
# (c) a contract count is not a share count
# ---------------------------------------------------------------------------
def test_record_share_adjustment_refuses_an_option(conn, acct):
    _option(conn, CALL)
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")
    with pytest.raises(ValueError, match="option contract"):
        investments.record_share_adjustment(conn, acct, CALL, "2025-11-30", "2")


def test_refusal_survives_an_alias_spelling(conn, acct):
    _security(conn, STOCK)
    _option(conn, CALL)
    conn.execute("INSERT INTO security_aliases(alias_symbol, canonical_symbol) "
                 "VALUES (?,?)", (CALL, STOCK))
    conn.commit()
    with pytest.raises(ValueError, match="option contract"):
        investments.record_share_adjustment(conn, acct, CALL, "2025-11-30", "2")


def test_the_refusal_creates_no_row(conn, acct):
    _option(conn, CALL)
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")
    before = len(investments.list_investment_txns(conn, acct))
    with pytest.raises(ValueError):
        investments.record_share_adjustment(conn, acct, CALL, "2025-11-30", "2")
    assert len(investments.list_investment_txns(conn, acct)) == before


# ---------------------------------------------------------------------------
# (e) a contract still open after expiration is a DATA ERROR
# ---------------------------------------------------------------------------
def test_open_position_past_expiration_is_reported(conn, acct):
    _option(conn, CALL, expiration=EXPIRY)
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")
    problems = investments.option_position_problems(conn, acct,
                                                    as_of=AFTER_EXPIRY)
    assert [p.problem for p in problems] == [investments.OPTION_PROBLEM_EXPIRED]
    p = problems[0]
    assert p.symbol == CALL
    assert p.quantity == Decimal(10)
    assert p.expiration == EXPIRY
    assert "missing" in p.detail


def test_a_short_position_past_expiration_is_reported_too(conn, acct):
    _option(conn, CALL, expiration=EXPIRY)
    _trade(conn, acct, "ShtSell", CALL, "5", "3.00", mult="100")
    problems = investments.option_position_problems(conn, acct,
                                                    as_of=AFTER_EXPIRY)
    assert [p.problem for p in problems] == [investments.OPTION_PROBLEM_EXPIRED]
    assert problems[0].quantity == Decimal(-5)
    assert "short" in problems[0].detail


def test_nothing_is_reported_before_expiration(conn, acct):
    _option(conn, CALL, expiration=EXPIRY)
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")
    assert investments.option_position_problems(conn, acct,
                                                as_of="2025-12-31") == []


def test_the_expired_position_is_neither_zeroed_nor_carried_silently(conn, acct):
    """Reporting it must not repair it: the replay still shows exactly what the
    user recorded, so no phantom disposal and no invented tax year appear."""
    _option(conn, CALL, expiration=EXPIRY)
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")
    investments.option_position_problems(conn, acct, as_of=AFTER_EXPIRY)
    held = {h["symbol"]: Decimal(h["quantity"])
            for h in investments.list_holdings(conn, acct)}
    assert held == {CALL: Decimal(10)}
    pos = next(p for p in investments.security_positions(conn, acct)
               if p.symbol == CALL)
    assert pos.realized_pl == 0


def test_an_option_with_no_recorded_multiplier_is_reported(conn, acct):
    """It values at 1x rather than a guessed 100x, and says so."""
    _option(conn, CALL, multiplier=None, expiration=None)
    _trade(conn, acct, "Buy", CALL, "10", "4.20")
    assert investments.contract_multiplier(conn, CALL) == Decimal(1)
    problems = investments.option_position_problems(conn, acct, as_of=TRADE)
    assert [p.problem for p in problems] == [
        investments.OPTION_PROBLEM_NO_MULTIPLIER]


def test_a_closed_option_position_is_never_reported(conn, acct):
    _option(conn, CALL, expiration=EXPIRY)
    _trade(conn, acct, "Buy", CALL, "10", "4.20", mult="100")
    _trade(conn, acct, "Sell", CALL, "10", "6.00", mult="100",
           date="2025-12-01")
    assert investments.option_position_problems(conn, acct,
                                                as_of=AFTER_EXPIRY) == []


# ---------------------------------------------------------------------------
# (d) the NULL-kind twin of every one of those paths
# ---------------------------------------------------------------------------
def test_null_kind_security_is_not_an_option(conn):
    _security(conn, STOCK)                       # row exists, kind is NULL
    assert investments.security_kind(conn, STOCK) is None
    assert investments.is_option(conn, STOCK) is False
    assert investments.option_terms(conn, STOCK) is None


def test_an_unrecorded_symbol_is_not_an_option_either(conn):
    assert investments.is_option(conn, "NOSUCH") is False
    assert investments.contract_multiplier(conn, "NOSUCH") == Decimal(1)


def test_null_kind_valuation_is_unchanged(conn, acct):
    _security(conn, STOCK)
    _trade(conn, acct, "Buy", STOCK, "100", "50.00")
    assert investments.contract_multiplier(conn, STOCK) == Decimal(1)
    assert _value(conn, acct, STOCK, "50.00") == 5000_00


def test_null_kind_short_is_unchanged(conn, acct):
    """A short STOCK position was already a negative market value; the option
    work must not have changed that arithmetic."""
    _security(conn, STOCK)
    _trade(conn, acct, "ShtSell", STOCK, "100", "50.00")
    assert _value(conn, acct, STOCK, "52.00") == -5200_00


def test_null_kind_share_adjustment_still_works(conn, acct):
    _security(conn, STOCK)
    _trade(conn, acct, "Buy", STOCK, "100", "50.00")
    txn_id = investments.record_share_adjustment(conn, acct, STOCK,
                                                 "2025-11-30", "2")
    assert txn_id
    investments.rebuild_holdings(conn, acct)
    held = {h["symbol"]: Decimal(h["quantity"])
            for h in investments.list_holdings(conn, acct)}
    assert held == {STOCK: Decimal(102)}


def test_null_kind_rename_identity_is_unchanged(conn, acct):
    """The ordinary ticker rename -- the thing the identity machinery exists for
    -- still unions both spellings."""
    _security(conn, "ACMEOLD")
    _security(conn, STOCK)
    investments.add_alias(conn, "ACMEOLD", STOCK)
    assert investments.share_identity(conn, STOCK) == [STOCK, "ACMEOLD"]
    assert investments.share_identity(conn, "ACMEOLD") == [STOCK, "ACMEOLD"]


def test_null_kind_rename_still_shares_one_price_series(conn, acct):
    _security(conn, "ACMEOLD")
    _security(conn, STOCK)
    investments.add_alias(conn, "ACMEOLD", STOCK)
    investments.record_price(conn, "ACMEOLD", "2025-06-30", "40.00")
    _trade(conn, acct, "Buy", STOCK, "100", "50.00")
    # The price recorded under the OLD spelling still values the new one.
    at = investments.holding_values_at(conn, acct, as_of="2025-11-30")
    assert at[STOCK] == 4000_00


def test_null_kind_reconciliation_offers_an_adjustment(conn, acct):
    _security(conn, STOCK)
    _trade(conn, acct, "Buy", STOCK, "100", "50.00")
    summary = _reconcile(conn, acct, STOCK, "2025-11-30", "102")
    assert summary["kind"] is None
    assert summary["adjustment_allowed"] is True
    assert summary["difference"] == Decimal(2)


def test_null_kind_positions_are_never_option_problems(conn, acct):
    _security(conn, STOCK)
    _trade(conn, acct, "Buy", STOCK, "100", "50.00")
    assert investments.option_position_problems(conn, acct,
                                                as_of="2030-01-01") == []


def test_a_classified_non_option_is_also_left_alone(conn, acct):
    """'equity' is not 'option': only the explicit option kind changes any
    behavior."""
    _security(conn, STOCK, kind=instruments.Kind.EQUITY.value)
    _trade(conn, acct, "Buy", STOCK, "100", "50.00")
    assert investments.is_option(conn, STOCK) is False
    assert investments.contract_multiplier(conn, STOCK) == Decimal(1)
    assert _value(conn, acct, STOCK, "50.00") == 5000_00
    assert investments.option_position_problems(conn, acct,
                                                as_of="2030-01-01") == []
    summary = investments.share_reconcile_summary(conn, acct, STOCK,
                                                  "2025-11-30", "100")
    assert summary["adjustment_allowed"] is True


def test_a_preferred_share_spelling_is_equity_not_an_option(conn, acct):
    """'ACME-A' looks option-ish to a loose pattern and is not one. The
    classifier says equity (:mod:`mammon.instruments`) and nothing here keys off
    the SPELLING, only off the stored kind -- so it takes the stock path."""
    pfd = "ACME-A"
    assert instruments.parse_option(pfd) is None
    assert instruments.classify(pfd) is instruments.Kind.EQUITY
    _security(conn, pfd, kind=instruments.Kind.EQUITY.value)
    _trade(conn, acct, "Buy", pfd, "100", "25.00")
    assert investments.is_option(conn, pfd) is False
    assert _value(conn, acct, pfd, "25.00") == 2500_00

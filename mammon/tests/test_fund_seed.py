"""Seeding a fund target from the last mix the user stated (SRD 5.8f-1).

All data is SYNTHETIC. The shapes under test are the ones a real ledger holds:
a same-day exchange between funds, a payroll split, a share-class conversion the
plan performed, and dividends swept back into the funds that paid them.

The reported motivation: "A useful default for the target values could be the
initial percentages, the contribution percentages. Those can change over the
life of the investments, as can the populated funds."
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import investments, ledger, rebalance
from mammon.tests import fresh_db

AS_OF = "2026-06-30"
OPEN = "2015-01-01"


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "seed.db")
    yield c
    c.close()


def _price(conn, symbol, date, price):
    investments.record_price(conn, symbol, date, Decimal(price))


def _trade(conn, acct, date, action, symbol, qty, price):
    cents = int(Decimal(qty) * Decimal(price) * 100)
    investments.record_investment(conn, acct, date, action, symbol=symbol,
                                  quantity=Decimal(qty), price=Decimal(price),
                                  amount=cents)
    _price(conn, symbol, date, price)
    return cents


def _cash_in(conn, acct, date, cents, action="XIn"):
    investments.record_investment(conn, acct, date, action, amount=cents)


@pytest.fixture
def plan(conn):
    """An account funded once, then reallocated: 60/30/10 becomes 50/30/20."""
    acct = ledger.create_account(conn, "ZZ Plan", "investment",
                                 opening_balance=0, opening_date=OPEN)
    _cash_in(conn, acct, "2016-01-04", 100_000_00)
    _trade(conn, acct, "2016-01-05", "Buy", "ZZAAA", "600", "100.00")
    _trade(conn, acct, "2016-01-05", "Buy", "ZZBBB", "300", "100.00")
    _trade(conn, acct, "2016-01-05", "Buy", "ZZCCC", "100", "100.00")
    # ...and later moved 10% of the account out of AAA into CCC.
    _trade(conn, acct, "2020-03-09", "Sell", "ZZAAA", "100", "100.00")
    _trade(conn, acct, "2020-03-09", "Buy", "ZZCCC", "100", "100.00")
    investments.rebuild_holdings(conn, acct)
    for sym in ("ZZAAA", "ZZBBB", "ZZCCC"):
        _price(conn, sym, AS_OF, "100.00")
    return acct


# --- a reallocation is a statement ------------------------------------------
def test_the_last_reallocation_is_read_as_the_mix_that_was_wanted(conn, plan):
    seed = rebalance.suggest_fund_lines(conn, plan, AS_OF)
    assert seed is not None
    assert seed.source == "reallocation"
    assert seed.stated_on == "2020-03-09"
    assert seed.lines == {"ZZAAA": Decimal("50"), "ZZBBB": Decimal("30"),
                          "ZZCCC": Decimal("20")}


def test_a_reallocation_beats_an_older_contribution(conn, plan):
    """Both statements exist here; the later one is what the user last said."""
    seed = rebalance.suggest_fund_lines(conn, plan, AS_OF)
    assert seed.stated_on == "2020-03-09"          # not the 2016 purchase


def test_an_exchange_too_small_to_be_a_decision_is_ignored(conn, plan):
    """A residual swept out of a closing fund is housekeeping. Below the
    threshold it must not overwrite the real statement behind it."""
    _trade(conn, plan, "2021-06-01", "Sell", "ZZBBB", "5", "100.00")
    _trade(conn, plan, "2021-06-01", "Buy", "ZZAAA", "5", "100.00")
    investments.rebuild_holdings(conn, plan)
    seed = rebalance.suggest_fund_lines(conn, plan, AS_OF)
    assert seed.stated_on == "2020-03-09"


def test_an_exchange_over_the_threshold_becomes_the_new_statement(conn, plan):
    _trade(conn, plan, "2021-06-01", "Sell", "ZZBBB", "100", "100.00")
    _trade(conn, plan, "2021-06-01", "Buy", "ZZAAA", "100", "100.00")
    investments.rebuild_holdings(conn, plan)
    seed = rebalance.suggest_fund_lines(conn, plan, AS_OF)
    assert seed.stated_on == "2021-06-01"
    assert seed.lines["ZZAAA"] == Decimal("60")
    assert seed.lines["ZZBBB"] == Decimal("20")


# --- a contribution is a statement too --------------------------------------
@pytest.fixture
def payroll(conn):
    """Money arrives from outside and is split the same way every time."""
    acct = ledger.create_account(conn, "ZZ Payroll", "investment",
                                 opening_balance=0, opening_date=OPEN)
    for date in ("2024-01-05", "2024-01-19", "2024-02-02"):
        _cash_in(conn, acct, date, 1_000_00)
        _trade(conn, acct, date, "Buy", "ZZDDD", "7", "100.00")
        _trade(conn, acct, date, "Buy", "ZZEEE", "3", "100.00")
    investments.rebuild_holdings(conn, acct)
    for sym in ("ZZDDD", "ZZEEE"):
        _price(conn, sym, AS_OF, "100.00")
    return acct


def test_how_new_money_was_split_is_the_election(conn, payroll):
    seed = rebalance.suggest_fund_lines(conn, payroll, AS_OF)
    assert seed.source == "contribution"
    assert seed.stated_on == "2024-02-02"
    assert seed.lines == {"ZZDDD": Decimal("70"), "ZZEEE": Decimal("30")}


def test_money_arriving_on_the_cash_side_funds_a_statement_too(conn):
    """A brokerage deposit is an ordinary ledger row, not an investment action,
    and a purchase it paid for says just as much."""
    acct = ledger.create_account(conn, "ZZ Brokerage", "investment",
                                 opening_balance=0, opening_date=OPEN)
    ledger.add_transaction(conn, acct, "2024-05-01", 20_000_00, payee="Deposit")
    _trade(conn, acct, "2024-05-02", "Buy", "ZZFFF", "150", "100.00")
    _trade(conn, acct, "2024-05-02", "Buy", "ZZGGG", "50", "100.00")
    investments.rebuild_holdings(conn, acct)
    for sym in ("ZZFFF", "ZZGGG"):
        _price(conn, sym, AS_OF, "100.00")
    seed = rebalance.suggest_fund_lines(conn, acct, AS_OF)
    assert seed.source == "contribution"
    assert seed.lines == {"ZZFFF": Decimal("75"), "ZZGGG": Decimal("25")}


def test_dividends_swept_back_into_the_funds_are_not_a_statement(conn, payroll):
    """The split follows what each fund PAID, not what the user wants. Nothing
    entered the account, so it says nothing about the mix."""
    investments.record_investment(conn, payroll, "2025-12-20", "Div",
                                  symbol="ZZDDD", amount=300_00)
    investments.record_investment(conn, payroll, "2025-12-20", "Div",
                                  symbol="ZZEEE", amount=700_00)
    _trade(conn, payroll, "2025-12-28", "Buy", "ZZDDD", "3", "100.00")
    _trade(conn, payroll, "2025-12-28", "Buy", "ZZEEE", "7", "100.00")
    investments.rebuild_holdings(conn, payroll)
    seed = rebalance.suggest_fund_lines(conn, payroll, AS_OF)
    assert seed.stated_on == "2024-02-02"          # the payroll, not the sweep
    assert seed.lines == {"ZZDDD": Decimal("70"), "ZZEEE": Decimal("30")}


# --- what the plan did, rather than the user --------------------------------
def test_a_share_class_conversion_is_not_a_reallocation(conn, payroll):
    """One fund out, one in, the same money: its "weights" are just yesterday's,
    and reading it as a statement would bury the election behind it."""
    cents = 21 * 100 * 100
    investments.record_investment(conn, payroll, "2026-01-02", "Sell",
                                  symbol="ZZDDD", quantity=Decimal("21"),
                                  price=Decimal("100.00"), amount=cents)
    investments.record_investment(conn, payroll, "2026-01-02", "Buy",
                                  symbol="ZZDDX", quantity=Decimal("21"),
                                  price=Decimal("100.00"), amount=cents)
    investments.rebuild_holdings(conn, payroll)
    _price(conn, "ZZDDX", AS_OF, "100.00")
    seed = rebalance.suggest_fund_lines(conn, payroll, AS_OF)
    assert seed.source == "contribution"
    assert seed.stated_on == "2024-02-02"
    # ...and the weight follows the money into the fund's new name, which is the
    # only reason this account can still be seeded at all.
    assert seed.lines == {"ZZDDX": Decimal("70"), "ZZEEE": Decimal("30")}


# --- staleness --------------------------------------------------------------
def test_a_statement_that_misses_a_fund_you_now_hold_is_refused(conn, plan):
    """Seeding the funds it names would target the rest at zero, which reads as
    "sell all of it" -- a worse answer than offering nothing."""
    _cash_in(conn, plan, "2022-04-01", 20_000_00)
    _trade(conn, plan, "2022-04-04", "Buy", "ZZNEW", "200", "100.00")
    investments.rebuild_holdings(conn, plan)
    _price(conn, "ZZNEW", AS_OF, "100.00")
    assert rebalance.suggest_fund_lines(conn, plan, AS_OF) is None


def test_a_residual_holding_does_not_make_a_statement_stale(conn, plan):
    """Under 1% of the account is a dividend crumb, not a position the user has
    an opinion about."""
    _trade(conn, plan, "2022-04-04", "Buy", "ZZTINY", "1", "100.00")
    investments.rebuild_holdings(conn, plan)
    _price(conn, "ZZTINY", AS_OF, "100.00")
    seed = rebalance.suggest_fund_lines(conn, plan, AS_OF)
    assert seed is not None and seed.stated_on == "2020-03-09"


def test_a_fund_since_sold_is_dropped_and_the_rest_rescaled(conn, plan):
    """The other direction is safe: not holding something is not an instruction
    to buy it."""
    _trade(conn, plan, "2021-06-01", "Sell", "ZZCCC", "200", "100.00")
    investments.rebuild_holdings(conn, plan)
    seed = rebalance.suggest_fund_lines(conn, plan, AS_OF)
    assert seed.dropped == ("ZZCCC",)
    # 50 and 30 of the old statement, rescaled over the two that remain.
    assert seed.lines == {"ZZAAA": Decimal("62.5"), "ZZBBB": Decimal("37.5")}
    assert sum(seed.lines.values()) == Decimal("100")


# --- the arithmetic the window depends on -----------------------------------
def test_weights_always_add_to_exactly_one_hundred(conn):
    """Seven funds rounded independently land on 99.9 or 100.1, and the window
    would then report the seed it just wrote as not adding up."""
    acct = ledger.create_account(conn, "ZZ Sevenths", "investment",
                                 opening_balance=0, opening_date=OPEN)
    _cash_in(conn, acct, "2024-03-01", 70_000_00)
    for i in range(7):
        _trade(conn, acct, "2024-03-02", "Buy", f"ZZS{i}", "100", "100.00")
        _price(conn, f"ZZS{i}", AS_OF, "100.00")
    investments.rebuild_holdings(conn, acct)
    seed = rebalance.suggest_fund_lines(conn, acct, AS_OF)
    assert sum(seed.lines.values()) == Decimal("100")
    assert sorted(seed.lines.values()) == (
        [Decimal("14.2")] + [Decimal("14.3")] * 6)


def test_an_account_with_one_holding_has_nothing_to_divide(conn):
    acct = ledger.create_account(conn, "ZZ Single", "investment",
                                 opening_balance=0, opening_date=OPEN)
    _cash_in(conn, acct, "2024-03-01", 10_000_00)
    _trade(conn, acct, "2024-03-02", "Buy", "ZZONE", "100", "100.00")
    investments.rebuild_holdings(conn, acct)
    _price(conn, "ZZONE", AS_OF, "100.00")
    assert rebalance.suggest_fund_lines(conn, acct, AS_OF) is None


def test_an_account_that_never_stated_anything_gets_no_seed(conn):
    acct = ledger.create_account(conn, "ZZ Silent", "investment",
                                 opening_balance=0, opening_date=OPEN)
    investments.record_investment(conn, acct, "2024-03-02", "ShrsIn",
                                  symbol="ZZIN1", quantity=Decimal("100"),
                                  price=Decimal("100.00"), amount=0)
    investments.record_investment(conn, acct, "2024-03-02", "ShrsIn",
                                  symbol="ZZIN2", quantity=Decimal("100"),
                                  price=Decimal("100.00"), amount=0)
    investments.rebuild_holdings(conn, acct)
    for sym in ("ZZIN1", "ZZIN2"):
        _price(conn, sym, AS_OF, "100.00")
    assert rebalance.suggest_fund_lines(conn, acct, AS_OF) is None


def test_the_seed_says_where_it_came_from(conn, plan):
    seed = rebalance.suggest_fund_lines(conn, plan, AS_OF)
    assert "reallocation on 2020-03-09" in seed.describe()

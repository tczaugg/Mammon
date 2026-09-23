"""Targets stated PER FUND, and the class mix they imply (SRD 5.8f-1).

All data is SYNTHETIC: ZZ-prefixed tickers, invented accounts, round numbers.

The user's own practice, and the reason this exists: "throughout my career I
chose funds and came up with a target percent that each fund would be in my
portfolio. Then rebalancing was easy. I just rebalanced the funds. That had the
correct buy-low/sell-high effect and restored my asset class balance."

A class weight is not tradeable -- a blended fund moves three classes at once --
so the app could only ever issue instructions the user then had to decompose.
Stated per fund the instruction is executable, and the class mix becomes a
computed check.
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mammon import investments, ledger, portfolio, rebalance, security_mix
from mammon.tests import fresh_db

AS_OF = "2026-06-30"
OPEN = "2020-01-01"


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "fund_targets.db")
    yield c
    c.close()


def _buy(conn, acct, sym, qty, price):
    investments.record_investment(conn, acct, "2020-01-02", "Buy", symbol=sym,
                                  quantity=Decimal(qty), price=Decimal(price),
                                  amount=int(Decimal(qty) * Decimal(price) * 100))
    investments.rebuild_holdings(conn, acct)
    investments.record_price(conn, sym, AS_OF, Decimal(price))


@pytest.fixture
def world(conn):
    """A 401(k) of one BLENDED fund plus one pure index, and a taxable account
    held outside the target -- the shape the whole design is about."""
    plan = ledger.create_account(conn, "ZZ 401k", "investment",
                                 opening_balance=100_000_00, opening_date=OPEN)
    _buy(conn, plan, "ZZBAL", "700", "100.00")     # $70,000, 70/25/5
    _buy(conn, plan, "ZZIDX", "300", "100.00")     # $30,000, pure domestic
    security_mix.set_mixture(conn, "ZZBAL",
                             {"domestic_stock": 70, "bond": 25, "cash": 5})
    portfolio.set_security(conn, "ZZIDX", asset_class="domestic_stock")

    taxable = ledger.create_account(conn, "ZZ Taxable", "investment",
                                    opening_balance=50_000_00, opening_date=OPEN)
    _buy(conn, taxable, "ZZBND", "500", "100.00")  # $50,000 bonds
    portfolio.set_security(conn, "ZZBND", asset_class="bond")

    tid = rebalance.create_target(conn, "Funds")
    rebalance.set_target_accounts(conn, tid, [plan])
    return {"plan": plan, "taxable": taxable, "target": tid}


# --- storing the weights ----------------------------------------------------
def test_fund_lines_round_trip_and_clear(conn, world):
    tid, plan = world["target"], world["plan"]
    assert rebalance.has_fund_lines(conn, tid) is False
    rebalance.set_fund_line(conn, tid, plan, "ZZBAL", 60)
    rebalance.set_fund_line(conn, tid, plan, "ZZIDX", 40)
    assert rebalance.fund_lines(conn, tid) == {
        (plan, "ZZBAL"): Decimal("60"), (plan, "ZZIDX"): Decimal("40")}
    assert rebalance.has_fund_lines(conn, tid) is True
    rebalance.set_fund_line(conn, tid, plan, "ZZIDX", 0)       # zero clears
    assert (plan, "ZZIDX") not in rebalance.fund_lines(conn, tid)
    with pytest.raises(ValueError):
        rebalance.set_fund_line(conn, tid, plan, "ZZBAL", -5)
    with pytest.raises(ValueError):
        rebalance.set_fund_line(conn, tid, plan, "  ", 10)


def test_a_weight_is_of_its_own_account(conn, world):
    """Percent is of the ACCOUNT, because an account is the unit you can trade
    within -- money does not move between a 401(k) and a taxable account, and a
    broker's auto-rebalance is configured the same way."""
    tid, plan = world["target"], world["plan"]
    rebalance.set_fund_lines(conn, tid, {(plan, "ZZBAL"): 60, (plan, "ZZIDX"): 40})
    r = rebalance.fund_target(conn, tid, AS_OF)
    assert r.account_totals[plan] == 100_000_00
    by_symbol = {f.symbol: f for f in r.funds}
    assert by_symbol["ZZBAL"].target_cents == 60_000_00
    assert by_symbol["ZZIDX"].target_cents == 40_000_00


# --- the instruction, which is the point ------------------------------------
def test_the_move_is_stated_in_the_fund_you_can_actually_trade(conn, world):
    tid, plan = world["target"], world["plan"]
    rebalance.set_fund_lines(conn, tid, {(plan, "ZZBAL"): 60, (plan, "ZZIDX"): 40})
    r = rebalance.fund_target(conn, tid, AS_OF)
    by_symbol = {f.symbol: f for f in r.funds}

    assert by_symbol["ZZBAL"].current_pct == Decimal("70")
    assert by_symbol["ZZBAL"].move_cents == -10_000_00
    assert by_symbol["ZZBAL"].action == "sell"
    assert by_symbol["ZZIDX"].move_cents == 10_000_00
    assert by_symbol["ZZIDX"].action == "buy"
    # Selling the overweight fund and buying the underweight one IS buy-low /
    # sell-high; no rule computes it, it falls out of holding the weights fixed.
    assert sum(f.move_cents for f in r.funds) == 0


def test_weights_that_do_not_add_to_a_hundred_are_shown_not_normalized(conn,
                                                                       world):
    """The same rule target_total follows: normalizing a forgotten line turns a
    mistake into a plausible, wrong target."""
    tid, plan = world["target"], world["plan"]
    rebalance.set_fund_lines(conn, tid, {(plan, "ZZBAL"): 50, (plan, "ZZIDX"): 30})
    r = rebalance.fund_target(conn, tid, AS_OF)
    assert r.account_pct_totals[plan] == Decimal("80")
    assert r.accounts_complete == [(plan, Decimal("80"))]
    rebalance.set_fund_line(conn, tid, plan, "ZZIDX", 50)
    assert rebalance.fund_target(conn, tid, AS_OF).accounts_complete == []


# --- the class mix, computed forward ---------------------------------------
def test_a_blended_fund_is_decomposed_into_its_classes(conn, world):
    """The whole difficulty, dissolved: the app never has to turn a class
    instruction into fund trades, it turns fund weights into a class figure."""
    tid, plan = world["target"], world["plan"]
    rebalance.set_fund_lines(conn, tid, {(plan, "ZZBAL"): 60, (plan, "ZZIDX"): 40})
    r = rebalance.fund_target(conn, tid, AS_OF)

    # Now: $70k of 70/25/5 plus $30k of pure domestic.
    assert r.current_blend["domestic_stock"] == 49_000_00 + 30_000_00
    assert r.current_blend["bond"] == 17_500_00
    assert r.current_blend["cash"] == 3_500_00
    # At target: $60k of 70/25/5 plus $40k of pure domestic.
    assert r.blend["domestic_stock"] == 42_000_00 + 40_000_00
    assert r.blend["bond"] == 15_000_00
    assert r.blend["cash"] == 3_000_00
    # Rebalancing the FUNDS moved the classes, which is the point.
    pct = r.pct(r.blend)
    assert pct["domestic_stock"] == pytest.approx(Decimal("82"))


def test_the_blend_and_the_allocation_report_agree_about_a_fund(conn, world):
    """Both defer to security_mix, so a fund cannot be two things at once."""
    tid, plan = world["target"], world["plan"]
    rebalance.set_fund_lines(conn, tid, {(plan, "ZZBAL"): 70, (plan, "ZZIDX"): 30})
    r = rebalance.fund_target(conn, tid, AS_OF)
    alloc = portfolio.allocation(conn, account_ids=[plan], as_of=AS_OF)
    by_class = {s.key: s.value for s in alloc.by_class}
    assert r.current_blend == by_class


# --- the big picture, which is the feature ----------------------------------
def test_it_says_what_the_change_does_to_everything_you_own(conn, world):
    tid, plan = world["target"], world["plan"]
    rebalance.set_fund_lines(conn, tid, {(plan, "ZZBAL"): 60, (plan, "ZZIDX"): 40})
    r = rebalance.fund_target(conn, tid, AS_OF)

    before, after = r.pct(r.portfolio_before), r.pct(r.portfolio_after)
    # $150,000 all in: the 401(k)'s $100k and the taxable account's $50k.
    assert sum(r.portfolio_before.values()) == 150_000_00
    assert before["domestic_stock"] == pytest.approx(Decimal("52.6667"), abs=Decimal("0.01"))
    assert after["domestic_stock"] == pytest.approx(Decimal("54.6667"), abs=Decimal("0.01"))
    assert after["bond"] < before["bond"]


def test_an_account_outside_the_target_is_left_exactly_alone(conn, world):
    """It is not being rebalanced, so its holdings appear in both the before and
    the after unchanged. A design that quietly moved it would be proposing
    trades in an account the user did not select."""
    tid, plan, taxable = world["target"], world["plan"], world["taxable"]
    rebalance.set_fund_lines(conn, tid, {(plan, "ZZBAL"): 60, (plan, "ZZIDX"): 40})
    r = rebalance.fund_target(conn, tid, AS_OF)
    assert all(f.account_id == plan for f in r.funds), "no trades outside the target"
    # The taxable account's $50k of bonds is in both totals.
    assert r.portfolio_before["bond"] - r.blend.get("bond", 0) >= 50_000_00 - 15_000_00


def test_moving_to_target_never_invents_or_destroys_money(conn, world):
    tid, plan = world["target"], world["plan"]
    rebalance.set_fund_lines(conn, tid, {(plan, "ZZBAL"): 60, (plan, "ZZIDX"): 40})
    r = rebalance.fund_target(conn, tid, AS_OF)
    assert sum(r.portfolio_before.values()) == sum(r.portfolio_after.values())
    assert sum(r.blend.values()) == r.account_totals[plan]


# --- cash, which the user holds by accident rather than by choice -----------
def test_weights_summing_to_a_hundred_sweep_the_idle_cash_in(conn, world):
    """Reported: "in my own retirement accounts I wouldn't hold cash
    deliberately. I have cash in my accounts because I've rarely bothered
    to manually reinvest my dividends." Weights that add to 100 spend it."""
    tid = world["target"]
    roth = ledger.create_account(conn, "ZZ Roth", "investment",
                                 opening_balance=20_000_00, opening_date=OPEN)
    _buy(conn, roth, "ZZIDX", "150", "100.00")     # $15,000 invested, $5,000 idle
    rebalance.set_target_accounts(conn, tid, [roth])
    rebalance.set_fund_lines(conn, tid, {(roth, "ZZIDX"): 100})

    r = rebalance.fund_target(conn, tid, AS_OF)
    assert r.current_blend["cash"] == 5_000_00          # the un-reinvested dividends
    assert r.blend.get("cash", 0) == 0                  # ...spent by the target
    fund = next(f for f in r.funds if f.symbol == "ZZIDX")
    assert fund.move_cents == 5_000_00
    assert fund.action == "buy"


def test_weights_summing_to_less_leave_the_remainder_as_visible_cash(conn, world):
    """Not silently scaled up to fill the account: 90% of the funds means 10%
    stays in cash, and saying so is how the user notices they meant 100."""
    tid = world["target"]
    roth = ledger.create_account(conn, "ZZ Roth", "investment",
                                 opening_balance=20_000_00, opening_date=OPEN)
    _buy(conn, roth, "ZZIDX", "150", "100.00")
    rebalance.set_target_accounts(conn, tid, [roth])
    rebalance.set_fund_lines(conn, tid, {(roth, "ZZIDX"): 90})
    r = rebalance.fund_target(conn, tid, AS_OF)
    assert r.blend["cash"] == 2_000_00                  # 10% of $20,000
    assert r.accounts_complete == [(roth, Decimal("90"))]


# --- a fund the target names but does not yet hold --------------------------
def test_a_fund_you_do_not_own_yet_is_a_buy_not_an_omission(conn, world):
    tid, plan = world["target"], world["plan"]
    rebalance.set_fund_lines(conn, tid, {(plan, "ZZBAL"): 50, (plan, "ZZIDX"): 30,
                                         (plan, "ZZNEW"): 20})
    r = rebalance.fund_target(conn, tid, AS_OF)
    new = next(f for f in r.funds if f.symbol == "ZZNEW")
    assert new.current_cents == 0
    assert new.target_cents == 20_000_00
    assert new.action == "buy"


def test_a_holding_with_no_target_reads_as_sell_it_all(conn, world):
    """A fund you hold and did not name is one you are leaving: its target is
    zero, so the report says sell it rather than passing over it in silence."""
    tid, plan = world["target"], world["plan"]
    rebalance.set_fund_lines(conn, tid, {(plan, "ZZIDX"): 100})
    r = rebalance.fund_target(conn, tid, AS_OF)
    bal = next(f for f in r.funds if f.symbol == "ZZBAL")
    assert bal.target_pct == Decimal("0")
    assert bal.move_cents == -70_000_00
    assert bal.action == "sell"

"""What an option DOES at the end -- the basis rules (SRD 5.8e-7).

Item 5b of the instrument taxonomy: an option contract has four endings a share
has no analogue for, and in three of them the premium does not become a gain or
a loss, it MOVES. Part 1.2 of ``docs/instrument_taxonomy.md`` states the rules
this file proves, one test per row of its table (IRS Pub 550, "Options"):

* long call exercised   -> shares acquired at ``strike + premium``
* written put assigned  -> shares acquired at ``strike - premium``
* long put exercised    -> shares sold, the premium REDUCING the proceeds
* written call assigned -> shares sold at ``strike + premium``
* written option expiring worthless -> the whole premium is a SHORT-term gain,
  however long the contract was open, and NO shares appear
* long option expiring worthless -> the whole premium is the loss
* a cash-settled index contract has no deliverable at all, so an exercise is
  refused rather than inventing a share leg

Every case is end to end over a real temporary ledger: create the account and
the contract, record the legs through the public writers, then assert the
positions, the lots, the basis and the realized gain the replay produces. The
cash is asserted too, because the one thing that makes the two-leg model worth
its complexity is that a rolled premium must not be paid twice.

All data is synthetic -- invented tickers on an invented issuer.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, instruments, investments, ledger, securities

# One synthetic issuer, two OSI contracts on it, and one cash-settled index
# contract. None of these is a real ticker.
STOCK = "ACME"
CALL = "ACME  260116C00050000"          # strike 50, call
PUT = "ACME  260116P00045000"           # strike 45, put
INDEX = "XDEX  260116C05000000"         # cash-settled, strike 5000, call

OPEN = "2025-11-03"
CLOSE = "2025-12-01"
EXPIRY = "2026-01-16"
LONG_AGO = "2024-06-03"                 # > 1 year before EXPIRY, for term tests


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "lifecycle.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Brokerage", "investment",
                                 opening_balance=0)


def _security(conn, symbol, kind=None, **terms):
    conn.execute("INSERT OR IGNORE INTO securities(symbol, name) VALUES (?,?)",
                 (symbol, symbol))
    conn.commit()
    if kind is not None:
        securities.set_kinds(conn, [dict(symbol=symbol, kind=kind,
                                         kind_source="user", **terms)])


def _option(conn, symbol, strike="50", right="C", multiplier="100",
            underlying=STOCK, expiration=EXPIRY):
    """One classified contract. ``underlying=None`` leaves the column NULL,
    which is what a cash-settled index contract looks like."""
    terms = dict(multiplier=multiplier, expiration=expiration, strike=strike,
                 option_right=right)
    if underlying is not None:
        terms["underlying"] = underlying
    _security(conn, symbol, kind=instruments.Kind.OPTION.value, **terms)


def _leg(conn, account_id, date, action, symbol, contracts, amount):
    """One raw option leg: ``amount`` is signed cents, positive = premium in."""
    return investments.record_investment(conn, account_id, date, action,
                                         symbol=symbol, quantity=contracts,
                                         amount=amount)


def _positions(conn, account_id):
    return investments._replay_positions(conn, account_id)


def _held(conn, account_id):
    """``{symbol: quantity}`` from the rebuilt holdings table."""
    return {h["symbol"]: Decimal(h["quantity"])
            for h in investments.list_holdings(conn, account_id)}


def _reconciled(conn, account_id, symbol, statement_date, stated):
    """Clear every row for ``symbol`` and re-summarize, so the computed ending
    share count is the replay's own answer rather than an untouched zero."""
    s = investments.share_reconcile_summary(conn, account_id, symbol,
                                            statement_date, stated)
    for row in s["uncleared_rows"]:
        investments.set_investment_cleared(conn, row["id"])
    return investments.share_reconcile_summary(conn, account_id, symbol,
                                               statement_date, stated)


# ---------------------------------------------------------------------------
# 1. Bought to open, sold to close -- the ordinary trade, x the multiplier
# ---------------------------------------------------------------------------
def test_buy_to_open_then_sell_to_close_realizes_the_premium_difference(
        conn, acct):
    """The baseline: no basis moves anywhere, the gain is just the premium
    difference. Two contracts at $2.00 cost $400 because each controls 100
    shares -- the multiplier is in the cash, which is why the gain is $300 and
    not $3."""
    _option(conn, CALL)
    _leg(conn, acct, OPEN, investments.OPTION_BUY_TO_OPEN, CALL, "2", -400_00)
    _leg(conn, acct, CLOSE, investments.OPTION_SELL_TO_CLOSE, CALL, "2", 700_00)
    investments.rebuild_holdings(conn, acct)

    pos = _positions(conn, acct)[CALL]
    assert pos.qty == Decimal(0)
    assert pos.cost == 0
    assert pos.realized == 300_00
    (gain,) = pos.gains
    assert (gain.proceeds, gain.basis, gain.gain) == (700_00, 400_00, 300_00)
    assert (gain.acquired, gain.sold, gain.term) == (OPEN, CLOSE, "short")
    # Nothing of the underlying was ever touched.
    assert STOCK not in _positions(conn, acct)
    assert investments.investment_cash(conn, acct) == 300_00


# ---------------------------------------------------------------------------
# 2. Written and left to expire -- the whole premium, short-term, no shares
# ---------------------------------------------------------------------------
def test_written_option_expiring_worthless_is_a_short_term_gain_of_the_premium(
        conn, acct):
    """The writer's best day. Two things are easy to get wrong and both are
    asserted: the gain is the WHOLE premium (nothing was paid to end the
    obligation), and it is SHORT-term even though this contract was open for
    nineteen months, because writing an option starts no holding period."""
    _option(conn, PUT, strike="45", right="P")
    _leg(conn, acct, LONG_AGO, investments.OPTION_SELL_TO_OPEN, PUT, "1", 300_00)
    investments.rebuild_holdings(conn, acct)
    assert _held(conn, acct)[PUT] == Decimal(-1)

    investments.record_option_expiration(conn, acct, EXPIRY, PUT)
    investments.rebuild_holdings(conn, acct)

    row = investments.list_investment_txns(conn, acct)[-1]
    assert row["action"] == investments.OPTION_EXPIRE_SHORT

    positions = _positions(conn, acct)
    pos = positions[PUT]
    assert pos.qty == Decimal(0)
    assert pos.cost == 0
    assert pos.realized == 300_00
    (gain,) = pos.gains
    assert (gain.proceeds, gain.basis, gain.gain) == (300_00, 0, 300_00)
    assert (gain.acquired, gain.sold) == (LONG_AGO, EXPIRY)
    assert gain.term == "short"          # NOT "long": term_override, Pub 550

    # No phantom shares: an expiring put does not deliver anything.
    assert STOCK not in positions
    assert _held(conn, acct).get(STOCK) is None
    # An expiry moves no cash either -- the premium was banked when it was sold.
    assert investments.investment_cash(conn, acct) == 300_00
    # ...and the share reconciliation agrees with the replay that the contract
    # count is back to zero (the reason expiry is two actions, not one).
    assert _reconciled(conn, acct, PUT, EXPIRY, 0)["difference"] == Decimal(0)


def test_long_option_expiring_worthless_loses_exactly_the_premium(conn, acct):
    """The mirror, and the one place a price column must be ignored: expired
    worthless means proceeds of zero by definition, so the loss is the whole
    premium paid."""
    _option(conn, CALL)
    _leg(conn, acct, OPEN, investments.OPTION_BUY_TO_OPEN, CALL, "4", -840_00)
    investments.rebuild_holdings(conn, acct)

    investments.record_option_expiration(conn, acct, EXPIRY, CALL)
    investments.rebuild_holdings(conn, acct)

    assert (investments.list_investment_txns(conn, acct)[-1]["action"]
            == investments.OPTION_EXPIRE)
    pos = _positions(conn, acct)[CALL]
    assert pos.qty == Decimal(0)
    assert pos.realized == -840_00
    (gain,) = pos.gains
    assert (gain.proceeds, gain.basis, gain.gain) == (0, 840_00, -840_00)
    assert STOCK not in _positions(conn, acct)
    assert investments.investment_cash(conn, acct) == -840_00


# ---------------------------------------------------------------------------
# 3. Written put assigned -- shares arrive with the premium OFF their basis
# ---------------------------------------------------------------------------
def test_assigned_put_creates_a_share_lot_with_basis_reduced_by_the_premium(
        conn, acct):
    """Pub 550: the premium received for writing the put is not income, it comes
    off the basis of the shares put to you. 100 shares at a $45 strike, written
    for $300, cost $4,200 -- and the cash that actually moved is the $4,500 the
    broker took, not a cent more."""
    _option(conn, PUT, strike="45", right="P")
    _leg(conn, acct, OPEN, investments.OPTION_SELL_TO_OPEN, PUT, "1", 300_00)

    res = investments.record_option_exercise(conn, acct, EXPIRY, PUT)
    investments.rebuild_holdings(conn, acct)

    assert res["action"] == investments.OPTION_ASSIGN
    assert res["share_action"] == "Buy"
    assert res["shares"] == Decimal(100)
    assert res["option_basis"] == -300_00        # a credit, carried negative
    assert res["share_amount"] == 4200_00

    positions = _positions(conn, acct)
    opt = positions[PUT]
    assert (opt.qty, opt.cost, opt.realized) == (Decimal(0), 0, 0)
    assert opt.gains == []                # an assignment books NO gain

    stock = positions[STOCK]
    assert stock.qty == Decimal(100)
    assert stock.cost == 4200_00
    (lot,) = stock.lots
    assert (lot.qty, lot.cost, lot.date) == (Decimal(100), 4200_00, EXPIRY)
    assert lot.txn_id == res["share_txn_id"]

    # Premium in, then $4,500 out for the shares: the rolled premium is not
    # paid twice.
    assert investments.investment_cash(conn, acct) == 300_00 - 4500_00


# ---------------------------------------------------------------------------
# 4. Long call exercised -- shares arrive at strike PLUS the premium
# ---------------------------------------------------------------------------
def test_exercised_call_gives_the_share_lot_a_basis_of_strike_plus_premium(
        conn, acct):
    """The headline rule. $200 premium plus a $50 strike on 100 shares is a
    $5,200 basis, the option books nothing at all, and its holding period is
    discarded -- the lot is dated the day of the exercise."""
    _option(conn, CALL, strike="50", right="C")
    _leg(conn, acct, OPEN, investments.OPTION_BUY_TO_OPEN, CALL, "1", -200_00)

    res = investments.record_option_exercise(conn, acct, CLOSE, CALL)
    investments.rebuild_holdings(conn, acct)

    assert res["action"] == investments.OPTION_EXERCISE
    assert res["share_action"] == "Buy"
    assert (res["option_basis"], res["share_amount"]) == (200_00, 5200_00)

    positions = _positions(conn, acct)
    opt = positions[CALL]
    assert (opt.qty, opt.cost, opt.realized) == (Decimal(0), 0, 0)
    assert opt.gains == []

    stock = positions[STOCK]
    assert stock.qty == Decimal(100)
    assert stock.cost == 5200_00
    (lot,) = stock.lots
    assert (lot.qty, lot.cost, lot.date) == (Decimal(100), 5200_00, CLOSE)
    assert _held(conn, acct) == {STOCK: Decimal(100)}

    # $200 for the contract and $5,000 for the shares -- $5,200 total, which is
    # also the basis. Exactly once.
    assert investments.investment_cash(conn, acct) == -5200_00


# ---------------------------------------------------------------------------
# 5. A covered call -- the shares are untouched until the assignment
# ---------------------------------------------------------------------------
def test_covered_call_leaves_the_share_lot_alone_until_it_is_assigned(
        conn, acct):
    """Writing a call against stock you already own changes nothing about that
    stock: same quantity, same lot, same basis, same acquisition date. Only the
    assignment touches it, and then the premium ADDS to the proceeds."""
    _option(conn, CALL, strike="50", right="C")
    _security(conn, STOCK)
    buy_id = investments.record_investment(conn, acct, OPEN, "Buy",
                                           symbol=STOCK, quantity="100",
                                           amount=-4800_00)
    _leg(conn, acct, OPEN, investments.OPTION_SELL_TO_OPEN, CALL, "1", 300_00)
    investments.rebuild_holdings(conn, acct)

    before = _positions(conn, acct)[STOCK]
    assert before.qty == Decimal(100)
    assert before.cost == 4800_00
    (lot,) = before.lots
    assert (lot.qty, lot.cost, lot.date, lot.txn_id) == (
        Decimal(100), 4800_00, OPEN, buy_id)
    assert before.realized == 0
    assert _held(conn, acct)[CALL] == Decimal(-1)

    res = investments.record_option_exercise(conn, acct, EXPIRY, CALL)
    investments.rebuild_holdings(conn, acct)

    assert res["action"] == investments.OPTION_ASSIGN
    assert res["share_action"] == "Sell"
    assert (res["option_basis"], res["share_amount"]) == (-300_00, 5300_00)

    positions = _positions(conn, acct)
    assert positions[CALL].gains == []           # the premium is not income
    assert positions[CALL].realized == 0

    stock = positions[STOCK]
    assert stock.qty == Decimal(0)
    assert stock.realized == 5300_00 - 4800_00
    (gain,) = stock.gains
    assert (gain.proceeds, gain.basis, gain.gain) == (5300_00, 4800_00, 500_00)
    assert (gain.acquired, gain.sold) == (OPEN, EXPIRY)   # the SHARES' dates

    # $4,800 out for the stock, $300 of premium in, $5,000 in at the strike.
    # The premium is counted ONCE: it is real money that arrived when the call
    # was written, and the option leg cancels only the copy of it that the
    # $5,300 share proceeds carry.
    assert investments.investment_cash(conn, acct) == (
        -4800_00 + 300_00 + 5000_00)


# ---------------------------------------------------------------------------
# 6. A cash-settled index option -- there is nothing to deliver
# ---------------------------------------------------------------------------
def test_cash_settled_index_option_closes_with_no_share_leg(conn, acct):
    """An index contract has no deliverable, so there is no share lot to roll a
    premium into and an exercise must be REFUSED rather than inventing one.
    Closing it is an ordinary premium-difference trade."""
    _option(conn, INDEX, strike="5000", right="C", underlying=None)
    _leg(conn, acct, OPEN, investments.OPTION_BUY_TO_OPEN, INDEX, "1", -1500_00)
    investments.rebuild_holdings(conn, acct)

    with pytest.raises(ValueError, match="cash-settled"):
        investments.record_option_exercise(conn, acct, EXPIRY, INDEX)
    # The refusal wrote nothing.
    assert len(investments.list_investment_txns(conn, acct)) == 1

    _leg(conn, acct, CLOSE, investments.OPTION_SELL_TO_CLOSE, INDEX, "1",
         2200_00)
    investments.rebuild_holdings(conn, acct)

    positions = _positions(conn, acct)
    assert list(positions) == [INDEX]            # no share leg anywhere
    assert positions[INDEX].qty == Decimal(0)
    assert positions[INDEX].realized == 700_00
    assert _held(conn, acct) == {}


# ---------------------------------------------------------------------------
# 7. Regression: a stock and two contracts on it are THREE instruments
# ---------------------------------------------------------------------------
def test_stock_and_two_contracts_are_three_independent_positions(conn, acct):
    """The fusion regression, restated over the life-cycle vocabulary: one
    issuer, three instruments, three price series, three reconciliations. If any
    of them ever folds into another, the share counts and the reconciliations
    are the first places it shows."""
    _security(conn, STOCK)
    _option(conn, CALL, strike="50", right="C")
    _option(conn, PUT, strike="45", right="P")

    investments.record_investment(conn, acct, OPEN, "Buy", symbol=STOCK,
                                  quantity="100", price="50.00",
                                  amount=-5000_00)
    _leg(conn, acct, OPEN, investments.OPTION_BUY_TO_OPEN, CALL, "10", -4200_00)
    _leg(conn, acct, OPEN, investments.OPTION_SELL_TO_OPEN, PUT, "3", 525_00)
    investments.rebuild_holdings(conn, acct)

    assert _held(conn, acct) == {STOCK: Decimal(100), CALL: Decimal(10),
                                 PUT: Decimal(-3)}

    # Three price series, three different prices on the same day.
    investments.record_price(conn, STOCK, OPEN, "50.00")
    investments.record_price(conn, CALL, OPEN, "4.20")
    investments.record_price(conn, PUT, OPEN, "1.75")
    values = {v.symbol: v.market_value
              for v in investments.holding_values(conn, acct)}
    assert values == {STOCK: 5000_00, CALL: 4200_00, PUT: -525_00}

    # Three reconciliations, each seeing only its own instrument's rows.
    for symbol, stated in ((STOCK, 100), (CALL, 10), (PUT, -3)):
        summary = _reconciled(conn, acct, symbol, OPEN, stated)
        assert summary["computed_ending_qty"] == Decimal(stated)
        assert summary["difference"] == Decimal(0)

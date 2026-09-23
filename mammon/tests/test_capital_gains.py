"""Tests for mammon.reports.capital_gains -- unrealized gains per OPEN tax lot,
the long/short holding-period clock, and the tax annotation.

All data here is SYNTHETIC: made-up tickers, made-up accounts, round numbers.
Every case pins a fixed ``as_of`` so the holding-period countdown is deterministic
whatever day the suite runs on.
"""
import datetime as _dt
from decimal import Decimal

import pytest

from mammon import db, investments, ledger
from mammon.reports.capital_gains import (
    DEFAULT_LONG_TERM_RATE,
    DEFAULT_ORDINARY_INCOME_RATE,
    LotTaxRow,
    capital_gains,
    combine_long_term,
)
from mammon.tests import fresh_db

AS_OF = "2026-06-30"
LONG = Decimal("0.15")
SHORT = Decimal("0.24")


def _days_before(n: int) -> str:
    return (_dt.date.fromisoformat(AS_OF) - _dt.timedelta(days=n)).isoformat()


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "capgains.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "Taxable Brokerage", "investment",
                                 opening_balance=0)


def _buy(conn, account_id, date, symbol, shares, price):
    """A synthetic purchase: ``shares`` at ``price`` dollars, cash out."""
    amount = -int((Decimal(str(shares)) * Decimal(str(price)) * 100)
                  .to_integral_value())
    return investments.record_investment(conn, account_id, date, "Buy",
                                         symbol=symbol, quantity=str(shares),
                                         price=str(price), amount=amount)


def _sell(conn, account_id, date, symbol, shares, price):
    amount = int((Decimal(str(shares)) * Decimal(str(price)) * 100)
                 .to_integral_value())
    return investments.record_investment(conn, account_id, date, "Sell",
                                         symbol=symbol, quantity=str(shares),
                                         price=str(price), amount=amount)


def _by_symbol(report):
    out = {}
    for row in report.lots:
        out.setdefault(row.symbol, []).append(row)
    return out


def _run(conn, **kw):
    kw.setdefault("long_term_rate", LONG)
    kw.setdefault("short_term_rate", SHORT)
    return capital_gains(conn, AS_OF, **kw)


# --- 1. long vs short -------------------------------------------------------

def test_lot_held_400_days_is_long_and_one_held_100_days_is_short(conn, account):
    _buy(conn, account, _days_before(400), "AAA", 100, "10.00")
    _buy(conn, account, _days_before(100), "BBB", 100, "10.00")
    investments.record_price(conn, "AAA", AS_OF, "12.00")
    investments.record_price(conn, "BBB", AS_OF, "12.00")
    investments.rebuild_holdings(conn, account)

    rows = _by_symbol(_run(conn))
    assert [r.term for r in rows["AAA"]] == ["long"]
    assert [r.term for r in rows["BBB"]] == ["short"]

    aaa, bbb = rows["AAA"][0], rows["BBB"][0]
    assert aaa.quantity == Decimal("100")
    assert aaa.cost_basis == 1000_00
    assert aaa.market_value == 1200_00
    assert aaa.unrealized == 200_00
    assert aaa.days_to_long == 0
    assert aaa.extra_tax_if_sold_now == 0          # no deadline to beat
    assert aaa.tax_at_short_rate is None
    assert bbb.days_to_long > 0
    assert bbb.extra_tax_if_sold_now > 0


def test_totals_split_long_and_short_gains(conn, account):
    _buy(conn, account, _days_before(400), "AAA", 100, "10.00")
    _buy(conn, account, _days_before(100), "BBB", 100, "10.00")
    investments.record_price(conn, "AAA", AS_OF, "12.00")    # +$200 long
    investments.record_price(conn, "BBB", AS_OF, "15.00")    # +$500 short
    investments.rebuild_holdings(conn, account)

    rep = _run(conn)
    assert rep.total_long_term_gain == 200_00
    assert rep.total_short_term_gain == 500_00
    assert rep.total_long_term_loss == 0
    assert rep.total_short_term_loss == 0
    assert rep.total_cost_basis == 2000_00
    assert rep.total_market_value == 2700_00
    assert rep.total_unrealized == 700_00
    # Selling the whole short book today: $500 gain at 24% instead of 15%.
    assert rep.total_extra_tax_if_sold_now == 45_00
    assert rep.long_term_rate == LONG and rep.short_term_rate == SHORT
    assert rep.as_of == AS_OF


# --- 2. the becomes-long date and the countdown -----------------------------

def test_becomes_long_is_acquisition_plus_one_year_and_a_day(conn, account):
    acquired = _days_before(100)
    _buy(conn, account, acquired, "CCC", 10, "50.00")
    investments.record_price(conn, "CCC", AS_OF, "50.00")
    investments.rebuild_holdings(conn, account)

    row = _run(conn).lots[0]
    a = _dt.date.fromisoformat(acquired)
    expected = a.replace(year=a.year + 1) + _dt.timedelta(days=1)
    assert row.acquired == acquired
    assert row.long_term_on == expected.isoformat()
    assert row.days_to_long == (expected - _dt.date.fromisoformat(AS_OF)).days
    # ...and that is what the domain-layer rule says, not a second copy of it.
    assert row.long_term_on == investments.long_term_date(acquired)


def test_days_remaining_counts_down_against_as_of(conn, account):
    _buy(conn, account, _days_before(100), "CCC", 10, "50.00")
    investments.record_price(conn, "CCC", AS_OF, "50.00")
    investments.rebuild_holdings(conn, account)

    row = _run(conn).lots[0]
    later = capital_gains(conn, "2026-07-30", long_term_rate=LONG,
                          short_term_rate=SHORT).lots[0]
    assert later.long_term_on == row.long_term_on
    assert later.days_to_long == row.days_to_long - 30


# --- 3. the boundary --------------------------------------------------------

def test_one_day_short_of_a_year_and_a_day_is_still_short(conn, account):
    # Acquired exactly one year before as_of: the sale falls ON the anniversary,
    # which the law (and RealizedGain.term) counts as SHORT.
    _buy(conn, account, "2025-06-30", "EDGE", 10, "10.00")
    investments.record_price(conn, "EDGE", AS_OF, "11.00")
    investments.rebuild_holdings(conn, account)

    row = _run(conn).lots[0]
    assert row.term == "short"
    assert row.long_term_on == "2026-07-01"
    assert row.days_to_long == 1


def test_one_day_past_the_anniversary_is_long(conn, account):
    _buy(conn, account, "2025-06-29", "EDGE", 10, "10.00")
    investments.record_price(conn, "EDGE", AS_OF, "11.00")
    investments.rebuild_holdings(conn, account)

    row = _run(conn).lots[0]
    assert row.term == "long"
    assert row.long_term_on == "2026-06-30"        # today; already crossed
    assert row.days_to_long == 0


def test_term_matches_the_realized_gain_rule_across_the_boundary(conn, account):
    for acquired in ("2025-06-28", "2025-06-29", "2025-06-30", "2025-07-01"):
        expected = investments.RealizedGain(
            sale_txn_id=None, symbol="X", acquired=acquired, sold=AS_OF,
            quantity=Decimal(0), proceeds=0, basis=0).term
        a = _dt.date.fromisoformat(acquired)
        crossed = _dt.date.fromisoformat(
            investments.long_term_date(acquired)) <= _dt.date.fromisoformat(AS_OF)
        assert ("long" if crossed else "short") == expected, (acquired, a)


# --- 4. the tax annotation --------------------------------------------------

def test_extra_tax_on_a_short_term_gain_is_the_rate_difference(conn, account):
    _buy(conn, account, _days_before(100), "GAIN", 100, "10.00")
    investments.record_price(conn, "GAIN", AS_OF, "20.00")
    investments.rebuild_holdings(conn, account)

    row = _run(conn).lots[0]
    assert row.unrealized == 1000_00
    assert row.tax_at_long_rate == 150_00
    assert row.tax_at_short_rate == 240_00
    assert row.extra_tax_if_sold_now == 90_00
    # gain * (short - long), rounded HALF_UP at the cents boundary
    assert row.extra_tax_if_sold_now == int(
        (Decimal(row.unrealized) * (SHORT - LONG)).quantize(Decimal("1")))
    assert "24%" in row.annotation and "15%" in row.annotation
    assert "more tax" in row.annotation
    assert row.long_term_on in row.annotation


def test_rounding_is_half_up_at_the_cents_boundary(conn, account):
    # A gain of $100.50 at a 1% spread is 100.5 cents -> 101, not 100.
    _buy(conn, account, _days_before(10), "ROUND", 1, "100.00")
    investments.record_price(conn, "ROUND", AS_OF, "200.50")
    investments.rebuild_holdings(conn, account)

    row = capital_gains(conn, AS_OF, long_term_rate=Decimal("0.10"),
                        short_term_rate=Decimal("0.11")).lots[0]
    assert row.unrealized == 100_50
    assert row.tax_at_long_rate == 10_05          # 1005.0 cents exactly
    assert row.tax_at_short_rate == 11_06         # 1105.5 -> 1106, HALF_UP
    assert row.extra_tax_if_sold_now == 1_01


def test_short_term_loss_is_annotated_the_other_way(conn, account):
    _buy(conn, account, _days_before(100), "LOSS", 100, "20.00")
    investments.record_price(conn, "LOSS", AS_OF, "10.00")
    investments.rebuild_holdings(conn, account)

    rep = _run(conn)
    row = rep.lots[0]
    assert row.unrealized == -1000_00
    assert row.is_loss and not row.is_gain
    # Negative = a tax BENEFIT, and the short-term one is worth more.
    assert row.tax_at_short_rate == -240_00
    assert row.tax_at_long_rate == -150_00
    assert row.extra_tax_if_sold_now == -90_00
    assert "SHORT-term loss" in row.annotation
    assert "ordinary income" in row.annotation
    # Losses land in their own bucket, not netted against gains.
    assert rep.total_short_term_loss == -1000_00
    assert rep.total_short_term_gain == 0
    assert rep.total_short_term_net == -1000_00
    assert rep.total_extra_tax_if_sold_now == -90_00


def test_long_term_loss_says_no_deadline_and_carries_no_short_figure(conn, account):
    _buy(conn, account, _days_before(400), "LTL", 100, "20.00")
    investments.record_price(conn, "LTL", AS_OF, "10.00")
    investments.rebuild_holdings(conn, account)

    rep = _run(conn)
    row = rep.lots[0]
    assert row.term == "long"
    assert row.tax_at_short_rate is None
    assert row.extra_tax_if_sold_now == 0
    assert "No deadline" in row.annotation
    assert rep.total_long_term_loss == -1000_00
    assert rep.total_extra_tax_if_sold_now == 0


def test_defaults_are_documented_assumptions_the_caller_overrides(conn, account):
    _buy(conn, account, _days_before(30), "DEF", 100, "10.00")
    investments.record_price(conn, "DEF", AS_OF, "20.00")
    investments.rebuild_holdings(conn, account)

    default = capital_gains(conn, AS_OF).lots[0]
    assert default.tax_at_long_rate == int(
        Decimal(default.unrealized) * DEFAULT_LONG_TERM_RATE)
    assert default.tax_at_short_rate == int(
        Decimal(default.unrealized) * DEFAULT_ORDINARY_INCOME_RATE)
    # ...and a caller with a real bracket gets their own numbers.
    mine = capital_gains(conn, AS_OF, long_term_rate=Decimal("0.20"),
                         short_term_rate=Decimal("0.37")).lots[0]
    assert mine.tax_at_long_rate == 200_00
    assert mine.tax_at_short_rate == 370_00
    assert mine.extra_tax_if_sold_now == 170_00


def test_unpriced_lot_reports_no_gain_and_no_tax(conn, account):
    _buy(conn, account, _days_before(100), "NOPRICE", 10, "10.00")
    investments.rebuild_holdings(conn, account)

    row = _run(conn).lots[0]
    assert row.price is None
    assert row.unrealized is None
    assert row.tax_at_long_rate is None
    assert row.tax_at_short_rate is None
    assert row.extra_tax_if_sold_now is None
    assert "cannot be estimated" in row.annotation


# --- 5. partial sells leave the right open lots -----------------------------

def test_two_partial_sells_leave_the_right_fifo_lots(conn, account):
    investments.set_lot_method(conn, account, "fifo")
    _buy(conn, account, "2025-01-05", "FIFO", 100, "10.00")
    _buy(conn, account, "2025-06-05", "FIFO", 100, "20.00")
    _buy(conn, account, "2025-09-05", "FIFO", 100, "30.00")
    _sell(conn, account, "2025-11-05", "FIFO", 150, "25.00")
    _sell(conn, account, "2026-01-05", "FIFO", 25, "25.00")
    investments.record_price(conn, "FIFO", AS_OF, "40.00")
    investments.rebuild_holdings(conn, account)

    lots = investments.open_lots(conn, account)["FIFO"]
    assert [(l.acquired, l.quantity, l.cost_basis) for l in lots] == [
        ("2025-06-05", Decimal("25"), 500_00),
        ("2025-09-05", Decimal("100"), 3000_00),
    ]
    # ...and the report shows exactly those, oldest first.
    rows = _run(conn).lots
    assert [(r.acquired, r.quantity, r.cost_basis) for r in rows] == [
        ("2025-06-05", Decimal("25"), 500_00),
        ("2025-09-05", Decimal("100"), 3000_00),
    ]
    assert [r.term for r in rows] == ["long", "short"]
    assert [r.market_value for r in rows] == [1000_00, 4000_00]
    # The lots reconcile with the average-cost position the Holdings window shows.
    holding = investments.compute_holdings(conn, account)["FIFO"]
    assert sum(l.quantity for l in lots) == holding.qty
    assert sum(l.cost_basis for l in lots) == holding.cost


def test_lifo_partial_sells_leave_the_oldest_lots(conn, account):
    investments.set_lot_method(conn, account, "lifo")
    _buy(conn, account, "2025-01-05", "LIFO", 100, "10.00")
    _buy(conn, account, "2025-06-05", "LIFO", 100, "20.00")
    _sell(conn, account, "2025-11-05", "LIFO", 60, "25.00")
    _sell(conn, account, "2026-01-05", "LIFO", 40, "25.00")
    investments.record_price(conn, "LIFO", AS_OF, "30.00")
    investments.rebuild_holdings(conn, account)

    lots = investments.open_lots(conn, account)["LIFO"]
    assert [(l.acquired, l.quantity) for l in lots] == [
        ("2025-01-05", Decimal("100"))]
    assert lots[0].cost_basis == 1000_00


def test_average_cost_sell_keeps_the_remaining_lot_dates(conn, account):
    # Average cost re-spreads the basis but does NOT rewrite acquisition dates --
    # the holding period of the shares still on hand survives the sale.
    _buy(conn, account, "2025-01-05", "AVG", 100, "10.00")
    _buy(conn, account, "2026-05-05", "AVG", 100, "20.00")
    _sell(conn, account, "2026-06-05", "AVG", 50, "25.00")
    investments.record_price(conn, "AVG", AS_OF, "25.00")
    investments.rebuild_holdings(conn, account)

    lots = investments.open_lots(conn, account)["AVG"]
    assert [l.acquired for l in lots] == ["2025-01-05", "2026-05-05"]
    assert sum(l.quantity for l in lots) == Decimal("150")
    holding = investments.compute_holdings(conn, account)["AVG"]
    assert sum(l.cost_basis for l in lots) == holding.cost
    rows = _run(conn).lots
    assert [r.term for r in rows] == ["long", "short"]
    assert sum(r.market_value for r in rows) == 3750_00       # 150 x $25


def test_a_fully_sold_security_has_no_open_lots(conn, account):
    _buy(conn, account, "2025-01-05", "GONE", 10, "10.00")
    _sell(conn, account, "2025-09-05", "GONE", 10, "15.00")
    investments.record_price(conn, "GONE", AS_OF, "15.00")
    investments.rebuild_holdings(conn, account)

    assert "GONE" not in investments.open_lots(conn, account)
    assert _run(conn).lots == []


# --- 6. end to end ----------------------------------------------------------

def test_full_life_cycle_renders_every_row_with_correct_numbers(conn, account):
    """Buy, buy again, sell part, price it, report it."""
    investments.set_lot_method(conn, account, "fifo")
    old = _days_before(500)      # long by as_of
    new = _days_before(50)       # short by as_of
    _buy(conn, account, old, "ZZZ", 100, "10.00")        # $1,000
    _buy(conn, account, new, "ZZZ", 100, "30.00")        # $3,000
    _sell(conn, account, _days_before(20), "ZZZ", 40, "35.00")   # fifo: 40 of the old lot
    investments.record_price(conn, "ZZZ", AS_OF, "40.00")
    investments.rebuild_holdings(conn, account)

    rep = _run(conn)
    assert len(rep.lots) == 2
    old_row, new_row = rep.lots
    assert old_row.account_name == "Taxable Brokerage"
    assert old_row.symbol == "ZZZ"
    assert old_row.acquired == old
    assert old_row.quantity == Decimal("60")
    assert old_row.cost_basis == 600_00
    assert old_row.price == Decimal("40.00")
    assert old_row.market_value == 2400_00
    assert old_row.unrealized == 1800_00
    assert old_row.term == "long"
    assert old_row.days_to_long == 0
    assert old_row.tax_at_long_rate == 270_00
    assert old_row.extra_tax_if_sold_now == 0

    assert new_row.acquired == new
    assert new_row.quantity == Decimal("100")
    assert new_row.cost_basis == 3000_00
    assert new_row.market_value == 4000_00
    assert new_row.unrealized == 1000_00
    assert new_row.term == "short"
    assert new_row.long_term_on == investments.long_term_date(new)
    assert new_row.days_to_long == 365 - 50 + 1
    assert new_row.tax_at_short_rate == 240_00
    assert new_row.extra_tax_if_sold_now == 90_00

    assert rep.total_cost_basis == 3600_00
    assert rep.total_market_value == 6400_00
    assert rep.total_long_term_gain == 1800_00
    assert rep.total_short_term_gain == 1000_00
    assert rep.total_unrealized == 2800_00
    assert rep.total_extra_tax_if_sold_now == 90_00
    # Every row reconciles: shares and basis tie to the position.
    holding = investments.compute_holdings(conn, account)["ZZZ"]
    assert sum(r.quantity for r in rep.lots) == holding.qty
    assert sum(r.cost_basis for r in rep.lots) == holding.cost


def test_accounts_are_selectable_and_hidden_ones_excluded(conn, account):
    other = ledger.create_account(conn, "Roth IRA", "investment", opening_balance=0)
    hidden = ledger.create_account(conn, "Old Plan", "investment", opening_balance=0)
    ledger.set_account_hidden(conn, hidden, True)
    checking = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    ledger.add_transaction(conn, checking, "2026-01-02", -20_00, payee="Coffee")
    for aid, sym in ((account, "ONE"), (other, "TWO"), (hidden, "THREE")):
        _buy(conn, aid, _days_before(30), sym, 10, "10.00")
        investments.record_price(conn, sym, AS_OF, "11.00")
        investments.rebuild_holdings(conn, aid)

    assert sorted({r.symbol for r in _run(conn).lots}) == ["ONE", "TWO"]
    assert sorted({r.symbol for r in _run(conn, include_hidden=True).lots}) == [
        "ONE", "THREE", "TWO"]
    only = _run(conn, account_ids=[other])
    assert [r.symbol for r in only.lots] == ["TWO"]
    assert only.account_ids == [other]


def test_price_override_beats_the_recorded_history(conn, account):
    _buy(conn, account, _days_before(30), "OVR", 10, "10.00")
    investments.record_price(conn, "OVR", AS_OF, "11.00")
    investments.rebuild_holdings(conn, account)

    row = _run(conn, prices={"OVR": Decimal("20.00")}).lots[0]
    assert row.price == Decimal("20.00")
    assert row.market_value == 200_00
    assert row.unrealized == 100_00


def test_bad_as_of_is_rejected(conn, account):
    with pytest.raises(ValueError):
        capital_gains(conn, "06/30/2026")


# --- 8. tax-deferred accounts ----------------------------------------------
# User, 2026-09-19: "401K, IRA and Roth IRA do not pay capital gains. But then
# you only have the name of the account to go by." The answer is the account's
# own ``tax_treatment`` column, never its name.
#
# These tests originally pinned the FIRST answer: show the sheltered lots with a
# blank term. The user rejected it on sight -- "On the Capital Gains report, if
# they're not taxed, don't put them in the report. That is just a lot of
# clutter." -- so they now pin the opposite: no row, no cent, one footnote line.

@pytest.fixture
def retirement(conn):
    aid = ledger.create_account(conn, "Rollover IRA", "investment",
                                opening_balance=0)
    ledger.update_account(conn, aid, tax_treatment="deferred")
    return aid


@pytest.mark.parametrize("treatment", ["deferred", "roth", "special"])
def test_sheltered_account_contributes_no_rows_at_all(conn, treatment):
    aid = ledger.create_account(conn, "Sheltered", "investment",
                                opening_balance=0)
    ledger.update_account(conn, aid, tax_treatment=treatment)
    _buy(conn, aid, _days_before(30), "SHEL", 100, "10.00")
    investments.record_price(conn, "SHEL", AS_OF, "20.00")
    investments.rebuild_holdings(conn, aid)

    report = _run(conn)
    assert report.lots == []                  # not one row, blank term or not
    # ...and no cent of it reaches ANY total, including the two that are not
    # about tax at all.
    assert report.total_cost_basis == 0
    assert report.total_market_value == 0
    assert report.total_unrealized == 0
    assert report.total_short_term_gain == 0
    assert report.total_long_term_gain == 0
    assert report.total_unknown_term == 0
    assert report.total_extra_tax_if_sold_now == 0
    # The single surviving trace: the name, for the footnote.
    assert report.excluded_accounts == ["Sheltered"]


def test_sheltered_gain_moves_no_total_and_leaves_only_a_footnote(
        conn, account, retirement):
    # Identical lots, one taxable and one in an IRA: the report is the taxable
    # one, entire. The IRA's $1,000 must not show up anywhere in the numbers.
    _buy(conn, account, _days_before(30), "TAXD", 100, "10.00")
    _buy(conn, retirement, _days_before(30), "SHEL", 100, "10.00")
    for sym, aid in (("TAXD", account), ("SHEL", retirement)):
        investments.record_price(conn, sym, AS_OF, "20.00")
        investments.rebuild_holdings(conn, aid)

    report = _run(conn)
    assert [r.symbol for r in report.lots] == ["TAXD"]
    assert report.total_short_term_gain == 1000_00        # the taxable lot only
    assert report.total_short_term_loss == 0
    assert report.total_long_term_gain == 0
    assert report.total_unknown_term == 0
    assert report.total_extra_tax_if_sold_now == 90_00    # 1000 * (24% - 15%)
    assert report.total_cost_basis == 1000_00             # the taxable lot only
    assert report.total_market_value == 2000_00
    assert report.total_unrealized == 1000_00             # NOT 2000_00
    assert report.excluded_accounts == ["Rollover IRA"]


def test_the_excluded_accounts_are_named_in_a_footnote(conn, account,
                                                       retirement):
    # Dropping an account silently is worse than the clutter it removes, so the
    # names come back as one footnote sentence -- and never as row data.
    roth = ledger.create_account(conn, "Roth IRA", "investment",
                                 opening_balance=0)
    ledger.update_account(conn, roth, tax_treatment="roth")
    _buy(conn, account, _days_before(30), "TAXD", 100, "10.00")
    _buy(conn, retirement, _days_before(30), "SHEL", 100, "10.00")
    _buy(conn, roth, _days_before(400), "ROTH", 100, "10.00")
    for sym, aid in (("TAXD", account), ("SHEL", retirement), ("ROTH", roth)):
        investments.record_price(conn, sym, AS_OF, "20.00")
        investments.rebuild_holdings(conn, aid)

    report = _run(conn)
    note = report.exclusion_note
    assert note == ("Excluded (not subject to capital gains): "
                    "Rollover IRA, Roth IRA")
    # The wording is a footnote, not a row: nothing in the rows mentions it.
    assert all("Excluded" not in r.account_name for r in report.lots)
    assert all(r.account_name == "Taxable Brokerage" for r in report.lots)


def test_no_excluded_accounts_means_no_footnote(conn, account):
    _buy(conn, account, _days_before(30), "TAXD", 100, "10.00")
    investments.record_price(conn, "TAXD", AS_OF, "20.00")
    investments.rebuild_holdings(conn, account)

    report = _run(conn)
    assert report.excluded_accounts == []
    assert report.exclusion_note is None


def test_taxable_and_unset_accounts_are_unchanged(conn, account):
    # A file that never recorded a treatment must read exactly as before: the
    # conservative default is TAXABLE, and nothing is inferred from the name.
    named = ledger.create_account(conn, "Old 401k Rollover IRA", "investment",
                                  opening_balance=0)
    _buy(conn, named, _days_before(30), "NAME", 100, "10.00")
    investments.record_price(conn, "NAME", AS_OF, "20.00")
    investments.rebuild_holdings(conn, named)

    report = _run(conn)
    assert report.lots[0].term == "short"
    assert report.total_short_term_gain == 1000_00
    assert report.excluded_accounts == []
    assert report.exclusion_note is None

# ---------------------------------------------------------------------------
# combining the long-term lots (reported)
# ---------------------------------------------------------------------------
def _reinvesting(conn, *, quarters: int = 12, symbol: str = "ZZREIT"):
    """An account that has reinvested a dividend every quarter for years.

    The reported shape: "if we had been reinvesting dividends, each of those
    would have become a lot ... dozens of small lots, all now long term except
    the last year's worth". A real position in the user's own ledger carries 92
    open lots, 88 of them long.
    """
    acct = ledger.create_account(conn, "ZZ Taxable Reinvest", "investment",
                                 opening_balance=100_000_00,
                                 opening_date="2015-01-01")
    # Anchored to the REPORT date, not a fixed year: the last reinvestment
    # lands a quarter before it, so the tail really is short-term and the fixture
    # cannot quietly become all-long as AS_OF moves.
    last = _dt.date.fromisoformat(AS_OF) - _dt.timedelta(days=91)
    start = last - _dt.timedelta(days=91 * (quarters - 1))
    for i in range(quarters):
        day = start + _dt.timedelta(days=91 * i)
        investments.record_investment(
            conn, acct, day.isoformat(), "ReinvDiv", symbol=symbol,
            quantity=Decimal("10"), price=Decimal("20.00"), amount=200_00)
    investments.rebuild_holdings(conn, acct)
    investments.record_price(conn, symbol, AS_OF, Decimal("30.00"))
    return acct


def test_every_reinvestment_is_its_own_lot(conn):
    """The premise. A reinvested dividend is a purchase, so it opens a lot --
    which is why a long-held reinvesting position accumulates dozens."""
    acct = _reinvesting(conn, quarters=12)
    lots = investments.open_lots(conn, acct)["ZZREIT"]
    assert len(lots) == 12
    assert all(lot.acquired for lot in lots)


def test_long_term_lots_combine_and_short_term_ones_do_not(conn):
    """Reported: "all the lots that are long-term already should be combined",
    while short-term lots keep a row each because their crossover dates differ.
    """
    acct = _reinvesting(conn, quarters=12)
    flat = capital_gains(conn, AS_OF, account_ids=[acct],
                            group_long_term=False)
    grouped = capital_gains(conn, AS_OF, account_ids=[acct])

    flat_long = [r for r in flat.lots if r.term == "long"]
    flat_short = [r for r in flat.lots if r.term == "short"]
    assert len(flat_long) > 1, "the fixture must have several long lots"
    assert flat_short, "and a short tail"

    grouped_long = [r for r in grouped.lots if r.term == "long"]
    grouped_short = [r for r in grouped.lots if r.term == "short"]
    assert len(grouped_long) == 1, "one combined long row"
    assert grouped_long[0].lot_count == len(flat_long)
    assert grouped_long[0].is_combined
    # The short tail is untouched: those dates are the information.
    assert len(grouped_short) == len(flat_short)
    assert all(r.lot_count == 1 for r in grouped_short)


def test_combining_changes_no_total(conn):
    """It sums the same cents into fewer rows. This report is documented as
    having to tie to the Holdings window, so nothing may move."""
    acct = _reinvesting(conn, quarters=16)
    flat = capital_gains(conn, AS_OF, account_ids=[acct],
                            group_long_term=False)
    grouped = capital_gains(conn, AS_OF, account_ids=[acct])
    for field in ("total_cost_basis", "total_market_value",
                  "total_long_term_gain", "total_long_term_loss",
                  "total_short_term_gain", "total_short_term_loss",
                  "total_unknown_term", "total_extra_tax_if_sold_now",
                  "total_unrealized"):
        assert getattr(flat, field) == getattr(grouped, field), field
    assert (sum(r.quantity for r in grouped.lots)
            == sum(r.quantity for r in flat.lots))


def test_a_combined_row_carries_the_span_it_covers(conn):
    """Its `acquired` is the oldest of the lots and `acquired_last` the newest,
    so the row can say what it stands for instead of wearing one lot's date."""
    acct = _reinvesting(conn, quarters=12)
    grouped = capital_gains(conn, AS_OF, account_ids=[acct])
    row = next(r for r in grouped.lots if r.is_combined)
    flat = capital_gains(conn, AS_OF, account_ids=[acct],
                            group_long_term=False)
    longs = [r for r in flat.lots if r.term == "long"]
    assert row.acquired == min(r.acquired for r in longs)
    assert row.acquired_last == max(r.acquired for r in longs)
    assert row.acquired < row.acquired_last
    assert row.days_to_long == 0
    assert row.extra_tax_if_sold_now == 0


def test_long_gains_and_long_losses_are_never_summed_together(conn):
    """The one thing summing destroys. A long lot at a loss netted against one
    at a gain hides the harvestable loss -- in the report whose job in December
    is to find it."""
    acct = ledger.create_account(conn, "ZZ Mixed", "investment",
                                 opening_balance=100_000_00,
                                 opening_date="2015-01-01")
    # Two long lots of one security: one bought high, one bought low.
    for price in ("50.00", "10.00"):
        investments.record_investment(
            conn, acct, "2020-06-01", "Buy", symbol="ZZMIXED",
            quantity=Decimal("100"), price=Decimal(price),
            amount=int(Decimal(price) * 100 * 100))
    investments.rebuild_holdings(conn, acct)
    investments.record_price(conn, "ZZMIXED", AS_OF, Decimal("30.00"))

    grouped = capital_gains(conn, AS_OF, account_ids=[acct])
    rows = [r for r in grouped.lots if r.symbol == "ZZMIXED"]
    assert len(rows) == 2, "a gain row and a loss row, never one netted row"
    assert any(r.is_gain for r in rows)
    assert any(r.is_loss for r in rows)
    # The loss is still visible at its full size, which is the whole point.
    assert min(r.unrealized for r in rows) == -20_00 * 100


def test_lots_of_unknown_term_are_never_folded_into_the_long_ones(conn):
    """A lot whose acquisition date was never recorded is a question, not a long
    holding; combining would answer it by assertion."""
    rows = [
        LotTaxRow(account_id=1, account_name="A", symbol="ZZX",
                     security_name=None, acquired=None, quantity=Decimal("5"),
                     cost_basis=100, price=None, market_value=200,
                     unrealized=100, term="unknown", long_term_on=None,
                     days_to_long=None),
        LotTaxRow(account_id=1, account_name="A", symbol="ZZX",
                     security_name=None, acquired="2018-01-01",
                     quantity=Decimal("5"), cost_basis=100, price=None,
                     market_value=200, unrealized=100, term="long",
                     long_term_on="2019-01-02", days_to_long=0),
    ]
    out = combine_long_term(rows)
    assert len(out) == 2
    assert {r.term for r in out} == {"unknown", "long"}
    assert all(r.lot_count == 1 for r in out)


def test_a_single_long_lot_is_left_exactly_as_it_was(conn):
    """Nothing to combine, nothing changed -- including the object itself, so a
    one-lot position cannot start rendering as a group of one."""
    acct = _reinvesting(conn, quarters=1)
    flat = capital_gains(conn, AS_OF, account_ids=[acct],
                            group_long_term=False)
    grouped = capital_gains(conn, AS_OF, account_ids=[acct])
    assert len(grouped.lots) == len(flat.lots)
    assert all(not r.is_combined for r in grouped.lots)

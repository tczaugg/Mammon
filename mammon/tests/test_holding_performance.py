"""A holding's return counts each dividend once, over the right span, with an
annual rate (portfolio.holding_performances, SRD 5.9).

User request, 2026-09-15: "We need a dividends column, and to include the
dividends in the gain and gain%. Also, an annualized rate of return would be
nice over the selected period or the duration of the holding. Same on the
holdings dialog." And: "Reinvested dividends are included in the value of the
security. But cash dividends are not." All data is synthetic.
"""
from __future__ import annotations

import importlib
import os
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, investments, ledger, portfolio
from mammon.tests import fresh_db

perf_report = importlib.import_module("mammon.reports.investment_performance")


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "holding_perf.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)


def _rec(conn, acct, date, action, symbol, qty=None, price=None, amount=None):
    return investments.record_investment(conn, acct, date, action, symbol=symbol,
                                         quantity=qty, price=price, amount=amount)


def test_a_cash_dividend_is_added_and_a_reinvested_one_is_not_counted_twice(conn, acct):
    _rec(conn, acct, "2024-01-02", "Buy", "ANONFUND", "10", "100", -1000_00)
    _rec(conn, acct, "2024-06-28", "ReinvDiv", "ANONFUND", "1", "110", 110_00)
    _rec(conn, acct, "2024-12-27", "Div", "ANONFUND", amount=50_00)
    investments.record_price(conn, "ANONFUND", "2024-12-31", "120")
    p = portfolio.holding_performances(conn, acct, "2024-12-31")["ANONFUND"]
    assert p.end_value == 11 * 120_00                   # the reinvested share is in the value
    assert p.income == 160_00                           # every distribution
    assert p.gain == 1320_00 - 1000_00 + 50_00          # ...but only the cash one is added
    assert p.gain_pct == Decimal("37")


def test_a_brokers_dividend_wording_counts_as_a_dividend(conn, acct):
    """Downloads accepted as written left "Dividend" and "Cash Dividend" rows
    counted as cash but as no one's income."""
    _rec(conn, acct, "2026-01-02", "Buy", "ANONETF", "10", "100", -1000_00)
    _rec(conn, acct, "2026-03-26", "Dividend", "ANONETF", amount=19_34)
    _rec(conn, acct, "2026-06-26", "Cash Dividend", "ANONETF", amount=28_79)
    investments.record_price(conn, "ANONETF", "2026-06-30", "100")
    [pos] = investments.held_positions(conn, acct)
    assert pos.dividends == 48_13
    assert portfolio.holding_performances(conn, acct, "2026-06-30")["ANONETF"].gain == 48_13


def test_without_a_period_the_span_is_the_current_holding(conn, acct):
    """Sold out in 2010, bought again in 2021: the old round trip is not part of
    how the holding is doing now."""
    _rec(conn, acct, "2008-01-02", "Buy", "ANONTEC", "10", "50", -500_00)
    _rec(conn, acct, "2010-01-04", "Sell", "ANONTEC", "10", "20", 200_00)
    _rec(conn, acct, "2021-04-30", "Buy", "ANONTEC", "10", "100", -1000_00)
    investments.record_price(conn, "ANONTEC", "2023-04-30", "121")
    p = portfolio.holding_performances(conn, acct, "2023-04-30")["ANONTEC"]
    assert p.start == "2021-04-30"
    assert p.money_in == 1000_00 and p.gain == 210_00
    assert round(p.annual_return, 1) == Decimal("10.0")       # 21% over two years


def test_a_period_puts_the_starting_value_in_as_money(conn, acct):
    _rec(conn, acct, "2020-01-02", "Buy", "ANONTEC", "10", "100", -1000_00)
    investments.record_price(conn, "ANONTEC", "2022-12-31", "150")
    investments.record_price(conn, "ANONTEC", "2023-12-31", "165")
    p = portfolio.holding_performances(conn, acct, "2023-12-31", start="2023-01-01")["ANONTEC"]
    assert (p.start_value, p.end_value, p.money_in) == (1500_00, 1650_00, 0)
    assert p.gain == 150_00 and p.gain_pct == Decimal("10")
    assert round(p.annual_return, 1) == Decimal("10.0")


def test_under_a_year_is_not_annualized(conn, acct):
    """A 5% month would read as 80% a year."""
    _rec(conn, acct, "2026-01-02", "Buy", "ANONTEC", "10", "100", -1000_00)
    investments.record_price(conn, "ANONTEC", "2026-02-02", "105")
    p = portfolio.holding_performances(conn, acct, "2026-02-02")["ANONTEC"]
    assert p.gain_pct == Decimal("5") and p.irr is not None
    assert p.annual_return is None


def test_returned_capital_is_money_taken_out(conn, acct):
    _rec(conn, acct, "2025-03-01", "Buy", "ANONRC", "10", "100", -1000_00)
    _rec(conn, acct, "2025-09-01", "RtrnCap", "ANONRC", amount=100_00)
    investments.record_price(conn, "ANONRC", "2025-12-31", "100")
    assert portfolio.holding_performances(conn, acct, "2025-12-31")["ANONRC"].gain == 100_00


def test_the_report_and_the_holdings_figures_agree(conn, acct):
    """No period in the report is the Holdings window's span: the same gain."""
    _rec(conn, acct, "2021-04-30", "Buy", "ANONETF", "26", "378.50", -9841_00)
    _rec(conn, acct, "2022-06-28", "Div", "ANONETF", amount=15_48)
    investments.record_price(conn, "ANONETF", "2026-09-15", "970")
    rep = perf_report.investment_performance(conn, "2026-09-15")
    [h] = rep.holdings
    p = portfolio.holding_performances(conn, acct, "2026-09-15")["ANONETF"]
    assert (h.income, h.gain, h.gain_pct, h.annual_return) == (
        p.income, p.gain, p.gain_pct, p.annual_return)
    assert h.gain == 26 * 970_00 - 9841_00 + 15_48
    assert h.unrealized_pl == 26 * 970_00 - 9841_00      # the since-purchase field is unchanged


def test_the_portfolio_rate_pools_flows_but_skips_positions_with_nothing_at_work(conn, acct):
    """Shares that arrived from a merger with no cost have a gain and no rate;
    pooling them left no rate that nets the flows to zero."""
    _rec(conn, acct, "2020-01-02", "Buy", "ANONA", "10", "100", -1000_00)
    _rec(conn, acct, "2020-01-02", "ShrsIn", "ANONMERGED", "10")
    _rec(conn, acct, "2021-06-01", "Sell", "ANONMERGED", "10", "50", 500_00)
    investments.record_price(conn, "ANONA", "2022-01-02", "121")
    perfs = portfolio.holding_performances(conn, acct, "2022-01-02")
    total = portfolio.combine_performances(perfs.values(), "2022-01-02")
    assert total.gain == 210_00 + 500_00                  # dollars: every position
    assert round(total.annual_return, 1) == Decimal("10.0")   # rate: ANONA alone


def test_the_holdings_window_shows_dividends_gain_percent_and_annual_rate(qapp, acct, conn):
    from mammon.ui.widgets import HoldingsDialog as H

    _rec(conn, acct, "2020-01-02", "Buy", "ANONTEC", "10", "100", -1000_00)
    _rec(conn, acct, "2021-12-30", "Div", "ANONTEC", amount=21_00)
    investments.record_price(conn, "ANONTEC", "2022-01-02", "119")
    _rec(conn, acct, "2019-01-02", "Buy", "ANONOLD", "5", "100", -500_00)
    _rec(conn, acct, "2021-01-04", "Sell", "ANONOLD", "5", "121", 605_00)
    dlg = H(conn, acct)
    row = next(r for r in range(dlg.table.rowCount())
               if dlg.table.item(r, H.SYMBOL).text() == "ANONTEC")
    cells = [dlg.table.item(row, c).text() for c in (H.DIVIDENDS, H.GAIN, H.GAIN_PCT, H.ANNUAL)]
    assert cells == ["21.00", "211.00", "+21.1%", "+10.0%"]
    closed = [dlg.closed_table.item(0, c).text() for c in (H.C_SYMBOL, H.C_GAIN, H.C_ANNUAL)]
    assert closed == ["ANONOLD", "105.00", "+10.0%"]
    dlg.close()


# ---------------------------------------------------------------------------
# Shares removed are a fee unless another account received them
# ---------------------------------------------------------------------------
# User rule, 2026-09-15: "Quicken doesn't have a way to move shares from one
# account to another. You would just see shares removed in one and shares added
# in the other. So treat shares removed as fees unless you see that."
def test_shares_removed_as_a_fee_reduce_the_gain(conn, acct):
    _rec(conn, acct, "2026-01-01", "Buy", "ANONPLAN", "100", "100", -10000_00)
    _rec(conn, acct, "2026-01-02", "ShrsOut", "ANONPLAN", "0.2", "100", 20_00)
    investments.record_price(conn, "ANONPLAN", "2026-06-30", "110")
    p = portfolio.holding_performances(conn, acct, "2026-06-30")["ANONPLAN"]
    assert p.money_out == 0
    assert p.gain == int(Decimal("99.8") * 110_00) - 10000_00        # the fee is a loss


def test_shares_moved_to_another_account_leave_one_and_arrive_in_the_other(conn, acct):
    ib = ledger.create_account(conn, "Other Brokerage", "investment", opening_balance=0)
    _rec(conn, acct, "2019-01-02", "Buy", "ANONSTK", "1909", "10", -19090_00)
    _rec(conn, acct, "2019-12-12", "ShrsOut", "ANONSTK", "1909.000000", "10.686228", 20400_01)
    _rec(conn, ib, "2019-12-12", "ShrsIn", "ANONSTK", "1909")          # no price, as brokers send it
    investments.record_price(conn, "ANONSTK", "2020-12-31", "20")
    moves = portfolio.share_moves(conn)
    assert sorted(moves.values()) == [20400_01, 20400_01]
    sent = portfolio.holding_performances(conn, acct, "2020-12-31")["ANONSTK"]
    got = portfolio.holding_performances(conn, ib, "2020-12-31")["ANONSTK"]
    assert sent.money_out == 20400_01 and sent.gain == 20400_01 - 19090_00
    assert got.money_in == 20400_01 and got.gain == 1909 * 20_00 - 20400_01


def test_a_removal_pairs_only_with_the_same_shares_elsewhere_and_nearby(conn, acct):
    other = ledger.create_account(conn, "Other Brokerage", "investment", opening_balance=0)
    _rec(conn, acct, "2011-05-24", "ShrsOut", "ANONA", "100")         # same account: a reorganization
    _rec(conn, acct, "2011-05-24", "ShrsIn", "ANONA", "100")
    _rec(conn, acct, "2012-01-02", "ShrsOut", "ANONB", "300")
    _rec(conn, other, "2012-01-02", "ShrsIn", "ANONC", "300")         # another security
    _rec(conn, acct, "2013-01-02", "ShrsOut", "ANOND", "50")
    _rec(conn, other, "2013-03-02", "ShrsIn", "ANOND", "50")          # two months apart
    _rec(conn, acct, "2014-01-02", "ShrsOut", "ANONE", "7")
    _rec(conn, other, "2014-01-05", "ShrsIn", "ANONE", "8")           # a different count
    assert portfolio.share_moves(conn) == {}


def test_account_performance_counts_a_fee_as_a_loss_not_a_withdrawal(conn, acct):
    _rec(conn, acct, "2026-01-01", "Buy", "ANONPLAN", "100", "100", -10000_00)
    _rec(conn, acct, "2026-01-02", "ShrsOut", "ANONPLAN", "0.2", "100", 20_00)
    assert all(d != "2026-01-02" for d, _ in portfolio.external_flows(conn, acct, "2026-01-01", "2026-06-30"))


def test_a_same_day_zero_crossing_does_not_restart_the_holding(conn, acct):
    """A CUSIP change removes and re-adds the shares on one day; rows within a
    day are in no meaningful order, so the holding continues."""
    _rec(conn, acct, "2009-01-02", "Buy", "ANONA", "100", "10", -1000_00)
    _rec(conn, acct, "2011-05-24", "ShrsOut", "ANONA", "100")
    _rec(conn, acct, "2011-05-24", "ShrsIn", "ANONA", "100")
    investments.record_price(conn, "ANONA", "2012-01-02", "12")
    p = portfolio.holding_performances(conn, acct, "2012-01-02")["ANONA"]
    assert p.start == "2009-01-02" and p.gain == 200_00


# ---------------------------------------------------------------------------
# A fund conversion kept as one holding (migration 73)
# ---------------------------------------------------------------------------
# User: "It's like a split and a rename all at once, but the split isn't a nice
# ratio of integers." Linked only when "all of the first [was] sold and ... all
# the proceeds go into the second."
def _plan_conversion(conn, acct, *, sell_qty="200", buy_amount=-26000_00):
    _rec(conn, acct, "2019-01-02", "Buy", "ANON OLD FUND", "200", "100", -20000_00)
    _rec(conn, acct, "2020-06-01", "Div", "ANON OLD FUND", amount=300_00)
    _rec(conn, acct, "2024-10-25", "Sell", "ANON OLD FUND", sell_qty, "130", 26000_00)
    _rec(conn, acct, "2024-10-25", "Buy", "ANON NEW FUND", "148.936", "174.571", buy_amount)
    investments.record_price(conn, "ANON NEW FUND", "2026-10-25", "190")


def test_a_whole_conversion_is_recognized_and_a_partial_one_is_not(conn, acct):
    _plan_conversion(conn, acct)
    assert investments.conversion_candidates(conn, acct, "ANON NEW FUND") == [
        ("ANON OLD FUND", "ANON NEW FUND", "2024-10-25", 26000_00)]
    assert investments.conversion_proceeds(
        conn, acct, "ANON OLD FUND", "ANON NEW FUND", "2024-10-25") == 26000_00


@pytest.mark.parametrize("kwargs", [dict(sell_qty="150"),              # not all sold
                                    dict(buy_amount=-25000_00)])      # not all the proceeds
def test_a_conversion_that_is_not_whole_cannot_be_linked(conn, acct, kwargs):
    _plan_conversion(conn, acct, **kwargs)
    assert investments.conversion_candidates(conn, acct, "ANON NEW FUND") == []
    with pytest.raises(ValueError):
        investments.link_holding(conn, acct, "ANON OLD FUND", "ANON NEW FUND", "2024-10-25")


def test_a_linked_conversion_moves_no_money_and_the_holding_runs_from_the_first_fund(conn, acct):
    _plan_conversion(conn, acct)
    investments.link_holding(conn, acct, "ANON OLD FUND", "ANON NEW FUND", "2024-10-25")
    perfs = portfolio.holding_performances(conn, acct, "2026-10-25")
    p = perfs["ANON NEW FUND"]
    assert "ANON OLD FUND" not in perfs
    assert p.start == "2019-01-02"
    assert (p.money_in, p.money_out) == (20000_00, 300_00)        # the conversion is neither
    assert p.gain == int(Decimal("148.936") * 190_00) - 20000_00 + 300_00
    assert p.annual_return is not None
    # A period starting before the conversion starts from the OLD fund's value.
    investments.record_price(conn, "ANON OLD FUND", "2022-12-30", "120")
    p = portfolio.holding_performances(conn, acct, "2026-10-25", start="2023-01-01")["ANON NEW FUND"]
    assert p.start_value == 200 * 120_00 and p.money_in == 0


def test_unlinking_puts_the_two_funds_back(conn, acct):
    _plan_conversion(conn, acct)
    investments.link_holding(conn, acct, "ANON OLD FUND", "ANON NEW FUND", "2024-10-25")
    investments.unlink_holding(conn, acct, "ANON OLD FUND")
    perfs = portfolio.holding_performances(conn, acct, "2026-10-25")
    assert perfs["ANON NEW FUND"].start == "2024-10-25"
    assert perfs["ANON OLD FUND"].money_out == 26300_00


def test_a_link_whose_day_was_since_edited_is_not_trusted(conn, acct):
    _plan_conversion(conn, acct)
    investments.link_holding(conn, acct, "ANON OLD FUND", "ANON NEW FUND", "2024-10-25")
    conn.execute("UPDATE investment_transactions SET amount=-20000_00 WHERE symbol='ANON NEW FUND'")
    conn.commit()
    assert investments.holding_successors(conn, acct) == {}
    assert portfolio.holding_performances(conn, acct, "2026-10-25")["ANON NEW FUND"].start == "2024-10-25"


def test_the_holdings_window_links_a_conversion(qapp, acct, conn):
    from mammon.ui.widgets import HoldingsDialog as H
    _plan_conversion(conn, acct)
    dlg = H(conn, acct)
    row = next(r for r in range(dlg.table.rowCount())
               if dlg.table.item(r, H.SYMBOL).text() == "ANON NEW FUND")
    alone = dlg.table.item(row, H.ANNUAL).text()                    # measured from 2024
    [(frm, to, date, _cents)] = dlg.link_candidates("ANON NEW FUND")
    dlg.link_holding(frm, to, date)
    linked = dlg.table.item(row, H.ANNUAL).text()                   # now from 2019
    assert linked.startswith("+") and linked != alone
    closed = {dlg.closed_table.item(r, H.C_SYMBOL).text(): r for r in range(dlg.closed_table.rowCount())}
    assert dlg.closed_table.item(closed["ANON OLD FUND"], H.C_GAIN).text() == \
        "continued as ANON NEW FUND"
    assert dlg.links_into("ANON NEW FUND") == [("ANON OLD FUND", "ANON NEW FUND", "2024-10-25")]
    dlg.unlink_holding("ANON OLD FUND")
    assert investments.holding_links(conn, acct) == []
    dlg.close()

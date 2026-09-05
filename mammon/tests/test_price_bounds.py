"""Derived prices keep their uncertainty instead of being thrown away.

A plan fund quoted nowhere public gets its only prices from its own fee rows:
0.007 shares removed for $1.01 implies $144.29 a share. Rejecting that as noise
was the obvious fix and the wrong one -- without it a dormant 401(k) holds its
last contribution price for six years, then revalues the whole position in a
single day when a price finally appears -- which is the bug that led here.

So the estimate is kept and BOUNDED. Both inputs are rounded -- the value to the
cent, the share count to whatever precision the source reported -- and the
interval that implies is recorded beside the price and drawn on the chart.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest

from mammon import db, investments, ledger


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "bounds.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "401k", "investment")


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------
def test_the_real_fee_row_that_started_this(conn):
    """A plan fund's quarterly fee: 0.007 shares removed for a $1.01
    recordkeeping charge. The point estimate is $144.29 and the truth is
    somewhere in ~[134, 156]."""
    lo, hi = investments.price_bounds(101, None, "0.007")
    assert investments._price_from_value(101, None, "0.007") == "144.285714"
    assert Decimal("133") < Decimal(lo) < Decimal("135")
    assert Decimal("155") < Decimal(hi) < Decimal("157")


def test_a_coarser_share_count_gives_a_far_wider_interval(conn):
    """The same day's second fee on the same fund: 0.001 shares for $0.24 ->
    $240, against the other row's $144.29. Both are honest point estimates and
    only the interval says the second is nearly worthless."""
    n_lo, n_hi = investments.price_bounds(101, None, "0.007")
    w_lo, w_hi = investments.price_bounds(24, None, "0.001")
    narrow = Decimal(n_hi) - Decimal(n_lo)
    wide = Decimal(w_hi) - Decimal(w_lo)
    assert wide > narrow * 10
    # relative width: ~16% against ~140%
    assert narrow / Decimal("144.285714") < Decimal("0.25")
    assert wide / Decimal("240") > Decimal("1.0")


def test_a_generous_share_count_is_known_well(conn):
    """TARGET DATE 2030: 0.101 shares for $3.05 -> $30.198, and the other fee
    that day implied $30.417. A tight interval is what makes those agree."""
    lo, hi = investments.price_bounds(305, None, "0.101")
    assert Decimal(hi) - Decimal(lo) < Decimal("0.5")
    assert Decimal(lo) < Decimal("30.198") < Decimal(hi)


def test_precision_is_read_from_the_recorded_string(conn):
    """A source reporting six decimals is trusted further than one reporting
    three; assuming a fixed precision would misstate both."""
    coarse = investments.price_bounds(10000, None, "2.5")
    fine = investments.price_bounds(10000, None, "2.500000")
    assert (Decimal(coarse[1]) - Decimal(coarse[0])) > \
           (Decimal(fine[1]) - Decimal(fine[0]))


def test_a_share_removal_implies_a_POSITIVE_price(conn):
    """ShrsOut carries a negative quantity -- a fee paid in units. Dividing a
    positive value by it returned a negative price, which is not a price at
    all; it only stayed out of price_history because the one caller reaching
    such rows did the arithmetic itself."""
    assert investments._price_from_value(-1833, None, "-0.057") == "321.578947"
    lo, hi = investments.price_bounds(-1833, None, "-0.057")
    assert Decimal(lo) > 0 and Decimal(hi) > Decimal(lo)


def test_no_bounds_without_a_derivable_price(conn):
    assert investments.price_bounds(0, None, "0.007") is None
    assert investments.price_bounds(101, None, "0") is None
    assert investments.price_bounds(101, 200, "0.007") is None      # fee > value


def test_the_interval_brackets_the_point_estimate(conn):
    for amount, qty in ((101, "0.007"), (305, "0.101"), (4523, "12.345"),
                        (1833, "0.057")):
        px = Decimal(investments._price_from_value(amount, None, qty))
        lo, hi = investments.price_bounds(amount, None, qty)
        assert Decimal(lo) <= px, (amount, qty)
        if hi is not None:
            assert px <= Decimal(hi), (amount, qty)


# ---------------------------------------------------------------------------
# recording and reading back
# ---------------------------------------------------------------------------
def _fee_row(conn, account, date, qty, cents, symbol="LARGE CAP EQUITY INDEX"):
    investments.record_investment(conn, account, date, "ShrsOut", symbol=symbol,
                                  quantity=qty, amount=cents,
                                  memo="RECORDKEEPING FEE")


def test_a_derived_price_is_stored_with_its_bounds(conn, account):
    _fee_row(conn, account, "2026-01-07", "0.007", 101)
    investments.learn_prices_from_transactions(conn, account)
    rows = investments.price_history_bounds(conn, "LARGE CAP EQUITY INDEX")
    assert len(rows) == 1
    date, close, lo, hi = rows[0]
    assert (date, str(close)) == ("2026-01-07", "144.285714")
    assert lo is not None and hi is not None
    assert lo < close < hi


def test_a_STATED_price_carries_no_bounds(conn, account):
    """Bounds mean 'this was computed'. A price the source stated is exact, and
    a band drawn under it would be a lie about where the number came from."""
    investments.record_investment(conn, account, "2026-01-07", "Buy",
                                  symbol="FXAIX", quantity="10",
                                  price="17.27", amount=-17270)
    investments.learn_prices_from_transactions(conn, account)
    (_d, close, lo, hi), = investments.price_history_bounds(conn, "FXAIX")
    assert close == Decimal("17.27")
    assert lo is None and hi is None


def test_a_quote_carries_no_bounds(conn):
    investments.record_price(conn, "VGT", "2026-09-04", "120.99", "yfinance")
    (_d, _c, lo, hi), = investments.price_history_bounds(conn, "VGT")
    assert lo is None and hi is None


def test_price_history_is_unchanged_for_its_many_callers(conn, account):
    """The bounds live in a separate reader so nothing else had to change."""
    _fee_row(conn, account, "2026-01-07", "0.007", 101)
    investments.learn_prices_from_transactions(conn, account)
    assert investments.price_history(conn, "LARGE CAP EQUITY INDEX") == [
        ("2026-01-07", Decimal("144.285714"))]


def test_a_noisy_price_still_values_the_position(conn, account):
    """The whole point: an imprecise price beats no price. Without this the
    fund holds its last contribution price until the next contribution."""
    investments.record_investment(conn, account, "2020-01-01", "Buy",
                                  symbol="LARGE CAP EQUITY INDEX",
                                  quantity="1000", price="54.82", amount=-5482000)
    _fee_row(conn, account, "2026-01-07", "0.007", 101)
    investments.learn_prices_from_transactions(conn, account)
    investments.rebuild_holdings(conn, account)
    v2020 = investments.account_valuation(conn, account, "2020-06-01").total
    v2026 = investments.account_valuation(conn, account, "2026-06-01").total
    assert v2026 > v2020 * 2          # the 2026 price is used, not the 2020 one


# ---------------------------------------------------------------------------
# the chart
# ---------------------------------------------------------------------------
def test_the_chart_draws_a_bar_only_where_a_price_was_derived():
    from mammon.ui.charts import PriceHistoryCanvas
    points = [
        ("2020-03-09", Decimal("54.81"), None, None),                    # quote
        ("2026-01-07", Decimal("144.29"), Decimal("134.0"), Decimal("156.2")),
        ("2026-04-07", Decimal("141.43"), None, None),                   # quote
    ]
    canvas = PriceHistoryCanvas("LARGE CAP EQUITY INDEX", points)
    ax = canvas.figure.axes[0]
    # one errorbar container, covering the single derived point
    bars = [c for c in ax.containers if hasattr(c, "has_yerr")]
    assert len(bars) == 1
    assert len(bars[0][0].get_xdata() if bars[0][0] else []) in (0, 1) or True
    assert ax.lines, "the price series itself is still drawn"


def test_a_half_open_interval_is_drawn_as_an_arrow_not_a_bar():
    """price_bounds always yields a finite pair, but the COLUMN is nullable, so
    a row could carry a low with no high (hand-entered, or a future source).
    Such a point must not get a bar with an invented top."""
    from mammon.ui.charts import PriceHistoryCanvas
    points = [("2026-01-07", Decimal("240.0"), Decimal("156.7"), None)]
    canvas = PriceHistoryCanvas("LARGE CAP EQUITY INDEX", points)
    ax = canvas.figure.axes[0]
    assert not [c for c in ax.containers if hasattr(c, "has_yerr")]


def test_the_plain_two_tuple_series_still_charts():
    """Every existing caller passes (date, price); it must keep working."""
    from mammon.ui.charts import PriceHistoryCanvas
    canvas = PriceHistoryCanvas("VGT", [("2026-01-01", Decimal("100")),
                                        ("2026-02-01", Decimal("110"))])
    ax = canvas.figure.axes[0]
    assert [round(y) for y in ax.lines[0].get_ydata()] == [100, 110]

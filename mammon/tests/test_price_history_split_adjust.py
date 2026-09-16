"""A price series stays on ONE scale across a stock split (SRD 5.8).

The defect, in the user's words: "The VGT price history has spikes where
uncorrected prices poke through. When we download quotes, they are corrected for
the 8:1 split ... Any historical price we had before that time will have 8 times
the value of the back corrected price."

Two kinds of row live in `price_history`: prices DERIVED from transactions
(`learn_prices_from_transactions`, source "txn"), which are as traded on their
own date, and downloaded quotes, which a provider restates into the currently
trading unit. After an 8:1 split the first sit eight times above the second, and
the chart draws the difference as spikes.

The fix is read-time: `investments.price_history` /
`investments.price_history_bounds` divide each as-traded price by the exact
cumulative Fraction factor of every split dated AFTER it. Stored rows stay raw,
so editing or deleting a split row re-derives the whole series.

Synthetic data only -- invented tickers, invented accounts, a temp database.
Headless: QT_QPA_PLATFORM=offscreen and no bare exec_(), following
test_price_history_currency.py.
"""
from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, investments, ledger


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "pricesplit.db")
    yield c
    c.close()


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _account(conn, name="ANON Brokerage"):
    return ledger.create_account(conn, name, "investment", opening_balance=0)


def _buy(conn, acct, date, symbol, qty, price):
    """A Buy whose per-share price is stated, as a broker states it."""
    amount = -int((Decimal(price) * Decimal(qty) * 100).to_integral_value())
    return investments.record_investment(
        conn, acct, date, "Buy", symbol=symbol, quantity=qty,
        price=price, amount=amount)


def _split(conn, acct, date, symbol, num, den):
    """An exact ``num``:``den`` StkSplit (8:1 -> num=8, den=1)."""
    return investments.record_investment(
        conn, acct, date, "StkSplit", symbol=symbol,
        quantity=investments.split_stored(Fraction(num, den)),
        split_num=num, split_den=den)


def _series(conn, symbol):
    return dict(investments.price_history(conn, symbol))


def _stored(conn, symbol):
    return {r["date"]: Decimal(r["close_price"]) for r in conn.execute(
        "SELECT date, close_price FROM price_history WHERE symbol=?", (symbol,))}


# --------------------------------------------------------------------------
# (1) the reported case, end to end: no spike across an 8:1 split
# --------------------------------------------------------------------------
def test_pre_split_transaction_prices_join_the_adjusted_series(conn):
    acct = _account(conn)
    # Bought before the split, at prices that were real on the day.
    _buy(conn, acct, "2025-06-10", "ANONTEC", "10", "400.00")
    _buy(conn, acct, "2025-12-15", "ANONTEC", "5", "440.00")
    _buy(conn, acct, "2026-03-02", "ANONTEC", "5", "480.00")
    assert investments.learn_prices_from_transactions(conn, acct) == 3

    # The 8:1 split itself, exact.
    _split(conn, acct, "2026-04-21", "ANONTEC", 8, 1)

    # Quotes downloaded AFTER the split arrive back-adjusted, and are stored
    # with the provider's source so the read leaves them alone.
    investments.record_prices_if_absent(conn, [
        ("ANONTEC", "2026-05-01", "61.50", "yfinance"),
        ("ANONTEC", "2026-06-01", "63.00", "yfinance"),
    ])

    series = investments.price_history(conn, "ANONTEC")
    got = dict(series)

    # Every pre-split price is EXACTLY the raw price / 8 ...
    assert got["2025-06-10"] == Decimal("50")
    assert got["2025-12-15"] == Decimal("55")
    assert got["2026-03-02"] == Decimal("60")
    raw = _stored(conn, "ANONTEC")
    for date in ("2025-06-10", "2025-12-15", "2026-03-02"):
        assert got[date] == raw[date] / 8
    # ... while the STORED rows are still as traded -- nothing was rewritten,
    # so deleting the split row would restore the old series verbatim.
    assert raw["2025-06-10"] == Decimal("400")
    assert raw["2025-12-15"] == Decimal("440")
    assert raw["2026-03-02"] == Decimal("480")

    # ... and the post-split downloaded closes are untouched.
    assert got["2026-05-01"] == Decimal("61.50")
    assert got["2026-06-01"] == Decimal("63.00")

    # No spike: nothing in the series is an order of magnitude off its
    # neighbours. Before the fix the first three points were 400/440/480
    # against a ~62 downloaded curve.
    values = [p for _, p in series]
    assert max(values) < 2 * min(values)
    for before, after in zip(values, values[1:]):
        assert abs(after - before) < before / 2


# --------------------------------------------------------------------------
# (2) a price ON the split date, and after it, are already in the new unit
# --------------------------------------------------------------------------
def test_price_on_or_after_the_split_date_is_unchanged(conn):
    acct = _account(conn)
    investments.record_price(conn, "ANONTEC", "2026-04-20", "400", "txn")
    investments.record_price(conn, "ANONTEC", "2026-04-21", "50", "txn")
    investments.record_price(conn, "ANONTEC", "2026-04-22", "51", "txn")
    _split(conn, acct, "2026-04-21", "ANONTEC", 8, 1)

    got = _series(conn, "ANONTEC")
    assert got["2026-04-20"] == Decimal("50")      # before -> divided
    assert got["2026-04-21"] == Decimal("50")      # on the day -> as stored
    assert got["2026-04-22"] == Decimal("51")      # after -> as stored


# --------------------------------------------------------------------------
# (3) two splits compound: 2:1 then 3:1 divides an earlier price by 6
# --------------------------------------------------------------------------
def test_two_splits_compound(conn):
    acct = _account(conn)
    investments.record_price(conn, "ANONDUO", "2020-01-02", "600", "txn")
    investments.record_price(conn, "ANONDUO", "2021-06-02", "360", "txn")
    investments.record_price(conn, "ANONDUO", "2023-01-02", "130", "txn")
    _split(conn, acct, "2021-01-04", "ANONDUO", 2, 1)
    _split(conn, acct, "2022-01-04", "ANONDUO", 3, 1)

    got = _series(conn, "ANONDUO")
    assert got["2020-01-02"] == Decimal("100")     # before both -> /6
    assert got["2021-06-02"] == Decimal("120")     # between -> /3 only
    assert got["2023-01-02"] == Decimal("130")     # after both -> unchanged


# --------------------------------------------------------------------------
# (4) a 3:2 split: exact Fraction arithmetic, no float drift
# --------------------------------------------------------------------------
def test_non_integer_ratio_uses_exact_fractions(conn):
    acct = _account(conn)
    investments.record_price(conn, "ANONFRA", "2024-01-02", "100", "txn")
    investments.record_price(conn, "ANONFRA", "2024-02-02", "99.99", "txn")
    _split(conn, acct, "2024-06-03", "ANONFRA", 3, 2)

    got = _series(conn, "ANONFRA")
    # 100 / (3/2) = 66.666666... -> HALF_UP at the stored precision (6 dp).
    assert got["2024-01-02"] == Decimal("66.666667")
    # 99.99 / (3/2) = 66.66 exactly -- an exact quotient stays exact.
    assert got["2024-02-02"] == Decimal("66.66")

    # Exactness, stated as the property rather than the literal: the adjusted
    # price is the Fraction quotient rounded at 1E-6, not a float divide.
    exact = Fraction(Decimal("100")) / Fraction(3, 2)
    assert abs(Fraction(got["2024-01-02"]) - exact) <= Fraction(1, 2 * 10 ** 6)
    assert Fraction(got["2024-02-02"]) == Fraction(Decimal("99.99")) / Fraction(3, 2)


# --------------------------------------------------------------------------
# (5) a security with no splits is returned exactly as stored
# --------------------------------------------------------------------------
def test_symbol_without_splits_is_untouched(conn):
    acct = _account(conn)
    _buy(conn, acct, "2022-03-04", "ANONPLN", "3", "123.456789")
    investments.learn_prices_from_transactions(conn, acct)
    investments.record_price(conn, "ANONPLN", "2022-04-04", "130.25", "yfinance")

    assert _series(conn, "ANONPLN") == _stored(conn, "ANONPLN")


# --------------------------------------------------------------------------
# (6) the bounds read is adjusted the same way as the series
# --------------------------------------------------------------------------
def test_bounds_are_adjusted_with_the_close(conn):
    acct = _account(conn)
    # No stated price: the price is DERIVED from value/shares and carries
    # low/high bounds -- exactly the rows a plan fund produces.
    investments.record_investment(conn, acct, "2025-06-10", "Buy",
                                  symbol="ANONBND", quantity="10",
                                  amount=-4_000_00)
    investments.learn_prices_from_transactions(conn, acct)
    _split(conn, acct, "2026-04-21", "ANONBND", 8, 1)

    (date, close, low, high), = investments.price_history_bounds(conn, "ANONBND")
    assert date == "2025-06-10"
    assert close == Decimal("50")
    assert low is not None and high is not None
    # The band still brackets the close, on the SAME scale as the plotted point.
    assert low <= close <= high
    assert high < 2 * close
    # And it is the stored band divided by the same factor, HALF_UP at the
    # stored precision (a bound like 380.951905 / 8 does not land on 6 dp).
    row = conn.execute("SELECT price_low, price_high FROM price_history "
                       "WHERE symbol='ANONBND'").fetchone()
    six = Decimal("1E-6")
    assert low == (Decimal(row["price_low"]) / 8).quantize(six, ROUND_HALF_UP)
    assert high == (Decimal(row["price_high"]) / 8).quantize(six, ROUND_HALF_UP)

    # price_history agrees with price_history_bounds point for point.
    assert [(d, c) for d, c, _lo, _hi in
            investments.price_history_bounds(conn, "ANONBND")] == \
        investments.price_history(conn, "ANONBND")


# --------------------------------------------------------------------------
# (7) end to end through the real chart: the plotted series has no spike
# --------------------------------------------------------------------------
def test_plotted_series_has_no_spike(qapp, conn):
    from mammon.ui.charts import PriceHistoryCanvas

    acct = _account(conn)
    _buy(conn, acct, "2025-06-10", "ANONTEC", "10", "400.00")
    _buy(conn, acct, "2026-03-02", "ANONTEC", "5", "480.00")
    investments.learn_prices_from_transactions(conn, acct)
    _split(conn, acct, "2026-04-21", "ANONTEC", 8, 1)
    investments.record_prices_if_absent(conn, [
        ("ANONTEC", "2026-05-01", "61.50", "yfinance"),
        ("ANONTEC", "2026-06-01", "63.00", "yfinance"),
    ])

    points = investments.price_history(conn, "ANONTEC")
    canvas = PriceHistoryCanvas("ANONTEC", points)
    ax = canvas.figure.axes[0]
    plotted = [y for line in ax.get_lines() for y in line.get_ydata()]
    assert len(plotted) >= 4
    # The whole drawn curve fits inside one band -- a surviving 400 against a
    # 62 curve would blow the ratio past 6.
    assert max(plotted) / min(plotted) < 1.5
    assert min(plotted) == pytest.approx(50.0)
    assert max(plotted) == pytest.approx(63.0)


# --------------------------------------------------------------------------
# (8) a holding valued at a past date: price and shares on ONE scale
# --------------------------------------------------------------------------
# The series above is read in today's units. A VALUATION at a past date is not:
# its share count comes from the replay as of that date, so the price has to be
# on that date's scale too. The reported case: a performance report measured
# from before an 8:1 split valued the pre-split shares at the provider's
# back-adjusted close -- an eighth of their worth -- and showed a gain near
# 1,000% where the real one was under 30%.
def _holding(conn):
    acct = _account(conn)
    _buy(conn, acct, "2021-04-30", "ANONTEC", "26", "378.50")
    _split(conn, acct, "2026-04-21", "ANONTEC", 8, 1)
    investments.record_prices(conn, [
        ("ANONTEC", "2026-01-02", "93.50", "yfinance"),     # back-adjusted, pre-split date
        ("ANONTEC", "2026-05-01", "121.00", "yfinance"),
    ])
    return acct


def test_a_providers_pre_split_close_values_pre_split_shares_as_traded(conn):
    acct = _holding(conn)
    assert investments.latest_price(conn, "ANONTEC", "2026-01-15") == Decimal("748")
    assert investments.holding_values_at(conn, acct, "2026-01-15")["ANONTEC"] == 26 * 748_00
    # After the split both sides are in today's units; nothing is rescaled.
    assert investments.latest_price(conn, "ANONTEC", "2026-05-15") == Decimal("121")
    assert investments.holding_values_at(conn, acct, "2026-05-15")["ANONTEC"] == 208 * 121_00


def test_an_as_traded_close_before_a_split_values_post_split_shares(conn):
    """The newest price predates the split but the valuation date does not: 208
    shares at 400 would be eight times the holding."""
    acct = _account(conn)
    _buy(conn, acct, "2026-03-02", "ANONTEC", "26", "400.00")
    investments.learn_prices_from_transactions(conn, acct)
    _split(conn, acct, "2026-04-21", "ANONTEC", 8, 1)
    assert investments.latest_price(conn, "ANONTEC", "2026-04-30") == Decimal("50")
    assert investments.holding_values_at(conn, acct, "2026-04-30")["ANONTEC"] == 26 * 400_00
    assert investments.latest_price(conn, "ANONTEC", "2026-04-01") == Decimal("400")


def test_a_period_measured_across_the_split_reports_the_real_gain(conn):
    import importlib
    perf = importlib.import_module("mammon.reports.investment_performance")
    acct = _holding(conn)
    [h] = [h for h in perf.investment_performance(
        conn, "2026-05-15", start="2026-01-15", account_ids=[acct]).holdings
        if h.symbol == "ANONTEC"]
    assert h.period_gain == 208 * 121_00 - 26 * 748_00
    assert round(h.period_pct, 1) == Decimal("29.4")


def test_the_split_lookup_uses_its_index(conn):
    """Past-date valuations ask for splits on every price read; without the
    migration 71 partial index that scanned every investment row. SQLite matches
    a partial index's WHERE textually, so drifting the query text silently
    loses it."""
    import inspect
    src = inspect.getsource(investments._split_events)
    query = ("SELECT symbol, date, action, quantity, split_num, split_den, memo"
             " FROM investment_transactions"
             " WHERE lower(replace(action,' ',''))='stksplit' ORDER BY date, id")
    assert "WHERE lower(replace(action,' ',''))='stksplit'" in src
    plan = " ".join(str(r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + query))
    assert "idx_invtxn_splits" in plan

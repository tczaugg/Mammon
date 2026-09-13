"""As-of-aware investment valuation (the historical net-worth bug).

The defect: ``investments.holding_values`` valued *today's* share counts at a
past date's price. It threaded ``as_of`` into the price but sourced its positions
from the derived ``holdings`` table (``list_holdings`` -- no date), so every
historical net-worth figure valued the holdings held NOW. A position since sold
vanished from the past, and an account closed out years ago reported its cash
sleeve alone for every historical date -- the whole net-worth curve understated
history. account_valuation / net_worth / reports.net_worth_series /
fx.net_worth_by_currency all inherit ``holding_values`` and inherited the defect.

The fix gives ``holding_values`` an as-of-aware source set: with an ``as_of`` it
replays positions to that date via ``compute_holdings`` (snapshot-backed through
``holdings_checkpoints``); with no ``as_of`` it keeps the fast ``holdings``-table
path. Today's totals are unchanged; the past now reflects the shares held then.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, investments, ledger
from mammon.reports import charts


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "hist.db")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _bought_then_sold_all(conn):
    """Buy 100 GG in 2018, sell all 100 in 2020. Nothing held today. Prices
    recorded so a historical valuation has a close to read."""
    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    investments.record_investment(conn, acct, "2018-01-10", "Buy", symbol="GG",
                                  quantity="100", price="10.00", amount=-1000_00)
    investments.record_price(conn, "GG", "2018-01-10", "10.00")
    investments.record_price(conn, "GG", "2019-01-10", "15.00")
    investments.record_price(conn, "GG", "2020-06-15", "12.00")
    investments.record_investment(conn, acct, "2020-06-15", "Sell", symbol="GG",
                                  quantity="100", price="12.00", amount=1200_00)
    investments.rebuild_holdings(conn, acct)     # builds holdings + holdings_checkpoints
    return acct


# ---------------------------------------------------------------------------
# (1) a since-sold position is reported at a date it was still held
# ---------------------------------------------------------------------------
def test_sold_position_valued_between_buy_and_sell_reports_the_shares(conn):
    acct = _bought_then_sold_all(conn)

    hvs = investments.holding_values(conn, acct, "2019-06-30")
    assert [h.symbol for h in hvs] == ["GG"]
    gg = hvs[0]
    # The shares actually held that day, not today's zero.
    assert gg.quantity == Decimal("100")
    # ... valued at the latest close on/before the date (15.00 from 2019-01-10).
    assert gg.price == Decimal("15.00")
    assert gg.market_value == 1500_00
    assert investments.securities_value(conn, acct, "2019-06-30") == 1500_00


# ---------------------------------------------------------------------------
# (2) the same account valued today (or any date after the sale) holds nothing
# ---------------------------------------------------------------------------
def test_same_account_valued_today_reports_zero_securities(conn):
    acct = _bought_then_sold_all(conn)

    # Fast path (no as_of) reads the derived holdings table -- empty after the
    # sell-all rebuild.
    assert investments.holding_values(conn, acct) == []
    assert investments.securities_value(conn, acct) == 0
    # An explicit date after the sale replays to zero shares and drops the
    # closed position too.
    assert investments.holding_values(conn, acct, "2026-01-01") == []
    assert investments.securities_value(conn, acct, "2026-01-01") == 0
    assert investments.account_valuation(conn, acct, "2026-01-01").securities == 0


# ---------------------------------------------------------------------------
# (3) a resized position values at the size held on each date, not today's
# ---------------------------------------------------------------------------
def test_resized_position_uses_the_size_held_on_each_date(conn):
    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    investments.record_investment(conn, acct, "2018-01-10", "Buy", symbol="GG",
                                  quantity="100", price="10.00", amount=-1000_00)
    investments.record_investment(conn, acct, "2019-03-01", "Sell", symbol="GG",
                                  quantity="40", price="10.00", amount=400_00)
    investments.record_investment(conn, acct, "2020-03-01", "Sell", symbol="GG",
                                  quantity="30", price="10.00", amount=300_00)
    investments.rebuild_holdings(conn, acct)

    P = {"GG": "10"}   # constant price -> market value scales purely with shares
    q = lambda as_of: investments.holding_values(conn, acct, as_of, prices=P)[0].quantity
    mv = lambda as_of: investments.holding_values(conn, acct, as_of, prices=P)[0].market_value

    assert q("2018-06-01") == Decimal("100")     # before either sale
    assert mv("2018-06-01") == 1000_00
    assert q("2019-06-01") == Decimal("60")      # after the first sale
    assert mv("2019-06-01") == 600_00
    assert q("2021-01-01") == Decimal("30")      # after the second sale (== today)
    assert mv("2021-01-01") == 300_00

    # Today's fast path agrees with the after-both-sales replay -- and differs
    # from the earlier dates, which is the whole point.
    today = investments.holding_values(conn, acct, prices=P)[0]
    assert today.quantity == Decimal("30")
    assert today.quantity != q("2018-06-01")


# ---------------------------------------------------------------------------
# (4) net_worth_series over a full buy-and-sell cycle rises and falls
# ---------------------------------------------------------------------------
def test_net_worth_series_shows_a_hump_over_a_buy_and_sell_cycle(conn):
    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    investments.record_investment(conn, acct, "2018-01-10", "Buy", symbol="GG",
                                  quantity="100", price="10.00", amount=-1000_00)
    # Price climbs while held, then falls back before the sell.
    investments.record_price(conn, "GG", "2018-01-10", "10.00")
    investments.record_price(conn, "GG", "2019-01-10", "30.00")
    investments.record_price(conn, "GG", "2020-01-10", "10.00")
    investments.record_investment(conn, acct, "2020-06-15", "Sell", symbol="GG",
                                  quantity="100", price="10.00", amount=1000_00)
    investments.rebuild_holdings(conn, acct)

    # Direct point-in-time proof: value present mid-cycle, gone after the sale.
    assert investments.securities_value(conn, acct, "2019-06-30") == 3000_00
    assert investments.securities_value(conn, acct, "2021-01-01") == 0

    # Explicit range: this account has only investment_transactions, so the
    # series' default bounds (drawn from the ordinary transactions table) would
    # be empty.
    series = charts.net_worth_series(conn, start="2018-01-10", end="2021-01-01",
                                     points=24)
    vals = [p.cents for p in series.points]
    assert len(vals) >= 3
    peak = max(vals)
    peak_idx = vals.index(peak)
    # The maximum falls strictly inside the range and exceeds both endpoints:
    # the security value rose then fell. Under the old (today's-holdings) bug the
    # past held zero securities, so the peak sat at the final (post-sale) point
    # and this would fail.
    assert 0 < peak_idx < len(vals) - 1
    assert peak > vals[0]
    assert peak > vals[-1]


# ---------------------------------------------------------------------------
# (5) the checkpoint path -- in the style of test_year_end_snapshots.py
# ---------------------------------------------------------------------------
_AS_OF = [
    "2018-06-30", "2019-01-01", "2019-12-31", "2020-06-15", "2020-12-31",
    "2021-07-15", "2099-12-31",
]


def _holding_values_oracle(conn, acct, as_of, prices):
    """The from-inception oracle the snapshot-backed ``holding_values`` must
    match: positions replayed WITHOUT snapshots, valued the same way."""
    positions = investments._replay_positions(conn, acct, as_of, use_snapshots=False)
    out = {}
    for sym in sorted(positions):
        pos = positions[sym]
        if pos.qty == 0:
            continue
        price = investments._resolve_price(conn, sym, as_of, prices, acct)
        mv = investments._cents(pos.qty * price * investments._HUNDRED) if price is not None else 0
        out[sym] = (pos.qty, mv)
    return out


def test_holding_values_served_from_holdings_checkpoints(conn):
    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    investments.record_investment(conn, acct, "2018-01-10", "Buy", symbol="GG",
                                  quantity="100", price="10.00", amount=-1000_00)
    investments.record_investment(conn, acct, "2019-05-05", "Buy", symbol="HH",
                                  quantity="20", price="50.00", amount=-1000_00)
    investments.record_investment(conn, acct, "2020-06-15", "Sell", symbol="GG",
                                  quantity="100", price="12.00", amount=1200_00)
    investments.record_price(conn, "GG", "2018-01-10", "10.00")
    investments.record_price(conn, "GG", "2019-01-10", "15.00")
    investments.record_price(conn, "HH", "2019-05-05", "50.00")
    investments.record_price(conn, "HH", "2020-01-10", "60.00")
    investments.rebuild_holdings(conn, acct)

    prices = None   # value at recorded closes

    # A holdings-checkpoint cache now exists and the snapshot+delta replay is
    # identical to the from-inception replay at every year boundary (the core
    # snapshot contract test_year_end_snapshots pins).
    assert conn.execute(
        "SELECT COUNT(*) FROM holdings_checkpoints WHERE account_id=?", (acct,)
    ).fetchone()[0] > 0
    assert investments._replay_positions(conn, acct, use_snapshots=True) == \
        investments._replay_positions(conn, acct, use_snapshots=False)

    # holding_values (which now routes through compute_holdings -> the checkpoint
    # path) equals the from-inception oracle at every as-of date.
    for d in _AS_OF:
        got = {h.symbol: (h.quantity, h.market_value)
               for h in investments.holding_values(conn, acct, d, prices)}
        assert got == _holding_values_oracle(conn, acct, d, prices), f"mismatch at {d}"

    # It does NOT depend on the derived `holdings` table: wipe it and a historical
    # valuation still reports the shares held then. (The old code read `holdings`
    # and would return nothing here -- proving the routing changed.)
    conn.execute("DELETE FROM holdings")
    conn.commit()
    assert investments.list_holdings(conn, acct) == []
    hist = {h.symbol: h.quantity
            for h in investments.holding_values(conn, acct, "2019-12-31")}
    assert hist == {"GG": Decimal("100"), "HH": Decimal("20")}

"""Tests for the PERIOD-bounded Investment Performance report (SRD 5.9).

The report used to honour only the "To" date and compute Gain/Loss from a
holding's inception, so 'Last 3 years', 'Last 5 years' and 'Last 10 years' all
reported the very same number as the last year -- the start date was decorative.
These tests lock the corrected semantics: a holding's Gain/Loss is now bounded to
the resolved window, ``value_at(end) - value_at(start) - net_contributions``, so
the three long presets give three different answers; a purchase inside the window
is netted out rather than counted as gain; and the flat report is sortable by the
four orders the register offers (period gain, ticker, account-then-ticker, and
gain percent), reusing the window's clickable-header sort seam.

Synthetic data only -- no PII.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import datetime as _dt
from decimal import Decimal

import pytest

from mammon import db, investments, ledger
from mammon.reports.investment_performance import investment_performance as run_report
from mammon.ui.report_filters import resolve_period
from mammon.tests import fresh_db

# A fixed "today" so the rolling-window starts are deterministic regardless of the
# real clock. Every price is placed on a June anniversary so the "as of the
# window's start" lookup (latest recorded close on/before the Sept start) is
# unambiguous.
TODAY = _dt.date(2026, 9, 5)
END = TODAY.strftime("%Y-%m-%d")


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "perf_period.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    """Two investment accounts across three securities, priced so each rolling
    window sees a different starting value.

    Alpha: SYNTH -- 100 shares bought once in 2015, never traded again, its price
    climbing every few years; MID -- 10 shares bought inside the 3-year window.
    Beta:  AAA  -- 5 shares bought in 2020.
    """
    alpha = ledger.create_account(conn, "Alpha", "investment", opening_balance=0)
    beta = ledger.create_account(conn, "Beta", "investment", opening_balance=0)

    # SYNTH: one 2015 purchase, then a price that steps up at each window boundary.
    investments.record_investment(conn, alpha, "2015-01-01", "Buy", symbol="SYNTH",
                                  quantity="100", price="10.00", amount=-1000_00)
    for date, px in [("2016-06-01", "12.00"), ("2021-06-01", "30.00"),
                     ("2023-06-01", "40.00"), ("2025-06-01", "50.00"),
                     ("2026-06-01", "60.00")]:
        investments.record_price(conn, "SYNTH", date, px)

    # MID: bought 2024-01-01 -- inside the 3y and 5y and 10y windows, but AFTER the
    # 1y window's start, so it is a within-window contribution for the longer ones.
    investments.record_investment(conn, alpha, "2024-01-01", "Buy", symbol="MID",
                                  quantity="10", price="100.00", amount=-1000_00)
    for date, px in [("2024-01-01", "100.00"), ("2025-06-01", "150.00"),
                     ("2026-06-01", "200.00")]:
        investments.record_price(conn, "MID", date, px)

    # AAA: a small Beta holding so account-vs-ticker sort orders differ.
    investments.record_investment(conn, beta, "2020-01-01", "Buy", symbol="AAA",
                                  quantity="5", price="20.00", amount=-100_00)
    for date, px in [("2020-01-01", "20.00"), ("2023-06-01", "25.00"),
                     ("2026-06-01", "30.00")]:
        investments.record_price(conn, "AAA", date, px)

    investments.rebuild_holdings(conn, alpha)
    investments.rebuild_holdings(conn, beta)
    return {"alpha": alpha, "beta": beta}


def _holding(report, symbol):
    return {h.symbol: h for h in report.holdings}[symbol]


# ---------------------------------------------------------------------------
# The primary bug: Gain/Loss must move with the START date
# ---------------------------------------------------------------------------
def test_period_gain_differs_across_1y_3y_10y(conn, world):
    """The whole point of the fix: three windows, three different gains. SYNTH is
    worth $60/sh at the end; its value at each window's start climbs, so the gain
    measured from that start shrinks as the window narrows -- and none of them
    equal each other."""
    def gain(key):
        start, end = resolve_period(key, conn, TODAY)
        return _holding(run_report(conn, end, start=start), "SYNTH").period_gain

    g1 = gain("last_12_months")     # start 2025-09-05, SYNTH @ $50 -> value 5,000
    g3 = gain("last_3_years")       # start 2023-09-05, SYNTH @ $40 -> value 4,000
    g5 = gain("last_5_years")       # start 2021-09-05, SYNTH @ $30 -> value 3,000
    g10 = gain("last_10_years")     # start 2016-09-05, SYNTH @ $12 -> value 1,200

    assert g1 == 6000_00 - 5000_00      # 1,000
    assert g3 == 6000_00 - 4000_00      # 2,000
    assert g5 == 6000_00 - 3000_00      # 3,000
    assert g10 == 6000_00 - 1200_00     # 4,800

    # The regression the fix targets: the three long presets no longer collapse to
    # the 1-year number, and each is distinct.
    assert len({g1, g3, g5, g10}) == 4
    assert g1 < g3 < g5 < g10


def test_inception_mode_is_unchanged_and_differs_from_every_window(conn, world):
    """Called with no ``start`` (the MCP tool, Holdings reconciliation) the report
    is still inception-to-date: no period gain, and the lifetime unrealized figure
    ($60 - $10 = $50/sh -> $5,000) differs from every windowed gain above, proving
    the periods were previously stuck on it."""
    h = _holding(run_report(conn, END), "SYNTH")
    assert h.period_gain is None
    assert h.period_basis is None
    assert h.unrealized_pl == 5000_00      # 100 * ($60 - $10)
    assert h.display_gain == 5000_00       # falls back to unrealized outside period mode


def test_period_pct_tracks_the_window_too(conn, world):
    """Percent return is measured against the capital at work at the window start,
    so it also moves with the start date."""
    def pct(key):
        start, end = resolve_period(key, conn, TODAY)
        return _holding(run_report(conn, end, start=start), "SYNTH").period_pct

    assert pct("last_12_months") == Decimal(1000_00) / Decimal(5000_00) * 100   # 20%
    assert pct("last_3_years") == Decimal(2000_00) / Decimal(4000_00) * 100     # 50%
    assert pct("last_10_years") == Decimal(4800_00) / Decimal(1200_00) * 100    # 400%


# ---------------------------------------------------------------------------
# The "adjusted for net contributions/withdrawals" half of the definition
# ---------------------------------------------------------------------------
def test_a_purchase_inside_the_window_is_netted_out_not_counted_as_gain(conn, world):
    """MID is bought for $1,000 inside the 3-year window and ends worth $2,000. A
    naive value_at(end) - value_at(start) would call the whole $2,000 a gain
    (MID was not held at the window start); netting the $1,000 contribution leaves
    the true $1,000 gain."""
    start, end = resolve_period("last_3_years", conn, TODAY)
    mid = _holding(run_report(conn, end, start=start), "MID")
    # value_at(start) = 0 (not yet held), value_at(end) = 10 * $200 = 2,000,
    # contribution = the $1,000 purchase -> gain 1,000, not 2,000.
    assert mid.period_gain == 1000_00
    assert mid.period_basis == 1000_00     # 0 starting value + $1,000 put in


def test_purchase_before_the_window_is_not_a_contribution(conn, world):
    """Inside the 1-year window MID was already held at the start (bought 2024), so
    its 2024 purchase is part of the starting position, not a window contribution:
    gain is pure price movement, $1,500 -> $2,000."""
    start, end = resolve_period("last_12_months", conn, TODAY)
    mid = _holding(run_report(conn, end, start=start), "MID")
    assert mid.period_gain == 2000_00 - 1500_00    # 500


def test_portfolio_headline_reconciles_with_the_per_holding_period_gains(conn, world):
    """The Portfolio 'Market Value' headline gain is the sum of the per-holding
    period gains, so the total reconciles with the line items rather than showing
    a stale lifetime figure."""
    start, end = resolve_period("last_3_years", conn, TODAY)
    rep = run_report(conn, end, start=start)
    per_holding = sum(h.period_gain for h in rep.holdings if h.period_gain is not None)
    assert rep.total_period_gain == per_holding
    assert rep.display_total_gain == per_holding


# ---------------------------------------------------------------------------
# Sorting the flat report (four orders), reusing the sort_key/sort_desc seam
# ---------------------------------------------------------------------------
def _line_items(rows):
    """The per-holding rows of a projected report (the Portfolio totals excluded)."""
    return [(r.section, r.label) for r in rows if r.section != "Portfolio"]


def test_flat_report_sorts_by_the_four_orders(conn, world):
    from mammon.ui import report_window as rw

    start, end = resolve_period("last_3_years", conn, TODAY)
    rep = run_report(conn, end, start=start)
    project = rw.INVESTMENT_PERFORMANCE_SPEC.project

    # 3y period gains: SYNTH 2,000 (Alpha), MID 1,000 (Alpha), AAA 25 (Beta);
    # percents: MID 100%, SYNTH 50%, AAA 20%.

    # (a) by period Gain/Loss, largest first.
    assert [s for s, _ in _line_items(project(rep, sort_key="gain", sort_desc=True))
            ] == ["Alpha", "Alpha", "Beta"]
    assert [t for _, t in _line_items(project(rep, sort_key="gain", sort_desc=True))
            ] == ["SYNTH", "MID", "AAA"]

    # (b) by ticker alphabetical -- account ignored, so Beta's AAA leads.
    assert [t for _, t in _line_items(project(rep, sort_key="ticker"))
            ] == ["AAA", "MID", "SYNTH"]

    # (c) by account then ticker -- Alpha's holdings first (ticker as the secondary
    # key), THEN Beta's. Distinct from a pure ticker sort.
    assert _line_items(project(rep, sort_key="account")) == [
        ("Alpha", "MID"), ("Alpha", "SYNTH"), ("Beta", "AAA")]

    # (d) by gain percentage, largest first: MID 100% > SYNTH 50% > AAA 20%.
    assert [t for _, t in _line_items(project(rep, sort_key="pct", sort_desc=True))
            ] == ["MID", "SYNTH", "AAA"]

    # A second click toggles direction (the mechanism the window drives).
    assert [t for _, t in _line_items(project(rep, sort_key="pct", sort_desc=False))
            ] == ["AAA", "SYNTH", "MID"]


def test_sorting_never_moves_the_portfolio_totals(conn, world):
    """Sorting is a display reshuffle of the line items only; the Portfolio section
    stays put at the bottom regardless of the order or direction."""
    from mammon.ui import report_window as rw

    start, end = resolve_period("last_3_years", conn, TODAY)
    rep = run_report(conn, end, start=start)
    project = rw.INVESTMENT_PERFORMANCE_SPEC.project

    for key in ("gain", "ticker", "account", "pct"):
        for desc in (False, True):
            rows = project(rep, sort_key=key, sort_desc=desc)
            totals = [r.label for r in rows if r.section == "Portfolio"]
            # The Portfolio block is contiguous and trailing.
            portfolio_start = next(i for i, r in enumerate(rows)
                                   if r.section == "Portfolio")
            assert all(r.section == "Portfolio" for r in rows[portfolio_start:])
            assert "Market Value" in totals

"""Tests for mammon.ui.investment_center (Investment Center, Phases 1 to 5).

The life-cycle test is the point: build a real database with ``init_db``, seed a
brokerage account with two securities, lots, a dividend and a price history,
construct the page against that connection, and assert the RENDERED text -- the
card's total against what ``investments.account_valuation`` reports for the same
data, and the holdings rows against hand-computed share counts and values. Then
write another lot and drive ``mark_stale`` / ``refresh_if_stale`` through a
round trip, which is the contract the main window's stacked home area will use.

Phase 2 adds the same treatment for the allocation and performance panels. Two
of those assertions exist to pin down a difference rather than a number: the
allocation's total is deliberately NOT the card's (options out, money-market as
cash), and an account funded only by an opening balance gets no return at all,
because the "gain" would be the funding arriving. Both are asserted as stated
behavior so a later "fix" that quietly reconciled them would fail here.

Phases 3 and 4 add the activity and freshness panels, and two of their
assertions pin a judgement rather than a number as well: a cash-only row's
Quantity cell is deliberately BLANK rather than "0", and an age is measured
against the page's as-of date rather than today's, so a ledger that has not been
downloaded for a year does not suddenly call every price stale.

Phase 5 adds the rebalance-drift panel, against a target whose weights make
every figure hand-computable, and pins two refusals rather than numbers: the
domain RAISES when there is no target, and the panel renders that reason inline
instead of a dialog or a traceback; and the unclassified bucket is never judged
against a band and never given a trade, because it is a gap in the records
rather than a position off its weight.

All data is synthetic: the tickers are made up (ZZ-prefixed, not issued), the
account and security names are inventions, and no position here corresponds to
anyone's holdings.
"""
from __future__ import annotations

import ast
import os
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, investments, ledger, portfolio, rebalance
from mammon.ui.investment_center import (
    ACTIVITY_BASIS,
    ACTIVITY_COLUMNS,
    ACTIVITY_EMPTY_TEXT,
    ACTIVITY_N,
    ALLOCATION_COLUMNS,
    ALLOCATION_EMPTY_TEXT,
    COLUMNS,
    DRIFT_COLUMNS,
    DRIFT_EMPTY_TEXT,
    DRIFT_IN_BAND,
    DRIFT_NO_TARGET_TEXT,
    DRIFT_ON_TARGET_TEXT,
    DRIFT_OUT_OF_BAND,
    DRIFT_UNCLASSIFIED_TEXT,
    EMPTY_TEXT,
    FRESHNESS_COLUMNS,
    FRESHNESS_CURRENT,
    FRESHNESS_EMPTY_TEXT,
    FRESHNESS_NO_PRICE,
    FRESHNESS_STALE,
    PERFORMANCE_COLUMNS,
    PERFORMANCE_EMPTY_TEXT,
    STALE_AFTER_DAYS,
    TOTAL_ROW_LABEL,
    UNPRICED_MARK,
    InvestmentCenterPanel,
    account_returns,
    asset_allocation,
    fmt_move,
    fmt_points,
    fmt_price,
    fmt_qty,
    period_start,
    portfolio_summary,
    price_freshness,
    rebalance_drift,
    recent_activity,
)
from mammon.ui.models import fmt_cents, fmt_date, fmt_money
from mammon.tests import fresh_db

PRICE_DATE = "2026-03-31"

#: An earlier close, so the performance window has something to start from.
START_PRICE_DATE = "2026-02-28"

#: The day after START_PRICE_DATE: pinned so the window's opening value is the
#: February close rather than whatever twelve months back happens to be.
WINDOW_START = "2026-03-01"


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "investment_center.db")
    yield c
    c.close()


def _buy(conn, account_id, date, symbol, qty, price, amount):
    investments.record_investment(conn, account_id, date, "Buy", symbol=symbol,
                                  quantity=qty, price=price, amount=amount)
    investments.rebuild_holdings(conn, account_id)


@pytest.fixture
def seeded(conn):
    """One brokerage account holding two made-up securities, plus a checking
    account that must NOT count toward the portfolio.

    ZZAA: 10 shares, cost 1000.00, priced 150.00 -> 1,500.00 market value
    ZZBB: 50 shares, cost 1000.00, priced  21.00 -> 1,050.00 market value
    """
    brokerage = ledger.create_account(conn, "Test Brokerage", "investment",
                                      opening_balance=5000_00,
                                      opening_date="2026-01-01")
    ledger.create_account(conn, "Test Checking", "checking",
                          opening_balance=2500_00, opening_date="2026-01-01")
    portfolio.set_security(conn, "ZZAA", name="Zeta Alpha Growth Fund",
                           sec_type="fund")
    portfolio.set_security(conn, "ZZBB", name="Zeta Beta Bond Fund",
                           sec_type="fund")
    _buy(conn, brokerage, "2026-01-05", "ZZAA", "10", "100.00", -1000_00)
    _buy(conn, brokerage, "2026-01-06", "ZZBB", "50", "20.00", -1000_00)
    investments.record_investment(conn, brokerage, "2026-02-01", "Div",
                                  symbol="ZZAA", amount=25_00)
    investments.rebuild_holdings(conn, brokerage)
    investments.record_price(conn, "ZZAA", PRICE_DATE, "150.00")
    investments.record_price(conn, "ZZBB", PRICE_DATE, "21.00")
    return brokerage


# ---------------------------------------------------------------------------
# The summary: composition over the domain layer, no re-derived math
# ---------------------------------------------------------------------------
def test_summary_scopes_to_investment_accounts_only(conn, seeded):
    summary = portfolio_summary(conn)
    assert summary.account_ids == [seeded]
    assert not summary.is_empty
    assert summary.as_of == PRICE_DATE


def test_summary_totals_match_account_valuation(conn, seeded):
    summary = portfolio_summary(conn)
    valuation = investments.account_valuation(conn, seeded, PRICE_DATE)
    assert (summary.cash, summary.securities, summary.total) == (
        valuation.cash, valuation.securities, valuation.total)
    # ... and the securities half is the hand-computed figure, so a domain
    # change that moved both sides together would still be caught.
    assert summary.securities == 1500_00 + 1050_00
    assert summary.total == summary.cash + summary.securities


def test_summary_holdings_are_largest_first(conn, seeded):
    summary = portfolio_summary(conn)
    assert [(h.symbol, h.quantity, h.market_value) for h in summary.holdings] == [
        ("ZZAA", Decimal("10"), 1500_00),
        ("ZZBB", Decimal("50"), 1050_00),
    ]
    assert [h.name for h in summary.holdings] == ["Zeta Alpha Growth Fund",
                                                  "Zeta Beta Bond Fund"]
    assert summary.unpriced == []


def test_same_symbol_in_two_accounts_is_one_row(conn, seeded):
    second = ledger.create_account(conn, "Test Rollover IRA", "investment",
                                   opening_balance=0, opening_date="2026-01-01")
    _buy(conn, second, "2026-02-10", "ZZBB", "50", "20.00", -1000_00)
    summary = portfolio_summary(conn)
    assert sorted(summary.account_ids) == sorted([seeded, second])
    by_symbol = {h.symbol: h for h in summary.holdings}
    assert set(by_symbol) == {"ZZAA", "ZZBB"}
    assert by_symbol["ZZBB"].quantity == Decimal("100")
    assert by_symbol["ZZBB"].market_value == 2100_00
    # Merged largest-first: ZZBB now outweighs ZZAA.
    assert [h.symbol for h in summary.holdings] == ["ZZBB", "ZZAA"]


def test_unpriced_symbol_is_kept_but_marked(conn, seeded):
    portfolio.set_security(conn, "ZZCC", name="Zeta Gamma Private Fund")
    _buy(conn, seeded, "2026-02-20", "ZZCC", "7", "10.00", -70_00)
    summary = portfolio_summary(conn)
    assert "ZZCC" in summary.unpriced
    unpriced = [h for h in summary.holdings if h.symbol == "ZZCC"][0]
    assert unpriced.price is None and unpriced.market_value == 0
    assert summary.securities == 1500_00 + 1050_00


# ---------------------------------------------------------------------------
# The page: what the user actually sees
# ---------------------------------------------------------------------------
def test_card_shows_the_valuation_account_valuation_reports(qapp, conn, seeded):
    panel = InvestmentCenterPanel(conn)
    valuation = investments.account_valuation(conn, seeded, PRICE_DATE)
    assert panel.total_label.text() == fmt_money(valuation.total)
    assert panel.securities_label.text() == fmt_money(valuation.securities)
    assert panel.cash_label.text() == fmt_money(valuation.cash)
    assert panel.as_of_label.text() == "as of " + fmt_date(PRICE_DATE)
    assert panel.empty_label.isHidden()
    assert not panel.card.isHidden()


def test_top_holdings_table_rows(qapp, conn, seeded):
    panel = InvestmentCenterPanel(conn)
    assert [panel.table.horizontalHeaderItem(i).text()
            for i in range(panel.table.columnCount())] == list(COLUMNS)
    assert panel.table.rowCount() == 2
    assert panel.row_text(0) == ("ZZAA", "Zeta Alpha Growth Fund", "10", "150.00",
                                 fmt_cents(1500_00), "58.8%")
    assert panel.row_text(1) == ("ZZBB", "Zeta Beta Bond Fund", "50", "21.00",
                                 fmt_cents(1050_00), "41.2%")
    assert panel.unpriced_label.text() == ""


def test_table_is_read_only(qapp, conn, seeded):
    from PyQt5.QtWidgets import QAbstractItemView

    panel = InvestmentCenterPanel(conn)
    assert panel.table.editTriggers() == QAbstractItemView.NoEditTriggers


def test_top_n_limits_the_rows_not_the_summary(qapp, conn, seeded):
    panel = InvestmentCenterPanel(conn, top_n=1)
    assert panel.table.rowCount() == 1
    assert panel.row_text(0)[0] == "ZZAA"
    assert len(panel.summary.holdings) == 2


def test_unpriced_holding_renders_a_mark_and_a_note(qapp, conn, seeded):
    portfolio.set_security(conn, "ZZCC", name="Zeta Gamma Private Fund")
    _buy(conn, seeded, "2026-02-20", "ZZCC", "7", "10.00", -70_00)
    panel = InvestmentCenterPanel(conn)
    row = [panel.row_text(r) for r in range(panel.table.rowCount())
           if panel.row_text(r)[0] == "ZZCC"][0]
    assert row[COLUMNS.index("Price")] == UNPRICED_MARK
    assert row[COLUMNS.index("Market Value")] == UNPRICED_MARK
    assert "ZZCC" in panel.unpriced_label.text()


def test_empty_database_renders_the_empty_state(qapp, conn):
    panel = InvestmentCenterPanel(conn)
    assert panel.summary.is_empty
    assert not panel.empty_label.isHidden()
    assert panel.empty_label.text() == EMPTY_TEXT
    assert panel.card.isHidden() and panel.table.isHidden()
    assert panel.table.rowCount() == 0
    assert panel.as_of_label.text() == ""


def test_ledger_without_investment_accounts_is_also_empty(qapp, conn):
    ledger.create_account(conn, "Test Checking", "checking",
                          opening_balance=2500_00, opening_date="2026-01-01")
    panel = InvestmentCenterPanel(conn)
    assert panel.summary.is_empty
    assert not panel.empty_label.isHidden()


# ---------------------------------------------------------------------------
# The refresh contract the stacked home area relies on
# ---------------------------------------------------------------------------
def test_mark_stale_refresh_if_stale_round_trip(qapp, conn, seeded):
    """The full life cycle: a populated page, a write behind its back, the
    invalidate/recompute round trip, and the new figure on screen."""
    panel = InvestmentCenterPanel(conn)
    before = investments.account_valuation(conn, seeded, PRICE_DATE)
    assert panel.total_label.text() == fmt_money(before.total)
    # Nothing has changed: refresh_if_stale must not recompute.
    assert panel.refresh_if_stale() is False

    _buy(conn, seeded, "2026-03-02", "ZZBB", "10", "21.00", -210_00)
    # A write the page has not been told about leaves it showing the old number.
    assert panel.total_label.text() == fmt_money(before.total)

    panel.mark_stale()
    assert panel.refresh_if_stale() is True
    after = investments.account_valuation(conn, seeded, PRICE_DATE)
    assert after.securities == 1500_00 + 1260_00
    assert panel.total_label.text() == fmt_money(after.total)
    assert panel.securities_label.text() == fmt_money(after.securities)
    row = {panel.row_text(r)[0]: panel.row_text(r)
           for r in range(panel.table.rowCount())}["ZZBB"]
    assert row[COLUMNS.index("Shares")] == "60"
    assert row[COLUMNS.index("Market Value")] == fmt_cents(1260_00)
    # Recomputed once, and only once.
    assert panel.refresh_if_stale() is False


def test_mark_stale_redraws_immediately_when_visible(qapp, conn, seeded):
    panel = InvestmentCenterPanel(conn)
    panel.show()
    qapp.processEvents()
    if not panel.isVisible():           # platform plugin refused to map it
        pytest.skip("widget never became visible under this platform plugin")
    _buy(conn, seeded, "2026-03-02", "ZZBB", "10", "21.00", -210_00)
    panel.mark_stale()
    after = investments.account_valuation(conn, seeded, PRICE_DATE)
    assert panel.total_label.text() == fmt_money(after.total)
    assert panel.refresh_if_stale() is False
    panel.hide()


def test_show_event_catches_up_a_stale_hidden_page(qapp, conn, seeded):
    panel = InvestmentCenterPanel(conn)
    _buy(conn, seeded, "2026-03-02", "ZZBB", "10", "21.00", -210_00)
    panel.mark_stale()                  # hidden: deferred, not drawn
    panel.show()
    qapp.processEvents()
    after = investments.account_valuation(conn, seeded, PRICE_DATE)
    assert panel.total_label.text() == fmt_money(after.total)
    panel.hide()


# ---------------------------------------------------------------------------
# Formatting: Decimal in, text out -- and no modal anywhere
# ---------------------------------------------------------------------------
def test_quantity_and_price_formatting_stay_decimal():
    assert fmt_qty(Decimal("10")) == "10"
    assert fmt_qty(Decimal("1234")) == "1,234"
    assert fmt_qty(Decimal("12.3456")) == "12.3456"
    assert fmt_qty(Decimal("0.00374000")) == "0.00374"
    assert fmt_qty(Decimal("0.0000001234")) == "0"      # below display precision
    assert fmt_price(None) == UNPRICED_MARK
    assert fmt_price(Decimal("150.00")) == "150.00"
    assert fmt_price(Decimal("0.1234")) == "0.1234"


# ---------------------------------------------------------------------------
# Phase 2: allocation by asset class
# ---------------------------------------------------------------------------
@pytest.fixture
def classified(conn, seeded):
    """The seeded brokerage with its two securities given asset classes, so the
    allocation has something other than "Unclassified" to say."""
    portfolio.set_security(conn, "ZZAA", name="Zeta Alpha Growth Fund",
                           sec_type="fund", asset_class="domestic_stock")
    portfolio.set_security(conn, "ZZBB", name="Zeta Beta Bond Fund",
                           sec_type="fund", asset_class="bond")
    return seeded


def test_asset_allocation_slices_are_percentages_of_its_own_total(conn, classified):
    view = asset_allocation(conn, PRICE_DATE)
    assert view.as_of == PRICE_DATE
    assert not view.is_empty
    # cash 3,025.00 + ZZAA 1,500.00 + ZZBB 1,050.00
    assert view.total == 3025_00 + 1500_00 + 1050_00
    assert [(s.key, s.value) for s in view.rows] == [
        ("cash", 3025_00), ("domestic_stock", 1500_00), ("bond", 1050_00)]
    assert [str(s.pct) for s in view.rows] == ["54.3", "26.9", "18.8"]
    # Decimal all the way out: the domain's float Slice.pct never reaches here.
    assert all(isinstance(s.pct, Decimal) for s in view.rows)


def test_allocation_panel_rows_and_stated_basis(qapp, conn, classified):
    panel = InvestmentCenterPanel(conn)
    assert [panel.allocation_table.horizontalHeaderItem(i).text()
            for i in range(panel.allocation_table.columnCount())] == list(
                ALLOCATION_COLUMNS)
    assert panel.allocation_table.rowCount() == 3
    assert panel.allocation_row_text(0) == ("Cash", fmt_cents(3025_00), "54.3%")
    assert panel.allocation_row_text(1) == ("Domestic stock", fmt_cents(1500_00),
                                            "26.9%")
    assert panel.allocation_row_text(2) == ("Bonds", fmt_cents(1050_00), "18.8%")
    assert not panel.allocation_table.isHidden()
    assert panel.allocation_empty.isHidden()
    # The basis is on screen, naming the allocation's OWN total -- the whole
    # defence against looking like it contradicts the card above it.
    note = panel.allocation_note.text()
    assert fmt_money(panel.allocation_view.total) in note
    assert "not the portfolio value above" in note


def test_allocation_covers_the_same_accounts_as_the_card(qapp, conn, classified):
    """A checking account is outside the scope for both, so the two totals
    agree here -- they are allowed to differ, but never because one of them
    silently counted a different set of accounts."""
    panel = InvestmentCenterPanel(conn)
    assert panel.allocation_view.total == panel.summary.total


def test_allocation_drops_an_unpriced_symbol_and_says_so(qapp, conn, classified):
    portfolio.set_security(conn, "ZZCC", name="Zeta Gamma Private Fund",
                           asset_class="other")
    _buy(conn, classified, "2026-02-20", "ZZCC", "7", "10.00", -70_00)
    panel = InvestmentCenterPanel(conn)
    assert panel.allocation_view.unpriced == ["ZZCC"]
    assert "other" not in [s.key for s in panel.allocation_view.rows]
    # The 70.00 left the cash, so the allocation's total falls by exactly that.
    assert panel.allocation_view.total == 3025_00 + 1500_00 + 1050_00 - 70_00
    assert "ZZCC" in panel.allocation_note.text()


def test_allocation_empty_state_is_a_label_not_a_dialog(qapp, conn):
    """An investment account with nothing in it: the page is not empty (there
    IS an account), but there is nothing to allocate, so the panel says so
    inline."""
    ledger.create_account(conn, "Test Empty Brokerage", "investment",
                          opening_balance=0, opening_date="2026-01-01")
    panel = InvestmentCenterPanel(conn)
    assert not panel.summary.is_empty
    assert panel.allocation_view.is_empty
    assert panel.allocation_table.isHidden()
    assert not panel.allocation_empty.isHidden()
    assert panel.allocation_empty.text() == ALLOCATION_EMPTY_TEXT


# ---------------------------------------------------------------------------
# Phase 2: per-account performance
# ---------------------------------------------------------------------------
@pytest.fixture
def with_history(conn, seeded):
    """The seeded brokerage plus a February close, so a window opening on
    1 March has a real starting value: ZZAA 10 x 100.00 + ZZBB 50 x 20.00 =
    2,000.00 of securities over 3,025.00 of cash."""
    investments.record_price(conn, "ZZAA", START_PRICE_DATE, "100.00")
    investments.record_price(conn, "ZZBB", START_PRICE_DATE, "20.00")
    return seeded


def test_period_start_is_the_trailing_window(conn):
    assert period_start("2026-03-31") == "2025-04-01"
    assert period_start("2026-03-31", months=1) == "2026-03-01"
    # 29 February steps back to the 28th instead of raising.
    assert period_start("2024-02-29") == "2023-03-01"


def test_account_returns_over_a_pinned_window(conn, with_history):
    view = account_returns(conn, PRICE_DATE, start=WINDOW_START)
    assert (view.start, view.end) == (WINDOW_START, PRICE_DATE)
    assert len(view.rows) == 1
    row = view.rows[0]
    assert row.name == "Test Brokerage"
    assert row.start_value == 3025_00 + 2000_00
    assert row.end_value == 3025_00 + 2550_00
    assert (row.money_in, row.money_out, row.income) == (0, 0, 0)
    assert row.gain == 550_00
    assert row.pct == Decimal("10.9")


def test_performance_panel_rows(qapp, conn, with_history):
    panel = InvestmentCenterPanel(conn, period_start=WINDOW_START)
    assert [panel.performance_table.horizontalHeaderItem(i).text()
            for i in range(panel.performance_table.columnCount())] == list(
                PERFORMANCE_COLUMNS)
    assert panel.performance_table.rowCount() == 1       # no total for one account
    assert panel.performance_row_text(0) == (
        "Test Brokerage", fmt_cents(5025_00), fmt_cents(5575_00),
        fmt_cents(0), fmt_cents(0), fmt_cents(0), fmt_cents(550_00), "10.9%")
    assert panel.performance_period.text() == (
        f"{fmt_date(WINDOW_START)} through {fmt_date(PRICE_DATE)}")
    assert panel.performance_note.text() == ""
    assert panel.performance_empty.isHidden()


def test_performance_adds_a_pooled_total_row_for_two_accounts(qapp, conn, with_history):
    second = ledger.create_account(conn, "Test Rollover IRA", "investment",
                                   opening_balance=0, opening_date="2026-01-01")
    _buy(conn, second, "2026-02-10", "ZZBB", "50", "20.00", -1000_00)
    panel = InvestmentCenterPanel(conn, period_start=WINDOW_START)
    assert panel.performance_table.rowCount() == 3
    names = [panel.performance_row_text(r)[0]
             for r in range(panel.performance_table.rowCount())]
    assert names == ["Test Brokerage", "Test Rollover IRA", TOTAL_ROW_LABEL]
    total = panel.performance_view.total
    assert total.start_value == sum(r.start_value for r in panel.performance_view.rows)
    assert total.end_value == sum(r.end_value for r in panel.performance_view.rows)
    assert panel.performance_row_text(2)[PERFORMANCE_COLUMNS.index("End Value")] == (
        fmt_cents(total.end_value))


def test_account_funded_only_by_an_opening_balance_earns_no_return(qapp, conn,
                                                                   with_history):
    """The honest refusal: over a window that contains the account's creation,
    the start value is zero and an opening balance is not an external flow, so
    the domain withholds the percentage -- and both the Gain and the Return cell
    show the mark rather than a number reading as an infinite return."""
    panel = InvestmentCenterPanel(conn, period_start="2025-04-01")
    row = panel.performance_view.rows[0]
    assert (row.start_value, row.money_in) == (0, 0)
    assert row.pct is None and row.gain is None
    text = panel.performance_row_text(0)
    assert text[PERFORMANCE_COLUMNS.index("Gain")] == UNPRICED_MARK
    assert text[PERFORMANCE_COLUMNS.index("Return")] == UNPRICED_MARK
    assert UNPRICED_MARK in panel.performance_note.text()
    assert not panel.performance_note.isHidden()


def test_performance_empty_state_is_a_label_not_a_dialog(qapp, conn):
    """An investment account with no transaction and no price: no valuation
    date, so there is no period to measure."""
    ledger.create_account(conn, "Test Empty Brokerage", "investment",
                          opening_balance=0, opening_date="2026-01-01")
    panel = InvestmentCenterPanel(conn)
    assert panel.performance_view.is_empty
    assert panel.performance_table.isHidden()
    assert not panel.performance_empty.isHidden()
    assert panel.performance_empty.text() == PERFORMANCE_EMPTY_TEXT
    assert panel.performance_period.text() == ""


# ---------------------------------------------------------------------------
# Phase 2: the new panels obey the same staleness contract
# ---------------------------------------------------------------------------
def test_new_panels_refresh_through_mark_stale(qapp, conn, classified):
    panel = InvestmentCenterPanel(conn, period_start=WINDOW_START)
    before_rows = [(s.key, s.value) for s in panel.allocation_view.rows]
    assert panel.refresh_if_stale() is False

    _buy(conn, classified, "2026-03-02", "ZZBB", "10", "21.00", -210_00)
    assert [(s.key, s.value) for s in panel.allocation_view.rows] == before_rows

    panel.mark_stale()
    assert panel.refresh_if_stale() is True
    after = {s.key: s.value for s in panel.allocation_view.rows}
    assert after["bond"] == 1260_00                  # 60 shares at 21.00
    assert after["cash"] == 3025_00 - 210_00
    assert panel.allocation_row_text(0)[ALLOCATION_COLUMNS.index("Value")] == (
        fmt_cents(after["cash"]))
    end = panel.performance_view.rows[0].end_value
    assert panel.performance_row_text(0)[
        PERFORMANCE_COLUMNS.index("End Value")] == fmt_cents(end)
    assert panel.refresh_if_stale() is False


def test_new_panels_are_hidden_on_an_empty_ledger(qapp, conn):
    panel = InvestmentCenterPanel(conn)
    assert panel.allocation_view is None and panel.performance_view is None
    assert panel.allocation_table.isHidden() and panel.allocation_title.isHidden()
    assert panel.performance_table.isHidden() and panel.performance_title.isHidden()
    assert panel.allocation_table.rowCount() == 0
    assert panel.performance_table.rowCount() == 0


def test_new_tables_are_read_only(qapp, conn, classified):
    from PyQt5.QtWidgets import QAbstractItemView

    panel = InvestmentCenterPanel(conn)
    assert panel.allocation_table.editTriggers() == QAbstractItemView.NoEditTriggers
    assert panel.performance_table.editTriggers() == QAbstractItemView.NoEditTriggers


# ---------------------------------------------------------------------------
# Phase 3: recent activity
# ---------------------------------------------------------------------------
def test_recent_activity_composes_the_domain_query(conn, seeded):
    view = recent_activity(conn)
    assert view.limit == ACTIVITY_N
    assert not view.is_empty
    assert [(r.date, r.action, r.symbol) for r in view.rows] == [
        ("2026-02-01", "Div", "ZZAA"),
        ("2026-01-06", "Buy", "ZZBB"),
        ("2026-01-05", "Buy", "ZZAA")]


def test_activity_table_rows_and_stated_basis(qapp, conn, seeded):
    panel = InvestmentCenterPanel(conn)
    assert [panel.activity_table.horizontalHeaderItem(i).text()
            for i in range(panel.activity_table.columnCount())] == list(
                ACTIVITY_COLUMNS)
    assert panel.activity_table.rowCount() == 3
    # Newest first, named by ACCOUNT rather than numbered by id. The dividend's
    # Quantity cell is blank: no shares changed hands, and "0" would say they did.
    assert panel.activity_row_text(0) == (
        fmt_date("2026-02-01"), "Test Brokerage", "Div", "ZZAA", "",
        fmt_cents(25_00))
    assert panel.activity_row_text(1) == (
        fmt_date("2026-01-06"), "Test Brokerage", "Buy", "ZZBB", fmt_qty(
            Decimal("50")), fmt_cents(-1000_00))
    assert panel.activity_row_text(2)[ACTIVITY_COLUMNS.index("Symbol")] == "ZZAA"
    assert not panel.activity_table.isHidden()
    assert panel.activity_empty.isHidden()
    # The list is truncated by design, and says so on screen.
    assert panel.activity_note.text() == ACTIVITY_BASIS.format(n=3)


def test_activity_spans_every_account_on_the_page(qapp, conn, seeded):
    second = ledger.create_account(conn, "Test Rollover IRA", "investment",
                                   opening_balance=0, opening_date="2026-01-01")
    _buy(conn, second, "2026-02-10", "ZZBB", "50", "20.00", -1000_00)
    panel = InvestmentCenterPanel(conn)
    assert panel.activity_table.rowCount() == 4
    assert panel.activity_row_text(0)[:4] == (
        fmt_date("2026-02-10"), "Test Rollover IRA", "Buy", "ZZBB")
    # The scope is the card's scope: the checking account never appears.
    names = {panel.activity_row_text(r)[ACTIVITY_COLUMNS.index("Account")]
             for r in range(panel.activity_table.rowCount())}
    assert names == {"Test Brokerage", "Test Rollover IRA"}


def test_activity_omits_a_voided_transaction(qapp, conn, seeded):
    txn = investments.record_investment(conn, seeded, "2026-03-02", "Sell",
                                        symbol="ZZAA", quantity="1",
                                        price="150.00", amount=150_00)
    investments.rebuild_holdings(conn, seeded)
    panel = InvestmentCenterPanel(conn)
    assert panel.activity_row_text(0)[ACTIVITY_COLUMNS.index("Action")] == "Sell"

    investments.void_investment(conn, txn)
    investments.rebuild_holdings(conn, seeded)
    panel.mark_stale()
    assert panel.refresh_if_stale() is True
    assert panel.activity_table.rowCount() == 3
    assert "Sell" not in [panel.activity_row_text(r)[ACTIVITY_COLUMNS.index("Action")]
                          for r in range(panel.activity_table.rowCount())]


def test_activity_cap_limits_the_rows(qapp, conn, seeded):
    panel = InvestmentCenterPanel(conn)
    panel.activity_n = 2
    panel.mark_stale()
    panel.refresh_if_stale()
    assert panel.activity_table.rowCount() == 2
    assert panel.activity_view.limit == 2
    assert panel.activity_row_text(0)[ACTIVITY_COLUMNS.index("Action")] == "Div"
    assert panel.activity_note.text() == ACTIVITY_BASIS.format(n=2)


def test_activity_empty_state_is_a_label_not_a_dialog(qapp, conn):
    """An investment account funded by an opening balance and nothing else: the
    page is not empty, but nothing was ever bought, sold or paid out."""
    ledger.create_account(conn, "Test Empty Brokerage", "investment",
                          opening_balance=1000_00, opening_date="2026-01-01")
    panel = InvestmentCenterPanel(conn)
    assert not panel.summary.is_empty
    assert panel.activity_view.is_empty
    assert panel.activity_table.isHidden()
    assert not panel.activity_empty.isHidden()
    assert panel.activity_empty.text() == ACTIVITY_EMPTY_TEXT
    assert panel.activity_note.text() == ""


# ---------------------------------------------------------------------------
# Phase 4: price data freshness
# ---------------------------------------------------------------------------
def test_freshness_rates_every_held_symbol(qapp, conn, seeded):
    panel = InvestmentCenterPanel(conn)
    assert [panel.freshness_table.horizontalHeaderItem(i).text()
            for i in range(panel.freshness_table.columnCount())] == list(
                FRESHNESS_COLUMNS)
    assert panel.freshness_table.rowCount() == 2
    assert panel.freshness_row_text(0) == (
        "ZZAA", "Zeta Alpha Growth Fund", fmt_date(PRICE_DATE), "0",
        FRESHNESS_CURRENT)
    assert panel.freshness_row_text(1)[0] == "ZZBB"
    assert panel.freshness_view.stale == []
    assert panel.freshness_view.as_of == PRICE_DATE
    assert panel.freshness_empty.isHidden()


def test_freshness_flags_a_stale_symbol_and_one_with_no_price_at_all(qapp, conn,
                                                                     seeded):
    portfolio.set_security(conn, "ZZCC", name="Zeta Gamma Value Fund")
    portfolio.set_security(conn, "ZZDD", name="Zeta Delta Private Fund")
    _buy(conn, seeded, "2026-02-20", "ZZCC", "4", "10.00", -40_00)
    _buy(conn, seeded, "2026-02-21", "ZZDD", "3", "10.00", -30_00)
    investments.record_price(conn, "ZZCC", "2025-12-31", "12.00")

    panel = InvestmentCenterPanel(conn)
    view = panel.freshness_view
    # Worst first: never priced, then oldest, then the current ones.
    assert [(r.symbol, r.status) for r in view.rows] == [
        ("ZZDD", FRESHNESS_NO_PRICE), ("ZZCC", FRESHNESS_STALE),
        ("ZZAA", FRESHNESS_CURRENT), ("ZZBB", FRESHNESS_CURRENT)]
    assert [r.symbol for r in view.stale] == ["ZZDD", "ZZCC"]
    assert panel.freshness_row_text(0) == (
        "ZZDD", "Zeta Delta Private Fund", UNPRICED_MARK, UNPRICED_MARK,
        FRESHNESS_NO_PRICE)
    # 90 days from the last close to this page's as-of date.
    assert panel.freshness_row_text(1) == (
        "ZZCC", "Zeta Gamma Value Fund", fmt_date("2025-12-31"), "90",
        FRESHNESS_STALE)


def test_freshness_states_its_threshold_and_reference_date_on_screen(qapp, conn,
                                                                     seeded):
    portfolio.set_security(conn, "ZZCC", name="Zeta Gamma Value Fund")
    _buy(conn, seeded, "2026-02-20", "ZZCC", "4", "10.00", -40_00)
    investments.record_price(conn, "ZZCC", "2025-12-31", "12.00")
    panel = InvestmentCenterPanel(conn)
    note = panel.freshness_note.text()
    # The threshold and the date the ages are measured against are BOTH on the
    # page, for the same reason the allocation states its own total.
    assert str(STALE_AFTER_DAYS) in note
    assert fmt_date(PRICE_DATE) in note
    assert "not today" in note
    assert "ZZCC" in note
    assert not panel.freshness_note.isHidden()


def test_freshness_threshold_is_the_only_thing_that_makes_a_price_stale(qapp, conn,
                                                                        seeded):
    portfolio.set_security(conn, "ZZCC", name="Zeta Gamma Value Fund")
    _buy(conn, seeded, "2026-02-20", "ZZCC", "4", "10.00", -40_00)
    investments.record_price(conn, "ZZCC", "2025-12-31", "12.00")
    panel = InvestmentCenterPanel(conn)
    panel.stale_after_days = 120
    panel.mark_stale()
    panel.refresh_if_stale()
    by_symbol = {r.symbol: r.status for r in panel.freshness_view.rows}
    assert by_symbol["ZZCC"] == FRESHNESS_CURRENT     # 90 days, under 120
    assert panel.freshness_view.stale == []
    assert "120" in panel.freshness_note.text()


def test_freshness_covers_exactly_the_holdings_shown_above_it(qapp, conn, seeded):
    """The two tables read one holdings computation, so the freshness list can
    never name a symbol the portfolio does not hold, or miss one it does."""
    panel = InvestmentCenterPanel(conn, top_n=1)
    assert [r.symbol for r in panel.freshness_view.rows] == sorted(
        h.symbol for h in panel.summary.holdings)


def test_freshness_empty_state_is_a_label_not_a_dialog(qapp, conn):
    ledger.create_account(conn, "Test Empty Brokerage", "investment",
                          opening_balance=1000_00, opening_date="2026-01-01")
    panel = InvestmentCenterPanel(conn)
    assert panel.freshness_view.is_empty
    assert panel.freshness_table.isHidden()
    assert not panel.freshness_empty.isHidden()
    assert panel.freshness_empty.text() == FRESHNESS_EMPTY_TEXT
    assert panel.freshness_note.text() == ""


def test_price_freshness_composer_takes_the_holdings_it_is_given(conn, seeded):
    summary = portfolio_summary(conn)
    view = price_freshness(conn, summary.holdings, as_of=PRICE_DATE)
    assert view.as_of == PRICE_DATE and view.stale_after == STALE_AFTER_DAYS
    assert all(r.status == FRESHNESS_CURRENT for r in view.rows)
    # No holdings, no rows -- and no second query that might have found some.
    assert price_freshness(conn, [], as_of=PRICE_DATE).is_empty


# ---------------------------------------------------------------------------
# Phases 3 and 4: the same staleness contract as every other panel
# ---------------------------------------------------------------------------
def test_activity_and_freshness_refresh_through_mark_stale(qapp, conn, seeded):
    panel = InvestmentCenterPanel(conn)
    assert panel.refresh_if_stale() is False
    before = panel.activity_table.rowCount()

    portfolio.set_security(conn, "ZZCC", name="Zeta Gamma Value Fund")
    _buy(conn, seeded, "2026-03-02", "ZZCC", "4", "10.00", -40_00)
    assert panel.activity_table.rowCount() == before        # not yet told

    panel.mark_stale()
    assert panel.refresh_if_stale() is True
    assert panel.activity_table.rowCount() == before + 1
    assert panel.activity_row_text(0)[ACTIVITY_COLUMNS.index("Symbol")] == "ZZCC"
    assert "ZZCC" in [r.symbol for r in panel.freshness_view.rows]
    assert panel.refresh_if_stale() is False


def test_activity_and_freshness_are_hidden_on_an_empty_ledger(qapp, conn):
    panel = InvestmentCenterPanel(conn)
    assert panel.activity_view is None and panel.freshness_view is None
    assert panel.activity_table.isHidden() and panel.activity_title.isHidden()
    assert panel.freshness_table.isHidden() and panel.freshness_title.isHidden()
    assert panel.activity_table.rowCount() == 0
    assert panel.freshness_table.rowCount() == 0
    assert panel.activity_note.isHidden() and panel.freshness_note.isHidden()


def test_activity_and_freshness_tables_are_read_only(qapp, conn, seeded):
    from PyQt5.QtWidgets import QAbstractItemView

    panel = InvestmentCenterPanel(conn)
    assert panel.activity_table.editTriggers() == QAbstractItemView.NoEditTriggers
    assert panel.freshness_table.editTriggers() == QAbstractItemView.NoEditTriggers


# ---------------------------------------------------------------------------
# Phase 5: rebalance drift
# ---------------------------------------------------------------------------
#: The target the drift fixture sets up. Its weights add to 100 on purpose, so
#: every figure below is drift and not a missing line.
TARGET_NAME = "Test Target 55/40/5"


@pytest.fixture
def targeted(conn, classified):
    """The classified brokerage plus an ACTIVE target to measure it against.

    The sleeve is the same one the allocation panel shows -- cash 3,025.00 +
    ZZAA 1,500.00 (domestic stock) + ZZBB 1,050.00 (bonds) = 5,575.00, the
    checking account excluded -- so every expectation below is hand-computable:

    cash            302,500c / 557,500c = 54.26..% against 55  ->  -0.7 pp
    domestic_stock  150,000c / 557,500c = 26.90..% against 40  -> -13.1 pp
    bond            105,000c / 557,500c = 18.83..% against  5  -> +13.8 pp

    and the target cents (55/40/5 percent of 557,500) come out exact at
    306,625 / 223,000 / 27,875, so the moves are +41.25, +730.00 and -771.25
    with no rounding to argue about.
    """
    rebalance.create_target(conn, TARGET_NAME, active=True,
                            lines={"cash": "55", "domestic_stock": "40",
                                   "bond": "5"})
    return classified


def test_drift_composes_the_domain_report(conn, targeted):
    view = rebalance_drift(conn, PRICE_DATE)
    assert view.refusal == ""
    assert (view.target_name, view.as_of) == (TARGET_NAME, PRICE_DATE)
    assert view.sleeve_total == 3025_00 + 1500_00 + 1050_00
    assert view.target_is_complete
    assert [(r.key, r.value) for r in view.rows] == [
        ("cash", 3025_00), ("domestic_stock", 1500_00), ("bond", 1050_00)]
    assert [str(r.target_pct) for r in view.rows] == ["55.0", "40.0", "5.0"]
    assert [str(r.current_pct) for r in view.rows] == ["54.3", "26.9", "18.8"]
    assert [str(r.drift_pp) for r in view.rows] == ["-0.7", "-13.1", "13.8"]
    assert [r.move_cents for r in view.rows] == [41_25, 730_00, -771_25]
    assert [r.action for r in view.rows] == ["raise", "buy", "sell"]
    assert [r.out_of_band for r in view.rows] == [False, True, True]
    assert (view.out_of_band_count, view.to_move_cents) == (2, 771_25)
    # Decimal all the way out, quantized once at composition: the domain's
    # full-precision ratios are inputs here, never displayed figures.
    assert all(isinstance(r.current_pct, Decimal) and isinstance(r.drift_pp, Decimal)
               for r in view.rows)


def test_drift_is_the_domains_own_figure_rounded_once(conn, targeted):
    """Pins a judgement, not a number: the Drift cell quantizes the domain's
    ``drift_pct`` rather than subtracting the two ROUNDED cells beside it. Here
    the bond row is where that shows -- 18.834 - 5 rounds to +13.8, while the
    displayed 18.8 - 5.0 would say +13.8 only by luck of this data. Re-deriving
    drift in the widget is what would eventually disagree with the domain's own
    band verdict."""
    row = rebalance_drift(conn, PRICE_DATE).rows[2]
    assert row.drift_pp == Decimal("13.8")
    report_row = next(r for r in rebalance.drift(conn, as_of=PRICE_DATE).rows
                      if r.asset_class == "bond")
    assert row.drift_pp == report_row.drift_pct.quantize(Decimal("0.1"))
    assert row.drift_pp != report_row.drift_pct          # rounded for display only


def test_drift_panel_rows_and_flags(qapp, conn, targeted):
    panel = InvestmentCenterPanel(conn)
    assert [panel.drift_table.horizontalHeaderItem(i).text()
            for i in range(panel.drift_table.columnCount())] == list(DRIFT_COLUMNS)
    assert panel.drift_table.rowCount() == 3
    assert panel.drift_row_text(0) == (
        "Cash", fmt_cents(3025_00), "55.0%", "54.3%", "-0.7 pp", "raise",
        "+41.25", DRIFT_IN_BAND)
    assert panel.drift_row_text(1) == (
        "Domestic stock", fmt_cents(1500_00), "40.0%", "26.9%", "-13.1 pp", "buy",
        "+730.00", DRIFT_OUT_OF_BAND)
    assert panel.drift_row_text(2) == (
        "Bonds", fmt_cents(1050_00), "5.0%", "18.8%", "+13.8 pp", "sell",
        "-771.25", DRIFT_OUT_OF_BAND)
    assert not panel.drift_table.isHidden()
    assert panel.drift_empty.isHidden()


def test_drift_names_its_target_and_states_its_sleeve_basis(qapp, conn, targeted):
    """The narrowest basis on the page, so it is the one most worth stating:
    these percentages are of the TARGET's sleeve, not of the portfolio value in
    the card above."""
    panel = InvestmentCenterPanel(conn)
    subtitle = panel.drift_subtitle.text()
    assert TARGET_NAME in subtitle
    assert fmt_date(PRICE_DATE) in subtitle

    note = panel.drift_note.text()
    assert TARGET_NAME in note
    assert fmt_money(5575_00) in note
    assert fmt_date(PRICE_DATE) in note
    assert "sleeve" in note
    assert "not everything owned above" in note
    # The bands are stated as numbers rather than left to be guessed at.
    assert "5 percentage points" in note
    assert "25 percent" in note
    # ... and so is the size of the rebalance those two flagged rows imply.
    assert fmt_money(771_25) in note
    assert DRIFT_ON_TARGET_TEXT not in note


def test_drift_says_so_when_every_class_is_inside_its_band(qapp, conn, classified):
    """The sleeve's own weights as the target: nothing to do, and the panel says
    that rather than leaving an unflagged table to be read as an implication."""
    rebalance.create_target(conn, "Test Target As Held", active=True,
                            lines={"cash": "54.26", "domestic_stock": "26.91",
                                   "bond": "18.83"})
    panel = InvestmentCenterPanel(conn)
    assert panel.drift_view.out_of_band_count == 0
    assert [r.out_of_band for r in panel.drift_view.rows] == [False, False, False]
    assert DRIFT_ON_TARGET_TEXT in panel.drift_note.text()


def test_drift_never_judges_or_trades_the_unclassified_bucket(qapp, conn, targeted):
    """A holding nobody has classified is a gap in the records, not a position
    off its weight: no drift, no trade, no band verdict -- and a note saying
    why the row is blank."""
    portfolio.set_security(conn, "ZZCC", name="Zeta Gamma Value Fund",
                           sec_type="fund")
    _buy(conn, targeted, "2026-03-02", "ZZCC", "20", "10.00", -200_00)
    investments.record_price(conn, "ZZCC", PRICE_DATE, "10.00")
    panel = InvestmentCenterPanel(conn)

    assert panel.drift_view.has_unclassified
    row = next(r for r in panel.drift_view.rows if r.is_unclassified)
    assert row.value == 200_00
    assert (row.drift_pp, row.move_cents, row.out_of_band) == (None, None, None)
    index = [r.key for r in panel.drift_view.rows].index(row.key)
    rendered = panel.drift_row_text(index)
    assert rendered[DRIFT_COLUMNS.index("Drift")] == UNPRICED_MARK
    assert rendered[DRIFT_COLUMNS.index("Amount")] == UNPRICED_MARK
    assert rendered[DRIFT_COLUMNS.index("Band")] == UNPRICED_MARK
    assert rendered[DRIFT_COLUMNS.index("Action")] == "classify"
    assert DRIFT_UNCLASSIFIED_TEXT.format(mark=UNPRICED_MARK) in panel.drift_note.text()


def test_no_target_renders_the_reason_inline_instead_of_raising(qapp, conn,
                                                               classified):
    """The domain REFUSES rather than returning an empty report -- an empty drift
    table would read as "on target" -- so the panel has to catch that and say
    what is missing. A label, never a dialog: CLAUDE.md's headless-modal rule."""
    with pytest.raises(ValueError):
        rebalance.drift(conn, as_of=PRICE_DATE)

    view = rebalance_drift(conn, PRICE_DATE)        # same call, no exception
    assert view.refusal
    assert view.is_empty and view.rows == []

    panel = InvestmentCenterPanel(conn)
    assert not panel.summary.is_empty              # the page itself has data
    assert panel.drift_table.isHidden()
    assert panel.drift_table.rowCount() == 0
    assert panel.drift_subtitle.isHidden()
    assert panel.drift_note.isHidden()
    assert not panel.drift_empty.isHidden()
    assert panel.drift_empty.text() == DRIFT_NO_TARGET_TEXT.format(
        reason=view.refusal)
    # The domain's own wording survives into the label rather than being
    # paraphrased into something that could drift away from the real reason.
    assert view.refusal in panel.drift_empty.text()


def test_drift_refreshes_through_mark_stale(qapp, conn, targeted):
    panel = InvestmentCenterPanel(conn)
    assert panel.refresh_if_stale() is False
    before = [r.value for r in panel.drift_view.rows]

    _buy(conn, targeted, "2026-03-02", "ZZBB", "10", "21.00", -210_00)
    assert [r.value for r in panel.drift_view.rows] == before      # not yet told

    panel.mark_stale()
    assert panel.refresh_if_stale() is True
    after = {r.key: r.value for r in panel.drift_view.rows}
    assert after["bond"] == 1260_00                  # 60 shares at 21.00
    assert after["cash"] == 3025_00 - 210_00
    assert panel.drift_view.sleeve_total == 5575_00  # cash out, bonds in
    assert panel.refresh_if_stale() is False


def test_drift_is_hidden_on_an_empty_ledger(qapp, conn):
    panel = InvestmentCenterPanel(conn)
    assert panel.drift_view is None
    assert panel.drift_table.isHidden() and panel.drift_title.isHidden()
    assert panel.drift_subtitle.isHidden() and panel.drift_note.isHidden()
    assert panel.drift_empty.isHidden()
    assert panel.drift_table.rowCount() == 0


def test_drift_table_is_read_only(qapp, conn, targeted):
    from PyQt5.QtWidgets import QAbstractItemView

    panel = InvestmentCenterPanel(conn)
    assert panel.drift_table.editTriggers() == QAbstractItemView.NoEditTriggers


def test_drift_and_move_formatting_stay_decimal_and_signed():
    """Both columns are read against a target, so the sign is the information:
    an unsigned drift reads as a magnitude and an unsigned amount hides whether
    it is a buy or a sell."""
    assert fmt_points(Decimal("13.834")) == "+13.8 pp"
    assert fmt_points(Decimal("-0.7399")) == "-0.7 pp"
    assert fmt_points(Decimal("0")) == "+0.0 pp"
    assert fmt_points(None) == UNPRICED_MARK
    assert fmt_move(730_00) == "+730.00"
    assert fmt_move(-771_25) == "-771.25"
    assert fmt_move(0) == fmt_cents(0)
    assert fmt_move(None) == UNPRICED_MARK


def test_page_opens_no_modal():
    """CLAUDE.md: a QDialog/QMessageBox built and exec_()-ed on this path would
    block forever under the offscreen platform. This page has no such path --
    it says everything inline."""
    from mammon.ui import investment_center

    tree = ast.parse(Path(investment_center.__file__).read_text(encoding="utf-8"))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not called & {"exec_", "exec", "open"}
    referenced = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    referenced |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    referenced |= {alias.asname or alias.name for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert not referenced & {"QDialog", "QMessageBox", "QInputDialog", "QFileDialog"}

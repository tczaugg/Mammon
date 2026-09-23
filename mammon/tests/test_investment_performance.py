"""mammon.reports.investment_performance: a consolidated per-holding performance
snapshot (symbol, shares, avg/current price, cost basis, market value, unrealized
and realized gain, dividend/interest income, return of capital) plus portfolio
totals, assembled read-only over :mod:`mammon.investments`.

Covers the pure report (per-holding fields, totals, account/hidden/sold filters,
unpriced and closed positions, return-of-capital roll-up, ``as_of`` price capping
and a ``prices`` override), the ReportWindow projector/spec seam, its row
right-click price-history entry (the register's chart, reached from the report),
and the read-only MCP tool (dollar strings, no leaked columns, works under query_only).
Synthetic data only -- no PII.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest

from mammon import db, investments, ledger, mcp_tools
# The reports package re-exports a function named ``investment_performance`` that
# shadows the submodule of the same name, so import the callable directly.
from mammon.reports.investment_performance import investment_performance as run_report
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "perf.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    """Two visible investment accounts, one hidden, one non-investment.

    Brokerage: AAPL (held, priced, a dividend), MSFT (partially sold -> realized
    gain), CLOSED (fully sold -> realized only, zero shares).
    IRA: RC (a return of capital), ZZZ (bought but never priced).
    Old 401k: hidden, holds HID.
    """
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=50000_00)
    bro = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    ira = ledger.create_account(conn, "IRA", "investment", opening_balance=0)
    hid = ledger.create_account(conn, "Old 401k", "investment", opening_balance=0)
    ledger.set_account_hidden(conn, hid, True)
    ledger.add_transaction(conn, chk, "2025-01-01", -20_00, payee="Coffee")

    # Brokerage -----------------------------------------------------------
    investments.record_investment(conn, bro, "2025-01-03", "Buy", symbol="AAPL",
                                  quantity="100", price="100.00", amount=-10000_00)
    investments.record_investment(conn, bro, "2025-06-01", "Div", symbol="AAPL",
                                  amount=200_00)
    investments.record_investment(conn, bro, "2025-02-01", "Buy", symbol="MSFT",
                                  quantity="50", price="200.00", amount=-10000_00)
    investments.record_investment(conn, bro, "2025-08-01", "Sell", symbol="MSFT",
                                  quantity="20", price="250.00", amount=5000_00)
    investments.record_investment(conn, bro, "2025-05-01", "Buy", symbol="CLOSED",
                                  quantity="10", price="50.00", amount=-500_00)
    investments.record_investment(conn, bro, "2025-07-01", "Sell", symbol="CLOSED",
                                  quantity="10", price="60.00", amount=600_00)
    investments.record_price(conn, "AAPL", "2025-12-31", "110.00")
    investments.record_price(conn, "MSFT", "2025-12-31", "210.00")
    investments.rebuild_holdings(conn, bro)

    # IRA -----------------------------------------------------------------
    investments.record_investment(conn, ira, "2025-03-01", "Buy", symbol="RC",
                                  quantity="10", price="100.00", amount=-1000_00)
    investments.record_investment(conn, ira, "2025-09-01", "RtrnCap", symbol="RC",
                                  amount=100_00)
    investments.record_investment(conn, ira, "2025-04-01", "Buy", symbol="ZZZ",
                                  quantity="5", price="10.00", amount=-50_00)
    investments.record_price(conn, "RC", "2025-12-31", "100.00")
    investments.rebuild_holdings(conn, ira)

    # Hidden --------------------------------------------------------------
    investments.record_investment(conn, hid, "2025-01-10", "Buy", symbol="HID",
                                  quantity="5", price="100.00", amount=-500_00)
    investments.record_price(conn, "HID", "2025-12-31", "120.00")
    investments.rebuild_holdings(conn, hid)

    return {"chk": chk, "bro": bro, "ira": ira, "hid": hid}


def _by_symbol(report):
    return {h.symbol: h for h in report.holdings}


# ---------------------------------------------------------------------------
# per-holding fields
# ---------------------------------------------------------------------------
def test_held_priced_holding_carries_cost_value_gain_and_income(conn, world):
    h = _by_symbol(run_report(conn, "2025-12-31"))["AAPL"]
    assert h.account_name == "Brokerage"
    assert h.quantity == Decimal("100")
    assert h.cost_basis == 10000_00
    assert h.price == Decimal("110.00")
    assert h.market_value == 11000_00
    assert h.unrealized_pl == 1000_00
    assert h.realized_pl == 0
    assert h.dividends == 200_00
    assert h.return_of_capital == 0
    assert h.is_open is True
    assert h.avg_cost == Decimal("100")
    assert h.pct_return == Decimal("10")
    assert h.total_pl == 1000_00


def test_partial_sale_books_realized_gain_and_reduces_basis(conn, world):
    h = _by_symbol(run_report(conn, "2025-12-31"))["MSFT"]
    assert h.quantity == Decimal("30")
    assert h.cost_basis == 6000_00        # 30 shares at $200 average
    assert h.market_value == 6300_00      # 30 * $210
    assert h.unrealized_pl == 300_00
    assert h.realized_pl == 1000_00       # 20 * ($250 - $200)
    assert h.pct_return == Decimal("5")
    assert h.total_pl == 1300_00


def test_unpriced_holding_reports_none_not_zero_gain(conn, world):
    h = _by_symbol(run_report(conn, "2025-12-31"))["ZZZ"]
    assert h.is_open is True
    assert h.cost_basis == 50_00
    assert h.price is None
    assert h.market_value == 0
    assert h.unrealized_pl is None
    assert h.pct_return is None
    assert h.avg_cost == Decimal("10")    # cost per share is still known


def test_closed_position_is_realized_only(conn, world):
    h = _by_symbol(run_report(conn, "2025-12-31"))["CLOSED"]
    assert h.is_open is False
    assert h.quantity == Decimal("0")
    assert h.cost_basis == 0
    assert h.market_value == 0
    assert h.unrealized_pl is None
    assert h.price is None
    assert h.realized_pl == 100_00        # 10 * ($60 - $50)
    assert h.avg_cost is None
    assert h.pct_return is None


def test_return_of_capital_is_rolled_up_and_reduces_basis(conn, world):
    h = _by_symbol(run_report(conn, "2025-12-31"))["RC"]
    assert h.return_of_capital == 100_00
    assert h.cost_basis == 900_00         # $1000 basis less the $100 returned
    assert h.market_value == 1000_00
    assert h.unrealized_pl == 100_00


# ---------------------------------------------------------------------------
# portfolio totals and scope
# ---------------------------------------------------------------------------
def test_portfolio_totals_sum_the_visible_holdings(conn, world):
    rep = run_report(conn, "2025-12-31")
    assert rep.total_cost_basis == 16950_00
    assert rep.total_market_value == 18300_00
    assert rep.total_unrealized_pl == 1400_00
    assert rep.total_realized_pl == 1100_00
    assert rep.total_dividends == 200_00
    assert rep.total_return_of_capital == 100_00
    assert rep.total_pl == 2500_00
    assert isinstance(rep.pct_return, Decimal)
    assert round(float(rep.pct_return), 2) == 8.26


def test_non_investment_accounts_never_appear(conn, world):
    rep = run_report(conn, "2025-12-31")
    assert all(h.account_name != "Checking" for h in rep.holdings)


def test_hidden_accounts_excluded_by_default_included_on_request(conn, world):
    default = run_report(conn, "2025-12-31")
    assert "HID" not in _by_symbol(default)

    with_hidden = run_report(conn, "2025-12-31", include_hidden=True)
    hid = _by_symbol(with_hidden)["HID"]
    assert hid.account_name == "Old 401k"
    assert hid.cost_basis == 500_00 and hid.market_value == 600_00
    # Its numbers roll into the totals too.
    assert with_hidden.total_cost_basis == default.total_cost_basis + 500_00
    assert with_hidden.total_market_value == default.total_market_value + 600_00


def test_account_ids_restricts_to_the_chosen_accounts(conn, world):
    rep = run_report(conn, "2025-12-31", account_ids=[world["ira"]])
    assert sorted(h.symbol for h in rep.holdings) == ["RC", "ZZZ"]
    assert rep.account_ids == [world["ira"]]
    assert rep.total_cost_basis == 950_00
    assert rep.total_market_value == 1000_00


def test_include_sold_false_drops_closed_positions(conn, world):
    rep = run_report(conn, "2025-12-31", include_sold=False)
    syms = _by_symbol(rep)
    assert "CLOSED" not in syms
    assert all(h.is_open for h in rep.holdings)
    # Dropping the closed position drops its realized gain from the total.
    assert rep.total_realized_pl == 1000_00


# ---------------------------------------------------------------------------
# valuation date and price override
# ---------------------------------------------------------------------------
def test_as_of_caps_the_valuation_price_only(conn, world):
    # Value is the latest recorded price ON OR BEFORE as-of; the share/cost replay
    # is NOT rewound (still the current 100 shares at either date).
    investments.record_price(conn, "AAPL", "2025-03-01", "90.00")
    investments.record_price(conn, "AAPL", "2026-03-01", "200.00")

    early = _by_symbol(run_report(conn, "2025-06-30"))["AAPL"]
    assert early.quantity == Decimal("100")
    assert early.price == Decimal("90.00")      # the 2025-03-01 price, not the later ones
    assert early.market_value == 9000_00

    late = _by_symbol(run_report(conn, "2026-03-01"))["AAPL"]
    assert late.quantity == Decimal("100")
    assert late.price == Decimal("200.00")
    assert late.market_value == 20000_00


def test_prices_override_wins_over_recorded_history(conn, world):
    rep = run_report(conn, "2025-12-31",
                                    prices={"AAPL": Decimal("120.00")})
    h = _by_symbol(rep)["AAPL"]
    assert h.price == Decimal("120.00")
    assert h.market_value == 12000_00
    assert h.unrealized_pl == 2000_00


def test_bad_as_of_is_rejected(conn, world):
    with pytest.raises(ValueError):
        run_report(conn, "not-a-date")


# ---------------------------------------------------------------------------
# ReportWindow projector / spec seam (pure -- no QApplication needed)
# ---------------------------------------------------------------------------
def test_report_window_projector_and_spec(conn, world):
    from mammon.ui import report_window as rw

    class _Filter:
        def start_iso(self):
            return "2000-01-01"

        def end_iso(self):
            return "2025-12-31"

        def selected_account_ids(self):
            return None

        def include_hidden(self):
            return False

    assert rw.INVESTMENT_PERFORMANCE_SPEC.title == "Investment Performance"
    assert rw.INVESTMENT_PERFORMANCE_SPEC.show_accounts is True

    # The report has its own header: the account/holding label, the ticker, and
    # its numeric columns -- Amount, Dividends, Gain/Loss $, Gain/Loss % and the
    # annual rate. No generic "Section" header, and the gain is broken out, not
    # buried in the label.
    assert rw.INVESTMENT_PERFORMANCE_SPEC.columns == [
        "Account", "Ticker", "Amount", "Dividends", "Gain/Loss $", "Gain/Loss %",
        "Annual Return %"]
    assert "Section" not in rw.INVESTMENT_PERFORMANCE_SPEC.columns

    report = rw.INVESTMENT_PERFORMANCE_SPEC.run(conn, _Filter())
    rows = rw.INVESTMENT_PERFORMANCE_SPEC.project(report)

    # Held positions become line items grouped by account; the closed one does not.
    aapl = [r for r in rows if r.section == "Brokerage" and r.label == "AAPL"]
    assert len(aapl) == 1
    assert aapl[0].amount == 11000_00
    assert not any(r.label.startswith("CLOSED") for r in rows)

    # A sample row maps cell-for-cell onto the header: account, ticker with share
    # count, market value, dividends, gain/loss dollars and percent (text, signed),
    # and the annual rate -- blank, because the span is under a year. The gain
    # INCLUDES the cash dividend: 1,000 price gain + 200 dividend on 10,000.
    cells = rw._row_cells(aapl[0], rw.INVESTMENT_PERFORMANCE_SPEC.columns)
    assert cells == ["Brokerage", "AAPL (100 sh)", "11,000.00", "200.00",
                     "1,200.00", "+12.0%", ""]

    totals = {r.label: r.amount for r in rows if r.section == "Portfolio"}
    assert totals["Cost Basis"] == 16950_00
    assert totals["Market Value"] == 18300_00
    assert totals["Unrealized Gain/Loss"] == 1400_00
    assert totals["Realized Gain/Loss"] == 1100_00
    assert totals["Dividend/Interest Income"] == 200_00
    assert totals["Return of Capital"] == 100_00

    # The Market Value total line breaks the portfolio gain out into its own
    # columns, not just the Amount column. The spec runs the report PERIOD-bounded
    # (the filter's From..To window), so this headline is the sum of the open
    # line items' gains, dividends included -- it reconciles with them, not with
    # the since-purchase unrealized figure. The window starts before every
    # position opened, so it captures each open holding's whole gain, including
    # the realized part of MSFT's partial sale and RC's returned capital: AAPL
    # 1,200 + MSFT 1,300 + RC 100 = 2,600 over the 21,000 put in (12.38% ->
    # +12.4%). The since-purchase totals keep their own labelled lines below.
    mv = [r for r in rows if r.section == "Portfolio" and r.label == "Market Value"][0]
    mv_cells = rw._row_cells(mv, rw.INVESTMENT_PERFORMANCE_SPEC.columns)
    assert mv_cells == ["Portfolio", "Market Value", "18,300.00", "200.00",
                        "2,600.00", "+12.4%", ""]


# ---------------------------------------------------------------------------
# right-click -> price history (parity with the investment register)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def charted_world(conn):
    """One synthetic brokerage holding two invented tickers, each with a couple of
    recorded prices so the shared chart really has a series to draw."""
    bro = ledger.create_account(conn, "Synthetic Brokerage", "investment",
                                opening_balance=0)
    investments.record_investment(conn, bro, "2026-01-05", "Buy", symbol="ZZA",
                                  quantity="10", price="10.00", amount=-100_00)
    investments.record_investment(conn, bro, "2026-01-06", "Buy", symbol="YYB",
                                  quantity="20", price="5.00", amount=-100_00)
    for date, price in (("2026-01-31", "11.00"), ("2026-02-27", "12.00")):
        investments.record_price(conn, "ZZA", date, price)
    for date, price in (("2026-01-31", "6.00"), ("2026-02-27", "7.00")):
        investments.record_price(conn, "YYB", date, price)
    investments.rebuild_holdings(conn, bro)
    return bro


def _fake_menu_class(built):
    """A stand-in QMenu that records the menus built and, on ``exec_``, chooses the
    'Price history' entry -- the same shape test_ui uses for the register's menu.
    A real ``QMenu.exec_`` would block forever under the offscreen platform."""
    class _FakeMenu:
        def __init__(self, *a, **k):
            self.labels = []
            self._acts = {}
            built.append(self)

        def addAction(self, text):
            act = object()
            self.labels.append(text)
            self._acts[text] = act
            return act

        def addSeparator(self):
            pass

        def exec_(self, *a, **k):
            for text, act in self._acts.items():
                if text.startswith("Price history"):
                    return act
            return None

    return _FakeMenu


def _perf_window(conn):
    from mammon.ui.report_window import INVESTMENT_PERFORMANCE_SPEC, ReportWindow

    win = ReportWindow(conn, spec=INVESTMENT_PERFORMANCE_SPEC)
    win.show()
    return win


def _row_showing(win, symbol):
    """The table row whose stashed symbol is ``symbol`` (the display cell reads
    'SYM (n sh)', which is exactly what the menu must NOT parse)."""
    from PyQt5.QtCore import Qt

    for row in range(win.table.rowCount()):
        if win.table.item(row, 0).data(Qt.UserRole) == symbol:
            return row
    raise AssertionError(f"no row for {symbol}")


def test_report_right_click_charts_that_rows_security(conn, charted_world, qapp,
                                                      monkeypatch):
    """Right-clicking a holding line in the Investment Performance report offers
    'Price history: SYM…' and charts THAT row's security -- through the very helper
    the investment register uses -- and keeps doing so after a re-sort."""
    from PyQt5.QtCore import Qt
    from mammon.ui import charts, report_window as rw

    built = []
    charted = []
    monkeypatch.setattr(rw, "QMenu", _fake_menu_class(built))
    monkeypatch.setattr(charts.ChartDialog, "exec_",
                        lambda self: charted.append((self.windowTitle(), self.canvas)))

    win = _perf_window(conn)
    zza = _row_showing(win, "ZZA")
    # The displayed Ticker cell is the composite; the symbol rides on the item.
    assert win.table.item(zza, 1).text().startswith("ZZA (")

    index = win.table.model().index(zza, 1)
    monkeypatch.setattr(win.table, "indexAt", lambda pos: index)
    win._on_table_context_menu(win.table.rect().center())

    assert built and any(l.startswith("Price history: ZZA") for l in built[-1].labels)
    assert len(charted) == 1
    title, canvas = charted[0]
    assert title.startswith("Price History - ZZA")
    assert canvas.figure.axes                       # a real series was drawn
    # The chart is told which account the holding sits in, so it can be labeled in
    # that account's currency (SRD 5.8).
    assert win._chart_account_id(zza, "ZZA") == charted_world

    # Re-sorting by Ticker moves the holdings under the SAME row indexes; the menu
    # must follow the item, not a remembered row -> right-clicking the same index
    # now charts whatever security that row shows.
    win._on_table_sort(1)                            # Ticker, ascending
    win._on_table_sort(1)                            # Ticker, descending
    moved = win.table.item(zza, 0).data(Qt.UserRole)
    assert moved and moved != "ZZA"                  # the two tickers swapped
    assert win.table.item(zza, 1).text().startswith(moved + " (")
    win._on_table_context_menu(win.table.rect().center())
    assert len(charted) == 2
    assert charted[1][0].startswith(f"Price History - {moved}")


def test_report_total_and_empty_rows_offer_no_price_history(conn, charted_world,
                                                            qapp, monkeypatch):
    """A Portfolio total line and a click over no row build no menu at all (never
    an empty popup) and chart nothing."""
    from PyQt5.QtCore import QModelIndex
    from mammon.ui import charts, report_window as rw

    built = []
    charted = []
    monkeypatch.setattr(rw, "QMenu", _fake_menu_class(built))
    monkeypatch.setattr(charts.ChartDialog, "exec_",
                        lambda self: charted.append(self.canvas))

    win = _perf_window(conn)
    totals = [r for r in range(win.table.rowCount())
              if win.table.item(r, 1).text() == "Market Value"]
    assert totals                                    # the report really shows one
    index = win.table.model().index(totals[0], 1)
    monkeypatch.setattr(win.table, "indexAt", lambda pos: index)
    win._on_table_context_menu(win.table.rect().center())
    assert built == [] and charted == []

    monkeypatch.setattr(win.table, "indexAt", lambda pos: QModelIndex())
    win._on_table_context_menu(win.table.rect().center())
    assert built == [] and charted == []


def test_only_the_investment_report_declares_price_history(conn):
    """The menu handler is generic across flat reports, so the OFFER is gated by
    the spec: no other report's rows name a security."""
    from mammon.ui import report_window as rw

    assert rw.INVESTMENT_PERFORMANCE_SPEC.price_history is True
    for spec in (rw.CASH_FLOW_SPEC, rw.BY_PAYEE_SPEC, rw.BY_TAG_SPEC,
                 rw.ACCOUNT_BALANCES_SPEC, rw.TRANSACTIONS_SPEC,
                 rw.INCOME_EXPENSE_SPEC, rw.ITEMIZE_SPEC):
        assert spec.price_history is False


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------
def test_mcp_tool_returns_dollar_strings_and_totals(conn, world):
    out = mcp_tools.investment_performance(conn, as_of="2025-12-31")
    assert out["as_of"] == "2025-12-31"

    holdings = {h["symbol"]: h for h in out["holdings"]}
    aapl = holdings["AAPL"]
    assert aapl["account"] == "Brokerage"
    assert aapl["shares"] == "100"
    assert aapl["cost_basis"] == "10000.00"
    assert aapl["market_value"] == "11000.00"
    assert aapl["unrealized_gain"] == "1000.00"
    assert aapl["pct_return"] == 10.0
    assert aapl["dividends"] == "200.00"
    # Unpriced holding: no price and no gain (null, not a fabricated zero); its
    # market value is zero cents, exactly as the `holdings` tool reports.
    assert holdings["ZZZ"]["price"] is None
    assert holdings["ZZZ"]["unrealized_gain"] is None
    assert holdings["ZZZ"]["pct_return"] is None
    assert holdings["ZZZ"]["market_value"] == "0.00"

    totals = out["totals"]
    assert totals["cost_basis"] == "16950.00"
    assert totals["market_value"] == "18300.00"
    assert totals["unrealized_gain"] == "1400.00"
    assert totals["realized_gain"] == "1100.00"
    assert totals["dividends"] == "200.00"
    assert totals["return_of_capital"] == "100.00"
    assert totals["pct_return"] == 8.26


def test_mcp_tool_registered_and_read_only(conn, world):
    assert mcp_tools.TOOLS["investment_performance"] is mcp_tools.investment_performance
    # Serves under a query_only connection (the MCP server's posture): pure read.
    conn.execute("PRAGMA query_only = ON")
    out = mcp_tools.investment_performance(conn, accounts=["Brokerage"])
    assert {h["symbol"] for h in out["holdings"]} == {"AAPL", "MSFT", "CLOSED"}
    # No identifying account columns leak through the tool's shape.
    forbidden = {"account_number", "url", "download_config"}
    for h in out["holdings"]:
        assert forbidden.isdisjoint(h)

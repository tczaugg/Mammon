"""Tests for the home page's spending trend chart.

Two halves, matching the two layers the feature spans:

* the PURE aggregation ``reports.charts.spending_by_period`` -- money-out per
  calendar bucket, integer cents, transfers excluded, quiet months zero-filled
  -- exercised headless with no Qt at all; and
* the ``ui.charts.SpendingBarCanvas`` renderer, whose value axis must NOT start
  at zero. The locked expectation is that when every period's spending is
  positive the y-axis lower bound stays strictly above zero, so a
  few-hundred-dollar swing is visible against thousand-dollar totals instead of
  being flattened by a zero baseline.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger
from mammon.reports.charts import (
    NetWorthPoint,
    NetWorthSeries,
    PeriodSpending,
    PieSlice,
    SpendingByPeriod,
    SpendingPie,
    spending_by_period,
)
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# --- the aggregation --------------------------------------------------------
def test_spending_by_period_sums_zero_fills_and_excludes_non_spending(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)

    # Two January purchases, one February purchase.
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    ledger.add_transaction(conn, chk, "2026-01-20", -75_00, payee="Costco")
    ledger.add_transaction(conn, chk, "2026-02-10", -50_00, payee="Shell")
    # Income (money IN) is not spending.
    ledger.add_transaction(conn, chk, "2026-03-01", 500_00, payee="Paycheck")
    # A transfer is money moving between your own accounts -- not spending,
    # even though its 'from' leg is a negative (money-out) row.
    ledger.create_transfer(conn, chk, sav, "2026-02-15", 40_00)

    report = spending_by_period(conn, "2026-01-01", "2026-03-31", bucket="month")

    assert [p.key for p in report.periods] == ["2026-01", "2026-02", "2026-03"]
    assert {p.key: p.cents for p in report.periods} == {
        "2026-01": 100_00,      # 25 + 75
        "2026-02": 50_00,       # transfer's -40 leg excluded
        "2026-03": 0,           # income only -> a zero-filled quiet month
    }
    # March is present despite no spending: the range yields every bucket.
    assert report.periods[2].cents == 0
    assert report.total_cents() == 150_00
    assert not report.is_empty()
    # Labels are human, date-formatted through datetime.
    assert report.periods[0].label == "Jan 2026"


def test_spending_by_period_honors_splits(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    groc = ledger.resolve_category(conn, "Groceries")
    fuel = ledger.resolve_category(conn, "Auto & Transport:Fuel")
    txn = ledger.add_transaction(conn, chk, "2026-04-03", -90_00, payee="Warehouse")
    ledger.set_splits(conn, txn, [
        {"category_id": groc, "amount": -60_00},
        {"category_id": fuel, "amount": -30_00},
    ])

    report = spending_by_period(conn, "2026-04-01", "2026-04-30", bucket="month")
    assert {p.key: p.cents for p in report.periods} == {"2026-04": 90_00}


def test_spending_by_period_empty_range_is_flagged(conn):
    ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    report = spending_by_period(conn, "2020-01-01", "2020-03-31", bucket="month")
    assert [p.key for p in report.periods] == ["2020-01", "2020-02", "2020-03"]
    assert report.is_empty()                 # buckets exist, but all zero


# --- the renderer -----------------------------------------------------------
def _positive_report():
    # Both series strictly positive so the non-zero value baseline still applies
    # (income magnitudes chosen distinct from spending, all above zero).
    return SpendingByPeriod(
        start="2026-01-01", end="2026-03-31", bucket="month",
        periods=[
            PeriodSpending("2026-01", "Jan 2026", 1000_00, 1500_00),
            PeriodSpending("2026-02", "Feb 2026", 1200_00, 1100_00),
            PeriodSpending("2026-03", "Mar 2026", 900_00, 950_00),
        ])


def test_bar_chart_y_axis_lower_bound_is_positive_when_all_values_positive(qapp):
    from mammon.ui.charts import SpendingBarCanvas

    canvas = SpendingBarCanvas(_positive_report())
    ax = canvas.figure.axes[0]
    lo, hi = ax.get_ylim()
    # Non-zero baseline: the axis floats above zero...
    assert lo > 0
    # ...and it is an actual zoom -- the bottom sits below the smallest bar
    # ($9,000), not at it, so the smallest bar still has visible height.
    assert 0 < lo < 900_00 / 100.0
    assert hi > 1200_00 / 100.0


def test_bar_chart_empty_report_renders_placeholder(qapp):
    from mammon.ui.charts import SpendingBarCanvas

    empty = SpendingByPeriod(start="2026-01-01", end="2026-01-31",
                             bucket="month",
                             periods=[PeriodSpending("2026-01", "Jan 2026", 0)])
    canvas = SpendingBarCanvas(empty)         # must not raise
    assert canvas.figure.axes                 # a placeholder Axes was drawn


def test_bar_chart_follows_dark_theme(qapp, monkeypatch):
    """The reported defect: in dark mode the chart kept matplotlib's light
    defaults. The canvas must recolor its figure, axes, ticks and title from the
    app's active theme palette (``mammon.ui.style``) so it reads on a dark
    background -- while the deliberate non-zero value baseline still holds. The
    spending/income bars keep their fixed red/green regardless of theme."""
    from matplotlib.colors import to_rgba

    from mammon.ui import charts, style

    monkeypatch.setattr(style, "theme", lambda: "dark")   # same idiom as test_predictions
    dark = style.DARK

    canvas = charts.SpendingBarCanvas(_positive_report())
    ax = canvas.figure.axes[0]

    # Figure + axes backgrounds come from the dark palette, not white.
    assert canvas.figure.patch.get_facecolor() == to_rgba(dark["window"])
    assert ax.get_facecolor() == to_rgba(dark["surface"])
    # Two series render: the first bar group is spending (red), the second income
    # (green). Three months -> three patches each.
    assert len(ax.patches) == 6, "expected grouped spending+income bars"
    assert ax.patches[0].get_facecolor() == to_rgba(charts._RED)
    assert ax.patches[3].get_facecolor() == to_rgba(charts._GREEN)
    # Spines and the title are light, not near-black defaults.
    assert ax.title.get_color() == dark["text"]
    assert to_rgba(ax.spines["left"].get_edgecolor()) == to_rgba(dark["line"])
    # Theming must not disturb the non-zero-baseline zoom (item under test earlier).
    lo, _hi = ax.get_ylim()
    assert lo > 0


def test_bar_chart_light_theme_two_series_red_and_green(qapp, monkeypatch):
    """Light mode: no palette override, so the figure stays on matplotlib's white
    default and the two bar series render in the fixed red (spending) and green
    (income)."""
    from matplotlib.colors import to_rgba

    from mammon.ui import charts, style

    monkeypatch.setattr(style, "theme", lambda: "light")

    canvas = charts.SpendingBarCanvas(_positive_report())
    ax = canvas.figure.axes[0]
    assert canvas.figure.patch.get_facecolor() == to_rgba("white")
    assert len(ax.patches) == 6
    assert ax.patches[0].get_facecolor() == to_rgba(charts._RED)
    assert ax.patches[3].get_facecolor() == to_rgba(charts._GREEN)


def test_bar_chart_dark_to_light_toggle_restores_light(qapp, monkeypatch):
    """The reported regression: a canvas built in DARK mode stayed dark after
    the user switched back to light, because it read the theme only once at
    construction. ``render()`` now re-reads the ACTIVE theme, so re-rendering the
    SAME canvas after a dark->light toggle must restore the classic light look --
    matplotlib's white figure and the original blue bars -- not leave it dark."""
    from matplotlib.colors import to_rgba

    from mammon.ui import charts, style

    # Built dark: figure carries the dark palette; bars stay red/green.
    monkeypatch.setattr(style, "theme", lambda: "dark")
    canvas = charts.SpendingBarCanvas(_positive_report())
    ax = canvas.figure.axes[0]
    assert canvas.figure.patch.get_facecolor() == to_rgba(style.DARK["window"])
    assert ax.patches[0].get_facecolor() == to_rgba(charts._RED)
    assert ax.patches[3].get_facecolor() == to_rgba(charts._GREEN)

    # Toggle to light and re-render the SAME canvas -- it must recolor the figure
    # back to white while keeping the two red/green series.
    monkeypatch.setattr(style, "theme", lambda: "light")
    canvas.render()
    ax = canvas.figure.axes[0]
    assert canvas.figure.patch.get_facecolor() == to_rgba("white")
    assert ax.patches[0].get_facecolor() == to_rgba(charts._RED)
    assert ax.patches[3].get_facecolor() == to_rgba(charts._GREEN)
    # Recoloring must not disturb the deliberate non-zero value baseline.
    lo, _hi = ax.get_ylim()
    assert lo > 0


# --- the other report canvases: dark/light theming + print-white -------------
# The Spending Chart and Income Chart (both ``SpendingPieCanvas``), Net Worth
# (``NetWorthCanvas``) and Asset Allocation (``SlicesPieCanvas``) must follow the
# ACTIVE theme on screen using the SAME idiom as ``SpendingBarCanvas`` (re-read
# ``style.theme()`` on every ``render()``), while a printed/exported chart
# (``for_print=True``) is forced white regardless of theme -- the chart analogue
# of the always-white report/register HTML export.
def _spending_pie() -> SpendingPie:
    return SpendingPie(
        start="2026-01-01", end="2026-03-31",
        slices=[PieSlice("Groceries", 600_00, 0.6),
                PieSlice("Fuel", 400_00, 0.4)],
        total_cents=1000_00)


def _net_worth_series() -> NetWorthSeries:
    return NetWorthSeries(
        start="2026-01-01", end="2026-03-31",
        points=[NetWorthPoint("2026-01-31", 1000_00),
                NetWorthPoint("2026-02-28", 1200_00),
                NetWorthPoint("2026-03-31", 1500_00)])


def _allocation_rows() -> list:
    return [("Real estate", 300000_00), ("Cash", 48490_00), ("Stock", 21000_00)]


def _report_canvas(charts, which, *, for_print=False):
    """One of the four report canvases, by name -- 'spending'/'income' share the
    pie class (they differ only by title), 'net_worth' and 'allocation' are the
    line and slices canvases."""
    if which == "spending":
        return charts.SpendingPieCanvas(_spending_pie(), for_print=for_print)
    if which == "income":
        return charts.SpendingPieCanvas(
            _spending_pie(), title="Income by Category",
            empty_text="No income in this period", for_print=for_print)
    if which == "net_worth":
        return charts.NetWorthCanvas(_net_worth_series(), for_print=for_print)
    if which == "allocation":
        return charts.SlicesPieCanvas("By asset class", _allocation_rows(),
                                      for_print=for_print)
    raise AssertionError(which)


@pytest.mark.parametrize("which", ["spending", "income", "net_worth", "allocation"])
def test_report_canvas_follows_dark_theme(qapp, monkeypatch, which):
    """Each of the four report canvases paints its figure and axes from the dark
    palette (not matplotlib's white default) and lightens its title, so it reads
    on the dark background instead of staying light-on-light."""
    from matplotlib.colors import to_rgba

    from mammon.ui import charts, style

    monkeypatch.setattr(style, "theme", lambda: "dark")
    dark = style.DARK

    canvas = _report_canvas(charts, which)
    ax = canvas.figure.axes[0]
    assert canvas.figure.patch.get_facecolor() == to_rgba(dark["window"])
    assert ax.get_facecolor() == to_rgba(dark["surface"])
    assert ax.title.get_color() == dark["text"]


@pytest.mark.parametrize("which", ["spending", "income", "net_worth", "allocation"])
def test_report_canvas_dark_to_light_toggle_restores_light(qapp, monkeypatch, which):
    """The SpendingBarCanvas regression, now guarded for the other three canvas
    classes: a canvas built DARK must snap its figure back to matplotlib's white
    after a dark->light toggle + re-render, because ``render()`` re-reads the
    ACTIVE theme rather than reading it once at construction."""
    from matplotlib.colors import to_rgba

    from mammon.ui import charts, style

    monkeypatch.setattr(style, "theme", lambda: "dark")
    canvas = _report_canvas(charts, which)
    assert canvas.figure.patch.get_facecolor() == to_rgba(style.DARK["window"])

    monkeypatch.setattr(style, "theme", lambda: "light")
    canvas.render()
    assert canvas.figure.patch.get_facecolor() == to_rgba("white")


@pytest.mark.parametrize("which", ["spending", "income", "net_worth", "allocation"])
def test_report_canvas_print_path_is_white_under_dark_theme(qapp, monkeypatch, which):
    """PRINT/PDF export forces white regardless of theme: even in DARK mode a
    canvas built with ``for_print=True`` paints a white figure, so a printed or
    exported chart is legible on paper -- the chart analogue of the always-white
    report/register HTML export."""
    from matplotlib.colors import to_rgba

    from mammon.ui import charts, style

    monkeypatch.setattr(style, "theme", lambda: "dark")

    canvas = _report_canvas(charts, which, for_print=True)
    assert canvas.figure.patch.get_facecolor() == to_rgba("white")

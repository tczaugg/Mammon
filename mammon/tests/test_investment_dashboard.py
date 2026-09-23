"""Tests for the Investment Dashboard page (``mammon/ui/investment_dashboard.py``).

All data here is SYNTHETIC: ZZ-prefixed tickers that cannot collide with a real
symbol, invented account names, round numbers. Nothing in this file touches a
real ledger.

The page is a picture, so most of what is worth asserting is structural: the
ring draws one wedge per subject with a distinct color each, a wedge click
becomes a filter, the center line states the four numbers the design asks for,
and an account is given an arrow exactly when it received at least four
deposits in the trailing year.
"""
import datetime as dt
import os
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mammon import (crypto, db, forecast, investments, ledger, portfolio,   # noqa: E402
                     security_mix)
from mammon.ui import investment_dashboard as dash         # noqa: E402
from mammon.tests import fresh_db

AS_OF = "2026-06-30"
OPEN_DATE = "2024-01-01"
BUY_DATE = "2024-02-01"


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "investment_dashboard.db")
    yield c
    c.close()


def _buy(conn, account_id, date, symbol, qty, price, amount):
    investments.record_investment(conn, account_id, date, "Buy", symbol=symbol,
                                  quantity=qty, price=price, amount=amount)
    investments.rebuild_holdings(conn, account_id)


@pytest.fixture
def seeded(conn):
    """Two investment accounts, three securities, and two deposit streams: the
    brokerage gets four deposits in the trailing year (an arrow), the IRA gets
    three (no arrow)."""
    checking = ledger.create_account(conn, "Test Checking", "checking",
                                     opening_balance=100_000_00,
                                     opening_date=OPEN_DATE)
    brokerage = ledger.create_account(conn, "Test Brokerage", "investment",
                                      opening_balance=10_000_00,
                                      opening_date=OPEN_DATE)
    ira = ledger.create_account(conn, "Test IRA", "investment",
                                opening_balance=5_000_00, opening_date=OPEN_DATE)

    portfolio.set_security(conn, "ZZAA", name="Zeta Alpha Growth Fund", sec_type="fund")
    portfolio.set_security(conn, "ZZBB", name="Zeta Beta Bond Fund", sec_type="fund")
    portfolio.set_security(conn, "ZZCC", name="Zeta Gamma Index Fund", sec_type="fund")

    _buy(conn, brokerage, BUY_DATE, "ZZAA", "10", "100.00", -1_000_00)
    _buy(conn, brokerage, BUY_DATE, "ZZBB", "50", "20.00", -1_000_00)
    _buy(conn, ira, BUY_DATE, "ZZCC", "20", "50.00", -1_000_00)

    investments.record_investment(conn, brokerage, "2026-02-01", "Div",
                                  symbol="ZZAA", amount=25_00)
    investments.rebuild_holdings(conn, brokerage)

    for symbol, then, now in (("ZZAA", "100.00", "150.00"),
                              ("ZZBB", "20.00", "21.00"),
                              ("ZZCC", "50.00", "55.00")):
        investments.record_price(conn, symbol, BUY_DATE, then)
        investments.record_price(conn, symbol, AS_OF, now)

    # Four deposits into the brokerage inside the trailing 365 days: $2,000.
    for date in ("2025-09-15", "2025-12-15", "2026-03-15", "2026-06-15"):
        ledger.create_transfer(conn, checking, brokerage, date, 500_00,
                               memo="Contribution")
    # Three into the IRA: below the threshold, so no arrow.
    for date in ("2025-10-01", "2026-01-02", "2026-04-01"):
        ledger.create_transfer(conn, checking, ira, date, 100_00,
                               memo="Contribution")
    # One deposit OUTSIDE the window: it must not lift the IRA to four.
    ledger.create_transfer(conn, checking, ira, "2025-01-05", 100_00,
                           memo="Contribution")
    return {"checking": checking, "brokerage": brokerage, "ira": ira}


@pytest.fixture
def page(qapp, conn, seeded):
    p = dash.InvestmentDashboardPage(conn, as_of=AS_OF)
    yield p
    p.deleteLater()


# --- formatting and color assignment ---------------------------------------
def test_money_formatting_is_cents_in_dollars_out():
    assert dash.fmt_money(2_000_00) == "$2,000.00"
    assert dash.fmt_money(-125) == "-$1.25"
    assert dash.fmt_signed(1_234_56) == "+$1,234.56"
    assert dash.fmt_signed(-1_234_56) == "-$1,234.56"


def test_fmt_pct_does_not_render_in_scientific_notation():
    assert dash.fmt_pct(Decimal("50")) == "50.0%"
    assert dash.fmt_pct(Decimal("8.2456")) == "8.2%"


def test_years_before_steps_a_leap_day_back_to_the_28th():
    assert dash.years_before("2026-06-30", 3) == "2023-06-30"
    assert dash.years_before("2024-02-29", 1) == "2023-02-28"


def test_ring_colors_are_distinct_and_independent_of_input_order():
    first = dash.ring_colors(["7", "3", "ZZAA"])
    second = dash.ring_colors(["ZZAA", "3", "7"])
    assert first == second                       # stable across sessions
    assert len(set(first.values())) == 3         # one color per slice


# --- the page builds --------------------------------------------------------
def test_page_builds_against_a_seeded_ledger(page):
    assert page.mode() == dash.MODE_ACCOUNTS
    assert page.filter() is None
    assert page.ring.wedge_count() > 0


def test_the_four_corners_are_real_launchers(page):
    from PyQt5.QtWidgets import QAbstractButton
    for name in dash.CORNER_NAMES:
        btn = page.placeholders[name]
        assert btn.objectName() == name
        # Filled, not reserved: each corner is a button the user can press.
        assert isinstance(btn, QAbstractButton)
        assert btn.isEnabled()
        assert btn.text() == dash.CORNER_LABELS[name]
        assert btn.toolTip()
        # Overlays ON the ring area now, not rows of the page: that is what
        # lets the ring own the full page height.
        assert btn.parent() is page.ring_area
    assert page.corner_buttons == page.placeholders


def test_the_hole_and_the_left_band_are_now_real_widgets(page):
    # What used to be four reserved frames is the actual furniture of 2.3/2.5.
    assert set(page.placeholders) == set(dash.CORNER_NAMES)
    assert isinstance(page.history_chart, dash.ValueHistoryChart)
    assert isinstance(page.projection_chart, dash.ProjectionChart)
    assert isinstance(page.thermometer, dash.Thermometer)
    assert isinstance(page.what_if_bar, dash.WhatIfBar)


def test_the_ring_sits_right_of_page_center_with_the_band_on_the_left(page, qapp):
    from PyQt5.QtCore import QPoint
    page.resize(1200, 800)
    # Nested layouts only lay out once the widget is polished: activating the
    # root layout alone leaves every grandchild at its default 100x30.
    page.show()
    qapp.processEvents()
    band_left = page.left_band.mapTo(page, QPoint(0, 0)).x()
    ring_left = page.ring_area.mapTo(page, QPoint(0, 0)).x()
    assert band_left < ring_left
    assert page.left_band.width() == dash.LEFT_BAND_WIDTH
    center_of_ring = ring_left + page.ring_area.width() / 2
    assert center_of_ring > page.width() / 2
    page.hide()


def test_the_hole_is_wider_than_the_inscribed_square_but_still_inside_the_circle(
        qapp):
    """Reported: "both plots have room to expand to the left". The hole stopped
    being the inscribed SQUARE and became the widest rectangle whose corners are
    still on or inside the inner circle -- so it buys width by giving up height,
    and it can never push a chart corner out from under the ring."""
    ring = dash.RingCanvas([("a", "A", 100), ("b", "B", 100)])
    from PyQt5.QtWidgets import QWidget
    area = dash.RingArea(ring, QWidget())
    area.resize(400, 400)
    x, y, w, h = area.hole_rect()
    import math
    # Against the radius actually DRAWN -- half the short side less the room the
    # data limits reserve for an exploded wedge -- not against half the widget.
    radius = dash.ring_outer_radius(400, 400)
    r_inner = radius * dash.RING_INNER_RADIUS
    square = int(2 * r_inner / math.sqrt(2))
    assert w > square                       # wider than the old square
    assert h < square                       # and that width is paid for in height
    # The RIGHT edge and the height are still the inner circle's business: the
    # rect the inequality allows, unmoved.
    inner_w = int(square * dash.HOLE_WIDTH_SCALE)
    assert (x + w) == pytest.approx((400 + inner_w) // 2, abs=2)
    assert (h / 2.0) ** 2 + (inner_w / 2.0) ** 2 <= r_inner ** 2
    assert y == (400 - h) // 2
    area.deleteLater()


def test_the_hole_is_stretched_left_as_far_as_the_annulus_will_hide(qapp):
    """"Stretch both plots by 10% of their width to the left." Only the left
    edge moves; the bound is the ring's OUTER radius, not its inner one --
    past that corner the hole would leave the donut and reach the band."""
    ring = dash.RingCanvas([("a", "A", 100), ("b", "B", 100)])
    from PyQt5.QtWidgets import QWidget
    area = dash.RingArea(ring, QWidget())
    area.resize(400, 400)
    x, y, w, h = area.hole_rect()
    import math
    radius = dash.ring_outer_radius(400, 400)
    r_inner = radius * dash.RING_INNER_RADIUS
    inner_w = int(2.0 * min(r_inner / math.sqrt(2.0) * dash.HOLE_WIDTH_SCALE,
                            r_inner))
    grow = w - inner_w
    assert grow > 0                                    # it did stretch
    assert grow <= round(inner_w * dash.HOLE_LEFT_STRETCH) + 1
    # ... and the near corners stay under the painted ring.
    left = 400 / 2.0 - x
    assert math.hypot(left, h / 2.0) <= radius * dash.RING_VIEW_LIMIT + 1
    area.deleteLater()


# --- the ring ---------------------------------------------------------------
def test_accounts_mode_draws_one_wedge_per_investment_account(page, seeded):
    assert set(page.ring.keys()) == {str(seeded["brokerage"]), str(seeded["ira"])}
    assert page.ring.wedge_count() == 2
    colors = page.ring.wedge_colors()
    assert len(set(colors.values())) == 2


def test_securities_mode_draws_one_wedge_per_security_plus_cash(page):
    """Reported: "add the cash wedge when displaying securities". Without it the
    two rings totalled different money and the cash simply vanished -- which is
    exactly where an un-reinvested dividend goes."""
    page.set_mode(dash.MODE_SECURITIES)
    assert set(page.ring.keys()) == {"ZZAA", "ZZBB", "ZZCC", dash.CASH_KEY}
    assert page.ring.wedge_count() == 4
    colors = page.ring.wedge_colors()
    assert len(set(colors.values())) == 4


def test_no_small_slice_is_folded_into_an_other_wedge(qapp):
    """A 1% wedge is legible on a ring this size, and it has to stay clickable."""
    slices = [("big", "Big", 99_000_00)] + [
        (f"s{i}", f"Small {i}", 100_00) for i in range(6)
    ]
    ring = dash.RingCanvas(slices)
    assert ring.wedge_count() == 7
    assert charts_group_label_absent(ring)
    ring.deleteLater()


def charts_group_label_absent(ring):
    return all(lab != ring.GROUP_LABEL for lab, _ in ring.drawn_slices())


def test_the_ring_is_a_donut_not_a_pie(qapp):
    ring = dash.RingCanvas([("a", "A", 100), ("b", "B", 300)])
    widths = {w.width for _, w in ring._wedges}
    assert widths == {1.0 - dash.RING_INNER_RADIUS}
    ring.deleteLater()


def test_the_ring_band_is_a_sixth_of_the_width_it_used_to_be(qapp):
    """First "like a third of its current width", then "reduce the ring width to
    half its current width" -- a sixth of the 0.34R originally drawn."""
    ring = dash.RingCanvas([("a", "A", 100), ("b", "B", 300)])
    was = 1.0 - dash.RING_INNER_RADIUS_WAS          # the 0.34R it was drawn at
    now = {w.width for _, w in ring._wedges}.pop()  # what it is drawn at today
    assert now == pytest.approx(was / 6.0, abs=0.005)
    ring.deleteLater()


def test_a_thinner_band_gives_the_hole_a_bigger_square(qapp):
    """The point of the thinner band: both plots inside the hole grow with it,
    and nothing clips them back down."""
    import math

    from PyQt5.QtWidgets import QWidget
    ring = dash.RingCanvas([("a", "A", 100)])
    area = dash.RingArea(ring, QWidget())
    area.resize(400, 400)
    _x, _y, w, _h = area.hole_rect()
    radius = dash.ring_outer_radius(400, 400)
    before = int(2 * radius * dash.RING_INNER_RADIUS_WAS / math.sqrt(2))
    assert w > before
    # And the two canvases are free to take that height -- a leftover floor
    # would be the only thing able to clip them.
    assert dash.ValueHistoryCanvas.MIN_HEIGHT <= w // 2
    area.deleteLater()


def test_an_empty_ring_says_so_rather_than_drawing_nothing(qapp):
    ring = dash.RingCanvas([])
    assert ring.wedge_count() == 0
    texts = [t.get_text() for t in ring.figure.axes[0].texts]
    assert dash.RING_EMPTY_TEXT in texts
    ring.deleteLater()


def test_every_wedge_has_a_hover_tooltip_naming_it(qapp):
    ring = dash.RingCanvas([("7", "Test Brokerage", 300_00), ("8", "Test IRA", 100_00)])
    assert "Test Brokerage" in ring._tooltips
    assert "$300.00" in ring._tooltips["Test Brokerage"]
    assert "75.0%" in ring._tooltips["Test Brokerage"]
    ring.deleteLater()


# --- clicking a wedge filters ----------------------------------------------
def _click_wedge(ring, label):
    """Synthesize a real matplotlib button press over the middle of a wedge, so
    the inherited mpl_connect wiring is exercised and not just ``pick``."""
    import math
    from matplotlib.backend_bases import MouseEvent
    ax = ring.figure.axes[0]
    wedge = dict(ring._wedges)[label]
    mid = math.radians((wedge.theta1 + wedge.theta2) / 2.0)
    r = wedge.r - wedge.width / 2.0
    x = wedge.center[0] + r * math.cos(mid)
    y = wedge.center[1] + r * math.sin(mid)
    px, py = ax.transData.transform((x, y))
    ring.callbacks.process("button_press_event",
                           MouseEvent("button_press_event", ring, px, py, button=1))


def test_clicking_a_wedge_filters_the_page_to_that_account(page, seeded):
    _click_wedge(page.ring, "Test IRA")
    assert page.filter() == ("account", seeded["ira"])
    assert page.ring.selected() == str(seeded["ira"])
    assert page.filter_subject() == "Test IRA"


def test_clicking_the_same_wedge_again_clears_the_filter(page, seeded):
    page.select_slice(seeded["brokerage"])
    assert page.filter() == ("account", seeded["brokerage"])
    page.select_slice(seeded["brokerage"])
    assert page.filter() is None
    assert page.ring.selected() is None


def test_clicking_the_ring_background_clears_the_filter(page, seeded):
    page.select_slice(seeded["brokerage"])
    page.ring.pick(None)
    assert page.filter() is None


def test_a_selected_wedge_is_pulled_out_of_the_ring(qapp):
    ring = dash.RingCanvas([("a", "A", 100), ("b", "B", 300)])
    ring.pick("a")
    exploded = {lab: (w.center[0] ** 2 + w.center[1] ** 2) > 0
                for lab, w in ring._wedges}
    assert exploded == {"A": True, "B": False}
    ring.deleteLater()


def test_the_filter_is_emitted_so_the_charts_can_follow(page, seeded):
    seen = []
    page.filterChanged.connect(seen.append)
    page.select_slice(seeded["ira"])
    page.select_slice(seeded["ira"])
    assert seen == [("account", seeded["ira"]), None]


def test_switching_mode_drops_a_filter_that_no_longer_means_anything(page, seeded):
    page.select_slice(seeded["brokerage"])
    page.set_mode(dash.MODE_SECURITIES)
    assert page.filter() is None
    page.select_slice("ZZAA")
    assert page.filter() == ("security", "ZZAA")


def test_an_unknown_ring_mode_is_refused(page):
    with pytest.raises(ValueError):
        page.set_mode("sectors")


# --- the ring by asset class (reported) -------------------------------------
@pytest.fixture
def classified(conn, seeded):
    """The seeded world with its securities given classes, so by_class has
    something to say."""
    portfolio.set_security(conn, "ZZAA", asset_class="domestic_stock")
    portfolio.set_security(conn, "ZZBB", asset_class="bond")
    portfolio.set_security(conn, "ZZCC", asset_class="intl_stock")
    return seeded


def test_asset_class_is_a_third_ring_mode(page, classified, qapp):
    """Reported: "add an Asset Class button next to Account and Securities"."""
    assert dash.MODE_CLASSES in dash.RING_MODES
    assert set(page.mode_buttons) == set(dash.RING_MODES)
    page.set_mode(dash.MODE_CLASSES)
    assert page.mode() == dash.MODE_CLASSES
    assert page.mode_buttons[dash.MODE_CLASSES].isChecked()
    assert not page.mode_buttons[dash.MODE_ACCOUNTS].isChecked()


def test_the_class_ring_totals_the_same_money_as_the_other_two(conn, classified):
    """Three pictures of one portfolio. by_class already counts cash and splits
    every mixture, so this mode needs no cash wedge of its own."""
    totals = {}
    for mode in dash.RING_MODES:
        slices = dash.ring_slices(conn, mode, AS_OF)
        totals[mode] = sum(c for _k, _l, c in slices)
    assert len(set(totals.values())) == 1, totals


def test_the_class_rings_colors_are_the_reports_colors(page, classified, qapp):
    """Reported: "we already have that in the top bar of the asset allocation
    report". Two pictures of one fact that disagreed about color would be worse
    than one picture."""
    from mammon.ui.asset_allocation import class_colors, unclassified_color
    page.set_mode(dash.MODE_CLASSES)
    qapp.processEvents()
    palette = class_colors()
    for key in page.ring.keys():
        want = unclassified_color() if key == "unclassified" else palette[key]
        assert page.ring.color_for(key) == want, key


def test_an_unallocated_slice_is_named_as_a_gap_not_by_its_key(conn, seeded):
    """In a ring the user reads as a picture of their portfolio, the word has to
    say something is MISSING, not name a category."""
    labels = {k: lab for k, lab, _c in
              dash.ring_slices(conn, dash.MODE_CLASSES, AS_OF)}
    assert labels.get("unclassified") == dash.UNALLOCATED_LABEL


def test_clicking_a_class_states_its_value_and_nothing_else(page, classified,
                                                            qapp):
    """A class is a property OF holdings, not one of them: performance is
    measured on holdings and their flows, so a gain or a rate here would be
    invented. Same rule as the cash wedge."""
    page.set_mode(dash.MODE_CLASSES)
    qapp.processEvents()
    page.select_slice("bond")
    qapp.processEvents()
    assert page._filter == ("class", "bond")
    assert page.filter_subject() == "Bonds"
    line = page.center.line()
    assert line.value_only is True
    assert line.headings() == ["Total"]
    alloc = portfolio.allocation(page.conn, as_of=AS_OF, scope="investments")
    assert line.total == next(s.value for s in alloc.by_class if s.key == "bond")


def test_a_class_scope_plots_that_class_history(page, classified, qapp):
    """A class IS chartable, via class_series -- it just is not chartable by
    value_series, which can only value a holding. It briefly fell back to the
    portfolio's curve for that reason; now it plots its own."""
    page.set_mode(dash.MODE_CLASSES)
    qapp.processEvents()
    page.select_slice("bond")
    qapp.processEvents()
    assert page._scope_symbol() is None
    assert page._scope_class() == "bond"
    plotted = page.history_chart.points()
    assert plotted, "the plot must not be emptied"
    expected = dash.class_series(page.conn, page.history_chart.years(),
                                 asset_class="bond", as_of=AS_OF,
                                 account_ids=page._scope_ids())
    assert plotted == expected
    # ...and it is NOT the portfolio's curve, which is what it used to show.
    whole = dash.value_series(page.conn, page.history_chart.years(), as_of=AS_OF,
                              account_ids=page._scope_ids())
    assert plotted[-1][1] < whole[-1][1]


def test_the_cash_wedge_likewise_does_not_empty_the_plots(page, seeded, qapp):
    """Its key is a sentinel, so value_series would price it at zero too."""
    page.set_mode(dash.MODE_SECURITIES)
    qapp.processEvents()
    page.select_slice(dash.CASH_KEY)
    qapp.processEvents()
    assert page._scope_symbol() is None
    assert page.history_chart.points()


def test_the_classes_histories_sum_to_the_portfolios(conn, classified):
    """The identity behind V = H @ C: C's rows are one security's class weights
    and they sum to 1, so summing V's columns gives back H's row sums. If a
    class curve is ever computed some other way, this is what catches it."""
    ids = dash._account_ids(conn)
    whole = dash.value_series(conn, 3, as_of=AS_OF, account_ids=ids)
    keys = [k for k, _l, _c in dash.ring_slices(conn, dash.MODE_CLASSES, AS_OF,
                                                account_ids=ids)]
    per_class = {k: dict(dash.class_series(conn, 3, asset_class=k, as_of=AS_OF,
                                           account_ids=ids))
                 for k in keys}
    for iso, total in whole:
        summed = sum(series.get(iso, 0) for series in per_class.values())
        assert summed == total, iso


def test_a_class_history_shares_the_portfolio_curves_date_grid(conn, classified):
    """Two curves meant for one pair of axes have to be sampled alike."""
    ids = dash._account_ids(conn)
    whole = [d for d, _c in dash.value_series(conn, 3, as_of=AS_OF,
                                              account_ids=ids)]
    bond = [d for d, _c in dash.class_series(conn, 3, asset_class="bond",
                                             as_of=AS_OF, account_ids=ids)]
    assert bond == whole


def test_a_class_with_nothing_in_it_plots_flat_rather_than_failing(conn,
                                                                   classified):
    ids = dash._account_ids(conn)
    series = dash.class_series(conn, 3, asset_class="real_estate", as_of=AS_OF,
                               account_ids=ids)
    assert all(c == 0 for _d, c in series)


# --- the thermometer follows the selection (reported) -----------------------
def test_selecting_a_security_moves_the_thermometer(page, classified, qapp):
    """Reported: "when I'm on Securities and I click a ring segment, the
    thermometer widget doesn't update".

    The scope was expressed as ACCOUNT IDS and a security is not one, so
    current_mix measured the whole portfolio however the ring was filtered --
    a bond fund and an equity fund in the same account read identically. A
    security HAS a mix; there was no reason to answer with its account's.
    """
    page.set_mode(dash.MODE_SECURITIES)
    qapp.processEvents()
    page.clear_filter()
    qapp.processEvents()
    portfolio_risk = page.thermometer.risk()

    page.select_slice("ZZBB")                     # the bond fund
    qapp.processEvents()
    bond_risk = page.thermometer.risk()
    page.select_slice("ZZAA")                     # the equity fund
    qapp.processEvents()
    equity_risk = page.thermometer.risk()

    assert bond_risk != portfolio_risk
    assert equity_risk > bond_risk, "equity must read riskier than bonds"
    # Back out to the whole portfolio and the needle returns.
    page.clear_filter()
    qapp.processEvents()
    assert page.thermometer.risk() == portfolio_risk


def test_a_securitys_measured_mix_is_its_own(conn, classified):
    """Its stated mixture if it has one, else its single class."""
    assert dash.current_mix(conn, symbol="ZZBB")["bond"] == pytest.approx(1.0)
    security_mix.set_mixture(conn, "ZZBB", {"bond": 70, "domestic_stock": 30})
    mixed = dash.current_mix(conn, symbol="ZZBB")
    assert mixed["bond"] == pytest.approx(0.70)
    assert mixed["domestic_stock"] == pytest.approx(0.30)


def test_selecting_an_asset_class_puts_the_needle_on_that_class(page, classified,
                                                                qapp):
    """A class is all of itself, so the needle reads that class's own risk."""
    page.set_mode(dash.MODE_CLASSES)
    qapp.processEvents()
    page.select_slice("cash")
    qapp.processEvents()
    assert page.thermometer.risk() == pytest.approx(
        forecast.risk_for_mix({"cash": 1.0}), abs=0.01)


# --- the center line --------------------------------------------------------
def test_the_center_line_states_total_gain_dividends_and_annualized(page, conn):
    line = page.center.line()
    everything = sum(investments.account_valuation(conn, a, AS_OF).total
                     for a in portfolio.scope_account_ids(conn, "investments"))
    assert line.total == everything
    assert line.year_gain is not None
    assert line.dividends == 25_00
    assert 1 in line.annualized
    assert set(line.annualized) <= set(dash.ANNUALIZED_YEARS)

    text = page.center_text()
    assert f"Total {dash.fmt_money(everything)}" in text
    assert "1-yr gain " in text
    assert "1-yr dividends $25.00" in text
    assert "1-yr return " in text
    assert page.center.label_texts() == line.parts()

    # The block is a TABLE: every heading has a figure under it, and both rows
    # come from the same columns() pairs.
    assert page.center.heading_texts() == line.headings()
    assert page.center.value_texts() == line.values()
    assert len(page.center.heading_texts()) == len(page.center.value_texts())
    assert page.center.heading_texts()[0] == "Total"
    assert page.center.value_texts()[0] == dash.fmt_money(everything)


def test_a_horizon_without_history_shows_nothing_not_a_dash_or_a_zero():
    line = dash.CenterLine(total=1_000_00, year_gain=50_00, dividends=10_00,
                           annualized={1: Decimal("8.2")})
    text = line.text()
    assert "1-yr return 8.2%" in text
    for absent in ("3-yr return", "5-yr return", "10-yr return"):
        assert absent not in text
    assert "--" not in text and "0.0%" not in text
    # A horizon with no history contributes no COLUMN either, so the table has
    # no empty cell to explain.
    assert [h for h in line.headings() if h.endswith("return")] == ["1-yr return"]


def test_the_block_is_two_rows_with_no_rules_between_them(page, qapp):
    """Reported: "make it a two-line table ... with the headings ... and the
    numbers underneath. No lines, though"."""
    from PyQt5.QtWidgets import QFrame, QGridLayout, QLabel
    page.resize(1200, 800)
    page.show()
    qapp.processEvents()
    lay = page.center.layout()
    assert isinstance(lay, QGridLayout), "columns must share edges with their headings"
    assert lay.rowCount() == 2

    # Every heading sits in row 0 directly above its figure in row 1.
    for i, (head, value) in enumerate(page.center.line().columns(), start=1):
        assert lay.itemAtPosition(0, i).widget().text() == head
        assert lay.itemAtPosition(1, i).widget().text() == value

    # No rules: nothing in the block is a frame, and no label draws a border.
    for child in page.center.findChildren(QFrame):
        assert not isinstance(child, QLabel) or child.frameShape() == QFrame.NoFrame
    assert "border" not in page.center.styleSheet().lower()
    page.hide()


def test_the_figures_are_bigger_than_their_headings_and_carry_the_accent(page, qapp):
    """Reported: "increase the font a bit and color it yellow or something so
    that it stands out". The figures carry the size and the accent; the headings
    recede, or nothing stands out against anything."""
    from mammon.ui import charts
    page.resize(1200, 800)
    page.show()
    qapp.processEvents()
    accent = charts._active_palette()["highlight"]
    muted = charts._active_palette()["muted"]
    for lab in page.center._values:
        assert accent.lower() in lab.styleSheet().lower()
        assert lab.font().bold()
    for lab in page.center._headings:
        assert muted.lower() in lab.styleSheet().lower()
        assert not lab.font().bold()
    heading_px = page.center._headings[0].font().pointSizeF()
    value_px = page.center._values[0].font().pointSizeF()
    assert value_px > heading_px
    page.hide()


def test_the_accent_is_legible_in_both_themes_not_a_literal_yellow():
    """Pure yellow is 1.07:1 on white -- invisible. The accent is a palette
    entry precisely so each theme gets a version that survives its own
    background, and both must clear WCAG AA for normal text (4.5:1)."""
    from mammon.ui import style

    def luminance(hexstr):
        hexstr = hexstr.lstrip("#")
        chan = [int(hexstr[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        chan = [(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
                for c in chan]
        return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2]

    def contrast(a, b):
        la, lb = luminance(a), luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    for palette in (style.LIGHT, style.DARK):
        assert contrast(palette["highlight"], palette["window"]) >= 4.5


def test_the_block_sits_a_line_above_the_midline_clear_of_both_plots(page, qapp):
    """Reported: "raise the centerline text by about one line".

    The strip reserved for it in the hole has to rise by the same amount. If
    only the overlay moved, the text would slide off its blank strip and land on
    the bottom of the top plot -- which is the defect the strip exists for.
    """
    page.resize(1200, 800)
    page.show()
    qapp.processEvents()
    area = page.ring_area
    lift = area.center_lift()
    assert lift > 0
    # About one line of the figures, which is the unit the constant is in.
    assert lift == pytest.approx(page.center.line_height(), abs=2)

    block = page.center.geometry()
    assert block.center().y() == pytest.approx(area.height() // 2 - lift, abs=2)

    # The blank strip in the hole moved with it: the block clears both canvases.
    from PyQt5.QtCore import QPoint, QRect
    for chart in (page.history_chart, page.projection_chart):
        origin = chart.canvas.mapTo(area, QPoint(0, 0))
        canvas = QRect(origin, chart.canvas.size())
        assert not block.intersects(canvas), "the block must not land on a plot"
    page.hide()


def test_filtering_to_an_account_recomputes_the_center_line(page, conn, seeded):
    whole = page.center.line().total
    page.select_slice(seeded["ira"])
    only_ira = page.center.line()
    assert only_ira.total == investments.account_valuation(conn, seeded["ira"],
                                                           AS_OF).total
    assert only_ira.total < whole
    assert only_ira.subject == "Test IRA"
    # The dividend was paid into the brokerage, so the IRA's own line is zero.
    assert only_ira.dividends == 0


def test_filtering_to_a_security_recomputes_the_center_line(page, conn):
    page.set_mode(dash.MODE_SECURITIES)
    page.select_slice("ZZAA")
    line = page.center.line()
    alloc = portfolio.allocation(conn, as_of=AS_OF)
    expected = next(s.value for s in alloc.by_security if s.key == "ZZAA")
    assert line.total == expected
    assert line.subject == "ZZAA"


def test_set_line_leaves_a_usable_size_hint_immediately(page, qapp):
    """The root cause of the vanishing center line.

    A QLabel built for a parent that is ALREADY visible stays hidden until the
    event loop shows it, and a hidden widget adds nothing to its layout's
    sizeHint. Every caller of set_line reads that hint SYNCHRONOUSLY, so the
    rebuilt line measured 4px -- its layout margins alone -- and got placed as a
    sliver. The hint has to be right before set_line returns, not one event-loop
    pass later.
    """
    page.resize(1200, 800)
    page.show()
    qapp.processEvents()
    settled = page.center.sizeHint().height()
    page.center.set_line(dash.center_line(page.conn, page.as_of))
    labels = page.center._headings + page.center._values
    assert [lab.isHidden() for lab in labels] == [False] * len(labels)
    assert page.center.sizeHint().height() == settled
    page.hide()


def test_the_center_line_survives_switching_scope_and_back(page, qapp, seeded):
    """Reported: the center line "disappears in the process of switching from
    the total to the value for one of the accounts or securities and does not
    recover when reverting to the total portfolio".

    It was never hidden and its labels always held the right text -- which is
    why the existing tests, all of which read the DATA, stayed green. What
    collapsed was its geometry: the overlay and the strip reserved for it in the
    hole were both sized from a sizeHint taken while the new labels were still
    hidden, so both became 4px and nothing ever put them back.
    """
    page.resize(1200, 800)
    page.show()
    qapp.processEvents()
    full = page.center.height()
    assert full > 4, "the line should not start out collapsed either"

    def scoped_height(select):
        select()
        qapp.processEvents()
        return page.center.height(), page.center_gap.height()

    for select in (lambda: page.select_slice(seeded["ira"]),
                   page.clear_filter,
                   lambda: (page.set_mode(dash.MODE_SECURITIES),
                            page.select_slice("ZZAA")),
                   page.clear_filter):
        height, gap = scoped_height(select)
        assert height == full, f"center line collapsed to {height}px"
        # The strip reserved in the hole has to track it, or the top plot runs
        # under the text.
        assert gap == full
    assert page.center.isVisible()
    assert page.center.label_texts()[0].startswith("Total ")
    page.hide()


# --- reinvested vs cash dividends (reported) ---------------------------------
@pytest.fixture
def two_dividend_styles(conn):
    """Two identical positions paying identical dividends by different routes.

    $10,000 each, $300 a year for five years, both worth $150/share at AS_OF.
    ZZCASHD banks its dividends; ZZREIND buys shares with them. Everything the
    dashboard says about the pair should differ ONLY where that difference is
    real.
    """
    acct = ledger.create_account(conn, "Dividend Brokerage", "investment",
                                 opening_balance=50_000_00, opening_date="2020-01-01")

    def buy(sym):
        investments.record_investment(
            conn, acct, "2021-01-04", "Buy", symbol=sym, quantity=Decimal("100"),
            price=Decimal("100.00"), amount=10_000_00)

    buy("ZZCASHD")
    buy("ZZREIND")
    for year in range(2021, 2026):
        investments.record_investment(conn, acct, f"{year}-12-15", "Div",
                                      symbol="ZZCASHD", amount=300_00)
        investments.record_investment(conn, acct, f"{year}-12-15", "ReinvDiv",
                                      symbol="ZZREIND", quantity=Decimal("2"),
                                      price=Decimal("150.00"), amount=300_00)
    investments.rebuild_holdings(conn, acct)
    for sym in ("ZZCASHD", "ZZREIND"):
        for i, year in enumerate(range(2021, 2027)):
            px = Decimal("100.00") + Decimal(i) * Decimal("10.00")
            investments.record_price(conn, sym, f"{year}-01-02", px)
            investments.record_price(conn, sym, f"{year}-06-30", px + Decimal("5.00"))
        investments.record_price(conn, sym, AS_OF, Decimal("150.00"))
    return {"account": acct, "cash_payer": "ZZCASHD", "reinvestor": "ZZREIND"}


def test_the_securities_ring_totals_the_same_money_as_the_accounts_ring(
        conn, two_dividend_styles):
    """The cash wedge is what makes the two agree. Before it the securities ring
    summed to the priced holdings alone while the center block showed the
    account total in both modes, and the difference -- the cash, where every
    un-reinvested dividend lands -- was on screen nowhere."""
    ids = [two_dividend_styles["account"]]
    accounts = dash.ring_slices(conn, dash.MODE_ACCOUNTS, AS_OF, account_ids=ids)
    securities = dash.ring_slices(conn, dash.MODE_SECURITIES, AS_OF, account_ids=ids)
    assert sum(c for _k, _l, c in securities) == sum(c for _k, _l, c in accounts)

    cash = [s for s in securities if s[0] == dash.CASH_KEY]
    assert len(cash) == 1, "exactly one cash wedge"
    assert cash[0][1] == dash.CASH_LABEL
    assert cash[0] == securities[-1], "cash reads as the remainder, so it goes last"


def test_an_account_swept_to_zero_draws_no_cash_wedge(conn, seeded):
    """A wedge for nothing is a lie about the allocation."""
    ids = [seeded["ira"]]
    cash = dash.center_line(conn, AS_OF, account_ids=ids, symbol=dash.CASH_KEY).total
    slices = dash.ring_slices(conn, dash.MODE_SECURITIES, AS_OF, account_ids=ids)
    has_wedge = any(k == dash.CASH_KEY for k, _l, _c in slices)
    assert has_wedge == (cash > 0)


def test_clicking_the_cash_wedge_states_a_value_and_nothing_else(
        conn, two_dividend_styles):
    """Cash has a value. It has no gain, no dividends and no rate of return, and
    inventing columns of zeros for it would say otherwise."""
    ids = [two_dividend_styles["account"]]
    line = dash.center_line(conn, AS_OF, account_ids=ids, symbol=dash.CASH_KEY,
                            subject=dash.CASH_LABEL)
    assert line.value_only is True
    assert line.headings() == ["Total"]
    assert line.footnote() == ""


def test_a_total_includes_both_kinds_of_dividend(conn, two_dividend_styles):
    """Reported: "when displaying the total for either breakout, both reinvested
    dividends and cash dividends should be included in the dividend number"."""
    ids = [two_dividend_styles["account"]]
    line = dash.center_line(conn, AS_OF, account_ids=ids)
    # One of each was paid in the trailing year, $300 apiece.
    assert line.dividends == 600_00
    assert dict(zip(line.headings(), line.values()))["1-yr dividends"] == "$600.00"
    assert line.uninvested_dividends == 0, "no asterisk on a whole-portfolio total"
    assert line.footnote() == ""


def test_a_cash_payers_value_is_asterisked_and_a_reinvestors_is_not(
        conn, two_dividend_styles):
    """Reported: "the gain for a security that paid dividends in cash would not
    include those dividends in the total ... so we need an asterisk on the value
    that says 'dividends not reinvested' when that is the case"."""
    ids = [two_dividend_styles["account"]]
    payer = dash.center_line(conn, AS_OF, account_ids=ids,
                             symbol=two_dividend_styles["cash_payer"],
                             subject=two_dividend_styles["cash_payer"])
    reinvestor = dash.center_line(conn, AS_OF, account_ids=ids,
                                  symbol=two_dividend_styles["reinvestor"],
                                  subject=two_dividend_styles["reinvestor"])

    assert payer.uninvested_dividends == 1_500_00      # five years of $300
    assert payer.values()[0].endswith("*")
    assert payer.footnote() == "* " + dash.UNINVESTED_NOTE

    assert reinvestor.uninvested_dividends == 0
    assert not reinvestor.values()[0].endswith("*")
    assert reinvestor.footnote() == ""

    # The asterisk marks a REAL difference: the reinvestor's dividends bought
    # shares and are in its value; the payer's are in the account's cash.
    assert reinvestor.total == 16_500_00               # 110 shares
    assert payer.total == 15_000_00                    # 100 shares


def test_the_return_percentages_include_dividends_paid_in_cash(
        conn, two_dividend_styles):
    """Reported: "but the return percentages should include the dividends paid
    over each period."

    They do, and by the same arithmetic for both routes: security_performance
    treats a cash dividend as money BACK, so it lands in the gain exactly as a
    reinvested one lands in the ending value. The two positions earned the same
    $6,500; only the timing differs, which is what a money-weighted rate is
    supposed to notice.
    """
    acct = two_dividend_styles["account"]
    payer = portfolio.security_performance(
        conn, acct, two_dividend_styles["cash_payer"], "2021-01-01", AS_OF)
    reinvestor = portfolio.security_performance(
        conn, acct, two_dividend_styles["reinvestor"], "2021-01-01", AS_OF)

    assert payer.gain == reinvestor.gain == 6_500_00
    assert payer.money_out == 1_500_00, "the cash dividends came back out"
    assert reinvestor.money_out == 0, "the reinvested ones never left"
    assert payer.income == reinvestor.income == 1_500_00

    # Every horizon has a rate, and the cash payer's is the higher of the two --
    # money returned sooner earns more per dollar-year, not less.
    ids = [acct]
    for symbol in (two_dividend_styles["cash_payer"],
                   two_dividend_styles["reinvestor"]):
        line = dash.center_line(conn, AS_OF, account_ids=ids, symbol=symbol,
                                subject=symbol)
        assert set(line.annualized) == set(dash.ANNUALIZED_YEARS)
    for years in dash.ANNUALIZED_YEARS:
        a = dash.center_line(conn, AS_OF, account_ids=ids,
                             symbol=two_dividend_styles["cash_payer"]).annualized[years]
        b = dash.center_line(conn, AS_OF, account_ids=ids,
                             symbol=two_dividend_styles["reinvestor"]).annualized[years]
        assert a > b, f"{years}y: cash payer {a} should beat reinvestor {b}"


def test_a_cash_dividend_is_counted_as_cash_and_a_reinvested_one_is_not(
        conn, two_dividend_styles):
    """portfolio.cash_dividends is what the asterisk is decided on, so it has to
    split them the way security_performance's flows do."""
    ids = [two_dividend_styles["account"]]
    assert portfolio.cash_dividends(
        conn, ids, two_dividend_styles["cash_payer"], AS_OF) == 1_500_00
    assert portfolio.cash_dividends(
        conn, ids, two_dividend_styles["reinvestor"], AS_OF) == 0
    # Un-symboled: the whole scope's cash income.
    assert portfolio.cash_dividends(conn, ids, None, AS_OF) == 1_500_00
    # Windowed, for a caller that wants one period.
    assert portfolio.cash_dividends(
        conn, ids, two_dividend_styles["cash_payer"], AS_OF,
        start="2025-01-01") == 300_00


def test_the_asterisk_carries_its_explanation_on_the_widget(
        conn, two_dividend_styles, qapp):
    """An asterisk with nothing to explain it is worse than none."""
    page = dash.InvestmentDashboardPage(conn, as_of=AS_OF)
    try:
        page.set_mode(dash.MODE_SECURITIES)
        page.select_slice(two_dividend_styles["cash_payer"])
        qapp.processEvents()
        assert page.center.value_texts()[0].endswith("*")
        assert page.center._values[0].toolTip() == dash.UNINVESTED_NOTE
        page.select_slice(two_dividend_styles["reinvestor"])
        qapp.processEvents()
        assert not page.center.value_texts()[0].endswith("*")
        assert page.center._values[0].toolTip() == ""
    finally:
        page.deleteLater()


def test_the_cash_wedges_caption_is_not_the_sentinel(conn, two_dividend_styles,
                                                     qapp):
    """The key is "__cash__" so it cannot collide with a ticker; the user must
    never see that string."""
    page = dash.InvestmentDashboardPage(conn, as_of=AS_OF)
    try:
        page.set_mode(dash.MODE_SECURITIES)
        page.select_slice(dash.CASH_KEY)
        qapp.processEvents()
        assert page.filter_subject() == dash.CASH_LABEL
        assert dash.CASH_KEY not in page.history_header.title()
        assert dash.CASH_KEY not in page.center_text()
    finally:
        page.deleteLater()


# --- the inflow arrows ------------------------------------------------------
def test_four_inflows_in_the_year_earn_an_arrow_with_the_yearly_total(conn, seeded):
    arrows = dash.inflow_arrows(conn, AS_OF)
    assert [a.account_id for a in arrows] == [seeded["brokerage"]]
    arrow = arrows[0]
    assert arrow.name == "Test Brokerage"
    assert arrow.count == 4
    # The plain SUM of the year's deposits, not an annualized extrapolation.
    assert arrow.total == 2_000_00


def test_three_inflows_in_the_year_earn_no_arrow(conn, seeded):
    assert seeded["ira"] not in [a.account_id for a in dash.inflow_arrows(conn, AS_OF)]


def test_an_inflow_outside_the_window_does_not_count(conn, seeded):
    """The IRA's fourth deposit is 18 months old; widening the window reaches
    it, which proves the threshold is the window and not the count alone."""
    assert len(dash.inflow_arrows(conn, AS_OF)) == 1
    wide = dash.inflow_arrows(conn, AS_OF, window_days=700)
    assert seeded["ira"] in [a.account_id for a in wide]
    assert next(a for a in wide if a.account_id == seeded["ira"]).total == 400_00


def test_outflows_are_not_counted_as_inflows(conn, seeded):
    ledger.create_transfer(conn, seeded["brokerage"], seeded["checking"],
                           "2026-05-01", 300_00, memo="Withdrawal")
    arrow = dash.inflow_arrows(conn, AS_OF)[0]
    assert arrow.count == 4
    assert arrow.total == 2_000_00


def test_the_page_draws_an_arrow_only_for_the_regular_account(page, seeded):
    assert page.arrow_accounts() == [seeded["brokerage"]]
    widget = page.arrow_widgets[0]
    assert widget.amount_text() == "$2,000.00"
    assert widget.account_name() == "Test Brokerage"
    assert "4 deposits" in widget.toolTip()


def test_an_arrow_widget_paints_without_a_display(qapp, conn, seeded):
    from PyQt5.QtGui import QPixmap
    widget = dash.InflowArrowWidget(dash.inflow_arrows(conn, AS_OF)[0])
    widget.resize(200, 50)
    widget.render(QPixmap(widget.size()))      # no exception == it draws
    widget.deleteLater()


# --- staleness --------------------------------------------------------------
def test_mark_stale_defers_the_recompute_until_the_page_is_shown(page, conn, seeded):
    page.mark_stale()
    assert page._stale is True                 # not visible: nothing recomputed
    ledger.create_transfer(conn, seeded["checking"], seeded["ira"],
                           "2026-05-20", 100_00, memo="Contribution")
    assert page.refresh_if_stale() is True
    assert page.refresh_if_stale() is False
    assert seeded["ira"] in page.arrow_accounts()


def test_refresh_keeps_a_selection_that_still_exists(page, seeded):
    page.select_slice(seeded["ira"])
    page.refresh()
    assert page.ring.selected() == str(seeded["ira"])


def test_the_window_is_the_trailing_year_from_today_by_default(qapp, conn, seeded):
    p = dash.InvestmentDashboardPage(conn)
    assert p.as_of == dt.date.today().isoformat()
    p.deleteLater()


# --- the value chart above the center line (design 2.3) ---------------------
def _all_ids(conn):
    return dash._account_ids(conn)


def test_the_value_chart_offers_exactly_the_listed_periods(page):
    assert page.history_chart.period_values() == [1, 2, 3, 5, 8, 10, None]
    assert page.history_chart.period_labels() == [
        "1 year", "2 years", "3 years", "5 years", "8 years", "10 years", "Max"]
    assert page.history_chart.period.objectName() == "historyPeriod"
    assert page.history_chart.years() == dash.DEFAULT_HISTORY_YEARS


def test_the_value_series_ends_at_as_of_with_the_subjects_value(conn, seeded):
    series = dash.value_series(conn, 1, as_of=AS_OF)
    assert len(series) == dash.HISTORY_POINTS
    assert series[0][0] == dash.years_before(AS_OF, 1)
    everything = sum(investments.account_valuation(conn, a, AS_OF).total
                     for a in _all_ids(conn))
    assert series[-1] == (AS_OF, everything)


def test_max_looks_back_only_as_far_as_the_money_goes(conn, seeded):
    """Nothing existed before 2024, so Max must not draw a flat decade of zero."""
    ids = _all_ids(conn)
    span = dash.history_span(conn, ids, None, AS_OF)
    assert 1 <= span <= dash.MAX_HISTORY_YEARS
    assert dash._value_at(conn, ids, None, dash.years_before(AS_OF, span)) <= 0
    assert dash._value_at(conn, ids, None,
                          dash.years_before(AS_OF, span // 2)) > 0
    series = dash.value_series(conn, None, as_of=AS_OF)
    # One zero is kept as the origin; everything after it is real money.
    assert all(cents > 0 for _iso, cents in series[1:])
    assert series[-1][0] == AS_OF


def test_changing_the_period_redraws_the_value_chart(page):
    """Both directions, and stated relative to each other rather than to the
    default: the default moved from 1 year to 10 (reported), and a test that
    assumed "3 is longer than the default" silently became an assertion about
    the fixture's data span instead of about the period control."""
    assert page.history_chart.years() == dash.DEFAULT_HISTORY_YEARS
    page.history_chart.set_years(1)
    short = page.history_chart.points()
    assert page.history_chart.years() == 1
    page.history_chart.set_years(3)
    longer = page.history_chart.points()
    assert page.history_chart.years() == 3
    assert longer != short
    assert longer[0][0] < short[0][0]        # 3 years reaches further back
    assert longer[-1] == short[-1] == (AS_OF, short[-1][1])


def test_both_plots_open_on_ten_years(page):
    """Reported: "I'd like the default time ranges for both plots to be 10
    years". The two used to disagree -- 1 year above, 20 below."""
    assert dash.DEFAULT_HISTORY_YEARS == 10
    assert dash.DEFAULT_PROJECTION_YEARS == 10
    assert 10 in [years for years, _ in dash.HISTORY_PERIODS]
    assert 10 in dash.PROJECTION_HORIZONS
    assert page.history_chart.years() == 10
    assert page.projection_chart.years() == 10


def test_the_plot_area_grew_left_into_the_margin_not_the_ring(page):
    """Reported: "both of the plots have some extra space to their left they
    could grow into ... increase the width by 10% and shift the center to the
    left by half that amount".

    The HOLE could not do that -- its corners are capped at the ring's outer
    edge and were already against the cap. The room was inside the canvas,
    where the left margin reserved 23.6% of the width for tick labels that need
    a third of it.
    """
    was_left, was_right = 0.26 / dash.HOLE_WIDTH_SCALE, 0.99
    now_left, now_right = dash.PLOT_AXES_LEFT, dash.PLOT_AXES_RIGHT
    assert now_right == was_right, "growth is leftward; the right edge holds"
    assert (now_right - now_left) == pytest.approx((was_right - was_left) * 1.10,
                                                   abs=0.002)
    moved = ((now_left + now_right) / 2) - ((was_left + was_right) / 2)
    assert moved == pytest.approx(-(was_right - was_left) * 0.05, abs=0.002)


def test_the_left_margin_still_clears_an_eight_figure_portfolio(qapp):
    """The margin is what stops "$12,500,000" running off the left edge, so the
    10% the plot took back has to leave that label its room."""
    from matplotlib.figure import Figure
    canvas_px = 689
    fig = Figure(figsize=(canvas_px / 100, 2.5), dpi=100)
    ax = fig.add_subplot(111)
    ax.tick_params(labelsize=dash.TICK_FONT_SIZE)
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["$12,500,000", "$12,500,000"])
    fig.canvas.draw()
    widest = max(lab.get_window_extent().width for lab in ax.get_yticklabels())
    assert widest < dash.PLOT_AXES_LEFT * canvas_px


def test_the_title_keeps_a_gap_from_the_dropdown_and_elides_rather_than_clips(
        page, qapp, conn, seeded):
    """Reported: the title "is sometimes truncated by running into the
    time-range dropdown". A QLabel clips, it does not elide, and a cut-off word
    beside a control reads as a collision; an ellipsis reads as "there is more".
    """
    from PyQt5.QtGui import QFontMetrics
    from mammon import ledger
    long_id = ledger.create_account(
        conn, "Vanguard Total Stock Market Index Admiral", "investment",
        opening_balance=5_000_00, opening_date=OPEN_DATE)
    page.resize(1200, 800)
    page.show()
    page.refresh()
    qapp.processEvents()

    for key in (None, seeded["ira"], long_id):
        if key is None:
            page.clear_filter()
        else:
            page.select_slice(key)
        for _ in range(3):
            qapp.processEvents()
        header = page.history_header
        label = header.title_label
        gap = (header.selector.geometry().x()
               - (label.geometry().x() + label.geometry().width()))
        assert gap >= dash.TITLE_GAP, "the title ran into the dropdown"
        needed = QFontMetrics(label.font()).horizontalAdvance(label.text())
        assert needed <= label.width() + 1, "displayed text must not be clipped"
    # The one that cannot fit is shortened with an ellipsis, not cut off.
    assert page.history_header.displayed_title() != page.history_header.title()
    assert page.history_header.displayed_title().rstrip().endswith("\u2026")
    page.hide()


def test_the_header_asks_for_the_full_title_immediately(page, qapp):
    """The slot reads sizeHint() synchronously inside the same refresh, so a new
    title has to reach the hint before set_title returns -- the deferred-hint
    trap that left "Test IRA Performance" wearing the shorter title's width with
    600px of hole free beside it."""
    page.resize(1200, 800)
    page.show()
    qapp.processEvents()
    header = page.history_header
    header.set_title("A Very Long Portfolio Name Indeed Performance")
    from PyQt5.QtGui import QFontMetrics
    wanted = QFontMetrics(header.title_label.font()).horizontalAdvance(header.title())
    assert header.sizeHint().width() >= wanted + dash.TITLE_GAP
    page.hide()


def test_eliding_does_not_feed_back_into_the_headers_width(page, qapp):
    """sizeHint must describe the FULL title. If it described the elided text,
    a narrowed header would elide, report a smaller hint, be granted less, and
    walk itself down to an ellipsis at a width where the title would have fit."""
    page.resize(1200, 800)
    page.show()
    qapp.processEvents()
    header = page.history_header
    header.set_title("Another Rather Long Account Name Performance")
    qapp.processEvents()
    first = header.sizeHint().width()
    for _ in range(4):
        header.resize(260, header.height())   # force repeated elision
        qapp.processEvents()
    assert header.sizeHint().width() == first
    assert header.title() == "Another Rather Long Account Name Performance"
    page.hide()


def test_the_corner_tiles_are_buttons_not_panels(page, qapp):
    """Reported: "shrink the size of the 4 corner tiles so that they look more
    like buttons than something that is actually trying to convey information.
    Maybe add a border around them and round the corners a bit more"."""
    page.resize(1200, 800)
    page.show()
    qapp.processEvents()
    area = page.ring_area
    rects = area.corner_rects()
    assert dash.CORNER_BOX_SCALE < 1.0
    assert dash.CORNER_RADIUS >= 10, "rounder than the platform button"
    assert dash.CORNER_BORDER >= 1

    # Inset from the page's corner rather than bolted to it.
    x, y, w, h = rects["cornerTopLeft"]
    assert (x, y) == (dash.CORNER_INSET, dash.CORNER_INSET)
    rx, ry, rw, rh = rects["cornerBottomRight"]
    assert rx + rw == area.width() - dash.CORNER_INSET
    assert ry + rh == area.height() - dash.CORNER_INSET

    # All four the same size, and each smaller than the offcut it sits in.
    sizes = {(r[2], r[3]) for r in rects.values()}
    assert len(sizes) == 1
    import math
    radius = area.outer_radius()
    side = radius * (1.0 - 1.0 / math.sqrt(2.0))
    offcut_w = int(side + (area.width() - 2.0 * radius) / 2.0)
    assert w < offcut_w

    # The caption still fits at every tile's size -- shrinking the box must not
    # clip a title, only step it down a size.
    for name, btn in area.corners.items():
        inner_w = btn.width() - 2 * dash.CORNER_PAD
        inner_h = btn.height() - 2 * dash.CORNER_PAD
        cap = max(1, int(inner_h * dash.CORNER_TITLE_MAX_FRACTION))
        _font, height = btn._title_font(inner_w, cap)
        assert height <= cap, f"{name}'s caption does not fit its tile"
    page.hide()


def test_the_account_scope_is_remembered_for_this_ledger(conn, seeded, qapp,
                                                         tmp_path):
    """Reported: "can we remember the account customization so that I don't
    have to keep excluding the same accounts every time?" """
    from PyQt5.QtCore import QSettings
    from mammon.ui import prefs
    store = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    path = tmp_path / "ledger.db"

    assert prefs.dashboard_account_scope(path, store) is None      # never set
    prefs.set_dashboard_account_scope(path, [seeded["ira"]], store)
    assert prefs.dashboard_account_scope(path, store) == [seeded["ira"]]

    # A DIFFERENT ledger must not inherit it: the ids mean nothing there.
    assert prefs.dashboard_account_scope(tmp_path / "other.db", store) is None

    # Forgetting restores "every investment account", which is not the same as
    # an empty list (the user having unticked everything).
    prefs.set_dashboard_account_scope(path, [], store)
    assert prefs.dashboard_account_scope(path, store) == []
    prefs.set_dashboard_account_scope(path, None, store)
    assert prefs.dashboard_account_scope(path, store) is None


def test_the_page_opens_on_the_remembered_scope(conn, seeded, qapp, monkeypatch,
                                                tmp_path):
    """The saved scope has to reach the PAGE at construction, not merely tick
    the gear's checkboxes -- otherwise the first render is still everything and
    the user re-applies the same exclusion they saved last time."""
    from PyQt5.QtCore import QSettings
    from mammon.ui import prefs
    store = QSettings(str(tmp_path / "s2.ini"), QSettings.IniFormat)
    monkeypatch.setattr(prefs, "_settings", lambda settings=None: store)

    page = dash.InvestmentDashboardPage(conn, as_of=AS_OF)
    try:
        assert page.account_scope() is None          # nothing remembered yet
        prefs.set_dashboard_account_scope(page._db_path(), [seeded["ira"]])
    finally:
        page.deleteLater()

    reopened = dash.InvestmentDashboardPage(conn, as_of=AS_OF)
    try:
        assert reopened.account_scope() == [seeded["ira"]]
        assert reopened.center.line().total == investments.account_valuation(
            conn, seeded["ira"], AS_OF).total
    finally:
        reopened.deleteLater()


def test_a_remembered_account_that_is_gone_is_dropped(conn, seeded, qapp,
                                                      monkeypatch, tmp_path):
    """An id saved last month may not be an investment account today. The page
    filters the remembered list the same way set_account_scope does, rather
    than trusting it."""
    from PyQt5.QtCore import QSettings
    from mammon.ui import prefs
    store = QSettings(str(tmp_path / "s3.ini"), QSettings.IniFormat)
    monkeypatch.setattr(prefs, "_settings", lambda settings=None: store)

    page = dash.InvestmentDashboardPage(conn, as_of=AS_OF)
    db_path = page._db_path()
    page.deleteLater()
    prefs.set_dashboard_account_scope(db_path, [seeded["ira"], 999_999])

    reopened = dash.InvestmentDashboardPage(conn, as_of=AS_OF)
    try:
        assert reopened.account_scope() == [seeded["ira"]]
    finally:
        reopened.deleteLater()


def test_an_unknown_period_is_refused(page):
    with pytest.raises(ValueError):
        page.history_chart.set_years(7)


def test_the_value_chart_follows_the_ring_filter_and_its_color(page, conn, seeded):
    page.select_slice(seeded["ira"])
    points = page.history_chart.points()
    assert points[-1][1] == investments.account_valuation(conn, seeded["ira"],
                                                          AS_OF).total
    assert page.history_chart.canvas.color() == page.ring.color_for(
        str(seeded["ira"]))


# --- the projection fan below the center line (design 2.3, 5.3) -------------
def test_the_projection_offers_exactly_the_listed_horizons(page):
    assert page.projection_chart.period_values() == [5, 10, 20, 30, 40, 50]
    assert page.projection_chart.period.objectName() == "projectionHorizon"
    assert page.projection_chart.years() == dash.DEFAULT_PROJECTION_YEARS


def test_the_fan_is_the_forecast_engines_and_not_a_second_implementation():
    mix = forecast.mix_for_risk(3.0)
    mu, sigma = forecast.portfolio_moments(mix)
    assert dash.projection_fan(100_000_00, 6_000_00, 3.0, 10) == forecast.fan(
        100_000_00, 6_000_00, mu, sigma, 10)


def test_the_lower_chart_draws_a_band_not_a_single_line(page, conn):
    fan = page.projection_chart.canvas.baseline()
    assert len(fan) == dash.DEFAULT_PROJECTION_YEARS + 1
    start = sum(investments.account_valuation(conn, a, AS_OF).total
                for a in _all_ids(conn))
    assert fan[0].p50 == start
    last = fan[-1]
    assert last.p05 < last.p25 < last.p50 < last.p75 < last.p95


def test_the_projection_starts_from_the_measured_inflows_and_mix(page, conn):
    start = sum(investments.account_valuation(conn, a, AS_OF).total
                for a in _all_ids(conn))
    expected = dash.projection_fan(start, 2_000_00,
                                   dash.current_risk(conn, _all_ids(conn), AS_OF),
                                   dash.DEFAULT_PROJECTION_YEARS)
    assert page.projection_chart.canvas.baseline() == expected


def test_a_wedge_without_an_inflow_arrow_projects_no_contributions(
        page, conn, seeded):
    """Reported: clicking a ring segment drew a fan that grew as if the WHOLE
    portfolio's inflow landed in that one account. The inflow is measured per
    account (the arrows say which accounts have one), so the IRA -- which earns
    no arrow -- must project zero contributions, not $2,000 of the brokerage's."""
    page.select_slice(seeded["ira"])
    assert page._measured_contribution() == 0
    start = investments.account_valuation(conn, seeded["ira"], AS_OF).total
    expected = dash.projection_fan(
        start, 0, dash.current_risk(conn, [seeded["ira"]], AS_OF),
        dash.DEFAULT_PROJECTION_YEARS)
    assert page.projection_chart.canvas.baseline() == expected


def test_a_wedge_with_an_arrow_projects_only_its_own_inflow(page, seeded):
    page.select_slice(seeded["brokerage"])
    assert page._measured_contribution() == 2_000_00     # its own, not the sum
    page.select_slice(seeded["brokerage"])               # clears the filter
    assert page._measured_contribution() == 2_000_00     # the portfolio's sum


def test_a_security_selection_projects_no_contributions(page):
    """An inflow arrives in an ACCOUNT, not in a holding. Charging one
    security's fan with the account's whole stream would inflate it with money
    that mostly buys something else, so a security scope contributes zero."""
    page.set_mode(dash.MODE_SECURITIES)
    page.select_slice("ZZAA")
    assert page._measured_contribution() == 0


def test_changing_the_horizon_redraws_the_fan(page):
    page.projection_chart.set_years(50)
    fan = page.projection_chart.canvas.baseline()
    assert page.projection_chart.years() == 50
    assert len(fan) == 51
    assert fan[-1].year == 50


# --- the thermometer (design 2.5, 4.5) --------------------------------------
def test_the_thermometer_runs_the_whole_risk_ladder(page):
    bar = page.thermometer.bar
    assert bar.objectName() == "riskSlider"
    assert bar.minimum() == 0
    assert bar.maximum() == forecast.MAX_RISK_LEVEL * dash.RISK_SLIDER_STEPS
    page.thermometer.set_risk(0)
    assert page.thermometer.mix()["cash"] == pytest.approx(1.0)
    page.thermometer.set_risk(forecast.MAX_RISK_LEVEL)
    mix = page.thermometer.mix()
    assert mix["domestic_stock"] + mix["intl_stock"] == pytest.approx(1.0)


def test_the_thermometer_caption_names_the_mix(page):
    page.thermometer.set_risk(6)
    caption = page.thermometer.caption_text()
    assert caption == dash.mix_caption(forecast.mix_for_risk(6))
    assert "% stocks" in caption
    assert "% cash" in caption


def test_the_mix_caption_is_whole_percents_that_name_the_classes():
    assert dash.mix_caption({"domestic_stock": 0.36, "intl_stock": 0.24,
                             "bond": 0.24, "cash": 0.16}) == (
        "60% stocks / 24% bonds / 16% cash")


def test_the_needle_starts_at_the_mix_the_portfolio_actually_holds(page, conn):
    measured = dash.current_risk(conn, _all_ids(conn), AS_OF)
    assert page.thermometer.risk() == pytest.approx(round(measured, 1), abs=0.05)
    assert 0.0 <= measured <= forecast.MAX_RISK_LEVEL


def test_an_empty_scope_is_all_cash_rather_than_a_crash(conn):
    assert dash.current_mix(conn, [], AS_OF)["cash"] == pytest.approx(1.0)
    assert dash.current_risk(conn, [], AS_OF) == 0.0


# --- the oval handle lives ON the bar ---------------------------------------
@pytest.fixture
def therm(qapp):
    """A thermometer on its own, shown so the bar has REAL geometry: under the
    offscreen platform only ``show()`` sizes a nested child, and every
    assertion below is about pixels."""
    t = dash.Thermometer()
    t.set_editable(True)                    # What If on: the handle is grabbable
    t.resize(60, 260)
    t.show()
    qapp.processEvents()
    yield t
    t.hide()


def _mouse(bar, kind, y, *, buttons=None):
    """Synthesize one mouse event straight at the bar, in ITS coordinates."""
    from PyQt5.QtCore import QPointF, Qt
    from PyQt5.QtGui import QMouseEvent
    from PyQt5.QtWidgets import QApplication
    button = Qt.LeftButton if kind != "move" else Qt.NoButton
    held = Qt.LeftButton if buttons is None else buttons
    types = {"press": QMouseEvent.MouseButtonPress,
             "move": QMouseEvent.MouseMove,
             "release": QMouseEvent.MouseButtonRelease}
    pos = QPointF(bar.width() / 2.0, float(y))
    QApplication.sendEvent(bar, QMouseEvent(types[kind], pos, button, held,
                                            Qt.NoModifier))


def _y_for(bar, fraction):
    """The y a handle centered at ``fraction`` of the ladder sits at."""
    return bar.HANDLE_HEIGHT / 2.0 + (1.0 - fraction) * (bar.height()
                                                         - bar.HANDLE_HEIGHT)


def test_the_thermometer_has_no_separate_slider_widget(therm):
    """The square QSlider beside the bar read as a cross (reported). The bar
    IS the control now, so a child QSlider anywhere is the regression."""
    from PyQt5.QtWidgets import QAbstractSlider, QSlider
    assert therm.findChildren(QSlider) == []
    assert isinstance(therm.bar, QAbstractSlider)
    assert therm.bar.parent() is therm
    assert therm.bar.width() >= 20      # wide enough to grab the oval


def test_a_press_on_the_bar_moves_the_handle_there_and_announces_it(therm):
    bar = therm.bar
    seen = []
    therm.riskChanged.connect(seen.append)
    _mouse(bar, "press", _y_for(bar, 0.5))
    assert therm.risk() == pytest.approx(forecast.MAX_RISK_LEVEL / 2.0,
                                         abs=0.05)
    assert seen == [pytest.approx(therm.risk())]
    # the oval is painted back at the point that was pressed
    assert bar.handle_center_y() == pytest.approx(_y_for(bar, 0.5), abs=1.0)


def test_dragging_the_handle_follows_the_mouse(therm):
    bar = therm.bar
    seen = []
    therm.riskChanged.connect(seen.append)
    _mouse(bar, "press", _y_for(bar, 0.2))
    assert therm.risk() == pytest.approx(0.2 * forecast.MAX_RISK_LEVEL,
                                         abs=0.1)
    _mouse(bar, "move", _y_for(bar, 0.8))
    assert therm.risk() == pytest.approx(0.8 * forecast.MAX_RISK_LEVEL,
                                         abs=0.1)
    _mouse(bar, "release", _y_for(bar, 0.8))
    assert bar.isSliderDown() is False
    assert len(seen) == 2
    # the drag is over: moving on does nothing
    _mouse(bar, "move", _y_for(bar, 0.1))
    assert therm.risk() == pytest.approx(0.8 * forecast.MAX_RISK_LEVEL,
                                         abs=0.1)


def test_the_level_clamps_at_both_ends_of_the_bar(therm):
    bar = therm.bar
    _mouse(bar, "press", -40)               # above the top of the column
    assert therm.risk() == pytest.approx(forecast.MAX_RISK_LEVEL)
    _mouse(bar, "move", bar.height() + 40)  # dragged off the bottom
    assert therm.risk() == 0.0
    _mouse(bar, "release", bar.height() + 40)
    assert bar.value_at(0) == bar.maximum()
    assert bar.value_at(bar.height()) == bar.minimum()


def test_the_arrow_keys_still_walk_the_ladder(therm):
    """Inherited from QAbstractSlider, which is the point of deriving from it:
    a hand-rolled handle would have dropped keyboard access silently."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtWidgets import QApplication
    therm.set_risk(5)
    for key, expected in ((Qt.Key_Up, 5.1), (Qt.Key_Down, 5.0),
                          (Qt.Key_PageUp, 6.0), (Qt.Key_Home, 0.0),
                          (Qt.Key_End, float(forecast.MAX_RISK_LEVEL))):
        QApplication.sendEvent(therm.bar, QKeyEvent(QKeyEvent.KeyPress, key,
                                                    Qt.NoModifier))
        assert therm.risk() == pytest.approx(expected)


def test_a_read_only_thermometer_ignores_the_mouse(qapp):
    """What If off: the bar still shows where the portfolio sits, but a stray
    click must not rewrite the measured mix."""
    t = dash.Thermometer(risk=3.0)
    t.resize(60, 260)
    t.show()
    qapp.processEvents()
    try:
        assert t.is_editable() is False
        _mouse(t.bar, "press", _y_for(t.bar, 1.0))
        assert t.risk() == pytest.approx(3.0)
    finally:
        t.hide()


# --- What If (design 2.5) ---------------------------------------------------
def test_what_if_off_leaves_the_inflows_and_the_slider_read_only(page):
    assert page.what_if_active() is False
    assert page.thermometer.is_editable() is False
    assert page.thermometer.bar.isEnabled() is False
    assert all(not w.is_editable() for w in page.arrow_widgets)
    assert page.projection_chart.has_what_if() is False


def test_what_if_on_makes_the_arrows_editable_and_the_slider_slideable(page):
    page.set_what_if(True)
    assert page.what_if_active() is True
    assert page.thermometer.is_editable() is True
    assert page.thermometer.bar.isEnabled() is True
    assert all(w.is_editable() for w in page.arrow_widgets)


def test_moving_the_slider_overlays_a_second_fan_without_losing_the_first(page):
    baseline = page.projection_chart.canvas.baseline()
    page.set_what_if(True)
    page.thermometer.set_risk(0, emit=True)
    calm = page.projection_chart.canvas.what_if()
    page.thermometer.set_risk(forecast.MAX_RISK_LEVEL, emit=True)
    bold = page.projection_chart.canvas.what_if()
    assert page.projection_chart.has_what_if() is True
    # The baseline stays put underneath: the difference is the visible thing.
    assert page.projection_chart.canvas.baseline() == baseline
    assert bold[-1].p95 > calm[-1].p95             # more risk, wider fan


def test_editing_an_arrow_moves_the_projection_and_never_the_database(page, conn):
    page.set_what_if(True)
    widget = page.arrow_widgets[0]
    widget.commit_text("$6,000.00")
    assert widget.amount() == 6_000_00
    what_if = page.projection_chart.canvas.what_if()
    assert what_if[-1].p50 > page.projection_chart.canvas.baseline()[-1].p50
    # Nothing was written: the ledger still says $2,000 a year.
    assert dash.inflow_arrows(conn, AS_OF)[0].total == 2_000_00


def test_gibberish_in_an_arrow_snaps_back_instead_of_raising(page):
    page.set_what_if(True)
    widget = page.arrow_widgets[0]
    widget.commit_text("soon")
    assert widget.amount() == 2_000_00
    assert widget.editor.text() == "$2,000.00"


def test_parse_money_takes_what_the_arrow_writes_back():
    assert dash.parse_money("$2,000.00") == 2_000_00
    assert dash.parse_money(" 1234.567 ") == 1_234_57       # ROUND_HALF_UP
    assert dash.parse_money("(50)") == -50_00
    with pytest.raises(ValueError):
        dash.parse_money("soon")


def test_reset_puts_the_measured_inflows_and_mix_back(page, conn):
    page.set_what_if(True)
    page.arrow_widgets[0].commit_text("$9,000.00")
    page.thermometer.set_risk(forecast.MAX_RISK_LEVEL, emit=True)
    page.reset_what_if()
    assert page.arrow_widgets[0].amount() == 2_000_00
    assert page.thermometer.risk() == pytest.approx(
        round(dash.current_risk(conn, _all_ids(conn), AS_OF), 1), abs=0.05)


def test_turning_what_if_off_drops_the_overlay(page):
    page.set_what_if(True)
    page.arrow_widgets[0].commit_text("$9,000.00")
    page.set_what_if(False)
    assert page.projection_chart.has_what_if() is False
    assert page.arrow_widgets[0].amount() == 2_000_00
    assert page.thermometer.bar.isEnabled() is False


def test_what_if_stays_available_while_a_wedge_is_selected(page, seeded):
    """Reported: "I want to be able to do what if on an individual account.
    Otherwise being able to change the inflow is meaningless as a tiny inflow in
    a small account can't move the needle vs a large total." The control used to
    gray itself out here, which made that question unaskable."""
    assert page.what_if_available() is True
    page.select_slice(seeded["ira"])
    assert page.filter() == ("account", seeded["ira"])
    assert page.what_if_available() is True
    assert page.what_if_bar.button.isEnabled() is True
    assert "portfolio-wide" not in page.what_if_bar.button.toolTip()
    # And it can be turned on from here: Reset unlocks as usual.
    page.set_what_if(True)
    assert page.what_if_active() is True
    assert page.what_if_bar.reset.isEnabled() is True


def test_the_what_if_bar_names_the_scope_it_is_acting_on(page, seeded):
    """A one-account fan misread as the portfolio's is worse than the old gray
    button, so the bar states its subject in words."""
    assert page.what_if_bar.scope_subject() == dash.WHAT_IF_ALL_SUBJECT
    page.select_slice(seeded["ira"])
    assert page.what_if_bar.scope_subject() == "Test IRA"
    assert "Test IRA" in page.what_if_bar.scope.toolTip()
    page.set_mode(dash.MODE_SECURITIES)
    page.select_slice("ZZCC")
    assert page.what_if_bar.scope_subject() == "ZZCC"
    page.select_slice("ZZCC")                     # clears the filter
    assert page.what_if_bar.scope_subject() == dash.WHAT_IF_ALL_SUBJECT


def test_selecting_a_wedge_re_scopes_what_if_instead_of_turning_it_off(page, seeded):
    page.set_what_if(True)
    page.select_slice(seeded["ira"])
    assert page.what_if_active() is True
    assert page.projection_chart.has_what_if() is True
    assert page.what_if_bar.scope_subject() == "Test IRA"
    page.select_slice(seeded["ira"])              # clearing re-scopes too
    assert page.what_if_active() is True
    assert page.projection_chart.has_what_if() is True
    assert page.what_if_bar.scope_subject() == dash.WHAT_IF_ALL_SUBJECT


def test_what_if_on_one_account_projects_that_account_not_the_portfolio(
        page, conn, seeded):
    page.set_what_if(True)
    whole = page.projection_chart.canvas.what_if()
    page.select_slice(seeded["ira"])
    scoped = page.projection_chart.canvas.what_if()

    ira_value = investments.account_valuation(conn, seeded["ira"], AS_OF).total
    portfolio_value = sum(investments.account_valuation(conn, a, AS_OF).total
                          for a in _all_ids(conn))
    assert 0 < ira_value < portfolio_value
    assert scoped[0].p50 == ira_value             # starts where the account is
    assert whole[0].p50 == portfolio_value
    assert scoped[-1].p50 != whole[-1].p50        # a different fan entirely
    # Both fans on the SAME subject: never a portfolio baseline underneath an
    # account's What If, which would read as a catastrophe, not a comparison.
    assert page.projection_chart.canvas.baseline()[0].p50 == ira_value


def test_changing_the_inflow_moves_the_small_accounts_own_curve(
        page, conn, seeded):
    """The user's actual question: $100 more a quarter into a $5,000 account."""
    # A fourth deposit inside the window earns the IRA an arrow to edit.
    ledger.create_transfer(conn, seeded["checking"], seeded["ira"], "2026-06-20",
                           100_00, memo="Contribution")
    page.refresh()
    page.select_slice(seeded["ira"])
    page.set_what_if(True)
    widget = next(w for w in page.arrow_widgets
                  if w.account_id() == seeded["ira"])
    assert widget.is_editable() is True
    assert widget.amount() == 400_00
    before = page.projection_chart.canvas.what_if()[-1].p50

    widget.commit_text("$2,000.00")
    after = page.projection_chart.canvas.what_if()[-1].p50
    assert after > before
    # Still the IRA's curve, and the measured baseline it is compared against
    # is the IRA's too.
    assert page.projection_chart.canvas.baseline()[0].p50 == \
        investments.account_valuation(conn, seeded["ira"], AS_OF).total
    # An arrow outside the scope is editable too -- it re-scopes the page to a
    # fan it can move rather than going quietly read-only (see
    # test_an_arrow_outside_the_ring_filter_is_still_editable_and_lands).
    other = next(w for w in page.arrow_widgets
                 if w.account_id() == seeded["brokerage"])
    assert other.is_editable() is True
    # And nothing was written: the ledger still says $400 a year.
    assert {a.account_id: a.total
            for a in dash.inflow_arrows(conn, AS_OF)}[seeded["ira"]] == 400_00


def test_a_refresh_that_drops_the_selected_key_clears_the_filter(page, seeded):
    """The filter is DERIVED from the ring after every rebuild. Held beside it,
    the page went on projecting a subject no wedge was showing as selected."""
    page.set_mode(dash.MODE_SECURITIES)
    page.select_slice("ZZCC")                     # only the IRA holds it
    assert page.filter() == ("security", "ZZCC")
    page.set_account_scope([seeded["brokerage"]])
    assert page.ring.selected() is None
    assert page.filter() is None
    assert page.what_if_bar.scope_subject() == dash.WHAT_IF_ALL_SUBJECT


# --- the arrow edit, end to end (reported: "no longer working") -------------
def _type_amount(qapp, editor, text):
    """Edit the way the user does: focus, select what is there, type over it,
    press Return. ``setText`` plus a hand-emitted signal would pass even if the
    editor were disabled, read-only or unreachable by the keyboard."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    editor.setFocus()
    editor.selectAll()
    QTest.keyClicks(editor, text)
    QTest.keyClick(editor, Qt.Key_Return)
    qapp.processEvents()


def _ira_arrow(page, conn, seeded):
    """Give the IRA a fourth deposit inside the window so it earns an arrow
    too, and hand back the two arrow widgets."""
    ledger.create_transfer(conn, seeded["checking"], seeded["ira"], "2026-06-20",
                           100_00, memo="Contribution")
    page.refresh()
    by_id = {w.account_id(): w for w in page.arrow_widgets}
    assert set(by_id) == {seeded["brokerage"], seeded["ira"]}
    return by_id


def test_an_arrow_editor_takes_the_mouse_and_a_typed_edit_moves_the_fan(
        page, qapp, seeded):
    """Reported: "The inflow arrow edit is no longer working."

    The unit-level wiring was never the problem, so this drives the whole path
    the user's hand takes: the editor has to be the widget ``childAt`` finds at
    its own center (not the ring area, not a corner launcher), typing into it
    has to reach ``_what_if_inflows``, and the fan on screen has to move."""
    page.set_what_if(True)
    _shown(page, qapp, 1400, 900)
    widget = next(w for w in page.arrow_widgets
                  if w.account_id() == seeded["brokerage"])
    assert widget.is_editable() is True
    _assert_hittable(page, "brokerage arrow editor", widget.editor)

    before = page.projection_chart.canvas.what_if()[-1].p50
    _type_amount(qapp, widget.editor, "9000.00")
    assert page._what_if_inflows[seeded["brokerage"]] == 9_000_00
    assert page.projection_chart.canvas.what_if()[-1].p50 > before
    page.hide()


def test_an_arrow_outside_the_ring_filter_is_still_editable_and_lands(
        page, qapp, conn, seeded):
    """THE DEFECT. With a wedge selected, every arrow for some other account
    went read-only -- drawn, hittable, and silently inert, with nothing on
    screen saying why. An arrow that is drawn is editable; the page re-scopes
    itself to the fan the edit can move, so the answer is visible."""
    arrows = _ira_arrow(page, conn, seeded)
    page.set_what_if(True)
    page.select_slice(seeded["brokerage"])
    assert page.filter() == ("account", seeded["brokerage"])
    _shown(page, qapp, 1400, 900)

    ira = arrows[seeded["ira"]]
    assert ira.is_editable() is True
    _assert_hittable(page, "IRA arrow under a brokerage filter", ira.editor)

    before = page.projection_chart.canvas.what_if()[-1].p50
    _type_amount(qapp, ira.editor, "5000.00")
    assert page._what_if_inflows[seeded["ira"]] == 5_000_00
    assert page.projection_chart.canvas.what_if()[-1].p50 != before
    # The edit LANDED somewhere the user can see it: the projection on screen
    # now counts this account's inflow.
    assert seeded["ira"] in page._scoped_inflows()
    assert page.filter() in (None, ("account", seeded["ira"]))
    # Still never the database.
    assert {a.account_id: a.total
            for a in dash.inflow_arrows(conn, AS_OF)}[seeded["ira"]] == 400_00
    page.hide()


def test_an_arrow_is_editable_while_a_security_wedge_is_selected(
        page, qapp, seeded):
    """A security selection scopes the fan to a holding, and an inflow lands in
    an account -- so under one, EVERY arrow used to go read-only at once. The
    edit widens the page back to a subject that can carry a contribution."""
    page.set_what_if(True)
    page.set_mode(dash.MODE_SECURITIES)
    page.select_slice("ZZAA")
    assert page.filter() == ("security", "ZZAA")
    _shown(page, qapp, 1400, 900)

    widget = next(w for w in page.arrow_widgets
                  if w.account_id() == seeded["brokerage"])
    assert widget.is_editable() is True
    _assert_hittable(page, "arrow under a security filter", widget.editor)

    _type_amount(qapp, widget.editor, "7500.00")
    assert page._what_if_inflows[seeded["brokerage"]] == 7_500_00
    assert page.filter() is None
    assert seeded["brokerage"] in page._scoped_inflows()
    assert page.projection_chart.canvas.what_if()[-1].p50 > \
        page.projection_chart.canvas.baseline()[-1].p50
    page.hide()


def test_an_arrow_under_a_narrowed_gear_scope_is_editable_and_lands(
        page, qapp, conn, seeded):
    """The gear's scope and the ring's filter are two different narrowings, and
    the defect needed both to show its worst form: an account the gear KEPT,
    drawn with an arrow, that a wedge selection then made uneditable."""
    arrows = _ira_arrow(page, conn, seeded)
    page.set_account_scope([seeded["brokerage"], seeded["ira"]])
    arrows = {w.account_id(): w for w in page.arrow_widgets}
    page.set_what_if(True)
    page.select_slice(seeded["brokerage"])
    _shown(page, qapp, 1400, 900)

    ira = arrows[seeded["ira"]]
    assert ira.is_editable() is True
    _assert_hittable(page, "IRA arrow under gear scope + filter", ira.editor)

    before = page.projection_chart.canvas.what_if()[-1].p50
    _type_amount(qapp, ira.editor, "5000.00")
    assert page._what_if_inflows[seeded["ira"]] == 5_000_00
    assert page.projection_chart.canvas.what_if()[-1].p50 != before
    assert seeded["ira"] in page._scoped_inflows()
    page.hide()


def test_every_drawn_arrow_takes_the_mouse_however_many_there_are(qapp, conn):
    """The other half of the rule: an arrow that is drawn must also be
    REACHABLE. The band is placed by hand, and the top-left corner launcher is
    raised over its column, so a tall enough arrow stack put its first arrow
    underneath that launcher -- and the old ``max(0, ...)`` clamp pushed the
    last one under the mode buttons. Both are invisible on screen: the arrow
    paints, the click just goes somewhere else."""
    checking = ledger.create_account(conn, "Test Checking", "checking",
                                     opening_balance=1_000_000_00,
                                     opening_date=OPEN_DATE)
    portfolio.set_security(conn, "ZZAA", name="Zeta Alpha Growth Fund",
                           sec_type="fund")
    investments.record_price(conn, "ZZAA", BUY_DATE, "100.00")
    investments.record_price(conn, "ZZAA", AS_OF, "150.00")
    for i in range(6):
        account = ledger.create_account(conn, f"Test Fund {i}", "investment",
                                        opening_balance=10_000_00,
                                        opening_date=OPEN_DATE)
        _buy(conn, account, BUY_DATE, "ZZAA", "10", "100.00", -1_000_00)
        for date in ("2025-09-15", "2025-12-15", "2026-03-15", "2026-06-15"):
            ledger.create_transfer(conn, checking, account, date, 500_00,
                                   memo="Contribution")

    crowded = dash.InvestmentDashboardPage(conn, as_of=AS_OF)
    try:
        crowded.set_what_if(True)
        for width, height in ((1400, 900), (1100, 760), (1000, 620)):
            _shown(crowded, qapp, width, height)
            assert len(crowded.arrow_widgets) == 6
            for w in crowded.arrow_widgets:
                assert w.is_editable() is True
                _assert_hittable(crowded, f"{width}x{height} {w.account_name()}",
                                 w.editor)
        crowded.hide()
    finally:
        crowded.deleteLater()


# --- alignment (the user's layout requirement) ------------------------------
def _shown(page, qapp, width=1200, height=800):
    """Give the page real geometry. Nested layouts only lay out once the widget
    is polished: activating the root layout alone leaves every grandchild at its
    default 100x30, and the band is placed against the canvases' real sizes."""
    page.resize(width, height)
    page.show()
    qapp.processEvents()
    qapp.processEvents()          # the band's placement is deferred one tick
    return page


def _center_y(page, widget):
    from PyQt5.QtCore import QPoint
    return widget.mapTo(page, QPoint(0, 0)).y() + widget.height() // 2


def test_the_band_stacks_arrows_then_what_if_then_thermometer(page, qapp):
    """The band has no layout -- it is placed by hand against the plots -- so the
    order is a fact about where the blocks land, not about layout indices.

    The mode switch is no longer one of them: it moved inside the ring
    (reported), which is what leaves What If alone in the gap.
    """
    from PyQt5.QtCore import QPoint
    _shown(page, qapp)
    assert page.left_band.layout() is None
    blocks = [page.arrow_box, page.what_if_bar, page.thermometer]
    ys = [w.mapTo(page, QPoint(0, 0)).y() for w in blocks]
    assert ys == sorted(ys)
    assert not page.left_band.isAncestorOf(page.mode_row)
    page.hide()


def test_the_mode_switch_clears_the_top_plots_title(page, qapp):
    """Reported: "moved higher so there is significant space between them and
    the title of the top plot". It sat on BAND_SPACING's 4px, which read as the
    switch and the title being one stacked block.

    How far it can rise is bounded, so this asserts a real separation rather
    than a literal: the inner circle narrows as it goes up, and a row placed by
    the requested gap alone would put its top corners out under the annulus,
    which is painted in front of it.
    """
    import math
    for size in ((1200, 800), (1000, 700), (1600, 1000)):
        page.resize(*size)
        page.show()
        for _ in range(4):
            qapp.processEvents()
        area = page.ring_area
        row = page.mode_row.geometry()
        title_y = area.selector_rects()["top"][1]
        gap = title_y - row.bottom()
        assert gap >= 4 * dash.BAND_SPACING, f"{size}: only {gap}px of air"
        assert row.bottom() < title_y, f"{size}: the switch overlaps the title"
        assert row.top() >= 0

        # Still inside the inner circle: both top corners covered.
        cy = area.height() / 2.0
        r_inner = area.outer_radius() * dash.RING_INNER_RADIUS
        half = math.sqrt(max(0.0, r_inner ** 2 - (cy - row.top()) ** 2))
        assert half >= row.width() / 2 - 1, (
            f"{size}: the switch's corners are out under the ring")
    page.hide()


def test_what_if_draws_labelled_connectors_to_what_it_turns_on(page, qapp):
    """Reported: "when the What-If button is pressed draw line(arrows) from the
    button to the arrow and the thermometer and label them 'change
    contributions' and 'change asset mix' respectively."

    What If is a MODE -- while it is on, the arrows and the thermometer stop
    reporting what was measured and start accepting what the user wants to try.
    Nothing said so before; these connectors are that sentence, drawn.
    """
    _shown(page, qapp)
    overlay = page.connectors
    assert not overlay.isVisible(), "no connectors until the mode is on"
    assert overlay._legs == []

    page.what_if_bar.button.setChecked(True)
    for _ in range(3):
        qapp.processEvents()
    assert overlay.isVisible()
    labels = [leg[2] for leg in overlay._legs]
    assert labels == [dash.CONNECTOR_TO_ARROWS, dash.CONNECTOR_TO_THERMOMETER]
    assert dash.CONNECTOR_TO_ARROWS == "change contributions"
    assert dash.CONNECTOR_TO_THERMOMETER == "change asset mix"

    # One leg points UP at the arrows, the other DOWN at the thermometer, and
    # each ends against the block it names.
    up, down = overlay._legs
    assert up[1] < up[0], "the contributions leg points up at the arrows"
    assert down[1] > down[0], "the asset-mix leg points down at the thermometer"
    assert up[1] >= page.arrow_box.geometry().bottom()
    assert down[1] <= page.thermometer.geometry().top()

    page.what_if_bar.button.setChecked(False)
    for _ in range(3):
        qapp.processEvents()
    assert not overlay.isVisible()
    assert overlay._legs == []
    page.hide()


def test_the_connectors_do_not_steal_clicks_from_what_they_cross(page, qapp):
    """The overlay covers the whole band, including the arrows and the
    thermometer it points at. Mouse-transparency is safe HERE and is not
    elsewhere on this page -- Qt skips a mouse-transparent widget's whole
    SUBTREE when picking a receiver, which is what made the band's own controls
    dead when it was tried there -- because this overlay has no children."""
    from PyQt5.QtCore import Qt
    _shown(page, qapp)
    page.what_if_bar.button.setChecked(True)
    for _ in range(3):
        qapp.processEvents()
    overlay = page.connectors
    assert overlay.testAttribute(Qt.WA_TransparentForMouseEvents)
    assert overlay.children() == []
    # The band still answers for a point the overlay covers.
    band = page.left_band
    point = page.what_if_bar.geometry().center()
    assert overlay.geometry().contains(point)
    assert band.childAt(point) is not overlay
    page.hide()


def test_what_if_sits_halfway_between_the_arrows_and_the_thermometer(page, qapp):
    """Reported: "put the What-If row half-way between the arrows and the
    thermometer". It used to be stacked hard against the mode switch at the top
    of the gap; with the switch gone it is centered in what is left."""
    _shown(page, qapp)
    bar = page.what_if_bar.geometry()
    above = bar.top() - page.arrow_box.geometry().bottom()
    below = page.thermometer.geometry().top() - bar.bottom()
    assert above > 0 and below > 0, "it must not overlap either neighbour"
    assert abs(above - below) <= 2, f"clearances {above} vs {below}"
    page.hide()


def test_the_mode_buttons_live_inside_the_ring_near_the_top(page, qapp):
    """Reported: "let's move the Accounts/Securities button inside the ring near
    the top". The switch says what the RING's wedges are, so it belongs with
    them. Same group, same exclusivity, same wiring -- only the parent changed.
    """
    from PyQt5.QtCore import QPoint
    _shown(page, qapp)
    area = page.ring_area
    for btn in page.mode_buttons.values():
        assert area.isAncestorOf(btn)
        assert page.mode_group.id(btn) is not None
    assert page.mode_group.exclusive() is True

    # Inside the ring: horizontally over the hole, and above the top caption.
    row = page.mode_row.geometry()
    hx, hy, hw, hh = area.hole_rect()
    assert hx <= row.center().x() <= hx + hw
    assert row.bottom() <= area.selector_rects()["top"][1]
    # ...and in the upper half of the ring area, not floated off the page top.
    assert row.top() < area.height() // 2
    # And they still switch the ring.
    page.mode_buttons[dash.MODE_SECURITIES].click()
    assert page.mode() == dash.MODE_SECURITIES
    assert page.mode_buttons[dash.MODE_ACCOUNTS].isChecked() is False
    page.hide()


def test_the_ring_owns_the_full_page_height(page, qapp):
    """Nothing is stacked above or below it any more: the corner launchers are
    overlays inside it."""
    from PyQt5.QtCore import QPoint
    _shown(page, qapp)
    top = page.ring_area.mapTo(page, QPoint(0, 0)).y()
    assert top == 0
    assert page.ring_area.height() == page.height()
    page.hide()


def test_all_four_launchers_are_still_in_their_corners_and_clickable(page, qapp):
    _shown(page, qapp)
    area = page.ring_area
    rects = area.corner_rects()
    assert set(rects) == set(dash.CORNER_NAMES)
    for name, (x, y, w, h) in rects.items():
        frame = page.placeholders[name]
        assert (frame.x(), frame.y(), frame.width(), frame.height()) == (x, y, w, h)
        assert frame.isVisible()
        # Raised over the ring canvas, so a click in the corner reaches the
        # launcher rather than the wedge underneath it.
        hit = area.childAt(x + w // 2, y + h // 2)
        assert hit is frame or frame.isAncestorOf(hit)
    # Corners, plainly: one box per quadrant of the ring area.
    assert rects["cornerTopLeft"][0] < rects["cornerTopRight"][0]
    assert rects["cornerTopLeft"][1] < rects["cornerBottomLeft"][1]
    page.hide()


def test_the_arrows_line_up_with_the_top_plot_and_the_thermometer_with_the_fan(
        page, qapp, seeded):
    """The user's alignment requirement, stated against the plots themselves:
    the arrow block centers on the value-history canvas and the thermometer on
    the projection fan."""
    _shown(page, qapp)
    assert page.arrow_widgets                       # the brokerage has an arrow
    assert _center_y(page, page.arrow_box) == pytest.approx(
        _center_y(page, page.history_chart.canvas), abs=8)
    assert _center_y(page, page.thermometer) == pytest.approx(
        _center_y(page, page.projection_chart.canvas), abs=8)

    # And it survives a resize -- the alignment is computed, not a fixed inset.
    page.resize(1000, 700)
    qapp.processEvents()
    qapp.processEvents()
    assert _center_y(page, page.arrow_box) == pytest.approx(
        _center_y(page, page.history_chart.canvas), abs=8)
    assert _center_y(page, page.thermometer) == pytest.approx(
        _center_y(page, page.projection_chart.canvas), abs=8)
    page.hide()


def test_the_thermometer_is_two_thirds_of_an_even_share_of_the_band(page, qapp):
    """It used to take a full stretch share of the band, which read as the
    page's main subject. It is a selector. Then, on the second pass: "the
    thermometer can be reduced in height to 3/4 its current height" -- three
    quarters of that two thirds, so half an even share."""
    assert dash.THERMOMETER_HEIGHT_SCALE == pytest.approx((2.0 / 3.0) * 0.75)
    _shown(page, qapp)
    furniture = (page.mode_row.sizeHint().height()
                 + page.what_if_bar.sizeHint().height()
                 + 3 * dash.BAND_SPACING)
    even_share = (page.left_band.height() - furniture) // 2
    assert page.thermometer.height() == pytest.approx(
        even_share * dash.THERMOMETER_HEIGHT_SCALE, abs=2)
    assert page.thermometer.height() < even_share
    # The floor came down with it: a minimum of 90 would have won this argument
    # silently on a short page.
    assert page.thermometer.minimumHeight() == dash.THERMOMETER_MIN_HEIGHT
    assert dash.THERMOMETER_MIN_HEIGHT < 90
    page.hide()


def test_the_hole_stacks_history_then_center_line_then_projection(page):
    """The center line itself left the layout (see the test below), but the strip
    it occupies is still reserved between the two plots."""
    lay = page.hole.layout()
    assert lay.indexOf(page.history_chart) < lay.indexOf(page.center_gap)
    assert lay.indexOf(page.center_gap) < lay.indexOf(page.projection_chart)
    assert lay.indexOf(page.center) == -1       # not a row in the hole any more


def test_the_center_line_has_its_own_rectangle_wider_than_the_plots(page, qapp):
    """Reported: the center line "is being cut off by being in the same rectangle
    as the two plots. It needs its own rectangle on top of the plot rectangle so
    it can extend the full width of the circle." So it is a child of the ring
    area at the inner circle's full width, over -- not inside -- the hole."""
    _shown(page, qapp)
    assert page.center.parent() is page.ring_area
    assert page.center.parent() is not page.hole

    _cx, _cy, cw, _ch = page.ring_area.center_rect()
    _hx, _hy, hw, _hh = page.ring_area.hole_rect()
    assert cw > hw
    r_inner = page.ring_area.outer_radius() * dash.RING_INNER_RADIUS
    assert cw == pytest.approx(2 * r_inner, abs=2)
    assert page.center.width() > page.hole.width()

    # Larger font than the plots' rectangle gave it, and the gap above it is the
    # line's own height rather than a layout spacing on top of it.
    assert dash.CENTER_FONT_SCALE > 1.0
    assert page.hole.layout().spacing() == 0
    assert page.center_gap.height() == max(1, page.center.sizeHint().height())
    page.hide()


# --- the ring in front, the selectors outside it (the second report) --------
def _drawn_ring_radius(ring):
    """The outer radius matplotlib actually painted, in widget pixels.

    Measured, not computed: the wedges run to r=1 in data units, so the distance
    from the data origin to (1, 0) on screen IS the ring's outer radius. Nothing
    here reads a module constant, which is the point -- the bug was the drawn
    circle drifting away from the geometry the rest of the page assumed."""
    ring.figure.canvas.draw()
    ax = ring.figure.axes[0]
    origin = ax.transData.transform((0.0, 0.0))
    edge = ax.transData.transform((1.0, 0.0))
    return abs(edge[0] - origin[0]) / (ring.devicePixelRatioF() or 1.0)


def test_the_ring_radius_survives_switching_accounts_and_securities(page, qapp):
    """Reported: "every time I switch from Accounts to Securities, the radius of
    the ring shrinks." It must be a pure function of the widget's rect -- not of
    the slice count, the label lengths, or a layout engine re-fitting the axes on
    every draw."""
    _shown(page, qapp)
    area = page.ring_area
    first = _drawn_ring_radius(page.ring)
    hole = area.hole_rect()
    assert first == pytest.approx(area.outer_radius(), abs=1.0)

    for _ in range(3):
        page.set_mode(dash.MODE_SECURITIES)
        qapp.processEvents()
        assert _drawn_ring_radius(page.ring) == pytest.approx(first, abs=0.5)
        assert area.hole_rect() == hole
        page.set_mode(dash.MODE_ACCOUNTS)
        qapp.processEvents()
        assert _drawn_ring_radius(page.ring) == pytest.approx(first, abs=0.5)
        assert area.hole_rect() == hole
    page.hide()


def test_a_bare_ring_draws_the_same_circle_however_often_it_is_redrawn(qapp):
    """The same defect one layer down, away from the page: re-rendering a canvas
    must not move the circle. It did -- an inherited tight-layout engine re-fit
    the manually placed axes at every draw."""
    ring = dash.RingCanvas([("a", "A", 100), ("b", "B", 300)])
    ring.resize(600, 400)
    first = _drawn_ring_radius(ring)
    for _ in range(4):
        ring.render()
        assert _drawn_ring_radius(ring) == pytest.approx(first, abs=0.5)
    # ...and a ring with more, longer-labelled slices draws the same circle.
    ring.set_slices([(str(i), f"A very long slice label number {i}", 100)
                     for i in range(9)])
    assert _drawn_ring_radius(ring) == pytest.approx(first, abs=0.5)
    ring.deleteLater()


def test_each_period_selector_sits_outside_the_hole(page, qapp):
    """Reported: "the time range selectors [should be] put above and below the
    top and bottom plots respectively" -- clear of the hole, where the ring is
    now painted in front and would cover them."""
    _shown(page, qapp)
    area = page.ring_area
    hx, hy, hw, hh = area.hole_rect()
    top = page.history_chart.period
    bottom = page.projection_chart.period

    # Each combo now travels inside the header that titles its plot, and the
    # HEADER is what the area places -- but it is still the very same combo.
    headers = {top: page.history_header, bottom: page.projection_header}
    for combo, header in headers.items():
        assert combo.parent() is header         # not a row inside the chart
        assert header.parent() is area
        assert combo.isVisible()
        assert combo.width() > 0 and combo.height() > 0

    top_geom = page.history_header.geometry()
    bottom_geom = page.projection_header.geometry()
    assert top_geom.bottom() < hy                        # wholly above the hole
    assert bottom_geom.top() > hy + hh                   # wholly below it
    assert top_geom.top() >= 0
    assert bottom_geom.bottom() <= area.height()

    # Same widgets, same options, same wiring: only the placement moved.
    assert page.history_chart.period_values() == [1, 2, 3, 5, 8, 10, None]
    assert page.projection_chart.period_values() == [5, 10, 20, 30, 40, 50]
    heard = []
    page.history_chart.periodChanged.connect(heard.append)
    page.history_chart.set_years(3)
    assert heard == [3]
    page.hide()


def test_the_top_plot_is_titled_with_whatever_it_is_currently_scoped_to(
        page, qapp, seeded):
    """"I need a title for the top plot, left of the time-range dropdown. Use
    the Total, the account name or the ticker followed by Performance." The
    curve is the same shape whichever of the three it is drawing, so the title
    is the only thing that says which."""
    _shown(page, qapp)
    header = page.history_header
    assert header.title() == "Total Performance"

    page.select_slice(seeded["ira"])
    qapp.processEvents()
    assert page.filter_subject() == "Test IRA"
    assert header.title() == "Test IRA Performance"

    page.clear_filter()
    qapp.processEvents()
    assert header.title() == "Total Performance"

    page.set_mode(dash.MODE_SECURITIES)
    page.select_slice("ZZAA")
    qapp.processEvents()
    assert header.title() == "ZZAA Performance"

    # It sits LEFT of the dropdown it shares a row with, and on that row.
    label = header.title_label
    combo = page.history_chart.period
    assert label.geometry().right() <= combo.geometry().left()
    assert label.geometry().center().y() == pytest.approx(
        combo.geometry().center().y(), abs=4)
    page.hide()


def test_the_fan_is_labelled_and_disclaimed(page, qapp):
    """"Put 'Projected Future Value' in front of the time-range of the bottom
    plot. Underneath that row put 'Disclaimer: ...'." A fan of futures read as
    a forecast is the misreading; the sentence is the user's own words."""
    _shown(page, qapp)
    header = page.projection_header
    assert header.title() == "Projected Future Value"
    assert header.note() == (
        "Disclaimer: projections show estimated future performance ranges "
        "based on risk models, but no model can predict the actual future.")

    combo = page.projection_chart.period
    label, note = header.title_label, header.note_label
    assert label.geometry().right() <= combo.geometry().left()
    assert label.geometry().center().y() == pytest.approx(
        combo.geometry().center().y(), abs=4)
    # Underneath that row, wrapped, and smaller than the title.
    assert note.geometry().top() >= combo.geometry().bottom()
    assert note.wordWrap() is True
    assert note.isVisible() and note.height() > 0
    assert dash.NOTE_FONT_SCALE < 1.0
    # Point size and pixel size are alternatives in Qt -- whichever the theme
    # used, the note's is the smaller one.
    def _size(widget):
        points = widget.font().pointSizeF()
        return points if points > 0 else widget.font().pixelSize()
    assert _size(note) < _size(label)
    page.hide()


def test_the_center_line_is_on_top_of_everything_in_the_hole(page, qapp):
    """Reported (again): "The center line is invisible... Needs to be on top of
    everything." childAt() is the only honest test of that -- it answers with
    the frontmost child that takes the mouse, and the transparent-for-mouse
    attribute that once hid the line makes Qt skip its whole subtree."""
    _shown(page, qapp)
    area = page.ring_area

    def hit_at_the_center_line():
        cx, cy, cw, ch = area.center_rect()
        found = area.childAt(cx + cw // 2, cy + ch // 2)
        return found

    hit = hit_at_the_center_line()
    assert hit is page.center or page.center.isAncestorOf(hit)

    # Last in the child list is frontmost, and it has to be re-raised by every
    # path that moves things -- a refresh...
    page.refresh()
    qapp.processEvents()
    hit = hit_at_the_center_line()
    assert hit is page.center or page.center.isAncestorOf(hit)

    # ...and a mode switch, which rebuilds the ring under it.
    page.set_mode(dash.MODE_SECURITIES)
    qapp.processEvents()
    hit = hit_at_the_center_line()
    assert hit is page.center or page.center.isAncestorOf(hit)

    from PyQt5.QtCore import Qt
    assert page.center.testAttribute(Qt.WA_TransparentForMouseEvents) is False
    page.hide()


def test_the_ring_is_painted_in_front_of_the_plots_but_not_over_their_clicks(
        page, qapp):
    """Reported: "the corners of the plots overlap the ring... it would work out
    okay if the ring were plotted in front of the plots." In front, translucent,
    and masked -- a Qt mask governs hit testing too, so the hole stays live."""
    from PyQt5.QtCore import QPoint, Qt
    from PyQt5.QtWidgets import QWidget
    _shown(page, qapp)
    area = page.ring_area

    # raise_() moves a child to the end of the parent's child list: the ring is
    # after the hole (in front of it) and, since the second report, after the
    # launchers too -- an exploded wedge has to paint over a corner box.
    kids = [c for c in area.children() if isinstance(c, QWidget)]
    assert kids.index(area.ring) > kids.index(area.hole)
    for name in dash.CORNER_NAMES:
        assert kids.index(area.ring) > kids.index(page.placeholders[name])

    # Translucent, or an opaque canvas would paint the charts out entirely.
    assert area.ring.testAttribute(Qt.WA_TranslucentBackground) is True
    assert area.ring.testAttribute(Qt.WA_OpaquePaintEvent) is False

    mask = area.ring.mask()
    assert not mask.isEmpty()
    center = QPoint(area.width() // 2, area.height() // 2)
    assert mask.contains(center) is False                       # the hole
    on_band = QPoint(int(area.width() / 2 - area.outer_radius() * 0.97),
                     area.height() // 2)
    assert mask.contains(on_band) is True                       # the band

    # Probed a quarter of the way down, in the TOP PLOT'S BODY: the hole's own
    # center belongs to the center line, which is deliberately laid over the
    # plots (see the center-line tests) and would answer there instead.
    hx, hy, hw, hh = area.hole_rect()
    hit = area.childAt(hx + hw // 2, hy + hh // 4)
    assert hit is not None and hit is not area.ring
    assert hit is area.hole or area.hole.isAncestorOf(hit)
    page.hide()


def test_an_empty_ring_keeps_its_message_and_gets_out_of_the_way(qapp):
    """With no wedges the canvas draws only its "nothing here" text, dead
    center. It is masked to the HOLE RECT -- enough for the text -- and NEVER
    left unmasked: an unmasked canvas is painted across the whole area and would
    swallow every click meant for the corner launchers, the selectors, the gear
    and the left band. With nothing to click on it, it also goes
    mouse-transparent, so the charts underneath keep their clicks."""
    from PyQt5.QtCore import QPoint, Qt
    from PyQt5.QtWidgets import QWidget
    ring = dash.RingCanvas([])
    area = dash.RingArea(ring, QWidget())
    area.resize(400, 400)
    area.show()
    qapp.processEvents()
    assert ring.wedge_count() == 0

    mask = ring.mask()
    assert not mask.isEmpty()
    assert mask.contains(QPoint(ring.width() // 2, ring.height() // 2))  # the text
    assert mask.boundingRect().width() < ring.width()      # not the whole rect
    assert ring.testAttribute(Qt.WA_TransparentForMouseEvents) is True
    area.hide()
    area.deleteLater()


def test_the_band_is_right_aligned_to_the_rings_left_tangent(page, qapp):
    """Reported: "I want them against the vertical line tangent to the left edge
    of the ring (before it shrunk)", then "the arrow and thermometer need a
    spacer between them and the ring, maybe 50 pixels" -- so the band's right
    edge is ``ring center x - R_outer - BAND_RING_GAP``, with the blocks still
    centered on their plots."""
    from PyQt5.QtCore import QPoint, Qt
    assert dash.BAND_RING_GAP == 50
    for width, height in ((1200, 800), (1000, 700)):
        _shown(page, qapp, width, height)
        area = page.ring_area
        area_x = area.mapTo(page, QPoint(0, 0)).x()
        tangent = area_x + round(area.width() / 2 - area.outer_radius())
        band_x = page.left_band.mapTo(page, QPoint(0, 0)).x()

        assert page.left_band.width() == dash.LEFT_BAND_WIDTH
        assert band_x + page.left_band.width() == pytest.approx(
            tangent - dash.BAND_RING_GAP, abs=2)
        assert band_x >= 0
        # The tangent is inside the ring area, so the band overlaps it. The ring
        # area is masked to its own children, which is what keeps the band's
        # blocks reachable -- the band must NOT be mouse-transparent, because Qt
        # skips such a widget's whole subtree and that killed every control on it.
        assert band_x < area_x + area.width()
        assert page.left_band.testAttribute(Qt.WA_TransparentForMouseEvents) is False

        assert _center_y(page, page.arrow_box) == pytest.approx(
            _center_y(page, page.history_chart.canvas), abs=8)
        assert _center_y(page, page.thermometer) == pytest.approx(
            _center_y(page, page.projection_chart.canvas), abs=8)
    page.hide()


# --- dark mode (the labels the user could not read) -------------------------
def _tick_label_colors(ax):
    return {label.get_color() for label in ax.get_yticklabels()} | {
        label.get_color() for label in ax.get_xticklabels()}


def test_the_hole_charts_take_their_label_colors_from_the_dark_palette(
        qapp, monkeypatch):
    """Reported: the plot labels are unreadable in dark mode. matplotlib's
    defaults are near-black, and the figure is transparent over a dark page."""
    from matplotlib.colors import to_rgba

    from mammon.ui import style

    canvas = dash.ValueHistoryCanvas()
    canvas.set_series([("2025-01-01", 100_000_00), ("2025-06-01", 150_000_00)])
    light = _tick_label_colors(canvas.figure.axes[0])

    monkeypatch.setattr(style, "theme", lambda: "dark")
    canvas.render()                     # resolved at DRAW time, not at build
    ax = canvas.figure.axes[0]
    dark_pal = style.palette_for("dark")
    dark = _tick_label_colors(ax)

    assert dark != light
    assert dark == {dark_pal["text"]}
    assert ax.yaxis.label.get_color() == dark_pal["text"]
    assert ax.xaxis.label.get_color() == dark_pal["text"]
    assert to_rgba(ax.spines["left"].get_edgecolor()) == to_rgba(dark_pal["line"])
    canvas.deleteLater()


def test_the_projection_fan_is_themed_too_and_so_is_its_empty_message(
        qapp, monkeypatch):
    from mammon.ui import style

    monkeypatch.setattr(style, "theme", lambda: "dark")
    dark_pal = style.palette_for("dark")

    fan = dash.ProjectionFanCanvas()            # no data: the empty message
    ax = fan.figure.axes[0]
    assert [t.get_text() for t in ax.texts] == [dash.PROJECTION_EMPTY_TEXT]
    assert ax.texts[0].get_color() == dark_pal["muted"]
    fan.deleteLater()


def test_light_mode_is_left_alone(qapp, monkeypatch):
    """The palette is only imposed in dark mode -- charts.py's own rule."""
    from mammon.ui import style

    monkeypatch.setattr(style, "theme", lambda: "light")
    canvas = dash.ValueHistoryCanvas()
    canvas.set_series([("2025-01-01", 100_000_00), ("2025-06-01", 150_000_00)])
    dark_pal = style.palette_for("dark")
    assert dark_pal["text"] not in _tick_label_colors(canvas.figure.axes[0])
    canvas.deleteLater()


# --- readability: fonts, gridlines, the band legend, contrast ----------------
def _both_canvases():
    hist = dash.ValueHistoryCanvas()
    hist.set_series([("2025-01-01", 100_000_00), ("2025-06-01", 150_000_00)])
    fan = dash.ProjectionFanCanvas()
    fan.set_fans(dash.projection_fan(100_000_00, 1_000_00, 3.0, 10))
    return hist, fan


def test_both_hole_charts_draw_gridlines(qapp):
    """Reported: "I'd also like gridlines." Behind the data, both axes."""
    hist, fan = _both_canvases()
    for canvas in (hist, fan):
        ax = canvas.figure.axes[0]
        assert ax.get_axisbelow() is True
        for lines in (ax.xaxis.get_gridlines(), ax.yaxis.get_gridlines()):
            assert lines and all(line.get_visible() for line in lines)
        canvas.deleteLater()


def test_the_plot_type_is_big_enough_to_read(qapp):
    """Reported: "The numbers on the two plots are too small to read." The
    ticks were 6pt and the empty message 7pt."""
    assert dash.TICK_FONT_SIZE >= 8
    assert dash.EMPTY_FONT_SIZE >= 9
    hist, fan = _both_canvases()
    for canvas in (hist, fan):
        ax = canvas.figure.axes[0]
        assert {label.get_fontsize() for label in ax.get_yticklabels()} == {
            float(dash.TICK_FONT_SIZE)}
        canvas.deleteLater()


def test_the_legend_and_the_ticks_are_bigger_than_the_plots_base_type(qapp):
    """Reported: "the legend is illegible. Increase the font size... increase
    the tick fonts by another point." Both are sized UP from the figure's base
    -- the band names are prose, which needs more type than a number does."""
    assert dash.LEGEND_FONT_SIZE > dash.PLOT_BASE_FONT_SIZE
    assert dash.TICK_FONT_SIZE > dash.PLOT_BASE_FONT_SIZE
    fan = dash.ProjectionFanCanvas()
    fan.set_fans(dash.projection_fan(100_000_00, 1_000_00, 3.0, 10))
    ax = fan.figure.axes[0]
    sizes = {t.get_fontsize() for t in ax.get_legend().get_texts()}
    assert sizes == {float(dash.LEGEND_FONT_SIZE)}
    assert min(sizes) > float(dash.PLOT_BASE_FONT_SIZE)
    assert {label.get_fontsize() for label in ax.get_xticklabels()} == {
        float(dash.TICK_FONT_SIZE)}
    fan.deleteLater()


def test_the_projection_bands_are_named_by_percentile_not_by_sigma(qapp):
    """Reported: "There is no indication on the projected value plot of what
    the different colored ranges mean. Is it 1 and 2 standard deviations?" It
    is neither -- design 2.3 draws the 5/25/50/75/95 percentiles."""
    fan = dash.ProjectionFanCanvas()
    fan.set_fans(dash.projection_fan(100_000_00, 1_000_00, 3.0, 10))
    legend = fan.figure.axes[0].get_legend()
    assert legend is not None
    labels = [t.get_text() for t in legend.get_texts()]
    assert labels == [dash.FAN_OUTER_LABEL, dash.FAN_INNER_LABEL,
                      dash.FAN_MEDIAN_LABEL]
    assert not any("sigma" in t or "deviation" in t for t in labels)
    for pct in ("5th", "25th", "75th", "95th", "50th"):
        assert any(pct in t for t in labels)
    fan.deleteLater()


def test_the_what_if_overlay_names_the_measured_fan_it_covers(qapp):
    fan = dash.ProjectionFanCanvas()
    fan.set_fans(dash.projection_fan(100_000_00, 1_000_00, 3.0, 10),
                 dash.projection_fan(100_000_00, 5_000_00, 5.0, 10))
    labels = [t.get_text() for t in fan.figure.axes[0].get_legend().get_texts()]
    assert labels.count(dash.FAN_BASELINE_LABEL) == 1   # one entry, three lines
    fan.deleteLater()


def test_the_series_colors_follow_the_theme(qapp, monkeypatch):
    """Reported: "The plots have poor contrast in both dark and light mode."
    The lines and bands used to be hardcoded hex tuned for a white page."""
    from mammon.ui import style

    monkeypatch.setattr(style, "theme", lambda: "light")
    light = dash.chart_colors()
    assert light["history"] == style.palette_for("light")["blue"]

    monkeypatch.setattr(style, "theme", lambda: "dark")
    dark = dash.chart_colors()
    assert dark["history"] == style.palette_for("dark")["blue"]
    assert dark["what_if"] == style.palette_for("dark")["negative"]
    assert dark["history"] != light["history"]

    canvas = dash.ValueHistoryCanvas()
    canvas.set_series([("2025-01-01", 100_000_00), ("2025-06-01", 150_000_00)])
    assert canvas.color() == dark["history"]
    assert canvas.figure.axes[0].lines[0].get_color() == dark["history"]
    canvas.deleteLater()


def test_clearing_the_ring_filter_puts_the_default_color_back(page, seeded):
    """A wedge color must not outlive the wedge: ``set_series`` always writes
    the color it was handed, so an unfiltered chart is never left painted in
    the color of the slice that used to be selected."""
    page.select_slice(seeded["ira"])
    wedge = page.history_chart.canvas.color()
    page.select_slice(seeded["ira"])            # clears the filter
    assert page.history_chart.canvas.color() == dash.chart_colors()["history"]
    assert page.history_chart.canvas.color() != wedge


# --- the four corner launchers ----------------------------------------------
# The page must not actually enter a modal loop here: a QDialog.exec_()'d under
# the offscreen platform never returns, so the run would hang rather than fail.
# The page funnels every launch through two seams -- ``_open_report`` for the
# modeless report window and ``_exec_dialog`` for a modal one -- and these tests
# replace those seams and assert what each corner aimed at.

def _capture(page, monkeypatch):
    """Replace both launch seams; return the list they record into."""
    seen = []
    monkeypatch.setattr(page, "_open_report",
                        lambda spec: seen.append(("report", spec)))
    monkeypatch.setattr(page, "_exec_dialog",
                        lambda dlg: seen.append(("dialog", dlg)))
    return seen


def test_the_corners_carry_the_four_labels_the_user_asked_for(page):
    assert [page.placeholders[n].text() for n in dash.CORNER_NAMES] == [
        "Capital Gains and Taxes",      # top left
        "Performance Report",           # top right
        "Set Asset Categories",         # bottom left
        "Explore Rebalancing",          # bottom right
    ]


def test_each_corner_button_calls_its_own_page_method(page, monkeypatch):
    """The wiring, tested without opening anything: pressing a corner calls the
    method named for it, and no other."""
    called = []
    for name, method in dash.CORNER_ACTIONS.items():
        monkeypatch.setattr(page, method,
                            lambda m=method: called.append(m))
    for name in dash.CORNER_NAMES:
        called.clear()
        page.placeholders[name].click()
        assert called == [dash.CORNER_ACTIONS[name]]


def test_top_left_opens_the_capital_gains_and_taxes_report(page, monkeypatch):
    from mammon.ui.report_window import CAPITAL_GAINS_SPEC
    seen = _capture(page, monkeypatch)
    page.placeholders["cornerTopLeft"].click()
    assert seen == [("report", CAPITAL_GAINS_SPEC)]


def test_top_right_opens_the_investment_performance_report(page, monkeypatch):
    from mammon.ui.report_window import INVESTMENT_PERFORMANCE_SPEC
    seen = _capture(page, monkeypatch)
    page.placeholders["cornerTopRight"].click()
    assert seen == [("report", INVESTMENT_PERFORMANCE_SPEC)]


def test_bottom_left_opens_the_asset_allocation_report(page, monkeypatch):
    """The requirement is a place a holding is GIVEN its mix. The old window
    could only display one -- set_mixture had no caller but the yfinance fetch
    -- so this corner now opens ui/asset_allocation, which can edit."""
    from mammon.ui.asset_allocation import AssetAllocationWindow
    seen = _capture(page, monkeypatch)
    page.placeholders["cornerBottomLeft"].click()
    assert len(seen) == 1
    kind, dlg = seen[0]
    assert kind == "dialog"
    assert isinstance(dlg, AssetAllocationWindow)
    # It covers what the dashboard is showing rather than re-deciding scope.
    assert dlg.account_ids == page.account_scope()
    assert dlg.as_of == page.as_of
    dlg.deleteLater()


def test_bottom_right_opens_the_fund_rebalancer(page, monkeypatch):
    """The class-based Target & Drift editor is gone (user, 2026-09-21: "I
    don't want the old interface at all. Just the new one"). A weight per asset
    class is not a tradeable instruction; this corner opens the window that
    states the target in funds."""
    from mammon.ui.fund_target_window import FundTargetWindow
    seen = _capture(page, monkeypatch)
    page.placeholders["cornerBottomRight"].click()
    assert len(seen) == 1
    kind, dlg = seen[0]
    assert kind == "dialog"
    assert isinstance(dlg, FundTargetWindow)
    assert dlg.as_of == page.as_of
    dlg.deleteLater()


def test_a_report_window_is_kept_alive_and_a_second_one_does_not_evict_it(
        page, qapp, monkeypatch):
    """``_open_report`` is the real seam this time. A modeless window with no
    reference is collected the moment the launcher returns, and a single
    attribute would drop the first report when the second corner is pressed."""
    from mammon.ui.report_window import ReportWindow

    shown = []
    monkeypatch.setattr(ReportWindow, "show", lambda self: shown.append(self))
    page.open_capital_gains()
    page.open_performance_report()
    try:
        assert len(page._report_windows) == 2
        assert shown == page._report_windows
        titles = [w.windowTitle() for w in page._report_windows]
        assert titles == ["Capital Gains and Taxes", "Investment Performance"]
        assert all(w.parent() is page for w in page._report_windows)
    finally:
        for w in page._report_windows:
            w.close()


# --- the second revision pass -----------------------------------------------
def test_a_clicked_wedge_moves_out_half_as_far_as_it_used_to(qapp):
    """Reported: "the segments move out too much when clicked. Reduce the
    displacement to half the current value." The view limit has to follow it, or
    the ring would keep reserving room for the old, bigger explode and shrink."""
    assert dash.SELECTED_EXPLODE == pytest.approx(dash.SELECTED_EXPLODE_WAS / 2.0)
    assert dash.RING_VIEW_LIMIT == pytest.approx(1.0 + dash.SELECTED_EXPLODE)

    import math
    ring = dash.RingCanvas([("a", "A", 100), ("b", "B", 100)])
    ring.pick("a")
    centers = {lab: w.center for lab, w in ring._wedges}
    # Measured off the wedges matplotlib drew, in the axes' data units, where
    # the ring's outer edge is 1.0.
    assert math.hypot(*centers["A"]) == pytest.approx(dash.SELECTED_EXPLODE)
    assert math.hypot(*centers["B"]) == pytest.approx(0.0, abs=1e-9)
    ring.deleteLater()


def test_the_corner_boxes_sit_over_the_band_and_under_the_ring(page, qapp):
    """Reported: "the left side corner boxes need to be in front of the arrow
    and thermometer panels but behind the ring segments." Three levels, and the
    ring's mask is what lets the bottom two show through: an ANNULUS, so only
    the drawn band (and the room an exploded wedge needs) is opaque to clicks."""
    from PyQt5.QtCore import QPoint, Qt
    from PyQt5.QtWidgets import QWidget
    _shown(page, qapp)
    area = page.ring_area

    # Level 1 -> 2: the ring area (which owns the launchers) is after the band
    # in the page's child order, so the launchers paint over the arrows.
    siblings = [c for c in page.children() if isinstance(c, QWidget)]
    assert siblings.index(area) > siblings.index(page.left_band)
    # ... and its own background cannot eat the band's clicks, because it is
    # masked to the union of its children rather than made mouse-transparent
    # (which would have taken every one of ITS controls down with it).
    assert area.testAttribute(Qt.WA_TransparentForMouseEvents) is False
    assert not area.mask().isEmpty()
    # Mid-height at its left edge no child of the area reaches, so a click there
    # falls through to the band's mode buttons.
    assert area.mask().contains(QPoint(0, area.height() // 2)) is False

    # Level 2 -> 3: the ring is raised over the launchers (asserted in
    # test_the_ring_is_painted_in_front_of_the_plots_but_not_over_their_clicks),
    # and it only paints where it is drawn.
    mask = area.ring.mask()
    cx, cy = area.width() // 2, area.height() // 2
    radius = area.outer_radius()
    assert mask.contains(QPoint(int(cx - radius * 0.97), cy)) is True   # band
    assert mask.contains(QPoint(int(cx - radius * 1.01), cy)) is True   # explode
    # Outside the room an exploded wedge needs, the corner boxes are live.
    for name, (x, y, w, h) in area.corner_rects().items():
        pt = QPoint(x + w // 2, y + h // 2)
        assert mask.contains(pt) is False
        hit = area.childAt(pt)
        assert hit is page.placeholders[name] or \
            page.placeholders[name].isAncestorOf(hit)
    page.hide()


# --- every control is reachable by the mouse (the "nothing works" defect) ----
def _controls(page):
    """``{label: widget}`` for everything on the page a user clicks or drags."""
    controls = {
        "accounts toggle": page.mode_buttons[dash.MODE_ACCOUNTS],
        "securities toggle": page.mode_buttons[dash.MODE_SECURITIES],
        "what if": page.what_if_bar.button,
        "history period": page.history_chart.period,
        "projection period": page.projection_chart.period,
        "thermometer": page.thermometer.bar,
        "gear": page.gear,
    }
    for name in dash.CORNER_NAMES:
        controls[name] = page.placeholders[name]
    return controls


def _assert_hittable(page, label, widget):
    """What Qt does when the mouse is pressed: pick the child at that point.
    ``childAt`` walks the same z-order and the same masks, and it is the ONLY
    check that catches a control that is laid out and painted but dead."""
    from PyQt5.QtCore import QPoint
    assert widget.isVisible(), f"{label} is not visible"
    local = QPoint(widget.width() // 2, widget.height() // 2)
    point = widget.mapTo(page, local)
    hit = page.childAt(point)
    assert hit is not None, f"{label}: nothing at all is hittable at {point}"
    assert hit is widget or widget.isAncestorOf(hit), (
        f"{label}: a click at {point} lands on "
        f"{hit.objectName() or hit.__class__.__name__}, not on the control")


def test_every_control_on_the_page_takes_the_mouse(page, qapp):
    """Reported: "None of the controls on the Investment Dashboard are working
    now." Nothing had crashed -- the page painted correctly, but two overlay
    containers (this widget and the left band) carried
    ``WA_TransparentForMouseEvents``, and Qt skips a mouse-transparent widget's
    ENTIRE SUBTREE when it picks a mouse receiver. Every control lived in one of
    those two subtrees.

    So: for both ring modes, at two window sizes, every control has to be the
    thing the mouse finds at its own center -- and the ring's band has to keep
    taking its own clicks, which is what stopped the containers from simply
    being made transparent again."""
    from PyQt5.QtCore import QPoint
    for width, height in ((1400, 900), (1100, 760)):
        for mode in (dash.MODE_ACCOUNTS, dash.MODE_SECURITIES):
            page.set_mode(mode)
            _shown(page, qapp, width, height)
            assert page.ring.wedge_count() > 0
            for label, widget in _controls(page).items():
                _assert_hittable(page, f"{width}x{height} {mode}: {label}", widget)

            # Not at the cost of the ring: a point on the drawn band is still
            # the canvas's, so wedge clicks, explode and hover survive.
            area = page.ring_area
            on_band = QPoint(int(area.width() / 2 - area.outer_radius() * 0.97),
                             area.height() // 2)
            assert page.childAt(area.mapTo(page, on_band)) is area.ring
    page.hide()


def test_every_control_takes_the_mouse_with_an_empty_ring(qapp, conn):
    """The same, with NO holdings: the empty canvas draws its message across the
    middle of the area, and it must not take the page down with it. This is the
    state the old code left completely unmasked."""
    empty = dash.InvestmentDashboardPage(conn, as_of=AS_OF)
    try:
        _shown(empty, qapp, 1400, 900)
        assert empty.ring.wedge_count() == 0
        for label, widget in _controls(empty).items():
            _assert_hittable(empty, f"empty ring: {label}", widget)
        empty.hide()
    finally:
        empty.deleteLater()


def test_the_gear_is_at_the_top_left_of_the_performance_report_corner(page, qapp):
    """Reported: "we need the customization widget for accounts. Put the gear at
    the top just left of the Performance Report button." The widget is the
    application's existing one -- the report bar's customize button and dialog --
    not a second account picker."""
    from mammon.ui.report_filters import CustomizeDialog
    _shown(page, qapp)
    assert isinstance(page.customize_dialog, CustomizeDialog)
    assert page.gear.parent() is page.ring_area
    assert page.gear.isVisible()

    gx, gy, gw, _gh = page.ring_area.gear_rect()
    corner_x = page.ring_area.corner_rects()["cornerTopRight"][0]
    assert gy == 0                              # at the top
    assert gx + gw <= corner_x                  # just left of the corner button
    assert gx + gw >= corner_x - dash.BAND_SPACING - 1
    page.hide()


def test_the_gear_narrows_the_ring_the_plots_and_the_arrows(page, seeded):
    """What the gear is FOR: its selection is the dashboard's account scope, and
    everything that reads accounts reads it. Driven through the dialog's list
    and signal rather than by clicking the button -- the button opens a modal,
    which never returns under the offscreen platform."""
    from PyQt5.QtCore import Qt

    assert page.account_scope() is None                  # everything, by default
    assert set(page.ring.keys()) == {str(seeded["brokerage"]), str(seeded["ira"])}

    def tick(keep) -> None:
        lst = page.customize_dialog.filters.account_list
        for i in range(lst.count()):
            item = lst.item(i)
            item.setCheckState(Qt.Checked
                               if int(item.data(Qt.UserRole)) in keep
                               else Qt.Unchecked)
        page.customize_dialog.applied.emit()

    tick({seeded["checking"], seeded["brokerage"]})
    # The checking account is not an investment account, so it never enters the
    # scope -- what the gear narrows is the dashboard's own universe.
    assert page.account_scope() == [seeded["brokerage"]]
    assert set(page.ring.keys()) == {str(seeded["brokerage"])}
    assert page._scope_ids() == [seeded["brokerage"]]
    assert [a.account_id for a in page.arrows] == [seeded["brokerage"]]

    # A security only the excluded account holds leaves the ring with it.
    page.set_mode(dash.MODE_SECURITIES)
    # Cash rides along in securities mode now, and the gear narrows it too.
    assert set(page.ring.keys()) - {dash.CASH_KEY} == {"ZZAA", "ZZBB"}

    page.customize_dialog.filters.mark_accounts()
    page.customize_dialog.applied.emit()
    assert page.account_scope() is None
    assert set(page.ring.keys()) - {dash.CASH_KEY} == {"ZZAA", "ZZBB", "ZZCC"}


# --- a crypto wallet is an investment-like account, and the ring must show it -
@pytest.fixture
def wallet(conn, seeded):
    """An exchange wallet alongside the two brokerage accounts: $5,000 deposited,
    $3,000 of it spent on 2 ZZETH now worth $2,000 each.

    Its cash sleeve lives in ``crypto_transactions``, so the BROKERAGE valuation
    cannot see it -- which is the whole point of the regression below."""
    account_id = crypto.create_account(conn, "Test Exchange",
                                       kind=crypto.CRYPTO_KIND_EXCHANGE,
                                       opening_date=OPEN_DATE)
    crypto.record_cash(conn, account_id, OPEN_DATE, 5_000_00)
    crypto.record_buy(conn, account_id, BUY_DATE, "ZZETH", "2", 3_000_00)
    crypto.rebuild_holdings(conn, account_id)
    # Coin prices live under the 'SYM-USD' pair, not the bare symbol.
    investments.record_price(conn, crypto.pair_symbol("ZZETH"), BUY_DATE, "1500.00")
    investments.record_price(conn, crypto.pair_symbol("ZZETH"), AS_OF, "2000.00")
    return dict(seeded, wallet=account_id)


def test_crypto_account_is_valued_by_the_crypto_engine_in_the_accounts_ring(conn, wallet):
    """scope_account_ids('investments') is INVESTMENT_LIKE_TYPES, which includes
    crypto, so the wallet was always IN scope -- but the ring valued it with
    investments.account_valuation, which reads the bank transfer legs alone and
    reports the sleeve as large NEGATIVE cash. The wedge then fell through the
    `total <= 0` guard and the account simply vanished."""
    ids = [wallet["brokerage"], wallet["ira"], wallet["wallet"]]
    expected = crypto.account_valuation(conn, wallet["wallet"], AS_OF).total
    # $2,000 cash left in the sleeve plus 2 ZZETH at $2,000.
    assert expected == 6_000_00

    slices = dash.ring_slices(conn, dash.MODE_ACCOUNTS, AS_OF, account_ids=ids)
    by_key = {key: value for key, _label, value in slices}
    assert str(wallet["wallet"]) in by_key
    assert by_key[str(wallet["wallet"])] == expected > 0
    assert dict((key, label) for key, label, _v in slices)[str(wallet["wallet"])] \
        == "Test Exchange"

    # The guard that the fix is real: the brokerage engine gives a DIFFERENT,
    # non-positive answer for this account, which is why the wedge was missing.
    assert investments.account_valuation(conn, wallet["wallet"], AS_OF).total <= 0

    # The wallet is in the DEFAULT scope too -- the user does not have to pick it.
    assert str(wallet["wallet"]) in {k for k, _l, _v in
                                     dash.ring_slices(conn, dash.MODE_ACCOUNTS, AS_OF)}


def test_crypto_coin_keeps_its_wedge_in_securities_mode(conn, wallet):
    """Securities mode already dispatched correctly (portfolio.allocation); this
    pins that the accounts-mode fix did not disturb it."""
    ids = [wallet["brokerage"], wallet["ira"], wallet["wallet"]]
    keys = {key for key, _label, _value in
            dash.ring_slices(conn, dash.MODE_SECURITIES, AS_OF, account_ids=ids)}
    assert keys - {dash.CASH_KEY} == {"ZZAA", "ZZBB", "ZZCC", "ZZETH"}


def test_an_empty_crypto_account_draws_no_wedge(conn, wallet):
    """A zero-valued account must not produce a bogus wedge -- neither a zero one
    nor (the old failure mode) a negative one drawn as its absolute value."""
    empty = crypto.create_account(conn, "Test Empty Wallet",
                                  kind=crypto.CRYPTO_KIND_EXCHANGE,
                                  opening_date=OPEN_DATE)
    assert crypto.account_valuation(conn, empty, AS_OF).total == 0
    keys = {key for key, _l, _v in
            dash.ring_slices(conn, dash.MODE_ACCOUNTS, AS_OF,
                             account_ids=[wallet["wallet"], empty])}
    assert keys == {str(wallet["wallet"])}


def test_center_line_and_value_chart_see_the_wallet(conn, wallet):
    """_value_at feeds the center line, the value chart and the projection's
    starting point, so all three carried the same bypass."""
    ids = [wallet["brokerage"], wallet["ira"], wallet["wallet"]]
    with_wallet = dash.center_line(conn, AS_OF, account_ids=ids).total
    without = dash.center_line(conn, AS_OF,
                               account_ids=[wallet["brokerage"], wallet["ira"]]).total
    assert with_wallet - without == crypto.account_valuation(
        conn, wallet["wallet"], AS_OF).total

    # The chart's last sample is the same number the center line states.
    points = dash.value_series(conn, 1, as_of=AS_OF, account_ids=ids)
    assert points[-1][1] == with_wallet


# --- the accounts gear only offers accounts this page can actually draw ------
@pytest.fixture
def mixed_roster(conn, wallet):
    """The wallet ledger plus a savings account, so the roster spans both
    investment-like types (brokerage, IRA, crypto) and two plain cash accounts
    (checking from ``seeded``, savings here)."""
    savings = ledger.create_account(conn, "Test Savings", "savings",
                                    opening_balance=7_500_00,
                                    opening_date=OPEN_DATE)
    return dict(wallet, savings=savings)


def _picker_items(bar):
    """(name, id, checked) for every row the account picker is offering."""
    from PyQt5.QtCore import Qt
    lst = bar.account_list
    return [(lst.item(i).text(), int(lst.item(i).data(Qt.UserRole)),
             lst.item(i).checkState() == Qt.Checked)
            for i in range(lst.count())]


def test_the_gear_offers_only_investment_accounts_all_of_them_checked(
        qapp, conn, mixed_roster):
    """The user's report: "default the customization picker to have only
    selected the investment accounts."

    The gear reused CustomizeDialog as-is, which lists the WHOLE roster
    all-checked -- and set_account_scope then dropped every non-investment id on
    the way back in, so the checking and savings ticks were doing nothing. The
    dialog is now restricted to ledger.INVESTMENT_LIKE_TYPES, crypto included.
    """
    page = dash.InvestmentDashboardPage(conn, as_of=AS_OF)
    try:
        offered = _picker_items(page.customize_dialog.filters)
        assert {name for name, _id, _c in offered} == {
            "Test Brokerage", "Test IRA", "Test Exchange"}
        assert {acct_id for _n, acct_id, _c in offered} == {
            mixed_roster["brokerage"], mixed_roster["ira"], mixed_roster["wallet"]}
        # Every one of them -- the crypto wallet included -- arrives ticked.
        assert all(checked for _n, _i, checked in offered)
        # All-checked still means "no filter", which for this page reads as
        # "every investment account": the same set the picker is showing, so a
        # brokerage opened tomorrow joins rather than being pinned out.
        assert page.customize_dialog.filters.selected_account_ids() is None
        assert page.account_scope() is None
    finally:
        page.deleteLater()


def test_a_plain_report_customize_dialog_still_offers_every_account(
        qapp, conn, mixed_roster):
    """Regression: the restriction is OPT-IN. A report window that asks for no
    account_types keeps the historical all-accounts, all-checked picker."""
    from mammon.ui.report_filters import CustomizeDialog
    dlg = CustomizeDialog(conn, OPEN_DATE, AS_OF, show_accounts=True)
    try:
        offered = _picker_items(dlg.filters)
        names = {name for name, _id, _c in offered}
        assert {"Test Checking", "Test Savings"} <= names
        assert {"Test Brokerage", "Test IRA", "Test Exchange"} <= names
        assert all(checked for _n, _i, checked in offered)
        # All-checked with nothing hidden still means "no filter".
        assert dlg.filters.selected_account_ids() is None
    finally:
        dlg.deleteLater()


# --- the four corner launchers: themed graphics under prominent titles ------
# Reported: "Replace the Explore Rebalancing box with a graphic showing two pie
# charts with different proportions of the same colors in each with an arrow
# between them and the title Explore Rebalancing. Similarly, replace the other
# boxes with themed graphics and prominant titles." The risk in painting a
# QPushButton yourself is that it stops being a button, so these tests pin the
# three things the graphic must not cost: the caption, the hit test, and the
# action -- plus a headless render in BOTH palettes, because a glyph drawn from
# hex literals is invisible in one of them.
def _render(widget):
    """Paint ``widget`` onto a pixmap the way a real expose would. Raises
    whatever paintEvent raises -- Qt swallows nothing here."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPixmap
    pm = QPixmap(max(1, widget.width()), max(1, widget.height()))
    pm.fill(Qt.white)
    widget.render(pm)
    assert not pm.isNull()
    return pm


def test_every_corner_launcher_has_its_own_glyph_and_keeps_its_caption(page):
    assert set(dash.CORNER_GLYPHS) == set(dash.CORNER_NAMES)
    # Four DIFFERENT pictures: the whole complaint was that they looked alike.
    assert len(set(dash.CORNER_GLYPHS.values())) == len(dash.CORNER_NAMES)
    assert dash.CORNER_LABELS["cornerBottomRight"] == "Explore Rebalancing"
    for name in dash.CORNER_NAMES:
        btn = page.placeholders[name]
        assert isinstance(btn, dash.CornerButton)
        assert btn.text() == dash.CORNER_LABELS[name]
        assert btn.glyph == dash.CORNER_GLYPHS[name]
        assert callable(getattr(btn, "_paint_" + btn.glyph))


def test_the_rebalancing_glyph_is_two_mixes_of_the_same_colors():
    """The user's words: two pies, "different proportions of the same colors"."""
    assert dash.CORNER_GLYPHS["cornerBottomRight"] == "rebalance"
    for mix in (dash.CORNER_PIE_DRIFTED, dash.CORNER_PIE_TARGET):
        assert len(mix) == dash.CORNER_WEDGE_COUNT
        assert abs(sum(mix) - 1.0) < 1e-9
    assert dash.CORNER_PIE_DRIFTED != dash.CORNER_PIE_TARGET
    # One color list, handed to both pies, and it is the RING's list.
    from mammon.ui import charts
    wedges = dash.corner_colors()["wedges"]
    assert wedges == charts.wedge_colors(
        [str(i) for i in range(dash.CORNER_WEDGE_COUNT)])


def test_the_rebalancing_glyph_actually_paints_those_colors(qapp):
    """Not just constants: the pies are on the pixels. Sampling the render is
    the only check that survives a paintEvent that returns early."""
    btn = dash.CornerButton("Explore Rebalancing", glyph="rebalance")
    try:
        btn.resize(240, 160)
        btn.show()
        qapp.processEvents()
        image = _render(btn).toImage()
        seen = {image.pixelColor(x, y).name()
                for y in range(0, image.height(), 2)
                for x in range(0, image.width(), 2)}
        wedges = set(dash.corner_colors()["wedges"])
        assert len(seen & wedges) >= 3, "the pies did not paint"
        btn.hide()
    finally:
        btn.deleteLater()


def test_the_launcher_title_is_bigger_and_bolder_than_the_body_font(qapp):
    """"Prominant titles". The fit walks a FINITE list of sizes, so it also
    cannot spin on a caption that never fits."""
    btn = dash.CornerButton("Capital Gains and Taxes", glyph="tax")
    try:
        btn.resize(200, 150)
        font, height = btn._title_font(188, 80)
        assert font.bold()
        if btn.font().pointSizeF() > 0:
            assert font.pointSizeF() > btn.font().pointSizeF()
        assert 0 < height <= 80
        # A hopeless box caps the block rather than looping or overflowing.
        _font, tight = btn._title_font(24, 12)
        assert 0 < tight <= 12
    finally:
        btn.deleteLater()


def test_each_launcher_is_still_hittable_and_still_fires_its_own_action(
        page, qapp):
    """The graphic must cost nothing: childAt() is what Qt does on a press, and
    the click must still reach the method the corner has always opened."""
    from PyQt5.QtCore import QPoint
    _shown(page, qapp)
    fired = []
    for name in dash.CORNER_NAMES:
        method = dash.CORNER_ACTIONS[name]
        setattr(page, method, lambda m=method: fired.append(m))
    for name in dash.CORNER_NAMES:
        btn = page.placeholders[name]
        assert btn.isVisible()
        point = btn.mapTo(page, QPoint(btn.width() // 2, btn.height() // 2))
        hit = page.childAt(point)
        assert hit is btn or btn.isAncestorOf(hit), (
            f"{name}: a click at {point} lands on "
            f"{(hit.objectName() or type(hit).__name__) if hit else 'nothing'}")
        btn.click()
    assert fired == [dash.CORNER_ACTIONS[n] for n in dash.CORNER_NAMES]
    page.hide()


def test_the_corner_graphics_paint_in_both_palettes(page, qapp, monkeypatch):
    """Reported before, about the plots: "poor contrast in both dark and light
    mode". Every color is a palette NAME resolved at paint time, so the same
    glyph has to render under either theme without raising."""
    from mammon.ui import style
    _shown(page, qapp)
    for theme in ("light", "dark"):
        monkeypatch.setattr(style, "theme", lambda t=theme: t)
        col = dash.corner_colors()
        pal = style.palette_for(theme)
        assert col["title"] == pal["text"]
        assert col["muted"] == pal["muted"]
        assert col["accent"] == pal["blue"]
        assert col["negative"] == pal["negative"]
        assert len(col["wedges"]) == dash.CORNER_WEDGE_COUNT
        for name in dash.CORNER_NAMES:
            _render(page.placeholders[name])
        _render(page)               # and the whole page, glyphs included
    page.hide()


def test_a_tiny_corner_keeps_the_title_and_drops_the_graphic(qapp):
    """The corner boxes shrink with the window down to CORNER_MIN_SIZE. A pie
    that small is a smudge, and an unreadable caption is the worse outcome."""
    btn = dash.CornerButton("Set Asset Categories", glyph="categories")
    try:
        btn.resize(dash.CORNER_MIN_SIZE, dash.CORNER_MIN_SIZE)
        _render(btn)                # no exception, no division by zero
        btn.resize(0, 0)
        _render(btn)
    finally:
        btn.deleteLater()


# ---- which toggle is ON is VISIBLE -----------------------------------------
# Both pairs of controls were checkable QPushButtons with no ``:checked`` rule
# anywhere, and the app's own QSS paints QPushButton -- which switches Qt off
# the native style, so the sunken checked chrome was never drawn. On screen the
# active mode and a live What If looked exactly like the inactive ones.


def _highlighted(page):
    """The set of dashboard toggles currently drawn highlighted."""
    names = set()
    for mode, btn in page.mode_buttons.items():
        if dash.is_toggle_highlighted(btn):
            names.add(mode)
    if dash.is_toggle_highlighted(page.what_if_bar.button):
        names.add("what_if")
    return names


def test_the_active_mode_button_is_the_highlighted_one(page):
    assert _highlighted(page) == {dash.MODE_ACCOUNTS}     # the opening mode

    page.mode_buttons[dash.MODE_SECURITIES].click()       # by hand
    assert page.mode() == dash.MODE_SECURITIES
    assert _highlighted(page) == {dash.MODE_SECURITIES}

    page.mode_buttons[dash.MODE_ACCOUNTS].click()
    assert page.mode() == dash.MODE_ACCOUNTS
    assert _highlighted(page) == {dash.MODE_ACCOUNTS}


def test_a_programmatic_mode_switch_moves_the_highlight_too(page):
    """Nothing clicked: set_mode() is what a saved view or a scope change
    calls, and the highlight has to follow the state, not the mouse."""
    page.set_mode(dash.MODE_SECURITIES)
    assert _highlighted(page) == {dash.MODE_SECURITIES}
    assert page.mode_buttons[dash.MODE_SECURITIES].isChecked() is True
    assert page.mode_buttons[dash.MODE_ACCOUNTS].isChecked() is False

    page.set_mode(dash.MODE_ACCOUNTS)
    assert _highlighted(page) == {dash.MODE_ACCOUNTS}

    page.set_mode(dash.MODE_ACCOUNTS)          # a no-op switch changes nothing
    assert _highlighted(page) == {dash.MODE_ACCOUNTS}


def test_exactly_one_mode_is_ever_highlighted(page, seeded):
    """A refresh, a wedge filter and clearing it all leave the mode alone --
    and must leave the pair showing one lit button, never two and never none."""
    for step in (lambda: page.refresh(),
                 lambda: page.set_account_scope([seeded["brokerage"]]),
                 lambda: page.set_mode(dash.MODE_SECURITIES),
                 lambda: page.refresh(),
                 lambda: page.clear_filter(),
                 lambda: page.set_account_scope(None)):
        step()
        lit = {m for m, b in page.mode_buttons.items()
               if dash.is_toggle_highlighted(b)}
        assert lit == {page.mode()}


def test_what_if_is_highlighted_only_while_it_is_on(page):
    assert page.what_if_active() is False
    assert "what_if" not in _highlighted(page)

    page.set_what_if(True)                      # programmatic
    assert page.what_if_active() is True
    assert "what_if" in _highlighted(page)
    assert page.what_if_bar.button.isChecked() is True

    page.set_what_if(False)
    assert page.what_if_active() is False
    assert "what_if" not in _highlighted(page)

    page.what_if_bar.button.click()             # by hand, on
    assert page.what_if_active() is True
    assert "what_if" in _highlighted(page)

    page.what_if_bar.button.click()             # by hand, off
    assert page.what_if_active() is False
    assert "what_if" not in _highlighted(page)


def test_what_if_highlight_survives_a_reset_and_stays_enabled(page, seeded):
    """Reset puts the measured numbers back; it does not turn What If off, so
    the button stays lit. And nothing here may disable it -- the disable path
    was removed deliberately."""
    page.set_what_if(True)
    page.reset_what_if()
    assert page.what_if_active() is True
    assert "what_if" in _highlighted(page)
    assert page.what_if_bar.button.isEnabled() is True

    page.set_account_scope([seeded["ira"]])
    page.refresh()
    assert page.what_if_bar.button.isEnabled() is True
    assert dash.is_toggle_highlighted(page.what_if_bar.button) is page.what_if_active()


def test_the_mode_and_what_if_highlights_are_independent(page):
    page.set_mode(dash.MODE_SECURITIES)
    page.set_what_if(True)
    assert _highlighted(page) == {dash.MODE_SECURITIES, "what_if"}

    page.set_mode(dash.MODE_ACCOUNTS)
    assert _highlighted(page) == {dash.MODE_ACCOUNTS, "what_if"}

    page.set_what_if(False)
    assert _highlighted(page) == {dash.MODE_ACCOUNTS}


def test_the_highlight_reads_in_both_light_and_dark(page):
    """The complaint the styling answers is contrast, and the two themes' accents
    sit on opposite sides of light: a fixed label color would be unreadable in
    one of them. Each theme gets a fill AND a label color, and they differ."""
    from PyQt5.QtGui import QColor

    seen = []
    for theme in ("light", "dark"):
        qss = dash.toggle_qss(theme)
        assert f'[{dash.TOGGLE_ACTIVE_PROP}="on"]' in qss
        accent = dash.style.palette_for(theme)["blue"]
        assert accent in qss
        label = dash._on_text_for(accent)
        assert label in qss
        # a real difference in lightness between fill and label, both ways
        gap = abs(QColor(accent).lightness() - QColor(label).lightness())
        assert gap > 90, (theme, gap)
        seen.append((accent, label))
    assert seen[0] != seen[1]                  # the themes are not the same look


def test_the_mode_buttons_carry_the_highlight_stylesheet(page):
    """The property is only half of it: without the widget-local QSS naming that
    property, setting it paints nothing at all."""
    for btn in list(page.mode_buttons.values()) + [page.what_if_bar.button]:
        assert f'[{dash.TOGGLE_ACTIVE_PROP}="on"]' in btn.styleSheet()
        assert btn.isCheckable() is True

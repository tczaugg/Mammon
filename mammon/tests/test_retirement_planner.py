"""The Retirement Planner page: the chart the plan produces, end to end.

Every figure here is synthetic - an "ANON" household born in 1960 with invented
earnings, and two accounts funded by one deposit each. Nothing in this file comes
from a real ledger, and no real name, account number or balance belongs in it.

The life-cycle test is the point: seed a tax-deferred account, a Roth account and
a person, store a withdrawal plan and a conversion plan, open the page, and check
that what was drawn is what was planned - three distinct stacked series with the
right per-year totals, and a fund-value line behind them.

The Roth section is exercised through the same gestures the user makes, minus the
mouse: :meth:`RothChart.on_click` is a plain method, so a stand-in event carrying
the four attributes matplotlib would have supplied stands in for a click on a
bracket line or a right-click on a bar. Nothing here opens a modal - the only
dialog left on the page is Social Security, and it is run through the
``_run_dialog`` seam.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import datetime as _dt
import types
from decimal import ROUND_HALF_UP, Decimal

import pytest
from PyQt5.QtCore import Qt

from mammon import forecast, ledger, rebalance, retirement
from mammon.tests import fresh_db
from mammon.ui import retirement_planner as rp
from mammon.ui.models import fmt_money
from mammon.ui.retirement_planner import (
    SERIES_CONVERSION,
    SERIES_DEFERRED,
    SERIES_ROTH,
    SERIES_SOCIAL_SECURITY,
    SERIES_TAXABLE,
    RetirementPlannerPage,
)

TODAY = _dt.date(2026, 9, 23)
BIRTH_YEAR = 1960
BIRTH_MONTH = 6
CLAIM_AGE_MONTHS = 67 * 12          # the household's own choice, stored on file

IRA_BALANCE = 500_000_00
ROTH_BALANCE = 200_000_00

IRA_DRAW_2030 = 40_000_00
IRA_DRAW_2031 = 42_000_00
ROTH_DRAW_2031 = 8_000_00
CONVERSION_2028 = 25_000_00


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "planner.db")
    yield c
    c.close()


@pytest.fixture
def plan(conn):
    """One tax-deferred account, one Roth, one person, and a plan for both."""
    ira = ledger.create_account(conn, "ANON Rollover IRA", "investment")
    roth = ledger.create_account(conn, "ANON Roth IRA", "investment")
    rebalance.set_account_treatment(conn, ira, "deferred")
    rebalance.set_account_treatment(conn, roth, "roth")
    # A single opening deposit each: enough for the accounts to have a value,
    # which is all the fund line needs.
    ledger.add_transaction(conn, ira, "2024-01-05", IRA_BALANCE)
    ledger.add_transaction(conn, roth, "2024-01-05", ROTH_BALANCE)

    person = retirement.add_person(
        conn, "ANON Saver", "self", birth_year=BIRTH_YEAR, birth_month=BIRTH_MONTH,
        planned_claim_age_months=CLAIM_AGE_MONTHS)
    for year in range(2015, 2026):
        retirement.set_earnings(conn, person, year, 80_000_00)

    retirement.set_withdrawal(conn, ira, 2030, IRA_DRAW_2030)
    retirement.set_withdrawal(conn, ira, 2031, IRA_DRAW_2031)
    retirement.set_withdrawal(conn, roth, 2031, ROTH_DRAW_2031)
    retirement.set_conversion(conn, ira, roth, 2028, CONVERSION_2028)
    return {"ira": ira, "roth": roth, "person": person}


def _mark_current_employer_plan(conn, account_id):
    """Tick "Plan at my current employer" in Account Details and save it.

    Through the real dialog rather than an UPDATE, because the point of the
    checkbox is that the planner's RMD behavior follows what the user ticks.
    The dialog is never exec_()-ed: under the offscreen platform a modal blocks
    forever, and its callers persist through ``dlg.values()`` anyway.
    """
    from mammon.ui.widgets import AccountDetailsDialog

    dlg = AccountDetailsDialog(ledger.get_account(conn, account_id), conn=conn)
    try:
        dlg.current_employer_plan.setChecked(True)
        dlg.accept()
        ledger.update_account(conn, account_id, **dlg.values())
    finally:
        dlg.setParent(None)


@pytest.fixture
def page(qapp, conn, plan):
    p = RetirementPlannerPage(conn, today=TODAY)
    p.refresh()
    yield p
    # Run whatever the test left queued (deferred rebuilds, the re-apply after
    # a conversion) while its database is still open. Left queued, it fired in
    # the NEXT test on a closed connection, and an exception in a Qt slot with
    # no sys.excepthook aborts the whole test process - a "crashed worker"
    # with no traceback. The app installs crashlog's hook, so it only logs.
    for _ in range(3):
        qapp.processEvents()
    p.deleteLater()
    qapp.processEvents()


def _dispose(page, qapp=None):
    """The ``page`` fixture's teardown, for a test that builds its own page:
    run what it left queued while its database is still open, then delete it.
    Skipped, a queued re-apply fired in the NEXT test on a closed connection
    and crashed the worker with no traceback."""
    from PyQt5.QtWidgets import QApplication
    app = qapp or QApplication.instance()
    for _ in range(3):
        app.processEvents()
    page.deleteLater()
    app.processEvents()


def _click(chart, year, dollars, button=1):
    """Stand in for the event a real mouse would have handed matplotlib.

    ``on_click`` reads exactly these four attributes with ``getattr``, which is
    what lets the Roth section's gestures be tested offscreen.
    """
    chart.on_click(types.SimpleNamespace(
        button=button, xdata=float(year), ydata=float(dollars), inaxes=chart.axes))


def _set_assumption(page, field, text):
    """Type one planner assumption into the Social Security dialog, where the
    assumptions live now, and close it (the page re-applies on close)."""
    def edit(dlg):
        getattr(dlg, field).setText(text)
        dlg._save_assumptions()
        dlg.setParent(None)
    page._run_dialog = edit
    page.open_social_security()


def _row(page, year):
    for row in page.income_rows():
        if row.year == year:
            return row
    raise AssertionError(f"no bar for {year}: {[r.year for r in page.income_rows()]}")


# ---------------------------------------------------------------------------
# the life cycle: seed a plan, open the page, read back what was drawn
# ---------------------------------------------------------------------------
def test_page_draws_the_planned_income_as_three_distinct_stacked_series(page, plan):
    chart = page.income_chart
    series = chart.series_cents()
    assert list(series) == [SERIES_SOCIAL_SECURITY, rp.SERIES_OTHER,
                            SERIES_DEFERRED, rp.SERIES_TAXABLE_DRAW, SERIES_ROTH]
    assert set(chart.bars) == set(series)

    # Distinct colors, or the stack cannot be read at all.
    colors = [chart.bars[label].patches[0].get_facecolor()
              for label in (SERIES_SOCIAL_SECURITY, SERIES_DEFERRED, SERIES_ROTH)]
    assert len(set(colors)) == 3

    years = [r.year for r in page.income_rows()]
    assert years[0] == TODAY.year
    assert years[-1] == BIRTH_YEAR + rp.DEFAULT_TERMINAL_AGE     # the 90 case
    for label in (SERIES_SOCIAL_SECURITY, SERIES_DEFERRED, SERIES_ROTH):
        assert len(chart.bars[label].patches) == len(years)

    # The draws land in the year and the account they were planned for, and
    # nowhere else.
    assert _row(page, 2030).deferred_draw_cents == IRA_DRAW_2030
    assert _row(page, 2030).roth_draw_cents == 0
    assert _row(page, 2031).deferred_draw_cents == IRA_DRAW_2031
    assert _row(page, 2031).roth_draw_cents == ROTH_DRAW_2031
    assert _row(page, 2029).deferred_draw_cents == 0


def test_social_security_is_prorated_from_the_stored_earnings(page, conn, plan):
    monthly = retirement.monthly_benefit(
        retirement.earnings_map(conn, plan["person"]), BIRTH_YEAR, CLAIM_AGE_MONTHS)
    assert monthly > 0

    # Age 67 is attained in June 2027, so that year pays seven months and every
    # later year pays twelve. Before the claim, nothing. Each year after this one
    # is raised by the COLA (default: the Trustees' long-range CPI assumption).
    cola = retirement.get_cola_pct(conn)
    assert cola == retirement.DEFAULT_COLA_PCT

    def raised(cents, year):
        return retirement.household_amount_cents(cents, cola, year - TODAY.year)

    assert _row(page, 2026).social_security_cents == 0
    assert _row(page, 2027).social_security_cents == raised(monthly * 7, 2027)
    assert _row(page, 2031).social_security_cents == raised(monthly * 12, 2031)

    # And the stacked total for a year is those three series, nothing else.
    row = _row(page, 2031)
    assert row.income_cents == raised(monthly * 12, 2031) + IRA_DRAW_2031 \
        + ROTH_DRAW_2031


def test_bar_heights_are_the_planned_dollars(page):
    chart = page.income_chart
    years = [r.year for r in page.income_rows()]
    index = years.index(2031)
    row = _row(page, 2031)
    heights = {label: chart.bars[label].patches[index].get_height()
               for label in chart.bars}
    assert heights[SERIES_DEFERRED] == pytest.approx(IRA_DRAW_2031 / 100.0)
    assert heights[SERIES_ROTH] == pytest.approx(ROTH_DRAW_2031 / 100.0)
    assert sum(heights.values()) == pytest.approx(row.income_cents / 100.0)
    # Stacked, not overlaid: the Roth slice starts where the others end.
    roth_bottom = chart.bars[SERIES_ROTH].patches[index].get_y()
    assert roth_bottom == pytest.approx(
        (row.social_security_cents + row.deferred_draw_cents) / 100.0)


def test_fund_value_line_is_drawn_behind_the_bars(page, plan):
    chart = page.income_chart
    assert chart.fund_line is not None
    values = list(chart.fund_line.get_ydata())
    assert len(values) == len(page.income_rows())
    # Point 0 is the pool as it stands today, both accounts together.
    assert values[0] == pytest.approx((IRA_BALANCE + ROTH_BALANCE) / 100.0)
    # On its own axis, drawn under the bars.
    assert chart.fund_axes is not None and chart.fund_axes is not chart.bars[
        SERIES_ROTH].patches[0].axes
    assert chart.fund_line.get_zorder() < chart.bars[SERIES_ROTH].patches[0].get_zorder()


def test_conversions_are_charted_separately_and_never_counted_as_income(page):
    assert _row(page, 2028).conversion_cents == CONVERSION_2028
    # Taxable, but not spendable: it is not in the stacked total.
    assert _row(page, 2028).income_cents == _row(page, 2028).social_security_cents
    assert _row(page, 2028).taxable_cents == CONVERSION_2028

    # But it IS in the Roth section's bars, stacked on top of that year's base:
    # not income to spend, income to be taxed.
    chart = page.roth_chart
    assert set(chart.bars) == {SERIES_TAXABLE, rp.SERIES_TAX_DRAW, SERIES_CONVERSION}
    years = [r.year for r in chart.rows]
    index = years.index(2028)
    base = chart.base_cents[2028]
    assert chart.bars[SERIES_TAXABLE].patches[index].get_height() == pytest.approx(
        base / 100.0)
    converted = chart.bars[SERIES_CONVERSION].patches[index]
    # What the conversion adds to TAXABLE income: itself, plus any Social
    # Security it makes taxable (IRC 86), less any deduction the base left
    # unused.
    added = page.taxable_with(2028, CONVERSION_2028) - base
    assert added > 0
    assert converted.get_height() == pytest.approx(added / 100.0)
    assert converted.get_y() == pytest.approx(base / 100.0)
    assert chart.bars[SERIES_CONVERSION].patches[years.index(2029)].get_height() \
        == pytest.approx(0.0)


def test_the_social_security_dialog_is_the_only_one_left_and_does_not_block(page):
    from mammon.ui.social_security_dialog import SocialSecurityDialog

    ran = []
    page._run_dialog = lambda dlg: ran.append(dlg)    # the headless seam

    page.ss_button.click()
    assert len(ran) == 1
    assert isinstance(ran[0], SocialSecurityDialog)
    # The Roth work happens on the page: there is no dialog to open for it.
    assert not hasattr(page, "conversions_button")
    assert not hasattr(page, "open_roth_conversions")
    for dlg in ran:
        dlg.setParent(None)
        dlg.deleteLater()


def test_a_dialog_change_is_on_the_chart_when_it_closes(page, conn, plan):
    assert _row(page, 2032).deferred_draw_cents == 0

    def edit_and_close(dlg):
        retirement.set_withdrawal(conn, plan["ira"], 2032, 11_000_00)
        dlg.changed.emit()
        dlg.setParent(None)

    page._run_dialog = edit_and_close
    page.open_social_security()
    assert _row(page, 2032).deferred_draw_cents == 11_000_00


# ---------------------------------------------------------------------------
# the shape of the projection
# ---------------------------------------------------------------------------
def test_terminal_age_is_any_year_the_user_picks(page):
    """Reported: the plan-through age came only in decades. Any year 60-120."""
    assert (page.terminal_age.minimum(), page.terminal_age.maximum()) == \
        (rp.MIN_TERMINAL_AGE, rp.MAX_TERMINAL_AGE) == (60, 120)
    assert page.terminal_age.value() == rp.DEFAULT_TERMINAL_AGE

    page.terminal_age.setValue(87)
    assert page.income_rows()[-1].year == BIRTH_YEAR + 87
    page.terminal_age.setValue(80)
    assert page.income_rows()[-1].year == BIRTH_YEAR + 80
    # The plan is still written through 100 whatever is shown...
    assert page.max_horizon()[-1] == BIRTH_YEAR + rp.PLAN_WRITTEN_THROUGH_AGE
    # ...or through the chosen age when that is later.
    page.terminal_age.setValue(105)
    assert page.max_horizon()[-1] == BIRTH_YEAR + 105
    # A plan year past the chosen case is still shown rather than silently lost.
    assert any(line.startswith("Longevity is shown as a case")
               for line in page.assumption_lines())


def test_a_planned_conversion_past_the_case_is_still_drawn(qapp, conn, plan):
    retirement.set_conversion(
        conn, plan["ira"], plan["roth"], BIRTH_YEAR + 95, 1_000_00)
    p = RetirementPlannerPage(conn, today=TODAY)
    try:
        p.refresh()                       # default case is 90
        assert p.income_rows()[-1].year == BIRTH_YEAR + 95
    finally:
        p.deleteLater()


def test_a_withdrawal_past_the_case_does_not_stretch_the_horizon(qapp, conn, plan):
    """The counterpart to the conversion above, and deliberately not symmetric.

    Withdrawals are seeded out to the longest longevity case the moment a birth
    year is on file, so a horizon that stretched to cover them would always
    stretch to age 100 and the terminal-age case would stop meaning anything.
    A conversion is a discrete event somebody scheduled, so dropping one would
    look like the plan had lost it.
    """
    retirement.set_withdrawal(conn, plan["ira"], BIRTH_YEAR + 95, 1_000_00)
    p = RetirementPlannerPage(conn, today=TODAY)
    try:
        p.refresh()                       # default case is 90
        assert p.income_rows()[-1].year == BIRTH_YEAR + 90
    finally:
        p.deleteLater()


def test_empty_ledger_says_so_instead_of_drawing_nothing(qapp, tmp_path):
    c = fresh_db(tmp_path / "empty.db")
    p = RetirementPlannerPage(c, today=TODAY)
    try:
        p.refresh()
        assert p.income_chart.bars == {}
        assert p.income_chart.fund_line is None
        # The Roth chart's empty state leaves NO axes, which is exactly what
        # makes a click on an empty chart do nothing.
        assert p.roth_chart.bars == {}
        assert p.roth_chart.axes is None
        _click(p.roth_chart, 2030, 10_000.0)          # no axes, no crash, no write
        assert retirement.list_conversions(c, 2030) == []
        assert any("birth year" in line for line in p.assumption_lines())
        assert len(p.income_rows()) == rp.FALLBACK_HORIZON_YEARS
    finally:
        p.deleteLater()
        c.close()


def test_the_captions_state_a_mechanism_and_not_a_citation(page):
    """What the chart shows, not which table it came from (SRD 5.8o)."""
    income = page.income_caption.text()
    assert "where each dollar comes from" in income
    assert "Tables:" not in income
    roth = page.roth_caption.text()
    assert roth.startswith("Taxable income")               # computed, not entered
    assert "Click a line" in roth and "\n" not in roth     # one line (reported)
    assert "Tables:" not in roth
    # The figures on their own line, label: value in fixed widths, so a changed
    # number keeps its place (reported).
    figures = page.roth_figures.text()
    for label in ("Converted:", "Federal tax:", "State tax:", "IRMAA/ACA:",
                  "Tax on IRAs left:", "Total tax:"):
        assert label in figures
    import re
    values = re.findall(r": ( *\$[\d,]+)", figures)
    assert len(values) == 6 and all(len(v) == 11 for v in values)
    assert income.index("Projected income across the plan") >         income.index("where each dollar comes from")


def test_the_faq_button_opens_the_faq_without_a_modal(page):
    faq = page.open_faq()
    try:
        assert faq.isVisible()
        assert page.open_faq() is faq        # one window, re-raised
        assert "Retirement FAQ" in faq.text()
    finally:
        faq.close()
        faq.deleteLater()


def test_no_fund_line_when_the_mix_cannot_be_measured(qapp, conn, plan, monkeypatch):
    # The seam every projection on the page reads its weights through.
    monkeypatch.setattr(rp, "projection_mix", lambda *a, **k: None)
    p = RetirementPlannerPage(conn, today=TODAY)
    try:
        p.refresh()
        assert p.income_chart.fund_line is None
        assert any("No fund line is drawn" in line for line in p.assumption_lines())
    finally:
        p.deleteLater()


def test_series_colors_differ_in_both_palettes(qapp, monkeypatch):
    from mammon.ui import style

    labels = (SERIES_SOCIAL_SECURITY, SERIES_DEFERRED, SERIES_ROTH,
              rp.SERIES_CONVERSION, rp.SERIES_FUND)
    seen = {}
    for theme in ("light", "dark"):
        monkeypatch.setattr(style, "theme", lambda t=theme: t)
        colors = rp.series_colors()
        series = [colors[label] for label in labels]
        assert len(set(series)) == 5, theme
        assert all(isinstance(c, str) and c for c in series)
        seen[theme] = series
    # A print render forces the light palette whatever the screen is set to, so
    # an exported figure can never come out dark-on-white.
    monkeypatch.setattr(style, "theme", lambda: "dark")
    assert [rp.series_colors(for_print=True)[label] for label in labels] \
        == seen["light"]


def test_the_page_never_counsels(page):
    """No amount to draw, no year to convert in, no age to claim at."""
    text = " ".join(page.assumption_lines()).lower()
    for word in ("should", "recommend", "we suggest", "best"):
        assert word not in text


# ---------------------------------------------------------------------------
# the Roth section: the two gestures, and the table behind them
# ---------------------------------------------------------------------------
def _year_item(page, year):
    """A year's line in the schedule - the one carrying its household income."""
    item = page.schedule.year_item(year)
    if item is None:
        raise AssertionError(f"{year} is not in the schedule")
    return item


def _edge_above(chart, year):
    """The lowest drawn bracket line with room under it in this year, as that
    line stands in ``year`` (the tops are indexed every year)."""
    base = chart.base_cents[year]
    for edge, _line in chart.lines:
        if chart.edge_in(edge, year) > base:
            return chart.edge_in(edge, year)
    raise AssertionError(f"no bracket line above {base} cents")


def test_the_base_income_series_is_seeded_from_the_plans_own_flows(page, conn):
    """The user edits a POPULATED series, not a column of zeros."""
    status = page.filing_status()

    def after_deduction(year, gross):
        # The deduction is INDEXED to the year, like the bracket tops.
        return max(0, gross - retirement.standard_deduction_in(
            status, rp.deduction_conditions(conn, year, status), year,
            retirement.get_bracket_index_pct(conn)))

    row = _row(page, 2031)
    # Taxable income is what the brackets apply to: AFTER the standard deduction.
    expected = after_deduction(2031, retirement.gross_with_social_security_cents(
        row.deferred_draw_cents, row.social_security_cents, status))
    assert expected > 0
    assert retirement.get_taxable_income(conn, 2031) == expected
    assert page.roth_chart.base_cents[2031] == expected
    # The seed is the BASE only: the conversion is stacked on top at render time,
    # so it is not folded into the number a click measures its gap from.
    assert page.roth_chart.base_cents[2028] == after_deduction(
        2028, retirement.gross_with_social_security_cents(
            0, _row(page, 2028).social_security_cents, status))
    stored = retirement.list_taxable_income(conn)
    assert len(stored) == len(page.income_rows())
    assert {r["source"] for r in stored} == {"seeded"}   # nothing typed yet


def test_bracket_lines_are_drawn_for_the_filing_status_shown(page):
    chart = page.roth_chart
    edges = [e for e, _line in chart.lines]
    assert edges and edges == sorted(edges)
    ladder = [b.upper_cents for b in retirement.tax_brackets(page.filing_status())
              if b.upper_cents is not None]
    assert edges == ladder[:len(edges)]
    # Switching the status redraws against the other ladder, and stores nothing.
    at = [page.filing_combo.itemData(i) for i in range(page.filing_combo.count())]
    page.filing_combo.setCurrentIndex(at.index("joint"))
    assert page.filing_status() == "joint"
    joint = [e for e, _line in page.roth_chart.lines]
    assert joint != edges
    assert joint == [b.upper_cents for b in retirement.tax_brackets("joint")
                     if b.upper_cents is not None][:len(joint)]


def _rendered(canvas):
    """Draw the canvas for real and hand back its renderer.

    Every claim below is measured against what matplotlib actually laid out -
    a window extent in figure pixels - because "the label is covered" and "the
    tick label is off the edge" are not visible in the data the chart was built
    from. Offscreen Agg draws the same geometry a screen would.
    """
    canvas.draw()
    return canvas.figure.canvas.get_renderer()


def test_bracket_percentages_are_outside_the_plot_where_no_bar_can_cover_them(page):
    chart = page.roth_chart
    renderer = _rendered(chart)
    labels = [t for t in chart.axes.texts
              if t.get_text().startswith("top of ") and t.get_visible()]
    # Every line that shows in the plot is labeled where it leaves it: in the
    # right margin, or above the plot where it rises through the top (the plot
    # is scaled to the bars, not to the highest line - reported).
    assert labels and len(labels) <= len(chart.lines)

    plot = chart.axes.get_window_extent(renderer=renderer)
    figure = chart.figure.bbox
    bars = [patch for container in chart.bars.values() for patch in container.patches]
    assert bars
    for text in labels:
        box = text.get_window_extent(renderer=renderer)
        assert box.x0 >= plot.x1 or box.y0 >= plot.y1, (
            f"{text.get_text()!r} is inside the plot ({box} in {plot})")
        assert box.x1 <= figure.x1, (
            f"{text.get_text()!r} runs off the figure ({box.x1} > {figure.x1})")
        for patch in bars:
            assert not box.overlaps(patch.get_window_extent(renderer)), (
                f"{text.get_text()!r} is covered by a bar")


def test_the_fund_axis_labels_fit_in_the_margin_beside_the_plot(page):
    chart = page.income_chart
    renderer = _rendered(chart)
    twin = chart.fund_axes
    assert twin is not None
    figure = chart.figure.bbox

    def right_edges():
        drawn = [lab for lab in twin.get_yticklabels() if lab.get_text()]
        assert drawn
        for label in drawn + [twin.yaxis.label]:
            box = label.get_window_extent(renderer=renderer)
            assert box.x1 <= figure.x1, (
                f"{label.get_text()!r} is clipped on the right "
                f"({box.x1} > {figure.x1})")

    right_edges()
    # And at the width this axis really reaches: a seven-figure fund total is
    # the widest tick label the chart ever draws, and it is what the old margin
    # pushed off the edge of the figure.
    twin.set_ylim(0, 4_000_000.0)
    renderer = _rendered(chart)
    assert any("$4,000,000" == lab.get_text() for lab in twin.get_yticklabels())
    right_edges()

    # The room came from the left margin, which must still hold its own labels.
    left = [lab for lab in chart.axes.get_yticklabels() if lab.get_text()]
    assert left
    for label in left + [chart.axes.yaxis.label]:
        box = label.get_window_extent(renderer=renderer)
        assert box.x0 >= figure.x0, (
            f"{label.get_text()!r} is clipped on the left "
            f"({box.x0} < {figure.x0})")
    # Both charts are laid out by the same pair of constants, so their plot
    # areas stay aligned one above the other.
    assert (chart.axes.get_position().x0
            == page.roth_chart.axes.get_position().x0)


def test_a_click_on_a_bracket_line_converts_exactly_the_gap(page, conn, plan):
    chart = page.roth_chart
    base = chart.base_cents[2031]
    edge = _edge_above(chart, 2031)
    assert retirement.list_conversions(conn, 2031) == []

    _click(chart, 2031, edge / 100.0)

    total = sum(int(r["amount_cents"])
                for r in retirement.list_conversions(conn, 2031))
    assert total == edge - base                     # exactly the gap, to the cent
    assert retirement.get_conversion(conn, plan["ira"], plan["roth"], 2031) == total
    # And the bar now stops precisely on the line that was clicked.
    years = [r.year for r in page.roth_chart.rows]
    assert page.roth_chart.taxable_cents()[years.index(2031)] == edge
    assert fmt_money(total) in page.roth_notice.text()

    # The same line again is a TOGGLE (reported: undoing meant editing the
    # table): the bar is filled to it, so the click takes the conversion off.
    _click(page.roth_chart, 2031, edge / 100.0)
    assert retirement.list_conversions(conn, 2031) == []
    assert "removed" in page.roth_notice.text()
    # ...and once more fills it again, from the base.
    _click(page.roth_chart, 2031, edge / 100.0)
    assert sum(int(r["amount_cents"])
               for r in retirement.list_conversions(conn, 2031)) == total

    # A higher line moves the same conversion up rather than adding a second one.
    higher = next((page.roth_chart.edge_in(e, 2031) for e, _l in page.roth_chart.lines
                   if page.roth_chart.edge_in(e, 2031) > edge), None)
    assert higher is not None      # lines are drawn above the plan, not just to it
    _click(page.roth_chart, 2031, higher / 100.0)
    assert len(retirement.list_conversions(conn, 2031)) == 1
    assert sum(int(r["amount_cents"])
               for r in retirement.list_conversions(conn, 2031)) == higher - base


def test_a_click_that_is_not_on_a_line_inside_a_bar_writes_nothing(page, conn):
    chart = page.roth_chart
    edges = [e for e, _line in chart.lines]
    edge = _edge_above(chart, 2031)

    # Between two lines: nothing was aimed at.
    between = (edges[0] + edges[1]) / 2.0
    assert chart.bracket_edge_at(between / 100.0) is None
    _click(chart, 2031, between / 100.0)
    assert retirement.list_conversions(conn, 2031) == []

    # On the line but in the gutter between two bars: no year was aimed at.
    assert chart.year_at(2031.5) is None
    _click(chart, 2031.5, edge / 100.0)
    assert retirement.list_conversions(conn, 2031) == []

    # A middle click is neither gesture.
    _click(chart, 2031, edge / 100.0, button=2)
    assert retirement.list_conversions(conn, 2031) == []
    assert not page.schedule_window.isVisible()


def test_a_line_below_the_bar_says_there_is_no_room_instead_of_converting(
        page, conn, plan):
    retirement.set_taxable_income(conn, 2031, 400_000_00)
    page.refresh()
    chart = page.roth_chart
    low = min(e for e, _line in chart.lines)
    assert low < 400_000_00

    _click(chart, 2031, low / 100.0)
    assert retirement.list_conversions(conn, 2031) == []
    assert "no room" in page.roth_notice.text()


def test_a_right_click_opens_the_schedule_focused_on_that_year(page):
    """The schedule is its own modeless window (reported: embedded, it crowded
    the page); a right-click opens it on the year clicked."""
    assert not page.schedule_window.isVisible()

    _click(page.roth_chart, 2031, 0.0, button=3)

    assert page.schedule_window.isVisible()
    assert not page.schedule_window.isModal()
    assert page.schedule.focused_year() == 2031
    # A right-click elsewhere moves the focus; it does not open a second thing.
    _click(page.roth_chart, 2035, 0.0, button=3)
    assert page.schedule.focused_year() == 2035
    page.schedule_window.close()


def test_the_schedule_edits_the_year_and_the_conversion_in_place(
        page, conn, plan, qapp):
    page.show_schedule_for(2031)
    year = _year_item(page, 2031)
    assert year.isExpanded()                   # focusing a year opens it
    # Only the IRA-type source gets a line: a Roth converts into nothing.
    names = [year.child(i).text(rp.COL_YEAR) for i in range(year.childCount())]
    assert names == ["ANON Rollover IRA"]

    # Taxable income is COMPUTED: no editor, and typing into it changes nothing.
    computed = retirement.get_taxable_income(conn, 2031)
    delegate = page.schedule.tree.itemDelegateForColumn(rp.COL_TAXABLE)
    assert delegate.createEditor(page.schedule.tree, None, None) is None
    year.setText(rp.COL_TAXABLE, "70,000.00")
    qapp.processEvents()
    assert retirement.get_taxable_income(conn, 2031) == computed

    line = page.schedule.account_item(2031, plan["ira"])
    line.setText(rp.COL_CONVERSION, "15,000.00")
    assert retirement.get_conversion(conn, plan["ira"], plan["roth"], 2031) \
        == 15_000_00
    qapp.processEvents()
    assert _year_item(page, 2031).text(rp.COL_CONVERSION) == "15,000.00"
    assert page.schedule.account_item(2031, plan["ira"]).data(
        rp.COL_TARGET, rp.TARGET_ROLE) == plan["roth"]

    # Blanking the cell forgets the conversion rather than storing a zero.
    page.schedule.account_item(2031, plan["ira"]).setText(rp.COL_CONVERSION, "")
    assert retirement.list_conversions(conn, 2031) == []
    # Let the deferred rebuild run before the fixture drops the page: a pending
    # singleShot into a tree that is about to be torn down is a crash at
    # interpreter shutdown, not a failure any assertion here would catch.
    qapp.processEvents()


def test_a_years_total_is_edited_on_its_own_line(page, conn, plan, qapp):
    """The year line carries the total; typing one spreads it like a bracket
    click does, capped at what the sources hold."""
    page.show_schedule_for(2032)
    _year_item(page, 2032).setText(rp.COL_CONVERSION, "12,000.00")
    assert sum(int(r["amount_cents"])
               for r in retirement.list_conversions(conn, 2032)) == 12_000_00
    qapp.processEvents()


def test_the_schedule_builds_account_lines_only_for_an_open_year(page):
    """Reported: clicking in the flat table pegged the CPU. Lines for years
    nobody opened are never built."""
    page.schedule.reload()
    closed = [page.schedule.tree.topLevelItem(i)
              for i in range(page.schedule.tree.topLevelItemCount())]
    assert closed and all(item.childCount() == 0 for item in closed)


def test_an_unowned_source_is_flagged_and_points_to_account_details(
        page, conn, plan, qapp):
    """Reported: a conversion out of an account with no owner is flagged with
    the amber triangle, and the account opens Account Details."""
    retirement.set_conversion(conn, plan["ira"], plan["roth"], 2031, 5_000_00)
    page.refresh()
    page.show_schedule_for(2031)
    line = page.schedule.account_item(2031, plan["ira"])
    assert not line.icon(rp.COL_YEAR).isNull()
    assert line.toolTip(rp.COL_YEAR) == rp.NO_OWNER_TIP
    assert not _year_item(page, 2031).icon(rp.COL_YEAR).isNull()

    asked = []
    page.accountDetailsRequested.connect(asked.append)
    page.schedule._on_double_clicked(line, rp.COL_YEAR)
    qapp.processEvents()                       # the request is deferred a tick
    assert asked == [plan["ira"]]

    # Owned, the flag is gone.
    retirement.set_account_owner(conn, plan["ira"], plan["person"])
    page.schedule.reload()
    line = page.schedule.account_item(2031, plan["ira"])
    assert line.icon(rp.COL_YEAR).isNull()


def test_the_assumptions_are_behind_an_info_button(page):
    """Reported: the paragraph under the page took the schedule's rows."""
    assert page.assumptions.toolTip().startswith("Assumptions")
    assert page.assumption_lines()[0] in page.assumptions.toolTip()


def test_a_conversion_never_lands_in_another_persons_roth(page, conn, plan):
    """IRC 408A(d)(3): there is no spousal Roth conversion. With the only Roth
    owned by the spouse, the saver's conversion goes to a PLANNED Roth of the
    saver's own instead - never to the spouse's."""
    saver = plan["person"]
    spouse = retirement.add_person(conn, "ANON Spouse", "spouse", birth_year=1962)
    retirement.set_account_owner(conn, plan["ira"], saver)
    retirement.set_account_owner(conn, plan["roth"], spouse)
    page.refresh()

    ira = next(a for a in retirement.plan_accounts(conn) if not a.is_roth)
    targets = page.schedule.eligible_targets(ira)
    assert [t.account_id for t in targets] == [rp.NEW_PLANNED_TARGET]
    assert targets[0].owner_person_id == saver

    edge = _edge_above(page.roth_chart, 2031)
    _click(page.roth_chart, 2031, edge / 100.0)
    rows = retirement.list_conversions(conn, 2031)
    assert len(rows) == 1 and retirement.is_planned(rows[0]["to_account_id"])
    assert retirement.account_owner(conn, rows[0]["to_account_id"]) == saver

    # The same Roth, owned by the same person at the same institution, is the
    # target again - and the planned one, now unused, is dropped.
    retirement.set_account_owner(conn, plan["roth"], saver)
    page.refresh()
    assert page.schedule.eligible_targets(ira)[0].account_id == plan["roth"]


def test_a_conversion_goes_to_the_same_institutions_roth_or_a_planned_one(
        page, conn, plan):
    """Reported: converting an IRA lands in a Roth at the same custodian, which
    it opens if there is none. The ledger's only Roth is elsewhere, so the plan
    converts into a hypothetical "ANON Custodian Roth" rather than into it."""
    saver = plan["person"]
    for key in ("ira", "roth"):
        retirement.set_account_owner(conn, plan[key], saver)
    ledger.update_account(conn, plan["ira"], institution="ANON Custodian")
    ledger.update_account(conn, plan["roth"], institution="ANON Other")
    key, name = retirement.resolve_conversion_target(conn, plan["ira"])
    assert key is None and name == "ANON Custodian Roth - ANON Saver (planned)"

    target = retirement.conversion_target(conn, plan["ira"])
    assert retirement.is_planned(target)
    assert retirement.conversion_target(conn, plan["ira"]) == target  # reused
    planned = [a for a in retirement.plan_accounts(conn) if a.planned]
    assert [a.account_id for a in planned] == [target]
    assert rp.account_value_cents(conn, target) == 0

    # A real Roth there wins over the planned one.
    ledger.update_account(conn, plan["roth"], institution="ANON Custodian")
    assert retirement.resolve_conversion_target(conn, plan["ira"])[0] == plan["roth"]


def test_a_planned_roth_is_dropped_when_nothing_converts_into_it(conn, plan):
    ledger.update_account(conn, plan["roth"], institution="ANON Other")
    target = retirement.conversion_target(conn, plan["ira"])
    assert retirement.is_planned(target)
    retirement.set_conversion(conn, plan["ira"], target, 2030, 1_000_00)
    retirement.set_withdrawal(conn, target, 2040, 500_00)
    assert retirement.get_withdrawal(conn, target, 2040) == 500_00
    assert retirement.plan_flows(conn, 2030)[target].conversion_in_cents == 1_000_00
    retirement.delete_conversion(conn, plan["ira"], target, 2030)
    assert not [a for a in retirement.plan_accounts(conn) if a.planned]
    assert retirement.get_withdrawal(conn, target, 2040) is None


# ---------------------------------------------------------------------------
# withdrawals: seeded from the law, then edited on the page
# ---------------------------------------------------------------------------
def test_completing_social_security_seeds_every_required_minimum(qapp, conn):
    """A plan with nobody's age on file has no minimums; finishing SS gives it some.

    The three accounts are the three cases the law distinguishes: a rollover IRA
    is floored, a Roth never is (IRC 408A(c)(4)), and a current employer's plan
    is not while the user still works there (IRC 401(a)(9)(C)(i)(II)).
    """
    ira = ledger.create_account(conn, "ANON Rollover IRA", "investment")
    roth = ledger.create_account(conn, "ANON Roth IRA", "investment")
    work = ledger.create_account(conn, "ANON Employer 401(k)", "investment")
    for account_id, treatment in ((ira, "deferred"), (roth, "roth"),
                                  (work, "deferred")):
        rebalance.set_account_treatment(conn, account_id, treatment)
        ledger.add_transaction(conn, account_id, "2024-01-05", IRA_BALANCE)
    # Set the flag the way the USER does -- the Account Details checkbox -- so
    # this test covers the whole path from the box to the missing RMD floor,
    # not just the column the migration writes.
    _mark_current_employer_plan(conn, work)
    assert conn.execute(
        "SELECT current_employer_plan FROM accounts WHERE id = ?",
        (work,)).fetchone()[0] == 1

    p = RetirementPlannerPage(conn, today=TODAY)
    try:
        p.refresh()
        assert retirement.get_withdrawal(conn, ira, 2035) is None

        def complete_social_security(dlg):
            retirement.add_person(
                conn, "ANON Saver", "self", birth_year=BIRTH_YEAR,
                birth_month=BIRTH_MONTH, planned_claim_age_months=CLAIM_AGE_MONTHS)
            dlg.changed.emit()
            dlg.setParent(None)

        p._run_dialog = complete_social_security
        p.open_social_security()

        years = p.max_horizon()
        assert years[-1] == BIRTH_YEAR + rp.PLAN_WRITTEN_THROUGH_AGE
        today = rp.account_value_cents(conn, ira)
        p.withdrawals.invalidate_plan()
        seeded = 0
        for year in years:
            # The PRIOR December 31 balance, projected - not today's
            # (IRC 401(a)(9); reported: every minimum used today's balance).
            balance = p.withdrawals.prior_year_end_balance_cents(ira, year)
            required = retirement.account_rmd(conn, ira, balance, year)
            stored = retirement.get_withdrawal(conn, ira, year)
            if required:
                assert stored == required, year
                seeded += 1
            else:
                # Before the applicable age there is no minimum, and a zero row
                # would claim the user had chosen to take nothing.
                assert stored is None, year
            assert retirement.get_withdrawal(conn, roth, year) is None, year
            assert retirement.get_withdrawal(conn, work, year) is None, year
        assert seeded > 0
        # The 1960 cohort's applicable age is 75 under SECURE 2.0.
        assert retirement.get_withdrawal(conn, ira, 2034) is None
        assert retirement.get_withdrawal(conn, ira, 2035) > 0
        # The first minimum is on a balance that has grown for nine years.
        assert p.withdrawals.prior_year_end_balance_cents(ira, 2035) > today
    finally:
        p.deleteLater()


def test_a_click_on_an_income_bar_opens_the_withdrawals_on_that_year(page):
    assert not page.withdrawals_window.isVisible()
    _click(page.income_chart, 2031, 10_000)
    assert page.withdrawals_window.isVisible()
    assert page.withdrawals.focused_year() == 2031
    page.withdrawals_window.close()


def _tax_paid(page, year):
    """The income tax the last plan paid out of the accounts in ``year``."""
    plan = getattr(page.withdrawals, "last_plan", None)
    entry = (next((e for e in plan.years if e.year == year), None)
             if plan is not None else None)
    return entry.tax_cents if entry is not None else 0


def _need(page, conn, year, amount_cents, offset, pct="0"):
    """What the accounts must pay in ``year``: the spending need less Social
    Security and other income (the amount is a spending need, not a draw),
    plus the income tax the plan pays out of them."""
    target = retirement.household_amount_cents(amount_cents, pct, offset)
    covered = (rp.social_security_cents(conn, year, base_year=TODAY.year)
               + retirement.other_income_cents(conn, year))
    return max(0, target - covered) + _tax_paid(page, year)


def test_the_household_button_applies_through_the_maximum_age(page, conn, plan):
    wd = page.withdrawals
    years = wd.apply_years()
    # Through the LONGEST case, not the one being drawn: the button says "every
    # year", and a plan that stopped at the chosen case would quietly not.
    assert years[-1] == BIRTH_YEAR + rp.PLAN_WRITTEN_THROUGH_AGE
    assert years[-1] > page.income_rows()[-1].year

    assert wd.apply_household(60_000_00) is True
    # One household amount for the whole pool - the user names a number, not an
    # account - and EVERY account gets a row, including the zero ones: a year
    # left out of the table is an unplanned year that seeding would refill.
    for year in (years[0], years[0] + 3):
        rows = {int(w["account_id"]): int(w["amount_cents"])
                for w in retirement.list_withdrawals(conn, year)}
        assert set(rows) == {plan["ira"], plan["roth"]}
        need = _need(page, conn, year, 60_000_00, year - years[0])
        assert need > 0
        assert sum(rows.values()) == need
        # The Roth is held in reserve: no lifetime minimum reaches it
        # (IRC 408A(c)(4)), so it is the last thing the plan spends.
        assert rows[plan["ira"]] == need
        assert rows[plan["roth"]] == 0


def test_the_household_amount_rises_with_the_yearly_increase(page, conn):
    wd = page.withdrawals
    years = wd.apply_years()
    assert wd.apply_household(60_000_00, "2.5") is True
    for offset in (0, 1, 5):
        year = years[offset]
        total = sum(int(w["amount_cents"])
                    for w in retirement.list_withdrawals(conn, year))
        assert total == _need(page, conn, year, 60_000_00, offset, "2.5")


def test_one_empty_account_beside_a_funded_one_is_not_a_refusal(page, conn, plan):
    """The reported defect. Picking the small account used to refuse outright."""
    small = ledger.create_account(conn, "ANON Small IRA", "investment")
    rebalance.set_account_treatment(conn, small, "deferred")
    ledger.add_transaction(conn, small, "2024-01-05", 2_000_00)
    page.refresh()

    wd = page.withdrawals
    assert wd.apply_household(60_000_00) is True
    first = wd.apply_years()[0]
    assert sum(int(w["amount_cents"])
               for w in retirement.list_withdrawals(conn, first)) \
        == _need(page, conn, first, 60_000_00, 0)


def test_a_plan_the_pool_cannot_fund_is_drawn_until_it_runs_out(page, conn, plan):
    """Reported: refusing a plan that runs dry hid the picture that answers
    "how long does it last". It is written, each year takes what is left, and
    the notice says when the money runs out."""
    # 700,000 in the pool; 500,000 a year runs out in the second year. This
    # year's draw is an amount still to be taken, all of it, in the months left.
    assert page.withdrawals.apply_household(500_000_00) is True
    notice = page.withdrawal_notice.text()
    year = TODAY.year + 1
    assert f"You run out at age {year - BIRTH_YEAR} ({year})" in notice
    assert retirement.get_withdrawal_plan(conn).is_set is True
    # After the money is gone, nothing more is taken.
    assert retirement.get_withdrawal(conn, plan["ira"], 2035) == 0


def test_the_household_plan_comes_back_when_the_page_is_reopened(qapp, conn, plan):
    first = RetirementPlannerPage(conn, today=TODAY)
    try:
        first.refresh()
        assert first.withdrawals.apply_household(10_000_00, "2") is True
    finally:
        first.deleteLater()

    second = RetirementPlannerPage(conn, today=TODAY)
    try:
        second.refresh()
        wd = second.withdrawals
        # The control row is plain and always there -- no checkable toggle to
        # press, and nothing hidden behind one.
        assert not hasattr(wd, "per_year_button")
        assert not wd.per_year_amount.isHidden()
        assert not wd.per_year_increase.isHidden()
        assert not wd.per_year_apply.isHidden()
        assert rp.parse_amount(wd.per_year_amount.text()) == 10_000_00
        assert wd.per_year_increase.text() == "2"
    finally:
        second.deleteLater()


def test_a_household_withdrawal_drains_four_accounts_together(qapp, conn):
    """The life cycle: three tax-deferred accounts and a Roth, one amount.

    A common situation. No single account could fund the household on its
    own - the smallest holds less than four years of it - and the old per-account
    control refused the amount for exactly that reason while three other accounts
    sat full. What is checked here: the amount is accepted, every
    year's rows add up to the amount they typed, nobody is taken below their
    required minimum, and no account is spent past what it held.
    """
    balances = {"big": 400_000_00, "middle": 250_000_00, "small": 90_000_00,
                "roth": 600_000_00}
    ids = {}
    for key, cents in balances.items():
        account_id = ledger.create_account(conn, f"ANON {key} account",
                                           "investment")
        rebalance.set_account_treatment(
            conn, account_id, "roth" if key == "roth" else "deferred")
        ledger.add_transaction(conn, account_id, "2024-01-05", cents)
        ids[key] = account_id
    retirement.add_person(conn, "ANON Saver", "self", birth_year=BIRTH_YEAR,
                          birth_month=BIRTH_MONTH,
                          planned_claim_age_months=CLAIM_AGE_MONTHS)

    page = RetirementPlannerPage(conn, today=TODAY)
    try:
        page.refresh()
        wd = page.withdrawals
        years = wd.apply_years()
        # Large enough that accounts really run dry: with minimums computed on
        # the prior year-end balance (not today's), 25,000 never drained one.
        start, pct = 40_000_00, "1"
        # Large enough that the smallest account alone could not carry it.
        assert balances["small"] < start * 4

        assert wd.apply_household(start, pct) is True, page.withdrawal_notice.text()

        # Check the rows that landed in the database against the balances the
        # plan itself carried - each account's share of the pool's median,
        # rescaled every year so the accounts and the pool cannot drift apart.
        by_key = {v: k for k, v in ids.items()}
        entries = {e.year: e for e in wd.plan_for(start, pct).years}
        emptied: dict[str, int] = {}
        for offset, year in enumerate(years):
            rows = {int(w["account_id"]): int(w["amount_cents"])
                    for w in retirement.list_withdrawals(conn, year)}
            assert set(rows) >= set(ids.values()), year
            target = retirement.household_amount_cents(start, pct, offset)
            total = sum(rows.get(a, 0) for a in ids.values())
            if entries[year].shortfall_cents:
                break                        # the pool ran out; nothing to check
            # A required minimum can only RAISE a year's total, never lower it;
            # before the 1960 cohort's applicable age there is no minimum at all.
            assert total >= target, year
            if year < BIRTH_YEAR + 75:
                assert total == target + entries[year].tax_cents, year
            held = entries[year].balances
            for account_id in ids.values():
                name = by_key[account_id]
                cents = rows.get(account_id, 0)
                assert cents >= 0, (name, year)
                assert cents <= held[account_id], (name, year)   # never negative
                # The minimum is owed, but only up to what the account holds -
                # and it is computed on what the account ENTERS the year with.
                floor = min(retirement.account_rmd(conn, account_id,
                                                   held[account_id], year),
                            held[account_id])
                assert cents >= floor, (name, year, cents, floor)
                if held[account_id] and cents == held[account_id]:
                    emptied.setdefault(name, year)
        assert "does not last" not in page.withdrawal_notice.text()
        # The point of the whole exercise: accounts DO run dry along the way and
        # the household keeps getting its amount from the ones that have money.
        assert emptied, "no account was ever drained; the case proves nothing"
        assert "roth" not in emptied
    finally:
        page.deleteLater()


def test_the_life_cycle_pool_really_running_out_says_which_year(qapp, conn):
    """The other half: when even the Roth cannot carry it, say when."""
    ira = ledger.create_account(conn, "ANON deferred account", "investment")
    roth = ledger.create_account(conn, "ANON roth account", "investment")
    rebalance.set_account_treatment(conn, ira, "deferred")
    rebalance.set_account_treatment(conn, roth, "roth")
    ledger.add_transaction(conn, ira, "2024-01-05", 30_000_00)
    ledger.add_transaction(conn, roth, "2024-01-05", 20_000_00)
    retirement.add_person(conn, "ANON Saver", "self", birth_year=BIRTH_YEAR,
                          birth_month=BIRTH_MONTH,
                          planned_claim_age_months=CLAIM_AGE_MONTHS)

    page = RetirementPlannerPage(conn, today=TODAY)
    try:
        page.refresh()
        wd = page.withdrawals
        years = wd.apply_years()
        assert wd.apply_household(30_000_00) is True
        notice = page.withdrawal_notice.text()
        # 50,000 funds one year, not two: it runs out in the second.
        assert f"You run out at age {years[1] - BIRTH_YEAR} ({years[1]})" in notice
    finally:
        page.deleteLater()


def test_a_cell_is_clamped_below_by_the_rmd_and_above_by_what_is_left(page, conn, plan):
    wd = page.withdrawals
    floor = wd.rmd_floor_cents(plan["ira"], 2040)
    assert floor > 0
    assert wd.set_withdrawal(plan["ira"], 2040, 100_00) == floor
    assert retirement.get_withdrawal(conn, plan["ira"], 2040) == floor

    cap = wd.cap_cents(plan["ira"], 2040)
    assert cap > floor
    assert wd.set_withdrawal(plan["ira"], 2040, cap + 1_000_000_00) == cap
    assert retirement.get_withdrawal(conn, plan["ira"], 2040) == cap


# ---------------------------------------------------------------------------
# the View menu
# ---------------------------------------------------------------------------
def test_view_menu_switches_to_the_planner(qapp, conn, plan):
    from mammon.ui.widgets import MainWindow

    w = MainWindow(conn)
    try:
        titles = []
        for action in w.menuBar().actions():
            menu = action.menu()
            if menu is None:
                continue
            if action.text().replace("&", "") == "View":
                titles = [a.text() for a in menu.actions()]
        assert "Retirement Planner…" in titles

        page = w.show_retirement_planner()
        assert page is w.retirement_planner
        assert w.stack.currentWidget() is page
        assert page.income_rows()                 # refreshed on the way in
        # A plan edit leaves the dashboard stale (its projection carries the
        # plan's draws), to be redrawn on its next show.
        w.investment_dashboard._stale = False
        page.withdrawals.changed.emit()
        assert w.investment_dashboard._stale
        # And a ledger write leaves the PLANNER stale: its fund line starts
        # from today's balances.
        page._stale = False
        w._refresh_all()
        assert page._stale
    finally:
        w.close()
        w.deleteLater()


# ---------------------------------------------------------------------------
# a household member too young for SSA to have published their cohort
# ---------------------------------------------------------------------------
def test_income_rows_does_not_raise_for_a_person_with_a_future_eligibility_year(
        conn, plan):
    """A child of record used to take the whole page down in ``showEvent``.

    Every person with a birth year went through the benefit formula, and a person
    born in 2017 turns 62 in 2079 - a cohort SSA cannot have published - so the
    bend-point lookup raised ``KeyError`` before a single row was built. Two
    things had to be true for the page to survive, and both are checked here: a
    person with no earnings on file is not a claimant at all, and a real worker
    whose cohort is unpublished gets projected bend points instead of an
    exception (SRD 5.8m, 5.8o).
    """
    child = retirement.add_person(
        conn, "ANON Child", "child", birth_year=2017, birth_month=3,
        planned_claim_age_months=67 * 12)
    # A younger WORKER, not a child: earnings on file, and a cohort - 1995 + 62 =
    # 2057 - that is past the published bend-point series either way.
    worker = retirement.add_person(
        conn, "ANON Spouse", "spouse", birth_year=1995, birth_month=4,
        planned_claim_age_months=67 * 12)
    for year in range(2020, 2026):
        retirement.set_earnings(conn, worker, year, 60_000_00)

    rows = rp.income_rows(conn, today=TODAY)
    assert [r.year for r in rows] == rp.plan_horizon(
        conn, rp.DEFAULT_TERMINAL_AGE, TODAY)
    # The horizon is the SELF person's age case (``horizon_people``), so the
    # page itself stops long before 2079; the benefit formula is asked about
    # 2079 - the year the old code died on - directly below.
    assert rows[0].year == TODAY.year
    assert rows[-1].year == BIRTH_YEAR + rp.DEFAULT_TERMINAL_AGE

    people = {p["id"]: p for p in rp.planning_people(conn)}
    child_row, worker_row = people[child], people[worker]
    saver_row = people[plan["person"]]

    for year in (2026, 2050, 2079, rows[-1].year):
        # No earnings means no benefit, in every year of the projection.
        assert rp.social_security_cents(conn, year, [child_row]) == 0
        # And the household total is its earners, with nothing for the child.
        assert rp.social_security_cents(conn, year) == (
            rp.social_security_cents(conn, year, [saver_row, worker_row]))

    # Not vacuous: the worker on a PROJECTED cohort still draws a benefit, and
    # the retiree on a published one still draws theirs.
    assert rp.social_security_cents(conn, 2079, [worker_row]) > 0
    assert retirement.pia_bend_points(1995 + retirement.SS_ELIGIBILITY_AGE).projected
    assert rp.social_security_cents(conn, 2040, [saver_row]) > 0


def test_a_conversion_is_capped_at_what_the_source_holds(page, conn, plan):
    """Reported: a small 401(k) converted millions into a Roth, and the Roth's
    projection climbed on money that never existed."""
    schedule = page.schedule
    cap = schedule.conversion_cap_cents(plan["ira"], 2029)
    assert 0 < cap < 10 * IRA_BALANCE
    stored = schedule.set_conversion(plan["ira"], plan["roth"], 2029,
                                     cap + 1_000_000_00)
    assert stored == cap
    assert retirement.get_conversion(conn, plan["ira"], plan["roth"], 2029) == cap


def test_a_bracket_fill_never_converts_more_than_the_source_holds(page, conn, plan):
    schedule = page.schedule
    cap = schedule.conversion_cap_cents(plan["ira"], 2031)
    schedule.set_total_conversion(2031, cap + 1_000_000_00)
    total = sum(int(r["amount_cents"])
                for r in retirement.list_conversions(conn, 2031))
    assert total == cap


def test_account_details_sets_a_retirement_accounts_owner(qapp, conn, plan):
    from mammon.ui.widgets import AccountDetailsDialog

    dlg = AccountDetailsDialog(ledger.get_account(conn, plan["ira"]), conn=conn)
    try:
        assert dlg._owner_applies()
        dlg.owner.setCurrentIndex(dlg.owner.findData(plan["person"]))
        ledger.update_account(conn, plan["ira"], **dlg.values())
    finally:
        dlg.setParent(None)
    assert retirement.account_owner(conn, plan["ira"]) == plan["person"]


def test_an_empty_planned_roth_still_grows_in_the_household_check(page, conn, plan):
    """Reported: one spending level lasted and a slightly higher one "ran out"
    while the chart showed millions left. A planned Roth starts at $0, its
    growth was read off a zero projection as a factor of 1, and everything converted into it sat at
    0% for decades in the check."""
    ledger.update_account(conn, plan["roth"], institution="ANON Other")
    target = retirement.conversion_target(conn, plan["ira"])
    page.refresh()
    years = page.withdrawals.apply_years()[:5]
    planned = page.withdrawals.growth_factors(target, years)
    real = page.withdrawals.growth_factors(plan["ira"], years)
    assert planned[years[0]] > 1
    assert planned[years[0]] == real[years[0]]      # it grows at its source's mix


def test_a_child_does_not_stretch_the_plan_horizon(conn, plan):
    """A child's 90th birthday is not a retirement the plan runs to: counted,
    it pushed the plan (and the does-it-last check) to the 60-year cap."""
    before = rp.plan_horizon(conn, 90, TODAY)
    retirement.add_person(conn, "ANON Child", "child", birth_year=2017)
    assert rp.plan_horizon(conn, 90, TODAY) == before
    assert before[-1] == BIRTH_YEAR + 90
    # "Plan through age 90" is the user's own 90, not a younger spouse's.
    retirement.add_person(conn, "ANON Spouse", "spouse", birth_year=BIRTH_YEAR + 7)
    assert rp.plan_horizon(conn, 90, TODAY)[-1] == BIRTH_YEAR + 90


def test_the_fund_line_and_the_household_check_tell_one_story(page, conn, plan):
    """Reported: the line showed a large balance in the year the notice said the money
    ran out. Both now come from the pool's tracked mean and variance,
    so the line enters the run-out year holding exactly what that year draws."""
    w = page.withdrawals
    assert w.apply_household(300_000_00, "3")
    page.refresh()
    notice = page.withdrawal_notice.text()
    year = next(int(t.strip("().")) for t in notice.split() if t.startswith("(20"))
    row = _row(page, year)
    drawn = row.deferred_draw_cents + row.roth_draw_cents
    assert 0 < row.fund_value_cents == drawn
    # From then on the money is gone: nothing is drawn and the line stays at
    # zero (reported: tapering draws after the run-out year).
    after = _row(page, year + 1)
    assert after.fund_value_cents == 0
    assert after.deferred_draw_cents + after.roth_draw_cents == 0


def test_other_income_is_charted_and_pays_part_of_the_need(page, conn, plan):
    """Reported: rentals and royalties were nowhere in the plan. They are
    charted, counted as taxable income, and pay part of the spending need."""
    rent = retirement.add_income_source(conn, "ANON rentals", 21_000_00, 2026)
    retirement.add_income_source(conn, "ANON royalties", 20_000_00, 2026,
                                 change_pct="-10")
    page.refresh()
    assert _row(page, 2026).other_income_cents == 41_000_00
    assert _row(page, 2027).other_income_cents == 21_000_00 + 18_000_00
    assert rp.SERIES_OTHER in page.income_chart.bars

    wd = page.withdrawals
    assert wd.apply_household(60_000_00) is True
    first = wd.apply_years()[0]
    drawn = sum(int(w["amount_cents"])
                for w in retirement.list_withdrawals(conn, first))
    assert drawn == _need(page, conn, first, 60_000_00, 0)
    assert drawn - _tax_paid(page, first) == 19_000_00      # 60,000 - 41,000

    retirement.update_income_source(conn, rent, taxable=False)
    assert retirement.other_income_cents(conn, 2026, taxable_only=True) == 20_000_00


def test_the_household_plan_starts_in_the_year_given(page, conn, plan):
    """Reported: retiring early, or waiting on Social Security, needs a start
    year. Before it only required minimums are drawn."""
    wd = page.withdrawals
    years = wd.apply_years()
    start = years[0] + 3
    assert wd.apply_household(60_000_00, start_year=start) is True
    before = sum(int(w["amount_cents"])
                 for w in retirement.list_withdrawals(conn, years[0]))
    assert before == 0
    at = sum(int(w["amount_cents"]) for w in retirement.list_withdrawals(conn, start))
    assert at == _need(page, conn, start, 60_000_00, 0)
    assert retirement.get_withdrawal_plan(conn).start_year == start


def test_a_bracket_target_keeps_ira_draws_under_its_top(page, conn, plan):
    """Reported: draw from the Roth enough to stay in the 22% bracket. IRA draws
    stop at the room under the top; Roth pays the rest of the need."""
    wd = page.withdrawals
    years = wd.apply_years()
    year = years[0]
    room = wd.household_income(years, wd.accounts(), "12")[1](year)
    assert wd.apply_household(room + 30_000_00, bracket_rate="12") is True
    rows = {int(w["account_id"]): int(w["amount_cents"])
            for w in retirement.list_withdrawals(conn, year)}
    assert rows[plan["ira"]] == room
    assert rows[plan["roth"]] == _need(page, conn, year, room + 30_000_00, 0) - room
    assert retirement.get_withdrawal_plan(conn).bracket_rate == "12"


def test_a_typed_taxable_year_is_recomputed_with_other_income(page, conn):
    """Reported: typed years (a cleared cell stored an "entered" $0) ignored
    other income. Every year is computed now, typed ones included."""
    retirement.set_taxable_income(conn, 2031, 0, source="entered")
    retirement.add_income_source(conn, "ANON rentals", 21_000_00, 2026)
    page.refresh()
    assert retirement.get_taxable_income(conn, 2031) > 0
    assert {r["source"] for r in retirement.list_taxable_income(conn)} == {"seeded"}


def test_the_cola_raises_social_security_and_can_be_changed(page, conn, plan):
    """Reported: benefits stood still while spending rose. The COLA box in the
    header stores the assumption and redraws the plan with it."""
    before = _row(page, 2031).social_security_cents
    _set_assumption(page, "cola_edit", "0")
    assert retirement.get_cola_pct(conn) == 0
    flat = _row(page, 2031).social_security_cents
    assert flat < before
    _set_assumption(page, "cola_edit", "5")
    assert _row(page, 2031).social_security_cents == \
        retirement.household_amount_cents(flat, 5, 2031 - TODAY.year)


def test_a_minimum_is_computed_on_the_balance_entering_its_year(page, conn, plan):
    """Reported: every year's RMD used TODAY's balance. The household plan now
    divides the balance the account enters the year with."""
    wd = page.withdrawals
    assert wd.apply_household(1_000_00) is True
    first_rmd_year = BIRTH_YEAR + 75
    stored = retirement.get_withdrawal(conn, plan["ira"], first_rmd_year)
    on_today = retirement.account_rmd(conn, plan["ira"],
                                      wd.balance_cents(plan["ira"]), first_rmd_year)
    assert stored > on_today              # the account grew before its first RMD


def test_stale_draws_past_the_plan_are_cleared_when_it_is_applied(page, conn, plan):
    """Reported: stuck Roth values - draws stored under an older, longer horizon
    that no re-apply ever touched."""
    wd = page.withdrawals
    beyond = wd.apply_years()[-1] + 5
    retirement.set_withdrawal(conn, plan["roth"], beyond, 99_000_00)
    assert wd.apply_household(10_000_00) is True
    assert retirement.get_withdrawal(conn, plan["roth"], beyond) is None


def test_a_conversion_re_applies_the_household_plan(page, conn, plan, qapp):
    """Reported: adding a conversion left the draws as they were. It re-applies
    the plan - and spending comes first: in a year where the Roth is already
    paying part of the need, the new conversion's room is the IRA's to spend,
    so it nets away rather than converting money back out of the same Roth."""
    wd = page.withdrawals
    assert wd.apply_household(100_000_00, bracket_rate="12") is True
    year = wd.apply_years()[0]
    ira = retirement.get_withdrawal(conn, plan["ira"], year)
    roth = retirement.get_withdrawal(conn, plan["roth"], year)
    assert roth > 10_000_00                 # at the top: the Roth pays the rest
    page.schedule.set_conversion(plan["ira"], plan["roth"], year, 10_000_00)
    qapp.processEvents()                    # the coalesced re-apply
    # The conversion stands as typed, and the Roth it goes into is not drawn
    # that year: the IRA, beyond the bracket target, pays what the Roth did,
    # plus the tax the extra income causes.
    assert retirement.get_conversion(conn, plan["ira"], plan["roth"], year) == 10_000_00
    assert retirement.get_withdrawal(conn, plan["roth"], year) == 0
    assert retirement.get_withdrawal(conn, plan["ira"], year) > ira + roth
    # And the plan reproduces itself: re-applied unchanged, nothing moves.
    before = _plan_rows(conn)
    page._income_changed()
    assert _plan_rows(conn) == before


def test_bracket_tops_and_the_deduction_rise_with_indexing(page, conn):
    """Reported: the 2026 brackets were used for every year to 2065. The tops
    and the deduction are indexed at the bracket rate, a separate setting."""
    assert retirement.get_bracket_index_pct(conn) == retirement.DEFAULT_BRACKET_INDEX_PCT
    chart = page.roth_chart
    edge = chart.lines[0][0]
    assert chart.edge_in(edge, retirement.TAX_TABLE_YEAR) == edge
    assert chart.edge_in(edge, 2036) == retirement.household_amount_cents(
        edge, retirement.DEFAULT_BRACKET_INDEX_PCT, 2036 - retirement.TAX_TABLE_YEAR)
    _set_assumption(page, "bracket_index_edit", "0")
    assert page.roth_chart.edge_in(edge, 2036) == edge


def test_social_security_is_cut_to_the_payable_share_after_the_trust_fund_runs_out(
        page, conn, plan):
    """Reported: do not plan on Social Security always being there. From the
    projected depletion year only the payable share is paid; 100 turns it off."""
    shortfall = retirement.get_ss_shortfall(conn)
    assert (shortfall.year, shortfall.payable_pct) == (
        retirement.DEFAULT_SS_SHORTFALL_YEAR, retirement.DEFAULT_SS_PAYABLE_PCT)
    cut = _row(page, shortfall.year).social_security_cents
    before = _row(page, shortfall.year - 1).social_security_cents

    _set_assumption(page, "shortfall_pct", "100")
    full = _row(page, shortfall.year).social_security_cents
    assert _row(page, shortfall.year - 1).social_security_cents == before
    assert cut == retirement.SocialSecurityShortfall(
        shortfall.year, retirement.DEFAULT_SS_PAYABLE_PCT).payable(full, shortfall.year)
    assert cut < full

    with pytest.raises(ValueError):
        retirement.set_ss_shortfall(conn, 2033, 120)


def test_most_we_can_spend_is_the_largest_amount_that_lasts(page, conn, plan):
    wd = page.withdrawals
    best = wd.most_we_can_spend("2")
    assert best > 0
    reach = wd.years_shown()[-1]
    plan_ok = wd.plan_for(best, "2")
    assert plan_ok.lasts or plan_ok.depleted_year > reach
    over = wd.plan_for(best + 2_000_00, "2")
    assert not over.lasts and over.depleted_year <= reach


def test_hovering_a_year_shows_its_details(page, conn, plan):
    text = page.year_details(2031)
    assert text.startswith("2031")
    assert "IRA / 401(k) draws" in text and "Taxable income" in text
    assert page.income_chart.hover_text is not None


def test_the_roth_is_never_drawn_in_a_year_that_converts_into_it(page, conn, plan):
    """Reported: some years drew from the Roth while converting into it. The
    plan itself never draws a Roth in a year that converts into it: the IRA,
    beyond the bracket target, pays that share. It used to be rewritten after
    the plan instead - the conversion shrunk by the Roth draw - and every
    re-apply of the unchanged plan drew the Roth again and shrank it again."""
    wd = page.withdrawals
    year = wd.apply_years()[0]
    retirement.set_conversion(conn, plan["ira"], plan["roth"], year, 20_000_00)
    wd.invalidate_plan()                    # the conversion was written directly
    room = wd.household_income(wd.apply_years(), wd.accounts(), "12")[1](year)
    need = room + 5_000_00                  # 5,000 more than the IRA may pay
    assert wd.apply_household(need, bracket_rate="12") is True
    assert retirement.get_withdrawal(conn, plan["roth"], year) == 0
    assert retirement.get_conversion(conn, plan["ira"], plan["roth"], year) == 20_000_00
    assert retirement.get_withdrawal(conn, plan["ira"], year) >= room + 5_000_00
    before = _plan_rows(conn)
    for _ in range(3):                      # re-applied, it holds still
        assert wd.apply_household(need, bracket_rate="12") is True
        assert _plan_rows(conn) == before


def test_clicking_the_conversion_segment_removes_it(page, conn, plan):
    """The violet part of a bar is the conversion; a click on it takes it off."""
    chart = page.roth_chart
    edge = _edge_above(chart, 2031)
    _click(chart, 2031, edge / 100.0)
    assert retirement.list_conversions(conn, 2031)
    base = page.roth_chart.base_cents[2031]
    middle = (base + edge) / 2 / 100.0         # inside the conversion segment
    _click(page.roth_chart, 2031, middle)
    assert retirement.list_conversions(conn, 2031) == []


def test_an_emptied_ira_is_never_drawn_again_even_past_one_hundred(page, conn, plan):
    """Reported: planning to a late age, the IRA ran out, then the Roth, then
    IRA draws came back - the accounts' own median path had reached $0 while
    the pool still held money, and the split spread the draw over every
    account. The accounts are rescaled to the pool each year now."""
    page.terminal_age.setValue(110)
    wd = page.withdrawals
    assert wd.apply_household(45_000_00, "3") is True
    emptied = None
    for year in wd.apply_years():
        entry = next(e for e in wd.plan_for(45_000_00, "3").years if e.year == year)
        if emptied is None and entry.balances.get(plan["ira"], 0) == 0 and year > 2027:
            emptied = year
        if emptied is not None:
            assert (retirement.get_withdrawal(conn, plan["ira"], year) or 0) == 0, year
    assert emptied is not None, "the IRA never ran out; the case proves nothing"


def test_raising_the_age_re_applies_the_plan_to_the_new_years(page, conn, plan):
    """Reported: raising the age past 100 left the new years unplanned, and
    the seeding pass filled them with IRA minimums after the IRAs were empty."""
    wd = page.withdrawals
    assert wd.apply_household(45_000_00, "3") is True
    last = wd.apply_years()[-1]
    page.terminal_age.setValue(112)                 # just the spin box
    assert wd.apply_years()[-1] == BIRTH_YEAR + 112 > last
    later = last + 3
    rows = {int(w["account_id"]): int(w["amount_cents"])
            for w in retirement.list_withdrawals(conn, later)}
    assert set(rows) == {plan["ira"], plan["roth"]}  # planned, not seeded
    entry = next(e for e in wd.plan_for(45_000_00, "3").years if e.year == later)
    assert rows == {k: v for k, v in entry.amounts.items()}


def test_contributions_continue_until_the_plan_starts(page, conn, plan):
    """Reported: a current employer's 401(k) was projected as if nothing more
    would go in. Its measured yearly inflow is carried until retirement."""
    checking = ledger.create_account(conn, "ANON Checking", "checking")
    for month in range(1, 9):                      # $1,000 a month this year
        ledger.create_transfer(conn, checking, plan["ira"], f"2026-{month:02d}-15",
                               1_000_00)
    wd = page.withdrawals
    assert wd.apply_household(40_000_00, start_year=2030) is True
    measured = rp.planned_contributions(conn, TODAY, 2030)
    assert set(measured) == {2026, 2027, 2028, 2029}   # stops at retirement
    yearly = measured[2027][plan["ira"]]
    assert yearly == 8_000_00                          # the trailing-year sum
    # This year's share is the months left: a contribution is a stream, while
    # the projection takes a draw or a conversion - an amount still to happen
    # this year - whole.
    assert forecast.first_year_periods(TODAY) == 3     # Oct-Dec
    assert measured[2026][plan["ira"]] == yearly * 3 // 12
    wd.invalidate_plan()
    assert wd.flows_for(2028)[plan["ira"]].contribution_cents == yearly
    assert plan["ira"] not in wd.flows_for(2030) or         wd.flows_for(2030)[plan["ira"]].contribution_cents == 0


def test_a_current_employer_plan_owes_minimums_after_retirement(page, conn, plan):
    """Reported: the current-employer flag never expired, so the plan never
    took a minimum from it. From the plan's start year it is a former
    employer's plan."""
    _mark_current_employer_plan(conn, plan["ira"])
    page.refresh()
    wd = page.withdrawals
    first_rmd = BIRTH_YEAR + 75
    assert retirement.account_rmd(conn, plan["ira"], 100_000_00, first_rmd) == 0
    assert retirement.account_rmd(conn, plan["ira"], 100_000_00, first_rmd,
                                  employer_until=2030) > 0
    assert wd.apply_household(10_000_00, start_year=2030) is True
    stored = retirement.get_withdrawal(conn, plan["ira"], first_rmd)
    assert stored >= retirement.account_rmd(
        conn, plan["ira"], wd.prior_year_end_balance_cents(plan["ira"], first_rmd),
        first_rmd, employer_until=2030) * 0.9


def test_a_salary_deferral_and_match_fund_the_linked_401k(page, conn, plan):
    """Reported: the planner could, if it knew the deferral percentage and the
    employer match. The linked account's contributions come from the salary, and
    the deferral is pre-tax and not spendable."""
    salary = retirement.add_income_source(conn, "ANON salary", 100_000_00, 2026,
                                          end_year=2029, change_pct=2)
    retirement.update_income_source(conn, salary, deferral_pct=7, match_pct=3,
                                    into_account_id=plan["ira"])
    measured = rp.planned_contributions(conn, TODAY, None)
    # 2027: salary 102,000; 10% of it goes in.
    assert measured[2027][plan["ira"]] == 10_200_00
    assert measured[2026][plan["ira"]] == 10_000_00 * 3 // 12   # Oct-Dec only
    assert 2030 not in measured                                  # salary ended
    # Taxable income: the 7% deferral is pre-tax.
    sources = retirement.list_income_sources(conn)
    assert rp.gross_taxable_cents(conn, 2027, 0, 0, sources) == 102_000_00 - 7_140_00
    # A Roth 401(k) deferral is not pre-tax.
    retirement.update_income_source(conn, salary, into_account_id=plan["roth"])
    sources = retirement.list_income_sources(conn)
    assert rp.gross_taxable_cents(conn, 2027, 0, 0, sources) == 102_000_00
    # The deferral never pays spending.
    wd = page.withdrawals
    covered, _room = wd.household_income([2027], wd.accounts(), None)
    assert covered[2027] == rp.social_security_cents(conn, 2027, base_year=TODAY.year)         + 102_000_00 - 7_140_00


def test_the_income_dialog_edits_other_income_and_re_applies(page, conn, plan, qapp):
    """Income moved to a dialog (reported: the embedded panel was awkward).
    Other income typed there is stored, and the plan re-applies once on close."""
    wd = page.withdrawals
    assert wd.apply_household(60_000_00) is True
    first = wd.apply_years()[0]
    before = sum(int(w["amount_cents"])
                 for w in retirement.list_withdrawals(conn, first))

    def type_rentals(dlg):
        dlg.other.add_source()
        qapp.processEvents()
        dlg.other.table.item(0, rp.IN_COL_NAME).setText("ANON rentals")
        qapp.processEvents()
        dlg.other.table.item(0, rp.IN_COL_AMOUNT).setText("21,000.00")
        qapp.processEvents()
        dlg.setParent(None)

    page._run_dialog = type_rentals
    page.open_income()
    qapp.processEvents()
    (src,) = retirement.list_income_sources(conn)
    assert (src.name, src.amount_cents, src.kind) == ("ANON rentals", 21_000_00, "other")
    after = sum(int(w["amount_cents"])
                for w in retirement.list_withdrawals(conn, first))
    assert after == before - 21_000_00


def test_each_earner_has_a_salary_on_their_own_schedule(page, conn, plan, qapp):
    """Reported: a second earner retires on their own schedule. One salary
    section per earner; each salary's Through year ends
    its own contributions and its own employer plan."""
    spouse = retirement.add_person(conn, "ANON Spouse", "spouse", birth_year=1963)
    _mark_current_employer_plan(conn, plan["ira"])
    dlg = page.income_dialog()
    try:
        assert [f.person_id for f in dlg.salaries] == [plan["person"], spouse]
        mine, hers = dlg.salary_for(plan["person"]), dlg.salary_for(spouse)
        mine.amount.setText("100,000.00")
        mine.end.setValue(2028)
        mine.deferral.setText("7")
        mine.match.setText("3")
        mine.into.setCurrentIndex(mine.into.findData(plan["ira"]))
        mine.save()
        hers.amount.setText("50,000.00")
        hers.end.setValue(2031)
        hers.deferral.setText("5")
        hers.into.setCurrentIndex(hers.into.findData(plan["roth"]))
        hers.save()
        assert dlg.changed_anything
    finally:
        dlg.setParent(None)

    salaries = {src.person_id: src for src in retirement.list_income_sources(conn)
                if src.kind == "salary"}
    assert salaries[plan["person"]].end_year == 2028
    assert salaries[spouse].end_year == 2031
    contributions = rp.planned_contributions(conn, TODAY, None)
    assert contributions[2028][plan["ira"]] == 10_000_00
    assert plan["ira"] not in contributions.get(2029, {})     # he retired
    assert contributions[2031][plan["roth"]] == 2_500_00       # she has not
    # His current employer's plan is a former one from the year after his
    # salary ends, whatever the household's retirement year.
    assert retirement.employer_plan_ends(conn, 2035)[plan["ira"]] == 2029


def test_fill_years_fills_each_year_to_the_bracket_top(page, conn, plan, qapp):
    """Reported: fill a run of years to a bracket at once."""
    total = page.fill_years_to_bracket(2029, 2031, "12")
    status = page.filing_status()
    pct = retirement.get_bracket_index_pct(conn)
    for year in (2029, 2030, 2031):
        top = retirement.bracket_top_cents("12", status, year=year, index_pct=pct)
        got = sum(int(r["amount_cents"]) for r in retirement.list_conversions(conn, year))
        assert got == max(0, top - page._base[year]) or got > 0, year
    assert total > 0 and "Filled" in page.roth_notice.text()
    qapp.processEvents()


def test_maximize_spending_leaves_the_ending_reserve(page, conn, plan):
    """Reported: compute the spending rate that leaves a reserve at the end."""
    wd = page.withdrawals
    reach = wd.years_shown()[-1]
    plain = wd.most_we_can_spend("2")
    kept = wd.most_we_can_spend("2", reserve_cents=200_000_00)
    assert 0 < kept < plain
    assert wd.ending_balance_cents(wd.plan_for(kept, "2"), reach) >= 200_000_00
    over = wd.plan_for(kept + 2_000_00, "2")
    assert wd.ending_balance_cents(over, reach) < 200_000_00


def test_a_taxable_account_is_spent_after_the_ira_room_and_before_the_roth(
        page, conn, plan):
    """Reported: add taxable brokerage accounts as a source of money."""
    brokerage = ledger.create_account(conn, "ANON Brokerage", "investment")
    rebalance.set_account_treatment(conn, brokerage, "taxable")
    ledger.add_transaction(conn, brokerage, "2024-01-05", 50_000_00)
    page.refresh()
    wd = page.withdrawals
    assert brokerage in [a.account_id for a in wd.accounts() if a.is_taxable]
    year = wd.apply_years()[0]
    room = wd.household_income(wd.apply_years(), wd.accounts(), "12")[1](year)
    assert wd.apply_household(room + 20_000_00, bracket_rate="12") is True
    drawn = {int(w["account_id"]): int(w["amount_cents"])
             for w in retirement.list_withdrawals(conn, year)}
    assert drawn[plan["ira"]] == room               # the IRA fills the room
    assert drawn[brokerage] > 0                      # then the taxable account
    assert drawn.get(plan["roth"], 0) == 0           # before any Roth money
    # A taxable account is never a conversion source.
    assert brokerage not in [a.account_id for a in page.schedule.sources()]


def test_the_plan_mix_is_typed_in_the_planner(page, conn, plan):
    """Reported: the plan's asset mix should be set explicitly in the plan as
    stocks / bonds / cash, not copied from whatever What If said."""
    before = [r.fund_value_cents for r in page.income_rows()]
    assert page.mix_button.text() == "Plan mix: as held today"
    # A mix that does not add up is refused, and says so.
    assert page.apply_plan_mix(("60", "30", "5")) is False
    assert "100%" in page.withdrawal_notice.text()
    assert retirement.get_plan_mix(conn) is None
    assert page.apply_plan_mix(("80", "15", "5")) is True
    assert retirement.get_plan_mix(conn) == (80, 15, 5)
    assert page.mix_button.text() == "Plan mix: 80% stocks / 15% bonds / 5% cash"
    level = retirement.get_planning_risk(conn)
    from mammon import forecast
    assert level == forecast.risk_for_mix(retirement.plan_mix_weights((80, 15, 5)))
    stocks = [r.fund_value_cents for r in page.income_rows()]
    assert stocks[-1] > before[-1]    # the fixture holds cash; stocks grow faster
    # The dialog reads the three fields, or None for the mix held today.
    dlg = rp.PlanMixDialog((Decimal(80), Decimal(15), Decimal(5)))
    assert not dlg.held.isChecked()
    dlg.fields[0].setText("70")
    dlg.fields[1].setText("25")
    assert dlg.values() == (70, 25, 5)
    dlg.held.setChecked(True)
    assert dlg.values() is None
    dlg.deleteLater()
    assert page.apply_plan_mix(None) is True
    assert retirement.get_planning_risk(conn) is None
    assert [r.fund_value_cents for r in page.income_rows()] == before


def test_a_conversion_beyond_what_the_ira_holds_is_cut_back_not_drawn(page, conn, plan):
    """Reported: IRA draws for years after the IRAs ran out. A conversion
    planned against a projection that still showed money in an emptied IRA
    was netted into IRA draws; it is cut back to what the account holds."""
    wd = page.withdrawals
    year = wd.apply_years()[0] + 5
    retirement.set_conversion(conn, plan["ira"], plan["roth"], year, 5_000_000_00)
    assert wd.apply_household(60_000_00) is True
    held = next(e for e in wd.plan_for(60_000_00, 0).years if e.year == year)
    converted = retirement.get_conversion(conn, plan["ira"], plan["roth"], year) or 0
    drawn = retirement.get_withdrawal(conn, plan["ira"], year) or 0
    assert converted < 5_000_000_00
    assert drawn + converted <= held.balances[plan["ira"]] + 1_00


def test_a_fill_leaves_room_for_the_tax_the_conversion_causes(page, conn, plan):
    """Reported: the plan pays the conversion's tax from the IRA, so a gap
    filled by the conversion alone went over the line every time. The fill
    lands the year - conversion, tax draw and all - on the top."""
    wd = page.withdrawals
    year = wd.apply_years()[0] + 4
    assert wd.apply_household(40_000_00, start_year=year) is True
    page.refresh()
    top = retirement.bracket_top_cents("12", page.filing_status(), year=year,
                                       index_pct=retirement.get_bracket_index_pct(conn))
    page.fill_years_to_bracket(year, year, "12")
    converted = sum(int(r["amount_cents"])
                    for r in retirement.list_conversions(conn, year))
    assert converted > 0
    assert top - 1_00 <= page.taxable_with(year, converted) <= top   # never over
    # The IRA draw that pays the tax is its own slice of the bar.
    assert page.roth_chart.tax_draw_cents.get(year, 0) > 0
    assert "of which the conversion adds" in page.year_details(year)


def test_the_spending_order_and_sources_are_set_in_the_withdrawal_window(
        page, conn, plan, qapp):
    """Reported: the order is the household's to set, in the spending
    section, and an account can be left out of it."""
    wd = page.withdrawals
    assert wd.apply_household(60_000_00) is True
    wd.reload()
    year = wd.apply_years()[0]
    assert retirement.get_withdrawal(conn, plan["roth"], year) == 0   # IRA first
    order = wd.order_list
    steps = [order.item(i).data(Qt.UserRole) for i in range(order.count())]
    assert steps == list(retirement.SPENDING_STEPS)
    # Select the Roth and move it to the top with the Up button.
    order.setCurrentRow(steps.index("roth"))
    for _ in range(len(steps)):
        wd.order_up.click()
    qapp.processEvents()                    # the coalesced re-apply
    assert order.item(0).data(Qt.UserRole) == "roth"
    assert order.currentItem().data(Qt.UserRole) == "roth"   # still selected
    assert retirement.get_spending_order(conn)[0] == "roth"
    assert retirement.get_withdrawal(conn, plan["roth"], year) > 0
    # Unchecking the Roth leaves it out again.
    wd.source_boxes[plan["roth"]].setChecked(False)
    qapp.processEvents()
    # Laid out in a grid, as many to a row as the names allow.
    window = page.withdrawals_window
    window.resize(1400, 800)
    window.show()
    qapp.processEvents()
    assert wd._source_columns == wd.source_columns() == len(wd.source_boxes)
    window.hide()
    assert plan["roth"] in retirement.spending_exempt_ids(conn)
    assert retirement.get_withdrawal(conn, plan["roth"], year) == 0


def test_a_fill_says_when_the_iras_are_empty(page, conn, plan):
    """Reported: a year the IRAs had run dry was reported as "already had more
    taxable income than that", which it did not."""
    wd = page.withdrawals
    first = wd.apply_years()[0]
    assert wd.apply_household(IRA_BALANCE, bracket_rate="37") is True   # IRA gone early
    page.refresh()
    late = first + 15
    page.fill_years_to_bracket(late, late, "22")
    assert page.fill_outcome(late, retirement.bracket_top_cents(
        "22", page.filing_status(), year=late,
        index_pct=retirement.get_bracket_index_pct(conn)), 0) == "empty"
    text = page.roth_notice.text()
    assert "nothing left in the tax-deferred accounts" in text
    assert "already had more taxable income" not in text


def test_irmaa_is_charged_shown_and_paid_from_two_years_back(page, conn, plan):
    """Reported: add IRMAA for the user and spouse. The saver turned 65 in
    2025; income two years before each premium year sets the surcharge, the
    plan pays it, and the page shows it."""
    wd = page.withdrawals
    assert wd.apply_household(400_000_00) is True      # high income: a tier
    page.refresh()
    charged = page.irmaa_by_year()
    first = page.rows[0].year
    assert first not in charged and first + 1 not in charged    # no lookback yet
    year = first + 2            # set by the first year's income, when the IRA
    cents, tier, months, _source = charged[year]     # still pays most of $400,000
    assert cents > 0 and tier >= 1 and months == 12
    entry = next(e for e in wd.last_plan.years if e.year == year)
    assert entry.surcharge_cents > 0
    assert "Medicare surcharges (IRMAA)" in page.tax_summary()
    text = page.year_details(year)
    assert "Medicare surcharge (IRMAA)" in text and f"Sets {year + 2}'s IRMAA" in text
    # The tier lines are drawn, and are click targets like the brackets.
    chart = page.roth_chart
    assert chart.irmaa_drawn
    _tier, values, _line = chart.irmaa_drawn[0]
    assert chart.bracket_edge_at(values[year] / 100.0, year) == values[year]


def test_a_fill_can_aim_at_an_irmaa_tier(page, conn, plan):
    """Reported: the IRMAA tiers belong in the list of lines to fill to,
    named as the chart names them, not behind a "stay under the next IRMAA
    tier" checkbox whose tier changed from year to year."""
    wd = page.withdrawals
    assert wd.apply_household(40_000_00) is True
    page.refresh()
    year = page.rows[0].year + 4
    lines = page.irmaa_lines()
    tier = next(n for n, line in enumerate(lines, start=1)
                if line.get(year, 0) > page._base[year])
    page.fill_years_to_bracket(year, year, rp.irmaa_fill_key(tier))
    top = page.irmaa_fill_tops(tier, [year])[year]
    converted = sum(int(r["amount_cents"])
                    for r in retirement.list_conversions(conn, year))
    assert converted > 0
    # At or just under the tier: it is a cliff, a cent over costs the tier.
    assert top - 1_00 <= page.taxable_with(year, converted) <= top
    assert f"IRMAA {tier}, landing at or just under it:" in page.roth_notice.text()


def test_the_fill_list_offers_the_bracket_tops_then_the_irmaa_tiers(page, qapp):
    combo = page.schedule.fill_bracket
    labels = [combo.itemText(i) for i in range(combo.count()) if combo.itemData(i)]
    tiers = len(retirement.irmaa_ceilings_cents("joint", TODAY.year))
    assert tiers == 5
    assert labels[:2] == ["top of 10%", "top of 12%"]
    assert labels[-tiers - 1:] == [f"IRMAA {n}" for n in range(1, tiers + 1)] + ["ACA cliff"]
    assert combo.currentText() == "top of 22%"
    assert not hasattr(page.schedule, "fill_irmaa")     # the checkbox is gone
    # Choosing a tier asks the page to fill to it.
    asked = []
    page.schedule.fillRangeRequested.disconnect()
    page.schedule.fillRangeRequested.connect(lambda *a: asked.append(a))
    combo.setCurrentIndex(combo.findData(rp.irmaa_fill_key(2)))
    page.schedule.fill_button.click()
    assert asked == [(page.schedule.fill_from.value(), page.schedule.fill_to.value(),
                      "irmaa:2")]
    assert rp.irmaa_fill_tier("irmaa:2") == 2 and rp.irmaa_fill_tier("22") is None


def test_an_irmaa_fill_leaves_a_year_that_sets_no_premium_alone(page, conn, plan):
    """A tier line exists only where the income sets a Medicare premium (two
    years later, once someone is 65). Born 1975, the saver's first such year
    is 2038, so 2037 is left exactly as it was and the notice says so."""
    retirement.update_person(conn, plan["person"], birth_year=1975)
    retirement.set_conversion(conn, plan["ira"], plan["roth"], 2037, 1_234_00)
    page.refresh()
    assert page.irmaa_fill_tops(1, [2037, 2038]).keys() == {2038}
    page.fill_years_to_bracket(2037, 2038, rp.irmaa_fill_key(1))
    assert retirement.get_conversion(conn, plan["ira"], plan["roth"], 2037) == 1_234_00
    assert sum(int(r["amount_cents"])
               for r in retirement.list_conversions(conn, 2038)) > 0
    assert "2037 set no Medicare premium" in page.roth_notice.text()
    # A range with no premium year at all changes nothing and says why.
    before = retirement.list_conversions(conn)
    assert page.fill_years_to_bracket(2026, 2030, rp.irmaa_fill_key(1)) == 0
    assert retirement.list_conversions(conn) == before
    assert "no IRMAA 1 line to fill" in page.roth_notice.text()


def test_the_survivor_scenario_runs_the_plan_on_for_the_spouse(page, conn, plan, qapp):
    """Reported: add the survivor scenario. The saver dies; the spouse files
    single, keeps the larger benefit, is the one enrollee, and lives on 75%."""
    spouse = retirement.add_person(
        conn, "ANON Spouse", "spouse", birth_year=BIRTH_YEAR + 4, birth_month=3,
        planned_claim_age_months=CLAIM_AGE_MONTHS)
    for year in range(2015, 2026):
        retirement.set_earnings(conn, spouse, year, 30_000_00)
    page.refresh()
    at = [page.filing_combo.itemData(i) for i in range(page.filing_combo.count())]
    page.filing_combo.setCurrentIndex(at.index("joint"))
    death = 2034
    later = death + 2
    both = rp.social_security_cents(conn, later, base_year=TODAY.year)
    # Choose "If <self> dies" in the header, then the year.
    combo = page.survivor_combo
    assert combo.isVisible() or combo.count() == 3
    combo.setCurrentIndex(combo.findData(plan["person"]))
    page.survivor_year.setValue(death)
    qapp.processEvents()
    scenario = retirement.get_survivor_scenario(conn)
    assert scenario.deceased_id == plan["person"] and scenario.death_year == death
    widowed = rp.social_security_cents(conn, later, base_year=TODAY.year)
    assert 0 < widowed < both                    # the larger of the two, not both
    assert page.status_in(death) == "joint" and page.status_in(later) == "single"
    assert [p["id"] for p in rp.horizon_people(conn)] == [spouse]
    # The survivor lives on 75% of the household's spending.
    wd = page.withdrawals
    assert wd.apply_household(60_000_00) is True
    entry = next(e for e in wd.last_plan.years if e.year == later)
    offset = later - wd.apply_years()[0]
    spending = retirement.household_amount_cents(60_000_00, 0, offset) * 3 // 4
    covered = widowed + retirement.other_income_cents(conn, later)
    assert entry.target_cents - entry.tax_cents - entry.surcharge_cents \
        == max(0, spending - covered)
    # Back to both living.
    combo.setCurrentIndex(combo.findData(None))
    qapp.processEvents()
    assert retirement.get_survivor_scenario(conn) is None


def test_the_clear_button_removes_every_conversion_after_asking(page, conn, plan,
                                                                 qapp):
    """Reported: a Clear button beside the conversion chart."""
    assert retirement.list_conversions(conn)
    page.confirm = lambda title, text: False            # "No": nothing changes
    page.clear_conversions_button.click()
    assert retirement.list_conversions(conn)
    asked = []
    page.confirm = lambda title, text: asked.append(text) or True
    page.clear_conversions_button.click()
    qapp.processEvents()
    assert asked and "planned conversions" in asked[0]
    assert retirement.list_conversions(conn) == []
    assert "Cleared" in page.roth_notice.text()
    assert page.clear_all_conversions() == 0            # nothing left to clear


def test_the_ssa44_appeal_sets_the_first_premiums_from_their_own_year(page, conn, plan):
    """Reported: retiring is a life-changing event - Social Security will use
    the current year's income instead of two years back (Form SSA-44)."""
    pay = retirement.add_income_source(conn, "ANON pay", 400_000_00, 2026,
                                       end_year=2029, kind="salary")
    retirement.update_income_source(conn, pay, person_id=plan["person"])
    assert rp.appealed_premium_years(conn) == {2030: 2030, 2031: 2030}
    page.refresh()
    appealed = page.irmaa_by_year()[2031]
    assert appealed[3] == 2031                  # the year's own income, not 2029's
    assert "SSA-44 appeal" in page.year_details(2031)
    retirement.set_ssa44_appeal(conn, False)
    assert rp.appealed_premium_years(conn) == {}
    page.refresh()
    normal = page.irmaa_by_year()[2031]
    assert normal[3] == 2029 and normal[1] > appealed[1]    # the salary's tier
    assert normal[0] > appealed[0]


def test_the_tax_can_come_from_a_taxable_account(page, conn, plan, qapp):
    """Reported: an option to pay the income tax from a taxable account, and
    its sales taxed as capital gains."""
    brokerage = ledger.create_account(conn, "ANON Brokerage", "investment")
    rebalance.set_account_treatment(conn, brokerage, "taxable")
    ledger.add_transaction(conn, brokerage, "2024-01-05", 300_000_00)
    page.refresh()
    wd = page.withdrawals
    assert wd.apply_household(60_000_00, bracket_rate="22") is True
    year = wd.apply_years()[0]
    before = retirement.get_withdrawal(conn, brokerage, year) or 0
    wd.tax_from.setCurrentIndex(wd.tax_from.findData("taxable"))
    qapp.processEvents()                     # the coalesced re-apply
    assert retirement.get_tax_paid_from(conn) == "taxable"
    entry = next(e for e in wd.last_plan.years if e.year == year)
    assert entry.tax_cents > 0
    assert (retirement.get_withdrawal(conn, brokerage, year) or 0) >= \
        before + entry.tax_cents - 1
    page.refresh()
    assert "Estimated federal income tax" in page.year_details(year)


def test_rent_marked_as_investment_income_pays_the_niit(page, conn, plan):
    """Reported: net rent is investment income for the 3.8% surtax; the
    Income dialog marks it, and a high-income year pays it."""
    rent = retirement.add_income_source(conn, "ANON rentals", 21_000_00, 2026)
    source = next(s for s in retirement.list_income_sources(conn) if s.id == rent)
    assert source.niit is False and source.investment_income is False
    retirement.update_income_source(conn, rent, niit=True)
    retirement.set_conversion(conn, plan["ira"], plan["roth"], 2027, 400_000_00)
    page.refresh()
    parts = page.tax_in(2027, _row(page, 2027).conversion_cents)
    assert parts.niit == retirement.niit_cents(21_000_00, page.magi_cents(
        2027, _row(page, 2027).conversion_cents), page.filing_status())
    assert parts.niit > 0
    assert "net investment income tax" in page.year_details(2027)
    # The income table's column sets it.
    table = rp.IncomeSourcesTable(conn, today=TODAY)
    table.reload()
    row = table._ids.index(rent)
    item = table.table.item(row, rp.IN_COL_NIIT)
    assert item.checkState() == Qt.Checked
    item.setCheckState(Qt.Unchecked)
    assert not next(s for s in retirement.list_income_sources(conn) if s.id == rent).niit
    table.deleteLater()


def test_filling_to_a_bracket_below_the_income_removes_the_conversions(page, conn, plan):
    """Reported: fill to 24%, then fill to 10% - below the year's income -
    left the 24% conversions in place; they should be removed."""
    page.fill_years_to_bracket(2029, 2031, "24")
    assert any(retirement.list_conversions(conn, y) for y in (2029, 2030, 2031))
    page.fill_years_to_bracket(2029, 2031, "10")
    status = page.filing_status()
    pct = retirement.get_bracket_index_pct(conn)
    for year in (2029, 2030, 2031):
        top = retirement.bracket_top_cents("10", status, year=year, index_pct=pct)
        converted = sum(int(r["amount_cents"])
                        for r in retirement.list_conversions(conn, year))
        # Filled to the line, counting the deduction left unused and the
        # Social Security the conversion pulls in (IRC 86).
        assert converted == page.conversion_room(year, top), year
        if converted:
            assert abs(page.taxable_with(year, converted) - top) <= 1, year


def test_the_tax_total_counts_what_is_left_in_the_iras(page, conn, plan):
    """Reported: add the tax on IRAs left at the end, so conversions (which
    shrink it) compare fairly."""
    wd = page.withdrawals
    assert wd.apply_household(10_000_00) is True     # spends little: IRAs remain
    page.refresh()
    left, tax, _today = page.end_ira_tax()
    assert left > 0
    assert tax == left * 24 // 100 or abs(tax - left * 24 / 100) < 1
    retirement.set_end_ira_tax_pct(conn, 10)
    assert page.end_ira_tax()[1] < tax
    assert "left in IRAs at the end" in page.tax_summary()


def test_go_to_investment_dashboard_asks_the_window(page):
    asked = []
    page.dashboardRequested.connect(lambda: asked.append(True))
    page.dashboard_link.linkActivated.emit("dashboard")
    assert asked == [True]


def test_the_charts_follow_a_theme_switch_and_stay_readable(page, qapp):
    """Reported in light mode: tick labels and legends unreadable (drawn in the
    dark theme's near-white and never redrawn), bracket lines hard to see, and
    legends on top of the bars."""
    from mammon.ui import style
    page.show()                     # a hidden page redraws when it is shown
    try:
        style.apply_theme(qapp, {"theme": "dark"})
        page.refresh()
        qapp.processEvents()
        style.apply_theme(qapp, {"theme": "light"})
        # The redraw is deferred to a timer; wait for it rather than for a
        # fixed number of event-loop turns.
        import time
        deadline = time.monotonic() + 5
        qapp.processEvents()
        while getattr(page, "_retheme_pending", False) and time.monotonic() < deadline:
            qapp.processEvents()
        assert not getattr(page, "_retheme_pending", False)
        ax = page.roth_chart.axes
        assert ax.get_yticklabels()[0].get_color() in ("black", "#000000", "k")
        legend = ax.get_legend()
        assert all(t.get_color() in ("black", "#000000", "k")
                   for t in legend.get_texts())
        # Above the plot, not on the bars.
        assert legend.get_bbox_to_anchor().transformed(
            ax.transAxes.inverted()).y0 > 1.0
        # Solid bracket lines.
        assert page.roth_chart.lines and all(
            line.get_linestyle() == "-" for _edge, line in page.roth_chart.lines)
        # Hidden, it waits: shown again, it redraws.
        page.hide()
        style.apply_theme(qapp, {"theme": "dark"})
        qapp.processEvents()
        assert page._retheme_on_show
        page.show()
        assert not page._retheme_on_show
    finally:
        page.hide()
        style.apply_theme(qapp, {"theme": "light"})


def test_the_conversion_chart_is_scaled_to_its_bars(page, conn, plan):
    """Reported: the tier-5 IRMAA line in 2055 set the top of the plot and
    squeezed the bars into its bottom third."""
    wd = page.withdrawals
    assert wd.apply_household(40_000_00) is True
    page.refresh()
    chart = page.roth_chart
    tallest = max(chart.taxable_cents()) / 100.0
    lines = [max(v for v in line.get_ydata() if v == v)
             for _t, _v, line in chart.irmaa_drawn]
    top = chart.axes.get_ylim()[1]
    assert top >= tallest
    if lines and max(lines) > top:
        # A line that runs out of the top is labeled where it does, above
        # the plot.
        assert chart._top_labels and all(t.get_visible() for t in chart._top_labels)


def test_each_line_is_labeled_where_it_leaves_the_plot(page, conn, plan, qapp):
    """Reported: label the lines at the point they exit the plot. A line that
    rises through the top is labeled above the plot at the crossing; one that
    stops inside the plot (the ACA cliff once both are on Medicare) at its
    end; one that runs to the last year in the right margin, level with it."""
    retirement.add_person(conn, "ANON Spouse", "spouse", birth_year=1966, birth_month=1)
    retirement.set_aca_benchmark_cents(conn, 20_000_00)
    retirement.set_withdrawal_plan(conn, 40_000_00, 0, start_year=2026)
    at = [page.filing_combo.itemData(i) for i in range(page.filing_combo.count())]
    page.filing_combo.setCurrentIndex(at.index("joint"))
    for year, cents in ((2027, 90_000_00), (2029, 120_000_00), (2031, 60_000_00)):
        retirement.set_conversion(conn, plan["ira"], plan["roth"], year, cents)
    page.refresh()
    chart = page.roth_chart
    chart.figure.set_size_inches(11, 3.8)
    page._render_roth()
    ax = chart.axes
    renderer = chart.figure.canvas.get_renderer()
    plot = ax.get_window_extent(renderer=renderer)
    ceiling = ax.get_ylim()[1]
    texts = {t.get_text(): t for t in ax.texts if t.get_visible()}
    assert not any(name.endswith("↑") for name in texts)       # no arrows any more

    # "top of 24%" rises out through the top: above the plot, at the crossing.
    edge = next(e for e, label in chart.bracket_edges_cents() if label == "top of 24%")
    years = list(chart._years_drawn)
    steps = [chart.edge_in(edge, y) / 100.0 for y in years]
    kind, x, _y = chart.line_exit(years, steps, ceiling)
    assert kind == "top"
    label = texts["top of 24%"]
    box = label.get_window_extent(renderer=renderer)
    assert box.y0 >= plot.y1 - 1
    crossing = ax.transData.transform((x, 0))[0]
    assert box.x0 - 2 <= crossing <= box.x1 + 2
    # The legend rose clear of it.
    legend = ax.get_legend().get_window_extent(renderer=renderer)
    assert legend.y0 >= box.y1 - 1 or legend.x1 <= box.x0 or legend.x0 >= box.x1

    # The ACA cliff stops inside the plot once both are on Medicare: labeled
    # where it ends, on the page's background, over the bars.
    aca = texts["ACA cliff"]
    last = max(chart.aca_values)
    assert last < years[-1]
    ax_x = ax.transData.transform((last + 0.5, 0))[0]
    aca_box = aca.get_window_extent(renderer=renderer)
    assert abs(aca_box.x0 - ax_x) <= 4 and plot.y0 <= aca_box.y0 <= plot.y1
    assert aca.get_bbox_patch() is not None and aca.get_zorder() > 3

    # "top of 12%" runs to the last year inside the plot: the right margin.
    right = texts["top of 12%"].get_window_extent(renderer=renderer)
    assert right.x0 >= plot.x1

    # line_exit itself, for each way out and for a line never seen.
    nan = float("nan")
    assert chart.line_exit([1, 2, 3], [1.0, 2.0, 3.0], 5.0) == ("right", 3.0, 3.0)
    assert chart.line_exit([1, 2, 3], [1.0, 6.0, 7.0], 5.0) == ("top", 1.5, 1.0)
    assert chart.line_exit([1, 2, 3], [1.0, 2.0, nan], 5.0) == ("end", 2.5, 2.0)
    assert chart.line_exit([1, 2, 3], [6.0, 7.0, 8.0], 5.0) is None
    qapp.processEvents()



def test_every_bracket_line_drawn_shows_at_the_left_with_no_conversions(page, conn):
    """Reported: with the conversions cleared, the 24% and 32% lines could not
    be seen at all."""
    page.confirm = lambda title, text: True
    page.clear_all_conversions()
    page.refresh()
    chart = page.roth_chart
    top = chart.axes.get_ylim()[1]
    first = chart.rows[0].year
    assert len(chart.lines) >= 3
    for edge, _line in chart.lines:
        assert chart.edge_in(edge, first) / 100.0 <= top


def test_the_aca_credit_slopes_down_to_the_cliff_and_then_goes_at_once(page, conn, plan):
    """A household retired before 65 buys marketplace coverage. The credit is
    the benchmark less a rising share of income (IRC 36B): a small conversion
    costs part of it, and a dollar over 400% of the poverty line the rest."""
    spouse = retirement.add_person(conn, "ANON Spouse", "spouse",
                                   birth_year=1970, birth_month=1)
    retirement.set_aca_benchmark_cents(conn, 20_000_00)
    retirement.set_withdrawal_plan(conn, 40_000_00, 0, start_year=2026)
    at = [page.filing_combo.itemData(i) for i in range(page.filing_combo.count())]
    page.filing_combo.setCurrentIndex(at.index("joint"))
    retirement.set_conversion(conn, plan["ira"], plan["roth"], 2027, 20_000_00)
    page.refresh()
    assert page.aca_line().get(2027) is not None      # the spouse is under 65
    small = page.aca_by_year().get(2027, 0)
    full = rp.aca_credit_in(conn, 2027, rp.aca_poverty_line_in(conn, 2027, "joint"), "joint")
    assert 0 < small < full                           # part of it, not all
    assert "ACA premium credit: " in page.year_details(2027)
    retirement.set_conversion(conn, plan["ira"], plan["roth"], 2027, 300_000_00)
    page.refresh()
    assert page.aca_by_year().get(2027, 0) > small    # over the cliff: all of it
    assert "ACA premium credit lost" in page.year_details(2027)
    assert spouse


def test_a_spouse_with_no_record_draws_the_spousal_benefit(conn, plan):
    """Audit: a spouse with no earnings was paid nothing. 42 USC 402(b): half
    the worker's PIA, once both have claimed, unreduced at full retirement age."""
    retirement.add_person(conn, "ANON Spouse", "spouse", birth_year=1961,
                          birth_month=6, planned_claim_age_months=67 * 12)
    earnings = retirement.earnings_map(conn, plan["person"])
    pia = retirement.primary_insurance_cents(earnings, BIRTH_YEAR)
    own = retirement.monthly_benefit(earnings, BIRTH_YEAR, CLAIM_AGE_MONTHS)
    spousal = retirement.spousal_benefit_cents(pia, 0, 1961, 67 * 12)
    assert spousal == retirement.to_lower_dime_cents(pia // 2) > 0
    # 2030: both claims are in force (2027 and 2028) for the whole year.
    assert rp.social_security_cents(conn, 2030) == (own + spousal) * 12
    # 2027: the worker's own claim from June; the spouse has not claimed yet.
    assert rp.social_security_cents(conn, 2027) == own * 7


def test_the_earnings_test_withholds_while_a_salary_runs_before_fra(conn, plan):
    """Audit: benefits were paid in full beside a salary. Claimed at 62 with
    wages far over the exempt amount, nothing is paid; at full retirement age
    the benefit is recomputed for the months withheld."""
    retirement.update_person(conn, plan["person"], planned_claim_age_months=62 * 12)
    retirement.add_income_source(conn, "ANON salary", 100_000_00, 2020, end_year=2026,
                                 kind="salary")
    earnings = retirement.earnings_map(conn, plan["person"])
    pia = retirement.primary_insurance_cents(earnings, BIRTH_YEAR)
    assert rp.social_security_cents(conn, 2026) == 0             # all withheld
    # Claimed June 2022, so 55 months were paid before full retirement age
    # (June 2027) and every one was withheld: from 2028 the benefit is the
    # one for a claim 55 months later.
    later = retirement.monthly_benefit(earnings, BIRTH_YEAR, 62 * 12 + 55)
    assert rp.social_security_cents(conn, 2028) == later * 12
    assert retirement.monthly_benefit(earnings, BIRTH_YEAR, 62 * 12) < later < pia


def test_prior_year_magi_sets_the_first_two_irmaa_years(page, conn, plan):
    """Audit: the plan's first two premium years charged nothing, having no
    year of their own two back. The return the household filed is typed."""
    assert 2026 not in page.irmaa_by_year()
    retirement.set_prior_magi(conn, 2024, 300_000_00)
    retirement.set_ssa44_appeal(conn, False)        # no appeal to the year's own
    page.refresh()
    cents, tier, months, source = page.irmaa_by_year()[2026]
    assert cents > 0 and tier >= 4 and months == 12 and source == 2024
    assert "from 2024 income (entered)" in page.year_details(2026)
    # The plan pays it from its start year.
    wd = page.withdrawals
    assert wd.apply_household(60_000_00, start_year=2026) is True
    assert wd.last_plan.years[0].year == 2026
    assert wd.last_plan.years[0].surcharge_cents == cents


def test_an_ira_drawn_before_59_and_a_half_comes_last_and_pays_ten_percent(page, conn, plan):
    """Audit: IRAs were spent before 59 1/2 at a rate ten points too low."""
    retirement.update_person(conn, plan["person"], birth_year=1975)
    page.refresh()
    wd = page.withdrawals
    assert wd.apply_household(60_000_00, start_year=2026) is True
    years = wd.last_plan.years
    fined = [y for y in years if y.penalty_cents]
    assert fined and fined[0].year > 2026                     # the Roth went first
    assert all(y.amounts.get(plan["ira"], 0) == 0 for y in years
               if y.year < fined[0].year)
    # At least 10% of the IRA draw: the Roth's earnings drawn that year pay it too.
    assert fined[0].penalty_cents >= fined[0].amounts[plan["ira"]] * 10 // 100 > 0
    assert all(y.penalty_cents == 0 for y in years if y.year > 2034)   # 59 1/2 in 2034
    assert "before 59 1/2" in page.withdrawal_notice.text()


def test_married_filing_separately_is_not_offered(page):
    """Audit: one household's income through the separate ladder taxed two
    people's income as one person's."""
    assert [page.filing_combo.itemData(i)
            for i in range(page.filing_combo.count())] == ["single", "joint"]


def test_a_current_employer_plan_does_not_convert_before_59_and_a_half(page, conn, plan):
    """Audit: fills converted out of a current 401(k) at any age; money there
    moves only after leaving or at 59 1/2 (IRC 401(k)(2)(B), 402A(c)(4))."""
    retirement.update_person(conn, plan["person"], birth_year=1975)
    _mark_current_employer_plan(conn, plan["ira"])
    page.refresh()
    sched = page.schedule
    assert sched.not_convertible_reason(plan["ira"], 2030) is not None
    assert sched.conversion_cap_cents(plan["ira"], 2030) == 0
    assert sched.not_convertible_reason(plan["ira"], 2035) is None      # 59 1/2 in 2034
    assert sched.set_conversion(plan["ira"], plan["roth"], 2030, 10_000_00) == 0
    assert "cannot be converted" in page.roth_notice.text()


def test_a_child_raises_the_poverty_line_and_retiring_without_a_salary_is_no_appeal(conn, plan):
    couple_only = rp.aca_poverty_line_in(conn, 2027, "single")
    retirement.add_person(conn, "ANON Child", "child", birth_year=2015, birth_month=1)
    assert rp.aca_poverty_line_in(conn, 2027, "single") > couple_only
    retirement.set_withdrawal_plan(conn, 40_000_00, 0, start_year=2028)
    assert rp.appealed_premium_years(conn) == {}                  # no salary ends
    retirement.add_income_source(conn, "ANON salary", 90_000_00, 2020, end_year=2027,
                                 kind="salary")
    assert rp.appealed_premium_years(conn) == {2028: 2028, 2029: 2028}


def test_account_details_carries_the_retirement_flags(qapp, conn, plan):
    """The five fields the audit added (db._V106) reach the plan's accounts."""
    from mammon.ui.widgets import AccountDetailsDialog

    dlg = AccountDetailsDialog(ledger.get_account(conn, plan["ira"]), conn=conn)
    try:
        dlg.is_ira.setChecked(True)
        dlg.inherited_year.setValue(2022)
        dlg.inherited_after_rbd.setChecked(True)
        dlg.after_tax_basis.setText("12,000")
        ledger.update_account(conn, plan["ira"], **dlg.values())
    finally:
        dlg.setParent(None)
    acct = next(a for a in retirement.plan_accounts(conn) if a.account_id == plan["ira"])
    assert acct.is_ira and acct.inherited_death_year == 2022 and acct.inherited_after_rbd
    assert acct.after_tax_basis_cents == 12_000_00
    # An inherited account is floored on the beneficiary's schedule and never converted.
    assert acct.floored_in(2030) and acct.floored_in(2032) and not acct.floored_in(2033)
    assert retirement.account_rmd(conn, plan["ira"], 100_000_00, 2030) > 0


# ---------------------------------------------------------------------------
# the audit of 2026-09-26: the two screens, one plan
# ---------------------------------------------------------------------------
def test_plan_rows_for_a_past_year_do_not_pull_the_horizon_back(page, conn, plan):
    """After a New Year the rows of the year gone by are still on file; the
    horizon starts THIS year regardless, and today's value sits at this year
    rather than at the start of last year with last year's draw taken again."""
    retirement.set_withdrawal(conn, plan["ira"], TODAY.year - 1, 5_000_00)
    page.refresh()
    rows = page.income_rows()
    assert rows[0].year == TODAY.year
    assert rp.plan_horizon(conn, rp.DEFAULT_TERMINAL_AGE, TODAY)[0] == TODAY.year
    start = sum(max(0, rp.account_value_cents(conn, a.account_id))
                for a in retirement.spending_accounts(conn))
    assert rows[0].fund_value_cents == start


def test_plan_last_year_is_the_planners_own_last_bar(conn, plan):
    age = retirement.get_plan_through_age(conn)
    assert rp.plan_last_year(conn, TODAY) == rp.plan_horizon(conn, age, TODAY)[-1] \
        == BIRTH_YEAR + age
    retirement.set_conversion(conn, plan["ira"], plan["roth"], BIRTH_YEAR + age + 3,
                              1_000_00)
    assert rp.plan_last_year(conn, TODAY) == rp.plan_horizon(conn, age, TODAY)[-1] \
        == BIRTH_YEAR + age + 3


def test_every_plan_write_announces_itself_to_the_dashboard(qapp, conn, plan):
    """One signal for every write the page makes, the seeded minimums and the
    mix included: wiring the two editors alone left the dashboard drawing a
    plan without the minimums a birth year had just seeded."""
    heard = []
    page = RetirementPlannerPage(conn, today=TODAY)
    page.planChanged.connect(lambda: heard.append(True))
    try:
        page.refresh()                          # seeds the required minimums
        assert heard, "seeding wrote rows and said nothing"
        heard.clear()
        assert page.apply_plan_mix((70, 25, 5)) is True
        assert heard
        heard.clear()
        page.terminal_age.setValue(page.terminal_age.value() + 1)
        assert heard
        heard.clear()
        page.withdrawals.changed.emit()
        page.schedule.changed.emit()
        assert len(heard) == 2
    finally:
        _dispose(page, qapp)


def test_the_fund_line_projects_at_the_typed_mix_not_the_ladders_rung(conn, plan):
    """A typed all-bond plan is projected at all bonds - not at the ladder
    rung of equal volatility, a 37/23/40 mix with a higher mean - and the
    first year is the months left in it."""
    retirement.set_plan_mix(conn, (0, 100, 0))
    ids = [plan["ira"], plan["roth"]]
    line = rp.fund_values_cents(conn, ids, [0] * 5, start_cents=100_000_00,
                                as_of=TODAY.isoformat())
    mu, sigma = forecast.portfolio_moments(retirement.plan_mix_weights((0, 100, 0)))
    first = forecast.first_year_periods(TODAY)
    assert first == 3
    steps = forecast.steps_from_annual([0] * 5, mu, sigma, first_year_periods=first)
    expected = [p.p50 for p in forecast.fan_from_steps(
        100_000_00, steps, first_year_periods=first)[:5]]
    assert line == expected
    rung_mu, rung_sigma = forecast.portfolio_moments(
        forecast.mix_for_risk(retirement.get_planning_risk(conn)))
    assert rung_sigma == pytest.approx(sigma, abs=1e-6)
    assert rung_mu > mu + 0.003                   # the rung's mean is not the mix's
    assert rp.measured_risk(conn, ids, TODAY.isoformat()) == pytest.approx(
        retirement.get_planning_risk(conn), abs=1e-9)


def test_a_fill_can_aim_at_the_aca_cliff(page, conn, plan):
    """The ACA cliff is in the fill list after the IRMAA tiers, named as the
    chart names it. Years no one buys marketplace coverage have no cliff."""
    combo = page.schedule.fill_bracket
    assert combo.itemText(combo.count() - 1) == "ACA cliff"
    assert combo.itemData(combo.count() - 1) == rp.ACA_FILL_KEY
    retirement.add_person(conn, "ANON Spouse", "spouse", birth_year=1970, birth_month=1)
    retirement.set_aca_benchmark_cents(conn, 20_000_00)
    retirement.set_withdrawal_plan(conn, 40_000_00, 0, start_year=2026)
    at = [page.filing_combo.itemData(i) for i in range(page.filing_combo.count())]
    page.filing_combo.setCurrentIndex(at.index("joint"))
    page.refresh()
    assert page.fill_years_to_bracket(2027, 2027, rp.ACA_FILL_KEY) > 0
    converted = sum(int(r["amount_cents"])
                    for r in retirement.list_conversions(conn, 2027))
    cliff = page.aca_line()[2027]
    assert cliff - 1_00 <= page.taxable_with(2027, converted) <= cliff
    # What the credit actually tests: MAGI at, never over, 400% of poverty.
    limit = rp.aca_cliff_in(conn, 2027, "joint")
    assert limit - 1_00 <= page.aca_magi(2027, converted) <= limit
    # The line is where MAGI reaches the cliff whatever the year's base
    # income: it no longer moves as the plan re-applies its draws.
    assert page.aca_line()[2027] == cliff
    assert "the ACA cliff, landing at or just under it:" in page.roth_notice.text()
    # Both on Medicare by 2036: no cliff, nothing changed, and it says why.
    before = retirement.list_conversions(conn)
    assert page.fill_years_to_bracket(2036, 2037, rp.ACA_FILL_KEY) == 0
    assert retirement.list_conversions(conn) == before
    assert "no ACA cliff to fill to" in page.roth_notice.text()


def test_a_fill_says_when_the_re_applied_plan_changed_another_year(page, conn, plan):
    """Reported: filling 2042 to IRMAA 1 and the 2031 conversion disappeared.
    Re-applying the household plan re-checks every year's conversions against
    what its source holds then, and the fill's own message used to replace the
    note saying so. The fill now names every other year it changed."""
    wd = page.withdrawals
    assert wd.apply_household(40_000_00) is True
    page.refresh()
    year = page.rows[0].year + 4
    later = year + 3
    retirement.set_conversion(conn, plan["ira"], plan["roth"], later, 30_000_00)
    real = wd.apply_household

    def trims_later(*args, **kwargs):             # what the trim step does
        done = real(*args, **kwargs)
        if retirement.get_conversion(conn, plan["ira"], plan["roth"], later):
            retirement.set_conversion(conn, plan["ira"], plan["roth"], later, 12_000_00)
        return done
    wd.apply_household = trims_later
    page._fill_to_bracket(year, page.irmaa_fill_tops(1, [year])[year])
    text = page.roth_notice.text()
    assert "also changed later years' conversions" in text
    assert f"{later} from $30,000.00 to $12,000.00" in text
    # Removed outright, it says removed; and Fill years says it too.
    retirement.set_conversion(conn, plan["ira"], plan["roth"], later, 30_000_00)

    def removes_later(*args, **kwargs):
        done = real(*args, **kwargs)
        retirement.delete_conversion(conn, plan["ira"], plan["roth"], later)
        return done
    wd.apply_household = removes_later
    page.fill_years_to_bracket(year, year, "12")
    assert f"{later} ($30,000.00) removed" in page.roth_notice.text()
    # A fill that changes no other year says nothing of the kind.
    wd.apply_household = real
    page.fill_years_to_bracket(year + 1, year + 1, "12")
    assert "later years" not in page.roth_notice.text()


def _plan_rows(conn):
    """Every stored conversion and withdrawal, keyed by (year, accounts)."""
    conversions = {(int(r["year"]), int(r["from_account_id"]), int(r["to_account_id"])):
                   int(r["amount_cents"]) for r in retirement.list_conversions(conn)}
    withdrawals = {(int(r["year"]), int(r["account_id"])): int(r["amount_cents"])
                   for r in retirement.list_withdrawals(conn)}
    return conversions, withdrawals


def _before(rows, year):
    conversions, withdrawals = rows
    return ({k: v for k, v in conversions.items() if k[0] < year},
            {k: v for k, v in withdrawals.items() if k[0] < year})


@pytest.mark.parametrize("how", ["click", "fill", "fill years"])
def test_filling_a_year_never_touches_an_earlier_year(page, conn, plan, how):
    """Reported: "When I click on a line, no years before that year should be
    affected at all." A fill re-applies the household plan to measure its
    year, and that re-apply used to rewrite every year's draws and trim every
    year's conversions - filling 2031 cut 2027's and 2030's. Earlier years
    here are deliberately STALE (a typed Roth draw in a year that also
    converts, which the plan's netting rule would fold into the conversion;
    a typed IRA draw the plan would not choose), so a full re-apply would
    change them; the fill must leave them exactly as they were."""
    wd = page.withdrawals
    assert wd.apply_household(40_000_00) is True
    page.refresh()
    first = page.rows[0].year
    year = first + 5
    retirement.set_conversion(conn, plan["ira"], plan["roth"], first + 2, 20_000_00)
    retirement.set_withdrawal(conn, plan["roth"], first + 2, 12_345_00)
    retirement.set_withdrawal(conn, plan["ira"], first + 1, 1_111_00)
    page.refresh()
    before = _before(_plan_rows(conn), year)
    edge = page.irmaa_fill_tops(1, [year])[year]
    if how == "click":
        chart = page.roth_chart
        event = types.SimpleNamespace(inaxes=chart.axes, xdata=float(year),
                                      ydata=edge / 100.0, button=1)
        chart.on_click(event)
    elif how == "fill":
        page._fill_to_bracket(year, edge)
    else:
        page.fill_years_to_bracket(year, year + 2, rp.irmaa_fill_key(1))
    assert sum(int(r["amount_cents"])
               for r in retirement.list_conversions(conn, year)) > 0
    assert _before(_plan_rows(conn), year) == before
    # The test has teeth: a full re-apply DOES change those earlier years.
    page._income_changed()
    assert _before(_plan_rows(conn), year) != before


def test_this_years_conversion_leaves_the_ira_in_full_for_the_cap_and_the_plan(
        page, conn, plan):
    """Reported: clicking 2031's IRMAA line stopped short of it. This year's
    flows were being taken as a yearly RATE for the months left, so the
    conversion cap moved a quarter of this year's conversion out of the IRA
    while the household plan moved all of it: the cap saw money the plan had
    already converted, a fill split its conversion by that, and the plan cut
    it back. A conversion planned for this year is an amount to move in full;
    both projections now agree on what is left."""
    wd = page.withdrawals
    assert wd.apply_household(30_000_00) is True
    retirement.set_conversion(conn, plan["ira"], plan["roth"], TODAY.year,
                              IRA_BALANCE // 2)
    page.refresh()
    wd.invalidate_plan()
    later = TODAY.year + 3
    p = retirement.get_withdrawal_plan(conn)
    entry = next(e for e in wd.plan_for(p.start_cents, p.increase_pct).years
                 if e.year == later)
    held = int(entry.balances[plan["ira"]]) - int(entry.amounts.get(plan["ira"], 0))
    cap = page.schedule.conversion_cap_cents(plan["ira"], later)
    assert abs(cap - held) <= max(1_000_00, held // 50), (cap, held)


def test_re_applying_the_plan_changes_nothing_before_a_later_change(page, conn, plan):
    """Reported: "Re-applying the plan should produce exactly the same results
    in years before the change." Two properties, checked on the whole plan
    with no fill-only protection: re-applying an unchanged plan reproduces it
    exactly, and changing one year's conversion leaves every earlier year's
    draws and conversions exactly as they were."""
    wd = page.withdrawals
    assert wd.apply_household(45_000_00, "2", bracket_rate="12") is True
    first = wd.apply_years()[0]
    for offset, cents in ((1, 30_000_00), (4, 40_000_00), (7, 25_000_00)):
        retirement.set_conversion(conn, plan["ira"], plan["roth"], first + offset, cents)
    page._income_changed()
    settled = _plan_rows(conn)
    page._income_changed()
    assert _plan_rows(conn) == settled, "an unchanged plan re-applied moved"
    for offset in (4, 7):
        year = first + offset
        before = _before(_plan_rows(conn), year)
        retirement.set_conversion(conn, plan["ira"], plan["roth"], year, 5_000_00)
        page._income_changed()
        assert _before(_plan_rows(conn), year) == before, year


def _fill_setup(conn, plan):
    """A household plan on file and a page on it: what every solver test fills."""
    page = RetirementPlannerPage(conn, today=TODAY)
    page.refresh()
    assert page.withdrawals.apply_household(40_000_00, "2") is True
    page.refresh()
    return page


def test_the_fill_solver_writes_the_plan_once_and_lands_every_year(qapp, conn, plan,
                                                                    monkeypatch):
    """Reported: "Implement the breakpoint method. It should result in much
    faster calculation and should be deterministic." Each round is the plan
    COMPUTED, never written; the plan is written once, at the end. Every year
    lands in the window under its line (a cliff is never crossed), or stops
    where its accounts can convert no more."""
    page = _fill_setup(conn, plan)
    try:
        wd = page.withdrawals
        writes, computed = [], []
        real_apply, real_plan = wd.apply_household, wd.plan_for
        monkeypatch.setattr(wd, "apply_household",
                            lambda *a, **k: (writes.append(1), real_apply(*a, **k))[1])
        monkeypatch.setattr(wd, "plan_for",
                            lambda *a, **k: (computed.append(1), real_plan(*a, **k))[1])
        first = page.rows[0].year + 3
        page.fill_years_to_bracket(first, first + 3, "22")
        assert len(writes) == 1                       # not once per round
        assert len(computed) <= page.FILL_MAX_ROUNDS + 4
        pct = retirement.get_bracket_index_pct(conn)
        for year in range(first, first + 4):
            top = retirement.bracket_top_cents("22", page.status_in(year), year=year,
                                               index_pct=pct)
            have = sum(int(r["amount_cents"]) for r in retirement.list_conversions(conn, year))
            taxable = page.taxable_with(year, have)
            caps = sum(page.schedule.conversion_cap_cents(a.account_id, year)
                       for a in page.schedule.sources())
            # Short of the line only when it converted all its sources hold.
            assert top - 1_00 <= taxable <= top or have >= caps - 1_00, \
                (year, taxable, top, have, caps)
    finally:
        _dispose(page, qapp)


def test_the_fill_solver_starts_from_the_formula_and_is_deterministic(qapp, tmp_path):
    """The first trial is the formula - a gap of A at marginal rate r converts
    A(1 - r), the rest paying the tax on the whole gap - and the same ledger
    filled twice gives the same plan to the cent."""
    results = []
    for n in range(2):
        conn = fresh_db(tmp_path / f"solver{n}.db")
        plan_ids = globals()["plan"].__wrapped__(conn)
        page = _fill_setup(conn, plan_ids)
        try:
            year = page.rows[0].year + 4
            top = retirement.bracket_top_cents(
                "22", page.status_in(year), year=year,
                index_pct=retirement.get_bracket_index_pct(conn))
            have = sum(int(r["amount_cents"]) for r in retirement.list_conversions(conn, year))
            taxable = page.taxable_with(year, have)
            rate = Decimal(retirement.marginal_bracket(
                taxable, page.status_in(year)).rate_label.rstrip("%")) / 100
            formula = have + int((Decimal(top - page.FILL_AIM_UNDER_CENTS - taxable)
                                  * (1 - rate)).to_integral_value(rounding="ROUND_DOWN"))
            asked = []
            real = page.schedule.set_total_conversion
            page.schedule.set_total_conversion = (
                lambda y, cents, **k: (asked.append((y, cents)), real(y, cents, **k))[1])
            page._fill_to_bracket(year, top)
            assert asked[0] == (year, formula)
            results.append(_plan_rows(conn))
        finally:
            _dispose(page, qapp)
            conn.close()
    assert results[0] == results[1]

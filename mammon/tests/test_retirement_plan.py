"""The retirement plan's engine, with no screen (mammon/retirement_plan.py).

The page's editors inherit this engine and the MCP surface builds one without a
widget, so the property that matters is that the two agree: a headless
:class:`HouseholdPlanner` must compute the same household plan, year for year
and cent for cent, as the page's own withdrawal schedule. Every figure is
synthetic - an "ANON" household - and nothing here comes from a real ledger.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import datetime as _dt
import subprocess
import sys

import pytest

from mammon import ledger, mcp_server, rebalance, retirement, retirement_plan
from mammon.tests import fresh_db

TODAY = _dt.date(2026, 9, 23)


@pytest.fixture
def dbfile(tmp_path):
    return tmp_path / "plan.db"


@pytest.fixture
def conn(dbfile):
    c = fresh_db(dbfile)
    yield c
    c.close()


@pytest.fixture
def household(conn):
    """An IRA, a Roth, a taxable brokerage account, a couple with earnings,
    a pension, a conversion, and a stored household spending plan."""
    ira = ledger.create_account(conn, "ANON Rollover IRA", "investment")
    roth = ledger.create_account(conn, "ANON Roth IRA", "investment")
    brokerage = ledger.create_account(conn, "ANON Brokerage", "investment")
    rebalance.set_account_treatment(conn, ira, "deferred")
    rebalance.set_account_treatment(conn, roth, "roth")
    ledger.add_transaction(conn, ira, "2024-01-05", 600_000_00)
    ledger.add_transaction(conn, roth, "2024-01-05", 150_000_00)
    ledger.add_transaction(conn, brokerage, "2024-01-05", 250_000_00)
    me = retirement.add_person(conn, "ANON Saver", "self", birth_year=1962,
                               birth_month=4, planned_claim_age_months=67 * 12)
    spouse = retirement.add_person(conn, "ANON Partner", "spouse", birth_year=1964,
                                   birth_month=9, planned_claim_age_months=65 * 12)
    for year in range(2010, 2026):
        retirement.set_earnings(conn, me, year, 90_000_00)
        retirement.set_earnings(conn, spouse, year, 40_000_00)
    retirement.set_account_owner(conn, ira, me)
    retirement.set_account_owner(conn, roth, me)
    retirement.add_income_source(conn, "ANON Pension", 12_000_00, 2028, kind="pension")
    retirement.set_conversion(conn, ira, roth, 2027, 30_000_00)
    retirement.set_withdrawal_plan(conn, 70_000_00, "2.5", start_year=2028,
                                   bracket_rate="22")
    conn.commit()
    return {"ira": ira, "roth": roth, "brokerage": brokerage, "me": me}


def test_the_engine_imports_no_qt_and_no_ui():
    """The MCP server imports this module; a Qt import here would put a GUI
    toolkit in a headless process, and a mammon.ui import would invert the
    layering the whole codebase depends on."""
    code = ("import sys, mammon.retirement_plan, mammon.mcp_tools\n"
            "bad = sorted(m for m in sys.modules if m.startswith(('PyQt5', 'mammon.ui', "
            "'matplotlib.backends.backend_qt')))\n"
            "assert not bad, bad\n")
    subprocess.run([sys.executable, "-c", code], check=True,
                   cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def _years(plan):
    return [(e.year, e.target_cents, e.drawn_cents, e.floor_cents, e.pool_cents,
             dict(e.amounts), e.tax_cents, e.surcharge_cents, e.gains_cents,
             e.penalty_cents) for e in plan.years]


def test_a_headless_planner_computes_the_pages_household_plan(conn, household):
    """The page's withdrawal schedule and a HouseholdPlanner, over the same
    stored plan, produce the same plan to the cent - both the stored one and
    a what-if amount. This is the whole reason the engine was extracted: the
    model connected over MCP and the user at the page must not see two
    different retirements."""
    from PyQt5.QtWidgets import QApplication

    from mammon.ui.retirement_planner import RetirementPlannerPage

    app = QApplication.instance() or QApplication([])
    page = RetirementPlannerPage(conn, today=TODAY)
    try:
        page.refresh()
        wd = page.withdrawals
        params = retirement.get_withdrawal_plan(conn)
        # Written, so the bars (and the tax on them) are the plan's draws.
        assert wd.apply_household(params.start_cents, params.increase_pct,
                                  start_year=params.start_year,
                                  bracket_rate=params.bracket_rate)
        page.refresh()
        on_page = wd.plan_for(params.start_cents, params.increase_pct,
                              start_year=params.start_year,
                              bracket_rate=params.bracket_rate)
        what_if_page = wd.plan_for(55_000_00, 2, start_year=2029)
        assert len(on_page.years) > 20 and any(e.tax_cents for e in on_page.years)

        headless = retirement_plan.HouseholdPlanner(
            conn, today=TODAY, terminal_age=page.terminal_age_case(),
            filing_status=page.filing_status())
        assert headless.apply_years() == wd.apply_years()
        assert headless._years == wd.years_shown()
        stored = headless.plan_for(params.start_cents, params.increase_pct,
                                   start_year=params.start_year,
                                   bracket_rate=params.bracket_rate)
        assert _years(stored) == _years(on_page)
        assert stored.depleted_year == on_page.depleted_year
        assert _years(headless.plan_for(55_000_00, 2, start_year=2029)) == \
            _years(what_if_page)

        # The page's "Estimated federal income tax across the plan" is
        # PlanTaxes over the bars it draws - computed headless, it is the
        # same to the cent, nominal and in today's dollars.
        bars = retirement_plan.income_rows(conn, terminal_age=page.terminal_age_case(),
                                           today=TODAY)
        taxes = retirement_plan.PlanTaxes(conn, bars, stored, page.filing_status(),
                                          headless)
        assert taxes.tax_by_year() == page.tax_by_year()
        assert taxes.tax_totals(TODAY.year) == page.tax_totals()
        assert page.tax_totals()[0] > 0
    finally:
        for _ in range(3):
            app.processEvents()
        page.deleteLater()
        app.processEvents()


def test_the_headless_planner_runs_on_a_connection_that_cannot_write(dbfile, conn,
                                                                     household):
    """The MCP connection is query_only. Computing a plan, a what-if and the
    most the plan sustains must not need a single write - a write slipping
    into the engine fails here instead of in front of a user's model."""
    ro = mcp_server.open_readonly(str(dbfile))
    try:
        before = ro.total_changes
        with retirement_plan.inflows_measured_once():
            planner = retirement_plan.HouseholdPlanner(ro, today=TODAY)
            params = retirement.get_withdrawal_plan(ro)
            plan = planner.plan_for(params.start_cents, params.increase_pct,
                                    start_year=params.start_year,
                                    bracket_rate=params.bracket_rate)
            assert plan.years
            assert planner.most_we_can_spend(2, start_year=2028) > 0
            assert retirement_plan.income_rows(ro, today=TODAY)
        assert ro.total_changes == before
    finally:
        ro.close()


def test_the_planner_defaults_to_the_stored_terminal_age(conn, household):
    retirement.set_plan_through_age(conn, 85)
    planner = retirement_plan.HouseholdPlanner(conn, today=TODAY)
    assert planner.terminal_age == 85
    assert planner._years[-1] == 1962 + 85
    # A required minimum is owed whether or not a year is on screen, so the
    # plan itself runs through age 100.
    assert planner.apply_years()[-1] == 1962 + retirement_plan.PLAN_WRITTEN_THROUGH_AGE
    assert retirement_plan.HouseholdPlanner(conn, today=TODAY,
                                            terminal_age=95)._years[-1] == 1962 + 95

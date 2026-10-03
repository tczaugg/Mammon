"""Savings goals end to end: mammon.goals, reports.budget.goal_table and the
Budget Planner's Save & Pay Down tab (SRD 5.12g).

The load-bearing claim of the whole feature is that a goal is measured by its
OWN funding, not by the spending report. Funding a goal is a transfer, and every
spending aggregation in this codebase excludes transfers on purpose; inverting
that rule for savings would double-count every dollar that moves between two
accounts the household owns. So the goal owns its own query, and the test below
asserts both halves at once: the transfer funds the goal, and the same transfer
is invisible to the same period's actuals.

All fixtures here are synthetic - invented account names, round invented amounts
and dates in an invented year.
"""
from __future__ import annotations

import datetime as _dt
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mammon import budgets, goals, ledger
from mammon.reports import budget as budget_report
from mammon.tests import fresh_db

#: A fixed "today" so months left and the required contribution are the same
#: numbers on every run. Mid-month, because a goal is usually opened mid-month.
TODAY = _dt.date(2031, 3, 12)

#: The synthetic goal: 2,500.00 over the three months March, April and May.
TARGET_CENTS = 250_000
TARGET_DATE = "2031-05-31"


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "savings.db")
    yield c
    c.close()


@pytest.fixture
def book(conn):
    """A checking account, a savings account and an opening balance."""
    checking = ledger.create_account(conn, "Everyday Checking", "checking")
    savings = ledger.create_account(conn, "Set Aside", "savings")
    ledger.add_transaction(conn, checking, "2031-01-02", 800_000,
                           payee="Opening Balance")
    return {"checking": checking, "savings": savings}


def _goal(conn, book, **over):
    kwargs = dict(target_date=TARGET_DATE, account_id=book["savings"],
                  baseline_cents=0, monthly_cents=0)
    kwargs.update(over)
    return goals.create_goal(conn, "Set Aside Goal", TARGET_CENTS, **kwargs)


# ---- the phase-5 end-to-end test -------------------------------------------
def test_account_backed_goal_over_transfers(conn, book):
    """An account-backed goal funded by transfers, end to end.

    Three assertions, in the order the feature depends on them:

    1. ``funded`` is the backing account's balance less the baseline.
    2. ``required_cents`` divides by ceiling, and the month-by-month schedule
       sums to the target EXACTLY - 833.34, 833.34, 833.32, never three times
       833.33, which lands a penny short of what the household was promised.
    3. the funding transfer does not appear as spending in the same period's
       actuals.
    """
    goal_id = _goal(conn, book)

    # Nothing moved yet: funded is zero and the whole target is required.
    start = goals.goal_progress(conn, goal_id, TODAY.isoformat())
    assert start.funded_cents == 0
    assert start.remaining_cents == TARGET_CENTS
    assert start.months_left == 3                    # March through May
    assert start.required_cents == 83_334            # ceil(250000 / 3)
    assert list(start.schedule) == [83_334, 83_334, 83_332]
    assert sum(start.schedule) == TARGET_CENTS       # and never 249_999

    # (2) restated on the pure function, away from any database state.
    assert goals.contribution_plan(TARGET_CENTS, 3) == [83_334, 83_334, 83_332]
    assert sum(goals.contribution_plan(TARGET_CENTS, 3)) == TARGET_CENTS

    # Fund it: two transfers from checking into the backing account.
    ledger.create_transfer(conn, book["checking"], book["savings"],
                           "2031-03-05", 83_334)
    ledger.create_transfer(conn, book["checking"], book["savings"],
                           "2031-03-20", 16_666)

    # (1) funded is the backing account's balance less the baseline.
    balance = ledger.account_balance(conn, book["savings"], TODAY.isoformat())
    # Only the first transfer is on or before "today"; the second is later in
    # the month, so as_of really does bound the balance.
    assert balance == 83_334
    assert goals.funded_cents(conn, goal_id, TODAY.isoformat()) == balance

    end_of_month = "2031-03-31"
    assert ledger.account_balance(conn, book["savings"], end_of_month) == 100_000
    assert goals.funded_cents(conn, goal_id, end_of_month) == 100_000

    after = goals.goal_progress(conn, goal_id, end_of_month)
    assert after.funded_cents == 100_000
    assert after.remaining_cents == 150_000
    assert after.required_cents == 50_000            # ceil(150000 / 3)

    # The baseline is money that was already there and is not this goal's work.
    goals.update_goal(conn, goal_id, baseline_cents=20_000)
    assert goals.funded_cents(conn, goal_id, end_of_month) == 80_000
    assert (goals.goal_progress(conn, goal_id, end_of_month).funded_cents
            == 100_000 - 20_000)
    goals.update_goal(conn, goal_id, baseline_cents=0)

    # The month's own funding, which is what the tab shows in "This month".
    assert goals.month_funding(conn, goal_id, "2031-03") == 100_000

    # (3) the funding transfer is not spending. Budget the category the goal
    # would use, spend some real money in it, and the actuals must show the
    # spending alone.
    budget_id = budgets.create_budget(conn, "Household Plan")
    category_id = ledger.resolve_category(conn, "Home:Repairs")
    budgets.set_line(conn, budget_id, category_id, "2031-03", 30_000)
    ledger.add_transaction(conn, book["checking"], "2031-03-18", -12_500,
                           payee="Hardware Store", category_id=category_id)

    rows = {r.category_id: r
            for r in budgets.budget_vs_actual(conn, budget_id, "2031-03")}
    assert rows[category_id].actual_cents == 12_500, (
        "the 1,000.00 of transfers into the goal leaked into spending")
    total_actual = sum(r.actual_cents for r in rows.values())
    assert total_actual == 12_500, (
        "something counted a transfer as spending: actuals should hold only "
        "the hardware-store purchase")

    # ... while the goal, asked its own question, sees all 1,000.00 of it.
    assert goals.month_funding(conn, goal_id, "2031-03") == 100_000


# ---- funding modes ----------------------------------------------------------
def test_allocated_goal_sums_its_annotations(conn, book):
    """A goal with no backing account is funded by annotations on transactions.

    Allocating writes to ``savings_goal_allocations`` and NOTHING else: the
    transaction's own amount, account and category are untouched, because
    ``ledger`` is the only writer of transaction rows.
    """
    goal_id = goals.create_goal(conn, "Shared Sinking Fund", 50_000,
                                target_date=TARGET_DATE)
    txn_id = ledger.add_transaction(conn, book["checking"], "2031-03-07",
                                    -15_000, payee="Set Aside")
    before = conn.execute("SELECT amount, account_id, category_id "
                          "FROM transactions WHERE id = ?", (txn_id,)).fetchone()

    goals.allocate(conn, goal_id, txn_id, 15_000)
    assert goals.funded_cents(conn, goal_id, "2031-03-31") == 15_000
    assert [a.txn_id for a in goals.allocations(conn, goal_id)] == [txn_id]

    after = conn.execute("SELECT amount, account_id, category_id "
                         "FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    assert tuple(after) == tuple(before), "allocating rewrote the transaction"

    # Re-allocating the same transaction replaces the amount, never duplicates.
    goals.allocate(conn, goal_id, txn_id, 20_000)
    assert len(goals.allocations(conn, goal_id)) == 1
    assert goals.funded_cents(conn, goal_id, "2031-03-31") == 20_000

    # Zero means "clear this", which is what the user means by blanking it.
    goals.allocate(conn, goal_id, txn_id, 0)
    assert goals.allocations(conn, goal_id) == []
    assert goals.funded_cents(conn, goal_id, "2031-03-31") == 0


def test_account_backed_goal_refuses_allocations(conn, book):
    """The two funding modes are exclusive: allowing both on one goal would
    count the same dollars twice, once in the balance and once in the sum."""
    goal_id = _goal(conn, book)
    txn_id = ledger.add_transaction(conn, book["checking"], "2031-03-07",
                                    -15_000, payee="Set Aside")
    with pytest.raises(ValueError):
        goals.allocate(conn, goal_id, txn_id, 15_000)


def test_suggest_allocations_offers_the_receiving_leg(conn, book):
    """Suggestions are the receiving leg of a transfer out of a cash account -
    the shape of "money left checking for something that is not spending" - and
    they never write anything."""
    goal_id = goals.create_goal(conn, "Shared Sinking Fund", 50_000)
    ledger.create_transfer(conn, book["checking"], book["savings"],
                           "2031-03-05", 25_000)
    found = goals.suggest_allocations(conn, goal_id, "2031-03-01", "2031-03-31")
    assert [c.amount_cents for c in found] == [25_000]
    assert [c.account_id for c in found] == [book["savings"]]
    assert goals.allocations(conn, goal_id) == [], "suggesting wrote something"

    # A leg another goal already claims is not offered again.
    other = goals.create_goal(conn, "Other Fund", 50_000)
    goals.allocate(conn, other, found[0].txn_id, 25_000)
    assert goals.suggest_allocations(
        conn, goal_id, "2031-03-01", "2031-03-31") == []


# ---- states -----------------------------------------------------------------
def test_the_three_states(conn, book):
    """On pace, behind, and no deadline. The state is a readout, never advice:
    it says where the goal stands and leaves the decision to the household."""
    goal_id = _goal(conn, book, monthly_cents=83_334)
    on_pace = goals.goal_progress(conn, goal_id, TODAY.isoformat())
    assert on_pace.state == goals.ON_PACE
    assert on_pace.shortfall_cents == 0

    goals.update_goal(conn, goal_id, monthly_cents=50_000)
    behind = goals.goal_progress(conn, goal_id, TODAY.isoformat())
    assert behind.state == goals.BEHIND
    assert behind.shortfall_cents == 83_334 - 50_000

    # No target date: no required contribution to be behind, just a projection.
    goals.update_goal(conn, goal_id, target_date=None, monthly_cents=50_000)
    open_ended = goals.goal_progress(conn, goal_id, TODAY.isoformat())
    assert open_ended.state == goals.NO_DEADLINE
    assert open_ended.months_left is None
    assert open_ended.required_cents == 0
    assert open_ended.projected_month == "2031-07"   # ceil(250000/50000) = 5

    # ... and with nothing planned, no projection at all.
    goals.update_goal(conn, goal_id, monthly_cents=0)
    unfunded = goals.goal_progress(conn, goal_id, TODAY.isoformat())
    assert unfunded.state == goals.NO_DEADLINE
    assert unfunded.projected_month is None


def test_an_overdue_goal_reports_this_month_not_a_zero_divide(conn, book):
    """Past its date and short: months_left floors at one, so the goal says
    what finishing now would take instead of dividing by zero."""
    goal_id = _goal(conn, book, monthly_cents=10_000)
    late = goals.goal_progress(conn, goal_id, "2031-09-04")
    assert late.months_left == 1
    assert late.required_cents == TARGET_CENTS
    assert late.state == goals.BEHIND


def test_a_finished_goal_is_complete(conn, book):
    goal_id = _goal(conn, book)
    ledger.create_transfer(conn, book["checking"], book["savings"],
                           "2031-03-05", TARGET_CENTS)
    done = goals.goal_progress(conn, goal_id, "2031-03-31")
    assert done.complete is True
    assert done.remaining_cents == 0
    assert done.required_cents == 0
    assert done.projected_month == "2031-03"
    assert done.percent == pytest.approx(1.0)


# ---- the cash floor ---------------------------------------------------------
def test_a_contribution_is_floor_tested_before_it_is_promised(conn, book):
    """A proposed contribution goes through the ONE cash-floor seam,
    ``budgets.check_floor``, before anything is written - and the goal's own
    backing account is excluded from the account set, or a checking-to-savings
    contribution would net to zero inside it and look free."""
    goal_id = _goal(conn, book)
    small = goals.check_contribution(conn, goal_id, "2031-03-20", 50_000,
                                     today=TODAY.isoformat())
    assert small.breaks is False

    huge = goals.check_contribution(conn, goal_id, "2031-03-20", 900_000,
                                    today=TODAY.isoformat())
    assert huge.breaks is True, (
        "moving more than the household has must break the floor; if it does "
        "not, the backing account is still inside the floor account set")
    assert huge.low_cents < 0
    assert huge.low_date == "2031-03-20"


# ---- the budget ------------------------------------------------------------
def test_apply_to_budget_routes_by_what_the_goal_names(conn, book):
    """A goal with a category writes an ordinary budget line; an account-backed
    goal without one writes a saving line. Either way ``budgets`` stays the
    sole writer of budget tables."""
    budget_id = budgets.create_budget(conn, "Household Plan")
    category_id = ledger.resolve_category(conn, "Home:Repairs")

    with_category = _goal(conn, book, budget_id=budget_id,
                          category_id=category_id, monthly_cents=83_334,
                          account_id=None)
    assert goals.apply_to_budget(conn, with_category, ["2031-03", "2031-04"]) == 2
    lines = {ln.period: ln.amount_cents
             for ln in budgets.get_lines(conn, budget_id)
             if ln.category_id == category_id}
    assert lines == {"2031-03": 83_334, "2031-04": 83_334}

    account_backed = _goal(conn, book, budget_id=budget_id,
                           monthly_cents=50_000)
    assert goals.apply_to_budget(conn, account_backed, ["2031-03"]) == 1
    saving = budgets.get_saving_lines(conn, budget_id, period="2031-03")
    assert [(s.account_id, s.amount_cents) for s in saving] == [
        (book["savings"], 50_000)]


def test_a_goal_with_nowhere_to_land_raises(conn, book):
    """Neither a budget nor a place in it: raising beats silently doing
    nothing, which reads as a bug in the budget rather than a goal that was
    never wired up."""
    detached = _goal(conn, book, monthly_cents=50_000)
    with pytest.raises(ValueError):
        goals.apply_to_budget(conn, detached, ["2031-03"])

    budget_id = budgets.create_budget(conn, "Household Plan")
    nowhere = goals.create_goal(conn, "Loose Goal", 50_000,
                                budget_id=budget_id, monthly_cents=5_000)
    with pytest.raises(ValueError):
        goals.apply_to_budget(conn, nowhere, ["2031-03"])


def test_goal_table_is_the_report_the_tab_renders(conn, book):
    budget_id = budgets.create_budget(conn, "Household Plan")
    goal_id = _goal(conn, book, budget_id=budget_id, monthly_cents=50_000)
    ledger.create_transfer(conn, book["checking"], book["savings"],
                           "2031-03-05", 100_000)

    table = budget_report.goal_table(conn, budget_id, "2031-03",
                                     as_of="2031-03-31")
    assert [r.name for r in table.rows] == ["Set Aside Goal"]
    row = table.rows[0]
    assert row.goal_id == goal_id
    assert row.target_cents == TARGET_CENTS
    assert row.funded_cents == 100_000
    assert row.remaining_cents == 150_000
    assert row.funded_this_month_cents == 100_000
    assert row.planned_cents == 50_000
    assert row.required_cents == 50_000            # ceil(150000 / 3)
    # Funded 1,000.00 against a 500.00 plan: ahead by 500.00 this month.
    assert row.variance_cents == 50_000
    assert table.total_target_cents == TARGET_CENTS
    assert table.total_funded_cents == 100_000

    # Archived goals are out of the way by default and back on request.
    goals.archive_goal(conn, goal_id)
    assert budget_report.goal_table(conn, budget_id, "2031-03").rows == []
    assert len(budget_report.goal_table(
        conn, budget_id, "2031-03", include_archived=True).rows) == 1


# ---- the tab ---------------------------------------------------------------
@pytest.fixture
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()

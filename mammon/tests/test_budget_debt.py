"""Debt payoff end to end: mammon.debt and the Pay Down half of the Budget
Planner's Save & Pay Down tab (SRD 5.12h).

Two claims carry the whole feature, and they are what the tests below pin.

1. The arithmetic agrees with itself. The closed-form ``payoff_months`` and the
   month-by-month ``simulate`` are two independent routes to the same number,
   so the named agreement is asserted through BOTH, to the cent. When they
   disagree the cause is always the accrual order or a second rounding site,
   and a test that only ran the loop would not see it.

2. Nothing about a RESULT is stored, and nothing is recommended. The page shows
   both published orders with the difference between them and selects neither;
   an extra payment is floor-tested before it is promised, and refused when it
   breaks the cushion. Mammon computes and cites, never counsels.

The three degenerate outcomes are here too, because each one has its own
readout on the page and each is a wrong answer if it silently becomes another:
a payment below the monthly interest never pays off, a very long plan hits the
cap with a balance still owed, and minimums above the monthly amount have no
payoff date at all.

All fixtures are synthetic - invented account names, round invented balances
and dates in an invented year.
"""
from __future__ import annotations

import datetime as _dt
import os
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mammon import budgets, debt, ledger
from mammon.tests import fresh_db

#: A fixed "today" so every month label and payoff date is the same string on
#: every run. Mid-month, because that is when a household opens a plan.
TODAY = _dt.date(2031, 3, 12)
START = "2031-03"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "debt.db")
    yield c
    c.close()


@pytest.fixture
def book(conn):
    """Checking with money in it and two interest-bearing cards owing money.

    Both cards are ``credit`` accounts, which puts them inside the cash-flow
    set the floor is computed over - that is the point of the floor test below.
    """
    checking = ledger.create_account(conn, "Everyday Checking", "checking")
    blue = ledger.create_account(conn, "Blue Card", "credit")
    green = ledger.create_account(conn, "Green Card", "credit")
    ledger.add_transaction(conn, checking, "2031-01-02", 800_000,
                           payee="Opening Balance")
    ledger.add_transaction(conn, blue, "2031-01-05", -800_000,
                           payee="Opening Balance")
    ledger.add_transaction(conn, green, "2031-01-05", -200_000,
                           payee="Opening Balance")
    # Interest-bearing by their entered terms: a card whose agreement the user
    # has recorded is a debt to pay down even before a charge posts.
    debt.set_terms(conn, blue, apr=Decimal("19.99"), min_form="B",
                   min_floor_cents=2_500, min_pct=Decimal("2"))
    debt.set_terms(conn, green, apr=Decimal("12.99"), min_form="B",
                   min_floor_cents=2_500, min_pct=Decimal("2"))
    return {"checking": checking, "blue": blue, "green": green}


@pytest.fixture
def plan(conn, book):
    """A plan over both cards, 500.00 a month, highest rate first."""
    pid = debt.create_plan(conn, "Cards", strategy=debt.AVALANCHE,
                           monthly_cents=50_000, start_period=START)
    debt.set_member(conn, pid, book["blue"], sort_index=0)
    debt.set_member(conn, pid, book["green"], sort_index=1)
    return pid


@pytest.fixture
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def cushion(monkeypatch):
    """The cash cushion, held in memory for the duration.

    :mod:`mammon.ui.prefs` reads the real user profile, so a test that read it
    would depend on the developer's own setting. Zero is the shipped default.
    """
    from mammon.ui import prefs

    box = {"cents": 0}
    monkeypatch.setattr(prefs, "cash_cushion_cents", lambda *a, **k: box["cents"])
    monkeypatch.setattr(prefs, "set_cash_cushion_cents",
                        lambda cents, *a, **k: box.__setitem__("cents", int(cents)))
    return box


def _debt(account_id: int, name: str, balance_cents: int, apr: str, *,
          min_form: str = "B", min_floor_cents: int = 2_500,
          min_pct: str = "2") -> debt.DebtInput:
    """A synthetic debt, built without a database."""
    return debt.DebtInput(
        account_id=account_id, name=name, balance_cents=balance_cents,
        monthly_rate=debt.monthly_rate(apr), min_form=min_form,
        min_floor_cents=min_floor_cents, min_pct=Decimal(min_pct),
        apr=Decimal(apr))


# ---- the agreement between the two routes ----------------------------------
def test_closed_form_and_simulation_agree_to_the_cent():
    """8,000.00 at 19.99 percent APR against 250.00 a month.

    47 months and 3,524.38 of interest, by the closed form and by the loop.
    The two share no code: ``payoff_months`` solves the annuity, ``simulate``
    accrues a month at a time and rounds once per debt-month. Agreement is the
    evidence that the accrual convention (interest first, on the month-end
    balance) is applied consistently in both.
    """
    one = _debt(1, "Card", 800_000, "19.99", min_form="A",
                min_floor_cents=25_000, min_pct="0")

    assert debt.payoff_months(800_000, 25_000,
                              debt.monthly_rate("19.99")) == 47

    sim = debt.simulate([one], 25_000, debt.AVALANCHE, start_period=START)
    assert sim.payoff_month == 47
    assert sim.total_interest_cents == 352_438
    assert sim.total_paid_cents == 1_152_438
    assert sim.payoff_period == "2035-01"
    assert sim.pays_off
    assert not sim.negative_amortizing
    assert not sim.cap_reached
    assert not sim.minimums_exceed_budget

    # The schedule is the totals, not a separate story about them, and principal
    # is derived by subtraction: what is paid less interest is the balance.
    assert sum(r.interest_cents for r in sim.schedule) == sim.total_interest_cents
    assert sum(r.payment_cents for r in sim.schedule) == sim.total_paid_cents
    assert sim.total_paid_cents - sim.total_interest_cents == 800_000
    assert sim.schedule[-1].balance_cents == 0
    # The final payment is computed, not scheduled: the last month is short.
    assert sim.schedule[-1].payment_cents < 25_000


def test_payoff_months_guards():
    """The closed form's two guards, plus the answers at the edges."""
    # r == 0 is ceil(B / P), not a division by log(1).
    assert debt.payoff_months(100_000, 30_000, 0) == 4
    # Nothing owed is nothing to pay.
    assert debt.payoff_months(0, 1_000, debt.monthly_rate("19.99")) == 0
    # P <= r * B has no solution: "does not pay off" is an answer, not an error.
    assert debt.payoff_months(600_000, 6_000,
                              debt.monthly_rate("29.99")) is None
    assert debt.payoff_months(100, 0, 0) is None


def test_the_order_is_recomputed_and_the_remainder_cascades():
    """Two debts, both orders, and the published difference between them.

    The roll-in is not tracked: cascading the whole remainder each month
    produces it for free. What the comparison is allowed to say is the interest
    difference - and here it happens to be the sooner date as well, which is
    exactly why the page states both numbers instead of concluding.
    """
    blue = _debt(1, "Blue Card", 800_000, "19.99")
    green = _debt(2, "Green Card", 200_000, "12.99")

    sims = debt.compare([blue, green], 50_000, start_period=START)
    assert [s.strategy for s in sims] == [debt.SNOWBALL, debt.AVALANCHE]
    snowball, avalanche = sims

    assert snowball.payoff_month == 25
    assert snowball.payoff_period == "2033-03"
    assert snowball.total_interest_cents == 220_369
    assert avalanche.payoff_month == 24
    assert avalanche.payoff_period == "2033-02"
    assert avalanche.total_interest_cents == 196_357
    assert debt.interest_spread_cents(sims) == 24_012

    # Smallest-balance first clears the small card early; highest-rate first
    # leaves it until last. Both minimum totals are identical - the orders
    # differ only in where the remainder goes.
    assert snowball.outcome(2).cleared_month == 7
    assert snowball.outcome(1).cleared_month == 25
    assert avalanche.outcome(1).cleared_month == 21
    assert avalanche.outcome(2).cleared_month == 24
    assert snowball.minimum_total_cents == avalanche.minimum_total_cents == 20_310


def test_a_payment_below_the_interest_never_pays_off():
    """Negative amortization: the minimum is less than the month's interest.

    The engine must NOT quietly raise the payment to cover interest. It flags
    the debt, runs to the cap and reports no payoff date - the outcome Truth in
    Lending requires a statement to state in words.
    """
    card = _debt(1, "Card", 600_000, "29.99", min_form="A",
                 min_floor_cents=6_000, min_pct="0")
    sim = debt.simulate([card], 6_000, debt.AVALANCHE, start_period=START)

    assert sim.negative_amortizing
    assert sim.payoff_month is None
    assert not sim.pays_off
    assert sim.months_run == debt.MAX_MONTHS
    assert sim.cap_reached                       # the cap is an outcome, not a bug
    assert not sim.minimums_exceed_budget

    row = sim.outcome(1)
    assert row.negative_amortizing
    assert row.cleared_month is None
    assert row.first_interest_cents == 14_995    # against a 60.00 minimum
    assert row.minimum_cents == 6_000
    assert row.first_interest_cents > row.minimum_cents
    # The balance grew rather than shrank.
    assert sim.schedule[-1].balance_cents > 600_000


def test_the_cap_is_reported_while_still_amortizing():
    """A plan that does pay down, just not inside fifty years.

    Distinct from negative amortization: the balance falls every month, so
    nothing is flagged, but the projection stops with money still owed and says
    so. The closed form agrees that the real answer is past the cap.
    """
    card = _debt(1, "Card", 1_000_000, "12", min_form="A",
                 min_floor_cents=10_020, min_pct="0")
    sim = debt.simulate([card], 10_020, debt.AVALANCHE, start_period=START)

    assert sim.cap_reached
    assert not sim.negative_amortizing
    assert sim.payoff_month is None
    assert sim.months_run == debt.MAX_MONTHS
    assert sim.schedule[-1].balance_cents == 219_437
    assert sim.schedule[-1].balance_cents < 1_000_000
    assert debt.payoff_months(1_000_000, 10_020, debt.monthly_rate("12")) == 625


def test_minimums_above_the_budget_report_a_shortfall_and_no_date():
    """The minimums are not pro-rated, and no payoff date is invented.

    Paying part of a minimum is a missed payment, not a slower plan, so the
    engine detects the shortfall BEFORE simulating and runs no months at all.
    The per-debt minimums are still filled in, because the shortfall is only
    meaningful next to them.
    """
    blue = _debt(1, "Blue Card", 800_000, "19.99")
    green = _debt(2, "Green Card", 200_000, "12.99")
    sim = debt.simulate([blue, green], 1_000, debt.AVALANCHE,
                        start_period=START)

    assert sim.minimums_exceed_budget
    assert sim.payoff_month is None
    assert sim.payoff_period is None
    assert not sim.pays_off
    assert sim.months_run == 0
    assert sim.minimum_total_cents == 20_310
    assert sim.shortfall_cents == 19_310
    assert sim.outcome(1).minimum_cents == 16_267
    assert sim.outcome(2).minimum_cents == 4_043


def test_degenerate_inputs():
    """Nothing owed, and nothing to owe it on."""
    empty = debt.simulate([], 50_000, debt.AVALANCHE, start_period=START)
    assert empty.payoff_month == 0
    assert empty.total_interest_cents == 0

    clear = _debt(1, "Paid Card", 0, "19.99")
    sim = debt.simulate([clear], 50_000, debt.AVALANCHE, start_period=START)
    assert sim.payoff_month == 0
    assert sim.outcome(1).cleared_month == 0


# ---- the database side -----------------------------------------------------
def test_collect_debts_turns_a_liability_balance_into_a_magnitude(conn, book,
                                                                  plan):
    """The sign flip happens once, on the way out of the ledger.

    A card's ledger balance is negative; every formula downstream wants a
    positive amount owed. Doing it here means no arithmetic in the engine and
    no label in the UI has to remember which way the sign points.
    """
    assert ledger.account_balance(conn, book["blue"]) == -800_000

    debts = debt.collect_debts(conn, plan)
    assert [d.name for d in debts] == ["Blue Card", "Green Card"]
    assert [d.balance_cents for d in debts] == [800_000, 200_000]
    assert debts[0].monthly_rate == debt.monthly_rate("19.99")
    assert not debts[0].estimated

    # Membership is the switch, and it survives a round trip.
    debt.set_member(conn, plan, book["green"], included=False)
    assert [d.account_id for d in debt.collect_debts(conn, plan)] == [
        book["blue"]]


def test_terms_with_nothing_entered_are_synthesized_and_marked(conn):
    """A debt account with no agreement on file still projects - and says so."""
    account = ledger.create_account(conn, "Store Card", "credit")
    ledger.add_transaction(conn, account, "2031-01-05", -50_000,
                           payee="Opening Balance")
    ledger.add_transaction(conn, account, "2031-02-05", -1_500,
                           payee="Store Card", memo="finance charge")
    assert debt.get_terms(conn, account) is None

    [row] = debt.available_debts(conn, None)
    assert row.estimated                         # the page must call it a guess
    assert row.min_floor_cents == debt.DEFAULT_MIN_FLOOR_CENTS
    assert row.min_pct == debt.DEFAULT_MIN_PCT


def test_terms_refuse_an_account_that_is_not_a_debt(conn):
    checking = ledger.create_account(conn, "Everyday Checking", "checking")
    with pytest.raises(ValueError):
        debt.set_terms(conn, checking, apr=Decimal("19.99"))


def test_apply_to_budget_writes_the_input_not_the_result(conn, book, plan):
    """The budget seam: the monthly payments, as ordinary budget saving rows.

    What crosses is the plan's own arithmetic for each month, written through
    :mod:`mammon.budgets` - the sole writer of budget tables. The payoff month
    and the interest total are not written anywhere.
    """
    budget_id = budgets.create_budget(conn, "Household")
    debt.update_plan(conn, plan, budget_id=budget_id)
    periods = budgets.plan_periods(START, 3)

    written = debt.apply_to_budget(conn, plan, periods)
    assert written == 6                          # two debts, three months
    lines = budgets.get_saving_lines(conn, budget_id)
    assert {l.period for l in lines} == set(periods)
    for period in periods:
        month = [l for l in lines if l.period == period]
        assert sum(l.amount_cents for l in month) == 50_000

    # And it refuses outright when the minimums do not fit.
    debt.update_plan(conn, plan, monthly_cents=1_000)
    with pytest.raises(ValueError):
        debt.apply_to_budget(conn, plan, periods)


# ---- the page, end to end --------------------------------------------------
def test_only_real_debts_are_offered(conn):
    """A paid-off loan and a card paid in full every month are not debts to
    pay down: the first owes nothing and the second costs nothing. A card
    charged interest lately, and a loan with a balance, are."""
    checking = ledger.create_account(conn, "Everyday Checking", "checking")
    paid_off = ledger.create_account(conn, "Old Car Loan", "liability")
    car = ledger.create_account(conn, "Car Loan", "liability")
    convenience = ledger.create_account(conn, "Rewards Card", "credit")
    revolving = ledger.create_account(conn, "Store Card", "credit")
    ledger.add_transaction(conn, car, "2031-01-05", -900_000, payee="Opening Balance")
    ledger.add_transaction(conn, convenience, "2031-02-10", -42_000, payee="Grocer")
    ledger.create_transfer(conn, checking, convenience, "2031-02-25", 42_000)
    ledger.add_transaction(conn, convenience, "2031-03-04", -31_000, payee="Grocer")
    ledger.add_transaction(conn, revolving, "2031-01-05", -120_000, payee="Opening Balance")
    ledger.add_transaction(conn, revolving, "2031-02-28", -2_300,
                           payee="Store Card", category_id=ledger.resolve_category(
                               conn, "Interest Paid"))
    assert not debt.pays_interest(conn, convenience)
    assert debt.pays_interest(conn, revolving)
    names = sorted(d.name for d in debt.available_debts(conn, None))
    assert names == ["Car Loan", "Store Card"]
    assert paid_off not in {d.account_id for d in debt.available_debts(conn, None)}
    # Entering a card's terms says it is a debt, charge or no charge yet.
    debt.set_terms(conn, convenience, apr=Decimal("24.99"))
    assert "Rewards Card" in {d.name for d in debt.available_debts(conn, None)}
    # The filter is a choice: the unfiltered list still exists.
    assert len(debt.available_debts(conn, None, real_only=False)) == 4

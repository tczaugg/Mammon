"""Debt payoff: mammon.debt's engine and the account side of a Budget page
pay-down line's projection (SRD 5.12h).

The arithmetic is pinned to the cent: interest accrues first on the month-end
balance and is the one rounding site, so a known annuity lands exactly.

The three degenerate outcomes are here too, because each one has its own
readout and each is a wrong answer if it silently becomes another: a payment
below the monthly interest never pays off, a very long payoff hits the cap
with a balance still owed, and minimums above the monthly amount have no
payoff date at all.

All fixtures are synthetic - invented account names, round invented balances
and dates in an invented year.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import debt, ledger
from mammon.tests import fresh_db

START = "2031-03"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "debt.db")
    yield c
    c.close()


def _debt(account_id: int, name: str, balance_cents: int, apr: str, *,
          min_form: str = "B", min_floor_cents: int = 2_500,
          min_pct: str = "2") -> debt.DebtInput:
    """A synthetic debt, built without a database."""
    return debt.DebtInput(
        account_id=account_id, name=name, balance_cents=balance_cents,
        monthly_rate=debt.monthly_rate(apr), min_form=min_form,
        min_floor_cents=min_floor_cents, min_pct=Decimal(min_pct),
        apr=Decimal(apr))


# ---- the engine --------------------------------------------------------------
def test_the_simulation_matches_the_annuity_to_the_cent():
    """8,000.00 at 19.99 percent APR against 250.00 a month.

    47 months and 3,524.38 of interest. The month count is the closed-form
    annuity's (n = -ln(1 - rB/P) / ln(1 + r), rounded up); ``simulate`` accrues
    a month at a time and rounds once per debt-month, so landing on it is the
    evidence that the accrual convention (interest first, on the month-end
    balance) has no second rounding site.
    """
    one = _debt(1, "Card", 800_000, "19.99", min_form="A",
                min_floor_cents=25_000, min_pct="0")

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


def test_the_order_is_recomputed_and_the_remainder_cascades():
    """Two debts under both sort keys of the one engine.

    The roll-in is not tracked: cascading the whole remainder each month
    produces it for free, and the orders differ only in where it goes.
    """
    blue = _debt(1, "Blue Card", 800_000, "19.99")
    green = _debt(2, "Green Card", 200_000, "12.99")

    snowball, avalanche = (
        debt.simulate([blue, green], 50_000, strategy, start_period=START)
        for strategy in (debt.SNOWBALL, debt.AVALANCHE))

    assert snowball.payoff_month == 25
    assert snowball.payoff_period == "2033-03"
    assert snowball.total_interest_cents == 220_369
    assert avalanche.payoff_month == 24
    assert avalanche.payoff_period == "2033-02"
    assert avalanche.total_interest_cents == 196_357

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
    so. (The closed-form annuity puts the real answer at 625 months.)
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
def test_the_ledger_balance_becomes_a_positive_amount_owed(conn):
    """The sign flip happens once, on the way out of the ledger.

    A card's ledger balance is negative; every formula downstream wants a
    positive amount owed. Doing it there means no arithmetic in the engine and
    no label in the UI has to remember which way the sign points.
    """
    card = ledger.create_account(conn, "Blue Card", "credit")
    ledger.add_transaction(conn, card, "2031-01-05", -800_000,
                           payee="Opening Balance")
    debt.set_terms(conn, card, apr=Decimal("19.99"), min_form="B",
                   min_floor_cents=2_500, min_pct=Decimal("2"))
    assert ledger.account_balance(conn, card) == -800_000

    row = debt.project_debt(conn, card, 50_000, as_of="2031-03-12").outcome(card)
    assert row.balance_cents == 800_000
    assert row.apr == Decimal("19.99")
    assert not row.estimated


def test_terms_with_nothing_entered_are_synthesized_and_marked(conn):
    """A debt account with no agreement on file still projects - and says so."""
    account = ledger.create_account(conn, "Store Card", "credit")
    ledger.add_transaction(conn, account, "2031-01-05", -50_000,
                           payee="Opening Balance")
    assert debt.get_terms(conn, account) is None

    row = debt.project_debt(conn, account, 5_000,
                            as_of="2031-03-12").outcome(account)
    assert row.estimated                         # the page must call it a guess
    assert row.minimum_cents == debt.DEFAULT_MIN_FLOOR_CENTS


def test_terms_refuse_an_account_that_is_not_a_debt(conn):
    checking = ledger.create_account(conn, "Everyday Checking", "checking")
    with pytest.raises(ValueError):
        debt.set_terms(conn, checking, apr=Decimal("19.99"))

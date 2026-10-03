"""The seam between the budget pages and the Retirement Planner (SRD 5.12i).

One figure crosses it: an annual spending LEVEL in base-year cents. These tests
follow that figure the whole way - a synthetic budget built through the domain
layer, the subtraction table the user confirms, and the spending box in the
withdrawal schedule it lands in - and they hold the four shapes the seam exists
to protect:

* the basis is the window's total less exactly the lines that stop, each with
  the evidence that picked it;
* escrow, property tax and insurance are NOT part of a mortgage exclusion, and
  work spending and health premiums are offered and never subtracted by default;
* what crosses is base-year dollars, un-inflated and un-applied - the planner's
  own Apply is still the only thing that writes a plan;
* nothing on the seam counsels, and nothing on the budget side of it brought a
  figure that moves with law or annual indexing.

The fixtures are synthetic: made-up accounts, round amounts and future months.
"""
from __future__ import annotations

import ast
import datetime as _dt
import os
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

from mammon import budgets, ledger, loans, retirement
from mammon.reports import budget as budget_report
from mammon.tests import fresh_db
from mammon.ui.models import fmt_cents, parse_amount

#: Pinned so the twelve-month window is exactly the months the fixture fills.
TODAY = _dt.date(2026, 6, 17)
AS_OF = "2026-06-30"
PERIOD = "2026-06"
BASIS_YEAR = 2026
PERIODS = ([f"2025-{m:02d}" for m in range(7, 13)]
           + [f"2026-{m:02d}" for m in range(1, 7)])

#: A month of the household's plan. No payroll tax here on purpose: the phase's
#: own test is "the total less exactly those three", and withheld tax gets its
#: own case below.
AMOUNTS = {
    "Home:Mortgage": 180000,
    "Home:Mortgage Escrow": 60000,
    "Auto:Car Loan": 52000,
    "Food:Groceries": 90000,
    "Utilities:Electric": 20000,
    "Work:Commuting": 15000,
    "Insurance:Health Insurance": 45000,
}
SAVING_CENTS = 30000
#: The three lines that stop at retirement, by category path or account.
STOPS = ("Home:Mortgage", "Auto:Car Loan")
#: What is left: escrow, groceries, electric, commuting, health premiums.
KEPT_MONTHLY = 60000 + 90000 + 20000 + 15000 + 45000
RETIREMENT_YEAR = 2030

COUNSEL_WORDS = ("should", "recommend", "advice", "advise", "suggest", "ought",
                 "you must", "better off", "we think", "best option",
                 "you need to")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def conn(tmp_path):
    c = fresh_db(tmp_path / "seam.db")
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def seeded(conn):
    """A household with a mortgage, an auto loan and a savings contribution."""
    checking = ledger.create_account(conn, "Everyday Checking", "checking")
    savings = ledger.create_account(conn, "Rainy Day Savings", "savings")
    mortgage_acct = ledger.create_account(conn, "Mortgage", "liability")
    car_acct = ledger.create_account(conn, "Car Loan", "liability")
    # Both schedules end well before RETIREMENT_YEAR, which is what makes the
    # two debt lines default to subtracted. A loan with no rate row cannot be
    # dated at all, so the rates are what give it a payoff year.
    loans.set_loan_params(conn, mortgage_acct, original_principal=20000000,
                          term_months=60, payment_amount=380000,
                          origination_date="2015-01-01",
                          rates=[loans.RateRow("2015-01-01", Decimal("5.0"))])
    loans.set_loan_params(conn, car_acct, original_principal=2400000,
                          term_months=48, payment_amount=52000,
                          origination_date="2016-03-01",
                          rates=[loans.RateRow("2016-03-01", Decimal("4.0"))])

    cats = {path: ledger.create_category(conn, path) for path in AMOUNTS}

    bid = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, bid, PERIODS[0])
    for period in PERIODS:
        for path, cents in AMOUNTS.items():
            budgets.set_line(conn, bid, cats[path], period, cents)
        budgets.set_saving_line(conn, bid, savings, period, SAVING_CENTS)

    return {"budget_id": bid, "cats": cats, "checking": checking,
            "savings": savings, "mortgage": mortgage_acct, "car": car_acct}


def _state_retirement_year(conn, year: int = RETIREMENT_YEAR) -> int:
    """Have the household state ``year`` as its retirement year, the one way
    Mammon records it: a person's planned Social Security claim age.

    :mod:`mammon.retirement` owns that figure; the budget side keeps no copy of
    it, it asks. The fixtures above deliberately state NO person, so a test that
    wants the resolved-year behavior asks for it here by name.
    """
    retirement.add_person(conn, "Planner One", "self", birth_year=year - 65,
                          birth_month=6, planned_claim_age_months=65 * 12)
    return year


def _basis(conn, seeded, **kw):
    kw.setdefault("retirement_year", RETIREMENT_YEAR)
    return budget_report.retirement_spending_basis(
        conn, seeded["budget_id"], AS_OF, **kw)


def _names(lines):
    return {line.category_name for line in lines}


def _by_name(lines):
    return {line.category_name: line for line in lines}


def _assert_no_counsel(text, where):
    low = (text or "").lower()
    for word in COUNSEL_WORDS:
        assert word not in low, f"{where} counsels ({word!r}): {text!r}"


# --------------------------------------------------------------------------
# the subtraction itself
# --------------------------------------------------------------------------
def test_basis_is_the_total_less_exactly_the_three_lines_that_stop(conn, seeded):
    basis = _basis(conn, seeded)

    assert basis.source == "budget"
    assert basis.budget_id == seeded["budget_id"]
    assert basis.months_observed == 12
    assert basis.basis_year == BASIS_YEAR
    assert basis.periods == PERIODS
    assert basis.coverage_pct == Decimal("100.0")

    monthly = sum(AMOUNTS.values()) + SAVING_CENTS
    assert basis.total_cents == monthly * 12

    assert _names(basis.excluded) == {"Home:Mortgage", "Auto:Car Loan",
                                      "Saving: Rainy Day Savings"}
    stopped = (AMOUNTS["Home:Mortgage"] + AMOUNTS["Auto:Car Loan"]
               + SAVING_CENTS) * 12
    assert sum(line.cents for line in basis.excluded) == stopped
    assert basis.included_cents == basis.total_cents - stopped
    # Twelve months observed, so the level is the window, not a projection.
    assert basis.annual_cents == basis.included_cents
    assert basis.annual_cents == KEPT_MONTHLY * 12


def test_every_subtracted_line_carries_the_evidence_that_picked_it(conn, seeded):
    basis = _basis(conn, seeded)
    lines = _by_name(basis.excluded)

    for line in basis.excluded + basis.offered:
        assert line.reason and line.reason.strip()
        assert line.reason[0].isupper() and line.reason.endswith(".")
        assert line.key.startswith(("cat:", "acct:"))
        _assert_no_counsel(line.reason, f"reason for {line.category_name}")

    # The mortgage cites the schedule it read and the year it compared against,
    # and says in so many words what the line does NOT include.
    mortgage = lines["Home:Mortgage"].reason
    assert "'Mortgage'" in mortgage
    assert str(RETIREMENT_YEAR) in mortgage
    assert "escrow" in mortgage.lower()
    assert lines["Auto:Car Loan"].reason.count("'Car Loan'") == 1
    assert "set aside" in lines["Saving: Rainy Day Savings"].reason


def test_escrow_property_tax_and_insurance_stay_in_the_basis(conn, seeded):
    basis = _basis(conn, seeded)
    escrow_key = f"cat:{seeded['cats']['Home:Mortgage Escrow']}"
    keys = {line.key for line in basis.excluded + basis.offered}

    # Not subtracted, and not even offered: the house still owes its taxes and
    # insurance after the mortgage is paid off.
    assert escrow_key not in keys
    assert basis.annual_cents >= AMOUNTS["Home:Mortgage Escrow"] * 12


def test_work_spend_and_health_premiums_are_offered_never_subtracted(conn,
                                                                     seeded):
    basis = _basis(conn, seeded)
    offered = _by_name(basis.offered)

    assert set(offered) == {"Work:Commuting", "Insurance:Health Insurance"}
    for line in basis.offered:
        assert line.default_on is False
        assert "confirm" in line.reason.lower()
    assert "medicare" in offered["Insurance:Health Insurance"].reason.lower()


def test_withheld_tax_is_subtracted_by_default(conn, seeded):
    # A second plan, identical but for a withholding line: the planner computes
    # tax on top of the spending level, so counting it here counts it twice.
    cat = ledger.create_category(conn, "Taxes:Payroll Tax Withheld")
    bid = budgets.create_budget(conn, "With Withholding")
    budgets.set_budget_period(conn, bid, PERIODS[0])
    for period in PERIODS:
        budgets.set_line(conn, bid, seeded["cats"]["Food:Groceries"], period,
                         AMOUNTS["Food:Groceries"])
        budgets.set_line(conn, bid, cat, period, 100000)

    basis = budget_report.retirement_spending_basis(
        conn, bid, AS_OF, retirement_year=RETIREMENT_YEAR)

    line = _by_name(basis.excluded)["Taxes:Payroll Tax Withheld"]
    assert line.default_on is True
    assert "twice" in line.reason
    assert basis.annual_cents == AMOUNTS["Food:Groceries"] * 12


def test_a_loan_still_running_at_retirement_is_offered_with_its_payoff_year(
        conn, seeded):
    basis = _basis(conn, seeded, retirement_year=2018)

    offered = _by_name(basis.offered)
    assert "Home:Mortgage" in offered and "Auto:Car Loan" in offered
    reason = offered["Home:Mortgage"].reason
    assert "2018" in reason and "not before retirement" in reason
    assert offered["Home:Mortgage"].default_on is False
    # Only the savings contribution is left as a default subtraction.
    assert _names(basis.excluded) == {"Saving: Rainy Day Savings"}


def test_with_no_year_stated_anywhere_no_debt_line_is_subtracted(conn, seeded):
    # No caller-supplied year AND no person in the fixture, so there is nothing
    # to compare a payoff year against: whether the loan is still being paid in
    # retirement is unknown, and unknown is never subtracted.
    basis = _basis(conn, seeded, retirement_year=None)

    assert basis.retirement_year is None
    assert _names(basis.excluded) == {"Saving: Rainy Day Savings"}
    assert "Home:Mortgage" in _names(basis.offered)


def test_the_households_own_stated_year_is_used_when_none_is_passed(conn,
                                                                   seeded):
    # Computed, not asserted: the report asks mammon.retirement for the year the
    # household says it retires, so a caller that holds no plan - the budget page
    # - still gets the debt lines dated, and is told which year did it.
    _state_retirement_year(conn)

    basis = _basis(conn, seeded, retirement_year=None)

    assert basis.retirement_year == RETIREMENT_YEAR
    assert _names(basis.excluded) == {"Home:Mortgage", "Auto:Car Loan",
                                      "Saving: Rainy Day Savings"}
    assert basis.annual_cents == KEPT_MONTHLY * 12
    assert str(RETIREMENT_YEAR) in _by_name(basis.excluded)["Home:Mortgage"].reason
    # A year the caller does pass wins: the page in front of the user is the
    # nearer evidence.
    passed = _basis(conn, seeded, retirement_year=2018)
    assert passed.retirement_year == 2018
    assert _names(passed.excluded) == {"Saving: Rainy Day Savings"}


def test_no_budget_id_reads_the_active_plan(conn, seeded):
    active = budget_report.retirement_spending_basis(
        conn, None, AS_OF, retirement_year=RETIREMENT_YEAR)

    assert active.source == "budget"
    assert active.budget_id == seeded["budget_id"]
    assert active.annual_cents == KEPT_MONTHLY * 12

    budgets.set_active(conn, seeded["budget_id"], False)
    none_active = budget_report.retirement_spending_basis(
        conn, None, AS_OF, retirement_year=RETIREMENT_YEAR)
    assert none_active.source == "trailing12"
    assert none_active.budget_id is None


def test_confirmed_keys_replace_the_defaults(conn, seeded):
    default = _basis(conn, seeded)
    health = f"cat:{seeded['cats']['Insurance:Health Insurance']}"

    none_confirmed = _basis(conn, seeded, exclude_keys=set())
    assert none_confirmed.excluded == []
    assert none_confirmed.annual_cents == none_confirmed.total_cents

    only_health = _basis(conn, seeded, exclude_keys={health})
    assert _names(only_health.excluded) == {"Insurance:Health Insurance"}
    assert only_health.annual_cents == (only_health.total_cents
                                        - AMOUNTS["Insurance:Health Insurance"]
                                        * 12)
    assert only_health.annual_cents != default.annual_cents


def test_a_partly_filled_window_is_annualized(conn, seeded):
    bid = budgets.create_budget(conn, "Half Year")
    budgets.set_budget_period(conn, bid, PERIODS[0])
    for period in PERIODS[6:]:                      # six months only
        budgets.set_line(conn, bid, seeded["cats"]["Food:Groceries"], period,
                         AMOUNTS["Food:Groceries"])

    basis = budget_report.retirement_spending_basis(
        conn, bid, AS_OF, retirement_year=RETIREMENT_YEAR)

    assert basis.months_observed == 6
    assert basis.included_cents == AMOUNTS["Food:Groceries"] * 6
    assert basis.annual_cents == AMOUNTS["Food:Groceries"] * 12
    assert "6 months" in basis.note


def test_no_plan_in_the_window_falls_back_to_measured_spending(conn, seeded):
    # Deactivated, so "no budget id" genuinely finds no plan to read.
    budgets.set_active(conn, seeded["budget_id"], False)
    groceries = seeded["cats"]["Food:Groceries"]
    for period in PERIODS:
        ledger.add_transaction(conn, seeded["checking"], f"{period}-10",
                               -20000, payee="Corner Market",
                               category_id=groceries)

    basis = budget_report.retirement_spending_basis(
        conn, None, AS_OF, retirement_year=RETIREMENT_YEAR)

    assert basis.source == "trailing12"
    assert basis.budget_id is None
    assert basis.months_observed == 12
    assert basis.total_cents == 20000 * 12
    assert basis.annual_cents == 20000 * 12
    assert basis.coverage_pct == Decimal("100.0")


def test_the_note_cites_the_window_and_says_the_dollars_are_base_year(conn,
                                                                     seeded):
    note = _basis(conn, seeded).note

    assert "'Household'" in note
    assert "12 months" in note and PERIODS[-1] in note
    assert "3 exclusions" in note
    assert str(BASIS_YEAR) in note and "inflates at its own rate" in note
    _assert_no_counsel(note, "the basis note")


# --------------------------------------------------------------------------
# the dialog: a rendering, not a second calculator
# --------------------------------------------------------------------------
def _dialog(conn, seeded, **kw):
    from mammon.ui.budget_basis import BudgetBasisDialog

    kw.setdefault("retirement_year", RETIREMENT_YEAR)
    return BudgetBasisDialog(conn, budget_id=seeded["budget_id"], as_of=AS_OF,
                             **kw)


def test_the_dialog_re_derives_the_subtraction_from_the_report(qapp, conn,
                                                              seeded):
    dialog = _dialog(conn, seeded)
    try:
        assert dialog.basis.annual_cents == KEPT_MONTHLY * 12
        # The total, one row per candidate, and the basis.
        candidates = len(dialog.basis.excluded) + len(dialog.basis.offered)
        assert dialog.table.rowCount() == candidates + 2
        last = dialog.table.item(dialog.table.rowCount() - 1, dialog.AMOUNT)
        assert last.text() == fmt_cents(dialog.basis.annual_cents)

        # Clearing every confirmation puts every line back in the basis.
        dialog.set_confirmed(set())
        assert dialog.basis.annual_cents == dialog.basis.total_cents
        assert dialog.basis.excluded == []

        # And moving the retirement year back moves the defaults with it, as
        # long as the user has not confirmed anything by hand.
        fresh = _dialog(conn, seeded)
        fresh.year.setValue(2018)
        assert "Home:Mortgage" in _names(fresh.basis.offered)
        assert fresh.basis.annual_cents > KEPT_MONTHLY * 12
        fresh.deleteLater()
    finally:
        dialog.deleteLater()


def test_the_dialog_shows_the_year_the_report_resolved(qapp, conn, seeded):
    _state_retirement_year(conn)
    dialog = _dialog(conn, seeded, retirement_year=None)
    try:
        # Opened with no year, the box ends up holding the one the arithmetic
        # used: a subtraction the user is about to accept may not rest on a
        # figure they cannot see.
        assert dialog.retirement_year() == RETIREMENT_YEAR
        assert dialog.basis.annual_cents == KEPT_MONTHLY * 12
        assert "no retirement year" not in dialog.status.text().lower()
        # And it is theirs to change: clearing it puts the loans back.
        dialog.year.setValue(2018)
        assert "Home:Mortgage" in _names(dialog.basis.offered)
    finally:
        dialog.deleteLater()


def test_the_dialog_states_facts_and_counsels_nothing(qapp, conn, seeded):
    dialog = _dialog(conn, seeded)
    try:
        _assert_no_counsel(dialog.header.text(), "the dialog header")
        _assert_no_counsel(dialog.note.text(), "the dialog note")
        _assert_no_counsel(dialog.status.text(), "the dialog status")
        for row in range(dialog.table.rowCount()):
            for col in range(len(dialog.HEADERS)):
                item = dialog.table.item(row, col)
                if item is not None:
                    _assert_no_counsel(item.text(), f"row {row} col {col}")
        basis_row = dialog.table.item(dialog.table.rowCount() - 1,
                                      dialog.REASON)
        assert str(BASIS_YEAR) in basis_row.text()
        assert "not inflated" in basis_row.text()
    finally:
        dialog.deleteLater()


# --------------------------------------------------------------------------
# the life cycle: budget page -> window -> withdrawal schedule
# --------------------------------------------------------------------------
def _accepting(captured):
    def run(dialog):
        captured["dialog"] = dialog
        captured["basis"] = dialog.basis
        captured["year"] = dialog.retirement_year()
        return True
    return run


def _open(conn, seeded=None):
    from mammon.ui.widgets import MainWindow

    win = MainWindow(conn)
    page = win.show_budget()                 # View > Budget
    page._today = TODAY
    if seeded is not None:
        budgets.set_only_active(conn, seeded["budget_id"])
    page.refresh()
    if seeded is not None and PERIOD in page._periods:
        page.month_combo.setCurrentIndex(page._periods.index(PERIOD))
    return win, page


def test_a_budget_figure_reaches_the_planner_spending_box(qapp, monkeypatch,
                                                          conn, seeded):
    _state_retirement_year(conn)
    win, page = _open(conn, seeded)
    try:
        assert page.budget_id == seeded["budget_id"]
        assert page.period == PERIOD           # the window the fixture filled

        before = retirement.get_withdrawal_plan(conn)
        captured = {}
        monkeypatch.setattr(page, "_run_dialog", _accepting(captured))
        page.retire_action.trigger()

        basis = captured["basis"]
        assert basis.annual_cents == KEPT_MONTHLY * 12
        assert basis.basis_year == BASIS_YEAR
        # The budget page passes no year - it holds no plan and must not keep a
        # retirement figure of its own - so the report resolved the household's
        # stated one and the dialog shows which year the loans were dated on.
        assert captured["year"] == RETIREMENT_YEAR
        assert basis.retirement_year == RETIREMENT_YEAR

        # The window brought the other page forward and staged the figure.
        sched = win.retirement_planner.withdrawals
        assert win.stack.currentWidget() is win.retirement_planner
        assert sched.per_year_amount.text() == fmt_cents(basis.annual_cents)
        assert parse_amount(sched.per_year_amount.text()) == basis.annual_cents

        # Base-year dollars, said out loud, with the date of the copy.
        prov = sched.basis_provenance
        assert not prov.isHidden()
        assert basis.note in prov.text()
        assert "a copy, not a link" in prov.text()
        assert str(basis.basis_year) in prov.text()

        # Nothing was written: Apply is still the only writer of a plan.
        after = retirement.get_withdrawal_plan(conn)
        assert after == before

        for where, text in (("the budget status", page.status.text()),
                            ("the planner notice", sched.last_notice),
                            ("the provenance line", prov.text())):
            _assert_no_counsel(text, where)
        assert "Apply" in sched.last_notice
    finally:
        win.close()
        qapp.processEvents()


def test_the_planners_own_from_budget_button_stages_the_same_figure(
        qapp, monkeypatch, conn, seeded):
    win, _page = _open(conn, seeded)
    try:
        sched = win.retirement_planner.withdrawals
        # The page's own today bounds the window, not the wall clock, so the
        # twelve months read are the twelve the fixture filled.
        sched._today = TODAY
        sched.per_year_start.setValue(RETIREMENT_YEAR)
        captured = {}
        monkeypatch.setattr(sched, "_run_dialog", _accepting(captured))

        sched.from_budget.click()

        # Opened from the planner, the retirement year is evidence it already
        # has, so the debt lines that clear first default to subtracted.
        assert captured["year"] == RETIREMENT_YEAR
        assert captured["basis"].annual_cents == KEPT_MONTHLY * 12
        assert sched.per_year_amount.text() == fmt_cents(KEPT_MONTHLY * 12)
        assert retirement.get_withdrawal_plan(conn).start_cents == 0
    finally:
        win.close()
        qapp.processEvents()


def test_an_empty_basis_is_refused_rather_than_staged(qapp, conn, seeded):
    win, _page = _open(conn, seeded)
    try:
        sched = win.retirement_planner.withdrawals
        before = sched.per_year_amount.text()
        assert sched.stage_spending_basis(None) is False
        assert sched.per_year_amount.text() == before
        _assert_no_counsel(sched.last_notice, "the refusal notice")
    finally:
        win.close()
        qapp.processEvents()


# --------------------------------------------------------------------------
# the one-file rule, and the direction of the dependency
# --------------------------------------------------------------------------
def _imported_modules(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.add(base)
            names.update(f"{base}.{a.name}" for a in node.names)
    return names


def test_the_budget_ui_imports_nothing_from_retirement():
    """The budget PAGE and its dialog know nothing about retirement.

    The report may ask :mod:`mammon.retirement` for the household's stated
    retirement year - asking is how there stays exactly one definition of it, and
    a year the household typed moves with nobody's legislation. What must not
    happen is a budget WIDGET holding a retirement figure: the window is what
    carries the basis across, so neither of these two files may reach for one.
    """
    import mammon.ui.budget_basis as _bb
    import mammon.ui.budget_page as _page

    for module in (_bb, _page):
        names = _imported_modules(module.__file__)
        offenders = {n for n in names if n.split(".")[-1] == "retirement"
                     or n.startswith("mammon.retirement")}
        assert not offenders, (
            f"{module.__name__} imports {offenders}: the budget side must hold "
            f"no figure that moves with law or annual indexing, so the window "
            f"is what carries the basis across")


def test_the_report_asks_for_the_year_and_derives_nothing_indexed():
    """The one place the budget side touches retirement, held to asking."""
    import inspect

    from mammon.reports import budget as _rb

    source = inspect.getsource(_rb)
    # Exactly one import of the module, and it is the lazy one in the helper
    # that asks for the household's year.
    assert source.count("from mammon import retirement") == 1
    asked = inspect.getsource(_rb._household_retirement_year)
    assert "retirement.retirement_year(" in asked
    assert "list_people(" in asked
    # Nothing is recomputed from a birth year or a claim age here.
    for derived in ("birth_year", "planned_claim_age_months", "62", "70"):
        assert derived not in asked


def test_the_seam_brought_no_indexed_figure_with_it():
    # The phase's own acceptance check, run from here so the seam's tests fail
    # if a rate, a threshold or a limit ever lands in the budget code.
    from mammon.tests.test_retirement import (
        test_no_indexed_figure_lives_outside_this_file as guard)

    guard()


def test_payroll_deductions_measured_from_paychecks_are_added_to_a_budget_basis(
        conn, seeded):
    """The budget is take-home (SRD 5.12), so withholding and premiums are not
    lines in it; the seam adds them back from the ledger's paychecks, classified
    as a category of the same name would be: tax subtracted by default, a
    health premium offered and kept in."""
    plain = _basis(conn, seeded)                 # before any paycheck posts
    tax = ledger.resolve_category(conn, "Taxes:Income Tax Withheld")
    health = ledger.resolve_category(conn, "Insurance:Health Insurance")
    salary = ledger.resolve_category(conn, "Salary")
    for period in PERIODS:
        tid = ledger.add_transaction(conn, seeded["checking"], f"{period}-15",
                                     3_000_00, payee="Employer")
        ledger.set_splits(conn, tid, [(salary, 3_700_00, ""), (tax, -500_00, ""),
                                      (health, -200_00, "")])
    basis = _basis(conn, seeded)
    n = len(PERIODS)
    assert basis.deductions_cents == n * 700_00
    assert basis.total_cents == plain.total_cents + n * 700_00
    excluded = _by_name(basis.excluded)
    offered = _by_name(basis.offered)
    before = _by_name(plain.offered)
    assert excluded["Taxes:Income Tax Withheld"].cents == n * 500_00
    assert (offered["Insurance:Health Insurance"].cents
            == before["Insurance:Health Insurance"].cents + n * 200_00)
    # Subtracted tax leaves the premiums IN the level, measured from paychecks.
    assert basis.included_cents == plain.included_cents + n * 200_00
    assert "payroll deductions measured from paychecks" in basis.note

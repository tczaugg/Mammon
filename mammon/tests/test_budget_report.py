"""Multi-period budget-vs-actual roll-ups (parity roadmap item 6).

The report layer only stacks and sums :func:`mammon.budgets.budget_vs_actual`
across a span, so these tests pin: the per-period exclusions it inherits
(income not netted, transfers dropped, out-of-range spend ignored) survive the
roll-up; categories that appear in only some months still sum correctly; an
unbudgeted category surfaces (budgeted 0) only when asked for; and the grand
totals equal the sum of the per-period totals -- the invariant that would break
if a category were double-counted or a month dropped.
"""
from __future__ import annotations

import pytest

from mammon import budgets, db, ledger
from mammon.reports import budget as budget_report


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "budget_report.db")
    yield c
    c.close()


@pytest.fixture
def cats(conn):
    return {
        "fuel": ledger.resolve_category(conn, "Fuel"),
        "groceries": ledger.resolve_category(conn, "Groceries"),
        "dining": ledger.resolve_category(conn, "Dining"),
        "shopping": ledger.resolve_category(conn, "Shopping"),
        "salary": ledger.resolve_category(conn, "Salary"),
    }


@pytest.fixture
def seeded(conn, cats):
    """Jan+Feb spending on one account, with income, a transfer, and spends
    outside 2026 that must all stay out of a 2026 roll-up. A budget lines up
    Fuel/Groceries in both months and Dining in January only; Shopping is spent
    in February with no line, to exercise the unbudgeted-surfacing path."""
    a = ledger.create_account(conn, "Checking", "checking")
    b = ledger.create_account(conn, "Savings", "savings")

    # January spending (money out -> negative)
    ledger.add_transaction(conn, a, "2026-01-05", -100_00, category_id=cats["fuel"])
    ledger.add_transaction(conn, a, "2026-01-12", -40_00, category_id=cats["fuel"])
    ledger.add_transaction(conn, a, "2026-01-15", -250_00, category_id=cats["groceries"])
    ledger.add_transaction(conn, a, "2026-01-20", -30_00, category_id=cats["dining"])
    # February spending
    ledger.add_transaction(conn, a, "2026-02-08", -60_00, category_id=cats["fuel"])
    ledger.add_transaction(conn, a, "2026-02-14", -200_00, category_id=cats["groceries"])
    ledger.add_transaction(conn, a, "2026-02-22", -80_00, category_id=cats["shopping"])
    # income must never net against or appear as spending
    ledger.add_transaction(conn, a, "2026-01-28", 3000_00, category_id=cats["salary"])
    ledger.add_transaction(conn, a, "2026-02-28", 3000_00, category_id=cats["salary"])
    # a transfer leg with a category must be excluded
    ledger.create_transfer(conn, a, b, "2026-01-25", 500_00)
    # spends outside 2026 must never reach a 2026 roll-up
    ledger.add_transaction(conn, a, "2025-12-31", -999_00, category_id=cats["fuel"])
    ledger.add_transaction(conn, a, "2027-01-02", -999_00, category_id=cats["fuel"])

    bud = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, bud, cats["fuel"], "2026-01", 120_00)
    budgets.set_line(conn, bud, cats["fuel"], "2026-02", 120_00)
    budgets.set_line(conn, bud, cats["groceries"], "2026-01", 300_00)
    budgets.set_line(conn, bud, cats["groceries"], "2026-02", 300_00)
    budgets.set_line(conn, bud, cats["dining"], "2026-01", 50_00)
    return bud


def test_range_sums_categories_and_periods(conn, cats, seeded):
    rep = budget_report.budget_vs_actual_range(conn, seeded, "2026-01", "2026-02")

    assert rep.budget_name == "Household"
    assert rep.periods == ["2026-01", "2026-02"]

    by_cat = {r.category_id: r for r in rep.category_totals}

    # Fuel: budgeted 120+120=240, actual 100+40+60=200 -> remaining 40
    fuel = by_cat[cats["fuel"]]
    assert (fuel.budgeted_cents, fuel.actual_cents, fuel.remaining_cents) == (240_00, 200_00, 40_00)
    # Groceries: budgeted 600, actual 250+200=450 -> remaining 150
    groc = by_cat[cats["groceries"]]
    assert (groc.budgeted_cents, groc.actual_cents, groc.remaining_cents) == (600_00, 450_00, 150_00)
    # Dining: line + spend in January only
    dining = by_cat[cats["dining"]]
    assert (dining.budgeted_cents, dining.actual_cents, dining.remaining_cents) == (50_00, 30_00, 20_00)
    # Shopping: spent in February with no line -> budgeted 0, overspent
    shopping = by_cat[cats["shopping"]]
    assert (shopping.budgeted_cents, shopping.actual_cents, shopping.remaining_cents) == (0, 80_00, -80_00)
    # income and transfers never appear
    assert cats["salary"] not in by_cat

    # category totals ordered by name, case-insensitive
    assert [r.category_name for r in rep.category_totals] == ["Dining", "Fuel", "Groceries", "Shopping"]

    # per-period totals
    per = {t.period: t for t in rep.period_totals}
    jan, feb = per["2026-01"], per["2026-02"]
    assert (jan.budgeted_cents, jan.actual_cents, jan.remaining_cents) == (470_00, 420_00, 50_00)
    assert (feb.budgeted_cents, feb.actual_cents, feb.remaining_cents) == (420_00, 340_00, 80_00)

    # grand totals equal the sum of the per-period totals (no double counting)
    assert rep.total_budgeted_cents == 890_00
    assert rep.total_actual_cents == 760_00
    assert rep.total_remaining_cents == 130_00
    assert rep.total_budgeted_cents == sum(t.budgeted_cents for t in rep.period_totals)
    assert rep.total_actual_cents == sum(t.actual_cents for t in rep.period_totals)
    # and equal the sum of the per-category totals
    assert rep.total_actual_cents == sum(r.actual_cents for r in rep.category_totals)
    # remaining is always budgeted - actual
    for r in rep.category_totals:
        assert r.remaining_cents == r.budgeted_cents - r.actual_cents


def test_single_month_range(conn, cats, seeded):
    rep = budget_report.budget_vs_actual_range(conn, seeded, "2026-01", "2026-01")
    assert rep.periods == ["2026-01"]
    # single month equals that month's own budget_vs_actual, summed trivially
    by_cat = {r.category_id: r for r in rep.category_totals}
    assert by_cat[cats["fuel"]].actual_cents == 140_00
    assert cats["shopping"] not in by_cat        # not spent in January


def test_exclude_unbudgeted(conn, cats, seeded):
    rep = budget_report.budget_vs_actual_range(
        conn, seeded, "2026-01", "2026-02", include_unbudgeted=False)
    by_cat = {r.category_id: r for r in rep.category_totals}
    # Shopping has no line anywhere in the range -> dropped
    assert cats["shopping"] not in by_cat
    assert set(by_cat) == {cats["fuel"], cats["groceries"], cats["dining"]}
    assert rep.total_actual_cents == 680_00        # 760 - 80 shopping


def test_ytd_through_month_matches_range(conn, cats, seeded):
    ytd = budget_report.budget_vs_actual_ytd(conn, seeded, 2026, through_month=2)
    rng = budget_report.budget_vs_actual_range(conn, seeded, "2026-01", "2026-02")
    assert ytd.periods == rng.periods == ["2026-01", "2026-02"]
    assert ytd.total_budgeted_cents == rng.total_budgeted_cents
    assert ytd.total_actual_cents == rng.total_actual_cents
    assert {r.category_id: r.actual_cents for r in ytd.category_totals} == \
           {r.category_id: r.actual_cents for r in rng.category_totals}


def test_ytd_full_year_excludes_other_years(conn, cats, seeded):
    rep = budget_report.budget_vs_actual_ytd(conn, seeded, 2026)   # through December
    assert rep.periods == [f"2026-{m:02d}" for m in range(1, 13)]
    # Dec-2025 and Jan-2027 fuel spends must not leak into 2026's actuals
    fuel = next(r for r in rep.category_totals if r.category_id == cats["fuel"])
    assert fuel.actual_cents == 200_00
    # empty later months contribute nothing, so grand totals match the Jan-Feb span
    assert rep.total_actual_cents == 760_00


def test_bad_arguments_raise(conn, seeded):
    with pytest.raises(ValueError):                       # end precedes start
        budget_report.budget_vs_actual_range(conn, seeded, "2026-03", "2026-01")
    with pytest.raises(ValueError):                       # malformed period
        budget_report.budget_vs_actual_range(conn, seeded, "2026-13", "2026-02")
    with pytest.raises(ValueError):                       # no such budget
        budget_report.budget_vs_actual_range(conn, 9999, "2026-01", "2026-02")
    with pytest.raises(ValueError):                       # through_month out of range
        budget_report.budget_vs_actual_ytd(conn, seeded, 2026, through_month=13)

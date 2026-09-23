"""Tests for mammon.budgets: the UI-free budgets domain layer (roadmap item 6).

Covers budget CRUD, the (budget, category, period) upsert (no duplicate on
re-set), line retrieval/filtering, and budget_vs_actual against known
transactions -- checking that it reuses reports.spending semantics (transfers
excluded, gross outflow as a positive magnitude, income not netted) and reports
budgeted, actual, and remaining per category.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from mammon import budgets, db, ledger
from mammon.tests import fresh_db


def test_budgets_is_importable_first_no_circular_import():
    """`import mammon.budgets` in a fresh interpreter must not blow up on a
    circular import. budgets -> reports.spending pulls in reports' package
    __init__, which imports reports.budget, which imports BudgetActualRow back
    from budgets; if reports.spending is imported at budgets' module top instead
    of lazily, that cycle raises ImportError whenever budgets (or the Budgets UI)
    is the first thing a process imports. Guards the lazy import in
    budget_vs_actual that breaks it."""
    for first in ("import mammon.budgets",
                  "from mammon.ui import budget_widget"):
        # timeout= is mandatory: a child interpreter that wedges on import would
        # otherwise block the whole suite forever with no output to say why.
        r = subprocess.run([sys.executable, "-c", first + "; print('ok')"],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{first!r} failed:\n{r.stderr}"
        assert "ok" in r.stdout


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "budgets.db")
    yield c
    c.close()


@pytest.fixture
def cats(conn):
    return {
        "fuel": ledger.resolve_category(conn, "Auto & Transport:Fuel"),
        "groceries": ledger.resolve_category(conn, "Groceries"),
        "dining": ledger.resolve_category(conn, "Dining"),
        "salary": ledger.resolve_category(conn, "Salary"),
    }


# ---- schema / migration -----------------------------------------------------
def test_schema_version_tracks_migrations():
    # Budgets landed at schema 39; later slices (e.g. rule conditions, _V40)
    # keep appending, so pin the length relationship rather than a frozen int.
    assert db.SCHEMA_VERSION == len(db.MIGRATIONS)
    assert db.SCHEMA_VERSION >= 39


def test_init_db_idempotent_fresh_and_existing(tmp_path):
    path = tmp_path / "idem.db"
    c1 = fresh_db(path)
    assert c1.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    # budget tables exist on a fresh DB
    names = {r["name"] for r in c1.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"budgets", "budget_lines"} <= names
    c1.close()

    # Re-opening an existing, already-migrated DB is a no-op that still works.
    c2 = fresh_db(path)
    assert c2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    names2 = {r["name"] for r in c2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"budgets", "budget_lines"} <= names2
    c2.close()


# ---- budget CRUD ------------------------------------------------------------
def test_create_and_list_budgets(conn):
    a = budgets.create_budget(conn, "Household")
    b = budgets.create_budget(conn, "Vacation", active=False)
    assert a != b

    all_b = budgets.list_budgets(conn)
    assert [x.name for x in all_b] == ["Household", "Vacation"]  # name-ordered
    assert {x.id for x in all_b} == {a, b}
    by_id = {x.id: x for x in all_b}
    assert by_id[a].active is True
    assert by_id[b].active is False

    active_only = budgets.list_budgets(conn, include_inactive=False)
    assert [x.id for x in active_only] == [a]


def test_get_budget(conn):
    a = budgets.create_budget(conn, "Household")
    got = budgets.get_budget(conn, a)
    assert got is not None and got.name == "Household" and got.active is True
    assert budgets.get_budget(conn, 9999) is None


def test_rename_and_set_active(conn):
    a = budgets.create_budget(conn, "Household")
    budgets.rename_budget(conn, a, "Home")
    budgets.set_active(conn, a, False)
    got = budgets.get_budget(conn, a)
    assert got.name == "Home"
    assert got.active is False


def test_delete_budget_removes_lines(conn, cats):
    a = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, a, cats["fuel"], "2026-01", 100_00)
    budgets.set_line(conn, a, cats["groceries"], "2026-01", 400_00)
    assert len(budgets.get_lines(conn, a)) == 2

    budgets.delete_budget(conn, a)
    assert budgets.get_budget(conn, a) is None
    # lines are gone, not orphaned
    left = conn.execute(
        "SELECT COUNT(*) AS n FROM budget_lines WHERE budget_id = ?", (a,)
    ).fetchone()["n"]
    assert left == 0


# ---- budget lines / upsert --------------------------------------------------
def test_set_line_upsert_dedup(conn, cats):
    a = budgets.create_budget(conn, "Household")
    id1 = budgets.set_line(conn, a, cats["fuel"], "2026-01", 100_00)
    # re-setting the SAME (budget, category, period) overwrites, not duplicates
    id2 = budgets.set_line(conn, a, cats["fuel"], "2026-01", 150_00, rollover=True)
    assert id1 == id2

    lines = budgets.get_lines(conn, a, period="2026-01")
    assert len(lines) == 1
    assert lines[0].amount_cents == 150_00
    assert lines[0].rollover is True


def test_set_line_distinct_periods_coexist(conn, cats):
    a = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, a, cats["fuel"], "2026-01", 100_00)
    budgets.set_line(conn, a, cats["fuel"], "2026-02", 120_00)
    assert len(budgets.get_lines(conn, a)) == 2
    jan = budgets.get_lines(conn, a, period="2026-01")
    assert len(jan) == 1 and jan[0].amount_cents == 100_00


def test_set_line_rejects_bad_period(conn, cats):
    a = budgets.create_budget(conn, "Household")
    for bad in ("2026", "2026-13", "26-01", "2026-01-05", "nope"):
        with pytest.raises(ValueError):
            budgets.set_line(conn, a, cats["fuel"], bad, 100_00)


def test_delete_line(conn, cats):
    a = budgets.create_budget(conn, "Household")
    lid = budgets.set_line(conn, a, cats["fuel"], "2026-01", 100_00)
    budgets.delete_line(conn, lid)
    assert budgets.get_lines(conn, a) == []


# ---- budget vs actual -------------------------------------------------------
@pytest.fixture
def seeded(conn, cats):
    """One account with January spending, plus an income row, a transfer, and an
    out-of-month spend that must all stay OUT of the January actuals."""
    a = ledger.create_account(conn, "Checking", "checking")
    b = ledger.create_account(conn, "Savings", "savings")

    # January spending (money out -> negative)
    ledger.add_transaction(conn, a, "2026-01-05", -100_00, category_id=cats["fuel"])
    ledger.add_transaction(conn, a, "2026-01-12", -40_00, category_id=cats["fuel"])
    ledger.add_transaction(conn, a, "2026-01-15", -250_00, category_id=cats["groceries"])
    ledger.add_transaction(conn, a, "2026-01-20", -30_00, category_id=cats["dining"])
    # income (positive) must not net against, or appear as, spending
    ledger.add_transaction(conn, a, "2026-01-28", 3000_00, category_id=cats["salary"])
    # out-of-month spend must not count in January
    ledger.add_transaction(conn, a, "2026-02-03", -75_00, category_id=cats["fuel"])
    # a transfer leg tagged to a category must be excluded by spending_by_category
    ledger.create_transfer(conn, a, b, "2026-01-25", 500_00)
    return a, b


def test_budget_vs_actual_known_transactions(conn, cats, seeded):
    a = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, a, cats["fuel"], "2026-01", 120_00)       # under? over?
    budgets.set_line(conn, a, cats["groceries"], "2026-01", 300_00)  # under budget
    budgets.set_line(conn, a, cats["dining"], "2026-01", 50_00)      # under budget

    rows = budgets.budget_vs_actual(conn, a, "2026-01")
    by_cat = {r.category_id: r for r in rows}

    # Fuel: two Jan buys (100 + 40 = 140), budget 120 -> overspent by 20
    fuel = by_cat[cats["fuel"]]
    assert fuel.budgeted_cents == 120_00
    assert fuel.actual_cents == 140_00
    assert fuel.remaining_cents == -20_00

    # Groceries: 250 spent against 300 -> 50 remaining
    groc = by_cat[cats["groceries"]]
    assert groc.actual_cents == 250_00
    assert groc.remaining_cents == 50_00

    # Dining: 30 spent against 50 -> 20 remaining
    dining = by_cat[cats["dining"]]
    assert dining.actual_cents == 30_00
    assert dining.remaining_cents == 20_00

    # Income category was never spent and never budgeted -> absent.
    assert cats["salary"] not in by_cat
    # The transfer leg must not surface as spending in any row.
    assert all(r.actual_cents >= 0 for r in rows)
    assert sum(r.actual_cents for r in rows) == 140_00 + 250_00 + 30_00


def test_budget_vs_actual_includes_unbudgeted_spend(conn, cats, seeded):
    a = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, a, cats["fuel"], "2026-01", 120_00)

    rows = budgets.budget_vs_actual(conn, a, "2026-01")
    by_cat = {r.category_id: r for r in rows}
    # groceries + dining were spent but not budgeted -> present with budget 0
    assert by_cat[cats["groceries"]].budgeted_cents == 0
    assert by_cat[cats["groceries"]].actual_cents == 250_00
    assert by_cat[cats["dining"]].budgeted_cents == 0

    # ...but excluded when the caller asks for budgeted categories only
    rows2 = budgets.budget_vs_actual(conn, a, "2026-01", include_unbudgeted=False)
    assert {r.category_id for r in rows2} == {cats["fuel"]}


def test_budget_vs_actual_zero_spend_budget_line_present(conn, cats, seeded):
    """A budgeted category with no spending this period still appears (actual 0)."""
    a = budgets.create_budget(conn, "Household")
    # nothing was spent on Salary; budget it anyway
    budgets.set_line(conn, a, cats["salary"], "2026-01", 10_00)
    rows = budgets.budget_vs_actual(conn, a, "2026-01")
    by_cat = {r.category_id: r for r in rows}
    assert by_cat[cats["salary"]].budgeted_cents == 10_00
    assert by_cat[cats["salary"]].actual_cents == 0
    assert by_cat[cats["salary"]].remaining_cents == 10_00


def test_budget_vs_actual_period_isolation(conn, cats, seeded):
    a = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, a, cats["fuel"], "2026-02", 200_00)
    rows = budgets.budget_vs_actual(conn, a, "2026-02")
    by_cat = {r.category_id: r for r in rows}
    # only the Feb 3 fuel buy (75) counts in February
    assert by_cat[cats["fuel"]].actual_cents == 75_00
    assert by_cat[cats["fuel"]].budgeted_cents == 200_00

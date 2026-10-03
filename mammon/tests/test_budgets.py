"""Tests for mammon.budgets: the UI-free budgets domain layer (roadmap item 6).

Covers budget CRUD, the (budget, category, period) upsert (no duplicate on
re-set), line retrieval/filtering, and budget_vs_actual against known
transactions -- checking that it reuses reports.spending semantics (transfers
excluded, gross outflow as a positive magnitude, income not netted) and reports
budgeted, actual, and remaining per category.
"""
from __future__ import annotations

import datetime as _dt
import subprocess
import sys
from decimal import Decimal

import pytest

from mammon import budgets, db, ledger
from mammon.tests import fresh_db


def test_budgets_is_importable_first_no_circular_import():
    """`import mammon.budgets` in a fresh interpreter must not blow up on a
    circular import. budgets -> reports.spending pulls in reports' package
    __init__, which imports reports.budget, which imports BudgetActualRow back
    from budgets; if reports.spending is imported at budgets' module top instead
    of lazily, that cycle raises ImportError whenever budgets (or anything that
    reaches it) is the first thing a process imports. Guards the lazy import in
    budget_vs_actual that breaks it. Entered from BOTH ends of the cycle, since
    which module a process happens to touch first is not under our control."""
    for first in ("import mammon.budgets",
                  "from mammon.reports import budget"):
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


# ---- phase 2: buckets, spreading, rollover modes ----------------------------
#: The clock every phase-2 test pins, so "the last twelve COMPLETE months" is a
#: fixed window (2025-06 .. 2026-05) and every expected figure can be computed by
#: hand rather than relative to whenever the suite happens to run.
TODAY = _dt.date(2026, 6, 15)
WINDOW_END = "2026-05"


@pytest.fixture
def history(conn, cats):
    """Twelve complete months of synthetic spending, shaped so each seed bucket
    has exactly one obvious answer:

    * groceries: 300.00 in every one of the 12 months -> flex, mean 300.00
    * dining: 60.00 in the 6 most recent months only  -> flex (seen in half the
      window), mean 30.00
    * fuel: 400.00 once and 600.00 once               -> non-monthly, 1,000.00 a
      year, whose twelfth is 83.34 / 83.33
    * salary: income, which is never a spending target and must not be proposed
    """
    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    for i, (period, first, _last) in enumerate(
            budgets.trailing_months(TODAY, 12)):
        ledger.add_transaction(conn, acct, f"{period}-10", -300_00,
                               category_id=cats["groceries"])
        ledger.add_transaction(conn, acct, f"{period}-11", 3_000_00,
                               category_id=cats["salary"])
        if i >= 6:                                   # the 6 most recent months
            ledger.add_transaction(conn, acct, f"{period}-12", -60_00,
                                   category_id=cats["dining"])
        assert first.startswith(period)
    # Two lumpy fuel months, a year apart in feel: rare enough to be non-monthly.
    ledger.add_transaction(conn, acct, "2025-08-04", -400_00,
                           category_id=cats["fuel"])
    ledger.add_transaction(conn, acct, "2026-02-04", -600_00,
                           category_id=cats["fuel"])
    # A partial current month, which must stay OUT of every average: including it
    # is what drags a target low on the first of the month.
    ledger.add_transaction(conn, acct, "2026-06-02", -9_999_00,
                           category_id=cats["groceries"])
    return acct


def test_seed_from_history_matches_hand_computed_averages(conn, cats, history):
    """The end-to-end seed of section 8: proposals from synthetic history, each
    asserted against a trailing average worked out by hand, INCLUDING a
    non-monthly category spread to its monthly twelfth."""
    proposals = budgets.seed_from_history(conn, months=12, today=TODAY)
    by_cat = {p.category_id: p for p in proposals}

    # Groceries: 300.00 x 12 = 3,600.00 over 12 months -> 300.00 a month, flex.
    groc = by_cat[cats["groceries"]]
    assert groc.bucket == "flex"
    assert groc.basis == "mean"
    assert groc.months_seen == 12
    assert groc.amount_cents == 300_00 == budgets.mean_cents(3_600_00, 12)
    assert sum(groc.sample_cents) == 3_600_00      # the partial month is excluded

    # Dining: 60.00 in 6 of 12 months = 360.00 / 12 -> 30.00 a month, still flex
    # (half the window is not "rare"), and volatile: the spread (60.00) exceeds
    # its own mean (30.00).
    dining = by_cat[cats["dining"]]
    assert dining.bucket == "flex"
    assert dining.months_seen == 6
    assert dining.amount_cents == 30_00 == budgets.mean_cents(360_00, 12)
    assert dining.spread_cents == 60_00
    assert dining.volatile is True

    # Fuel: seen in 2 of 12 months -> non-monthly. 1,000.00 over the window is
    # 1,000.00 a year, and its twelfth is 83.34 (four months carry the extra
    # cent, eight carry 83.33, and the twelve sum to the year EXACTLY).
    fuel = by_cat[cats["fuel"]]
    assert fuel.bucket == "nonmonthly"
    assert fuel.months_seen == 2
    assert fuel.annual_cents == 100_000
    assert fuel.amount_cents == 8334
    spread = budgets.spread_annual(fuel.annual_cents, 12)
    assert spread[0] == fuel.amount_cents
    assert sorted(set(spread)) == [8333, 8334]
    assert sum(spread) == 100_000

    # Income is not a spending target.
    assert cats["salary"] not in by_cat


def test_apply_proposals_writes_the_twelfths_and_the_buckets(conn, cats,
                                                             history):
    proposals = budgets.seed_from_history(conn, months=12, today=TODAY)
    b = budgets.create_budget(conn, "Household")
    written = budgets.apply_proposals(conn, b, proposals,
                                     start_period="2026-06", months=12)
    assert written == 12 * len(proposals)

    lines = budgets.get_lines(conn, b)
    fuel = [ln for ln in lines if ln.category_id == cats["fuel"]]
    assert len(fuel) == 12
    # Stored as twelve ordinary monthly lines that sum to the year exactly --
    # nothing is divided again at display time.
    assert sum(ln.amount_cents for ln in fuel) == 100_000
    assert sorted({ln.amount_cents for ln in fuel}) == [8333, 8334]
    groc = [ln for ln in lines if ln.category_id == cats["groceries"]]
    assert {ln.amount_cents for ln in groc} == {300_00}

    settings = budgets.get_settings(conn, b)
    assert settings[cats["fuel"]].bucket == "nonmonthly"
    assert settings[cats["fuel"]].annual_cents == 100_000
    assert settings[cats["fuel"]].rollover_mode == "both"   # an envelope carries
    assert settings[cats["groceries"]].bucket == "flex"
    assert settings[cats["groceries"]].rollover_mode == "none"
    # The legacy per-line flag stays in step with the stored mode.
    assert all(ln.rollover is True for ln in fuel)
    assert all(ln.rollover is False for ln in groc)


def test_round_to_dollar_applies_per_line(conn, cats, history):
    """Rounding is per line with NO remainder redistribution: the point is
    round numbers, and re-adding pennies to make the year come out would undo
    exactly that."""
    proposals = [p for p in budgets.seed_from_history(conn, months=12,
                                                      today=TODAY)
                 if p.category_id == cats["fuel"]]
    b = budgets.create_budget(conn, "Household")
    budgets.apply_proposals(conn, b, proposals, start_period="2026-06",
                            months=12, round_to_dollar=True)
    amounts = {ln.amount_cents for ln in budgets.get_lines(conn, b)}
    assert amounts == {83_00}                     # 83.34 and 83.33 both -> 83
    assert sum(ln.amount_cents for ln in budgets.get_lines(conn, b)) == 996_00


def test_spread_annual_is_exact():
    assert budgets.spread_annual(100_000, 12) == [8334] * 4 + [8333] * 8
    for total in (1, 99, 100_000, 123_457, -100_000):
        spread = budgets.spread_annual(total, 12)
        assert len(spread) == 12
        assert sum(spread) == total


def test_settings_default_without_a_row(conn, cats):
    """An absent settings row is a real answer -- flex, no rollover -- not None:
    every category in a budget has a bucket whether anyone has chosen one yet."""
    b = budgets.create_budget(conn, "Household")
    st = budgets.get_setting(conn, b, cats["fuel"])
    assert st.bucket == "flex"
    assert st.rollover_mode == "none"
    assert st.rollover is False
    assert st.annual_cents == 0
    assert budgets.get_settings(conn, b) == {}


def test_set_settings_rejects_nonsense(conn, cats):
    b = budgets.create_budget(conn, "Household")
    with pytest.raises(ValueError):
        budgets.set_settings(conn, b, cats["fuel"], bucket="whenever")
    with pytest.raises(ValueError):
        budgets.set_settings(conn, b, cats["fuel"], rollover_mode="sometimes")


def test_carry_override_round_trip(conn, cats):
    b = budgets.create_budget(conn, "Household")
    budgets.set_carry_override(conn, b, cats["fuel"], "2026-06", 25_00,
                              note="opening envelope")
    got = budgets.get_carry_overrides(conn, b)
    assert len(got) == 1
    assert got[0].amount_cents == 25_00
    assert got[0].note == "opening envelope"
    assert got[0].set_at                          # an audit trail, not a blank
    # Re-setting the same (budget, category, period) overwrites, not duplicates.
    budgets.set_carry_override(conn, b, cats["fuel"], "2026-06", -5_00)
    got = budgets.get_carry_overrides(conn, b, period="2026-06")
    assert [o.amount_cents for o in got] == [-5_00]
    budgets.clear_carry_override(conn, b, cats["fuel"], "2026-06")
    assert budgets.get_carry_overrides(conn, b) == []


def test_copy_budget_shifts_every_period(conn, cats):
    src = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, src, "2026-01", "2026-12")
    budgets.set_line(conn, src, cats["fuel"], "2026-01", 100_00)
    budgets.set_line(conn, src, cats["fuel"], "2026-02", 120_00)
    budgets.set_settings(conn, src, cats["fuel"], bucket="nonmonthly",
                         rollover_mode="both", annual_cents=100_000)
    budgets.set_carry_override(conn, src, cats["fuel"], "2026-01", 25_00)

    copy = budgets.copy_budget(conn, src, "Next year", shift_months=12)
    assert copy != src
    # A scenario never takes over by itself.
    assert budgets.get_budget(conn, copy).active is False
    assert budgets.get_budget(conn, copy).start_period == "2027-01"
    assert sorted((ln.period, ln.amount_cents)
                  for ln in budgets.get_lines(conn, copy)) == [
        ("2027-01", 100_00), ("2027-02", 120_00)]
    assert budgets.get_settings(conn, copy)[cats["fuel"]].annual_cents == 100_000
    assert [(o.period, o.amount_cents)
            for o in budgets.get_carry_overrides(conn, copy)] == [
        ("2027-01", 25_00)]
    # The source is untouched: a copy is not a move.
    assert len(budgets.get_lines(conn, src)) == 2


# ---- category operations must not orphan the new tables ---------------------
def _orphans(conn, category_id):
    """Rows in EITHER phase-2 table still naming ``category_id``."""
    return {
        "settings": conn.execute(
            "SELECT COUNT(*) AS n FROM budget_category_settings "
            "WHERE category_id = ? OR group_id = ?",
            (category_id, category_id)).fetchone()["n"],
        "overrides": conn.execute(
            "SELECT COUNT(*) AS n FROM budget_carry_overrides "
            "WHERE category_id = ?", (category_id,)).fetchone()["n"],
    }


def test_merge_category_leaves_no_orphan_budget_state(conn, cats):
    """The other half of section 8: merge two categories through mammon.ledger
    and no row in either new table may survive naming the loser. A merge that
    left one behind would be invisible until a REUSED category id resurfaced it
    as someone else's budget."""
    b = budgets.create_budget(conn, "Household")
    loser, winner = cats["dining"], cats["groceries"]
    budgets.set_line(conn, b, loser, "2026-06", 40_00)
    budgets.set_line(conn, b, winner, "2026-06", 300_00)
    budgets.set_settings(conn, b, loser, bucket="nonmonthly",
                        rollover_mode="both", annual_cents=60_000)
    budgets.set_settings(conn, b, winner, bucket="flex",
                        rollover_mode="positive", annual_cents=12_000)
    budgets.set_carry_override(conn, b, loser, "2026-06", 10_00)
    budgets.set_carry_override(conn, b, winner, "2026-06", 5_00)

    ledger.merge_category(conn, loser, winner)

    assert _orphans(conn, loser) == {"settings": 0, "overrides": 0}
    # Nothing was dropped either: the survivor carries the merged state.
    st = budgets.get_setting(conn, b, winner)
    assert st.bucket == "flex"                 # the target's choice wins
    assert st.rollover_mode == "positive"
    assert st.annual_cents == 60_000 + 12_000  # annual totals ADD
    assert [(o.category_id, o.amount_cents)
            for o in budgets.get_carry_overrides(conn, b)] == [
        (winner, 15_00)]
    # budget_lines is repointed and summed by ledger's own merge, as before.
    lines = budgets.get_lines(conn, b, period="2026-06")
    assert [(ln.category_id, ln.amount_cents) for ln in lines] == [
        (winner, 340_00)]


def test_delete_category_leaves_no_orphan_budget_state(conn, cats):
    b = budgets.create_budget(conn, "Household")
    doomed = cats["dining"]
    budgets.set_settings(conn, b, doomed, bucket="nonmonthly",
                        annual_cents=60_000)
    food = budgets.create_group(conn, b, "Food")
    budgets.set_member_group(conn, b, doomed, food)
    budgets.set_member_group(conn, b, cats["groceries"], food)
    budgets.set_carry_override(conn, b, doomed, "2026-06", 10_00)

    ledger.delete_category(conn, doomed)

    assert _orphans(conn, doomed) == {"settings": 0, "overrides": 0}
    # The group survives with its remaining member; the doomed one is simply
    # no longer in it.
    assert budgets.group_members(conn, b) == {food: [cats["groceries"]]}
    assert budgets.get_setting(conn, b, cats["groceries"]).group_id == food


def test_rename_category_keeps_budget_state(conn, cats):
    """A rename keeps the id, so nothing to repoint -- pinned so a future rename
    that recreated the row instead would fail loudly here."""
    b = budgets.create_budget(conn, "Household")
    budgets.set_settings(conn, b, cats["dining"], bucket="fixed",
                        rollover_mode="positive")
    ledger.rename_category(conn, cats["dining"], "Restaurants")
    st = budgets.get_setting(conn, b, cats["dining"])
    assert st.bucket == "fixed"
    assert st.rollover_mode == "positive"


# ---- committed spend and the carry chain (phase 3, SRD 5.12c) ---------------
def test_entering_a_scheduled_bill_moves_cents_but_not_the_sum(conn, cats):
    """The 6.2 invariant: actual + committed does not move when a reminder is paid.

    A scheduled bill due later in the month is COMMITTED, not actual. Entering it
    through :mod:`mammon.ledger` must turn exactly those cents into actual and
    drop them from committed -- their sum is what the month is on the hook for,
    and it cannot change just because a row appeared. Quicken double counts here.
    """
    from mammon import scheduled

    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    util = ledger.resolve_category(conn, "Bills:Utilities")
    b = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, b, util, "2026-06", 150_00)
    # Spending already in the register, so the row is not a pure-commitment row.
    ledger.add_transaction(conn, acct, "2026-06-03", -20_00, category_id=util)
    scheduled.add_scheduled(conn, acct, payee="City Utilities", amount=-120_00,
                            frequency="monthly", next_date="2026-06-25",
                            category_id=util)

    before = {r.category_id: r for r in budgets.budget_vs_actual(conn, b, "2026-06")}[util]
    assert before.actual_cents == 20_00
    assert before.committed_cents == 120_00
    assert before.remaining_cents == 150_00 - 20_00        # untouched by commitments
    assert before.uncommitted_cents == 150_00 - 20_00 - 120_00
    on_the_hook = before.actual_cents + before.committed_cents

    ledger.add_transaction(conn, acct, "2026-06-25", -120_00, category_id=util)

    after = {r.category_id: r for r in budgets.budget_vs_actual(conn, b, "2026-06")}[util]
    assert after.actual_cents == 20_00 + 120_00
    assert after.committed_cents == 0
    assert before.committed_cents - after.committed_cents == 120_00
    assert after.actual_cents + after.committed_cents == on_the_hook
    # Free money is unchanged too: paying a bill you had promised frees nothing.
    assert after.uncommitted_cents == before.uncommitted_cents


def test_three_month_carry_chain_matches_hand_computation(conn, cats):
    """A surplus and then a deficit chained across three months in ``both`` mode.

    Hand-computed, 300.00 budgeted every month::

        Jan  spend 250.00  carry_in    0.00  -> Feb carry_in  +50.00
        Feb  spend 400.00  carry_in  +50.00  -> Mar carry_in  -50.00
        Mar  spend 200.00  carry_in  -50.00  -> remaining     +50.00
    """
    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    groc = cats["groceries"]
    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-01", "2026-12")
    budgets.set_settings(conn, b, groc, rollover_mode="both")
    for period in ("2026-01", "2026-02", "2026-03"):
        budgets.set_line(conn, b, groc, period, 300_00)
    ledger.add_transaction(conn, acct, "2026-01-10", -250_00, category_id=groc)
    ledger.add_transaction(conn, acct, "2026-02-10", -400_00, category_id=groc)
    ledger.add_transaction(conn, acct, "2026-03-10", -200_00, category_id=groc)

    def row(period):
        return {r.category_id: r
                for r in budgets.budget_vs_actual(conn, b, period)}[groc]

    jan, feb, mar = row("2026-01"), row("2026-02"), row("2026-03")
    assert jan.carried_in_cents == 0                       # the budget's floor
    assert jan.remaining_cents == 50_00
    assert feb.carried_in_cents == 50_00
    assert feb.remaining_cents == 50_00 + 300_00 - 400_00  # -50.00
    assert mar.carried_in_cents == -50_00
    assert mar.remaining_cents == 50_00


def test_positive_mode_carries_a_surplus_and_not_a_deficit(conn, cats):
    """``positive`` clamps the carry at zero, so a bad month starts over.

    Same three months as the ``both`` chain, overspent first: February must open
    at 0.00 rather than -100.00, and February's own surplus must still reach
    March -- clamping a deficit is not the same as switching rollover off.
    """
    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    groc = cats["groceries"]
    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-01", "2026-12")
    budgets.set_settings(conn, b, groc, rollover_mode="positive")
    for period in ("2026-01", "2026-02", "2026-03"):
        budgets.set_line(conn, b, groc, period, 300_00)
    ledger.add_transaction(conn, acct, "2026-01-10", -400_00, category_id=groc)
    ledger.add_transaction(conn, acct, "2026-02-10", -250_00, category_id=groc)

    def row(period):
        return {r.category_id: r
                for r in budgets.budget_vs_actual(conn, b, period)}[groc]

    feb = row("2026-02")
    assert feb.carried_in_cents == 0                       # -100.00 was clamped
    assert feb.rollover_mode == "positive"
    mar = row("2026-03")
    assert mar.carried_in_cents == 50_00                   # the surplus carries
    assert mar.remaining_cents == 350_00                   # nothing spent yet

    # In ``both`` mode the same history carries the deficit instead.
    budgets.set_settings(conn, b, groc, rollover_mode="both")
    assert row("2026-02").carried_in_cents == -100_00
    assert row("2026-03").carried_in_cents == -50_00


def test_delete_budget_removes_settings_and_overrides(conn, cats):
    """Deleting a budget clears the new tables EXPLICITLY, rather than trusting
    ON DELETE CASCADE: a connection opened without PRAGMA foreign_keys = ON
    would leave rows nobody can see and nobody deletes."""
    b = budgets.create_budget(conn, "Household")
    budgets.set_settings(conn, b, cats["fuel"], bucket="nonmonthly",
                        annual_cents=100_000)
    budgets.set_carry_override(conn, b, cats["fuel"], "2026-06", 25_00)
    budgets.delete_budget(conn, b)
    for table in ("budget_category_settings", "budget_carry_overrides"):
        left = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE budget_id = ?",  # noqa: S608
            (b,)).fetchone()["n"]
        assert left == 0, table


# ---- saving and debt pay-down targets (SRD 5.12d) --------------------------
@pytest.fixture
def saver(conn):
    """A checking account and a 401(k), with a monthly paycheck whose split
    carries a 450.00 deferral leg into the 401(k) for January to March 2026."""
    chk = ledger.create_account(conn, "Everyday Checking", "checking")
    k401 = ledger.create_account(conn, "Employer 401K", "investment")
    salary = ledger.resolve_category(conn, "Salary")
    for month in ("01", "02", "03"):
        tid = ledger.add_transaction(conn, chk, f"2026-{month}-15", 3_550_00,
                                     payee="Employer Inc")
        ledger.set_splits(conn, tid, [
            {"category_id": salary, "amount": 4_000_00},
            {"transfer_account_id": k401, "amount": -450_00},
        ])
    return chk, k401


def test_saving_line_upserts_per_account_and_month(conn, saver):
    _chk, k401 = saver
    b = budgets.create_budget(conn, "Household")
    budgets.set_saving_line(conn, b, k401, "2026-03", 400_00)
    budgets.set_saving_line(conn, b, k401, "2026-03", 500_00)    # re-set: no dup
    budgets.set_saving_line(conn, b, k401, "2026-04", 500_00)
    assert [(s.period, s.amount_cents) for s in budgets.get_saving_lines(conn, b)] \
        == [("2026-03", 500_00), ("2026-04", 500_00)]
    budgets.delete_saving_line(conn, b, k401, "2026-04")
    assert [s.period for s in budgets.get_saving_lines(conn, b)] == ["2026-03"]
    with pytest.raises(ValueError):
        budgets.set_saving_line(conn, b, k401, "March", 1)


def test_saving_vs_actual_reads_ahead_and_to_go(conn, saver):
    """Target 500.00, saved 450.00: 50.00 still to go. The row is not in
    budget_vs_actual at all -- saving is never mixed into spending."""
    _chk, k401 = saver
    b = budgets.create_budget(conn, "Household")
    budgets.set_saving_line(conn, b, k401, "2026-03", 500_00)
    [row] = budgets.saving_vs_actual(conn, b, "2026-03")
    assert (row.account_id, row.account_name, row.budgeted_cents,
            row.actual_cents, row.remaining_cents) == (
        k401, "Employer 401K", 500_00, 450_00, 50_00)
    assert budgets.budget_vs_actual(conn, b, "2026-03") == []
    # Unbudgeted saving still shows, with a zero target.
    [feb] = budgets.saving_vs_actual(conn, b, "2026-02")
    assert (feb.budgeted_cents, feb.actual_cents, feb.remaining_cents) == (
        0, 450_00, -450_00)
    assert budgets.saving_vs_actual(conn, b, "2026-02",
                                    include_unbudgeted=False) == []


def test_a_scheduled_paycheck_commits_its_deferral_then_moves_it_to_actual(
        conn, saver):
    """A paycheck schedule's split template carries the 401(k) leg: until the
    April paycheck is entered its deferral is COMMITTED saving; entering it
    moves the cents to actual and leaves actual + committed unchanged."""
    from mammon import scheduled

    chk, k401 = saver
    salary = ledger.resolve_category(conn, "Salary")
    scheduled.add_scheduled(
        conn, chk, payee="Employer Inc", amount=3_550_00, frequency="monthly",
        next_date="2026-04-15", auto_enter=False,
        splits=[{"category_id": salary, "transfer_account_id": None,
                 "amount": 4_000_00, "memo": ""},
                {"category_id": None, "transfer_account_id": k401,
                 "amount": -450_00, "memo": ""}])
    b = budgets.create_budget(conn, "Household")
    budgets.set_saving_line(conn, b, k401, "2026-04", 450_00)

    [before] = budgets.saving_vs_actual(conn, b, "2026-04")
    assert (before.actual_cents, before.committed_cents,
            before.uncommitted_cents) == (0, 450_00, 0)

    tid = ledger.add_transaction(conn, chk, "2026-04-15", 3_550_00,
                                 payee="Employer Inc")
    ledger.set_splits(conn, tid, [
        {"category_id": salary, "amount": 4_000_00},
        {"transfer_account_id": k401, "amount": -450_00},
    ])
    [after] = budgets.saving_vs_actual(conn, b, "2026-04")
    assert (after.actual_cents, after.committed_cents) == (450_00, 0)
    assert (after.actual_cents + after.committed_cents
            == before.actual_cents + before.committed_cents)


def test_seed_saving_proposes_positive_open_accounts_only(conn, saver):
    """The 401(k) gets 450.00 a month (three months of 450.00 over a three-month
    window). A brokerage that was net withdrawn from and a loan that is now
    closed are not offered."""
    chk, k401 = saver
    brokerage = ledger.create_account(conn, "Brokerage", "investment")
    ledger.create_transfer(conn, brokerage, chk, "2026-02-10", 1_000_00)
    loan = ledger.create_account(conn, "Paid Off Loan", "liability")
    ledger.create_transfer(conn, chk, loan, "2026-01-05", 2_000_00)
    conn.execute("UPDATE accounts SET closed_flag = 1 WHERE id = ?", (loan,))
    conn.commit()

    props = budgets.seed_saving_from_history(conn, months=3,
                                             end_period="2026-03")
    assert [(p.account_id, p.amount_cents, p.months_seen) for p in props] == [
        (k401, 450_00, 3)]

    b = budgets.create_budget(conn, "Household")
    written = budgets.apply_saving_proposals(conn, b, props,
                                             start_period="2026-04", months=12)
    assert written == 12
    lines = budgets.get_saving_lines(conn, b)
    assert {s.amount_cents for s in lines} == {450_00}
    assert (lines[0].period, lines[-1].period) == ("2026-04", "2027-03")


def test_copy_and_delete_budget_carry_saving_lines(conn, saver):
    _chk, k401 = saver
    b = budgets.create_budget(conn, "Household")
    budgets.set_saving_line(conn, b, k401, "2026-03", 450_00)
    c = budgets.copy_budget(conn, b, "Next year", shift_months=12)
    assert [(s.account_id, s.period, s.amount_cents)
            for s in budgets.get_saving_lines(conn, c)] == [
        (k401, "2027-03", 450_00)]
    budgets.delete_budget(conn, b)
    assert conn.execute("SELECT COUNT(*) AS n FROM budget_saving_lines "
                        "WHERE budget_id = ?", (b,)).fetchone()["n"] == 0
    assert len(budgets.get_saving_lines(conn, c)) == 1


# ---- the twelve-month plan (SRD 5.12) ---------------------------------------
def test_frequency_periods_step_from_the_first_month_and_never_wrap():
    fp = budgets.frequency_periods
    assert fp("2026-09", "2026-09", "monthly") == budgets.plan_periods("2026-09")
    assert fp("2026-09", "2026-10", "quarterly") == [
        "2026-10", "2027-01", "2027-04", "2027-07"]
    assert fp("2026-09", "2026-09", "bi-monthly") == [
        "2026-09", "2026-11", "2027-01", "2027-03", "2027-05", "2027-07"]
    assert fp("2026-09", "2027-08", "semi-annually") == ["2027-08"]
    assert fp("2026-09", "2027-02", "once") == ["2027-02"]
    with pytest.raises(ValueError):
        fp("2026-09", "2027-09", "monthly")          # outside the plan
    with pytest.raises(ValueError):
        fp("2026-09", "2026-09", "weekly")


def test_default_budget_name_is_unique(conn):
    assert budgets.default_budget_name(conn, "2026-09") == "new-budget-9-26"
    budgets.create_budget(conn, "new-budget-9-26")
    assert budgets.default_budget_name(conn, "2026-09") == "new-budget-9-26-2"


def test_new_budget_is_empty_unless_copying(conn, cats):
    first = budgets.new_budget(conn, "2026-09")
    b = budgets.get_budget(conn, first)
    assert (b.name, b.start_period, b.end_period, b.active) == (
        "new-budget-9-26", "2026-09", "2027-08", True)
    budgets.set_line(conn, first, cats["fuel"], "2026-10", 40_00)
    empty = budgets.new_budget(conn, "2026-09")
    assert budgets.get_lines(conn, empty) == []
    assert budgets.get_budget(conn, empty).active is False
    copy = budgets.new_budget(conn, "2027-01", copy_from=first)
    assert [(ln.period, ln.amount_cents) for ln in budgets.get_lines(conn, copy)] \
        == [("2027-10", 40_00)]


def test_move_budget_start_wraps_every_kind_of_row(conn, cats):
    acct = ledger.create_account(conn, "Employer 401K", "investment")
    b = budgets.new_budget(conn, "2026-09")
    budgets.set_line(conn, b, cats["fuel"], "2026-11", 10_00)
    budgets.set_line(conn, b, cats["fuel"], "2027-03", 30_00)
    budgets.set_saving_line(conn, b, acct, "2026-09", 450_00)
    budgets.set_carry_override(conn, b, cats["fuel"], "2026-12", 5_00, note="n")
    budgets.move_budget_start(conn, b, "2027-01")
    assert budgets.get_budget(conn, b).end_period == "2027-12"
    assert {ln.period: ln.amount_cents for ln in budgets.get_lines(conn, b)} == {
        "2027-11": 10_00, "2027-03": 30_00}
    assert [s.period for s in budgets.get_saving_lines(conn, b)] == ["2027-09"]
    [ov] = budgets.get_carry_overrides(conn, b)
    assert (ov.period, ov.amount_cents, ov.note) == ("2027-12", 5_00, "n")


def test_move_budget_start_keeps_the_visible_line_on_a_collision(conn, cats):
    """A legacy budget with lines outside its twelve months: when two land on
    one calendar month, the one the user could see (inside the old window)
    wins."""
    b = budgets.new_budget(conn, "2026-09")
    budgets.set_line(conn, b, cats["fuel"], "2026-10", 11_00)     # visible
    budgets.set_line(conn, b, cats["fuel"], "2025-10", 99_00)     # stray
    budgets.move_budget_start(conn, b, "2027-09")
    assert [(ln.period, ln.amount_cents) for ln in budgets.get_lines(conn, b)] \
        == [("2027-10", 11_00)]


def test_fill_line_and_remove_rows(conn, cats):
    acct = ledger.create_account(conn, "Home Loan", "liability")
    b = budgets.new_budget(conn, "2026-09")
    assert budgets.fill_line(conn, b, 60_00, first_period="2026-09",
                             frequency="quarterly", category_id=cats["fuel"]) \
        == ["2026-09", "2026-12", "2027-03", "2027-06"]
    budgets.fill_line(conn, b, 500_00, first_period="2027-01", frequency="once",
                      account_id=acct)
    assert [s.period for s in budgets.get_saving_lines(conn, b)] == ["2027-01"]
    with pytest.raises(ValueError):
        budgets.fill_line(conn, b, 1, first_period="2026-09", frequency="once")
    budgets.clear_line(conn, b, cats["fuel"], "2026-12")
    assert len(budgets.get_lines(conn, b)) == 3
    budgets.set_settings(conn, b, cats["fuel"], bucket="fixed")
    budgets.remove_category(conn, b, cats["fuel"])
    assert budgets.get_lines(conn, b) == [] and budgets.get_settings(conn, b) == {}
    budgets.remove_saving_account(conn, b, acct)
    assert budgets.get_saving_lines(conn, b) == []


# ---- burn-down: the calendar's budget mode (SRD 5.10e, 5.12e) ---------------
@pytest.fixture
def burn(conn, cats):
    """One checking account and a 150 Groceries envelope for March."""
    acct = ledger.create_account(conn, "Everyday", "checking")
    b = budgets.create_budget(conn, "Plan")
    budgets.set_line(conn, b, cats["groceries"], "2026-03", 150_00)
    return acct, b


def test_burn_down_marks_every_expense_past_the_limit(conn, cats, burn):
    """Three 90 buys against a 150 envelope: the second CROSSES the line and the
    third is deeper past it, and BOTH carry the mark. Marking only the crossing
    expense would let the rest of a blown month look fine, which is the whole
    thing the mode exists to show."""
    acct, b = burn
    for day in ("2026-03-02", "2026-03-09", "2026-03-16"):
        ledger.add_transaction(conn, acct, day, -90_00, payee="Market",
                               category_id=cats["groceries"])

    bd = budgets.burn_down(conn, b, "2026-03", include_predictions=False,
                           today="2026-03-31")
    assert bd.allowance_cents == 150_00
    marks = [(e.date, e.over_cents, e.category_name)
             for d in bd.days for e in d.expenses]
    assert marks == [("2026-03-02", 0, "Groceries"),
                     ("2026-03-09", 30_00, "Groceries"),
                     ("2026-03-16", 120_00, "Groceries")]

    by_date = {d.date: d for d in bd.days}
    assert by_date["2026-03-02"].over_categories == ()
    assert by_date["2026-03-09"].over_categories == ("Groceries",)
    assert by_date["2026-03-16"].over_categories == ("Groceries",)
    # The running remainder goes negative and stays there: that tail is the view.
    assert by_date["2026-03-02"].remaining_cents == 60_00
    assert by_date["2026-03-09"].remaining_cents == -30_00
    assert by_date["2026-03-31"].remaining_cents == -120_00
    assert bd.remaining_cents == -120_00
    assert bd.spent_cents == 270_00


def test_burn_down_crossing_expense_carries_only_its_own_overrun(conn, cats, burn):
    """The same three 90 buys against a 200 envelope. Nothing is marked until
    the third, which lands 70 past the limit -- the mark follows the CUMULATIVE
    total, not the size of any one expense."""
    acct, b = burn
    budgets.set_line(conn, b, cats["groceries"], "2026-03", 200_00)
    for day in ("2026-03-02", "2026-03-09", "2026-03-16"):
        ledger.add_transaction(conn, acct, day, -90_00, payee="Market",
                               category_id=cats["groceries"])

    bd = budgets.burn_down(conn, b, "2026-03", include_predictions=False,
                           today="2026-03-31")
    assert [e.over_cents for d in bd.days for e in d.expenses] == [0, 0, 70_00]
    assert [e.over for d in bd.days for e in d.expenses] == [False, False, True]


def test_burn_down_ignores_income_and_excludes_transfers(conn, cats, burn):
    """Income never refills an envelope (the mode answers 'is the plan holding',
    not 'will the account clear'), and money moved to savings is not spending."""
    acct, b = burn
    savings = ledger.create_account(conn, "Rainy Day", "savings")
    ledger.add_transaction(conn, acct, "2026-03-04", -120_00, payee="Market",
                           category_id=cats["groceries"])
    ledger.add_transaction(conn, acct, "2026-03-05", 4000_00, payee="Employer",
                           category_id=cats["salary"])
    ledger.create_transfer(conn, acct, savings, "2026-03-06", 500_00)

    bd = budgets.burn_down(conn, b, "2026-03", include_predictions=False,
                           today="2026-03-31")
    assert bd.spent_cents == 120_00
    assert bd.remaining_cents == 30_00
    assert [e.payee for d in bd.days for e in d.expenses] == ["Market"]


def test_burn_down_burns_the_interest_leg_and_not_the_principal(conn, cats, burn):
    """A debt payment split into principal (a transfer leg) and interest: only
    the interest reaches an envelope. Principal is not spending, here as
    everywhere in the budget."""
    acct, b = burn
    loan = ledger.create_account(conn, "Home Loan", "liability")
    interest = ledger.resolve_category(conn, "Home:Mortgage Interest")
    budgets.set_line(conn, b, interest, "2026-03", 400_00)
    txn = ledger.add_transaction(conn, acct, "2026-03-01", -1000_00, payee="Lender")
    ledger.set_splits(conn, txn, [
        {"amount": -700_00, "transfer_account_id": loan},
        {"amount": -300_00, "category_id": interest},
    ])

    bd = budgets.burn_down(conn, b, "2026-03", include_predictions=False,
                           today="2026-03-31")
    lines = [(e.category_id, e.amount_cents) for d in bd.days for e in d.expenses]
    assert lines == [(interest, 300_00)]
    assert bd.remaining_cents == 150_00 + 400_00 - 300_00


def test_burn_down_lines_sum_to_the_month_actuals(conn, cats, seeded):
    """The per-line extractor must agree category-for-category with the
    aggregate the Track tab reads. If these two ever drift, the calendar and the
    budget report show different spending for the same month. Zero entries are
    dropped from the comparison: the aggregate carries a row for a category that
    only saw income, and a line extractor has nothing to emit for one."""
    per_cat: dict = {}
    for ln in budgets._expense_lines(conn, "2026-01-01", "2026-01-31"):
        per_cat[ln["category_id"]] = per_cat.get(ln["category_id"], 0) + ln["cents"]
    actuals = {c: v for c, v in budgets._month_actuals(conn, "2026-01").items() if v}
    assert per_cat == actuals and per_cat


def test_committed_events_match_month_committed(conn, cats):
    """The day-resolved committed events must aggregate to exactly what
    month_committed reports, so the burn-down's scheduled tail and the Track
    tab's committed column can never tell different stories."""
    from mammon import scheduled as _scheduled

    acct = ledger.create_account(conn, "Everyday", "checking")
    _scheduled.add_scheduled(conn, account_id=acct, payee="Utility",
                             amount=-80_00, frequency="monthly",
                             next_date="2026-03-10", category_id=cats["fuel"])
    _scheduled.add_scheduled(conn, account_id=acct, payee="Gym",
                             amount=-25_00, frequency="monthly",
                             next_date="2026-03-20", category_id=cats["dining"])
    # One of them has already been paid: the real row claims the occurrence.
    ledger.add_transaction(conn, acct, "2026-03-20", -25_00, payee="Gym",
                           category_id=cats["dining"])

    per_cat: dict = {}
    for ev in budgets._committed_events(conn, "2026-03-01", "2026-03-31"):
        for cid, cents in ev["legs"].items():
            per_cat[cid] = per_cat.get(cid, 0) + cents
    entered = budgets._month_actuals(conn, "2026-03")
    assert per_cat == budgets.month_committed(conn, "2026-03", entered=entered)
    assert per_cat == {cats["fuel"]: 80_00}


def test_burn_down_counts_a_scheduled_bill_once(conn, cats, burn):
    """A scheduled grocery bill the register does not hold yet burns the
    envelope as committed; once it is entered the same money must not be
    counted twice."""
    from mammon import scheduled as _scheduled

    acct, b = burn
    _scheduled.add_scheduled(conn, account_id=acct, payee="Box Scheme",
                             amount=-60_00, frequency="monthly",
                             next_date="2026-03-12",
                             category_id=cats["groceries"])

    bd = budgets.burn_down(conn, b, "2026-03", include_predictions=False,
                           today="2026-03-01")
    assert (bd.spent_cents, bd.committed_cents) == (0, 60_00)
    assert bd.remaining_cents == 90_00

    ledger.add_transaction(conn, acct, "2026-03-12", -60_00, payee="Box Scheme",
                           category_id=cats["groceries"])
    bd = budgets.burn_down(conn, b, "2026-03", include_predictions=False,
                           today="2026-03-31")
    assert (bd.spent_cents, bd.committed_cents) == (60_00, 0)
    assert bd.remaining_cents == 90_00


def test_burn_down_carry_in_raises_the_allowance(conn, cats, burn):
    """Rollover headroom is part of the allowance, taken from the same carry
    recursion the Track tab uses -- the two cannot disagree."""
    acct, b = burn
    budgets.set_line(conn, b, cats["groceries"], "2026-02", 150_00, rollover=True)
    budgets.set_line(conn, b, cats["groceries"], "2026-03", 150_00, rollover=True)
    ledger.add_transaction(conn, acct, "2026-02-10", -100_00, payee="Market",
                           category_id=cats["groceries"])

    bd = budgets.burn_down(conn, b, "2026-03", include_predictions=False,
                           today="2026-03-31")
    assert bd.allowance_cents == 150_00 + 50_00


def test_month_category_status_reports_every_envelope_in_cents(conn, cats, burn):
    """The per-item reading the calendar's bars are drawn from: one row per
    budgeted category with its allowance, what it has spent, and what is LEFT --
    negative once the envelope is blown. In the fixed by-path order, so a
    category keeps its place whatever the month's spending did."""
    acct, b = burn                       # Groceries 150 for March
    budgets.set_line(conn, b, cats["fuel"], "2026-03", 80_00)
    budgets.set_line(conn, b, cats["dining"], "2026-03", 60_00)
    ledger.add_transaction(conn, acct, "2026-03-05", -40_00, payee="Market",
                           category_id=cats["groceries"])
    ledger.add_transaction(conn, acct, "2026-03-06", -90_00, payee="Cafe",
                           category_id=cats["dining"])
    # Fuel is deliberately untouched: a full envelope must still show up.

    rows = budgets.month_category_status(conn, b, "2026-03",
                                        include_predictions=False,
                                        today="2026-03-31")
    by_name = {r.category_name: r for r in rows}
    assert set(by_name) == {"Groceries", "Fuel", "Dining"}

    assert (by_name["Fuel"].allowance_cents, by_name["Fuel"].spent_cents,
            by_name["Fuel"].remaining_cents) == (80_00, 0, 80_00)
    assert (by_name["Groceries"].spent_cents,
            by_name["Groceries"].remaining_cents) == (40_00, 110_00)
    over = by_name["Dining"]
    assert (over.spent_cents, over.remaining_cents) == (90_00, -30_00)
    assert over.over and over.over_cents == 30_00
    assert not by_name["Fuel"].over and by_name["Fuel"].over_cents == 0

    # Fixed by display path, NOT by what is left: "Auto & Transport:Fuel",
    # then "Dining", then "Groceries" -- the blown envelope does not jump.
    assert [r.category_name for r in rows] == ["Fuel", "Dining", "Groceries"]

    # One arithmetic, three views: the same numbers the day cells total up.
    bd = budgets.burn_down(conn, b, "2026-03", include_predictions=False,
                           today="2026-03-31")
    assert bd.per_category == rows
    assert sum(r.spent_cents for r in rows) == bd.spent_cents
    assert sum(r.allowance_cents for r in rows) == bd.allowance_cents


def test_month_category_status_narrows_spending_but_not_the_allowance(
        conn, cats, burn):
    """``account_ids`` is a SPEND-side filter: an envelope asked about through
    one account keeps the household's whole allowance and only loses the other
    account's charges. Narrowing the allowance too would invent headroom."""
    acct, b = burn                       # Groceries 150 for March
    other = ledger.create_account(conn, "Card", "credit")
    ledger.add_transaction(conn, acct, "2026-03-05", -40_00, payee="Market",
                           category_id=cats["groceries"])
    ledger.add_transaction(conn, other, "2026-03-06", -25_00, payee="Market",
                           category_id=cats["groceries"])

    whole = budgets.month_category_status(conn, b, "2026-03",
                                         include_predictions=False,
                                         today="2026-03-31")[0]
    assert (whole.allowance_cents, whole.spent_cents,
            whole.remaining_cents) == (150_00, 65_00, 85_00)

    narrowed = budgets.month_category_status(conn, b, "2026-03",
                                             include_predictions=False,
                                             account_ids=[acct],
                                             today="2026-03-31")[0]
    assert narrowed.allowance_cents == 150_00       # NOT narrowed
    assert (narrowed.spent_cents, narrowed.remaining_cents) == (40_00, 110_00)


def test_month_category_status_counts_scheduled_charges_as_committed(
        conn, cats, burn):
    """A bill still to come is charged against the envelope but is not SPENT:
    the bar has to show the room it has already lost without claiming the money
    left the account."""
    from mammon import scheduled as _scheduled

    acct, b = burn
    _scheduled.add_scheduled(conn, account_id=acct, payee="Box Scheme",
                             amount=-50_00, frequency="monthly",
                             next_date="2026-03-20",
                             category_id=cats["groceries"])

    row = budgets.month_category_status(conn, b, "2026-03",
                                       include_predictions=False,
                                       today="2026-03-01")[0]
    assert row.spent_cents == 0
    assert row.committed_cents == 50_00
    assert row.charged_cents == 50_00
    assert row.remaining_cents == 100_00


def test_budget_coverage_is_the_share_of_recent_spending_in_budgeted_cats(
        conn, cats):
    """A deliberately sparse budget: only Groceries has a line, so a window
    holding 240 of groceries and 360 of everything else covers 40 percent --
    below the floor, and the flag says so."""
    acct = ledger.create_account(conn, "Everyday", "checking")
    ledger.add_transaction(conn, acct, "2026-03-02", -240_00, payee="Market",
                           category_id=cats["groceries"])
    ledger.add_transaction(conn, acct, "2026-03-03", -300_00, payee="Station",
                           category_id=cats["fuel"])
    ledger.add_transaction(conn, acct, "2026-03-04", -60_00, payee="Cafe",
                           category_id=cats["dining"])
    b = budgets.create_budget(conn, "Sparse")
    budgets.set_line(conn, b, cats["groceries"], "2026-03", 150_00)

    pct = budgets.budget_coverage(conn, b, as_of="2026-03-31")
    assert pct == Decimal("40.0")
    assert pct < budgets.COVERAGE_FLOOR
    bd = budgets.burn_down(conn, b, "2026-03", include_predictions=False,
                           today="2026-03-31")
    assert bd.low_coverage is True and bd.coverage_pct == Decimal("40.0")
    # The gaps name what to budget next, largest first.
    gaps = [name for name, _cents in budgets.coverage_gaps(conn, b,
                                                           as_of="2026-03-31")]
    assert gaps == ["Auto & Transport:Fuel", "Dining"]


def test_budget_coverage_window_and_empty_window(conn, cats):
    """Spending older than the window does not count, and a window with no
    spending at all is vacuously fully covered rather than zero: nothing escaped
    the plan, which reads better than a zero suggesting the plan is broken."""
    acct = ledger.create_account(conn, "Everyday", "checking")
    ledger.add_transaction(conn, acct, "2025-12-01", -500_00, payee="Station",
                           category_id=cats["fuel"])
    b = budgets.create_budget(conn, "Plan")
    budgets.set_line(conn, b, cats["groceries"], "2026-06", 150_00)
    assert budgets.budget_coverage(conn, b, as_of="2026-06-30") == Decimal("100.0")


# ---- the cash floor: balances on paper, breaks on the calendar (SRD 5.12f) ---
@pytest.fixture
def floor_month(conn, cats):
    """June 2026: rent clears the day BEFORE the paycheck lands.

    Deliberately a month that is right on paper. 2,000 of income against a
    1,500 plan, every cent of the plan a real expense, remaining exactly zero --
    and 300 in the account when the 1,500 rent clears on the 14th, a day ahead
    of the deposit on the 15th. Nothing about the amounts is wrong; only the
    order is. Returns ``(account_id, budget_id, rent_category_id)``."""
    rent = ledger.resolve_category(conn, "Home:Rent")
    acct = ledger.create_account(conn, "Everyday", "checking")
    ledger.add_transaction(conn, acct, "2026-05-31", 300_00, payee="Opening")
    ledger.add_transaction(conn, acct, "2026-06-14", -1500_00, payee="Landlord",
                           category_id=rent)
    ledger.add_transaction(conn, acct, "2026-06-15", 2000_00, payee="Employer",
                           category_id=cats["salary"])
    b = budgets.create_budget(conn, "Plan")
    budgets.set_line(conn, b, rent, "2026-06", 1500_00)
    return acct, b, rent


def test_month_that_balances_still_breaks_the_floor_the_day_before_payday(
        conn, floor_month):
    """The whole reason (D) exists: a month can balance and still overdraw.

    The budget is satisfied -- income covers the plan and the rent envelope has
    exactly nothing left over -- and the projected balance is 1,200 in the hole
    on the 14th, because the rent cleared before the money that was meant to pay
    it arrived. The burn-down mode cannot see this: it ignores income by
    construction, so 1,500 spent out of a 1,500 envelope reads as a perfect
    month. The flagged date is the day BEFORE the deposit."""
    _acct, b, rent = floor_month

    # On paper: the plan balances, category by category.
    rows = {r.category_id: r for r in budgets.budget_vs_actual(conn, b, "2026-06")}
    assert rows[rent].budgeted_cents == 1500_00
    assert rows[rent].actual_cents == 1500_00
    assert rows[rent].remaining_cents == 0        # spent to the cent, not over

    floor = budgets.month_floor(conn, b, "2026-06", include_predictions=False,
                                today="2026-06-30")
    assert floor.planned_cents == 1500_00
    assert floor.income_cents == 2000_00
    assert floor.balances is True                 # 500 to the good for the month
    assert floor.surplus_cents == 500_00

    # Day by day: it breaks, and the date is the one a person can act on.
    assert floor.breaks is True
    assert floor.flagged is True
    assert floor.low_cents == -1200_00
    assert floor.low_date == "2026-06-14"         # the day before the deposit
    assert floor.cushion_cents == 0               # the default: zero
    assert floor.shortfall_cents == 1200_00

    # And the burn-down, looking at the same month, sees nothing wrong.
    bd = budgets.burn_down(conn, b, "2026-06", include_predictions=False,
                           today="2026-06-30")
    assert bd.allowance_cents == 1500_00
    assert bd.remaining_cents == 0
    assert all(not d.over_categories for d in bd.days)


def test_floor_low_date_stays_inside_the_month_asked_about(conn, floor_month):
    """A month whose every day sits above its opening reports a date INSIDE it.

    ``projection.project`` seeds its low with the opening balance dated the day
    BEFORE the span, so surfacing its ``low_date`` unfiltered would flag July
    with a date in June. July here holds nothing at all, so its lowest day is
    its first."""
    _acct, b, _rent = floor_month
    floors = budgets.month_floors(conn, b, ["2026-06", "2026-07"],
                                  include_predictions=False, today="2026-06-30")
    assert set(floors) == {"2026-06", "2026-07"}
    july = floors["2026-07"]
    assert july.low_date.startswith("2026-07")
    assert july.low_cents == 800_00               # what June closed with
    assert july.breaks is False and july.flagged is False


def test_floor_cushion_is_the_line_and_a_dip_to_exactly_it_is_not_a_break(
        conn, cats):
    """The cushion is a floor to stay AT or above, so equality is not a break.

    Raising it turns a month nothing overdrew into a flagged one, which is the
    point of having the preference at all: the warning should arrive before the
    account is empty, not as it goes negative."""
    acct = ledger.create_account(conn, "Everyday", "checking")
    ledger.add_transaction(conn, acct, "2026-05-31", 500_00, payee="Opening")
    ledger.add_transaction(conn, acct, "2026-06-10", -300_00, payee="Landlord",
                           category_id=cats["groceries"])
    ledger.add_transaction(conn, acct, "2026-06-20", 400_00, payee="Employer",
                           category_id=cats["salary"])
    b = budgets.create_budget(conn, "Plan")
    budgets.set_line(conn, b, cats["groceries"], "2026-06", 300_00)

    kw = dict(include_predictions=False, today="2026-06-30")
    plain = budgets.month_floor(conn, b, "2026-06", **kw)
    assert plain.low_cents == 200_00 and plain.breaks is False

    exact = budgets.month_floor(conn, b, "2026-06", cushion_cents=200_00, **kw)
    assert exact.breaks is False and exact.shortfall_cents == 0

    raised = budgets.month_floor(conn, b, "2026-06", cushion_cents=500_00, **kw)
    assert raised.breaks is True and raised.flagged is True
    assert raised.low_date == "2026-06-10" and raised.shortfall_cents == 300_00


def test_check_floor_tests_a_proposed_payment_before_anything_is_written(
        conn, floor_month):
    """The reusable seam: ``extra`` is money not in the ledger yet.

    A savings contribution or an extra principal payment can be calendar-tested
    against the floor before it is committed, and a proposal that breaks the
    floor is known by the DATE it breaks on. Each entry moves every day from its
    own date onward and nothing is stored."""
    _acct, b, _rent = floor_month
    assert b                                       # the budget is not consulted

    kw = dict(include_predictions=False, today="2026-06-30")
    # July, which closes 800 to the good, survives a 500 contribution...
    ok = budgets.check_floor(conn, "2026-07-01", "2026-07-31",
                             extra=[("2026-07-20", -500_00)], **kw)
    assert ok.breaks is False and ok.low_cents == 300_00

    # ...and does not survive 900 of it. The date is when it goes under.
    bad = budgets.check_floor(conn, "2026-07-01", "2026-07-31",
                              extra=[("2026-07-20", -900_00)], **kw)
    assert bad.breaks is True
    assert bad.low_cents == -100_00 and bad.low_date == "2026-07-20"
    assert bad.shortfall_cents == 100_00

    # A proposal that only breaks a RAISED cushion is still caught.
    cushioned = budgets.check_floor(conn, "2026-07-01", "2026-07-31",
                                    cushion_cents=400_00,
                                    extra=[("2026-07-20", -500_00)], **kw)
    assert cushioned.breaks is True and cushioned.low_date == "2026-07-20"

    # Nothing was written: the plain check is unchanged afterwards.
    assert budgets.check_floor(conn, "2026-07-01", "2026-07-31",
                              **kw).low_cents == 800_00


def test_floor_accounts_exclude_closed_ones_unlike_spending_accounts(conn):
    """A closed account's balance cannot pay the rent.

    :func:`budgets.spending_account_ids` includes closed accounts on purpose --
    money spent out of one was still spending -- but counting it toward a
    FORWARD floor would cushion the projection with cash that is not there."""
    open_acct = ledger.create_account(conn, "Everyday", "checking")
    gone = ledger.create_account(conn, "Old Thrift", "savings")
    ledger.update_account(conn, gone, closed_flag=1)
    assert set(budgets.spending_account_ids(conn)) == {open_acct, gone}
    assert budgets.floor_account_ids(conn) == [open_acct]


def test_month_allowance_is_the_same_plan_the_burn_down_starts_from(conn, cats):
    """One definition of "the month's plan", shared so the two cannot drift.

    The burn-down's allowance and the floor's planned spending are the same
    figure -- each line's target plus what rollover carried in -- and this pins
    them to the same helper rather than to two copies of the arithmetic."""
    acct = ledger.create_account(conn, "Everyday", "checking")
    b = budgets.create_budget(conn, "Plan")
    budgets.set_line(conn, b, cats["groceries"], "2026-03", 150_00, rollover=True)
    budgets.set_line(conn, b, cats["groceries"], "2026-04", 150_00, rollover=True)
    ledger.add_transaction(conn, acct, "2026-03-05", -100_00, payee="Market",
                           category_id=cats["groceries"])

    allowance = budgets.month_allowance(conn, b, "2026-04")
    assert allowance == {cats["groceries"]: 150_00 + 50_00}   # 50 carried in
    assert budgets.planned_spending_cents(conn, b, "2026-04") == 200_00
    bd = budgets.burn_down(conn, b, "2026-04", include_predictions=False,
                           today="2026-04-30")
    assert bd.allowance_cents == budgets.planned_spending_cents(conn, b, "2026-04")


# ---- savings goals, from the budgets side ----------------------------------
def test_goals_write_budget_rows_only_through_budgets(conn, cats):
    """``mammon.budgets`` stays the sole writer of budget tables, including when
    a savings goal pushes its monthly contribution into a plan.

    A goal naming a category lands in ``budget_lines``; an account-backed goal
    without one lands in ``budget_saving_lines`` (SRD 5.12d), which is the row
    the Set tab already renders. The point of the test is the routing, not the
    arithmetic: nothing in ``goals`` may INSERT into these tables itself."""
    from mammon import goals

    acct = ledger.create_account(conn, "Set Aside", "savings")
    b = budgets.create_budget(conn, "Plan")

    by_category = goals.create_goal(conn, "Category Goal", 1_200_00,
                                    budget_id=b, category_id=cats["groceries"],
                                    monthly_cents=100_00)
    assert goals.apply_to_budget(conn, by_category, ["2026-03"]) == 1
    assert [(ln.category_id, ln.amount_cents)
            for ln in budgets.get_lines(conn, b, period="2026-03")] == [
                (cats["groceries"], 100_00)]

    by_account = goals.create_goal(conn, "Account Goal", 2_400_00, budget_id=b,
                                   account_id=acct, monthly_cents=200_00)
    assert goals.apply_to_budget(conn, by_account, ["2026-03"]) == 1
    assert [(s.account_id, s.amount_cents)
            for s in budgets.get_saving_lines(conn, b, period="2026-03")] == [
                (acct, 200_00)]

    # Re-applying the same month overwrites rather than duplicating, the same
    # upsert every other budget writer gets.
    goals.update_goal(conn, by_account, monthly_cents=250_00)
    goals.apply_to_budget(conn, by_account, ["2026-03"])
    assert [(s.account_id, s.amount_cents)
            for s in budgets.get_saving_lines(conn, b, period="2026-03")] == [
                (acct, 250_00)]


def test_a_savings_contribution_is_checked_against_the_one_floor_seam(conn):
    """``goals.check_contribution`` must reach :func:`budgets.check_floor`, not
    a second copy of the projection. Patching the seam and watching the call
    arrive is the only way to pin that from the outside."""
    from mammon import goals

    checking = ledger.create_account(conn, "Everyday", "checking")
    savings = ledger.create_account(conn, "Set Aside", "savings")
    ledger.add_transaction(conn, checking, "2026-03-01", 500_00,
                           payee="Opening Balance")
    gid = goals.create_goal(conn, "Account Goal", 1_200_00, account_id=savings)

    seen = {}
    real = budgets.check_floor

    def spy(c, start, end, **kw):
        seen.update(kw)
        seen["start"], seen["end"] = start, end
        return real(c, start, end, **kw)

    budgets.check_floor = spy
    try:
        check = goals.check_contribution(conn, gid, "2026-03-10", 100_00,
                                         cushion_cents=50_00,
                                         today="2026-03-01")
    finally:
        budgets.check_floor = real

    assert seen["start"] == "2026-03-01" and seen["end"] == "2026-03-31"
    assert seen["cushion_cents"] == 50_00
    # The proposed payment is passed as `extra`, so it is tested BEFORE any row
    # is written...
    assert seen["extra"] == (("2026-03-10", -100_00),)
    # ... and the goal's own backing account is left out of the floor set, or a
    # checking-to-savings contribution would net to zero and look free.
    assert savings not in seen["account_ids"]
    assert checking in seen["account_ids"]
    assert check.breaks is False

    # Nothing was written by asking.
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


# ---- take-home, schedules and named groups (the 2026-09 revision) -----------
def _paycheck(conn, acct, cats, *, next_date="2026-06-05", frequency="biweekly"):
    """A scheduled biweekly paycheck: 1,000.00 gross, 100.00 state tax and
    40.00 medical withheld, 860.00 net. Money IN with deduction legs."""
    from mammon import scheduled

    tax = ledger.resolve_category(conn, "Taxes:State")
    medical = ledger.resolve_category(conn, "Medical:Premiums")
    sid = scheduled.add_scheduled(
        conn, acct, payee="Employer", amount=860_00, frequency=frequency,
        next_date=next_date, category_id=None,
        splits=[{"category_id": cats["salary"], "amount": 1_000_00, "memo": ""},
                {"category_id": tax, "amount": -100_00, "memo": ""},
                {"category_id": medical, "amount": -40_00, "memo": ""}])
    return sid, tax, medical


def _post_paycheck(conn, acct, cats, date, *, net=860_00, tax_cents=100_00,
                   medical_cents=40_00):
    tax = ledger.resolve_category(conn, "Taxes:State")
    medical = ledger.resolve_category(conn, "Medical:Premiums")
    tid = ledger.add_transaction(conn, acct, date, net, payee="Employer")
    ledger.set_splits(conn, tid, [
        (cats["salary"], net + tax_cents + medical_cents, ""),
        (tax, -tax_cents, ""), (medical, -medical_cents, "")])
    return tid


def test_budget_actuals_are_take_home_and_deductions_are_not_spending(conn, cats):
    """The budget is planned from take-home pay: a paycheck's withholding and
    premium legs never reach a budget actual, the burn-down or Everything
    else, while a spending REPORT on the same category still finds them and
    the retirement basis can add them back."""
    from mammon.reports.spending import spending_by_category

    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    tax = ledger.resolve_category(conn, "Taxes:State")
    medical = ledger.resolve_category(conn, "Medical:Premiums")
    _post_paycheck(conn, acct, cats, "2026-06-05")
    _post_paycheck(conn, acct, cats, "2026-06-19")
    # A copay paid from checking IS spending in the premium's category.
    ledger.add_transaction(conn, acct, "2026-06-10", -25_00, category_id=medical)
    # An unsplit paycheck, as a beginner enters it: nothing to deduct.
    ledger.add_transaction(conn, acct, "2026-06-26", 500_00, category_id=cats["salary"])

    actuals = budgets._month_actuals(conn, "2026-06")
    assert tax not in actuals
    assert actuals[medical] == 25_00
    full = {r.category_id: r.own_cents
            for r in spending_by_category(conn, "2026-06-01", "2026-06-30").flat()}
    assert full[tax] == 200_00 and full[medical] == 80_00 + 25_00
    assert budgets.payroll_deductions(conn, "2026-06-01", "2026-06-30") == {
        tax: 200_00, medical: 80_00}
    # Take-home received, attributed to the paycheck's income leg.
    assert budgets.month_income(conn, "2026-06") == {cats["salary"]: 2 * 860_00 + 500_00}

    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-06", "2027-05")
    budgets.set_line(conn, b, medical, "2026-06", 30_00)
    rows = {r.category_id: r for r in budgets.budget_vs_actual(conn, b, "2026-06")}
    assert rows[medical].actual_cents == 25_00
    assert tax not in rows                        # not even as unbudgeted
    other = budgets.other_spending(conn, b, "2026-06")
    assert other.actual_cents == 0 and other.by_category == ()
    burn = budgets.burn_down(conn, b, "2026-06", include_predictions=False,
                             today="2026-06-30")
    assert burn.spent_cents == 25_00


def test_income_lines_plan_take_home_and_stay_out_of_expense_rows(conn, cats):
    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    _post_paycheck(conn, acct, cats, "2026-06-05")
    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-06", "2027-05")
    budgets.set_settings(conn, b, cats["salary"], bucket="income")
    budgets.set_line(conn, b, cats["salary"], "2026-06", 1_720_00)
    budgets.set_line(conn, b, cats["groceries"], "2026-06", 400_00)

    expense = budgets.budget_vs_actual(conn, b, "2026-06")
    assert [r.category_id for r in expense] == [cats["groceries"]]
    with_income = {r.category_id: r for r in budgets.budget_vs_actual(
        conn, b, "2026-06", include_income=True)}
    pay = with_income[cats["salary"]]
    assert pay.is_income and not pay.discretionary
    assert (pay.budgeted_cents, pay.actual_cents, pay.remaining_cents) == (
        1_720_00, 860_00, 860_00)
    # Not an envelope: the allowance, the item set and coverage ignore it.
    assert budgets.month_allowance(conn, b, "2026-06") == {cats["groceries"]: 400_00}
    assert budgets.budget_item_category_ids(conn, b) == (cats["groceries"],)


def test_scheduled_amounts_count_a_biweekly_bill_per_month_both_ways(conn, cats):
    """A biweekly bill lands two or three times a month, and the plan's months
    BEFORE the schedule's next date are counted too; a scheduled paycheck's
    deduction legs are NOT bills (take-home), but its net is scheduled income."""
    from mammon import scheduled

    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    daycare = ledger.resolve_category(conn, "Childcare")
    scheduled.add_scheduled(conn, acct, payee="Daycare", amount=-100_00,
                            frequency="biweekly", next_date="2026-06-05",
                            category_id=daycare)
    _sid, tax, _medical = _paycheck(conn, acct, cats, next_date="2026-06-05")
    periods = budgets.period_sequence("2026-01", 12)
    by_cat = budgets.scheduled_amounts(conn, periods)
    assert tax not in by_cat
    # Every 14 days through 2026-06-05, walked back to Jan 2: three in January
    # (2, 16, 30) and July (3, 17, 31), two in every other month.
    counts = {p: by_cat[daycare][p] // 100_00 for p in periods}
    assert sum(counts.values()) == 26
    assert counts["2026-01"] == 3 and counts["2026-07"] == 3
    assert counts["2026-05"] == 2 and counts["2026-06"] == 2
    income = budgets.scheduled_income(conn, periods)
    assert income == {cats["salary"]: {p: c * 860_00 for p, c in counts.items()}}
    summary = budgets.schedule_summary(conn, daycare, periods)
    assert summary == [{"payee": "Daycare", "frequency": "biweekly",
                        "per_occurrence_cents": 100_00, "occurrences": 26}]


def test_seed_proposes_a_scheduled_bill_as_fixed_and_applies_it_per_month(
        conn, cats):
    """A scheduled bill is proposed as ``fixed`` from the schedule, and Accept
    writes the occurrence count per month -- not the annual total over twelve.
    A paycheck's deduction legs are never proposed."""
    from mammon import scheduled

    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    daycare = ledger.resolve_category(conn, "Childcare")
    scheduled.add_scheduled(conn, acct, payee="Daycare", amount=-100_00,
                            frequency="biweekly", next_date="2026-07-03",
                            category_id=daycare)
    _sid, tax, _medical = _paycheck(conn, acct, cats, next_date="2026-07-03")
    proposals = {p.category_id: p for p in budgets.seed_from_history(
        conn, months=12, today=_dt.date(2026, 6, 17))}
    assert tax not in proposals
    care = proposals[daycare]
    assert (care.bucket, care.basis) == ("fixed", "scheduled")
    assert care.annual_cents == 26 * 100_00
    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-07", "2027-06")
    budgets.apply_proposals(conn, b, [care], start_period="2026-07")
    lines = {ln.period: ln.amount_cents for ln in budgets.get_lines(conn, b)}
    assert sum(lines.values()) == 26 * 100_00
    assert sorted(set(lines.values())) == [200_00, 300_00]
    assert budgets.get_setting(conn, b, daycare).bucket == "fixed"
    written = budgets.fill_line_from_schedule(conn, b, daycare)
    assert sum(written.values()) == 26 * 100_00
    with pytest.raises(ValueError):
        budgets.fill_line_from_schedule(conn, b, cats["dining"])


def test_a_scheduled_bill_is_committed_until_it_posts_and_a_paycheck_is_not(
        conn, cats):
    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    _sid, tax, _medical = _paycheck(conn, acct, cats, next_date="2026-06-26",
                                    frequency="monthly")
    assert budgets.month_committed(conn, "2026-06") == {}
    b = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, b, tax, "2026-06", 100_00)
    row = {r.category_id: r for r in budgets.budget_vs_actual(conn, b, "2026-06")}[tax]
    assert (row.actual_cents, row.committed_cents) == (0, 0)


def test_other_spending_is_what_the_plan_has_no_line_for(conn, cats):
    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    groc, dining, fuel = cats["groceries"], cats["dining"], cats["fuel"]
    ledger.add_transaction(conn, acct, "2026-06-03", -300_00, category_id=groc)
    ledger.add_transaction(conn, acct, "2026-06-04", -45_00, category_id=dining)
    ledger.add_transaction(conn, acct, "2026-06-05", -60_00, category_id=fuel)
    ledger.add_transaction(conn, acct, "2026-06-06", -12_50)        # uncategorized
    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-06", "2027-05")
    budgets.set_line(conn, b, groc, "2026-06", 400_00)
    food = budgets.create_group(conn, b, "Food")
    budgets.set_member_group(conn, b, dining, food)

    other = budgets.other_spending(conn, b, "2026-06")
    assert other.actual_cents == 72_50
    assert [(cid, cents) for cid, _p, cents in other.by_category] == [
        (fuel, 60_00), (None, 12_50)]
    assert other.category_ids == (fuel,)
    assert other.planned_cents == 0
    budgets.set_other_line(conn, b, "2026-06", 100_00)
    assert budgets.other_spending(conn, b, "2026-06").planned_cents == 100_00
    assert budgets.get_other_lines(conn, b) == {"2026-06": 100_00}
    budgets.clear_other_line(conn, b, "2026-06")
    assert budgets.get_other_lines(conn, b) == {}


def test_line_order_is_kept_per_budget_and_copied(conn, cats):
    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-06", "2027-05")
    food = budgets.create_group(conn, b, "Food")
    sav = ledger.create_account(conn, "Savings", "savings")
    order = [("category", cats["salary"]), ("account", sav), ("group", food),
             ("category", cats["fuel"])]
    budgets.set_line_order(conn, b, order)
    assert budgets.line_order(conn, b) == {k: i for i, k in enumerate(order)}
    with pytest.raises(ValueError):
        budgets.set_line_order(conn, b, [("envelope", 1)])
    budgets.set_other_line(conn, b, "2026-07", 50_00)
    copy = budgets.copy_budget(conn, b, "Next", shift_months=12)
    (cg,) = budgets.list_groups(conn, copy)
    copied = budgets.line_order(conn, copy)
    assert copied[("group", cg.id)] == 2 and copied[("account", sav)] == 1
    assert budgets.get_other_lines(conn, copy) == {"2027-07": 50_00}
    budgets.move_budget_start(conn, copy, "2028-01")
    assert budgets.get_other_lines(conn, copy) == {"2028-07": 50_00}
    budgets.delete_budget(conn, copy)
    assert budgets.line_order(conn, copy) == {}


def test_trailing_samples_pivot_spending_income_and_saving(conn, cats):
    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    sav = ledger.create_account(conn, "Savings", "savings")
    for period, _s, _e in budgets.trailing_months(_dt.date(2026, 6, 17), 12):
        ledger.add_transaction(conn, acct, f"{period}-10", -300_00,
                               category_id=cats["groceries"])
        _post_paycheck(conn, acct, cats, f"{period}-05")
        ledger.create_transfer(conn, acct, sav, f"{period}-06", 100_00)
    hist = budgets.trailing_samples(conn, today=_dt.date(2026, 6, 17))
    assert len(hist.periods) == 12 and hist.periods[-1] == "2026-05"
    assert hist.spending[cats["groceries"]] == [300_00] * 12
    assert ledger.resolve_category(conn, "Taxes:State") not in hist.spending
    assert hist.income[cats["salary"]] == [860_00] * 12
    assert hist.saving[sav] == [100_00] * 12
    summary = budgets.HistorySamples.summary([100_00, 300_00, 200_00])
    assert summary["total"] == 600_00 and summary["mean"] == 200_00
    assert summary["high"] == 300_00 and summary["high_index"] == 1
    assert summary["volatile"] is False
    assert budgets.HistorySamples.summary([0, 0, 900_00])["volatile"] is True


def test_group_holds_the_budget_and_members_hold_the_actuals(conn, cats):
    """Joining a group folds the member's lines into the pot; the group row
    carries the budget, the members' summed spending and commitments, and the
    carry; members are left out of the default row set and reported, not
    graded, when asked for."""
    from mammon import scheduled

    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    groc, dining = cats["groceries"], cats["dining"]
    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-05", "2027-04")
    for period in ("2026-05", "2026-06"):
        budgets.set_line(conn, b, groc, period, 400_00)
        budgets.set_line(conn, b, dining, period, 200_00)
    food = budgets.create_group(conn, b, "Food", rollover_mode="both")
    budgets.set_member_group(conn, b, groc, food)
    budgets.set_member_group(conn, b, dining, food)
    assert budgets.get_lines(conn, b) == []
    assert {gl.period: gl.amount_cents for gl in budgets.get_group_lines(conn, b)} \
        == {"2026-05": 600_00, "2026-06": 600_00}
    assert budgets.group_members(conn, b) == {food: [groc, dining]}
    assert budgets.member_groups(conn, b) == {groc: food, dining: food}

    ledger.add_transaction(conn, acct, "2026-05-10", -500_00, category_id=groc)
    ledger.add_transaction(conn, acct, "2026-05-12", -150_00, category_id=dining)
    ledger.add_transaction(conn, acct, "2026-06-03", -300_00, category_id=groc)
    scheduled.add_scheduled(conn, acct, payee="Bistro", amount=-60_00,
                            frequency="monthly", next_date="2026-06-20",
                            category_id=dining)

    rows = budgets.budget_vs_actual(conn, b, "2026-06")
    assert [r.category_name for r in rows] == ["Food"]
    pot = rows[0]
    assert pot.is_group and pot.group_id == food
    assert pot.category_id == budgets.group_key(food) == -food
    assert pot.budgeted_cents == 600_00
    assert pot.actual_cents == 300_00
    assert pot.committed_cents == 60_00
    assert pot.carried_in_cents == 600_00 - 650_00          # May overspent
    assert pot.remaining_cents == 600_00 - 50_00 - 300_00
    assert pot.uncommitted_cents == pot.remaining_cents - 60_00

    with_members = budgets.budget_vs_actual(conn, b, "2026-06",
                                            include_members=True)
    assert [r.category_name for r in with_members] == ["Dining", "Food", "Groceries"]
    by_name = {r.category_name: r for r in with_members}
    assert by_name["Groceries"].is_member and by_name["Groceries"].group_id == food
    assert by_name["Groceries"].actual_cents == 300_00
    assert by_name["Groceries"].budgeted_cents == 0
    assert by_name["Groceries"].carried_in_cents == 0
    assert by_name["Dining"].committed_cents == 60_00
    # The default row set partitions the money: summing it counts the pot once.
    assert sum(r.actual_cents for r in rows) == 300_00
    # The calendar's item set and allowance see one item, the pot.
    assert budgets.budget_item_category_ids(conn, b) == (-food,)
    assert budgets.month_allowance(conn, b, "2026-06") == {-food: 550_00}
    burn = budgets.burn_down(conn, b, "2026-06", include_predictions=False,
                             today="2026-06-30")
    assert [s.category_name for s in burn.per_category] == ["Food"]
    assert burn.per_category[0].spent_cents == 300_00
    assert burn.per_category[0].committed_cents == 60_00
    # Coverage counts a member as budgeted through its group's line.
    assert budgets.budget_coverage(conn, b, as_of="2026-06-30") == Decimal("100.0")


def test_group_lifecycle_release_delete_copy_and_move(conn, cats):
    groc, dining = cats["groceries"], cats["dining"]
    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-01", "2026-12")
    food = budgets.create_group(conn, b, "Food", bucket="flex")
    with pytest.raises(ValueError):
        budgets.create_group(conn, b, "food")           # names are unique
    with pytest.raises(ValueError):
        budgets.create_group(conn, b, "   ")
    other = budgets.create_budget(conn, "Other", active=False)
    with pytest.raises(ValueError):
        budgets.set_settings(conn, other, groc, group_id=food)   # not its group
    budgets.set_line(conn, b, groc, "2026-03", 100_00)
    budgets.set_group_members(conn, food, [groc, dining])
    budgets.set_group_line(conn, b, food, "2026-04", 50_00)
    budgets.update_group(conn, food, name="Eating", rollover_mode="positive")
    assert budgets.get_group(conn, food).name == "Eating"

    # A scenario copies the group, its lines and its members, shifted.
    copy = budgets.copy_budget(conn, b, "Next year", shift_months=12)
    (cg,) = budgets.list_groups(conn, copy)
    assert cg.id != food and cg.name == "Eating"
    assert budgets.group_members(conn, copy) == {cg.id: [groc, dining]}
    assert {gl.period: gl.amount_cents for gl in budgets.get_group_lines(conn, copy)} \
        == {"2027-03": 100_00, "2027-04": 50_00}
    # Moving the start wraps group lines with everything else.
    budgets.move_budget_start(conn, copy, "2028-01")
    assert {gl.period for gl in budgets.get_group_lines(conn, copy)} == \
        {"2028-03", "2028-04"}

    # Releasing a member clears the pointer and leaves the pot's money alone.
    budgets.set_member_group(conn, b, dining, None)
    assert budgets.group_members(conn, b) == {food: [groc]}
    assert budgets.get_lines(conn, b) == []
    # Deleting the group releases the rest and drops its lines.
    budgets.delete_group(conn, food)
    assert budgets.list_groups(conn, b) == []
    assert budgets.get_setting(conn, b, groc).group_id is None
    assert budgets.get_group_lines(conn, b) == []
    # And deleting a budget takes its groups with it.
    budgets.delete_budget(conn, copy)
    assert budgets.list_groups(conn, copy) == []


def test_seeding_folds_a_members_proposal_into_its_group(conn, cats):
    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    groc, dining = cats["groceries"], cats["dining"]
    for period, _s, _e in budgets.trailing_months(_dt.date(2026, 6, 17), 12):
        ledger.add_transaction(conn, acct, f"{period}-10", -300_00, category_id=groc)
        ledger.add_transaction(conn, acct, f"{period}-11", -100_00, category_id=dining)
    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-06", "2027-05")
    food = budgets.create_group(conn, b, "Food")
    budgets.set_group_members(conn, food, [groc, dining])
    proposals = budgets.seed_from_history(conn, months=12, today=_dt.date(2026, 6, 17))
    budgets.apply_proposals(conn, b, proposals, start_period="2026-06")
    assert budgets.get_lines(conn, b) == []
    lines = {gl.period: gl.amount_cents for gl in budgets.get_group_lines(conn, b)}
    assert lines == {p: 400_00 for p in budgets.period_sequence("2026-06", 12)}


def test_group_rows_reach_the_range_report_and_the_retirement_basis(conn, cats):
    """The range report sums the pot once and flags it; the retirement basis
    reads group lines as lines and classifies them by the group's name."""
    from mammon.reports import budget as budget_report

    groc, dining = cats["groceries"], cats["dining"]
    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-01", "2026-12")
    food = budgets.create_group(conn, b, "Food")
    budgets.set_group_members(conn, food, [groc, dining])
    taxes = budgets.create_group(conn, b, "Taxes withheld")
    for period in budgets.period_sequence("2026-01", 12):
        budgets.set_group_line(conn, b, food, period, 500_00)
        budgets.set_group_line(conn, b, taxes, period, 300_00)
        budgets.set_line(conn, b, cats["fuel"], period, 100_00)

    rep = budget_report.budget_vs_actual_range(conn, b, "2026-01", "2026-03")
    totals = {t.category_name: t for t in rep.category_totals}
    assert totals["Food"].is_group and totals["Food"].group_id == food
    assert totals["Food"].budgeted_cents == 1_500_00
    assert not totals["Auto & Transport:Fuel" if "Auto & Transport:Fuel" in totals
                      else "Fuel"].is_group
    assert rep.total_budgeted_cents == 3 * 900_00

    basis = budget_report.retirement_spending_basis(conn, b, "2026-12-31")
    assert basis.total_cents == 12 * 900_00
    assert [e.category_name for e in basis.excluded] == ["Taxes withheld"]
    assert basis.included_cents == 12 * 600_00


def test_dated_frequency_counts_and_fill_line_by_first_date(conn, cats):
    """A weekly or biweekly line takes a first DATE: each plan month gets the
    occurrences that land in it, anchored on that date whether it falls before
    or inside the plan, and ``fill_line`` writes occurrences times the amount
    per occurrence."""
    # Plan Jun 2026 - May 2027; paydays every 14 days from 2026-06-19.
    counts = budgets.dated_frequency_counts("2026-06", "2026-06-19", "biweekly")
    assert sum(counts.values()) == 25                 # June has only the 19th
    assert counts["2026-06"] == 1 and counts["2026-07"] == 3
    assert counts["2027-01"] == 3
    assert {p for p, n in counts.items() if n == 2} == {
        "2026-08", "2026-09", "2026-10", "2026-11", "2026-12", "2027-02",
        "2027-03", "2027-04", "2027-05"}
    # A first date BEFORE the plan anchors the grid; the plan takes what lands.
    earlier = budgets.dated_frequency_counts("2026-06", "2026-05-22", "biweekly")
    assert earlier["2026-06"] == 2 and sum(earlier.values()) == 26
    # Weekly: 4 or 5 a month, 52 or 53 in the year.
    weekly = budgets.dated_frequency_counts("2026-06", "2026-06-01", "weekly")
    assert sorted(set(weekly.values())) == [4, 5]
    assert sum(weekly.values()) in (52, 53)
    with pytest.raises(ValueError):
        budgets.dated_frequency_counts("2026-06", "2027-07-01", "biweekly")
    with pytest.raises(ValueError):
        budgets.dated_frequency_counts("2026-06", "2026-06-01", "monthly")

    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-06", "2027-05")
    written = budgets.fill_line(conn, b, 100_00, frequency="biweekly",
                                first_date="2026-06-19", category_id=cats["fuel"])
    assert written == sorted(counts)
    lines = {ln.period: ln.amount_cents for ln in budgets.get_lines(conn, b)}
    assert lines == {p: n * 100_00 for p, n in counts.items()}
    with pytest.raises(ValueError):
        budgets.fill_line(conn, b, 100_00, frequency="biweekly",
                          category_id=cats["fuel"])          # no first date
    with pytest.raises(ValueError):
        budgets.fill_line(conn, b, 100_00, frequency="monthly",
                          category_id=cats["fuel"])          # no first month


def test_a_payee_line_counts_the_whole_payment_once(conn, cats):
    """A mortgage payment is a split - principal to the loan, interest, escrow.
    A payee line claims it WHOLE, and the interest and escrow legs leave the
    category actuals, Everything else and the coverage gaps, so the money is
    counted once. A plain transfer to the loan counts too; a card payment
    between two spending accounts does not."""
    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    loan = ledger.create_account(conn, "Home Loan", "liability")
    card = ledger.create_account(conn, "Rewards Card", "credit")
    interest = ledger.resolve_category(conn, "Mortgage:Interest")
    escrow = ledger.resolve_category(conn, "Mortgage:Escrow")
    tid = ledger.add_transaction(conn, acct, "2026-06-01", -1_500_00, payee="Acme Mortgage")
    ledger.set_splits(conn, tid, [
        {"transfer_account_id": loan, "amount": -900_00, "memo": ""},
        {"category_id": interest, "amount": -450_00, "memo": ""},
        {"category_id": escrow, "amount": -150_00, "memo": ""}])
    ledger.create_transfer(conn, acct, loan, "2026-06-15", 200_00, payee="Acme Mortgage extra")
    ledger.create_transfer(conn, acct, card, "2026-06-16", 300_00, payee="Acme Card")
    ledger.add_transaction(conn, acct, "2026-06-20", -40_00, category_id=interest)   # unrelated

    found = budgets.payee_payments(conn, "2026-06-01", "2026-06-30", "acme mortgage")
    assert found.whole_cents == 1_700_00
    assert found.legs == {interest: 450_00, escrow: 150_00}
    assert [p[2] for p in found.payments] == [1_500_00, 200_00]
    assert budgets.payee_payments(conn, "2026-06-01", "2026-06-30", "acme card").whole_cents == 0
    assert budgets.payee_history(conn, "Acme Mortgage", ["2026-05", "2026-06"]) == [0, 1_700_00]

    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-06", "2027-05")
    mortgage = budgets.create_group(conn, b, "Mortgage", bucket="fixed",
                                    payee_match="Acme Mortgage")
    assert budgets.get_group(conn, mortgage).by_payee
    budgets.set_group_line(conn, b, mortgage, "2026-06", 1_700_00)
    rows = {r.category_name: r for r in budgets.budget_vs_actual(conn, b, "2026-06")}
    assert rows["Mortgage"].actual_cents == 1_700_00
    assert rows["Mortgage"].is_group and rows["Mortgage"].bucket == "fixed"
    # The interest category keeps only the unrelated 40.00.
    assert rows["Interest"].actual_cents == 40_00
    assert "Escrow" not in rows
    other = budgets.other_spending(conn, b, "2026-06")
    assert [(cid, cents) for cid, _p, cents in other.by_category] == [(interest, 40_00)]
    # Category spending in the window is 640.00 (interest 490, escrow 150); the
    # line claims 600.00 of it. The principal is a transfer, not spending.
    assert budgets.budget_coverage(conn, b, as_of="2026-06-30") == Decimal("93.8")
    burn = budgets.burn_down(conn, b, "2026-06", include_predictions=False,
                             today="2026-06-30")
    bars = {s.category_name: s for s in burn.per_category}
    assert bars["Mortgage"].spent_cents == 1_700_00
    # Copying a budget keeps the match; a second line never claims the same row.
    copy = budgets.copy_budget(conn, b, "Next", shift_months=12)
    assert [g.payee_match for g in budgets.list_groups(conn, copy)] == ["Acme Mortgage"]
    twin = budgets.create_group(conn, b, "Mortgage again", payee_match="acme")
    claims = budgets.payee_claims(conn, b, "2026-06")
    assert claims[mortgage].whole_cents == 1_700_00 and claims[twin].whole_cents == 0
    budgets.update_group(conn, twin, payee_match=None)
    assert not budgets.get_group(conn, twin).by_payee


def test_a_scheduled_payment_is_committed_to_its_payee_line_until_it_posts(conn, cats):
    from mammon import scheduled

    acct = ledger.create_account(conn, "Everyday Checking", "checking")
    loan = ledger.create_account(conn, "Home Loan", "liability")
    interest = ledger.resolve_category(conn, "Mortgage:Interest")
    scheduled.add_scheduled(
        conn, acct, payee="Acme Mortgage", amount=-1_500_00, frequency="monthly",
        next_date="2026-06-28", category_id=None,
        splits=[{"transfer_account_id": loan, "amount": -1_000_00, "memo": ""},
                {"category_id": interest, "amount": -500_00, "memo": ""}])
    b = budgets.create_budget(conn, "Household")
    budgets.set_budget_period(conn, b, "2026-06", "2027-05")
    mortgage = budgets.create_group(conn, b, "Mortgage", bucket="fixed",
                                    payee_match="Acme Mortgage")
    budgets.set_group_line(conn, b, mortgage, "2026-06", 1_500_00)
    rows = {r.category_name: r for r in budgets.budget_vs_actual(conn, b, "2026-06")}
    assert (rows["Mortgage"].actual_cents, rows["Mortgage"].committed_cents) == (
        0, 1_500_00)
    assert "Interest" not in rows                  # its leg went to the line
    tid = ledger.add_transaction(conn, acct, "2026-06-28", -1_500_00, payee="Acme Mortgage")
    ledger.set_splits(conn, tid, [
        {"transfer_account_id": loan, "amount": -1_000_00, "memo": ""},
        {"category_id": interest, "amount": -500_00, "memo": ""}])
    rows = {r.category_name: r for r in budgets.budget_vs_actual(conn, b, "2026-06")}
    assert (rows["Mortgage"].actual_cents, rows["Mortgage"].committed_cents) == (
        1_500_00, 0)

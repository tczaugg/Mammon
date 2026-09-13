"""The pie-chart Other rollup, drill-down and whole-total percentages (SRD 5.9c).

All three category pies -- Spending by Category, Income by Category and Asset
Allocation -- share ``ui/charts.SlicesPieCanvas`` and its ``group_small_slices``
helper. This pins the three behaviours the helper must guarantee for every one
of them:

1. ``Other`` is the SET OF LOWEST-share categories that together reach 10% of
   the total (accumulate smallest-upward until the running sum clears 10%; the
   category that tips it over is included), NOT a per-slice threshold.
2. Clicking ``Other`` drills into its component categories.
3. Every wedge's percentage is its share of the WHOLE period total, at any drill
   depth -- a category that is 5% of everything reads 5% even when it is 50% of
   the ``Other`` it was drilled into.

The pure helper is exercised without a database; the drill/percentage path is
exercised through a seeded ledger and the real ``reports.spending_category_rows``
(the ungrouped feed that fixed the previously-broken Spending pie drill-down).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger, reports
from mammon.ui.charts import SlicesPieCanvas, group_small_slices


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "pie_rollup.db")
    yield c
    c.close()


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


START, END = "2026-01-01", "2026-01-31"


# --- 1. the pure rollup helper: Other = lowest categories summing to >=10% ----
def test_other_is_lowest_categories_summing_to_at_least_ten_percent():
    # Total 1000; smallest-upward 25 + 35 + 40 = 100 = exactly 10%, so those
    # three lowest categories are Other and the two big ones stand alone.
    rows = [("Salary", 700_00), ("Bonus", 200_00), ("Interest", 40_00),
            ("Dividends", 35_00), ("Gifts", 25_00)]
    drawn, grouped = group_small_slices(rows, 10.0)
    assert drawn == [("Salary", 700_00), ("Bonus", 200_00),
                     ("Other", 40_00 + 35_00 + 25_00)]
    assert grouped == [("Interest", 40_00), ("Dividends", 35_00),
                       ("Gifts", 25_00)]                    # largest first
    total = sum(c for _, c in rows)
    assert dict(drawn)["Other"] >= total // 10             # at least 10%


def test_other_includes_the_category_that_crosses_the_bar():
    # 6% alone is under the bar; 6 + 6 = 12% clears it, so BOTH smallest go into
    # Other -- the category that tips the running sum over the bar is part of the
    # set. A per-slice "< 10%" rule would instead have folded B (8%) too.
    rows = [("A", 80_00), ("B", 8_00), ("C", 6_00), ("D", 6_00)]
    drawn, grouped = group_small_slices(rows, 10.0)
    assert dict(drawn)["Other"] == 6_00 + 6_00
    assert {lab for lab, _ in grouped} == {"C", "D"}
    assert set(dict(drawn)) == {"A", "B", "Other"}         # B stays on its own


def test_single_slice_over_the_bar_is_never_renamed_other():
    # The smallest category alone already clears 10%, so nothing is grouped: a
    # single slice is never hidden behind a nameless "Other".
    rows = [("A", 60_00), ("B", 40_00)]
    assert group_small_slices(rows, 10.0) == (rows, [])


def test_rollup_never_swallows_every_category():
    # 5% is under the bar, but reaching it would have to pull in the 95%
    # category, leaving nothing to draw -- so the pie stays ungrouped rather than
    # collapsing into a single wedge.
    rows = [("A", 95_00), ("B", 5_00)]
    assert group_small_slices(rows, 10.0) == (rows, [])


def test_a_category_literally_called_other_joins_the_group():
    drawn, grouped = group_small_slices(
        [("Big", 60_00), ("Other", 35_00), ("Tiny", 3_00), ("Sliver", 2_00)], 10.0)
    assert drawn == [("Big", 60_00), ("Other", 40_00)]     # one Other wedge only
    assert grouped == [("Other", 35_00), ("Tiny", 3_00), ("Sliver", 2_00)]


# --- 2 & 3. drill-down + whole-total percentages, through the reports layer ---
@pytest.fixture
def spending(conn):
    """A checking account whose top-level EXPENSE categories have a long low
    tail: Fuel + Coffee + Snacks are the lowest set that reaches 10% of the
    1000.00 spent. Salary (money IN) and an out-of-window charge must not leak
    into the pie."""
    chk = ledger.create_account(conn, "Checking", "checking")
    rent = ledger.resolve_category(conn, "Rent")
    groc = ledger.resolve_category(conn, "Groceries")
    fuel = ledger.resolve_category(conn, "Fuel")
    coffee = ledger.resolve_category(conn, "Coffee")
    snacks = ledger.resolve_category(conn, "Snacks")
    salary = ledger.resolve_category(conn, "Salary")

    ledger.add_transaction(conn, chk, "2026-01-02", -700_00, category_id=rent)
    ledger.add_transaction(conn, chk, "2026-01-05", -200_00, category_id=groc)
    ledger.add_transaction(conn, chk, "2026-01-08", -50_00, category_id=fuel)
    ledger.add_transaction(conn, chk, "2026-01-12", -30_00, category_id=coffee)
    ledger.add_transaction(conn, chk, "2026-01-15", -20_00, category_id=snacks)
    ledger.add_transaction(conn, chk, "2026-01-20", 500_00, category_id=salary)
    ledger.add_transaction(conn, chk, "2026-02-03", -99_00, category_id=coffee)
    return conn


def test_spending_category_rows_are_raw_and_ungrouped(spending):
    # The feed for the pie is every top-level EXPENSE category, largest first,
    # with no Other collapse -- the canvas owns the rollup so it can break it
    # back out. Income (Salary) and the out-of-window Coffee charge are excluded.
    rows = reports.spending_category_rows(spending, START, END)
    assert rows == [("Rent", 700_00), ("Groceries", 200_00), ("Fuel", 50_00),
                    ("Coffee", 30_00), ("Snacks", 20_00)]


def test_spending_pie_drills_into_other_with_whole_total_percentages(qapp, spending):
    rows = reports.spending_category_rows(spending, START, END)
    canvas = SlicesPieCanvas("Spending by Category", rows)
    total = 1000_00

    # Top level: the lowest categories summing to >=10% become Other.
    assert dict(canvas.drawn_slices()) == {
        "Rent": 700_00, "Groceries": 200_00, "Other": 50_00 + 30_00 + 20_00}
    assert dict(canvas.grouped_members()) == {
        "Fuel": 50_00, "Coffee": 30_00, "Snacks": 20_00}
    assert canvas.whole_total() == total
    assert dict(canvas.slice_percentages())["Other"] == pytest.approx(10.0)

    # Clicking Other drills into its components (the fixed drill-down path).
    assert canvas.drill_into_other() is True
    assert canvas.zoom_path() == ["Other"]
    assert dict(canvas.current_slices()) == {
        "Fuel": 50_00, "Coffee": 30_00, "Snacks": 20_00}

    # Each drilled wedge's percentage is of the WHOLE period total, not of the
    # Other subset: Fuel is 5% of everything, NOT 50% of Other.
    pct = dict(canvas.slice_percentages())
    assert pct["Fuel"] == pytest.approx(50_00 / total * 100.0)     # 5.0
    assert pct["Fuel"] != pytest.approx(50_00 / (50_00 + 30_00 + 20_00) * 100.0)
    assert canvas.whole_total() == total                           # unchanged by drill

    assert canvas.zoom_out() is True and canvas.zoom_path() == []
    canvas.deleteLater()


def test_income_pie_shares_the_same_rollup_and_drill(qapp, conn):
    # The same helper drives the Income pie: raw income_category_rows in, Other
    # rolled up and drillable out.
    chk = ledger.create_account(conn, "Checking", "checking")
    for name, cents, day in [("Salary", 700_00, "10"), ("Bonus", 200_00, "12"),
                             ("Interest", 40_00, "14"), ("Dividends", 35_00, "16"),
                             ("Gifts", 25_00, "18")]:
        cat = ledger.resolve_category(conn, name)
        ledger.add_transaction(conn, chk, f"2026-01-{day}", cents, category_id=cat)

    rows = reports.income_category_rows(conn, START, END)
    canvas = SlicesPieCanvas("Income by Category", rows)
    assert dict(canvas.grouped_members()) == {
        "Interest": 40_00, "Dividends": 35_00, "Gifts": 25_00}
    assert canvas.drill_into_other() is True
    assert dict(canvas.current_slices()) == {
        "Interest": 40_00, "Dividends": 35_00, "Gifts": 25_00}
    # Interest is 4% of the 1000.00 whole, not 40% of the 100.00 Other subset.
    assert dict(canvas.slice_percentages())["Interest"] == pytest.approx(4.0)
    canvas.deleteLater()

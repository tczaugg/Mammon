"""Tests for the Income Chart's 'Other' rollup and drill-down (SRD 5.8d).

The Income Chart mirrors the asset-allocation pie's readability rule: a long
tail of tiny income categories is folded into ONE ``Other`` slice (everything
under 10% of the total), and clicking ``Other`` breaks it back out into its
component categories at full size. Both charts reuse the SAME shared helpers --
``reports.income_category_rows`` (the raw, ungrouped top-level rows) feeding
``ui.charts.SlicesPieCanvas`` / ``group_small_slices`` -- so the threshold and
rollup logic lives in exactly one place rather than being duplicated per chart.

Two halves, matching the two layers:

* the PURE aggregation ``reports.income_category_rows`` -- money IN per
  top-level income category, integer cents, transfers and expenses excluded,
  the date window honored, NO rollup -- exercised headless with no Qt; and
* the ``SlicesPieCanvas`` renderer, which performs the 10%-``Other`` rollup and
  the drill-down on those raw rows.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger, reports
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "income_rollup.db")
    yield c
    c.close()


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# Total income in January = $1,000. The three smallest categories are each under
# 10% of that (4%, 3.5%, 2.5%) and together sum to exactly 10% -- the material
# the rollup folds into one ``Other`` wedge.
START, END = "2026-01-01", "2026-01-31"


@pytest.fixture
def seeded(conn):
    """A checking account with several top-level INCOME categories (a couple of
    large ones and a long tail of tiny ones), plus the rows the income chart
    must ignore: an expense, a transfer, and out-of-range income."""
    chk = ledger.create_account(conn, "Checking", "checking")
    sav = ledger.create_account(conn, "Savings", "savings")

    salary = ledger.resolve_category(conn, "Salary")
    bonus = ledger.resolve_category(conn, "Bonus")
    interest = ledger.resolve_category(conn, "Interest")
    dividends = ledger.resolve_category(conn, "Dividends")
    gifts = ledger.resolve_category(conn, "Gifts")
    groceries = ledger.resolve_category(conn, "Groceries")
    consulting = ledger.resolve_category(conn, "Consulting")

    # In-range income (positive amounts -> income-type categories).
    ledger.add_transaction(conn, chk, "2026-01-28", 700_00, category_id=salary)
    ledger.add_transaction(conn, chk, "2026-01-15", 200_00, category_id=bonus)
    ledger.add_transaction(conn, chk, "2026-01-10", 40_00, category_id=interest)
    ledger.add_transaction(conn, chk, "2026-01-12", 35_00, category_id=dividends)
    ledger.add_transaction(conn, chk, "2026-01-20", 25_00, category_id=gifts)

    # Money OUT is not income; a transfer is not income (moving your own money);
    # an income row outside the window does not count toward the window's total.
    ledger.add_transaction(conn, chk, "2026-01-18", -50_00, category_id=groceries)
    ledger.create_transfer(conn, sav, chk, "2026-01-22", 500_00)
    ledger.add_transaction(conn, chk, "2026-02-05", 5000_00, category_id=consulting)
    return conn


def test_income_category_rows_are_raw_and_ungrouped(seeded):
    """The reports layer returns every top-level income category as its own row,
    largest first, in integer cents -- NO 'Other' rollup here (that is the
    canvas's job), transfers/expenses/out-of-range excluded."""
    rows = reports.income_category_rows(seeded, START, END)
    assert rows == [
        ("Salary", 700_00),
        ("Bonus", 200_00),
        ("Interest", 40_00),
        ("Dividends", 35_00),
        ("Gifts", 25_00),
    ]
    labels = [lab for lab, _ in rows]
    assert "Other" not in labels          # the reports layer never rolls up
    assert "Groceries" not in labels      # money out is not income
    assert "Consulting" not in labels     # out of the [START, END] window
    assert sum(c for _, c in rows) == 1000_00   # transfer of $500 excluded


def test_income_category_rows_account_filter(seeded):
    """Filtering to an account with no income yields nothing; ``[]`` account
    filter is 'no accounts', also nothing."""
    empty = [a["id"] for a in ledger.list_accounts(seeded)
             if a["name"] == "Savings"]
    assert reports.income_category_rows(seeded, START, END, empty) == []
    assert reports.income_category_rows(seeded, START, END, []) == []


def test_other_rollup_folds_sub_ten_percent(qapp, seeded):
    """SlicesPieCanvas folds the sub-10% tail into one ``Other`` slice that
    meets-or-exceeds 10% of the total, keeping the large categories drawn on
    their own."""
    from mammon.ui.charts import SlicesPieCanvas

    rows = reports.income_category_rows(seeded, START, END)
    total = sum(c for _, c in rows)
    canvas = SlicesPieCanvas("Income by Category", rows)

    assert canvas.has_group()
    drawn = dict(canvas.drawn_slices())
    # The two large categories survive as their own wedges...
    assert drawn["Salary"] == 700_00
    assert drawn["Bonus"] == 200_00
    # ...and the three tiny ones collapse into a single Other wedge.
    assert drawn["Other"] == 40_00 + 35_00 + 25_00
    assert set(drawn) == {"Salary", "Bonus", "Other"}
    # The rollup threshold is honored: Other is at least 10% of the total.
    assert drawn["Other"] >= total // 10
    # Every category folded into Other was itself under 10% of the total.
    grouped = canvas.grouped_members()
    assert all(c < total * 0.10 for _, c in grouped)


def test_clicking_other_drills_into_components(qapp, seeded):
    """Drilling into ``Other`` redraws the pie as its component categories alone,
    and backing out restores the top level."""
    from mammon.ui.charts import SlicesPieCanvas

    rows = reports.income_category_rows(seeded, START, END)
    canvas = SlicesPieCanvas("Income by Category", rows)

    members = dict(canvas.grouped_members())
    assert members == {"Interest": 40_00, "Dividends": 35_00, "Gifts": 25_00}

    assert canvas.drill_into_other() is True
    assert canvas.zoom_path() == ["Other"]
    # At the Other level the pie IS its components; none is under 10% of that
    # smaller total, so there is no nested Other.
    assert dict(canvas.current_slices()) == members
    assert canvas.has_group() is False

    assert canvas.zoom_out() is True
    assert canvas.zoom_path() == []
    assert canvas.has_group() is True     # back at the top, Other is present again

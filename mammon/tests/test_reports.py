"""Tests for mammon.reports.spending: the by-category spending report (SRD 5.9).

Covers the period helper (month/quarter/year), per-category totals with parent
roll-ups and the leaf breakdown, transfer exclusion, the account filter, split
attribution, gross-outflow semantics (income/refunds not netted), the
Uncategorized bucket, and the text formatter -- over a month and a custom range.
"""
from __future__ import annotations

import pytest

from mammon import db, ledger
from mammon.reports import (
    SpendingReport,
    format_itemized_report,
    format_spending_report,
    income_pie,
    itemize_by_category,
    itemize_tree,
    net_worth_series,
    period_range,
    preset_range,
    spending_by_category,
    spending_pie,
)

from datetime import date
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "reports.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    """Two accounts with spending across several categories, an income row, a
    refund, an uncategorized spend, an out-of-range spend, and a transfer."""
    a = ledger.create_account(conn, "Checking", "checking")
    b = ledger.create_account(conn, "Savings", "savings")

    fuel = ledger.resolve_category(conn, "Auto & Transport:Fuel")
    parking = ledger.resolve_category(conn, "Auto & Transport:Parking")
    groceries = ledger.resolve_category(conn, "Groceries")
    dining = ledger.resolve_category(conn, "Dining")
    salary = ledger.resolve_category(conn, "Salary")

    # Account A (checking)
    ledger.add_transaction(conn, a, "2026-01-05", -100_00, category_id=fuel)
    ledger.add_transaction(conn, a, "2026-01-10", -20_00, category_id=parking)
    ledger.add_transaction(conn, a, "2026-01-15", -80_00, category_id=groceries)
    ledger.add_transaction(conn, a, "2026-01-20", 10_00, category_id=groceries)   # refund (+)
    ledger.add_transaction(conn, a, "2026-01-25", -5_00)                          # uncategorized
    ledger.add_transaction(conn, a, "2026-01-28", 2000_00, category_id=salary)    # income (+)
    ledger.add_transaction(conn, a, "2026-02-05", -50_00, category_id=fuel)       # out of Jan

    # Account B (savings)
    ledger.add_transaction(conn, b, "2026-01-18", -30_00, category_id=dining)

    # A -> B transfer inside the window: must be excluded from spending.
    ledger.create_transfer(conn, a, b, "2026-01-22", 200_00)

    return {"a": a, "b": b, "fuel": fuel, "parking": parking,
            "groceries": groceries, "dining": dining, "salary": salary}


def _row(report: SpendingReport, path: str):
    for r in report.flat():
        if r.path == path:
            return r
    return None


# ---------------------------------------------------------------------------
# period helper
# ---------------------------------------------------------------------------
def test_period_range_month():
    assert period_range("month", 2026, month=1) == ("2026-01-01", "2026-01-31")
    assert period_range("month", 2026, month=2) == ("2026-02-01", "2026-02-28")
    assert period_range("month", 2024, month=2) == ("2024-02-01", "2024-02-29")  # leap


def test_period_range_quarter():
    assert period_range("quarter", 2026, quarter=1) == ("2026-01-01", "2026-03-31")
    assert period_range("quarter", 2026, quarter=4) == ("2026-10-01", "2026-12-31")


def test_period_range_year():
    assert period_range("year", 2026) == ("2026-01-01", "2026-12-31")


def test_period_range_validates():
    with pytest.raises(ValueError):
        period_range("month", 2026)               # month missing
    with pytest.raises(ValueError):
        period_range("quarter", 2026, quarter=5)  # out of range
    with pytest.raises(ValueError):
        period_range("decade", 2026)              # unknown period


# ---- UI period presets (This/Last Month, This/Last Year, YTD) --------------
def test_preset_range_this_month():
    assert preset_range("this_month", date(2026, 8, 24)) == ("2026-08-01", "2026-08-31")
    # a leap February resolves its own last day
    assert preset_range("this_month", date(2024, 2, 10)) == ("2024-02-01", "2024-02-29")


def test_preset_range_last_month():
    assert preset_range("last_month", date(2026, 8, 24)) == ("2026-07-01", "2026-07-31")
    # January's "last month" is the previous December (crosses the year)
    assert preset_range("last_month", date(2026, 1, 15)) == ("2025-12-01", "2025-12-31")


def test_preset_range_this_and_last_year():
    assert preset_range("this_year", date(2026, 8, 24)) == ("2026-01-01", "2026-12-31")
    assert preset_range("last_year", date(2026, 8, 24)) == ("2025-01-01", "2025-12-31")


def test_preset_range_ytd():
    # Year-to-Date: Jan 1 of today's year through today, inclusive.
    assert preset_range("ytd", date(2026, 8, 24)) == ("2026-01-01", "2026-08-24")
    # on Jan 1 the range collapses to that single day
    assert preset_range("ytd", date(2026, 1, 1)) == ("2026-01-01", "2026-01-01")


def test_preset_range_rolling_windows():
    # The unified report dropdown's rolling presets (§5.9b), all ending today.
    assert preset_range("last_7_days", date(2026, 8, 24)) == ("2026-08-18", "2026-08-24")
    assert preset_range("last_30_days", date(2026, 8, 24)) == ("2026-07-26", "2026-08-24")
    # "Last 12 months": one year ago (same day) through today.
    assert preset_range("last_12_months", date(2026, 8, 24)) == ("2025-08-24", "2026-08-24")
    # Feb 29 steps back to Feb 28 of the prior year rather than raising.
    assert preset_range("last_12_months", date(2024, 2, 29)) == ("2023-02-28", "2024-02-29")


def test_preset_range_quarters():
    # This quarter: the calendar quarter the date falls in.
    assert preset_range("this_quarter", date(2026, 8, 24)) == ("2026-07-01", "2026-09-30")
    # Last quarter: Q3 -> Q2.
    assert preset_range("last_quarter", date(2026, 8, 24)) == ("2026-04-01", "2026-06-30")
    # Q1's "last quarter" is the previous year's Q4 (crosses the year).
    assert preset_range("last_quarter", date(2026, 2, 10)) == ("2025-10-01", "2025-12-31")


def test_preset_range_validates():
    # "custom" is not a computable preset (it opens the customize dialog).
    with pytest.raises(ValueError):
        preset_range("custom", date(2026, 8, 24))
    # "earliest" needs the ledger's own bounds -> resolved in the UI, not here.
    with pytest.raises(ValueError):
        preset_range("earliest", date(2026, 8, 24))
    with pytest.raises(ValueError):
        preset_range("decade", date(2026, 8, 24))


# ---------------------------------------------------------------------------
# a month
# ---------------------------------------------------------------------------
def test_month_totals_with_rollup(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    rep = spending_by_category(conn, start, end)

    # Grand total: Auto 120 + Groceries 80 + Dining 30 + Uncategorized 5 = 235.
    assert rep.total_cents == 235_00

    auto = _row(rep, "Auto & Transport")
    assert auto is not None
    assert auto.total_cents == 120_00      # rolled up from its children
    assert auto.own_cents == 0             # nothing booked directly on the parent
    # leaf breakdown exposed as children, sorted by magnitude desc
    assert [(c.name, c.total_cents) for c in auto.children] == [
        ("Fuel", 100_00), ("Parking", 20_00)]

    assert _row(rep, "Groceries").total_cents == 80_00   # +10 refund NOT netted
    assert _row(rep, "Dining").total_cents == 30_00


def test_month_excludes_transfers_and_income(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    rep = spending_by_category(conn, start, end)

    # The transfer has no category; if it were counted it would land in
    # Uncategorized. Uncategorized == just the single -5.00 proves exclusion.
    unc = _row(rep, "Uncategorized")
    assert unc is not None
    assert unc.total_cents == 5_00

    # Income category never appears in a spending report.
    assert _row(rep, "Salary") is None


def test_top_level_rows_sorted_by_magnitude(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    rep = spending_by_category(conn, start, end)
    assert [r.path for r in rep.rows] == [
        "Auto & Transport", "Groceries", "Dining", "Uncategorized"]


# ---------------------------------------------------------------------------
# account filter
# ---------------------------------------------------------------------------
def test_account_filter(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    # Only the checking account: the savings-only Dining -30 drops out.
    rep = spending_by_category(conn, start, end, account_ids=[seeded["a"]])
    assert rep.total_cents == 205_00
    assert _row(rep, "Dining") is None
    assert _row(rep, "Auto & Transport").total_cents == 120_00

    # Only savings: just the Dining spend.
    rep_b = spending_by_category(conn, start, end, account_ids=[seeded["b"]])
    assert rep_b.total_cents == 30_00
    assert [r.path for r in rep_b.rows] == ["Dining"]

    # Empty selection -> empty report (logical, if rarely useful).
    rep_none = spending_by_category(conn, start, end, account_ids=[])
    assert rep_none.total_cents == 0
    assert rep_none.rows == []


# ---------------------------------------------------------------------------
# a custom range
# ---------------------------------------------------------------------------
def test_custom_range_spans_two_months(conn, seeded):
    # Jan 1 - Feb 28 pulls in the Feb 5 fuel (-50) on top of January.
    rep = spending_by_category(conn, "2026-01-01", "2026-02-28")
    assert rep.total_cents == 285_00                       # 235 + 50
    auto = _row(rep, "Auto & Transport")
    assert auto.total_cents == 170_00                      # Fuel 150 + Parking 20
    assert _row(rep, "Auto & Transport:Fuel").total_cents == 150_00


def test_custom_range_narrower_than_a_month(conn, seeded):
    # A tight window catching only the two mid-January rows.
    rep = spending_by_category(conn, "2026-01-14", "2026-01-19")
    assert rep.total_cents == 110_00                       # Groceries 80 + Dining 30
    assert {r.path for r in rep.rows} == {"Groceries", "Dining"}


# ---------------------------------------------------------------------------
# splits
# ---------------------------------------------------------------------------
def test_split_transaction_attributes_to_split_categories(conn, seeded):
    # A single -50 purchase split across two categories; the parent's own
    # amount/category must NOT also be counted (no double counting).
    txn = ledger.add_transaction(conn, seeded["a"], "2026-03-05", -50_00,
                                 category_id=seeded["groceries"])
    conn.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?,?,?)",
                 (txn, seeded["groceries"], -30_00))
    conn.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?,?,?)",
                 (txn, seeded["dining"], -20_00))
    conn.commit()

    rep = spending_by_category(conn, "2026-03-01", "2026-03-31")
    assert rep.total_cents == 50_00                        # not 100 (no double count)
    assert _row(rep, "Groceries").total_cents == 30_00
    assert _row(rep, "Dining").total_cents == 20_00


def test_zero_amount_reclassification_attributes_expense_side(conn, seeded):
    # the user's pattern (2026-08-12): a ZERO-amount transaction moves money between
    # CATEGORIES -- e.g. mortgage escrow -> property tax when the lender pays the
    # tax bill from escrow. It nets to $0 on the account, but its split lines
    # reclassify. The spending report must NOT be fooled by the $0 amount (it reads
    # the split lines) and must attribute the EXPENSE (negative) split to its
    # category. The positive "credit back to escrow" split is treated like any
    # other positive (income/refund) and is NOT netted against spending: on real
    # data most zero-amount positive splits are income- or transfer-side (Bonus,
    # 401K Match, a loan payoff), so a blanket net would drive many categories
    # nonsensically negative. Money-out stays a pure sum of money that went out.
    escrow = ledger.resolve_category(conn, "Housing:Escrow")          # noqa: F841
    ledger.resolve_category(conn, "Housing:Property Tax")
    txn = ledger.add_transaction(conn, seeded["a"], "2026-04-10", 0,
                                 payee="Escrow disbursement -> property tax")
    conn.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?,?,?)",
                 (txn, escrow, 1000_00))        # +: reverse the escrow contribution
    conn.execute("INSERT INTO splits(transaction_id, category_id, amount) VALUES (?,?,?)",
                 (txn, ledger.resolve_category(conn, "Housing:Property Tax"), -1000_00))
    conn.commit()

    rep = spending_by_category(conn, "2026-04-01", "2026-04-30")
    # the property-tax EXPENSE is captured despite the transaction's $0 amount
    assert _row(rep, "Housing:Property Tax").total_cents == 1000_00
    # the positive escrow credit is NOT counted as spending (no negative rows)
    assert _row(rep, "Housing:Escrow") is None
    assert rep.total_cents == 1000_00
    assert all(r.total_cents >= 0 for r in rep.flat())


# ---------------------------------------------------------------------------
# text formatter
# ---------------------------------------------------------------------------
def test_format_spending_report(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    rep = spending_by_category(conn, start, end)
    text = format_spending_report(rep)

    assert "Spending by Category  2026-01-01 to 2026-01-31" in text
    # parent line, then its indented children
    assert "Auto & Transport" in text
    assert "  Fuel" in text
    assert "  Parking" in text
    # dollar formatting from integer cents, and a grand total line
    assert "100.00" in text
    assert "235.00" in text
    lines = text.splitlines()
    assert lines[-1].startswith("Total")
    assert lines[-1].rstrip().endswith("235.00")


def test_format_empty_report(conn):
    rep = spending_by_category(conn, "2030-01-01", "2030-01-31")
    text = format_spending_report(rep)
    assert "(no spending in this period)" in text
    assert rep.total_cents == 0


def test_invalid_dates_rejected(conn):
    with pytest.raises(ValueError):
        spending_by_category(conn, "01/01/2026", "2026-01-31")


# ---------------------------------------------------------------------------
# chart data: spending pie
# ---------------------------------------------------------------------------
def test_spending_pie_top_level_slices(conn, seeded):
    # Jan 2026: Auto & Transport 120 (fuel 100 + parking 20), Groceries 80,
    # Dining 30, Uncategorized 5. Salary/refund (income) not counted; transfer
    # excluded. Grand total 235.00.
    pie = spending_pie(conn, "2026-01-01", "2026-01-31")
    assert pie.total_cents == 235_00
    labels = [s.label for s in pie.slices]
    assert labels == ["Auto & Transport", "Groceries", "Dining", "Uncategorized"]
    # largest first; slice cents sum to the grand total; fractions sum to ~1.
    assert [s.cents for s in pie.slices] == [120_00, 80_00, 30_00, 5_00]
    assert sum(s.cents for s in pie.slices) == pie.total_cents
    assert abs(sum(s.fraction for s in pie.slices) - 1.0) < 1e-9
    assert abs(pie.slices[0].fraction - 120_00 / 235_00) < 1e-9


def test_spending_pie_collapses_tail_into_other(conn, seeded):
    # max_slices=2 keeps the single biggest and folds the rest into "Other".
    pie = spending_pie(conn, "2026-01-01", "2026-01-31", max_slices=2)
    assert [s.label for s in pie.slices] == ["Auto & Transport", "Other"]
    assert [s.cents for s in pie.slices] == [120_00, 115_00]
    assert sum(s.cents for s in pie.slices) == pie.total_cents


def test_spending_pie_empty_period(conn, seeded):
    pie = spending_pie(conn, "2030-01-01", "2030-01-31")
    assert pie.is_empty() and pie.total_cents == 0 and pie.slices == []


def test_spending_pie_rejects_bad_max_slices(conn, seeded):
    with pytest.raises(ValueError):
        spending_pie(conn, "2026-01-01", "2026-01-31", max_slices=1)


# ---------------------------------------------------------------------------
# chart data: net worth over time
# ---------------------------------------------------------------------------
def test_net_worth_series_samples_range(conn, seeded):
    # seeded txns span 2026-01-05 .. 2026-02-05.
    series = net_worth_series(conn, points=5)
    assert series.start == "2026-01-05" and series.end == "2026-02-05"
    assert 2 <= len(series.points) <= 5
    dates = [p.date for p in series.points]
    assert dates == sorted(dates)                    # ascending, de-duplicated
    assert len(dates) == len(set(dates))
    assert dates[0] == "2026-01-05" and dates[-1] == "2026-02-05"
    # the final sample equals net worth over ALL transactions.
    assert series.points[-1].cents == ledger.net_worth(conn)


def test_net_worth_series_empty_db(conn):
    assert net_worth_series(conn).is_empty()


def test_net_worth_series_rejects_bad_points(conn, seeded):
    with pytest.raises(ValueError):
        net_worth_series(conn, points=1)


# ---------------------------------------------------------------------------
# Itemize by Category: ALL categories, REAL signed amounts, income-first
# ---------------------------------------------------------------------------
def test_itemize_signed_rows_and_ordering(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    rep = itemize_by_category(conn, start, end)

    # sections are contiguous and ordered: income, then expense, then transfer.
    types = [r.type for r in rep.rows]
    assert types[0] == "income"
    rank = {"income": 0, "expense": 1, "transfer": 2}
    ranks = [rank[t] for t in types]
    assert ranks == sorted(ranks)
    assert "transfer" in types

    by_path = {r.path: r for r in rep.rows}
    # income row: the REAL positive net, no sign stripping.
    assert by_path["Salary"].net_cents == 2000_00
    assert by_path["Salary"].type == "income"
    # expense rows carry their REAL negative amounts (signed).
    assert by_path["Auto & Transport:Fuel"].net_cents == -100_00
    assert by_path["Auto & Transport:Parking"].net_cents == -20_00
    assert by_path["Dining"].net_cents == -30_00
    # the +10 refund IS netted here (signed report), unlike the spending report.
    assert by_path["Groceries"].net_cents == -70_00
    # uncategorized (-5) is not a category -> still excluded.
    assert "Uncategorized" not in by_path
    # the A->B $200 transfer now appears as bracketed transfer rows, one per
    # counterparty: money moved TO Savings reads negative; Savings' own receiving
    # leg reads positive under [Checking].
    assert by_path["[Savings]"].type == "transfer"
    assert by_path["[Savings]"].net_cents == -200_00
    assert by_path["[Checking]"].type == "transfer"
    assert by_path["[Checking]"].net_cents == 200_00


def test_itemize_signed_total(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    rep = itemize_by_category(conn, start, end)
    # +2000 (Salary) -100 -20 (Auto) -30 (Dining) -70 (Groceries) = +1780.
    assert rep.total_cents == 1780_00
    # the total sums the SIGNED values -- no magnitude/sign stripping.
    assert rep.total_cents == sum(r.net_cents for r in rep.rows)


def test_itemize_skips_zero_activity_and_respects_accounts(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    # Savings (account b) has the Dining -30 spend plus the RECEIVING leg of the
    # A->B transfer (grouped under its counterparty, [Checking], +200).
    rep = itemize_by_category(conn, start, end, account_ids=[seeded["b"]])
    assert [(r.path, r.net_cents) for r in rep.rows] == [
        ("Dining", -30_00), ("[Checking]", 200_00)]
    assert rep.total_cents == 170_00


def test_itemize_honors_category_subset(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    full = itemize_by_category(conn, start, end)

    # None == include every category (identical to the unfiltered report).
    same = itemize_by_category(conn, start, end, category_ids=None)
    assert [r.path for r in same.rows] == [r.path for r in full.rows]

    # A single-category subset keeps only that income/expense row; the transfer
    # section (not a category) is unaffected, and the total re-sums the rows.
    only = itemize_by_category(conn, start, end,
                               category_ids=[seeded["groceries"]])
    cat_rows = [(r.path, r.net_cents) for r in only.rows if r.type != "transfer"]
    assert cat_rows == [("Groceries", -70_00)]
    xfer = {r.path: r.net_cents for r in only.rows if r.type == "transfer"}
    assert xfer == {"[Savings]": -200_00, "[Checking]": 200_00}
    assert only.total_cents == sum(r.net_cents for r in only.rows)

    # A parent's two sub-categories are selected independently by id.
    both = itemize_by_category(conn, start, end,
                               category_ids=[seeded["fuel"], seeded["parking"]])
    assert {r.path for r in both.rows if r.type != "transfer"} == {
        "Auto & Transport:Fuel", "Auto & Transport:Parking"}

    # An explicit empty subset drops every category row; transfers remain.
    none_cats = itemize_by_category(conn, start, end, category_ids=[])
    assert none_cats.rows and all(r.type == "transfer" for r in none_cats.rows)


def test_format_itemized_signed(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    rep = itemize_by_category(conn, start, end)
    text = format_itemized_report(rep)
    assert "Itemize by Category  2026-01-01 to 2026-01-31" in text
    # negative amounts carry a leading minus; positives do not.
    assert "-100.00" in text
    assert "2,000.00" in text
    lines = text.splitlines()
    assert lines[-1].startswith("Total")
    assert lines[-1].rstrip().endswith("1,780.00")   # signed total


# ---- Itemize: transfer accounts as their own signed line items -------------
def test_itemize_transfer_rows_aggregate_and_no_double_count(conn, seeded):
    a, b = seeded["a"], seeded["b"]
    # a second A->B transfer in January; both should aggregate onto [Savings].
    ledger.create_transfer(conn, a, b, "2026-01-24", 50_00)
    start, end = period_range("month", 2026, month=1)
    rep = itemize_by_category(conn, start, end)
    by_path = {r.path: r for r in rep.rows}

    # [Savings] carries the SIGNED sum of the legs whose counterparty is Savings:
    # the two A-legs (-200 and -50) = -250. Their mirrors land on [Checking].
    assert by_path["[Savings]"].type == "transfer"
    assert by_path["[Savings]"].net_cents == -250_00
    assert by_path["[Checking]"].net_cents == 250_00

    # the row equals the raw sum of transfer legs pointing at Savings in range.
    legs = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE transfer_account_id=? AND date BETWEEN ? AND ?",
        (b, start, end)).fetchone()["s"]
    assert by_path["[Savings]"].net_cents == legs

    # no double counting: the legs net to zero across all accounts, so the grand
    # total is unchanged from the pure income/expense figure (+1780).
    assert sum(r.net_cents for r in rep.rows if r.type == "transfer") == 0
    assert rep.total_cents == 1780_00


def test_itemize_transfer_respects_period(conn, seeded):
    a, b = seeded["a"], seeded["b"]
    # a transfer OUTSIDE January must not leak into the January report.
    ledger.create_transfer(conn, a, b, "2026-02-10", 999_00)
    jstart, jend = period_range("month", 2026, month=1)
    jrep = itemize_by_category(conn, jstart, jend)
    assert {r.path: r for r in jrep.rows}["[Savings]"].net_cents == -200_00
    # the February window sees the out-of-range transfer instead.
    fstart, fend = period_range("month", 2026, month=2)
    frep = itemize_by_category(conn, fstart, fend)
    assert {r.path: r for r in frep.rows}["[Savings]"].net_cents == -999_00


def test_itemize_split_transfer_leg_counted_once(conn, seeded):
    a, b = seeded["a"], seeded["b"]
    groc = seeded["groceries"]
    # a $100 spend in A split into a $60 grocery line and a $40 transfer to B.
    tid = ledger.add_transaction(conn, a, "2026-01-26", -100_00)
    ledger.set_splits(conn, tid, [
        {"category_id": groc, "amount": -60_00},
        {"transfer_account_id": b, "amount": -40_00},
    ])
    start, end = period_range("month", 2026, month=1)
    rep = itemize_by_category(conn, start, end)
    by_path = {r.path: r for r in rep.rows}

    # [Savings] sums the $200 whole transfer AND the $40 split leg = -240; its
    # mirror (+40) joins the whole transfer's +200 on [Checking] = +240.
    assert by_path["[Savings]"].net_cents == -240_00
    assert by_path["[Checking]"].net_cents == 240_00
    # split source leg + its mirror counted once each -> transfers net to zero.
    assert sum(r.net_cents for r in rep.rows if r.type == "transfer") == 0
    # the $60 grocery split line is an expense, not a transfer: -70 seed + -60.
    assert by_path["Groceries"].type == "expense"
    assert by_path["Groceries"].net_cents == -130_00


# ---------------------------------------------------------------------------
# Itemize by Category: the hierarchical, expandable TREE (Quicken register view)
# ---------------------------------------------------------------------------
def _node(nodes, label):
    for n in nodes:
        if n.label == label:
            return n
    return None


def test_itemize_tree_sections_and_classification(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    tree = itemize_tree(conn, start, end)
    # INCOME first, then EXPENSES, then TRANSFERS; each section rolls up its tops.
    assert [s.label for s in tree.sections] == ["INCOME", "EXPENSES", "TRANSFERS"]
    inc, exp, xfer = tree.sections
    assert inc.net_cents == 2000_00                    # Salary
    assert exp.net_cents == -220_00                    # Auto -120, Dining -30, Groc -70
    assert xfer.net_cents == 0                         # +200/-200 net to zero
    # a top-level is placed by the SIGN of its rolled-up net (Salary income).
    assert [n.label for n in inc.children] == ["Salary"]
    assert [n.label for n in exp.children] == \
        ["Auto & Transport", "Dining", "Groceries"]   # name-sorted within a section


def test_itemize_tree_rollup_and_subcategories(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    tree = itemize_tree(conn, start, end)
    auto = _node(_node(tree.sections, "EXPENSES").children, "Auto & Transport")
    # a parent with sub-categories: its amount is the roll-up of its children.
    assert auto.net_cents == -120_00
    assert [(n.label, n.net_cents) for n in auto.children] == [
        ("Fuel", -100_00), ("Parking", -20_00)]
    # a sub-category that is itself a leaf expands straight to its transactions.
    fuel = _node(auto.children, "Fuel")
    assert [n.kind for n in fuel.children] == ["txn"]
    assert fuel.children[0].line.amount_cents == -100_00


def test_itemize_tree_leaf_expands_to_transactions(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    tree = itemize_tree(conn, start, end)
    groc = _node(_node(tree.sections, "EXPENSES").children, "Groceries")
    # a leaf category rolls up its own txns (spend + refund) and lists them.
    assert groc.net_cents == -70_00
    assert all(ch.kind == "txn" for ch in groc.children)
    assert sorted(ch.line.amount_cents for ch in groc.children) == [-80_00, 10_00]


def test_itemize_tree_other_node_for_parent_direct_postings(conn, seeded):
    # A direct posting on the Auto & Transport PARENT must show under an
    # "Other Auto & Transport" child, ordered AFTER the real sub-categories.
    auto_parent = ledger.resolve_category(conn, "Auto & Transport")
    ledger.add_transaction(conn, seeded["a"], "2026-01-11", -15_00,
                           category_id=auto_parent)
    start, end = period_range("month", 2026, month=1)
    tree = itemize_tree(conn, start, end)
    auto = _node(_node(tree.sections, "EXPENSES").children, "Auto & Transport")
    assert auto.net_cents == -135_00                   # -100 -20 -15
    assert [n.label for n in auto.children] == [
        "Fuel", "Parking", "Other Auto & Transport"]
    other = auto.children[-1]
    assert other.kind == "other" and other.net_cents == -15_00
    assert [ch.line.amount_cents for ch in other.children] == [-15_00]


def test_itemize_tree_keeps_zero_net_category_with_activity(conn, seeded):
    # Two offsetting postings net zero but the category still appears (unlike the
    # flat report, which drops net-zero categories) -- matching Quicken.
    hobby = ledger.resolve_category(conn, "Hobby")
    ledger.add_transaction(conn, seeded["a"], "2026-01-06", 25_00, category_id=hobby)
    ledger.add_transaction(conn, seeded["a"], "2026-01-07", -25_00, category_id=hobby)
    start, end = period_range("month", 2026, month=1)
    tree = itemize_tree(conn, start, end)
    hob = _node(_node(tree.sections, "EXPENSES").children, "Hobby")
    assert hob is not None and hob.net_cents == 0
    assert len(hob.children) == 2


def test_itemize_tree_account_and_category_filters(conn, seeded):
    start, end = period_range("month", 2026, month=1)
    # Account filter: Savings holds only the Dining -30 spend (Salary is on
    # Checking, so INCOME disappears).
    only_b = itemize_tree(conn, start, end, account_ids=[seeded["b"]])
    assert [n.label for n in _node(only_b.sections, "EXPENSES").children] == ["Dining"]
    assert _node(only_b.sections, "INCOME") is None
    # Top-level name filter narrows which categories show; transfers are a
    # non-category section and stay regardless.
    only_groc = itemize_tree(conn, start, end, top_level_names={"Groceries"})
    assert [n.label for n in _node(only_groc.sections, "EXPENSES").children] == \
        ["Groceries"]
    assert _node(only_groc.sections, "INCOME") is None
    assert _node(only_groc.sections, "TRANSFERS") is not None


def test_itemize_tree_rollups_are_internally_consistent(conn, seeded):
    # Every grouping node's net equals the sum of its children's nets, all the
    # way down to the transaction leaves.
    start, end = period_range("month", 2026, month=1)
    tree = itemize_tree(conn, start, end)

    def check(node):
        if node.kind == "txn":
            return node.line.amount_cents
        total = sum(check(ch) for ch in node.children)
        assert node.net_cents == total, (node.label, node.net_cents, total)
        return node.net_cents

    assert sum(check(s) for s in tree.sections) == tree.total_cents


# ---------------------------------------------------------------------------
# Pies: expense-only spending pie + a separate income pie
# ---------------------------------------------------------------------------
def test_spending_pie_excludes_income_categories(conn, seeded):
    # A negative correction booked on the INCOME 'Salary' category. It is
    # money-out by amount, but Salary is income, so it must NOT appear in the
    # spending (expense) pie -- only expense categories do.
    ledger.add_transaction(conn, seeded["a"], "2026-01-27", -50_00,
                           category_id=seeded["salary"])
    pie = spending_pie(conn, "2026-01-01", "2026-01-31")
    labels = [s.label for s in pie.slices]
    assert "Salary" not in labels
    assert set(labels) == {"Auto & Transport", "Groceries", "Dining",
                           "Uncategorized"}
    # expense magnitudes are the money-out totals (Groceries refund NOT netted).
    cents = dict(zip(labels, (s.cents for s in pie.slices)))
    assert cents["Groceries"] == 80_00
    assert pie.total_cents == 235_00


def test_income_pie_contents(conn, seeded):
    pie = income_pie(conn, "2026-01-01", "2026-01-31")
    labels = [s.label for s in pie.slices]
    # only income-type categories: Salary. The Groceries +10 refund is on an
    # EXPENSE category, so it is not income and does not appear.
    assert labels == ["Salary"]
    assert "Groceries" not in labels
    assert pie.total_cents == 2000_00
    assert pie.slices[0].cents == 2000_00
    assert abs(pie.slices[0].fraction - 1.0) < 1e-9


def test_income_pie_empty_when_no_income(conn):
    a = ledger.create_account(conn, "Checking", "checking")
    groceries = ledger.resolve_category(conn, "Groceries")
    ledger.add_transaction(conn, a, "2026-05-01", -40_00, category_id=groceries)
    pie = income_pie(conn, "2026-05-01", "2026-05-31")
    assert pie.is_empty() and pie.total_cents == 0 and pie.slices == []


def test_income_pie_rejects_bad_max_slices(conn, seeded):
    with pytest.raises(ValueError):
        income_pie(conn, "2026-01-01", "2026-01-31", max_slices=1)


# ---------------------------------------------------------------------------
# Net worth: hidden accounts are decluttered, not deleted
# ---------------------------------------------------------------------------
def test_hiding_an_account_excludes_it_from_net_worth(conn):
    """Hiding is how a user says "the records here are incomplete, leave it out",
    so it EXCLUDES by default. The fuller picture is opt-in: a report asks for it
    explicitly, and nothing puts a hidden balance back into a total unasked."""
    from mammon import investments

    a = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    plan = ledger.create_account(conn, "Old 401k", "checking", opening_balance=0)
    ledger.add_transaction(conn, a, "2020-01-01", 100_00, payee="Pay")
    ledger.add_transaction(conn, plan, "2020-01-01", 900_00, payee="Contribution")

    assert ledger.net_worth(conn) == 1000_00
    conn.execute("UPDATE accounts SET hidden=1 WHERE id=?", (plan,))
    conn.commit()
    assert ledger.net_worth(conn) == 100_00                # the default
    assert ledger.net_worth(conn, include_hidden=True) == 1000_00
    assert investments.net_worth(conn, include_hidden=True) == 1000_00


def test_net_worth_series_subsets_by_account_and_hidden(conn):
    from mammon import reports

    a = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    plan = ledger.create_account(conn, "Old 401k", "checking", opening_balance=0)
    ledger.add_transaction(conn, a, "2020-01-01", 100_00, payee="Pay")
    ledger.add_transaction(conn, plan, "2020-01-01", 900_00, payee="Contribution")
    conn.execute("UPDATE accounts SET hidden=1 WHERE id=?", (plan,))
    conn.commit()

    # Default matches the account bar, so a chart never disagrees with the
    # sidebar figure the user just looked at.
    default = reports.net_worth_series(conn, "2020-01-01", "2020-12-31", points=2)
    assert [p.cents for p in default.points] == [100_00, 100_00]

    # ...and a growth curve can opt into the hidden history.
    with_hidden = reports.net_worth_series(conn, "2020-01-01", "2020-12-31",
                                           points=2, include_hidden=True)
    assert [p.cents for p in with_hidden.points] == [1000_00, 1000_00]

    just_plan = reports.net_worth_series(conn, "2020-01-01", "2020-12-31",
                                         points=2, account_ids=[plan],
                                         include_hidden=True)
    assert [p.cents for p in just_plan.points] == [900_00, 900_00]


def test_net_worth_series_excludes_a_category_as_a_what_if(conn):
    """Unticking a category draws a counterfactual: net worth as if that
    spending had never happened. The effect compounds forward -- every later
    sample is lifted by everything the category ever cost."""
    from mammon import reports

    a = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    vac = ledger.resolve_category(conn, "Vacation")
    air = ledger.resolve_category(conn, "Vacation:Airfare")
    food = ledger.resolve_category(conn, "Groceries")
    ledger.add_transaction(conn, a, "2020-01-01", 10_000_00, payee="Pay")
    ledger.add_transaction(conn, a, "2020-02-01", -1_000_00, payee="Hotel",
                           category_id=vac)
    ledger.add_transaction(conn, a, "2020-03-01", -500_00, payee="Flight",
                           category_id=air)
    ledger.add_transaction(conn, a, "2020-04-01", -200_00, payee="Store",
                           category_id=food)

    real = reports.net_worth_series(conn, "2020-01-01", "2020-12-31", points=2)
    assert real.points[-1].cents == 8_300_00

    # A top-level name reaches its CHILDREN: money posts to leaves, so Vacation
    # has to pick up Vacation:Airfare or excluding it would barely move.
    whatif = reports.net_worth_series(conn, "2020-01-01", "2020-12-31", points=2,
                                      exclude_categories=["Vacation"])
    assert whatif.points[-1].cents == 8_300_00 + 1_500_00

    # and the earliest sample, before any of it was spent, is unchanged
    assert real.points[0].cents == whatif.points[0].cents


def test_net_worth_what_if_counts_split_lines_and_skips_transfers(conn):
    """A split's parent row carries a NULL category, so missing split lines
    would leave half of a categorized purchase in the total. Transfers have no
    category at all and must never register as spending."""
    from mammon import reports

    a = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    b = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    vac = ledger.resolve_category(conn, "Vacation")
    food = ledger.resolve_category(conn, "Groceries")
    ledger.add_transaction(conn, a, "2020-01-01", 10_000_00, payee="Pay")
    big = ledger.add_transaction(conn, a, "2020-02-01", -400_00, payee="Big Box")
    ledger.set_splits(conn, big, [(vac, -300_00, "luggage"),
                                  (food, -100_00, "snacks")])
    ledger.create_transfer(conn, a, b, "2020-03-01", 1_000_00, payee="Sweep")

    base = reports.net_worth_series(conn, "2020-01-01", "2020-12-31", points=2)
    assert base.points[-1].cents == 9_600_00        # the transfer nets to zero

    whatif = reports.net_worth_series(conn, "2020-01-01", "2020-12-31", points=2,
                                      exclude_categories=["Vacation"])
    assert whatif.points[-1].cents == 9_600_00 + 300_00

"""mammon.reports.flows: income vs. expense by bucket, cash flow with external
transfers, period comparison, and per-bucket averages (roadmap item 4).

The fixture is small enough to add up by hand; every expectation below is that
sum. Signs follow the ledger (negative = out); a refund shrinks its expense
category rather than counting as income; transfers are excluded except in
the cash-flow transfers section, where a transfer to an account OUTSIDE the
selected set is money that really left.
"""
from __future__ import annotations

import pytest

from mammon import db, ledger
from mammon.reports import flows


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "flows.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    hid = ledger.create_account(conn, "Old Card", "credit", opening_balance=0)
    ledger.set_account_hidden(conn, hid, True)
    cat = {n: ledger.resolve_category(conn, n) for n in
           ("Salary", "Housing:Rent", "Groceries", "Household", "Auto:Fuel", "Fees")}
    for month in ("01", "02", "03"):
        ledger.add_transaction(conn, chk, f"2026-{month}-01", 3000_00, payee="Employer",
                               category_id=cat["Salary"])
        ledger.add_transaction(conn, chk, f"2026-{month}-02", -1000_00, payee="Landlord",
                               category_id=cat["Housing:Rent"], tag="home")
    ledger.add_transaction(conn, chk, "2026-01-10", -200_00, payee="Grocer",
                           category_id=cat["Groceries"])
    ledger.add_transaction(conn, chk, "2026-01-12", -50_00, payee="Gas Co",
                           category_id=cat["Auto:Fuel"])
    ledger.create_transfer(conn, chk, sav, "2026-01-15", 500_00, payee="Stash")
    split = ledger.add_transaction(conn, chk, "2026-02-10", -180_00, payee="Grocer")
    ledger.set_splits(conn, split, [(cat["Groceries"], -150_00, ""),
                                    (cat["Household"], -30_00, "")])
    ledger.add_transaction(conn, chk, "2026-02-12", 20_00, payee="Grocer",
                           category_id=cat["Groceries"])          # refund
    ledger.add_transaction(conn, sav, "2026-02-20", -5_00, payee="Bank",
                           category_id=cat["Fees"])
    ledger.add_transaction(conn, chk, "2026-03-15", -40_00, payee="Mystery")   # uncategorized
    sched = ledger.add_transaction(conn, chk, "2026-03-28", -1000_00, payee="Landlord",
                                   category_id=cat["Housing:Rent"])
    conn.execute("UPDATE transactions SET scheduled=1 WHERE id=?", (sched,))
    ledger.add_transaction(conn, hid, "2026-02-05", -999_00, payee="Grocer",
                           category_id=cat["Groceries"])          # hidden account
    conn.commit()
    return {"chk": chk, "sav": sav, "hid": hid, **cat}


def _by_path(rows):
    return {r.path: r for r in rows}


# ---------------------------------------------------------------------------
# buckets
# ---------------------------------------------------------------------------
def test_bucket_helpers():
    assert flows.buckets_in("2026-01-15", "2026-03-02", "month") == ["2026-01", "2026-02", "2026-03"]
    assert flows.buckets_in("2026-01-15", "2026-03-02", "quarter") == ["2026-Q1"]
    assert flows.buckets_in("2025-11-01", "2026-02-01", "quarter") == ["2025-Q4", "2026-Q1"]
    assert flows.buckets_in("2025-11-01", "2026-02-01", "year") == ["2025", "2026"]
    assert flows.buckets_in("2026-03-02", "2026-01-15", "month") == ["2026-01", "2026-02", "2026-03"]
    assert flows.buckets_in("2026-01-01", "2026-12-31", "total") == ["total"]
    assert flows.bucket_of("2026-05-17", "quarter") == "2026-Q2"
    assert flows.bucket_end("2026-02", "month", "2026-03-02") == "2026-02-28"
    assert flows.bucket_end("2026-03", "month", "2026-03-02") == "2026-03-02"
    assert flows.bucket_end("2026-Q1", "quarter", "2026-12-31") == "2026-03-31"
    assert flows.bucket_end("2026", "year", "2026-12-31") == "2026-12-31"
    with pytest.raises(ValueError):
        flows.buckets_in("2026-01-01", "2026-12-31", "week")


# ---------------------------------------------------------------------------
# income vs. expense
# ---------------------------------------------------------------------------
def test_income_expense_by_month_adds_up(conn, seeded):
    rep = flows.income_expense(conn, "2026-01-01", "2026-03-31", bucket="month")
    assert rep.buckets == ["2026-01", "2026-02", "2026-03"]
    assert rep.account_ids == [seeded["chk"], seeded["sav"]]        # hidden left out
    inc = _by_path(rep.income)
    exp = _by_path(rep.expense)
    assert list(inc) == ["Salary"]
    assert inc["Salary"].by_bucket == {"2026-01": 3000_00, "2026-02": 3000_00, "2026-03": 3000_00}
    assert inc["Salary"].total == 9000_00 and inc["Salary"].top == "Salary"
    assert [r.path for r in rep.expense] == [
        "Housing:Rent", "Groceries", "Auto:Fuel", "Uncategorized", "Household", "Fees"]
    assert exp["Groceries"].by_bucket == {"2026-01": -200_00, "2026-02": -130_00, "2026-03": 0}
    assert exp["Housing:Rent"].top == "Housing" and exp["Housing:Rent"].total == -3000_00
    assert exp["Fees"].total == -5_00                                 # savings counts
    assert exp["Uncategorized"].category_id is None and exp["Uncategorized"].total == -40_00
    assert rep.total_income == 9000_00
    assert rep.total_expense == -3455_00
    assert rep.net == 5545_00
    assert rep.net_by_bucket == {"2026-01": 1750_00, "2026-02": 1835_00, "2026-03": 1960_00}
    # The scheduled placeholder (a March rent pre-entry) is not counted...
    assert exp["Housing:Rent"].by_bucket["2026-03"] == -1000_00
    # ...unless asked for, and the hidden account joins only when asked for.
    rep2 = flows.income_expense(conn, "2026-01-01", "2026-03-31", bucket="total",
                                include_scheduled=True, include_hidden=True)
    assert _by_path(rep2.expense)["Housing:Rent"].total == -4000_00
    assert _by_path(rep2.expense)["Groceries"].total == -330_00 - 999_00


def test_income_expense_account_subset_and_empty_selection(conn, seeded):
    rep = flows.income_expense(conn, "2026-01-01", "2026-03-31", bucket="total",
                               account_ids=[seeded["sav"]])
    assert [r.path for r in rep.rows()] == ["Fees"]
    assert rep.total_income == 0 and rep.net == -5_00
    empty = flows.income_expense(conn, "2026-01-01", "2026-03-31", account_ids=[])
    assert empty.rows() == [] and empty.buckets == ["2026-01", "2026-02", "2026-03"]


# ---------------------------------------------------------------------------
# cash flow
# ---------------------------------------------------------------------------
def test_cash_flow_over_a_subset_shows_transfers_out(conn, seeded):
    rep = flows.cash_flow(conn, "2026-01-01", "2026-03-31", account_ids=[seeded["chk"]])
    assert rep.total_income == 9000_00
    assert rep.total_expense == -3450_00                              # no savings fee
    assert [(t.name, t.cents) for t in rep.transfers] == [("Savings", -500_00)]
    assert rep.net_transfers == -500_00
    assert rep.net == 9000_00 - 3450_00 - 500_00
    # Over every account the transfer nets out and the section is empty.
    whole = flows.cash_flow(conn, "2026-01-01", "2026-03-31")
    assert whole.transfers == [] and whole.net == 5545_00
    # From the savings side the same transfer is money IN.
    sav = flows.cash_flow(conn, "2026-01-01", "2026-03-31", account_ids=[seeded["sav"]])
    assert [(t.name, t.cents) for t in sav.transfers] == [("Checking", 500_00)]
    assert sav.net == 500_00 - 5_00


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------
def test_compare_periods_reports_delta_and_percent(conn, seeded):
    rep = flows.compare_periods(conn, "2026-01-01", "2026-01-31",
                                "2026-02-01", "2026-02-28")
    rows = _by_path(rep.rows)
    g = rows["Groceries"]
    assert (g.a_cents, g.b_cents, g.delta, g.pct) == (-200_00, -130_00, 70_00, 35.0)
    f = rows["Auto:Fuel"]
    assert (f.a_cents, f.b_cents, f.delta, f.pct) == (-50_00, 0, 50_00, 100.0)
    h = rows["Household"]
    assert (h.a_cents, h.b_cents, h.pct) == (0, -30_00, None)         # nothing to compare against
    assert rep.rows[0].path == "Salary"                               # income first
    totals = {t.label: t for t in rep.totals}
    assert (totals["net"].a_cents, totals["net"].b_cents) == (1750_00, 1835_00)
    assert totals["income"].delta == 0 and totals["income"].pct == 0.0
    assert totals["expense"].delta == 1250_00 - 1165_00


# ---------------------------------------------------------------------------
# averages
# ---------------------------------------------------------------------------
def test_category_averages_divide_by_every_bucket_spanned(conn, seeded):
    rep = flows.category_averages(conn, "2026-01-01", "2026-03-31", bucket="month")
    assert rep.n_buckets == 3
    rows = {r.path: r for r in rep.rows}
    assert rows["Salary"].average == 3000_00
    assert rows["Auto:Fuel"].average == -16_67          # -50.00 / 3, half-up
    assert rows["Groceries"].average == -110_00
    assert rep.average_income == 3000_00
    assert rep.average_expense == -1151_67
    assert rep.average_net == 1848_33
    with pytest.raises(ValueError):
        flows.category_averages(conn, "2026-01-01", "2026-03-31", bucket="total")

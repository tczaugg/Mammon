"""The read-only budget MCP tools (parity roadmap items 3 + 6).

These tools sit on top of :mod:`mammon.reports.budget`, so the tests pin what
the *tool surface* adds: money crosses as decimal dollar strings (never floats),
the read-only ``query_only`` connection can serve them, and -- the property the
whole MCP surface exists to guarantee -- nothing identifying leaks. Budgets name
categories and dollars only; the account_number / url / download_config columns
live on ``accounts`` and must never appear, and the budget tables must not carry
such a column in the first place.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from mammon import budgets, db, ledger, mcp_server, mcp_tools, sqldriver
from mammon.tests import fresh_db


@pytest.fixture
def dbfile(tmp_path):
    return tmp_path / "budgets_mcp.db"


@pytest.fixture
def conn(dbfile):
    c = fresh_db(dbfile)
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    """One account carrying secrets, Jan+Feb spending, and a two-month budget
    that lines up Fuel/Groceries with Dining in January; Shopping is spent in
    February with no line (an unbudgeted category)."""
    chk = ledger.create_account(conn, "Checking", "checking")
    sav = ledger.create_account(conn, "Savings", "savings")
    ledger.update_account(conn, chk, account_number="XXXX1234",
                          url="https://bank.example")
    fuel = ledger.resolve_category(conn, "Fuel")
    groc = ledger.resolve_category(conn, "Groceries")
    dining = ledger.resolve_category(conn, "Dining")
    shopping = ledger.resolve_category(conn, "Shopping")
    salary = ledger.resolve_category(conn, "Salary")

    ledger.add_transaction(conn, chk, "2026-01-05", -100_00, category_id=fuel)
    ledger.add_transaction(conn, chk, "2026-01-12", -40_00, category_id=fuel)
    ledger.add_transaction(conn, chk, "2026-01-15", -250_00, category_id=groc)
    ledger.add_transaction(conn, chk, "2026-01-20", -30_00, category_id=dining)
    ledger.add_transaction(conn, chk, "2026-02-08", -60_00, category_id=fuel)
    ledger.add_transaction(conn, chk, "2026-02-14", -200_00, category_id=groc)
    ledger.add_transaction(conn, chk, "2026-02-22", -80_00, category_id=shopping)
    ledger.add_transaction(conn, chk, "2026-01-28", 3000_00, category_id=salary)
    ledger.create_transfer(conn, chk, sav, "2026-01-25", 500_00)

    active = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, active, fuel, "2026-01", 120_00)
    budgets.set_line(conn, active, fuel, "2026-02", 120_00)
    budgets.set_line(conn, active, groc, "2026-01", 300_00)
    budgets.set_line(conn, active, groc, "2026-02", 300_00)
    budgets.set_line(conn, active, dining, "2026-01", 50_00)
    retired = budgets.create_budget(conn, "Old Plan", active=False)
    return {"budget": active, "retired": retired, "fuel": fuel}


def test_list_budgets(conn, seeded):
    out = mcp_tools.list_budgets(conn)
    names = {b["name"]: b for b in out["budgets"]}
    assert names["Household"]["active"] is True and names["Household"]["id"] == seeded["budget"]
    assert names["Old Plan"]["active"] is False
    # include_inactive=False drops the retired budget
    active_only = mcp_tools.list_budgets(conn, include_inactive=False)
    assert [b["name"] for b in active_only["budgets"]] == ["Household"]


def _money_strings(report):
    """Every money value anywhere in a budget report dict, for a float check."""
    vals = [report["budgeted"], report["actual"], report["remaining"]]
    for r in report["categories"]:
        vals += [r["budgeted"], r["actual"], r["remaining"]]
    for t in report["by_period"]:
        vals += [t["budgeted"], t["actual"], t["remaining"]]
    return vals


def test_budget_vs_actual_dollars(conn, seeded):
    out = mcp_tools.budget_vs_actual(conn, seeded["budget"], "2026-01", "2026-02")
    assert out["budget"] == "Household"
    assert out["periods"] == ["2026-01", "2026-02"]

    by_cat = {r["category"]: r for r in out["categories"]}
    assert by_cat["Fuel"] == {"category": "Fuel", "category_id": seeded["fuel"],
                              "budgeted": "240.00", "actual": "200.00", "remaining": "40.00"}
    assert by_cat["Groceries"]["actual"] == "450.00"
    assert by_cat["Dining"]["remaining"] == "20.00"
    # unbudgeted category surfaces with 0 budget and negative remaining
    shopping = by_cat["Shopping"]
    assert (shopping["budgeted"], shopping["actual"], shopping["remaining"]) == ("0.00", "80.00", "-80.00")
    # income and transfers never appear
    assert "Salary" not in by_cat

    per = {t["period"]: t for t in out["by_period"]}
    assert per["2026-01"] == {"period": "2026-01", "budgeted": "470.00",
                              "actual": "420.00", "remaining": "50.00"}
    assert per["2026-02"]["actual"] == "340.00"

    assert out["budgeted"] == "890.00" and out["actual"] == "760.00"
    assert out["remaining"] == "130.00"

    # every money value is a decimal string, never a float
    for v in _money_strings(out):
        assert isinstance(v, str)
    # a single month omits `end`
    one = mcp_tools.budget_vs_actual(conn, seeded["budget"], "2026-01")
    assert one["periods"] == ["2026-01"]


def test_budget_vs_actual_exclude_unbudgeted(conn, seeded):
    out = mcp_tools.budget_vs_actual(conn, seeded["budget"], "2026-01", "2026-02",
                                     include_unbudgeted=False)
    assert "Shopping" not in {r["category"] for r in out["categories"]}
    assert out["actual"] == "680.00"


def test_budget_ytd(conn, seeded):
    ytd = mcp_tools.budget_ytd(conn, seeded["budget"], 2026, through_month=2)
    rng = mcp_tools.budget_vs_actual(conn, seeded["budget"], "2026-01", "2026-02")
    assert ytd["categories"] == rng["categories"]
    assert ytd["budgeted"] == rng["budgeted"] and ytd["actual"] == rng["actual"]
    # full year keeps the twelve calendar months
    full = mcp_tools.budget_ytd(conn, seeded["budget"], 2026)
    assert full["periods"] == [f"2026-{m:02d}" for m in range(1, 13)]


def test_no_identifying_data_leaks(conn, seeded):
    """Budgets name categories and dollars only; the account's secrets, set on
    the same ledger, must not ride along in any budget tool's output."""
    outs = [
        mcp_tools.list_budgets(conn),
        mcp_tools.budget_vs_actual(conn, seeded["budget"], "2026-01", "2026-02"),
        mcp_tools.budget_ytd(conn, seeded["budget"], 2026),
    ]
    for out in outs:
        text = json.dumps(out)
        assert "XXXX1234" not in text and "bank.example" not in text


def test_budget_tables_have_no_sensitive_columns(conn):
    """The columns the MCP surface blanks (account_number/url/download_config)
    live on `accounts`; verify the budget tables cannot even hold them."""
    sensitive = {c for _, c in mcp_tools.SENSITIVE_COLUMNS}
    for table in ("budgets", "budget_lines"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert cols and not (cols & sensitive), table


def test_tools_serve_over_readonly_connection(dbfile, conn, seeded):
    """The tools must run under `PRAGMA query_only` -- pure reads, no stray
    write -- and return the same numbers as the writer connection."""
    ro = mcp_server.open_readonly(str(dbfile))
    try:
        with pytest.raises(sqldriver.OperationalError):
            ro.execute("DELETE FROM budget_lines")     # connection really is read-only
        got = mcp_tools.budget_vs_actual(ro, seeded["budget"], "2026-01", "2026-02")
        assert got["actual"] == "760.00"
        assert [b["name"] for b in mcp_tools.list_budgets(ro)["budgets"]] == ["Household", "Old Plan"]
    finally:
        ro.close()

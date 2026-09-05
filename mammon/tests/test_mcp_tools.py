"""The read-only LLM tool surface (roadmap item 3): every tool returns JSON,
resolves accounts and categories by name, never returns an identifying column,
and the server's connection cannot write."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from mammon import (budgets, db, investments, ledger, loans, mcp_server, mcp_tools,
                    rebalance, scheduled)


@pytest.fixture
def dbfile(tmp_path):
    return tmp_path / "tools.db"


@pytest.fixture
def conn(dbfile):
    c = db.init_db(dbfile)
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    loan_acct = ledger.create_account(conn, "Mortgage", "liability", opening_balance=0)
    ledger.update_account(conn, chk, account_number="XXXX1234", url="https://bank.example")
    rent = ledger.resolve_category(conn, "Housing:Rent")
    groc = ledger.resolve_category(conn, "Food:Groceries")
    salary = ledger.resolve_category(conn, "Salary")
    for m in ("01", "02", "03"):
        ledger.add_transaction(conn, chk, f"2026-{m}-01", 3000_00, payee="Employer",
                               category_id=salary)
        ledger.add_transaction(conn, chk, f"2026-{m}-02", -1000_00, payee="Landlord",
                               category_id=rent, tag="home")
    ledger.add_transaction(conn, chk, "2026-01-10", -200_00, payee="Grocer", category_id=groc)
    ledger.create_transfer(conn, chk, sav, "2026-01-15", 500_00, payee="Stash")
    investments.record_investment(conn, inv, "2026-01-20", "Buy", symbol="VTI",
                                  quantity="10", price="100", amount=-1000_00)
    investments.rebuild_holdings(conn, inv)
    investments.record_price(conn, "VTI", "2026-02-27", "110")
    loans.set_loan_params(conn, loan_acct, original_principal=100000_00,
                          origination_date="2025-12-01", term_months=360,
                          payment_amount=600_00, interval="monthly",
                          rates=[("2025-12-01", "6")])          # 6% (RateRow is a percentage)
    scheduled.add_scheduled(conn, chk, payee="Streaming Co", amount=-15_00,
                            frequency="monthly", next_date="2099-01-05")
    rebalance.create_target(conn, "Sixty forty",
                            lines={"domestic_stock": 60, "bond": 40}, active=True)
    bud = budgets.create_budget(conn, "Household")
    budgets.set_line(conn, bud, rent, "2026-01", 1200_00)
    budgets.set_line(conn, bud, groc, "2026-01", 300_00)
    return {"chk": chk, "sav": sav, "inv": inv, "loan": loan_acct, "budget": bud}


def _json(obj):
    return json.loads(json.dumps(obj))


def _payload(result):
    """The dict a tool call produced, whatever shape the SDK version hands
    back: a (content, structured) pair, a structured dict (possibly wrapped
    in {"result": ...}), or a list of text content blocks carrying JSON."""
    if isinstance(result, tuple):
        content, structured = result
        if isinstance(structured, dict):
            return structured.get("result", structured)
        result = content
    if isinstance(result, dict):
        return result.get("result", result)
    first = list(result)[0]
    return json.loads(getattr(first, "text", "{}"))


# ---------------------------------------------------------------------------
# conversions and resolution
# ---------------------------------------------------------------------------
def test_dollars_and_cents_round_trip():
    assert mcp_tools.dollars(-123456) == "-1234.56"
    assert mcp_tools.dollars(5) == "0.05" and mcp_tools.dollars(None) is None
    assert mcp_tools.cents_of("12.50") == 1250
    assert mcp_tools.cents_of(12.5) == 1250 and mcp_tools.cents_of("$1,000") == 100000
    assert mcp_tools.cents_of(None) is None
    with pytest.raises(ValueError):
        mcp_tools.cents_of("lots")


def test_accounts_and_categories_resolve_by_name_or_id(conn, seeded):
    chk = seeded["chk"]
    assert mcp_tools.resolve_accounts(conn, None) is None
    assert mcp_tools.resolve_accounts(conn, ["checking", chk]) == [chk, chk]
    assert mcp_tools.resolve_account(conn, "Savings") == seeded["sav"]
    with pytest.raises(ValueError, match="accounts: Brokerage, Checking"):
        mcp_tools.resolve_accounts(conn, ["Nope"])
    with pytest.raises(ValueError):
        mcp_tools.resolve_accounts(conn, [999])
    groc = ledger.resolve_category(conn, "Food:Groceries")
    assert mcp_tools.resolve_categories(conn, ["food:groceries", groc]) == [groc, groc]
    with pytest.raises(ValueError, match="list_categories"):
        mcp_tools.resolve_categories(conn, ["Nope"])


# ---------------------------------------------------------------------------
# every tool answers with JSON and nothing identifying
# ---------------------------------------------------------------------------
def test_every_tool_returns_json_without_identifying_data(conn, seeded):
    calls = {
        "overview": {}, "list_accounts": {"include_hidden": True}, "list_categories": {},
        "net_worth": {"as_of": "2026-03-31"}, "account_balances": {},
        "balances_over_time": {"start": "2026-01-01", "end": "2026-03-31"},
        "income_expense": {"start": "2026-01-01", "end": "2026-03-31", "accounts": ["Checking"]},
        "cash_flow": {"start": "2026-01-01", "end": "2026-03-31", "accounts": ["Checking"]},
        "compare_periods": {"a_start": "2026-01-01", "a_end": "2026-01-31",
                            "b_start": "2026-02-01", "b_end": "2026-02-28"},
        "category_averages": {"start": "2026-01-01", "end": "2026-03-31"},
        "spending_by_category": {"start": "2026-01-01", "end": "2026-03-31"},
        "by_payee": {"start": "2026-01-01", "end": "2026-03-31"},
        "by_tag": {"start": "2026-01-01", "end": "2026-03-31", "direction": "net"},
        "transactions": {"start": "2026-01-01", "end": "2026-03-31"},
        "search": {"query": "landlord"},
        "holdings": {"account": "Brokerage", "as_of": "2026-02-28"},
        "lots": {"account": "Brokerage", "as_of": "2026-02-28"},
        "capital_gains": {"account": "Brokerage", "start": "2026-01-01", "end": "2026-03-31"},
        "performance": {"account": "Brokerage", "start": "2026-01-01", "end": "2026-02-28"},
        "investment_performance": {"as_of": "2026-02-28"},
        "allocation": {"as_of": "2026-02-28"},
        "allocation_drift": {"as_of": "2026-02-28"},
        "security_mixtures": {},
        "upcoming": {"days": 30},
        "loan": {"account": "Mortgage"},
        "list_budgets": {"include_inactive": True},
        "budget_vs_actual": {"budget_id": seeded["budget"], "start": "2026-01",
                             "end": "2026-03"},
        "budget_ytd": {"budget_id": seeded["budget"], "year": 2026},
        "schema": {},
        "query": {"sql": "SELECT id, name, account_number, url FROM accounts"},
    }
    assert set(calls) == set(mcp_tools.TOOLS)
    for name, kwargs in calls.items():
        out = mcp_tools.TOOLS[name](conn, **kwargs)
        text = json.dumps(out)                       # JSON-serializable
        assert "XXXX1234" not in text and "bank.example" not in text, name
        assert _json(out) == out


def test_allocation_tool_takes_a_scope_and_never_allocates_debt(conn, seeded):
    """The scope the window offers is on the tool too, so a question asked in
    words ("what am I worth, house included") gets the same answer."""
    inv = mcp_tools.allocation(conn, as_of="2026-02-28")
    assert inv["scope"] == "investments"
    every = mcp_tools.allocation(conn, as_of="2026-02-28", scope="everything")
    assert every["scope"] == "everything"
    labels = [s["label"] for s in every["by_account"]]
    assert "Checking" in labels and "Mortgage" not in labels     # debt is never allocated
    assert float(every["total"]) > float(inv["total"])
    with pytest.raises(ValueError, match="scope"):
        mcp_tools.allocation(conn, scope="nonsense")


def test_report_tools_carry_the_numbers_in_dollars(conn, seeded):
    ie = mcp_tools.income_expense(conn, "2026-01-01", "2026-03-31", accounts=["Checking"])
    assert ie["total_income"] == "9000.00" and ie["total_expense"] == "-3200.00"
    assert ie["net"] == "5800.00" and ie["buckets"] == ["2026-01", "2026-02", "2026-03"]
    assert ie["expense"][0] == {"category": "Housing:Rent", "type": "expense",
                                "total": "-3000.00",
                                "by_bucket": {"2026-01": "-1000.00", "2026-02": "-1000.00",
                                              "2026-03": "-1000.00"}}
    cf = mcp_tools.cash_flow(conn, "2026-01-01", "2026-03-31", accounts=["Checking"])
    assert cf["transfers"] == [{"account": "Savings", "net": "-500.00"}]
    assert cf["net"] == "5300.00"
    cmp = mcp_tools.compare_periods(conn, "2026-01-01", "2026-01-31", "2026-02-01", "2026-02-28")
    groc = next(r for r in cmp["rows"] if r["category"] == "Food:Groceries")
    assert groc == {"category": "Food:Groceries", "type": "expense", "a": "-200.00",
                    "b": "0.00", "change": "200.00", "percent": 100.0}
    # by_payee is SIGNED and NET by default: a payee who pays you reads positive,
    # one you pay reads negative, and a payee on both sides nets out. Ranking is
    # by magnitude, so the employer's 9,000.00 leads.
    top = mcp_tools.by_payee(conn, "2026-01-01", "2026-03-31", limit=1)
    assert top["direction"] == "net"
    assert top["rows"] == [{"payee": "Employer", "count": 3, "amount": "9000.00"}]
    assert top["truncated"] is True
    everyone = mcp_tools.by_payee(conn, "2026-01-01", "2026-03-31")
    assert {"payee": "Landlord", "count": 3, "amount": "-3000.00"} in everyone["rows"]
    bal = mcp_tools.balances_over_time(conn, "2026-01-01", "2026-02-28", accounts=["Savings"])
    assert [s["total"] for s in bal["samples"]] == ["500.00", "500.00"]


def test_row_tools_filter_and_bound(conn, seeded):
    tx = mcp_tools.transactions(conn, "2026-01-01", "2026-03-31", categories=["Housing:Rent"],
                                min_amount="999", limit=2)
    assert tx["count"] == 2 and tx["truncated"] is True
    assert tx["rows"][0]["payee"] == "Landlord" and tx["rows"][0]["amount"] == "-1000.00"
    assert tx["rows"][0]["category"] == "Housing:Rent" and tx["rows"][0]["tag"] == "home"
    hit = mcp_tools.search(conn, "stash")
    assert hit["count"] == 2 and {r["account"] for r in hit["rows"]} == {"Checking", "Savings"}
    with pytest.raises(ValueError, match="ISO date"):
        mcp_tools.transactions(conn, "yesterday", "2026-03-31")


def test_holdings_upcoming_and_loan_shapes(conn, seeded):
    h = mcp_tools.holdings(conn, "Brokerage", as_of="2026-02-28")
    assert h["holdings"][0]["symbol"] == "VTI" and h["holdings"][0]["quantity"] == "10"
    assert h["holdings"][0]["market_value"] == "1100.00" and h["holdings"][0]["gain"] == "100.00"
    assert h["cash"] == "-1000.00" and h["total"] == "100.00"
    up = mcp_tools.upcoming(conn, days=30)
    assert all(r["payee"] != "Streaming Co" for r in up["rows"])       # due in 2099
    far = mcp_tools.upcoming(conn, days=40000)
    assert any(r["payee"] == "Streaming Co" and r["amount"] == "-15.00" for r in far["rows"])
    ln = mcp_tools.loan(conn, "Mortgage")
    assert ln["original_principal"] == "100000.00"
    from decimal import Decimal
    assert Decimal(ln["annual_rate"]) == Decimal("6")
    assert ln["term_months"] == 360 and len(ln["next_payments"]) == 3
    assert ln["payoff_date"] > ln["next_payments"][0]["date"]
    with pytest.raises(ValueError, match="loan parameters"):
        mcp_tools.loan(conn, "Checking")


# ---------------------------------------------------------------------------
# the SQL tool and the schema it advertises
# ---------------------------------------------------------------------------
def test_query_is_read_only_and_blanks_identifying_columns(conn, seeded):
    out = mcp_tools.query(conn, "SELECT name, account_number, url FROM accounts ORDER BY name")
    assert out["columns"] == ["name", "account_number", "url"]
    assert out["rows"][1] == ["Checking", None, None]
    assert out["count"] == 4 and out["truncated"] is False
    capped = mcp_tools.query(conn, "SELECT id FROM transactions", limit=3)
    assert capped["count"] == 3 and capped["truncated"] is True
    for bad in ("DELETE FROM transactions", "UPDATE accounts SET name='x'",
                "SELECT 1; DELETE FROM transactions", "PRAGMA user_version = 1",
                "CREATE TABLE t (x)"):
        with pytest.raises((ValueError, sqlite3.Error)):
            mcp_tools.query(conn, bad)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] > 0
    sch = mcp_tools.schema(conn)
    cols = {c["name"] for c in sch["tables"]["accounts"]}
    assert "name" in cols and "account_number" not in cols and "url" not in cols
    assert "download_config" not in cols


# ---------------------------------------------------------------------------
# the server: a connection that cannot write, tools bound without `conn`
# ---------------------------------------------------------------------------
def test_open_readonly_refuses_writes_and_schema_mismatch(dbfile, conn, seeded):
    ro = mcp_server.open_readonly(str(dbfile))
    assert ro.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 4
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM transactions")
    ro.close()
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
    conn.commit()
    with pytest.raises(mcp_server.SchemaMismatch, match="migrate"):
        mcp_server.open_readonly(str(dbfile))
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    conn.commit()
    with pytest.raises(mcp_server.SchemaMismatch, match="newer"):
        mcp_server.open_readonly(str(dbfile))


def test_server_lists_every_tool_and_answers_calls(dbfile, conn, seeded):
    pytest.importorskip("mcp")
    ro = mcp_server.open_readonly(str(dbfile))
    server = mcp_server.build_server(ro)

    async def run():
        tools = await server.list_tools()
        names = {t.name for t in tools}
        assert names == set(mcp_tools.TOOLS)
        by_name = {t.name: t for t in tools}
        # `conn` is bound, not a parameter the model sees.
        assert "conn" not in by_name["income_expense"].inputSchema["properties"]
        assert "start" in by_name["income_expense"].inputSchema["properties"]
        assert "read-only" in (server.instructions or "").lower() or \
            "read-only" in mcp_tools.INSTRUCTIONS.lower()
        nw = _payload(await server.call_tool("net_worth", {"as_of": "2026-01-31"}))
        assert nw["as_of"] == "2026-01-31" and nw["net_worth"]
        err = _payload(await server.call_tool("holdings", {"account": "Nope"}))
        assert "no account named" in err["error"]
        return True

    assert asyncio.run(run()) is True
    ro.close()

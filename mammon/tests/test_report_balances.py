"""mammon.reports.balances: balances now and at every bucket end, valued the
way the account bar values them (investments at market), hidden accounts out
unless asked for (roadmap item 4)."""
from __future__ import annotations

import pytest

from mammon import db, investments, ledger
from mammon.reports import balances


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "balances.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    hid = ledger.create_account(conn, "Old Card", "credit", opening_balance=-100_00)
    ledger.set_account_hidden(conn, hid, True)
    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    ledger.add_transaction(conn, chk, "2026-01-05", 3000_00, payee="Employer")
    ledger.create_transfer(conn, chk, sav, "2026-01-15", 500_00)
    ledger.add_transaction(conn, chk, "2026-02-10", -200_00, payee="Grocer")
    ledger.add_transaction(conn, sav, "2026-03-01", 1_00, payee="Interest")
    investments.record_investment(
        conn, inv, "2026-01-20", "Buy", symbol="VTI", quantity="10", price="100",
        amount=-1000_00)
    investments.rebuild_holdings(conn, inv)
    # The raw writer records no price (the import path learns one from the
    # trade); value the position explicitly at each sample.
    investments.record_price(conn, "VTI", "2026-01-20", "100")
    investments.record_price(conn, "VTI", "2026-02-27", "110")
    return {"chk": chk, "sav": sav, "hid": hid, "inv": inv}


def test_account_balances_match_the_account_bar(conn, seeded):
    rep = balances.account_balances(conn, "2026-01-31")
    by = {r.name: r for r in rep.rows}
    assert set(by) == {"Checking", "Savings", "Brokerage"}          # hidden left out
    assert by["Checking"].cents == 1000_00 + 3000_00 - 500_00
    assert by["Savings"].cents == 500_00
    assert by["Brokerage"].cents == investments.display_balance(conn, seeded["inv"], "2026-01-31")
    assert rep.total == sum(r.cents for r in rep.rows)
    assert rep.total == ledger.net_worth(conn, as_of="2026-01-31")
    full = balances.account_balances(conn, "2026-01-31", include_hidden=True)
    assert {r.name for r in full.rows} == {"Checking", "Savings", "Brokerage", "Old Card"}
    assert {r.name: r.hidden for r in full.rows}["Old Card"] is True
    assert full.total == ledger.net_worth(conn, as_of="2026-01-31", include_hidden=True)


def test_balances_over_time_samples_each_bucket_end(conn, seeded):
    series = balances.balances_over_time(conn, "2026-01-01", "2026-03-15", bucket="month",
                                         account_ids=[seeded["chk"], seeded["sav"]])
    assert [a["name"] for a in series.accounts] == ["Checking", "Savings"]
    assert [s.as_of for s in series.samples] == ["2026-01-31", "2026-02-28", "2026-03-15"]
    chk, sav = seeded["chk"], seeded["sav"]
    jan, feb, mar = series.samples
    assert jan.by_account == {chk: 3500_00, sav: 500_00} and jan.total == 4000_00
    assert feb.by_account[chk] == 3300_00                           # the grocery spend
    assert mar.by_account[sav] == 501_00 and mar.total == 3300_00 + 501_00
    # An investment account is valued at market at each sample date.
    inv = balances.balances_over_time(conn, "2026-01-01", "2026-02-28", bucket="month",
                                      account_ids=[seeded["inv"]])
    assert [s.total for s in inv.samples] == [
        investments.display_balance(conn, seeded["inv"], "2026-01-31"),
        investments.display_balance(conn, seeded["inv"], "2026-02-28")]
    # The buy was funded from the account's own (zero) cash, so the position's
    # rise from 100 to 110 on ten shares is exactly what moves the balance.
    assert inv.samples[1].total - inv.samples[0].total == 100_00
    with pytest.raises(ValueError):
        balances.balances_over_time(conn, "2026-01-01", "2026-02-28", bucket="total")

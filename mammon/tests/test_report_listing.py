"""mammon.reports.listing: the filtered transaction listing -- every filter,
category subtrees through split lines, the register's category labels, and a
bounded result (roadmap item 4)."""
from __future__ import annotations

import pytest

from mammon import db, ledger
from mammon.reports import listing
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "listing.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1000_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    hid = ledger.create_account(conn, "Old Card", "credit")
    ledger.set_account_hidden(conn, hid, True)
    food = ledger.resolve_category(conn, "Food")
    groc = ledger.resolve_category(conn, "Food:Groceries")
    house = ledger.resolve_category(conn, "Household")
    rent = ledger.resolve_category(conn, "Housing:Rent")
    ledger.add_transaction(conn, chk, "2026-01-01", 3000_00, payee="Employer")
    ledger.add_transaction(conn, chk, "2026-01-02", -1000_00, payee="Landlord",
                           category_id=rent, tag="home", cleared=1)
    ledger.add_transaction(conn, chk, "2026-01-10", -200_00, payee="Grocer", memo="weekly",
                           category_id=groc)
    split = ledger.add_transaction(conn, chk, "2026-02-10", -180_00, payee="Big Box")
    ledger.set_splits(conn, split, [(groc, -150_00, ""), (house, -30_00, "")])
    ledger.create_transfer(conn, chk, sav, "2026-02-15", 500_00, payee="Stash")
    ledger.add_transaction(conn, chk, "2026-03-15", -40_00, payee="Mystery",
                           cleared=1, reconciled=1)
    sched = ledger.add_transaction(conn, chk, "2026-03-28", -1000_00, payee="Landlord",
                                   category_id=rent)
    conn.execute("UPDATE transactions SET scheduled=1 WHERE id=?", (sched,))
    ledger.add_transaction(conn, hid, "2026-02-05", -99_00, payee="Grocer", category_id=groc)
    conn.commit()
    return {"chk": chk, "sav": sav, "hid": hid, "food": food, "groc": groc,
            "house": house, "rent": rent, "split": split}


def _payees(rep):
    return [r["payee"] for r in rep.rows]


def test_listing_defaults_and_row_shape(conn, seeded):
    rep = listing.transactions(conn, "2026-01-01", "2026-03-31")
    assert rep.count == 7 and not rep.truncated                    # both transfer legs, no placeholder
    assert _payees(rep)[:3] == ["Employer", "Landlord", "Grocer"]
    row = next(r for r in rep.rows if r["payee"] == "Big Box")
    assert row["category"] == "--Split--" and row["is_split"] is True
    leg = next(r for r in rep.rows if r["payee"] == "Stash" and r["account"] == "Checking")
    assert leg["category"] == "[Savings]" and leg["transfer_account"] == "Savings"
    assert leg["amount"] == -500_00
    rent = next(r for r in rep.rows if r["payee"] == "Landlord")
    assert rent["category"] == "Housing:Rent" and rent["tag"] == "home" and rent["cleared"]
    assert rep.total_cents == sum(r["amount"] for r in rep.rows)
    assert rep.total_cents == 3000_00 - 1000_00 - 200_00 - 180_00 - 500_00 + 500_00 - 40_00


def test_listing_filters(conn, seeded):
    t = lambda **kw: listing.transactions(conn, "2026-01-01", "2026-03-31", **kw)
    assert t(include_transfers=False).count == 5
    assert t(include_scheduled=True).count == 8
    assert t(include_hidden=True).count == 8
    assert _payees(t(category_ids=[seeded["groc"]])) == ["Grocer", "Big Box"]   # split line hit
    assert _payees(t(category_ids=[seeded["food"]])) == ["Grocer", "Big Box"]   # subtree
    assert _payees(t(payee_contains="land")) == ["Landlord"]
    assert _payees(t(memo_contains="WEEK")) == ["Grocer"]
    assert _payees(t(tag="home")) == ["Landlord"]
    assert _payees(t(amount_min=100000)) == ["Employer", "Landlord"]
    assert _payees(t(amount_max=50_00)) == ["Mystery"]
    assert _payees(t(cleared="cleared")) == ["Landlord"]
    assert _payees(t(cleared="reconciled")) == ["Mystery"]
    assert t(cleared="uncleared").count == 5
    assert _payees(t(account_ids=[seeded["sav"]])) == ["Stash"]
    assert t(account_ids=[]).count == 0
    with pytest.raises(ValueError):
        t(cleared="maybe")


def test_listing_order_and_limit(conn, seeded):
    rep = listing.transactions(conn, "2026-01-01", "2026-03-31", newest_first=True)
    assert rep.rows[0]["payee"] == "Mystery"
    cut = listing.transactions(conn, "2026-01-01", "2026-03-31", limit=3)
    assert cut.count == 3 and cut.truncated is True
    assert _payees(cut) == ["Employer", "Landlord", "Grocer"]
    assert cut.total_cents == 3000_00 - 1000_00 - 200_00

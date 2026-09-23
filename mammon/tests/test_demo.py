"""The synthetic demo ledger.

Two of these guard promises the module makes rather than mechanics: that every
name in the seeded ledger comes from the module's own invented tables (the
README's screenshots are taken from this data, so a real name reaching it is a
privacy failure, not a cosmetic one), and that the same anchor date always
produces the same ledger (a screenshot regenerated next month should differ
only by the dates that moved).
"""

import os
import sqlite3
from datetime import date, timedelta

import pytest

from mammon import asset_values, db, demo, investments, ledger, scheduled
from mammon.tests import fresh_db


@pytest.fixture()
def conn(tmp_path):
    return fresh_db(str(tmp_path / "demo.db"))


ANCHOR = date(2026, 6, 15)

# Six months is enough history for every property these tests check, and the
# full 30-month build costs ~7s a call -- ten of those is a minute of suite time
# spent re-proving the same invariants.


def test_seeds_one_account_in_every_account_bar_group(conn):
    """The account bar groups Banking / Credit Card / Investing / Property &
    Debt and subtotals each. A demo missing a group shows an empty heading."""
    demo.build(conn, today=ANCHOR, months=6)
    types = {a["type"] for a in ledger.list_accounts(conn)}
    assert {"checking", "savings"} & types            # Banking
    assert "credit" in types                          # Credit Card
    assert "investment" in types                      # Investing
    assert {"asset", "liability"} <= types            # Property & Debt


def test_every_payee_is_an_invented_one(conn):
    """No name may reach this ledger except through the module's own tables.

    This is the check that matters: the demo is what the public README shows,
    and profiling a real ledger to calibrate it is exactly the situation where a
    real payee or category could be copied across by accident."""
    demo.build(conn, today=ANCHOR, months=6)
    allowed = {p for names in demo.PAYEES.values() for p in names}
    allowed |= {b[2] for b in demo.BILLS}
    allowed |= {"Ridgeline Systems", "Anytown Credit Union", "ATM Withdrawal"}
    found = {r[0] for r in conn.execute(
        "SELECT DISTINCT payee FROM transactions WHERE payee IS NOT NULL AND payee <> ''")}
    assert found <= allowed, "unexpected payee(s): %s" % sorted(found - allowed)


def test_the_same_anchor_date_rebuilds_the_same_ledger(tmp_path):
    """Seeded amounts, so regenerating a screenshot does not reshuffle rows."""
    def totals(path):
        c = fresh_db(str(path))
        demo.build(c, today=ANCHOR, months=6)
        return (c.execute("SELECT COUNT(*), SUM(amount) FROM transactions").fetchone(),
                c.execute("SELECT COUNT(*) FROM splits").fetchone()[0])

    assert totals(tmp_path / "a.db") == totals(tmp_path / "b.db")


def test_split_lines_sum_to_their_parent(conn):
    """The paycheck and the warehouse run are seeded as splits; a split whose
    legs do not sum to the parent is the invariant the register enforces."""
    demo.build(conn, today=ANCHOR, months=6)
    rows = conn.execute("""
        SELECT t.id, t.amount, SUM(s.amount) legs
        FROM transactions t JOIN splits s ON s.transaction_id = t.id
        GROUP BY t.id""").fetchall()
    assert rows, "the demo seeded no splits at all"
    assert [r for r in rows if r["amount"] != r["legs"]] == []


def test_transfers_are_mirrored_on_both_sides(conn):
    """Every transfer leg must have its partner, or net worth double-counts.

    ``transfer_pair_id`` cross-links each row to the OTHER row's id (it is not a
    shared group key), so the property to check is that the link points back and
    the two legs are equal and opposite."""
    demo.build(conn, today=ANCHOR, months=6)
    legs = conn.execute(
        "SELECT id, transfer_pair_id, amount, account_id FROM transactions"
        " WHERE transfer_pair_id IS NOT NULL").fetchall()
    assert legs, "the demo seeded no transfers"
    by_id = {r["id"]: r for r in legs}
    for leg in legs:
        partner = by_id.get(leg["transfer_pair_id"])
        assert partner is not None, f"leg {leg['id']} has no partner row"
        assert partner["transfer_pair_id"] == leg["id"]      # links back
        assert partner["amount"] == -leg["amount"]           # equal and opposite
        assert partner["account_id"] != leg["account_id"]


def test_investment_cash_is_not_left_deeply_negative(conn):
    """Buys are funded by transfers first. Without that the investing group
    subtotals read as large negatives -- the account bar looks broken."""
    ids = demo.build(conn, today=ANCHOR, months=6)
    for role in ("brokerage", "plan"):
        val = investments.account_valuation(conn, ids[role], as_of=ANCHOR.isoformat())
        assert val.securities > 0, f"{role} holds nothing"
        assert val.cash > -1_00, f"{role} cash is {val.cash}"


def test_holdings_are_priced(conn):
    """Lots plus price history: a holding with no close reads as unpriced, and
    the whole valuation column goes blank."""
    ids = demo.build(conn, today=ANCHOR, months=6)
    values = investments.holding_values(conn, ids["brokerage"], ANCHOR.isoformat())
    assert values
    assert all(hv.market_value > 0 for hv in values)


def test_scheduled_definitions_are_upcoming_so_the_calendar_fills(conn):
    """The window opens on the Financial Calendar. With no definition due after
    the anchor date it opens on an empty month."""
    demo.build(conn, today=ANCHOR, months=6)
    defs = scheduled.list_scheduled(conn, active_only=True)
    assert len(defs) >= 5
    horizon = (ANCHOR + timedelta(days=31)).isoformat()
    assert [d for d in defs
            if ANCHOR.isoformat() <= d["next_date"] <= horizon]


def test_the_house_carries_a_valuation_series_above_its_cost_basis(conn):
    """The register tracks cost basis; value lives in its own editable history.
    Equal numbers would make the distinction invisible in a screenshot."""
    ids = demo.build(conn, today=ANCHOR, months=6)
    basis = ledger.account_balance(conn, ids["house"])
    value = asset_values.market_value(conn, ids["house"], ANCHOR.isoformat())
    assert value and value > basis
    assert len(asset_values.value_history(conn, ids["house"])) >= 3


def test_demo_flag_seeds_only_an_empty_database(conn):
    """--demo must never add a second copy on top of existing accounts."""
    from mammon import app
    app._ensure_seed(conn, True)
    first = len(ledger.list_accounts(conn, include_closed=True))
    app._ensure_seed(conn, True)
    assert len(ledger.list_accounts(conn, include_closed=True)) == first
    assert first >= 7

"""Tests for the holdings/balance SNAPSHOT import (SRD 5.11b, compartment F):
mammon/importers/holdings_csv.py (pure) + investments.apply_holdings_snapshot
(the DB-facing half) + importers/holdings_core.py (the two-line glue).

What must hold, and why:

  * a snapshot NEVER becomes a transaction -- it is a statement of fact, and
    turning it into one would invent history. Asserted directly: the investment
    transaction count is unchanged by an import.
  * funds match BY SYMBOL when the file has one and BY NAME otherwise, because a
    401(k) export of internal funds usually has no symbol column at all. Both
    routes end at the canonical symbol, so an aliased (renamed) fund is matched
    under its old name and lands on the new identity.
  * a TICKERLESS fund's statement price is the only price that will ever exist
    for it, so it lands in price_history under its own source.
  * the stated ending share count is left where the share reconcile dialog picks
    it up: the per-security reconcile draft.
  * a fund the ledger does not know is REPORTED, never guessed at.

All data is synthetic: ANON fund names, invented share counts, no account
numbers, no PII.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from mammon import db, investments, ledger
from mammon.importers import holdings_core
from mammon.importers.holdings_csv import (
    looks_like_holdings_csv,
    parse_holdings_csv,
    parse_holdings_file,
)
from mammon.tests import fresh_db

FIXTURE = Path(__file__).parent / "fixtures" / "anon_401k_holdings.csv"

BALANCED = "ANON BALANCED FUND"
STABLE = "ANON STABLE VALUE FUND"
LARGE = "ANONX ANON LARGE CAP INDEX"


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "shareimport.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "ANON 401(k)", "investment",
                                 opening_balance=0)


def _buy(conn, a, date, sym, qty, price="10.00"):
    amount = -int(Decimal(qty) * Decimal(price) * 100)
    return investments.record_investment(conn, a, date, "Buy", symbol=sym,
                                         quantity=qty, price=price,
                                         amount=amount)


def _seed(conn, a):
    """The three funds the fixture names, as the ledger already knows them."""
    _buy(conn, a, "2026-01-15", BALANCED, "100", "20.00")
    _buy(conn, a, "2026-02-15", STABLE, "1000", "1.00")
    _buy(conn, a, "2026-02-15", LARGE, "50.5", "30.00")


def _txn_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM investment_transactions").fetchone()[0]


def _by_name(results, name):
    for r in results:
        if r["name"] == name:
            return r
    raise AssertionError("no snapshot result for " + name)


# --- the pure parser -------------------------------------------------------

def test_parser_reads_the_fixture_without_a_database():
    rows = parse_holdings_file(FIXTURE)
    names = [r.name for r in rows]
    assert BALANCED in names and STABLE in names and LARGE in names
    # The "Total" line is not a position.
    assert not any(r.name.lower().startswith("total") for r in rows)
    bal = [r for r in rows if r.name == BALANCED][0]
    assert bal.quantity == "123.456"
    assert bal.price == "24.19"
    assert bal.market_value == "2986.40"      # thousands separator stripped
    assert bal.symbol == ""                   # an untickered plan fund
    assert bal.date == "2026-03-31"           # from the preamble, as ISO
    assert bal.key == BALANCED                # name is the identity it carries


def test_parser_recognises_a_snapshot_and_rejects_a_transaction_file():
    assert looks_like_holdings_csv(FIXTURE.read_text(encoding="utf-8"))
    txns = "Date,Description,Amount\n2026-03-01,ANON STORE,-12.34\n"
    assert not looks_like_holdings_csv(txns)
    assert parse_holdings_csv(txns) == []


def test_parser_keeps_numbers_as_decimal_text_never_floats():
    text = ("Fund,Shares,Price\n"
            "ANON FUND,\"1,234.5678\",$1.005\n"
            "ANON SHORT FUND,(12.5),2.00\n")
    rows = parse_holdings_csv(text, as_of="2026-03-31")
    assert [r.quantity for r in rows] == ["1234.5678", "-12.5"]
    assert rows[0].price == "1.005"
    assert all(isinstance(r.quantity, str) for r in rows)


def test_parser_takes_a_per_row_date_column_over_the_fallback():
    text = ("Security,Units,NAV,As of Date\n"
            "ANON FUND,10,5.00,3/31/2026\n")
    rows = parse_holdings_csv(text, as_of="2020-01-01")
    assert rows[0].date == "2026-03-31"


# --- applying it -----------------------------------------------------------

def test_import_creates_no_transactions(conn, acct):
    _seed(conn, acct)
    before = _txn_count(conn)
    holdings_core.import_holdings_file(conn, acct, FIXTURE)
    assert _txn_count(conn) == before


def test_untickered_funds_match_by_name_and_state_the_ending_quantity(conn, acct):
    _seed(conn, acct)
    results = holdings_core.import_holdings_file(conn, acct, FIXTURE)
    bal = _by_name(results, BALANCED)
    assert bal["symbol"] == BALANCED
    assert bal["matched_by"] == "name"
    assert bal["quantity"] == "123.456"
    assert bal["book_qty"] == "100"
    assert bal["difference"] == "23.456"       # what a reconcile must explain

    draft = investments.get_share_reconcile_draft(conn, acct, BALANCED)
    assert draft["statement_date"] == "2026-03-31"
    assert draft["ending_qty"] == "123.456"
    assert draft["ending_price"] == "24.19"


def test_a_ticker_column_matches_the_stored_security(conn, acct):
    _seed(conn, acct)
    results = holdings_core.import_holdings_file(conn, acct, FIXTURE)
    large = _by_name(results, LARGE)
    assert large["symbol"] == LARGE
    assert large["matched_by"] == "symbol"     # the "ANONX" column found it


def test_tickerless_fund_price_lands_in_price_history(conn, acct):
    _seed(conn, acct)
    # No source ever recorded a ticker for a plan's internal fund, so no quote
    # feed will ever price it: the statement figure is the only price there is.
    assert conn.execute("SELECT COUNT(*) FROM securities WHERE symbol=? "
                        "AND COALESCE(ticker,'') <> ''",
                        (BALANCED,)).fetchone()[0] == 0
    holdings_core.import_holdings_file(conn, acct, FIXTURE)
    row = conn.execute(
        "SELECT close_price, source FROM price_history WHERE symbol=? AND date=?",
        (BALANCED, "2026-03-31")).fetchone()
    assert row is not None
    assert Decimal(row["close_price"]) == Decimal("24.19")
    assert row["source"] == investments.SNAPSHOT_PRICE_SOURCE
    assert investments.latest_price(conn, BALANCED,
                                    as_of="2026-03-31") == Decimal("24.19")


def test_price_is_derived_from_value_when_the_file_prints_no_price(conn, acct):
    _buy(conn, acct, "2026-01-15", BALANCED, "100", "20.00")
    text = ("Fund,Shares,Market Value\n"
            "ANON BALANCED FUND,100,2500.00\n")
    results = holdings_core.import_holdings_csv(conn, acct, text,
                                               as_of="2026-03-31")
    assert results[0]["price"] == "25"
    assert investments.latest_price(conn, BALANCED,
                                    as_of="2026-03-31") == Decimal("25")


def test_an_unknown_fund_is_reported_not_guessed(conn, acct):
    _seed(conn, acct)
    results = holdings_core.import_holdings_file(conn, acct, FIXTURE)
    ghost = _by_name(results, "ANON FUND NOBODY OWNS")
    assert ghost["matched_by"] is None
    assert ghost["draft_saved"] is False
    assert ghost["price_recorded"] is False
    assert conn.execute(
        "SELECT COUNT(*) FROM price_history WHERE symbol=?",
        ("ANON FUND NOBODY OWNS",)).fetchone()[0] == 0
    summary = holdings_core.summarize_snapshot(results)
    assert summary["matched"] == 3 and summary["unmatched"] == 1
    assert summary["unmatched_names"] == ["ANON FUND NOBODY OWNS"]
    assert summary["dates"] == ["2026-03-31"]


def test_a_renamed_fund_matches_through_its_alias(conn, acct):
    """The statement still prints the OLD fund name; the ledger has moved on."""
    _buy(conn, acct, "2026-01-15", "ANON BALANCED FUND II", "100", "20.00")
    conn.execute("INSERT INTO securities(symbol) VALUES (?)",
                 ("ANON BALANCED FUND II",))
    investments.add_alias(conn, BALANCED, "ANON BALANCED FUND II")
    text = "Fund,Shares,Price\nANON BALANCED FUND,123.456,24.19\n"
    results = holdings_core.import_holdings_csv(conn, acct, text,
                                                as_of="2026-03-31")
    assert results[0]["symbol"] == "ANON BALANCED FUND II"
    assert results[0]["matched_by"] == "name"
    assert results[0]["book_qty"] == "100"


def test_match_by_the_securities_description(conn, acct):
    _buy(conn, acct, "2026-01-15", "ANONBAL", "100", "20.00")
    conn.execute("INSERT INTO securities(symbol, name) VALUES (?,?)",
                 ("ANONBAL", BALANCED))
    match = investments.match_snapshot_security(conn, acct, name=BALANCED)
    assert match == {"symbol": "ANONBAL", "matched_by": "name",
                     "name": BALANCED}


def test_a_line_with_no_date_anywhere_is_an_error(conn, acct):
    _seed(conn, acct)
    text = "Fund,Shares,Price\nANON BALANCED FUND,123.456,24.19\n"
    with pytest.raises(ValueError):
        holdings_core.import_holdings_csv(conn, acct, text)


def test_reimporting_the_same_snapshot_is_a_no_op(conn, acct):
    _seed(conn, acct)
    first = holdings_core.import_holdings_file(conn, acct, FIXTURE)
    before = _txn_count(conn)
    again = holdings_core.import_holdings_file(conn, acct, FIXTURE)
    assert _txn_count(conn) == before
    assert [r["symbol"] for r in again] == [r["symbol"] for r in first]
    assert conn.execute(
        "SELECT COUNT(*) FROM price_history WHERE symbol=? AND date=?",
        (BALANCED, "2026-03-31")).fetchone()[0] == 1

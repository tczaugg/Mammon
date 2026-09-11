"""Lifecycle tests for the security alias / ticker-rename model.

A security's SYMBOL is its whole-history identity (price_history and holdings key
on it), so a renamed ticker would otherwise split one security into two. The
``security_aliases`` table (migration 64) records that an old ticker is a former
spelling of a surviving canonical symbol; :func:`investments.resolve_symbol`
follows it, price/holdings/valuation lookups route through it, and the position
replay folds an aliased ticker onto the canonical symbol -- all at READ time, so
no historical row is rewritten. These tests exercise the whole lifecycle:

  * init_db on a fresh database creates the table (created BY the migration);
  * add_alias maps old -> new via resolve_symbol, and list_aliases reports it;
  * a renamed security (history under the old ticker, then aliased to the new)
    is valued as ONE identity across the rename date -- holdings and valuation
    are unchanged by the mere act of adding the alias;
  * two tickers of one renamed security fold into a single canonical holding,
    and the fast paths (rebuild_holdings / list_holdings / get_holding) keep
    working under the canonical symbol;
  * remove_alias reverses the mapping (holdings and prices revert);
  * the guards fire: self-alias, cycle, and a canonical that is not a security.

Pure domain layer -- no Qt, so no offscreen platform is needed.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, investments, ledger, portfolio


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "alias.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)


# ---------------------------------------------------------------------------
# creation
# ---------------------------------------------------------------------------
def test_init_db_creates_security_aliases(conn):
    """The table is created by migration 64, so a fresh init_db has it, and the
    schema version reflects the appended migration."""
    assert "security_aliases" in db.table_names(conn)
    assert db.SCHEMA_VERSION == len(db.MIGRATIONS)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# add / resolve / remove
# ---------------------------------------------------------------------------
def test_add_alias_maps_old_to_new(conn):
    portfolio.set_security(conn, "OLD", name="Old Widget Co")
    portfolio.set_security(conn, "NEW", name="New Widget Co")
    investments.add_alias(conn, "OLD", "NEW")

    assert investments.resolve_symbol(conn, "OLD") == "NEW"      # alias -> canonical
    assert investments.resolve_symbol(conn, "NEW") == "NEW"      # canonical -> itself
    assert investments.resolve_symbol(conn, "OTHER") == "OTHER"  # unknown -> identity
    assert investments.resolve_symbol(conn, "") == ""            # blank -> identity
    assert investments.list_aliases(conn) == [("OLD", "NEW")]


def test_remove_alias_reverses_holdings_and_prices(conn, acct):
    portfolio.set_security(conn, "OLDX", name="Old")
    portfolio.set_security(conn, "NEWX", name="New")
    investments.record_investment(conn, acct, "2020-01-01", "Buy",
                                  symbol="OLDX", quantity="10", price="100.00",
                                  amount=-1000_00)
    investments.record_price(conn, "OLDX", "2020-06-01", "110.00")

    investments.add_alias(conn, "OLDX", "NEWX")
    assert set(investments.compute_holdings(conn, acct, as_of="2020-12-31")) == {"NEWX"}
    # the canonical inherits the old ticker's price series while the alias stands
    assert investments.latest_price(conn, "NEWX", as_of="2020-12-31") == Decimal("110.00")

    investments.remove_alias(conn, "OLDX")
    assert investments.resolve_symbol(conn, "OLDX") == "OLDX"
    assert investments.list_aliases(conn) == []
    # holdings and prices revert to the raw ticker
    assert set(investments.compute_holdings(conn, acct, as_of="2020-12-31")) == {"OLDX"}
    assert investments.latest_price(conn, "OLDX", as_of="2020-12-31") == Decimal("110.00")
    assert investments.latest_price(conn, "NEWX", as_of="2020-12-31") is None


# ---------------------------------------------------------------------------
# continuity: one identity across the rename date
# ---------------------------------------------------------------------------
def test_rename_valued_as_one_identity_across_rename_date(conn, acct):
    """A security whose whole history is under the OLD ticker, then aliased to the
    NEW canonical symbol: adding the alias must not change holdings or valuation
    (it is the SAME security, now under one name), and must attribute the position
    and the price series to the canonical symbol."""
    portfolio.set_security(conn, "OLDX", name="Old Widget Co")
    portfolio.set_security(conn, "NEWX", name="New Widget Co")
    investments.record_investment(conn, acct, "2020-01-01", "Buy",
                                  symbol="OLDX", quantity="10", price="100.00",
                                  amount=-1000_00)
    investments.record_price(conn, "OLDX", "2020-06-01", "110.00")

    as_of = "2020-12-31"
    before = investments.securities_value(conn, acct, as_of=as_of)
    assert before == 1100_00                                   # 10 shares * $110
    holdings_before = investments.compute_holdings(conn, acct, as_of=as_of)
    assert set(holdings_before) == {"OLDX"}
    assert holdings_before["OLDX"].qty == Decimal("10")

    investments.add_alias(conn, "OLDX", "NEWX")

    after = investments.securities_value(conn, acct, as_of=as_of)
    assert after == before                                     # ONE identity, unchanged

    holdings_after = investments.compute_holdings(conn, acct, as_of=as_of)
    assert set(holdings_after) == {"NEWX"}                      # attributes to canonical
    assert holdings_after["NEWX"].qty == Decimal("10")
    # the canonical symbol now finds the pre-rename price series
    assert investments.latest_price(conn, "NEWX", as_of=as_of) == Decimal("110.00")


def test_alias_folds_both_tickers_and_keeps_fast_paths(conn, acct):
    """Shares bought under BOTH the old and new tickers of one renamed security
    pool into a single canonical holding, and rebuild_holdings / list_holdings /
    get_holding (the fast paths) all present it under the canonical symbol."""
    portfolio.set_security(conn, "OLDY", name="Y one")
    portfolio.set_security(conn, "NEWY", name="Y two")
    investments.record_investment(conn, acct, "2019-01-01", "Buy",
                                  symbol="OLDY", quantity="10", price="50.00",
                                  amount=-500_00)
    investments.record_investment(conn, acct, "2021-02-01", "Buy",
                                  symbol="NEWY", quantity="5", price="50.00",
                                  amount=-250_00)
    investments.record_price(conn, "NEWY", "2021-06-01", "50.00")
    investments.add_alias(conn, "OLDY", "NEWY")

    # replay path folds the two spellings into one canonical position
    holdings = investments.compute_holdings(conn, acct)
    assert set(holdings) == {"NEWY"}
    assert holdings["NEWY"].qty == Decimal("15")

    # fast path: rebuild writes ONE canonical row; list_holdings reads it
    rows = investments.rebuild_holdings(conn, acct)
    assert [r["symbol"] for r in rows] == ["NEWY"]
    assert rows[0]["quantity"] == "15"
    assert [r["symbol"] for r in investments.list_holdings(conn, acct)] == ["NEWY"]

    # valuation through the holdings-table fast path (as_of=None) prices via identity
    assert investments.securities_value(conn, acct) == 750_00   # 15 shares * $50

    # get_holding resolves a renamed ticker to the canonical holding row
    assert investments.get_holding(conn, acct, "OLDY")["symbol"] == "NEWY"


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------
def test_self_alias_rejected(conn):
    portfolio.set_security(conn, "AAA", name="A")
    with pytest.raises(ValueError):
        investments.add_alias(conn, "AAA", "AAA")
    assert investments.list_aliases(conn) == []


def test_cycle_rejected(conn):
    portfolio.set_security(conn, "AAA", name="A")
    portfolio.set_security(conn, "BBB", name="B")
    investments.add_alias(conn, "AAA", "BBB")           # AAA -> BBB
    with pytest.raises(ValueError):
        investments.add_alias(conn, "BBB", "AAA")       # would close a cycle
    # the good alias survives; resolution is unambiguous
    assert investments.resolve_symbol(conn, "AAA") == "BBB"
    assert investments.list_aliases(conn) == [("AAA", "BBB")]


def test_canonical_must_be_a_security(conn):
    portfolio.set_security(conn, "AAA", name="A")
    with pytest.raises(ValueError):
        investments.add_alias(conn, "AAA", "GHOST")     # GHOST is not a security
    assert investments.list_aliases(conn) == []

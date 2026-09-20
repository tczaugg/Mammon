"""Currency-aware net-worth presentation (SRD 5.4a).

The user asked for net worth laid out as: each currency's NATIVE subtotal, then
its conversion into USD, then a grand total in USD equal to the sum of the
conversions (ask f67de76e -- "each currency listed, then the conversion, then
the total in USD"). :func:`mammon.fx.net_worth_currencies` builds that object and
:func:`mammon.ledger.net_worth` (hence the whole reports/charts stack) folds
through it.

The load-bearing rule these tests pin: a NON-ZERO foreign balance with no
recorded FX rate is NEVER added into the USD total at 1:1. It is surfaced as an
explicit unconverted line so the total is honestly incomplete rather than
silently overstated by the raw foreign number. A ZERO foreign balance converts
free and needs no rate. A single-currency (all-USD) ledger is byte-for-byte the
naive base sum.

Synthetic data only -- no real account names, numbers, or amounts.
"""
from __future__ import annotations

from mammon import db, fx, investments, ledger
from mammon.reports import charts

import pytest
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "nwc.db")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# (1) USD + foreign WITH a rate: native subtotals, conversions, grand total
# ---------------------------------------------------------------------------
def test_each_currency_native_conversion_and_grand_total(conn):
    ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    ledger.create_account(conn, "Euro Cash", "cash",
                          opening_balance=50_00, currency="EUR")
    fx.set_rate(conn, "2026-01-01", "EUR", "USD", "1.10")

    nwc = fx.net_worth_currencies(conn)
    assert nwc.target == "USD"
    # base currency leads, then the rest A->Z
    assert [ln.currency for ln in nwc.lines] == ["USD", "EUR"]

    by = {ln.currency: ln for ln in nwc.lines}
    # native subtotals are the account balances in their OWN currency
    assert by["USD"].native_cents == 100_00
    assert by["EUR"].native_cents == 50_00
    # the conversion column: USD is the identity, EUR * 1.10
    assert by["USD"].converted_cents == 100_00
    assert by["EUR"].converted_cents == 55_00
    assert not by["EUR"].rate_missing

    # grand total == sum of the conversions, and nothing else
    assert nwc.total_cents == 100_00 + 55_00 == 155_00
    assert nwc.total_cents == sum(ln.converted_cents for ln in nwc.lines)
    assert nwc.is_complete and nwc.unconverted == []

    # the ledger + report entry points agree with the presentation object
    assert ledger.net_worth(conn) == 155_00


# ---------------------------------------------------------------------------
# (2) non-zero foreign with NO rate: shown unconverted, NOT added at 1:1
# ---------------------------------------------------------------------------
def test_missing_rate_is_unconverted_not_folded(conn):
    ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    ledger.create_account(conn, "Euro Cash", "cash",
                          opening_balance=50_00, currency="EUR")
    # no EUR->USD rate recorded

    nwc = fx.net_worth_currencies(conn)
    by = {ln.currency: ln for ln in nwc.lines}
    assert by["EUR"].native_cents == 50_00
    assert by["EUR"].converted_cents is None        # left blank, not 1:1
    assert by["EUR"].rate_missing

    # the total is the honest USD-only figure -- the 50.00 EUR is NOT in it
    assert nwc.total_cents == 100_00
    assert nwc.total_cents != 150_00                # the 1:1-folded wrong answer
    assert nwc.unconverted == ["EUR"]
    assert not nwc.is_complete

    assert ledger.net_worth(conn) == 100_00


def test_zero_foreign_balance_needs_no_rate(conn):
    # a zero foreign balance converts free (convert_cents guards it) -- it is a
    # clean converted line, not an unconverted one, even with no rate on file.
    ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    ledger.create_account(conn, "Empty EUR", "cash", opening_balance=0,
                          currency="EUR")
    nwc = fx.net_worth_currencies(conn)
    by = {ln.currency: ln for ln in nwc.lines}
    assert by["EUR"].native_cents == 0
    assert by["EUR"].converted_cents == 0
    assert nwc.is_complete
    assert nwc.total_cents == 100_00


# ---------------------------------------------------------------------------
# (3) USD-only ledger: total unchanged, single identity line, fast path
# ---------------------------------------------------------------------------
def test_usd_only_total_unchanged(conn):
    ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    ledger.create_account(conn, "US Savings", "savings", opening_balance=250_00)

    # ledger.net_worth takes the single-currency fast path == investments.net_worth
    assert ledger.net_worth(conn) == investments.net_worth(conn) == 350_00

    nwc = fx.net_worth_currencies(conn)
    assert [ln.currency for ln in nwc.lines] == ["USD"]
    assert nwc.lines[0].converted_cents == 350_00   # identity conversion
    assert nwc.total_cents == 350_00
    assert nwc.is_complete


# ---------------------------------------------------------------------------
# (4) net_worth_series over time is currency-aware for each of the above
# ---------------------------------------------------------------------------
def test_net_worth_series_is_currency_aware(conn):
    us = ledger.create_account(conn, "US Checking", "checking")
    eu = ledger.create_account(conn, "Euro Cash", "cash", currency="EUR")
    # money arrives over time in both accounts
    ledger.add_transaction(conn, us, "2026-01-01", 100_00)
    ledger.add_transaction(conn, eu, "2026-01-01", 50_00)
    ledger.add_transaction(conn, us, "2026-06-01", 100_00)   # +100 USD
    ledger.add_transaction(conn, eu, "2026-06-01", 50_00)    # +50 EUR
    # a rate that holds across the whole window
    fx.set_rate(conn, "2026-01-01", "EUR", "USD", "1.10")

    series = charts.net_worth_series(conn, start="2026-01-01", end="2026-06-01",
                                     points=2)
    pts = {p.date: p.cents for p in series.points}
    # first sample: 100 USD + 50 EUR*1.10 = 155.00
    assert pts["2026-01-01"] == 100_00 + 55_00
    # last sample: 200 USD + 100 EUR*1.10 = 310.00
    assert pts["2026-06-01"] == 200_00 + 110_00


def test_net_worth_series_excludes_foreign_without_rate(conn):
    us = ledger.create_account(conn, "US Checking", "checking")
    eu = ledger.create_account(conn, "Euro Cash", "cash", currency="EUR")
    ledger.add_transaction(conn, us, "2026-01-01", 100_00)
    ledger.add_transaction(conn, eu, "2026-01-01", 50_00)
    ledger.add_transaction(conn, us, "2026-06-01", 100_00)
    # no EUR->USD rate: the EUR balance is left out of every sample, not folded 1:1
    series = charts.net_worth_series(conn, start="2026-01-01", end="2026-06-01",
                                     points=2)
    pts = {p.date: p.cents for p in series.points}
    assert pts["2026-01-01"] == 100_00
    assert pts["2026-06-01"] == 200_00

"""Tests for mammon.fx: the _V47 fx_rates schema, per-account native currency,
the Decimal-text FX-rate store (direct + derived inverse, as-of lookup),
HALF_UP cents conversion, net worth grouped by currency, and fetch_rates against
an injected fake source (no network -- the same seam as the investment quotes).

Synthetic data only: no real account numbers, names, or amounts.
"""
from __future__ import annotations

import importlib.util
from decimal import Decimal

import pytest

from mammon import db, fx, ledger
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "fx.db")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# fake FX source (mirrors test_investments._FakeSource)
# ---------------------------------------------------------------------------
class _FakeFxSource:
    source_name = "fake"

    def __init__(self, rates):
        self._rates = rates
        self.asked = None

    def get_rates(self, pairs):
        self.asked = list(pairs)
        return self._rates


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
def test_migration_creates_fx_rates(conn):
    info = list(conn.execute("PRAGMA table_info(fx_rates)"))
    cols = {r["name"] for r in info}
    assert cols == {"date", "base", "quote", "rate"}
    # composite primary key on (date, base, quote); rate is not part of the key
    pk = {r["name"]: r["pk"] for r in info}
    assert pk["date"] and pk["base"] and pk["quote"]
    assert not pk["rate"]


def test_accounts_have_currency_column(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    assert "currency" in cols


def test_init_db_idempotent(tmp_path):
    p = tmp_path / "idem.db"
    c1 = fresh_db(p)
    v1 = c1.execute("PRAGMA user_version").fetchone()[0]
    c1.close()
    # re-opening applies no further migrations and does not raise
    c2 = fresh_db(p)
    v2 = c2.execute("PRAGMA user_version").fetchone()[0]
    c2.close()
    assert v1 == v2 == db.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# per-account native currency
# ---------------------------------------------------------------------------
def test_account_currency_defaults_to_usd(conn):
    a = ledger.create_account(conn, "Checking", "checking")
    assert fx.get_account_currency(conn, a) == "USD"


def test_set_and_get_account_currency(conn):
    a = ledger.create_account(conn, "Euro Cash", "cash")
    fx.set_account_currency(conn, a, "eur")           # normalized to upper-case
    assert fx.get_account_currency(conn, a) == "EUR"


def test_blank_currency_resets_to_base(conn):
    a = ledger.create_account(conn, "Reset Me", "cash")
    fx.set_account_currency(conn, a, "GBP")
    fx.set_account_currency(conn, a, "")              # blank -> base
    assert fx.get_account_currency(conn, a) == "USD"


def test_get_currency_unknown_account_raises(conn):
    with pytest.raises(ValueError):
        fx.get_account_currency(conn, 999999)


# ---------------------------------------------------------------------------
# rate store: Decimal text, direct, inverse, as-of
# ---------------------------------------------------------------------------
def test_rate_stored_as_decimal_text_not_float(conn):
    fx.set_rate(conn, "2026-01-01", "USD", "EUR", "0.8")
    raw = conn.execute(
        "SELECT rate FROM fx_rates WHERE base='USD' AND quote='EUR'"
    ).fetchone()["rate"]
    assert isinstance(raw, str)
    assert raw == "0.8"


def test_rate_text_is_exponent_free(conn):
    fx.set_rate(conn, "2026-01-01", "USD", "CLP", 100)   # 100 CLP per USD
    raw = conn.execute(
        "SELECT rate FROM fx_rates WHERE base='USD' AND quote='CLP'"
    ).fetchone()["rate"]
    assert raw == "100"                                  # not '1E+2'


def test_set_rate_upserts(conn):
    fx.set_rate(conn, "2026-01-01", "USD", "EUR", "0.80")
    fx.set_rate(conn, "2026-01-01", "USD", "EUR", "0.85")
    n = conn.execute(
        "SELECT COUNT(*) c FROM fx_rates WHERE base='USD' AND quote='EUR'"
    ).fetchone()["c"]
    assert n == 1
    assert fx.get_rate(conn, "USD", "EUR", "2026-01-01") == Decimal("0.85")


def test_get_rate_same_currency_is_one(conn):
    assert fx.get_rate(conn, "USD", "USD") == Decimal(1)
    assert fx.get_rate(conn, "eur", "EUR") == Decimal(1)


def test_get_rate_direct(conn):
    fx.set_rate(conn, "2026-01-01", "USD", "EUR", "0.9")
    assert fx.get_rate(conn, "USD", "EUR") == Decimal("0.9")


def test_get_rate_derives_inverse(conn):
    fx.set_rate(conn, "2026-01-01", "USD", "EUR", "0.8")
    # only USD->EUR recorded; EUR->USD is derived as 1 / 0.8 = 1.25
    assert fx.get_rate(conn, "EUR", "USD") == Decimal("1.25")


def test_get_rate_missing_is_none(conn):
    assert fx.get_rate(conn, "USD", "JPY") is None


def test_get_rate_as_of_picks_most_recent_on_or_before(conn):
    fx.set_rate(conn, "2026-01-01", "USD", "EUR", "0.80")
    fx.set_rate(conn, "2026-06-01", "USD", "EUR", "0.90")
    assert fx.get_rate(conn, "USD", "EUR", "2026-03-01") == Decimal("0.80")
    assert fx.get_rate(conn, "USD", "EUR", "2026-07-01") == Decimal("0.90")
    assert fx.get_rate(conn, "USD", "EUR", "2025-12-01") is None   # nothing before
    assert fx.get_rate(conn, "USD", "EUR") == Decimal("0.90")      # latest


# ---------------------------------------------------------------------------
# convert_cents: HALF_UP at the cents boundary
# ---------------------------------------------------------------------------
def test_convert_same_currency_is_identity(conn):
    assert fx.convert_cents(conn, -123_45, "USD", "USD") == -123_45


def test_convert_direct_rounds_half_up(conn):
    fx.set_rate(conn, "2026-01-01", "USD", "EUR", "0.5")
    # 12345 * 0.5 = 6172.5 -> HALF_UP -> 6173
    assert fx.convert_cents(conn, 123_45, "USD", "EUR") == 6173


def test_convert_via_inverse(conn):
    fx.set_rate(conn, "2026-01-01", "USD", "EUR", "0.8")   # inverse 1.25
    # 100.00 EUR -> USD at 1.25 = 125.00
    assert fx.convert_cents(conn, 100_00, "EUR", "USD") == 125_00


def test_convert_preserves_sign(conn):
    fx.set_rate(conn, "2026-01-01", "USD", "EUR", "0.8")
    assert fx.convert_cents(conn, -100_00, "USD", "EUR") == -80_00


def test_convert_missing_rate_raises(conn):
    with pytest.raises(fx.FxRateUnavailable):
        fx.convert_cents(conn, 100_00, "USD", "JPY")


def test_convert_zero_needs_no_rate(conn):
    # Zero converts to zero at any rate: short-circuit BEFORE the lookup so an
    # empty foreign account never demands an FX rate. fx_rates is empty here.
    assert fx.convert_cents(conn, 0, "CAD", "USD") == 0


# ---------------------------------------------------------------------------
# net worth by currency + optional FX total
# ---------------------------------------------------------------------------
def test_net_worth_by_currency_groups_balances(conn):
    usd = ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    eur = ledger.create_account(conn, "EU Cash", "cash", opening_balance=50_00)
    fx.set_account_currency(conn, eur, "EUR")
    assert fx.net_worth_by_currency(conn) == {"USD": 100_00, "EUR": 50_00}


def test_net_worth_by_currency_excludes_hidden_includes_closed(conn):
    usd = ledger.create_account(conn, "Open USD", "checking", opening_balance=100_00)
    closed = ledger.create_account(conn, "Closed USD", "checking", opening_balance=25_00)
    hidden = ledger.create_account(conn, "Hidden EUR", "cash", opening_balance=50_00)
    fx.set_account_currency(conn, hidden, "EUR")
    ledger.update_account(conn, closed, closed_flag=1)
    ledger.set_account_hidden(conn, hidden, True)
    # closed account still counts; hidden EUR account drops out entirely
    assert fx.net_worth_by_currency(conn) == {"USD": 125_00}
    # ...but can be opted back in
    assert fx.net_worth_by_currency(conn, include_hidden=True) == {
        "USD": 125_00, "EUR": 50_00}


def test_total_in_currency_folds_through_fx(conn):
    usd = ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    eur = ledger.create_account(conn, "EU Cash", "cash", opening_balance=50_00)
    fx.set_account_currency(conn, eur, "EUR")
    fx.set_rate(conn, "2026-01-01", "USD", "EUR", "0.8")   # EUR->USD = 1.25
    # 100.00 USD + (50.00 EUR -> 62.50 USD) = 162.50 USD
    assert fx.total_in_currency(conn, "USD") == 162_50


def test_total_in_currency_missing_rate_raises(conn):
    # REGRESSION: a NON-zero foreign balance with no recorded rate must still
    # raise -- the zero-guard must not soften the honest failure for real money.
    ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    eur = ledger.create_account(conn, "EU Cash", "cash", opening_balance=50_00)
    fx.set_account_currency(conn, eur, "EUR")
    with pytest.raises(fx.FxRateUnavailable):
        fx.total_in_currency(conn, "USD")


def test_total_in_currency_zero_foreign_balance_needs_no_rate(conn):
    # A foreign account with an exactly-zero balance and no rows in fx_rates must
    # not sink the whole net-worth total: its bucket converts to zero for free.
    ledger.create_account(conn, "US Checking", "checking", opening_balance=100_00)
    cad = ledger.create_account(conn, "CAD Cash", "cash", opening_balance=0)
    fx.set_account_currency(conn, cad, "CAD")
    assert fx.get_rate(conn, "CAD", "USD") is None      # no recorded rate
    assert fx.total_in_currency(conn, "USD") == 100_00


# ---------------------------------------------------------------------------
# fetch_rates against an injected fake source (no network)
# ---------------------------------------------------------------------------
def test_fetch_rates_writes_and_dedups(conn):
    src = _FakeFxSource([
        fx.FxRate("2026-08-07", "EUR", "USD", "1.10", "fake"),
        fx.FxRate("2026-08-07", "GBP", "USD", "1.27", "fake"),
    ])
    written = fx.fetch_rates(
        conn,
        [("EUR", "USD"), ("EUR", "USD"), ("usd", "USD"), ("GBP", "USD")],
        source=src,
    )
    # same-currency dropped, duplicate collapsed, order preserved
    assert src.asked == [("EUR", "USD"), ("GBP", "USD")]
    assert len(written) == 2
    assert fx.get_rate(conn, "EUR", "USD") == Decimal("1.10")
    assert fx.get_rate(conn, "GBP", "USD") == Decimal("1.27")


def test_fetch_rates_no_pairs_is_noop(conn):
    # only same-currency requested -> returns [] without touching the source
    assert fx.fetch_rates(conn, [("USD", "USD")], source=_FakeFxSource([])) == []


def test_default_fx_source_depends_on_yfinance():
    have_yf = importlib.util.find_spec("yfinance") is not None
    if have_yf:
        assert isinstance(fx.default_fx_source(), fx.YFinanceFxSource)
    else:
        with pytest.raises(fx.FxRateUnavailable):
            fx.default_fx_source()

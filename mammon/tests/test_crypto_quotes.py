"""Crypto price quotes, valuation refresh, register-sourced price history, and
the read-only MCP crypto-holdings surface (upgrade_priorities #14).

The crypto quote path reuses the SAME plumbing securities have -- a bare coin
symbol maps to its ``'{SYM}-USD'`` yfinance pair and prices land in the shared
``price_history`` table under that pair (see mammon.crypto.pair_symbol) -- so
these tests exercise the crypto layer directly with an INJECTED fake quote
source and never touch the network:

  * a CURRENT quote refresh moves a coin's valuation off "unpriced",
  * a HISTORICAL quote download backfills the coin's price series,
  * a source with no ``get_history`` is refused rather than silently empty,
  * the register itself round-trips as price history (each buy/reward carries
    its own per-unit USD price, extracted into ``price_history``), and
  * mcp_tools.crypto_holdings reports positions in decimal dollars WITHOUT
    leaking the wallet address (stored in the same protected column as an
    account number).

Synthetic data only -- no real wallet addresses, tx hashes, amounts or PII.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from mammon import crypto, db, mcp_tools
from mammon.investments import Quote
from mammon.tests import fresh_db

# A synthetic, obviously-fake wallet address (no PII) -- stored in the protected
# account_number column, so it must never appear in the MCP surface.
WALLET = "0xTESTWALLET0000000000000000000000000000FAKE"


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "crypto_quotes.db")
    yield c
    c.close()


class _FakeQuotes:
    """The injected quote seam. Returns Quotes ALREADY keyed by the '{SYM}-USD'
    pair -- exactly what CryptoQuoteSource yields -- so crypto.fetch_quotes /
    fetch_quote_history store them under the pair without remapping. Records the
    symbols it was asked for so the test can confirm it is handed BARE symbols."""

    source_name = "fake"

    def __init__(self, latest=None, history=None):
        self._latest = list(latest or [])
        self._history = list(history or [])
        self.asked = None
        self.asked_history = None

    def get_quotes(self, symbols):
        self.asked = list(symbols)
        return list(self._latest)

    def get_history(self, symbols, *, start=None, end=None, interval="1mo"):
        self.asked_history = (list(symbols), start, end, interval)
        return list(self._history)


class _LatestOnly:
    """A source that only reports the latest close -- no historical support."""

    source_name = "fake"

    def get_quotes(self, symbols):
        return []


# ---------------------------------------------------------------------------
# current-quote refresh updates valuation
# ---------------------------------------------------------------------------
def test_current_quote_refresh_updates_a_coins_valuation(conn):
    acct = crypto.create_account(conn, "Exchange", opening_balance=0)
    crypto.record_buy(conn, acct, "2024-03-01", "BTC", "1", 30_000_00)
    crypto.rebuild_holdings(conn, acct)

    # Before any quote the coin is UNPRICED: valued at 0, listed under `unpriced`,
    # so the only thing moving the total is the -$30,000 cash sleeve.
    before = crypto.account_valuation(conn, acct, "2024-03-01")
    assert before.unpriced == ["BTC"]
    assert before.securities == 0
    assert before.total == -30_000_00

    # A fetched current quote updates the recorded price, so valuation moves.
    fake = _FakeQuotes(latest=[Quote("BTC-USD", "2024-03-01", "50000", "fake")])
    got = crypto.fetch_quotes(conn, ["BTC"], source=fake)
    assert fake.asked == ["BTC"]                        # handed BARE symbols
    assert [q.symbol for q in got] == ["BTC-USD"]       # stored under the pair

    after = crypto.account_valuation(conn, acct, "2024-03-01")
    assert after.unpriced == []
    assert after.securities == 50_000_00                # 1 BTC * $50,000
    assert after.total == 20_000_00                     # 50,000 value - 30,000 cash
    assert crypto.latest_price(conn, "BTC", "2024-03-01") == Decimal("50000")


# ---------------------------------------------------------------------------
# historical-quote download populates the crypto price series
# ---------------------------------------------------------------------------
def test_historical_quote_download_populates_price_history(conn):
    acct = crypto.create_account(conn, "Exchange", opening_balance=0)
    crypto.record_buy(conn, acct, "2024-01-10", "ETH", "10", 20_000_00)
    crypto.rebuild_holdings(conn, acct)

    hist = [Quote("ETH-USD", "2024-01-31", "2000", "fake"),
            Quote("ETH-USD", "2024-02-29", "2500", "fake"),
            Quote("ETH-USD", "2024-03-31", "3000", "fake")]
    fake = _FakeQuotes(history=hist)
    n = crypto.fetch_quote_history(conn, ["ETH"], start="2024-01-01", source=fake)
    assert n == 3
    assert fake.asked_history[0] == ["ETH"]             # handed BARE symbols
    assert fake.asked_history[1] == "2024-01-01"        # start threaded through

    # The whole series round-trips out of price_history, ascending, as Decimals.
    assert crypto.price_history(conn, "ETH") == [
        ("2024-01-31", Decimal("2000")),
        ("2024-02-29", Decimal("2500")),
        ("2024-03-31", Decimal("3000"))]

    # Valuation uses the latest close on/before the as-of date.
    assert crypto.latest_price(conn, "ETH", "2024-02-15") == Decimal("2000")
    v = crypto.account_valuation(conn, acct, "2024-02-15")
    assert v.securities == 20_000_00                    # 10 ETH * $2,000 (Jan close)


def test_history_source_without_get_history_is_refused(conn):
    """A source that reports only the latest close raises rather than returning an
    empty series a caller could mistake for 'no prices exist'."""
    with pytest.raises(crypto.QuoteSourceUnavailable):
        crypto.fetch_quote_history(conn, ["BTC"], source=_LatestOnly())


# ---------------------------------------------------------------------------
# register-sourced price history round-trips
# ---------------------------------------------------------------------------
def test_register_sourced_prices_round_trip(conn):
    acct = crypto.create_account(conn, "Exchange", opening_balance=0)
    # A buy and an in-kind reward each carry their own per-unit USD price, filled
    # from the fiat leg by the wrappers.
    crypto.record_buy(conn, acct, "2024-02-01", "BTC", "1", 42_000_00)          # $42,000
    crypto.record_income(conn, acct, "2024-02-15", "REWARD", "BTC",
                         "0.01", 500_00)                                        # $50,000
    crypto.rebuild_holdings(conn, acct)

    # The prices live only on the register rows so far.
    assert crypto.price_history(conn, "BTC") == []

    written = crypto.learn_prices_from_transactions(conn, acct)
    assert written == 2
    assert crypto.latest_price(conn, "BTC", "2024-02-01") == Decimal("42000")
    assert crypto.latest_price(conn, "BTC", "2024-02-15") == Decimal("50000")
    assert crypto.price_history(conn, "BTC") == [
        ("2024-02-01", Decimal("42000")),
        ("2024-02-15", Decimal("50000"))]

    # A re-run writes nothing new (DO-NOTHING precedence for a txn price).
    assert crypto.learn_prices_from_transactions(conn, acct) == 0

    # A raw record_event row with a value + quantity but no price DERIVES it.
    crypto.record_event(conn, acct, "2024-03-01", "BUY", symbol="BTC",
                        quantity="2", amount=-90_000_00)
    tid = conn.execute("SELECT id FROM crypto_transactions WHERE date='2024-03-01'"
                       ).fetchone()[0]
    assert crypto.learn_prices_from_transactions(conn, txn_id=tid) == 1
    assert crypto.latest_price(conn, "BTC", "2024-03-01") == Decimal("45000")

    # A freshly fetched CURRENT quote (an upsert) supersedes a register price.
    crypto.fetch_quotes(conn, ["BTC"], source=_FakeQuotes(
        latest=[Quote("BTC-USD", "2024-02-01", "39000", "fake")]))
    assert crypto.latest_price(conn, "BTC", "2024-02-01") == Decimal("39000")


# ---------------------------------------------------------------------------
# the read-only MCP crypto-holdings tool
# ---------------------------------------------------------------------------
def test_mcp_crypto_holdings_reports_without_leaking_restricted_columns(conn):
    acct = crypto.create_account(conn, "Ledger Nano", opening_balance=0,
                                 wallet_address=WALLET)
    crypto.record_buy(conn, acct, "2024-02-01", "BTC", "2", 60_000_00)
    crypto.rebuild_holdings(conn, acct)
    crypto.fetch_quotes(conn, ["BTC"], source=_FakeQuotes(
        latest=[Quote("BTC-USD", "2024-02-01", "40000", "fake")]))

    out = mcp_tools.crypto_holdings(conn, "Ledger Nano", as_of="2024-02-01")

    # Shape mirrors the securities `holdings` tool; money is decimal dollars,
    # quantities/prices exact decimal strings.
    assert out["account"] == "Ledger Nano" and out["as_of"] == "2024-02-01"
    assert out["cash"] == "-60000.00"
    assert out["securities"] == "80000.00"              # 2 BTC * $40,000
    assert out["total"] == "20000.00"
    assert out["unpriced"] == []
    assert out["holdings"] == [{
        "symbol": "BTC", "quantity": "2", "cost_basis": "60000.00",
        "price": "40000", "market_value": "80000.00", "gain": "20000.00"}]

    # Everything is JSON-serializable and the wallet address (which lives in the
    # protected account_number column) never leaves, in any field or key.
    text = json.dumps(out)
    assert WALLET not in text
    assert "account_number" not in out
    assert "url" not in out and "download_config" not in out


def test_mcp_crypto_holdings_lists_an_unpriced_coin(conn):
    """A coin with no recorded quote is reported under `unpriced`, valued at 0,
    rather than guessed at -- the same contract the securities tool honours."""
    acct = crypto.create_account(conn, "Exchange", opening_balance=0)
    crypto.record_buy(conn, acct, "2024-02-01", "SOL", "5", 500_00)
    crypto.rebuild_holdings(conn, acct)

    out = mcp_tools.crypto_holdings(conn, "Exchange")
    assert out["unpriced"] == ["SOL"]
    assert out["holdings"][0]["price"] is None
    assert out["holdings"][0]["market_value"] == "0.00"
    assert out["securities"] == "0.00"

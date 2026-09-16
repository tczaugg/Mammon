"""What a security's KIND alone settles: never-quote and pinned price (SRD 5.8e-2c).

Two instrument kinds need no market data at all, and asking for it is worse
than useless:

  * a TICKERLESS PLAN FUND (``kind='mutual_fund'`` with no resolvable ticker) --
    a 401(k)/529 internal fund like "INTL EQUITY INDEX" that no provider lists.
    ``fetch_ticker`` rule 3 already refuses to GUESS a ticker from its name
    (INTL is a real listed company), but the kind makes the protection explicit
    and lets the quote path skip the row outright instead of trying and failing.
  * a MONEY-MARKET SWEEP (``kind='money_market'``) -- priced at 1.00 by
    construction. A downloaded 0.9998 is noise that makes a cash sleeve drift.

Everything here is synthetic. A row whose kind is still NULL is UNCLASSIFIED,
not equity, and must behave exactly as it did before any of this existed; that
is asserted too, because "protect the classified rows" is only safe if it
changes nothing for the ones nobody has classified.
"""
from decimal import Decimal

import pytest

from mammon import db, instruments, investments, ledger, portfolio, securities
from mammon.investments import Quote

EQUITY = "ZZZT"                    # an ordinary listed stock, kind left NULL
PLAN = "INTL EQUITY INDEX"         # tickerless plan fund, kind='mutual_fund'
SWEEP = "SWEEP MONEY MARKET"       # cash sweep, kind='money_market'
DATE = "2024-01-02"


class RecordingSource:
    """A quote provider that fetches nothing and remembers being asked.

    The whole point of a never-quote kind is that the provider is never
    reached, so the assertion these tests make is on ``calls`` being EMPTY --
    a source that returned plausible prices would hide the failure.
    """

    source_name = "recording"

    def __init__(self):
        self.calls = []

    def get_quotes(self, symbols):
        self.calls.append(list(symbols))
        return [Quote(symbol=s, date=DATE, close="99.99", source="recording")
                for s in symbols]

    def get_history(self, symbols, start=None, end=None, interval="1mo"):
        self.calls.append(list(symbols))
        return {s: [(DATE, "99.99")] for s in symbols}


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "kinds.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Brokerage", "investment",
                                 opening_balance=0)


def _security(conn, symbol, name=None, asset_class=None, ticker=None,
              kind=None):
    """One synthetic securities row, classified through the public writer."""
    conn.execute(
        "INSERT INTO securities(symbol, name, sec_type, asset_class, ticker)"
        " VALUES (?,?,?,?,?)",
        (symbol, name or symbol, "fund", asset_class, ticker))
    conn.commit()
    if kind is not None:
        securities.set_kinds(conn, [{"symbol": symbol, "kind": kind,
                                     "kind_source": "user"}])


def _buy(conn, account_id, symbol, qty, price):
    """A Buy of ``qty`` at ``price``; cash out is qty*price in cents."""
    amount = -int((Decimal(qty) * Decimal(price) * 100).to_integral_value())
    investments.record_investment(conn, account_id, DATE, "Buy", symbol=symbol,
                                  quantity=str(qty), price=str(price),
                                  amount=amount)
    investments.rebuild_holdings(conn, account_id)


# ---------------------------------------------------------------------------
# 1. A tickerless plan fund never reaches the quote path
# ---------------------------------------------------------------------------
def test_tickerless_plan_fund_is_never_quoted(conn):
    _security(conn, PLAN, kind=instruments.Kind.MUTUAL_FUND.value)
    src = RecordingSource()

    assert securities.never_quote(conn, PLAN) is True
    assert investments.fetch_quotes(conn, [PLAN], source=src) == []
    assert src.calls == []


def test_plan_fund_is_dropped_even_when_a_ticker_is_offered(conn):
    """The holding arrives as ticker -> name, which is how it slipped through:
    "INTL" looks perfectly quotable until you ask what it is a ticker FOR."""
    _security(conn, PLAN, kind=instruments.Kind.MUTUAL_FUND.value)
    src = RecordingSource()

    assert investments.fetch_quotes(conn, ["INTL"], source=src,
                                    names={"INTL": [PLAN]}) == []
    assert src.calls == []
    assert investments.fetch_quote_history(conn, [(PLAN, "INTL")],
                                           source=src) == 0
    assert src.calls == []


def test_quotable_neighbours_still_go_out(conn):
    """Skipping is per-security: the equity beside it is fetched as always."""
    _security(conn, PLAN, kind=instruments.Kind.MUTUAL_FUND.value)
    _security(conn, EQUITY)
    src = RecordingSource()

    quotes = investments.fetch_quotes(conn, [EQUITY, PLAN], source=src)

    assert [q.symbol for q in quotes] == [EQUITY]
    assert src.calls == [[EQUITY]]


def test_null_kind_keeps_exactly_the_old_rule_3_behavior(conn):
    """An UNCLASSIFIED multi-token name is still offered to the provider. Rule 3
    refuses to guess a TICKER for it; that is a different question, and this
    change must not quietly start dropping rows nobody has classified."""
    _security(conn, PLAN)   # no kind at all

    assert investments.security_kind(conn, PLAN) is None
    assert securities.never_quote(conn, PLAN) is False
    assert securities.fetch_ticker(conn, PLAN) is None

    src = RecordingSource()
    investments.fetch_quotes(conn, [PLAN], source=src)
    assert src.calls == [[PLAN]]


def test_a_mutual_fund_with_a_real_ticker_is_still_quoted(conn):
    """kind='mutual_fund' is not itself a reason to skip -- a retail fund has a
    ticker and a daily NAV. It is the ABSENCE of a ticker that makes it a plan
    fund, so the guard asks ``fetch_ticker`` rather than the kind alone."""
    _security(conn, "FIPDX", name="Fidelity Freedom Index 2035",
              kind=instruments.Kind.MUTUAL_FUND.value)
    src = RecordingSource()

    assert securities.never_quote(conn, "FIPDX") is False
    assert [q.symbol for q in
            investments.fetch_quotes(conn, ["FIPDX"], source=src)] == ["FIPDX"]
    assert src.calls == [["FIPDX"]]


# ---------------------------------------------------------------------------
# 2. A money-market sweep values at exactly 1 per unit, unquoted
# ---------------------------------------------------------------------------
def test_money_market_values_at_one_per_unit(conn, acct):
    _security(conn, SWEEP, kind=instruments.Kind.MONEY_MARKET.value)
    _buy(conn, acct, SWEEP, "5000", "1")

    (hv,) = investments.holding_values(conn, acct)

    assert hv.symbol == SWEEP
    assert hv.price == Decimal(1)
    assert isinstance(hv.price, Decimal)      # never a float: SRD 5.8e-2c
    assert hv.market_value == 500000          # 5,000 units -> $5,000.00


def test_pinned_price_beats_a_recorded_or_injected_price(conn, acct):
    """A downloaded 0.9998, or a caller's override, is exactly the drift the pin
    exists to stop -- so the pin is applied FIRST, ahead of both."""
    _security(conn, SWEEP, kind=instruments.Kind.MONEY_MARKET.value)
    _buy(conn, acct, SWEEP, "5000", "1")
    investments.record_price(conn, SWEEP, DATE, "0.9998", "yfinance")

    (hv,) = investments.holding_values(conn, acct)
    assert hv.price == Decimal(1)
    assert hv.market_value == 500000

    (hv,) = investments.holding_values(conn, acct,
                                       prices={SWEEP: Decimal("2")})
    assert hv.price == Decimal(1)
    assert hv.market_value == 500000


def test_money_market_is_never_quoted(conn):
    _security(conn, SWEEP, kind=instruments.Kind.MONEY_MARKET.value)
    src = RecordingSource()

    assert investments.is_money_market(conn, SWEEP) is True
    assert securities.never_quote(conn, SWEEP) is True
    assert securities.fetch_ticker(conn, SWEEP) is None
    assert investments.fetch_quotes(conn, [SWEEP], source=src) == []
    assert src.calls == []


def test_only_money_market_is_pinned(conn):
    _security(conn, SWEEP, kind=instruments.Kind.MONEY_MARKET.value)
    _security(conn, PLAN, kind=instruments.Kind.MUTUAL_FUND.value)
    _security(conn, EQUITY)

    assert investments.pinned_price(conn, SWEEP) == Decimal(1)
    assert investments.pinned_price(conn, PLAN) is None
    assert investments.pinned_price(conn, EQUITY) is None


# ---------------------------------------------------------------------------
# 3. Life cycle: the preference moves the sweep, and moves nothing else
# ---------------------------------------------------------------------------
EQUITY_VALUE = 200000      # 100 shares at 20.00
PLAN_VALUE = 62500         # 50 units at 12.50
SWEEP_VALUE = 500000       # 5,000 units at 1.00 (pinned)


@pytest.fixture
def portfolio_acct(conn, acct):
    """One synthetic account holding an equity, a plan fund and a sweep."""
    _security(conn, EQUITY, name="Zzzt Industries",
              asset_class="domestic_stock")
    _security(conn, PLAN, asset_class="intl_stock",
              kind=instruments.Kind.MUTUAL_FUND.value)
    _security(conn, SWEEP, kind=instruments.Kind.MONEY_MARKET.value)

    _buy(conn, acct, EQUITY, "100", "20")
    _buy(conn, acct, PLAN, "50", "12.50")
    _buy(conn, acct, SWEEP, "5000", "1")
    # The equity has a market quote; the plan fund has only what the statement
    # said, entered by hand. The sweep has neither and needs neither.
    investments.record_price(conn, EQUITY, DATE, "20", "yfinance")
    investments.record_price(conn, PLAN, DATE, "12.50", "manual")
    return acct


def test_valuation_moves_only_the_sweep(conn, portfolio_acct):
    off = investments.account_valuation(conn, portfolio_acct,
                                        money_market_as_cash=False)
    on = investments.account_valuation(conn, portfolio_acct,
                                       money_market_as_cash=True)

    # Same money, filed differently. The total is not a matter of opinion.
    assert off.total == on.total
    assert off.unpriced == [] and on.unpriced == []

    assert off.cash_equivalents == 0
    assert on.cash_equivalents == SWEEP_VALUE
    assert on.cash == off.cash + SWEEP_VALUE
    assert on.securities == off.securities - SWEEP_VALUE

    # Every holding -- the sweep included -- values identically either way.
    def held(v):
        return {h.symbol: h.market_value for h in v.holdings}

    assert held(off) == held(on) == {EQUITY: EQUITY_VALUE,
                                     PLAN: PLAN_VALUE,
                                     SWEEP: SWEEP_VALUE}


def test_allocation_moves_only_the_sweep(conn, portfolio_acct):
    off = portfolio.allocation(conn, [portfolio_acct],
                               money_market_as_cash=False)
    on = portfolio.allocation(conn, [portfolio_acct],
                              money_market_as_cash=True)

    assert off.total == on.total

    def classes(a):
        return {s.key: s.value for s in a.by_class}

    def secs(a):
        return {s.key: s.value for s in a.by_security}

    # By security is untouched: how much of the fund you hold is not an opinion.
    assert secs(off) == secs(on) == {EQUITY: EQUITY_VALUE,
                                     PLAN: PLAN_VALUE,
                                     SWEEP: SWEEP_VALUE}

    off_classes, on_classes = classes(off), classes(on)
    # The equity and the plan fund do not move.
    assert off_classes["domestic_stock"] == on_classes["domestic_stock"] == EQUITY_VALUE
    assert off_classes["intl_stock"] == on_classes["intl_stock"] == PLAN_VALUE
    # The sweep does, and lands in cash -- once, not twice.
    assert off_classes["unclassified"] == SWEEP_VALUE
    assert "unclassified" not in on_classes
    assert on_classes["cash"] == off_classes.get("cash", 0) + SWEEP_VALUE
    assert sum(on_classes.values()) == on.total


def test_the_default_changes_nothing(conn, portfolio_acct):
    """The preference ships OFF, so a file opened after this change reports the
    numbers it reported before it (SRD 5.8e-2c)."""
    from mammon.ui import prefs

    assert prefs.DEFAULT_MONEY_MARKET_AS_CASH is False

    default = investments.account_valuation(conn, portfolio_acct)
    explicit = investments.account_valuation(conn, portfolio_acct,
                                             money_market_as_cash=False)
    assert default == explicit
    assert default.cash_equivalents == 0
    assert default.securities == EQUITY_VALUE + PLAN_VALUE + SWEEP_VALUE

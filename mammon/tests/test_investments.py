"""Tests for mammon.investments: holdings derived from investment_transactions
(quantity + average-cost basis), price recording/lookup, account valuation from
injected prices, and fetch_quotes against a mocked quote source (no network)."""
from __future__ import annotations

import importlib.util
from decimal import Decimal
from fractions import Fraction

import pytest

from mammon import db, investments, ledger
from mammon.investments import (
    AccountValuation,
    Quote,
    QuoteSourceUnavailable,
    WebSlingerQuoteSource,
    YFinanceQuoteSource,
)


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "inv.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)


def _buy(conn, a, date, sym, qty, price, amount, commission=None):
    investments.record_investment(conn, a, date, "Buy", symbol=sym, quantity=qty,
                                  price=price, amount=amount, commission=commission)


# ---------------------------------------------------------------------------
# holdings: quantity + average-cost basis
# ---------------------------------------------------------------------------
def test_single_buy(conn, acct):
    _buy(conn, acct, "2026-01-05", "AAPL", "10", "100.00", -1000_00)
    h = investments.rebuild_holdings(conn, acct)
    assert h == [{"symbol": "AAPL", "quantity": "10", "cost_basis": 1000_00}]


def test_average_cost_across_buys_then_sell(conn, acct):
    _buy(conn, acct, "2026-01-05", "AAPL", "10", "100.00", -1000_00)
    _buy(conn, acct, "2026-02-05", "AAPL", "10", "120.00", -1200_00)
    # avg cost = 2200.00 / 20 = 110.00/share
    investments.record_investment(conn, acct, "2026-03-05", "Sell", symbol="AAPL",
                                  quantity="5", price="130.00", amount=650_00)
    investments.rebuild_holdings(conn, acct)
    h = investments.get_holding(conn, acct, "AAPL")
    assert h["quantity"] == "15"
    assert h["cost_basis"] == 1650_00        # 2200.00 - 5*110.00

def test_commission_adds_to_basis_when_no_amount(conn, acct):
    # amount omitted -> basis = price*qty + commission
    investments.record_investment(conn, acct, "2026-01-05", "Buy", symbol="MSFT",
                                  quantity="4", price="50.00", amount=None, commission=9_95)
    investments.rebuild_holdings(conn, acct)
    h = investments.get_holding(conn, acct, "MSFT")
    assert h["cost_basis"] == 200_00 + 9_95


def test_fractional_shares_text(conn, acct):
    _buy(conn, acct, "2026-01-05", "VTI", "1.5", "100.00", -150_00)
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "VTI")["quantity"] == "1.5"


def test_reinvested_dividend_adds_shares_and_cost(conn, acct):
    _buy(conn, acct, "2026-01-05", "X", "10", "50.00", -500_00)
    investments.record_investment(conn, acct, "2026-02-01", "ReinvDiv", symbol="X",
                                  quantity="2", price="55.00", amount=-110_00)
    investments.rebuild_holdings(conn, acct)
    h = investments.get_holding(conn, acct, "X")
    assert h["quantity"] == "12"
    assert h["cost_basis"] == 610_00


def test_cash_dividend_does_not_change_holding(conn, acct):
    _buy(conn, acct, "2026-01-05", "KO", "10", "60.00", -600_00)
    investments.record_investment(conn, acct, "2026-02-01", "Div", symbol="KO",
                                  amount=44_00)
    investments.rebuild_holdings(conn, acct)
    h = investments.get_holding(conn, acct, "KO")
    assert h["quantity"] == "10"
    assert h["cost_basis"] == 600_00
    # but the dividend is cash into the account
    assert investments.investment_cash(conn, acct) == -600_00 + 44_00


def test_share_transfer_in_and_out(conn, acct):
    investments.record_investment(conn, acct, "2026-01-05", "XIn", symbol="Y",
                                  quantity="5", price="20.00", amount=-100_00)
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "Y")["quantity"] == "5"
    investments.record_investment(conn, acct, "2026-02-05", "XOut", symbol="Y",
                                  quantity="5", amount=0)
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "Y") is None      # fully transferred out


def test_return_of_capital_reduces_basis(conn, acct):
    _buy(conn, acct, "2026-01-05", "Z", "10", "10.00", -100_00)
    investments.record_investment(conn, acct, "2026-02-05", "RtrnCap", symbol="Z",
                                  amount=20_00)
    investments.rebuild_holdings(conn, acct)
    h = investments.get_holding(conn, acct, "Z")
    assert h["quantity"] == "10"
    assert h["cost_basis"] == 100_00 - 20_00


def test_full_sale_removes_holding(conn, acct):
    _buy(conn, acct, "2026-01-05", "W", "10", "10.00", -100_00)
    investments.record_investment(conn, acct, "2026-02-05", "Sell", symbol="W",
                                  quantity="10", price="12.00", amount=120_00)
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "W") is None
    assert investments.list_holdings(conn, acct) == []


def test_income_row_without_symbol_ignored_for_holdings(conn, acct):
    investments.record_investment(conn, acct, "2026-01-05", "IntInc", amount=10_00)
    investments.rebuild_holdings(conn, acct)
    assert investments.list_holdings(conn, acct) == []
    assert investments.investment_cash(conn, acct) == 10_00


# ---------------------------------------------------------------------------
# stock splits: Quicken stores the ratio in quantity as new-shares-per-10-old
# ---------------------------------------------------------------------------
def test_stksplit_scales_shares_forward_and_reverse(conn, acct):
    # Forward 2:1 split (quantity=20 => 20 new per 10 old) doubles the shares.
    _buy(conn, acct, "2026-01-05", "FWD", "100", "10.00", -1000_00)
    investments.record_investment(conn, acct, "2026-02-01", "StkSplit",
                                  symbol="FWD", quantity="20")
    # Reverse 1:4 split (quantity=2.5 => 2.5 new per 10 old) quarters the shares.
    _buy(conn, acct, "2026-01-05", "REV", "60", "20.00", -1200_00)
    investments.record_investment(conn, acct, "2026-02-01", "StkSplit",
                                  symbol="REV", quantity="2.5")
    h = {r["symbol"]: r for r in investments.rebuild_holdings(conn, acct)}
    assert h["FWD"]["quantity"] == "200"        # 100 * 20/10
    assert h["FWD"]["cost_basis"] == 1000_00    # a split does not change basis
    assert h["REV"]["quantity"] == "15"         # 60 * 2.5/10 (reverse split)
    assert h["REV"]["cost_basis"] == 1200_00


# ---------------------------------------------------------------------------
# short sales: ShtSell opens a negative position, CvrShrt buys it back
# ---------------------------------------------------------------------------
def test_short_sale_open_partial_cover_and_full_cover(conn, acct):
    # ShtSell 200 @ 5.00: -200 shares; +1000.00 proceeds credited as negative basis.
    investments.record_investment(conn, acct, "2026-01-05", "ShtSell",
                                  symbol="SHT", quantity="200", price="5.00", amount=1000_00)
    # Partial cover of 100 relieves exactly half the credit.
    investments.record_investment(conn, acct, "2026-02-05", "CvrShrt",
                                  symbol="SHT", quantity="100", price="4.00", amount=400_00)
    investments.rebuild_holdings(conn, acct)
    h = investments.get_holding(conn, acct, "SHT")
    assert h["quantity"] == "-100"
    assert h["cost_basis"] == -500_00           # half of the -1000.00 credit remains
    # An open short values as a NEGATIVE (liability) market value.
    hv = investments.holding_values(conn, acct, prices={"SHT": "6.00"})[0]
    assert hv.quantity == Decimal("-100")
    assert hv.market_value == -600_00           # -100 * 6.00
    # Full cover of the remaining 100 closes the position; the holding is dropped.
    investments.record_investment(conn, acct, "2026-03-05", "CvrShrt",
                                  symbol="SHT", quantity="100", price="4.50", amount=450_00)
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "SHT") is None


# ---------------------------------------------------------------------------
# security rename: a renamed holding is unpriced until its own quote is entered
# ---------------------------------------------------------------------------
def test_renamed_security_is_unpriced_until_its_own_quote_is_recorded(conn, acct):
    """A Quicken security rename (same-date, same-quantity ShrsOut(old) +
    ShrsIn(new)) orphans the price series under the OLD name. There is no alias
    fallback that reuses it -- the renamed holding is reported unpriced until a
    real quote is recorded under the NEW symbol."""
    OLD, NEW = "OLDCO ENERGY TRUST", "OLDCO PETROLEUM LTD"
    _buy(conn, acct, "2020-01-05", OLD, "100", "10.00", -1000_00)
    investments.record_price(conn, OLD, "2020-12-31", "23.92")
    # Quicken renames it: a same-date, same-quantity ShrsOut(old) + ShrsIn(new).
    investments.record_investment(conn, acct, "2021-06-30", "ShrsOut",
                                  symbol=OLD, quantity="100", amount=0)
    investments.record_investment(conn, acct, "2021-06-30", "ShrsIn",
                                  symbol=NEW, quantity="100", amount=0)
    investments.rebuild_holdings(conn, acct)
    # The holding now lives under NEW and has no price series of its own.
    assert investments.get_holding(conn, acct, OLD) is None
    assert investments.get_holding(conn, acct, NEW)["quantity"] == "100"
    assert investments.latest_price(conn, NEW) is None

    val = investments.account_valuation(conn, acct, as_of="2021-12-31")
    assert NEW in val.unpriced
    hv = {h.symbol: h for h in val.holdings}[NEW]
    assert hv.price is None
    assert hv.market_value == 0

    # Once a real quote is recorded under the NEW symbol, it prices normally.
    investments.record_price(conn, NEW, "2021-12-31", "25.00")
    val2 = investments.account_valuation(conn, acct, as_of="2021-12-31")
    hv2 = {h.symbol: h for h in val2.holdings}[NEW]
    assert hv2.price == Decimal("25.00")
    assert hv2.market_value == 2500_00
    assert NEW not in val2.unpriced


# ---------------------------------------------------------------------------
# valuation from injected prices
# ---------------------------------------------------------------------------
def test_holding_values_and_securities_from_injected_prices(conn, acct):
    _buy(conn, acct, "2026-01-05", "AAPL", "10", "100.00", -1000_00)
    _buy(conn, acct, "2026-02-05", "AAPL", "10", "120.00", -1200_00)
    investments.record_investment(conn, acct, "2026-03-05", "Sell", symbol="AAPL",
                                  quantity="5", price="130.00", amount=650_00)
    investments.rebuild_holdings(conn, acct)
    hvs = investments.holding_values(conn, acct, prices={"AAPL": "140"})
    assert len(hvs) == 1
    hv = hvs[0]
    assert hv.quantity == Decimal("15")
    assert hv.price == Decimal("140")
    assert hv.market_value == 2100_00       # 15 * 140.00
    assert hv.gain == 2100_00 - 1650_00
    assert investments.securities_value(conn, acct, prices={"AAPL": "140"}) == 2100_00


def test_account_valuation_cash_plus_securities(conn, acct):
    # opening cash 3000.00 on the account
    a = ledger.create_account(conn, "Brokerage2", "investment", opening_balance=3000_00)
    _buy(conn, a, "2026-01-05", "AAPL", "10", "100.00", -1000_00)
    _buy(conn, a, "2026-02-05", "AAPL", "10", "120.00", -1200_00)
    investments.record_investment(conn, a, "2026-03-05", "Sell", symbol="AAPL",
                                  quantity="5", price="130.00", amount=650_00)
    investments.rebuild_holdings(conn, a)
    val = investments.account_valuation(conn, a, prices={"AAPL": "140"})
    assert isinstance(val, AccountValuation)
    # cash = 3000.00 opening + investment cash flows (-1000 -1200 +650) = 1450.00
    assert val.cash == 3000_00 - 1000_00 - 1200_00 + 650_00
    assert val.securities == 2100_00
    assert val.total == val.cash + val.securities
    assert val.unpriced == []


def test_unpriced_holding_flagged(conn, acct):
    # No price_history AND no per-transaction price -> genuinely unpriced.
    investments.record_investment(conn, acct, "2026-01-05", "Buy", symbol="OBSCURE",
                                  quantity="3", amount=-30_00)
    investments.rebuild_holdings(conn, acct)
    val = investments.account_valuation(conn, acct)      # no prices anywhere
    assert val.securities == 0
    assert val.unpriced == ["OBSCURE"]
    assert val.holdings[0].market_value == 0
    assert val.holdings[0].gain is None


def test_unpriced_holding_stays_unpriced_without_price_history(conn, acct):
    """A held security with no price_history row is reported unpriced -- no
    fallback synthesizes a value from its transaction prices. The price must be
    entered explicitly (price_history or an injected ``prices`` override)."""
    _buy(conn, acct, "2026-01-05", "FUND", "10", "10.00", -100_00)
    _buy(conn, acct, "2026-03-05", "FUND", "10", "12.00", -120_00)
    investments.rebuild_holdings(conn, acct)
    val = investments.account_valuation(conn, acct)      # no price_history at all
    assert val.unpriced == ["FUND"]
    assert val.holdings[0].price is None
    assert val.securities == 0
    # once a real price_history quote is recorded, it is picked up normally.
    investments.record_price(conn, "FUND", "2026-03-05", "15.00")
    val2 = investments.account_valuation(conn, acct)
    assert val2.holdings[0].price == Decimal("15.00")
    assert val2.securities == 20 * 15_00


# ---------------------------------------------------------------------------
# prices: record + latest lookup (with as_of)
# ---------------------------------------------------------------------------
def test_record_and_latest_price_upsert(conn):
    investments.record_price(conn, "AAPL", "2026-08-01", "150.00", "test")
    investments.record_price(conn, "AAPL", "2026-08-05", "160.00", "test")
    assert investments.latest_price(conn, "AAPL") == Decimal("160.00")
    assert investments.latest_price(conn, "AAPL", as_of="2026-08-03") == Decimal("150.00")
    assert investments.latest_price(conn, "NONE") is None
    # upsert: re-record same (symbol,date) updates in place, no duplicate row
    investments.record_price(conn, "AAPL", "2026-08-05", "161.00", "test")
    n = conn.execute("SELECT COUNT(*) FROM price_history WHERE symbol='AAPL'").fetchone()[0]
    assert n == 2
    assert investments.latest_price(conn, "AAPL") == Decimal("161.00")


# ---------------------------------------------------------------------------
# auto-quotes: injected source (no network)
# ---------------------------------------------------------------------------
class _FakeSource:
    source_name = "fake"

    def __init__(self, quotes):
        self._quotes = quotes
        self.asked = None

    def get_quotes(self, symbols):
        self.asked = list(symbols)
        return self._quotes


def test_fetch_quotes_writes_price_history(conn):
    src = _FakeSource([
        Quote("AAPL", "2026-08-07", "140.25", "fake"),
        Quote("MSFT", "2026-08-07", "410.10", "fake"),
    ])
    written = investments.fetch_quotes(conn, ["AAPL", "AAPL", " ", "MSFT"], source=src)
    assert src.asked == ["AAPL", "MSFT"]                 # deduped + stripped
    assert len(written) == 2
    assert investments.latest_price(conn, "AAPL") == Decimal("140.25")
    assert investments.latest_price(conn, "MSFT") == Decimal("410.10")
    row = conn.execute("SELECT source FROM price_history WHERE symbol='AAPL'").fetchone()
    assert row["source"] == "fake"


def test_fetch_quotes_source_name_fallback(conn):
    # a Quote with no explicit source falls back to the source's source_name
    src = _FakeSource([Quote("TSLA", "2026-08-07", "250.00", "")])
    investments.fetch_quotes(conn, ["TSLA"], source=src)
    row = conn.execute("SELECT source FROM price_history WHERE symbol='TSLA'").fetchone()
    assert row["source"] == "fake"


def test_fetch_quotes_empty_symbols_noop(conn):
    assert investments.fetch_quotes(conn, ["", "   "], source=_FakeSource([])) == []


def test_default_quote_source_depends_on_yfinance():
    have_yf = importlib.util.find_spec("yfinance") is not None
    if have_yf:
        assert isinstance(investments.default_quote_source(), YFinanceQuoteSource)
    else:
        with pytest.raises(QuoteSourceUnavailable):
            investments.default_quote_source()


def test_webslinger_quote_source_is_a_hook():
    with pytest.raises(NotImplementedError):
        WebSlingerQuoteSource().get_quotes(["ANY"])


# ---------------------------------------------------------------------------
# action-aware cash (works whether amount was stored signed or gross-positive)
# ---------------------------------------------------------------------------
def test_investment_cash_gross_positive_amounts(conn, acct):
    # The QIF importer stores the gross Quicken amount (always positive); the
    # cash direction must come from the ACTION. A Buy is cash out even at +1000.
    investments.record_investment(conn, acct, "1997-01-05", "Buy", symbol="F",
                                  quantity="10", price="100.00", amount=1000_00)
    investments.record_investment(conn, acct, "1997-02-05", "Div", symbol="F", amount=40_00)
    investments.record_investment(conn, acct, "1997-03-05", "Sell", symbol="F",
                                  quantity="3", price="110.00", amount=330_00)
    # -1000 (buy) + 40 (div) + 330 (sell)
    assert investments.investment_cash(conn, acct) == -1000_00 + 40_00 + 330_00


def test_investment_cash_zero_for_transfer_and_reinvest_actions(conn, acct):
    # BuyX (buy funded by a transfer in) and ReinvDiv (dividend reinvested) move
    # no NET cash in the account, whatever magnitude Quicken records.
    investments.record_investment(conn, acct, "1997-01-05", "BuyX", symbol="G",
                                  quantity="100", price="24.43", amount=2443_00)
    investments.record_investment(conn, acct, "1997-02-05", "ReinvDiv", symbol="G",
                                  quantity="4", price="25.00", amount=100_00)
    assert investments.investment_cash(conn, acct) == 0
    # ...but both still add shares
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "G")["quantity"] == "104"


def test_investment_cash_contribx_in_withdrwx_out(conn, acct):
    # Retirement/529 transfer actions: Quicken stores BOTH as positive magnitudes.
    # ContribX (contribution transferred IN) is cash in; WithdrwX (withdrawal
    # transferred OUT) is cash out -- the twin of XOut. Regression for the State529
    # 529 accounts where a defaulted-to-cash-in WithdrwX doubled the balance.
    investments.record_investment(conn, acct, "2015-01-05", "ContribX", amount=2000_00)
    investments.record_investment(conn, acct, "2024-01-04", "WithdrwX", amount=500_00)
    assert investments.investment_cash(conn, acct) == 2000_00 - 500_00
    # neither moves shares (no symbol) -> no non-zero holding is created
    investments.rebuild_holdings(conn, acct)
    assert all(h["quantity"] in ("0", "0.00", 0) for h in investments.list_holdings(conn, acct))


def test_display_balance_and_net_worth_value_investments(conn):
    inv = ledger.create_account(conn, "IRA", "investment", opening_balance=0)
    bank = ledger.create_account(conn, "Checking", "checking", opening_balance=500_00)
    investments.record_investment(conn, inv, "1997-01-05", "Buy", symbol="H",
                                  quantity="10", price="20.00", amount=200_00)
    investments.rebuild_holdings(conn, inv)
    investments.record_price(conn, "H", "1997-12-31", "30.00")
    # investment display balance = securities (10*30) + cash (-200 buy)
    assert investments.display_balance(conn, inv, as_of="1997-12-31") == 300_00 - 200_00
    # a non-investment account is unchanged (plain ledger balance)
    assert investments.display_balance(conn, bank) == 500_00
    # net worth values the investment at market and delegates from ledger
    nw = 500_00 + (300_00 - 200_00)
    assert investments.net_worth(conn, as_of="1997-12-31") == nw
    assert ledger.net_worth(conn, as_of="1997-12-31") == nw


def test_record_prices_bulk_upserts(conn):
    n = investments.record_prices(conn, [
        ("AAA", "1997-01-31", "10.00", "qif"),
        ("AAA", "1997-02-28", "11.00", "qif"),
        ("BBB", "1997-01-31", "5 1/4", "qif"),   # (fraction already normalized upstream)
    ])
    assert n == 3
    assert investments.latest_price(conn, "AAA") == Decimal("11.00")
    # re-record same (symbol,date) updates in place, no duplicate row
    investments.record_prices(conn, [("AAA", "1997-02-28", "12.00", "qif")])
    rows = conn.execute("SELECT COUNT(*) FROM price_history WHERE symbol='AAA'").fetchone()[0]
    assert rows == 2
    assert investments.latest_price(conn, "AAA") == Decimal("12.00")


def test_parse_price_text_handles_fractions_and_decimals():
    from mammon.importers.record import parse_price_text
    assert parse_price_text("25.93") == "25.93"
    assert parse_price_text("22 3/4") == "22.75"
    assert parse_price_text("1/2") == "0.5"
    assert parse_price_text("$1,234.50") == "1234.50"
    assert parse_price_text("") == ""
    assert parse_price_text("N/A") == ""


# ---------------------------------------------------------------------------
# register_rows: Quicken investment-register running balances (Task 47)
# ---------------------------------------------------------------------------
def test_register_rows_running_share_and_cash_balances(conn, acct):
    # A sequence across TWO securities exercising every branch:
    #   XIn   +cash into the account (no security)
    #   Buy   AAPL 10 @ 100  -> shares & -cash
    #   ReinvDiv AAPL 1 @ 50 -> +shares, cash-NEUTRAL (Quicken: blank Cash Amt)
    #   Buy   MSFT 4 @ 25    -> a DIFFERENT security's share balance
    #   Div   AAPL           -> cash income, no share move
    investments.record_investment(conn, acct, "2026-01-01", "XIn", amount=2_000_00)
    investments.record_investment(conn, acct, "2026-01-05", "Buy", symbol="AAPL",
                                  quantity="10", price="100", amount=-1_000_00)
    investments.record_investment(conn, acct, "2026-02-01", "ReinvDiv", symbol="AAPL",
                                  quantity="1", price="50", amount=50_00)
    investments.record_investment(conn, acct, "2026-02-05", "Buy", symbol="MSFT",
                                  quantity="4", price="25", amount=-100_00)
    investments.record_investment(conn, acct, "2026-03-01", "Div", symbol="AAPL",
                                  amount=12_00)

    rows = investments.register_rows(conn, acct)
    assert [r["action"] for r in rows] == ["XIn", "Buy", "ReinvDiv", "Buy", "Div"]

    # share_bal: per-security running quantity, None on cash/non-share-moving rows
    assert [r["share_bal"] for r in rows] == [None, "10", "11", "4", None]

    # inv_amt: gross security amount on share-moving rows, None otherwise
    assert [r["inv_amt"] for r in rows] == [None, 1_000_00, 50_00, 100_00, None]

    # cash_amt: XIn +2000, Buy -1000, ReinvDiv 0 (cash-neutral), Buy -100, Div +12
    assert [r["cash_amt"] for r in rows] == [2_000_00, -1_000_00, 0, -100_00, 12_00]

    # cash_bal: running account cash from opening 0
    assert [r["cash_bal"] for r in rows] == [
        2_000_00, 1_000_00, 1_000_00, 900_00, 912_00]

    # the last AAPL share_bal ties to the rebuilt holding (11 = 10 buy + 1 reinvdiv)
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "AAPL")["quantity"] == "11"
    assert investments.get_holding(conn, acct, "MSFT")["quantity"] == "4"


def _one_sided_transfer_split(conn, checking, target, date, leg_cents, payee):
    """Emulate the importer: a paycheck txn whose transfer leg is written
    one-sided (transfer_account_id set, transfer_pair_id NULL, no mirror), exactly
    what importers.core._insert_split produces. Returns the parent txn id."""
    salary = ledger.resolve_category(conn, "Salary")
    t = ledger.add_transaction(conn, checking, date, 2000_00, payee=payee)
    conn.execute("UPDATE transactions SET category_id=NULL WHERE id=?", (t,))
    conn.execute("INSERT INTO splits(transaction_id, category_id, amount, memo) "
                 "VALUES (?,?,?,?)", (t, salary, 2000_00 - leg_cents, None))
    conn.execute("INSERT INTO splits(transaction_id, transfer_account_id, amount, "
                 "memo) VALUES (?,?,?,?)", (t, target, leg_cents, "deferral"))
    conn.commit()
    return t


def test_register_shows_a_rebalances_sales_before_its_buys(conn, acct):
    """Same day, entered buys first: the register shows the cash coming in (the
    transfer, then the sale) before the purchases it pays for, and the running
    cash never goes negative. Share-only rows sit between the two."""
    investments.record_investment(conn, acct, "2026-03-02", "Buy", symbol="BOND",
                                  quantity="10", price="10", amount=-100_00)
    investments.record_investment(conn, acct, "2026-03-02", "Buy", symbol="STOCK",
                                  quantity="20", price="20", amount=-400_00)
    investments.record_investment(conn, acct, "2026-03-02", "ShrsIn", symbol="GIFT",
                                  quantity="1")
    investments.record_investment(conn, acct, "2026-03-02", "Sell", symbol="FUND",
                                  quantity="30", price="10", amount=300_00)
    investments.record_investment(conn, acct, "2026-03-02", "XIn", amount=200_00)
    rows = investments.register_rows(conn, acct)
    assert [(r["action"], r["cash_amt"]) for r in rows] == [
        ("Sell", 300_00), ("XIn", 200_00), ("ShrsIn", 0), ("Buy", -100_00), ("Buy", -400_00)]
    assert [r["cash_bal"] for r in rows] == [300_00, 500_00, 500_00, 400_00, 0]


def test_register_rows_lists_backfilled_transfer_leg(conn, acct):
    """A split leg transferring INTO an investment account, repaired by
    ledger.backfill_split_transfer_mirrors, is fabricated into the cash
    ``transactions`` table (the backfill is investment-unaware). The investment
    register reads ``investment_transactions`` only, so register_rows must merge
    that cash leg in -- otherwise it is invisible here even though the itemize
    report (which reads ``transactions``) counts it. This is the bug: the report
    shows a 401(k) total but the register shows nothing."""
    checking = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    _one_sided_transfer_split(conn, checking, acct, "2026-01-02", -453_11, "Vanguard")

    assert investments.register_rows(conn, acct) == []       # invisible before

    summary = ledger.backfill_split_transfer_mirrors(conn)
    assert summary["created"] == 1

    rows = investments.register_rows(conn, acct)
    assert len(rows) == 1                                    # now surfaced
    leg = rows[0]
    assert leg["date"] == "2026-01-02"
    assert leg["cash_amt"] == 453_11                         # +cash in (opposite sign)
    assert leg["cash_bal"] == 453_11
    assert leg["action"] == "XIn"
    assert leg["category_label"] == "[Checking]"             # transfer counterparty
    assert leg["payee"] == "Vanguard"
    assert leg["cash_leg"] is True                           # sourced from transactions
    # Ties to the report side: the summed transactions legs for the account.
    report_total = conn.execute(
        "SELECT SUM(amount) s FROM transactions WHERE account_id=? "
        "AND transfer_account_id IS NOT NULL", (acct,)).fetchone()["s"]
    assert sum(r["cash_amt"] for r in rows) == report_total


def test_register_rows_dedupes_backfilled_duplicate_but_lists_unrepresented(conn, acct):
    """The real 401(k) case: some contributions are ALREADY recorded as XIn cash
    rows (from the investment import); the backfill -- unable to see
    investment_transactions -- fabricates a duplicate ``transactions`` leg for each
    of those AND a leg for the periods with no XIn. register_rows must suppress the
    duplicates (match on date+counter-account+|amount|) yet surface the
    unrepresented legs, so every transfer shows exactly once."""
    checking = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    # A contribution the investment import already recorded as an XIn cash leg.
    investments.record_investment(conn, acct, "2026-01-02", "XIn",
                                  amount=453_11, transfer_account_id=checking)
    # A paycheck split references THAT SAME contribution one-sided...
    _one_sided_transfer_split(conn, checking, acct, "2026-01-02", -453_11, "Vanguard")
    # ...and a LATER contribution that has no XIn counterpart at all.
    _one_sided_transfer_split(conn, checking, acct, "2026-02-02", -453_11, "Vanguard")

    # Backfill fabricates a `transactions` leg for BOTH (it cannot adopt the XIn).
    assert ledger.backfill_split_transfer_mirrors(conn)["created"] == 2

    rows = investments.register_rows(conn, acct)
    # Exactly two rows: the XIn (Jan, deduped against its duplicate) and the
    # surfaced Feb leg -- not three.
    assert len(rows) == 2
    jan, feb = rows
    assert jan["date"] == "2026-01-02" and not jan.get("cash_leg")   # real XIn
    assert feb["date"] == "2026-02-02" and feb["cash_leg"] is True   # surfaced leg
    assert [r["cash_amt"] for r in rows] == [453_11, 453_11]
    assert feb["cash_bal"] == 906_22                                 # running cash


def test_account_list_balance_matches_register_total_with_duplicate_leg(conn, acct):
    """Regression (Vanguard 401K / account 82): the account-list balance
    (``display_balance`` -> ``account_valuation``) must equal the investment
    register's running cash total.

    A contribution recorded BOTH as an XIn in ``investment_transactions`` AND as a
    backfilled mirror leg in ``transactions`` was double-counted by the account-list
    figure -- ``account_balance`` (all ``transactions`` legs) plus ``investment_cash``
    (all XIn/XOut) summed the shared transfer twice -- while the register deduped it
    to once. On the real 401(k) this inflated 32,147.01 to 57,044.43. The valuation
    now subtracts the represented (duplicate) legs, matching the register."""
    checking = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    # Jan: contribution already recorded as an XIn cash leg AND referenced by a
    # one-sided paycheck split -> backfill fabricates a duplicate transactions leg.
    investments.record_investment(conn, acct, "2026-01-02", "XIn",
                                  amount=453_11, transfer_account_id=checking)
    _one_sided_transfer_split(conn, checking, acct, "2026-01-02", -453_11, "Vanguard")
    # Feb: contribution with no XIn counterpart -> a truly unrepresented leg.
    _one_sided_transfer_split(conn, checking, acct, "2026-02-02", -453_11, "Vanguard")
    assert ledger.backfill_split_transfer_mirrors(conn)["created"] == 2

    rows = investments.register_rows(conn, acct)
    register_total = rows[-1]["cash_bal"]
    assert register_total == 906_22                    # Jan XIn + Feb leg, once each

    # No holdings here, so the account-list valuation is pure cash and must tie to
    # the register total -- not the double-counted 906_22 + 453_11 the bug produced.
    assert investments.account_valuation(conn, acct).cash == register_total
    assert investments.display_balance(conn, acct) == register_total


def test_register_rows_opens_at_account_opening_balance(conn):
    a = ledger.create_account(conn, "Cash Brokerage", "investment",
                              opening_balance=500_00)
    investments.record_investment(conn, a, "2026-01-05", "Buy", symbol="AAPL",
                                  quantity="1", price="100", amount=-100_00)
    rows = investments.register_rows(conn, a)
    # running cash starts at the account's opening_balance, not 0
    assert rows[0]["cash_bal"] == 500_00 - 100_00


# ---------------------------------------------------------------------------
# price_history: the series the price-history chart plots (Task 46)
# ---------------------------------------------------------------------------
def test_price_history_ascending_scoped_and_as_of(conn):
    # Recorded out of order + a different symbol -> price_history returns THIS
    # symbol's closes ascending by date, as Decimals.
    investments.record_price(conn, "AAPL", "2026-08-05", "160.00", "test")
    investments.record_price(conn, "AAPL", "2026-08-01", "150.00", "test")
    investments.record_price(conn, "AAPL", "2026-08-03", "155.00", "test")
    investments.record_price(conn, "MSFT", "2026-08-02", "410.00", "test")

    hist = investments.price_history(conn, "AAPL")
    assert [d for d, _p in hist] == ["2026-08-01", "2026-08-03", "2026-08-05"]
    assert [p for _d, p in hist] == [
        Decimal("150"), Decimal("155"), Decimal("160")]
    assert all(isinstance(p, Decimal) for _d, p in hist)

    # as_of caps the series at that date (inclusive)
    capped = investments.price_history(conn, "AAPL", as_of="2026-08-03")
    assert [d for d, _p in capped] == ["2026-08-01", "2026-08-03"]

    # a symbol with no recorded prices -> empty (the chart shows a placeholder)
    assert investments.price_history(conn, "NONE") == []


# ---------------------------------------------------------------------------
# per-security filtered view <-> holdings reconciliation
# ---------------------------------------------------------------------------
def test_security_report_currently_held_reconciles_to_holdings(conn, acct):
    """Filtering by a still-held security shows running shares + dividend sum +
    cost basis + P/L that tie to compute_holdings / holding_values."""
    _buy(conn, acct, "2026-01-05", "AAPL", "10", "100.00", -1000_00)
    _buy(conn, acct, "2026-02-05", "AAPL", "10", "120.00", -1200_00)  # avg 110/sh
    investments.record_investment(conn, acct, "2026-02-10", "Div",
                                  symbol="AAPL", amount=50_00)
    investments.record_investment(conn, acct, "2026-03-05", "Sell", symbol="AAPL",
                                  quantity="5", price="130.00", amount=650_00)
    investments.rebuild_holdings(conn, acct)

    rep = investments.security_report(conn, acct, "AAPL", prices={"AAPL": "140"})
    pos = rep.position

    # running SHARE BALANCE after each transaction (blank on the cash Div row).
    assert [r["share_bal"] for r in rep.rows] == ["10", "20", None, "15"]

    # reconcile to compute_holdings (qty + average cost) ...
    lots = investments.compute_holdings(conn, acct)
    assert pos.quantity == lots["AAPL"].qty == Decimal("15")
    assert pos.cost_basis == lots["AAPL"].cost == 1650_00
    # ... and to holding_values (the Holdings view's priced row): same P/L.
    hv = {h.symbol: h for h in
          investments.holding_values(conn, acct, prices={"AAPL": "140"})}["AAPL"]
    assert pos.market_value == hv.market_value == 2100_00
    assert pos.unrealized_pl == hv.gain == 450_00       # 2100 - 1650
    # dividend SUM and realized P/L (net proceeds 650 - relieved cost 550).
    assert pos.dividends == 50_00
    assert pos.realized_pl == 100_00
    assert pos.total_pl == 550_00                        # 450 unrealized + 100 realized
    assert pos.is_open is True


def test_held_positions_match_holding_values_row_for_row(conn, acct):
    _buy(conn, acct, "2026-01-05", "AAPL", "10", "100.00", -1000_00)
    _buy(conn, acct, "2026-01-05", "MSFT", "4", "50.00", -200_00)
    investments.rebuild_holdings(conn, acct)
    prices = {"AAPL": "140", "MSFT": "55"}
    held = {p.symbol: p for p in investments.held_positions(conn, acct, prices=prices)}
    hvs = {h.symbol: h for h in investments.holding_values(conn, acct, prices=prices)}
    assert set(held) == set(hvs)
    for sym, p in held.items():
        assert p.quantity == hvs[sym].quantity
        assert p.cost_basis == hvs[sym].cost_basis
        assert p.market_value == hvs[sym].market_value
        assert p.unrealized_pl == hvs[sym].gain


def test_closed_position_previously_held_carries_pl(conn, acct):
    """A sold-out security vanishes from holdings but survives as a previously-held
    position with its realized P/L and dividend total (its own Holdings tab)."""
    _buy(conn, acct, "2026-01-05", "KO", "10", "60.00", -600_00)
    investments.record_investment(conn, acct, "2026-02-01", "Div",
                                  symbol="KO", amount=20_00)
    investments.record_investment(conn, acct, "2026-03-05", "Sell", symbol="KO",
                                  quantity="10", price="75.00", amount=750_00)
    investments.rebuild_holdings(conn, acct)

    # gone from the Currently-Held view / holdings table ...
    assert investments.get_holding(conn, acct, "KO") is None
    assert "KO" not in {h.symbol for h in investments.holding_values(conn, acct)}
    assert investments.held_positions(conn, acct) == []
    # ... but compute_holdings still nets it to zero shares / zero cost ...
    lots = investments.compute_holdings(conn, acct)
    assert lots["KO"].qty == 0 and lots["KO"].cost == 0
    # ... and it shows in the Previously-Held tab with P/L.
    closed = investments.closed_positions(conn, acct)
    assert len(closed) == 1
    ko = closed[0]
    assert ko.symbol == "KO"
    assert ko.is_open is False
    assert ko.quantity == 0
    assert ko.cost_basis == 0
    assert ko.dividends == 20_00
    assert ko.realized_pl == 150_00                      # 750 proceeds - 600 basis
    assert ko.unrealized_pl is None
    assert ko.market_value == 0
    assert ko.total_pl == 150_00


def test_dividend_only_symbol_is_not_a_previously_held_position(conn, acct):
    """A symbol that only ever paid a cash dividend (never held) is excluded from
    both tabs -- ever_held gates it out."""
    investments.record_investment(conn, acct, "2026-02-01", "Div",
                                  symbol="GHOST", amount=5_00)
    investments.rebuild_holdings(conn, acct)
    assert investments.held_positions(conn, acct) == []
    assert investments.closed_positions(conn, acct) == []


def test_security_report_filters_rows_to_one_symbol(conn, acct):
    _buy(conn, acct, "2026-01-05", "AAPL", "10", "100.00", -1000_00)
    _buy(conn, acct, "2026-01-06", "MSFT", "4", "50.00", -200_00)
    rep = investments.security_report(conn, acct, "AAPL")
    assert {r["symbol"] for r in rep.rows} == {"AAPL"}
    assert len(rep.rows) == 1


def test_review_accept_learns_price_history_from_the_transaction(conn):
    """A 401(k)'s funds are quoted nowhere public, so the only price history
    available is what the broker's own rows state. Accepting an investment row
    through review must capture it -- the straight-through bulk path always did,
    but the single-account review path did not, so a broker QIF imported its
    transactions and learned no prices at all."""
    from mammon import import_review, importers, ledger

    acct = ledger.create_account(conn, "401k", "investment", opening_balance=0)
    records = importers.qif.parse_qif(
        "!Type:Invst\n"
        "D01/07/2026\nNShrsOut\nYTARGET 2030 FUND\nI30.33\nQ0.125\nT3.78\n^\n",
        default_account="401k")
    summary = import_review.import_records_via_review(conn, acct, records)
    assert summary["added"] == 1

    rows = conn.execute(
        "SELECT symbol, date, close_price, source FROM price_history "
        "WHERE symbol='TARGET 2030 FUND'").fetchall()
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-01-07"
    from decimal import Decimal
    assert Decimal(str(rows[0]["close_price"])) == Decimal("30.33")
    assert rows[0]["source"] == "txn"


def test_an_explicit_quote_outranks_a_transaction_price(conn):
    """A transaction price is one trade, not a close, so an explicit quote wins
    for the same (symbol, date)."""
    from mammon import investments, ledger

    acct = ledger.create_account(conn, "401k", "investment", opening_balance=0)
    conn.execute(
        "INSERT INTO investment_transactions"
        "(account_id, date, action, symbol, quantity, price, amount) "
        "VALUES (?,?,?,?,?,?,?)",
        (acct, "2026-01-07", "Buy", "FUND A", "1", "30.33", 3033))
    conn.commit()

    investments.record_price(conn, "FUND A", "2026-01-07", "31.00", "quote")
    investments.learn_prices_from_transactions(conn, acct)
    row = conn.execute(
        "SELECT close_price, source FROM price_history "
        "WHERE symbol='FUND A' AND date='2026-01-07'").fetchone()
    from decimal import Decimal
    assert Decimal(str(row["close_price"])) == Decimal("31.00")
    assert row["source"] == "quote"

    # ...but an unquoted date is learned from the transaction.
    conn.execute(
        "INSERT INTO investment_transactions"
        "(account_id, date, action, symbol, quantity, price, amount) "
        "VALUES (?,?,?,?,?,?,?)",
        (acct, "2026-01-08", "Buy", "FUND A", "1", "30.50", 3050))
    conn.commit()
    investments.learn_prices_from_transactions(conn, acct)
    learned = conn.execute(
        "SELECT close_price, source FROM price_history "
        "WHERE symbol='FUND A' AND date='2026-01-08'").fetchone()
    assert Decimal(str(learned["close_price"])) == Decimal("30.50")
    assert learned["source"] == "txn"


# ---- Get Quotes (investment register gear menu) ------------------------------
def test_ticker_of_rejects_internally_named_plan_funds():
    """A plan's funds are named internally and have no public listing. The
    danger is not that a lookup fails -- it is that it SUCCEEDS: 'INTL EQUITY
    INDEX' yields INTL, a real listed company, so an unasked fetch would file a
    stranger's price against the user's holding."""
    from mammon import investments

    assert investments.ticker_of("ALTY") == "ALTY"
    assert investments.ticker_of("ALTY GLOBAL X SUPERDIVIDEND ALTER") == "ALTY"
    assert investments.ticker_of("QTUM DEFIANCE QUANTUM ETF") == "QTUM"
    assert investments.ticker_of("FXAIX") == "FXAIX"
    # No ticker-shaped head token -> no guess at all.
    for name in ("DOMESTIC BOND INDEX", "S&P 500 EQUITY INDEX",
                 "Fidelity 500 Index Fund", "TARGET 2030 FUND", "", None):
        assert investments.ticker_of(name) == "", name


# ---------------------------------------------------------------------------
# renaming a security (search/replace, scoped to one account)
# ---------------------------------------------------------------------------
def test_plan_security_rename_matches_by_substring_either_way(conn, acct):
    """The two real shapes: drop a descriptive tail, and replace a name outright
    with a ticker it shares no text with."""
    _buy(conn, acct, "2026-01-05", "ALTY GLOBAL X SUPERDIVIDEND ALTER", "10", "20", -200_00)
    _buy(conn, acct, "2026-01-06", "Fidelity 500 Index Fund", "3", "100", -300_00)

    tail = investments.plan_security_rename(
        conn, acct, "global x superdividend alter", "")     # case-insensitive
    assert [(r.old, r.new) for r in tail] == [
        ("ALTY GLOBAL X SUPERDIVIDEND ALTER", "ALTY")]

    whole = investments.plan_security_rename(
        conn, acct, "Fidelity 500 Index Fund", "FXAIX")
    assert [(r.old, r.new, r.txns) for r in whole] == [
        ("Fidelity 500 Index Fund", "FXAIX", 1)]


def test_plan_flags_a_rename_that_merges_two_positions(conn, acct):
    _buy(conn, acct, "2026-01-05", "FXAIX", "1", "100", -100_00)
    _buy(conn, acct, "2026-01-06", "Fidelity 500 Index Fund", "3", "100", -300_00)
    plan = investments.plan_security_rename(
        conn, acct, "Fidelity 500 Index Fund", "FXAIX")
    assert [(r.new, r.merges) for r in plan] == [("FXAIX", True)]
    # ...and does NOT flag one whose new name is not already here
    plan = investments.plan_security_rename(
        conn, acct, "Fidelity 500 Index Fund", "FSPGX")
    assert [(r.new, r.merges) for r in plan] == [("FSPGX", False)]


def test_plan_refuses_to_leave_a_security_nameless(conn, acct):
    _buy(conn, acct, "2026-01-05", "FXAIX", "1", "100", -100_00)
    with pytest.raises(ValueError):
        investments.plan_security_rename(conn, acct, "FXAIX", "")


def test_apply_rename_merges_lots_and_rebuilds_holdings(conn, acct):
    """The point of a merge: one position with both sides' shares and basis."""
    _buy(conn, acct, "2026-01-05", "Fidelity 500 Index Fund", "3", "100", -300_00)
    _buy(conn, acct, "2026-02-05", "FXAIX", "1", "200", -200_00)
    investments.rebuild_holdings(conn, acct)
    assert len(investments.list_holdings(conn, acct)) == 2

    renamed = investments.apply_security_renames(
        conn, acct, [("Fidelity 500 Index Fund", "FXAIX")])
    assert renamed == 1

    held = investments.list_holdings(conn, acct)
    assert [h["symbol"] for h in held] == ["FXAIX"]
    assert held[0]["quantity"] == "4"
    assert held[0]["cost_basis"] == 500_00
    # the derived year-end snapshot was rebuilt, not patched: no stale row under
    # the old name to carry a pre-merge basis forward
    stale = conn.execute(
        "SELECT COUNT(*) FROM holdings_checkpoints WHERE account_id=? AND symbol=?",
        (acct, "Fidelity 500 Index Fund")).fetchone()[0]
    assert stale == 0


def test_apply_rename_carries_price_history_to_the_new_name(conn, acct):
    _buy(conn, acct, "2026-01-05", "Fidelity 500 Index Fund", "3", "100", -300_00)
    investments.record_price(conn, "Fidelity 500 Index Fund", "2026-01-05", "100")
    investments.record_price(conn, "Fidelity 500 Index Fund", "2026-02-05", "110")
    investments.record_price(conn, "FXAIX", "2026-02-05", "115")   # a real quote

    investments.apply_security_renames(
        conn, acct, [("Fidelity 500 Index Fund", "FXAIX")])

    hist = dict(investments.price_history(conn, "FXAIX"))
    assert hist["2026-01-05"] == Decimal("100")    # carried across
    assert hist["2026-02-05"] == Decimal("115")    # the existing quote WINS
    # nothing refers to the old name any more, so its rows are gone
    assert investments.price_history(conn, "Fidelity 500 Index Fund") == []


def test_rename_is_scoped_to_one_account(conn, acct):
    """Another account keeps both the old name and the prices it needs."""
    other = ledger.create_account(conn, "Other Brokerage", "investment",
                                  opening_balance=0)
    _buy(conn, acct, "2026-01-05", "Fidelity 500 Index Fund", "3", "100", -300_00)
    _buy(conn, other, "2026-01-05", "Fidelity 500 Index Fund", "2", "100", -200_00)
    investments.record_price(conn, "Fidelity 500 Index Fund", "2026-01-05", "100")

    investments.apply_security_renames(
        conn, acct, [("Fidelity 500 Index Fund", "FXAIX")])

    assert investments.symbols_used(conn, acct) == ["FXAIX"]
    assert investments.symbols_used(conn, other) == ["Fidelity 500 Index Fund"]
    # the other account did not lose the price it is valued with
    assert investments.latest_price(conn, "Fidelity 500 Index Fund") == Decimal("100")
    assert investments.latest_price(conn, "FXAIX") == Decimal("100")


# ---------------------------------------------------------------------------
# valuation as-of: a fetched quote has to count
# ---------------------------------------------------------------------------
def test_valuation_as_of_includes_a_price_newer_than_the_last_transaction(conn, acct):
    """Regression: Get Quotes reported nine quotes downloaded and the account
    total did not move, because the as-of was capped at the last TRANSACTION and
    a fresh quote is always later than that."""
    _buy(conn, acct, "2026-01-05", "AAPL", "10", "100.00", -1000_00)
    investments.rebuild_holdings(conn, acct)
    investments.record_price(conn, "AAPL", "2026-01-05", "100")
    before = investments.display_balance(conn, acct)

    investments.record_price(conn, "AAPL", "2026-03-31", "150", "yfinance")

    assert ledger.latest_activity_date(conn) == "2026-01-05"
    assert investments.valuation_as_of(conn) == "2026-03-31"
    assert investments.display_balance(conn, acct) - before == 500_00


# ---------------------------------------------------------------------------
# stock splits: one encoding, applied everywhere
# ---------------------------------------------------------------------------
def test_split_ratio_helpers_round_trip():
    assert investments.split_stored("8") == Decimal("80")
    assert investments.split_ratio("80") == Decimal("8")
    assert investments.split_ratio_text("80") == "8:1"
    assert investments.split_ratio_text("30") == "3:1"
    assert investments.split_ratio_text("5") == "1:2"      # a reverse split
    assert investments.split_ratio_text("15") == "3:2"
    assert investments.split_ratio_text(None) == ""


def test_register_share_balance_follows_a_split(conn, acct):
    """Regression: a StkSplit is neither an ADD nor a REMOVE, so the register's
    running Share Bal skipped it -- leaving the split row blank AND every later
    row for that security reporting a pre-split balance that disagreed with
    holdings."""
    _buy(conn, acct, "2026-01-05", "VGT", "26", "700", -18_200_00)
    investments.record_investment(conn, acct, "2026-04-21", "StkSplit",
                                  symbol="VGT", quantity="80")     # 8-for-1
    investments.record_investment(conn, acct, "2026-05-05", "Buy", symbol="VGT",
                                  quantity="2", price="95", amount=-190_00)

    rows = investments.register_rows(conn, acct)
    bal = [r["share_bal"] for r in rows]
    assert bal == ["26", "208", "210"]

    # the column's last value per symbol ties to holdings, as documented
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "VGT")["quantity"] == "210"


def test_split_row_reports_no_cash_and_no_inv_amount(conn, acct):
    _buy(conn, acct, "2026-01-05", "VGT", "26", "700", -18_200_00)
    investments.record_investment(conn, acct, "2026-04-21", "StkSplit",
                                  symbol="VGT", quantity="80")
    split = investments.register_rows(conn, acct)[-1]
    assert split["cash_amt"] == 0
    assert split["inv_amt"] is None


def test_parse_split_ratio_accepts_what_the_register_displays():
    """The register renders a split as "8:1", so the field that edits one has to
    read "8:1" -- it took a bare number only."""
    P = investments.parse_split_ratio
    assert P("8:1") == Fraction(8)
    assert P("8") == Fraction(8)
    assert P("3:2") == Fraction(3, 2)
    assert P("1.5") == Fraction(3, 2)
    assert P("1:2") == Fraction(1, 2)          # a reverse split
    assert P("8-for-1") == Fraction(8)
    assert P("3 for 2") == Fraction(3, 2)
    assert P("1/2") == Fraction(1, 2)
    assert P("") is None and P("abc") is None and P("8:0") is None


def test_odd_ratio_splits_are_exact(conn, acct):
    """4:3 and 1:3 have no exact decimal form, so the old per-ten encoding could
    not hold them and they were unrecordable. The ratio is stored as a PAIR and
    applied by multiplying before dividing, which is exact."""
    _buy(conn, acct, "2026-01-05", "ABC", "300", "40", -12_000_00)
    ratio = investments.parse_split_ratio("4:3")
    investments.record_investment(
        conn, acct, "2026-04-21", "StkSplit", symbol="ABC",
        quantity=investments.split_stored(ratio),
        split_num=ratio.numerator, split_den=ratio.denominator)
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "ABC")["quantity"] == "400"

    row = investments.register_rows(conn, acct)[-1]
    assert row["share_bal"] == "400"
    assert investments.split_display(row) == "4:3"


def test_reverse_one_for_three_is_exact(conn, acct):
    _buy(conn, acct, "2026-01-05", "ABC", "300", "40", -12_000_00)
    ratio = investments.parse_split_ratio("1:3")
    investments.record_investment(
        conn, acct, "2026-04-21", "StkSplit", symbol="ABC",
        quantity=investments.split_stored(ratio),
        split_num=ratio.numerator, split_den=ratio.denominator)
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "ABC")["quantity"] == "100"


def test_a_legacy_split_row_still_replays(conn, acct):
    """Rows written before the pair existed carry only the per-ten quantity.
    They are read through the same path, so no backfill was needed."""
    _buy(conn, acct, "2026-01-05", "ABC", "36", "40", -1_440_00)
    investments.record_investment(conn, acct, "2026-04-21", "StkSplit",
                                  symbol="ABC", quantity="30")   # a 3-for-1
    conn.execute("UPDATE investment_transactions SET split_num=NULL, "
                 "split_den=NULL WHERE action='StkSplit'")
    conn.commit()
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "ABC")["quantity"] == "108"
    row = investments.register_rows(conn, acct)[-1]
    assert investments.split_display(row) == "3:1"
    assert row["share_bal"] == "108"


def test_three_for_two_split_is_exact(conn, acct):
    _buy(conn, acct, "2026-01-05", "ABC", "100", "60", -6_000_00)
    stored = investments.split_stored(investments.parse_split_ratio("3:2"))
    investments.record_investment(conn, acct, "2026-04-21", "StkSplit",
                                  symbol="ABC", quantity=stored)
    investments.rebuild_holdings(conn, acct)
    assert investments.get_holding(conn, acct, "ABC")["quantity"] == "150"
    row = investments.register_rows(conn, acct)[-1]
    assert row["share_bal"] == "150"
    assert investments.split_display(investments.register_rows(conn, acct)[-1]) == "3:2"


# ---------------------------------------------------------------------------
# Historical backfill: the net-worth curve should not step on the dates prices
# happened to be recorded
# ---------------------------------------------------------------------------
class _FakeHistorySource:
    """A quote source with history, in the shape yfinance's wrapper returns."""

    source_name = "fake"

    def __init__(self, series):
        self.series = series          # {ticker: [(date, close), ...]}
        self.asked = []

    def get_quotes(self, symbols):
        out = []
        for s in symbols:
            rows = self.series.get(s.upper())
            if rows:
                out.append(investments.Quote(s, rows[-1][0], rows[-1][1], "fake"))
        return out

    def get_history(self, symbols, *, start=None, end=None, interval="1mo"):
        self.asked.append((tuple(symbols), start, end, interval))
        out = []
        for s in symbols:
            for date, close in self.series.get(s.upper(), []):
                if start and date < start:
                    continue
                if end and date > end:
                    continue
                out.append(investments.Quote(s, date, close, "fake"))
        return out


def test_fetch_quote_history_records_under_the_holding_name(conn, acct):
    """price_history is keyed by the name a holding is STORED under, so history
    filed under the bare ticker prices nothing."""
    src = _FakeHistorySource({"ALTY": [("2026-01-31", "20"), ("2026-02-28", "21")]})
    n = investments.fetch_quote_history(
        conn, [("ALTY GLOBAL X SUPERDIVIDEND ALTER", "ALTY")], source=src)
    assert n == 2
    hist = dict(investments.price_history(conn, "ALTY GLOBAL X SUPERDIVIDEND ALTER"))
    assert hist == {"2026-01-31": Decimal("20"), "2026-02-28": Decimal("21")}
    assert investments.price_history(conn, "ALTY") == []


def test_fetch_quote_history_never_overwrites_a_recorded_price(conn, acct):
    """A price carried by a real transaction is what the user actually paid that
    day; a monthly close must not displace it."""
    investments.record_price(conn, "ABC", "2026-01-31", "99.5", "txn")
    src = _FakeHistorySource({"ABC": [("2026-01-31", "20"), ("2026-02-28", "21")]})
    investments.fetch_quote_history(conn, [("ABC", "ABC")], source=src)
    hist = dict(investments.price_history(conn, "ABC"))
    assert hist["2026-01-31"] == Decimal("99.5")     # the transaction price wins
    assert hist["2026-02-28"] == Decimal("21")       # the gap got filled


def test_fetch_quote_history_reports_a_source_without_history(conn, acct):
    class _LatestOnly:
        source_name = "latest-only"

        def get_quotes(self, symbols):
            return []

    with pytest.raises(investments.QuoteSourceUnavailable):
        investments.fetch_quote_history(conn, [("ABC", "ABC")], source=_LatestOnly())


def test_first_transaction_dates_bound_the_fetch(conn, acct):
    _buy(conn, acct, "2020-03-04", "OLD", "1", "10", -10_00)
    _buy(conn, acct, "2026-01-05", "NEW", "1", "10", -10_00)
    firsts = investments.first_transaction_dates(conn, acct)
    assert firsts == {"OLD": "2020-03-04", "NEW": "2026-01-05"}


def test_monthly_history_smooths_the_net_worth_curve(conn, acct):
    """The point of the feature. With one price in 2024 and the next in 2026, a
    holding is valued flat at the stale price and then jumps; monthly closes give
    the curve its real shape."""
    from mammon import reports

    _buy(conn, acct, "2024-01-15", "ABC", "100", "10", -1_000_00)
    investments.rebuild_holdings(conn, acct)
    investments.record_price(conn, "ABC", "2024-01-15", "10")
    investments.record_price(conn, "ABC", "2026-01-15", "20")

    sparse = [p.cents for p in reports.net_worth_series(
        conn, "2024-01-15", "2026-01-15", points=5).points]
    # a flat run at the stale price, then one jump at the end
    assert sparse[0] == sparse[1] == sparse[2] == sparse[3]
    assert sparse[4] > sparse[3]

    src = _FakeHistorySource({"ABC": [
        ("2024-07-01", "12.5"), ("2025-01-01", "15"), ("2025-07-01", "17.5")]})
    investments.fetch_quote_history(conn, [("ABC", "ABC")], start="2024-01-15",
                                    source=src)
    assert src.asked[0][1] == "2024-01-15"          # bounded by the first buy

    smooth = [p.cents for p in reports.net_worth_series(
        conn, "2024-01-15", "2026-01-15", points=5).points]
    assert smooth == sorted(smooth)                 # monotonically rising
    assert len(set(smooth)) == 5                    # every sample moved


# ---------------------------------------------------------------------------
# A share move's price: recorded, derived, or lost
# ---------------------------------------------------------------------------
def test_price_derived_from_value_and_shares(conn, acct):
    """A plan's fee removal states the shares taken and what they were worth but
    no per-share price. That price is not a guess -- it is the same number the
    price column would have held -- and for a fund quoted nowhere public it is
    the only quote that exists."""
    investments.record_investment(conn, acct, "2000-10-12", "ShrsOut",
                                  symbol="Employer Common Stock",
                                  quantity="0.045", amount=1_28, memo="Fees")
    assert investments.learn_prices_from_transactions(conn, acct) == 1
    hist = dict(investments.price_history(conn, "Employer Common Stock"))
    assert hist["2000-10-12"] == Decimal("28.444444")


def test_derived_price_takes_commission_out_first(conn, acct):
    """Basis is price*qty + commission, so dividing the gross by the shares
    would report a price no share ever traded at."""
    investments.record_investment(conn, acct, "2020-01-05", "Buy", symbol="ABC",
                                  quantity="10", amount=-1_005_00, commission=5_00)
    investments.learn_prices_from_transactions(conn, acct)
    assert dict(investments.price_history(conn, "ABC"))["2020-01-05"] == Decimal("100")


def test_a_stated_price_still_wins_over_the_derived_one(conn, acct):
    investments.record_investment(conn, acct, "2020-01-05", "Sell", symbol="ABC",
                                  quantity="10", price="99", amount=1_000_00)
    investments.learn_prices_from_transactions(conn, acct)
    assert dict(investments.price_history(conn, "ABC"))["2020-01-05"] == Decimal("99")


def test_a_share_move_with_no_value_yields_no_price(conn, acct):
    """597 of 599 ShrsOut rows in the real ledger carry only a share count.
    Nothing can be recovered from those, and nothing must be invented."""
    investments.record_investment(conn, acct, "2001-01-11", "ShrsOut",
                                  symbol="Employer Common Stock", quantity="0.023")
    assert investments.learn_prices_from_transactions(conn, acct) == 0
    assert investments.price_history(conn, "Employer Common Stock") == []

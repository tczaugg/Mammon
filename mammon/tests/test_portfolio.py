"""mammon.portfolio: open lots valued at a date, the money-weighted return of
an account and of one security, and allocation by asset class."""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, instruments, investments, ledger, portfolio, securities


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "portfolio.db")
    yield c
    c.close()


@pytest.fixture
def world(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=50000_00)
    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    ledger.create_transfer(conn, chk, inv, "2025-01-02", 10000_00, payee="Fund brokerage")
    investments.record_investment(conn, inv, "2025-01-03", "Buy", symbol="AAPL",
                                  quantity="100", price="100.00", amount=-10000_00)
    investments.record_investment(conn, inv, "2025-06-01", "Div", symbol="AAPL", amount=200_00)
    investments.record_price(conn, "AAPL", "2025-12-31", "110.00")
    investments.rebuild_holdings(conn, inv)
    return {"chk": chk, "inv": inv}


# ---------------------------------------------------------------------------
# lots valued at a date
# ---------------------------------------------------------------------------
def test_open_lots_are_valued_with_term_and_days(conn, world):
    inv = world["inv"]
    investments.record_investment(conn, inv, "2025-11-20", "Buy", symbol="AAPL",
                                  quantity="10", price="105.00", amount=-1050_00)
    investments.rebuild_holdings(conn, inv)
    lots = portfolio.open_lots(conn, inv, as_of="2026-02-01")
    assert [(l.acquired, str(l.quantity), l.cost, l.term, l.days_held) for l in lots] == [
        ("2025-01-03", "100", 10000_00, "long", 394),
        ("2025-11-20", "10", 1050_00, "short", 73)]
    assert lots[0].price == Decimal("110.00") and lots[0].market_value == 11000_00
    assert lots[0].gain == 1000_00 and lots[1].gain == 50_00
    assert portfolio.open_lots(conn, inv, symbol="MSFT") == []
    # Unpriced: value and gain unknown, not zero gain.
    investments.record_investment(conn, inv, "2025-12-01", "Buy", symbol="ZZZ",
                                  quantity="1", price="5.00", amount=-5_00)
    z = [l for l in portfolio.open_lots(conn, inv, as_of="2026-02-01") if l.symbol == "ZZZ"]
    assert z[0].price is None and z[0].gain is None and z[0].market_value == 0


# ---------------------------------------------------------------------------
# money-weighted return
# ---------------------------------------------------------------------------
def test_xirr_solves_known_rates_and_refuses_one_signed_flows():
    r = portfolio.xirr([("2025-01-01", -1000_00), ("2026-01-01", 1100_00)])
    assert abs(r - 0.10) < 1e-6
    r2 = portfolio.xirr([("2025-01-01", -1000_00), ("2025-07-01", -500_00),
                         ("2026-01-01", 1650_00)])
    assert abs(1000_00 + 500_00 / (1 + r2) ** (181 / 365) - 1650_00 / (1 + r2)) < 1e-3
    assert portfolio.xirr([("2025-01-01", 100_00), ("2026-01-01", 100_00)]) is None
    assert portfolio.xirr([]) is None
    assert portfolio.xirr([("2025-01-01", -100_00), ("2026-01-01", 20_00)]) < -0.7


def test_account_performance_counts_only_what_crossed_the_boundary(conn, world):
    inv = world["inv"]
    p = portfolio.account_performance(conn, inv, "2025-01-01", "2025-12-31")
    assert (p.start_value, p.end_value, p.money_in, p.money_out, p.income) == \
        (0, 11200_00, 10000_00, 0, 200_00)
    assert p.gain == 1200_00
    assert 0.115 < p.irr < 0.125             # 10,000 -> 11,200 over 363 days
    # The transfer leg is one flow, not two (the ledger leg only).
    assert portfolio.external_flows(conn, inv, "2025-01-01", "2025-12-31") == \
        [("2025-01-02", 10000_00)]
    # A withdrawal recorded as an investment row is money out; a duplicate
    # ledger leg for the same transfer is not counted again.
    investments.record_investment(conn, inv, "2025-09-01", "XOut", amount=500_00,
                                  transfer_account_id=world["chk"])
    ledger.create_transfer(conn, inv, world["chk"], "2025-09-01", 500_00, payee="to checking")
    p2 = portfolio.account_performance(conn, inv, "2025-01-01", "2025-12-31")
    assert (p2.money_in, p2.money_out) == (10000_00, 500_00)


def test_security_performance_uses_buys_sales_and_cash_dividends(conn, world):
    inv = world["inv"]
    p = portfolio.security_performance(conn, inv, "AAPL", "2025-01-01", "2025-12-31")
    assert (p.start_value, p.end_value, p.money_in, p.money_out, p.income) == \
        (0, 11000_00, 10000_00, 200_00, 200_00)
    assert p.gain == 1200_00 and 0.115 < p.irr < 0.125
    # A later window starts from the shares' value the day before.
    investments.record_price(conn, "AAPL", "2025-12-30", "108.00")
    p3 = portfolio.security_performance(conn, inv, "AAPL", "2025-12-31", "2025-12-31")
    assert (p3.start_value, p3.end_value, p3.money_in) == (10800_00, 11000_00, 0)


# ---------------------------------------------------------------------------
# allocation
# ---------------------------------------------------------------------------
def test_allocation_groups_by_asset_class_security_and_account(conn, world):
    inv = world["inv"]
    ira = ledger.create_account(conn, "IRA", "investment", opening_balance=0)
    ledger.create_transfer(conn, world["chk"], ira, "2025-01-15", 5000_00, payee="Fund IRA")
    investments.record_investment(conn, ira, "2025-02-01", "Buy", symbol="BND",
                                  quantity="50", price="80.00", amount=-4000_00)
    investments.record_investment(conn, ira, "2025-02-01", "Buy", symbol="MYST",
                                  quantity="1", price="1.00", amount=-1_00)
    investments.record_price(conn, "BND", "2025-12-31", "80.00")
    investments.rebuild_holdings(conn, ira)
    portfolio.set_security(conn, "AAPL", name="Apple", sec_type="stock",
                           asset_class="domestic_stock")
    portfolio.set_security(conn, "BND", sec_type="etf", asset_class="bond")
    with pytest.raises(ValueError):
        portfolio.set_security(conn, "BND", asset_class="crypto")
    assert portfolio.get_security(conn, "AAPL")["name"] == "Apple"
    portfolio.set_security(conn, "AAPL", name="")             # clears just the name
    assert portfolio.get_security(conn, "AAPL")["asset_class"] == "domestic_stock"

    a = portfolio.allocation(conn, as_of="2025-12-31")
    ira_cash = 5000_00 - 4000_00 - 1_00
    assert a.total == 11200_00 + 4000_00 + ira_cash
    assert [(s.key, s.value) for s in a.by_class] == [
        ("domestic_stock", 11000_00), ("bond", 4000_00), ("cash", 200_00 + ira_cash)]
    assert abs(sum(s.pct for s in a.by_class) - 100.0) < 1e-9
    assert [(s.key, s.value) for s in a.by_security] == [("AAPL", 11000_00), ("BND", 4000_00)]
    assert [(s.label, s.value) for s in a.by_account] == \
        [("Brokerage", 11200_00), ("IRA", 4000_00 + ira_cash)]
    assert a.unpriced == ["MYST"]
    only = portfolio.allocation(conn, [ira], as_of="2025-12-31")
    assert [(s.key, s.value) for s in only.by_class] == [("bond", 4000_00), ("cash", ira_cash)]


def test_allocation_scope_reaches_cash_accounts_and_property_but_never_debt(conn, world):
    """Quicken allocates investment accounts only; Mammon widens the scope, so
    a house is part of the answer to "where is my money" once classified."""
    house = ledger.create_account(conn, "House", "asset", opening_balance=400000_00)
    car = ledger.create_account(conn, "Car", "asset", opening_balance=20000_00)
    ledger.create_account(conn, "Mortgage", "liability", opening_balance=-250000_00)
    ledger.create_account(conn, "Visa", "credit", opening_balance=-1500_00)
    portfolio.set_security(conn, "AAPL", asset_class="domestic_stock")
    chk_balance = ledger.account_balance(conn, world["chk"], "2025-12-31")

    inv_only = portfolio.allocation(conn, as_of="2025-12-31")           # the default
    assert [s.key for s in inv_only.by_class] == ["domestic_stock", "cash"]
    assert inv_only.scope == "investments" and inv_only.account_classes == {}

    with_cash = portfolio.allocation(conn, as_of="2025-12-31", scope="with_cash")
    # A cash-shaped account counts as cash with nothing said -- the rule that
    # already applied to a brokerage's idle cash, not a guess about the user.
    assert dict((s.key, s.value) for s in with_cash.by_class)["cash"] == 200_00 + chk_balance
    assert with_cash.account_classes == {world["chk"]: "cash"}

    every = portfolio.allocation(conn, as_of="2025-12-31", scope="everything")
    by_class = dict((s.key, s.value) for s in every.by_class)
    assert by_class["unclassified"] == 400000_00 + 20000_00      # never guessed
    assert every.account_classes[house] == "unclassified"
    # Debt is in no scope: an allocation is of what you own.
    assert "Mortgage" not in [s.label for s in every.by_account]
    assert "Visa" not in [s.label for s in every.by_account]
    assert every.total == inv_only.total + chk_balance + 420000_00

    portfolio.set_account_asset_class(conn, house, "real_estate")
    every = portfolio.allocation(conn, as_of="2025-12-31", scope="everything")
    by_class = dict((s.key, s.value) for s in every.by_class)
    assert by_class["real_estate"] == 400000_00 and by_class["unclassified"] == 20000_00
    assert portfolio.account_asset_class(ledger.get_account(conn, house)) == "real_estate"
    portfolio.set_account_asset_class(conn, house, None)          # back to unsaid
    assert portfolio.account_asset_class(ledger.get_account(conn, house)) == "unclassified"
    with pytest.raises(ValueError):
        portfolio.set_account_asset_class(conn, house, "crypto")
    with pytest.raises(ValueError):
        portfolio.allocation(conn, scope="nonsense")
    # Naming a liability outright still does not allocate it.
    mortgage = ledger.get_account_by_name(conn, "Mortgage")["id"]
    assert portfolio.allocation(conn, [mortgage], as_of="2025-12-31").total == 0


# ---------------------------------------------------------------------------
# option contracts are excluded from an allocation, and SAID SO (SRD 5.8e-9)
# ---------------------------------------------------------------------------
CALL = "ACME  260116C00050000"          # long, 2 contracts
PUT = "ACME  260116P00045000"           # short, 1 contract
TWIN = "ACME  260116C00055000"          # OSI-SHAPED but nobody classified it


def _security_row(conn, symbol):
    conn.execute("INSERT OR IGNORE INTO securities(symbol, name) VALUES (?,?)",
                 (symbol, symbol))
    conn.commit()


def _classify_option(conn, symbol, right="C", strike="50"):
    _security_row(conn, symbol)
    securities.set_kinds(conn, [dict(symbol=symbol, kind=instruments.Kind.OPTION.value,
                                     kind_source="user", multiplier="100",
                                     underlying="ACME", expiration="2026-01-16",
                                     strike=strike, option_right=right)])


@pytest.fixture
def contracts(conn, world):
    """The brokerage from ``world`` plus a long call, a short put, and a
    NULL-KIND twin whose symbol looks exactly like a contract's."""
    inv = world["inv"]
    _classify_option(conn, CALL, right="C", strike="50")
    _classify_option(conn, PUT, right="P", strike="45")
    _security_row(conn, TWIN)                       # left unclassified on purpose
    investments.record_investment(conn, inv, "2025-11-03", "Buy", symbol=CALL,
                                  quantity="2", price="3", amount=-600_00)
    investments.record_investment(conn, inv, "2025-11-03", "ShtSell", symbol=PUT,
                                  quantity="1", price="2", amount=200_00)
    investments.record_investment(conn, inv, "2025-11-03", "Buy", symbol=TWIN,
                                  quantity="5", price="4", amount=-20_00)
    investments.rebuild_holdings(conn, inv)
    for sym, px in ((CALL, "4"), (PUT, "3"), (TWIN, "5")):
        investments.record_price(conn, sym, "2025-12-31", px)
    portfolio.set_security(conn, "AAPL", asset_class="domestic_stock")
    return world


def test_allocation_leaves_option_contracts_out_and_names_them(conn, contracts):
    """A contract is not shares of its underlying, so it is neither counted as
    equity nor dropped in silence: it comes out of every number and is named,
    with its premium value, in a note shown beside the percentages."""
    inv = contracts["inv"]
    cash = 200_00 - 600_00 + 200_00 - 20_00          # dividend, then the three trades
    a = portfolio.allocation(conn, [inv], as_of="2025-12-31")

    # Not in the total, the classes, the securities or the account slice.
    assert a.total == 11000_00 + 25_00 + cash
    assert dict((s.key, s.value) for s in a.by_class) == \
        {"domestic_stock": 11000_00, "unclassified": 25_00, "cash": cash}
    assert [s.key for s in a.by_security] == ["AAPL", TWIN]
    assert [(s.label, s.value) for s in a.by_account] == [("Brokerage", a.total)]
    # ...and excluding them is not the same as pretending they are unpriced.
    assert a.unpriced == []

    # Named, with the premium value the allocation is NOT reporting: the long
    # call is worth 2 x 4.00 x 100, the short put is a liability of 1 x 3.00 x 100.
    assert a.excluded_options == [(CALL, 800_00), (PUT, -300_00)]
    assert a.option_value == 500_00
    note = a.options_note
    assert CALL in note and PUT in note
    assert "800.00" in note and "-300.00" in note and "500.00" in note
    assert "excluded" in note.lower()


def test_allocation_with_no_contracts_says_nothing_about_options(conn, world):
    """The note is empty when there is nothing to exclude -- no sentence about
    options ever appears beside an allocation that has none."""
    portfolio.set_security(conn, "AAPL", asset_class="domestic_stock")
    a = portfolio.allocation(conn, [world["inv"]], as_of="2025-12-31")
    assert a.excluded_options == [] and a.option_value == 0 and a.options_note == ""
    assert a.total == 11000_00 + 200_00


def test_a_null_kind_security_allocates_exactly_as_it_always_did(conn, contracts):
    """TWIN's symbol is an OSI contract string, and it is STILL allocated: kind
    IS NULL means unclassified, never "option" and never "equity". Forty years
    of imported history is NULL throughout, and this task changes none of it."""
    inv = contracts["inv"]
    a = portfolio.allocation(conn, [inv], as_of="2025-12-31")
    assert TWIN not in [sym for sym, _v in a.excluded_options]
    assert dict((s.key, s.value) for s in a.by_security)[TWIN] == 25_00   # 5 x 5.00, no multiplier
    assert investments.is_option(conn, TWIN) is False

    # Classify it and the same ledger reports it as a contract instead -- the
    # difference is the STATEMENT, nothing about the symbol or the trades.
    _classify_option(conn, TWIN, right="C", strike="55")
    b = portfolio.allocation(conn, [inv], as_of="2025-12-31")
    assert dict(b.excluded_options)[TWIN] == 2500_00     # now 5 x 5.00 x 100
    assert TWIN not in [s.key for s in b.by_security]
    assert b.total == a.total - 25_00                    # the 25.00 it used to add


def test_hidden_accounts_and_records_gaps_are_kept_out_of_the_way(conn, world):
    """Two ways an investment account reports pure CASH, and what each does.

    Hidden means "leave this out of my totals" (investments.net_worth), so the
    allocation must agree -- it used to disagree, counting money net worth did
    not. An account that is merely INCOMPLETE is still counted, because it is
    still the user's money, but it is NAMED: its balance lands wholly in Cash
    and is otherwise indistinguishable from a real cash position."""
    chk = world["chk"]
    # A 529 plan: contributions went in, the shares they bought were never
    # entered. Visible, so still counted -- but reported.
    plan = ledger.create_account(conn, "State529 -- Child A", "investment",
                                 opening_balance=0)
    ledger.create_transfer(conn, chk, plan, "2025-02-01", 46_000_00, payee="529")
    # An account the user has hidden as incomplete: out of the totals entirely.
    espp = ledger.create_account(conn, "ESPP (incomplete)", "investment",
                                 opening_balance=0)
    ledger.create_transfer(conn, chk, espp, "2025-02-01", 87_000_00, payee="ESPP")
    ledger.set_account_hidden(conn, espp, True)

    a = portfolio.allocation(conn, as_of="2025-12-31")
    names = [n for n, _ in a.cash_only_accounts]
    assert names == ["State529 -- Child A"]
    assert dict(a.cash_only_accounts)["State529 -- Child A"] == 46_000_00
    assert "ESPP (incomplete)" not in [s.label for s in a.by_account]

    # Opting back in is available, and matches what the old default did.
    fuller = portfolio.allocation(conn, as_of="2025-12-31", include_hidden=True)
    assert fuller.total == a.total + 87_000_00
    assert "ESPP (incomplete)" in [n for n, _ in fuller.cash_only_accounts]

    # A brokerage with real holdings AND idle cash is not a records gap.
    assert "Brokerage" not in names


# ---------------------------------------------------------------------------
# recent activity
# ---------------------------------------------------------------------------
def test_recent_activity_is_newest_first_and_names_its_account(conn, world):
    inv = world["inv"]
    second = ledger.create_account(conn, "Rollover IRA", "investment",
                                   opening_balance=0)
    investments.record_investment(conn, second, "2025-07-04", "Buy", symbol="MSFT",
                                  quantity="5", price="20.00", amount=-100_00)
    investments.rebuild_holdings(conn, second)

    rows = portfolio.recent_investment_activity(conn)
    assert [(r.date, r.account_name, r.action, r.symbol) for r in rows] == [
        ("2025-07-04", "Rollover IRA", "Buy", "MSFT"),
        ("2025-06-01", "Brokerage", "Div", "AAPL"),
        ("2025-01-03", "Brokerage", "Buy", "AAPL")]
    # Money stays signed cents; a quantity stays Decimal, and a cash-only row
    # (the dividend) has none at all rather than a zero that claims shares moved.
    assert [r.amount for r in rows] == [-100_00, 200_00, -10000_00]
    assert rows[0].quantity == Decimal("5")
    assert isinstance(rows[0].quantity, Decimal)
    assert rows[1].quantity is None
    # The checking account contributed nothing: this reads investment rows only.
    assert {r.account_id for r in rows} == {inv, second}


def test_recent_activity_honours_the_cap_and_an_empty_scope(conn, world):
    assert len(portfolio.recent_investment_activity(conn, limit=1)) == 1
    assert portfolio.recent_investment_activity(conn, limit=1)[0].date == "2025-06-01"
    # Nothing in scope, and nothing asked for: both are an empty list, never the
    # whole ledger.
    assert portfolio.recent_investment_activity(conn, account_ids=[]) == []
    assert portfolio.recent_investment_activity(conn, limit=0) == []
    assert portfolio.recent_investment_activity(conn, account_ids=[world["chk"]]) == []


def test_recent_activity_leaves_voided_rows_out(conn, world):
    inv = world["inv"]
    txn = investments.record_investment(conn, inv, "2025-08-01", "Sell",
                                        symbol="AAPL", quantity="10",
                                        price="110.00", amount=1100_00)
    investments.rebuild_holdings(conn, inv)
    assert portfolio.recent_investment_activity(conn)[0].id == txn

    assert investments.void_investment(conn, txn) is True
    investments.rebuild_holdings(conn, inv)
    rows = portfolio.recent_investment_activity(conn)
    assert txn not in [r.id for r in rows]
    # The cap still yields a full page: the void was filtered before the LIMIT,
    # not after it.
    assert len(portfolio.recent_investment_activity(conn, limit=2)) == 2


def test_recent_activity_skips_a_hidden_account_unless_asked(conn, world):
    ledger.set_account_hidden(conn, world["inv"], True)
    assert portfolio.recent_investment_activity(conn) == []
    assert len(portfolio.recent_investment_activity(conn, include_hidden=True)) == 2


# ---------------------------------------------------------------------------
# price freshness
# ---------------------------------------------------------------------------
def test_price_freshness_ages_against_the_given_date(conn, world):
    fresh = portfolio.price_freshness(conn, ["AAPL"], as_of="2026-01-30")
    assert [(f.symbol, f.latest, f.days) for f in fresh] == [
        ("AAPL", "2025-12-31", 30)]
    # An as-of BEFORE the newest close is zero days old, never negative.
    assert portfolio.price_freshness(conn, ["AAPL"], as_of="2025-06-30")[0].days == 0
    # The default reference point is the ledger's own latest known date.
    when = investments.valuation_as_of(conn)
    assert portfolio.price_freshness(conn, ["AAPL"])[0].days == (
        portfolio.price_freshness(conn, ["AAPL"], as_of=when)[0].days)


def test_price_freshness_reports_a_never_priced_symbol_as_unknown(conn, world):
    portfolio.set_security(conn, "ZZNP", name="Zeta Never Priced Fund")
    fresh = portfolio.price_freshness(conn, ["AAPL", "ZZNP"], as_of="2026-01-30")
    # Worst first: no price at all outranks merely old.
    assert [f.symbol for f in fresh] == ["ZZNP", "AAPL"]
    never = fresh[0]
    assert never.latest is None and never.days is None


def test_price_freshness_sorts_oldest_first_and_dedupes(conn, world):
    portfolio.set_security(conn, "ZZOLDER", name="Zeta Older Fund")
    investments.record_price(conn, "ZZOLDER", "2025-03-31", "5.00")
    fresh = portfolio.price_freshness(conn, ["AAPL", "ZZOLDER", "AAPL"],
                                      as_of="2026-01-30")
    assert [(f.symbol, f.days) for f in fresh] == [("ZZOLDER", 305), ("AAPL", 30)]


def test_price_freshness_follows_a_rename(conn, world):
    """A renamed ticker is ONE identity: the close recorded under the old
    spelling is the surviving symbol's newest close, not a missing price."""
    portfolio.set_security(conn, "ZZNEW", name="Zeta Renamed Fund")
    portfolio.set_security(conn, "ZZOLD", name="Zeta Renamed Fund (old ticker)")
    investments.record_price(conn, "ZZOLD", "2025-11-30", "9.00")
    investments.add_alias(conn, "ZZOLD", "ZZNEW")
    fresh = portfolio.price_freshness(conn, ["ZZNEW"], as_of="2025-12-31")
    assert (fresh[0].latest, fresh[0].days) == ("2025-11-30", 31)


def test_price_freshness_of_nothing_is_nothing(conn, world):
    assert portfolio.price_freshness(conn, []) == []
    assert portfolio.price_freshness(conn, [""], as_of="2026-01-30") == []

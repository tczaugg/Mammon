"""mammon.securities: one identity per security, plus the quote-keying fixes.

The bug these hold down, end to end: the same ETF arrived as
"VGT VANGUARD INFO TECH ETF" from a QIF and "VGT" from an Interactive Brokers
CSV, so the position, its dividends and its prices split across two names and a
chart drew two points. Alongside it, three quote defects that made the file
uncorrectable -- a phantom series keyed by ticker, a provider series on the
dividend-adjusted scale, and an upsert that could never replace either.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from mammon import db, investments, ledger, securities


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "sec.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "IB IRA", "investment")


# ---------------------------------------------------------------------------
# suggesting a split
# ---------------------------------------------------------------------------
def test_broker_style_name_splits_into_ticker_and_description():
    s = securities.suggest("VGT VANGUARD INFO TECH ETF")
    assert (s.symbol, s.name) == ("VGT", "VANGUARD INFO TECH ETF")
    assert s.changes_key


def test_a_bare_ticker_is_already_its_identity_and_invents_no_name():
    s = securities.suggest("FIPDX")
    assert (s.symbol, s.name) == ("FIPDX", None)
    assert not s.changes_key


def test_a_plan_fund_with_no_ticker_keeps_its_name_as_identity():
    """Inventing a ticker for an internally-named fund is worse than the problem:
    ticker_of('TARGET 2030 FUND') is '' precisely so nothing fetches TARGET."""
    s = securities.suggest("DOMESTIC BOND INDEX")
    assert (s.symbol, s.name) == ("DOMESTIC BOND INDEX", "DOMESTIC BOND INDEX")
    assert not s.changes_key


def test_the_dangerous_case_is_still_only_a_suggestion():
    """'FID BALANCED K6' yields ticker 'FID' and 'INTL EQUITY INDEX' yields
    'INTL' -- a real listed company. suggest() may propose these; nothing in
    this module applies a split the caller did not hand back."""
    assert securities.suggest("FID BALANCED K6").symbol == "FID"
    assert securities.suggest("INTL EQUITY INDEX").symbol == "INTL"


# ---------------------------------------------------------------------------
# the security master: a stated ticker beats a derived one
# ---------------------------------------------------------------------------
# Shape taken verbatim from Quicken's own export (D:\QLite\data\mammon_2025.qif):
# a security block carries N name, S ticker and T type, and OMITS S for a fund
# with no public ticker. 533 of its 614 securities state a ticker.
QIF_MASTER = [
    ("VGT VANGUARD INFO TECH ETF", "VGT", "Stock"),
    ("QYLD GLOBAL X NASD 100 COV CALL", "QYLD", "Stock"),
    ("PWE PENN WEST ENERGY TRUST ORD SHR", "PWE", "Stock"),
    ("PWE PENN WEST PETROLEUM LTD", "PWE", "Stock"),
    ("FID BALANCED K6", "", "Mutual Fund"),          # no S in the export
    ("INTL EQUITY INDEX", "", "Mutual Fund"),        # no S in the export
]


def test_a_stated_ticker_is_used_verbatim_and_needs_no_guess(conn):
    securities.record_master(conn, QIF_MASTER)
    s = securities.suggest("VGT VANGUARD INFO TECH ETF",
                           securities.recorded_ticker(conn, "VGT VANGUARD INFO TECH ETF"))
    assert (s.symbol, s.name, s.confident) == ("VGT", "VANGUARD INFO TECH ETF", True)


def test_an_omitted_ticker_is_a_STATEMENT_that_there_is_none(conn):
    """The case that makes this worth a column. ticker_of('INTL EQUITY INDEX')
    is 'INTL' -- a real listed company already priced in this file. Quicken
    omits S for such a fund, and that omission has to survive the round trip or
    the import hands the security straight back to the heuristic."""
    securities.record_master(conn, QIF_MASTER)
    for name in ("FID BALANCED K6", "INTL EQUITY INDEX"):
        assert securities.recorded_ticker(conn, name) == ""      # recorded absence
        s = securities.suggest(name, securities.recorded_ticker(conn, name))
        assert s.symbol == name and not s.changes_key


def test_a_security_no_source_has_described_is_still_only_a_guess(conn):
    """None means nobody said, so the leading token is a GUESS and must be
    confirmed -- this is the exact string the guess gets wrong."""
    assert securities.recorded_ticker(conn, "FID BALANCED K6") is None
    guess = securities.suggest("FID BALANCED K6")
    assert guess.symbol == "FID" and guess.confident is False
    # A name with no ticker-shaped leading token needs no confirmation: leaving
    # it as its own identity is always safe.
    assert securities.suggest("MYSTERY GROWTH FUND").confident is True


def test_suggest_all_prefers_the_recorded_ticker_over_the_heuristic(conn, account):
    securities.record_master(conn, QIF_MASTER)
    _buy(conn, account, "INTL EQUITY INDEX", "2021-05-03", 5, "12.00")
    _buy(conn, account, "VGT VANGUARD INFO TECH ETF", "2021-05-03", 5, "380.00")
    got = {s.old: s for s in securities.suggest_all(conn)}
    assert got["INTL EQUITY INDEX"].symbol == "INTL EQUITY INDEX"   # not INTL
    assert got["VGT VANGUARD INFO TECH ETF"].symbol == "VGT"


def test_a_later_source_silent_on_the_ticker_cannot_blank_a_known_one(conn):
    securities.record_master(conn, [("VGT VANGUARD INFO TECH ETF", "VGT", "Stock")])
    securities.record_master(conn, [("VGT VANGUARD INFO TECH ETF", "", None)])
    assert securities.recorded_ticker(conn, "VGT VANGUARD INFO TECH ETF") == "VGT"


def test_the_security_type_is_kept_too(conn):
    """`T` was parsed and discarded along with `S`; securities.sec_type has
    never been populated by any import."""
    securities.record_master(conn, QIF_MASTER)
    row = conn.execute("SELECT sec_type FROM securities WHERE symbol=?",
                       ("FID BALANCED K6",)).fetchone()
    assert row["sec_type"] == "Mutual Fund"


def test_recording_the_master_renames_nothing(conn, account):
    """An import records what the source said; it does not restate the user's
    securities behind their back."""
    _buy(conn, account, "VGT VANGUARD INFO TECH ETF", "2021-05-03", 5, "380.00")
    securities.record_master(conn, QIF_MASTER)
    rows = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM investment_transactions")]
    assert rows == ["VGT VANGUARD INFO TECH ETF"]


def test_the_renamed_company_still_merges_on_its_stated_ticker(conn):
    """Quicken gives both Penn West spellings S=PWE, so the merge is the
    source's own judgement, not ours."""
    securities.record_master(conn, QIF_MASTER)
    splits = [securities.suggest(n, securities.recorded_ticker(conn, n))
              for n in ("PWE PENN WEST ENERGY TRUST ORD SHR",
                        "PWE PENN WEST PETROLEUM LTD")]
    assert {s.symbol for s in splits} == {"PWE"}
    assert securities.collisions(splits) == {
        "PWE": ["PWE PENN WEST ENERGY TRUST ORD SHR", "PWE PENN WEST PETROLEUM LTD"]}


def test_fetch_ticker_prices_a_name_that_IS_a_ticker(conn):
    """Quicken's block for these funds is bare -- NFIPDX / TStock, no S line --
    because the user entered them by their symbol. That is an incomplete record,
    not a statement that no ticker exists, and the name is a single token so
    nothing is being guessed."""
    securities.record_master(conn, [("FIPDX", "", "Stock"), ("FXAIX", "", "Stock")])
    assert securities.fetch_ticker(conn, "FIPDX") == "FIPDX"
    assert securities.fetch_ticker(conn, "FXAIX") == "FXAIX"


def test_fetch_ticker_refuses_to_guess_at_a_multi_word_name(conn):
    """The damage case: a provider will happily price INTL and SP, with a
    stranger's numbers, against a retirement fund."""
    securities.record_master(conn, [("INTL EQUITY INDEX", "", "Mutual Fund")])
    assert securities.fetch_ticker(conn, "INTL EQUITY INDEX") is None
    # and with no master row at all, still no guess
    assert securities.fetch_ticker(conn, "SP 500 INDEX PL CL D") is None
    assert securities.fetch_ticker(conn, "DOMESTIC BOND INDEX") is None


def test_fetch_ticker_uses_a_stated_ticker_over_everything(conn):
    securities.record_master(conn, [("VGT VANGUARD INFO TECH ETF", "VGT", "Stock")])
    assert securities.fetch_ticker(conn, "VGT VANGUARD INFO TECH ETF") == "VGT"


def test_collisions_name_the_securities_that_would_merge():
    splits = [securities.suggest(x) for x in
              ("VGT", "VGT VANGUARD INFO TECH ETF", "ARKK ARK INNOVATION ETF")]
    assert securities.collisions(splits) == {
        "VGT": ["VGT", "VGT VANGUARD INFO TECH ETF"]}


# ---------------------------------------------------------------------------
# applying it
# ---------------------------------------------------------------------------
def _buy(conn, account, symbol, date, qty, price):
    """One Buy, holdings rebuilt -- record_investment deliberately leaves the
    rebuild to the caller so a batch import does it once."""
    rid = investments.record_investment(
        conn, account, date, "Buy", symbol=symbol,
        quantity=str(qty), price=str(price),
        amount=-int(Decimal(str(qty)) * Decimal(str(price)) * 100))
    investments.rebuild_holdings(conn, account)
    return rid


def test_merging_two_spellings_unifies_the_position(conn, account):
    """The whole bug: shares under the long name, dividends under the ticker."""
    _buy(conn, account, "VGT VANGUARD INFO TECH ETF", "2021-05-03", 20, "380.00")
    investments.record_investment(conn, account, "2026-03-26", "Div",
                                  symbol="VGT", amount=4200)
    report = securities.apply_splits(
        conn, [securities.suggest("VGT VANGUARD INFO TECH ETF")])
    assert "VGT" in report["merged"]

    rows = [r["symbol"] for r in conn.execute(
        "SELECT symbol FROM investment_transactions WHERE account_id=?", (account,))]
    assert set(rows) == {"VGT"}
    held = {h["symbol"]: h for h in investments.list_holdings(conn, account)}
    assert set(held) == {"VGT"}
    assert Decimal(held["VGT"]["quantity"]) == 20


def test_the_description_lands_in_the_columns_built_for_it(conn, account):
    _buy(conn, account, "VGT VANGUARD INFO TECH ETF", "2021-05-03", 20, "380.00")
    securities.apply_splits(conn, [securities.suggest("VGT VANGUARD INFO TECH ETF")])
    assert securities.name_of(conn, "VGT") == "VANGUARD INFO TECH ETF"
    row = conn.execute("SELECT name FROM holdings WHERE symbol='VGT'").fetchone()
    assert row["name"] == "VANGUARD INFO TECH ETF"
    assert securities.display(conn, "VGT") == "VGT -- VANGUARD INFO TECH ETF"


def test_price_series_merge_and_the_existing_identity_wins(conn, account):
    """Both spellings may hold the same date. The row already under the new
    identity survives, matching investments._migrate_price_history."""
    _buy(conn, account, "VGT VANGUARD INFO TECH ETF", "2021-05-03", 20, "380.00")
    investments.record_price(conn, "VGT VANGUARD INFO TECH ETF", "2021-05-03",
                             "380.00", "qif")
    investments.record_price(conn, "VGT VANGUARD INFO TECH ETF", "2026-09-04",
                             "121.00", "qif")
    investments.record_price(conn, "VGT", "2026-09-04", "999.00", "yfinance")
    securities.apply_splits(conn, [securities.suggest("VGT VANGUARD INFO TECH ETF")])

    series = dict(investments.price_history(conn, "VGT"))
    assert series["2021-05-03"] == Decimal("380.00")
    assert series["2026-09-04"] == Decimal("999.00")     # incumbent wins
    assert investments.price_history(conn, "VGT VANGUARD INFO TECH ETF") == []


def test_a_merge_spans_every_account_not_just_one(conn):
    """investments.apply_security_renames is account-scoped on purpose; a merge
    is the opposite -- leaving another account on the old spelling IS the bug."""
    a = ledger.create_account(conn, "IB One", "investment")
    b = ledger.create_account(conn, "IB Two", "investment")
    _buy(conn, a, "QYLD GLOBAL X NASD 100 COV CALL", "2021-05-03", 10, "22.62")
    _buy(conn, b, "QYLD GLOBAL X NASD 100 COV CALL", "2021-05-03", 10, "22.62")
    securities.apply_splits(
        conn, [securities.suggest("QYLD GLOBAL X NASD 100 COV CALL")])
    left = conn.execute(
        "SELECT COUNT(*) n FROM investment_transactions "
        "WHERE symbol LIKE 'QYLD %'").fetchone()["n"]
    assert left == 0
    for acct in (a, b):
        assert [h["symbol"] for h in investments.list_holdings(conn, acct)] == ["QYLD"]


def test_a_rename_onto_an_existing_identity_is_reported_as_a_merge(conn, account):
    """The case `collisions` cannot see. The bare "VGT" needs no change of its
    own, so it is never among the proposals -- but renaming the long spelling
    onto it merges two securities, and describing that as a simple rename
    understates the one change here that re-running cannot undo."""
    _buy(conn, account, "VGT VANGUARD INFO TECH ETF", "2021-05-03", 20, "380.00")
    investments.record_investment(conn, account, "2026-03-26", "Div",
                                  symbol="VGT", amount=4200)
    only_the_change = [securities.suggest("VGT VANGUARD INFO TECH ETF")]
    assert securities.collisions(only_the_change) == {}          # blind to it
    assert securities.merge_preview(conn, only_the_change) == {
        "VGT": ["VGT", "VGT VANGUARD INFO TECH ETF"]}


def test_merge_preview_leaves_a_plain_rename_alone(conn, account):
    _buy(conn, account, "ARKK ARK INNOVATION ETF", "2021-05-03", 5, "100.00")
    assert securities.merge_preview(
        conn, [securities.suggest("ARKK ARK INNOVATION ETF")]) == {}


def test_a_merge_recomputes_year_end_snapshots_rather_than_renaming_them(conn,
                                                                         account):
    """holdings_checkpoints is PRIMARY KEY(account_id, year, symbol), so a merge
    collides on the first year both spellings held the security -- and renaming
    would be wrong anyway, since a merged position's year-end state is the
    combined replay, not either half's."""
    _buy(conn, account, "PWE PENN WEST ENERGY TRUST ORD SHR", "2009-05-07", 10, "20.00")
    _buy(conn, account, "PWE PENN WEST PETROLEUM LTD", "2009-08-14", 10, "22.00")
    securities.apply_splits(conn, [
        securities.suggest("PWE PENN WEST ENERGY TRUST ORD SHR"),
        securities.suggest("PWE PENN WEST PETROLEUM LTD")])
    rows = conn.execute(
        "SELECT symbol, quantity FROM holdings_checkpoints "
        "WHERE account_id=? AND year=2009", (account,)).fetchall()
    assert [r["symbol"] for r in rows] == ["PWE"]
    assert Decimal(rows[0]["quantity"]) == 20          # both halves, replayed


def test_suggest_all_sees_securities_no_holding_row_mentions(conn, account):
    _buy(conn, account, "ARKK ARK INNOVATION ETF", "2021-05-03", 5, "100.00")
    investments.record_price(conn, "ORPHAN FUND", "2020-01-01", "10.00", "qif")
    olds = {s.old for s in securities.suggest_all(conn)}
    assert "ARKK ARK INNOVATION ETF" in olds
    assert "ORPHAN FUND" in olds


# ---------------------------------------------------------------------------
# the three quote defects
# ---------------------------------------------------------------------------
class _FakeQuotes:
    source_name = "yfinance"

    def __init__(self, price="121.00", date="2026-09-04"):
        self.price, self.date = price, date

    def get_quotes(self, symbols):
        return [investments.Quote(s, self.date, self.price, "yfinance")
                for s in symbols]


def test_a_quote_lands_on_the_holding_name_and_leaves_no_phantom(conn, account):
    """fetch_quotes wrote under the TICKER it was handed, so a fetch for ALTY
    filed a price against 'ALTY' -- a name nothing is stored under -- and left a
    two-row series that charted instead of the real one."""
    name = "ALTY GLOBAL X SUPERDIVIDEND ALTER"
    _buy(conn, account, name, "2021-05-03", 10, "11.69")
    investments.fetch_quotes(conn, ["ALTY"], source=_FakeQuotes(),
                             names={"ALTY": [name]})
    assert [d for d, _p in investments.price_history(conn, name)] == ["2026-09-04"]
    assert investments.price_history(conn, "ALTY") == []


def test_without_a_mapping_a_quote_still_prices_the_name_it_was_given(conn, account):
    """A holding genuinely stored under its ticker must still price."""
    _buy(conn, account, "FIPDX", "2021-05-03", 10, "11.69")
    investments.fetch_quotes(conn, ["FIPDX"], source=_FakeQuotes())
    assert len(investments.price_history(conn, "FIPDX")) == 1


def test_a_refetch_replaces_a_downloaded_row_but_not_a_transaction_price(conn):
    """The reason re-downloading could never fix the adjusted series."""
    investments.record_price(conn, "VGT", "2026-08-01", "15.00", "yfinance")
    investments.record_price(conn, "VGT", "2026-08-02", "380.00", "txn")
    written = investments.record_prices_if_absent(
        conn,
        [("VGT", "2026-08-01", "120.00", "yfinance"),
         ("VGT", "2026-08-02", "999.00", "yfinance")],
        replace_sources=investments.REFETCHABLE_SOURCES)
    series = dict(investments.price_history(conn, "VGT"))
    assert series["2026-08-01"] == Decimal("120.00")     # download corrected
    assert series["2026-08-02"] == Decimal("380.00")     # as-paid protected
    assert written == 1


def test_the_default_still_never_overwrites(conn):
    """record_position_prices and the transaction-price path both promise this."""
    investments.record_price(conn, "VGT", "2026-08-01", "15.00", "yfinance")
    written = investments.record_prices_if_absent(
        conn, [("VGT", "2026-08-01", "120.00", "ofx-pos")])
    assert dict(investments.price_history(conn, "VGT"))["2026-08-01"] == Decimal("15.00")
    assert written == 0


def test_the_written_count_is_what_landed_not_what_was_offered(conn):
    """It returned len(payload) regardless, so a re-run that inserted nothing
    still reported filling in hundreds of prices -- which is how the
    uncorrectable-row bug stayed hidden."""
    rows = [("VGT", "2026-08-0%d" % i, "10.00", "yfinance") for i in range(1, 5)]
    assert investments.record_prices_if_absent(conn, rows) == 4
    assert investments.record_prices_if_absent(conn, rows) == 0


def test_names_by_ticker_maps_a_fetch_onto_stored_rows(conn):
    got = securities.names_by_ticker(
        conn, ["VGT VANGUARD INFO TECH ETF", "FIPDX", "DOMESTIC BOND INDEX"])
    assert got["VGT"] == ["VGT VANGUARD INFO TECH ETF"]
    assert got["FIPDX"] == ["FIPDX"]
    assert got["DOMESTIC BOND INDEX"] == ["DOMESTIC BOND INDEX"]

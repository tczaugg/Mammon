"""Option contracts coming IN: what a source states, what it leaves unsaid, and
the multiplier that stands between quantity x price and a 100x error.

Three separate claims are tested here, because they fail in three different
ways:

* A QIF ``!Type:Security`` block STATES the kind ("Option") and the terms (the
  ``N``/``S`` strings). Stated facts are recorded as stated -- and a pre-2010
  symbol, which physically cannot state the strike or the year, lands PARTIAL
  with those absences NAMED rather than filled in with a plausible number.
  A guessed strike is indistinguishable from a known one once it is in a column.
* The multiplier is the factor between quantity x price and cash, and a source's
  OWN totals say what it is. A broker counts CONTRACTS at a per-share premium --
  two contracts at 1.75 cost $350, x100 -- so every place that derives or
  validates ``amount`` from ``quantity x price`` multiplies by it. Quicken's QIF
  counts so that T = Q x I, so for its options the factor is 1
  (``securities.observed_multiplier``, section (e)).
* A broker writes one contract at least four ways. A column that says "the text
  here identifies a contract" is what lets those spellings collapse onto one
  canonical OSI symbol -- and specifically NOT onto the underlying's ticker,
  which is sitting in the next column on the same row.

Every fixture here is synthetic (XYZ / ACME, no account numbers).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal

import pytest

from mammon import db, importers, instruments, ledger, securities
from mammon.importers import csvimp
from mammon.importers.record import derive_investment_amounts


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    ledger.create_account(c, "Brokerage", "investment", opening_balance=0)
    yield c
    c.close()


def _import(conn, tmp_path, text, name="broker.qif"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return importers.import_file(conn, str(path), account="Brokerage",
                                 account_type="investment")


def _security(conn, symbol):
    return conn.execute(
        "SELECT kind, kind_source, multiplier, underlying, expiration, strike, "
        "       option_right FROM securities WHERE symbol=?", (symbol,)).fetchone()


# A modern contract in a QIF whose totals count CONTRACTS at a per-share premium
# (2 x 1.75 x 100 = 350) -- the shape a broker feed uses, and Mammon's own export
# of downloaded rows. The type word states the kind, the name carries the OSI
# symbol, and S is the ROOT (not the contract) -- which is exactly why the root
# must never become the identity. Quicken's OWN exports are the other shape; see
# _QUICKEN_SHAPE_QIF below.
_MODERN_QIF = (
    "!Type:Security\n"
    "NXYZ 260117C00150000\n"
    "SXYZ\n"
    "TOption\n"
    "^\n"
    "!Type:Invst\n"
    "D01/20/2026\nNBuy\nYXYZ 260117C00150000\nQ2\nI1.75\nT350.00\n^\n"
)

# The same file as a 2007 Quicken export wrote it. ACMEKS is the whole symbol:
# root ACME, expiry month code K, strike code S. The day, the year and the
# strike itself are NOT IN THE STRING -- the strike letter meant different
# dollars for different strike intervals, from a table the file does not carry.
_LEGACY_QIF = (
    "!Type:Security\n"
    "NACME Nov Call\n"
    "SACMEKS\n"
    "TOption\n"
    "^\n"
    "!Type:Invst\n"
    "D11/02/2007\nNBuy\nYACME Nov Call\nQ1\nT205.00\n^\n"
)


# ---------------------------------------------------------------------------
# (a) a stated option lands classified, with its terms
# ---------------------------------------------------------------------------
def test_qif_type_word_classifies_the_security_as_stated(conn, tmp_path):
    """``T Option`` is a STATEMENT, so kind_source is 'source' -- not 'derived',
    which is what a shape test on the symbol alone would have earned."""
    res = _import(conn, tmp_path, _MODERN_QIF)
    assert res.added == 1

    row = _security(conn, "XYZ 260117C00150000")
    assert row is not None, "the security master was not recorded at all"
    assert row["kind"] == "option"
    assert row["kind_source"] == "source"


def test_qif_option_terms_are_parsed_from_the_n_and_s_strings(conn, tmp_path):
    _import(conn, tmp_path, _MODERN_QIF)
    row = _security(conn, "XYZ 260117C00150000")
    assert row["underlying"] == "XYZ"
    assert row["expiration"] == "2026-01-17"
    assert row["strike"] == "150"
    assert row["option_right"] == "C"
    assert row["multiplier"] == "100"


def test_a_stock_stays_unclassified(conn, tmp_path):
    """NULL kind means UNCLASSIFIED, never equity. Nothing in this work
    classifies the instruments it has no defect to fix in."""
    _import(conn, tmp_path,
            "!Type:Security\nNACME INC\nSACME\nTStock\n^\n"
            "!Type:Invst\nD01/20/2026\nNBuy\nYACME INC\nQ10\nI15.00\nT150.00\n^\n")
    row = _security(conn, "ACME INC")
    assert row["kind"] is None
    assert row["kind_source"] is None


def test_an_import_never_reclassifies_an_already_classified_security(conn, tmp_path):
    """Only NULLs are filled. A user's (or an earlier source's) classification
    survives every later import of the same security untouched -- reclassifying
    existing rows is a separate, deliberate act."""
    _import(conn, tmp_path, _MODERN_QIF)
    conn.execute("UPDATE securities SET kind='option', kind_source='user', "
                 "multiplier='10' WHERE symbol=?", ("XYZ 260117C00150000",))
    conn.commit()

    _import(conn, tmp_path, _MODERN_QIF, name="broker2.qif")

    row = _security(conn, "XYZ 260117C00150000")
    assert row["kind_source"] == "user"
    assert row["multiplier"] == "10"


# ---------------------------------------------------------------------------
# (b) a pre-2010 symbol lands partial, and SAYS which terms are unknown
# ---------------------------------------------------------------------------
def test_legacy_option_lands_partial_rather_than_guessed(conn, tmp_path):
    """The strike, the expiration date and the multiplier are not in a 2007
    symbol. They stay NULL -- a plausible 100 or a decoded strike would read as
    a recorded fact forever after."""
    _import(conn, tmp_path, _LEGACY_QIF)

    row = _security(conn, "ACME Nov Call")
    assert row["kind"] == "option"
    assert row["kind_source"] == "source"
    assert row["underlying"] == "ACME"
    assert row["option_right"] == "C"
    assert row["strike"] is None
    assert row["expiration"] is None
    assert row["multiplier"] is None


def test_legacy_unknown_terms_are_named_not_merely_absent():
    """The NULLs above are an ABSENCE OF KNOWLEDGE, and classify_source says
    which ones -- otherwise they are indistinguishable from a stock's NULL
    expiration, which means "never expires"."""
    k = securities.classify_source("ACME Nov Call", "ACMEKS", "Option")
    assert k is not None
    assert k.kind == "option"
    assert set(k.unknown) == {"strike", "expiration", "multiplier"}

    modern = securities.classify_source("XYZ 260117C00150000", "XYZ", "Option")
    assert modern.unknown == ()


def test_an_unreadable_option_symbol_marks_every_term_unknown():
    """The source said "Option" and then said nothing this code can read. That
    is still a classification -- and an admission of five unknowns."""
    k = securities.classify_source("SOME CONTRACT", "", "Option")
    assert k.kind == "option"
    assert k.kind_source == "source"
    assert set(k.unknown) == {"underlying", "expiration", "strike",
                              "option_right", "multiplier"}


# ---------------------------------------------------------------------------
# (c) amount == quantity x price x multiplier, everywhere it is derived
# ---------------------------------------------------------------------------
def test_contract_multiplier_reads_kind_beside_multiplier(conn, tmp_path):
    """A NULL multiplier means ONE for a share and UNSTATED for an option, so
    the column is never read without ``kind`` beside it."""
    _import(conn, tmp_path, _MODERN_QIF)
    _import(conn, tmp_path, _LEGACY_QIF, name="legacy.qif")

    assert securities.contract_multiplier(conn, "XYZ 260117C00150000") == Decimal(100)
    assert securities.contract_multiplier(conn, "ACME Nov Call") is instruments.UNKNOWN
    # Never-seen security, and a share: one, which is what the arithmetic has
    # always assumed.
    assert securities.contract_multiplier(conn, "NOT A SECURITY") == Decimal(1)


def test_qif_option_amount_agrees_with_quantity_times_price_times_multiplier(conn, tmp_path):
    """The QIF states the cash and this import records it verbatim; the check is
    that the stated cash is the one the multiplier explains. Without the
    multiplier the same row reads as $3.50 and the real $350 looks like an
    error."""
    _import(conn, tmp_path, _MODERN_QIF)
    row = conn.execute(
        "SELECT symbol, quantity, price, amount FROM investment_transactions"
    ).fetchone()

    m = securities.contract_multiplier(conn, row["symbol"])
    expected = Decimal(row["quantity"]) * Decimal(row["price"]) * m
    assert abs(Decimal(row["amount"])) == expected * 100     # cents
    assert abs(row["amount"]) == 35000


def test_import_derivation_multiplies_by_the_contract_size():
    q, p, cents, ok = derive_investment_amounts("2", "1.75", None, multiplier=100)
    assert (q, p, cents, ok) == ("2", "1.75", 35000, True)
    # A share is the unchanged arithmetic.
    assert derive_investment_amounts("2", "1.75", None)[2] == 350


def test_import_validation_does_not_call_a_correct_option_total_inconsistent():
    _q, _p, cents, ok = derive_investment_amounts("2", "1.75", "350.00", multiplier=100)
    assert (cents, ok) == (35000, True)
    # ... and still catches a total that the multiplier does NOT explain.
    assert derive_investment_amounts("2", "1.75", "3.50", multiplier=100)[3] is False


def test_an_unstated_multiplier_derives_nothing_and_flags_nothing():
    """An inconsistency indistinguishable from an unknown contract size is not a
    finding to put in front of a user: the source's own total stands."""
    _q, _p, cents, ok = derive_investment_amounts(
        "1", "2.05", "205.00", multiplier=instruments.UNKNOWN)
    assert (cents, ok) == (20500, True)
    # Nothing is invented when the total is the missing value.
    assert derive_investment_amounts("1", "2.05", None,
                                     multiplier=instruments.UNKNOWN)[2] is None


def test_manual_entry_solver_uses_the_multiplier():
    """The other derivation site: the investment-transaction dialog's pure
    Quantity/Price/Amount solver."""
    from mammon.ui.widgets import InvestmentTransactionDialog as D

    two, premium = Decimal("2"), Decimal("1.75")
    assert D.resolve_qpa(two, premium, None, Decimal(100)) == (
        two, premium, 35000, "computed_amount")
    # Unchanged for everything that is not a contract.
    assert D.resolve_qpa(two, premium, None)[2] == 350

    assert D.resolve_qpa(two, premium, 35000, Decimal(100))[3] == "consistent"
    assert D.resolve_qpa(two, premium, 35000)[3] == "conflict"

    # Back out the premium from the cash, contract size included.
    q, p, a, status = D.resolve_qpa(two, None, 35000, Decimal(100))
    assert status == "computed_price" and p == Decimal("1.75")


def test_manual_entry_refuses_to_invent_cash_it_cannot_derive():
    """With no stated contract size nothing is derived and nothing is called a
    conflict -- except the one case where the cash is what is missing, which is
    the value that cannot be recovered from quantity and price at all."""
    from mammon.ui.widgets import InvestmentTransactionDialog as D

    one, premium = Decimal("1"), Decimal("2.05")
    assert D.resolve_qpa(one, premium, 20500, instruments.UNKNOWN) == (
        one, premium, 20500, "unstated")
    assert D.resolve_qpa(one, premium, None, instruments.UNKNOWN)[3] == "needs_amount"


def test_srd_records_where_the_multiplier_has_to_be_applied():
    """The inventory is the requirement: a THIRD derivation site is how this
    comes back."""
    from pathlib import Path

    srd = Path(__file__).resolve().parents[2] / "docs" / "SRD.md"
    text = srd.read_text(encoding="utf-8")
    assert "derive_investment_amounts" in text
    assert "resolve_qpa" in text
    assert "contract_multiplier" in text


# ---------------------------------------------------------------------------
# (d) the broker-CSV "option symbol" column semantic
# ---------------------------------------------------------------------------
_CSV_SPELLINGS = (
    "Date,Symbol,Option Symbol,Action,Quantity,Price,Amount\n"
    "01/20/2026,XYZ,XYZ 01/17/2026 150.00 C,Buy,2,1.75,350.00\n"
    "01/21/2026,XYZ,-XYZ260117C150,Buy,1,1.80,180.00\n"
    "01/22/2026,XYZ,XYZ 260117C00150000,Buy,1,1.90,190.00\n"
    "01/23/2026,XYZ,CALL XYZ 01/17/26 150,Buy,1,2.00,200.00\n"
)


def test_option_symbol_column_normalizes_every_spelling_to_one_contract():
    """Four ways of writing one contract, one identity. Without the column
    semantic these are four securities, four holdings and four price series."""
    records = csvimp.parse_csv(_CSV_SPELLINGS, default_account="Brokerage")
    assert len(records) == 4
    assert {r.symbol for r in records} == {"XYZ   260117C00150000"}


def test_the_option_column_wins_over_the_underlyings_ticker():
    """"Symbol" on the same row says XYZ. Taking it would file the contract's
    activity against the stock -- the fusion this guard exists to prevent."""
    records = csvimp.parse_csv(_CSV_SPELLINGS, default_account="Brokerage")
    assert all(r.symbol != "XYZ" for r in records)


def test_option_rows_price_the_contract_not_the_share():
    """2 contracts at 1.75 is $350: the amount the broker states, and the one
    the import arrives at from quantity and price."""
    records = csvimp.parse_csv(_CSV_SPELLINGS, default_account="Brokerage")
    assert records[0].amount_cents == 35000

    no_total = (
        "Date,Option Symbol,Action,Quantity,Price\n"
        "01/20/2026,XYZ 01/17/2026 150.00 C,Buy,2,1.75\n")
    derived = csvimp.parse_csv(no_total, default_account="Brokerage")
    assert derived[0].amount_cents == 35000


def test_an_unreadable_option_spelling_keeps_the_sources_own_text():
    """An invented contract is worse than an unparsed one: the text is kept as
    the identity, the multiplier is UNSTATED, and the stated cash stands."""
    text = ("Date,Option Symbol,Action,Quantity,Price,Amount\n"
            "01/20/2026,XYZ WEEKLY SPECIAL,Buy,2,1.75,350.00\n")
    records = csvimp.parse_csv(text, default_account="Brokerage")
    assert records[0].symbol == "XYZ WEEKLY SPECIAL"
    assert records[0].amount_cents == 35000


def test_option_contract_reports_what_it_could_not_read():
    assert csvimp.option_contract({"Option Symbol": ""}) is None

    symbol, terms = csvimp.option_contract({"Contract Symbol": "-XYZ260117C150"})
    assert symbol == "XYZ   260117C00150000"
    assert terms.multiplier == 100

    symbol, terms = csvimp.option_contract({"Option Symbol": "XYZ WEEKLY SPECIAL"})
    assert terms is None, "an unreadable spelling must not parse to a contract"


# ---------------------------------------------------------------------------
# (e) Quicken's own shape: T = Q x I, whatever Q counted
# ---------------------------------------------------------------------------
# The QIF specification defines Q as "number of shares", I as the price and T as
# the amount, and real Quicken exports follow it for options too: every option
# trade in decades of them has T = Q x I -- shares at a per-share premium, or
# contracts at a per-contract price. Reading the contract size (100) from the
# symbol and multiplying by it valued those positions a hundred times too high.
# The name below uses the standard symbol's PADDED root, as Quicken writes it.
_OPT = "ACME  260417C00045000 ACME 17APR26 45.0 C"
_QUICKEN_SHAPE_QIF = (
    "!Type:Security\n"
    f"N{_OPT}\nSACME  260417C00045000\nTOption\n^\n"
    "NACME INC\nSACME\nTStock\n^\n"
    "!Type:Prices\n"
    '"ACME  260417C00045000",4.00," 3/31\'26"\n^\n'
    '"ACME",51.25," 3/19\'26"\n^\n'
    '"ACME",45," 3/20\'26"\n^\n'
    "!Account\nNBrokerage\nTInvst\n^\n"
    "!Type:Invst\n"
    f"D3/02'26\nNShtSell\nY{_OPT}\nI4.10\nQ500\nU2,045.00\nT2,045.00\nO5.00\n^\n"
)


def _canon_option(conn):
    from mammon.importers.record import normalize_security_name
    return normalize_security_name(_OPT)


def test_quicken_totals_make_the_multiplier_one_and_value_the_position_right(conn, tmp_path):
    from mammon import investments
    _import(conn, tmp_path, _QUICKEN_SHAPE_QIF)
    sym = _canon_option(conn)
    assert investments.is_option(conn, sym), "the padded name must be one security"
    assert _security(conn, sym)["multiplier"] == "1"
    aid = conn.execute("SELECT id FROM accounts WHERE name='Brokerage'").fetchone()[0]
    values = investments.holding_values_at(conn, aid, "2026-03-31")
    assert values[sym] == -500 * 4_00                  # 500 x 4.00 x 1, a liability


def test_a_contract_shaped_total_keeps_the_contract_size(conn, tmp_path):
    _import(conn, tmp_path, _MODERN_QIF)
    assert _security(conn, "XYZ 260117C00150000")["multiplier"] == "100"


def test_a_rounded_penny_premium_still_reads_as_quicken_shaped(conn, tmp_path):
    """The export writes a 0.026 premium as I0.03, so T is 13% below Q x I. At
    scale 1 the implied price rounds back to 0.03; at 10 or 100 it cannot."""
    text = (
        "!Type:Security\n"
        "NXYZ  270115C00020000 XYZ 15JAN27 20.0 C\nSXYZ  270115C00020000\nTOption\n^\n"
        "!Account\nNBrokerage\nTInvst\n^\n!Type:Invst\n"
        "D5/18'26\nNBuy\nYXYZ  270115C00020000 XYZ 15JAN27 20.0 C\nI0.03\nQ2000\n"
        "U53.50\nT53.50\nO1.50\n^\n"
    )
    _import(conn, tmp_path, text)
    from mammon.importers.record import normalize_security_name
    sym = normalize_security_name("XYZ  270115C00020000 XYZ 15JAN27 20.0 C")
    assert _security(conn, sym)["multiplier"] == "1"


def test_quicken_prices_reach_the_same_security_as_its_trades(conn, tmp_path):
    _import(conn, tmp_path, _QUICKEN_SHAPE_QIF)
    row = conn.execute("SELECT close_price FROM price_history WHERE symbol=? AND date='2026-03-31'",
                       (_canon_option(conn),)).fetchone()
    assert row is not None and row[0] in ("4", "4.00")


def test_a_close_with_no_total_moves_no_cash_and_realizes_the_premium(conn, tmp_path):
    """Quicken writes an expiry or assignment removal as "Buy 20 @ 100" with no T.
    The specification: an omitted item is blank. No cash moved, so the whole
    premium is the gain -- not premium less 20 x 100."""
    from mammon import investments
    text = (
        "!Type:Security\n"
        "NXYZ  200619C00018000 XYZ 19JUN20 18.0 C\nSXYZ  200619C00018000\nTOption\n^\n"
        "!Account\nNBrokerage\nTInvst\n^\n!Type:Invst\n"
        "D3/26'20\nNSell\nYXYZ  200619C00018000 XYZ 19JUN20 18.0 C\nI42.50\nQ20\n"
        "U841.20\nT841.20\nO8.80\n^\n"
        "D6/19'20\nNBuy\nYXYZ  200619C00018000 XYZ 19JUN20 18.0 C\nI100\nQ20\n^\n"
    )
    _import(conn, tmp_path, text)
    aid = conn.execute("SELECT id FROM accounts WHERE name='Brokerage'").fetchone()[0]
    [closed] = [p for p in investments.closed_positions(conn, aid) if p.symbol.startswith("XYZ")]
    assert closed.realized_pl == 841_20


def test_the_price_lists_strike_on_a_delivery_day_is_dropped(conn, tmp_path):
    """Assigned: the stock sold short at the 45 strike, the contract closed with no
    price. Quicken's price list says the stock closed at 45 that day; it did not."""
    text = _QUICKEN_SHAPE_QIF + (
        "D3/20'26\nNShtSell\nYACME INC\nI45\nQ500\nU22,499.00\nT22,499.00\nO1.00\n^\n"
        f"D3/20'26\nNCvrShrt\nY{_OPT}\nQ500\n^\n"
    )
    _import(conn, tmp_path, text)
    closes = dict(conn.execute("SELECT date, close_price FROM price_history WHERE symbol='ACME INC'"))
    assert "2026-03-20" not in closes
    assert closes.get("2026-03-19") in ("51.25",)


def test_an_import_leaves_a_persons_multiplier_alone_but_a_repair_may_change_it(conn, tmp_path):
    from mammon import securities as sec
    sym = _canon_option(conn)
    conn.execute("INSERT INTO securities(symbol, name) VALUES (?,?)", (sym, sym))
    sec.set_kinds(conn, [dict(symbol=sym, kind="option", kind_source="user", multiplier="100",
                              underlying="ACME", expiration="2026-04-17", strike="45",
                              option_right="C")])
    _import(conn, tmp_path, _QUICKEN_SHAPE_QIF)
    assert _security(conn, sym)["multiplier"] == "100"
    assert sec.record_observed_multipliers(conn, [sym], include_user=True) == {sym: ("100", "1")}
    assert _security(conn, sym)["multiplier"] == "1"

"""Table-driven tests for mammon.instruments (pure domain: no DB, no Qt).

Every symbol here is public (YHOO, SPX, LAMR, INTC) or synthetic (XYZ, ACME).
No ledger rows, no account numbers, no names.
"""
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mammon import instruments
from mammon.instruments import (
    UNKNOWN,
    Kind,
    OptionTerms,
    classify,
    parse_future,
    parse_legacy_option,
    parse_option,
)


# --------------------------------------------------------------------------
# The module must stay in the pure-domain tier.
# --------------------------------------------------------------------------
def test_module_is_pure_domain():
    source = Path(instruments.__file__).read_text(encoding="utf-8")
    imports = [
        line
        for line in source.splitlines()
        if re.match(r"\s*(?:import|from)\s+", line)
    ]
    joined = "\n".join(imports)
    assert "sqlite3" not in joined
    assert "PyQt5" not in joined
    assert "mammon.db" not in joined
    assert "mammon.ledger" not in joined


# --------------------------------------------------------------------------
# parse_option: every documented spelling of the SAME three contracts.
#
# (text, underlying, root, expiration, strike, right, standard)
# --------------------------------------------------------------------------
OPTION_CASES = [
    # The three worked OSI examples from Part 1 of docs/instrument_taxonomy.md.
    ("YHOO150416C00030000", "YHOO", "YHOO", "2015-04-16", "30", "C", True),
    ("SPX   141122P01950000", "SPX", "SPX", "2014-11-22", "1950", "P", True),
    ("LAMR  150117C00052500", "LAMR", "LAMR", "2015-01-17", "52.5", "C", True),
    # Unpadded and dotted broker variants of the same SPX put.
    ("SPX141122P01950000", "SPX", "SPX", "2014-11-22", "1950", "P", True),
    (".SPX141122P1950", "SPX", "SPX", "2014-11-22", "1950", "P", True),
    ("SPX 11/22/2014 1950.00 P", "SPX", "SPX", "2014-11-22", "1950", "P", True),
    # The four spellings one broker emits for one synthetic contract.
    ("XYZ 01/17/2026 150.00 C", "XYZ", "XYZ", "2026-01-17", "150", "C", True),
    ("XYZ 17JAN26 150 C", "XYZ", "XYZ", "2026-01-17", "150", "C", True),
    ("CALL XYZ 01/17/26 150", "XYZ", "XYZ", "2026-01-17", "150", "C", True),
    ("-XYZ260117C150", "XYZ", "XYZ", "2026-01-17", "150", "C", True),
    ("XYZ 260117C00150000", "XYZ", "XYZ", "2026-01-17", "150", "C", True),
    ("XYZ260117C00150000", "XYZ", "XYZ", "2026-01-17", "150", "C", True),
    ("xyz260117c00150000", "XYZ", "XYZ", "2026-01-17", "150", "C", True),
    # An OCC-adjusted root: the trailing digit belongs to the root.
    ("INTC1260117C00150000", "INTC", "INTC1", "2026-01-17", "150", "C", False),
    # A mini (root + 7), listed 2013-2014, 10-share deliverable.
    ("XYZ7  140117P00150000", "XYZ", "XYZ7", "2014-01-17", "150", "P", False),
    # A QIF security name: OSI symbol followed by a human rendering of it.
    ("XYZ   260117C00150000 XYZ 17JAN26 150 C",
     "XYZ", "XYZ", "2026-01-17", "150", "C", True),
]


@pytest.mark.parametrize(
    "text,underlying,root,expiration,strike,right,standard", OPTION_CASES
)
def test_parse_option_fields(
    text, underlying, root, expiration, strike, right, standard
):
    terms = parse_option(text)
    assert terms is not None, text
    assert terms.underlying == underlying
    assert terms.root == root
    assert terms.expiration == expiration
    assert terms.strike == Decimal(strike)
    assert isinstance(terms.strike, Decimal)
    assert terms.right == right
    assert terms.standard is standard


@pytest.mark.parametrize("text,underlying,root,expiration,strike,right,standard",
                         OPTION_CASES)
def test_parse_option_round_trips_through_canonical_osi(
    text, underlying, root, expiration, strike, right, standard
):
    """Parse anything, emit one thing, and parsing that again is a fixed point."""
    first = parse_option(text)
    canonical = first.osi()
    assert len(canonical) == 21, canonical
    again = parse_option(canonical)
    assert again == first
    assert parse_option(first.osi(padded=False)) == first


def test_osi_strike_has_three_implied_decimals():
    """00030000 is 30, not 30000 -- the classic 1000x options bug."""
    assert parse_option("YHOO150416C00030000").strike == Decimal("30")
    assert parse_option("LAMR  150117C00052500").strike == Decimal("52.50")
    assert parse_option("YHOO150416C00030000").osi() == "YHOO  150416C00030000"


def test_multiplier_is_unknown_for_a_non_standard_contract():
    assert parse_option("XYZ260117C00150000").multiplier == 100
    adjusted = parse_option("INTC1260117C00150000")
    assert adjusted.multiplier is UNKNOWN
    assert not adjusted.multiplier  # UNKNOWN is falsey, never mistaken for 0
    assert parse_option("XYZ7  140117P00150000").mini is True
    assert parse_option("XYZ260117C00150000").mini is False


def test_expiration_cadence_needs_no_flag():
    """Monthly, weekly and LEAPS all land in the same YYMMDD field."""
    monthly = parse_option("XYZ260116C00150000")  # a Friday monthly
    weekly = parse_option("XYZ260130C00150000")
    assert monthly.expiration == "2026-01-16"
    assert weekly.expiration == "2026-01-30"


# --------------------------------------------------------------------------
# parse_option: negatives. None of these is an exchange-traded option.
# --------------------------------------------------------------------------
NOT_OPTIONS = [
    None,
    "",
    "   ",
    "XYZ",
    "ACME.PRA",                                  # preferred share suffix
    "ACME-A",
    "BALANCED FUND K6",                          # tickerless plan fund
    "DOMESTIC BOND INDEX",
    "IBMAF",                                     # legacy OPRA, not OSI
    "ESM26",                                     # a future
    "US TREASURY 2.625% DUE 05/15/2026 100 P",   # bond text with option-ish tokens
    "XYZ 13/45/2026 150.00 C",                   # impossible calendar date
]


@pytest.mark.parametrize("text", NOT_OPTIONS)
def test_parse_option_refuses_non_options(text):
    assert parse_option(text) is None


# --------------------------------------------------------------------------
# parse_legacy_option: pre-2010 OPRA. Unknowns stay unknown.
# --------------------------------------------------------------------------
LEGACY_CASES = [
    ("IBMAF", "IBM", 1, "C", "F"),    # January call
    ("IBMMF", "IBM", 1, "P", "F"),    # January put, same strike code
    ("IBMLT", "IBM", 12, "C", "T"),   # December call
    ("IBMXT", "IBM", 12, "P", "T"),   # December put
    (".ACMEAE", "ACME", 1, "C", "E"),  # five-char root, dotted
]


@pytest.mark.parametrize("text,underlying,month,right,strike_code", LEGACY_CASES)
def test_parse_legacy_option(text, underlying, month, right, strike_code):
    terms = parse_legacy_option(text)
    assert terms is not None, text
    assert terms.underlying == underlying
    assert terms.root == underlying
    assert terms.month == month
    assert terms.right == right
    assert terms.strike_code == strike_code


@pytest.mark.parametrize("text,underlying,month,right,strike_code", LEGACY_CASES)
def test_legacy_strike_and_day_are_explicitly_unknown(
    text, underlying, month, right, strike_code
):
    """The strike letter needs a per-underlying table; the day is not encoded."""
    terms = parse_legacy_option(text)
    assert terms.strike is UNKNOWN
    assert terms.day is UNKNOWN
    assert terms.year is UNKNOWN
    assert terms.expiration is UNKNOWN
    assert set(terms.unknown_fields()) == {"strike", "day", "year"}
    assert terms.strike is not None  # 'unknown' is not 'absent'
    assert not terms.strike        # ...but it is falsey


@pytest.mark.parametrize(
    "text",
    [None, "", "IBM", "IBMYF", "IBMZF", "IBM1F", "ACMECOAF", "XYZ 17JAN26 150 C"],
)
def test_parse_legacy_option_refuses(text):
    """Y and Z are not month/right codes, and a 3-letter ticker is not decodable."""
    assert parse_legacy_option(text) is None


def test_legacy_root_length_is_genuinely_ambiguous():
    """A legacy root is 1-5 alpha, so a 6-letter string has two readings and the
    decoder takes the longest root: IBMAFG is IBMA/June/call, not IBM plus noise.
    That is exactly why parse_legacy_option is a decoder and not a detector --
    the caller must already know the row is an option."""
    terms = parse_legacy_option("IBMAFG")
    assert terms is not None
    assert (terms.root, terms.month, terms.right) == ("IBMA", 6, "C")


# --------------------------------------------------------------------------
# parse_future: root + month code + year, ambiguity flagged not resolved.
# --------------------------------------------------------------------------
def test_parse_future_two_digit_year():
    terms = parse_future("ESM26")
    assert (terms.root, terms.month_code, terms.month) == ("ES", "M", 6)
    assert terms.year == 2026
    assert terms.year_ambiguous is False
    assert terms.candidate_years == (2026,)


def test_parse_future_one_digit_year_is_flagged_not_guessed():
    terms = parse_future("ESF9")
    assert (terms.root, terms.month) == ("ES", 1)
    assert terms.year is UNKNOWN
    assert terms.year_ambiguous is True
    assert 1999 in terms.candidate_years and 2019 in terms.candidate_years
    assert len(terms.candidate_years) > 1


@pytest.mark.parametrize(
    "text,root,month,year",
    [
        ("/ESM26", "ES", 6, 2026),
        ("clz26", "CL", 12, 2026),
        ("GCZ2026", "GC", 12, 2026),
        ("ZNH98", "ZN", 3, 1998),   # the 1970 pivot: 98 is 1998
    ],
)
def test_parse_future_variants(text, root, month, year):
    terms = parse_future(text)
    assert terms is not None, text
    assert (terms.root, terms.month, terms.year) == (root, month, year)


@pytest.mark.parametrize("text", [None, "", "ESI26", "ESL26", "ESO26", "ES26", "ESM"])
def test_parse_future_refuses(text):
    """I, L and O are deliberately not month codes."""
    assert parse_future(text) is None


# --------------------------------------------------------------------------
# classify
# (symbol, name, sec_type, source_hint) -> Kind
# --------------------------------------------------------------------------
CLASSIFY_CASES = [
    # --- options, from the symbol alone or from the source's type word
    ("YHOO150416C00030000", None, "Option", None, Kind.OPTION),
    (None, "XYZ 17JAN26 150 C", "Option", None, Kind.OPTION),
    ("XYZ260117C00150000", "XYZ Jan 2026 150 Call", None, None, Kind.OPTION),
    ("IBMAF", "IBM Jan call", "Option", None, Kind.OPTION),
    # --- an employee grant is NOT an exchange-traded option
    (None, "ACME Corp RSU vest 2024", None, None, Kind.EMPLOYER_GRANT),
    (None, "ACME Employee Stock Option", "Option", None, Kind.EMPLOYER_GRANT),
    (None, "ACME ESPP shares", "Stock", None, Kind.EMPLOYER_GRANT),
    # --- futures
    ("/ESM26", None, None, None, Kind.FUTURE),
    ("ESM26", "E-mini S&P 500 Jun 2026", "Future", None, Kind.FUTURE),
    # --- the ordinary securities
    ("XYZ", "Acme Industries Inc", "Stock", None, Kind.EQUITY),
    ("XYZ", None, None, None, Kind.EQUITY),
    ("SPY", "SPDR S&P 500 ETF Trust", "ETF", None, Kind.ETF),
    ("VFIAX", "Vanguard 500 Index Fund Admiral", "Mutual Fund", None,
     Kind.MUTUAL_FUND),
    ("VFIAX", None, None, None, Kind.MUTUAL_FUND),
    ("SPAXX", "Fidelity Government Money Market", "Mutual Fund", None,
     Kind.MONEY_MARKET),
    (None, "US Treasury Note 2.625% due 05/15/2026", "Bond", None, Kind.BOND),
    (None, "US Treasury Note maturing 2026", None, None, Kind.BOND),
    ("ACME.WS", "Acme Corp Warrant", None, None, Kind.WARRANT),
    (None, "Acme Corp Rights", None, None, Kind.RIGHT),
    ("BTC-USD", "Bitcoin", None, "crypto exchange", Kind.CRYPTO),
    ("CASH", None, None, None, Kind.CASH),
    (None, "1 oz American Gold Eagle coin", None, None, Kind.PHYSICAL_METAL),
    (None, "Gold bullion 100 oz bar", None, "metals", Kind.PHYSICAL_METAL),
    (None, None, "Index", None, Kind.OTHER),
    # --- NEGATIVE cases: these must never be read as options or futures.
    # A tickerless 401(k) plan option: the NAME is the identity.
    (None, "BALANCED FUND K6", None, "401k", Kind.MUTUAL_FUND),
    # Contains the word BOND but is a plan index fund, not a bond.
    (None, "DOMESTIC BOND INDEX", None, "401k", Kind.MUTUAL_FUND),
    (None, "Stable Value Fund", None, "401k", Kind.MUTUAL_FUND),
    # A name whose FIRST TOKEN merely looks like a ticker.
    (None, "AMERICAN Funds EuroPacific Growth Fund Class R6", None, None,
     Kind.MUTUAL_FUND),
    (None, "GLD Medallion Partners LLC units", None, None, Kind.OTHER),
    (None, "ESM Holdings", None, None, Kind.OTHER),
]


@pytest.mark.parametrize("symbol,name,sec_type,hint,expected", CLASSIFY_CASES)
def test_classify(symbol, name, sec_type, hint, expected):
    assert classify(symbol, name, sec_type, hint) is expected


# A preferred-share suffix is the third negative case from the proposal: it
# arrives under three spellings from three sources and must NOT be mistaken for
# an option (the dot/dash suffix looks like a broker's dotted option form).
# Mechanically a preferred share is still equity, so that -- not 'other' -- is
# the honest answer; what matters is that it is never option/future/fund.
@pytest.mark.parametrize("symbol", ["ACME-A", "ACME.PRA", "ACMEpA"])
def test_preferred_share_suffix_is_not_an_option(symbol):
    kind = classify(symbol, "Acme Corp 6.5% Preferred Series A")
    assert parse_option(symbol) is None
    assert kind not in (Kind.OPTION, Kind.FUTURE, Kind.MUTUAL_FUND)
    assert kind is Kind.EQUITY


def test_classify_never_raises_on_empty_input():
    assert classify() is Kind.OTHER
    assert classify(None, None, None, None) is Kind.OTHER
    assert classify("", "", "", "") is Kind.OTHER


def test_kind_is_a_closed_set():
    assert {k.value for k in Kind} == {
        "equity", "etf", "mutual_fund", "money_market", "bond", "option",
        "future", "warrant", "right", "physical_metal", "employer_grant",
        "crypto", "cash", "other",
    }
    assert Kind.OPTION == "option"  # stable serializable spelling


def test_option_terms_are_hashable_value_objects():
    a = parse_option("XYZ260117C00150000")
    b = parse_option("XYZ 01/17/2026 150.00 C")
    assert a == b
    assert len({a, b}) == 1
    assert isinstance(a, OptionTerms)

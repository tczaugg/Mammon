"""mammon.instruments -- what KIND of thing a security is, and how to read its symbol.

A PURE-DOMAIN module: no sqlite3, no Qt, no I/O, no schema. It sits in the same
tier as :mod:`mammon.loans` -- plain functions over plain strings, returning
frozen dataclasses -- so it can be built, tested and reviewed before any decision
about storage. Nothing here writes anything; callers wire it up elsewhere.

Why a ``kind`` axis at all, separate from ``asset_class``
--------------------------------------------------------
They answer two different questions and they disagree in real portfolios:

  * ``asset_class`` answers "stocks or bonds?" -- it is an ALLOCATION axis, used
    to draw a pie chart. A bond mutual fund is bond-class; a gold ETF and a bar
    of gold in a vault are both commodity-class.
  * ``kind`` answers "what are the MECHANICS of this thing?" -- how it is
    quantified, priced, and closed out. A bond mutual fund is a
    :attr:`Kind.MUTUAL_FUND` (fractional shares, one NAV a day) while a bond is a
    :attr:`Kind.BOND` (quantity is face value, price is percent of par, accrued
    interest is not basis, it self-liquidates at maturity). The gold ETF is a
    :attr:`Kind.ETF` (a ticker with a daily close); the bar is a
    :attr:`Kind.PHYSICAL_METAL` (quantity is a WEIGHT, and no quote provider is
    ever asked).

Collapsing the two axes is how a register ends up multiplying a contract count by
a per-share premium and reporting $6.45 where $645 moved, or charting option
premiums of 0.35 on the same series as a 400-dollar share price. ``kind`` is what
lets the rest of the app ask "is the quantity a share count?" before it does
arithmetic; ``asset_class`` can never answer that.

The enumeration is deliberately CLOSED and small (see :class:`Kind`). Two
distinctions in it are easy to get wrong and are load-bearing:

  * An exchange-traded option (:attr:`Kind.OPTION`) and an employee stock option
    (:attr:`Kind.EMPLOYER_GRANT`) share an English word and nothing else. The
    grant has no OSI symbol, no multiplier of 100, no market price and no
    counterparty, so the classifier checks grant wording FIRST.
  * A money-market sweep (:attr:`Kind.MONEY_MARKET`) is mechanically a fund whose
    price is pinned at 1.00 but behaves like cash in a rollup. It gets its own
    slot precisely so that "show it as cash" can be a setting rather than a lie.

:attr:`Kind.FUTURE` has no importer behind it today; it exists so a later futures
import does not have to reopen this enumeration (and, downstream, the schema).

Why unknowns are EXPLICIT
-------------------------
Pre-2010 OPRA option symbols encode the root, the expiration MONTH and the
call/put right -- and a strike LETTER whose dollar value came from a per-underlying
table that is not in the symbol. The expiration day is likewise absent (it was
the third Friday by convention, but conventions are not data). A 40-year ledger
imported from Quicken contains such rows. A parser that returned a plausible
strike here would be inventing 1990s cost basis, so :func:`parse_legacy_option`
returns :data:`UNKNOWN` for the strike and the day, and
:meth:`PartialOptionTerms.unknown_fields` names them. Callers must fill them from
the accompanying name text, the trade price, or the user -- never from a guess.

The same rule drives :func:`parse_future`: a one-digit year (``ESF9``) is
genuinely ambiguous across decades, so ``year`` is :data:`UNKNOWN`,
``year_ambiguous`` is True, and every candidate decade is listed rather than one
being picked silently.

And :attr:`OptionTerms.multiplier` is 100 only for a STANDARD contract. An
adjusted root (``INTC1``) means the deliverable was changed by a corporate
action -- odd share counts, baskets, cash in lieu -- so the multiplier is
:data:`UNKNOWN`, not 100. Mini options (root + ``7``, listed 2013-03-18 and
discontinued 2014-12-17) delivered 10 shares.

Precision
---------
Strikes are :class:`decimal.Decimal`, never float: the OSI strike field is the
price times 1000 in 8 zero-padded digits, and reading ``00030000`` as 30000
instead of 30 is the classic 1000x options bug. Dates are ISO ``YYYY-MM-DD``
strings, the domain-layer convention everywhere in Mammon.

Tolerance
---------
The same broker exports one contract as ``XYZ 01/17/2026 150.00 C``,
``-XYZ260117C150``, ``XYZ 260117C00150000`` and ``CALL XYZ 01/17/26 150`` across
four report types. :func:`parse_option` accepts all of them (plus the padded
21-character OSI form and the dotted ``.SPX141122P1950`` variant) and normalizes
to one internal shape; :meth:`OptionTerms.osi` emits the canonical spelling, so
"parse anything, store one thing" round-trips.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional, Tuple, Union

__all__ = [
    "Kind",
    "UNKNOWN",
    "OptionTerms",
    "PartialOptionTerms",
    "FutureTerms",
    "classify",
    "parse_option",
    "parse_legacy_option",
    "parse_future",
    "MONTH_CODES",
]


# --------------------------------------------------------------------------
# The explicit-unknown sentinel.
#
# None already means "the caller did not ask" / "no such field" in most Python
# APIs, and conflating that with "this datum is genuinely not recoverable from
# the source" is how a 1990s option row acquires a fictitious strike. UNKNOWN is
# falsey so `if terms.strike:` is safe, and it has a loud repr so it shows up in
# a traceback or a debug print instead of masquerading as a number.
# --------------------------------------------------------------------------
class _Unknown:
    """Singleton meaning 'not recoverable from this source' (never 'zero')."""

    _instance: Optional["_Unknown"] = None

    def __new__(cls) -> "_Unknown":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "UNKNOWN"

    def __str__(self) -> str:
        return "unknown"

    def __reduce__(self):
        return (_Unknown, ())


UNKNOWN = _Unknown()

Maybe = Union[_Unknown, None]


class Kind(str, Enum):
    """What the MECHANICS of an instrument are. Closed set; see module docstring.

    Deliberately NOT the allocation axis (``asset_class``). Values are the stable
    lowercase spellings -- ``Kind.OPTION == "option"`` -- so a caller can persist
    or JSON-serialize one without a conversion table.
    """

    EQUITY = "equity"
    ETF = "etf"
    MUTUAL_FUND = "mutual_fund"
    MONEY_MARKET = "money_market"
    BOND = "bond"
    OPTION = "option"
    FUTURE = "future"
    WARRANT = "warrant"
    RIGHT = "right"
    PHYSICAL_METAL = "physical_metal"
    EMPLOYER_GRANT = "employer_grant"
    CRYPTO = "crypto"
    CASH = "cash"
    OTHER = "other"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.value


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
_MONTH_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

#: CME futures month codes. I, L and O are deliberately unused (confusable with
#: digits and with each other).
MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

# Pre-2010 OPRA month/right codes: A..L = Jan..Dec calls, M..X = Jan..Dec puts.
_LEGACY_MONTH_RIGHT = {}
for _i in range(12):
    _LEGACY_MONTH_RIGHT[chr(ord("A") + _i)] = (_i + 1, "C")
    _LEGACY_MONTH_RIGHT[chr(ord("M") + _i)] = (_i + 1, "P")

# Two-digit years below this pivot are 20xx, at or above it 19xx. A 40-year
# ledger holds both; the OSI form has only ever produced 20xx.
_YY_PIVOT = 70


def _norm(text: Optional[str]) -> str:
    """Upper-case, collapse whitespace. ``None`` becomes ``''``."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text).strip().upper())


def _tidy(value: Decimal) -> Decimal:
    """Drop meaningless trailing zeros without going exponential.

    ``Decimal('30.000').normalize()`` is ``3E+1``, which prints wrong in a UI, so
    integral values are quantized to 1 instead.
    """
    if value == value.to_integral_value():
        return value.quantize(Decimal(1))
    return value.normalize()


def _iso(year: int, month: int, day: int) -> Optional[str]:
    """ISO date string, or None if the triple is not a real calendar date."""
    try:
        return _dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def _year_from_yy(text: str) -> int:
    yy = int(text)
    return 1900 + yy if yy >= _YY_PIVOT else 2000 + yy


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class OptionTerms:
    """Fully recovered exchange-traded option terms.

    ``root`` keeps any OCC adjustment digit (``INTC1``); ``underlying`` is the
    root with that digit removed, which is the thing that might actually price
    from a quote provider. ``standard`` is False whenever a digit was appended --
    the deliverable is then not 100 ordinary shares and ``multiplier`` is
    :data:`UNKNOWN`.
    """

    underlying: str
    root: str
    expiration: str  # ISO YYYY-MM-DD, the contract's ORIGINAL expiration
    strike: Decimal
    right: str  # 'C' or 'P'
    standard: bool = True
    mini: bool = False
    multiplier: Union[int, _Unknown] = 100

    def osi(self, padded: bool = True) -> str:
        """Canonical OSI spelling: 6-char root, YYMMDD, C/P, strike x 1000 in 8."""
        y, m, d = self.expiration.split("-")
        thousandths = int(
            (self.strike * 1000).to_integral_value(rounding=ROUND_HALF_UP)
        )
        root = self.root.ljust(6) if padded else self.root
        return f"{root}{y[2:]}{m}{d}{self.right}{thousandths:08d}"

    @property
    def is_call(self) -> bool:
        return self.right == "C"


# root, optional padding, YYMMDD, C/P, strike digits. The root's trailing digit
# (INTC1, XYZ7) is part of the root, never noise; the regex lets the engine
# backtrack into it so INTC1260117C00150000 splits as INTC1 / 260117 / C.
_OSI_RE = re.compile(
    r"(?<![A-Z0-9])"
    r"(?P<root>[A-Z][A-Z.]{0,4}\d?)"
    r"[ ]{0,6}"
    r"(?P<ymd>\d{6})"
    r"(?P<right>[CP])"
    r"(?P<strike>\d{1,8})"
    r"(?![0-9])"
)

_RIGHT_WORDS = {"C": "C", "P": "P", "CALL": "C", "PUT": "P", "CALLS": "C", "PUTS": "P"}
_HUMAN_ROOT_RE = re.compile(r"[A-Z][A-Z.]{0,4}\d?")
_NUMBER_RE = re.compile(r"\d{1,7}(?:\.\d{1,4})?")


def _split_root(root: str) -> Tuple[str, bool, bool]:
    """(underlying, standard, mini) for a possibly-adjusted root."""
    if len(root) > 1 and root[-1].isdigit():
        # The digit says only "non-standard in some way". '7' was the mini
        # suffix (10-share deliverable, 2013-2014); any digit can also be an
        # OCC adjustment, so mini is reported as a hint, not a certainty.
        return root[:-1], False, root[-1] == "7"
    return root, True, False


def _terms(root: str, iso_date: str, right: str, strike: Decimal) -> OptionTerms:
    underlying, standard, mini = _split_root(root)
    return OptionTerms(
        underlying=underlying,
        root=root,
        expiration=iso_date,
        strike=_tidy(strike),
        right=right,
        standard=standard,
        mini=mini,
        multiplier=100 if standard else UNKNOWN,
    )


def _osi_strike(digits: str) -> Decimal:
    """8 digits means strike x 1000; anything shorter is a broker's whole dollars."""
    if len(digits) == 8:
        return Decimal(digits).scaleb(-3)
    return Decimal(digits)


def _date_token(token: str) -> Optional[str]:
    """ISO date from one token of a human option spelling, else None."""
    t = token.strip(".,")
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})", t)
    if m:
        yy = m.group(3)
        year = int(yy) if len(yy) == 4 else _year_from_yy(yy)
        return _iso(year, int(m.group(1)), int(m.group(2)))
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})([A-Z]{3})(\d{2}|\d{4})", t)
    if m and m.group(2) in _MONTH_NAMES:
        yy = m.group(3)
        year = int(yy) if len(yy) == 4 else _year_from_yy(yy)
        return _iso(year, _MONTH_NAMES[m.group(2)], int(m.group(1)))
    m = re.fullmatch(r"\d{6}", t)
    if m:
        return _iso(_year_from_yy(t[:2]), int(t[2:4]), int(t[4:6]))
    return None


def _parse_osi(text: str) -> Optional[OptionTerms]:
    for m in _OSI_RE.finditer(text):
        ymd = m.group("ymd")
        iso_date = _iso(_year_from_yy(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
        if iso_date is None:
            continue
        return _terms(
            m.group("root"), iso_date, m.group("right"), _osi_strike(m.group("strike"))
        )
    return None


def _parse_human(text: str) -> Optional[OptionTerms]:
    """'XYZ 01/17/2026 150.00 C', 'XYZ 17JAN26 150 C', 'CALL XYZ 01/17/26 150'.

    Every token must be consumed as root / date / strike / right. That strictness
    is what keeps a bond name ('US TREASURY 2.625% DUE 05/15/2026 100 P') from
    parsing as an option: 'TREASURY' is left over, so the whole match is refused.
    """
    tokens = [t for t in re.split(r"[\s,]+", text.strip()) if t]
    if not (4 <= len(tokens) <= 5):
        return None
    root = right = iso_date = None
    strike: Optional[Decimal] = None
    for i, raw in enumerate(tokens):
        tok = raw.strip(".").lstrip("$")
        if root is None and i == 0 and len(tok) > 1 and tok in _RIGHT_WORDS:
            right = _RIGHT_WORDS[tok]  # leading 'CALL XYZ ...'
            continue
        if root is None and _HUMAN_ROOT_RE.fullmatch(tok) and _date_token(tok) is None:
            root = tok  # the ticker is always the first non-word token
            continue
        if iso_date is None:
            got = _date_token(tok)
            if got is not None:
                iso_date = got
                continue
        if strike is None and _NUMBER_RE.fullmatch(tok):
            strike = Decimal(tok)
            continue
        if right is None and tok in _RIGHT_WORDS:
            right = _RIGHT_WORDS[tok]
            continue
        return None  # an unconsumed token: this is not an option spelling
    if root and right and iso_date and strike is not None:
        return _terms(root, iso_date, right, strike)
    return None


def parse_option(text: Optional[str]) -> Optional[OptionTerms]:
    """Read any documented spelling of an exchange-traded option, else None.

    Accepts padded OSI (``SPX   141122P01950000``), unpadded
    (``SPX141122P01950000``), dotted/signed broker variants (``.SPX141122P1950``,
    ``-XYZ260117C150``), adjusted and mini roots (``INTC1...``, ``XYZ7...``), and
    the human forms (``XYZ 01/17/2026 150.00 C``, ``XYZ 17JAN26 150 C``,
    ``CALL XYZ 01/17/26 150``). Returns None for anything else -- including a
    pre-2010 OPRA symbol, which is :func:`parse_legacy_option`'s job.
    """
    raw = _norm(text)
    if not raw:
        return None
    found = _parse_osi(raw)
    if found is not None:
        return found
    return _parse_human(raw)


@dataclass(frozen=True)
class PartialOptionTerms:
    """Pre-2010 OPRA terms: what the symbol really carries, and nothing more.

    ``strike``, ``day`` and ``year`` are :data:`UNKNOWN` by construction -- the
    strike letter mapped through a per-underlying table that is not in the symbol,
    and the day and year were never encoded at all. ``strike_code`` is preserved
    so a caller that finds the right table can finish the job.
    """

    underlying: str
    root: str
    right: str  # 'C' or 'P'
    month: int  # 1..12
    month_code: str
    strike_code: str
    strike: Union[Decimal, _Unknown] = UNKNOWN
    day: Union[int, _Unknown] = UNKNOWN
    year: Union[int, _Unknown] = UNKNOWN

    @property
    def expiration(self) -> Union[str, _Unknown]:
        """Never an ISO date: the day and year are not in a legacy symbol."""
        return UNKNOWN

    def unknown_fields(self) -> Tuple[str, ...]:
        """Names of the fields this source genuinely could not supply."""
        return tuple(
            name
            for name in ("strike", "day", "year")
            if getattr(self, name) is UNKNOWN
        )


# Root, then the month\right and strike letters. The optional single space is
# the BROKER's spelling of the same thing: IB writes the root in a fixed-width
# three-character field, so a two-character root arrives space-padded ("MS DJ")
# while a three-character one closes up ("LOWFX"). Both are one symbol, and the
# space is what makes the spaced form unmistakably NOT a ticker.
_LEGACY_RE = re.compile(
    r"^(?P<root>[A-Z]{1,5}) ?(?P<month>[A-Z])(?P<strike>[A-Z])$")


def parse_legacy_option(text: Optional[str]) -> Optional[PartialOptionTerms]:
    """Decode a pre-2010 OPRA symbol (``IBMAF``, ``MS DJ``) into what it encodes.

    NOT a detector: ``IBM`` is a perfectly good legacy symbol ('I', February call,
    strike code M) and also a perfectly good ticker, so the caller must already
    know this row is an option (QIF's ``T`` field, OFX ``OPTINFO``, the account's
    context) before asking. To keep the worst collisions out of reach this
    requires a root of two or more letters.

    The one exception to "not a detector" is the SPACED broker form: a ticker
    never contains a space, so ``MS DJ`` can only be the padded shorthand. The
    month/right letter is still the validity test in both forms -- a pair that is
    not a month/right code returns None rather than a guess.

    Returns the underlying, the expiration MONTH and the call/put right; the
    strike, day and year come back as :data:`UNKNOWN`.
    """
    raw = _norm(text).lstrip(".-+")
    if not raw:
        return None
    m = _LEGACY_RE.fullmatch(raw)
    # Root of one letter is rejected here rather than in the pattern: "A BC"
    # would otherwise decode, and a one-letter root is the collision this
    # decoder's four-letter minimum was written to keep out of reach.
    if not m or len(m.group("root")) < 2:
        return None
    code = m.group("month")
    if code not in _LEGACY_MONTH_RIGHT:
        return None  # Y and Z are not month/right codes
    month, right = _LEGACY_MONTH_RIGHT[code]
    root = m.group("root")
    return PartialOptionTerms(
        underlying=root,
        root=root,
        right=right,
        month=month,
        month_code=code,
        strike_code=m.group("strike"),
    )


# --------------------------------------------------------------------------
# Futures
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FutureTerms:
    """Futures contract terms, with the one-digit-year ambiguity kept visible.

    ``ESF9`` is January of 1989, 1999, 2009, 2019, 2029 or 2039 and the symbol
    does not say which. ``year`` is then :data:`UNKNOWN`, ``year_ambiguous`` is
    True and ``candidate_years`` lists every decade, so a caller can resolve it
    from the trade date instead of this module guessing.
    """

    root: str
    month_code: str
    month: int
    year_text: str
    year: Union[int, _Unknown] = UNKNOWN
    year_ambiguous: bool = False
    candidate_years: Tuple[int, ...] = ()


_FUTURE_RE = re.compile(
    r"^(?P<root>[A-Z]{1,4})(?P<code>[A-Z])(?P<year>\d{1,4})$"
)

# Decades a one-digit year could plausibly mean in a 40-year ledger.
_FUTURE_YEAR_LO, _FUTURE_YEAR_HI = 1980, 2039


def parse_future(text: Optional[str]) -> Optional[FutureTerms]:
    """Read ``root + month code + year`` (``ESM26``, ``/ESM26``, ``ESF9``).

    Flags the one-digit year rather than resolving it. A three-digit year is
    refused outright; four digits are taken literally; two digits use the same
    1970 pivot as everywhere else in this module.
    """
    raw = _norm(text).lstrip("/.").replace(" ", "")
    if not raw:
        return None
    m = _FUTURE_RE.fullmatch(raw)
    if not m:
        return None
    code = m.group("code")
    if code not in MONTH_CODES:
        return None  # I, L, O and the letters that are not month codes
    digits = m.group("year")
    if len(digits) == 1:
        candidates = tuple(
            y
            for y in range(_FUTURE_YEAR_LO, _FUTURE_YEAR_HI + 1)
            if y % 10 == int(digits)
        )
        year: Union[int, _Unknown] = UNKNOWN
        ambiguous = True
    elif len(digits) == 2:
        year = _year_from_yy(digits)
        candidates = (year,)
        ambiguous = False
    elif len(digits) == 4:
        year = int(digits)
        candidates = (year,)
        ambiguous = False
    else:
        return None
    return FutureTerms(
        root=m.group("root"),
        month_code=code,
        month=MONTH_CODES[code],
        year_text=digits,
        year=year,
        year_ambiguous=ambiguous,
        candidate_years=candidates,
    )


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
# An employee grant and an exchange-traded option share a word and nothing else,
# so this runs before every other rule.
_EMPLOYER_RE = re.compile(
    r"\bRSUS?\b|RESTRICTED\s+STOCK|\bESPP\b|EMPLOYEE\s+STOCK|"
    r"\bNQSOS?\b|\bNSOS?\b|\b(?:ISO|NSO|NQSO)\s+(?:GRANT|OPTION)|"
    r"(?:INCENTIVE|NON-?QUALIFIED)\s+STOCK\s+OPTION|STOCK\s+OPTION\s+PLAN|"
    r"(?:STOCK|SHARE|OPTION|EQUITY)\s+GRANT|GRANT\s+(?:OF\s+)?(?:STOCK|SHARES|OPTIONS)"
)

_MONEY_MARKET_RE = re.compile(
    r"MONEY\s*MARKET|MONEY\s*MKT|\bMMKT\b|CASH\s+RESERVES|\bSWEEP\b"
)

# Applied to the source's own type word (QIF 'T', OFX, a broker's column) and
# then to source_hint. These are the most trustworthy signals available.
_TYPE_RULES = (
    (_MONEY_MARKET_RE, Kind.MONEY_MARKET),
    (re.compile(r"\bETFS?\b|EXCHANGE[- ]TRADED"), Kind.ETF),
    (re.compile(r"MUTUAL\s*FUND|\bFUNDS?\b"), Kind.MUTUAL_FUND),
    (re.compile(r"\bOPTION"), Kind.OPTION),
    (re.compile(r"\bFUTURE"), Kind.FUTURE),
    (re.compile(r"\bWARRANT"), Kind.WARRANT),
    (re.compile(r"\bRIGHTS?\b"), Kind.RIGHT),
    (re.compile(r"BULLION|PRECIOUS\s+METAL|\bMETALS?\b|PHYSICAL\s+(?:GOLD|SILVER)"),
     Kind.PHYSICAL_METAL),
    (re.compile(r"CRYPTO|DIGITAL\s+(?:CURRENCY|ASSET)"), Kind.CRYPTO),
    (re.compile(r"\bBONDS?\b|\bNOTES?\b|\bBILLS?\b|TREASUR|\bCDS?\b|"
                r"CERTIFICATE\s+OF\s+DEPOSIT|\bMUNI"), Kind.BOND),
    (re.compile(r"\bCASH\b"), Kind.CASH),
    (re.compile(r"\bSTOCK\b|\bEQUIT|\bSHARES?\b|\bCOMMON\b|\bPREFERRED\b|"
                r"\bADR\b|\bREIT\b"), Kind.EQUITY),
    # An index is not a holdable instrument; Quicken emits 'Index' anyway.
    (re.compile(r"\bINDEX\b"), Kind.OTHER),
)

_METAL_NAME_RE = re.compile(r"\b(?:GOLD|SILVER|PLATINUM|PALLADIUM)\b")
_METAL_UNIT_RE = re.compile(
    r"\b(?:OZ|OUNCES?|TROY|BULLION|BARS?|COINS?|INGOTS?|EAGLES?|"
    r"KRUGERRANDS?|MAPLE\s+LEAF)\b"
)

# Applied to the security NAME. Weaker than a type word, and deliberately does
# NOT treat a bare 'BOND' as a bond: 'DOMESTIC BOND INDEX' is a tickerless 401(k)
# option, i.e. a fund, and calling it a bond would put face-value semantics on
# something quoted in NAV.
_NAME_RULES = (
    (_MONEY_MARKET_RE, Kind.MONEY_MARKET),
    (re.compile(r"\bETFS?\b|EXCHANGE[- ]TRADED\s+FUND|\bISHARES\b|\bSPDR\b"), Kind.ETF),
    (re.compile(r"\bWARRANTS?\b"), Kind.WARRANT),
    (re.compile(r"\bRIGHTS?\b"), Kind.RIGHT),
    (re.compile(r"\bPREFERRED\b|\bPFD\b"), Kind.EQUITY),
    (re.compile(r"TREASUR|\bT-BILL\b|\bT-BOND\b|CERTIFICATE\s+OF\s+DEPOSIT|"
                r"\bMATUR|\bDUE\s+\d|\d(?:\.\d+)?%\s"), Kind.BOND),
    (re.compile(r"\bFUNDS?\b|\bPORTFOLIOS?\b|\bINDEX\b|STABLE\s+VALUE|"
                r"TARGET\s+(?:DATE|RETIREMENT)|\bBALANCED\b|\bALLOCATION\b"),
     Kind.MUTUAL_FUND),
)

# Applied to the SYMBOL, last. Warrant/right suffixes come before the preferred
# pattern because '.WS' also matches the preferred shape.
_SYMBOL_RULES = (
    (re.compile(r"^\$?CASH$|^USD$"), Kind.CASH),
    (re.compile(r"^[A-Z]{2,5}[-/](?:USD|USDT|USDC)$"), Kind.CRYPTO),
    (re.compile(r"^[A-Z]{1,5}\.(?:WS|WT)$|^[A-Z]{1,5}\+$"), Kind.WARRANT),
    (re.compile(r"^[A-Z]{1,5}\.(?:RT|R)$"), Kind.RIGHT),
    (re.compile(r"^[A-Z]{1,5}(?:[-.](?:PR)?[A-Z]{1,2}|P[A-Z])$"), Kind.EQUITY),
    # The US mutual-fund convention: five letters ending in X (VFIAX, FXAIX).
    (re.compile(r"^[A-Z]{4}X$"), Kind.MUTUAL_FUND),
)

_TICKER_RE = re.compile(r"[A-Z][A-Z.]{0,5}\d?")


def _match_rules(text: str, rules) -> Optional[Kind]:
    if not text:
        return None
    for pattern, kind in rules:
        if pattern.search(text):
            return kind
    return None


def classify(
    symbol: Optional[str] = None,
    name: Optional[str] = None,
    sec_type: Optional[str] = None,
    source_hint: Optional[str] = None,
) -> Kind:
    """Decide what KIND of instrument this is. Never raises; worst case OTHER.

    ``symbol`` is the ticker or machine symbol, ``name`` the security's display
    name, ``sec_type`` whatever short type word the source supplied (QIF's ``T``
    field, OFX, a broker column), ``source_hint`` a free-text note about where the
    row came from ('crypto exchange', 'futures', 'metals', '401k').

    Evidence is weighed in decreasing order of trustworthiness: employer-grant
    wording, a parseable option symbol, the source's own type word, the hint, the
    name, then the symbol's shape. With no evidence at all beyond a
    ticker-shaped symbol the answer is :attr:`Kind.EQUITY` (the baseline of an
    investment account); with no symbol and no fund wording it is
    :attr:`Kind.OTHER` rather than a guess.
    """
    sym = _norm(symbol)
    nm = _norm(name)
    st = _norm(sec_type)
    hint = _norm(source_hint)
    blob = " ".join(t for t in (nm, st, hint) if t)

    if _EMPLOYER_RE.search(blob):
        return Kind.EMPLOYER_GRANT

    if parse_option(sym) is not None or parse_option(nm) is not None:
        return Kind.OPTION

    for text in (st, hint):
        kind = _match_rules(text, _TYPE_RULES)
        if kind is not None:
            if kind is Kind.MUTUAL_FUND and _MONEY_MARKET_RE.search(blob):
                return Kind.MONEY_MARKET  # a sweep fund is its own mechanic
            if kind is Kind.OPTION and parse_legacy_option(sym) is not None:
                return Kind.OPTION
            return kind

    # A bare 'ESM26' is also a plausible ticker, so a future is only read out of
    # the symbol when the source marks it (a leading slash, or the hint above).
    if _norm(symbol).startswith("/") and parse_future(sym) is not None:
        return Kind.FUTURE

    if (
        _METAL_NAME_RE.search(nm)
        and _METAL_UNIT_RE.search(nm)
        and not sym
    ):
        return Kind.PHYSICAL_METAL

    kind = _match_rules(nm, _NAME_RULES)
    if kind is not None:
        return kind

    kind = _match_rules(sym, _SYMBOL_RULES)
    if kind is not None:
        return kind

    if sym and _TICKER_RE.fullmatch(sym):
        return Kind.EQUITY
    return Kind.OTHER

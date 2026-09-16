"""mammon.securities -- one identity per security, and a name to show for it.

A security has always had two facts about it and only one column to hold them.
``investment_transactions.symbol`` (and ``holdings``, ``price_history``,
``review_items``, ``holdings_checkpoints``, ``security_mixtures``,
``securities``) stores whatever the source happened to call the thing, so the
same ETF is "VGT VANGUARD INFO TECH ETF" from a 2021 QIF and "VGT" from a 2026
Interactive Brokers CSV -- two securities, two price series, one holding split
in half, and a chart that shows two points because the dividends went to one
name and the prices to the other.

The room was already there and empty: ``securities.name`` and ``holdings.name``
have existed since migration 37 and were never written to, while
``securities.symbol`` -- the primary key -- carried the display name. This
module makes the split real:

* **symbol is the IDENTITY**: the ticker where the security has one, so the key
  a quote provider understands and the key the holding is stored under are the
  same string. That is what stops a fetch filing a price under a name nothing
  uses.
* **name is the DESCRIPTION**: "VANGUARD INFO TECH ETF", shown wherever a person
  reads a security, never used to look anything up.

**A ticker is suggested and confirmed, never derived.** :func:`suggest` proposes
a split; nothing is applied until a caller passes it back. This is not caution
for its own sake -- ``investments.ticker_of`` returns ``FID`` for the plan fund
"FID BALANCED K6" and ``INTL`` for "INTL EQUITY INDEX", and INTL is a real
listed company whose price is already in the file. A blind migration would file
a stranger's prices against a retirement fund and there would be nothing in the
data to say it had happened. Same discipline as ``asset_values`` uses for a
property address and ``investments.ticker_of`` for a quote target.

**A security with no ticker keeps its name as its identity.** A plan's internal
fund ("DOMESTIC BOND INDEX", "TARGET 2030 FUND") has no public ticker and never
will; inventing one would be worse than the problem. Identity is therefore the
ticker when a ticker exists and the full name otherwise, which is heterogeneous
but honest -- and is what the surrogate-key design would replace if this file
ever outgrows it.

Renaming is GLOBAL here, unlike ``investments.apply_security_renames``, which is
scoped to one account because a user fixing a typo means it in the register they
are looking at. Merging two spellings of one security is the opposite: leaving
account 58 on the old spelling is precisely the bug being fixed.

**An OPTION CONTRACT IS NOT A SPELLING OF ITS UNDERLYING**, and this module used
to think it was. Everything above assumes the two strings in front of it are two
names for one continuous instrument; a contract and its stock are two
instruments, and this file cannot tell, because ``investments.ticker_of`` reads
the first token of "XYZ 260117C00150000 XYZ 17JAN26 150 C" as "XYZ" and a QIF
option block states the ROOT in its ``S`` field -- so :func:`suggest` proposed
renaming the contract to the stock and marked it confident, i.e. recorded fact.
:func:`apply_splits` then executes such a proposal DESTRUCTIVELY: ``_rekey``
copies what fits and DELETES the rest, so the contract's transactions, prices
and holdings are absorbed into the stock's with nothing left to undo from.

Three refusals close that, all keyed on ``investments.looks_like_option`` (a
deliberately conservative OSI-shape test, documented there as interim): an
option is never proposed for an identity change and never confident; a split
whose target identity already belongs to a differently-classified security is
refused outright; and :func:`fetch_ticker` returns None for a contract, so no
stock price is ever filed against one. A real instrument classifier replaces the
shape test later; the refusals are the part that must not wait for it.

That shape test knows only the MODERN 21-character OSI symbol, and a broker's
pre-2010 shorthand ("MS DJ", "LOWFX") is an option the test says False about --
so those rows were still being proposed for collapse onto their underlying,
dozens at a time, with nothing on screen to say what they were.
:func:`suggest` therefore also consults ``instruments.parse_legacy_option``, and
every refusal now carries a ``reason`` string the dialog shows, because a row
the user cannot identify is a row they cannot safely untick.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, Optional, Tuple

from mammon import instruments, investments

# Every table that keys a security by its text symbol. Kept as data because the
# list has grown twice (security_mixtures in migration 46) and a rename that
# misses one leaves a security half-renamed -- which reads as data loss, since
# the rows that kept the old name simply stop being found.
SYMBOL_TABLES = (
    "investment_transactions",
    "holdings",
    "holdings_checkpoints",
    "price_history",
    "review_items",
    "security_mixtures",
)


def _case_only(a: Optional[str], b: Optional[str]) -> bool:
    """True when two spellings differ by NOTHING but letter case.

    THE one place that decision is made. Letter case is not part of a security's
    identity: ``investments.ticker_of`` upper-cases what it derives, so a file
    storing "vgt" produced a proposal to "rename" it to "VGT" -- a real
    :func:`_rekey` across price_history, holdings and every transaction, for the
    same security -- and the dialog listed one such row per lower-cased symbol
    for the user to approve. The user's words: "requiring approval to change case
    is annoying and stupid ... this just clutters the table."

    Equal strings are NOT case-only differences (they are no difference at all),
    so callers can use this to mean "differs, but only in a way nobody cares
    about"."""
    a_s, b_s = str(a or ""), str(b or "")
    return a_s != b_s and a_s.casefold() == b_s.casefold()


# Shown for the ONE case difference that is a real change: two DISTINCT stored
# rows whose spellings differ only by case are two rows for one security, and
# merging them is the repair.
CASE_MERGE_REASON = ("two stored rows differ only in letter case -- merged into "
                     "one identity")


@dataclass
class Split:
    """A proposed or confirmed identity for one security.

    ``old`` is what the file currently stores. ``symbol`` is the identity to key
    on, ``name`` the description to show. ``confident`` is False when the ticker
    was guessed from a name that may not contain one -- the caller must show
    those for confirmation rather than applying them.

    ``reason`` is set only when :func:`suggest` REFUSED to propose anything, and
    says why in words a person can read ("option contract ..."). It exists
    because "no proposal" and "proposal that happens to change nothing" look
    identical from the outside -- ``old == symbol``, ``name is None`` -- and the
    securities dialog was showing a wall of unexplained rows the user could not
    tell apart. Appended last and defaulted, so the positional three-argument
    construction used by callers that rebuild a Split still works.

    ``case_merge`` is the single exception to "case is never a change": it is set
    by :func:`suggest_all`, which is the only caller that can see the whole set
    of stored symbols, when ANOTHER stored row spells this one's identity with
    different case. Then the two rows really are one security stored twice and
    the merge is the repair, so ``changes_key`` says True again and ``reason``
    carries :data:`CASE_MERGE_REASON`. One split alone cannot know this --
    hence a field rather than a rule inside :func:`suggest`.

    ``unused`` marks a symbol NO stored row carries (:func:`_settle_unused`).
    It is a refusal like the others, but a distinct one: the dialog counts
    option refusals separately and must not describe a catalog orphan as an
    option contract."""
    old: str
    symbol: str
    name: Optional[str]
    confident: bool = True
    reason: Optional[str] = None
    case_merge: bool = False
    unused: bool = False

    @property
    def changes_key(self) -> bool:
        """Whether applying this would re-key rows -- case alone never does.

        The ``symbol == old`` guard comes first and is load-bearing: ``_rekey``
        is INSERT OR IGNORE + DELETE, so re-keying a spelling onto ITSELF would
        delete every row of that security."""
        if self.symbol == self.old:
            return False
        if self.case_merge:
            return True
        return not _case_only(self.symbol, self.old)

    @property
    def refused(self) -> bool:
        """True when :func:`suggest` proposed nothing and said why. A case merge
        carries a reason too, but it is a proposal, not a refusal."""
        return bool(self.reason) and not self.case_merge

    @property
    def actionable(self) -> bool:
        """Whether ticking this row would change anything. The dialog's tick, its
        counts and its sort all read this, so "is this a proposal?" is answered
        once -- three copies of the expression is how case-only rows came to be
        counted as proposed in one place and not another."""
        if self.refused or self.unused:
            return False
        return self.changes_key or bool(self.name)


def _name_after_ticker(symbol: str, ticker: str) -> Optional[str]:
    """The descriptive remainder of ``symbol`` once ``ticker`` is taken off the
    front, or None when nothing is left (the source gave a bare ticker)."""
    rest = str(symbol or "").strip()[len(ticker):].strip()
    return rest or None


@dataclass(frozen=True)
class SourceKind:
    """What a SOURCE stated about an instrument's kind, and what it left unsaid.

    ``unknown`` is the point of this type. A field that is None here is None in
    the database, and ``unknown`` names which of those Nones are ABSENCES OF
    KNOWLEDGE rather than absences of the property -- NULL ``expiration`` means
    "never expires" for a stock and "the source did not say when" for a 2007
    option symbol, and only the source can tell those apart."""
    kind: str
    kind_source: str
    multiplier: Optional[str] = None
    underlying: Optional[str] = None
    expiration: Optional[str] = None
    strike: Optional[str] = None
    option_right: Optional[str] = None
    unknown: Tuple[str, ...] = ()


# Quicken's `!Type:Security` T word for a contract. The whole vocabulary is
# Stock / Mutual Fund / Bond / ETF / Option / Index / Other, and only this one is
# acted on: the others would be a CLASSIFICATION of instruments this module has
# no defect to fix in, and NULL kind means unclassified, never equity.
_OPTION_TYPE_WORDS = frozenset({"option", "options", "option contract"})

# The columns whose NULL is a recorded absence of knowledge for an option: the
# source said "Option" and then did not say this. Kept as data so a caller can
# show them without re-deriving which parse produced the row.
_OPTION_TERM_FIELDS = ("underlying", "expiration", "strike", "option_right",
                       "multiplier")


def classify_source(name, ticker, sec_type) -> Optional[SourceKind]:
    """Classify one security master row from what the SOURCE stated.

    Two statements arrive together and neither is a guess: the type word
    ("Option") and the symbol strings. :func:`instruments.parse_option` reads a
    modern contract out of either string and yields every term including the
    multiplier, so a row that parses is fully stated and lands as
    ``kind_source='source'``.

    **A pre-2010 symbol lands PARTIAL, and that is the honest outcome.** The
    OCC's pre-2010 encoding packed the strike into a single letter whose meaning
    depended on a strike-interval table the symbol does not carry, and the year
    is simply not in there: ``instruments.parse_legacy_option`` returns UNKNOWN
    sentinels for exactly those fields. Defaulting them would manufacture a
    contract that never traded -- a $30 strike where the file says $130 -- and
    nothing downstream could tell it from a real one. They stay NULL and are
    NAMED in :attr:`SourceKind.unknown` instead.

    The multiplier follows the same rule. 100 is the answer for a standard
    contract and the parser states it; an adjusted root (``XYZ1``) or a mini
    (``XYZ7``) has a multiplier the symbol does not determine, so
    :data:`instruments.UNKNOWN` becomes NULL-and-named rather than 100. For an
    option, a NULL multiplier means UNSTATED -- never the 1 that it means for an
    ordinary share -- so no amount may be derived from one.

    Returns None when the source said nothing that classifies the row, which is
    the overwhelmingly common case and leaves ``kind`` NULL: unclassified."""
    name = (name or "").strip()
    ticker = (ticker or "").strip()
    stated = (sec_type or "").strip().lower() in _OPTION_TYPE_WORDS

    terms = instruments.parse_option(name) or instruments.parse_option(ticker)
    if terms is not None:
        unstated = () if terms.multiplier is not instruments.UNKNOWN else ("multiplier",)
        return SourceKind(
            kind=instruments.Kind.OPTION.value,
            # The type word is the STATEMENT; the symbol shape alone is only a
            # derivation, and the two must stay distinguishable forever.
            kind_source="source" if stated else "derived",
            multiplier=(None if terms.multiplier is instruments.UNKNOWN
                        else str(terms.multiplier)),
            underlying=terms.underlying,
            expiration=terms.expiration,
            strike=str(terms.strike),
            option_right=terms.right,
            unknown=unstated,
        )

    if not stated:
        # An unparseable string is not evidence of anything. Without the type
        # word there is nothing here to record.
        return None

    partial = (instruments.parse_legacy_option(ticker)
               or instruments.parse_legacy_option(name))
    if partial is None:
        # The source said "Option" and the symbol reads as nothing known. That
        # is still a fact worth keeping -- it is what stops the row being
        # treated as a share -- with every term named as unstated.
        return SourceKind(kind=instruments.Kind.OPTION.value, kind_source="source",
                          unknown=_OPTION_TERM_FIELDS)

    missing = set(partial.unknown_fields())
    strike_unknown = "strike" in missing
    # Expiration is ONE column and a legacy symbol carries only the month; a
    # month with no year is not a date and must not be written as though it
    # were. The month survives in the parse for a caller that wants to show it.
    exp = partial.expiration
    exp_unknown = exp is instruments.UNKNOWN
    unknown = ["multiplier"]  # pre-2010 symbols never carried one
    if strike_unknown:
        unknown.append("strike")
    if exp_unknown:
        unknown.append("expiration")
    return SourceKind(
        kind=instruments.Kind.OPTION.value,
        kind_source="source",
        underlying=partial.underlying,
        option_right=partial.right,
        strike=(None if strike_unknown else str(partial.strike)),
        expiration=(None if exp_unknown else exp),
        unknown=tuple(sorted(unknown)),
    )


def _stated(value) -> Optional[str]:
    """A term the source actually STATED, as text -- None for anything it left
    open, which includes :data:`instruments.UNKNOWN`. The one funnel every
    parsed term passes through on its way to a column, so an UNKNOWN sentinel
    can never reach the database as the string "unknown"."""
    if value is None or value is instruments.UNKNOWN:
        return None
    text = str(value).strip()
    return text or None


# What an OFX ``<SECLIST>`` block TYPE states outright. ``<STOCKINFO>`` and
# ``<OTHERINFO>`` are missing on purpose: a broker files a share, an ETF, an
# ADR and a money-market fund alike under STOCKINFO, so mapping it to 'equity'
# would record a guess with the standing of a statement. They stay NULL.
_SECLIST_KINDS = {
    "MFINFO": instruments.Kind.MUTUAL_FUND.value,
    "DEBTINFO": instruments.Kind.BOND.value,
}


def classify_seclist(entry) -> Optional[SourceKind]:
    """Classify one OFX ``<SECLIST>`` entry from what the FEED stated.

    The block type IS the statement -- ``<OPTINFO>`` is the broker saying "this
    is a contract" as plainly as Quicken's ``T Option`` line -- so the decision
    keys on that and never on whether the terms happened to parse. An OPTINFO
    whose ticker is unreadable is still an option, and the alternative (keying
    on a successful parse) silently demotes exactly the pre-2010 rows this
    whole path exists to keep honest.

    Returns None when the feed named no kind, which leaves the row
    unclassified. NULL kind is never equity."""
    info = str(getattr(entry, "info_type", "") or "").strip().upper()
    if info == "OPTINFO":
        return _seclist_option(entry)
    kind = _SECLIST_KINDS.get(info)
    return None if kind is None else SourceKind(kind=kind, kind_source="source")


def _seclist_option(entry) -> SourceKind:
    """The contract an ``<OPTINFO>`` states: every term it gave, and NULL --
    named in ``unknown`` -- for every term it did not.

    ``SHPERCTRCT`` describes THIS contract (an adjusted one may deliver an odd
    share count), so it outranks whatever the symbol implied; absent, the fully
    parsed terms supply it and an UNKNOWN multiplier stays NULL rather than
    becoming the 100 that would misprice the position by two decimal places."""
    terms = getattr(entry, "option", None)
    partial = getattr(entry, "partial_option", None)
    values = {f: None for f in _OPTION_TERM_FIELDS}
    values["multiplier"] = _stated(getattr(entry, "multiplier", ""))
    values["underlying"] = _stated(getattr(entry, "underlying_symbol", ""))
    if terms is not None:
        values["underlying"] = values["underlying"] or _stated(terms.underlying)
        values["expiration"] = _stated(terms.expiration)
        values["strike"] = _stated(terms.strike)
        values["option_right"] = _stated(terms.right)
        values["multiplier"] = values["multiplier"] or _stated(terms.multiplier)
    elif partial is not None:
        # A pre-2010 spelling. It states the root, the month and the right and
        # genuinely does not state the strike, the day or the year.
        values["underlying"] = values["underlying"] or _stated(partial.underlying)
        values["option_right"] = _stated(partial.right)
        values["strike"] = _stated(partial.strike)
        values["expiration"] = _stated(partial.expiration)
    return SourceKind(
        kind=instruments.Kind.OPTION.value,
        kind_source="source",
        unknown=tuple(sorted(f for f in _OPTION_TERM_FIELDS if values[f] is None)),
        **values,
    )


def _record_identity(conn, symbol, ticker, name, sec_type) -> None:
    """The one INSERT of a security master row, shared by every import path.

    Identity and description only. The classification columns are written
    separately, by :func:`record_stated_kinds`, because they answer to a
    precedence rule (user > source > derived) that no single UPSERT can
    express: COALESCE alone protects a user's row but also freezes a guess,
    leaving a 'derived' classification permanently unimprovable by the feed
    that states the answer.

    ``ticker=''`` is a RECORDED ABSENCE and overwrites; None means the source
    said nothing about a ticker and leaves whatever is there."""
    conn.execute(
        "INSERT INTO securities(symbol, ticker, name, sec_type) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET "
        "ticker=CASE WHEN excluded.ticker <> '' THEN excluded.ticker "
        "            ELSE COALESCE(securities.ticker, excluded.ticker) END, "
        "name=COALESCE(excluded.name, securities.name), "
        "sec_type=COALESCE(excluded.sec_type, securities.sec_type)",
        (symbol, ticker, name, sec_type))


def record_master(conn, rows) -> int:
    """Store a source's security master: ``(name, ticker, sec_type)`` triples.

    This is the difference between a recorded fact and a guess. Quicken's
    ``!Type:Security`` block states the ticker outright and omits it for a fund
    that has none, so an import that keeps it removes both the derivation and
    the confirmation step for every security it covers -- 533 of 614 in the
    reference export. Keyed by the name the file currently stores rows under,
    because that is still the identity until a split is applied.

    Nothing is re-keyed here. An import must not silently restate a user's
    securities; it records what the source said, and
    :func:`suggest` reads it back.

    The same applies to the instrument KIND: :func:`classify_source` reads the
    type word and the symbol, and :func:`record_stated_kinds` decides whether
    that statement may land. A row a USER classified comes out of an import
    unchanged, always."""
    n = 0
    stated: list = []
    for name, ticker, sec_type in rows or []:
        name = (name or "").strip()
        if not name:
            continue
        # "" is a RECORDED ABSENCE, not a missing value: Quicken writes a
        # security block with no S field for a fund that has no public ticker,
        # and that statement is the whole reason "INTL EQUITY INDEX" must not be
        # guessed at. NULL would be indistinguishable from never-imported and
        # would send it back to the heuristic that returns INTL.
        ticker = (ticker or "").strip()
        sec_type = (sec_type or "").strip() or None
        # The DESCRIPTION comes free with the pair: Quicken's name is the ticker
        # followed by the descriptive remainder ("VGT VANGUARD INFO TECH ETF"),
        # so the ticker it states is exactly the prefix to remove. Recording
        # only the ticker left `securities.name` NULL, which meant the register
        # and holdings tooltips had nothing to show until a rename was applied
        # -- the description was sitting in the file, derivable, unused.
        desc = None
        if ticker and name.upper().startswith(ticker.upper()):
            desc = _name_after_ticker(name, ticker)
        elif ticker and ticker.upper() != name.upper():
            # Ticker stated but not a prefix of the name: the whole name is the
            # description, since none of it is the ticker.
            desc = name
        _record_identity(conn, name, ticker, desc, sec_type)
        # What the source said this INSTRUMENT is, where it said anything. The
        # identity has to be in place first: a classification cannot conjure a
        # row, and this one is keyed by the same name.
        k = classify_source(name, ticker, sec_type)
        if k is not None:
            stated.append((name, k))
        n += 1
    record_stated_kinds(conn, stated)
    conn.commit()
    return n


def record_seclist(conn, entries) -> int:
    """Record what an OFX ``<SECLIST>`` STATED about the instruments it named.

    A broker's ``<OPTINFO>`` carries the right, the strike, the expiry and the
    shares per contract, and saying so is a statement of the same standing as
    Quicken's ``T Option`` line: both land ``kind_source='source'`` and both go
    through the same precedence gate, so neither can overrule a person.

    Entries the feed merely LISTS state no kind, and nothing at all is written
    for them -- no row, no NULL-kind placeholder, no guess. Returns the number
    of classifications actually applied."""
    stated: list = []
    for entry in entries or []:
        symbol = str(getattr(entry, "symbol", "") or "").strip()
        kind = classify_seclist(entry)
        if not symbol or kind is None:
            continue
        # The identity usually does NOT exist yet for a contract: the canonical
        # OSI symbol is what the transactions are keyed by, and only the SECLIST
        # says what it is. Record identity and description, never a ticker --
        # None here means "this source said nothing about a ticker", which is a
        # different claim from the recorded absence a QIF's missing S field is.
        _record_identity(conn, symbol, None,
                         str(getattr(entry, "name", "") or "").strip() or None,
                         None)
        stated.append((symbol, kind))
    n = record_stated_kinds(conn, stated)
    conn.commit()
    return n


def recorded_ticker(conn, symbol: str) -> Optional[str]:
    """What a SOURCE stated about this security's ticker.

    Three-valued on purpose: the ticker, ``""`` when a source recorded the
    security and gave it no ticker, and None when no source has said anything.
    Collapsing the middle case into None is what would send "INTL EQUITY INDEX"
    back to the heuristic."""
    row = conn.execute("SELECT ticker FROM securities WHERE symbol=?",
                       (symbol,)).fetchone()
    return None if row is None else row[0]


def contract_multiplier(conn, symbol: Optional[str]):
    """The factor that turns ``quantity x price`` into cash for this security.

    ``Decimal(1)`` for everything that is not KNOWN to be an option -- an
    unclassified row keeps behaving exactly as it always has, which is the whole
    point of NULL kind meaning unclassified. The stated multiplier for a
    classified contract. :data:`instruments.UNKNOWN` when the row is an option
    whose multiplier no source has stated, so a caller derives nothing from it
    rather than assuming the usual 100.

    This exists so the arithmetic sites have ONE place to ask. A NULL multiplier
    column means 1 for a share and UNSTATED for an option, and reading the column
    without reading ``kind`` beside it is exactly how a 100x error gets in."""
    if not symbol:
        return Decimal(1)
    row = conn.execute("SELECT kind, multiplier FROM securities WHERE symbol=?",
                       (symbol,)).fetchone()
    if row is None:
        return Decimal(1)
    kind, mult = row[0], row[1]
    if (kind or "").strip().lower() != instruments.Kind.OPTION.value:
        return Decimal(1)
    if mult is None or str(mult).strip() == "":
        return instruments.UNKNOWN
    try:
        value = Decimal(str(mult).strip())
    except InvalidOperation:
        return instruments.UNKNOWN
    return value if value > 0 else instruments.UNKNOWN


#: The contract sizes a stated cash total can reveal. A ratio off every one of
#: these by more than _SCALE_TOLERANCE says nothing (tiny premiums round badly).
_SCALES = (Decimal(1), Decimal(10), Decimal(100), Decimal(1000))
_SCALE_TOLERANCE = Decimal("0.03")
_SELL_WORDS = {"sell", "shtsell", "sellx", "selltoclose", "selltoopen"}
_BUY_WORDS = {"buy", "cvrshrt", "buyx", "buytoopen", "buytoclose"}


def observed_multiplier(conn, symbol: str) -> Optional[Decimal]:
    """The factor this security's OWN recorded trades show between quantity x
    price and the cash that moved, or None when they do not settle it.

    Each trade stating a quantity, a price and a nonzero amount gives
    ``gross / (quantity x price)``, gross being the amount before commission.
    Ratios within 3% of 1, 10, 100 or 1000 count; anything else is silent. So
    does a scale at which the price the trade implies, ``gross / (quantity x
    scale)``, rounds to the stated price at the places it was written with: an
    export rounds a penny premium (0.0455 written as 0.05), which is 9% off at
    the right scale and cannot round to the stated figure at any other. The
    answer is returned only when at least one trade counts and every counting
    trade agrees.

    Why the rows and not the symbol: an option symbol states the CONTRACT size,
    but the multiplier here is the factor between a quoted price and a
    position's value, and that depends on how the source counted quantity. A
    broker feed counts contracts at a per-share premium (x100). Quicken's QIF
    follows its specification -- Q is "number of shares", I the price, T the
    amount -- and in decades of real exports every option trade had T = Q x I:
    shares at a per-share premium in some years, contracts at a per-contract
    price in others. Reading 100 from the symbol valued those positions a
    hundred times too high."""
    rows = conn.execute(
        "SELECT action, quantity, price, amount, commission FROM investment_transactions "
        "WHERE symbol=? AND quantity IS NOT NULL AND price IS NOT NULL "
        "AND amount IS NOT NULL AND amount <> 0", (symbol,)).fetchall()
    seen = set()
    for r in rows:
        try:
            qty, price = abs(Decimal(r["quantity"])), abs(Decimal(r["price"]))
        except (InvalidOperation, TypeError):
            continue
        if not qty or not price:
            continue
        action = (r["action"] or "").strip().lower().replace(" ", "")
        amount, commission = abs(int(r["amount"])), abs(int(r["commission"] or 0))
        if action in _SELL_WORDS:
            gross = amount + commission
        elif action in _BUY_WORDS:
            gross = amount - commission
        else:
            continue
        ratio = Decimal(gross) / (qty * price * 100)
        places = Decimal(1).scaleb(min(price.as_tuple().exponent, 0))
        for scale in _SCALES:
            implied = (Decimal(gross) / (qty * scale * 100)).quantize(places, ROUND_HALF_UP)
            if abs(ratio - scale) <= scale * _SCALE_TOLERANCE or implied == price:
                seen.add(scale)
                break
    return seen.pop() if len(seen) == 1 else None


def record_observed_multipliers(conn, symbols, *, include_user: bool = False) -> dict:
    """Set each classified option's multiplier to what its own trades show
    (:func:`observed_multiplier`) where that differs from the stored one.

    A classification a person made (``kind_source='user'``) is left alone unless
    ``include_user`` -- an import never overrides a person's decision, but a
    repair the person asked for may. Rows whose trades do not settle the factor
    keep what they have. Returns ``{symbol: (old, new)}`` for every change."""
    changed = {}
    for symbol in sorted({s for s in symbols or () if s}):
        row = conn.execute("SELECT kind, kind_source, multiplier FROM securities WHERE symbol=?",
                           (symbol,)).fetchone()
        if row is None or (row["kind"] or "").lower() != instruments.Kind.OPTION.value:
            continue
        if (row["kind_source"] or "").lower() == "user" and not include_user:
            continue
        seen = observed_multiplier(conn, symbol)
        if seen is None:
            continue
        new = str(int(seen))                      # always one of 1, 10, 100, 1000
        if (row["multiplier"] or "") != new:
            conn.execute("UPDATE securities SET multiplier=? WHERE symbol=?", (new, symbol))
            changed[symbol] = (row["multiplier"], new)
    return changed


# The two refusal reasons, in words the securities dialog shows verbatim. Both
# start with "option contract" so a caller can key on that prefix without
# parsing the parenthetical.
OPTION_REASON = ("option contract -- kept as its own security, never merged "
                 "into its underlying")


def _legacy_option_reason(terms: instruments.PartialOptionTerms) -> str:
    """Why a pre-2010 shorthand symbol was left alone, decoded so it is checkable.

    The user's report was "a bunch of securities like 'MS DJ' and 'MS FH' that
    it wants to merge but I have no idea what they are" -- so the refusal spells
    out what the letters mean rather than just asserting they are an option."""
    right = "call" if terms.right == "C" else "put"
    return (f"option contract (pre-2010 broker shorthand: {terms.underlying} "
            f"{calendar.month_abbr[terms.month]} {right}, strike code "
            f"{terms.strike_code}) -- kept as its own security, never merged "
            f"into its underlying")


def _propose(raw: str, ticker: Optional[str]) -> Split:
    """The identity/description split itself, with no option refusals applied.

    Split out of :func:`suggest` so the refusals can inspect what WOULD be
    proposed before deciding; see there for why that ordering matters."""
    if ticker is not None:
        stated = ticker.strip()
        if not stated:
            # The source spoke and said there is no ticker.
            return Split(raw, raw, raw or None, confident=True)
        rest = _name_after_ticker(raw, stated) if \
            raw.upper().startswith(stated.upper()) else raw
        return Split(raw, stated, rest, confident=True)
    ticker = investments.ticker_of(raw)
    if not ticker:
        # No ticker to be had: the name IS the identity, and it is also the name.
        return Split(raw, raw, raw or None, confident=True)
    rest = _name_after_ticker(raw, ticker)
    if rest is None:
        # Already a bare ticker; nothing to split, and no description to invent.
        return Split(raw, ticker, None, confident=True)
    # A GUESS: "VGT VANGUARD INFO TECH ETF" splits correctly and "FID BALANCED
    # K6" does not, and nothing in the string distinguishes them.
    return Split(raw, ticker, rest, confident=False)


def suggest(symbol: str, ticker: Optional[str] = None) -> Split:
    """Propose an identity/description split for one stored security name.

    ``ticker`` is the one a SOURCE stated (``securities.ticker``, from a QIF's
    ``S`` field). When given it is used verbatim and ``confident`` is True: it
    is recorded fact, not derivation, and it settles the cases the heuristic
    gets wrong. Quicken OMITS ``S`` for a fund with no public ticker, so an
    empty string passed deliberately means "this security has none" and the name
    stays the identity -- which is the right answer for "FID BALANCED K6" and
    "INTL EQUITY INDEX", the two the heuristic mangles.

    With no source ticker, the leading token is guessed and ``confident`` is
    False, so the caller must confirm -- see the module docstring. A bare token
    could equally be a ticker already ("FIPDX") or a plan fund's whole name.

    AN OPTION CONTRACT IS NEVER GIVEN A NEW IDENTITY HERE, and never confidently.
    Both routes below would otherwise propose renaming the contract to its
    underlying: the heuristic because ``investments.ticker_of`` reads the first
    token of "XYZ 260117C00150000 XYZ 17JAN26 150 C" as "XYZ", and the stated
    route because a QIF option block puts the ROOT in its ``S`` field -- which
    turns the worst proposal in the file into recorded fact, and
    :func:`apply_splits` executes it by deleting the contract's rows. A stated
    root is a statement about the underlying, not about the contract's
    identity.

    THE PRE-2010 SHORTHAND IS AN OPTION TOO. ``investments.looks_like_option``
    only knows the modern 21-character OSI body, so a broker's older shorthand
    -- "MS DJ", "MSDJ", "LOWFX": root, then one month/right letter and one
    strike letter -- walked straight into the heuristic, which read "MS" as the
    ticker and proposed collapsing the contract onto the stock. Decoding it is
    the job of ``instruments.parse_legacy_option``.

    That decoder is documented as NOT a detector, and the ordering here is what
    makes using it safe: it is consulted ONLY when the proposal above would
    otherwise change the key. Every four- or five-letter ticker "decodes"
    (AAPL -> AA/PL, VFIAX -> VFI/AX), so asking first would stamp the refusal on
    half the funds in the file and make the dialog's overwhelm worse; but a row
    that keeps its own key is already being left alone, and a row that does NOT
    is exactly the merge that must never happen. The spaced form always changes
    its key (the heuristic takes the first token); the compact form only when a
    source stated the root as the ticker, which is the QIF/IB case where
    ``apply_splits`` would execute the merge as recorded fact."""
    raw = str(symbol or "").strip()
    if investments.looks_like_option(raw):
        # No key change and no description to write: the only safe proposal for
        # a contract is to leave it exactly as it is. confident=False so that a
        # caller applying only its confident splits touches nothing at all.
        return Split(raw, raw, None, confident=False, reason=OPTION_REASON)
    proposal = _propose(raw, ticker)
    if proposal.changes_key:
        legacy = instruments.parse_legacy_option(raw)
        if legacy is not None:
            # Same refusal, same shape: old == symbol and name is None, so
            # neither apply_splits nor _rekey has anything it could act on.
            return Split(raw, raw, None, confident=False,
                         reason=_legacy_option_reason(legacy))
    return proposal


def fetch_ticker(conn, symbol: str) -> Optional[str]:
    """The ticker it is SAFE to ask a quote provider about, or None.

    Three rules, in order, and the middle one is the whole subtlety:

    1. A ticker a source stated is used.
    2. A stored name that is a SINGLE ticker-shaped token is its own ticker.
       Nothing is being guessed -- the name is the token -- so "FIPDX" prices
       itself even though Quicken's block for it is bare ``NFIPDX``/``TStock``
       with no ``S`` line. Quicken omitting the symbol on a fund the user
       entered BY its symbol is an incomplete record, not a statement that no
       ticker exists.
    3. Anything else is None. This is where the damage lives: taking the
       leading word of a MULTI-token name gives INTL for "INTL EQUITY INDEX"
       and SP for "SP 500 INDEX PL CL D", both of which a provider will happily
       price -- with a stranger's numbers, against a retirement fund, leaving
       nothing in the data to show it happened.

    So a recorded absence suppresses only the guess, never the safe case.

    Rule 0, ahead of all three: an OPTION CONTRACT has no ticker to ask about.
    Its stated ``securities.ticker`` is the underlying's, so rule 1 would file
    the STOCK's closing price into the contract's ``price_history`` every time
    quotes ran -- and a contract is not worth its underlying at any point in its
    life. None until option quoting exists, which means the contract simply has
    no price series rather than a wrong one.

    Rule 0b, likewise ahead of the three: a security whose KIND pins its price
    (a money-market sweep, :data:`investments.PINNED_PRICE_KINDS`) has nothing
    to ask about either. Its name is often ticker-shaped -- a sweep really does
    trade under a symbol -- so rule 2 would fetch a quote for a position whose
    price is 1 by construction.
    """
    raw = str(symbol or "").strip()
    if not raw:
        return None
    if investments.looks_like_option(raw):
        return None
    if investments.pinned_price(conn, raw) is not None:
        return None
    stated = recorded_ticker(conn, raw)
    if stated:
        return stated
    if len(raw.split()) == 1 and investments.ticker_of(raw) == raw.upper():
        return raw
    return None


def never_quote(conn, symbol: str) -> bool:
    """True when this security must not be handed to a quote provider AT ALL.

    Driven by ``securities.kind``, so it is silent about a row that is still
    unclassified: NULL kind answers False and keeps exactly the behavior it has
    always had, where :func:`fetch_ticker` rule 3 is the only protection. This
    turns that half-protection into an explicit one for rows that ARE
    classified.

    Two kinds answer True:

    * ``money_market`` -- the price is pinned at 1
      (:func:`investments.pinned_price`), so a fetched quote can only disagree
      with the truth.
    * ``mutual_fund`` with no safe ticker of its own -- the tickerless plan
      fund. ``fetch_ticker`` already refuses to GUESS one, but the guess was
      being made further upstream, off the leading word of the name
      (``investments.ticker_of``), and the quote path then asked about INTL for
      "INTL EQUITY INDEX". A classified fund with no ticker is SKIPPED there
      rather than attempted and failed. A fund that does have a ticker --
      "FIPDX", or one a source stated -- is quoted exactly as before.
    """
    kind = investments.security_kind(conn, symbol)
    if kind == instruments.Kind.MONEY_MARKET.value:
        return True
    if kind == instruments.Kind.MUTUAL_FUND.value:
        return fetch_ticker(conn, symbol) is None
    return False


def suggest_all(conn) -> list:
    """A :class:`Split` for every security the file knows, ordered by identity.

    Sourced from the union of the symbol-bearing tables rather than from
    ``securities`` alone, because ``securities`` is populated by the allocation
    feature and holds only what that feature has seen -- 18 rows against 24
    distinct holdings in a real file.

    A ticker RECORDED by an import wins over the derived one, so a security
    whose source stated its ticker needs no guess and no confirmation.

    A second pass (:func:`_settle_case`) then looks at the whole set, which is
    the only vantage point from which a case difference can be judged."""
    stated = {row[0]: row[1] for row in conn.execute(
        "SELECT symbol, ticker FROM securities WHERE ticker IS NOT NULL")}
    recorded = {row[0]: row[1] for row in conn.execute(
        "SELECT symbol, name FROM securities WHERE name IS NOT NULL")}
    seen: dict = {}
    for table in SYMBOL_TABLES:
        for row in conn.execute(
                f"SELECT DISTINCT symbol FROM {table} "
                "WHERE symbol IS NOT NULL AND symbol<>''"):
            sym = row[0]
            if sym not in seen:
                seen[sym] = suggest(sym, stated.get(sym))
    for row in conn.execute(
            "SELECT symbol FROM securities WHERE symbol IS NOT NULL AND symbol<>''"):
        if row[0] not in seen:
            seen[row[0]] = suggest(row[0], stated.get(row[0]))
    _settle_case(seen.values(), recorded)
    _settle_overlaps(conn, seen.values())
    _settle_unused(conn, seen.values())
    return sorted(seen.values(), key=lambda s: (s.symbol.upper(), s.old.upper()))


# Why a symbol is listed but nothing is proposed for it. Two wordings because
# the two are different facts about the file: a catalog row the allocation
# feature left behind, versus a spelling that matches nothing at all.
UNUSED_CATALOG_REASON = ("in the security catalog only -- no transaction, "
                         "holding or price row uses it, so there is nothing "
                         "to change")
UNUSED_REASON = ("no transaction, holding or price row carries this symbol, "
                 "so there is nothing to change")


def _settle_unused(conn, splits: Iterable[Split]) -> None:
    """Refuse any proposal whose affected-row count is ZERO.

    The user's report, on a row reading "0" beside "description recorded":
    *"Why would you propose something if it affects 0 items?"* -- and they were
    right, because applying it would have written a ``securities`` catalog name
    and touched no ledger row at all. :func:`suggest_all` draws its symbols from
    the union of :data:`SYMBOL_TABLES` AND the ``securities`` catalog, and the
    catalog outlives the data: a security whose transactions were deleted, or
    one the allocation feature recorded and nothing else ever used, stays there
    forever and became a pre-ticked proposal every time the dialog opened.

    THE COUNT USED HERE IS THE ONE THE DIALOG SHOWS. Both sides read
    :func:`usage_counts` keyed on ``Split.old`` -- which is also the key
    :func:`apply_splits` and :func:`_rekey` act on -- so "the Rows column says
    0" and "this row is not actionable" cannot drift apart. That identity is the
    point: a proposal is offered exactly when it moves rows the user can see
    counted.

    Mutates into the same shape the option and overlap refusals use (``symbol ==
    old``, ``name is None``), so :func:`apply_splits` has nothing it could
    execute even if a caller passed the whole list back. ``unused`` is set so
    the dialog's tally does not file these under "option contracts". Runs LAST,
    after the case and overlap passes, because it overrides whatever they
    decided -- a change affecting no rows is not worth describing as a merge.
    """
    counts = usage_counts(conn)
    catalog = {row[0] for row in conn.execute(
        "SELECT symbol FROM securities WHERE symbol IS NOT NULL AND symbol<>''")}
    for s in splits:
        if s.refused or counts.get(s.old, 0):
            continue
        s.symbol = s.old
        s.name = None
        s.confident = False
        s.case_merge = False
        s.unused = True
        s.reason = (UNUSED_CATALOG_REASON if s.old in catalog
                    else UNUSED_REASON)


def _overlap_reason(other: str, period: Tuple[str, str]) -> str:
    """Why an identity change was refused, naming the security it collided with
    and when -- the user has to be able to check the claim against the file."""
    start, end = period
    span = start if start == end else f"{start} - {end}"
    return (f"held at the same time as {other} ({span}) -- two securities held "
            f"together are not one renamed to the other")


def _settle_overlaps(conn, splits: Iterable[Split]) -> None:
    """Refuse an identity change whose target was HELD ALONGSIDE this one.

    The user's rule, and the whole of it: "if it showed the date ranges when it
    was owned (long or short) ... you could disqualify a name change proposition
    if the time ranges overlap". A renaming is a succession -- the old symbol
    stops being held and the new one starts -- so a single day on which BOTH had
    an open position proves they are two different securities, and applying the
    proposal would fold two real holdings into one.

    Only a proposal that changes the key is judged, and only when the target
    identity ALSO exists as a distinct stored symbol with quantity rows of its
    own: an identity that exists nowhere else in the data has no ranges and
    cannot overlap anything, which is the ordinary "rename to its ticker" case
    and must stay untouched.

    THE CASE TWIN IS EXEMPT. ``case_merge`` means two spellings of ONE security
    (zzta / ZZTA), whose ranges overlap totally by construction -- that total
    overlap is the evidence they are the same thing, not evidence against it.
    Runs after :func:`_settle_case` for exactly that reason.

    Mutates in place into the same shape the option refusals use (``symbol ==
    old``, ``name is None``), so the row is non-actionable and ``apply_splits``
    has nothing it could execute."""
    stored = {str(s.old) for s in splits}
    cache: dict = {}

    def ranges(sym: str) -> list:
        if sym not in cache:
            cache[sym] = investments.held_ranges(conn, sym)
        return cache[sym]

    for s in splits:
        if s.case_merge or s.refused or not s.changes_key:
            continue
        target = s.symbol
        if target == s.old or target not in stored:
            continue
        hit = None
        for mine in ranges(s.old):
            for theirs in ranges(target):
                hit = investments.held_overlap(mine, theirs)
                if hit:
                    break
            if hit:
                break
        if not hit:
            continue
        s.symbol = s.old
        s.name = None
        s.confident = False
        s.reason = _overlap_reason(target, hit)


def _settle_case(splits: Iterable[Split], recorded: dict) -> None:
    """Decide the two case questions that need to see more than one row.

    1. A proposed DESCRIPTION that differs from the one already recorded only by
       case is dropped. Writing it would re-case ``securities.name`` and
       ``holdings.name`` and nothing else, and the dialog would have shown the
       row as "description recorded" for the user to approve.
    2. Two DISTINCT stored spellings that fold together are one security stored
       twice; the one whose identity is not itself becomes a real merge
       (``case_merge``). Detected from the actual set of stored symbols -- never
       guessed -- and never applied to a row :func:`suggest` refused (an option
       contract is not merged for any reason, case included).

    Mutates in place; ``Split`` is not frozen and these are freshly built."""
    folded: dict = {}
    for s in splits:
        folded.setdefault(str(s.old or "").casefold(), set()).add(s.old)
    for s in splits:
        current = recorded.get(s.old)
        if current is None:
            current = recorded.get(s.symbol)
        if s.name and not s.changes_key and _case_only(s.name, current):
            s.name = None
        if s.reason or s.symbol == s.old:
            continue
        twins = folded.get(str(s.old or "").casefold(), set()) - {s.old}
        if twins and _case_only(s.symbol, s.old):
            s.case_merge = True
            s.reason = CASE_MERGE_REASON


def usage_counts(conn) -> dict:
    """``{stored symbol: row count}`` across every symbol-bearing table.

    Shown beside a proposed change so the size of what is about to move is
    visible before it moves: "12,988 transactions" and "3 review items" are very
    different confirmations, and a merge that reports two rows when the user
    expected two hundred has found the wrong security."""
    counts: dict = {}
    for table in SYMBOL_TABLES:
        for sym, n in conn.execute(
                f"SELECT symbol, COUNT(*) FROM {table} "
                "WHERE symbol IS NOT NULL AND symbol<>'' GROUP BY symbol"):
            counts[sym] = counts.get(sym, 0) + int(n)
    return counts


def collisions(splits: Iterable[Split]) -> dict:
    """``{identity: [old, ...]}`` for identities more than one security maps to.

    A collision is the WHOLE POINT when it is "VGT" and "VGT VANGUARD INFO TECH
    ETF" -- that is the merge being asked for. It is a disaster when it is two
    genuinely different securities that happen to share a leading token, so the
    caller shows them and the user decides. ``PWE PENN WEST ENERGY TRUST ORD
    SHR`` and ``PWE PENN WEST PETROLEUM LTD`` are one company across a rename
    and DO belong together; a ticker reused by an unrelated issuer does not."""
    by_key: dict = {}
    for s in splits:
        by_key.setdefault(s.symbol, []).append(s.old)
    return {k: sorted(v) for k, v in by_key.items() if len(v) > 1}


def stored_symbols(conn) -> set:
    """Every security identity the file currently stores, across all tables."""
    found = set()
    for table in SYMBOL_TABLES + ("securities",):
        for row in conn.execute(
                f"SELECT DISTINCT symbol FROM {table} "
                "WHERE symbol IS NOT NULL AND symbol<>''"):
            found.add(row[0])
    return found


def merge_preview(conn, splits: Iterable[Split]) -> dict:
    """``{identity: [stored symbol, ...]}`` for identities that will end up
    holding rows from more than one of today's securities.

    :func:`collisions` compares the proposals to each other, which is not
    enough: renaming "VGT VANGUARD INFO TECH ETF" to "VGT" merges it into the
    "VGT" rows ALREADY in the file, and that spelling needs no change of its own
    so it is never among the proposals. Confirming only proposal-vs-proposal
    collisions therefore described a two-way merge as a simple rename -- the one
    change here that re-running cannot undo, reported as the one that can."""
    existing = stored_symbols(conn)
    groups: dict = {}
    for s in splits:
        if not s.old:
            continue
        groups.setdefault(s.symbol, set()).add(s.old)
        if s.changes_key and s.symbol in existing:
            groups[s.symbol].add(s.symbol)
    return {k: sorted(v) for k, v in groups.items() if len(v) > 1}


def apply_splits(conn, splits: Iterable[Split]) -> dict:
    """Apply confirmed splits across every table, globally. Returns a report
    ``{"renamed": n, "named": n, "merged": [identity, ...]}``.

    Order matters and is the reason this is not a loop of UPDATEs:

    1. Rows are re-keyed first, table by table, so nothing is left half-renamed.
    2. ``price_history`` and ``holdings`` carry UNIQUE constraints that a merge
       violates by construction (both spellings may hold 2026-09-04), so the
       merge is done with INSERT OR IGNORE + DELETE rather than UPDATE. The
       SURVIVING row is the one already under the new identity, matching
       ``investments._migrate_price_history``.
    3. ``securities`` is upserted next, since it is keyed by the identity being
       written -- and BEFORE the rebuild, because ``rebuild_holdings`` reads the
       description back out of it to repopulate ``holdings.name``.
    4. Holdings are rebuilt LAST, for every account touched -- a merge changes
       the lot replay itself, so a patched checkpoint would carry a pre-merge
       cost basis forward from every year that already had one.

    Raises ``ValueError``, BEFORE touching anything, for a split whose target
    identity already belongs to a differently-classified security -- today that
    means an option contract and a plain security in either direction. Step 2's
    ``INSERT OR IGNORE`` + ``DELETE`` is a merge, and a merge of two different
    instruments is the one outcome here that no re-run can undo: the losing
    rows are deleted, not shadowed. Refusing the whole batch is deliberate; a
    partial application would leave the file half-merged, which is worse than
    doing nothing and telling the caller why.
    """
    report = {"renamed": 0, "named": 0, "merged": []}
    todo = [s for s in splits if s.old]
    if not todo:
        return report
    _refuse_cross_kind_merges(conn, todo)
    accounts: set = set()
    for s in todo:
        if s.changes_key:
            for row in conn.execute(
                    "SELECT DISTINCT account_id FROM investment_transactions "
                    "WHERE symbol=?", (s.old,)):
                accounts.add(int(row[0]))

    for s in todo:
        if s.changes_key:
            report["renamed"] += _rekey(conn, s.old, s.symbol)
            report["merged"].append(s.symbol)
        if s.name:
            # Where the rows actually ARE. When the key is not changing, a
            # symbol that differs from the stored spelling by case alone is the
            # SAME security (see _case_only), so writing the description under
            # the proposed spelling would invent a second securities row.
            report["named"] += _set_name(
                conn, s.symbol if s.changes_key else s.old, s.name)
    conn.commit()
    for account_id in sorted(accounts):
        investments.rebuild_holdings(conn, account_id)
    report["merged"] = sorted(set(report["merged"]))
    return report


def _refuse_cross_kind_merges(conn, splits: Iterable[Split]) -> None:
    """Raise if any split would merge one KIND of instrument into another.

    :func:`_rekey` is destructive by construction -- the losing rows are deleted
    once the surviving identity has them -- so this check has to happen before
    the first statement of the batch runs, not per-split inside the loop.

    "Differently classified" is, for now, exactly "one side is an option
    contract and the other is not" (``investments.looks_like_option``, a
    conservative OSI-shape test). That is the collapse this whole guard exists
    for: a QIF states the root ticker on an option block, something proposes
    renaming ``XYZ 260117C00150000`` to ``XYZ``, and the contract's transactions,
    prices and holdings are silently absorbed into the stock's. Only a target
    that ALREADY EXISTS is refused here, because that is the case where rows are
    destroyed."""
    existing = stored_symbols(conn)
    for s in splits:
        if not s.changes_key or s.symbol not in existing:
            continue
        if investments.looks_like_option(s.old) != \
                investments.looks_like_option(s.symbol):
            raise ValueError(
                f"refusing to merge {s.old!r} into {s.symbol!r}: one is an "
                "option contract and the other is not, so this would delete a "
                "distinct instrument's rows rather than rename a security")


def _rekey(conn, old: str, new: str) -> int:
    """Move every row keyed by ``old`` onto ``new``. Returns rows re-keyed."""
    if old == new:
        # INSERT OR IGNORE then DELETE WHERE symbol=old would delete the lot.
        return 0
    moved = 0
    # UNIQUE(symbol, date) / UNIQUE(account_id, symbol): copy what does not
    # collide, then drop the old rows. A straight UPDATE raises on the first
    # date both spellings happen to share.
    conn.execute(
        "INSERT OR IGNORE INTO price_history(symbol, date, close_price, source) "
        "SELECT ?, date, close_price, source FROM price_history WHERE symbol=?",
        (new, old))
    moved += conn.execute("DELETE FROM price_history WHERE symbol=?",
                          (old,)).rowcount
    conn.execute(
        "INSERT OR IGNORE INTO holdings(account_id, symbol, name, quantity, cost_basis) "
        "SELECT account_id, ?, name, quantity, cost_basis FROM holdings WHERE symbol=?",
        (new, old))
    moved += conn.execute("DELETE FROM holdings WHERE symbol=?", (old,)).rowcount
    conn.execute(
        "INSERT OR IGNORE INTO security_mixtures(symbol, asset_class, pct, source, as_of) "
        "SELECT ?, asset_class, pct, source, as_of FROM security_mixtures WHERE symbol=?",
        (new, old))
    moved += conn.execute("DELETE FROM security_mixtures WHERE symbol=?",
                          (old,)).rowcount
    # `holdings_checkpoints` is keyed PRIMARY KEY(account_id, year, symbol) and
    # is DERIVED, so it is dropped rather than re-keyed. Renaming a snapshot
    # would collide on the first year both spellings held the security -- and
    # would be wrong even if it did not, because a merged position's year-end
    # state is not either half's state, it is the combined lot replay. The
    # rebuild at the end of apply_splits regenerates these from inception.
    conn.execute("DELETE FROM holdings_checkpoints WHERE symbol=?", (old,))
    # These two have no uniqueness on symbol, so a plain UPDATE is correct.
    for table in ("investment_transactions", "review_items"):
        moved += conn.execute(
            f"UPDATE {table} SET symbol=? WHERE symbol=?", (new, old)).rowcount
    # `securities` is keyed BY the symbol: carry the old row's classification
    # onto the new identity when the new one has none, then drop the old.
    conn.execute(
        "INSERT OR IGNORE INTO securities(symbol, name, sec_type, asset_class) "
        "SELECT ?, name, sec_type, asset_class FROM securities WHERE symbol=?",
        (new, old))
    conn.execute("DELETE FROM securities WHERE symbol=?", (old,))
    return moved


def _set_name(conn, symbol: str, name: str) -> int:
    """Record ``name`` as the description for ``symbol`` in both places that
    hold one. Returns how many rows now carry it."""
    conn.execute(
        "INSERT INTO securities(symbol, name) VALUES (?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name",
        (symbol, name))
    n = conn.execute("UPDATE holdings SET name=? WHERE symbol=?",
                     (name, symbol)).rowcount
    return n + 1


def name_of(conn, symbol: str) -> Optional[str]:
    """The description recorded for ``symbol``, or None."""
    row = conn.execute("SELECT name FROM securities WHERE symbol=?",
                       (symbol,)).fetchone()
    return row[0] if row is not None and row[0] else None


def display(conn, symbol: str) -> str:
    """What a person should read for ``symbol``: ``TICKER -- Description`` when a
    description is recorded, the bare identity otherwise.

    One chokepoint, so the register, the holdings dialog and the reports cannot
    drift into three answers -- the same reason ``ui.models.fmt_date`` exists."""
    sym = str(symbol or "").strip()
    if not sym:
        return ""
    name = name_of(conn, sym)
    return f"{sym} -- {name}" if name and name != sym else sym


@dataclass(frozen=True)
class KindUpdate:
    """One confirmed classification, ready to be written.

    ``kind`` of None is not a no-op and not an error: it puts the row BACK to
    unclassified and clears every term with it, which is what makes a
    reclassification reversible. ``kind_source`` defaults to ``'user'`` because
    the only caller that should be building one of these is a screen where a
    person said yes; an importer states ``'source'`` and the audit report
    proposes ``'derived'``.
    """
    symbol: str
    kind: Optional[str] = None
    kind_source: str = "user"
    multiplier: Optional[str] = None
    underlying: Optional[str] = None
    expiration: Optional[str] = None
    strike: Optional[str] = None
    option_right: Optional[str] = None


# Exactly the columns _V67 added, and nothing else. `symbol` and `ticker` are
# IDENTITY and are absent from this tuple on purpose: an identity change is not
# reversible and does not belong in a classification write.
KIND_COLUMNS = ("kind", "kind_source", "multiplier", "underlying",
                "expiration", "strike", "option_right")

_KIND_VALUES = frozenset(k.value for k in instruments.Kind)
_TERM_FIELDS = ("multiplier", "underlying", "expiration", "strike",
                "option_right")


def _as_update(item) -> KindUpdate:
    """Accept a :class:`KindUpdate`, a mapping, or anything with the fields."""
    if isinstance(item, KindUpdate):
        return item
    if isinstance(item, dict):
        unknown = set(item) - {"symbol"} - set(KIND_COLUMNS)
        if unknown:
            raise ValueError("not a classification column: %s"
                             % ", ".join(sorted(unknown)))
        return KindUpdate(**item)
    return KindUpdate(
        symbol=getattr(item, "symbol"),
        **{c: getattr(item, c, None) or None for c in KIND_COLUMNS
           if c != "kind_source"},
        kind_source=getattr(item, "kind_source", None) or "user")


def _checked(update: KindUpdate) -> Tuple:
    """Validate one update and return its bind values, or raise ValueError.

    Validation happens for the WHOLE batch before anything is written, so a
    typo in the tenth row cannot leave the first nine applied.
    """
    symbol = str(update.symbol or "").strip()
    if not symbol:
        raise ValueError("a classification needs a symbol")
    kind = (update.kind or "").strip().lower() or None
    if kind is not None and kind not in _KIND_VALUES:
        raise ValueError("unknown instrument kind: %r" % (update.kind,))

    terms = {f: (getattr(update, f) or None) for f in _TERM_FIELDS}
    if kind is None:
        # Unclassifying clears the terms with it; a term surviving the kind it
        # described is exactly how a strike ends up on a stock.
        terms = {f: None for f in _TERM_FIELDS}
    elif kind != instruments.Kind.OPTION.value and any(terms.values()):
        raise ValueError(
            "option terms do not belong on a %s: %s" % (
                kind, ", ".join(sorted(f for f, v in terms.items() if v))))

    right = terms["option_right"]
    if right is not None:
        right = str(right).strip().upper()
        if right not in ("C", "P"):
            raise ValueError("option_right is 'C' or 'P', not %r"
                             % (update.option_right,))
        terms["option_right"] = right

    for field in ("multiplier", "strike"):
        value = terms[field]
        if value is None:
            continue
        try:
            # Decimal-encoded TEXT, exactly as the column is documented --
            # never a float, and never a string the reader cannot parse back.
            terms[field] = str(Decimal(str(value).strip()))
        except InvalidOperation:
            raise ValueError("%s must be a decimal number, not %r"
                             % (field, value))

    source = (update.kind_source or "").strip() or None
    if kind is None:
        source = None
    return (kind, source, terms["multiplier"], terms["underlying"],
            terms["expiration"], terms["strike"], terms["option_right"],
            symbol)


def set_kinds(conn, updates: Iterable) -> int:
    """Record confirmed instrument KINDS. Writes nothing else, ever.

    The one writer for the classification columns, and deliberately the
    narrowest write in this module: ``UPDATE securities SET`` the seven columns
    migration 67 added, ``WHERE symbol=?``. It does not touch ``symbol`` or
    ``ticker``, does not INSERT (a classification cannot conjure an identity --
    a symbol with no ``securities`` row is skipped and counted out), and never
    goes near ``holdings``, ``lots`` or any transaction table.

    That narrowness is the point. ``apply_splits`` is the destructive
    neighbour: it re-keys, and ``_rekey`` deletes the rows that lose. Saying
    "this is an option" must not be able to do any of that, so the two are
    separate functions with separate confirmations, and reclassification stays
    additive and reversible -- re-run it with ``kind=None`` and the row is
    exactly as it was.

    Returns the number of rows actually updated.
    """
    checked = [_checked(_as_update(u)) for u in updates or []]
    if not checked:
        return 0
    sql = ("UPDATE securities SET %s WHERE symbol=?"
           % ", ".join("%s=?" % c for c in KIND_COLUMNS))
    n = 0
    for values in checked:
        n += conn.execute(sql, values).rowcount
    conn.commit()
    return n


# user > source > derived. A person's decision outranks a feed's statement,
# which outranks anything this code worked out for itself from a symbol's
# shape. Storing `kind_source` at all is what keeps this comparison possible
# years later, when nobody remembers which import said what.
_KIND_RANK = {"derived": 1, "source": 2, "user": 3}


def record_stated_kinds(conn, rows) -> int:
    """Apply what SOURCES stated about instrument kinds, under precedence.

    ``rows`` are ``(symbol, SourceKind)`` pairs, from :func:`classify_source`
    (a QIF security master) or :func:`classify_seclist` (an OFX SECLIST). Every
    import path that classifies anything comes through here, and the rule is:

    * ``kind_source='user'`` is NEVER overwritten by an import. Full stop; it
      is checked before anything else.
    * A NULL ``kind`` -- unclassified -- is filled by any statement.
    * A statement that OUTRANKS what is recorded replaces it, which is the one
      thing a blanket COALESCE could not do: the feed that states "option"
      outright must be able to correct a classification derived from a symbol's
      shape, or the first guess wins forever.
    * At equal rank, a second source restating the same kind lands only when it
      is STRICTLY MORE COMPLETE: it agrees with every term already recorded and
      adds at least one that was missing. A source that says less, or says
      something different, is not a refinement -- applying it would flip the
      row back and forth on every import and would make re-importing the same
      file a change.

    Writes through :func:`set_kinds`, so the classification columns keep
    exactly one writer, and a symbol with no ``securities`` row is still
    skipped rather than conjured. Returns the number of rows updated."""
    updates: list = []
    for symbol, stated in rows or []:
        symbol = str(symbol or "").strip()
        if not symbol or stated is None:
            continue
        current = conn.execute(
            "SELECT %s FROM securities WHERE symbol=?" % ", ".join(KIND_COLUMNS),
            (symbol,)).fetchone()
        if current is None or not _supersedes(current, stated):
            continue
        updates.append(KindUpdate(
            symbol=symbol, kind=stated.kind, kind_source=stated.kind_source,
            **{f: getattr(stated, f) for f in _TERM_FIELDS}))
    return set_kinds(conn, updates)


def _supersedes(current, stated: SourceKind) -> bool:
    """May ``stated`` replace the classification already on this row?"""
    if current["kind"] is None:
        return True
    source = (current["kind_source"] or "").strip().lower()
    if source == "user":
        return False
    new_rank = _KIND_RANK.get((stated.kind_source or "").strip().lower(), 0)
    old_rank = _KIND_RANK.get(source, 0)
    if new_rank != old_rank:
        return new_rank > old_rank
    return _more_complete(current, stated)


def _more_complete(current, stated: SourceKind) -> bool:
    """Does ``stated`` say everything the row already says, and something more?

    Disagreement is not completeness. Two equally ranked sources differing
    about what the instrument IS, or about a strike, is not something an import
    gets to settle -- what is recorded stands until a person or a higher-ranked
    source says otherwise."""
    if (current["kind"] or "") != stated.kind:
        return False
    gained = False
    for field in _TERM_FIELDS:
        have, new = current[field], getattr(stated, field)
        if have is None:
            gained = gained or new is not None
        elif new is None or not _same_term(field, have, new):
            return False
    return gained


def _same_term(field: str, have, new) -> bool:
    """Compare two stated terms by VALUE. ``multiplier`` and ``strike`` are
    Decimal-encoded text, so "100" and "100.00" are the same statement and must
    not read as a refinement."""
    if field in ("multiplier", "strike"):
        try:
            return Decimal(str(have)) == Decimal(str(new))
        except InvalidOperation:
            pass
    return str(have).strip().upper() == str(new).strip().upper()


def names_by_ticker(conn, symbols: Iterable[str]) -> dict:
    """``{ticker: [stored symbol, ...]}`` for a quote fetch.

    Once identities ARE tickers this is very nearly the identity mapping, which
    is the point: :func:`investments.fetch_quotes` needs to know which stored
    rows a quote should land on, and after the split there is exactly one."""
    out: dict = {}
    for sym in symbols:
        sym = str(sym or "").strip()
        if not sym:
            continue
        tick = investments.ticker_of(sym) or sym
        out.setdefault(tick.upper(), []).append(sym)
    return out

"""Classification audit over ``securities`` -- what each row IS, and what got
fused into it. Read-only (SRD 5.8e-2b, instrument taxonomy Part 3 Item 4.1).

Migration ``_V67`` added ``kind`` and the option terms and deliberately did not
fill them: a 40-year ledger is exactly where a blind backfill does its damage,
and ``NULL`` kind means UNCLASSIFIED, so a file that has never been through this
report behaves precisely as it always did. This module is the safe half of the
backfill -- it looks, counts and proposes, and writes nothing. The confirmed
write lives in :func:`mammon.securities.set_kinds`, behind a human.

Three things are reported per row, and the third is the one the user actually
needs:

* the CURRENT kind (``None`` = unclassified, never equity),
* the PROPOSED kind from :func:`mammon.instruments.classify` plus, for a
  contract, the terms :func:`~mammon.instruments.parse_option` or
  :func:`~mammon.instruments.parse_legacy_option` can read out of it,
* whether this row is FUSED or points somewhere else.

The fusion checks exist because ``securities.apply_splits`` used to treat an
option contract as a SPELLING of its underlying, and ``_rekey`` applies such a
proposal by moving what fits and deleting the losing row. What is left behind is
visible in three shapes, and each is reported separately rather than rolled into
one alarm:

``ticker_mismatch``
    the row's stored ``ticker`` is not its own symbol. On its own this is
    benign and expected -- "VGT VANGUARD INFO TECH ETF" with ticker ``VGT`` is
    the ordinary rename case.
``ticker_points_elsewhere``
    the row IS a contract and its ticker is the UNDERLYING's. That ticker is
    what a quote fetch and a merge proposal both key on, so this row is one
    Apply away from being absorbed into the stock.
``fused``
    one row that is two instruments: an identity that does not parse as a
    contract carrying a description that does. This is what a completed merge
    of a contract into its stock looks like from the securities table alone.

Groups of symbols sharing one effective ticker are reported too, because a
group whose members propose DIFFERENT kinds (a stock and three of its options)
is the population the destructive merge was drawn from.

Nothing here deletes, rekeys or classifies anything; the un-fuse operation
(Item 4.3) is not in this module and cannot be built out of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from mammon import instruments, investments

# The securities columns this report reads. Listed as data so the SELECT and the
# row shape cannot drift apart, and so a reader can see at a glance that the
# identity columns are read and never written.
COLUMNS = ("symbol", "name", "sec_type", "ticker", "kind", "kind_source",
           "multiplier", "underlying", "expiration", "strike", "option_right")

# Type words that entitle this report to try the PRE-2010 OPRA decoder.
# `parse_legacy_option` is not a detector -- "IBM" is a perfectly good legacy
# symbol -- so it is only ever asked about a row some source already called an
# option, exactly as `securities.classify_source` does.
_OPTION_TYPE_WORDS = frozenset({"option", "options", "option contract"})


@dataclass(frozen=True)
class KindProposal:
    """What this row would be classified as, and the terms behind that.

    ``kind_source`` is ``'derived'``: this is the report's own provenance, a
    classification read out of the strings. A caller that puts the proposal in
    front of a person and gets a yes writes ``'user'`` instead -- the whole
    reason the column is three-valued.

    ``unknown`` names the term fields the source genuinely could not supply (a
    pre-2010 symbol encodes neither the year nor an unambiguous strike). Those
    come back None and NAMED, never defaulted: a manufactured $30 strike on a
    $130 contract is indistinguishable from a real one once it is stored.
    """
    kind: Optional[str]
    kind_source: str = "derived"
    multiplier: Optional[str] = None
    underlying: Optional[str] = None
    expiration: Optional[str] = None
    strike: Optional[str] = None
    option_right: Optional[str] = None
    unknown: Tuple[str, ...] = ()

    @property
    def is_option(self) -> bool:
        return self.kind == instruments.Kind.OPTION.value

    @property
    def has_terms(self) -> bool:
        return any((self.underlying, self.expiration, self.strike,
                    self.option_right))


@dataclass(frozen=True)
class SecurityRow:
    """One ``securities`` row, as seen by the audit."""
    symbol: str
    name: Optional[str]
    sec_type: Optional[str]
    ticker: Optional[str]
    kind: Optional[str]
    kind_source: Optional[str]
    proposal: KindProposal
    group_key: str
    ticker_mismatch: bool = False
    ticker_points_elsewhere: bool = False
    fused: bool = False
    note: str = ""

    @property
    def classified(self) -> bool:
        """True when a kind is already recorded. NULL is unclassified."""
        return bool(self.kind)

    @property
    def changes(self) -> bool:
        """True when applying the proposal would change the stored kind."""
        return (self.proposal.kind or None) != (self.kind or None)

    @property
    def suspect(self) -> bool:
        """True when this row needs a person to look at it, not just tick it."""
        return self.fused or self.ticker_points_elsewhere


@dataclass(frozen=True)
class TickerGroup:
    """Symbols that resolve to one ticker -- the shape a merge is drawn from."""
    ticker: str
    symbols: Tuple[str, ...]
    kinds: Tuple[Optional[str], ...]

    @property
    def mixed(self) -> bool:
        """True when the members are not all the same proposed kind, i.e. this
        group holds a stock and its contracts under one ticker."""
        return len(set(self.kinds)) > 1


@dataclass(frozen=True)
class SecurityAudit:
    """The whole report. ``counts`` is plain ints for a summary line."""
    rows: List[SecurityRow] = field(default_factory=list)
    groups: List[TickerGroup] = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    def by_symbol(self, symbol: str) -> Optional[SecurityRow]:
        for r in self.rows:
            if r.symbol == symbol:
                return r
        return None

    @property
    def suspect_rows(self) -> List[SecurityRow]:
        return [r for r in self.rows if r.suspect]


def _text(value) -> str:
    return str(value or "").strip()


def _stated(value) -> Optional[str]:
    """A term's stored TEXT, or None for the UNKNOWN sentinel.

    A term nobody stated stays NULL and is named in ``unknown``; it is never
    defaulted into a number, and a Decimal never becomes a float.
    """
    if value is None or value is instruments.UNKNOWN:
        return None
    return str(value)


def _from_terms(terms) -> KindProposal:
    """A full modern contract: every term is stated except, sometimes, the
    multiplier of an adjusted or mini root."""
    unknown = () if terms.multiplier is not instruments.UNKNOWN \
        else ("multiplier",)
    return KindProposal(
        kind=instruments.Kind.OPTION.value,
        multiplier=_stated(terms.multiplier),
        underlying=terms.underlying,
        expiration=terms.expiration,
        strike=_stated(terms.strike),
        option_right=terms.right,
        unknown=unknown)


def _from_legacy(legacy) -> KindProposal:
    """A pre-2010 OPRA symbol: the underlying, the month and the right are in
    there; the year, the strike and the multiplier are not, and stay NULL."""
    return KindProposal(
        kind=instruments.Kind.OPTION.value,
        underlying=legacy.underlying,
        expiration=_stated(legacy.expiration),
        strike=_stated(legacy.strike),
        option_right=legacy.right,
        unknown=tuple(legacy.unknown_fields()) + ("multiplier",))


def _propose(symbol: str, name: Optional[str], sec_type: Optional[str],
             kind: Optional[str]) -> Tuple[KindProposal, bool]:
    """Classify one row from its own strings. Never raises.

    Returns the proposal and whether the row is FUSED. Order matters here: a
    row whose IDENTITY is not a contract but whose DESCRIPTION is one is two
    instruments in one row, and the answer to that is not a classification. It
    proposes the kind the row already has -- i.e. no change -- so that a
    confirmation screen cannot quietly restate a stock as an option, or the
    reverse. Un-fusing is a separate, explicitly confirmed operation.
    """
    sym_terms = instruments.parse_option(symbol)
    name_terms = instruments.parse_option(name)
    if sym_terms is None and name_terms is not None:
        return KindProposal(kind=kind), True
    if sym_terms is not None:
        return _from_terms(sym_terms), False

    kind_value = instruments.classify(symbol=symbol, name=name,
                                      sec_type=sec_type).value

    # Only now, and only for a row something already calls an option: the
    # pre-2010 decoder says "yes" to plenty of ordinary tickers.
    already_option = (_text(kind).lower() == instruments.Kind.OPTION.value
                      or _text(sec_type).lower() in _OPTION_TYPE_WORDS
                      or kind_value == instruments.Kind.OPTION.value)
    if already_option:
        legacy = (instruments.parse_legacy_option(symbol)
                  or instruments.parse_legacy_option(name))
        if legacy is not None:
            return _from_legacy(legacy), False

    return KindProposal(kind=kind_value), False


def _effective_ticker(symbol: str, ticker: Optional[str],
                      proposal: KindProposal) -> str:
    """The ticker this row would be quoted and merged under.

    The stored ticker when a source stated one, the leading-token heuristic
    otherwise -- which is precisely why a contract groups with its stock here:
    ``investments.ticker_of`` reads the root out of both, and that is the
    collision this report exists to show.
    """
    stated = _text(ticker)
    if stated:
        return stated.upper()
    if proposal.is_option and proposal.underlying:
        return proposal.underlying.upper()
    return (investments.ticker_of(symbol) or symbol).upper()


def _flags(symbol: str, ticker: Optional[str], proposal: KindProposal,
           fused: bool) -> Tuple[bool, bool, str]:
    """The ticker signals, plus a sentence naming whichever one fired."""
    sym = _text(symbol)
    stated = _text(ticker)
    mismatch = bool(stated) and stated.upper() != sym.upper()

    points_elsewhere = False
    if mismatch and proposal.is_option:
        # The ticker is a different instrument unless it is this same contract
        # spelled another way.
        stated_terms = instruments.parse_option(stated)
        same_contract = (stated_terms is not None
                         and proposal.expiration == stated_terms.expiration
                         and proposal.option_right == stated_terms.right
                         and proposal.strike == _stated(stated_terms.strike)
                         and (proposal.underlying or "").upper()
                         == stated_terms.underlying.upper())
        points_elsewhere = not same_contract

    if fused:
        note = "identity is not a contract but its description is one"
    elif points_elsewhere:
        note = "contract filed under ticker %s (its underlying)" % stated
    elif mismatch:
        note = "ticker %s differs from the stored symbol" % stated
    else:
        note = ""
    return mismatch, points_elsewhere, note


def audit(conn, symbols: Optional[Iterable[str]] = None) -> SecurityAudit:
    """Audit every ``securities`` row (or just ``symbols``). Reads only.

    One SELECT, no transaction of its own, no write of any kind -- run it on a
    real 40-year ledger and the file is unchanged, which is the property the
    whole "report before action" split is for.
    """
    wanted = None if symbols is None else {_text(s) for s in symbols}
    rows: List[SecurityRow] = []
    groups: dict = {}

    for raw in conn.execute(
            "SELECT %s FROM securities ORDER BY symbol" % ", ".join(COLUMNS)):
        symbol = _text(raw["symbol"])
        if not symbol or (wanted is not None and symbol not in wanted):
            continue
        name = raw["name"]
        sec_type = raw["sec_type"]
        ticker = raw["ticker"]
        kind = raw["kind"] or None

        proposal, fused = _propose(symbol, name, sec_type, kind)
        group_key = _effective_ticker(symbol, ticker, proposal)
        mismatch, elsewhere, note = _flags(symbol, ticker, proposal, fused)

        rows.append(SecurityRow(
            symbol=symbol, name=name, sec_type=sec_type, ticker=ticker,
            kind=kind, kind_source=raw["kind_source"], proposal=proposal,
            group_key=group_key, ticker_mismatch=mismatch,
            ticker_points_elsewhere=elsewhere, fused=fused, note=note))
        groups.setdefault(group_key, []).append(
            (symbol, proposal.kind))

    shared = [TickerGroup(ticker=key,
                          symbols=tuple(s for s, _ in members),
                          kinds=tuple(k for _, k in members))
              for key, members in sorted(groups.items())
              if len(members) > 1]

    counts = {
        "total": len(rows),
        "classified": sum(1 for r in rows if r.classified),
        "unclassified": sum(1 for r in rows if not r.classified),
        "changes": sum(1 for r in rows if r.changes),
        "proposed_options": sum(1 for r in rows if r.proposal.is_option),
        "incomplete_terms": sum(1 for r in rows if r.proposal.unknown),
        "ticker_mismatch": sum(1 for r in rows if r.ticker_mismatch),
        "ticker_points_elsewhere": sum(
            1 for r in rows if r.ticker_points_elsewhere),
        "fused": sum(1 for r in rows if r.fused),
        "shared_ticker_groups": len(shared),
        "shared_ticker_symbols": sum(len(g.symbols) for g in shared),
        "mixed_kind_groups": sum(1 for g in shared if g.mixed),
    }
    return SecurityAudit(rows=rows, groups=shared, counts=counts)

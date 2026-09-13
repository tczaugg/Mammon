"""QFX/OFX parser for ongoing bank / card / investment downloads.

OFX comes in two dialects: OFX 1.x is SGML with frequently-UNCLOSED leaf tags,
OFX 2.x is well-formed XML. We avoid a strict XML parser (it chokes on 1.x) and
instead bound each ``<STMTTRN>`` block by the next delimiter and pull leaf tags
with a tolerant regex, which works for both dialects.

Cash lives in ``<STMTTRN>``. A bank/CC statement's STMTTRN is a cash row; an
``<INVBANKTRAN>`` inside an INVESTMENT statement is the brokerage's own cash
activity (interest credited, account fees) and becomes an INVESTMENT row, not a
cash one -- see :func:`_parse_cash`. Investment security actions live in
``<BUYSTOCK>``/``<SELLSTOCK>``/``<INCOME>``/``<REINVEST>`` aggregates and are
parsed best-effort.

A statement also names its own CURRENCY: ``<CURDEF>`` is an ISO 4217 code (OFX
2.2 sec 5.2), carried out on ``NormalizedTxn.account_currency`` so a
foreign-currency feed creates its account in its real currency instead of
silently inheriting the USD default. OFX is the ONLY import format that can
state this -- QIF has no currency field in any version of its spec -- which is
why it is read here rather than asked for in the import UI.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from fractions import Fraction
from decimal import Decimal, InvalidOperation
from typing import Optional

from mammon.importers.record import (
    NormalizedTxn,
    decimal_text,
    dollars_to_cents,
    parse_date,
)

logger = logging.getLogger(__name__)

_LEAF = re.compile(r"<([A-Za-z0-9.]+)>([^<\r\n]*)")

# Simple investment aggregate wrapper -> ledger action. These all share one shape:
# a security (TICKER/UNIQUEID), UNITS/UNITPRICE, and a signed TOTAL cash amount.
_INV_WRAPPERS = [
    ("BUYSTOCK", "Buy"),
    ("BUYMF", "Buy"),
    ("BUYBOND", "Buy"),
    ("BUYOTHER", "Buy"),
    ("SELLSTOCK", "Sell"),
    ("SELLMF", "Sell"),
    ("SELLOTHER", "Sell"),
    ("REINVEST", "ReinvDiv"),
    ("INCOME", "Div"),
]

# The full set of standard OFX <INVTRANLIST> child aggregates (OFX 2.2 sec 13.9).
# Every one either maps to a ledger action here or is reported by the post-import
# audit -- so an action type is NEVER silently swallowed into a cash-in default.
_KNOWN_OFX_INV_ACTIONS = {
    "BUYDEBT", "BUYMF", "BUYOPT", "BUYOTHER", "BUYSTOCK",
    "CLOSUREOPT", "INCOME", "INVEXPENSE", "JRNLFUND", "JRNLSEC",
    "MARGININTEREST", "REINVEST", "RETOFCAP",
    "SELLDEBT", "SELLMF", "SELLOPT", "SELLOTHER", "SELLSTOCK",
    "SPLIT", "TRANSFER",
}
# Every OFX investment aggregate this importer maps to a ledger effect: the simple
# wrappers above, the non-standard BUYBOND alias we also accept, and the six
# special-shape actions handled by the _parse_* helpers below.
_MAPPED_OFX_INV_ACTIONS = {w for w, _ in _INV_WRAPPERS} | {
    "BUYBOND",
    "SPLIT", "TRANSFER", "CLOSUREOPT", "RETOFCAP", "MARGININTEREST", "INVEXPENSE",
}


def _ofx_body(text: str) -> str:
    """Slice from the opening ``<OFX>`` marker (case-insensitive); pass through if
    absent."""
    for marker in ("<OFX>", "<ofx>"):
        i = text.find(marker)
        if i != -1:
            return text[i:]
    return text


def parse_ofx(text: str, default_account: Optional[str] = None) -> list[NormalizedTxn]:
    body = _ofx_body(text)

    acctid = _first(body, "ACCTID")
    account = default_account or (f"OFX:{acctid}" if acctid else "")

    # <SECLIST> maps a SECID UNIQUEID (CUSIP) to its ticker; resolving BOTH
    # transactions and positions through it keeps holdings and the <INVPOS>
    # snapshot keyed by the same symbol, so reconciliation lines up row-for-row.
    sec = _parse_seclist(body)
    records: list[NormalizedTxn] = []
    records.extend(_parse_investment(body, account, sec))
    records.extend(_parse_cash(body, account))
    # <CURDEF> is a property of the STATEMENT (hence of the account), not of any
    # one row, so it is stamped on every record here rather than threaded through
    # each parse helper: core.py takes it from whichever record first resolves the
    # account, and applies it only if that account is being created.
    currency = _statement_currency(body)
    if currency:
        for r in records:
            r.account_currency = currency
    return records


def _statement_currency(body: str) -> str:
    """The statement's own currency from ``<CURDEF>``, as an ISO 4217 code.

    Returns "" when absent or not a plausible code, leaving the account on the
    schema's USD default. The shape check is deliberate: this value lands in
    ``accounts.currency``, which is immutable once set (SRD 5.4a), so a malformed
    feed must not be able to brand an account with junk the user cannot correct
    without rebuilding it.

    A per-transaction ``<CURRENCY>``/``<ORIGCURRENCY>`` aggregate -- an amount in
    some other currency than the statement's -- is deliberately NOT read: mammon
    stores every amount in its account's own currency, so honouring one needs an
    FX rate per row. A mixed-currency statement is separate work, not something to
    approximate silently."""
    code = _first(body, "CURDEF").strip().upper()
    return code if len(code) == 3 and code.isalpha() else ""


# OFX TRNTYPE is a fixed enumeration from the SPEC -- not an institution's own
# activity vocabulary -- so translating it is reading the format, not guessing at
# a broker's wording. Only the unambiguous ones are mapped; anything else keeps
# its raw TRNTYPE for the review list to show and the learned action tree to
# resolve from the user's corrections.
_INVBANK_ACTION = {
    "INT": "IntInc",
    "DIV": "Div",
    "FEE": "MiscExp",
    "SRVCHG": "MiscExp",
    "DEP": "XIn",
    "DIRECTDEP": "XIn",
    "XFER": "XIn",          # sign-corrected below: a debit transfer is XOut
    "CASH": "XOut",
    "DIRECTDEBIT": "XOut",
}


def _invbank_spans(body: str) -> list[tuple]:
    """(start, end) of every ``<INVBANKTRAN>`` aggregate in ``body``."""
    spans = []
    for m in re.finditer(r"<INVBANKTRAN>", body, re.I):
        end = body.upper().find("</INVBANKTRAN>", m.end())
        spans.append((m.start(), len(body) if end == -1 else end))
    return spans


def _parse_cash(body: str, account: str) -> list[NormalizedTxn]:
    """Every ``<STMTTRN>``, routed by the aggregate that CONTAINS it.

    A plain bank/CC STMTTRN is a cash row. One inside ``<INVBANKTRAN>`` is the
    brokerage's own cash activity within an investment account -- interest
    credited, account fees -- and must become an INVESTMENT row.

    Routing it to the cash table (its literal OFX shape) made the destination
    depend on the FILE FORMAT: the same interest payment landed in
    ``investment_transactions`` from a broker's CSV and in ``transactions`` from
    its QFX, so the two could never dedup against each other. Importing both
    formats over one period duplicated every interest and fee row and
    double-counted the account's cash.
    """
    inv_spans = _invbank_spans(body)

    def inside_invbank(pos: int) -> bool:
        return any(lo <= pos < hi for lo, hi in inv_spans)

    out: list[NormalizedTxn] = []
    for m in re.finditer(r"<STMTTRN>", body, flags=re.I):
        chunk = body[m.end():]
        d = _leaves(_bound(chunk, "</STMTTRN>", "</BANKTRANLIST>", "</INVBANKTRAN>"))
        if not d.get("DTPOSTED") and not d.get("DTUSER"):
            continue
        trntype = (d.get("TRNTYPE", "") or "").strip().upper()
        amount = dollars_to_cents(d.get("TRNAMT", "0"))
        if inside_invbank(m.start()):
            action = _INVBANK_ACTION.get(trntype, trntype or "Cash")
            if action == "XIn" and amount < 0:
                action = "XOut"        # the spec's XFER carries either direction
            out.append(
                NormalizedTxn(
                    external_account=account,
                    account_type="investment",
                    date=parse_date(d.get("DTPOSTED") or d.get("DTUSER")),
                    amount_cents=amount,
                    action=action,
                    memo=d.get("MEMO", "") or d.get("NAME", ""),
                    fitid=d.get("FITID", ""),
                    type=trntype,
                )
            )
            continue
        out.append(
            NormalizedTxn(
                external_account=account,
                date=parse_date(d.get("DTPOSTED") or d.get("DTUSER")),
                amount_cents=amount,
                payee=d.get("NAME") or d.get("PAYEE", ""),
                memo=d.get("MEMO", ""),
                check_number=d.get("CHECKNUM", ""),
                fitid=d.get("FITID", ""),
                type=trntype,
            )
        )
    return out


def _parse_investment(body: str, account: str, sec: Optional[dict] = None) -> list[NormalizedTxn]:
    out: list[NormalizedTxn] = []
    # Simple buy/sell/income/reinvest aggregates.
    for wrapper, action in _INV_WRAPPERS:
        for m in re.finditer(rf"<{wrapper}>(.*?)</{wrapper}>", body, re.I | re.S):
            txn = _simple_txn(account, action, _leaves(m.group(1)), sec)
            if txn is not None:
                out.append(txn)
    # Special-shape aggregates that do NOT fit the buy/sell mold.
    out.extend(_parse_split(body, account, sec))
    out.extend(_parse_transfer(body, account, sec))
    out.extend(_parse_closureopt(body, account, sec))
    out.extend(_parse_cash_action(body, account, "RETOFCAP", "RtrnCap", sec))
    out.extend(_parse_cash_action(body, account, "MARGININTEREST", "MargInt", sec))
    out.extend(_parse_cash_action(body, account, "INVEXPENSE", "MiscExp", sec))
    # AUDIT: surface any standard OFX investment action we do not map, so it is
    # visible instead of being silently dropped (and never corrupts holdings).
    for tag in _unmapped_in_body(body):
        logger.warning(
            "OFX import: investment action <%s> is not mapped -- its transaction(s) "
            "were NOT recorded (holdings may be incomplete). Map it in "
            "mammon.importers.ofx before importing this feed.",
            tag,
        )
    return out


def _date_of(d: dict) -> str:
    return d.get("DTTRADE") or d.get("DTSETTLE") or d.get("DTPOSTED") or ""


def _symbol_of(d: dict, sec: Optional[dict] = None) -> str:
    """Ticker for this security: the SECID ``TICKER`` if present, else the ticker
    the ``<SECLIST>`` maps this ``UNIQUEID`` (CUSIP) to, else the raw UNIQUEID."""
    tkr = d.get("TICKER")
    if tkr:
        return tkr
    uid = d.get("UNIQUEID", "")
    if sec and uid in sec:
        return sec[uid]
    return uid


def _simple_txn(account: str, action: str, d: dict, sec: Optional[dict] = None) -> Optional[NormalizedTxn]:
    date = _date_of(d)
    if not date:
        return None
    return NormalizedTxn(
        external_account=account,
        account_type="investment",
        date=parse_date(date),
        amount_cents=dollars_to_cents(d.get("TOTAL", "0")),
        action=action,
        symbol=_symbol_of(d, sec),
        quantity=decimal_text(d.get("UNITS", "")),
        price=decimal_text(d.get("UNITPRICE", "")),
        commission_cents=dollars_to_cents(d.get("COMMISSION", "0")),
        memo=d.get("MEMO", ""),
        fitid=d.get("FITID", ""),
    )


def _parse_cash_action(body: str, account: str, wrapper: str, action: str, sec: Optional[dict] = None) -> list[NormalizedTxn]:
    """Cash-only investment actions (return of capital, margin interest, expense).
    No share change; the ledger action classifies the cash direction (RtrnCap =
    cash in + basis relief; MargInt/MiscExp = cash out)."""
    out: list[NormalizedTxn] = []
    for m in re.finditer(rf"<{wrapper}>(.*?)</{wrapper}>", body, re.I | re.S):
        d = _leaves(m.group(1))
        date = _date_of(d)
        if not date:
            continue
        out.append(
            NormalizedTxn(
                external_account=account,
                account_type="investment",
                date=parse_date(date),
                amount_cents=dollars_to_cents(d.get("TOTAL", "0")),
                action=action,
                symbol=_symbol_of(d, sec),
                memo=d.get("MEMO", ""),
                fitid=d.get("FITID", ""),
            )
        )
    return out


def _parse_transfer(body: str, account: str, sec: Optional[dict] = None) -> list[NormalizedTxn]:
    """<TRANSFER>: securities move in/out of the account with NO cash effect here.
    TFERACTION=OUT relieves shares (ShrsOut, not a sale -> no realized P/L);
    anything else adds them (ShrsIn)."""
    out: list[NormalizedTxn] = []
    for m in re.finditer(r"<TRANSFER>(.*?)</TRANSFER>", body, re.I | re.S):
        d = _leaves(m.group(1))
        date = _date_of(d)
        if not date:
            continue
        out_dir = (d.get("TFERACTION", "") or "").strip().upper() == "OUT"
        out.append(
            NormalizedTxn(
                external_account=account,
                account_type="investment",
                date=parse_date(date),
                amount_cents=0,
                action="ShrsOut" if out_dir else "ShrsIn",
                symbol=_symbol_of(d, sec),
                quantity=_abs_units(d.get("UNITS", "")),
                price=decimal_text(d.get("UNITPRICE", "")),
                memo=d.get("MEMO", ""),
                fitid=d.get("FITID", ""),
            )
        )
    return out


def _parse_closureopt(body: str, account: str, sec: Optional[dict] = None) -> list[NormalizedTxn]:
    """<CLOSUREOPT>: an option position is closed (exercise/assign/expire). Model
    it as removing the option units (RemoveShares -- cash-neutral, no realized P/L;
    any cash from an exercise settles via its own BUY/SELL leg). Short options are
    rare and out of scope, so the units are relieved as a long close."""
    out: list[NormalizedTxn] = []
    for m in re.finditer(r"<CLOSUREOPT>(.*?)</CLOSUREOPT>", body, re.I | re.S):
        d = _leaves(m.group(1))
        date = _date_of(d)
        if not date:
            continue
        out.append(
            NormalizedTxn(
                external_account=account,
                account_type="investment",
                date=parse_date(date),
                amount_cents=0,
                action="RemoveShares",
                symbol=_symbol_of(d, sec),
                quantity=_abs_units(d.get("UNITS", "")),
                memo=d.get("MEMO", ""),
                fitid=d.get("FITID", ""),
            )
        )
    return out


def _parse_split(body: str, account: str, sec: Optional[dict] = None) -> list[NormalizedTxn]:
    """<SPLIT>: a stock split, cash-neutral.

    OFX states the ratio as NUMERATOR/DENOMINATOR, and that pair is carried
    through WHOLE (NormalizedTxn.split_num/split_den) rather than divided: a 4:3
    split has no exact decimal form, and pre-dividing would leave a share count
    of 399.99999999999999 where the broker says 400. The legacy per-ten
    ``quantity`` is filled alongside it for readers that predate the pair."""
    out: list[NormalizedTxn] = []
    for m in re.finditer(r"<SPLIT>(.*?)</SPLIT>", body, re.I | re.S):
        d = _leaves(m.group(1))
        date = _date_of(d)
        if not date:
            continue
        pair = _split_pair(d)
        ratio = _split_ratio_per_ten(d)
        if ratio is None and pair is None:
            logger.warning(
                "OFX import: <SPLIT> for %r lacks NUMERATOR/DENOMINATOR and "
                "OLD/NEWUNITS -- skipped (share count unchanged).",
                _symbol_of(d, sec) or "?",
            )
            continue
        out.append(
            NormalizedTxn(
                external_account=account,
                account_type="investment",
                date=parse_date(date),
                amount_cents=0,
                action="StkSplit",
                symbol=_symbol_of(d, sec),
                quantity=ratio or "",
                split_num=pair[0] if pair else 0,
                split_den=pair[1] if pair else 0,
                memo=d.get("MEMO", ""),
                fitid=d.get("FITID", ""),
            )
        )
    return out


def _split_pair(d: dict) -> Optional[tuple]:
    """The split's EXACT (new, old) ratio as integers, from NUMERATOR/DENOMINATOR
    or NEWUNITS/OLDUNITS. None when the aggregate states neither."""
    def _dec(key):
        txt = decimal_text(d.get(key, ""))
        try:
            return Decimal(txt) if txt else None
        except InvalidOperation:
            return None

    for a, b in (("NUMERATOR", "DENOMINATOR"), ("NEWUNITS", "OLDUNITS")):
        new, old = _dec(a), _dec(b)
        if new is not None and old not in (None, Decimal(0)):
            frac = Fraction(new) / Fraction(old)
            return frac.numerator, frac.denominator
    return None


def _split_ratio_per_ten(d: dict) -> Optional[str]:
    """OFX split ratio -> mammon's 'new shares per 10 old' quantity, or None if the
    aggregate carries neither the NUMERATOR/DENOMINATOR ratio nor OLD/NEWUNITS."""
    def _dec(key):
        txt = decimal_text(d.get(key, ""))
        try:
            return Decimal(txt) if txt else None
        except InvalidOperation:
            return None
    num, den = _dec("NUMERATOR"), _dec("DENOMINATOR")
    if num is not None and den not in (None, Decimal(0)):
        return str(Decimal(10) * num / den)
    new, old = _dec("NEWUNITS"), _dec("OLDUNITS")
    if new is not None and old not in (None, Decimal(0)):
        return str(Decimal(10) * new / old)
    return None


def _abs_units(value) -> str:
    """Share quantity as clean, unsigned Decimal text -- direction is carried by
    the ledger action (ShrsIn/ShrsOut), not the sign, so a feed that stores an
    'out' transfer as negative units does not double-negate."""
    txt = decimal_text(value)
    return txt[1:] if txt.startswith("-") else txt


def _unmapped_in_body(body: str) -> list[str]:
    """Standard OFX investment action tags present in ``body`` that this importer
    does not map to a ledger effect (sorted, distinct)."""
    return [
        tag
        for tag in sorted(_KNOWN_OFX_INV_ACTIONS - _MAPPED_OFX_INV_ACTIONS)
        if re.search(rf"<{tag}>", body, re.I)
    ]


def unmapped_investment_actions(text: str) -> list[str]:
    """Post-import AUDIT hook: the standard OFX investment action types present in
    ``text`` that this importer leaves unmapped. Surfaced on
    :class:`ImportResult.unmapped_actions` so an unhandled action is visible
    instead of silently dropped."""
    return _unmapped_in_body(_ofx_body(text))


@dataclass
class OfxPosition:
    """A broker-reported holding snapshot from ``<INVPOSLIST>/<INVPOS>``: how many
    units the statement says are held, and their per-share ``UNITPRICE`` as of
    ``date`` (the position's ``DTPRICEASOF`` or the statement ``DTASOF``)."""
    symbol: str
    date: str          # ISO YYYY-MM-DD
    units: str         # Decimal text (unsigned share count as reported)
    unitprice: str     # Decimal text, dollars/share (NOT cents)


def _parse_seclist(body: str) -> dict:
    """Map ``SECID`` ``UNIQUEID`` (usually a CUSIP) -> ticker symbol from the
    ``<SECLIST>``'s ``<SECINFO>`` blocks, so an ``<INVPOS>`` that identifies its
    security only by CUSIP resolves to the ticker that transactions/holdings use."""
    out: dict[str, str] = {}
    for m in re.finditer(r"<SECINFO>(.*?)</SECINFO>", body, re.I | re.S):
        d = _leaves(m.group(1))
        uid, tkr = d.get("UNIQUEID", ""), d.get("TICKER", "")
        if uid and tkr:
            out.setdefault(uid, tkr)
    return out


def parse_ofx_positions(text: str) -> list[OfxPosition]:
    """Parse the ``<INVPOSLIST>/<INVPOS>`` holdings snapshot of an investment
    statement. Each ``<INVPOS>`` carries ``UNITS`` and a ``UNITPRICE`` as of
    ``DTPRICEASOF`` (falling back to the statement ``DTASOF``). Surfacing these
    lets the caller (a) land the ``UNITPRICE`` in ``price_history`` keyed to that
    date -- valuation then picks the latest price on/before any as-of -- and
    (b) reconcile the reported share counts against holdings computed from
    transactions, rather than overwriting them. Aggregates (``INVPOS``, ``SECID``)
    are always closed even in OFX 1.x SGML, so bounding on ``</INVPOS>`` is safe."""
    body = _ofx_body(text)
    sec = _parse_seclist(body)
    stmt_date = _first(body, "DTASOF")
    out: list[OfxPosition] = []
    for m in re.finditer(r"<INVPOS>(.*?)</INVPOS>", body, re.I | re.S):
        d = _leaves(m.group(1))
        symbol = (
            d.get("TICKER")
            or sec.get(d.get("UNIQUEID", ""), "")
            or d.get("UNIQUEID", "")
        )
        raw_date = d.get("DTPRICEASOF") or stmt_date
        if not symbol or not raw_date:
            continue
        out.append(
            OfxPosition(
                symbol=symbol,
                date=parse_date(raw_date),
                units=_abs_units(d.get("UNITS", "")),
                unitprice=decimal_text(d.get("UNITPRICE", "")),
            )
        )
    return out


def _bound(chunk: str, *closers: str) -> str:
    upper = chunk.upper()
    cut = len(chunk)
    for closer in closers:
        j = upper.find(closer.upper())
        if j != -1:
            cut = min(cut, j)
    return chunk[:cut]


def _leaves(chunk: str) -> dict:
    d: dict[str, str] = {}
    for tag, val in _LEAF.findall(chunk):
        key = tag.upper()
        val = val.strip()
        if val and key not in d:      # first non-empty wins; ignore aggregate opens
            d[key] = val
    return d


def _first(body: str, tag: str) -> str:
    m = re.search(rf"<{tag}>([^<\r\n]+)", body, re.I)
    return m.group(1).strip() if m else ""

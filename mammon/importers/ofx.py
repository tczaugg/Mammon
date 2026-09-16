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

OPTIONS are carried through rather than flattened. A ``<SECLIST>``'s
``<OPTINFO>`` states the contract outright -- OPTTYPE (call/put), STRIKEPRICE,
DTEXPIRE, SHPERCTRCT (shares per contract) and the UNDERLYING security's SECID
-- so every broker spelling of the same contract is canonicalized to ONE 21-char
OSI symbol via :mod:`mammon.instruments` (``XYZ   260117C00150000``). Two option
contracts on the same underlying are different securities with different strikes
and expirations; collapsing them onto the underlying's ticker (or onto each
other) silently fuses unrelated positions, so it is never done here -- the OSI
string is the identity. Where a feed states only partial terms (a pre-2010 OPRA
symbol has no strike or day) the unknown parts stay unknown: the broker's own
spelling is kept as the symbol rather than a guessed contract.

The parsed terms have nowhere to PERSIST yet -- ``securities`` has no strike /
expiration / multiplier column -- so they ride out of the parser on the
NormalizedTxn as an :class:`OfxOptionLeg` (see :func:`option_leg`), which
``importers/core.py`` currently ignores. When those columns land, core.py reads
the leg and writes them; nothing else about this parser has to change. The
contract MULTIPLIER matters for exactly that reason: a 2-contract trade moves 200
shares of deliverable, and the day an amount is derived from quantity x price it
must be multiplied by it. This parser never derives one -- the OFX ``<TOTAL>`` is
authoritative and taken verbatim -- so no multiplier arithmetic happens here.

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

from mammon import instruments
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
    "BUYOPT", "SELLOPT",
}

# <OPTINFO> OPTTYPE -> the OSI right letter.
_OPT_RIGHT = {"CALL": "C", "PUT": "P", "C": "C", "P": "P"}

# <BUYOPT>/<SELLOPT> carry an open/close flag, and it decides the LEDGER ACTION,
# not just a label: writing an option (SELLTOOPEN) opens a SHORT position, and
# recording it as a plain Sell would relieve units the account never held and
# invent a realized gain. Mapping the four flags onto the existing short-position
# vocabulary (ShtSell opens a short, CvrShrt closes one) keeps holdings correct
# without teaching the domain layer a new action.
_OPT_BUY_ACTIONS = {                     # <OPTBUYTYPE>
    "BUYTOOPEN":  ("Buy", "open"),       # long the contract
    "BUYTOCLOSE": ("CvrShrt", "close"),  # buy back a contract we wrote
}
_OPT_SELL_ACTIONS = {                    # <OPTSELLTYPE>
    "SELLTOOPEN":  ("ShtSell", "open"),  # write the contract: SHORT, cash in
    "SELLTOCLOSE": ("Sell", "close"),    # sell a contract we hold
}


@dataclass
class OfxOptionLeg:
    """The option facts a parsed row carries that ``investment_transactions`` has
    nowhere to store yet: the contract's terms, its multiplier, and the link to
    the share leg of an exercise/assignment.

    Attached to the NormalizedTxn as ``.option`` (read it with
    :func:`option_leg`) so the information survives the parse instead of being
    discarded at the point it is known. The importer core ignores it today; when
    the securities table grows kind/strike/expiration/multiplier columns, the
    handoff is to read this leg there -- no reparse, no second guess at the
    symbol."""
    symbol: str = ""            # canonical 21-char OSI, or the feed's spelling
    underlying: str = ""        # underlying TICKER (never used as the symbol)
    expiration: str = ""        # ISO YYYY-MM-DD, "" if the feed did not say
    strike: str = ""            # Decimal text, dollars/share (NOT cents)
    right: str = ""             # "C" | "P" | ""
    multiplier: str = ""        # Decimal text: deliverable shares per contract
    open_close: str = ""        # "open" | "close" | "" (unstated)
    opt_action: str = ""        # EXERCISE | ASSIGN | EXPIRE | ""
    related_fitid: str = ""     # <RELFITID>: the linked share/offsetting leg
    terms: object = None        # instruments.OptionTerms, when fully recovered
    partial_terms: object = None  # instruments.PartialOptionTerms, when not


def option_leg(txn: NormalizedTxn) -> Optional[OfxOptionLeg]:
    """The :class:`OfxOptionLeg` attached to a parsed row, or None if the row is
    not an option."""
    return getattr(txn, "option", None)


def _attach_option(txn: NormalizedTxn, d: dict, sec: Optional[dict], **extra) -> NormalizedTxn:
    """Hang the option facts for this row on the record, merging what the
    ``<SECLIST>`` knows about the contract with what the transaction aggregate
    states itself (the aggregate's own SHPERCTRCT wins -- it describes THIS
    trade, e.g. an adjusted contract delivering 87 shares)."""
    entry = _sec_entry(d, sec)
    leg = OfxOptionLeg(symbol=txn.symbol, **extra)
    if entry is not None and entry.is_option:
        leg.underlying = entry.underlying_symbol
        leg.multiplier = entry.multiplier
        leg.terms = entry.option
        leg.partial_terms = entry.partial_option
        if entry.option is not None:
            leg.expiration = entry.option.expiration
            leg.strike = str(entry.option.strike)
            leg.right = entry.option.right
    own_mult = decimal_text(d.get("SHPERCTRCT", ""))
    if own_mult:
        leg.multiplier = own_mult
    txn.option = leg
    return txn


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
    out.extend(_parse_option_trades(body, account, sec))
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


def _sec_entry(d: dict, sec: Optional[dict]):
    """The ``<SECLIST>`` entry this SECID resolves to, or None."""
    return sec.get(d.get("UNIQUEID", "")) if sec else None


def _symbol_of(d: dict, sec: Optional[dict] = None) -> str:
    """Ticker for this security: the SECID ``TICKER`` if present, else the ticker
    the ``<SECLIST>`` maps this ``UNIQUEID`` (CUSIP) to, else the raw UNIQUEID.

    An OPTION resolved through the SECLIST overrides the row's own TICKER: the
    SECLIST knows the contract's terms, so its canonical OSI spelling is the one
    identity every row for that contract must share."""
    entry = _sec_entry(d, sec)
    if entry is not None and entry.is_option:
        return entry.symbol
    tkr = d.get("TICKER")
    if tkr:
        return tkr
    return entry.symbol if entry is not None else d.get("UNIQUEID", "")


def _option_symbol_of(d: dict, sec: Optional[dict] = None) -> str:
    """Symbol for a row we KNOW is an option (it sits in an option aggregate).

    Prefers the SECLIST's canonical OSI; failing that, canonicalizes the row's
    own TICKER, because a feed with no ``<SECLIST>`` still spells the contract
    somehow and every spelling of one contract must land on one symbol. An
    unrecognizable spelling is left exactly as the broker wrote it -- never
    truncated toward the underlying, which would fuse distinct contracts."""
    entry = _sec_entry(d, sec)
    if entry is not None and entry.is_option:
        return entry.symbol
    raw = d.get("TICKER", "")
    if raw:
        terms = instruments.parse_option(raw)
        if terms is not None:
            return terms.osi()
    return _symbol_of(d, sec)


def _simple_txn(
    account: str,
    action: str,
    d: dict,
    sec: Optional[dict] = None,
    symbol: Optional[str] = None,
) -> Optional[NormalizedTxn]:
    date = _date_of(d)
    if not date:
        return None
    return NormalizedTxn(
        external_account=account,
        account_type="investment",
        date=parse_date(date),
        amount_cents=dollars_to_cents(d.get("TOTAL", "0")),
        action=action,
        symbol=_symbol_of(d, sec) if symbol is None else symbol,
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


def _parse_option_trades(body: str, account: str, sec: Optional[dict] = None) -> list[NormalizedTxn]:
    """<BUYOPT>/<SELLOPT>: an option contract is bought or sold.

    These were formerly UNMAPPED -- reported by the audit and dropped -- which
    left an options account's holdings permanently short whatever it traded.
    They share the plain buy/sell shape (UNITS = CONTRACTS, UNITPRICE = premium
    per share, TOTAL = the signed cash), so the only real work is the open/close
    flag: OPTBUYTYPE/OPTSELLTYPE decide whether the row opens or closes, and a
    SELLTOOPEN is a short, not a sale (see _OPT_SELL_ACTIONS).

    UNITS is taken as a MAGNITUDE. OFX signs it (a sell reports negative units),
    but the ledger carries direction in the ACTION alone, and
    ``investments.share_qty_delta`` negates a Sell's quantity itself -- so a
    negative quantity on a Sell would ADD contracts to the position."""
    out: list[NormalizedTxn] = []
    for wrapper, flag_tag, table, default in (
        ("BUYOPT", "OPTBUYTYPE", _OPT_BUY_ACTIONS, ("Buy", "")),
        ("SELLOPT", "OPTSELLTYPE", _OPT_SELL_ACTIONS, ("Sell", "")),
    ):
        for m in re.finditer(rf"<{wrapper}>(.*?)</{wrapper}>", body, re.I | re.S):
            d = _leaves(m.group(1))
            flag = (d.get(flag_tag, "") or "").strip().upper()
            action, open_close = table.get(flag, default)
            txn = _simple_txn(account, action, d, sec, symbol=_option_symbol_of(d, sec))
            if txn is None:
                continue
            txn.quantity = _abs_units(d.get("UNITS", ""))
            out.append(
                _attach_option(
                    txn, d, sec,
                    open_close=open_close,
                    related_fitid=d.get("RELFITID", ""),
                )
            )
    return out


# <CLOSUREOPT> OPTACTION -> (action when the position is LONG, when it is SHORT).
# The three outcomes are genuinely different events, and collapsing them (as this
# importer once did, with a cash-neutral RemoveShares for all of them) throws away
# the only realized result an option position ever has:
#   EXPIRE   the contract dies worthless. A long loses the whole premium and a
#            writer keeps it -- so it settles at a price of ZERO and the lot math
#            realizes the premium. RemoveShares would have hidden that P/L.
#   EXERCISE we exercised a long: the contract is relieved with no result of its
#            own because its cost rolls into the shares that arrive on the linked
#            leg (RELFITID). This one IS cash-neutral, deliberately.
#   ASSIGN   we were assigned on a contract we WROTE. The position is short, so
#            it must be covered (CvrShrt), not "removed" -- removing units from a
#            short position drives it further negative.
_CLOSURE_ACTIONS = {
    "EXPIRE":   ("Sell", "CvrShrt"),
    "EXERCISE": ("RemoveShares", "CvrShrt"),
    "ASSIGN":   ("CvrShrt", "CvrShrt"),
    "":         ("RemoveShares", "CvrShrt"),
}


def _parse_closureopt(body: str, account: str, sec: Optional[dict] = None) -> list[NormalizedTxn]:
    """<CLOSUREOPT>: an option position ends by exercise, assignment or expiry.

    OPTACTION says which, and RELFITID names the OTHER leg -- the share purchase
    or sale the exercise/assignment produced -- which is carried on the row's
    :class:`OfxOptionLeg` so the two halves can be tied together rather than
    arriving as unrelated rows. No cash moves on THIS row in any case: an
    exercise settles its strike money on the share leg, and an expiry moves none
    at all. What differs is the share effect and the realized result; see
    _CLOSURE_ACTIONS.

    Long vs short is taken from the sign of UNITS (a feed that reports a written
    contract as negative units is telling us the position is short), with ASSIGN
    implying short regardless -- only a writer can be assigned."""
    out: list[NormalizedTxn] = []
    for m in re.finditer(r"<CLOSUREOPT>(.*?)</CLOSUREOPT>", body, re.I | re.S):
        d = _leaves(m.group(1))
        date = _date_of(d)
        if not date:
            continue
        opt_action = (d.get("OPTACTION", "") or "").strip().upper()
        units = decimal_text(d.get("UNITS", ""))
        short = units.startswith("-") or opt_action == "ASSIGN"
        long_action, short_action = _CLOSURE_ACTIONS.get(
            opt_action, _CLOSURE_ACTIONS[""]
        )
        action = short_action if short else long_action
        txn = NormalizedTxn(
            external_account=account,
            account_type="investment",
            date=parse_date(date),
            amount_cents=0,
            action=action,
            symbol=_option_symbol_of(d, sec),
            quantity=_abs_units(units),
            # An expiry is a real settlement at zero: stating the price keeps the
            # row self-describing (and the derived amount consistent at 0).
            price="0" if opt_action == "EXPIRE" else "",
            memo=d.get("MEMO", ""),
            fitid=d.get("FITID", ""),
        )
        out.append(
            _attach_option(
                txn, d, sec,
                opt_action=opt_action,
                open_close="close",
                related_fitid=d.get("RELFITID", ""),
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


@dataclass
class OfxSecurity:
    """One ``<SECLIST>`` entry: the identity a SECID ``UNIQUEID`` (usually a
    CUSIP) resolves to, plus -- for an ``<OPTINFO>`` -- the contract it names."""
    uniqueid: str
    symbol: str                   # ticker, or the canonical OSI for an option
    name: str = ""                # <SECNAME>
    option: object = None         # instruments.OptionTerms when fully recovered
    partial_option: object = None  # instruments.PartialOptionTerms when not
    multiplier: str = ""          # Decimal text: <SHPERCTRCT>, shares/contract
    underlying_symbol: str = ""
    underlying_uniqueid: str = ""
    # The <SECLIST> wrapper tag the feed filed this entry under: OPTINFO,
    # MFINFO, DEBTINFO, STOCKINFO, OTHERINFO -- or "" when it said nothing.
    # This is the feed's own STATEMENT of what the instrument is, and it is
    # kept separate from `option`/`partial_option`, which only record how much
    # of the contract could be recovered. A broker can file an option whose
    # ticker nothing can parse; it is still an option because OPTINFO said so.
    info_type: str = ""

    @property
    def is_option(self) -> bool:
        return self.option is not None or self.partial_option is not None


def _parse_seclist(body: str) -> dict:
    """Map ``SECID`` ``UNIQUEID`` (usually a CUSIP) -> :class:`OfxSecurity` from
    the ``<SECLIST>``, so an ``<INVPOS>`` or transaction that identifies its
    security only by CUSIP resolves to the symbol that holdings use.

    Two passes, and the order matters: plain ``<SECINFO>`` first (that includes
    the one nested INSIDE each OPTINFO, harmlessly), then ``<OPTINFO>``, which
    overwrites its own entry with the full contract AND can resolve its
    underlying's ticker from what pass one collected."""
    out: dict[str, OfxSecurity] = {}
    types = _seclist_info_types(body)
    for m in re.finditer(r"<SECINFO>(.*?)</SECINFO>", body, re.I | re.S):
        d = _leaves(m.group(1))
        uid, tkr = d.get("UNIQUEID", ""), d.get("TICKER", "")
        if uid and tkr and uid not in out:
            out[uid] = OfxSecurity(uniqueid=uid, symbol=tkr, name=d.get("SECNAME", ""),
                                   info_type=types.get(uid, ""))
    for m in re.finditer(r"<OPTINFO>(.*?)</OPTINFO>", body, re.I | re.S):
        entry = _option_security(m.group(1), out)
        if entry is not None:
            out[entry.uniqueid] = entry
    return out


# The ``<SECLIST>`` wrappers that state a type outright. OPTINFO is absent
# because it is handled by its own pass, which needs the block's contents.
_SECLIST_WRAPPERS = ("STOCKINFO", "MFINFO", "DEBTINFO", "OTHERINFO")


def _seclist_info_types(body: str) -> dict:
    """Map ``UNIQUEID`` -> the ``<SECLIST>`` wrapper tag that declared it.

    The wrapper is the only place an OFX file says what KIND of instrument a
    security is; the nested ``<SECINFO>`` carries identity alone. Scanned
    separately rather than folded into the SECINFO pass because the SECINFO
    inside an OPTINFO is matched by that same pass and must not inherit a
    neighbouring wrapper's tag -- it gets its type from the option pass."""
    out: dict[str, str] = {}
    for tag in _SECLIST_WRAPPERS:
        for m in re.finditer(rf"<{tag}>(.*?)</{tag}>", body, re.I | re.S):
            uid = _leaves(m.group(1)).get("UNIQUEID", "")
            if uid:
                out.setdefault(uid, tag)
    return out


def _option_security(chunk: str, known: dict) -> Optional[OfxSecurity]:
    """Build an :class:`OfxSecurity` from one ``<OPTINFO>`` block.

    The block holds TWO SECIDs: the option's own (inside its nested ``<SECINFO>``)
    and the UNDERLYING's (a bare ``<SECID>`` after SHPERCTRCT). They are told
    apart by POSITION -- the chunk is split on ``</SECINFO>`` -- because a flat
    leaf scan keeps the first UNIQUEID it sees and would silently report the
    option's own id as its underlying, aliasing the contract onto itself.

    The canonical symbol is built from the stated terms, NOT from the ticker
    text: brokers spell the same contract as ``XYZ 260117C00150000``,
    ``.XYZ260117C150`` or ``-XYZ260117C150``, and one position must not become
    three. The ticker is still parsed first, for the one thing the terms cannot
    give: an ADJUSTED root (``XYZ1``), which is a different deliverable from the
    plain contract and must survive into the symbol."""
    head, tail = _partition_secinfo(chunk)
    d = _leaves(head)
    t = _leaves(tail)
    uid = d.get("UNIQUEID", "")
    if not uid:
        return None
    ticker, name = d.get("TICKER", ""), d.get("SECNAME", "")
    under_uid = t.get("UNIQUEID", "")
    under_entry = known.get(under_uid)
    under_sym = under_entry.symbol if under_entry is not None else ""

    right = _OPT_RIGHT.get(_pick(t, d, "OPTTYPE").upper(), "")
    strike = _decimal_or_none(_pick(t, d, "STRIKEPRICE"))
    expire_raw = _pick(t, d, "DTEXPIRE")
    try:
        expiration = parse_date(expire_raw) if expire_raw else ""
    except ValueError:
        expiration = ""
    multiplier = decimal_text(_pick(t, d, "SHPERCTRCT"))

    # The broker's own spelling, parsed for its root (and as a fallback source of
    # terms when the OPTINFO is incomplete). SECNAME is tried too -- some feeds
    # put the only readable contract description there.
    parsed = instruments.parse_option(ticker) or instruments.parse_option(name)
    root = (parsed.root if parsed is not None else "") or under_sym or ticker
    underlying = (parsed.underlying if parsed is not None else "") or under_sym or root

    partial = None
    if right and strike is not None and expiration and root:
        terms = instruments.OptionTerms(
            underlying=underlying,
            root=root,
            expiration=expiration,
            strike=strike,
            right=right,
            standard=_is_standard(parsed, root, multiplier),
            mini=parsed.mini if parsed is not None else False,
            multiplier=_multiplier_value(multiplier, parsed),
        )
        symbol = terms.osi()
    elif parsed is not None:
        # The OPTINFO is incomplete but the symbol itself spells the contract out.
        terms = parsed
        symbol = parsed.osi()
    else:
        # Terms are genuinely unknown (a pre-2010 OPRA symbol states no strike or
        # day). Recover what the spelling does say, guess NOTHING, and keep the
        # broker's own text as the symbol -- an invented contract is worse than
        # an unparsed one.
        terms = None
        partial = instruments.parse_legacy_option(ticker) if ticker else None
        symbol = ticker or uid
    return OfxSecurity(
        uniqueid=uid,
        symbol=symbol,
        name=name,
        option=terms,
        partial_option=partial,
        multiplier=multiplier or (str(terms.multiplier) if terms is not None and terms.multiplier else ""),
        underlying_symbol=underlying if terms is not None else under_sym,
        underlying_uniqueid=under_uid,
        info_type="OPTINFO",
    )


def _partition_secinfo(chunk: str) -> tuple[str, str]:
    """Split an ``<OPTINFO>`` at its nested ``</SECINFO>``: (the option's own
    SECINFO, everything after it). Aggregates are closed even in OFX 1.x SGML, so
    the closer is reliable; a block without one yields an empty tail and the
    underlying SECID is simply treated as absent."""
    m = re.search(r"</SECINFO>", chunk, re.I)
    return (chunk, "") if m is None else (chunk[: m.start()], chunk[m.end():])


def _pick(first: dict, second: dict, key: str) -> str:
    return (first.get(key) or second.get(key) or "").strip()


def _decimal_or_none(text: str) -> Optional[Decimal]:
    txt = decimal_text(text)
    if not txt:
        return None
    try:
        return Decimal(txt)
    except InvalidOperation:
        return None


def _is_standard(parsed, root: str, multiplier: str) -> bool:
    """A contract is non-standard if its root carries the adjustment digit (XYZ1)
    or it does not deliver the standard 100 shares."""
    if multiplier and multiplier != "100":
        return False
    if parsed is not None:
        return parsed.standard
    return not root[-1:].isdigit()


def _multiplier_value(multiplier: str, parsed):
    """SHPERCTRCT wins when the feed states it -- it describes THIS contract,
    including an adjusted one delivering an odd share count. Absent, fall back to
    what the symbol implied (which is UNKNOWN, not 100, for an adjusted root: a
    guessed multiplier is how $6.45 gets reported where $645 moved)."""
    if multiplier:
        try:
            return int(Decimal(multiplier))
        except (InvalidOperation, ValueError):
            pass
    return parsed.multiplier if parsed is not None else 100


def parse_ofx_securities(text: str) -> list[OfxSecurity]:
    """The ``<SECLIST>`` entries an investment statement declares, as the feed
    STATED them -- identity, wrapper type, and the contract terms of any
    ``<OPTINFO>``.

    A sidecar channel, like :func:`parse_ofx_positions`: transactions carry the
    symbol and nothing else, so what the file says the instrument IS would be
    thrown away by the time records reach the importer. The caller decides what
    may be persisted (statements never outrank a person's classification); this
    only reports."""
    return list(_parse_seclist(_ofx_body(text)).values())


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
        symbol = _symbol_of(d, sec)
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

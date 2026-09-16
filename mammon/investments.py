"""mammon.investments -- holdings, price history, and account valuation (SRD 5.8).

Sits on mammon.db + mammon.ledger. Investment activity lives in the
``investment_transactions`` table (Buy/Sell/ReinvDiv/Div/IntInc/XIn/XOut/...);
this module DERIVES per-symbol ``holdings`` (quantity + average-cost basis) from
that history, records a per-symbol ``price_history``, and values a holding or a
whole investment account using the latest price.

Money is signed integer cents; share quantities and prices are Decimal-precision
TEXT so no float error creeps into share math. All arithmetic here goes through
Decimal and rounds money HALF_UP at the cents boundary.

A security's SYMBOL is its whole-history identity (``price_history`` and holdings
key on it), so a ticker rename would otherwise split one security in two. The
``security_aliases`` table maps an old ticker to its surviving canonical symbol;
:func:`resolve_symbol` follows it, every price/holdings/valuation lookup routes
through :func:`resolve_symbol` / :func:`_identity_symbols`, and the position
replay folds aliased tickers onto the canonical symbol (:func:`_fold_aliases`).
Continuity is entirely read-time -- no historical row is rewritten -- so both
spellings value as one holding across the rename date. This module is the SOLE
writer of ``security_aliases`` (:func:`add_alias` / :func:`remove_alias`).

``security_aliases`` MEANS RENAME AND ONLY RENAME. It is not a place to record
that one instrument derives from another, and specifically an option contract is
never an alias of its underlying -- it has its own terms, its own price series
and its own expiry. The pull towards that mistake is real, because
:func:`ticker_of` reads the first token of "XYZ 260117C00150000 XYZ 17JAN26 150
C" as "XYZ" and a QIF option block states the root ticker outright, so every
derivation path in this codebase is one step from proposing the collapse.
:func:`looks_like_option` is the interim shape test that refuses it, here and in
``mammon.securities``, until a real instrument classifier exists.

Stock splits are absorbed the same way -- at READ time, nothing rewritten. A
``price_history`` row is stored RAW, in the units that were trading the day it
was recorded, and :func:`price_history` / :func:`price_history_bounds` divide
each as-traded price by the exact cumulative factor of every ``StkSplit`` dated
after it, so the plotted series reads in today's units end to end. The
alternative -- restating the stored prices when a split lands -- is a one-way
door: a mistyped ratio could not be undone, deleting a split entered by accident
could not put the old numbers back, and a second pass over the same history
would divide twice. Recomputing from the split rows means editing or deleting
one self-corrects the whole series. Prices that came from a quote provider are
already on the current scale (Yahoo restates OHLC for splits regardless of
``auto_adjust``) and are passed through untouched -- see
:data:`SPLIT_ADJUSTED_SOURCES`. That mixture of scales in one table is exactly
what made a pre-split purchase spike above the downloaded curve.

Auto-quotes: :func:`fetch_quotes` pulls the latest close for a set of symbols and
writes ``price_history`` rows; :func:`fetch_quote_history` backfills a monthly
series over a date range, which is what keeps a net-worth curve from stepping
between whatever dates prices happened to get recorded on. It takes a QuoteSource so the network stays behind
a thin, injectable seam -- production uses :class:`YFinanceQuoteSource` (lazy
import; only touches the network inside ``get_quotes``), tests pass a fake source.
Symbols no Python package covers route to a webSlinger recording via the
documented :class:`WebSlingerQuoteSource` HOOK (not recorded here).
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import re
from fractions import Fraction
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

from mammon import asset_values, instruments, ledger

_CENT = Decimal("1")
_HUNDRED = Decimal("100")

# Action vocabulary (compared case-insensitively). Share-ADDING actions increase
# quantity and add cost from the cash spent; share-REMOVING actions decrease
# quantity and relieve average cost; RTRNCAP reduces basis without moving shares;
# everything else (Div/IntInc/CGLong/...) is a cash event with no holding effect.
_ADD_ACTIONS = {
    "buy", "buyx", "buybond", "buymf", "buyother", "buystock",
    "reinvdiv", "reinvest", "reinvlg", "reinvsh", "reinvint", "reinvmd",
    "shrsin", "xin", "addshares",
}
_REMOVE_ACTIONS = {
    "sell", "sellx", "sellbond", "sellmf", "sellother", "sellstock",
    "shrsout", "xout", "removeshares",
}
_RTRNCAP_ACTIONS = {"rtrncap", "returnofcapital"}
# Short sales: ShtSell OPENS/adds to a short (shares go negative, proceeds credit
# the basis); CvrShrt buys the borrowed shares back (shares rise toward zero,
# relieving the credit). A fully-covered short nets to zero shares and is dropped.
_SHORT_OPEN_ACTIONS = {"shtsell"}
_SHORT_COVER_ACTIONS = {"cvrshrt"}
# Stock splits: Quicken stores the split ratio in the quantity field as the number
# of NEW shares per 10 OLD shares (20 = 2:1 forward split, 2.5 = 1:4 reverse
# split). The running share count is scaled by quantity/10; cost basis is unchanged.
_SPLIT_ACTIONS = {"stksplit"}

# The subset of _REMOVE_ACTIONS that are true DISPOSALS (a sale for cash), as
# opposed to a share TRANSFER out (ShrsOut/XOut/RemoveShares -- basis simply
# leaves the account, no gain is realized). Only a sale books realized P/L
# (net proceeds - the average cost relieved); see :func:`_replay_positions`.
_SALE_ACTIONS = {
    "sell", "sellx", "sellbond", "sellmf", "sellother", "sellstock",
}

# Income DISTRIBUTIONS attributed to a security -- cash dividends (Div/DivX),
# reinvested dividends (ReinvDiv), capital-gains distributions (CGLong/CGShort/
# CGMid and their reinvested twins), and interest (IntInc/ReinvInt). These are
# summed per symbol into the "Dividends" total shown beside a holding; a reinvest
# action ALSO adds shares/cost (see _ADD_ACTIONS) so it counts as both. The
# magnitude used is abs(amount) so it is correct whether Quicken stored the
# distribution signed or gross-positive.
_DIVIDEND_ACTIONS = {
    "div", "divx",
    "reinvdiv",
    "cglong", "cglongx", "cgshort", "cgshortx", "cgmid", "cgmidx",
    "reinvlg", "reinvsh", "reinvmd",
    "intinc", "intincx", "reinvint",
    # A broker download's own words for a cash dividend. Accepted through review
    # as written, they were cash into the account but no one's income: a real
    # ledger held 63 such rows, every 2026 dividend of its ETFs, in no dividend
    # total and no return (migration 72 drops the snapshots that summed without
    # them).
    "dividend", "cashdividend",
}

# Cash effect of an action on the account's CASH balance. Quicken exports a
# BUY/SELL/DIV amount as a positive MAGNITUDE (direction implied by the action),
# but a generic "Cash" entry SIGNED (a +deposit or a -fee, e.g. "Administration
# Fees"). So:
#   * share-transfer / reinvest / cross actions net to zero (a BuyX's transfer-in
#     funds the buy; a ReinvDiv's dividend funds the reinvestment),
#   * cash-OUT actions force -abs (Quicken's positive Buy magnitude is money out),
#   * everything else keeps the STORED sign -- Sell/Div/IntInc/CGLong/CGShort/XIn
#     and ContribX (a retirement/529 contribution transferred in) are positive =
#     cash in, and "Cash" is already signed (so a fee stays negative). This is
#     correct whether the amount was stored signed (tests: Buy = -1000) or
#     gross-positive (the QIF importer: Buy = +1000).
#   * WithdrwX (a withdrawal/transfer OUT of a retirement/529 account) is the
#     cash-OUT twin of XOut: Quicken stores it as a positive magnitude, so it
#     MUST force -abs like XOut. Without it the default branch would add the
#     withdrawal as cash IN -- a 2x-the-withdrawal error (see State 529 accounts).
#   * CvrShrt (cover a short) pays cash to buy back a borrowed position, so it is
#     cash-OUT like a buy; Quicken stores it as a positive magnitude, and its
#     mirror ShtSell keeps the stored positive sign = cash IN via the default
#     branch. Both ALSO move shares (see compute_holdings): ShtSell drives the
#     position negative, CvrShrt raises it back toward zero, so an open short
#     survives as a negative holding and a covered one nets to zero.
_CASH_OUT_ACTIONS = {
    "buy", "buybond", "buymf", "buyother", "buystock",
    "cvrshrt",
    "xout", "withdrwx", "withdraw", "miscexp", "miscexpx", "margint", "commission",
}
_CASH_ZERO_ACTIONS = {
    "buyx", "sellx",
    "reinvdiv", "reinvest", "reinvlg", "reinvsh", "reinvint", "reinvmd",
    "stksplit", "shrsin", "shrsout", "addshares", "removeshares", "stockdividend",
}

# Actions that CONTRIBUTE capital to (or WITHDRAW it from) a specific security
# position, as opposed to income (dividends/interest) or price movement. A buy
# deploys cash into the position; a sell returns it. The X-funded twins (BuyX /
# SellX) do the same with the cash arriving/leaving via a transfer, so they count
# too. Reinvested dividends, stock splits and pure share transfers are NOT here: a
# reinvestment is funded by income already earned (its added value is return, not
# new capital), and a split/transfer moves shares without a cash contribution.
# This vocabulary mirrors Quicken's BUY*/SELL* actions and is kept beside
# _CASH_OUT_ACTIONS so the two stay in step. It is the "net contributions" term of
# a period-bounded gain (see :func:`net_contributions_by_symbol`).
_ACQUIRE_ACTIONS = {
    "buy", "buybond", "buymf", "buyother", "buystock", "buyx",
}
_DISPOSE_ACTIONS = {
    "sell", "sellbond", "sellmf", "sellother", "sellstock", "sellx",
}

# ---------------------------------------------------------------------------
# Option life-cycle actions (SRD 5.8e-7)
# ---------------------------------------------------------------------------
# An option contract has four ways in and out that a share has no analogue for,
# and Part 1.2 of the instrument taxonomy names them: buy/sell TO OPEN (the
# position is created, long or short) and buy/sell TO CLOSE (it is unwound), plus
# the three ENDINGS that are not trades at all -- EXERCISE (the long holder takes
# the deal), ASSIGN (the writer is made to honour it) and EXPIRE (nobody does
# anything and the contract simply stops existing).
#
# These are NEW vocabulary. No pre-existing ledger row carries one of them, which
# is why the branches below can be added without a kind test: the option rules
# reach a row only when that row was WRITTEN with an option action, and the two
# writers that emit them (:func:`record_option_exercise`,
# :func:`record_option_expiration`) both refuse a symbol that is not explicitly
# ``kind='option'``. A NULL-kind row is UNCLASSIFIED and keeps every path it had.
#
# The constants are the display spellings actually stored in
# ``investment_transactions.action``; the sets are keyed by the normalized form
# (lowercased, spaces stripped) that :func:`_action_key` produces.
OPTION_BUY_TO_OPEN = "Buy to Open"
OPTION_SELL_TO_CLOSE = "Sell to Close"
OPTION_SELL_TO_OPEN = "Sell to Open"
OPTION_BUY_TO_CLOSE = "Buy to Close"
OPTION_EXERCISE = "Exercise"
OPTION_ASSIGN = "Assign"
OPTION_EXPIRE = "Expire"
OPTION_EXPIRE_SHORT = "Expire Short"

#: Every option life-cycle action, normalized.
OPTION_ACTIONS = {
    "buytoopen", "selltoclose", "selltoopen", "buytoclose",
    "exercise", "assign", "expire", "expireshort",
}

#: Why an expiry is TWO actions rather than one "Expire" whose direction is read
#: off the position: a share-quantity row has to state its own direction. The
#: reconciliation sums :func:`share_qty_delta` per row to get the contract count
#: (:func:`share_reconcile_rows`), and it has no position to consult -- so a
#: single "Expire" would be counted with the wrong sign for one of the two sides
#: and the reconciliation would silently disagree with the replay. Split in two,
#: each expiry is an ordinary removal or an ordinary cover and EVERY existing
#: path -- quantity, cash, realized P/L, reconciliation -- is already correct.
#: :func:`record_option_expiration` picks the right one from the position, so
#: the choice is never the caller's to get wrong.
_EXPIRE_ACTIONS = {"expire", "expireshort"}

#: A written contract leaving the books against its OWN short lots, so the credit
#: released is lot-exact rather than the position average (see
#: :func:`_relieve_short`). All three are short covers for quantity purposes.
_SHORT_LOT_CLOSE_ACTIONS = {"buytoclose", "assign", "expireshort"}
#: ...and of those, the ones that BOOK realized P/L. An assignment books none:
#: the premium rolls into the share lot instead (Pub 550), which is the whole
#: point of the exercise/assignment pair.
_SHORT_REALIZING_ACTIONS = {"buytoclose", "expireshort"}
#: The two endings whose premium rolls into a share lot instead of being realized.
_BASIS_ROLL_ACTIONS = {"exercise", "assign"}

_ADD_ACTIONS |= {"buytoopen"}
# Exercise is a REMOVAL that is not a SALE: the contract leaves at its own cost
# and books nothing, because that cost is about to reappear inside a share lot.
# A long expiry IS a sale -- of nothing, for nothing: proceeds of zero against
# the basis relieved is exactly the loss of the whole premium, which is why it
# needs no arithmetic of its own.
_REMOVE_ACTIONS |= {"selltoclose", "exercise", "expire"}
_SALE_ACTIONS |= {"selltoclose", "expire"}
_SHORT_OPEN_ACTIONS |= {"selltoopen"}
_SHORT_COVER_ACTIONS |= _SHORT_LOT_CLOSE_ACTIONS
_ACQUIRE_ACTIONS |= {"buytoopen"}
_DISPOSE_ACTIONS |= {"selltoclose", "expire"}
# Cash: paying a premium and paying to close a written contract are money out;
# an expiry of either sign moves no cash at all. Selling to open/close keeps the
# stored sign (positive = the premium received), like every other credit in this
# module, and so do Exercise/Assign -- whose amount is the SIGNED premium being
# rolled, cash in for a long contract's basis and cash out for a written one's
# credit.
_CASH_OUT_ACTIONS |= {"buytoopen", "buytoclose"}
_CASH_ZERO_ACTIONS |= _EXPIRE_ACTIONS

# Plain trades, whose DIRECTION the action states but whose MEANING the position
# decides (:func:`_apply_trade`). Quicken's four verbs do not track the sign of
# the position they act on: a short is covered with ``Buy`` as often as with
# ``CvrShrt``, a long is sold with ``ShtSell``, and an option writer's whole
# history is ``ShtSell``/``CvrShrt`` pairs. Keyed on the action alone, a Buy
# against a short stacked a long lot on top of it, a Sell with nothing held
# booked its entire proceeds as gain, and a cover realized nothing at all -- on
# a migrated option-writing history that put realized P/L several times away
# from the cash the trades actually produced. The option life-cycle verbs
# are deliberately NOT here: they are written only for a classified contract,
# by writers that already know the position's sign (SRD 5.8e-7).
_TRADE_BUY_ACTIONS = {"buy", "buybond", "buymf", "buyother", "buystock", "buyx", "cvrshrt"}
_TRADE_SELL_ACTIONS = {"sell", "sellbond", "sellmf", "sellother", "sellstock", "sellx", "shtsell"}


def _cash_effect(action, amount) -> int:
    a = (action or "").strip().lower().replace(" ", "")
    amt = amount or 0
    if a in _CASH_ZERO_ACTIONS:
        return 0
    if a in _CASH_OUT_ACTIONS:
        return -abs(amt)
    return amt


# ---------------------------------------------------------------------------
# Decimal / money helpers
# ---------------------------------------------------------------------------
def _D(value) -> Decimal:
    """Tolerant Decimal parse; '' / None -> 0."""
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return Decimal(0)


def _cents(value: Decimal) -> int:
    """Round a Decimal amount to integer cents, HALF_UP."""
    return int(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _qty_text(qty: Decimal) -> str:
    """A clean, exponent-free Decimal string for storage ('100' not '1E+2')."""
    if qty == 0:
        return "0"
    return format(qty.normalize(), "f")


# ---------------------------------------------------------------------------
# Recording investment transactions
# ---------------------------------------------------------------------------
def ticker_of(symbol) -> str:
    """The leading token of a security string, upper-cased -- its ticker, or ""
    when the name carries none.

    A security is stored under whatever the source called it, and sources
    disagree: "ALTY", "ALTY GLOBAL X SUPERDIVIDEND ALTER", "QTUM DEFIANCE
    QUANTUM ETF". The first token is the one stable part across every spelling
    observed in a real ledger, and it is what a quote provider understands.

    Returns "" for a name whose first token is not ticker-shaped -- a plan's
    internally-named fund ("TARGET 2030 FUND", "DOMESTIC BOND INDEX") has no
    public ticker, and asking a provider about "TARGET" would fetch a quote for
    an unrelated listed company. Callers use that to skip such holdings rather
    than price them wrongly."""
    head = str(symbol or "").strip().upper().split(" ")[0]
    if not head or not (1 <= len(head) <= 5) or not head.isalpha():
        return ""
    return head


# The body of an OSI option symbol: YYMMDD, C or P, then the strike x 1000 in 8
# zero-padded digits. "XYZ260117C00150000" and the padded fixed-width form
# "XYZ   260117C00150000" both contain it; so does the QIF name that follows the
# symbol with a human rendering. Nothing that is not an option contract looks
# like this, which is the point -- see :func:`looks_like_option`.
_OSI_BODY_RE = re.compile(r"\d{6}[CP]\d{8}")


def looks_like_option(*texts) -> bool:
    """True when any of ``texts`` carries an OSI option-contract symbol.

    DELIBERATELY CONSERVATIVE, and a placeholder. This is a shape test, not a
    classifier: it recognises only the unambiguous OSI body (``260117C00150000``)
    anywhere in the string, so it says "yes" to the 21-character symbol in every
    spelling seen in this ledger -- padded, unpadded, and followed by a human
    rendering -- and "no" to everything else, including pre-2010 OPRA symbols
    (``IBMAF``) and broker prose (``XYZ 01/17/2026 150.00 C``). Those are false
    NEGATIVES on purpose: every caller uses this to REFUSE an action, so a miss
    leaves today's behaviour and a false positive would block a legitimate
    rename.

    It exists because :func:`ticker_of` cannot tell a contract from its
    underlying -- the first token of "XYZ 260117C00150000 XYZ 17JAN26 150 C" is
    "XYZ", so every ticker-derivation path in this codebase quietly proposes
    collapsing the contract onto the stock. An option is a distinct instrument,
    never a spelling of its underlying. A real ``instruments.classify`` (a
    closed Kind enumeration plus a full OSI/legacy/human parser) is the intended
    replacement; when it lands, this function and its call sites go with it."""
    for text in texts:
        s = str(text or "").strip().upper()
        if s and _OSI_BODY_RE.search(s):
            return True
    return False


# ---------------------------------------------------------------------------
# Stock splits: one encoding, converted at every boundary
# ---------------------------------------------------------------------------
# A split is STORED the way Quicken and QIF/OFX store it: new shares per TEN old.
# An 8-for-1 is 80, a 3-for-1 is 30, a 1-for-2 reverse is 5, and _apply_txn
# multiplies the running quantity by q/10. That number is an ENCODING, not a
# share count, and nothing a user reads or types should be in it -- a register
# row saying the split's quantity was "80" tells the holder of 26 shares
# nothing, and a ratio field that stores what it is given turns "8" into a
# 0.8x reverse split. These three functions are the only places the /10 appears
# outside the replay.
def split_ratio(quantity) -> Optional[Decimal]:
    """Stored split quantity -> the ratio it means (new shares per OLD share)."""
    if quantity in (None, ""):
        return None
    try:
        return Decimal(str(quantity)) / 10
    except (InvalidOperation, ValueError):
        return None


_SPLIT_SEP = r"(?::|/|\s*-?\s*for\s*-?\s*)"


def parse_split_ratio(text) -> Optional[Fraction]:
    """A typed split ratio -> new shares per OLD share, as an EXACT Fraction.

    Accepts what a broker announces and what the register displays: "8:1",
    "3:2", "8-for-1", "3 for 2", "1/2", and a bare multiplier ("8", "1.5").
    Returns None when the text is not a ratio at all.

    The register renders a split as "8:1" (:func:`split_ratio_text`), so the
    field that EDITS one has to read "8:1". It took a bare number only, which
    made the displayed value un-typeable and left "3:2" as arithmetic homework.

    A Fraction, not a Decimal, because 1:3 has no exact decimal form and the
    caller has to be able to TELL -- dividing first would silently produce
    0.333... and lose the evidence.
    """
    s = str(text or "").strip()
    if not s:
        return None
    parts = re.split(_SPLIT_SEP, s, maxsplit=1, flags=re.IGNORECASE)
    try:
        if len(parts) == 2:
            num = Fraction(Decimal(parts[0].strip()))
            den = Fraction(Decimal(parts[1].strip()))
            return None if den == 0 else num / den
        return Fraction(Decimal(s))
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return None


def split_stored(ratio) -> Optional[Decimal]:
    """An announced ratio -> the DERIVED legacy ``quantity`` (new per ten old).

    Kept only so a split row still carries the column older code reads. The
    authoritative form is the ``split_num``/``split_den`` pair (migration 34);
    this value is quantized, and for a ratio like 4:3 it is an approximation
    that nothing consults while the pair is present.
    """
    frac = as_split_fraction(ratio)
    if frac is None:
        return None
    per_ten = Decimal(frac.numerator * 10) / Decimal(frac.denominator)
    return per_ten.quantize(Decimal("1E-10")).normalize()


def as_split_fraction(ratio) -> Optional[Fraction]:
    """Coerce a ratio (Fraction, Decimal, str, number) to an exact Fraction."""
    if ratio in (None, ""):
        return None
    if isinstance(ratio, Fraction):
        return ratio
    try:
        return Fraction(Decimal(str(ratio)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _split_pair(t) -> Optional[tuple]:
    """The exact ``(num, den)`` of a split row, or None when it predates
    migration 34 and only carries the legacy per-ten ``quantity``."""
    num = _row_value(t, "split_num")
    den = _row_value(t, "split_den")
    if num in (None, "") or den in (None, "") or int(den) == 0:
        return None
    return int(num), int(den)


def split_factor(t) -> Optional[Fraction]:
    """The exact multiplier a split applies to a share count, from whichever
    form the row carries. None when the row says nothing usable."""
    pair = _split_pair(t)
    if pair is not None:
        return Fraction(pair[0], pair[1])
    q = _row_value(t, "quantity")
    frac = as_split_fraction(q)
    if frac is None or frac == 0:
        return None
    return frac / 10


def apply_split(qty: Decimal, t) -> Decimal:
    """``qty`` rescaled by a split row, EXACTLY.

    Multiplies before dividing -- 300 shares at 4:3 is (300*4)/3 = 400, where
    300 * (4/3) would be 399.9999999999999999999999999. That ordering is the
    whole reason the ratio is stored as a pair.
    """
    factor = split_factor(t)
    if factor is None:
        return qty
    return qty * Decimal(factor.numerator) / Decimal(factor.denominator)


def split_display(t) -> str:
    """A split row's ratio as a broker announces it: "8:1", "4:3", "1:2"."""
    factor = split_factor(t)
    if factor is None:
        return ""
    return "%d:%d" % (factor.numerator, factor.denominator)


def split_ratio_text(quantity) -> str:
    """The ratio as a broker announces it: "8:1", "3:2", "1:2" for a reverse."""
    ratio = split_ratio(quantity)
    if ratio is None:
        return ""
    frac = Fraction(ratio).limit_denominator(1000)
    return "%d:%d" % (frac.numerator, frac.denominator)


def is_known_action(action) -> bool:
    """Whether ``action`` is a canonical Quicken action this module understands
    (any spelling the holdings replay recognises). Raw source activity text --
    "Credit Interest", "RECORDKEEPING FEE" -- is not, which is how callers tell
    an importer's untranslated passthrough from a real action."""
    a = str(action or "").strip().lower().replace(" ", "")
    if not a:
        return False
    known = (_ADD_ACTIONS | _REMOVE_ACTIONS | _RTRNCAP_ACTIONS
             | _SHORT_OPEN_ACTIONS | _SHORT_COVER_ACTIONS | _SPLIT_ACTIONS
             | _SALE_ACTIONS | _DIVIDEND_ACTIONS | OPTION_ACTIONS)
    return a in known or a in {"cash", "miscinc", "miscexp", "xin", "xout",
                               "withdraw", "withdrwx", "margint", "div", "divx",
                               "stockdividend"}


def record_investment(
    conn,
    account_id: int,
    date: str,
    action: str,
    *,
    symbol: Optional[str] = None,
    quantity=None,
    price=None,
    amount: Optional[int] = None,
    commission: Optional[int] = None,
    memo: Optional[str] = None,
    transfer_account_id: Optional[int] = None,
    fitid: Optional[str] = None,
    import_id: Optional[int] = None,
    split_num: Optional[int] = None,
    split_den: Optional[int] = None,
) -> int:
    """Insert one investment transaction. ``amount``/``commission`` are signed
    integer cents (amount negative = cash out, e.g. a Buy); ``quantity``/``price``
    are Decimal text. Does NOT rebuild holdings -- call :func:`rebuild_holdings`
    after a batch. Returns the new row id."""
    cur = conn.execute(
        "INSERT INTO investment_transactions"
        "(account_id, date, action, symbol, quantity, price, amount, commission, memo,"
        " transfer_account_id, import_id, fitid, split_num, split_den)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            account_id, date, action,
            symbol or None,
            _qty_text(_D(quantity)) if quantity not in (None, "") else None,
            _qty_text(_D(price)) if price not in (None, "") else None,
            amount,
            commission,
            memo or None,
            transfer_account_id,
            import_id,
            fitid or None,
            split_num,
            split_den,
        ),
    )
    _invalidate_holdings_checkpoints_from(conn, account_id, date)
    conn.commit()
    return cur.lastrowid


def list_investment_txns(conn, account_id: int) -> list:
    """All investment transactions for an account in application order
    (date, then insertion id so same-day buys settle before sells)."""
    return conn.execute(
        "SELECT * FROM investment_transactions WHERE account_id=? ORDER BY date, id",
        (account_id,),
    ).fetchall()


def get_investment_txn(conn, txn_id: int):
    """One investment transaction by id (a ``sqlite3.Row``), or ``None``."""
    return conn.execute(
        "SELECT * FROM investment_transactions WHERE id=?", (txn_id,)
    ).fetchone()


def update_investment(
    conn,
    txn_id: int,
    date: str,
    action: str,
    *,
    symbol: Optional[str] = None,
    quantity=None,
    price=None,
    amount: Optional[int] = None,
    commission: Optional[int] = None,
    memo: Optional[str] = None,
    transfer_account_id: Optional[int] = None,
    split_num: Optional[int] = None,
    split_den: Optional[int] = None,
) -> None:
    """Replace the editable columns of an existing investment transaction with
    the given values (``None`` clears a column). Same unit conventions as
    :func:`record_investment`: ``amount``/``commission`` are signed integer
    cents, ``quantity``/``price`` are Decimal text. Does NOT rebuild holdings --
    call :func:`rebuild_holdings` afterward (the register widget does)."""
    prior = get_investment_txn(conn, txn_id)
    conn.execute(
        "UPDATE investment_transactions SET"
        " date=?, action=?, symbol=?, quantity=?, price=?, amount=?,"
        " commission=?, memo=?, transfer_account_id=?, split_num=?, split_den=?"
        " WHERE id=?",
        (
            date, action,
            symbol or None,
            _qty_text(_D(quantity)) if quantity not in (None, "") else None,
            _qty_text(_D(price)) if price not in (None, "") else None,
            amount,
            commission,
            memo or None,
            transfer_account_id,
            split_num,
            split_den,
            txn_id,
        ),
    )
    if prior is not None:
        # Invalidate from the EARLIER of the old/new date so a back-dated edit
        # cascades correctly once holdings are rebuilt.
        earliest = min(d for d in (prior["date"], date) if d)
        _invalidate_holdings_checkpoints_from(conn, prior["account_id"], earliest)
    conn.commit()


def update_investment_fields(conn, txn_id: int, **changes) -> bool:
    """Change a SUBSET of an investment transaction's editable columns, keeping
    the rest as they are, then write through :func:`update_investment` (the
    full-row writer). A single-field correction or a batch edit stays on the ONE
    investment write path instead of minting a second UPDATE. Returns False if
    the row is gone. ``changes`` keys are :func:`update_investment`'s parameter
    names (date, action, symbol, quantity, price, amount, commission, memo,
    transfer_account_id, split_num, split_den)."""
    prior = get_investment_txn(conn, txn_id)
    if prior is None:
        return False

    def pick(name):
        return changes[name] if name in changes else prior[name]

    update_investment(
        conn, txn_id, pick("date"), pick("action"),
        symbol=pick("symbol"), quantity=pick("quantity"), price=pick("price"),
        amount=pick("amount"), commission=pick("commission"), memo=pick("memo"),
        transfer_account_id=pick("transfer_account_id"),
        split_num=pick("split_num"), split_den=pick("split_den"))
    return True


def is_void_investment(txn) -> bool:
    """True when the investment row carries Quicken's void mark.

    The cash register stamps ``**VOID**`` on the (visible) payee; an
    ``investment_transactions`` row has no payee, so the mark lives on the memo --
    the only free text such a row carries -- and the register renders it on the
    Action cell (see :class:`mammon.ui.models.InvestmentRegisterModel`)."""
    return str((_row_value(txn, "memo")) or "").startswith(ledger.VOID_PREFIX)


def void_investment(conn, txn_id: int) -> bool:
    """Quicken's Void for an investment transaction: keep the row as a record but
    take it out of the money AND out of the share math.

    The mirror of :func:`ledger.void_transaction`, on the
    ``investment_transactions`` schema (which has no payee to stamp and no split
    lines to drop). ``amount``/``commission``/``quantity``/``price`` all go to
    zero, so :func:`register_rows`'s running cash and share balances -- and
    :func:`rebuild_holdings` -- treat the row as inert, and the memo gains the
    ``**VOID**`` prefix the register shows, with the original amount noted so
    nothing is lost. Idempotent: returns False (changing nothing) when the row is
    already void. Does NOT rebuild holdings -- the caller does, exactly as
    update/delete do. Raises ``KeyError`` if the id is unknown."""
    prior = get_investment_txn(conn, txn_id)
    if prior is None:
        raise KeyError(f"no investment transaction {txn_id}")
    if is_void_investment(prior):
        return False
    was = abs(int(prior["amount"] or 0))
    note = f"voided; was {was // 100:,}.{was % 100:02d}"
    memo = (prior["memo"] or "").strip()
    new_memo = f"{ledger.VOID_PREFIX} {memo}".strip() + f" ({note})"
    conn.execute(
        "UPDATE investment_transactions SET amount=0, commission=0,"
        " quantity=NULL, price=NULL, memo=? WHERE id=?",
        (new_memo, txn_id))
    _invalidate_holdings_checkpoints_from(conn, prior["account_id"], prior["date"])
    conn.commit()
    return True


def symbols_used(conn, account_id: int) -> list:
    """Distinct security symbols already recorded on an account, in first-seen
    order (blank/cash rows dropped). Seeds the new/edit dialog's Security combo;
    there is no securities table -- symbols are free text on the rows."""
    seen: list = []
    for t in list_investment_txns(conn, account_id):
        s = (t["symbol"] or "").strip()
        if s and s not in seen:
            seen.append(s)
    return seen


# ---------------------------------------------------------------------------
# Renaming a security (search / replace, scoped to one account)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SecurityRename:
    """One proposed name change -- ``old`` -> ``new``, over ``txns`` rows.

    ``merges`` marks the case that changes more than a label: ``new`` already
    names a security in this account, so applying the rename FUSES two positions
    into one and their lots, cost basis and dividends replay as a single holding.
    That is usually the whole point -- a broker who started sending "ALTY" for
    the security it spent five years calling "ALTY GLOBAL X SUPERDIVIDEND ALTER"
    -- but renaming back does not separate them again, so a caller shows it
    before applying."""

    old: str
    new: str
    txns: int
    merges: bool


def _replace_ci(text: str, needle: str, repl: str) -> str:
    """Case-insensitive substring replace, then collapse runs of whitespace.

    ``repl`` is inserted literally: the user typed a ticker, not a regex
    template, so a backslash or ``\1`` in it is text. Collapsing whitespace is
    what makes "delete the descriptive tail" work -- removing the middle of a
    name would otherwise leave a double space no one can see but every later
    comparison can."""
    out = re.sub(re.escape(needle), lambda _m: repl, text, flags=re.IGNORECASE)
    return " ".join(out.split())


def plan_security_rename(conn, account_id: int, search: str, replace: str) -> list:
    """The renames a search/replace over this account's security names WOULD make.

    Read-only. It reports; :func:`apply_security_renames` applies.

    Matching is by SUBSTRING, case-insensitively, because the two shapes this has
    to fix look nothing alike. A broker abbreviates by dropping the tail --
    "ALTY GLOBAL X SUPERDIVIDEND ALTER" -> "ALTY", which is find "GLOBAL X
    SUPERDIVIDEND ALTER" and replace with nothing. A fund company renames
    outright -- "Fidelity 500 Index Fund" -> "FXAIX", where the new name shares
    no text at all with the old. Whole-name matching handles only the second,
    prefix logic only the first; substring handles both.

    Scoped to ONE account deliberately. A security name means what that account's
    broker meant by it, and fusing two positions is a judgement about one
    account's history -- a ledger-wide rename would reshape holdings the user was
    not looking at.

    There is deliberately no "shorten every name to its ticker" bulk action. In
    the real trading account this was built against, 310 of 313 distinct names
    have a ticker-shaped first token, because that is how option contracts are
    named; blanket shortening would fuse "AGNC AMERICAN CAPITAL AGENCY CORP" with
    "AGNC 110219P00029000 AGNC 19FEB11 29 P" and its April twin into a single
    position -- a stock and two expired options held as one. Every rename is
    proposed by name and confirmed by name for exactly that reason.

    Raises ValueError if the replacement would leave a security with no name.
    """
    needle = (search or "").strip()
    if not needle:
        return []
    repl = " ".join((replace or "").split())
    low = needle.lower()
    counts = {
        r["symbol"]: r["n"]
        for r in conn.execute(
            "SELECT symbol, COUNT(*) AS n FROM investment_transactions "
            "WHERE account_id=? AND symbol IS NOT NULL AND symbol<>'' "
            "GROUP BY symbol",
            (account_id,),
        )
    }
    existing = set(counts)
    out: list = []
    for sym in sorted(counts):
        if low not in sym.lower():
            continue
        new = _replace_ci(sym, needle, repl)
        if not new:
            raise ValueError(
                "That replacement would leave %r with no name. A security row "
                "has to name something." % sym)
        if new == sym:
            continue
        out.append(SecurityRename(
            old=sym, new=new, txns=counts[sym],
            merges=new in existing or any(r.new == new for r in out)))
    return out


def apply_security_renames(conn, account_id: int, pairs) -> int:
    """Apply ``[(old, new)]`` within one account. Returns the rows renamed.

    Renames the account's investment transactions -- the authoritative rows --
    and any of its review items still naming the old security, so a rename made
    while a review is open does not leave the queue pointing at a name the
    register no longer has.

    ``holdings`` and ``holdings_checkpoints`` are DERIVED and are rebuilt, not
    patched: a merge changes the lot replay itself, and a checkpoint patched in
    place would carry the pre-merge cost basis forward from every year that
    already had one. Unlike :func:`update_investment` this rebuilds rather than
    only invalidating, because a rename has no single date to cascade from -- it
    restates the whole history of the securities it touches.
    """
    renamed = 0
    touched = False
    for old, new in pairs:
        old = (old or "").strip()
        new = " ".join((new or "").split())
        if not old or not new or old == new:
            continue
        cur = conn.execute(
            "UPDATE investment_transactions SET symbol=? "
            "WHERE account_id=? AND symbol=?", (new, account_id, old))
        renamed += cur.rowcount
        conn.execute(
            "UPDATE review_items SET symbol=? WHERE account_id=? AND symbol=?",
            (new, account_id, old))
        _migrate_price_history(conn, old, new)
        touched = True
    if touched:
        conn.commit()
        rebuild_holdings(conn, account_id)
    return renamed


def _migrate_price_history(conn, old: str, new: str) -> None:
    """Carry a renamed security's prices across without stranding another account.

    ``price_history`` is keyed by symbol ALONE -- a price is a property of the
    security, not of an account -- so an account-scoped rename must not simply
    UPDATE it: another account may still hold the security under the old name and
    would lose every price it has.

    Prices are therefore COPIED, filling only the dates the new name lacks: an
    existing row wins, because a quote fetched under the ticker is a better
    number than one derived from a decade-old transaction. The old name's rows
    are dropped only once nothing anywhere refers to it any more.
    """
    conn.execute(
        "INSERT OR IGNORE INTO price_history(symbol, date, close_price, source) "
        "SELECT ?, date, close_price, source FROM price_history WHERE symbol=?",
        (new, old))
    still = conn.execute(
        "SELECT 1 FROM investment_transactions WHERE symbol=? LIMIT 1",
        (old,)).fetchone()
    if still is None:
        still = conn.execute(
            "SELECT 1 FROM review_items WHERE symbol=? LIMIT 1", (old,)).fetchone()
    if still is None:
        conn.execute("DELETE FROM price_history WHERE symbol=?", (old,))


def _row_value(row, key):
    """Read ``key`` from a sqlite3.Row OR a plain dict, tolerating its absence
    (a Row raises IndexError on an unknown column; a dict raises KeyError)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def category_display(conn, txn) -> str:
    """The 'Security / Category' text for an investment row that names no security.

    A Quicken investment row is EITHER a security trade OR a cash line -- never
    both -- and a cash line still points somewhere: a transfer leg (XIn/XOut and
    friends set ``transfer_account_id``) reads as the OTHER account in brackets,
    e.g. ``[Anytown CU Ck]``; any other cash line (Div/IntInc/MiscInc/'Cash')
    reads as its ``memo`` -- the free-text category descriptor Quicken carries on
    such an entry (e.g. 'Administration Fees', 'Interest'), the closest thing this
    schema stores to a category (``investment_transactions`` has no category_id).
    Empty when neither applies. The account lookup goes through
    :func:`ledger.get_account` -- no raw SQL here."""
    taid = _row_value(txn, "transfer_account_id")
    if taid is not None:
        other = ledger.get_account(conn, taid)
        return f"[{other['name']}]" if other is not None else "[Transfer]"
    return (_row_value(txn, "memo") or "").strip()


def register_rows(conn, account_id: int) -> list[dict]:
    """The account's investment transactions in application order, each augmented
    with the Quicken investment-register running balances -- so the register UI
    stays a thin projection and the running-balance math is tested HERE, not in
    the view. Each returned row is a plain dict (the stored columns plus four
    derived keys):

      * ``share_bal`` -- running quantity of THAT ROW's security as clean Decimal
        text, moved by :func:`share_qty_delta` on every share-MOVING action and
        SCALED by a split (:data:`_SPLIT_ACTIONS` -> x q/10, which takes
        precedence because its effect is multiplicative, not additive).
        ``None`` on cash-only or non-share-moving rows
        (Div/IntInc/RtrnCap/XIn/MiscInc/Cash) -- Quicken leaves Share Bal blank
        there.

        Classification is DELEGATED, not repeated here: :func:`is_quantity_action`
        and :func:`share_qty_delta` are the same pair the holdings replay uses, so
        the last share_bal per symbol ties to holdings/valuation. This function
        used to carry its own second copy of the rule (``a in _ADD_ACTIONS or a in
        _REMOVE_ACTIONS``), which silently omitted the two SHORT sets --
        :data:`_SHORT_OPEN_ACTIONS` (ShtSell) and :data:`_SHORT_COVER_ACTIONS`
        (CvrShrt) are separate sets in this module -- so a short leg fell through
        to the cash-only branch and the register rendered its (correctly negative)
        Share Bal as an empty cell.

        The split case was likewise missing once, and it broke the tie to holdings
        rather than just the split's own row: a StkSplit is neither an ADD nor a
        REMOVE, so the running total was left at its pre-split value and EVERY
        later row for that security reported a stale balance. An 8-for-1 on 26
        shares left the column reading 26 forever while holdings said 208.
      * ``inv_amt`` -- ``abs(amount)`` on share-moving rows (the gross security
        amount Quicken shows under 'Inv Amt'); ``None`` otherwise.
      * ``cash_amt`` -- the row's effect on the account CASH (:func:`_cash_effect`):
        cash-neutral actions (ReinvDiv, share transfers) are 0.
      * ``cash_bal`` -- running account cash AFTER the row, opening at the account's
        ``opening_balance`` and accumulating ``cash_amt``.
      * ``category_label`` -- the transfer account (``[Other]``) or category text a
        cash line points at (:func:`category_display`); blank for a security trade
        (which shows its symbol instead). Mirrors the cash register's key of the
        same name so the two register models stay parallel.
    """
    acct = ledger.get_account(conn, account_id)
    cash = 0
    opening = 0
    if acct is not None:
        try:
            opening = acct["opening_balance"] or 0
        except (KeyError, IndexError):
            opening = 0
    # The opening balance joins the running cash on its own date, as it does in
    # ledger.register_rows and ledger.account_balance.
    opened = not opening
    share_bal: dict[str, Decimal] = {}
    out: list[dict] = []
    for t in _register_sequence(conn, account_id):
        if not opened and ledger.opening_balance_on(acct, t["date"]):
            cash += opening
            opened = True
        cash_leg = isinstance(t, dict) and t.get("cash_leg")
        a = (t["action"] or "").strip().lower().replace(" ", "")
        # A backfilled transfer leg lives in the cash ``transactions`` table (see
        # _register_sequence); its stored signed amount IS its cash effect, so it
        # bypasses the action-based _cash_effect classification.
        camt = int(t["amount"] or 0) if cash_leg else _cash_effect(t["action"], t["amount"])
        cash += camt
        sym = t["symbol"]
        sbal = None
        inv_amt = None
        # A security row is one that MOVES shares of a named security -- a bare
        # XIn/ShrsIn cash transfer names no security, so (like Quicken) it gets no
        # Share Bal and no Inv Amt, only a Cash Amt.
        if sym and a in _SPLIT_ACTIONS:
            # Cash-neutral and quantity-neutral: it RESCALES what is held. Same
            # rule as _apply_txn, so the column keeps tying to holdings.
            bal = apply_split(share_bal.get(sym, Decimal(0)), t)
            share_bal[sym] = bal
            sbal = _qty_text(bal)
        elif sym and is_quantity_action(a):
            # ONE classification for the whole module: share_qty_delta already
            # signs ADD/CvrShrt positive and REMOVE/ShtSell negative, so a short
            # leg carries the balance negative instead of falling through to the
            # blank cash-only branch. (_SPLIT_ACTIONS is caught above; its delta
            # is 0 because a split scales rather than adds.)
            bal = share_bal.get(sym, Decimal(0)) + share_qty_delta(t)
            share_bal[sym] = bal
            sbal = _qty_text(bal)
            if t["amount"] is not None:
                inv_amt = abs(t["amount"])
        row = dict(t)
        row["share_bal"] = sbal
        row["inv_amt"] = inv_amt
        row["cash_amt"] = camt
        row["cash_bal"] = cash
        # The category/transfer destination Quicken shows beside a cash line's
        # action (blank for a security trade, which shows its symbol instead).
        row["category_label"] = category_display(conn, t)
        out.append(row)
    return out


def _unrepresented_transfer_legs(conn, account_id: int, inv_rows) -> list[dict]:
    """Transfer legs for this account that live in the cash ``transactions`` table
    but have NO matching leg in ``investment_transactions``.

    ``ledger.backfill_split_transfer_mirrors`` is investment-unaware: it only ever
    writes/adopts ``transactions`` rows for a split's counter-account, even when
    that account is an investment account whose activity otherwise lives in
    ``investment_transactions``. The investment register reads only
    ``investment_transactions``, so without this those backfilled legs are invisible
    here even though the itemize report (which reads ``transactions``) counts them.

    A leg is treated as ALREADY represented -- and skipped -- when an investment
    transfer row shares its ``(date, counter-account, |amount|)``. That suppresses
    the duplicate legs the backfill created for periods an import already recorded
    as an ``XIn``/``XOut`` cash transfer, so each transfer shows exactly once and
    the register still ties to the report total (which sums every ``transactions``
    leg once)."""
    represented: dict = {}
    for t in inv_rows:
        taid = _row_value(t, "transfer_account_id")
        if taid is not None:
            key = (t["date"], int(taid), abs(int(t["amount"] or 0)))
            represented[key] = represented.get(key, 0) + 1
    legs: list[dict] = []
    rows = conn.execute(
        "SELECT id, date, amount, payee, num, memo, transfer_account_id "
        "FROM transactions WHERE account_id=? AND transfer_account_id IS NOT NULL "
        "ORDER BY date, id",
        (account_id,),
    ).fetchall()
    for r in rows:
        key = (r["date"], int(r["transfer_account_id"]), abs(int(r["amount"] or 0)))
        if represented.get(key, 0) > 0:
            represented[key] -= 1        # consume one; a duplicate of an XIn/XOut
            continue
        amt = int(r["amount"] or 0)
        legs.append({
            "id": r["id"],
            "account_id": account_id,
            "date": r["date"],
            # A bare transfer leg names no security; label it by direction so the
            # Action column reads like an imported cash transfer.
            "action": "XIn" if amt >= 0 else "XOut",
            "symbol": None,
            "quantity": None,
            "price": None,
            "amount": amt,
            "commission": None,
            "memo": r["memo"],
            "num": r["num"],
            "payee": r["payee"],
            "transfer_account_id": r["transfer_account_id"],
            # Marks a row sourced from ``transactions``, not ``investment_transactions``
            # -- read-only here (the edit dialogs key off investment_transactions.id).
            "cash_leg": True,
        })
    return legs


def transfer_targets(conn, row) -> list:
    """The other side of an investment register row's transfer, as
    ``(account_id, txn_id)`` pairs -- what the register's 'Go to [account]' menu
    entry needs to navigate. Empty for a row that is not a transfer leg, which is
    how the menu knows to hide the entry; ``txn_id`` may be ``None``, and the jump
    then lands on the target register without selecting a row.

    ``row`` is a register row (see :func:`register_rows`), because the register
    shows two different kinds of transfer leg:

      1. a backfilled CASH leg (``cash_leg``) -- an ordinary ``transactions`` row
         sitting in this account, cross-linked to its mirror by
         ``transfer_pair_id`` exactly like the cash register's legs, so the mirror
         id is simply read back;
      2. an ``XIn``/``XOut`` ``investment_transactions`` row -- which carries
         ``transfer_account_id`` but has NO pair-id column at all, so its
         counterpart is found the only way this module ever matches the two
         tables: same ``(date, counter-account, |amount|)``, the identical key
         :func:`_unrepresented_transfer_legs` and the valuation double-count
         guard use. Requiring the counterpart to point BACK at this account keeps
         a same-day, same-amount stranger from being offered as the mirror.

    Reads only; kept out of :mod:`mammon.ui` so the register stays a thin
    projection with no SQL of its own."""
    if not row:
        return []
    if _row_value(row, "cash_leg"):
        r = conn.execute(
            "SELECT transfer_account_id, transfer_pair_id FROM transactions "
            "WHERE id=?", (int(row["id"]),)).fetchone()
        if r is None or r["transfer_account_id"] is None:
            return []
        pair = r["transfer_pair_id"]
        return [(int(r["transfer_account_id"]),
                 int(pair) if pair is not None else None)]
    acct = _row_value(row, "transfer_account_id")
    own = _row_value(row, "account_id")
    if acct is None or own is None:
        return []
    hit = conn.execute(
        "SELECT id FROM transactions WHERE account_id=? AND date=? "
        "AND ABS(amount)=? AND transfer_account_id=? ORDER BY id LIMIT 1",
        (int(acct), row["date"], abs(int(_row_value(row, "amount") or 0)),
         int(own)),
    ).fetchone()
    return [(int(acct), int(hit[0]) if hit is not None else None)]


def investment_txn_for_cash_leg(conn, txn):
    """The ``investment_transactions.id`` that REPRESENTS a cash register row's
    transfer into this investment account, or ``None`` -- the mirror image of
    :func:`transfer_targets`, walked from the ordinary side.

    The cash register knows its counterpart only as ``transfer_pair_id``, a
    ``transactions`` id: the mirror leg ``ledger.create_transfer`` wrote on the
    investment account. But the investment register shows that leg ONLY when no
    investment row already represents the same movement --
    :func:`_unrepresented_transfer_legs` dedupes it away against the XIn/XOut on
    the identical ``(date, counter-account, |amount|)`` key. When it IS deduped
    away, forwarding the pair id sends ``select_txn`` an id that names no visible
    row, and the jump lands on the register with nothing selected. So the cash
    side has to ask, in that same key, 'is there an investment row standing in
    for my mirror?' and select that instead.

    Returns ``None`` when there is none, which is the signal to keep using the
    pair id -- the backfilled cash leg is then genuinely what the register shows.

    ``txn`` is a cash-side row (sqlite3.Row or dict) with ``account_id``,
    ``date``, ``amount`` and ``transfer_account_id``. Ambiguity (two same-day,
    same-size rows) is resolved toward a matching memo, then the lowest id, so
    the jump is deterministic rather than absent."""
    if not txn:
        return None
    acct = _row_value(txn, "transfer_account_id")
    own = _row_value(txn, "account_id")
    if acct is None or own is None:
        return None
    rows = conn.execute(
        "SELECT id, memo FROM investment_transactions WHERE account_id=? "
        "AND date=? AND ABS(amount)=? AND transfer_account_id=? ORDER BY id",
        (int(acct), _row_value(txn, "date"),
         abs(int(_row_value(txn, "amount") or 0)), int(own)),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        memo = (_row_value(txn, "memo") or "").strip()
        if memo:
            for r in rows:
                if (r["memo"] or "").strip() == memo:
                    return int(r["id"])
    return int(rows[0]["id"])


def _register_sequence(conn, account_id: int) -> list:
    """The account's rows for the register in register order -- the real
    ``investment_transactions`` rows merged with any cash-only transfer legs that
    only exist in ``transactions`` (:func:`_unrepresented_transfer_legs`).

    Ordered by date, then CASH EFFECT high to low, then investment rows before
    backfilled cash legs, then id (SRD 5.1b, the same rule as
    :func:`ledger.register_rows`). A day's rows carry no time, and in id order a
    rebalance's buys could show ahead of the sales that paid for them, or a
    purchase ahead of the transfer that funded it, with the cash balance dipping
    negative in between. Cash in first and the largest spend last keeps the
    running cash as high as it can be at every row. Share-only rows (reinvest,
    share transfers, splits) move no cash and sit between the two. This is the
    DISPLAY order: the holdings replay keeps application order (date, id), so a
    same-day buy and sale of one security are never replayed as a short."""
    inv_rows = list_investment_txns(conn, account_id)
    legs = _unrepresented_transfer_legs(conn, account_id, inv_rows)

    def _key(r):
        is_leg = isinstance(r, dict) and r.get("cash_leg")
        cash = int(r["amount"] or 0) if is_leg else _cash_effect(r["action"], r["amount"])
        return (r["date"] or "", -cash, 1 if is_leg else 0, int(r["id"] or 0))

    return sorted([*inv_rows, *legs], key=_key)


# ---------------------------------------------------------------------------
# Holdings (derived from investment_transactions)
# ---------------------------------------------------------------------------
@dataclass
class _Lot:
    """One tax lot: shares acquired together, and what they cost. ``date`` and
    ``txn_id`` name the acquiring transaction; both are None for shares whose
    lot history is not known (a position restored from a snapshot written
    before lots were kept), which then count as acquired on an unknown date."""
    qty: Decimal = Decimal(0)
    cost: int = 0        # cents
    date: Optional[str] = None
    txn_id: Optional[int] = None


LOT_METHODS = ("average", "fifo", "lifo")


def _add_year(d: _dt.date) -> _dt.date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:                    # Feb 29
        return d.replace(year=d.year + 1, day=28)


@dataclass
class RealizedGain:
    """One sale matched to one lot -- what the tax form asks for: when the
    shares were acquired and sold, what they brought, what they cost."""
    sale_txn_id: Optional[int]
    symbol: str
    acquired: Optional[str]
    sold: str
    quantity: Decimal
    proceeds: int                     # cents, net of commission
    basis: int                        # cents relieved
    #: Set ONLY where the law fixes the term regardless of the dates. The one
    #: case that matters here: gain or loss on a contract the user WROTE is
    #: short-term however long the contract was open (Pub 550, "Options" --
    #: writing is not holding property, so there is no holding period to run).
    #: Without it a LEAPS written two years ago and expiring worthless would
    #: report a long-term gain, which is wrong on the tax form.
    term_override: Optional[str] = None

    @property
    def gain(self) -> int:
        return self.proceeds - self.basis

    @property
    def term(self) -> str:
        """``long`` when held more than one year, ``short`` otherwise,
        ``unknown`` when the acquisition date is not known -- unless
        ``term_override`` fixes it (a written option; see that field)."""
        if self.term_override:
            return self.term_override
        if not self.acquired:
            return "unknown"
        a = _dt.date.fromisoformat(self.acquired)
        s = _dt.date.fromisoformat(self.sold)
        return "long" if s > _add_year(a) else "short"


@dataclass
class _Position:
    """The full per-symbol replay state: the average-cost lot (qty + cost) PLUS
    the running totals the per-security view and the holdings tabs need -- income
    ``dividends`` distributed by the security, ``realized`` gain/loss booked on
    sales, and ``ever_held`` (a share-moving action touched this symbol, so a bare
    dividend-only symbol is not mistaken for a closed position)."""
    qty: Decimal = Decimal(0)
    cost: int = 0            # cents, average-cost basis of the remaining shares
    dividends: int = 0       # cents, income distributions attributed to the symbol
    realized: int = 0        # cents, realized gain/loss from sales (long disposals)
    ever_held: bool = False  # a share-moving action touched this symbol
    lots: list = field(default_factory=list)                  # open _Lot rows, oldest first
    gains: list = field(default_factory=list, compare=False)  # RealizedGain rows booked by THIS replay


def _planned(plan: list, lot) -> Decimal:
    return sum((x for l, x in plan if l is lot), Decimal(0))


def _spread_cost(pos) -> None:
    """Re-spread ``pos.cost`` over the open lots in proportion to their shares
    (average cost after a sale; a return of capital), the last lot absorbing
    the rounding so the lots always sum to the position."""
    total_qty = sum((lot.qty for lot in pos.lots), Decimal(0))
    if not pos.lots or total_qty <= 0:
        return
    allotted = 0
    for i, lot in enumerate(pos.lots):
        lot.cost = (pos.cost - allotted if i == len(pos.lots) - 1
                    else _cents(Decimal(pos.cost) * lot.qty / total_qty))
        allotted += lot.cost


def _relieve(pos, q: Decimal, method: str, assigned) -> list:
    """Take ``q`` shares out of ``pos`` and return what each lot gave up as
    ``[(lot_date, lot_txn_id, qty, cost)]``, lowering ``pos.cost`` by the
    total. WHICH lots give up shares: those ``assigned`` to this sale first
    (Specify Lots), then the method's order -- oldest first, newest first for
    lifo. WHAT they cost: the lot's own cost for fifo/lifo; for average, the
    position's average per share, after which every remaining lot carries that
    average, as average cost means. With no lot history to draw on (a short,
    or shares that predate lot tracking) the aggregate average is relieved
    exactly as the pre-lot replay did."""
    if q <= 0 or pos.qty <= 0:
        return []
    if not pos.lots:
        avg = Decimal(pos.cost) / pos.qty
        taken = min(pos.cost, _cents(avg * q))
        pos.cost -= taken
        return [(None, None, q, taken)]
    method = (method or "average").lower()
    plan: list = []                       # (lot, qty) in the order shares leave
    remaining = q
    if assigned:
        by_id = {lot.txn_id: lot for lot in pos.lots if lot.txn_id is not None}
        for lot_txn_id, aq in assigned:
            lot = by_id.get(int(lot_txn_id))
            if lot is None or remaining <= 0:
                continue
            take = min(_D(aq), lot.qty - _planned(plan, lot), remaining)
            if take > 0:
                plan.append((lot, take))
                remaining -= take
    order = list(reversed(pos.lots)) if method == "lifo" else list(pos.lots)
    for lot in order:
        if remaining <= 0:
            break
        avail = lot.qty - _planned(plan, lot)
        if avail <= 0:
            continue
        take = min(avail, remaining)
        plan.append((lot, take))
        remaining -= take
    out: list = []
    if method == "average":
        avg = Decimal(pos.cost) / pos.qty
        total = min(pos.cost, _cents(avg * q))
        planned_qty = sum((x for _, x in plan), Decimal(0))
        allotted = 0
        for i, (lot, take) in enumerate(plan):
            c = (total - allotted if i == len(plan) - 1
                 else _cents(Decimal(total) * take / planned_qty))
            allotted += c
            out.append((lot.date, lot.txn_id, take, c))
        for lot, take in plan:
            lot.qty -= take
        pos.lots = [lot for lot in pos.lots if lot.qty > 0]
        pos.cost -= total
        _spread_cost(pos)
    else:
        total = 0
        for lot, take in plan:
            c = lot.cost if take >= lot.qty else _cents(Decimal(lot.cost) * take / lot.qty)
            lot.cost -= c
            lot.qty -= take
            total += c
            out.append((lot.date, lot.txn_id, take, c))
        pos.lots = [lot for lot in pos.lots if lot.qty > 0]
        pos.cost -= total
    return out


def _relieve_short(pos, q: Decimal) -> list:
    """The mirror of :func:`_relieve` for a SHORT position: take ``q`` units back
    out of the obligation and return what each short lot gives up as
    ``[(lot_date, lot_txn_id, qty, credit)]``, where ``credit`` is NEGATIVE --
    the money that came IN when the position was opened is carried as a negative
    cost, so releasing it raises ``pos.cost`` back toward zero.

    Why a short needs its own function rather than the position average
    :func:`_apply_txn` used to apply: for an option, short is the NORMAL
    direction (writing calls and puts is the common retail strategy), so the
    premium of the exact contract being closed has to be identifiable. Lots are
    taken oldest-first; there is no ``method`` because lot selection on a short
    is not the user's to make -- what is relieved is the obligation that is
    actually being closed.

    ``pos.qty`` is left to the caller, exactly as :func:`_relieve` does. Falls
    back to the aggregate average when no short lot history exists (a ShtSell
    from before this vocabulary, or a snapshot seeded position), which reproduces
    the pre-existing average-credit behaviour to the cent."""
    if q <= 0 or pos.qty >= 0:
        return []
    q = min(q, -pos.qty)
    short_lots = [lot for lot in pos.lots if lot.qty < 0]
    if not short_lots:
        per = Decimal(pos.cost) / pos.qty          # both negative -> positive
        taken = -_cents(per * q)                   # negative: a credit released
        if abs(taken) > abs(pos.cost):
            taken = pos.cost
        pos.cost -= taken
        return [(None, None, q, taken)]
    out: list = []
    remaining, total = q, 0
    for lot in short_lots:
        if remaining <= 0:
            break
        avail = -lot.qty
        take = min(avail, remaining)
        c = lot.cost if take >= avail else _cents(Decimal(lot.cost) * take / avail)
        lot.cost -= c
        lot.qty += take
        total += c
        remaining -= take
        out.append((lot.date, lot.txn_id, take, c))
    # Only fully-released lots go; a partially-closed short lot stays negative.
    pos.lots = [lot for lot in pos.lots if lot.qty != 0]
    pos.cost -= total
    return out


def _short_gain_rows(t, sym: str, taken: list, cost_paid: int,
                     term: Optional[str] = "short") -> list:
    """One :class:`RealizedGain` per SHORT lot a close drew on. A written
    contract's P/L is the mirror of a long's: the premium received when it was
    written is the proceeds (``-credit``, since a credit is carried negative) and
    what it cost to make the obligation go away is the basis -- zero when it
    simply expired worthless, which is what makes the whole premium the gain.

    ``acquired`` is the date the contract was WRITTEN and ``sold`` the date it
    was closed, so the row reads in calendar order; ``term`` is pinned short by
    default because writing an option starts no holding period (see
    :attr:`RealizedGain.term_override`)."""
    if not taken:
        return []
    total_qty = sum((x for _, _, x, _ in taken), Decimal(0))
    out, allotted = [], 0
    for i, (date, txn_id, take, credit) in enumerate(taken):
        c = (cost_paid - allotted if i == len(taken) - 1
             else _cents(Decimal(cost_paid) * take / total_qty))
        allotted += c
        out.append(RealizedGain(_row_value(t, "id"), sym, date, t["date"],
                                take, -credit, c, term_override=term))
    return out


def _gain_rows(t, sym: str, q: Decimal, proceeds: int, taken: list) -> list:
    """One :class:`RealizedGain` per lot a sale drew on, the sale's net
    proceeds shared out by shares (the last lot absorbs the rounding)."""
    if not taken:
        return []
    total_qty = sum((x for _, _, x, _ in taken), Decimal(0))
    out, allotted = [], 0
    for i, (date, txn_id, take, cost) in enumerate(taken):
        p = (proceeds - allotted if i == len(taken) - 1
             else _cents(Decimal(proceeds) * take / total_qty))
        allotted += p
        out.append(RealizedGain(_row_value(t, "id"), sym, date, t["date"], take, p, cost))
    return out


def _apply_trade(pos, t, sym: str, a: str, q: Decimal, method: str, assignments) -> None:
    """Fold one plain trade into ``pos`` by what it does to the POSITION, not by
    what its action is called (see :data:`_TRADE_BUY_ACTIONS`).

    A buying trade first COVERS any short -- releasing the credit the short was
    opened for and realizing that credit less the share of the cash spent on
    covering -- and only what is left over opens or adds to a long lot. A
    selling trade first SELLS any long, exactly as a sale always has (lot
    method, specified lots, one gain row per lot), and only what is left over
    opens or adds to a short. When a trade crosses zero, its cash is shared out
    by shares between the side it closes and the side it opens.

    Every trade that stays on one side is the previous arithmetic unchanged: a
    long-only history replays to the same cost, lots and gains to the cent.

    A covered short is SHORT-term whatever its dates. For a written option that
    is the law (Pub 550: writing starts no holding period); for an equity short
    sale it is the normal case, and an exception needs facts this row does not
    carry."""
    pos.ever_held = True
    txn_id = _row_value(t, "id")
    if a in _TRADE_BUY_ACTIONS:
        total = _cost_of(t, q)
        cover = min(q, -pos.qty) if pos.qty < 0 else Decimal(0)
        cover_cost = total if cover == q else _cents(Decimal(total) * cover / q)
        if cover > 0:
            taken = _relieve_short(pos, cover)
            pos.qty += cover
            rows = _short_gain_rows(t, sym, taken, cover_cost)
            pos.realized += sum(g.gain for g in rows)
            pos.gains.extend(rows)
            if pos.qty == 0:
                pos.cost = 0
                pos.lots = []
        rest = q - cover
        if rest > 0:
            cost = total - cover_cost
            pos.qty += rest
            pos.cost += cost
            pos.lots.append(_Lot(rest, cost, t["date"], txn_id))
        return

    total = _proceeds_of(t, q)
    close = min(q, pos.qty) if pos.qty > 0 else Decimal(0)
    close_proceeds = total if close == q else _cents(Decimal(total) * close / q)
    if close > 0:
        cost_before = pos.cost
        taken = _relieve(pos, close, method, (assignments or {}).get(txn_id))
        pos.qty -= close
        if pos.qty == 0:
            pos.cost = 0
            pos.lots = []
        pos.realized += close_proceeds - (cost_before - pos.cost)
        pos.gains.extend(_gain_rows(t, sym, close, close_proceeds, taken))
    rest = q - close
    if rest > 0:
        pos.qty -= rest
        pos.cost -= total - close_proceeds


def _apply_txn(positions: dict, t, method: str = "average", assignments=None) -> None:
    """Fold one investment transaction into the running per-symbol
    :class:`_Position` map. Extracted so both the from-inception replay and the
    snapshot+delta replay share ONE branch table, which is what keeps the
    filtered security views tied to the Holdings window.

    Shares are kept as tax LOTS, one per acquiring transaction. A disposal
    takes shares out of the lots per ``method`` -- ``average`` (every share
    carries the position's average cost; lots give up shares oldest first,
    which is how the holding period is fixed under average cost), ``fifo`` or
    ``lifo`` (each lot's own cost) -- or first from the lots the user named
    for that sale (``assignments``: sale txn id -> [(lot txn id, qty)]). Every
    sale books one :class:`RealizedGain` per lot it drew on."""
    sym = t["symbol"]
    if not sym:
        return                            # cash-only income row (no security)
    a = (t["action"] or "").strip().lower().replace(" ", "")
    pos = positions.setdefault(sym, _Position())
    q = _D(t["quantity"])
    if a in _DIVIDEND_ACTIONS:
        # Income distributed BY the security (also adds shares if it is a
        # reinvest action, handled by the _ADD_ACTIONS branch below).
        pos.dividends += abs(t["amount"] or 0)
    if q > 0 and (a in _TRADE_BUY_ACTIONS or a in _TRADE_SELL_ACTIONS):
        # A plain trade is read against the position's sign. A row with no
        # positive quantity (an amount-only or oddly signed import) keeps the
        # branches below exactly as they always were.
        _apply_trade(pos, t, sym, a, q, method, assignments)
        return
    if a in _ADD_ACTIONS:
        cost = _cost_of(t, q)
        pos.qty += q
        pos.cost += cost
        if q > 0:
            pos.lots.append(_Lot(q, cost, t["date"], _row_value(t, "id")))
        pos.ever_held = True
    elif a in _REMOVE_ACTIONS:
        cost_before = pos.cost
        taken = _relieve(pos, q, method,
                         (assignments or {}).get(_row_value(t, "id")))
        pos.qty -= q
        if pos.qty == 0:
            pos.cost = 0
            pos.lots = []
        if a in _SALE_ACTIONS:
            # Realized gain = net proceeds - the cost actually relieved
            # (cost_before - cost_after). A share TRANSFER out
            # (ShrsOut/XOut/RemoveShares) is not a sale and books nothing.
            # "Expired worthless" is taken literally -- proceeds are zero by
            # definition, never whatever price column the row happens to carry,
            # so the loss is always exactly the premium paid.
            proceeds = 0 if a in _EXPIRE_ACTIONS else _proceeds_of(t, q)
            pos.realized += proceeds - (cost_before - pos.cost)
            pos.gains.extend(_gain_rows(t, sym, q, proceeds, taken))
        pos.ever_held = True
    elif a in _SHORT_OPEN_ACTIONS:
        # Open/add to a short: shares go negative; proceeds credit the basis
        # (a negative cost, the mirror of a Buy's positive basis).
        credit = _cost_of(t, q)
        pos.qty -= q
        pos.cost -= credit
        if a in OPTION_ACTIONS and q > 0:
            # A WRITTEN option gets a lot of its own -- negative quantity,
            # negative cost -- so that the close, assignment or expiry that
            # eventually relieves it can name the premium it was written for and
            # the date it was written on. Short is the normal direction for an
            # option, so "the position average" is not good enough here. ShtSell
            # is deliberately excluded: an equity short keeps the lot-less
            # average behaviour it has always had. A negative lot is inert to the
            # rest of the module -- both _spread_cost and _relieve return early
            # while the position quantity is not positive.
            pos.lots.append(_Lot(-q, -credit, t["date"], _row_value(t, "id")))
        pos.ever_held = True
    elif a in _SHORT_COVER_ACTIONS:
        if a in _SHORT_LOT_CLOSE_ACTIONS:
            # A WRITTEN option leaving the books: Buy to Close (bought back),
            # Expire Short (nobody used it) or Assign (the writer was made to
            # honour it). All three release the exact credit of the contracts
            # being closed. The first two REALIZE it -- the premium received
            # less what it cost to end the obligation, which for an expiry is
            # nothing at all, making the whole premium the gain. An assignment
            # books none: its premium is not income, it rolls into the share lot
            # the assignment creates or disposes of (Pub 550; see
            # :func:`record_option_exercise`).
            taken = _relieve_short(pos, q)
            pos.qty += q
            if pos.qty == 0:
                pos.cost = 0
                pos.lots = []
            if a in _SHORT_REALIZING_ACTIONS:
                paid = 0 if a in _EXPIRE_ACTIONS else _cost_of(t, q)
                rows = _short_gain_rows(t, sym, taken, paid)
                pos.realized += sum(g.gain for g in rows)
                pos.gains.extend(rows)
        else:
            # Reached only by a CvrShrt with no positive quantity: every real
            # cover goes through _apply_trade, which realizes the short. This is
            # the old quantity-only fallback for a malformed row.
            if pos.qty < 0:
                avg_per_share = Decimal(pos.cost) / abs(pos.qty)
                pos.cost -= _cents(avg_per_share * q)
            pos.qty += q
            if pos.qty == 0:
                pos.cost = 0
        pos.ever_held = True
    elif a in _SPLIT_ACTIONS:
        # Rescale the running share count by the split's exact ratio; a reverse
        # split shrinks it. Total cost basis is unchanged. Every lot scales
        # too, keeping its date -- a split is not an acquisition.
        pos.qty = apply_split(pos.qty, t)
        for lot in pos.lots:
            lot.qty = apply_split(lot.qty, t)
    elif a in _RTRNCAP_ACTIONS:
        pos.cost = max(0, pos.cost - abs(t["amount"] or 0))
        _spread_cost(pos)
    # else: Div/IntInc/CGLong/... -> no share/cost change here (a Div's income
    # was already added to pos.dividends above).


def _merge_position(dst, src) -> None:
    """Fold ``src`` into ``dst`` (both :class:`_Position`) when two ticker
    spellings resolve to one identity. Shares, cost basis and income are
    additive; the open lots concatenate (re-sorted oldest-first) so average cost
    over the pooled lots is the combined basis over the combined shares."""
    dst.qty += src.qty
    dst.cost += src.cost
    dst.dividends += src.dividends
    dst.realized += src.realized
    dst.ever_held = dst.ever_held or src.ever_held
    dst.lots.extend(src.lots)
    dst.lots.sort(key=lambda l: (l.date or "", l.txn_id or 0))
    dst.gains.extend(src.gains)


def _fold_aliases(conn, positions: dict) -> dict:
    """Re-key a per-symbol position map onto canonical identities, merging the two
    spellings of a renamed security into ONE :class:`_Position`. This is where a
    ticker rename becomes a single continuous holding: the old ticker's lots and
    the new ticker's lots pool under the canonical symbol, so the Holdings window
    and every valuation see one identity across the rename date. A no-op (the same
    object) when no aliases exist -- the overwhelmingly common case -- so the hot
    replay path pays nothing.

    An option contract is NEVER folded (SRD 5.8e-2a). :func:`add_alias` refuses to
    record such an alias in the first place; this raises if one exists anyway,
    because merging a contract's position into its underlying's is unrecoverable
    at read time -- the contract count and the share count become one number and
    nothing downstream can tell them apart again. Loud beats wrong here: a
    ValueError names the two symbols, and the user removes the alias."""
    alias_map = {a: c for a, c in list_aliases(conn)}
    if not alias_map:
        return positions

    def canon(sym):
        seen = {sym}
        while sym in alias_map and alias_map[sym] not in seen:
            sym = alias_map[sym]
            seen.add(sym)
        return sym

    folded: dict = {}
    for sym, pos in positions.items():
        key = canon(sym)
        # Only a symbol that actually MOVES is checked, so an unaliased ledger --
        # and every NULL-kind row, which is UNCLASSIFIED and never an option --
        # pays nothing and behaves exactly as before.
        if key != sym and (_stored_kind(conn, sym) == instruments.Kind.OPTION.value
                           or _stored_kind(conn, key) == instruments.Kind.OPTION.value):
            raise ValueError(
                f"refusing to fold {sym!r} into {key!r}: one of them is an "
                "option contract, and an option is a distinct instrument from "
                "its underlying, never another spelling of it -- remove the "
                "security_aliases row")
        if key in folded:
            _merge_position(folded[key], pos)
        else:
            folded[key] = pos
    return folded


def _replay_positions(conn, account_id: int, as_of: Optional[str] = None,
                      use_snapshots: bool = True) -> dict:
    """Replay the account's investment transactions into per-symbol
    :class:`_Position` records (average cost) as of ``as_of`` (default: all
    transactions). This is the ONE engine behind both :func:`compute_holdings`
    (which projects out just qty+cost) and the per-security / holdings-tab views
    (:func:`security_positions`), so a filtered security view and the Holdings
    window can NEVER disagree -- they read the same replay. Pure read; does not
    touch the holdings table.

    When ``use_snapshots`` is set (and year-end snapshots exist), the position is
    seeded from the prior year's ``holdings_checkpoints`` row and only the
    remaining current-year transactions are replayed -- current value = previous
    year's snapshot + current-year delta. The result is IDENTICAL to a
    from-inception replay (verified in the tests); pass ``use_snapshots=False``
    for that inception oracle."""
    positions: dict[str, _Position]
    lower: Optional[str] = None
    if use_snapshots:
        boundary_year = _boundary_year(conn, account_id, as_of)
        positions, snap_year = _load_holdings_checkpoint(conn, account_id, boundary_year)
        if snap_year is not None:
            lower = f"{snap_year}-12-31"     # replay strictly AFTER this snapshot
    else:
        positions = {}
    method, assigned = _replay_context(conn, account_id)
    for t in _list_txns_in_range(conn, account_id, lower, as_of):
        _apply_txn(positions, t, method, assigned)
    # Fold aliased tickers onto their canonical identity AFTER the raw replay, so
    # the snapshot+delta path and the from-inception oracle fold an identical raw
    # dict (checkpoints stay stored per raw ticker; continuity is read-time).
    return _fold_aliases(conn, positions)


def _replay_context(conn, account_id: int) -> tuple:
    """What a replay of this account needs besides its transactions: the
    account's lot method and the lots the user named for particular sales."""
    return get_lot_method(conn, account_id), lot_assignments(conn, account_id)


def get_lot_method(conn, account_id: int) -> str:
    """How this account costs a disposal: ``average`` (the default, and what
    every figure was computed under before lots were kept), ``fifo`` or
    ``lifo``. Brokerages report stock sales FIFO unless lots were specified;
    mutual funds are commonly averaged."""
    row = conn.execute("SELECT lot_method FROM accounts WHERE id=?",
                       (account_id,)).fetchone()
    m = (row["lot_method"] if row is not None else None) or "average"
    return m if m in LOT_METHODS else "average"


def set_lot_method(conn, account_id: int, method: str) -> None:
    """Change how the account costs disposals and rebuild what depends on it:
    the cost basis of every open position, every realized gain, and every
    year-end snapshot -- all derived, all replayed."""
    method = (method or "average").strip().lower()
    if method not in LOT_METHODS:
        raise ValueError(f"unknown lot method {method!r}; one of {LOT_METHODS}")
    conn.execute("UPDATE accounts SET lot_method=? WHERE id=?", (method, account_id))
    conn.commit()
    rebuild_holdings(conn, account_id)


def lot_assignments(conn, account_id: int) -> dict:
    """The lots named for this account's sales: ``{sale_txn_id: [(lot_txn_id,
    quantity_text), ...]}``."""
    out: dict = {}
    for r in conn.execute(
            "SELECT la.sale_txn_id, la.lot_txn_id, la.quantity FROM lot_assignments la "
            "JOIN investment_transactions t ON t.id = la.sale_txn_id "
            "WHERE t.account_id=? ORDER BY la.id", (account_id,)):
        out.setdefault(int(r["sale_txn_id"]), []).append((int(r["lot_txn_id"]), r["quantity"]))
    return out


def lot_assignments_for(conn, sale_txn_id: int) -> list:
    return [(int(r["lot_txn_id"]), r["quantity"]) for r in conn.execute(
        "SELECT lot_txn_id, quantity FROM lot_assignments WHERE sale_txn_id=? ORDER BY id",
        (sale_txn_id,))]


def assign_lots(conn, sale_txn_id: int, assignments) -> None:
    """Specify Lots: name which purchase lots a sale disposes of, as
    ``[(lot_txn_id, quantity), ...]`` (an empty list clears the choice and the
    account's method decides again). Each lot must be a share-adding
    transaction of the same account and security dated on or before the
    sale, and the named shares may not exceed the sale's. The account's
    snapshots from the sale's year on are dropped so the next read replays
    the choice; nothing here rebuilds holdings (the register does, as after
    any investment edit)."""
    sale = get_investment_txn(conn, sale_txn_id)
    if sale is None:
        raise KeyError(f"no investment transaction {sale_txn_id}")
    if (sale["action"] or "").strip().lower().replace(" ", "") not in _REMOVE_ACTIONS:
        raise ValueError("lots can only be assigned to a sale or share removal")
    total = Decimal(0)
    cleaned = []
    for lot_txn_id, qty in assignments or []:
        lot = get_investment_txn(conn, int(lot_txn_id))
        if lot is None or lot["account_id"] != sale["account_id"]                 or (lot["symbol"] or "") != (sale["symbol"] or "")                 or (lot["action"] or "").strip().lower().replace(" ", "") not in _ADD_ACTIONS                 or lot["date"] > sale["date"]:
            raise ValueError(f"transaction {lot_txn_id} is not a purchase lot of this security")
        q = _D(qty)
        if q <= 0:
            raise ValueError("a lot assignment needs a positive quantity")
        total += q
        cleaned.append((int(lot_txn_id), _qty_text(q)))
    if total > _D(sale["quantity"]):
        raise ValueError("more shares assigned than the sale disposes of")
    conn.execute("DELETE FROM lot_assignments WHERE sale_txn_id=?", (sale_txn_id,))
    for lot_txn_id, q in cleaned:
        conn.execute("INSERT INTO lot_assignments(sale_txn_id, lot_txn_id, quantity) "
                     "VALUES (?,?,?)", (sale_txn_id, lot_txn_id, q))
    _invalidate_holdings_checkpoints_from(conn, sale["account_id"], sale["date"])
    conn.commit()


def _boundary_year(conn, account_id: int, as_of: Optional[str]) -> int:
    """The year whose transactions form the 'current-year delta'. For an explicit
    ``as_of`` that is its year; with no ``as_of`` it is the latest year the
    account has activity, so the last completed year's snapshot seeds it."""
    if as_of is not None:
        return int(as_of[:4])
    row = conn.execute(
        "SELECT MAX(substr(date,1,4)) FROM investment_transactions WHERE account_id=?",
        (account_id,),
    ).fetchone()
    return int(row[0]) if row and row[0] else 0


def _list_txns_in_range(conn, account_id: int,
                        after: Optional[str], through: Optional[str]) -> list:
    """Investment transactions in application order (date, id) with dates in
    ``(after, through]`` -- either bound may be ``None`` for open-ended."""
    sql = "SELECT * FROM investment_transactions WHERE account_id=?"
    params: list = [account_id]
    if after is not None:
        sql += " AND date>?"
        params.append(after)
    if through is not None:
        sql += " AND date<=?"
        params.append(through)
    sql += " ORDER BY date, id"
    return conn.execute(sql, tuple(params)).fetchall()


def _load_holdings_checkpoint(conn, account_id: int, before_year: int):
    """Seed positions from the newest year-end snapshot strictly before
    ``before_year``. Returns ``(positions, snapshot_year)``; ``snapshot_year`` is
    ``None`` (and positions empty) when no earlier snapshot exists, so the caller
    falls back to a from-inception replay."""
    yr = conn.execute(
        "SELECT MAX(year) FROM holdings_checkpoints WHERE account_id=? AND year<?",
        (account_id, before_year),
    ).fetchone()
    snap_year = yr[0] if yr else None
    if snap_year is None:
        return {}, None
    positions: dict[str, _Position] = {}
    for r in conn.execute(
        "SELECT symbol, quantity, cost_basis, dividends, realized, ever_held, lots "
        "FROM holdings_checkpoints WHERE account_id=? AND year=?",
        (account_id, snap_year),
    ).fetchall():
        qty = _D(r["quantity"])
        raw = _row_value(r, "lots")
        if raw:
            lots = [_Lot(_D(l[1]), int(l[2]), l[0], l[3]) for l in json.loads(raw)]
        else:
            # A snapshot without lots (none should exist after migration 37,
            # which drops them): the shares are one lot of unknown date.
            lots = [_Lot(qty, r["cost_basis"] or 0, None, None)] if qty > 0 else []
        positions[r["symbol"]] = _Position(
            qty=qty,
            cost=r["cost_basis"],
            dividends=r["dividends"],
            realized=r["realized"],
            ever_held=bool(r["ever_held"]),
            lots=lots,
        )
    return positions, snap_year


def compute_holdings(conn, account_id: int, as_of: Optional[str] = None) -> dict:
    """Replay the account's investment transactions into per-symbol
    (quantity, cost_basis) using AVERAGE cost. Pure read -- returns
    ``{symbol: _Lot}`` without touching the holdings table. A thin projection of
    :func:`_replay_positions` (qty + cost only)."""
    return {
        sym: _Lot(pos.qty, pos.cost)
        for sym, pos in _replay_positions(conn, account_id, as_of).items()
    }


def _states_no_cash(t) -> bool:
    """True for a buy or sell trade whose amount is stated as exactly zero.

    The QIF specification: "If an item is omitted from the transaction in the
    QIF file, Quicken treats it as a blank item." A trade exported with a price
    and a quantity but no ``T`` moved no cash in Quicken's books -- Quicken writes
    an option's expiry or assignment removal that way ("Buy 20 @ 100", no total)
    -- and the cash balance already reads it as zero. Pricing it from quantity x
    price instead charged $2,000 that never left the account. A blank amount
    (NULL, as manual entry leaves it) still derives from quantity x price, and a
    share transfer (ShrsIn/ShrsOut) still takes its basis from its price, which is
    the only basis it states."""
    if t["amount"] is None or t["amount"] != 0:
        return False
    a = (t["action"] or "").strip().lower().replace(" ", "")
    return a in _TRADE_BUY_ACTIONS or a in _TRADE_SELL_ACTIONS


def _cost_of(t, qty: Decimal) -> int:
    """Cash cost (cents) a share-adding transaction adds to basis: the actual
    cash out if the row carries an amount, else price*qty plus commission (never
    for a trade whose amount is stated as zero; see :func:`_states_no_cash`)."""
    amount = t["amount"]
    if amount not in (None, 0):
        return abs(amount)
    if _states_no_cash(t):
        return 0
    base = _cents(qty * _D(t["price"]) * _HUNDRED) if t["price"] else 0
    return base + abs(t["commission"] or 0)


def _proceeds_of(t, qty: Decimal) -> int:
    """Net cash (cents) a share-removing SALE brings in: the row's amount when it
    carries one, else price*qty LESS commission -- the exact mirror of
    :func:`_cost_of`, so realized P/L (proceeds - relieved basis) nets the
    commission paid on both the buy and the sell exactly once.

    A trade's ``amount`` is the cash that actually moved, commission included,
    in both directions: that is what Quicken's ``T`` and an OFX ``<TOTAL>``
    state, what :func:`_cash_effect` posts to the cash balance, and what a
    migrated ledger holds for all but a handful of penny-rounded commissioned
    trades. This used to subtract the
    commission from the amount as well, which charged it twice: every closed
    position read short by its sale commissions, and a position's realized P/L
    no longer tied to the cash it produced."""
    amount = t["amount"]
    if amount not in (None, 0):
        return abs(amount)
    if _states_no_cash(t):
        return 0
    gross = _cents(qty * _D(t["price"]) * _HUNDRED) if t["price"] else 0
    return gross - abs(t["commission"] or 0)


def rebuild_holdings(conn, account_id: int) -> list[dict]:
    """Recompute the account's holdings from its investment transactions and
    replace the holdings rows (dropping any fully-closed position). Returns the
    resulting holdings as dicts."""
    # Refresh the year-end snapshots FIRST (from-inception, snapshot-independent)
    # so the compute_holdings replay below reads fresh checkpoints and its
    # snapshot+delta result equals the from-inception one.
    rebuild_holdings_checkpoints(conn, account_id)
    lots = compute_holdings(conn, account_id)
    conn.execute("DELETE FROM holdings WHERE account_id=?", (account_id,))
    out: list[dict] = []
    for sym, lot in sorted(lots.items()):
        if lot.qty == 0:
            continue
        # `holdings` is DERIVED and replaced wholesale, so the description has to
        # be re-read from the security registry every time or it survives exactly
        # until the next rebuild -- and a rebuild happens on every import, rename
        # and lot-method change. `securities.name` is the one place it lives;
        # this column is a denormalised copy for display.
        conn.execute(
            "INSERT INTO holdings(account_id, symbol, name, quantity, cost_basis) "
            "VALUES (?,?,(SELECT name FROM securities WHERE symbol=?),?,?)",
            (account_id, sym, sym, _qty_text(lot.qty), lot.cost),
        )
        out.append({"symbol": sym, "quantity": _qty_text(lot.qty), "cost_basis": lot.cost})
    conn.commit()
    return out


# ---------------------------------------------------------------------------
# Year-end holdings snapshots (performance twin of ledger.balance_checkpoints)
# ---------------------------------------------------------------------------
def _txn_years(conn, account_id: int) -> list[int]:
    """Ascending list of years in which the account has investment activity."""
    rows = conn.execute(
        "SELECT DISTINCT substr(date,1,4) AS yr FROM investment_transactions "
        "WHERE account_id=? ORDER BY yr",
        (account_id,),
    ).fetchall()
    return [int(r["yr"]) for r in rows]


def _write_snapshot(conn, account_id: int, year: int, positions: dict) -> None:
    """Persist the per-symbol replay state as the year-end snapshot for ``year``
    (replacing any existing rows for that year)."""
    conn.execute(
        "DELETE FROM holdings_checkpoints WHERE account_id=? AND year=?",
        (account_id, year),
    )
    for sym, pos in positions.items():
        lots = json.dumps([[lot.date, _qty_text(lot.qty), lot.cost, lot.txn_id]
                           for lot in pos.lots])
        conn.execute(
            "INSERT INTO holdings_checkpoints"
            "(account_id, year, symbol, quantity, cost_basis, dividends, realized,"
            " ever_held, lots) VALUES (?,?,?,?,?,?,?,?,?)",
            (account_id, year, sym, _qty_text(pos.qty), pos.cost,
             pos.dividends, pos.realized, int(pos.ever_held), lots),
        )


def rebuild_holdings_checkpoints(conn, account_id: int) -> None:
    """Recompute every year-end holdings snapshot for the account from inception.
    A snapshot for year Y is the full replay state through Dec 31 of Y."""
    conn.execute("DELETE FROM holdings_checkpoints WHERE account_id=?", (account_id,))
    _replay_snapshots_from(conn, account_id, _txn_years(conn, account_id), {})
    conn.commit()


def recompute_holdings_checkpoints_from_year(conn, account_id: int, from_year: int) -> None:
    """Cascade a change dated in ``from_year``: drop snapshots for that year and
    all later ones, reseed from the ``from_year - 1`` snapshot, and replay each
    affected year forward. Earlier snapshots are untouched."""
    conn.execute(
        "DELETE FROM holdings_checkpoints WHERE account_id=? AND year>=?",
        (account_id, from_year),
    )
    seed, _ = _load_holdings_checkpoint(conn, account_id, from_year)
    years = [y for y in _txn_years(conn, account_id) if y >= from_year]
    _replay_snapshots_from(conn, account_id, years, seed)
    conn.commit()


def _replay_snapshots_from(conn, account_id: int, years: list[int], seed: dict) -> None:
    """Replay ``years`` forward starting from ``seed`` (a per-symbol
    :class:`_Position` map), writing the running state as each year's snapshot."""
    positions = seed
    method, assigned = _replay_context(conn, account_id)
    for year in years:
        lower = f"{year - 1}-12-31"
        for t in _list_txns_in_range(conn, account_id, lower, f"{year}-12-31"):
            _apply_txn(positions, t, method, assigned)
        _write_snapshot(conn, account_id, year, positions)


def _invalidate_holdings_checkpoints_from(conn, account_id: int, date: str) -> None:
    """Drop snapshots for the year of ``date`` and later after a raw investment
    write, so reads fall back to a correct from-inception replay until the next
    :func:`rebuild_holdings` rebuilds them."""
    if not date:
        return
    conn.execute(
        "DELETE FROM holdings_checkpoints WHERE account_id=? AND year>=?",
        (account_id, int(date[:4])),
    )


def list_holdings(conn, account_id: int) -> list:
    return conn.execute(
        "SELECT * FROM holdings WHERE account_id=? ORDER BY symbol", (account_id,)
    ).fetchall()


def get_holding(conn, account_id: int, symbol: str):
    # Resolve first so a lookup by a renamed (aliased) ticker finds the holding,
    # which rebuild_holdings now keys under the canonical symbol.
    return conn.execute(
        "SELECT * FROM holdings WHERE account_id=? AND symbol=?",
        (account_id, resolve_symbol(conn, symbol)),
    ).fetchone()


# ---------------------------------------------------------------------------
# Security aliases (ticker renames)
# ---------------------------------------------------------------------------
# A ticker rename must not split one security's history into two identities. The
# alias table records that an old ticker IS a former spelling of a surviving
# (canonical) symbol; every symbol lookup on the price/holdings/valuation paths
# routes through resolve_symbol, and price reads union an alias's rows into the
# canonical identity (_identity_symbols). Nothing rewrites a historical row -- the
# continuity is entirely a read-time resolution, which is what lets both spellings
# keep resolving to one holding across the rename date. This module is the SOLE
# writer of security_aliases (add_alias / remove_alias).
def list_aliases(conn) -> list:
    """Every ``(alias_symbol, canonical_symbol)`` pair, in alias order."""
    return [(r["alias_symbol"], r["canonical_symbol"])
            for r in conn.execute(
                "SELECT alias_symbol, canonical_symbol FROM security_aliases "
                "ORDER BY alias_symbol")]


def resolve_symbol(conn, symbol):
    """The canonical symbol ``symbol`` belongs to: follow its alias if one is
    recorded, else ``symbol`` itself (identity; also for a falsy/blank symbol).
    add_alias forbids self-aliases and cycles, so a single hop is all that ever
    exists, but this follows a short bounded chain defensively so a stray chain
    can never spin."""
    if not symbol:
        return symbol
    seen = {symbol}
    cur = symbol
    for _ in range(16):
        row = conn.execute(
            "SELECT canonical_symbol FROM security_aliases WHERE alias_symbol=?",
            (cur,)).fetchone()
        if row is None:
            return cur
        cur = row["canonical_symbol"]
        if cur in seen:                       # defensive; add_alias prevents this
            return cur
        seen.add(cur)
    return cur


def _identity_symbols(conn, symbol) -> list:
    """Every price_history spelling that shares ``symbol``'s canonical identity:
    the canonical symbol itself PLUS every alias that resolves to it. A price read
    over this SET is what keeps a renamed ticker's series continuous across the
    rename date -- the old ticker's price rows attribute to the new identity with
    nothing rewritten."""
    canon = resolve_symbol(conn, symbol)
    if not canon:
        return [symbol]
    aliases = [r["alias_symbol"] for r in conn.execute(
        "SELECT alias_symbol FROM security_aliases WHERE canonical_symbol=?",
        (canon,))]
    return [canon, *aliases]


def _stored_kind(conn, symbol) -> Optional[str]:
    """The ``securities.kind`` stored under EXACTLY this spelling, lowercased, or
    ``None`` for "no row" and for "row exists, kind is NULL" alike -- both mean
    UNCLASSIFIED (SRD 5.8e-2). No alias resolution: this answers "what is THIS
    name", which is what the identity partition below has to ask before it can
    resolve anything."""
    name = str(symbol or "").strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT kind FROM securities WHERE symbol=?", (name,)).fetchone()
    kind = ((row[0] if row is not None else None) or "").strip().lower()
    return kind or None


def _kind_identity_symbols(conn, symbol) -> list:
    """:func:`_identity_symbols` narrowed to ONE INSTRUMENT (SRD 5.8e-2d).

    An alias set means "two ticker spellings of one continuous identity", and
    :func:`add_alias` refuses to put an option contract in one. This is the
    braces to that belt: a set recorded before the guard landed, or written by
    hand, can still fuse a contract onto its underlying, and a union across
    them would value the contract at the stock's price and sum its contract
    count into the stock's share count. So the set is partitioned: a known
    option resolves only over option-kind spellings, and anything NOT known to
    be an option drops the option-kind spellings.

    Bit-for-bit today's list whenever no spelling in the set is an EXPLICIT
    ``kind='option'`` row -- which is every identity in an unclassified ledger,
    because NULL kind is UNCLASSIFIED and never equity. A single-element
    identity (the overwhelmingly common case) never even looks a kind up."""
    syms = _identity_symbols(conn, symbol)
    if len(syms) < 2:
        return syms
    option = instruments.Kind.OPTION.value
    kinds = {s: _stored_kind(conn, s) for s in syms}
    if not any(k == option for k in kinds.values()):
        return syms                       # today's behaviour, untouched
    want_option = _stored_kind(conn, symbol) == option
    kept = [s for s in syms if (kinds.get(s) == option) == want_option]
    # Never widen back to the fused set when the partition empties: the asked-for
    # spelling alone is the honest answer, not "everything".
    return kept or [str(symbol or "").strip()]


def _kind_canon(conn, symbol):
    """:func:`resolve_symbol` narrowed to ONE INSTRUMENT (SRD 5.8e-2d).

    The canonical spelling when it is the same instrument as ``symbol``, else
    ``symbol`` itself. Identical to ``resolve_symbol`` for every identity that
    :func:`_kind_identity_symbols` leaves whole -- which is every identity in an
    unclassified ledger -- so this is a no-op there.

    It exists because canonicalising FIRST is how a hand-written alias row
    fusing a contract onto its underlying would still win: ask to reconcile the
    contract, resolve to the stock, and reconcile the stock's shares under the
    contract's name. The partition has to be applied to the resolution itself,
    not only to the set it is resolved over."""
    name = str(symbol or "").strip()
    canon = resolve_symbol(conn, name)
    return canon if canon in _kind_identity_symbols(conn, name) else (name or canon)


def _price_identity_clause(conn, symbol):
    """``(where_fragment, params)`` selecting every price_history row that shares
    ``symbol``'s canonical identity -- ``symbol IN (?,...)``.

    Unchanged for renames, which is the whole point of it. It just never unions
    across instruments of different kinds (:func:`_kind_identity_symbols`): an
    option's premium series and its underlying's price series are two different
    series, and reading one for the other is a silent mis-valuation."""
    syms = _kind_identity_symbols(conn, symbol)
    return "symbol IN (%s)" % ",".join("?" for _ in syms), list(syms)


def add_alias(conn, alias_symbol, canonical_symbol) -> None:
    """Record that ``alias_symbol`` is a former ticker for ``canonical_symbol``.
    Thereafter every price/holdings/valuation lookup for the alias resolves to the
    canonical security, so the two spellings value as one continuous identity.

    Four guards, each guarding against a corrupt mapping rather than a typo:
    a security cannot alias itself; the canonical MUST be an existing security
    (the identity everything folds onto); the new alias must not close a cycle
    (the canonical must not already resolve back to the alias), which would make
    resolution ambiguous; and NEITHER SIDE may be an option contract.

    That last one is what this table means. ``security_aliases`` says "two ticker
    spellings of one continuous identity" -- a rename, and only a rename. An
    option contract is a different instrument from its underlying: different
    terms, different price series, different lifetime, and it expires while the
    stock goes on. Folding one onto the other would value the contract at the
    stock's price and merge two unrelated holdings, and no read-time resolution
    could tell them apart again. Refused on both sides, because the mistake is
    as easy to make in either direction."""
    alias = (alias_symbol or "").strip()
    canon = (canonical_symbol or "").strip()
    if not alias or not canon:
        raise ValueError("both alias and canonical symbols are required")
    if alias == canon:
        raise ValueError(f"a security cannot alias itself: {alias!r}")
    if looks_like_option(alias, canon):
        raise ValueError(
            f"refusing alias {alias!r} -> {canon!r}: one side is an option "
            "contract, and an option is never another spelling of its "
            "underlying -- security_aliases records renames only")
    if conn.execute("SELECT 1 FROM securities WHERE symbol=?", (canon,)).fetchone() is None:
        raise ValueError(f"canonical symbol {canon!r} is not a known security")
    if resolve_symbol(conn, canon) == alias:
        raise ValueError(
            f"alias {alias!r} -> {canon!r} would form a cycle")
    conn.execute(
        "INSERT INTO security_aliases(alias_symbol, canonical_symbol) VALUES (?,?) "
        "ON CONFLICT(alias_symbol) DO UPDATE SET canonical_symbol=excluded.canonical_symbol",
        (alias, canon))
    conn.commit()


def remove_alias(conn, alias_symbol) -> None:
    """Drop an alias so its symbol resolves to itself again (the rename is
    reversed for lookup purposes; no historical row was ever touched)."""
    conn.execute("DELETE FROM security_aliases WHERE alias_symbol=?",
                 ((alias_symbol or "").strip(),))
    conn.commit()


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
def record_price(conn, symbol: str, date: str, close_price, source: Optional[str] = None) -> None:
    """Upsert one price_history row (Decimal-text close). Unique on (symbol, date)."""
    conn.execute(
        "INSERT INTO price_history(symbol, date, close_price, source) VALUES (?,?,?,?) "
        "ON CONFLICT(symbol, date) DO UPDATE SET close_price=excluded.close_price, "
        "source=excluded.source",
        (symbol, date, _qty_text(_D(close_price)), source),
    )
    conn.commit()


def record_prices(conn, rows) -> int:
    """Bulk-upsert many ``(symbol, date, close_price, source)`` price rows in ONE
    transaction. A full Quicken price history is tens of thousands of quotes, so
    committing per row (record_price) would take minutes; this commits once.
    Returns the number of rows written."""
    payload = [
        (sym, date, _qty_text(_D(close)), source)
        for (sym, date, close, source) in rows
    ]
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO price_history(symbol, date, close_price, source) VALUES (?,?,?,?) "
        "ON CONFLICT(symbol, date) DO UPDATE SET close_price=excluded.close_price, "
        "source=excluded.source",
        payload,
    )
    conn.commit()
    return len(payload)


#: Option-leg actions that say outright that a contract was exercised or
#: assigned: the ones :func:`record_option_exercise` writes.
_DELIVERY_OPTION_ACTIONS = {OPTION_EXERCISE.lower(), OPTION_ASSIGN.lower()}


def option_delivery_leg_ids(conn, rows) -> set:
    """The ids among ``rows`` that are the SHARE side of an option exercise or
    assignment: rows whose price comes from the contract, not the market.

    A share leg changes hands at the strike, or at strike +/- premium when the
    premium is rolled into its basis (SRD 5.8e-7). Neither is what a share traded
    for that day, so neither may become price history. Quicken makes exactly this
    mistake: its price list records the strike as the day's close on assignment
    days, so a real ledger held closes tens of percent away from where the stock
    traded. :func:`learn_prices_from_transactions` would write the same figures
    from the share legs themselves on any day that had no price.

    Two shapes, and both require the contract to be EXPLICITLY classified
    ``kind='option'`` with its underlying recorded, so no option rule reaches an
    unclassified security (SRD 5.8e-2):

    * **Mammon's own pair** (:func:`record_option_exercise`): an ``Exercise`` or
      ``Assign`` row, same account and date, on a contract whose underlying is
      this row's security, for the same number of shares.
    * **The broker's and Quicken's pair**: this row states the STRIKE as its
      price, and the same account holds, that date, a row closing a contract on
      this underlying at that strike, for the same number of shares, with no
      price and no amount. That is how an OFX ``<CLOSUREOPT>`` and a Quicken
      export both write the contract side.

    Shares match either one-for-one or as contracts x multiplier: Quicken
    records an option's quantity in shares, Mammon's writer in contracts.

    Deliberately narrow. A same-day stock trade that happens to be at a strike
    but has no matching unpriced option close keeps its price, so an ordinary
    trade is never mistaken for a delivery.
    """
    days: dict = {}
    for r in rows:
        if r["symbol"] and r["date"]:
            days.setdefault((r["account_id"], r["date"]), []).append(r)
    if not days:
        return set()

    cols = "id, account_id, date, action, symbol, quantity, price, amount"
    if len(days) == 1:
        ((aid, date),) = days
        found = conn.execute(
            f"SELECT {cols} FROM investment_transactions "
            "WHERE account_id=? AND date=?", (aid, date)).fetchall()
    else:
        accounts = sorted({aid for aid, _ in days})
        marks = ",".join("?" * len(accounts))
        found = conn.execute(
            f"SELECT {cols} FROM investment_transactions "
            f"WHERE account_id IN ({marks})", accounts).fetchall()

    terms_by_symbol: dict = {}

    def terms_of(symbol):
        key = str(symbol or "").strip()
        if key not in terms_by_symbol:
            terms_by_symbol[key] = option_terms(conn, key) if key else None
        return terms_by_symbol[key]

    names_by_symbol: dict = {}

    def names_of(symbol):
        """Every spelling a share row's security answers to: its alias identity
        plus each spelling's recorded ticker. A QIF import stores a stock under
        its NAME ("ACME INC") while an option records its underlying as the
        TICKER ("ACME"), and ``securities.ticker`` is what joins the two."""
        key = str(symbol or "").strip()
        if key not in names_by_symbol:
            spellings = [str(s).strip() for s in _identity_symbols(conn, key) if s]
            names = {s.upper() for s in spellings}
            for s in spellings:
                row = conn.execute("SELECT ticker FROM securities WHERE symbol=?",
                                   (s,)).fetchone()
                if row is not None and (row[0] or "").strip():
                    names.add(row[0].strip().upper())
            names_by_symbol[key] = names
        return names_by_symbol[key]

    legs: dict = {}
    for o in found:
        key = (o["account_id"], o["date"])
        if key not in days:
            continue
        terms = terms_of(o["symbol"])
        if terms and (terms.get("underlying") or "").strip():
            legs.setdefault(key, []).append((o, terms))

    out: set = set()
    for key, option_legs in legs.items():
        for r in days[key]:
            if terms_of(r["symbol"]) is not None:
                continue                          # an option row, not a share leg
            identity = names_of(r["symbol"])
            shares = abs(_D(r["quantity"]))
            for o, terms in option_legs:
                if terms["underlying"].strip().upper() not in identity:
                    continue
                contracts = abs(_D(o["quantity"]))
                mult = terms.get("multiplier")
                if shares != contracts and not (mult and shares == contracts * mult):
                    continue
                action = str(o["action"] or "").replace(" ", "").lower()
                if action in _DELIVERY_OPTION_ACTIONS:
                    out.add(r["id"])
                    break
                unpriced = _D(o["price"]) == 0 and _D(o["amount"]) == 0
                strike = terms.get("strike")
                if (unpriced and strike is not None and _D(r["price"]) != 0
                        and _D(r["price"]) == strike):
                    out.add(r["id"])
                    break
    return out


def drop_delivery_strike_closes(conn, account_ids, sources=("qif",)) -> int:
    """Delete recorded closes that are a delivery leg's STRIKE on its own day.

    Quicken's price list records the strike as the close on the day an option is
    exercised or assigned, so an imported history carries closes tens of percent
    away from the market on exactly those days. For each share leg in
    ``account_ids`` that :func:`option_delivery_leg_ids` recognizes, a price row
    from one of ``sources`` for that security and date whose close EQUALS the leg's
    price is removed. Run after the investment rows land, so it holds whichever
    order a yearly set of files arrives in. A genuine close that happens to equal
    the strike that day is lost too; the neighbouring closes stand in for it.
    Returns rows deleted."""
    ids = sorted({int(a) for a in account_ids or () if a is not None})
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT id, account_id, symbol, date, price, amount, commission, quantity "
        f"FROM investment_transactions WHERE account_id IN ({marks})", ids).fetchall()
    legs = option_delivery_leg_ids(conn, rows)
    if not legs:
        return 0
    src_marks = ",".join("?" * len(sources))
    deleted = 0
    for r in rows:
        if r["id"] not in legs or not r["price"]:
            continue
        for ph in conn.execute(
                "SELECT id, close_price FROM price_history WHERE symbol=? AND date=? "
                f"AND source IN ({src_marks})", (r["symbol"], r["date"], *sources)).fetchall():
            if _D(ph["close_price"]) == _D(r["price"]):
                deleted += conn.execute("DELETE FROM price_history WHERE id=?",
                                        (ph["id"],)).rowcount
    return deleted


def learn_prices_from_transactions(conn, account_id=None, *, txn_id=None) -> int:
    """Record each investment transaction's own per-share price into price_history.

    A Buy/Sell/ReinvDiv/ShrsOut row states what one share was worth on its date --
    the QIF ``I`` field -- which is real price history the broker already handed
    us. Capturing it means a 401(k) whose fund prices are quoted nowhere public
    still values correctly, and it costs nothing: the data is already in the row.

    Except the share side of an option exercise or assignment: its price is the
    strike (or strike +/- premium), not a trade at market, so it is skipped
    (:func:`option_delivery_leg_ids`).

    Written with DO-NOTHING precedence and source ``txn`` so an explicit quote
    (a !Type:Prices block, or a fetched quote) always wins for the same
    (symbol, date) -- a transaction price is a single trade, not a close.

    When a row states no price but DOES state a gross amount and a share count,
    the price is derived as ``(|amount| - |commission|) / quantity``. That is not
    a guess -- it is the same number the price column would have held -- and it
    is the only quote that exists for the case that needs it most: a plan's
    quarterly fee removal from a fund quoted nowhere public, where the shares and
    the dollar value are recorded and the per-share price never was.

    Scope it with ``account_id`` (one account) or ``txn_id`` (one row, the accept
    path); omitting both walks every investment account. Returns rows written.
    """
    cols = "id, account_id, symbol, date, price, amount, commission, quantity"
    if txn_id is not None:
        rows = conn.execute(
            "SELECT %s FROM investment_transactions WHERE id=?" % cols,
            (txn_id,)).fetchall()
    elif account_id is not None:
        rows = conn.execute(
            "SELECT %s FROM investment_transactions WHERE account_id=?" % cols,
            (account_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT %s FROM investment_transactions" % cols).fetchall()
    delivery = option_delivery_leg_ids(conn, rows)
    priced = []
    for r in rows:
        sym = str(r["symbol"] or "").strip()
        if not sym or not r["date"] or r["id"] in delivery:
            continue
        price = r["price"]
        bounds = None
        if not price:
            # DERIVED from value / shares: keep it (a noisy price beats the
            # six-year flat line a dormant plan fund otherwise holds) and record
            # how well it is known alongside it.
            price = _price_from_value(r["amount"], r["commission"], r["quantity"])
            bounds = price_bounds(r["amount"], r["commission"], r["quantity"])
        if price:
            lo, hi = bounds if bounds else (None, None)
            priced.append((sym, r["date"], price, "txn", lo, hi))
    return record_prices_if_absent(conn, priced) if priced else 0


def _half_step(text) -> Decimal:
    """Half the granularity of a recorded decimal, from the text as stored.

    "0.007" was rounded to the millishare, so the true value is within 0.0005 of
    it; "1500.439" likewise; a bare "12" within 0.5. Read from the STRING rather
    than assumed, because sources differ in how many places they report and
    assuming three would understate a coarse source and overstate a precise one.
    """
    s = str(text or "").strip()
    if "." not in s:
        return Decimal("0.5")
    places = len(s.split(".", 1)[1].rstrip())
    return Decimal(1).scaleb(-places) / 2


def price_bounds(amount, commission, quantity) -> Optional[tuple]:
    """``(low, high)`` bracketing the per-share price a row implies, or None.

    The derived price is ``value / quantity`` and BOTH inputs are rounded: the
    value to the cent (integer cents in this schema) and the share count to
    whatever precision the source reported. The interval is therefore asymmetric
    -- the smallest price pairs the smallest value with the largest share count,
    and vice versa:

        low  = (value - dv) / (quantity + dq)
        high = (value + dv) / (quantity - dq)

    The interval is always finite -- the smallest representable share count at
    n places is twice its own half-step -- but it can be enormous, and that is
    the point. The real rows that led here, both on one day for one fund:

        0.007 shares / $1.01  ->  144.29, interval [134.0, 156.2]   +/-  8%
        0.001 shares / $0.24  ->  240.00, interval [156.7, 490.0]   +/- 70%

    Neither is wrong; the second is nearly worthless, and only the interval says
    so. A caller wanting the tighter of two same-day estimates compares widths.
    """
    try:
        qty = abs(_D(quantity))
        gross = Decimal(abs(int(amount or 0))) / _HUNDRED
    except (InvalidOperation, ValueError, TypeError):
        return None
    if qty == 0 or gross == 0:
        return None
    fee = Decimal(abs(int(commission or 0))) / _HUNDRED
    value = gross - fee
    if value <= 0:
        return None
    # Money is stored as integer cents, so a rounded cent is the granularity;
    # a commission subtracted from it was rounded too.
    dv = Decimal("0.005") * (2 if fee else 1)
    dq = _half_step(quantity)
    low = (value - dv) / (qty + dq)
    high = (value + dv) / (qty - dq)
    return (_qty_text(low.quantize(Decimal("1E-6"))),
            _qty_text(high.quantize(Decimal("1E-6"))))


def _price_from_value(amount, commission, quantity) -> Optional[str]:
    """Per-share price implied by a row's gross value and share count, or None.

    Commission is taken OUT before dividing, matching the basis convention
    (record_investment: basis = price*qty + commission), so a commissioned trade
    does not report a price a share never traded at.
    """
    try:
        # abs on BOTH: a share REMOVAL (ShrsOut, a fee paid in units) carries a
        # negative quantity, and dividing a positive value by it returned a
        # NEGATIVE price -- which is not a price at all. It never reached
        # price_history only because the one live caller that hits negative
        # quantities computed the quotient itself; the next caller would not
        # have been so lucky. Matches price_bounds, which always took abs.
        qty = abs(_D(quantity))
        gross = Decimal(abs(int(amount or 0))) / _HUNDRED
    except (InvalidOperation, ValueError, TypeError):
        return None
    if qty == 0 or gross == 0:
        return None
    fee = Decimal(abs(int(commission or 0))) / _HUNDRED
    net = gross - fee
    if net <= 0:
        return None
    return _qty_text((net / qty).quantize(Decimal("1E-6")))


# Price sources that a re-fetch is allowed to REPLACE. A row from a provider is
# the provider's current opinion and nothing more: when it was fetched on the
# wrong scale (see YFinanceQuoteSource.get_history's auto_adjust note), the only
# route back is to fetch it again. A transaction-carried price is not an opinion
# -- it is what was actually paid that day -- so it is never displaced.
REFETCHABLE_SOURCES = ("yfinance", "quote", "webslinger")


def record_prices_if_absent(conn, rows, replace_sources=()) -> int:
    """Bulk-insert ``(symbol, date, close, source)`` price rows, keeping any
    existing row that came from a transaction. Returns the number of rows
    actually written.

    Used for transaction-carried prices (the QIF ``I`` field on a
    Buy/Sell/ReinvDiv), so an authoritative ``!Type:Prices`` quote always takes
    precedence over a price merely implied by a transaction on the same day.
    Zero/blank closes are skipped -- a $0 price is not a valid quote.

    A row already carrying one of ``replace_sources`` IS overwritten; the
    default replaces nothing, so every existing caller keeps the behaviour its
    own docstring promises. :func:`fetch_quote_history` opts in with
    :data:`REFETCHABLE_SOURCES`, because ``DO NOTHING`` for every conflict made
    a downloaded price permanently uncorrectable: a whole history fetched on the
    wrong scale survived every attempt to re-download it, and the only visible
    symptom was that re-running the fetch changed nothing.

    The count is what was WRITTEN, not what was offered. Returning
    ``len(payload)`` meant a re-run that inserted nothing still reported filling
    in hundreds of prices, which is how the uncorrectable-row bug stayed hidden.
    """
    payload = []
    for row in rows:
        # Rows are (symbol, date, close, source) or, for a DERIVED price, that
        # plus (low, high) bounds -- see price_bounds. Both shapes accepted so
        # every existing caller is unchanged.
        sym, date, close, source = row[:4]
        low, high = (row[4], row[5]) if len(row) >= 6 else (None, None)
        if not sym or close is None:
            continue
        d = _D(close)
        if d == 0:
            continue
        payload.append((sym, date, _qty_text(d), source, low, high))
    if not payload:
        return 0
    before = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    if replace_sources:
        marks = ",".join("?" * len(replace_sources))
        sql = ("INSERT INTO price_history"
               "(symbol, date, close_price, source, price_low, price_high) "
               "VALUES (?,?,?,?,?,?) ON CONFLICT(symbol, date) DO UPDATE SET "
               "close_price=excluded.close_price, source=excluded.source, "
               "price_low=excluded.price_low, price_high=excluded.price_high "
               f"WHERE price_history.source IN ({marks})")
        payload = [row + tuple(replace_sources) for row in payload]
    else:
        sql = ("INSERT INTO price_history"
               "(symbol, date, close_price, source, price_low, price_high) "
               "VALUES (?,?,?,?,?,?) ON CONFLICT(symbol, date) DO NOTHING")
    cur = conn.executemany(sql, payload)
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    # executemany's rowcount counts updates too where the driver reports it;
    # fall back to the row delta when it does not.
    written = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else (after - before)
    return max(written, 0)


# ---------------------------------------------------------------------------
# Broker position snapshots (OFX <INVPOSLIST>/<INVPOS>)
# ---------------------------------------------------------------------------
def record_position_prices(conn, positions) -> int:
    """Land each broker-reported ``UNITPRICE`` in ``price_history`` keyed to the
    position's as-of date, so valuation (latest price on/before any as-of) can use
    it. ``positions`` is an iterable of ``(symbol, date, units, unitprice)``.
    Never overwrites an existing (symbol, date) quote -- a conflicting existing
    price is surfaced by :func:`reconcile_positions`, not silently replaced.
    Returns the number of price rows submitted."""
    rows = [
        (sym, date, price, "ofx-pos")
        for (sym, date, _units, price) in positions
        if sym and date and price
    ]
    return record_prices_if_absent(conn, rows)


def reconcile_positions(conn, account_id: int, positions) -> list[dict]:
    """Compare broker-reported ``<INVPOS>`` positions against holdings COMPUTED
    from this account's transactions, without mutating either. ``positions`` is an
    iterable of ``(symbol, date, units, unitprice)``. Returns a discrepancy dict
    per mismatch -- ``field='quantity'`` when the reported share count differs from
    the replayed holding as of that date, ``field='price'`` when a price already on
    file for that exact (symbol, date) differs from the reported ``UNITPRICE`` (so
    an authoritative quote is surfaced rather than silently overwritten). The
    computed holdings remain authoritative; callers surface these for review."""
    out: list[dict] = []
    for (sym, date, units, price) in positions:
        if not sym:
            continue
        # compute_holdings keys by the canonical symbol, so resolve the broker's
        # (possibly renamed) ticker before comparing share counts or prices.
        canon = resolve_symbol(conn, sym)
        lots = compute_holdings(conn, account_id, as_of=date)
        computed_qty = lots[canon].qty if canon in lots else Decimal(0)
        reported_qty = _D(units) if units not in (None, "") else Decimal(0)
        if computed_qty != reported_qty:
            out.append({
                "symbol": sym, "date": date, "field": "quantity",
                "computed": _qty_text(computed_qty),
                "reported": _qty_text(reported_qty),
            })
        if price not in (None, ""):
            reported_price = _D(price)
            clause, params = _price_identity_clause(conn, sym)
            row = conn.execute(
                "SELECT close_price FROM price_history WHERE " + clause + " AND date=?",
                (*params, date),
            ).fetchone()
            if row is not None and _D(row["close_price"]) != reported_price:
                out.append({
                    "symbol": sym, "date": date, "field": "price",
                    "computed": _qty_text(_D(row["close_price"])),
                    "reported": _qty_text(reported_price),
                })
    return out


# ---------------------------------------------------------------------------
# Split-adjusting a price series at READ time
# ---------------------------------------------------------------------------
# Price rows are stored RAW, on the scale that was current when they were
# recorded, and the split adjustment is recomputed on every read from the
# ``StkSplit`` rows themselves. Rewriting the stored prices when a split lands
# would be a one-way door: correcting a mistyped ratio, or deleting a split
# entered by accident, could not put the old numbers back, and a second pass
# over the same history would divide twice. Read-time adjustment self-corrects
# instead -- edit or delete the split row and the series follows.
_PRICE_EXP = Decimal("1E-6")      # the precision _price_from_value stores at

# Sources whose HISTORY arrives ALREADY back-adjusted for splits: everything a
# quote provider hands back is expressed on the scale trading at fetch time
# (see :class:`YFinanceQuoteSource.get_history`). These rows must NOT be scaled
# again -- doing so would put a downloaded pre-split close an extra factor below
# the series instead of on it. Same set as :data:`REFETCHABLE_SOURCES`, for the
# same reason: a provider row is the provider's CURRENT opinion, in today's
# units. Everything else ("txn", "qif-txn", a broker position, a hand-typed
# price) is AS TRADED on its own date and is what needs adjusting.
SPLIT_ADJUSTED_SOURCES = frozenset(REFETCHABLE_SOURCES)


def _split_events(conn, symbol) -> list:
    """``(date, factor)`` for every stock split on ``symbol``'s identity, ascending.

    ``factor`` is the EXACT :class:`~fractions.Fraction` a split multiplies a
    share count by (:func:`split_factor`) -- 8 for an 8:1 forward split. Read
    over the alias identity (:func:`_share_identity_clause`) so a renamed
    security still finds the splits recorded under its old ticker.

    Deduplicated BY DATE, and deliberately not scoped to an account: the same
    corporate action is recorded once per account that held the security, so
    summing the rows would tell a two-account holder the 8:1 split was 64:1.
    Voided rows are out of the share math (:func:`void_investment`) and so are
    out of the price math.
    """
    # Split rows first, through the partial index migration 71 keeps on exactly
    # this expression, then the identity match in Python. Filtering by identity
    # in SQL scanned every investment row, and this runs on every past-date
    # price read (latest_price): it tripled the time to draw net worth history.
    # The WHERE text must stay identical to the index's for SQLite to use it.
    _, params = _share_identity_clause(conn, symbol)
    identity = set(params)
    rows = conn.execute(
        "SELECT symbol, date, action, quantity, split_num, split_den, memo"
        " FROM investment_transactions"
        " WHERE lower(replace(action,' ',''))='stksplit' ORDER BY date, id",
    )
    seen: dict = {}
    for r in rows:
        if str(r["symbol"] or "").strip().upper() not in identity:
            continue
        if _action_key(r["action"]) not in _SPLIT_ACTIONS:
            continue
        if is_void_investment(r):
            continue
        factor = split_factor(r)
        if not factor or factor <= 0 or factor == 1:
            continue
        seen.setdefault(r["date"], factor)
    return sorted(seen.items())


def _cumulative_split(events, date: str) -> Fraction:
    """The product of every split factor dated strictly AFTER ``date``.

    Strict, so a price stamped ON the split date is already on the new scale and
    is left alone. Multiple splits compound exactly (2:1 then 3:1 -> 6)."""
    factor = Fraction(1)
    for when, f in events:
        if when > date:
            factor *= f
    return factor


def _rescale_price(price: Decimal, factor: Fraction) -> Decimal:
    """``price`` divided by an exact split ``factor``, HALF_UP at the stored
    precision. Fraction arithmetic throughout -- a float divide by 3 would put
    drift into a price the chart then plots as a step."""
    if factor == 1 or price == 0:
        return price
    exact = Fraction(price) / factor
    places = -_PRICE_EXP.as_tuple().exponent
    scale = 10 ** places
    num, den = exact.numerator * scale, exact.denominator
    # HALF_UP on the exact integer quotient (no Decimal context rounding).
    whole, rem = divmod(abs(num), den)
    if rem * 2 >= den:
        whole += 1
    out = Decimal(whole if num >= 0 else -whole).scaleb(-places)
    out = out.normalize()
    if out.as_tuple().exponent > 0:                   # 5E+1 -> 50
        out = out.quantize(Decimal(1))
    return out


def _split_adjusted(events, date: str, source, value) -> Optional[Decimal]:
    """One stored price as the series should read it. ``None`` passes through."""
    if value is None:
        return None
    price = _D(value)
    if not events or str(source or "").strip().lower() in SPLIT_ADJUSTED_SOURCES:
        return price
    return _rescale_price(price, _cumulative_split(events, date))


def latest_price(conn, symbol: str, as_of: Optional[str] = None) -> Optional[Decimal]:
    """The most recent recorded close for ``symbol`` on/before ``as_of`` (or ever),
    expressed on the SHARE SCALE OF ``as_of``. Reads across the whole canonical
    identity, so a renamed ticker's pre-rename closes still price its holding
    (:func:`_identity_symbols`).

    It prices a holding at ``as_of``, and the share count it multiplies comes from
    the replay AS OF that same date, which has applied every split up to
    ``as_of`` and none after (:func:`_apply_txn`). The price has to be on that
    same scale, and a stored row is on one of two others:

    * an AS-TRADED row (a transaction's own price, a QIF price list, a typed
      price) is on the scale of its own date, so a split between that date and
      ``as_of`` divides it;
    * a PROVIDER row (:data:`SPLIT_ADJUSTED_SOURCES`) arrives already restated
      in today's units, so a split AFTER ``as_of`` multiplies it back up.

    Returning every row raw paired the provider's back-adjusted closes with
    pre-split share counts: a holding's shares before an 8:1 split were valued at
    an eighth of their worth, and a performance report measured from a pre-split
    date showed a gain near 1,000% where the real one was under 30%. A symbol
    with no splits reads exactly the stored close."""
    clause, params = _price_identity_clause(conn, symbol)
    sql = "SELECT date, close_price, source FROM price_history WHERE " + clause
    if as_of is not None:
        sql += " AND date<=?"
        params.append(as_of)
    sql += " ORDER BY date DESC, id DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    price = _D(row["close_price"])
    events = _split_events(conn, symbol)
    if not events:
        return price
    after_as_of = _cumulative_split(events, as_of) if as_of is not None else Fraction(1)
    if str(row["source"] or "").strip().lower() in SPLIT_ADJUSTED_SOURCES:
        return _rescale_price(price, 1 / after_as_of)
    return _rescale_price(price, _cumulative_split(events, row["date"]) / after_as_of)


def price_history(conn, symbol: str, as_of: Optional[str] = None) -> list:
    """The recorded closes for ``symbol`` as ``(date, close_price)`` pairs, ASCENDING
    by date (then id for a stable order within a day), optionally capped at
    ``as_of``. ``close_price`` is a :class:`~decimal.Decimal` (dollars per share),
    so no float error creeps into the series the price-history chart plots. An
    empty list means the symbol has no recorded prices. Rows across the whole
    canonical identity are merged, so an aliased ticker's series is continuous
    across its rename date (:func:`_identity_symbols`).

    SPLIT-ADJUSTED at read time: each as-traded price is divided by the exact
    cumulative factor of every split dated after it (:func:`_cumulative_split`),
    so the whole series reads in TODAY's units. Without it an 8:1 split left the
    pre-split transaction prices standing eight times above the back-adjusted
    downloaded closes -- the spikes the user sees on the chart. Prices from a
    quote provider are already on that scale and are passed through untouched
    (:data:`SPLIT_ADJUSTED_SOURCES`)."""
    clause, params = _price_identity_clause(conn, symbol)
    sql = "SELECT date, close_price, source FROM price_history WHERE " + clause
    if as_of is not None:
        sql += " AND date<=?"
        params.append(as_of)
    sql += " ORDER BY date, id"
    events = _split_events(conn, symbol)
    return [(row["date"],
             _split_adjusted(events, row["date"], row["source"], row["close_price"]))
            for row in conn.execute(sql, params)]


def price_history_bounds(conn, symbol: str, as_of: Optional[str] = None) -> list:
    """``(date, close, low, high)`` per recorded price, ascending.

    ``low``/``high`` are Decimals for a DERIVED price and None for an exactly
    known one (a quote, or a price the source stated). ``high`` alone can be
    None on a row whose share count was too coarse to bound it from above
    (:func:`price_bounds`). Kept separate from :func:`price_history` so the many
    callers that only want the series are unaffected. Unions the canonical
    identity like :func:`price_history`.

    Split-adjusted exactly like :func:`price_history`, close and bounds alike:
    they are drawn on the same axes, so a low/high left on the pre-split scale
    would put the error band eight times above the close it brackets."""
    clause, params = _price_identity_clause(conn, symbol)
    sql = ("SELECT date, close_price, price_low, price_high, source "
           "FROM price_history WHERE " + clause)
    if as_of is not None:
        sql += " AND date<=?"
        params.append(as_of)
    sql += " ORDER BY date, id"
    events = _split_events(conn, symbol)
    out = []
    for row in conn.execute(sql, params):
        date, src = row["date"], row["source"]
        out.append((date,
                    _split_adjusted(events, date, src, row["close_price"]),
                    _split_adjusted(events, date, src, row["price_low"]),
                    _split_adjusted(events, date, src, row["price_high"])))
    return out


# ---------------------------------------------------------------------------
# Instrument kind: what a security's CLASS alone settles about its price
# ---------------------------------------------------------------------------
# NULL kind means UNCLASSIFIED, never "equity" (SRD 5.8e-2), so everything
# here is a no-op until a row actually carries a kind. Nothing below
# classifies anything; it only reads what the source, the deriver or the user
# already stored.

#: Kinds whose price is fixed by construction rather than observed. A
#: money-market sweep is one unit of currency per share by definition -- that
#: is what makes it a sweep -- so there is no market price to look up and any
#: number that is not 1 is an error that shows up as phantom gain or loss.
#: Decimal, never float: prices are Decimal-encoded TEXT everywhere.
PINNED_PRICE_KINDS = {instruments.Kind.MONEY_MARKET.value: Decimal(1)}


def security_kind(conn, symbol) -> Optional[str]:
    """The stored ``securities.kind`` for ``symbol``, lowercased, or ``None``.

    ``None`` covers both "no such row" and "row exists, kind is NULL" because
    they mean the same thing to every caller: UNCLASSIFIED. The row's OWN kind
    wins; only when this spelling says nothing is the answer resolved across the
    identity, so a renamed holding still finds the kind stored under its other
    spelling -- but a contract sitting in a hand-written alias set can never
    inherit its underlying's kind (:func:`_kind_identity_symbols`, SRD 5.8e-2d)."""
    name = str(symbol or "").strip()
    if not name:
        return None
    own = _stored_kind(conn, name)
    if own:
        return own
    for s in _kind_identity_symbols(conn, name):
        kind = _stored_kind(conn, s)
        if kind:
            return kind
    return None


def is_money_market(conn, symbol) -> bool:
    """True for a money-market sweep -- the kind the cash rollup can absorb."""
    return security_kind(conn, symbol) == instruments.Kind.MONEY_MARKET.value


def is_option(conn, symbol) -> bool:
    """True ONLY for a security EXPLICITLY classified ``kind='option'``.

    The one predicate every option-aware branch in this module keys off. False
    for a NULL-kind row, which is UNCLASSIFIED rather than "not an option"
    (SRD 5.8e-2) -- so an unclassified forty-year ledger takes the pre-existing
    code path everywhere, unchanged, until its rows are actually classified."""
    return security_kind(conn, symbol) == instruments.Kind.OPTION.value


def option_terms(conn, symbol) -> Optional[dict]:
    """The stored contract terms for an option, or ``None`` when ``symbol`` is
    not one (SRD 5.8e-5).

    ``{'symbol', 'multiplier', 'underlying', 'expiration', 'strike', 'right'}``.
    ``multiplier`` and ``strike`` come back Decimal (never float); ``multiplier``
    is ``None`` when the column is NULL, which the caller must treat as "not
    recorded", NOT as a licence to assume 100 -- a mini contract is 10 and an
    index contract can be anything. Terms are READ here, never parsed: parsing
    belongs to :mod:`mammon.instruments` and the write belongs to the backfill.
    Resolved across the identity so a contract stored under either spelling of a
    renamed underlying still finds its own row -- but over the KIND-scoped
    identity (SRD 5.8e-2d), so a stock sharing an alias set with a contract never
    picks up that contract's strike, expiration or multiplier."""
    for s in _kind_identity_symbols(conn, symbol):
        if _stored_kind(conn, s) != instruments.Kind.OPTION.value:
            continue
        row = conn.execute(
            "SELECT symbol, multiplier, underlying, expiration, strike, "
            "option_right FROM securities WHERE symbol=?", (s,)).fetchone()
        if row is None:
            continue
        mult = (row["multiplier"] or "").strip() if row["multiplier"] else ""
        strike = (row["strike"] or "").strip() if row["strike"] else ""
        return {
            "symbol": row["symbol"],
            "multiplier": _D(mult) if mult else None,
            "underlying": row["underlying"],
            "expiration": (row["expiration"] or None),
            "strike": _D(strike) if strike else None,
            "right": (row["option_right"] or None),
        }
    return None


def contract_multiplier(conn, symbol) -> Decimal:
    """How many units of the underlying ONE unit of ``symbol``'s quantity
    controls -- the factor between a quoted price and a position's value.

    ``Decimal(1)`` for everything that is not an explicitly classified option,
    including every NULL-kind row, so this is a no-op multiply on the existing
    ledger. For an option it is ``securities.multiplier`` (Decimal TEXT; 100 for
    a standard US equity contract, 10 for a mini).

    An option whose multiplier was never recorded falls back to the schema's
    documented ``NULL = 1`` rather than guessing 100 -- but that is a data gap,
    not an answer, so :func:`option_position_problems` reports it. Guessing
    would be a silent 100x error on exactly the contracts (minis, index,
    adjusted-for-split) where the guess is wrong.

    The kind/multiplier reading itself is :func:`mammon.securities
    .contract_multiplier` -- the one place that knows a NULL multiplier column
    means 1 for a share and UNSTATED for a contract. This wrapper adds the two
    things the arithmetic here needs and that one deliberately withholds:
    identity resolution (a contract stored under either spelling of a renamed
    underlying), and a number rather than :data:`instruments.UNKNOWN`, because a
    valuation site cannot multiply by a sentinel. Imported inside the function:
    :mod:`mammon.securities` imports this module at load time."""
    from mammon import securities as _securities   # circular at import time
    terms = option_terms(conn, symbol)
    if terms is None:
        return Decimal(1)
    mult = _securities.contract_multiplier(conn, terms["symbol"])
    return Decimal(1) if mult is instruments.UNKNOWN else mult


def pinned_price(conn, symbol) -> Optional[Decimal]:
    """The price this security's KIND fixes, or ``None`` if its kind fixes none.

    See :data:`PINNED_PRICE_KINDS`. Consulted ahead of both the caller's
    override and ``price_history``: a sweep that drifted off 1 is exactly what
    a stale recorded close or a downloaded quote does to it, and the kind is
    the more reliable statement."""
    return PINNED_PRICE_KINDS.get(security_kind(conn, symbol))


def _resolve_price(conn, symbol, as_of, prices, account_id=None) -> Optional[Decimal]:
    """The price to value ``symbol`` at ``as_of``: a price PINNED by the
    security's kind wins outright, then an explicit caller-supplied ``prices``
    override (symbol -> price), otherwise the latest recorded ``price_history``
    close on/before ``as_of``. ``None`` when none is available -- the holding is
    reported unpriced rather than guessed at. The caller-supplied override may
    be keyed by any spelling of the identity (the old ticker or the new), so it
    is matched across the identity too -- across the spellings of the SAME
    instrument only (:func:`_kind_identity_symbols`), so a quote injected for a
    stock can never price an option contract fused onto it."""
    fixed = pinned_price(conn, symbol)
    if fixed is not None:
        return fixed
    if prices:
        for s in _kind_identity_symbols(conn, symbol):
            if s in prices:
                return _D(prices[s])
    return latest_price(conn, symbol, as_of)


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------
@dataclass
class HoldingValue:
    symbol: str
    quantity: Decimal
    cost_basis: int                  # cents
    price: Optional[Decimal]         # None if no price is known
    market_value: int                # cents (0 when price unknown)
    gain: Optional[int]              # market_value - cost_basis, None if unpriced


@dataclass
class AccountValuation:
    account_id: int
    cash: int                        # cents (ledger cash + investment cash flows)
    securities: int                  # cents (sum of priced holdings)
    total: int                       # cents (cash + securities)
    holdings: list = field(default_factory=list)   # list[HoldingValue]
    unpriced: list = field(default_factory=list)    # symbols with no price
    # Cents of money-market value folded OUT of ``securities`` and INTO
    # ``cash`` because the caller asked for it (money_market_as_cash). 0 when
    # it did not, which is the default. ``total`` is the same either way --
    # the money did not move, only the bucket it is reported in.
    cash_equivalents: int = 0


def _market_value(conn, symbol, qty: Decimal, price: Decimal) -> int:
    """Market value in CENTS of ``qty`` units of ``symbol`` quoted at ``price``.

    The ONE place in this module where a quantity meets a price (SRD 5.8e-5), so
    that the multiplier is applied once and cannot be forgotten at one of the
    three valuation entry points.

    For a share, and for every NULL-kind (UNCLASSIFIED) row, the multiplier is 1
    and this is the arithmetic that has always been here, bit for bit. For an
    option the quoted price is a PER-SHARE PREMIUM while the quantity is
    CONTRACTS, so the value is ``contracts x premium x multiplier``: ten standard
    contracts at a $4.20 premium are worth $4,200, not $42. Valuing a contract at
    premium x 1 is the defect this fixes -- it understated an options sleeve by
    100x, in the direction that makes net worth wrong.

    The SIGN follows the quantity and nothing takes an absolute value, so a SHORT
    option position (negative contracts -- written to open, not yet bought back)
    values NEGATIVE and subtracts from net worth. That is correct: an open short
    contract is an obligation to deliver, i.e. a liability, and reporting it as a
    positive asset double-counts the premium already received in cash."""
    return _cents(qty * price * contract_multiplier(conn, symbol) * _HUNDRED)


def _holdings_as_of(conn, account_id: int, as_of: Optional[str]):
    """Yield ``(symbol, quantity, cost_basis_cents)`` for the securities the
    account held on ``as_of`` -- the SOURCE SET :func:`holding_values` values.

    With no ``as_of`` this reads the DERIVED ``holdings`` table: one indexed
    SELECT, and the fast path the 40-year open leans on. With an explicit
    ``as_of`` it REPLAYS positions to that date via :func:`compute_holdings`
    (snapshot-seeded through ``holdings_checkpoints``, so the replay stays cheap),
    and a position closed by ``as_of`` (qty 0) is dropped -- exactly as the
    ``holdings`` table excludes a fully-closed position.

    Sourcing from ``holdings`` regardless of ``as_of`` was the historical-
    valuation bug: every past date valued TODAY's share counts, so a since-sold
    position vanished from the past (and an account closed out years ago reported
    its cash sleeve alone), making the whole net-worth curve understate history.
    The two row shapes -- ``holdings`` rows (symbol/quantity/cost_basis) and
    ``compute_holdings``'s ``{symbol: _Lot(qty, cost)}`` -- are normalised HERE,
    at the one boundary, so every caller above sees a single shape."""
    if as_of is None:
        for h in list_holdings(conn, account_id):
            yield h["symbol"], _D(h["quantity"]), (h["cost_basis"] or 0)
    else:
        for sym, lot in sorted(compute_holdings(conn, account_id, as_of).items()):
            if lot.qty == 0:
                continue
            yield sym, lot.qty, lot.cost


def holding_values(conn, account_id: int, as_of: Optional[str] = None,
                   prices: Optional[dict] = None) -> list[HoldingValue]:
    """Value each holding at its latest price on/before ``as_of`` (or an injected
    ``prices`` override, symbol -> price). Unpriced holdings get market_value 0
    and gain None.

    ``as_of`` rewinds the POSITIONS too, not just the price: the shares valued are
    those actually held on that date (:func:`_holdings_as_of`), so a historical
    net-worth figure reflects the past holdings rather than today's. With ``as_of``
    None the current holdings are valued at their latest price (the fast path)."""
    out: list[HoldingValue] = []
    for sym, qty, cost in _holdings_as_of(conn, account_id, as_of):
        price = _resolve_price(conn, sym, as_of, prices, account_id)
        if price is None:
            out.append(HoldingValue(sym, qty, cost, None, 0, None))
        else:
            mv = _market_value(conn, sym, qty, price)
            out.append(HoldingValue(sym, qty, cost, price, mv, mv - cost))
    return out


# ---------------------------------------------------------------------------
# Per-security position (filtered register view + holdings tabs)
# ---------------------------------------------------------------------------
@dataclass
class SecurityPosition:
    """One security's position in an account, valued as of a date. The single
    record shown BOTH beside a security-filtered register and in the Holdings
    window's Currently-Held / Previously-Held tabs, so the two always agree.

    For a currently-held (``is_open``) & priced security ``unrealized_pl`` equals
    the matching :class:`HoldingValue`'s ``gain`` (same qty, cost, price, market
    value); a previously-held security (``is_open`` False) reports zero shares /
    zero cost and its P/L is purely ``realized_pl``."""
    symbol: str
    quantity: Decimal
    cost_basis: int                  # cents (avg cost of remaining shares; 0 if closed)
    dividends: int                   # cents, total income distributions for the symbol
    realized_pl: int                 # cents, realized gain/loss booked on sales
    price: Optional[Decimal]         # None if unpriced or closed
    market_value: int                # cents (0 if unpriced or closed)
    unrealized_pl: Optional[int]     # market_value - cost_basis; None if unpriced/closed
    is_open: bool                    # quantity != 0 -> currently held

    @property
    def total_pl(self) -> int:
        """Realized plus (when priced & held) unrealized P/L -- the P/L to show."""
        return self.realized_pl + (self.unrealized_pl or 0)


@dataclass
class SecurityReport:
    """A security-filtered register: the account's transactions for one symbol
    (each carrying the running ``share_bal`` from :func:`register_rows`) plus the
    :class:`SecurityPosition` summary they reconcile to."""
    symbol: str
    rows: list                       # register_rows filtered to this symbol
    position: SecurityPosition


def _value_position(conn, account_id: int, symbol: str, pos: _Position,
                    as_of: Optional[str], prices: Optional[dict]) -> SecurityPosition:
    """Value a replayed :class:`_Position` at ``as_of`` (the latest recorded price
    on/before as-of, via :func:`_resolve_price`). A closed position (qty 0) is not
    priced; its P/L is realized only. Held & priced -> unrealized = market - cost,
    the same formula :func:`holding_values` uses, so the two reconcile."""
    is_open = pos.qty != 0
    price = None
    market_value = 0
    unrealized = None
    if is_open:
        price = _resolve_price(conn, symbol, as_of, prices, account_id)
        if price is not None:
            market_value = _market_value(conn, symbol, pos.qty, price)
            unrealized = market_value - pos.cost
    return SecurityPosition(
        symbol=symbol, quantity=pos.qty, cost_basis=pos.cost,
        dividends=pos.dividends, realized_pl=pos.realized, price=price,
        market_value=market_value, unrealized_pl=unrealized, is_open=is_open,
    )


def security_positions(conn, account_id: int, as_of: Optional[str] = None,
                       prices: Optional[dict] = None) -> list[SecurityPosition]:
    """Every security the account has EVER held (bought/transferred in), valued as
    of ``as_of``, sorted by symbol. Includes previously-held positions (now zero
    shares) so the Holdings window can list them in their own tab with each one's
    realized P/L. A bare dividend/interest-only symbol (never actually held) is
    excluded. As-of here caps only the valuation PRICE, never the replay -- the
    share counts are always TODAY's. So the open positions here match
    :func:`compute_holdings` and match :func:`holding_values` only at the current
    date (as-of None or the latest activity date); for a historical as-of
    ``holding_values`` rewinds the share counts to that date and these do not,
    deliberately -- this view answers "what I hold now, priced then", the
    valuation path answers "what I held then, priced then"."""
    positions = _replay_positions(conn, account_id)
    return [
        _value_position(conn, account_id, sym, positions[sym], as_of, prices)
        for sym in sorted(positions)
        if positions[sym].ever_held
    ]


def held_positions(conn, account_id: int, as_of: Optional[str] = None,
                   prices: Optional[dict] = None) -> list[SecurityPosition]:
    """Currently-held securities (non-zero shares) -- the Holdings 'Currently
    Held' tab. Ties row-for-row to :func:`holding_values`."""
    return [p for p in security_positions(conn, account_id, as_of, prices) if p.is_open]


def closed_positions(conn, account_id: int, as_of: Optional[str] = None,
                     prices: Optional[dict] = None) -> list[SecurityPosition]:
    """Previously-held securities (now zero shares) -- the Holdings 'Previously
    Held' tab. Each carries its realized P/L and dividend total."""
    return [p for p in security_positions(conn, account_id, as_of, prices) if not p.is_open]


def security_report(conn, account_id: int, symbol: str, as_of: Optional[str] = None,
                    prices: Optional[dict] = None) -> SecurityReport:
    """The security-filtered register for ``symbol``: its transactions (each with
    the running share balance :func:`register_rows` already derives) plus the
    :class:`SecurityPosition` summary (running/total dividends, cost basis, P/L)
    they reconcile to -- the SAME position the Holdings window shows for it."""
    rows = [r for r in register_rows(conn, account_id) if r["symbol"] == symbol]
    positions = _replay_positions(conn, account_id)
    pos = positions.get(symbol, _Position())
    position = _value_position(conn, account_id, symbol, pos, as_of, prices)
    return SecurityReport(symbol=symbol, rows=rows, position=position)


def securities_value(conn, account_id: int, as_of: Optional[str] = None,
                     prices: Optional[dict] = None) -> int:
    """Market value (cents) of all priced holdings in the account."""
    return sum(hv.market_value for hv in holding_values(conn, account_id, as_of, prices))


def delete_investment(conn, txn_id: int) -> bool:
    """Delete one investment transaction. Returns True if a row was removed.

    Mirrors :func:`update_investment`: it invalidates the holdings checkpoints
    from the deleted row's date forward, so a back-dated deletion cascades once
    holdings are rebuilt, and it does NOT rebuild them itself -- the register
    widget does that (matching the update path exactly).

    There was no delete at all: a wrongly-mapped or bogus imported row could be
    edited but never removed, and an import inevitably brings rows that should
    not exist -- a plan's "Change in Market Value" line, a duplicate, a fee
    booked against a security that turned out to be wrong.
    """
    prior = get_investment_txn(conn, txn_id)
    if prior is None:
        return False
    conn.execute("DELETE FROM investment_transactions WHERE id=?", (txn_id,))
    _invalidate_holdings_checkpoints_from(conn, prior["account_id"], prior["date"])
    conn.commit()
    return True


def holding_values_at(conn, account_id: int, as_of: Optional[str] = None,
                      prices: Optional[dict] = None) -> dict:
    """``{symbol: market_value_cents}`` for every security the account HELD on
    ``as_of`` -- the shares ACTUALLY held that day (the replay is rewound to the
    date, unlike :func:`security_positions`, which freezes the share count at the
    latest transaction and only caps the price), valued at the price recorded
    on/before ``as_of``.

    Symbols with no shares held, or with no price available, are omitted (their
    value is zero or unknown). This is the true point-in-time valuation a
    period-bounded gain needs for ``value_at(start)``; ``prices`` overrides the
    recorded close for the named symbols (used only on the 'end' side, where an
    injected quote applies -- a start-date value always reads recorded history)."""
    positions = _replay_positions(conn, account_id, as_of=as_of)
    out: dict = {}
    for sym, pos in positions.items():
        if pos.qty == 0:
            continue
        price = _resolve_price(conn, sym, as_of, prices, account_id)
        if price is not None:
            out[sym] = _market_value(conn, sym, pos.qty, price)
    return out


# ---------------------------------------------------------------------------
# Option positions that cannot be true (SRD 5.8e-6)
# ---------------------------------------------------------------------------
#: Problem codes :func:`option_position_problems` reports.
OPTION_PROBLEM_EXPIRED = "expired"
OPTION_PROBLEM_NO_MULTIPLIER = "missing_multiplier"


@dataclass
class OptionPositionProblem:
    """One option position that the data says cannot be true, as of a date."""
    account_id: int
    symbol: str
    quantity: Decimal                 # signed contracts (negative = short)
    problem: str                      # OPTION_PROBLEM_*
    as_of: str                        # ISO date the check was made against
    expiration: Optional[str] = None  # ISO, when the row records one
    detail: str = ""                  # a sentence for the user


def option_position_problems(conn, account_id: int,
                             as_of: Optional[str] = None) -> list:
    """Option positions in this account that are DATA ERRORS as of ``as_of``
    (today when omitted). Read-only: it reports, it never repairs.

    A nonzero option position dated past its ``expiration`` is the case that
    matters. Every option ends -- exercised, assigned, closed or expired
    worthless -- so a contract still showing contracts after its expiration date
    means a closing transaction is MISSING from the ledger. The replay must not
    silently carry it forward (it then values forever off a stale premium and
    inflates net worth) and must not silently zero it either (that invents a
    disposal the user never made, with a realized gain and a tax year attached).
    A missing close is the user's to resolve, so it surfaces as an error and the
    position is left exactly as recorded.

    Also reported: an option whose ``multiplier`` was never recorded, because
    :func:`contract_multiplier` refuses to guess 100 for it and the position is
    therefore being valued at 1x (see that function).

    A NULL-kind row is never reported -- UNCLASSIFIED is not "option", so an
    unclassified ledger returns an empty list and nothing new appears under the
    user until they classify a row."""
    from mammon import securities as _securities   # circular at import time
    when = as_of or _dt.date.today().isoformat()
    _validate_iso_date(when)
    out: list = []
    for sym, qty, _cost in _holdings_as_of(conn, account_id, as_of):
        if qty == 0 or not is_option(conn, sym):
            continue
        terms = option_terms(conn, sym) or {}
        expires = terms.get("expiration")
        if expires and str(expires) < when:
            side = "short" if qty < 0 else "long"
            out.append(OptionPositionProblem(
                account_id=account_id, symbol=sym, quantity=qty,
                problem=OPTION_PROBLEM_EXPIRED, as_of=when,
                expiration=str(expires),
                detail=(f"{_qty_text(qty)} contracts of {sym} are still open "
                        f"({side}) after expiration {expires}: the closing "
                        "transaction (exercise, assignment, close or expiry) "
                        "is missing from the ledger"),
            ))
        # Asked of :mod:`mammon.securities`, not of the column, so "blank",
        # "unparseable" and "zero or negative" are the one UNSTATED answer here
        # that they are at the valuation site.
        if _securities.contract_multiplier(
                conn, terms.get("symbol") or sym) is instruments.UNKNOWN:
            out.append(OptionPositionProblem(
                account_id=account_id, symbol=sym, quantity=qty,
                problem=OPTION_PROBLEM_NO_MULTIPLIER, as_of=when,
                expiration=(str(expires) if expires else None),
                detail=(f"{sym} has no recorded contract multiplier, so it is "
                        "being valued at 1 unit per contract instead of the "
                        "100 a standard contract controls"),
            ))
    return out


# ---------------------------------------------------------------------------
# The holdings VIEW: contracts grouped under the underlying (SRD 5.8e-8)
# ---------------------------------------------------------------------------
#: Expiration-proximity cues :func:`holdings_view` attaches to an option line.
OPTION_CUE_EXPIRED = "expired"      # already past its expiration, still open
OPTION_CUE_EXPIRING = "expiring"    # expires within ``soon_days``
#: How far ahead "expiring" looks, in days.
OPTION_EXPIRY_SOON_DAYS = 7


@dataclass
class HoldingLine:
    """One row of the Holdings window: a :class:`SecurityPosition` plus what the
    UI needs to DISPLAY it, so the widget layer needs no SQL and no option rules.

    ``group`` is the symbol this line sorts under -- an option's recorded
    ``underlying`` when it has one, otherwise the line's own symbol. It is a
    presentation grouping ONLY: contracts are never folded into the underlying's
    share count, because 3 contracts and 300 shares are different quantities of
    different instruments and adding them is the option-as-a-share bug that
    5.8e-5 exists to stop. Every line keeps its own position, quantity and
    market value; the account total is unchanged.

    For a line that is not EXPLICITLY ``kind='option'`` every option field is
    None/False and ``group`` is the symbol itself, so an unclassified ledger
    produces exactly the rows, in exactly the order, that it did before options
    existed."""
    position: SecurityPosition
    kind: Optional[str] = None            # lowercased securities.kind, or None
    is_option: bool = False
    group: str = ""                       # symbol this line sorts under
    underlying: Optional[str] = None
    expiration: Optional[str] = None      # ISO
    strike: Optional[Decimal] = None
    right: Optional[str] = None           # "call" / "put"
    multiplier: Optional[Decimal] = None
    cue: Optional[str] = None             # OPTION_CUE_*, or None
    problems: list = field(default_factory=list)   # OptionPositionProblem

    @property
    def symbol(self) -> str:
        return self.position.symbol

    @property
    def quantity(self) -> Decimal:
        """Signed: shares for a share line, CONTRACTS for an option line."""
        return self.position.quantity

    @property
    def is_short(self) -> bool:
        """A written contract (or a shorted security): an obligation, not an
        asset. Its market value is already negative (5.8e-5)."""
        return self.position.quantity < 0

    @property
    def grouped(self) -> bool:
        """True when this line sorts under a DIFFERENT symbol -- the cue the UI
        uses to indent it under its underlying."""
        return self.group != self.position.symbol


def holdings_view(conn, account_id: int, as_of: Optional[str] = None,
                  prices: Optional[dict] = None,
                  soon_days: int = OPTION_EXPIRY_SOON_DAYS) -> list:
    """:func:`held_positions` as display lines: option contracts grouped under
    their underlying, each carrying its terms and an expiration cue. Read-only.

    Ordering is ``(group, options after shares, expiration, strike, symbol)``.
    With no options in the account that degenerates to ``sorted by symbol`` --
    byte for byte the order :func:`held_positions` already returns -- so an
    UNCLASSIFIED ledger sees no change at all.

    The cue is not derived here. Expiry logic lives in
    :func:`option_position_problems` and is asked twice: once at ``as_of`` (what
    is expired NOW) and once at ``as_of + soon_days`` (what will be), the
    difference being what is merely expiring. One rule, one implementation; a
    second copy of "is it past expiry" is how the window and the problem report
    start disagreeing. An account holding no contracts never makes the second
    call."""
    lines = []
    for pos in held_positions(conn, account_id, as_of, prices):
        sym = pos.symbol
        terms = option_terms(conn, sym)
        if terms is None:
            lines.append(HoldingLine(position=pos, kind=security_kind(conn, sym),
                                     group=sym))
            continue
        under = terms.get("underlying") or None
        lines.append(HoldingLine(
            position=pos, kind="option", is_option=True,
            group=str(under) if under else sym,
            underlying=(str(under) if under else None),
            expiration=(str(terms["expiration"]) if terms.get("expiration") else None),
            strike=terms.get("strike"), right=terms.get("right"),
            multiplier=terms.get("multiplier"),
        ))
    lines.sort(key=lambda ln: (ln.group, 1 if ln.is_option else 0,
                               ln.expiration or "",
                               ln.strike if ln.strike is not None else Decimal(0),
                               ln.symbol))
    if not any(ln.is_option for ln in lines):
        return lines

    when = as_of or _dt.date.today().isoformat()
    _validate_iso_date(when)
    by_symbol: dict = {}
    for prob in option_position_problems(conn, account_id, when):
        by_symbol.setdefault(prob.symbol, []).append(prob)
    expired = {s for s, ps in by_symbol.items()
               if any(p.problem == OPTION_PROBLEM_EXPIRED for p in ps)}
    horizon = (_dt.date.fromisoformat(when)
               + _dt.timedelta(days=max(0, soon_days))).isoformat()
    expiring = {p.symbol for p in option_position_problems(conn, account_id, horizon)
                if p.problem == OPTION_PROBLEM_EXPIRED} - expired
    for ln in lines:
        ln.problems = by_symbol.get(ln.symbol, [])
        if ln.symbol in expired:
            ln.cue = OPTION_CUE_EXPIRED
        elif ln.symbol in expiring:
            ln.cue = OPTION_CUE_EXPIRING
    return lines


# ---------------------------------------------------------------------------
# Fund conversions kept as one holding (migration 73, SRD 5.8d)
# ---------------------------------------------------------------------------
# User, 2026-09-15: "A brokerage creating a new fund and transfering an old fund
# to the new is a tougher case, especially when the price of the funds is
# different so the number of shares change. It's like a split and a rename all at
# once, but the split isn't a nice ratio of integers." The link is the person's
# decision; the rule below is what entitles them to make it: "require that all of
# the first be sold and that all the proceeds go into the second."
def conversion_proceeds(conn, account_id: int, from_symbol: str, to_symbol: str,
                        date: str) -> Optional[int]:
    """The cents that moved from ``from_symbol`` into ``to_symbol`` on ``date``
    in this account, when that day was a whole conversion, else None.

    Whole means both halves of the user's rule: the old position was open going
    into the day and EMPTY at its end (every share sold), and the purchases of
    the new fund that day cost exactly what the sales brought in, to the cent.
    Partial, or with money left over or added, it is two ordinary trades."""
    old, new = resolve_symbol(conn, from_symbol), resolve_symbol(conn, to_symbol)
    if not old or not new or old == new or not date:
        return None
    rows = conn.execute("SELECT * FROM investment_transactions WHERE account_id=? AND date=?",
                        (account_id, date)).fetchall()
    proceeds = cost = 0
    sold = bought = False
    for t in rows:
        if is_void_investment(t):
            continue
        sym = resolve_symbol(conn, (t["symbol"] or "").strip())
        a = _action_key(t["action"])
        q = _D(t["quantity"])
        if sym == old and a in _SALE_ACTIONS:
            proceeds += _proceeds_of(t, q)
            sold = True
        elif sym == new and a in _ACQUIRE_ACTIONS:
            cost += _cost_of(t, q)
            bought = True
    if not (sold and bought) or proceeds <= 0 or proceeds != cost:
        return None
    prior = (_dt.date.fromisoformat(date) - _dt.timedelta(days=1)).isoformat()
    before = _replay_positions(conn, account_id, prior).get(old)
    after = _replay_positions(conn, account_id, date).get(old)
    if before is None or before.qty <= 0 or (after is not None and after.qty != 0):
        return None
    return proceeds


def holding_links(conn, account_id: int) -> list:
    """``[(from_symbol, to_symbol, date)]`` recorded for the account, oldest first."""
    return [(r["from_symbol"], r["to_symbol"], r["date"]) for r in conn.execute(
        "SELECT from_symbol, to_symbol, date FROM holding_links WHERE account_id=? "
        "ORDER BY date, from_symbol", (account_id,))]


def holding_successors(conn, account_id: int) -> dict:
    """``{symbol: the holding it continues as today}`` for every link that STILL
    meets the conversion rule, chains followed (A -> B -> C gives A: C, B: C). A
    link whose transactions have since been edited so the day is no longer a
    whole conversion is ignored rather than trusted."""
    step = {}
    for frm, to, date in holding_links(conn, account_id):
        if conversion_proceeds(conn, account_id, frm, to, date) is not None:
            step[resolve_symbol(conn, frm)] = resolve_symbol(conn, to)
    out = {}
    for frm in step:
        cur, seen = frm, {frm}
        while cur in step and step[cur] not in seen:
            cur = step[cur]
            seen.add(cur)
        out[frm] = cur
    return out


def conversion_candidates(conn, account_id: int, symbol: str) -> list:
    """``[(from_symbol, to_symbol, date, cents)]``: every fund in the account that
    could be linked into ``symbol``'s holding -- into ``symbol`` itself or into a
    fund already linked to it -- because on a day it was bought, another fund was
    wholly sold for exactly that money. Links already recorded are left out."""
    target = resolve_symbol(conn, symbol)
    succ = holding_successors(conn, account_id)
    group = {target} | {f for f, t in succ.items() if t == target}
    linked = {resolve_symbol(conn, f) for f, _t, _d in holding_links(conn, account_id)}
    out = []
    for to in sorted(group):
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM investment_transactions WHERE account_id=? AND symbol=? "
            "ORDER BY date", (account_id, to))]
        for day in days:
            sellers = {r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM investment_transactions WHERE account_id=? "
                "AND date=? AND symbol IS NOT NULL AND symbol<>? ", (account_id, day, to))}
            for frm in sorted(sellers):
                key = resolve_symbol(conn, frm)
                if key in linked or key in group:
                    continue
                cents = conversion_proceeds(conn, account_id, frm, to, day)
                if cents is not None:
                    out.append((frm, to, day, cents))
    return out


def link_holding(conn, account_id: int, from_symbol: str, to_symbol: str, date: str) -> None:
    """Record that ``from_symbol`` continues as ``to_symbol`` in this account from
    ``date``, for measuring a holding's return (migration 73). Refused unless the
    day was a whole conversion (:func:`conversion_proceeds`), for an option
    contract, or when it would close a loop. Changes no transaction, price or
    holding; :func:`unlink_holding` undoes it completely."""
    if looks_like_option(from_symbol, to_symbol):
        raise ValueError("an option contract does not continue as another security")
    if conversion_proceeds(conn, account_id, from_symbol, to_symbol, date) is None:
        raise ValueError(
            f"{from_symbol} was not wholly sold on {date} with exactly the proceeds "
            f"buying {to_symbol}, so {to_symbol} does not continue it")
    succ = holding_successors(conn, account_id)
    if succ.get(resolve_symbol(conn, to_symbol), resolve_symbol(conn, to_symbol)) == \
            resolve_symbol(conn, from_symbol):
        raise ValueError(f"linking {from_symbol} to {to_symbol} would form a loop")
    conn.execute(
        "INSERT INTO holding_links(account_id, from_symbol, to_symbol, date) VALUES (?,?,?,?) "
        "ON CONFLICT(account_id, from_symbol) DO UPDATE SET to_symbol=excluded.to_symbol, "
        "date=excluded.date", (account_id, from_symbol.strip(), to_symbol.strip(), date))
    conn.commit()


def unlink_holding(conn, account_id: int, from_symbol: str) -> None:
    conn.execute("DELETE FROM holding_links WHERE account_id=? AND from_symbol=?",
                 (account_id, (from_symbol or "").strip()))
    conn.commit()


# ---------------------------------------------------------------------------
# Writing the endings: exercise, assignment, expiry (SRD 5.8e-7)
# ---------------------------------------------------------------------------
def _open_option_position(conn, account_id: int, symbol: str, date: str):
    """``(position, lot_method, canonical_symbol)`` for the option ``symbol`` on
    this account as it stands immediately BEFORE a row dated ``date``.

    The kind gate for both writers below lives here: a symbol that is not
    EXPLICITLY ``kind='option'`` is refused outright, so no option rule can ever
    reach an unclassified security (SRD 5.8e-2). The position is read back off
    the replay rather than off ``holdings`` because the lots -- which premium,
    paid on which date -- are what the ending has to consume, and only the
    replay has them."""
    if not is_option(conn, symbol):
        raise ValueError(
            f"{symbol} is not classified as an option (kind="
            f"{security_kind(conn, symbol)!r}); option life-cycle rows are "
            "written only for kind='option'")
    positions = _replay_positions(conn, account_id, as_of=date)
    sym, pos = symbol, positions.get(symbol)
    if pos is None:
        for s in _kind_identity_symbols(conn, symbol):
            if s in positions:
                sym, pos = s, positions[s]
                break
    if pos is None or pos.qty == 0:
        raise ValueError(
            f"no open position in {symbol} on {date} to exercise, assign or "
            "expire")
    return pos, get_lot_method(conn, account_id), sym


def _closing_basis(pos, q: Decimal, method: str) -> int:
    """The SIGNED cents a close of ``q`` contracts takes out of ``pos``:
    positive for a long position (the premium PAID, its cost basis), negative
    for a written one (the premium RECEIVED, carried as a negative cost).

    Computed on a throwaway copy of the position, by the same two functions the
    replay itself will use when it later reaches the row this answer is about --
    so the figure written into the share leg cannot drift from the figure the
    replay relieves. A full close reports ``pos.cost`` outright, matching
    :func:`_apply_txn`'s rule that a position reaching zero quantity has zero
    cost: otherwise a lot-by-lot sum could leave a stray cent behind."""
    if q >= abs(pos.qty):
        return pos.cost
    dup = _Position(qty=pos.qty, cost=pos.cost,
                    lots=[_Lot(l.qty, l.cost, l.date, l.txn_id)
                          for l in pos.lots])
    taken = (_relieve(dup, q, method, None) if pos.qty > 0
             else _relieve_short(dup, q))
    return sum(c for _, _, _, c in taken)


def record_option_exercise(conn, account_id: int, date: str, symbol: str,
                           quantity=None, *, memo: Optional[str] = None) -> dict:
    """Exercise a long option, or record the assignment of a written one, and
    roll its premium into the basis of the shares that change hands.

    This is the rule Part 1.2 of the instrument taxonomy exists to state, and
    the reason an option cannot be modelled as "a share with a funny symbol":
    when a contract is exercised the premium does not become a gain or a loss.
    It moves. A call exercised buys stock at ``strike + premium``; a written put
    assigned buys it at ``strike - premium``; a put exercised sells at
    ``strike - premium``; a written call assigned sells at ``strike + premium``
    (IRS Pub 550, "Options"). The contract's own holding period is discarded --
    what matters afterwards is how long the SHARES are held.

    Two rows are written, and they are two for a reason. The share leg is a
    plain ``Buy``/``Sell`` carrying the adjusted figure, so every existing
    mechanism -- lots, lot method, realized gain, reconciliation, the register
    -- applies to it with no option awareness whatsoever. The option leg
    (``Exercise`` long, ``Assign`` short) closes the contract and books no gain,
    and its amount is the basis being rolled, SIGNED. That makes the pair
    cash-neutral on the premium: a long call's ``+premium`` cancels the premium
    embedded in the share leg's cost, leaving exactly ``strike x shares`` of
    cash actually moving, which is what the broker's statement will say. Writing
    the share leg at the adjusted basis WITHOUT that cancelling leg would
    double-count a premium that left the account when the contract was bought.

    ``quantity`` is in CONTRACTS and defaults to the whole position; the share
    leg is ``quantity x multiplier`` units of the underlying. Refuses a
    cash-settled contract (one with no underlying recorded): there is nothing to
    deliver, and such a position is closed with Sell to Close or an expiry.
    Commission is deliberately not accepted -- an exercise fee is its own row,
    because folding it into an amount that also encodes a basis roll makes both
    unreadable. Returns the ids and the figures used. Does NOT rebuild holdings;
    call :func:`rebuild_holdings` after."""
    _validate_iso_date(date)
    pos, method, sym = _open_option_position(conn, account_id, symbol, date)
    q = abs(pos.qty) if quantity is None or quantity == "" else _D(quantity)
    if q <= 0:
        raise ValueError("quantity must be a positive number of contracts")
    if q > abs(pos.qty):
        raise ValueError(
            f"{_qty_text(q)} contracts of {sym} exceeds the open position of "
            f"{_qty_text(abs(pos.qty))}")

    terms = option_terms(conn, sym) or {}
    right = (terms.get("right") or "").strip().upper()[:1]
    if right not in ("C", "P"):
        raise ValueError(
            f"{sym} records no option right (call or put), so which way the "
            "shares move is unknown; classify the contract first")
    strike = terms.get("strike")
    if strike is None or strike <= 0:
        raise ValueError(
            f"{sym} records no strike price, so the shares have no price to "
            "change hands at; classify the contract first")
    underlying = (terms.get("underlying") or "").strip()
    if not underlying:
        raise ValueError(
            f"{sym} records no underlying, so there is nothing to deliver -- a "
            "cash-settled contract is closed with Sell to Close or an expiry, "
            "never an exercise")

    mult = contract_multiplier(conn, sym)
    shares = q * mult
    # ``strike`` is a per-share DOLLAR price, like every price column in this
    # module, so it crosses into cents by the same ``* _HUNDRED`` every other
    # site uses (:func:`_cents` only rounds; it does not convert).
    strike_cash = _cents(strike * shares * _HUNDRED)
    long_side = pos.qty > 0
    basis = _closing_basis(pos, q, method)
    # Shares are ACQUIRED by exercising a call or being assigned on a written
    # put, and DISPOSED of by exercising a put or being assigned on a written
    # call. One expression, because those are the two cases where the right and
    # the direction agree.
    acquire = (right == "C") == long_side
    action = OPTION_EXERCISE if long_side else OPTION_ASSIGN
    share_amount = strike_cash + basis if acquire else strike_cash - basis
    if share_amount < 0:
        raise ValueError(
            f"{action} of {sym} would give the shares a negative "
            f"{'cost' if acquire else 'proceeds'} of {share_amount} cents "
            f"(strike {strike_cash}, premium {basis}); check the recorded "
            "strike, multiplier and premium before recording this")

    roll = ("premium rolled into the share basis" if acquire
            else "premium applied against the proceeds")
    opt_id = record_investment(
        conn, account_id, date, action, symbol=sym, quantity=q,
        amount=basis,
        memo=memo or f"{action} {_qty_text(q)} {sym} at {strike} ({roll})")
    shr_id = record_investment(
        conn, account_id, date, "Buy" if acquire else "Sell",
        symbol=underlying, quantity=shares,
        amount=-share_amount if acquire else share_amount,
        memo=(memo or f"{action} {sym}: {_qty_text(shares)} {underlying} "
                      f"at {strike} ({roll})"))
    return {
        "action": action,
        "option_txn_id": opt_id,
        "share_txn_id": shr_id,
        "share_action": "Buy" if acquire else "Sell",
        "symbol": sym,
        "underlying": underlying,
        "contracts": q,
        "shares": shares,
        "option_basis": basis,
        "strike_cash": strike_cash,
        "share_amount": share_amount,
    }


def record_option_expiration(conn, account_id: int, date: str, symbol: str,
                             quantity=None, *,
                             memo: Optional[str] = None) -> int:
    """Let an option expire worthless -- the ending where nobody does anything.

    One row, no cash, and above all NO SHARES: an expiring contract that leaves
    a phantom position behind is the classic option-as-a-share bug. Which of the
    two expiry actions is written is decided here from the position's sign, so
    the caller cannot get it wrong: a long position expiring is an ordinary
    removal whose proceeds are zero, making the loss exactly the premium paid; a
    written one is an ordinary cover that costs nothing, making the gain exactly
    the premium received -- and SHORT-term however long the contract was open,
    because writing an option starts no holding period (Pub 550; see
    :attr:`RealizedGain.term_override`).

    ``quantity`` is in CONTRACTS and defaults to the whole position, which is
    the normal case: expiry is not selective. Does NOT rebuild holdings; call
    :func:`rebuild_holdings` after."""
    _validate_iso_date(date)
    pos, _method, sym = _open_option_position(conn, account_id, symbol, date)
    q = abs(pos.qty) if quantity is None or quantity == "" else _D(quantity)
    if q <= 0:
        raise ValueError("quantity must be a positive number of contracts")
    if q > abs(pos.qty):
        raise ValueError(
            f"{_qty_text(q)} contracts of {sym} exceeds the open position of "
            f"{_qty_text(abs(pos.qty))}")
    long_side = pos.qty > 0
    action = OPTION_EXPIRE if long_side else OPTION_EXPIRE_SHORT
    return record_investment(
        conn, account_id, date, action, symbol=sym, quantity=q, amount=0,
        memo=(memo or f"{_qty_text(q)} {sym} expired worthless ("
                      f"{'premium paid is the loss' if long_side else 'premium received is the gain'})"))


def net_contributions_by_symbol(conn, account_id: int, start: str,
                                end: str) -> dict:
    """``{symbol: net cents}`` contributed to each security over the window
    ``(start, end]`` -- cash paid for share PURCHASES minus cash received for share
    SALES. Positive means net capital was put into the position during the window.

    The window is start-EXCLUSIVE so a transaction dated on ``start`` belongs to
    the starting position (``value_at(start)`` already reflects it), not the
    window's flows -- the same lower-exclusive boundary the checkpoint replay uses.
    Dividend/interest income and reinvestments are deliberately NOT counted (they
    are return, not contributed capital, and a reinvestment nets to zero cash), so
    ``period_gain = value_at(end) - value_at(start) - net_contributions`` isolates
    the security's gain measured from the period start -- matching how the report's
    Gain/Loss column already excludes income (see
    :mod:`mammon.reports.investment_performance`)."""
    out: dict = {}
    rows = conn.execute(
        "SELECT symbol, action, amount FROM investment_transactions "
        "WHERE account_id=? AND date>? AND date<=? "
        "AND symbol IS NOT NULL AND symbol<>''",
        (account_id, start, end),
    ).fetchall()
    for r in rows:
        a = (r["action"] or "").strip().lower().replace(" ", "")
        amt = abs(int(r["amount"] or 0))
        if a in _ACQUIRE_ACTIONS:
            out[r["symbol"]] = out.get(r["symbol"], 0) + amt
        elif a in _DISPOSE_ACTIONS:
            out[r["symbol"]] = out.get(r["symbol"], 0) - amt
    return out


def investment_cash(conn, account_id: int, as_of: Optional[str] = None) -> int:
    """Net cash (cents) from investment transactions on/before ``as_of``: Buys
    spend, Sells/Divs/contributions add, share-transfer/reinvest actions net to
    zero. Applies the action's cash direction to ``abs(amount)`` so it is correct
    whether the amount was stored signed or gross-positive (see ``_cash_sign``).
    This is the cash side that lives in investment_transactions, separate from
    ordinary ``transactions`` rows."""
    sql = "SELECT action, amount FROM investment_transactions WHERE account_id=?"
    params: list = [account_id]
    if as_of is not None:
        sql += " AND date<=?"
        params.append(as_of)
    total = 0
    for row in conn.execute(sql, params):
        total += _cash_effect(row["action"], row["amount"])
    return total


def _duplicated_transfer_leg_total(conn, account_id: int,
                                   as_of: Optional[str] = None) -> int:
    """Signed cents of this account's ``transactions`` transfer legs that ALSO
    appear as an ``XIn``/``XOut`` row in ``investment_transactions`` -- matched by
    ``(date, counter-account, |amount|)``, the same multiset rule the register's
    :func:`_unrepresented_transfer_legs` uses to hide the duplicate.

    ``account_balance`` sums the ``transactions`` leg and ``investment_cash`` sums
    the investment row, so a transfer recorded in BOTH tables (e.g. a split-transfer
    mirror leg backfilled into ``transactions`` for a period an import already
    recorded as an ``XIn``/``XOut``) is counted twice by their sum. The valuation
    subtracts this total so the account's cash matches the register, which counts
    each such transfer exactly once."""
    inv_sql = ("SELECT date, transfer_account_id, amount FROM investment_transactions "
               "WHERE account_id=? AND transfer_account_id IS NOT NULL")
    params: list = [account_id]
    if as_of is not None:
        inv_sql += " AND date<=?"
        params.append(as_of)
    represented: dict = {}
    for t in conn.execute(inv_sql, params):
        key = (t["date"], int(t["transfer_account_id"]), abs(int(t["amount"] or 0)))
        represented[key] = represented.get(key, 0) + 1

    leg_sql = ("SELECT date, id, transfer_account_id, amount FROM transactions "
               "WHERE account_id=? AND transfer_account_id IS NOT NULL")
    params = [account_id]
    if as_of is not None:
        leg_sql += " AND date<=?"
        params.append(as_of)
    leg_sql += " ORDER BY date, id"
    total = 0
    for r in conn.execute(leg_sql, params):
        key = (r["date"], int(r["transfer_account_id"]), abs(int(r["amount"] or 0)))
        if represented.get(key, 0) > 0:
            represented[key] -= 1        # consume one; the pair is double-counted
            total += int(r["amount"] or 0)
    return total


def account_valuation(conn, account_id: int, as_of: Optional[str] = None,
                      prices: Optional[dict] = None,
                      money_market_as_cash: bool = False) -> AccountValuation:
    """Total value of an investment account: cash (ordinary ledger balance plus
    the investment-transaction cash flows) plus the market value of its holdings.
    ``prices`` (symbol -> price) overrides recorded prices for what-if / testing.

    ``money_market_as_cash`` reports a money-market sweep under ``cash`` instead
    of ``securities`` -- which is what a sweep IS to most people, and what a
    brokerage statement usually calls it. It is a REPORTING choice, so it moves
    nothing: ``total`` is identical either way and the holding still appears in
    ``holdings``. Off by default, because the alternative would silently change
    every cash-vs-securities figure an existing ledger already shows. The
    parameter is passed in rather than read here: the domain layer does not
    read UI preferences (the UI reads ``prefs.money_market_as_cash`` and hands
    it down, exactly as it does with the allocation scope)."""
    hvs = holding_values(conn, account_id, as_of, prices)
    securities = sum(hv.market_value for hv in hvs)
    cash = (ledger.account_balance(conn, account_id, as_of)
            + investment_cash(conn, account_id, as_of)
            - _duplicated_transfer_leg_total(conn, account_id, as_of))
    equivalents = 0
    if money_market_as_cash:
        equivalents = sum(hv.market_value for hv in hvs
                          if is_money_market(conn, hv.symbol))
        cash += equivalents
        securities -= equivalents
    unpriced = [hv.symbol for hv in hvs if hv.price is None]
    return AccountValuation(
        account_id=account_id, cash=cash, securities=securities,
        total=cash + securities, holdings=hvs, unpriced=unpriced,
        cash_equivalents=equivalents,
    )


def valuation_as_of(conn) -> Optional[str]:
    """The date to value an account at when the caller names none.

    The most recent date the ledger knows ANYTHING about -- a transaction or a
    recorded price -- not the last transaction alone.

    This used to be :func:`ledger.latest_activity_date` on its own, to stop a
    dormant account being valued at a price recorded long after its last trade
    (a 1997 snapshot shown at a 2014 quote). That cap also made Get Quotes look
    broken, and quietly: a fetched quote is by definition dated later than the
    last transaction, so ``latest_price``'s ``date <= as_of`` filter excluded the
    very price just written and the account total did not move. The user is told
    nine quotes were downloaded and sees nothing change.

    A recorded price IS activity -- it is the ledger learning what something is
    worth today -- so it counts here. The original concern survives where it
    actually bites: a security with no price after 1997 is still valued at its
    1997 price, because ``latest_price`` takes the newest quote per SYMBOL on or
    before this date. Only a security that genuinely has a newer price gets one.
    """
    txn = ledger.latest_activity_date(conn)
    row = conn.execute("SELECT MAX(date) FROM price_history").fetchone()
    price = row[0] if row else None
    return max([d for d in (txn, price) if d], default=None)


def display_balance(conn, account_id: int, as_of: Optional[str] = None,
                    prices: Optional[dict] = None) -> int:
    """The balance to SHOW for an account (cents). An investment account holds
    securities whose worth is NOT captured by summing its cash transactions, so
    its displayed balance -- and its net-worth contribution -- is the full market
    valuation (cash + securities).

    An ASSET account is the same problem in slower motion: its ledger balance is
    what was paid plus improvements (its cost basis), which for a house bought
    decades ago is not remotely what it is worth. When such an account has a
    recorded market value on or before ``as_of`` (``mammon.asset_values``), that
    value is shown instead. With none recorded the ledger balance stands, so an
    unvalued asset is unchanged rather than zeroed.

    Every other account type is its plain ledger balance, untouched."""
    acct = ledger.get_account(conn, account_id)
    # A crypto wallet is investment-LIKE but its holdings live in the crypto_*
    # tables, not investment_transactions/holdings -- so valuing it here would
    # see zero securities and report cash alone. Hand it to the crypto domain
    # layer, which values the coin holdings + cash sleeve. Lazy import: crypto
    # imports investments, so a top-level import here would be circular.
    if acct is not None and (acct["type"] or "") == "crypto":
        from mammon import crypto
        return crypto.display_balance(conn, account_id, as_of, prices)
    if acct is not None and acct["type"] in ledger.INVESTMENT_LIKE_TYPES:
        # No explicit date -> as of the last thing the ledger knows, transaction
        # or quote (valuation_as_of explains why a quote has to count).
        eff = as_of if as_of is not None else valuation_as_of(conn)
        return account_valuation(conn, account_id, eff, prices).total
    if acct is not None and (acct["type"] or "") in asset_values.VALUABLE_TYPES:
        mv = asset_values.market_value(conn, account_id, as_of)
        if mv is not None:
            return mv
    return ledger.account_balance(conn, account_id, as_of)


def net_worth(conn, as_of: Optional[str] = None, prices: Optional[dict] = None,
              *, account_ids=None, include_hidden: bool = False) -> int:
    """Net worth (cents) that VALUES investment accounts at market instead of
    counting their cash alone. ``mammon.ledger.net_worth`` delegates here.

    HIDDEN accounts are excluded, and that is deliberate: hiding is how a user
    says "the records for this account are incomplete -- leave it out of my
    totals". An employer plan whose internals were never recorded holds a
    balance the ledger cannot justify (contributions went in, the shares they
    bought were never entered), and hiding it is the only tool the app offers
    for that. This default was briefly flipped to include them, on the reasoning
    that hidden is a declutter flag and the money is still yours; that reasoning
    is right about a genuinely zeroed account and wrong about the case the
    feature is actually used for, and flipping it silently added a phantom
    balance to the user's net worth.

    So the exclusion stays, and the CHOICE lives in the report bar
    (``ui/report_filters`` -- "Include hidden accounts"), where a net-worth chart
    can opt into the fuller history: an account zeroed before it was hidden
    contributes nothing today but did hold money for years, and leaving it out
    makes decades of saving look like a sudden windfall.

    ``account_ids`` restricts to a chosen subset.
    """
    wanted = None if account_ids is None else {int(a) for a in account_ids}
    return sum(
        display_balance(conn, a["id"], as_of, prices)
        for a in ledger.list_accounts(conn, include_closed=True,
                                      include_hidden=include_hidden)
        if wanted is None or int(a["id"]) in wanted
    )


# ---------------------------------------------------------------------------
# Auto-quotes (network behind an injectable QuoteSource)
# ---------------------------------------------------------------------------
@dataclass
class Quote:
    symbol: str
    date: str            # ISO YYYY-MM-DD
    close: str           # Decimal text
    source: str = "yfinance"


class QuoteSourceUnavailable(RuntimeError):
    """No usable quote backend (e.g. yfinance not installed and no source given)."""


def first_transaction_dates(conn, account_id: int) -> dict:
    """``{symbol: earliest ISO date}`` for the account's securities.

    The range worth fetching history over: nothing before a security was first
    held can affect this account's valuation, so asking a provider for twenty
    years of a stock bought last year is wasted."""
    rows = conn.execute(
        "SELECT symbol, MIN(date) AS d FROM investment_transactions "
        "WHERE account_id=? AND symbol IS NOT NULL AND symbol<>'' "
        "GROUP BY symbol", (account_id,)).fetchall()
    return {r["symbol"]: r["d"] for r in rows if r["d"]}


def fetch_quote_history(conn, pairs, *, start=None, end=None, source=None,
                        interval: str = "1mo") -> int:
    """Fill price history for ``pairs`` -- ``[(holding_name, ticker)]`` -- and
    return the number of rows written.

    Net worth values a holding at the newest price recorded ON OR BEFORE the
    sampled date (:func:`latest_price`), so a security priced in 2015 and not
    again until 2023 holds its 2015 value flat for eight years and then jumps.
    Across a portfolio those steps land on different dates and the growth curve
    comes out jagged -- not because the money moved that way, but because that
    is when prices happened to get recorded. Monthly closes give every holding
    the same resolution and the jaggedness goes away.

    A price carried by an actual TRANSACTION is what the user really paid that
    day, and a monthly close never displaces it
    (:func:`record_prices_if_absent`). A previously DOWNLOADED row is a
    different thing -- a provider's opinion, replaceable by a newer one -- so
    re-running corrects it. Protecting both alike is what made a history
    fetched on the wrong scale permanently unfixable.

    Recorded under the HOLDING's name, not the ticker, for the same reason
    :func:`fetch_quotes` is -- ``price_history`` is keyed by the name a holding
    is stored under, so history filed under "ALTY" prices nothing when the
    holding is "ALTY GLOBAL X SUPERDIVIDEND ALTER".

    A never-quote holding (:func:`securities.never_quote`) is dropped before the
    provider is called, same as in :func:`fetch_quotes` -- a pinned or
    tickerless security has no series to download.
    """
    wanted: dict = {}
    for name, ticker in pairs:
        tick = (ticker or "").strip()
        if tick:
            wanted.setdefault(tick.upper(), []).append(name)
    if not wanted:
        return 0
    _, wanted = _quotable_plan(conn, sorted(wanted), wanted)
    if not wanted:
        return 0
    src = source or default_quote_source()
    getter = getattr(src, "get_history", None)
    if getter is None:
        raise QuoteSourceUnavailable(
            "%s cannot fetch historical quotes; it only reports the latest "
            "close." % type(src).__name__)
    quotes = getter(sorted(wanted), start=start, end=end, interval=interval)
    default_name = getattr(src, "source_name", None)
    rows = []
    for q in quotes:
        for name in wanted.get((q.symbol or "").upper(), ()):
            rows.append((name, q.date, q.close, q.source or default_name))
    # A previously DOWNLOADED row is replaced; a transaction-carried one is not.
    # Without this a re-fetch could never correct a bad series (see
    # record_prices_if_absent), which is exactly the state a wrong-scale
    # download leaves the file in.
    return record_prices_if_absent(conn, rows,
                                   replace_sources=REFETCHABLE_SOURCES)


class YFinanceQuoteSource:
    """Latest close via the ``yfinance`` package. The import and the network call
    happen ONLY inside get_quotes, so importing this module never hits the
    network and tests that inject a fake source never import yfinance."""

    source_name = "yfinance"

    def get_quotes(self, symbols: list[str]) -> list[Quote]:      # pragma: no cover - network
        import yfinance as yf

        out: list[Quote] = []
        for sym in symbols:
            try:
                # As traded, for the same reason get_history is (see there). Over
                # a single day the adjustment is usually nil, so this costs
                # nothing -- but "usually" is not a basis for a price, and a
                # quote fetched the day before an ex-dividend date would land on
                # a different scale from the one fetched the day after.
                hist = yf.Ticker(sym).history(period="1d", auto_adjust=False)
                if hist is None or hist.empty:
                    continue
                close = hist["Close"].iloc[-1]
                day = hist.index[-1].date().isoformat()
                out.append(Quote(sym, day, str(round(Decimal(str(close)), 4)), "yfinance"))
            except Exception:
                continue
        return out

    def get_history(self, symbols: list[str], *, start=None, end=None,
                    interval: str = "1mo") -> list[Quote]:   # pragma: no cover - network
        """Every close in the range, one bar per ``interval`` (default monthly).

        One symbol at a time and failures skipped, matching get_quotes: a ticker
        the provider does not know must not cost the caller the other twenty.
        """
        import yfinance as yf

        out: list[Quote] = []
        for sym in symbols:
            try:
                # auto_adjust=False is LOAD-BEARING, but it buys LESS than it
                # was once thought to. It suppresses the DIVIDEND back-adjust
                # only (yfinance defaults it to True in 1.7.0, returning a
                # total-return series -- that is how a high-yield holding came
                # back at half its real 2021 price, QYLD 11.67 against an
                # as-traded 22.62). It does NOT suppress the SPLIT adjustment:
                # Yahoo's chart endpoint serves OHLC already restated in the
                # currently-trading unit, so after VGT's 8:1 split on 2026-04-21
                # every pre-split close in this download arrives at an eighth of
                # what it traded for, whatever this flag says. That is not
                # fixable at the provider, so it is absorbed on the READ side:
                # rows from a provider are marked with their source and left
                # alone, while the as-traded prices this app derives itself are
                # divided by the cumulative split factor when the series is read
                # (`price_history` / `SPLIT_ADJUSTED_SOURCES`). A dividend, by
                # contrast, is a transaction and not a price revision -- hence
                # the flag stays False.
                kw = {"interval": interval, "auto_adjust": False}
                if start:
                    kw["start"] = start
                if end:
                    kw["end"] = end
                if not start and not end:
                    kw["period"] = "max"
                hist = yf.Ticker(sym).history(**kw)
                if hist is None or hist.empty:
                    continue
                for stamp, row in hist.iterrows():
                    close = row["Close"]
                    if close is None or close != close:      # NaN
                        continue
                    out.append(Quote(sym, stamp.date().isoformat(),
                                     str(round(Decimal(str(close)), 4)), "yfinance"))
            except Exception:
                continue
        return out


class WebSlingerQuoteSource:
    """HOOK ONLY -- do NOT record here. Fallback for symbols no Python package
    covers (illiquid/obscure securities): a recorded webSlinger quote-page script
    would fill these, wired through the same runner seam as mammon.downloads. Left
    unimplemented on purpose per this task's brief (SRD 5.8)."""

    source_name = "webslinger"

    def __init__(self, script: Optional[str] = None):
        self.script = script

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        raise NotImplementedError(
            "webSlinger quote fallback is a documented hook: record a quote-page "
            "script and wire a runner (see mammon.downloads), then implement "
            "get_quotes. Not recorded here per SRD 5.8."
        )


def default_quote_source():
    """The best available quote backend, or raise if none is installed."""
    if importlib.util.find_spec("yfinance") is None:
        raise QuoteSourceUnavailable(
            "yfinance is not installed; pass a QuoteSource explicitly or record a "
            "webSlinger quote script (see WebSlingerQuoteSource)."
        )
    return YFinanceQuoteSource()


def _quotable_plan(conn, symbols, names):
    """``(symbols, ticker -> [holding name])`` with every NEVER-QUOTE security
    removed, de-duplicated, order preserved.

    The guard has to run on the holding NAMES a ticker would be filed against,
    not on the ticker itself, because the dangerous case is precisely the one
    where the two differ: a tickerless plan fund named "INTL EQUITY INDEX"
    reaches a quote path as ticker "INTL", which is a real listed company whose
    price has nothing to do with the fund. A ticker whose targets are ALL
    never-quote is dropped entirely, so the provider is never asked at all --
    skipping beats fetching-then-discarding, which still spends a request and
    still risks writing the wrong price. A ticker no one mapped is checked under
    its own name (that caller is asking for a holding stored under its ticker).

    ``securities`` is imported lazily: it imports THIS module at module scope,
    so importing it at ours would be a cycle."""
    from mammon import securities

    lookup = {k.upper(): list(v) for k, v in (names or {}).items()}
    syms: list[str] = []
    keep: dict = {}
    for s in symbols:
        s = (s or "").strip()
        if not s or s in syms:
            continue
        targets = lookup.get(s.upper())
        if targets is None:
            if securities.never_quote(conn, s):
                continue
            syms.append(s)
            continue
        live = [t for t in targets if not securities.never_quote(conn, t)]
        if not live:
            continue
        syms.append(s)
        keep[s.upper()] = live
    return syms, keep


def fetch_quotes(conn, symbols, source=None, names=None) -> list[Quote]:
    """Fetch the latest close for ``symbols`` and write price_history rows.

    ``source`` is any object with ``get_quotes(symbols) -> list[Quote]`` (and an
    optional ``source_name``); omit it to use :func:`default_quote_source`
    (yfinance). The network lives entirely inside the source, so tests inject a
    fake and never touch it. Returns the quotes fetched.

    ``names`` maps ``ticker -> [holding name, ...]`` and is how a quote reaches
    the row that values a holding, exactly as :func:`fetch_quote_history` does
    it. Without it this wrote every quote under the TICKER it was handed, which
    is a name nothing is stored under: fetching ALTY filed a price against
    "ALTY" while the holding is "ALTY GLOBAL X SUPERDIVIDEND ALTER", so the
    write looked like it worked and valued nothing. Worse, it left a two-row
    phantom series behind that charted instead of the real one. Callers that
    already know the mapping pass it; a caller that passes none is asking for
    the symbols it named to be priced under those same names, which is right
    when the holding IS stored under its ticker."""
    syms, lookup = _quotable_plan(conn, symbols, names)
    if not syms:
        return []
    src = source or default_quote_source()
    quotes = src.get_quotes(syms)
    default_name = getattr(src, "source_name", None)
    for q in quotes:
        targets = lookup.get((q.symbol or "").upper()) or [q.symbol]
        for target in targets:
            record_price(conn, target, q.date, q.close, q.source or default_name)
    return quotes


# ---------------------------------------------------------------------------
# Share reconciliation (SRD 5.11b)
#
# The share balance is to a security what the cash balance is to a bank
# account, and a 401(k) of untickered internal funds is the case that forces
# this: there is no quote source, so the statement's share count is the only
# truth available. So share reconciliation mirrors the cash reconcile shapes
# one-for-one (ledger.reconcile_summary / finish_reconciliation / the draft
# get-save-clear), on the investment_transactions schema, one period per
# (account, security).
#
# Two things a naive "sum the buys, subtract the sells" would get wrong, both
# of them real history the user hit in Quicken:
#
#   * A STOCK SPLIT is not a share delta -- it RESCALES the running balance by
#     M/N on its date. Summing quantities across a split period is off by the
#     whole ratio. The split row here is the EXISTING ``StkSplit`` action
#     replayed through :func:`apply_split`, the same exact-arithmetic helper
#     _apply_txn uses, so a reconciliation and the Holdings window can never
#     disagree about a split.
#   * A RENAMED security is two ticker spellings of ONE identity. Quicken's
#     answer was a huge share "adjustment"; ours is the security_aliases table,
#     so every quantity is summed over the whole identity (via
#     :func:`_identity_symbols`) BEFORE any difference is reported and long
#     before an adjustment is offered.
# ---------------------------------------------------------------------------

_QTY_ACTIONS = (_ADD_ACTIONS | _REMOVE_ACTIONS | _SHORT_OPEN_ACTIONS
                | _SHORT_COVER_ACTIONS | _SPLIT_ACTIONS)

#: Memo marker stamped on a share-balance adjustment, so the row stays
#: identifiable (and therefore deletable) forever after.
SHARE_ADJUSTMENT_MARK = "**SHARE ADJ**"

#: What the UI must tell the user whenever an adjustment is offered or listed.
#: The reconciliation is a stamped fact about rows; deleting the adjustment
#: cannot un-stamp them.
SHARE_ADJUSTMENT_WARNING = (
    "This adjustment is a real transaction: you can delete it later if you find "
    "the missing shares. Deleting it does NOT undo the reconciliation -- the "
    "affected period has to be reconciled again by hand."
)


def _validate_iso_date(date: str) -> None:
    """Storage dates are ISO ``YYYY-MM-DD`` everywhere (SRD compartment M); a
    statement date typed by the dialog has to be checked before it becomes one."""
    try:
        _dt.date.fromisoformat(date)
    except (TypeError, ValueError):
        raise ValueError(f"date must be ISO 'YYYY-MM-DD', got {date!r}")


def _action_key(action) -> str:
    """An action string in the normalized form the action sets are keyed by."""
    return (action or "").strip().lower().replace(" ", "")


def is_quantity_action(action) -> bool:
    """True when this action CHANGES the share balance -- an acquisition, a
    disposal, a short leg, or a split. A Div/IntInc/RtrnCap row moves cash or
    basis only and is invisible to a share reconciliation."""
    return _action_key(action) in _QTY_ACTIONS


def share_qty_delta(txn) -> Decimal:
    """The signed share change one row contributes, or 0 for a split (whose
    effect is multiplicative -- see :func:`apply_split`) or a non-share row."""
    a = _action_key(_row_value(txn, "action"))
    q = _D(_row_value(txn, "quantity"))
    if a in _ADD_ACTIONS or a in _SHORT_COVER_ACTIONS:
        return q
    if a in _REMOVE_ACTIONS or a in _SHORT_OPEN_ACTIONS:
        return -q
    return Decimal(0)


def is_share_adjustment(txn) -> bool:
    """True when the row is a reconciliation share adjustment (memo marker)."""
    return str(_row_value(txn, "memo") or "").startswith(SHARE_ADJUSTMENT_MARK)


@dataclass(frozen=True)
class HeldRange:
    """One uninterrupted stretch during which a symbol had a non-zero position.

    ``end`` is None while the position is STILL open -- which is a different
    statement from "closed today" and has to survive into the overlap test
    (:func:`held_overlap`), where an open range extends to today."""
    start: str
    end: Optional[str]
    direction: str  # "long" or "short"


def held_ranges(conn, symbol) -> list:
    """Every period this stored symbol was actually held, oldest first.

    The share balance is replayed across ALL accounts in date order: a range
    opens on the date the running quantity leaves zero and closes on the date it
    returns to zero, and the sign says whether it was held long or short. A
    symbol with no quantity-changing rows (a dividend-only spelling, a name that
    exists only in ``securities``) therefore has NO ranges at all, which is the
    answer the securities review needs -- it means "nothing here can overlap
    anything", not "held forever".

    Matched on the LITERAL stored spelling (trimmed, case-sensitive) rather than
    on a canonical identity, because the caller -- the securities dialog and its
    overlap rule -- is reasoning about the stored spellings themselves, and
    folding "zzta" into "ZZTA" here would make a case twin look like one symbol
    that had always been held.

    Decimal throughout; a split contributes 0 (:func:`share_qty_delta`), so it
    rescales a position without ever opening or closing a range. A sign flip
    that never lands on a recorded zero closes one range and opens the other on
    that same date, so a long and a short of one symbol are never merged into a
    single stretch."""
    raw = str(symbol or "").strip()
    if not raw:
        return []
    rows = conn.execute(
        "SELECT * FROM investment_transactions "
        "WHERE TRIM(COALESCE(symbol,''))=? ORDER BY date, id", (raw,))
    out: list = []
    qty = Decimal(0)
    start: Optional[str] = None
    direction = "long"
    for t in rows:
        if not is_quantity_action(_row_value(t, "action")) or is_void_investment(t):
            continue
        date = str(_row_value(t, "date") or "")
        prev, qty = qty, qty + share_qty_delta(t)
        if prev == 0 and qty != 0:
            start, direction = date, ("short" if qty < 0 else "long")
        elif prev != 0 and qty == 0:
            out.append(HeldRange(start or date, date, direction))
            start = None
        elif prev != 0 and qty != 0 and (qty < 0) != (prev < 0):
            out.append(HeldRange(start or date, date, direction))
            start, direction = date, ("short" if qty < 0 else "long")
    if start is not None:
        out.append(HeldRange(start, None, direction))
    return out


def held_overlap(a: HeldRange, b: HeldRange, today: Optional[str] = None):
    """The ``(start, end)`` the two ranges have in common, or None.

    Dates are INCLUSIVE on both ends -- selling out of one security and buying
    another on the same day is a same-day handover, and the securities review
    treats that as an overlap rather than as a clean succession. An open range
    (``end is None``) extends to ``today``."""
    now = today or _dt.date.today().isoformat()
    start = max(a.start, b.start)
    end = min(a.end or now, b.end or now)
    if start > end:
        return None
    return (start, end)


def format_held_range(r: HeldRange) -> str:
    """One range as text: ``2004-03-12 - 2011-07-01``, an open one as
    ``2019-05-02 - present``, a single-day one as just that date, and a short
    position marked ``(short)``."""
    if r.end == r.start:
        text = r.start
    else:
        text = f"{r.start} - {r.end or 'present'}"
    return f"{text} (short)" if r.direction == "short" else text


def format_held_ranges(ranges, limit: Optional[int] = None) -> str:
    """Ranges joined by ``; ``. With ``limit``, only that many are spelled out
    and the rest are counted, so a cell stays readable while the caller can show
    the unlimited form in a tooltip."""
    items = list(ranges)
    if not items:
        return ""
    if limit is not None and len(items) > limit:
        shown = [format_held_range(r) for r in items[:limit]]
        return "; ".join(shown) + f"; +{len(items) - limit} more"
    return "; ".join(format_held_range(r) for r in items)


def _share_identity_clause(conn, symbol):
    """``(where_fragment, params)`` selecting every investment row that shares
    ``symbol``'s canonical identity -- the canonical spelling plus every alias
    of it, compared case-insensitively.

    Scoped to ONE instrument (:func:`_kind_identity_symbols`, SRD 5.8e-2d): a
    reconciliation is always within one instrument, so an option contract
    reconciles its own contract count against the statement's options section
    and can never contribute a single unit to the underlying stock's share
    count. Unchanged for every identity without an explicit option in it."""
    syms = [str(s).strip().upper() for s in _kind_identity_symbols(conn, symbol) if s]
    if not syms:
        syms = [str(symbol or "").strip().upper()]
    frag = "UPPER(TRIM(COALESCE(symbol,''))) IN (%s)" % ",".join("?" for _ in syms)
    return frag, syms


def share_identity(conn, symbol) -> list:
    """Every ticker spelling that reconciles as ``symbol``: its canonical symbol
    first, then its aliases. Public so the dialog can show the user WHICH names
    a share balance was summed over. Scoped to one instrument, exactly as
    :func:`_share_identity_clause` sums it, so what the dialog displays is what
    was actually counted."""
    return [s for s in _kind_identity_symbols(conn, symbol) if s]


def share_reconcile_rows(conn, account_id: int, symbol: str,
                         through: Optional[str] = None) -> list:
    """Every share-quantity-changing row for ``symbol``'s identity on this
    account, in application order (date, id), as plain dicts. Voided rows are
    dropped -- a void is out of the share math (see :func:`void_investment`).

    Each dict carries the raw ``split_num``/``split_den``/``quantity`` columns,
    so it can be handed straight to :func:`apply_split`."""
    frag, params = _share_identity_clause(conn, symbol)
    sql = "SELECT * FROM investment_transactions WHERE account_id=? AND " + frag
    args = [account_id, *params]
    if through:
        sql += " AND date<=?"
        args.append(through)
    sql += " ORDER BY date, id"
    out = []
    for t in conn.execute(sql, tuple(args)):
        action = t["action"]
        if not is_quantity_action(action) or is_void_investment(t):
            continue
        split = _action_key(action) in _SPLIT_ACTIONS
        out.append({
            "id": t["id"],
            "date": t["date"],
            "action": action,
            "symbol": t["symbol"],
            "quantity": t["quantity"],
            "split_num": _row_value(t, "split_num"),
            "split_den": _row_value(t, "split_den"),
            "memo": t["memo"],
            "cleared": bool(_row_value(t, "cleared")),
            "reconciled": bool(_row_value(t, "reconciled")),
            "split": split,
            "split_display": split_display(t) if split else "",
            "delta": share_qty_delta(t),
            "adjustment": is_share_adjustment(t),
        })
    return out


def _run_shares(rows, seed=None, *, count_reconciled: bool = True,
                count_cleared: bool = True,
                skip_reconciled: bool = False) -> Decimal:
    """Replay ``rows`` (already in date order) into a running share balance.

    A split RESCALES the balance in place; every other row adds its signed
    delta, but only if the caller counts it. This is the one place the share
    running balance is computed -- summary and finish both call it, so they can
    never disagree."""
    qty = _D(seed)
    for r in rows:
        if skip_reconciled and r["reconciled"]:
            continue
        if r["split"]:
            qty = apply_split(qty, r)
        elif (r["reconciled"] and count_reconciled) or (
                r["cleared"] and not r["reconciled"] and count_cleared):
            qty += r["delta"]
    return qty


def last_share_reconciliation(conn, account_id: int, symbol: str,
                              before: Optional[str] = None):
    """The newest finished share reconciliation for ``symbol``'s canonical
    identity on this account (optionally strictly before a date)."""
    canon = resolve_symbol(conn, symbol)
    sql = ("SELECT * FROM share_reconciliations WHERE account_id=? "
           "AND UPPER(symbol)=?")
    args = [account_id, str(canon or "").upper()]
    if before:
        sql += " AND statement_date<?"
        args.append(before)
    sql += " ORDER BY statement_date DESC, id DESC LIMIT 1"
    return conn.execute(sql, tuple(args)).fetchone()


def list_share_reconciliations(conn, account_id: int,
                               symbol: Optional[str] = None) -> list:
    """Finished share reconciliations for the account, oldest first."""
    sql = "SELECT * FROM share_reconciliations WHERE account_id=?"
    args = [account_id]
    if symbol:
        sql += " AND UPPER(symbol)=?"
        args.append(str(resolve_symbol(conn, symbol) or "").upper())
    sql += " ORDER BY statement_date, id"
    return conn.execute(sql, tuple(args)).fetchall()


def share_reconcile_summary(conn, account_id: int, symbol: str,
                            statement_date: str, stated_ending_qty,
                            starting_qty=None) -> dict:
    """What a share reconciliation of ``symbol`` through ``statement_date``
    stands at. The share mirror of :func:`mammon.ledger.reconcile_summary`.

    ``prior_qty`` is the balance already reconciled (or ``starting_qty`` when
    the user types the statement's starting share count, exactly as the cash
    dialog lets them type a beginning balance). ``computed_ending_qty`` adds the
    CLEARED rows to it; ``difference`` is what is still unexplained -- the
    number the user clears items (or records an adjustment) to drive to zero.

    Quantities in and out are Decimal (strings are accepted and parsed); never
    floats. Splits and aliases are handled by the replay, not by the caller.

    A reconciliation runs WITHIN ONE INSTRUMENT (SRD 5.8e-2d). For an option the
    quantities here are CONTRACTS, reconciled against the statement's options
    section, never against the underlying's share count; ``kind`` says which,
    and ``adjustment_allowed`` is False for an option because there is no such
    thing as adjusting a contract count by inventing shares
    (:func:`record_share_adjustment` refuses it)."""
    if not symbol or not str(symbol).strip():
        raise ValueError("share reconciliation needs a security symbol")
    _validate_iso_date(statement_date)
    canon = _kind_canon(conn, symbol)
    rows = share_reconcile_rows(conn, account_id, canon, through=statement_date)
    explicit = starting_qty not in (None, "")
    if explicit:
        seed = _D(starting_qty)
        prior = _run_shares(rows, seed, count_reconciled=False,
                            count_cleared=False, skip_reconciled=True)
        computed = _run_shares(rows, seed, count_reconciled=False,
                               count_cleared=True, skip_reconciled=True)
    else:
        prior = _run_shares(rows, count_reconciled=True, count_cleared=False)
        computed = _run_shares(rows, count_reconciled=True, count_cleared=True)
    stated = _D(stated_ending_qty)
    prior_row = last_share_reconciliation(conn, account_id, canon,
                                          before=statement_date)
    kind = security_kind(conn, canon)
    return {
        "account_id": account_id,
        "symbol": canon,
        "kind": kind,
        "adjustment_allowed": kind != instruments.Kind.OPTION.value,
        "identity_symbols": share_identity(conn, canon),
        "statement_date": statement_date,
        "prior_qty": prior,
        "prior_statement_date": (prior_row["statement_date"]
                                 if prior_row is not None else None),
        "prior_statement_qty": (_D(prior_row["ending_qty"])
                                if prior_row is not None else None),
        "starting_qty_given": _D(starting_qty) if explicit else None,
        # A split is NOT a statement line the user clears -- it is history the
        # statement's share count already reflects -- so it is reported only in
        # split_rows, and finish stamps it alongside the cleared lines.
        "reconciled_rows": [r for r in rows
                            if r["reconciled"] and not r["split"]],
        "cleared_rows": [r for r in rows if r["cleared"]
                         and not r["reconciled"] and not r["split"]],
        "uncleared_rows": [r for r in rows if not r["cleared"]
                           and not r["reconciled"] and not r["split"]],
        "split_rows": [r for r in rows if r["split"]],
        "cleared_qty_change": computed - prior,
        "computed_ending_qty": computed,
        "stated_ending_qty": stated,
        "difference": stated - computed,
        "adjustment_qty": stated - computed,
        "adjustment_warning": SHARE_ADJUSTMENT_WARNING,
    }


def set_investment_cleared(conn, txn_id: int, cleared: bool = True) -> bool:
    """Mark one investment row cleared (or not) for share reconciliation.
    Refuses to un-clear an already-reconciled row -- reconciling the period
    again is what that is for. Returns True when a row changed."""
    row = get_investment_txn(conn, txn_id)
    if row is None:
        return False
    if _row_value(row, "reconciled"):
        return False
    conn.execute("UPDATE investment_transactions SET cleared=? WHERE id=?",
                 (1 if cleared else 0, txn_id))
    conn.commit()
    return True


def record_share_adjustment(conn, account_id: int, symbol: str, date: str,
                            qty_delta, *, memo: Optional[str] = None,
                            cleared: bool = True) -> int:
    """Record the share adjustment that closes an unexplained share gap.

    Quicken offered these, and they were sometimes enormous -- usually because a
    security had been renamed and the old spelling's shares were invisible. Here
    the alias identity is summed FIRST (:func:`share_reconcile_summary`), so an
    adjustment only ever appears for a genuinely missing share count.

    It is an ordinary ``ShrsIn``/``ShrsOut`` row (no new action invented, no
    cash effect) carrying :data:`SHARE_ADJUSTMENT_MARK` on its memo, so it is
    identifiable in the register and deletable later with
    :func:`delete_share_adjustment`. The caller MUST show the user
    :data:`SHARE_ADJUSTMENT_WARNING` first.

    REFUSED for an option contract (SRD 5.8e-2d). ``ShrsIn``/``ShrsOut`` move
    SHARES, and the gap in an option position is never missing shares -- it is a
    missing open or close of a contract, which has a premium, a multiplier and a
    cost basis attached. Papering over it with a share adjustment would create a
    zero-cost phantom position that values at the contract's premium and never
    expires. Returns the new row id."""
    q = _D(qty_delta)
    if q == 0:
        raise ValueError("a share adjustment of zero shares changes nothing")
    _validate_iso_date(date)
    canon = _kind_canon(conn, symbol)
    if is_option(conn, canon) or is_option(conn, str(symbol).strip()):
        raise ValueError(
            f"refusing a share adjustment for option contract {canon!r}: a "
            "contract count is not a share count, and the missing piece is an "
            "opening or closing trade with a premium and a basis, not shares "
            "-- record the trade instead")
    note = (memo or "").strip()
    text = SHARE_ADJUSTMENT_MARK + " share balance adjustment"
    if note:
        text = text + ": " + note
    txn_id = record_investment(
        conn, account_id, date, "ShrsIn" if q > 0 else "ShrsOut",
        symbol=canon, quantity=_qty_text(abs(q)), amount=0, memo=text)
    if cleared:
        conn.execute("UPDATE investment_transactions SET cleared=1 WHERE id=?",
                     (txn_id,))
        conn.commit()
    return txn_id


def list_share_adjustments(conn, account_id: int,
                           symbol: Optional[str] = None) -> list:
    """Every share adjustment on the account (optionally for one security's
    identity), oldest first, each with the reconciliation it closed. This is
    what lets the user find and delete an adjustment once the real shares turn
    up."""
    frag, params = "", []
    if symbol:
        frag, params = _share_identity_clause(conn, symbol)
    sql = ("SELECT * FROM investment_transactions WHERE account_id=? "
           "AND memo LIKE ?")
    args = [account_id, SHARE_ADJUSTMENT_MARK + "%"]
    if frag:
        sql += " AND " + frag
        args.extend(params)
    sql += " ORDER BY date, id"
    out = []
    for t in conn.execute(sql, tuple(args)):
        recs = conn.execute(
            "SELECT id, statement_date FROM share_reconciliations "
            "WHERE adjustment_txn_id=?", (t["id"],)).fetchall()
        out.append({
            "id": t["id"], "date": t["date"], "action": t["action"],
            "symbol": t["symbol"], "quantity": t["quantity"],
            "delta": share_qty_delta(t), "memo": t["memo"],
            "reconciled": bool(_row_value(t, "reconciled")),
            "reconciliation_ids": [r["id"] for r in recs],
            "statement_dates": [r["statement_date"] for r in recs],
            "warning": SHARE_ADJUSTMENT_WARNING,
        })
    return out


def delete_share_adjustment(conn, txn_id: int) -> dict:
    """Delete a share adjustment the user no longer believes in.

    Deliberately does NOT unwind anything else: the rows it let them reconcile
    stay stamped, and the share_reconciliations row stays as the record that the
    period WAS reconciled -- it just loses its link and gains a note saying the
    adjustment was removed. Restoring the reconciliation is manual, which is
    exactly what :data:`SHARE_ADJUSTMENT_WARNING` tells the user up front.

    Returns ``{"deleted", "reconciliation_ids", "warning"}``."""
    row = get_investment_txn(conn, txn_id)
    if row is None or not is_share_adjustment(row):
        return {"deleted": False, "reconciliation_ids": [],
                "warning": SHARE_ADJUSTMENT_WARNING}
    recs = [r["id"] for r in conn.execute(
        "SELECT id FROM share_reconciliations WHERE adjustment_txn_id=?",
        (txn_id,))]
    for rid in recs:
        conn.execute(
            "UPDATE share_reconciliations SET adjustment_txn_id=NULL, "
            "note=TRIM(COALESCE(note,'') || ' adjustment deleted; restore this"
            " reconciliation by hand.') WHERE id=?", (rid,))
    conn.commit()
    delete_investment(conn, txn_id)
    return {"deleted": True, "reconciliation_ids": recs,
            "warning": SHARE_ADJUSTMENT_WARNING}


def finish_share_reconciliation(conn, account_id: int, symbol: str,
                                statement_date: str, stated_ending_qty,
                                *, starting_qty=None, adjust: bool = False,
                                adjust_memo: Optional[str] = None,
                                note: Optional[str] = None) -> int:
    """Stamp the cleared share rows reconciled and record the finished period.
    The share mirror of :func:`mammon.ledger.finish_reconciliation`.

    Refuses a non-zero difference, exactly as the cash side does, UNLESS
    ``adjust=True``: then the remaining gap is booked as a share adjustment
    (:func:`record_share_adjustment`) dated ``statement_date`` and the period
    closes on it. Splits in the period are stamped along with the cleared rows
    -- a split is part of the history the statement's share count already
    reflects, not a line the user clears.

    Idempotent: finishing the same (account, security, statement date) again
    upserts the same row and re-stamps nothing, because the rows are already
    reconciled and the difference is therefore already zero.

    Returns the share_reconciliations row id."""
    _validate_iso_date(statement_date)
    canon = resolve_symbol(conn, str(symbol).strip())
    summary = share_reconcile_summary(conn, account_id, canon, statement_date,
                                      stated_ending_qty, starting_qty)
    adj_id = None
    if summary["difference"] != 0 and adjust:
        adj_id = record_share_adjustment(
            conn, account_id, canon, statement_date, summary["difference"],
            memo=adjust_memo)
        summary = share_reconcile_summary(conn, account_id, canon,
                                          statement_date, stated_ending_qty,
                                          starting_qty)
    if summary["difference"] != 0:
        raise ValueError(
            "cannot finish share reconciliation for " + str(canon) + ": off by "
            + _qty_text(summary["difference"]) + " shares (clear items until "
            "the difference is zero, or record an adjustment)")
    ids = [r["id"] for r in summary["cleared_rows"]]
    ids += [r["id"] for r in summary["split_rows"] if not r["reconciled"]]
    for txn_id in sorted(set(ids)):
        conn.execute("UPDATE investment_transactions SET cleared=1, "
                     "reconciled=1 WHERE id=?", (txn_id,))
    conn.execute(
        "INSERT INTO share_reconciliations"
        "(account_id, symbol, statement_date, starting_qty, ending_qty,"
        " adjustment_txn_id, note) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(account_id, symbol, statement_date) DO UPDATE SET"
        " starting_qty=excluded.starting_qty, ending_qty=excluded.ending_qty,"
        " adjustment_txn_id=COALESCE(excluded.adjustment_txn_id,"
        " adjustment_txn_id), note=COALESCE(excluded.note, note)",
        (account_id, canon, statement_date, _qty_text(summary["prior_qty"]),
         _qty_text(summary["computed_ending_qty"]), adj_id, note))
    clear_share_reconcile_draft(conn, account_id, canon)
    conn.commit()
    row = conn.execute(
        "SELECT id FROM share_reconciliations WHERE account_id=? AND symbol=? "
        "AND statement_date=?", (account_id, canon, statement_date)).fetchone()
    return row["id"]


_SHARE_DRAFT_FIELDS = (
    "statement_date", "starting_qty", "ending_qty",
    "starting_price", "ending_price",
)


def get_share_reconcile_draft(conn, account_id: int,
                              symbol: str) -> Optional[dict]:
    """The in-progress share reconciliation for one security, or None. Mirrors
    :func:`mammon.ledger.get_reconcile_draft`; the price fields are carried for
    the dialog's starting/ending value columns (the share reconciliation itself
    needs only the two quantities)."""
    canon = resolve_symbol(conn, str(symbol).strip())
    row = conn.execute(
        "SELECT * FROM share_reconcile_drafts WHERE account_id=? AND symbol=?",
        (account_id, canon)).fetchone()
    return {k: row[k] for k in _SHARE_DRAFT_FIELDS} if row is not None else None


def save_share_reconcile_draft(conn, account_id: int, symbol: str,
                               **fields) -> None:
    """Upsert the in-progress share reconciliation for one security."""
    bad = set(fields) - set(_SHARE_DRAFT_FIELDS)
    if bad:
        raise ValueError("unknown share reconcile draft field(s): "
                         + str(sorted(bad)))
    canon = resolve_symbol(conn, str(symbol).strip())
    conn.execute(
        "INSERT OR IGNORE INTO share_reconcile_drafts(account_id, symbol) "
        "VALUES (?,?)", (account_id, canon))
    if fields:
        sets = ", ".join(k + "=?" for k in fields)
        conn.execute(
            "UPDATE share_reconcile_drafts SET " + sets + ", "
            "updated_at=datetime('now') WHERE account_id=? AND symbol=?",
            (*[str(v) for v in fields.values()], account_id, canon))
    conn.commit()


def clear_share_reconcile_draft(conn, account_id: int,
                                symbol: Optional[str] = None) -> None:
    """Drop the draft for one security (or every draft on the account)."""
    if symbol:
        canon = resolve_symbol(conn, str(symbol).strip())
        conn.execute("DELETE FROM share_reconcile_drafts WHERE account_id=? "
                     "AND symbol=?", (account_id, canon))
    else:
        conn.execute("DELETE FROM share_reconcile_drafts WHERE account_id=?",
                     (account_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Holdings / balance SNAPSHOT application (SRD 5.11b; the parser is
# mammon/importers/holdings_csv.py)
#
# A snapshot is a statement of FACT -- "you hold 123.456 shares of ANON
# BALANCED FUND, priced 24.19, as of 2026-03-31" -- so it must never become a
# transaction: that would invent history. What it legitimately does is exactly
# two things, both of them writes only this module is allowed to make:
#
#   * it states the ENDING share count a share reconciliation reconciles TO, so
#     it is stored in the reconcile draft (share_reconcile_drafts) where the
#     dialog picks it up, rather than being typed by hand per fund;
#   * it carries a per-share price for a fund that HAS no ticker and therefore
#     no quote source -- the user's stated gap. That price goes into
#     price_history like any quote, marked with its own source so a later real
#     quote can be told from a statement figure.
#
# Matching is by symbol when the file has one and by NAME otherwise, because a
# plan export of internal funds usually has no symbol column at all. Both routes
# end at the canonical symbol through resolve_symbol / security_aliases, so a
# renamed security stays ONE identity here exactly as it does in the reconcile.
# ---------------------------------------------------------------------------

#: price_history.source stamped on a price that came from a statement snapshot
#: rather than a quote feed.
SNAPSHOT_PRICE_SOURCE = "snapshot"


def _snap_field(rec, key: str) -> str:
    """One field of a snapshot record, which may be a
    :class:`mammon.importers.holdings_csv.HoldingSnapshot` or a plain dict."""
    if isinstance(rec, dict):
        return str(rec.get(key, "") or "")
    return str(getattr(rec, key, "") or "")


def _known_symbol(conn, account_id: int, text) -> Optional[str]:
    """The canonical symbol ``text`` names -- as a securities row, as a recorded
    alias, or as a symbol already used on this account -- else None. Compared
    case-insensitively and trimmed, because a statement prints what it likes."""
    t = str(text or "").strip()
    if not t:
        return None
    row = conn.execute(
        "SELECT symbol FROM securities WHERE UPPER(TRIM(symbol))=?",
        (t.upper(),)).fetchone()
    if row is not None:
        return resolve_symbol(conn, row["symbol"])
    row = conn.execute(
        "SELECT alias_symbol FROM security_aliases "
        "WHERE UPPER(TRIM(alias_symbol))=?", (t.upper(),)).fetchone()
    if row is not None:
        return resolve_symbol(conn, row["alias_symbol"])
    # A security is STORED under whatever a source called it, with the public
    # ticker (when one is known) in its own column -- so a file that prints only
    # "ANONX" must be allowed to find "ANONX ANON LARGE CAP INDEX".
    row = conn.execute(
        "SELECT symbol FROM securities WHERE UPPER(TRIM(COALESCE(ticker,'')))=?",
        (t.upper(),)).fetchone()
    if row is not None:
        return resolve_symbol(conn, row["symbol"])
    used = symbols_used(conn, account_id)
    for s in used:
        if str(s).strip().upper() == t.upper():
            return resolve_symbol(conn, s)
    for s in used:                      # exact spelling first, ticker second
        if ticker_of(s) and ticker_of(s) == t.upper():
            return resolve_symbol(conn, s)
    return None


def match_snapshot_security(conn, account_id: int, symbol=None,
                            name=None) -> dict:
    """Which security a snapshot line is about: ``{"symbol", "matched_by",
    "name"}`` with ``matched_by`` one of ``"symbol"``, ``"name"`` or None.

    Symbol first when the file has one; then the NAME, which for an untickered
    401(k) fund is the only identity it carries -- matched against
    ``securities.name`` and then against the symbols the account already uses
    (a tickerless fund is recorded under its own name as its symbol). Every hit
    resolves through :func:`resolve_symbol`, so a fund matched under a former
    spelling lands on the canonical identity the reconciliation sums over."""
    sym = str(symbol or "").strip()
    nm = str(name or "").strip()
    if sym:
        hit = _known_symbol(conn, account_id, sym)
        if hit:
            return {"symbol": hit, "matched_by": "symbol", "name": nm}
    if nm:
        row = conn.execute(
            "SELECT symbol FROM securities "
            "WHERE UPPER(TRIM(COALESCE(name,'')))=?", (nm.upper(),)).fetchone()
        if row is not None:
            return {"symbol": resolve_symbol(conn, row["symbol"]),
                    "matched_by": "name", "name": nm}
        hit = _known_symbol(conn, account_id, nm)
        if hit:
            return {"symbol": hit, "matched_by": "name", "name": nm}
    return {"symbol": resolve_symbol(conn, sym) if sym else None,
            "matched_by": None, "name": nm}


def _snapshot_price(quantity: Decimal, price_text: str,
                    value_text: str) -> str:
    """The per-share price a snapshot line states, as Decimal text. Falls back
    to value / shares when the statement printed only a position value, which
    plan statements routinely do. Never a float; "" when neither is available
    (or the share count is zero, which cannot imply a price)."""
    if price_text:
        return _qty_text(_D(price_text))
    if value_text and quantity != 0:
        derived = (_D(value_text) / quantity).quantize(Decimal("0.000001"))
        return _qty_text(derived)
    return ""


def apply_holdings_snapshot(conn, account_id: int, records, *,
                            as_of: Optional[str] = None,
                            source: str = SNAPSHOT_PRICE_SOURCE,
                            write_prices: bool = True,
                            seed_drafts: bool = True) -> list[dict]:
    """Apply a parsed holdings snapshot to one investment account.

    **Creates no transactions, ever.** Per matched line it records the stated
    per-share price in ``price_history`` (the only price a tickerless fund will
    ever have) and seeds that security's share-reconcile draft with the
    statement date, ending share count and ending price, so the share reconcile
    dialog opens with the statement's numbers already in it.

    ``as_of`` is the fallback statement date for lines whose file carried none;
    a line with no date at all is an error, because an undated share count
    cannot be reconciled to anything.

    Returns one result dict per input line: ``name``, ``symbol`` (canonical or
    None), ``matched_by``, ``date``, ``quantity``/``price``/``market_value`` as
    Decimal text, the account's current ``book_qty`` for that security and the
    ``difference`` the reconciliation would have to explain, plus
    ``price_recorded`` / ``draft_saved``. An UNMATCHED line is reported, not
    guessed at and not silently dropped: the user is the only one who can say
    which security an unknown fund name is."""
    out: list[dict] = []
    books: dict = {}
    for rec in records:
        name = _snap_field(rec, "name")
        symbol = _snap_field(rec, "symbol")
        qty_text = _snap_field(rec, "quantity")
        date = _snap_field(rec, "date") or (as_of or "")
        label = symbol or name or "(unnamed)"
        if not date:
            raise ValueError("holdings snapshot line for " + label
                             + " has no as-of date")
        _validate_iso_date(date)
        qty = _D(qty_text)
        price_text = _snapshot_price(qty, _snap_field(rec, "price"),
                                     _snap_field(rec, "market_value"))
        match = match_snapshot_security(conn, account_id, symbol, name)
        canon = match["symbol"]
        priced = drafted = False
        book = None
        if canon and match["matched_by"]:
            if date not in books:
                books[date] = compute_holdings(conn, account_id, as_of=date)
            lot = books[date].get(canon)
            book = lot.qty if lot is not None else Decimal(0)
            if write_prices and price_text:
                record_price(conn, canon, date, price_text, source)
                priced = True
            if seed_drafts:
                fields = {"statement_date": date,
                          "ending_qty": _qty_text(qty)}
                if price_text:
                    fields["ending_price"] = price_text
                save_share_reconcile_draft(conn, account_id, canon, **fields)
                drafted = True
        out.append({
            "name": name,
            "symbol": canon,
            "matched_by": match["matched_by"],
            "date": date,
            "quantity": _qty_text(qty),
            "price": price_text,
            "market_value": _snap_field(rec, "market_value"),
            "book_qty": _qty_text(book) if book is not None else None,
            "difference": _qty_text(qty - book) if book is not None else None,
            "price_recorded": priced,
            "draft_saved": drafted,
        })
    return out


def snapshot_targets(conn, account_id: int) -> dict:
    """``{canonical_symbol: {"statement_date", "ending_qty", "ending_price"}}``
    for every security on this account with a saved reconcile draft -- what an
    imported snapshot left behind for the share reconcile dialog to open on."""
    out: dict = {}
    for row in conn.execute(
            "SELECT symbol FROM share_reconcile_drafts WHERE account_id=? "
            "ORDER BY symbol", (account_id,)):
        draft = get_share_reconcile_draft(conn, account_id, row["symbol"])
        if draft is not None:
            out[row["symbol"]] = draft
    return out

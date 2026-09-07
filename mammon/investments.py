"""mammon.investments -- holdings, price history, and account valuation (SRD 5.8).

Sits on mammon.db + mammon.ledger. Investment activity lives in the
``investment_transactions`` table (Buy/Sell/ReinvDiv/Div/IntInc/XIn/XOut/...);
this module DERIVES per-symbol ``holdings`` (quantity + average-cost basis) from
that history, records a per-symbol ``price_history``, and values a holding or a
whole investment account using the latest price.

Money is signed integer cents; share quantities and prices are Decimal-precision
TEXT so no float error creeps into share math. All arithmetic here goes through
Decimal and rounds money HALF_UP at the cents boundary.

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

from mammon import asset_values, ledger

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
    a = str(action or "").strip().lower()
    if not a:
        return False
    known = (_ADD_ACTIONS | _REMOVE_ACTIONS | _RTRNCAP_ACTIONS
             | _SHORT_OPEN_ACTIONS | _SHORT_COVER_ACTIONS | _SPLIT_ACTIONS
             | _SALE_ACTIONS | _DIVIDEND_ACTIONS)
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
        text, updated on share-MOVING actions (:data:`_ADD_ACTIONS` -> +qty,
        :data:`_REMOVE_ACTIONS` -> -qty) and SCALED by a split
        (:data:`_SPLIT_ACTIONS` -> x q/10) -- the SAME classification
        :func:`compute_holdings` uses, so the last share_bal per symbol ties to
        holdings/valuation. ``None`` on cash-only or non-share-moving rows
        (Div/IntInc/XIn/MiscInc/Cash) -- Quicken leaves Share Bal blank there.

        The split case was missing, and it broke the tie to holdings rather than
        just the split's own row: a StkSplit is neither an ADD nor a REMOVE, so
        the running total was left at its pre-split value and EVERY later row for
        that security reported a stale balance. An 8-for-1 on 26 shares left the
        column reading 26 forever while holdings said 208.
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
    if acct is not None:
        try:
            cash = acct["opening_balance"] or 0
        except (KeyError, IndexError):
            cash = 0
    share_bal: dict[str, Decimal] = {}
    out: list[dict] = []
    for t in _register_sequence(conn, account_id):
        cash_leg = isinstance(t, dict) and t.get("cash_leg")
        a = (t["action"] or "").strip().lower().replace(" ", "")
        # A backfilled transfer leg lives in the cash ``transactions`` table (see
        # _register_sequence); its stored signed amount IS its cash effect, so it
        # bypasses the action-based _cash_effect classification.
        camt = int(t["amount"] or 0) if cash_leg else _cash_effect(t["action"], t["amount"])
        cash += camt
        sym = t["symbol"]
        share_moving = a in _ADD_ACTIONS or a in _REMOVE_ACTIONS
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
        elif sym and share_moving:
            q = _D(t["quantity"])
            bal = share_bal.get(sym, Decimal(0))
            bal = bal + q if a in _ADD_ACTIONS else bal - q
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


def _register_sequence(conn, account_id: int) -> list:
    """The account's rows for the register in application order -- the real
    ``investment_transactions`` rows merged with any cash-only transfer legs that
    only exist in ``transactions`` (:func:`_unrepresented_transfer_legs`). Ordered
    by date, then investment rows before backfilled cash legs, then id, so the
    running cash balance accumulates in a stable, date-correct sequence. With no
    stray legs this is exactly ``list_investment_txns`` order (existing behaviour)."""
    inv_rows = list_investment_txns(conn, account_id)
    legs = _unrepresented_transfer_legs(conn, account_id, inv_rows)
    if not legs:
        return list(inv_rows)

    def _key(r):
        is_leg = isinstance(r, dict) and r.get("cash_leg")
        return (r["date"] or "", 1 if is_leg else 0, int(r["id"] or 0))

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

    @property
    def gain(self) -> int:
        return self.proceeds - self.basis

    @property
    def term(self) -> str:
        """``long`` when held more than one year, ``short`` otherwise,
        ``unknown`` when the acquisition date is not known."""
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
            proceeds = _proceeds_of(t, q)
            pos.realized += proceeds - (cost_before - pos.cost)
            pos.gains.extend(_gain_rows(t, sym, q, proceeds, taken))
        pos.ever_held = True
    elif a in _SHORT_OPEN_ACTIONS:
        # Open/add to a short: shares go negative; proceeds credit the basis
        # (a negative cost, the mirror of a Buy's positive basis).
        pos.qty -= q
        pos.cost -= _cost_of(t, q)
        pos.ever_held = True
    elif a in _SHORT_COVER_ACTIONS:
        # Buy the shares back: relieve the proportional credit, raise qty
        # toward zero. avg credit/share uses abs(qty) so its sign survives.
        # (Short realized P/L is intentionally not booked here -- shorts are
        # rare and out of scope for the per-security P/L view.)
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
    return positions


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


def _cost_of(t, qty: Decimal) -> int:
    """Cash cost (cents) a share-adding transaction adds to basis: the actual
    cash out if the row carries an amount, else price*qty plus commission."""
    amount = t["amount"]
    if amount not in (None, 0):
        return abs(amount)
    base = _cents(qty * _D(t["price"]) * _HUNDRED) if t["price"] else 0
    return base + abs(t["commission"] or 0)


def _proceeds_of(t, qty: Decimal) -> int:
    """Net cash (cents) a share-removing SALE brings in: the row's amount if it
    carries one, else price*qty, in both cases LESS commission -- the mirror of
    :func:`_cost_of`, so realized P/L (proceeds - relieved basis) nets the
    commission paid on both the buy and the sell."""
    amount = t["amount"]
    if amount not in (None, 0):
        gross = abs(amount)
    else:
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
    return conn.execute(
        "SELECT * FROM holdings WHERE account_id=? AND symbol=?", (account_id, symbol)
    ).fetchone()


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


def learn_prices_from_transactions(conn, account_id=None, *, txn_id=None) -> int:
    """Record each investment transaction's own per-share price into price_history.

    A Buy/Sell/ReinvDiv/ShrsOut row states what one share was worth on its date --
    the QIF ``I`` field -- which is real price history the broker already handed
    us. Capturing it means a 401(k) whose fund prices are quoted nowhere public
    still values correctly, and it costs nothing: the data is already in the row.

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
    cols = "symbol, date, price, amount, commission, quantity"
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
    priced = []
    for r in rows:
        sym = str(r["symbol"] or "").strip()
        if not sym or not r["date"]:
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
        lots = compute_holdings(conn, account_id, as_of=date)
        computed_qty = lots[sym].qty if sym in lots else Decimal(0)
        reported_qty = _D(units) if units not in (None, "") else Decimal(0)
        if computed_qty != reported_qty:
            out.append({
                "symbol": sym, "date": date, "field": "quantity",
                "computed": _qty_text(computed_qty),
                "reported": _qty_text(reported_qty),
            })
        if price not in (None, ""):
            reported_price = _D(price)
            row = conn.execute(
                "SELECT close_price FROM price_history WHERE symbol=? AND date=?",
                (sym, date),
            ).fetchone()
            if row is not None and _D(row["close_price"]) != reported_price:
                out.append({
                    "symbol": sym, "date": date, "field": "price",
                    "computed": _qty_text(_D(row["close_price"])),
                    "reported": _qty_text(reported_price),
                })
    return out


def latest_price(conn, symbol: str, as_of: Optional[str] = None) -> Optional[Decimal]:
    """The most recent recorded close for ``symbol`` on/before ``as_of`` (or ever)."""
    sql = "SELECT close_price FROM price_history WHERE symbol=?"
    params: list = [symbol]
    if as_of is not None:
        sql += " AND date<=?"
        params.append(as_of)
    sql += " ORDER BY date DESC, id DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return _D(row["close_price"]) if row else None


def price_history(conn, symbol: str, as_of: Optional[str] = None) -> list:
    """The recorded closes for ``symbol`` as ``(date, close_price)`` pairs, ASCENDING
    by date (then id for a stable order within a day), optionally capped at
    ``as_of``. ``close_price`` is a :class:`~decimal.Decimal` (dollars per share),
    so no float error creeps into the series the price-history chart plots. An
    empty list means the symbol has no recorded prices."""
    sql = "SELECT date, close_price FROM price_history WHERE symbol=?"
    params: list = [symbol]
    if as_of is not None:
        sql += " AND date<=?"
        params.append(as_of)
    sql += " ORDER BY date, id"
    return [(row["date"], _D(row["close_price"])) for row in conn.execute(sql, params)]


def price_history_bounds(conn, symbol: str, as_of: Optional[str] = None) -> list:
    """``(date, close, low, high)`` per recorded price, ascending.

    ``low``/``high`` are Decimals for a DERIVED price and None for an exactly
    known one (a quote, or a price the source stated). ``high`` alone can be
    None on a row whose share count was too coarse to bound it from above
    (:func:`price_bounds`). Kept separate from :func:`price_history` so the many
    callers that only want the series are unaffected."""
    sql = ("SELECT date, close_price, price_low, price_high FROM price_history "
           "WHERE symbol=?")
    params: list = [symbol]
    if as_of is not None:
        sql += " AND date<=?"
        params.append(as_of)
    sql += " ORDER BY date, id"
    out = []
    for row in conn.execute(sql, params):
        lo = row["price_low"]
        hi = row["price_high"]
        out.append((row["date"], _D(row["close_price"]),
                    _D(lo) if lo is not None else None,
                    _D(hi) if hi is not None else None))
    return out


def _resolve_price(conn, symbol, as_of, prices, account_id=None) -> Optional[Decimal]:
    """The price to value ``symbol`` at ``as_of``: an explicit caller-supplied
    ``prices`` override (symbol -> price) takes precedence, otherwise the latest
    recorded ``price_history`` close on/before ``as_of``. ``None`` when neither
    is available -- the holding is reported unpriced rather than guessed at."""
    if prices and symbol in prices:
        return _D(prices[symbol])
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


def holding_values(conn, account_id: int, as_of: Optional[str] = None,
                   prices: Optional[dict] = None) -> list[HoldingValue]:
    """Value each holding at its latest price (or an injected ``prices`` override,
    symbol -> price). Unpriced holdings get market_value 0 and gain None."""
    out: list[HoldingValue] = []
    for h in list_holdings(conn, account_id):
        sym = h["symbol"]
        qty = _D(h["quantity"])
        cost = h["cost_basis"] or 0
        price = _resolve_price(conn, sym, as_of, prices, account_id)
        if price is None:
            out.append(HoldingValue(sym, qty, cost, None, 0, None))
        else:
            mv = _cents(qty * price * _HUNDRED)
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
            market_value = _cents(pos.qty * price * _HUNDRED)
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
    excluded. As-of caps only the valuation price, never the replay -- so the
    open positions here match :func:`compute_holdings` / :func:`holding_values`."""
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
            out[sym] = _cents(pos.qty * price * _HUNDRED)
    return out


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
                      prices: Optional[dict] = None) -> AccountValuation:
    """Total value of an investment account: cash (ordinary ledger balance plus
    the investment-transaction cash flows) plus the market value of its holdings.
    ``prices`` (symbol -> price) overrides recorded prices for what-if / testing."""
    hvs = holding_values(conn, account_id, as_of, prices)
    securities = sum(hv.market_value for hv in hvs)
    cash = (ledger.account_balance(conn, account_id, as_of)
            + investment_cash(conn, account_id, as_of)
            - _duplicated_transfer_leg_total(conn, account_id, as_of))
    unpriced = [hv.symbol for hv in hvs if hv.price is None]
    return AccountValuation(
        account_id=account_id, cash=cash, securities=securities,
        total=cash + securities, holdings=hvs, unpriced=unpriced,
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
    """
    wanted: dict = {}
    for name, ticker in pairs:
        tick = (ticker or "").strip()
        if tick:
            wanted.setdefault(tick.upper(), []).append(name)
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
                # auto_adjust=False is LOAD-BEARING, not a default worth taking.
                # yfinance defaults it to True (1.7.0), which back-adjusts every
                # historical close for dividends AND splits -- a total-return
                # series. Every other price in this file is AS TRADED: the QIF
                # price, the transaction-carried price, the latest close. Mixing
                # the two scales does not merely look wrong, it IS wrong: a
                # high-yield holding came back at half its real 2021 price
                # (QYLD 11.67 against an as-traded 22.62) and an 8:1 split put
                # VGT's whole pre-split history at an eighth of what it traded
                # for, so the chart read as a flat line with the real prices
                # spiking out of it. Splits are already modelled here
                # (`split_ratio`), and a dividend is a transaction, not a price
                # revision -- so the provider must not adjust for either.
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
    syms: list[str] = []
    for s in symbols:
        s = (s or "").strip()
        if s and s not in syms:
            syms.append(s)
    if not syms:
        return []
    src = source or default_quote_source()
    quotes = src.get_quotes(syms)
    default_name = getattr(src, "source_name", None)
    lookup = {k.upper(): list(v) for k, v in (names or {}).items()}
    for q in quotes:
        targets = lookup.get((q.symbol or "").upper()) or [q.symbol]
        for target in targets:
            record_price(conn, target, q.date, q.close, q.source or default_name)
    return quotes

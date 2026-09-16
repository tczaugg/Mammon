"""A GENERIC reader for delimited crypto exports: infer the columns, infer the
structure, emit :class:`~mammon.importers.coinbase_csv.ExchangeRecord` rows.

The crypto twin of :mod:`mammon.importers.tabular` (which does the same job for
cash statements), and it exists for the same reason: every venue invents its own
layout, and writing a parser per venue does not scale past the first three. This
one is the FALLBACK -- an export whose signature matches a known reader
(Etherscan, Coinbase retail) still goes to that reader, because a proven parser
beats an inference every time. Everything else lands here.

Two things have to be inferred, and the second is what makes crypto harder than
cash.

**1. Which column plays which ROLE.** Header vocabulary first, then content
sniffing for anything left over, exactly as ``tabular`` does. The roles are the
ones a coin event needs: when, what type, which asset, how much of it, at what
price, for how much fiat, minus what fee, to or from whom.

**2. Whether a row is an EVENT or a LEG.** Etherscan and Coinbase's retail export
put one event per row. A Coinbase Pro account statement puts one ASSET SIDE per
row: a single sale is three rows -- the coin leg, the cash leg and the fee --
sharing a ``trade id``. Reading those as three events books a coin disposal, an
unrelated cash deposit and a mystery expense. The tell is structural and needs no
vendor knowledge: a grouping column exists, and rows sharing a value in it carry
DIFFERENT assets. One asset per group is just an id column; several is a trade
taken apart.

**A type name means different things in different exports, so the type name is
never the last word.** Coinbase's retail export calls a sale-and-cash-out
``Withdrawal``; Coinbase Pro calls a move to the retail account ``withdrawal``
too. The same word, opposite meanings. What separates them is on the row: the
retail one carries a FIAT total (dollars changed hands, so it was a sale), the
Pro one carries none (only coin moved, so it was a transfer). Direction always
comes from the sign; the type narrows it; the presence of a fiat leg decides
between a trade and a transfer. That rule is derived from the data and holds for
both files without either being named.

What this deliberately does NOT do is guess silently. When the roles it infers
leave a source unreadable it raises, naming what it could not find, so the caller
can report something a user can act on rather than importing plausible nonsense
-- the lesson ``tabular``'s column-map wizard records: a wrong column still
produces well-formed output, so values alone never reveal a bad mapping.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from mammon.importers.coinbase_csv import ExchangeRecord
from mammon.importers.record import stamp_time
from mammon.importers.tabular import (Frame, locate_and_frame, parse_date_flex,
                                      read_grid, _column_values, _pick)

# Fiat tickers a crypto export quotes against. A row whose ASSET is one of these
# moves money, not coin -- it opens no position and belongs to the cash sleeve.
FIAT = {"USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY", "NZD", "SEK", "NOK",
        "DKK", "PLN", "MXN", "BRL", "ZAR", "SGD", "HKD", "TRY", "INR"}

# ---------------------------------------------------------------------------
# Role vocabulary. Header names first, in priority order, as in `tabular`.
# ---------------------------------------------------------------------------
DATE_COLS = ("Timestamp", "Time", "Date", "DateTime (UTC)", "DateTime",
             "Created At", "created at", "UnixTimestamp")
TYPE_COLS = ("Transaction Type", "Type", "Side", "Activity", "Operation",
             "Category")
ASSET_COLS = ("Asset", "amount/balance unit", "Currency", "Coin", "Symbol",
              "Token", "size unit", "Base Currency", "Product")
QTY_COLS = ("Quantity Transacted", "Amount", "Size", "size", "Quantity",
            "amount", "Volume")
QTY_IN_COLS = ("Value_IN(ETH)", "Value_IN", "Received Amount", "Buy Amount",
               "Amount In")
QTY_OUT_COLS = ("Value_OUT(ETH)", "Value_OUT", "Sent Amount", "Sell Amount",
                "Amount Out")
PRICE_COLS = ("Price at Transaction", "Spot Price at Transaction", "Price",
              "price", "Historical $Price/Eth", "Unit Price")
GROSS_COLS = ("Total (inclusive of fees and/or spread)", "Total", "total",
              "Subtotal", "Proceeds", "Net Amount", "Value")
FEE_COLS = ("Fees and/or Spread", "Fee", "fee", "TxnFee(ETH)", "Commission",
            "Fees")
FEE_ASSET_COLS = ("Fee Currency", "fee unit", "Fee Asset")
FROM_COLS = ("Sender Address", "From", "from", "Source", "Sender")
TO_COLS = ("Recipient Address", "To", "to", "Destination", "Recipient")
GROUP_COLS = ("trade id", "Trade ID", "order id", "Order ID", "Group",
              "Transaction ID")
ID_COLS = ("ID", "Txhash", "Transaction Hash", "transfer id", "Transfer ID",
           "Reference", "Tx Hash")
BALANCE_COLS = ("balance", "Balance", "Running Balance")
NOTE_COLS = ("Notes", "Note", "Memo", "Description", "Comment")
CURRENCY_COLS = ("Price Currency", "price/fee/total unit", "Quote Currency")

_ROLE_ALIASES = {
    "date": DATE_COLS, "kind": TYPE_COLS, "asset": ASSET_COLS,
    "quantity": QTY_COLS, "quantity_in": QTY_IN_COLS,
    "quantity_out": QTY_OUT_COLS, "price": PRICE_COLS, "gross": GROSS_COLS,
    "fee": FEE_COLS, "fee_asset": FEE_ASSET_COLS, "from": FROM_COLS,
    "to": TO_COLS, "group": GROUP_COLS, "external_id": ID_COLS,
    "balance": BALANCE_COLS, "note": NOTE_COLS, "currency": CURRENCY_COLS,
}

# Order matters: an earlier role claims its column before a later one can. Money
# columns are the ambiguous ones (`tabular`'s recorded lesson -- a balance column
# parses as money exactly like an amount column), so the specific names are
# resolved before the generic ones.
_ROLE_ORDER = ("date", "kind", "asset", "quantity_in", "quantity_out",
               "quantity", "price", "fee_asset", "fee", "gross", "balance",
               "currency", "group", "external_id", "from", "to", "note")

# A ticker contains at least one LETTER. Without that, a column of block
# numbers or row ids passes as an asset -- and a sniffed "asset" of 1000001
# would book a holding of a coin named after a block.
_ASSET_RE = re.compile(r"^(?=[A-Z0-9]{2,10}$)[A-Z0-9]*[A-Z][A-Z0-9]*$")

# Words a venue puts in a type name when the row moves value to ANOTHER PLACE the
# same user holds -- a sub-ledger, a pro/advanced venue, a vault. Their presence
# is a statement about destination, so such a row is a transfer no matter what
# valuation the export prints beside it.
_VENUE_WORDS = ("pro", "exchange", "vault", "portfolio", "wallet", "advanced",
                "internal", "sub-account", "subaccount")


def _names_a_venue(kind: str) -> bool:
    return any(w in kind for w in _VENUE_WORDS)


@dataclass
class Layout:
    """What the reader worked out about a source: role -> column name, plus the
    structure. Returned alongside the records so a caller can SHOW the mapping --
    a wrong column produces plausible output, so the mapping has to be
    inspectable (``tabular``'s rule, restated for coin)."""

    roles: dict
    structure: str                 # "events" (one row each) | "legs" (grouped)
    header: list


def _dec(value) -> Optional[Decimal]:
    s = str(value or "").strip().replace(",", "").replace("$", "")
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1].strip()
    if not s or s in ("-", "."):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if neg else d


def _cents(value: Optional[Decimal]) -> int:
    from decimal import ROUND_HALF_UP
    if value is None:
        return 0
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _date_of(value) -> Optional[str]:
    """ISO date from a cell, tolerating what crypto exports actually contain.

    ``parse_date_flex`` handles the cash world's date columns but not a date with
    a TIME stapled to it (``2/15/2020 0:00``) nor a raw unix stamp
    (``1581724800``) -- and a block explorer ships both, side by side. Rather
    than widen the shared cash parser, whose behaviour a lot of statement imports
    depend on, this narrows the value first and then hands it over."""
    s = str(value or "").strip()
    if not s:
        return None
    got = parse_date_flex(s)
    if got:
        return got
    if s.upper().endswith(" UTC"):
        s = s[:-4].strip()
    if s.endswith("Z"):
        s = s[:-1]
    for candidate in (s.replace("T", " ").split(" ")[0],):
        got = parse_date_flex(candidate)
        if got:
            return got
    if s.isdigit() and 10 ** 9 <= int(s) <= 4 * 10 ** 9:      # unix seconds
        import datetime as _dt
        return _dt.datetime.fromtimestamp(
            int(s), _dt.timezone.utc).date().isoformat()
    return None


def _looks_asset(values) -> bool:
    """Whether a column reads as asset TICKERS: short, upper-case, few distinct
    values. Deliberately strict -- a memo column of short words would otherwise
    pass, and mistaking prose for a ticker invents a holding."""
    seen = [v for v in values if v]
    if not seen:
        return False
    hits = sum(1 for v in seen if _ASSET_RE.match(v))
    return hits >= max(1, int(0.9 * len(seen))) and len(set(seen)) <= 40


def _frame(text: str) -> Optional[Frame]:
    """Locate the header and data block.

    ``tabular.locate_and_frame`` is tried first -- it is the shared, exercised
    implementation. It finds the data block by looking for rows that PARSE AS
    DATES, which is right for a bank statement but leaves crypto exports out:
    a block explorer stamps ``2/15/2020 0:00`` beside a raw unix seconds column,
    and neither reads as a date to that scanner, so a real export framed as
    nothing at all.

    The fallback keys on what a crypto export DOES have reliably -- a header row
    full of role words (asset, quantity, type, fee...). Header vocabulary is the
    strong signal here, exactly as a dated column is for cash, so this is the same
    idea applied to a different shape rather than a weaker guess."""
    frame = locate_and_frame(text)
    if frame is not None and frame.rows:
        return frame
    grid = read_grid(text)
    if not grid:
        return None
    aliases = {a.strip().lower()
               for names in _ROLE_ALIASES.values() for a in names}
    best_i, best_hits = None, 0
    for i, rec in enumerate(grid[:20]):        # a header is never deep in a file
        cells = [str(c or "").strip().lower() for c in rec]
        hits = sum(1 for c in cells if c and c in aliases)
        if hits > best_hits:
            best_i, best_hits = i, hits
    if best_i is None or best_hits < 3:
        return None                            # not a table we can name columns in
    header = [str(c or "").strip() for c in grid[best_i]]
    rows = [dict(zip(header, rec)) for rec in grid[best_i + 1:]
            if any(str(c or "").strip() for c in rec)]
    return Frame(header=header, rows=rows, header_index=best_i,
                 preamble_rows=best_i, footer_rows=0, dropped_blank=0)


def infer_roles(frame: Frame) -> dict:
    """Map each role to a column of ``frame``, by header name then by content."""
    roles: dict = {}
    used: set = set()
    for role in _ROLE_ORDER:
        col = _pick([h for h in frame.header if h not in used],
                    _ROLE_ALIASES[role])
        if col is not None:
            roles[role] = col
            used.add(col)
    # Content fallbacks for the two roles a source cannot be read without.
    if "asset" not in roles:
        for h in frame.header:
            if h in used:
                continue
            if _looks_asset(_column_values(frame.rows, h)):
                roles["asset"] = h
                used.add(h)
                break
    if "date" not in roles:
        for h in frame.header:
            if h in used:
                continue
            vals = [v for v in _column_values(frame.rows, h) if v]
            if vals and all(_date_of(v) for v in vals[:8]):
                roles["date"] = h
                used.add(h)
                break
    return roles


def detect_structure(frame: Frame, roles: dict) -> str:
    """``"legs"`` when rows are asset SIDES grouped by a key, else ``"events"``.

    The test is structural, not a vendor check: a group whose rows carry
    DIFFERENT assets is one trade taken apart (coin leg, cash leg, fee). A group
    key whose rows all share one asset is just an identifier."""
    key = roles.get("group")
    asset = roles.get("asset")
    if not key or not asset:
        return "events"
    groups: dict = {}
    for r in frame.rows:
        g = str(r.get(key, "") or "").strip()
        if g:
            groups.setdefault(g, set()).add(str(r.get(asset, "") or "").strip())
    return "legs" if any(len(v) > 1 for v in groups.values()) else "events"


def _is_fiat(asset: str) -> bool:
    return (asset or "").strip().upper() in FIAT


def _action_for(kind: str, qty: Decimal, is_cash: bool, has_fiat_leg: bool) -> str:
    """The action for one event.

    Direction comes from the SIGN, always. The type narrows it. And where a type
    is used by different venues for opposite things -- ``withdrawal`` is a
    sale-and-cash-out in Coinbase's retail export and a move to the retail
    account in Coinbase Pro -- the presence of a FIAT leg decides: dollars
    changed hands, so it was a trade; no dollars, so only coin moved."""
    k = " ".join((kind or "").strip().lower().split())
    positive = qty > 0
    if is_cash:
        return "DEPOSIT" if positive else "WITHDRAW"
    if k in ("buy", "sell", "match", "advanced trade buy", "advanced trade sell",
             "trade", "conversion"):
        return "BUY" if positive else "SELL"
    if k in ("deposit", "withdrawal", "transfer") or _names_a_venue(k):
        # A type that NAMES A VENUE ("Pro Deposit", "Exchange Withdrawal") is a
        # move between the user's own places, whatever else is on the row.
        # Otherwise a fiat leg on a COIN row means the venue converted it: a bare
        # "Withdrawal" of ETH that carries dollars was a sale-and-cash-out.
        #
        # The fiat leg alone is NOT enough to decide, which cost a wrong reading:
        # Coinbase's retail export prints a USD valuation on EVERY row, transfers
        # included, so "has dollars on it" made 31 venue transfers read as sales.
        if not _names_a_venue(k) and has_fiat_leg:
            return "BUY" if positive else "SELL"
        return "TRANSFER_IN" if positive else "TRANSFER_OUT"
    if k in ("rewards income", "staking income", "inflation reward",
             "learning reward", "coinbase earn", "reward"):
        return "REWARD"
    if k == "interest":
        return "INTEREST"
    if k == "send":
        return "SEND"
    if k == "receive":
        return "RECEIVE"
    return "RECEIVE" if positive else "SEND"


def read_records(text: str) -> tuple[Layout, list[ExchangeRecord]]:
    """Read any delimited crypto export into records. Raises ``ValueError`` when
    the columns a coin event needs cannot be found."""
    frame = _frame(text)
    if frame is None or not frame.rows:
        # An export for a year with no activity is a header and nothing else.
        # That is an empty statement, not an unreadable one -- raising here made
        # a nine-file import die on the first quiet year.
        return Layout(roles={}, structure="events", header=[]), []
    roles = infer_roles(frame)
    header_asset = _asset_from_header(roles, frame.header)
    missing = [r for r in ("date",) if r not in roles]
    if "asset" not in roles and header_asset is None:
        missing.append("asset")
    if not any(k in roles for k in ("quantity", "quantity_in", "quantity_out")):
        missing.append("quantity")
    if missing:
        raise ValueError(
            "could not identify the %s column(s); header was: %s"
            % (", ".join(missing), ", ".join(map(str, frame.header))))
    structure = detect_structure(frame, roles)
    layout = Layout(roles=roles, structure=structure, header=list(frame.header))
    rows = (_read_legs(frame, roles) if structure == "legs"
            else [_read_event(r, roles, header_asset) for r in frame.rows])
    return layout, [r for r in rows if r is not None]


_HEADER_ASSET_RE = re.compile(r"[(\[]\s*([A-Za-z0-9]{2,10})\s*[)\]]")


def _asset_from_header(roles: dict, header) -> Optional[str]:
    """The asset ticker read out of a COLUMN NAME, when no column holds it.

    A block explorer's by-address export never names the coin in a cell -- the
    address is the account, so the coin is a property of the whole file and it
    appears only in the headers: ``Value_IN(ETH)``, ``Value_OUT(ETH)``,
    ``TxnFee(ETH)``. Content sniffing cannot find what is not in the content, so
    this is the one place the reader looks at a header for a VALUE rather than a
    role. Returns ``None`` when nothing in the in/out headers looks like a
    ticker, which is a genuine "cannot read this" rather than a guess."""
    for role in ("quantity_in", "quantity_out", "quantity", "fee"):
        col = roles.get(role)
        if not col:
            continue
        m = _HEADER_ASSET_RE.search(str(col))
        if m and _ASSET_RE.match(m.group(1).upper()):
            return m.group(1).upper()
    return None


def _qty_of(row, roles) -> Optional[Decimal]:
    """The signed quantity, from a single signed column or an in/out PAIR (the
    shape a block explorer uses: exactly one of the two is non-zero)."""
    if "quantity" in roles:
        return _dec(row.get(roles["quantity"]))
    got_in = _dec(row.get(roles.get("quantity_in"))) or Decimal(0)
    got_out = _dec(row.get(roles.get("quantity_out"))) or Decimal(0)
    if got_in > 0:
        return got_in
    if got_out > 0:
        return -got_out
    return None


def _read_event(row, roles, header_asset=None) -> Optional[ExchangeRecord]:
    qty = _qty_of(row, roles)
    if qty is None or qty == 0:
        return None                       # nothing moved: not a ledger event
    asset = (str(row.get(roles["asset"], "") or "").strip().upper()
             if "asset" in roles else (header_asset or ""))
    currency = str(row.get(roles.get("currency"), "") or "").strip().upper() or "USD"
    is_cash = _is_fiat(asset) and (asset == currency or "currency" not in roles)
    gross = _dec(row.get(roles.get("gross")))
    price = _dec(row.get(roles.get("price")))
    fee = _dec(row.get(roles.get("fee")))
    kind = str(row.get(roles.get("kind"), "") or "").strip()
    action = _action_for(kind, qty, is_cash, gross is not None and gross != 0)
    if is_cash:
        amount = _cents(qty)
    elif action in ("BUY", "SELL"):
        value = gross if gross is not None else (
            price * abs(qty) if price is not None else None)
        amount = abs(_cents(value))
        amount = -amount if action == "BUY" else amount
    else:
        amount = 0
    date = _date_of(row.get(roles["date"]))
    if not date:
        return None
    return ExchangeRecord(
        txn_id=str(row.get(roles.get("external_id"), "") or "").strip(),
        date=date, time=stamp_time(row.get(roles["date"])) or "", action=action,
        symbol="" if is_cash else asset,
        quantity=format(qty, "f"),
        price="" if (is_cash or price is None) else format(price, "f"),
        currency=currency, amount_cents=amount, fee_cents=abs(_cents(fee)),
        payee=str(row.get(roles.get("from") if qty > 0 else roles.get("to"),
                          "") or "").strip(),
        memo=str(row.get(roles.get("note"), "") or "").strip(),
        raw_type=kind,
    )


def _read_legs(frame: Frame, roles: dict) -> list[ExchangeRecord]:
    """Fold asset SIDES back into events.

    A group is one trade: the non-fiat leg says which coin moved and how much,
    the fiat leg says for how much money, and any fee leg says what it cost.
    Reading them as three independent rows books a disposal, an unrelated
    deposit and a mystery expense -- three wrong entries from one right trade.
    Rows carrying no group key are ordinary single events (a deposit, a
    withdrawal) and go through the event path unchanged."""
    key, asset_col = roles["group"], roles["asset"]
    groups: dict = {}
    singles = []
    for r in frame.rows:
        g = str(r.get(key, "") or "").strip()
        (groups.setdefault(g, []) if g else singles).append(r)

    out = [rec for r in singles if (rec := _read_event(r, roles)) is not None]
    for gid, legs in groups.items():
        coin = fiat = None
        fee_total = Decimal(0)
        kind = ""
        for leg in legs:
            a = str(leg.get(asset_col, "") or "").strip().upper()
            amt = _qty_of(leg, roles) or Decimal(0)
            k = str(leg.get(roles.get("kind"), "") or "").strip().lower()
            if k == "fee" or "fee" in k:
                fee_total += abs(amt)
                continue
            kind = kind or k
            if _is_fiat(a):
                fiat = (a, amt)
            else:
                coin = (a, amt)
        if coin is None:
            # A group with no coin leg is not a trade; fall back to reading its
            # rows individually rather than silently dropping them.
            out.extend(rec for leg in legs
                       if (rec := _read_event(leg, roles)) is not None)
            continue
        symbol, qty = coin
        gross = fiat[1] if fiat else None
        action = _action_for(kind, qty, False, gross is not None)
        amount = 0
        if action in ("BUY", "SELL") and gross is not None:
            # The cash that actually moved is NET of the fee. In a leg-structured
            # export the fee is its OWN ROW, so the trade's cash leg is the GROSS
            # and the fee has to be taken off it; a venue that reports one Total
            # column beside a Subtotal has already done that subtraction, which is
            # why the event path must not do it twice. Subtracting works in both
            # directions: a sale's proceeds shrink, a purchase's cost grows.
            amount = _cents(gross) - (_cents(fee_total)
                                      if _is_fiat(fiat[0] if fiat else "") else 0)
        first = legs[0]
        date = _date_of(first.get(roles["date"]))
        if not date:
            continue
        price = None
        if gross is not None and qty != 0:
            price = abs(gross) / abs(qty)
        out.append(ExchangeRecord(
            txn_id=gid, date=date, time=stamp_time(first.get(roles["date"])) or "",
            action=action, symbol=symbol,
            quantity=format(qty, "f"),
            price=format(price, "f") if price is not None else "",
            currency=(fiat[0] if fiat else "USD"),
            amount_cents=amount, fee_cents=_cents(fee_total),
            payee="", memo=str(first.get(roles.get("note"), "") or "").strip(),
            raw_type=kind or "trade",
        ))
    out.sort(key=lambda r: (r.date, r.time, r.txn_id))
    return out

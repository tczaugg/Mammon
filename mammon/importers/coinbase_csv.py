"""Parse a Coinbase transaction-history CSV into :class:`ExchangeRecord` rows.

The EXCHANGE-side twin of :mod:`mammon.importers.crypto_csv` (which reads a
block-explorer by-address export for a wallet). Same contract, and the same
reason for it: this is a pure ``text -> list[ExchangeRecord]`` function with NO
database access and no knowledge of accounts, dedup or the crypto writers. All
DB-facing work lives in :mod:`mammon.importers.crypto_core`.

Export shape -- three preamble lines, THEN the header::

    <blank>
    Transactions
    User,<name>,<account uuid>
    ID,Timestamp,Transaction Type,Asset,Quantity Transacted,Price Currency,
    Price at Transaction,Subtotal,Total (inclusive of fees and/or spread),
    Fees and/or Spread,Notes,Sender Address,Recipient Address

Four things about this file are load-bearing and easy to get wrong:

- **The header is NOT the first line.** Coinbase puts a blank line, a title and a
  USER line (the account holder's real name) above it. A reader that assumes row
  0 is the header reads "Transactions" as a one-column layout and finds nothing.
  The header is located by its ``Transaction Type``/``Asset`` columns, the way
  the Etherscan reader locates its own by ``Transaction Hash``.

- **The SIGN lives on ``Quantity Transacted``, not in the type name.** The same
  ``Withdrawal`` type appears as coin leaving AND as dollars leaving, and
  ``Exchange Withdrawal`` is money ARRIVING (withdrawn from the exchange venue
  INTO this account). Deriving direction from the word inverts those. The sign is
  authoritative; the type only chooses WHICH action of that direction.

- **A row can be pure FIAT.** When ``Asset`` equals ``Price Currency`` the row
  moves dollars, not coin -- a deposit or withdrawal against the exchange's cash
  sleeve, with no position to open or relieve. A reader that assumes every row
  carries a coin books a phantom "USD" holding.

- **The type vocabulary is open and Coinbase keeps extending it** (Pro Deposit,
  Exchange Withdrawal, Advanced Trade Buy, Learning Reward, Inflation Reward...).
  An unknown type is therefore NOT an error: it falls back to the direction the
  sign already fixed, and the raw type rides ``raw_type`` so the review row can
  show what the file actually said and the user can correct the action before
  accepting. Refusing a file over one unrecognised word would make every future
  Coinbase product a broken import.

Synthetic-fixture note: a real export carries the account holder's NAME in its
preamble and bank names in ``Notes``. Fixtures under ``mammon/tests/fixtures/``
are synthetic ANON stand-ins; no real name, address or account number is copied
into the repo.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

# The actions this parser may emit. All are already in ``crypto.ACTIONS``
# (BUY/SELL/RECEIVE/SEND/TRANSFER_IN/TRANSFER_OUT/REWARD/INTEREST) or in the
# cash-sleeve pair (DEPOSIT/WITHDRAW). Kept as plain strings so the parser stays
# free of any domain import.
_COIN_IN = "RECEIVE"
_COIN_OUT = "SEND"
_CASH_IN = "DEPOSIT"
_CASH_OUT = "WITHDRAW"


@dataclass
class ExchangeRecord:
    """One custodial-exchange event, before it hits the ledger.

    Quantities and per-unit prices are Decimal-encoded TEXT (exact at any decimal
    count); fiat is signed integer cents, the repo-wide convention. ``quantity``
    keeps its SIGN, because on this export the sign is the only trustworthy
    statement of direction.
    """

    txn_id: str = ""          # Coinbase's own row id -- the exact-dedup key
    date: str = ""            # ISO YYYY-MM-DD
    action: str = ""          # BUY|SELL|RECEIVE|SEND|TRANSFER_IN|TRANSFER_OUT|
                              # REWARD|INTEREST|DEPOSIT|WITHDRAW
    symbol: str = ""          # asset ticker; "" on a pure fiat row
    quantity: str = ""        # Decimal text, SIGNED (negative = out)
    price: str = ""           # Decimal text, per-unit price in `currency`
    currency: str = "USD"     # the fiat the prices are quoted in
    amount_cents: int = 0     # signed fiat effect on the cash sleeve; 0 if none
    fee_cents: int = 0        # fee/spread, unsigned
    payee: str = ""           # counterparty (sender on an in, recipient on an out)
    memo: str = ""            # the export's own Notes
    raw_type: str = ""        # the export's Transaction Type, verbatim

    @property
    def tx_hash(self) -> str:
        """The exact-dedup key, under the name the crypto import path already
        uses for one. Coinbase's row ``ID`` plays exactly the role an on-chain
        hash plays for a wallet -- globally unique and immutable -- so the two
        record types duck-type together for dedup and counting."""
        return self.txn_id

    @property
    def is_success(self) -> bool:
        """Every exported row is a completed event; there is no failed-txn
        concept here as there is on-chain. Present so the shared import helpers
        can ask both record types the same question."""
        return True

    @property
    def is_cash(self) -> bool:
        """A pure fiat movement: dollars in or out of the cash sleeve, no coin."""
        return not self.symbol


# ---------------------------------------------------------------------------
# Type vocabulary
# ---------------------------------------------------------------------------
# Coinbase's Transaction Type -> the action for each DIRECTION, as
# (action when the quantity is positive, action when it is negative). ``None``
# means "no special mapping: use the direction default". Matched on a normalised
# (lower-cased, single-spaced) name.
_TYPE_ACTIONS: dict[str, tuple[Optional[str], Optional[str]]] = {
    "buy": ("BUY", None),
    "advanced trade buy": ("BUY", None),
    "sell": (None, "SELL"),
    "advanced trade sell": (None, "SELL"),
    # A COIN "Withdrawal" is a sale plus a cash-out, not a coin send. Coinbase
    # converts and wires the dollars: the row carries a Subtotal, a Total and a
    # fee, has NO recipient address, and its note names a bank. Their own later
    # exports emit the same event as two rows (`Sell` ETH, then `Withdrawal`
    # USD); the older vintage collapses it into one. A genuine coin move off the
    # platform is type `Send`, and it carries a recipient address instead.
    # The is_cash guard below sends a FIAT withdrawal to WITHDRAW, so only the
    # coin case reaches SELL.
    "withdrawal": (None, "SELL"),
    # Coin moved between the user's own Coinbase venues (retail <-> Pro/Exchange).
    # A basis-preserving TRANSFER, never a disposal: booking these as SEND would
    # realize a gain on coin that never left the user's control.
    "pro deposit": ("TRANSFER_IN", "TRANSFER_OUT"),
    "pro withdrawal": ("TRANSFER_IN", "TRANSFER_OUT"),
    "exchange deposit": ("TRANSFER_IN", "TRANSFER_OUT"),
    "exchange withdrawal": ("TRANSFER_IN", "TRANSFER_OUT"),
    # In-kind income: credited as coin at fair market value.
    "rewards income": ("REWARD", None),
    "staking income": ("REWARD", None),
    "inflation reward": ("REWARD", None),
    "learning reward": ("REWARD", None),
    "coinbase earn": ("REWARD", None),
    "interest": ("INTEREST", None),
}


def _norm_type(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _dec(value: str) -> Optional[Decimal]:
    """Tolerant Decimal parse of a cell. Strips the currency symbol, thousands
    separators and a parenthesised negative; blank/garbage -> ``None``."""
    s = (value or "").strip().replace(",", "").replace("$", "")
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
    """Decimal dollars -> signed integer cents, HALF_UP (the repo convention)."""
    if value is None:
        return 0
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _qty_text(d: Optional[Decimal]) -> str:
    """Exponent-free Decimal text, so a wei-scale quantity keeps every digit."""
    if d is None:
        return ""
    return format(d, "f")


def _iso_date(stamp: str) -> str:
    """The event date as ISO ``YYYY-MM-DD``. Coinbase stamps UTC, in a couple of
    shapes across export vintages."""
    s = (stamp or "").strip()
    if s.upper().endswith(" UTC"):
        s = s[:-4].strip()
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"cannot parse a Coinbase timestamp from {stamp!r}")


def _find_col(header: list[str], *predicates) -> Optional[int]:
    """First column index whose normalised name satisfies any predicate."""
    for pred in predicates:
        for i, name in enumerate(header):
            if pred(" ".join((name or "").strip().lower().split())):
                return i
    return None


def _locate_header(rows: list[list[str]]) -> tuple[Optional[list[str]], int]:
    """The header row, and the index of the row after it.

    Found by CONTENT, never by position: the export opens with a blank line, a
    'Transactions' title and a User line carrying the account holder's name."""
    for i, row in enumerate(rows):
        names = {" ".join((c or "").strip().lower().split()) for c in row}
        if "transaction type" in names and "asset" in names:
            return row, i + 1
    return None, 0


def looks_like_coinbase(text: str) -> bool:
    """A cheap signature test: does ``text`` look like a Coinbase transaction
    history? Used to route a CSV by its CONTENT rather than by the account it
    happens to be imported into."""
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return False
    header, _ = _locate_header(rows)
    if header is None:
        return False
    return _find_col(header, lambda n: n.startswith("quantity")) is not None


def parse_coinbase(text: str, default_account: Optional[str] = None
                   ) -> list[ExchangeRecord]:
    """Turn a Coinbase transaction-history CSV into :class:`ExchangeRecord` rows.

    Pure: no DB, no account resolution, no dedup. ``default_account`` is accepted
    for signature-parity with the other parsers and is unused (a crypto import
    always targets an explicit account chosen by the caller).

    A row that moves nothing (no quantity) is dropped -- there is no delta to
    record. An unknown Transaction Type is NOT an error (see the module
    docstring).
    """
    rows = list(csv.reader(io.StringIO(text)))
    header, start = _locate_header(rows)
    if header is None:
        raise ValueError(
            "no Coinbase header row (no 'Transaction Type' column) found")

    i_id = _find_col(header, lambda n: n == "id",
                     lambda n: n == "transaction id")
    i_when = _find_col(header, lambda n: n in ("timestamp", "time", "date"))
    i_type = _find_col(header, lambda n: n == "transaction type")
    i_asset = _find_col(header, lambda n: n == "asset")
    i_qty = _find_col(header, lambda n: n.startswith("quantity"))
    i_ccy = _find_col(header, lambda n: n == "price currency")
    i_price = _find_col(header, lambda n: n.startswith("price at"),
                        lambda n: n.startswith("spot price"))
    i_sub = _find_col(header, lambda n: n.startswith("subtotal"))
    i_total = _find_col(header, lambda n: n.startswith("total"))
    i_fee = _find_col(header, lambda n: n.startswith("fees"))
    i_notes = _find_col(header, lambda n: n == "notes")
    i_from = _find_col(header, lambda n: n == "sender address")
    i_to = _find_col(header, lambda n: n == "recipient address")

    missing = [name for name, idx in (
        ("Transaction Type", i_type), ("Asset", i_asset),
        ("Quantity Transacted", i_qty), ("Timestamp", i_when),
    ) if idx is None]
    if missing:
        raise ValueError(
            f"Coinbase CSV missing required column(s): {', '.join(missing)}")

    def cell(row, idx):
        if idx is None or idx >= len(row):
            return ""
        return (row[idx] or "").strip()

    out: list[ExchangeRecord] = []
    for row in rows[start:]:
        if not row or not any((c or "").strip() for c in row):
            continue
        qty = _dec(cell(row, i_qty))
        if qty is None or qty == 0:
            continue                      # nothing moved: not a ledger event
        asset = cell(row, i_asset).upper()
        currency = (cell(row, i_ccy) or "USD").upper()
        # A row whose ASSET IS the pricing currency moves dollars, not coin.
        is_cash = bool(asset) and asset == currency
        raw_type = cell(row, i_type)
        action = _action_for(raw_type, qty, is_cash)

        price = _dec(cell(row, i_price))
        total = _dec(cell(row, i_total))
        subtotal = _dec(cell(row, i_sub))
        fee = _dec(cell(row, i_fee))

        if is_cash:
            # The dollars themselves. `Total` restates the same figure; the
            # QUANTITY is the money, and it already carries the sign.
            amount = _cents(qty)
        elif action in ("BUY", "SELL"):
            # A trade really does move the cash sleeve. Prefer the TOTAL (what
            # the account was actually debited or credited, fees included); fall
            # back to the subtotal, then to price x quantity.
            gross = total if total is not None else subtotal
            if gross is None and price is not None:
                gross = price * abs(qty)
            amount = abs(_cents(gross))
            amount = -amount if action == "BUY" else amount
        else:
            # Coin in or out with NO fiat leg. Coinbase still prints a USD figure
            # on these rows for tax purposes; it is a VALUATION, not a movement,
            # and booking it would invent cash the account never saw.
            amount = 0

        out.append(ExchangeRecord(
            txn_id=cell(row, i_id),
            date=_iso_date(cell(row, i_when)),
            action=action,
            symbol="" if is_cash else asset,
            quantity=_qty_text(qty),
            price="" if is_cash else _qty_text(price),
            currency=currency,
            amount_cents=amount,
            fee_cents=abs(_cents(fee)),
            payee=cell(row, i_from) if qty > 0 else cell(row, i_to),
            memo=cell(row, i_notes),
            raw_type=raw_type,
        ))
    return out


def _action_for(raw_type: str, qty: Decimal, is_cash: bool) -> str:
    """The crypto action for one row: the DIRECTION comes from the sign, and the
    type only chooses which action of that direction. An unrecognised type falls
    back to the plain move -- Coinbase keeps adding product names, and refusing
    the file over one unknown word would break every future export."""
    positive = qty > 0
    mapped = _TYPE_ACTIONS.get(_norm_type(raw_type))
    if mapped is not None:
        act = mapped[0] if positive else mapped[1]
        if act is not None:
            # A fiat row can never take a coin action: an 'Exchange Withdrawal'
            # of DOLLARS is a cash deposit, not a coin TRANSFER_IN.
            if is_cash and act not in (_CASH_IN, _CASH_OUT):
                return _CASH_IN if positive else _CASH_OUT
            return act
    if is_cash:
        return _CASH_IN if positive else _CASH_OUT
    return _COIN_IN if positive else _COIN_OUT

"""Pure parser for a *holdings / balance snapshot* CSV -- what a plan or broker
prints as "your positions as of <date>", NOT a transaction export.

WHY this is its own parser and its own record type:

- **A snapshot is a statement of fact, not a list of events.** Every other
  importer in this package produces rows that BECOME transactions. A snapshot
  row is "you hold 123.456 shares of ANON BALANCED FUND, priced 24.19, worth
  2,987.40, as of 2026-03-31". Turning that into a transaction would invent
  history that never happened, so this file's product deliberately cannot reach
  ``importers/core.py``'s insert path at all: it feeds a SHARE RECONCILIATION's
  stated ending quantities (SRD 5.11b) and, for a fund with no ticker, the only
  price anyone will ever have (``price_history``).

- **The user's stated gap.** Transactions and balances arrive in different
  files: "I can manually download quotes and balances to a CSV file, but not the
  same file as transactions." A 401(k)'s internal funds have no ticker, so no
  quote source exists (``ticker_of`` returns "" -- Compartment D) and the
  statement's share count and unit price are the ONLY truth available.

- **The hourglass discipline is preserved.** This module is a pure
  ``text -> list[HoldingSnapshot]`` function with NO database access: it does
  not know what an account is, cannot match a fund to a security and never
  writes a price. All DB-facing work (matching by symbol, then by NAME, through
  ``securities`` / ``security_aliases``; writing ``price_history``; seeding the
  reconcile drafts) lives in :func:`mammon.investments.apply_holdings_snapshot`,
  because ``mammon/investments.py`` is the only writer on that side.

Shape read (columns located BY NAME, never by position, so an extra trailing
column cannot shift a share count into a price)::

    Fund, Symbol, Shares, Price, Market Value, As of Date

Every one of those has several real-world spellings (Description / Security /
Investment; Ticker; Units / Quantity; NAV / Unit Price / Share Price; Value /
Current Value / Balance / Ending Value), and a plan export usually has **no
Symbol column at all** -- which is the whole reason matching falls back to the
fund NAME. A preamble line before the header ("Balances as of 03/31/2026") is
read for a file-level as-of date when no per-row date column exists.

Numbers are kept as Decimal-encoded TEXT, exactly as they were printed, and
never become floats: share quantities and per-share prices are Decimal TEXT
everywhere in this codebase (SRD 5.8). Thousands separators, currency symbols
and parenthesised negatives are stripped; nothing is rounded here.

Synthetic-fixture note: a real snapshot is PII (plan name, account number,
balances). This parser is exercised only against synthetic ANON fixtures under
``mammon/tests/fixtures/`` -- never a copied-in real statement.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional


@dataclass
class HoldingSnapshot:
    """One position line as printed on a statement, before it touches the DB.

    ``symbol`` is often EMPTY (an untickered plan fund); ``name`` is then the
    only identity the file carries, and the DB-facing side matches on it. All
    numeric fields are Decimal-encoded TEXT ("" when the column was absent or
    unparseable) so no float error can enter share math.
    """

    name: str = ""            # fund/security name as printed -- the fallback key
    symbol: str = ""          # ticker when the file has one; often ""
    quantity: str = ""        # share count, Decimal text
    price: str = ""           # per-share price / NAV, Decimal text
    market_value: str = ""    # position value, Decimal text
    date: str = ""            # ISO YYYY-MM-DD, as-of date of the snapshot
    raw: dict = field(default_factory=dict)   # the source row, for diagnostics

    @property
    def key(self) -> str:
        """What identifies this line to a human: the ticker if there is one,
        else the fund name."""
        return (self.symbol or self.name or "").strip()


# --- column vocabularies ---------------------------------------------------
# Each entry is matched against the lower-cased, punctuation-stripped header
# cell. Order matters only in that the FIRST matching column wins, so the more
# specific spellings are listed first.
_NAME_COLS = ("fundname", "securityname", "investmentname", "fund", "security",
              "investment", "description", "name", "holding", "asset")
_SYMBOL_COLS = ("symbol", "ticker", "tickersymbol", "cusip")
_QTY_COLS = ("shares", "quantity", "units", "sharebalance", "endingshares",
             "numberofshares", "shareunits", "balanceshares")
_PRICE_COLS = ("price", "shareprice", "unitprice", "nav", "navpershare",
               "closeprice", "priceper share", "endingprice", "unitvalue")
_VALUE_COLS = ("marketvalue", "currentvalue", "endingvalue", "endingbalance",
               "totalvalue", "value", "balance", "amount")
_DATE_COLS = ("asofdate", "asof", "date", "statementdate", "valuationdate",
              "pricedate", "balancedate")

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})|(\d{1,2}/\d{1,2}/\d{2,4})")


def _norm(cell) -> str:
    """A header cell reduced to comparable letters: lower-cased with spaces,
    punctuation and currency noise removed, so "Market Value ($)" and
    "market_value" are the same column."""
    return re.sub(r"[^a-z0-9]", "", str(cell or "").lower())


def _find_col(header: list, names) -> Optional[int]:
    """Index of the first header cell that IS one of ``names`` (exact after
    normalisation), else the first that CONTAINS one. Exact-first keeps a
    "Price" column from losing to "Price Date" and vice versa."""
    norms = [_norm(h) for h in header]
    for want in names:
        for i, n in enumerate(norms):
            if n == want:
                return i
    for want in names:
        for i, n in enumerate(norms):
            if n and want in n:
                return i
    return None


def _dec_text(value) -> str:
    """A printed number as exponent-free Decimal TEXT, or "" when it is not a
    number. Strips currency symbols, thousands separators and percent signs, and
    reads a parenthesised figure as negative (statements print "(12.34)")."""
    s = str(value or "").strip()
    if not s:
        return ""
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    s = re.sub(r"[,$%\s ]", "", s)
    if s in ("", "-", "--"):
        return ""
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return ""
    if neg:
        d = -d
    # format(d, 'f') is the exponent-free spelling used everywhere in the store.
    return format(d, "f")


def _iso(value) -> str:
    """A printed date as ISO ``YYYY-MM-DD``; "" when there is no date in it.
    Storage, the domain layer and this importer all speak ISO (SRD compartment
    M) -- a display format is never written."""
    s = str(value or "").strip()
    if not s:
        return ""
    m = _DATE_RE.search(s)
    if not m:
        return ""
    text = m.group(0)
    if "-" in text:
        try:
            return _dt.date.fromisoformat(text).isoformat()
        except ValueError:
            return ""
    mm, dd, yy = text.split("/")
    year = int(yy)
    if year < 100:                       # 2-digit years: 70..99 -> 19xx
        year += 2000 if year < 70 else 1900
    try:
        return _dt.date(year, int(mm), int(dd)).isoformat()
    except ValueError:
        return ""


def _header_score(row: list) -> int:
    """How much this row looks like the header of a holdings snapshot: it needs
    an identity column (name or symbol) AND a share-count column. Scoring rather
    than matching the first non-empty row lets a title/preamble block sit above
    the table, which plan exports habitually print."""
    if not row:
        return 0
    ident = (_find_col(row, _NAME_COLS) is not None
             or _find_col(row, _SYMBOL_COLS) is not None)
    qty = _find_col(row, _QTY_COLS) is not None
    if not (ident and qty):
        return 0
    score = 2
    for group in (_PRICE_COLS, _VALUE_COLS, _DATE_COLS):
        if _find_col(row, group) is not None:
            score += 1
    return score


def looks_like_holdings_csv(text: str) -> bool:
    """True when ``text`` is a holdings/balance snapshot rather than a
    transaction export. Used by the import chooser so a snapshot can never be
    fed to the transaction path (it would invent history)."""
    return _locate_header(text)[0] is not None


def _locate_header(text: str):
    """``(header_row, index, preamble_rows)`` for the best header candidate in
    the first 25 rows, or ``(None, -1, rows)``."""
    rows = list(csv.reader(io.StringIO(text)))
    best, best_i, best_score = None, -1, 0
    for i, row in enumerate(rows[:25]):
        score = _header_score(row)
        if score > best_score:
            best, best_i, best_score = row, i, score
    return best, best_i, rows


def _preamble_date(rows, upto: int) -> str:
    """The as-of date printed above the table ("Balances as of 03/31/2026"),
    or "". The LAST such date wins: preambles print the plan name first and the
    statement date closest to the table."""
    found = ""
    for row in rows[:max(upto, 0)]:
        for cell in row:
            iso = _iso(cell)
            if iso:
                found = iso
    return found


def parse_holdings_csv(text: str, as_of: Optional[str] = None,
                       default_account: Optional[str] = None
                       ) -> list[HoldingSnapshot]:
    """Turn a holdings/balance snapshot CSV into :class:`HoldingSnapshot` rows.

    Pure: no DB, no account resolution, no security matching, no dedup.
    ``as_of`` is the caller's fallback date, used only when the file carries
    neither a date column nor a dated preamble line. ``default_account`` is
    accepted for signature parity with the other parsers and is unused -- a
    snapshot is always applied to an explicitly chosen account.

    A row with no identity (neither name nor symbol) or no parseable share
    count is dropped: statement tables end in "Total" lines, blank separators
    and footnotes, none of which are positions.
    """
    header, idx, rows = _locate_header(text)
    if header is None:
        return []
    c_name = _find_col(header, _NAME_COLS)
    c_sym = _find_col(header, _SYMBOL_COLS)
    c_qty = _find_col(header, _QTY_COLS)
    c_price = _find_col(header, _PRICE_COLS)
    c_val = _find_col(header, _VALUE_COLS)
    c_date = _find_col(header, _DATE_COLS)
    # A single column cannot be two things: when the same index answers for both
    # price and value (e.g. only "Value" exists), leave price empty.
    if c_price is not None and c_price == c_val:
        c_price = None
    if c_date is not None and c_date in (c_qty, c_price, c_val):
        c_date = None
    file_date = _iso(as_of) or _preamble_date(rows, idx)

    out: list[HoldingSnapshot] = []
    for row in rows[idx + 1:]:
        if not row or not any(str(c).strip() for c in row):
            continue

        def cell(i):
            return row[i] if i is not None and i < len(row) else ""

        name = str(cell(c_name) or "").strip()
        symbol = str(cell(c_sym) or "").strip()
        if not name and not symbol:
            continue
        low = _norm(name)
        if low.startswith("total") or low.startswith("grandtotal"):
            continue
        qty = _dec_text(cell(c_qty))
        if qty == "":
            continue
        out.append(HoldingSnapshot(
            name=name,
            symbol=symbol,
            quantity=qty,
            price=_dec_text(cell(c_price)),
            market_value=_dec_text(cell(c_val)),
            date=_iso(cell(c_date)) or file_date,
            raw={str(h): (row[i] if i < len(row) else "")
                 for i, h in enumerate(header)},
        ))
    return out


def parse_holdings_file(path, as_of: Optional[str] = None
                        ) -> list[HoldingSnapshot]:
    """:func:`parse_holdings_csv` over a file path (UTF-8, BOM tolerated)."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return parse_holdings_csv(fh.read(), as_of=as_of)

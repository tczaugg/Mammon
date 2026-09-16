"""What an investment import could not settle on its own -- read-only (SRD 6.2a).

A fresh import of someone's Quicken history is checked against Quicken's own
balances once, by whoever builds the importer. Everyone after that imports their
OWN history with nobody to compare it for them, so what the import cannot know
has to be put in front of the person who can. Each finding names an account, a
security and a date, and says what to look at; nothing here changes a row.

Four shapes, each one seen in a real 27-year Quicken export:

``removed_unheld``
    Shares removed that the account did not hold, leaving a negative position
    Quicken does not show. Quicken exports a plan fund under two spellings
    across the years, and the removals of one land on a security that never
    received the purchases; or the arrival that preceded a removal was
    exported with no share count. A classified option is exempt -- selling a
    contract you do not hold is how one is written.
``zero_basis_arrival``
    Shares that arrived with no price and no amount (``ShrsIn`` and the like),
    so they carry a cost basis of zero. That is how Quicken exports the new
    shares of a merger or spin-off; every later sale then reports the whole
    proceeds as gain until the basis is entered.
``stale_price``
    A position still held in an open account whose newest price is more than
    ``stale_days`` older than the newest price anywhere in the ledger, or that
    has no price at all. Quicken does not export every valuation it holds, so a
    holding can be valued at a price years old while everything around it is
    current; it values correctly once its prices are fetched or entered.
``option_units``
    A classified option whose own trades do not settle what quantity x price
    means in cash (:func:`mammon.securities.observed_multiplier`), so its value
    rests on the multiplier the symbol states. A person's own classification is
    not reported: they already decided.

One pass per account over its rows in date order, grouped by security identity
the same way the share reconciliation groups them, so an alias never reports a
removal its canonical spelling actually held.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, List, Optional

from mammon import investments, securities

REMOVED_UNHELD = "removed_unheld"
ZERO_BASIS_ARRIVAL = "zero_basis_arrival"
STALE_PRICE = "stale_price"
OPTION_UNITS = "option_units"

#: Report order, most damaging to a balance first.
KINDS = (REMOVED_UNHELD, ZERO_BASIS_ARRIVAL, STALE_PRICE, OPTION_UNITS)

_HEADINGS = {
    REMOVED_UNHELD: "Shares removed that were not held",
    ZERO_BASIS_ARRIVAL: "Shares that arrived with no cost basis",
    STALE_PRICE: "Holdings with no recent price",
    OPTION_UNITS: "Options whose contract size the trades do not settle",
}

#: Arrivals that state a cost of their own. A reinvestment always carries its
#: amount, and a purchase's cost is its cash, so only a share transfer IN can
#: arrive with nothing behind it.
_TRANSFER_IN_ACTIONS = {"shrsin", "addshares", "xin"}


@dataclass(frozen=True)
class ImportFinding:
    kind: str
    account_id: int
    account: str
    symbol: str
    date: Optional[str]
    detail: str


def _key(action) -> str:
    return (action or "").strip().lower().replace(" ", "")


def _zero(value) -> bool:
    return value is None or investments._D(value) == 0


def audit(conn, account_ids: Iterable[int], *, as_of: Optional[str] = None,
          stale_days: int = 90) -> List[ImportFinding]:
    """Findings for ``account_ids``, ordered by kind, account, symbol, date."""
    as_of = as_of or _dt.date.today().isoformat()
    ids = sorted({int(a) for a in account_ids or () if a is not None})
    out: List[ImportFinding] = []
    identity: dict = {}

    def canon(symbol):
        if symbol not in identity:
            spellings = investments._kind_identity_symbols(conn, symbol)
            identity[symbol] = str(spellings[0] if spellings else symbol).strip()
        return identity[symbol]

    # Staleness is measured from the newest price the ledger holds, not from
    # today: an export's price list ends when the export was made, and every
    # holding is equally old against the calendar. What matters is a holding the
    # source STOPPED pricing while it went on pricing the rest.
    newest = conn.execute("SELECT MAX(date) FROM price_history WHERE date<=?",
                          (as_of,)).fetchone()[0] or as_of
    cutoff = (_dt.date.fromisoformat(newest) - _dt.timedelta(days=stale_days)).isoformat()

    options_traded: dict = {}
    for aid in ids:
        acct = conn.execute("SELECT name, closed_flag FROM accounts WHERE id=?",
                            (aid,)).fetchone()
        if acct is None:
            continue
        name = acct["name"]
        qty: dict = {}
        negative: dict = {}
        day, opened, removed_today = None, {}, {}

        def close_day():
            """Judge a day's removals by where the day ENDS: rows within one date
            are in no meaningful order, and a sale recorded ahead of that day's
            arrival is not a shortfall. A position that later comes back to zero
            or above drops its record, so a short entered as Sell-then-Buy is
            never reported; what remains names the removal that last took it
            below zero."""
            for ident, held in opened.items():
                now = qty.get(ident, Decimal(0))
                if now >= 0:
                    negative.pop(ident, None)
                elif held >= 0 and ident in removed_today:
                    symbol, removed = removed_today[ident]
                    negative[ident] = (symbol, day, held, removed)
            opened.clear()
            removed_today.clear()

        for t in conn.execute("SELECT * FROM investment_transactions WHERE account_id=? "
                              "AND symbol IS NOT NULL ORDER BY date, id", (aid,)):
            if investments.is_void_investment(t):
                continue
            if t["date"] != day:
                close_day()
                day = t["date"]
            symbol = str(t["symbol"]).strip()
            if not symbol:
                continue
            action = _key(t["action"])
            option = investments.is_option(conn, symbol)
            if option:
                options_traded.setdefault(symbol, (aid, name))
            if (action in _TRANSFER_IN_ACTIONS and not _zero(t["quantity"])
                    and _zero(t["price"]) and _zero(t["amount"])):
                out.append(ImportFinding(
                    ZERO_BASIS_ARRIVAL, aid, name, symbol, t["date"],
                    f"{t['action']} of {investments._D(t['quantity'])} shares with "
                    f"no price or amount: their cost basis is zero until one is entered."))
            if not investments.is_quantity_action(t["action"]):
                continue
            ident = canon(symbol)
            held = qty.get(ident, Decimal(0))
            opened.setdefault(ident, held)
            if action in investments._SPLIT_ACTIONS:
                qty[ident] = investments.apply_split(held, t)
                continue
            qty[ident] = held + investments.share_qty_delta(t)
            if not option and action in investments._REMOVE_ACTIONS:
                prior = removed_today.get(ident, (symbol, Decimal(0)))[1]
                removed_today[ident] = (symbol, prior + investments._D(t["quantity"]))
        close_day()
        for ident, (symbol, date, held, removed) in negative.items():
            out.append(ImportFinding(
                REMOVED_UNHELD, aid, name, symbol, date,
                f"{removed} shares removed when {held} were held; the account now "
                f"shows {qty.get(ident, Decimal(0))}. Look for the same security under "
                f"another name, or an arrival with no share count."))
        if acct["closed_flag"]:
            continue
        for ident, shares in qty.items():
            if shares == 0 or (shares < 0 and ident in negative):
                continue
            terms = investments.option_terms(conn, ident)
            if terms and terms.get("expiration") and terms["expiration"] < as_of:
                continue
            row = conn.execute(
                "SELECT MAX(date) FROM price_history WHERE symbol IN (%s) AND date<=?"
                % ",".join("?" * len(investments._identity_symbols(conn, ident))),
                (*investments._identity_symbols(conn, ident), as_of)).fetchone()
            last = row[0] if row else None
            if last is None or last < cutoff:
                out.append(ImportFinding(
                    STALE_PRICE, aid, name, ident, None,
                    f"{shares} held; " + (f"newest price {last}." if last
                                          else "no price recorded.")))

    for symbol, (aid, name) in sorted(options_traded.items()):
        row = conn.execute("SELECT kind_source, multiplier FROM securities WHERE symbol=?",
                           (symbol,)).fetchone()
        if row is None or (row["kind_source"] or "") == "user":
            continue
        if securities.observed_multiplier(conn, symbol) is None:
            stated = row["multiplier"] or "not stated"
            out.append(ImportFinding(
                OPTION_UNITS, aid, name, symbol, None,
                f"valued with multiplier {stated}; its trades do not show whether "
                f"quantity x price is the cash or a hundredth of it."))

    order = {k: i for i, k in enumerate(KINDS)}
    out.sort(key=lambda f: (order[f.kind], f.account.lower(), f.symbol, f.date or ""))
    return out


def format_findings(findings: List[ImportFinding], limit: int = 8) -> List[str]:
    """Text lines for an import report: one heading per kind with its count, and
    up to ``limit`` findings under each."""
    lines: List[str] = []
    for kind in KINDS:
        group = [f for f in findings if f.kind == kind]
        if not group:
            continue
        lines.append(f"{_HEADINGS[kind]} ({len(group)}):")
        for f in group[:limit]:
            when = f"{f.date} " if f.date else ""
            lines.append(f"  {f.account} - {f.symbol}: {when}{f.detail}")
        if len(group) > limit:
            lines.append(f"  ...and {len(group) - limit} more")
    return lines

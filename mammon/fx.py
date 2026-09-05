"""Foreign-exchange support: per-account native currency and a dated FX-rate
store, so a multi-currency ledger can report net worth *per currency* and,
optionally, total it through FX into one presentation currency.

Why this is its own pure-domain module, parallel to :mod:`mammon.investments`:

- It has no Qt and no UI, and it adds NO transaction write path. Cash amounts
  stay signed integer cents and :mod:`mammon.ledger` remains the sole writer of
  transaction rows; FX only reads balances and multiplies. The one account
  field it sets (``accounts.currency``) is routed through
  :func:`mammon.ledger.update_account`, the canonical account writer, so there
  is no second writer of the accounts table either.
- Rates are Decimal-encoded TEXT, exactly like ``price_history.close_price`` and
  share quantities -- never a float. A rate is "units of ``quote`` per 1 unit of
  ``base`` on ``date``"; :func:`get_rate` reads the most recent rate on/before a
  date and derives the inverse when only one direction was recorded, so storing
  USD->EUR also answers EUR->USD.
- ``accounts.currency`` is ``NOT NULL DEFAULT 'USD'`` in the schema, so an
  account with no explicit currency IS USD, the base. Everything here normalises
  a missing/blank currency to ``'USD'`` (:func:`_norm_ccy`) rather than treating
  a NULL specially -- the schema never stores one.

The network lives behind an injectable seam, exactly like the investment quote
source (see :class:`mammon.investments.YFinanceQuoteSource`):
:class:`YFinanceFxSource` imports ``yfinance`` lazily -- only inside
``get_rates`` -- so importing this module never touches the network, and tests
pass a fake source and never import yfinance.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

BASE_CURRENCY = "USD"
_CENT = Decimal("1")


# ---------------------------------------------------------------------------
# Decimal / currency helpers (mirroring investments._D / _qty_text)
# ---------------------------------------------------------------------------
def _norm_ccy(code) -> str:
    """A missing/blank currency is the base (USD); otherwise upper-cased ISO text."""
    if code is None:
        return BASE_CURRENCY
    s = str(code).strip().upper()
    return s or BASE_CURRENCY


def _D(value) -> Decimal:
    """Tolerant Decimal parse; '' / None -> 0."""
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return Decimal(0)


def _rate_text(value) -> str:
    """A clean, exponent-free Decimal string for storage ('0.85' not '8.5E-1',
    '100' not '1E+2')."""
    d = _D(value)
    if d == 0:
        return "0"
    return format(d.normalize(), "f")


# ---------------------------------------------------------------------------
# Per-account native currency
# ---------------------------------------------------------------------------
def get_account_currency(conn: sqlite3.Connection, account_id: int) -> str:
    """The account's native currency (``'USD'`` when unset -- the base)."""
    row = conn.execute(
        "SELECT currency FROM accounts WHERE id=?", (account_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no such account: {account_id}")
    return _norm_ccy(row["currency"])


def set_account_currency(conn: sqlite3.Connection, account_id: int, currency) -> None:
    """Set an account's native currency. Routed through
    :func:`mammon.ledger.update_account` so the accounts table keeps a single
    writer; a blank/None value resets it to the base currency (USD)."""
    from mammon import ledger  # local import avoids an import cycle

    ledger.update_account(conn, account_id, currency=_norm_ccy(currency))


# ---------------------------------------------------------------------------
# FX-rate store
# ---------------------------------------------------------------------------
def set_rate(conn: sqlite3.Connection, date: str, base, quote, rate) -> None:
    """Upsert one dated FX rate: ``rate`` units of ``quote`` per 1 unit of
    ``base`` on ``date``. Stored as Decimal-encoded TEXT, never a float. Unique
    on (date, base, quote)."""
    conn.execute(
        "INSERT INTO fx_rates(date, base, quote, rate) VALUES (?,?,?,?) "
        "ON CONFLICT(date, base, quote) DO UPDATE SET rate=excluded.rate",
        (date, _norm_ccy(base), _norm_ccy(quote), _rate_text(rate)),
    )
    conn.commit()


def _lookup_rate(conn: sqlite3.Connection, base: str, quote: str,
                 date: Optional[str]) -> Optional[str]:
    """Raw rate text for base->quote as of ``date`` (most recent on/before), or
    the latest recorded when ``date`` is None. No inverse/same-currency logic."""
    if date is None:
        row = conn.execute(
            "SELECT rate FROM fx_rates WHERE base=? AND quote=? "
            "ORDER BY date DESC LIMIT 1", (base, quote)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT rate FROM fx_rates WHERE base=? AND quote=? AND date<=? "
            "ORDER BY date DESC LIMIT 1", (base, quote, date)
        ).fetchone()
    return row["rate"] if row else None


def get_rate(conn: sqlite3.Connection, base, quote,
             date: Optional[str] = None) -> Optional[Decimal]:
    """The rate to convert ``base`` -> ``quote`` (units of ``quote`` per 1
    ``base``) as of ``date`` (default: latest recorded).

    Returns ``Decimal(1)`` for a same-currency request, derives the inverse when
    only the opposite direction was recorded, and ``None`` when neither
    direction is known.
    """
    base = _norm_ccy(base)
    quote = _norm_ccy(quote)
    if base == quote:
        return Decimal(1)
    direct = _lookup_rate(conn, base, quote, date)
    if direct is not None:
        return _D(direct)
    inverse = _lookup_rate(conn, quote, base, date)
    if inverse is not None:
        d = _D(inverse)
        if d != 0:
            return Decimal(1) / d
    return None


def convert_cents(conn: sqlite3.Connection, amount_cents: int, from_ccy, to_ccy,
                  date: Optional[str] = None) -> int:
    """Convert signed integer cents from ``from_ccy`` to ``to_ccy`` as of
    ``date``, rounding ROUND_HALF_UP at the cents boundary.

    Raises :class:`FxRateUnavailable` when no rate (direct or inverse) is known.
    """
    from_ccy = _norm_ccy(from_ccy)
    to_ccy = _norm_ccy(to_ccy)
    if from_ccy == to_ccy:
        return int(amount_cents)
    rate = get_rate(conn, from_ccy, to_ccy, date)
    if rate is None:
        raise FxRateUnavailable(
            f"no FX rate for {from_ccy}->{to_ccy} on/before {date or 'latest'}"
        )
    converted = (Decimal(int(amount_cents)) * rate).quantize(
        _CENT, rounding=ROUND_HALF_UP
    )
    return int(converted)


# ---------------------------------------------------------------------------
# Net worth by currency (and an optional FX total)
# ---------------------------------------------------------------------------
def net_worth_by_currency(conn: sqlite3.Connection, as_of: Optional[str] = None,
                          *, include_hidden: bool = False) -> dict[str, int]:
    """Net worth grouped by each account's native currency: ``{currency: cents}``.

    Mirrors :func:`mammon.investments.net_worth` account selection (closed
    accounts included, hidden excluded unless asked) and uses the same
    market-valued ``display_balance`` so investment holdings count. NO currency
    conversion happens here -- each account's cents land under its own currency,
    the honest per-currency view. Use :func:`total_in_currency` to fold the
    buckets into one presentation currency through FX.
    """
    from mammon import investments, ledger  # local import avoids an import cycle

    totals: dict[str, int] = {}
    for a in ledger.list_accounts(conn, include_closed=True,
                                  include_hidden=include_hidden):
        ccy = _norm_ccy(a["currency"])
        bal = investments.display_balance(conn, a["id"], as_of)
        totals[ccy] = totals.get(ccy, 0) + bal
    return totals


def total_in_currency(conn: sqlite3.Connection, target_ccy, as_of: Optional[str] = None,
                      *, include_hidden: bool = False) -> int:
    """Fold :func:`net_worth_by_currency` into one presentation currency through
    FX, rounding each bucket HALF_UP.

    Raises :class:`FxRateUnavailable` when a needed rate is missing -- the caller
    decides how to surface that rather than silently dropping a currency.
    """
    target = _norm_ccy(target_ccy)
    total = 0
    buckets = net_worth_by_currency(conn, as_of, include_hidden=include_hidden)
    for ccy, cents in buckets.items():
        total += convert_cents(conn, cents, ccy, target, as_of)
    return total


# ---------------------------------------------------------------------------
# Network fetch behind an injectable seam (see module docstring)
# ---------------------------------------------------------------------------
@dataclass
class FxRate:
    date: str            # ISO YYYY-MM-DD
    base: str
    quote: str
    rate: str            # Decimal text: units of `quote` per 1 `base`
    source: str = "yfinance"


class FxRateUnavailable(RuntimeError):
    """No usable FX backend (yfinance absent and no source given), or a required
    rate was not found for a conversion."""


def _fx_symbol(base, quote) -> str:
    """yfinance FX pair symbol: 'EURUSD=X' is USD (quote) per 1 EUR (base)."""
    return f"{_norm_ccy(base)}{_norm_ccy(quote)}=X"


class YFinanceFxSource:
    """Latest FX close via the ``yfinance`` package. The import and the network
    call happen ONLY inside ``get_rates``, so importing this module never hits
    the network and tests that inject a fake source never import yfinance."""

    source_name = "yfinance"

    def get_rates(self, pairs) -> list[FxRate]:      # pragma: no cover - network
        import yfinance as yf

        out: list[FxRate] = []
        for base, quote in pairs:
            base = _norm_ccy(base)
            quote = _norm_ccy(quote)
            try:
                hist = yf.Ticker(_fx_symbol(base, quote)).history(period="1d")
                if hist is None or hist.empty:
                    continue
                close = hist["Close"].iloc[-1]
                day = hist.index[-1].date().isoformat()
                out.append(FxRate(day, base, quote,
                                  str(round(Decimal(str(close)), 6)), "yfinance"))
            except Exception:
                continue
        return out


def default_fx_source() -> "YFinanceFxSource":
    """The best available FX backend, or raise if none is installed."""
    if importlib.util.find_spec("yfinance") is None:
        raise FxRateUnavailable(
            "yfinance is not installed; pass an FxSource explicitly (install the "
            "'quotes' extra to enable network fetch)."
        )
    return YFinanceFxSource()


def fetch_rates(conn: sqlite3.Connection, pairs, source=None) -> list[FxRate]:
    """Fetch the latest close for each ``(base, quote)`` pair and write fx_rates
    rows.

    ``source`` is any object with ``get_rates(pairs) -> list[FxRate]`` (and an
    optional ``source_name``); omit it to use :func:`default_fx_source`
    (yfinance). The network lives entirely inside the source, so tests inject a
    fake and never touch it. Same-currency and duplicate pairs are dropped.
    Returns the rates written.
    """
    seen: list[tuple[str, str]] = []
    for base, quote in pairs:
        pair = (_norm_ccy(base), _norm_ccy(quote))
        if pair[0] != pair[1] and pair not in seen:
            seen.append(pair)
    if not seen:
        return []
    src = source or default_fx_source()
    rates = src.get_rates(seen)
    for r in rates:
        set_rate(conn, r.date, r.base, r.quote, r.rate)
    return rates

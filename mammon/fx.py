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
  account with no explicit currency IS USD, the base. Everything here normalizes
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


def list_rates(conn: sqlite3.Connection, base=None, quote=None) -> list:
    """Every recorded FX rate as ``{date, base, quote, rate}`` dicts, newest date
    first then by pair -- the dated store laid out for display and editing.

    A pure read over the same table :func:`set_rate` writes and :func:`get_rate`
    reads; the FX-rate UI lists through here so the UI layer keeps no SQL of its
    own (CLAUDE.md). ``base``/``quote`` optionally narrow to one pair (both
    normalized)."""
    sql = "SELECT date, base, quote, rate FROM fx_rates"
    clauses, params = [], []
    if base is not None:
        clauses.append("base=?")
        params.append(_norm_ccy(base))
    if quote is not None:
        clauses.append("quote=?")
        params.append(_norm_ccy(quote))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY date DESC, base, quote"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


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
    # Zero converts to zero at any rate, so short-circuit BEFORE the lookup: an
    # empty foreign account (balance 0, no recorded rate) must not sink the whole
    # net-worth total. The raise below still fires for every non-zero amount --
    # that strictness is deliberate (see docstring), do not soften it.
    if amount_cents == 0:
        return 0
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
                          *, include_hidden: bool = False,
                          account_ids=None) -> dict[str, int]:
    """Net worth grouped by each account's native currency: ``{currency: cents}``.

    Mirrors :func:`mammon.investments.net_worth` account selection (closed
    accounts included, hidden excluded unless asked, ``account_ids`` restricting
    to a chosen subset) and uses the same market-valued ``display_balance`` so
    investment holdings count. NO currency conversion happens here -- each
    account's cents land under its own currency, the honest per-currency view.
    Use :func:`net_worth_currencies` to lay those buckets out with their FX
    conversions, or :func:`total_in_currency` to fold them into one number.
    """
    from mammon import investments, ledger  # local import avoids an import cycle

    wanted = None if account_ids is None else {int(a) for a in account_ids}
    totals: dict[str, int] = {}
    for a in ledger.list_accounts(conn, include_closed=True,
                                  include_hidden=include_hidden):
        if wanted is not None and int(a["id"]) not in wanted:
            continue
        ccy = _norm_ccy(a["currency"])
        bal = investments.display_balance(conn, a["id"], as_of)
        totals[ccy] = totals.get(ccy, 0) + bal
    return totals


def account_currencies(conn: sqlite3.Connection, *, include_hidden: bool = False,
                       account_ids=None) -> set:
    """The distinct native currencies among the accounts that would count toward
    net worth -- the same account selection as :func:`net_worth_by_currency`, but
    reading only ``accounts.currency`` and never a balance.

    Cheap on purpose: :func:`mammon.ledger.net_worth` calls this per as-of sample
    to decide whether the currency-aware fold is needed at all, so a single- (or
    all-base-) currency ledger keeps its original fast path untouched.
    """
    from mammon import ledger  # local import avoids an import cycle

    wanted = None if account_ids is None else {int(a) for a in account_ids}
    out: set = set()
    for a in ledger.list_accounts(conn, include_closed=True,
                                  include_hidden=include_hidden):
        if wanted is not None and int(a["id"]) not in wanted:
            continue
        out.add(_norm_ccy(a["currency"]))
    return out


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
        if cents == 0:
            continue  # a zero bucket needs no rate; convert_cents guards this too
        total += convert_cents(conn, cents, ccy, target, as_of)
    return total


@dataclass
class CurrencyLine:
    """One currency's contribution to net worth: its NATIVE subtotal (signed
    cents in that currency) and its value CONVERTED into the presentation
    currency. ``converted_cents`` is ``None`` when a non-zero native balance has
    no usable FX rate -- the row is then surfaced UNCONVERTED and left OUT of the
    grand total, never folded in at a dishonest 1:1 (which would overstate net
    worth by the whole foreign balance)."""

    currency: str
    native_cents: int
    converted_cents: Optional[int]      # None => rate missing (non-zero balance)

    @property
    def rate_missing(self) -> bool:
        return self.converted_cents is None


@dataclass
class NetWorthCurrencies:
    """Net worth laid out for the multi-currency presentation the user asked for:
    each currency listed with its native subtotal, then its conversion, then the
    grand total in one currency. ``lines`` is one :class:`CurrencyLine` per
    currency (the ``target`` currency first, then the rest A->Z); ``total_cents``
    is the grand total in ``target`` and equals the SUM of the convertible lines'
    ``converted_cents`` and nothing else.

    ``unconverted`` names the currencies whose rate was missing so a caller can
    say the total is honestly INCOMPLETE rather than silently wrong. An all-
    ``target`` ledger yields a single line whose conversion is the identity, so
    ``total_cents`` equals today's naive base sum exactly (byte for byte)."""

    target: str
    lines: list                         # list[CurrencyLine]
    total_cents: int

    @property
    def unconverted(self) -> list:
        return [ln.currency for ln in self.lines if ln.rate_missing]

    @property
    def is_complete(self) -> bool:
        return not self.unconverted


def net_worth_currencies(conn: sqlite3.Connection, as_of: Optional[str] = None,
                         *, target_ccy=BASE_CURRENCY, include_hidden: bool = False,
                         account_ids=None) -> "NetWorthCurrencies":
    """Net worth as a per-currency presentation: each currency's native subtotal,
    its value converted into ``target_ccy``, and a grand total that is the sum of
    ONLY the convertible lines.

    This is the honest multi-currency view the account bar and reports render. A
    foreign balance with no recorded FX rate becomes an UNCONVERTED line
    (``converted_cents is None``) and is excluded from ``total_cents`` -- it is
    NEVER added at 1:1, which would overstate net worth by the raw foreign
    number. A zero foreign balance converts free (:func:`convert_cents` guards
    it) and needs no rate. An all-``target`` ledger yields one line whose
    conversion is the identity, so ``total_cents`` matches today's naive base
    sum exactly.
    """
    target = _norm_ccy(target_ccy)
    buckets = net_worth_by_currency(conn, as_of, include_hidden=include_hidden,
                                    account_ids=account_ids)
    total = 0
    lines: list = []
    # Presentation order: the user's own (target) currency leads, then the rest
    # alphabetically, so the layout is stable across reloads.
    for ccy in sorted(buckets, key=lambda c: (c != target, c)):
        native = buckets[ccy]
        try:
            conv = convert_cents(conn, native, ccy, target, as_of)
            total += conv
        except FxRateUnavailable:
            conv = None                 # non-zero balance, no rate: leave it out
        lines.append(CurrencyLine(ccy, native, conv))
    return NetWorthCurrencies(target, lines, total)


# ---------------------------------------------------------------------------
# Net worth by ASSET -- one line per coin AND per fiat currency
# ---------------------------------------------------------------------------
@dataclass
class AssetLine:
    """One asset's contribution to net worth. A coin position carries its NATIVE
    ``quantity`` (a :class:`~decimal.Decimal`) and its USD-converted market value;
    a fiat bucket carries ``native_cents`` and its FX-converted value. ``is_coin``
    says which, and exactly one of ``quantity`` / ``native_cents`` is set."""

    asset: str                        # coin symbol ('ETH') or ISO currency ('USD')
    is_coin: bool
    quantity: Optional[Decimal]       # native coin quantity (coins only)
    native_cents: Optional[int]       # native cents (fiat only)
    usd_cents: int                    # value converted to the base currency


@dataclass
class NetWorthBreakdown:
    """Net worth split into one :class:`AssetLine` per coin/currency plus the
    base-currency ``total_usd_cents``. Coins list first (A->Z), then currencies.
    ``total_usd_cents`` equals :func:`total_in_currency` for the base currency --
    the split never double-counts a crypto holding."""

    lines: list                       # list[AssetLine]
    total_usd_cents: int


def net_worth_by_asset(conn: sqlite3.Connection, as_of: Optional[str] = None,
                       *, include_hidden: bool = False) -> "NetWorthBreakdown":
    """Break net worth into one line per coin and per fiat currency: each coin's
    NATIVE quantity and its USD-converted market value (USD is applied only at
    this net-worth layer, never on a wallet row), each currency bucket's native
    cents and its FX-converted value, and the base-currency grand total.

    A crypto account contributes its holdings as coin lines (``symbol`` x latest
    ``{SYM}-USD`` price, via :func:`mammon.crypto.account_valuation`) and its cash
    sleeve as a fiat line -- exactly the two halves ``display_balance`` already
    sums for that account, so the crypto holdings are counted EXACTLY once and
    ``total_usd_cents`` matches :func:`total_in_currency`. A wallet has no cash
    sleeve (``cash`` is 0), so it adds only coin lines.
    """
    from mammon import crypto, investments, ledger  # local import avoids a cycle

    coin_qty: dict[str, Decimal] = {}
    coin_usd: dict[str, int] = {}
    fiat_cents: dict[str, int] = {}
    for a in ledger.list_accounts(conn, include_closed=True,
                                  include_hidden=include_hidden):
        if crypto.is_crypto_account(a):
            val = crypto.account_valuation(conn, a["id"], as_of)
            for hv in val.holdings:
                coin_qty[hv.symbol] = coin_qty.get(hv.symbol, Decimal(0)) + hv.quantity
                coin_usd[hv.symbol] = coin_usd.get(hv.symbol, 0) + hv.market_value
            if val.cash:
                ccy = _norm_ccy(a["currency"])
                fiat_cents[ccy] = fiat_cents.get(ccy, 0) + val.cash
        else:
            ccy = _norm_ccy(a["currency"])
            fiat_cents[ccy] = (fiat_cents.get(ccy, 0)
                               + investments.display_balance(conn, a["id"], as_of))

    lines: list = []
    total = 0
    for sym in sorted(coin_qty):
        if coin_qty[sym] == 0:
            continue                              # a fully-spent position is not a line
        usd = coin_usd.get(sym, 0)
        total += usd
        lines.append(AssetLine(sym, True, coin_qty[sym], None, usd))
    for ccy in sorted(fiat_cents):
        native = fiat_cents[ccy]
        if native == 0:
            continue
        usd = convert_cents(conn, native, ccy, BASE_CURRENCY, as_of)
        total += usd
        lines.append(AssetLine(ccy, False, None, native, usd))
    return NetWorthBreakdown(lines, total)


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
    """The best available FX backend, or raise if none is installed.

    ``yfinance`` is a REQUIRED dependency -- it is the default source for FX
    rates, investment quotes, crypto prices and the asset-mix split -- so
    reaching this error means a broken or partial install, not a feature the user
    declined. The message used to name a ``quotes`` extra, which no longer
    exists; pointing at an install target that is not there is worse than saying
    nothing. The find_spec check itself stays, because a caller may always inject
    its own source: that is how the tests, and an offline session, avoid the
    network entirely."""
    if importlib.util.find_spec("yfinance") is None:
        raise FxRateUnavailable(
            "yfinance is not installed, but it is a required dependency -- "
            "reinstall with 'pip install -r requirements.txt'. To record a rate "
            "without it, enter one by hand in Tools > Exchange Rates, or pass an "
            "FxSource explicitly."
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

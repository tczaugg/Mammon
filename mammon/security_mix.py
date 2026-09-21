"""mammon.security_mix -- one security, several asset classes (SRD 5.8g).

A target-date fund is not "stocks". It is roughly 58% equity, 40% bonds and 2%
cash, and counting the whole position under one class does not merely coarsen the
allocation: it makes every drift figure in :mod:`mammon.rebalance` **wrong**,
because the misallocated weight is subtracted from some other class, which then
reads underweight by exactly that amount. Quicken has had per-security mixtures
since the 1990s; this is the same idea over Mammon's classes.

**Where the split comes from.** ``yfinance`` exposes a fund's own composition
(``Ticker(sym).funds_data.asset_classes``): cash, stock, bond, preferred,
convertible and other, as fractions. That is the Morningstar X-Ray axis, and it
is authoritative for everything except one thing --

**the domestic/international split, which yfinance does not provide.** There is
no region breakdown in ``funds_data``; the domicile lives only in
``fund_overview['categoryName']`` as English ("Foreign Large Blend"). Mammon
splits equity by domicile, so the stock slice has to land in a bucket the data
cannot name. It therefore lands in the class the USER already assigned to that
security (``securities.asset_class``) -- a decision they have already made --
and, where they have not made it, in ``unclassified``, visibly, rather than
being guessed into one hemisphere. :func:`suggest_stock_class` reads the
category text as a SUGGESTION for the UI to offer, never as a fetch-time
default; the same discipline as ``investments.ticker_of``, where guessing
turned ``INTL EQUITY INDEX`` into a real listed company called INTL.

A fund of funds therefore reports its equity in one bucket even when its
underlying is split across two. That is a known limit of the source, recorded
here rather than papered over.

**Preferred and convertible holdings map to ``other``**, not to bonds or to
equity. They are hybrids, they are routinely under 0.5% of a fund, and Mammon
has no class that means either -- ``other`` is the honest bucket rather than a
guess about how they behave.

Nothing here writes a transaction, and a security with no mixture keeps its
single asset class and behaves exactly as it did before: this is additive.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

from mammon import portfolio

_HUNDRED = Decimal("100")

# yfinance's own keys -> the Mammon class they land in. ``stock`` is absent on
# purpose: it is routed per-security (see the module docstring).
_DIRECT = {
    "cashPosition": "cash",
    "bondPosition": "bond",
    "otherPosition": "other",
    "preferredPosition": "other",
    "convertiblePosition": "other",
}
STOCK_KEY = "stockPosition"
STOCK_CLASSES = ("domestic_stock", "intl_stock")

# Words in a fund's Morningstar category that mark its equity as non-US. Used
# ONLY to suggest a class for the user to confirm.
_FOREIGN_HINTS = ("foreign", "international", "global", "world", "emerging",
                  "europe", "japan", "pacific", "china", "india", "latin")


class MixtureSourceUnavailable(RuntimeError):
    """No usable mixture backend (yfinance absent and no source given)."""


@dataclass
class SecurityMixture:
    """One security's split across asset classes, as percentages."""
    symbol: str
    weights: dict = field(default_factory=dict)      # {asset_class: Decimal pct}
    source: Optional[str] = None
    as_of: Optional[str] = None
    category: Optional[str] = None                   # the provider's own label


def _D(value, default=Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value).strip().rstrip("%").strip())
    except Exception:
        return default


def _today() -> str:
    return _dt.date.today().isoformat()


def normalize(weights: dict, places: str = "0.01") -> dict:
    """Round the weights and force them to total exactly 100.

    A provider's fractions come back as 1.64 + 98.36 + 0.01 = 100.01, and a
    mixture that totals 100.01 would allocate more than the position is worth.
    The rounding remainder lands on the LARGEST class, where it is proportionally
    least visible. Zero and negative weights are dropped."""
    clean = {k: _D(v).quantize(Decimal(places), rounding=ROUND_HALF_UP)
             for k, v in (weights or {}).items()}
    clean = {k: v for k, v in clean.items() if v > 0}
    if not clean:
        return {}
    remainder = _HUNDRED - sum(clean.values(), Decimal("0"))
    if remainder:
        biggest = max(clean, key=lambda k: clean[k])
        clean[biggest] = clean[biggest] + remainder
    return clean


def suggest_stock_class(category: Optional[str]) -> Optional[str]:
    """Read a provider's category text for a domestic/international hint, or
    None when it says nothing either way.

    A SUGGESTION for the UI to offer, never a fetch-time default: 'Foreign Large
    Blend' is clear, 'Target-Date 2030' holds both hemispheres, and guessing
    which one is how a stranger's data ends up in someone's allocation."""
    text = (category or "").strip().lower()
    if not text:
        return None
    if any(hint in text for hint in _FOREIGN_HINTS):
        return "intl_stock"
    return None


def map_positions(positions: dict, stock_class: Optional[str] = None) -> dict:
    """A provider's raw positions -> ``{mammon class: pct}``.

    ``positions`` values may be fractions (0.58) or percentages (58); both are
    accepted, since providers differ and a silent 100x error in an allocation is
    not the kind of bug that announces itself. ``stock_class`` is where the
    equity slice lands; without one it lands in ``unclassified``."""
    raw = {k: _D(v) for k, v in (positions or {}).items()}
    total = sum(raw.values(), Decimal("0"))
    if total and total <= Decimal("1.5"):            # fractions, not percents
        raw = {k: v * _HUNDRED for k, v in raw.items()}
    out: dict = {}
    for key, value in raw.items():
        if value <= 0:
            continue
        if key == STOCK_KEY:
            cls = stock_class if stock_class in STOCK_CLASSES else "unclassified"
        else:
            cls = _DIRECT.get(key)
            if cls is None:
                continue                              # a key we do not model
        out[cls] = out.get(cls, Decimal("0")) + value
    return normalize(out)


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def set_mixture(conn, symbol: str, weights: dict, source: Optional[str] = None,
                as_of: Optional[str] = None) -> None:
    """Record a security's class mixture, replacing any existing one.

    An empty mapping CLEARS it, so the security falls back to its single asset
    class. Weights are normalized to total 100; an unknown class is refused
    rather than stored, since a typo there would quietly vanish value from the
    allocation."""
    sym = (symbol or "").strip()
    if not sym:
        raise ValueError("a mixture needs a symbol")
    clean = normalize(weights)
    for cls in clean:
        if cls not in portfolio.ASSET_CLASSES and cls != "unclassified":
            raise ValueError(f"unknown asset class {cls!r}; one of "
                             f"{portfolio.ASSET_CLASSES}")
    conn.execute("DELETE FROM security_mixtures WHERE symbol=?", (sym,))
    stamp = as_of or _today()
    for cls, pct in sorted(clean.items(), key=lambda kv: (-kv[1], kv[0])):
        conn.execute(
            "INSERT INTO security_mixtures(symbol, asset_class, pct, source, as_of) "
            "VALUES (?,?,?,?,?)", (sym, cls, str(pct), source, stamp))
    conn.commit()


def _settle_stock_slice(weights: dict, stock_class) -> dict:
    """The stored mixture as it should READ today: an ``unclassified`` slice is
    the EQUITY the provider could not place (it publishes no domestic/overseas
    split), so it belongs to whatever stock class the security carries NOW.

    Read-time, deliberately, and for the reason split adjustment is read-time
    (5.8e-4): the slice was stored against the class the security had when the
    composition was fetched, so a fund marked "bond" by mistake froze nearly all
    of itself into ``unclassified`` and setting its class afterwards changed
    nothing until someone re-fetched. Now the correction takes effect at once,
    and un-setting the class puts it back to unclassified.
    """
    if "unclassified" not in weights or stock_class not in STOCK_CLASSES:
        return weights
    out = {k: v for k, v in weights.items() if k != "unclassified"}
    out[stock_class] = out.get(stock_class, Decimal("0")) + weights["unclassified"]
    return out


def get_mixture(conn, symbol: str) -> dict:
    """``{asset_class: Decimal pct}`` for the security, ``{}`` when it has none.
    The equity slice follows the security's CURRENT class (:func:`_settle_stock_slice`)."""
    sym = (symbol or "").strip()
    weights = {r["asset_class"]: _D(r["pct"]) for r in conn.execute(
        "SELECT asset_class, pct FROM security_mixtures WHERE symbol=? "
        "ORDER BY asset_class", (sym,)).fetchall()}
    row = conn.execute("SELECT asset_class FROM securities WHERE symbol=?",
                       (sym,)).fetchone()
    return _settle_stock_slice(weights, row["asset_class"] if row is not None else None)


def mixture_meta(conn, symbol: str) -> Optional[dict]:
    """``{'source': ..., 'as_of': ...}`` for the security's mixture, or None."""
    row = conn.execute(
        "SELECT source, as_of FROM security_mixtures WHERE symbol=? LIMIT 1",
        ((symbol or "").strip(),)).fetchone()
    return None if row is None else {"source": row["source"], "as_of": row["as_of"]}


def clear_mixture(conn, symbol: str) -> bool:
    cur = conn.execute("DELETE FROM security_mixtures WHERE symbol=?",
                       ((symbol or "").strip(),))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# the same idea for an ACCOUNT (SRD 5.8g)
# ---------------------------------------------------------------------------
# An account's balance could only ever be one class, so a conservative sleeve or
# a managed account reported as a single balance had to pick one and be wrong
# about the rest. These are the account-shaped twins of the four functions
# above, over `account_mixtures`, and they deliberately share normalize() and
# split_value() with them so a mixture means the same thing either side.
def set_account_mixture(conn, account_id: int, weights: dict,
                        source: Optional[str] = None,
                        as_of: Optional[str] = None) -> None:
    """Record (or, with an empty mapping, clear) an account's class mixture."""
    aid = int(account_id)
    clean = normalize(weights)
    for cls in clean:
        if cls not in portfolio.ASSET_CLASSES:
            raise ValueError(f"unknown asset class {cls!r}; one of "
                             f"{portfolio.ASSET_CLASSES}")
    conn.execute("DELETE FROM account_mixtures WHERE account_id=?", (aid,))
    stamp = as_of or _today()
    for cls, pct in sorted(clean.items(), key=lambda kv: (-kv[1], kv[0])):
        conn.execute(
            "INSERT INTO account_mixtures(account_id, asset_class, pct, source, as_of) "
            "VALUES (?,?,?,?,?)", (aid, cls, str(pct), source, stamp))
    conn.commit()


def get_account_mixture(conn, account_id: int) -> dict:
    """``{asset_class: Decimal pct}`` for the account, ``{}`` when it has none.

    No ``unclassified`` settling, unlike a security's: that slice exists because
    a data provider cannot split equity by domicile, and nobody fetches an
    account's composition from a provider. An account mixture is stated by the
    user or it does not exist."""
    return {r["asset_class"]: _D(r["pct"]) for r in conn.execute(
        "SELECT asset_class, pct FROM account_mixtures WHERE account_id=? "
        "ORDER BY asset_class", (int(account_id),)).fetchall()}


def clear_account_mixture(conn, account_id: int) -> bool:
    cur = conn.execute("DELETE FROM account_mixtures WHERE account_id=?",
                       (int(account_id),))
    conn.commit()
    return cur.rowcount > 0


def all_account_mixtures(conn) -> dict:
    """``{account_id: {asset_class: Decimal pct}}`` for every account that has
    one -- one query, for the allocation's per-account loop."""
    out: dict = {}
    for row in conn.execute(
            "SELECT account_id, asset_class, pct FROM account_mixtures"):
        out.setdefault(int(row["account_id"]), {})[row["asset_class"]] = _D(row["pct"])
    return out


def all_mixtures(conn) -> dict:
    """``{symbol: {asset_class: Decimal pct}}`` for every security that has one.
    One query, because the allocation asks for all of them at once. Each equity
    slice follows the security's CURRENT class (:func:`_settle_stock_slice`)."""
    out: dict = {}
    for r in conn.execute(
            "SELECT symbol, asset_class, pct FROM security_mixtures "
            "ORDER BY symbol, asset_class").fetchall():
        out.setdefault(r["symbol"], {})[r["asset_class"]] = _D(r["pct"])
    assigned = {r["symbol"]: r["asset_class"] for r in conn.execute(
        "SELECT symbol, asset_class FROM securities WHERE asset_class IS NOT NULL")}
    return {sym: _settle_stock_slice(w, assigned.get(sym))
            for sym, w in out.items()}


def split_value(cents: int, mixture: dict) -> dict:
    """Divide ``cents`` across a mixture, EXACTLY.

    Largest-remainder apportionment: naive rounding of each share loses or
    invents cents, and an allocation whose parts do not sum to the position is a
    reconciliation bug waiting to be found by someone else. Returns
    ``{asset_class: cents}`` summing to ``cents``."""
    if not mixture:
        return {}
    total = int(cents)
    exact = {cls: (Decimal(total) * pct / _HUNDRED) for cls, pct in mixture.items()}
    floors = {cls: int(v.to_integral_value(rounding="ROUND_FLOOR"))
              for cls, v in exact.items()}
    short = total - sum(floors.values())
    # Hand the leftover cents to the largest fractional parts first.
    order = sorted(exact, key=lambda c: (-(exact[c] - floors[c]), c))
    for i in range(abs(short)):
        cls = order[i % len(order)]
        floors[cls] += 1 if short > 0 else -1
    return floors


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
class YFinanceMixtureSource:
    """Fund composition from yfinance (``funds_data.asset_classes``).

    The network lives entirely here, so tests inject a fake and never touch it.
    A symbol that is not a fund, or that the provider has nothing for, is simply
    absent from the result -- never a zeroed or guessed mixture."""

    source_name = "yfinance"

    def get_mixtures(self, symbols: Iterable[str]) -> list:
        import yfinance as yf

        out: list = []
        for sym in symbols:
            sym = (sym or "").strip()
            if not sym:
                continue
            try:
                data = yf.Ticker(sym).funds_data
                positions = dict(data.asset_classes or {})
            except Exception:
                continue                       # not a fund, or nothing published
            if not positions:
                continue
            category = None
            try:
                category = (data.fund_overview or {}).get("categoryName")
            except Exception:
                pass
            out.append(SecurityMixture(symbol=sym, weights=positions,
                                       source=self.source_name, as_of=_today(),
                                       category=category))
        return out


def default_mixture_source():
    """The best available mixture backend, or raise if none is installed."""
    import importlib.util

    if importlib.util.find_spec("yfinance") is None:
        raise MixtureSourceUnavailable(
            "yfinance is not installed; pass a source explicitly or enter the "
            "mixture by hand.")
    return YFinanceMixtureSource()


@dataclass
class MixtureFetchReport:
    """What :func:`fetch_mixtures` did, and what it could not do -- so the UI
    can name the securities that need a decision instead of reporting a silent
    partial success."""
    written: list = field(default_factory=list)      # SecurityMixture
    missing: list = field(default_factory=list)      # (symbol, reason)
    needs_stock_class: list = field(default_factory=list)   # (symbol, suggestion)

    @property
    def ok(self) -> bool:
        return not self.missing


def fetch_mixtures(conn, symbols: Iterable[str], source=None,
                   stock_classes: Optional[dict] = None) -> MixtureFetchReport:
    """Fetch and store class mixtures for ``symbols``.

    The equity slice of each fund lands in the class that security is already
    assigned (``securities.asset_class``), or in ``stock_classes[symbol]`` when
    the caller overrides it. With neither, it lands in ``unclassified`` and the
    symbol is listed in ``needs_stock_class`` with whatever the provider's
    category suggests -- surfaced for the user to confirm, never applied.

    A source that returns nothing for a symbol leaves any existing mixture
    alone: a stale split is a far better answer than a wiped one.
    """
    wanted = [s.strip() for s in symbols if (s or "").strip()]
    if not wanted:
        return MixtureFetchReport()
    if source is None:
        source = default_mixture_source()
    assigned = {r["symbol"]: r["asset_class"] for r in portfolio.list_securities(conn)}
    overrides = dict(stock_classes or {})

    report = MixtureFetchReport()
    found = {}
    for mix in source.get_mixtures(wanted):
        found[mix.symbol] = mix
    for sym in wanted:
        mix = found.get(sym)
        if mix is None:
            report.missing.append(
                (sym, "no fund composition published for this symbol"))
            continue
        stock_class = overrides.get(sym) or assigned.get(sym)
        if stock_class not in STOCK_CLASSES:
            stock_class = None
        weights = map_positions(mix.weights, stock_class)
        if not weights:
            report.missing.append((sym, "the provider returned an empty mixture"))
            continue
        if "unclassified" in weights:
            report.needs_stock_class.append((sym, suggest_stock_class(mix.category)))
        set_mixture(conn, sym, weights, source=mix.source or
                    getattr(source, "source_name", None), as_of=mix.as_of)
        report.written.append(SecurityMixture(sym, weights, mix.source, mix.as_of,
                                              mix.category))
    return report


def describe(mixture: dict) -> str:
    """'58% Domestic stock / 40% Bonds / 2% Cash' -- the one-line summary a
    table cell shows in place of a single class name."""
    if not mixture:
        return ""
    parts = sorted(mixture.items(), key=lambda kv: (-kv[1], kv[0]))
    return " / ".join(f"{_trim(pct)}% {portfolio.ASSET_CLASS_LABELS.get(cls, cls)}"
                      for cls, pct in parts)


def _trim(pct: Decimal) -> str:
    """A percentage with its trailing zeros gone and no exponent.

    ``Decimal.normalize()`` alone strips zeros on BOTH sides of the point, so a
    whole 100.00 became ``1E+2`` and the Mix column read "1E+2% Bonds"
    (reported). The ``f`` presentation type forces fixed-point notation, which
    is what turns that back into "100" while still shortening 58.50 to 58.5.
    """
    return f"{pct.normalize():f}"

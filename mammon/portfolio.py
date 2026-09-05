"""mammon.portfolio -- lots, capital gains, performance and allocation over the
investment replay (roadmap item 7).

Everything here is a READ over :mod:`mammon.investments`: the lot state a
replay leaves behind (open lots, the realized gains each sale booked), the
account's valuation at two dates with the cash that moved in between, and the
market value of each holding grouped by the class the user gave its security.
No table is written except ``securities`` (a security's type and asset class,
which nothing else knows) and, through :mod:`mammon.ledger`, an account's own
asset class -- the one thing that lets a house be allocated at all.

Money is signed integer cents; share quantities are Decimal. A RATE is the one
thing that is not money, so the money-weighted return comes back as a float.

Performance is MONEY-WEIGHTED (an internal rate of return over dated flows,
Quicken's "IRR"): what the account earned on the money it actually held, when
it held it. The flows are what crossed the account's boundary -- cash
transferred in or out, securities moved in or out -- never buys, sells or
dividends, which only move value between cash and securities inside it. For
one security the boundary is the security itself: buys and shares in are
money put in, sales, cash dividends and shares out are money taken out.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

from mammon import asset_values, investments, ledger, security_mix
from mammon.investments import RealizedGain, _D, _HUNDRED, _cents

ASSET_CLASSES = ("domestic_stock", "intl_stock", "bond", "cash", "real_estate", "other")
# The one asset class that cannot be TRADED to its own weight: cash is raised by
# selling securities and spent by buying them, so a rebalancer must never propose
# "selling" it (see rebalance.ClassDrift.action). Named so that rule has a single
# source of truth rather than a literal scattered across modules.
CASH_CLASS = "cash"
ASSET_CLASS_LABELS = {
    "domestic_stock": "Domestic stock", "intl_stock": "International stock",
    "bond": "Bonds", "cash": "Cash", "real_estate": "Real estate", "other": "Other",
    "unclassified": "Unclassified",
}
SECURITY_TYPES = ("stock", "fund", "etf", "bond", "option", "cd", "other")

# What an allocation is OF. Quicken has only the first of these: its Allocations
# view and its rebalancer read investment accounts, so a house never appears and
# its own forums answer the question with "invent a dummy security in a dummy
# brokerage". Mammon instead widens the scope, because the honest answer to
# "where is my money" for someone with a mortgage-sized asset is not "in your
# brokerage". Debt is in NO scope: an allocation is of what you own, and a pie
# cannot show a negative slice; net worth is where a mortgage belongs.
ALLOCATION_SCOPES = ("investments", "with_cash", "everything")
ALLOCATION_SCOPE_LABELS = {
    "investments": "Investment accounts",
    "with_cash": "Investments and cash accounts",
    "everything": "Everything you own",
}
_CASH_TYPES = ("checking", "savings", "cash")
_SCOPE_TYPES = {
    "investments": ("investment",),
    "with_cash": ("investment",) + _CASH_TYPES,
    "everything": ("investment",) + _CASH_TYPES + ("asset",),
}


def account_asset_class(acct) -> str:
    """The asset class an account's own balance counts as (an accounts row).

    An explicit ``accounts.asset_class`` wins. With none said, a cash-shaped
    account is cash -- the same rule that has always counted a brokerage's idle
    cash as cash, not a guess about the user -- and everything else is
    ``unclassified``, because what an "other asset" account holds (a house, a
    car, a violin) is not derivable from the ledger and must never be invented.
    """
    cls = (acct["asset_class"] if "asset_class" in acct.keys() else None) or ""
    if cls:
        return cls
    return "cash" if (acct["type"] or "") in _CASH_TYPES else "unclassified"


def set_account_asset_class(conn, account_id: int, asset_class) -> None:
    """Record (or clear, with ``None``/``""``) the asset class an account's
    balance is allocated to. Writes through :mod:`mammon.ledger`, which owns the
    accounts table."""
    if asset_class:
        if asset_class not in ASSET_CLASSES:
            raise ValueError(f"unknown asset class {asset_class!r}; one of {ASSET_CLASSES}")
    ledger.update_account(conn, int(account_id), asset_class=asset_class or None)


def scope_account_ids(conn, scope: str = "investments",
                      include_hidden: bool = False) -> list:
    """The open accounts an allocation of ``scope`` covers.

    HIDDEN accounts are excluded, matching :func:`investments.net_worth`, and
    that consistency is the point. Hiding is how a user says "the records for
    this account are incomplete -- leave it out of my totals": an employer plan
    whose contributions went in but whose share purchases were never entered
    holds a balance the ledger cannot justify. Such an account values as pure
    CASH, so including it does not merely inflate the total -- it invents a cash
    slice out of nothing and skews every other class's percentage with it. On a
    real file a hidden plan turned a 5,000 brokerage-cash position into 245,000
    of reported cash.

    ``include_hidden`` opts into the fuller picture, the same escape hatch the
    report bar offers for net worth.
    """
    if scope not in _SCOPE_TYPES:
        raise ValueError(f"unknown allocation scope {scope!r}; one of {ALLOCATION_SCOPES}")
    types = _SCOPE_TYPES[scope]
    return [int(a["id"]) for a in ledger.list_accounts(conn, include_closed=False,
                                                       include_hidden=include_hidden)
            if (a["type"] or "") in types]


# Actions that move money across the ACCOUNT's boundary (cash or shares
# transferred in / out), signed toward the account.
_EXTERNAL_IN = {"xin", "shrsin", "addshares", "contribx"}
_EXTERNAL_OUT = {"xout", "shrsout", "removeshares", "withdrwx", "withdraw"}
_CASH_DIVIDENDS = investments._DIVIDEND_ACTIONS - {
    "reinvdiv", "reinvlg", "reinvsh", "reinvmd", "reinvint"}


def _action(t) -> str:
    return (t["action"] or "").strip().lower().replace(" ", "")


def _day_before(iso: str) -> str:
    return (_dt.date.fromisoformat(iso) - _dt.timedelta(days=1)).isoformat()


def _value_of(t) -> int:
    """What a transaction moved, in cents: its amount when it carries one,
    else price times shares."""
    amount = t["amount"]
    if amount not in (None, 0):
        return abs(int(amount))
    q = _D(t["quantity"])
    return _cents(q * _D(t["price"]) * _HUNDRED) if t["price"] and q else 0


# ---------------------------------------------------------------------------
# lots and capital gains
# ---------------------------------------------------------------------------
@dataclass
class LotView:
    """An open lot valued at a date: what the register cannot show -- when
    these particular shares were bought, what they cost, what they are worth,
    and whether selling them today would be a long- or short-term gain."""
    symbol: str
    acquired: Optional[str]
    txn_id: Optional[int]
    quantity: Decimal
    cost: int
    price: Optional[Decimal]
    market_value: int
    gain: Optional[int]
    term: str                         # long | short | unknown, as of the valuation date
    days_held: Optional[int]


def open_lots(conn, account_id: int, symbol: Optional[str] = None,
              as_of: Optional[str] = None, prices: Optional[dict] = None) -> list:
    """Every open lot in the account (or of one ``symbol``), valued at ``as_of``
    (today when omitted), oldest first within each security."""
    today = as_of or _dt.date.today().isoformat()
    positions = investments._replay_positions(conn, account_id, as_of)
    out = []
    for sym in sorted(positions):
        if symbol is not None and sym != symbol:
            continue
        pos = positions[sym]
        if pos.qty <= 0:
            continue
        price = investments._resolve_price(conn, sym, as_of, prices, account_id)
        lots = pos.lots or [investments._Lot(pos.qty, pos.cost, None, None)]
        for lot in lots:
            mv = _cents(lot.qty * price * _HUNDRED) if price is not None else 0
            if lot.date:
                a = _dt.date.fromisoformat(lot.date)
                d = _dt.date.fromisoformat(today)
                days = (d - a).days
                term = "long" if d > investments._add_year(a) else "short"
            else:
                days, term = None, "unknown"
            out.append(LotView(sym, lot.date, lot.txn_id, lot.qty, lot.cost, price, mv,
                               (mv - lot.cost) if price is not None else None, term, days))
    return out


def capital_gains(conn, account_id: int, start: Optional[str] = None,
                  end: Optional[str] = None, symbol: Optional[str] = None) -> list:
    """Every realized gain the account's sales booked with a sale date in
    ``[start, end]`` (either bound open when omitted), one row per lot drawn
    on, in sale-date order. Replayed from inception: a snapshot carries the
    open lots forward but not the gains already booked."""
    positions = investments._replay_positions(conn, account_id, end, use_snapshots=False)
    rows: list = []
    for sym, pos in positions.items():
        if symbol is not None and sym != symbol:
            continue
        for g in pos.gains:
            if start is not None and g.sold < start:
                continue
            if end is not None and g.sold > end:
                continue
            rows.append(g)
    rows.sort(key=lambda g: (g.sold, g.symbol, g.acquired or "", g.sale_txn_id or 0))
    return rows


def gains_summary(gains: Iterable[RealizedGain]) -> dict:
    """Proceeds, basis, gain and lot count by holding period (``short``,
    ``long``, ``unknown``) plus ``total`` -- the Schedule D footings."""
    out = {k: {"proceeds": 0, "basis": 0, "gain": 0, "lots": 0}
           for k in ("short", "long", "unknown", "total")}
    for g in gains:
        for k in (g.term, "total"):
            out[k]["proceeds"] += g.proceeds
            out[k]["basis"] += g.basis
            out[k]["gain"] += g.gain
            out[k]["lots"] += 1
    return out


# ---------------------------------------------------------------------------
# performance (money-weighted return)
# ---------------------------------------------------------------------------
def xirr(flows, *, tol: float = 1e-10) -> Optional[float]:
    """The annualized rate at which dated ``flows`` (``[(iso_date, cents)]``,
    money out negative, money back positive) net to zero -- an internal rate
    of return on irregular dates. ``None`` when the flows do not admit one
    (all one sign, or no root between -99% and +1000%/yr). Bisection on the
    net present value, which is what makes it safe to call on any data the
    user might have."""
    pts = [(_dt.date.fromisoformat(d), float(c)) for d, c in flows if c]
    if not pts or all(c > 0 for _, c in pts) or all(c < 0 for _, c in pts):
        return None
    t0 = min(d for d, _ in pts)
    ts = [((d - t0).days / 365.0, c) for d, c in pts]

    def npv(r: float) -> float:
        return sum(c / (1.0 + r) ** t for t, c in ts)

    lo, hi = -0.99, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if (f_lo < 0) == (f_hi < 0):
        return None
    mid = lo
    for _ in range(300):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < tol or (hi - lo) < 1e-12:
            break
        if (f_lo < 0) == (f_mid < 0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid
    return mid


@dataclass
class Performance:
    """What an account (or one security in it) earned over a period."""
    account_id: int
    start: str
    end: str
    start_value: int                  # cents, the day before ``start``
    end_value: int                    # cents, at ``end``
    money_in: int                     # cents that crossed the boundary inward
    money_out: int                    # cents that crossed the boundary outward
    income: int                       # cents of dividends/interest/distributions in the period
    irr: Optional[float]              # annualized money-weighted return, or None
    symbol: Optional[str] = None
    flows: list = field(default_factory=list)   # the dated flows the rate was solved on

    @property
    def gain(self) -> int:
        """Ending value less starting value less net money put in: what the
        period earned, income and price change together."""
        return self.end_value - self.start_value - (self.money_in - self.money_out)


def external_flows(conn, account_id: int, start: str, end: str) -> list:
    """Money that crossed the account's boundary in ``[start, end]`` as
    ``[(date, signed cents into the account)]``: ledger transfer legs (a cash
    transfer from checking), less those an ``XIn``/``XOut`` investment row also
    records (the same multiset rule the valuation uses, so nothing is counted
    twice), plus the investment rows that move cash or shares in or out."""
    out: list = []
    inv = conn.execute(
        "SELECT * FROM investment_transactions WHERE account_id=? AND date BETWEEN ? AND ? "
        "ORDER BY date, id", (account_id, start, end)).fetchall()
    represented: dict = {}
    for t in inv:
        if t["transfer_account_id"] is not None:
            key = (t["date"], int(t["transfer_account_id"]), abs(int(t["amount"] or 0)))
            represented[key] = represented.get(key, 0) + 1
        a = _action(t)
        if a in _EXTERNAL_IN:
            out.append((t["date"], _value_of(t)))
        elif a in _EXTERNAL_OUT:
            out.append((t["date"], -_value_of(t)))
    for r in conn.execute(
            "SELECT date, transfer_account_id, amount FROM transactions "
            "WHERE account_id=? AND transfer_account_id IS NOT NULL AND scheduled=0 "
            "AND date BETWEEN ? AND ? ORDER BY date, id", (account_id, start, end)):
        key = (r["date"], int(r["transfer_account_id"]), abs(int(r["amount"] or 0)))
        if represented.get(key, 0) > 0:
            represented[key] -= 1
            continue
        out.append((r["date"], int(r["amount"] or 0)))
    out.sort(key=lambda f: f[0])
    return out


def account_performance(conn, account_id: int, start: str, end: str,
                        prices: Optional[dict] = None) -> Performance:
    """The account's money-weighted return over ``[start, end]``: its value
    the day before ``start`` is money put in, each external flow is money in
    or out on its date, its value at ``end`` is money back."""
    before = _day_before(start)
    start_value = investments.account_valuation(conn, account_id, before, prices).total
    end_value = investments.account_valuation(conn, account_id, end, prices).total
    flows = external_flows(conn, account_id, start, end)
    money_in = sum(c for _, c in flows if c > 0)
    money_out = -sum(c for _, c in flows if c < 0)
    income = 0
    for t in conn.execute(
            "SELECT action, amount FROM investment_transactions "
            "WHERE account_id=? AND date BETWEEN ? AND ?", (account_id, start, end)):
        if _action(t) in investments._DIVIDEND_ACTIONS:
            income += abs(int(t["amount"] or 0))
    dated = [(before, -start_value)] + [(d, -c) for d, c in flows] + [(end, end_value)]
    return Performance(account_id, start, end, start_value, end_value, money_in,
                       money_out, income, xirr(dated), flows=dated)


def security_performance(conn, account_id: int, symbol: str, start: str, end: str,
                         prices: Optional[dict] = None) -> Performance:
    """One security's money-weighted return in the account over
    ``[start, end]``: shares held the day before ``start`` at that day's price
    are money put in; buys and shares transferred in are money put in; sales,
    cash dividends and distributions, and shares transferred out are money
    back; shares held at ``end`` at that day's price are money back."""
    before = _day_before(start)

    def value_at(date: str) -> int:
        pos = investments._replay_positions(conn, account_id, date).get(symbol)
        if pos is None or pos.qty <= 0:
            return 0
        price = investments._resolve_price(conn, symbol, date, prices, account_id)
        return _cents(pos.qty * price * _HUNDRED) if price is not None else 0

    start_value, end_value = value_at(before), value_at(end)
    flows: list = []
    income = 0
    for t in conn.execute(
            "SELECT * FROM investment_transactions WHERE account_id=? AND symbol=? "
            "AND date BETWEEN ? AND ? ORDER BY date, id", (account_id, symbol, start, end)):
        a = _action(t)
        if a in _CASH_DIVIDENDS:
            flows.append((t["date"], abs(int(t["amount"] or 0))))
            income += abs(int(t["amount"] or 0))
        elif a in investments._DIVIDEND_ACTIONS:
            income += abs(int(t["amount"] or 0))        # reinvested: stays inside
        if a in investments._SALE_ACTIONS:
            flows.append((t["date"], investments._proceeds_of(t, _D(t["quantity"]))))
        elif a in _EXTERNAL_OUT and a in investments._REMOVE_ACTIONS:
            flows.append((t["date"], _value_of(t)))
        elif a in investments._ADD_ACTIONS and a not in investments._DIVIDEND_ACTIONS:
            flows.append((t["date"], -investments._cost_of(t, _D(t["quantity"]))))
    money_in = -sum(c for _, c in flows if c < 0)
    money_out = sum(c for _, c in flows if c > 0)
    dated = [(before, -start_value)] + flows + [(end, end_value)]
    return Performance(account_id, start, end, start_value, end_value, money_in,
                       money_out, income, xirr(dated), symbol=symbol, flows=dated)


# ---------------------------------------------------------------------------
# securities and allocation
# ---------------------------------------------------------------------------
def get_security(conn, symbol: str):
    return conn.execute("SELECT * FROM securities WHERE symbol=?", (symbol,)).fetchone()


def list_securities(conn) -> list:
    return conn.execute("SELECT * FROM securities ORDER BY symbol").fetchall()


def set_security(conn, symbol: str, *, name: Optional[str] = None,
                 sec_type: Optional[str] = None, asset_class: Optional[str] = None) -> None:
    """Record what a security is. A field passed as None is left as it was;
    pass "" to clear it. ``asset_class`` must be one of ``ASSET_CLASSES``."""
    if asset_class:
        if asset_class not in ASSET_CLASSES:
            raise ValueError(f"unknown asset class {asset_class!r}; one of {ASSET_CLASSES}")
    if sec_type and sec_type not in SECURITY_TYPES:
        raise ValueError(f"unknown security type {sec_type!r}; one of {SECURITY_TYPES}")
    cur = get_security(conn, symbol)
    vals = {
        "name": cur["name"] if cur is not None else None,
        "sec_type": cur["sec_type"] if cur is not None else None,
        "asset_class": cur["asset_class"] if cur is not None else None,
    }
    for k, v in (("name", name), ("sec_type", sec_type), ("asset_class", asset_class)):
        if v is not None:
            vals[k] = v or None
    conn.execute(
        "INSERT INTO securities(symbol, name, sec_type, asset_class) VALUES (?,?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, sec_type=excluded.sec_type, "
        "asset_class=excluded.asset_class",
        (symbol, vals["name"], vals["sec_type"], vals["asset_class"]))
    conn.commit()


@dataclass
class Slice:
    key: str
    label: str
    value: int
    pct: float


@dataclass
class Allocation:
    as_of: Optional[str]
    total: int
    by_class: list = field(default_factory=list)      # Slice, largest first
    by_security: list = field(default_factory=list)   # Slice keyed by symbol, largest first
    by_account: list = field(default_factory=list)    # Slice keyed by account id
    unpriced: list = field(default_factory=list)      # symbols left out for want of a price
    scope: str = "investments"
    # Investment accounts that contributed CASH but hold no securities at all.
    # Almost always incomplete records rather than a real cash position: money
    # went in as a transfer and the shares it bought were never entered, so the
    # ledger can only call the balance cash. Reported because the resulting
    # slice is indistinguishable from a genuine one and silently distorts every
    # percentage -- on a real file, five 529 plans and a 401k put ~200,000 into
    # "Cash" and the user reasonably concluded the scope was pulling in their
    # bank accounts.
    cash_only_accounts: list = field(default_factory=list)   # (name, cents)
    # Accounts whose whole balance was allocated as one thing (id -> asset
    # class): a house, a savings account. The window offers a class picker for
    # exactly these, since an investment account's classes come from its
    # securities instead.
    account_classes: dict = field(default_factory=dict)


def allocation(conn, account_ids: Optional[Iterable[int]] = None,
               as_of: Optional[str] = None, prices: Optional[dict] = None,
               scope: str = "investments", include_hidden: bool = False) -> Allocation:
    """Where the money is: the market value of every priced holding, grouped by
    the asset class recorded for its security -- ``unclassified`` until the user
    says -- and by security and account; each investment account's cash counts
    as ``cash``.

    ``account_ids`` names the accounts outright; with none given, ``scope``
    picks them (:data:`ALLOCATION_SCOPES`) -- the investment accounts alone
    (Quicken's answer), those plus the cash-shaped ones, or everything the user
    owns, property included. A NON-investment account brings its whole balance
    in as one slice of :func:`account_asset_class`; only investment accounts are
    broken down by security. Liabilities are never included: an allocation is of
    what you own.

    HIDDEN accounts are left out (see :func:`scope_account_ids`) unless
    ``include_hidden``; naming ``account_ids`` explicitly overrides both, since
    a caller that asked for an account means it.
    """
    given = account_ids is not None
    ids = ([int(a) for a in account_ids] if given
           else scope_account_ids(conn, scope, include_hidden=include_hidden))
    classes = {r["symbol"]: r["asset_class"] for r in list_securities(conn)}
    # A security may be several classes at once (a target-date fund is ~58/40/2).
    # Where a mixture exists it WINS over the single class, and the position's
    # value is split across it exactly -- see security_mix.split_value, which
    # apportions by largest remainder so the parts sum to the position rather
    # than losing a cent per holding.
    mixtures = security_mix.all_mixtures(conn)
    by_class: dict = {}
    by_sec: dict = {}
    by_acct: dict = {}
    acct_classes: dict = {}
    unpriced: list = []
    cash_only: list = []
    total = 0
    for aid in ids:
        acct = ledger.get_account(conn, aid)
        name = acct["name"] if acct is not None else str(aid)
        atype = (acct["type"] if acct is not None else "") or ""
        if atype != "investment":
            # An explicitly named liability is still not allocated -- the caller
            # asking for "these accounts" does not make a debt an asset.
            if atype in ("credit", "liability"):
                continue
            # A house is worth what it is worth, not what it cost: an asset
            # account with a recorded market value contributes that. Without one
            # the ledger balance stands (see asset_values.market_value).
            value = asset_values.market_value(conn, aid, as_of)
            if value is None:
                value = ledger.account_balance(conn, aid, as_of)
            cls = account_asset_class(acct)
            acct_classes[aid] = cls
            by_acct[aid] = (name, value)
            by_class[cls] = by_class.get(cls, 0) + value
            total += value
            continue
        v = investments.account_valuation(conn, aid, as_of, prices)
        by_acct[aid] = (name, v.total)
        total += v.total
        if v.cash:
            by_class["cash"] = by_class.get("cash", 0) + v.cash
            if not v.holdings:
                cash_only.append((name, int(v.cash)))
        for h in v.holdings:
            if h.price is None:
                unpriced.append(h.symbol)
                continue
            mix = mixtures.get(h.symbol)
            if mix:
                for cls, part in security_mix.split_value(h.market_value, mix).items():
                    by_class[cls] = by_class.get(cls, 0) + part
            else:
                cls = classes.get(h.symbol) or "unclassified"
                by_class[cls] = by_class.get(cls, 0) + h.market_value
            # By SECURITY is unaffected: a mixture splits what a holding is made
            # of, not how much of it there is.
            by_sec[h.symbol] = by_sec.get(h.symbol, 0) + h.market_value

    def pct(v: int) -> float:
        return (v / total * 100.0) if total else 0.0

    out = Allocation(as_of=as_of, total=total, unpriced=sorted(set(unpriced)),
                     scope=("custom" if given else scope),
                     account_classes=acct_classes,
                     cash_only_accounts=sorted(cash_only, key=lambda p: -p[1]))
    out.by_class = [Slice(k, ASSET_CLASS_LABELS.get(k, k), v, pct(v))
                    for k, v in sorted(by_class.items(), key=lambda kv: -kv[1])]
    out.by_security = [Slice(k, k, v, pct(v))
                       for k, v in sorted(by_sec.items(), key=lambda kv: (-kv[1], kv[0]))]
    out.by_account = [Slice(str(aid), name, v, pct(v))
                      for aid, (name, v) in sorted(by_acct.items(), key=lambda kv: -kv[1][1])]
    return out

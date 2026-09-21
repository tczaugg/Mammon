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

from mammon import asset_values, crypto, investments, ledger, security_mix
from mammon.investments import RealizedGain, _D, _HUNDRED, _cents

#: The classes a holding, a security mixture or an account may be assigned to.
#:
#: ``crypto`` joined them on 2026-09-21. It had lived only in
#: ``forecast.PROJECTION_CLASSES`` -- the projection knew it was four times as
#: volatile as equity while nothing upstream could EMIT it, so a spot-crypto
#: ETF or a wallet could not be described as crypto in an allocation at all.
#: forecast.py's own comment anticipated this ("ahead of the day
#: portfolio.ASSET_CLASSES gains the column"), and PROJECTION_CLASSES is now
#: exactly this tuple.
ASSET_CLASSES = ("domestic_stock", "intl_stock", "bond", "cash", "crypto",
                 "real_estate", "other")
# The one asset class that cannot be TRADED to its own weight: cash is raised by
# selling securities and spent by buying them, so a rebalancer must never propose
# "selling" it (see rebalance.ClassDrift.action). Named so that rule has a single
# source of truth rather than a literal scattered across modules.
CASH_CLASS = "cash"
ASSET_CLASS_LABELS = {
    "domestic_stock": "Domestic stock", "intl_stock": "International stock",
    "bond": "Bonds", "cash": "Cash", "crypto": "Crypto",
    "real_estate": "Real estate", "other": "Other",
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
    "investments": ledger.INVESTMENT_LIKE_TYPES,
    "with_cash": ledger.INVESTMENT_LIKE_TYPES + _CASH_TYPES,
    "everything": ledger.INVESTMENT_LIKE_TYPES + _CASH_TYPES + ("asset",),
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


def account_valuation(conn, account_id: int, as_of: Optional[str] = None,
                      prices: Optional[dict] = None, *,
                      money_market_as_cash: bool = False):
    """One account's :class:`investments.AccountValuation`, valued by the right
    engine for its KIND -- the single dispatch every scope-wide caller must use.

    A crypto account's money lives in ``crypto_transactions``, and the brokerage
    valuation cannot see it: it reads the bank transfer legs alone and reported a
    wallet whose own balance is zero as tens of thousands of NEGATIVE cash. The
    mistake is easy to repeat because both engines return the SAME dataclass, so
    calling the wrong one type-checks and merely produces a wrong number -- the
    Investment Dashboard ring did exactly that and dropped a Coinbase account out
    of the accounts ring entirely (its bogus total valued <= 0). Scope comes from
    :func:`scope_account_ids`, whose ``investments`` scope is
    ``ledger.INVESTMENT_LIKE_TYPES`` and therefore INCLUDES crypto; anything that
    values those ids must come through here.

    ``money_market_as_cash`` is a brokerage-only reporting choice and is ignored
    for crypto, which has no sweep.
    """
    acct = ledger.get_account(conn, account_id)
    if crypto.is_crypto_account(acct):
        return crypto.account_valuation(conn, account_id, as_of, prices)
    return investments.account_valuation(conn, account_id, as_of, prices,
                                         money_market_as_cash=money_market_as_cash)


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


# Share removals and additions, the only way Quicken can express shares moving
# between accounts: it has no share transfer, so a move reads as shares removed
# in one account and the same shares added in another.
_SHARE_REMOVALS = {"shrsout", "removeshares"}
_SHARE_ADDITIONS = {"shrsin", "addshares"}
#: How far apart the two sides of a share move may be dated and still pair.
SHARE_MOVE_WINDOW_DAYS = 10


def share_moves(conn) -> dict:
    """``{txn_id: cents}`` for every share removal and addition that is one side
    of shares MOVED between two of the user's accounts, valued at what the move
    was worth.

    User rule, 2026-09-15: "Quicken doesn't have a way to move shares from one
    account to another. You would just see shares removed in one and shares added
    in the other. So treat shares removed as fees unless you see that." Unpaired,
    a removal is a FEE -- a 401(k) takes its administrative and recordkeeping
    fees as shares -- and counting it as money handed back added every fee to the
    return instead of taking it off. Paired, the removal is money leaving that
    account's position and the addition money arriving in the other's.

    A pair is a removal and an addition in DIFFERENT accounts, of exactly the
    same number of shares of the same security (same canonical symbol, or the
    same recorded ticker), dated within :data:`SHARE_MOVE_WINDOW_DAYS`; nearest
    date wins and each row pairs once. A removal and an addition in the SAME
    account are a reorganization (a CUSIP change), not a move. The value is the
    row's own amount (or price x shares), else its partner's -- a broker records
    the arriving shares with no price at all."""
    alias_map = {a: c for a, c in investments.list_aliases(conn)}
    tickers = {r[0]: (r[1] or "").strip().upper() for r in conn.execute(
        "SELECT symbol, ticker FROM securities WHERE ticker IS NOT NULL")}

    def canon(sym):
        seen = {sym}
        while sym in alias_map and alias_map[sym] not in seen:
            sym = alias_map[sym]
            seen.add(sym)
        return sym

    def same_security(a, b):
        if canon(a) == canon(b):
            return True
        ta, tb = tickers.get(a), tickers.get(b)
        return bool(ta) and ta == (tb or canon(b).upper()) or bool(tb) and tb == canon(a).upper()

    removals, additions = [], []
    for t in conn.execute(
            "SELECT id, account_id, date, action, symbol, quantity, price, amount, memo "
            "FROM investment_transactions WHERE symbol IS NOT NULL AND symbol<>'' "
            "ORDER BY date, id"):
        a = _action(t)
        if investments.is_void_investment(t) or not (a in _SHARE_REMOVALS or a in _SHARE_ADDITIONS):
            continue
        q = abs(_D(t["quantity"]))
        if q == 0:
            continue
        (removals if a in _SHARE_REMOVALS else additions).append((t, q))
    out: dict = {}
    used: set = set()
    for rem, q in removals:
        day = _dt.date.fromisoformat(rem["date"])
        best = None
        for add, aq in additions:
            if add["id"] in used or aq != q or add["account_id"] == rem["account_id"]:
                continue
            gap = abs((_dt.date.fromisoformat(add["date"]) - day).days)
            if gap > SHARE_MOVE_WINDOW_DAYS or not same_security(rem["symbol"], add["symbol"]):
                continue
            if best is None or gap < best[0]:
                best = (gap, add)
        if best is None:
            continue
        add = best[1]
        used.add(add["id"])
        value = _value_of(rem) or _value_of(add)
        out[rem["id"]] = value
        out[add["id"]] = _value_of(add) or value
    return out


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

    @property
    def gain_pct(self) -> Optional[Decimal]:
        """:attr:`gain` as a percent of the capital at work -- the starting value
        plus every dollar put in -- or None when there was none."""
        base = self.start_value + self.money_in
        if base <= 0:
            return None
        return Decimal(self.gain) / Decimal(base) * _HUNDRED

    @property
    def annual_return(self) -> Optional[Decimal]:
        """The money-weighted return per year as a percent, over the span the
        flows actually cover (from the first dollar at work to ``end``), or None
        under a year. Annualizing a few months compounds noise into a rate
        nobody earned -- a 5% month reads as 80% a year -- so a return shorter
        than a year is shown only as its plain :attr:`gain_pct`."""
        if self.irr is None:
            return None
        dated = [d for d, c in self.flows if c]
        if not dated:
            return None
        span = (_dt.date.fromisoformat(self.end) - _dt.date.fromisoformat(min(dated))).days
        if span < MIN_ANNUALIZED_DAYS:
            return None
        return Decimal(str(round(self.irr * 100, 4)))


#: The shortest span a return is annualized over (see Performance.annual_return).
MIN_ANNUALIZED_DAYS = 365


def combine_performances(perfs, end: str) -> "Performance":
    """Several securities' performance as ONE: values and money summed, and the
    rate solved over all their dated flows together. Averaging the rates would
    weight a $500 position like a $50,000 one; one rate over the pooled flows
    weights each dollar by how long it was at work.

    Only a security with capital at work (a starting value or money put in)
    joins the pooled RATE; every one still counts in the dollar totals. A
    position that returned money without any going in -- shares that arrived
    from a merger with no cost recorded, a written option that expired -- has
    no rate of return at all, and pooling it left a real 30-year ledger with no
    rate that nets its flows to zero, so the portfolio's annual return was blank."""
    perfs = list(perfs)
    flows = [f for p in perfs if p.start_value + p.money_in > 0 for f in p.flows]
    starts = [p.start for p in perfs] or [end]
    return Performance(
        perfs[0].account_id if perfs else 0, min(starts), end,
        sum(p.start_value for p in perfs), sum(p.end_value for p in perfs),
        money_in=sum(p.money_in for p in perfs), money_out=sum(p.money_out for p in perfs),
        income=sum(p.income for p in perfs), irr=xirr(flows), flows=flows)


def holding_performances(conn, account_id: int, end: str, *,
                         start: Optional[str] = None,
                         prices: Optional[dict] = None) -> dict:
    """``{symbol: Performance}`` for every security the account held on or
    before ``end``, keyed by canonical symbol (aliases folded, as the replay
    folds them). One replay per boundary date for the whole account, not two
    per security.

    With ``start``: each security over ``[start, end]``, its value the day
    before ``start`` as money put in -- the report's selected period.

    Without ``start``: each security over its CURRENT HOLDING -- from the day
    its share count last left zero (for a position now closed, its last round
    trip) -- so a fund sold out in 2010 and bought again in 2021 is measured
    from 2021, and nothing was at work the day before.

    The gain counts every dividend exactly once. A cash dividend left the
    position, so it is money taken out; a reinvested one bought shares that are
    already in the ending value, so it is neither money in nor out. Adding the
    reinvestment as income on top of the value would count it twice; leaving
    cash dividends out would count them not at all, which is what the
    Investment Performance report and the Holdings window used to do."""
    alias_map = {a: c for a, c in investments.list_aliases(conn)}

    def canon(sym):
        seen = {sym}
        while sym in alias_map and alias_map[sym] not in seen:
            sym = alias_map[sym]
            seen.add(sym)
        return sym

    # A fund converted into another (investments.link_holding) is one holding
    # here: its rows count under the fund it continues as, and the conversion
    # day's sale and purchase move no money -- the value simply carries over at
    # the new fund's share count, like a split with an uneven ratio.
    successors = investments.holding_successors(conn, account_id)
    exchanged: set = set()
    for frm, to, day in investments.holding_links(conn, account_id):
        if canon(frm) not in successors:
            continue
        for t in conn.execute("SELECT id, action, symbol FROM investment_transactions "
                              "WHERE account_id=? AND date=?", (account_id, day)):
            a, sym = _action(t), canon((t["symbol"] or "").strip())
            if (sym == canon(frm) and a in investments._SALE_ACTIONS) or \
                    (sym == canon(to) and a in investments._ACQUIRE_ACTIONS):
                exchanged.add(t["id"])

    def holding_key(sym):
        key = canon(sym)
        return successors.get(key, key)

    before = _day_before(start) if start else None
    moves = share_moves(conn)
    flows: dict = {}
    income: dict = {}
    qty: dict = {}
    zero_on: dict = {}                       # the date each position last reached zero
    for t in investments._list_txns_in_range(conn, account_id, None, end):
        sym = (t["symbol"] or "").strip()
        if not sym or investments.is_void_investment(t):
            continue
        key = holding_key(sym)
        a = _action(t)
        if start is None and investments.is_quantity_action(t["action"]):
            held = qty.get(key, Decimal(0))
            now = (investments.apply_split(held, t) if a in investments._SPLIT_ACTIONS
                   else held + investments.share_qty_delta(t))
            qty[key] = now
            if held != 0 and now == 0:
                zero_on[key] = t["date"]
            elif held == 0 and now != 0 and zero_on.get(key, "") < t["date"]:
                # A holding (re)opens: measure from here. Only when it was empty
                # at the end of an EARLIER day -- a same-day zero crossing (shares
                # removed and re-added by a CUSIP change, or a same-day sale and
                # purchase) is one continuous holding, since rows within a day
                # are in no meaningful order.
                flows[key], income[key] = [], 0
        if start is not None and t["date"] < start:
            continue
        rows = flows.setdefault(key, [])
        income.setdefault(key, 0)
        if t["id"] in exchanged:
            continue
        q = _D(t["quantity"])
        if a in _CASH_DIVIDENDS:
            rows.append((t["date"], abs(int(t["amount"] or 0))))
            income[key] += abs(int(t["amount"] or 0))
        elif a in investments._DIVIDEND_ACTIONS:
            income[key] += abs(int(t["amount"] or 0))   # reinvested: already in the value
        if a in investments._SALE_ACTIONS or a in investments._SHORT_OPEN_ACTIONS:
            rows.append((t["date"], investments._proceeds_of(t, q)))
        elif a in investments._SHORT_COVER_ACTIONS:
            rows.append((t["date"], -investments._cost_of(t, q)))
        elif a in _SHARE_REMOVALS:
            # Money taken out only when another account received the shares; an
            # unpaired removal is a fee, already a loss in the ending value.
            if t["id"] in moves:
                rows.append((t["date"], moves[t["id"]]))
        elif a in _SHARE_ADDITIONS and t["id"] in moves:
            rows.append((t["date"], -max(moves[t["id"]], investments._cost_of(t, q))))
        elif a in _EXTERNAL_OUT and a in investments._REMOVE_ACTIONS:
            rows.append((t["date"], _value_of(t)))
        elif a in investments._RTRNCAP_ACTIONS:
            # Capital handed back is money taken out of the position, like a sale
            # with no shares; left out, a fund paying it showed no return at all.
            rows.append((t["date"], abs(int(t["amount"] or 0))))
        elif a in investments._ADD_ACTIONS and a not in investments._DIVIDEND_ACTIONS:
            rows.append((t["date"], -investments._cost_of(t, q)))

    def values_at(date):
        out = {}
        for sym, pos in investments._replay_positions(conn, account_id, date).items():
            if pos.qty == 0:
                continue
            price = investments._resolve_price(conn, sym, date, prices, account_id)
            if price is not None:
                key = holding_key(sym)
                out[key] = out.get(key, 0) + investments._market_value(conn, sym, pos.qty, price)
        return out

    end_values = values_at(end)
    start_values = values_at(before) if before else {}
    out = {}
    for key in set(flows) | set(start_values) | set(end_values):
        rows = flows.get(key, [])
        sv, ev = start_values.get(key, 0), end_values.get(key, 0)
        dated = ([(before, -sv)] if before else []) + rows + [(end, ev)]
        out[key] = Performance(
            account_id, start or (min((d for d, _ in rows), default=end)), end, sv, ev,
            money_in=-sum(c for _, c in rows if c < 0),
            money_out=sum(c for _, c in rows if c > 0),
            income=income.get(key, 0), irr=xirr(dated), symbol=key, flows=dated)
    return out


def cash_dividends(conn, account_ids, symbol: Optional[str] = None,
                   end: Optional[str] = None, start: Optional[str] = None) -> int:
    """Cents of income PAID OUT AS CASH, i.e. not reinvested.

    :attr:`Performance.income` deliberately counts every dividend action, cash
    and reinvested alike, because both are income earned. This answers the
    narrower question the Investment Dashboard has to ask about a single
    security: how much of what it earned LEFT it, and so is not in its market
    value today. A reinvested dividend bought shares and is inside that value;
    a cash one went to the account's cash and is not.

    ``_CASH_DIVIDENDS`` is the authority on which is which -- the dividend
    actions minus the five reinvest ones -- so this cannot drift from the flow
    classification in :func:`security_performance`.

    ``start`` defaults to the beginning of time: the question is asked about a
    point-in-time VALUE, and every cash dividend ever paid is missing from it,
    not merely this period's.
    """
    ids = [int(a) for a in account_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    sql = (f"SELECT action, amount, symbol FROM investment_transactions "
           f"WHERE account_id IN ({placeholders})")
    params = list(ids)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    if start:
        sql += " AND date >= ?"
        params.append(start)
    want = investments.resolve_symbol(conn, symbol) if symbol else None
    total = 0
    for row in conn.execute(sql, params):
        if _action(row) not in _CASH_DIVIDENDS:
            continue
        if want is not None:
            have = (row["symbol"] or "").strip()
            if investments.resolve_symbol(conn, have) != want:
                continue
        total += abs(int(row["amount"] or 0))
    return total


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
    moves = share_moves(conn)
    for t in inv:
        if t["transfer_account_id"] is not None:
            key = (t["date"], int(t["transfer_account_id"]), abs(int(t["amount"] or 0)))
            represented[key] = represented.get(key, 0) + 1
        a = _action(t)
        if a in _SHARE_REMOVALS or a in _SHARE_ADDITIONS:
            # Shares crossed the boundary only when another account took them
            # (share_moves); an unpaired removal is a fee, a loss inside.
            if t["id"] in moves:
                sign = 1 if a in _SHARE_ADDITIONS else -1
                out.append((t["date"], sign * moves[t["id"]]))
            elif a in _SHARE_ADDITIONS:
                out.append((t["date"], _value_of(t)))
        elif a in _EXTERNAL_IN:
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
    or out on its date, its value at ``end`` is money back.

    Both endpoint values go through :func:`account_valuation`, so a crypto
    wallet is valued by the crypto engine. Its FLOWS are still brokerage-only:
    :func:`external_flows` reads ``investment_transactions``, which a crypto
    account has none of, so a wallet's deposits are not recognised as money in
    and its return reads as if the whole change in value were gain. That is a
    known gap (it needs crypto sleeve events in ``external_flows``), but it is
    strictly better than the endpoints themselves being wrong."""
    before = _day_before(start)
    start_value = account_valuation(conn, account_id, before, prices).total
    end_value = account_valuation(conn, account_id, end, prices).total
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
    cash dividends and distributions, returned capital and shares transferred
    out are money back; shares held at ``end`` at that day's price are money
    back. The single-security view of :func:`holding_performances`, so the
    Performance dialog, the Investment Performance report and the Holdings
    window cannot classify a flow three different ways."""
    key = investments.resolve_symbol(conn, symbol)
    key = investments.holding_successors(conn, account_id).get(key, key)
    perf = holding_performances(conn, account_id, end, start=start, prices=prices).get(key)
    if perf is not None:
        return perf
    before = _day_before(start)
    return Performance(account_id, start, end, 0, 0, 0, 0, 0, None, symbol=symbol,
                       flows=[(before, 0), (end, 0)])


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


def _dollars(cents: int) -> str:
    """Cents as a plain decimal dollar string, for a human-readable note only.
    Decimal, never float: this is money being shown, and the rest of the module
    keeps it in integer cents."""
    return format(Decimal(int(cents)) / 100, ".2f")


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
    # Option contracts LEFT OUT of every number above (symbol, premium value in
    # cents), largest first. See SRD 5.8e-9: a contract is not shares of its
    # underlying, so it is neither allocated nor silently dropped -- it is named
    # here, and :attr:`options_note` is the sentence that says so out loud.
    excluded_options: list = field(default_factory=list)   # (symbol, cents)

    @property
    def options_note(self) -> str:
        """The VISIBLE sentence naming the excluded contracts, "" when there
        are none. Every presenter of an Allocation -- the MCP tool, a report,
        a window -- shows this verbatim when it is non-empty, so the exclusion
        can never be invisible to whoever reads the percentages."""
        if not self.excluded_options:
            return ""
        parts = ", ".join("%s %s" % (sym, _dollars(v))
                          for sym, v in self.excluded_options)
        return ("Option contracts are excluded from this allocation (SRD "
                "5.8e-9): %s. Total premium value %s. A contract is not shares "
                "of its underlying, so it is named here rather than counted as "
                "equity or dropped." % (parts, _dollars(self.option_value)))

    @property
    def option_value(self) -> int:
        """Cents of premium value excluded (a short position counts negative)."""
        return sum(v for _sym, v in self.excluded_options)


def allocation(conn, account_ids: Optional[Iterable[int]] = None,
               as_of: Optional[str] = None, prices: Optional[dict] = None,
               scope: str = "investments", include_hidden: bool = False,
               money_market_as_cash: bool = False) -> Allocation:
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

    OPTION CONTRACTS are excluded outright -- from ``total``, ``by_class``,
    ``by_security`` and each account's slice -- and named in
    :attr:`Allocation.excluded_options` with their premium value, which
    :attr:`Allocation.options_note` renders as a visible sentence (SRD 5.8e-9).
    A contract is not shares of its underlying: counting one as 100 shares
    invents exposure the premium never bought, and dropping it silently makes
    the percentages a lie about a position the user holds. Only an EXPLICIT
    ``kind='option'`` is removed (:func:`investments.is_option`); a NULL-kind
    row is unclassified and allocates exactly as it always did.

    HIDDEN accounts are left out (see :func:`scope_account_ids`) unless
    ``include_hidden``; naming ``account_ids`` explicitly overrides both, since
    a caller that asked for an account means it.

    ``money_market_as_cash`` files a money-market sweep's value under ``cash``
    instead of its asset class (SRD 5.8e-2c). It is the user's preference
    (``prefs.money_market_as_cash``, off by default so existing numbers do not
    move), passed down rather than read here -- this layer reads no Qt settings.
    ``total`` and ``by_security`` are identical either way: how much of the fund
    you hold is not a matter of opinion, only which bucket it is shown in.
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
    # An account may state its own mixture, which governs whatever balance the
    # account contributes ITSELF: a non-investment account's whole value, and an
    # investment account's idle CASH. It never touches the securities inside --
    # those say what they are for themselves.
    acct_mixtures = security_mix.all_account_mixtures(conn)
    by_class: dict = {}
    by_sec: dict = {}
    by_acct: dict = {}
    acct_classes: dict = {}
    unpriced: list = []
    cash_only: list = []
    excluded_opts: dict = {}      # symbol -> premium value in cents, left out
    total = 0
    for aid in ids:
        acct = ledger.get_account(conn, aid)
        name = acct["name"] if acct is not None else str(aid)
        atype = (acct["type"] if acct is not None else "") or ""
        if atype not in ledger.INVESTMENT_LIKE_TYPES:
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
            amix = acct_mixtures.get(aid)
            if amix:
                # Stated outright, so it wins over the single class exactly as a
                # security's mixture wins over its own.
                acct_classes[aid] = security_mix.describe(amix)
                for cls, part in security_mix.split_value(value, amix).items():
                    by_class[cls] = by_class.get(cls, 0) + part
            else:
                cls = account_asset_class(acct)
                acct_classes[aid] = cls
                by_class[cls] = by_class.get(cls, 0) + value
            by_acct[aid] = (name, value)
            total += value
            continue
        # One valuation per KIND of account, the same one the accounts list and
        # net worth show -- see account_valuation for what calling the brokerage
        # engine on a wallet reported instead.
        v = account_valuation(conn, aid, as_of, prices,
                              money_market_as_cash=money_market_as_cash)
        # Option contracts come OUT before anything is totalled (SRD 5.8e-9).
        # An allocation answers "how much of what do I own", and a contract has
        # no honest answer: counting one as 100 shares of the underlying
        # overstates equity by the notional the premium did not buy, and
        # counting the premium as equity is a different thing again. Only an
        # EXPLICIT kind='option' is removed -- a NULL-kind row is UNCLASSIFIED,
        # not "not an option", and keeps the pre-existing behavior exactly.
        opts = [h for h in v.holdings if investments.is_option(conn, h.symbol)]
        opt_symbols = {h.symbol for h in opts}
        opt_value = sum(int(h.market_value) for h in opts)
        for h in opts:
            excluded_opts[h.symbol] = excluded_opts.get(h.symbol, 0) + int(h.market_value)
        acct_total = v.total - opt_value
        by_acct[aid] = (name, acct_total)
        total += acct_total
        if v.cash:
            # Idle cash is cash UNLESS the account says otherwise. A sleeve
            # reported as one balance, or a stable-value fund that reaches the
            # ledger as cash, could not say so before: the class of an
            # investment account was never consulted at all, so its balance was
            # cash whatever the user set (reported).
            amix = acct_mixtures.get(aid)
            if amix:
                for cls, part in security_mix.split_value(int(v.cash), amix).items():
                    by_class[cls] = by_class.get(cls, 0) + part
            else:
                own = account_asset_class(acct) if acct is not None else "cash"
                if (acct["asset_class"] if acct is not None
                        and "asset_class" in acct.keys() else None):
                    # An explicit choice on an investment account is honored now.
                    by_class[own] = by_class.get(own, 0) + v.cash
                else:
                    by_class["cash"] = by_class.get("cash", 0) + v.cash
            if not v.holdings:
                cash_only.append((name, int(v.cash)))
        for h in v.holdings:
            if h.symbol in opt_symbols:
                # Excluded above; not reported unpriced either, since a price
                # would not have brought it into the allocation.
                continue
            if h.price is None:
                unpriced.append(h.symbol)
                continue
            if money_market_as_cash and investments.is_money_market(conn, h.symbol):
                # account_valuation ALREADY folded this position into v.cash,
                # which was added to the cash class above; adding it to a class
                # again here would count the same dollars twice. By security is
                # still by security -- the position exists either way.
                by_sec[h.symbol] = by_sec.get(h.symbol, 0) + h.market_value
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
                     cash_only_accounts=sorted(cash_only, key=lambda p: -p[1]),
                     excluded_options=sorted(excluded_opts.items(),
                                             key=lambda kv: (-kv[1], kv[0])))
    out.by_class = [Slice(k, ASSET_CLASS_LABELS.get(k, k), v, pct(v))
                    for k, v in sorted(by_class.items(), key=lambda kv: -kv[1])]
    out.by_security = [Slice(k, k, v, pct(v))
                       for k, v in sorted(by_sec.items(), key=lambda kv: (-kv[1], kv[0]))]
    out.by_account = [Slice(str(aid), name, v, pct(v))
                      for aid, (name, v) in sorted(by_acct.items(), key=lambda kv: -kv[1][1])]
    return out


# ---------------------------------------------------------------------------
# recent activity and price freshness (reads for a glance page)
# ---------------------------------------------------------------------------
@dataclass
class ActivityRow:
    """One investment transaction as a reader sees it: the account by NAME, the
    quantity as Decimal, the amount in signed cents."""

    id: int
    date: str
    account_id: int
    account_name: str
    action: str
    symbol: str
    quantity: Optional[Decimal]      # None for a cash-only row (a dividend)
    amount: int                      # cents, signed (negative = money out)
    memo: str = ""


def recent_investment_activity(conn, account_ids: Optional[Iterable[int]] = None,
                               limit: int = 25, include_hidden: bool = False) -> list:
    """The ``limit`` most recent investment transactions across the given
    accounts, newest first (:class:`ActivityRow`).

    One ordered query, not one per account: a glance panel that asked per
    account would then have to merge and re-sort in the presentation layer, and
    the merge is exactly the part that gets the tie-break wrong. The order is
    ``date DESC, id DESC`` -- the same tie-break the replay uses, so two rows
    entered on one day read in the reverse of the order they were entered.

    VOIDED rows are left out. A void keeps its row and zeroes its numbers
    (:func:`investments.void_investment`), so including one would show a
    0.00 "Buy" that the user cannot act on;
    :func:`investments.is_void_investment` stays the authority on what a void
    is, the SQL prefix filter only saving the rows from being fetched.

    ``account_ids`` defaults to the investment scope
    (:func:`scope_account_ids`, closed and hidden accounts excluded).
    """
    if account_ids is None:
        ids = scope_account_ids(conn, "investments", include_hidden=include_hidden)
    else:
        ids = [int(a) for a in account_ids]
    n = int(limit)
    if not ids or n <= 0:
        return []
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT t.id AS id, t.date AS date, t.account_id AS account_id, "
        "       a.name AS account_name, t.action AS action, t.symbol AS symbol, "
        "       t.quantity AS quantity, t.amount AS amount, t.memo AS memo "
        "FROM investment_transactions t "
        "LEFT JOIN accounts a ON a.id = t.account_id "
        f"WHERE t.account_id IN ({marks}) "
        "  AND (t.memo IS NULL OR t.memo NOT LIKE ?) "
        "ORDER BY t.date DESC, t.id DESC LIMIT ?",
        list(ids) + [ledger.VOID_PREFIX + "%", n]).fetchall()
    out = []
    for r in rows:
        if investments.is_void_investment(r):
            continue
        qty = r["quantity"]
        out.append(ActivityRow(
            id=int(r["id"]), date=r["date"] or "",
            account_id=int(r["account_id"] or 0),
            account_name=r["account_name"] or "",
            action=r["action"] or "", symbol=r["symbol"] or "",
            quantity=(_D(qty) if qty not in (None, "") else None),
            amount=int(r["amount"] or 0), memo=r["memo"] or ""))
    return out


@dataclass
class PriceFreshness:
    """How current the price series behind one symbol's valuation is."""

    symbol: str
    latest: Optional[str]            # ISO date of the newest close, None if never priced
    days: Optional[int]              # its age at the as-of date, None if never priced


def price_freshness(conn, symbols: Iterable[str], as_of: Optional[str] = None) -> list:
    """For each of ``symbols``, the newest ``price_history`` date and how old it
    is at ``as_of`` -- worst first: never priced, then oldest, then by symbol.

    The age is measured against ``as_of`` (defaulting to
    :func:`investments.valuation_as_of`, the ledger's own latest known date) and
    NOT against today's clock, so an archived file does not report every symbol
    as months stale merely because time passed outside it. It never goes
    negative: a close dated after ``as_of`` is current, not "-3 days old".

    Each symbol is asked over its whole canonical identity
    (:func:`investments._price_identity_clause`), the same rule
    :func:`investments.latest_price` values it by -- one grouped
    ``MAX(date)`` over the raw symbol column would call a renamed ticker
    unpriced while the valuation happily prices it from the old spelling. No
    price is rescaled here: this is a date, and a split does not move it.
    """
    when = as_of if as_of is not None else investments.valuation_as_of(conn)
    out = []
    for symbol in dict.fromkeys(s for s in symbols if s):
        clause, params = investments._price_identity_clause(conn, symbol)
        row = conn.execute(
            "SELECT MAX(date) AS latest FROM price_history WHERE " + clause,
            params).fetchone()
        latest = row["latest"] if row is not None else None
        days = None
        if latest and when:
            days = max(0, (_dt.date.fromisoformat(when)
                           - _dt.date.fromisoformat(latest)).days)
        out.append(PriceFreshness(symbol=symbol, latest=latest or None, days=days))
    return sorted(out, key=lambda f: (f.latest is not None,
                                      -(f.days if f.days is not None else 0),
                                      f.symbol))

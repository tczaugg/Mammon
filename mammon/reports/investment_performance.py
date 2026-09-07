"""mammon.reports.investment_performance -- consolidated per-holding investment
performance (SRD 5.9).

Quicken's most-requested investment view is a single, portfolio-wide table that
answers "how are my holdings doing": for every security ever held, its shares,
average and current price, cost basis, market value, unrealized gain/loss,
realized gain/loss, dividend/interest income and return of capital -- plus the
portfolio totals and a percent return. Everything here is a READ-ONLY assembly of
values the domain layer already computes; this module adds no new money math and
touches no write path.

Definitions (locked, so the report always reconciles with the Holdings window):

- One row per (investment account, security) via
  :func:`mammon.investments.security_positions`, which is the same replay the
  Holdings tabs and a security-filtered register read -- the three can never
  disagree. Non-investment accounts contribute nothing.
- Cost values are signed integer cents; share quantities and per-share prices are
  ``Decimal`` (decoded from the ledger's Decimal-TEXT storage). ``unrealized_pl``
  and ``price`` are ``None`` for an unpriced or closed (sold-out) position, never
  a fabricated zero.
- ``as_of`` caps only the valuation PRICE, never the share/cost replay -- exactly
  as :func:`mammon.investments.security_positions` documents. The report therefore
  shows current holdings valued at prices as of that date; it is not a rewind of
  the portfolio to a past date.
- Return of capital is NOT accumulated by the replay (it silently reduces cost
  basis), so it is rolled up here from the RTRNCAP transactions directly, using
  the domain layer's own action vocabulary so the two stay in lock-step.
- Hidden accounts follow the ledger's net-worth convention: excluded unless
  ``include_hidden`` is set. Closed (sold-out) positions are included by default
  so their realized gain and income still count; pass ``include_sold=False`` to
  drop them.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from mammon import investments, ledger

_HUNDRED = Decimal("100")


def _validate_date(value: str) -> str:
    try:
        return _dt.date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"as_of must be an ISO date YYYY-MM-DD, got {value!r}")


@dataclass
class HoldingPerformance:
    """One security's performance in one account. Cost fields are integer cents;
    ``quantity``/``price``/``avg_cost``/``pct_return`` are ``Decimal`` or ``None``."""
    account_id: int
    account_name: str
    symbol: str
    quantity: Decimal
    cost_basis: int                  # cents (average cost of remaining shares)
    market_value: int                # cents (0 if unpriced or closed)
    unrealized_pl: Optional[int]     # cents; None if unpriced or closed
    realized_pl: int                 # cents, realized gain/loss booked on sales
    dividends: int                   # cents, dividend + interest income
    return_of_capital: int           # cents, capital returned (RTRNCAP)
    price: Optional[Decimal]         # current per-share price; None if unpriced/closed
    is_open: bool                    # currently held (non-zero shares)
    # Period-bounded gain (cents) over the report's [start, end] window, and the
    # capital base it is measured against. Both are None OUTSIDE period mode (the
    # report was built without a ``start``) and for an unpriced holding -- never a
    # fabricated zero. ``period_gain = value_at(end) - value_at(start) -
    # net_contributions``; see :func:`investment_performance`.
    period_gain: Optional[int] = None
    period_basis: Optional[int] = None

    @property
    def avg_cost(self) -> Optional[Decimal]:
        """Average cost per share in dollars, or ``None`` when no shares held."""
        if self.quantity == 0:
            return None
        return (Decimal(self.cost_basis) / _HUNDRED) / self.quantity

    @property
    def total_pl(self) -> int:
        """Realized plus (when priced & held) unrealized P/L."""
        return self.realized_pl + (self.unrealized_pl or 0)

    @property
    def pct_return(self) -> Optional[Decimal]:
        """Unrealized gain as a percentage of cost basis; ``None`` if unpriced or
        there is no basis to measure against (e.g. a closed position)."""
        if self.unrealized_pl is None or self.cost_basis == 0:
            return None
        return Decimal(self.unrealized_pl) / Decimal(self.cost_basis) * _HUNDRED

    @property
    def period_pct(self) -> Optional[Decimal]:
        """Period gain as a percentage of the capital that was at work (starting
        value plus net capital added during the window); ``None`` outside period
        mode or when there is no base to measure against."""
        if self.period_gain is None or not self.period_basis:
            return None
        return Decimal(self.period_gain) / Decimal(self.period_basis) * _HUNDRED

    @property
    def display_gain(self) -> Optional[int]:
        """The Gain/Loss to render: the period-bounded gain when the report was
        built for a period, else the inception-to-date unrealized P/L."""
        return self.period_gain if self.period_gain is not None else self.unrealized_pl

    @property
    def display_pct(self) -> Optional[Decimal]:
        """The Gain/Loss percent to render: period percent in period mode, else the
        inception-to-date percent return."""
        return self.period_pct if self.period_gain is not None else self.pct_return


@dataclass
class InvestmentPerformanceReport:
    """A portfolio-wide per-holding performance snapshot plus its totals."""
    as_of: Optional[str]                     # None = the ledger's latest valuation date
    account_ids: Optional[list[int]]         # None = every investment account
    holdings: list[HoldingPerformance]
    total_cost_basis: int
    total_market_value: int
    total_unrealized_pl: int
    total_realized_pl: int
    total_dividends: int
    total_return_of_capital: int
    # Portfolio period gain (sum of the per-holding period gains) and its base;
    # both None outside period mode. The lifetime ``total_*`` fields above are
    # unchanged so the report still reconciles with the Holdings window and the
    # inception-based ``investment_performance`` MCP tool.
    total_period_gain: Optional[int] = None
    total_period_basis: Optional[int] = None

    @property
    def total_pl(self) -> int:
        return self.total_realized_pl + self.total_unrealized_pl

    @property
    def pct_return(self) -> Optional[Decimal]:
        """Portfolio unrealized gain over portfolio cost basis, as a percent."""
        if self.total_cost_basis == 0:
            return None
        return Decimal(self.total_unrealized_pl) / Decimal(self.total_cost_basis) * _HUNDRED

    @property
    def display_total_gain(self) -> int:
        """The headline portfolio Gain/Loss: the period total in period mode, else
        the lifetime unrealized total."""
        return (self.total_period_gain if self.total_period_gain is not None
                else self.total_unrealized_pl)

    @property
    def display_total_pct(self) -> Optional[Decimal]:
        """The headline portfolio percent: period percent in period mode, else the
        lifetime percent return."""
        if self.total_period_gain is not None:
            if not self.total_period_basis:
                return None
            return (Decimal(self.total_period_gain)
                    / Decimal(self.total_period_basis) * _HUNDRED)
        return self.pct_return


def _return_of_capital(conn, account_id: int) -> dict:
    """``{symbol: cents}`` of capital returned per security in the account.

    The replay folds a return of capital into a reduced cost basis without keeping
    a running total, so it is summed here from the RTRNCAP transactions. The action
    set is borrowed from :mod:`mammon.investments` (not re-listed) so a new
    return-of-capital alias added there is honoured here automatically."""
    out: dict = {}
    rows = conn.execute(
        "SELECT symbol, action, amount FROM investment_transactions "
        "WHERE account_id=? AND symbol IS NOT NULL AND symbol<>''",
        (account_id,),
    ).fetchall()
    for r in rows:
        if (r["action"] or "").lower() in investments._RTRNCAP_ACTIONS:
            out[r["symbol"]] = out.get(r["symbol"], 0) + abs(int(r["amount"] or 0))
    return out


def investment_performance(conn, as_of: Optional[str] = None, *,
                           start: Optional[str] = None,
                           account_ids: Optional[Iterable[int]] = None,
                           include_hidden: bool = False,
                           include_sold: bool = True,
                           prices: Optional[dict] = None) -> InvestmentPerformanceReport:
    """Consolidated per-holding investment performance across the investment
    accounts (or the subset in ``account_ids``), valued at prices as of ``as_of``
    (default: the ledger's latest valuation date). Pure read; see the module
    docstring for the locked definitions.

    When ``start`` is given (and ``as_of`` bounds the window's end), the report is
    PERIOD-bounded: each open, priced holding also carries a ``period_gain`` =
    ``value_at(end) - value_at(start) - net_contributions`` over ``(start, end]``,
    the gain measured from the period's start rather than from inception. The
    lifetime ``unrealized_pl`` / ``realized_pl`` / totals are still computed
    unchanged, so a caller that passes no ``start`` (the MCP tool, the Holdings
    reconciliation) sees exactly the previous inception-to-date numbers."""
    d = _validate_date(as_of) if as_of is not None else None
    s = _validate_date(start) if start is not None else None
    # Period mode needs BOTH ends: a start to rewind to, and a concrete end date to
    # bound the contribution window and value_at(end). Given only a start, fall
    # back to inception mode rather than compute against an open-ended window.
    period_mode = s is not None and d is not None
    wanted = None if account_ids is None else {int(a) for a in account_ids}
    accounts = [
        a for a in ledger.list_accounts(conn, include_closed=True,
                                        include_hidden=include_hidden)
        if (a["type"] or "") == "investment"
        and (wanted is None or int(a["id"]) in wanted)
    ]

    holdings: list[HoldingPerformance] = []
    for a in accounts:
        aid = int(a["id"])
        roc = _return_of_capital(conn, aid)
        if period_mode:
            start_vals = investments.holding_values_at(conn, aid, s)
            end_vals = investments.holding_values_at(conn, aid, d, prices)
            contribs = investments.net_contributions_by_symbol(conn, aid, s, d)
        for pos in investments.security_positions(conn, aid, d, prices):
            if not include_sold and not pos.is_open:
                continue
            period_gain = None
            period_basis = None
            if period_mode and pos.is_open and pos.price is not None:
                v_start = start_vals.get(pos.symbol, 0)
                v_end = end_vals.get(pos.symbol, 0)
                net = contribs.get(pos.symbol, 0)
                period_gain = v_end - v_start - net
                period_basis = v_start + max(net, 0)
            holdings.append(HoldingPerformance(
                account_id=aid, account_name=a["name"], symbol=pos.symbol,
                quantity=pos.quantity, cost_basis=pos.cost_basis,
                market_value=pos.market_value, unrealized_pl=pos.unrealized_pl,
                realized_pl=pos.realized_pl, dividends=pos.dividends,
                return_of_capital=roc.get(pos.symbol, 0),
                price=pos.price, is_open=pos.is_open,
                period_gain=period_gain, period_basis=period_basis,
            ))

    period_holdings = [h for h in holdings if h.period_gain is not None]
    return InvestmentPerformanceReport(
        as_of=d,
        account_ids=None if wanted is None else sorted(wanted),
        holdings=holdings,
        total_cost_basis=sum(h.cost_basis for h in holdings),
        total_market_value=sum(h.market_value for h in holdings),
        total_unrealized_pl=sum(h.unrealized_pl or 0 for h in holdings),
        total_realized_pl=sum(h.realized_pl for h in holdings),
        total_dividends=sum(h.dividends for h in holdings),
        total_return_of_capital=sum(h.return_of_capital for h in holdings),
        total_period_gain=(sum(h.period_gain for h in period_holdings)
                           if period_mode else None),
        total_period_basis=(sum(h.period_basis or 0 for h in period_holdings)
                            if period_mode else None),
    )

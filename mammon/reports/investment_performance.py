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

    @property
    def total_pl(self) -> int:
        return self.total_realized_pl + self.total_unrealized_pl

    @property
    def pct_return(self) -> Optional[Decimal]:
        """Portfolio unrealized gain over portfolio cost basis, as a percent."""
        if self.total_cost_basis == 0:
            return None
        return Decimal(self.total_unrealized_pl) / Decimal(self.total_cost_basis) * _HUNDRED


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
                           account_ids: Optional[Iterable[int]] = None,
                           include_hidden: bool = False,
                           include_sold: bool = True,
                           prices: Optional[dict] = None) -> InvestmentPerformanceReport:
    """Consolidated per-holding investment performance across the investment
    accounts (or the subset in ``account_ids``), valued at prices as of ``as_of``
    (default: the ledger's latest valuation date). Pure read; see the module
    docstring for the locked definitions."""
    d = _validate_date(as_of) if as_of is not None else None
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
        for pos in investments.security_positions(conn, aid, d, prices):
            if not include_sold and not pos.is_open:
                continue
            holdings.append(HoldingPerformance(
                account_id=aid, account_name=a["name"], symbol=pos.symbol,
                quantity=pos.quantity, cost_basis=pos.cost_basis,
                market_value=pos.market_value, unrealized_pl=pos.unrealized_pl,
                realized_pl=pos.realized_pl, dividends=pos.dividends,
                return_of_capital=roc.get(pos.symbol, 0),
                price=pos.price, is_open=pos.is_open,
            ))

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
    )

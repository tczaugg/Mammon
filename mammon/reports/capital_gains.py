"""mammon.reports.capital_gains -- unrealized capital gains and the tax clock on
the OPEN tax lots (SRD 5.9).

The question this answers is the one a taxable brokerage account asks every
December: of what I hold right now, which shares would be taxed as LONG-term if I
sold them today, which would be taxed as SHORT-term, when does each short lot
cross over, and what does selling before that crossover actually cost me. It is a
READ-ONLY assembly of values the domain layer already computes; it adds no new
money math beyond applying the caller's tax rates, and it touches no write path.

Definitions (locked, so the report always reconciles with the Holdings window):

- One row per OPEN TAX LOT, from :func:`mammon.investments.open_lots` -- the same
  replay behind :func:`mammon.investments.compute_holdings` and the Holdings
  tabs. Per security the rows sum to that position's shares and cost basis, so
  this report and the performance report can never disagree.
- The long/short rule is NOT restated here. ``term`` is read off a hypothetical
  :class:`mammon.investments.RealizedGain` for a sale on the report date, and
  ``long_term_on`` comes from :func:`mammon.investments.long_term_date` -- one
  year and a day after acquisition, because the law counts a sale as long-term
  only when it falls strictly AFTER the anniversary. There is exactly one holding
  period definition in this codebase and both fields read it.
- Cost, value, gain and every tax figure are signed integer cents; share counts
  and per-share prices are ``Decimal``. Tax figures round ROUND_HALF_UP at the
  cents boundary, like all money here.
- ``as_of`` caps only the valuation PRICE and fixes the date the holding period is
  measured to; it is NOT a rewind of the portfolio (exactly as
  :func:`mammon.investments.security_positions` documents). Default: today.
- Shares whose lot history is not known (a position restored from a snapshot
  written before lots were kept) still appear, as one row per security with
  ``acquired=None`` and ``term='unknown'``, carrying whatever shares and basis the
  lots do not account for. They are never silently dropped, because the totals
  have to tie to the Holdings window; they are also never guessed to be long.
- **The tax rates are the CALLER'S, never the ledger's.** The module constants
  below are conventional assumptions for a US taxable account, nothing more.
  Mammon does not know the user's bracket, does not store one, and must never
  infer one from the data; a caller that knows better passes both rates in. The
  rates used are echoed back on the report so a rendering can label the numbers
  as the estimates they are.
- **A tax-deferred account is not in this report at all.** User, 2026-09-19:
  *"401K, IRA and Roth IRA do not pay capital gains."* and, on seeing them
  listed with an empty term, *"if they're not taxed, don't put them in the
  report. That is just a lot of clutter."* An account whose ``tax_treatment`` is
  deferred, Roth or special-purpose
  (:data:`mammon.rebalance.CAPITAL_GAINS_EXEMPT_TREATMENTS`) is SKIPPED whole:
  it contributes no lot rows and no cents to any total or subtotal here,
  including ``total_cost_basis``, ``total_market_value`` and
  ``total_unrealized``. Its only trace is :attr:`CapitalGainsReport.
  excluded_accounts`, the names, rendered as the one-line
  :attr:`CapitalGainsReport.exclusion_note` footnote so the omission is stated
  rather than silent. That means this report deliberately does NOT tie to the
  Holdings window -- Holdings is where the sheltered money is read. The account
  attribute is the USER'S, never guessed from the name --
  ``accounts.tax_treatment``, set in Account Details.
- **Losses count too, and in the opposite direction.** A short-term LOSS is worth
  MORE to realize now than later: capital losses offset capital gains of the same
  term first and then up to $3,000 of ordinary income, so a loss taken at
  short-term rates shelters income taxed at the ordinary rate. For a loss row the
  tax figures are therefore negative (a benefit) and ``extra_tax_if_sold_now`` is
  negative -- waiting costs money rather than saving it -- and the row's
  ``annotation`` says so in words. The totals keep gains and losses in separate
  buckets so netting them is the reader's choice, not this module's.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional

from mammon import investments, ledger, rebalance

_CENT = Decimal("0.01")
_HUNDRED = Decimal("100")

#: Assumed federal LONG-term capital gains rate. 15% is the middle of the three
#: US brackets (0/15/20) and the one most taxable accounts land in. This is an
#: ASSUMPTION the caller overrides with ``long_term_rate=``; the app neither
#: stores nor infers the user's real bracket.
DEFAULT_LONG_TERM_RATE = Decimal("0.15")

#: Assumed ORDINARY income rate, which is what a short-term capital gain is taxed
#: at. 24% is a common middle federal bracket and ignores state tax and the net
#: investment income tax. Also an ASSUMPTION the caller overrides with
#: ``short_term_rate=``.
DEFAULT_ORDINARY_INCOME_RATE = Decimal("0.24")


def _validate_date(value: str) -> str:
    try:
        return _dt.date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"date must be ISO YYYY-MM-DD, got {value!r}")


def _cents(value: Decimal) -> int:
    """Round a Decimal amount ALREADY EXPRESSED IN CENTS to a whole cent, HALF_UP
    (away from zero on a tie, in both directions -- a loss rounds like the gain it
    mirrors).

    Quantize to ``1``, not to ``_CENT``: the input is a cent count, so quantizing
    to 0.01 would leave 1105.50 and then ``int()`` would TRUNCATE it to 1105 --
    a silent round-half-DOWN that only shows up on exact half-cent ties."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _dollars(cents: int) -> str:
    """Plain decimal dollar text for the annotation sentence ('1234.50')."""
    return str((Decimal(cents) / _HUNDRED).quantize(_CENT))


def _pct_text(rate: Decimal) -> str:
    """A rate as a percentage for the annotation ('15%', '22.5%'). Quantized and
    trimmed, never ``Decimal.normalize`` -- that renders 50 as '5E+1'."""
    p = (rate * _HUNDRED).quantize(Decimal("0.1"))
    s = f"{p}"
    return (s[:-2] if s.endswith(".0") else s) + "%"


@dataclass
class LotTaxRow:
    """One open tax lot and its tax clock. Money is signed integer cents;
    ``quantity`` and ``price`` are ``Decimal`` (``price`` ``None`` if unpriced)."""
    account_id: int
    account_name: str
    symbol: str
    security_name: Optional[str]
    acquired: Optional[str]          # ISO acquisition date; None if not known
    quantity: Decimal
    cost_basis: int                  # cents paid for these shares
    price: Optional[Decimal]         # per-share price used to value the lot
    market_value: int                # cents (0 when unpriced)
    unrealized: Optional[int]        # cents, market_value - cost_basis; None unpriced
    term: str                        # 'long' | 'short' | 'unknown'
    long_term_on: Optional[str]      # ISO date the lot turns long-term
    days_to_long: Optional[int]      # days from the report date; 0 once long
    # What the CALLER'S rates say about selling this lot today. None when the lot
    # is unpriced (no gain to tax) -- never a fabricated zero. Negative = a tax
    # BENEFIT, which is what a loss is. ``tax_at_short_rate`` is filled only for a
    # lot that would actually be short-term today; a long lot has no short-rate
    # outcome to compare against and carries ``extra_tax_if_sold_now = 0``.
    tax_at_long_rate: Optional[int] = None
    tax_at_short_rate: Optional[int] = None
    extra_tax_if_sold_now: Optional[int] = None
    annotation: str = ""

    @property
    def is_gain(self) -> bool:
        return (self.unrealized or 0) > 0

    @property
    def is_loss(self) -> bool:
        return (self.unrealized or 0) < 0


@dataclass
class CapitalGainsReport:
    """The open lots of the selected accounts with their holding-period status and
    the tax consequence of selling short, plus the totals. ``long_term_rate`` and
    ``short_term_rate`` are echoed back because every tax figure here is an
    estimate at THOSE rates -- see the module docstring."""
    as_of: str                               # the date the holding period is measured to
    account_ids: Optional[list]              # None = every investment account
    long_term_rate: Decimal
    short_term_rate: Decimal
    lots: list = field(default_factory=list)  # list[LotTaxRow], by symbol then date
    total_cost_basis: int = 0
    total_market_value: int = 0
    # Gains and losses stay in separate buckets: netting them is a tax question
    # (wash sales, carry-forwards, the $3,000 ordinary-income cap) this module
    # does not answer. ``*_net`` is provided for convenience only.
    total_long_term_gain: int = 0            # cents, sum of the POSITIVE long lots
    total_long_term_loss: int = 0            # cents, negative
    total_short_term_gain: int = 0           # cents, sum of the POSITIVE short lots
    total_short_term_loss: int = 0           # cents, negative
    total_unknown_term: int = 0              # cents, lots with no known acquisition
    #: Names of the tax-deferred/Roth/special accounts LEFT OUT of this report,
    #: in account order. They contribute no row and no cents anywhere above; this
    #: list exists only so :attr:`exclusion_note` can say the omission out loud
    #: instead of letting a user wonder where the 401(k) went.
    excluded_accounts: list = field(default_factory=list)
    #: Aggregate of every short-term row's ``extra_tax_if_sold_now``: what selling
    #: the whole short-term book TODAY costs over waiting for each lot to turn
    #: long. Short-term losses push it DOWN (they are worth more now), which is
    #: the honest answer for the book as a whole.
    total_extra_tax_if_sold_now: int = 0

    @property
    def total_long_term_net(self) -> int:
        return self.total_long_term_gain + self.total_long_term_loss

    @property
    def total_short_term_net(self) -> int:
        return self.total_short_term_gain + self.total_short_term_loss

    @property
    def total_unrealized(self) -> int:
        return (self.total_long_term_net + self.total_short_term_net
                + self.total_unknown_term)

    @property
    def exclusion_note(self) -> Optional[str]:
        """One line naming the accounts this report left out, or ``None`` when
        it left none out. It is a SENTENCE for a footnote, deliberately not a
        row: the user's complaint was that sheltered holdings in the table were
        "just a lot of clutter", and a total row would re-introduce them."""
        if not self.excluded_accounts:
            return None
        return ("Excluded (not subject to capital gains): "
                + ", ".join(self.excluded_accounts))

    @property
    def short_term_lots(self) -> list:
        return [r for r in self.lots if r.term == "short"]

    @property
    def long_term_lots(self) -> list:
        return [r for r in self.lots if r.term == "long"]


def _term_of(symbol: str, acquired: Optional[str], on: str) -> str:
    """The holding-period verdict for a hypothetical sale of this lot on ``on``.

    Deliberately routed through :class:`mammon.investments.RealizedGain` rather
    than re-implemented: that class is where 'long' is defined for the realized
    side, and a second copy of the rule here is exactly how the two views drift
    apart a leap year from now."""
    return investments.RealizedGain(
        sale_txn_id=None, symbol=symbol, acquired=acquired, sold=on,
        quantity=Decimal(0), proceeds=0, basis=0,
    ).term


def _annotate(row: LotTaxRow, long_rate: Decimal, short_rate: Decimal) -> str:
    """The plain-language tax consequence of selling this lot today."""
    if row.unrealized is None:
        return ("No price on record for this lot, so its gain and the tax on it "
                "cannot be estimated.")
    if row.term == "unknown":
        return ("Acquisition date unknown for these shares, so the holding period "
                "cannot be determined; treat the term as unsettled until the "
                "purchase is recorded.")
    amount = _dollars(abs(row.unrealized))
    if row.term == "long":
        if row.unrealized >= 0:
            return (f"Long-term already: a ${amount} gain sold now is taxed at the "
                    f"long-term rate ({_pct_text(long_rate)}), about "
                    f"${_dollars(abs(row.tax_at_long_rate or 0))}. No deadline to beat.")
        return (f"Long-term already: selling now realizes a ${amount} LONG-term "
                f"loss, which offsets long-term gains first. No deadline to beat.")
    # Short-term.
    when = f"long-term on {row.long_term_on}"
    days = row.days_to_long
    if days is not None:
        when += f" ({days} day{'' if days == 1 else 's'} away)"
    if row.unrealized > 0:
        return (f"Selling now taxes a ${amount} gain as ORDINARY income at "
                f"{_pct_text(short_rate)} (${_dollars(abs(row.tax_at_short_rate or 0))}) "
                f"instead of {_pct_text(long_rate)} "
                f"(${_dollars(abs(row.tax_at_long_rate or 0))}) -- about "
                f"${_dollars(abs(row.extra_tax_if_sold_now or 0))} more tax. Turns {when}.")
    if row.unrealized < 0:
        return (f"Selling now realizes a ${amount} SHORT-term loss, which offsets "
                f"short-term gains and then ordinary income -- worth about "
                f"${_dollars(abs(row.extra_tax_if_sold_now or 0))} MORE as a deduction "
                f"than the same loss taken after it turns {when}.")
    return f"No gain or loss on this lot at today's price. Turns {when}."


def _allocate(total: int, weights: list) -> list:
    """Split ``total`` cents across ``weights`` (Decimal shares) in proportion,
    the last slice absorbing the rounding so the parts always sum to the whole --
    the same discipline :func:`mammon.investments._spread_cost` uses on lots."""
    if not weights:
        return []
    denom = sum(weights, Decimal(0))
    if denom <= 0:
        return [0] * len(weights)
    out: list = []
    allotted = 0
    for i, w in enumerate(weights):
        c = (total - allotted if i == len(weights) - 1
             else _cents(Decimal(total) * w / denom))
        allotted += c
        out.append(c)
    return out


def capital_gains(conn, as_of: Optional[str] = None, *,
                  account_ids: Optional[Iterable[int]] = None,
                  include_hidden: bool = False,
                  prices: Optional[dict] = None,
                  long_term_rate: Decimal = DEFAULT_LONG_TERM_RATE,
                  short_term_rate: Decimal = DEFAULT_ORDINARY_INCOME_RATE,
                  ) -> CapitalGainsReport:
    """Unrealized capital gains per OPEN TAX LOT across the investment accounts
    (or the subset in ``account_ids``), with each lot's long/short status, the
    date a short lot turns long-term, the days remaining, and the extra tax that
    selling it today at ordinary rates would cost. Pure read.

    ``as_of`` (ISO, default today) both caps the valuation price and fixes the
    date the holding period is measured to -- it does NOT rewind the share counts;
    see the module docstring. ``prices`` overrides the recorded price history as
    ``{symbol: Decimal}``, exactly as the performance report takes it.

    ``long_term_rate`` and ``short_term_rate`` are the CALLER'S marginal rates as
    Decimal fractions (0.15 = 15%). The defaults are conventional assumptions
    documented at the top of this module; nothing here reads a bracket out of the
    database, because the ledger does not know one."""
    d = _validate_date(as_of) if as_of is not None else None
    on = d or _dt.date.today().isoformat()
    long_rate = Decimal(str(long_term_rate))
    short_rate = Decimal(str(short_term_rate))
    wanted = None if account_ids is None else {int(a) for a in account_ids}
    accounts = [
        a for a in ledger.list_accounts(conn, include_closed=True,
                                        include_hidden=include_hidden)
        if (a["type"] or "") == "investment"
        and (wanted is None or int(a["id"]) in wanted)
    ]

    rows: list[LotTaxRow] = []
    excluded_names: list = []
    for a in accounts:
        aid = int(a["id"])
        # The USER'S answer, off the account row -- never a guess from the name.
        # An exempt account is skipped WHOLE, before a single lot is built: it
        # owes no capital gains, so every row and every cent of it would be
        # clutter in a report about capital gains. Only the name survives, for
        # the footnote.
        if rebalance.is_capital_gains_exempt(a):
            if a["name"] not in excluded_names:
                excluded_names.append(a["name"])
            continue
        lots_by_symbol = investments.open_lots(conn, aid)
        for pos in investments.held_positions(conn, aid, d, prices):
            if pos.quantity <= 0:            # a written option: no holding period
                continue
            lots = lots_by_symbol.get(pos.symbol, [])
            quantities = [lot.quantity for lot in lots]
            bases = [lot.cost_basis for lot in lots]
            acquired = [lot.acquired for lot in lots]
            # Shares the lot history does not account for (a pre-lot snapshot)
            # become one 'unknown' row, so the rows still sum to the position.
            residue = pos.quantity - sum(quantities, Decimal(0))
            if residue > 0:
                quantities.append(residue)
                bases.append(pos.cost_basis - sum(bases))
                acquired.append(None)
            values = _allocate(pos.market_value, quantities)
            for qty, basis, acq, value in zip(quantities, bases, acquired, values):
                unrealized = None if pos.price is None else value - basis
                term = _term_of(pos.symbol, acq, on)
                long_on = None if not acq else investments.long_term_date(acq)
                days = None
                if long_on is not None:
                    days = max(0, (_dt.date.fromisoformat(long_on)
                                   - _dt.date.fromisoformat(on)).days)
                row = LotTaxRow(
                    account_id=aid, account_name=a["name"], symbol=pos.symbol,
                    security_name=_security_name(conn, pos.symbol),
                    acquired=acq, quantity=qty, cost_basis=basis,
                    price=pos.price, market_value=value, unrealized=unrealized,
                    term=term, long_term_on=long_on, days_to_long=days,
                )
                if unrealized is not None and term != "unknown":
                    row.tax_at_long_rate = _cents(Decimal(unrealized) * long_rate)
                    if term == "short":
                        row.tax_at_short_rate = _cents(Decimal(unrealized) * short_rate)
                        # The difference of the two ROUNDED figures, so the three
                        # numbers a reader sees always add up on screen.
                        row.extra_tax_if_sold_now = (row.tax_at_short_rate
                                                     - row.tax_at_long_rate)
                    else:
                        row.extra_tax_if_sold_now = 0
                row.annotation = _annotate(row, long_rate, short_rate)
                rows.append(row)

    rows.sort(key=lambda r: (r.symbol, r.acquired or "", r.account_name, -r.cost_basis))
    report = CapitalGainsReport(
        as_of=on, account_ids=None if wanted is None else sorted(wanted),
        long_term_rate=long_rate, short_term_rate=short_rate, lots=rows,
        total_cost_basis=sum(r.cost_basis for r in rows),
        total_market_value=sum(r.market_value for r in rows),
        excluded_accounts=excluded_names,
    )
    for r in rows:
        g = r.unrealized or 0
        if r.term == "unknown":
            report.total_unknown_term += g
        elif r.term == "long":
            if g >= 0:
                report.total_long_term_gain += g
            else:
                report.total_long_term_loss += g
        else:
            if g >= 0:
                report.total_short_term_gain += g
            else:
                report.total_short_term_loss += g
            report.total_extra_tax_if_sold_now += r.extra_tax_if_sold_now or 0
    return report


def _security_name(conn, symbol: str) -> Optional[str]:
    row = conn.execute("SELECT name FROM securities WHERE symbol=?",
                       (symbol,)).fetchone()
    return row["name"] if row is not None else None

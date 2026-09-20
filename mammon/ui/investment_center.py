"""The Investment Center, Phase 2: a home-area PAGE (not a modal) that answers
the questions a portfolio owner asks at a glance -- "what is it worth?", "what
is most of it in?", "how is it spread across asset classes?", "is that still the
mix I chose?", "which account earned what?", "what has happened lately?" and
"can I even trust these numbers today?" -- with a portfolio-value card, a
top-holdings table, an allocation-by-asset-class table, a rebalance-drift table,
a per-account performance table, a recent-activity table and a
price-data-freshness table.

Why a page and not a dialog: the calendar established the shape
(:class:`mammon.ui.projection_dialogs.CalendarPanel`) -- a widget that lives in
the main window's stacked home area, outlives the writes going on around it,
and is invalidated by :meth:`mark_stale` rather than rebuilt per glance. This
class is duck-typed against the same four things the main window calls, so
Phase 2 can add it to the stack and a View-menu action without touching this
file: ``__init__(conn, parent=None)``, ``mark_stale()``,
``refresh_if_stale()``, plus a ``showEvent`` that catches up before the user
sees a stale number. It is NOT wired into the window yet, deliberately -- this
file and its test are the whole of Phases 1 and 2. Every panel is redrawn by
the one :meth:`InvestmentCenterPanel.refresh`, so a panel added here cannot
forget to participate in the staleness contract.

Panels deliberately NOT here: the design doc's remaining proposals (movers,
income, watchlist) are later phases, and a *day*
change card is recommended against outright -- the price series is close-only
and backfilled monthly for many symbols, so with no prior-close concept in the
schema a "today" delta would read as zero or noise on most days. The watchlist
is the one panel that would need a migration; there is none here, on purpose.

No SQL and no money logic live in this file, per CLAUDE.md. Every figure comes
from the domain layer -- :func:`mammon.investments.account_valuation` for each
account's cash/securities/holdings split, :func:`mammon.portfolio.scope_account_ids`
for which accounts count, :func:`mammon.investments.valuation_as_of` for the
date -- and :func:`portfolio_summary` only *sums* those results across accounts
and merges a symbol held in two accounts into one row. That summing is done
here rather than in ``investments.py`` because Phase 1 adds no domain surface;
if a second caller ever needs it, it belongs in the domain layer, not a copy.

One source, not two: the card total and the top-holdings table are both derived
from the SAME ``account_valuation`` call per account. Taking the total from one
aggregation and the rows from another (``portfolio.allocation``, say, which
excludes option contracts and can fold money-market funds into cash) would let
the card and the rows under it disagree by a value the user can see but not
explain.

Which is exactly why the allocation panel is presented on ITS OWN terms.
:func:`mammon.portfolio.allocation` IS that different aggregation -- option
contracts are excluded (a contract is not shares of its underlying) and a
money-market fund can count as cash -- so its total need not equal the card's,
and the panel must not be "fixed" by re-sourcing either side from the other.
Instead every slice is a percentage of the allocation's own total, and
:data:`ALLOCATION_BASIS` says so on screen, next to that total: a labelled
difference the user can read beats an unexplained one.

The rebalance-drift table is the allocation panel's question turned around:
not "what is the mix" but "is it still the one you chose". Every figure in it is
:func:`mammon.rebalance.drift`'s -- the target weights, the current weights, the
signed drift, the cents to move and the in/out-of-band verdict (the 5/25 rule,
:func:`mammon.rebalance.in_band`) -- and :func:`rebalance_drift` only quantizes
the percentages to the one decimal the cells show and withholds an action from
the unclassified bucket. Its basis is narrower than every other panel's and
:data:`DRIFT_BASIS` says so: percentages are of the TARGET'S SLEEVE (the
accounts that target governs), not of the portfolio value in the card, and the
property/fixed rows the report carries are context only -- they cannot be traded
to hit a weight, so they are named beside the table and never inside a
percentage.

That domain function RAISES when there is no target at all, rather than
returning an empty report that would read as "on target". This page turns that
refusal into the inline :data:`DRIFT_NO_TARGET_TEXT`, quoting the domain's own
reason: a page that showed a blank drift table would be telling the user their
portfolio is on a target they never set. No dialog, no traceback -- there is no
``exec_()`` on this path either.

The performance table is likewise the domain's own arithmetic --
:func:`mammon.portfolio.account_performance` per account, and
:func:`mammon.portfolio.combine_performances` for the all-accounts row, which
pools the dated flows instead of averaging rates. One honest refusal lives
here: when an account's starting value AND its money in are both zero over the
period -- the ledger funded it with an opening balance, which is not an
external flow -- the domain reports no ``gain_pct``, because the "gain" would
just be the funding arriving. Both the Gain and the Return cell show
:data:`UNPRICED_MARK` in that case rather than a number that would read as an
infinite return.

The recent-activity table is the page's only backward-looking list: the last
:data:`ACTIVITY_N` investment transactions across the scoped accounts, newest
first, named by ACCOUNT rather than numbered by id. Its ordering, its void
filter and those names are all
:func:`mammon.portfolio.recent_investment_activity`'s -- one ordered query
across every account, because a per-account query merged here is exactly the
merge that gets the same-day tie-break wrong.

The freshness table is where the rest of the page admits its own weakness. Every
total above it values an unpriced position at zero and a stale one at its last
known close, silently; this panel names the symbols that happens to, with the
age of each newest close and the three states (:data:`FRESHNESS_CURRENT`,
:data:`FRESHNESS_STALE`, :data:`FRESHNESS_NO_PRICE`) it sorts them into, worst
first. Ages are measured against the page's as-of date -- the ledger's own
latest known date -- and NOT today's clock, so an archived file does not report
every symbol as years stale merely because time passed outside it. Like
:data:`ALLOCATION_BASIS`, :data:`FRESHNESS_BASIS` states the threshold and that
reference date ON SCREEN: a threshold hidden in a tooltip is read as whatever
the reader assumed it was. The threshold lives in this file, not the domain,
because "too old" is a presentation judgement -- :func:`mammon.portfolio.price_freshness`
reports only a date and an age, and asks each symbol over its whole canonical
identity so a renamed ticker is not called unpriced while the card happily
prices it.

This page never opens a dialog. Everything it has to say -- no investment
accounts, no valuation date, symbols it could not price -- it says in an inline
label, which is also why it is safe under the offscreen platform: there is no
``exec_()`` here to block a headless test forever.

Money is signed integer cents until the display edge (:func:`mammon.ui.models.fmt_money`
/ ``fmt_cents``); share quantities and prices are Decimal and are formatted
without ever becoming a float; the as-of date is rendered through
:func:`mammon.ui.models.fmt_date`, never a hardcoded format.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mammon import investments, ledger, portfolio, rebalance
from mammon.ui.models import fmt_cents, fmt_date, fmt_money

#: Columns of the top-holdings table, in order. Tests index by these.
COLUMNS = ("Symbol", "Name", "Shares", "Price", "Market Value", "% of Securities")

#: Columns of the allocation table, in order.
ALLOCATION_COLUMNS = ("Asset Class", "Value", "% of Allocation")

#: Columns of the rebalance-drift table, in order.
DRIFT_COLUMNS = ("Asset Class", "Value", "Target %", "Current %", "Drift",
                 "Action", "Amount", "Band")

#: Printed in the Band column. A row the domain does not judge -- the
#: unclassified bucket -- gets :data:`UNPRICED_MARK` instead, never "in band":
#: "in band" would claim a verdict nobody reached.
DRIFT_OUT_OF_BAND = "out of band"
DRIFT_IN_BAND = "in band"

#: Shown when :func:`mammon.rebalance.drift` refuses, quoting its own reason.
#: The domain raises rather than returning an empty report, and an empty drift
#: table would read as "on target" against a target that does not exist.
DRIFT_NO_TARGET_TEXT = (
    "No rebalance drift to show -- {reason}.\n\n"
    "A target is the mix you are aiming at: one weight per asset class, over "
    "the accounts it governs. Set one up in the Target & Drift editor and make "
    "it active, and this panel will show how far each class has drifted from it "
    "and what it would take to get back."
)

DRIFT_EMPTY_TEXT = (
    "Nothing to measure against \"{target}\" yet: it carries no asset-class "
    "weights, and the accounts it governs hold nothing to weigh."
)

#: Printed under the drift table. Its basis is the narrowest on this page -- the
#: TARGET's sleeve, not the portfolio value in the card -- and, like
#: :data:`ALLOCATION_BASIS`, it is stated on screen rather than assumed.
DRIFT_BASIS = (
    "Percentages are of \"{target}\"'s own sleeve, {total} as of {as_of} -- the "
    "accounts that target governs, not everything owned above. A class reads "
    "\"{flag}\" once it is {abs_pp} percentage points or more away from its "
    "target weight, or {rel_pct} percent or more of that weight away from it, "
    "whichever is tighter."
)

#: Fixed assets come back from the domain as context beside the sleeve. They are
#: named, with their total, precisely so nobody reads their absence from the
#: percentages as an omission.
DRIFT_FIXED_TEXT = (
    "Context only, outside the sleeve and outside every percentage here: "
    "{rows} ({total} of property and other fixed assets, which cannot be traded "
    "to hit a weight)."
)

DRIFT_TO_MOVE_TEXT = (
    "{n} {noun} out of band; returning to target would move {total}."
)

DRIFT_ON_TARGET_TEXT = "Every class is inside its band -- nothing to rebalance."

#: Printed when the target's weights do not add to 100: the current column is
#: still honest, but the drift column is measured against an incomplete target.
DRIFT_INCOMPLETE_TEXT = (
    "\"{target}\"'s weights add to {total}, not 100% -- the missing weight "
    "shows up as drift spread across the classes below."
)

#: Printed when any row is the unclassified bucket, to explain its blank verdict.
DRIFT_UNCLASSIFIED_TEXT = (
    "{mark} where holdings have no asset class yet: an unclassified bucket is a "
    "gap in the records, not a position off its weight, so it is never judged "
    "against a band and never given a trade."
)

DRIFT_TARGET_LINE = "Target \"{target}\", as of {as_of}"

#: Columns of the per-account performance table, in order.
PERFORMANCE_COLUMNS = ("Account", "Start Value", "End Value", "Money In",
                       "Money Out", "Income", "Gain", "Return")

#: The performance table's default window: the twelve months ending on the
#: valuation date. A glance wants one period, and a year is the period a
#: statement, a tax form and the user's own question all use.
PERFORMANCE_MONTHS = 12

#: The label of the all-accounts row, and the name shown for an account whose
#: row has gone missing (deleted between the scope query and the name lookup).
TOTAL_ROW_LABEL = "All accounts"

#: How many positions the top-holdings table shows. "Top holdings" is a glance,
#: not a holdings report -- the Holdings window already lists everything.
TOP_N = 10

#: Shown where a number would be if the symbol has no price at all.
UNPRICED_MARK = "--"

EMPTY_TEXT = (
    "No investment accounts yet.\n\n"
    "Create an investment or crypto account, record what you bought, and this "
    "page will show what the portfolio is worth and which positions carry it."
)

NO_DATE_TEXT = "nothing to value yet"

#: Printed under the allocation table, with the allocation's own total
#: substituted. The panel's whole defence against looking like it contradicts
#: the card above it (see the module docstring).
ALLOCATION_BASIS = (
    "Percentages are of this allocation's own total, {total} -- not the "
    "portfolio value above. Option contracts are excluded and a money-market "
    "fund can count as cash, so the two totals need not agree."
)

ALLOCATION_EMPTY_TEXT = (
    "Nothing to allocate yet: the scoped accounts hold no cash and no priced "
    "security."
)

PERFORMANCE_EMPTY_TEXT = (
    "No period to measure yet: the ledger has no valuation date, so there is "
    "no year to look back over."
)

#: Printed under the performance table when at least one row had no capital
#: base (see the module docstring's "honest refusal").
NO_BASE_TEXT = (
    "{marker} where an account had no starting value and no money in during "
    "the period -- an opening balance is not an external flow, so its gain "
    "would only be the funding arriving."
)

#: Columns of the recent-activity table, in order.
ACTIVITY_COLUMNS = ("Date", "Account", "Action", "Symbol", "Quantity", "Amount")

#: How many recent investment transactions the activity table shows. A glance
#: at "what has been happening lately", not a register -- the account's own
#: register is one click away and lists everything.
ACTIVITY_N = 15

ACTIVITY_EMPTY_TEXT = (
    "No investment transactions yet: the scoped accounts hold nothing that was "
    "bought, sold or paid out."
)

#: Printed under the activity table so the user knows the list is truncated and
#: is not the whole history.
ACTIVITY_BASIS = (
    "The {n} most recent investment transactions across every account on this "
    "page, newest first. Voided rows are not shown."
)

#: Columns of the price-freshness table, in order.
FRESHNESS_COLUMNS = ("Symbol", "Name", "Last Price", "Days Old", "Status")

#: How old a symbol's newest close may be before this page calls it stale.
#: A month is the interval at which a fund that only prices monthly still
#: reports, so anything older is genuinely missing rather than merely slow.
STALE_AFTER_DAYS = 30

#: Statuses the freshness table prints, worst first.
FRESHNESS_NO_PRICE = "no price"
FRESHNESS_STALE = "stale"
FRESHNESS_CURRENT = "current"

#: Printed under the freshness table. States the threshold and the reference
#: date on screen rather than in a tooltip, for the same reason
#: :data:`ALLOCATION_BASIS` does: a number whose meaning is hidden gets read as
#: the meaning the reader assumed.
FRESHNESS_BASIS = (
    "Age is measured against this page's as-of date, {as_of} -- the ledger's "
    "own latest known date, not today. A symbol is called \"{stale}\" once its "
    "newest close is more than {days} days old, and \"{none}\" when no close "
    "was ever recorded, in which case its shares are valued at zero above."
)

FRESHNESS_EMPTY_TEXT = (
    "No positions to price: the scoped accounts hold no security."
)


# ---------------------------------------------------------------------------
# Display helpers (Decimal in, str out -- no float anywhere on this path)
# ---------------------------------------------------------------------------
def fmt_qty(qty) -> str:
    """A Decimal share quantity -> display text with thousands separators.

    Whole share counts render bare (``100``); fractional ones keep up to six
    decimals with trailing zeros trimmed, because a crypto position can be
    ``0.00374`` of a coin and a DRIP position ``12.3456`` shares. Never float:
    the value stays Decimal right up to ``format``.
    """
    d = qty if isinstance(qty, Decimal) else Decimal(str(qty))
    if d == d.to_integral_value():
        return f"{d.to_integral_value():,}"
    exp = d.as_tuple().exponent
    if isinstance(exp, int) and -exp > 6:
        d = d.quantize(Decimal("0.000001"))
    text = f"{d:,f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def fmt_price(price) -> str:
    """A Decimal per-share price -> display text. ``None`` (no price recorded)
    becomes :data:`UNPRICED_MARK` rather than a misleading zero."""
    if price is None:
        return UNPRICED_MARK
    d = price if isinstance(price, Decimal) else Decimal(str(price))
    d = d.quantize(Decimal("0.0001"))
    text = f"{d:,f}"
    if text.endswith("00") and "." in text and len(text.split(".")[1]) == 4:
        text = text[:-2]          # 4dp is the storage precision, 2dp the usual truth
    return text


def fmt_pct(pct: Optional[Decimal]) -> str:
    """A Decimal percentage -> ``12.3%``; ``None`` (nothing to be a percent of)
    -> :data:`UNPRICED_MARK`."""
    if pct is None:
        return UNPRICED_MARK
    return f"{pct:.1f}%"


def fmt_points(pp: Optional[Decimal]) -> str:
    """A signed drift in percentage POINTS -> ``+3.4 pp``. The sign is always
    printed: the whole value of the column is knowing which side of the target a
    class sits on, and an unsigned ``3.4`` reads as a magnitude."""
    if pp is None:
        return UNPRICED_MARK
    return f"{pp:+.1f} pp"


def fmt_move(cents: Optional[int]) -> str:
    """Cents to move -> ``+10,000.00`` to buy, ``-10,000.00`` to sell.

    The plus is explicit because the column sits next to an ``Action`` word and
    an unsigned figure beside "sell" invites reading the sale as a gain.
    ``None`` -- a row the domain gives no trade for -- is :data:`UNPRICED_MARK`,
    not a zero that would read as "already on target".
    """
    if cents is None:
        return UNPRICED_MARK
    return ("+" if cents > 0 else "") + fmt_cents(cents)


def _band_text(pct) -> str:
    """A band threshold as brief prose: ``5``, not ``5.0``.

    Deliberately NOT ``Decimal.normalize()``, which renders ``Decimal("50")`` as
    ``5E+1`` and would put scientific notation in a sentence.
    """
    if pct is None:
        return UNPRICED_MARK
    d = pct if isinstance(pct, Decimal) else Decimal(str(pct))
    text = f"{d.quantize(Decimal('0.1')):f}"
    return text[:-2] if text.endswith(".0") else text


def _quantize_pct(pct) -> Optional[Decimal]:
    """A domain percentage -> the one decimal the cells show, in Decimal.

    The same contract as :func:`_share` and :func:`_as_return`: the domain's
    full-precision figure is a computation input, the quantized one is what the
    screen and the tests agree on. Quantizing here rather than inside ``format``
    means a caller comparing two cells is comparing the numbers the user sees.
    """
    if pct is None:
        return None
    d = pct if isinstance(pct, Decimal) else Decimal(str(pct))
    return d.quantize(Decimal("0.1"))


def _row_text(table, columns: int, row: int) -> tuple:
    """One table row's rendered text, left to right. A cell never written is
    the empty string rather than an ``AttributeError`` on ``None``."""
    return tuple(
        (table.item(row, col).text() if table.item(row, col) else "")
        for col in range(columns)
    )


def _share(value: int, whole: int) -> Optional[Decimal]:
    """``value`` as a percentage of ``whole``, in Decimal so the presentation
    layer never introduces a float. ``None`` when there is no whole."""
    if not whole:
        return None
    return (Decimal(value) * 100 / Decimal(whole)).quantize(Decimal("0.1"))


# ---------------------------------------------------------------------------
# Composition over the domain layer
# ---------------------------------------------------------------------------
@dataclass
class TopHolding:
    """One position across the whole scoped portfolio: the same symbol held in
    two accounts is ONE row here, its shares and value summed."""

    symbol: str
    name: str
    quantity: Decimal
    price: Optional[Decimal]         # None if the symbol has no recorded price
    market_value: int                # cents (0 when unpriced)
    cost_basis: int                  # cents
    gain: Optional[int]              # cents, None when unpriced
    pct: Optional[Decimal] = None    # share of the portfolio's securities value


@dataclass
class PortfolioSummary:
    """What the page draws: the card's three totals, the holdings behind them,
    and the symbols that had to be left out for want of a price."""

    as_of: Optional[str]
    account_ids: list = field(default_factory=list)
    cash: int = 0                    # cents
    securities: int = 0              # cents
    total: int = 0                   # cents (cash + securities)
    holdings: list = field(default_factory=list)   # list[TopHolding], largest first
    unpriced: list = field(default_factory=list)   # symbols, sorted

    @property
    def is_empty(self) -> bool:
        """True when the ledger has no investment accounts at all -- the page
        shows its explanatory empty state instead of a card full of zeros."""
        return not self.account_ids


def portfolio_summary(conn, as_of: Optional[str] = None, account_ids=None,
                      include_hidden: bool = False) -> PortfolioSummary:
    """Roll the scoped investment accounts up into what this page displays.

    Every figure comes from :func:`mammon.investments.account_valuation`, once
    per account; this function only adds cents together and merges a symbol
    held in more than one account. ``account_ids`` overrides the default scope
    (:func:`mammon.portfolio.scope_account_ids` with the ``investments`` scope,
    which is investment- and crypto-type accounts, closed and hidden ones
    excluded). ``as_of`` defaults to
    :func:`mammon.investments.valuation_as_of` -- the latest date the ledger
    knows anything about, price or transaction.
    """
    if account_ids is None:
        ids = portfolio.scope_account_ids(conn, "investments",
                                          include_hidden=include_hidden)
    else:
        ids = [int(a) for a in account_ids]
    when = as_of or investments.valuation_as_of(conn)
    out = PortfolioSummary(as_of=when, account_ids=list(ids))
    if not ids:
        return out

    names = _security_names(conn)
    merged: dict = {}
    unpriced: set = set()
    for account_id in ids:
        valuation = investments.account_valuation(conn, account_id, when)
        out.cash += valuation.cash
        out.securities += valuation.securities
        out.total += valuation.total
        unpriced.update(valuation.unpriced)
        for hv in valuation.holdings:
            held = merged.get(hv.symbol)
            if held is None:
                merged[hv.symbol] = TopHolding(
                    symbol=hv.symbol, name=names.get(hv.symbol, ""),
                    quantity=Decimal(hv.quantity), price=hv.price,
                    market_value=hv.market_value, cost_basis=hv.cost_basis,
                    gain=hv.gain,
                )
                continue
            held.quantity += Decimal(hv.quantity)
            held.market_value += hv.market_value
            held.cost_basis += hv.cost_basis
            # The price is per-symbol, not per-account: whichever leg knows it
            # speaks for both, and a gain is only claimed once every leg is
            # priced (otherwise it would understate by the unpriced shares).
            if held.price is None:
                held.price = hv.price
            held.gain = None if (held.gain is None or hv.gain is None) else held.gain + hv.gain

    holdings = sorted(merged.values(), key=lambda h: (-h.market_value, h.symbol))
    for h in holdings:
        h.pct = _share(h.market_value, out.securities)
    out.holdings = holdings
    out.unpriced = sorted(unpriced)
    return out


def _security_names(conn) -> dict:
    """symbol -> descriptive name, for the table's Name column. A symbol with
    no ``securities`` row (or a nameless one) simply shows no name; the page
    never withholds a position because nobody typed a name for it."""
    out = {}
    for row in portfolio.list_securities(conn):
        try:
            out[row["symbol"]] = row["name"] or ""
        except (IndexError, KeyError):      # a row shape without a name column
            continue
    return out


# ---------------------------------------------------------------------------
# Allocation by asset class (a DIFFERENT aggregation -- see the module docstring)
# ---------------------------------------------------------------------------
@dataclass
class AllocationSlice:
    """One asset class's share of the allocation."""

    key: str                         # e.g. "domestic_stock", "unclassified"
    label: str                       # its display label, from the domain layer
    value: int                       # cents
    pct: Optional[Decimal]           # of the ALLOCATION's total, not the card's


@dataclass
class AllocationView:
    """What the allocation panel draws, on its own terms."""

    as_of: Optional[str]
    total: int = 0                   # cents -- the allocation's own total
    rows: list = field(default_factory=list)      # AllocationSlice, largest first
    unpriced: list = field(default_factory=list)  # symbols left out for want of a price
    options_note: str = ""           # the domain's sentence about excluded contracts

    @property
    def is_empty(self) -> bool:
        return not self.rows


def asset_allocation(conn, as_of: Optional[str] = None, account_ids=None,
                     include_hidden: bool = False) -> AllocationView:
    """Compose :func:`mammon.portfolio.allocation` into display rows.

    No money math happens here: the cents are the domain's, and the only thing
    added is each slice's percentage of the domain's own total, computed in
    Decimal by :func:`_share` so the presentation layer never introduces a
    float (``Slice.pct`` is one).

    ``account_ids`` should be the SAME ids the card was summed over, so the two
    panels differ only by the domain rules the basis line names -- never by
    which accounts they looked at.
    """
    when = as_of or investments.valuation_as_of(conn)
    alloc = portfolio.allocation(conn, account_ids=account_ids, as_of=when,
                                 include_hidden=include_hidden)
    rows = [AllocationSlice(s.key, s.label, s.value, _share(s.value, alloc.total))
            for s in alloc.by_class]
    return AllocationView(as_of=when, total=alloc.total, rows=rows,
                          unpriced=list(alloc.unpriced),
                          options_note=alloc.options_note)


# ---------------------------------------------------------------------------
# Rebalance drift (the allocation question turned around -- see the docstring)
# ---------------------------------------------------------------------------
@dataclass
class DriftRow:
    """One asset class measured against the target's weight for it.

    Every figure is :func:`mammon.rebalance.drift`'s. The percentages arrive
    quantized to the decimal the cell shows (:func:`_quantize_pct`) so a caller
    comparing rows compares what the user reads; the cents are untouched.
    """

    key: str                         # e.g. "domestic_stock", "cash", "unclassified"
    label: str                       # the domain's display label
    value: int                       # cents held now
    target_pct: Optional[Decimal]    # of the target's SLEEVE
    current_pct: Optional[Decimal]   # of the same sleeve
    drift_pp: Optional[Decimal]      # signed percentage POINTS, the domain's own
    action: str                      # the domain's verb: buy/sell/invest/raise/hold/classify
    move_cents: Optional[int]        # + buy, - sell; None where no trade applies
    out_of_band: Optional[bool]      # None where the domain passes no verdict
    is_unclassified: bool = False


@dataclass
class DriftView:
    """What the drift panel draws -- or, when ``refusal`` is set, why it cannot.

    ``refusal`` carries :func:`mammon.rebalance.drift`'s own ``ValueError`` text
    verbatim. The domain refuses instead of returning an empty report, because an
    empty drift table reads as "on target"; this view keeps that refusal as data
    so the panel can render it as a label rather than a dialog.
    """

    as_of: Optional[str]
    target_name: str = ""
    sleeve_total: int = 0            # cents -- the basis of every percentage here
    rows: list = field(default_factory=list)      # DriftRow, biggest holding first
    fixed_rows: list = field(default_factory=list)   # (label, cents), context only
    fixed_total: int = 0             # cents
    band_abs_pct: Optional[Decimal] = None
    band_rel_pct: Optional[Decimal] = None
    target_total_pct: Optional[Decimal] = None
    target_is_complete: bool = True
    to_move_cents: int = 0           # cents that would change hands
    out_of_band_count: int = 0
    unpriced: list = field(default_factory=list)
    refusal: str = ""                # the domain's reason, when there is no target

    @property
    def is_empty(self) -> bool:
        return not self.rows

    @property
    def has_unclassified(self) -> bool:
        return any(r.is_unclassified for r in self.rows)


def rebalance_drift(conn, as_of: Optional[str] = None,
                    target_id: Optional[int] = None) -> DriftView:
    """Compose :func:`mammon.rebalance.drift` into display rows.

    No money math and no band arithmetic happen here: the weights, the signed
    drift, the cents to move and the in/out-of-band verdict are all the domain's.
    This function quantizes the percentages for display, withholds the verdict
    and the trade from the unclassified bucket (a gap in the records is not a
    position off its weight), and turns the domain's refusal -- ``ValueError``
    when there is no target to measure against -- into ``refusal`` text the panel
    shows inline.
    """
    when = as_of or investments.valuation_as_of(conn)
    try:
        report = rebalance.drift(conn, target_id=target_id, as_of=when)
    except ValueError as exc:
        return DriftView(as_of=when, refusal=str(exc))

    rows = []
    for r in report.rows:
        unclassified = r.is_unclassified
        rows.append(DriftRow(
            key=r.asset_class,
            label=r.label,
            value=r.current_cents,
            target_pct=_quantize_pct(r.target_pct),
            current_pct=_quantize_pct(r.current_pct),
            drift_pp=None if unclassified else _quantize_pct(r.drift_pct),
            action=r.action,
            move_cents=None if unclassified else r.move_cents,
            out_of_band=None if unclassified else bool(r.out_of_band),
            is_unclassified=unclassified,
        ))
    return DriftView(
        as_of=report.as_of or when,
        target_name=report.target_name,
        sleeve_total=report.sleeve_total,
        rows=rows,
        fixed_rows=list(report.fixed_rows),
        fixed_total=report.fixed_total,
        band_abs_pct=report.band_abs_pct,
        band_rel_pct=report.band_rel_pct,
        target_total_pct=report.target_total_pct,
        target_is_complete=report.target_is_complete,
        to_move_cents=report.to_move_cents,
        out_of_band_count=len(report.out_of_band),
        unpriced=list(report.unpriced),
    )


# ---------------------------------------------------------------------------
# Per-account performance
# ---------------------------------------------------------------------------
@dataclass
class AccountReturn:
    """One account's (or every account's) money over the period."""

    account_id: int
    name: str
    start_value: int                 # cents, the day before the period
    end_value: int                   # cents, at the period's end
    money_in: int                    # cents
    money_out: int                   # cents
    income: int                      # cents of dividends/interest/distributions
    gain: Optional[int]              # cents, None when there was no capital base
    pct: Optional[Decimal]           # gain as a percent of that base, or None
    is_total: bool = False


@dataclass
class PerformanceView:
    """What the performance panel draws: one row per account, then the pooled
    all-accounts row."""

    start: Optional[str]
    end: Optional[str]
    rows: list = field(default_factory=list)      # AccountReturn, largest end value first
    total: Optional[AccountReturn] = None

    @property
    def is_empty(self) -> bool:
        return not self.rows


def period_start(end: str, months: int = PERFORMANCE_MONTHS) -> str:
    """The ISO date the trailing window opens on, so that ``[start, end]`` is
    an inclusive ``months``-long period. Dates only -- no money crosses here.

    The 29th of February is stepped back to the 28th rather than raising, which
    is the whole reason this is a function and not an inline subtraction.
    """
    d = _dt.date.fromisoformat(end)
    year, month = d.year, d.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = d.day
    while day > 1:
        try:
            back = _dt.date(year, month, day)
            break
        except ValueError:
            day -= 1
    else:
        back = _dt.date(year, month, 1)
    return (back + _dt.timedelta(days=1)).isoformat()


def account_returns(conn, as_of: Optional[str] = None, start: Optional[str] = None,
                    account_ids=None, include_hidden: bool = False) -> PerformanceView:
    """Compose :func:`mammon.portfolio.account_performance` over the scoped
    accounts, plus :func:`mammon.portfolio.combine_performances` for the total.

    The rate and every figure in a row are the domain's; this function picks
    the period, looks up account names, orders the rows by size, and drops the
    gain of a row the domain gave no ``gain_pct`` -- the opening-balance case
    described in the module docstring, where a "gain" would just be the account
    being funded.
    """
    when = as_of or investments.valuation_as_of(conn)
    if not when:
        return PerformanceView(start=None, end=None)
    if account_ids is None:
        ids = portfolio.scope_account_ids(conn, "investments",
                                          include_hidden=include_hidden)
    else:
        ids = [int(a) for a in account_ids]
    begin = start or period_start(when)
    view = PerformanceView(start=begin, end=when)
    if not ids:
        return view

    perfs = []
    rows = []
    for account_id in ids:
        perf = portfolio.account_performance(conn, account_id, begin, when)
        perfs.append(perf)
        rows.append(_account_return(conn, account_id, perf))
    view.rows = sorted(rows, key=lambda r: (-r.end_value, r.name))
    view.total = _as_return(portfolio.combine_performances(perfs, when),
                            0, TOTAL_ROW_LABEL, is_total=True)
    return view


def _account_return(conn, account_id: int, perf) -> AccountReturn:
    acct = ledger.get_account(conn, account_id)
    name = (acct["name"] if acct is not None else "") or f"Account {account_id}"
    return _as_return(perf, account_id, name)


def _as_return(perf, account_id: int, name: str, is_total: bool = False) -> AccountReturn:
    """A domain :class:`mammon.portfolio.Performance` -> one display row. The
    gain is withheld exactly when the domain withholds the percentage, so the
    two cells can never disagree about whether there was anything to earn on.

    The percentage is quantized here, to the one decimal the cell shows, the
    way :func:`_share` quantizes the allocation's. The domain's full-precision
    ratio is a computation input, not a displayable figure; rounding it only at
    the rendering edge would leave every caller of this function comparing
    ``10.945...`` against a screen that says ``10.9%``."""
    pct = perf.gain_pct
    if pct is not None:
        pct = pct.quantize(Decimal("0.1"))
    return AccountReturn(account_id=account_id, name=name,
                         start_value=perf.start_value, end_value=perf.end_value,
                         money_in=perf.money_in, money_out=perf.money_out,
                         income=perf.income,
                         gain=None if pct is None else perf.gain,
                         pct=pct, is_total=is_total)


@dataclass
class ActivityView:
    """The recent-activity table's rows (:class:`mammon.portfolio.ActivityRow`)
    and the cap they were fetched under, so the panel can say how many it is
    showing without hardcoding the number twice."""

    rows: list = field(default_factory=list)
    limit: int = ACTIVITY_N

    @property
    def is_empty(self) -> bool:
        return not self.rows


def recent_activity(conn, account_ids=None, limit: Optional[int] = None,
                    include_hidden: bool = False) -> ActivityView:
    """Compose :func:`mammon.portfolio.recent_investment_activity` over the
    scoped accounts. Ordering, the void filter and the account NAMES are all
    the domain's; this function only chooses the cap."""
    n = int(limit) if limit else ACTIVITY_N
    rows = portfolio.recent_investment_activity(
        conn, account_ids=account_ids, limit=n, include_hidden=include_hidden)
    return ActivityView(rows=rows, limit=n)


@dataclass
class FreshnessRow:
    """One held symbol's price currency: when it was last priced, how old that
    is, and which of the three states this page sorts it into."""

    symbol: str
    name: str
    latest: Optional[str]            # ISO date, None if never priced
    days: Optional[int]              # age in days at the as-of date
    status: str                      # one of the FRESHNESS_* constants

    @property
    def is_stale(self) -> bool:
        return self.status != FRESHNESS_CURRENT


@dataclass
class FreshnessView:
    """What the freshness panel draws, plus the date the ages are measured
    against -- which the panel prints, because an age is meaningless without
    the day it was taken from."""

    as_of: Optional[str]
    stale_after: int = STALE_AFTER_DAYS
    rows: list = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.rows

    @property
    def stale(self) -> list:
        """The rows the panel is complaining about, worst first (the domain
        already ordered them that way)."""
        return [r for r in self.rows if r.is_stale]


def price_freshness(conn, holdings, as_of: Optional[str] = None,
                    stale_after: int = STALE_AFTER_DAYS) -> FreshnessView:
    """Compose :func:`mammon.portfolio.price_freshness` over the symbols the
    page is ALREADY holding.

    The symbols come from ``holdings`` (the summary this refresh computed)
    rather than from a second holdings query, so the table can never disagree
    with the one above it about what is held. Names come from the same
    holdings for the same reason.

    The threshold lives here, not in the domain: what counts as "too old" is a
    presentation judgement this page states on screen, and a report that wanted
    a different one would pass its own rather than inherit this page's.
    """
    when = as_of or investments.valuation_as_of(conn)
    rows = list(holdings)
    names = {h.symbol: h.name for h in rows}
    fresh = portfolio.price_freshness(conn, [h.symbol for h in rows], as_of=when)
    out = FreshnessView(as_of=when, stale_after=int(stale_after))
    for f in fresh:
        if f.latest is None:
            status = FRESHNESS_NO_PRICE
        elif f.days is not None and f.days > out.stale_after:
            status = FRESHNESS_STALE
        else:
            # Priced, and either current or undatable (no as-of date at all) --
            # an unknown age is not evidence of staleness.
            status = FRESHNESS_CURRENT
        out.rows.append(FreshnessRow(symbol=f.symbol, name=names.get(f.symbol, ""),
                                     latest=f.latest, days=f.days, status=status))
    return out


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
class InvestmentCenterPanel(QWidget):
    """The Investment Center page: a portfolio-value card over a top-holdings
    table, an allocation-by-asset-class table, a per-account performance table,
    a recent-activity table and a price-freshness table. All of them are
    redrawn by the single :meth:`refresh`.

    Construction takes the connection and nothing else the main window has to
    remember to set later (the calendar page proved the value of that). The
    panel recomputes on construction, when shown after a write, and never
    otherwise -- an edit in a register must not pay for a valuation nobody is
    looking at, which is what :meth:`mark_stale` is for.

    ``as_of`` pins the valuation date (tests do; the app does not) -- left
    ``None``, every refresh re-asks the ledger for its latest known date, so a
    window left open overnight is not still valuing yesterday after an import.
    ``period_start`` likewise pins the performance window's opening date; left
    ``None`` it is the :data:`PERFORMANCE_MONTHS` months ending at ``as_of``,
    so it moves with the valuation date rather than with the wall clock.
    """

    TOP_N = TOP_N
    ACTIVITY_N = ACTIVITY_N
    STALE_AFTER_DAYS = STALE_AFTER_DAYS

    def __init__(self, conn, parent=None, as_of: Optional[str] = None,
                 top_n: Optional[int] = None, period_start: Optional[str] = None):
        super().__init__(parent)
        self.conn = conn
        self._as_of = as_of
        self._period_start = period_start
        self.top_n = int(top_n) if top_n else self.TOP_N
        # Not constructor arguments: how much recent activity to list and when
        # a price is old enough to complain about are this page's editorial
        # choices, not the caller's. A test that wants a different cap sets the
        # attribute before calling refresh().
        self.activity_n = self.ACTIVITY_N
        self.stale_after_days = self.STALE_AFTER_DAYS
        self._stale = False
        self.summary: Optional[PortfolioSummary] = None
        self.allocation_view: Optional[AllocationView] = None
        self.drift_view: Optional[DriftView] = None
        self.performance_view: Optional[PerformanceView] = None
        self.activity_view: Optional[ActivityView] = None
        self.freshness_view: Optional[FreshnessView] = None

        # The title box is a register's: same object names, same insets, so
        # switching between this page and a register does not shift the layout
        # under the user.
        self.header_box = QFrame()
        self.header_box.setObjectName("registerTitleBox")
        top = QHBoxLayout(self.header_box)
        top.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel("Investment Center")
        self.header.setObjectName("registerTitle")
        top.addWidget(self.header)
        top.addStretch(1)
        self.as_of_label = QLabel("")
        self.as_of_label.setObjectName("registerSub")
        top.addWidget(self.as_of_label)

        self.card = self._build_card()

        self.holdings_title = QLabel("Top Holdings")
        font = QFont(self.holdings_title.font())
        font.setBold(True)
        self.holdings_title.setFont(font)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        # Read-only by construction. A picker opened from a delegate's
        # setModelData is the documented heap-corruption path; a glance table
        # has no business editing anything anyway.
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeToContents)
        head.setSectionResizeMode(COLUMNS.index("Name"), QHeaderView.Stretch)

        self.unpriced_label = QLabel("")
        self.unpriced_label.setObjectName("registerSub")
        self.unpriced_label.setWordWrap(True)

        # -- allocation by asset class ---------------------------------------
        self.allocation_title = QLabel("Allocation by Asset Class")
        self.allocation_title.setFont(font)
        self.allocation_table = self._make_table(ALLOCATION_COLUMNS, "Asset Class")
        self.allocation_note = QLabel("")
        self.allocation_note.setObjectName("registerSub")
        self.allocation_note.setWordWrap(True)
        self.allocation_empty = QLabel(ALLOCATION_EMPTY_TEXT)
        self.allocation_empty.setObjectName("registerSub")
        self.allocation_empty.setWordWrap(True)

        # -- rebalance drift ---------------------------------------------------
        self.drift_title = QLabel("Rebalance Drift")
        self.drift_title.setFont(font)
        self.drift_subtitle = QLabel("")
        self.drift_subtitle.setObjectName("registerSub")
        self.drift_table = self._make_table(DRIFT_COLUMNS, "Asset Class")
        self.drift_note = QLabel("")
        self.drift_note.setObjectName("registerSub")
        self.drift_note.setWordWrap(True)
        # Carries both "no target at all" (the domain's refusal, quoted) and
        # "a target with nothing to weigh". Always a label: a modal here would
        # block forever under the offscreen platform, and a page explaining
        # itself should not need dismissing.
        self.drift_empty = QLabel("")
        self.drift_empty.setObjectName("registerSub")
        self.drift_empty.setWordWrap(True)

        # -- per-account performance -----------------------------------------
        self.performance_title = QLabel("Account Performance")
        self.performance_title.setFont(font)
        self.performance_period = QLabel("")
        self.performance_period.setObjectName("registerSub")
        self.performance_table = self._make_table(PERFORMANCE_COLUMNS, "Account")
        self.performance_note = QLabel("")
        self.performance_note.setObjectName("registerSub")
        self.performance_note.setWordWrap(True)
        self.performance_empty = QLabel(PERFORMANCE_EMPTY_TEXT)
        self.performance_empty.setObjectName("registerSub")
        self.performance_empty.setWordWrap(True)

        # -- recent activity ---------------------------------------------------
        self.activity_title = QLabel("Recent Activity")
        self.activity_title.setFont(font)
        self.activity_table = self._make_table(ACTIVITY_COLUMNS, "Account")
        self.activity_note = QLabel("")
        self.activity_note.setObjectName("registerSub")
        self.activity_note.setWordWrap(True)
        self.activity_empty = QLabel(ACTIVITY_EMPTY_TEXT)
        self.activity_empty.setObjectName("registerSub")
        self.activity_empty.setWordWrap(True)

        # -- price data freshness ---------------------------------------------
        self.freshness_title = QLabel("Price Data Freshness")
        self.freshness_title.setFont(font)
        self.freshness_table = self._make_table(FRESHNESS_COLUMNS, "Name")
        self.freshness_note = QLabel("")
        self.freshness_note.setObjectName("registerSub")
        self.freshness_note.setWordWrap(True)
        self.freshness_empty = QLabel(FRESHNESS_EMPTY_TEXT)
        self.freshness_empty.setObjectName("registerSub")
        self.freshness_empty.setWordWrap(True)

        self.empty_label = QLabel(EMPTY_TEXT)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignCenter)

        lay = QVBoxLayout(self)
        # Match the register pages' 8px inset (see RegisterWidget).
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        lay.addWidget(self.header_box)
        lay.addWidget(self.card)
        lay.addWidget(self.holdings_title)
        lay.addWidget(self.table, 2)
        lay.addWidget(self.unpriced_label)
        lay.addWidget(self.allocation_title)
        lay.addWidget(self.allocation_table, 1)
        lay.addWidget(self.allocation_note)
        lay.addWidget(self.allocation_empty)
        lay.addWidget(self.drift_title)
        lay.addWidget(self.drift_subtitle)
        lay.addWidget(self.drift_table, 1)
        lay.addWidget(self.drift_note)
        lay.addWidget(self.drift_empty)
        lay.addWidget(self.performance_title)
        lay.addWidget(self.performance_period)
        lay.addWidget(self.performance_table, 1)
        lay.addWidget(self.performance_note)
        lay.addWidget(self.performance_empty)
        lay.addWidget(self.activity_title)
        lay.addWidget(self.activity_table, 1)
        lay.addWidget(self.activity_note)
        lay.addWidget(self.activity_empty)
        lay.addWidget(self.freshness_title)
        lay.addWidget(self.freshness_table, 1)
        lay.addWidget(self.freshness_note)
        lay.addWidget(self.freshness_empty)
        lay.addWidget(self.empty_label, 1)
        self.refresh()

    @staticmethod
    def _make_table(columns, stretch_column: str) -> QTableWidget:
        """A read-only glance table. Read-only by construction for the same
        reason the holdings table is: a picker opened from a delegate's
        ``setModelData`` is the documented heap-corruption path, and none of
        these numbers is the user's to type anyway."""
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(list(columns))
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.verticalHeader().setVisible(False)
        head = table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeToContents)
        head.setSectionResizeMode(columns.index(stretch_column), QHeaderView.Stretch)
        return table

    def _build_card(self) -> QFrame:
        """The portfolio-value card: one big home-currency total with the
        securities/cash split under it, because "what is it worth" and "how
        much of that is not invested" are the same glance."""
        card = QFrame()
        card.setObjectName("investmentValueCard")
        card.setFrameShape(QFrame.StyledPanel)
        grid = QGridLayout(card)
        grid.setContentsMargins(12, 8, 12, 8)

        caption = QLabel("Portfolio value")
        caption.setObjectName("registerSub")
        self.total_label = QLabel(fmt_money(0))
        font = QFont(self.total_label.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 8)
        self.total_label.setFont(font)

        self.securities_label = QLabel(fmt_money(0))
        self.cash_label = QLabel(fmt_money(0))
        sec_caption = QLabel("Securities")
        sec_caption.setObjectName("registerSub")
        cash_caption = QLabel("Cash")
        cash_caption.setObjectName("registerSub")

        grid.addWidget(caption, 0, 0)
        grid.addWidget(self.total_label, 1, 0)
        grid.addWidget(sec_caption, 0, 1)
        grid.addWidget(self.securities_label, 1, 1)
        grid.addWidget(cash_caption, 0, 2)
        grid.addWidget(self.cash_label, 1, 2)
        grid.setColumnStretch(3, 1)
        return card

    # -- drawing -------------------------------------------------------------
    def refresh(self) -> None:
        """Recompute from the ledger and redraw. The only place this page reads
        the database."""
        summary = portfolio_summary(self.conn, self._as_of)
        self.summary = summary
        self._stale = False

        empty = summary.is_empty
        self.card.setVisible(not empty)
        self.holdings_title.setVisible(not empty)
        self.table.setVisible(not empty)
        self.unpriced_label.setVisible(not empty)
        self.allocation_title.setVisible(not empty)
        self.drift_title.setVisible(not empty)
        self.performance_title.setVisible(not empty)
        self.activity_title.setVisible(not empty)
        self.freshness_title.setVisible(not empty)
        self.empty_label.setVisible(empty)
        if empty:
            # One empty state, not three: with no investment accounts at all the
            # page says so once and draws nothing that would need explaining.
            self.as_of_label.setText("")
            self.table.setRowCount(0)
            self.allocation_view = None
            self.drift_view = None
            self.performance_view = None
            self.activity_view = None
            self.freshness_view = None
            self.allocation_table.setRowCount(0)
            self.drift_table.setRowCount(0)
            self.performance_table.setRowCount(0)
            self.activity_table.setRowCount(0)
            self.freshness_table.setRowCount(0)
            for w in (self.allocation_table, self.allocation_note,
                      self.allocation_empty, self.drift_subtitle,
                      self.drift_table, self.drift_note, self.drift_empty,
                      self.performance_period,
                      self.performance_table, self.performance_note,
                      self.performance_empty, self.activity_table,
                      self.activity_note, self.activity_empty,
                      self.freshness_table, self.freshness_note,
                      self.freshness_empty):
                w.setVisible(False)
            return

        self.as_of_label.setText(
            "as of " + (fmt_date(summary.as_of) if summary.as_of else NO_DATE_TEXT))
        self.total_label.setText(fmt_money(summary.total))
        self.securities_label.setText(fmt_money(summary.securities))
        self.cash_label.setText(fmt_money(summary.cash))
        self._fill_table(summary.holdings[:self.top_n])
        self.unpriced_label.setText(self._unpriced_text(summary.unpriced))
        self._fill_allocation(summary)
        self._fill_drift(summary)
        self._fill_performance(summary)
        self._fill_activity(summary)
        self._fill_freshness(summary)

    @staticmethod
    def _unpriced_text(unpriced) -> str:
        """Say what the total is missing, rather than quietly valuing an
        unpriced position at zero."""
        if not unpriced:
            return ""
        shown = ", ".join(unpriced[:6])
        if len(unpriced) > 6:
            shown += ", ..."
        noun = "symbol" if len(unpriced) == 1 else "symbols"
        return f"{len(unpriced)} {noun} valued at zero for want of a price: {shown}"

    def _fill_table(self, holdings) -> None:
        self.table.setRowCount(len(holdings))
        for row, h in enumerate(holdings):
            right = Qt.AlignRight | Qt.AlignVCenter
            self._set(row, "Symbol", h.symbol)
            self._set(row, "Name", h.name)
            self._set(row, "Shares", fmt_qty(h.quantity), right)
            self._set(row, "Price", fmt_price(h.price), right)
            self._set(row, "Market Value",
                      fmt_cents(h.market_value) if h.price is not None else UNPRICED_MARK,
                      right)
            self._set(row, "% of Securities", fmt_pct(h.pct), right)

    def _set(self, row: int, column: str, text: str, align=None) -> None:
        item = QTableWidgetItem(text)
        if align is not None:
            item.setTextAlignment(align)
        self.table.setItem(row, COLUMNS.index(column), item)

    def row_text(self, row: int) -> tuple:
        """The rendered text of one holdings row, in :data:`COLUMNS` order --
        what the user actually sees, which is what a test should assert."""
        return _row_text(self.table, len(COLUMNS), row)

    # -- allocation ----------------------------------------------------------
    def _fill_allocation(self, summary) -> None:
        """Draw the allocation panel over the SAME accounts the card was summed
        over, and label the basis so the (legitimately) different total reads as
        a stated difference rather than a contradiction."""
        view = asset_allocation(self.conn, summary.as_of,
                                account_ids=summary.account_ids)
        self.allocation_view = view
        right = Qt.AlignRight | Qt.AlignVCenter
        self.allocation_table.setRowCount(len(view.rows))
        for row, slice_ in enumerate(view.rows):
            self._put(self.allocation_table, ALLOCATION_COLUMNS, row,
                      "Asset Class", slice_.label)
            self._put(self.allocation_table, ALLOCATION_COLUMNS, row,
                      "Value", fmt_cents(slice_.value), right)
            self._put(self.allocation_table, ALLOCATION_COLUMNS, row,
                      "% of Allocation", fmt_pct(slice_.pct), right)

        self.allocation_table.setVisible(not view.is_empty)
        self.allocation_empty.setVisible(view.is_empty)
        notes = []
        if not view.is_empty:
            notes.append(ALLOCATION_BASIS.format(total=fmt_money(view.total)))
        if view.options_note:
            notes.append(view.options_note)
        unpriced = self._unpriced_text(view.unpriced)
        if unpriced:
            notes.append(unpriced)
        self.allocation_note.setText(" ".join(notes))
        self.allocation_note.setVisible(bool(notes))

    def allocation_row_text(self, row: int) -> tuple:
        """The rendered text of one allocation row, in
        :data:`ALLOCATION_COLUMNS` order."""
        return _row_text(self.allocation_table, len(ALLOCATION_COLUMNS), row)

    # -- rebalance drift -----------------------------------------------------
    def _fill_drift(self, summary) -> None:
        """Draw the drift panel, or -- when the domain refuses for want of a
        target -- the reason it refused, inline.

        Deliberately NOT scoped to ``summary.account_ids``: this panel's basis is
        the TARGET's sleeve, whatever accounts that target names, and
        :data:`DRIFT_BASIS` says so on screen. Silently re-scoping it to the
        card's accounts would make every percentage disagree with the target the
        user set.
        """
        view = rebalance_drift(self.conn, summary.as_of)
        self.drift_view = view
        as_of = fmt_date(view.as_of) if view.as_of else NO_DATE_TEXT

        if view.refusal:
            # The domain raised rather than returning an empty report; this page
            # turns that into a label, never a dialog (see the module docstring).
            self.drift_table.setRowCount(0)
            self.drift_table.setVisible(False)
            self.drift_subtitle.setVisible(False)
            self.drift_note.setVisible(False)
            self.drift_empty.setText(DRIFT_NO_TARGET_TEXT.format(reason=view.refusal))
            self.drift_empty.setVisible(True)
            return

        right = Qt.AlignRight | Qt.AlignVCenter
        self.drift_table.setRowCount(len(view.rows))
        for row, r in enumerate(view.rows):
            self._put(self.drift_table, DRIFT_COLUMNS, row, "Asset Class", r.label)
            self._put(self.drift_table, DRIFT_COLUMNS, row,
                      "Value", fmt_cents(r.value), right)
            self._put(self.drift_table, DRIFT_COLUMNS, row,
                      "Target %", fmt_pct(r.target_pct), right)
            self._put(self.drift_table, DRIFT_COLUMNS, row,
                      "Current %", fmt_pct(r.current_pct), right)
            self._put(self.drift_table, DRIFT_COLUMNS, row,
                      "Drift", fmt_points(r.drift_pp), right)
            self._put(self.drift_table, DRIFT_COLUMNS, row, "Action", r.action)
            self._put(self.drift_table, DRIFT_COLUMNS, row,
                      "Amount", fmt_move(r.move_cents), right)
            band = (UNPRICED_MARK if r.out_of_band is None
                    else DRIFT_OUT_OF_BAND if r.out_of_band else DRIFT_IN_BAND)
            self._put(self.drift_table, DRIFT_COLUMNS, row, "Band", band)

        self.drift_subtitle.setText(
            DRIFT_TARGET_LINE.format(target=view.target_name, as_of=as_of))
        self.drift_subtitle.setVisible(True)
        self.drift_table.setVisible(not view.is_empty)
        self.drift_empty.setText(
            DRIFT_EMPTY_TEXT.format(target=view.target_name) if view.is_empty else "")
        self.drift_empty.setVisible(view.is_empty)

        notes = []
        if not view.is_empty:
            notes.append(DRIFT_BASIS.format(
                target=view.target_name,
                total=fmt_money(view.sleeve_total),
                as_of=as_of,
                flag=DRIFT_OUT_OF_BAND,
                abs_pp=_band_text(view.band_abs_pct),
                rel_pct=_band_text(view.band_rel_pct),
            ))
            if not view.target_is_complete:
                notes.append(DRIFT_INCOMPLETE_TEXT.format(
                    target=view.target_name,
                    total=fmt_pct(_quantize_pct(view.target_total_pct))))
            if view.out_of_band_count:
                notes.append(DRIFT_TO_MOVE_TEXT.format(
                    n=view.out_of_band_count,
                    noun="class is" if view.out_of_band_count == 1 else "classes are",
                    total=fmt_money(view.to_move_cents)))
            else:
                notes.append(DRIFT_ON_TARGET_TEXT)
            if view.has_unclassified:
                notes.append(DRIFT_UNCLASSIFIED_TEXT.format(mark=UNPRICED_MARK))
        if view.fixed_rows:
            notes.append(DRIFT_FIXED_TEXT.format(
                rows="; ".join(f"{label} {fmt_money(cents)}"
                               for label, cents in view.fixed_rows),
                total=fmt_money(view.fixed_total)))
        unpriced = self._unpriced_text(view.unpriced)
        if unpriced:
            notes.append(unpriced)
        self.drift_note.setText(" ".join(notes))
        self.drift_note.setVisible(bool(notes))

    def drift_row_text(self, row: int) -> tuple:
        """The rendered text of one drift row, in :data:`DRIFT_COLUMNS` order."""
        return _row_text(self.drift_table, len(DRIFT_COLUMNS), row)

    # -- performance ---------------------------------------------------------
    def _fill_performance(self, summary) -> None:
        """Draw one row per scoped account over the trailing window, plus the
        pooled all-accounts row -- which is omitted for a single account, where
        it would only repeat the row above it."""
        view = account_returns(self.conn, summary.as_of, start=self._period_start,
                               account_ids=summary.account_ids)
        self.performance_view = view
        rows = list(view.rows)
        if view.total is not None and len(rows) > 1:
            rows.append(view.total)

        right = Qt.AlignRight | Qt.AlignVCenter
        self.performance_table.setRowCount(len(rows))
        for row, r in enumerate(rows):
            cells = (
                ("Account", r.name, None),
                ("Start Value", fmt_cents(r.start_value), right),
                ("End Value", fmt_cents(r.end_value), right),
                ("Money In", fmt_cents(r.money_in), right),
                ("Money Out", fmt_cents(r.money_out), right),
                ("Income", fmt_cents(r.income), right),
                ("Gain", UNPRICED_MARK if r.gain is None else fmt_cents(r.gain), right),
                ("Return", fmt_pct(r.pct), right),
            )
            for column, text, align in cells:
                self._put(self.performance_table, PERFORMANCE_COLUMNS, row,
                          column, text, align)

        has_rows = not view.is_empty
        self.performance_table.setVisible(has_rows)
        self.performance_period.setVisible(has_rows)
        self.performance_empty.setVisible(not has_rows)
        if has_rows and view.start and view.end:
            self.performance_period.setText(
                f"{fmt_date(view.start)} through {fmt_date(view.end)}")
        else:
            self.performance_period.setText("")
        no_base = any(r.pct is None for r in rows)
        self.performance_note.setText(
            NO_BASE_TEXT.format(marker=UNPRICED_MARK) if no_base else "")
        self.performance_note.setVisible(no_base)

    def performance_row_text(self, row: int) -> tuple:
        """The rendered text of one performance row, in
        :data:`PERFORMANCE_COLUMNS` order."""
        return _row_text(self.performance_table, len(PERFORMANCE_COLUMNS), row)

    # -- recent activity -----------------------------------------------------
    def _fill_activity(self, summary) -> None:
        """Draw the last few investment transactions over the SAME accounts the
        card was summed over, newest first.

        Accounts are named, never numbered: this table's whole job is to answer
        "what happened lately, and where", which an account id does not.
        """
        view = recent_activity(self.conn, account_ids=summary.account_ids,
                               limit=self.activity_n)
        self.activity_view = view
        right = Qt.AlignRight | Qt.AlignVCenter
        self.activity_table.setRowCount(len(view.rows))
        for row, r in enumerate(view.rows):
            cells = (
                ("Date", fmt_date(r.date), None),
                ("Account", r.account_name, None),
                ("Action", r.action, None),
                ("Symbol", r.symbol, None),
                # A cash-only row (a dividend, an interest payment) has no
                # quantity; an empty cell says so, where "0" would claim shares
                # changed hands.
                ("Quantity", "" if r.quantity is None else fmt_qty(r.quantity), right),
                ("Amount", fmt_cents(r.amount), right),
            )
            for column, text, align in cells:
                self._put(self.activity_table, ACTIVITY_COLUMNS, row, column,
                          text, align)

        self.activity_table.setVisible(not view.is_empty)
        self.activity_empty.setVisible(view.is_empty)
        self.activity_note.setText(
            "" if view.is_empty else ACTIVITY_BASIS.format(n=len(view.rows)))
        self.activity_note.setVisible(not view.is_empty)

    def activity_row_text(self, row: int) -> tuple:
        """The rendered text of one activity row, in :data:`ACTIVITY_COLUMNS`
        order."""
        return _row_text(self.activity_table, len(ACTIVITY_COLUMNS), row)

    # -- price data freshness ------------------------------------------------
    def _fill_freshness(self, summary) -> None:
        """Draw how current each held symbol's price is, worst first, and state
        the threshold and the reference date under the table.

        The card above values an unpriced position at zero and a stale one at
        its last known close; this panel is where that is admitted, which is
        why the threshold is printed rather than tucked into a tooltip.
        """
        view = price_freshness(self.conn, summary.holdings, as_of=summary.as_of,
                               stale_after=self.stale_after_days)
        self.freshness_view = view
        right = Qt.AlignRight | Qt.AlignVCenter
        self.freshness_table.setRowCount(len(view.rows))
        for row, r in enumerate(view.rows):
            cells = (
                ("Symbol", r.symbol, None),
                ("Name", r.name, None),
                ("Last Price", fmt_date(r.latest) if r.latest else UNPRICED_MARK, None),
                ("Days Old", UNPRICED_MARK if r.days is None else f"{r.days:,}", right),
                ("Status", r.status, None),
            )
            for column, text, align in cells:
                self._put(self.freshness_table, FRESHNESS_COLUMNS, row, column,
                          text, align)

        self.freshness_table.setVisible(not view.is_empty)
        self.freshness_empty.setVisible(view.is_empty)
        notes = []
        if not view.is_empty:
            notes.append(FRESHNESS_BASIS.format(
                as_of=fmt_date(view.as_of) if view.as_of else NO_DATE_TEXT,
                stale=FRESHNESS_STALE, days=view.stale_after,
                none=FRESHNESS_NO_PRICE))
            stale = view.stale
            if stale:
                noun = "symbol needs" if len(stale) == 1 else "symbols need"
                notes.append(f"{len(stale)} {noun} a price update: "
                             + ", ".join(r.symbol for r in stale[:6])
                             + (", ..." if len(stale) > 6 else ""))
        self.freshness_note.setText(" ".join(notes))
        self.freshness_note.setVisible(bool(notes))

    def freshness_row_text(self, row: int) -> tuple:
        """The rendered text of one freshness row, in :data:`FRESHNESS_COLUMNS`
        order."""
        return _row_text(self.freshness_table, len(FRESHNESS_COLUMNS), row)

    @staticmethod
    def _put(table, columns, row: int, column: str, text: str, align=None) -> None:
        item = QTableWidgetItem(text)
        if align is not None:
            item.setTextAlignment(align)
        table.setItem(row, columns.index(column), item)

    # -- staleness -----------------------------------------------------------
    def mark_stale(self) -> None:
        """A write somewhere else changed a holding, a price or an account's
        cash: recompute now if the page is on screen, otherwise the next time
        it is shown."""
        self._stale = True
        if self.isVisible():
            self.refresh_if_stale()

    def refresh_if_stale(self) -> bool:
        """Recompute iff a write invalidated the figures since the last draw;
        returns whether it did.

        No clock check, unlike the calendar: this page's as-of date comes from
        the LEDGER's latest known date, not today's, so only a write can move
        it -- and a write calls :meth:`mark_stale`.
        """
        if not self._stale:
            return False
        self.refresh()
        return True

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_if_stale()

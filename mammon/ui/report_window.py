"""A reusable report-window framework — table, CSV/HTML export, print (PDF).

It gives every §5.9a report computation a proper window: the shared
:class:`~mammon.ui.report_filters.ReportFilterBar` on top driving a plain table
below, plus an "Export CSV…" action, HTML/PDF export and print. A window is
described by a :class:`ReportSpec` (title + a ``run`` that calls a pure
``reports.*`` function with the bar's getters + a ``project`` that flattens the
result into ``ReportRow``\\s); the module ships a spec per report
(:data:`CASH_FLOW_SPEC`, :data:`INCOME_EXPENSE_SPEC`, :data:`ACCOUNT_BALANCES_SPEC`,
:data:`BY_PAYEE_SPEC`, :data:`TRANSACTIONS_SPEC`, :data:`ITEMIZE_SPEC`) and
:class:`ReportWindow` defaults to Cash Flow. Adding another report is one ``run``
+ one ``*_rows`` projector + one spec — no new window class, no new write path, no
money math; a report needing a different header (e.g. Itemize's four
``Category``/``Date``/``Payee / Memo``/``Amount`` columns) sets
``ReportSpec.columns``, otherwise it inherits the shared three.

**Flat table vs. drill-down tree.** Most reports render a flat QTableWidget. The
Itemize-by-Category report is instead a DRILL-DOWN tree (``ReportSpec.is_tree``):
its top level is each category with a rolled-up total, expanding a category shows
its sub-categories, and expanding a leaf shows the individual transactions (Date,
Payee/Memo, Amount). Category is the FIRST column so the disclosure triangles sit
under the ``Category`` header — restoring the expandable register-style detail and
fixing the regression where a per-transaction ``Date`` header sat over the category
column. The pure :func:`itemize_tree_rows` projection feeds both the QTreeWidget
(re-nested by depth) and the CSV/HTML/PDF export (flattened with the Category
column indented by depth), so Period, gear and every export work on the tree too.

**One unified customization experience.** Every hosted report shows the same
shape: a shared **Period dropdown** on top (Last 7 days, Last 30 days, Last 12
months, This quarter, Last quarter, Earliest to date, Custom -- see
:data:`~mammon.ui.report_filters.PERIOD_PRESETS`) beside one enlarged gear. A
preset re-ranges and refreshes immediately; "Custom" opens the gear's
:class:`~mammon.ui.report_filters.CustomizeDialog` to pick an explicit range.
**Named saved filter sets** live INSIDE that gear popup (a combo of saved names
plus Save / Delete), not inline in the window, so date range, accounts,
categories AND saved sets share the one affordance. They persist to QSettings via
:mod:`mammon.ui.report_saved_filters` (a UI display preference -- NEVER the
ledger database) through the pure ``filter_state_to_dict`` / ``apply_filter_state``
serializers, so the capture/re-apply round-trip is unit-testable without
QSettings. The name prompt is an overridable seam
(:meth:`ReportWindow._prompt_filter_name`) so headless tests never block on a modal.

Why the shape is the way it is:

- **Reports stay pure.** This module holds NO SQL and NO money arithmetic. It
  calls the pure :func:`mammon.reports.cash_flow`, then only *projects* the
  already-computed integer cents into display rows. Building the "Total" rows
  reads ``report.total_income`` / ``report.net`` etc. — it never sums money
  itself, so the ledger's single-source-of-truth for money is preserved.

- **CSV serialization is a pure, importable function.**
  :func:`report_rows_to_csv` takes the exact rows on screen and returns a CSV
  string, with no file dialog and no Qt. The window's Export action is a thin
  wrapper: pick a path (file dialog / :func:`QFileDialog.getSaveFileName`), then
  hand the path to :meth:`ReportWindow.export_csv_to`, which writes what
  :func:`report_rows_to_csv` produced. Tests exercise the serializer and the
  writer directly, never a modal.

- **Money renders through the one display chokepoint.** Amounts are integer
  cents everywhere and are only ever turned into text by
  :func:`mammon.ui.models.fmt_cents`, so the table and the CSV agree by
  construction (both walk the same ``ReportRow`` list through the same helper).
"""

from __future__ import annotations

import csv
import datetime as _dt
import html as _html
import io
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QBrush, QColor, QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from mammon import ledger, reports
from mammon.ui.models import fmt_cents, fmt_date
from mammon.ui.swatch import color_square_icon
from mammon.ui.report_filters import (
    CATEGORY_KIND_BOTH,
    PERIOD_DEFAULT,
    CustomizeDialog,
    customize_button,
    make_period_combo,
    resolve_period,
    sync_period_combo,
)
from mammon.ui.report_saved_filters import (
    apply_filter_state,
    delete_filter_set,
    filter_state_to_dict,
    load_filter_set,
    save_filter_set,
    saved_filter_names,
)

# The three columns rendered by the table and written to the CSV, in order.
COLUMNS = ["Section", "Category / Account", "Amount"]


@dataclass
class ReportRow:
    """One displayed line of a report.

    ``amount`` is signed integer cents (negative = money out), exactly as it
    arrived from the pure report — this class never does money arithmetic, it
    just carries a value for :func:`fmt_cents` to render.

    Most reports fit the shared two text columns, so a projector fills only
    ``section`` and ``label`` and :func:`_row_cells` maps them onto the header.
    A report that needs more than two text columns (the Transactions listing:
    Date, Payee, Category / Account, Tag, Memo) sets ``cells`` to the explicit
    per-column strings for every column BUT the trailing amount; when present it
    overrides the section/label projection. ``section``/``label`` stay populated
    as a plain-text summary so nothing that reads them breaks.

    ``tooltips`` is ``{column_index: text}`` for cells whose full meaning does not
    fit the column. A column wide enough for a sentence pushes the money columns
    off screen (the defect reported against Capital Gains: *"the tax consequences
    field is way too long and unreadable without expanding the report to full
    screen"*), so the SHORT form goes in the cell and the sentence hangs off it
    here. Export still reads ``cells``, which is why the short form has to be
    true on its own rather than a teaser.
    """

    section: str
    label: str
    amount: int
    cells: list = None
    tooltips: dict = None


def cash_flow_rows(report: "reports.CashFlowReport") -> list[ReportRow]:
    """Project a :class:`~mammon.reports.CashFlowReport` into flat display rows.

    Pure: reads the report's already-computed cents and re-packages them for a
    table/CSV. Income categories then their total, expense categories then their
    total, any cross-boundary transfers then their net, and finally the net.
    """
    rows: list[ReportRow] = []
    for fr in report.income:
        rows.append(ReportRow("Income", fr.path, fr.total))
    rows.append(ReportRow("Income", "Total Income", report.total_income))

    for fr in report.expense:
        rows.append(ReportRow("Expense", fr.path, fr.total))
    rows.append(ReportRow("Expense", "Total Expense", report.total_expense))

    if report.transfers:
        for tr in report.transfers:
            rows.append(ReportRow("Transfers", tr.name, tr.cents))
        rows.append(ReportRow("Transfers", "Net Transfers", report.net_transfers))

    rows.append(ReportRow("Net", "Net Cash Flow", report.net))
    return rows


def income_expense_rows(report: "reports.IncomeExpenseReport") -> list[ReportRow]:
    """Project an :class:`~mammon.reports.IncomeExpenseReport` into flat rows.

    Income categories then their total, expense categories then their total,
    finally net income. Pure: every amount is the report's own signed cents.
    """
    rows: list[ReportRow] = []
    for fr in report.income:
        rows.append(ReportRow("Income", fr.path, fr.total))
    rows.append(ReportRow("Income", "Total Income", report.total_income))

    for fr in report.expense:
        rows.append(ReportRow("Expense", fr.path, fr.total))
    rows.append(ReportRow("Expense", "Total Expense", report.total_expense))

    rows.append(ReportRow("Net", "Net Income", report.net))
    return rows


# The Account-Balance report is a print/export table, so its rows follow the LEFT
# SIDEBAR's account grouping rather than the ledger's flat sort_order -- the printed
# report then reads in the same order as the account bar the user is looking at.
# This list MUST mirror the type sequence of widgets._BAR_GROUPS (Banking, then
# Credit Card, then Investing, then Property & Debt); a drift-guard test
# (test_report_table_defects) fails if the two disagree. INVESTMENT_LIKE_TYPES is
# read from the ledger so the "Investing" group cannot drift here. Within a group
# the rows keep the ledger's own sort_order/name sequence, because the reorder is a
# STABLE sort on the already-ordered report rows.
_SIDEBAR_TYPE_ORDER = ["checking", "savings", "cash",     # Banking
                       "credit",                          # Credit Card
                       *ledger.INVESTMENT_LIKE_TYPES,     # Investing
                       "asset", "liability"]              # Property & Debt


def _balance_type_rank(account_type: str) -> int:
    """The sidebar-group rank for an account type; unknown types sort last (the
    account bar's 'Other' group), matching ``widgets.AccountBar.refresh``."""
    try:
        return _SIDEBAR_TYPE_ORDER.index(account_type)
    except ValueError:
        return len(_SIDEBAR_TYPE_ORDER)


def account_balances_rows(report: "reports.BalanceReport") -> list[ReportRow]:
    """Project a :class:`~mammon.reports.BalanceReport` into flat rows: one line
    per account (section = account type, valued the way the account bar values
    it), then the net-worth total. Pure — no valuation happens here.

    Rows are re-ordered to match the LEFT SIDEBAR's account grouping (see
    :data:`_SIDEBAR_TYPE_ORDER`); a stable sort preserves the report's own
    sort_order/name order within each group.
    """
    ordered = sorted(report.rows, key=lambda ab: _balance_type_rank(ab.type))
    rows: list[ReportRow] = [ReportRow(ab.type, ab.name, ab.cents)
                             for ab in ordered]
    rows.append(ReportRow("Total", "Net Worth", report.total))
    return rows


def payee_rows(report: "reports.PayeeReport") -> list[ReportRow]:
    """Project a :class:`~mammon.reports.PayeeReport` into flat rows. The section
    names the grouping ("Payee" or "Tag"); the amount is the report's own signed
    cents (magnitude for out/in, signed for net). Pure."""
    section = report.key.capitalize()
    rows: list[ReportRow] = [ReportRow(section, r.name, r.cents) for r in report.rows]
    rows.append(ReportRow(section, "Total", report.total))
    return rows


# The Transactions listing is the one flat report with more than two text
# columns. Under the shared three-column default the date landed in the generic
# "Section" column (a "Section" header over the date value) and the payee was
# glued onto the category; this gives the date its own "Date" header and the
# payee its own column, and surfaces the Tag and Memo the register already
# carries. Amount stays the trailing, right-aligned money column.
TRANSACTIONS_COLUMNS = ["Date", "Payee", "Category / Account", "Tag", "Memo",
                        "Amount"]


def listing_rows(report: "reports.ListingReport") -> list[ReportRow]:
    """Project a :class:`~mammon.reports.ListingReport` into flat rows aligned to
    :data:`TRANSACTIONS_COLUMNS`: Date, Payee (its OWN column), Category / Account
    (``[Other Account]`` for a transfer, already resolved by the pure report), the
    transaction's Tag(s) and Memo, and its own signed cents. Dates render through
    the one :func:`fmt_date` chokepoint. Pure — no filtering or summing here beyond
    echoing the report's precomputed count/total."""
    from mammon.ui.models import fmt_date

    rows: list[ReportRow] = []
    for r in report.rows:
        payee = r["payee"] or "(no payee)"
        rows.append(ReportRow(
            r["date"], payee, r["amount"],
            cells=[fmt_date(r["date"]), payee, r["category"] or "",
                   r["tag"] or "", r["memo"] or ""]))
    summary = "%d transactions" % report.count
    rows.append(ReportRow("Total", summary, report.total_cents,
                          cells=["Total", summary, "", "", ""]))
    return rows


def _fmt_qty(q) -> str:
    """A share quantity as a plain fixed-point string (no exponent, trailing
    zeros trimmed): ``Decimal('1E+2')`` -> ``'100'``, ``Decimal('1.5000')`` ->
    ``'1.5'``."""
    return format(q.normalize(), "f")


# The Investment Performance report needs THREE numeric columns per line -- the
# holding's Amount (market value), its Gain/Loss in dollars and the same as a
# percent -- so it cannot use the shared three-column layout, where the sole
# trailing money column landed the market value under a generic "Section" header
# and buried the gain inside the label text. Its own header gives the account and
# the ticker their own columns and breaks the gain out into a dollar column and a
# text percent column. Percent is not money, so it is formatted as text and each
# ReportRow fills every column explicitly via ``cells`` (see :func:`_row_cells`).
INVESTMENT_PERFORMANCE_COLUMNS = ["Account", "Ticker", "Amount", "Dividends",
                                  "Gain/Loss $", "Gain/Loss %", "Annual Return %"]


def _fmt_pct(pct) -> str:
    """A signed one-decimal percent, or blank when there is nothing to measure
    (an unpriced or closed position). Text, never a number -- the Gain/Loss %
    column is a display string, not money."""
    return "" if pct is None else "%+.1f%%" % pct


# Click-to-sort keys for the flat Investment Performance report, mapping the
# clicked column's sort key to a key function over a HoldingPerformance. Account ->
# account then ticker; Ticker -> ticker alone; Gain/Loss $ -> the (period) gain;
# Gain/Loss % -> the (period) percent. Unpriced holdings (no gain/percent) sort as
# zero so they cluster at one end rather than scatter. Sorting is a pure DISPLAY
# reshuffle of already-computed rows (no SQL, no money arithmetic -- amounts are
# only compared, never summed), reusing the window's sort_key/sort_desc seam (the
# same one the Itemize tree uses, see _TXN_SORT_KEYS / _on_tree_sort).
_HOLDING_SORT_KEYS = {
    "account": lambda h: ((h.account_name or "").lower(), (h.symbol or "").lower()),
    "ticker":  lambda h: (h.symbol or "").lower(),
    "income":  lambda h: h.income,
    "gain":    lambda h: h.display_gain if h.display_gain is not None else 0,
    "pct":     lambda h: h.display_pct if h.display_pct is not None else 0,
    "annual":  lambda h: h.annual_return if h.annual_return is not None else 0,
}


def _sorted_holdings(holdings, sort_key, sort_desc):
    """Order the per-holding line items for display under the active sort, or
    return them untouched when no sort is active. Only the security line items
    move; the Portfolio totals are appended afterward and never reorder."""
    keyfn = _HOLDING_SORT_KEYS.get(sort_key)
    if keyfn is None:
        return holdings
    return sorted(holdings, key=keyfn, reverse=sort_desc)


def investment_performance_rows(report: "reports.InvestmentPerformanceReport", *,
                                sort_key: str = None,
                                sort_desc: bool = False) -> list[ReportRow]:
    """Project an :class:`~mammon.reports.InvestmentPerformanceReport` into flat
    rows aligned to :data:`INVESTMENT_PERFORMANCE_COLUMNS`: one line per
    currently-held security carrying its account, its ticker (with share count),
    its market value (Amount), its unrealized Gain/Loss in dollars and the same as
    a percent, then a Portfolio section with the cost/value/gain/income totals.
    Pure — every amount is the report's own cents, rendered once through
    :func:`fmt_cents`; the percent is the report's own :attr:`pct_return`.

    Each row fills every column explicitly through ``cells`` because the report has
    three distinct numeric columns and the shared layout offers only one trailing
    money column. ``section``/``label``/``amount`` stay populated as a plain-text
    summary so anything reading them (older tests, ad-hoc callers) still works.

    Sold-out positions are not line items (their market value is zero), but their
    realized gain and income still land in the Portfolio totals, which the report
    computes over every holding; the per-position detail lives in the pure report,
    the CSV/HTML export's source, and the ``capital_gains`` tool.

    ``sort_key``/``sort_desc`` reorder the per-holding line items (the same seam
    the Itemize tree uses): a clickable header re-projects with the active sort.
    Dividends is every distribution over the report's span; the Gain/Loss columns
    are the TOTAL return over that span with each dividend counted once (cash
    dividends added, reinvested ones already in the value -- see
    :mod:`mammon.reports.investment_performance`), and Annual Return % is the
    money-weighted rate per year, blank for a span under a year.
    """
    rows: list[ReportRow] = []
    holdings = [h for h in report.holdings if h.is_open]
    if sort_key:
        holdings = _sorted_holdings(holdings, sort_key, sort_desc)
    for h in holdings:
        ticker = "%s (%s sh)" % (h.symbol, _fmt_qty(h.quantity))
        gl = "" if h.display_gain is None else fmt_cents(h.display_gain)
        rows.append(ReportRow(
            h.account_name, h.symbol, h.market_value,
            cells=[h.account_name, ticker, fmt_cents(h.market_value),
                   fmt_cents(h.income), gl, _fmt_pct(h.display_pct),
                   _fmt_pct(h.annual_return)]))

    # Portfolio totals. The market-value line doubles as the portfolio Gain/Loss
    # summary (its unrealized dollars and percent); the remaining totals each name a
    # single figure, placed in the Amount column with the gain columns blank.
    rows.append(ReportRow(
        "Portfolio", "Cost Basis", report.total_cost_basis,
        cells=["Portfolio", "Cost Basis", fmt_cents(report.total_cost_basis),
               "", "", "", ""]))
    # The Market Value line is the headline gain: the per-holding gains shown above
    # summed, dividends included, over the same span (the period, or each current
    # holding), with one money-weighted annual rate for the whole portfolio. The
    # breakdown lines below are SINCE PURCHASE and say so, because they do not
    # follow the period and read as contradicting the line above when unlabeled.
    rows.append(ReportRow(
        "Portfolio", "Market Value", report.total_market_value,
        cells=["Portfolio", "Market Value", fmt_cents(report.total_market_value),
               fmt_cents(report.total_income),
               fmt_cents(report.display_total_gain),
               _fmt_pct(report.display_total_pct),
               _fmt_pct(report.total_annual_return)]))
    rows.append(ReportRow(
        "Portfolio", "Unrealized Gain/Loss", report.total_unrealized_pl,
        cells=["Portfolio", "Unrealized Gain/Loss (since purchase)", "", "",
               fmt_cents(report.total_unrealized_pl),
               _fmt_pct(report.pct_return), ""]))
    rows.append(ReportRow(
        "Portfolio", "Realized Gain/Loss", report.total_realized_pl,
        cells=["Portfolio", "Realized Gain/Loss (since purchase)", "", "",
               fmt_cents(report.total_realized_pl), "", ""]))
    rows.append(ReportRow(
        "Portfolio", "Dividend/Interest Income", report.total_dividends,
        cells=["Portfolio", "Dividend/Interest Income (since purchase)",
               fmt_cents(report.total_dividends), "", "", "", ""]))
    if report.total_return_of_capital:
        rows.append(ReportRow(
            "Portfolio", "Return of Capital", report.total_return_of_capital,
            cells=["Portfolio", "Return of Capital",
                   fmt_cents(report.total_return_of_capital), "", "", "", ""]))
    return rows


# -- Capital Gains and Taxes (the dashboard's top-left launcher) --------------
# One line per OPEN TAX LOT, because the holding period -- the whole point of this
# report -- is a property of the lot, not of the position: the same ticker can hold
# a long-term lot and a short-term one at once. "If Sold Now" comes BEFORE the
# money columns on purpose: the trailing column is always right-aligned by
# ``_populate``, and a right-aligned verdict reads as a figure. It holds a few
# characters ("+$412 tax"), never the sentence -- see :func:`_if_sold_now_cell`.
CAPITAL_GAINS_COLUMNS = ["Account", "Ticker", "Acquired", "Term",
                         "Becomes Long-Term", "If Sold Now", "Shares",
                         "Cost Basis", "Market Value", "Unrealized"]

#: How a lot's term reads in the Term column. ``unknown`` is its own word rather
#: than a blank: a lot whose acquisition date was never recorded is a question,
#: not a long-term holding. There is no sheltered term any more -- a
#: 401(k)/IRA/Roth account never reaches this table at all (see
#: :func:`capital_gains_footnote`).
_TERM_TEXT = {"long": "Long-term", "short": "Short-term", "unknown": "Unknown"}

#: Longest an "If Sold Now" cell may render. The column has to sit between two
#: date columns and four money columns in a 1000px window, so the cell is a
#: verdict ("+$412 tax") and the sentence lives in its tooltip.
IF_SOLD_NOW_MAX_CHARS = 16


def _whole_dollars(cents: int) -> str:
    """Signed-magnitude cents as WHOLE dollars with separators ('1,234'). Cents
    are noise in a verdict cell and cost four characters; the tooltip keeps the
    exact figure."""
    d = (Decimal(abs(int(cents))) / 100).quantize(Decimal(1),
                                                  rounding=ROUND_HALF_UP)
    return f"{int(d):,}"


def _if_sold_now_cell(r) -> str:
    """The "If Sold Now" verdict for one lot, in a few characters.

    Each form is complete on its own (it is what CSV export carries): '+$412 tax'
    = selling this short-term gain now costs $412 MORE tax than waiting;
    '-$95 tax' = this short-term LOSS is worth $95 more taken now; '$412 tax' =
    what a long-term lot's gain would cost, with no deadline. A tax-deferred
    account has no verdict here because it has no row here at all."""
    if r.unrealized is None:
        return "No price"
    if r.term == "unknown":
        return "Term unknown"
    if r.term == "long":
        if r.unrealized > 0:
            return "$%s tax" % _whole_dollars(r.tax_at_long_rate or 0)
        if r.unrealized < 0:
            # No figure: a long-term loss has no deadline and no extra tax, so a
            # dollar amount here would only duplicate the Unrealized column.
            return "LT loss"
        return "No gain"
    extra = r.extra_tax_if_sold_now or 0
    if r.unrealized > 0:
        return "+$%s tax" % _whole_dollars(extra)
    if r.unrealized < 0:
        return "-$%s tax" % _whole_dollars(extra)
    return "No gain"


def capital_gains_footnote(report: "reports.CapitalGainsReport") -> str:
    """The sentences that belong under the table rather than inside a cell: the
    rate assumptions every tax figure rests on, and the names of the accounts
    the report dropped because the user marked them tax-deferred.

    The exclusion sentence is the report's own :attr:`exclusion_note`, not a
    phrasing invented here: it is the ONLY trace those accounts leave, so the
    wording lives with the rule that omits them."""
    parts = ["Tax figures are estimates at %s long-term / %s ordinary income; "
             "Mammon does not know your bracket."
             % (_rate_text(report.long_term_rate),
                _rate_text(report.short_term_rate))]
    note = getattr(report, "exclusion_note", None)
    if note:
        parts.append(note + ". These accounts owe no capital gains, so no row "
                            "or total above includes them.")
    parts.append("Hover any If Sold Now cell for the full explanation.")
    return "  ".join(parts)

# Click-to-sort keys for the flat Capital Gains report, over a LotTaxRow. A lot
# with no acquisition date sorts last under either date key (it is the one the
# user has to go fix), and an unpriced lot sorts as zero unrealized rather than
# scattering.
_LOT_SORT_KEYS = {
    "account":    lambda r: ((r.account_name or "").lower(), (r.symbol or "").lower(),
                             r.acquired or "9999-12-31"),
    "ticker":     lambda r: ((r.symbol or "").lower(), r.acquired or "9999-12-31"),
    "acquired":   lambda r: r.acquired or "9999-12-31",
    # Short lots first, then long, then unknown, then the tax-deferred ones: a
    # deadline outranks a holding with no deadline, which is what the user opened
    # this report to see, and a sheltered lot has no tax story at all.
    "term":       lambda r: ({"short": 0, "long": 1, "unknown": 2}.get(r.term, 3),
                             r.long_term_on or "9999-12-31"),
    "longon":     lambda r: r.long_term_on or "9999-12-31",
    "value":      lambda r: r.market_value,
    "unrealized": lambda r: r.unrealized if r.unrealized is not None else 0,
}


def capital_gains_rows(report: "reports.CapitalGainsReport", *,
                       sort_key: str = None,
                       sort_desc: bool = False) -> list[ReportRow]:
    """Project a :class:`~mammon.reports.CapitalGainsReport` into flat rows aligned
    to :data:`CAPITAL_GAINS_COLUMNS`: one line per open tax lot carrying its
    account, ticker, acquisition date, whether it is long- or short-term TODAY, the
    date a short lot turns long-term (with the days still to run), a SHORT verdict
    on selling it now (the report's own sentence rides along in ``tooltips``), and
    the lot's shares, cost basis, market value and unrealized gain. Then the
    totals, split long/short and gain/loss, plus a tax-deferred line when any
    account is sheltered, and what selling the whole short-term book now costs
    over waiting.

    Pure — every figure is the report's own cents and its own annotation string;
    nothing here computes a tax or a holding period. Dates render through the one
    :func:`fmt_date` chokepoint. ``sort_key``/``sort_desc`` reorder only the lot
    lines, exactly as :func:`investment_performance_rows` does; the totals are
    appended afterwards and never move."""
    from mammon.ui.models import fmt_date

    lots = list(report.lots)
    keyfn = _LOT_SORT_KEYS.get(sort_key)
    if keyfn is not None:
        lots = sorted(lots, key=keyfn, reverse=sort_desc)

    rows: list[ReportRow] = []
    for r in lots:
        # "SYM (n sh)" so _row_symbol recognizes a holding line and offers its
        # price history, the same composite the performance report renders.
        ticker = "%s (%s sh)" % (r.symbol, _fmt_qty(r.quantity))
        becomes = ""
        if r.term == "short" and r.long_term_on:
            becomes = fmt_date(r.long_term_on)
            if r.days_to_long is not None:
                becomes += " (%d day%s)" % (r.days_to_long,
                                            "" if r.days_to_long == 1 else "s")
        # The VERDICT in the cell, the report's own sentence in the tooltip: a
        # column wide enough for that sentence pushed the money columns off
        # screen, which is the second defect the user reported.
        rows.append(ReportRow(
            r.account_name, r.symbol, r.unrealized or 0,
            cells=[r.account_name, ticker,
                   fmt_date(r.acquired) if r.acquired else "",
                   _TERM_TEXT.get(r.term, r.term), becomes, _if_sold_now_cell(r),
                   _fmt_qty(r.quantity), fmt_cents(r.cost_basis),
                   fmt_cents(r.market_value),
                   "" if r.unrealized is None else fmt_cents(r.unrealized)],
            tooltips={5: r.annotation} if r.annotation else None))

    def _total(label: str, cents: int, note: str = "",
               short: str = "") -> ReportRow:
        return ReportRow("Totals", label, cents,
                         cells=["Totals", label, "", "", "", short, "", "", "",
                                fmt_cents(cents)],
                         tooltips={5: note} if note else None)

    # Gains and losses stay in separate buckets here because they are separate in
    # the pure report -- netting them is a tax question (wash sales,
    # carry-forwards, the $3,000 cap) neither layer answers.
    rows.append(_total("Long-term gain", report.total_long_term_gain))
    rows.append(_total("Long-term loss", report.total_long_term_loss))
    rows.append(_total("Short-term gain", report.total_short_term_gain))
    rows.append(_total("Short-term loss", report.total_short_term_loss))
    if report.total_unknown_term:
        rows.append(_total("Unknown term", report.total_unknown_term,
                           "Acquisition date missing on these lots, so Mammon "
                           "cannot say whether they are long- or short-term.",
                           "No date"))
    # No sheltered-money row: a tax-deferred account is out of this report
    # entirely (user: "if they're not taxed, don't put them in the report"), and
    # the footnote -- not a row -- names what was left out.
    rows.append(_total("Total unrealized", report.total_unrealized))
    rows.append(_total(
        "Extra tax if the short-term book is sold now",
        report.total_extra_tax_if_sold_now,
        "Estimated at %s long-term / %s ordinary; a short-term LOSS is worth more "
        "now, which pushes this figure down."
        % (_rate_text(report.long_term_rate), _rate_text(report.short_term_rate)),
        "Estimate"))
    return rows


def _rate_text(rate) -> str:
    """A tax rate (a Decimal fraction) as a percent for the totals note. Not
    ``Decimal.normalize`` -- that renders 0.50 -> '5E+1' once scaled."""
    pct = (Decimal(str(rate)) * 100).quantize(Decimal("0.1"))
    s = f"{pct}"
    return (s[:-2] if s.endswith(".0") else s) + "%"


def _row_symbol(row: ReportRow) -> str:
    """The bare ticker a report line names, or ``''`` when the line names none.

    :func:`investment_performance_rows` puts the BARE symbol on ``label`` and
    renders the Ticker cell as the composite ``"SYM (n sh)"``; the Portfolio total
    lines put prose ("Cost Basis", "Market Value") in that same column. Matching
    the Ticker cell against the label tells a holding line from a total line
    without ever parsing the composite display text -- so a total row is offered
    no price-history chart, and a symbol never arrives with " (12 sh)" glued on.
    """
    label = (row.label or "").strip()
    cells = row.cells or []
    if not label or len(cells) < 2:
        return ""
    return label if str(cells[1]).startswith(label + " (") else ""


def itemize_rows(report: "reports.ItemizedReport") -> list[ReportRow]:
    """Project an :class:`~mammon.reports.ItemizedReport` into the flat two-column
    (Category, Amount) rows the Itemize window shows: one line per category (or
    transfer counterparty) carrying its SIGNED net over the period, then the grand
    total. The ``section`` field is blank — this report renders only Category +
    Amount, so the row's ``label`` (the category path or bracketed ``[Account]``
    label) is the whole Category column. Pure: every amount is the report's own
    signed cents and the closing total is ``report.total_cents``, never summed here.

    Retained as the flat projector for the pure ``ItemizedReport``; the Itemize
    *window* now renders the hierarchical :func:`itemize_tree_rows` instead, so a
    category expands into its sub-categories and finally the transactions.
    """
    rows: list[ReportRow] = [ReportRow("", r.path, r.net_cents)
                             for r in report.rows]
    rows.append(ReportRow("", "Total", report.total_cents))
    return rows


# -- the hierarchical Itemize projection (Category -> sub-cat -> transactions) --
# The Itemize window is a drill-down TREE, not a flat table: top level is each
# category with its rolled-up total, expanding shows sub-categories, expanding a
# leaf shows the individual transactions (Date, Payee/Memo, Amount). The columns
# put Category FIRST so the tree's disclosure triangles sit under the "Category"
# header -- fixing the regression where a per-transaction "Date" header sat over
# the category column.
TREE_COLUMNS = ["Category", "Date", "Payee / Memo", "Amount"]


@dataclass
class TreeRow:
    """One node of the Itemize drill-down, flattened to ``(depth, cells)`` so the
    QTreeWidget (which re-nests by depth) and the CSV/HTML/PDF export (which
    indents the first column by depth) read from ONE pure projection.

    ``cells`` already holds display strings (dates via :func:`fmt_date`, money via
    the one :func:`fmt_cents` chokepoint) aligned to :data:`TREE_COLUMNS`. ``amount``
    is the node's signed cents, kept only so the renderer can right-align/red-tint
    the last column; this class does no money arithmetic. ``expanded`` seeds the
    section rows open and everything else collapsed; ``bold`` marks the section and
    grand-total rows.
    """

    depth: int
    kind: str
    cells: list
    amount: int
    expanded: bool = False
    bold: bool = False


def _payee_memo(line: "reports.TxnLine") -> str:
    """A transaction leaf's middle column: the payee, with the memo appended after
    an em dash when present (memo alone if there is no payee). Pure text join."""
    payee = line.description or ""
    if line.memo:
        return "%s — %s" % (payee, line.memo) if payee else line.memo
    return payee


# Click-to-sort keys for the Itemize drill-down. Each maps a clicked column to a
# (primary, secondary) pair of extractors over a reports.TxnLine, exactly as the
# defect specifies: Date -> 2nd key payee, Payee -> 2nd key date, Amount -> 2nd key
# date. Sorting is a pure DISPLAY reshuffle of already-computed rows (no SQL, no
# money arithmetic -- amounts are only compared, never summed), so it lives in this
# projector rather than the domain tree; the tree's default account/date leaf order
# is preserved when sort_key is None.
_TXN_SORT_KEYS = {
    "date":   (lambda l: l.date, lambda l: (l.description or "").lower()),
    "payee":  (lambda l: (l.description or "").lower(), lambda l: l.date),
    "amount": (lambda l: l.amount_cents, lambda l: l.date),
}


def _sorted_children(node, sort_key, sort_desc) -> list:
    """Order one grouping node's children for display under the active sort.

    A group whose children are transaction leaves sorts by the clicked column,
    with the specified secondary key. The TRANSFERS section's counterparty nodes
    stay in their incoming ALPHABETICAL order EXCEPT when Amount is the sort key,
    where they re-sort by their rolled-up net -- "transfers stay sorted
    alphabetically unless Amount is the sort key". Ordinary category / sub-category
    grouping nodes keep their alphabetical order always. The secondary key is
    applied in a first stable pass so a descending primary never scrambles ties.
    """
    children = node.children
    if not sort_key or not children:
        return children
    if all(c.kind == "txn" for c in children):
        primary, secondary = _TXN_SORT_KEYS[sort_key]
        ordered = sorted(children, key=lambda c: secondary(c.line))
        ordered.sort(key=lambda c: primary(c.line), reverse=sort_desc)
        return ordered
    if all(c.kind == "transfer" for c in children) and sort_key == "amount":
        ordered = sorted(children, key=lambda c: c.label.lower())
        ordered.sort(key=lambda c: c.net_cents, reverse=sort_desc)
        return ordered
    return children


def itemize_tree_rows(tree: "reports.ItemizedTree", *,
                      sort_key: str = None, sort_desc: bool = False) -> list[TreeRow]:
    """Project an :class:`~mammon.reports.ItemizedTree` into a depth-tagged list of
    :class:`TreeRow`\\s: each section (INCOME / EXPENSES / TRANSFERS), then its
    categories, their sub-categories and "Other" nodes, and finally the individual
    transactions, followed by the grand-total row.

    Pure: it only re-packages the tree's already-computed signed cents for display
    (money through :func:`fmt_cents`, dates through :func:`fmt_date`) -- no SQL, no
    summing. Grouping nodes carry their label in the Category column and their net
    in Amount; transaction leaves carry the date, payee/memo and their own amount,
    with a blank Category cell so the tree structure reads down the first column.

    ``sort_key`` (``"date"`` / ``"payee"`` / ``"amount"``) re-orders the transaction
    leaves within each open group per the clicked column; ``None`` keeps the tree's
    default account/date order. See :func:`_sorted_children` for the transfer rule.
    """
    from mammon.ui.models import fmt_date

    rows: list[TreeRow] = []

    def walk(node, depth: int) -> None:
        if node.kind == "txn":
            line = node.line
            rows.append(TreeRow(
                depth, "txn",
                ["", fmt_date(line.date), _payee_memo(line),
                 fmt_cents(line.amount_cents)],
                line.amount_cents))
            return
        is_section = node.kind == "section"
        rows.append(TreeRow(
            depth, node.kind,
            [node.label, "", "", fmt_cents(node.net_cents)],
            node.net_cents, expanded=is_section, bold=is_section))
        for child in _sorted_children(node, sort_key, sort_desc):
            walk(child, depth + 1)

    for section in tree.sections:
        walk(section, 0)
    rows.append(TreeRow(0, "total", ["TOTAL", "", "", fmt_cents(tree.total_cents)],
                        tree.total_cents, bold=True))
    return rows


def _tree_row_cells(row: TreeRow) -> list:
    """A :class:`TreeRow`'s display strings for a flat (CSV/HTML) export: the
    Category column is indented two spaces per depth so the hierarchy survives the
    flatten; the remaining columns are the row's cells verbatim."""
    cells = list(row.cells)
    cells[0] = ("  " * row.depth) + cells[0]
    return cells


def tree_rows_to_csv(rows, columns=TREE_COLUMNS) -> str:
    """Serialize Itemize :class:`TreeRow`\\s to a CSV string (pure; no Qt, no I/O).

    The tree is flattened depth-first with the Category column indented by depth
    (:func:`_tree_row_cells`); money is already rendered by :func:`fmt_cents`, so
    the file matches the on-screen tree. Mirrors :func:`report_rows_to_csv`.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for r in rows:
        writer.writerow(_tree_row_cells(r))
    return buf.getvalue()


def tree_rows_to_html(rows, title: str, columns=TREE_COLUMNS) -> str:
    """Serialize Itemize :class:`TreeRow`\\s to a standalone HTML document (pure).

    Mirrors :func:`report_rows_to_html` -- same forced white-page/black-text so a
    dark-mode export stays legible, same right-aligned trailing Amount column --
    but flattens the drill-down with the Category column indented by depth. Section
    and total rows render bold.
    """
    def esc(value) -> str:
        return _html.escape("" if value is None else str(value))

    last = len(columns) - 1
    head = []
    for i, col in enumerate(columns):
        align = "right" if i == last else "left"
        head.append(f'<th align="{align}">{esc(col)}</th>')

    body = []
    for r in rows:
        cells = _tree_row_cells(r)
        tds = []
        for i, c in enumerate(cells):
            align = "right" if i == last else "left"
            text = esc(c).replace("  ", "&nbsp;&nbsp;")
            if r.bold:
                text = f"<b>{text}</b>"
            tds.append(f'<td align="{align}">{text}</td>')
        body.append("<tr>" + "".join(tds) + "</tr>")

    return f"""<html><head><meta charset="utf-8"><title>{esc(title)}</title><style>
      /* Print/export artifact: force a white page + black text so a report saved
         or printed from DARK mode is still legible on paper -- never dark-on-dark. */
      body {{ font-family: 'Segoe UI', Arial, sans-serif; font-size: 9pt;
              background: #ffffff; color: #000000; }}
      h2 {{ margin: 0 0 8px 0; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border-bottom: 1px solid #ccc; padding: 2px 6px; }}
      th {{ border-bottom: 1px solid #333; }}
    </style></head><body>
      <h2>{esc(title)}</h2>
      <table><tr>{''.join(head)}</tr>{''.join(body)}</table>
    </body></html>"""


def _row_cells(row, columns) -> list:
    """The display strings for one :class:`ReportRow` under ``columns``.

    The amount is always the trailing, right-aligned column (rendered by the one
    :func:`fmt_cents` chokepoint); the leading text columns come from the row. A
    row that carries explicit ``cells`` (the Transactions listing: Date, Payee,
    Category / Account, Tag, Memo) uses them verbatim -- one per column before the
    amount. Otherwise a three-column report shows ``(section, label)``; a
    two-column report -- the Itemize window's ``Category``/``Amount`` -- drops the
    section and shows the label alone. Money is only ever turned into text here, so
    the table, CSV and HTML agree by construction.
    """
    if row.cells is not None:
        # A row whose cells already fill EVERY column (the Investment Performance
        # report, which carries three distinct numeric columns -- Amount,
        # Gain/Loss $ and Gain/Loss % -- and so cannot lean on the single trailing
        # ``amount``) is rendered verbatim; the projector already rendered its money
        # through this same fmt_cents chokepoint. Otherwise the cells are the leading
        # columns and the amount is appended as the trailing money column.
        if len(row.cells) == len(columns):
            return list(row.cells)
        return [*row.cells, fmt_cents(row.amount)]
    text = [row.section, row.label][-(len(columns) - 1):]
    return [*text, fmt_cents(row.amount)]


def report_rows_to_csv(rows, columns=COLUMNS) -> str:
    """Serialize display rows to a CSV string (pure; no Qt, no I/O).

    The amount column is rendered with :func:`fmt_cents`, so the file matches
    what is on screen. ``columns`` is the header row (a report's own set — the
    shared three-column default, or the Itemize report's two); the cells follow it
    via :func:`_row_cells`. Uses ``\\n`` line endings for predictable,
    cross-platform output; the ``csv`` module quotes any field (a
    thousands-separated amount, a category name) that contains the delimiter.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for r in rows:
        writer.writerow(_row_cells(r, columns))
    return buf.getvalue()


def report_rows_to_html(rows, title: str, columns=COLUMNS) -> str:
    """Serialize display rows to a standalone HTML document (pure; no Qt, no I/O).

    Mirrors :func:`report_rows_to_csv`: the same header ``columns`` in the same
    order (a report's own set — the shared three, or the Itemize report's two),
    the same rows, the amount rendered with :func:`fmt_cents` so the page matches
    what is on screen. Every field is HTML-escaped -- the analogue of the CSV
    writer's quoting -- so a category name containing ``&`` or ``<`` renders as
    text, not markup. The amount column (the last) is right-aligned; ``title``
    becomes both the document ``<title>`` and its heading.
    """
    def esc(value) -> str:
        return _html.escape("" if value is None else str(value))

    last = len(columns) - 1
    head = []
    for i, col in enumerate(columns):
        align = "right" if i == last else "left"
        head.append(f'<th align="{align}">{esc(col)}</th>')

    body = []
    for r in rows:
        cells = _row_cells(r, columns)
        tds = [f'<td align="{"right" if i == last else "left"}">{esc(c)}</td>'
               for i, c in enumerate(cells)]
        body.append("<tr>" + "".join(tds) + "</tr>")

    return f"""<html><head><meta charset="utf-8"><title>{esc(title)}</title><style>
      /* Print/export artifact: force a white page + black text so a report saved
         or printed from DARK mode is still legible on paper -- never dark-on-dark.
         The on-screen report table follows the active theme via the app's
         stylesheet; this exported document deliberately does not. */
      body {{ font-family: 'Segoe UI', Arial, sans-serif; font-size: 9pt;
              background: #ffffff; color: #000000; }}
      h2 {{ margin: 0 0 8px 0; }}
      table {{ border-collapse: collapse; width: 100%; }}
      th, td {{ border-bottom: 1px solid #ccc; padding: 2px 6px; }}
      th {{ border-bottom: 1px solid #333; }}
    </style></head><body>
      <h2>{esc(title)}</h2>
      <table><tr>{''.join(head)}</tr>{''.join(body)}</table>
    </body></html>"""


def _default_range(conn) -> tuple[str, str]:
    """The window's initial From/To, matching the ``PERIOD_DEFAULT``
    "Year-to-Date" selection (Jan 1 of this year through today) so the dropdown
    and the shown range agree from the first paint (see :func:`resolve_period`)."""
    return resolve_period(PERIOD_DEFAULT, conn, _dt.date.today())


# -- report specs: what to run and how to project it -------------------------
# Each ``run`` calls a pure reports.* function with the filter bar's getters;
# each ``project`` re-packages the already-computed cents into ReportRows. The
# window holds NO SQL and NO money math — those two callables are the whole seam.

@dataclass
class ReportSpec:
    """One report the reusable window can host.

    ``run(conn, filters)`` returns the pure report object; ``project(report)``
    flattens it into ``ReportRow``\\s. ``show_accounts`` hides the account
    checklist for reports that do not take an account filter (e.g. balances);
    ``show_hidden_toggle`` likewise hides the include-hidden checkbox for a report
    whose pure function takes no such flag. ``category_kind`` turns on the
    top-level category check-list AND says which categories belong in it --
    ``expense``, ``income`` or ``both`` (``report_filters.CATEGORY_KIND_*``) --
    and defaults to None, meaning no list, because a visible control that
    filters nothing is worse than no control: only a report whose ``run``
    actually consumes ``filters.selected_categories()`` may set it (today
    Itemize, Cash Flow, Income vs Expense and Transactions, all of which show
    both sides and so ask for ``both`` -- By Payee, By Tag, Account Balances and
    Investment Performance do not group by category at all). ``show_categories``
    is the older boolean and is kept in step with it by ``__post_init__``, so a
    True with no kind means ``both``. ``columns`` is the report's own header
    set — ``None`` means the shared three-column default, and a report like Itemize
    overrides it (``["Category", "Amount"]``); :func:`_row_cells` maps a
    ``ReportRow`` onto whichever set is in force.
    """

    title: str
    run: Callable
    project: Callable
    show_accounts: bool = True
    show_hidden_toggle: bool = True
    show_categories: bool = False
    # Which categories the check-list offers: ``expense``, ``income`` or ``both``
    # (``report_filters.CATEGORY_KIND_*``). None means no list. Kept in step with
    # ``show_categories`` by ``__post_init__`` so neither can contradict the other.
    category_kind: str = None
    columns: list = None
    is_tree: bool = False
    # Size the first (text) column to its contents so long values are not clipped
    # -- By Payee sets this so a full payee name shows without a manual drag.
    fit_first_column: bool = False
    # Clickable-header sort for a FLAT report: ``{column_index: sort_key}`` naming
    # which columns sort and to which key the projector understands. ``None`` (the
    # default) leaves the header inert. The projector must accept
    # ``sort_key``/``sort_desc`` (as ``investment_performance_rows`` does); this
    # reuses the same instance-level sort seam the Itemize tree uses.
    sortable: dict = None
    # This report's line items name a SECURITY (its bare ticker on ``ReportRow.label``,
    # rendered as "SYM (n sh)"), so the window offers the shared price-history chart
    # on a right-click -- the same entry the investment register offers. False for
    # every other report: their rows name a payee or a category, and a menu that can
    # produce no action is worse than no menu (same reasoning as ``category_kind``).
    price_history: bool = False
    # Income vs Expense is read by the user as "where do I stand as of ___",
    # so it alone shows the selected range's END date in the header, centered
    # between the Period selector and the gear button. False for every other
    # report -- a label naming nothing meaningful is worse than no label (same
    # reasoning as ``category_kind`` above).
    show_end_date: bool = False
    # Right-align every column from this index on, for a report whose trailing
    # columns are ALL figures (Investment Performance: Amount, Dividends, the gain
    # dollars and both percents). None aligns only the last column, as before.
    right_align_from: int = None
    # Size EVERY column to its contents after each populate, for a wide report
    # whose columns are all short but of very different widths (Capital Gains:
    # two dates, a term, a verdict and four money columns). Distinct from
    # ``fit_first_column``, and deliberately a one-shot resize rather than
    # ``ResizeToContents`` mode, so the user can still drag a column afterwards.
    fit_columns: bool = False
    # ``footnote(report) -> str`` for the wrapping label under the table: the
    # assumptions and exclusions that belong to the whole report rather than to
    # any one cell. Putting them in a cell is what made the Capital Gains "If
    # Sold Now" column unreadable. None (the default) shows no label.
    footnote: Callable = None
    # Opening size for a report whose default 620x640 would clip it. ``(w, h)``.
    default_size: tuple = None

    def __post_init__(self):
        if self.columns is None:
            self.columns = list(COLUMNS)
        # One switch, two names: a spec may say "I filter by category" either way
        # round, and they must agree or the bar and the spec would disagree about
        # whether a control exists. A bare ``show_categories=True`` means the
        # widest scope; a kind implies the control is on.
        if self.category_kind is None and self.show_categories:
            self.category_kind = CATEGORY_KIND_BOTH
        self.show_categories = self.category_kind is not None


# Every category-filtered report reads the picker the same way, in one place:
# all-ticked (or, deliberately, none-ticked) means "no filter". The getter answers
# None for all-checked and an empty selection for none-checked, and an empty
# report is never what Clear-all was for -- it reads as broken, whereas the
# account list at least names the accounts you unticked.
#
# The IDS the picker's tree has ticked, which is what the reports filter on:
# every checked row at any depth, so a sub-category can be chosen on its own
# (`Taxes:Federal` without `Taxes:Property`). Ids rather than names because
# `ledger.rename_category` keeps the id -- a name-keyed filter silently drops a
# category the moment it is renamed (SRD 5.9c). The set is already EXACT: the
# tree pushed each tick down its subtree, so no report should re-expand it.
def _category_ids(f):
    return f.selected_category_ids() or None


def _run_cash_flow(conn, f):
    # The category pick narrows the Income and Expense sections only: transfers
    # carry no category, so Cash Flow's third section (and the transfers inside
    # its Net) stays whole -- see reports.cash_flow.
    return reports.cash_flow(conn, f.start_iso(), f.end_iso(),
                             account_ids=f.selected_account_ids(),
                             category_ids=_category_ids(f),
                             include_hidden=f.include_hidden())


def _run_income_expense(conn, f):
    return reports.income_expense(conn, f.start_iso(), f.end_iso(),
                                  account_ids=f.selected_account_ids(),
                                  category_ids=_category_ids(f),
                                  include_hidden=f.include_hidden())


def _run_account_balances(conn, f):
    # The "To" date is this report's as-of; the account check-list subsets which
    # accounts are valued. account_balances used to take no account filter at
    # all, which is why ACCOUNT_BALANCES_SPEC hid the picker -- "what are these
    # three accounts worth" is a real question, and its ``total`` is net worth
    # over exactly the rows shown.
    return reports.account_balances(conn, f.end_iso(),
                                    account_ids=f.selected_account_ids(),
                                    include_hidden=f.include_hidden())


def _run_by_payee(conn, f):
    # NET, not the pure function's "out" default. A by-payee report answers "how
    # much moved between me and this payee", and half of a household's payees pay
    # IN -- an employer, a pension, a brokerage. Summing only outflow reported an
    # employer at their payroll withholding; summing only inflow would hide every
    # store. Net signs each payee the way the register does (negative = money
    # out), so both read correctly in one column.
    return reports.by_payee(conn, f.start_iso(), f.end_iso(),
                            account_ids=f.selected_account_ids(),
                            include_hidden=f.include_hidden(),
                            direction="net")


def _run_by_tag(conn, f):
    # Money by first-class tag; a transaction counts under each tag it carries.
    return reports.spending_by_tag(conn, f.start_iso(), f.end_iso(),
                                   account_ids=f.selected_account_ids(),
                                   include_hidden=f.include_hidden())


def _run_transactions(conn, f):
    # A category pick lists only the rows posted to the TICKED categories; a split
    # matches on any of its lines. ``expand_subtree=False`` because the tree has
    # already expanded the pick -- letting the listing re-expand a ticked parent
    # would put back the one child the user deliberately unticked. Rows carrying
    # no category at all -- uncategorized entries and transfer legs -- match no
    # category filter, so a narrowed listing drops them (reports.transactions).
    return reports.transactions(conn, f.start_iso(), f.end_iso(),
                                account_ids=f.selected_account_ids(),
                                category_ids=_category_ids(f),
                                expand_subtree=False,
                                include_hidden=f.include_hidden())


def _run_investment_performance(conn, f):
    # A per-holding snapshot valued as of the "To" date, with Gain/Loss bounded to
    # the resolved period [From, To] -- so 'Last 3 years' and 'Last 10 years' report
    # different gains, not the same inception-to-date figure. The account checklist
    # filters it, and only investment accounts contribute holdings.
    return reports.investment_performance(conn, f.end_iso(),
                                          start=f.start_iso(),
                                          account_ids=f.selected_account_ids(),
                                          include_hidden=f.include_hidden())


def _run_capital_gains(conn, f):
    # A snapshot as of the "To" date: the holding period of every OPEN lot is
    # measured to that date, and the lot is valued at the price on it. There is no
    # start date to honour -- a lot's term depends on when it was bought and what
    # day it is, not on a reporting window -- so the bar's "From" is ignored here
    # rather than silently dropping lots bought before it.
    return reports.capital_gains(conn, f.end_iso(),
                                 account_ids=f.selected_account_ids(),
                                 include_hidden=f.include_hidden())


def _run_itemize(conn, f):
    # The hierarchical Itemize: INCOME / EXPENSES / TRANSFERS sections, each
    # category expanding into its sub-categories and finally its transactions, with
    # every amount rolled up (income first, then expense, then transfer
    # counterparties). itemize_tree takes no include-hidden flag, so ITEMIZE_SPEC
    # sets show_hidden_toggle=False rather than show a dead control. The picker's
    # ticked IDS restrict which categories appear, at any depth --
    # ITEMIZE_SPEC.show_categories is what makes that tree exist -- so an unticked
    # sub-category drops out of the body AND out of its parent's rolled-up total.
    # The filter applies BEFORE income/expense classification, so picking one
    # income and one expense category keeps both sections, each still expanding to
    # its sub-categories and transactions. ``_category_ids`` normalizes an EMPTY
    # selection back to "all" -- see there.
    return reports.itemize_tree(conn, f.start_iso(), f.end_iso(),
                                account_ids=f.selected_account_ids(),
                                category_ids=_category_ids(f))


# Cash Flow and Income vs Expense are deliberately BOTH kept -- they are not the
# same table: Cash Flow adds a TRANSFERS section for money crossing the selected
# account set and its Net INCLUDES those transfers (did cash in these accounts rise
# or fall), while Income vs Expense excludes transfers entirely and its Net is
# income + expense only (did I earn more than I spent). Cash Flow's first column is
# therefore "Direction" (Income / Expense / Transfers / Net), not the shared
# "Section", to name what that column actually distinguishes.
CASH_FLOW_SPEC = ReportSpec("Cash Flow", _run_cash_flow, cash_flow_rows,
                            category_kind=CATEGORY_KIND_BOTH,
                            columns=["Direction", "Category / Account", "Amount"])
INCOME_EXPENSE_SPEC = ReportSpec("Income vs Expense", _run_income_expense,
                                 income_expense_rows,
                                 category_kind=CATEGORY_KIND_BOTH,
                                 show_end_date=True)
ACCOUNT_BALANCES_SPEC = ReportSpec("Account Balances", _run_account_balances,
                                   account_balances_rows,
                                   columns=["Account Type", "Category / Account",
                                            "Amount"])
BY_PAYEE_SPEC = ReportSpec("By Payee", _run_by_payee, payee_rows,
                           columns=["Payee", "Net Amount"], fit_first_column=True)
BY_TAG_SPEC = ReportSpec("By Tag", _run_by_tag, payee_rows)
TRANSACTIONS_SPEC = ReportSpec("Transactions", _run_transactions, listing_rows,
                               category_kind=CATEGORY_KIND_BOTH,
                               columns=TRANSACTIONS_COLUMNS)
# Sortable: Account (col 0) -> account then ticker, Ticker (col 1) -> ticker
# alphabetical, Dividends (col 3), Gain/Loss $ (col 4), Gain/Loss % (col 5) and
# Annual Return % (col 6) by their values. Amount (col 2) is left inert.
INVESTMENT_PERFORMANCE_SPEC = ReportSpec("Investment Performance",
                                         _run_investment_performance,
                                         investment_performance_rows,
                                         columns=INVESTMENT_PERFORMANCE_COLUMNS,
                                         sortable={0: "account", 1: "ticker",
                                                   3: "income", 4: "gain",
                                                   5: "pct", 6: "annual"},
                                         right_align_from=2,
                                         price_history=True)
# Sortable: Account (col 0), Ticker (col 1), Acquired (col 2), Term (col 3),
# Becomes Long-Term (col 4), Market Value (col 8) and Unrealized (col 9). The
# "If Sold Now" verdict (5) and the share count (6) are left inert -- sorting
# "+$412 tax" alphabetically answers no question; sort by Term instead.
# Ten columns, so it fits them to their contents and opens wider than the shared
# default: at 620px the money columns fell off the right edge, which is the
# defect the user reported ("unreadable without expanding the report to full
# screen"). The prose that used to be in column 5 now hangs off it as a tooltip
# and under the table as ``capital_gains_footnote``.
CAPITAL_GAINS_SPEC = ReportSpec("Capital Gains and Taxes", _run_capital_gains,
                                capital_gains_rows,
                                columns=CAPITAL_GAINS_COLUMNS,
                                sortable={0: "account", 1: "ticker",
                                          2: "acquired", 3: "term",
                                          4: "longon", 8: "value",
                                          9: "unrealized"},
                                right_align_from=6,
                                show_end_date=True,
                                fit_columns=True,
                                footnote=capital_gains_footnote,
                                default_size=(1040, 660),
                                price_history=True)
ITEMIZE_SPEC = ReportSpec("Itemize by Category", _run_itemize, itemize_tree_rows,
                          show_hidden_toggle=False,
                          category_kind=CATEGORY_KIND_BOTH,
                          columns=TREE_COLUMNS, is_tree=True)


class ReportWindow(QDialog):
    """A window hosting one report (a :class:`ReportSpec`) whose customization
    controls live behind a gear button.

    Modeless: open it with :meth:`show` and keep a reference. The gear button
    (``customize_button``) opens a :class:`~mammon.ui.report_filters.CustomizeDialog`
    holding the shared filter bar — the same idiom the app's other report windows
    use; its Apply button re-runs the report. "Export CSV…" writes the current
    rows. ``spec`` defaults to Cash Flow so the historical one-arg call still works.
    """

    def __init__(self, conn, parent=None, *, settings=None, spec=None):
        super().__init__(parent)
        self.conn = conn
        # Optional injected QSettings store for the saved filter sets; None uses
        # the shared user-scope store (which honors QSettings.setPath in tests).
        self._settings = settings
        self.spec = spec or CASH_FLOW_SPEC
        self.setWindowTitle(self.spec.title)
        # A report's own header set: the shared three columns, or its override
        # (Itemize's Category/Amount). Table, CSV, HTML and PDF all read this.
        self.columns = list(self.spec.columns)
        self._rows: list[ReportRow] = []
        # Clickable-header sort state, shared by the Itemize drill-down tree and any
        # flat report that declares ``spec.sortable`` (Investment Performance): the
        # clicked column's key and direction, plus the last pure report object so a
        # header click can re-project without re-querying the ledger. See
        # _on_tree_sort / _on_table_sort.
        self._sort_key = None
        self._sort_desc = False
        self._report = None

        start, end = _default_range(conn)
        # The customization/filter controls (date range, accounts, the
        # include-hidden toggle) live behind a gear button, not inline — the same
        # gear-and-CustomizeDialog idiom the app's other report windows use (see
        # report_filters.customize_button / CustomizeDialog, and MainWindow's
        # _report_customize_header). The spec decides which check-lists are
        # meaningful: ``show_accounts`` for the account list, ``category_kind``
        # for the top-level category list. The latter used to be hardcoded off on
        # the grounds that "these reports carry no category filter" — untrue since
        # Itemize gained ``top_level_names``, which left its category picker dead:
        # _run_itemize threaded selected_categories() through to itemize_tree, but
        # the list was never built so the getter always answered None. A report
        # whose ``run`` ignores selected_categories() must still leave the kind
        # None, or the user gets a control that silently does nothing. The window
        # no longer builds the list itself: it names a SCOPE and the shared picker
        # (report_filters.category_picker_names) answers with the categories of
        # that kind, so Itemize's income section is tickable like its expenses.
        self.customize_dialog = CustomizeDialog(
            conn, start, end,
            show_accounts=self.spec.show_accounts,
            category_kind=self.spec.category_kind,
            show_hidden_toggle=self.spec.show_hidden_toggle,
            parent=self)
        # The live bar inside the popup. Every getter (start_iso / end_iso /
        # selected_account_ids …) and the saved-filter (de)serializers read through
        # this attribute, so tucking the controls behind the gear changes only where
        # they render, nothing about how the report reads them.
        self.filters = self.customize_dialog.filters

        # A right-aligned gear opens the popup — matches _report_customize_header
        # on the other report windows exactly.
        self.gear_button = customize_button(self.customize_dialog, self)

        # -- Period dropdown: one preset picker every report shares, on top ----
        # Built by the shared factory so its width (sized for "Earliest to date")
        # matches the chart windows'; the currentIndexChanged connect below runs
        # AFTER this, so seeding the default here fires no refresh.
        self.period_combo = make_period_combo()
        # Kept as an attribute (not a local) so a test can inspect the row's
        # layout items directly -- e.g. confirming the end-date label sits
        # between two stretches rather than just checking its text.
        self.period_row = period_row = QHBoxLayout()
        period_row.addWidget(QLabel("Period:"))
        period_row.addWidget(self.period_combo)
        period_row.addStretch(1)
        # Income vs Expense only: the range's end date, centered between the
        # combo and the gear via a stretch on each side. ``refresh`` keeps the
        # text in step with the active range; every other spec leaves this None
        # and the row is unchanged from before this control existed.
        self.end_date_label = QLabel() if self.spec.show_end_date else None
        if self.end_date_label is not None:
            period_row.addWidget(self.end_date_label)
            period_row.addStretch(1)
        period_row.addWidget(self.gear_button)

        # -- named saved filter sets (persist to QSettings, never the DB) ------
        # These render INSIDE the customize (gear) popup, not inline in the
        # window, so range / accounts / categories AND saved sets share the one
        # gear affordance. Save/Delete are denied auto-default so Enter in the
        # popup still means Apply, not "save" or "delete".
        self.saved_combo = QComboBox()
        self.saved_combo.setToolTip("Recall a saved filter set")
        self.save_filters_button = QPushButton("Save Filters…")
        self.delete_filters_button = QPushButton("Delete")
        self.save_filters_button.setAutoDefault(False)
        self.delete_filters_button.setAutoDefault(False)
        saved_row = QHBoxLayout()
        saved_row.addWidget(QLabel("Saved filters:"))
        saved_row.addWidget(self.saved_combo)
        saved_row.addWidget(self.save_filters_button)
        saved_row.addWidget(self.delete_filters_button)
        saved_row.addStretch(1)
        self.customize_dialog.add_saved_filter_row(saved_row)

        # Two rendering shapes share this one window. A flat report fills a
        # QTableWidget; a drill-down report (Itemize) fills a QTreeWidget whose
        # rows nest Category -> sub-category -> transactions. Exactly one of
        # ``self.table`` / ``self.tree`` is created; the other stays None.
        self.table = None
        self.tree = None
        if self.spec.is_tree:
            self.tree = QTreeWidget()
            self.tree.setColumnCount(len(self.columns))
            self.tree.setHeaderLabels(self.columns)
            self.tree.setEditTriggers(QTreeWidget.NoEditTriggers)
            self.tree.setUniformRowHeights(True)
            header = self.tree.header()
            header.setStretchLastSection(False)
            header.setSectionResizeMode(0, QHeaderView.Stretch)
            for i in range(1, len(self.columns)):
                header.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            # Click a column header to sort the transactions within each open group
            # (Date / Payee / Amount). The tree is populated by hand, not by Qt's
            # own model sort, so we drive the ordering through itemize_tree_rows;
            # the indicator is cosmetic. Category (col 0) is the tree itself.
            header.setSectionsClickable(True)
            header.setSortIndicatorShown(True)
            header.sectionClicked.connect(self._on_tree_sort)
            self._body_widget = self.tree
        else:
            self.table = QTableWidget(0, len(self.columns))
            self.table.setHorizontalHeaderLabels(self.columns)
            self.table.setEditTriggers(QTableWidget.NoEditTriggers)
            self.table.setSelectionBehavior(QTableWidget.SelectRows)
            self.table.horizontalHeader().setStretchLastSection(True)
            if self.spec.fit_first_column:
                # By Payee: fit the Payee column to its widest name so no payee is
                # truncated on open; the trailing amount column still stretches.
                self.table.horizontalHeader().setSectionResizeMode(
                    0, QHeaderView.ResizeToContents)
            self.table.verticalHeader().setVisible(False)
            if self.spec.sortable:
                # Drive ordering by hand through the projector (Qt's own model sort
                # stays off) so the Portfolio totals never move; the indicator is
                # cosmetic. Same seam as the tree, see _on_table_sort.
                header = self.table.horizontalHeader()
                header.setSectionsClickable(True)
                header.setSortIndicatorShown(True)
                header.sectionClicked.connect(self._on_table_sort)
            # Right-click a row -> see _on_table_context_menu. Connected for every
            # flat report because it is generically harmless: the handler pops up
            # nothing at all unless the row under the cursor names a security.
            self.table.setContextMenuPolicy(Qt.CustomContextMenu)
            self.table.customContextMenuRequested.connect(
                self._on_table_context_menu)
            self._body_widget = self.table

        self.export_button = QPushButton("Export CSV…")
        self.export_html_button = QPushButton("Export HTML…")
        self.print_button = QPushButton("Print / PDF…")
        self.close_button = QPushButton("Close")
        buttons = QHBoxLayout()
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.export_html_button)
        buttons.addWidget(self.print_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        # The whole-report caveats (rate assumptions, which accounts were left out
        # of the tax math) live here rather than repeated down a table column --
        # that repetition is what made Capital Gains unreadable at default width.
        self.footnote_label = None
        if self.spec.footnote is not None:
            self.footnote_label = QLabel("")
            self.footnote_label.setWordWrap(True)
            f = self.footnote_label.font()
            f.setPointSizeF(max(f.pointSizeF() - 1, 6.0))
            self.footnote_label.setFont(f)

        layout = QVBoxLayout(self)
        layout.addLayout(period_row)
        layout.addWidget(self._body_widget)
        if self.footnote_label is not None:
            layout.addWidget(self.footnote_label)
        layout.addLayout(buttons)

        # Apply in the popup re-runs the report, then the dialog dismisses itself
        # (CustomizeDialog re-emits `applied` and calls accept on Apply). The
        # combo sync runs FIRST so the Period label already agrees with the range
        # the refreshed report was built from.
        self.customize_dialog.applied.connect(self.sync_period_combo)
        self.customize_dialog.applied.connect(self.refresh)
        # Connect AFTER the initial setCurrentIndex above so building the combo
        # fires no refresh; a later user pick does.
        self.period_combo.currentIndexChanged.connect(self._on_period)
        self.saved_combo.activated.connect(self._on_saved_selected)
        self.save_filters_button.clicked.connect(self._save_filters_dialog)
        self.delete_filters_button.clicked.connect(self._delete_filters)
        self.export_button.clicked.connect(self._export_csv_dialog)
        self.export_html_button.clicked.connect(self._export_html_dialog)
        self.print_button.clicked.connect(self._print_pdf_dialog)
        self.close_button.clicked.connect(self.reject)

        self._reload_saved_filters()
        # 620px suits a three-column report; a ten-column one opens wider or the
        # user has to maximize the window to read it (the reported defect).
        self.resize(*(self.spec.default_size or (620, 640)))
        self.refresh()

    # The window used to build its own category name list here
    # (``_top_level_categories``). It is gone: the list is now built once, for
    # every report, by ``report_filters.category_picker_names`` over
    # ``category_types.top_level_categories``, and this window's only say in it
    # is ``spec.category_kind``. Four ad-hoc lists is how the picker ended up
    # expense-only in the first place.

    # -- data ----------------------------------------------------------------
    def refresh(self):
        """Re-run the report against the current filter selection and repaint.

        The spec's ``run`` calls the pure report function with the bar's getters
        and ``project`` flattens the result — this method does no SQL, no math.
        """
        report = self.spec.run(self.conn, self.filters)
        self._report = report
        if self.end_date_label is not None:
            self.end_date_label.setText(fmt_date(self.filters.end_iso()))
        if self.spec.is_tree:
            # The tree projector takes the active column sort; flat projectors do
            # not, so only this branch threads it (spec.project is itemize_tree_rows).
            self._rows = self.spec.project(report, sort_key=self._sort_key,
                                           sort_desc=self._sort_desc)
            self._populate_tree(self._rows)
        elif self.spec.sortable:
            # A flat report with clickable-header sort threads the active sort into
            # its projector, just like the tree branch above.
            self._rows = self.spec.project(report, sort_key=self._sort_key,
                                           sort_desc=self._sort_desc)
            self._populate(self._rows)
        else:
            self._rows = self.spec.project(report)
            self._populate(self._rows)

    def _populate(self, rows):
        self.table.setRowCount(len(rows))
        last = len(self.columns) - 1
        # The By Tag report carries a tag NAME per row; color each row's label
        # cell with that tag's identity color (ledger.tag_colors), so a tag reads
        # the same here as in the register. The Total row (no tag) is left plain.
        tag_colors = (ledger.tag_colors(self.conn)
                      if getattr(self._report, "key", None) == "tag" else None)
        for i, r in enumerate(rows):
            # A report whose lines name a security carries the BARE ticker on every
            # item of the row (Qt.UserRole), so the right-click menu reads the
            # symbol off the item the user clicked instead of re-deriving it from a
            # row index -- a click-to-sort reshuffles the rows under the same
            # indexes, and an index-to-data lookup would then chart the wrong
            # holding. Total rows carry nothing, which is how they offer nothing.
            symbol = _row_symbol(r) if self.spec.price_history else ""
            for col, cell in enumerate(_row_cells(r, self.columns)):
                item = QTableWidgetItem(cell)
                if symbol:
                    item.setData(Qt.UserRole, symbol)
                align_from = self.spec.right_align_from
                if col == last or (align_from is not None and col >= align_from):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if tag_colors is not None and col == last - 1 and r.label != "Total":
                    color = tag_colors.get((r.label or "").casefold())
                    if color:
                        item.setIcon(color_square_icon(color))
                # The long form of a cell that had to be short. Hover, not width:
                # a column sized to hold the sentence is what pushed the money
                # columns off screen in Capital Gains.
                tip = (r.tooltips or {}).get(col) if r.tooltips else None
                if tip:
                    item.setToolTip(tip)
                self.table.setItem(i, col, item)
        if self.spec.fit_columns:
            # One-shot, AFTER the rows exist: leaves every column interactive, so
            # the user can still widen one by hand (ResizeToContents would not).
            self.table.resizeColumnsToContents()
        if self.footnote_label is not None:
            self.footnote_label.setText(self.spec.footnote(self._report)
                                        if self._report is not None else "")

    # -- right-click: price history (Investment Performance) -----------------
    def _symbol_at(self, index) -> str:
        """The bare ticker named by the row under ``index``, or ``''`` for a total
        row, a click over empty space, or a report whose rows name no security."""
        if self.table is None or index is None or not index.isValid():
            return ""
        item = self.table.item(index.row(), index.column())
        if item is None:
            return ""
        return str(item.data(Qt.UserRole) or "")

    def _chart_account_id(self, row: int, symbol: str):
        """The account the displayed holding line lives in, so its chart is drawn
        in THAT account's currency (SRD 5.8). Unlike the register and the Holdings
        window, this report is not scoped to one account, so the account is read
        back off the already-computed report (no SQL here) and disambiguated by the
        row's Account cell -- one security can be held in several accounts."""
        item = self.table.item(row, 0) if self.table is not None else None
        account = item.text() if item is not None else ""
        for h in getattr(self._report, "holdings", None) or ():
            if h.symbol == symbol and h.account_name == account:
                return h.account_id
        return None

    def _open_price_history(self, symbol, account_id=None):
        """Seam: open the SHARED price-history chart -- literally the helper the
        investment register and the Holdings window open, so all three entry
        points behave identically (currency labeling, bounds, the "nothing
        recorded" note). Imported lazily so this module does not drag the register
        widgets (and matplotlib behind them) into every report import, and kept
        this small so a test can patch one method instead of a modal."""
        from mammon.ui.widgets import _chart_price_history
        _chart_price_history(self, self.conn, symbol, account_id)

    def _on_table_context_menu(self, pos):
        """Right-click on a flat report row.

        Today it offers exactly one entry, "Price history: SYM…", carrying the same
        label as the investment register's context menu so the two read identically
        (SRD 5.8). A row naming no security -- a Portfolio total, any other report --
        and a click over empty space return before a menu is built, so the user
        never gets an empty popup.
        """
        index = self.table.indexAt(pos)
        symbol = self._symbol_at(index)
        if not symbol:
            return
        menu = QMenu(self)
        act_price = menu.addAction(f"Price history: {symbol}…")
        chosen = menu.exec_(self.table.viewport().mapToGlobal(pos))
        if chosen is not None and chosen is act_price:
            self._open_price_history(
                symbol, self._chart_account_id(index.row(), symbol))

    def _populate_tree(self, rows, expanded_paths=None):
        """Rebuild the drill-down tree from the flat depth-tagged projection.

        The projection is a pre-order list of :class:`TreeRow`\\s; nesting is
        reconstructed with a depth stack (a row parents the deepest open row with a
        smaller depth). The last column (Amount) is right-aligned and tinted red
        when negative; section and total rows render bold. No money math here — the
        cells are already formatted by :func:`itemize_tree_rows`.

        ``expanded_paths`` (a set of column-0 label tuples from
        :meth:`_tree_expanded_paths`) restores which groups were open across a
        re-sort; ``None`` uses each row's default (sections open, rest collapsed).
        """
        self.tree.clear()
        last = len(self.columns) - 1
        red = QBrush(QColor("#c0392b"))
        bold = QFont()
        bold.setBold(True)
        stack: list = []  # (depth, item, path)
        for r in rows:
            item = QTreeWidgetItem(list(r.cells))
            item.setTextAlignment(last, Qt.AlignRight | Qt.AlignVCenter)
            if r.amount < 0:
                item.setForeground(last, red)
            if r.bold:
                for col in range(len(self.columns)):
                    item.setFont(col, bold)
            while stack and stack[-1][0] >= r.depth:
                stack.pop()
            path = (stack[-1][2] if stack else ()) + (r.cells[0],)
            if stack:
                stack[-1][1].addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            if expanded_paths is None:
                item.setExpanded(r.expanded)
            else:
                item.setExpanded(path in expanded_paths)
            stack.append((r.depth, item, path))

    def _tree_expanded_paths(self) -> set:
        """The column-0 label paths of the currently-expanded tree items.

        A re-sort never renames or reorders the grouping nodes (only the leaf
        transactions and — under Amount — the transfer counterparties move), so a
        label path is a stable identity for restoring the user's open groups.
        """
        paths: set = set()

        def walk(item, prefix):
            path = prefix + (item.text(0),)
            if item.isExpanded():
                paths.add(path)
            for i in range(item.childCount()):
                walk(item.child(i), path)

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i), ())
        return paths

    def _on_tree_sort(self, col: int) -> None:
        """Sort the transactions within each open group by the clicked column.

        Date -> (date, payee), Payee -> (payee, date), Amount -> (amount, date); a
        second click on the same column toggles ascending/descending. Transfer
        counterparty nodes stay alphabetical unless Amount is the key. Column 0
        (Category) is the tree structure itself and is not a sort key. Re-projects
        the cached report (no ledger re-read) and keeps the open groups open.
        """
        key = {1: "date", 2: "payee", 3: "amount"}.get(col)
        if key is None or self._report is None:
            return
        if self._sort_key == key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key, self._sort_desc = key, False
        order = Qt.DescendingOrder if self._sort_desc else Qt.AscendingOrder
        self.tree.header().setSortIndicator(col, order)
        expanded = self._tree_expanded_paths()
        self._rows = self.spec.project(self._report, sort_key=self._sort_key,
                                       sort_desc=self._sort_desc)
        self._populate_tree(self._rows, expanded_paths=expanded)

    def _on_table_sort(self, col: int) -> None:
        """Sort a flat report's rows by the clicked column, reusing the very same
        sort_key/sort_desc mechanism as the Itemize tree (:meth:`_on_tree_sort`).

        The spec's ``sortable`` map says which columns sort and to which key; a
        column outside it is inert (Amount). A second click on the same column
        toggles ascending/descending. Re-projects the cached report (no ledger
        re-read) so only the display order changes; the Portfolio totals, appended
        by the projector after the sorted line items, never move.
        """
        key = (self.spec.sortable or {}).get(col)
        if key is None or self._report is None:
            return
        if self._sort_key == key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key, self._sort_desc = key, False
        order = Qt.DescendingOrder if self._sort_desc else Qt.AscendingOrder
        self.table.horizontalHeader().setSortIndicator(col, order)
        self._rows = self.spec.project(self._report, sort_key=self._sort_key,
                                       sort_desc=self._sort_desc)
        self._populate(self._rows)

    # -- period preset dropdown ----------------------------------------------
    def _on_period(self, index) -> None:
        """Apply the chosen period preset. 'Custom' opens the gear's customize
        popup so the user sets an explicit range; every other key resolves to a
        concrete date range and refreshes immediately."""
        key = self.period_combo.itemData(index)
        if key == "custom":
            self._open_customize()
            return
        rng = resolve_period(key, self.conn, _dt.date.today())
        if rng:
            self.filters.set_range(*rng)
            self.refresh()

    def sync_period_combo(self) -> str:
        """Make the Period dropdown describe the range the report actually uses.

        The dropdown is an input AND a label. A range typed into the customize
        (gear) dialog, or recalled from a saved filter set, used to leave it
        advertising the stale preset it no longer matched; now it re-reads the
        live From/To dates and shows the preset they equal, or ``Custom`` when
        they equal none (§5.9b). Signals stay blocked inside
        :func:`sync_period_combo`, so this never re-enters :meth:`_on_period` --
        on ``"custom"`` that would reopen the very dialog that triggered it.
        """
        return sync_period_combo(self.period_combo, self.filters.start_iso(),
                                 self.filters.end_iso(), self.conn,
                                 _dt.date.today())

    def _open_customize(self) -> None:
        """Open the customize (gear) popup. Overridable seam so a headless test
        can select 'Custom' without blocking on the modal ``exec_()``."""
        self.customize_dialog.exec_()

    # -- saved filter sets ---------------------------------------------------
    def current_filter_state(self) -> dict:
        """The live filter bar captured as a plain dict (pure serializer)."""
        return filter_state_to_dict(self.filters)

    def save_current_filters(self, name) -> bool:
        """Persist the current filter selection under ``name`` and select it in
        the combo. Returns False for a blank name (nothing saved). Testable seam:
        the name prompt lives in :meth:`_save_filters_dialog`."""
        name = (name or "").strip()
        if not name:
            return False
        save_filter_set(name, self.current_filter_state(), self._settings)
        self._reload_saved_filters(select=name)
        return True

    def apply_saved_filters(self, name) -> bool:
        """Load the saved set ``name``, apply it to the bar, and refresh. Returns
        False when there is no such set."""
        state = load_filter_set(name, self._settings)
        if state is None:
            return False
        apply_filter_state(self.filters, state)
        # A saved set carries its own date range, so the Period label has to
        # follow it too -- same reason as the gear dialog's Apply.
        self.sync_period_combo()
        self.refresh()
        return True

    def delete_saved_filters(self, name) -> bool:
        """Remove the saved set ``name`` and rebuild the combo. Returns False for
        a blank name."""
        name = (name or "").strip()
        if not name:
            return False
        delete_filter_set(name, self._settings)
        self._reload_saved_filters()
        return True

    def _reload_saved_filters(self, select=None) -> None:
        """Rebuild the combo from QSettings. Index 0 is a blank placeholder so
        the combo has a "nothing chosen" state and construction never auto-applies
        a set. Signals are blocked so the rebuild itself fires no selection."""
        self.saved_combo.blockSignals(True)
        self.saved_combo.clear()
        self.saved_combo.addItem("")
        for name in saved_filter_names(self._settings):
            self.saved_combo.addItem(name)
        idx = self.saved_combo.findText(select) if select else 0
        self.saved_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.saved_combo.blockSignals(False)

    def _on_saved_selected(self, index) -> None:
        name = self.saved_combo.itemText(index)
        if name:
            self.apply_saved_filters(name)

    def _prompt_filter_name(self):
        """Ask for a filter-set name (defaulting to the combo's current text).
        Overridable seam so headless tests never open the modal QInputDialog;
        returns the trimmed name, or None if cancelled/blank."""
        text, ok = QInputDialog.getText(
            self, "Save Filters", "Name for this filter set:",
            text=self.saved_combo.currentText())
        if not ok:
            return None
        return (text or "").strip() or None

    def _save_filters_dialog(self) -> None:
        name = self._prompt_filter_name()
        if name:
            self.save_current_filters(name)

    def _delete_filters(self) -> None:
        self.delete_saved_filters(self.saved_combo.currentText())

    # -- export --------------------------------------------------------------
    def _rows_to_csv(self) -> str:
        """The current rows as CSV text via the matching pure serializer: the
        drill-down (:func:`tree_rows_to_csv`, indented by depth) for a tree report,
        else the flat :func:`report_rows_to_csv`."""
        if self.spec.is_tree:
            return tree_rows_to_csv(self._rows, self.columns)
        return report_rows_to_csv(self._rows, self.columns)

    def _rows_to_html(self, title) -> str:
        """The current rows as an HTML document via the matching pure serializer
        (tree vs flat), paralleling :meth:`_rows_to_csv`."""
        if self.spec.is_tree:
            return tree_rows_to_html(self._rows, title, self.columns)
        return report_rows_to_html(self._rows, title, self.columns)

    def export_csv_to(self, path) -> None:
        """Write the currently displayed rows to ``path`` as CSV.

        Testable seam: the file dialog lives in :meth:`_export_csv_dialog`; this
        method takes an explicit path so a test can drive the write without a
        modal.
        """
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write(self._rows_to_csv())

    def _default_filename(self, ext) -> str:
        """A dialog default like ``cash_flow.csv`` derived from the report title."""
        slug = self.spec.title.lower().replace(" ", "_")
        return "%s.%s" % (slug, ext)

    def _export_csv_dialog(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", self._default_filename("csv"),
            "CSV files (*.csv)")
        if not path:
            return
        try:
            self.export_csv_to(path)
        except OSError as exc:
            QMessageBox.warning(self, "Export CSV",
                                "Could not write file:\n%s" % exc)
            return
        QMessageBox.information(
            self, "Export CSV",
            "Wrote %d rows to\n%s" % (len(self._rows), path))

    def export_html_to(self, path) -> None:
        """Write the currently displayed rows to ``path`` as an HTML document.

        Testable seam paralleling :meth:`export_csv_to`: the file dialog lives in
        :meth:`_export_html_dialog`, this method takes an explicit path so a test
        can drive the write without a modal.
        """
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self._rows_to_html(self.windowTitle()))

    def _export_html_dialog(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export HTML", self._default_filename("html"),
            "HTML files (*.html)")
        if not path:
            return
        try:
            self.export_html_to(path)
        except OSError as exc:
            QMessageBox.warning(self, "Export HTML",
                                "Could not write file:\n%s" % exc)
            return
        QMessageBox.information(
            self, "Export HTML",
            "Wrote %d rows to\n%s" % (len(self._rows), path))

    # -- print / pdf ---------------------------------------------------------
    def print_to_pdf(self, path) -> str:
        """Render the currently displayed rows to a PDF file at ``path``.

        Testable seam: renders through the shared QTextDocument/QPrinter helper
        in :mod:`mammon.ui.printing` (no second print path), with no dialog, so
        the PDF output is verified headless. Returns the path written.
        """
        from mammon.ui import printing

        title = self.windowTitle()
        html_str = self._rows_to_html(title)
        return printing.render_html_to_pdf(html_str, str(path), title=title)

    def _print_pdf_dialog(self):
        from PyQt5.QtPrintSupport import QPrinter, QPrintDialog

        from mammon.ui import printing

        title = self.windowTitle()
        html_str = self._rows_to_html(title)
        printer = QPrinter(QPrinter.HighResolution)
        printer.setDocName("Mammon - %s" % title)
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("Print / PDF")
        # The system dialog includes 'Print to PDF/File', so this one action
        # covers both a physical printout and a saved PDF (Quicken's Print).
        if dlg.exec_() != QDialog.Accepted:
            return
        try:
            printing.render_html_to_printer(html_str, printer)
        except Exception as exc:  # pragma: no cover - printer/IO failure path
            QMessageBox.warning(self, "Print / PDF",
                                "Could not print:\n%s" % exc)

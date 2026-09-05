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
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from mammon import ledger, reports
from mammon.ui.models import fmt_cents
from mammon.ui.report_filters import (
    PERIOD_DEFAULT,
    CustomizeDialog,
    customize_button,
    make_period_combo,
    resolve_period,
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
    """

    section: str
    label: str
    amount: int
    cells: list = None


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
INVESTMENT_PERFORMANCE_COLUMNS = ["Account", "Ticker", "Amount",
                                  "Gain/Loss $", "Gain/Loss %"]


def _fmt_pct(pct) -> str:
    """A signed one-decimal percent, or blank when there is nothing to measure
    (an unpriced or closed position). Text, never a number -- the Gain/Loss %
    column is a display string, not money."""
    return "" if pct is None else "%+.1f%%" % pct


def investment_performance_rows(report: "reports.InvestmentPerformanceReport") -> list[ReportRow]:
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
    """
    rows: list[ReportRow] = []
    for h in report.holdings:
        if not h.is_open:
            continue
        ticker = "%s (%s sh)" % (h.symbol, _fmt_qty(h.quantity))
        gl = "" if h.unrealized_pl is None else fmt_cents(h.unrealized_pl)
        rows.append(ReportRow(
            h.account_name, h.symbol, h.market_value,
            cells=[h.account_name, ticker, fmt_cents(h.market_value),
                   gl, _fmt_pct(h.pct_return)]))

    # Portfolio totals. The market-value line doubles as the portfolio Gain/Loss
    # summary (its unrealized dollars and percent); the remaining totals each name a
    # single figure, placed in the Amount column with the gain columns blank.
    rows.append(ReportRow(
        "Portfolio", "Cost Basis", report.total_cost_basis,
        cells=["Portfolio", "Cost Basis", fmt_cents(report.total_cost_basis),
               "", ""]))
    rows.append(ReportRow(
        "Portfolio", "Market Value", report.total_market_value,
        cells=["Portfolio", "Market Value", fmt_cents(report.total_market_value),
               fmt_cents(report.total_unrealized_pl),
               _fmt_pct(report.pct_return)]))
    rows.append(ReportRow(
        "Portfolio", "Unrealized Gain/Loss", report.total_unrealized_pl,
        cells=["Portfolio", "Unrealized Gain/Loss", "",
               fmt_cents(report.total_unrealized_pl),
               _fmt_pct(report.pct_return)]))
    rows.append(ReportRow(
        "Portfolio", "Realized Gain/Loss", report.total_realized_pl,
        cells=["Portfolio", "Realized Gain/Loss", "",
               fmt_cents(report.total_realized_pl), ""]))
    rows.append(ReportRow(
        "Portfolio", "Dividend/Interest Income", report.total_dividends,
        cells=["Portfolio", "Dividend/Interest Income",
               fmt_cents(report.total_dividends), "", ""]))
    if report.total_return_of_capital:
        rows.append(ReportRow(
            "Portfolio", "Return of Capital", report.total_return_of_capital,
            cells=["Portfolio", "Return of Capital",
                   fmt_cents(report.total_return_of_capital), "", ""]))
    return rows


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
    whose pure function takes no such flag. ``columns`` is the report's own header
    set — ``None`` means the shared three-column default, and a report like Itemize
    overrides it (``["Category", "Amount"]``); :func:`_row_cells` maps a
    ``ReportRow`` onto whichever set is in force.
    """

    title: str
    run: Callable
    project: Callable
    show_accounts: bool = True
    show_hidden_toggle: bool = True
    columns: list = None
    is_tree: bool = False
    # Size the first (text) column to its contents so long values are not clipped
    # -- By Payee sets this so a full payee name shows without a manual drag.
    fit_first_column: bool = False

    def __post_init__(self):
        if self.columns is None:
            self.columns = list(COLUMNS)


def _run_cash_flow(conn, f):
    return reports.cash_flow(conn, f.start_iso(), f.end_iso(),
                             account_ids=f.selected_account_ids(),
                             include_hidden=f.include_hidden())


def _run_income_expense(conn, f):
    return reports.income_expense(conn, f.start_iso(), f.end_iso(),
                                  account_ids=f.selected_account_ids(),
                                  include_hidden=f.include_hidden())


def _run_account_balances(conn, f):
    # account_balances takes no account filter; the "To" date is its as-of.
    return reports.account_balances(conn, f.end_iso(),
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
    return reports.transactions(conn, f.start_iso(), f.end_iso(),
                                account_ids=f.selected_account_ids(),
                                include_hidden=f.include_hidden())


def _run_investment_performance(conn, f):
    # A per-holding snapshot valued as of the "To" date; the account checklist
    # filters it, and only investment accounts contribute holdings.
    return reports.investment_performance(conn, f.end_iso(),
                                          account_ids=f.selected_account_ids(),
                                          include_hidden=f.include_hidden())


def _run_itemize(conn, f):
    # The hierarchical Itemize: INCOME / EXPENSES / TRANSFERS sections, each
    # category expanding into its sub-categories and finally its transactions, with
    # every amount rolled up (income first, then expense, then transfer
    # counterparties). itemize_tree takes no include-hidden flag, so ITEMIZE_SPEC
    # sets show_hidden_toggle=False rather than show a dead control. The category
    # checklist's picks (selected_categories) restrict which top-level categories
    # appear; None means all.
    return reports.itemize_tree(conn, f.start_iso(), f.end_iso(),
                                account_ids=f.selected_account_ids(),
                                top_level_names=f.selected_categories())


# Cash Flow and Income vs Expense are deliberately BOTH kept -- they are not the
# same table: Cash Flow adds a TRANSFERS section for money crossing the selected
# account set and its Net INCLUDES those transfers (did cash in these accounts rise
# or fall), while Income vs Expense excludes transfers entirely and its Net is
# income + expense only (did I earn more than I spent). Cash Flow's first column is
# therefore "Direction" (Income / Expense / Transfers / Net), not the shared
# "Section", to name what that column actually distinguishes.
CASH_FLOW_SPEC = ReportSpec("Cash Flow", _run_cash_flow, cash_flow_rows,
                            columns=["Direction", "Category / Account", "Amount"])
INCOME_EXPENSE_SPEC = ReportSpec("Income vs Expense", _run_income_expense,
                                 income_expense_rows)
ACCOUNT_BALANCES_SPEC = ReportSpec("Account Balances", _run_account_balances,
                                   account_balances_rows, show_accounts=False,
                                   columns=["Account Type", "Category / Account",
                                            "Amount"])
BY_PAYEE_SPEC = ReportSpec("By Payee", _run_by_payee, payee_rows,
                           columns=["Payee", "Net Amount"], fit_first_column=True)
BY_TAG_SPEC = ReportSpec("By Tag", _run_by_tag, payee_rows)
TRANSACTIONS_SPEC = ReportSpec("Transactions", _run_transactions, listing_rows,
                               columns=TRANSACTIONS_COLUMNS)
INVESTMENT_PERFORMANCE_SPEC = ReportSpec("Investment Performance",
                                         _run_investment_performance,
                                         investment_performance_rows,
                                         columns=INVESTMENT_PERFORMANCE_COLUMNS)
ITEMIZE_SPEC = ReportSpec("Itemize by Category", _run_itemize, itemize_tree_rows,
                          show_hidden_toggle=False, columns=TREE_COLUMNS,
                          is_tree=True)


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
        # Itemize drill-down sort state (unused by flat reports): the clicked
        # column key and direction, plus the last pure report object so a header
        # click can re-project without re-querying the ledger. See _on_tree_sort.
        self._sort_key = None
        self._sort_desc = False
        self._report = None

        start, end = _default_range(conn)
        # The customization/filter controls (date range, accounts, the
        # include-hidden toggle) live behind a gear button, not inline — the same
        # gear-and-CustomizeDialog idiom the app's other report windows use (see
        # report_filters.customize_button / CustomizeDialog, and MainWindow's
        # _report_customize_header). These reports carry no category filter, so the
        # category checklist stays hidden; the spec decides whether the account
        # checklist is meaningful.
        self.customize_dialog = CustomizeDialog(
            conn, start, end,
            show_accounts=self.spec.show_accounts,
            categories=None,
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
        period_row = QHBoxLayout()
        period_row.addWidget(QLabel("Period:"))
        period_row.addWidget(self.period_combo)
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

        layout = QVBoxLayout(self)
        layout.addLayout(period_row)
        layout.addWidget(self._body_widget)
        layout.addLayout(buttons)

        # Apply in the popup re-runs the report, then the dialog dismisses itself
        # (CustomizeDialog re-emits `applied` and calls accept on Apply).
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
        self.resize(620, 640)
        self.refresh()

    # -- data ----------------------------------------------------------------
    def refresh(self):
        """Re-run the report against the current filter selection and repaint.

        The spec's ``run`` calls the pure report function with the bar's getters
        and ``project`` flattens the result — this method does no SQL, no math.
        """
        report = self.spec.run(self.conn, self.filters)
        self._report = report
        if self.spec.is_tree:
            # The tree projector takes the active column sort; flat projectors do
            # not, so only this branch threads it (spec.project is itemize_tree_rows).
            self._rows = self.spec.project(report, sort_key=self._sort_key,
                                           sort_desc=self._sort_desc)
            self._populate_tree(self._rows)
        else:
            self._rows = self.spec.project(report)
            self._populate(self._rows)

    def _populate(self, rows):
        self.table.setRowCount(len(rows))
        last = len(self.columns) - 1
        for i, r in enumerate(rows):
            for col, cell in enumerate(_row_cells(r, self.columns)):
                item = QTableWidgetItem(cell)
                if col == last:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, col, item)

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

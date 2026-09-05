"""Tests for the generalized report window (§5.9b).

The reusable :class:`~mammon.ui.report_window.ReportWindow` now hosts every
§5.9a report, not just Cash Flow: Income vs Expense, Account Balances, By Payee
and the Transactions listing each ship a pure ``*_rows`` projector plus a
``ReportSpec`` (title + a ``run`` that calls the pure ``reports.*`` function).
This module covers the pure projectors with no Qt or DB, then the offscreen
construction / CSV / HTML / PDF seams of the real windows. Synthetic data only —
no PII.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db
from mammon.reports import (
    AccountBalance,
    BalanceReport,
    FlowRow,
    IncomeExpenseReport,
    ListingReport,
    PayeeReport,
    PayeeRow,
)
from mammon.ui.report_window import (
    ACCOUNT_BALANCES_SPEC,
    BY_PAYEE_SPEC,
    CASH_FLOW_SPEC,
    COLUMNS,
    INCOME_EXPENSE_SPEC,
    ITEMIZE_SPEC,
    TRANSACTIONS_COLUMNS,
    TRANSACTIONS_SPEC,
    TREE_COLUMNS,
    ReportRow,
    ReportWindow,
    _row_cells,
    account_balances_rows,
    income_expense_rows,
    itemize_rows,
    itemize_tree_rows,
    listing_rows,
    payee_rows,
    report_rows_to_csv,
    report_rows_to_html,
    tree_rows_to_csv,
    tree_rows_to_html,
)
from mammon.ui.models import fmt_cents, fmt_date


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _flow(path, type_, total):
    return FlowRow(category_id=None, path=path, top=path, type=type_,
                   by_bucket={"total": total}, total=total)


# ---- pure: Income vs Expense projection -----------------------------------

def test_income_expense_rows_projection():
    report = IncomeExpenseReport(
        start="2026-01-01", end="2026-12-31", bucket="month", buckets=["total"],
        account_ids=[1],
        income=[_flow("Salary", "income", 500000)],
        expense=[_flow("Groceries", "expense", -30000)],
        total_income=500000, total_expense=-30000, net=470000)

    rows = income_expense_rows(report)

    assert rows == [
        ReportRow("Income", "Salary", 500000),
        ReportRow("Income", "Total Income", 500000),
        ReportRow("Expense", "Groceries", -30000),
        ReportRow("Expense", "Total Expense", -30000),
        ReportRow("Net", "Net Income", 470000),
    ]


# ---- pure: Account Balances projection ------------------------------------

def test_account_balances_rows_projection():
    report = BalanceReport(
        as_of="2026-12-31",
        rows=[
            AccountBalance(account_id=1, name="Checking", type="checking",
                           hidden=False, closed=False, cents=250000),
            AccountBalance(account_id=2, name="Visa", type="credit",
                           hidden=False, closed=False, cents=-12000),
        ],
        total=238000)

    rows = account_balances_rows(report)

    # Section is the account type; the trailing row is the net-worth total.
    assert rows == [
        ReportRow("checking", "Checking", 250000),
        ReportRow("credit", "Visa", -12000),
        ReportRow("Total", "Net Worth", 238000),
    ]


# ---- pure: By Payee / By Tag projection -----------------------------------

def test_payee_rows_projection_names_the_grouping():
    report = PayeeReport(
        start="2026-01-01", end="2026-12-31", key="payee", direction="out",
        account_ids=[1],
        rows=[PayeeRow(name="Whole Foods", count=3, cents=15000),
              PayeeRow(name="Trattoria", count=1, cents=3210)],
        total=18210)

    rows = payee_rows(report)

    assert rows == [
        ReportRow("Payee", "Whole Foods", 15000),
        ReportRow("Payee", "Trattoria", 3210),
        ReportRow("Payee", "Total", 18210),
    ]


def test_payee_rows_projection_uses_tag_section_for_by_tag():
    report = PayeeReport(
        start="2026-01-01", end="2026-12-31", key="tag", direction="net",
        account_ids=[1],
        rows=[PayeeRow(name="vacation", count=2, cents=-9000)],
        total=-9000)

    rows = payee_rows(report)

    assert [r.section for r in rows] == ["Tag", "Tag"]
    assert rows[0] == ReportRow("Tag", "vacation", -9000)


# ---- pure: Transactions listing projection --------------------------------

def _listing_row(date, payee, category, amount, tag="", memo=""):
    return {"id": 1, "date": date, "account_id": 1, "account": "Checking",
            "num": "", "payee": payee, "category": category, "memo": memo,
            "tag": tag, "amount": amount, "cleared": True, "reconciled": False,
            "is_split": False, "transfer_account": ""}


def test_transactions_spec_has_six_named_columns():
    # The Transactions report overrides the shared 3-column default so the Date
    # header sits over the date value and the payee is its own column -- with Tag
    # and Memo surfaced -- rather than a "Section" header over the date and the
    # payee jammed onto the category.
    assert TRANSACTIONS_SPEC.columns == TRANSACTIONS_COLUMNS
    assert TRANSACTIONS_COLUMNS == [
        "Date", "Payee", "Category / Account", "Tag", "Memo", "Amount"]


def test_listing_rows_projection_gives_a_cell_per_column():
    report = ListingReport(
        start="2026-01-01", end="2026-12-31", account_ids=[1],
        rows=[
            _listing_row("2026-01-05", "Employer", "Income:Salary", 500000,
                         tag="work", memo="Jan pay"),
            _listing_row("2026-01-08", "", "", -8532),
        ],
        count=2, total_cents=491468, truncated=False)

    rows = listing_rows(report)
    cols = TRANSACTIONS_SPEC.columns

    # Every path (table/CSV/HTML/PDF) renders through _row_cells; it must place the
    # date, payee (its OWN column, no longer glued to the category), category /
    # account, tag, memo and finally the money -- one cell per column.
    assert _row_cells(rows[0], cols) == [
        fmt_date("2026-01-05"), "Employer", "Income:Salary", "work", "Jan pay",
        fmt_cents(500000)]
    # A payee-less row still fills its own Payee column with the placeholder, and
    # empty tag/memo are blank cells rather than shifting the columns.
    assert _row_cells(rows[1], cols) == [
        fmt_date("2026-01-08"), "(no payee)", "", "", "", fmt_cents(-8532)]
    # The trailing total row summarizes the count and the signed cents total.
    assert _row_cells(rows[2], cols) == [
        "Total", "2 transactions", "", "", "", fmt_cents(491468)]


# ---- pure: Itemize by Category projection ---------------------------------

def test_itemize_rows_projection_is_category_and_amount_only():
    from mammon.reports import ItemizedReport, ItemizedRow
    report = ItemizedReport(
        start="2026-01-01", end="2026-12-31", account_ids=[1],
        rows=[
            ItemizedRow(category_id=1, name="Salary", path="Income:Salary",
                        type="income", net_cents=500000),
            ItemizedRow(category_id=2, name="Groceries", path="Groceries",
                        type="expense", net_cents=-30000),
            ItemizedRow(category_id=None, name="[Savings]", path="[Savings]",
                        type="transfer", net_cents=-20000, account_id=9),
        ],
        total_cents=450000)

    rows = itemize_rows(report)

    # The section is blank: this report renders only Category + Amount, so the
    # label (the category path or [Account] label) is the whole Category column.
    # The grand total is the report's own total_cents, never re-summed here.
    assert rows == [
        ReportRow("", "Income:Salary", 500000),
        ReportRow("", "Groceries", -30000),
        ReportRow("", "[Savings]", -20000),
        ReportRow("", "Total", 450000),
    ]


# ---- pure: fixed header preserved across reports --------------------------

def test_generalized_reports_keep_the_fixed_three_column_header():
    # Every projector produces the same (section, label, amount) shape, so the
    # one CSV writer and its fixed header serve all reports unchanged.
    rows = account_balances_rows(BalanceReport(
        as_of="2026-12-31",
        rows=[AccountBalance(1, "Checking", "checking", False, False, 100)],
        total=100))
    text = report_rows_to_csv(rows)
    assert text.splitlines()[0] == ",".join(COLUMNS)
    assert text.splitlines()[0] == "Section,Category / Account,Amount"


# ---- windows: offscreen construction for every spec -----------------------

def _win(conn, spec):
    return ReportWindow(conn, spec=spec)


@pytest.mark.parametrize("spec", [
    CASH_FLOW_SPEC,
    INCOME_EXPENSE_SPEC,
    ACCOUNT_BALANCES_SPEC,
    BY_PAYEE_SPEC,
    TRANSACTIONS_SPEC,
])
def test_report_window_constructs_for_each_spec(qapp, tmp_path, spec):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, spec)
    try:
        assert win.windowTitle() == spec.title
        headers = [win.table.horizontalHeaderItem(i).text()
                   for i in range(win.table.columnCount())]
        # Each flat report renders its own header set: the shared three-column
        # default, or a report's override (By Payee's ["Payee", "Net Amount"]).
        assert headers == spec.columns
        # Sample data feeds every report, so each projects at least one row and
        # the table mirrors the projection exactly.
        assert len(win._rows) >= 1
        assert win.table.rowCount() == len(win._rows)
    finally:
        win.close()
        conn.close()


def test_report_window_defaults_to_cash_flow(qapp, tmp_path):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    # The historical one-arg call still opens Cash Flow (regression guard).
    win = ReportWindow(conn)
    try:
        assert win.windowTitle() == "Cash Flow"
        assert win._rows[-1].label == "Net Cash Flow"
    finally:
        win.close()
        conn.close()


# ---- windows: export / print seams reuse the shared serializers -----------

def test_generalized_window_export_csv_matches_serializer(qapp, tmp_path):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, BY_PAYEE_SPEC)
    try:
        out = tmp_path / "by_payee.csv"
        win.export_csv_to(out)
        text = out.read_text(encoding="utf-8")
        # The export path reuses the shared serializer, driven with the window's
        # own columns -- By Payee's are ["Payee", "Net Amount"], not the shared three.
        assert text == report_rows_to_csv(win._rows, win.columns)
        assert text.startswith("Payee,Net Amount\n")
    finally:
        win.close()
        conn.close()


def test_generalized_window_export_html_matches_serializer(qapp, tmp_path):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, INCOME_EXPENSE_SPEC)
    try:
        out = tmp_path / "income_vs_expense.html"
        win.export_html_to(out)
        text = out.read_text(encoding="utf-8")
        assert text == report_rows_to_html(win._rows, win.windowTitle())
        assert "<h2>Income vs Expense</h2>" in text
    finally:
        win.close()
        conn.close()


def test_generalized_window_print_to_pdf_writes_pdf(qapp, tmp_path):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, ACCOUNT_BALANCES_SPEC)
    try:
        out = tmp_path / "account_balances.pdf"
        result = win.print_to_pdf(out)
        assert str(result) == str(out)
        assert out.read_bytes()[:5] == b"%PDF-"
    finally:
        win.close()
        conn.close()


def test_report_export_html_is_white_under_dark_theme(qapp, monkeypatch):
    """On-screen report windows follow the active theme, but a report EXPORTED
    or PRINTED must stay legible on paper: the HTML/PDF artifact is always a
    white page with black text, even when the app is in DARK mode. The print
    seam (``print_to_pdf``) renders this same HTML, so forcing white here forces
    it for the PDF too."""
    from mammon.ui import style

    monkeypatch.setattr(style, "theme", lambda: "dark")

    rows = [ReportRow("Income", "Salary", 500000),
            ReportRow("Net", "Net Income", 500000)]
    html = report_rows_to_html(rows, "Income vs Expense")

    assert "background: #ffffff" in html
    assert "color: #000000" in html
    # No dark-palette colour may leak into the print/export artifact.
    assert style.DARK["window"] not in html
    assert style.DARK["text"] not in html


def test_report_print_to_pdf_stays_white_under_dark_theme(qapp, tmp_path, monkeypatch):
    """The PDF print seam must not theme itself dark either: under dark mode it
    still renders the white HTML document to a real PDF."""
    from mammon.app import sample_data
    from mammon.ui import style

    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)
    monkeypatch.setattr(style, "theme", lambda: "dark")

    win = _win(conn, ACCOUNT_BALANCES_SPEC)
    try:
        out = tmp_path / "balances_dark.pdf"
        result = win.print_to_pdf(out)
        assert str(result) == str(out)
        assert out.read_bytes()[:5] == b"%PDF-"
        # The HTML the PDF is built from is the forced-white document.
        html = report_rows_to_html(win._rows, win.windowTitle())
        assert "background: #ffffff" in html and "color: #000000" in html
    finally:
        win.close()
        conn.close()


def test_default_filename_derives_from_title(qapp, tmp_path):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, INCOME_EXPENSE_SPEC)
    try:
        assert win._default_filename("csv") == "income_vs_expense.csv"
        assert win._default_filename("html") == "income_vs_expense.html"
    finally:
        win.close()
        conn.close()


def test_account_balances_spec_hides_account_checklist(qapp, tmp_path):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, ACCOUNT_BALANCES_SPEC)
    try:
        # The balances report takes no account filter, so its spec suppresses
        # the checklist rather than showing an ignored control.
        assert ACCOUNT_BALANCES_SPEC.show_accounts is False
        # The projection ends with the net-worth total row.
        assert win._rows[-1].label == "Net Worth"
        assert win._rows[-1].section == "Total"
    finally:
        win.close()
        conn.close()


# ---- windows: customization controls live behind the gear -----------------

def test_customization_controls_live_behind_the_gear(qapp, tmp_path):
    """The date/account customization controls are tucked behind a gear button
    (the CustomizeDialog popup the app's other report windows use), not laid out
    inline in the window.

    Offscreen-safe: the gear's click opens a modal via ``exec_()`` which would
    block forever under the offscreen platform, so this asserts the wiring
    structurally and drives the popup's Apply signal directly rather than
    clicking the gear.
    """
    from PyQt5.QtWidgets import QToolButton
    from mammon.app import sample_data
    from mammon.ui.report_filters import CustomizeDialog, ReportFilterBar
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, INCOME_EXPENSE_SPEC)
    try:
        # A gear tool-button on the window opens the customize popup.
        assert isinstance(win.gear_button, QToolButton)
        assert win.gear_button.text() == "⚙"  # ⚙
        assert isinstance(win.customize_dialog, CustomizeDialog)

        # The filter bar is the popup's live bar, reparented into it — so the
        # controls render behind the gear, not inline in the window.
        assert isinstance(win.filters, ReportFilterBar)
        assert win.customize_dialog.filters is win.filters
        assert win.filters.parentWidget() is win.customize_dialog

        # The customization controls are reachable through that bar.
        assert win.filters.start_edit is not None
        assert win.filters.account_list is not None  # I-vs-E shows the account list

        # Applying in the popup drives the window's refresh, exactly like the
        # other report windows (customize.applied -> refresh). Clicking the bar's
        # Apply button re-emits the dialog's `applied`; the gear is never exec_()'d.
        win.filters.set_range("2099-01-01", "2099-01-02")
        win.filters.apply_button.click()
        assert {r.label for r in win._rows} == {
            "Total Income", "Total Expense", "Net Income"}
    finally:
        win.close()
        conn.close()


# ---- windows: the unified Period dropdown ---------------------------------

PERIOD_LABELS = ["Last 7 days", "Last 30 days", "This Month", "Last Month",
                 "This quarter", "Last quarter", "Last 12 months",
                 "Year-to-Date", "This Year", "Last Year",
                 "Earliest to date", "Custom"]


def test_period_dropdown_options_and_default(qapp, tmp_path):
    """Every hosted report shows the SAME Period dropdown on top, with the
    unified option set and 'Year-to-Date' as the default so a freshly opened
    report answers 'how am I doing this year' without a trip to the dropdown."""
    from PyQt5.QtWidgets import QComboBox
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, CASH_FLOW_SPEC)
    try:
        assert isinstance(win.period_combo, QComboBox)
        labels = [win.period_combo.itemText(i)
                  for i in range(win.period_combo.count())]
        assert labels == PERIOD_LABELS
        assert win.period_combo.currentData() == "ytd"
        # The dropdown lives on the window itself (on top), not in the gear popup.
        assert win.period_combo.window() is win
    finally:
        win.close()
        conn.close()


def test_period_dropdown_is_identical_across_reports(qapp, tmp_path):
    """The Period dropdown is one shared control, not one bespoke picker per
    report: Cash Flow, Income vs Expense, By Payee, Transactions and Account
    Balances all offer exactly the same options."""
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)
    try:
        for spec in (CASH_FLOW_SPEC, INCOME_EXPENSE_SPEC, BY_PAYEE_SPEC,
                     TRANSACTIONS_SPEC, ACCOUNT_BALANCES_SPEC):
            win = _win(conn, spec)
            try:
                labels = [win.period_combo.itemText(i)
                          for i in range(win.period_combo.count())]
                assert labels == PERIOD_LABELS
            finally:
                win.close()
    finally:
        conn.close()


def test_period_preset_reranges_and_refreshes(qapp, tmp_path):
    """Picking a non-custom preset resolves to a concrete range on the filter
    bar and re-runs the report (the always-present totals survive)."""
    from datetime import date
    from mammon.reports.spending import preset_range
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, CASH_FLOW_SPEC)
    try:
        idx = win.period_combo.findData("last_30_days")
        win.period_combo.setCurrentIndex(idx)   # fires _on_period
        start, end = preset_range("last_30_days", date.today())
        assert (win.filters.start_iso(), win.filters.end_iso()) == (start, end)
        assert {"Total Income", "Total Expense", "Net Cash Flow"} <= {
            r.label for r in win._rows}
    finally:
        win.close()
        conn.close()


def test_period_custom_opens_the_gear(qapp, tmp_path):
    """Selecting 'Custom' opens the gear's customize popup so the user picks an
    explicit range. Driven through the overridable ``_open_customize`` seam so no
    modal ``exec_()`` blocks under the offscreen platform."""
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, CASH_FLOW_SPEC)
    try:
        opened = []
        win._open_customize = lambda: opened.append(True)
        win.period_combo.setCurrentIndex(win.period_combo.findData("custom"))
        assert opened == [True]
    finally:
        win.close()
        conn.close()


# ---- windows: saved-filter controls moved behind the gear -----------------

def test_saved_filter_controls_live_in_the_gear(qapp, tmp_path):
    """Save / Recall / Delete moved off the old inline row INTO the gear's
    customize popup, so range, accounts, categories AND named saved sets all
    share the one gear affordance rather than cluttering the window."""
    from PyQt5.QtWidgets import QComboBox
    from mammon.ui.report_filters import CustomizeDialog
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, CASH_FLOW_SPEC)
    try:
        assert isinstance(win.customize_dialog, CustomizeDialog)
        assert isinstance(win.saved_combo, QComboBox)
        # Each saved-filter control's top-level window is the customize popup,
        # NOT the report window -- i.e. they render behind the gear.
        for ctrl in (win.saved_combo, win.save_filters_button,
                     win.delete_filters_button):
            assert ctrl.window() is win.customize_dialog
        # By contrast the export button really is inline in the window.
        assert win.export_button.window() is win
    finally:
        win.close()
        conn.close()


# ---- windows: the Itemize by Category report ------------------------------

def test_itemize_report_drills_down_with_correct_headers(qapp, tmp_path):
    """The Itemize window restores the expandable drill-down: a QTreeWidget whose
    four columns are Category, Date, Payee / Memo, Amount -- Category first, so the
    Date header no longer sits mislabeled over the category column (the migration
    regression). It never falls back to the shared three-column header."""
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, ITEMIZE_SPEC)
    try:
        # The window defaults to Year-to-Date, but the sample data predates this
        # year; widen to the whole ledger so the drill-down has rows to expand
        # (this test is about the tree's columns and expansion, not the default).
        win.period_combo.setCurrentIndex(win.period_combo.findData("earliest"))
        assert win.windowTitle() == "Itemize by Category"
        assert ITEMIZE_SPEC.is_tree is True
        assert ITEMIZE_SPEC.columns == ["Category", "Date", "Payee / Memo", "Amount"]
        assert win.table is None  # the tree replaces the flat table
        headers = [win.tree.headerItem().text(i)
                   for i in range(win.tree.columnCount())]
        assert headers == ["Category", "Date", "Payee / Memo", "Amount"]
        assert headers != COLUMNS
        assert win.tree.columnCount() == 4
        # The top level is bold sections (INCOME / EXPENSES / ...), each expanded.
        assert win.tree.topLevelItemCount() >= 1
        top = win.tree.topLevelItem(0)
        assert top.text(0)  # a section label sits in the Category column
        assert top.isExpanded()
    finally:
        win.close()
        conn.close()


def test_itemize_report_offers_csv_html_pdf_export(qapp, tmp_path):
    """Itemize gets the same CSV / HTML / Print (PDF) export as every other report
    -- reusing the one serializer + print seam, now driven with its two-column set
    -- rather than the old Close-only dialog with no export at all."""
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = _win(conn, ITEMIZE_SPEC)
    try:
        # The three inline export controls exist on the window itself.
        for btn in (win.export_button, win.export_html_button, win.print_button):
            assert btn.window() is win

        # CSV: four-column header, and byte-identical to the tree serializer
        # driven with this window's projected rows (no second export path).
        csv_path = tmp_path / "itemize.csv"
        win.export_csv_to(csv_path)
        csv_text = csv_path.read_text(encoding="utf-8")
        assert csv_text.splitlines()[0] == "Category,Date,Payee / Memo,Amount"
        assert csv_text == tree_rows_to_csv(win._rows)

        # HTML: the same four columns, matching the tree serializer.
        html_path = tmp_path / "itemize.html"
        win.export_html_to(html_path)
        html_text = html_path.read_text(encoding="utf-8")
        assert html_text == tree_rows_to_html(win._rows, win.windowTitle())
        assert '<th align="left">Category</th>' in html_text
        assert "Section" not in html_text  # the shared third column is gone

        # PDF: the print seam renders that same four-column HTML to a real PDF.
        pdf_path = tmp_path / "itemize.pdf"
        assert str(win.print_to_pdf(pdf_path)) == str(pdf_path)
        assert pdf_path.read_bytes()[:5] == b"%PDF-"
    finally:
        win.close()
        conn.close()

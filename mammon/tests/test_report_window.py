"""Tests for the reusable report window (roadmap item 9, slice a).

Covers the pure CSV serializer and the report-row projection with no Qt or DB,
then the offscreen construction and CSV export of the real window. Synthetic
data only — no PII.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db
from mammon.reports import CashFlowReport, FlowRow, TransferRow
from mammon.ui.report_window import (
    COLUMNS,
    ReportRow,
    ReportWindow,
    cash_flow_rows,
    report_rows_to_csv,
    report_rows_to_html,
)


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _flow(path, type_, total):
    return FlowRow(category_id=None, path=path, top=path, type=type_,
                   by_bucket={"total": total}, total=total)


# ---- pure: CSV serialization ---------------------------------------------

def test_report_rows_to_csv_serializes_and_quotes():
    rows = [
        ReportRow("Income", "Salary", 500000),          # amount gets a comma
        ReportRow("Expense", "Groceries, Dining", -4567),  # label gets a comma
    ]
    text = report_rows_to_csv(rows)
    assert text == (
        "Section,Category / Account,Amount\n"
        'Income,Salary,"5,000.00"\n'
        'Expense,"Groceries, Dining",-45.67\n'
    )


# ---- pure: HTML serialization --------------------------------------------

def test_report_rows_to_html_header_rows_and_negatives():
    rows = [
        ReportRow("Income", "Salary", 500000),      # thousands separator
        ReportRow("Expense", "Groceries", -4567),   # negative keeps its sign
    ]
    html = report_rows_to_html(rows, "Cash Flow")

    # Title drives both the document <title> and the visible heading.
    assert "<title>Cash Flow</title>" in html
    assert "<h2>Cash Flow</h2>" in html

    # Every column header appears, in COLUMNS order.
    for col in COLUMNS:
        assert f">{col}</th>" in html
    assert html.index(">Section</th>") < html.index(">Amount</th>")

    # Amounts render through fmt_cents (no currency symbol), right-aligned.
    assert '<td align="right">5,000.00</td>' in html
    assert '<td align="right">-45.67</td>' in html
    # And the labels/sections land in their cells.
    assert '<td align="left">Income</td>' in html
    assert '<td align="left">Salary</td>' in html


def test_report_rows_to_html_escapes_markup_like_csv_quotes():
    # The HTML analogue of the CSV writer quoting a delimiter-bearing field:
    # special characters must render as text, never as markup.
    rows = [ReportRow("Expense", "Food & <Dining>", -100)]
    html = report_rows_to_html(rows, "R&D <Report>")

    assert "Food &amp; &lt;Dining&gt;" in html
    assert "<title>R&amp;D &lt;Report&gt;</title>" in html
    # No raw injected tag survives.
    assert "<Dining>" not in html
    assert "<Report>" not in html


def test_report_rows_to_csv_header_matches_columns():
    text = report_rows_to_csv([])
    assert text == ",".join(COLUMNS) + "\n"


def test_report_rows_to_csv_renders_zero_and_negative():
    text = report_rows_to_csv([
        ReportRow("Net", "Net Cash Flow", 0),
        ReportRow("Expense", "Rent", -123456),
    ])
    lines = text.splitlines()
    assert lines[1] == "Net,Net Cash Flow,0.00"
    assert lines[2] == 'Expense,Rent,"-1,234.56"'


# ---- pure: projection -----------------------------------------------------

def test_cash_flow_rows_projection_full():
    report = CashFlowReport(
        start="2026-01-01", end="2026-12-31", account_ids=[1],
        income=[_flow("Salary", "income", 500000)],
        expense=[_flow("Groceries", "expense", -30000)],
        transfers=[TransferRow(account_id=9, name="Savings", cents=-10000)],
        total_income=500000, total_expense=-30000,
        net_transfers=-10000, net=460000)

    rows = cash_flow_rows(report)

    assert rows == [
        ReportRow("Income", "Salary", 500000),
        ReportRow("Income", "Total Income", 500000),
        ReportRow("Expense", "Groceries", -30000),
        ReportRow("Expense", "Total Expense", -30000),
        ReportRow("Transfers", "Savings", -10000),
        ReportRow("Transfers", "Net Transfers", -10000),
        ReportRow("Net", "Net Cash Flow", 460000),
    ]


def test_cash_flow_rows_omits_empty_transfers_section():
    report = CashFlowReport(
        start="2026-01-01", end="2026-12-31", account_ids=[1],
        income=[_flow("Salary", "income", 500000)],
        expense=[_flow("Groceries", "expense", -30000)],
        transfers=[], total_income=500000, total_expense=-30000,
        net_transfers=0, net=470000)

    rows = cash_flow_rows(report)

    assert [r.section for r in rows] == [
        "Income", "Income", "Expense", "Expense", "Net"]
    assert not any(r.section == "Transfers" for r in rows)


# ---- window: offscreen construction + export ------------------------------

def test_report_window_constructs_offscreen(qapp, tmp_path):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "cf.db")
    sample_data(conn)

    win = ReportWindow(conn)
    try:
        headers = [win.table.horizontalHeaderItem(i).text()
                   for i in range(win.table.columnCount())]
        assert headers == COLUMNS
        # The report always emits at least the Total Income / Total Expense /
        # Net rows, so the projection is never empty and the table mirrors it.
        assert len(win._rows) >= 3
        assert win.table.rowCount() == len(win._rows)
        assert win._rows[-1].label == "Net Cash Flow"
        assert win._rows[-1].section == "Net"
    finally:
        win.close()
        conn.close()


def test_report_window_export_csv_matches_serializer(qapp, tmp_path):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "cf.db")
    sample_data(conn)

    win = ReportWindow(conn)
    try:
        out = tmp_path / "cash_flow.csv"
        win.export_csv_to(out)
        text = out.read_text(encoding="utf-8")
        assert text == report_rows_to_csv(win._rows)
        assert text.startswith("Section,Category / Account,Amount\n")
    finally:
        win.close()
        conn.close()


def test_report_window_refresh_reflects_filter_range(qapp, tmp_path):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "cf.db")
    sample_data(conn)

    win = ReportWindow(conn)
    try:
        # An empty date window (end before start) yields no category rows, only
        # the always-present totals — proving refresh() re-reads the filter bar.
        win.filters.set_range("2099-01-01", "2099-01-02")
        win.refresh()
        labels = {r.label for r in win._rows}
        assert labels == {"Total Income", "Total Expense", "Net Cash Flow"}
    finally:
        win.close()
        conn.close()


def test_report_window_export_html_matches_serializer(qapp, tmp_path):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "cf.db")
    sample_data(conn)

    win = ReportWindow(conn)
    try:
        out = tmp_path / "cash_flow.html"
        win.export_html_to(out)
        text = out.read_text(encoding="utf-8")
        # The seam writes exactly what the pure serializer produces for the
        # displayed rows and the window title.
        assert text == report_rows_to_html(win._rows, win.windowTitle())
        assert "<h2>Cash Flow</h2>" in text
    finally:
        win.close()
        conn.close()


def test_report_window_print_to_pdf_writes_pdf(qapp, tmp_path):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "cf.db")
    sample_data(conn)

    win = ReportWindow(conn)
    try:
        out = tmp_path / "cash_flow.pdf"
        # The seam renders through printing.render_html_to_pdf headless (no
        # QPrintDialog), so the print path is verified without a real printer.
        result = win.print_to_pdf(out)
        assert str(result) == str(out)
        data = out.read_bytes()
        assert data[:5] == b"%PDF-"
    finally:
        win.close()
        conn.close()

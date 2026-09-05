"""Regression: the By-Payee report's columns (user-reported).

The By-Payee report used to render the shared three-column header --
``Section`` (always the dead literal "Payee") and ``Category / Account`` (a
meaningless header for a by-payee grouping, since a payee has no single category
or account) plus ``Amount``. It now renders a two-column set whose FIRST column
is a proper ``Payee`` column holding the payee name, followed by ``Net Amount``.

A second user report renamed that trailing column. The window ran the pure
report at its ``"out"`` default, so a payee who pays YOU -- an employer, a
pension, a tenant -- was summed from the wrong side; the window now asks for
``"net"``, and the header says so rather than leaving "Amount" to mean whichever
side the caller happened to pick.

The fix is a spec-only change: ``BY_PAYEE_SPEC`` declares
``columns=["Payee", "Net Amount"]`` and the existing generic ``_row_cells`` maps a
projected row onto it -- a two-column report drops the ``section`` and shows the
``label`` (the payee name) alone. The shared ``payee_rows`` projector is
untouched, so the By-Tag report that also uses it keeps its ``Tag`` section
column. Synthetic data only -- no PII.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db
from mammon.reports import PayeeReport, PayeeRow
from mammon.ui.report_window import (
    BY_PAYEE_SPEC,
    BY_TAG_SPEC,
    ReportWindow,
    payee_rows,
    report_rows_to_csv,
    report_rows_to_html,
)


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---- spec: the By-Payee column set ----------------------------------------

def test_by_payee_spec_is_payee_first_two_columns():
    # First column is a proper Payee column; the always-"Payee" Section column
    # and the meaningless Category / account column are both gone.
    assert BY_PAYEE_SPEC.columns == ["Payee", "Net Amount"]
    assert BY_PAYEE_SPEC.columns[0] == "Payee"
    assert "Section" not in BY_PAYEE_SPEC.columns
    assert "Category / Account" not in BY_PAYEE_SPEC.columns


def test_by_tag_report_still_uses_the_section_column():
    # The shared projector was left untouched, so By Tag keeps the default
    # three-column header (Section = "Tag") -- other reports are not disturbed.
    assert BY_TAG_SPEC.columns == ["Section", "Category / Account", "Amount"]


# ---- pure: the payee name lands in the Payee column -----------------------

def _report():
    return PayeeReport(
        start="2026-01-01", end="2026-12-31", key="payee", direction="out",
        account_ids=[1],
        rows=[PayeeRow(name="Acme Grocers", count=3, cents=15000),
              PayeeRow(name="Blue Diner", count=1, cents=3210)],
        total=18210)


def test_by_payee_csv_header_is_payee_first_with_payee_value():
    rows = payee_rows(_report())
    text = report_rows_to_csv(rows, BY_PAYEE_SPEC.columns)
    lines = text.splitlines()

    # Header: Payee first, Amount last -- no Section, no Category / account.
    assert lines[0] == "Payee,Net Amount"
    assert "Section" not in lines[0]
    assert "Category / Account" not in lines[0]

    # The payee name is the first (Payee) column; the amount is the last.
    assert lines[1] == "Acme Grocers,150.00"
    assert lines[2] == "Blue Diner,32.10"
    assert lines[-1] == "Total,182.10"


def test_by_payee_html_header_is_payee_first():
    rows = payee_rows(_report())
    html = report_rows_to_html(rows, "By Payee", BY_PAYEE_SPEC.columns)

    assert '<th align="left">Payee</th>' in html
    assert '<th align="right">Net Amount</th>' in html
    assert ">Section<" not in html
    assert "Category / Account" not in html
    # The payee name renders as a cell, not folded behind a Section column.
    assert ">Acme Grocers<" in html


# ---- window: the offscreen table renders the new columns ------------------

def test_by_payee_window_table_is_payee_first(qapp, tmp_path):
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = ReportWindow(conn, spec=BY_PAYEE_SPEC)
    try:
        assert win.windowTitle() == "By Payee"

        headers = [win.table.horizontalHeaderItem(i).text()
                   for i in range(win.table.columnCount())]
        assert headers == ["Payee", "Net Amount"]
        assert headers[0] == "Payee"
        assert "Section" not in headers
        assert "Category / Account" not in headers

        # Sample data feeds the report, so it projects rows; the first table
        # column holds the payee name (the projected row's label), not the
        # dead "Payee" section string.
        assert win._rows
        assert win.table.columnCount() == 2
        first = win._rows[0]
        assert first.section == "Payee"  # the projector still sets it...
        assert win.table.item(0, 0).text() == first.label  # ...but it is dropped
        # The Amount column is preserved as the trailing column.
        from mammon.ui.models import fmt_cents
        assert win.table.item(0, 1).text() == fmt_cents(first.amount)
    finally:
        win.close()
        conn.close()


def test_by_payee_window_keeps_period_gear_and_export(qapp, tmp_path):
    """Only the columns changed: the Period dropdown, the gear, and the
    CSV / HTML / PDF export controls remain intact on the By-Payee window."""
    from PyQt5.QtWidgets import QComboBox, QToolButton
    from mammon.app import sample_data
    conn = db.init_db(tmp_path / "gen.db")
    sample_data(conn)

    win = ReportWindow(conn, spec=BY_PAYEE_SPEC)
    try:
        assert isinstance(win.period_combo, QComboBox)
        assert isinstance(win.gear_button, QToolButton)
        for btn in (win.export_button, win.export_html_button, win.print_button):
            assert btn.window() is win
    finally:
        win.close()
        conn.close()


# ---- regression: the window sums NET, so a payee who pays you shows -------

def test_by_payee_window_runs_net_so_an_employer_is_not_summed_backwards(
        qapp, tmp_path):
    """User-reported: an employer showed $12,000 year-to-date against $60,000 of
    actual deposits. Two causes, both fixed -- the pure report billed the paycheck
    by its legs (see test_report_payees) and the window asked for ``direction="out"``,
    which on an employer sums the withholding. The window now asks for ``"net"``.

    Synthetic data: one paycheck in, one purchase out."""
    from mammon import ledger
    from mammon.ui.report_filters import ReportFilterBar

    conn = db.init_db(tmp_path / "net.db")
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    plan = ledger.create_account(conn, "Plan 401K", "savings", opening_balance=0)
    salary = ledger.resolve_category(conn, "Salary")
    tax = ledger.resolve_category(conn, "Fed")
    pay = ledger.add_transaction(conn, chk, "2026-01-15", 2800_00,
                                 payee="Acme Corp.")
    ledger.set_splits(conn, pay, [
        {"category_id": salary, "amount": 4000_00},
        {"category_id": tax, "amount": -900_00},
        {"transfer_account_id": plan, "amount": -300_00},
    ])
    ledger.add_transaction(conn, chk, "2026-01-20", -85_32, payee="Blue Diner")

    bar = ReportFilterBar(conn, "2026-01-01", "2026-01-31")
    try:
        report = BY_PAYEE_SPEC.run(conn, bar)
        assert report.direction == "net"
        got = {r.name: r.cents for r in report.rows}
        # The employer's DEPOSIT, not their withholding; spending stays negative.
        assert got == {"Acme Corp.": 2800_00, "Blue Diner": -85_32}
    finally:
        bar.deleteLater()
        conn.close()

"""The Capital Gains and Taxes report, end to end through the shared window.

The pure aggregation is covered by ``test_capital_gains.py``; what is tested here
is the projection the user actually reads: that every open lot reaches the table,
that each row says whether it is long- or short-term, that a short-term row names
the date it turns long, and that the tax consequence of selling before that date
is spelled out rather than left to be inferred.

The ledger is synthetic -- two made-up tickers in one made-up brokerage, one lot
bought long enough before the as-of date to be long-term and one bought inside
the twelve-month window.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, investments, ledger, portfolio
from mammon.ui.report_window import (CAPITAL_GAINS_COLUMNS, CAPITAL_GAINS_SPEC,
                                     IF_SOLD_NOW_MAX_CHARS, ReportWindow,
                                     capital_gains_footnote, capital_gains_rows)
from mammon.tests import fresh_db

# A fixed "today" so the holding-period arithmetic is not a moving target.
AS_OF = "2026-06-30"
OPEN_DATE = "2023-01-01"
LONG_BUY = "2024-02-01"       # far more than a year before AS_OF
SHORT_BUY = "2026-03-01"      # inside the window: turns long 2027-03-02
SHORT_LONG_ON = "2027-03-02"


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "capital_gains_window.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    """One brokerage holding two lots: ZZLT (long-term, at a gain) and ZZST
    (short-term, also at a gain -- the case whose tax bill the report exists to
    warn about)."""
    acct = ledger.create_account(conn, "ZZ Test Brokerage", "investment",
                                 opening_balance=100_000_00,
                                 opening_date=OPEN_DATE)
    portfolio.set_security(conn, "ZZLT", name="ZZ Long Holdings Fund",
                           sec_type="fund")
    portfolio.set_security(conn, "ZZST", name="ZZ Short Holdings Fund",
                           sec_type="fund")

    investments.record_investment(conn, acct, LONG_BUY, "Buy", symbol="ZZLT",
                                  quantity="10", price="100.00",
                                  amount=-1_000_00)
    investments.record_investment(conn, acct, SHORT_BUY, "Buy", symbol="ZZST",
                                  quantity="20", price="50.00",
                                  amount=-1_000_00)
    investments.rebuild_holdings(conn, acct)

    # Both positions are worth double what they cost as of AS_OF.
    investments.record_price(conn, "ZZLT", LONG_BUY, "100.00")
    investments.record_price(conn, "ZZLT", AS_OF, "200.00")
    investments.record_price(conn, "ZZST", SHORT_BUY, "50.00")
    investments.record_price(conn, "ZZST", AS_OF, "100.00")
    conn.commit()
    return acct


def _rows(conn):
    from mammon import reports
    report = reports.capital_gains(conn, AS_OF)
    return report, capital_gains_rows(report)


def _row_for(rows, symbol):
    for r in rows:
        if r.label == symbol:
            return r
    raise AssertionError("no row for %s in %s" % (symbol,
                                                  [r.label for r in rows]))


# -- the projection ---------------------------------------------------------

def test_every_open_lot_reaches_the_table(seeded, conn):
    _report, rows = _rows(conn)
    labels = [r.label for r in rows]
    assert "ZZLT" in labels
    assert "ZZST" in labels


def test_cells_match_the_declared_columns(seeded, conn):
    # A row whose cells exactly fill the header set is rendered verbatim by
    # _row_cells; a short row would silently gain an amount column instead.
    _report, rows = _rows(conn)
    assert len(CAPITAL_GAINS_COLUMNS) == 10
    for r in rows:
        assert len(r.cells) == len(CAPITAL_GAINS_COLUMNS)


def test_each_row_states_its_term(seeded, conn):
    _report, rows = _rows(conn)
    term = CAPITAL_GAINS_COLUMNS.index("Term")
    assert _row_for(rows, "ZZLT").cells[term] == "Long-term"
    assert _row_for(rows, "ZZST").cells[term] == "Short-term"


def test_short_lot_names_the_date_it_becomes_long(seeded, conn):
    from mammon.ui.models import fmt_date
    _report, rows = _rows(conn)
    col = CAPITAL_GAINS_COLUMNS.index("Becomes Long-Term")
    becomes = _row_for(rows, "ZZST").cells[col]
    assert fmt_date(SHORT_LONG_ON) in becomes
    assert "day" in becomes                     # ...and how far off it is
    # A lot that is already long-term has no such deadline to report.
    assert _row_for(rows, "ZZLT").cells[col] == ""


def test_short_lot_is_annotated_with_the_cost_of_selling_early(seeded, conn):
    # The CELL is a verdict, the SENTENCE is the tooltip. User, 2026-09-19: "the
    # tax consequences field is way to long and unreadible without expanding the
    # report to full screen."
    report, rows = _rows(conn)
    col = CAPITAL_GAINS_COLUMNS.index("If Sold Now")
    row = _row_for(rows, "ZZST")
    note = row.cells[col]
    assert note, "a short-term lot must say what selling it now costs"
    assert len(note) <= IF_SOLD_NOW_MAX_CHARS, note
    assert "tax" in note.lower()
    # The full explanation is still there -- on hover, not in the column.
    tip = (row.tooltips or {})[col]
    assert "ordinary" in tip.lower()
    assert len(tip) > len(note) * 3


def test_every_if_sold_now_cell_is_short(seeded, conn):
    # Every row, lots and totals alike: nothing in this column may be wide enough
    # to push the money columns off a default-width window.
    _report, rows = _rows(conn)
    col = CAPITAL_GAINS_COLUMNS.index("If Sold Now")
    for r in rows:
        assert len(r.cells[col]) <= IF_SOLD_NOW_MAX_CHARS, (r.label,
                                                            r.cells[col])


def test_totals_split_long_from_short_and_price_the_difference(seeded, conn):
    report, rows = _rows(conn)
    labels = [r.label for r in rows]
    for want in ("Long-term gain", "Long-term loss", "Short-term gain",
                 "Short-term loss", "Total unrealized"):
        assert want in labels
    extra = [r for r in rows
             if r.label.startswith("Extra tax if the short-term book")]
    assert len(extra) == 1
    assert extra[0].amount == report.total_extra_tax_if_sold_now
    # The rates it rests on are on hover and in the footnote, not in the column.
    col = CAPITAL_GAINS_COLUMNS.index("If Sold Now")
    assert "%" in (extra[0].tooltips or {})[col]
    assert "%" in capital_gains_footnote(report)


def test_sorting_by_term_puts_short_lots_first(seeded, conn):
    from mammon import reports
    report = reports.capital_gains(conn, AS_OF)
    rows = capital_gains_rows(report, sort_key="term")
    lot_labels = [r.label for r in rows if r.label in ("ZZLT", "ZZST")]
    assert lot_labels == ["ZZST", "ZZLT"]
    rows = capital_gains_rows(report, sort_key="term", sort_desc=True)
    lot_labels = [r.label for r in rows if r.label in ("ZZLT", "ZZST")]
    assert lot_labels == ["ZZLT", "ZZST"]


# -- the window -------------------------------------------------------------

def test_window_opens_on_the_spec_and_renders_the_lots(qapp, seeded, conn):
    win = ReportWindow(conn, spec=CAPITAL_GAINS_SPEC)
    try:
        assert win.windowTitle() == "Capital Gains and Taxes"
        headers = [win.table.horizontalHeaderItem(i).text()
                   for i in range(win.table.columnCount())]
        assert headers == CAPITAL_GAINS_COLUMNS

        win.filters.set_range("2000-01-01", AS_OF)
        win.refresh()
        assert win.table.rowCount() == len(win._rows)

        term = CAPITAL_GAINS_COLUMNS.index("Term")
        seen = {}
        for row in range(win.table.rowCount()):
            label = win.table.item(row, 1)
            if label is None:
                continue
            for sym in ("ZZLT", "ZZST"):
                if label.text().startswith(sym + " ("):
                    seen[sym] = win.table.item(row, term).text()
        assert seen == {"ZZLT": "Long-term", "ZZST": "Short-term"}
    finally:
        win.close()


def test_window_export_carries_the_tax_annotation(qapp, seeded, conn, tmp_path):
    win = ReportWindow(conn, spec=CAPITAL_GAINS_SPEC)
    try:
        win.filters.set_range("2000-01-01", AS_OF)
        win.refresh()
        out = tmp_path / "capital_gains.csv"
        win.export_csv_to(str(out))
    finally:
        win.close()
    text = out.read_text(encoding="utf-8")
    assert "Becomes Long-Term" in text
    assert "ZZST" in text
    # Export carries ``cells``, so the short verdict has to be true on its own
    # rather than a teaser for a tooltip a CSV cannot show.
    assert "tax" in text.lower()


def test_window_hangs_the_long_explanation_off_the_short_cell(qapp, seeded,
                                                              conn):
    """The rendered table, not just the projection: a short cell with the
    sentence on hover. This is the defect the user reported."""
    win = ReportWindow(conn, spec=CAPITAL_GAINS_SPEC)
    try:
        win.filters.set_range("2000-01-01", AS_OF)
        win.refresh()
        col = CAPITAL_GAINS_COLUMNS.index("If Sold Now")
        tips = 0
        for row in range(win.table.rowCount()):
            item = win.table.item(row, col)
            assert item is not None
            assert len(item.text()) <= IF_SOLD_NOW_MAX_CHARS, item.text()
            if item.toolTip():
                tips += 1
                assert len(item.toolTip()) > IF_SOLD_NOW_MAX_CHARS
        assert tips, "the full explanation must survive somewhere"
        # ...and the assumptions sit under the table rather than in a column.
        assert win.footnote_label is not None
        assert "%" in win.footnote_label.text()
    finally:
        win.close()


def test_tax_deferred_account_is_absent_and_named_in_the_footnote(qapp, conn):
    """A 401(k)/IRA/Roth account produces no row and no total here; the footnote
    under the table is the only trace. User, 2026-09-19: "401K, IRA and Roth IRA
    do not pay capital gains" and "if they're not taxed, don't put them in the
    report. That is just a lot of clutter." """
    acct = ledger.create_account(conn, "ZZ Retirement 401k", "investment",
                                 opening_balance=100_000_00,
                                 opening_date=OPEN_DATE)
    ledger.update_account(conn, acct, tax_treatment="deferred")
    portfolio.set_security(conn, "ZZIRA", name="ZZ Retirement Fund",
                           sec_type="fund")
    investments.record_investment(conn, acct, SHORT_BUY, "Buy", symbol="ZZIRA",
                                  quantity="10", price="100.00",
                                  amount=-1_000_00)
    investments.rebuild_holdings(conn, acct)
    investments.record_price(conn, "ZZIRA", AS_OF, "200.00")
    conn.commit()

    report, rows = _rows(conn)
    ticker = CAPITAL_GAINS_COLUMNS.index("Ticker")
    assert all(r.cells[ticker] != "ZZIRA" for r in rows)
    assert all("ZZ Retirement 401k" not in r.cells[0] for r in rows)
    # None of it reached any bucket -- not even the term-free ones.
    assert report.total_short_term_gain == 0
    assert report.total_extra_tax_if_sold_now == 0
    assert report.total_unrealized == 0
    # ...and the footnote, not a row, says which account was left out.
    foot = capital_gains_footnote(report)
    assert "ZZ Retirement 401k" in foot
    assert "not subject to capital gains" in foot.lower()


# --- Reaching the report -----------------------------------------------------
# The report shipped wired only to the Investment Dashboard's corner launcher,
# so a user who never visited that page had no way to it. These pin BOTH ways in.


@pytest.fixture
def main_win(qapp, tmp_path):
    """The real main window, not a stub: the regression lived in the wiring
    between a menu action and the report window, which a stub would have passed."""
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow
    c = fresh_db(tmp_path / "capital_gains_menu.db")
    sample_data(c)
    w = MainWindow(c)
    try:
        yield w
    finally:
        w.close()
        c.close()


def _reports_menu(window):
    for menu_act in window.menuBar().actions():
        menu = menu_act.menu()
        if menu is not None and "Reports" in menu_act.text():
            return menu
    return None


def test_reports_menu_offers_capital_gains_and_taxes(main_win):
    reports = _reports_menu(main_win)
    assert reports is not None, "no Reports menu on the menu bar"
    hits = [a for a in reports.actions()
            if a.text().startswith("Capital Gains and Taxes")]
    assert hits, ("the Reports menu does not offer Capital Gains and Taxes: "
                  f"{[a.text() for a in reports.actions()]}")
    assert len(hits) == 1, f"duplicate entries: {[a.text() for a in hits]}"
    assert hits[0].isEnabled()


def test_the_menu_action_opens_the_capital_gains_report_window(main_win):
    """Triggering it opens the shared ReportWindow on CAPITAL_GAINS_SPEC -- the
    same spec object the dashboard's top-left corner launcher opens, so the two
    entry points cannot drift into two different reports."""
    act = next(a for a in _reports_menu(main_win).actions()
               if a.text().startswith("Capital Gains and Taxes"))
    before = list(getattr(main_win, "_report_windows", []))
    act.trigger()          # modeless: show(), never exec_()
    opened = [w for w in getattr(main_win, "_report_windows", [])
              if all(w is not old for old in before)]
    assert len(opened) == 1, f"expected one new report window, got {len(opened)}"
    win = opened[0]
    try:
        assert isinstance(win, ReportWindow)
        assert win.spec is CAPITAL_GAINS_SPEC
        assert win.windowTitle() == CAPITAL_GAINS_SPEC.title
    finally:
        win.close()


def test_both_entry_points_open_the_same_report_spec():
    """The dashboard's top-left corner and the Reports menu must name the SAME
    spec object, so the two ways in cannot drift into two different reports.
    Checked against the page class directly -- no Qt page needs building, and no
    modal loop is entered, to learn which spec a corner aims at."""
    from mammon.ui.investment_dashboard import InvestmentDashboardPage

    class _Recorder:
        """Stands in for the page: records what _open_report was handed."""
        opened = None

        def _open_report(self, spec):
            self.opened = spec
            return spec

    rec = _Recorder()
    InvestmentDashboardPage.open_capital_gains(rec)
    assert rec.opened is CAPITAL_GAINS_SPEC

"""The share reconcile workspace (SRD 5.11b): mammon/ui/share_reconcile_dialog.py.

The life cycle these tests hold down, end to end and headless:

    import a holdings snapshot  ->  each fund's stated ending count lands in the
    dialog  ->  the per-security tab lists that security's quantity-changing
    rows and nothing else  ->  clearing rows down to the stated count finishes
    the period  ->  a residual that cannot be cleared away is closed only by an
    adjustment the user explicitly accepts, after which the account balances.

Two invariants are asserted directly because they are the ones a later change
would quietly break:

  * a snapshot import creates NO transactions (it is a statement of fact);
  * the adjustment's consequence -- deleting it later does not undo the
    reconciliation -- is on the FACE of the window, not only in the prompt the
    user clicks through once.

Nothing is ``exec_()``-ed: the dialog's three modal seams (``confirm_adjustment``,
``warn``, ``choose_snapshot_file``) are replaced, because a modal built and
executed for real under the offscreen platform blocks forever (CLAUDE.md,
headless-modal hazard).

All data is synthetic: ANON fund names, invented share counts, no PII.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal
from pathlib import Path

import pytest

from PyQt5.QtCore import QDate
from PyQt5.QtWidgets import QApplication

from mammon import db, investments, ledger
from mammon.ui.share_reconcile_dialog import ShareReconcileDialog
from mammon.tests import fresh_db

FIXTURE = Path(__file__).parent / "fixtures" / "anon_401k_holdings.csv"

BALANCED = "ANON BALANCED FUND"
STABLE = "ANON STABLE VALUE FUND"
LARGE = "ANONX ANON LARGE CAP INDEX"
STATEMENT = "2026-03-31"


@pytest.fixture(scope="module", autouse=True)
def qapp():
    """One QApplication for the module: constructing a QWidget without one takes
    the interpreter down with no traceback."""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "sharerec.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "ANON 401(k)", "investment",
                                 opening_balance=0)


def _buy(conn, a, date, sym, qty, price="10.00"):
    amount = -int(Decimal(qty) * Decimal(price) * 100)
    return investments.record_investment(conn, a, date, "Buy", symbol=sym,
                                         quantity=qty, price=price,
                                         amount=amount)


@pytest.fixture
def world(conn, acct):
    """Three funds, two purchases of one of them, and a dividend that moves cash
    but no shares."""
    _buy(conn, acct, "2026-01-15", BALANCED, "60", "20.00")
    _buy(conn, acct, "2026-02-15", BALANCED, "40", "21.00")
    _buy(conn, acct, "2026-02-15", STABLE, "1000", "1.00")
    _buy(conn, acct, "2026-02-15", LARGE, "50.5", "30.00")
    investments.record_investment(conn, acct, "2026-03-01", "Div",
                                  symbol=BALANCED, amount=1234)
    return acct


def _dialog(conn, account_id, date=STATEMENT):
    dlg = ShareReconcileDialog(conn, account_id)
    dlg.date.setDate(QDate.fromString(date, "yyyy-MM-dd"))
    return dlg


def _seams(dlg, *, confirm=False):
    """Replace every modal with a recorder. Returns the log."""
    log = {"warn": [], "confirm": [], "chosen": 0}
    dlg.warn = lambda title, text: log["warn"].append((title, text))
    def _confirm(symbol, qty, warning):
        log["confirm"].append((symbol, qty, warning))
        return confirm
    dlg.confirm_adjustment = _confirm
    return log


def _txn_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM investment_transactions").fetchone()[0]


def _clear_all(dlg, symbol):
    for i, _row in enumerate(list(dlg.rows_for(symbol))):
        dlg.toggle_row(symbol, i)


# --- the tabs ---------------------------------------------------------------

def test_one_tab_per_security_holding_only_quantity_changing_rows(conn, world):
    dlg = _dialog(conn, world)
    assert dlg.symbols == sorted([BALANCED, STABLE, LARGE])
    assert [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())] == dlg.symbols

    rows = dlg.rows_for(BALANCED)
    assert [r["date"] for r in rows] == ["2026-01-15", "2026-02-15"]
    # The dividend moved cash, not shares: it is not a statement line here.
    assert all(r["action"].lower() != "div" for r in rows)
    assert [r["symbol"] for r in rows] == [BALANCED, BALANCED]
    assert len(dlg.rows_for(STABLE)) == 1


def test_the_tab_table_shows_what_the_row_says(conn, world):
    dlg = _dialog(conn, world)
    dlg.select_symbol(BALANCED)
    table = dlg._tables[BALANCED]
    assert table.rowCount() == 2
    assert table.item(0, dlg.DATE).text() == "2026-01-15"
    assert table.item(0, dlg.QTY).text() == "60"
    assert table.item(0, dlg.CLR).text() == ""
    dlg.toggle_row(BALANCED, 0)
    assert table.item(0, dlg.CLR).text() == "c"


def test_clearing_a_row_persists_immediately_and_toggles_back(conn, world):
    dlg = _dialog(conn, world)
    tid = dlg.rows_for(BALANCED)[0]["id"]
    assert dlg.toggle_row(BALANCED, 0) is True
    assert conn.execute("SELECT cleared FROM investment_transactions WHERE id=?",
                        (tid,)).fetchone()[0] == 1
    assert dlg.toggle_row(BALANCED, 0) is True
    assert conn.execute("SELECT cleared FROM investment_transactions WHERE id=?",
                        (tid,)).fetchone()[0] == 0


# --- the running difference -------------------------------------------------

def test_the_difference_falls_to_zero_as_rows_are_cleared(conn, world):
    dlg = _dialog(conn, world)
    dlg.set_ending(BALANCED, "100")
    assert dlg.difference(BALANCED) == Decimal("100")
    dlg.toggle_row(BALANCED, 0)
    assert dlg.difference(BALANCED) == Decimal("40")
    dlg.toggle_row(BALANCED, 1)
    assert dlg.difference(BALANCED) == Decimal(0)
    # And the security's own row in the top table says the same thing.
    i = dlg.symbols.index(BALANCED)
    assert dlg.summary_table.item(i, dlg.DIFF).text() == "0"
    assert dlg.summary_table.item(i, dlg.CLEARED).text() == "100"


def test_each_security_keeps_its_own_difference(conn, world):
    dlg = _dialog(conn, world)
    dlg.set_ending(BALANCED, "100")
    dlg.set_ending(STABLE, "1000")
    _clear_all(dlg, BALANCED)
    assert dlg.difference(BALANCED) == Decimal(0)
    assert dlg.difference(STABLE) == Decimal("1000")


def test_a_typed_ending_count_that_is_not_a_number_is_refused(conn, world):
    dlg = _dialog(conn, world)
    log = _seams(dlg)
    i = dlg.symbols.index(BALANCED)
    dlg.set_ending(BALANCED, "100")
    dlg.summary_table.item(i, dlg.END).setText("one hundred")
    assert log["warn"], "the user was told"
    assert dlg.summary_table.item(i, dlg.END).text() == "100"
    assert dlg._state[BALANCED]["ending"] == "100"


# --- finishing --------------------------------------------------------------

def test_clearing_to_the_stated_count_finishes_the_reconciliation(conn, world):
    dlg = _dialog(conn, world)
    log = _seams(dlg)
    dlg.select_symbol(BALANCED)
    dlg.set_ending(BALANCED, "100")
    _clear_all(dlg, BALANCED)
    assert dlg.finish_current() is True
    assert log["confirm"] == [], "nothing to adjust, so nothing was asked"
    assert dlg.finished_ok and dlg.finished_symbols == [BALANCED]

    row = conn.execute(
        "SELECT statement_date, ending_qty FROM share_reconciliations "
        "WHERE account_id=? AND symbol=?", (world, BALANCED)).fetchone()
    assert row["statement_date"] == STATEMENT
    assert Decimal(row["ending_qty"]) == Decimal("100")
    assert all(r["reconciled"] for r in dlg.rows_for(BALANCED))
    # The draft is spent, and the other funds are untouched.
    assert investments.get_share_reconcile_draft(conn, world, BALANCED) is None
    assert conn.execute(
        "SELECT COUNT(*) FROM share_reconciliations WHERE symbol=?",
        (STABLE,)).fetchone()[0] == 0


def test_finishing_out_of_balance_without_an_adjustment_is_refused(conn, world):
    dlg = _dialog(conn, world)
    log = _seams(dlg, confirm=False)
    dlg.select_symbol(BALANCED)
    dlg.set_ending(BALANCED, "105")
    _clear_all(dlg, BALANCED)
    assert dlg.difference(BALANCED) == Decimal("5")
    assert dlg.finish_current() is False
    assert len(log["confirm"]) == 1
    assert log["confirm"][0][0] == BALANCED
    assert log["confirm"][0][1] == "5"
    assert conn.execute(
        "SELECT COUNT(*) FROM share_reconciliations WHERE symbol=?",
        (BALANCED,)).fetchone()[0] == 0


def test_an_accepted_adjustment_balances_the_account(conn, world):
    dlg = _dialog(conn, world)
    log = _seams(dlg, confirm=True)
    dlg.select_symbol(BALANCED)
    dlg.set_ending(BALANCED, "105")
    _clear_all(dlg, BALANCED)
    assert dlg.finish_current() is True
    # The user was shown the consequence before the row was written.
    assert investments.SHARE_ADJUSTMENT_WARNING in log["confirm"][0][2]

    holding = investments.compute_holdings(conn, world, as_of=STATEMENT)
    assert holding[BALANCED].qty == Decimal("105")
    adjustments = investments.list_share_adjustments(conn, world, BALANCED)
    assert len(adjustments) == 1
    assert Decimal(adjustments[0]["quantity"]) == Decimal("5")
    assert dlg.difference(BALANCED) == Decimal(0)
    # ... and the window says what deleting it would cost.
    assert investments.SHARE_ADJUSTMENT_WARNING in dlg.status_label.text()


def test_the_adjustment_warning_is_on_the_face_of_the_dialog(conn, world):
    dlg = _dialog(conn, world)
    assert dlg.warning_label.text() == investments.SHARE_ADJUSTMENT_WARNING
    assert not dlg.warning_label.isHidden()
    text = dlg.warning_label.text().lower()
    assert "delete" in text and "by hand" in text


# --- the import path --------------------------------------------------------

def test_importing_a_snapshot_states_the_ending_counts(conn, world):
    dlg = _dialog(conn, world, date="2026-01-01")
    before = _txn_count(conn)
    summary = dlg.import_snapshot(str(FIXTURE))

    assert _txn_count(conn) == before, "a snapshot is not a transaction file"
    assert summary["matched"] == 3 and summary["unmatched"] == 1
    assert dlg.statement_date() == STATEMENT      # the file's date was adopted
    assert dlg._state[BALANCED]["ending"] == "123.456"
    assert dlg._state[LARGE]["ending"] == "50.5"
    i = dlg.symbols.index(BALANCED)
    assert dlg.summary_table.item(i, dlg.END).text() == "123.456"
    # The fund nobody owns is reported, not guessed at.
    assert "ANON FUND NOBODY OWNS" in dlg.status_label.text()
    assert "ANON FUND NOBODY OWNS" not in dlg.symbols


def test_a_file_the_dialog_cannot_read_is_reported_not_raised(conn, world,
                                                              tmp_path):
    dlg = _dialog(conn, world)
    log = _seams(dlg)
    bad = tmp_path / "anon_nodate.csv"
    bad.write_text("Fund,Shares,Price\nANON BALANCED FUND,1,1.00\n",
                   encoding="utf-8")
    assert dlg.import_snapshot(str(bad)) is None
    assert log["warn"], "the failure was shown, not raised at the user"


def test_no_file_chosen_changes_nothing(conn, world):
    dlg = _dialog(conn, world)
    dlg.choose_snapshot_file = lambda: ""
    before = _txn_count(conn)
    assert dlg.import_snapshot() is None
    assert _txn_count(conn) == before


# --- the whole life cycle ---------------------------------------------------

def test_import_then_reconcile_with_an_adjustment(conn, world):
    """Snapshot in, tabs populated, one fund cleared and balanced, another
    closed with the adjustment it needs -- all in one sitting."""
    dlg = _dialog(conn, world, date="2026-01-01")
    log = _seams(dlg, confirm=True)
    dlg.import_snapshot(str(FIXTURE))

    # STABLE's statement count matches the books once its purchase is cleared.
    dlg.select_symbol(STABLE)
    _clear_all(dlg, STABLE)
    assert dlg.difference(STABLE) == Decimal(0)
    assert dlg.finish_current() is True

    # BALANCED's statement says 123.456 and the books say 100: 23.456 unexplained.
    dlg.select_symbol(BALANCED)
    _clear_all(dlg, BALANCED)
    assert dlg.difference(BALANCED) == Decimal("23.456")
    assert dlg.finish_current() is True
    assert log["confirm"][-1][1] == "23.456"

    holdings = investments.compute_holdings(conn, world, as_of=STATEMENT)
    assert holdings[BALANCED].qty == Decimal("123.456")
    assert holdings[STABLE].qty == Decimal("1000")
    assert sorted(dlg.finished_symbols) == sorted([STABLE, BALANCED])
    done = {r["symbol"] for r in conn.execute(
        "SELECT symbol FROM share_reconciliations WHERE account_id=?",
        (world,))}
    assert done == {STABLE, BALANCED}
    # The statement price of a tickerless plan fund came in with the snapshot.
    assert investments.latest_price(conn, BALANCED,
                                    as_of=STATEMENT) == Decimal("24.19")


def test_a_finished_period_is_resumed_showing_R_not_a_blank_slate(conn, world):
    dlg = _dialog(conn, world)
    _seams(dlg)
    dlg.select_symbol(STABLE)
    dlg.set_ending(STABLE, "1000")
    _clear_all(dlg, STABLE)
    assert dlg.finish_current() is True

    later = _dialog(conn, world, date="2026-06-30")
    rows = later.rows_for(STABLE)
    assert all(r["reconciled"] for r in rows)
    assert later._tables[STABLE].item(0, later.CLR).text() == "R"
    # A reconciled row is not the user's to un-clear.
    assert later.toggle_row(STABLE, 0) is False

"""Headless smoke tests for the register UI.

These construct the Qt models (and, in one case, the whole main window) against
a temp database under the offscreen Qt platform -- no display needed -- and
assert that inline edits, transfers, and net worth flow through mammon.ledger.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import categorize, db, investments, ledger
from mammon.ui import prefs
from mammon.ui.delegates import date_edit_iso
from mammon.ui.models import (
    AccountsModel, RegisterModel, SearchResultsModel,
    fmt_cents, fmt_date, fmt_money, parse_amount,
)

from PyQt5.QtCore import QCoreApplication, Qt


def _set_date(edit, iso):
    """Seed a date editor the way the app does. These fields were free-text
    QLineEdits accepting only ISO; they are now QDateEdits carrying the user's
    chosen format, so tests set them by value rather than by typed string."""
    from PyQt5.QtCore import QDate
    edit.setDate(QDate.fromString(iso, "yyyy-MM-dd"))


def _settle():
    """Turn the event loop once, so a deferred inline-edit reload actually runs.

    ``RegisterModel.setData`` writes to the database immediately but pushes the
    model RESET to the next event-loop turn, because Qt calls setModelData before
    it destroys the cell editor, and resetting under a live editor takes the app
    down (see ``RegisterModel._write``). The database is therefore correct the
    instant setData returns, while the model's cached rows -- what ``txn_at``
    reads, and what the ``committed`` signal refreshes -- are one turn behind. A
    running app turns the loop constantly; a test has to do it by hand or it
    asserts against the pre-edit cache.
    """
    QCoreApplication.processEvents()


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    """Redirect QSettings (used by mammon.ui.prefs) to a per-test temp dir, so
    the display/view preferences never read or write the developer's real
    user settings and each test starts from the defaults."""
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def dbfile(tmp_path):
    return tmp_path / "ui.db"


@pytest.fixture
def conn(dbfile):
    c = db.init_db(dbfile)
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=100_00)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    return chk, sav


# ---- money helpers ---------------------------------------------------------
def test_parse_amount_forms():
    assert parse_amount("$1,234.56") == 123456
    assert parse_amount("(5.00)") == -500
    assert parse_amount("-12") == -1200
    assert parse_amount("") == 0
    assert parse_amount("garbage") == 0


def test_fmt_cents():
    assert fmt_cents(-85_32) == "-85.32"
    # thousands separators (from feedback)
    assert fmt_cents(5_000_00) == "5,000.00"
    assert fmt_cents(1_234_567_89) == "1,234,567.89"
    assert fmt_cents(-1_234_56) == "-1,234.56"
    assert fmt_cents(0) == "0.00"
    assert fmt_cents(None) == ""


def test_fmt_money_has_dollar_sign():
    # '$' in front for totals / net worth; sign before the symbol
    assert fmt_money(5_000_00) == "$5,000.00"
    assert fmt_money(-1_234_56) == "-$1,234.56"
    assert fmt_money(0) == "$0.00"
    assert fmt_money(None) == ""
    assert fmt_money(5_000_00, symbol=False) == "5,000.00"


def test_fmt_date_us_format():
    assert fmt_date("2026-08-09") == "08/09/2026"
    assert fmt_date("1995-01-05") == "01/05/1995"
    assert fmt_date("") == ""
    assert fmt_date(None) == ""
    assert fmt_date("not-a-date") == "not-a-date"


def test_fmt_date_all_offered_formats():
    # The explicit fmt= argument renders each offered format from one ISO date.
    from mammon.ui import prefs

    iso = "2026-08-09"
    assert fmt_date(iso, "MM/DD/YYYY") == "08/09/2026"
    assert fmt_date(iso, "DD/MM/YYYY") == "09/08/2026"
    assert fmt_date(iso, "YYYY-MM-DD") == "2026-08-09"
    # Every offered format renders a real ISO date to a non-empty string.
    for f in prefs.DATE_FORMATS:
        assert fmt_date(iso, f)
    # Non-ISO / blank input passes through unchanged whatever the format.
    assert fmt_date("not-a-date", "DD/MM/YYYY") == "not-a-date"
    assert fmt_date("", "YYYY-MM-DD") == ""
    assert fmt_date(None, "MM/DD/YYYY") == ""


def test_fmt_date_follows_preference(qapp):
    # With no explicit fmt, fmt_date reads the persisted preference; the default
    # is US MM/DD/YYYY and changing the pref re-renders through the same helper.
    from mammon.ui import prefs

    assert prefs.date_format() == "MM/DD/YYYY"          # default out of the box
    assert fmt_date("2026-08-09") == "08/09/2026"
    prefs.set_date_format("YYYY-MM-DD")
    assert fmt_date("2026-08-09") == "2026-08-09"
    prefs.set_date_format("DD/MM/YYYY")
    assert fmt_date("2026-08-09") == "09/08/2026"
    # A hand-edited / unknown stored value falls back to the default format.
    prefs.set_date_format("bogus")
    assert prefs.date_format() == "MM/DD/YYYY"
    assert fmt_date("2026-08-09") == "08/09/2026"


# ---- register model shape --------------------------------------------------
def test_columns_headers_and_blank_row(qapp, conn, accounts):
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    ledger.add_transaction(conn, chk, "2026-01-10", 200_00, payee="Paycheck")
    m = RegisterModel(conn, chk)

    assert m.columnCount() == 10
    headers = [m.headerData(c, Qt.Horizontal, Qt.DisplayRole) for c in range(10)]
    # Tag follows Category; Clr sits between Payment and Deposit (from feedback)
    assert headers == ["Date", "Num", "Payee", "Category", "Tag", "Memo",
                       "Payment", "Clr", "Deposit", "Balance"]
    assert m.rowCount() == 3  # two txns + a blank quick-entry row
    # blank row renders empty
    assert m.data(m.index(2, RegisterModel.PAYEE), Qt.DisplayRole) == ""


def test_date_column_displays_us_but_edits_iso(qapp, conn, accounts):
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "1995-01-05", -25_00, payee="Safeway")
    m = RegisterModel(conn, chk)
    idx = m.index(0, RegisterModel.DATE)
    assert m.data(idx, Qt.DisplayRole) == "01/05/1995"   # US display
    assert m.data(idx, Qt.EditRole) == "1995-01-05"      # ISO for the editor/storage


def test_payment_deposit_split_and_balance(qapp, conn, accounts):
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    ledger.add_transaction(conn, chk, "2026-01-10", 200_00, payee="Paycheck")
    m = RegisterModel(conn, chk)

    def cell(r, c):
        return m.data(m.index(r, c), Qt.DisplayRole)

    # negative -> Payment column, positive -> Deposit column
    assert cell(0, RegisterModel.PAYMENT) == "25.00"
    assert cell(0, RegisterModel.DEPOSIT) == ""
    assert cell(1, RegisterModel.PAYMENT) == ""
    assert cell(1, RegisterModel.DEPOSIT) == "200.00"
    # running balance from opening 100.00
    assert cell(0, RegisterModel.BALANCE) == "75.00"
    assert cell(1, RegisterModel.BALANCE) == "275.00"


# ---- bounds safety against a stale view row count --------------------------
def _assert_model_consistent(m):
    """QAbstractItemModelTester-style invariant: rowCount() agrees with the
    backing list (real rows + optional pending + the blank row), and calling
    data()/flags() for EVERY valid index across EVERY interesting role never
    raises -- the property the user's IndexError violated."""
    expected = len(m._rows) + (1 if m.has_pending() else 0) + 1
    assert m.rowCount() == expected
    roles = (Qt.DisplayRole, Qt.EditRole, Qt.ToolTipRole, Qt.DecorationRole,
             Qt.ForegroundRole, Qt.TextAlignmentRole, RegisterModel.SECOND_LINE_ROLE)
    for r in range(m.rowCount()):
        for c in range(m.columnCount()):
            idx = m.index(r, c)
            m.flags(idx)
            for role in roles:
                m.data(idx, role)  # must not raise


def test_data_is_bounds_safe_against_stale_row_index(qapp, conn, accounts):
    """A view whose cached rowCount went stale across a shrink/reload (backup
    restore, account switch, filter) can still paint an index past the current
    list end. data()/flags()/setData() must return an empty value instead of
    raising IndexError on self._rows[row] (models.py ~546, the user's crash)."""
    chk, _ = accounts
    for i in range(3):
        ledger.add_transaction(conn, chk, f"2026-01-0{i + 1}", -10_00, payee=f"P{i}")
    m = RegisterModel(conn, chk)
    assert m.rowCount() == 4  # 3 txns + blank quick-entry row

    # Shrink to zero real rows the way a restore-triggered reload does. reload()
    # brackets the swap in begin/endResetModel, so this is the correct path...
    m.beginResetModel()
    m._rows = []
    m._view = m._project()    # reload() rebuilds the displayed projection this way
    m.endResetModel()
    assert m.rowCount() == 1  # just the blank row now

    # ...but a stale index fabricated the way Qt's C++ view holds one across the
    # transition (createIndex bypasses hasIndex/rowCount validation) still lands
    # in data(). Row 3 is neither the (recomputed) pending nor blank slot.
    far = m.createIndex(3, RegisterModel.PAYEE)
    assert far.isValid()
    assert m.data(far, Qt.DisplayRole) is None
    assert m.data(far, Qt.EditRole) is None
    assert m.data(far, Qt.ToolTipRole) is None
    assert m.data(far, Qt.DecorationRole) is None
    assert m.data(far, Qt.ForegroundRole) is None
    # A stale edit aimed past the end is rejected rather than crashing.
    assert m.setData(far, "x", Qt.EditRole) is False
    # flags() on the stale index is bounds-safe too.
    m.flags(far)
    _assert_model_consistent(m)


def test_model_stays_consistent_through_shrink_reloads(qapp, conn, accounts):
    """Drive the model through repeated shrink+reload cycles (each delete_row
    triggers a full begin/endResetModel) and prove rowCount tracks the backing
    list and every cell stays accessible -- no gap where the view could read a
    stale, out-of-range row."""
    chk, _ = accounts
    for i in range(5):
        ledger.add_transaction(conn, chk, f"2026-02-0{i + 1}", -5_00, payee=f"Q{i}")
    m = RegisterModel(conn, chk)
    assert m.rowCount() == 6
    _assert_model_consistent(m)
    while m.rowCount() > 1:  # 1 == the lone blank row
        before = m.rowCount()
        assert m.delete_row(0)
        assert m.rowCount() == before - 1
        _assert_model_consistent(m)
    assert m.rowCount() == 1  # only the blank quick-entry row remains


# ---- inline add via the blank quick-entry row ------------------------------
def test_add_via_blank_row_persists(qapp, conn, dbfile, accounts):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    blank = m.rowCount() - 1
    m.setData(m.index(blank, RegisterModel.DATE), "2026-02-01", Qt.EditRole)
    m.setData(m.index(blank, RegisterModel.PAYEE), "Rent Co", Qt.EditRole)
    # entering a payment commits the row
    m.setData(m.index(blank, RegisterModel.PAYMENT), "500.00", Qt.EditRole)

    assert ledger.account_balance(conn, chk) == 100_00 - 500_00
    # a fresh connection sees it -> it really hit the file
    c2 = db.connect(dbfile)
    rows = ledger.register_rows(c2, chk)
    c2.close()
    assert any(r["payee"] == "Rent Co" and r["amount"] == -500_00 for r in rows)


# ---- transfer via the category picker --------------------------------------
def test_transfer_via_category_mirrors(qapp, conn, accounts):
    chk, sav = accounts
    m = RegisterModel(conn, chk)
    m.category_choices()  # builds the [Account] target map
    blank = m.rowCount() - 1
    m.setData(m.index(blank, RegisterModel.DATE), "2026-03-01", Qt.EditRole)
    m.setData(m.index(blank, RegisterModel.CATEGORY), "[Savings]", Qt.EditRole)
    m.setData(m.index(blank, RegisterModel.PAYMENT), "300.00", Qt.EditRole)

    # money left checking, arrived in savings, mirrored (net worth unchanged)
    assert ledger.account_balance(conn, chk) == 100_00 - 300_00
    assert ledger.account_balance(conn, sav) == 300_00
    assert ledger.net_worth(conn) == 100_00

    # the checking row shows the transfer category label
    chk_rows = ledger.register_rows(conn, chk)
    assert chk_rows[0]["category_label"] == "[Savings]"
    # and the mirror exists in savings pointing back
    sav_m = RegisterModel(conn, sav)
    assert sav_m.data(sav_m.index(0, RegisterModel.CATEGORY), Qt.DisplayRole) == "[Checking]"
    assert sav_m.data(sav_m.index(0, RegisterModel.DEPOSIT), Qt.DisplayRole) == "300.00"


def test_transfer_payee_displays_on_both_legs(qapp, conn, accounts):
    # the user's bug: a transfer entered with a payee showed a BLANK payee. The
    # register must display and persist the payee on the entered leg AND mirror
    # it to the linked leg (Quicken keeps the same payee on both sides).
    chk, sav = accounts
    m = RegisterModel(conn, chk)
    m.category_choices()
    blank = m.rowCount() - 1
    m.setData(m.index(blank, RegisterModel.DATE), "2026-03-01", Qt.EditRole)
    m.setData(m.index(blank, RegisterModel.PAYEE), "Discover", Qt.EditRole)
    m.setData(m.index(blank, RegisterModel.CATEGORY), "[Savings]", Qt.EditRole)
    m.setData(m.index(blank, RegisterModel.PAYMENT), "300.00", Qt.EditRole)
    _settle()  # blank-row commit defers the model reset a turn (see _settle)

    # entered (checking) leg shows the payee, not a blank cell
    assert m.data(m.index(0, RegisterModel.PAYEE), Qt.DisplayRole) == "Discover"
    # mirror (savings) leg carries the same payee
    sav_m = RegisterModel(conn, sav)
    assert sav_m.data(sav_m.index(0, RegisterModel.PAYEE), Qt.DisplayRole) == "Discover"

    # editing the payee on the mirror leg syncs back to the entered leg
    sav_m.setData(sav_m.index(0, RegisterModel.PAYEE), "Discover Card", Qt.EditRole)
    chk_m = RegisterModel(conn, chk)
    assert chk_m.data(chk_m.index(0, RegisterModel.PAYEE), Qt.DisplayRole) == "Discover Card"


def test_deposit_side_transfer_direction(qapp, conn, accounts):
    chk, sav = accounts
    ledger.create_account(conn, "Savings2", "savings")  # noise account
    m = RegisterModel(conn, chk)
    m.category_choices()
    blank = m.rowCount() - 1
    m.setData(m.index(blank, RegisterModel.DATE), "2026-03-02", Qt.EditRole)
    m.setData(m.index(blank, RegisterModel.CATEGORY), "[Savings]", Qt.EditRole)
    m.setData(m.index(blank, RegisterModel.DEPOSIT), "50.00", Qt.EditRole)  # money IN
    assert ledger.account_balance(conn, chk) == 100_00 + 50_00
    assert ledger.account_balance(conn, sav) == -50_00


# ---- inline edits, cleared toggle, delete ----------------------------------
def test_edit_payee_amount_and_delete(qapp, conn, accounts):
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    m = RegisterModel(conn, chk)

    m.setData(m.index(0, RegisterModel.PAYEE), "Trader Joes", Qt.EditRole)
    _settle()
    assert m.txn_at(0)["payee"] == "Trader Joes"

    # move the value from Payment to Deposit by editing the Deposit column
    m.setData(m.index(0, RegisterModel.DEPOSIT), "40.00", Qt.EditRole)
    _settle()
    assert ledger.account_balance(conn, chk) == 100_00 + 40_00

    assert m.delete_row(0) is True
    assert ledger.account_balance(conn, chk) == 100_00


def test_leading_minus_flips_amount_column(qapp, conn, accounts):
    """feature parity: a leading '-' in an amount column negates the row's
    signed amount and re-renders the magnitude in the OTHER column as a positive
    value, clearing the origin cell -- without disturbing the other fields."""
    chk, _ = accounts
    # A charge that imported with the wrong sign (positive -> Deposit column).
    ledger.add_transaction(
        conn, chk, "2026-01-05", 45_00,
        payee="WF", memo="note", cleared=1,
    )
    m = RegisterModel(conn, chk)

    def cell(c):
        return m.data(m.index(0, c), Qt.DisplayRole)

    # 1) Leading '-' in Payment -> positive amount, shown in Deposit; Payment clears.
    m.setData(m.index(0, RegisterModel.PAYMENT), "-45.00", Qt.EditRole)
    _settle()
    txn = m.txn_at(0)
    assert txn["amount"] == 45_00
    assert cell(RegisterModel.PAYMENT) == ""
    assert cell(RegisterModel.DEPOSIT) == "45.00"
    # other fields untouched
    assert txn["payee"] == "WF"
    assert txn["memo"] == "note"
    assert txn["cleared"] == 1

    # 2) Leading '-' in Deposit -> negative amount, shown in Payment; Deposit clears.
    m.setData(m.index(0, RegisterModel.DEPOSIT), "-45.00", Qt.EditRole)
    _settle()
    txn = m.txn_at(0)
    assert txn["amount"] == -45_00
    assert cell(RegisterModel.DEPOSIT) == ""
    assert cell(RegisterModel.PAYMENT) == "45.00"
    assert txn["payee"] == "WF"
    assert txn["memo"] == "note"
    assert txn["cleared"] == 1


def test_edit_num_is_editable_and_persists(qapp, conn, accounts):
    """The Num (check number) column is editable inline like the other fields;
    the edit persists to the transaction so a user can enter/correct a check
    number manually."""
    chk, _ = accounts
    tid = ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    m = RegisterModel(conn, chk)
    idx = m.index(0, RegisterModel.NUM)
    assert m.flags(idx) & Qt.ItemIsEditable
    assert m.setData(idx, "1234", Qt.EditRole) is True
    _settle()
    assert m.txn_at(0)["num"] == "1234"
    row = conn.execute("SELECT num FROM transactions WHERE id=?", (tid,)).fetchone()
    assert row["num"] == "1234"
    # clearing it writes NULL (mirrors the other nullable text fields)
    m.setData(idx, "", Qt.EditRole)
    assert conn.execute("SELECT num FROM transactions WHERE id=?",
                        (tid,)).fetchone()["num"] is None


def test_clr_shows_letter_and_toggles(qapp, conn, accounts):
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    m = RegisterModel(conn, chk)
    idx = m.index(0, RegisterModel.CLR)
    # Clr is a centered letter, not a checkbox (from feedback)
    assert m.data(idx, Qt.DisplayRole) == ""
    assert not (m.flags(idx) & Qt.ItemIsUserCheckable)
    assert int(m.data(idx, Qt.TextAlignmentRole)) == int(Qt.AlignCenter)
    m.toggle_cleared(0)
    assert m.txn_at(0)["cleared"] == 1
    assert m.data(m.index(0, RegisterModel.CLR), Qt.DisplayRole) == "c"  # cleared
    # a reconciled row shows 'R'
    ledger.update_transaction(conn, m.txn_at(0)["id"], reconciled=1)
    m.reload()
    assert m.data(m.index(0, RegisterModel.CLR), Qt.DisplayRole) == "R"


def test_toggle_unmarks_reconciled_transfer_leg(qapp, conn, accounts):
    """A transfer leg that reached 'R' must be unmarkable in the register exactly
    like any non-transfer row: one click on Clr resets it fully to uncleared, it
    becomes a reconcile candidate again, and the OTHER leg's status is untouched
    (the two legs reconcile independently)."""
    chk, sav = accounts
    from_id, to_id = ledger.create_transfer(conn, chk, sav, "2026-04-10", 40_00)
    # A leg that reached reconciled ('R') -- a genuine reconcile or a legacy auto-mark.
    ledger.update_transaction(conn, from_id, cleared=1, reconciled=1)

    m = RegisterModel(conn, chk)
    row = m.row_for_txn(from_id)
    assert m.data(m.index(row, RegisterModel.CLR), Qt.DisplayRole) == "R"

    # One click fully unmarks it: R -> blank.
    m.toggle_cleared(row)
    leg = ledger.get_transaction(conn, from_id)
    assert leg["cleared"] == 0 and leg["reconciled"] == 0
    assert m.data(m.index(m.row_for_txn(from_id), RegisterModel.CLR),
                  Qt.DisplayRole) == ""

    # The counterparty leg was never touched.
    other = ledger.get_transaction(conn, to_id)
    assert other["cleared"] == 0 and other["reconciled"] == 0

    # And the unmarked leg is a reconcile candidate again.
    assert any(r["id"] == from_id for r in ledger.unreconciled_rows(conn, chk))


def test_toggle_cleared_cycles_blank_c_r_blank_and_persists(qapp, conn, accounts):
    """The register Clr flag cycles blank -> c -> R -> blank (the classic desktop ledger) and
    each step persists to the ledger for THAT transaction (model write path)."""
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    m = RegisterModel(conn, chk)
    tid = m.txn_at(0)["id"]

    def state():
        r = ledger.get_transaction(conn, tid)
        return (r["cleared"], r["reconciled"])

    assert state() == (0, 0)                      # starts blank
    m.toggle_cleared(0)
    assert state() == (1, 0)                       # blank -> c
    assert m.data(m.index(0, RegisterModel.CLR), Qt.DisplayRole) == "c"
    m.toggle_cleared(0)
    assert state() == (1, 1)                       # c -> R
    assert m.data(m.index(0, RegisterModel.CLR), Qt.DisplayRole) == "R"
    m.toggle_cleared(0)
    assert state() == (0, 0)                       # R -> blank
    assert m.data(m.index(0, RegisterModel.CLR), Qt.DisplayRole) == ""


def test_clr_cycle_next_flags_reconciled_steps(qapp, conn, accounts):
    """clr_cycle_next reports which cycle step touches the reconcile-managed 'R'
    flag so the register can confirm exactly those two steps."""
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    m = RegisterModel(conn, chk)

    # blank -> c: no reconciled change.
    assert m.clr_cycle_next(0) == (1, 0, False, False)
    m.toggle_cleared(0)
    # c -> R: SETS reconciled -> confirm.
    assert m.clr_cycle_next(0) == (1, 1, True, True)
    m.toggle_cleared(0)
    # R -> blank: CLEARS reconciled -> confirm.
    assert m.clr_cycle_next(0) == (0, 0, True, False)
    # blank/pending rows have no cycle.
    assert m.clr_cycle_next(m.rowCount() - 1) is None


def test_register_clr_click_cycles_and_confirms_r(qapp, conn, accounts, monkeypatch):
    """Driving the ACTUAL register UI click path: a single click on the Clr cell
    cycles blank -> c -> R -> blank and persists. The c->R set and the R->blank
    clear each raise a confirmation (R is reconcile-managed); blank->c does not."""
    from mammon.ui import widgets
    from mammon.ui.widgets import RegisterWidget
    from PyQt5.QtWidgets import QMessageBox

    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    w = RegisterWidget(conn, chk)
    tid = w.model.txn_at(0)["id"]
    clr = w.model.index(0, RegisterModel.CLR)

    prompts: list = []

    def fake_question(parent, title, text, *a, **k):
        prompts.append((title, text))
        return QMessageBox.Yes

    monkeypatch.setattr(widgets.QMessageBox, "question",
                        staticmethod(fake_question))

    def state():
        r = ledger.get_transaction(conn, tid)
        return (r["cleared"], r["reconciled"])

    # blank -> c: click, persists, NO confirmation.
    w._on_cell_clicked(clr)
    assert state() == (1, 0)
    assert prompts == []

    # c -> R: click, persists, ONE confirmation shown.
    w._on_cell_clicked(clr)
    assert state() == (1, 1)
    assert len(prompts) == 1

    # R -> blank: click, persists, a SECOND confirmation shown.
    w._on_cell_clicked(clr)
    assert state() == (0, 0)
    assert len(prompts) == 2


def test_register_clr_click_r_confirmation_cancel_aborts(qapp, conn, accounts, monkeypatch):
    """Answering No to the 'R' confirmation leaves the flag unchanged."""
    from mammon.ui import widgets
    from mammon.ui.widgets import RegisterWidget
    from PyQt5.QtWidgets import QMessageBox

    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    w = RegisterWidget(conn, chk)
    tid = w.model.txn_at(0)["id"]
    clr = w.model.index(0, RegisterModel.CLR)

    monkeypatch.setattr(widgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.No))

    w.model.toggle_cleared(0)                       # -> c (direct, no prompt)
    assert ledger.get_transaction(conn, tid)["cleared"] == 1
    # Clicking would advance c -> R, but the user cancels the confirmation.
    w._on_cell_clicked(clr)
    r = ledger.get_transaction(conn, tid)
    assert (r["cleared"], r["reconciled"]) == (1, 0)   # unchanged: still 'c'


def test_register_clr_click_is_per_leg_isolated(qapp, conn, accounts, monkeypatch):
    """Editing one transfer leg's Clr through the register click path touches ONLY
    that leg's row -- the counter-leg's cleared/reconciled are never mirrored."""
    from mammon.ui import widgets
    from mammon.ui.widgets import RegisterWidget
    from PyQt5.QtWidgets import QMessageBox

    chk, sav = accounts
    from_id, to_id = ledger.create_transfer(conn, chk, sav, "2026-04-10", 40_00)
    monkeypatch.setattr(widgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))

    w = RegisterWidget(conn, chk)
    row = w.model.row_for_txn(from_id)
    clr = w.model.index(row, RegisterModel.CLR)

    # Cycle this leg all the way to R; the counter-leg must stay (0, 0).
    w._on_cell_clicked(clr)                          # blank -> c
    w._on_cell_clicked(w.model.index(w.model.row_for_txn(from_id),
                                     RegisterModel.CLR))  # c -> R
    leg = ledger.get_transaction(conn, from_id)
    other = ledger.get_transaction(conn, to_id)
    assert (leg["cleared"], leg["reconciled"]) == (1, 1)
    assert (other["cleared"], other["reconciled"]) == (0, 0)


def test_tag_field_roundtrips(qapp, conn, accounts):
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    # add with a tag via the values path (dialog / blank row)
    m.add_from_values({"date": "2026-04-01", "payee": "REI", "payment": "50.00",
                       "category": "Recreation", "tag": "Vacation"})
    txn = m.txn_at(0)
    assert txn["tag"] == "Vacation"
    assert m.data(m.index(0, RegisterModel.TAG), Qt.DisplayRole) == "Vacation"
    # inline-edit the tag
    m.setData(m.index(0, RegisterModel.TAG), "Ski Trip", Qt.EditRole)
    _settle()
    assert m.txn_at(0)["tag"] == "Ski Trip"


def test_transfer_category_is_editable_and_retargets(qapp, conn, accounts):
    """A plain two-sided transfer's Category cell is now EDITABLE inline, and
    picking another '[Account]' moves the mirror leg to the new account with no
    orphan/duplicate (the user's wrong-transfer-account bug)."""
    chk, sav = accounts
    third = ledger.create_account(conn, "Brokerage", "checking")
    from_id, to_id = ledger.create_transfer(conn, chk, sav, "2026-01-05", 10_00)
    m = RegisterModel(conn, chk)
    m.category_choices()
    # the transfer's Category is editable (no longer locked)
    assert m.flags(m.index(0, RegisterModel.CATEGORY)) & Qt.ItemIsEditable
    row = m.row_for_txn(from_id)
    assert m.setData(m.index(row, RegisterModel.CATEGORY), "[Brokerage]",
                     Qt.EditRole) is True
    # the editing leg stayed in checking but now points at Brokerage
    src = ledger.get_transaction(conn, from_id)
    assert src["transfer_account_id"] == third
    # the OLD mirror in savings is gone, exactly one NEW mirror is in brokerage
    assert ledger.get_transaction(conn, to_id) is None
    sav_rows = ledger.register_rows(conn, sav)
    assert sav_rows == []
    brk_rows = ledger.register_rows(conn, third)
    assert len(brk_rows) == 1
    mirror = brk_rows[0]
    assert mirror["amount"] == -src["amount"] == 10_00
    assert mirror["transfer_account_id"] == chk
    # both legs cross-link each other; balances/net worth stay consistent
    assert src["transfer_pair_id"] == mirror["id"]
    assert mirror["transfer_pair_id"] == from_id
    assert ledger.account_balance(conn, sav) == 0
    assert ledger.account_balance(conn, third) == 10_00
    assert ledger.net_worth(conn) == 100_00


def test_inline_category_set_to_account_creates_transfer(qapp, conn, accounts):
    """BUG: setting an existing PLAIN transaction's Category to an account
    ("[Account]") must persist it as a transfer AND create the linked opposite
    entry in that account -- the inline edit was previously silently dropped."""
    chk, sav = accounts
    t = ledger.add_transaction(conn, chk, "2026-03-01", -40_00, payee="Move")
    m = RegisterModel(conn, chk)
    m.category_choices()  # build the [Account] target map
    row = m.row_for_txn(t)
    assert m.setData(m.index(row, RegisterModel.CATEGORY), "[Savings]",
                     Qt.EditRole) is True

    src = ledger.get_transaction(conn, t)
    assert src["transfer_account_id"] == sav and src["category_id"] is None
    assert ledger.account_balance(conn, chk) == 100_00 - 40_00
    assert ledger.account_balance(conn, sav) == 40_00
    assert ledger.net_worth(conn) == 100_00
    # register now renders the transfer label; its Category stays editable so a
    # mistargeted transfer can be re-pointed (the user's bug).
    m.reload()
    r = m.row_for_txn(t)
    assert m.data(m.index(r, RegisterModel.CATEGORY), Qt.DisplayRole) == "[Savings]"
    assert m.flags(m.index(r, RegisterModel.CATEGORY)) & Qt.ItemIsEditable


def test_details_dialog_category_to_account_creates_transfer(qapp, conn, accounts):
    """The details/edit dialog path (update_from_values) also converts a plain
    transaction to a transfer when its category is set to an account, using the
    edited amount for the mirror."""
    chk, sav = accounts
    t = ledger.add_transaction(conn, chk, "2026-03-01", -40_00, payee="Move")
    m = RegisterModel(conn, chk)
    m.category_choices()
    row = m.row_for_txn(t)
    assert m.update_from_values(row, {
        "date": "2026-03-02", "payee": "Move", "category": "[Savings]",
        "payment": "55.00", "deposit": ""}) is True
    src = ledger.get_transaction(conn, t)
    assert src["transfer_account_id"] == sav and src["amount"] == -55_00
    assert src["date"] == "2026-03-02"
    assert ledger.account_balance(conn, sav) == 55_00  # mirror got the new amount


def test_details_dialog_retargets_transfer_and_syncs_edits(qapp, conn, accounts):
    """The edit dialog re-points an existing transfer AND applies a same-save
    amount/date/memo change to the moved mirror (the user's wrong-account bug)."""
    chk, sav = accounts
    third = ledger.create_account(conn, "Brokerage", "checking")
    from_id, to_id = ledger.create_transfer(conn, chk, sav, "2026-05-01", 40_00,
                                            memo="old", payee="Move")
    m = RegisterModel(conn, chk)
    m.category_choices()
    row = m.row_for_txn(from_id)
    assert m.update_from_values(row, {
        "date": "2026-05-02", "payee": "Move", "category": "[Brokerage]",
        "memo": "new", "payment": "55.00", "deposit": ""}) is True
    src = ledger.get_transaction(conn, from_id)
    assert src["transfer_account_id"] == third
    assert src["amount"] == -55_00 and src["date"] == "2026-05-02"
    assert src["memo"] == "new"
    assert ledger.get_transaction(conn, to_id) is None       # old mirror gone
    mirror = ledger.get_transaction(conn, src["transfer_pair_id"])
    assert mirror["account_id"] == third
    assert mirror["amount"] == 55_00 and mirror["date"] == "2026-05-02"
    assert mirror["memo"] == "new"
    assert ledger.account_balance(conn, sav) == 0
    assert ledger.account_balance(conn, third) == 55_00


def test_details_dialog_transfer_fields_enabled_for_retarget(qapp, conn, accounts):
    """The edit dialog's Category/Payee fields are ENABLED for a plain transfer
    so its account can be changed there (not grayed out)."""
    from mammon.ui.widgets import TransactionDialog
    chk, sav = accounts
    from_id, _ = ledger.create_transfer(conn, chk, sav, "2026-05-01", 40_00)
    m = RegisterModel(conn, chk)
    row = m.row_for_txn(from_id)
    dlg = TransactionDialog(m, row=row)
    assert dlg.category.isEnabled()
    assert dlg.payee.isEnabled()


# ---- auto-categorization wiring (SRD 5.5) ----------------------------------
def test_new_entry_autofills_category_from_history(qapp, conn, accounts):
    chk, _ = accounts
    groceries = ledger.resolve_category(conn, "Groceries")
    for i in range(3):
        ledger.add_transaction(conn, chk, f"2026-01-0{i+1}", -30_00,
                               payee="Safeway #100", category_id=groceries)
    categorize.learn_from_history(conn)

    m = RegisterModel(conn, chk)
    # New entry, same payee (different store number), no category typed.
    m.add_from_values({"date": "2026-02-01", "payee": "Safeway #777",
                       "payment": "42.00"})
    added = next(r for r in m._rows if r["payee"] == "Safeway #777")
    assert added["category_id"] == groceries


def test_typed_category_records_user_override(qapp, conn, accounts):
    chk, _ = accounts
    groceries = ledger.resolve_category(conn, "Groceries")
    dining = ledger.resolve_category(conn, "Dining")
    for i in range(3):
        ledger.add_transaction(conn, chk, f"2026-01-0{i+1}", -30_00,
                               payee="Corner Market", category_id=groceries)
    categorize.learn_from_history(conn)
    assert categorize.suggest_category(conn, "Corner Market") == groceries

    # User types a DIFFERENT category on a new entry for that payee.
    m = RegisterModel(conn, chk)
    m.add_from_values({"date": "2026-02-01", "payee": "Corner Market",
                       "payment": "12.00", "category": "Dining"})
    # The override is learned and now outranks the historical pattern.
    assert categorize.suggest_category(conn, "Corner Market") == dining
    assert categorize.mapping_for(conn, "Corner Market")["source"] == "user"


def test_inline_category_edit_records_override(qapp, conn, accounts):
    chk, _ = accounts
    dining = ledger.resolve_category(conn, "Dining")
    ledger.add_transaction(conn, chk, "2026-01-05", -18_00, payee="Bistro 9")
    m = RegisterModel(conn, chk)
    m.setData(m.index(0, RegisterModel.CATEGORY), "Dining", Qt.EditRole)
    _settle()
    assert m.txn_at(0)["category_id"] == dining
    # ...and that manual edit taught the mapping for next time.
    assert categorize.suggest_category(conn, "Bistro 9") == dining
    assert categorize.mapping_for(conn, "Bistro 9")["source"] == "user"


def test_autofill_does_not_override_typed_category(qapp, conn, accounts):
    chk, _ = accounts
    groceries = ledger.resolve_category(conn, "Groceries")
    dining = ledger.resolve_category(conn, "Dining")
    for i in range(3):
        ledger.add_transaction(conn, chk, f"2026-01-0{i+1}", -30_00,
                               payee="Deli", category_id=groceries)
    categorize.learn_from_history(conn)
    m = RegisterModel(conn, chk)
    # User explicitly typed Dining -> that wins over the Groceries suggestion.
    m.add_from_values({"date": "2026-02-01", "payee": "Deli",
                       "payment": "9.00", "category": "Dining"})
    added = next(r for r in m._rows if r["date"] == "2026-02-01")
    assert added["category_id"] == dining


# ---- accounts model + net worth --------------------------------------------
def test_accounts_model_and_net_worth(qapp, conn, accounts):
    chk, sav = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00)
    am = AccountsModel(conn)
    assert am.rowCount() == 2
    assert am.columnCount() == 3
    assert am.net_worth() == 100_00 - 25_00
    # balance column formats cents
    names = {am.data(am.index(r, AccountsModel.NAME), Qt.DisplayRole) for r in range(2)}
    assert names == {"Checking", "Savings"}


def test_account_bar_groups_and_activates(qapp, conn, accounts):
    from mammon.ui.widgets import AccountBar
    chk, sav = accounts
    bar = AccountBar(conn)
    # every account is a selectable child item under its group section
    assert set(bar._item_by_account) == {chk, sav}
    # clicking an account item emits its id
    seen = []
    bar.accountActivated.connect(seen.append)
    bar._on_item(bar._item_by_account[chk], 0)
    assert seen == [chk]
    # net worth is exposed for the bottom strip
    assert bar.model.net_worth() == ledger.net_worth(conn)


def test_account_bar_boxes_and_dollar_networth(qapp, conn, accounts):
    from PyQt5.QtWidgets import QFrame

    from mammon.ui.widgets import AccountBar
    chk, sav = accounts
    bar = AccountBar(conn)
    # net worth is '$'-prefixed with commas and aligned right (from feedback)
    assert bar.net_amount.text() == "$100.00"
    # each populated category is drawn as its own boxed QFrame
    boxes = bar._body.findChildren(QFrame, "acctGroup")
    assert len(boxes) >= 1
    # selecting an account marks its row 'selected' for the highlight
    bar.select_account(chk)
    assert bar._item_by_account[chk].property("selected") is True
    bar.select_account(sav)
    assert bar._item_by_account[chk].property("selected") is False
    assert bar._item_by_account[sav].property("selected") is True


def test_account_bar_min_width_fits_widest_name_plus_scrollbar(qapp, conn):
    """The accounts panel raises its minimum width so the widest account name
    AND the vertical scrollbar fit -- no horizontal scrollbar at the default
    width (Task 49). It stays draggable: it sets a minimum only, and the max cap
    is never left below the min. The floor is content-driven and recomputed on
    refresh, so a longer name widens it."""
    from PyQt5.QtGui import QFontMetrics
    from PyQt5.QtWidgets import QStyle

    from mammon.ui.widgets import AccountBar, _text_width

    long_name = "Property & Debt: 2nd Mortgage Escrow Reserve"
    ledger.create_account(conn, "Cash", "cash", opening_balance=0)
    ledger.create_account(conn, long_name, "liability", opening_balance=-5000_00)

    bar = AccountBar(conn)
    floor = bar._required_width()
    # the panel's minimum tracks the computed floor
    assert bar.minimumWidth() == floor
    # and that floor comfortably covers the widest name PLUS the vertical scrollbar
    widest = _text_width(QFontMetrics(bar.font()), long_name)
    sb = bar.style().pixelMetric(QStyle.PM_ScrollBarExtent)
    assert floor >= widest + sb
    # never a fixed pin: the max cap stays >= the min so the splitter can move
    assert bar.maximumWidth() >= bar.minimumWidth()

    # a longer account name pushes the floor wider (content-driven, recomputed)
    ledger.create_account(
        conn, long_name + " plus an even longer descriptive suffix",
        "liability", opening_balance=-100_00)
    bar.refresh()
    assert bar._required_width() > floor
    assert bar.minimumWidth() == bar._required_width()


def test_ledger_update_account_and_date_bounds(conn, accounts):
    chk, _ = accounts
    ledger.update_account(conn, chk, name="Main Checking", note="primary",
                          institution="Second Credit Union")
    a = ledger.get_account(conn, chk)
    assert a["name"] == "Main Checking"
    assert a["note"] == "primary"
    assert a["institution"] == "Second Credit Union"
    # date bounds: empty, then the min/max ISO dates across transactions
    assert ledger.transaction_date_bounds(conn) == (None, None)
    ledger.add_transaction(conn, chk, "1995-01-05", -10_00)
    ledger.add_transaction(conn, chk, "1996-12-31", 20_00)
    assert ledger.transaction_date_bounds(conn) == ("1995-01-05", "1996-12-31")
    # opening_balance is intentionally NOT editable via update_account
    with pytest.raises(ValueError):
        ledger.update_account(conn, chk, opening_balance=999)


def test_main_window_menus_present(qapp, tmp_path):
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow
    conn = db.init_db(tmp_path / "menus.db")
    sample_data(conn)
    win = MainWindow(conn)
    titles = {a.text().replace("&", "") for a in win.menuBar().actions()}
    assert {"File", "Edit", "Settings", "Reports"} <= titles
    win.close()
    conn.close()


# ---- transaction search (within-account + global) --------------------------
def test_search_transactions_scopes_and_fields(conn, accounts):
    chk, sav = accounts
    groc = ledger.resolve_category(conn, "Groceries")
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway",
                           category_id=groc, tag="Weekly", memo="milk and eggs")
    ledger.add_transaction(conn, chk, "2026-01-10", 200_00, payee="Paycheck")
    ledger.add_transaction(conn, sav, "2026-01-06", -12_34, payee="Safeway Fuel")

    # GLOBAL: both Safeway rows across accounts (case-insensitive payee match)
    hits = ledger.search_transactions(conn, "safeway")
    assert {h["account_name"] for h in hits} == {"Checking", "Savings"}
    assert len(hits) == 2

    # WITHIN an account: only the checking Safeway row
    scoped = ledger.search_transactions(conn, "safeway", account_id=chk)
    assert len(scoped) == 1 and scoped[0]["account_name"] == "Checking"

    # matches on memo, tag, category path, and amount
    assert len(ledger.search_transactions(conn, "milk")) == 1        # memo
    assert len(ledger.search_transactions(conn, "weekly")) == 1      # tag
    assert len(ledger.search_transactions(conn, "groceries")) == 1   # category
    assert len(ledger.search_transactions(conn, "12.34")) == 1       # amount
    # blank query and a genuine miss both return nothing
    assert ledger.search_transactions(conn, "   ") == []
    assert ledger.search_transactions(conn, "nonexistent-payee") == []


def test_search_results_model(qapp, conn, accounts):
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "1995-01-05", -25_00, payee="Safeway")
    hits = ledger.search_transactions(conn, "safeway")
    m = SearchResultsModel(hits)
    assert m.rowCount() == 1
    assert m.columnCount() == 8
    # account name + US date + signed amount render for cross-account legibility
    assert m.data(m.index(0, SearchResultsModel.ACCOUNT), Qt.DisplayRole) == "Checking"
    assert m.data(m.index(0, SearchResultsModel.DATE), Qt.DisplayRole) == "01/05/1995"
    assert m.data(m.index(0, SearchResultsModel.AMOUNT), Qt.DisplayRole) == "-25.00"
    assert m.result_at(0)["payee"] == "Safeway"


def test_search_matches_investment_security_amount_memo(qapp, conn):
    """Search reaches investment transactions too: security symbol + name,
    amount (as displayed dollars) and memo -- fields the cash-only search missed
    entirely (an investment account used to be an unsearchable no-op)."""
    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    investments.record_investment(conn, acct, "2021-03-04", "Buy", symbol="AAPL",
                                  quantity="10", price="150", amount=-1500_00,
                                  memo="opening lot")
    investments.record_investment(conn, acct, "2021-05-01", "Buy", symbol="VBTLX",
                                  quantity="20", price="11", amount=-220_00,
                                  memo="bond fund add")
    investments.rebuild_holdings(conn, acct)
    # holdings.name is NULL after a rebuild; importers set it. Emulate that so
    # the security-NAME search path (LEFT JOIN holdings) is exercised.
    conn.execute("UPDATE holdings SET name=? WHERE account_id=? AND symbol=?",
                 ("Apple Inc", acct, "AAPL"))
    conn.commit()

    # by security SYMBOL (always available, straight off investment_transactions)
    by_sym = ledger.search_transactions(conn, "aapl")
    assert len(by_sym) == 1
    assert by_sym[0]["is_investment"] is True
    assert by_sym[0]["symbol"] == "AAPL"
    assert by_sym[0]["account_name"] == "Brokerage"

    # by security NAME (joined from holdings.name; label renders "AAPL — Apple Inc")
    by_name = ledger.search_transactions(conn, "apple")
    assert len(by_name) == 1 and by_name[0]["symbol"] == "AAPL"
    assert "Apple Inc" in by_name[0]["payee"]

    # by MEMO on an investment row
    assert len(ledger.search_transactions(conn, "bond fund")) == 1

    # by AMOUNT: -220_00 cents -> "220.00" displayed dollars
    by_amt = ledger.search_transactions(conn, "220.00")
    assert len(by_amt) == 1 and by_amt[0]["symbol"] == "VBTLX"

    # scoped to the investment account (previously returned nothing at all)
    scoped = ledger.search_transactions(conn, "vbtlx", account_id=acct)
    assert len(scoped) == 1 and scoped[0]["symbol"] == "VBTLX"

    # a genuine miss still returns nothing
    assert ledger.search_transactions(conn, "tsla") == []

    # an investment hit renders through SearchResultsModel (amount is int cents,
    # not None -> the model's signed-amount path stays crash-free)
    sm = SearchResultsModel(by_sym)
    assert sm.rowCount() == 1
    assert sm.data(sm.index(0, SearchResultsModel.AMOUNT), Qt.DisplayRole) == "-1,500.00"
    assert "AAPL" in sm.data(sm.index(0, SearchResultsModel.PAYEE), Qt.DisplayRole)
    assert sm.data(sm.index(0, SearchResultsModel.CATEGORY), Qt.DisplayRole) == "Buy"

    # navigation plumbing: the register model can locate the row for a hit's id
    from mammon.ui.models import InvestmentRegisterModel
    im = InvestmentRegisterModel(conn, acct)
    assert im.row_for_txn(by_sym[0]["id"]) >= 0
    assert im.row_for_txn(-999) == -1


def test_search_dialog_finds_and_opens(qapp, tmp_path):
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow, SearchDialog
    conn = db.init_db(tmp_path / "find.db")
    sample_data(conn)
    win = MainWindow(conn)
    first = ledger.list_accounts(conn)[0]["id"]
    tid = ledger.add_transaction(conn, first, "2026-06-01", -77_00,
                                 payee="Zzyzx Diner")

    dlg = SearchDialog(conn, default_account_id=first, parent=win)
    # scope defaulted to the current account
    assert dlg.scope.currentData() == first
    dlg.query.setText("zzyzx")
    hits = dlg.run_search()
    assert [h["id"] for h in hits] == [tid]
    assert dlg.results.rowCount() == 1

    # activating a result opens that account's register and selects the row
    opened = []
    dlg.activated.connect(lambda aid, t: opened.append((aid, t)))
    win._open_search_result(first, tid)  # what the activated signal drives
    reg = win.open_register(first)
    assert reg.select_txn(tid) is True
    win.close()
    conn.close()


def test_ensure_seed_only_on_demo(tmp_path):
    # A plain open must NOT fabricate the sample "test set"; only --demo seeds.
    from mammon import app as appmod
    c = db.init_db(tmp_path / "plain.db")
    appmod._ensure_seed(c, demo=False)
    assert ledger.list_accounts(c) == []
    appmod._ensure_seed(c, demo=True)
    assert len(ledger.list_accounts(c)) >= 1
    c.close()


def test_main_window_open_database_switches(qapp, tmp_path):
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow
    a_path = tmp_path / "a.db"
    a = db.init_db(a_path)
    sample_data(a)
    win = MainWindow(a, db_path=str(a_path))
    assert win.accounts.model.rowCount() >= 3
    # a second, different database with just one account
    b_path = tmp_path / "b.db"
    b = db.init_db(b_path)
    ledger.create_account(b, "Solo", "checking", opening_balance=500_00)
    b.close()
    win.open_database(str(b_path))
    assert win.db_path == str(b_path)
    assert {r["name"] for r in win.accounts.model.rows()} == {"Solo"}
    win.close()


def test_main_window_new_database_creates_empty(qapp, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QFileDialog
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow
    a_path = tmp_path / "a.db"
    a = db.init_db(a_path)
    sample_data(a)
    win = MainWindow(a, db_path=str(a_path))
    assert win.accounts.model.rowCount() >= 3        # sample data present

    # 'New Database…' is wired into the File menu (the user couldn't find it).
    file_menu = next(m.menu() for m in win.menuBar().actions()
                     if m.text().replace("&", "") == "File")
    labels = {act.text().replace("…", "").strip() for act in file_menu.actions()}
    assert "New Database" in labels

    # Choosing a brand-new filename points the window at a fresh EMPTY ledger.
    new_path = tmp_path / "fresh.db"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (str(new_path), "")))
    win._new_database_dialog()
    assert win.db_path == str(new_path)
    assert new_path.exists()
    assert win.accounts.model.rowCount() == 0        # no accounts in a new db
    win.close()


# ---- whole window opens (exercises widgets + delegates) --------------------
def test_main_window_opens_register(qapp, tmp_path):
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow

    conn = db.init_db(tmp_path / "win.db")
    sample_data(conn)
    win = MainWindow(conn)
    first = ledger.list_accounts(conn)[0]["id"]
    reg = win.open_register(first)
    assert reg.model.rowCount() >= 1
    # net worth is unaffected by the seeded transfer (mirror nets to zero)
    assert win.accounts.model.net_worth() == ledger.net_worth(conn)
    win.close()
    conn.close()


def test_account_details_shows_address_for_assets_and_a_lien_for_loans(qapp, conn):
    """Type-conditional rows: Address on an asset (seeded from the Institution
    field people typed addresses into before it existed), Secured by on a loan."""
    from mammon import asset_values
    from mammon.ui.widgets import AccountDetailsDialog

    house = ledger.create_account(conn, "120 Cedar Ln", "asset",
                                  opening_balance=185_000_00)
    ledger.update_account(conn, house, institution="120 Cedar Ln, ANYTOWN ST")
    loan = ledger.create_account(conn, "240 Birch Loan", "liability",
                                 opening_balance=-50_000_00)

    dlg = AccountDetailsDialog(ledger.get_account(conn, house), conn=conn)
    # The address already typed into Institution seeds the new field...
    assert dlg.property_address.text() == "120 Cedar Ln, ANYTOWN ST"
    assert dlg.property_address.isVisibleTo(dlg)
    # ...and Institution itself is gone: a house has no institution.
    assert not dlg.institution.isVisibleTo(dlg)
    assert not dlg.secured_by.isVisibleTo(dlg)
    assert dlg.values()["property_address"] == "120 Cedar Ln, ANYTOWN ST"
    dlg.deleteLater()

    dlg2 = AccountDetailsDialog(ledger.get_account(conn, loan), conn=conn)
    assert dlg2.secured_by.isVisibleTo(dlg2)
    assert not dlg2.property_address.isVisibleTo(dlg2)
    assert [dlg2.secured_by.itemText(i) for i in range(dlg2.secured_by.count())] == \
        ["(not secured by an asset)", "120 Cedar Ln"]
    assert dlg2.values()["secured_by_account_id"] is None
    dlg2.secured_by.setCurrentIndex(dlg2.secured_by.findData(house))
    assert dlg2.values()["secured_by_account_id"] == house
    ledger.update_account(conn, loan, **{k: v for k, v in dlg2.values().items()
                                         if k != "lot_method"})
    assert asset_values.lien_of(conn, loan) == house
    dlg2.deleteLater()

    # Re-opened, the saved lien is selected; a lien onto a CLOSED property is
    # still offered rather than silently reading as "(not secured)".
    ledger.update_account(conn, house, closed_flag=1)
    dlg3 = AccountDetailsDialog(ledger.get_account(conn, loan), conn=conn)
    assert dlg3.secured_by.currentData() == house
    assert "closed" in dlg3.secured_by.currentText()
    dlg3.deleteLater()


# ---- report charts (P2f: matplotlib-backed pie + net-worth line) -----------
def test_report_charts_build(qapp, conn, accounts):
    from mammon import reports
    from mammon.ui.charts import ChartDialog, NetWorthCanvas, SpendingPieCanvas

    chk, sav = accounts
    groc = ledger.resolve_category(conn, "Groceries")
    fuel = ledger.resolve_category(conn, "Auto & Transport:Fuel")
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, category_id=groc)
    ledger.add_transaction(conn, chk, "2026-01-10", -40_00, category_id=fuel)
    ledger.add_transaction(conn, chk, "2026-02-01", 500_00, payee="Paycheck")

    pie = reports.spending_pie(conn, "2026-01-01", "2026-02-28")
    pcanvas = SpendingPieCanvas(pie)
    assert pcanvas.figure.axes                      # a real Axes was drawn
    dlg = ChartDialog("Spending", pcanvas)
    assert dlg.canvas is pcanvas

    series = reports.net_worth_series(conn, points=6)
    ncanvas = NetWorthCanvas(series)
    assert ncanvas.figure.axes
    dlg2 = ChartDialog("Net Worth", ncanvas)
    assert dlg2.canvas is ncanvas


def test_slices_pie_groups_small_slices_and_drills_into_other(qapp):
    """The lowest categories that together make up 10% of the total become one
    Other wedge, anything still under 5% of the drawn pie loses its label, and
    Other opens (with a way back)."""
    from mammon.ui.charts import SlicesPieCanvas, group_small_slices

    rows = [("Real estate", 300000_00), ("Cash", 48490_00), ("Stock", 21000_00),
            ("Bonds", 9000_00), ("Gold", 500_00), ("Crypto", 300_00)]
    # Total 379290_00; smallest-upward Crypto+Gold+Bonds+Stock reach only 8.1%,
    # so Cash is pulled in too -- the set that first clears 10% -- leaving Real
    # estate as the only stand-alone wedge.
    c = SlicesPieCanvas("By asset class", rows)
    assert c.drawn_slices() == [
        ("Real estate", 300000_00),
        ("Other", 48490_00 + 21000_00 + 9000_00 + 500_00 + 300_00)]
    assert [lab for lab, _ in c.grouped_members()] == \
        ["Cash", "Stock", "Bonds", "Gold", "Crypto"]            # largest first
    assert c.visible_labels() == ["Real estate", "Other"]       # Other always labelled
    assert "click it to break it out" in c.figure.axes[0].get_title()

    assert c.drill_into_other() is True and c.zoom_path() == ["Other"]
    # Inside Other the members are drawn at full size (their own long tail --
    # Bonds+Gold+Crypto -- regroups), but their percentages read against the
    # WHOLE, not against Other.
    assert [lab for lab, _ in c.drawn_slices()] == ["Cash", "Stock", "Other"]
    assert c.visible_labels() == ["Cash", "Stock", "Other"]
    whole = 379290_00
    pct = dict(c.slice_percentages())
    assert pct["Cash"] == pytest.approx(48490_00 / whole * 100.0)
    assert pct["Stock"] == pytest.approx(21000_00 / whole * 100.0)
    assert pct["Other"] == pytest.approx((9000_00 + 500_00 + 300_00) / whole * 100.0)
    assert "\u25b8 Other" in c.figure.axes[0].get_title()
    # ...and there is always a way back out.
    assert c.zoom_out() is True and c.zoom_path() == []
    assert c.zoom_out() is False and c.drill_into_other() is True
    c.zoom_out()

    # Guards: a single slice that already clears the bar is never renamed Other,
    # and when the roll-up would swallow every category the pie is drawn
    # ungrouped rather than collapsing into a single wedge.
    assert group_small_slices([("A", 70_00), ("B", 30_00)], 10.0) == \
        ([("A", 70_00), ("B", 30_00)], [])
    assert group_small_slices([("A", 95_00), ("B", 5_00)], 10.0) == \
        ([("A", 95_00), ("B", 5_00)], [])
    # A slice genuinely called "Other" joins the group rather than being drawn
    # a second time under the same name.
    drawn, grouped = group_small_slices(
        [("Big", 60_00), ("Other", 35_00), ("Tiny", 3_00), ("Sliver", 2_00)], 10.0)
    assert drawn == [("Big", 60_00), ("Other", 40_00)]
    assert grouped == [("Other", 35_00), ("Tiny", 3_00), ("Sliver", 2_00)]
    c.deleteLater()


def test_report_charts_handle_empty(qapp, conn):
    # Empty ledger: both canvases render a placeholder instead of raising.
    from mammon import reports
    from mammon.ui.charts import NetWorthCanvas, SpendingPieCanvas

    pie = reports.spending_pie(conn, "2026-01-01", "2026-01-31")
    assert pie.is_empty()
    assert SpendingPieCanvas(pie).figure.axes            # placeholder axes
    series = reports.net_worth_series(conn)
    assert series.is_empty()
    assert NetWorthCanvas(series).figure.axes


# ---- splits (P2h: Split dialog + register --Split-- display) ---------------
def test_split_dialog_applies_and_register_shows_split(qapp, conn, accounts):
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import SplitDialog

    chk, _ = accounts
    groc = ledger.resolve_category(conn, "Groceries")
    dining = ledger.resolve_category(conn, "Dining")
    t = ledger.add_transaction(conn, chk, "2026-01-05", -100_00, payee="Costco")

    model = RegisterModel(conn, chk)
    row = model.row_for_txn(t)
    dlg = SplitDialog(model, row)
    # clear the seeded lines, then add two categories summing to -100.00.
    for e in list(dlg._lines):
        dlg._remove_line(e)
    dlg.add_line("Groceries", -60.00, "food")
    dlg.add_line("Dining", -40.00, "")
    dlg._update_remainder()
    assert dlg.remainder_cents() == 0
    assert dlg.ok_btn.isEnabled()
    assert dlg.apply_split() is True

    lines = ledger.get_splits(conn, t)
    assert [(l["category_label"], l["amount"]) for l in lines] == [
        ("Groceries", -60_00), ("Dining", -40_00)]

    # the register now shows --Split-- and locks Category/Payment/Deposit inline.
    model.reload()
    r = model.row_for_txn(t)
    assert model.data(model.index(r, RegisterModel.CATEGORY)) == "--Split--"
    for col in (RegisterModel.CATEGORY, RegisterModel.PAYMENT, RegisterModel.DEPOSIT):
        assert not (model.flags(model.index(r, col)) & Qt.ItemIsEditable)


def test_split_copy_from_previous_payee_populates_categories_and_amounts(
        qapp, conn, accounts):
    """USER REQUEST: recurring paycheck/bill splits are tedious to re-enter. When
    a payee has a PRIOR split, the dialog offers a 'Copy from previous <payee>
    split' button that duplicates the prior split's category rows AND amounts."""
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import SplitDialog

    chk, _ = accounts
    salary = ledger.resolve_category(conn, "Salary")
    taxes = ledger.resolve_category(conn, "Taxes")

    # A prior split paycheck for payee "Employer" (1500 gross - 500 tax = 1000).
    t1 = ledger.add_transaction(conn, chk, "2026-01-01", 1000_00, payee="Employer")
    ledger.set_splits(conn, t1, [
        {"category_id": salary, "amount": 1500_00, "memo": "gross"},
        {"category_id": taxes, "amount": -500_00, "memo": "withholding"},
    ])

    # A later same-payee transaction not yet split.
    t2 = ledger.add_transaction(conn, chk, "2026-02-01", 1000_00, payee="Employer")

    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, model.row_for_txn(t2))
    # Button is present and labelled with the actual payee name.
    assert hasattr(dlg, "copy_prev_btn")
    assert dlg.copy_prev_btn.text() == "Copy from previous Employer split"

    dlg._copy_previous_split()
    got = [(e["cat"].currentText(), e["amount"].value(), e["memo"].text())
           for e in dlg._lines]
    assert got == [
        ("Salary", 1500.00, "gross"),
        ("Taxes", -500.00, "withholding"),
    ]
    # Amounts sum to the new transaction's total, so OK is enabled straight away.
    dlg._update_remainder()
    assert dlg.remainder_cents() == 0
    assert dlg.ok_btn.isEnabled()


def test_split_copy_button_absent_without_prior_payee_split(qapp, conn, accounts):
    """No button when the payee has no prior split -- including when a prior split
    exists under a DIFFERENT payee, or a same-payee transaction is not split."""
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import SplitDialog

    chk, _ = accounts
    salary = ledger.resolve_category(conn, "Salary")
    taxes = ledger.resolve_category(conn, "Taxes")

    # A prior split under a DIFFERENT payee.
    other = ledger.add_transaction(conn, chk, "2026-01-01", 1000_00, payee="Acme")
    ledger.set_splits(conn, other, [
        {"category_id": salary, "amount": 1500_00, "memo": ""},
        {"category_id": taxes, "amount": -500_00, "memo": ""},
    ])
    # A prior NON-split transaction for the same payee we will open.
    ledger.add_transaction(conn, chk, "2026-01-15", -20_00, payee="Costco")

    t = ledger.add_transaction(conn, chk, "2026-02-01", -100_00, payee="Costco")
    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, model.row_for_txn(t))
    assert not hasattr(dlg, "copy_prev_btn")


def test_split_dialog_allows_transfer_leg(qapp, conn, accounts):
    """USER REQUEST: a split line may be a transfer to an account (a paycheck's
    401(k) deferral, a mortgage principal leg). The dialog OFFERS the '[Account]'
    choices (previously filtered out) and persists the chosen leg as a transfer
    with a reciprocal mirror in that account."""
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import SplitDialog

    chk, _ = accounts
    k401 = ledger.create_account(conn, "401K", "investment")
    t = ledger.add_transaction(conn, chk, "2026-02-01", 1000_00, payee="Payroll")

    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, model.row_for_txn(t))
    assert "[401K]" in dlg._cats                 # account choice is offered now
    for e in list(dlg._lines):
        dlg._remove_line(e)
    dlg.add_line("Salary", 1200.00, "")          # +1200 income
    dlg.add_line("[401K]", -200.00, "deferral")  # -200 to the 401K account
    dlg._update_remainder()
    assert dlg.remainder_cents() == 0
    assert dlg.apply_split() is True

    legs = {l["category_label"]: l for l in ledger.get_splits(conn, t)}
    assert legs["[401K]"]["transfer_account_id"] == k401
    assert ledger.account_balance(conn, k401) == 200_00     # reciprocal mirror


def test_split_prepopulates_existing_category(qapp, conn, accounts):
    """SPLIT DEFECT A: opening a split on a transaction that has NO split yet
    pre-populates line 1 with the transaction's existing single category (and
    its full amount), instead of two blank rows that discard the category."""
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import SplitDialog
    chk, _ = accounts
    groc = ledger.resolve_category(conn, "Groceries")
    t = ledger.add_transaction(conn, chk, "2026-01-05", -100_00, payee="Costco",
                               category_id=groc)
    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, model.row_for_txn(t))
    assert len(dlg._lines) == 2
    assert dlg._lines[0]["cat"].currentText() == "Groceries"
    assert int(round(dlg._lines[0]["amount"].value() * 100)) == -100_00
    assert dlg._lines[1]["cat"].currentText() == ""


def test_split_ok_enables_when_transfer_category_chosen(qapp, conn, accounts):
    """SPLIT DEFECT B: choosing a transfer account for a split line must
    re-evaluate the OK button. Line 2 here carries no amount, so it only starts
    counting once it gets a category; picking "[Savings]" from the dropdown must
    enable OK WITHOUT any manual _update_remainder() call."""
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import SplitDialog
    chk, sav = accounts
    t = ledger.add_transaction(conn, chk, "2026-01-05", -100_00, payee="Costco")
    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, model.row_for_txn(t))
    for e in list(dlg._lines):
        dlg._remove_line(e)
    dlg.add_line("Groceries", -100.00, "")   # full amount on a category line
    dlg.add_line("", 0.0, "")                 # second line, no amount yet
    assert not dlg.ok_btn.isEnabled()         # only one counting line so far
    # Pick a transfer account from the dropdown -> must re-check OK (defect B).
    dlg._lines[1]["cat"].setCurrentText("[Savings]")
    assert dlg.ok_btn.isEnabled()


def _wheel_event(delta=-120):
    """A synthetic one-notch vertical wheel event (down when delta<0)."""
    from PyQt5.QtGui import QWheelEvent
    from PyQt5.QtCore import QPoint, QPointF
    return QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0),
                       QPoint(0, delta), Qt.NoButton, Qt.NoModifier,
                       Qt.NoScrollPhase, False)


def test_register_category_combo_ignores_wheel(qapp, conn, accounts):
    """USER UX BUG (2026-08-16): scrolling the mouse wheel over a register
    category combo silently CHANGED the category -- dangerous while scrolling
    through splits. The register's category editor must ignore wheel events
    (so the register scrolls) while click/typing selection still works."""
    from PyQt5.QtWidgets import QComboBox, QStyleOptionViewItem
    from mammon.ui.delegates import CategoryDelegate, NoWheelComboBox

    chk, _ = accounts
    for c in ("Groceries", "Dining", "Utilities"):
        ledger.resolve_category(conn, c)
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    m = RegisterModel(conn, chk)
    delegate = CategoryDelegate()
    idx = m.index(0, RegisterModel.CATEGORY)
    combo = delegate.createEditor(None, QStyleOptionViewItem(), idx)
    assert isinstance(combo, NoWheelComboBox)
    assert combo.count() >= 3                       # blank + real category choices

    # Baseline: the SAME event flips a stock combo's value and is accepted --
    # proving the event is live and the hazard is real.
    plain = QComboBox()
    plain.addItems(["a", "b", "c", "d"])
    plain.setCurrentIndex(1)
    base_ev = _wheel_event(-120)
    plain.wheelEvent(base_ev)
    assert plain.currentIndex() == 2 and base_ev.isAccepted()

    # The register combo IGNORES the wheel: value unchanged, event not accepted
    # (so it propagates to the view and the register scrolls instead).
    combo.setCurrentIndex(1)
    before = combo.currentText()
    ev = _wheel_event(-120)
    combo.wheelEvent(ev)
    assert combo.currentText() == before
    assert not ev.isAccepted()
    # ... and the opposite scroll direction is equally inert.
    up = _wheel_event(120)
    combo.wheelEvent(up)
    assert combo.currentText() == before
    assert not up.isAccepted()

    # Dropdown / click selection STILL changes the value (only wheel is muted).
    combo.setCurrentIndex(2)
    assert combo.currentIndex() == 2
    picked = combo.itemText(2)
    combo.setCurrentText(picked)
    assert combo.currentText() == picked


def test_new_category_confirm_dialog(qapp, conn, accounts, monkeypatch):
    """Typing a category that does not exist must CONFIRM before creating it
    (typo guard). Cancel writes nothing and creates no category; an existing
    category (or a transfer target) never prompts.

    The prompt is DEFERRED one event-loop turn: raising a modal inside
    setModelData opens a nested loop while Qt is destroying the editor, which
    corrupted the heap (0xc0000374, no traceback). Hence the _settle() calls."""
    from PyQt5.QtWidgets import QMessageBox, QStyleOptionViewItem
    from mammon.ui.delegates import CategoryDelegate

    chk, _ = accounts
    ledger.resolve_category(conn, "Groceries")
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    m = RegisterModel(conn, chk)
    delegate = CategoryDelegate()
    idx = m.index(0, RegisterModel.CATEGORY)
    editor = delegate.createEditor(None, QStyleOptionViewItem(), idx)

    asked: list[str] = []
    answer = [QMessageBox.No]

    def fake_question(parent, title, text, *a, **k):
        asked.append(text)
        return answer[0]

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))

    # 1) unknown category -> prompt; user cancels -> nothing written/created.
    editor.setEditText("Vacaton")            # a typo
    delegate.setModelData(editor, m, idx)
    _settle()
    assert asked and "Vacaton" in asked[0]
    assert m.data(idx, Qt.DisplayRole) != "Vacaton"
    assert "Vacaton" not in m.category_choices()

    # 2) existing category -> NO prompt, commits normally.
    asked.clear()
    editor.setEditText("Groceries")
    delegate.setModelData(editor, m, idx)
    _settle()
    _settle()
    assert asked == []
    assert m.data(idx, Qt.DisplayRole) == "Groceries"

    # 3) unknown category, user confirms -> created and committed.
    asked.clear()
    answer[0] = QMessageBox.Yes
    editor.setEditText("Vacation")
    delegate.setModelData(editor, m, idx)
    _settle()
    _settle()
    assert asked and "Vacation" in asked[0]
    assert m.data(idx, Qt.DisplayRole) == "Vacation"


def test_split_dialog_combo_and_amount_ignore_wheel(qapp, conn, accounts):
    """The split editor's per-row category combo and amount spin box must also
    ignore the wheel, so scrolling the split list can't silently retag a row or
    change its amount."""
    from mammon.ui.delegates import NoWheelComboBox, NoWheelDoubleSpinBox
    from mammon.ui.widgets import SplitDialog

    chk, _ = accounts
    ledger.resolve_category(conn, "Groceries")
    ledger.resolve_category(conn, "Dining")
    t = ledger.add_transaction(conn, chk, "2026-01-05", -100_00, payee="Costco")
    m = RegisterModel(conn, chk)
    dlg = SplitDialog(m, m.row_for_txn(t))
    for e in list(dlg._lines):
        dlg._remove_line(e)
    dlg.add_line("Groceries", -60.00, "food")
    entry = dlg._lines[-1]
    cat, amt = entry["cat"], entry["amount"]
    assert isinstance(cat, NoWheelComboBox)
    assert isinstance(amt, NoWheelDoubleSpinBox)

    cat_before = cat.currentText()
    ev = _wheel_event(-120)
    cat.wheelEvent(ev)
    assert cat.currentText() == cat_before and not ev.isAccepted()

    amt_before = amt.value()
    ev2 = _wheel_event(-120)
    amt.wheelEvent(ev2)
    assert amt.value() == amt_before and not ev2.isAccepted()

    # Typing/programmatic edits still work (only the wheel is muted).
    cat.setCurrentText("Dining")
    assert cat.currentText() == "Dining"
    amt.setValue(-42.00)
    assert amt.value() == -42.00


def test_split_open_and_edit_transfer_linked_split(qapp, conn, accounts, monkeypatch):
    """USER BUG (2026-08-16): a Crossland Mortgage payment imported as BOTH a
    transfer to [House] AND a principal+interest split reads as --Split--, but
    clicking to open its split raised a false 'A transfer cannot be split.' and
    refused to open. The register's split-open guard and ledger.set_splits must
    permit editing the legs of a transfer-linked txn that already carries
    splits, preserving the parent transfer link and round-tripping the legs."""
    from mammon.ui import widgets
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import RegisterWidget, SplitDialog
    from PyQt5.QtWidgets import QDialog

    chk, _ = accounts
    house = ledger.create_account(conn, "House", "asset")
    int_exp = ledger.resolve_category(conn, "Int Exp")
    t = ledger.add_transaction(conn, chk, "2000-12-08", -829_56,
                               payee="Crossland Mortgage Corp")
    ledger.set_splits(conn, t, [
        {"transfer_account_id": house, "amount": -120_46, "memo": "principal"},
        {"category_id": int_exp, "amount": -709_10, "memo": "interest"},
    ])
    # Legacy import state: the parent row ALSO carries the [House] transfer link
    # (Quicken echoed it in L) -- this is what tripped the old open/edit guard.
    conn.execute("UPDATE transactions SET transfer_account_id=? WHERE id=?",
                 (house, t))
    conn.commit()
    assert ledger.get_transaction(conn, t)["transfer_account_id"] == house
    assert ledger.has_splits(conn, t)

    # Opening the split must NOT be refused: no "A transfer cannot be split."
    w = RegisterWidget(conn, chk)
    infos = []
    monkeypatch.setattr(widgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: infos.append(a)))
    monkeypatch.setattr(SplitDialog, "exec_", lambda self: QDialog.Rejected)
    w._split_row(w.model.row_for_txn(t))
    assert infos == []                                   # opened, not refused

    # Editing the legs round-trips through the dialog -> ledger.set_splits, and
    # the parent transfer link survives.
    dlg = SplitDialog(w.model, w.model.row_for_txn(t))
    assert [round(l["amount"].value(), 2) for l in dlg._lines] == [-120.46, -709.10]
    for e in list(dlg._lines):
        dlg._remove_line(e)
    dlg.add_line("[House]", -200.00, "principal")        # bump the principal leg
    dlg.add_line("Int Exp", -629.56, "interest")         # still sums to -829.56
    dlg._update_remainder()
    assert dlg.remainder_cents() == 0
    assert dlg.apply_split() is True

    row = ledger.get_transaction(conn, t)
    assert row["transfer_account_id"] == house           # transfer link preserved
    assert ledger.category_display(conn, row) == ledger.SPLIT_LABEL
    legs = {l["category_label"]: l["amount"] for l in ledger.get_splits(conn, t)}
    assert legs == {"[House]": -200_00, "Int Exp": -629_56}


def test_split_dialog_absorbs_unbalanced_into_uncategorized(qapp, conn, accounts):
    """NEW BEHAVIOR: an unbalanced split is no longer rejected. OK stays enabled,
    and on save the signed remainder is folded into an UNCATEGORIZED split line so
    the save is never blocked; the register then flags the leftover with a warning
    triangle before '--Split--'."""
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import SplitDialog
    from PyQt5.QtGui import QIcon

    chk, _ = accounts
    t = ledger.add_transaction(conn, chk, "2026-01-05", -100_00)
    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, model.row_for_txn(t))
    for e in list(dlg._lines):
        dlg._remove_line(e)
    dlg.add_line("Groceries", -60.00, "")
    dlg.add_line("Dining", -30.00, "")           # sums to -90, not -100
    dlg._update_remainder()
    assert dlg.remainder_cents() == -10_00
    assert dlg.ok_btn.isEnabled()                # NOT blocked -> remainder allowed
    assert dlg.apply_split() is True

    # The -10.00 remainder landed in an uncategorized line; the total is unchanged.
    legs = ledger.get_splits(conn, t)
    assert [(l["category_label"], l["amount"]) for l in legs] == [
        ("Groceries", -60_00), ("Dining", -30_00), ("", -10_00)]
    assert ledger.uncategorized_split_amount(conn, t) == -10_00
    assert ledger.get_transaction(conn, t)["amount"] == -100_00

    # The register cell reads '--Split--' AND carries a warning-triangle icon.
    model.reload()
    r = model.row_for_txn(t)
    idx = model.index(r, RegisterModel.CATEGORY)
    assert model.data(idx, Qt.DisplayRole) == "--Split--"
    deco = model.data(idx, Qt.DecorationRole)
    assert isinstance(deco, QIcon) and not deco.isNull()
    # A fully-covered split shows NO triangle.
    t2 = ledger.add_transaction(conn, chk, "2026-01-06", -50_00)
    ledger.set_splits(conn, t2, [
        {"category_id": ledger.resolve_category(conn, "Groceries"), "amount": -30_00},
        {"category_id": ledger.resolve_category(conn, "Dining"), "amount": -20_00}])
    model.reload()
    r2 = model.row_for_txn(t2)
    assert model.data(model.index(r2, RegisterModel.CATEGORY),
                      Qt.DecorationRole) is None


def test_split_dialog_edit_total_absorbs_difference(qapp, conn, accounts):
    """Editing the Total in the split editor is allowed: the signed difference vs
    the split-line sum is absorbed into an uncategorized line, and the
    transaction's own amount follows the new total."""
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import SplitDialog

    chk, _ = accounts
    t = ledger.add_transaction(conn, chk, "2026-01-05", -100_00, payee="Costco")
    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, model.row_for_txn(t))
    for e in list(dlg._lines):
        dlg._remove_line(e)
    dlg.add_line("Groceries", -60.00, "")
    dlg.add_line("Dining", -40.00, "")           # lines sum to -100
    dlg._update_remainder()
    assert dlg.remainder_cents() == 0
    # Bump the total to -130: the -30 difference must go to uncategorized.
    dlg.total_spin.setValue(-130.00)
    assert dlg.remainder_cents() == -30_00
    assert dlg.ok_btn.isEnabled()
    assert dlg.apply_split() is True

    assert ledger.get_transaction(conn, t)["amount"] == -130_00
    legs = ledger.get_splits(conn, t)
    assert [(l["category_label"], l["amount"]) for l in legs] == [
        ("Groceries", -60_00), ("Dining", -40_00), ("", -30_00)]
    assert ledger.uncategorized_split_amount(conn, t) == -30_00


def test_split_dialog_adj_button_snaps_total_to_lines(qapp, conn, accounts):
    """The 'Adj' button sets the Total to the current sum of the split lines,
    zeroing the remainder -- so an unbalanced split can be balanced by moving the
    total to the lines rather than the lines to the total."""
    from mammon.ui.models import RegisterModel
    from mammon.ui.widgets import SplitDialog

    chk, _ = accounts
    t = ledger.add_transaction(conn, chk, "2026-01-05", -100_00, payee="Costco")
    model = RegisterModel(conn, chk)
    dlg = SplitDialog(model, model.row_for_txn(t))
    for e in list(dlg._lines):
        dlg._remove_line(e)
    dlg.add_line("Groceries", -60.00, "")
    dlg.add_line("Dining", -30.00, "")           # sums to -90, remainder -10
    dlg._update_remainder()
    assert dlg.remainder_cents() == -10_00

    dlg._adjust_total_to_lines()                 # emulate clicking 'Adj'
    assert int(round(dlg.total_spin.value() * 100)) == -90_00
    assert dlg.remainder_cents() == 0
    assert dlg.apply_split() is True

    # Total followed the lines; no uncategorized leftover.
    assert ledger.get_transaction(conn, t)["amount"] == -90_00
    assert ledger.uncategorized_split_amount(conn, t) == 0
    legs = ledger.get_splits(conn, t)
    assert [(l["category_label"], l["amount"]) for l in legs] == [
        ("Groceries", -60_00), ("Dining", -30_00)]


def test_reconcile_two_pane_split_and_finish(qapp, conn, accounts):
    from mammon.ui.widgets import ReconcileDialog

    chk, _ = accounts                            # opening 100_00
    a = ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Store")
    b = ledger.add_transaction(conn, chk, "2026-01-10", 200_00, payee="Pay")

    dlg = ReconcileDialog(conn, chk)
    dlg.set_balances("2026-01-31", 100_00, 275_00)   # the three statement inputs
    # Debit lands in the LEFT (Payments and Checks) pane, credit in the RIGHT
    # (Deposits) pane, and both payees are filled.
    assert dlg.debits_table.rowCount() == 1
    assert dlg.credits_table.rowCount() == 1
    assert dlg.debits_table.item(0, ReconcileDialog.PAYEE).text() == "Store"
    assert dlg.credits_table.item(0, ReconcileDialog.PAYEE).text() == "Pay"
    # The debit shows its absolute amount in the Amount column (no signed clutter).
    assert dlg.debits_table.item(0, ReconcileDialog.AMOUNT).text() == "25.00"
    # Unbalanced until items are marked: difference is 175.00 -> no Finish.
    assert not dlg.finish_btn.isEnabled()
    dlg.mark_all()
    assert ledger.get_transaction(conn, a)["cleared"] == 1
    assert ledger.get_transaction(conn, b)["cleared"] == 1
    # Cleared balance now equals the statement -> difference zero -> Finish on.
    assert dlg.finish_btn.isEnabled()
    dlg._finish()
    assert dlg.finished_ok
    assert ledger.get_transaction(conn, a)["reconciled"] == 1
    assert ledger.get_transaction(conn, b)["reconciled"] == 1
    assert ledger.last_reconciliation(conn, chk)["statement_balance"] == 275_00


def test_reconcile_rejects_unbalanced_finish(qapp, conn, accounts):
    from mammon.ui.widgets import ReconcileDialog

    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Store")
    dlg = ReconcileDialog(conn, chk)
    dlg.set_balances("2026-01-31", 100_00, 500_00)   # wrong ending balance
    # Difference is nonzero, so Finish stays disabled -- the user can never
    # trigger a bad finish. (The domain guard is covered in test_ledger.)
    assert not dlg.finish_btn.isEnabled()
    assert ledger.last_reconciliation(conn, chk) is None


def test_reconcile_row_click_toggles_and_mark_clear_all(qapp, conn, accounts):
    from mammon.ui.widgets import ReconcileDialog

    chk, _ = accounts
    a = ledger.add_transaction(conn, chk, "2026-02-01", -10_00, payee="A")
    b = ledger.add_transaction(conn, chk, "2026-02-02", -20_00, payee="B")
    dlg = ReconcileDialog(conn, chk)
    dlg.set_balances("2026-02-28", 100_00, 70_00)
    # Clicking anywhere on a row toggles its cleared mark (no checkbox to hit),
    # and the Clr column shows a visible 'c'.
    dlg._toggle_at(dlg.debits_table, 0)
    assert ledger.get_transaction(conn, a)["cleared"] == 1
    assert dlg.debits_table.item(0, ReconcileDialog.CLR).text() == "c"
    dlg._toggle_at(dlg.debits_table, 0)
    assert ledger.get_transaction(conn, a)["cleared"] == 0
    assert dlg.debits_table.item(0, ReconcileDialog.CLR).text() == ""
    # Mark All / Clear All flip every shown row.
    dlg.mark_all()
    assert ledger.get_transaction(conn, a)["cleared"] == 1
    assert ledger.get_transaction(conn, b)["cleared"] == 1
    dlg.clear_all()
    assert ledger.get_transaction(conn, a)["cleared"] == 0
    assert ledger.get_transaction(conn, b)["cleared"] == 0


def test_reconcile_statement_date_filters_rows(qapp, conn, accounts):
    from mammon.ui.widgets import ReconcileDialog

    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-03-05", -10_00, payee="Before")
    ledger.add_transaction(conn, chk, "2026-03-25", -20_00, payee="After")
    dlg = ReconcileDialog(conn, chk)
    dlg.set_balances("2026-03-15", 100_00, 90_00)    # only rows on/before 03-15
    assert dlg.debits_table.rowCount() == 1
    assert dlg.debits_table.item(0, ReconcileDialog.PAYEE).text() == "Before"
    # Changing the statement date re-filters: the later transaction now appears.
    dlg.set_balances("2026-03-31", 100_00, 70_00)
    assert dlg.debits_table.rowCount() == 2


def test_reconcile_transfer_payee_and_balances_roundtrip(qapp, conn, accounts):
    from mammon.ui.widgets import ReconcileDialog, ReconcileStartDialog

    chk, sav = accounts
    # A transfer out of Checking carries no payee text -> the pane shows the
    # linked-account label (user: the old dialog left transfer payees blank).
    ledger.create_transfer(conn, chk, sav, "2026-04-10", 40_00)
    dlg = ReconcileDialog(conn, chk)
    dlg.set_balances("2026-04-30", 100_00, 60_00)
    assert dlg.debits_table.rowCount() == 1
    assert dlg.debits_table.item(0, ReconcileDialog.PAYEE).text() == "[Savings]"
    # The Balances dialog round-trips the three inputs verbatim.
    start = ReconcileStartDialog(dlg.statement_date, dlg.beginning_cents,
                                 dlg.ending_cents, account_name="Checking")
    v = start.values()
    assert v["date"] == "2026-04-30"
    assert v["beginning"] == 100_00
    assert v["ending"] == 60_00


# ---- credit-card reconcile variant (the classic desktop ledger) --------------------------
def test_credit_reconcile_setup_dialog_has_card_fields(qapp, conn):
    from mammon.ui.widgets import CreditReconcileStartDialog

    ledger.create_account(conn, "Visa", "credit", opening_balance=0)
    # The credit-card SETUP screen collects the card-statement figures (charges,
    # payments, credits, ending-OWED) plus a separate finance-charge box -- NOT
    # the bank statement's beginning/ending pair.
    dlg = CreditReconcileStartDialog(
        conn, "2026-01-31", 0, 0, 0, 0, 0, "Interest Exp", account_name="Visa")
    dlg.charges.setValue(50.00)
    dlg.payments.setValue(20.00)
    dlg.credits.setValue(1.00)
    dlg.ending.setValue(35.00)               # amount OWED, entered positive
    dlg.finance.setValue(5.00)
    assert dlg.values() == {
        "date": "2026-01-31", "charges": 50_00, "payments": 20_00,
        "credits": 1_00, "ending": 35_00, "finance_charge": 5_00,
        "finance_category": "Interest Exp"}


def test_credit_reconcile_flow_and_finance_charge(qapp, conn):
    from mammon.ui.widgets import ReconcileDialog

    cc = ledger.create_account(conn, "Visa", "credit", opening_balance=0)
    charge = ledger.add_transaction(conn, cc, "2026-01-05", -50_00, payee="Store")
    ledger.add_transaction(conn, cc, "2026-01-10", 20_00, payee="Payment")

    dlg = ReconcileDialog(conn, cc)
    assert dlg.is_credit                      # credit-card variant is selected
    dlg.set_credit_balances({
        "date": "2026-01-31", "charges": 50_00, "payments": 20_00, "credits": 0,
        "ending": 35_00, "finance_charge": 5_00, "finance_category": "Interest Exp"})

    # The finance charge is posted as a CLEARED charge (negative) on the
    # statement date, so it clears against the statement that already includes it.
    fc = ledger.get_transaction(conn, dlg._finance_charge_id)
    assert (fc["amount"], fc["cleared"], fc["date"], fc["payee"]) == \
        (-5_00, 1, "2026-01-31", "Finance Charge")
    # Charge + finance charge land LEFT (charges), payment lands RIGHT.
    assert dlg.debits_table.rowCount() == 2
    assert dlg.credits_table.rowCount() == 1
    # Ending-owed 35.00 is stored register-signed (owed => negative) as the target.
    assert dlg.ending_cents == -35_00
    # Live summary reads in credit-card terms and shows the three balances.
    assert "charges" in dlg.summary_label.text()
    assert "Cleared balance" in dlg.summary_label.text()
    assert "Statement ending" in dlg.summary_label.text()
    assert "Difference" in dlg.summary_label.text()
    # Unbalanced until the statement items clear; then difference hits zero.
    assert not dlg.finish_btn.isEnabled()
    dlg.mark_all()
    assert dlg.finish_btn.isEnabled()         # -50 + 20 - 5 == -35 target
    dlg._finish()
    assert dlg.finished_ok
    assert ledger.get_transaction(conn, charge)["reconciled"] == 1
    assert ledger.last_reconciliation(conn, cc)["statement_balance"] == -35_00


def test_credit_reconcile_finance_charge_idempotent(qapp, conn):
    from mammon.ui.widgets import ReconcileDialog

    cc = ledger.create_account(conn, "Visa", "credit", opening_balance=0)
    dlg = ReconcileDialog(conn, cc)
    base = {"date": "2026-02-28", "charges": 0, "payments": 0, "credits": 0,
            "ending": 0, "finance_charge": 5_00, "finance_category": "Interest Exp"}
    dlg.set_credit_balances(base)
    first = dlg._finance_charge_id
    assert ledger.get_transaction(conn, first)["amount"] == -5_00
    # Reopening 'Balances...' with a new amount UPDATES the same row (no dupe).
    dlg.set_credit_balances({**base, "finance_charge": 8_00})
    assert dlg._finance_charge_id == first
    assert ledger.get_transaction(conn, first)["amount"] == -8_00
    only = [r for r in ledger.register_rows(conn, cc) if r["payee"] == "Finance Charge"]
    assert len(only) == 1
    # A zero finance charge removes the posted transaction entirely.
    dlg.set_credit_balances({**base, "finance_charge": 0})
    assert dlg._finance_charge_id is None
    assert ledger.get_transaction(conn, first) is None


def test_banking_reconcile_keeps_start_end_variant(qapp, conn, accounts):
    from mammon.ui.widgets import ReconcileDialog

    chk, _ = accounts
    dlg = ReconcileDialog(conn, chk)
    # A non-credit account keeps the bank start/end flow (no credit branch).
    assert dlg.is_credit is False
    dlg.set_balances("2026-01-31", 100_00, 100_00)
    assert "deposits" in dlg.summary_label.text()


# ---- print register (Settings > Print Register) ----------------------------
def test_print_register_renders_pdf_and_menu_action(qapp, conn, accounts, tmp_path):
    from mammon.ui import printing
    from mammon.ui.widgets import MainWindow

    chk, _ = accounts                            # opening 100_00
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Store")
    ledger.add_transaction(conn, chk, "2026-01-10", 200_00, payee="Pay")

    rows = ledger.register_rows(conn, chk)
    bal = ledger.account_balance(conn, chk)
    html = printing.register_html("Checking", rows, bal,
                                  subtitle=f"{len(rows)} transactions")
    # The register's real amounts/balance reach the printout.
    assert "275.00" in html                      # ending balance 100 - 25 + 200
    assert "Ending Balance: $275.00" in html

    pdf = tmp_path / "register.pdf"
    printing.render_html_to_pdf(html, str(pdf), title="Checking")
    assert pdf.exists()
    data = pdf.read_bytes()
    assert data[:5] == b"%PDF-" and len(data) > 500   # a real, non-empty PDF

    # 'Print Register...' is wired into the Settings submenu.
    win = MainWindow(conn)
    settings = next(m.menu() for m in win.menuBar().actions()
                    if m.text().replace("&", "") == "Settings")
    labels = {a.text().replace("…", "").strip() for a in settings.actions()}
    assert "Print Register" in labels
    win.close()


# ---- two-line register rows + one/two-line toggle (P2i) ---------------------
def test_two_line_view_pref_roundtrip(qapp):
    # The display preference persists across reads (QSettings, isolated to tmp).
    from mammon.ui import prefs
    assert prefs.two_line_default() is False        # default = one-line
    prefs.set_two_line_default(True)
    assert prefs.two_line_default() is True
    prefs.set_two_line_default(False)
    assert prefs.two_line_default() is False


def test_two_line_model_second_line_role(qapp, conn, accounts):
    chk, _ = accounts
    groc = ledger.resolve_category(conn, "Groceries")
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway",
                           category_id=groc, memo="weekly shop", tag="Food")
    m = RegisterModel(conn, chk)
    payee = m.index(0, RegisterModel.PAYEE)
    # one-line mode: the Payee cell carries no second line
    assert m.data(payee, RegisterModel.SECOND_LINE_ROLE) == ""
    # two-line mode: category + memo + #tag render under the payee
    m.set_two_line(True)
    second = m.data(payee, RegisterModel.SECOND_LINE_ROLE)
    assert "Groceries" in second
    assert "weekly shop" in second
    assert "#Food" in second
    # only the Payee column has a second line; other columns and the blank row don't
    assert m.data(m.index(0, RegisterModel.DATE), RegisterModel.SECOND_LINE_ROLE) == ""
    blank = m.rowCount() - 1
    assert m.data(m.index(blank, RegisterModel.PAYEE),
                  RegisterModel.SECOND_LINE_ROLE) == ""


def test_payee_delegate_two_line_sizehint(qapp, conn, accounts):
    from PyQt5.QtWidgets import QStyleOptionViewItem
    from mammon.ui.delegates import PayeeTwoLineDelegate

    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    m = RegisterModel(conn, chk)
    d = PayeeTwoLineDelegate()
    opt = QStyleOptionViewItem()
    idx = m.index(0, RegisterModel.PAYEE)
    one = d.sizeHint(opt, idx).height()
    d.set_two_line(True)
    two = d.sizeHint(opt, idx).height()
    assert two > one            # the two-line row is roughly double height


def test_register_widget_two_line_toggle_and_inline_edit(qapp, conn, accounts):
    from mammon.ui.widgets import RegisterWidget

    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    w = RegisterWidget(conn, chk)
    R = RegisterModel

    # default is one-line: every column visible
    assert w.view_mode == "one"
    for col in (R.CATEGORY, R.TAG, R.MEMO):
        assert not w.view.isColumnHidden(col)
    one_h = w.view.verticalHeader().defaultSectionSize()

    # switch to two-line: Category/Tag/Memo collapse under the payee, rows grow
    w.set_view_mode("two")
    assert w.view_mode == "two"
    assert w.model.two_line is True
    assert w.payee_delegate.two_line is True
    for col in (R.CATEGORY, R.TAG, R.MEMO):
        assert w.view.isColumnHidden(col)
    for col in (R.DATE, R.NUM, R.PAYEE, R.PAYMENT, R.CLR, R.DEPOSIT, R.BALANCE):
        assert not w.view.isColumnHidden(col)
    assert w.view.verticalHeader().defaultSectionSize() > one_h

    # inline editing of line-1 fields still works in two-line mode
    assert w.model.setData(w.model.index(0, R.PAYEE), "Trader Joes", Qt.EditRole)
    _settle()
    assert w.model.txn_at(0)["payee"] == "Trader Joes"
    assert w.model.setData(w.model.index(0, R.PAYMENT), "40.00", Qt.EditRole)
    _settle()
    assert ledger.get_transaction(conn, w.model.txn_at(0)["id"])["amount"] == -40_00

    # back to one-line restores the columns and the row height
    w.set_view_mode("one")
    for col in (R.CATEGORY, R.TAG, R.MEMO):
        assert not w.view.isColumnHidden(col)
    assert w.view.verticalHeader().defaultSectionSize() == one_h


def test_category_delegate_commits_completion_on_enter(qapp, conn, accounts,
                                                       monkeypatch):
    """Commit-on-Enter bug: Enter/Return out of the category combo must accept
    the active completion (exactly like Tab) BEFORE the base class commits, so
    the picked category is saved instead of the half-typed prefix reverting."""
    from PyQt5.QtCore import QEvent
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtWidgets import QStyleOptionViewItem
    import mammon.ui.delegates as delegates

    chk, _ = accounts
    ledger.resolve_category(conn, "Groceries")
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    m = RegisterModel(conn, chk)
    delegate = delegates.CategoryDelegate()
    idx = m.index(0, RegisterModel.CATEGORY)
    combo = delegate.createEditor(None, QStyleOptionViewItem(), idx)

    accepted: list = []
    monkeypatch.setattr(delegates, "_accept_active_completion",
                        lambda e: accepted.append(e))

    # Return and Tab both accept the completion; an ordinary key does not.
    for key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
        accepted.clear()
        delegate.eventFilter(combo, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
        assert accepted == [combo]
    accepted.clear()
    delegate.eventFilter(combo, QKeyEvent(QEvent.KeyPress, Qt.Key_A, Qt.NoModifier))
    assert accepted == []


def test_inline_commit_keeps_selection(qapp, conn, accounts):
    """Keep-focus bug: pressing Enter to save an edited row snapped the register
    to the top and dropped the selection. After any inline commit the SAME
    transaction must stay selected (and therefore in view)."""
    from PyQt5.QtCore import QEvent as _QEvent  # noqa: F401 (import parity)
    from mammon.ui.widgets import RegisterWidget

    chk, _ = accounts
    for i in range(1, 8):
        ledger.add_transaction(conn, chk, f"2026-01-0{i}", -i * 100,
                               payee=f"Payee {i}")
    w = RegisterWidget(conn, chk)
    R = RegisterModel
    target_row = 3
    tid = w.model.txn_at(target_row)["id"]
    w.view.setCurrentIndex(w.model.index(target_row, R.PAYEE))

    # commit an inline edit on that row -> model reloads via begin/endResetModel
    assert w.model.setData(w.model.index(target_row, R.MEMO), "note", Qt.EditRole)

    cur = w.view.currentIndex()
    assert cur.isValid()
    assert w.model.txn_at(cur.row())["id"] == tid   # not snapped to row 0


def test_main_window_view_menu_toggles_registers(qapp, tmp_path):
    from mammon.app import sample_data
    from mammon.ui import prefs
    from mammon.ui.widgets import MainWindow

    conn = db.init_db(tmp_path / "view.db")
    sample_data(conn)
    win = MainWindow(conn)
    titles = {a.text().replace("&", "") for a in win.menuBar().actions()}
    assert "View" in titles

    accts = ledger.list_accounts(conn)
    reg = win.open_register(accts[0]["id"])
    # default follows the (isolated, unset -> one-line) preference
    assert reg.view_mode == "one"

    # the View menu flips every open register and persists the choice
    win._set_register_view_mode("two")
    assert win.register_view_mode == "two"
    assert reg.view_mode == "two"
    assert win._two_line_act.isChecked() and not win._one_line_act.isChecked()
    assert prefs.two_line_default() is True

    # a register opened afterwards inherits the current mode
    reg2 = win.open_register(accts[1]["id"])
    assert reg2.view_mode == "two"

    # toggling back returns to one-line everywhere
    win._set_register_view_mode("one")
    assert reg.view_mode == "one" and reg2.view_mode == "one"
    assert prefs.two_line_default() is False
    win.close()
    conn.close()


# ---- two-line REWORK: editable line 2 + two-line header (P2i, Task 29) ------
def test_two_line_header_labels_both_lines(qapp, conn, accounts):
    # Fix (a): the header must read for BOTH lines. In two-line mode the Payee
    # header carries the line-1 label AND the line-2 classification labels.
    chk, _ = accounts
    m = RegisterModel(conn, chk)
    assert m.headerData(RegisterModel.PAYEE, Qt.Horizontal, Qt.DisplayRole) == "Payee"
    m.set_two_line(True)
    h = m.headerData(RegisterModel.PAYEE, Qt.Horizontal, Qt.DisplayRole)
    assert "Payee" in h and "Category" in h and "Memo" in h and "Tag" in h
    assert "\n" in h                                   # genuinely two lines
    # the other line-1 columns keep their single label
    assert m.headerData(RegisterModel.DATE, Qt.Horizontal, Qt.DisplayRole) == "Date"


def test_two_line_second_line_zones_fill_width(qapp):
    # Fix (c) + the user's follow-up: category/memo/tag are three contiguous boxes
    # that COLLECTIVELY FILL the row over the extent of the Payee field.
    from PyQt5.QtCore import QPoint, QRect
    from mammon.ui.delegates import PayeeTwoLineDelegate

    d = PayeeTwoLineDelegate()
    d.set_two_line(True)
    rect = QRect(0, 0, 400, 40)
    zones = d.second_line_rects(rect)
    cat, memo, tag = zones["category"], zones["memo"], zones["tag"]
    # all in the lower half of the cell
    for z in (cat, memo, tag):
        assert z.top() >= rect.height() // 2
    # fill edge-to-edge: start at the cell's left, end at its right, contiguous
    assert cat.left() == rect.left()
    assert cat.right() + 1 == memo.left()
    assert memo.right() + 1 == tag.left()
    assert tag.right() == rect.right()
    # field_at maps a point to the field under it; the payee line returns None
    assert d.field_at(rect, cat.center()) == "category"
    assert d.field_at(rect, memo.center()) == "memo"
    assert d.field_at(rect, tag.center()) == "tag"
    assert d.field_at(rect, QPoint(20, 5)) is None      # top (payee) line


def test_two_line_header_boxes_align_with_row_boxes(qapp):
    # The header's Category/Memo/Tag boxes sit directly above each row's
    # second-line boxes (same geometry), and fill the Payee column width.
    from PyQt5.QtCore import QRect
    from mammon.ui.delegates import PayeeTwoLineDelegate, TwoLineHeaderView

    rect = QRect(0, 0, 400, 44)
    header = TwoLineHeaderView()
    header.set_two_line(True)
    hboxes = header.classification_boxes(rect)
    # header boxes fill the width contiguously
    assert hboxes["category"].left() == rect.left()
    assert hboxes["tag"].right() == rect.right()
    assert hboxes["category"].right() + 1 == hboxes["memo"].left()
    # and share x-geometry with the delegate's row boxes (so they line up)
    d = PayeeTwoLineDelegate()
    d.set_two_line(True)
    rboxes = d.second_line_rects(rect)
    for field in ("category", "memo", "tag"):
        assert hboxes[field].left() == rboxes[field].left()
        assert hboxes[field].width() == rboxes[field].width()


def test_two_line_edit_category_memo_tag_persists(qapp, conn, accounts):
    # Fix (b): editing a line-2 zone commits to THAT field (via the ledger),
    # not the payee -- the core of the rework.
    from PyQt5.QtWidgets import QComboBox, QLineEdit, QStyleOptionViewItem
    from mammon.ui.delegates import PayeeTwoLineDelegate

    chk, _ = accounts
    groc = ledger.resolve_category(conn, "Groceries")
    tid = ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway",
                                 category_id=groc, memo="old memo", tag="Old")
    m = RegisterModel(conn, chk)
    m.set_two_line(True)
    d = PayeeTwoLineDelegate()
    d.set_two_line(True)
    opt = QStyleOptionViewItem()
    idx = m.index(0, RegisterModel.PAYEE)

    # memo (a plain line edit)
    d._active_field = "memo"
    ed = d.createEditor(None, opt, idx)
    assert isinstance(ed, QLineEdit)
    d.setEditorData(ed, idx)
    assert ed.text() == "old memo"
    ed.setText("dinner out")
    d.setModelData(ed, m, idx)
    assert ledger.get_transaction(conn, tid)["memo"] == "dinner out"

    # tag (a plain line edit)
    idx = m.index(0, RegisterModel.PAYEE)
    d._active_field = "tag"
    ed = d.createEditor(None, opt, idx)
    ed.setText("Fun")
    d.setModelData(ed, m, idx)
    assert ledger.get_transaction(conn, tid)["tag"] == "Fun"

    # category (the editable combo)
    idx = m.index(0, RegisterModel.PAYEE)
    d._active_field = "category"
    ed = d.createEditor(None, opt, idx)
    assert isinstance(ed, QComboBox)
    ed.setEditText("Dining")
    d.setModelData(ed, m, idx)
    _settle()
    assert m.txn_at(0)["category_label"] == "Dining"
    # the payee is untouched by any of the line-2 edits
    assert ledger.get_transaction(conn, tid)["payee"] == "Safeway"


def test_two_line_payee_edit_still_routes_to_payee(qapp, conn, accounts):
    # Line 1 stays editable in two-line mode: with no line-2 field active the
    # delegate opens the default payee editor and commits to the payee.
    from PyQt5.QtWidgets import QLineEdit, QStyleOptionViewItem
    from mammon.ui.delegates import PayeeTwoLineDelegate

    chk, _ = accounts
    tid = ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    m = RegisterModel(conn, chk)
    m.set_two_line(True)
    d = PayeeTwoLineDelegate()
    d.set_two_line(True)
    opt = QStyleOptionViewItem()
    idx = m.index(0, RegisterModel.PAYEE)
    d._active_field = None                              # line 1 (payee)
    ed = d.createEditor(None, opt, idx)
    assert isinstance(ed, QLineEdit)
    ed.setText("Trader Joes")
    d.setModelData(ed, m, idx)
    assert ledger.get_transaction(conn, tid)["payee"] == "Trader Joes"


def test_two_line_transfer_category_retargetable_memo_editable(qapp, conn, accounts):
    # A plain two-sided transfer's line-2 category is now editable (it re-points
    # the mirror); memo stays editable too.
    from PyQt5.QtWidgets import QLineEdit, QStyleOptionViewItem, QComboBox
    from mammon.ui.delegates import PayeeTwoLineDelegate

    chk, sav = accounts
    ledger.create_transfer(conn, chk, sav, "2026-04-10", 40_00)
    m = RegisterModel(conn, chk)
    m.set_two_line(True)
    d = PayeeTwoLineDelegate()
    d.set_two_line(True)
    opt = QStyleOptionViewItem()
    idx = m.index(0, RegisterModel.PAYEE)

    d._active_field = "category"
    ed = d.createEditor(None, opt, idx)                 # transfer: retargetable now
    assert isinstance(ed, QComboBox)

    d._active_field = "memo"
    ed = d.createEditor(None, opt, idx)
    assert isinstance(ed, QLineEdit)
    ed.setText("moved to savings")
    d.setModelData(ed, m, idx)
    assert ledger.get_transaction(conn, m.txn_at(0)["id"])["memo"] == "moved to savings"


# ---- display preferences: colors + font (P2g, Task 22) ---------------------
def test_display_prefs_defaults_reproduce_current_look(qapp):
    # Out of the box the display preferences MUST equal today's appearance, so
    # nothing changes unless the user opts in (defaults sourced from style.py).
    from mammon.ui import prefs, style

    assert prefs.theme() == style.DEFAULT_THEME == "light"
    # Compared against the constants, not literals: the default font is chosen
    # per platform (Segoe UI on Windows, the system face on macOS, DejaVu on
    # Linux), so a hardcoded name here would fail the suite everywhere but one OS.
    assert prefs.font_family() == style.DEFAULT_FONT_FAMILY
    assert prefs.font_size() == style.DEFAULT_FONT_SIZE
    assert prefs.row_shading() is True
    assert prefs.alt_row_color() == style.ALT_ROW == "#f4f6f9"
    assert prefs.negative_color() == style.RED == "#c0392b"
    assert prefs.two_line_default() is False
    # the active theme state starts at those same (light) defaults
    assert style.theme() == "light"
    assert style.negative_color() == "#c0392b"
    assert style.alt_row_color() == "#f4f6f9"


def test_display_prefs_roundtrip(qapp):
    # Every preference persists and reads back (QSettings, isolated to tmp).
    from mammon.ui import prefs

    prefs.set_display_prefs({
        "theme": "dark",
        "font_family": "Consolas",
        "font_size": 12,
        "row_shading": False,
        "alt_row_color": "#101820",
        "negative_color": "#ff5555",
        "two_line": True,
        "date_format": "DD/MM/YYYY",
    })
    assert prefs.theme() == "dark"
    assert prefs.font_family() == "Consolas"
    assert prefs.font_size() == 12
    assert prefs.row_shading() is False
    assert prefs.alt_row_color() == "#101820"
    assert prefs.negative_color() == "#ff5555"
    assert prefs.two_line_default() is True
    assert prefs.date_format() == "DD/MM/YYYY"
    # display_prefs() bundles the whole set for apply_theme()
    d = prefs.display_prefs()
    assert d == {
        "theme": "dark",
        "font_family": "Consolas", "font_size": 12, "row_shading": False,
        "alt_row_color": "#101820", "negative_color": "#ff5555", "two_line": True,
        "date_format": "DD/MM/YYYY",
    }


def test_apply_theme_updates_active_colors_and_font(qapp):
    # apply_theme(values) makes the chosen colors the ACTIVE state that models
    # read live, injects the alt-row color into the QSS, and sets the app font.
    from mammon.ui import style

    saved_font = qapp.font()          # restore exactly, so no global leak
    saved_style = qapp.styleSheet()
    try:
        style.apply_theme(qapp, {
            "font_family": "Consolas", "font_size": 13, "row_shading": True,
            "alt_row_color": "#123456", "negative_color": "#abcdef",
        })
        assert style.negative_color() == "#abcdef"
        assert style.alt_row_color() == "#123456"
        assert "#123456" in qapp.styleSheet()
        assert qapp.font().family() == "Consolas"
        assert qapp.font().pointSize() == 13
        # a register model paints negative amounts in the active color
        from PyQt5.QtGui import QColor
        assert QColor(style.negative_color()) == QColor("#abcdef")
        # apply_theme(None) restores the default (current) look
        style.apply_theme(qapp, None)
        assert style.negative_color() == "#c0392b"
        assert style.alt_row_color() == "#f4f6f9"
    finally:
        style.apply_theme(qapp, None)     # reset active color state to default
        qapp.setFont(saved_font)          # restore exact prior app state
        qapp.setStyleSheet(saved_style)


def test_display_preferences_dialog_builds_and_values(qapp):
    # The dialog builds headless, seeds from the defaults, and round-trips edits.
    from mammon.ui import prefs
    from mammon.ui.widgets import DisplayPreferencesDialog

    dlg = DisplayPreferencesDialog()
    assert dlg.font_size.value() == prefs.DEFAULT_FONT_SIZE
    assert dlg.row_shading.isChecked() is True
    assert dlg.alt_row_btn.color() == prefs.DEFAULT_ALT_ROW_COLOR
    assert dlg.negative_btn.color() == prefs.DEFAULT_NEGATIVE_COLOR
    assert dlg.two_line.isChecked() is False

    # change some fields; values() reflects them
    dlg.font_size.setValue(11)
    dlg.row_shading.setChecked(False)
    dlg.negative_btn.set_color("#ff0000")
    dlg.two_line.setChecked(True)
    v = dlg.values()
    assert v["font_size"] == 11 and v["row_shading"] is False
    assert v["negative_color"] == "#ff0000" and v["two_line"] is True

    # Restore Defaults puts every field back to the current look
    dlg.restore_defaults()
    assert dlg.font_size.value() == prefs.DEFAULT_FONT_SIZE
    assert dlg.row_shading.isChecked() is True
    assert dlg.negative_btn.color() == prefs.DEFAULT_NEGATIVE_COLOR
    assert dlg.two_line.isChecked() is False


def test_main_window_applies_display_prefs_live(qapp, tmp_path):
    from mammon.app import sample_data
    from mammon.ui import prefs, style
    from mammon.ui.widgets import MainWindow

    conn = db.init_db(tmp_path / "prefs.db")
    sample_data(conn)
    win = MainWindow(conn)
    accts = ledger.list_accounts(conn)
    reg = win.open_register(accts[0]["id"])
    assert reg.view.alternatingRowColors() is True     # default shading on

    saved_font = qapp.font()          # restore exactly, so no global leak
    saved_style = qapp.styleSheet()
    try:
        # Settings menu exposes the Display Preferences action
        settings = next(m.menu() for m in win.menuBar().actions()
                        if m.text().replace("&", "") == "Settings")
        labels = {a.text().replace("…", "").strip() for a in settings.actions()}
        assert "Display Preferences" in labels

        # apply a changed set live: shading off, custom font + negative color,
        # and the two-line default flipped on
        win.apply_display_prefs({
            "font_family": "Consolas", "font_size": 12, "row_shading": False,
            "alt_row_color": "#f4f6f9", "negative_color": "#0000ff",
            "two_line": True,
        })
        assert reg.view.alternatingRowColors() is False
        assert reg.view.font().family() == "Consolas"
        assert reg.view_mode == "two"                  # default view flipped
        assert win._two_line_act.isChecked() is True
        assert style.negative_color() == "#0000ff"     # active neg color updated
    finally:
        style.apply_theme(qapp, None)                  # reset active colors
        qapp.setFont(saved_font)                       # restore exact prior state
        qapp.setStyleSheet(saved_style)
        win.close()
        conn.close()


# ---- dark mode (P2g-dark, Task 31) -----------------------------------------
def test_dark_theme_qss_is_dark_bg_light_fg():
    # The dark palette QSS paints a dark window with light text and a legible
    # bright-red negative color; the light palette stays the original look.
    from mammon.ui import style

    dark = style.build_qss(style.DARK)
    assert style.DARK["window"] == "#1e1f22"
    assert f"background: {style.DARK['window']}" in dark   # dark window fill
    assert "color: #e3e5e8" in dark                        # light foreground
    assert style.DARK["negative"] == "#ff6b6b"             # legible-on-dark red
    # the light default is unchanged (original classic look)
    light = style.build_qss(style.LIGHT)
    assert "background: #ffffff" in light
    assert style.LIGHT["negative"] == "#c0392b"


def test_shipped_stylesheet_parses_cleanly(qapp):
    """Every theme's QSS must be valid Qt CSS and apply without Qt reporting a
    parse error.

    Regression guard: DARK["extra"] was a plain (non-f) triple-quoted string
    whose ``{{``/``}}`` were never collapsed to single braces, so once it was
    interpolated verbatim into build_qss()'s f-string the dark theme shipped
    literal double braces -- invalid QSS that Qt rejected at startup with
    "Could not parse application stylesheet". Assert, per theme, that the
    generated QSS has balanced single braces (no literal '{{'/'}}') AND that
    applying it to the live QApplication emits no parse warning.
    """
    from mammon.ui import style
    from PyQt5.QtCore import qInstallMessageHandler
    from PyQt5.QtWidgets import QWidget

    themes = (("light", style.LIGHT), ("dark", style.DARK))

    for name, palette in themes:
        qss = style.build_qss(palette)
        assert "{{" not in qss, name + " QSS contains a literal double open-brace"
        assert "}}" not in qss, name + " QSS contains a literal double close-brace"
        assert qss.count("{") == qss.count("}"), name + " QSS has unbalanced braces"

    captured = []
    prev = qInstallMessageHandler(lambda mode, ctx, msg: captured.append(msg))
    try:
        for _name, palette in themes:
            qss = style.build_qss(palette)
            qapp.setStyleSheet(qss)
            w = QWidget()
            w.setStyleSheet(qss)
            w.ensurePolished()  # forces Qt to actually parse the sheet
            w.deleteLater()
    finally:
        qInstallMessageHandler(prev)
        qapp.setStyleSheet("")  # don't leak styling into other session tests
    assert not any("parse" in m.lower() for m in captured), (
        "Qt reported a stylesheet parse error: " + repr(captured)
    )


def test_theme_pref_roundtrip_and_theme_aware_color_defaults(qapp):
    from mammon.ui import prefs, style

    # default is light; unset colors default to the LIGHT palette
    assert prefs.theme() == "light"
    assert prefs.alt_row_color() == style.LIGHT["alt_row"]
    assert prefs.negative_color() == style.LIGHT["negative"]

    # switch to dark: unset colors now default to the DARK palette
    prefs.set_theme("dark")
    assert prefs.theme() == "dark"
    assert prefs.alt_row_color() == style.DARK["alt_row"] == "#3b3e43"
    # ...and it must differ from the unshaded row, or the shading does nothing.
    assert style.DARK["alt_row"] != style.DARK["base_row"]
    assert prefs.negative_color() == style.DARK["negative"] == "#ff6b6b"

    # an explicit pick overrides the theme default
    prefs.set_display_prefs({"negative_color": "#00ffaa"})
    assert prefs.negative_color() == "#00ffaa"
    # bogus theme value falls back to light
    prefs.set_theme("chartreuse")
    assert prefs.theme() == "light"


def test_each_theme_remembers_its_own_colors(qapp):
    # A theme-sensitive color picked in one theme must NOT bleed into or be
    # overwritten by the other: light and dark each keep their own alt-row and
    # negative colors, so a dark -> light -> dark round trip returns the pick made
    # under dark (the reported defect: switching themes lost the alt-row color).
    from mammon.ui import prefs, style

    # (1) pick an explicit alt-row color while DARK is active
    prefs.set_theme("dark")
    prefs.set_display_prefs({"alt_row_color": "#123456"})
    assert prefs.alt_row_color() == "#123456"

    # switch to light: its own (unset) slot -> the light palette default, not dark's
    prefs.set_theme("light")
    assert prefs.alt_row_color() == style.LIGHT["alt_row"]

    # switch back to dark: the dark pick survived the round trip
    prefs.set_theme("dark")
    assert prefs.alt_row_color() == "#123456"

    # (2) symmetric case: a light pick is remembered independently of dark's
    prefs.set_theme("light")
    prefs.set_display_prefs({"alt_row_color": "#eeddcc"})
    assert prefs.alt_row_color() == "#eeddcc"
    prefs.set_theme("dark")
    assert prefs.alt_row_color() == "#123456"      # dark keeps its own value
    prefs.set_theme("light")
    assert prefs.alt_row_color() == "#eeddcc"      # light keeps its own value

    # (3) negative_color is per-theme too
    prefs.set_theme("dark")
    prefs.set_display_prefs({"negative_color": "#00ff00"})
    prefs.set_theme("light")
    prefs.set_display_prefs({"negative_color": "#ff00ff"})
    assert prefs.negative_color() == "#ff00ff"
    prefs.set_theme("dark")
    assert prefs.negative_color() == "#00ff00"

    # a color chosen in the SAME call that changes the theme lands in the new
    # theme's slot (the dialog reseeds swatches to the new theme before saving)
    prefs.set_display_prefs({"theme": "light", "alt_row_color": "#0a0b0c"})
    assert prefs.theme() == "light"
    assert prefs.alt_row_color() == "#0a0b0c"       # written to light's slot
    prefs.set_theme("dark")
    assert prefs.alt_row_color() == "#123456"       # dark's slot untouched


def test_legacy_single_color_key_migrates_into_active_theme(qapp):
    # An existing user's pre-namespacing single-key pick must survive the
    # per-theme split: it migrates into whatever theme is active on first read,
    # and never leaks into the other theme.
    from PyQt5.QtCore import QSettings
    from mammon.ui import prefs, style

    s = QSettings(QSettings.IniFormat, QSettings.UserScope, "Mammon", "Mammon")
    s.setValue(prefs._THEME_KEY, "dark")
    s.setValue(prefs._ALT_ROW_COLOR_KEY, "#abcdef")     # legacy single key
    s.sync()

    # first read while dark is active adopts the legacy value...
    assert prefs.alt_row_color() == "#abcdef"
    # ...and it did not leak into light, which keeps its palette default
    prefs.set_theme("light")
    assert prefs.alt_row_color() == style.LIGHT["alt_row"]
    # the dark slot retained the migrated value
    prefs.set_theme("dark")
    assert prefs.alt_row_color() == "#abcdef"


def test_apply_theme_dark_sets_dark_palette(qapp):
    from mammon.ui import prefs, style

    saved_font = qapp.font()
    saved_style = qapp.styleSheet()
    try:
        # the real flow: theme persisted -> display_prefs() defaults the colors
        # to the dark palette -> apply_theme adopts them
        prefs.set_theme("dark")
        style.apply_theme(qapp, prefs.display_prefs())
        assert style.theme() == "dark"
        assert style.negative_color() == style.DARK["negative"]
        assert style.text_color() == style.DARK["text"]
        assert style.accent_color() == style.DARK["blue"]
        assert style.balance_text_color() == style.DARK["balance_text"]
        assert f"background: {style.DARK['window']}" in qapp.styleSheet()
        # apply_theme(None) restores the light look
        style.apply_theme(qapp, None)
        assert style.theme() == "light"
        assert style.negative_color() == "#c0392b"
        assert style.text_color() == style.LIGHT["text"]
    finally:
        style.apply_theme(qapp, None)
        qapp.setFont(saved_font)
        qapp.setStyleSheet(saved_style)


def test_display_preferences_dialog_theme_switch_reseeds_colors(qapp):
    from mammon.ui import style
    from mammon.ui.widgets import DisplayPreferencesDialog

    dlg = DisplayPreferencesDialog()
    # opens on Light with the light color defaults
    assert dlg.theme.currentData() == "light"
    assert dlg.alt_row_btn.color() == style.LIGHT["alt_row"]
    assert dlg.negative_btn.color() == style.LIGHT["negative"]

    # selecting Dark reseeds both swatches to the dark palette and values() carries it
    dlg.theme.setCurrentIndex(1)
    assert dlg.theme.currentData() == "dark"
    assert dlg.alt_row_btn.color() == style.DARK["alt_row"]
    assert dlg.negative_btn.color() == style.DARK["negative"]
    assert dlg.values()["theme"] == "dark"

    # Restore Defaults returns to Light + the light look
    dlg.restore_defaults()
    assert dlg.theme.currentData() == "light"
    assert dlg.negative_btn.color() == style.LIGHT["negative"]


def test_main_window_applies_dark_theme_live(qapp, tmp_path):
    from mammon.app import sample_data
    from mammon.ui import style
    from mammon.ui.widgets import MainWindow

    conn = db.init_db(tmp_path / "dark.db")
    sample_data(conn)
    win = MainWindow(conn)
    accts = ledger.list_accounts(conn)
    reg = win.open_register(accts[0]["id"])

    saved_font = qapp.font()
    saved_style = qapp.styleSheet()
    try:
        win.apply_display_prefs({
            "theme": "dark", "font_family": "Segoe UI", "font_size": 9,
            "row_shading": True,
            "alt_row_color": style.DARK["alt_row"],
            "negative_color": style.DARK["negative"],
            "two_line": False,
        })
        assert style.theme() == "dark"
        assert style.negative_color() == style.DARK["negative"]
        assert f"background: {style.DARK['window']}" in qapp.styleSheet()
        # the register still renders under the dark theme
        reg.view.grab()
    finally:
        style.apply_theme(qapp, None)
        qapp.setFont(saved_font)
        qapp.setStyleSheet(saved_style)
        win.close()
        conn.close()


def test_two_line_register_follows_dark_theme(qapp, conn, accounts):
    # The two-line delegate + custom header paint via the ACTIVE palette
    # accessors, so line 2 and its header follow dark mode (no light band).
    from mammon.ui import style
    from mammon.ui.widgets import RegisterWidget

    chk, _ = accounts
    saved_font = qapp.font()
    saved_style = qapp.styleSheet()
    try:
        style.apply_theme(qapp, {
            "theme": "dark", "font_family": "Segoe UI", "font_size": 9,
            "row_shading": True, "alt_row_color": style.DARK["alt_row"],
            "negative_color": style.DARK["negative"],
        })
        # the accessors the two-line delegate/header read are now dark
        assert style.muted_color() == style.DARK["muted"]
        assert style.line_color() == style.DARK["line"]
        assert style.text_color() == style.DARK["text"]
        assert style.header_bg_color() == style.DARK["header_bg"]
        w = RegisterWidget(conn, chk)
        w.set_view_mode("two")
        w.resize(800, 300)
        w.view.grab()                            # renders line-2 boxes under dark
        w.view.horizontalHeader().grab()         # renders the two-line header
        w.deleteLater()
    finally:
        style.apply_theme(qapp, None)
        qapp.setFont(saved_font)
        qapp.setStyleSheet(saved_style)


def test_register_cell_text_follows_dark_theme(qapp, conn, accounts):
    # the user's dark-mode report (data/darkModeActual.png): the register's item text
    # -- one-line rows AND the two-line payee's FIRST line -- rendered near-black
    # on the dark background, because a QTableView delegate paints item text from
    # the widget PALETTE, which the QSS 'color' rule does not reach. The models
    # now supply an explicit cell-text color via ForegroundRole: the dark theme's
    # light text in dark mode, and None in light mode (so the light look is
    # byte-for-byte unchanged). Negative balances stay red, the Clr glyph green.
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QBrush, QColor
    from mammon.ui import style

    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    m = RegisterModel(conn, chk)
    payee = m.index(0, RegisterModel.PAYEE)
    date = m.index(0, RegisterModel.DATE)
    payment = m.index(0, RegisterModel.PAYMENT)
    balance = m.index(0, RegisterModel.BALANCE)          # 75.00 after -25 opening 100
    clr = m.index(0, RegisterModel.CLR)
    blank_payee = m.index(len(m._rows), RegisterModel.PAYEE)

    saved_font = qapp.font()
    saved_style = qapp.styleSheet()
    try:
        # LIGHT: no explicit cell-text color -> ordinary cells keep the palette
        # default, so the register renders exactly as it does today.
        style.apply_theme(qapp, None)
        assert style.cell_text_color() is None
        assert m.data(payee, Qt.ForegroundRole) is None
        assert m.data(date, Qt.ForegroundRole) is None
        assert m.data(payment, Qt.ForegroundRole) is None

        # DARK: every ordinary cell -- and the blank quick-entry row -- paints in
        # the dark theme's light text color, so the register is legible.
        style.apply_theme(qapp, {
            "theme": "dark", "font_family": "Segoe UI", "font_size": 9,
            "row_shading": True, "alt_row_color": style.DARK["alt_row"],
            "negative_color": style.DARK["negative"],
        })
        assert style.cell_text_color() == style.DARK["text"]
        light = QColor(style.DARK["text"]).name()
        for idx in (payee, date, payment, balance, blank_payee):
            brush = m.data(idx, Qt.ForegroundRole)
            assert isinstance(brush, QBrush)
            assert brush.color().name() == light
        # a NEGATIVE balance still overrides to the red; the Clr glyph stays green
        ledger.add_transaction(conn, chk, "2026-01-06", -200_00, payee="Rent")
        m2 = RegisterModel(conn, chk)
        neg_bal = m2.index(1, RegisterModel.BALANCE)     # 75 - 200 = -125.00
        assert m2.data(neg_bal, Qt.ForegroundRole).color().name() == \
            QColor(style.DARK["negative"]).name()
        assert m.data(clr, Qt.ForegroundRole).color().name() == QColor("#2e7d32").name()
    finally:
        style.apply_theme(qapp, None)
        qapp.setFont(saved_font)
        qapp.setStyleSheet(saved_style)


def test_dark_theme_sets_palette_and_button_text(qapp):
    # the user's report: in dark mode the inline field editors, the Split dialog, and
    # the buttons showed invisible (dark-on-dark) text -- because apply_theme set
    # only a stylesheet, so widgets that draw text from the PALETTE (button labels,
    # in-cell editors, QTableWidget items, dialog fields) fell back to near-black.
    # apply_theme now installs a full dark palette (plus an explicit QPushButton
    # color in the QSS), and light restores the captured default palette so the
    # light look is byte-for-byte unchanged.
    from PyQt5.QtGui import QPalette
    from mammon.ui import style

    saved_font = qapp.font()
    saved_style = qapp.styleSheet()
    saved_palette = QPalette(qapp.palette())
    try:
        style.apply_theme(qapp, None)                       # light baseline
        light_text = qapp.palette().color(QPalette.WindowText).name()
        light_btn = qapp.palette().color(QPalette.ButtonText).name()
        assert light_text != style.DARK["text"]             # light is not dark-mode text

        style.apply_theme(qapp, {
            "theme": "dark", "font_family": "Segoe UI", "font_size": 9,
            "row_shading": True, "alt_row_color": style.DARK["alt_row"],
            "negative_color": style.DARK["negative"],
        })
        pal = qapp.palette()
        # button labels, window text, and editor text are all the light dark color
        assert pal.color(QPalette.ButtonText).name() == style.DARK["text"]
        assert pal.color(QPalette.WindowText).name() == style.DARK["text"]
        assert pal.color(QPalette.Text).name() == style.DARK["text"]
        assert pal.color(QPalette.Window).name() == style.DARK["window"]
        assert pal.color(QPalette.Base).name() == "#2b2d31"
        # and the QSS spells out button text so a stylesheet-styled button is legible
        qss = qapp.styleSheet()
        assert "QPushButton {" in qss and "color: #e3e5e8" in qss
        assert "QDialogButtonBox QPushButton" in qss

        # back to light -> the ORIGINAL palette is restored (not dark text)
        style.apply_theme(qapp, None)
        assert qapp.palette().color(QPalette.WindowText).name() == light_text
        assert qapp.palette().color(QPalette.ButtonText).name() == light_btn
    finally:
        style.apply_theme(qapp, None)
        qapp.setFont(saved_font)
        qapp.setStyleSheet(saved_style)
        qapp.setPalette(saved_palette)


def test_split_dialog_widgets_are_legible_in_dark(qapp, conn, accounts):
    # A concrete check that the palette fix reaches real dialog widgets: build a
    # SplitDialog in dark mode and confirm its buttons and editable fields resolve
    # to the light dark-mode text color (they would be dark-on-dark under a
    # stylesheet-only theme).
    from PyQt5.QtGui import QPalette
    from mammon.ui import style
    from mammon.ui.widgets import SplitDialog

    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -60_00, payee="Costco")
    m = RegisterModel(conn, chk)

    saved_font = qapp.font()
    saved_style = qapp.styleSheet()
    saved_palette = QPalette(qapp.palette())
    try:
        style.apply_theme(qapp, {
            "theme": "dark", "font_family": "Segoe UI", "font_size": 9,
            "row_shading": True, "alt_row_color": style.DARK["alt_row"],
            "negative_color": style.DARK["negative"],
        })
        dlg = SplitDialog(m, 0)
        light = style.DARK["text"]
        # OK is disabled until the split balances, so check the ACTIVE group's
        # button text (an enabled button paints with it) -- it is the light color.
        assert dlg.ok_btn.palette().color(
            QPalette.Active, QPalette.ButtonText).name() == light
        assert dlg.palette().color(QPalette.WindowText).name() == light
        # an editable category combo + its line edit + the memo field read light text
        line = dlg._lines[0]
        assert line["cat"].palette().color(QPalette.Text).name() == light
        assert line["memo"].palette().color(QPalette.Text).name() == light
        assert line["amount"].palette().color(QPalette.Text).name() == light
        dlg.deleteLater()
    finally:
        style.apply_theme(qapp, None)
        qapp.setFont(saved_font)
        qapp.setStyleSheet(saved_style)
        qapp.setPalette(saved_palette)


def test_reconcile_and_search_dialogs_legible_in_dark(qapp, conn, accounts):
    # the user asked to confirm the OTHER dialogs (reconcile, search) also pick up the
    # dark theme. With the app palette installed in dark mode (Task 33) they do:
    # buttons, input fields, labels, and table cells all resolve to light text.
    # The only deliberately-colored bits are the green 'c' cleared marker and the
    # green/red reconcile summary, which stay readable on dark.
    from PyQt5.QtGui import QColor, QPalette
    from PyQt5.QtWidgets import QPushButton
    from mammon.ui import style
    from mammon.ui.widgets import ReconcileDialog, SearchDialog

    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Safeway")
    ledger.add_transaction(conn, chk, "2026-01-06", 900_00, payee="Paycheck")

    saved_font = qapp.font()
    saved_style = qapp.styleSheet()
    saved_palette = QPalette(qapp.palette())
    try:
        style.apply_theme(qapp, {
            "theme": "dark", "font_family": "Segoe UI", "font_size": 9,
            "row_shading": True, "alt_row_color": style.DARK["alt_row"],
            "negative_color": style.DARK["negative"],
        })
        light = style.DARK["text"]

        # ---- Search dialog ----
        sdlg = SearchDialog(conn, default_account_id=chk)
        assert sdlg.palette().color(QPalette.WindowText).name() == light
        assert sdlg.query.palette().color(QPalette.Text).name() == light      # search box
        assert sdlg.view.palette().color(QPalette.Text).name() == light       # results
        for b in sdlg.findChildren(QPushButton):
            assert b.palette().color(QPalette.Active, QPalette.ButtonText).name() == light
        sdlg.deleteLater()

        # ---- Reconcile dialog ----
        rec = ReconcileDialog(conn, chk)
        assert rec.palette().color(QPalette.WindowText).name() == light
        # data cells render from the (dark) table palette -> light text
        assert rec.debits_table.palette().color(QPalette.Text).name() == light
        assert rec.credits_table.palette().color(QPalette.Text).name() == light
        # action + Finish buttons paint light in their active state
        for b in (rec.mark_all_btn, rec.clear_all_btn, rec.balances_btn, rec.finish_btn):
            assert b.palette().color(QPalette.Active, QPalette.ButtonText).name() == light
        # the 'c' cleared marker stays green (deliberate), still legible on dark
        rec.debits_table.setRowCount(1)
        rec._fill_pane(rec.debits_table, rec._debits[:1], payment=True)
        if rec._debits:
            clr_item = rec.debits_table.item(0, ReconcileDialog.CLR)
            assert clr_item.foreground().color().name() == QColor("#2e7d32").name()
        rec.deleteLater()
    finally:
        style.apply_theme(qapp, None)
        qapp.setFont(saved_font)
        qapp.setStyleSheet(saved_style)
        qapp.setPalette(saved_palette)


# ---- GUI fixes from the user's 2026-08-11 feedback ------------------------------
def test_menu_selected_style_is_high_contrast():
    # The blanket 'QWidget {background:#fff}' rule used to leave a highlighted
    # menu item drawing its text in the same color as its background (it
    # vanished on hover). The theme now gives menus an explicit selection.
    from mammon.ui import style
    qss = style.QUICKEN_QSS
    assert "QMenuBar::item:selected" in qss
    assert "QMenu::item:selected" in qss
    # the drop-down selection is white text on the blue fill (high contrast)
    assert f"background: {style.BLUE}" in qss
    assert "color: #ffffff" in qss


def test_register_title_is_boxed(qapp, conn, accounts):
    from PyQt5.QtWidgets import QFrame, QLabel
    from mammon.ui.widgets import RegisterWidget

    chk, _ = accounts
    w = RegisterWidget(conn, chk)
    box = w.findChild(QFrame, "registerTitleBox")
    assert box is not None                       # the account title sits in a box
    assert w.header.parent() is box              # ...and the title label is inside it
    assert isinstance(w.header, QLabel)
    assert w.header.text() == "Checking"         # the box shows the account name


def test_search_dialog_is_modeless_and_dense(qapp, conn, accounts):
    from mammon.ui.widgets import SearchDialog

    dlg = SearchDialog(conn)
    # Modeless so the register stays usable beside it (by request).
    assert dlg.isModal() is False
    # Dense: tight rows (about one line of text) and a smaller results font.
    vh = dlg.view.verticalHeader()
    assert vh.defaultSectionSize() <= dlg.view.fontMetrics().height() + 4
    assert dlg.view.fontMetrics().height() <= dlg.query.fontMetrics().height()


def test_find_dialog_refreshes_when_an_edit_drops_a_match(qapp, tmp_path):
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow

    conn = db.init_db(tmp_path / "findlive.db")
    sample_data(conn)
    win = MainWindow(conn)
    first = ledger.list_accounts(conn)[0]["id"]
    win._current_account = first
    tid = ledger.add_transaction(conn, first, "2026-06-01", -77_00,
                                 payee="Zzyzx Diner")

    # Open the modeless Find dialog and search the distinctive payee.
    win._find_transactions_dialog()
    dlg = win._find_dialog
    assert dlg is not None
    i = dlg.scope.findData(first)
    dlg.scope.setCurrentIndex(i)
    dlg.query.setText("zzyzx")
    hits = dlg.run_search()
    assert [h["id"] for h in hits] == [tid]

    # Rename the payee in the register: committing the edit refreshes the open
    # Find dialog, and the row (no longer matching "zzyzx") drops out live.
    reg = win.open_register(first)
    row = reg.model.row_for_txn(tid)
    reg.model.setData(reg.model.index(row, RegisterModel.PAYEE),
                      "Corner Cafe", Qt.EditRole)
    _settle()          # the 'committed' signal the dialog listens for is deferred
    assert dlg.results.rowCount() == 0
    win.close()
    conn.close()


# ---- investment register (Task 45): investment accounts show their txns -----
def _seed_investment_account(conn, name="Brokerage"):
    """An investment account with a Buy, a cash Div, and a partial Sell (with a
    commission) -- enough to exercise every investment-register column."""
    from mammon import investments
    acct = ledger.create_account(conn, name, "investment")
    investments.record_investment(conn, acct, "2020-01-05", "Buy", symbol="AAPL",
                                  quantity="10", price="100", amount=-1_000_00)
    investments.record_investment(conn, acct, "2020-06-01", "Div", symbol="AAPL",
                                  amount=25_00)
    investments.record_investment(conn, acct, "2020-07-01", "Sell", symbol="AAPL",
                                  quantity="4", price="120", amount=480_00,
                                  commission=5_00)
    investments.rebuild_holdings(conn, acct)
    return acct


def test_investment_register_model_shape(qapp, conn):
    from PyQt5.QtGui import QBrush
    from mammon.ui.models import InvestmentRegisterModel as M
    acct = _seed_investment_account(conn)
    m = M(conn, acct)

    # Quicken investment columns (our Quantity/Price split stands in for Quicken's
    # combined 'Description'), plus the running Share Bal / Cash Amt / Cash Bal.
    assert m.columnCount() == 9
    headers = [m.headerData(c, Qt.Horizontal, Qt.DisplayRole) for c in range(9)]
    assert headers == ["Date", "Action", "Security / Category", "Quantity",
                       "Price", "Share Bal", "Inv Amt", "Cash Amt", "Cash Bal"]
    # read-only: one row per investment transaction, NO blank quick-entry row
    assert m.rowCount() == 3
    for c in range(9):
        assert not (m.flags(m.index(0, c)) & Qt.ItemIsEditable)

    def cell(r, c):
        return m.data(m.index(r, c), Qt.DisplayRole)

    # row 0 = the Buy: running Share Bal 10, Inv Amt 1,000.00, Cash Amt/Bal -1,000.00
    assert cell(0, M.DATE) == "01/05/2020"
    assert cell(0, M.ACTION) == "Buy"
    assert cell(0, M.SECURITY) == "AAPL"
    assert cell(0, M.QUANTITY) == "10"
    assert cell(0, M.PRICE) == "100"
    assert cell(0, M.SHARE_BAL) == "10"
    assert cell(0, M.INV_AMT) == "1,000.00"
    assert cell(0, M.CASH_AMT) == "-1,000.00"
    assert cell(0, M.CASH_BAL) == "-1,000.00"
    # row 1 = a cash Div: no shares moved -> Share Bal + Inv Amt blank, cash rises
    assert cell(1, M.ACTION) == "Div"
    assert cell(1, M.SHARE_BAL) == ""
    assert cell(1, M.INV_AMT) == ""
    assert cell(1, M.CASH_AMT) == "25.00"
    assert cell(1, M.CASH_BAL) == "-975.00"
    # row 2 = the Sell: Share Bal drops 10 -> 6, cash rises by the proceeds
    assert cell(2, M.ACTION) == "Sell"
    assert cell(2, M.SHARE_BAL) == "6"
    assert cell(2, M.INV_AMT) == "480.00"
    assert cell(2, M.CASH_AMT) == "480.00"
    assert cell(2, M.CASH_BAL) == "-495.00"

    # numeric columns right-aligned; a negative Cash Bal paints red (a QBrush)
    align = m.data(m.index(0, M.CASH_BAL), Qt.TextAlignmentRole)
    assert int(align) == int(Qt.AlignRight | Qt.AlignVCenter)
    assert isinstance(m.data(m.index(0, M.CASH_BAL), Qt.ForegroundRole), QBrush)


def test_investment_register_inline_transfer_and_category(qapp, conn):
    """Display feedback: a cash line shows WHERE it goes. A transfer (XIn) reads
    as the counter-account in brackets in the (renamed) Security/Category column,
    and a bare 'Cash' line reads as MiscInc carrying its category -- while a real
    security trade keeps showing its symbol there. Action + Security/Category read
    across together, e.g. 'XIn  [Anytown CU Ck]'."""
    from mammon import investments
    from mammon.ui.models import InvestmentRegisterModel as M

    bank = ledger.create_account(conn, "Anytown CU Ck", "checking")
    acct = ledger.create_account(conn, "Brokerage", "investment")
    # a security trade, a cash transfer IN from the bank, and a bare cash MiscInc
    investments.record_investment(conn, acct, "2020-01-05", "Buy", symbol="AAPL",
                                  quantity="10", price="100", amount=-1_000_00)
    investments.record_investment(conn, acct, "2020-02-01", "XIn", amount=500_00,
                                  transfer_account_id=bank)
    investments.record_investment(conn, acct, "2020-03-01", "Cash", amount=12_34,
                                  memo="Interest")
    m = M(conn, acct)

    def cell(r, c):
        return m.data(m.index(r, c), Qt.DisplayRole)

    # security row: symbol in the Security/Category column, action verbatim
    assert cell(0, M.ACTION) == "Buy"
    assert cell(0, M.SECURITY) == "AAPL"

    # transfer row: action stays 'XIn'; the destination account shows inline as
    # '[Anytown CU Ck]' -- so the row reads 'XIn  [Anytown CU Ck]'
    assert cell(1, M.ACTION) == "XIn"
    assert cell(1, M.SECURITY) == "[Anytown CU Ck]"
    # ...and being a bare cash transfer it moves no shares (Share Bal blank)
    assert cell(1, M.SHARE_BAL) == ""

    # cash row: 'Cash' reads as MiscInc and carries its category inline
    assert cell(2, M.ACTION) == "MiscInc"
    assert cell(2, M.SECURITY) == "Interest"


def test_investment_register_xout_and_miscexp(qapp, conn):
    """XOut mirrors XIn (transfer account inline), and a NEGATIVE bare cash line --
    a fee -- reads as MiscExp (an expense), not MiscInc."""
    from mammon import investments
    from mammon.ui.models import InvestmentRegisterModel as M

    bank = ledger.create_account(conn, "Anytown CU Ck", "checking")
    acct = ledger.create_account(conn, "Brokerage", "investment")
    investments.record_investment(conn, acct, "2020-02-01", "XOut", amount=-200_00,
                                  transfer_account_id=bank)
    investments.record_investment(conn, acct, "2020-03-01", "Cash", amount=-9_00,
                                  memo="Administration Fees")
    m = M(conn, acct)

    def cell(r, c):
        return m.data(m.index(r, c), Qt.DisplayRole)

    assert cell(0, M.ACTION) == "XOut"
    assert cell(0, M.SECURITY) == "[Anytown CU Ck]"
    assert cell(1, M.ACTION) == "MiscExp"
    assert cell(1, M.SECURITY) == "Administration Fees"


def test_open_register_branches_on_account_type(qapp, tmp_path):
    from mammon.ui.widgets import (InvestmentRegisterWidget, MainWindow,
                                  RegisterWidget)
    conn = db.init_db(tmp_path / "invbranch.db")
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=100_00)
    inv = _seed_investment_account(conn)
    win = MainWindow(conn)

    # a non-investment account still opens the cash register (unchanged behavior)
    cash_reg = win.open_register(chk)
    assert isinstance(cash_reg, RegisterWidget)
    assert not isinstance(cash_reg, InvestmentRegisterWidget)

    # an investment account opens the investment register instead
    inv_reg = win.open_register(inv)
    assert isinstance(inv_reg, InvestmentRegisterWidget)
    # ...and it actually shows the investment transactions (the whole point:
    # opening an Investing account used to show NOTHING)
    assert inv_reg.model.rowCount() == 3
    # the valuation header summarizes cash + securities for the account
    assert "Total:" in inv_reg.valuation_label.text()
    # reopening returns the SAME cached widget (register-stack contract holds)
    assert win.open_register(inv) is inv_reg
    win.close()
    conn.close()


def test_investment_widget_display_prefs_and_stack_api(qapp, tmp_path):
    # The investment widget honors the register-stack contract MainWindow relies
    # on: apply_display_prefs / set_view_mode / select_txn / model.reload, and a
    # View-menu one/two-line flip must not raise on an open investment register.
    from mammon.ui.widgets import InvestmentRegisterWidget, MainWindow

    conn = db.init_db(tmp_path / "invapi.db")
    inv = _seed_investment_account(conn)
    win = MainWindow(conn)
    reg = win.open_register(inv)
    assert isinstance(reg, InvestmentRegisterWidget)

    # display prefs apply live (row shading toggles the view)
    win.apply_display_prefs({
        "font_family": "Consolas", "font_size": 12, "row_shading": False,
        "alt_row_color": "#f4f6f9", "negative_color": "#0000ff", "two_line": True,
    })
    assert reg.view.alternatingRowColors() is False
    assert reg.view.font().family() == "Consolas"

    # the one/two-line View toggle is a no-op here but must be safe to call
    win._set_register_view_mode("two")
    win._set_register_view_mode("one")

    # select_txn is a benign no-op (search never targets investment accounts)
    assert reg.select_txn(12345) is False
    # a global refresh re-reads the model + valuation header without error
    win._refresh_all()
    assert reg.model.rowCount() == 3
    win.close()
    conn.close()


# ---- holdings window + price-history chart (Task 46) ------------------------
def _seed_holdings_account(conn, name="Portfolio"):
    """An investment account with a priced holding (gain), a priced holding
    (loss), and an UNPRICED holding -- enough to exercise every Holdings column."""
    from mammon import investments
    acct = ledger.create_account(conn, name, "investment", opening_balance=0)
    # AAPL: 10 @ 100 = 1,000.00 cost; priced 140 -> MV 1,400.00, gain +400.00
    investments.record_investment(conn, acct, "2020-01-05", "Buy", symbol="AAPL",
                                  quantity="10", price="100", amount=-1_000_00)
    # MSFT: 4 @ 50 = 200.00 cost; priced 40 -> MV 160.00, gain -40.00 (red)
    investments.record_investment(conn, acct, "2020-01-06", "Buy", symbol="MSFT",
                                  quantity="4", price="50", amount=-200_00)
    # OBSCURE: 3 shares, 30.00 cost, but NO price anywhere -- no price_history
    # AND no per-transaction price -> genuinely unpriced -> blank Price/MV/Gain.
    investments.record_investment(conn, acct, "2020-01-07", "Buy", symbol="OBSCURE",
                                  quantity="3", amount=-30_00)
    # Prices dated within the activity window (holdings value as of the ledger's
    # last activity date, so a later quote would be excluded from the valuation).
    investments.record_price(conn, "AAPL", "2020-01-07", "140")
    investments.record_price(conn, "MSFT", "2020-01-07", "40")
    return acct


def test_holdings_dialog_shows_positions_gain_and_unpriced(qapp, conn):
    from mammon.ui import style
    from mammon.ui.widgets import HoldingsDialog as H

    acct = _seed_holdings_account(conn)
    dlg = H(conn, acct)

    # three securities + the trailing Cash row
    assert dlg.table.rowCount() == 4
    # columnCount(), not a hardcoded width: a column added later then shows up
    # here as an extra entry instead of being silently cropped out of the check.
    headers = [dlg.table.horizontalHeaderItem(c).text()
               for c in range(dlg.table.columnCount())]
    assert headers == ["Symbol", "Description", "Shares", "Cost Basis", "Price",
                       "Market Value", "Dividends", "Gain/Loss"]

    def cell(r, c):
        return dlg.table.item(r, c).text()

    rows = {cell(r, H.SYMBOL): r for r in range(3)}

    # priced holding with a gain
    a = rows["AAPL"]
    assert cell(a, H.SHARES) == "10"
    assert cell(a, H.COST) == "1,000.00"
    assert cell(a, H.PRICE) == "140"
    assert cell(a, H.MARKET) == "1,400.00"
    assert cell(a, H.GAIN) == "400.00"
    # a positive gain is not painted red
    assert dlg.table.item(a, H.GAIN).foreground().color().name() != style.negative_color()

    # priced holding with a loss -> red gain/loss
    m = rows["MSFT"]
    assert cell(m, H.MARKET) == "160.00"
    assert cell(m, H.GAIN) == "-40.00"
    assert dlg.table.item(m, H.GAIN).foreground().color().name() == style.negative_color()

    # unpriced holding -> blank Price / Market Value / Gain-Loss
    o = rows["OBSCURE"]
    assert cell(o, H.COST) == "30.00"
    assert cell(o, H.PRICE) == ""
    assert cell(o, H.MARKET) == ""
    assert cell(o, H.GAIN) == ""

    # Cash is the last row: market value only, no lot columns, and no symbol
    # behind it (so the price-history double-click passes over it).
    cash_row = dlg.table.rowCount() - 1
    assert cell(cash_row, H.SYMBOL) == "Cash"
    assert cell(cash_row, H.MARKET) == fmt_cents(dlg.valuation.cash)
    assert cell(cash_row, H.SHARES) == "" and cell(cash_row, H.COST) == ""
    assert dlg.table.item(cash_row, H.SYMBOL).data(Qt.UserRole) is None

    # footer: securities (1,400 + 160 + 0), cash, and the total the ACCOUNTS
    # LIST shows for this account -- the reason the cash row exists at all.
    assert "1,560.00" in dlg.total_label.text()
    assert fmt_money(dlg.valuation.cash) in dlg.total_label.text()
    assert fmt_money(investments.display_balance(conn, acct)) in dlg.total_label.text()


def test_holdings_button_opens_dialog(qapp, tmp_path, monkeypatch):
    # The register's Holdings button opens HoldingsDialog AND still emits the
    # holdingsRequested signal (kept for testability / future listeners).
    from mammon.ui import widgets
    from mammon.ui.widgets import InvestmentRegisterWidget

    conn = db.init_db(tmp_path / "holdbtn.db")
    inv = _seed_holdings_account(conn)
    reg = InvestmentRegisterWidget(conn, inv)

    seen = []
    reg.holdingsRequested.connect(seen.append)

    created = {}

    class _FakeDialog:
        def __init__(self, c, aid, parent=None):
            created["args"] = (c, aid)

        def exec_(self):
            created["shown"] = True
            return 0

    monkeypatch.setattr(widgets, "HoldingsDialog", _FakeDialog)
    reg._on_holdings()

    assert seen == [inv]                    # signal still fires
    assert created["args"] == (conn, inv)   # dialog built for this account
    assert created.get("shown") is True     # and shown
    conn.close()


def test_holdings_dialog_double_click_charts_price_history(qapp, conn, monkeypatch):
    # Double-clicking a priced holding opens its price-history chart; an unpriced
    # holding shows an informational message instead of an empty chart.
    from mammon.ui import charts, widgets
    from mammon.ui.widgets import HoldingsDialog

    acct = _seed_holdings_account(conn)
    dlg = HoldingsDialog(conn, acct)

    # Capture what would be charted without popping a modal window. show_price_history
    # imports ChartDialog from mammon.ui.charts, so patch the class there.
    charted = []
    monkeypatch.setattr(
        charts.ChartDialog, "exec_", lambda self: charted.append(self.canvas))
    infos = []
    monkeypatch.setattr(
        widgets.QMessageBox, "information",
        staticmethod(lambda *a, **k: infos.append(a)))

    # AAPL has a recorded price -> a real price-history canvas is charted
    dlg.show_price_history("AAPL")
    assert len(charted) == 1
    assert charted[0].figure.axes                    # a real Axes was drawn
    assert not infos

    # OBSCURE has no recorded prices -> informational message, no chart
    dlg.show_price_history("OBSCURE")
    assert len(charted) == 1                          # still no new chart
    assert infos                                      # user was told instead


def test_price_history_canvas_priced_and_placeholder(qapp):
    from decimal import Decimal
    from mammon.ui.charts import ChartDialog, PriceHistoryCanvas

    points = [("2020-01-31", Decimal("100.00")),
              ("2020-02-28", Decimal("110.00")),
              ("2020-03-31", Decimal("105.50"))]
    canvas = PriceHistoryCanvas("AAPL", points)
    assert canvas.figure.axes                       # a real Axes was drawn
    dlg = ChartDialog("Price History - AAPL", canvas)
    assert dlg.canvas is canvas

    # no prices -> placeholder axes, not a crash
    assert PriceHistoryCanvas("OBSCURE", []).figure.axes


# ---- loan principal-decline projection chart (from the loan register) --------
def test_loan_register_charts_declining_principal_projection(qapp, conn, monkeypatch):
    """A loan register can open the projected principal-payoff chart; the plotted
    series steps DOWN (the outstanding balance declining into the future from the
    amortization schedule). ChartDialog.exec_ is patched so no modal shows."""
    from mammon import loans
    from mammon.ui import charts
    from mammon.ui.widgets import RegisterWidget

    # A synthetic 30-year, $300,000 mortgage at 6% with $200/mo escrow.
    principal, term = 300_000_00, 360
    pi = loans.standard_payment(principal, "6.0", term)          # level P&I
    acct = ledger.create_account(conn, "Home Mortgage", "liability",
                                 opening_balance=-principal)
    loans.set_loan_params(
        conn, acct, original_principal=principal, term_months=term,
        payment_amount=pi + 200_00, origination_date="2024-01-01",
        interval="monthly", rates=[("2024-01-01", "6.0")],
        extras=[("Escrow", 200_00, "Taxes + insurance")])

    reg = RegisterWidget(conn, acct)
    assert reg.model.is_loan()                                   # register knows it's a loan

    charted = []
    monkeypatch.setattr(
        charts.ChartDialog, "exec_", lambda self: charted.append(self.canvas))
    reg._chart_projection()

    assert len(charted) == 1
    canvas = charted[0]
    assert isinstance(canvas, charts.LoanProjectionCanvas)
    assert canvas.figure.axes                                    # a real Axes was drawn
    ys = list(canvas.figure.axes[0].lines[0].get_ydata())
    assert len(ys) > 0                                           # a projected series exists
    # projected balance declines into the future (monotonically non-increasing)
    assert all(later <= earlier for earlier, later in zip(ys, ys[1:]))
    assert ys[-1] < ys[0]                                        # and it genuinely falls

    # empty schedule -> placeholder axes, not a crash (matches the other canvases)
    assert charts.LoanProjectionCanvas([]).figure.axes


def test_loan_setup_launches_from_register_toolbar_not_settings(qapp, tmp_path,
                                                                monkeypatch):
    """Loan Setup lives at the TOP of the loan register (a toolbar button), not
    in Settings: the button shows only for loan/liability accounts, re-opens the
    wizard on the stored params (labelled 'Edit Loan…'), and the loan register
    hides clutter columns (Num, Tag)."""
    from mammon import loans
    from mammon.ui.widgets import MainWindow
    from mammon.ui.models import RegisterModel

    conn = db.init_db(tmp_path / "loanbtn.db")
    # A fully-configured loan (liability + loan_params) and a plain cash account.
    principal, term = 200_000_00, 360
    pi = loans.standard_payment(principal, "5.0", term)
    loan_id = ledger.create_account(conn, "Mortgage", "liability",
                                    opening_balance=-principal)
    loans.set_loan_params(
        conn, loan_id, original_principal=principal, term_months=term,
        payment_amount=pi, origination_date="2024-01-01",
        interval="monthly", rates=[("2024-01-01", "5.0")])
    chk_id = ledger.create_account(conn, "Checking", "checking",
                                   opening_balance=1000_00)

    win = MainWindow(conn)

    # Settings menu no longer offers Loan Setup (it moved to the register).
    settings = next(a.menu() for a in win.menuBar().actions()
                    if a.text().replace("&", "") == "Settings")
    assert not any("Loan Setup" in a.text() for a in settings.actions())

    # Loan register: button visible + labelled Edit Loan…, Num/Tag hidden.
    loan_reg = win.open_register(loan_id)
    tb = loan_reg.toolbar
    assert tb.act_loan.isVisible()
    assert tb.act_loan.text() == "Edit Loan…"
    assert loan_reg.view.isColumnHidden(RegisterModel.NUM)
    assert loan_reg.view.isColumnHidden(RegisterModel.TAG)

    # Clicking the button re-opens the wizard, pre-selecting THIS loan account.
    opened = []
    monkeypatch.setattr(win, "_open_loan_wizard", lambda pre: opened.append(pre))
    tb.act_loan.trigger()
    assert opened == [loan_id]

    # A plain cash register hides the button and keeps Num/Tag visible.
    cash_reg = win.open_register(chk_id)
    assert not cash_reg.toolbar.act_loan.isVisible()
    assert not cash_reg.view.isColumnHidden(RegisterModel.NUM)
    assert not cash_reg.view.isColumnHidden(RegisterModel.TAG)

    win.close()
    conn.close()


# ---- chart ANY security from the register (Task 48) -------------------------
def _seed_register_chart_account(conn, name="Trader"):
    """An investment account whose register includes a FULLY SOLD-OUT security
    (ZZZ: bought then entirely sold -> no current holding) that still has recorded
    prices, plus a held security with NO recorded price -- so charting from the
    register must reach securities the Holdings window cannot."""
    from mammon import investments
    acct = ledger.create_account(conn, name, "investment", opening_balance=0)
    # ZZZ: buy 5 @ 20, then sell ALL 5 @ 30 -> 0 shares held (sold out)
    investments.record_investment(conn, acct, "2019-02-01", "Buy", symbol="ZZZ",
                                  quantity="5", price="20", amount=-100_00)
    investments.record_investment(conn, acct, "2019-08-01", "Sell", symbol="ZZZ",
                                  quantity="5", price="30", amount=150_00)
    # NOPX: bought and held, but NO recorded price -> the info-note path
    investments.record_investment(conn, acct, "2019-03-01", "Buy", symbol="NOPX",
                                  quantity="2", price="15", amount=-30_00)
    investments.record_price(conn, "ZZZ", "2019-05-01", "25")
    investments.record_price(conn, "ZZZ", "2019-07-01", "28")
    investments.rebuild_holdings(conn, acct)
    return acct


def _register_row_for(reg, symbol):
    for r in range(reg.model.rowCount()):
        if reg.model.txn_at(r)["symbol"] == symbol:
            return r
    raise AssertionError(f"no register row for {symbol}")


def test_register_charts_any_security_including_sold_out(qapp, conn, monkeypatch):
    # Double-clicking a Security cell charts that security -- including one that is
    # FULLY SOLD OUT (absent from current holdings), which the Holdings window
    # cannot reach. A security with no recorded prices shows the info note instead.
    from mammon import investments
    from mammon.ui import charts, widgets
    from mammon.ui.widgets import InvestmentRegisterWidget
    from mammon.ui.models import InvestmentRegisterModel as M

    acct = _seed_register_chart_account(conn)
    reg = InvestmentRegisterWidget(conn, acct)

    # ZZZ is sold out -> NOT a current holding, so Holdings can't chart it.
    held = {hv.symbol for hv in investments.holding_values(conn, acct)}
    assert "ZZZ" not in held

    charted = []
    monkeypatch.setattr(
        charts.ChartDialog, "exec_", lambda self: charted.append(self.canvas))
    infos = []
    monkeypatch.setattr(
        widgets.QMessageBox, "information",
        staticmethod(lambda *a, **k: infos.append(a)))

    # double-click the sold-out ZZZ's Security cell -> a real price-history chart
    zrow = _register_row_for(reg, "ZZZ")
    reg._on_cell_double_clicked(reg.model.index(zrow, M.SECURITY))
    assert len(charted) == 1
    assert charted[0].figure.axes
    assert not infos

    # a held security with no recorded prices -> info note, not an empty chart
    nrow = _register_row_for(reg, "NOPX")
    reg._on_cell_double_clicked(reg.model.index(nrow, M.SECURITY))
    assert len(charted) == 1                 # no new chart
    assert infos                             # user was told instead

    # double-clicking a NON-Security column does nothing (guard)
    reg._on_cell_double_clicked(reg.model.index(zrow, M.DATE))
    assert len(charted) == 1


def test_register_right_click_menu_charts_security(qapp, conn, monkeypatch):
    # Right-clicking a row with a security offers a 'Price history' action that
    # charts it; a row with no security shows no menu.
    from PyQt5.QtCore import QModelIndex
    from mammon.ui import charts, widgets
    from mammon.ui.widgets import InvestmentRegisterWidget
    from mammon.ui.models import InvestmentRegisterModel as M

    acct = _seed_register_chart_account(conn)
    reg = InvestmentRegisterWidget(conn, acct)

    charted = []
    monkeypatch.setattr(
        charts.ChartDialog, "exec_", lambda self: charted.append(self.canvas))

    class _FakeMenu:
        # The menu now also carries New / Edit (+ a separator); return a distinct
        # action per label and let exec_ pick the 'Price history' item.
        def __init__(self, *a, **k):
            self._acts = {}

        def addAction(self, text):
            act = object()
            self._acts[text] = act
            return act

        def addSeparator(self):
            pass

        def exec_(self, *a, **k):            # simulate choosing 'Price history'
            for text, act in self._acts.items():
                if text.startswith("Price history"):
                    return act
            return None

    monkeypatch.setattr(widgets, "QMenu", _FakeMenu)

    # aim indexAt at ZZZ's Security cell (avoids depending on headless geometry)
    zrow = _register_row_for(reg, "ZZZ")
    index = reg.model.index(zrow, M.SECURITY)
    monkeypatch.setattr(reg.view, "indexAt", lambda pos: index)
    reg._on_view_context_menu(reg.view.rect().center())
    assert len(charted) == 1
    assert charted[0].figure.axes

    # an invalid index (a click off any security) -> menu has no Price-history
    # item, so choosing it is a no-op and nothing charts
    monkeypatch.setattr(reg.view, "indexAt", lambda pos: QModelIndex())
    reg._on_view_context_menu(reg.view.rect().center())
    assert len(charted) == 1


def test_holdings_right_click_charts_price_history(qapp, conn, monkeypatch):
    # Parity: right-clicking a holding also offers the 'Price history' action.
    from mammon.ui import charts, widgets
    from mammon.ui.widgets import HoldingsDialog

    acct = _seed_holdings_account(conn)
    dlg = HoldingsDialog(conn, acct)

    charted = []
    monkeypatch.setattr(
        charts.ChartDialog, "exec_", lambda self: charted.append(self.canvas))

    class _FakeMenu:
        def __init__(self, *a, **k):
            self._act = object()

        def addAction(self, text):
            return self._act

        def exec_(self, *a, **k):
            return self._act

    monkeypatch.setattr(widgets, "QMenu", _FakeMenu)

    arow = next(r for r in range(dlg.table.rowCount())
                if dlg.table.item(r, HoldingsDialog.SYMBOL).text() == "AAPL")
    monkeypatch.setattr(dlg.table, "itemAt", lambda pos: dlg.table.item(arow, 0))
    dlg._on_context_menu(dlg.table.rect().center())
    assert len(charted) == 1
    assert charted[0].figure.axes


def test_holdings_dialog_previously_held_tab(qapp, conn):
    """A sold-out security shows in the Previously Held tab with its realized P/L
    (not in Currently Held); the tabs reconcile to mammon.investments."""
    from mammon import investments
    from mammon.ui.widgets import HoldingsDialog as H

    acct = _seed_register_chart_account(conn)   # ZZZ sold out (+50.00), NOPX held
    dlg = H(conn, acct)

    held_syms = {dlg.table.item(r, H.SYMBOL).text()
                 for r in range(dlg.table.rowCount())}
    assert "NOPX" in held_syms and "ZZZ" not in held_syms

    closed_syms = {dlg.closed_table.item(r, H.C_SYMBOL).text()
                   for r in range(dlg.closed_table.rowCount())}
    assert closed_syms == {"ZZZ"}
    zrow = next(r for r in range(dlg.closed_table.rowCount())
                if dlg.closed_table.item(r, H.C_SYMBOL).text() == "ZZZ")
    # realized P/L = 150.00 proceeds - 100.00 cost = 50.00
    assert dlg.closed_table.item(zrow, H.C_PL).text() == "50.00"

    cp = {p.symbol: p for p in investments.closed_positions(conn, acct)}
    assert cp["ZZZ"].realized_pl == 50_00


def test_register_security_filter_reconciles_to_holdings(qapp, conn):
    """Selecting a security filters the register to its rows and shows a summary
    (shares + dividends + cost basis + P/L) that ties to the Holdings view."""
    from mammon import investments
    from mammon.ui.widgets import InvestmentRegisterWidget

    acct = _seed_holdings_account(conn)          # AAPL priced (gain +400.00)
    investments.rebuild_holdings(conn, acct)     # populate the holdings table
    reg = InvestmentRegisterWidget(conn, acct)

    idx = reg.security_filter.findData("AAPL")
    assert idx > 0
    reg.security_filter.setCurrentIndex(idx)
    assert {reg.model.txn_at(r)["symbol"]
            for r in range(reg.model.rowCount())} == {"AAPL"}

    as_of = ledger.latest_activity_date(conn)
    pos = investments.security_report(conn, acct, "AAPL", as_of=as_of).position
    hv = {h.symbol: h for h in
          investments.holding_values(conn, acct, as_of=as_of)}["AAPL"]
    assert pos.unrealized_pl == hv.gain == 400_00
    text = reg.security_summary.text()
    assert "Shares: 10" in text
    assert "400.00" in text                      # the reconciled P/L

    # '(All securities)' clears the filter and the summary line.
    reg.security_filter.setCurrentIndex(0)
    assert reg.model.rowCount() == 3
    assert reg.security_summary.text() == ""


# ---- investment new/edit dialog (per-action, interdependent Qty/Price/Amount) -
def test_investment_dialog_computes_third_of_qpa(qapp, conn):
    """Enter any two of Quantity/Price/Amount and the dialog derives the third;
    all three together are classed consistent / conflict, fewer than two is
    insufficient."""
    from decimal import Decimal
    from mammon.ui.widgets import InvestmentTransactionDialog as Dlg

    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    dlg = Dlg(conn, inv)                      # Buy is the default action

    # qty + price -> amount (signed cash-out for a Buy)
    _set_date(dlg.date, "2026-05-01")
    dlg.security.setEditText("AAPL")
    dlg.quantity.setText("10")
    dlg.price.setText("100.00")
    v = dlg.values()
    assert v["action"] == "Buy" and v["symbol"] == "AAPL"
    assert v["amount"] == -1000_00

    # qty + amount -> price
    dlg.price.clear()
    dlg.amount.setText("1500.00")
    assert dlg.values()["price"] == Decimal("150")

    # price + amount -> qty
    dlg.quantity.clear()
    dlg.amount.setText("900.00")
    dlg.price.setText("90.00")
    assert dlg.values()["quantity"] == Decimal("10")

    # pure solver: consistency / conflict / insufficient
    assert Dlg.resolve_qpa(Decimal("10"), Decimal("100"), 1000_00)[3] == "consistent"
    assert Dlg.resolve_qpa(Decimal("10"), Decimal("100"), 900_00)[3] == "conflict"
    assert Dlg.resolve_qpa(Decimal("10"), None, None)[3] == "insufficient"


def test_investment_dialog_validate_requires_security_and_two_of_qpa(qapp, conn):
    from mammon.ui.widgets import InvestmentTransactionDialog as Dlg

    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    dlg = Dlg(conn, inv)
    _set_date(dlg.date, "2026-05-01")
    dlg.quantity.setText("10")
    dlg.price.setText("100.00")

    ok, msg = dlg.validate()
    assert not ok and "security" in msg.lower()

    dlg.security.setEditText("AAPL")
    ok, msg = dlg.validate()
    assert ok, msg

    # only one of the trio -> insufficient
    dlg.price.clear()
    ok, msg = dlg.validate()
    assert not ok and "two" in msg.lower()


def test_investment_register_new_inserts(qapp, conn, monkeypatch):
    """The New button opens the per-action dialog and inserts a valid investment
    transaction; the register model reloads to show it."""
    from PyQt5.QtWidgets import QDialog
    from mammon import investments
    from mammon.ui.widgets import InvestmentRegisterWidget, InvestmentTransactionDialog

    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    reg = InvestmentRegisterWidget(conn, inv)

    def fake_exec(self):
        _set_date(self.date, "2026-05-01")
        self.security.setEditText("AAPL")
        self.quantity.setText("10")
        self.price.setText("100.00")
        self.commission.setText("4.95")
        return QDialog.Accepted

    monkeypatch.setattr(InvestmentTransactionDialog, "exec_", fake_exec)
    reg.on_new()

    rows = investments.list_investment_txns(conn, inv)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "Buy" and r["symbol"] == "AAPL"
    assert r["quantity"] == "10" and r["price"] == "100"
    assert r["amount"] == -1000_00 and r["commission"] == 4_95
    assert reg.model.rowCount() == 1         # register reloaded


def test_investment_register_edit_roundtrips(qapp, conn, monkeypatch):
    """Context-menu Edit loads an existing transaction into the dialog and, on
    accept, writes the amended row back (untouched fields survive)."""
    from PyQt5.QtWidgets import QDialog
    from mammon import investments
    from mammon.ui.widgets import InvestmentRegisterWidget, InvestmentTransactionDialog

    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    tid = investments.record_investment(
        conn, inv, "2026-01-05", "Buy", symbol="AAPL", quantity="10",
        price="100.00", amount=-1000_00, commission=9_95)
    reg = InvestmentRegisterWidget(conn, inv)

    seen = {}

    def fake_exec(self):
        # edit mode pre-populated the widgets from the stored row
        seen["date"] = date_edit_iso(self.date)
        seen["action"] = self.action.currentData()
        seen["symbol"] = self.security.currentText()
        seen["quantity"] = self.quantity.text()
        seen["amount"] = self.amount.text()
        # raise the price; clear amount so it recomputes from qty*price
        self.price.setText("110.00")
        self.amount.clear()
        return QDialog.Accepted

    monkeypatch.setattr(InvestmentTransactionDialog, "exec_", fake_exec)
    reg._edit_row(0)

    assert seen["action"] == "Buy" and seen["symbol"] == "AAPL"
    assert seen["quantity"] == "10" and seen["amount"] == "1,000.00"

    row = investments.get_investment_txn(conn, tid)
    assert row["price"] == "110" and row["amount"] == -1100_00
    assert row["commission"] == 9_95         # untouched field round-trips


def test_investment_dialog_div_category_and_transfer(qapp, conn):
    """A cash Dividend shows a Category/Transfer picker: free text becomes the
    memo/category, a known account becomes a transfer leg."""
    from mammon.ui.widgets import InvestmentTransactionDialog as Dlg

    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    dlg = Dlg(conn, inv)
    dlg.action.setCurrentIndex(dlg.action.findData("Div"))
    _set_date(dlg.date, "2026-03-01")
    dlg.security.setEditText("AAPL")
    dlg.amount.setText("25.00")

    dlg.catxfer.setEditText("Dividend Income")
    v = dlg.values()
    assert v["action"] == "Div" and v["amount"] == 25_00     # cash in, positive
    assert v["memo"] == "Dividend Income" and v["transfer_account_id"] is None

    dlg.catxfer.setCurrentIndex(dlg.catxfer.findData(chk))
    v2 = dlg.values()
    assert v2["transfer_account_id"] == chk and v2["memo"] is None

    # a cash transfer (XIn/XOut) surfaces the same transfer picker; XOut is cash
    # out (negative), XIn cash in (positive)
    xdlg = Dlg(conn, inv)
    xdlg.action.setCurrentIndex(xdlg.action.findData("XOut"))
    _set_date(xdlg.date, "2026-03-02")
    xdlg.amount.setText("200.00")
    xdlg.catxfer.setCurrentIndex(xdlg.catxfer.findData(chk))
    vx = xdlg.values()
    assert vx["action"] == "XOut" and vx["amount"] == -200_00
    assert vx["transfer_account_id"] == chk and vx["symbol"] is None


def test_investment_dialog_reinvest_breakdown_and_shorthand(qapp, conn):
    """Reinvest carries per-income-type lines; a pure-dividend reinvestment is
    stored as the 'ReinvDiv' shorthand, a mixed one as generic 'Reinvest'."""
    from mammon.ui.widgets import InvestmentTransactionDialog as Dlg

    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    dlg = Dlg(conn, inv)
    dlg.action.setCurrentIndex(dlg.action.findData("Reinvest"))
    _set_date(dlg.date, "2026-04-01")
    dlg.security.setEditText("VTI")
    dlg.quantity.setText("2")
    dlg.price.setText("50.00")
    dlg.reinv_div.setText("100.00")

    v = dlg.values()
    assert v["action"] == "ReinvDiv"          # only the dividend line -> shorthand
    assert v["amount"] == 100_00              # 2 * 50.00 (cash-neutral gross)
    assert "Div" in (v["memo"] or "")

    dlg.reinv_lt.setText("10.00")             # add a long-term cap-gain line
    assert dlg.values()["action"] == "Reinvest"


def test_investment_register_context_menu_offers_new_and_edit(qapp, conn, monkeypatch):
    """Right-clicking a row offers New / Edit alongside the existing Price-history
    item, and choosing New invokes on_new."""
    from mammon.ui import widgets
    from mammon.ui.widgets import InvestmentRegisterWidget
    from mammon.ui.models import InvestmentRegisterModel as M

    inv = _seed_investment_account(conn)
    reg = InvestmentRegisterWidget(conn, inv)

    labels = []
    pick = {"text": "New…"}

    class _FakeMenu:
        def __init__(self, *a, **k):
            self._acts = {}

        def addAction(self, text):
            act = object()
            self._acts[text] = act
            labels.append(text)
            return act

        def addSeparator(self):
            pass

        def exec_(self, *a, **k):
            return self._acts.get(pick["text"])

    monkeypatch.setattr(widgets, "QMenu", _FakeMenu)

    called = {}
    monkeypatch.setattr(reg, "on_new", lambda: called.__setitem__("new", True))
    monkeypatch.setattr(reg, "_edit_row", lambda row: called.__setitem__("edit", row))

    index = reg.model.index(0, M.SECURITY)
    monkeypatch.setattr(reg.view, "indexAt", lambda pos: index)
    reg._on_view_context_menu(reg.view.rect().center())

    assert "New…" in labels and "Edit…" in labels
    assert any(t.startswith("Price history") for t in labels)   # existing item kept
    assert called.get("new") is True


def test_register_context_menu_go_to_transfer(qapp, conn, accounts, monkeypatch):
    """Right-clicking one leg of a transfer offers 'Go to [other account]', and
    choosing it switches the main window to the counterparty register with the
    mirror transaction selected. A plain (non-transfer) row offers no such
    entry -- the action hides for anything that isn't a transfer leg."""
    from mammon.ui import widgets
    from mammon.ui.widgets import MainWindow
    from mammon.ui.models import RegisterModel as M

    chk, sav = accounts
    from_id, to_id = ledger.create_transfer(
        conn, chk, sav, "2026-06-01", 50_00, payee="Move money")
    plain = ledger.add_transaction(conn, chk, "2026-06-02", -10_00, payee="Coffee")

    win = MainWindow(conn)
    reg = win.open_register(chk)
    transfer_row = reg.model.row_for_txn(from_id)
    plain_row = reg.model.row_for_txn(plain)
    assert transfer_row >= 0 and plain_row >= 0

    labels = []
    pick = {"text": None}

    class _FakeMenu:
        def __init__(self, *a, **k):
            self._acts = {}

        def addAction(self, text):
            act = object()
            self._acts[text] = act
            labels.append(text)
            return act

        def addSeparator(self):
            pass

        def exec_(self, *a, **k):
            return self._acts.get(pick["text"])

    monkeypatch.setattr(widgets, "QMenu", _FakeMenu)

    # A non-transfer row must NOT offer a 'Go to' entry.
    labels.clear()
    monkeypatch.setattr(
        reg.view, "indexAt", lambda pos: reg.model.index(plain_row, M.PAYEE))
    pick["text"] = None
    reg._context_menu(reg.view.rect().center())
    assert not any(t.startswith("Go to") for t in labels)

    # The transfer leg offers 'Go to [Savings]' and navigates to the mirror.
    labels.clear()
    monkeypatch.setattr(
        reg.view, "indexAt", lambda pos: reg.model.index(transfer_row, M.PAYEE))
    pick["text"] = "Go to [Savings]"
    reg._context_menu(reg.view.rect().center())
    assert "Go to [Savings]" in labels

    sav_reg = win._registers[sav]
    assert win._current_account == sav
    assert win.stack.currentWidget() is sav_reg
    selected = sav_reg.model.txn_at(sav_reg._selected_row())
    assert selected is not None and selected["id"] == to_id
    win.close()


def test_go_to_transfer_from_loan_mirror_leg_via_split(qapp, conn):
    """The loan-register case: a regular loan payment posts on CHECKING as a
    split whose principal line transfers into the loan; the loan side is only a
    ONE-SIDED mirror leg (its own transfer_pair_id NULL, referenced by the
    checking split's transfer_pair_id). Right-clicking that mirror leg in the
    LOAN register must still offer 'Go to [Checking]' and land on the checking
    payment -- the menu has to follow the REVERSE link, because the loan row has
    no top-level transfer and no splits of its own to inspect."""
    from mammon.ui.widgets import MainWindow

    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1_000_00)
    loan = ledger.create_account(conn, "Mortgage", "liability", opening_balance=-100_000_00)
    pay = ledger.add_transaction(conn, chk, "2026-06-01", -1_200_00, payee="US Bank")
    ledger.set_splits(conn, pay, [
        {"transfer_account_id": loan, "amount": -300_00, "memo": "principal"},
        {"category_id": None, "amount": -900_00, "memo": "interest"},
    ])
    # The loan side is a one-sided mirror leg: the SPLIT remembers it, the leg's
    # own transfer_pair_id stays NULL (exactly the recast acct-81 shape).
    leg = conn.execute(
        "SELECT transfer_pair_id FROM splits "
        "WHERE transaction_id=? AND transfer_account_id=?", (pay, loan),
    ).fetchone()["transfer_pair_id"]
    assert leg is not None
    assert conn.execute(
        "SELECT transfer_pair_id FROM transactions WHERE id=?", (leg,),
    ).fetchone()["transfer_pair_id"] is None

    win = MainWindow(conn)
    reg = win.open_register(loan)
    row = reg.model.row_for_txn(leg)
    assert row >= 0
    targets = reg._transfer_targets(row)
    assert [(t["account_id"], t["txn_id"]) for t in targets] == [(chk, pay)]
    assert targets[0]["name"] == "Checking"
    # Clicking it navigates to the checking payment.
    reg._go_to_transfer(targets[0])
    assert win._current_account == chk
    chk_reg = win._registers[chk]
    sel = chk_reg.model.txn_at(chk_reg._selected_row())
    assert sel is not None and sel["id"] == pay
    win.close()


def test_go_to_transfer_lists_each_distinct_split_target_deduped(qapp, conn):
    """A split with several transfer legs offers one 'Go to [account]' per
    DISTINCT target account: two legs to two accounts -> two entries; two legs
    to the SAME account -> a single entry (deduped, not one per leg)."""
    from mammon.ui.widgets import MainWindow

    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=10_000_00)
    a = ledger.create_account(conn, "Alpha", "savings", opening_balance=0)
    b = ledger.create_account(conn, "Beta", "savings", opening_balance=0)

    multi = ledger.add_transaction(conn, chk, "2026-06-01", -300_00, payee="Split out")
    ledger.set_splits(conn, multi, [
        {"transfer_account_id": a, "amount": -100_00},
        {"transfer_account_id": b, "amount": -150_00},
        {"category_id": None, "amount": -50_00},
    ])
    same = ledger.add_transaction(conn, chk, "2026-06-02", -200_00, payee="Two to Alpha")
    ledger.set_splits(conn, same, [
        {"transfer_account_id": a, "amount": -120_00},
        {"transfer_account_id": a, "amount": -80_00},
    ])

    win = MainWindow(conn)
    reg = win.open_register(chk)

    tg = reg._transfer_targets(reg.model.row_for_txn(multi))
    assert [t["account_id"] for t in tg] == [a, b]            # one per distinct target
    assert [t["name"] for t in tg] == ["Alpha", "Beta"]

    tg2 = reg._transfer_targets(reg.model.row_for_txn(same))
    assert [t["account_id"] for t in tg2] == [a]             # two legs, one entry
    win.close()


def test_go_to_transfer_split_leg_without_pair_id_still_offered(qapp, conn):
    """A transfer split with no transfer_pair_id (legacy/import legs -- the bulk
    of a real data) is still a transfer, so it must still offer 'Go to
    [account]'. Navigation lands on the account; with no specific mirror row it
    selects nothing rather than crashing."""
    from mammon.ui.widgets import MainWindow

    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=10_000_00)
    dest = ledger.create_account(conn, "Destination", "savings", opening_balance=0)
    txn = ledger.add_transaction(conn, chk, "2026-06-01", -250_00, payee="Legacy split")
    ledger.set_splits(conn, txn, [
        {"transfer_account_id": dest, "amount": -100_00},
        {"category_id": None, "amount": -150_00},
    ])
    # Simulate the legacy shape: drop the split's pair link (and its mirror leg),
    # leaving a transfer leg with transfer_account_id set but pair_id NULL.
    leg = conn.execute(
        "SELECT transfer_pair_id FROM splits "
        "WHERE transaction_id=? AND transfer_account_id=?", (txn, dest),
    ).fetchone()["transfer_pair_id"]
    conn.execute("DELETE FROM transactions WHERE id=?", (leg,))
    conn.execute(
        "UPDATE splits SET transfer_pair_id=NULL "
        "WHERE transaction_id=? AND transfer_account_id=?", (txn, dest))
    conn.commit()

    win = MainWindow(conn)
    reg = win.open_register(chk)
    tg = reg._transfer_targets(reg.model.row_for_txn(txn))
    assert [(t["account_id"], t["txn_id"]) for t in tg] == [(dest, None)]
    assert tg[0]["name"] == "Destination"
    reg._go_to_transfer(tg[0])                               # no mirror -> no crash
    assert win._current_account == dest
    win.close()


# ---- loan setup wizard (Task 53: guided loan-parameter entry) --------------
def test_loan_wizard_creates_new_account_and_params(qapp, conn):
    """Driving the wizard headlessly and completing it creates a liability
    account AND writes a valid loan_params set that the engine accepts."""
    from decimal import Decimal

    from mammon import loans
    from mammon.ui.loan_wizard import LoanSetupWizard, _NEW_ACCOUNT

    pi = loans.standard_payment(300_000_00, "6.0", 360)      # level P&I = 1798.65
    total = pi + 200_00                                       # + escrow

    wiz = LoanSetupWizard(conn)
    wiz.account_combo.setCurrentIndex(wiz.account_combo.findData(_NEW_ACCOUNT))
    wiz.new_name.setText("Home Mortgage")
    wiz.principal.setValue(300_000.00)
    _set_date(wiz.first_payment, "2024-02-01")
    wiz.term_months.setValue(360)
    wiz.payment.setValue(total / 100.0)
    wiz.interval.setCurrentIndex(wiz.interval.findData("monthly"))
    wiz.add_rate_row("2024-02-01", "6.0")
    wiz.add_extra_row("Escrow", "200.00", "Taxes + insurance")

    assert wiz.save() is True
    aid = wiz.saved_account_id

    acct = ledger.get_account(conn, aid)
    assert acct["name"] == "Home Mortgage" and acct["type"] == "liability"
    # a new loan account opens with the balance owed (negative)
    assert ledger.account_balance(conn, aid) == -300_000_00

    lp = loans.get_loan_params(conn, aid)
    assert lp is not None
    assert lp.original_principal == 300_000_00
    assert lp.term_months == 360
    assert lp.payment_amount == total
    assert lp.interval == "monthly"
    assert [(r.effective_date, r.annual_rate) for r in lp.rates] == [
        ("2024-02-01", Decimal("6.0"))]
    assert [(e.category, e.amount, e.label) for e in lp.extras] == [
        ("Escrow", 200_00, "Taxes + insurance")]

    # the engine accepts it: schedule dates land on the entered first payment and
    # the first split reconstitutes the payment to the cent
    sched = loans.amortization_schedule(conn, aid)
    assert sched[0].date == "2024-02-01"
    split = loans.payment_split(conn, aid, "2024-02-01", total)
    assert split.balance_before == 300_000_00
    assert split.interest == 1500_00                         # 300000 * 6%/12
    assert split.escrow == 200_00
    assert split.principal + split.interest + split.escrow == total


def test_loan_wizard_reloads_and_edits_existing(qapp, conn):
    """Opening the wizard on an account with saved params reloads them into the
    fields (the edit path), and saving again replaces the stored set."""
    from decimal import Decimal

    from mammon import loans
    from mammon.ui.loan_wizard import LoanSetupWizard

    aid = ledger.create_account(conn, "Auto Loan", "liability",
                                opening_balance=-20_000_00)
    loans.set_loan_params(
        conn, aid, original_principal=20_000_00, term_months=60,
        payment_amount=400_00, origination_date="2024-01-01", interval="monthly",
        rates=[("2024-02-01", "5.0")], extras=[("Escrow", 50_00, None)])

    wiz = LoanSetupWizard(conn, account_id=aid)
    # fields are pre-filled from the stored params
    assert wiz.account_combo.currentData() == aid
    assert round(wiz.principal.value(), 2) == 20_000.00
    assert wiz.term_months.value() == 60
    assert round(wiz.payment.value(), 2) == 400.00
    assert wiz.interval.currentData() == "monthly"
    # first-payment date reproduces (origination + one interval)
    assert date_edit_iso(wiz.first_payment) == "2024-02-01"
    assert wiz.rates_table.rowCount() == 1
    assert wiz._cell_text(wiz.rates_table, 0, 0) == "2024-02-01"
    assert wiz.extras_table.rowCount() == 1
    assert wiz._extra_category(0) == "Escrow"

    # edit: raise the payment and add a later rate row, then save
    wiz.payment.setValue(450.00)
    wiz.add_rate_row("2025-01-01", "5.5")
    assert wiz.save() is True

    lp = loans.get_loan_params(conn, aid)
    assert lp.payment_amount == 450_00
    assert [(r.effective_date, r.annual_rate) for r in lp.rates] == [
        ("2024-02-01", Decimal("5.0")), ("2025-01-01", Decimal("5.5"))]
    # no duplicate account was created by the edit
    assert len([a for a in ledger.list_accounts(conn) if a["name"] == "Auto Loan"]) == 1


def test_loan_wizard_extras_carry_effective_dates(qapp, conn):
    """The extras step is effective-dated like the rate step: two rows for the
    same category with different dates persist a dated escrow history, and the
    engine honors it (old amount before the date, new amount on/after)."""
    from mammon import loans
    from mammon.ui.loan_wizard import LoanSetupWizard, _NEW_ACCOUNT

    pi = loans.standard_payment(300_000_00, "6.0", 360)      # 1798.65
    wiz = LoanSetupWizard(conn)
    wiz.account_combo.setCurrentIndex(wiz.account_combo.findData(_NEW_ACCOUNT))
    wiz.new_name.setText("Home Mortgage")
    wiz.principal.setValue(300_000.00)
    _set_date(wiz.first_payment, "2024-02-01")
    wiz.term_months.setValue(360)
    wiz.payment.setValue((pi + 300_00) / 100.0)              # room for higher escrow
    wiz.interval.setCurrentIndex(wiz.interval.findData("monthly"))
    wiz.add_rate_row("2024-02-01", "6.0")
    # add_extra_row(category, amount, label, effective_date)
    wiz.add_extra_row("Escrow", "200.00", "Taxes", "2024-02-01")
    wiz.add_extra_row("Escrow", "300.00", "Taxes", "2025-01-01")
    assert wiz.extras_table.columnCount() == 5

    assert wiz.save() is True
    aid = wiz.saved_account_id

    lp = loans.get_loan_params(conn, aid)
    assert sorted((e.effective_date, e.amount)
                  for e in lp.extras if e.category == "Escrow") == [
        ("2024-02-01", 200_00), ("2025-01-01", 300_00)]
    # the split honors the dated escrow history
    assert loans.payment_split(conn, aid, "2024-12-01", pi + 200_00).escrow == 200_00
    assert loans.payment_split(conn, aid, "2025-01-01", pi + 300_00).escrow == 300_00

    # reopening the wizard reloads the dated rows into the extras table
    wiz2 = LoanSetupWizard(conn, account_id=aid)
    assert wiz2.extras_table.rowCount() == 2
    assert wiz2._cell_text(wiz2.extras_table, 0, 2) == "2024-02-01"
    assert wiz2._cell_text(wiz2.extras_table, 1, 2) == "2025-01-01"


def test_loan_wizard_new_total_payment_persists_and_defaults(qapp, conn):
    """The extras step exposes an editable 'New total payment' per dated change:
    it defaults to the recomputed total (level P&I + active extras) yet is
    user-authoritative, and persists to loan_payments so the schedule forward
    adopts the entered amount. Step-2 numeric fields carry no spinner arrows."""
    from PyQt5.QtWidgets import QAbstractSpinBox
    from mammon import loans
    from mammon.ui.loan_wizard import LoanSetupWizard, _NEW_ACCOUNT

    pi = loans.standard_payment(300_000_00, "6.0", 360)      # 1798.65

    # Task 2: the typed-in numeric fields have their up/down arrows removed.
    wiz = LoanSetupWizard(conn)
    for spin in (wiz.principal, wiz.term_months, wiz.payment):
        assert spin.buttonSymbols() == QAbstractSpinBox.NoButtons

    wiz.account_combo.setCurrentIndex(wiz.account_combo.findData(_NEW_ACCOUNT))
    wiz.new_name.setText("Mortgage With Recast")
    wiz.principal.setValue(300_000.00)
    _set_date(wiz.first_payment, "2024-02-01")
    wiz.term_months.setValue(360)
    wiz.payment.setValue((pi + 200_00) / 100.0)
    wiz.interval.setCurrentIndex(wiz.interval.findData("monthly"))
    wiz.add_rate_row("2024-02-01", "6.0")
    wiz.add_extra_row("Escrow", "200.00", "Taxes", "2024-02-01")

    # The default equals level P&I + the extras active on that date.
    assert wiz._recomputed_total_cents("2024-02-01") == pi + 200_00

    # A dated escrow change whose New-total-payment is OVERRIDDEN: the recomputed
    # default on that date would be pi + 300 = 2098.65, but the user's 2050 wins.
    wiz.add_extra_row("Escrow", "300.00", "Taxes", "2025-01-01", "2050.00")
    assert wiz._recomputed_total_cents("2025-01-01") == pi + 300_00

    assert wiz.save() is True
    aid = wiz.saved_account_id

    lp = loans.get_loan_params(conn, aid)
    payments = {p.effective_date: p.amount for p in lp.payments}
    # the dated override landed in loan_payments and became the current total
    assert payments["2025-01-01"] == 2050_00
    assert lp.payment_amount == 2050_00
    # forward splits use the entered total; earlier periods keep the baseline
    assert loans._active_payment(lp.payments, lp.payment_amount, "2025-06-01") == 2050_00
    assert loans._active_payment(lp.payments, lp.payment_amount, "2024-06-01") == pi + 200_00

    # the interactive Add-row button seeds the cell with the recomputed default
    wiz._add_extra_row_interactive()
    seeded = wiz._cell_text(wiz.extras_table, wiz.extras_table.rowCount() - 1, 4)
    assert seeded == f"{(pi + 200_00) / 100:.2f}"


def test_loan_wizard_new_payment_edit_resplits_forward_via_save(qapp, conn,
                                                                monkeypatch):
    """LIVE BUG (the user): editing the 'New total payment' field in the Edit Loan
    dialog must RE-DERIVE the schedule from the effective date FORWARD, not merely
    store the number (the old save path called loans.add_payment_change but never
    loans_schedule.apply_payment_change, so already-materialised register rows kept
    their stale split -- the edit looked like it did nothing). This drives the save
    handler directly (no modal) and asserts the engine ran, the forward split is
    recomputed to the cent, the running balance cascades, and no rows are doubled."""
    from mammon import ledger, loans, loans_schedule
    from mammon.ui.loan_wizard import LoanSetupWizard

    # A 300k @ 6% / 360-mo mortgage, $200 escrow, level total 1998.65.
    aid = ledger.create_account(conn, "Home Mortgage", "liability",
                                opening_balance=-300_000_00)
    loans.set_loan_params(
        conn, aid, original_principal=300_000_00, term_months=360,
        payment_amount=1998_65, origination_date="2024-01-01", interval="monthly",
        rates=[("2024-02-01", "6.0")], extras=[("Escrow", 200_00, "Taxes + ins")])

    # Pre-enter pending payments straddling the effective date: 03-01 stays BEFORE
    # the change (must be untouched); 04-01 and 05-01 must be re-derived.
    ids = {d: loans_schedule.create_pending_payment(conn, aid, d)
           for d in ("2024-03-01", "2024-04-01", "2024-05-01")}

    def legs(pid):
        return {l["category_label"]: l["amount"]
                for l in ledger.get_splits(conn, pid)}

    before = {d: legs(pid) for d, pid in ids.items()}
    sched_before = {r.date: r for r in loans.amortization_schedule(conn, aid)}

    # Spy on the engine to prove the save path actually invokes it (keep the real
    # behaviour by delegating through).
    calls = []
    real = loans_schedule.apply_payment_change

    def spy(*a, **k):
        calls.append((a, k))
        return real(*a, **k)

    monkeypatch.setattr(loans_schedule, "apply_payment_change", spy)

    # Open the Edit Loan dialog on the existing loan and set a NEW total payment of
    # 2100.00 effective 2024-04-01 -- a PURE payment change (no escrow category, so
    # it also exercises the collect fix that used to discard such a row).
    wiz = LoanSetupWizard(conn, account_id=aid)
    wiz.add_extra_row(effective_date="2024-04-01", new_payment="2100.00")
    assert wiz.save() is True

    # (a) the save path called apply_payment_change with the dated new total.
    assert len(calls) == 1
    (_c_conn, c_aid, c_eff), c_kw = calls[0]
    assert c_aid == aid and c_eff == "2024-04-01"
    assert c_kw["change_type"] == "payment"
    assert c_kw["new_payment_amount"] == 2100_00

    after = {d: legs(pid) for d, pid in ids.items()}

    # earlier period is untouched -- old total, old split
    assert after["2024-03-01"] == before["2024-03-01"]
    assert ledger.get_transaction(conn, ids["2024-03-01"])["amount"] == 1998_65

    # (b) on/after the effective date every payment adopts the new total and its
    #     interest/principal split is recomputed to match the engine to the cent.
    # (e) the principal leg -- the amount that in the user's data posts as the transfer
    #     to the loan account -- equals total - interest - escrow.
    for d in ("2024-04-01", "2024-05-01"):
        txn = ledger.get_transaction(conn, ids[d])
        assert txn["amount"] == 2100_00
        split = loans.payment_split(conn, aid, d, 2100_00)
        assert after[d]["Interest Exp"] == split.interest
        assert after[d]["Escrow"] == 200_00
        assert after[d]["Principal"] == split.principal
        assert after[d]["Principal"] == 2100_00 - split.interest - 200_00
        assert sum(after[d].values()) == 2100_00
        # the higher total drives more money into principal than the old split did
        assert after[d]["Principal"] > before[d]["Principal"]

    # (c) the running balance cascades: from the effective date the schedule uses
    #     the new total and, paying down faster, lands on a LOWER balance than the
    #     old schedule at the same date -- and stays internally consistent.
    sched_after = {r.date: r for r in loans.amortization_schedule(conn, aid)}
    assert sched_after["2024-03-01"].payment == 1998_65        # before eff: unchanged
    assert sched_after["2024-04-01"].payment == 2100_00
    assert sched_after["2024-04-01"].balance < sched_before["2024-04-01"].balance
    prev = 300_000_00
    for d in ("2024-02-01", "2024-03-01", "2024-04-01", "2024-05-01"):
        r = sched_after[d]
        assert r.principal + r.interest + r.escrow == r.payment
        assert r.balance == prev - r.principal
        prev = r.balance

    # (d) no duplicate rows: apply_payment_change re-split IN PLACE; it did not
    #     insert a second payment for any date.
    for d in ids:
        n = conn.execute(
            "SELECT COUNT(*) c FROM transactions WHERE account_id=? AND date=?",
            (aid, d)).fetchone()["c"]
        assert n == 1


def test_loan_wizard_validation_rejects_bad_input(qapp, conn):
    """validate() is a pure gate: it refuses missing rates, a payment too small to
    amortize, and out-of-order rate rows -- and nothing is written on a refusal."""
    from mammon import loans
    from mammon.ui.loan_wizard import LoanSetupWizard, _NEW_ACCOUNT

    wiz = LoanSetupWizard(conn)
    wiz.account_combo.setCurrentIndex(wiz.account_combo.findData(_NEW_ACCOUNT))
    wiz.new_name.setText("Bad Loan")
    wiz.principal.setValue(100_000.00)
    _set_date(wiz.first_payment, "2024-02-01")
    wiz.term_months.setValue(360)
    wiz.payment.setValue(700.00)
    wiz.interval.setCurrentIndex(wiz.interval.findData("monthly"))

    # (a) no interest-rate rows -> invalid
    ok, msg = wiz.validate()
    assert not ok and "rate" in msg.lower()

    # (b) a payment that cannot even cover the first month's interest (100000 *
    #     6%/12 = 500) -> invalid
    wiz.add_rate_row("2024-02-01", "6.0")
    wiz.payment.setValue(100.00)
    ok, _msg = wiz.validate()
    assert not ok

    # (c) a real payment validates
    wiz.payment.setValue(700.00)
    ok, msg = wiz.validate()
    assert ok, msg

    # (d) a rate row dated before an earlier row -> ordering error
    wiz.add_rate_row("2023-01-01", "5.0")
    ok, msg = wiz.validate()
    assert not ok and "order" in msg.lower()

    # a never-saved wizard writes nothing
    assert ledger.get_account_by_name(conn, "Bad Loan") is None


def test_loan_setup_moved_out_of_settings_menu(qapp, tmp_path):
    """Loan Setup no longer lives in Settings (it moved to the loan register's
    toolbar). The window still builds and the Settings menu keeps its other
    entries."""
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow

    conn = db.init_db(tmp_path / "loanmenu.db")
    sample_data(conn)
    win = MainWindow(conn)
    settings = next(m.menu() for m in win.menuBar().actions()
                    if m.text().replace("&", "") == "Settings")
    labels = {a.text().replace("…", "").strip() for a in settings.actions()}
    assert "Loan Setup" not in labels        # relocated to the loan register
    assert "Reconcile to Statement" in labels  # other entries remain
    win.close()
    conn.close()


# ---- loan register fidelity: Increase/Decrease labels + split (Task 51) -----
def test_liability_register_uses_increase_decrease_labels(qapp, conn):
    """A liability/loan register labels the money columns Increase/Decrease with
    the correct sign mapping; a bank account keeps Payment/Deposit."""
    liab = ledger.create_account(conn, "Car Loan", "liability",
                                 opening_balance=-10_000_00)
    ledger.add_transaction(conn, liab, "2024-01-15", -300_00, payee="New charge")  # owe more
    ledger.add_transaction(conn, liab, "2024-02-01", 500_00, payee="Payment")      # owe less
    m = RegisterModel(conn, liab)

    # the money-column headers are relabelled for a liability
    assert m.headerData(RegisterModel.PAYMENT, Qt.Horizontal, Qt.DisplayRole) == "Increase"
    assert m.headerData(RegisterModel.DEPOSIT, Qt.Horizontal, Qt.DisplayRole) == "Decrease"

    def cell(r, c):
        return m.data(m.index(r, c), Qt.DisplayRole)

    charge = m.row_for_txn(next(x["id"] for x in m._rows if x["payee"] == "New charge"))
    payment = m.row_for_txn(next(x["id"] for x in m._rows if x["payee"] == "Payment"))
    # a charge / opening principal (negative) lands in the Increase column
    assert cell(charge, RegisterModel.PAYMENT) == "300.00"
    assert cell(charge, RegisterModel.DEPOSIT) == ""
    # a principal payment (positive) lands in the Decrease column
    assert cell(payment, RegisterModel.DEPOSIT) == "500.00"
    assert cell(payment, RegisterModel.PAYMENT) == ""

    # a bank account is unchanged
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    mc = RegisterModel(conn, chk)
    assert mc.headerData(RegisterModel.PAYMENT, Qt.Horizontal, Qt.DisplayRole) == "Payment"
    assert mc.headerData(RegisterModel.DEPOSIT, Qt.Horizontal, Qt.DisplayRole) == "Deposit"
    assert mc.uses_increase_decrease() is False
    assert m.uses_increase_decrease() is True


def _mortgage_account(conn):
    from mammon import loans
    aid = ledger.create_account(conn, "Home Mortgage", "liability",
                                opening_balance=-300_000_00)
    loans.set_loan_params(
        conn, aid, original_principal=300_000_00, term_months=360,
        payment_amount=1998_65, origination_date="2024-01-01", interval="monthly",
        rates=[("2024-02-01", "6.0")], extras=[("Escrow", 200_00, None)])
    return aid


def test_loan_payment_row_renders_computed_split(qapp, conn):
    """A loan payment with no stored split still renders principal/interest/escrow
    in its Category cell, computed via loans.payment_split."""
    aid = _mortgage_account(conn)
    ledger.add_transaction(conn, aid, "2024-02-01", 1998_65, payee="Servicer")
    m = RegisterModel(conn, aid)
    row = m.row_for_txn(m._rows[0]["id"])

    legs = dict(m.loan_payment_legs(row))
    assert legs["Interest"] == 1500_00           # 300000 * 6%/12
    assert legs["Escrow"] == 200_00
    assert legs["Principal"] == 1998_65 - 1500_00 - 200_00
    assert sum(m.loan_payment_legs(row)[i][1] for i in range(3)) == 1998_65

    # the Category cell shows the breakdown (interest + escrow are visible)
    cat = m.data(m.index(row, RegisterModel.CATEGORY), Qt.DisplayRole)
    assert "Interest" in cat and "1,500.00" in cat
    assert "Escrow" in cat and "200.00" in cat
    assert "Principal" in cat
    # the full breakdown is also the cell tooltip
    assert m.data(m.index(row, RegisterModel.CATEGORY), Qt.ToolTipRole) == cat
    # the computed-split category is read-only (not a free-typed category)
    assert not (m.flags(m.index(row, RegisterModel.CATEGORY)) & Qt.ItemIsEditable)


def test_loan_payment_row_renders_stored_split(qapp, conn):
    """A loan payment imported with a stored split (Task 52) renders those legs."""
    from mammon import importers

    aid = _mortgage_account(conn)
    importers.import_records(conn, [importers.NormalizedTxn(
        external_account="Home Mortgage", date="2024-02-01", amount_cents=1998_65,
        payee="Servicer", fitid="PMT-1")], provider="test")
    m = RegisterModel(conn, aid)
    row = m.row_for_txn(m._rows[0]["id"])
    cat = m.data(m.index(row, RegisterModel.CATEGORY), Qt.DisplayRole)
    # the imported split's own categories (Interest Exp / Escrow / Principal) show
    assert "Interest Exp" in cat and "Escrow" in cat and "Principal" in cat


def test_non_loan_liability_has_no_split_and_charge_is_not_split(qapp, conn):
    """A liability WITHOUT loan params gets the labels but no split rendering; and
    even on a loan account a charge (Increase) is never split."""
    plain = ledger.create_account(conn, "Perkins Loan", "liability",
                                  opening_balance=-5_000_00)
    ledger.add_transaction(conn, plain, "2024-02-01", 100_00, payee="Payment",
                           category_id=ledger.resolve_category(conn, "Loan Pmt"))
    m = RegisterModel(conn, plain)
    row = m.row_for_txn(m._rows[0]["id"])
    assert m.loan_payment_legs(row) == []
    # the real typed category still shows (no split hijacks it)
    assert m.data(m.index(row, RegisterModel.CATEGORY), Qt.DisplayRole) == "Loan Pmt"

    # on a loan account, a charge (negative -> Increase) is not treated as a payment
    aid = _mortgage_account(conn)
    ledger.add_transaction(conn, aid, "2024-02-05", -400_00, payee="Fee")
    lm = RegisterModel(conn, aid)
    crow = lm.row_for_txn(lm._rows[0]["id"])
    assert lm.loan_payment_legs(crow) == []


def test_loan_extra_principal_transfer_renders_as_transfer_not_split(qapp, conn):
    """An ADDITIONAL principal payment entered as a transfer from checking shows
    the TRANSFER ACCOUNT ([Checking]) in the loan register -- NOT a forced
    interest/escrow/principal split -- while a regular payment still splits. The
    engine still credits the extra as principal and shortens the loan."""
    from mammon import loans
    aid = _mortgage_account(conn)
    checking = ledger.create_account(conn, "Checking", "checking",
                                     opening_balance=50_000_00)
    # a regular scheduled payment (rendered as a computed split) ...
    ledger.add_transaction(conn, aid, "2024-02-01", 1998_65, payee="Servicer")
    # ... plus a $10,000 extra principal payment moved in from checking
    ledger.create_transfer(conn, checking, aid, "2024-02-10", 10_000_00)

    m = RegisterModel(conn, aid)
    xid = [r["id"] for r in m._rows if r["date"] == "2024-02-10"][0]
    xrow = m.row_for_txn(xid)
    # the extra principal transfer renders as the transfer account, no split legs
    assert m.loan_payment_legs(xrow) == []
    assert m.data(m.index(xrow, RegisterModel.CATEGORY), Qt.DisplayRole) == "[Checking]"

    # the regular payment still renders its computed interest/principal split
    pid = [r["id"] for r in m._rows if r["date"] == "2024-02-01"][0]
    prow = m.row_for_txn(pid)
    pcat = m.data(m.index(prow, RegisterModel.CATEGORY), Qt.DisplayRole)
    assert "Interest" in pcat and "Principal" in pcat

    # the extra principal shortens the term (credited as principal by the engine)
    sched = loans.amortization_schedule(conn, aid)
    assert sched[-1].balance == 0 and len(sched) < 360


# ---------------------------------------------------------------------------
# Account management: details dialog, toolbar, hide logic, accounts list,
# per-account import, double-click split (the user's account-management request).
# ---------------------------------------------------------------------------
def test_account_details_dialog_values_and_roundtrip(qapp, conn, accounts):
    from mammon.ui.widgets import AccountDetailsDialog
    chk, _ = accounts
    acct = ledger.get_account(conn, chk)
    dlg = AccountDetailsDialog(acct)
    dlg.url.setText("https://bank.example/login")
    dlg.account_number.setText("1234567-1")
    dlg.institution.setText("Anytown Credit Union")
    dlg.hidden.setChecked(True)
    v = dlg.values()
    assert v["url"] == "https://bank.example/login"
    assert v["account_number"] == "1234567-1"
    assert v["institution"] == "Anytown Credit Union"
    assert v["hidden"] == 1 and v["closed_flag"] == 0
    # the getter dict feeds ledger.update_account straight through
    ledger.update_account(conn, chk, **v)
    a = ledger.get_account(conn, chk)
    assert a["url"] == "https://bank.example/login"
    assert a["account_number"] == "1234567-1"
    assert a["hidden"] == 1


def test_account_details_dialog_prefills_existing(qapp, conn, accounts):
    from mammon.ui.widgets import AccountDetailsDialog
    chk, _ = accounts
    ledger.update_account(conn, chk, url="https://x", account_number="42", hidden=1)
    dlg = AccountDetailsDialog(ledger.get_account(conn, chk))
    assert dlg.url.text() == "https://x"
    assert dlg.account_number.text() == "42"
    assert dlg.hidden.isChecked() is True


def test_set_account_hidden_and_list_filtering(conn, accounts):
    chk, sav = accounts
    # both visible by default
    ids = {a["id"] for a in ledger.list_accounts(conn)}
    assert {chk, sav} <= ids
    ledger.set_account_hidden(conn, sav, True)
    visible = {a["id"] for a in ledger.list_accounts(conn)}
    assert sav not in visible and chk in visible
    # include_hidden brings it back
    all_ids = {a["id"] for a in ledger.list_accounts(conn, include_hidden=True)}
    assert sav in all_ids
    # unhide restores it to the default list
    ledger.set_account_hidden(conn, sav, False)
    assert sav in {a["id"] for a in ledger.list_accounts(conn)}


def test_account_bar_drops_hidden_account(qapp, conn, accounts):
    from mammon.ui.widgets import AccountBar
    chk, sav = accounts
    ledger.set_account_hidden(conn, sav, True)
    bar = AccountBar(conn)
    assert set(bar._item_by_account) == {chk}
    # net worth still counts the hidden account's balance (it is only hidden
    # from the bar, not excluded from the ledger)
    assert bar.model.net_worth() == ledger.net_worth(conn)


def test_account_toolbar_emits_account_id(qapp):
    from mammon.ui.widgets import AccountToolbar
    tb = AccountToolbar(7)
    seen = {}
    tb.detailsRequested.connect(lambda a: seen.setdefault("details", a))
    tb.reconcileRequested.connect(lambda a: seen.setdefault("reconcile", a))
    tb.importRequested.connect(lambda a: seen.setdefault("import", a))
    tb.downloadRequested.connect(lambda a: seen.setdefault("download", a))
    tb.hideRequested.connect(lambda a: seen.setdefault("hide", a))
    tb.act_details.trigger()
    tb.act_reconcile.trigger()
    tb.act_import.trigger()
    # Download is always enabled/clickable so its handler can run a preflight.
    assert tb.act_download.isEnabled() is True
    tb.act_download.trigger()
    tb.act_hide.trigger()
    assert seen == {"details": 7, "reconcile": 7, "import": 7,
                    "download": 7, "hide": 7}
    # The Accounts roster moved off the toolbar to the Tools menu.
    assert not hasattr(tb, "act_accounts")


def test_register_double_click_opens_split(qapp, conn, accounts):
    from PyQt5.QtWidgets import QAbstractItemView
    from mammon.ui.widgets import RegisterWidget
    from mammon.ui.models import RegisterModel
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2024-01-05", -25_00, payee="Store")
    w = RegisterWidget(conn, chk)
    # double-click is repurposed to open the split, so it is NOT an edit trigger
    assert not (w.view.editTriggers() & QAbstractItemView.DoubleClicked)
    calls = []
    w._split_row = lambda row: calls.append(row)
    assert not w.model.is_blank_row(0)
    # double-clicking a NON-category column no longer opens the split -- it keeps
    # that column's single-click edit and does nothing extra.
    w.view.doubleClicked.emit(w.model.index(0, RegisterModel.DATE))
    w.view.doubleClicked.emit(w.model.index(0, RegisterModel.MEMO))
    assert calls == []
    # ...only a double-click on the CATEGORY column routes to the split opener.
    w.view.doubleClicked.emit(w.model.index(0, RegisterModel.CATEGORY))
    assert calls == [0]


def test_register_single_click_edits_immediately(qapp, conn, accounts):
    """A single click on any editable field enters inline edit at once (no
    required second click), so Delete/typing act immediately."""
    from PyQt5.QtWidgets import QAbstractItemView, QLineEdit
    from mammon.ui.widgets import RegisterWidget
    from mammon.ui.models import RegisterModel
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2024-01-05", -25_00, payee="Store")
    w = RegisterWidget(conn, chk)
    assert w.view.state() != QAbstractItemView.EditingState
    # one click on the Payee cell opens its editor straight away
    w.view.clicked.emit(w.model.index(0, RegisterModel.PAYEE))
    assert w.view.state() == QAbstractItemView.EditingState
    editor = w.view.viewport().findChild(QLineEdit)
    assert editor is not None
    assert editor.text() == "Store"
    # the whole field is pre-selected, so the FIRST Delete (or any keystroke)
    # replaces the contents instead of nibbling one character.
    assert editor.selectedText() == "Store"


def test_register_category_single_click_defers_for_double_click(qapp, conn, accounts):
    """A single click on the Category column defers its edit past the
    double-click window so a genuine double-click opens the split instead; once
    the window passes the deferred edit fires."""
    from PyQt5.QtWidgets import QAbstractItemView
    from mammon.ui.widgets import RegisterWidget
    from mammon.ui.models import RegisterModel
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2024-01-05", -25_00, payee="Store")
    w = RegisterWidget(conn, chk)
    idx = w.model.index(0, RegisterModel.CATEGORY)
    # a single click arms the deferred edit but does NOT open an editor yet
    w.view.clicked.emit(idx)
    assert w.view.state() != QAbstractItemView.EditingState
    assert w._pending_edit == (0, RegisterModel.CATEGORY)
    # a double-click within the window cancels the deferred edit and splits
    calls = []
    w._split_row = lambda row: calls.append(row)
    w.view.doubleClicked.emit(idx)
    assert calls == [0]
    assert w._pending_edit is None
    assert not w._edit_timer.isActive()
    # a lone single click, once the window elapses, opens the editor
    w.view.clicked.emit(idx)
    w._begin_pending_edit()          # simulate the timer firing
    assert w.view.state() == QAbstractItemView.EditingState


def test_accounts_list_dialog_lists_all_and_toggles_hidden(qapp, conn, accounts):
    from mammon.ui.widgets import AccountsListDialog
    chk, sav = accounts
    ledger.set_account_hidden(conn, sav, True)
    dlg = AccountsListDialog(conn)
    # every account (including the hidden one) is listed
    assert dlg.table.rowCount() == 2
    ids = {r["id"] for r in dlg._rows}
    assert {chk, sav} == ids
    # select the hidden account and toggle it visible again
    hidden_row = next(i for i, r in enumerate(dlg._rows) if r["id"] == sav)
    dlg.table.setCurrentCell(hidden_row, 0)
    fired = []
    dlg.changed.connect(lambda: fired.append(True))
    dlg._toggle_hidden_selected()
    assert ledger.get_account(conn, sav)["hidden"] == 0
    assert fired == [True]


def test_import_asks_only_for_a_file_no_institution_picker(qapp, conn, tmp_path,
                                                           monkeypatch):
    """Import... goes straight to the file picker.

    The institution combo that used to front this was worse than useless: the
    caller read only the path and DISCARDED the chosen institution, so it asked a
    question and threw the answer away -- while implying the account's institution
    determined how the file would be read. Format is a property of the FILE
    (sniffed from its contents, column map inferred and correctable for delimited
    sources), never of the account it lands in."""
    from mammon.ui.widgets import MainWindow
    aid = ledger.create_account(conn, "AF Checking", "checking",
                                institution="Anytown Credit Union")
    f = tmp_path / "stmt.csv"
    f.write_text("Date,Description,Amount\n2026-07-01,STORE,-5.00\n", encoding="utf-8")

    from PyQt5.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    win = MainWindow(conn)
    seen = {}
    monkeypatch.setattr(win, "_choose_import_files",
                        lambda acct: (seen.setdefault("acct", acct), [str(f)])[1])
    ingested = []

    def _fake_ingest(account_id, acct_row, path, **kw):
        ingested.append((account_id, acct_row, path, kw))
        return {"parsed": 1, "inserted": 1, "prior": {}}

    monkeypatch.setattr(win, "_ingest_file_via_review", _fake_ingest)
    try:
        win._import_account(aid)
        assert ingested, "the chosen file should reach the review pipeline"
        assert ingested[0][2] == str(f)
        # every file of one import shares a batch, and reporting is deferred to
        # the caller so a multi-file import speaks once, not once per file
        assert ingested[0][3]["batch_id"] is not None
        assert ingested[0][3]["report"] is False
        # the account is passed for the dialog TITLE only -- nothing routes on it
        assert seen["acct"]["name"] == "AF Checking"
    finally:
        win.close()


def test_multiple_files_import_as_one_batch(qapp, conn, tmp_path, monkeypatch):
    """Institutions that will not export an arbitrary range are fetched a month
    per file. Those files are one import: one batch, one review, one report."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon import import_review
    from mammon.ui.widgets import MainWindow
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    aid = ledger.create_account(conn, "Piecemeal", "checking")
    files = []
    for month in ("01", "02", "03"):
        f = tmp_path / f"2026-{month}.csv"
        f.write_text(f"Date,Description,Amount\n2026-{month}-05,STORE,-5.00\n",
                     encoding="utf-8")
        files.append(str(f))

    win = MainWindow(conn)
    monkeypatch.setattr(win, "_choose_import_files", lambda acct: files)
    batches = []

    def _fake_ingest(account_id, acct_row, path, **kw):
        batches.append(kw.get("batch_id"))
        return {"parsed": 1, "inserted": 1, "prior": {}}

    monkeypatch.setattr(win, "_ingest_file_via_review", _fake_ingest)
    try:
        win._import_account(aid)
        assert len(batches) == 3                 # all three files ingested
        assert len(set(batches)) == 1            # under ONE batch id
        row = conn.execute(
            "SELECT file_count FROM import_batches WHERE id=?",
            (batches[0],)).fetchone()
        assert row[0] == 3
        assert import_review.current_batch_id(conn, aid) == batches[0]
    finally:
        win.close()


def test_import_cancelled_file_picker_does_nothing_quietly(qapp, conn, monkeypatch):
    """Cancelling the picker is not an error -- no warning, no import."""
    from mammon.ui.widgets import MainWindow
    from PyQt5.QtWidgets import QMessageBox
    aid = ledger.create_account(conn, "Checking2", "checking")
    win = MainWindow(conn)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a)))
    monkeypatch.setattr(win, "_choose_import_files", lambda acct: [])
    ingested = []
    monkeypatch.setattr(win, "_ingest_file_via_review",
                        lambda *a, **k: ingested.append(a))
    try:
        win._import_account(aid)
        assert ingested == [] and warned == []
    finally:
        win.close()


def test_main_window_toolbar_wiring_and_hide_flow(qapp, tmp_path, monkeypatch):
    """The account toolbar defers to the MainWindow: Account Details opens the
    dialog, and Hide Account confirms, hides, and drops the register off both the
    account bar and the open-register stack."""
    from PyQt5.QtWidgets import QDialog, QMessageBox
    from mammon.app import sample_data
    from mammon.ui.widgets import AccountDetailsDialog, MainWindow
    conn = db.init_db(tmp_path / "toolbar.db")
    sample_data(conn)
    win = MainWindow(conn)
    acct = next(a for a in ledger.list_accounts(conn) if a["type"] != "investment")
    aid = acct["id"]
    reg = win.open_register(aid)
    assert hasattr(reg, "toolbar")
    # Account Details… -> opens the dialog (cancelled here) without raising
    monkeypatch.setattr(AccountDetailsDialog, "exec_", lambda self: QDialog.Rejected)
    reg.toolbar.act_details.trigger()
    assert aid in win._registers  # cancelling changed nothing
    # Hide Account -> confirm 'Yes'; the account leaves the bar and the stack
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    reg.toolbar.act_hide.trigger()
    assert aid not in win._registers
    assert aid not in win.accounts._item_by_account
    assert ledger.get_account(conn, aid)["hidden"] == 1
    win.close()
    conn.close()


def test_backup_now_action_writes_a_manual_snapshot(qapp, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    from mammon import backup
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow

    bdir = tmp_path / "backups"
    monkeypatch.setattr(backup, "DEFAULT_BACKUP_DIR", bdir)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))

    path = tmp_path / "mammon_2026.db"
    conn = db.init_db(path)
    sample_data(conn)
    win = MainWindow(conn, db_path=str(path))

    file_menu = next(m.menu() for m in win.menuBar().actions()
                     if m.text().replace("&", "") == "File")
    labels = {act.text().replace("…", "").strip() for act in file_menu.actions()}
    assert "Back Up Database Now" in labels

    win._backup_now()
    manual = backup.list_backups(path, tag="manual", backup_dir=bdir)
    assert len(manual) == 1
    copy = db.connect(manual[0])
    assert ledger.list_accounts(copy)          # the snapshot carries the data
    copy.close()
    win.close()
    conn.close()


def test_autobackup_timer_ticks_and_rotates(qapp, tmp_path, monkeypatch):
    """The timer is armed for the session, a tick AFTER a change writes a
    snapshot, and closing stops it.

    A tick only snapshots when the database actually changed since the last one
    (see MainWindow._autobackup_tick): the timer fires on a fixed interval
    regardless of activity, so backing up unconditionally rewrote the whole file
    every minute of an idle session. Hence the edit below -- without it there is
    correctly nothing to back up."""
    from mammon import backup, ledger
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow

    bdir = tmp_path / "backups"
    monkeypatch.setattr(backup, "DEFAULT_BACKUP_DIR", bdir)

    path = tmp_path / "mammon_2026.db"
    conn = db.init_db(path)
    sample_data(conn)
    win = MainWindow(conn, db_path=str(path))

    # A ~1-minute repeating timer is armed for the session.
    assert win._autobackup_timer is not None
    assert win._autobackup_timer.isActive()
    assert win._autobackup_timer.interval() == backup.AUTO_INTERVAL_MS

    # Idle ticks write nothing at all.
    win._autobackup_tick()
    win._autobackup_tick()
    assert backup.list_backups(path, tag="auto", backup_dir=bdir) == []

    # A real edit makes the next tick snapshot.
    ledger.create_account(conn, "Brokerage", "investment")
    win._autobackup_tick()
    autos = backup.list_backups(path, tag="auto", backup_dir=bdir)
    assert len(autos) >= 1                     # the change was captured

    win.close()
    assert not win._autobackup_timer.isActive()  # closing stops it
    conn.close()


def test_no_autobackup_without_db_path(qapp, tmp_path):
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow
    conn = db.init_db(tmp_path / "nopath.db")
    sample_data(conn)
    win = MainWindow(conn)                      # no db_path -> nothing to name
    assert win._autobackup_timer is None
    win.close()
    conn.close()


def test_autobackup_startup_purges_stale_beyond_retention(qapp, tmp_path, monkeypatch):
    """Opening the app trims the auto-backup folder to the retention window, while
    the newest-N safety floor is preserved."""
    from datetime import datetime, timedelta
    from mammon import backup
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow

    bdir = tmp_path / "backups"
    bdir.mkdir()
    monkeypatch.setattr(backup, "DEFAULT_BACKUP_DIR", bdir)

    def _auto(when):
        p = bdir / f"mammon_2026.db.auto.{when.strftime('%Y%m%d_%H%M%S')}.bak"
        p.write_bytes(b"snapshot")
        return p

    now = datetime.now()
    # A stale snapshot from long before the window, plus enough fresh ones that
    # the stale file is not shielded by the newest-N floor.
    stale = _auto(now - timedelta(days=30))
    fresh = [_auto(now - timedelta(minutes=i + 1))
             for i in range(backup.AUTO_RETENTION_FLOOR + 1)]

    path = tmp_path / "mammon_2026.db"
    conn = db.init_db(path)
    sample_data(conn)
    win = MainWindow(conn, db_path=str(path))   # __init__ -> _start_autobackup -> purge

    assert not stale.exists()                   # startup cleanup removed it
    assert all(f.exists() for f in fresh)       # within-window snapshots kept
    win.close()
    conn.close()


# ---------------------------------------------------------------------------
# Automated Download feature: Tools menu, split Import/Download, dynamic form,
# gating on webSlinger + a script name + its inputData (NO credential gate).
# ---------------------------------------------------------------------------
_CHECKING_SCHEMA = {
    "success": True,
    "display_name": "GetCheckingTransactionsForRange",
    "description": "Login to bank (MFA), export checking transactions for a range.",
    "inputs": [
        {"description": "account number", "example": "1234567",
         "name": "userName", "type": ""},
        {"description": "Start date for transaction export", "example": "1/1/2026",
         "name": "startDate", "type": ""},
        {"description": "End date for transaction export", "example": "7/31/2026",
         "name": "endDate", "type": ""},
        {"description": "Export file format (e.g., CSV, OFX)",
         "example": "OFX File (.OFX)", "name": "format", "type": ""},
    ],
    "input_template": {"userName": "1234567", "startDate": "1/1/2026",
                       "endDate": "7/31/2026", "format": "OFX File (.OFX)"},
    "target_website": "https://anytowncu.example",
}

_OFX = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><ACCTID>1234567<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20260701<DTEND>20260710
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260705
<TRNAMT>-50.00
<FITID>AF-1001
<NAME>Smiths Marketplace
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260710
<TRNAMT>200.00
<FITID>AF-1002
<NAME>Payroll ACH
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>1150.00<DTASOF>20260710</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def _af_name():
    return "Anytown Credit Union"


def _fake_client(available=True, results=None):
    from mammon.webslinger import FakeWebSlingerClient
    return FakeWebSlingerClient(
        schemas={"GetCheckingTransactionsForRange": _CHECKING_SCHEMA},
        results=results or {}, available=available)


def test_tools_menu_has_accounts_and_toolbar_lacks_it(qapp, conn, accounts):
    from mammon.ui.widgets import MainWindow
    win = MainWindow(conn, webslinger=_fake_client())

    def menu(title):
        return next(m.menu() for m in win.menuBar().actions()
                    if m.text().replace("&", "") == title)

    assert "Accounts…" in [a.text() for a in menu("Tools").actions()]
    assert "Accounts…" not in [a.text() for a in menu("File").actions()]
    win.close()


def test_account_details_download_section_renders_fields(qapp, conn, accounts):
    import json
    from PyQt5.QtWidgets import QLineEdit
    from mammon.ui.widgets import AccountDetailsDialog
    chk, _ = accounts
    ledger.update_account(conn, chk, download_script="GetCheckingTransactionsForRange")
    dlg = AccountDetailsDialog(ledger.get_account(conn, chk), client=_fake_client())
    # auto-loaded on open (a script is set and a client is available). The load is
    # DEFERRED via QTimer.singleShot(0) so the dialog paints before the (formerly
    # blocking) describe_script call; flush the event loop to let it fire.
    qapp.processEvents()
    assert dlg._schema_rows is not None
    edits = dlg._schema_rows.findChildren(QLineEdit)
    assert len(edits) == 4                        # one per declared input
    # EDITABLE so the user can fill and SAVE the inputs (not a read-only preview).
    assert not any(e.isReadOnly() for e in edits)
    # Fill fields, then values() persists them as download_config JSON.
    dlg._field_editors["userName"].setText("1234567")
    dlg._field_editors["format"].setText("OFX File (.OFX)")
    v = dlg.values()
    assert v["download_script"] == "GetCheckingTransactionsForRange"
    cfg = json.loads(v["download_config"])
    assert cfg["userName"] == "1234567" and cfg["format"] == "OFX File (.OFX)"
    # Round-trips through ledger.update_account and re-prefills the form on reopen.
    ledger.update_account(conn, chk, **v)
    dlg2 = AccountDetailsDialog(ledger.get_account(conn, chk), client=_fake_client())
    qapp.processEvents()  # let the deferred auto-load fire (see above)
    assert dlg2._field_editors["userName"].text() == "1234567"
    assert dlg2._field_editors["format"].text() == "OFX File (.OFX)"


def test_account_details_array_field_persists_as_list(qapp, conn, accounts):
    """An array-typed input (e.g. subAccountName) entered as a lone value must be
    stored in download_config as a REAL JSON array, survive a save/reload of the
    Account Details dialog as a list (rendered as JSON text, never a repr'd
    string), and re-save as a list again."""
    import json
    from mammon.webslinger import FakeWebSlingerClient
    from mammon.ui.widgets import AccountDetailsDialog
    schema = {
        "success": True, "display_name": "GetCheckingTransactionsForRange",
        "description": "export sub-accounts",
        "inputs": [
            {"name": "userName", "description": "account number", "type": ""},
            {"name": "subAccountName", "description": "sub-accounts to export",
             "type": "array"},
        ],
        "input_template": {"userName": "1234567", "subAccountName": []},
        "target_website": "https://anytowncu.example",
    }

    def client():
        return FakeWebSlingerClient(
            schemas={"GetCheckingTransactionsForRange": schema},
            results={}, available=True)

    chk, _ = accounts
    ledger.update_account(conn, chk, download_script="GetCheckingTransactionsForRange")
    dlg = AccountDetailsDialog(ledger.get_account(conn, chk), client=client())
    qapp.processEvents()  # deferred auto-load (QTimer.singleShot(0)) fires here
    dlg._field_editors["subAccountName"].setText("Share Savings")  # a lone value
    v = dlg.values()
    cfg = json.loads(v["download_config"])
    assert cfg["subAccountName"] == ["Share Savings"]              # real JSON array
    assert isinstance(cfg["subAccountName"], list)
    # Reload: the saved list re-prefills the field as JSON text, not repr'd text.
    ledger.update_account(conn, chk, **v)
    dlg2 = AccountDetailsDialog(ledger.get_account(conn, chk), client=client())
    qapp.processEvents()  # let the deferred auto-load fire (see above)
    assert dlg2._field_editors["subAccountName"].text() == '["Share Savings"]'
    # Re-saving preserves the list type through the round trip.
    cfg2 = json.loads(dlg2.values()["download_config"])
    assert cfg2["subAccountName"] == ["Share Savings"]


def test_account_details_load_fields_hides_script_description(qapp, conn, accounts):
    """Regression: 'Load fields' must NOT render the webSlinger script's task
    DESCRIPTION -- that text is written for the script-generator LLM, is long,
    hides the actual input fields, and used to push the OK button off-screen.
    Only the input fields (plus a short user prompt) may appear, and the dialog
    must scroll so OK stays clickable however many fields a script declares."""
    from PyQt5.QtWidgets import QScrollArea, QDialogButtonBox
    from mammon.ui.widgets import AccountDetailsDialog
    chk, _ = accounts
    ledger.update_account(conn, chk, download_script="GetCheckingTransactionsForRange")
    dlg = AccountDetailsDialog(ledger.get_account(conn, chk), client=_fake_client())
    qapp.processEvents()  # deferred auto-load (QTimer.singleShot(0)) fires here
    # The script description brief must never be shown to the Mammon user.
    note = dlg._fields_note.text()
    assert _CHECKING_SCHEMA["description"] not in note
    assert "export checking transactions" not in note.lower()
    # ...but the input fields themselves are still rendered.
    assert dlg._schema_rows is not None
    # Fields live in a scroll area and the OK button is pinned outside it, so it
    # is always visible/clickable regardless of the number of fields.
    assert dlg.findChild(QScrollArea) is not None
    box = dlg.findChild(QDialogButtonBox)
    assert box is not None and box.button(QDialogButtonBox.Ok) is not None


def test_account_details_download_section_without_client(qapp, conn, accounts):
    from mammon.ui.widgets import AccountDetailsDialog
    chk, _ = accounts
    dlg = AccountDetailsDialog(ledger.get_account(conn, chk), client=None)
    dlg.download_script.setText("whatever")
    dlg._load_script_fields()
    assert dlg._schema_rows is None               # nothing to render without a client
    note = dlg._fields_note.text()
    assert "not configured" in note.lower()
    # The message must say EXACTLY what to configure and where -- not a dead end.
    assert ".claude.json" in note and "mcpServers" in note
    assert "MAMMON_WEBSLINGER_MCP_CMD" in note
    assert dlg.values()["download_script"] == "whatever"   # name still saved


def _capture_download_modal(monkeypatch):
    """Record (title, text) of every QMessageBox.information the Download flow
    raises and silence the dialog, so tests can assert the user-visible message
    for each missing-prerequisite branch."""
    from PyQt5.QtWidgets import QMessageBox
    seen = []
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda parent, title, text, *a, **k: seen.append((title, text))))
    return seen


def test_download_gate_enabled_when_fully_configured(qapp, conn):
    from mammon.ui.widgets import MainWindow
    chk = ledger.create_account(conn, "AF Checking", "checking")
    ledger.update_account(conn, chk, institution=_af_name(),
                          download_script="GetCheckingTransactionsForRange")
    win = MainWindow(conn, webslinger=_fake_client())
    reg = win.open_register(chk)
    assert reg.toolbar.act_download.isEnabled() is True
    win.close()


def test_download_button_always_enabled_even_when_unconfigured(qapp, conn):
    # Bug 6cfb82e4: Download must NEVER be a dead/greyed-out button. It stays
    # clickable in every setup state (no client, no creds, no script...) so its
    # click handler can always run and explain what's missing.
    from mammon.ui.widgets import MainWindow
    chk = ledger.create_account(conn, "AF Checking", "checking")  # nothing set up
    win = MainWindow(conn, webslinger=_fake_client(available=False))
    reg = win.open_register(chk)
    assert reg.toolbar.act_download.isEnabled() is True
    win.close()


def test_download_click_reports_missing_webslinger(qapp, conn, monkeypatch):
    from mammon.ui.widgets import MainWindow
    chk = ledger.create_account(conn, "AF Checking", "checking")
    ledger.update_account(conn, chk, institution=_af_name(),
                          download_script="GetCheckingTransactionsForRange")
    client = _fake_client(available=False)          # (1) webSlinger unavailable
    win = MainWindow(conn, webslinger=client)
    reg = win.open_register(chk)
    assert reg.toolbar.act_download.isEnabled() is True
    seen = _capture_download_modal(monkeypatch)
    win._download_account(chk)
    assert seen, "clicking Download must produce a visible message"
    title, text = seen[-1]
    assert "webslinger" in title.lower() and "available" in text.lower()
    assert client.calls == []                       # run flow NOT reached
    win.close()


def test_download_click_no_longer_blocks_on_api_key(qapp, conn, monkeypatch):
    # Bug fix: the API-key/credential gate is GONE. A reachable MCP + a
    # configured script + SAVED inputData must pass preflight with Mammon holding
    # NO secret of any kind -- webSlinger logs in with the credentials in the
    # user's own browser, so Mammon never handles a login secret on this path.
    import json
    from mammon.ui.widgets import MainWindow
    from mammon.webslinger import RunResult
    from PyQt5.QtWidgets import QMessageBox
    chk = ledger.create_account(conn, "AF Checking", "checking")
    ledger.update_account(conn, chk, institution=_af_name(),
                          download_script="GetCheckingTransactionsForRange",
                          download_config=json.dumps({"userName": "1234567"}))
    client = _fake_client(results={
        "GetCheckingTransactionsForRange": RunResult(records=[
            {"date": "2026-07-05", "description": "Store", "amount": "-9.99",
             "type": "debit", "fitid": "S-1"}])})
    win = MainWindow(conn, webslinger=client)
    reg = win.open_register(chk)
    assert reg.toolbar.act_download.isEnabled() is True
    seen = _capture_download_modal(monkeypatch)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    # The script declares a date range, so Download now prompts for it; accept it.
    from PyQt5.QtWidgets import QDialog
    from mammon.ui.widgets import DownloadDateDialog
    monkeypatch.setattr(DownloadDateDialog, "exec_", lambda self: QDialog.Accepted)
    win._download_account(chk)
    # No api-key/credential "setup incomplete" modal -- preflight passed straight
    # to the run flow, proven by describe + run reaching the client.
    assert not any("api key" in text.lower() or "credential" in text.lower()
                   for _t, text in seen)
    assert any(kind == "describe" for kind, *_ in client.calls)
    assert any(kind == "run" for kind, *_ in client.calls)
    win.close()


def test_download_click_reports_missing_input_data(qapp, conn, monkeypatch):
    # There is NO credential gate anymore. The remaining input gate: a script is
    # configured but its inputData was never filled in (no saved download_config).
    from mammon.ui.widgets import MainWindow
    chk = ledger.create_account(conn, "AF Checking", "checking")
    ledger.update_account(conn, chk, institution=_af_name(),
                          download_script="GetCheckingTransactionsForRange")
    client = _fake_client()                          # reachable, but no saved inputs
    win = MainWindow(conn, webslinger=client)
    reg = win.open_register(chk)
    assert reg.toolbar.act_download.isEnabled() is True
    seen = _capture_download_modal(monkeypatch)
    win._download_account(chk)
    assert seen and "input" in seen[-1][1].lower()
    assert "credential" not in seen[-1][1].lower()
    assert "api key" not in seen[-1][1].lower()
    assert client.calls == []                        # gate stops before the MCP
    win.close()


def test_download_click_reports_missing_script(qapp, conn, monkeypatch):
    from mammon.ui.widgets import MainWindow
    chk = ledger.create_account(conn, "AF Checking", "checking")
    ledger.update_account(conn, chk, institution=_af_name())   # (4) no script set
    client = _fake_client()
    win = MainWindow(conn, webslinger=client)
    reg = win.open_register(chk)
    assert reg.toolbar.act_download.isEnabled() is True
    seen = _capture_download_modal(monkeypatch)
    win._download_account(chk)
    assert seen
    text = seen[-1][1].lower()
    assert "script" in text and "account details" in text   # names the concrete step
    assert client.calls == []
    win.close()


def test_download_dialog_prefill_and_inputs(qapp):
    from mammon.ui.widgets import DownloadDialog
    from mammon.webslinger import ScriptSchema
    dlg = DownloadDialog(ScriptSchema.from_mcp(_CHECKING_SCHEMA),
                         prefill={"userName": "99"})
    vals = dlg.inputs()
    assert set(vals) == {"userName", "startDate", "endDate", "format"}
    assert vals["userName"] == "99"                 # prefill overrides the template
    assert vals["format"] == "OFX File (.OFX)"       # falls back to the template


def test_download_account_runs_script_and_routes_to_review(qapp, conn, monkeypatch):
    # The run RETURNS DATA (a scrape): the records payload is NO LONGER imported
    # straight into the account. It is classified and routed to the register's
    # import-review list; NOTHING enters the register until the user accepts
    # (MATCHING) or saves (NEW). Saved inputData is still passed to run_script.
    import json
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow
    from mammon.webslinger import RunResult
    chk = ledger.create_account(conn, "Anytown CU Checking", "checking",
                                opening_balance=1000_00, opening_date="2026-06-30")
    ledger.update_account(conn, chk, institution=_af_name(),
                          download_script="GetCheckingTransactionsForRange",
                          download_config=json.dumps({"userName": "1234567"}))
    client = _fake_client(results={
        "GetCheckingTransactionsForRange": RunResult(records=[
            {"date": "2026-07-05", "description": "Smiths", "amount": "-50.00",
             "type": "debit", "fitid": "AF-1001"},
            {"date": "2026-07-10", "description": "Payroll", "amount": "200.00",
             "type": "credit", "fitid": "AF-1002"}])})
    win = MainWindow(conn, webslinger=client)
    reg = win.open_register(chk)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    # GetCheckingTransactionsForRange declares a date range, so Download now
    # prompts for it -- accept with a fixed range instead of a live modal.
    import datetime
    from PyQt5.QtWidgets import QDialog
    from mammon.ui.widgets import DownloadDateDialog
    monkeypatch.setattr(DownloadDateDialog, "exec_", lambda self: QDialog.Accepted)
    monkeypatch.setattr(DownloadDateDialog, "dates",
                        lambda self: (datetime.date(2026, 7, 1),
                                      datetime.date(2026, 7, 31)))
    win._download_account(chk)
    run_call = next(c for c in client.calls if c[0] == "run")
    assert run_call[1] == "GetCheckingTransactionsForRange"
    assert run_call[2]["userName"] == "1234567"
    # The chosen range is sent, formatted to the script's example (%m/%d/%Y).
    assert run_call[2]["startDate"] == "07/01/2026"
    assert run_call[2]["endDate"] == "07/31/2026"
    # Review-first: the balance is UNCHANGED (nothing imported yet) and the two
    # downloaded rows are sitting in the register's review panel, pending.
    assert ledger.account_balance(conn, chk) == 1000_00
    # show_review un-hides the panel (the window itself isn't shown in tests, so
    # isHidden() is the reliable check rather than isVisible()).
    assert not reg.review_panel.isHidden()
    assert reg.review_panel.pending_count() == 2
    assert reg.review_panel.has_pending()
    # The Review… toolbar action is enabled while rows await action.
    assert reg.toolbar.act_review.isEnabled()
    # the chosen end date is remembered so next time defaults to end+1
    acct = ledger.get_account(conn, chk)
    assert json.loads(acct["download_config"])["_last_end"] == "2026-07-31"
    win.close()


_EXPORT_SCHEMA = {"success": True, "display_name": "GetExport",
                  "description": "drive the site's own export button",
                  "inputs": [{"name": "userName", "type": ""}],
                  "input_template": {"userName": "x"},
                  "target_website": "https://example.com"}

_EXPORT_OFX = """OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><ACCTID>0000<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260705<TRNAMT>-9.99<FITID>E-1<NAME>Store</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260710<TRNAMT>20.00<FITID>E-2<NAME>Refund</STMTTRN>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def _export_download_client(script="GetExport"):
    from mammon.webslinger import FakeWebSlingerClient, RunResult
    return FakeWebSlingerClient(
        schemas={script: _EXPORT_SCHEMA},
        results={script: RunResult(file_path=None, records=None)})  # -> EXPORT branch


def _drop_download(monkeypatch, tmp_path, name, text):
    """Drop a file into a fake ~/Downloads whose mtime is AFTER the run starts
    (newest_download only takes post-run files), and point default_downloads_dir
    at it."""
    import os
    from mammon import downloads
    ddir = tmp_path / "Downloads"
    ddir.mkdir(exist_ok=True)
    p = ddir / name
    p.write_text(text, encoding="utf-8")
    os.utime(p, (2_000_000_000, 2_000_000_000))       # far future -> always newest
    monkeypatch.setattr(downloads, "default_downloads_dir",
                        lambda *a, **k: str(ddir))
    return p


def test_download_account_export_file_routes_cash_to_review(
        qapp, conn, tmp_path, monkeypatch):
    """A webSlinger EXPORT run (site dropped a bank file, no records) must land in
    the register's review panel -- NOT straight in the register. Closes the
    EXPORT-file bypass for cash accounts."""
    import json
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow
    chk = ledger.create_account(conn, "AF Checking", "checking",
                                opening_balance=1000_00, opening_date="2026-06-30")
    ledger.update_account(conn, chk, download_script="GetExport",
                          download_config=json.dumps({"userName": "x"}))
    _drop_download(monkeypatch, tmp_path, "statement.ofx", _EXPORT_OFX)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    win = MainWindow(conn, webslinger=_export_download_client())
    reg = win.open_register(chk)
    win._download_account(chk)
    # Nothing imported; both exported rows sit in review, pending.
    assert ledger.account_balance(conn, chk) == 1000_00
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id=?", (chk,)
    ).fetchone()[0] == 0
    assert not reg.review_panel.isHidden()
    assert reg.review_panel.pending_count() == 2
    win.close()


def test_download_account_export_multiple_files_all_route_to_review(
        qapp, conn, tmp_path, monkeypatch):
    """A webSlinger EXPORT run that drops SEVERAL single-account files routes
    EVERY one through the register's review panel -- none straight to the
    register. Proves the UI loops over all dropped files, not just the newest."""
    import json
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow
    chk = ledger.create_account(conn, "AF Checking", "checking",
                                opening_balance=1000_00, opening_date="2026-06-30")
    ledger.update_account(conn, chk, download_script="GetExport",
                          download_config=json.dumps({"userName": "x"}))
    _drop_download(monkeypatch, tmp_path, "one.ofx", _EXPORT_OFX)
    # Fully DISTINCT rows (dedup is content-based, not fitid-based).
    two_ofx = (_EXPORT_OFX
               .replace("E-1", "E-9").replace("E-2", "E-10")
               .replace("-9.99", "-3.33").replace("20.00", "44.00")
               .replace("Store", "Cafe").replace("Refund", "Rebate")
               .replace("20260705", "20260715").replace("20260710", "20260716"))
    _drop_download(monkeypatch, tmp_path, "two.ofx", two_ofx)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    win = MainWindow(conn, webslinger=_export_download_client())
    reg = win.open_register(chk)
    win._download_account(chk)
    # Nothing imported; rows from BOTH files sit in review, pending.
    assert ledger.account_balance(conn, chk) == 1000_00
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id=?", (chk,)
    ).fetchone()[0] == 0
    assert not reg.review_panel.isHidden()
    assert reg.review_panel.pending_count() == 4      # 2 rows per file, all distinct
    win.close()


def test_download_account_export_investment_file_routes_via_review_pipeline(
        qapp, conn, tmp_path, monkeypatch):
    """A webSlinger EXPORT of an INVESTMENT file (buy/sell/div) goes through the
    review PIPELINE (classify + dedup + accept) into investment_transactions, not
    a straight bulk write; the cash register stays empty."""
    import json
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow
    broker = ledger.create_account(conn, "Broker", "investment")
    ledger.update_account(conn, broker, download_script="GetExport",
                          download_config=json.dumps({"userName": "x"}))
    _drop_download(monkeypatch, tmp_path, "activity.csv",
                   "Trade Date,Symbol,Shares,Price\n02/01/2026,AAPL,10,150.00\n")
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    win = MainWindow(conn, webslinger=_export_download_client())
    win.open_register(broker)
    win._download_account(broker)
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions").fetchone()[0] == 0     # no cash rows
    # Investment rows now WAIT IN REVIEW like cash ones: an importer's action and
    # security are guesses, so nothing reaches the register unreviewed.
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions").fetchone()[0] == 0
    from mammon import import_review as _ir
    pending = _ir.load_pending(conn, broker)
    assert len(pending) == 1
    assert pending[0].mapped.symbol == "AAPL"
    assert pending[0].mapped.quantity == "10"

    # Accepting from the register's pending row is what posts it.
    reg = win._registers[broker]
    assert reg.model.has_pending()
    reg._accept_pending()
    inv = conn.execute(
        "SELECT symbol, quantity, amount FROM investment_transactions").fetchall()
    assert len(inv) == 1 and inv[0]["symbol"] == "AAPL"
    assert inv[0]["quantity"] == "10" and inv[0]["amount"] == 150_000
    win.close()


def test_import_qif_menu_single_account_investment_routes_via_review_pipeline(
        qapp, conn, tmp_path, monkeypatch):
    """A single-account INVESTMENT QIF chosen from the QIF menu no longer bulk-
    imports straight to the register: it goes through the review pipeline into
    investment_transactions (dedup-protected)."""
    from PyQt5.QtWidgets import QFileDialog, QMessageBox
    from mammon.ui.widgets import MainWindow
    qif = ("!Account\nNRoll IRA\nTInvst\n^\n!Type:Invst\n"
           "D02/01'26\nNBuy\nYAAPL\nQ10\nI150.00\nT1500.00\n^\n")
    p = tmp_path / "roll.qif"
    p.write_text(qif, encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(p), "")))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    win = MainWindow(conn)
    win._import_qif_dialog()
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    inv = conn.execute(
        "SELECT symbol, quantity FROM investment_transactions").fetchall()
    assert len(inv) == 1 and inv[0]["symbol"] == "AAPL" and inv[0]["quantity"] == "10"
    win.close()


def _seed_review(conn, account_id, rows):
    """Build + persist review rows and return the reloaded pending entries."""
    from mammon import import_review
    entries = import_review.build_review(conn, account_id, rows)
    import_review.persist_entries(conn, account_id, entries)
    return import_review.load_pending(conn, account_id)


def test_review_list_is_read_only_except_num(qapp, conn, accounts):
    """The review list shows the imported data as-is and is read-only EXCEPT the
    Num column, which is editable free text (check number / 3rd-party label)."""
    from mammon.ui.import_review_widget import NUM
    from mammon.ui.widgets import RegisterWidget
    chk, _ = accounts
    entries = _seed_review(conn, chk, [
        {"transactionId": "RO-1", "postedDate": "2026-08-15", "amount": "30.00",
         "isDebit": True, "statementDescription": "POS COFFEE"}])
    reg = RegisterWidget(conn, chk)
    reg.show_review(entries)
    panel = reg.review_panel
    assert panel.table.rowCount() == 1
    for r in range(panel.table.rowCount()):
        for c in range(panel.table.columnCount()):
            item = panel.table.item(r, c)
            editable = bool(item.flags() & Qt.ItemIsEditable)
            assert editable == (c == NUM), (r, c)
    reg.deleteLater()


def test_review_list_has_no_category_column(qapp, conn, accounts):
    """Category was removed from the read-only review list: it is unused there
    (classification happens later in the register's pending row, not here)."""
    from mammon.ui.widgets import RegisterWidget
    chk, _ = accounts
    entries = _seed_review(conn, chk, [
        {"transactionId": "NC-1", "postedDate": "2026-08-15", "amount": "30.00",
         "isDebit": True, "statementDescription": "POS COFFEE"}])
    reg = RegisterWidget(conn, chk)
    reg.show_review(entries)
    panel = reg.review_panel
    headers = [panel.table.horizontalHeaderItem(c).text()
               for c in range(panel.table.columnCount())]
    assert "Category" not in headers
    assert headers == ["Status", "Date", "Num", "Payee", "Memo", "Amount"]
    reg.deleteLater()


def test_review_list_num_column_editable_and_persists_on_accept(qapp, conn,
                                                                accounts):
    """The review list shows an editable Num column (free text: check number,
    Venmo/Paypal label, 'Sched'). The imported value is shown, can be corrected
    during review, and the edit carries through to the accepted transaction."""
    from mammon.ui.import_review_widget import NUM
    from mammon.ui.widgets import RegisterWidget
    chk, _ = accounts
    entries = _seed_review(conn, chk, [
        {"transactionId": "NUM-1", "postedDate": "2026-08-15", "amount": "42.00",
         "isDebit": True, "statementDescription": "VENMO PAYMENT",
         "checkNumber": "555"}])
    reg = RegisterWidget(conn, chk)
    reg.show_review(entries)
    panel = reg.review_panel
    headers = [panel.table.horizontalHeaderItem(c).text()
               for c in range(panel.table.columnCount())]
    assert "Num" in headers
    item = panel.table.item(0, NUM)
    assert item.text() == "555"                     # imported num is visible
    assert item.flags() & Qt.ItemIsEditable         # and editable as free text
    item.setText("Venmo-1234")                      # user corrects it in review
    assert panel._entries[0].mapped.check_number == "Venmo-1234"
    # The edited Num carries through onto the accepted register transaction.
    txn_id = panel.accept_new(panel._entries[0], {})
    assert ledger.get_transaction(conn, txn_id)["num"] == "Venmo-1234"
    reg.deleteLater()


def test_pending_row_num_prefilled_editable_and_persists_on_accept(qapp, conn,
                                                                   accounts):
    """An imported check number pre-fills the register pending row's Num cell,
    which is editable there; the (corrected) value is written on accept."""
    from mammon import import_review
    from mammon.ui.import_review_widget import ImportReviewPanel
    chk, _ = accounts
    [entry] = import_review.build_review(conn, chk, [{
        "transactionId": "PN", "postedDate": "2026-08-15", "amount": "125.00",
        "isDebit": True, "statementDescription": "WATER UTILITY",
        "checkNumber": "8899"}])
    import_review.persist_entries(conn, chk, [entry])
    m = RegisterModel(conn, chk)
    m.set_pending(entry)
    prow = m.pending_row()
    idx = m.index(prow, RegisterModel.NUM)
    assert m.data(idx, Qt.DisplayRole) == "8899"       # imported check number shown
    assert m.flags(idx) & Qt.ItemIsEditable            # and editable in the register
    m.setData(idx, "9001", Qt.EditRole)                # user corrects it
    values = m.pending_values()
    assert values["num"] == "9001"
    panel = ImportReviewPanel(conn, chk)
    txn_id = panel.accept_new(panel._entries[0], values)
    assert ledger.get_transaction(conn, txn_id)["num"] == "9001"


def test_review_match_click_highlights_and_reveals_register_row(qapp, conn, accounts):
    """the user's rework #2: selecting a MATCHING review item selects + scrolls the
    existing register line into view (and clears any NEW pending row)."""
    from mammon.ui.widgets import RegisterWidget
    chk, _ = accounts
    existing = ledger.add_transaction(conn, chk, "2026-07-20", -2500, payee="Grocer")
    entries = _seed_review(conn, chk, [
        {"transactionId": "MC-N", "postedDate": "2026-07-05", "amount": "50.00",
         "isDebit": True, "statementDescription": "POS COFFEE"},          # NEW
        {"transactionId": "MC-M", "postedDate": "2026-07-21", "amount": "25.00",
         "isDebit": True, "statementDescription": "GROCER"}])             # MATCHING
    reg = RegisterWidget(conn, chk)
    reg.show_review(entries)
    panel = reg.review_panel
    # Row 0 is the NEW row -> the register opens an editable pending row.
    assert reg.model.has_pending()
    match_i = next(i for i, e in enumerate(panel._entries) if e.is_matching)
    panel.table.selectRow(match_i)
    # The existing matched line is now current + selected, and the transient NEW
    # pending row was torn down.
    expected = reg.model.row_for_txn(existing)
    assert expected >= 0
    assert reg.view.currentIndex().row() == expected
    assert expected in [ix.row() for ix in reg.view.selectionModel().selectedRows()]
    assert not reg.model.has_pending()
    reg.deleteLater()


def test_review_accept_auto_advances_to_next(qapp, conn, accounts):
    """Accepting a review item retires it and auto-advances the review selection
    to the next transaction still needing action.

    In the default visibility the accepted row STAYS on screen (greyed) rather
    than vanishing, so the advance moves DOWN the list instead of the next row
    sliding up into the vacated slot."""
    from mammon.ui.widgets import RegisterWidget
    chk, _ = accounts
    a = ledger.add_transaction(conn, chk, "2026-07-10", -1000, payee="A")
    ledger.add_transaction(conn, chk, "2026-07-11", -2000, payee="B")
    entries = _seed_review(conn, chk, [
        {"transactionId": "AA", "postedDate": "2026-07-10", "amount": "10.00",
         "isDebit": True, "statementDescription": "A STORE"},            # MATCHING a
        {"transactionId": "AB", "postedDate": "2026-07-11", "amount": "20.00",
         "isDebit": True, "statementDescription": "B STORE"}])           # MATCHING
    reg = RegisterWidget(conn, chk)
    reg.show_review(entries)
    panel = reg.review_panel
    assert panel.pending_count() == 2
    panel.table.selectRow(0)
    first = panel._entries[0]
    panel._on_accept()                       # Accept the selected (MATCHING) row
    # The first row is retired in place; the selection moved to the second.
    assert panel.pending_count() == 1
    assert first.is_actioned and panel._entries[0] is first
    assert panel.current_entry() is not None
    assert panel.current_entry() is not first
    assert panel._selected_index() == 1
    # The accepted MATCHING line was reconciled in the register.
    assert conn.execute("SELECT cleared FROM transactions WHERE id=?",
                        (a,)).fetchone()[0] == 1
    reg.deleteLater()


def test_review_new_item_editable_register_row_accept_button_and_enter(qapp, conn, accounts):
    """the user's rework #4: a NEW review item opens a fully-editable pending row in
    the register (date/payee/category/memo/amount), accepted via its inline
    Accept button OR by pressing Enter -- persisting it and shrinking the review."""
    from PyQt5.QtCore import QEvent
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtWidgets import QApplication
    from mammon.ui.widgets import RegisterWidget
    M = RegisterModel
    chk, _ = accounts
    entries = _seed_review(conn, chk, [
        {"transactionId": "NW-1", "postedDate": "2026-07-05", "amount": "50.00",
         "isDebit": True, "statementDescription": "POS COFFEE"},
        {"transactionId": "NW-2", "postedDate": "2026-07-06", "amount": "12.00",
         "isDebit": True, "statementDescription": "POS SNACK"}])
    reg = RegisterWidget(conn, chk)
    reg.show_review(entries)
    panel = reg.review_panel
    assert panel.pending_count() == 2

    # Row 0 (NEW) opened an editable pending register row with an Accept button.
    assert reg.model.has_pending()
    prow = reg.model.pending_row()
    assert prow >= 0
    assert reg._accept_btn is not None
    for col in (M.DATE, M.PAYEE, M.CATEGORY, M.MEMO, M.PAYMENT, M.DEPOSIT):
        assert reg.model.flags(reg.model.index(prow, col)) & Qt.ItemIsEditable

    # Edit ALL fields in the register (not the review list) and Accept via button.
    cat_id = ledger.resolve_category(conn, "Dining")
    reg.model.setData(reg.model.index(prow, M.DATE), "2026-07-07", Qt.EditRole)
    reg.model.setData(reg.model.index(prow, M.PAYEE), "Coffee Shop", Qt.EditRole)
    reg.model.setData(reg.model.index(prow, M.CATEGORY), "Dining", Qt.EditRole)
    reg.model.setData(reg.model.index(prow, M.MEMO), "latte", Qt.EditRole)
    reg.model.setData(reg.model.index(prow, M.PAYMENT), "55.00", Qt.EditRole)
    reg._accept_btn.click()

    saved = conn.execute(
        "SELECT date, amount, payee, memo, category_id, fitid FROM transactions "
        "WHERE fitid='NW-1'").fetchone()
    assert saved is not None
    assert saved["date"] == "2026-07-07"
    assert saved["amount"] == -5500          # edited amount, not the imported -5000
    assert saved["payee"] == "Coffee Shop"
    assert saved["memo"] == "latte"
    assert saved["category_id"] == cat_id

    # Auto-advanced to the second NEW row -> a fresh editable pending row + button.
    assert panel.pending_count() == 1
    assert reg.model.has_pending()
    assert reg._accept_btn is not None
    prow2 = reg.model.pending_row()
    reg.view.setCurrentIndex(reg.model.index(prow2, M.PAYEE))
    reg.view.selectRow(prow2)
    # Accept the second NEW row via the Enter key on the register view.
    QApplication.sendEvent(
        reg.view, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE fitid='NW-2'").fetchone()[0] == 1

    # Nothing left to action: the register's pending row is gone and the Review…
    # toolbar action re-syncs to disabled. The panel itself STAYS up, now showing
    # both rows greyed -- the user has just accepted them and can still see (and
    # undo) what they did. It is a reopened account, not a finished session, that
    # gets no panel: see reload_pending.
    assert panel.pending_count() == 0
    assert not reg.model.has_pending()
    assert not panel.isHidden()
    assert [e.state for e in panel._entries] == ["accepted", "accepted"]
    assert not panel.has_pending()
    assert not reg.toolbar.act_review.isEnabled()
    reg.deleteLater()


def test_review_accept_empty_memo_enter_idempotent_no_crash(qapp, conn, accounts):
    """Regression (task 264da340 fallout, the user's crash): editing a pending
    register row, clearing the memo, and pressing Enter must accept the row
    exactly once with NO RuntimeError -- and a second Enter is a harmless no-op
    (acceptance is idempotent). An empty memo is valid."""
    from PyQt5.QtCore import QEvent
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtWidgets import QApplication
    from mammon.ui.widgets import RegisterWidget
    M = RegisterModel
    chk, _ = accounts
    entries = _seed_review(conn, chk, [
        {"transactionId": "EM-1", "postedDate": "2026-07-05", "amount": "9.00",
         "isDebit": True, "statementDescription": "POS COFFEE"}])
    reg = RegisterWidget(conn, chk)
    reg.show_review(entries)
    panel = reg.review_panel
    assert panel.pending_count() == 1
    assert reg.model.has_pending()
    prow = reg.model.pending_row()
    assert reg._accept_btn is not None

    # Edit the memo, then clear it -- an emptied memo field must accept cleanly.
    reg.model.setData(reg.model.index(prow, M.MEMO), "typo", Qt.EditRole)
    reg.model.setData(reg.model.index(prow, M.MEMO), "", Qt.EditRole)

    # Put the selection on the pending row and press Enter -> accept once.
    reg.view.setCurrentIndex(reg.model.index(prow, M.PAYEE))
    reg.view.selectRow(prow)
    QApplication.sendEvent(
        reg.view, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))

    row = conn.execute(
        "SELECT memo FROM transactions WHERE fitid='EM-1'").fetchone()
    assert row is not None                       # accepted exactly once...
    assert not row["memo"]                        # ...with an empty memo
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE fitid='EM-1'").fetchone()[0] == 1
    # Nothing left pending; the row + Accept button were torn down.
    assert not reg.model.has_pending()
    assert reg._accept_btn is None

    # A second Enter is a harmless no-op: no crash, no duplicate row.
    QApplication.sendEvent(
        reg.view, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE fitid='EM-1'").fetchone()[0] == 1
    reg.deleteLater()


def test_accept_pending_survives_dangling_accept_button(qapp, conn, accounts):
    """Regression for the exact crash trace: a RegisterModel reset can delete the
    inline Accept button's C++ object while ``_accept_btn`` still holds the stale
    wrapper and the model still reports a pending row. _accept_pending must not
    call deleteLater() on the dead button (RuntimeError: wrapped C/C++ object of
    type QPushButton has been deleted) -- it accepts cleanly instead."""
    from mammon.ui.widgets import RegisterWidget, sip
    chk, _ = accounts
    entries = _seed_review(conn, chk, [
        {"transactionId": "DG-1", "postedDate": "2026-07-05", "amount": "9.00",
         "isDebit": True, "statementDescription": "POS COFFEE"}])
    reg = RegisterWidget(conn, chk)
    reg.show_review(entries)
    assert reg.model.has_pending()
    btn = reg._accept_btn
    assert btn is not None
    # Kill the C++ object out from under the still-set wrapper, exactly as a
    # model reset (begin/endResetModel dropping index widgets) would.
    sip.delete(btn)
    assert sip.isdeleted(reg._accept_btn)         # dangling wrapper, still pending
    assert reg.model.has_pending()
    reg._accept_pending()                          # old code: RuntimeError here
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE fitid='DG-1'").fetchone()[0] == 1
    assert not reg.model.has_pending()
    assert reg._accept_btn is None
    reg.deleteLater()


# ---- persisted review: sidebar dot + bulk controls + manual match ----------
def test_account_bar_pending_icon_reflects_review_items(qapp, conn, accounts):
    """The left account bar shows a pending-review dot for an account that has
    persisted pending review_items, and clears it once they are discarded."""
    from mammon import import_review
    from mammon.ui.widgets import AccountBar
    chk, sav = accounts
    bar = AccountBar(conn)
    assert bar.pending_icon_visible(chk) is False
    assert bar.pending_icon_visible(sav) is False

    entries = import_review.build_review(conn, chk, [{
        "transactionId": "S-1", "postedDate": "2026-08-15", "amount": "12.00",
        "isDebit": True, "statementDescription": "COFFEE"}])
    import_review.persist_entries(conn, chk, entries)
    bar.refresh()
    assert bar.pending_icon_visible(chk) is True        # count_pending>0 -> dot
    assert bar.pending_icon_visible(sav) is False

    import_review.discard_all(conn, chk)
    bar.refresh()
    assert bar.pending_icon_visible(chk) is False        # cleared after discard


def test_import_review_panel_bulk_accept_all(qapp, conn, accounts):
    """Accept All saves NEW rows + stamps MATCHING rows through the panel, then
    empties and hides the (offscreen) panel; pending count reaches 0."""
    from mammon import import_review
    from mammon.ui.import_review_widget import ImportReviewPanel
    chk, _ = accounts
    existing = ledger.add_transaction(conn, chk, "2026-08-20", -2500, payee="Grocer")
    entries = import_review.build_review(conn, chk, [
        {"transactionId": "PN-1", "postedDate": "2026-08-15", "amount": "30.00",
         "isDebit": True, "statementDescription": "COFFEE"},     # NEW
        {"transactionId": "PM-1", "postedDate": "2026-08-21", "amount": "25.00",
         "isDebit": True, "statementDescription": "GROCER"},     # MATCHING
    ])
    import_review.persist_entries(conn, chk, entries)

    panel = ImportReviewPanel(conn, chk)
    panel.set_entries(import_review.load_pending(conn, chk))
    assert panel.has_pending() is True
    assert panel.table.rowCount() == 2

    panel._on_accept_all()
    assert import_review.count_pending(conn, chk) == 0
    assert panel.table.rowCount() == 0
    assert panel.isHidden()                              # empty -> hidden
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE fitid='PN-1'").fetchone()[0] == 1
    assert conn.execute(
        "SELECT cleared FROM transactions WHERE id=?", (existing,)).fetchone()[0] == 1


def test_import_review_panel_reopen_shows_only_pending(qapp, conn, accounts):
    """reload_pending re-reads the DB: after one row is saved, the panel shows
    only the still-pending remainder and stays visible (offscreen: not hidden)."""
    from mammon import import_review
    from mammon.ui.import_review_widget import ImportReviewPanel
    chk, _ = accounts
    entries = import_review.build_review(conn, chk, [
        {"transactionId": "K-1", "postedDate": "2026-08-15", "amount": "30.00",
         "isDebit": True, "statementDescription": "COFFEE"},
        {"transactionId": "K-2", "postedDate": "2026-08-16", "amount": "8.00",
         "isDebit": True, "statementDescription": "SNACK"},
    ])
    import_review.persist_entries(conn, chk, entries)
    # Save the first row directly in the DB layer (threading its review_id).
    first = next(e for e in entries if e.mapped.transaction_id == "K-1")
    import_review.save_new(conn, chk, first.mapped, review_id=first.review_id)

    panel = ImportReviewPanel(conn, chk)
    panel.reload_pending()
    assert not panel.isHidden()                          # still has a pending row
    assert panel.table.rowCount() == 1
    assert panel._entries[0].mapped.transaction_id == "K-2"


def test_import_review_panel_manual_match(qapp, conn, accounts, monkeypatch):
    """Right-click Manual Match flips a NEW row to a manual MATCHING against a
    hand-picked register line; the offset warning is shown but non-blocking."""
    from mammon.ui import import_review_widget as irw
    from mammon import import_review
    from mammon.ui.import_review_widget import ImportReviewPanel, STATUS
    chk, _ = accounts
    # A register line 5 days off the downloaded row (outside the 3-day window ->
    # the offset warning fires; monkeypatched so it never blocks). Its SIGNED
    # amount equals the downloaded row (-7700) so the strict amount gate passes;
    # the point under test is the DATE-offset warning, not an amount mismatch.
    target = ledger.add_transaction(conn, chk, "2026-08-25", -7700, payee="Diner")
    entries = import_review.build_review(conn, chk, [{
        "transactionId": "MMU-1", "postedDate": "2026-08-20", "amount": "77.00",
        "isDebit": True, "statementDescription": "MYSTERY"}])
    import_review.persist_entries(conn, chk, entries)
    panel = ImportReviewPanel(conn, chk)
    panel.set_entries(import_review.load_pending(conn, chk))
    assert panel._entries[0].is_new

    warned = []
    monkeypatch.setattr(irw.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a)))
    panel.manual_match_index(0, chosen_id=target)
    assert warned                                        # >3-day offset warned
    e = panel._entries[0]
    assert e.is_matching and e.match_method == "manual" and e.matched_txn_id == target
    assert panel.table.item(0, STATUS).text() == "MATCHING"
    # persisted as a manual match, still pending (user still accepts it)
    row = conn.execute(
        "SELECT match_method, matched_txn_id, state FROM review_items WHERE id=?",
        (e.review_id,)).fetchone()
    assert row["match_method"] == "manual"
    assert row["matched_txn_id"] == target
    assert row["state"] == "pending"


# ---------------------------------------------------------------------------
# the user's six fixes: UI-level coverage
# ---------------------------------------------------------------------------
def test_review_panel_sits_below_button_row(qapp, conn, accounts):
    """Fix 5: the import-review list is added to the register layout AFTER the
    New/Edit/Split/Delete button row, not wedged between register and buttons."""
    from PyQt5.QtWidgets import QPushButton
    from mammon.ui.widgets import RegisterWidget
    chk, _ = accounts
    w = RegisterWidget(conn, chk)
    lay = w.layout()
    bar_idx = review_idx = None
    for i in range(lay.count()):
        item = lay.itemAt(i)
        if item.widget() is w.review_panel:
            review_idx = i
        sub = item.layout()
        if sub is not None:
            for j in range(sub.count()):
                wj = sub.itemAt(j).widget()
                if isinstance(wj, QPushButton) and wj.text() == "New…":
                    bar_idx = i
    assert bar_idx is not None and review_idx is not None
    assert review_idx > bar_idx


def test_open_register_scrolls_to_newest_once_per_session(qapp, conn, accounts,
                                                          monkeypatch):
    """Fix 6: first open of an account this session scrolls to the newest row;
    re-opening the same account does not re-scroll."""
    from mammon.ui.widgets import MainWindow, RegisterWidget
    chk, sav = accounts
    calls = []
    orig = RegisterWidget.scroll_to_newest
    monkeypatch.setattr(RegisterWidget, "scroll_to_newest",
                        lambda self: (calls.append(id(self)), orig(self)))
    win = MainWindow(conn)
    calls.clear()                       # ignore any auto-opened account on init
    reg = win.open_register(sav)        # first open of Savings this session
    assert calls == [id(reg)]
    win.open_register(sav)              # re-open: no second scroll
    assert calls == [id(reg)]
    win.close()


def test_category_tab_commits_picked_completion(qapp, conn, accounts):
    """Fix 3: Tab out of the category editor saves the picked completion, not the
    half-typed prefix hidden behind the completer popup."""
    from PyQt5.QtCore import QEvent
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtWidgets import QStyleOptionViewItem
    from mammon.ui.delegates import CategoryDelegate
    chk, _ = accounts
    ledger.resolve_category(conn, "Groceries")      # make it a real choice
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Store")
    m = RegisterModel(conn, chk)
    d = CategoryDelegate()
    idx = m.index(0, RegisterModel.CATEGORY)
    editor = d.createEditor(None, QStyleOptionViewItem(), idx)
    editor.setEditText("Groc")                      # a prefix, not the full name
    editor.completer().setCompletionPrefix("Groc")
    tab = QKeyEvent(QEvent.KeyPress, Qt.Key_Tab, Qt.NoModifier)
    d.eventFilter(editor, tab)
    assert editor.currentText() == "Groceries"      # completion accepted on Tab
    d.setModelData(editor, m, idx)
    _settle()
    assert m.txn_at(0)["category_label"] == "Groceries"


def test_category_tab_leaves_freshly_typed_new_category(qapp, conn, accounts):
    """Fix 3 guard: a typed brand-new path with no completion is NOT rewritten."""
    from PyQt5.QtCore import QEvent
    from PyQt5.QtGui import QKeyEvent
    from PyQt5.QtWidgets import QStyleOptionViewItem
    from mammon.ui.delegates import CategoryDelegate
    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Store")
    m = RegisterModel(conn, chk)
    d = CategoryDelegate()
    idx = m.index(0, RegisterModel.CATEGORY)
    editor = d.createEditor(None, QStyleOptionViewItem(), idx)
    editor.setEditText("Brand:New Path")
    editor.completer().setCompletionPrefix("Brand:New Path")
    d.eventFilter(editor, QKeyEvent(QEvent.KeyPress, Qt.Key_Tab, Qt.NoModifier))
    assert editor.currentText() == "Brand:New Path"


def _persist_two_review_rows(conn, account_id):
    from mammon import import_review
    entries = import_review.build_review(conn, account_id, [
        {"transactionId": "A", "postedDate": "2026-08-15", "amount": "5.00",
         "isDebit": True, "statementDescription": "COFFEE"},
        {"transactionId": "B", "postedDate": "2026-08-16", "amount": "6.00",
         "isDebit": True, "statementDescription": "LUNCH"}])
    import_review.persist_entries(conn, account_id, entries)


def _press_delete(panel):
    from PyQt5.QtCore import QEvent
    from PyQt5.QtGui import QKeyEvent
    panel.eventFilter(panel.table,
                      QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))


def test_review_panel_delete_key_discards_selected_row(qapp, conn, accounts):
    """The Delete key discards the selected pending review row.

    In the DEFAULT visibility ("Pending + accepted") a discarded row is retired
    IN PLACE -- it stays on screen, greyed and no longer actionable -- so the
    user can see what they threw away. What must drop is its PENDING status.
    """
    from mammon import import_review
    from mammon.ui.import_review_widget import ImportReviewPanel
    chk, _ = accounts
    _persist_two_review_rows(conn, chk)
    panel = ImportReviewPanel(conn, chk)             # auto-loads persisted pending
    assert len(panel._entries) == 2
    panel.table.selectRow(0)
    killed = panel._entries[0].mapped.transaction_id
    _press_delete(panel)

    still_listed = [e.mapped.transaction_id for e in panel._entries]
    assert still_listed == [killed, "B"]             # retired in place, not removed
    discarded = panel._entries[0]
    assert discarded.state == "discarded" and discarded.is_actioned
    assert import_review.count_pending(conn, chk) == 1
    # and the selection advanced to the row that still needs action
    assert panel.current_entry().mapped.transaction_id == "B"


def test_review_panel_delete_key_removes_row_in_pending_only_mode(qapp, conn,
                                                                  accounts):
    """In "Pending only" there is nowhere for a discarded row to go, so it leaves
    the list outright -- the pre-visibility-toggle behaviour, still correct for
    that one mode."""
    from mammon import import_review
    from mammon.ui import prefs
    from mammon.ui.import_review_widget import ImportReviewPanel
    chk, _ = accounts
    _persist_two_review_rows(conn, chk)
    prefs.set_review_visibility(chk, prefs.VIS_PENDING)
    panel = ImportReviewPanel(conn, chk)
    assert panel.visibility.currentData() == prefs.VIS_PENDING
    assert len(panel._entries) == 2
    panel.table.selectRow(0)
    gone = panel._entries[0].mapped.transaction_id
    _press_delete(panel)
    remaining = [e.mapped.transaction_id for e in panel._entries]
    assert gone not in remaining and len(remaining) == 1
    assert import_review.count_pending(conn, chk) == 1


def test_review_accept_transfer_via_bracket_category_creates_transfer(qapp, conn,
                                                                       accounts):
    """Fix 1 (UI wiring): accepting a NEW transfer row whose category names an
    account creates a real double-entry transfer and learns the mapping."""
    from mammon import import_review, transfer_rules
    from mammon.ui.import_review_widget import ImportReviewPanel
    chk, sav = accounts
    [entry] = import_review.build_review(conn, chk, [{
        "transactionId": "XT", "postedDate": "2026-08-15", "amount": "40.00",
        "isDebit": True, "statementDescription": "TRANSFER TO SAVINGS"}])
    import_review.persist_entries(conn, chk, [entry])
    panel = ImportReviewPanel(conn, chk)
    e = panel._entries[0]
    txn_id = panel.accept_new(e, {"date": "2026-08-15", "payee": "",
                                  "category": "[Savings]", "memo": "",
                                  "amount_cents": -40_00})
    t = ledger.get_transaction(conn, txn_id)
    assert t["transfer_account_id"] == sav
    assert transfer_rules.apply_rules(conn, "TRANSFER TO SAVINGS") == sav


def test_pending_transfer_row_prefills_learned_account(qapp, conn, accounts):
    """Fix 1 (prediction): a learned transfer rule pre-fills the pending row's
    Category cell with the '[Account]' target."""
    from mammon import import_review, transfer_rules
    chk, sav = accounts
    transfer_rules.upsert_rule(conn, "TRANSFER TO SAVINGS", sav)
    [entry] = import_review.build_review(conn, chk, [{
        "postedDate": "2026-08-15", "amount": "40.00", "isDebit": True,
        "statementDescription": "TRANSFER TO SAVINGS"}])
    m = RegisterModel(conn, chk)
    m.set_pending(entry)
    assert m.pending_values()["category"] == "[Savings]"


def test_copy_previous_split_creates_transfer_mirror(qapp, conn, accounts):
    """'Copy from previous <payee> split' repopulates the dialog from a prior
    same-payee split -- INCLUDING its [Account] transfer leg -- and saving must
    create a FRESH mirror in the target account for the new transaction. The copy
    path routes through ledger.set_splits (via lines_cents), not a raw insert, so
    the double-entry leg is always written. Regression for the Vanguard 401(k)
    paychecks whose imported split legs had no counterpart in the 401(k)."""
    from mammon.ui.widgets import SplitDialog
    chk, _ = accounts
    k401 = ledger.create_account(conn, "Vanguard 401K", "investment")
    salary = ledger.resolve_category(conn, "Salary")
    # A prior paycheck, already split with a 401(k) transfer leg (its own mirror).
    t1 = ledger.add_transaction(conn, chk, "2026-01-02", 2000_00, payee="Vanguard")
    ledger.set_splits(conn, t1, [
        {"category_id": salary, "amount": 2200_00},
        {"transfer_account_id": k401, "amount": -200_00, "memo": "deferral"},
    ])
    assert len(ledger.register_rows(conn, k401)) == 1
    # A new same-payee paycheck, not yet split.
    t2 = ledger.add_transaction(conn, chk, "2026-01-16", 2000_00, payee="Vanguard")

    m = RegisterModel(conn, chk)
    row = next(i for i in range(m.rowCount())
               if m.txn_at(i) is not None and m.txn_at(i)["id"] == t2)
    dlg = SplitDialog(m, row)
    assert dlg._prior_split                       # the copy button is offered
    dlg._copy_previous_split()                    # emulate clicking it
    assert dlg.apply_split()                      # OK -> ledger.set_splits

    # t2 got its OWN reciprocal mirror in the 401(k); the prior one still stands.
    krows = ledger.register_rows(conn, k401)
    assert len(krows) == 2
    assert all(r["amount"] == 200_00 and r["category_label"] == "[Checking]"
               for r in krows)
    # t2's transfer leg is linked to a mirror -- not left orphan.
    pair = conn.execute("SELECT transfer_pair_id FROM splits WHERE transaction_id=? "
                        "AND transfer_account_id=?", (t2, k401)).fetchone()[0]
    assert pair is not None


# ---------------------------------------------------------------------------
# QIF menu import routing: a SINGLE cash account's QIF goes through the review
# list (treated like a download -- its transfer legs create the counterparty
# mirror on accept, matches prevent double-entry); a QIF carrying BOTH accounts
# of a transfer imports in bulk, collapsing the two legs into one linked pair.
# ---------------------------------------------------------------------------
_QIF_MENU_SINGLE = """!Account
NAF Checking
TBank
^
!Type:Bank
D01/05'26
T-25.00
PSafeway
LGroceries
^
D01/15'26
PTransfer to savings
L[AF Savings]
T-40.00
^
"""


def test_import_qif_menu_single_account_routes_to_review(qapp, conn, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QFileDialog, QMessageBox
    from mammon.ui.widgets import MainWindow
    from mammon import import_review
    p = tmp_path / "checking.qif"
    p.write_text(_QIF_MENU_SINGLE, encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(p), "")))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    win = MainWindow(conn)
    win._import_qif_dialog()
    # Nothing entered the register yet -- both rows sit in the review list.
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    chk = conn.execute("SELECT id FROM accounts WHERE name='AF Checking'").fetchone()["id"]
    assert import_review.count_pending(conn, chk) == 2
    reg = win._registers.get(chk)
    assert reg is not None and not reg.review_panel.isHidden()
    assert reg.review_panel.pending_count() == 2
    win.close()


def test_import_qif_menu_multi_account_imports_directly(qapp, conn, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QFileDialog, QMessageBox
    from mammon.ui.widgets import MainWindow
    qif = (_QIF_MENU_SINGLE
           + "!Account\nNAF Savings\nTBank\n^\n"
             "!Type:Bank\nD01/15'26\nL[AF Checking]\nT40.00\n^\n")
    p = tmp_path / "both.qif"
    p.write_text(qif, encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(p), "")))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    win = MainWindow(conn)
    win._import_qif_dialog()
    # Both legs present -> bulk import wrote a single linked pair straight in
    # (no review list involved).
    rows = conn.execute(
        "SELECT transfer_pair_id FROM transactions WHERE transfer_account_id IS NOT NULL"
    ).fetchall()
    assert len(rows) == 2 and all(r["transfer_pair_id"] is not None for r in rows)
    win.close()


def test_autobackup_after_the_first_is_an_incremental_delta(qapp, tmp_path,
                                                            monkeypatch):
    """The session's auto-snapshots go through the delta path, not just the
    backup primitive: the first tick lays down a baseline and every tick after it
    stores only the changed pages, which is what keeps a 1/minute cadence from
    filling the folder with near-identical copies of the whole ledger."""
    from mammon import backup, ledger
    from mammon.app import sample_data
    from mammon.ui.widgets import MainWindow

    bdir = tmp_path / "backups"
    monkeypatch.setattr(backup, "DEFAULT_BACKUP_DIR", bdir)
    path = tmp_path / "mammon_2026.db"
    conn = db.init_db(path)
    sample_data(conn)
    win = MainWindow(conn, db_path=str(path))

    ledger.create_account(conn, "Brokerage", "investment")
    win._autobackup_tick()
    ledger.add_transaction(conn, ledger.list_accounts(conn)[0]["id"],
                           "2026-08-18", -42_00, payee="Delta Probe")
    win._autobackup_tick()

    autos = backup.list_backups(path, tag="auto", backup_dir=bdir)
    assert len(autos) == 2
    assert autos[0].suffix == backup.FULL_EXT       # baseline
    assert autos[1].suffix == backup.DELTA_EXT      # incremental
    assert autos[1].stat().st_size < autos[0].stat().st_size

    # And the delta is a real restore point: it rebuilds a sound database that
    # contains the edit made after the baseline was taken.
    import sqlite3
    out = backup.restore_backup(autos[1], tmp_path / "restored.db")
    c = sqlite3.connect(str(out))
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT COUNT(*) FROM transactions "
                     "WHERE payee='Delta Probe'").fetchone()[0] == 1
    c.close()
    win.close()
    conn.close()


# ---- reconcile: what "Cleared balance" means, and drafts -------------------
def test_card_reconcile_ignores_the_registers_reconciled_history(qapp, conn):
    """A card statement is self-contained: its own figures imply what was owed
    when it opened, and THAT is what the checked items are measured against. The
    account's R rows are never consulted -- this card carries $3,142.00 of
    imported Quicken history whose only effect must be none at all."""
    from mammon.ui.widgets import ReconcileDialog

    cc = ledger.create_account(conn, "Card", "credit", opening_balance=0)
    hist = ledger.add_transaction(conn, cc, "2025-11-24", -3_142_00, payee="Old")
    ledger.update_transaction(conn, hist, cleared=1, reconciled=1)
    charge = ledger.add_transaction(conn, cc, "2026-08-10", -100_00, payee="New")
    pay = ledger.add_transaction(conn, cc, "2026-08-12", 30_00, payee="Payment")

    dlg = ReconcileDialog(conn, cc)
    # Statement: owed 500 before, 100 of charges, 30 paid -> ending owed 570.
    dlg.set_credit_balances({"date": "2026-08-22", "charges": 100_00,
                             "payments": 30_00, "credits": 0, "ending": 570_00,
                             "finance_charge": 0, "finance_category": ""})
    # 570 - 100 - 0 + 30 + 0 = 500 owed at the start, register-signed.
    assert dlg.beginning_cents == -500_00
    assert "implied by the statement" in dlg.detail_label.text()
    assert "-$500.00" in dlg.detail_label.text()

    # 'Cleared balance' is the checked items and nothing else.
    dlg.clear_all()
    assert "Cleared balance: $0.00" in dlg.summary_label.text()
    assert not dlg.finish_btn.isEnabled()
    # Checking the statement's two items closes it -- the $3,142.00 of R history
    # never enters the arithmetic.
    dlg.mark_all()
    assert "Cleared balance: -$70.00" in dlg.summary_label.text()
    assert "Difference: $0.00" in dlg.summary_label.text()
    assert dlg.finish_btn.isEnabled()
    dlg._finish()
    assert dlg.finished_ok
    assert ledger.get_transaction(conn, charge)["reconciled"] == 1
    assert ledger.get_transaction(conn, pay)["reconciled"] == 1


def test_bank_reconcile_still_uses_reconciled_history(qapp, conn, accounts):
    """The card change is card-only: a bank account keeps deriving its beginning
    from opening + already-reconciled rows."""
    from mammon.ui.widgets import ReconcileDialog

    chk, _ = accounts                                # opening 100_00
    a = ledger.add_transaction(conn, chk, "2026-01-05", -25_00)
    ledger.update_transaction(conn, a, cleared=1, reconciled=1)
    dlg = ReconcileDialog(conn, chk)
    dlg.set_balances("2026-02-28", 75_00, 75_00)
    assert "already reconciled" in dlg.detail_label.text()
    assert "$75.00" in dlg.detail_label.text()       # 100_00 opening - 25_00 R


def test_reconcile_clear_all_leaves_no_unreachable_cleared_rows(qapp, conn):
    """Rows dated after the statement date are in neither pane, so Clear All
    cannot reach them. They must therefore not count toward the statement -- they
    are reported separately as held for the next one."""
    from mammon.ui.widgets import ReconcileDialog

    cc = ledger.create_account(conn, "Card", "credit", opening_balance=0)
    inside = ledger.add_transaction(conn, cc, "2026-01-10", -50_00, payee="Charge")
    later = ledger.add_transaction(conn, cc, "2026-07-23", 1_275_00, payee="Payment")
    ledger.update_transaction(conn, inside, cleared=1)
    ledger.update_transaction(conn, later, cleared=1)

    dlg = ReconcileDialog(conn, cc)
    dlg.set_credit_balances({"date": "2026-01-25", "charges": 50_00,
                             "payments": 0, "credits": 0, "ending": 50_00,
                             "finance_charge": 0, "finance_category": ""})
    assert dlg.beginning_cents == 0                 # 50 - 50 = nothing owed before
    assert dlg.credits_table.rowCount() == 0        # the July payment is hidden
    dlg.clear_all()
    assert "Cleared balance: $0.00" in dlg.summary_label.text()
    assert "held for the next statement" in dlg.detail_label.text()
    # The hidden row keeps its mark -- it was never this statement's business.
    assert ledger.get_transaction(conn, later)["cleared"] == 1


def test_reconcile_reopen_restores_statement_inputs(qapp, conn):
    """Closing the window to check something in the register and reopening must
    not cost the user the statement figures (or duplicate the finance charge)."""
    from mammon.ui.widgets import ReconcileDialog

    cc = ledger.create_account(conn, "Card", "credit", opening_balance=0)
    ledger.add_transaction(conn, cc, "2026-03-05", -120_00, payee="Store")

    first = ReconcileDialog(conn, cc)
    assert first.has_draft is False                  # nothing saved yet -> prompt
    first.set_credit_balances({"date": "2026-03-31", "charges": 120_00,
                               "payments": 40_00, "credits": 7_00,
                               "ending": 90_00, "finance_charge": 10_00,
                               "finance_category": "Interest Exp"})
    fc_id = first._finance_charge_id
    first.close()

    # Reopening restores every input, so the caller skips the setup prompt.
    again = ReconcileDialog(conn, cc)
    assert again.has_draft is True
    assert again.statement_date == "2026-03-31"
    assert again.ending_cents == -90_00
    assert (again._charges_cents, again._payments_cents, again._credits_cents) == \
        (120_00, 40_00, 7_00)
    assert again._finance_cents == 10_00
    assert again._finance_category == "Interest Exp"
    # The finance charge is the SAME row, not a second one posted on reopen.
    assert again._finance_charge_id == fc_id
    again.set_credit_balances({"date": "2026-03-31", "charges": 120_00,
                               "payments": 40_00, "credits": 7_00,
                               "ending": 90_00, "finance_charge": 10_00,
                               "finance_category": "Interest Exp"})
    charges = [r for r in ledger.register_rows(conn, cc)
               if r["payee"] == "Finance Charge"]
    assert len(charges) == 1


def test_reconcile_draft_is_dropped_once_finished(qapp, conn, accounts):
    from mammon.ui.widgets import ReconcileDialog

    chk, _ = accounts                                # opening 100_00
    a = ledger.add_transaction(conn, chk, "2026-01-05", -25_00, payee="Store")
    ledger.update_transaction(conn, a, cleared=1)
    dlg = ReconcileDialog(conn, chk)
    dlg.set_balances("2026-01-31", 100_00, 75_00)
    assert ledger.get_reconcile_draft(conn, chk) is not None
    dlg._finish()
    assert dlg.finished_ok
    # Finished: the next reconcile starts from the setup prompt again.
    assert ledger.get_reconcile_draft(conn, chk) is None
    assert ReconcileDialog(conn, chk).has_draft is False


def test_credit_setup_dialog_shows_the_credits_field(qapp, conn):
    """The Credits box round-tripped through values() but was never added to the
    form, so a credits figure typed on the statement had nowhere to go."""
    from mammon.ui.widgets import CreditReconcileStartDialog

    ledger.create_account(conn, "Visa", "credit", opening_balance=0)
    dlg = CreditReconcileStartDialog(
        conn, "2026-01-31", 0, 0, 0, 0, 0, "Interest Exp", account_name="Visa")
    assert dlg.credits.parent() is not None          # actually laid out now
    assert dlg.credits.isVisibleTo(dlg)


def test_bank_reconcile_flags_a_beginning_balance_mismatch(qapp, conn, accounts):
    """A bank account's running balance must carry forward unbroken, so the
    register's beginning keeps driving the math and the typed one is a check. A
    disagreement means previously-reconciled history moved -- surfaced, not
    silently absorbed, and not silently discarded either (which is what the field
    used to do)."""
    from mammon.ui.widgets import ReconcileDialog

    chk, _ = accounts                                # opening 100_00
    a = ledger.add_transaction(conn, chk, "2026-01-05", -25_00)
    ledger.update_transaction(conn, a, cleared=1, reconciled=1)

    dlg = ReconcileDialog(conn, chk)
    dlg.set_balances("2026-02-28", 75_00, 75_00)     # agrees: 100_00 - 25_00
    assert "reconciled history has changed" not in dlg.detail_label.text()

    dlg.set_balances("2026-02-28", 90_00, 75_00)     # statement says otherwise
    assert "reconciled history has changed" in dlg.detail_label.text()
    assert "$90.00" in dlg.detail_label.text()
    # The math is unmoved: the register's beginning still rules.
    assert "$75.00" in dlg.detail_label.text()
    assert "Difference: $0.00" in dlg.summary_label.text()
    assert dlg.finish_btn.isEnabled()                # flagged, never blocked


def test_reconcile_rejects_a_malformed_statement_date(qapp, conn):
    """Every reconcile bound is a string comparison on ISO text, so a malformed
    date does not raise -- it sorts after every real row and silently widens the
    statement to months that are not on it. A live ledger stored
    '2026-03-152025-11-04' (typed into a pre-filled field without selecting
    first) and reported four payments from other months as cleared."""
    from mammon.ui.widgets import ReconcileDialog

    cc = ledger.create_account(conn, "Card", "credit", opening_balance=0)
    later = ledger.add_transaction(conn, cc, "2026-07-23", 1_275_00, payee="Pay")
    ledger.update_transaction(conn, later, cleared=1)

    dlg = ReconcileDialog(conn, cc)
    dlg.set_credit_balances({"date": "2026-03-152025-11-04", "charges": 0,
                             "payments": 0, "credits": 0, "ending": 0,
                             "finance_charge": 0, "finance_category": ""})
    # Dropped to blank -- visibly unbounded rather than plausibly bounded.
    assert dlg.statement_date == ""
    # A good date bounds it, and the July payment falls outside December.
    dlg.set_credit_balances({"date": "2025-12-28", "charges": 0,
                             "payments": 0, "credits": 0, "ending": 0,
                             "finance_charge": 0, "finance_category": ""})
    assert dlg.statement_date == "2025-12-28"
    assert "Cleared balance: $0.00" in dlg.summary_label.text()
    assert "cleared after" in dlg.detail_label.text()


def test_statement_date_field_cannot_hold_a_non_date(qapp, conn):
    """The date control is a calendar field, not free text: there is nothing to
    append to and no way to hand the bounds a non-date."""
    from mammon.ui.widgets import CreditReconcileStartDialog, ReconcileStartDialog

    ledger.create_account(conn, "Visa", "credit", opening_balance=0)
    card = CreditReconcileStartDialog(conn, "2025-12-28", 0, 0, 0, 0, 0, "")
    assert card.values()["date"] == "2025-12-28"
    bank = ReconcileStartDialog("2026-01-31", 0, 0)
    assert bank.values()["date"] == "2026-01-31"


def test_reconcile_grays_cleared_rows(qapp, conn, accounts):
    """The Clr mark is at the far left and the amount at the far right, so with
    two identical amounts it is easy to re-click the row already cleared. A
    dimmed row answers 'have I done this one?' at the amount itself."""
    from mammon.ui.widgets import ReconcileDialog

    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-02-01", -7_28, payee="First")
    ledger.add_transaction(conn, chk, "2026-02-02", -7_28, payee="Second")
    dlg = ReconcileDialog(conn, chk)
    dlg.set_balances("2026-02-28", 100_00, 85_44)

    def colors(row):
        t = dlg.debits_table
        return {t.item(row, c).foreground().color().name()
                for c in range(t.columnCount()) if c != ReconcileDialog.CLR}

    dimmed = ReconcileDialog._CLEARED_ROW.name()
    assert colors(0) != {dimmed} and colors(1) != {dimmed}

    dlg._toggle_at(dlg.debits_table, 0)              # clear the FIRST $7.28
    assert colors(0) == {dimmed}                     # grayed end to end
    assert colors(1) != {dimmed}                     # the other one still stands out
    # The Clr glyph keeps its own green rather than being dimmed with the row.
    green = dlg.debits_table.item(0, ReconcileDialog.CLR).foreground().color().name()
    assert green != dimmed

    dlg._toggle_at(dlg.debits_table, 0)              # unclearing restores it
    assert colors(0) != {dimmed}


def test_reconcile_graying_survives_a_pane_rebuild(qapp, conn, accounts):
    """Mark All / Clear All refill the panes, so the paint must come from the
    fill path too, not only from the click path."""
    from mammon.ui.widgets import ReconcileDialog

    chk, _ = accounts
    ledger.add_transaction(conn, chk, "2026-02-01", -7_28, payee="First")
    ledger.add_transaction(conn, chk, "2026-02-02", -7_28, payee="Second")
    dlg = ReconcileDialog(conn, chk)
    dlg.set_balances("2026-02-28", 100_00, 85_44)
    dimmed = ReconcileDialog._CLEARED_ROW.name()

    dlg.mark_all()
    t = dlg.debits_table
    for row in range(t.rowCount()):
        assert t.item(row, ReconcileDialog.AMOUNT).foreground().color().name() == dimmed
    dlg.clear_all()
    for row in range(t.rowCount()):
        assert t.item(row, ReconcileDialog.AMOUNT).foreground().color().name() != dimmed


def test_account_bar_gives_credit_cards_their_own_heading(qapp, conn):
    """Cards used to sit unlabelled at the foot of Banking, the boundary implied
    only by ordering. The heading makes it a glance instead of a scan."""
    from PyQt5.QtWidgets import QLabel

    from mammon.ui.widgets import AccountBar, _BAR_GROUPS

    ledger.create_account(conn, "Checking", "checking", opening_balance=100_00)
    ledger.create_account(conn, "Visa", "credit", opening_balance=0)
    ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)

    bar = AccountBar(conn)
    titles = [lbl.text() for lbl in bar.findChildren(QLabel)
              if lbl.objectName() == "acctGroupTitle"]
    assert "Credit Card" in titles
    # Cards sit directly below the bank accounts, as in Quicken.
    assert titles.index("Credit Card") == titles.index("Banking") + 1
    assert titles.index("Investing") > titles.index("Credit Card")
    # 'credit' belongs to exactly one group, so an account cannot be listed twice.
    owners = [t for t, types in _BAR_GROUPS if "credit" in types]
    assert owners == ["Credit Card"]


def test_account_bar_omits_a_heading_with_no_accounts(qapp, conn):
    """An empty group draws nothing, so a user with no cards sees no Credit Card
    header (the pre-existing rule, re-asserted now that a group can be empty)."""
    from PyQt5.QtWidgets import QLabel

    from mammon.ui.widgets import AccountBar

    ledger.create_account(conn, "Checking", "checking", opening_balance=100_00)
    bar = AccountBar(conn)
    titles = [lbl.text() for lbl in bar.findChildren(QLabel)
              if lbl.objectName() == "acctGroupTitle"]
    assert titles == ["Banking"]


# ---- gear menu (account actions move off the toolbar row) -------------------
def test_register_actions_live_in_a_gear_menu_not_a_toolbar_row(qapp, conn, accounts):
    """Every account action opens a dialog, so a whole toolbar row of buttons
    that only lead elsewhere is a poor trade for register height. They move to a
    gear beside the account name; the toolbar still OWNS the actions (and their
    account-id wiring), it is simply never shown."""
    from mammon.ui.widgets import RegisterWidget

    chk, _ = accounts
    reg = RegisterWidget(conn, chk)
    assert reg.toolbar.isVisible() is False
    labels = [a.text() for a in reg.gear_menu.actions() if a.text()]
    for expected in ("Account Details…", "Reconcile…", "Import…",
                     "Download…", "Review…", "Hide Account"):
        assert expected in labels, f"{expected} missing from the gear menu"
    # Same QAction objects, so existing wiring and callers still work.
    assert reg.toolbar.act_download in reg.gear_menu.actions()
    assert reg.gear_button.menu() is reg.gear_menu


def test_gear_menu_view_mode_is_per_account(qapp, conn, accounts):
    """The layout that suits a register depends on the account, so the choice is
    the account's, not one global switch for every register at once."""
    from mammon.ui.widgets import RegisterWidget

    chk, sav = accounts
    chk_reg, sav_reg = RegisterWidget(conn, chk), RegisterWidget(conn, sav)
    assert chk_reg.view_mode == "one" and sav_reg.view_mode == "one"

    chk_reg._choose_view_mode("two")
    assert chk_reg.view_mode == "two"
    assert prefs.account_view_mode(chk) == "two"
    assert sav_reg.view_mode == "one"                 # the other account is untouched
    assert prefs.has_account_view_mode(sav) is False
    # The gear's radio marks follow the live mode.
    assert chk_reg._view_mode_acts["two"].isChecked()
    assert not chk_reg._view_mode_acts["one"].isChecked()
    # A reopened register comes back in the mode that account chose.
    assert RegisterWidget(conn, chk).view_mode == "two"


def test_global_view_default_does_not_override_an_account_choice(qapp, conn, accounts):
    """Settings sets the DEFAULT. An account that chose for itself keeps its
    choice, or the global switch would silently undo the more specific one."""
    from mammon.ui.widgets import MainWindow

    chk, sav = accounts
    win = MainWindow(conn)
    chk_reg = win.open_register(chk)
    sav_reg = win.open_register(sav)
    chk_reg._choose_view_mode("two")                  # this account opts in

    win._set_register_view_mode("one")                # global default -> one line
    assert chk_reg.view_mode == "two"                 # explicit choice survives
    assert sav_reg.view_mode == "one"                 # follower moves
    win._set_register_view_mode("two")
    assert sav_reg.view_mode == "two"
    win.close()


# ---- transaction-accepted sound --------------------------------------------
def test_sound_is_once_per_transaction_not_once_per_field(qapp, conn, accounts):
    """The register saves PER FIELD -- each cell writes as its editor closes --
    so tabbing across four fields is four writes to one transaction. The user
    works in transactions: change what you like, then Enter, and hear it once."""
    from mammon.ui.widgets import RegisterWidget

    chk, _ = accounts
    a = ledger.add_transaction(conn, chk, "2026-02-01", -50_00, payee="Store")
    b = ledger.add_transaction(conn, chk, "2026-02-02", -60_00, payee="Other")
    reg = RegisterWidget(conn, chk)
    dings = []
    reg._play_accepted = lambda: dings.append(1)

    row = reg.model.row_for_txn(a)
    for col, value in ((RegisterModel.PAYEE, "Market"),
                       (RegisterModel.MEMO, "weekly"),
                       (RegisterModel.NUM, "1234"),
                       (RegisterModel.PAYMENT, "75.00")):
        reg.model.setData(reg.model.index(row, col), value, Qt.EditRole)
        _settle()
    assert dings == [], "dinged while still editing the same transaction"

    reg._flush_transaction_sound()                 # what Enter does
    assert len(dings) == 1

    # Enter again with nothing further changed stays silent.
    reg._flush_transaction_sound()
    assert len(dings) == 1

    # Moving to a DIFFERENT transaction acknowledges the one just left.
    row_a, row_b = reg.model.row_for_txn(a), reg.model.row_for_txn(b)
    reg.model.setData(reg.model.index(row_a, RegisterModel.MEMO), "again", Qt.EditRole)
    _settle()
    reg.model.setData(reg.model.index(row_b, RegisterModel.MEMO), "other", Qt.EditRole)
    _settle()
    assert len(dings) == 2                         # the first row's edits landed
    reg._flush_transaction_sound()
    assert len(dings) == 3                         # ...and then the second's


def test_new_transaction_sounds_immediately(qapp, conn, accounts):
    """The blank quick-entry row commits as a WHOLE, so there is nothing to
    coalesce -- it is already one gesture."""
    from mammon.ui.widgets import RegisterWidget

    chk, _ = accounts
    reg = RegisterWidget(conn, chk)
    dings = []
    reg._play_accepted = lambda: dings.append(1)
    assert reg.model.add_from_values(
        {"date": "2026-02-02", "payee": "New", "payment": "10.00"})
    _settle()
    assert len(dings) == 1


def test_sound_fires_only_when_a_transaction_actually_changes(qapp, conn, accounts):
    """Qt commits an editor on focus-out whether or not it was touched. A chime
    keyed off 'an edit was committed' would fire for clicking a cell and clicking
    away, and a confirmation that fires on nothing is one you stop hearing."""
    chk, _ = accounts
    tid = ledger.add_transaction(conn, chk, "2026-02-01", -50_00, payee="Store")
    model = RegisterModel(conn, chk)
    beeps = []
    model.transactionSaved.connect(lambda _id: beeps.append(1))
    row = model.row_for_txn(tid)

    # Rewriting the same payee changes nothing -- silence.
    model.setData(model.index(row, RegisterModel.PAYEE), "Store", Qt.EditRole)
    _settle()
    assert beeps == []

    # A real edit sounds once.
    model.setData(model.index(row, RegisterModel.PAYEE), "Market", Qt.EditRole)
    _settle()
    assert len(beeps) == 1

    # So does a real amount change; an identical rewrite does not.
    model.setData(model.index(row, RegisterModel.PAYMENT), "75.00", Qt.EditRole)
    _settle()
    assert len(beeps) == 2
    model.setData(model.index(row, RegisterModel.PAYMENT), "75.00", Qt.EditRole)
    _settle()
    assert len(beeps) == 2


def test_sound_fires_for_a_brand_new_transaction(qapp, conn, accounts):
    chk, _ = accounts
    model = RegisterModel(conn, chk)
    beeps = []
    model.transactionSaved.connect(lambda _id: beeps.append(1))
    assert model.add_from_values(
        {"date": "2026-02-02", "payee": "New", "payment": "10.00"})
    assert len(beeps) == 1


def test_sound_respects_the_settings_switch(qapp, conn, accounts, tmp_path):
    """Settings can turn the chime off; the signal still fires either way, so the
    switch is consulted at playback, not by disconnecting the wiring."""
    from mammon.ui import sounds

    played = []
    sounds.reset_for_tests()
    assert sounds.play_accepted(enabled=False) is False
    assert played == []

    # The waveform is generated, not shipped as a binary blob.
    path = sounds.sound_path(tmp_path)
    assert path.exists() and path.suffix == ".wav"
    import wave
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 1 and w.getnframes() > 0

    # A missing audio backend must never interrupt a save.
    import builtins
    real_import = builtins.__import__

    def no_multimedia(name, *a, **k):
        if name == "PyQt5.QtMultimedia":
            raise ImportError("no audio backend")
        return real_import(name, *a, **k)

    sounds.reset_for_tests()
    builtins.__import__ = no_multimedia
    try:
        assert sounds.play_accepted(enabled=True) is False   # degrades, no raise
    finally:
        builtins.__import__ = real_import
    sounds.reset_for_tests()


def test_display_preferences_round_trips_the_sound_switch(qapp, conn):
    from mammon.ui.widgets import DisplayPreferencesDialog

    dlg = DisplayPreferencesDialog()
    assert dlg.sound.isChecked() is prefs.DEFAULT_SOUND
    dlg.sound.setChecked(False)
    prefs.set_display_prefs(dlg.values())
    assert prefs.sound_enabled() is False


# ---- one date format, everywhere -------------------------------------------
def test_parse_date_accepts_the_users_format_and_stores_iso():
    """The parse chokepoint, paired with fmt_date's display chokepoint. Storage,
    the ledger validators and download scripts all speak ISO, so conversion
    happens once here rather than at each place a date can be typed."""
    from mammon.ui.models import parse_date

    # ISO is always accepted -- it is the storage format and what scripts return.
    for fmt in prefs.DATE_FORMATS:
        assert parse_date("2026-03-04", fmt) == "2026-03-04"

    # A two-slash date is genuinely ambiguous, so the PREFERENCE decides.
    assert parse_date("03/04/2026", "MM/DD/YYYY") == "2026-03-04"
    assert parse_date("03/04/2026", "DD/MM/YYYY") == "2026-04-03"
    # Separators and 2-digit years, and a 4-digit lead is unambiguous either way.
    assert parse_date("3-4-26", "MM/DD/YYYY") == "2026-03-04"
    assert parse_date("4.3.2026", "DD/MM/YYYY") == "2026-03-04"
    assert parse_date("2026/03/04", "DD/MM/YYYY") == "2026-03-04"
    # Nonsense returns "" rather than a guess.
    for junk in ("", "garbage", "13/45/2026", "2026-02-30"):
        assert parse_date(junk, "MM/DD/YYYY") == ""


def test_date_display_and_entry_round_trip_under_every_preference(qapp):
    """What you type in your format comes back displayed in your format."""
    from mammon.ui.models import fmt_date, parse_date

    for fmt, typed in (("MM/DD/YYYY", "03/04/2026"),
                       ("DD/MM/YYYY", "04/03/2026"),
                       ("YYYY-MM-DD", "2026-03-04")):
        prefs.set_date_format(fmt)
        assert parse_date(typed) == "2026-03-04"      # same day, all three
        assert fmt_date("2026-03-04") == typed        # rendered back as typed


def test_every_date_editor_follows_the_preference(qapp, conn, accounts):
    """One preference, every date field. Each site used to hardcode its own
    answer: the register said MM/dd/yyyy while report filters, scheduled payments
    and the reconcile setup said yyyy-MM-dd, and three dialogs took free text
    that only accepted ISO."""
    from PyQt5.QtWidgets import QDateEdit
    from mammon.ui.delegates import DateDelegate, make_date_edit
    from mammon.ui.models import qt_date_format
    from mammon.ui.report_filters import ReportFilterBar
    from mammon.ui.widgets import (CreditReconcileStartDialog, ReconcileStartDialog,
                                   TransactionDialog)

    chk, _ = accounts
    ledger.create_account(conn, "Visa", "credit", opening_balance=0)
    model = RegisterModel(conn, chk)

    for fmt in ("YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY"):
        prefs.set_date_format(fmt)
        want = qt_date_format(fmt)

        # Hold the owners: a collected parent takes its child editor with it.
        owners = [
            TransactionDialog(model),
            ReportFilterBar(conn, "2026-01-01", "2026-12-31"),
            ReconcileStartDialog("2026-01-31", 0, 0),
            CreditReconcileStartDialog(conn, "2026-01-31", 0, 0, 0, 0, 0, ""),
        ]
        editors = {
            "register cell": DateDelegate().createEditor(
                None, None, model.index(0, RegisterModel.DATE)),
            "transaction dialog": owners[0].date,
            "report filters": owners[1].start_edit,
            "reconcile (bank)": owners[2].date,
            "reconcile (card)": owners[3].date,
            "shared factory": make_date_edit(),
        }
        for where, edit in editors.items():
            assert isinstance(edit, QDateEdit), f"{where} is not a date editor"
            assert edit.displayFormat() == want, (
                f"{where} shows {edit.displayFormat()!r}, expected {want!r} "
                f"for preference {fmt}")
            # Both entry routes: a calendar popup AND keyboard entry.
            assert edit.calendarPopup(), f"{where} has no date picker"


def test_date_editors_always_hand_back_iso(qapp, conn, accounts):
    """Whatever the display format, storage and scripts get ISO automatically."""
    from mammon.ui.delegates import date_edit_iso, make_date_edit
    from mammon.ui.widgets import TransactionDialog

    chk, _ = accounts
    model = RegisterModel(conn, chk)
    for fmt in prefs.DATE_FORMATS:
        prefs.set_date_format(fmt)
        edit = make_date_edit(iso="2026-03-04")
        assert date_edit_iso(edit) == "2026-03-04"
        dlg = TransactionDialog(model)
        _set_date(dlg.date, "2026-03-04")
        assert dlg.values()["date"] == "2026-03-04"


def test_changing_the_preference_reaches_widgets_already_on_screen(qapp, conn):
    """Long-lived dialogs and filter bars are built once, so the preference has
    to reach what is already open, not only newly created fields."""
    from mammon.ui.delegates import refresh_date_format
    from mammon.ui.models import qt_date_format
    from mammon.ui.report_filters import ReportFilterBar

    prefs.set_date_format("MM/DD/YYYY")
    bar = ReportFilterBar(conn, "2026-01-01", "2026-12-31")
    assert bar.start_edit.displayFormat() == "MM/dd/yyyy"

    prefs.set_date_format("YYYY-MM-DD")
    assert refresh_date_format(bar) >= 2          # start and end
    assert bar.start_edit.displayFormat() == qt_date_format("YYYY-MM-DD")
    assert bar.end_edit.displayFormat() == qt_date_format("YYYY-MM-DD")


def test_no_date_field_is_free_text_anymore(qapp, conn, accounts):
    """A guard against regressing to a typed-only ISO field: every date input in
    the register/dialog surfaces must be a real date editor."""
    from PyQt5.QtWidgets import QDateEdit
    from mammon.ui.loan_wizard import LoanSetupWizard
    from mammon.ui.widgets import (InvestmentTransactionDialog,
                                   TransactionDialog)

    chk, _ = accounts
    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    liab = ledger.create_account(conn, "Mortgage", "liability", opening_balance=0)
    model = RegisterModel(conn, chk)
    assert isinstance(TransactionDialog(model).date, QDateEdit)
    assert isinstance(InvestmentTransactionDialog(conn, inv).date, QDateEdit)
    assert isinstance(LoanSetupWizard(conn, account_id=liab).first_payment,
                      QDateEdit)


def test_investment_import_reports_rows_queued_for_review(qapp, conn, tmp_path):
    """Investment files no longer import straight through, so the report is the
    review queue's, not a direct-write summary. The old wording announced
    "Nothing to import ... its layout may not be recognised" over a successful
    import, because the direct path returned no counts for the batch to report."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon import import_review as _ir
    from mammon.ui.widgets import MainWindow

    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    acct = ledger.get_account(conn, inv)
    path = tmp_path / "history.qif"
    path.write_text(
        "!Type:Invst\nD01/07/2026\nNShrsOut\nYFUND A\nI30.33\nQ0.125\nT3.78\n^\nD01/08/2026\nNShrsOut\nYFUND B\nI12.72\nQ0.062\nT0.76\n^\n",
        encoding="utf-8")

    win = MainWindow(conn)
    shown = []
    QMessageBox.information = staticmethod(
        lambda parent, title, msg, *a, **k: shown.append((title, msg)))
    try:
        win._ingest_files_as_batch(inv, acct, [str(path)])
    finally:
        win.close()

    assert len(_ir.load_pending(conn, inv)) == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions").fetchone()[0] == 0
    assert shown, "the import reported nothing at all"
    title, msg = shown[-1]
    assert "Nothing to import" not in title
    assert "may not be recognised" not in msg




def test_every_action_round_trips_through_the_investment_dialog(qapp, conn):
    """Opening a transaction in the editor must never change what KIND of
    transaction it is. Any action missing from the dropdown fell through to
    index 0 -- Buy -- so a SellX redemption opened as a purchase and saving wrote
    that back. importers/csvimp actively produces BuyX and SellX (it maps
    "contribution" and "redemption" onto them), and a QIF carries whatever its
    source wrote, so the list has to cover more than a user would pick by hand."""
    from mammon.ui.widgets import InvestmentTransactionDialog

    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    for action in ("Buy", "BuyX", "Sell", "SellX", "Div", "Reinvest", "ReinvDiv",
                   "ReinvInt", "ReinvSh", "ReinvMd", "ReinvLg", "MiscInc",
                   "ShrsIn", "ShrsOut", "MiscExp", "RtrnCap", "ShtSell",
                   "CvrShrt", "XIn", "XOut"):
        seed = {"date": "2026-01-05", "action": action, "symbol": "FUND A",
                "quantity": "1", "price": "10.00", "amount": -1000,
                "commission": None, "memo": "", "transfer_account_id": None}
        dlg = InvestmentTransactionDialog(conn, inv, txn=seed)
        assert dlg.values()["action"] == action, (
            f"{action} opened as {dlg.values()['action']}")


def test_an_unknown_action_is_preserved_rather_than_rewritten(qapp, conn):
    """Raw activity text an importer passed through is not in any list, and the
    editor must still not rewrite it -- it is added to the combo as-is so the
    user changes it deliberately or not at all."""
    from mammon.ui.widgets import InvestmentTransactionDialog

    inv = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    seed = {"date": "2026-01-05", "action": "Change in Market Value",
            "symbol": "FUND A", "quantity": "0", "price": "", "amount": 208,
            "commission": None, "memo": "", "transfer_account_id": None}
    dlg = InvestmentTransactionDialog(conn, inv, txn=seed)
    assert dlg.values()["action"] == "Change in Market Value"
    assert dlg.action.findData("Change in Market Value") >= 0
    assert "as imported" in dlg.action.currentText()


# Actions the editor MUST offer: each is a distinct thing that happens in a real
# brokerage or IRA, supported by mammon.investments and reachable from an import.
# Spelling variants the domain also accepts (buymf / buystock / addshares /
# returnofcapital ...) are deliberately absent -- nobody picks between synonyms,
# and an imported one still round-trips through the "(as imported)" fallback.
_REQUIRED_ACTIONS = {
    "Buy", "BuyX", "Sell", "SellX",
    "Div", "DivX",
    "Reinvest", "ReinvDiv", "ReinvInt", "ReinvSh", "ReinvMd", "ReinvLg",
    "IntInc", "IntIncX",
    "CGLong", "CGLongX", "CGMid", "CGMidX", "CGShort", "CGShortX",
    "StockDividend", "MargInt",
    "MiscInc", "MiscExp", "RtrnCap",
    "ShrsIn", "ShrsOut", "StkSplit",
    "ShtSell", "CvrShrt",
    "XIn", "XOut", "Withdraw", "WithdrwX",
}


def test_editor_offers_every_action_a_user_would_choose(qapp, conn):
    """The dropdown kept drifting behind the domain: IntInc is documented in the
    schema, handled by investments._DIVIDEND_ACTIONS and covered by a test, yet
    could not be selected -- so cash earning interest in an IRA had no action.
    ReinvDiv was the same story. This pins the list."""
    from mammon.ui.widgets import _INV_ACTION_CHOICES, _INV_ACTION_FIELDS

    offered = {code for _label, code in _INV_ACTION_CHOICES}
    missing = _REQUIRED_ACTIONS - offered
    assert not missing, f"actions the editor should offer but does not: {sorted(missing)}"
    # Every offered action needs a field group, or its editor shows nothing.
    for code in offered:
        assert code in _INV_ACTION_FIELDS, f"{code} has no field group"
    # No duplicates, and every label is distinct so the list is readable.
    codes = [c for _l, c in _INV_ACTION_CHOICES]
    labels = [l for l, _c in _INV_ACTION_CHOICES]
    assert len(codes) == len(set(codes))
    assert len(labels) == len(set(labels))


def test_offered_actions_are_all_understood_by_the_domain(qapp, conn):
    """The converse: the editor must not offer an action the ledger cannot post.
    Anything outside the domain's known sets has to be a deliberate cash-only
    passthrough, not a typo in the list."""
    from mammon import investments as I
    from mammon.ui.widgets import _INV_ACTION_CHOICES

    known = set()
    for name in dir(I):
        if name.endswith("_ACTIONS") and isinstance(getattr(I, name), (set, frozenset)):
            known |= {a.lower() for a in getattr(I, name)}
    # Cash-only lines carry no share effect, so they appear in no share-action set.
    cash_only = {"miscinc", "miscexp", "xin", "xout", "cash"}
    for _label, code in _INV_ACTION_CHOICES:
        assert code.lower() in known or code.lower() in cash_only, (
            f"{code} is offered but the domain does not recognise it")


def test_interest_income_posts_and_moves_cash(qapp, conn):
    """The user's case: cash sitting in an IRA earns interest."""
    from mammon import investments
    from mammon.ui.widgets import InvestmentTransactionDialog

    inv = ledger.create_account(conn, "IRA", "investment", opening_balance=0)
    seed = {"date": "2026-01-05", "action": "IntInc", "symbol": "", "quantity": "",
            "price": "", "amount": 10_00, "commission": None, "memo": "",
            "transfer_account_id": None}
    dlg = InvestmentTransactionDialog(conn, inv, txn=seed)
    assert dlg.values()["action"] == "IntInc"

    investments.record_investment(conn, inv, "2026-01-05", "IntInc", amount=10_00)
    assert investments.investment_cash(conn, inv) == 10_00


def test_reinvest_variants_are_selectable_not_collapsed(qapp, conn):
    """ReinvDiv was accepted by the domain and typeable in the pending row, but
    the dropdown only offered a generic 'Reinvest' -- so the editor quietly
    rewrote the more specific code."""
    from mammon.ui.widgets import _INV_ACTION_CHOICES

    codes = {code for _label, code in _INV_ACTION_CHOICES}
    for code in ("ReinvDiv", "ReinvInt", "ReinvSh", "ReinvMd", "ReinvLg",
                 "BuyX", "SellX", "ShtSell", "CvrShrt"):
        assert code in codes, f"{code} is not offered in the editor"


def test_accepted_sound_is_silent_under_a_headless_run(qapp, monkeypatch):
    """The suite saves hundreds of transactions; without this guard it played the
    chime for every one, out of any UI. A headless run has nobody to hear it."""
    from mammon.ui import sounds

    sounds.reset_for_tests()
    played = []
    monkeypatch.setattr(
        sounds, "sound_path", lambda *a, **k: played.append(1) or "x.wav")
    assert sounds.play_accepted(enabled=True) is False
    assert played == [], "the sound path was even consulted"

    # With a real platform it goes through the normal (degrading) path.
    monkeypatch.setenv("QT_QPA_PLATFORM", "windows")
    sounds.reset_for_tests()
    sounds.play_accepted(enabled=True)          # may fail on no backend; must not raise
    sounds.reset_for_tests()


# ---- Get Quotes (investment register gear menu) ----------------------------
def test_quotable_holdings_pairs_names_with_tickers(qapp, conn):
    """price_history is keyed by the name a holding is stored under, so the
    pairing (not just the ticker) is what makes a fetched quote usable."""
    from mammon import investments
    from mammon.ui.widgets import InvestmentRegisterWidget

    aid = ledger.create_account(conn, "IB", "investment", opening_balance=0)
    for sym, qty in (("ALTY GLOBAL X SUPERDIVIDEND ALTER", "10"),
                     ("DOMESTIC BOND INDEX", "5"),
                     ("QTUM DEFIANCE QUANTUM ETF", "3")):
        investments.record_investment(conn, aid, "2026-01-05", "Buy", symbol=sym,
                                      quantity=qty, price="10.00", amount=-1000)
    investments.rebuild_holdings(conn, aid)

    reg = InvestmentRegisterWidget(conn, aid)
    pairs, skipped = reg.quotable_holdings()
    assert dict(pairs) == {"ALTY GLOBAL X SUPERDIVIDEND ALTER": "ALTY",
                           "QTUM DEFIANCE QUANTUM ETF": "QTUM"}
    assert skipped == ["DOMESTIC BOND INDEX"]


def test_get_quotes_records_against_the_holding_name(qapp, conn, monkeypatch):
    """A quote fetched for 'ALTY' must be recorded against the holding's own
    name -- recording it under the bare ticker would look like it worked and
    value nothing, because latest_price() looks up by the holding's symbol."""
    from mammon import investments
    from mammon.ui import widgets
    from mammon.ui.widgets import InvestmentRegisterWidget

    aid = ledger.create_account(conn, "IB", "investment", opening_balance=0)
    name = "ALTY GLOBAL X SUPERDIVIDEND ALTER"
    investments.record_investment(conn, aid, "2026-01-05", "Buy", symbol=name,
                                  quantity="10", price="10.00", amount=-10000)
    investments.rebuild_holdings(conn, aid)

    class _Source:
        """The network seam investments.fetch_quotes injects.

        Stub THIS rather than the widget's _fetch_quotes: the ticker -> name
        mapping is applied by fetch_quotes, so replacing the widget seam skips
        the write entirely and the test can no longer see where the price
        landed -- which is the one thing it exists to check."""
        source_name = "fake"

        def get_quotes(self, syms):
            return [investments.Quote(s, "2026-08-31", "26.40", "fake")
                    for s in syms]

    reg = InvestmentRegisterWidget(conn, aid)
    asked = []
    reg._confirm_quote_targets = lambda pairs, skipped: (pairs, False)
    reg._fetch_quotes = lambda ticks, names=None: (
        asked.extend(ticks)
        or investments.fetch_quotes(conn, ticks, source=_Source(), names=names))
    monkeypatch.setattr(widgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    reg.get_quotes()

    assert asked == ["ALTY"]                       # fetched BY ticker
    from decimal import Decimal
    assert investments.latest_price(conn, name) == Decimal("26.40")   # keyed BY NAME
    assert investments.latest_price(conn, "ALTY") is None   # no phantom series


def test_get_quotes_fetches_nothing_the_user_unticked(qapp, conn, monkeypatch):
    """The derived ticker is a guess shown for confirmation. Unticking it must
    mean no request is made for that security at all."""
    from mammon import investments
    from mammon.ui import widgets
    from mammon.ui.widgets import InvestmentRegisterWidget

    aid = ledger.create_account(conn, "401k", "investment", opening_balance=0)
    for sym in ("INTL EQUITY INDEX", "FXAIX"):
        investments.record_investment(conn, aid, "2026-01-05", "Buy", symbol=sym,
                                      quantity="1", price="10.00", amount=-1000)
    investments.rebuild_holdings(conn, aid)

    reg = InvestmentRegisterWidget(conn, aid)
    pairs, _skipped = reg.quotable_holdings()
    assert ("INTL EQUITY INDEX", "INTL") in pairs   # the dangerous guess IS offered
    asked = []
    # The user unticks the plan fund.
    reg._confirm_quote_targets = lambda p, s: ([x for x in p if x[1] != "INTL"], False)
    reg._fetch_quotes = lambda ticks, names=None: asked.extend(ticks) or []
    monkeypatch.setattr(widgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    reg.get_quotes()
    assert asked == ["FXAIX"]
    assert investments.latest_price(conn, "INTL EQUITY INDEX") is None

    # Cancelling the dialog fetches nothing at all.
    asked.clear()
    reg._confirm_quote_targets = lambda p, s: ([], False)
    reg.get_quotes()
    assert asked == []


def test_get_quotes_reports_a_missing_backend_as_setup(qapp, conn, monkeypatch):
    """yfinance is deliberately not installed; that is a setup step, not an
    import failure, and it must never raise into the register."""
    from mammon import investments
    from mammon.ui import widgets
    from mammon.ui.widgets import InvestmentRegisterWidget

    aid = ledger.create_account(conn, "IB", "investment", opening_balance=0)
    investments.record_investment(conn, aid, "2026-01-05", "Buy", symbol="ALTY",
                                  quantity="1", price="10.00", amount=-1000)
    investments.rebuild_holdings(conn, aid)

    reg = InvestmentRegisterWidget(conn, aid)
    reg._confirm_quote_targets = lambda p, s: (p, False)

    def boom(_ticks, _names=None):
        raise investments.QuoteSourceUnavailable("yfinance is not installed")

    reg._fetch_quotes = boom
    shown = []
    monkeypatch.setattr(widgets.QMessageBox, "warning", staticmethod(
        lambda p_, t_, m_, *a, **k: shown.append((t_, m_))))
    reg.get_quotes()                                # must not raise
    assert shown and "No quote source" in shown[0][1]


def test_investment_register_actions_live_in_a_gear_menu(qapp, conn):
    from mammon.ui.widgets import InvestmentRegisterWidget

    aid = ledger.create_account(conn, "IB", "investment", opening_balance=0)
    reg = InvestmentRegisterWidget(conn, aid)
    assert reg.toolbar.isVisible() is False
    labels = [a.text() for a in reg.gear_menu.actions() if a.text()]
    for expected in ("Account Details…", "Reconcile…", "Import…", "Download…",
                     "Review…", "Hide Account", "Get Quotes…"):
        assert expected in labels, f"{expected} missing from the gear menu"
    assert reg.gear_button.menu() is reg.gear_menu
    assert reg.toolbar.act_download in reg.gear_menu.actions()


# ---------------------------------------------------------------------------
# Rename Security: search/replace over one account's security names
# ---------------------------------------------------------------------------
def _seed_rename_account(conn):
    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    for date, sym, qty, price, amt in (
            ("2026-01-05", "Fidelity 500 Index Fund", "3", "100", -300_00),
            ("2026-02-05", "FXAIX", "1", "200", -200_00),
            ("2026-02-06", "Fidelity Mid Cap Index Fund", "2", "50", -100_00)):
        investments.record_investment(conn, acct, date, "Buy", symbol=sym,
                                      quantity=qty, price=price, amount=amt)
    investments.rebuild_holdings(conn, acct)
    return acct


def test_security_rename_dialog_previews_before_it_writes(qapp, conn):
    from PyQt5.QtWidgets import QDialogButtonBox
    from mammon.ui.widgets import SecurityRenameDialog

    acct = _seed_rename_account(conn)
    dlg = SecurityRenameDialog(conn, acct)

    # Nothing typed -> nothing to do, and Rename is not offered.
    assert dlg.list.count() == 0
    assert not dlg.buttons.button(QDialogButtonBox.Ok).isEnabled()

    # A substring reaches BOTH Fidelity funds -- which is exactly why the
    # preview lists every one of them by name.
    dlg.search_edit.setText("Fidelity")
    assert dlg.list.count() == 2
    assert dlg.buttons.button(QDialogButtonBox.Ok).isEnabled()

    dlg.search_edit.setText("Fidelity 500 Index Fund")
    dlg.replace_edit.setText("FXAIX")
    assert dlg.list.count() == 1
    text = dlg.list.item(0).text()
    assert "Fidelity 500 Index Fund" in text and "FXAIX" in text
    assert "1 transaction" in text
    # FXAIX is already held here, so the merge is called out
    assert "merges" in text
    assert "merge" in dlg.status.text()
    assert dlg.chosen() == [("Fidelity 500 Index Fund", "FXAIX")]

    # Unticking a row excludes it -- the preview is a veto, not a notice.
    dlg.list.item(0).setCheckState(Qt.Unchecked)
    assert dlg.chosen() == []

    # A replacement that would leave a nameless security is refused, not applied.
    dlg.search_edit.setText("FXAIX")
    dlg.replace_edit.setText("")
    assert dlg.list.count() == 0
    assert not dlg.buttons.button(QDialogButtonBox.Ok).isEnabled()
    assert "no name" in dlg.status.text()

    # ...and after all that, the ledger is untouched.
    assert set(investments.symbols_used(conn, acct)) == {
        "Fidelity 500 Index Fund", "FXAIX", "Fidelity Mid Cap Index Fund"}


def test_gear_rename_security_applies_and_refreshes_the_register(qapp, conn, monkeypatch):
    from mammon.ui import widgets
    from mammon.ui.widgets import InvestmentRegisterWidget

    acct = _seed_rename_account(conn)
    reg = InvestmentRegisterWidget(conn, acct)
    assert "Rename Security…" in [a.text() for a in reg.gear_menu.actions()]

    monkeypatch.setattr(widgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(
        InvestmentRegisterWidget, "_ask_security_renames",
        lambda self: [("Fidelity 500 Index Fund", "FXAIX")])

    changed = []
    reg.changed.connect(lambda: changed.append(1))
    reg.act_rename_security.trigger()

    # one position, both sides' shares and basis
    held = investments.list_holdings(conn, acct)
    assert [h["symbol"] for h in held] == ["FXAIX", "Fidelity Mid Cap Index Fund"] or \
           sorted(h["symbol"] for h in held) == ["FXAIX", "Fidelity Mid Cap Index Fund"]
    fxaix = investments.get_holding(conn, acct, "FXAIX")
    assert fxaix["quantity"] == "4" and fxaix["cost_basis"] == 500_00
    # the register and its security filter followed the rename
    assert "Fidelity 500 Index Fund" not in investments.symbols_used(conn, acct)
    combo = [reg.security_filter.itemText(i)
             for i in range(reg.security_filter.count())]
    assert "Fidelity 500 Index Fund" not in combo and "FXAIX" in combo
    assert changed


def test_rename_dialog_opens_on_the_security_in_view(qapp, conn):
    """The Find box is seeded from the register's security filter, so renaming
    the fund you are looking at does not mean retyping its name."""
    from mammon.ui.widgets import InvestmentRegisterWidget

    acct = _seed_rename_account(conn)
    reg = InvestmentRegisterWidget(conn, acct)
    idx = reg.security_filter.findData("Fidelity Mid Cap Index Fund")
    assert idx > 0
    reg.security_filter.setCurrentIndex(idx)
    assert reg._current_security() == "Fidelity Mid Cap Index Fund"


def test_split_ratio_round_trips_as_quicken_encodes_it(qapp, conn):
    """A split is stored as new-shares-per-TEN-old (a 2-for-1 is 20), because
    that is what QIF/OFX carry and what investments._apply_txn replays. The
    dialog asks for the ratio the broker announces and converts at the boundary
    -- it used to store the typed number raw, so a 2-for-1 was stored as 2 and
    replayed as 2/10, shrinking the position to a fifth."""
    from decimal import Decimal
    from mammon.ui.widgets import InvestmentTransactionDialog as D

    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    investments.record_investment(conn, acct, "2026-01-05", "Buy", symbol="VGT",
                                  quantity="26", price="700", amount=-18_200_00)

    dlg = D(conn, acct)
    dlg.action.setCurrentIndex(dlg.action.findData("StkSplit"))
    dlg.security.setEditText("VGT")
    dlg.split.setText("8")                       # an 8-for-1, as announced
    v = dlg.values()
    assert (v["split_num"], v["split_den"]) == (8, 1)
    assert v["quantity"] == Decimal("80")        # legacy per-ten, kept beside it

    investments.record_investment(conn, acct, "2026-04-21", "StkSplit",
                                  symbol="VGT", quantity="80",
                                  split_num=8, split_den=1)
    investments.rebuild_holdings(conn, acct)
    held = investments.get_holding(conn, acct, "VGT")
    assert held["quantity"] == "208"             # 26 x 8, not 26 / 5
    assert held["cost_basis"] == 18_200_00       # basis unchanged by a split

    # ...and the editor shows the ratio back in the notation the register uses,
    # so what is displayed is also what the field accepts.
    txn = conn.execute(
        "SELECT * FROM investment_transactions WHERE action='StkSplit'").fetchone()
    edit = D(conn, acct, txn=txn)
    assert edit.split.text() == "8:1"


def test_register_shows_a_split_as_its_ratio_not_the_encoding(qapp, conn):
    """80 in a Quantity column means nothing to the holder of 26 shares. The
    register shows the ratio the broker announced, and the Share Bal beside it
    shows what the split produced."""
    from mammon.ui.models import InvestmentRegisterModel as M

    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    investments.record_investment(conn, acct, "2026-01-05", "Buy", symbol="VGT",
                                  quantity="26", price="700", amount=-18_200_00)
    investments.record_investment(conn, acct, "2026-04-21", "StkSplit",
                                  symbol="VGT", quantity="80")
    model = M(conn, acct)
    model.reload()

    def cell(row, col):
        return model.data(model.index(row, col), Qt.DisplayRole)

    assert cell(1, M.ACTION) == "StkSplit"
    assert cell(1, M.QUANTITY) == "8:1"          # not "80"
    assert cell(1, M.SHARE_BAL) == "208"
    assert cell(1, M.CASH_AMT) == ""             # cash-neutral


def test_split_field_accepts_the_notation_it_displays(qapp, conn):
    """"Please enter a split value" for "8:1" -- the exact string the register
    shows -- is the field disagreeing with its own display."""
    from mammon.ui.widgets import InvestmentTransactionDialog as D

    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    dlg = D(conn, acct)
    dlg.action.setCurrentIndex(dlg.action.findData("StkSplit"))
    dlg.security.setEditText("ABC")

    for typed, pair in (("8:1", (8, 1)), ("8", (8, 1)), ("3:2", (3, 2)),
                        ("1.5", (3, 2)), ("1:2", (1, 2)), ("8-for-1", (8, 1)),
                        ("4:3", (4, 3)), ("1:3", (1, 3))):
        dlg.split.setText(typed)
        ok, msg = dlg.validate()
        assert ok, "%r rejected: %s" % (typed, msg)
        v = dlg.values()
        assert (v["split_num"], v["split_den"]) == pair, typed

    for bad in ("", "abc", "8:0", "-2:1"):
        dlg.split.setText(bad)
        ok, _ = dlg.validate()
        assert not ok, bad


def test_get_quotes_history_checkbox_backfills_and_reports(qapp, conn, monkeypatch):
    from decimal import Decimal
    from mammon.ui import widgets
    from mammon.ui.widgets import InvestmentRegisterWidget

    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    investments.record_investment(conn, acct, "2024-01-15", "Buy", symbol="ABC",
                                  quantity="10", price="10", amount=-100_00)
    investments.rebuild_holdings(conn, acct)
    reg = InvestmentRegisterWidget(conn, acct)

    shown = []
    monkeypatch.setattr(widgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.append(a[2])))
    reg._confirm_quote_targets = lambda p, s: (p, True)       # history ticked
    reg._fetch_quotes = lambda tickers, names=None: [
        investments.Quote("ABC", "2026-09-01", "20", "fake")]
    asked = []

    def _hist(chosen):
        asked.append(list(chosen))
        return investments.record_prices_if_absent(
            conn, [("ABC", "2025-01-31", "15", "fake")])

    reg._fetch_quote_history = _hist
    reg.get_quotes()

    assert asked == [[("ABC", "ABC")]]
    assert dict(investments.price_history(conn, "ABC"))["2025-01-31"] == Decimal("15")
    assert "1 historical price filled in." in shown[0]


def test_get_quotes_keeps_the_latest_close_when_history_fails(qapp, conn, monkeypatch):
    """A provider with no history for one security must not cost the user the
    closes it already returned -- those are what the register shows now."""
    from decimal import Decimal
    from mammon.ui import widgets
    from mammon.ui.widgets import InvestmentRegisterWidget

    acct = ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    investments.record_investment(conn, acct, "2024-01-15", "Buy", symbol="ABC",
                                  quantity="10", price="10", amount=-100_00)
    investments.rebuild_holdings(conn, acct)
    reg = InvestmentRegisterWidget(conn, acct)

    shown = []
    monkeypatch.setattr(widgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.append(a[2])))
    reg._confirm_quote_targets = lambda p, s: (p, True)

    class _Source:
        """Stubbed at the network seam, not the widget seam: the price is
        written by investments.fetch_quotes, so replacing _fetch_quotes
        outright would make the "close survived" assertion below vacuous."""
        source_name = "fake"

        def get_quotes(self, syms):
            return [investments.Quote(s, "2026-09-01", "20", "fake")
                    for s in syms]

    reg._fetch_quotes = lambda tickers, names=None: investments.fetch_quotes(
        conn, tickers, source=_Source(), names=names)

    def _boom(chosen):
        raise investments.QuoteSourceUnavailable("no history from this source")

    reg._fetch_quote_history = _boom
    reg.get_quotes()

    # the latest close was still recorded, against the holding name
    assert investments.latest_price(conn, "ABC") == Decimal("20")
    assert "1 of 1 securities priced." in shown[0]
    assert "no history from this source" in shown[0]


def test_editing_a_share_move_keeps_its_price_and_value(qapp, conn):
    """Regression: ShrsOut listed neither price nor amount in the action field
    map, so opening a fee removal in the editor and pressing OK blanked what the
    broker sent -- and that price is often the only quote a plan's fund has."""
    from decimal import Decimal
    from mammon.ui.widgets import InvestmentTransactionDialog as D

    acct = ledger.create_account(conn, "Plan", "investment", opening_balance=0)
    txn_id = investments.record_investment(
        conn, acct, "2000-10-12", "ShrsOut", symbol="EMPLOYER COMMON STOCK",
        quantity="0.045", price="28.50", amount=1_28, memo="Fees")
    txn = investments.get_investment_txn(conn, txn_id)

    dlg = D(conn, acct, txn=txn)
    # storage normalises the Decimal text (28.50 -> 28.5), so compare as numbers
    assert Decimal(dlg.price.text()) == Decimal("28.50")
    v = dlg.values()
    assert Decimal(str(v["quantity"])) == Decimal("0.045")
    assert Decimal(str(v["price"])) == Decimal("28.50")
    assert v["amount"] == 1_28              # survives the round trip

    investments.update_investment(
        conn, txn_id, v["date"], v["action"], symbol=v["symbol"],
        quantity=v["quantity"], price=v["price"], amount=v["amount"],
        commission=v["commission"], memo=v["memo"],
        transfer_account_id=v["transfer_account_id"],
        split_num=v["split_num"], split_den=v["split_den"])
    after = investments.get_investment_txn(conn, txn_id)
    assert Decimal(after["price"]) == Decimal("28.50") and after["amount"] == 1_28


def test_dark_tabs_are_legible():
    """Regression: the Holdings dialog's tab labels were unreadable in dark mode.

    With no QTabBar rule the native Windows style paints the tab in its own LIGHT
    chrome while the label comes from the palette -- which dark mode has made
    light -- so the text landed light-on-light. Asserting CONTRAST rather than
    the presence of a rule, so a future edit that sets a dark foreground on a
    dark tab fails here instead of on screen.
    """
    import re
    from mammon.ui import style

    def luminance(hex_colour):
        r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
        def chan(c):
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = chan(r), chan(g), chan(b)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def contrast(fg, bg):
        a, b = sorted((luminance(fg), luminance(bg)))
        return (b + 0.05) / (a + 0.05)

    dark = style.build_qss(style.DARK)
    rules = re.findall(
        r"QTabBar::tab(?::\w+)?\s*\{([^}]*)\}", dark)
    assert rules, "dark QSS names no QTabBar::tab rule at all"

    checked = 0
    for body in rules:
        fg = re.search(r"\bcolor:\s*(#[0-9a-fA-F]{6})", body)
        bg = re.search(r"\bbackground:\s*(#[0-9a-fA-F]{6})", body)
        if fg and bg:
            ratio = contrast(fg.group(1), bg.group(1))
            assert ratio >= 3.0, (
                "tab text %s on %s is %.1f:1 -- unreadable"
                % (fg.group(1), bg.group(1), ratio))
            checked += 1
    assert checked >= 2                       # normal and selected, at least

    # the light theme is untouched: it never had the problem
    assert "QTabBar" not in style.build_qss(style.LIGHT)


def test_holdings_tabs_exist_to_be_styled(qapp, conn):
    """The dialog the report came from: two tabs, both labelled."""
    from mammon.ui.widgets import HoldingsDialog

    acct = _seed_holdings_account(conn)
    dlg = HoldingsDialog(conn, acct)
    labels = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    assert len(labels) == 2
    assert labels[0].startswith("Currently Held")
    assert labels[1].startswith("Previously Held")

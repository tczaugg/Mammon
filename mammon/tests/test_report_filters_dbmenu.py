"""Tests for the reinstated report customization controls (time range /
accounts / categories) and the new File-menu items 'Save Database As' and
'Restore from Backup'. Runs headless under the offscreen Qt platform.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger
from mammon.reports.spending import SpendingReport

from PyQt5.QtCore import Qt


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "rf.db")
    ledger.create_account(c, "Checking", "checking", opening_balance=100_00)
    ledger.create_account(c, "Savings", "savings", opening_balance=0)
    yield c
    c.close()


class _Row:
    """Minimal stand-in for a SpendingReport top-level row."""
    def __init__(self, name, cents):
        self.name = name
        self.total_cents = cents
        self.children = []


def _report(*rows):
    return SpendingReport(start="2026-01-01", end="2026-12-31",
                          account_ids=None, rows=list(rows),
                          total_cents=sum(r.total_cents for r in rows))


# ---- headless category-filter helpers -------------------------------------
def test_filter_spending_report_by_category():
    from mammon.ui.report_filters import filter_spending_report
    rep = _report(_Row("Groceries", 5000), _Row("Gas", 3000))
    # None -> unchanged (same object)
    assert filter_spending_report(rep, None) is rep
    only = filter_spending_report(rep, {"Groceries"})
    assert [r.name for r in only.rows] == ["Groceries"]
    assert only.total_cents == 5000          # total recomputed from survivors


def test_spending_pie_from_report_filters_and_groups():
    from mammon.ui.report_filters import spending_pie_from_report
    rep = _report(_Row("Groceries", 5000), _Row("Gas", 3000))
    full = spending_pie_from_report(rep, None)
    assert {s.label for s in full.slices} == {"Groceries", "Gas"}
    assert full.total_cents == 8000
    one = spending_pie_from_report(rep, {"Gas"})
    assert [s.label for s in one.slices] == ["Gas"]
    assert one.total_cents == 3000
    # long tail collapses into a single 'Other' slice
    big = _report(*[_Row(f"C{i}", (20 - i) * 100) for i in range(12)])
    pie = spending_pie_from_report(big, None, max_slices=8)
    assert len(pie.slices) == 8
    assert pie.slices[-1].label == "Other"


# ---- the filter bar widget -------------------------------------------------
def test_report_filter_bar_selection(qapp, conn):
    from mammon.ui.report_filters import ReportFilterBar
    bar = ReportFilterBar(conn, "2026-01-01", "2026-06-30",
                          show_accounts=True, categories=["Groceries", "Gas"])
    assert bar.start_iso() == "2026-01-01"
    assert bar.end_iso() == "2026-06-30"
    # everything checked -> "no filter" (None), which the reports read as "all"
    assert bar.selected_account_ids() is None
    assert bar.selected_categories() is None
    # unchecking narrows the selection
    bar.account_list.item(0).setCheckState(Qt.Unchecked)
    ids = bar.selected_account_ids()
    assert isinstance(ids, list) and len(ids) == bar.account_list.count() - 1
    bar.category_list.item(0).setCheckState(Qt.Unchecked)
    assert bar.selected_categories() == {"Gas"}


def test_report_filter_bar_clear_all_accounts(qapp, conn):
    from mammon.ui.report_filters import ReportFilterBar
    bar = ReportFilterBar(conn, "2026-01-01", "2026-06-30", show_accounts=True)
    assert bar.selected_account_ids() is None       # all checked -> no filter
    bar.clear_accounts()                            # "Clear all" button action
    assert bar.selected_account_ids() == []         # every account unchecked


# ---- the category TREE picker (itemize report) -----------------------------
def _seed_category_tree(conn):
    fuel = ledger.resolve_category(conn, "Auto & Transport:Fuel")
    parking = ledger.resolve_category(conn, "Auto & Transport:Parking")
    groc = ledger.resolve_category(conn, "Groceries")
    acct = ledger.list_accounts(conn)[0]["id"]
    ledger.add_transaction(conn, acct, "2026-01-05", -40_00, payee="Store",
                           category_id=groc)
    return fuel, parking, groc


def test_category_children_and_transactions_helpers(conn):
    fuel, parking, groc = _seed_category_tree(conn)
    tops = ledger.category_children(conn, None)
    names = [c["name"] for c in tops]
    assert "Auto & Transport" in names and "Groceries" in names
    auto_id = next(c["id"] for c in tops if c["name"] == "Auto & Transport")
    # children are returned sorted case-insensitively by name
    assert [c["name"] for c in ledger.category_children(conn, auto_id)] == \
        ["Fuel", "Parking"]
    assert ledger.category_children(conn, groc) == []      # leaf has no children
    txns = ledger.category_transactions(conn, groc)
    assert len(txns) == 1
    assert txns[0]["amount"] == -40_00 and txns[0]["payee"] == "Store"


def test_category_transactions_includes_split_lines(conn):
    groc = ledger.resolve_category(conn, "Groceries")
    acct = ledger.list_accounts(conn)[0]["id"]
    tid = ledger.add_transaction(conn, acct, "2026-02-01", -100_00, payee="Mart")
    ledger.set_splits(conn, tid, [{"category_id": groc, "amount": -60_00},
                                  {"category_id": None, "amount": -40_00}])
    txns = ledger.category_transactions(conn, groc)
    assert [t["amount"] for t in txns] == [-60_00]        # the split line only


def test_itemize_category_checklist_selection_and_clear(qapp, conn):
    """The itemize customization picks WHICH top-level categories are eligible via
    a flat check-list (the expandable tree lives in the report, not here)."""
    from mammon.ui.report_filters import ReportFilterBar
    _seed_category_tree(conn)
    bar = ReportFilterBar(conn, "2026-01-01", "2026-06-30", show_accounts=False,
                          categories=["Auto & Transport", "Groceries"])
    # all checked -> no filter (caller passes None straight through)
    assert bar.selected_categories() is None
    # unchecking one narrows to the remaining name(s), as a set
    bar.category_list.item(0).setCheckState(Qt.Unchecked)   # "Auto & Transport"
    assert bar.selected_categories() == {"Groceries"}
    # "Clear all" empties the whole selection
    bar.clear_categories()
    assert bar.selected_categories() == set()


# ---- report/chart dialogs build with controls & refresh --------------------
@pytest.fixture
def _no_modal(monkeypatch):
    """Stop the report dialogs from blocking on exec_()."""
    from PyQt5.QtWidgets import QDialog
    monkeypatch.setattr(QDialog, "exec_", lambda self: 0)


def _win_with_spending(tmp_path):
    from mammon.ui.widgets import MainWindow
    path = tmp_path / "win.db"
    c = db.init_db(path)
    chk = ledger.create_account(c, "Checking", "checking", opening_balance=500_00)
    groceries = ledger.resolve_category(c, "Groceries")
    salary = ledger.resolve_category(c, "Salary")
    ledger.add_transaction(c, chk, "2026-03-01", -40_00, payee="Store",
                           category_id=groceries)
    ledger.add_transaction(c, chk, "2026-04-01", -25_00, payee="Gas")
    ledger.add_transaction(c, chk, "2026-04-15", 3000_00, payee="Employer",
                           category_id=salary)
    return MainWindow(c, db_path=str(path))


def test_report_dialogs_open_with_controls(qapp, tmp_path, _no_modal):
    win = _win_with_spending(tmp_path)
    # None of these should raise; each builds its customize popup and refreshes.
    win._spending_report_dialog()
    win._itemize_window()
    win._spending_chart_dialog()
    win._income_chart_dialog()
    win._net_worth_chart_dialog()
    win.close()


def test_report_dialogs_default_to_ytd_except_net_worth(qapp, tmp_path,
                                                        monkeypatch):
    """Every report/chart dialog opens on Year-to-Date, EXCEPT Net Worth Over
    Time, which keeps its whole-ledger 'Earliest to date' default -- a cumulative
    curve is meaningless over a partial-year slice (SRD §5.9c). Each dialog exposes
    its Period combo as ``dlg._period_combo`` before it calls exec_(); we capture
    the selection there instead of letting the modal block."""
    from PyQt5.QtWidgets import QDialog
    captured = []

    def _capture(self):
        combo = getattr(self, "_period_combo", None)
        if combo is not None:
            captured.append(combo.currentData())
        return 0

    monkeypatch.setattr(QDialog, "exec_", _capture)
    win = _win_with_spending(tmp_path)
    try:
        win._spending_report_dialog()
        win._spending_chart_dialog()
        win._income_chart_dialog()
        assert captured == ["ytd", "ytd", "ytd"]
        captured.clear()
        win._net_worth_chart_dialog()
        assert captured == ["earliest"]
    finally:
        win.close()


# ---- the category-report period dropdown -----------------------------------
def _period_header(win):
    """Assemble the pieces a category report feeds to ``_report_period_header``
    and attach it, returning ``(dlg, combo, customize, refresh_calls)``. The
    caller MUST keep ``dlg`` alive -- it owns ``combo``/``customize`` and Qt
    deletes them when the dialog is garbage-collected."""
    from PyQt5.QtWidgets import QDialog, QVBoxLayout
    from mammon.ui.report_filters import CustomizeDialog
    dlg = QDialog()
    lay = QVBoxLayout(dlg)
    customize = CustomizeDialog(win.conn, "2020-01-01", "2020-12-31",
                                show_accounts=True, categories=None, parent=dlg)
    calls = []
    combo = win._report_period_header(lay, dlg, customize,
                                      lambda: calls.append(1))
    return dlg, combo, customize, calls


def test_report_period_dropdown_options_and_default(qapp, tmp_path):
    dlg, combo, customize, _ = _period_header(_win_with_spending(tmp_path))
    labels = [combo.itemText(i) for i in range(combo.count())]
    # The UNION of the calendar and rolling presets (§5.9b) -- both sets survive.
    assert labels == ["Last 7 days", "Last 30 days", "This Month", "Last Month",
                      "This quarter", "Last quarter", "Last 12 months",
                      "Last 3 years", "Last 5 years", "Last 10 years",
                      "Year-to-Date", "This Year", "Last Year",
                      "Earliest to date", "Custom"]
    # DEFAULT selection is Year-to-Date (the shared default for report windows).
    assert combo.currentData() == "ytd"


def test_report_period_presets_refilter(qapp, tmp_path):
    from datetime import date
    from mammon.reports.spending import preset_range
    dlg, combo, customize, calls = _period_header(_win_with_spending(tmp_path))
    keys = ("last_7_days", "last_30_days", "last_12_months",
            "this_quarter", "last_quarter")
    for key in keys:
        combo.setCurrentIndex(combo.findData(key))   # fires the handler
        start, end = preset_range(key, date.today())
        assert customize.filters.start_iso() == start
        assert customize.filters.end_iso() == end
    # every preset change re-filtered (called refresh)
    assert len(calls) == len(keys)


def test_report_period_custom_opens_dialog_with_default(qapp, tmp_path, _no_modal):
    from datetime import date
    from mammon.ui.report_filters import resolve_period
    win = _win_with_spending(tmp_path)
    dlg, combo, customize, _ = _period_header(win)
    # No custom range used yet -> Custom defaults the dialog to the ledger's
    # earliest-to-date span (the default selection's range).
    combo.setCurrentIndex(combo.findData("custom"))
    assert (customize.filters.start_iso(), customize.filters.end_iso()) \
        == resolve_period("earliest", win.conn, date.today())
    # The user picks + applies a custom range; it is remembered for the session.
    customize.filters.set_range("2025-02-01", "2025-02-15")
    customize.applied.emit()
    assert win._last_custom_range == ("2025-02-01", "2025-02-15")
    # Re-opening Custom now defaults to that remembered range.
    combo.setCurrentIndex(combo.findData("last_30_days"))
    combo.setCurrentIndex(combo.findData("custom"))
    assert (customize.filters.start_iso(), customize.filters.end_iso()) \
        == ("2025-02-01", "2025-02-15")


# ---- the Period label follows the range the user actually picked ------------
def _report_win(tmp_path, spec=None):
    """A real :class:`ReportWindow` (the combo every hosted report shares)."""
    from mammon.ui.report_window import ReportWindow, CASH_FLOW_SPEC
    spec = spec or CASH_FLOW_SPEC
    # One fresh file per window: reusing the path would re-open a ledger that
    # already holds "Checking" and the seeding below would collide.
    path = tmp_path / f"rw_{spec.title.replace(' ', '_')}.db"
    c = db.init_db(path)
    chk = ledger.create_account(c, "Checking", "checking", opening_balance=500_00)
    ledger.add_transaction(c, chk, "2026-03-01", -40_00, payee="Store")
    return ReportWindow(c, spec=spec), c


def test_period_combo_follows_a_hand_picked_range(qapp, tmp_path):
    """The user's report: editing the dates inside the customization dialog must
    move the Period dropdown. A 17-day span matches no preset, so it reads
    'Custom' -- and applying must NOT reopen the dialog (selecting Custom in the
    combo is what opens it, so a naive sync loops forever)."""
    win, conn = _report_win(tmp_path)
    try:
        assert win.period_combo.currentData() == "ytd"   # the shared default
        reopened, painted = [], []
        win._open_customize = lambda: reopened.append(True)
        # Count repaints at _populate: PyQt holds the bound ``self.refresh`` it
        # was connected to, so replacing the attribute would not be seen.
        real_populate = win._populate
        win._populate = lambda rows: (painted.append(True), real_populate(rows))[1]

        win.filters.set_range("2026-02-05", "2026-02-21")
        win.filters.applied.emit()      # what the Apply button does

        assert win.period_combo.currentData() == "custom"
        assert reopened == []           # the dialog did not reopen
        assert len(painted) == 1        # and the report re-ran exactly once
        # The range the user typed survives the sync untouched.
        assert (win.filters.start_iso(), win.filters.end_iso()) \
            == ("2026-02-05", "2026-02-21")
    finally:
        win.close()
        conn.close()


def test_period_combo_names_a_preset_range_picked_by_hand(qapp, tmp_path):
    """Dates typed in the dialog that happen to equal a preset's range are
    labelled with that preset, not left on 'Custom'."""
    from datetime import date
    from mammon.ui.report_filters import resolve_period
    win, conn = _report_win(tmp_path)
    try:
        # Start somewhere else so the assertion cannot pass by standing still.
        win.period_combo.setCurrentIndex(win.period_combo.findData("last_7_days"))
        start, end = resolve_period("ytd", conn, date.today())
        win.filters.set_range(start, end)
        win.filters.applied.emit()
        assert win.period_combo.currentData() == "ytd"
    finally:
        win.close()
        conn.close()


def test_period_combo_sync_reaches_every_report_kind(qapp, tmp_path):
    """One combo, one sync: the Period label follows the range on every hosted
    report, not just the one this was first noticed on."""
    from mammon.ui.report_window import (CASH_FLOW_SPEC, BY_PAYEE_SPEC,
                                         TRANSACTIONS_SPEC, ITEMIZE_SPEC)
    for spec in (CASH_FLOW_SPEC, BY_PAYEE_SPEC, TRANSACTIONS_SPEC, ITEMIZE_SPEC):
        win, conn = _report_win(tmp_path, spec)
        try:
            win._open_customize = lambda: None
            win.filters.set_range("2026-02-05", "2026-02-21")
            win.filters.applied.emit()
            assert win.period_combo.currentData() == "custom", spec.title
        finally:
            win.close()
            conn.close()


def test_chart_dialog_period_combo_follows_a_hand_picked_range(qapp, tmp_path,
                                                               _no_modal):
    """The legacy chart windows share the same dropdown through
    ``_report_period_header``; their Apply path syncs it too."""
    win = _win_with_spending(tmp_path)
    dlg, combo, customize, calls = _period_header(win)
    try:
        assert combo.currentData() == "ytd"
        customize.filters.set_range("2026-02-05", "2026-02-21")
        customize.applied.emit()
        assert combo.currentData() == "custom"
    finally:
        win.close()
    win.close()


def test_file_menu_has_saveas_and_restore(qapp, tmp_path):
    win = _win_with_spending(tmp_path)
    file_menu = next(m.menu() for m in win.menuBar().actions()
                     if m.text().replace("&", "") == "File")
    labels = {act.text().replace("…", "").strip() for act in file_menu.actions()}
    assert "Save Database As" in labels
    assert "Restore from Backup" in labels
    win.close()


# ---- Save Database As ------------------------------------------------------
def test_save_database_as_switches_window(qapp, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QFileDialog, QMessageBox
    win = _win_with_spending(tmp_path)
    new_path = tmp_path / "copy.db"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(new_path), "")))
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    win._save_db_as_dialog()
    assert new_path.exists()
    assert win.db_path == str(new_path)
    # the copy carries the original data
    assert any(a["name"] == "Checking"
               for a in win.accounts.model.rows())
    win.close()


# ---- Restore from Backup ---------------------------------------------------
def test_restore_from_backup_replaces_current(qapp, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QFileDialog, QMessageBox
    from mammon import backup
    win = _win_with_spending(tmp_path)
    target = win.db_path
    # a snapshot with DIFFERENT content
    snap = tmp_path / "snapshot.bak"
    s = db.init_db(snap)
    ledger.create_account(s, "Solo", "checking", opening_balance=1_00)
    s.close()

    monkeypatch.setattr(backup, "create_backup",
                        lambda *a, **k: None)          # no repo-dir side effects
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(snap), "")))
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: None))

    win._restore_backup_dialog()
    # still pointed at the same file, now holding the snapshot's data
    assert win.db_path == target
    names = {a["name"] for a in win.accounts.model.rows()}
    assert names == {"Solo"}
    win.close()


# ---------------------------------------------------------------------------
# Include hidden accounts (the universal toggle)
# ---------------------------------------------------------------------------
def _hide(conn, name):
    conn.execute("UPDATE accounts SET hidden=1 WHERE name=?", (name,))
    conn.commit()


def test_hidden_toggle_governs_the_picker_and_the_report(qapp, conn):
    from mammon.ui.report_filters import ReportFilterBar

    _hide(conn, "Savings")
    bar = ReportFilterBar(conn, "2026-01-01", "2026-06-30", show_accounts=True)

    # Default: hidden accounts are OUT, as they are everywhere else.
    assert bar.include_hidden() is False
    assert not bar.hidden_check.isChecked()
    names = [bar.account_list.item(i).text() for i in range(bar.account_list.count())]
    assert names == ["Checking"]
    # An all-checked list is a REAL filter here, not "no filter": a report handed
    # None goes on to query every account, including the one just excluded.
    picked = bar.selected_account_ids()
    assert picked is not None and len(picked) == 1

    bar.hidden_check.setChecked(True)
    assert bar.include_hidden() is True
    names = [bar.account_list.item(i).text() for i in range(bar.account_list.count())]
    assert names == ["Checking", "Savings"]
    assert bar.selected_account_ids() is None      # now it really is everything


def test_hidden_toggle_preserves_account_ticks_across_a_rebuild(qapp, conn):
    """Glancing at the roster must not silently undo a selection."""
    from mammon.ui.report_filters import ReportFilterBar

    ledger.create_account(conn, "Brokerage", "investment", opening_balance=0)
    _hide(conn, "Savings")
    bar = ReportFilterBar(conn, "2026-01-01", "2026-06-30", show_accounts=True)
    bar.hidden_check.setChecked(True)
    checking = next(i for i in range(bar.account_list.count())
                    if bar.account_list.item(i).text() == "Checking")
    bar.account_list.item(checking).setCheckState(Qt.Unchecked)
    picked = set(bar.selected_account_ids())

    bar.hidden_check.setChecked(False)      # rebuild without hidden
    bar.hidden_check.setChecked(True)       # ...and back
    assert set(bar.selected_account_ids()) == picked


def test_a_bar_without_the_toggle_matches_the_account_bar(qapp, conn):
    from mammon.ui.report_filters import ReportFilterBar

    _hide(conn, "Savings")
    bar = ReportFilterBar(conn, "2026-01-01", "2026-06-30", show_accounts=True,
                          show_hidden_toggle=False)
    assert bar.hidden_check is None
    assert bar.include_hidden() is False


def test_enter_in_a_date_field_applies_and_never_clears_accounts(qapp, conn):
    """Regression: Enter in the To field wiped every account checkbox. A
    QPushButton in a dialog is autoDefault, so Return fired the FIRST one in the
    focus chain -- the accounts "Clear all" -- instead of Apply."""
    from PyQt5.QtTest import QTest
    from mammon.ui.report_filters import CustomizeDialog

    dlg = CustomizeDialog(conn, "2026-01-01", "2026-06-30", show_accounts=True,
                          categories=["Groceries", "Gas"])
    bar = dlg.filters
    # QDialog only routes Return to a default button that is VISIBLE, so the
    # dialog has to be shown for this to exercise the real path at all.
    dlg.show()
    try:
        # The structural half: no Clear-all button may answer the Return key.
        for btn in (bar._clear_button(lambda: None),):
            assert not btn.autoDefault() and not btn.isDefault()
        assert bar.apply_button.isDefault()

        before = [bar.account_list.item(i).checkState()
                  for i in range(bar.account_list.count())]
        assert before and all(s == Qt.Checked for s in before)

        fired = []
        dlg.applied.connect(lambda: fired.append(1))
        QTest.keyClick(bar.end_edit, Qt.Key_Return)

        after = [bar.account_list.item(i).checkState()
                 for i in range(bar.account_list.count())]
        assert after == before                # nothing was cleared
        assert fired == [1]                   # Enter meant Apply
        assert bar.selected_account_ids() is None
    finally:
        dlg.close()


def test_clear_all_still_clears_when_actually_clicked(qapp, conn):
    from mammon.ui.report_filters import ReportFilterBar

    bar = ReportFilterBar(conn, "2026-01-01", "2026-06-30", show_accounts=True)
    bar.clear_accounts()
    assert bar.selected_account_ids() == []


def test_mark_all_restores_every_item(qapp, conn):
    """Narrowing to one account out of ninety is clear-then-tick-one; widening
    back should be one click, not ninety."""
    from mammon.ui.report_filters import ReportFilterBar

    bar = ReportFilterBar(conn, "2026-01-01", "2026-06-30", show_accounts=True,
                          categories=["Groceries", "Gas"])
    bar.clear_accounts()
    bar.clear_categories()
    assert bar.selected_account_ids() == [] and bar.selected_categories() == set()

    bar.mark_accounts()
    bar.mark_categories()
    # everything checked -> "no filter", the state a fresh bar opens in
    assert bar.selected_account_ids() is None
    assert bar.selected_categories() is None


def test_no_list_button_can_claim_the_return_key(qapp, conn):
    """Structural guard: every Mark/Clear button comes from one factory that
    denies auto-default, so adding a button above Apply can never reclaim Enter
    the way the accounts "Clear all" once did."""
    from PyQt5.QtWidgets import QPushButton
    from mammon.ui.report_filters import CustomizeDialog

    dlg = CustomizeDialog(conn, "2026-01-01", "2026-06-30", show_accounts=True,
                          categories=["Groceries", "Gas"])
    labels = {"Mark all", "Clear all"}
    found = [b for b in dlg.findChildren(QPushButton) if b.text() in labels]
    assert len(found) == 4                      # both lists, both buttons
    for b in found:
        assert not b.autoDefault() and not b.isDefault(), b.text()
    assert dlg.filters.apply_button.isDefault()

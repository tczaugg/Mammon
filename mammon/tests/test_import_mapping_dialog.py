"""The column-mapping wizard: the UI that was missing from the tabular engine.

``mammon.importers.tabular`` could already infer, re-interpret and remember a
delimited file's column map, but nothing ever ASKED the user -- ``apply_wizard_answers``
had no caller outside its own test. These drive the dialog headlessly.

The behaviour that matters most here is that the preview names the SOURCE COLUMN
behind each role. A mapping that picked the wrong column still produces
plausible-looking values (a running-balance column parses as money exactly like an
amount column), so the parsed values alone cannot tell a user whether the mapping
is right -- only the column names can.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger
from mammon.importers import tabular
from mammon.tests import fresh_db


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "m.db")
    yield c
    c.close()


# A statement whose AMOUNT column is ambiguous by value: "Balance" is money-shaped
# in every row, exactly like "Amount". Only the header name distinguishes them --
# which is the whole point of showing source columns in the preview.
CSV = (
    "Date,Description,Amount,Balance\n"
    "2026-03-01,COFFEE SHOP,-4.50,1000.00\n"
    "2026-03-02,PAYCHECK,2500.00,3500.00\n"
    "2026-03-03,GROCERY MART,-82.13,3417.87\n"
)


def _plan(conn, text=CSV):
    return tabular.plan_tabular(conn, text, default_account="Checking",
                                account_type="checking")


def test_dialog_loads_inferred_mapping(qapp, conn):
    from mammon.ui.import_mapping import ImportMappingDialog
    plan = _plan(conn)
    dlg = ImportMappingDialog(plan)
    # inference picked the real amount column, not the balance
    assert dlg.plan.roles.date == "Date"
    assert dlg.plan.roles.amount == "Amount"
    assert dlg.amount_combo.currentData() == "Amount"
    assert dlg.date_combo.currentData() == "Date"


def test_result_preview_titles_name_their_source_column(qapp, conn):
    """The fix for 'how can I tell if it mixed up the headers?' -- every result
    column is titled with the source column it was taken from."""
    from mammon.ui.import_mapping import ImportMappingDialog
    dlg = ImportMappingDialog(_plan(conn))
    titles = [dlg.result_table.horizontalHeaderItem(i).text()
              for i in range(dlg.result_table.columnCount())]
    assert titles[0].startswith("Date  <-  Date")
    assert titles[1].startswith("Amount  <-  Amount")
    assert any("Description" in t for t in titles)   # payee or memo names its source


def test_source_preview_shows_the_files_own_headers(qapp, conn):
    from mammon.ui.import_mapping import ImportMappingDialog
    dlg = ImportMappingDialog(_plan(conn))
    headers = [dlg.source_table.horizontalHeaderItem(i).text()
               for i in range(dlg.source_table.columnCount())]
    assert headers == ["Date", "Description", "Amount", "Balance"]
    assert dlg.source_table.rowCount() == 3
    assert dlg.source_table.item(0, 0).text() == "2026-03-01"


def test_changing_amount_column_re_parses_live(qapp, conn):
    """Pointing Amount at the Balance column re-interprets the whole file through
    the real importer -- the preview is what import would actually do."""
    from mammon.ui.import_mapping import ImportMappingDialog
    dlg = ImportMappingDialog(_plan(conn))
    before = [r.amount_cents for r in dlg.plan.records]
    assert before == [-4_50, 2500_00, -82_13]

    i = dlg.amount_combo.findData("Balance")
    assert i >= 0
    dlg.amount_combo.setCurrentIndex(i)              # triggers _replan

    assert dlg.plan.roles.amount == "Balance"
    assert [r.amount_cents for r in dlg.plan.records] == [1000_00, 3500_00, 3417_87]
    titles = [dlg.result_table.horizontalHeaderItem(i).text()
              for i in range(dlg.result_table.columnCount())]
    assert "Amount  <-  Balance" in titles[1]


def test_balance_column_trips_the_sanity_warning(qapp, conn):
    """Mapping a balance column as the amount produces perfectly plausible
    numbers, so the preview alone will not catch it -- the warning does."""
    from mammon.ui.import_mapping import ImportMappingDialog
    dlg = ImportMappingDialog(_plan(conn))
    assert dlg.warning.text() == ""                  # correct mapping: quiet
    dlg.amount_combo.setCurrentIndex(dlg.amount_combo.findData("Balance"))
    assert "no row is negative" in dlg.warning.text()


def test_answers_round_trip_through_the_engine(qapp, conn):
    from mammon.ui.import_mapping import ImportMappingDialog
    dlg = ImportMappingDialog(_plan(conn))
    dlg.invert.setChecked(True)
    a = dlg.answers()
    assert a["amount_mode"] == "signed"
    assert a["invert_amount"] is True
    assert a["account_type"] == "checking"
    # inverting flips every sign
    assert [r.amount_cents for r in dlg.plan.records] == [4_50, -2500_00, 82_13]


def test_description_checkboxes_drive_the_memo(qapp, conn):
    from mammon.ui.import_mapping import ImportMappingDialog
    from PyQt5.QtCore import Qt
    dlg = ImportMappingDialog(_plan(conn))
    items = {dlg.desc_list.item(i).text(): dlg.desc_list.item(i)
             for i in range(dlg.desc_list.count())}
    assert set(items) == {"Date", "Description", "Amount", "Balance"}
    items["Balance"].setCheckState(Qt.Checked)       # triggers _replan
    assert "Balance" in dlg.plan.roles.description
    assert any("1000.00" in (r.memo or "") for r in dlg.plan.records)


def test_accepting_saves_a_profile_that_suppresses_the_next_prompt(qapp, conn):
    """The loop the whole feature exists for: correct the map, save it, and the
    same format imports silently next time."""
    from mammon.ui.import_mapping import ImportMappingDialog
    dlg = ImportMappingDialog(_plan(conn))
    dlg.amount_combo.setCurrentIndex(dlg.amount_combo.findData("Balance"))
    tabular.accept_profile(conn, dlg.plan, name="test format")

    again = _plan(conn)
    assert again.is_new is False                     # known format -> no prompt
    assert again.roles.amount == "Balance"           # and it remembered the fix


def test_debit_credit_pair_mode(qapp, conn):
    from mammon.ui.import_mapping import ImportMappingDialog
    text = ("Posted,Payee,Withdrawal,Deposit\n"
            "2026-04-01,STORE,25.00,\n"
            "2026-04-02,SALARY,,1500.00\n")
    dlg = ImportMappingDialog(_plan(conn, text))
    dlg.amt_pair.setChecked(True)
    dlg.debit_combo.setCurrentIndex(dlg.debit_combo.findData("Withdrawal"))
    dlg.credit_combo.setCurrentIndex(dlg.credit_combo.findData("Deposit"))
    assert dlg.plan.roles.debit == "Withdrawal"
    assert dlg.plan.roles.credit == "Deposit"
    assert dlg.plan.roles.amount is None
    assert sorted(r.amount_cents for r in dlg.plan.records) == [-25_00, 1500_00]


def test_unparseable_mapping_reports_instead_of_raising(qapp, conn):
    from mammon.ui.import_mapping import ImportMappingDialog
    dlg = ImportMappingDialog(_plan(conn))
    dlg.date_combo.setCurrentIndex(dlg.date_combo.findData("Description"))
    assert dlg.warning.text()                        # says something is wrong
    assert dlg.isVisible() is False                  # and the dialog survived


# ---------------------------------------------------------------------------
# The refine LOOP in MainWindow._offer_import_profile.
#
# These exist because the first cut of this feature deadlocked: it built a
# QMessageBox and called exec_(), bypassing the QMessageBox.question seam the
# headless tests patch, so the suite hung with no one to click. Anything on this
# path that can run unattended must terminate on its own.
# ---------------------------------------------------------------------------
CSV_UI = (
    "Date,Description,Amount,Balance\n"
    "2026-05-01,STORE,-10.00,500.00\n"
    "2026-05-02,REFUND,25.00,525.00\n"
)


def _win(conn):
    from mammon.ui.widgets import MainWindow
    return MainWindow(conn)


def _acct(conn):
    aid = ledger.create_account(conn, "Checking", "checking")
    return aid, {"id": aid, "name": "Checking", "type": "checking"}


def test_adjust_then_save_runs_the_wizard_and_persists_it(qapp, conn, tmp_path,
                                                          monkeypatch):
    """'No' opens the wizard; the revised plan is what gets saved."""
    from PyQt5.QtWidgets import QMessageBox
    p = tmp_path / "stmt.csv"
    p.write_text(CSV_UI, encoding="utf-8")
    aid, acct = _acct(conn)

    answers = iter([QMessageBox.No, QMessageBox.Yes])   # adjust once, then save
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: next(answers)))

    win = _win(conn)
    # Stand in for the modal wizard: repoint Amount at Balance and accept.
    def _fake_wizard(plan):
        return tabular.apply_wizard_answers(
            plan, {"amount_mode": "signed", "amount_col": "Balance"})
    monkeypatch.setattr(win, "_run_mapping_wizard", _fake_wizard)
    try:
        win._offer_import_profile(acct, str(p))
        saved = tabular.get_profile(conn, tabular.fingerprint(
            tabular.locate_and_frame(CSV_UI).header))
        assert saved is not None
        assert saved["roles"].amount == "Balance"      # the WIZARD's choice stuck
    finally:
        win.close()


def test_cancelling_the_wizard_saves_nothing(qapp, conn, tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    p = tmp_path / "stmt.csv"
    p.write_text(CSV_UI, encoding="utf-8")
    aid, acct = _acct(conn)
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.No))
    win = _win(conn)
    monkeypatch.setattr(win, "_run_mapping_wizard", lambda plan: None)   # cancelled
    try:
        win._offer_import_profile(acct, str(p))
        assert conn.execute(
            "SELECT COUNT(*) FROM import_profiles").fetchone()[0] == 0
    finally:
        win.close()


def test_refine_loop_terminates_even_if_the_user_never_settles(qapp, conn,
                                                               tmp_path, monkeypatch):
    """A user who keeps choosing 'adjust' must not spin forever."""
    from PyQt5.QtWidgets import QMessageBox
    p = tmp_path / "stmt.csv"
    p.write_text(CSV_UI, encoding="utf-8")
    aid, acct = _acct(conn)
    calls = {"n": 0}

    def _always_adjust(*a, **k):
        calls["n"] += 1
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_always_adjust))
    win = _win(conn)
    monkeypatch.setattr(win, "_run_mapping_wizard", lambda plan: plan)  # never settles
    try:
        win._offer_import_profile(acct, str(p))          # must RETURN
        assert calls["n"] == win._MAX_MAPPING_ROUNDS
    finally:
        win.close()


def test_prompt_text_names_each_source_column(qapp, conn, tmp_path):
    """The prompt has to make a mix-up visible, not merely show plausible values."""
    aid, acct = _acct(conn)
    win = _win(conn)
    try:
        plan = tabular.plan_tabular(conn, CSV_UI, default_account="Checking")
        text = win._plan_prompt(acct, plan)
        assert "Date    <-  Date" in text
        assert "Amount  <-  Amount" in text
        assert "Description" in text
    finally:
        win.close()


# ---------------------------------------------------------------------------
# Import reporting.
#
# The old wording reported the PENDING QUEUE length while calling it "row(s)
# from the file". Re-importing a file whose rows had all been accepted therefore
# announced "0 row(s) from the file ... 0 new, 0 matching", which reads as "the
# file was empty or unreadable" -- when in fact all 30 rows were recognised and
# correctly refused re-entry. File count and queue count are different numbers.
# ---------------------------------------------------------------------------
def _report(**kw):
    from mammon.ui.widgets import MainWindow
    args = dict(acct={"name": "Venmo"}, path="VenmoStatement_June.csv",
                parsed=0, inserted=0, prior={}, pending=[], new=0)
    args.update(kw)
    return MainWindow._import_report(
        args["acct"], args["path"], args["parsed"], args["inserted"],
        args["prior"], args["pending"], args["new"])


def test_report_all_rows_already_accepted_says_so(qapp):
    """The exact case that confused a user: 30 rows in the file, all previously
    accepted. It must NOT read as an empty file."""
    title, msg = _report(parsed=30, inserted=0, prior={"accepted": 30})
    assert title == "Already imported"
    assert "All 30 transaction(s)" in msg
    assert "already accepted" in msg
    assert "nothing was duplicated" in msg
    # the failure mode being guarded against
    assert not msg.startswith("0 ")
    assert "0 new, 0 matching" not in msg


def test_report_empty_file_is_distinct_from_already_imported(qapp):
    title, msg = _report(parsed=0)
    assert title == "Nothing to import"
    assert "No transactions could be read" in msg
    assert "already" not in msg.lower()          # a different outcome entirely
    assert "Adjust mapping" in msg               # points at the fix


def test_report_partial_reimport_counts_both_halves(qapp):
    title, msg = _report(parsed=30, inserted=7, prior={"accepted": 23},
                         pending=[0] * 7, new=7)
    assert title == "Import ready for review"
    assert "7 of 30" in msg
    assert "other 23 were already imported" in msg
    assert "7 row(s) now await review" in msg


def test_report_first_import_does_not_mention_already_imported(qapp):
    title, msg = _report(parsed=30, inserted=30, prior={"pending": 30},
                         pending=[0] * 30, new=30)
    assert title == "Import ready for review"
    assert "30 of 30" in msg
    assert "already imported" not in msg
    assert "Nothing has been added to the register yet" in msg


def test_entry_states_distinguishes_accepted_from_pending(qapp, conn):
    """entry_states is what makes the 'already imported' message possible."""
    from mammon import import_review
    from mammon.import_review import MappedRow, ReviewEntry
    aid = ledger.create_account(conn, "Venmo", "checking")
    entries = [
        ReviewEntry(mapped=MappedRow(transaction_id=f"T{i}", date="2026-05-01",
                                     amount_cents=-100 * (i + 1), payee=f"P{i}"),
                    label="NEW")
        for i in range(3)
    ]
    assert import_review.persist_entries(conn, aid, entries) == 3
    assert import_review.entry_states(conn, entries) == {"pending": 3}

    # accept one -> the states split, which is what the message reports
    conn.execute("UPDATE review_items SET state='accepted' WHERE id=?",
                 (entries[0].review_id,))
    conn.commit()
    assert import_review.entry_states(conn, entries) == {"accepted": 1, "pending": 2}

    # re-persisting the SAME entries inserts nothing but still resolves review_ids
    assert import_review.persist_entries(conn, aid, entries) == 0
    assert import_review.entry_states(conn, entries) == {"accepted": 1, "pending": 2}


# ---------------------------------------------------------------------------
# Auto-backup only fires on a real change (end to end through MainWindow).
# ---------------------------------------------------------------------------
def test_idle_session_writes_no_autobackups(qapp, tmp_path, monkeypatch):
    from mammon import backup, db
    from mammon.ui.widgets import MainWindow
    p = tmp_path / "m.db"
    c = fresh_db(p)
    bdir = tmp_path / "backups"
    monkeypatch.setattr(backup, "DEFAULT_BACKUP_DIR", bdir)
    win = MainWindow(c, db_path=str(p))
    try:
        for _ in range(5):                 # five ticks, no edits in between
            win._autobackup_tick()
        assert not bdir.exists() or list(bdir.glob("*.bak")) == []
    finally:
        win.close()
        c.close()


def test_autobackup_fires_once_per_change_not_per_tick(qapp, tmp_path, monkeypatch):
    """Counts CALLS, not files: auto-backup names are timestamped to the second,
    so two snapshots inside one second share a filename and the second silently
    overwrites the first. The behaviour under test is whether a snapshot is
    attempted at all."""
    from mammon import backup, db
    from mammon.ui.widgets import MainWindow
    p = tmp_path / "m.db"
    c = fresh_db(p)
    win = MainWindow(c, db_path=str(p))
    calls = []
    monkeypatch.setattr(backup, "create_backup",
                        lambda *a, **k: calls.append(1))
    monkeypatch.setattr(backup, "purge_auto_backups", lambda *a, **k: None)
    try:
        for _ in range(3):                 # idle: nothing attempted
            win._autobackup_tick()
        assert calls == []

        ledger.create_account(c, "Checking", "checking")
        win._autobackup_tick()
        assert len(calls) == 1             # the change was captured

        for _ in range(4):                 # idle again: still nothing
            win._autobackup_tick()
        assert len(calls) == 1

        ledger.create_account(c, "Savings", "savings")
        win._autobackup_tick()
        assert len(calls) == 2             # second change -> second snapshot
    finally:
        win.close()
        c.close()

"""Import-batch grouping, review retention, and review visibility.

Accepting a review row used to make it disappear permanently even though the row
stayed in the database forever -- retained and invisible at once, which is the
worst of both. These cover the three mechanics that fix it: batches as the unit
of an import, retention bounded by subsequent activity rather than elapsed time,
and a visibility setting that can bring actioned rows back on screen.

The review row itself is GROUND TRUTH and is never rewritten by a rename; that
contract is asserted here too, since it is the thing most likely to be undone by
accident later.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, import_review as ir, ledger


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "m.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "Venmo", "checking")


def _batch(conn, account_id, days_ago=0, **kw):
    b = ir.start_batch(conn, account_id, **kw)
    if days_ago:
        conn.execute("UPDATE import_batches SET created_at=datetime('now', ?) "
                     "WHERE id=?", (f"-{int(days_ago)} days", b))
        conn.commit()
    return b


def _row(conn, account_id, batch_id, tid, state="pending", date="2026-01-01"):
    conn.execute(
        "INSERT INTO review_items(account_id, transaction_id, date, amount, payee,"
        " memo, label, state, batch_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (account_id, tid, date, -100, "", "RAW BANK TEXT", "NEW", state, batch_id))
    conn.commit()


def _ids(entries):
    return [e.mapped.transaction_id for e in entries]


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------
def test_one_import_of_several_files_is_one_batch(conn, account):
    """Files fetched together are one import. Some institutions refuse an
    arbitrary date range, so history arrives a month per file -- those files are
    one period, reviewed together, retained together."""
    b = ir.start_batch(conn, account, source="import", file_count=3,
                       note="jan.csv, feb.csv, mar.csv")
    for i in range(6):
        _row(conn, account, b, f"T{i}")
    batches = conn.execute(
        "SELECT COUNT(*) FROM import_batches WHERE account_id=?", (account,)).fetchone()[0]
    assert batches == 1
    assert conn.execute("SELECT file_count FROM import_batches WHERE id=?",
                        (b,)).fetchone()[0] == 3
    assert len({e.batch_id for e in ir.load_review(conn, account, ir.SHOW_ALL)}) == 1


def test_current_batch_is_the_newest(conn, account):
    first = ir.start_batch(conn, account)
    second = ir.start_batch(conn, account)
    assert ir.current_batch_id(conn, account) == second
    assert first != second


# ---------------------------------------------------------------------------
# retention: three batches OR one year, whichever is MORE
# ---------------------------------------------------------------------------
def test_batch_count_keeps_history_for_an_infrequent_importer(conn, account):
    """Someone importing twice a year would lose everything to a time window, so
    the newest N batches are kept however old they are."""
    old = _batch(conn, account, days_ago=900)
    _row(conn, account, old, "ancient", state="accepted")
    for d in (800, 700):
        _row(conn, account, _batch(conn, account, days_ago=d), f"b{d}", state="accepted")
    ir.purge_old_batches(conn, account)
    assert "ancient" in _ids(ir.load_review(conn, account, ir.SHOW_ALL))


def test_one_year_keeps_history_for_a_frequent_importer(conn, account):
    """Someone importing weekly would lose a fortnight to a batch count, so
    anything inside the last year is kept however many batches followed it."""
    recent = _batch(conn, account, days_ago=200)
    _row(conn, account, recent, "within-a-year", state="accepted")
    for d in (5, 4, 3, 2, 1):                     # five newer batches
        _row(conn, account, _batch(conn, account, days_ago=d), f"n{d}", state="accepted")
    ir.purge_old_batches(conn, account)
    assert "within-a-year" in _ids(ir.load_review(conn, account, ir.SHOW_ALL))


def test_batch_past_both_rules_is_purged(conn, account):
    doomed = _batch(conn, account, days_ago=900)
    _row(conn, account, doomed, "doomed", state="accepted")
    for d in (5, 4, 3):
        _row(conn, account, _batch(conn, account, days_ago=d), f"n{d}", state="accepted")
    assert ir.purge_old_batches(conn, account) == 1
    assert "doomed" not in _ids(ir.load_review(conn, account, ir.SHOW_ALL))


def test_pending_is_never_purged_however_old(conn, account):
    """Unreviewed work is not history. A user can import repeatedly without
    reviewing any of it, and none of that may be dropped to satisfy retention."""
    ancient = _batch(conn, account, days_ago=1500)
    _row(conn, account, ancient, "never-reviewed", state="pending")
    _row(conn, account, ancient, "was-accepted", state="accepted")
    for d in (5, 4, 3):
        _row(conn, account, _batch(conn, account, days_ago=d), f"n{d}", state="accepted")
    ir.purge_old_batches(conn, account)
    survivors = _ids(ir.load_review(conn, account, ir.SHOW_ALL))
    assert "never-reviewed" in survivors
    # The WHOLE batch survives, accepted siblings included: pending work holds it
    # open, and the "pending + accepted" view exists precisely to show those
    # siblings alongside the rows still to do. Purging them would empty it.
    assert "was-accepted" in survivors
    assert set(_ids(ir.load_review(conn, account, ir.SHOW_BATCH))) == {
        "never-reviewed", "was-accepted"}


def test_accepting_a_row_does_not_delete_it(conn, account):
    b = ir.start_batch(conn, account)
    _row(conn, account, b, "T1")
    conn.execute("UPDATE review_items SET state='accepted' WHERE transaction_id='T1'")
    conn.commit()
    assert _ids(ir.load_review(conn, account, ir.SHOW_PENDING)) == []
    assert _ids(ir.load_review(conn, account, ir.SHOW_ALL)) == ["T1"]


# ---------------------------------------------------------------------------
# visibility
# ---------------------------------------------------------------------------
def test_show_batch_spans_every_batch_holding_pending_work(conn, account):
    """Not "the current batch": several imports can pile up unreviewed, so the
    review in progress spans batches and so must the accepted rows shown with it."""
    b1, b2, b3 = (ir.start_batch(conn, account) for _ in range(3))
    _row(conn, account, b1, "b1-pending", state="pending")
    _row(conn, account, b1, "b1-accepted", state="accepted")
    _row(conn, account, b2, "b2-accepted", state="accepted")   # no pending here
    _row(conn, account, b3, "b3-pending", state="pending")
    _row(conn, account, b3, "b3-accepted", state="accepted")

    got = set(_ids(ir.load_review(conn, account, ir.SHOW_BATCH)))
    assert got == {"b1-pending", "b1-accepted", "b3-pending", "b3-accepted"}
    assert "b2-accepted" not in got            # that batch has no pending work

    assert set(_ids(ir.load_review(conn, account, ir.SHOW_PENDING))) == {
        "b1-pending", "b3-pending"}
    assert len(ir.load_review(conn, account, ir.SHOW_ALL)) == 5


def test_show_batch_falls_back_to_newest_when_nothing_pending(conn, account):
    """Finishing a review is exactly when you want to look at what you just did,
    so an empty pending set must not empty the list."""
    b = ir.start_batch(conn, account)
    _row(conn, account, b, "done", state="accepted")
    assert _ids(ir.load_review(conn, account, ir.SHOW_BATCH)) == ["done"]


def test_unknown_mode_degrades_to_pending(conn, account):
    b = ir.start_batch(conn, account)
    _row(conn, account, b, "p", state="pending")
    _row(conn, account, b, "a", state="accepted")
    assert _ids(ir.load_review(conn, account, "nonsense")) == ["p"]


def test_entries_carry_state_so_the_panel_can_grey_them(conn, account):
    b = ir.start_batch(conn, account)
    _row(conn, account, b, "p", state="pending")
    _row(conn, account, b, "a", state="accepted")
    by = {e.mapped.transaction_id: e for e in ir.load_review(conn, account, ir.SHOW_ALL)}
    assert by["p"].state == "pending" and by["p"].is_actioned is False
    assert by["a"].state == "accepted" and by["a"].is_actioned is True
    assert by["a"].batch_id == b


# ---------------------------------------------------------------------------
# the review row is ground truth
# ---------------------------------------------------------------------------
DESC = "POS DEBIT 1234 SAFEWAY #918 SEATTLE WA"


def test_no_payee_is_manufactured_from_the_description(conn, account):
    [entry] = ir.build_review(conn, account, [
        {"transactionId": "T1", "postedDate": "2026-05-01", "amount": "12.34",
         "isDebit": True, "statementDescription": DESC}])
    assert entry.mapped.payee == ""          # the source sent none; none invented
    assert entry.mapped.memo == DESC         # and the bank text is verbatim


def test_predicting_a_payee_does_not_rewrite_the_review_row(conn, account):
    """The register's suggestion used to be written back onto the shared MappedRow,
    so selecting a row silently rewrote the review list's own Payee cell."""
    from mammon import rename_tree
    for _ in range(4):                       # the high-confidence floor
        rename_tree.learn(conn, DESC, "Safeway")
    [entry] = ir.build_review(conn, account, [
        {"transactionId": "T1", "postedDate": "2026-05-01", "amount": "12.34",
         "isDebit": True, "statementDescription": DESC}])
    m = entry.mapped
    predicted, _cat = ir.predict_fields(conn, m)
    assert predicted == "Safeway"             # the register gets the rename
    assert m.payee == ""                      # the review row does NOT
    assert m.memo == DESC


def test_transfer_rows_keep_their_parsed_payee(conn, account):
    """A "Transfer from X" payee is not a rename -- it is the same parse that
    produced is_transfer / transfer_account, so it stays."""
    [entry] = ir.build_review(conn, account, [
        {"transactionId": "X1", "postedDate": "2026-05-01", "amount": "500.00",
         "isDebit": False,
         "statementDescription": "SHARE TRANSFER FROM SHARE ACCOUNT: 0002"}])
    assert entry.mapped.is_transfer is True
    assert entry.mapped.payee == "Transfer from Share Account"


def test_persisted_row_round_trips_as_ground_truth(conn, account):
    b = ir.start_batch(conn, account)
    entries = ir.build_review(conn, account, [
        {"transactionId": "T1", "postedDate": "2026-05-01", "amount": "12.34",
         "isDebit": True, "statementDescription": DESC}])
    ir.persist_entries(conn, account, entries, batch_id=b)
    [loaded] = ir.load_review(conn, account, ir.SHOW_ALL)
    assert loaded.mapped.payee == ""
    assert loaded.mapped.memo == DESC
    assert loaded.batch_id == b


# ---------------------------------------------------------------------------
# Accepting a row in a mode that SHOWS actioned rows must retire it in place.
#
# Reported: "when I accept a transaction (in pending + accepted mode) the
# transaction vanishes. If I switch to pending only, then back, it becomes
# visible again." The panel deleted the entry from its in-memory list on accept
# -- correct when it only ever showed pending rows -- so the row disappeared
# from the one view whose purpose is to keep it, and only came back when the
# toggle forced a re-query of what the list had thrown away.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


def _panel(conn, account_id, mode):
    from mammon.ui import prefs
    from mammon.ui.import_review_widget import ImportReviewPanel
    prefs.set_review_visibility(account_id, mode)
    panel = ImportReviewPanel(conn, account_id)
    panel.set_entries(ir.load_review(conn, account_id, mode))
    return panel


def _seed(conn, account_id):
    b = ir.start_batch(conn, account_id)
    entries = ir.build_review(conn, account_id, [
        {"transactionId": f"T{i}", "postedDate": f"2026-05-0{i+1}",
         "amount": "10.00", "isDebit": True,
         "statementDescription": f"STORE {i}"} for i in range(3)])
    ir.persist_entries(conn, account_id, entries, batch_id=b)
    return b


def test_accepted_row_stays_visible_and_greys_in_pending_plus_accepted(
        qapp, conn, account):
    from mammon.ui import prefs
    # A register line for the first seeded row, so it classifies MATCHING and can
    # be ACCEPTED -- discard is no longer a stand-in for "action a row", because
    # it deletes.
    ledger.add_transaction(conn, account, "2026-05-01", -1000, payee="Store 0")
    _seed(conn, account)
    panel = _panel(conn, account, prefs.VIS_BATCH)
    assert panel.table.rowCount() == 3
    assert panel._entries[0].is_matching

    panel.accept_index(0)

    assert panel.table.rowCount() == 3, "the row must NOT vanish from this view"
    assert panel._entries[0].is_actioned is True
    assert panel._entries[0].state == "accepted"
    # and a re-query agrees with what is on screen -- the bug was that it did not
    reloaded = ir.load_review(conn, account, prefs.VIS_BATCH)
    assert len(reloaded) == 3
    panel.deleteLater()


def test_a_discarded_row_leaves_every_view(qapp, conn, account):
    """Discard deletes its ``review_items`` row, so greying it would put the
    screen at odds with a re-query -- the same disagreement the greying was
    introduced to fix, pointed the other way."""
    from mammon.ui import prefs
    _seed(conn, account)
    panel = _panel(conn, account, prefs.VIS_BATCH)
    assert panel.table.rowCount() == 3

    panel.discard_index(0)

    assert panel.table.rowCount() == 2
    assert len(ir.load_review(conn, account, prefs.VIS_BATCH)) == 2
    panel.deleteLater()


def test_accepted_row_still_disappears_in_pending_only(qapp, conn, account):
    """The old behaviour is still right for the view that hides actioned rows."""
    from mammon.ui import prefs
    _seed(conn, account)
    panel = _panel(conn, account, prefs.VIS_PENDING)
    assert panel.table.rowCount() == 3
    panel.discard_index(0)
    assert panel.table.rowCount() == 2
    panel.deleteLater()


def test_selection_advances_past_actioned_rows(qapp, conn, account):
    """Auto-advance must skip the row just retired rather than landing on it."""
    from mammon.ui import prefs
    _seed(conn, account)
    panel = _panel(conn, account, prefs.VIS_BATCH)
    panel.discard_index(0)
    cur = panel.current_entry()
    assert cur is not None
    assert cur.is_actioned is False, "selection landed on an already-actioned row"
    panel.deleteLater()


# ---------------------------------------------------------------------------
# Clicking an ALREADY-ACTIONED row must point at what it produced, never offer
# to enter it again.
#
# Reported: "when I click on an accepted transaction, instead of highlighting
# the transaction in the register, it creates a new row at the bottom of the
# register as if it were a pending transaction." An accepted row keeps the NEW
# label it arrived with, so the is_new branch opened a fresh editable pending
# row for a transaction that had already been posted.
# ---------------------------------------------------------------------------
def test_actioned_entry_hydrates_the_transaction_it_created(conn, account):
    b = ir.start_batch(conn, account)
    entries = ir.build_review(conn, account, [
        {"transactionId": "T1", "postedDate": "2026-05-01", "amount": "12.34",
         "isDebit": True, "statementDescription": "STORE"}])
    ir.persist_entries(conn, account, entries, batch_id=b)
    e = entries[0]
    txn_id = ir.save_new(conn, account, e.mapped, payee="Store",
                         review_id=e.review_id)

    [loaded] = ir.load_review(conn, account, ir.SHOW_ALL)
    assert loaded.is_actioned is True
    assert loaded.accepted_txn_id == txn_id     # what the selection must target
    assert loaded.is_new is True                # still labelled NEW from arrival


def test_selecting_an_actioned_row_highlights_instead_of_opening_a_pending_row(
        qapp, conn, account, monkeypatch):
    from mammon.ui import prefs
    from mammon.ui.widgets import MainWindow
    b = ir.start_batch(conn, account)
    entries = ir.build_review(conn, account, [
        {"transactionId": "T1", "postedDate": "2026-05-01", "amount": "12.34",
         "isDebit": True, "statementDescription": "STORE"}])
    ir.persist_entries(conn, account, entries, batch_id=b)
    txn_id = ir.save_new(conn, account, entries[0].mapped, payee="Store",
                         review_id=entries[0].review_id)
    prefs.set_review_visibility(account, prefs.VIS_ALL)

    win = MainWindow(conn)
    try:
        reg = win.open_register(account)
        [actioned] = ir.load_review(conn, account, ir.SHOW_ALL)
        reg.review_panel.set_entries([actioned])
        reg.review_panel.show()

        selected, pending = [], []
        monkeypatch.setattr(reg, "select_txn", lambda t: selected.append(t))
        monkeypatch.setattr(reg, "_show_pending", lambda e: pending.append(e))

        reg._on_review_row_selected(actioned)
        assert selected == [txn_id], "should highlight the posted transaction"
        assert pending == [], "must NOT offer to enter it a second time"
    finally:
        win.close()


# ---------------------------------------------------------------------------
# Every way of opening the panel must honour the account's saved visibility.
#
# Reported: "when I click on Review and I'm in pending + accepted mode, only the
# pending transactions show until I switch back and forth between modes." Each
# entry point built its own list with load_pending(), so the panel disagreed
# with its own toggle until the toggle was flipped and forced a re-query.
# ---------------------------------------------------------------------------
def test_reopening_review_honours_the_saved_visibility(qapp, conn, account):
    from mammon.ui import prefs
    from mammon.ui.widgets import MainWindow
    b = ir.start_batch(conn, account)
    _row(conn, account, b, "still-pending", state="pending")
    _row(conn, account, b, "already-done", state="accepted")
    prefs.set_review_visibility(account, prefs.VIS_BATCH)

    win = MainWindow(conn)
    try:
        reg = win.open_register(account)
        reg.reopen_review()                       # the toolbar Review… action
        shown = {e.mapped.transaction_id for e in reg.review_panel._entries}
        assert shown == {"still-pending", "already-done"}, (
            "Review... must show what the toggle asks for, without flipping it")
    finally:
        win.close()


def test_reopening_in_pending_only_shows_just_pending(qapp, conn, account):
    from mammon.ui import prefs
    from mammon.ui.widgets import MainWindow
    b = ir.start_batch(conn, account)
    _row(conn, account, b, "still-pending", state="pending")
    _row(conn, account, b, "already-done", state="accepted")
    prefs.set_review_visibility(account, prefs.VIS_PENDING)

    win = MainWindow(conn)
    try:
        reg = win.open_register(account)
        reg.reopen_review()
        shown = {e.mapped.transaction_id for e in reg.review_panel._entries}
        assert shown == {"still-pending"}
    finally:
        win.close()


def test_no_ui_path_loads_pending_directly():
    """Guard the fix: every entry point must go through load_review (which
    applies the toggle), not load_pending (which cannot)."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "ui" / "widgets.py"
    assert "load_pending(self.conn" not in src.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The pending register row must survive every data() role.
#
# Reported crash: entering a category on a new transaction and pressing Enter
# raised IndexError at models.py data() -> self._rows[row]. The pending row's
# index IS len(self._rows) (see RegisterModel.is_pending_row), so any branch
# that dereferences self._rows[row] without excluding it is a guaranteed crash.
# The ToolTipRole branch checked `not blank` but not `not pending`, and its
# inner `not pending` test came AFTER the dereference. Pressing Enter asks the
# view for that cell's tooltip.
# ---------------------------------------------------------------------------
def test_pending_row_survives_every_data_role(qapp, conn, account):
    """Every role, every column, on the pending row -- none may raise."""
    from PyQt5.QtCore import Qt
    from mammon.ui.models import RegisterModel

    b = ir.start_batch(conn, account)
    entries = ir.build_review(conn, account, [
        {"transactionId": "T1", "postedDate": "2026-05-01", "amount": "12.34",
         "isDebit": True, "statementDescription": "STORE"}])
    ir.persist_entries(conn, account, entries, batch_id=b)

    m = RegisterModel(conn, account)
    m.set_pending(entries[0])
    row = m.pending_row()
    assert m.is_pending_row(row)
    assert row == len(m._rows), "the pending row sits one past the real rows"

    roles = (Qt.DisplayRole, Qt.EditRole, Qt.ToolTipRole, Qt.DecorationRole,
             Qt.TextAlignmentRole, Qt.ForegroundRole, Qt.BackgroundRole,
             m.SECOND_LINE_ROLE)
    for col in range(m.columnCount()):
        idx = m.index(row, col)
        for role in roles:
            m.data(idx, role)          # must not raise


def test_pending_category_tooltip_is_the_exact_reported_crash(qapp, conn, account):
    from PyQt5.QtCore import Qt
    from mammon.ui.models import RegisterModel

    b = ir.start_batch(conn, account)
    entries = ir.build_review(conn, account, [
        {"transactionId": "T1", "postedDate": "2026-05-01", "amount": "12.34",
         "isDebit": True, "statementDescription": "STORE"}])
    ir.persist_entries(conn, account, entries, batch_id=b)
    m = RegisterModel(conn, account)
    m.set_pending(entries[0])
    idx = m.index(m.pending_row(), m.CATEGORY)
    assert m.data(idx, Qt.ToolTipRole) is None      # used to raise IndexError


def test_real_rows_still_get_their_category_tooltip(qapp, conn, account):
    """The guard must not silence the tooltip on committed rows."""
    from PyQt5.QtCore import Qt
    from mammon import ledger as L
    from mammon.ui.models import RegisterModel
    L.add_transaction(conn, account, "2026-05-01", -50_00, payee="Store")
    m = RegisterModel(conn, account)
    idx = m.index(0, m.CATEGORY)
    m.data(idx, Qt.ToolTipRole)        # no split -> None, but must not raise
    assert m.rowCount() >= 1


def test_two_accepted_rows_select_two_different_register_rows(qapp, conn, account,
                                                              monkeypatch):
    """Distinct accepted rows must resolve to their OWN register transactions.

    The link is review_items.accepted_txn_id (a NEW row saved into the register)
    or matched_txn_id (a MATCHING row reconciled against an existing line) -- an
    exact id both ways, never a payee or amount heuristic. Same-payee rows are
    used here because that is the case where a sloppy fallback would collapse
    two rows onto one transaction."""
    from mammon.ui import prefs
    from mammon.ui.widgets import MainWindow

    b = ir.start_batch(conn, account)
    rows = [{"transactionId": f"T{i}", "postedDate": f"2026-05-0{i+1}",
             "amount": f"{10 + i}.00", "isDebit": True,
             "statementDescription": "STANDARD TRANSFER"} for i in range(3)]
    entries = ir.build_review(conn, account, rows)
    ir.persist_entries(conn, account, entries, batch_id=b)
    txn_ids = [ir.save_new(conn, account, e.mapped, payee="Standard Transfer",
                           review_id=e.review_id) for e in entries]
    assert len(set(txn_ids)) == 3, "three saves must create three transactions"
    prefs.set_review_visibility(account, prefs.VIS_ALL)

    win = MainWindow(conn)
    try:
        reg = win.open_register(account)
        loaded = ir.load_review(conn, account, ir.SHOW_ALL)
        assert len({e.accepted_txn_id for e in loaded}) == 3, (
            "each review row must carry its own accepted_txn_id")
        reg.review_panel.set_entries(loaded)
        reg.review_panel.show()

        picked = []
        monkeypatch.setattr(reg, "select_txn", lambda t: picked.append(t))
        for e in loaded:
            reg._on_review_row_selected(e)
        assert len(picked) == 3
        assert len(set(picked)) == 3, (
            "identical payees must NOT collapse onto one register transaction")
        assert set(picked) == set(txn_ids)

        # and the ids resolve to real, distinct rows in the register itself
        assert len({reg.model.row_for_txn(t) for t in picked}) == 3
        assert all(reg.model.row_for_txn(t) >= 0 for t in picked)
    finally:
        win.close()


def test_in_session_accept_can_still_be_clicked_back_to(qapp, conn, account,
                                                        monkeypatch):
    """Accept a row, then click it: it must highlight what it just created.

    The earlier test loaded entries from the DB, which hid this: the panel's
    live entry objects come from build_review and are NEVER reloaded, so
    retiring one in place recorded that it was accepted but not what it became.
    accepted_txn_id stayed None and the click highlighted nothing."""
    from mammon.ui import prefs
    from mammon.ui.widgets import MainWindow

    b = ir.start_batch(conn, account)
    entries = ir.build_review(conn, account, [
        {"transactionId": "T1", "postedDate": "2026-05-01", "amount": "12.34",
         "isDebit": True, "statementDescription": "STORE"}])
    ir.persist_entries(conn, account, entries, batch_id=b)
    prefs.set_review_visibility(account, prefs.VIS_BATCH)

    win = MainWindow(conn)
    try:
        reg = win.open_register(account)
        reg.review_panel.set_entries(ir.load_review(conn, account, ir.SHOW_BATCH))
        reg.review_panel.show()
        live = reg.review_panel._entries[0]
        assert live.accepted_txn_id is None          # not yet accepted

        txn_id = reg.review_panel.accept_new(
            live, {"date": "2026-05-01", "payee": "Store", "category": "",
                   "memo": "", "amount_cents": -1234})

        assert live.is_actioned is True
        assert live.accepted_txn_id == txn_id, (
            "the retired row must remember the transaction it created")

        picked = []
        monkeypatch.setattr(reg, "select_txn", lambda t: picked.append(t))
        reg._on_review_row_selected(live)
        assert picked == [txn_id], "clicking it must highlight that transaction"
    finally:
        win.close()


def test_accepting_with_edits_leaves_the_review_row_untouched(qapp, conn, account):
    """Edits made in the register's pending row go to the LEDGER, not back onto
    the review row. accept_new used to overwrite mapped.date / amount_cents /
    check_number, so the greyed row afterwards showed the edit rather than what
    the bank sent -- and disagreed with its own stored row until a reload."""
    from mammon import ledger as L
    from mammon.ui import prefs
    from mammon.ui.widgets import MainWindow

    b = ir.start_batch(conn, account)
    entries = ir.build_review(conn, account, [
        {"transactionId": "T1", "postedDate": "2026-05-01", "amount": "12.34",
         "isDebit": True, "statementDescription": "STORE", "checkNumber": "101"}])
    ir.persist_entries(conn, account, entries, batch_id=b)
    prefs.set_review_visibility(account, prefs.VIS_BATCH)

    win = MainWindow(conn)
    try:
        reg = win.open_register(account)
        reg.review_panel.set_entries(ir.load_review(conn, account, ir.SHOW_BATCH))
        reg.review_panel.show()
        live = reg.review_panel._entries[0]

        txn_id = reg.review_panel.accept_new(live, {
            "date": "2026-05-09",            # user corrected all three
            "payee": "Store", "category": "", "memo": "",
            "num": "999", "amount_cents": -9999,
        })

        # the LEDGER got the edits
        txn = L.get_transaction(conn, txn_id)
        assert txn["date"] == "2026-05-09"
        assert txn["amount"] == -9999
        assert (txn["num"] or "") == "999"

        # the review row still holds what the bank sent
        assert live.mapped.date == "2026-05-01"
        assert live.mapped.amount_cents == -1234
        assert live.mapped.check_number == "101"

        # and memory agrees with storage -- the inconsistency that caused the
        # earlier "it changes when I toggle" reports
        [stored] = ir.load_review(conn, account, ir.SHOW_ALL)
        assert stored.mapped.date == live.mapped.date
        assert stored.mapped.amount_cents == live.mapped.amount_cents
        assert stored.mapped.check_number == live.mapped.check_number
    finally:
        win.close()


def test_edited_num_survives_a_reload(qapp, conn, account):
    """Num is the one review cell the user edits directly, and it is a real
    correction -- it must be saved like every other field, not held only in
    memory where a restart discarded it."""
    from mammon.ui.import_review_widget import ImportReviewPanel
    b = ir.start_batch(conn, account)
    entries = ir.build_review(conn, account, [
        {"transactionId": "T1", "postedDate": "2026-05-01", "amount": "12.34",
         "isDebit": True, "statementDescription": "VENMO PAYMENT"}])
    ir.persist_entries(conn, account, entries, batch_id=b)

    panel = ImportReviewPanel(conn, account)
    panel.set_entries(ir.load_review(conn, account, ir.SHOW_ALL))
    panel.table.item(0, 2).setText("Venmo")        # NUM column
    assert panel._entries[0].mapped.check_number == "Venmo"

    # a fresh read -- as after a restart -- still has it
    [reloaded] = ir.load_review(conn, account, ir.SHOW_ALL)
    assert reloaded.mapped.check_number == "Venmo"
    panel.deleteLater()


# ---------------------------------------------------------------------------
# Switching accounts with a cell editor OPEN.
#
# Reported: "clicking on a different account in the accounts list while editing
# a transaction crashes Mammon with no error message." No message because it is
# not a Python exception -- the crash handler never sees it. Switching the
# stacked widget away from a view with a live editor destroys that editor
# mid-commit at the C++ level. The editor must be committed and closed first.
# ---------------------------------------------------------------------------
def test_switching_accounts_closes_an_open_editor_first(qapp, conn, account,
                                                        monkeypatch):
    from PyQt5.QtWidgets import QAbstractItemView, QMessageBox
    from mammon import ledger as L
    from mammon.ui.widgets import MainWindow

    other = L.create_account(conn, "Checking", "checking")
    L.add_transaction(conn, account, "2026-05-01", -50_00, payee="Store")

    # Leaving a register mid-edit now ASKS; answer Save so the switch proceeds.
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Save))
    win = MainWindow(conn)
    try:
        reg = win.open_register(account)
        idx = reg.model.index(0, reg.model.PAYEE)
        reg.view.setCurrentIndex(idx)
        reg.view.edit(idx)                       # open a real editor
        assert reg.view.state() == QAbstractItemView.EditingState
        assert reg.view.viewport().focusWidget() is not None

        win.open_register(other)                 # the crashing gesture

        # the editor is gone, and the switch completed
        assert reg.view.state() != QAbstractItemView.EditingState
        assert win.stack.currentWidget() is win._registers[other]
    finally:
        win.close()


def test_commit_open_editor_is_a_no_op_with_nothing_open(qapp, conn, account):
    from mammon import ledger as L
    from mammon.ui.widgets import MainWindow
    L.add_transaction(conn, account, "2026-05-01", -50_00, payee="Store")
    win = MainWindow(conn)
    try:
        reg = win.open_register(account)
        assert reg.commit_open_editor() is False    # nothing to close
    finally:
        win.close()


def test_editor_closes_even_when_focus_already_left_it(qapp, conn, account):
    """The first fix relied on view.viewport().focusWidget(), but clicking the
    accounts list moves focus OUT of the editor before the switch runs -- so the
    editor was still open, focusWidget() was already None, and the guard bailed
    out. Detection must not depend on where focus currently is."""
    from PyQt5.QtWidgets import QAbstractItemView
    from mammon import ledger as L
    from mammon.ui.widgets import MainWindow

    other = L.create_account(conn, "Checking", "checking")
    L.add_transaction(conn, account, "2026-05-01", -50_00, payee="Store")
    win = MainWindow(conn)
    try:
        reg = win.open_register(account)
        idx = reg.model.index(0, reg.model.PAYEE)
        reg.view.setCurrentIndex(idx)
        reg.view.edit(idx)
        assert reg.view.state() == QAbstractItemView.EditingState

        # simulate focus having moved to the accounts list already
        win.accounts.setFocus()

        assert reg.commit_open_editor() is True, (
            "must close the editor without relying on focusWidget()")
        assert reg.view.state() != QAbstractItemView.EditingState
    finally:
        win.close()


def test_hiding_a_register_closes_its_editor(qapp, conn, account, monkeypatch):
    """Every route out of a register ends in the widget being hidden, so the
    guard lives on hideEvent rather than on one caller."""
    from PyQt5.QtWidgets import QAbstractItemView, QMessageBox
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Save))
    from mammon import ledger as L
    from mammon.ui.widgets import MainWindow

    other = L.create_account(conn, "Elsewhere", "checking")
    L.add_transaction(conn, account, "2026-05-01", -50_00, payee="Store")
    win = MainWindow(conn)
    # The window must actually be SHOWN: Qt only delivers a hide event to a
    # widget that was visible, so a never-shown window makes hide() a no-op and
    # the hook look broken when it is not.
    win.show()
    qapp.processEvents()
    try:
        reg = win.open_register(account)
        qapp.processEvents()
        idx = reg.model.index(0, reg.model.PAYEE)
        reg.view.setCurrentIndex(idx)
        reg.view.edit(idx)
        assert reg.view.state() == QAbstractItemView.EditingState

        win.open_register(other)         # the reported gesture, end to end
        qapp.processEvents()
        assert reg.view.state() != QAbstractItemView.EditingState
    finally:
        win.close()


# ---------------------------------------------------------------------------
# Leaving a register mid-edit ASKS. Clicking another account is navigation, not
# a decision to commit whatever is half-typed into a cell.
# ---------------------------------------------------------------------------
def _two_accounts(conn, account):
    from mammon import ledger as L
    other = L.create_account(conn, "Elsewhere", "checking")
    L.add_transaction(conn, account, "2026-05-01", -50_00, payee="Store")
    return other


def _editing(win, account):
    from PyQt5.QtWidgets import QAbstractItemView
    reg = win.open_register(account)
    idx = reg.model.index(0, reg.model.PAYEE)
    reg.view.setCurrentIndex(idx)
    reg.view.edit(idx)
    assert reg.view.state() == QAbstractItemView.EditingState
    return reg


def test_cancel_keeps_you_on_the_account_still_being_edited(qapp, conn, account,
                                                            monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow
    other = _two_accounts(conn, account)
    win = MainWindow(conn)
    win.show(); qapp.processEvents()
    try:
        reg = _editing(win, account)
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Cancel))
        win.open_register(other)
        assert win.stack.currentWidget() is reg, "Cancel must not navigate away"
        assert reg.has_open_editor() is True, "and must not close the editor"
    finally:
        win.close()


def test_discard_leaves_the_transaction_unchanged(qapp, conn, account, monkeypatch):
    from PyQt5.QtWidgets import QLineEdit, QMessageBox
    from mammon import ledger as L
    from mammon.ui.widgets import MainWindow
    other = _two_accounts(conn, account)
    win = MainWindow(conn)
    win.show(); qapp.processEvents()
    try:
        reg = _editing(win, account)
        ed = reg.view.viewport().focusWidget()
        if isinstance(ed, QLineEdit):
            ed.setText("TYPED BUT NOT MEANT")
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.Discard))
        win.open_register(other)
        qapp.processEvents()
        assert win.stack.currentWidget() is win._registers[other]
        rows = L.register_rows(conn, account)
        assert rows[0]["payee"] == "Store", "a discarded edit must not reach the ledger"
    finally:
        win.close()


def test_no_prompt_when_nothing_is_being_edited(qapp, conn, account, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow
    other = _two_accounts(conn, account)
    win = MainWindow(conn)
    win.show(); qapp.processEvents()
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.Save))
    try:
        win.open_register(account)
        win.open_register(other)
        assert asked == [], "switching with no edit open must not interrupt"
    finally:
        win.close()


# ---- the accepted sound reaches the review flow -----------------------------
def test_review_accept_and_save_sound_but_discard_does_not(qapp, conn):
    """Review is where the confirmation sound earns its keep: it is the
    high-volume accept gesture, done heads-down at the keyboard. It fires for a
    MATCHING row accepted and a NEW row saved -- both put a transaction in the
    book -- and stays silent for a discard, which does not."""
    from mammon.ui.import_review_widget import ImportReviewPanel

    acct = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    existing = ledger.add_transaction(conn, acct, "2026-01-05", -25_00, payee="Shop")
    rows = [
        {"transactionId": "R-M", "postedDate": "2026-01-05", "amount": "25.00",
         "isDebit": True, "statementDescription": "SHOP #123"},        # matches
        {"transactionId": "R-N", "postedDate": "2026-01-09", "amount": "11.00",
         "isDebit": True, "statementDescription": "BRAND NEW"},        # new
        {"transactionId": "R-D", "postedDate": "2026-01-10", "amount": "12.00",
         "isDebit": True, "statementDescription": "TO DISCARD"},       # dropped
    ]
    entries = ir.build_review(conn, acct, rows)
    ir.persist_entries(conn, acct, entries)

    panel = ImportReviewPanel(conn, acct)
    panel.set_entries(ir.load_pending(conn, acct))
    beeps = []
    panel.transactionSaved.connect(lambda: beeps.append(1))

    matching = [i for i, e in enumerate(panel._entries) if e.is_matching]
    assert matching, "expected the SHOP row to be classified MATCHING"
    panel.accept_index(matching[0])
    assert len(beeps) == 1
    assert ledger.get_transaction(conn, existing)["cleared"] == 1

    new_rows = [i for i, e in enumerate(panel._entries)
                if not e.is_matching and not panel._states[i].done]
    assert new_rows
    entry = panel._entries[new_rows[0]]
    panel.accept_new(entry, {"date": "2026-01-09", "payee": "Brand New",
                             "category": "", "memo": "", "amount_cents": -11_00})
    assert len(beeps) == 2

    remaining = [i for i, s in enumerate(panel._states) if not s.done]
    assert remaining
    panel.discard_index(remaining[0])
    assert len(beeps) == 2          # nothing entered the ledger, so no sound


def test_register_connects_the_review_panels_saved_signal(qapp, conn):
    """A guard against the wiring being dropped: the register must hear the
    panel's save signal, not just its own model's."""
    from mammon.ui.widgets import RegisterWidget

    acct = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    reg = RegisterWidget(conn, acct)
    played = []
    reg._play_accepted = lambda: played.append(1)
    reg.review_panel.transactionSaved.connect(reg._play_accepted)
    reg.review_panel.transactionSaved.emit()
    assert played == [1]


# ---- investment imports go through review -----------------------------------
_BROKER_QIF = ("!Type:Invst\n"
               "D01/07/2026\nNShrsOut\nYTARGET 2030 FUND(TDLB)\n"
               "I30.33\nQ0.125\nT3.78\nMFees\n^\n")


def _inv_account(conn, name="401k"):
    aid = ledger.create_account(conn, name, "investment", opening_balance=0)
    conn.execute(
        "INSERT INTO investment_transactions"
        "(account_id,date,action,symbol,quantity,price,amount) VALUES (?,?,?,?,?,?,?)",
        (aid, "2025-01-02", "Buy", "TARGET 2030 FUND", "100", "29.00", 290000))
    conn.commit()
    from mammon import investments
    investments.rebuild_holdings(conn, aid)
    return aid


def test_investment_import_waits_in_review_instead_of_landing_directly(qapp, conn, tmp_path):
    """Investment files used to import straight through, so an unrecognised
    security was never questioned. Nothing may reach the register until reviewed."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")
    before = conn.execute(
        "SELECT COUNT(*) FROM investment_transactions WHERE account_id=?", (aid,)
    ).fetchone()[0]

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        after = conn.execute(
            "SELECT COUNT(*) FROM investment_transactions WHERE account_id=?", (aid,)
        ).fetchone()[0]
        assert after == before, "the row reached the register without being reviewed"
        assert len(ir.load_pending(conn, aid)) == 1
        panel = win._registers[aid].review_panel
        # VISIBLE, not merely loaded. ImportReviewPanel hides itself on
        # construction, so a show_review that only set entries left the rows in a
        # panel nobody ever saw and the import looked like it did nothing.
        assert not panel.isHidden(), "the review panel never opened"
        assert win._registers[aid].toolbar.act_review.isEnabled()
        assert panel.is_investment
        headers = [panel.table.horizontalHeaderItem(i).text()
                   for i in range(panel.table.columnCount())]
        assert headers == ["Status", "Date", "Security", "Action",
                           "Shares", "Price", "Amount"]
        # Shares AND value are both present, which is what makes the price derivable.
        row = [panel.table.item(0, i).text() for i in range(len(headers))]
        assert row[2] == "TARGET 2030 FUND(TDLB)"
        assert row[3] == "ShrsOut" and row[4] == "0.125"
        assert row[5].startswith("30.33")
    finally:
        win.close()


def test_accepting_an_unedited_security_still_creates_a_new_one(qapp, conn, tmp_path):
    """The review does not GUESS -- leaving the name alone is a valid choice that
    means 'this really is a new security'. Only the user's edit redirects it."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        panel = win._registers[aid].review_panel
        entry = panel._entries[0]
        panel.accept_new(entry, {}) if not entry.is_matching else panel.accept_index(0)
    finally:
        win.close()

    assert sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM investment_transactions")) == [
            "TARGET 2030 FUND", "TARGET 2030 FUND(TDLB)"]


def test_investment_review_reopens_from_the_gear_action(qapp, conn, tmp_path):
    """Closing the panel and choosing Review... must bring it back, the same
    contract the cash register honours."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg = win._registers[aid]
        reg.review_panel.hide()
        assert reg.review_panel.isHidden()
        win._reopen_review(aid)
        assert not reg.review_panel.isHidden(), "Review... did not reopen the panel"
        assert reg.review_panel.table.rowCount() == 1
    finally:
        win.close()


def test_accept_button_commits_an_investment_row(qapp, conn, tmp_path):
    """Accept did nothing on an investment row: _on_accept delegated NEW rows to
    the register's editable pending row, which the investment register does not
    have, so the signal went nowhere. An investment row needs no pending row --
    every field the save takes came from the file, and the one the user corrects
    (Security) is editable in the panel itself."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        panel = win._registers[aid].review_panel
        before = conn.execute(
            "SELECT COUNT(*) FROM investment_transactions").fetchone()[0]
        assert ir.count_pending(conn, aid) == 1

        from PyQt5.QtCore import Qt
        from mammon.ui.models import InvestmentRegisterModel as M
        reg = win._registers[aid]
        assert reg.model.has_pending()
        # Corrections happen in the register's PENDING row, not the review list.
        reg.model.setData(reg.model.index(reg.model.pending_row(), M.SECURITY),
                          "TARGET 2030 FUND", Qt.EditRole)
        panel._on_accept()                                    # the Accept button

        after = conn.execute(
            "SELECT COUNT(*) FROM investment_transactions").fetchone()[0]
        assert after == before + 1, "Accept did not commit the row"
        assert ir.count_pending(conn, aid) == 0
        assert panel._states[0].done and panel._entries[0].state == "accepted"
    finally:
        win.close()

    # ...and it landed on the corrected security, with its price.
    assert sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM investment_transactions")) == ["TARGET 2030 FUND"]
    assert conn.execute(
        "SELECT 1 FROM price_history WHERE symbol='TARGET 2030 FUND' "
        "AND date='2026-01-07'").fetchone()


def test_accept_all_still_works_for_investment_rows(qapp, conn, tmp_path):
    """The bulk path goes through import_review.accept_all, not the panel's
    per-row Accept, so it needs its own guard."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        win._registers[aid].review_panel._on_accept_all()
    finally:
        win.close()
    assert ir.count_pending(conn, aid) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions").fetchone()[0] == 2


def test_every_dated_row_reaches_review_whatever_its_activity_text(qapp, conn, tmp_path):
    """A row the importer drops is one the user cannot see, and therefore cannot
    correct or delete -- strictly worse than a row mapped to the wrong action,
    which at least appears with an Action that can be edited. "Change in Market
    Value" almost certainly is not a transaction, but deciding that silently
    takes the choice away."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.csv"
    path.write_text(
        "\nPlan name:,EXAMPLE PLAN\n\n"
        "Date,Investment,Transaction Type,Amount,Shares/Unit\n"
        '01/07/2026,TARGET 2030 FUND,RECORDKEEPING FEE,"-3.78","-0.125"\n'
        '01/07/2026,TARGET 2030 FUND,Change in Market Value,"2.08","0.000"\n',
        encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        panel = win._registers[aid].review_panel
        assert panel.table.rowCount() == 2
        actions = {e.mapped.action for e in panel._entries}
        # The fee is MAPPED (a guess, correctable); the valuation arrives under
        # its own name for the user to discard.
        assert "ShrsOut" in actions
        assert "Change in Market Value" in actions
    finally:
        win.close()


def test_accept_on_an_already_accepted_row_does_nothing(qapp, conn, tmp_path):
    """An actioned row is history, not work. Accept did not check, so pressing it
    on a greyed row committed the same import row a second time -- a duplicate
    the register had no way to explain. discard_index has always guarded this."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        panel = win._registers[aid].review_panel
        panel.table.selectRow(0)
        panel._on_accept()
        once = conn.execute(
            "SELECT COUNT(*) FROM investment_transactions").fetchone()[0]

        panel.table.selectRow(0)                 # the same, now-accepted row
        panel._on_accept()
        panel._on_accept()
        assert conn.execute(
            "SELECT COUNT(*) FROM investment_transactions").fetchone()[0] == once
    finally:
        win.close()


def test_investment_transaction_can_be_deleted(qapp, conn):
    """There was no delete at all -- a wrongly-mapped or bogus imported row could
    be edited but never removed, and an import inevitably brings rows that should
    not exist."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon import investments
    from mammon.ui.widgets import InvestmentRegisterWidget

    aid = ledger.create_account(conn, "401k", "investment", opening_balance=0)
    investments.record_investment(conn, aid, "2026-01-05", "Buy",
                                  symbol="FUND A", quantity="10", price="10.00",
                                  amount=-10000)
    investments.rebuild_holdings(conn, aid)
    assert conn.execute("SELECT COUNT(*) FROM holdings WHERE account_id=?",
                        (aid,)).fetchone()[0] == 1

    reg = InvestmentRegisterWidget(conn, aid)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    reg._delete_row(0)
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions").fetchone()[0] == 0
    # Holdings follow, so the deleted shares do not linger in the valuation.
    assert conn.execute("SELECT COUNT(*) FROM holdings WHERE account_id=? "
                        "AND CAST(quantity AS REAL) != 0", (aid,)).fetchone()[0] == 0


def test_delete_is_confirmed_and_cancellable(qapp, conn):
    from PyQt5.QtWidgets import QMessageBox
    from mammon import investments
    from mammon.ui.widgets import InvestmentRegisterWidget

    aid = ledger.create_account(conn, "401k", "investment", opening_balance=0)
    investments.record_investment(conn, aid, "2026-01-05", "Buy",
                                  symbol="FUND A", quantity="10", price="10.00",
                                  amount=-10000)
    reg = InvestmentRegisterWidget(conn, aid)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
    reg._delete_row(0)
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions").fetchone()[0] == 1


# ---- the editable pending row in the investment register --------------------
def _open_pending(win, aid):
    panel = win._registers[aid].review_panel
    panel.table.selectRow(0)
    panel.row_selected.emit(panel.current_entry())
    return win._registers[aid], panel


def test_selecting_a_review_row_opens_an_editable_pending_register_row(qapp, conn, tmp_path):
    """Selecting a NEW review row -- which happens automatically when the panel
    opens, and on every later click -- puts it at the bottom of the register,
    seeded
    from what the importer read and editable in every field that matters. The
    importer's action and security are guesses from a lookup table; this is where
    they get corrected, in place, before anything is written."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.models import InvestmentRegisterModel as M
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg, panel = _open_pending(win, aid)

        assert reg.model.has_pending()
        # The pending row sits AFTER the posted history, one row longer.
        assert reg.model.rowCount() == len(reg.model._rows) + 1
        row = reg.model.pending_row()
        assert row == len(reg.model._rows)
        assert reg.model.is_pending_row(row)

        cols = (M.DATE, M.ACTION, M.SECURITY, M.QUANTITY, M.PRICE, M.INV_AMT)
        seeded = [reg.model.data(reg.model.index(row, c), Qt.DisplayRole) for c in cols]
        assert seeded[1] == "ShrsOut"
        assert seeded[2] == "TARGET 2030 FUND(TDLB)"
        assert seeded[3] == "0.125"
        # Every field the user might need to fix is editable.
        for c in cols:
            assert reg.model.flags(reg.model.index(row, c)) & Qt.ItemIsEditable, c
        # Posted history stays read-only.
        assert not (reg.model.flags(reg.model.index(0, M.SECURITY)) & Qt.ItemIsEditable)
        # Accept sits at the end of the pending row, where the editing is.
        assert reg.view.indexWidget(reg.model.index(row, M.CASH_BAL)) is not None
    finally:
        win.close()


def test_pending_row_edits_are_what_gets_committed(qapp, conn, tmp_path):
    """Correcting the security AND the action in place, then accepting, commits
    the corrected values -- not what the importer guessed."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.models import InvestmentRegisterModel as M
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg, panel = _open_pending(win, aid)
        row = reg.model.pending_row()
        reg.model.setData(reg.model.index(row, M.SECURITY),
                          "TARGET 2030 FUND", Qt.EditRole)
        reg.model.setData(reg.model.index(row, M.ACTION), "MiscExp", Qt.EditRole)
        reg.model.setData(reg.model.index(row, M.QUANTITY), "0.200", Qt.EditRole)
        reg._accept_pending()
    finally:
        win.close()

    got = conn.execute(
        "SELECT date, action, symbol, quantity FROM investment_transactions "
        "WHERE action='MiscExp'").fetchone()
    assert tuple(got) == ("2026-01-07", "MiscExp", "TARGET 2030 FUND", "0.200")
    # No phantom: the corrected name is the only security.
    assert sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM investment_transactions")) == ["TARGET 2030 FUND"]
    assert ir.count_pending(conn, aid) == 0


def test_pending_row_is_torn_down_when_the_selection_leaves_it(qapp, conn, tmp_path):
    """A cleared selection, a hidden panel or an already-actioned row must not
    leave a stale editable row hanging at the bottom of the register."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        reg = None
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg, panel = _open_pending(win, aid)
        assert reg.model.has_pending()

        panel.row_selected.emit(None)                  # selection cleared
        assert not reg.model.has_pending()

        panel.table.selectRow(0)
        panel.row_selected.emit(panel.current_entry())
        assert reg.model.has_pending()
        panel.hide()                                   # panel closed
        panel.row_selected.emit(panel.current_entry())
        assert not reg.model.has_pending()
    finally:
        win.close()




def test_correcting_the_security_in_the_pending_row_prices_the_real_fund(qapp, conn, tmp_path):
    """The point of the whole feature. The broker names the fund
    'TARGET 2030 FUND(TDLB)'; the ledger holds 'TARGET 2030 FUND'. Accepted
    uncorrected that makes a phantom security which collects the derived price,
    leaving the real holding with none. The correction happens in the register's
    pending row -- the same place a cash row is corrected."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.models import InvestmentRegisterModel as M
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg = win._registers[aid]
        assert reg.model.has_pending()
        row = reg.model.pending_row()
        assert reg.model.data(reg.model.index(row, M.SECURITY),
                              Qt.DisplayRole) == "TARGET 2030 FUND(TDLB)"
        reg.model.setData(reg.model.index(row, M.SECURITY),
                          "TARGET 2030 FUND", Qt.EditRole)
        reg._accept_pending()
    finally:
        win.close()

    assert sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM investment_transactions")) == ["TARGET 2030 FUND"]
    price = conn.execute(
        "SELECT close_price, source FROM price_history "
        "WHERE symbol='TARGET 2030 FUND' AND date='2026-01-07'").fetchone()
    assert price is not None, "no price history for the fund actually held"
    from decimal import Decimal
    assert Decimal(str(price["close_price"])) == Decimal("30.33")
    assert price["source"] == "txn"
    assert not conn.execute("SELECT 1 FROM holdings WHERE symbol LIKE '%(TDL%'").fetchone()
    held = conn.execute(
        "SELECT quantity FROM holdings WHERE account_id=?", (aid,)).fetchone()[0]
    assert Decimal(str(held)) == Decimal("99.875")


def test_investment_register_context_menu_offers_delete(qapp, conn):
    """A posted investment TRANSACTION must be deletable. An import inevitably
    brings rows that should not exist -- a plan's "Change in Market Value" line,
    a duplicate, a fee booked against the wrong security."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon import investments
    from mammon.ui.widgets import InvestmentRegisterWidget

    aid = ledger.create_account(conn, "401k", "investment", opening_balance=0)
    investments.record_investment(conn, aid, "2026-01-05", "Buy", symbol="FUND A",
                                  quantity="10", price="10.00", amount=-10000)
    investments.rebuild_holdings(conn, aid)
    reg = InvestmentRegisterWidget(conn, aid)

    labels = []

    class _FakeMenu:
        def __init__(self, *a, **k):
            self._acts = {}

        def addAction(self, text):
            labels.append(text)
            self._acts[text] = object()
            return self._acts[text]

        def addSeparator(self):
            pass

        def exec_(self, *a, **k):
            return None

    import mammon.ui.widgets as W
    real, W.QMenu = W.QMenu, _FakeMenu
    # Point the menu at a real row; over empty space Edit/Delete correctly hide.
    reg.view.indexAt = lambda pos: reg.model.index(0, 0)
    try:
        reg._on_view_context_menu(reg.view.rect().center())
    finally:
        W.QMenu = real
    assert "Delete" in labels, f"no Delete in the context menu: {labels}"

    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    reg._delete_row(0)
    assert conn.execute(
        "SELECT COUNT(*) FROM investment_transactions").fetchone()[0] == 0
    # Holdings follow, so deleted shares do not linger in the valuation.
    assert conn.execute("SELECT COUNT(*) FROM holdings WHERE account_id=? "
                        "AND CAST(quantity AS REAL) != 0", (aid,)).fetchone()[0] == 0


def test_reselecting_review_rows_does_not_crash_on_the_accept_button(qapp, conn, tmp_path):
    """set_pending()'s begin/endResetModel drops index widgets, so re-seeding the
    pending row -- which every review-row click does -- left _accept_btn a
    dangling C++ wrapper and deleteLater() raised RuntimeError."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.models import InvestmentRegisterModel as M
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(
        "!Type:Invst\n"
        "D01/07/2026\nNShrsOut\nYFUND A\nI30.33\nQ0.125\nT3.78\n^\n"
        "D01/08/2026\nNShrsOut\nYFUND B\nI12.72\nQ0.062\nT0.76\n^\n"
        "D01/09/2026\nNShrsOut\nYFUND C\nI10.00\nQ0.100\nT1.00\n^\n",
        encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg, panel = win._registers[aid], win._registers[aid].review_panel
        for _pass in range(2):
            for i in range(panel.table.rowCount()):
                panel.table.selectRow(i)          # raised RuntimeError here
        assert reg.model.has_pending()
        # The button survives the churn and belongs to the CURRENT pending row.
        assert reg.view.indexWidget(
            reg.model.index(reg.model.pending_row(), M.CASH_BAL)) is not None
    finally:
        win.close()


def test_context_menu_offers_edit_everywhere_and_delete_only_on_posted_rows(
        qapp, conn, tmp_path):
    """Edit applies to both kinds of row: the dialog exists BECAUSE an investment
    transaction cannot be fully expressed on a row (its fields depend on the
    action), so withholding it from the pending row while allowing inline editing
    there makes the register disagree with its own reason for having a dialog.
    Delete stays posted-only -- a pending row is discarded from the review list,
    not deleted from the register."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow
    import mammon.ui.widgets as W

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    labels = []

    class _FakeMenu:
        def __init__(self, *a, **k):
            pass

        def addAction(self, text):
            labels.append(text)
            return object()

        def addSeparator(self):
            pass

        def exec_(self, *a, **k):
            return None

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    real, W.QMenu = W.QMenu, _FakeMenu
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg = win._registers[aid]
        assert reg.model.has_pending()

        reg.view.indexAt = lambda pos: reg.model.index(reg.model.pending_row(), 0)
        labels.clear()
        reg._on_view_context_menu(reg.view.rect().center())
        # Edit IS offered: the dialog exists because a row cannot express
        # everything about an investment transaction, and that is as true of the
        # pending row as of a posted one.
        assert any(t.startswith("Edit") for t in labels), labels
        # Delete is not: a pending row is discarded from the review list.
        assert "Delete" not in labels, labels

        reg.view.indexAt = lambda pos: reg.model.index(0, 0)
        labels.clear()
        reg._on_view_context_menu(reg.view.rect().center())
        assert any(t.startswith("Edit") for t in labels), labels
        assert "Delete" in labels, labels
    finally:
        W.QMenu = real
        win.close()


def test_dialog_edits_write_back_into_the_pending_row(qapp, conn, tmp_path):
    """Editing the pending row through the full dialog fills in the SAME row --
    nothing is committed, Accept still does that. This is the richer way to fill
    in fields the row cannot express."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.models import InvestmentRegisterModel as M
    from mammon.ui.widgets import MainWindow
    import mammon.ui.widgets as W

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    class _FakeDialog:
        def __init__(self, *a, **k):
            pass

        def exec_(self):
            return 1                       # QDialog.Accepted

        def values(self):
            return {"date": "2026-02-02", "action": "MiscExp",
                    "symbol": "TARGET 2030 FUND", "quantity": "1.5",
                    "price": "9.99", "amount": -1499, "commission": None,
                    "memo": "m", "transfer_account_id": None}

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    real, W.InvestmentTransactionDialog = W.InvestmentTransactionDialog, _FakeDialog
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg = win._registers[aid]
        before = conn.execute(
            "SELECT COUNT(*) FROM investment_transactions").fetchone()[0]
        reg._edit_pending_row()

        row = reg.model.pending_row()
        shown = [reg.model.data(reg.model.index(row, c), Qt.DisplayRole)
                 for c in (M.DATE, M.ACTION, M.SECURITY, M.QUANTITY, M.PRICE)]
        assert shown == ["02/02/2026", "MiscExp", "TARGET 2030 FUND", "1.5", "9.99"]
        # The dialog did NOT commit -- Accept still owns that.
        assert conn.execute(
            "SELECT COUNT(*) FROM investment_transactions").fetchone()[0] == before

        reg._accept_pending()
        got = conn.execute(
            "SELECT date, action, symbol, quantity FROM investment_transactions "
            "WHERE action='MiscExp'").fetchone()
        assert tuple(got) == ("2026-02-02", "MiscExp", "TARGET 2030 FUND", "1.5")
    finally:
        W.InvestmentTransactionDialog = real
        win.close()


def test_pending_row_is_open_however_the_review_list_arrives(qapp, conn, tmp_path):
    """Every path that REPOPULATES the panel must open the pending row for the
    selected entry. Refilling the table re-selects the first row, but when that
    row was already selected Qt emits no itemSelectionChanged -- so nothing
    opened the pending row and it appeared only after clicking away to another
    item and back."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui import prefs
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        # 1. straight after an import
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg = win._registers[aid]
        assert reg.model.has_pending(), "no pending row after import"

        # 2. reopened from the gear's Review... action
        reg.review_panel.hide()
        reg._end_pending()
        win._reopen_review(aid)
        assert reg.model.has_pending(), "no pending row after Review... reopen"

        # 3. after flipping the show/hide-history toggle
        reg._reload_review(prefs.VIS_BATCH)
        assert reg.model.has_pending(), "no pending row after a visibility change"
    finally:
        win.close()

    # 4. a freshly opened register on an account that already has pending rows
    win2 = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        reg2 = win2.open_register(aid)
        reg2.show_review(ir.load_pending(conn, aid))
        assert reg2.model.has_pending(), "no pending row on a fresh register"
    finally:
        win2.close()


def test_hidden_panel_leaves_no_pending_row_behind(qapp, conn, tmp_path):
    """The converse: repopulating while hidden must not leave an editable row
    dangling at the bottom of a register with no visible review list."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg = win._registers[aid]
        assert reg.model.has_pending()
        reg.review_panel.hide()
        reg._open_pending_for_selection()
        assert not reg.model.has_pending()
        # An empty list closes the panel and clears the row too.
        reg.show_review([])
        assert not reg.model.has_pending()
        assert reg.review_panel.isHidden()
    finally:
        win.close()


def test_a_dividend_pair_can_be_collapsed_into_one_reinvdiv(qapp, conn, tmp_path):
    """A brokerage reports a reinvested dividend as TWO rows -- a DIVIDEND
    RECEIVED credit and an equal REINVESTMENT debit that buys the shares. Quicken
    users collapse the pair into a single ReinvDiv, and the review list has to
    allow that: change the action on the pending row, accept it, discard the
    other. This only works because ReinvDiv is reachable -- the editor's action
    list previously offered a generic 'Reinvest' and rewrote anything it did not
    know to Buy."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QMessageBox
    from mammon import investments
    from mammon.ui.models import InvestmentRegisterModel as M
    from mammon.ui.widgets import MainWindow

    aid = ledger.create_account(conn, "IRA", "investment", opening_balance=0)
    path = tmp_path / "ira.qif"
    path.write_text(
        "!Type:Invst" + chr(10) +
        "D07/10/2026" + chr(10) + "NBuy" + chr(10) + "YINFLAT-PROT BD INDEX" +
        chr(10) + "Q63.76" + chr(10) + "T2235.88" + chr(10) + "MREINVESTMENT" +
        chr(10) + "^" + chr(10) +
        "D07/10/2026" + chr(10) + "NDiv" + chr(10) + "YINFLAT-PROT BD INDEX" +
        chr(10) + "T2235.88" + chr(10) + "MDIVIDEND RECEIVED" + chr(10) + "^" +
        chr(10), encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg, panel = win._registers[aid], win._registers[aid].review_panel
        assert panel.table.rowCount() == 2

        row = reg.model.pending_row()
        reg.model.setData(reg.model.index(row, M.ACTION), "ReinvDiv", Qt.EditRole)
        reg._accept_pending()

        remaining = [i for i, s in enumerate(panel._states) if not s.done]
        assert remaining, "the second row of the pair should still be pending"
        panel.discard_index(remaining[0])
    finally:
        win.close()

    posted = conn.execute(
        "SELECT action, symbol, quantity, amount FROM investment_transactions"
    ).fetchall()
    assert len(posted) == 1, "the pair must collapse to ONE transaction"
    assert tuple(posted[0]) == ("ReinvDiv", "INFLAT-PROT BD INDEX", "63.76", 223588)
    investments.rebuild_holdings(conn, aid)
    held = conn.execute(
        "SELECT quantity FROM holdings WHERE account_id=?", (aid,)).fetchone()[0]
    from decimal import Decimal
    assert Decimal(str(held)) == Decimal("63.76")
    assert ir.count_pending(conn, aid) == 0


def test_edit_on_the_pending_row_actually_opens_the_dialog(qapp, conn, tmp_path):
    """Edit appeared in the pending row's context menu and did nothing: the menu
    calls _edit_row, which looks the row up with txn_at() -- and the pending row
    has no investment_transactions id, so it returned silently. The menu entry
    and the handler have to agree about which rows they serve."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.models import InvestmentRegisterModel as M
    from mammon.ui.widgets import MainWindow
    import mammon.ui.widgets as W

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    seeded = []

    class _FakeDialog:
        def __init__(self, *a, **k):
            seeded.append(k.get("txn"))

        def exec_(self):
            return 1

        def values(self):
            return {"date": "2026-02-02", "action": "IntInc", "symbol": "",
                    "quantity": "", "price": "", "amount": 1000,
                    "commission": None, "memo": "", "transfer_account_id": None}

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    real, W.InvestmentTransactionDialog = W.InvestmentTransactionDialog, _FakeDialog
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg = win._registers[aid]
        assert reg.model.has_pending()

        # Exactly what the context menu does.
        reg._edit_row(reg.model.pending_row())

        assert seeded, "Edit on the pending row opened no dialog"
        assert seeded[0]["action"] == "ShrsOut"          # seeded from the row
        assert seeded[0]["symbol"] == "TARGET 2030 FUND(TDLB)"

        row = reg.model.pending_row()
        assert reg.model.data(reg.model.index(row, M.ACTION),
                              Qt.DisplayRole) == "IntInc"
        # Still pending -- the dialog fills the row in, Accept commits it.
        assert reg.model.has_pending()
        assert conn.execute(
            "SELECT COUNT(*) FROM investment_transactions "
            "WHERE action='IntInc'").fetchone()[0] == 0
    finally:
        W.InvestmentTransactionDialog = real
        win.close()


def test_edit_still_opens_the_dialog_on_a_posted_row(qapp, conn):
    """The other half of the same contract, so routing the pending row did not
    break editing a real transaction."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon import investments
    from mammon.ui.widgets import InvestmentRegisterWidget
    import mammon.ui.widgets as W

    aid = ledger.create_account(conn, "IRA", "investment", opening_balance=0)
    investments.record_investment(conn, aid, "2026-01-05", "Buy", symbol="FUND A",
                                  quantity="10", price="10.00", amount=-10000)
    reg = InvestmentRegisterWidget(conn, aid)
    seeded = []

    class _FakeDialog:
        def __init__(self, *a, **k):
            seeded.append(k.get("txn"))

        def exec_(self):
            return 0                       # cancelled

        def values(self):
            return {}

    QMessageBox.information = staticmethod(lambda *a, **k: None)
    real, W.InvestmentTransactionDialog = W.InvestmentTransactionDialog, _FakeDialog
    try:
        reg._edit_row(0)
    finally:
        W.InvestmentTransactionDialog = real
    assert seeded, "Edit on a posted row opened no dialog"
    assert seeded[0]["symbol"] == "FUND A"


# ---- the learned ACTION tree in the review flow -----------------------------
def test_accepting_corrected_actions_teaches_the_next_import(qapp, conn, tmp_path):
    """The importer's action map is a lookup-table guess; every accept is a
    correction the tree replays. Correct 'Credit Interest' twice and the third
    import arrives with the pending row already seeded IntInc -- no code, no
    profile edit, exactly how payee renames learn."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.models import InvestmentRegisterModel as M
    from mammon.ui.widgets import MainWindow

    aid = ledger.create_account(conn, "IB", "investment", opening_balance=0)
    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        # Distinct dates and amounts, or the second month would classify as
        # MATCHING against the first month's just-accepted row (and a MATCHING
        # entry opens no pending row).
        for month, date, amt in (("Mar", "03/05/2026", "7.12"),
                                 ("Apr", "04/05/2026", "6.98")):
            path = tmp_path / f"ib_{month}.csv"
            path.write_text(
                "Transaction History,Header,Date,Description,Transaction Type,"
                "Symbol,Quantity,Price,Net Amount\n"
                f"Transaction History,Data,{date},USD Credit Interest for "
                f"{month}-2026,Credit Interest,-,-,-,{amt}\n",
                encoding="utf-8")
            win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
            reg = win._registers[aid]
            assert reg.model.has_pending()
            row = reg.model.pending_row()
            # The user corrects the action in the pending row and accepts.
            reg.model.setData(reg.model.index(row, M.ACTION), "IntInc", Qt.EditRole)
            reg._accept_pending()

        # Third month: the tree has two corroborating corrections (the action
        # domain's min_count), so the pending row arrives pre-seeded.
        path = tmp_path / "ib_May.csv"
        path.write_text(
            "Transaction History,Header,Date,Description,Transaction Type,"
            "Symbol,Quantity,Price,Net Amount\n"
            "Transaction History,Data,05/05/2026,USD Credit Interest for "
            "May-2026,Credit Interest,-,-,-,7.40\n",
            encoding="utf-8")
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg = win._registers[aid]
        row = reg.model.pending_row()
        assert reg.model.data(reg.model.index(row, M.ACTION),
                              Qt.DisplayRole) == "IntInc"
        # The review LIST still shows the raw ground truth, unrewritten.
        panel = win._registers[aid].review_panel
        pend = [i for i, s in enumerate(panel._states) if not s.done]
        assert panel._entries[pend[0]].mapped.action == "Credit Interest"
    finally:
        win.close()


def test_action_learning_survives_via_bootstrap_for_existing_history(qapp, conn):
    """An install with years of accepted investment rows starts warm: app launch
    bootstraps the action tree from that history (separate meta flag, so it
    runs once even where the payee tree bootstrapped long ago)."""
    from mammon import rename_tree

    aid = ledger.create_account(conn, "IB", "investment", opening_balance=0)
    for i in range(2):
        conn.execute(
            "INSERT INTO investment_transactions(account_id,date,action,amount,memo)"
            " VALUES (?,?,?,?,?)",
            (aid, "2026-01-0%d" % (i + 1), "MiscExp", -450,
             "x*****99:US Equity and Options Add-On Streaming Bundle"))
    conn.commit()
    rename_tree.ensure_bootstrapped(conn)
    s = rename_tree.suggest(
        conn, "x*****99:US Equity and Options Add-On Streaming Bundle for Sep",
        kind="action")
    assert (s.action, s.payee, s.high_confidence) \
        == (rename_tree.ACTION_AUTO, "MiscExp", True)


def test_security_column_shows_the_description_for_cash_rows(qapp, conn, tmp_path):
    """Security/Category is a shared column (as in the register): a fee or
    interest row names no security, and the cell used to show the source's
    literal '-' placeholder -- then, once placeholders were blanked, nothing at
    all. It shows the row's description, the only text such a row carries."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow

    aid = _inv_account(conn)
    path = tmp_path / "ib.csv"
    path.write_text(
        "Transaction History,Header,Date,Description,Transaction Type,"
        "Symbol,Quantity,Price,Net Amount\n"
        "Transaction History,Data,03/05/2026,USD Credit Interest for "
        "Mar-2026,Credit Interest,-,-,-,7.12\n"
        "Transaction History,Data,03/12/2026,ALTY(US37954Y8066) Cash Dividend "
        "USD 0.079 per Share,Dividend,ALTY,-,-,102.70\n",
        encoding="utf-8")
    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        panel = win._registers[aid].review_panel
        cells = {panel.table.item(i, 2).text() for i in range(panel.table.rowCount())}
        assert "USD Credit Interest for Mar-2026" in cells   # description, not blank
        assert "ALTY" in cells                               # a real security stays
        assert "-" not in cells and "" not in cells
    finally:
        win.close()


def test_matching_review_row_highlights_the_register_line(qapp, conn, tmp_path):
    """Selecting a MATCHING row must show WHICH register line it matched -- the
    one thing that makes accepting a match a judgement rather than a guess. Only
    the NEW branch existed for investments, so selecting a MATCHING row silently
    did nothing."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon import investments
    from mammon.ui.widgets import MainWindow

    aid = ledger.create_account(conn, "IB", "investment", opening_balance=0)
    # An existing dividend, and a file that re-reports it plus a new one.
    existing = investments.record_investment(
        conn, aid, "2026-01-07", "Div", symbol="ALTY", amount=9750)
    path = tmp_path / "ib.csv"
    path.write_text(
        "Date,Symbol,Transaction Type,Description,Quantity,Price,Net Amount\n"
        "01/07/2026,ALTY,Dividend,ALTY Cash Dividend,-,-,97.50\n"
        "02/11/2026,ALTY,Dividend,ALTY Cash Dividend,-,-,100.10\n",
        encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg, panel = win._registers[aid], win._registers[aid].review_panel

        matching = [i for i, e in enumerate(panel._entries) if e.is_matching]
        assert matching, "the re-reported dividend should classify MATCHING"
        panel.table.selectRow(matching[0])

        row = reg.view.currentIndex().row()
        assert row >= 0, "no register row was selected"
        assert reg.model.txn_at(row)["id"] == existing
        # A match is not an entry: no pending row is opened for it.
        assert not reg.model.has_pending()

        # ...and a NEW row still opens its editable pending row.
        new_rows = [i for i, e in enumerate(panel._entries) if e.is_new]
        assert new_rows
        panel.table.selectRow(new_rows[0])
        assert reg.model.has_pending()
    finally:
        win.close()


def test_actioned_review_row_points_at_what_it_produced(qapp, conn, tmp_path):
    """An accepted row is history: selecting it highlights the transaction it
    created rather than offering to enter it a second time."""
    from PyQt5.QtWidgets import QMessageBox
    from mammon.ui.widgets import MainWindow
    from mammon.ui import prefs

    aid = _inv_account(conn)
    path = tmp_path / "history.qif"
    path.write_text(_BROKER_QIF, encoding="utf-8")

    win = MainWindow(conn)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        prefs.set_review_visibility(aid, prefs.VIS_BATCH)   # keep history visible
        win._ingest_files_as_batch(aid, ledger.get_account(conn, aid), [str(path)])
        reg, panel = win._registers[aid], win._registers[aid].review_panel
        reg._accept_pending()
        posted = conn.execute(
            "SELECT id FROM investment_transactions WHERE action='ShrsOut'"
        ).fetchone()[0]

        actioned = [i for i, s in enumerate(panel._states) if s.done]
        assert actioned, "the accepted row should remain as greyed history"
        # Re-selecting an ALREADY-selected row emits no itemSelectionChanged, so
        # drive the handler the way a click on a different row would.
        panel.table.selectRow(actioned[0])
        panel.row_selected.emit(panel._entries[actioned[0]])
        row = reg.view.currentIndex().row()
        assert row >= 0 and reg.model.txn_at(row)["id"] == posted
        assert not reg.model.has_pending()
    finally:
        win.close()

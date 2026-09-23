"""The match-merge policy is non-destructive, and the review queue says so.

DECISION (locked): accepting a MATCHING review row merges INTO the existing
register line and never clobbers a field the user entered by hand. The merge only
reconciles metadata -- it marks the line cleared and stamps the source's
transaction id when the line had none -- so a download can never overwrite a
manually-entered date with the bank's posting date (the exact failure users
report of other tools).

``mammon.import_review.accept_match`` is the sole writer of this path; these tests
assert (1) the behavior at that seam and (2) that ``ImportReviewPanel`` surfaces
the policy per matched row so it is inspectable rather than implicit. All data
here is synthetic.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, import_review, ledger
from mammon.import_review import build_review
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "Anytown Checking", "checking")


@pytest.fixture
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    """Keep the panel's per-account visibility read/write off the real user
    settings so the test starts from defaults and touches nothing global."""
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


# ---------------------------------------------------------------------------
# behavior: a match never overwrites a user-entered field
# ---------------------------------------------------------------------------
def test_matching_accept_does_not_overwrite_user_edited_fields(conn, account):
    """A hand-entered register line is matched by an incoming download whose
    date, payee and memo all DIFFER. Accepting the match must leave every field
    the user typed untouched -- especially the date (never the bank posting
    date) -- and only reconcile metadata (cleared + a fitid where there was
    none)."""
    user_date = "2026-08-20"
    user_payee = "Corner Grocer (hand-typed)"
    user_memo = "week's groceries, split with roommate"
    user_num = "1071"
    amount = -2500
    existing = ledger.add_transaction(
        conn, account, user_date, amount,
        payee=user_payee, memo=user_memo, num=user_num)

    # Incoming bank row: same SIGNED amount, a posting date two days later, and
    # gobbledygook statement text -- i.e. a genuine MATCH whose every visible
    # field disagrees with what the user entered.
    [entry] = build_review(conn, account, [{
        "transactionId": "BANK-777",
        "postedDate": "2026-08-22",
        "amount": "25.00", "isDebit": True,
        "statementDescription": "POS DEBIT GROCERY OUTLET #42 ANYTOWN",
    }])
    assert entry.is_matching
    assert entry.matched_txn_id == existing
    assert entry.match_method == "date+amount"

    import_review.accept_match(conn, entry)

    after = conn.execute(
        "SELECT date, amount, payee, memo, num, fitid, cleared "
        "FROM transactions WHERE id=?", (existing,)).fetchone()
    # Nothing the user entered by hand is rewritten -- the download does NOT win.
    assert after["date"] == user_date          # NOT the bank's 2026-08-22
    assert after["amount"] == amount
    assert after["payee"] == user_payee        # NOT the raw statement text
    assert after["memo"] == user_memo
    assert after["num"] == user_num
    # The merge only reconciles metadata.
    assert after["cleared"] == 1
    assert after["fitid"] == "BANK-777"        # stamped only because it had none


# ---------------------------------------------------------------------------
# visibility: the panel states the policy on each matched row
# ---------------------------------------------------------------------------
def test_matching_row_status_tooltip_states_merge_policy(qapp, conn, account):
    """A MATCHING row's Status cell carries a tooltip stating what merges vs. is
    preserved; a NEW row carries none (nothing merges into an existing line)."""
    from mammon.ui.import_review_widget import ImportReviewPanel, STATUS

    ledger.add_transaction(conn, account, "2026-08-20", -2500, payee="Grocer")
    entries = build_review(conn, account, [
        {   # matches the hand-entered line above
            "transactionId": "BANK-9", "postedDate": "2026-08-21",
            "amount": "25.00", "isDebit": True,
            "statementDescription": "POS DEBIT GROCER"},
        {   # different amount -> NEW, no existing line to merge into
            "transactionId": "BANK-10", "postedDate": "2026-08-21",
            "amount": "88.00", "isDebit": True,
            "statementDescription": "HARDWARE"},
    ])
    matched, new = entries
    assert matched.is_matching and new.is_new

    panel = ImportReviewPanel(conn, account)
    panel.set_entries(entries)

    match_item = panel.table.item(0, STATUS)
    assert match_item.text() == "MATCHING"
    tip = match_item.toolTip().lower()
    assert tip, "matched row must carry a merge-policy tooltip"
    # It states what is preserved and that the download does not overwrite it.
    assert "preserved" in tip
    assert "does not overwrite" in tip
    assert "never overwrites" in tip

    # A NEW row gets no such tooltip -- there is no existing line to merge into.
    assert panel.table.item(1, STATUS).text() == "NEW"
    assert panel.table.item(1, STATUS).toolTip() == ""

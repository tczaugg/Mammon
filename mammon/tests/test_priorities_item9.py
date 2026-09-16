"""Deterministic same-date register ordering (verify-first regression).

The concrete guarantee: transactions that share a date keep a stable total
order, so the account register never reshuffles between opens or after an
unrelated edit. Since 2026-09-15 that order is by AMOUNT high to low (cash in
before the payments it funds, SRD 5.1b), with the insertion ``id`` breaking ties
between equal amounts. If someone later drops the ``id`` tiebreak (e.g.
``ORDER BY date, amount DESC`` alone) or makes an edit that re-assigns a row's
id, one of these fails.

The mechanism being pinned:

* ``ledger.register_rows`` queries ``ORDER BY date, amount DESC, id`` --
  same-date, same-amount rows fall into insertion order, not SQLite's
  unspecified default row order;
* ``id`` is a monotonic ``INTEGER PRIMARY KEY`` written only by ``ledger``, so a
  later insert always sorts after an earlier equal one;
* ``update_transaction`` edits in place, preserving ``id`` -- so a field edit
  that leaves the amount alone (or a date round-trip) never moves a row within
  its date, and an amount edit moves it only to where its new amount belongs;
* ``RegisterModel`` preserves ``(date, id)`` as the tiebreak under EVERY column
  sort, and Date-descending is the exact reverse of Date-ascending.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from mammon import db, ledger
from mammon.ui.models import RegisterModel

R = RegisterModel


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "item9.db"


@pytest.fixture
def conn(db_path):
    c = db.init_db(db_path)
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "Checking", "checking", opening_balance=0)


def _ids(rows):
    return [r["id"] for r in rows]


def _model_ids(m):
    # rowCount() includes the trailing blank quick-entry row; skip it.
    return [m.txn_at(i)["id"] for i in range(m.rowCount() - 1)]


# ---------------------------------------------------------------------------
# ledger.register_rows -- the query that anchors the whole guarantee
# ---------------------------------------------------------------------------
def test_same_date_rows_are_in_insertion_order(conn, account):
    """Three equal transactions on one date come back in the order they were
    added, which is ascending id -- not payee order, not reverse, not arbitrary."""
    a = ledger.add_transaction(conn, account, "2026-04-01", -10_00, payee="Zeta")
    b = ledger.add_transaction(conn, account, "2026-04-01", -10_00, payee="Alpha")
    c = ledger.add_transaction(conn, account, "2026-04-01", -10_00, payee="Mu")
    assert a < b < c  # ids are monotonic: the load-bearing fact
    assert _ids(ledger.register_rows(conn, account)) == [a, b, c]


def test_date_is_primary_id_only_breaks_ties(conn, account):
    """A row added LAST but dated EARLIER still sorts first: date wins over id;
    id only orders rows that share a date."""
    late = ledger.add_transaction(conn, account, "2026-04-10", -1_00, payee="Late")
    early = ledger.add_transaction(conn, account, "2026-04-01", -1_00, payee="Early")
    same = ledger.add_transaction(conn, account, "2026-04-10", -1_00, payee="Late2")
    # early (older date) first, then the two 2026-04-10 rows in insertion order.
    assert _ids(ledger.register_rows(conn, account)) == [early, late, same]


def test_order_is_stable_across_repeated_queries(conn, account):
    for i in range(5):
        ledger.add_transaction(conn, account, "2026-06-15", -(i + 1) * 100, payee=f"P{i}")
    first = _ids(ledger.register_rows(conn, account))
    for _ in range(4):
        assert _ids(ledger.register_rows(conn, account)) == first


def test_order_is_stable_across_reopen(conn, account, db_path):
    """Reopening the database file -- a fresh connection, the real 'between
    opens' scenario -- yields the identical same-date ordering."""
    for i in range(5):
        ledger.add_transaction(conn, account, "2026-06-15", -(i + 1) * 100, payee=f"P{i}")
    before = _ids(ledger.register_rows(conn, account))
    c2 = db.init_db(db_path)  # init_db is idempotent; opens the same file
    try:
        assert _ids(ledger.register_rows(c2, account)) == before
    finally:
        c2.close()


def test_new_same_date_row_appends_others_unmoved(conn, account):
    """Adding another equal same-date transaction later puts it AFTER the
    existing ones (larger id) and does not disturb their relative order."""
    a = ledger.add_transaction(conn, account, "2026-07-01", -5_00, payee="First")
    b = ledger.add_transaction(conn, account, "2026-07-01", -5_00, payee="Second")
    assert _ids(ledger.register_rows(conn, account)) == [a, b]
    c = ledger.add_transaction(conn, account, "2026-07-01", -5_00, payee="Third")
    assert _ids(ledger.register_rows(conn, account)) == [a, b, c]


# ---------------------------------------------------------------------------
# Edits never move a row within its date ("changes by itself" complaint)
# ---------------------------------------------------------------------------
def test_field_edit_preserves_id_and_position(conn, account):
    a = ledger.add_transaction(conn, account, "2026-08-01", -1_00, payee="A")
    b = ledger.add_transaction(conn, account, "2026-08-01", -2_00, payee="B")
    c = ledger.add_transaction(conn, account, "2026-08-01", -3_00, payee="C")
    ledger.update_transaction(conn, b, payee="B-renamed", memo="edited")
    rows = ledger.register_rows(conn, account)
    assert _ids(rows) == [a, b, c]            # position unchanged
    assert rows[1]["id"] == b                 # id unchanged -> in-place edit
    assert rows[1]["payee"] == "B-renamed"    # the edit did land


def test_an_amount_edit_moves_the_row_only_to_where_its_amount_belongs(conn, account):
    """Same-day order is by amount (SRD 5.1b), so a larger payment now shows last;
    the other rows keep their order."""
    a = ledger.add_transaction(conn, account, "2026-08-01", -1_00, payee="A")
    b = ledger.add_transaction(conn, account, "2026-08-01", -2_00, payee="B")
    c = ledger.add_transaction(conn, account, "2026-08-01", -3_00, payee="C")
    ledger.update_transaction(conn, b, amount=-999_00)
    assert _ids(ledger.register_rows(conn, account)) == [a, c, b]


def test_date_roundtrip_returns_to_same_position(conn, account):
    """Move a row to another date and back; because its id is preserved it
    returns to exactly where it was among its same-date peers."""
    a = ledger.add_transaction(conn, account, "2026-09-01", -1_00, payee="A")
    b = ledger.add_transaction(conn, account, "2026-09-01", -2_00, payee="B")
    c = ledger.add_transaction(conn, account, "2026-09-01", -3_00, payee="C")
    ledger.update_transaction(conn, b, date="2026-12-25")
    assert _ids(ledger.register_rows(conn, account)) == [a, c, b]  # b moved to its later date
    ledger.update_transaction(conn, b, date="2026-09-01")
    assert _ids(ledger.register_rows(conn, account)) == [a, b, c]  # and back into place


# ---------------------------------------------------------------------------
# RegisterModel -- the view preserves the order under every sort
# ---------------------------------------------------------------------------
def test_model_default_is_insertion_order_for_same_date(qapp, conn, account):
    a = ledger.add_transaction(conn, account, "2026-05-05", -1_00, payee="A")
    b = ledger.add_transaction(conn, account, "2026-05-05", -1_00, payee="B")
    c = ledger.add_transaction(conn, account, "2026-05-05", -1_00, payee="C")
    m = RegisterModel(conn, account)
    assert m.sort_state() == (R.DATE, Qt.AscendingOrder)
    assert _model_ids(m) == [a, b, c]


def test_model_date_descending_is_exact_reverse(qapp, conn, account):
    for i in range(6):
        ledger.add_transaction(conn, account, "2026-05-05", -(i + 1) * 100, payee=f"P{i}")
    m = RegisterModel(conn, account)
    asc = _model_ids(m)
    m.set_sort(R.DATE, Qt.DescendingOrder)
    assert _model_ids(m) == list(reversed(asc))


def test_model_nondate_sort_tiebreaks_by_insertion(qapp, conn, account):
    """When rows share a date AND an identical value in the sorted column, the
    only thing left to order by is insertion id. Ascending gives id order;
    descending gives its exact reverse -- both fully determined, never random."""
    ids = [
        ledger.add_transaction(conn, account, "2026-10-10", -(i + 1) * 100,
                               payee="Tie Co")  # identical payee for all
        for i in range(5)
    ]
    m = RegisterModel(conn, account)
    m.set_sort(R.PAYEE, Qt.AscendingOrder)
    assert _model_ids(m) == ids
    m.set_sort(R.PAYEE, Qt.DescendingOrder)
    assert _model_ids(m) == list(reversed(ids))


def test_model_order_stable_across_reload_and_sort_toggles(qapp, conn, account):
    ids = [
        ledger.add_transaction(conn, account, "2026-11-11", -(i + 1) * 100, payee="Same")
        for i in range(5)
    ]
    m = RegisterModel(conn, account)
    baseline = _model_ids(m)
    assert baseline == ids
    # Toggle through several sorts and return to Date-ascending; the same-date
    # order must be exactly what it was.
    m.set_sort(R.PAYEE, Qt.AscendingOrder)
    m.set_sort(R.CLR, Qt.DescendingOrder)
    m.set_sort(R.DATE, Qt.AscendingOrder)
    assert _model_ids(m) == baseline
    m.reload()
    assert _model_ids(m) == baseline

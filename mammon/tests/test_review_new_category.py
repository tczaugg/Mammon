"""Accepting a review row whose Category is a BRAND-NEW name must create the
category and assign it to the posted transaction.

Reported: the user types a category that does not exist yet into the register's
editable pending row, confirms the "create new category?" prompt, and Accepts
the review row -- and the transaction lands with NO category, forcing a manual
re-add. The confirm dialog only wrote raw text into the pending buffer; the
accept path then resolved it with the LOOKUP-ONLY ``category_id_for_name``
(docstring: "we never invent categories here"), which returns ``None`` for
unknown text. The live register instead get-or-creates through
``ledger.resolve_category`` -- the single writer of category rows -- so accept
now routes typed category text through the same writer via
``import_review.resolve_or_create_category``.

Load-bearing invariants covered here:
  * a NEW typed category is CREATED and ASSIGNED (the reported bug);
  * an existing category is REUSED case-insensitively, never forked;
  * a '[Account]' transfer target still takes precedence and creates NO category;
  * a blank category invents nothing.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, import_review as ir, ledger


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "m.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "Anytown CU Checking", "checking")


def _panel_with_new_row(conn, account_id, *, amount="42.00", desc="HOBBY SHOP"):
    """Build a single NEW review row and hand back the panel + its live entry,
    exactly as the register does after a download the user is reviewing."""
    from mammon.ui.import_review_widget import ImportReviewPanel

    entries = ir.build_review(conn, account_id, [{
        "transactionId": "N-1", "postedDate": "2026-03-04",
        "amount": amount, "isDebit": True, "statementDescription": desc}])
    ir.persist_entries(conn, account_id, entries)
    panel = ImportReviewPanel(conn, account_id)
    panel.set_entries(ir.load_pending(conn, account_id))
    entry = panel._entries[0]
    assert entry.is_new
    return panel, entry


def _category_id(conn, txn_id):
    return conn.execute(
        "SELECT category_id FROM transactions WHERE id=?", (txn_id,)).fetchone()[0]


def _categories_named(conn, name):
    """Rows in the categories table whose leaf name matches ``name`` (any case)."""
    return conn.execute(
        "SELECT COUNT(*) FROM categories WHERE name=? COLLATE NOCASE",
        (name,)).fetchone()[0]


def test_accepting_a_new_typed_category_creates_and_assigns_it(qapp, conn, account):
    """The reported bug: a brand-new category typed into the pending row is
    created AND set on the transaction -- not silently dropped."""
    panel, entry = _panel_with_new_row(conn, account)
    # Precondition: the category does not exist yet.
    assert ir.category_id_for_name(conn, "Model Trains") is None

    txn_id = panel.accept_new(entry, {
        "date": "2026-03-04", "payee": "Hobby Shop",
        "category": "Model Trains", "memo": "", "amount_cents": -4200})

    # (a) the category row now exists and is queryable by name
    cid = ir.category_id_for_name(conn, "Model Trains")
    assert cid is not None, "accept must CREATE the typed category"
    # (b) the posted transaction carries it
    assert _category_id(conn, txn_id) == cid, "accept must ASSIGN the new category"
    panel.deleteLater()


def test_accepting_a_new_nested_category_path_creates_each_level(qapp, conn, account):
    """A 'Parent:Child' path the user types is created level by level, the same
    as ledger.resolve_category does for the live register."""
    panel, entry = _panel_with_new_row(conn, account)
    assert ir.category_id_for_name(conn, "Hobbies:Model Trains") is None

    txn_id = panel.accept_new(entry, {
        "date": "2026-03-04", "payee": "Hobby Shop",
        "category": "Hobbies:Model Trains", "memo": "", "amount_cents": -4200})

    cid = ir.category_id_for_name(conn, "Hobbies:Model Trains")
    assert cid is not None
    assert _category_id(conn, txn_id) == cid
    # the parent level was created too
    assert ir.category_id_for_name(conn, "Hobbies") is not None
    panel.deleteLater()


def test_existing_category_is_reused_not_forked(qapp, conn, account):
    """Typing an existing category (in a different case) reuses it -- accept
    must not mint a near-duplicate."""
    existing = ledger.resolve_category(conn, "Groceries")
    panel, entry = _panel_with_new_row(conn, account)

    txn_id = panel.accept_new(entry, {
        "date": "2026-03-04", "payee": "Corner Market",
        "category": "groceries", "memo": "", "amount_cents": -4200})

    assert _category_id(conn, txn_id) == existing
    assert _categories_named(conn, "Groceries") == 1, "no duplicate category row"
    panel.deleteLater()


def test_transfer_target_text_creates_no_category(qapp, conn, account):
    """A '[Account]' transfer target still wins over category creation: the row
    posts as a transfer and NO category row is invented for the bracket text."""
    ledger.create_account(conn, "Anytown CU Savings", "savings")
    before = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    panel, entry = _panel_with_new_row(conn, account)

    txn_id = panel.accept_new(entry, {
        "date": "2026-03-04", "payee": "Move to savings",
        "category": "[Anytown CU Savings]", "memo": "", "amount_cents": -4200})

    # posted as a transfer (virtual category), so category_id stays NULL
    assert _category_id(conn, txn_id) is None
    row = conn.execute(
        "SELECT transfer_account_id FROM transactions WHERE id=?", (txn_id,)
    ).fetchone()
    assert row[0] is not None, "the bracket text must post a transfer"
    # and nothing category-shaped was created from the bracket text
    assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == before
    assert _categories_named(conn, "Anytown CU Savings") == 0
    panel.deleteLater()


def test_blank_category_invents_nothing(qapp, conn, account):
    """An accepted row with no category text creates no category and posts with
    category_id NULL."""
    before = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    panel, entry = _panel_with_new_row(conn, account)

    txn_id = panel.accept_new(entry, {
        "date": "2026-03-04", "payee": "Cash", "category": "",
        "memo": "", "amount_cents": -4200})

    assert _category_id(conn, txn_id) is None
    assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == before
    panel.deleteLater()


def test_resolve_or_create_category_is_a_get_or_create_helper(conn, account):
    """The seam the accept path uses: blank -> None, new -> created, existing
    -> reused. (No Qt needed; asserts the helper directly.)"""
    assert ir.resolve_or_create_category(conn, "") is None
    assert ir.resolve_or_create_category(conn, "   ") is None
    # Bracketed text names the transfer namespace; an unknown '[Account]' must
    # stay uncategorized, never mint a literal '[Name]' category row.
    assert ir.resolve_or_create_category(conn, "[Nonexistent Account]") is None
    assert _categories_named(conn, "[Nonexistent Account]") == 0

    created = ir.resolve_or_create_category(conn, "Dining Out")
    assert created is not None
    assert ir.category_id_for_name(conn, "Dining Out") == created
    # idempotent / case-insensitive reuse
    assert ir.resolve_or_create_category(conn, "dining out") == created
    assert _categories_named(conn, "Dining Out") == 1

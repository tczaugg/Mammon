"""Tests for the category-model features layered on the ledger:

* the management CRUD in :mod:`mammon.ledger` (create / count-usage / delete,
  with the intelligent reassign-before-delete path and the silent delete of an
  unused category), and
* the classic income-vs-expense classification in
  :mod:`mammon.category_types` over a mixed set of sample categories.
"""
from __future__ import annotations

import pytest

from mammon import category_types, db, ledger


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def checking(conn):
    return ledger.create_account(conn, "Checking", "checking", opening_balance=0)


# ---------------------------------------------------------------------------
# management CRUD: create + count usage
# ---------------------------------------------------------------------------
def test_create_category_is_get_or_create(conn):
    a = ledger.create_category(conn, "Dining")
    b = ledger.create_category(conn, "Dining")
    assert a is not None and a == b                       # no duplicate fork
    assert ledger.create_category(conn, "   ") is None    # blank -> nothing
    paths = {c["path"] for c in ledger.list_categories(conn)}
    assert "Dining" in paths


def test_resolve_category_is_case_insensitive(conn):
    # A capitalization-only difference reuses the existing category instead of
    # forking a near-duplicate (issue 6 / feature parity), at every level.
    parent = ledger.resolve_category(conn, "Business")
    child = ledger.resolve_category(conn, "Business:Writing")
    assert ledger.resolve_category(conn, "business") == parent
    assert ledger.resolve_category(conn, "BUSINESS:writing") == child
    assert ledger.resolve_category(conn, "business:WRITING") == child
    # no duplicates were created by the case variants
    paths = [c["path"] for c in ledger.list_categories(conn)]
    assert paths.count("Business") == 1
    assert paths.count("Business:Writing") == 1


def test_count_category_usage_counts_txns_and_splits(conn, checking):
    dining = ledger.create_category(conn, "Dining")
    shopping = ledger.create_category(conn, "Shopping")
    assert ledger.count_category_usage(conn, dining) == 0

    ledger.add_transaction(conn, checking, "2026-01-05", -30_00, category_id=dining)
    split = ledger.add_transaction(conn, checking, "2026-01-06", -50_00)
    ledger.set_splits(conn, split, [(dining, -30_00, None), (shopping, -20_00, None)])

    assert ledger.count_category_usage(conn, dining) == 2      # 1 txn + 1 split line
    assert ledger.count_category_usage(conn, shopping) == 1    # 1 split line


# ---------------------------------------------------------------------------
# delete-with-reassign path
# ---------------------------------------------------------------------------
def test_delete_with_reassign_moves_txns_and_splits(conn, checking):
    dining = ledger.create_category(conn, "Dining")
    shopping = ledger.create_category(conn, "Shopping")

    t1 = ledger.add_transaction(conn, checking, "2026-01-05", -30_00, category_id=dining)
    split = ledger.add_transaction(conn, checking, "2026-01-06", -50_00)
    ledger.set_splits(conn, split, [(dining, -30_00, None), (shopping, -20_00, None)])

    moved = ledger.delete_category(conn, dining, replacement_id=shopping)

    assert moved == 2                                          # txn + split reassigned
    # the doomed category is gone and nothing still points at it
    assert conn.execute(
        "SELECT COUNT(*) FROM categories WHERE id=?", (dining,)).fetchone()[0] == 0
    assert ledger.count_category_usage(conn, dining) == 0
    # everything landed on the replacement (its own line + the two reassigned)
    assert ledger.count_category_usage(conn, shopping) == 3
    assert conn.execute(
        "SELECT category_id FROM transactions WHERE id=?", (t1,)).fetchone()[0] == shopping


def test_delete_with_reassign_into_a_new_category(conn, checking):
    """The replacement may be a brand-new category (UI's 'type a new name')."""
    old = ledger.create_category(conn, "Misc")
    ledger.add_transaction(conn, checking, "2026-01-05", -10_00, category_id=old)

    new = ledger.create_category(conn, "Household:Supplies")   # created on the fly
    moved = ledger.delete_category(conn, old, replacement_id=new)

    assert moved == 1
    assert ledger.count_category_usage(conn, new) == 1
    assert ledger.count_category_usage(conn, old) == 0


# ---------------------------------------------------------------------------
# delete-empty path
# ---------------------------------------------------------------------------
def test_delete_unused_category_is_silent(conn, checking):
    unused = ledger.create_category(conn, "Never Used")
    keep = ledger.create_category(conn, "Groceries")
    kept_txn = ledger.add_transaction(
        conn, checking, "2026-01-05", -15_00, category_id=keep)

    assert ledger.count_category_usage(conn, unused) == 0
    moved = ledger.delete_category(conn, unused)               # no replacement needed

    assert moved == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM categories WHERE id=?", (unused,)).fetchone()[0] == 0
    # untouched category and its transaction survive intact
    assert conn.execute(
        "SELECT category_id FROM transactions WHERE id=?", (kept_txn,)
    ).fetchone()[0] == keep


# ---------------------------------------------------------------------------
# income vs expense classification of a mixed sample
# ---------------------------------------------------------------------------
@pytest.fixture
def mixed(conn, checking):
    """A deliberately mixed set: pure income, pure expense, and two categories
    whose sign is decided only by the NET of positive and negative activity."""
    cats = {
        "salary": ledger.create_category(conn, "Salary"),
        "interest": ledger.create_category(conn, "Interest"),
        "groceries": ledger.create_category(conn, "Groceries"),
        "rebates": ledger.create_category(conn, "Rebates"),      # net positive
        "fees": ledger.create_category(conn, "Fees"),            # net negative
        "empty": ledger.create_category(conn, "Empty"),          # no activity
    }
    add = lambda amt, cid: ledger.add_transaction(
        conn, checking, "2026-01-05", amt, category_id=cid)
    add(200_00, cats["salary"]); add(200_00, cats["salary"])     # +400 -> income
    add(5_00, cats["interest"])                                  # +5   -> income
    add(-30_00, cats["groceries"]); add(-20_00, cats["groceries"])  # -50 -> expense
    add(-10_00, cats["rebates"]); add(80_00, cats["rebates"])    # +70  -> income
    add(5_00, cats["fees"]); add(-30_00, cats["fees"])           # -25  -> expense
    return cats


def test_classification_of_mixed_categories(conn, mixed):
    cls = category_types.classify_categories(conn)
    assert cls[mixed["salary"]] == category_types.INCOME
    assert cls[mixed["interest"]] == category_types.INCOME
    assert cls[mixed["groceries"]] == category_types.EXPENSE
    assert cls[mixed["rebates"]] == category_types.INCOME      # decided by net sign
    assert cls[mixed["fees"]] == category_types.EXPENSE        # decided by net sign
    assert cls[mixed["empty"]] == category_types.EXPENSE       # no activity -> expense


def test_classification_counts_split_lines_not_parent(conn, checking):
    salary = ledger.create_category(conn, "Salary")
    groceries = ledger.create_category(conn, "Groceries")
    # a split paycheck: +300 to Salary, -50 to Groceries on the same txn
    split = ledger.add_transaction(conn, checking, "2026-01-10", 250_00)
    ledger.set_splits(conn, split,
                      [(salary, 300_00, None), (groceries, -50_00, None)])
    cls = category_types.classify_categories(conn)
    assert cls[salary] == category_types.INCOME
    assert cls[groceries] == category_types.EXPENSE


def test_transfers_excluded_from_classification(conn):
    """A transfer carries no category, so it must not skew any classification."""
    a = ledger.create_account(conn, "Checking", "checking", opening_balance=0)
    b = ledger.create_account(conn, "Savings", "savings", opening_balance=0)
    ledger.create_transfer(conn, a, b, "2026-01-05", 500_00)
    nets = category_types.net_by_category(conn)
    assert nets == {}                                          # nothing categorized


def test_persist_types_and_category_type_reads_stored(conn, mixed):
    n = category_types.persist_types(conn)
    assert n >= len(mixed)
    # the derived label is now stored on the row...
    stored = conn.execute(
        "SELECT type FROM categories WHERE id=?", (mixed["salary"],)).fetchone()[0]
    assert stored == category_types.INCOME
    # ...and category_type returns the stored label without re-scanning
    assert category_types.category_type(conn, mixed["groceries"]) == category_types.EXPENSE


def test_group_by_type_orders_income_before_expense(conn, mixed):
    grouped = category_types.group_by_type(conn)
    assert list(grouped.keys())[0] == category_types.INCOME    # income group first
    income_ids = {r["id"] for r in grouped[category_types.INCOME]}
    expense_ids = {r["id"] for r in grouped[category_types.EXPENSE]}
    assert mixed["salary"] in income_ids
    assert mixed["groceries"] in expense_ids
    # each group is path-sorted (inherited from list_categories)
    paths = [r["path"] for r in grouped[category_types.EXPENSE]]
    assert paths == sorted(paths, key=str.lower)

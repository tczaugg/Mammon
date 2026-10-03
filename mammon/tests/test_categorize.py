"""Tests for mammon.categorize: what a new transaction's Category cell starts as
(SRD 5.5).

The learning is the category tree's (:mod:`mammon.category_tree`); this module
is the register's side of it. Covers payee normalization, the register edits
that teach the tree (per payee and per account, categories and transfers), the
text-less suggestion (the payee's dominant category), the auto-fill hooks, and
the no-confident-match cases. The old ``import_mappings`` learned/user rows are
gone (migration 116): every learned behavior is a tree.
"""
from __future__ import annotations

import pytest

from mammon import categorize, category_tree, ledger
from mammon.tests import fresh_db


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    return ledger.create_account(conn, "Checking", "checking")


def _cat(conn, path):
    return ledger.resolve_category(conn, path)


def _txn(conn, account_id, payee, cents, category_id=None, date="2026-01-01", **fields):
    return ledger.add_transaction(
        conn, account_id, date, cents, payee=payee, category_id=category_id, **fields
    )


def _teach(conn, acct, payee, category_id, times=1):
    for _ in range(times):
        categorize.record_user_categorization(conn, payee, category_id,
                                              account_id=acct)


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------
def test_normalized_pattern_strips_store_numbers_and_case():
    assert categorize.normalized_pattern("SAFEWAY #123") == "SAFEWAY"
    assert categorize.normalized_pattern("Safeway  #456") == "SAFEWAY"
    assert categorize.normalized_pattern("  ") == ""
    assert categorize.normalized_pattern(None) == ""


# ---------------------------------------------------------------------------
# teaching + suggesting
# ---------------------------------------------------------------------------
def test_a_consistent_payee_is_suggested_after_two_choices(conn, acct):
    groceries = _cat(conn, "Groceries")
    _teach(conn, acct, "Safeway #100", groceries)
    assert categorize.suggest_category(conn, "Safeway #100") is None   # once is not enough
    _teach(conn, acct, "Safeway #100", groceries)
    assert categorize.suggest_category(conn, "Safeway #100") == groceries
    # A different store number normalizes to the same payee -> same suggestion.
    assert categorize.suggest_category(conn, "SAFEWAY #999") == groceries


def test_suggest_none_when_no_history(conn, acct):
    assert categorize.suggest_category(conn, "Totally Unknown Payee") is None
    assert categorize.suggest_category(conn, "") is None
    assert categorize.suggest_category(conn, None) is None


def test_an_evenly_split_payee_is_not_suggested(conn, acct):
    dining = _cat(conn, "Dining")
    fuel = _cat(conn, "Auto:Fuel")
    _teach(conn, acct, "Costco", dining, times=2)
    _teach(conn, acct, "Costco", fuel, times=2)
    assert categorize.suggest_category(conn, "Costco") is None


def test_dominant_category_wins_when_consistent_enough(conn, acct):
    # 3 groceries vs 1 dining -> 75% share, above the 60% bar.
    groceries = _cat(conn, "Groceries")
    dining = _cat(conn, "Dining")
    _teach(conn, acct, "Wholefoods", groceries, times=3)
    _teach(conn, acct, "Wholefoods", dining)
    assert categorize.suggest_category(conn, "Wholefoods") == groceries


def test_the_paying_account_answers_once_it_has_history(conn, acct):
    """A business account buying at the household's warehouse store: the
    payee's own history in that account wins over the payee everywhere."""
    business = ledger.create_account(conn, "Business Checking", "checking")
    groceries = _cat(conn, "Groceries")
    supplies = _cat(conn, "Office Supplies")
    _teach(conn, acct, "Costco", groceries, times=5)
    assert categorize.suggest_category(conn, "Costco", account_id=business) == groceries
    _teach(conn, business, "Costco", supplies, times=2)
    assert categorize.suggest_category(conn, "Costco", account_id=business) == supplies
    assert categorize.suggest_category(conn, "Costco", account_id=acct) == groceries


def test_a_transfer_teaches_its_account_not_a_category(conn, acct):
    savings = ledger.create_account(conn, "Savings", "savings")
    for _ in range(2):
        categorize.record_user_categorization(
            conn, "Move to savings", None, account_id=acct,
            transfer_account_id=savings)
    assert categorize.suggest_category(conn, "Move to savings") is None
    assert category_tree.dominant(conn, "Move to savings") == \
        category_tree.account_label(savings)


def test_clearing_a_category_teaches_nothing(conn, acct):
    categorize.record_user_categorization(conn, "Kwik Stop", None, account_id=acct)
    assert category_tree.known_categories(conn, "Kwik Stop") == []


# ---------------------------------------------------------------------------
# auto-fill hook
# ---------------------------------------------------------------------------
def test_autofill_fills_empty_category_on_new_txn(conn, acct):
    groceries = _cat(conn, "Groceries")
    _teach(conn, acct, "Trader Joes", groceries, times=2)
    new_id = _txn(conn, acct, "Trader Joes #22", -3100, None, date="2026-06-01")
    assert categorize.autofill_transaction(conn, new_id) == groceries
    assert ledger.get_transaction(conn, new_id)["category_id"] == groceries


def test_autofill_does_not_overwrite_existing_category(conn, acct):
    groceries = _cat(conn, "Groceries")
    dining = _cat(conn, "Dining")
    _teach(conn, acct, "Panera", groceries, times=2)
    already = _txn(conn, acct, "Panera", -1600, dining, date="2026-06-01")
    assert categorize.autofill_transaction(conn, already) is None
    assert ledger.get_transaction(conn, already)["category_id"] == dining
    # ...but overwrite=True honors the learned suggestion.
    assert categorize.autofill_transaction(conn, already, overwrite=True) == groceries


def test_autofill_no_confident_match_leaves_category_empty(conn, acct):
    new_id = _txn(conn, acct, "Some New Merchant", -900, None, date="2026-06-02")
    assert categorize.autofill_transaction(conn, new_id) is None
    assert ledger.get_transaction(conn, new_id)["category_id"] is None


def test_autofill_skips_transfers(conn, acct):
    other = ledger.create_account(conn, "Savings", "savings")
    from_id, _to = ledger.create_transfer(conn, acct, other, "2026-06-03", 5000)
    assert categorize.autofill_transaction(conn, from_id) is None


def test_autocategorize_import_batch(conn, acct):
    groceries = _cat(conn, "Groceries")
    _teach(conn, acct, "Aldi", groceries, times=2)
    import_id = conn.execute(
        "INSERT INTO imports(provider, status) VALUES ('test','done')"
    ).lastrowid
    conn.commit()
    a = _txn(conn, acct, "Aldi #7", -2200, None, date="2026-06-01", import_id=import_id)
    b = _txn(conn, acct, "Unknown Co", -500, None, date="2026-06-01", import_id=import_id)
    assert categorize.autocategorize_import(conn, import_id) == 1
    assert ledger.get_transaction(conn, a)["category_id"] == groceries
    assert ledger.get_transaction(conn, b)["category_id"] is None


# ---------------------------------------------------------------------------
# a newer choice outvotes, and forgetting starts over
# ---------------------------------------------------------------------------
def test_a_new_habit_takes_over_once_it_dominates(conn, acct):
    groceries = _cat(conn, "Groceries")
    dining = _cat(conn, "Dining")
    _teach(conn, acct, "Corner Market", groceries, times=2)
    assert categorize.suggest_category(conn, "Corner Market") == groceries
    _teach(conn, acct, "Corner Market", dining, times=2)
    assert categorize.suggest_category(conn, "Corner Market") is None    # 50/50
    _teach(conn, acct, "Corner Market", dining, times=2)
    assert categorize.suggest_category(conn, "Corner Market") == dining


def test_forgetting_a_payee_starts_it_over(conn, acct):
    groceries = _cat(conn, "Groceries")
    _teach(conn, acct, "Flux Store", groceries, times=3)
    category_tree.forget_payee(conn, "Flux Store")
    assert categorize.suggest_category(conn, "Flux Store") is None
    assert category_tree.known_categories(conn, "Flux Store", account_id=acct) == []

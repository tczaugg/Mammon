"""Tests for mammon.categorize: auto-categorization from history (SRD 5.5).

Covers learning a consistent payee -> category mapping, suggesting it,
auto-filling a new transaction, a user override that outranks learned and
survives re-learning, and the no-confident-match (ambiguous / unknown) cases.
Also: payee normalization (store numbers, case), transfer exclusion, and the
import-batch auto-fill hook.
"""
from __future__ import annotations

import pytest

from mammon import categorize, db, ledger
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


def _mapping(conn, payee):
    return categorize.mapping_for(conn, payee)


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------
def test_normalized_pattern_strips_store_numbers_and_case():
    assert categorize.normalized_pattern("SAFEWAY #123") == "SAFEWAY"
    assert categorize.normalized_pattern("Safeway  #456") == "SAFEWAY"
    assert categorize.normalized_pattern("  ") == ""
    assert categorize.normalized_pattern(None) == ""


# ---------------------------------------------------------------------------
# learn + suggest
# ---------------------------------------------------------------------------
def test_learn_consistent_payee_then_suggest(conn, acct):
    groceries = _cat(conn, "Groceries")
    for i in range(3):
        _txn(conn, acct, "Safeway #100", -4200, groceries, date=f"2026-01-0{i+1}")
    n = categorize.learn_from_history(conn)
    assert n == 1
    assert categorize.suggest_category(conn, "Safeway #100") == groceries
    # A different store number normalizes to the same pattern -> same suggestion.
    assert categorize.suggest_category(conn, "SAFEWAY #999") == groceries
    row = _mapping(conn, "Safeway #100")
    assert row["source"] == "learned"
    assert row["payee_pattern"] == "SAFEWAY"
    assert row["hit_count"] == 3


def test_suggest_none_when_no_history(conn, acct):
    assert categorize.suggest_category(conn, "Totally Unknown Payee") is None
    assert categorize.suggest_category(conn, "") is None
    assert categorize.suggest_category(conn, None) is None


def test_ambiguous_payee_is_not_learned(conn, acct):
    # Same payee split evenly across two categories -> no confident mapping.
    dining = _cat(conn, "Dining")
    fuel = _cat(conn, "Auto:Fuel")
    _txn(conn, acct, "Costco", -5000, dining, date="2026-02-01")
    _txn(conn, acct, "Costco", -5000, fuel, date="2026-02-02")
    categorize.learn_from_history(conn)
    assert categorize.suggest_category(conn, "Costco") is None
    assert _mapping(conn, "Costco") is None


def test_dominant_category_wins_when_consistent_enough(conn, acct):
    # 3 groceries vs 1 dining -> 75% share, above the 60% bar -> learn groceries.
    groceries = _cat(conn, "Groceries")
    dining = _cat(conn, "Dining")
    for i in range(3):
        _txn(conn, acct, "Wholefoods", -3000, groceries, date=f"2026-03-0{i+1}")
    _txn(conn, acct, "Wholefoods", -3000, dining, date="2026-03-09")
    categorize.learn_from_history(conn)
    assert categorize.suggest_category(conn, "Wholefoods") == groceries


def test_transfers_are_never_learned(conn, acct):
    other = ledger.create_account(conn, "Savings", "savings")
    ledger.create_transfer(conn, acct, other, "2026-04-01", 10000, memo="move")
    # A transfer has no category and transfer_account_id set; learning ignores it.
    assert categorize.learn_from_history(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM import_mappings").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# auto-fill hook
# ---------------------------------------------------------------------------
def test_autofill_fills_empty_category_on_new_txn(conn, acct):
    groceries = _cat(conn, "Groceries")
    for i in range(2):
        _txn(conn, acct, "Trader Joes", -2500, groceries, date=f"2026-05-0{i+1}")
    categorize.learn_from_history(conn)
    # New transaction, same payee, no category.
    new_id = _txn(conn, acct, "Trader Joes #22", -3100, None, date="2026-06-01")
    filled = categorize.autofill_transaction(conn, new_id)
    assert filled == groceries
    assert ledger.get_transaction(conn, new_id)["category_id"] == groceries


def test_autofill_does_not_overwrite_existing_category(conn, acct):
    groceries = _cat(conn, "Groceries")
    dining = _cat(conn, "Dining")
    _txn(conn, acct, "Panera", -1500, groceries, date="2026-05-01")
    categorize.learn_from_history(conn)
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
    _txn(conn, acct, "Aldi", -2000, groceries, date="2026-05-01")
    categorize.learn_from_history(conn)
    import_id = conn.execute(
        "INSERT INTO imports(provider, status) VALUES ('test','done')"
    ).lastrowid
    conn.commit()
    a = _txn(conn, acct, "Aldi #7", -2200, None, date="2026-06-01", import_id=import_id)
    b = _txn(conn, acct, "Unknown Co", -500, None, date="2026-06-01", import_id=import_id)
    filled = categorize.autocategorize_import(conn, import_id)
    assert filled == 1
    assert ledger.get_transaction(conn, a)["category_id"] == groceries
    assert ledger.get_transaction(conn, b)["category_id"] is None


# ---------------------------------------------------------------------------
# user override outranks learned
# ---------------------------------------------------------------------------
def test_user_override_updates_mapping_and_outranks_learned(conn, acct):
    groceries = _cat(conn, "Groceries")
    dining = _cat(conn, "Dining")
    for i in range(3):
        _txn(conn, acct, "Corner Market", -1000, groceries, date=f"2026-05-0{i+1}")
    categorize.learn_from_history(conn)
    assert categorize.suggest_category(conn, "Corner Market") == groceries

    # User re-categorizes this payee to Dining.
    categorize.record_user_categorization(conn, "Corner Market", dining)
    assert categorize.suggest_category(conn, "Corner Market") == dining
    row = _mapping(conn, "Corner Market")
    assert row["source"] == "user"

    # Re-learning from history (which still says Groceries) must NOT clobber it.
    categorize.learn_from_history(conn)
    assert categorize.suggest_category(conn, "Corner Market") == dining
    assert _mapping(conn, "Corner Market")["source"] == "user"


def test_user_override_creates_mapping_without_prior_history(conn, acct):
    dining = _cat(conn, "Dining")
    assert categorize.suggest_category(conn, "Brand New Cafe") is None
    categorize.record_user_categorization(conn, "Brand New Cafe", dining)
    assert categorize.suggest_category(conn, "Brand New Cafe") == dining
    # And a fresh transaction with that payee now auto-fills to the user's choice.
    new_id = _txn(conn, acct, "Brand New Cafe #4", -750, None, date="2026-07-01")
    assert categorize.autofill_transaction(conn, new_id) == dining


def test_user_override_to_none_suppresses_suggestion(conn, acct):
    groceries = _cat(conn, "Groceries")
    for i in range(3):
        _txn(conn, acct, "Kwik Stop", -800, groceries, date=f"2026-05-0{i+1}")
    categorize.learn_from_history(conn)
    assert categorize.suggest_category(conn, "Kwik Stop") == groceries
    # User clears the category for this payee -> no more suggestions.
    categorize.record_user_categorization(conn, "Kwik Stop", None)
    assert categorize.suggest_category(conn, "Kwik Stop") is None
    # ...and re-learning still respects the user's decision.
    categorize.learn_from_history(conn)
    assert categorize.suggest_category(conn, "Kwik Stop") is None
    assert _mapping(conn, "Kwik Stop")["source"] == "user"


# ---------------------------------------------------------------------------
# incremental relearn + stale-learned cleanup
# ---------------------------------------------------------------------------
def test_relearn_payee_targets_one_pattern(conn, acct):
    groceries = _cat(conn, "Groceries")
    _txn(conn, acct, "Local Deli", -1200, groceries, date="2026-05-01")
    assert categorize.relearn_payee(conn, "Local Deli") == groceries
    assert categorize.suggest_category(conn, "Local Deli") == groceries


def test_learned_mapping_dropped_when_history_becomes_ambiguous(conn, acct):
    groceries = _cat(conn, "Groceries")
    dining = _cat(conn, "Dining")
    g = _txn(conn, acct, "Flux Store", -1000, groceries, date="2026-05-01")
    categorize.learn_from_history(conn)
    assert categorize.suggest_category(conn, "Flux Store") == groceries
    # Add an equal, conflicting observation -> now 50/50, no longer confident.
    _txn(conn, acct, "Flux Store", -1000, dining, date="2026-05-02")
    categorize.learn_from_history(conn)
    assert categorize.suggest_category(conn, "Flux Store") is None
    assert _mapping(conn, "Flux Store") is None

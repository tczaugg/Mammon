"""Optional account/amount/memo conditions on category & transfer rules.

Roadmap item 10 (Rules manager), slice A: schema + domain only. A learned
``keyword -> value`` rule may additionally carry ANDed conditions -- an account
scope, an inclusive SIGNED integer-cent amount range, and a case-insensitive
memo substring. The load-bearing contract these tests pin down:

* the migration adds the four NULLABLE columns to BOTH rule tables (it was
  migration 40; later migrations append, so the version is only ever >= that);
* conditions are honoured only when the matcher is handed transaction context;
* with NULL conditions OR no context, matching is byte-identical to a plain
  keyword rule -- so every pre-existing caller keeps working unchanged;
* category- and transfer-rule engines behave identically (shared machinery).
"""
from __future__ import annotations

import pytest

from mammon import category_rules, db, ledger, transfer_rules


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def accounts(conn):
    return {
        "checking": ledger.create_account(conn, "First Checking", "checking"),
        "savings": ledger.create_account(conn, "First Savings", "savings"),
        "venmo": ledger.create_account(conn, "Venmo", "checking"),
    }


@pytest.fixture
def cats(conn):
    return {
        "shopping": ledger.resolve_category(conn, "Shopping"),
        "dining": ledger.resolve_category(conn, "Dining"),
        "fuel": ledger.resolve_category(conn, "Auto & Transport:Fuel"),
        "gym": ledger.resolve_category(conn, "Health:Gym"),
    }


# ---------------------------------------------------------------------------
# migration / schema
# ---------------------------------------------------------------------------
def test_schema_version_covers_this_migration():
    # Pinned as a FLOOR, not an equality: migrations are appended, so a test
    # that demands the exact number fails every time an unrelated feature adds
    # one -- which says nothing about these columns.
    assert db.SCHEMA_VERSION == len(db.MIGRATIONS)
    assert db.SCHEMA_VERSION >= 40


def test_migration_applies_to_fresh_db(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


@pytest.mark.parametrize("table", ["category_rules", "transfer_rules"])
def test_condition_columns_exist_on_both_tables(conn, table):
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    assert {"account_id", "amount_min_cents",
            "amount_max_cents", "memo_contains"} <= cols


# ---------------------------------------------------------------------------
# category rules -- conditions
# ---------------------------------------------------------------------------
def test_category_rule_scoped_by_account(conn, accounts, cats):
    category_rules.upsert_rule(
        conn, "AMAZON", cats["shopping"], account_id=accounts["checking"])
    rules = category_rules.load_rules(conn)
    desc = "AMAZON MARKETPLACE"

    # In-scope account: fires.
    m = category_rules.match_rule(
        desc, rules, context={"account_id": accounts["checking"]})
    assert m is not None and m["category_id"] == cats["shopping"]

    # Other account: the account condition is not satisfied -> no match.
    assert category_rules.match_rule(
        desc, rules, context={"account_id": accounts["savings"]}) is None

    # Context supplied but no account_id to confirm the scope -> no match.
    assert category_rules.match_rule(desc, rules, context={}) is None

    # BACKWARD COMPAT: no context at all -> scoped rule behaves as a plain
    # keyword rule (this is exactly what today's callers pass).
    assert category_rules.match_rule(desc, rules) is not None
    assert category_rules.apply_rules(conn, desc) == cats["shopping"]
    assert category_rules.apply_rules(
        conn, desc, context={"account_id": accounts["savings"]}) is None


def test_category_rule_amount_range_gates(conn, cats):
    # Between -$20.00 and -$5.00 (an expense of that magnitude).
    category_rules.upsert_rule(
        conn, "SPOTIFY", cats["dining"],
        amount_min_cents=-2000, amount_max_cents=-500)
    rules = category_rules.load_rules(conn)
    desc = "SPOTIFY USA"

    def match(cents):
        return category_rules.match_rule(
            desc, rules, context={"amount_cents": cents})

    assert match(-1000) is not None          # inside
    assert match(-2000) is not None          # inclusive lower bound
    assert match(-500) is not None           # inclusive upper bound
    assert match(-2001) is None              # below the range
    assert match(-499) is None               # above the range
    # SIGNED comparison: a +$10 deposit is NOT in a -$20..-$5 window even
    # though its magnitude is. A naive abs() would wrongly match here.
    assert match(1000) is None
    # Context lacking the amount cannot confirm the range -> no match.
    assert category_rules.match_rule(desc, rules, context={}) is None
    # No context -> plain keyword rule.
    assert category_rules.match_rule(desc, rules) is not None


def test_category_rule_memo_contains_case_insensitive(conn, cats):
    category_rules.upsert_rule(
        conn, "ACME FITNESS", cats["gym"], memo_contains="Membership")
    rules = category_rules.load_rules(conn)
    desc = "ACME FITNESS CENTER"

    def match(memo):
        return category_rules.match_rule(
            desc, rules, context={"memo": memo})

    assert match("Monthly MEMBERSHIP dues") is not None   # case-insensitive
    assert match("annual membership") is not None
    assert match("one-time guest pass") is None            # substring absent
    assert match(None) is None                             # no memo to test
    assert category_rules.match_rule(desc, rules, context={}) is None
    # No context -> plain keyword rule.
    assert category_rules.match_rule(desc, rules) is not None


def test_all_null_conditions_is_a_plain_keyword_rule(conn, cats):
    rid = category_rules.upsert_rule(conn, "NETFLIX", cats["dining"])
    stored = category_rules.get_rule(conn, rid)
    assert stored["account_id"] is None
    assert stored["amount_min_cents"] is None
    assert stored["amount_max_cents"] is None
    assert stored["memo_contains"] is None

    rules = category_rules.load_rules(conn)
    desc = "NETFLIX.COM"
    # Identical result with rich context or none at all -- no condition gates it.
    assert category_rules.match_rule(desc, rules) is not None
    assert category_rules.match_rule(
        desc, rules,
        context={"account_id": 999, "amount_cents": -1234,
                 "memo": "whatever"})["category_id"] == cats["dining"]
    assert category_rules.apply_rules(conn, desc) == cats["dining"]


def test_multiple_conditions_all_must_hold(conn, accounts, cats):
    category_rules.upsert_rule(
        conn, "SHELL", cats["fuel"],
        account_id=accounts["checking"],
        amount_min_cents=-10000, amount_max_cents=-1000,
        memo_contains="fuel")
    rules = category_rules.load_rules(conn)
    desc = "SHELL OIL"
    good = {"account_id": accounts["checking"],
            "amount_cents": -4500, "memo": "SHELL FUEL PURCHASE"}
    assert category_rules.match_rule(desc, rules, context=good) is not None
    # Break exactly one condition at a time -> no match each time.
    assert category_rules.match_rule(
        desc, rules, context={**good, "account_id": accounts["savings"]}) is None
    assert category_rules.match_rule(
        desc, rules, context={**good, "amount_cents": -50}) is None
    assert category_rules.match_rule(
        desc, rules, context={**good, "memo": "grocery run"}) is None


def test_upsert_preserves_conditions_when_reteaching_without_them(conn, accounts,
                                                                  cats):
    rid = category_rules.upsert_rule(
        conn, "AMAZON", cats["shopping"], account_id=accounts["checking"])
    # Re-teach the same keyword (e.g. the learn path) without conditions: the
    # payload updates but the account scope must NOT be silently wiped.
    again = category_rules.upsert_rule(conn, "AMAZON", cats["dining"])
    assert again == rid
    stored = category_rules.get_rule(conn, rid)
    assert stored["category_id"] == cats["dining"]
    assert stored["account_id"] == accounts["checking"]


# ---------------------------------------------------------------------------
# transfer rules -- conditions (same machinery, payload is an account id)
# ---------------------------------------------------------------------------
def test_transfer_rule_scoped_by_account(conn, accounts):
    # account_id (scope: where the row lives) is distinct from the payload
    # transfer_account_id (the transfer's far side, here Venmo).
    transfer_rules.upsert_rule(
        conn, "VENMO", accounts["venmo"], account_id=accounts["checking"])
    rules = transfer_rules.load_rules(conn)
    desc = "VENMO CASHOUT"

    m = transfer_rules.match_rule(
        desc, rules, context={"account_id": accounts["checking"]})
    assert m is not None and m["transfer_account_id"] == accounts["venmo"]

    assert transfer_rules.match_rule(
        desc, rules, context={"account_id": accounts["savings"]}) is None
    assert transfer_rules.match_rule(desc, rules, context={}) is None

    # BACKWARD COMPAT: no context -> plain keyword rule (today's callers).
    assert transfer_rules.match_rule(desc, rules) is not None
    assert transfer_rules.apply_rules(conn, desc) == accounts["venmo"]
    assert transfer_rules.apply_rules(
        conn, desc, context={"account_id": accounts["savings"]}) is None


def test_transfer_rule_amount_range_gates(conn, accounts):
    transfer_rules.upsert_rule(
        conn, "VENMO", accounts["venmo"],
        amount_min_cents=-5000, amount_max_cents=5000)
    rules = transfer_rules.load_rules(conn)
    desc = "VENMO PAYMENT"

    def match(cents):
        return transfer_rules.match_rule(
            desc, rules, context={"amount_cents": cents})

    assert match(0) is not None
    assert match(-5000) is not None
    assert match(5000) is not None
    assert match(-5001) is None
    assert match(5001) is None
    assert transfer_rules.match_rule(desc, rules) is not None  # no context


def test_transfer_rule_memo_contains_case_insensitive(conn, accounts):
    transfer_rules.upsert_rule(
        conn, "VENMO", accounts["venmo"], memo_contains="rent")
    rules = transfer_rules.load_rules(conn)
    desc = "VENMO TRANSFER"

    assert transfer_rules.match_rule(
        desc, rules, context={"memo": "MONTHLY RENT"}) is not None
    assert transfer_rules.match_rule(
        desc, rules, context={"memo": "dinner split"}) is None
    assert transfer_rules.match_rule(desc, rules) is not None  # no context


def test_transfer_rule_all_null_conditions_is_plain(conn, accounts):
    rid = transfer_rules.upsert_rule(conn, "VENMO", accounts["venmo"])
    stored = transfer_rules.load_rules(conn)[0]
    assert stored["id"] == rid
    assert stored["account_id"] is None
    assert stored["amount_min_cents"] is None
    assert stored["amount_max_cents"] is None
    assert stored["memo_contains"] is None

    rules = transfer_rules.load_rules(conn)
    desc = "VENMO CASHOUT"
    assert transfer_rules.match_rule(desc, rules) is not None
    assert transfer_rules.match_rule(
        desc, rules,
        context={"account_id": 42, "amount_cents": 999,
                 "memo": "x"})["transfer_account_id"] == accounts["venmo"]
    assert transfer_rules.apply_rules(conn, desc) == accounts["venmo"]

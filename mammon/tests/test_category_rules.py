"""Tests for the category learning engine (the category-side twin of
payee_rules): keyword->category persistence/CRUD, whole-token matching,
longest-keyword precedence, learning from a user's category choice, and
keyword-scoped refinement that differentiates a one-off correction from a good
broad rule without clobbering it."""
from __future__ import annotations

import pytest

from mammon import category_rules, db, ledger


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def cats(conn):
    return {
        "dining": ledger.resolve_category(conn, "Dining"),
        "shopping": ledger.resolve_category(conn, "Shopping"),
        "aws": ledger.resolve_category(conn, "Business:Cloud"),
    }


# ---------------------------------------------------------------------------
# CRUD + persistence
# ---------------------------------------------------------------------------
def test_upsert_creates_then_updates_same_keyword(conn, cats):
    rid = category_rules.upsert_rule(conn, "STARBUCKS", cats["dining"])
    assert rid is not None
    again = category_rules.upsert_rule(conn, "STARBUCKS", cats["shopping"])
    assert again == rid  # same keyword -> update, not a new row
    rules = category_rules.list_rules(conn)
    assert len(rules) == 1
    assert rules[0]["category_id"] == cats["shopping"]


def test_upsert_rejects_blank_keyword_or_none_category(conn, cats):
    assert category_rules.upsert_rule(conn, "", cats["dining"]) is None
    assert category_rules.upsert_rule(conn, "STARBUCKS", None) is None
    assert category_rules.list_rules(conn) == []


def test_update_and_delete_rule(conn, cats):
    rid = category_rules.upsert_rule(conn, "COSTCO", cats["shopping"])
    category_rules.update_rule(conn, rid, category_id=cats["dining"])
    assert category_rules.get_rule(conn, rid)["category_id"] == cats["dining"]
    category_rules.delete_rule(conn, rid)
    assert category_rules.get_rule(conn, rid) is None


def test_delete_category_cascades_rule(conn, cats):
    rid = category_rules.upsert_rule(conn, "NETFLIX", cats["dining"])
    assert category_rules.get_rule(conn, rid) is not None
    # ON DELETE CASCADE: removing the category removes the rule.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM categories WHERE id=?", (cats["dining"],))
    conn.commit()
    assert category_rules.get_rule(conn, rid) is None


# ---------------------------------------------------------------------------
# matching / apply
# ---------------------------------------------------------------------------
def test_match_is_whole_token(conn, cats):
    category_rules.upsert_rule(conn, "CAT", cats["dining"])
    assert category_rules.apply_rules(conn, "POS CAT CAFE 12") == cats["dining"]
    assert category_rules.apply_rules(conn, "CATERPILLAR INC PMT") is None


def test_apply_none_when_no_rule(conn):
    assert category_rules.apply_rules(conn, "WHOLE FOODS MARKET") is None


def test_more_specific_keyword_wins(conn, cats):
    category_rules.upsert_rule(conn, "STORE", cats["shopping"])
    category_rules.upsert_rule(conn, "STARBUCKS", cats["dining"])
    assert category_rules.apply_rules(conn, "STARBUCKS STORE 1234") == cats["dining"]


# ---------------------------------------------------------------------------
# learning from a user's category choice
# ---------------------------------------------------------------------------
def test_learn_creates_rule_only_on_change(conn, cats):
    # no change from provisional -> nothing learned
    assert category_rules.learn_from_edit(
        conn, "AMAZON.COM PURCHASE", cats["shopping"],
        provisional=cats["shopping"]) is None
    assert category_rules.list_rules(conn) == []
    # a real choice -> a rule keyed on the leading token
    rid = category_rules.learn_from_edit(
        conn, "AMAZON.COM PURCHASE 998", cats["shopping"], provisional=None)
    assert rid is not None
    assert category_rules.apply_rules(conn, "AMAZON MKTP US*XYZ") == cats["shopping"]


def test_learn_ignores_blank_choice(conn, cats):
    assert category_rules.learn_from_edit(
        conn, "STARBUCKS", None, provisional=cats["dining"]) is None
    assert category_rules.list_rules(conn) == []


# ---------------------------------------------------------------------------
# keyword-scoped refinement: a wrong auto-fill must NOT clobber the broad rule
# ---------------------------------------------------------------------------
def test_refine_does_not_clobber_broad_rule(conn, cats):
    # Broad rule: anything AMAZON -> Shopping.
    category_rules.upsert_rule(conn, "AMAZON", cats["shopping"])
    # User sees "Shopping" auto-filled for an AWS invoice and corrects it.
    category_rules.learn_from_edit(
        conn, "AMAZON AWS CLOUD 12", cats["aws"], provisional=cats["shopping"])
    # The AWS variant now maps to Cloud...
    assert category_rules.apply_rules(conn, "AMAZON AWS CLOUD 99") == cats["aws"]
    # ...while the broad rule still serves ordinary Amazon shopping.
    assert category_rules.apply_rules(conn, "AMAZON.COM ORDER 77") == cats["shopping"]
    # The broad rule was refined, not overwritten (two rules now exist).
    kws = {r["keyword"] for r in category_rules.list_rules(conn)}
    assert "AMAZON" in kws and "AMAZON AWS" in kws


def test_refine_falls_back_to_overwrite_without_distinguishing_token(conn, cats):
    # A description with no extra distinguishing token can only overwrite.
    category_rules.upsert_rule(conn, "SPOTIFY", cats["shopping"])
    category_rules.learn_from_edit(
        conn, "SPOTIFY", cats["dining"], provisional=cats["shopping"])
    assert category_rules.apply_rules(conn, "SPOTIFY") == cats["dining"]
    assert len(category_rules.list_rules(conn)) == 1

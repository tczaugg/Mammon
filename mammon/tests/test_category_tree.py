"""Tests for the per-payee category tree (mammon.category_tree).

The regressions worth naming here are the ones the flat keyword table could not
express: a candidate category that the payee has never carried, and a catalogue
payee (Amazon) auto-filling one category onto everything because its root looks
pure. Both are asserted directly.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from mammon import category_tree as ct  # noqa: E402
from mammon import db as mdb  # noqa: E402
from mammon import ledger  # noqa: E402
from mammon.tests import fresh_db


@pytest.fixture()
def conn(tmp_path):
    c = fresh_db(str(tmp_path / "t.db"))
    yield c
    c.close()


def _cat(conn, path):
    return ledger.resolve_category(conn, path)


# ---------------------------------------------------------------------------
# source text: the two importer shapes
# ---------------------------------------------------------------------------
def test_source_text_prefers_the_bank_description():
    assert ct.source_text("WAL-MART #4321 ANYTOWN UT", "Walmart") == \
        "WAL-MART #4321 ANYTOWN UT"


def test_source_text_falls_back_to_a_supplied_payee():
    """The Costco card sends no description; the same text arrives as the payee
    and an empty memo. A tree reading only ``memo`` is blind on that account."""
    assert ct.source_text("", "COSTCO GAS #1234 ANYTOWN UT",
                          payee_supplied=True) == "COSTCO GAS #1234 ANYTOWN UT"
    assert ct.source_text(None, None) == ""


# ---------------------------------------------------------------------------
# the Costco case, end to end
# ---------------------------------------------------------------------------
def test_one_token_splits_a_payee_into_two_confident_categories(conn):
    fuel = _cat(conn, "Auto:Fuel")
    groc = _cat(conn, "Groceries")
    for _ in range(10):
        ct.learn(conn, "Costco", "COSTCO WHSE #1234 ANYTOWN UT", groc)
    for _ in range(6):
        ct.learn(conn, "Costco", "COSTCO GAS #1234 ANYTOWN UT", fuel)

    gas = ct.suggest(conn, "Costco", "COSTCO GAS #1234 ANYTOWN UT")
    whse = ct.suggest(conn, "Costco", "COSTCO WHSE #1234 ANYTOWN UT")
    assert gas.action == ct.ACTION_AUTO and gas.category_id == fuel
    assert whse.action == ct.ACTION_AUTO and whse.category_id == groc


def test_a_third_shape_earns_its_own_answer_once_established(conn):
    """The third Costco shape the user named -- neither GAS nor WHSE.

    While it has only one sighting it is NOT treated as its own case: too
    little evidence to say this shape differs from the payee's mainstream, so
    the answer falls back to that (see
    :func:`test_a_stray_correction_does_not_capture_the_mainstream_branch` for
    why a one-vote node must not override its parent). Once the shape has
    MIN_COUNT corroboration it takes over, which is the "once counts get
    established" behaviour: the tree does not need to be told that WWW is a
    different kind of Costco purchase, it works it out from how often it is
    corrected.
    """
    fuel = _cat(conn, "Auto:Fuel")
    groc = _cat(conn, "Groceries")
    hh = _cat(conn, "Household")
    for _ in range(10):
        ct.learn(conn, "Costco", "COSTCO WHSE #1234 ANYTOWN UT", groc)
    for _ in range(6):
        ct.learn(conn, "Costco", "COSTCO GAS #1234 ANYTOWN UT", fuel)

    # One sighting: not yet its own case, so the payee's mainstream answers.
    for _ in range(ct.MIN_COUNT - 1):
        ct.learn(conn, "Costco", "WWW COSTCO COM 800-955-2292 WA", hh)
    early = ct.suggest(conn, "Costco", "WWW COSTCO COM 800-955-2292 WA")
    assert early.category_id != hh
    assert hh in early.category_ids           # still offered in the picker

    # Corroborated: now it is.
    for _ in range(ct.MIN_COUNT):
        ct.learn(conn, "Costco", "WWW COSTCO COM 800-955-2292 WA", hh)
    late = ct.suggest(conn, "Costco", "WWW COSTCO COM 800-955-2292 WA")
    assert late.action == ct.ACTION_AUTO and late.category_id == hh
    assert set(late.category_ids) == {hh, groc, fuel}


def test_a_stray_correction_does_not_capture_the_mainstream_branch(conn):
    """Regression, found on the real ledger: ONE Costco warehouse trip
    categorized Dining minted a ``WHSE`` child holding a single vote. Because
    WHSE ranks first, every later "COSTCO WHSE ..." row walked into that
    one-vote node instead of stopping at the root's 41 Groceries -- so the
    confident answer was lost and the picker led with Dining.
    """
    groc = _cat(conn, "Groceries")
    dining = _cat(conn, "Dining")
    fuel = _cat(conn, "Auto:Fuel")
    for _ in range(10):
        ct.learn(conn, "Costco", "COSTCO WHSE #1234 ANYTOWN UT", groc)
    for _ in range(4):
        ct.learn(conn, "Costco", "COSTCO GAS #1234 ANYTOWN UT", fuel)
    ct.learn(conn, "Costco", "COSTCO WHSE #1234 ANYTOWN UT", dining)  # the stray

    s = ct.suggest(conn, "Costco", "COSTCO WHSE #1234 ANYTOWN UT")
    assert s.action == ct.ACTION_AUTO and s.category_id == groc
    assert s.category_ids[0] == groc          # and Dining does not lead the picker


# ---------------------------------------------------------------------------
# the invariant the user asked for
# ---------------------------------------------------------------------------
def test_never_proposes_a_category_the_payee_has_not_carried(conn):
    groc = _cat(conn, "Groceries")
    util = _cat(conn, "Utilities:Gas & Electric")
    # Another payee in the same town, categorized as Utilities. The old keyword
    # engine learned "ANYTOWN" from it and then fired on every local merchant.
    for _ in range(8):
        ct.learn(conn, "Dominion Energy", "DOMINION ENERGY ANYTOWN UT", util)
    for _ in range(8):
        ct.learn(conn, "Subway", "SUBWAY 61276 ANYTOWN UT", groc)

    s = ct.suggest(conn, "Subway", "SUBWAY 61276 ANYTOWN UT")
    assert util not in s.category_ids
    assert s.category_id != util


def test_an_unknown_payee_proposes_nothing_at_all(conn):
    """The learning period. A first sighting has no history, so there is nothing
    honest to offer -- blank category, empty promotion, full alphabetical list."""
    _cat(conn, "Groceries")
    s = ct.suggest(conn, "Brand New Merchant", "BRAND NEW MERCHANT SLC UT")
    assert s.action == ct.ACTION_LEAVE
    assert s.category_id is None
    assert s.candidates == []


def test_one_sighting_is_never_confident(conn):
    groc = _cat(conn, "Groceries")
    ct.learn(conn, "Kroger", "KROGER #123 YPSILANTI MI", groc)
    s = ct.suggest(conn, "Kroger", "KROGER #123 YPSILANTI MI")
    assert s.action == ct.ACTION_DROPDOWN     # known payee, but MIN_COUNT unmet
    assert s.category_id is None
    assert s.category_ids == [groc]           # still worth promoting


# ---------------------------------------------------------------------------
# the payee-coherence gate
# ---------------------------------------------------------------------------
def test_a_catalogue_payee_never_auto_fills(conn):
    """Amazon: every item description is unique, so the root looks pure to the
    node gate and would auto-fill its most common category onto everything. 61
    of 67 errors in the measured replay were exactly this."""
    hh = _cat(conn, "Household")
    others = [_cat(conn, p) for p in
              ("Clothing", "Computer:Accessories", "Medical:Equipment",
               "Gifts", "Books", "Groceries")]
    for i in range(8):
        ct.learn(conn, "Amazon", f"AMAZON MARKETPLACE ITEM{i} SEATTLE WA", hh)
    for i, cid in enumerate(others):
        for j in range(2):
            ct.learn(conn, "Amazon", f"AMAZON THING {i}{j} SEATTLE WA", cid)

    assert ct.coherence(conn, "Amazon") < ct.PAYEE_COHERENCE
    s = ct.suggest(conn, "Amazon", "AMAZON SOMETHING ELSE SEATTLE WA")
    assert s.action == ct.ACTION_DROPDOWN
    assert s.category_id is None
    assert s.category_ids[0] == hh            # still ranked, just not filled in


def test_a_coherent_payee_still_auto_fills(conn):
    """The gate must not swallow the ordinary case."""
    groc = _cat(conn, "Groceries")
    fuel = _cat(conn, "Auto:Fuel")
    for i in range(12):
        ct.learn(conn, "Kroger", f"KROGER #12{i} YPSILANTI MI", groc)
    ct.learn(conn, "Kroger", "KROGER FUEL YPSILANTI MI", fuel)
    assert ct.coherence(conn, "Kroger") >= ct.PAYEE_COHERENCE
    s = ct.suggest(conn, "Kroger", "KROGER #999 YPSILANTI MI")
    assert s.action == ct.ACTION_AUTO and s.category_id == groc


# ---------------------------------------------------------------------------
# text-less evidence
# ---------------------------------------------------------------------------
def test_textless_votes_feed_the_tally_but_not_the_trie(conn):
    """A register edit (and every imported Quicken row) has a payee and a
    category but no bank text. Those votes must not land on the root, or the
    mismatched ones pile up there and the confident case stops firing."""
    groc = _cat(conn, "Groceries")
    fuel = _cat(conn, "Auto:Fuel")
    for _ in range(10):
        ct.learn(conn, "Costco", "COSTCO WHSE #1234 ANYTOWN UT", groc)
    for _ in range(6):
        ct.learn(conn, "Costco", "COSTCO GAS #1234 ANYTOWN UT", fuel)
    for _ in range(30):                        # history, no description
        ct.learn(conn, "Costco", "", groc)
    for _ in range(20):
        ct.learn(conn, "Costco", "", fuel)

    s = ct.suggest(conn, "Costco", "COSTCO WHSE #1234 ANYTOWN UT")
    assert s.action == ct.ACTION_AUTO and s.category_id == groc
    # ...but the tally saw all of it, which is what the picker ranks on.
    assert dict(ct.known_categories(conn, "Costco"))[fuel] == 26


def test_normalization_collapses_store_numbers(conn):
    groc = _cat(conn, "Groceries")
    for _ in range(6):
        ct.learn(conn, "Safeway #123", "SAFEWAY 123 SLC UT", groc)
    assert ct.known_categories(conn, "Safeway  #456") == [(groc, 6)]


# ---------------------------------------------------------------------------
# housekeeping
# ---------------------------------------------------------------------------
def test_unlearn_withdraws_a_vote(conn):
    groc = _cat(conn, "Groceries")
    for _ in range(3):
        ct.learn(conn, "Kroger", "KROGER SLC UT", groc)
    ct.unlearn(conn, "Kroger", "KROGER SLC UT", groc)
    assert dict(ct.known_categories(conn, "Kroger"))[groc] == 2


def test_unlearn_to_zero_removes_the_category(conn):
    groc = _cat(conn, "Groceries")
    ct.learn(conn, "Kroger", "KROGER SLC UT", groc)
    ct.unlearn(conn, "Kroger", "KROGER SLC UT", groc)
    assert ct.known_categories(conn, "Kroger") == []
    assert ct.suggest(conn, "Kroger", "KROGER SLC UT").action == ct.ACTION_LEAVE


def test_forget_payee_clears_only_that_payee(conn):
    groc = _cat(conn, "Groceries")
    ct.learn(conn, "Kroger", "KROGER SLC UT", groc)
    ct.learn(conn, "Costco", "COSTCO WHSE SLC UT", groc)
    ct.forget_payee(conn, "Kroger")
    assert ct.known_categories(conn, "Kroger") == []
    assert ct.known_categories(conn, "Costco") == [(groc, 1)]


def test_learn_ignores_a_blank_category_or_payee(conn):
    groc = _cat(conn, "Groceries")
    ct.learn(conn, "Kroger", "KROGER SLC UT", None)
    ct.learn(conn, "", "SOMETHING", groc)
    assert ct.known_categories(conn, "Kroger") == []
    assert ct.list_payees(conn) == []


def test_payee_summary_reports_the_gate(conn):
    groc = _cat(conn, "Groceries")
    fuel = _cat(conn, "Auto:Fuel")
    for _ in range(9):
        ct.learn(conn, "Kroger", "KROGER SLC UT", groc)
    ct.learn(conn, "Kroger", "KROGER FUEL SLC UT", fuel)
    s = ct.payee_summary(conn, "Kroger")
    assert s["auto_ok"] is True
    assert s["coherence"] == pytest.approx(0.9)
    assert s["categories"][0] == (groc, 9)


def test_a_discriminating_token_beats_an_incoherent_payee(conn):
    """The reason the coherence gate is scoped to the ROOT.

    A payee split evenly between two categories has coherence 0.5 -- below the
    gate -- but that says nothing about a branch the tree can separate cleanly.
    Gating every depth on the payee's overall mix would refuse GAS, the one
    answer the Costco tree is surest of, exactly when the user buys as much fuel
    as groceries.
    """
    fuel = _cat(conn, "Auto:Fuel")
    groc = _cat(conn, "Groceries")
    for _ in range(8):
        ct.learn(conn, "Costco", "COSTCO WHSE #1234 ANYTOWN UT", groc)
        ct.learn(conn, "Costco", "COSTCO GAS #1234 ANYTOWN UT", fuel)

    assert ct.coherence(conn, "Costco") < ct.PAYEE_COHERENCE
    gas = ct.suggest(conn, "Costco", "COSTCO GAS #1234 ANYTOWN UT")
    assert gas.depth > 0
    assert gas.action == ct.ACTION_AUTO and gas.category_id == fuel
    # ...while the ROOT, which has no discriminating token, correctly declines.
    root = ct.suggest(conn, "Costco", "COSTCO WHSE #1234 ANYTOWN UT")
    assert root.depth == 0
    assert root.action == ct.ACTION_DROPDOWN


# ---------------------------------------------------------------------------
# the review path, end to end
# ---------------------------------------------------------------------------
def test_accepting_and_undoing_a_row_leaves_no_vote_behind(tmp_path):
    """An accept the user immediately undoes must not keep voting. Those counts
    ARE the confidence gate, so a stale vote keeps pushing a rejected category."""
    from mammon import import_review

    conn = fresh_db(str(tmp_path / "u.db"))
    acct = ledger.create_account(conn, "Card", "credit")
    groc = _cat(conn, "Groceries")
    entries = import_review.build_review(conn, acct, [
        {"transactionId": "R-1", "postedDate": "2026-08-15", "amount": "42.10",
         "isDebit": True,
         "statementDescription": "COSTCO WHSE #1234 ANYTOWN UT"}])
    import_review.persist_entries(conn, acct, entries)
    e = entries[0]
    txn_id = import_review.save_new(conn, acct, e.mapped, payee="Costco",
                                    category_id=groc, review_id=e.review_id)
    assert ct.known_categories(conn, "Costco") == [(groc, 1)]

    import_review.delete_saved(conn, txn_id, review_id=e.review_id)
    assert ct.known_categories(conn, "Costco") == []
    conn.close()


def test_a_supplied_payee_source_still_trains_the_tree(tmp_path):
    """The Costco card sends its description AS the payee with an empty memo.
    Every earlier category mechanism tokenized the memo and so was structurally
    dead on that account -- it must train here."""
    from mammon import import_review

    conn = fresh_db(str(tmp_path / "s.db"))
    acct = ledger.create_account(conn, "Costco Card", "credit")
    fuel = _cat(conn, "Auto:Fuel")
    for i in range(5):
        entries = import_review.build_review(conn, acct, [
            {"transactionId": f"S-{i}", "postedDate": f"2026-0{i + 1}-10",
             "amount": "50.00", "isDebit": True,
             "payee": "COSTCO GAS #1234 ANYTOWN UT"}])
        import_review.persist_entries(conn, acct, entries)
        e = entries[0]
        import_review.save_new(conn, acct, e.mapped, payee="Costco",
                               category_id=fuel, review_id=e.review_id)
    assert dict(ct.known_categories(conn, "Costco"))[fuel] == 5
    conn.close()


# ---------------------------------------------------------------------------
# the register's category picker
# ---------------------------------------------------------------------------
def test_the_picker_promotes_the_payees_own_categories(tmp_path):
    """The other half of the feature: when the tree is not confident enough to
    fill the cell, the categories this payee HAS carried lead the dropdown and
    the full alphabetical list follows, so nothing becomes unreachable."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from mammon.ui.models import RegisterModel

    conn = fresh_db(str(tmp_path / "p.db"))
    acct = ledger.create_account(conn, "Card", "credit")
    fuel = _cat(conn, "Auto:Fuel")
    groc = _cat(conn, "Groceries")
    _cat(conn, "Aardvark Supplies")          # sorts first alphabetically
    _cat(conn, "Zoo")
    txn = ledger.add_transaction(conn, acct, "2026-08-15", -4210, payee="Costco",
                                 memo="COSTCO GAS #1234 ANYTOWN UT",
                                 category_id=fuel)
    for _ in range(4):
        ct.learn(conn, "Costco", "COSTCO GAS #1234 ANYTOWN UT", fuel)
    ct.learn(conn, "Costco", "COSTCO WHSE #1234 ANYTOWN UT", groc)

    model = RegisterModel(conn, acct)
    row = model.row_for_txn(txn)
    plain = model.category_choices()
    ranked = model.category_choices(row)

    assert ranked[0] == "Auto:Fuel"                    # this row's own shape leads
    assert "Groceries" in ranked[:2]                   # the payee's other category
    assert set(ranked) == set(plain)                   # nothing added or lost
    assert "Aardvark Supplies" in ranked and "Zoo" in ranked
    assert plain[0] != "Auto:Fuel"                     # the plain list is unranked
    conn.close()

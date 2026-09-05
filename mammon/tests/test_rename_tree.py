"""Tests for the online-learning payee rename tree (:mod:`mammon.rename_tree`).

Covers the normalization/overfitting guard (numeric + short noise never becomes a
node), inverse-frequency ranking, the learn walk (absent/matches/differs), the
split choosing the lowest-frequency differentiating token, the confidence
function and its auto/dropdown/leave gating, per-payee applied/overridden tallies,
forgetting a payee, bootstrapping from register history, and the end-to-end
import-review integration (auto-fill, learning on save, restart survival).
"""
from __future__ import annotations

import pytest

from mammon import db, import_review, ledger, rename_tree


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "Anytown CU Checking", "checking")


def _row(desc, *, tid="", amount="12.34", debit=True, date="2026-05-01"):
    return {
        "transactionId": tid,
        "postedDate": date,
        "amount": amount,
        "isDebit": debit,
        "statementDescription": desc,
    }


def _open_in_register(conn, account, row):
    """Build a review row, then run the prediction the REGISTER runs when it opens
    that row for editing -- mirroring ``RegisterModel.set_pending``.

    The rename tree is applied here, not in ``build_review``: the stored review row
    shows the bank's text verbatim, and the suggested payee lives only on the
    editable register row (``predicted_payee``). Any test about what payee the user
    is actually shown therefore has to go through this path.
    """
    mapped = import_review.build_review(conn, account, [row])[0].mapped
    payee, _cat = import_review.predict_fields(conn, mapped)
    mapped.predicted_payee = payee
    return mapped


def _tokens_of(conn, parent_id):
    return {c["token"] for c in rename_tree.children(conn, parent_id)}


def _child(conn, parent_id, token):
    for c in rename_tree.children(conn, parent_id):
        if c["token"] == token:
            return c
    return None


# ---------------------------------------------------------------------------
# normalization / overfitting guard
# ---------------------------------------------------------------------------
def test_normalize_drops_numeric_and_short_noise():
    toks = rename_tree.normalize_tokens("POS DEBIT 1234 AMAZON.COM 55 A BB")
    # pure numbers (1234, 55) and short tokens (A, BB) are dropped as noise.
    assert toks == ["POS", "DEBIT", "AMAZON", "COM"]
    assert not any(t.isdigit() for t in toks)


def test_normalize_dedupes_preserving_order():
    assert rename_tree.normalize_tokens("STARBUCKS STARBUCKS COFFEE") == [
        "STARBUCKS", "COFFEE"]


def test_numeric_noise_never_becomes_a_node(conn):
    # Two visits to the same merchant differ only by a per-transaction number;
    # the number must not overfit into its own node -- both collapse to one node.
    rename_tree.learn(conn, "AMZN MKTP 12345", "Amazon")
    rename_tree.learn(conn, "AMZN MKTP 67890", "Amazon")
    nodes = rename_tree.list_nodes(conn)
    assert len(nodes) == 1                        # a single AMZN node, no number nodes
    assert nodes[0]["token"] == "AMZN"
    assert not any(n["token"].isdigit() for n in nodes)
    payees = rename_tree.node_payees(conn, nodes[0]["id"])
    assert payees == [{"payee": "Amazon", "count": 2}]


# ---------------------------------------------------------------------------
# inverse-frequency ranking
# ---------------------------------------------------------------------------
def _set_stats(conn, rows):
    """Seed the payee snapshot with explicit (token, freq, entropy) rows."""
    conn.execute("DELETE FROM rename_token_freq")
    conn.executemany(
        "INSERT INTO rename_token_freq(token, freq, entropy) VALUES(?,?,?)", rows)
    conn.commit()


def test_ranked_tokens_purest_and_best_supported_first(conn):
    """Ranking is by label ENTROPY, not rarity. Measured on the real ledger,
    rarity was the wrong axis: 'IAT' appears 373 times mapping to 3 payees
    (keep it early) while 'VENMO' appears 282 times mapping to 73 payees (it
    discriminates nothing). Pure tokens sort first, support breaks ties, and a
    high-entropy token sorts last however common or rare it is."""
    _set_stats(conn, [("PURE", 300, 0.0), ("SPREAD", 300, 5.0),
                      ("RAREPURE", 3, 0.0)])
    assert rename_tree.ranked_tokens(conn, "SPREAD PURE RAREPURE") == [
        "PURE", "RAREPURE", "SPREAD"]
    # An unseen token may be a brand-new merchant name: it ties with the
    # exactly-pure tokens and support breaks the tie, so it lands after proven
    # discriminators but ahead of anything with real label spread.
    assert rename_tree.ranked_tokens(conn, "NOVELTOKEN PURE") == [
        "PURE", "NOVELTOKEN"]


def test_ranked_tokens_drop_unseen_mixed_alnum_noise(conn):
    """Mixed letter+digit tokens not seen at least twice are per-transaction ids
    (ISINs, auth codes, masked ids) -- 87% of them occur exactly once in the real
    corpus. The old ranking put them FIRST for being rare, rooting learned paths
    in tokens that never recur."""
    _set_stats(conn, [("DIVIDEND", 30, 0.0), ("US37954Y8066", 5, 0.0)])
    # unseen mixed-alnum ids are dropped entirely...
    assert rename_tree.ranked_tokens(
        conn, "ALTY0X99 DIVIDEND US37954Y8066") == ["DIVIDEND", "US37954Y8066"]
    # ...but a mixed token the snapshot has seen >=2 times is a real word.
    assert "US37954Y8066" in rename_tree.ranked_tokens(conn, "US37954Y8066 FEE")


def test_payee_field_joins_the_evidence(conn):
    """The source's own Payee field is EVIDENCE, not a gate: its tokens join the
    description's in one ranked pool (deduplicated)."""
    toks = rename_tree.ranked_tokens(
        conn, "DIVIDEND EARNED FOR PERIOD", extra="DIVIDEND EARNED VANGUARD")
    assert "VANGUARD" in toks
    assert toks.count("DIVIDEND") == 1


def test_cold_start_ranking_is_positional(conn):
    # With no snapshot every token is frequency 0, so first-appearance order wins.
    assert rename_tree.ranked_tokens(conn, "FIRST SECOND THIRD") == [
        "FIRST", "SECOND", "THIRD"]


# ---------------------------------------------------------------------------
# the learn walk: absent / matches / differs
# ---------------------------------------------------------------------------
def test_absent_token_creates_node_with_count_one(conn):
    assert rename_tree.learn(conn, "NETFLIX COM", "Netflix") is True
    roots = rename_tree.children(conn, None)
    assert len(roots) == 1 and roots[0]["token"] == "NETFLIX"
    assert rename_tree.node_payees(conn, roots[0]["id"]) == [{"payee": "Netflix", "count": 1}]


def test_matching_payee_increments(conn):
    rename_tree.learn(conn, "NETFLIX COM 1", "Netflix")
    rename_tree.learn(conn, "NETFLIX COM 2", "Netflix")
    root = rename_tree.children(conn, None)[0]
    assert rename_tree.node_payees(conn, root["id"]) == [{"payee": "Netflix", "count": 2}]


def test_learn_ignores_blank_payee_and_tokenless_desc(conn):
    assert rename_tree.learn(conn, "AMAZON", "  ") is False
    assert rename_tree.learn(conn, "12 34 5", "Amazon") is False   # all noise
    assert rename_tree.list_nodes(conn) == []


def test_differing_payee_splits_into_child(conn):
    rename_tree.learn(conn, "COSTCO GAS", "Costco Gas")
    rename_tree.learn(conn, "COSTCO WHOLESALE", "Costco")
    roots = rename_tree.children(conn, None)
    assert len(roots) == 1 and roots[0]["token"] == "COSTCO"
    # the first payee stays at the shared node...
    assert rename_tree.node_payees(conn, roots[0]["id"]) == [{"payee": "Costco Gas", "count": 1}]
    # ...and the conflicting one splits off under the differentiating token.
    child = _child(conn, roots[0]["id"], "WHOLESALE")
    assert child is not None and child["depth"] == 2
    assert rename_tree.node_payees(conn, child["id"]) == [{"payee": "Costco", "count": 1}]


def test_tokens_exhausted_adds_second_payee_to_node(conn):
    rename_tree.learn(conn, "PLAZA", "Store A")
    rename_tree.learn(conn, "PLAZA", "Store B")     # same lone token, different payee
    root = rename_tree.children(conn, None)[0]
    payees = {p["payee"] for p in rename_tree.node_payees(conn, root["id"])}
    assert payees == {"Store A", "Store B"}         # both live at the one node


# ---------------------------------------------------------------------------
# the split picks the LOWEST-FREQUENCY differentiating token
# ---------------------------------------------------------------------------
def test_split_picks_the_purest_differentiator(conn):
    # SHARED (pure, well-supported) leads both descriptions; among the rest the
    # pure RARE token out-ranks the high-entropy COMMON one, so the split must
    # descend on RARE, never COMMON.
    _set_stats(conn, [("SHARED", 50, 0.0), ("COMMON", 50, 3.0), ("RARE", 5, 0.0)])
    rename_tree.learn(conn, "SHARED COMMON", "First")
    rename_tree.learn(conn, "SHARED COMMON RARE", "Second")
    shared = rename_tree.children(conn, None)[0]
    assert shared["token"] == "SHARED"
    kids = _tokens_of(conn, shared["id"])
    assert kids == {"RARE"}                          # split on RARE, never COMMON


# ---------------------------------------------------------------------------
# confidence function
# ---------------------------------------------------------------------------
def test_confidence_shallow_many_high_deep_few_low():
    shallow_many = rename_tree.confidence(10, 1)
    deep_few = rename_tree.confidence(1, 5)
    assert shallow_many > 0.8
    assert deep_few < 0.2
    assert shallow_many > deep_few
    # monotonic: more hits raise it, more depth lowers it.
    assert rename_tree.confidence(5, 1) > rename_tree.confidence(2, 1)
    assert rename_tree.confidence(3, 1) > rename_tree.confidence(3, 4)
    assert rename_tree.confidence(0, 1) == 0.0


# ---------------------------------------------------------------------------
# confidence TIER: a node needs >=2 counts before it can be 'high' (issue 7)
# ---------------------------------------------------------------------------
def test_confidence_tier_low_for_single_example():
    # One hit, however shallow, is never 'high' -- a lone prior rename is too
    # weak to auto-apply.
    assert rename_tree.confidence_tier(1, 1) == rename_tree.CONFIDENCE_LOW
    assert rename_tree.confidence_tier(1, 3) == rename_tree.CONFIDENCE_LOW


def test_confidence_tier_high_needs_four_counts():
    # Raised from 2 on measurement: replaying the ledger's renames online, 4
    # corroborating examples cut wrong auto-renames from 5.7% to 1.8% at
    # identical coverage (entropy ranking generalizes better, so the extra
    # examples arrive sooner, not less often).
    assert rename_tree.confidence_tier(2, 1) == rename_tree.CONFIDENCE_LOW
    assert rename_tree.confidence_tier(4, 1) == rename_tree.CONFIDENCE_HIGH
    assert rename_tree.confidence_tier(5, 1) == rename_tree.CONFIDENCE_HIGH
    # Enough hits but so deep it falls below the suggest floor -> still low.
    assert rename_tree.confidence_tier(4, 20) == rename_tree.CONFIDENCE_LOW


def test_suggest_single_example_is_low_confidence(conn):
    rename_tree.learn(conn, "GITHUB", "GitHub")           # one hit
    s = rename_tree.suggest(conn, "GITHUB PRO")
    assert s.action == rename_tree.ACTION_AUTO             # cardinality unchanged
    assert s.high_confidence is False                     # but not trusted yet
    assert s.tier == rename_tree.CONFIDENCE_LOW


def test_suggest_four_examples_is_high_confidence(conn):
    for _ in range(4):
        rename_tree.learn(conn, "GITHUB", "GitHub")
    s = rename_tree.suggest(conn, "GITHUB PRO")
    assert s.action == rename_tree.ACTION_AUTO
    assert s.high_confidence is True
    assert s.tier == rename_tree.CONFIDENCE_HIGH


def test_single_example_shows_raw_statement_description(conn, account):
    # A payee learned from ONE example is low confidence: the register shows the
    # RAW statement description (verbatim), not the (weakly) learned name.
    rename_tree.learn(conn, "POS NETFLIX.COM 88", "Netflix")
    m = _open_in_register(conn, account, _row("POS NETFLIX.COM 88", tid="N1"))
    assert m.predicted_payee == "POS NETFLIX.COM 88"     # raw, not "Netflix"
    assert m.payee == ""                                 # review row left untouched
    assert "Netflix" in m.payee_candidates               # still offered in the dropdown


def test_four_examples_autofill_confident_payee(conn, account):
    for _ in range(4):
        rename_tree.learn(conn, "POS NETFLIX.COM 88", "Netflix")
    m = _open_in_register(conn, account, _row("POS NETFLIX.COM 99", tid="N2"))
    assert m.predicted_payee == "Netflix"
    assert m.payee == ""                                 # review row left untouched


# ---------------------------------------------------------------------------
# suggest: auto / dropdown / leave gating
# ---------------------------------------------------------------------------
def test_suggest_auto_when_single_confident_payee(conn):
    for _ in range(3):                               # 3 hits @ depth 1 -> conf 0.75
        rename_tree.learn(conn, "STARBUCKS COFFEE", "Starbucks")
    s = rename_tree.suggest(conn, "STARBUCKS COFFEE 8891")
    assert s.action == rename_tree.ACTION_AUTO
    assert s.payee == "Starbucks"
    assert s.confidence >= 0.75


def test_suggest_dropdown_when_multiple_payees(conn):
    rename_tree.learn(conn, "PLAZA", "Store A")
    rename_tree.learn(conn, "PLAZA", "Store B")
    s = rename_tree.suggest(conn, "PLAZA 12")
    assert s.action == rename_tree.ACTION_DROPDOWN
    assert set(s.payees) == {"Store A", "Store B"}   # the typeable dropdown options


def test_suggest_auto_for_lone_payee_below_old_threshold(conn):
    # Cardinality -- not a tunable confidence knob -- decides auto vs dropdown.
    # A single payee with one hit scores conf 0.5, which the retired 0.75 auto
    # threshold would have demoted to a dropdown; under the cardinality rule a
    # lone payee above the leave-floor auto-renames.
    rename_tree.learn(conn, "GITHUB", "GitHub")      # 1 hit @ depth 1 -> conf 0.5
    s = rename_tree.suggest(conn, "GITHUB PRO")
    assert s.action == rename_tree.ACTION_AUTO
    assert s.payee == "GitHub"
    assert 0.34 <= s.confidence < 0.75               # would NOT clear the old knob


def test_suggest_leaves_unknown_description(conn):
    rename_tree.learn(conn, "NETFLIX", "Netflix")
    s = rename_tree.suggest(conn, "SOME BRAND NEW MERCHANT")
    assert s.action == rename_tree.ACTION_LEAVE
    assert s.payees == []


def test_suggest_leaves_deep_low_confidence_node(conn):
    # A depth-2, single-hit node scores 1/3 = 0.33, below the suggest floor.
    rename_tree.learn(conn, "ALPHA BETA", "PayeeX")
    rename_tree.learn(conn, "ALPHA GAMMA", "PayeeY")   # splits to depth 2
    s = rename_tree.suggest(conn, "ALPHA GAMMA")
    assert s.action == rename_tree.ACTION_LEAVE


def test_cardinality_not_confidence_flips_auto_to_dropdown(conn):
    # One payee at the node -> AUTO. Adding a SECOND, distinct payee to the SAME
    # node flips it to DROPDOWN purely on cardinality: the node's confidence only
    # RISES (a further hit), so no confidence threshold could explain the flip.
    # A single lone token ("SPOTIFY", the numeric dropped) keeps both payees on
    # the one root node via token exhaustion rather than splitting to a child.
    for _ in range(3):
        rename_tree.learn(conn, "SPOTIFY", "Spotify")
    first = rename_tree.suggest(conn, "SPOTIFY 5")
    assert first.action == rename_tree.ACTION_AUTO
    rename_tree.learn(conn, "SPOTIFY", "Spotify Family")  # 2nd payee, same node
    second = rename_tree.suggest(conn, "SPOTIFY 5")
    assert second.action == rename_tree.ACTION_DROPDOWN
    assert second.confidence >= first.confidence     # confidence did not drop
    assert set(second.payees) == {"Spotify", "Spotify Family"}


def test_suggest_dropdown_for_venmo_cashout_terminal_node(conn):
    # "VENMO CASHOUT" is boilerplate the bank stamps on every cashout regardless
    # of who was paid, so the co-occurring tokens VENMO+CASHOUT are all the tree
    # has. Different payees pile onto the terminal CASHOUT node (the first payee
    # claims the shallower VENMO node alone; subsequent differing payees descend
    # and then exhaust onto CASHOUT). A multi-payee terminal node -> a typeable,
    # frequency-sorted dropdown, never an auto-rename.
    rename_tree.learn(conn, "VENMO CASHOUT", "Alice (rent)")     # -> VENMO node
    rename_tree.learn(conn, "VENMO CASHOUT", "Bob (groceries)")  # -> CASHOUT child
    rename_tree.learn(conn, "VENMO CASHOUT", "Bob (groceries)")  # Bob -> count 2
    rename_tree.learn(conn, "VENMO CASHOUT", "Carol (utils)")    # +Carol at CASHOUT
    s = rename_tree.suggest(conn, "VENMO CASHOUT 9999")
    assert s.action == rename_tree.ACTION_DROPDOWN
    # The terminal CASHOUT node serves two payees, offered most-frequent first
    # (Bob: 2 hits, Carol: 1); the dropdown is user-typeable in the UI.
    assert s.payees == ["Bob (groceries)", "Carol (utils)"]


# ---------------------------------------------------------------------------
# applied / overridden tallies + management listing
# ---------------------------------------------------------------------------
def test_applied_and_overridden_counts(conn):
    rename_tree.note_applied(conn, "Amazon")
    rename_tree.note_applied(conn, "Amazon")
    rename_tree.note_overridden(conn, "Amazon")
    rename_tree.learn(conn, "AMAZON MKTP", "Amazon")
    stats = {r["payee"]: r for r in rename_tree.rename_stats(conn)}
    assert stats["Amazon"]["applied"] == 2
    assert stats["Amazon"]["overridden"] == 1
    assert stats["Amazon"]["examples"] == 1          # one learned hit


def test_forget_payee_prunes_empty_nodes(conn):
    rename_tree.learn(conn, "COSTCO GAS", "Costco Gas")
    rename_tree.learn(conn, "COSTCO WHOLESALE", "Costco")
    rename_tree.note_applied(conn, "Costco")
    removed = rename_tree.forget_payee(conn, "Costco")
    assert removed == 1
    root = rename_tree.children(conn, None)[0]
    assert root["token"] == "COSTCO"                 # the other payee's node survives
    assert rename_tree.children(conn, root["id"]) == []   # emptied child pruned
    assert all(r["payee"] != "Costco" for r in rename_tree.rename_stats(conn))


# ---------------------------------------------------------------------------
# bootstrap from register history
# ---------------------------------------------------------------------------
def test_bootstrap_replays_history_and_is_idempotent(conn, account):
    ledger.add_transaction(conn, account, "2026-01-01", -500,
                           payee="Netflix", memo="POS NETFLIX.COM 111")
    ledger.add_transaction(conn, account, "2026-01-02", -600,
                           payee="Netflix", memo="POS NETFLIX.COM 222")
    ledger.add_transaction(conn, account, "2026-01-03", -700,
                           payee="Netflix", memo="POS NETFLIX.COM 333")
    n = rename_tree.bootstrap(conn)
    assert n == 3
    s = rename_tree.suggest(conn, "POS NETFLIX.COM 999")
    assert s.action == rename_tree.ACTION_AUTO and s.payee == "Netflix"
    # a second call is a no-op (guarded by the meta flag) -- counts do not double.
    assert rename_tree.bootstrap(conn) == 0
    root = rename_tree.children(conn, None)[0]
    total = sum(p["count"] for p in rename_tree.node_payees(conn, root["id"]))
    assert total == 3


def test_bootstrap_skips_transfers(conn):
    a = ledger.create_account(conn, "Checking", "checking")
    b = ledger.create_account(conn, "Savings", "savings")
    ledger.create_transfer(conn, a, b, "2026-01-01", 1000,
                           memo="XFER TO SAVINGS 42", payee="Transfer to Savings")
    assert rename_tree.bootstrap(conn) == 0          # only transfer legs exist
    assert rename_tree.suggest(conn, "XFER TO SAVINGS 42").action == rename_tree.ACTION_LEAVE


# ---------------------------------------------------------------------------
# end-to-end through import_review
# ---------------------------------------------------------------------------
def test_register_autofills_confident_payee(conn, account):
    for _ in range(4):
        rename_tree.learn(conn, "WALMART SUPERCENTER 4455", "Walmart")
    m = _open_in_register(conn, account, _row("WALMART SUPERCENTER 9999"))
    assert m.predicted_payee == "Walmart"


def test_register_offers_dropdown_when_unsure(conn, account):
    # Two payees collide on the same node (>1 cardinality) -> the register offers a
    # typeable dropdown of both rather than committing to one. A single lone token
    # ("SQ" is dropped as short noise) keeps both payees on the one node.
    rename_tree.learn(conn, "SQ COFFEE", "Blue Bottle")
    rename_tree.learn(conn, "SQ COFFEE", "Philz Coffee")
    m = _open_in_register(conn, account, _row("SQ COFFEE 77"))
    assert set(m.payee_candidates) == {"Blue Bottle", "Philz Coffee"}
    # One of them is pre-filled, but both stay on offer in the dropdown.
    assert m.predicted_payee in {"Blue Bottle", "Philz Coffee"}


def test_save_new_learns_a_correction(conn, account):
    first = import_review.build_review(
        conn, account, [_row("POS DEBIT 7788 WALMART SC BENTONVILLE AR", tid="A1")])[0]
    assert first.mapped.payee != "Walmart"           # provisional, unlearned
    import_review.save_new(conn, account, first.mapped, payee="Walmart")
    # the correction is now learned and offered for a similar description.
    s = rename_tree.suggest(conn, "WALMART SC 4455 SPRINGFIELD MO")
    assert "Walmart" in s.payees


def test_default_accept_does_not_learn(conn, account):
    entries = import_review.build_review(
        conn, account, [_row("SOME RANDOM MERCHANT LLC", tid="R1")])
    import_review.save_new(conn, account, entries[0].mapped)     # no payee override
    assert rename_tree.list_nodes(conn) == []
    assert rename_tree.rename_stats(conn) == []


def test_kept_autofill_tallies_applied(conn, account):
    for _ in range(4):
        rename_tree.learn(conn, "NETFLIX COM", "Netflix")
    m = _open_in_register(conn, account, _row("NETFLIX COM 12", tid="N9"))
    assert m.predicted_payee == "Netflix"            # auto-filled
    import_review.save_new(conn, account, m)                     # kept as-is
    stats = {r["payee"]: r for r in rename_tree.rename_stats(conn)}
    assert stats["Netflix"]["applied"] == 1


def test_overriding_autofill_tallies_overridden_and_learns(conn, account):
    for _ in range(4):
        rename_tree.learn(conn, "AMAZON MKTP", "Amazon")
    m = _open_in_register(conn, account, _row("AMAZON MKTP 5", tid="Z1"))
    assert m.predicted_payee == "Amazon"             # auto-filled
    import_review.save_new(conn, account, m, payee="Amazon Prime")
    stats = {r["payee"]: r for r in rename_tree.rename_stats(conn)}
    assert stats["Amazon"]["overridden"] == 1
    assert stats["Amazon Prime"]["examples"] >= 1    # the correction was learned


def test_transfer_row_never_learns(conn, account):
    entry = import_review.build_review(
        conn, account, [_row("SHARE TRANSFER FROM SHARE ACCOUNT: 0123", tid="T1")])[0]
    assert entry.mapped.is_transfer is True
    import_review.save_new(conn, account, entry.mapped, payee="My Savings")
    assert rename_tree.list_nodes(conn) == []


def test_learning_survives_restart(tmp_path):
    path = tmp_path / "persist.db"
    c1 = db.init_db(path)
    acct = ledger.create_account(c1, "Checking", "checking")
    for i in range(3):
        first = import_review.build_review(
            c1, acct, [_row("POS DEBIT SPOTIFY USA 555", tid="S%d" % i)])[0]
        import_review.save_new(c1, acct, first.mapped, payee="Spotify")
    c1.close()

    c2 = db.init_db(path)                             # reopen the same file
    s = rename_tree.suggest(c2, "SPOTIFY USA NEW YORK NY")
    assert "Spotify" in s.payees
    c2.close()


# ---------------------------------------------------------------------------
# the ACTION domain: the same tree, mapping activity text -> Quicken action
# ---------------------------------------------------------------------------
def test_action_domain_is_isolated_from_the_payee_tree(conn):
    rename_tree.learn(conn, "USD CREDIT INTEREST", "IntInc", kind="action")
    rename_tree.learn(conn, "USD CREDIT INTEREST PAYMENT", "Interest Co", kind="payee")
    assert rename_tree.suggest(conn, "USD CREDIT INTEREST", kind="action").payee == "IntInc"
    assert rename_tree.list_nodes(conn, kind="action") != []
    assert rename_tree.list_nodes(conn, kind="payee") != []
    # Clearing one tree leaves the other standing.
    rename_tree.clear(conn, kind="action")
    assert rename_tree.list_nodes(conn, kind="action") == []
    assert rename_tree.list_nodes(conn, kind="payee") != []
    assert rename_tree.suggest(conn, "USD CREDIT INTEREST", kind="action").action \
        == rename_tree.ACTION_LEAVE


def test_action_tree_generalizes_across_securities(conn):
    """The point of the shape filter: an IB dividend line embeds a per-security
    ISIN, and the old rarity ranking would have rooted each learned path in it --
    unreachable for every other security. Filtered, the tree roots in the
    dividend vocabulary and a NEVER-SEEN security maps correctly."""
    conn.execute("INSERT INTO accounts(name, type) VALUES ('IB', 'investment')")
    aid = conn.execute("SELECT id FROM accounts WHERE name='IB'").fetchone()["id"]
    for i, desc in enumerate((
        "ALTY(US37954Y8066) Cash Dividend USD 0.079 per Share (Ordinary Dividend)",
        "QTUM(US26924G8134) Cash Dividend USD 0.11 per Share (Ordinary Dividend)",
        "PSCT(US73936T6688) Cash Dividend USD 0.06 per Share (Ordinary Dividend)",
        "ALTY(US37954Y8066) Cash Dividend USD 0.081 per Share (Ordinary Dividend)",
    )):
        conn.execute(
            "INSERT INTO investment_transactions(account_id,date,action,amount,memo)"
            " VALUES (?,?,?,?,?)", (aid, "2026-01-0%d" % (i + 1), "Div", 100, desc))
    conn.commit()
    # Bootstrap snapshots the token stats FIRST (as app startup does), so the
    # shared dividend vocabulary out-ranks the per-security ticker tokens.
    assert rename_tree.bootstrap_actions(conn) == 4
    s = rename_tree.suggest(
        conn, "SCHD(US78468R6633) Cash Dividend USD 0.25 per Share (Ordinary Dividend)",
        kind="action")
    assert s.action == rename_tree.ACTION_AUTO
    assert s.payee == "Div"
    assert s.high_confidence          # action domain min_count is 2


def test_bootstrap_actions_replays_investment_history(conn):
    conn.execute(
        "INSERT INTO accounts(name, type) VALUES ('B', 'investment')")
    aid = conn.execute("SELECT id FROM accounts WHERE name='B'").fetchone()["id"]
    for i in range(3):
        conn.execute(
            "INSERT INTO investment_transactions(account_id,date,action,amount,memo)"
            " VALUES (?,?,?,?,?)",
            (aid, "2026-01-0%d" % (i + 1), "IntInc", 100,
             "USD Credit Interest for Month-%d" % i))
    conn.commit()
    n = rename_tree.bootstrap_actions(conn)
    assert n == 3
    assert rename_tree.bootstrap_actions(conn) == 0          # idempotent
    s = rename_tree.suggest(conn, "USD Credit Interest for Month-9", kind="action")
    assert (s.action, s.payee) == (rename_tree.ACTION_AUTO, "IntInc")


def test_dominated_node_recovers_auto_after_one_stray(conn):
    """The purity gate: under the cardinality rule one stray learn at a busy
    node demoted it to a dropdown forever. A node whose top label is outvoting
    the stray 9+:1 auto-applies again."""
    for _ in range(9):
        rename_tree.learn(conn, "COSTCO WHOLESALE", "Costco")
    rename_tree.learn(conn, "COSTCO", "Costco Gas")   # the stray, same node
    s = rename_tree.suggest(conn, "COSTCO WHOLESALE 445")
    assert s.action == rename_tree.ACTION_AUTO
    assert s.payee == "Costco"
    assert s.high_confidence
    # A genuinely contested node (under the purity threshold) -> dropdown,
    # both offered.
    for _ in range(3):
        rename_tree.learn(conn, "COSTCO", "Costco Gas")
    s = rename_tree.suggest(conn, "COSTCO WHOLESALE 445")
    assert s.action == rename_tree.ACTION_DROPDOWN
    assert set(s.payees) == {"Costco", "Costco Gas"}


def test_supplied_payee_loses_only_to_a_high_confidence_rename(conn, account):
    """The payee FIELD is evidence, not a gate. Unlearned, it stands verbatim
    (the old behavior); once the user has corrected the same evidence
    min_count times, the learned rename wins -- the failure being fixed was a
    truncated field ('Dividend Earned For Period O') standing forever."""
    raw = "Dividend Earned For Period O"
    desc = "DIVIDEND EARNED FOR PERIOD OF 08/01/2026 THRU 08/31/2026"

    def row(tid):
        return _row(desc, tid=tid) | {}

    # Unlearned: verbatim.
    e = import_review.build_review(conn, account, [_row(desc, tid="D0")])[0]
    e.mapped.payee = raw
    e.mapped.payee_supplied = True
    p, _cat = import_review.predict_fields(conn, e.mapped)
    assert p == raw

    for i in range(4):
        rename_tree.learn(conn, desc, "Dividend", extra=raw)
    p, _cat = import_review.predict_fields(conn, e.mapped)
    assert p == "Dividend"


def test_ranking_change_rebuilds_the_trees_from_history(conn, account):
    """A trie's paths are laid down in ranking order and walked in ranking
    order, so a tree built under one ranking is silently unreachable under
    another -- after the entropy rewrite, a real ledger's payee tree answered
    LEAVE for descriptions it had learned hundreds of times. ensure_bootstrapped
    detects a ranking-version change and rebuilds from history, once."""
    for i in range(4):
        ledger.add_transaction(conn, account, "2026-01-0%d" % (i + 1), -10_00,
                               payee="Netflix", memo="POS NETFLIX.COM 88")
    rename_tree.ensure_bootstrapped(conn)
    assert rename_tree.suggest(conn, "POS NETFLIX.COM 99").payee == "Netflix"

    # Simulate a tree left over from an older ranking algorithm.
    conn.execute("UPDATE rename_meta SET value='0' WHERE key='ranking_version'")
    conn.commit()
    n = rename_tree.ensure_bootstrapped(conn)
    assert n >= 4                                   # rebuilt, not skipped
    assert rename_tree.suggest(conn, "POS NETFLIX.COM 99").payee == "Netflix"
    assert rename_tree.ensure_bootstrapped(conn) == 0    # and only once

"""Tests for the rebuilt-per-query decision-tree renamer (:mod:`mammon.rename_tree`).

Covers tokenization and the noise guards, tree construction (split choice,
tie-breaks, contested leaves), the four steps of ``suggest`` (candidates,
tree, leaf matching, fill-or-offer), the corrections-only corpus, LIVE labels
(register edit, undo, forget), survival of review retention, the applied /
overridden tallies, the action domain, bootstrap, the v60 migration seed, and
the end-to-end behaviour through ``import_review`` that the user specified:
the raw text shows until the same text has been corrected twice, then the
rename fills; a leaf with several payees is a dropdown, never a fill.
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


_TID = [0]


def _open(conn, account, desc, **kw):
    """Build + persist a review row, then run the prediction the REGISTER runs
    when it opens the row (``RegisterModel.set_pending``). Returns the entry
    with ``predicted_payee`` set. Every row gets its own amount so it classifies
    NEW rather than matching a row accepted earlier."""
    _TID[0] += 1
    n = _TID[0]
    kw.setdefault("amount", "%d.%02d" % (10 + n // 100, n % 100))
    row = _row(desc, tid=kw.pop("tid", "T%d" % n), **kw)
    entry = import_review.build_review(conn, account, [row])[0]
    import_review.persist_entries(conn, account, [entry])
    payee, _cat = import_review.predict_fields(conn, entry.mapped)
    entry.mapped.predicted_payee = payee
    return entry


def _accept(conn, account, desc, payee=None, **kw):
    """Open a row and accept it, with ``payee`` typed (None = kept as shown)."""
    entry = _open(conn, account, desc, **kw)
    txn_id = import_review.save_new(conn, account, entry.mapped, payee=payee,
                                    review_id=entry.review_id)
    return entry, txn_id


def _ex(id_, text, label, extra=""):
    return rename_tree.make_example(id_, text, label, extra)


# ---------------------------------------------------------------------------
# tokenization
# ---------------------------------------------------------------------------
def test_normalize_drops_numeric_and_short_noise():
    toks = rename_tree.normalize_tokens("POS DEBIT 1234 AMAZON.COM 55 A BB")
    assert toks == ["POS", "DEBIT", "AMAZON", "COM"]


def test_normalize_dedupes_preserving_order():
    assert rename_tree.normalize_tokens("STARBUCKS STARBUCKS COFFEE") == [
        "STARBUCKS", "COFFEE"]


def test_tidy_text_title_cases_shouting_feeds_only():
    assert rename_tree.tidy_text("AUTOMATIC WITHDRAWAL,  POS  PMT") == "Automatic Withdrawal, POS Pmt"
    assert rename_tree.tidy_text("Amazon web services") == "Amazon web services"
    assert rename_tree.tidy_text("   ") == ""


def test_is_correction_recognises_every_kept_default():
    memo = "AUTOMATIC WITHDRAWAL, CH DONATION WEB (S)"
    assert rename_tree.is_correction("Church", memo) is True
    assert rename_tree.is_correction(memo, memo) is False                    # raw kept
    assert rename_tree.is_correction(rename_tree.tidy_text(memo), memo) is False  # tidied kept
    assert rename_tree.is_correction("Jane Doe", "PAYMENT 1", "Jane Doe") is False  # supplied kept
    assert rename_tree.is_correction("  ", memo) is False


def test_unseen_mixed_alnum_tokens_are_dropped(conn):
    """A letter+digit token fewer than two examples carry is a per-row id
    (auth code, ISIN, masked account) and never a feature -- but one that
    recurs is a real word."""
    rename_tree.learn(conn, "ALTY(US37954Y8066) Cash Dividend", "Div", kind="action")
    rename_tree.learn(conn, "ALTY(US37954Y8066) Cash Dividend", "Div", kind="action")
    rename_tree.learn(conn, "QTUM(US26924G8134) Cash Dividend", "Div", kind="action")
    ex = {e["text"]: e["tokens"] for e in rename_tree.examples(conn, kind="action")}
    assert "US37954Y8066" in ex["ALTY(US37954Y8066) Cash Dividend"]
    assert "US26924G8134" not in ex["QTUM(US26924G8134) Cash Dividend"]


# ---------------------------------------------------------------------------
# the tree
# ---------------------------------------------------------------------------
def test_build_tree_splits_on_the_token_that_discriminates():
    """The Venmo case: CASHOUT is in every deposit and no payment, so it is the
    split -- not AUTOMATIC, which both carry and the old trie descended on."""
    ex = [_ex(1, "AUTOMATIC DEPOSIT, VENMO CASHOUT PPD", "Standard Deposit"),
          _ex(2, "AUTOMATIC DEPOSIT, VENMO CASHOUT PPD", "Standard Deposit"),
          _ex(3, "AUTOMATIC WITHDRAWAL, VENMO PAYMENT WEB (S)", "Venmo")]
    root = rename_tree.build_tree(ex)
    assert root.token == "CASHOUT"
    assert root.present.is_leaf and set(root.present.labels) == {"Standard Deposit"}
    assert root.absent.is_leaf and set(root.absent.labels) == {"Venmo"}


def test_build_tree_prefers_a_merchant_token_over_boilerplate_on_a_tie():
    # DEPOSIT / WITHDRAWAL (boilerplate) separate these as perfectly as GAS
    # (a real word); the real word wins the tie.
    ex = [_ex(1, "AUTOMATIC WITHDRAWAL COSTCO GAS", "Costco Gas"),
          _ex(2, "AUTOMATIC DEPOSIT COSTCO", "Costco")]
    assert rename_tree.build_tree(ex).token == "GAS"


def test_identical_texts_with_two_labels_make_a_contested_leaf():
    ex = [_ex(1, "MOBILE DEPOSIT", "Alice"), _ex(2, "MOBILE DEPOSIT", "Bob")]
    root = rename_tree.build_tree(ex)
    assert root.is_leaf and set(root.labels) == {"Alice", "Bob"}


def test_single_label_is_a_leaf_without_splitting():
    ex = [_ex(1, "NETFLIX COM 1", "Netflix"), _ex(2, "NETFLIX COM 2", "Netflix")]
    assert rename_tree.build_tree(ex).is_leaf


# ---------------------------------------------------------------------------
# the pattern: generalized over a label's examples like an array selector
# ---------------------------------------------------------------------------
def test_generalize_keeps_agreed_values_binarizes_varying_ones_drops_missing():
    ANY = rename_tree.ANY
    one = rename_tree.generalize([_ex(1, "June rent", "Tenant")])
    assert one == {"has:JUNE": "JUNE", "has:RENT": "RENT", "next:JUNE": "RENT",
                   "prev:RENT": "JUNE", "f:none": "f:none"}
    two = rename_tree.generalize([_ex(1, "June rent", "Tenant"),
                                  _ex(2, "July rent", "Tenant")])
    assert two == {"has:RENT": "RENT", "prev:RENT": ANY, "f:none": "f:none"}
    three = rename_tree.generalize([_ex(1, "June rent", "Tenant"),
                                    _ex(2, "July rent", "Tenant"),
                                    _ex(3, "rent", "Tenant")])
    assert three == {"has:RENT": "RENT", "f:none": "f:none"}


def test_fits_requires_every_pattern_feature():
    from mammon.rename_tree import features, fits
    pattern = rename_tree.generalize([_ex(1, "June rent", "Tenant"),
                                      _ex(2, "July rent", "Tenant")])
    assert fits(pattern, features(("AUGUST", "RENT")))
    assert not fits(pattern, features(("RENT",)))                 # nothing before RENT
    assert not fits(pattern, features(("AUGUST", "RENT"), ("JAMIE", "CHEN")))  # a field appeared
    assert not fits(pattern, features(("RENT", "AND", "UTILITIES")))          # RENT leads: nothing before it
    assert fits(pattern, features(("LATE", "AUGUST", "RENT", "PLUS", "FEES")))


# ---------------------------------------------------------------------------
# suggest: fill after two corrections, never after one
# ---------------------------------------------------------------------------
def test_one_example_is_neither_filled_nor_offered(conn):
    rename_tree.learn(conn, "POS NETFLIX.COM 88", "Netflix")
    s = rename_tree.suggest(conn, "POS NETFLIX.COM 99")
    assert s.action == rename_tree.ACTION_LEAVE
    assert s.payees == []


def test_two_examples_fill(conn):
    for _ in range(rename_tree.MIN_FILL):
        rename_tree.learn(conn, "POS NETFLIX.COM 88", "Netflix")
    s = rename_tree.suggest(conn, "POS NETFLIX.COM 99")
    assert (s.action, s.payee, s.high_confidence) == (rename_tree.ACTION_AUTO, "Netflix", True)
    assert s.tier == rename_tree.CONFIDENCE_HIGH
    assert s.payees == ["Netflix"]


def test_contested_leaf_is_a_dropdown_not_a_fill(conn):
    """The user's rule: a leaf with several payees goes in the dropdown."""
    for _ in range(3):
        rename_tree.learn(conn, "MOBILE DEPOSIT", "Alice")
    rename_tree.learn(conn, "MOBILE DEPOSIT", "Church")
    rename_tree.learn(conn, "MOBILE DEPOSIT", "Church")
    s = rename_tree.suggest(conn, "MOBILE DEPOSIT")
    assert s.action == rename_tree.ACTION_DROPDOWN
    assert s.payees == ["Alice", "Church"]          # leaf labels, most first
    assert s.high_confidence is False


def test_contested_leaf_fills_once_one_payee_outvotes_the_rest(conn):
    """...unless the leading payee holds FILL_PURITY of the leaf: nine
    Citibank rows outvote one 'Citi Bank' typo, five to one does not."""
    for _ in range(9):
        rename_tree.learn(conn, "AUTOMATIC WITHDRAWAL, CITI AUTOPAY PAYMENT WEB (R)", "Citibank")
    rename_tree.learn(conn, "AUTOMATIC WITHDRAWAL, CITI AUTOPAY PAYMENT WEB (R)", "Citi Bank")
    s = rename_tree.suggest(conn, "AUTOMATIC WITHDRAWAL, CITI AUTOPAY PAYMENT WEB (R)")
    assert (s.action, s.payee) == (rename_tree.ACTION_AUTO, "Citibank")
    for _ in range(5):
        rename_tree.learn(conn, "MOBILE DEPOSIT", "Alice")
    rename_tree.learn(conn, "MOBILE DEPOSIT", "Church")
    s = rename_tree.suggest(conn, "MOBILE DEPOSIT")
    assert s.action == rename_tree.ACTION_DROPDOWN and s.payees == ["Alice"]


def test_dropdown_omits_a_payee_seen_once(conn):
    for _ in range(2):
        rename_tree.learn(conn, "MOBILE DEPOSIT", "Alice")
    rename_tree.learn(conn, "MOBILE DEPOSIT", "Church")
    s = rename_tree.suggest(conn, "MOBILE DEPOSIT")
    assert s.action == rename_tree.ACTION_DROPDOWN
    assert s.payees == ["Alice"]                   # Church: one sighting


def test_all_boilerplate_text_matches_by_exact_token_set(conn):
    """``MOBILE DEPOSIT`` has no merchant token at all, so it finds its own
    history by whole-set equality -- and nothing else does."""
    for _ in range(2):
        rename_tree.learn(conn, "MOBILE DEPOSIT", "Alice")
    assert rename_tree.suggest(conn, "MOBILE DEPOSIT").payee == "Alice"
    assert rename_tree.suggest(conn, "MOBILE BANKING FUNDS TRANSFER").action == rename_tree.ACTION_LEAVE
    assert rename_tree.suggest(conn, "DEPOSIT").action == rename_tree.ACTION_LEAVE


def test_unrelated_text_matches_nothing(conn):
    for _ in range(3):
        rename_tree.learn(conn, "AUTOMATIC WITHDRAWAL, CHASE CREDIT CRDAUTOPAY PPD", "Chase")
    s = rename_tree.suggest(conn, "BILL PAYMENT, COMCAST ONLINE PMTWEB (S)")
    assert s.action == rename_tree.ACTION_LEAVE and s.payees == []


# ---------------------------------------------------------------------------
# suggest: the leaf's pattern must fit the row (step 3)
# ---------------------------------------------------------------------------
def test_a_pattern_learned_from_one_shape_stays_exact(conn):
    """VENMO PAYMENT shares VENMO with the CASHOUT examples, walks into their
    leaf, and must not inherit it: every CASHOUT example carries CASHOUT, so
    the pattern requires it."""
    for _ in range(3):
        rename_tree.learn(conn, "AUTOMATIC DEPOSIT, VENMO CASHOUT PPD", "Standard Deposit")
    s = rename_tree.suggest(conn, "AUTOMATIC WITHDRAWAL, VENMO PAYMENT WEB (S)")
    assert s.action == rename_tree.ACTION_DROPDOWN
    assert s.payees == ["Standard Deposit"]        # offered, one click away
    # ...and once the payment text has its own two corrections, both fill.
    for _ in range(2):
        rename_tree.learn(conn, "AUTOMATIC WITHDRAWAL, VENMO PAYMENT WEB (S)", "Venmo")
    assert rename_tree.suggest(conn, "AUTOMATIC WITHDRAWAL, VENMO PAYMENT WEB (S)").payee == "Venmo"
    s = rename_tree.suggest(conn, "AUTOMATIC DEPOSIT, VENMO CASHOUT PPD")
    assert (s.action, s.payee) == (rename_tree.ACTION_AUTO, "Standard Deposit")
    assert s.payees == ["Standard Deposit", "Venmo"]   # the fill leads its dropdown


def test_identical_examples_do_not_generalize_on_shared_town_tokens(conn):
    """The town-name failure. A youth theater in Anytown shares ANYTOWN
    with Walmart's rows and nothing else; the tree lands on the
    Walmart leaf, whose pattern -- learned from identical rows -- requires
    SUPERCENTER, and the theater does not fit."""
    for _ in range(3):
        rename_tree.learn(conn, "WM SUPERCENTER ANYTOWN UT", "Walmart")
    s = rename_tree.suggest(conn, "STRDEVNT-ANYTOWN YOUTH THEATER UT")
    assert s.action == rename_tree.ACTION_DROPDOWN
    assert s.payees == ["Walmart"]
    # A genuine Walmart row (numbers differ) fits.
    assert rename_tree.suggest(conn, "WM SUPERCENTER 1234 ANYTOWN UT").payee == "Walmart"


def test_a_slot_that_varies_becomes_any_token(conn):
    """The user's rule, in the array-selector sense: once the examples have
    shown variation in a slot, that slot accepts anything. Two Springfield
    rows do not fit Ozark; Springfield plus Ozark fits Nixa."""
    for _ in range(2):
        rename_tree.learn(conn, "WALMART SC 4455 SPRINGFIELD MO", "Walmart")
    s = rename_tree.suggest(conn, "WALMART SC 9999 OZARK MO")
    assert s.action == rename_tree.ACTION_DROPDOWN and s.payees == ["Walmart"]
    rename_tree.learn(conn, "WALMART SC 9999 OZARK MO", "Walmart")
    assert rename_tree.suggest(conn, "WALMART SC 1111 OZARK MO").payee == "Walmart"
    assert rename_tree.suggest(conn, "WALMART SC 7777 NIXA MO").payee == "Walmart"
    assert rename_tree.suggest(conn, "WALMART").action == rename_tree.ACTION_DROPDOWN


def test_the_rent_scenario(conn):
    """The user's worked example. June and July rent renamed Tenant generalize
    to 'a token, then RENT': August rent fits, a bare 'rent' does not; once
    'rent' is named too the slot is dropped and anything with RENT fits. A
    different counterparty with the same note never fits -- the payee field's
    features are part of the pattern."""
    for note in ("June rent", "July rent"):
        rename_tree.learn(conn, note, "Tenant", extra="Jamie Chen")
    s = rename_tree.suggest(conn, "August rent", extra="Jamie Chen")
    assert (s.action, s.payee) == (rename_tree.ACTION_AUTO, "Tenant")
    s = rename_tree.suggest(conn, "rent", extra="Jamie Chen")
    assert s.action == rename_tree.ACTION_DROPDOWN and s.payees == ["Tenant"]
    rename_tree.learn(conn, "rent", "Tenant", extra="Jamie Chen")
    assert rename_tree.suggest(conn, "rent for October", extra="Jamie Chen").payee == "Tenant"
    assert rename_tree.suggest(conn, "dinner", extra="Jamie Chen").action == rename_tree.ACTION_DROPDOWN
    s = rename_tree.suggest(conn, "June rent", extra="Bob Smith")
    assert s.action == rename_tree.ACTION_DROPDOWN and s.payees == ["Tenant"]


def test_a_label_with_several_formats_is_fitted_by_shape(conn):
    """Amazon's marketplace and order lines share no merchant token, so the
    label's pattern alone would pin nothing and fit anything. The examples
    are split by shape first, and a row is fitted against the format it
    resembles."""
    for _ in range(2):
        rename_tree.learn(conn, "AMZN MKTP US", "Amazon")
        rename_tree.learn(conn, "AMAZON.COM ORDER", "Amazon")
    assert rename_tree.suggest(conn, "AMZN MKTP US 9").payee == "Amazon"
    assert rename_tree.suggest(conn, "AMAZON.COM ORDER 5").payee == "Amazon"
    s = rename_tree.suggest(conn, "AMAZON PRIME VIDEO")
    assert s.action == rename_tree.ACTION_DROPDOWN and s.payees == ["Amazon"]


def test_a_token_shared_by_too_many_payees_is_not_evidence(conn):
    """A card feed's town name reaches every merchant in it; sharing it with an
    example says nothing about which merchant this is."""
    names = ["Subway", "OReilly", "Taco Bell", "Zupas", "Maverik", "Smiths"]
    for name in names:
        for _ in range(2):
            rename_tree.learn(conn, f"{name.upper().replace(' ', '')} ANYTOWN UT", name)
    s = rename_tree.suggest(conn, "NEWPLACE ANYTOWN UT")
    assert s.action == rename_tree.ACTION_LEAVE
    # The merchant's own token still identifies it.
    assert rename_tree.suggest(conn, "ZUPAS ANYTOWN UT").payee == "Zupas"


def test_the_supplied_payee_field_is_evidence(conn):
    """A source that sends its description in the payee column (the Costco
    card) trains and matches through ``extra``."""
    for _ in range(2):
        rename_tree.learn(conn, "", "Costco", extra="COSTCO WHSE #1234")
    s = rename_tree.suggest(conn, "", extra="COSTCO WHSE #5678")
    assert (s.action, s.payee) == (rename_tree.ACTION_AUTO, "Costco")


def test_confidence_shallow_many_high_deep_few_low():
    assert rename_tree.confidence(10, 1) > rename_tree.confidence(1, 5)
    assert rename_tree.confidence(5, 1) > rename_tree.confidence(2, 1)
    assert rename_tree.confidence(3, 1) > rename_tree.confidence(3, 4)
    assert rename_tree.confidence(0, 1) == 0.0


# ---------------------------------------------------------------------------
# the corpus: corrections only, live labels
# ---------------------------------------------------------------------------
def test_a_kept_default_is_not_a_correction(conn, account):
    """Accepting the row as shown -- the title-cased bank text -- teaches
    nothing. The old engine learned that text as a payee, and it was the
    'name I never entered' the user then saw."""
    memo = "AUTOMATIC WITHDRAWAL, CH DONATION WEB (S)"
    entry, _ = _accept(conn, account, memo)          # kept as shown
    assert entry.mapped.predicted_payee == rename_tree.tidy_text(memo)
    assert rename_tree.examples(conn) == []
    assert rename_tree.rename_stats(conn) == []
    _accept(conn, account, memo, payee="Church")
    assert [e["label"] for e in rename_tree.examples(conn)] == ["Church"]


def test_kept_supplied_payee_is_not_a_correction(conn):
    rename_tree.learn(conn, "PAYMENT 1", "Jane Doe", extra="Jane Doe")
    rename_tree.learn(conn, "PAYMENT 2", "Jane Doe", extra="Jane Doe")
    assert rename_tree.examples(conn) == []


def test_label_is_read_live_from_the_register(conn, account):
    """A payee edited in the register is the correction of a correction."""
    memo = "AUTOMATIC WITHDRAWAL, ENBRIDGE GAS QGC PPD"
    _, t1 = _accept(conn, account, memo, payee="Enbridge")
    _, t2 = _accept(conn, account, memo, payee="Enbridge")
    assert rename_tree.suggest(conn, memo).payee == "Enbridge"
    for tid in (t1, t2):
        ledger.update_transaction(conn, tid, payee="Enbridge Gas")
    assert rename_tree.suggest(conn, memo).payee == "Enbridge Gas"
    # A payee cleared in the register withdraws the example.
    ledger.update_transaction(conn, t2, payee="")
    assert rename_tree.suggest(conn, memo).action == rename_tree.ACTION_LEAVE


def test_undoing_an_accept_withdraws_its_example(conn, account):
    memo = "AUTOMATIC WITHDRAWAL, CITI AUTOPAY PAYMENT WEB (R)"
    e1, t1 = _accept(conn, account, memo, payee="Citibank")
    e2, t2 = _accept(conn, account, memo, payee="Citibank")
    assert rename_tree.suggest(conn, memo).action == rename_tree.ACTION_AUTO
    import_review.delete_saved(conn, t2, e2.review_id)
    assert len(rename_tree.examples(conn)) == 1
    assert rename_tree.suggest(conn, memo).action == rename_tree.ACTION_LEAVE


def test_forget_payee_removes_its_examples_and_stats(conn, account):
    memo = "AUTOMATIC WITHDRAWAL, CHASE CREDIT CRDAUTOPAY PPD"
    for _ in range(2):
        _accept(conn, account, memo, payee="Chase")
    _accept(conn, account, memo)                       # kept the fill -> applied
    assert rename_tree.rename_stats(conn) == [
        {"payee": "Chase", "examples": 3, "applied": 1, "overridden": 0}]
    assert rename_tree.forget_payee(conn, "Chase") == 3
    assert rename_tree.examples(conn) == []
    assert rename_tree.rename_stats(conn) == []
    assert rename_tree.suggest(conn, memo).action == rename_tree.ACTION_LEAVE


def test_examples_survive_review_retention(conn, account):
    """The reason the corpus is its own table: review rows are purged after
    three batches / a year, and an annual bill must not be forgotten with
    them."""
    memo = "AUTOMATIC WITHDRAWAL, ANYTOWN CITY CUST PMTS PPD"
    batch = import_review.start_batch(conn, account)
    for i in range(2):
        entry = _open(conn, account, memo)
        conn.execute("UPDATE review_items SET batch_id=? WHERE id=?", (batch, entry.review_id))
        import_review.save_new(conn, account, entry.mapped, payee="Anytown City",
                               review_id=entry.review_id)
    conn.execute("UPDATE import_batches SET created_at='2020-01-01 00:00:00'")
    conn.commit()
    for _ in range(3):
        import_review.start_batch(conn, account)
    assert import_review.purge_old_batches(conn, account, keep=1, keep_days=1) == 2
    assert conn.execute("SELECT COUNT(*) FROM review_items").fetchone()[0] == 0
    assert rename_tree.suggest(conn, memo).payee == "Anytown City"


def test_learn_ignores_blank_label_and_tokenless_text(conn):
    assert rename_tree.learn(conn, "AMAZON", "  ") is False
    assert rename_tree.learn(conn, "12 34 5", "Amazon") is False
    assert rename_tree.examples(conn) == []


def test_sources_for_returns_the_bank_text_behind_a_payee(conn):
    rename_tree.learn(conn, "POS NETFLIX.COM 88", "Netflix")
    rename_tree.learn(conn, "NETFLIX.COM AMSTERDAM", "Netflix")
    rename_tree.learn(conn, "", "Costco", extra="COSTCO WHSE #1234")
    assert rename_tree.sources_for(conn, "Netflix") == ["NETFLIX.COM AMSTERDAM", "POS NETFLIX.COM 88"]
    assert rename_tree.sources_for(conn, "Costco") == ["COSTCO WHSE #1234"]


# ---------------------------------------------------------------------------
# applied / overridden tallies
# ---------------------------------------------------------------------------
def test_overriding_a_fill_tallies_overridden_and_learns(conn, account):
    memo = "AMAZON MKTP US 5"
    for _ in range(2):
        _accept(conn, account, memo, payee="Amazon")
    entry = _open(conn, account, memo)
    assert entry.mapped.predicted_payee == "Amazon"
    import_review.save_new(conn, account, entry.mapped, payee="Amazon Prime",
                           review_id=entry.review_id)
    stats = {r["payee"]: r for r in rename_tree.rename_stats(conn)}
    assert stats["Amazon"]["overridden"] == 1
    assert stats["Amazon Prime"]["examples"] == 1
    # The same text now carries two payees: a dropdown, never a fill.
    entry = _open(conn, account, memo)
    assert entry.mapped.predicted_payee == rename_tree.tidy_text(memo)
    assert entry.mapped.payee_candidates == ["Amazon"]   # Prime: one sighting


def test_picking_a_dropdown_candidate_tallies_applied(conn, account):
    for _ in range(2):
        rename_tree.learn(conn, "MOBILE DEPOSIT", "Alice")
        rename_tree.learn(conn, "MOBILE DEPOSIT", "Church")
    entry = _open(conn, account, "MOBILE DEPOSIT")
    assert set(entry.mapped.payee_candidates) == {"Alice", "Church"}
    import_review.save_new(conn, account, entry.mapped, payee="Church",
                           review_id=entry.review_id)
    stats = {r["payee"]: r for r in rename_tree.rename_stats(conn)}
    assert stats["Church"]["applied"] == 1


# ---------------------------------------------------------------------------
# end-to-end through import_review: the user's specification
# ---------------------------------------------------------------------------
def test_raw_text_shows_until_corrected_twice_then_fills(conn, account):
    memo = "AUTOMATIC DEPOSIT, ACME INC PAYROLL PPD"
    first = _open(conn, account, memo)
    assert first.mapped.predicted_payee == rename_tree.tidy_text(memo)
    assert first.mapped.payee == ""                      # the review row is untouched
    assert first.mapped.payee_candidates == []
    import_review.save_new(conn, account, first.mapped, payee="Acme Inc.",
                           review_id=first.review_id)
    second = _open(conn, account, memo)
    assert second.mapped.predicted_payee == rename_tree.tidy_text(memo)   # once is not enough
    assert second.mapped.payee_candidates == []
    import_review.save_new(conn, account, second.mapped, payee="Acme Inc.",
                           review_id=second.review_id)
    third = _open(conn, account, memo)
    assert third.mapped.predicted_payee == "Acme Inc."
    assert third.mapped.payee == ""


def test_supplied_payee_shows_verbatim_until_renamed_twice(conn, account):
    from mammon.importers.record import NormalizedTxn

    def rec(fitid):
        return NormalizedTxn(date="2026-05-01", amount_cents=-1234, fitid=fitid,
                             payee="Dividend Earned For Period O",
                             memo="DIVIDEND EARNED FOR PERIOD OF 08/01/2026 THRU 08/31/2026")

    def open_rec(fitid):
        [entry] = import_review.build_review_from_records(conn, account, [rec(fitid)])
        import_review.persist_entries(conn, account, [entry])
        payee, _ = import_review.predict_fields(conn, entry.mapped)
        return entry, payee

    entry, shown = open_rec("D1")
    assert shown == "Dividend Earned For Period O"       # verbatim
    import_review.save_new(conn, account, entry.mapped, payee="Dividend",
                           review_id=entry.review_id)
    entry, shown = open_rec("D2")
    assert shown == "Dividend Earned For Period O"
    import_review.save_new(conn, account, entry.mapped, payee="Dividend",
                           review_id=entry.review_id)
    _, shown = open_rec("D3")
    assert shown == "Dividend"


def test_transfer_row_never_learns(conn, account):
    entry = import_review.build_review(
        conn, account, [_row("SHARE TRANSFER FROM SHARE ACCOUNT: 0123", tid="X1")])[0]
    assert entry.mapped.is_transfer is True
    import_review.save_new(conn, account, entry.mapped, payee="My Savings")
    assert rename_tree.examples(conn) == []


def test_bulk_accept_records_an_applied_fill_but_not_a_kept_default(conn, account):
    memo = "POS NETFLIX.COM 88"
    for _ in range(2):
        _accept(conn, account, memo, payee="Netflix")
    entries = import_review.build_review(conn, account, [
        _row(memo, tid="B1", amount="77.01"),
        _row("POS DEBIT 8842 WHOZIT LLC", tid="B2", amount="77.02")])
    import_review.persist_entries(conn, account, entries)
    assert all(e.is_new for e in entries)
    assert import_review.accept_all(conn, account) == 2
    labels = [e["label"] for e in rename_tree.examples(conn)]
    assert labels == ["Netflix", "Netflix", "Netflix"]   # the applied fill was logged
    assert rename_tree.suggest(conn, "POS DEBIT 8842 WHOZIT LLC").action == rename_tree.ACTION_LEAVE
    # Nothing was tallied: nobody looked at the bulk rows.
    assert rename_tree.rename_stats(conn) == [
        {"payee": "Netflix", "examples": 3, "applied": 0, "overridden": 0}]


def test_learning_survives_restart(tmp_path):
    path = tmp_path / "persist.db"
    c1 = db.init_db(path)
    acct = ledger.create_account(c1, "Checking", "checking")
    for i in range(2):
        _accept(c1, acct, "POS DEBIT SPOTIFY USA 555", payee="Spotify")
    c1.close()
    c2 = db.init_db(path)
    assert rename_tree.suggest(c2, "POS DEBIT SPOTIFY USA 777").payee == "Spotify"
    c2.close()


# ---------------------------------------------------------------------------
# the ACTION domain: the same engine, activity text -> Quicken action
# ---------------------------------------------------------------------------
def test_action_domain_is_isolated_from_the_payee_domain(conn):
    for _ in range(2):
        rename_tree.learn(conn, "USD CREDIT INTEREST", "IntInc", kind="action")
        rename_tree.learn(conn, "USD CREDIT INTEREST PAYMENT", "Interest Co", kind="payee")
    assert rename_tree.suggest(conn, "USD CREDIT INTEREST", kind="action").payee == "IntInc"
    rename_tree.clear(conn, kind="action")
    assert rename_tree.examples(conn, kind="action") == []
    assert rename_tree.examples(conn, kind="payee") != []
    assert rename_tree.suggest(conn, "USD CREDIT INTEREST", kind="action").action \
        == rename_tree.ACTION_LEAVE


def test_action_domain_counts_a_kept_importer_guess(conn):
    """An action the user KEPT is a confirmation worth counting -- unlike a
    kept description, which is no answer at all."""
    rename_tree.learn(conn, "Credit Interest", "IntInc", kind="action", extra="Credit Interest")
    rename_tree.learn(conn, "Credit Interest", "IntInc", kind="action", extra="Credit Interest")
    assert len(rename_tree.examples(conn, kind="action")) == 2


def test_action_tree_generalizes_across_securities(conn):
    """An IB dividend line embeds a per-security ISIN; the shape filter drops
    the ones seen once, so the leaf's core is the dividend vocabulary and a
    NEVER-SEEN security maps correctly."""
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
    assert rename_tree.bootstrap_actions(conn) == 4
    assert rename_tree.bootstrap_actions(conn) == 0          # idempotent
    s = rename_tree.suggest(
        conn, "SCHD(US78468R6633) Cash Dividend USD 0.25 per Share (Ordinary Dividend)",
        kind="action")
    assert (s.action, s.payee, s.high_confidence) == (rename_tree.ACTION_AUTO, "Div", True)


def test_action_label_is_read_live(conn):
    conn.execute("INSERT INTO accounts(name, type) VALUES ('B', 'investment')")
    aid = conn.execute("SELECT id FROM accounts WHERE name='B'").fetchone()["id"]
    ids = []
    for i in range(2):
        cur = conn.execute(
            "INSERT INTO investment_transactions(account_id,date,action,amount,memo)"
            " VALUES (?,?,?,?,?)", (aid, "2026-01-0%d" % (i + 1), "ShrsOut", -450,
                                    "RECORDKEEPING FEE"))
        ids.append(cur.lastrowid)
        rename_tree.learn(conn, "RECORDKEEPING FEE", "ShrsOut", kind="action",
                          txn_id=int(cur.lastrowid))
    assert rename_tree.suggest(conn, "RECORDKEEPING FEE", kind="action").payee == "ShrsOut"
    conn.execute("UPDATE investment_transactions SET action='MiscExp' WHERE id IN (?,?)", ids)
    conn.commit()
    assert rename_tree.suggest(conn, "RECORDKEEPING FEE", kind="action").payee == "MiscExp"


# ---------------------------------------------------------------------------
# bootstrap (explicit; nothing calls it on open)
# ---------------------------------------------------------------------------
def test_bootstrap_replays_register_history_once(conn, account):
    for i in range(3):
        ledger.add_transaction(conn, account, "2026-01-0%d" % (i + 1), -500,
                               payee="Netflix", memo="POS NETFLIX.COM %d" % i)
    ledger.add_transaction(conn, account, "2026-01-09", -500,
                           payee="Deposit", memo="DEPOSIT")            # memo == payee: no rename
    assert rename_tree.bootstrap(conn) == 4
    assert rename_tree.bootstrap(conn) == 0
    assert [e["label"] for e in rename_tree.examples(conn)] == ["Netflix"] * 3
    assert rename_tree.suggest(conn, "POS NETFLIX.COM 999").payee == "Netflix"


def test_bootstrap_skips_transfers(conn):
    a = ledger.create_account(conn, "Checking", "checking")
    b = ledger.create_account(conn, "Savings", "savings")
    ledger.create_transfer(conn, a, b, "2026-01-01", 1000,
                           memo="XFER TO SAVINGS 42", payee="Transfer to Savings")
    assert rename_tree.ensure_bootstrapped(conn) == 0
    assert rename_tree.suggest(conn, "XFER TO SAVINGS 42").action == rename_tree.ACTION_LEAVE


# ---------------------------------------------------------------------------
# migration v60: the example log is seeded from the accepted review rows
# ---------------------------------------------------------------------------
def test_v60_seeds_examples_from_accepted_review_rows(tmp_path):
    path = tmp_path / "old.db"
    c = db.connect(path)
    for m in db.MIGRATIONS[:59]:
        c.executescript(m)
    c.execute("PRAGMA user_version = 59")
    c.commit()
    aid = ledger.create_account(c, "Chk", "checking")

    def accepted(memo, payee, *, supplied="", action=None, raw_action=None):
        if action is not None:
            cur = c.execute(
                "INSERT INTO investment_transactions(account_id,date,action,amount,memo)"
                " VALUES (?,?,?,?,?)", (aid, "2026-05-01", action, -100, memo))
        else:
            cur = c.execute(
                "INSERT INTO transactions(account_id, date, amount, payee, memo) "
                "VALUES (?,?,?,?,?)", (aid, "2026-05-01", -100, payee, memo))
        c.execute(
            "INSERT INTO review_items(account_id, date, amount, payee, memo, label, "
            " state, accepted_txn_id, payee_supplied, is_investment, action, created_at) "
            "VALUES (?,?,?,?,?,'NEW','accepted',?,?,?,?, datetime('now'))",
            (aid, "2026-05-01", -100, supplied, memo, cur.lastrowid,
             1 if supplied else 0, 1 if action is not None else 0, raw_action))

    accepted("POS NETFLIX.COM 88", "Netflix")
    accepted("POS NETFLIX.COM 88", "POS Netflix.com 88")          # kept the tidied default
    accepted("", "Costco", supplied="COSTCO WHSE #1234")
    accepted("", "COSTCO WHSE #1234", supplied="COSTCO WHSE #1234")  # kept the supplied payee
    accepted("Credit Interest", None, action="IntInc", raw_action="Credit Interest")
    c.commit()
    c.close()

    conn = db.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert "rename_nodes" not in db.table_names(conn)
    rows = conn.execute(
        "SELECT kind, text, extra, label FROM rename_examples ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("payee", "POS NETFLIX.COM 88", "", "Netflix"),
        ("payee", "", "COSTCO WHSE #1234", "Costco"),
        ("action", "Credit Interest", "Credit Interest", "IntInc"),
    ]
    assert rename_tree.suggest(conn, "Credit Interest", kind="action").action == rename_tree.ACTION_LEAVE
    assert rename_tree.suggest(conn, "", extra="COSTCO WHSE #9").payees == []   # one sighting
    conn.close()

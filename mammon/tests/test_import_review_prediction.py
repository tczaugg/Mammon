"""Prediction + learning for downloaded/imported transactions.

Covers the fix for "payee/category prediction never reaches the register row":
prediction now happens AT REGISTER-ROW-CREATION time (RegisterModel.set_pending
-> import_review.predict_fields), sourced from LIVE history so it reflects rules
and accepts made earlier in the SAME review session. Also covers category
learning (the new category_rules engine), unlearn/keyword-scoped refinement on a
corrected auto-fill, and transfer interoperation.
"""
from __future__ import annotations

import pytest

from mammon import category_rules, category_tree, db, import_review, ledger, rename_tree
from mammon.importers.record import NormalizedTxn


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "Checking", "checking")


@pytest.fixture
def cats(conn):
    return {
        "dining": ledger.resolve_category(conn, "Dining"),
        "shopping": ledger.resolve_category(conn, "Shopping"),
        "cloud": ledger.resolve_category(conn, "Business:Cloud"),
        "fitness": ledger.resolve_category(conn, "Health:Fitness"),
    }


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _row(desc, *, tid="", amount="12.34", debit=True, date="2026-05-01"):
    return {
        "transactionId": tid,
        "postedDate": date,
        "amount": amount,
        "isDebit": debit,
        "statementDescription": desc,
    }


def _rec(*, payee="", memo="", amount_cents=-1234, date="2026-05-01", fitid="",
         category=""):
    """A parsed file record (OFX NAME / QIF payee / tabular payee column ->
    ``payee``) as ``mapped_from_record`` receives it."""
    return NormalizedTxn(date=date, amount_cents=amount_cents, payee=payee,
                         memo=memo, fitid=fitid, category=category)


# ---------------------------------------------------------------------------
# predict_fields: applies learned rules (this is what set_pending calls)
# ---------------------------------------------------------------------------
def test_predict_applies_existing_payee_and_category_rules(conn, account, cats):
    # Enough corroborating renames (HIGH_CONFIDENCE_MIN_COUNT) -> a CONFIDENT
    # (high) payee that prefills. A
    # single example is low confidence (see test_predict_low_confidence_shows_raw).
    for _ in range(4):
        rename_tree.learn(conn, "POS DEBIT NETFLIX.COM 8888", "Netflix")
    # The category is learned UNDER THE PAYEE now (mammon.category_tree), not as
    # a global keyword rule -- that is the whole point of the rewrite.
    for _ in range(4):
        category_tree.learn(conn, "Netflix", "POS DEBIT NETFLIX.COM 8888",
                            cats["dining"])
    m = import_review.map_row(_row("POS DEBIT NETFLIX.COM 8888"))
    payee, cat = import_review.predict_fields(conn, m)
    assert payee == "Netflix"
    assert cat == cats["dining"]


def test_predict_low_confidence_shows_raw(conn, account, cats):
    # A payee learned from ONE example is not enough: predict shows the bank's
    # own text (tidied for reading) rather than silently renaming; the category
    # tree, keyed on that text, still applies.
    rename_tree.learn(conn, "POS DEBIT NETFLIX.COM 8888", "Netflix")
    # The displayed payee stays the bank text here, and the category follows
    # whatever payee is resolved -- so it is that text that has to carry the
    # history.
    for _ in range(4):
        category_tree.learn(conn, "POS DEBIT NETFLIX.COM 8888",
                            "POS DEBIT NETFLIX.COM 8888", cats["dining"])
    m = import_review.map_row(_row("POS DEBIT NETFLIX.COM 8888"))
    payee, cat = import_review.predict_fields(conn, m)
    assert payee == import_review._clean_payee(m.memo)      # the text, not "Netflix"
    assert cat == cats["dining"]
    # ...and ONE sighting is not offered in the dropdown either (see
    # RENAME_MIN_SIGHTINGS).
    assert m.payee_candidates == []


def test_prior_register_rows_never_fill_the_payee(conn, account, cats):
    """Register rows sharing the statement text used to supply the PAYEE. They
    no longer do: rows that never went through review carry memos the user
    typed, and matching a download against those is how a 1998 note of
    "deposit" once renamed MOBILE DEPOSIT. Their CATEGORY is still back-filled
    when the resolved payee is theirs.

    Three rows saved WITHOUT learning (a bulk path) carry the payee here, and
    that is exactly what does count: a bulk accept whose payee is a RENAME of
    the bank text is logged for the tree, so the tree -- not the register scan
    -- is what fills the fourth.
    """
    for i in range(3):
        row = import_review.map_row(_row("SQ *GYM MEMBERSHIP", tid=f"G{i}",
                                         amount="20.%02d" % i))
        import_review.save_new(conn, account, row, payee="City Gym",
                               category_id=cats["fitness"], learn=False)
    assert len(rename_tree.examples(conn)) == 3
    assert category_rules.list_rules(conn) == []

    nxt = import_review.map_row(_row("SQ *GYM MEMBERSHIP", tid="G-next"))
    payee, cat = import_review.predict_fields(conn, nxt)
    assert payee == "City Gym"
    assert cat == cats["fitness"]

    # The same three rows with the payee KEPT as the bank text teach nothing,
    # and the register scan supplies no payee for the next one.
    for i in range(3):
        row = import_review.map_row(_row("SQ *YOGA STUDIO", tid=f"Y{i}",
                                         amount="30.%02d" % i))
        import_review.save_new(conn, account, row,
                               payee=import_review._clean_payee(row.memo),
                               category_id=cats["fitness"], learn=False)
    assert len(rename_tree.examples(conn)) == 3
    nxt = import_review.map_row(_row("SQ *YOGA STUDIO", tid="Y-next"))
    payee, cat = import_review.predict_fields(conn, nxt)
    assert payee == import_review._clean_payee(nxt.memo)
    assert cat == cats["fitness"]                 # same payee -> category still back-fills


def test_prior_register_rows_that_disagree_supply_nothing(conn, account, cats):
    """the user's bug: "it already knows Paypal?" -- on a ledger started fresh.

    It did not know it. Five register rows shared the text "AUTOMATIC DEPOSIT,
    PAYPAL TRANSFER PPD" carrying FOUR different payees, and the newest was
    filled in as established fact. Identical bank text is strong evidence of
    identity, but only when the rows carrying it agree.
    """
    for i, name in enumerate(("PAYPAL", "Alex", "Transferto", "Alex")):
        row = import_review.map_row(_row("AUTOMATIC DEPOSIT, PAYPAL TRANSFER PPD",
                                         tid=f"PP{i}"))
        import_review.save_new(conn, account, row, payee=name,
                               category_id=cats["shopping"], learn=False)

    nxt = import_review.map_row(_row("AUTOMATIC DEPOSIT, PAYPAL TRANSFER PPD",
                                     tid="PP-next"))
    assert import_review._prior_txn_for(conn, nxt) is None
    payee, _cat = import_review.predict_fields(conn, nxt)
    assert payee not in {"PAYPAL", "Alex", "Transferto"}


def test_auto_filled_payee_gets_the_registers_category_like_a_typed_one(conn, account, cats):
    """the user's report: Enbridge Gas has always been one category in the
    register, the tree filled the payee on the third download, and the
    category sat blank -- while TYPING the payee on the second had pre-entered
    it, because the blank row asks QuickFill. The category tree needs four
    review votes and never reads the register; an auto-filled payee now asks
    QuickFill exactly as a typed one does."""
    for i in range(3):
        ledger.add_transaction(conn, account, "2026-0%d-18" % (i + 1), -80_00,
                               payee="Enbridge Gas", category_id=cats["cloud"])
    memo = "AUTOMATIC WITHDRAWAL, ENBRIDGE GAS QGC PPD"
    for _ in range(2):
        rename_tree.learn(conn, memo, "Enbridge Gas")
    m = import_review.map_row(_row(memo, tid="E3"))
    payee, cat = import_review.predict_fields(conn, m, account_id=account)
    assert payee == "Enbridge Gas"
    assert cat == cats["cloud"]

    # An UNSETTLED payee -- the bank text in the cell -- asks nothing.
    m = import_review.map_row(_row("AUTOMATIC WITHDRAWAL, NEW UTILITY PPD", tid="N1"))
    payee, cat = import_review.predict_fields(conn, m, account_id=account)
    assert payee == import_review._clean_payee(m.memo) and cat is None

    # A payee review history has shown to be a catalogue is left to the
    # picker: the tree's refusal is measured, the last row's category a toss.
    for i, key in enumerate(("dining", "shopping", "cloud", "fitness")):
        category_tree.learn(conn, "Amazon", "AMZN MKTP US %d" % i, cats[key])
    ledger.add_transaction(conn, account, "2026-05-01", -10_00,
                           payee="Amazon", category_id=cats["dining"])
    for _ in range(2):
        rename_tree.learn(conn, "AMZN MKTP US", "Amazon")
    m = import_review.map_row(_row("AMZN MKTP US", tid="A3"))
    payee, cat = import_review.predict_fields(conn, m, account_id=account)
    assert payee == "Amazon" and cat is None
    assert set(m.category_candidates) == {cats[k] for k in ("dining", "shopping", "cloud", "fitness")}


def test_predict_falls_back_to_tidied_bank_text_when_nothing_known(conn, account):
    # Nothing learned and no prior transaction to copy: the REGISTER still offers
    # a tidied form of the bank text rather than a blank Payee cell. The stored
    # review row keeps its empty payee -- the suggestion exists only in the
    # editable register row, where the user can accept or overwrite it.
    m = import_review.map_row(_row("BRAND NEW MERCHANT LLC"))
    assert m.payee == ""
    payee, cat = import_review.predict_fields(conn, m)
    assert payee == "Brand New Merchant LLC"
    assert cat is None


# ---------------------------------------------------------------------------
# supplied payee (OFX NAME / QIF payee / tabular payee column): use verbatim,
# skip the description-driven rename rules, still auto-assign the category.
# (user: "imports that already have a Payee ... should just be copied over.")
# ---------------------------------------------------------------------------
def test_mapped_from_record_flags_supplied_payee(conn):
    # A record that carries a payee is flagged authoritative; one without a payee
    # (payee to be inferred from the description) is not.
    with_payee = import_review.mapped_from_record(
        _rec(payee="Jane Doe", memo="POS DEBIT VENMO 8888"))
    assert with_payee.payee == "Jane Doe"
    assert with_payee.payee_supplied is True

    without_payee = import_review.mapped_from_record(
        _rec(payee="", memo="POS DEBIT NETFLIX.COM 8888"))
    assert without_payee.payee == ""
    assert without_payee.payee_supplied is False


def test_predict_keeps_supplied_payee_and_skips_rename(conn, account, cats):
    # A rename learned from the DESCRIPTION alone must not rewrite a payee the
    # record supplied -- the Venmo case: the description names the
    # intermediary, the From column names the real counterparty. The absence
    # of a payee field is a feature of the learned pattern, so a row that
    # carries one does not fit it. (A supplied payee renamed twice IS
    # overridden; pinned in test_rename_tree.) Category auto-assign still runs.
    for _ in range(2):
        rename_tree.learn(conn, "POS DEBIT VENMO 8888", "Rename Rule Payee")
    for _ in range(4):
        category_tree.learn(conn, "Jane Doe", "POS DEBIT VENMO 8888",
                            cats["dining"])
    m = import_review.mapped_from_record(
        _rec(payee="Jane Doe", memo="POS DEBIT VENMO 8888"))
    payee, cat = import_review.predict_fields(conn, m)
    assert payee == "Jane Doe"            # verbatim, NOT "Rename Rule Payee"
    assert cat == cats["dining"]          # category still auto-assigned


def test_predict_supplied_payee_low_confidence_category_blank(conn, account, cats):
    # Supplied payee, but nothing matches the category confidently -> leave the
    # category blank (do not guess), while still passing the payee through.
    m = import_review.mapped_from_record(
        _rec(payee="Some Merchant", memo="TOTALLY UNKNOWN STATEMENT TEXT"))
    payee, cat = import_review.predict_fields(conn, m)
    assert payee == "Some Merchant"
    assert cat is None


def test_predict_supplied_payee_prior_fills_category_not_payee(conn, account, cats):
    # A prior register row with the same statement text supplies a CATEGORY but
    # must never override the record's own payee.
    prior = import_review.map_row(_row("SQ *GYM", tid="G1"))
    import_review.save_new(conn, account, prior, payee="City Gym",
                           category_id=cats["fitness"], learn=False)
    m = import_review.mapped_from_record(_rec(payee="My Own Payee", memo="SQ *GYM"))
    payee, cat = import_review.predict_fields(conn, m)
    assert payee == "My Own Payee"        # supplied payee wins over prior "City Gym"
    # ...and the category does NOT come across. The prior row carried the same
    # statement text but a DIFFERENT payee ("City Gym"), and Fitness has never
    # been seen for "My Own Payee" -- the locked rule is that a category is only
    # ever proposed for a payee that has carried it. Back-filling from a prior
    # row is kept for the SAME payee (test_predict_falls_back_to_prior_...).
    assert cat is None


def test_predict_absent_payee_still_runs_rename_rules(conn, account, cats):
    # The renaming rules are exactly for records whose payee must be inferred from
    # the description -- a record with NO supplied payee still gets renamed.
    for _ in range(4):
        rename_tree.learn(conn, "POS DEBIT NETFLIX.COM 8888", "Netflix")
    for _ in range(4):
        category_tree.learn(conn, "Netflix", "POS DEBIT NETFLIX.COM 8888",
                            cats["dining"])
    m = import_review.mapped_from_record(
        _rec(payee="", memo="POS DEBIT NETFLIX.COM 8888"))
    assert m.payee_supplied is False
    payee, cat = import_review.predict_fields(conn, m)
    assert payee == "Netflix"             # rename rule applied (payee inferred)
    assert cat == cats["dining"]


def test_supplied_payee_flag_survives_persist_reload(conn, account, cats):
    # Regression (the user, Venmo): EVERY file/download review is persisted then
    # reloaded (persist_entries -> load_pending) before the register shows it, so
    # the authoritative-payee flag MUST round-trip through review_items. It used to
    # live only in memory, so the reloaded row came back payee_supplied=False and
    # predict_fields re-ran the description rename tree, clobbering a good 'From'
    # payee (a Venmo file has a 'From'/'To' column, never a literal 'Payee' header).
    #
    # A learned-but-below-the-floor rename for the statement text is the
    # discriminator: if the flag were lost, predict would treat the row as
    # unsupplied and return the bank's description (the no-rename display
    # rule) instead of the supplied name. ONE example, so the tree cannot fill
    # either way (two would be a legitimate rename of this exact text).
    rename_tree.learn(conn, "PAYMENT FROM JAMIE CHEN JUNE RENT",
                      "Rename Rule Payee")
    for _ in range(4):
        category_tree.learn(conn, "Jamie Chen",
                            "PAYMENT FROM JAMIE CHEN JUNE RENT", cats["dining"])
    # Venmo-shaped record: payee came from the 'From' column (no 'Payee' header),
    # memo is the raw note the renamer would otherwise fire on.
    rec = _rec(payee="Jamie Chen",
               memo="PAYMENT FROM JAMIE CHEN JUNE RENT",
               amount_cents=225000, fitid="VENMO1")
    entries = import_review.build_review_from_records(conn, account, [rec])
    assert entries[0].mapped.payee_supplied is True          # set at build time

    import_review.persist_entries(conn, account, entries)
    reloaded = import_review.load_pending(conn, account)
    assert len(reloaded) == 1
    m = reloaded[0].mapped
    assert m.payee == "Jamie Chen"
    assert m.payee_supplied is True                          # SURVIVED the reload

    payee, cat = import_review.predict_fields(conn, m)
    assert payee == "Jamie Chen"     # verbatim, NOT "Rename Rule Payee"
    assert cat == cats["dining"]         # category still auto-assigned


def test_absent_payee_flag_reloads_false_and_renames(conn, account, cats):
    # The other half of the round trip: a record with NO supplied payee reloads
    # payee_supplied=False, so predict_fields still infers a payee from the
    # description (the scrape/renamer path must not be suppressed by the fix).
    for _ in range(4):
        rename_tree.learn(conn, "POS DEBIT NETFLIX.COM 8888", "Netflix")
    rec = _rec(payee="", memo="POS DEBIT NETFLIX.COM 8888", fitid="NFLX1")
    entries = import_review.build_review_from_records(conn, account, [rec])
    import_review.persist_entries(conn, account, entries)
    m = import_review.load_pending(conn, account)[0].mapped
    assert m.payee_supplied is False
    payee, _cat = import_review.predict_fields(conn, m)
    assert payee == "Netflix"            # renamer still applies on reload


# ---------------------------------------------------------------------------
# in-session learning: accept one row, the NEXT row of the same kind predicts it
# ---------------------------------------------------------------------------
def test_in_session_learning_payee_and_category(conn, account, cats):
    # Accept four coffee charges, correcting the payee and setting a category
    # (four corroborating renames reach the high-confidence floor); the
    # category rule learns from the first accept already.
    m1 = import_review.map_row(_row("POS COFFEE SHOP 12", tid="C1"))
    p1, c1 = import_review.predict_fields(conn, m1)   # what set_pending would fill
    m1.payee, m1.category_id = p1, c1
    assert c1 is None                                 # nothing learned yet
    import_review.save_new(conn, account, m1, payee="Coffee Shop",
                           category_id=cats["dining"])
    for i, chg in enumerate(("22", "31", "47")):      # up to the confidence floor
        mb = import_review.map_row(_row("POS COFFEE SHOP " + chg, tid="C1%d" % i))
        import_review.save_new(conn, account, mb, payee="Coffee Shop",
                               category_id=cats["dining"])

    # A LATER row of the same kind, still in this session, now predicts both.
    m2 = import_review.map_row(_row("POS COFFEE SHOP 34", tid="C2"))
    payee, cat = import_review.predict_fields(conn, m2)
    assert payee == "Coffee Shop"
    assert cat == cats["dining"]


# ---------------------------------------------------------------------------
# category learning is symmetric with payee learning through save_new
# ---------------------------------------------------------------------------
def test_save_new_learns_the_category_under_the_payee(conn, account, cats):
    """Accepting a row teaches the PAYEE-scoped tree, not a global keyword rule.

    The old learner minted ``keyword -> category`` from a single correction and
    matched it against every merchant; this records a vote under "Whole Foods",
    where it can only ever affect "Whole Foods".
    """
    m = import_review.map_row(_row("WHOLE FOODS MKT 123", tid="W1"))
    import_review.save_new(conn, account, m, payee="Whole Foods",
                           category_id=cats["shopping"])
    assert category_tree.known_categories(conn, "Whole Foods") == [
        (cats["shopping"], 1)]
    assert category_rules.list_rules(conn) == []      # no global rule minted


def test_save_new_does_not_learn_category_when_unchanged(conn, account, cats):
    # Category equal to the predicted provisional -> no (spurious) learning.
    m = import_review.map_row(_row("TARGET STORE 55", tid="T1"))
    m.category_id = cats["shopping"]  # pretend prediction pre-filled this
    import_review.save_new(conn, account, m, payee="Target",
                           category_id=cats["shopping"])
    assert category_rules.list_rules(conn) == []


# ---------------------------------------------------------------------------
# differentiate: the tree splits an AWS variant off from ordinary Amazon
# ---------------------------------------------------------------------------
def test_tree_differentiates_shared_prefix_payees(conn, account, cats):
    # Ordinary Amazon shopping is well-learned (confident, shallow).
    for _ in range(4):
        rename_tree.learn(conn, "AMAZON.COM ORDER", "Amazon")
    # AWS invoices, sharing the AMAZON prefix, are corrected a couple of times --
    # they split off under their own differentiating token.
    # (4 corrections: the payee domain's high-confidence floor.)
    for _ in range(4):
        rename_tree.learn(conn, "AMAZON AWS CLOUD", "AWS")

    # The AWS variant now predicts AWS (its own deeper node)...
    m2 = import_review.map_row(_row("AMAZON AWS CLOUD 6", tid="AW2"))
    assert import_review.predict_fields(conn, m2)[0] == "AWS"
    # ...while ordinary Amazon shopping still predicts Amazon (the shallow node).
    m3 = import_review.map_row(_row("AMAZON.COM ORDER 9", tid="AW3"))
    assert import_review.predict_fields(conn, m3)[0] == "Amazon"


# ---------------------------------------------------------------------------
# transfer interoperation (requirement 4)
# ---------------------------------------------------------------------------
def test_transfer_row_keeps_transfer_payee_no_category_no_learning(conn, account, cats):
    rename_tree.learn(conn, "SHARE 0123", "Should Not Apply")
    category_rules.upsert_rule(conn, "SHARE", cats["shopping"])
    m = import_review.map_row(_row("SHARE TRANSFER FROM SHARE ACCOUNT: 0123", tid="X1"))
    assert m.is_transfer is True

    payee, cat = import_review.predict_fields(conn, m)
    assert payee.startswith("Transfer from")
    assert cat is None

    # Saving an edited transfer must learn neither a payee (tree) nor a category.
    before_examples = rename_tree.examples(conn)
    before_c = category_rules.list_rules(conn)
    import_review.save_new(conn, account, m, payee="My Savings",
                           category_id=cats["dining"])
    assert rename_tree.examples(conn) == before_examples
    assert category_rules.list_rules(conn) == before_c


# ---------------------------------------------------------------------------
# persistence across restart
# ---------------------------------------------------------------------------
def test_learned_categories_survive_restart(tmp_path):
    path = tmp_path / "persist.db"
    c1 = db.init_db(path)
    acct = ledger.create_account(c1, "Checking", "checking")
    cid = ledger.resolve_category(c1, "Dining")
    # Save four Hulu rows: enough for the learned payee to be CONFIDENT (the
    # rename tree's high-confidence floor) AND for the category tree's own
    # MIN_COUNT, so both halves still prefill after the restart.
    for tid in ("H1", "H1b", "H1c", "H1d"):
        m = import_review.map_row(_row("HULU 877-8244 SANTA MONICA", tid=tid))
        import_review.save_new(c1, acct, m, payee="Hulu", category_id=cid)
    assert category_tree.known_categories(c1, "Hulu")[0][0] == cid
    c1.close()

    c2 = db.init_db(path)
    # a fresh connection to the same file still predicts the learned category.
    m2 = import_review.map_row(_row("HULU 877-9999 SANTA MONICA", tid="H2"))
    payee, cat = import_review.predict_fields(c2, m2)
    assert payee == "Hulu"
    assert cat == cid
    # A charge from a city the rename has never seen is offered, not applied
    # (the pattern learned from identical rows requires SANTA MONICA), and the
    # category then hangs off the raw text -- which is what keeps a wrong fill
    # from dragging a category along.
    m3 = import_review.map_row(_row("HULU LOS GATOS CA", tid="H3"))
    payee, cat = import_review.predict_fields(c2, m3)
    assert payee == import_review._clean_payee(m3.memo)
    assert m3.payee_candidates == ["Hulu"]
    assert cat is None
    c2.close()


# ---------------------------------------------------------------------------
# apply-on-register-row-creation: RegisterModel.set_pending pre-fills the cells
# ---------------------------------------------------------------------------
def test_set_pending_prefills_payee_and_category_cells(qapp, conn, account, cats):
    from PyQt5.QtCore import Qt
    from mammon.ui.models import RegisterModel as M

    for _ in range(4):   # the high-confidence floor -> a payee that prefills
        rename_tree.learn(conn, "POS NETFLIX.COM 88", "Netflix")
    for _ in range(4):
        category_tree.learn(conn, "Netflix", "POS NETFLIX.COM 88", cats["dining"])
    entries = import_review.build_review(
        conn, account, [_row("POS NETFLIX.COM 88", tid="N1")])

    model = M(conn, account)
    model.set_pending(entries[0])
    prow = model.pending_row()
    assert model.data(model.index(prow, M.PAYEE), Qt.DisplayRole) == "Netflix"
    assert model.data(model.index(prow, M.CATEGORY), Qt.DisplayRole) == "Dining"
    # The prediction is stashed on the mapped row so a later accept can tell
    # whether the user corrected it.
    assert entries[0].mapped.category_id == cats["dining"]


def test_pending_payee_editor_is_typeable_dropdown(qapp, conn, account):
    # When the tree is UNSURE (two payees at one node), the pending row's payee
    # editor is an editable combo seeded with the candidate names -- the user can
    # pick one or type a brand-new payee.
    from PyQt5.QtWidgets import QComboBox
    from mammon.ui.models import RegisterModel as M
    from mammon.ui.delegates import PayeeTwoLineDelegate

    # Twice each: a payee seen once is not offered at all (RENAME_MIN_SIGHTINGS),
    # and this test is about the EDITOR being a typeable dropdown of candidates.
    for _ in range(2):
        rename_tree.learn(conn, "SQ MARKETPLACE", "Vendor A")
        rename_tree.learn(conn, "SQ MARKETPLACE", "Vendor B")
    entries = import_review.build_review(
        conn, account, [_row("SQ MARKETPLACE 7", tid="M1")])
    # The candidates are attached when the REGISTER opens the row (set_pending ->
    # predict_fields), not when the review list is built, so they are empty here.
    assert entries[0].mapped.payee_candidates == []

    model = M(conn, account)
    model.set_pending(entries[0])
    row = model.pending_row()
    assert set(model.pending_entry().mapped.payee_candidates) == {"Vendor A", "Vendor B"}

    delegate = PayeeTwoLineDelegate(None)
    editor = delegate._payee_combo(None, model.index(row, M.PAYEE))
    assert isinstance(editor, QComboBox) and editor.isEditable()
    assert {editor.itemText(i) for i in range(editor.count())} == {"Vendor A", "Vendor B"}


def test_boilerplate_overlap_never_renames(conn, account, cats):
    """the user's bug: a ledger started fresh renamed MOBILE DEPOSIT to "Foothill
    Place", a payee never seen in any review.

    One 1998 transaction whose memo the user had TYPED as "deposit" put that
    payee on the old trie's DEPOSIT node, and "MOBILE DEPOSIT" -- nothing but
    bank boilerplate -- walked into it. Boilerplate is not evidence: a row whose
    only shared token is DEPOSIT has no candidates at all, so even three
    examples of "deposit" propose nothing for it.
    """
    for _ in range(2):
        rename_tree.learn(conn, "deposit", "Foothill Place")
        rename_tree.learn(conn, "deposit", "Greg Smith")
        rename_tree.learn(conn, "Deposit", "U-Haul")

    m = import_review.map_row(_row("MOBILE DEPOSIT", tid="MD-1"))
    payee, _cat = import_review.predict_fields(conn, m)
    assert payee == import_review._clean_payee("MOBILE DEPOSIT")   # the text, not a guess
    assert m.payee_candidates == []
    # The exact text "DEPOSIT" does find its own history -- and it is contested.
    m = import_review.map_row(_row("DEPOSIT", tid="MD-2"))
    payee, _cat = import_review.predict_fields(conn, m)
    assert payee == "Deposit"
    assert set(m.payee_candidates) == {"Foothill Place", "Greg Smith", "U-Haul"}


def test_a_corroborated_rename_still_prefills(conn, account, cats):
    """The gate must not swallow the case it exists to serve."""
    for _ in range(rename_tree.HIGH_CONFIDENCE_MIN_COUNT):
        rename_tree.learn(conn, "POS DEBIT NETFLIX.COM 8888", "Netflix")
    m = import_review.map_row(_row("POS DEBIT NETFLIX.COM 8888", tid="NF-1"))
    payee, _cat = import_review.predict_fields(conn, m)
    assert payee == "Netflix"


def test_offering_and_filling_are_separate_gates(conn, account, cats):
    """RENAME_MIN_SIGHTINGS decides whether a name is OFFERED at all;
    rename_tree.MIN_FILL decides whether it is APPLIED to the cell. A payee
    renamed twice for the same text clears both; a payee with two sightings
    on OTHER text is offered for a new variant but not applied to it -- the
    user gets a one-click dropdown and the bank's own text in the cell.
    """
    for _ in range(2):
        rename_tree.learn(conn, "SQ *BLUE BOTTLE 4471 OAKLAND", "Blue Bottle")
    m = import_review.map_row(_row("SQ *BLUE BOTTLE 4471 OAKLAND", tid="BB-1"))
    payee, _cat = import_review.predict_fields(conn, m)
    assert payee == "Blue Bottle"                 # the same text, corrected twice
    m = import_review.map_row(_row("SQ *BLUE BOTTLE 9 SAN FRANCISCO", tid="BB-2"))
    payee, _cat = import_review.predict_fields(conn, m)
    assert payee == import_review._clean_payee(m.memo)   # a new variant: not applied
    assert m.payee_candidates == ["Blue Bottle"]         # ...but offered

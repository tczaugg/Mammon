"""Tests for the import-review data layer (mammon/import_review.py).

Covers field mapping (signed amount, ISO-offset date, provisional payee,
raw memo, transfer detection) and NEW/MATCHING classification (date+amount
window, transactionId dedupe).
"""
import pytest

from mammon import db, import_review, ledger
from mammon.import_review import (
    LABEL_MATCHING,
    LABEL_NEW,
    build_review,
    map_row,
)


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "mammon.db")
    yield c
    c.close()


@pytest.fixture
def account(conn):
    return ledger.create_account(conn, "Anytown CU Checking", "checking")


# ---------------------------------------------------------------------------
# field mapping
# ---------------------------------------------------------------------------
def test_dividend_row_maps_to_positive_credit(conn, account):
    """A dividend (credit) row: positive signed amount, tidy payee, raw memo."""
    row = {
        "transactionId": "DIV-1",
        "SubAccountId": "S1",
        "postedDate": "2026-07-31T00:00:00.000-06:00",
        "amount": "1.25",
        "isDebit": False,
        "checkNumber": "",
        "statementDescription": "DIVIDEND",
    }
    [entry] = build_review(conn, account, [row])
    m = entry.mapped

    assert m.amount_cents == 125            # credit -> positive
    assert m.date == "2026-07-31"           # ISO offset reduced to calendar day
    # The review row is GROUND TRUTH: the feed sent only a description, so no
    # payee is manufactured from it. The register proposes one at selection
    # time (predict_fields) -- that is a suggestion, not source data.
    assert m.payee == ""
    assert m.memo == "DIVIDEND"             # raw statementDescription preserved
    assert m.is_transfer is False
    assert m.transaction_id == "DIV-1"
    assert entry.label == LABEL_NEW         # empty register -> NEW


def test_debit_row_is_negative(conn, account):
    row = {
        "postedDate": "2026-08-10",
        "amount": "42.00",
        "isDebit": True,
        "statementDescription": "POS PURCHASE COFFEE SHOP",
    }
    m = map_row(row)
    assert m.amount_cents == -4200
    # No payee is invented from the description -- the review list shows what
    # the bank sent, verbatim, in Memo.
    assert m.payee == ""
    assert m.memo == "POS PURCHASE COFFEE SHOP"


def test_amount_sign_falls_back_to_amount_when_no_isdebit(conn):
    m = map_row({"postedDate": "2026-08-10", "amount": "-7.50",
                 "statementDescription": "REFUND"})
    assert m.amount_cents == -750


def test_transfer_row_detected(conn, account):
    """'SHARE TRANSFER FROM SHARE ACCOUNT: ...' -> flagged transfer."""
    row = {
        "transactionId": "XFER-9",
        "postedDate": "2026-08-05",
        "amount": "500.00",
        "isDebit": False,
        "statementDescription": "SHARE TRANSFER FROM SHARE ACCOUNT: 0002",
    }
    [entry] = build_review(conn, account, [row])
    m = entry.mapped

    assert m.is_transfer is True
    assert m.transfer_account == "Share Account"
    assert m.payee == "Transfer from Share Account"
    # Raw description kept verbatim in memo (including the trailing account ref).
    assert m.memo == "SHARE TRANSFER FROM SHARE ACCOUNT: 0002"
    assert m.amount_cents == 50000


def test_transfer_to_direction(conn):
    m = map_row({"postedDate": "2026-08-05", "amount": "10.00", "isDebit": True,
                 "statementDescription": "Transfer to Checking"})
    assert m.is_transfer is True
    assert m.transfer_account == "Checking"
    assert m.payee == "Transfer to Checking"


# ---------------------------------------------------------------------------
# classification: NEW vs MATCHING
# ---------------------------------------------------------------------------
def test_matching_vs_new_by_date_and_amount(conn, account):
    """One incoming row matches an existing register txn (date+amount within
    the window); a same-shaped-but-different row is NEW."""
    # Existing hand-entered txn: $25.00 debit on Aug 20.
    ledger.add_transaction(conn, account, "2026-08-20", -2500, payee="Grocer")

    rows = [
        {   # posts a day later, same signed amount -> MATCHING
            "transactionId": "A",
            "postedDate": "2026-08-21T12:00:00-06:00",
            "amount": "25.00",
            "isDebit": True,
            "statementDescription": "GROCER STORE #14",
        },
        {   # different amount -> NEW
            "transactionId": "B",
            "postedDate": "2026-08-21",
            "amount": "99.99",
            "isDebit": True,
            "statementDescription": "HARDWARE STORE",
        },
    ]
    matched, new = build_review(conn, account, rows)

    assert matched.label == LABEL_MATCHING
    assert matched.match_method == "date+amount"
    assert matched.matched_txn_id is not None
    assert matched.is_matching

    assert new.label == LABEL_NEW
    assert new.matched_txn_id is None
    assert new.is_new


def test_outside_window_is_new(conn, account):
    ledger.add_transaction(conn, account, "2026-08-01", -2500)
    # Same amount but 10 days later -> outside the default +/-3d window.
    [entry] = build_review(conn, account, [{
        "postedDate": "2026-08-11", "amount": "25.00", "isDebit": True,
        "statementDescription": "SOMETHING",
    }])
    assert entry.label == LABEL_NEW


def test_dedupe_by_transaction_id(conn, account):
    """A stored transactionId (fitid) wins even when amount/date differ."""
    existing = ledger.add_transaction(
        conn, account, "2026-08-01", 1000, payee="Seed", fitid="TXN-123")

    [entry] = build_review(conn, account, [{
        "transactionId": "TXN-123",
        "postedDate": "2026-01-01",     # far from the stored date
        "amount": "999.99",            # different amount
        "isDebit": False,
        "statementDescription": "DOES NOT MATTER",
    }])

    assert entry.label == LABEL_MATCHING
    assert entry.match_method == "transactionId"
    assert entry.matched_txn_id == existing


def test_unknown_transaction_id_falls_through_to_amount_match(conn, account):
    """An un-stored transactionId must not short-circuit; date+amount still runs."""
    tid = ledger.add_transaction(conn, account, "2026-08-02", -1500)
    [entry] = build_review(conn, account, [{
        "transactionId": "NEVER-STORED",
        "postedDate": "2026-08-02",
        "amount": "15.00",
        "isDebit": True,
        "statementDescription": "X",
    }])
    assert entry.label == LABEL_MATCHING
    assert entry.match_method == "date+amount"
    assert entry.matched_txn_id == tid


# ---------------------------------------------------------------------------
# transfer matching: a downloaded row that corresponds to the already-recorded
# opposite leg of a manually-entered double-entry transfer must MATCH that leg
# (not appear as a NEW duplicate), and accepting it must reconcile the leg
# without inserting a second transaction.
# ---------------------------------------------------------------------------
def test_transfer_download_matches_existing_transfer_leg(conn, account):
    """Enter a transfer A -> B by hand, then download B: B's incoming row must
    MATCH the register leg created in B, via the ``transfer`` method."""
    savings = ledger.create_account(conn, "Share Savings", "savings")
    # Manual transfer of $500 from checking (A) into savings (B).
    _from_id, to_id = ledger.create_transfer(
        conn, account, savings, "2026-08-05", 50000)

    [entry] = build_review(conn, savings, [{
        "transactionId": "XFER-9",
        "postedDate": "2026-08-06",          # +1 day, within window
        "amount": "500.00", "isDebit": False,
        "statementDescription": "SHARE TRANSFER FROM SHARE ACCOUNT: 0002",
    }])

    assert entry.mapped.is_transfer
    assert entry.label == LABEL_MATCHING
    assert entry.match_method == "transfer"
    assert entry.matched_txn_id == to_id


def test_transfer_prefers_leg_over_coincidental_plain_txn(conn, account):
    """A same-amount plain txn that is CLOSER in date must not steal the match
    from the transfer leg when the download reads as a transfer."""
    savings = ledger.create_account(conn, "Share Savings", "savings")
    _from_id, to_id = ledger.create_transfer(
        conn, account, savings, "2026-08-05", 50000)
    # Coincidental same-amount non-transfer deposit, one day closer to the feed.
    plain = ledger.add_transaction(conn, savings, "2026-08-06", 50000,
                                   payee="Refund")

    [entry] = build_review(conn, savings, [{
        "postedDate": "2026-08-06", "amount": "500.00", "isDebit": False,
        "statementDescription": "SHARE TRANSFER FROM SHARE ACCOUNT: 0002",
    }])

    assert entry.match_method == "transfer"
    assert entry.matched_txn_id == to_id       # the leg, not the closer plain txn
    assert entry.matched_txn_id != plain


def test_accept_transfer_match_reconciles_leg_without_duplicating(conn, account):
    """Accepting the transfer MATCH clears the downloaded leg (stamps fitid +
    cleared) and inserts nothing; the counterparty leg is left untouched."""
    savings = ledger.create_account(conn, "Share Savings", "savings")
    from_id, to_id = ledger.create_transfer(
        conn, account, savings, "2026-08-05", 50000)

    [entry] = build_review(conn, savings, [{
        "transactionId": "XFER-9",
        "postedDate": "2026-08-06", "amount": "500.00", "isDebit": False,
        "statementDescription": "SHARE TRANSFER FROM SHARE ACCOUNT: 0002",
    }])
    assert entry.is_matching and entry.matched_txn_id == to_id

    count_before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    import_review.accept_match(conn, entry)
    count_after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    assert count_after == count_before          # no duplicate transaction

    downloaded = _txn(conn, to_id)              # B's leg = the downloaded side
    assert downloaded["fitid"] == "XFER-9"
    assert downloaded["cleared"] == 1

    counterparty = _txn(conn, from_id)          # A's leg is not the downloaded side
    assert counterparty["fitid"] is None
    assert (counterparty["cleared"] or 0) == 0


# ---------------------------------------------------------------------------
# persistence: save_new / accept_match / revert_match / delete_saved
# (the ONLY paths by which a reviewed row enters the register)
# ---------------------------------------------------------------------------
def test_accept_transfer_match_leaves_legs_unreconciled_and_independent(conn, account):
    """The import/match path that reconciles a downloaded leg against a
    manually-entered transfer must CLEAR the leg but NEVER auto-reconcile it,
    leave the counterparty leg fully untouched, keep the cleared leg visible in
    ITS account's reconcile dialog, and let each account reconcile independently."""
    savings = ledger.create_account(conn, "Share Savings", "savings")
    from_id, to_id = ledger.create_transfer(conn, account, savings, "2026-08-05", 50000)

    [entry] = build_review(conn, savings, [{
        "transactionId": "XFER-9",
        "postedDate": "2026-08-06", "amount": "500.00", "isDebit": False,
        "statementDescription": "SHARE TRANSFER FROM SHARE ACCOUNT: 0002",
    }])
    assert entry.is_matching and entry.matched_txn_id == to_id
    import_review.accept_match(conn, entry)

    # Downloaded leg is CLEARED (bank shows it) but NOT auto-reconciled.
    downloaded = ledger.get_transaction(conn, to_id)
    assert downloaded["cleared"] == 1 and downloaded["reconciled"] == 0
    # Counterparty leg is untouched: still uncleared, unreconciled.
    counterparty = ledger.get_transaction(conn, from_id)
    assert counterparty["cleared"] == 0 and counterparty["reconciled"] == 0

    # The just-cleared leg is still a candidate in savings' reconcile dialog
    # (it was NOT hidden by an auto-R).
    assert any(r["id"] == to_id for r in ledger.unreconciled_rows(conn, savings))

    # Reconcile savings independently; the checking leg stays unreconciled and
    # reconcilable on its own.
    stmt = ledger.account_balance(conn, savings)
    ledger.finish_reconciliation(conn, savings, "2026-08-31", stmt)
    assert ledger.get_transaction(conn, to_id)["reconciled"] == 1
    assert ledger.get_transaction(conn, from_id)["reconciled"] == 0
    assert any(r["id"] == from_id for r in ledger.unreconciled_rows(conn, account))


def _txn(conn, txn_id):
    return conn.execute(
        "SELECT date, amount, payee, memo, category_id, fitid, cleared "
        "FROM transactions WHERE id=?", (txn_id,)).fetchone()


def test_save_new_writes_edited_fields_into_register(conn, account):
    """A NEW row is not in the register until save_new; edited payee/category/
    memo and the feed fitid are persisted."""
    [entry] = build_review(conn, account, [{
        "transactionId": "NEW-1",
        "postedDate": "2026-08-15",
        "amount": "30.00", "isDebit": True,
        "statementDescription": "POS PURCHASE COFFEE",
    }])
    assert entry.is_new
    # Nothing in the register yet.
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    cat_id = ledger.resolve_category(conn, "Dining")
    txn_id = import_review.save_new(
        conn, account, entry.mapped,
        payee="Coffee Shop", category_id=cat_id, memo="latte")

    row = _txn(conn, txn_id)
    assert row["amount"] == -3000
    assert row["date"] == "2026-08-15"
    assert row["payee"] == "Coffee Shop"
    assert row["memo"] == "latte"
    assert row["category_id"] == cat_id
    assert row["fitid"] == "NEW-1"


def test_category_id_for_name_resolves_existing_only(conn, account):
    cat_id = ledger.resolve_category(conn, "Groceries")
    assert import_review.category_id_for_name(conn, "groceries") == cat_id
    assert import_review.category_id_for_name(conn, "  GROCERIES ") == cat_id
    assert import_review.category_id_for_name(conn, "") is None
    assert import_review.category_id_for_name(conn, "Nonexistent") is None


def test_delete_saved_removes_the_row(conn, account):
    [entry] = build_review(conn, account, [{
        "postedDate": "2026-08-15", "amount": "5.00", "isDebit": True,
        "statementDescription": "SNACK"}])
    txn_id = import_review.save_new(conn, account, entry.mapped)
    assert _txn(conn, txn_id) is not None
    import_review.delete_saved(conn, txn_id)
    assert _txn(conn, txn_id) is None


def test_accept_match_stamps_fitid_and_cleared_then_reverts(conn, account):
    """Accepting a MATCHING row reconciles the EXISTING register line (stamps the
    feed fitid + cleared) without adding a new row; revert_match undoes it."""
    existing = ledger.add_transaction(
        conn, account, "2026-08-20", -2500, payee="Grocer")
    before = _txn(conn, existing)
    assert before["fitid"] is None
    assert (before["cleared"] or 0) == 0

    [entry] = build_review(conn, account, [{
        "transactionId": "FEED-9",
        "postedDate": "2026-08-21",         # +1 day, same signed amount
        "amount": "25.00", "isDebit": True,
        "statementDescription": "GROCER"}])
    assert entry.is_matching
    assert entry.matched_txn_id == existing
    # No new row is created by accepting.
    count_before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    prior = import_review.accept_match(conn, entry)
    count_after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert count_after == count_before        # accept != insert

    after = _txn(conn, existing)
    assert after["fitid"] == "FEED-9"         # stamped from the feed
    assert after["cleared"] == 1

    import_review.revert_match(conn, entry, prior)
    reverted = _txn(conn, existing)
    assert reverted["fitid"] is None
    assert (reverted["cleared"] or 0) == 0


def test_accept_match_keeps_existing_fitid(conn, account):
    """Accept must NOT clobber a real fitid already on the register line."""
    existing = ledger.add_transaction(
        conn, account, "2026-08-01", 1000, payee="Seed", fitid="TXN-123")
    [entry] = build_review(conn, account, [{
        "transactionId": "OTHER-1",
        "postedDate": "2026-08-01", "amount": "10.00", "isDebit": False,
        "statementDescription": "SEED"}])
    assert entry.matched_txn_id == existing
    import_review.accept_match(conn, entry)
    assert _txn(conn, existing)["fitid"] == "TXN-123"   # preserved


def test_accept_match_carries_qif_reconciled_onto_matched_transfer_leg(conn, account):
    """A single-account QIF re-import whose transfer leg is RECONCILED (Quicken
    'CX') and MATCHES an existing register leg must carry that R onto the leg --
    not just cleared. Regression: ~30 years of already-reconciled transfers
    matched here via accept_match and silently lost their R because accept only
    ever forced cleared=1. accept_match must RAISE reconciled from the incoming
    row's own status and revert_match must restore the prior value exactly."""
    from mammon.importers.record import NormalizedTxn

    savings = ledger.create_account(conn, "Share Savings", "savings")
    checking_name = ledger.get_account(conn, account)["name"]
    # Existing double-entry transfer -- both legs UNreconciled (the stale state a
    # pre-fix import left behind).
    from_id, to_id = ledger.create_transfer(conn, account, savings, "2020-01-05", 10000)
    assert ledger.get_transaction(conn, to_id)["reconciled"] == 0

    # Incoming QIF leg for 'Share Savings': +$100 transfer from checking, CX.
    rec = NormalizedTxn(
        external_account="Share Savings", account_type="savings",
        date="2020-01-05", amount_cents=10000, payee="Move to savings",
        transfer_account=checking_name, cleared=1, reconciled=1,
    )
    [entry] = import_review.build_review_from_records(conn, savings, [rec])
    assert entry.is_matching and entry.matched_txn_id == to_id

    prior = import_review.accept_match(conn, entry)
    leg = ledger.get_transaction(conn, to_id)
    assert (leg["cleared"], leg["reconciled"]) == (1, 1)   # R carried, not dropped
    # Counterparty (checking) leg is untouched -- each leg reconciles independently.
    assert ledger.get_transaction(conn, from_id)["reconciled"] == 0

    # Undo restores the leg's pre-accept (unreconciled) state exactly.
    import_review.revert_match(conn, entry, prior)
    back = ledger.get_transaction(conn, to_id)
    assert (back["cleared"], back["reconciled"]) == (0, 0)


def test_accept_match_never_lowers_an_already_reconciled_row(conn, account):
    """RAISE-only: a plain download (reconciled=0) that merely matches an
    already-RECONCILED register line must not knock its R off."""
    existing = ledger.add_transaction(conn, account, "2026-08-20", -2500, payee="Grocer")
    conn.execute("UPDATE transactions SET cleared=1, reconciled=1 WHERE id=?", (existing,))
    conn.commit()
    [entry] = build_review(conn, account, [{
        "transactionId": "FEED-1", "postedDate": "2026-08-21",
        "amount": "25.00", "isDebit": True, "statementDescription": "GROCER"}])
    assert entry.is_matching and entry.matched_txn_id == existing
    import_review.accept_match(conn, entry)
    row = conn.execute(
        "SELECT cleared, reconciled FROM transactions WHERE id=?", (existing,)).fetchone()
    assert (row["cleared"], row["reconciled"]) == (1, 1)   # preserved, not lowered


# ---------------------------------------------------------------------------
# persisted review list (schema v11): persist / load / count and the bulk +
# manual-match operations that act on a whole account's review_items rows.
# ---------------------------------------------------------------------------
def _new_rows():
    return [
        {"transactionId": "P-1", "postedDate": "2026-08-10", "amount": "5.00",
         "isDebit": True, "statementDescription": "COFFEE"},
        {"transactionId": "P-2", "postedDate": "2026-08-11", "amount": "9.00",
         "isDebit": True, "statementDescription": "LUNCH"},
    ]


def test_persist_and_load_pending_roundtrip_and_dedupe(conn, account):
    """persist_entries stores pending rows and stamps review_id; load_pending
    rebuilds them; a re-persist of the same source ids adds nothing (dedupe)."""
    entries = build_review(conn, account, _new_rows())
    inserted = import_review.persist_entries(conn, account, entries)
    assert inserted == 2
    assert all(e.review_id is not None for e in entries)

    loaded = import_review.load_pending(conn, account)
    assert [e.mapped.transaction_id for e in loaded] == ["P-1", "P-2"]
    assert [e.mapped.amount_cents for e in loaded] == [-500, -900]
    assert loaded[0].mapped.memo == "COFFEE"
    assert all(e.review_id is not None for e in loaded)
    # raw dict round-trips through raw_json
    assert loaded[0].mapped.raw["transactionId"] == "P-1"

    # Re-persisting the identical download is idempotent (partial-unique dedupe).
    again = import_review.persist_entries(conn, account, build_review(conn, account, _new_rows()))
    assert again == 0
    assert import_review.count_pending(conn, account) == 2


def test_persist_blank_transaction_id_always_inserts(conn, account):
    """Rows with no transaction_id are outside the dedupe index -> each inserts."""
    row = {"postedDate": "2026-08-10", "amount": "5.00", "isDebit": True,
           "statementDescription": "CASH"}
    import_review.persist_entries(conn, account, build_review(conn, account, [row]))
    import_review.persist_entries(conn, account, build_review(conn, account, [row]))
    assert import_review.count_pending(conn, account) == 2


def test_accept_all_saves_new_and_stamps_matches(conn, account):
    """accept_all: NEW rows create register txns, MATCHING rows stamp fitid +
    cleared; afterward pending count is 0 and the accepted rows are in the
    register."""
    existing = ledger.add_transaction(conn, account, "2026-08-20", -2500, payee="Grocer")
    entries = build_review(conn, account, [
        {"transactionId": "N-1", "postedDate": "2026-08-15", "amount": "30.00",
         "isDebit": True, "statementDescription": "COFFEE"},     # NEW
        {"transactionId": "M-1", "postedDate": "2026-08-21", "amount": "25.00",
         "isDebit": True, "statementDescription": "GROCER"},     # MATCHING
    ])
    assert entries[0].is_new and entries[1].is_matching
    import_review.persist_entries(conn, account, entries)

    n = import_review.accept_all(conn, account)
    assert n == 2
    assert import_review.count_pending(conn, account) == 0

    new_txn = conn.execute("SELECT * FROM transactions WHERE fitid='N-1'").fetchone()
    assert new_txn is not None and new_txn["amount"] == -3000
    matched = _txn(conn, existing)
    assert matched["fitid"] == "M-1" and matched["cleared"] == 1


def test_discard_all_marks_discarded_and_blocks_readd(conn, account):
    """discard_all: pending -> 0, nothing entered the register, rows persist as
    discarded so a re-download of the same ids does NOT re-add them."""
    import_review.persist_entries(conn, account, build_review(conn, account, _new_rows()))
    n = import_review.discard_all(conn, account)
    assert n == 2
    assert import_review.count_pending(conn, account) == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    added = import_review.persist_entries(conn, account, build_review(conn, account, _new_rows()))
    assert added == 0
    assert import_review.count_pending(conn, account) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM review_items WHERE state='discarded'").fetchone()[0] == 2


def test_undo_all_matches_reverts_matched_txns(conn, account):
    """After accept_all stamps a matched txn, undo_all_matches restores its prior
    fitid/cleared and returns the review row to pending."""
    existing = ledger.add_transaction(conn, account, "2026-08-20", -2500, payee="Grocer")
    import_review.persist_entries(conn, account, build_review(conn, account, [{
        "transactionId": "M-1", "postedDate": "2026-08-21", "amount": "25.00",
        "isDebit": True, "statementDescription": "GROCER"}]))
    import_review.accept_all(conn, account)
    assert _txn(conn, existing)["fitid"] == "M-1"
    assert _txn(conn, existing)["cleared"] == 1

    n = import_review.undo_all_matches(conn, account)
    assert n == 1
    after = _txn(conn, existing)
    assert after["fitid"] is None
    assert (after["cleared"] or 0) == 0
    assert import_review.count_pending(conn, account) == 1
    assert import_review.load_pending(conn, account)[0].is_matching


def test_manual_match_candidates_and_set_manual_match(conn, account):
    """manual_match_candidates offers a WIDER DATE window than the auto match
    (ordered by date proximity) but ONLY equal same-signed amounts -- the date
    tolerance never loosens the amount match; set_manual_match flips a NEW entry
    to a MATCHING with method 'manual' (and persists it)."""
    # Downloaded row is 77.00 OUT -> -7700 signed cents, dated 2026-08-21. The
    # equal-amount register lines sit OUTSIDE the 3-day auto window (so the row
    # stays NEW) but inside the +/-30d manual window.
    near = ledger.add_transaction(conn, account, "2026-08-26", -7700, payee="Near")
    wide = ledger.add_transaction(conn, account, "2026-09-15", -7700, payee="Wide")
    wrong_amt = ledger.add_transaction(conn, account, "2026-08-22", -1234, payee="WrongAmt")
    wrong_sign = ledger.add_transaction(conn, account, "2026-08-22", 7700, payee="WrongSign")
    far = ledger.add_transaction(conn, account, "2026-06-01", -7700, payee="Far")

    [entry] = build_review(conn, account, [{
        "transactionId": "MM-1", "postedDate": "2026-08-21", "amount": "77.00",
        "isDebit": True, "statementDescription": "MYSTERY"}])
    assert entry.is_new                                  # no auto match at all
    import_review.persist_entries(conn, account, [entry])

    cands = import_review.manual_match_candidates(conn, account, entry.mapped)
    ids = [c["id"] for c in cands]
    assert near in ids and wide in ids                  # equal amount, within +/-30d
    assert wrong_amt not in ids                         # different value -> excluded
    assert wrong_sign not in ids                        # opposite sign -> excluded
    assert far not in ids                               # >30d out
    assert ids.index(near) < ids.index(wide)            # nearest date first

    # A mismatched-amount/sign candidate cannot be forced through the confirm
    # path even when hand-picked past the (now filtered) candidate list.
    with pytest.raises(ValueError):
        import_review.set_manual_match(conn, entry, wrong_sign)
    with pytest.raises(ValueError):
        import_review.set_manual_match(conn, entry, wrong_amt)
    assert entry.is_new                                 # rejected -> unchanged

    import_review.set_manual_match(conn, entry, wide)
    assert entry.is_matching
    assert entry.matched_txn_id == wide
    assert entry.match_method == "manual"
    row = conn.execute(
        "SELECT label, matched_txn_id, match_method, state FROM review_items WHERE id=?",
        (entry.review_id,)).fetchone()
    assert row["label"] == LABEL_MATCHING
    assert row["matched_txn_id"] == wide
    assert row["match_method"] == "manual"
    assert row["state"] == "pending"


def test_manual_match_candidates_no_date_returns_all(conn, account):
    """A mapped row with no date falls back to the whole account's register,
    still restricted to EQUAL same-signed amounts (amount is never loosened)."""
    a = ledger.add_transaction(conn, account, "2020-01-01", -500, payee="A")
    b = ledger.add_transaction(conn, account, "2026-12-31", -500, payee="B")
    other = ledger.add_transaction(conn, account, "2021-01-01", -200, payee="Other")
    mapped = import_review.map_row({"amount": "5.00", "isDebit": True,
                                    "statementDescription": "NODATE"})
    assert mapped.date == ""
    ids = {c["id"] for c in import_review.manual_match_candidates(conn, account, mapped)}
    assert ids == {a, b}                                 # equal -500, both returned
    assert other not in ids                              # different amount excluded


def test_auto_match_is_strict_on_signed_amount_within_date_tolerance(conn, account):
    """The auto matcher matches an equal same-signed amount within the +/-3d date
    tolerance, but NEVER an opposite-sign or off-by-a-cent amount -- even on the
    exact same date. The date tolerance widens the date search alone."""
    # Equal amount, 2 days off (inside the 3-day tolerance) -> MATCHES.
    tgt = ledger.add_transaction(conn, account, "2026-08-18", -5000, payee="Grocer")
    [e1] = build_review(conn, account, [{
        "transactionId": "A-1", "postedDate": "2026-08-20", "amount": "50.00",
        "isDebit": True, "statementDescription": "GROCER"}])
    assert e1.is_matching and e1.matched_txn_id == tgt

    # Opposite sign on the EXACT same date -> NO match (a wrong-sign Venmo row).
    ledger.add_transaction(conn, account, "2026-09-10", 6000, payee="Refund")
    [e2] = build_review(conn, account, [{
        "transactionId": "A-2", "postedDate": "2026-09-10", "amount": "60.00",
        "isDebit": True, "statementDescription": "REFUND"}])
    assert e2.is_new

    # Off by a single cent on the exact same date -> NO match.
    ledger.add_transaction(conn, account, "2026-10-01", -7000, payee="Bill")
    [e3] = build_review(conn, account, [{
        "transactionId": "A-3", "postedDate": "2026-10-01", "amount": "70.01",
        "isDebit": True, "statementDescription": "BILL"}])
    assert e3.is_new


def test_manual_match_rejects_sign_and_amount_mismatch(conn, account):
    """the user's bug: manual match 'would let me match almost anything'. A same-account
    match means the download IS that register line, so the candidate's SIGNED
    amount must equal the downloaded amount to the cent. The date tolerance only
    widens the DATE search; a nearer-dated but wrong-sign/wrong-value row is
    neither OFFERED nor accepted if hand-picked."""
    # Downloaded row: 50.00 OUT -> -5000 signed cents, dated 2026-08-20.
    equal_far = ledger.add_transaction(conn, account, "2026-08-27", -5000, payee="Equal")   # 7d off, equal
    opp_sign = ledger.add_transaction(conn, account, "2026-08-21", 5000, payee="Venmo")      # 1d off, opp sign
    diff_amt = ledger.add_transaction(conn, account, "2026-08-21", -5001, payee="Off1c")     # 1d off, 1c off

    [entry] = build_review(conn, account, [{
        "transactionId": "SM-1", "postedDate": "2026-08-20", "amount": "50.00",
        "isDebit": True, "statementDescription": "MYSTERY"}])
    assert entry.is_new                                  # equal_far is >3d -> no auto match

    # Only the equal same-signed amount is OFFERED, despite opp_sign/diff_amt
    # being NEARER in date.
    ids = [c["id"] for c in import_review.manual_match_candidates(conn, account, entry.mapped)]
    assert ids == [equal_far]

    # Even hand-picking a mismatch (bypassing the candidate list) is REJECTED --
    # no match is created.
    with pytest.raises(ValueError):
        import_review.set_manual_match(conn, entry, opp_sign)
    with pytest.raises(ValueError):
        import_review.set_manual_match(conn, entry, diff_amt)
    assert entry.is_new

    # The exact same-signed amount within the manual tolerance IS accepted.
    import_review.set_manual_match(conn, entry, equal_far)
    assert entry.is_matching and entry.matched_txn_id == equal_far


def test_load_pending_excludes_accepted_and_discarded(conn, account):
    """Returning to a review shows ONLY the still-pending rows -- an accepted
    (saved) row and a discarded row both drop out."""
    rows = [
        {"transactionId": "R-NEW", "postedDate": "2026-08-15", "amount": "30.00",
         "isDebit": True, "statementDescription": "COFFEE"},
        {"transactionId": "R-MATCH", "postedDate": "2026-08-21", "amount": "25.00",
         "isDebit": True, "statementDescription": "GROCER"},
        {"transactionId": "R-DISCARD", "postedDate": "2026-08-16", "amount": "8.00",
         "isDebit": True, "statementDescription": "SNACK"},
    ]
    entries = build_review(conn, account, rows)
    import_review.persist_entries(conn, account, entries)
    by_tid = {e.mapped.transaction_id: e for e in entries}

    # Save one NEW row (single-row, threading its review_id) and discard another.
    import_review.save_new(conn, account, by_tid["R-NEW"].mapped,
                           review_id=by_tid["R-NEW"].review_id)
    conn.execute("UPDATE review_items SET state='discarded' WHERE id=?",
                 (by_tid["R-DISCARD"].review_id,))
    conn.commit()

    remaining = import_review.load_pending(conn, account)
    assert [e.mapped.transaction_id for e in remaining] == ["R-MATCH"]
    assert import_review.count_pending(conn, account) == 1


def test_save_new_backward_compatible_without_review_id(conn, account):
    """Existing callers pass no review_id: save_new still returns the new txn id
    and writes the register, and leaves review_items untouched."""
    [entry] = build_review(conn, account, [{
        "postedDate": "2026-08-15", "amount": "5.00", "isDebit": True,
        "statementDescription": "SNACK"}])
    txn_id = import_review.save_new(conn, account, entry.mapped)
    assert _txn(conn, txn_id) is not None
    assert conn.execute("SELECT COUNT(*) FROM review_items").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# transfer learning (the fix 1): learn the account a transfer statement text
# maps to, then pre-fill + create a real transfer on future imports.
# ---------------------------------------------------------------------------
def test_save_new_transfer_creates_double_entry_and_learns(conn, account):
    from mammon import transfer_rules
    savings = ledger.create_account(conn, "Share Savings", "savings")
    [entry] = build_review(conn, account, [{
        "transactionId": "XT-1", "postedDate": "2026-08-15",
        "amount": "75.00", "isDebit": True,
        "statementDescription": "TRANSFER TO SHARE SAVINGS"}])
    assert entry.mapped.is_transfer and entry.label == LABEL_NEW

    txn_id = import_review.save_new(
        conn, account, entry.mapped, transfer_account_id=savings,
        review_id=entry.review_id)

    t = ledger.get_transaction(conn, txn_id)
    assert t["transfer_account_id"] == savings   # real double-entry leg
    assert t["transfer_pair_id"] is not None
    assert t["fitid"] == "XT-1"                   # source id stamped for dedupe
    assert t["amount"] == -75_00                  # money out of this account
    # the counterparty leg carries the opposite amount
    pair = ledger.get_transaction(conn, t["transfer_pair_id"])
    assert pair["account_id"] == savings and pair["amount"] == 75_00
    # and the statement text -> account mapping was learned
    assert transfer_rules.apply_rules(conn, "TRANSFER TO SHARE SAVINGS") == savings


def test_transfer_prediction_applies_learned_account(conn, account):
    from mammon import transfer_rules
    savings = ledger.create_account(conn, "Share Savings", "savings")
    transfer_rules.upsert_rule(conn, "TRANSFER TO SHARE SAVINGS", savings)
    # build_review pre-fills the learned account on a fresh matching statement
    [entry] = build_review(conn, account, [{
        "postedDate": "2026-09-01", "amount": "20.00", "isDebit": True,
        "statementDescription": "TRANSFER TO SHARE SAVINGS"}])
    assert entry.mapped.transfer_account_id == savings
    assert import_review.predict_transfer_account(conn, entry.mapped) == savings


def test_transfer_account_id_for_name_resolves_bracket_text(conn, account):
    savings = ledger.create_account(conn, "Share Savings", "savings")
    assert import_review.transfer_account_id_for_name(conn, "[Share Savings]") == savings
    assert import_review.transfer_account_id_for_name(conn, "[ share savings ]") == savings
    assert import_review.transfer_account_id_for_name(conn, "Groceries") is None
    assert import_review.transfer_account_id_for_name(conn, "[No Such Account]") is None


# ---------------------------------------------------------------------------
# discard a single review row (the fix 2): stray blank-line rows.
# ---------------------------------------------------------------------------
def test_discard_one_marks_only_that_row_discarded(conn, account):
    entries = build_review(conn, account, [
        {"transactionId": "A", "postedDate": "2026-08-15", "amount": "5.00",
         "isDebit": True, "statementDescription": "COFFEE"},
        {"transactionId": "B", "postedDate": "2026-08-16", "amount": "6.00",
         "isDebit": True, "statementDescription": "LUNCH"}])
    import_review.persist_entries(conn, account, entries)
    by_tid = {e.mapped.transaction_id: e for e in entries}

    import_review.discard_one(conn, by_tid["A"].review_id)

    remaining = import_review.load_pending(conn, account)
    assert [e.mapped.transaction_id for e in remaining] == ["B"]
    # discarded rows stay in the table so a re-download won't re-add them
    again = import_review.persist_entries(
        conn, account, build_review(conn, account, [
            {"transactionId": "A", "postedDate": "2026-08-15", "amount": "5.00",
             "isDebit": True, "statementDescription": "COFFEE"}]))
    assert again == 0


def test_discard_one_none_id_is_noop(conn, account):
    import_review.discard_one(conn, None)   # unpersisted entry: must not raise


# ---------------------------------------------------------------------------
# investment near-matching across export formats
# ---------------------------------------------------------------------------
def _inv_row(conn, aid, date, amount, symbol=None, action="Div", fitid=None):
    conn.execute(
        "INSERT INTO investment_transactions(account_id,date,action,symbol,amount,fitid)"
        " VALUES (?,?,?,?,?,?)", (aid, date, action, symbol, amount, fitid))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _inv_mapped(date, amount, symbol="", tid=""):
    from mammon.import_review import MappedRow
    return MappedRow(transaction_id=tid, account_ref="", date=date,
                     amount_cents=amount, is_investment=True, action="Dividend",
                     symbol=symbol, memo="x")


def test_dividend_matches_across_export_formats(conn, account):
    """The user's report: a year of dividends stored from one export format
    ("ALTY GLOBAL X SUPERDIVIDEND ALTER" -- ticker + name) classified NEW
    against a re-download in another format (bare ticker "ALTY"), because exact
    identity can never bridge two spellings. The leading TICKER token is the
    stable part; same amount + near date + same ticker is the same dividend."""
    stored = _inv_row(conn, account, "2025-12-10", 10010,
                      symbol="ALTY GLOBAL X SUPERDIVIDEND ALTER")
    e = import_review.classify_row(conn, account,
                                   _inv_mapped("2025-12-10", 10010, symbol="ALTY"))
    assert e.label == import_review.LABEL_MATCHING
    assert e.matched_txn_id == stored
    assert e.match_method == "date+amount"


def test_cash_lines_match_only_cash_lines(conn, account):
    """Interest and fees carry no security on either side -> they match each
    other; but a row WITH a security never near-matches one without, so a fee
    cannot swallow a same-amount dividend."""
    interest = _inv_row(conn, account, "2026-01-06", 497, symbol=None,
                        action="IntInc")
    e = import_review.classify_row(conn, account, _inv_mapped("2026-01-06", 497))
    assert (e.label, e.matched_txn_id) == (import_review.LABEL_MATCHING, interest)

    div = _inv_row(conn, account, "2026-02-01", 500, symbol="ALTY GLOBAL X")
    e = import_review.classify_row(conn, account, _inv_mapped("2026-02-01", 500))
    assert e.label == import_review.LABEL_NEW          # blank vs security: no
    e = import_review.classify_row(conn, account,
                                   _inv_mapped("2026-02-01", 500, symbol="QTUM"))
    assert e.label == import_review.LABEL_NEW          # different ticker: no


def test_near_match_respects_the_date_window(conn, account):
    _inv_row(conn, account, "2026-03-10", 999, symbol="ALTY GLOBAL X")
    near = import_review.classify_row(conn, account,
                                      _inv_mapped("2026-03-12", 999, symbol="ALTY"))
    assert near.label == import_review.LABEL_MATCHING  # 2 days: statement drift
    far = import_review.classify_row(conn, account,
                                     _inv_mapped("2026-04-10", 999, symbol="ALTY"))
    assert far.label == import_review.LABEL_NEW        # a month is a new payment


def test_near_match_is_count_aware_and_exact_identity_wins(conn, account):
    a = _inv_row(conn, account, "2026-05-10", 700, symbol="ALTY GLOBAL X")
    b = _inv_row(conn, account, "2026-05-10", 700, symbol="ALTY GLOBAL X")
    rows = [{"transactionId": "", "postedDate": "2026-05-10", "amount": "7.00",
             "isDebit": False, "statementDescription": "d%d" % i} for i in range(3)]
    # Three identical incoming dividends against two stored: two match distinct
    # rows, the surplus stays NEW.
    mapped = [_inv_mapped("2026-05-10", 700, symbol="ALTY") for _ in range(3)]
    claimed = set()
    labels = []
    for m in mapped:
        from mammon.import_review import _find_investment_match
        mid, method = _find_investment_match(conn, account, m, frozenset(claimed))
        if mid:
            claimed.add(mid)
        labels.append(mid)
    assert sorted(x for x in labels if x) == sorted([a, b])
    assert labels.count(None) == 1

    # An exact-identity candidate outranks a near one.
    exact = _inv_row(conn, account, "2026-06-10", 800, symbol="ALTY")
    _near = _inv_row(conn, account, "2026-06-11", 800, symbol="ALTY GLOBAL X")
    e = import_review.classify_row(conn, account,
                                   _inv_mapped("2026-06-10", 800, symbol="ALTY"))
    assert (e.matched_txn_id, e.match_method) == (exact, "identity")


# ---------------------------------------------------------------------------
# Accept All has to agree with accepting each row by hand
# ---------------------------------------------------------------------------
def test_accept_all_applies_the_learned_rename_and_category(conn, account):
    """Regression: Accept All saved the RAW mapped values, so a rename tree the
    user had spent weeks training produced bank gobbledygook the moment they used
    the bulk button instead of accepting rows one at a time."""
    from mammon import category_rules, rename_tree

    cat = ledger.create_category(conn, "Coffee")
    # Train the tree the way accepting rows individually does.
    for _ in range(4):
        rename_tree.learn(conn, "SQ *BLUE BOTTLE COFFEE 4471", "Blue Bottle")
    category_rules.upsert_rule(conn, "BLUE BOTTLE", cat)

    entries = build_review(conn, account, [
        {"transactionId": "N-9", "postedDate": "2026-08-15", "amount": "6.50",
         "isDebit": True,
         "statementDescription": "SQ *BLUE BOTTLE COFFEE 4471"}])
    import_review.persist_entries(conn, account, entries)

    # What the register's pending row WOULD show for this entry.
    predicted, predicted_cat = import_review.predict_fields(
        conn, entries[0].mapped)
    assert predicted == "Blue Bottle"

    assert import_review.accept_all(conn, account) == 1
    txn = conn.execute("SELECT * FROM transactions WHERE fitid='N-9'").fetchone()
    assert txn["payee"] == "Blue Bottle"          # not the statement text
    assert txn["category_id"] == predicted_cat == cat


def test_accept_all_learns_nothing_from_rows_nobody_read(conn, account):
    """A prediction nobody looked at is not evidence. Bulk-accepting an UNKNOWN
    description must not teach the tree that it maps to a tidied copy of itself
    -- that is how a trained tree gets poisoned by one careless click."""
    from mammon import rename_tree

    def taught():
        return conn.execute(
            "SELECT COUNT(*) FROM rename_node_payees").fetchone()[0]

    rename_tree.ensure_bootstrapped(conn)
    before = taught()
    entries = build_review(conn, account, [
        {"transactionId": "N-7", "postedDate": "2026-08-15", "amount": "4.00",
         "isDebit": True, "statementDescription": "POS DEBIT 8842 WHOZIT LLC"}])
    import_review.persist_entries(conn, account, entries)
    assert import_review.accept_all(conn, account) == 1

    # The row still posted, with the tidied payee the pending row would show.
    txn = conn.execute("SELECT * FROM transactions WHERE fitid='N-7'").fetchone()
    assert txn is not None and txn["payee"]
    # ...but the tree was taught nothing by it.
    assert taught() == before
    assert rename_tree.suggest(conn, "POS DEBIT 8842 WHOZIT LLC").action == \
        rename_tree.ACTION_LEAVE

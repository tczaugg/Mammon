"""Regression tests for the Spanish-Fork-Recast doubled-loan-row defect.

The balance-driven recast/regenerate work (dated ``loan_payments``, migration
_V19) could re-materialise a loan's scheduled principal legs from a change's
effective date forward WITHOUT clearing the prior generation, so on the LOAN
side every regenerated payment landed twice while the single mirrored checking
leg was untouched. The principal-only paydown (a genuine PAIRED transfer) and
the final payment (a unique-amount singleton) escaped the doubling.

Two guards are proven here:
  1. ``loans_schedule.create_pending_payment`` is idempotent against a date that
     ALREADY carries a payment -- a posted split payment OR a bare transfer leg
     -- so re-running generation can never double a loan row.
  2. Migration _V21 dedupes any ALREADY-doubled loan rows in existing files,
     keeping exactly one per (date, amount, transfer) group, never touching the
     single checking legs, the paired principal-only paydown, or the final row.
"""
from __future__ import annotations

import pytest

from mammon import db, ledger, loans, loans_schedule

# A small synthetic fixed-rate loan.
PRINCIPAL = 300_000_00
TERM = 360
ORIGINATION = "2024-01-01"
ESCROW = 200_00
PI_PAYMENT = loans.standard_payment(PRINCIPAL, "6.0", TERM)
PAYMENT = PI_PAYMENT + ESCROW


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "loans.db")
    yield c
    c.close()


def _fixed_rate_loan(conn):
    acct = ledger.create_account(conn, "Home Mortgage", "liability",
                                 opening_balance=-PRINCIPAL)
    loans.set_loan_params(
        conn, acct,
        original_principal=PRINCIPAL, term_months=TERM, payment_amount=PAYMENT,
        origination_date=ORIGINATION, interval="monthly",
        rates=[(ORIGINATION, "6.0")],
        extras=[("Escrow", ESCROW, "Taxes + insurance")],
    )
    return acct


def _rows_on(conn, account_id, date):
    return conn.execute(
        "SELECT id FROM transactions WHERE account_id=? AND date=?",
        (account_id, date)).fetchall()


# ---------------------------------------------------------------------------
# Guard 1: generation is idempotent against an already-posted payment
# ---------------------------------------------------------------------------
def test_regenerate_does_not_double_a_posted_split_payment(conn):
    acct = _fixed_rate_loan(conn)
    due = loans_schedule.upcoming_due_dates(conn, acct, ORIGINATION, count=1)[0].date

    # Pre-enter, then POST it (as the importer would: scheduled flips to 0).
    pend = loans_schedule.create_pending_payment(conn, acct, due)
    loans_schedule.merge_import_into_pending(
        conn, pend, date=due, amount_cents=PAYMENT, fitid="F1")
    assert len(_rows_on(conn, acct, due)) == 1

    # Re-running generation for the SAME date (the recast/regenerate trigger)
    # must NOT create a second row -- it returns the existing one.
    again = loans_schedule.create_pending_payment(conn, acct, due)
    assert again == pend
    assert len(_rows_on(conn, acct, due)) == 1

    # And the windowed generator is likewise idempotent, run twice.
    loans_schedule.ensure_pending_payments(conn, acct, due, lead_days=0)
    loans_schedule.ensure_pending_payments(conn, acct, due, lead_days=0)
    assert len(_rows_on(conn, acct, due)) == 1


def test_regenerate_does_not_double_an_existing_transfer_leg(conn):
    """The live regression's shape: the payment is a bare one-sided transfer leg
    (like an imported loan-payment leg), NOT a split pre-entry. Generation must
    still recognise it and refuse to add a second loan-side row."""
    acct = _fixed_rate_loan(conn)
    checking = ledger.create_account(conn, "Checking", "cash")
    due = loans_schedule.upcoming_due_dates(conn, acct, ORIGINATION, count=1)[0].date

    # A bare transfer leg already sits on the due date (no splits, one-sided) --
    # exactly how the importer books a loan-payment leg (raw insert, not the
    # paired create_transfer), so transfer_pair_id stays NULL.
    leg = conn.execute(
        "INSERT INTO transactions(account_id, date, amount, payee, "
        "transfer_account_id, scheduled, cleared) VALUES (?,?,?,?,?,0,0)",
        (acct, due, 1_200_00, "US Bank", checking)).lastrowid
    conn.commit()
    assert len(_rows_on(conn, acct, due)) == 1

    same = loans_schedule.create_pending_payment(conn, acct, due)
    assert same == leg
    assert len(_rows_on(conn, acct, due)) == 1


# ---------------------------------------------------------------------------
# Guard 2: the _V21 dedupe migration repairs already-doubled files
# ---------------------------------------------------------------------------
def _frozen_before_v21(path):
    """A DB frozen at the schema version just before _V21, so calling init_db
    later triggers the in-place _V21 upgrade against pre-existing legacy rows
    (a fresh init_db would run _V21 before any rows exist and never exercise
    the dedupe)."""
    conn = db.connect(path)
    idx = db.MIGRATIONS.index(db._V21)
    for target in range(idx):
        conn.executescript(db.MIGRATIONS[target])
        conn.execute(f"PRAGMA user_version = {target + 1}")
    conn.commit()
    return conn, idx


def _ins(conn, account_id, date, amount, *, transfer_account_id=None,
         transfer_pair_id=None, scheduled=0, cleared=0, reconciled=0,
         import_id=None, payee="US Bank"):
    return conn.execute(
        "INSERT INTO transactions(account_id, date, amount, payee, cleared, "
        "reconciled, scheduled, transfer_account_id, transfer_pair_id, import_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (account_id, date, amount, payee, cleared, reconciled, scheduled,
         transfer_account_id, transfer_pair_id, import_id)).lastrowid


def test_v21_dedupe_collapses_doubled_loan_rows(tmp_path):
    path = tmp_path / "recast.db"
    conn, v21_idx = _frozen_before_v21(path)

    # Two accounts: the loan (in loan_params) and its linked checking.
    checking = conn.execute(
        "INSERT INTO accounts(name, type) VALUES ('Checking','cash')").lastrowid
    loan = conn.execute(
        "INSERT INTO accounts(name, type) VALUES ('Recast','liability')"
    ).lastrowid
    conn.execute(
        "INSERT INTO loan_params(account_id, original_principal, term_months, "
        "payment_amount) VALUES (?,?,?,?)", (loan, 1000_00, 120, 100_00))

    # Regular scheduled principal legs: one REAL checking-side leg each, and on
    # the loan side the imported original PLUS a regeneration DUPLICATE.
    reg_dates = ["2025-02-01", "2025-03-01", "2025-04-01"]
    reg_amts = {"2025-02-01": 127_87, "2025-03-01": 128_46, "2025-04-01": 129_12}
    keep_ids = {}
    for d in reg_dates:
        amt = reg_amts[d]
        # single checking-side leg (must be preserved: checking is not a loan)
        _ins(conn, checking, d, -amt, transfer_account_id=loan)
        # loan-side ORIGINAL (inserted first -> lowest id -> the kept survivor)
        keep_ids[d] = _ins(conn, loan, d, amt, transfer_account_id=checking)
        # loan-side regeneration DUPLICATE
        _ins(conn, loan, d, amt, transfer_account_id=checking)

    # A duplicate where one copy is RECONCILED -> the reconciled row must win.
    rec_keep = _ins(conn, loan, "2025-05-01", 130_00,
                    transfer_account_id=checking, cleared=1, reconciled=1)
    _ins(conn, loan, "2025-05-01", 130_00, transfer_account_id=checking)

    # Principal-only PAYDOWN: a genuine PAIRED transfer (both legs) -> untouched.
    pay_loan = _ins(conn, loan, "2025-06-15", 500_00, transfer_account_id=checking,
                    payee="loan paydown")
    pay_chk = _ins(conn, checking, "2025-06-15", -500_00, transfer_account_id=loan,
                   payee="loan paydown")
    conn.execute("UPDATE transactions SET transfer_pair_id=? WHERE id=?",
                 (pay_chk, pay_loan))
    conn.execute("UPDATE transactions SET transfer_pair_id=? WHERE id=?",
                 (pay_loan, pay_chk))

    # FINAL payment: a unique-amount singleton -> untouched.
    final_id = _ins(conn, loan, "2025-07-01", 812_34, transfer_account_id=checking)

    # Same-date rows with DIFFERENT amounts are NOT duplicates -> both kept.
    diff_a = _ins(conn, loan, "2025-08-01", 136_25, transfer_account_id=checking)
    diff_b = _ins(conn, loan, "2025-08-01", 235_94, transfer_account_id=checking)

    conn.commit()
    before = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (loan,)
    ).fetchone()["c"]
    conn.close()

    # Trigger the in-place _V21 upgrade.
    conn = db.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)

    def loan_rows_on(date):
        return _rows_on(conn, loan, date)

    # Every duplicated regular date now has exactly ONE loan row -- the imported
    # survivor -- and it matches the SINGLE checking leg 1:1.
    removed = before - conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (loan,)
    ).fetchone()["c"]
    assert removed == 4  # 3 regular dups + 1 reconciled-pair dup
    for d in reg_dates:
        ids = [r["id"] for r in loan_rows_on(d)]
        assert ids == [keep_ids[d]]
        loan_ct = len(loan_rows_on(d))
        chk_ct = len(_rows_on(conn, checking, d))
        assert loan_ct == chk_ct == 1  # transfer leg 1:1

    # Reconciled row survived (not the unreconciled copy).
    rec_rows = loan_rows_on("2025-05-01")
    assert [r["id"] for r in rec_rows] == [rec_keep]

    # Paydown pair intact -- both legs present, linkage preserved.
    assert conn.execute("SELECT transfer_pair_id FROM transactions WHERE id=?",
                        (pay_loan,)).fetchone()["transfer_pair_id"] == pay_chk
    assert conn.execute("SELECT 1 FROM transactions WHERE id=?",
                        (pay_chk,)).fetchone() is not None

    # Final payment present exactly once.
    assert [r["id"] for r in loan_rows_on("2025-07-01")] == [final_id]

    # Differing-amount same-date pair: BOTH kept (ambiguous, never guessed away).
    diff_ids = {r["id"] for r in loan_rows_on("2025-08-01")}
    assert diff_ids == {diff_a, diff_b}

    # Idempotent: a second upgrade pass removes nothing more.
    after = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (loan,)
    ).fetchone()["c"]
    conn.close()
    conn = db.init_db(path)
    assert conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE account_id=?", (loan,)
    ).fetchone()["c"] == after
    conn.close()


# ---------------------------------------------------------------------------
# Guard 3: the REAL Spanish-Fork model -- the payment posts on CHECKING as a split
# whose principal leg transfers INTO the loan; the loan side is only that one-sided
# mirror. The synthetic pending-payment tests above never exercised this shape, so
# they stayed green while a real file re-broke. These lock in that (a) the
# different-amount orphan-beside-live-mirror duplication collapses, and (b) an Edit
# Loan "New Payment" (loans_schedule.apply_payment_change, change_type='payment')
# re-splits those posted checking payments forward to the balance-driven model --
# leg == principal, new escrow applied -- WITHOUT doubling, even re-applied.
# ---------------------------------------------------------------------------
def _post_real_payment(conn, loan, checking, date, total):
    """Post a payment the user's way: a checking split of interest + escrow + a [Loan]
    principal transfer leg (which creates the one-sided loan-side mirror). Returns
    the checking transaction id."""
    sp = loans.payment_split(conn, loan, date, total)
    pid = ledger.add_transaction(conn, checking, date, -total, payee="US Bank")
    ledger.set_splits(conn, pid, [
        {"transfer_account_id": loan, "amount": -sp.principal, "memo": "Principal"},
        {"category_id": ledger.resolve_category(conn, "Interest"),
         "amount": -sp.interest, "memo": "Interest"},
        {"category_id": ledger.resolve_category(conn, "Escrow"),
         "amount": -sp.escrow, "memo": "Escrow"},
    ])
    return pid


def _loan_leg_on(conn, loan, date):
    return conn.execute(
        "SELECT id, amount FROM transactions WHERE account_id=? AND date=? "
        "AND transfer_account_id IS NOT NULL AND scheduled=0 ORDER BY id",
        (loan, date)).fetchall()


def test_dedupe_collapses_orphan_beside_live_mirror(conn):
    loan = ledger.create_account(conn, "recast", "liability",
                                 opening_balance=-PRINCIPAL)
    checking = ledger.create_account(conn, "Checking", "cash")
    loans.set_loan_params(
        conn, loan, original_principal=PRINCIPAL, term_months=TERM,
        payment_amount=PAYMENT, origination_date=ORIGINATION, interval="monthly",
        rates=[(ORIGINATION, "6.0")], extras=[("Escrow", ESCROW, "Taxes")])

    dates = ["2024-02-01", "2024-03-01", "2024-04-01"]
    for d in dates:
        _post_real_payment(conn, loan, checking, d, PAYMENT)
        # A STALE orphan leg for the SAME payment, DIFFERENT amount (the
        # reverted-regenerate artefact _V21 cannot collapse). One-sided, no split,
        # not a live mirror.
        conn.execute(
            "INSERT INTO transactions(account_id, date, amount, payee, "
            "transfer_account_id, scheduled) VALUES (?,?,?,?,?,0)",
            (loan, d, PAYMENT // 3 + 111, "US Bank", checking))
    conn.commit()

    for d in dates:                                  # doubled before repair
        assert len(_loan_leg_on(conn, loan, d)) == 2

    removed = loans_schedule.dedupe_loan_payment_legs(conn, loan)
    assert removed == 3
    for d in dates:
        legs = _loan_leg_on(conn, loan, d)
        assert len(legs) == 1                        # 1:1 with the checking leg
        # the SURVIVOR is the live split mirror (referenced by a checking split)
        assert conn.execute(
            "SELECT 1 FROM splits WHERE transfer_pair_id=?", (legs[0]["id"],)
        ).fetchone() is not None
    # idempotent
    assert loans_schedule.dedupe_loan_payment_legs(conn, loan) == 0
    # a LONE mirror with no orphan twin is never touched
    assert loans_schedule.dedupe_loan_payment_legs(conn, loan) == 0


def test_apply_payment_change_resplits_real_checking_payments(conn):
    loan = ledger.create_account(conn, "recast", "liability",
                                 opening_balance=-PRINCIPAL)
    checking = ledger.create_account(conn, "Checking", "cash")
    loans.set_loan_params(
        conn, loan, original_principal=PRINCIPAL, term_months=TERM,
        payment_amount=PAYMENT, origination_date=ORIGINATION, interval="monthly",
        rates=[(ORIGINATION, "6.0")], extras=[("Escrow", ESCROW, "Taxes")])

    NEW_ESCROW = 250_00
    NEW_TOTAL = 2100_00
    EFF = "2024-03-01"
    # Feb posts under the OLD escrow/total; Mar & Apr are the recast draws that
    # imported at the NEW total (as the user's bank drew more).
    _post_real_payment(conn, loan, checking, "2024-02-01", PAYMENT)
    for d in ("2024-03-01", "2024-04-01"):
        _post_real_payment(conn, loan, checking, d, NEW_TOTAL)
    # Add a stale orphan duplicate on a forward date, to prove apply also dedupes.
    conn.execute(
        "INSERT INTO transactions(account_id, date, amount, payee, "
        "transfer_account_id, scheduled) VALUES (?,?,?,?,?,0)",
        (loan, "2024-03-01", 999_00, "US Bank", checking))
    conn.commit()

    # The Edit-Loan save records the new dated escrow (set_loan_params) ...
    loans.add_extra_change(conn, loan, EFF, "Escrow", NEW_ESCROW)
    # ... then invokes the exact handler the OK button calls.
    def apply():
        return loans_schedule.apply_payment_change(
            conn, loan, EFF, change_type="payment", new_payment_amount=NEW_TOTAL)
    apply()

    def check_forward():
        rate = loans._period_rate(loans._D("6.0"), 12)
        prev = None
        for d in ("2024-03-01", "2024-04-01"):
            legs = _loan_leg_on(conn, loan, d)
            assert len(legs) == 1                    # no duplicate, 1:1
            sp = loans.payment_split(conn, loan, d, NEW_TOTAL)
            # model: interest = prior balance x periodic rate
            assert sp.interest == loans._cents(loans._D(sp.balance_before) * rate)
            # principal = total - interest - escrow(new)
            assert sp.escrow == NEW_ESCROW
            assert sp.principal == NEW_TOTAL - sp.interest - NEW_ESCROW
            # the stored transfer leg equals principal
            assert legs[0]["amount"] == sp.principal
            # balance cascades
            if prev is not None:
                assert sp.balance_before == prev
            prev = sp.balance_after
        # Feb (before the effective date) keeps the OLD escrow, untouched.
        feb = loans.payment_split(conn, loan, "2024-02-01", PAYMENT)
        assert feb.escrow == ESCROW

    check_forward()
    # Re-applying (saving Edit Loan twice) is idempotent: still 1 leg per date.
    apply()
    check_forward()


def test_apply_payment_change_preserves_reconciled_loan_transfer_leg(conn):
    """The loan-side transfer leg of a posted checking payment keeps its own 'R'
    across an Edit-Loan resplit. apply_payment_change re-derives the split (the
    funding leg is deleted+recreated), and that leg's per-account reconciled
    status must survive the rebuild -- never silently drop to uncleared. Only the
    reconciled leg keeps R; a leg that was never reconciled stays unreconciled."""
    loan = ledger.create_account(conn, "recast", "liability",
                                 opening_balance=-PRINCIPAL)
    checking = ledger.create_account(conn, "Checking", "cash")
    loans.set_loan_params(
        conn, loan, original_principal=PRINCIPAL, term_months=TERM,
        payment_amount=PAYMENT, origination_date=ORIGINATION, interval="monthly",
        rates=[(ORIGINATION, "6.0")], extras=[("Escrow", ESCROW, "Taxes")])

    NEW_TOTAL = 2100_00
    EFF = "2024-03-01"
    _post_real_payment(conn, loan, checking, "2024-03-01", PAYMENT)
    _post_real_payment(conn, loan, checking, "2024-04-01", PAYMENT)

    # Reconcile ONLY the Mar loan-side leg in the loan account so it reaches 'R'.
    mar = _loan_leg_on(conn, loan, "2024-03-01")[0]
    conn.execute("UPDATE transactions SET cleared=1, reconciled=1 WHERE id=?",
                 (mar["id"],))
    conn.commit()

    loans_schedule.apply_payment_change(
        conn, loan, EFF, change_type="payment", new_payment_amount=NEW_TOTAL)

    # After the resplit the leg is rebuilt (new row id, new principal) but keeps R.
    rebuilt = _loan_leg_on(conn, loan, "2024-03-01")
    assert len(rebuilt) == 1
    mar_after = ledger.get_transaction(conn, rebuilt[0]["id"])
    assert mar_after["reconciled"] == 1 and mar_after["cleared"] == 1
    # The Apr leg was never reconciled -- it must NOT gain R.
    apr = _loan_leg_on(conn, loan, "2024-04-01")[0]
    assert ledger.get_transaction(conn, apr["id"])["reconciled"] == 0
    for d in ("2024-03-01", "2024-04-01"):
        assert len(_loan_leg_on(conn, loan, d)) == 1

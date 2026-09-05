"""Acceptance: interest accrual on a real Recast loan.

the user's loan (account 81 in ``data/mammon_2026.db``) took a $75,000 principal-only
paydown around the 02/2026 payment and re-amortized. Two models have been wrong
here in turn:

  * the ORIGINAL period model did not credit the paydown until the next SCHEDULED
    period, so the payment that actually followed it (posted 02/27/2026) charged
    interest on the pre-paydown balance -- ~$531, one payment too late;
  * the DAY-WEIGHTED model that replaced it credited the paydown immediately but
    prorated the period's quantum by actual day count. The register dates that
    paydown 2026-02-02, one day into the period, so it charged $183.34 where the
    bank charged $169.22 -- and that $14.12 then offset every later payment by 7c.

US Bank charges ONE periodic quantum (annual_rate / 12) per payment regardless of
the period's length -- the 02/27 payment covered 26 days and the 04/01 payment 33,
and both were charged exactly one month. Days never enter the accrual, so a paydown
is not prorated either: it retires principal when it posts and the next payment
accrues a full quantum on the reduced balance. That model reproduces every figure
below to the cent from the register AS IT STANDS -- no date alignment and no
balance reconciliation needed, which is what makes it the right one.

Statement ground truth (interest / principal), to the cent:
    01/30/2026 payment (posted 2026-02-01): interest 537.07, principal 135.77
    01/30/2026 principal-only paydown: 75,000.00 (register dates it 2026-02-02)
    02/27/2026 payment: interest 169.22, principal 503.62
    04/01/2026 payment: interest 166.75, principal 506.09
    05/01/2026 payment: interest 164.27, principal 508.57
    06/01/2026 payment: interest 161.78, principal 511.06
    07/01/2026 payment: interest 159.28, principal 513.56
    07/31/2026 payment (posted 2026-08-01): interest 156.77, principal 516.07

Driven against a COPY of the real DB through the actual ``apply_payment_change``
recompute path (the Edit-Loan save).
"""
from __future__ import annotations

import os
import hashlib
import shutil
from pathlib import Path

import pytest

from mammon import db, ledger, loans, loans_schedule

# Acceptance tests run against a real ledger, and ONLY when one is named
# explicitly via $MAMMON_ACCEPTANCE_DB. They deliberately do NOT fall back to
# probing for data/mammon.db: an unrelated database that merely happened to
# sit at that path made these run against the wrong ledger and fail with
# confusing AttributeErrors, and any test that opens a real ledger by
# accident is one migration away from modifying it.
REAL_DB = Path(os.environ.get("MAMMON_ACCEPTANCE_DB") or "__acceptance_db_not_configured__")
LOAN = 81                      # "Recast"
CHECKING = 2

# (posted date -> (interest_cents, principal_cents)) from the statements above.
GROUND_TRUTH = {
    "2026-02-01": (537_07, 135_77),
    "2026-02-27": (169_22, 503_62),
    "2026-04-01": (166_75, 506_09),
    "2026-05-01": (164_27, 508_57),
    "2026-06-01": (161_78, 511_06),
    "2026-07-01": (159_28, 513_56),
    "2026-08-01": (156_77, 516_07),
}

pytestmark = pytest.mark.skipif(
    not REAL_DB.exists(),
    reason=f"real data file {REAL_DB} not present -- acceptance test needs it",
)


@pytest.fixture(autouse=True)
def _original_unmutated():
    """Guard: the real file is never opened writable."""
    before = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    yield
    assert hashlib.sha256(REAL_DB.read_bytes()).hexdigest() == before, \
        "the ORIGINAL data/mammon_2026.db was mutated"


@pytest.fixture
def real_conn(tmp_path):
    copy = tmp_path / "mammon_copy.db"
    shutil.copy2(REAL_DB, copy)
    c = db.init_db(copy)
    yield c
    c.close()


def _escrow_cat_id(conn):
    return conn.execute("SELECT id FROM categories WHERE name='Escrow'").fetchone()["id"]


def _interest_and_principal(conn, date):
    """(interest_cents, principal_cents) of the posted loan payment on ``date`` --
    the interest is the non-transfer, non-Escrow split; principal is the leg that
    transfers into the loan. Both stored negative on the funding side."""
    tid = conn.execute(
        "SELECT DISTINCT s.transaction_id AS tid FROM splits s "
        "JOIN transactions t ON t.id = s.transaction_id "
        "WHERE s.transfer_account_id=? AND t.date=? AND t.scheduled=0",
        (LOAN, date)).fetchone()["tid"]
    splits = conn.execute("SELECT * FROM splits WHERE transaction_id=?", (tid,)).fetchall()
    escrow = _escrow_cat_id(conn)
    principal = -next(s["amount"] for s in splits if s["transfer_account_id"] == LOAN)
    interest = -next(s["amount"] for s in splits
                     if s["transfer_account_id"] is None
                     and s["amount"] and s["category_id"] != escrow)
    return interest, principal


def _recompute(conn):
    """The real Edit-Loan save recompute: re-split every posted payment forward from
    the first one under the (unchanged) total, re-deriving interest/principal."""
    current_total = loans.get_loan_params(conn, LOAN).payment_amount
    return loans_schedule.apply_payment_change(
        conn, LOAN, "2023-09-01", change_type="payment",
        new_payment_amount=current_total)


# ---------------------------------------------------------------------------
def test_paydown_credited_in_full_to_the_very_next_payment(real_conn):
    """The 02/27 payment on the UNMODIFIED register: not ~$531 (the paydown ignored
    until a later scheduled period) and not $183.34 (the quantum day-weighted
    because the register dates the paydown a day into the period), but the bank's
    $169.22 -- a full month on the post-paydown balance."""
    _recompute(real_conn)
    interest, principal = _interest_and_principal(real_conn, "2026-02-27")
    assert (interest, principal) == (169_22, 503_62)


def test_daycount_reproduces_every_statement_figure_to_the_cent(real_conn):
    """The whole ground-truth table, to the cent, through apply_payment_change."""
    _recompute(real_conn)
    for date, (gi, gp) in GROUND_TRUTH.items():
        interest, principal = _interest_and_principal(real_conn, date)
        assert (interest, principal) == (gi, gp), (
            f"{date}: got interest {interest} principal {principal}, "
            f"want {gi} / {gp}")


def test_recompute_is_idempotent_no_row_duplication(real_conn):
    """Saving Edit Loan twice must not double any leg and must give the same split
    (clear-before-regenerate preserved)."""
    _recompute(real_conn)
    first = {d: _interest_and_principal(real_conn, d) for d in GROUND_TRUTH}
    n_first = real_conn.execute(
        "SELECT COUNT(*) c FROM splits s JOIN transactions t ON t.id=s.transaction_id "
        "WHERE t.account_id=? AND t.date>=?", (CHECKING, "2026-01-01")).fetchone()["c"]

    _recompute(real_conn)                       # save again
    second = {d: _interest_and_principal(real_conn, d) for d in GROUND_TRUTH}
    n_second = real_conn.execute(
        "SELECT COUNT(*) c FROM splits s JOIN transactions t ON t.id=s.transaction_id "
        "WHERE t.account_id=? AND t.date>=?", (CHECKING, "2026-01-01")).fetchone()["c"]

    assert first == second                      # same figures
    assert n_first == n_second                  # no rows added on the second save

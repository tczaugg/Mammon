"""Acceptance tests for the loan wizard's per-loan interest-expense category.

The loan setup wizard used to hard-code the computed-interest split line to a
single constant ("Interest Exp"), but the right category differs per loan (the user's
Recast posts interest to "Int Exp", a rental to "Landlord:Int Exp").
Step 3 (Interest rate history) of the wizard now exposes an interest-expense
category selector; the chosen path persists with the loan and every
generated/regenerated payment split posts interest to THAT category, falling back
to the module default only when it is left unset.

These are driven against a COPY of a real ``data/mammon_2026.db`` (loan
account 81 -- "Recast", whose payments post on checking as a split
that transfers principal into the loan). The original file is NEVER opened
writable: every test works on a tmp-dir copy, and the suite asserts the original
is byte-for-byte unchanged. Per the task, synthetic-only fixtures are NOT trusted
as acceptance for this behavior.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, loans, loans_schedule
from mammon.ui.loan_wizard import LoanSetupWizard
from mammon.tests import fresh_db

# Repo-root/data/mammon_2026.db  (mammon/tests/<this file> -> parents[2] == repo root)
# Acceptance tests run against a real ledger, and ONLY when one is named
# explicitly via $MAMMON_ACCEPTANCE_DB. They deliberately do NOT fall back to
# probing for data/mammon.db: an unrelated database that merely happened to
# sit at that path made these run against the wrong ledger and fail with
# confusing AttributeErrors, and any test that opens a real ledger by
# accident is one migration away from modifying it.
REAL_DB = Path(os.environ.get("MAMMON_ACCEPTANCE_DB") or "__acceptance_db_not_configured__")

LOAN_ACCT = 81                       # "Recast"
FIRST_POSTED = "2023-09-01"          # first posted transfer-payment date on the loan
CHOSEN_CATEGORY = "Int Exp"          # a real interest-expense category for this loan
DEFAULT_INTEREST_PATH = "Loan Payment:Interest"  # what the real rows already carry


pytestmark = pytest.mark.skipif(
    not REAL_DB.exists(),
    reason=f"real data file {REAL_DB} not present -- acceptance test needs it",
)


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


@pytest.fixture(autouse=True)
def _original_unmutated():
    """Guard: the real data file must never be touched. Hash it around each test."""
    before = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    yield
    after = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    assert before == after, "the ORIGINAL data/mammon_2026.db was mutated"


@pytest.fixture
def real_conn(tmp_path):
    """A writable connection to a COPY of a real DB (migrated forward)."""
    copy = tmp_path / "mammon_copy.db"
    shutil.copy2(REAL_DB, copy)
    c = fresh_db(copy)            # applies the interest_category migration
    yield c
    c.close()


def _catpath(conn, cid):
    if cid is None:
        return None
    parts = []
    while cid is not None:
        r = conn.execute("SELECT name, parent_id FROM categories WHERE id=?",
                         (cid,)).fetchone()
        if r is None:
            break
        parts.append(r["name"])
        cid = r["parent_id"]
    return ":".join(reversed(parts))


def _payment_parent(conn, loan_acct, on_date):
    """The checking-side transaction whose split transfers principal into
    ``loan_acct`` on ``on_date`` (the user's real payment shape)."""
    row = conn.execute(
        "SELECT DISTINCT s.transaction_id AS tid FROM splits s "
        "JOIN transactions t ON t.id = s.transaction_id "
        "WHERE s.transfer_account_id=? AND t.date=? AND t.scheduled=0",
        (loan_acct, on_date)).fetchone()
    assert row is not None, f"no posted loan payment into {loan_acct} on {on_date}"
    return row["tid"]


def _interest_line_path(conn, tid, loan_acct):
    """The category path of the interest leg of payment ``tid``: the one split that
    is neither the principal transfer leg nor an escrow/extra line."""
    splits = conn.execute("SELECT * FROM splits WHERE transaction_id=? ORDER BY id",
                          (tid,)).fetchall()
    non_transfer = [s for s in splits if s["transfer_account_id"] is None
                    and s["amount"]]
    interest = [s for s in non_transfer
                if _catpath(conn, s["category_id"]) != "Escrow"]
    assert len(interest) == 1, f"expected one interest line, got {len(interest)}"
    return _catpath(conn, interest[0]["category_id"])


def _splits_total(conn, tid):
    return sum(s["amount"] for s in conn.execute(
        "SELECT amount FROM splits WHERE transaction_id=?", (tid,)).fetchall())


def _txn_amount(conn, tid):
    return conn.execute("SELECT amount FROM transactions WHERE id=?",
                        (tid,)).fetchone()["amount"]


def _clear_interest_category(conn, loan_acct):
    """Normalize the loan's STORED interest category to unset (NULL).

    the user has legitimately set ``interest_category='Int Exp'`` on this loan in the
    real db, so a fresh copy no longer starts unset. Each test that needs the
    "never set one" precondition establishes it here rather than assuming the
    live copy's mutable state.
    """
    conn.execute("UPDATE loan_params SET interest_category=NULL WHERE account_id=?",
                 (loan_acct,))
    conn.commit()


def _establish_unset_default(conn, loan_acct):
    """Establish the full "unset" precondition these tests assume: every posted
    payment's interest leg posts to ``DEFAULT_INTEREST_PATH`` AND the loan's stored
    ``interest_category`` is NULL -- independent of whatever the live db copy holds.

    the user has since set ``interest_category='Int Exp'`` on this loan and recategorized
    its posted interest, so the copy no longer starts in the default state. We
    (1) point the interest category at the default path and drive the wizard's own
    regenerate path to force every posted interest leg back onto it, then (2) clear
    the stored category to NULL. Because the unset-regeneration rule PRESERVES each
    interest leg's existing category (rather than forcing a constant), the posted
    legs stay on ``DEFAULT_INTEREST_PATH`` after the config is cleared.
    """
    conn.execute("UPDATE loan_params SET interest_category=? WHERE account_id=?",
                 (DEFAULT_INTEREST_PATH, loan_acct))
    conn.commit()
    lp = loans.get_loan_params(conn, loan_acct)
    loans_schedule.apply_payment_change(
        conn, loan_acct, FIRST_POSTED,
        change_type="payment", new_payment_amount=lp.payment_amount)
    _clear_interest_category(conn, loan_acct)


# ---------------------------------------------------------------------------
# criterion 1 + 2: the selector exists, its value persists and reloads
# ---------------------------------------------------------------------------
def test_step3_selector_exists_and_starts_empty(qapp, real_conn):
    # Precondition (established, not assumed): this loan has no stored interest
    # category, so the selector must load blank.
    _clear_interest_category(real_conn, LOAN_ACCT)
    w = LoanSetupWizard(real_conn, account_id=LOAN_ACCT)
    # Step 3 (index 2) is "Interest rate history"; the selector lives on it.
    assert w.STEP_TITLES[2] == "Interest rate history"
    assert hasattr(w, "interest_category")
    # A loan that never set one loads blank (=> module-default fallback).
    assert loans.get_loan_params(real_conn, LOAN_ACCT).interest_category is None
    assert w.interest_category.currentText() == ""


def test_wizard_save_persists_and_reloads_interest_category(qapp, real_conn):
    w = LoanSetupWizard(real_conn, account_id=LOAN_ACCT)
    w.interest_category.setCurrentText(CHOSEN_CATEGORY)
    assert w.save() is True

    # Persists on the loan configuration...
    lp = loans.get_loan_params(real_conn, LOAN_ACCT)
    assert lp.interest_category == CHOSEN_CATEGORY

    # ...and reloads into a freshly-opened wizard (the Edit Loan path).
    w2 = LoanSetupWizard(real_conn, account_id=LOAN_ACCT)
    assert w2.interest_category.currentText() == CHOSEN_CATEGORY


# ---------------------------------------------------------------------------
# criterion 3: regenerated payment splits post interest to the chosen category,
# driven entirely through the actual wizard save -> apply_payment_change path.
# ---------------------------------------------------------------------------
def test_wizard_save_regenerates_interest_to_chosen_category(qapp, real_conn):
    # Establish the 'Before' precondition ourselves rather than trusting the live
    # copy: interest category unset and the posted interest posting to the default.
    _establish_unset_default(real_conn, LOAN_ACCT)
    # Before: the real posted payment's interest posts to Quicken's category.
    tid = _payment_parent(real_conn, LOAN_ACCT, FIRST_POSTED)
    assert _interest_line_path(real_conn, tid, LOAN_ACCT) == DEFAULT_INTEREST_PATH

    w = LoanSetupWizard(real_conn, account_id=LOAN_ACCT)
    w.interest_category.setCurrentText(CHOSEN_CATEGORY)
    # A pure new-total row makes persist() drive apply_payment_change (the wizard's
    # own regenerate path) from FIRST_POSTED forward -- no separate call needed.
    total = loans.get_loan_params(real_conn, LOAN_ACCT).payment_amount
    w.add_extra_row(new_payment=f"{total / 100:.2f}", effective_date=FIRST_POSTED)
    assert w.save() is True

    # After: the SAME posted payment's interest now posts to the chosen category,
    # and the split still reconciles to the cent (principal absorbed the change).
    tid = _payment_parent(real_conn, LOAN_ACCT, FIRST_POSTED)
    assert _interest_line_path(real_conn, tid, LOAN_ACCT) == CHOSEN_CATEGORY
    assert _splits_total(real_conn, tid) == _txn_amount(real_conn, tid)

    # And it is not a one-off: several posted payments were re-categorized.
    parents = real_conn.execute(
        "SELECT DISTINCT s.transaction_id AS tid FROM splits s "
        "JOIN transactions t ON t.id = s.transaction_id "
        "WHERE s.transfer_account_id=? AND t.scheduled=0 AND t.date>=?",
        (LOAN_ACCT, FIRST_POSTED)).fetchall()
    changed = 0
    for p in parents:
        try:
            if _interest_line_path(real_conn, p["tid"], LOAN_ACCT) == CHOSEN_CATEGORY:
                changed += 1
        except AssertionError:
            pass                      # a skipped, oddly-shaped parent -- ignore
    assert changed >= 5


# ---------------------------------------------------------------------------
# fallback: unset interest category preserves prior behavior (Quicken category)
# ---------------------------------------------------------------------------
def test_unset_interest_category_preserves_existing(qapp, real_conn):
    # Precondition (established, not assumed): the loan's interest category is
    # unset AND its posted interest already posts to the default path.
    _establish_unset_default(real_conn, LOAN_ACCT)
    w = LoanSetupWizard(real_conn, account_id=LOAN_ACCT)
    assert w.interest_category.currentText() == ""      # leave it unset
    total = loans.get_loan_params(real_conn, LOAN_ACCT).payment_amount
    w.add_extra_row(new_payment=f"{total / 100:.2f}", effective_date=FIRST_POSTED)
    assert w.save() is True

    # apply_payment_change ran, but with no chosen category the interest leg keeps
    # its stored (Quicken) category rather than being forced to a constant.
    tid = _payment_parent(real_conn, LOAN_ACCT, FIRST_POSTED)
    assert _interest_line_path(real_conn, tid, LOAN_ACCT) == DEFAULT_INTEREST_PATH
    assert loans.get_loan_params(real_conn, LOAN_ACCT).interest_category is None

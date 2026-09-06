"""Regression tests for four user-reported Loan Setup wizard defects.

The wizard (:class:`mammon.ui.loan_wizard.LoanSetupWizard`) had four rough edges:

1. Step 1 "Paid from" was a plain combo that only jumped to the first item
   matching the typed letter -- unlike every other picker in the app. It is now
   the register's own category/transfer input machinery
   (:func:`mammon.ui.delegates.make_category_combo`) restricted to ACCOUNTS ONLY,
   so it auto-completes, while the account-name -> account-id mapping and the
   "(not set)" default survive.

2. Step 3 (rates) and Step 4 (extras) effective-date columns stored/showed RAW
   ISO text, ignoring the ``ui/prefs.date_format`` preference. They are now the
   app's standard date editor (:func:`mammon.ui.delegates.make_date_edit`):
   storage/domain stay ISO, only display/entry follow the preference.

3. The Step 5 "payment too small" check pitted the step-2 (initial) payment
   against a step-4-edited (current) escrow, so a consistent escrow+payment edit
   false-tripped. It now compares interest + extras + the payment ALL in force at
   the SAME period (``loans._active_payment`` / ``_active_extras`` /
   ``_period_rate`` at the first-payment date).

4. A payment whose computed principal is NEGATIVE (interest + escrow exceed the
   payment, so the payment would INCREASE the balance) now gets its own distinct
   warning, separate from the amortization "too small" message; and a valid
   adjustment leaves no uncategorized split remainder (the register's warning
   triangle clears).

Qt-driven, so the offscreen platform is selected before importing PyQt5, exactly
like the sibling wizard test modules.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mammon import db, ledger, loans
from mammon.ui import prefs
from mammon.ui.delegates import date_edit_iso
from mammon.ui.models import fmt_date
from mammon.ui.loan_wizard import LoanSetupWizard, _FUNDING_UNSET, _NEW_ACCOUNT


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path):
    """Keep the date-format preference (QSettings) out of the real user config
    and fresh per test."""
    from PyQt5.QtCore import QSettings
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / "qs"))
    yield


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "wizfix.db")
    yield c
    c.close()


def _setup_new_loan(w, *, new_name="Test Loan", principal=100_000.0, rate="5",
                    term=360, payment=600.0, first="2025-01-15"):
    """Drive the wizard's new-loan path to a minimal valid state (one rate row)."""
    from PyQt5.QtCore import QDate
    w.account_combo.setCurrentIndex(w.account_combo.findData(_NEW_ACCOUNT))
    w.new_name.setText(new_name)
    w.principal.setValue(principal)
    w.term_months.setValue(term)
    w.payment.setValue(payment)
    y, m, d = (int(x) for x in first.split("-"))
    w.first_payment.setDate(QDate(y, m, d))
    w.add_rate_row(first, rate)


# ---------------------------------------------------------------------------
# DEFECT 1: "Paid from" is an editable, account-only auto-complete input
# ---------------------------------------------------------------------------
def test_paid_from_is_an_editable_account_only_completer(qapp, conn):
    chk = ledger.create_account(conn, "Checking", "checking")
    sav = ledger.create_account(conn, "Savings", "savings")
    ledger.create_account(conn, "Visa", "credit")
    ledger.create_account(conn, "Brokerage", "investment")   # not fundable
    ledger.create_account(conn, "Mortgage", "liability")     # not fundable
    ledger.resolve_category(conn, "Groceries")               # a CATEGORY must not leak in

    w = LoanSetupWizard(conn)
    combo = w.funding_combo
    # It is the standard auto-complete input, not a bare combo.
    assert combo.isEditable()
    assert combo.completer() is not None

    # Account-only choices: the fundable accounts, and no category.
    choices = set(combo.completer()._choices)
    assert choices == {"Checking", "Savings", "Visa"}
    assert "Groceries" not in choices and "Brokerage" not in choices
    assert "Mortgage" not in choices

    # The "(not set)" default is preserved (id None), and a chosen name reads back
    # its account id via the preserved name -> id mapping.
    assert combo.itemText(0) == _FUNDING_UNSET
    assert w._funding_account_id() is None
    combo.setCurrentText("Savings")
    assert w._funding_account_id() == sav
    combo.setCurrentText("Checking")
    assert w._funding_account_id() == chk
    w.deleteLater()


# ---------------------------------------------------------------------------
# DEFECT 2: effective-date cells follow the date-format preference, round-trip ISO
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind,col", [("rate", 0), ("extra", 2)])
def test_effective_date_cell_follows_prefs_and_roundtrips(qapp, conn, kind, col):
    prefs.set_date_format("DD/MM/YYYY")                      # a non-ISO, non-default fmt
    w = LoanSetupWizard(conn)
    iso = "2025-01-15"
    if kind == "rate":
        w.add_rate_row(iso, "5")
        table = w.rates_table
    else:
        w.add_extra_row("Escrow", "250.00", "", iso, "")
        table = w.extras_table

    edit = table.cellWidget(0, col)
    assert edit is not None                                  # a date editor, not raw text
    # Rendered in the chosen non-ISO format -- NOT the stored ISO string.
    shown = edit.date().toString(edit.displayFormat())
    assert shown == fmt_date(iso) == "15/01/2025"
    assert shown != iso
    # ...and it round-trips back to ISO for storage/domain.
    assert date_edit_iso(edit) == iso
    assert w._date_cell_iso(table, 0, col) == iso
    # The collected value is ISO regardless of the display format.
    v = w._collect()
    if kind == "rate":
        assert v["rates"][0][0] == iso
    else:
        assert v["extras"][0][2] == iso
    w.deleteLater()


# ---------------------------------------------------------------------------
# DEFECT 3: time-aligned "too small" check -- same-period payment vs escrow
# ---------------------------------------------------------------------------
def test_same_period_payment_covers_interest_escrow_and_other(qapp, conn):
    """A payment (in force at the first period) that covers that period's
    interest + escrow + another periodic extra passes validation."""
    first = "2025-01-15"
    w = LoanSetupWizard(conn)
    _setup_new_loan(w, payment=600.0)          # step-2 payment deliberately low
    # first interest ~= 100000 * 5% / 12 = 416.67
    w.add_extra_row("Escrow", "200.00", "", first, "720.00")   # New total for this date
    w.add_extra_row("PMI", "50.00", "", first, "")             # +another periodic amount
    ok, msg = w.validate()
    assert ok, msg                              # 720 - 416.67 - 200 - 50 = 53.33 > 0
    w.deleteLater()


def test_step4_consistent_escrow_and_payment_edit_no_longer_trips(qapp, conn):
    """Proves the fix is time-alignment, not the old initial-vs-current mismatch:
    the SAME raised escrow fails against the step-2 payment but passes once the
    step-4 New-total payment for that period is raised to match it."""
    first = "2025-01-15"
    # Escrow raised to 250 with the step-2 payment (600) left untouched: the
    # payment in force really is too small, so validation correctly fails.
    w1 = LoanSetupWizard(conn)
    _setup_new_loan(w1, payment=600.0)
    w1.add_extra_row("Escrow", "250.00", "", first, "")        # no New-total override
    assert w1.validate()[0] is False
    w1.deleteLater()

    # Same escrow, but the New-total payment for that SAME period is raised to
    # cover it -- the consistent step-4 edit that used to false-trip now passes.
    w2 = LoanSetupWizard(conn)
    _setup_new_loan(w2, payment=600.0)
    w2.add_extra_row("Escrow", "250.00", "", first, "700.00")
    ok, msg = w2.validate()
    assert ok is True, msg                      # 700 - 416.67 - 250 = 33.33 > 0
    w2.deleteLater()


def test_payment_exactly_covering_interest_and_escrow_is_too_small(qapp, conn):
    """principal == 0 (covers interest+escrow but nothing toward principal) is the
    amortization 'too small' case, and NOT the loan-increase message."""
    w = LoanSetupWizard(conn)
    _setup_new_loan(w, payment=416.67)          # exactly the first period's interest
    ok, msg = w.validate()
    assert ok is False
    assert "too small" in msg.lower()
    assert "increase" not in msg.lower()
    w.deleteLater()


# ---------------------------------------------------------------------------
# DEFECT 4: negative principal -> distinct loan-increase warning; clean splits
# ---------------------------------------------------------------------------
def test_negative_principal_returns_a_distinct_loan_increase_warning(qapp, conn):
    """A payment SMALLER than interest + escrow would grow the balance; that gets
    its own message, separate from the 'too small' amortization one, and blocks
    the save so no negative-principal split is ever persisted."""
    first = "2025-01-15"
    w = LoanSetupWizard(conn)
    _setup_new_loan(w, payment=600.0)
    # Escrow 300 with a 600 payment: 600 - 416.67 - 300 = -116.67 (principal < 0).
    w.add_extra_row("Escrow", "300.00", "", first, "600.00")
    ok, msg = w.validate()
    assert ok is False
    assert "increase" in msg.lower()            # the distinct loan-increase warning
    assert "too small" not in msg.lower()
    assert w.save() is False                     # and it is a hard gate: nothing persists
    w.deleteLater()


@pytest.fixture
def funded_loan(conn):
    """A loan paid from checking with two posted payments, each a clean split of
    interest + escrow + a [Loan] principal leg (the user's real payment shape)."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=1_000_000)
    loan = ledger.create_account(conn, "Mortgage", "liability",
                                 opening_balance=-11_339_432)
    ledger.resolve_category(conn, "Int Exp")
    ledger.resolve_category(conn, "Escrow")
    loans.set_loan_params(conn, loan, original_principal=11_339_432,
                          origination_date="2024-12-01", term_months=357,
                          payment_amount=126_899, interval="monthly",
                          rates=[("2024-12-01", "5.25")],
                          extras=[("Escrow", 25_000, "Escrow")],
                          interest_category="Int Exp", funding_account_id=chk)
    for date in ("2025-01-01", "2025-02-01"):
        split = loans.payment_split(conn, loan, date, 126_899)
        tid = ledger.add_transaction(conn, chk, date, -126_899, payee="US Bank")
        ledger.set_splits(conn, tid, [
            {"category_id": ledger.resolve_category(conn, "Int Exp"),
             "amount": -split.interest},
            {"category_id": ledger.resolve_category(conn, "Escrow"),
             "amount": -split.escrow},
            {"transfer_account_id": loan, "amount": -split.principal}])
    return {"chk": chk, "loan": loan}


def _posted_parents(conn, loan):
    rows = conn.execute(
        "SELECT DISTINCT s.transaction_id AS tid FROM splits s "
        "JOIN transactions t ON t.id = s.transaction_id "
        "WHERE s.transfer_account_id=? AND t.scheduled=0", (loan,)).fetchall()
    return [r["tid"] for r in rows]


def test_split_adjustment_leaves_no_uncategorized_remainder(qapp, conn, funded_loan):
    """After the wizard adjusts payments (escrow + New-total change), every posted
    payment's split still reconciles to the cent -- uncategorized_split_amount is 0,
    so the register's warning triangle clears."""
    loan = funded_loan["loan"]
    parents = _posted_parents(conn, loan)
    assert len(parents) == 2

    w = LoanSetupWizard(conn, account_id=loan)
    # Mid-loan escrow bump on 2025-02-01 with the total raised by the same delta,
    # so the payment stays large enough (positive principal) -- a valid change that
    # re-splits the 2025-02-01 payment.
    w.add_extra_row("Escrow", "300.00", "", "2025-02-01", "1318.99")
    assert w.save() is True, w.validate()[1]

    for tid in _posted_parents(conn, loan):
        assert ledger.uncategorized_split_amount(conn, tid) == 0
        legs = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM splits WHERE transaction_id=?",
            (tid,)).fetchone()["s"]
        amt = conn.execute("SELECT amount FROM transactions WHERE id=?",
                           (tid,)).fetchone()["amount"]
        assert legs == amt                       # legs reconcile to the payment total
    w.deleteLater()

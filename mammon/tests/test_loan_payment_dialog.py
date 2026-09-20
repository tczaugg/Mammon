"""Enter Payment… on a loan register's gear menu, and its dialog: the way to
post a loan payment when no pre-entry stands, gray while one does."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

from mammon import db, ledger, loans, loans_schedule
from mammon.ui.delegates import date_edit_iso
from mammon.ui.loan_payment_dialog import EnterLoanPaymentDialog
from mammon.ui.widgets import RegisterWidget
from mammon.tests import fresh_db

PAYMENT = 1268_99


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "enter.db")
    yield c
    c.close()


@pytest.fixture
def house(conn):
    """Checking pays a mortgage the Quicken way (see test_loan_funding)."""
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=10000_00)
    loan = ledger.create_account(conn, "Mortgage", "liability", opening_balance=-113394_32)
    ledger.resolve_category(conn, "Int Exp")
    ledger.resolve_category(conn, "Escrow")
    loans.set_loan_params(conn, loan, original_principal=113394_32,
                          origination_date="2025-12-01", term_months=357,
                          payment_amount=PAYMENT, interval="monthly",
                          rates=[("2025-12-01", "5.25")],
                          extras=[("Escrow", 250_00, "Escrow")],
                          interest_category="Int Exp")
    for date in ("2026-01-01", "2026-02-01"):
        split = loans.payment_split(conn, loan, date, PAYMENT)
        tid = ledger.add_transaction(conn, chk, date, -PAYMENT, payee="US Bank")
        ledger.set_splits(conn, tid, [
            {"category_id": ledger.resolve_category(conn, "Int Exp"), "amount": -split.interest},
            {"category_id": ledger.resolve_category(conn, "Escrow"), "amount": -split.escrow},
            {"transfer_account_id": loan, "amount": -split.principal}])
    return {"chk": chk, "loan": loan}


def _legs(conn, tid):
    return {s["category_label"]: s["amount"] for s in ledger.get_splits(conn, tid)}


def test_gear_offers_enter_payment_for_a_configured_loan_and_names_a_pending(
        qapp, conn, house):
    chk, loan = house["chk"], house["loan"]
    w = RegisterWidget(conn, loan)
    w._sync_loan_actions()
    tb = w.toolbar
    assert tb.act_loan.isVisible() and tb.act_loan.text() == "Edit Loan…"
    assert tb.act_enter_payment.isVisible() and tb.act_enter_payment.isEnabled()
    assert "stands" not in tb.act_enter_payment.toolTip()
    # A standing pre-entry does not block Enter Payment (that is the month you
    # may need to pay from elsewhere); the tooltip names it instead.
    pid = loans_schedule.create_pending_payment(conn, loan, "2026-03-01")
    w._sync_loan_actions()
    assert tb.act_enter_payment.isEnabled()
    assert "stands" in tb.act_enter_payment.toolTip()
    ledger.delete_transaction(conn, pid)
    w._sync_loan_actions()
    assert "stands" not in tb.act_enter_payment.toolTip()
    # Neither loan action on a plain cash register.
    c = RegisterWidget(conn, chk)
    c._sync_loan_actions()
    assert not c.toolbar.act_enter_payment.isVisible()
    assert not c.toolbar.act_loan.isVisible()
    w.deleteLater()
    c.deleteLater()


def test_dialog_previews_the_split_and_posts_on_the_funder(qapp, conn, house):
    chk, loan = house["chk"], house["loan"]
    dlg = EnterLoanPaymentDialog(conn, loan, today="2026-02-15")
    assert date_edit_iso(dlg.date_edit) == "2026-03-01"        # next unpaid period
    assert dlg.payee.text() == "US Bank" and dlg.amount.text() == "1268.99"
    text = dlg.breakdown.text()
    assert "Int Exp" in text and "Escrow" in text and "[Mortgage]" in text
    assert dlg.validate() == (True, "")

    tid = loans_schedule.enter_payment(conn, loan, **dlg.values())
    row = ledger.get_transaction(conn, tid)
    assert (row["account_id"], row["date"], row["amount"], row["payee"],
            row["scheduled"]) == (chk, "2026-03-01", -PAYMENT, "US Bank", 0)
    legs = _legs(conn, tid)
    assert legs["[Mortgage]"] < 0 and legs["Int Exp"] < 0 and legs["Escrow"] == -250_00
    assert sum(legs.values()) == -PAYMENT
    mirror = conn.execute(
        "SELECT t.scheduled FROM transactions t JOIN splits s ON s.transfer_pair_id=t.id "
        "WHERE s.transaction_id=?", (tid,)).fetchone()
    assert mirror["scheduled"] == 0
    assert loans_schedule.next_due_date(conn, loan, "2026-02-15") == "2026-04-01"

    # The same period again: the preview says so; saving would be an extra payment.
    dlg._refresh_breakdown()
    assert "already covers" in dlg.breakdown.text()
    # An amount that does not cover interest and extras cannot be saved.
    dlg.amount.setText("100")
    ok, msg = dlg.validate()
    assert not ok and "does not cover" in msg
    warned = []
    dlg._warn = warned.append
    dlg.accept()
    assert warned == [msg] and dlg.result() != dlg.Accepted
    dlg.deleteLater()


def test_enter_payment_restates_a_standing_pre_entry(conn, house):
    """A standing pre-entry is a placeholder: Enter posts it with what the
    user entered rather than leaving a second payment beside it."""
    loan = house["loan"]
    pid = loans_schedule.create_pending_payment(conn, loan, "2026-03-01")
    assert loans_schedule.pending_payment(conn, loan)[0] == pid
    assert loans_schedule.enter_payment(conn, loan, "2026-03-01",
                                        amount_cents=999_99) == pid
    row = ledger.get_transaction(conn, pid)
    assert row["scheduled"] == 0 and row["amount"] == -999_99
    assert loans_schedule.pending_payment(conn, loan) is None

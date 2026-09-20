"""Pending pre-entries as the user meets them: gray in the register with a way
to post them by hand; Enter Payment paying from another account once and
restating a standing pre-entry; Loan Setup moving standing pre-entries when
"Paid from" changes; and the manager's Suggest… finding regular payees."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from mammon import db, ledger, loans, loans_schedule, scheduled
from mammon.ui.loan_payment_dialog import EnterLoanPaymentDialog
from mammon.ui.loan_wizard import LoanSetupWizard
from mammon.ui.scheduled_payments_dialog import SuggestRecurringDialog
from mammon.ui.widgets import RegisterWidget
from mammon.tests import fresh_db

PAYMENT = 1268_99


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def conn(tmp_path):
    c = fresh_db(tmp_path / "pending.db")
    yield c
    c.close()


@pytest.fixture
def house(conn):
    chk = ledger.create_account(conn, "Checking", "checking", opening_balance=10000_00)
    card = ledger.create_account(conn, "Visa", "credit", opening_balance=0)
    sav = ledger.create_account(conn, "Savings", "savings", opening_balance=5000_00)
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
    return {"chk": chk, "card": card, "sav": sav, "loan": loan}


def _mirror(conn, tid):
    row = conn.execute("SELECT transfer_pair_id FROM splits WHERE transaction_id=? "
                       "AND transfer_pair_id IS NOT NULL", (tid,)).fetchone()
    return ledger.get_transaction(conn, row["transfer_pair_id"]) if row else None


# ---------------------------------------------------------------------------
# the register shows a pending row and can post it
# ---------------------------------------------------------------------------
def test_pending_row_is_gray_and_posts_by_hand_or_when_cleared(qapp, conn, house):
    chk, loan = house["chk"], house["loan"]
    pid = loans_schedule.create_pending_payment(conn, loan, "2026-03-01")
    w = RegisterWidget(conn, chk)
    m = w.model
    row = m.row_for_txn(pid)
    assert row is not None and m.is_scheduled_row(row)
    idx = m.index(row, m.PAYEE)
    assert m.data(idx, Qt.ForegroundRole).color().name() == "#8a8a8a"
    assert "Pending pre-entry" in m.data(idx, Qt.ToolTipRole)
    posted_row = m.row_for_txn(house_posted := conn.execute(
        "SELECT id FROM transactions WHERE account_id=? AND date='2026-02-01'",
        (chk,)).fetchone()["id"])
    assert not m.is_scheduled_row(posted_row)
    assert m.data(m.index(posted_row, m.PAYEE), Qt.ToolTipRole) is None
    # Enter Pending Payment posts it, mirror included.
    assert m.post_row(row)
    assert ledger.get_transaction(conn, pid)["scheduled"] == 0
    assert _mirror(conn, pid)["scheduled"] == 0
    # Marking a pending row cleared posts it too.
    pid2 = loans_schedule.create_pending_payment(conn, loan, "2026-04-01")
    m.reload()
    assert m.toggle_cleared(m.row_for_txn(pid2))
    r2 = ledger.get_transaction(conn, pid2)
    assert (r2["cleared"], r2["scheduled"]) == (1, 0)
    assert _mirror(conn, pid2)["scheduled"] == 0
    assert house_posted
    w.deleteLater()


# ---------------------------------------------------------------------------
# Enter Payment: pay from, and a standing pre-entry
# ---------------------------------------------------------------------------
def test_enter_payment_dialog_pays_from_another_account_and_restates_a_pending(
        qapp, conn, house):
    chk, card, loan = house["chk"], house["card"], house["loan"]
    pid = loans_schedule.create_pending_payment(conn, loan, "2026-03-01")
    # The gear stays usable while the pre-entry stands, and says what Enter does.
    w = RegisterWidget(conn, loan)
    w._sync_loan_actions()
    assert w.toolbar.act_enter_payment.isEnabled()
    assert "stands" in w.toolbar.act_enter_payment.toolTip()
    dlg = EnterLoanPaymentDialog(conn, loan, today="2026-02-15")
    assert dlg.pay_from.currentData() == chk                     # the loan's own funder
    assert dlg.pay_from.findData(loan) == -1                     # never from itself
    dlg.pay_from.setCurrentIndex(dlg.pay_from.findData(card))
    assert "Posts on Visa" in dlg.breakdown.text()
    v = dlg.values()
    assert v["funding_account_id"] == card and v["date"] == "2026-04-01"  # first unpaid
    dlg.date_edit.setDate(dlg.date_edit.date().addMonths(-1))   # the standing period
    assert "stands on Checking" in dlg.breakdown.text()
    v = dlg.values()
    assert v["date"] == "2026-03-01"
    tid = loans_schedule.enter_payment(conn, loan, **v)
    row = ledger.get_transaction(conn, tid)
    assert (row["account_id"], row["amount"], row["scheduled"]) == (card, -PAYMENT, 0)
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=? AND "
                        "date='2026-03-01'", (chk,)).fetchone()["c"] == 0
    assert loans.funding_account(conn, loan) == chk                # the default kept
    assert loans_schedule.pending_payment(conn, loan) is None
    dlg.deleteLater()
    w.deleteLater()


# ---------------------------------------------------------------------------
# Loan Setup: a new "Paid from" moves the standing pre-entries
# ---------------------------------------------------------------------------
def test_loan_setup_moves_standing_pre_entries_to_the_new_account(qapp, conn, house):
    chk, sav, loan = house["chk"], house["sav"], house["loan"]
    pid = loans_schedule.create_pending_payment(conn, loan, "2026-03-01")
    assert ledger.get_transaction(conn, pid)["account_id"] == chk
    wiz = LoanSetupWizard(conn, account_id=loan)
    wiz.funding_combo.setCurrentIndex(wiz.funding_combo.findData(sav))
    assert wiz.save() is True
    assert loans.funding_account(conn, loan) == sav
    standing = loans_schedule.standing_pre_entries(conn, loan)
    assert len(standing) == 1 and standing[0][1:] == ("2026-03-01", sav)
    assert conn.execute("SELECT COUNT(*) c FROM transactions WHERE account_id=? AND "
                        "scheduled=1", (chk,)).fetchone()["c"] == 0
    wiz.deleteLater()


# ---------------------------------------------------------------------------
# Suggest…: regular payees from history
# ---------------------------------------------------------------------------
def test_suggest_recurring_finds_steady_payees_and_adds_the_ticked_ones(qapp, conn, house):
    chk = house["chk"]
    util = ledger.resolve_category(conn, "Utilities")
    pay = ledger.resolve_category(conn, "Salary")
    for date, amt in (("2025-11-05", -80_00), ("2025-12-06", -85_00),
                      ("2026-01-05", -78_00), ("2026-02-05", -82_00)):
        ledger.add_transaction(conn, chk, date, amt, payee="Power Co", category_id=util)
    d = "2025-11-07"
    for _ in range(7):
        ledger.add_transaction(conn, chk, d, 2000_00, payee="Employer", category_id=pay)
        d = scheduled.advance_date(d, "biweekly")
    ledger.add_transaction(conn, chk, "2026-01-20", -30_00, payee="Rare Shop")
    ledger.add_transaction(conn, chk, "2026-02-19", -30_00, payee="Rare Shop")
    for date in ("2025-12-01", "2026-01-01", "2026-02-01"):
        ledger.add_transaction(conn, chk, date, -15_99, payee="Streaming")
    scheduled.add_scheduled(conn, chk, payee="Streaming", amount=-15_99,
                            frequency="monthly", next_date="2026-03-01")
    found = scheduled.suggest_recurring(conn, "2026-02-15")
    by = {s["payee"]: s for s in found}
    assert set(by) == {"Power Co", "Employer"}       # mortgage splits, Rare, Streaming out
    p = by["Power Co"]
    assert (p["frequency"], p["amount"], p["varies"], p["next_date"], p["count"],
            p["category_id"], p["account_id"]) == \
        ("monthly", -82_00, True, "2026-03-05", 4, util, chk)
    e = by["Employer"]
    assert (e["frequency"], e["amount"], e["varies"], e["count"]) == \
        ("biweekly", 2000_00, False, 7)
    assert e["next_date"] >= "2026-02-15"
    dlg = SuggestRecurringDialog(conn, found)
    assert dlg.table.rowCount() == 2
    assert "≈" in dlg.table.item([i for i in range(2) if dlg.table.item(i, dlg.PAYEE).text()
                                  == "Power Co"][0], dlg.AMOUNT).text()
    for i in range(2):
        if dlg.table.item(i, dlg.PAYEE).text() == "Employer":
            dlg.table.item(i, dlg.ADD).setCheckState(Qt.Unchecked)
    ids = dlg.add_chosen()
    assert len(ids) == 1
    added = scheduled.get_scheduled(conn, ids[0])
    assert (added["payee"], added["amount"], added["frequency"], added["next_date"],
            added["category_id"]) == ("Power Co", -82_00, "monthly", "2026-03-05", util)
    assert [s["payee"] for s in scheduled.suggest_recurring(conn, "2026-02-15")] == ["Employer"]
    dlg.deleteLater()

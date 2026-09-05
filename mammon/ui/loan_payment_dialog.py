"""Enter Loan Payment -- the loan register's gear-menu way to post one payment.

A configured loan is pre-entered automatically inside its reminder lead, but
outside that window (or with pre-entry at launch switched off) the register had
no way to record a payment short of typing the three-leg split by hand. This
dialog posts one exactly as the schedule would -- as interest + extras + a
``[Loan]`` principal leg -- on the account that pays the loan, or on another
account for this one payment (the card, because checking is low this month),
and shows that split before anything is saved, recomputed as the date, amount
or account changes.

A pending pre-entry already standing for the period is a placeholder, not a
reason to refuse: saving posts it with the date, amount, payee and account
entered here (moving it when the account differs), so Enter never doubles a
period and is never blocked by its own reminder -- see
``loans_schedule.enter_payment``. A period that already holds a POSTED payment
is different: an extra payment is a legitimate thing to enter, so the dialog
only says so, and saves.

Nothing here exec_()s or pops a modal on its own (the headless-modal hazard):
the window opens it and posts through
``loans_schedule.enter_payment(conn, loan, **dlg.values())``; an invalid entry
is reported through the overridable ``_warn`` seam.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from PyQt5.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                             QLabel, QLineEdit, QMessageBox, QVBoxLayout)

from mammon import ledger, loans, loans_schedule
from mammon.importers.record import dollars_to_cents
from mammon.ui.delegates import date_edit_iso, make_date_edit
from mammon.ui.models import fmt_date, fmt_money

# Account types a loan payment can be made from.
PAYING_TYPES = ("checking", "savings", "credit", "cash")


class EnterLoanPaymentDialog(QDialog):
    def __init__(self, conn, loan_account_id: int, parent=None,
                 today: Optional[str] = None):
        super().__init__(parent)
        self.conn = conn
        self.loan_id = int(loan_account_id)
        self.lp = loans.get_loan_params(conn, self.loan_id)
        if self.lp is None:
            raise LookupError(f"account {loan_account_id} has no loan parameters")
        acct = ledger.get_account(conn, self.loan_id)
        self.loan_name = acct["name"] if acct is not None else "Loan"
        self.funder = loans.funding_account(conn, self.loan_id)
        self.setWindowTitle(f"Enter Loan Payment — {self.loan_name}")

        today = today or _dt.date.today().isoformat()
        due = loans_schedule.next_due_date(conn, self.loan_id, today) or today
        payee = ((loans.last_payment_payee(conn, self.loan_id, self.funder)
                  if self.funder is not None else None)
                 or f"{self.loan_name} Payment")

        self.date_edit = make_date_edit(self, due)
        self.payee = QLineEdit(payee)
        self.amount = QLineEdit(f"{self.lp.payment_amount / 100:.2f}")
        self.amount.setPlaceholderText("the whole payment, e.g. 1268.99")
        # The account the money leaves: the loan's own funder by default, any
        # other spending account for this one payment.
        self.pay_from = QComboBox()
        if self.funder is None:
            self.pay_from.addItem(f"{self.loan_name} (its own register)", None)
        for a in ledger.list_accounts(conn, include_closed=False, include_hidden=True):
            if (a["type"] or "") in PAYING_TYPES and int(a["id"]) != self.loan_id:
                self.pay_from.addItem(a["name"], int(a["id"]))
        if self.funder is not None:
            i = self.pay_from.findData(self.funder)
            if i >= 0:
                self.pay_from.setCurrentIndex(i)
        self.pay_from.setToolTip(
            "Where the money leaves from. Choosing another account pays this "
            "one payment from there; the loan keeps its usual account.")
        self.breakdown = QLabel()
        self.breakdown.setWordWrap(True)
        self.breakdown.setStyleSheet("color: gray;")

        form = QFormLayout()
        form.addRow("Date", self.date_edit)
        form.addRow("Payee", self.payee)
        form.addRow("Amount", self.amount)
        form.addRow("Pay from", self.pay_from)
        form.addRow("Split", self.breakdown)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.date_edit.dateChanged.connect(lambda *_: self._refresh_breakdown())
        self.amount.textChanged.connect(lambda *_: self._refresh_breakdown())
        self.pay_from.currentIndexChanged.connect(lambda *_: self._refresh_breakdown())
        self._refresh_breakdown()

    # -- the split preview ---------------------------------------------------
    def _amount_cents(self) -> Optional[int]:
        text = self.amount.text().strip()
        if not text:
            return None
        try:
            cents = abs(dollars_to_cents(text))
        except Exception:
            return None
        return cents or None

    def _split(self):
        """``(split, problem)`` for the current date/amount; ``problem`` is the
        message that blocks saving, or None."""
        date = date_edit_iso(self.date_edit)
        cents = self._amount_cents()
        if not date:
            return None, "Enter a date."
        if cents is None:
            return None, "Enter the payment amount."
        try:
            split = loans.payment_split(self.conn, self.loan_id, date, cents)
        except Exception as e:            # a date before origination, etc.
            return None, str(e)
        if split.principal <= 0:
            return split, (f"{fmt_money(cents)} does not cover the interest and "
                           f"extras due on {fmt_date(date)}.")
        return split, None

    def _refresh_breakdown(self) -> None:
        split, problem = self._split()
        if problem:
            self.breakdown.setText(problem)
            return
        parts = []
        if split.interest:
            parts.append(f"{self.lp.interest_category or loans_schedule.INTEREST_CATEGORY} "
                         f"{fmt_money(split.interest)}")
        for ex in split.extras:
            if ex.amount:
                parts.append(f"{ex.label or ex.category} {fmt_money(ex.amount)}")
        parts.append(f"principal {fmt_money(split.principal)} → [{self.loan_name}]")
        text = " · ".join(parts)
        where = self.pay_from.currentText()
        if self.pay_from.currentData() is not None:
            text += f"\nPosts on {where} as a split."
        date = date_edit_iso(self.date_edit)
        existing = loans_schedule.payment_for(self.conn, self.loan_id, date)
        row = (ledger.get_transaction(self.conn, existing)
               if existing is not None else None)
        if row is not None:
            racct = ledger.get_account(self.conn, row["account_id"])
            rname = racct["name"] if racct is not None else "its account"
            if row["scheduled"]:
                text += (f"\nA pending pre-entry dated {fmt_date(row['date'])} stands "
                         f"on {rname} for this period; saving posts it with these values.")
            else:
                text += (f"\nA payment dated {fmt_date(row['date'])} on {rname} already "
                         f"covers this period; saving adds another.")
        self.breakdown.setText(text)

    # -- result --------------------------------------------------------------
    def validate(self):
        _split, problem = self._split()
        return (problem is None), (problem or "")

    def values(self) -> dict:
        """Keyword arguments for ``loans_schedule.enter_payment``."""
        return {"date": date_edit_iso(self.date_edit),
                "amount_cents": self._amount_cents(),
                "payee": self.payee.text().strip() or None,
                "funding_account_id": self.pay_from.currentData()}

    def _warn(self, msg: str) -> None:
        QMessageBox.warning(self, "Enter Loan Payment", msg)

    def accept(self) -> None:
        ok, msg = self.validate()
        if not ok:
            self._warn(msg)
            return
        super().accept()

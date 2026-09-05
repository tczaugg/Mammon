"""Scheduled Payments manager (Tools menu).

One place to view/add/edit/delete recurring payment definitions -- subscriptions,
fixed-price utilities (cable/internet), memberships -- plus each loan's payment
schedule: Enter posts the next payment on the account that pays the loan, the
Edit slot opens Loan Setup, and Delete removes the loan setup (never the account
or its history). Skip has no meaning for a loan -- a period is paid, not skipped
-- so it stays disabled there. See :mod:`mammon.scheduled` for the data model and why these pre-entries are Mammon-GENERATED (not part of any imported
file) and survive a QIF re-import.
"""
from __future__ import annotations

from PyQt5.QtCore import QDate, Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog,
    QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from mammon import categorize, ledger, loans, loans_schedule, scheduled
from mammon.importers.record import dollars_to_cents
from mammon.ui import prefs
from mammon.ui.models import fmt_date

# Reminder status -> (label, foreground) for the manager's Status column.
_STATUS_TEXT = {
    scheduled.OVERDUE: lambda n: f"Overdue {-n}d",
    scheduled.DUE_TODAY: lambda n: "Due today",
    scheduled.DUE_SOON: lambda n: f"Due in {n}d",
    scheduled.UPCOMING: lambda n: "Upcoming",
}
_STATUS_COLOR = {scheduled.OVERDUE: "#b2382c", scheduled.DUE_TODAY: "#a8701a",
                 scheduled.DUE_SOON: "#a8701a"}


def _fmt_cents(c: int) -> str:
    if c is None:
        return ""
    sign = "-" if c < 0 else ""
    return f"{sign}${abs(c) / 100:,.2f}"


def _today() -> str:
    return QDate.currentDate().toString("yyyy-MM-dd")


class ScheduledPaymentEditor(QDialog):
    """Add/edit one manual scheduled-payment definition."""

    def __init__(self, conn, entry=None, parent=None, *, learn_splits=False):
        super().__init__(parent)
        self.conn = conn
        self.entry = entry
        # Only the calendar's "Schedule <payee>" flow actually LEARNS a split
        # onto the definition (projection_dialogs.schedule_prediction, via
        # ledger.previous_split_for_payee); so only there does the dialog surface
        # the inherited split -- claiming one on the plain manager Add, which
        # stores no template, would be a lie. Category prefill is truthful either
        # way and is offered regardless.
        self.learn_splits = bool(learn_splits)
        self._inherited_splits: list = []
        self.setWindowTitle("Edit Scheduled Payment" if entry else
                            "Add Scheduled Payment")
        self.resize(420, 0)

        form = QFormLayout()

        self.account = QComboBox()
        for acct in ledger.list_accounts(conn, include_closed=False,
                                         include_hidden=True):
            self.account.addItem(acct["name"], acct["id"])
        form.addRow("Account", self.account)

        self.payee = QLineEdit()
        # Leaving the payee pre-enters the category (or shows the split) it will
        # inherit from history, the way the register's QuickFill does.
        self.payee.editingFinished.connect(self._prefill_from_payee)
        form.addRow("Payee", self.payee)

        self.amount = QLineEdit()
        self.amount.setPlaceholderText("e.g. -15.99  (a bill is negative)")
        form.addRow("Amount", self.amount)

        self.frequency = QComboBox()
        for f in scheduled.FREQUENCIES:
            self.frequency.addItem(f.capitalize(), f)
        self.frequency.setCurrentIndex(scheduled.FREQUENCIES.index("monthly"))
        form.addRow("Frequency", self.frequency)

        self.next_date = QDateEdit()
        self.next_date.setCalendarPopup(True)
        # The user's chosen format, like every other date field.
        from mammon.ui.models import qt_date_format
        self.next_date.setDisplayFormat(qt_date_format())
        self.next_date.setDate(QDate.currentDate())
        form.addRow("Next date", self.next_date)

        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.addItem("")
        for c in ledger.list_categories(conn):
            self.category.addItem(c["path"], c["id"])
        form.addRow("Category", self.category)

        # A quiet note that the category/split above was taken from the payee's
        # history -- so an inherited split is never invisible behind a lone
        # category. Created before _load() so the signals _load() fires can
        # write to it; added to the form here so it sits under Category.
        self.inherit_hint = QLabel("")
        self.inherit_hint.setStyleSheet("color: gray; font-style: italic;")
        self.inherit_hint.setWordWrap(True)
        self.inherit_hint.setVisible(False)
        form.addRow("", self.inherit_hint)

        # A TRANSFER reminder (a savings sweep, a card payment): pick the other
        # account and the category no longer applies -- the pre-entry becomes a
        # transfer with its mirror. The sign of the amount says the direction.
        self.transfer = QComboBox()
        self.transfer.addItem("(none — a category)", None)
        for acct in ledger.list_accounts(conn, include_closed=False, include_hidden=True):
            self.transfer.addItem(acct["name"], acct["id"])
        self.transfer.currentIndexChanged.connect(self._sync_transfer)
        form.addRow("Transfer to/from", self.transfer)

        self.memo = QLineEdit()
        form.addRow("Memo", self.memo)

        # Reminder behaviour (parity): remind this many days ahead (Default =
        # the app's usual lead), and whether to pre-enter it automatically or
        # only remind and wait for Enter.
        self.lead_days = QSpinBox()
        self.lead_days.setRange(-1, 120)
        self.lead_days.setSpecialValueText("Default")
        self.lead_days.setValue(-1)
        self.lead_days.setSuffix(" days ahead")
        form.addRow("Remind", self.lead_days)

        self.auto_enter = QCheckBox("Enter automatically (pre-enter as a pending row)")
        self.auto_enter.setChecked(True)
        form.addRow("", self.auto_enter)

        self.active = QCheckBox("Active")
        self.active.setChecked(True)
        form.addRow("", self.active)

        if entry is not None:
            self._load(entry)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        note = QLabel("Generated by Mammon — not part of any imported file.")
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _load(self, entry: dict) -> None:
        idx = self.account.findData(entry["account_id"])
        if idx >= 0:
            self.account.setCurrentIndex(idx)
        self.payee.setText(entry.get("payee") or "")
        self.amount.setText(f"{entry['amount'] / 100:.2f}")
        fidx = self.frequency.findData(entry["frequency"])
        if fidx >= 0:
            self.frequency.setCurrentIndex(fidx)
        d = QDate.fromString(entry["next_date"], "yyyy-MM-dd")
        if d.isValid():
            self.next_date.setDate(d)
        taid = entry.get("transfer_account_id")
        if taid is not None:
            t = self.transfer.findData(taid)
            if t >= 0:
                self.transfer.setCurrentIndex(t)
        else:
            self.category.setEditText(entry.get("category_label") or "")
        self.memo.setText(entry.get("memo") or "")
        lead = entry.get("lead_days")
        self.lead_days.setValue(-1 if lead is None else int(lead))
        self.auto_enter.setChecked(bool(entry.get("auto_enter", True)))
        self.active.setChecked(bool(entry.get("active", True)))
        self._sync_transfer()

    def _sync_transfer(self, *_args) -> None:
        # A transfer definition is a single whole-transaction move: no category
        # and no split. Switching to/from one re-evaluates the payee hint.
        self._prefill_from_payee()

    def _refresh_category_enabled(self) -> None:
        # The single Category is moot when this is a transfer, or when an
        # inherited split will override it at entry; disabling it says so.
        self.category.setEnabled(self.transfer.currentData() is None
                                 and not self._inherited_splits)

    def _set_inherit_hint(self, text: str) -> None:
        self.inherit_hint.setText(text)
        self.inherit_hint.setVisible(bool(text))

    def _split_summary(self, s: dict) -> str:
        label = s.get("category_label") or "(uncategorized)"
        return f"{label} {_fmt_cents(int(s['amount']))}"

    def _prefill_from_payee(self, *_args) -> None:
        """Pre-enter the category (or surface the split) this payee inherits from
        history, and note it under the Category field. Read-only: the write still
        funnels through scheduled/ledger unchanged. Never clobbers a category the
        user has already typed."""
        if getattr(self, "inherit_hint", None) is None:
            return                                    # too early (still building)
        if self.transfer.currentData() is not None:
            self._inherited_splits = []
            self._set_inherit_hint("")
            self._refresh_category_enabled()
            return
        payee = self.payee.text().strip()
        info = (categorize.inherited_entry_for_payee(self.conn, payee)
                if payee else {"category_label": "", "splits": []})
        # Only a flow that actually learns the split (the calendar) may claim it.
        self._inherited_splits = info["splits"] if self.learn_splits else []
        if self._inherited_splits:
            lines = "; ".join(self._split_summary(s) for s in self._inherited_splits)
            self._set_inherit_hint(
                f"Inherits the split from your most recent “{payee}”: "
                f"{lines}. This overrides the single Category above.")
        elif info["category_label"]:
            if not self.category.currentText().strip():
                self.category.setEditText(info["category_label"])
            # Confirm the provenance only while the field actually shows it, so
            # the note is never contradicted by a category the user retyped.
            if self.category.currentText().strip() == info["category_label"]:
                self._set_inherit_hint(
                    f"Category from your most recent “{payee}”.")
            else:
                self._set_inherit_hint("")
        else:
            self._set_inherit_hint("")
        self._refresh_category_enabled()

    def _on_accept(self) -> None:
        if self.account.currentData() is None:
            QMessageBox.warning(self, "Scheduled Payment", "Pick an account.")
            return
        taid = self.transfer.currentData()
        if taid is not None and taid == self.account.currentData():
            QMessageBox.warning(self, "Scheduled Payment",
                                "A transfer needs a different account on the other side.")
            return
        text = self.amount.text().strip()
        if not text:
            QMessageBox.warning(self, "Scheduled Payment", "Enter an amount.")
            return
        try:
            amount = dollars_to_cents(text)
        except Exception:
            QMessageBox.warning(self, "Scheduled Payment",
                                f"Could not read the amount {text!r}.")
            return
        if amount == 0:
            QMessageBox.warning(self, "Scheduled Payment",
                                "Amount must not be zero.")
            return
        self.accept()

    def values(self) -> dict:
        taid = self.transfer.currentData()
        cat_text = self.category.currentText().strip()
        category_id = (None if taid is not None
                       else ledger.resolve_category(self.conn, cat_text) if cat_text else None)
        lead = self.lead_days.value()
        return {
            "account_id": self.account.currentData(),
            "payee": self.payee.text().strip() or None,
            "amount": dollars_to_cents(self.amount.text().strip()),
            "frequency": self.frequency.currentData(),
            "next_date": self.next_date.date().toString("yyyy-MM-dd"),
            "category_id": category_id,
            "transfer_account_id": taid,
            "memo": self.memo.text().strip() or None,
            "lead_days": None if lead < 0 else int(lead),
            "auto_enter": self.auto_enter.isChecked(),
            "active": self.active.isChecked(),
        }


class ScheduledPaymentsDialog(QDialog):
    """View/add/edit/delete scheduled payments; loan schedules appear read-only."""

    changed = pyqtSignal()
    STATUS, SOURCE, ACCOUNT, PAYEE, AMOUNT, FREQ, NEXT, CATEGORY = range(8)

    def __init__(self, conn, parent=None, today=None):
        super().__init__(parent)
        self.conn = conn
        self.today = today or _today()
        self.setWindowTitle("Scheduled Payments")
        self.resize(860, 480)
        self._rows: list = []

        note = QLabel(
            "Scheduled payments are recurring bills, income and transfers Mammon "
            "reminds you of and PRE-ENTERS for you (subscriptions, fixed "
            "utilities, paychecks, loan payments). They are generated by Mammon "
            "— NOT carried in any imported QIF/OFX file — and survive re-imports. "
            "Enter records the next one now; Skip moves past it. A loan row's "
            "next date is the first period no register holds yet: Enter posts "
            "that payment on the account that pays the loan, Loan Setup… "
            "changes the loan (or which account pays it), and Delete removes "
            "the loan setup, leaving the account and its history alone.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Status", "Source", "Account", "Payee", "Amount", "Frequency",
             "Next date", "Category"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.currentCellChanged.connect(lambda *_: self._sync_buttons())
        self.table.doubleClicked.connect(lambda *_: self._edit())

        self.add_btn = QPushButton("Add…")
        self.add_btn.clicked.connect(self._add)
        self.suggest_btn = QPushButton("Suggest…")
        self.suggest_btn.setToolTip(
            "Find payees that recur at a steady interval and amount in your "
            "registers and are not scheduled yet, and add the ones you tick.")
        self.suggest_btn.clicked.connect(self._suggest)
        self.edit_btn = QPushButton("Edit…")
        self.edit_btn.clicked.connect(self._edit)
        self.delete_btn = QPushButton("Delete…")
        self.delete_btn.clicked.connect(self._delete)
        self.enter_btn = QPushButton("Enter")
        self.enter_btn.setToolTip("Record the next occurrence now, as a posted "
                                  "transaction, and move the reminder on.")
        self.enter_btn.clicked.connect(self._enter)
        self.skip_btn = QPushButton("Skip")
        self.skip_btn.setToolTip("Move past the next occurrence without recording it.")
        self.skip_btn.clicked.connect(self._skip)
        self.gen_btn = QPushButton("Generate pre-entries")
        self.gen_btn.setToolTip(
            "Pre-enter every due payment (through each one's lead) into its "
            "register as a pending row.")
        self.gen_btn.clicked.connect(self._generate)
        self.auto_launch = QCheckBox("Pre-enter due payments when Mammon starts")
        self.auto_launch.setChecked(prefs.auto_enter_on_launch())
        self.auto_launch.toggled.connect(prefs.set_auto_enter_on_launch)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.suggest_btn)
        btn_row.addWidget(self.edit_btn)
        btn_row.addWidget(self.delete_btn)
        btn_row.addSpacing(12)
        btn_row.addWidget(self.enter_btn)
        btn_row.addWidget(self.skip_btn)
        btn_row.addSpacing(12)
        btn_row.addWidget(self.gen_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.table)
        layout.addLayout(btn_row)
        layout.addWidget(self.auto_launch)

        self._reload()

    # -- data ---------------------------------------------------------------
    def _reload(self) -> None:
        active = scheduled.list_reminders(self.conn, self.today)
        inactive = [d for d in scheduled.list_scheduled(self.conn) if not d["active"]]
        self._rows = active + inactive
        self.table.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            status = row.get("status")
            status_text = (_STATUS_TEXT[status](row["days_until"]) if status
                           else "Inactive")
            cells = [
                status_text,
                "Loan" if row["source"] == "loan" else
                {"income": "Income", "transfer": "Transfer"}.get(row.get("kind"), "Bill"),
                row.get("account_name") or "",
                row.get("payee") or "",
                _fmt_cents(row["amount"]),
                (row.get("frequency") or "").capitalize(),
                row.get("next_date") or "",
                row.get("category_label") or "",
            ]
            is_inactive = status is None
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == self.AMOUNT:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if is_inactive:
                    item.setForeground(Qt.gray)
                elif col == self.STATUS and status in _STATUS_COLOR:
                    item.setForeground(QColor(_STATUS_COLOR[status]))
                self.table.setItem(i, col, item)
        self.table.resizeColumnsToContents()
        self._sync_buttons()

    def _current(self):
        r = self.table.currentRow()
        if 0 <= r < len(self._rows):
            return self._rows[r]
        return None

    def _sync_buttons(self) -> None:
        row = self._current()
        manual = row is not None and row["source"] == "manual"
        loan = row is not None and row["source"] == "loan"
        active = row is not None and bool(row.get("active", True))
        self.edit_btn.setText("Loan Setup…" if loan else "Edit…")
        self.edit_btn.setEnabled(manual or loan)
        self.delete_btn.setEnabled(manual or loan)
        self.delete_btn.setToolTip(
            "Remove this loan's setup (rates, term, payment, extras). The "
            "account and its transactions stay." if loan else "")
        self.enter_btn.setEnabled((manual or loan) and active
                                  and bool(row.get("next_date")))
        self.skip_btn.setEnabled(manual and active)
        self.skip_btn.setToolTip(
            "Loan payments follow the amortization schedule; a period is paid, "
            "not skipped." if loan else
            "Move past the next occurrence without recording it.")

    def _enter(self) -> None:
        row = self._current()
        if row is None or not row.get("active", True) or not row.get("next_date"):
            return
        if row["source"] == "loan":
            loans_schedule.enter_payment(self.conn, row["id"], row["next_date"])
        elif row["source"] == "manual":
            scheduled.enter_next(self.conn, row["id"])
        else:
            return
        self._reload()
        self.changed.emit()

    def _skip(self) -> None:
        row = self._current()
        if row is None or row["source"] != "manual" or not row.get("active", True):
            return
        scheduled.skip_next(self.conn, row["id"])
        self._reload()
        self.changed.emit()

    # -- actions ------------------------------------------------------------
    def _add(self) -> None:
        dlg = ScheduledPaymentEditor(self.conn, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            scheduled.add_scheduled(self.conn, **dlg.values())
            self._reload()
            self.changed.emit()

    def _loan_setup(self, account_id: int) -> bool:
        """Open Loan Setup on this loan; True when the wizard saved. A small
        overridable seam so tests never exec_() a modal under offscreen."""
        from mammon.ui.loan_wizard import LoanSetupWizard
        wiz = LoanSetupWizard(self.conn, account_id=account_id, parent=self)
        return wiz.exec_() == QDialog.Accepted

    def _info(self, msg: str) -> None:
        QMessageBox.information(self, "Scheduled Payments", msg)

    def _suggest(self) -> None:
        """Suggest…: regular payees from history, ticked into definitions."""
        found = scheduled.suggest_recurring(self.conn, self.today)
        if not found:
            self._info("Nothing regular was found in your registers that is not "
                       "already scheduled.")
            return
        dlg = SuggestRecurringDialog(self.conn, found, parent=self)
        if dlg.exec_() == QDialog.Accepted and dlg.add_chosen():
            self._reload()
            self.changed.emit()

    def _edit(self) -> None:
        row = self._current()
        if row is None:
            return
        if row["source"] == "loan":
            if self._loan_setup(row["id"]):
                self._reload()
                self.changed.emit()
            return
        if row["source"] != "manual":
            return
        dlg = ScheduledPaymentEditor(self.conn, entry=row, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            scheduled.update_scheduled(self.conn, row["id"], **dlg.values())
            self._reload()
            self.changed.emit()

    def _delete(self) -> None:
        row = self._current()
        if row is None or row["source"] not in ("manual", "loan"):
            return
        if row["source"] == "loan":
            acct = ledger.get_account(self.conn, row["id"])
            name = acct["name"] if acct is not None else "this loan"
            question = (
                f"Remove the loan setup for {name}?\n\n"
                "The account and every transaction on it stay exactly as they "
                "are; only the loan parameters (rates, term, payment, extras) "
                "go, so no further payments are scheduled. Already-generated "
                "pending pre-entries stay in the registers.")
        else:
            name = row.get("payee") or row.get("account_name") or "this payment"
            question = (
                f"Delete the scheduled payment for {name}?\n\n"
                "(Already-generated pending pre-entries stay in the register; "
                "delete those from the register if you no longer want them.)")
        if QMessageBox.question(self, "Delete Scheduled Payment", question,
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        if row["source"] == "loan":
            loans.delete_loan_params(self.conn, row["id"])
        else:
            scheduled.delete_scheduled(self.conn, row["id"])
        self._reload()
        self.changed.emit()

    def _generate(self) -> None:
        ids = scheduled.generate_all_due(self.conn, self.today)
        self._reload()
        self.changed.emit()
        QMessageBox.information(
            self, "Scheduled Payments",
            f"{len(ids)} pending pre-entr{'y' if len(ids) == 1 else 'ies'} are "
            "up to date in the registers.")


class SuggestRecurringDialog(QDialog):
    """Regular payees found in history (``scheduled.suggest_recurring``),
    offered as definitions: tick the ones to keep. Nothing is added until OK;
    ``add_chosen`` does the adding so the manager (and tests) can drive it
    without exec_()."""
    ADD, ACCOUNT, PAYEE, AMOUNT, FREQ, NEXT, SEEN = range(7)

    def __init__(self, conn, suggestions, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.suggestions = list(suggestions)
        self.setWindowTitle("Suggest Scheduled Payments")
        self.resize(760, 380)
        note = QLabel(
            "These payees recur at a steady interval and amount in your registers "
            "and have no scheduled payment yet. Tick the ones to add. An amount "
            "marked ≈ varies a little from month to month; the latest is used, "
            "and a download that differs merges into the pre-entry anyway.")
        note.setWordWrap(True)
        self.table = QTableWidget(len(self.suggestions), 7)
        self.table.setHorizontalHeaderLabels(
            ["Add", "Account", "Payee", "Amount", "Frequency", "Next date", "Seen"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, s in enumerate(self.suggestions):
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            tick.setCheckState(Qt.Checked)
            self.table.setItem(i, self.ADD, tick)
            cells = [s["account_name"], s["payee"],
                     ("≈ " if s["varies"] else "") + _fmt_cents(s["amount"]),
                     s["frequency"].capitalize(), fmt_date(s["next_date"]),
                     f"{s['count']}× since {fmt_date(s['last_date'])}"]
            for col, text in enumerate(cells, start=1):
                item = QTableWidgetItem(text)
                if col == self.AMOUNT:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, col, item)
        self.table.resizeColumnsToContents()
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Add ticked")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.table)
        layout.addWidget(buttons)

    def chosen(self) -> list:
        return [s for i, s in enumerate(self.suggestions)
                if self.table.item(i, self.ADD).checkState() == Qt.Checked]

    def add_chosen(self) -> list:
        """Add every ticked suggestion as a definition; returns their ids."""
        ids = []
        for s in self.chosen():
            ids.append(scheduled.add_scheduled(
                self.conn, s["account_id"], payee=s["payee"], amount=s["amount"],
                frequency=s["frequency"], next_date=s["next_date"],
                category_id=s["category_id"]))
        return ids

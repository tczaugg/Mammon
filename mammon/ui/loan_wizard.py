"""Loan setup wizard: guided entry of a loan's parameters (Task 53).

A multi-step QDialog that walks the user through everything mammon.loans needs to
amortize a loan and split its payments -- beating Quicken's buried,
hard-to-remember loan setup by laying the steps out plainly and PREVIEWING the
first payment's principal/interest/escrow split before anything is saved.

Steps:
  1. Account -- attach to an existing liability account, or create a new one
                (a new account opens with the loan's balance owed).
  2. Terms   -- original principal, first-payment date, term, total payment,
                and payment interval.
  3. Rates   -- the interest-rate HISTORY (effective date + annual %), so a loan
                that resets is modeled faithfully (Quicken stores a single rate).
  4. Extras  -- categorized extra amounts folded into each payment (escrow, PMI,
                HOA, ...), each EFFECTIVE-DATED like the rate history, so an
                escrow/PMI change mid-loan is modeled faithfully and interest and
                escrow are never lost on a payment.
  5. Review  -- the computed first-payment split + a sanity check, then Finish.

Persistence goes through mammon.loans.set_loan_params (the sole owner of the
loan_params / loan_rates / loan_extras rows); a brand-new account is created via
mammon.ledger. No SQL lives here. Follows the QDialog conventions in
mammon/ui/widgets.py (the codebase uses stepped QDialogs, not QWizard).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal, InvalidOperation

from PyQt5.QtWidgets import (
    QAbstractSpinBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from mammon import ledger, loans, loans_schedule
from mammon.ui.models import fmt_date, fmt_money, parse_amount
from mammon.ui.delegates import date_edit_iso, make_category_combo, make_date_edit


def _seed_date(edit, iso) -> None:
    """Seed a date editor from a stored ISO date (today when absent)."""
    from PyQt5.QtCore import QDate
    d = QDate.fromString(str(iso or "").strip(), "yyyy-MM-dd")
    edit.setDate(d if d.isValid() else QDate.currentDate())


# Ordered for the interval combo; validated against loans._INTERVALS.
_INTERVAL_NAMES = ["weekly", "biweekly", "semimonthly", "monthly",
                   "quarterly", "semiannual", "annual"]
# Sentinel account-combo choice meaning "create a new loan account".
_NEW_ACCOUNT = "__new__"
# Default "Paid from" label (id None): pre-enter the payment on the loan register.
_FUNDING_UNSET = "(not set — pre-enter on the loan register)"
# Common extra-amount categories offered as suggestions (editable).
_EXTRA_SUGGESTIONS = ["Escrow", "PMI", "HOA", "Insurance", "Taxes"]


def _is_iso_date(text) -> bool:
    try:
        _dt.date.fromisoformat((text or "").strip())
        return True
    except ValueError:
        return False


def _parse_rate(text):
    """A non-negative Decimal rate from text, or None if it does not parse."""
    try:
        d = Decimal((text or "").strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d >= 0 else None


def _parse_cents_or_none(text):
    """Positive integer cents from a money string, or None if blank/invalid."""
    try:
        cents = parse_amount(text)
    except Exception:
        return None
    return cents if cents > 0 else None


class LoanSetupWizard(QDialog):
    """The guided loan-parameter wizard. Construct with ``account_id`` set to an
    account whose parameters should be pre-loaded (the edit path); leave it
    ``None`` to set up a fresh loan. ``save()`` validates and persists (returning
    a bool) and ``validate()`` is a pure check returning ``(ok, message)`` -- both
    are used by tests as well as by the Finish button, so no modal dialog is
    shown on the programmatic path."""

    STEP_TITLES = ["Account", "Terms", "Interest rate history",
                   "Extra amounts", "Review"]

    def __init__(self, conn, account_id=None, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.saved_account_id = None
        self.setWindowTitle("Loan Setup")
        self.resize(580, 480)

        outer = QVBoxLayout(self)
        self.step_label = QLabel()
        self.step_label.setStyleSheet("font-weight: bold;")
        outer.addWidget(self.step_label)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_account_page())
        self.stack.addWidget(self._build_terms_page())
        self.stack.addWidget(self._build_rates_page())
        self.stack.addWidget(self._build_extras_page())
        self.stack.addWidget(self._build_review_page())
        outer.addWidget(self.stack, 1)

        nav = QHBoxLayout()
        nav.addStretch()
        self.back_btn = QPushButton("< Back")
        self.back_btn.clicked.connect(self._go_back)
        self.next_btn = QPushButton("Next >")
        self.next_btn.clicked.connect(self._go_next)
        self.finish_btn = QPushButton("Finish")
        self.finish_btn.clicked.connect(self._on_finish)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        for b in (self.back_btn, self.next_btn, self.finish_btn, cancel_btn):
            nav.addWidget(b)
        outer.addLayout(nav)

        self._populate_accounts()
        if account_id is not None:
            self._load_existing(account_id)
        self._show_step(0)

    # ---- page construction ------------------------------------------------
    def _build_account_page(self):
        page = QWidget()
        form = QFormLayout(page)
        self.account_combo = QComboBox()
        self.account_combo.currentIndexChanged.connect(self._on_account_choice)
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("e.g. Home Mortgage")
        form.addRow("Loan account", self.account_combo)
        form.addRow("New account name", self.new_name)
        # The account the payment is made FROM. Pre-entries and projections
        # post the whole payment there as a split (interest, escrow, a [Loan]
        # principal leg) -- the shape real payments have -- so that account's
        # download merges into them. Pre-filled from history when unset.
        #
        # Built with the register's own category/transfer input machinery
        # (make_category_combo: editable, case-insensitive popup completer, the
        # ':' parent-complete gesture) so it auto-completes exactly like every
        # other picker in the app instead of only jumping to the first letter
        # typed. The completer's choices are the fundable ACCOUNTS ONLY (no
        # categories -- a loan is paid from an account, never a category).
        self._funding_by_name = {}
        for acct in ledger.list_accounts(self.conn, include_closed=False,
                                         include_hidden=True):
            if (acct["type"] or "") in ("checking", "savings", "credit", "cash"):
                self._funding_by_name[acct["name"]] = int(acct["id"])
        self.funding_combo = make_category_combo(None, list(self._funding_by_name))
        # make_category_combo seeds a blank first item; relabel it as the explicit
        # "not set" default (id None) and carry each account's id as item data, so
        # the existing account-name -> account-id mapping and currentData() are
        # preserved (the Edit path still findData()s the funder) while
        # _funding_account_id() reads back a typed-and-completed name too.
        self.funding_combo.setItemText(0, _FUNDING_UNSET)
        self.funding_combo.setItemData(0, None)
        for i in range(1, self.funding_combo.count()):
            self.funding_combo.setItemData(
                i, self._funding_by_name.get(self.funding_combo.itemText(i)))
        form.addRow("Paid from", self.funding_combo)
        hint = QLabel("Attach these parameters to a liability account, or create "
                      "a new one. A new account opens with the loan's balance "
                      "owed (a negative starting balance).")
        hint.setWordWrap(True)
        hint.setObjectName("registerSub")
        form.addRow(hint)
        return page

    def _build_terms_page(self):
        page = QWidget()
        form = QFormLayout(page)
        self.principal = QDoubleSpinBox()
        self.principal.setRange(0.0, 1_000_000_000.0)
        self.principal.setDecimals(2)
        # Typeable and calendar-pickable, in the user's chosen date format --
        # this was free text that only accepted ISO.
        self.first_payment = make_date_edit()
        self.term_months = QSpinBox()
        self.term_months.setRange(1, 1200)
        self.term_months.setValue(360)
        self.payment = QDoubleSpinBox()
        self.payment.setRange(0.0, 1_000_000_000.0)
        self.payment.setDecimals(2)
        # Amounts are typed in, not stepped: drop the up/down spinner arrows on
        # every numeric field while keeping the spinbox's numeric validation.
        for spin in (self.principal, self.term_months, self.payment):
            spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.interval = QComboBox()
        for name in _INTERVAL_NAMES:
            self.interval.addItem(name.capitalize(), name)
        self.interval.setCurrentIndex(_INTERVAL_NAMES.index("monthly"))
        form.addRow("Original principal ($)", self.principal)
        form.addRow("First payment date", self.first_payment)
        form.addRow("Term (months)", self.term_months)
        form.addRow("Total payment ($, incl. escrow/extras)", self.payment)
        form.addRow("Payment interval", self.interval)
        return page

    def _build_rates_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lbl = QLabel("Interest-rate history -- each rate applies on and after its "
                     "effective date. Add a row for every rate change (an ARM "
                     "reset, a refinance). Rows must be in ascending date order.")
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        self.rates_table = QTableWidget(0, 2)
        self.rates_table.setHorizontalHeaderLabels(["Effective date", "Annual rate %"])
        self.rates_table.horizontalHeader().setStretchLastSection(True)
        self.rates_table.verticalHeader().setVisible(False)
        lay.addWidget(self.rates_table, 1)
        lay.addLayout(self._table_buttons(self.rates_table, self.add_rate_row))
        # Interest-expense category the computed-interest split line posts to. It
        # is per-LOAN (not per rate row): the same expense category applies across
        # every rate period, so it lives here alongside the rate history rather
        # than being hard-coded. Leave blank to fall back to the module default
        # ('Interest Exp'). Editable so a not-yet-created path can be typed; it is
        # get-or-created on save like every category.
        ic_row = QHBoxLayout()
        ic_lbl = QLabel("Interest-expense category:")
        ic_lbl.setToolTip("Where each payment's interest posts (e.g. 'Int Exp' or "
                          "'Landlord:Int Exp'). Blank uses the default.")
        ic_row.addWidget(ic_lbl)
        self.interest_category = QComboBox()
        self.interest_category.setEditable(True)
        self.interest_category.addItem("")   # blank => module-default fallback
        for c in ledger.list_categories(self.conn):
            self.interest_category.addItem(c["path"])
        self.interest_category.setCurrentText("")
        ic_row.addWidget(self.interest_category, 1)
        lay.addLayout(ic_row)
        return page

    def _build_extras_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lbl = QLabel("Extra amounts folded into every payment, each posting to "
                     "its own category (escrow, PMI, HOA...). Each amount applies "
                     "on and after its effective date -- add another row for the "
                     "same category to change escrow/PMI mid-loan (payments before "
                     "the new date keep the old amount). Leave empty for a plain "
                     "principal + interest loan.")
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        self.extras_table = QTableWidget(0, 5)
        self.extras_table.setHorizontalHeaderLabels(
            ["Category", "Amount ($)", "Effective date", "Label",
             "New total payment ($)"])
        self.extras_table.horizontalHeader().setStretchLastSection(True)
        self.extras_table.verticalHeader().setVisible(False)
        lay.addWidget(self.extras_table, 1)
        lay.addLayout(
            self._table_buttons(self.extras_table, self._add_extra_row_interactive))
        return page

    def _build_review_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lbl = QLabel("Review -- the first payment's split is previewed below. "
                     "Finish to save the loan parameters.")
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        self.review_text = QPlainTextEdit()
        self.review_text.setReadOnly(True)
        lay.addWidget(self.review_text, 1)
        return page

    def _table_buttons(self, table, adder):
        row = QHBoxLayout()
        add = QPushButton("Add row")
        add.clicked.connect(lambda: adder())
        rem = QPushButton("Remove selected")
        rem.clicked.connect(lambda: self._remove_selected(table))
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch()
        return row

    # ---- account choice ---------------------------------------------------
    def _populate_accounts(self):
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for a in ledger.list_accounts(self.conn, include_closed=True):
            if a["type"] == "liability":
                self.account_combo.addItem(a["name"], a["id"])
        self.account_combo.addItem("Create a new loan account…", _NEW_ACCOUNT)
        self.account_combo.blockSignals(False)
        self._on_account_choice()

    def _on_account_choice(self, *_):
        self.new_name.setEnabled(self.account_combo.currentData() == _NEW_ACCOUNT)

    def _funding_account_id(self):
        """The chosen 'Paid from' account id, or None for the unset default.
        Resolves the combo's current text against the fundable-account map, so a
        typed-and-completed name reads back even when no dropdown item was
        formally selected, and falls back to the selected item's stored id."""
        text = self.funding_combo.currentText().strip()
        if text in self._funding_by_name:
            return self._funding_by_name[text]
        data = self.funding_combo.currentData()
        return int(data) if data is not None else None

    # ---- effective-date cells ---------------------------------------------
    def _set_date_cell(self, table, r, c, iso=""):
        """Put the app's standard date editor (typeable AND calendar-pickable, in
        the user's chosen date format) into a table cell, seeded from an ISO date.
        Read it back with :meth:`_date_cell_iso`. Replaces the bare ISO-text cell
        the rates/extras effective-date columns used to carry, which ignored the
        date-format preference; storage/domain stay ISO, only display/entry follow
        the preference (the single chokepoint every date field goes through)."""
        from PyQt5.QtCore import QDate
        edit = make_date_edit(blank_ok=True)
        d = QDate.fromString(str(iso or "").strip(), "yyyy-MM-dd")
        # An unseeded cell stays blank (the minimum sentinel) rather than snapping
        # to today, so a truly empty rate/extra row is still collected as empty.
        edit.setDate(d if d.isValid() else edit.minimumDate())
        table.setCellWidget(r, c, edit)
        return edit

    def _date_cell_iso(self, table, r, c):
        """The ISO ``YYYY-MM-DD`` value of a date-editor cell ("" when blank),
        falling back to a plain item's text for robustness."""
        w = table.cellWidget(r, c)
        if w is not None:
            return date_edit_iso(w).strip()
        return self._cell_text(table, r, c)

    # ---- row helpers ------------------------------------------------------
    def add_rate_row(self, effective_date="", annual_rate=""):
        t = self.rates_table
        r = t.rowCount()
        t.insertRow(r)
        # The effective date is a date editor (user's format, calendar-pickable),
        # not raw ISO text -- storage stays ISO, display/entry follow the pref.
        self._set_date_cell(t, r, 0, effective_date)
        t.setItem(r, 1, QTableWidgetItem(str(annual_rate)))

    def add_extra_row(self, category="", amount="", label="", effective_date="",
                      new_payment=""):
        t = self.extras_table
        r = t.rowCount()
        t.insertRow(r)
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(_EXTRA_SUGGESTIONS)
        combo.setCurrentText(str(category))
        t.setCellWidget(r, 0, combo)
        t.setItem(r, 1, QTableWidgetItem(str(amount)))
        # New rows default to the first payment date, so a plain loan needs no
        # date typed; a mid-loan escrow change is entered with its own later date.
        # The date is a date editor (user's format, calendar-pickable), not raw
        # ISO text -- storage stays ISO, display/entry follow the pref.
        if not effective_date:
            effective_date = date_edit_iso(self.first_payment)
        self._set_date_cell(t, r, 2, effective_date)
        t.setItem(r, 3, QTableWidgetItem(str(label)))
        # The whole scheduled payment moves with a dated escrow/extra change; this
        # cell captures the new TOTAL (persisted to loan_payments) and defaults to
        # the recomputed payment, but the user can override it -- it is authoritative.
        t.setItem(r, 4, QTableWidgetItem(str(new_payment)))

    def _add_extra_row_interactive(self):
        """Add-row button handler: seed the New-total-payment cell with the
        recomputed default for the first-payment date so the user sees a sensible
        figure to accept or override."""
        cents = self._recomputed_total_cents(date_edit_iso(self.first_payment))
        new_payment = f"{cents / 100:.2f}" if cents is not None else ""
        self.add_extra_row(new_payment=new_payment)

    def _remove_selected(self, table):
        rows = sorted({i.row() for i in table.selectedIndexes()}, reverse=True)
        for r in rows:
            table.removeRow(r)

    @staticmethod
    def _cell_text(table, r, c):
        item = table.item(r, c)
        return item.text().strip() if item is not None else ""

    def _extra_category(self, r):
        w = self.extras_table.cellWidget(r, 0)
        if w is not None:
            return w.currentText().strip()
        return self._cell_text(self.extras_table, r, 0)

    # ---- new-total-payment default ----------------------------------------
    def _recomputed_total_cents(self, effective_date):
        """Best-effort recomputed TOTAL payment (cents) for ``effective_date``
        from the current wizard state: level principal+interest over the original
        principal/term at the rate in force, plus the extras active on that date
        (mirrors the Review page's 'typical total payment'). Returns None when the
        inputs are not yet complete enough to compute."""
        if not _is_iso_date(effective_date):
            return None
        v = self._collect()
        if v["principal_cents"] <= 0 or v["term_months"] < 1 or not v["rates"]:
            return None
        ppy = loans._INTERVALS.get(v["interval"], (12,))[0]
        rate_rows = [loans.RateRow(d, _parse_rate(rate)) for d, rate in v["rates"]
                     if _is_iso_date(d) and _parse_rate(rate) is not None]
        if not rate_rows:
            return None
        active = loans._active_rate(rate_rows, effective_date)
        if active is None:
            return None
        try:
            pi = loans.standard_payment(
                v["principal_cents"], active, v["term_months"], ppy)
        except Exception:
            return None
        extra_rows = [loans.ExtraLine(cat, _parse_cents_or_none(amt) or 0, None, eff)
                      for cat, amt, eff, _l in v["extras"] if _is_iso_date(eff)]
        extras_total = sum(
            e.amount for e in loans._active_extras(extra_rows, effective_date))
        return pi + extras_total

    def _stored_payments_map(self):
        """The dated total-payment history already stored for the selected
        account, as ``{effective_date: cents}`` (empty for a brand-new loan)."""
        data = self.account_combo.currentData()
        if data in (None, _NEW_ACCOUNT):
            return {}
        lp = loans.get_loan_params(self.conn, int(data))
        if lp is None:
            return {}
        return {p.effective_date: p.amount for p in lp.payments}

    def _prefill_new_totals(self):
        """Fill any blank New-total-payment cell with the amount already stored
        for that date, else the recomputed default -- so the field is defaulted
        yet stays user-authoritative (a value already typed is left alone)."""
        stored = self._stored_payments_map()
        t = self.extras_table
        for r in range(t.rowCount()):
            if self._cell_text(t, r, 4):
                continue
            eff = self._date_cell_iso(t, r, 2)
            cents = stored.get(eff)
            if cents is None:
                cents = self._recomputed_total_cents(eff)
            if cents is not None:
                t.setItem(r, 4, QTableWidgetItem(f"{cents / 100:.2f}"))

    # ---- navigation -------------------------------------------------------
    def _show_step(self, index):
        index = max(0, min(index, self.stack.count() - 1))
        self.stack.setCurrentIndex(index)
        self.step_label.setText(
            f"Step {index + 1} of {self.stack.count()}: {self.STEP_TITLES[index]}")
        self.back_btn.setEnabled(index > 0)
        last = index == self.stack.count() - 1
        self.next_btn.setVisible(not last)
        self.finish_btn.setVisible(last)
        self.finish_btn.setDefault(last)
        if index == 2:                    # entering the rates step
            self._seed_first_rate()
        if index == 3:                    # entering the extras step
            self._prefill_new_totals()
        if last:
            self._refresh_review()

    def _go_back(self):
        self._show_step(self.stack.currentIndex() - 1)

    def _go_next(self):
        self._show_step(self.stack.currentIndex() + 1)

    def _seed_first_rate(self):
        """Convenience: give the rates step a first row dated to the first
        payment, so a plain fixed-rate loan needs only its rate typed in."""
        t = self.rates_table
        fp = date_edit_iso(self.first_payment)
        if t.rowCount() == 0:
            self.add_rate_row(fp, "")
        elif t.rowCount() == 1:
            if fp and not self._date_cell_iso(t, 0, 0):
                self._set_date_cell(t, 0, 0, fp)

    # ---- collect / validate / persist -------------------------------------
    def _collect(self):
        data = self.account_combo.currentData()
        if data == _NEW_ACCOUNT:
            account_id, new_name = None, self.new_name.text().strip()
        else:
            account_id = int(data) if data is not None else None
            new_name = ""
        rates = []
        for r in range(self.rates_table.rowCount()):
            d = self._date_cell_iso(self.rates_table, r, 0)
            rate = self._cell_text(self.rates_table, r, 1)
            if not d and not rate:
                continue
            rates.append((d, rate))
        extras = []
        new_totals = []
        for r in range(self.extras_table.rowCount()):
            cat = self._extra_category(r)
            amt = self._cell_text(self.extras_table, r, 1)
            eff = self._date_cell_iso(self.extras_table, r, 2)
            label = self._cell_text(self.extras_table, r, 3)
            # A dated new total payment (persisted to loan_payments) needs a valid
            # effective date to key it; a blank/invalid cell just means "no dated
            # override" and leaves the schedule on its recomputed default. Collect
            # it BEFORE the extras filter so a row carrying ONLY a new total +
            # effective date (a pure payment change with no escrow/PMI category) is
            # not discarded -- that discard was half of why editing the New Payment
            # field appeared to do nothing.
            total_cents = _parse_cents_or_none(self._cell_text(self.extras_table, r, 4))
            if total_cents is not None and _is_iso_date(eff):
                new_totals.append((eff, total_cents))
            # An extras (escrow/PMI) row still needs a category or amount; a pure
            # new-total row carries neither and must not become an empty extra.
            if not cat and not amt:
                continue
            extras.append((cat, amt, eff, label))
        return {
            "account_id": account_id,
            "new_name": new_name,
            "principal_cents": int(round(self.principal.value() * 100)),
            "payment_cents": int(round(self.payment.value() * 100)),
            "first_payment": date_edit_iso(self.first_payment),
            "term_months": self.term_months.value(),
            "interval": self.interval.currentData(),
            "rates": rates,
            "extras": extras,
            "new_totals": new_totals,
            "funding_account_id": self._funding_account_id(),
            "interest_category": self.interest_category.currentText().strip(),
        }

    def validate(self):
        """Pure validation (no dialog): returns ``(ok, message)``."""
        v = self._collect()
        if v["account_id"] is None:
            if not v["new_name"]:
                return False, "Enter a name for the new loan account."
            if ledger.get_account_by_name(self.conn, v["new_name"]) is not None:
                return False, f"An account named {v['new_name']!r} already exists."
        if v["principal_cents"] <= 0:
            return False, "Original principal must be greater than zero."
        if v["term_months"] < 1:
            return False, "Term must be at least one month."
        if not _is_iso_date(v["first_payment"]):
            return False, "Enter the first payment date as YYYY-MM-DD."
        if v["interval"] not in loans._INTERVALS:
            return False, "Choose a payment interval."
        if v["payment_cents"] <= 0:
            return False, "Payment amount must be greater than zero."
        if not v["rates"]:
            return False, "Add at least one interest-rate row."
        last_date = None
        for d, rate in v["rates"]:
            if not _is_iso_date(d):
                return False, f"Rate effective date {d!r} is not a valid date (YYYY-MM-DD)."
            if _parse_rate(rate) is None:
                return False, f"Rate {rate!r} is not a valid non-negative number."
            if last_date is not None and d <= last_date:
                return False, ("Interest-rate rows must be in ascending date "
                               "order, with no duplicate dates.")
            last_date = d
        extra_rows, seen = [], set()
        for cat, amt, eff, _label in v["extras"]:
            if not cat:
                return False, "Every extra-amount line needs a category."
            cents = parse_amount(amt)
            if cents <= 0:
                return False, f"Extra amount for {cat!r} must be greater than zero."
            if not _is_iso_date(eff):
                return False, (f"Extra {cat!r} needs a valid effective date "
                               "(YYYY-MM-DD).")
            if (cat, eff) in seen:
                return False, (f"Two {cat!r} amounts share the effective date "
                               f"{eff}: use one amount per category per date.")
            seen.add((cat, eff))
            extra_rows.append(loans.ExtraLine(cat, cents, None, eff))
        # Amortization sanity check, kept strictly TIME-ALIGNED: everything is
        # measured at the SAME period (the first payment). The interest and extras
        # in force then, and -- crucially -- the payment in force then, which is a
        # dated New-total override when the user entered one for that date
        # (loans._active_payment), NOT the raw step-2 amount. Pitting the step-2
        # (initial) payment against a later-edited (current) escrow is what made a
        # consistent escrow+payment edit in step 4 false-trip "too small".
        extras_total = sum(
            e.amount for e in loans._active_extras(extra_rows, v["first_payment"]))
        ppy = loans._INTERVALS[v["interval"]][0]
        rate_rows = [loans.RateRow(d, _parse_rate(rate)) for d, rate in v["rates"]]
        active = loans._active_rate(rate_rows, v["first_payment"])
        first_interest = loans._cents(
            Decimal(v["principal_cents"]) * loans._period_rate(active, ppy))
        payment_rows = [loans.PaymentRow(eff, cents) for eff, cents in v["new_totals"]]
        first_payment_amt = loans._active_payment(
            payment_rows, v["payment_cents"], v["first_payment"])
        first_principal = first_payment_amt - first_interest - extras_total
        if first_principal < 0:
            # The payment is SMALLER than interest + extras: it would ADD to the
            # balance (negative amortization) rather than pay the loan down. A
            # distinct problem from a payment that merely fails to reduce principal,
            # and worth a distinct message (payment_split would compute principal
            # negative, so the [Loan] leg would move money the wrong way).
            return False, (
                "This payment would INCREASE the loan balance. On "
                f"{fmt_date(v['first_payment'])} the payment is "
                f"{fmt_money(first_payment_amt)}, but the period's interest "
                f"({fmt_money(first_interest)}) plus extra amounts "
                f"({fmt_money(extras_total)}) total "
                f"{fmt_money(first_interest + extras_total)} -- more than the "
                "payment. Raise the payment or lower the extras.")
        if first_principal == 0:
            # Covers interest + extras exactly, leaving nothing for principal, so
            # the balance never declines and the loan never amortizes.
            return False, ("The payment is too small: it must cover the first "
                           f"period's interest ({fmt_money(first_interest)}) plus "
                           f"extra amounts ({fmt_money(extras_total)}) and leave "
                           "something toward principal.")
        return True, ""

    def persist(self):
        """Write the loan parameters (assumes :meth:`validate` already passed),
        creating the account first if this is a new loan. Returns the account id."""
        v = self._collect()
        account_id = v["account_id"]
        if account_id is None:
            account_id = ledger.create_account(
                self.conn, v["new_name"], "liability",
                opening_balance=-v["principal_cents"])
        origination = loans.origination_from_first_payment(
            v["first_payment"], v["interval"])
        extras = [(cat, parse_amount(amt), (label or None), (eff or None))
                  for cat, amt, eff, label in v["extras"]]
        loans.set_loan_params(
            self.conn, account_id,
            original_principal=v["principal_cents"],
            term_months=v["term_months"],
            payment_amount=v["payment_cents"],
            origination_date=origination,
            interval=v["interval"],
            rates=v["rates"],
            extras=extras,
            interest_category=(v["interest_category"] or None),
            funding_account_id=v["funding_account_id"],
        )
        # Record each dated new TOTAL payment AND re-derive the schedule from its
        # effective date forward: apply_payment_change stores the dated total
        # (loan_payments) and re-splits every pending/posted payment on/after the
        # date -- interest = prior balance x periodic rate, principal = total -
        # interest - extras, balance cascading -- so the checking (transfer) and
        # loan registers show the new split/running balance. Preserve ONLY the
        # total; everything else is computed. (Bare add_payment_change stored the
        # total but never re-split existing rows, so the edit appeared to do
        # nothing.) Idempotent: it overwrites existing splits, creating no rows.
        for eff, total_cents in v["new_totals"]:
            loans_schedule.apply_payment_change(
                self.conn, account_id, eff,
                change_type="payment", new_payment_amount=total_cents)
        # Reminders already sitting in a register follow the new setup: a
        # changed "Paid from" moves them to that account, a changed payment,
        # rate or extra re-splits them. Otherwise the rows the user is looking
        # at would still show the old account and the old split.
        loans_schedule.realign_pending_payments(self.conn, account_id)
        self.saved_account_id = account_id
        return account_id

    def save(self):
        """Validate then persist. Returns True on success, False if invalid
        (no dialog is shown -- callers/tests decide how to report)."""
        ok, _msg = self.validate()
        if not ok:
            return False
        self.persist()
        return True

    def _on_finish(self):
        ok, msg = self.validate()
        if not ok:
            QMessageBox.warning(self, "Loan Setup", msg)
            return
        self.persist()
        self.accept()

    # ---- review preview ---------------------------------------------------
    def _refresh_review(self):
        ok, msg = self.validate()
        if not ok:
            self.review_text.setPlainText("Not ready to save yet:\n\n" + msg)
            return
        v = self._collect()
        ppy = loans._INTERVALS[v["interval"]][0]
        rate_rows = [loans.RateRow(d, _parse_rate(rate)) for d, rate in v["rates"]]
        active = loans._active_rate(rate_rows, v["first_payment"])
        first_interest = loans._cents(
            Decimal(v["principal_cents"]) * loans._period_rate(active, ppy))
        extra_rows = [loans.ExtraLine(cat, parse_amount(amt), None, eff)
                      for cat, amt, eff, _l in v["extras"]]
        extras_total = sum(
            e.amount for e in loans._active_extras(extra_rows, v["first_payment"]))
        # The payment in force on the first payment (a dated New-total override
        # wins over the step-2 amount), so the previewed split matches what the
        # schedule will actually post -- same time-alignment as validate().
        payment_rows = [loans.PaymentRow(eff, cents) for eff, cents in v["new_totals"]]
        first_payment_amt = loans._active_payment(
            payment_rows, v["payment_cents"], v["first_payment"])
        first_principal = first_payment_amt - first_interest - extras_total
        suggested_pi = loans.standard_payment(
            v["principal_cents"], active, v["term_months"], ppy)
        if v["account_id"] is not None:
            acct = ledger.get_account(self.conn, v["account_id"])
            name = acct["name"] if acct else "(account)"
        else:
            name = v["new_name"]
        lines = [
            f"Account:            {name}",
            f"Original principal: {fmt_money(v['principal_cents'])}",
            f"Term:               {v['term_months']} months, {v['interval']} payments",
            f"First payment:      {fmt_date(v['first_payment'])}  (rate {active}%)",
            "",
            f"Total payment:      {fmt_money(first_payment_amt)}",
            f"  first interest:   {fmt_money(first_interest)}",
            f"  escrow / extras:  {fmt_money(extras_total)}",
            f"  first principal:  {fmt_money(first_principal)}",
            "",
            f"Level principal+interest for this rate/term: {fmt_money(suggested_pi)}",
            f"(a typical total payment would be about "
            f"{fmt_money(suggested_pi + extras_total)})",
        ]
        self.review_text.setPlainText("\n".join(lines))

    # ---- edit path --------------------------------------------------------
    def _load_existing(self, account_id):
        """Pre-select ``account_id`` and load its stored parameters (if any) into
        the pages, so the wizard doubles as an editor."""
        idx = self.account_combo.findData(account_id)
        if idx >= 0:
            self.account_combo.setCurrentIndex(idx)
        lp = loans.get_loan_params(self.conn, account_id)
        if lp is None:
            return
        # Stored funder, else the one history implies, so saving without
        # touching the field records what the loan has always been paid from.
        funder = loans.funding_account(self.conn, account_id)
        fi = self.funding_combo.findData(funder) if funder is not None else -1
        self.funding_combo.setCurrentIndex(fi if fi >= 0 else 0)
        self.principal.setValue(lp.original_principal / 100.0)
        self.term_months.setValue(lp.term_months)
        self.payment.setValue(lp.payment_amount / 100.0)
        ii = self.interval.findData(lp.interval)
        if ii >= 0:
            self.interval.setCurrentIndex(ii)
        try:
            _seed_date(self.first_payment, lp.first_payment_date())
        except ValueError:
            _seed_date(self.first_payment, lp.origination_date)
        self.rates_table.setRowCount(0)
        for rr in lp.rates:
            self.add_rate_row(rr.effective_date, loans._rate_text(rr.annual_rate))
        self.interest_category.setCurrentText(lp.interest_category or "")
        self.extras_table.setRowCount(0)
        for ex in lp.extras:
            self.add_extra_row(ex.category, f"{ex.amount / 100:.2f}",
                               ex.label or "", ex.effective_date or "")

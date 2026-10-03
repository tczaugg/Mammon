"""Social Security: who is in the household, what they earned, and what each
claim age buys (SRD 5.9c).

This is the surface over :mod:`mammon.retirement` and nothing more. It holds no
SQL and no money math: people are written through ``add_person`` /
``update_person`` / ``delete_person``, earnings through ``set_earnings`` /
``replace_earnings``, and every figure on the benefit panel comes from
``monthly_benefit``. The domain layer stays the only writer, exactly as the
register does with :mod:`mammon.ledger`.

Three panels, top to bottom, because that is the order the arithmetic runs in:

* **The household.** Name, relationship, birth MONTH and YEAR, the
  born-on-the-first flag, and a planned claim age. A full birth date is never
  demanded: the only day-of-month that changes a Social Security answer is the
  first, because SSA follows the common-law rule that a person attains an age
  on the day before their birthday (20 CFR 404.2(c)(4)), so a checkbox covers
  the entire edge case and the app never asks for a date it has no use for.

* **The earnings history.** Either typed off the SSA Earnings Report or
  estimated by Mammon from this ledger's wage categories -- both
  are offered -- with per-year projections for the years since the statement. Which
  basis is in force is printed above the table, always, not hidden in a mode
  the user has to remember setting: the two bases produce visibly different
  benefits, and a figure whose basis is off-screen is a figure the reader
  cannot check. Typing over an ESTIMATED year promotes it to reported, so the
  correction survives the next re-estimate (``replace_earnings`` only replaces
  rows it wrote itself).

* **The benefit.** One row per claim age -- 62, full retirement age, 70, plus
  the planned age when it is something else. Every assumption behind the column
  prints underneath it: the AIME, how many of SSA's 35 averaging years are
  zeros, that no future COLA is projected, and what the earnings rest on.
  Mammon shows the rule and never the recommendation; which age to claim is a
  judgment about health, work and a spouse that this program has no business
  making.

  What this table no longer carries is PROVENANCE. It used to name the bend
  points, PIA factors and full-retirement-age tables on every row, and raise an
  amber triangle when one of them was an edition behind. The citation repeated
  itself down the column and was unreadable there, and the triangle means
  MISSING OR CONFLICTING DATA everywhere else in the app -- an old-but-correct
  published table is neither. Both moved to ``ui/retirement_faq.py``, which
  renders one provenance row per rule table, once.

Modal safety (CLAUDE.md, headless-modal hazard): the only modal is the delete
confirmation, routed through the overridable ``_confirm`` seam over
``QMessageBox.question``. Everything else -- adding a person, adding a year,
estimating from the ledger, a rejected edit -- reports through the
``self.notice`` label, so nothing here can open a window that blocks forever
under the offscreen platform.

Editor safety, the same root cause one step over: both tables edit in place
with cell widgets, so a handler must never rebuild the rows while the widget
that called it is still emitting -- ``setCellWidget`` deletes the old widget
immediately, and deleting a combo box inside its own ``currentIndexChanged``
is how you get a silent ``0xc0000374``. An in-place edit therefore refreshes
only the derived text (``_refresh_derived``), and the two edits that really do
need the rows rebuilt defer it with ``QTimer.singleShot(0, ...)``, exactly as
``RegisterModel._write`` defers its reload.
"""
from __future__ import annotations

import datetime as _dt

from PyQt5.QtCore import QDate, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGroupBox,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from mammon import retirement
from mammon.ui.delegates import date_edit_iso, make_date_edit, NoWheelComboBox, NoWheelSpinBox
from mammon.ui.models import fmt_cents, fmt_date, fmt_money, parse_amount

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

#: The spin box has no empty state, so its minimum doubles as "not given" and
#: renders as ``?``. Nobody planning a retirement was born in 1900.
UNSET_YEAR = 1900

_SOURCE_LABELS = {
    "reported": "SSA Earnings Report",
    "estimated": "estimated from this ledger",
    "projected": "projected",
}


def claim_age_entries(birth_year) -> list[tuple[str, object]]:
    """``[(label, months or None)]`` for the planned-claim-age combo.

    Whole years from 62 to 70, with full retirement age labeled where it falls
    -- on its own line when it is not a whole year, which it is not for anyone
    born after 1954."""
    entries: list[tuple[str, object]] = [("(not decided)", None)]
    fra = None
    if birth_year:
        fra = retirement.full_retirement_age_months(int(birth_year))
    for age in range(62, 71):
        months = age * 12
        label = str(age)
        if fra == months:
            label = f"{age} (full retirement age)"
        entries.append((label, months))
        if fra is not None and months < fra < months + 12:
            entries.append(
                (f"{fra // 12} and {fra % 12}mo (full retirement age)", fra)
            )
    return entries


class SocialSecurityDialog(QDialog):
    """The household's Social Security picture, computed and cited.

    ``changed`` fires on every write so the retirement planner page can
    recompute the projection it draws."""

    changed = pyqtSignal()

    PEOPLE_COLUMNS = (
        "Name", "Relationship", "Birth month", "Birth year", "Born on the 1st",
        "Planned claim age", "Part D",
    )
    EARNINGS_COLUMNS = ("Year", "Earnings", "Where this figure came from")
    BENEFIT_COLUMNS = ("If claimed at", "Monthly benefit")

    def __init__(self, conn, parent=None, today=None):
        super().__init__(parent)
        self.conn = conn
        self._today = today or _dt.date.today()
        self._loading = False
        self._people: list[dict] = []
        self._earnings: list[dict] = []
        self._benefits: list[dict] = []
        self.setWindowTitle("Social Security")
        self.resize(820, 760)

        lay = QVBoxLayout(self)
        blurb = QLabel(
            "Mammon computes what each claim age would pay and names the rule "
            "that produced the figure. It does not recommend a claim age -- "
            "that answer depends on health, on whether you keep working and on "
            "a survivor benefit, none of which this program knows."
        )
        blurb.setWordWrap(True)
        lay.addWidget(blurb)

        lay.addWidget(self._build_people_group(), 1)
        lay.addWidget(self._build_earnings_group(), 2)
        lay.addWidget(self._build_benefit_group(), 2)
        lay.addWidget(self._build_assumptions_group())

        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        lay.addWidget(self.notice)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.reload()

    # ---- construction ----------------------------------------------------
    def _build_people_group(self):
        box = QGroupBox("Household")
        lay = QVBoxLayout(box)
        self.people_table = QTableWidget(0, len(self.PEOPLE_COLUMNS))
        self.people_table.setHorizontalHeaderLabels(list(self.PEOPLE_COLUMNS))
        self.people_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.people_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.people_table.verticalHeader().setVisible(False)
        self.people_table.itemChanged.connect(self._on_person_item_changed)
        self.people_table.currentCellChanged.connect(
            lambda *_: self._on_person_selected()
        )
        lay.addWidget(self.people_table)

        bar = QHBoxLayout()
        self.btn_add_person = QPushButton("Add Person")
        self.btn_add_person.clicked.connect(self.on_add_person)
        self.btn_remove_person = QPushButton("Remove Person")
        self.btn_remove_person.clicked.connect(self.on_remove_person)
        bar.addWidget(self.btn_add_person)
        bar.addWidget(self.btn_remove_person)
        bar.addStretch()
        lay.addLayout(bar)
        return box

    def _build_earnings_group(self):
        box = QGroupBox("Earnings history")
        lay = QVBoxLayout(box)
        self.basis_label = QLabel("")
        self.basis_label.setWordWrap(True)
        lay.addWidget(self.basis_label)

        stmt = QHBoxLayout()
        stmt.addWidget(QLabel("SSA Earnings Report dated"))
        self.statement_date = make_date_edit(self, "", blank_ok=True)
        self.statement_date.setDate(self.statement_date.minimumDate())
        self.statement_date.dateChanged.connect(self._on_statement_date_changed)
        stmt.addWidget(self.statement_date)
        stmt.addWidget(QLabel("(years after it are yours to project)"))
        stmt.addStretch()
        lay.addLayout(stmt)

        self.earnings_table = QTableWidget(0, len(self.EARNINGS_COLUMNS))
        self.earnings_table.setHorizontalHeaderLabels(list(self.EARNINGS_COLUMNS))
        self.earnings_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.earnings_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.earnings_table.verticalHeader().setVisible(False)
        self.earnings_table.itemChanged.connect(self._on_earnings_item_changed)
        lay.addWidget(self.earnings_table)

        bar = QHBoxLayout()
        self.btn_add_year = QPushButton("Add Year")
        # Through a lambda: ``clicked`` hands its checked flag to the first
        # positional argument, which here is the year.
        self.btn_add_year.clicked.connect(lambda: self.on_add_year())
        self.btn_remove_year = QPushButton("Remove Year")
        self.btn_remove_year.clicked.connect(self.on_remove_year)
        self.btn_estimate = QPushButton("Estimate from Ledger")
        self.btn_estimate.setToolTip(
            "Sum this ledger's wage-category deposits by calendar year. Years "
            "you typed yourself are left alone."
        )
        self.btn_estimate.clicked.connect(self.on_estimate)
        for b in (self.btn_add_year, self.btn_remove_year, self.btn_estimate):
            bar.addWidget(b)
        bar.addStretch()
        lay.addLayout(bar)

        self.earnings_notes = QLabel("")
        self.earnings_notes.setWordWrap(True)
        lay.addWidget(self.earnings_notes)
        return box

    def _build_benefit_group(self):
        box = QGroupBox("Monthly benefit")
        lay = QVBoxLayout(box)
        self.benefit_table = QTableWidget(0, len(self.BENEFIT_COLUMNS))
        self.benefit_table.setHorizontalHeaderLabels(list(self.BENEFIT_COLUMNS))
        self.benefit_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.benefit_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.benefit_table.verticalHeader().setVisible(False)
        lay.addWidget(self.benefit_table)
        self.benefit_notes = QLabel("")
        self.benefit_notes.setWordWrap(True)
        lay.addWidget(self.benefit_notes)
        return box

    def _build_assumptions_group(self):
        """The planner's forward-looking assumptions, moved here from a row on
        the Retirement Planner page (reported). Each saves as it is left and
        emits ``changed``; the planner re-applies its plan when the dialog
        closes. Bracket indexing is a tax assumption, but it rides with the
        COLA it is set against - both are "how fast do the numbers rise"."""
        from decimal import Decimal, InvalidOperation
        from PyQt5.QtWidgets import QFormLayout, QLineEdit, QSpinBox

        self._Decimal, self._InvalidOperation = Decimal, InvalidOperation
        box = QGroupBox("Assumptions for projecting")
        form = QFormLayout(box)
        self.cola_edit = QLineEdit(str(retirement.get_cola_pct(self.conn)))
        self.cola_edit.setMaximumWidth(60)
        self.cola_edit.setToolTip(
            "The yearly cost-of-living raise applied to Social Security after "
            "this year. Default: the Trustees Report's long-range CPI assumption.")
        form.addRow("Social Security COLA % per year", self.cola_edit)
        shortfall = retirement.get_ss_shortfall(self.conn)
        self.shortfall_year = NoWheelSpinBox()
        self.shortfall_year.setRange(self._today.year, self._today.year + 60)
        self.shortfall_year.setValue(max(self._today.year, shortfall.year))
        self.shortfall_pct = QLineEdit(str(shortfall.payable_pct))
        self.shortfall_pct.setMaximumWidth(60)
        row = QHBoxLayout()
        row.addWidget(self.shortfall_year)
        row.addWidget(QLabel("pays"))
        row.addWidget(self.shortfall_pct)
        row.addWidget(QLabel("% of scheduled benefits"))
        row.addStretch()
        form.addRow("From", row)
        tip = ("Social Security's retirement trust fund is projected to run out, "
               "after which payroll taxes pay only part of scheduled benefits. "
               "Default: the 2025 Trustees Report (2033, about 77%). 100 assumes "
               "full benefits.")
        self.shortfall_year.setToolTip(tip)
        self.shortfall_pct.setToolTip(tip)
        self.bracket_index_edit = QLineEdit(str(retirement.get_bracket_index_pct(self.conn)))
        self.bracket_index_edit.setMaximumWidth(60)
        self.bracket_index_edit.setToolTip(
            "How fast the tax bracket tops and the standard deduction rise each "
            "year after the published table (chained CPI-U, a little below the "
            "CPI behind the COLA).")
        form.addRow("Tax bracket indexing % per year", self.bracket_index_edit)
        self.end_ira_tax_edit = QLineEdit(str(retirement.get_end_ira_tax_pct(self.conn)))
        self.end_ira_tax_edit.setMaximumWidth(60)
        self.end_ira_tax_edit.setToolTip(
            "The rate applied to what the IRAs and 401(k)s still hold at the end "
            "of the plan - taxed later, to you or to your heirs. Counted in the "
            "planner's tax total so conversions, which shrink it, compare fairly.")
        form.addRow("Tax rate on IRAs left at the end %", self.end_ira_tax_edit)
        self.end_ira_tax_edit.editingFinished.connect(self._save_assumptions)
        self.medicare_growth_edit = QLineEdit(
            str(retirement.get_medicare_growth_pct(self.conn)))
        self.medicare_growth_edit.setMaximumWidth(60)
        self.medicare_growth_edit.setToolTip(
            "How fast Medicare premiums - and the IRMAA surcharges, a share of "
            "the same cost - grow each year. The standard Part B premium rose "
            "about 5% a year 2016-2026, faster than prices; the income tiers "
            "themselves follow inflation.")
        form.addRow("Medicare premium growth % per year", self.medicare_growth_edit)
        self.medicare_growth_edit.editingFinished.connect(self._save_assumptions)
        self.survivor_spending_edit = QLineEdit(
            str(retirement.get_survivor_spending_pct(self.conn)))
        self.survivor_spending_edit.setMaximumWidth(60)
        self.survivor_spending_edit.setToolTip(
            "Under the survivor scenario (set on the planner page), what the "
            "survivor lives on, as a share of the household's spending.")
        form.addRow("Survivor's spending % of the household's",
                    self.survivor_spending_edit)
        self.survivor_spending_edit.editingFinished.connect(self._save_assumptions)
        self.pension_survivor_edit = QLineEdit(
            str(retirement.get_pension_survivor_pct(self.conn)))
        self.pension_survivor_edit.setMaximumWidth(60)
        self.pension_survivor_edit.setToolTip(
            "Under the survivor scenario, the share of the deceased's pension "
            "that keeps paying. 50% is the least a married participant's "
            "joint-and-survivor annuity pays; 0 for a single-life pension.")
        form.addRow("Pension survivor benefit %", self.pension_survivor_edit)
        self.pension_survivor_edit.editingFinished.connect(self._save_assumptions)
        self.ssa44_check = QCheckBox("Assume the IRMAA appeal after retiring (Form SSA-44)")
        self.ssa44_check.setChecked(retirement.get_ssa44_appeal(self.conn))
        self.ssa44_check.setToolTip(
            "Retiring, cutting back work or a spouse's death lets you ask Social "
            "Security to set the Medicare surcharge from the current year's "
            "income instead of two years back. The planner uses it for the "
            "premium year of the event and the year after, when it is lower. "
            "A conversion is never itself a reason to appeal.")
        form.addRow("", self.ssa44_check)
        self.ssa44_check.toggled.connect(self._save_ssa44)
        self.state_tax_edit = QLineEdit(str(retirement.get_state_tax_pct(self.conn)))
        self.state_tax_edit.setMaximumWidth(60)
        self.state_tax_edit.setToolTip(
            "Your state's income tax, as one flat rate on federal adjusted gross "
            "income. 0 for a state without one.")
        form.addRow("State income tax %", self.state_tax_edit)
        self.state_tax_edit.editingFinished.connect(self._save_assumptions)
        self.state_ss_check = QCheckBox("State taxes Social Security")
        self.state_ss_check.setChecked(retirement.get_state_taxes_ss(self.conn))
        self.state_ss_check.setToolTip("Most states do not; a few do.")
        form.addRow("", self.state_ss_check)
        self.state_ss_check.toggled.connect(self._save_state_ss)
        self.state_retirement_check = QCheckBox("State exempts retirement income")
        self.state_retirement_check.setChecked(
            retirement.get_state_excludes_retirement(self.conn))
        self.state_retirement_check.setToolTip(
            "Pensions, IRA and 401(k) draws and Roth conversions left out of the "
            "state's tax, as many states do in whole or in part.")
        form.addRow("", self.state_retirement_check)
        self.state_retirement_check.toggled.connect(
            lambda on: self._save_flag(retirement.set_state_excludes_retirement, on))
        self.defer_rmd_check = QCheckBox("Take the first required minimum by the "
                                         "following April 1")
        self.defer_rmd_check.setChecked(retirement.get_defer_first_rmd(self.conn))
        self.defer_rmd_check.setToolTip(
            "IRC 401(a)(9)(C)(i): the first year's minimum may wait until April 1 "
            "of the next year, which then takes two. Nothing is drawn for it in "
            "the first year; the plan's other draws are unchanged.")
        form.addRow("", self.defer_rmd_check)
        self.defer_rmd_check.toggled.connect(
            lambda on: self._save_flag(retirement.set_defer_first_rmd, on))
        self.community_check = QCheckBox("Community-property state")
        self.community_check.setChecked(retirement.get_community_property(self.conn))
        self.community_check.setToolTip(
            "Under the survivor scenario the couple's taxable holdings take a new "
            "cost basis at the first death: all of them in a community-property "
            "state (IRC 1014(b)(6)), half otherwise.")
        form.addRow("", self.community_check)
        self.community_check.toggled.connect(
            lambda on: self._save_flag(retirement.set_community_property, on))
        self.aca_edit = QLineEdit(fmt_cents(retirement.get_aca_benchmark_cents(self.conn)))
        self.aca_edit.setMaximumWidth(90)
        self.aca_edit.setToolTip(
            "Retiring before 65: the yearly premium of the marketplace's "
            "benchmark plan (the second-lowest-cost silver plan) for the "
            "household, in today's dollars - healthcare.gov shows it. The "
            "premium tax credit is that less a share of income rising from "
            "2.1% to 9.96% between the poverty line and four times it, and "
            "nothing above; the planner charges the credit its own draws and "
            "conversions cost. 0 if you will not buy marketplace coverage.")
        form.addRow("ACA benchmark premium before 65 ($/yr)", self.aca_edit)
        self.aca_edit.editingFinished.connect(self._save_assumptions)
        # IRMAA in the plan's first two years is set by returns from before
        # the plan, which nothing here can read: they are typed.
        self.prior_magi_edits = {}
        for back in (2, 1):
            year = self._today.year - back
            edit = QLineEdit(fmt_cents(retirement.get_prior_magi(self.conn, year) or 0))
            edit.setMaximumWidth(90)
            edit.setToolTip(
                f"Adjusted gross income plus tax-exempt interest on the {year} "
                f"return. It sets the Medicare surcharge (IRMAA) for {year + 2}, "
                "a year the plan has no income of its own to read it from. "
                "0 = not entered, and that year is not charged.")
            form.addRow(f"Modified AGI for {year} ($)", edit)
            edit.editingFinished.connect(self._save_assumptions)
            self.prior_magi_edits[year] = edit
        self.cola_edit.editingFinished.connect(self._save_assumptions)
        self.shortfall_year.editingFinished.connect(self._save_assumptions)
        self.shortfall_pct.editingFinished.connect(self._save_assumptions)
        self.bracket_index_edit.editingFinished.connect(self._save_assumptions)
        return box

    def _save_state_ss(self, on: bool) -> None:
        retirement.set_state_taxes_ss(self.conn, bool(on))
        self.changed.emit()

    def _save_flag(self, setter, on: bool) -> None:
        setter(self.conn, bool(on))
        self.changed.emit()

    def _save_ssa44(self, on: bool) -> None:
        retirement.set_ssa44_appeal(self.conn, bool(on))
        self.changed.emit()

    def _save_assumptions(self) -> None:
        D = self._Decimal
        try:
            cola = D(self.cola_edit.text().strip().rstrip("%") or "0")
            index = D(self.bracket_index_edit.text().strip().rstrip("%") or "0")
            payable = D(self.shortfall_pct.text().strip().rstrip("%") or "100")
            end_tax = D(self.end_ira_tax_edit.text().strip().rstrip("%") or "0")
            growth = D(self.medicare_growth_edit.text().strip().rstrip("%") or "0")
            state = D(self.state_tax_edit.text().strip().rstrip("%") or "0")
            aca = abs(parse_amount(self.aca_edit.text().strip() or "0"))
            prior = {year: abs(parse_amount(edit.text().strip() or "0"))
                     for year, edit in self.prior_magi_edits.items()}
            survivor = D(self.survivor_spending_edit.text().strip().rstrip("%") or "0")
            pension = D(self.pension_survivor_edit.text().strip().rstrip("%") or "0")
        except (self._InvalidOperation, ValueError):
            self.notice.setText("Each assumption has to be a percentage, like 2.4, "
                                "or an amount, like 85,000.")
            return
        changed = False
        for year, cents in prior.items():
            if cents != (retirement.get_prior_magi(self.conn, year) or 0):
                retirement.set_prior_magi(self.conn, year, cents)
                changed = True
        if cola != retirement.get_cola_pct(self.conn):
            retirement.set_cola_pct(self.conn, cola)
            changed = True
        if index != retirement.get_bracket_index_pct(self.conn):
            retirement.set_bracket_index_pct(self.conn, index)
            changed = True
        for value, getter, setter in (
                (survivor, retirement.get_survivor_spending_pct,
                 retirement.set_survivor_spending_pct),
                (pension, retirement.get_pension_survivor_pct,
                 retirement.set_pension_survivor_pct)):
            if value != getter(self.conn):
                try:
                    setter(self.conn, value)
                except ValueError as exc:
                    self.notice.setText(f"Not changed: {exc}")
                    return
                changed = True
        if state != retirement.get_state_tax_pct(self.conn):
            try:
                retirement.set_state_tax_pct(self.conn, state)
            except ValueError as exc:
                self.notice.setText(f"Not changed: {exc}")
                return
            changed = True
        if aca != retirement.get_aca_benchmark_cents(self.conn):
            retirement.set_aca_benchmark_cents(self.conn, aca)
            changed = True
        if growth != retirement.get_medicare_growth_pct(self.conn):
            try:
                retirement.set_medicare_growth_pct(self.conn, growth)
            except ValueError as exc:
                self.notice.setText(f"Not changed: {exc}")
                return
            changed = True
        if end_tax != retirement.get_end_ira_tax_pct(self.conn):
            try:
                retirement.set_end_ira_tax_pct(self.conn, end_tax)
            except ValueError as exc:
                self.notice.setText(f"Not changed: {exc}")
                return
            changed = True
        wanted = retirement.SocialSecurityShortfall(self.shortfall_year.value(), payable)
        if wanted != retirement.get_ss_shortfall(self.conn):
            try:
                retirement.set_ss_shortfall(self.conn, wanted.year, wanted.payable_pct)
            except ValueError as exc:
                self.notice.setText(f"Not changed: {exc}")
                return
            changed = True
        if changed:
            self.changed.emit()

    # ---- seams -----------------------------------------------------------
    def _confirm(self, title: str, text: str) -> bool:
        """The one modal here, isolated so a headless test can answer it.

        Same shape as the tags dialog's confirm: tests patch this method rather
        than trying to click a message box that the offscreen platform would
        never show."""
        return QMessageBox.question(
            self, title, text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) == QMessageBox.Yes

    # ---- display ---------------------------------------------------------
    def reload(self):
        """Re-read everything: household, then the selected person's history."""
        selected = self.selected_person_id()
        self._people = retirement.list_people(self.conn)
        self._loading = True
        try:
            self.people_table.setRowCount(len(self._people))
            for row, person in enumerate(self._people):
                self._fill_person_row(row, person)
            self.people_table.resizeColumnsToContents()
        finally:
            self._loading = False
        if self._people:
            ids = [p["id"] for p in self._people]
            row = ids.index(selected) if selected in ids else 0
            self.people_table.selectRow(row)
        self._sync_buttons()
        self.reload_earnings()

    def _fill_person_row(self, row: int, person: dict):
        name = QTableWidgetItem(str(person["name"] or ""))
        name.setData(Qt.UserRole, int(person["id"]))
        self.people_table.setItem(row, 0, name)

        rel = NoWheelComboBox()
        rel.addItems(list(retirement.RELATIONSHIPS))
        rel.setCurrentText(str(person["relationship"] or "other"))
        rel.currentTextChanged.connect(
            lambda text, pid=int(person["id"]): self._write_person(pid, relationship=text)
        )
        self.people_table.setCellWidget(row, 1, rel)

        month = NoWheelComboBox()
        month.addItem("?", None)
        for i, label in enumerate(MONTH_NAMES, start=1):
            month.addItem(label, i)
        month.setCurrentIndex(int(person["birth_month"] or 0))
        month.currentIndexChanged.connect(
            lambda _i, pid=int(person["id"]), combo=month: self._write_person(
                pid, birth_month=combo.currentData()
            )
        )
        self.people_table.setCellWidget(row, 2, month)

        year = NoWheelSpinBox()
        year.setRange(UNSET_YEAR, self._today.year)
        year.setSpecialValueText("?")
        year.setValue(int(person["birth_year"] or UNSET_YEAR))
        year.valueChanged.connect(
            lambda value, pid=int(person["id"]): self._write_person(
                pid, birth_year=(None if value == UNSET_YEAR else value)
            )
        )
        self.people_table.setCellWidget(row, 3, year)

        first = QTableWidgetItem("")
        first.setFlags(
            (first.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable
        )
        first.setCheckState(
            Qt.Checked if person["born_on_the_first"] else Qt.Unchecked
        )
        first.setToolTip(
            "Social Security treats someone born on the first of a month as "
            "attaining each age in the PRIOR month."
        )
        self.people_table.setItem(row, 4, first)

        self._fill_claim_cell(row, person)

        part_d = QTableWidgetItem("")
        part_d.setFlags((part_d.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
        part_d.setCheckState(Qt.Checked if person.get("part_d", 1) else Qt.Unchecked)
        part_d.setToolTip(
            "Medicare Part D drug coverage (or a Medicare Advantage plan with "
            "drugs). Its income surcharge (IRMAA) is owed only with it.")
        self.people_table.setItem(row, 6, part_d)

    def _fill_claim_cell(self, row: int, person):
        """The claim-age combo alone: its LABELS depend on the birth year."""
        claim = NoWheelComboBox()
        for label, months in claim_age_entries(person["birth_year"]):
            claim.addItem(label, months)
        planned = person["planned_claim_age_months"]
        index = claim.findData(int(planned) if planned is not None else None)
        claim.setCurrentIndex(index if index >= 0 else 0)
        claim.currentIndexChanged.connect(
            lambda _i, pid=int(person["id"]), combo=claim: self._write_person(
                pid, planned_claim_age_months=combo.currentData()
            )
        )
        self.people_table.setCellWidget(row, 5, claim)

    def reload_earnings(self):
        """Repaint the earnings table, the basis line and the benefit panel."""
        person = self.selected_person()
        self._earnings = (
            retirement.list_earnings(self.conn, person["id"]) if person else []
        )
        self._loading = True
        try:
            self.earnings_table.setRowCount(len(self._earnings))
            for row, item in enumerate(self._earnings):
                self._fill_earnings_row(row, item)
            self.earnings_table.resizeColumnsToContents()
            statement = (person or {}).get("ss_statement_date") or ""
            if statement:
                self._set_statement_date(statement)
            else:
                self.statement_date.setDate(self.statement_date.minimumDate())
        finally:
            self._loading = False
        self.basis_label.setText(
            retirement.describe_earnings_basis(self._earnings)
            if person
            else "Add a person to record an earnings history."
        )
        self.earnings_table.setEnabled(person is not None)
        self.refresh_benefits()
        self._sync_buttons()

    def _set_statement_date(self, iso: str):
        # Storage is ISO everywhere (CLAUDE.md); what the editor SHOWS is the
        # user's date preference, which make_date_edit already set.
        parsed = QDate.fromString(iso, "yyyy-MM-dd")
        if parsed.isValid():
            self.statement_date.setDate(parsed)

    def _fill_earnings_row(self, row: int, item: dict):
        year = QTableWidgetItem(str(item["year"]))
        self.earnings_table.setItem(row, 0, year)
        amount = QTableWidgetItem(fmt_cents(item["earnings_cents"]))
        amount.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.earnings_table.setItem(row, 1, amount)

        source = NoWheelComboBox()
        for key in retirement.EARNINGS_SOURCES:
            source.addItem(_SOURCE_LABELS[key], key)
        source.setCurrentIndex(list(retirement.EARNINGS_SOURCES).index(item["source"]))
        source.currentIndexChanged.connect(
            lambda _i, y=int(item["year"]), combo=source: self._write_earnings_source(
                y, combo.currentData()
            )
        )
        self.earnings_table.setCellWidget(row, 2, source)

    def benefit_rows(self) -> list[dict]:
        """What the benefit table shows, as plain data.

        Separate from the painting so the figures can be read (and tested)
        without going through the widget."""
        person = self.selected_person()
        if not person or not person["birth_year"]:
            return []
        birth_year = int(person["birth_year"])
        earnings = retirement.earnings_map(self.conn, person["id"])
        if not earnings:
            return []
        choices = list(retirement.claim_age_choices(birth_year))
        planned = person["planned_claim_age_months"]
        if planned is not None and int(planned) not in [m for _, m in choices]:
            months = int(planned)
            label = f"{months // 12}"
            if months % 12:
                label += f" and {months % 12}mo"
            choices.append((label, months))
            choices.sort(key=lambda c: c[1])
        # No provenance column and no staleness mark: this table used to carry a
        # citation per row plus an amber triangle when a published table was an
        # edition behind. The citation repeated itself on every row and the
        # triangle means missing or conflicting data everywhere else in the app.
        # Both now live once, in the Retirement FAQ.
        rows = []
        for label, months in choices:
            rows.append({
                "label": label,
                "claim_age_months": months,
                "planned": planned is not None and int(planned) == months,
                # "62" pays from 62 and one month unless born on the first.
                "cents": retirement.monthly_benefit(
                    earnings, birth_year, retirement.earliest_claim_months(person, months)),
            })
        return rows

    def refresh_benefits(self):
        """Repaint the benefit table and every assumption standing under it."""
        self._benefits = self.benefit_rows()
        self.benefit_table.setRowCount(len(self._benefits))
        for row, data in enumerate(self._benefits):
            label = data["label"]
            if data["planned"]:
                label += "  (your plan)"
            self.benefit_table.setItem(row, 0, QTableWidgetItem(label))
            amount = QTableWidgetItem(fmt_money(data["cents"]))
            amount.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.benefit_table.setItem(row, 1, amount)
        self.benefit_table.resizeColumnsToContents()
        self.benefit_notes.setText("\n".join(self.assumption_lines()))

    def assumption_lines(self) -> list[str]:
        """Every assumption the benefit column rests on, in printable order.

        A figure whose assumptions are off-screen is a figure the user cannot
        correct, so this runs even when there is nothing to show -- then it
        says what is missing instead."""
        person = self.selected_person()
        if not person:
            return ["Add a person to see a benefit."]
        if not person["birth_year"]:
            return [
                "No benefit yet: Social Security needs a birth year (the month "
                "too, for the exact claim month)."
            ]
        earnings = retirement.earnings_map(self.conn, person["id"])
        if not earnings:
            return [
                "No benefit yet: enter the years from the SSA Earnings Report, "
                "or estimate them from this ledger."
            ]
        birth_year = int(person["birth_year"])
        aime = retirement.average_indexed_monthly_earnings(earnings, birth_year)
        counted = len([c for c in earnings.values() if c])
        lines = [
            "Average indexed monthly earnings: %s, from %d year%s of earnings."
            % (fmt_money(aime), counted, "" if counted == 1 else "s"),
        ]
        if counted < retirement.SS_AIME_YEARS:
            lines.append(
                "Social Security averages the best %d years, so %d zero year%s "
                "are averaged in. Filling those years in raises the figure."
                % (
                    retirement.SS_AIME_YEARS,
                    retirement.SS_AIME_YEARS - counted,
                    "" if retirement.SS_AIME_YEARS - counted == 1 else "s",
                )
            )
        awi = retirement.awi_provenance(birth_year)
        lines.append(
            "Earnings are indexed to %d national-average wages%s."
            % (awi.effective_year, "" if not awi.note else "; " + awi.note)
        )
        turns_62 = birth_year + retirement.SS_ELIGIBILITY_AGE
        dollars = (f"In {turns_62}'s dollars, the year this person turns 62 - wages "
                   f"projected at {retirement.SS_WAGE_GROWTH * 100:.1f}% a year until "
                   "then, no cost-of-living raise before it"
                   if turns_62 > self._today.year else "In today's dollars")
        lines.append(
            f"{dollars}, this person's own record only: the planner adds its "
            "COLA from 62, the spousal benefit where half the other's amount is "
            "more, and the earnings test while a salary runs before full "
            "retirement age. A claim at 62 pays from 62 and one month unless "
            "born on the first."
        )
        lines.append(retirement.describe_earnings_basis(
            retirement.list_earnings(self.conn, person["id"])
        ))
        statement = person.get("ss_statement_date")
        if statement:
            lines.append(
                "The SSA Earnings Report on hand is dated %s." % fmt_date(statement)
            )
        if any(r["source"] == "estimated" for r in self._earnings):
            estimate = retirement.estimate_earnings_from_ledger(self.conn)
            lines.extend(estimate.assumptions())
        # Nothing about the EDITION of a published table belongs here. These are
        # the assumptions the user can correct - an earnings year, a claim age,
        # a statement date. Which year's wage index or bend points produced the
        # figure is in the Retirement FAQ's table.
        return lines

    def _sync_buttons(self):
        person = self.selected_person()
        for b in (self.btn_remove_person,):
            b.setEnabled(person is not None)
        for b in (self.btn_add_year, self.btn_estimate):
            b.setEnabled(person is not None)
        self.btn_remove_year.setEnabled(bool(self._earnings))
        self.statement_date.setEnabled(person is not None)

    # ---- selection -------------------------------------------------------
    def selected_person(self):
        row = self.people_table.currentRow()
        if 0 <= row < len(self._people):
            return self._people[row]
        return None

    def selected_person_id(self):
        person = self.selected_person()
        return int(person["id"]) if person else None

    def select_person(self, person_id: int):
        for row, person in enumerate(self._people):
            if int(person["id"]) == int(person_id):
                self.people_table.selectRow(row)
                return

    def _on_person_selected(self):
        if self._loading:
            return
        self.reload_earnings()

    # ---- people writes ---------------------------------------------------
    def _write_person(self, person_id: int, **fields):
        if self._loading:
            return
        try:
            retirement.update_person(self.conn, person_id, **fields)
        except ValueError as exc:
            # A rejected edit leaves the widget showing something the database
            # does not hold, so the row has to be rebuilt -- but not from
            # inside the editor that is still emitting.
            self.notice.setText(str(exc))
            QTimer.singleShot(0, self.reload)
            return
        self.notice.setText("")
        self._people = retirement.list_people(self.conn)
        if "birth_year" in fields:
            # A birth year decides full retirement age, which is a LABEL in
            # the claim-age combo, so that one cell is rebuilt. Rebuilding the
            # whole row would delete the spin box mid-signal.
            for row, person in enumerate(self._people):
                if int(person["id"]) == int(person_id):
                    self._loading = True
                    try:
                        self._fill_claim_cell(row, person)
                    finally:
                        self._loading = False
                    break
        self._refresh_derived()
        self.changed.emit()

    def _refresh_derived(self):
        """Re-read what an in-place edit changed, touching no cell widget.

        Everything below the tables is derived: the basis line, the benefit
        column and its assumptions. Refreshing them costs nothing and keeps a
        figure from outliving the edit that invalidated it."""
        person = self.selected_person()
        self._earnings = (
            retirement.list_earnings(self.conn, person["id"]) if person else []
        )
        self.basis_label.setText(
            retirement.describe_earnings_basis(self._earnings)
            if person
            else "Add a person to record an earnings history."
        )
        self.refresh_benefits()
        self._sync_buttons()

    def _on_person_item_changed(self, item):
        if self._loading:
            return
        row = item.row()
        if not (0 <= row < len(self._people)):
            return
        person_id = int(self._people[row]["id"])
        if item.column() == 0:
            self._write_person(person_id, name=item.text())
        elif item.column() == 4:
            self._write_person(
                person_id, born_on_the_first=item.checkState() == Qt.Checked
            )
        elif item.column() == 6:
            self._write_person(person_id, part_d=1 if item.checkState() == Qt.Checked else 0)

    def on_add_person(self):
        """Add a household member with a placeholder name, then select the row.

        No dialog: an empty row the user types over is one fewer window, and
        the people table IS the editor."""
        existing = {str(p["name"]) for p in self._people}
        name = "New person"
        n = 2
        while name in existing:
            name, n = f"New person {n}", n + 1
        relationship = "self" if not self._people else "spouse"
        person_id = retirement.add_person(self.conn, name, relationship)
        self.reload()
        self.select_person(person_id)
        self.notice.setText(
            "Type the name, birth month and birth year for the new person."
        )
        self.changed.emit()
        return person_id

    def on_remove_person(self):
        person = self.selected_person()
        if not person:
            return
        if not self._confirm(
            "Remove Person",
            "Remove %s and every earnings year recorded for them?"
            % (person["name"] or "this person"),
        ):
            return
        retirement.delete_person(self.conn, person["id"])
        self.reload()
        self.changed.emit()

    def _on_statement_date_changed(self, *_):
        if self._loading:
            return
        person = self.selected_person()
        if not person:
            return
        iso = date_edit_iso(self.statement_date)
        retirement.update_person(
            self.conn, person["id"], ss_statement_date=iso or None
        )
        self._people = retirement.list_people(self.conn)
        self.refresh_benefits()
        self.changed.emit()

    # ---- earnings writes -------------------------------------------------
    def _on_earnings_item_changed(self, item):
        if self._loading:
            return
        person = self.selected_person()
        row = item.row()
        if not person or not (0 <= row < len(self._earnings)):
            return
        current = dict(self._earnings[row])
        if item.column() == 0:
            self._rename_year(person, current, item.text())
        elif item.column() == 1:
            cents = abs(parse_amount(item.text()))
            # A figure the user types is his own, so an ESTIMATED year becomes
            # reported -- otherwise the next "Estimate from Ledger" would throw
            # the correction away. A projected or already-reported year keeps
            # the source it has.
            source = current["source"]
            if source == "estimated":
                source = "reported"
            retirement.set_earnings(
                self.conn, person["id"], int(current["year"]), cents, source
            )
            self.notice.setText("")
            self._loading = True
            try:
                item.setText(fmt_cents(cents))
                combo = self.earnings_table.cellWidget(row, 2)
                if combo is not None:
                    combo.setCurrentIndex(
                        list(retirement.EARNINGS_SOURCES).index(source)
                    )
            finally:
                self._loading = False
            self._refresh_derived()
            self.changed.emit()

    def _rename_year(self, person, current, text):
        # A year moves the row's place in the table, so this is one of the two
        # edits that really does rebuild -- after the editor has finished.
        try:
            year = int(str(text).strip())
        except ValueError:
            self.notice.setText("A year is four digits, like 2019.")
            QTimer.singleShot(0, self.reload_earnings)
            return
        if year == int(current["year"]):
            return
        if any(int(r["year"]) == year for r in self._earnings):
            self.notice.setText("%d is already in the list." % year)
            QTimer.singleShot(0, self.reload_earnings)
            return
        retirement.delete_earnings(self.conn, person["id"], int(current["year"]))
        retirement.set_earnings(
            self.conn, person["id"], year,
            int(current["earnings_cents"]), current["source"],
        )
        self.notice.setText("")
        QTimer.singleShot(0, self.reload_earnings)
        self._refresh_derived()
        self.changed.emit()

    def _write_earnings_source(self, year: int, source: str):
        if self._loading:
            return
        person = self.selected_person()
        if not person:
            return
        existing = next(
            (r for r in self._earnings if int(r["year"]) == int(year)), None
        )
        if existing is None or existing["source"] == source:
            return
        retirement.set_earnings(
            self.conn, person["id"], int(year),
            int(existing["earnings_cents"]), source,
        )
        # No rebuild: the combo that called this is still emitting, and only
        # the basis line below the table depends on the answer.
        self._refresh_derived()
        self.changed.emit()

    def on_add_year(self, year=None, cents: int = 0, source: str = "reported"):
        """Add one earnings year. Defaults to the year after the last one held.

        Takes its values as arguments so the planner page (and a test) can add
        a year without driving the table."""
        person = self.selected_person()
        if not person:
            return None
        if year is None:
            years = [int(r["year"]) for r in self._earnings]
            year = max(years) + 1 if years else self._today.year - 1
            while any(int(r["year"]) == year for r in self._earnings):
                year += 1
        retirement.set_earnings(
            self.conn, person["id"], int(year), int(cents), source
        )
        self.notice.setText("")
        self.reload_earnings()
        self.changed.emit()
        return int(year)

    def on_remove_year(self):
        person = self.selected_person()
        row = self.earnings_table.currentRow()
        if not person or not (0 <= row < len(self._earnings)):
            return
        year = int(self._earnings[row]["year"])
        if not self._confirm("Remove Year", "Remove the earnings for %d?" % year):
            return
        retirement.delete_earnings(self.conn, person["id"], year)
        self.reload_earnings()
        self.changed.emit()

    def on_estimate(self):
        """Fill the estimated years from this ledger's wage categories.

        Reports through the notice label rather than a message box: the result
        is a sentence, not a decision, and a modal here would block headless."""
        person = self.selected_person()
        if not person:
            return None
        estimate = retirement.estimate_earnings_from_ledger(self.conn)
        if not estimate.rows:
            self.notice.setText(" ".join(estimate.assumptions()))
            self.earnings_notes.setText("")
            return estimate
        retirement.replace_earnings(
            self.conn, person["id"], estimate.rows, "estimated"
        )
        self.reload_earnings()
        kept = [
            y for y in estimate.rows
            if not any(
                int(r["year"]) == int(y) and r["source"] == "estimated"
                for r in self._earnings
            )
        ]
        note = "Estimated %d year%s from this ledger." % (
            len(estimate.rows) - len(kept),
            "" if len(estimate.rows) - len(kept) == 1 else "s",
        )
        if kept:
            note += (
                " %d year%s left as you entered them."
                % (len(kept), "" if len(kept) == 1 else "s")
            )
        self.notice.setText(note)
        self.earnings_notes.setText("\n".join(estimate.assumptions()))
        self.changed.emit()
        return estimate

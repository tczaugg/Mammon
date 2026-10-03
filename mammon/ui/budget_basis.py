"""mammon.ui.budget_basis -- the subtraction table that crosses the seam between
the Budget Planner and the Retirement Planner (SRD 5.12i).

ONE figure crosses: an annual spending LEVEL in base-year cents. This dialog
exists so that figure is never handed over silently. It shows the arithmetic -
the window's whole plan, every candidate line with its cents and the evidence
that picked it, and the remainder - and it lets the user clear or confirm each
line before anything is used. Nothing is applied here; the caller gets a figure
and the planner's own Apply is still what writes a plan.

Three shapes in here are load-bearing:

* **It re-derives no money.** Every toggle and every change of the retirement
  year calls :func:`mammon.reports.budget.retirement_spending_basis` again with
  the confirmed keys, so the subtraction exists in exactly one place. The table
  is a rendering of what that function returned, including its provenance note.
* **It imports nothing from :mod:`mammon.retirement`.** The budget side holds no
  figure that moves with law or annual indexing, so the one-file rule stays
  intact; the retirement year arrives as a plain argument from whoever opened
  the dialog, and is evidence (what a loan's payoff year is compared against),
  not a setting this dialog owns. Opened from the budget page, where no plan is
  in view, no year is passed: the report resolves the household's own stated one
  (:mod:`mammon.retirement` owns it) and reports back which year it used, and
  this dialog puts that year in the box so the comparison behind a subtracted
  loan is visible. With nothing stated anywhere the year stays unset, and the
  loan lines say why they were listed rather than subtracted.
* **It never calls ``exec_()`` on itself.** Under the offscreen platform an
  ``exec_()`` never returns, so running the dialog belongs to the caller's one
  overridable ``_run_dialog`` seam, which a headless test replaces.

It also states facts and cites them, and recommends nothing: no row here says a
figure is right, only what it is and where it came from.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox,
                             QHBoxLayout, QLabel, QSpinBox, QTableWidget,
                             QTableWidgetItem, QVBoxLayout)

from mammon.reports import budget as budget_report
from mammon.ui.models import fmt_cents
from mammon.ui.delegates import NoWheelSpinBox

#: The spin box's minimum doubles as "no year given", which is how the budget
#: page opens the dialog: it does not know the household's plan and must not
#: guess one. Plain widget bounds - neither is a financial figure.
_YEAR_UNSET = 1900
_YEAR_LAST = 2200


class BudgetBasisDialog(QDialog):
    """The subtraction table, and the figure it arrives at.

    ``basis`` holds the last :class:`~mammon.reports.budget.RetirementSpendingBasis`
    computed - the same object whether the dialog was accepted or not - and
    :meth:`accepted_basis` is what a caller reads after the dialog closed with
    ``Accepted``.
    """

    LINE, AMOUNT, REASON = range(3)
    HEADERS = ("Line", "Yearly amount", "Where it came from")

    def __init__(self, conn, *, budget_id: Optional[int] = None,
                 as_of: Optional[str] = None,
                 retirement_year: Optional[int] = None,
                 ok_text: str = "Use this figure", parent=None):
        super().__init__(parent)
        self.conn = conn
        self._budget_id = budget_id
        self._as_of = as_of
        self._ok_text = ok_text
        #: ``None`` until the user touches a checkbox, so that changing the
        #: retirement year still moves the defaults; a set afterwards, meaning
        #: "subtract exactly these".
        self._confirmed: Optional[set] = None
        self._filling = False
        self.basis = None
        self.setWindowTitle("Spending basis from the budget")
        self.resize(760, 460)
        self._build()
        if retirement_year:
            self.year.setValue(int(retirement_year))
        self.reload()

    # -- construction -------------------------------------------------------
    def _build(self) -> None:
        box = QVBoxLayout(self)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(6)

        self.header = QLabel("")
        self.header.setWordWrap(True)
        box.addWidget(self.header)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Retirement year"))
        self.year = NoWheelSpinBox(self)
        self.year.setRange(_YEAR_UNSET, _YEAR_LAST)
        self.year.setSpecialValueText("not set")
        self.year.setToolTip(
            "What a loan's payoff year is compared against. Left unset, a loan "
            "payment is listed but not subtracted, because whether it is still "
            "being paid in retirement is unknown here.")
        bar.addWidget(self.year)
        bar.addStretch(1)
        box.addLayout(bar)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(list(self.HEADERS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        box.addWidget(self.table, 1)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        box.addWidget(self.note)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        box.addWidget(self.status)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok
                                        | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText(self._ok_text)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        box.addWidget(self.buttons)

        self.table.itemChanged.connect(self._on_toggle)
        self.year.valueChanged.connect(self._on_year)

    # -- the one computation ------------------------------------------------
    def retirement_year(self) -> Optional[int]:
        """The year the loan lines are compared against, or ``None`` if unset."""
        value = int(self.year.value())
        return None if value == _YEAR_UNSET else value

    def reload(self) -> None:
        """Ask the report again and render what it returned. Every figure on
        screen comes from that one call - this method computes no money."""
        basis = budget_report.retirement_spending_basis(
            self.conn, self._budget_id, self._as_of,
            retirement_year=self.retirement_year(),
            exclude_keys=self._confirmed)
        self.basis = basis
        if self.retirement_year() is None and basis.retirement_year:
            # The report resolved the household's own stated year. Show it: a
            # debt line was subtracted on the strength of that comparison, and a
            # figure the arithmetic used must not be invisible to the user about
            # to accept it. No recompute - the basis on screen already used it.
            self._filling = True
            try:
                self.year.setValue(int(basis.retirement_year))
            finally:
                self._filling = False
        rows = list(basis.excluded) + list(basis.offered)
        rows.sort(key=lambda r: (-r.cents, r.category_name.lower()))
        applied = {line.key for line in basis.excluded}

        self._filling = True
        try:
            self.table.setRowCount(len(rows) + 2)
            where = ("the budget plan" if basis.source == "budget"
                     else "measured spending")
            self._plain_row(0, f"Total, {where}", basis.total_cents,
                            f"{basis.months_observed} month"
                            + ("" if basis.months_observed == 1 else "s")
                            + (f" ending {basis.end_period}"
                               if basis.end_period else ""))
            for offset, line in enumerate(rows, start=1):
                item = QTableWidgetItem(line.category_name)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                              | Qt.ItemIsUserCheckable)
                item.setData(Qt.UserRole, line.key)
                subtracted = line.key in applied
                item.setCheckState(Qt.Checked if subtracted else Qt.Unchecked)
                self.table.setItem(offset, self.LINE, item)
                shown = -line.cents if subtracted else line.cents
                amount = QTableWidgetItem(fmt_cents(shown))
                amount.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                amount.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.table.setItem(offset, self.AMOUNT, amount)
                self.table.setItem(offset, self.REASON,
                                   self._read_only(line.reason))
            self._plain_row(len(rows) + 1, "Spending basis, a year",
                            basis.annual_cents,
                            f"Base-year {basis.basis_year} dollars, not "
                            f"inflated. The planner applies its own increase "
                            f"rate.")
            self.table.resizeColumnsToContents()
        finally:
            self._filling = False

        self.header.setText(
            "Checked lines are subtracted. Clearing one puts it back in the "
            "basis; every line says what evidence picked it.")
        self.note.setText(basis.note)
        self.status.setText(self._status_text(basis))
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(
            basis.annual_cents > 0)

    def _status_text(self, basis) -> str:
        window = len(basis.periods)
        parts = []
        if basis.annual_cents <= 0:
            parts.append("Nothing is left to hand over: the exclusions account "
                         "for the whole window.")
        if basis.months_observed and basis.months_observed < window:
            parts.append(f"Only {basis.months_observed} of {window} months "
                         f"carry anything, so the remainder was scaled up to a "
                         f"year.")
        if basis.source == "budget" and basis.coverage_pct < 100:
            parts.append(f"The plan covers {basis.coverage_pct} percent of the "
                         f"categories money actually went to, so it may "
                         f"understate what the household spends.")
        if basis.source != "budget":
            parts.append("No budget plan covered this window, so the figure is "
                         "twelve months of measured spending.")
        if self.retirement_year() is None:
            parts.append("With no retirement year set, a loan payment is "
                         "listed but not subtracted.")
        return " ".join(parts)

    # -- rendering helpers ---------------------------------------------------
    def _plain_row(self, row: int, label: str, cents: int, reason: str) -> None:
        self.table.setItem(row, self.LINE, self._read_only(label))
        amount = QTableWidgetItem(fmt_cents(cents))
        amount.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        amount.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.table.setItem(row, self.AMOUNT, amount)
        self.table.setItem(row, self.REASON, self._read_only(reason))

    @staticmethod
    def _read_only(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text or "")
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        return item

    # -- signals -------------------------------------------------------------
    def _on_toggle(self, item) -> None:
        if self._filling or item.column() != self.LINE:
            return
        if item.data(Qt.UserRole) is None:      # a total or the basis row
            return
        self._confirmed = self._checked_keys()
        self.reload()

    def _on_year(self, _value) -> None:
        if self._filling:
            return
        self.reload()

    def _checked_keys(self) -> set:
        keys = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, self.LINE)
            if item is None:
                continue
            key = item.data(Qt.UserRole)
            if key is not None and item.checkState() == Qt.Checked:
                keys.add(str(key))
        return keys

    # -- what the caller reads ----------------------------------------------
    def accepted_basis(self):
        """The basis as shown when the dialog closed. A COPY of a figure, not a
        link: nothing keeps it in step with a later edit to the budget."""
        return self.basis

    def set_confirmed(self, keys) -> None:
        """Confirm exactly ``keys`` (a test seam and a caller convenience), then
        re-derive."""
        self._confirmed = {str(k) for k in keys}
        self.reload()

"""The Budgets panel: a thin projection over :mod:`mammon.budgets` (schema v39).

Rationale / invariants this file must keep holding:

- **No SQL and no money logic here.** Every read and write goes through
  ``mammon.budgets`` (which is itself the sole budget writer and commits on its
  own). This widget only projects the domain layer onto Qt, exactly like every
  other ``mammon.ui`` surface. Amounts stay **signed integer cents** end to end:
  cells render with :func:`mammon.ui.models.fmt_cents` and typed text is turned
  back into cents by :func:`mammon.ui.models.parse_amount` -- never
  ``float(...)`` and never a hand-rolled parse.
- **Actuals are never edited.** ``budget_vs_actual`` derives them read-only from
  the ledger; only the *Budgeted* column is editable, and editing it calls
  ``budgets.set_line`` for the selected ``(category, period)``.
- **Offscreen-safe.** Nothing here ``exec_()``-s a blocking modal on its own:
  the delete confirmation goes through :func:`QMessageBox.question` (the seam the
  tests patch) and the new-budget name prompt goes through the overridable
  :meth:`BudgetWidget._ask_budget_name` seam. The dialog itself is only
  ``exec_()``-ed by its opener (the Tools menu), never in a test.
- The month/period control is built with :func:`mammon.ui.delegates.make_date_edit`
  and read back with :func:`~mammon.ui.delegates.date_edit_iso`, so the display
  format follows the one date-format preference rather than being hardcoded; the
  period is that ISO date truncated to its ``'YYYY-MM'`` month.

Editing writes back through the model's :meth:`~BudgetLinesModel.setData` with a
targeted ``dataChanged`` (not a ``beginResetModel``) so a commit arriving while
the register-style :class:`~mammon.ui.delegates.MoneyDelegate` editor is being
torn down never resets the view out from under it.
"""
from __future__ import annotations

from dataclasses import replace

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt, QDate
from PyQt5.QtGui import QBrush, QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
)

from mammon import budgets, ledger
from mammon.ui import style
from mammon.ui.delegates import MoneyDelegate, date_edit_iso, make_date_edit
from mammon.ui.models import fmt_cents, fmt_money, parse_amount


class BudgetLinesModel(QAbstractTableModel):
    """One row per category for a ``(budget, period)``: the editable *Budgeted*
    target beside the read-only *Carried*, *Actual* and *Remaining* derived by
    :func:`mammon.budgets.budget_vs_actual`. *Carried* is the rollover remainder
    brought in from prior periods (0 unless the line rolls over); *Remaining* is
    ``budgeted + carried - actual``. Money renders with ``fmt_cents`` and parses
    back through ``parse_amount``, matching the register; the widget itself does
    no money arithmetic beyond re-adding the domain layer's own cents."""

    CATEGORY, BUDGETED, CARRIED, ACTUAL, REMAINING = range(5)
    HEADERS = ["Category", "Budgeted", "Carried", "Actual", "Remaining"]

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.budget_id = None
        self.period = ""
        self._rows: list[budgets.BudgetActualRow] = []

    # -- data loading ------------------------------------------------------
    def configure(self, budget_id, period):
        """Point the model at a ``(budget, period)`` and refetch."""
        self.budget_id = budget_id
        self.period = period or ""
        self.reload()

    def reload(self):
        self.beginResetModel()
        if self.budget_id is None or not self.period:
            self._rows = []
        else:
            self._rows = budgets.budget_vs_actual(self.conn, self.budget_id, self.period)
        self.endResetModel()

    def row_at(self, row: int) -> budgets.BudgetActualRow:
        return self._rows[row]

    def totals(self) -> tuple[int, int, int, int]:
        budgeted = sum(r.budgeted_cents for r in self._rows)
        carried = sum(r.carried_in_cents for r in self._rows)
        actual = sum(r.actual_cents for r in self._rows)
        return budgeted, carried, actual, budgeted + carried - actual

    # -- Qt model surface --------------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        f = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if index.column() == self.BUDGETED and self.budget_id is not None:
            f |= Qt.ItemIsEditable
        return f

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r = self._rows[index.row()]
        col = index.column()
        if role in (Qt.DisplayRole, Qt.EditRole):
            if col == self.CATEGORY:
                return r.category_name
            if col == self.BUDGETED:
                return fmt_cents(r.budgeted_cents)
            if col == self.CARRIED:
                return fmt_cents(r.carried_in_cents)
            if col == self.ACTUAL:
                return fmt_cents(r.actual_cents)
            if col == self.REMAINING:
                return fmt_cents(r.remaining_cents)
        if role == Qt.TextAlignmentRole and col in (
            self.BUDGETED, self.CARRIED, self.ACTUAL, self.REMAINING
        ):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if col == self.REMAINING and r.remaining_cents < 0:
                return QBrush(QColor(style.negative_color()))
            if col == self.CARRIED and r.carried_in_cents < 0:
                return QBrush(QColor(style.negative_color()))
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid():
            return False
        if index.column() != self.BUDGETED or self.budget_id is None:
            return False
        r = self._rows[index.row()]
        cents = parse_amount(value)
        budgets.set_line(self.conn, self.budget_id, r.category_id, self.period, cents)
        # Update in place + targeted dataChanged rather than a full reset: a
        # reset while the MoneyDelegate editor is being destroyed would yank the
        # view out from under Qt's teardown. Carried-in is unchanged by editing
        # the target, so remaining just re-adds it to the new budgeted amount.
        self._rows[index.row()] = replace(
            r, budgeted_cents=cents,
            remaining_cents=cents + r.carried_in_cents - r.actual_cents,
        )
        self.dataChanged.emit(
            self.index(index.row(), self.BUDGETED),
            self.index(index.row(), self.REMAINING),
        )
        return True


class BudgetWidget(QDialog):
    """Choose or create a budget, edit its per-category monthly targets, and see
    budgeted / actual / remaining for a selected month. All state lives in
    :mod:`mammon.budgets`; this is a projection only."""

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Budgets")

        outer = QVBoxLayout(self)

        # -- budget selector row ------------------------------------------
        sel = QHBoxLayout()
        sel.addWidget(QLabel("Budget:"))
        self.budget_combo = QComboBox()
        sel.addWidget(self.budget_combo, 1)
        self.active_check = QCheckBox("Active")
        sel.addWidget(self.active_check)
        self.new_btn = QPushButton("New…")
        sel.addWidget(self.new_btn)
        self.delete_btn = QPushButton("Delete")
        sel.addWidget(self.delete_btn)
        outer.addLayout(sel)

        # -- period row ----------------------------------------------------
        per = QHBoxLayout()
        per.addWidget(QLabel("Month:"))
        self.period_edit = make_date_edit(self)
        per.addWidget(self.period_edit)
        self.period_label = QLabel("")
        per.addWidget(self.period_label)
        per.addStretch(1)
        outer.addLayout(per)

        # -- the per-category table ---------------------------------------
        self.model = BudgetLinesModel(conn, self)
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.verticalHeader().setVisible(False)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.setItemDelegateForColumn(
            BudgetLinesModel.BUDGETED, MoneyDelegate(self.view)
        )
        outer.addWidget(self.view, 1)

        # -- add-category row ---------------------------------------------
        add = QHBoxLayout()
        add.addWidget(QLabel("Add category:"))
        self.cat_combo = QComboBox()
        for c in ledger.list_categories(conn):
            self.cat_combo.addItem(c["path"], c["id"])
        add.addWidget(self.cat_combo, 1)
        self.add_btn = QPushButton("Add")
        add.addWidget(self.add_btn)
        outer.addLayout(add)

        # -- summary + close ----------------------------------------------
        foot = QHBoxLayout()
        self.summary_label = QLabel("")
        foot.addWidget(self.summary_label, 1)
        self.close_btn = QPushButton("Close")
        foot.addWidget(self.close_btn)
        outer.addLayout(foot)

        # Wire signals after the whole UI exists so no slot fires against a
        # half-built widget.
        self.budget_combo.currentIndexChanged.connect(self._on_budget_changed)
        self.active_check.toggled.connect(self._on_active_toggled)
        self.new_btn.clicked.connect(self._on_new)
        self.delete_btn.clicked.connect(self._on_delete)
        self.period_edit.dateChanged.connect(self._on_period_changed)
        self.add_btn.clicked.connect(self._on_add)
        self.close_btn.clicked.connect(self.accept)
        self.model.modelReset.connect(self._update_summary)
        self.model.dataChanged.connect(lambda *a: self._update_summary())

        self._reload_budgets()

    # -- public-ish accessors (used by callers and tests) -----------------
    def current_budget_id(self):
        return self.budget_combo.currentData()

    def period(self) -> str:
        return date_edit_iso(self.period_edit)[:7]

    def set_period(self, period: str):
        """Point the panel at an ISO ``'YYYY-MM'`` month."""
        year, month = int(period[:4]), int(period[5:7])
        self.period_edit.blockSignals(True)
        self.period_edit.setDate(QDate(year, month, 1))
        self.period_edit.blockSignals(False)
        self.refresh()

    def add_category(self, category_id):
        """Add a zero-target line for ``category_id`` in the current period so it
        shows up in the table ready to be edited."""
        bid = self.current_budget_id()
        if bid is None or category_id is None:
            return
        budgets.set_line(self.conn, bid, int(category_id), self.period(), 0)
        self.refresh()

    def refresh(self):
        self.model.configure(self.current_budget_id(), self.period())
        self.period_label.setText(f"({self.period()})")
        self.view.resizeColumnsToContents()
        self._update_summary()

    # -- seams ------------------------------------------------------------
    def _ask_budget_name(self):
        """Prompt for a new budget name. Overridable seam so tests never hit a
        blocking modal under the offscreen platform."""
        text, ok = QInputDialog.getText(self, "New Budget", "Budget name:")
        text = (text or "").strip()
        return text if ok and text else None

    # -- slots ------------------------------------------------------------
    def _reload_budgets(self, select_id=None):
        self.budget_combo.blockSignals(True)
        self.budget_combo.clear()
        for b in budgets.list_budgets(self.conn):
            self.budget_combo.addItem(b.name, b.id)
        if select_id is not None:
            idx = self.budget_combo.findData(select_id)
            if idx >= 0:
                self.budget_combo.setCurrentIndex(idx)
        self.budget_combo.blockSignals(False)
        self._on_budget_changed()

    def _on_budget_changed(self, *args):
        bid = self.current_budget_id()
        self.delete_btn.setEnabled(bid is not None)
        self.add_btn.setEnabled(bid is not None)
        self._sync_active_check(bid)
        self.refresh()

    def _sync_active_check(self, bid):
        self.active_check.blockSignals(True)
        if bid is None:
            self.active_check.setChecked(False)
            self.active_check.setEnabled(False)
        else:
            b = budgets.get_budget(self.conn, bid)
            self.active_check.setEnabled(True)
            self.active_check.setChecked(bool(b and b.active))
        self.active_check.blockSignals(False)

    def _on_active_toggled(self, checked):
        bid = self.current_budget_id()
        if bid is None:
            return
        budgets.set_active(self.conn, bid, bool(checked))

    def _on_new(self):
        name = self._ask_budget_name()
        if not name:
            return
        bid = budgets.create_budget(self.conn, name)
        self._reload_budgets(select_id=bid)

    def _on_delete(self):
        bid = self.current_budget_id()
        if bid is None:
            return
        b = budgets.get_budget(self.conn, bid)
        name = b.name if b else ""
        if QMessageBox.question(
            self,
            "Delete Budget",
            f"Delete budget '{name}' and all its lines?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        budgets.delete_budget(self.conn, bid)
        self._reload_budgets()

    def _on_period_changed(self, *args):
        self.refresh()

    def _on_add(self):
        self.add_category(self.cat_combo.currentData())

    def _update_summary(self):
        budgeted, carried, actual, remaining = self.model.totals()
        parts = [f"Budgeted {fmt_money(budgeted)}"]
        if carried:
            parts.append(f"Carried {fmt_money(carried)}")
        parts.append(f"Actual {fmt_money(actual)}")
        parts.append(f"Remaining {fmt_money(remaining)}")
        self.summary_label.setText("    ".join(parts))

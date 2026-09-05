"""Column-mapping wizard for delimited (CSV-ish) imports.

:mod:`mammon.importers.tabular` can already infer a delimited file's column map,
re-interpret it from a user's answers (:func:`~mammon.importers.tabular.apply_wizard_answers`)
and remember the result as a profile. What it had no way to do was ASK. This
dialog is that missing surface.

Two things it deliberately shows side by side:

* **Source (as read)** -- the raw grid exactly as the structure detector framed
  it, with the file's OWN header names. This is the ground truth.
* **Result** -- the parsed rows, whose column titles read ``Role  <-  source
  column``.

Showing only the parsed result (what the old preview did) hides the failure that
actually matters: a mapping that picked the WRONG column still produces
plausible-looking output. A running-balance column parses as money exactly like
an amount column does, so "1,204.55" in a Amount preview tells the reader
nothing. Naming the source column each role was taken FROM is what makes a
mix-up visible.

Every change re-interprets the whole file through the real importer, so the
preview is never a mock-up of what import would do -- it IS what import would do.
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QRadioButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from mammon.importers import tabular

_ACCOUNT_TYPES = ("checking", "savings", "credit", "cash",
                  "investment", "asset", "liability")

_NONE = ""          # combo sentinel for "no column chosen"
_PREVIEW_ROWS = 8


def _fill(combo: QComboBox, header, current, *, allow_none=True) -> None:
    """(Re)populate a column combo, restoring ``current`` when it still exists."""
    combo.blockSignals(True)
    combo.clear()
    if allow_none:
        combo.addItem("— none —", _NONE)
    for h in header:
        if h:
            combo.addItem(h, h)
    idx = combo.findData(current if current else _NONE)
    combo.setCurrentIndex(idx if idx >= 0 else 0)
    combo.blockSignals(False)


class ImportMappingDialog(QDialog):
    """Edit a :class:`~mammon.importers.tabular.TabularPlan`'s column map.

    Construct with the plan to revise; on accept, :attr:`plan` holds the final
    (re-interpreted) plan, ready for
    :func:`~mammon.importers.tabular.accept_profile`.
    """

    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("Map import columns")
        self.resize(980, 620)

        root = QVBoxLayout(self)
        root.addWidget(QLabel(self._structure_text()))

        body = QHBoxLayout()
        body.addWidget(self._build_controls(), 0)
        body.addWidget(self._build_previews(), 1)
        root.addLayout(body)

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #a33; font-weight: bold;")
        root.addWidget(self.warning)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Save mapping")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._load_from_plan()
        self._replan()

    # -- construction --------------------------------------------------------
    def _structure_text(self) -> str:
        p = self.plan
        bits = [f"{len(p.header)} columns"]
        if p.preamble_rows:
            bits.append(f"{p.preamble_rows} preamble row(s) skipped")
        if p.footer_rows:
            bits.append(f"{p.footer_rows} footer row(s) skipped")
        if p.dropped_blank:
            bits.append(f"{p.dropped_blank} blank row(s) dropped")
        return "Detected structure: " + ", ".join(bits)

    def _build_controls(self) -> QWidget:
        wrap = QWidget()
        box = QVBoxLayout(wrap)
        box.setContentsMargins(0, 0, 0, 0)

        # ---- date
        g_date = QGroupBox("Date")
        f = QFormLayout(g_date)
        self.date_combo = QComboBox()
        self.date_combo.currentIndexChanged.connect(self._replan)
        f.addRow("Column", self.date_combo)
        box.addWidget(g_date)

        # ---- amount
        g_amt = QGroupBox("Amount")
        f = QFormLayout(g_amt)
        self.amt_signed = QRadioButton("One signed column")
        self.amt_pair = QRadioButton("Separate debit / credit columns")
        self.amt_group = QButtonGroup(self)
        self.amt_group.addButton(self.amt_signed)
        self.amt_group.addButton(self.amt_pair)
        self.amount_combo = QComboBox()
        self.debit_combo = QComboBox()
        self.credit_combo = QComboBox()
        self.invert = QCheckBox("Invert sign (file lists spending as positive)")
        self.debit_positive = QCheckBox("Debit column is already positive")
        f.addRow(self.amt_signed)
        f.addRow("Amount", self.amount_combo)
        f.addRow(self.amt_pair)
        f.addRow("Debit", self.debit_combo)
        f.addRow("Credit", self.credit_combo)
        f.addRow(self.invert)
        f.addRow(self.debit_positive)
        for w in (self.amount_combo, self.debit_combo, self.credit_combo):
            w.currentIndexChanged.connect(self._replan)
        for w in (self.amt_signed, self.amt_pair):
            w.toggled.connect(self._amount_mode_changed)
        for w in (self.invert, self.debit_positive):
            w.toggled.connect(self._replan)
        box.addWidget(g_amt)

        # ---- payee
        g_payee = QGroupBox("Payee")
        f = QFormLayout(g_payee)
        self.payee_col_radio = QRadioButton("One column")
        self.payee_pair_radio = QRadioButton("From / To pair (direction decides)")
        self.payee_desc_radio = QRadioButton("None — take it from the description")
        self.payee_group = QButtonGroup(self)
        for w in (self.payee_col_radio, self.payee_pair_radio, self.payee_desc_radio):
            self.payee_group.addButton(w)
            w.toggled.connect(self._payee_mode_changed)
        self.payee_combo = QComboBox()
        self.from_combo = QComboBox()
        self.to_combo = QComboBox()
        for w in (self.payee_combo, self.from_combo, self.to_combo):
            w.currentIndexChanged.connect(self._replan)
        f.addRow(self.payee_col_radio)
        f.addRow("Payee", self.payee_combo)
        f.addRow(self.payee_pair_radio)
        f.addRow("From", self.from_combo)
        f.addRow("To", self.to_combo)
        f.addRow(self.payee_desc_radio)
        box.addWidget(g_payee)

        # ---- description
        g_desc = QGroupBox("Description (joined into the memo)")
        v = QVBoxLayout(g_desc)
        self.desc_list = QListWidget()
        self.desc_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.desc_list.setMaximumHeight(110)
        self.desc_list.itemChanged.connect(self._replan)
        v.addWidget(self.desc_list)
        box.addWidget(g_desc)

        # ---- account type
        g_type = QGroupBox("Account type")
        f = QFormLayout(g_type)
        self.type_combo = QComboBox()
        for t in _ACCOUNT_TYPES:
            self.type_combo.addItem(t, t)
        self.type_combo.currentIndexChanged.connect(self._replan)
        f.addRow("Type", self.type_combo)
        box.addWidget(g_type)

        box.addStretch(1)
        return wrap

    def _build_previews(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)

        v.addWidget(QLabel("<b>Source (as read)</b> — the file's own headers"))
        self.source_table = QTableWidget()
        self.source_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.source_table.setAlternatingRowColors(True)
        v.addWidget(self.source_table, 1)

        v.addWidget(QLabel(
            "<b>Result</b> — each column titled <i>Role &lt;- source column</i>"))
        self.result_table = QTableWidget()
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.result_table.setAlternatingRowColors(True)
        v.addWidget(self.result_table, 1)

        self.count_label = QLabel("")
        v.addWidget(self.count_label)
        return wrap

    # -- state <-> widgets ---------------------------------------------------
    def _load_from_plan(self) -> None:
        r = self.plan.roles
        header = self.plan.header

        _fill(self.date_combo, header, r.date)
        _fill(self.amount_combo, header, r.amount)
        _fill(self.debit_combo, header, r.debit)
        _fill(self.credit_combo, header, r.credit)
        _fill(self.payee_combo, header, r.payee)
        _fill(self.from_combo, header, r.payee_from)
        _fill(self.to_combo, header, r.payee_to)

        for w in (self.amt_signed, self.amt_pair, self.payee_col_radio,
                  self.payee_pair_radio, self.payee_desc_radio):
            w.blockSignals(True)
        if r.debit or r.credit:
            self.amt_pair.setChecked(True)
        else:
            self.amt_signed.setChecked(True)
        if r.payee_from or r.payee_to:
            self.payee_pair_radio.setChecked(True)
        elif r.payee:
            self.payee_col_radio.setChecked(True)
        else:
            self.payee_desc_radio.setChecked(True)
        for w in (self.amt_signed, self.amt_pair, self.payee_col_radio,
                  self.payee_pair_radio, self.payee_desc_radio):
            w.blockSignals(False)

        self.invert.blockSignals(True)
        self.invert.setChecked(bool(getattr(r, "invert_amount", False)))
        self.invert.blockSignals(False)
        self.debit_positive.blockSignals(True)
        self.debit_positive.setChecked(bool(getattr(r, "debit_positive", False)))
        self.debit_positive.blockSignals(False)

        self.desc_list.blockSignals(True)
        self.desc_list.clear()
        for h in header:
            if not h:
                continue
            it = QListWidgetItem(h)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if h in r.description else Qt.Unchecked)
            self.desc_list.addItem(it)
        self.desc_list.blockSignals(False)

        i = self.type_combo.findData(self.plan.account_type)
        self.type_combo.blockSignals(True)
        self.type_combo.setCurrentIndex(i if i >= 0 else 0)
        self.type_combo.blockSignals(False)

        self._sync_enabled()

    def _sync_enabled(self) -> None:
        signed = self.amt_signed.isChecked()
        self.amount_combo.setEnabled(signed)
        self.debit_combo.setEnabled(not signed)
        self.credit_combo.setEnabled(not signed)
        self.debit_positive.setEnabled(not signed)
        self.payee_combo.setEnabled(self.payee_col_radio.isChecked())
        pair = self.payee_pair_radio.isChecked()
        self.from_combo.setEnabled(pair)
        self.to_combo.setEnabled(pair)

    def _amount_mode_changed(self, on) -> None:
        if not on:
            return          # only react to the newly-checked button
        self._sync_enabled()
        self._replan()

    def _payee_mode_changed(self, on) -> None:
        if not on:
            return
        self._sync_enabled()
        self._replan()

    def answers(self) -> dict:
        """The wizard's current choices in
        :func:`~mammon.importers.tabular.apply_wizard_answers` form."""
        def col(combo):
            v = combo.currentData()
            return v or None

        a = {
            "date_col": col(self.date_combo),
            "invert_amount": self.invert.isChecked(),
            "debit_positive": self.debit_positive.isChecked(),
            "account_type": self.type_combo.currentData(),
            "description_cols": [
                self.desc_list.item(i).text()
                for i in range(self.desc_list.count())
                if self.desc_list.item(i).checkState() == Qt.Checked
            ],
        }
        if self.amt_signed.isChecked():
            a["amount_mode"] = "signed"
            a["amount_col"] = col(self.amount_combo)
        else:
            a["amount_mode"] = "debit_credit"
            a["debit_col"] = col(self.debit_combo)
            a["credit_col"] = col(self.credit_combo)
        if self.payee_col_radio.isChecked():
            a["payee_mode"] = "column"
            a["payee_col"] = col(self.payee_combo)
        elif self.payee_pair_radio.isChecked():
            a["payee_mode"] = "from_to"
            a["payee_from_col"] = col(self.from_combo)
            a["payee_to_col"] = col(self.to_combo)
        else:
            a["payee_mode"] = "in_description"
        return a

    # -- preview -------------------------------------------------------------
    def _replan(self, *_args) -> None:
        """Re-interpret the file through the REAL importer and refresh both
        previews. Never raises into the dialog: a mapping that cannot parse
        leaves the tables empty and shows why."""
        try:
            self.plan = tabular.apply_wizard_answers(self.plan, self.answers())
            err = ""
        except Exception as exc:                       # pragma: no cover - defensive
            err = f"That mapping could not be applied: {exc}"
        self._refresh_source()
        self._refresh_result()
        self.warning.setText(err or self._sanity_warning())

    def _refresh_source(self) -> None:
        frame = tabular.locate_and_frame(self.plan._text)
        header = list(frame.header) if frame else list(self.plan.header)
        rows = list(frame.rows)[:_PREVIEW_ROWS] if frame else []
        t = self.source_table
        t.clear()
        t.setColumnCount(len(header))
        t.setHorizontalHeaderLabels(header)
        t.setRowCount(len(rows))
        for ri, row in enumerate(rows):
            for ci, name in enumerate(header):
                val = row.get(name, "") if isinstance(row, dict) else ""
                t.setItem(ri, ci, QTableWidgetItem(str(val)))
        t.resizeColumnsToContents()

    def _refresh_result(self) -> None:
        r = self.plan.roles
        if r.debit or r.credit:
            amt_src = " / ".join(x for x in (r.debit, r.credit) if x) or "—"
        else:
            amt_src = r.amount or "—"
        if r.payee_from or r.payee_to:
            payee_src = " / ".join(x for x in (r.payee_from, r.payee_to) if x)
        else:
            payee_src = r.payee or "(from description)"
        titles = [
            f"Date  <-  {r.date or '—'}",
            f"Amount  <-  {amt_src}",
            f"Payee  <-  {payee_src}",
            f"Memo  <-  {' + '.join(r.description) if r.description else '—'}",
        ]
        rows = self.plan.preview[:_PREVIEW_ROWS]
        t = self.result_table
        t.clear()
        t.setColumnCount(len(titles))
        t.setHorizontalHeaderLabels(titles)
        t.setRowCount(len(rows))
        for ri, row in enumerate(rows):
            cents = row.get("amount_cents") or 0
            cells = [row.get("date") or "", f"{cents / 100:,.2f}",
                     row.get("payee") or "", row.get("memo") or ""]
            for ci, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if ci == 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                t.setItem(ri, ci, item)
        t.resizeColumnsToContents()
        self.count_label.setText(
            f"{len(self.plan.records)} transaction(s) would be imported.")

    def _sanity_warning(self) -> str:
        """Flag the mistakes that still LOOK right in a preview."""
        recs = self.plan.records
        if not recs:
            return "No rows parsed — check the Date and Amount columns."
        problems = []
        if not self.plan.roles.date:
            problems.append("no Date column is mapped")
        if all(r.amount_cents == 0 for r in recs):
            problems.append("every amount is 0.00")
        if all(r.amount_cents >= 0 for r in recs):
            problems.append("no row is negative — a spending column may need "
                            "'Invert sign', or this may be a balance column")
        if not any((r.payee or "").strip() for r in recs):
            problems.append("no row has a payee")
        return ("Check this mapping: " + "; ".join(problems) + ".") if problems else ""

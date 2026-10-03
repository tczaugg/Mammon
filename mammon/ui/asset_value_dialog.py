"""Property valuation UI -- what an asset account is WORTH, kept apart from the
register, which records what it COST.

The register is a cost-basis ledger: the purchase and the improvements posted
against it, each a dated event that actually moved money. A Zestimate is not an
event, it is a MEASUREMENT of a continuous quantity, and it is the same shape as
a security's closing price. So it lives in the same shape of place --
``asset_values`` is ``price_history`` for a thing you own exactly one of -- and
this dialog is the register's price-history chart plus an editor.

Writing valuations into the register as balance adjustments (what Quicken's
"Update Account Balance" does) was considered and rejected, for three reasons
that are all visible from this dialog:

* **A revaluation repeats.** Fifteen years of quarterly Zestimates is sixty
  adjusting transactions in a register holding maybe three real ones, and the
  real ones stop being findable.
* **Every adjustment needs a category**, so the file grows an "Unrealized Gain"
  pseudo-category that then has to be excluded from every income report, budget
  and spending pie -- the same pervasive-exclusion burden transfers already
  carry, and the main way this codebase breaks.
* **Basis becomes unrecoverable.** A $30k roof and a $30k appreciation are the
  same row once both are transactions, and the roof is the half a capital gain
  needs.

The series is fully EDITABLE here, which is the point of a separate store rather
than a fetch-only cache: a house has an appraisal history that predates the app,
and backfilling 2010's purchase appraisal and 2018's refinance appraisal is how
the chart becomes worth looking at. A hand-entered value is recorded with source
``manual`` so it stays distinguishable from a fetched one.

Both modals a user can reach from here (delete confirmation, replace-on-date
confirmation) go through ``QMessageBox.question``, and the fetch goes through a
``_fetch_values`` seam -- the headless-modal hazard in CLAUDE.md, so the tests
open no window that would block forever under the offscreen platform.
"""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from mammon import asset_values, ledger
from mammon.importers.record import dollars_to_cents
from mammon.ui.delegates import date_edit_iso, make_date_edit
from mammon.ui.models import fmt_date, fmt_money

# What a value typed by hand is tagged with, so the history can say where each
# number came from. A fetched value carries its source's own name ("zillow").
MANUAL_SOURCE = "manual"


def run_value_fetch(parent, fetch, account_name: str) -> bool:
    """Run ``fetch`` (a no-arg callable returning a
    :class:`~mammon.asset_values.FetchReport`), report what happened, and return
    whether anything was written.

    Shared by the register's gear action and this dialog's Get Value button so
    the two report a failure identically -- and so ``fetch`` stays the single
    injection seam a headless test overrides."""
    try:
        report = fetch()
    except asset_values.ValueSourceUnavailable as exc:
        QMessageBox.warning(
            parent, "Get Value",
            "No valuation source is configured." + chr(10) + chr(10) + str(exc))
        return False
    except Exception as exc:                      # provider / script failure
        QMessageBox.warning(parent, "Get Value",
                            "Could not fetch a value: %s" % exc)
        return False
    lines = ["Recorded %s for %s as of %s."
             % (fmt_money(v.value_cents), account_name, fmt_date(v.date))
             for v in report.written]
    # Name WHICH property failed and why: a silent partial success is exactly how
    # a stale value gets mistaken for a fresh one.
    lines += ["%s: %s" % (name, reason) for _aid, name, reason in report.missing]
    QMessageBox.information(parent, "Get Value",
                            chr(10).join(lines) or "Nothing to fetch.")
    return bool(report.written)


class AssetValueEditor(QDialog):
    """Add or edit ONE dated market value.

    The date is the series key, so an edit that moves a value onto a date that
    already holds one is a replacement; the caller confirms that rather than
    letting ``set_value``'s upsert silently eat a row."""

    def __init__(self, conn, account_id, value=None, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = int(account_id)
        self.original_date = value.date if value is not None else None
        self.setWindowTitle("Edit Value" if value is not None else "Add Value")
        self.resize(420, 0)

        form = QFormLayout()
        self.date = make_date_edit(self, value.date if value is not None else "")
        self.value = QLineEdit("" if value is None
                               else "%.2f" % (value.value_cents / 100.0))
        self.value.setPlaceholderText("412,700.00")
        self.note = QLineEdit("" if value is None else (value.note or ""))
        self.note.setPlaceholderText("appraisal, refinance, Zestimate...")
        form.addRow("Date", self.date)
        form.addRow("Value", self.value)
        form.addRow("Note", self.note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        hint = QLabel("What the property was worth on this date. The register "
                      "keeps what it cost; this is what it is worth.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addWidget(buttons)

    def _on_accept(self):
        try:
            cents = dollars_to_cents(self.value.text())
        except ValueError:
            QMessageBox.warning(self, "Value",
                                "That is not a value I can read.")
            return
        # Zero is never a real valuation, and writing it would quietly zero the
        # property out of net worth rather than leaving the last good value.
        if cents <= 0:
            QMessageBox.warning(self, "Value",
                                "A property's value has to be more than zero.")
            return
        self.accept()

    def values(self):
        """``(iso_date, value_cents, note)`` as entered."""
        return (date_edit_iso(self.date),
                dollars_to_cents(self.value.text()),
                self.note.text().strip() or None)


class AssetValueHistoryDialog(QDialog):
    """The value series for one asset account: view, edit, backfill, fetch, chart.

    ``value_source`` is the injection seam :func:`mammon.asset_values.fetch_values`
    already takes; tests hand in a fake and never reach the network."""

    changed = pyqtSignal()

    COLUMNS = ("Date", "Value", "Change", "Source", "Note")

    def __init__(self, conn, account_id, parent=None, value_source=None,
                 client=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = int(account_id)
        self._value_source = value_source
        self._client = client
        acct = ledger.get_account(conn, self.account_id)
        self.account_name = acct["name"] if acct is not None else str(account_id)
        self.setWindowTitle("Value History - %s" % self.account_name)
        self.resize(660, 430)

        lay = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setObjectName("assetValueSummary")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(lambda *_: self.on_edit())
        lay.addWidget(self.table)

        bar = QHBoxLayout()
        self.btn_add = QPushButton("Add...")
        self.btn_add.clicked.connect(self.on_add)
        self.btn_edit = QPushButton("Edit...")
        self.btn_edit.clicked.connect(self.on_edit)
        self.btn_delete = QPushButton("Delete")
        self.btn_delete.clicked.connect(self.on_delete)
        self.btn_fetch = QPushButton("Get Value...")
        self.btn_fetch.setToolTip(
            "Look this property's current value up from its address and record "
            "it. The address is set in Account Details.")
        self.btn_fetch.clicked.connect(self.on_fetch)
        self.btn_chart = QPushButton("Chart...")
        self.btn_chart.clicked.connect(self.on_chart)
        for b in (self.btn_add, self.btn_edit, self.btn_delete, self.btn_fetch,
                  self.btn_chart):
            bar.addWidget(b)
        bar.addStretch()
        lay.addLayout(bar)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.reload()

    # ---- display ---------------------------------------------------------
    def reload(self):
        """Re-read the series and the exposure summary from the database."""
        self._history = asset_values.value_history(self.conn, self.account_id)
        self.table.setRowCount(len(self._history))
        prev = None
        for row, v in enumerate(self._history):
            change = "" if prev is None else self._fmt_change(v.value_cents, prev)
            cells = (fmt_date(v.date), fmt_money(v.value_cents), change,
                     v.source or "", v.note or "")
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col in (1, 2):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)
            prev = v.value_cents
        self.table.resizeColumnsToContents()
        if self._history:
            self.table.selectRow(len(self._history) - 1)
        self._refresh_summary()
        self._sync_buttons()

    @staticmethod
    def _fmt_change(cents: int, prev: int) -> str:
        """One row's move against the row before it. The percentage is what makes
        a series of appraisals readable as a rate rather than a list of numbers."""
        delta = cents - prev
        sign = "+" if delta >= 0 else "-"
        pct = "  (%s%.1f%%)" % (sign, abs(delta) * 100.0 / prev) if prev else ""
        return "%s%s%s" % (sign, fmt_money(abs(delta)), pct)

    def _refresh_summary(self):
        """The line this dialog exists to show: value, what is owed against it,
        and the equity that leaves. Debt comes from the loans the user linked to
        this property (Account Details, "Secured by"), so a house with no linked
        mortgage simply reports its value against its basis."""
        exp = asset_values.exposure(self.conn, self.account_id)
        latest = self._history[-1] if self._history else None
        if latest is None:
            head = ("No value recorded yet. The register's %s is what it COST."
                    % fmt_money(exp.basis))
        else:
            head = ("Value %s as of %s     Cost basis %s"
                    % (fmt_money(latest.value_cents), fmt_date(latest.date),
                       fmt_money(exp.basis)))
        if exp.debt:
            head += ("     Debt %s     Equity %s"
                     % (fmt_money(exp.debt), fmt_money(exp.net)))
            if exp.leverage:
                head += " (%.2fx)" % exp.leverage
        self.summary.setText(head)

    def _sync_buttons(self):
        has_rows = bool(self._history)
        self.btn_edit.setEnabled(has_rows)
        self.btn_delete.setEnabled(has_rows)
        self.btn_chart.setEnabled(has_rows)

    def _selected(self):
        row = self.table.currentRow()
        if 0 <= row < len(self._history):
            return self._history[row]
        return None

    # ---- editing ---------------------------------------------------------
    def on_add(self):
        dlg = AssetValueEditor(self.conn, self.account_id, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        date, cents, note = dlg.values()
        if not self._confirm_replace(date, None):
            return
        asset_values.set_value(self.conn, self.account_id, date, cents,
                               MANUAL_SOURCE, note)
        self.reload()
        self.changed.emit()

    def on_edit(self):
        current = self._selected()
        if current is None:
            return
        dlg = AssetValueEditor(self.conn, self.account_id, current, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        date, cents, note = dlg.values()
        if not self._confirm_replace(date, current.date):
            return
        # A moved date is a NEW key: drop the old row, or the edit leaves the
        # original standing and the series silently grows a duplicate.
        if date != current.date:
            asset_values.delete_value(self.conn, self.account_id, current.date)
        asset_values.set_value(self.conn, self.account_id, date, cents,
                               current.source or MANUAL_SOURCE, note)
        self.reload()
        self.changed.emit()

    def on_delete(self):
        current = self._selected()
        if current is None:
            return
        if QMessageBox.question(
                self, "Delete Value",
                "Delete the %s value recorded on %s?"
                % (fmt_money(current.value_cents), fmt_date(current.date)),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        asset_values.delete_value(self.conn, self.account_id, current.date)
        self.reload()
        self.changed.emit()

    def _confirm_replace(self, date: str, original_date) -> bool:
        """True when it is safe to write ``date``. ``set_value`` upserts, so
        landing on a date that already holds a value REPLACES it -- silently,
        and with nothing to undo it. Ask first, unless the row being written is
        the one already sitting there."""
        if date == original_date:
            return True
        existing = next((v for v in self._history if v.date == date), None)
        if existing is None:
            return True
        return QMessageBox.question(
            self, "Replace Value",
            "%s already has a value of %s. Replace it?"
            % (fmt_date(date), fmt_money(existing.value_cents)),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No) == QMessageBox.Yes

    # ---- fetch -----------------------------------------------------------
    def _fetch_values(self):
        """Seam: the actual lookup. Overridden by headless tests so no test ever
        reaches the network -- the same shape as the register's ``_fetch_quotes``."""
        return asset_values.fetch_values(
            self.conn, [self.account_id],
            source=self._value_source, client=self._client)

    def on_fetch(self):
        wrote = run_value_fetch(self, self._fetch_values, self.account_name)
        self.reload()
        if wrote:
            self.changed.emit()

    # ---- chart -----------------------------------------------------------
    def on_chart(self):
        """The stock-price analog: the value series over time, with the ledger's
        cost basis as a reference line -- the gap between them IS the unrealized
        appreciation. matplotlib is imported LAZILY here so this dialog stays
        importable in a matplotlib-free environment."""
        if not self._history:
            QMessageBox.information(
                self, "Value History",
                "No recorded values for %s yet." % self.account_name)
            return
        from mammon.ui.charts import AssetValueCanvas, ChartDialog
        points = [(v.date, v.value_cents) for v in self._history]
        basis = ledger.account_balance(self.conn, self.account_id)
        canvas = AssetValueCanvas(self.account_name, points, basis)
        ChartDialog("Value History - %s" % self.account_name, canvas,
                    parent=self).exec_()

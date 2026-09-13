"""Exchange-rate entry and refresh UI (SRD 5.4a) -- the surface over the existing
:mod:`mammon.fx` dated rate store.

The store, the reader, the per-account currency column and the net-worth fold all
already existed (``test_fx.py``, ``test_multicurrency_ui.py``); what was missing
was any way for the user to PUT a rate in, or to refresh one. This dialog is that
surface and nothing more:

* it LISTS through :func:`mammon.fx.list_rates` (a pure read -- the UI holds no
  SQL of its own),
* it ENTERS a rate through :func:`mammon.fx.set_rate`, the store's single writer,
  an upsert on ``(date, from, to)``, and
* it REFRESHES through :func:`mammon.fx.fetch_rates`, which itself funnels every
  fetched rate back through ``set_rate``.

So the UI adds NO second write path to ``fx_rates``, exactly as the task requires.

A rate reads "how many ``to`` units equal 1 ``from`` unit on this date"; a
foreign-currency account converts to the base currency (USD) through the most
recent rate on/before a date (:func:`mammon.fx.get_rate`). Entering EUR->USD 1.10
therefore values a EUR account's balance at 1.10 USD per euro, and also answers
USD->EUR through the derived inverse.

Modal safety (CLAUDE.md, headless-modal hazard): the network fetch goes through
the ``_fetch_rates`` seam and the result notice through the overridable
``_notify``, so a headless test injects a fake source, patches ``_notify``, and
never opens a window that would block forever under the offscreen platform.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from mammon import fx, ledger
from mammon.ui.delegates import date_edit_iso, make_date_edit
from mammon.ui.models import fmt_date


class FxRateEditor(QDialog):
    """Add one dated rate, or correct the rate on an existing (from, to, date).

    When editing, the key (from/to/date) is locked: :mod:`mammon.fx` has one
    writer, :func:`~mammon.fx.set_rate`, and it upserts on that key, so only the
    rate value changes here -- moving the key would leave the original row
    orphaned (there is no delete writer, by design)."""

    def __init__(self, conn, rate_row=None, parent=None):
        super().__init__(parent)
        self.conn = conn
        editing = rate_row is not None
        self.setWindowTitle("Edit Rate" if editing else "Add Rate")
        self.resize(420, 0)

        # Local import: widgets is a heavy module, and this shortlist is the only
        # thing needed from it. The combos stay editable, so any ISO 4217 code
        # works -- the list is a convenience, not a whitelist.
        from mammon.ui.widgets import _CURRENCY_CODES

        form = QFormLayout()
        self.date = make_date_edit(self, rate_row["date"] if editing else "")
        self.base = QComboBox()
        self.base.setEditable(True)
        self.quote = QComboBox()
        self.quote.setEditable(True)
        for combo in (self.base, self.quote):
            combo.addItems(list(dict.fromkeys(_CURRENCY_CODES)))
        self.base.setCurrentText(rate_row["base"] if editing else "EUR")
        self.quote.setCurrentText(rate_row["quote"] if editing else fx.BASE_CURRENCY)
        self.rate = QLineEdit(rate_row["rate"] if editing else "")
        self.rate.setPlaceholderText("1.085")
        form.addRow("Date", self.date)
        form.addRow("From (1 unit of)", self.base)
        form.addRow("To", self.quote)
        form.addRow("Rate", self.rate)

        # The key is immutable on an edit (see class docstring).
        if editing:
            for w in (self.date, self.base, self.quote):
                w.setEnabled(False)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)
        lay.addWidget(buttons)

        for w in (self.base, self.quote):
            w.currentTextChanged.connect(self._refresh_hint)
        self.rate.textChanged.connect(self._refresh_hint)
        self._refresh_hint()

    def _refresh_hint(self, *_):
        b = (self.base.currentText() or "").strip().upper() or "?"
        q = (self.quote.currentText() or "").strip().upper() or "?"
        r = self.rate.text().strip() or "?"
        self.hint.setText("1 %s = %s %s on this date." % (b, r, q))

    def _parsed_rate(self):
        """The entered rate as a positive :class:`~decimal.Decimal`, or ``None``
        when it is unreadable or not greater than zero (a zero/negative rate is
        never a real exchange rate and would silently zero an account out)."""
        try:
            d = Decimal(self.rate.text().strip())
        except (InvalidOperation, ValueError):
            return None
        return d if d > 0 else None

    def _on_accept(self):
        base = self.base.currentText().strip().upper()
        quote = self.quote.currentText().strip().upper()
        if not base or not quote:
            QMessageBox.warning(self, "Rate", "Pick both currencies.")
            return
        if base == quote:
            QMessageBox.warning(self, "Rate", "Pick two different currencies.")
            return
        if self._parsed_rate() is None:
            QMessageBox.warning(self, "Rate", "Enter a rate greater than zero.")
            return
        if not date_edit_iso(self.date):
            QMessageBox.warning(self, "Rate", "Pick a date for this rate.")
            return
        self.accept()

    def values(self):
        """``(iso_date, base, quote, rate_text)`` as entered."""
        return (date_edit_iso(self.date),
                self.base.currentText().strip().upper(),
                self.quote.currentText().strip().upper(),
                self.rate.text().strip())


class FxRatesDialog(QDialog):
    """List, enter and refresh the dated exchange rates a multi-currency ledger
    values against.

    ``fx_source`` is the same injection seam :func:`mammon.fx.fetch_rates`
    already takes (an object with ``get_rates(pairs) -> list[fx.FxRate]``); tests
    hand in a fake and never reach the network. ``changed`` fires whenever a rate
    is written, so the main window can revalue its foreign-currency accounts."""

    changed = pyqtSignal()

    COLUMNS = ("Date", "From", "To", "Rate", "1 From =")

    def __init__(self, conn, parent=None, fx_source=None):
        super().__init__(parent)
        self.conn = conn
        self._fx_source = fx_source
        self.setWindowTitle("Exchange Rates")
        self.resize(560, 420)

        lay = QVBoxLayout(self)
        blurb = QLabel(
            "A rate says how many of the second currency equal 1 unit of the "
            "first on a date. Foreign-currency accounts convert to %s through the "
            "most recent rate on or before a date." % fx.BASE_CURRENCY)
        blurb.setWordWrap(True)
        lay.addWidget(blurb)

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
        self.btn_refresh = QPushButton("Refresh Rates")
        self.btn_refresh.setToolTip(
            "Fetch the latest rate for every currency your accounts use, and for "
            "every pair already listed.")
        self.btn_refresh.clicked.connect(self.on_refresh)
        for b in (self.btn_add, self.btn_edit, self.btn_refresh):
            bar.addWidget(b)
        bar.addStretch()
        lay.addLayout(bar)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.reload()

    # ---- display ---------------------------------------------------------
    def reload(self):
        """Re-read the whole rate store and repaint the table."""
        self._rates = fx.list_rates(self.conn)
        self.table.setRowCount(len(self._rates))
        for row, r in enumerate(self._rates):
            desc = "1 %s = %s %s" % (r["base"], r["rate"], r["quote"])
            cells = (fmt_date(r["date"]), r["base"], r["quote"], r["rate"], desc)
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 3:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        if self._rates:
            self.table.selectRow(0)
        self._sync_buttons()

    def _sync_buttons(self):
        self.btn_edit.setEnabled(bool(self._rates))

    def _selected(self):
        row = self.table.currentRow()
        if 0 <= row < len(self._rates):
            return self._rates[row]
        return None

    # ---- editing ---------------------------------------------------------
    def on_add(self):
        dlg = FxRateEditor(self.conn, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        date, base, quote, rate = dlg.values()
        fx.set_rate(self.conn, date, base, quote, rate)
        self.reload()
        self.changed.emit()

    def on_edit(self):
        current = self._selected()
        if current is None:
            return
        dlg = FxRateEditor(self.conn, current, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        date, base, quote, rate = dlg.values()
        fx.set_rate(self.conn, date, base, quote, rate)
        self.reload()
        self.changed.emit()

    # ---- refresh ---------------------------------------------------------
    def _pairs_to_refresh(self):
        """Every (from, to) worth a fresh quote: each foreign account currency
        against the base, plus every pair already recorded. Deduped here;
        same-currency pairs are dropped by :func:`~mammon.fx.fetch_rates`."""
        pairs, seen = [], set()

        def add(b, q):
            b = (b or fx.BASE_CURRENCY).strip().upper() or fx.BASE_CURRENCY
            q = (q or fx.BASE_CURRENCY).strip().upper() or fx.BASE_CURRENCY
            if b != q and (b, q) not in seen:
                seen.add((b, q))
                pairs.append((b, q))

        for a in ledger.list_accounts(self.conn, include_closed=True,
                                      include_hidden=True):
            add(a["currency"], fx.BASE_CURRENCY)
        for r in self._rates:
            add(r["base"], r["quote"])
        return pairs

    def _fetch_rates(self, pairs):
        """Seam: the actual fetch. Overridden by headless tests so no test reaches
        the network -- the same shape as the asset dialog's ``_fetch_values``."""
        return fx.fetch_rates(self.conn, pairs, source=self._fx_source)

    def _notify(self, title, text):
        """Overridable so a headless test does not open a blocking modal
        (CLAUDE.md: even an unconditional success notice must be patchable)."""
        QMessageBox.information(self, title, text)

    def on_refresh(self):
        pairs = self._pairs_to_refresh()
        if not pairs:
            self._notify("Refresh Rates",
                         "No foreign-currency accounts or rates to refresh.")
            return
        try:
            written = self._fetch_rates(pairs)
        except fx.FxRateUnavailable as exc:
            self._notify("Refresh Rates",
                         "No exchange-rate source is available." + chr(10) * 2
                         + str(exc))
            return
        except Exception as exc:                      # provider / network failure
            self._notify("Refresh Rates", "Could not fetch rates: %s" % exc)
            return
        self.reload()
        if written:
            lines = ["1 %s = %s %s on %s"
                     % (r.base, r.rate, r.quote, fmt_date(r.date))
                     for r in written]
            self._notify("Refresh Rates", chr(10).join(lines))
            self.changed.emit()
        else:
            self._notify("Refresh Rates", "No new rates were returned.")

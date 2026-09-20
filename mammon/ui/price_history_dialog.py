"""Price history UI -- the recorded closes for one symbol, and an editor for them.

`mammon.ui.asset_value_dialog` is this dialog's twin: an asset's valuation series
IS `price_history` for a thing you own exactly one of, so the two windows are
deliberately the same shape (a table, Add/Edit/Delete, a chart button, every
modal behind an overridable seam). Read that module's docstring for why a
measurement series lives apart from the register at all; what follows is what is
different HERE.

**A price is an observation, so "edit" is narrower than it looks.** A close is
what the market did on a day. The app cannot know better offline, so the editor's
real repertoire is: record a close nobody downloaded (a backfill), and DELETE one
that is wrong. Changing a price in place is offered because a typo in a
hand-entered backfill is a real thing, but a wrong DOWNLOADED close is deleted
rather than guessed at -- an unpriced day is a state every valuation already
reports honestly, while an invented number is not.

**What is shown is what is STORED.** `investments.price_history` divides each
close by the splits dated after it so the chart reads in today's units; this
dialog reads `investments.stored_prices` instead, which does not. An editor
showing adjusted numbers would write one back as though it were as-traded, and a
single round-trip through this window would silently restate the history. The
SOURCE column is shown for the same reason: `yfinance` and `manual` are treated
differently by the split adjustment (:data:`~mammon.investments.SPLIT_ADJUSTED_SOURCES`),
so it is not an incidental detail the user should have to guess at.

**Crypto arrives under its pair symbol.** A coin's prices are filed as
``{SYM}-USD`` (`crypto.pair_symbol`) so a coin can never collide with a stock of
the same ticker, which is why asking for ``ETH`` found nothing at all. Callers
pass whatever symbol they hold and :func:`price_symbol_for` maps it, so a crypto
holding and a security holding reach the same window by the same gesture.

**An aliased ticker's rows span two names.** ``stored_prices`` unions the
canonical identity, and each row carries the symbol it is actually filed under,
so a delete removes the row where it lives rather than missing it. The Symbol
column appears only when more than one is present -- it is noise on the ordinary
single-name series.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QAbstractItemView,
    QHeaderView, QDialogButtonBox, QMessageBox,
)

from mammon import investments
from mammon.ui.delegates import make_date_edit, date_edit_iso
from mammon.ui.models import fmt_date


#: What a hand-entered price is filed as. It matters beyond bookkeeping: the
#: split adjustment treats a provider's closes as already back-adjusted and a
#: manual one as as-traded, so mislabelling a backfill rescales it.
MANUAL_SOURCE = "manual"


def price_symbol_for(conn, symbol: str) -> str:
    """The symbol a price series is FILED under, for a security or a coin.

    A security is filed under its own ticker; a coin under ``{SYM}-USD``. The
    caller holds a holding, not a filing convention, so the mapping lives here
    rather than in five call sites -- asking for ``ETH`` is what found no price
    history at all."""
    name = (symbol or "").strip()
    if not name:
        return ""
    from mammon import crypto
    pair = crypto.pair_symbol(name)
    if pair != name and investments.stored_prices(conn, pair):
        return pair
    # No coin series: either it IS a security, or it is a coin nobody has priced
    # yet, and then the pair symbol is still where a price would go.
    if investments.stored_prices(conn, name):
        return name
    return pair if _looks_like_coin(conn, name) else name


def _looks_like_coin(conn, symbol: str) -> bool:
    """Whether any crypto wallet holds this symbol -- the only way to tell an
    unpriced coin from an unpriced stock, since neither has a price row yet."""
    row = conn.execute(
        "SELECT 1 FROM crypto_holdings WHERE symbol = ? COLLATE NOCASE LIMIT 1",
        (symbol,)).fetchone()
    return row is not None


class PriceEditor(QDialog):
    """Add or edit ONE dated close.

    The date is the series key, so moving a price onto a date that already holds
    one is a replacement; the caller confirms it rather than letting
    ``record_price``'s upsert silently eat a row -- the same rule the asset-value
    editor follows."""

    def __init__(self, symbol, row=None, parent=None):
        super().__init__(parent)
        self.symbol = symbol
        self.original_date = row["date"] if row else None
        self.setWindowTitle("Edit Price" if row else "Add Price")
        self.resize(420, 0)

        form = QFormLayout()
        self.date = make_date_edit(self, row["date"] if row else "")
        self.close_price = QLineEdit(
            "" if row is None else _plain(row["close_price"]))
        self.close_price.setPlaceholderText("2444.8899")
        form.addRow("Date", self.date)
        form.addRow("Close", self.close_price)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        hint = QLabel(
            f"What {symbol} closed at on this date, per share or per coin. "
            f"Saved as '{MANUAL_SOURCE}', which is how the split adjustment "
            "tells a hand-entered price from a downloaded one.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addWidget(buttons)

    def _on_accept(self):
        try:
            price = _parse_price(self.close_price.text())
        except ValueError:
            QMessageBox.warning(self, "Close",
                                "That is not a price I can read.")
            return
        # A zero close is never an observation. Writing one would value the
        # holding at nothing, which reads exactly like a real collapse; an
        # ABSENT price is the honest way to say a day is not known.
        if price <= 0:
            QMessageBox.warning(
                self, "Close",
                "A price has to be more than zero. To say a day's price is not "
                "known, delete the row instead of entering nothing.")
            return
        self.accept()

    def values(self):
        """``(iso_date, close_price)`` as entered."""
        return date_edit_iso(self.date), _parse_price(self.close_price.text())


class PriceHistoryDialog(QDialog):
    """The recorded closes for one symbol: Date | Close | Source, editable.

    Every modal is an overridable method (:meth:`_confirm`, :meth:`_warn`, and
    the two editor seams) so a headless test drives Add/Edit/Delete by calling
    them -- a ``QDialog.exec_`` blocks forever under the offscreen platform."""

    DATE, CLOSE, SOURCE, SYMBOL = range(4)

    def __init__(self, conn, symbol, parent=None, currency=None):
        super().__init__(parent)
        self.conn = conn
        self.symbol = price_symbol_for(conn, symbol)
        self.display_symbol = (symbol or "").strip()
        self.currency = currency
        title = f"Price History - {self.symbol}"
        if currency:
            title = f"{title} ({currency})"
        self.setWindowTitle(title)
        self.resize(560, 520)

        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Date", "Close", "Source",
                                              "Symbol"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.itemDoubleClicked.connect(lambda _it: self.on_edit())
        lay.addWidget(self.table, 1)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        row = QHBoxLayout()
        self.add_btn = QPushButton("Add…")
        self.edit_btn = QPushButton("Edit…")
        self.delete_btn = QPushButton("Delete")
        self.chart_btn = QPushButton("Chart")
        for btn, slot in ((self.add_btn, self.on_add),
                          (self.edit_btn, self.on_edit),
                          (self.delete_btn, self.on_delete),
                          (self.chart_btn, self.on_chart)):
            btn.setAutoDefault(False)
            btn.clicked.connect(lambda _c=False, s=slot: s())
            row.addWidget(btn)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        lay.addLayout(row)

        self.reload()

    # -- reading -------------------------------------------------------------
    def reload(self):
        """Re-read the series. Nothing is cached: a delete or an add is one
        reload away from being on screen, and the numbers are always the
        table's."""
        self.rows = investments.stored_prices(self.conn, self.symbol)
        names = {r["symbol"] for r in self.rows}
        # The Symbol column earns its place only on an aliased series, where a
        # row may be filed under the other ticker.
        self.table.setColumnHidden(self.SYMBOL, len(names) <= 1)
        self.table.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            cells = (fmt_date(row["date"]), _plain(row["close_price"]),
                     row["source"] or "", row["symbol"])
            for col, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if col == self.CLOSE:
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, col, cell)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(
            self.SOURCE, QHeaderView.Interactive)
        if self.rows:
            self.table.selectRow(len(self.rows) - 1)
        self._refresh_summary()
        self._sync_buttons()

    def _refresh_summary(self):
        if not self.rows:
            self.summary.setText(
                f"No recorded prices for {self.symbol}. Anything valued from "
                "this symbol reads as unpriced until one is added or "
                "downloaded.")
            return
        first, last = self.rows[0], self.rows[-1]
        self.summary.setText(
            "%d price%s, %s to %s. Latest close %s."
            % (len(self.rows), "" if len(self.rows) == 1 else "s",
               fmt_date(first["date"]), fmt_date(last["date"]),
               _plain(last["close_price"])))

    def _sync_buttons(self):
        has = self._selected() is not None
        self.edit_btn.setEnabled(has)
        self.delete_btn.setEnabled(has)
        self.chart_btn.setEnabled(bool(self.rows))

    def _selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.rows):
            return None
        return self.rows[row]

    def table_rows(self) -> list:
        """What the table is showing, as plain strings -- the test seam."""
        return [tuple(self.table.item(r, c).text() for c in range(4))
                for r in range(self.table.rowCount())]

    # -- the modal seams -----------------------------------------------------
    def _confirm(self, title, text) -> bool:
        return QMessageBox.question(
            self, title, text,
            QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes

    def _warn(self, title, text) -> None:
        QMessageBox.warning(self, title, text)

    def _ask_price(self, row=None):
        """The add/edit modal. Returns ``(date, close)`` or None."""
        dlg = PriceEditor(self.symbol, row, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return None
        return dlg.values()

    # -- the verbs -----------------------------------------------------------
    def on_add(self) -> bool:
        entered = self._ask_price()
        if entered is None:
            return False
        date, price = entered
        return self._write(date, price, original_date=None)

    def on_edit(self) -> bool:
        row = self._selected()
        if row is None:
            return False
        entered = self._ask_price(row)
        if entered is None:
            return False
        date, price = entered
        return self._write(date, price, original_date=row["date"],
                           symbol=row["symbol"])

    def _write(self, date, price, original_date=None, symbol=None) -> bool:
        target = symbol or self.symbol
        existing = {r["date"] for r in self.rows
                    if r["symbol"] == target and r["date"] != original_date}
        if date in existing and not self._confirm(
                "Replace price",
                f"{target} already has a price for {fmt_date(date)}. "
                "Replace it?"):
            return False
        investments.record_price(self.conn, target, date, price, MANUAL_SOURCE)
        # Moving a price to another date is an add plus a delete: the date is the
        # key, so the row it used to occupy would otherwise survive as a ghost.
        if original_date and original_date != date:
            investments.delete_price(self.conn, target, original_date)
        self.reload()
        return True

    def on_delete(self) -> bool:
        row = self._selected()
        if row is None:
            return False
        if not self._confirm(
                "Delete price",
                f"Delete {row['symbol']}'s close of {_plain(row['close_price'])} "
                f"on {fmt_date(row['date'])}?\n\nAnything valued on that date "
                "will read as unpriced until a price is recorded again."):
            return False
        investments.delete_price(self.conn, row["symbol"], row["date"])
        self.reload()
        return True

    def on_chart(self) -> None:
        if not self.rows:
            self._warn("Chart", f"No recorded price history for {self.symbol}.")
            return
        from mammon.ui.charts import ChartDialog, PriceHistoryCanvas
        points = investments.price_history_bounds(self.conn, self.symbol)
        canvas = PriceHistoryCanvas(self.symbol, points, currency=self.currency)
        title = f"Price History - {self.symbol}"
        if self.currency:
            title = f"{title} ({self.currency})"
        ChartDialog(title, canvas, parent=self).exec_()


def _plain(value) -> str:
    """A Decimal price without exponent or trailing-zero noise."""
    if value is None:
        return ""
    text = format(Decimal(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _parse_price(text):
    """A typed price as a Decimal. Commas and a leading currency sign are
    tolerated, because a pasted quote carries them."""
    clean = (text or "").strip().replace(",", "").lstrip("$").strip()
    if not clean:
        raise ValueError("no price")
    try:
        return Decimal(clean)
    except (InvalidOperation, ValueError):
        raise ValueError(f"cannot read {text!r} as a price")

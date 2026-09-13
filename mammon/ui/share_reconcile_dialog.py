"""Reconcile SHARE balances against a brokerage or plan statement (SRD 5.11b),
and import the holdings snapshot that states them.

The cash reconcile (``ReconcileDialog`` in :mod:`mammon.ui.widgets`) answers one
question -- does the account's cleared balance equal the statement's ending
balance. An investment statement asks that question once PER SECURITY, in
shares, and the answers do not add up to one number: 3.117 missing shares of one
fund says nothing about another. So this dialog is the cash one, multiplied:

  * the top table lists EVERY security on the account with the statement's
    starting and ending share counts (both editable in place), what clearing has
    explained so far, and the difference still unexplained;
  * one TAB per security lists that security's quantity-changing rows -- and
    only those, because a dividend or a return-of-capital moves cash, not shares,
    and would be noise on a share reconcile (:func:`investments.is_quantity_action`);
  * clicking a row toggles its cleared mark, persisted immediately, so a
    half-finished reconcile survives closing the window;
  * Finish reconciles the CURRENT security's period and leaves the window open,
    because a statement covers several funds and closing after each one would
    make the user reopen the dialog once per fund.

Where the ending numbers come from. A brokerage download used to carry positions
along with transactions; downloading by hand usually does not -- the user can
fetch quotes and balances as a CSV, but not in the same file as the
transactions. 'Import Holdings Snapshot...' takes that second file
(:mod:`mammon.importers.holdings_core`), matches each fund to a security by
symbol when the file prints one and BY NAME when it does not (a 401(k) export of
internal funds has no symbol column at all), and leaves each fund's stated share
count in this dialog's Ending column. It creates NO transactions: a snapshot is
a statement of fact, and turning it into history would invent trades. For a
tickerless plan fund it also records the statement price, which is the only
price such a fund will ever have.

The adjustment, and why its warning is on the face of the window. When shares
cannot be cleared away, the domain layer offers the same escape Quicken did -- a
real ShrsIn/ShrsOut row that closes the gap. Deleting that row later (once the
missing shares turn up) does NOT undo the reconciliation; the period has to be
reconciled again by hand. That is exactly the sort of thing a user learns two
years later, so :data:`investments.SHARE_ADJUSTMENT_WARNING` is shown verbatim
in a permanent label, not buried in a docstring or in one dismissed prompt.

Modal seams. :meth:`confirm_adjustment`, :meth:`warn` and
:meth:`choose_snapshot_file` are overridable methods rather than inline
``QMessageBox``/``QFileDialog`` calls: under the offscreen platform a modal
built and ``exec_()``-ed directly blocks forever, so the tests replace these
three and drive the whole life cycle headless (CLAUDE.md, headless-modal hazard).
No SQL and no share arithmetic live here -- every number on the screen comes
from :mod:`mammon.investments`.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from mammon import investments, ledger
from mammon.importers import holdings_core
from mammon.ui.delegates import date_edit_iso, make_date_edit

_GREEN = "color:#2e7d32;"
_RED = "color:#c0392b;"


def qty_text(value) -> str:
    """A share quantity as exponent-free text with no trailing-zero noise.
    Display only -- the stored spelling is whatever the domain layer wrote."""
    if value in (None, ""):
        return ""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def parse_qty(text) -> Optional[Decimal]:
    """User-typed share count -> Decimal, or None when it is not a number.
    Thousands separators are tolerated because statements print them."""
    t = str(text or "").strip().replace(",", "")
    if not t:
        return None
    try:
        return Decimal(t)
    except (InvalidOperation, ValueError):
        return None


def _item(text, *, editable=False, align=None, color=None) -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    flags = it.flags() & ~Qt.ItemIsEditable
    if editable:
        flags |= Qt.ItemIsEditable
    it.setFlags(flags)
    if align is not None:
        it.setTextAlignment(align | Qt.AlignVCenter)
    if color:
        from PyQt5.QtGui import QColor
        it.setForeground(QColor(color))
    return it


class ShareReconcileDialog(QDialog):
    """The share mirror of the cash reconcile workspace. See the module
    docstring for why it is per-security and why the adjustment warning is
    permanent."""

    # Top summary table
    SEC, START, END, CLEARED, COMPUTED, DIFF = range(6)
    # Per-security transaction table
    CLR, DATE, ACTION, QTY, MEMO = range(5)

    def __init__(self, conn, account_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = account_id
        self.finished_ok = False
        self.finished_symbols: list = []
        acct = ledger.get_account(conn, account_id)
        self.account_name = acct["name"] if acct else ""
        title = "Reconcile Shares"
        if self.account_name:
            title += " - " + self.account_name
        self.setWindowTitle(title)
        self.resize(940, 620)

        self._loading = True
        self.symbols: list = []
        self._state: dict = {}
        self._rows: dict = {}
        self._tables: dict = {}

        outer = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Statement ending date"))
        self.date = make_date_edit(iso=self._initial_date())
        self.date.dateChanged.connect(self._date_changed)
        top.addWidget(self.date)
        top.addStretch(1)
        self.import_btn = QPushButton("Import Holdings Snapshot...")
        self.import_btn.clicked.connect(lambda: self.import_snapshot())
        top.addWidget(self.import_btn)
        outer.addLayout(top)

        outer.addWidget(QLabel(
            "Starting and ending share counts come from the statement; edit "
            "either one in place."))
        self.summary_table = QTableWidget(0, 6)
        self.summary_table.setHorizontalHeaderLabels(
            ["Security", "Starting shares", "Ending shares", "Cleared change",
             "Computed ending", "Difference"])
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.summary_table.horizontalHeader().setSectionResizeMode(
            self.SEC, QHeaderView.Stretch)
        self.summary_table.setMaximumHeight(190)
        self.summary_table.itemChanged.connect(self._summary_edited)
        self.summary_table.itemSelectionChanged.connect(self._summary_selected)
        outer.addWidget(self.summary_table)

        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(lambda _i: self._refresh_labels())
        outer.addWidget(self.tabs, 1)

        marks = QHBoxLayout()
        self.mark_all_btn = QPushButton("Mark All")
        self.mark_all_btn.clicked.connect(lambda: self._mark_all(True))
        self.clear_all_btn = QPushButton("Clear All")
        self.clear_all_btn.clicked.connect(lambda: self._mark_all(False))
        marks.addWidget(self.mark_all_btn)
        marks.addWidget(self.clear_all_btn)
        marks.addStretch(1)
        self.detail_label = QLabel("")
        marks.addWidget(self.detail_label)
        outer.addLayout(marks)

        self.summary_label = QLabel("")
        outer.addWidget(self.summary_label)

        # Permanent, never dismissed: what deleting an adjustment later costs.
        self.warning_label = QLabel(investments.SHARE_ADJUSTMENT_WARNING)
        self.warning_label.setWordWrap(True)
        outer.addWidget(self.warning_label)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        self.finish_btn = QPushButton("Finish Security")
        buttons.addButton(self.finish_btn, QDialogButtonBox.AcceptRole)
        self.finish_btn.clicked.connect(self.finish_current)
        outer.addWidget(buttons)

        self._loading = False
        self.reload()

    # -- seams the tests replace (see the module docstring) ------------------

    def warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def confirm_adjustment(self, symbol: str, qty: str, warning: str) -> bool:
        """Ask before booking the share adjustment. The warning is repeated
        here as well as on the window, because this is the moment the user is
        deciding to live with a number they cannot explain."""
        text = ("Reconciling " + str(symbol) + " leaves " + qty + " shares "
                "unexplained.\n\nRecord a share adjustment for that amount so "
                "the period can be reconciled?\n\n" + warning)
        return QMessageBox.question(
            self, "Record share adjustment", text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def choose_snapshot_file(self) -> str:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Holdings Snapshot", "",
            "CSV files (*.csv);;All files (*)")
        return path

    # -- state ---------------------------------------------------------------

    def statement_date(self) -> str:
        return date_edit_iso(self.date)

    def _initial_date(self) -> str:
        """The date an imported snapshot (or a saved draft) already stated;
        today otherwise. Picking it up means an import followed by 'reconcile'
        needs no retyping."""
        dates = [d.get("statement_date") for d in
                 investments.snapshot_targets(self.conn, self.account_id).values()
                 if d.get("statement_date")]
        if dates:
            return max(dates)
        from datetime import date as _date
        return _date.today().isoformat()

    def _canonical_symbols(self) -> list:
        seen = {}
        raw = list(investments.symbols_used(self.conn, self.account_id))
        raw += list(investments.snapshot_targets(self.conn, self.account_id))
        for s in raw:
            if not str(s or "").strip():
                continue
            canon = investments.resolve_symbol(self.conn, str(s).strip())
            if canon:
                seen[canon] = True
        return sorted(seen)

    def _load_state(self) -> None:
        """Per-security starting/ending counts: the saved draft (which is where
        an imported snapshot leaves the statement's numbers) if there is one,
        otherwise the book quantity, so the dialog opens on real figures."""
        date = self.statement_date()
        self.symbols = self._canonical_symbols()
        books = investments.compute_holdings(self.conn, self.account_id,
                                             as_of=date)
        state = {}
        for sym in self.symbols:
            old = self._state.get(sym, {})
            draft = investments.get_share_reconcile_draft(
                self.conn, self.account_id, sym) or {}
            ending = draft.get("ending_qty") or old.get("ending") or ""
            if not ending:
                lot = books.get(sym)
                ending = qty_text(lot.qty if lot is not None else 0)
            state[sym] = {
                "starting": draft.get("starting_qty") or old.get("starting") or "",
                "ending": qty_text(ending),
                "price": draft.get("ending_price") or "",
                "summary": None,
            }
        self._state = state

    def _summary_for(self, symbol: str) -> dict:
        st = self._state[symbol]
        return investments.share_reconcile_summary(
            self.conn, self.account_id, symbol, self.statement_date(),
            st["ending"] or "0", st["starting"] or None)

    # -- building ------------------------------------------------------------

    def reload(self) -> None:
        """Rebuild everything from the database (opening, date change, import,
        finish). Cheap enough at statement scale, and it cannot drift."""
        current = self.current_symbol()
        self._load_state()
        self._build_tabs()
        self._fill_summary()
        self._recompute()
        if current in self.symbols:
            self.tabs.setCurrentIndex(self.symbols.index(current))

    def _build_tabs(self) -> None:
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        self._tables = {}
        self._rows = {}
        date = self.statement_date()
        for sym in self.symbols:
            rows = investments.share_reconcile_rows(
                self.conn, self.account_id, sym, through=date)
            self._rows[sym] = rows
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(0, 0, 0, 0)
            table = QTableWidget(len(rows), 5)
            table.setHorizontalHeaderLabels(
                ["Clr", "Date", "Action", "Quantity", "Memo"])
            table.verticalHeader().setVisible(False)
            table.setSelectionBehavior(QAbstractItemView.SelectRows)
            table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            table.horizontalHeader().setSectionResizeMode(
                self.MEMO, QHeaderView.Stretch)
            for i, r in enumerate(rows):
                clr = _item(self._clr_text(r), align=Qt.AlignCenter)
                clr.setData(Qt.UserRole, r["id"])
                table.setItem(i, self.CLR, clr)
                table.setItem(i, self.DATE, _item(r["date"]))
                action = r["action"]
                if r["split"] and r["split_display"]:
                    action = str(action) + " " + str(r["split_display"])
                table.setItem(i, self.ACTION, _item(action))
                table.setItem(i, self.QTY,
                              _item(qty_text(r["delta"]) if not r["split"]
                                    else "", align=Qt.AlignRight))
                table.setItem(i, self.MEMO, _item(r["memo"] or ""))
            table.cellClicked.connect(
                lambda row, _col, s=sym: self.toggle_row(s, row))
            lay.addWidget(table)
            self._tables[sym] = table
            self.tabs.addTab(page, sym)
        self.tabs.blockSignals(False)

    def _clr_text(self, row) -> str:
        if row["split"]:
            return "*"          # history the statement already reflects
        if row["reconciled"]:
            return "R"
        return "c" if row["cleared"] else ""

    def _fill_summary(self) -> None:
        self._loading = True
        try:
            self.summary_table.setRowCount(len(self.symbols))
            for i, sym in enumerate(self.symbols):
                st = self._state[sym]
                self.summary_table.setItem(i, self.SEC, _item(sym))
                self.summary_table.setItem(
                    i, self.START, _item(st["starting"], editable=True,
                                         align=Qt.AlignRight))
                self.summary_table.setItem(
                    i, self.END, _item(st["ending"], editable=True,
                                       align=Qt.AlignRight))
                for col in (self.CLEARED, self.COMPUTED, self.DIFF):
                    self.summary_table.setItem(i, col,
                                               _item("", align=Qt.AlignRight))
        finally:
            self._loading = False

    # -- recomputation -------------------------------------------------------

    def _recompute(self) -> None:
        self._loading = True
        try:
            for i, sym in enumerate(self.symbols):
                summary = self._summary_for(sym)
                self._state[sym]["summary"] = summary
                diff = summary["difference"]
                self.summary_table.item(i, self.CLEARED).setText(
                    qty_text(summary["cleared_qty_change"]))
                self.summary_table.item(i, self.COMPUTED).setText(
                    qty_text(summary["computed_ending_qty"]))
                cell = self.summary_table.item(i, self.DIFF)
                cell.setText(qty_text(diff))
                from PyQt5.QtGui import QColor
                cell.setForeground(QColor("#2e7d32" if diff == 0 else "#c0392b"))
        finally:
            self._loading = False
        self._refresh_labels()

    def _refresh_labels(self) -> None:
        sym = self.current_symbol()
        if sym is None:
            self.detail_label.setText("")
            self.summary_label.setText("This account holds no securities.")
            self.finish_btn.setEnabled(False)
            return
        self.finish_btn.setEnabled(True)
        summary = self._state[sym].get("summary")
        if summary is None:
            return
        rows = self._rows.get(sym, [])
        cleared = sum(1 for r in rows if r["cleared"] and not r["reconciled"])
        self.detail_label.setText(
            str(cleared) + " of " + str(len(rows)) + " items cleared")
        diff = summary["difference"]
        self.summary_label.setText(
            sym + ":  starting " + qty_text(summary["prior_qty"])
            + "   cleared " + qty_text(summary["cleared_qty_change"])
            + "   computed ending " + qty_text(summary["computed_ending_qty"])
            + "   statement ending " + qty_text(summary["stated_ending_qty"])
            + "   difference " + qty_text(diff))
        self.summary_label.setStyleSheet(_GREEN if diff == 0 else _RED)

    # -- user actions --------------------------------------------------------

    def current_symbol(self) -> Optional[str]:
        i = self.tabs.currentIndex()
        if i < 0 or i >= len(self.symbols):
            return None
        return self.symbols[i]

    def select_symbol(self, symbol: str) -> bool:
        if symbol not in self.symbols:
            return False
        self.tabs.setCurrentIndex(self.symbols.index(symbol))
        return True

    def rows_for(self, symbol: str) -> list:
        return self._rows.get(symbol, [])

    def difference(self, symbol: str) -> Decimal:
        summary = self._state.get(symbol, {}).get("summary")
        return Decimal(0) if summary is None else summary["difference"]

    def toggle_row(self, symbol: str, index: int) -> bool:
        """Toggle one row's cleared mark (persisted immediately, like the cash
        dialog). Reconciled rows and splits are not the user's to toggle."""
        rows = self._rows.get(symbol) or []
        if index < 0 or index >= len(rows):
            return False
        row = rows[index]
        if row["reconciled"] or row["split"]:
            return False
        want = not row["cleared"]
        if not investments.set_investment_cleared(self.conn, row["id"], want):
            return False
        row["cleared"] = want
        table = self._tables.get(symbol)
        if table is not None and table.item(index, self.CLR) is not None:
            table.item(index, self.CLR).setText(self._clr_text(row))
        self._recompute()
        return True

    def _mark_all(self, cleared: bool) -> None:
        sym = self.current_symbol()
        if sym is None:
            return
        for i, row in enumerate(self._rows.get(sym, [])):
            if row["reconciled"] or row["split"] or row["cleared"] == cleared:
                continue
            self.toggle_row(sym, i)

    def set_ending(self, symbol: str, text) -> None:
        self._set_field(symbol, "ending", text)

    def set_starting(self, symbol: str, text) -> None:
        self._set_field(symbol, "starting", text)

    def _set_field(self, symbol: str, field: str, text) -> None:
        if symbol not in self._state:
            return
        value = "" if text in (None, "") else qty_text(text)
        self._state[symbol][field] = value
        self._save_draft(symbol)
        i = self.symbols.index(symbol)
        col = self.END if field == "ending" else self.START
        self._loading = True
        try:
            self.summary_table.item(i, col).setText(value)
        finally:
            self._loading = False
        self._recompute()

    def _save_draft(self, symbol: str) -> None:
        st = self._state[symbol]
        investments.save_share_reconcile_draft(
            self.conn, self.account_id, symbol,
            statement_date=self.statement_date(),
            starting_qty=st["starting"], ending_qty=st["ending"])

    def _summary_edited(self, item) -> None:
        if self._loading or item.column() not in (self.START, self.END):
            return
        row = item.row()
        if row >= len(self.symbols):
            return
        sym = self.symbols[row]
        field = "starting" if item.column() == self.START else "ending"
        typed = item.text().strip()
        if typed and parse_qty(typed) is None:
            self._loading = True
            try:
                item.setText(self._state[sym][field])
            finally:
                self._loading = False
            self.warn("Reconcile Shares",
                      "'" + typed + "' is not a share count.")
            return
        self._set_field(sym, field, parse_qty(typed) if typed else "")

    def _summary_selected(self) -> None:
        rows = self.summary_table.selectionModel().selectedRows() \
            if self.summary_table.selectionModel() else []
        if rows:
            self.tabs.setCurrentIndex(rows[0].row())

    def _date_changed(self, *_a) -> None:
        if self._loading:
            return
        self.reload()

    # -- import --------------------------------------------------------------

    def import_snapshot(self, path: Optional[str] = None) -> Optional[dict]:
        """Apply a holdings/balance snapshot CSV to this account and adopt its
        stated share counts. Creates no transactions (see the module docstring)."""
        if not path:
            path = self.choose_snapshot_file()
        if not path:
            return None
        try:
            results = holdings_core.import_holdings_file(
                self.conn, self.account_id, path)
        except Exception as exc:                        # parse/date failures
            self.warn("Import Holdings Snapshot", str(exc))
            return None
        summary = holdings_core.summarize_snapshot(results)
        if summary["dates"]:
            self._loading = True
            try:
                from PyQt5.QtCore import QDate
                self.date.setDate(QDate.fromString(max(summary["dates"]),
                                                   "yyyy-MM-dd"))
            finally:
                self._loading = False
        self.reload()
        text = (str(summary["matched"]) + " of " + str(summary["lines"])
                + " lines matched a security; "
                + str(summary["prices_recorded"]) + " prices recorded.")
        if summary["unmatched_names"]:
            text += ("  Not matched (nothing was guessed at): "
                     + ", ".join(summary["unmatched_names"]))
        self.status_label.setText(text)
        return summary

    # -- finishing -----------------------------------------------------------

    def finish_current(self) -> bool:
        """Finish the CURRENT security's period. A residual difference is only
        closed with an adjustment the user explicitly accepts."""
        sym = self.current_symbol()
        if sym is None:
            return False
        st = self._state[sym]
        summary = st.get("summary") or self._summary_for(sym)
        adjust = False
        if summary["difference"] != 0:
            if not self.confirm_adjustment(sym, qty_text(summary["difference"]),
                                           summary["adjustment_warning"]):
                return False
            adjust = True
        try:
            investments.finish_share_reconciliation(
                self.conn, self.account_id, sym, self.statement_date(),
                st["ending"] or "0", starting_qty=st["starting"] or None,
                adjust=adjust)
        except ValueError as exc:
            self.warn("Reconcile Shares", str(exc))
            return False
        self.finished_ok = True
        if sym not in self.finished_symbols:
            self.finished_symbols.append(sym)
        note = sym + " reconciled through " + self.statement_date() + "."
        if adjust:
            note += ("  A share adjustment was recorded.  "
                     + investments.SHARE_ADJUSTMENT_WARNING)
        self.reload()
        self.select_symbol(sym)
        self.status_label.setText(note)
        return True

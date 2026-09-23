"""The classic import-review list shown beneath a register.

A download no longer writes straight to the register: :func:`downloads.
download_rows_for_review` returns the raw rows, :func:`import_review.build_review`
classifies each as NEW or MATCHING, and this panel renders them for the user to
act on. Nothing enters the register until a row is accepted (MATCHING) or saved
(NEW); every commit goes through the :mod:`mammon.import_review` persistence
helpers, which are the sole writers here.

The panel is created hidden and lives at the bottom of ``RegisterWidget``. It is
shown (auto-opened) after a download that produced reviewable rows, and can be
re-opened from the register's "Review…" toolbar button while a review is
pending. It keeps its per-row action state in memory so an accept/save can be
undone until the panel is dismissed.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import QEvent, QStandardPaths, Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFontMetrics
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import crypto, import_review
from . import prefs, style
from .models import fmt_date

# Column layout of the review table. Category is intentionally absent:
# classification (category) is chosen later in the register's pending row, not
# here. Num IS shown and is the one editable review cell -- the user needs to
# SEE the check number / 3rd-party label (Venmo/Paypal/'Sched') to look up a
# payee, and CORRECT it before accepting.
STATUS, DATE, NUM, PAYEE, MEMO, AMOUNT = range(6)

# Investment accounts get their own column set. A cash row's identity is
# date+payee+amount; an investment row's is date+SECURITY+shares, and the shares
# and per-share price are the whole point -- a plan fee that redeems units states
# both, which is how the price is derived.
#
# SECURITY is the editable column here, for the same reason Num is editable in
# the cash layout: it is the field the user must be able to CORRECT before
# accepting. Brokers rename funds and append tickers to their names, so an
# imported row routinely names a security the ledger does not know. Accepting it
# unedited creates a second, phantom security -- and the price history derived
# from that row lands on the phantom instead of the fund the user actually
# holds. Editing the name here is what puts it on the right one.
I_STATUS, I_DATE, I_SECURITY, I_ACTION, I_SHARES, I_PRICE, I_AMOUNT = range(7)

# A crypto WALLET (a paper-wallet address) gets a THIRD column set, because the
# other two are both wrong for it. A wallet row's identity is date + coin +
# quantity + the counterparty ADDRESS; no fiat ever moves, so an Amount column
# has nothing to put in it. Reviewing a by-address export through the cash layout
# is what produced the reported defect -- the block number landed under Amount
# and a Cash Bal column appeared on an account that has no cash sleeve.
#
# Increases and decreases get their OWN columns (the source's Value_IN / Value_OUT),
# the way a cash register separates payment from deposit: on-chain the two are
# different events with different counterparties, and one signed column hides that.
# FEE is coin-denominated (ETH gas), never USD, and only a send carries one.
C_STATUS, C_DATE, C_PAYEE, C_MEMO, C_COIN, C_IN, C_OUT, C_FEE = range(8)

# A crypto EXCHANGE gets a FOURTH set. It is a wallet's columns plus the fiat a
# custodial account really does move: a trade's per-unit Price and the Amount
# debited or credited to the cash sleeve. Giving it the wallet's set would hide
# what a Buy cost; giving it the CASH set would hide the coin entirely, which is
# what it did before -- a Coinbase history reviewed as cash showed a dollar
# figure and no asset, for rows whose whole content is "0.099 ETH moved".
X_STATUS, X_DATE, X_PAYEE, X_MEMO, X_ACTION, X_COIN, X_QTY, X_PRICE, X_AMOUNT = range(9)

# Foreground for an already-accepted / discarded row: present but inert.
_ACTIONED_FG = "#9a9a9a"
_HEADERS = ["Status", "Date", "Num", "Payee", "Memo", "Amount"]
_INV_HEADERS = ["Status", "Date", "Security", "Action", "Shares", "Price", "Amount"]
_CRYPTO_HEADERS = ["Status", "Date", "Payee", "Memo", "Coin",
                   "Coin In", "Coin Out", "Fee"]
_EXCHANGE_HEADERS = ["Status", "Date", "Payee", "Memo", "Action", "Coin",
                     "Quantity", "Price", "Amount"]
# Which side of the register a wallet row's quantity renders on. Read from the
# domain layer's own vocabulary so the panel cannot drift from the writers.
_COIN_OUT_ACTIONS = crypto.WALLET_DEBIT_ACTIONS


def _fmt_amount(cents: int) -> str:
    return f"{cents / 100:,.2f}"


def _date_offset_days(a: str, b: str):
    """Absolute day gap between two ISO dates, or ``None`` if either is unusable."""
    from datetime import date
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except (ValueError, TypeError):
        return None


def _merge_policy_tooltip(entry) -> str:
    """State the no-overwrite merge policy for a MATCHING row, for the user.

    Accepting a match reconciles the EXISTING register line in place through
    :func:`import_review.accept_match` -- the sole writer of this path. It marks
    the line cleared and stamps the source's transaction id ONLY when the line
    had none; it never rewrites the date, amount, payee, memo or category the
    user entered by hand. So a download can never overwrite a manually-entered
    date with the bank's posting date -- the exact failure users report of other
    tools. Rendering it as a per-row tooltip turns that policy from implicit into
    inspectable, which is the whole point of the review queue."""
    method = getattr(entry, "match_method", "") or "amount + date"
    if getattr(entry.mapped, "is_investment", False):
        merged = "the source's transaction id, only if the line had none"
    else:
        merged = ("this line marked cleared, plus the source's transaction id "
                  "only if the line had none")
    return (
        f"Matches an existing register line (by {method}).\n"
        f"Accepting MERGES into that line -- it does not overwrite it.\n"
        f"  Merged in: {merged}.\n"
        f"  Preserved: your date, amount, payee, memo and category, kept as-is.\n"
        f"A download never overwrites a field you entered by hand."
    )


class _RowState:
    """Per-entry action bookkeeping (kept out of the immutable ReviewEntry)."""

    __slots__ = ("done", "saved_txn_id", "prior")

    def __init__(self):
        self.done = False              # accepted (MATCHING) or saved (NEW)
        self.saved_txn_id = None       # txn id created by a NEW save (for undo)
        self.prior = None              # prior {fitid,cleared} for a match undo


class ImportReviewPanel(QWidget):
    """Review list for freshly downloaded rows. Emits :attr:`changed` whenever a
    row is committed or undone so the owning register refreshes."""

    changed = pyqtSignal()
    # A review row actually entered the ledger -- a MATCHING row accepted against
    # its register line, or a NEW row saved. The register's accepted sound hangs
    # off this. `changed` is not a substitute: it also fires for edits, visibility
    # switches and discards, none of which put a transaction in the book. Review
    # is where the sound earns its keep -- it is the high-volume accept gesture,
    # done heads-down at the keyboard, and the whole point is confirming the save
    # without looking away from the next row.
    transactionSaved = pyqtSignal()   # one accept gesture == one whole transaction
    # A review row became the selection (its ReviewEntry) or the selection was
    # cleared (None). The owning register listens: a MATCHING row highlights and
    # scrolls its existing register line into view; a NEW row opens an editable
    # not-yet-accepted pending row in the register itself.
    row_selected = pyqtSignal(object)
    # The panel's Accept was pressed on a NEW row -- acceptance actually happens
    # on the register's editable pending row, so the register handles this.
    accept_new_requested = pyqtSignal(object)
    # The Num cell of a review row was edited (entry, new text). The register
    # mirrors it into its open pending row so the edit isn't lost when accept
    # reads the pending buffer (which was seeded before the edit). Selecting a
    # row seeds the pending buffer first, so this after-seed sync is required.
    num_edited = pyqtSignal(object, str)
    # The show/hide-history choice changed (new mode). The register reloads the
    # entry list through import_review.load_review and hands it back.
    visibility_changed = pyqtSignal(str)

    def __init__(self, conn, account_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = int(account_id)
        self._entries: list = []
        self._states: list[_RowState] = []
        self.is_investment = self._account_is_investment()
        self.is_crypto_wallet = self._account_is_crypto_wallet()
        self.is_crypto_exchange = self._account_is_crypto_exchange()
        if self.is_crypto_wallet:
            self._headers = _CRYPTO_HEADERS
        elif self.is_crypto_exchange:
            self._headers = _EXCHANGE_HEADERS
        elif self.is_investment:
            self._headers = _INV_HEADERS
        else:
            self._headers = _HEADERS
        # The review list is GROUND TRUTH -- what the source sent -- and is
        # read-only, save for the cash layout's Num (a check number the user
        # needs in order to identify a payee). Corrections to an investment row
        # belong in the register's editable PENDING row, which is where the same
        # correction happens for cash. Two editable surfaces onto one value could
        # disagree about what Accept would commit.
        self._edit_col = (None if (self.is_investment or self.is_crypto_wallet
                                   or self.is_crypto_exchange) else NUM)
        # Guards _on_num_edited against the setItem() calls in _render_row, which
        # would otherwise re-fire itemChanged while we are just re-drawing.
        self._suppress_num_edit = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(4)

        self._box = QFrame()
        self._box.setObjectName("importReviewBox")
        box = QVBoxLayout(self._box)
        box.setContentsMargins(6, 6, 6, 6)
        box.setSpacing(4)

        head = QHBoxLayout()
        self.title = QLabel("Import Review")
        self.title.setStyleSheet("font-weight: bold;")
        head.addWidget(self.title)
        head.addStretch()
        # How much already-actioned history to keep on screen. Accepting a row
        # used to make it disappear for good even though the row was kept in the
        # database forever; the useful middle ground is grayed-out history, and
        # how much of it is a per-account habit, so the choice is remembered per
        # account and survives a restart (being interrupted mid-review is exactly
        # when it matters).
        head.addWidget(QLabel("Show:"))
        self.visibility = QComboBox()
        self.visibility.addItem("Pending only", prefs.VIS_PENDING)
        self.visibility.addItem("Pending + accepted", prefs.VIS_BATCH)
        self.visibility.addItem("All retained", prefs.VIS_ALL)
        self.visibility.setToolTip(
            "How much of the review history to show.\n"
            "Pending only - hide anything already accepted or discarded.\n"
            "This import - also show this import's actioned rows, grayed.\n"
            "Past imports - also show earlier imports still retained.")
        mode = prefs.review_visibility(self.account_id)
        i = self.visibility.findData(mode)
        self.visibility.setCurrentIndex(i if i >= 0 else 0)
        self.visibility.currentIndexChanged.connect(self._on_visibility_changed)
        head.addWidget(self.visibility)
        self.close_btn = QPushButton("Dismiss")
        self.close_btn.setToolTip(
            "Hide the review list. Un-actioned rows stay pending and can be "
            "re-opened with Review…")
        self.close_btn.clicked.connect(self.hide)
        # Dismissing clears any editable pending register row the panel opened.
        self.close_btn.clicked.connect(lambda: self.row_selected.emit(None))
        head.addWidget(self.close_btn)
        box.addLayout(head)

        self.table = QTableWidget(0, len(self._headers))
        self.table.setHorizontalHeaderLabels(self._headers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        # The review list shows the downloaded rows as imported and is read-only
        # EXCEPT the Num column: only its cells carry Qt.ItemIsEditable (set in
        # _render_row), so these edit triggers can only ever open a Num editor.
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.AnyKeyPressed)
        hh = self.table.horizontalHeader()
        if self.is_crypto_wallet:
            # The counterparty is a 42-character hex address; it needs the room,
            # and it is the one field that identifies the row to the user.
            hh.setSectionResizeMode(C_PAYEE, QHeaderView.Stretch)
            hh.setSectionResizeMode(C_MEMO, QHeaderView.Stretch)
            # Coin quantities are NOT dollar amounts: ETH carries 18 decimals, so
            # a real row reads 25.566401739928923937 -- 21 characters against a
            # fiat cell's 9. At the default column width those elided to "0....",
            # which on a number is worse than useless. Measured from the FONT
            # rather than left to ResizeToContents, which takes whatever the
            # widest row wants and starves the stretched Payee beside it -- and
            # the payee is the 42-character address that identifies the row.
            # Clamped at both ends: a measurement is only as sane as the font it
            # is taken in, and an unbounded one starved the Payee to a 21px stub.
            # Past the cap the number elides and the cell's tooltip carries the
            # full value.
            fm = QFontMetrics(self.table.font())
            coin_w = max(110, min(200, fm.width("25.566401739928923937") + 16))
            hh.setMinimumSectionSize(72)
            for col, w in ((C_COIN, max(56, fm.width("WBTC") + 16)),
                           (C_IN, coin_w), (C_OUT, coin_w),
                           (C_FEE, coin_w + 36)):        # + " ETH"
                hh.setSectionResizeMode(col, QHeaderView.Fixed)
                self.table.setColumnWidth(col, w)
        elif self.is_crypto_exchange:
            hh.setSectionResizeMode(X_PAYEE, QHeaderView.Stretch)
            hh.setSectionResizeMode(X_MEMO, QHeaderView.Stretch)
            fm = QFontMetrics(self.table.font())
            coin_w = max(110, min(200, fm.width("25.566401739928923937") + 16))
            hh.setMinimumSectionSize(72)
            for col, w in ((X_COIN, max(56, fm.width("WBTC") + 16)),
                           (X_QTY, coin_w), (X_PRICE, coin_w)):
                hh.setSectionResizeMode(col, QHeaderView.Fixed)
                self.table.setColumnWidth(col, w)
        else:
            hh.setSectionResizeMode(PAYEE, QHeaderView.Stretch)
            hh.setSectionResizeMode(MEMO, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        # An edited Num cell flows straight to entry.mapped.check_number so it
        # carries through save_new when the row is accepted.
        self.table.itemChanged.connect(self._on_num_edited)
        # Delete key on a selected row discards it (the user's stray blank-line rows).
        self.table.installEventFilter(self)
        box.addWidget(self.table)

        bar = QHBoxLayout()
        self.accept_btn = QPushButton("Accept")
        self.accept_btn.setToolTip(
            "Accept the selected row: a MATCHING row is reconciled against its "
            "existing register line; a NEW row is committed from its editable "
            "pending register row. Selection then advances to the next row.")
        self.accept_btn.clicked.connect(self._on_accept)
        self.accept_all_btn = QPushButton("Accept All")
        self.accept_all_btn.setToolTip(
            "Save every NEW row and accept every MATCHING row, then clear the "
            "review list.")
        self.accept_all_btn.clicked.connect(self._on_accept_all)
        self.discard_all_btn = QPushButton("Discard All")
        self.discard_all_btn.setToolTip(
            "Drop every pending row without adding it to the register. They are "
            "removed, so downloading the same range again brings them back.")
        self.discard_all_btn.clicked.connect(self._on_discard_all)
        self.undo_all_btn = QPushButton("Undo All Matches")
        self.undo_all_btn.setToolTip(
            "Revert every accepted MATCHING row back to pending.")
        self.undo_all_btn.clicked.connect(self._on_undo_all_matches)
        for b in (self.accept_btn, self.accept_all_btn, self.discard_all_btn,
                  self.undo_all_btn):
            bar.addWidget(b)
        bar.addStretch()
        # Load Amazon invoice itemization (cash accounts only). Reads a
        # time-tagged invoice file from the user's Downloads folder and runs a
        # review session that itemizes each order into per-item split legs
        # (requirement A6/A8). Hidden on investment accounts, which have no
        # item-split concept. The register also offers the same action from its
        # gear menu, so this stays reachable when the panel is otherwise empty.
        # Parent the button to the panel AT CONSTRUCTION (the ``self`` argument).
        # ``bar`` is a bare QHBoxLayout not installed on any widget until the
        # ``box.addLayout(bar)`` below, so a button merely added to it stays
        # parentless -- and calling setVisible() on a parentless QPushButton makes
        # Qt briefly realise it as its own TOP-LEVEL window. That stray window
        # steals focus from a register cell being edited in ANOTHER account,
        # closing (and committing) that editor before MainWindow.open_register's
        # leave-guard can see it -- a silent cross-account edit commit. Parented
        # up front, setVisible only marks it shown-with-the-panel and grabs no
        # focus (the panel itself is hidden until a load).
        self.load_amazon_btn = QPushButton("Load Amazon Invoices…", self)
        self.load_amazon_btn.setToolTip(
            "Load a time-tagged Amazon invoice file (from your Downloads folder) "
            "and review each order as an itemized split against this account. "
            "Nothing is stored -- the file can be re-loaded any time.")
        self.load_amazon_btn.clicked.connect(self.load_amazon_invoices)
        self.load_amazon_btn.setVisible(not self.is_investment)
        bar.addWidget(self.load_amazon_btn)
        box.addLayout(bar)

        # Right-click a row -> Manual Match… (hand-pick the existing register line).
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        outer.addWidget(self._box)
        # Seed from any persisted pending rows so a restart / re-open shows them.
        self._load_pending_into_memory()
        self.hide()

    # ---- population -------------------------------------------------------
    def set_entries(self, entries) -> None:
        """Load a fresh review list (replaces any prior one).

        A list can now contain already-actioned rows (the "pending + accepted"
        and "all retained" views), so each row's action state is seeded from what
        is STORED. Starting every row un-done would let the user act twice on a
        row already committed, and would keep the Review… button lit with nothing
        left to review."""
        self._entries = list(entries)
        self._states = [self._state_for(e) for e in self._entries]
        self._rebuild()

    @staticmethod
    def _state_for(entry) -> "_RowState":
        st = _RowState()
        if getattr(entry, "is_actioned", False):
            st.done = True
            st.saved_txn_id = getattr(entry, "matched_txn_id", None)
        return st

    def _load_pending_into_memory(self) -> None:
        """Populate the table from the persisted rows for this account, honouring
        the account's saved visibility so a reopened panel matches the toggle."""
        self._entries = import_review.load_review(
            self.conn, self.account_id,
            prefs.review_visibility(self.account_id))
        self._states = [self._state_for(e) for e in self._entries]
        self._rebuild()

    def reload_pending(self) -> None:
        """Re-read the persisted pending rows and show or hide accordingly."""
        self._load_pending_into_memory()
        # Show only when something still needs action: a list of nothing but
        # grayed history is not a review waiting to be done.
        if self.has_pending():
            self.show()
        else:
            self.hide()

    def load_amazon_invoices(self, path: "Optional[str]" = None) -> int:
        """Load a time-tagged Amazon invoice file and run an itemization review.

        Reads the file on demand (nothing is stored -- requirement A6), classifies
        each order into a NEW or MATCHING review row through
        :func:`import_review.build_amazon_review` (the sole review-flow writer),
        and shows the resulting rows in this panel. The proposed per-item split
        rides on each row transiently; accepting it later writes the legs through
        the Amazon accept path. Returns the number of review rows produced.

        The file dialog defaults to the user's Downloads folder, where the
        webSlinger Amazon script drops its report. ``path`` bypasses the dialog
        (tests / callers that already have a file)."""
        if self.is_investment:
            return 0
        if path is None:
            downloads = QStandardPaths.writableLocation(
                QStandardPaths.DownloadLocation) or ""
            path, _ = QFileDialog.getOpenFileName(
                self, "Load Amazon Invoices", downloads,
                "Amazon invoice export (*.json);;All files (*)")
            if not path:
                return 0
        try:
            entries = import_review.build_amazon_review(
                self.conn, self.account_id, path)
        except Exception as exc:             # pragma: no cover - UI error path
            QMessageBox.warning(
                self, "Load Amazon Invoices",
                "Could not read that invoice file:\n%s" % exc)
            return 0
        if not entries:
            QMessageBox.information(
                self, "Load Amazon Invoices",
                "No card charges to reconcile were found in that file.\n"
                "Orders fully covered by a gift-card balance (and refunds) "
                "create no card charge.")
            return 0
        # Reveal BEFORE populating: set_entries selects row 0 and emits
        # row_selected, and the register's handler acts on it only when the panel
        # is already visible (a MATCHING row then highlights its register line).
        self.show()
        self.set_entries(entries)
        return len(entries)

    def has_pending(self) -> bool:
        """True while any row is still un-actioned (nothing committed yet)."""
        return any(not s.done for s in self._states)

    def pending_count(self) -> int:
        return sum(1 for s in self._states if not s.done)

    def _rebuild(self) -> None:
        self._populate()
        if self._entries:
            self.table.selectRow(0)
        else:
            self._sync_buttons()

    def refresh_display(self) -> None:
        """Re-render the visible rows without changing the selection -- used when
        a display preference (e.g. the date format) changes so open review dates
        update live to match the register."""
        self._populate()

    def _populate(self) -> None:
        """Fill the table from ``self._entries`` without changing the selection."""
        self.table.setRowCount(len(self._entries))
        for i in range(len(self._entries)):
            self._render_row(i)
        self._update_title()

    def _render_row(self, i: int) -> None:
        entry = self._entries[i]
        m = entry.mapped

        def cell(text):
            # Read-only cell: shows the imported data as-is (Num is the one
            # editable column, built separately below).
            it = QTableWidgetItem("" if text is None else str(text))
            it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            return it

        actioned = bool(getattr(entry, "is_actioned", False))
        self._suppress_num_edit = True
        try:
            status = (entry.state or "").title() if actioned else entry.label
            # A MATCHING row's Status cell explains, on hover, exactly what
            # accepting does: it merges into the existing line and PRESERVES the
            # user's fields (no silent overwrite). The policy lives in one place
            # (import_review.accept_match); this only surfaces it.
            merge_tip = (_merge_policy_tooltip(entry)
                         if getattr(entry, "is_matching", False) else None)
            if self.is_crypto_wallet:
                self._render_crypto_row(i, m, status, cell, actioned, merge_tip)
                if actioned:
                    self._gray_row(i)
                return
            if self.is_crypto_exchange:
                self._render_exchange_row(i, m, status, cell, actioned, merge_tip)
                if actioned:
                    self._gray_row(i)
                return
            if self.is_investment:
                self._render_investment_row(i, m, status, cell, actioned, merge_tip)
                if actioned:
                    self._gray_row(i)
                return
            st = cell(status)
            if merge_tip:
                st.setToolTip(merge_tip)
            self.table.setItem(i, STATUS, st)
            self.table.setItem(i, DATE, cell(fmt_date(m.date)))
            # Num is editable free text: a check number, a 3rd-party label
            # (Venmo/Paypal), or 'Sched'. Edits land on m.check_number.
            num = cell(m.check_number)
            if not actioned:                 # history is inert
                num.setFlags(num.flags() | Qt.ItemIsEditable)
            self.table.setItem(i, NUM, num)
            # GROUND TRUTH ONLY. Payee is what the SOURCE supplied -- blank for a
            # row that carried nothing but a statement description -- and Memo is
            # that description verbatim. The proposed rename lives in the
            # register's editable row, so it stays obvious that a suggested payee
            # was DERIVED from the description rather than sent by the bank.
            self.table.setItem(i, PAYEE, cell(m.payee))
            self.table.setItem(i, MEMO, cell(m.memo))
            amt = cell(_fmt_amount(m.amount_cents))
            amt.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(i, AMOUNT, amt)
            if actioned:
                self._gray_row(i)
        finally:
            self._suppress_num_edit = False

    def _account_is_investment(self) -> bool:
        """Whether this panel is reviewing an investment account (its rows carry
        a security, shares and a price rather than a payee)."""
        row = self.conn.execute(
            "SELECT type FROM accounts WHERE id=?", (self.account_id,)).fetchone()
        return bool(row) and str(row[0]).strip().lower() == "investment"

    def _account_is_crypto_wallet(self) -> bool:
        """Whether this panel is reviewing a coin-native crypto WALLET -- a single
        address whose rows move coin and no fiat. A crypto EXCHANGE account is
        deliberately excluded: it has a cash sleeve and trades coin for dollars,
        so its rows really do carry an amount and a price. The classification is
        read through :mod:`mammon.crypto` rather than compared here, so the two
        kinds are distinguished in exactly one place."""
        return crypto.is_wallet_account(
            crypto.get_account(self.conn, self.account_id))

    def _account_is_crypto_exchange(self) -> bool:
        """Whether this panel is reviewing a custodial crypto EXCHANGE -- coins
        PLUS a fiat cash sleeve. Distinguished from a wallet in the one place the
        two kinds are ever distinguished, :mod:`mammon.crypto`."""
        return crypto.is_exchange_account(
            crypto.get_account(self.conn, self.account_id))

    def _render_exchange_row(self, i, m, status, cell, actioned, merge_tip=None) -> None:
        """Draw one custodial-exchange review row: the coin AND the fiat.

        The ACTION is shown because on this source it is the least certain field.
        A Coinbase history states product names ("Pro Deposit", "Exchange
        Withdrawal", "Advanced Trade Buy") that only approximate what happened,
        and the same name means opposite directions on different rows. The mapped
        action is the importer's reading of it; the register's pending row is
        where the user corrects it."""
        st = cell(status)
        if merge_tip:
            st.setToolTip(merge_tip)
        self.table.setItem(i, X_STATUS, st)
        self.table.setItem(i, X_DATE, cell(fmt_date(m.date)))
        self.table.setItem(i, X_PAYEE, cell(m.payee))
        self.table.setItem(i, X_MEMO, cell(m.memo))
        act = cell(m.action)
        source_type = (m.raw or {}).get("source_type")
        if source_type and source_type.upper() != (m.action or "").upper():
            # Show what the FILE called it, so a mapping the user disagrees with
            # is visible rather than silently substituted.
            act.setToolTip(f"The export called this {source_type!r}.")
        self.table.setItem(i, X_ACTION, act)
        self.table.setItem(i, X_COIN, cell(m.symbol))
        for col, text in ((X_QTY, (m.quantity or "").strip()),
                          (X_PRICE, (m.price or "").strip())):
            it = cell(text)
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if text:
                it.setToolTip(text)
            self.table.setItem(i, col, it)
        # Fiat only moves on a trade or a bare cash deposit/withdrawal. A coin
        # move carries a USD valuation in the source, but no money changed hands,
        # so the cell stays EMPTY rather than showing a figure the sleeve never saw.
        amt = cell(_fmt_amount(m.amount_cents) if m.amount_cents else "")
        amt.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if m.amount_cents < 0:
            amt.setForeground(QBrush(QColor(style.negative_color())))
        self.table.setItem(i, X_AMOUNT, amt)

    def _render_crypto_row(self, i, m, status, cell, actioned, merge_tip=None) -> None:
        """Draw one coin-native wallet review row: the counterparty address, the
        coin, and the quantity in whichever direction it moved. Everything is the
        imported ground truth and read-only -- the chain is not a guess the way an
        importer's action mapping is, so there is nothing here to correct.

        The quantity is stored UNSIGNED with the direction carried by the action,
        so it renders under Coin In or Coin Out rather than as a signed number."""
        st = cell(status)
        if merge_tip:
            st.setToolTip(merge_tip)
        self.table.setItem(i, C_STATUS, st)
        self.table.setItem(i, C_DATE, cell(fmt_date(m.date)))
        # The on-chain counterparty IS the payee: the From address on a coin
        # increase, the To address on a decrease.
        self.table.setItem(i, C_PAYEE, cell(m.payee))
        self.table.setItem(i, C_MEMO, cell(m.memo))
        if not getattr(m, "is_crypto", False):
            # A row queued for this wallet by the CASH path -- the leftovers of a
            # by-address export that was routed to the generic delimited importer
            # before crypto routing existed. It carries a fiat amount and no coin,
            # so there is nothing to show in the coin columns and nothing this
            # account could do with it. Name it rather than drawing four blanks,
            # which read as a corrupt row.
            legacy = cell("(not on-chain)")
            legacy.setToolTip(
                "This row was queued by the cash importer before on-chain "
                "routing existed. It carries no coin, so it cannot be added to "
                "a wallet. Discard it and import the export again.")
            self.table.setItem(i, C_COIN, legacy)
            for col in (C_IN, C_OUT, C_FEE):
                self.table.setItem(i, col, cell(""))
            return
        self.table.setItem(i, C_COIN, cell(m.symbol))
        is_out = (m.action or "").strip().upper() in _COIN_OUT_ACTIONS
        qty = (m.quantity or "").strip()
        for col, text in ((C_IN, "" if is_out else qty),
                          (C_OUT, qty if is_out else "")):
            it = cell(text)
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            # A quantity long enough to elide anyway is one hover from readable
            # rather than lost behind an ellipsis.
            if text:
                it.setToolTip(text)
            self.table.setItem(i, col, it)
        # The network fee is paid in the COIN (ETH gas), never in dollars, and
        # only the sender pays it -- so an inbound row's Fee cell stays empty.
        fee = ""
        if m.fee_quantity:
            fee = f"{m.fee_quantity} {m.fee_symbol}".strip()
        fee_item = cell(fee)
        fee_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if fee:
            fee_item.setToolTip(fee)
        self.table.setItem(i, C_FEE, fee_item)

    def _render_investment_row(self, i, m, status, cell, actioned, merge_tip=None) -> None:
        """Draw one investment review row. Security is editable; everything else
        is the imported ground truth."""
        st = cell(status)
        if merge_tip:
            st.setToolTip(merge_tip)
        self.table.setItem(i, I_STATUS, st)
        self.table.setItem(i, I_DATE, cell(fmt_date(m.date)))
        # The column does double duty, matching the register
        # (investments.category_display): a trade shows its security, a cash
        # line -- a fee, interest, a dividend paid in cash -- shows its
        # DESCRIPTION, the only text such a row carries. It used to show the
        # source's literal placeholder ("-"), and after placeholder blanking a
        # bare empty cell, neither of which tells the user what the row is.
        sec = cell(m.symbol or m.memo)
        if m.symbol:
            sec.setToolTip(
                "The security as the source named it. Correct it in the "
                "register's pending row below before accepting -- a name the "
                "ledger does not know becomes a NEW security, and the price "
                "derived from this row goes to that one.")
        else:
            sec.setToolTip("No security on this row -- showing its description.")
        self.table.setItem(i, I_SECURITY, sec)
        self.table.setItem(i, I_ACTION, cell(m.action))
        for col, text in ((I_SHARES, m.quantity), (I_PRICE, m.price),
                          (I_AMOUNT, _fmt_amount(m.amount_cents))):
            it = cell(text)
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(i, col, it)

    def _on_visibility_changed(self, _index=None) -> None:
        """Remember the choice for THIS account and ask the owner to reload.

        Persisted per account rather than globally: an account fed by one clean
        monthly download wants actioned rows hidden, while one that needs
        constant correction wants the history in view."""
        mode = self.visibility.currentData() or prefs.VIS_PENDING
        prefs.set_review_visibility(self.account_id, mode)
        self.visibility_changed.emit(mode)

    def _gray_row(self, i: int) -> None:
        """Render an already-accepted / discarded row as inert history.

        Kept visible rather than deleted: it is the ground truth to compare
        against when two rows are confusable, the way back when a rename turns
        out wrong, and the bearings after an interruption. (Recovering a
        DISCARDED row is no longer this view's job -- discarding removes the row,
        so re-downloading the range brings it back.)"""
        for col in range(self.table.columnCount()):
            it = self.table.item(i, col)
            if it is not None:
                it.setForeground(QBrush(QColor(_ACTIONED_FG)))

    def _on_num_edited(self, item) -> None:
        """Persist the layout's one editable cell -- Num for cash, Security for an
        investment -- so the correction carries through to accept. Ignores the
        setItem() churn during a re-render."""
        if (self._suppress_num_edit or self._edit_col is None
                or item.column() != self._edit_col):
            return
        row = item.row()
        if not (0 <= row < len(self._entries)):
            return
        entry = self._entries[row]
        entry.mapped.check_number = item.text().strip()
        # Persist it: this is a correction the user made, not a transient
        # display value, so it must survive a restart like every other field.
        import_review.set_check_number(
            self.conn, getattr(entry, "review_id", None),
            entry.mapped.check_number)
        self.num_edited.emit(entry, entry.mapped.check_number)

    def _update_title(self) -> None:
        total = len(self._entries)
        new = sum(1 for e in self._entries if e.is_new)
        match = total - new
        pending = self.pending_count()
        self.title.setText(
            f"Import Review — {total} row(s): {new} new, {match} matching "
            f"({pending} pending)")

    # ---- selection helpers ------------------------------------------------
    def _selected_index(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def current_entry(self):
        """The selected ReviewEntry, or ``None`` when nothing is selected."""
        i = self._selected_index()
        return self._entries[i] if 0 <= i < len(self._entries) else None

    def eventFilter(self, obj, event):
        """Delete key on the review table discards the selected pending row."""
        if (obj is self.table and event.type() == QEvent.KeyPress
                and event.key() == Qt.Key_Delete):
            i = self._selected_index()
            if i >= 0:
                self.discard_index(i)
                return True
        return super().eventFilter(obj, event)

    def _on_selection_changed(self) -> None:
        self._sync_buttons()
        self.row_selected.emit(self.current_entry())

    def _sync_buttons(self) -> None:
        self.accept_btn.setEnabled(self.current_entry() is not None)

    # ---- actions (also callable directly by tests) ------------------------
    def _select_index(self, i: int) -> None:
        """Move the visible selection and emit row_selected exactly once.

        After a repopulate the row index is often reused, so selectRow alone may
        not fire itemSelectionChanged for the NEW entry -- hence the explicit
        emit with signals blocked around the move."""
        self.table.blockSignals(True)
        self.table.selectRow(i)
        self.table.blockSignals(False)
        self._sync_buttons()
        self.row_selected.emit(self._entries[i])

    def _remove_and_advance(self, i: int, state: str = "accepted",
                            txn_id=None, *, drop: bool = False) -> None:
        """Retire the acted-on entry ``i`` and advance to the next actionable row.

        In "pending only" the row leaves the list, as it always did. In a mode
        that SHOWS actioned rows it must stay put and go gray instead -- dropping
        it made an accepted row vanish from a view whose whole purpose is to keep
        it visible, and it reappeared as soon as the user toggled the filter,
        because the reload re-queried what the in-memory list had thrown away.

        ``drop`` forces removal in EVERY mode. That is for DISCARD, which now
        deletes its ``review_items`` row: graying a row the database no longer
        holds puts the screen at odds with a re-query, which is the same
        disagreement the graying was introduced to fix, pointed the other way.

        Either way the selection advances to the next row still needing action,
        which is the classic auto-advance."""
        show_actioned = (self.visibility.currentData() != prefs.VIS_PENDING
                         and not drop)
        if show_actioned:
            entry = self._entries[i]
            try:
                entry.state = state
                # Record WHAT it produced, not just that it is done. These entry
                # objects came from build_review and are never reloaded, so
                # without this an in-session accept leaves accepted_txn_id None
                # and clicking the row afterwards highlights nothing -- the row
                # knows it is accepted but not what it became.
                if txn_id is not None:
                    entry.accepted_txn_id = txn_id
            except Exception:            # pragma: no cover - defensive
                pass
            self._states[i].done = True
        else:
            del self._entries[i]
            del self._states[i]
        self._populate()
        n = len(self._entries)
        if show_actioned:
            # Advance to the next row that still needs action; if none is left,
            # the review is finished even though rows remain on screen.
            nxt = next((j for j in range(i + 1, n)
                        if not getattr(self._entries[j], "is_actioned", False)), None)
            if nxt is None:
                nxt = next((j for j in range(0, n)
                            if not getattr(self._entries[j], "is_actioned", False)), None)
            self._sync_buttons()
            if nxt is None:
                self.row_selected.emit(None)
                return
            self._select_index(nxt)
            return
        if n == 0:
            self.hide()
            self._sync_buttons()
            self.row_selected.emit(None)
            return
        self._select_index(i if i < n else n - 1)

    def accept_index(self, i: int) -> int:
        """Accept the MATCHING row at ``i`` against its existing register line,
        then drop it and advance. Returns the matched txn id."""
        entry = self._entries[i]
        if not entry.is_matching:
            raise ValueError("row %d is not a MATCHING row" % i)
        import_review.accept_match(self.conn, entry)
        txn_id = entry.matched_txn_id
        self.transactionSaved.emit()
        # Refresh the register FIRST (its reload resets the view); only then
        # advance, so a next NEW row's freshly embedded Accept button survives.
        self.changed.emit()
        self._remove_and_advance(i, txn_id=txn_id)
        return txn_id

    def accept_amazon_index(self, i: int) -> int:
        """Accept an Amazon itemization row at ``i``, then drop it and advance.

        A MATCHING row REPLACES its existing register line's splits/categories
        with the invoice's per-item split (requirement A8); a NEW row posts a
        fresh card charge carrying that split. Both write through the Amazon
        accept path in :mod:`import_review` (``ledger.set_splits`` /
        ``ledger.add_transaction``) -- no second write path. Amazon rows are never
        persisted to ``review_items``, so acceptance always removes the row."""
        entry = self._entries[i]
        if getattr(entry, "amazon_alloc", None) is None:
            raise ValueError("row %d carries no Amazon allocation" % i)
        if entry.is_matching:
            txn_id = import_review.accept_amazon_match(self.conn, entry)
        else:
            txn_id = import_review.accept_amazon_new(
                self.conn, self.account_id, entry)
        self.transactionSaved.emit()
        # Refresh the register FIRST (its reload resets the view); only then drop.
        self.changed.emit()
        self._remove_and_advance(i, txn_id=txn_id, drop=True)
        return txn_id

    def discard_index(self, i: int) -> None:
        """Discard the pending row at ``i`` without adding it to the register.
        A persisted row is DELETED, so downloading the same range again brings it
        back -- discard means "not now", not "never again". The row is then
        dropped from the list and the selection advances. An already-actioned row
        is left alone."""
        if i < 0 or i >= len(self._entries) or self._states[i].done:
            return
        entry = self._entries[i]
        import_review.discard_one(self.conn, getattr(entry, "review_id", None))
        self._remove_and_advance(i, state="discarded", drop=True)
        self.changed.emit()

    def accept_new(self, entry, values: dict) -> int:
        """Commit a NEW ``entry`` into the register using the fields the user
        edited in the register's pending row (``values``: date, payee, category,
        memo, amount_cents), then drop it and advance. Returns the new txn id.

        The edited date/amount replace the provisional mapped values so the
        single :func:`import_review.save_new` chokepoint still does the write."""
        try:
            i = self._entries.index(entry)
        except ValueError:
            return -1
        if not entry.is_new:
            raise ValueError("entry is not a NEW row")
        m = entry.mapped
        # The user's edits from the pending row are passed to save_new as
        # ARGUMENTS, never written onto ``m``. That object is the review row's
        # ground truth -- what the bank sent -- and overwriting it made the row,
        # once grayed, display the edit instead of the source, disagreeing with
        # its own stored row until a reload silently put it back.
        edited_date = values.get("date") or None
        edited_amount = values.get("amount_cents")
        edited_num = values.get("num")
        cat_text = values.get("category", "")
        # A '[Account]' category names a transfer target: resolve it to an
        # account id so save_new creates the double-entry (and learns the
        # statement text -> account mapping). Otherwise it is a plain category.
        # Transfer detection takes precedence over category creation; only when
        # the text is neither a transfer target nor blank is a category resolved.
        transfer_account_id = import_review.transfer_account_id_for_name(
            self.conn, cat_text)
        # Accept is the commit point: a category the user typed and confirmed
        # here must be CREATED and assigned, not silently dropped. The old
        # lookup-only resolve returned None for a brand-new name, so the register
        # posted the row with no category and the user had to re-add it by hand.
        # resolve_or_create_category routes through ledger.resolve_category (the
        # sole writer of category rows), so this stays the single write path.
        category_id = (None if transfer_account_id is not None
                       else import_review.resolve_or_create_category(
                           self.conn, cat_text))
        txn_id = import_review.save_new(
            self.conn, self.account_id, m,
            payee=values.get("payee"), category_id=category_id,
            transfer_account_id=transfer_account_id,
            memo=values.get("memo"), review_id=entry.review_id,
            date=edited_date, amount_cents=edited_amount, num=edited_num,
            # Investment corrections from the pending row: action and security
            # are the importer's guesses, and shares/price are what the derived
            # price rests on.
            action=values.get("action"), symbol=values.get("symbol"),
            quantity=values.get("quantity"), price=values.get("price"))
        self.transactionSaved.emit()
        # Refresh the register FIRST (its reload resets the view); only then
        # advance, so a next NEW row's freshly embedded Accept button survives.
        self.changed.emit()
        self._remove_and_advance(i, txn_id=txn_id)
        return txn_id

    # ---- button slots -----------------------------------------------------
    def _on_accept(self):
        entry = self.current_entry()
        if entry is None:
            return
        # An already-actioned row is history, not work. Accept did not check,
        # so pressing it on a grayed row committed the SAME import row a second
        # time -- a duplicate transaction the register had no way to explain.
        # discard_index has always guarded this; accept did not.
        if getattr(entry, "is_actioned", False):
            return
        # An Amazon itemization row (transient, carries its own multi-leg split)
        # is accepted through the Amazon split writer whether NEW or MATCHING --
        # never through the single-category NEW pending-row path.
        if getattr(entry, "amazon_alloc", None) is not None:
            self.accept_amazon_index(self._selected_index())
        elif entry.is_matching:
            self.accept_index(self._selected_index())
        else:
            # NEW rows -- cash AND investment -- are committed from the
            # register's editable pending row, which owns the user's corrections.
            self.accept_new_requested.emit(entry)

    # ---- bulk operations --------------------------------------------------
    def _has_amazon_pending(self) -> bool:
        """Whether the in-memory list holds any un-actioned Amazon itemization
        row. Such rows are transient (never in ``review_items``), so the DB-based
        bulk helpers cannot see them."""
        return any(getattr(e, "amazon_alloc", None) is not None
                   and not getattr(e, "is_actioned", False)
                   for e in self._entries)

    def _on_accept_all(self):
        # Transient Amazon rows live only in memory, so accept_all (which reads
        # review_items) cannot see them -- accept each in place through the Amazon
        # split writer instead. accept_amazon_index drops the row it accepts, so
        # re-scan from the front each pass.
        if self._has_amazon_pending():
            while True:
                i = next((j for j, e in enumerate(self._entries)
                          if getattr(e, "amazon_alloc", None) is not None
                          and not getattr(e, "is_actioned", False)), None)
                if i is None:
                    break
                self.accept_amazon_index(i)
            return
        import_review.accept_all(self.conn, self.account_id)
        self.reload_pending()
        self.changed.emit()

    def _on_discard_all(self):
        # Transient Amazon rows aren't in review_items: discarding them just
        # clears the in-memory list (the file can be re-loaded to bring them
        # back), so there is nothing to delete and no confirmation to ask.
        if self._has_amazon_pending():
            self.set_entries([])
            self.hide()
            self.row_selected.emit(None)
            return
        if import_review.count_pending(self.conn, self.account_id) == 0:
            return
        resp = QMessageBox.question(
            self, "Discard All",
            "Discard all pending review rows? They will NOT be added to the "
            "register. They are removed, not hidden, so downloading the same "
            "date range again brings them back.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if resp != QMessageBox.Yes:
            return
        import_review.discard_all(self.conn, self.account_id)
        self.reload_pending()
        self.changed.emit()

    def _on_undo_all_matches(self):
        import_review.undo_all_matches(self.conn, self.account_id)
        self.reload_pending()
        self.changed.emit()

    # ---- manual match -----------------------------------------------------
    def _on_context_menu(self, pos):
        """Right-click a row -> Manual Match / Unmatch / Delete.

        An ALREADY-ACTIONED row still gets a menu when it is a MATCHING row:
        undoing one wrong match used to mean "Undo All Matches", tearing down
        every correct one alongside it (by request)."""
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        i = index.row()
        if i < 0 or i >= len(self._entries):
            return
        entry = self._entries[i]
        done = self._states[i].done
        matched = bool(getattr(entry, "is_matching", False))
        if done and not matched:
            return                       # accepted NEW row: nothing to offer
        menu = QMenu(self)
        match_act = unmatch_act = delete_act = None
        if not done:
            match_act = menu.addAction("Manual Match…")
        if matched:
            unmatch_act = menu.addAction("Unmatch")
        if not done:
            delete_act = menu.addAction("Delete Row")
        if menu.isEmpty():
            return
        chosen = menu.exec_(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is match_act:
            self.manual_match_index(i)
        elif chosen is unmatch_act:
            self.unmatch_index(i)
        elif chosen is delete_act:
            self.discard_index(i)

    def unmatch_index(self, i: int) -> None:
        """Break row ``i``'s match and return it to pending NEW.

        Restores the register line's prior fitid/cleared/reconciled when the row
        had been accepted, so undoing one match leaves the ledger exactly as
        "Undo All Matches" would have for that single row."""
        if i < 0 or i >= len(self._entries):
            return
        entry = self._entries[i]
        if not import_review.unmatch_one(
                self.conn, getattr(entry, "review_id", None)):
            return
        self.reload_pending()
        self.changed.emit()

    def manual_match_index(self, i: int, *, chosen_id: Optional[int] = None) -> None:
        """Hand-pick the existing register line row ``i`` matches. When
        ``chosen_id`` is given the dialog is skipped (tests call it directly)."""
        entry = self._entries[i]
        if self._states[i].done:
            return
        if chosen_id is None:
            candidates = import_review.manual_match_candidates(
                self.conn, self.account_id, entry.mapped)
            dlg = ManualMatchDialog(candidates, self,
                                    amount_cents=entry.mapped.amount_cents)
            if dlg.exec_() != QDialog.Accepted:
                return
            chosen = dlg.selected_candidate()
            if chosen is None:
                return
            chosen_id = chosen["id"]
            chosen_date = chosen["date"]
        else:
            chosen_date = self._txn_date(chosen_id)
        offset = _date_offset_days(entry.mapped.date, chosen_date)
        if offset is not None and offset > 3:
            QMessageBox.warning(
                self, "Date offset",
                f"The chosen transaction's date ({chosen_date}) is {offset} day(s) "
                f"from the downloaded row ({entry.mapped.date}). Matching anyway.")
        import_review.set_manual_match(self.conn, entry, chosen_id)
        self._render_row(i)
        self._update_title()
        self._sync_buttons()
        self.changed.emit()

    def _txn_date(self, txn_id) -> str:
        row = self.conn.execute(
            "SELECT date FROM transactions WHERE id=?", (txn_id,)).fetchone()
        return (row["date"] if row is not None else "") or ""


class ManualMatchDialog(QDialog):
    """Pick the existing register transaction a reviewed row corresponds to.

    Shows the wide-window candidates from
    :func:`import_review.manual_match_candidates`; the caller reads
    :meth:`selected_candidate` on accept.

    A DIFFERENCE column is shown when the downloaded amount is supplied.
    Candidates no longer have to match to the cent -- a scheduled payment whose
    escrow moved is exactly the row this dialog exists to find -- so "$1,200.00"
    on its own no longer tells the user whether they are looking at the payment
    or at something else that month. The delta does."""

    def __init__(self, candidates, parent=None, *, amount_cents=None):
        super().__init__(parent)
        self.setWindowTitle("Manual Match")
        self._candidates = list(candidates)
        self._amount_cents = amount_cents

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Select the existing register transaction this downloaded row matches:"))
        show_diff = amount_cents is not None
        self.table = QTableWidget(len(self._candidates), 4 if show_diff else 3)
        self.table.setHorizontalHeaderLabels(
            ["Date", "Payee", "Amount"] + (["Difference"] if show_diff else []))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for r, c in enumerate(self._candidates):
            self.table.setItem(r, 0, QTableWidgetItem(fmt_date(c.get("date") or "")))
            self.table.setItem(r, 1, QTableWidgetItem(c.get("payee") or ""))
            amt = QTableWidgetItem(_fmt_amount(c.get("amount") or 0))
            amt.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, 2, amt)
            if show_diff:
                delta = int(c.get("amount") or 0) - int(amount_cents)
                cell = QTableWidgetItem("--" if delta == 0
                                        else _fmt_amount(delta))
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, 3, cell)
        lay.addWidget(self.table)
        if self._candidates:
            self.table.selectRow(0)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def selected_candidate(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self._candidates[rows[0].row()]

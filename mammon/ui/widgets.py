"""Widgets that assemble the register model + delegates into a usable app:
a per-account register, an accounts overview, edit dialogs, and the main
tabbed window. All persistence goes through the models -> mammon.ledger.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from PyQt5.QtCore import QDate, QEvent, Qt, QTimer, pyqtSignal
try:                                       # PyQt5 >= 5.11 ships sip as a submodule
    from PyQt5 import sip
except ImportError:                        # older PyQt5 exposes a top-level module
    import sip
from PyQt5.QtGui import QBrush, QColor, QFont, QFontMetrics, QKeySequence
from PyQt5.QtWidgets import (
    QAbstractItemDelegate, QAbstractItemView, QActionGroup, QApplication,
    QCheckBox, QColorDialog,
    QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFontComboBox, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QStackedWidget, QStyle, QTableView, QTableWidget,
    QListWidget, QListWidgetItem,
    QTableWidgetItem, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget,
)

# The gear menu is built through this alias, not the module-global ``QMenu``.
# Tests monkeypatch ``widgets.QMenu`` to intercept the RIGHT-CLICK context menu
# (a deliberate seam), and a register built while that patch is active would
# otherwise hand a stub object to QToolButton.setMenu and take the app down.
# The context menu keeps the patchable name; structural chrome does not need it.
_GearMenu = QMenu

from mammon import (backup, categorize, crypto, db, downloads, import_review,
                    investments, ledger, loans, scheduled)
from mammon import webslinger as webslinger_mod
from mammon.ui import prefs, sounds, style
from mammon.ui.import_review_widget import ImportReviewPanel
from mammon.ui.delegates import (
    CategoryDelegate, DateDelegate, MoneyDelegate, NoWheelComboBox,
    NoWheelDoubleSpinBox, PayeeCompleter, PayeeTwoLineDelegate, SplitAmountSpinBox,
    TagDelegate, TwoLineHeaderView, _accept_active_completion, accept_category_text,
    date_edit_iso, make_category_combo, make_date_edit, refresh_date_format,
)
from mammon.ui.models import (
    AccountsModel, CryptoRegisterModel, InvestmentRegisterModel, RegisterFilter,
    RegisterModel, SearchResultsModel, fmt_cents, fmt_date, fmt_money, fmt_qty,
    parse_amount,
)

# classic account groupings (account type -> section box)
#
# Credit cards get their OWN heading rather than sitting unlabelled at the foot
# of Banking. Quicken separates them only by ordering, which leaves the boundary
# implicit; naming it costs one header row and makes "where are my cards" a
# glance instead of a scan. The order still matches Quicken's -- cards directly
# below the bank accounts.
def _app_icon():
    """The application icon, or None when the icon files are absent. Never
    fatal: a missing icon is cosmetic, and must not stop the app opening."""
    try:
        from mammon.ui.icons import app_icon
        return app_icon()
    except Exception:                        # pragma: no cover - defensive
        return None


_BAR_GROUPS = [
    ("Banking", ("checking", "savings", "cash")),
    ("Credit Card", ("credit",)),
    ("Investing", ledger.INVESTMENT_LIKE_TYPES),
    ("Property & Debt", ("asset", "liability")),
]

_ACCOUNT_TYPES = ["checking", "savings", "credit", "cash",
                  "investment", "asset", "liability"]


def _text_width(fm: QFontMetrics, text: str) -> int:
    """Pixel advance of ``text`` under font-metrics ``fm``, across PyQt5
    versions (``horizontalAdvance`` is Qt >= 5.11; ``width`` is the older API)."""
    try:
        return fm.horizontalAdvance(text)
    except AttributeError:  # pragma: no cover - very old Qt
        return fm.width(text)


def _row_get(row, key, default=None):
    """Read ``key`` from a sqlite3.Row or a plain dict, tolerating a column that
    a pre-migration row (or a hand-built test dict) simply doesn't carry. NULL
    reads back as ``default`` so callers can `or ""` freely."""
    try:
        val = row[key]
    except (IndexError, KeyError):
        return default
    return default if val is None else val


# ---------------------------------------------------------------------------
# transaction details dialog (covers tags + transfers)
# ---------------------------------------------------------------------------
class TransactionDialog(QDialog):
    """Full add/edit form. Complements inline editing by exposing every
    field at once and an explicit Payment/Deposit split."""

    def __init__(self, model: RegisterModel, row=None, parent=None):
        super().__init__(parent)
        self.model = model
        self.row = row
        editing = row is not None
        self.setWindowTitle("Edit transaction" if editing else "New transaction")

        form = QFormLayout(self)
        # A real date field: typeable AND calendar-pickable, in the user's chosen
        # format. It was free text that only accepted ISO, so with the preference
        # set to MM/DD/YYYY the register showed one format and this dialog
        # demanded another.
        self.date = make_date_edit()
        self.num = QLineEdit()
        self.payee = QLineEdit()
        # QuickFill, same as the register's payee cell: complete the payee from
        # the register's own history and, for a NEW transaction, pre-enter its
        # last category/memo/tag/amount into the fields still blank when the
        # payee field is left.
        self.payee.setCompleter(PayeeCompleter(model.payee_choices(), self.payee))
        if not editing:
            self.payee.editingFinished.connect(self._quickfill)
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.addItem("")
        self.category.addItems(model.category_choices())
        self.tag = QLineEdit()
        self.memo = QLineEdit()
        self.payment = QLineEdit()
        self.deposit = QLineEdit()
        self.cleared = QCheckBox("Cleared")

        form.addRow("Date", self.date)
        form.addRow("Num", self.num)
        form.addRow("Payee", self.payee)
        form.addRow("Category", self.category)
        form.addRow("Tag", self.tag)
        form.addRow("Memo", self.memo)
        form.addRow("Payment", self.payment)
        form.addRow("Deposit", self.deposit)
        form.addRow("", self.cleared)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        if editing:
            self._load(model.txn_at(row))

    def _quickfill(self) -> None:
        """Pre-enter the payee's remembered fields into whatever is still blank.
        Never overwrites: a category, memo, tag or amount the user already typed
        stands, and a typed amount in either money column suppresses the
        remembered one so a guessed deposit cannot net against a typed payment."""
        fill = self.model.quickfill_fields(self.payee.text())
        if not fill:
            return
        if not self.category.currentText().strip() and fill.get("category"):
            self.category.setEditText(fill["category"])
        for field, key in ((self.memo, "memo"), (self.tag, "tag")):
            if not field.text().strip() and fill.get(key):
                field.setText(fill[key])
        if not (self.payment.text().strip() or self.deposit.text().strip()):
            for field, key in ((self.payment, "payment"), (self.deposit, "deposit")):
                if fill.get(key):
                    field.setText(fill[key])

    def _load(self, txn):
        if not txn:
            return
        _set_date_edit(self.date, txn["date"])
        self.num.setText(txn["num"] or "")
        self.payee.setText(txn["payee"] or "")
        idx = self.category.findText(txn["category_label"])
        if idx >= 0:
            self.category.setCurrentIndex(idx)
        else:
            self.category.setEditText(txn["category_label"])
        self.tag.setText(txn["tag"] or "")
        self.memo.setText(txn["memo"] or "")
        amount = txn["amount"]
        if amount < 0:
            self.payment.setText(fmt_cents(-amount))
        elif amount > 0:
            self.deposit.setText(fmt_cents(amount))
        self.cleared.setChecked(bool(txn["cleared"]))
        # A transfer's Category cell names the linked account. A plain two-sided
        # transfer can be re-pointed here (and its payee edited -- both mirror to
        # the pair); only a split-transfer or a one-sided mirror leg (no pair to
        # move) keeps payee/category locked.
        if txn["transfer_account_id"] is not None:
            retargetable = (not txn.get("is_split")
                            and txn["transfer_pair_id"] is not None)
            if not retargetable:
                self.payee.setEnabled(False)
                self.category.setEnabled(False)

    def values(self) -> dict:
        return {
            "date": date_edit_iso(self.date),
            "num": self.num.text().strip(),
            "payee": self.payee.text().strip(),
            "category": self.category.currentText().strip(),
            "tag": self.tag.text().strip(),
            "memo": self.memo.text().strip(),
            "payment": self.payment.text().strip(),
            "deposit": self.deposit.text().strip(),
            "cleared": 1 if self.cleared.isChecked() else 0,
        }


# ---------------------------------------------------------------------------
# register widget (table view + toolbar)
# ---------------------------------------------------------------------------
class RegisterWidget(QWidget):
    """A per-account register: the nine Quicken columns, inline editing, a
    blank quick-entry row, delegates, and a small toolbar."""

    changed = pyqtSignal()  # forwarded from the model when the DB is written

    def __init__(self, conn, account_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = account_id
        self.model = RegisterModel(conn, account_id)
        self.model.error.connect(self._on_error)
        # The ka-ching hangs off transactionSaved, NOT committed: committed also
        # fires for a write that changed nothing, and a chime on a stray click is
        # a confirmation you stop hearing.
        self.model.transactionSaved.connect(self._note_transaction_saved)
        self.model.committed.connect(self.changed)
        self.model.committed.connect(self._refresh_header)
        # Every inline commit reloads the model via begin/endResetModel, which
        # otherwise clears the selection and snaps the view to the top. Capture
        # the selected transaction + scroll offset before each reset, then
        # restore them on `committed` (fires right AFTER reload, so after the
        # view has processed the reset and cleared its current index) so pressing
        # Enter to save keeps the row selected and in view.
        self._saved_txn_id = None
        self._saved_scroll = 0
        self.model.modelAboutToBeReset.connect(self._remember_position)
        self.model.committed.connect(self._restore_position)

        layout = QVBoxLayout(self)
        # Match the left account bar's 8px top/side inset so the title box top
        # lines up flush with the top of the first account group box.
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        # The account title lives in a bordered box spanning the register width
        # (by request); the box is styled via QFrame#registerTitleBox.
        self.header_box = QFrame()
        self.header_box.setObjectName("registerTitleBox")
        header_layout = QHBoxLayout(self.header_box)
        header_layout.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel()
        self.header.setObjectName("registerTitle")
        header_layout.addWidget(self.header)
        header_layout.addStretch(1)

        # The account-page actions live in a GEAR menu at the right of the title
        # line rather than on a toolbar row of their own. Every one of them opens
        # a dialog -- they all carried a trailing "…" -- and a row of buttons that
        # only lead elsewhere is a poor trade for a full row of register height.
        # Quicken puts exactly this class of command behind a gear beside the
        # account name; the register is the thing worth the vertical space.
        #
        # AccountToolbar still OWNS the actions (it wires each one's account-id
        # signal); it is simply never shown. QActions live in as many widgets as
        # you like, so the menu displays the same objects the rest of the code --
        # and the tests -- reach through `self.toolbar.act_*`.
        self.toolbar = AccountToolbar(account_id, self)
        self.toolbar.setVisible(False)
        self.gear_menu = _GearMenu(self)
        for act in self.toolbar.actions():
            self.gear_menu.addAction(act)
        # The loan actions reflect the ledger at the moment the gear opens.
        self.gear_menu.aboutToShow.connect(self._sync_loan_actions)
        self.gear_menu.addSeparator()
        self._build_view_mode_actions(self.gear_menu)
        # Filter Register (parity): a bar above the rows narrowing them by text,
        # date range, amount range and cleared state. Hidden by default; hiding
        # it again clears the filter, so the register is never silently narrowed
        # behind a closed bar.
        self.gear_menu.addSeparator()
        self.act_filter = self.gear_menu.addAction("Filter Register")
        self.act_filter.setCheckable(True)
        self.act_filter.setShortcut(Qt.CTRL + Qt.SHIFT + Qt.Key_F)
        self.act_filter.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.act_filter.toggled.connect(self.set_filter_visible)
        self.addAction(self.act_filter)
        # A property's market value is NOT a transaction -- it is a dated series,
        # exactly like a security's closing price -- so it is not in the register
        # and these are its two doors: the investment register's "Get Quotes…"
        # and its price-history chart, for a thing you own one of. Shown only on
        # asset accounts (_sync_asset_actions), where the register's running
        # balance is a cost basis and says nothing about what the thing is worth.
        self.gear_menu.addSeparator()
        self.act_get_value = self.gear_menu.addAction("Get Value…")
        self.act_get_value.setToolTip(
            "Look this property's current value up from its address and record "
            "it in the value history.")
        self.act_get_value.triggered.connect(self.get_value)
        self.act_value_history = self.gear_menu.addAction("Value History…")
        self.act_value_history.setToolTip(
            "What this property has been worth over time -- add past appraisals, "
            "edit a value, or chart it against what it cost.")
        self.act_value_history.triggered.connect(self.value_history)
        self.gear_menu.aboutToShow.connect(self._sync_asset_actions)
        self._sync_asset_actions()
        self.gear_button = QToolButton()
        self.gear_button.setObjectName("registerGear")
        self.gear_button.setText("⚙")
        self.gear_button.setToolTip("Account actions")
        self.gear_button.setAutoRaise(True)
        self.gear_button.setPopupMode(QToolButton.InstantPopup)
        self.gear_button.setMenu(self.gear_menu)
        header_layout.addWidget(self.gear_button)
        layout.addWidget(self.header_box)

        self.filter_bar = self._build_filter_bar()
        self.filter_bar.setVisible(False)
        layout.addWidget(self.filter_bar)

        self.view = QTableView()
        self.view.setModel(self.model)
        # Custom header: in two-line mode it boxes Category/Memo/Tag beneath the
        # Payee label, aligned above each row's second-line boxes.
        self.header_view = TwoLineHeaderView(Qt.Horizontal, self.view)
        self.view.setHorizontalHeader(self.header_view)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Several rows can be selected (Shift/Ctrl-click) for the batch edits
        # on the context menu; single-row gestures are unchanged.
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # A SINGLE click enters edit mode immediately (see _on_cell_clicked ->
        # self.view.edit), so the mouse edit triggers are OFF here to avoid a
        # competing SelectedClicked "second click" edit; the keyboard triggers
        # (type/F2 to edit) stay. A double-click on the Category column is
        # reserved for "open the split" (see _on_row_double_clicked), and the
        # Category single-click edit is deferred so that gesture wins.
        self.view.setEditTriggers(
            QAbstractItemView.EditKeyPressed | QAbstractItemView.AnyKeyPressed)
        self.view.setItemDelegateForColumn(RegisterModel.DATE, DateDelegate(self.view))
        self.view.setItemDelegateForColumn(RegisterModel.CATEGORY, CategoryDelegate(self.view))
        # One-line Tag cell: a colored square before each tag name (identity color
        # from ledger.tag_colors, incl. the split-leg union). In two-line mode the
        # Tag column is hidden and PayeeTwoLineDelegate paints the same chips.
        self.view.setItemDelegateForColumn(RegisterModel.TAG, TagDelegate(self.view))
        # Money columns need an editor that writes only on a real change: the
        # default one commits on focus-out even when untouched, and the empty half
        # of the Payment/Deposit pair then committed "" -> amount 0.
        for _money_col in (RegisterModel.PAYMENT, RegisterModel.DEPOSIT):
            self.view.setItemDelegateForColumn(_money_col, MoneyDelegate(self.view))
        # Payee delegate paints the two-line row's classification second line.
        self.payee_delegate = PayeeTwoLineDelegate(self.view)
        self.view.setItemDelegateForColumn(RegisterModel.PAYEE, self.payee_delegate)
        # Enter inside ANY cell editor of the blank quick-entry row records the
        # row (Quicken's gesture; the way a QuickFilled amount gets recorded
        # without retyping it). A delegate reports an Enter-driven close through
        # closeEditor's SubmitModelCache hint -- and it does so for editable
        # combos too, which swallow the key before the view ever sees it -- so
        # that hint, not the key event, is the hook. See _on_editor_closed.
        _seen = set()
        for _d in [self.view.itemDelegate()] + [
                self.view.itemDelegateForColumn(c) for c in range(len(RegisterModel.HEADERS))]:
            if _d is not None and id(_d) not in _seen:
                _seen.add(id(_d))
                _d.closeEditor.connect(self._on_editor_closed)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        # A single click acts immediately: the Clr column toggles cleared and
        # every other field enters inline edit (see _on_cell_clicked).
        self.view.clicked.connect(self._on_cell_clicked)
        # A double-click on the Category column opens its split editor, mirroring
        # the right-click "Split…" action (by request); other columns keep
        # their single-click edit and do nothing extra on double-click.
        self.view.doubleClicked.connect(self._on_row_double_clicked)
        # The Category single-click edit is deferred by this timer so a genuine
        # double-click can open the split instead; _on_row_double_clicked stops
        # it. Holds the (row, col) awaiting the deferred edit, or None.
        self._pending_edit = None
        self._edit_timer = QTimer(self)
        self._edit_timer.setSingleShot(True)
        self._edit_timer.timeout.connect(self._begin_pending_edit)
        self._configure_columns()
        # Header click sorts (parity). The header flips its own indicator on a
        # click (sections are clickable); the model re-projects its rows to
        # match. Wired by hand rather than setSortingEnabled so an open cell
        # editor is committed BEFORE the reset that sorting causes, and so the
        # indicator starts on Date ascending -- the ledger order -- without a
        # spurious first sort.
        hh = self.view.horizontalHeader()
        hh.setSortIndicatorShown(True)
        hh.setSortIndicator(RegisterModel.DATE, Qt.AscendingOrder)
        hh.sortIndicatorChanged.connect(self._on_sort_changed)
        # Right-click on the header: the column chooser (parity).
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._header_menu)
        # one-line by default; MainWindow applies the persisted preference.
        self.view_mode = "one"
        self._one_line_row_h = self.view.verticalHeader().defaultSectionSize()
        # Pristine default row height -- the FLOOR for one-line rows, so a larger
        # display-preference font can grow them while the default look is unchanged.
        self._base_row_h = self._one_line_row_h
        self._one_line_header_h = max(
            self.view.horizontalHeader().sizeHint().height(),
            self.view.fontMetrics().height() + 6)
        layout.addWidget(self.view)

        # The import-review list sits below the register and its button row
        # (added to the layout after the buttons, further down): a download
        # populates it (NEW vs MATCHING) and nothing enters the register until a
        # row is accepted/saved here. Created hidden; shown after a download or
        # via the toolbar's Review… action. Its commits write the DB, so refresh
        # this register (and re-sync the Review… button) on change.
        from .import_review_widget import ImportReviewPanel
        self.review_panel = ImportReviewPanel(conn, account_id, self)
        self.review_panel.changed.connect(self.model.reload)
        self.review_panel.changed.connect(self._refresh_header)
        self.review_panel.changed.connect(self.changed)
        self.review_panel.changed.connect(self._sync_review_action)
        # Selecting a review row drives the register: a MATCHING row highlights +
        # scrolls its existing line into view; a NEW row opens an editable pending
        # register row (accepted via its inline Accept button or the Enter key).
        self.review_panel.row_selected.connect(self._on_review_row_selected)
        # Auto-advance removes rows without re-firing `changed`, so re-sync the
        # Review… action's enabled state on every selection change too (it fires
        # with None when the list empties).
        self.review_panel.row_selected.connect(self._sync_review_action)
        self.review_panel.accept_new_requested.connect(
            lambda _entry: self._accept_pending())
        # Accepting from the review list is the high-volume save gesture, and the
        # one place the confirmation sound is most worth having.
        self.review_panel.transactionSaved.connect(self._play_accepted)
        # A Num edited in the review list mirrors into the open pending row so
        # the (later) accept, which reads the pending buffer, keeps the edit.
        self.review_panel.num_edited.connect(self._on_review_num_edited)
        # Changing how much history to show reloads the list from the DB:
        # the extra rows are accepted/discarded ones the panel never held.
        self.review_panel.visibility_changed.connect(self._reload_review)
        # The Accept button embedded at the end of the pending register row; it
        # and the Enter-key handler both commit the pending row.
        self._accept_btn = None
        # Re-entrancy guard: acceptance must be idempotent so a second trigger
        # (button click AND Enter, or two Enters) cannot double-tear-down a row.
        self._accepting = False
        # The transaction whose edits have not been acknowledged yet (see
        # _note_transaction_saved).
        self._pending_sound_txn = None
        self.view.installEventFilter(self)
        self.view.selectionModel().currentRowChanged.connect(
            lambda *_: self._flush_transaction_sound())

        bar = QHBoxLayout()
        for text, slot in (("New…", self.on_new),
                           ("Edit…", self.on_edit),
                           ("Split…", self.on_split),
                           ("Delete", self.on_delete),
                           ("Toggle Cleared", self.on_toggle)):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            bar.addWidget(btn)
        bar.addStretch()
        self.balance_label = QLabel()
        bar.addWidget(self.balance_label)
        layout.addLayout(bar)

        # The import-review list sits BELOW the New/Edit/Split/Delete button row
        # (by request), not wedged between the register and its buttons.
        layout.addWidget(self.review_panel)

        self._refresh_header()
        self._sync_review_action()
        # Open in the layout THIS account chose (falling back to the global
        # default). Applied here rather than only in MainWindow.open_register so
        # the widget is self-consistent however it is constructed -- otherwise a
        # register built directly ignored the account's own preference.
        self.set_view_mode(prefs.account_view_mode(account_id))

    def _note_transaction_saved(self, txn_id: int = 0) -> None:
        """Remember that a transaction changed; sound it once the user is DONE
        with that transaction.

        The register saves per field -- each cell writes as its editor closes --
        so tabbing across four fields is four writes to one transaction. Sounding
        each would turn a confirmation into a rattle. The unit the user works in
        is the transaction: change what you like, then press Enter (or move to
        another row) and hear it once.

        A brand-new row commits as a whole and arrives with id 0, so it sounds
        immediately -- there is nothing to coalesce.
        """
        if not txn_id:
            self._pending_sound_txn = None
            self._play_accepted()
            return
        if (self._pending_sound_txn is not None
                and txn_id != self._pending_sound_txn):
            self._play_accepted()          # the previous transaction is finished
        self._pending_sound_txn = txn_id

    def _flush_transaction_sound(self) -> None:
        """Sound any transaction the user has finished editing."""
        if self._pending_sound_txn is not None:
            self._pending_sound_txn = None
            self._play_accepted()

    def _play_accepted(self) -> None:
        """Sound the transaction-accepted chime, if it is switched on."""
        sounds.play_accepted(prefs.sound_enabled())

    def scroll_to_newest(self) -> None:
        """Scroll the register so its newest row (the blank quick-entry line at
        the bottom) is visible. Called once per account per session on first
        open, so a freshly opened register lands on the latest activity rather
        than the oldest (by request)."""
        self.view.scrollToBottom()

    # ---- import review ----------------------------------------------------
    def _reload_review(self, _mode=None) -> None:
        """Re-query the review list under the panel's current visibility."""
        entries = import_review.load_review(
            self.conn, self.account_id, prefs.review_visibility(self.account_id))
        self.review_panel.set_entries(entries)

    def show_review(self, entries):
        """Load ``entries`` (from import_review.build_review) into the review
        panel, reveal it, and enable the toolbar's Review… action. An empty
        list clears any prior review without showing the panel."""
        self.review_panel.set_entries(entries)
        if entries:
            self.review_panel.show()
            self._on_review_row_selected(self.review_panel.current_entry())
        else:
            self.review_panel.hide()
            self._end_pending()
        self._sync_review_action()

    def reopen_review(self):
        """Re-show a pending review list (toolbar Review… action).

        Reloads from the persisted ``review_items`` under THIS ACCOUNT'S saved
        visibility, so re-opening shows the same mix of pending and greyed
        history the toggle is set to -- survives restarts and reflects any bulk
        action. Loading pending-only here regardless of the setting made the
        panel disagree with its own toggle until the user flipped it."""
        self.review_panel.reload_pending()
        if not self.review_panel.isHidden():
            self._on_review_row_selected(self.review_panel.current_entry())
        else:
            self._end_pending()
        self._sync_review_action()

    # ---- review-driven register interaction -------------------------------
    def _on_review_row_selected(self, entry):
        """React to the review list's selection: highlight the matched register
        line (MATCHING or already-actioned) or open an editable pending register
        row (NEW). A cleared selection or a hidden panel tears down any pending
        row."""
        if entry is None or self.review_panel.isHidden():
            self._end_pending()
            return
        # An ALREADY-ACTIONED row is history, not work. It kept its NEW label
        # from when it arrived, so the is_new branch below used to open a fresh
        # editable pending row for a transaction that had already been posted --
        # offering to enter it a second time. Point at what it produced instead.
        if getattr(entry, "is_actioned", False):
            self._end_pending()
            txn_id = (getattr(entry, "accepted_txn_id", None)
                      or entry.matched_txn_id)
            if txn_id is not None:
                self.select_txn(txn_id)
            return
        if entry.is_matching and entry.matched_txn_id is not None:
            self._end_pending()
            self.select_txn(entry.matched_txn_id)
        elif entry.is_new:
            self._show_pending(entry)
        else:
            self._end_pending()

    def _on_review_num_edited(self, entry, text):
        """Mirror a review-list Num edit into the open pending row's Num cell so
        it survives accept (which reads the pending buffer seeded at selection,
        before this edit). No-op unless this entry is the live pending row."""
        if self.model.pending_entry() is not entry:
            return
        row = self.model.pending_row()
        if row >= 0:
            self.model.setData(
                self.model.index(row, RegisterModel.NUM), text, Qt.EditRole)

    def _show_pending(self, entry):
        """Open the editable, not-yet-accepted register row for a NEW review
        ``entry`` and embed an Accept button at the end of the row."""
        self._end_pending()
        self.model.set_pending(entry)
        row = self.model.pending_row()
        if row < 0:
            return
        self._accept_btn = QPushButton("Accept")
        self._accept_btn.setToolTip(
            "Accept this downloaded transaction into the register (or press "
            "Enter while on the row).")
        self._accept_btn.clicked.connect(self._accept_pending)
        self.view.setIndexWidget(
            self.model.index(row, RegisterModel.BALANCE), self._accept_btn)
        idx = self.model.index(row, RegisterModel.PAYEE)
        self.view.setCurrentIndex(idx)
        self.view.selectRow(row)
        self.view.scrollTo(idx, QAbstractItemView.PositionAtCenter)

    def _drop_accept_btn(self):
        """Remove the inline Accept button, tolerating a C++ object that Qt has
        already deleted out from under us (a RegisterModel.reload()'s
        begin/endResetModel drops index widgets, leaving ``_accept_btn`` a
        dangling wrapper). Always clears the Python reference so a later call
        can null-check it."""
        btn, self._accept_btn = self._accept_btn, None
        if btn is not None and not sip.isdeleted(btn):
            btn.deleteLater()

    def _end_pending(self):
        """Discard the editable pending register row without accepting it."""
        self._drop_accept_btn()
        if self.model.has_pending():
            self.model.clear_pending()

    def _accept_pending(self):
        """Commit the pending register row: persist the edited transaction via
        the review panel (single import_review chokepoint), which then drops the
        item and auto-advances to the next review row.

        Idempotent and re-entrancy-safe: a second trigger (Accept click AND
        Enter, or two Enters) is a no-op. Enter's eventFilter can fire this after
        the pending row / Accept button have already been torn down (e.g. by a
        model reset), so we must not touch a stale button or clear twice."""
        if self._accepting or not self.model.has_pending():
            return None
        self._accepting = True
        try:
            entry = self.model.pending_entry()
            values = self.model.pending_values()
            self._drop_accept_btn()
            self.model.clear_pending()
            return self.review_panel.accept_new(entry, values)
        finally:
            self._accepting = False

    def eventFilter(self, obj, event):
        """Enter/Return while the pending review row is current accepts it. Any
        open cell editor is force-committed FIRST so the just-typed value is in
        the model before the accept reads it (commit-on-Enter bug: an editable
        combo swallows Enter, so without this the value reverted unless the user
        clicked another cell)."""
        if (obj is self.view and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and self.model.has_pending()
                and self.model.is_pending_row(self._selected_row())):
            self._commit_open_editor()
            self._accept_pending()
            return True
        # Enter on an EXISTING row is the accept gesture for that transaction:
        # commit whatever cell is open, then acknowledge the transaction once.
        # (The rows themselves are already saved -- the register writes per field
        # -- so this confirms the transaction, it does not perform the write.)
        if (obj is self.view and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)):
            self._commit_open_editor()
            self._flush_transaction_sound()
            # Enter on the blank quick-entry row RECORDS it (QuickFill's accept
            # gesture), deferred a turn so the model reset never runs inside an
            # editor's teardown (the setModelData hazard in CLAUDE.md).
            if self.model.is_blank_row(self._selected_row()):
                QTimer.singleShot(0, self._commit_blank_row)
        # Delete/Backspace on a TAB-selected cell (no editor open) clears it,
        # matching the click path -- where the editor opens with its text
        # selected, so Delete erases it. Keyboard navigation lands on a cell
        # without opening an editor, so Qt's Delete had nothing to act on and the
        # cell appeared "stuck". We clear the field's text directly instead.
        if (obj is self.view and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Delete, Qt.Key_Backspace)
                and self._clear_current_cell()):
            return True
        return super().eventFilter(obj, event)

    # Text columns a bare Delete/Backspace may clear from the keyboard; money and
    # date cells are left to their editors so a stray Delete cannot zero an amount.
    _CLEARABLE_COLS = (RegisterModel.NUM, RegisterModel.PAYEE,
                       RegisterModel.CATEGORY, RegisterModel.TAG, RegisterModel.MEMO)

    def _build_view_mode_actions(self, menu):
        """One Line / Two Lines, checkable, applying to THIS account only.

        The layout that suits a register depends on the account: a card whose
        rows carry long statement descriptions wants the second line, a
        hand-entered checking account does not. Choosing here records the
        account's own preference (ui/prefs.set_account_view_mode); accounts that
        never choose keep following the global default under Settings."""
        group = QActionGroup(self)
        group.setExclusive(True)
        self._view_mode_acts = {}
        for mode, label in (("one", "One Line"), ("two", "Two Lines")):
            act = menu.addAction(label)
            act.setCheckable(True)
            group.addAction(act)
            act.triggered.connect(
                lambda _checked=False, m=mode: self._choose_view_mode(m))
            self._view_mode_acts[mode] = act

    def _choose_view_mode(self, mode: str) -> None:
        """Gear menu: make this account's choice stick and apply it now."""
        prefs.set_account_view_mode(self.account_id, mode)
        self.set_view_mode(mode)

    def _sync_view_mode_actions(self) -> None:
        for mode, act in getattr(self, "_view_mode_acts", {}).items():
            act.setChecked(self.view_mode == mode)

    def _clear_current_cell(self) -> bool:
        """Clear the current cell's text when it is an editable text field and no
        inline editor is open. Returns True when it acted (so the key is
        consumed). No-op on the blank quick-entry row, read-only cells (a
        transfer's category, a split total), and money/date columns."""
        idx = self.view.currentIndex()
        if not idx.isValid() or idx.column() not in self._CLEARABLE_COLS:
            return False
        if self.model.is_blank_row(idx.row()):
            return False
        if not (idx.flags() & Qt.ItemIsEditable):
            return False
        return self.model.setData(idx, "", Qt.EditRole)

    def _commit_open_editor(self) -> None:
        """Force the currently-open inline editor to write its value to the model
        before an Enter-driven save reads it. Qt commits on Tab and on focus-out
        but an editable combo swallows Enter, so without this the just-typed value
        would revert unless the user first clicked another cell."""
        editor = QApplication.focusWidget()
        if editor is None or not self.view.isAncestorOf(editor):
            return
        _accept_active_completion(editor)
        col = self.view.currentIndex().column()
        delegate = self.view.itemDelegateForColumn(col) or self.view.itemDelegate()
        # The view connected these signals when it opened the editor; emitting
        # them runs setModelData (commit) then tears the editor down.
        delegate.commitData.emit(editor)
        delegate.closeEditor.emit(editor)

    def _on_editor_closed(self, editor, hint=QAbstractItemDelegate.NoHint) -> None:
        """A cell editor closed. ``SubmitModelCache`` is the hint every
        QStyledItemDelegate sends for an Enter-driven close (Tab is EditNextItem,
        Escape RevertModelCache, focus-out NoHint), so it is the one signal that
        means "the user pressed Enter in this cell" for line edits, spin boxes
        and combos alike. On the blank quick-entry row that records the row.
        Deferred a turn: the model reset must not run while Qt still holds the
        editor it is tearing down."""
        if hint != QAbstractItemDelegate.SubmitModelCache:
            return
        if self.model.is_blank_row(self._selected_row()):
            QTimer.singleShot(0, self._commit_blank_row)

    def _commit_blank_row(self) -> None:
        """Record the blank row if it is complete (RegisterModel.commit_blank);
        a no-op when it is not, or when a typed amount already committed it."""
        self.model.commit_blank()

    def _remember_position(self) -> None:
        """Snapshot the selected (existing) transaction and vertical scroll
        offset before a model reset, so :meth:`_restore_position` can put the
        view back instead of letting Enter snap it to the top."""
        idx = self.view.currentIndex()
        row = idx.row() if idx.isValid() else -1
        txn = None
        if (row >= 0 and not self.model.is_pending_row(row)
                and not self.model.is_blank_row(row)):
            txn = self.model.txn_at(row)
        self._saved_txn_id = txn["id"] if txn else None
        self._saved_scroll = self.view.verticalScrollBar().value()

    def _restore_position(self) -> None:
        """Re-select the remembered transaction and restore the scroll offset
        after a model reset (keep-focus bug). Review/pending flows set their own
        selection afterwards, so this never fights them (their reset snapshots no
        existing txn)."""
        if self._saved_txn_id is not None:
            row = self.model.row_for_txn(self._saved_txn_id)
            if row is not None and row >= 0:
                idx = self.model.index(row, RegisterModel.PAYEE)
                self.view.setCurrentIndex(idx)
                self.view.selectRow(row)
        self.view.verticalScrollBar().setValue(self._saved_scroll)

    def _sync_review_action(self):
        """Enable Review… only while the panel holds a pending review."""
        act = getattr(self.toolbar, "act_review", None)
        if act is not None:
            act.setEnabled(self.review_panel.has_pending())

    # ---- sort + filter ----------------------------------------------------
    def _on_sort_changed(self, column, order) -> None:
        """Header indicator moved: commit any open editor (the reset would
        orphan it), re-project, then put the selection back on the same
        transaction wherever it landed."""
        self._commit_open_editor()
        self.model.set_sort(column, order)
        self._restore_position()

    def _sync_loan_actions(self) -> None:
        """Gear ▸ aboutToShow: the loan actions reflect the ledger NOW -- Loan
        Setup vs Edit Loan, and Enter Payment gray while a pending pre-entry
        stands -- since both change under an open register (the wizard saves,
        Generate pre-enters, a pending row is accepted or deleted)."""
        from mammon import loans, loans_schedule
        acct = ledger.get_account(self.conn, self.account_id)
        is_liability = acct is not None and acct["type"] == "liability"
        is_loan = bool(is_liability and
                       loans.get_loan_params(self.conn, self.account_id) is not None)
        pending = (loans_schedule.pending_payment(self.conn, self.account_id)
                   if is_loan else None)
        self.toolbar.configure_loan(is_liability, is_loan, pending=pending)

    def _is_asset_account(self) -> bool:
        from mammon import asset_values
        acct = ledger.get_account(self.conn, self.account_id)
        return (acct is not None
                and (acct["type"] or "") in asset_values.VALUABLE_TYPES)

    def _sync_asset_actions(self) -> None:
        """Gear ▸ aboutToShow: the valuation actions appear only on an asset
        account. Re-checked on every open rather than fixed at construction
        because Account Details can change an account's type under an open
        register, and a "Get Value…" left behind on a chequing account would
        offer to look a bank account up on Zillow."""
        asset = self._is_asset_account()
        self.act_get_value.setVisible(asset)
        self.act_value_history.setVisible(asset)

    def _fetch_asset_values(self):
        """Seam: the actual lookup. Overridden by headless tests so no test ever
        reaches the network (the same shape as ``_fetch_quotes``)."""
        from mammon import asset_values
        return asset_values.fetch_values(self.conn, [self.account_id])

    def get_value(self):
        """Gear menu: fetch this property's current market value and record it.

        Nothing is written when the source declines -- the previous value stands
        and the reason is named, because a wrong valuation is far worse than a
        stale one (see :mod:`mammon.asset_values`)."""
        from mammon.ui.asset_value_dialog import run_value_fetch
        if run_value_fetch(self, self._fetch_asset_values,
                           self.model.account_name()):
            self._refresh_header()
            self.changed.emit()

    def value_history(self):
        """Gear menu: the property's value series -- view, backfill an old
        appraisal, edit, delete, or chart it against its cost basis."""
        from mammon.ui.asset_value_dialog import AssetValueHistoryDialog
        dlg = AssetValueHistoryDialog(self.conn, self.account_id, parent=self)
        dlg.changed.connect(self._refresh_header)
        dlg.changed.connect(self.changed)
        dlg.exec_()

    def _build_filter_bar(self):
        bar = QFrame()
        bar.setObjectName("registerFilterBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.filter_text = QLineEdit()
        self.filter_text.setPlaceholderText(
            "Filter payee, memo, num, tag, category or amount…")
        self.filter_text.setClearButtonEnabled(True)
        self.filter_from = make_date_edit(bar, blank_ok=True)
        self.filter_to = make_date_edit(bar, blank_ok=True)
        for edit in (self.filter_from, self.filter_to):
            edit.setDate(edit.minimumDate())      # start BLANK, not at today
        self.filter_min = QLineEdit()
        self.filter_min.setPlaceholderText("min")
        self.filter_min.setFixedWidth(72)
        self.filter_max = QLineEdit()
        self.filter_max.setPlaceholderText("max")
        self.filter_max.setFixedWidth(72)
        self.filter_clr = QComboBox()
        for label, key in (("Any", "any"), ("Uncleared", "uncleared"),
                           ("Cleared", "cleared"), ("Reconciled", "reconciled")):
            self.filter_clr.addItem(label, key)
        clear_btn = QPushButton("Clear")
        clear_btn.setAutoDefault(False)
        clear_btn.clicked.connect(self._clear_filter)
        self.filter_count = QLabel("")
        self.filter_count.setObjectName("registerSub")
        lay.addWidget(QLabel("Filter"))
        lay.addWidget(self.filter_text, 1)
        lay.addWidget(QLabel("From"))
        lay.addWidget(self.filter_from)
        lay.addWidget(QLabel("To"))
        lay.addWidget(self.filter_to)
        lay.addWidget(QLabel("Amount"))
        lay.addWidget(self.filter_min)
        lay.addWidget(QLabel("to"))
        lay.addWidget(self.filter_max)
        lay.addWidget(self.filter_clr)
        lay.addWidget(clear_btn)
        lay.addWidget(self.filter_count)
        self.filter_text.textChanged.connect(self._apply_filter)
        self.filter_from.dateChanged.connect(self._apply_filter)
        self.filter_to.dateChanged.connect(self._apply_filter)
        self.filter_min.editingFinished.connect(self._apply_filter)
        self.filter_max.editingFinished.connect(self._apply_filter)
        self.filter_clr.currentIndexChanged.connect(self._apply_filter)
        return bar

    def current_filter(self) -> RegisterFilter:
        """The filter the bar's controls describe (empty when all are blank)."""
        def cents(text):
            t = (text or "").strip()
            return abs(parse_amount(t)) if t else None
        return RegisterFilter(
            text=self.filter_text.text(),
            date_from=date_edit_iso(self.filter_from),
            date_to=date_edit_iso(self.filter_to),
            amount_min=cents(self.filter_min.text()),
            amount_max=cents(self.filter_max.text()),
            clr=self.filter_clr.currentData() or "any",
        )

    def _apply_filter(self, *_args) -> None:
        if self.filter_bar.isHidden():
            return
        flt = self.current_filter()
        self._commit_open_editor()
        self.model.set_filter(None if flt.is_empty() else flt)
        self._restore_position()
        self._update_filter_count()

    def _clear_filter(self) -> None:
        widgets = (self.filter_text, self.filter_from, self.filter_to,
                   self.filter_min, self.filter_max, self.filter_clr)
        for w in widgets:
            w.blockSignals(True)
        try:
            self.filter_text.clear()
            self.filter_min.clear()
            self.filter_max.clear()
            self.filter_clr.setCurrentIndex(0)
            for edit in (self.filter_from, self.filter_to):
                edit.setDate(edit.minimumDate())   # the blank_ok sentinel
        finally:
            for w in widgets:
                w.blockSignals(False)
        self._commit_open_editor()
        self.model.set_filter(None)
        self._restore_position()
        self._update_filter_count()

    def set_filter_visible(self, on: bool) -> None:
        """Show the filter bar (and focus its text box) or hide it, clearing
        the filter so a closed bar never leaves the register narrowed."""
        on = bool(on)
        self.filter_bar.setVisible(on)
        if hasattr(self, "act_filter") and self.act_filter.isChecked() != on:
            self.act_filter.setChecked(on)
        if on:
            self.filter_text.setFocus()
            self._apply_filter()
        else:
            self._clear_filter()

    def _update_filter_count(self) -> None:
        if self.model.filter_state() is None:
            self.filter_count.setText("")
            return
        shown, total = self.model.view_counts()
        self.filter_count.setText(f"Showing {shown:,} of {total:,}")

    # ---- column chooser ---------------------------------------------------
    # The columns a user may hide. Date, Payee, Category and the money columns
    # are the register; hiding them would leave rows unreadable.
    HIDEABLE_COLUMNS = (RegisterModel.NUM, RegisterModel.TAG, RegisterModel.MEMO,
                        RegisterModel.CLR, RegisterModel.BALANCE)

    def _apply_column_visibility(self) -> None:
        """The one place column visibility is decided: what the layout hides
        (two-line mode collapses Category/Memo/Tag under the payee; a loan
        register drops Num/Tag) plus what the user hid in the column chooser
        (ui/prefs.hidden_columns, global)."""
        R = RegisterModel
        two = getattr(self, "view_mode", "one") == "two"
        loan = self.model.is_loan()
        user_hidden = set(prefs.hidden_columns())
        for col in range(len(R.HEADERS)):
            hide = ((two and col in (R.CATEGORY, R.MEMO, R.TAG))
                    or (loan and col in (R.NUM, R.TAG))
                    or (col in self.HIDEABLE_COLUMNS and R.HEADERS[col] in user_hidden))
            self.view.setColumnHidden(col, hide)

    def user_hidden_columns(self) -> set[int]:
        names = set(prefs.hidden_columns())
        return {c for c in self.HIDEABLE_COLUMNS if RegisterModel.HEADERS[c] in names}

    def set_column_hidden(self, col: int, hidden: bool) -> None:
        """Hide or show one of the hideable columns, remembered across sessions."""
        if col not in self.HIDEABLE_COLUMNS:
            return
        name = RegisterModel.HEADERS[col]
        names = [n for n in prefs.hidden_columns() if n != name]
        if hidden:
            names.append(name)
        prefs.set_hidden_columns(names)
        self._apply_column_visibility()

    def _header_menu(self, pos) -> None:
        menu = _GearMenu(self)
        acts = {}
        hidden = self.user_hidden_columns()
        for col in self.HIDEABLE_COLUMNS:
            act = menu.addAction(RegisterModel.HEADERS[col])
            act.setCheckable(True)
            act.setChecked(col not in hidden)
            acts[act] = col
        menu.addSeparator()
        show_all = menu.addAction("Show All Columns")
        chosen = menu.exec_(self.view.horizontalHeader().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == show_all:
            prefs.set_hidden_columns([])
            self._apply_column_visibility()
            return
        col = acts.get(chosen)
        if col is not None:
            # Qt has already toggled the checkable action by the time exec_ returns.
            self.set_column_hidden(col, not chosen.isChecked())

    # ---- multi-row selection: batch edits and void ------------------------
    def _selected_rows(self) -> list[int]:
        """The selected REAL rows (blank and pending rows excluded), ascending."""
        rows = sorted({i.row() for i in self.view.selectionModel().selectedRows()})
        return [r for r in rows
                if not self.model.is_blank_row(r) and not self.model.is_pending_row(r)]

    def _ask_batch_text(self, title: str, label: str):
        """Seam (tests override): the text a batch edit applies, or None."""
        text, ok = QInputDialog.getText(self, title, label)
        return text if ok else None

    def _ask_batch_category(self, title: str):
        """Seam (tests override): a category path or ``[Account]``, or None."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        lay = QFormLayout(dlg)
        combo = make_category_combo(dlg, self.model.category_choices())
        lay.addRow("Category", combo)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addRow(buttons)
        if dlg.exec_() != QDialog.Accepted:
            return None
        accept_category_text(combo, take_first=True)
        return combo.currentText().strip()

    def _notify(self, title: str, text: str) -> None:
        """Seam (tests override): a non-blocking-in-spirit notice for a batch
        whose rows were partly skipped."""
        QMessageBox.information(self, title, text)

    def _report_batch(self, verb: str, result) -> None:
        changed, skipped = result
        self.last_batch = (verb, changed, skipped)
        if skipped:
            self._notify(
                "Batch edit",
                f"{verb} {changed} transaction(s); {skipped} skipped "
                "(a transfer, a split, or a reconciled row).")

    def _batch_category(self, rows) -> None:
        ids = self.model.txn_ids_at(rows)
        text = self._ask_batch_category(f"Change category for {len(ids)} transactions")
        if not text:
            return
        self._report_batch("Recategorized", self.model.batch_set_category(ids, text))

    def _batch_field(self, rows, field: str, label: str) -> None:
        ids = self.model.txn_ids_at(rows)
        text = self._ask_batch_text(f"Change {label} for {len(ids)} transactions",
                                    f"New {label} (blank clears it):")
        if text is None:
            return
        self._report_batch(f"Changed {label} on", self.model.batch_set_field(ids, field, text))

    def _batch_cleared(self, rows, cleared: bool) -> None:
        ids = self.model.txn_ids_at(rows)
        verb = "Marked cleared" if cleared else "Marked uncleared"
        self._report_batch(verb, self.model.batch_set_cleared(ids, cleared))

    def _void_row(self, row) -> None:
        txn = self.model.txn_at(row)
        if not txn or ledger.is_voided(txn):
            return
        note = " (both sides of the transfer)" if txn["transfer_account_id"] is not None else ""
        if QMessageBox.question(
                self, "Void transaction",
                f"Void the {txn['date']} transaction{note}? Its amount becomes "
                "zero and the row stays as a **VOID** record.",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.model.void_row(row)

    def _void_rows(self, rows) -> None:
        ids = self.model.txn_ids_at(rows)
        if QMessageBox.question(
                self, "Void transactions",
                f"Void {len(ids)} transactions? Their amounts become zero and the "
                "rows stay as **VOID** records.",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._report_batch("Voided", self.model.batch_void(ids))

    def _delete_rows(self, rows) -> None:
        ids = self.model.txn_ids_at(rows)
        if QMessageBox.question(
                self, "Delete transactions",
                f"Delete {len(ids)} transactions (both sides of any transfer)?",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self._report_batch("Deleted", self.model.batch_delete(ids))

    # ---- helpers ----------------------------------------------------------
    def _configure_columns(self):
        """classic column widths: a narrow Num (~4 digits) and a tight,
        centered Clr; Payee/Memo stretch; money columns fixed and right-sized."""
        hh = self.view.horizontalHeader()
        R = RegisterModel
        fixed = {R.DATE: 84, R.NUM: 46, R.CLR: 30,
                 R.PAYMENT: 92, R.DEPOSIT: 92, R.BALANCE: 100}
        for col, width in fixed.items():
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.view.setColumnWidth(col, width)
        for col in (R.CATEGORY, R.TAG):
            hh.setSectionResizeMode(col, QHeaderView.Interactive)
        # A liability/loan register renders each payment's principal/interest/
        # escrow split in the Category cell (Task 51), so give it more room there.
        self.view.setColumnWidth(
            R.CATEGORY, 250 if self.model.uses_increase_decrease() else 130)
        self.view.setColumnWidth(R.TAG, 76)
        # Payee is the ONLY stretching column, so it absorbs every spare pixel.
        # Payee and Memo were both Stretch, and Stretch divides the leftover
        # EVENLY: on a default (non-maximized) window that gave each about 175px,
        # so an ordinary payee like "Anytown Water District" clipped while the
        # Memo beside it -- blank on ~80% of rows -- held the same width. A
        # register is read down its payee column; Memo is supplementary, so Memo
        # takes a fixed, still-useful width and Payee gets the rest and grows
        # with the window.
        hh.setSectionResizeMode(R.MEMO, QHeaderView.Interactive)
        self.view.setColumnWidth(R.MEMO, 150)
        hh.setSectionResizeMode(R.PAYEE, QHeaderView.Stretch)
        # Which columns show is decided in ONE place (_apply_column_visibility):
        # the loan register drops Num/Tag, two-line mode collapses
        # Category/Memo/Tag, and the user's own column chooser layers on top.
        self._apply_column_visibility()

    def _on_cell_clicked(self, index):
        """A single click acts at once: the Clr column toggles cleared (a
        display-only letter), and any other field enters inline edit
        immediately -- so Delete/typing work without a required second click.

        The Category column is the sole exception: its edit is DEFERRED by the
        system double-click interval so that a genuine double-click can open the
        split dialog instead (see _on_row_double_clicked, which cancels the
        pending edit). Every other column edits with no delay."""
        if not index.isValid():
            return
        col = index.column()
        if col == RegisterModel.CLR:
            if not self.model.is_blank_row(index.row()) \
                    and not self.model.is_pending_row(index.row()):
                self._cycle_clr(index.row())
            return
        if col == RegisterModel.CATEGORY:
            self._pending_edit = (index.row(), col)
            self._edit_timer.start(QApplication.doubleClickInterval())
            return
        self._edit_cell(index)

    def _begin_pending_edit(self):
        """Open the deferred Category editor once the double-click window has
        passed without a second click (armed in _on_cell_clicked)."""
        pending, self._pending_edit = self._pending_edit, None
        if pending is None:
            return
        row, col = pending
        if 0 <= row < self.model.rowCount():
            self._edit_cell(self.model.index(row, col))

    def _edit_cell(self, index):
        """Enter inline edit for an editable cell and select the editor's whole
        text, so the first Delete (or any keystroke) replaces the field's
        contents rather than nibbling one character."""
        if not index.isValid() or not (index.flags() & Qt.ItemIsEditable):
            return
        self.view.edit(index)
        # VIEWPORT, not the view. QWidget.focusWidget() reports the last child of
        # that widget which had focus, and after an editor is destroyed it can
        # still name it -- so selectAll() landed on freed memory. An item view's
        # editor is always a child of the viewport, and asking there returns the
        # live editor or nothing.
        editor = self.view.viewport().focusWidget()
        if isinstance(editor, QComboBox):
            editor = editor.lineEdit()
        if isinstance(editor, QLineEdit):
            editor.selectAll()

    def _on_row_double_clicked(self, index):
        """Double-click opens the split editor -- ONLY on the Category column
        (Quicken opens the split from the category), in addition to the
        right-click menu. It also cancels the deferred single-click Category
        edit so an inline editor does not open behind the dialog. The blank
        quick-entry row is ignored and a plain transfer shows the 'cannot be
        split' notice, both handled by ``_split_row``."""
        self._edit_timer.stop()
        self._pending_edit = None
        if index.isValid() and index.column() == RegisterModel.CATEGORY:
            self._split_row(index.row())

    def set_view_mode(self, mode: str) -> None:
        """Switch the register between Quicken's one-line and two-line layouts.

        Two-line: rows are double height, the Category/Tag/Memo columns collapse
        (their content moves under the payee, where it is now GENUINELY EDITABLE
        via the PayeeTwoLineDelegate), and line 1 keeps
        Date|Num|Payee|Payment|Clr|Deposit|Balance. The header grows so its
        Payee section labels both lines. Inline editing works on BOTH lines in
        two-line mode and on every column in one-line mode."""
        two = (mode == "two")
        self.view_mode = "two" if two else "one"
        self._sync_view_mode_actions()
        self.model.set_two_line(two)
        self.payee_delegate.set_two_line(two)
        self.header_view.set_two_line(two)
        vh = self.view.verticalHeader()
        hh = self.view.horizontalHeader()
        if two:
            fm = self.view.fontMetrics()
            vh.setDefaultSectionSize(fm.height() * 2 + 10)
            hh.setFixedHeight(fm.height() * 2 + 8)   # room for the 2-line header
        else:
            vh.setDefaultSectionSize(self._one_line_row_h)
            hh.setFixedHeight(self._one_line_header_h)
        self._apply_column_visibility()
        self.view.viewport().update()

    def apply_display_prefs(self, mode: str | None = None) -> None:
        """Re-apply the persisted Display Preferences to THIS open register:
        the register font, alternating-row shading on/off, and (live, via the
        model's ForegroundRole reading style.negative_color()) the negative
        amount color. ``mode`` optionally switches the one/two-line view at the
        same time; omit it to keep the current mode. Called by the main window
        after the Display Preferences dialog changes settings."""
        self.view.setAlternatingRowColors(prefs.row_shading())
        font = QFont(prefs.font_family(), prefs.font_size())
        self.view.setFont(font)
        self.view.horizontalHeader().setFont(font)
        # Keep one-line rows tall enough for a bigger font (the default font
        # leaves this at the pristine floor, so today's look is unchanged).
        fm = self.view.fontMetrics()
        self._one_line_row_h = max(self._base_row_h, fm.height() + 6)
        self._one_line_header_h = max(self._one_line_header_h, fm.height() + 6)
        self.set_view_mode(mode if mode is not None else self.view_mode)
        self.view.viewport().update()
        # A date-format (or other display) change must also re-render an open
        # review list so its dates match the register.
        self.review_panel.refresh_display()

    def has_open_editor(self) -> bool:
        """True while a cell editor is open on this register."""
        try:
            view = getattr(self, "view", None)
            return view is not None and view.state() == QAbstractItemView.EditingState
        except Exception:              # pragma: no cover - defensive
            return False

    def _open_editor(self):
        """The live editor widget, or None. Children of the VIEWPORT only."""
        view = getattr(self, "view", None)
        if view is None:
            return None
        editor = view.viewport().focusWidget()
        if editor is None:
            kids = [w for w in view.viewport().children() if isinstance(w, QWidget)]
            editor = kids[-1] if kids else None
        return editor

    def discard_open_editor(self) -> bool:
        """Close the editor WITHOUT writing its value back to the model."""
        try:
            if not self.has_open_editor():
                return False
            editor = self._open_editor()
            if editor is None:
                return False
            self.view.closeEditor(editor, QAbstractItemDelegate.RevertModelCache)
            return True
        except Exception:              # pragma: no cover - never block a switch
            return False

    def commit_open_editor(self) -> bool:
        """Commit and close any cell editor this register has open.

        Switching the stacked widget away from a view with a LIVE editor is a
        hard (C++) crash, not a Python exception -- which is why it left no
        traceback and no crash-log entry. The delegate's editor is reparented and
        destroyed while it is still mid-commit, and the write lands on a view
        that is no longer current.

        Returns True if an editor was actually closed."""
        try:
            view = getattr(self, "view", None)
            if view is None or view.state() != QAbstractItemView.EditingState:
                return False
            # focusWidget() alone is NOT enough. Clicking the accounts list moves
            # focus OUT of the editor before this runs, so the editor is still
            # alive and open while focusWidget() already returns None -- the first
            # version of this guard bailed out there and the crash survived. The
            # editor is a child of the viewport either way, so fall back to that.
            editor = view.viewport().focusWidget()
            if editor is None:
                # NOT filtered on isVisible(): inside hideEvent the children are
                # already hidden, so a visibility test finds nothing and the
                # editor survives into the teardown that crashes. Direct children
                # of a table viewport are the delegate's editor and little else.
                kids = [w for w in view.viewport().children()
                        if isinstance(w, QWidget)]
                editor = kids[-1] if kids else None
            if editor is None:
                return False
            view.commitData(editor)
            view.closeEditor(editor, QAbstractItemDelegate.NoHint)
            return True
        except Exception:              # pragma: no cover - never block a switch
            return False

    def hideEvent(self, event):
        """Close any live editor as this register is hidden.

        The hook lives HERE rather than at each switch site because every way of
        leaving a register -- the accounts list, the stack falling back to its
        placeholder, a future navigation path nobody has written yet -- ends in
        the widget being hidden. Guarding one caller only fixed one route."""
        self.commit_open_editor()
        super().hideEvent(event)

    def select_txn(self, txn_id) -> bool:
        """Select and scroll to a transaction by id -- the Find dialog uses this
        to jump to a search result once its account register is open."""
        row = self.model.row_for_txn(txn_id)
        if row < 0:
            return False
        idx = self.model.index(row, RegisterModel.PAYEE)
        self.view.setCurrentIndex(idx)
        self.view.selectRow(row)
        self.view.scrollTo(idx, QAbstractItemView.PositionAtCenter)
        return True

    def _refresh_header(self):
        self.header.setText(self.model.account_name())
        # An ASSET register's running balance is a COST BASIS -- what was paid
        # plus the improvements posted against it -- and labelling it "Ending
        # Balance" beside a decades-old purchase price states a number nobody
        # wants as though it were the answer. Say what it is, and put the market
        # value (and the debt secured against it) next to it, the way the
        # investment register shows Market Value beside its cash.
        if self._is_asset_account():
            self.balance_label.setText(self._asset_balance_text())
            return
        self.balance_label.setText(
            f"Ending Balance: {fmt_money(self.model.current_balance())}")

    def _asset_balance_text(self) -> str:
        """The asset register's status line: cost basis, market value, and the
        equity left after the loans secured by this property.

        An unvalued property shows its basis alone with a nudge to the gear --
        never a fabricated value, and never a bare number whose meaning the user
        has to guess."""
        from mammon import asset_values
        exp = asset_values.exposure(self.conn, self.account_id)
        latest = asset_values.value_at(self.conn, self.account_id)
        parts = [f"Cost Basis: {fmt_money(exp.basis)}"]
        if latest is None:
            parts.append("Value: not recorded (gear menu: Get Value)")
            return "     ".join(parts)
        parts.append(f"Value: {fmt_money(latest.value_cents)} "
                     f"({fmt_date(latest.date)})")
        if exp.debt:
            parts.append(f"Debt: {fmt_money(exp.debt)}")
            parts.append(f"Equity: {fmt_money(exp.net)}")
        return "     ".join(parts)

    def _selected_row(self):
        idxs = self.view.selectionModel().selectedRows()
        if idxs:
            return idxs[0].row()
        cur = self.view.currentIndex()
        return cur.row() if cur.isValid() else -1

    def _on_error(self, message):
        QMessageBox.warning(self, "Could not save", message)

    def _context_menu(self, pos):
        index = self.view.indexAt(pos)
        menu = QMenu(self)
        act_new = menu.addAction("New…")
        act_edit = menu.addAction("Edit…")
        act_split = menu.addAction("Split…")
        act_delete = menu.addAction("Delete")
        act_toggle = menu.addAction("Toggle Cleared")
        act_void = menu.addAction("Void")
        # A pending pre-entry offers the way to make it real by hand.
        act_post = None
        if index.isValid() and self.model.is_scheduled_row(index.row()):
            act_post = menu.addAction("Enter Pending Payment")
        # A transfer leg offers a jump to the mirror transaction in the other
        # account (user: "from one side of a transfer, get to the other side").
        goto_actions = []
        goto_targets = self._transfer_targets(index.row()) if index.isValid() else []
        if goto_targets:
            menu.addSeparator()
            for tgt in goto_targets:
                goto_actions.append((menu.addAction(f"Go to [{tgt['name']}]"), tgt))
        # A loan register also offers the projected principal-payoff chart.
        act_project = None
        if self.model.is_loan():
            menu.addSeparator()
            act_project = menu.addAction("Principal Projection…")
        # Several rows selected: the batch edits (parity). Each acts on the
        # whole selection and reports what it skipped.
        batch = {}
        sel = self._selected_rows()
        if len(sel) > 1:
            n = len(sel)
            menu.addSeparator()
            batch[menu.addAction(f"Change Category for {n}…")] = \
                lambda: self._batch_category(sel)
            batch[menu.addAction(f"Change Payee for {n}…")] = \
                lambda: self._batch_field(sel, "payee", "payee")
            batch[menu.addAction(f"Change Memo for {n}…")] = \
                lambda: self._batch_field(sel, "memo", "memo")
            batch[menu.addAction(f"Change Tag for {n}…")] = \
                lambda: self._batch_field(sel, "tag", "tag")
            batch[menu.addAction(f"Mark {n} Cleared")] = \
                lambda: self._batch_cleared(sel, True)
            batch[menu.addAction(f"Mark {n} Uncleared")] = \
                lambda: self._batch_cleared(sel, False)
            batch[menu.addAction(f"Void {n}…")] = lambda: self._void_rows(sel)
            batch[menu.addAction(f"Delete {n}…")] = lambda: self._delete_rows(sel)
        chosen = menu.exec_(self.view.viewport().mapToGlobal(pos))
        for act, tgt in goto_actions:
            if chosen == act:
                self._go_to_transfer(tgt)
                return
        if chosen in batch:
            batch[chosen]()
        elif chosen == act_new:
            self.on_new()
        elif chosen == act_edit and index.isValid():
            self._edit_row(index.row())
        elif chosen == act_split and index.isValid():
            self._split_row(index.row())
        elif chosen == act_delete and index.isValid():
            self._delete_row(index.row())
        elif chosen == act_toggle and index.isValid():
            self._cycle_clr(index.row())
        elif chosen == act_void and index.isValid():
            self._void_row(index.row())
        elif act_post is not None and chosen == act_post:
            self.model.post_row(index.row())
        elif act_project is not None and chosen == act_project:
            self._chart_projection()

    def _transfer_targets(self, row):
        """Jump targets for the context menu's 'Go to [account]' entries: one
        dict {account_id, txn_id (the row to select there), name} per DISTINCT
        target account this transaction transfers to. Empty for non-transfer
        rows, which hides the entries. Three sources feed it, all deduped by
        target account:

          1. a whole-transaction transfer leg -- top-level transfer_account_id
             + transfer_pair_id (Quicken's mirror model cross-links the pair);
          2. any transfer split line the transaction itself carries -- each
             split keeps its own transfer_account_id/transfer_pair_id, so a
             single split can transfer to several accounts at once (get_splits
             drops the pair id, so read the splits table directly);
          3. the REVERSE link -- a one-sided mirror leg (this row's own
             transfer_pair_id is NULL) that is referenced *by* a transfer split
             (or a top-level leg) sitting in ANOTHER account. This is the loan
             register case: a regular loan payment posts on checking as a split
             whose principal line transfers into the loan, and the loan side is
             only a one-sided mirror leg, so its counterparty lives on the
             checking transaction that points back at this leg -- there is no
             top-level or split transfer on the loan row itself to inspect."""
        if row < 0 or self.model.is_blank_row(row) or self.model.is_pending_row(row):
            return []
        txn = self.model.txn_at(row)
        if not txn:
            return []
        pairs = []  # (target_account_id, txn_id_to_select_there)
        # 1. whole-transaction transfer leg
        if txn.get("transfer_account_id") is not None and txn.get("transfer_pair_id") is not None:
            pairs.append((txn["transfer_account_id"], txn["transfer_pair_id"]))
        # 2. transfer split lines this transaction itself carries. A split leg
        #    is a transfer whenever transfer_account_id is set; transfer_pair_id
        #    (the exact mirror row to land on) is often NULL for legacy/import
        #    legs, so we still offer the jump -- navigation falls back to just
        #    opening the target account when there is no specific mirror.
        if txn.get("is_split"):
            for s in self.conn.execute(
                "SELECT transfer_account_id, transfer_pair_id FROM splits "
                "WHERE transaction_id=? AND transfer_account_id IS NOT NULL "
                "ORDER BY id",
                (txn["id"],),
            ).fetchall():
                pairs.append((s["transfer_account_id"], s["transfer_pair_id"]))
        # 3. reverse link: this row is a one-sided mirror leg referenced from
        #    another account (loan payments, one-sided import mirrors). Only when
        #    the row has no pair of its own -- a two-sided leg is already covered.
        if txn.get("transfer_pair_id") is None:
            for s in self.conn.execute(
                "SELECT t.account_id AS acct, s.transaction_id AS txn "
                "FROM splits s JOIN transactions t ON t.id = s.transaction_id "
                "WHERE s.transfer_pair_id=? ORDER BY s.id",
                (txn["id"],),
            ).fetchall():
                pairs.append((s["acct"], s["txn"]))
            for t in self.conn.execute(
                "SELECT account_id AS acct, id AS txn FROM transactions "
                "WHERE transfer_pair_id=? ORDER BY id",
                (txn["id"],),
            ).fetchall():
                pairs.append((t["acct"], t["txn"]))
        own_account = getattr(self.model, "account_id", None)
        by_acct = {}   # account_id -> target dict
        order = []     # first-seen account order, so the menu is stable
        for acct_id, pair_id in pairs:
            # one link per DISTINCT target account; a self-referential leg (same
            # account) is a meaningless 'Go to here'.
            if acct_id == own_account:
                continue
            if acct_id not in by_acct:
                acct = ledger.get_account(self.conn, acct_id)
                if acct is None:
                    continue          # deleted account -> no live jump target
                by_acct[acct_id] = {
                    "account_id": acct_id,
                    "txn_id": pair_id,
                    "name": acct["name"],
                }
                order.append(acct_id)
            elif by_acct[acct_id]["txn_id"] is None and pair_id is not None:
                # a later leg to the same account knows the exact mirror row --
                # upgrade so the jump lands on the transaction, not just the tab.
                by_acct[acct_id]["txn_id"] = pair_id
        return [by_acct[a] for a in order]

    def _go_to_transfer(self, target):
        """Navigate to the other side of a transfer: switch the main window to
        the counterparty account's register and select the mirror transaction.
        Reuses MainWindow.open_register + RegisterWidget.select_txn (the same
        pair the global Find dialog uses to land on a result)."""
        win = self.window()
        if win is None or not hasattr(win, "open_register"):
            return
        reg = win.open_register(target["account_id"])
        # Landing on the target account is the win; selecting the mirror row is
        # best-effort -- txn_id is None for legacy/import legs with no pair, and
        # select_txn simply no-ops (returns False) when the id isn't shown.
        if reg is not None and target.get("txn_id") is not None and hasattr(reg, "select_txn"):
            reg.select_txn(target["txn_id"])

    def _chart_projection(self):
        """Chart the loan's projected outstanding principal declining into the
        future (loan registers only). Delegates to the module-level helper, which
        lazily imports the matplotlib canvas so mammon.ui stays import-cheap."""
        _chart_loan_projection(self, self.conn, self.model.account_id)

    # ---- toolbar actions --------------------------------------------------
    def on_new(self):
        dlg = TransactionDialog(self.model, row=None, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self.model.add_from_values(dlg.values())

    def on_edit(self):
        self._edit_row(self._selected_row())

    def _edit_row(self, row):
        if row < 0 or self.model.is_blank_row(row) or self.model.is_pending_row(row):
            return
        dlg = TransactionDialog(self.model, row=row, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self.model.update_from_values(row, dlg.values())

    def on_split(self):
        self._split_row(self._selected_row())

    def _split_row(self, row):
        if row < 0:
            return
        if self.model.is_blank_row(row):
            # A half-typed NEW transaction on the quick-entry row has no persisted
            # transaction yet, so it cannot be split as-is. Mirror the pending path
            # below (Quicken opens the split on the SAVED transaction): commit the
            # in-progress row through the ledger first, then split the row it
            # created. A blank row still missing a date or an amount is not
            # splittable -- commit_blank_returning_id returns None and this stays a
            # no-op, exactly as before.
            txn_id = self.model.commit_blank_returning_id()
            if not txn_id or txn_id < 0:
                return
            row = self.model.row_for_txn(txn_id)
            if row is None or row < 0:
                return
        elif self.model.is_pending_row(row):
            # A NEW review row has no persisted transaction yet, so there is
            # nothing to split -- the Split button used to silently do nothing.
            # Accept it into the register first (Quicken opens the split on the
            # saved transaction), then split the row it just created.
            txn_id = self._accept_pending()
            if not txn_id or txn_id < 0:
                return
            row = self.model.row_for_txn(txn_id)
            if row is None or row < 0:
                return
        txn = self.model.txn_at(row)
        if txn is None:
            return
        # A plain whole-transaction transfer cannot be split, but a transfer
        # that ALREADY carries split lines (an imported mortgage payment that is
        # a transfer to [House] AND a principal+interest split) is a legitimate
        # split whose legs must stay editable -- open the split editor for it.
        if txn["transfer_account_id"] is not None and not txn.get("is_split"):
            QMessageBox.information(self, "Split", "A transfer cannot be split.")
            return
        undo_before = self.model.undo_stack.capture(txn["id"])
        dlg = SplitDialog(self.model, row, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            # Record the split change for Undo (the set_splits ran inside the
            # dialog, through the ledger); a no-op change records nothing.
            self.model.undo_stack.record_edit(
                txn["id"], undo_before, label="Edit splits")
            self.model.reload()
            self.model.committed.emit()

    def on_delete(self):
        rows = self._selected_rows()
        if len(rows) > 1:
            self._delete_rows(rows)
        else:
            self._delete_row(self._selected_row())

    def _delete_row(self, row):
        txn = self.model.txn_at(row)
        if not txn:
            return
        note = " (both sides of the transfer)" if txn["transfer_account_id"] is not None else ""
        if QMessageBox.question(
                self, "Delete transaction",
                f"Delete the {txn['date']} transaction{note}?",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.model.delete_row(row)

    def on_toggle(self):
        row = self._selected_row()
        if row >= 0:
            self._cycle_clr(row)

    def _cycle_clr(self, row):
        """Advance the Clr flag on ``row`` one step in Quicken's
        blank -> c -> R -> blank cycle, persisting to that leg only. The two
        steps that SET or CLEAR 'R' (normally reconcile-managed) first ask for a
        brief confirmation but proceed on Yes; the blank<->c step never prompts.
        """
        nxt = self.model.clr_cycle_next(row)
        if nxt is None:
            return
        _cleared, _reconciled, touches_r, setting_r = nxt
        if touches_r:
            verb = "set" if setting_r else "clear"
            if QMessageBox.question(
                    self, "Reconciled flag",
                    "The reconciled ('R') flag is normally managed by the "
                    f"Reconcile tool.\n\nManually {verb} it on this "
                    "transaction anyway?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No) != QMessageBox.Yes:
                return
        self.model.toggle_cleared(row)


# ---------------------------------------------------------------------------
# investment transaction dialog (per-action add/edit form)
# ---------------------------------------------------------------------------
_INV_HUNDRED = Decimal("100")


def _round_cents(value: Decimal) -> int:
    """Round a Decimal dollar-amount to integer cents, HALF_UP (matches the
    money rounding in :mod:`mammon.investments`)."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _dec_or_none(text):
    """A user-typed share quantity/price -> Decimal, or None when blank/garbage
    (so 'entered vs omitted' stays distinct for the Qty/Price/Amount solver)."""
    s = "" if text is None else str(text).strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


# The investment actions this dialog supports in v1 (a practical subset of
# Quicken's ~40). The label is what the user picks; the code is stored in
# investment_transactions.action (vocabulary per mammon.investments). "Reinvest"
# narrows to "ReinvDiv" at persist time when the only income line is the
# dividend (Quicken's shorthand for a pure dividend reinvestment).
# The actions the dialog offers. This list must cover everything an IMPORTER can
# produce, not just what a user would pick from scratch: importers/csvimp maps a
# plan's own vocabulary onto Quicken codes ("contribution" -> BuyX, "redemption"
# -> SellX), and a QIF carries whatever its source wrote. An action missing here
# used to fall back to index 0 -- Buy -- so opening a redemption in the editor
# silently turned a sale into a purchase.
_INV_ACTION_CHOICES = [
    ("Buy", "Buy"),
    ("Buy (from cash transferred in)", "BuyX"),
    ("Sell", "Sell"),
    ("Sell (proceeds transferred out)", "SellX"),
    ("Dividend (cash)", "Div"),
    ("Reinvest", "Reinvest"),
    ("Reinvest dividend", "ReinvDiv"),
    ("Reinvest interest", "ReinvInt"),
    ("Reinvest short-term cap gain", "ReinvSh"),
    ("Reinvest mid-term cap gain", "ReinvMd"),
    ("Reinvest long-term cap gain", "ReinvLg"),
    ("Income (MiscInc / Cash)", "MiscInc"),
    # Cash held in an investment account earns interest, and a fund distributes
    # capital gains -- both are ordinary events in an IRA or brokerage, supported
    # by the domain (investments._DIVIDEND_ACTIONS) and documented in the schema,
    # but unofferable until now. The "X" variants move the cash to another
    # account rather than leaving it as the account's own cash.
    ("Interest income", "IntInc"),
    ("Interest income (transferred out)", "IntIncX"),
    ("Dividend (transferred out)", "DivX"),
    ("Long-term cap gain distribution", "CGLong"),
    ("Long-term cap gain (transferred out)", "CGLongX"),
    ("Mid-term cap gain distribution", "CGMid"),
    ("Mid-term cap gain (transferred out)", "CGMidX"),
    ("Short-term cap gain distribution", "CGShort"),
    ("Short-term cap gain (transferred out)", "CGShortX"),
    ("Stock dividend (shares)", "StockDividend"),
    ("Margin interest paid", "MargInt"),
    ("Cash withdrawal", "Withdraw"),
    ("Cash withdrawal (transferred out)", "WithdrwX"),
    ("Add shares", "ShrsIn"),
    ("Remove shares", "ShrsOut"),
    ("Misc expense", "MiscExp"),
    ("Return of capital", "RtrnCap"),
    ("Stock split", "StkSplit"),
    ("Short sell", "ShtSell"),
    ("Cover short", "CvrShrt"),
    ("Transfer cash in", "XIn"),
    ("Transfer cash out", "XOut"),
]

# A stock split is STORED as Quicken stores it: new shares per TEN old, so a
# 2-for-1 is 20 and a 1-for-2 reverse is 5 (investments._apply_txn multiplies the
# running quantity by q/10, and the OFX importer's <SPLIT> handler writes the
# same encoding). Nobody says "eighty-for-ten", so the dialog asks for the ratio
# the broker announces -- 8 for an 8-for-1 -- and converts at the boundary.
#
# These two functions exist because the field did NOT convert: it stored the
# typed number raw, so typing 2 for a 2-for-1 split stored 2, and the replay read
# that as 2-per-10 and shrank the position to a FIFTH instead of doubling it.
# The placeholder said "e.g. 2 for a 2-for-1 split", so the field documented the
# wrong thing as well as doing it.
def _split_from_row(txn):
    """A split row -> the ratio to show, in the SAME notation the register shows
    it in ("8:1"), so the field displays exactly what it accepts."""
    return investments.split_display(txn)


# Which field groups each action shows. "quantity"/"price"/"amount" are the
# interdependent trio; "reinv" the per-income-type breakdown lines; "split" the
# stock-split ratio; "catxfer" the category-or-transfer picker for cash lines;
# "memo" a free note. Everything else stays hidden for that action.
_TRADE_FIELDS = {"security", "quantity", "price", "amount", "commission", "memo"}
_REINV_FIELDS = {"security", "quantity", "price", "amount", "commission", "reinv"}
_INV_ACTION_FIELDS = {
    "Buy":      _TRADE_FIELDS,
    "BuyX":     _TRADE_FIELDS,
    "Sell":     _TRADE_FIELDS,
    "SellX":    _TRADE_FIELDS,
    "ShtSell":  _TRADE_FIELDS,
    "CvrShrt":  _TRADE_FIELDS,
    "Div":      {"security", "amount", "catxfer"},
    "DivX":     {"security", "amount", "catxfer"},
    "CGLong":   {"security", "amount", "catxfer"},
    "CGLongX":  {"security", "amount", "catxfer"},
    "CGMid":    {"security", "amount", "catxfer"},
    "CGMidX":   {"security", "amount", "catxfer"},
    "CGShort":  {"security", "amount", "catxfer"},
    "CGShortX": {"security", "amount", "catxfer"},
    # A stock dividend pays in SHARES, not cash: no amount, no category.
    "StockDividend": {"security", "quantity", "memo"},
    "IntInc":   {"amount", "catxfer"},
    "IntIncX":  {"amount", "catxfer"},
    "MargInt":  {"amount", "catxfer"},
    "Withdraw": {"amount", "catxfer"},
    "WithdrwX": {"amount", "catxfer"},
    "Reinvest": _REINV_FIELDS,
    "ReinvDiv": _REINV_FIELDS,
    "ReinvInt": _REINV_FIELDS,
    "ReinvSh":  _REINV_FIELDS,
    "ReinvMd":  _REINV_FIELDS,
    "ReinvLg":  _REINV_FIELDS,
    "MiscInc":  {"amount", "catxfer"},
    # A share move carries a PRICE and a gross value, and both are worth keeping:
    # a plan's quarterly fee removal ("ShrsOut 0.045 shares, $1.28") is often the
    # only quote that exists for a fund quoted nowhere public, and the register is
    # where a net-worth curve gets its shape. ShrsOut listed neither, so opening
    # one in the editor and pressing OK silently blanked what the broker sent --
    # 552 of 599 ShrsOut rows in the real ledger have no price at all.
    # Cash effect stays zero either way (investments._CASH_ZERO_ACTIONS), so the
    # amount is a gross annotation, never a cash movement.
    "ShrsIn":   {"security", "quantity", "price", "amount", "memo"},
    "ShrsOut":  {"security", "quantity", "price", "amount", "memo"},
    "MiscExp":  {"amount", "catxfer"},
    "RtrnCap":  {"security", "amount", "catxfer"},
    "StkSplit": {"security", "split", "memo"},
    "XIn":      {"amount", "catxfer"},
    "XOut":     {"amount", "catxfer"},
}

# Actions whose stored amount is cash OUT (negative); the rest store a positive
# amount -- cash in for Sell/Div/MiscInc/RtrnCap/XIn, or a cash-neutral gross
# trade value used for cost basis for Reinvest/ShrsIn.
_INV_CASH_OUT = {"Buy", "MiscExp", "XOut"}


class InvestmentTransactionDialog(QDialog):
    """Add/edit one investment transaction. The visible fields depend on the
    chosen action -- a Buy shows Security/Quantity/Price/Amount/Commission, a
    Dividend shows Security/Amount/Category, a Reinvest adds per-income-type
    breakdown lines (Dividend/Interest/Short-Mid-Long cap gain), a cash line
    (Div/Inc/MiscExp/RtrnCap) shows a Category-or-Transfer picker, etc.

    Quantity, Price and Amount are interdependent: enter any two and the third is
    computed by :meth:`resolve_qpa`; enter all three inconsistently and
    :meth:`accept` asks which one to recompute. Following the mammon dialog
    convention, ``values()`` returns storage-ready kwargs for
    :func:`mammon.investments.record_investment` /
    :func:`~mammon.investments.update_investment`, and ``validate()`` is a pure
    ``(ok, message)`` check that shows no modal, so tests drive it headlessly.
    """

    def __init__(self, conn, account_id, txn=None, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = account_id
        self.txn = txn
        editing = txn is not None
        self.setWindowTitle("Edit investment transaction" if editing
                            else "New investment transaction")

        # field key -> list of (label widget, field widget), for show/hide
        self._field_rows: dict = {}
        form = QFormLayout(self)

        self.date = make_date_edit()
        form.addRow("Date", self.date)

        self.action = NoWheelComboBox()
        for label, code in _INV_ACTION_CHOICES:
            self.action.addItem(label, code)
        form.addRow("Action", self.action)

        self.security = NoWheelComboBox()
        self.security.setEditable(True)
        self.security.setInsertPolicy(QComboBox.NoInsert)
        self.security.addItem("")
        for sym in investments.symbols_used(conn, account_id):
            self.security.addItem(sym)
        self._add_field(form, "security", "Security", self.security)

        self.quantity = QLineEdit()
        self._add_field(form, "quantity", "Quantity", self.quantity)
        self.price = QLineEdit()
        self._add_field(form, "price", "Price", self.price)
        self.amount = QLineEdit()
        self.amount.setPlaceholderText("$ gross (Quantity x Price)")
        self._add_field(form, "amount", "Amount", self.amount)
        self.commission = QLineEdit()
        self._add_field(form, "commission", "Commission", self.commission)

        self.split = QLineEdit()
        self.split.setPlaceholderText(
            "8:1, 4:3, or 1:2 for a reverse split (a bare 8 also works)")
        self._add_field(form, "split", "Split ratio", self.split)

        # Category-or-transfer picker for cash lines: a known account name reads
        # as a transfer leg ([Account]); free text is the category (memo).
        self.catxfer = NoWheelComboBox()
        self.catxfer.setEditable(True)
        self.catxfer.setInsertPolicy(QComboBox.NoInsert)
        self.catxfer.addItem("", None)
        for a in ledger.list_accounts(conn, include_closed=True):
            if a["id"] != account_id:
                self.catxfer.addItem(a["name"], a["id"])
        self._add_field(form, "catxfer", "Category / Transfer", self.catxfer)

        # Reinvest breakdown -- internal tax categories that Quicken splits into
        # separate reinvest actions; we keep them on one form and summarize into
        # the memo (the schema has no category_id).
        self.reinv_div = QLineEdit()
        self.reinv_int = QLineEdit()
        self.reinv_st = QLineEdit()
        self.reinv_mid = QLineEdit()
        self.reinv_lt = QLineEdit()
        self._add_field(form, "reinv", "Dividend", self.reinv_div)
        self._add_field(form, "reinv", "Interest", self.reinv_int)
        self._add_field(form, "reinv", "Short-term cap gain", self.reinv_st)
        self._add_field(form, "reinv", "Mid-term cap gain", self.reinv_mid)
        self._add_field(form, "reinv", "Long-term cap gain", self.reinv_lt)

        self.memo = QLineEdit()
        self._add_field(form, "memo", "Memo", self.memo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self.action.currentIndexChanged.connect(self._apply_action)
        if editing:
            self._load(txn)
        self._apply_action()

    # ---- form plumbing ----------------------------------------------------
    def _add_field(self, form, key, label, widget):
        lbl = QLabel(label)
        form.addRow(lbl, widget)
        self._field_rows.setdefault(key, []).append((lbl, widget))

    def _apply_action(self):
        """Show only the field rows relevant to the current action."""
        show = _INV_ACTION_FIELDS.get(self.action.currentData(), set())
        for key, rows in self._field_rows.items():
            visible = key in show
            for lbl, widget in rows:
                lbl.setVisible(visible)
                widget.setVisible(visible)

    def _amount_or_none(self):
        s = self.amount.text().strip()
        return parse_amount(s) if s else None

    def _read_catxfer(self):
        """(transfer_account_id, category_memo) from the Category/Transfer combo:
        text matching a known account is a transfer; any other text is a free
        category stored in memo (matching investments.category_display)."""
        text = self.catxfer.currentText().strip()
        if not text:
            return None, None
        idx = self.catxfer.findText(text)
        if idx > 0:
            data = self.catxfer.itemData(idx)
            if isinstance(data, int):
                return data, None
        return None, text

    def _reinvest_memo_and_action(self, code):
        """Summarize the reinvest breakdown lines into a memo, and pick the
        stored action: 'ReinvDiv' when the only income is the dividend, else the
        generic 'Reinvest'."""
        lines = [("Div", self.reinv_div), ("Int", self.reinv_int),
                 ("STCG", self.reinv_st), ("MidCG", self.reinv_mid),
                 ("LTCG", self.reinv_lt)]
        parts, nonzero = [], []
        for label, widget in lines:
            s = widget.text().strip()
            if s:
                cents = parse_amount(s)
                if cents:
                    parts.append(f"{label} {fmt_cents(cents)}")
                    nonzero.append(label)
        memo = "; ".join(parts) or None
        action = "ReinvDiv" if nonzero == ["Div"] else code
        return memo, action

    # ---- interdependent Quantity / Price / Amount -------------------------
    @staticmethod
    def resolve_qpa(qty, price, amount):
        """Pure solver for the Quantity/Price/Amount trio. ``qty``/``price`` are
        Decimals (or None); ``amount`` is integer cents (or None). Returns
        ``(qty, price, amount, status)`` where status is ``computed_amount`` /
        ``computed_price`` / ``computed_qty`` (two supplied, third derived),
        ``consistent`` (all three agree), ``conflict`` (all three supplied but
        Qty*Price != Amount) or ``insufficient`` (fewer than two, or a zero
        divisor)."""
        have = sum(v is not None for v in (qty, price, amount))
        if have < 2:
            return qty, price, amount, "insufficient"
        if amount is None:
            return qty, price, _round_cents(qty * price * _INV_HUNDRED), "computed_amount"
        if price is None:
            if qty == 0:
                return qty, price, amount, "insufficient"
            return qty, Decimal(amount) / (qty * _INV_HUNDRED), amount, "computed_price"
        if qty is None:
            if price == 0:
                return qty, price, amount, "insufficient"
            return Decimal(amount) / (price * _INV_HUNDRED), price, amount, "computed_qty"
        if _round_cents(qty * price * _INV_HUNDRED) == amount:
            return qty, price, amount, "consistent"
        return qty, price, amount, "conflict"

    # ---- edit-mode population --------------------------------------------
    def _load(self, txn):
        if txn is None:
            return
        _set_date_edit(self.date, txn["date"])
        code = (txn["action"] or "").strip()
        idx = self.action.findData(code)
        if idx < 0 and code:
            # An action the list does not know -- raw activity text an importer
            # passed through, or a Quicken code newer than this list. ADD it
            # rather than leaving the combo on its first entry: falling through
            # left the editor showing "Buy" for a row that was nothing of the
            # kind, and saving then wrote that back. Editing a transaction must
            # never silently change what KIND of transaction it is.
            self.action.addItem(f"{code} (as imported)", code)
            idx = self.action.findData(code)
        if idx >= 0:
            self.action.setCurrentIndex(idx)
        if txn["symbol"]:
            self.security.setEditText(txn["symbol"])
        if code.lower() == "stksplit":
            # From the ratio PAIR when the row has one -- a 4:3 split carries no
            # usable ``quantity``, so keying off that column would blank the field.
            self.split.setText(_split_from_row(txn))
        elif txn["quantity"]:
            self.quantity.setText(str(txn["quantity"]))
        if txn["price"]:
            self.price.setText(str(txn["price"]))
        if txn["amount"] is not None:
            self.amount.setText(fmt_cents(abs(txn["amount"])))
        if txn["commission"] is not None:
            self.commission.setText(fmt_cents(txn["commission"]))
        taid = txn["transfer_account_id"]
        if taid is not None:
            i = self.catxfer.findData(taid)
            if i >= 0:
                self.catxfer.setCurrentIndex(i)
        elif txn["memo"]:
            self.catxfer.setEditText(txn["memo"])
            self.memo.setText(txn["memo"])

    # ---- validation + values ---------------------------------------------
    def validate(self):
        """Pure ``(ok, message)`` gate (no modal), for the OK button and tests."""
        code = self.action.currentData()
        fields = _INV_ACTION_FIELDS.get(code, set())
        if not date_edit_iso(self.date):
            return False, "Enter a date."
        if "security" in fields and not self.security.currentText().strip():
            return False, "Choose a security."
        if code in ("Buy", "Sell", "Reinvest"):
            _q, _p, _a, status = self.resolve_qpa(
                _dec_or_none(self.quantity.text()),
                _dec_or_none(self.price.text()),
                self._amount_or_none())
            if status == "insufficient":
                return False, "Enter at least two of Quantity, Price and Amount."
            if status == "conflict":
                return False, ("Quantity x Price does not equal Amount -- "
                               "choose which value to recompute.")
        if code in ("Div", "MiscInc", "MiscExp", "RtrnCap", "XIn", "XOut"):
            if self._amount_or_none() in (None, 0):
                return False, "Enter an amount."
        if code in ("ShrsIn", "ShrsOut") and _dec_or_none(self.quantity.text()) is None:
            return False, "Enter a quantity."
        if code == "StkSplit":
            ratio = investments.parse_split_ratio(self.split.text())
            if ratio is None:
                return False, ("Enter the split ratio -- 8:1 for an 8-for-1, "
                               "4:3, or 1:2 for a reverse split.")
            if ratio <= 0:
                return False, "A split ratio has to be greater than zero."
        return True, ""

    def values(self) -> dict:
        """Storage-ready kwargs for record_investment / update_investment.
        Quantity/Price are Decimals (or None) -- the store normalizes them to
        text; ``amount``/``commission`` are signed integer cents."""
        code = self.action.currentData()
        fields = _INV_ACTION_FIELDS.get(code, set())
        symbol = quantity = price = amount = commission = memo = None
        transfer_account_id = None
        split_num = split_den = None
        action = code

        if "security" in fields:
            symbol = self.security.currentText().strip().upper() or None

        if code == "StkSplit":
            # The exact ratio is the pair; ``quantity`` is the derived legacy
            # per-ten value kept beside it (investments.split_stored).
            ratio = investments.parse_split_ratio(self.split.text())
            split_num = ratio.numerator if ratio is not None else None
            split_den = ratio.denominator if ratio is not None else None
            quantity = investments.split_stored(ratio)
        else:
            qraw = _dec_or_none(self.quantity.text()) if "quantity" in fields else None
            praw = _dec_or_none(self.price.text()) if "price" in fields else None
            araw = self._amount_or_none() if "amount" in fields else None
            quantity, price, amount, status = self.resolve_qpa(qraw, praw, araw)
            if status == "insufficient":
                quantity, price, amount = qraw, praw, araw

        if "commission" in fields:
            c = self.commission.text().strip()
            commission = parse_amount(c) if c else None

        if "reinv" in fields:
            memo, action = self._reinvest_memo_and_action(code)

        if "catxfer" in fields:
            transfer_account_id, cat_memo = self._read_catxfer()
            if transfer_account_id is None:
                memo = cat_memo

        if "memo" in fields:
            m = self.memo.text().strip()
            if m:
                memo = m

        if amount is not None:
            amount = -abs(amount) if code in _INV_CASH_OUT else abs(amount)

        return {
            "date": date_edit_iso(self.date),
            "action": action,
            "symbol": symbol,
            "quantity": quantity,
            "price": price,
            "amount": amount,
            "commission": commission,
            "memo": memo,
            "transfer_account_id": transfer_account_id,
            "split_num": split_num,
            "split_den": split_den,
        }

    # ---- OK: resolve conflicts before closing ----------------------------
    def accept(self):
        code = self.action.currentData()
        if code in ("Buy", "Sell", "Reinvest"):
            qraw = _dec_or_none(self.quantity.text())
            praw = _dec_or_none(self.price.text())
            araw = self._amount_or_none()
            _q, _p, _a, status = self.resolve_qpa(qraw, praw, araw)
            if status == "conflict":
                self._prompt_recompute(qraw, praw, araw)
                return  # leave open; user re-confirms with the fixed value
        ok, msg = self.validate()
        if not ok:
            QMessageBox.warning(self, "Investment transaction", msg)
            return
        super().accept()

    def _prompt_recompute(self, qty, price, amount):
        choice, ok = QInputDialog.getItem(
            self, "Recompute",
            "Quantity x Price does not equal Amount. Which value should be "
            "recomputed?", ["Amount", "Price", "Quantity"], 0, False)
        if not ok:
            return
        if choice == "Amount":
            self.amount.setText(fmt_cents(_round_cents(qty * price * _INV_HUNDRED)))
        elif choice == "Price" and qty:
            self.price.setText(str(Decimal(amount) / (qty * _INV_HUNDRED)))
        elif choice == "Quantity" and price:
            self.quantity.setText(str(Decimal(amount) / (price * _INV_HUNDRED)))


# ---------------------------------------------------------------------------
# investment register widget (transaction view + valuation header)
# ---------------------------------------------------------------------------
class SecurityRenameDialog(QDialog):
    """Search/replace over the security names of ONE investment account.

    A broker starts sending "ALTY" for the security it spent five years calling
    "ALTY GLOBAL X SUPERDIVIDEND ALTER"; a fund company renames "Fidelity 500
    Index Fund" to "FXAIX". Either way the register ends up holding one position
    under two names, and fixing that by editing rows means opening a hundred
    transactions. See :func:`investments.plan_security_rename` for why matching is
    by substring and why the account is the scope.

    The PREVIEW is the feature, not decoration. A substring reaches names the
    user was not thinking of -- in a real options account, "AGNC" appears in a
    stock and in six expired option contracts -- so every change is listed as
    ``old -> new (n transactions)``, flagged when it merges two positions, and
    individually untickable. Nothing is written until Rename.
    """

    def __init__(self, conn, account_id, parent=None, initial=""):
        super().__init__(parent)
        self.conn = conn
        self.account_id = account_id
        acct = ledger.get_account(conn, account_id)
        name = acct["name"] if acct else ""
        self.setWindowTitle(
            f"Rename Security - {name}" if name else "Rename Security")
        self._plan: list = []

        outer = QVBoxLayout(self)
        form = QFormLayout()
        self.search_edit = QLineEdit(initial or "")
        self.search_edit.setPlaceholderText("text to find in the security name")
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText(
            "replacement -- leave blank to delete the text found")
        form.addRow("Find:", self.search_edit)
        form.addRow("Replace with:", self.replace_edit)
        outer.addLayout(form)

        scope = QLabel(
            f"Applies only to securities in {name}." if name
            else "Applies only to this account.")
        scope.setObjectName("registerSub")
        outer.addWidget(scope)

        self.list = QListWidget()
        self.list.setMinimumWidth(520)
        outer.addWidget(self.list)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName("registerSub")
        outer.addWidget(self.status)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Rename")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

        self.search_edit.textChanged.connect(self.refresh)
        self.replace_edit.textChanged.connect(self.refresh)
        self.refresh()
        self.resize(640, 420)

    def refresh(self):
        """Recompute the preview from the current Find/Replace text."""
        try:
            self._plan = investments.plan_security_rename(
                self.conn, self.account_id,
                self.search_edit.text(), self.replace_edit.text())
            problem = ""
        except ValueError as exc:
            self._plan, problem = [], str(exc)
        self.list.clear()
        for r in self._plan:
            label = "%s   \u2192   %s      (%d transaction%s)" % (
                r.old, r.new, r.txns, "" if r.txns == 1 else "s")
            if r.merges:
                label += "   \u2022 merges with a security already here"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.list.addItem(item)
        self.status.setText(self._status_text(problem))
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(self._plan))

    def _status_text(self, problem: str) -> str:
        if problem:
            return problem
        if not self.search_edit.text().strip():
            return "Type the text to find. Matching ignores case."
        if not self._plan:
            return "No security in this account matches."
        merges = sum(1 for r in self._plan if r.merges)
        msg = "%d security name%s will change." % (
            len(self._plan), "" if len(self._plan) == 1 else "s")
        if merges:
            msg += ("  %d of them merge%s into a security this account already "
                    "holds: the two positions become one, and their lots, cost "
                    "basis and dividends combine." % (
                        merges, "s" if merges == 1 else ""))
        return msg

    def chosen(self) -> list:
        """The ticked ``(old, new)`` pairs."""
        return [(r.old, r.new) for i, r in enumerate(self._plan)
                if self.list.item(i).checkState() == Qt.Checked]


class InvestmentRegisterWidget(QWidget):
    """A per-account view for INVESTMENT accounts: their Buys/Sells/Divs from the
    ``investment_transactions`` table (which the cash RegisterWidget never shows),
    plus a header summarizing the account's market valuation (cash + securities).

    Kept API-compatible with :class:`RegisterWidget` so MainWindow can stack
    either in ``self._registers`` / ``self.stack`` interchangeably: it exposes a
    ``changed`` signal, a ``model`` with ``reload()``, and ``apply_display_prefs``
    / ``set_view_mode`` / ``select_txn``. Transactions are entered via the New
    button / context menu (:class:`InvestmentTransactionDialog`) and edited from
    the context menu; the model itself stays a read-only projection (edits go
    through :mod:`mammon.investments`, then ``reload()``). The Holdings button
    opens :class:`HoldingsDialog`; double- or right-clicking a Security cell charts
    that security's price history (any security ever traded, not just held ones).
    """

    changed = pyqtSignal()               # kept for the register-stack contract
    holdingsRequested = pyqtSignal(int)  # account_id -- Task 46 wires the window

    def __init__(self, conn, account_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = account_id
        self.model = InvestmentRegisterModel(conn, account_id)

        layout = QVBoxLayout(self)
        # Match the cash register's 8px inset so the title box lines up.
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Account title box (shares the cash register's QFrame styling) with the
        # market valuation summary tucked to the right.
        self.header_box = QFrame()
        self.header_box.setObjectName("registerTitleBox")
        header_layout = QHBoxLayout(self.header_box)
        header_layout.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel()
        self.header.setObjectName("registerTitle")
        header_layout.addWidget(self.header)
        header_layout.addStretch()
        self.valuation_label = QLabel()
        self.valuation_label.setObjectName("investmentValuation")
        header_layout.addWidget(self.valuation_label)

        # The account actions live in a GEAR beside the account name, as they do
        # in the cash register -- every one of them opens a dialog, and a toolbar
        # row that only leads elsewhere is a poor trade for register height.
        # AccountToolbar still OWNS the actions and their account-id wiring; it
        # is simply never shown.
        self.toolbar = AccountToolbar(account_id, self)
        self.toolbar.setVisible(False)
        self.gear_menu = _GearMenu(self)
        for act in self.toolbar.actions():
            self.gear_menu.addAction(act)
        self.gear_menu.addSeparator()
        self.act_quotes = self.gear_menu.addAction("Get Quotes…")
        self.act_quotes.setToolTip(
            "Fetch the latest close for every security held here and record it "
            "in price history.")
        self.act_quotes.triggered.connect(self.get_quotes)
        self.act_rename_security = self.gear_menu.addAction("Rename Security\u2026")
        self.act_rename_security.setToolTip(
            "Search and replace within this account's security names -- shorten "
            "a fund's full name to its ticker, or fuse two names for the same "
            "security.")
        self.act_rename_security.triggered.connect(self.rename_security)
        # The portfolio windows (roadmap item 7): what the register cannot show.
        self.gear_menu.addSeparator()
        self.act_lots = self.gear_menu.addAction("Lots…")
        self.act_lots.setToolTip("Every open tax lot: when it was bought, its cost, value, "
                                 "gain and holding period.")
        self.act_lots.triggered.connect(lambda: self._portfolio_dialog("LotsDialog"))
        self.act_gains = self.gear_menu.addAction("Capital Gains…")
        self.act_gains.setToolTip("Realized gains and losses from sales in a year, one row "
                                  "per lot, with short- and long-term totals.")
        self.act_gains.triggered.connect(lambda: self._portfolio_dialog("CapitalGainsDialog"))
        self.act_performance = self.gear_menu.addAction("Performance…")
        self.act_performance.setToolTip("Money-weighted return (IRR) of the account or one "
                                        "security over a period.")
        self.act_performance.triggered.connect(
            lambda: self._portfolio_dialog("PerformanceDialog"))
        self.act_allocation = self.gear_menu.addAction("Allocation…")
        self.act_allocation.setToolTip("Asset allocation by class, security and account, "
                                       "over the accounts the window is set to show.")
        self.act_allocation.triggered.connect(
            lambda: self._portfolio_dialog("AllocationDialog"))
        self.gear_button = QToolButton()
        self.gear_button.setObjectName("registerGear")
        self.gear_button.setText("\u2699")
        self.gear_button.setToolTip("Account actions")
        self.gear_button.setAutoRaise(True)
        self.gear_button.setPopupMode(QToolButton.InstantPopup)
        self.gear_button.setMenu(self.gear_menu)
        header_layout.addWidget(self.gear_button)
        layout.addWidget(self.header_box)

        # Same account-page action bar as the cash register (details/reconcile/
        # download-import/accounts/hide). Investment double-click stays bound to
        # the price-history chart below, so there is no split action here.

        # Security filter: pick one security to see only its transactions (with the
        # running Share Bal) plus a summary line -- running shares, dividend total,
        # cost basis, and P/L -- that RECONCILES to the Holdings window (both read
        # mammon.investments). '(All securities)' clears the filter.
        filter_bar = QHBoxLayout()
        filter_bar.addWidget(QLabel("Security:"))
        self.security_filter = QComboBox()
        self.security_filter.setObjectName("securityFilter")
        self.security_filter.setMinimumWidth(220)
        self._reload_security_filter()
        self.security_filter.currentIndexChanged.connect(self._on_security_filter_changed)
        filter_bar.addWidget(self.security_filter)
        filter_bar.addStretch()
        self.security_summary = QLabel()
        self.security_summary.setObjectName("securitySummary")
        filter_bar.addWidget(self.security_summary)
        layout.addLayout(filter_bar)

        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Posted rows stay read-only -- InvestmentRegisterModel.flags() only
        # marks the PENDING review row editable. Leaving the view on
        # NoEditTriggers meant that flag could never be exercised: the pending
        # row rendered as an editable row that refused to open an editor.
        self.view.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
            | QAbstractItemView.EditKeyPressed | QAbstractItemView.AnyKeyPressed)
        self._configure_columns()
        # Double-click a Security cell -> its price-history chart; right-click any
        # cell of a row with a security -> a 'Price history' menu. Reaches ANY
        # security ever traded here, not just the current holdings (Task 48).
        self.view.doubleClicked.connect(self._on_cell_double_clicked)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_view_context_menu)
        layout.addWidget(self.view)

        bar = QHBoxLayout()
        self.new_btn = QPushButton("New…")
        self.new_btn.clicked.connect(self.on_new)
        bar.addWidget(self.new_btn)
        self.holdings_btn = QPushButton("Holdings…")
        self.holdings_btn.clicked.connect(self._on_holdings)
        bar.addWidget(self.holdings_btn)
        bar.addStretch()
        self.balance_label = QLabel()
        bar.addWidget(self.balance_label)
        layout.addLayout(bar)

        # An investment import goes through the SAME review queue a cash import
        # does. Without this panel there was nowhere to review one, so investment
        # files were imported straight through -- and an unrecognised security
        # name went in unchallenged, creating a phantom security that then
        # collected the price history derived from the row. Reviewing is where
        # the security gets corrected, before any of that is written.
        self.review_panel = ImportReviewPanel(conn, account_id, self)
        self.review_panel.changed.connect(self._on_review_changed)
        self.review_panel.transactionSaved.connect(self._play_accepted)
        self.review_panel.visibility_changed.connect(self._reload_review)
        # Selecting a NEW review row opens it as an editable PENDING row at the
        # bottom of the register -- the same gesture the cash register offers,
        # and the one that makes an importer's action/security guess correctable
        # in place rather than through a separate dialog.
        self.review_panel.row_selected.connect(self._on_review_row_selected)
        self.review_panel.accept_new_requested.connect(
            lambda _entry: self._accept_pending())
        self._accept_btn = None
        self._accepting = False
        self.view.installEventFilter(self)
        layout.addWidget(self.review_panel)

        self._refresh_header()
        self._sync_review_action()

    # ---- import review ----------------------------------------------------
    def _play_accepted(self) -> None:
        sounds.play_accepted(prefs.sound_enabled())

    def _on_review_changed(self) -> None:
        """A reviewed row landed in the register: reload, revalue, and re-sync the
        pending row.

        The re-sync matters for the BULK operations. Discard All and Accept All
        empty the review list without touching the register's pending row, which
        was left pointing at an entry that no longer exists -- a row that could
        not be accepted (it posted nothing) and could not be deleted (the context
        menu reads it as pending, so it offers no Delete). It just sat there."""
        self.model.reload()
        self._open_pending_for_selection()
        self._refresh_header()
        self.changed.emit()

    def _reload_review(self, _mode=None) -> None:
        entries = import_review.load_review(
            self.conn, self.account_id, prefs.review_visibility(self.account_id))
        self.review_panel.set_entries(entries)
        self._open_pending_for_selection()
        self._sync_review_action()

    # ---- pending review row -----------------------------------------------
    def _on_review_row_selected(self, entry):
        """React to the review list's selection, as the cash register does:
        highlight the matched register line (MATCHING, or already-actioned) or
        open an editable pending row (NEW).

        Only the NEW branch existed here, so selecting a MATCHING row silently
        did nothing -- the user could see the review row claiming a match but had
        no way to see WHICH register line it matched, which is the one thing that
        makes accepting a match a judgement rather than a guess."""
        if entry is None or self.review_panel.isHidden():
            self._end_pending()
            return
        # An already-actioned row is history: point at what it produced.
        if getattr(entry, "is_actioned", False):
            self._end_pending()
            txn_id = (getattr(entry, "accepted_txn_id", None)
                      or entry.matched_txn_id)
            if txn_id is not None:
                self.select_txn(txn_id)
            return
        if entry.is_matching and entry.matched_txn_id is not None:
            self._end_pending()
            self.select_txn(entry.matched_txn_id)
            return
        if not getattr(entry, "is_new", False):
            self._end_pending()
            return
        self.model.set_pending(entry)
        row = self.model.pending_row()
        self._embed_accept_button(row)
        idx = self.model.index(row, InvestmentRegisterModel.SECURITY)
        self.view.setCurrentIndex(idx)
        self.view.scrollTo(idx)

    def _embed_accept_button(self, row):
        """Put an Accept button at the end of the pending row, as the cash
        register does, so the commit is where the editing is."""
        self._drop_accept_btn()
        self._accept_btn = QPushButton("Accept")
        self._accept_btn.setToolTip(
            "Commit this row into the register using the values above.")
        self._accept_btn.clicked.connect(self._accept_pending)
        self.view.setIndexWidget(
            self.model.index(row, InvestmentRegisterModel.CASH_BAL),
            self._accept_btn)

    def _drop_accept_btn(self):
        """Remove the inline Accept button, tolerating a C++ object Qt has
        already deleted out from under us. set_pending()'s begin/endResetModel
        drops index widgets, so re-seeding the pending row (which is what a
        second review-row selection does) leaves ``_accept_btn`` a dangling
        wrapper and deleteLater() raises. Always clears the Python reference so a
        later call can null-check it -- the cash register learned this first."""
        btn, self._accept_btn = self._accept_btn, None
        if btn is not None and not sip.isdeleted(btn):
            btn.deleteLater()

    def _end_pending(self):
        self._drop_accept_btn()
        self.model.clear_pending()

    def _accept_pending(self):
        """Commit the pending row through the review panel (the single
        import_review chokepoint), which drops the item and advances."""
        if self._accepting or not self.model.has_pending():
            return None
        self._accepting = True
        try:
            entry = self.model.pending_entry()
            values = self.model.pending_values()
            self._drop_accept_btn()
            self.model.clear_pending()
            return self.review_panel.accept_new(entry, values)
        finally:
            self._accepting = False

    def eventFilter(self, obj, event):
        """Enter on the pending row accepts it, committing any open editor first
        (an editable combo swallows Enter, so the typed value would revert)."""
        if (obj is self.view and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and self.model.has_pending()
                and self.model.is_pending_row(self._selected_row())):
            self.view.setFocus()
            self._accept_pending()
            return True
        return super().eventFilter(obj, event)

    def show_review(self, entries):
        """Load ``entries`` into the review panel and REVEAL it. An empty list
        clears any prior review without showing the panel.

        The reveal is the point: ImportReviewPanel hides itself on construction,
        so setting entries without showing left the rows in a panel the user
        never saw -- the import looked like it had done nothing at all."""
        self.review_panel.set_entries(entries)
        if entries:
            self.review_panel.show()
            # set_entries emits row_selected while the panel is still HIDDEN, so
            # the handler bailed and no pending row opened. Re-fire now that it
            # is visible.
            self._open_pending_for_selection()
        else:
            self.review_panel.hide()
        self._sync_review_action()

    def reopen_review(self):
        """Re-show a pending review list (the gear's Review... action) under this
        account's saved visibility -- the cash register's contract."""
        self.review_panel.reload_pending()
        self._open_pending_for_selection()
        self._sync_review_action()

    def _open_pending_for_selection(self):
        """Open the pending row for whatever the panel currently has selected.

        Every path that REPOPULATES the panel has to do this. Refilling the table
        re-selects the first row, but if that row was already selected Qt emits no
        itemSelectionChanged -- so nothing opened the pending row, and it appeared
        only after clicking away to another item and back. Re-firing the handler
        explicitly makes the row present the moment the list is.
        """
        if self.review_panel.isHidden():
            self._end_pending()
            return
        self._on_review_row_selected(self.review_panel.current_entry())

    def _sync_review_action(self):
        """Enable Review... only while the panel holds a pending review."""
        act = getattr(self.toolbar, "act_review", None)
        if act is not None:
            act.setEnabled(self.review_panel.has_pending())

    def _configure_columns(self):
        """classic widths: fixed Date + the five right-aligned numeric columns
        (Quantity/Price/Share Bal/Inv Amt/Cash Amt/Cash Bal), a tight interactive
        Action, and a stretched Security column."""
        hh = self.view.horizontalHeader()
        M = InvestmentRegisterModel
        fixed = {M.DATE: 84, M.QUANTITY: 84, M.PRICE: 90, M.SHARE_BAL: 90,
                 M.INV_AMT: 92, M.CASH_AMT: 92, M.CASH_BAL: 100}
        for col, width in fixed.items():
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.view.setColumnWidth(col, width)
        hh.setSectionResizeMode(M.ACTION, QHeaderView.Interactive)
        self.view.setColumnWidth(M.ACTION, 96)
        hh.setSectionResizeMode(M.SECURITY, QHeaderView.Stretch)

    def _refresh_header(self):
        """Re-read the account name and its market valuation (cash + securities).
        Valued as of the ledger's last activity -- the same basis the account bar
        and net worth use -- so the header total matches the sidebar balance."""
        self.header.setText(self.model.account_name())
        as_of = investments.valuation_as_of(self.conn)
        val = investments.account_valuation(self.conn, self.account_id, as_of)
        self.valuation_label.setText(
            f"Cash: {fmt_money(val.cash)}     "
            f"Securities: {fmt_money(val.securities)}     "
            f"Total: {fmt_money(val.total)}")
        self.balance_label.setText(f"Market Value: {fmt_money(val.total)}")

    # ---- security filter (per-security running shares / dividends / P/L) --
    def _reload_security_filter(self):
        """(Re)populate the security combo from the account's traded symbols,
        preserving the current selection when it still exists."""
        combo = self.security_filter
        prev = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("(All securities)", None)
        for sym in investments.symbols_used(self.conn, self.account_id):
            combo.addItem(sym, sym)
        idx = combo.findData(prev) if prev else 0
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _on_security_filter_changed(self, _index):
        symbol = self.security_filter.currentData()
        self.model.set_symbol_filter(symbol)
        self._refresh_security_summary()

    def _refresh_security_summary(self):
        """Show the selected security's running shares, dividend total, cost basis
        and P/L -- the SAME SecurityPosition the Holdings window reports, so the
        filtered register and Holdings always agree. Blank when no security is
        selected (the '(All securities)' view)."""
        symbol = self.security_filter.currentData()
        if not symbol:
            self.security_summary.setText("")
            return
        as_of = investments.valuation_as_of(self.conn)
        pos = investments.security_report(
            self.conn, self.account_id, symbol, as_of=as_of).position
        parts = [
            f"Shares: {fmt_qty(pos.quantity)}",
            f"Dividends: {fmt_money(pos.dividends)}",
            f"Cost Basis: {fmt_money(pos.cost_basis)}",
        ]
        if pos.is_open and pos.unrealized_pl is not None:
            parts.append(f"Unrealized P/L: {fmt_money(pos.unrealized_pl)}")
        if pos.realized_pl:
            parts.append(f"Realized P/L: {fmt_money(pos.realized_pl)}")
        parts.append(f"P/L: {fmt_money(pos.total_pl)}")
        self.security_summary.setText("     ".join(parts))

    def _on_holdings(self):
        """Open the account's holdings window (positions, cost basis, market
        value, gain/loss -- and a price-history chart per security on
        double-click). Still emits ``holdingsRequested`` first so a test (or a
        future listener) can observe the request without driving the modal."""
        self.holdingsRequested.emit(self.account_id)
        dlg = HoldingsDialog(self.conn, self.account_id, parent=self)
        dlg.exec_()

    # ---- price-history charts from the transaction rows ------------------
    def _symbol_at(self, index) -> str:
        """The security symbol for the register row under ``index`` -- blank for
        cash-only rows (a plain interest/dividend line with no security)."""
        if index is None or not index.isValid():
            return ""
        row = self.model.txn_at(index.row())
        return (row["symbol"] or "") if row else ""

    def _on_cell_double_clicked(self, index):
        """Double-clicking a Security cell charts that security's price history."""
        if index.column() == InvestmentRegisterModel.SECURITY:
            _chart_price_history(self, self.conn, self._symbol_at(index))

    def _on_view_context_menu(self, pos):
        """Right-click: New / Edit the selected transaction, plus the existing
        'Price history' action when the row names a security."""
        index = self.view.indexAt(pos)
        symbol = self._symbol_at(index)
        # Edit... applies to the PENDING row as much as to a posted one. The
        # dialog exists BECAUSE an investment transaction cannot be fully
        # expressed on a row -- the visible fields depend on the action, a
        # Reinvest carries per-income-type lines, a cash line names a category or
        # a transfer target. Offering inline editing on the pending row and then
        # withholding the dialog from it makes the register disagree with its own
        # reason for having the dialog. Delete stays posted-only: a pending row is
        # discarded from the review list, not deleted from the register.
        posted = index.isValid() and not self.model.is_pending_row(index.row())
        menu = QMenu(self)
        act_new = menu.addAction("New…")
        act_edit = menu.addAction("Edit…") if index.isValid() else None
        # Delete a posted transaction. An import inevitably brings rows that
        # should not exist -- a plan's "Change in Market Value" line, a
        # duplicate, a fee booked against the wrong security -- and until now
        # they could be edited but never removed.
        act_delete = menu.addAction("Delete") if posted else None
        act_price = None
        if symbol:
            menu.addSeparator()
            act_price = menu.addAction(f"Price history: {symbol}…")
        # A sale (or share removal) can name the lots it disposes of.
        act_lots = None
        if posted and symbol:
            row = self.model.txn_at(index.row())
            a = ((row["action"] if row else "") or "").strip().lower().replace(" ", "")
            if a in investments._REMOVE_ACTIONS:
                act_lots = menu.addAction("Specify Lots…")
        chosen = menu.exec_(self.view.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_new:
            self.on_new()
        elif act_edit is not None and chosen is act_edit:
            self._edit_row(index.row())
        elif act_delete is not None and chosen is act_delete:
            self._delete_row(index.row())
        elif act_price is not None and chosen is act_price:
            _chart_price_history(self, self.conn, symbol)
        elif act_lots is not None and chosen is act_lots:
            self._specify_lots(int(self.model.txn_at(index.row())["id"]))

    def _portfolio_dialog(self, name: str) -> None:
        """Open one of the portfolio windows (Lots, Capital Gains, Performance,
        Allocation) on this account; Allocation is not per-account -- it opens on
        the scope it remembers, the same window Reports ▸ Asset Allocation
        opens."""
        from mammon.ui import portfolio_dialogs
        cls = getattr(portfolio_dialogs, name)
        dlg = (cls(self.conn, parent=self) if name == "AllocationDialog"
               else cls(self.conn, self.account_id, parent=self))
        dlg.exec_()

    def _specify_lots(self, sale_txn_id: int) -> None:
        """Name the lots a sale disposes of; holdings and snapshots follow."""
        from mammon.ui.portfolio_dialogs import SpecifyLotsDialog
        dlg = SpecifyLotsDialog(self.conn, sale_txn_id, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self._after_write()

    # ---- new / edit an investment transaction ----------------------------
    def _selected_row(self) -> int:
        rows = self.view.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def on_new(self):
        dlg = InvestmentTransactionDialog(self.conn, self.account_id, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            v = dlg.values()
            investments.record_investment(
                self.conn, self.account_id, v["date"], v["action"],
                symbol=v["symbol"], quantity=v["quantity"], price=v["price"],
                amount=v["amount"], commission=v["commission"], memo=v["memo"],
                transfer_account_id=v["transfer_account_id"],
                split_num=v["split_num"], split_den=v["split_den"])
            self._after_write()

    def on_edit(self):
        self._edit_row(self._selected_row())

    def _edit_pending_row(self):
        """Open the full transaction dialog on the PENDING review row and write
        the result back into it. Nothing is committed -- Accept still does that --
        so the dialog is simply a richer way to fill in the same row."""
        if not self.model.has_pending():
            return
        v = self.model.pending_values()
        entry = self.model.pending_entry()
        seed = {
            "date": v.get("date"), "action": v.get("action"),
            "symbol": v.get("symbol"), "quantity": v.get("quantity"),
            "price": v.get("price"), "amount": v.get("amount_cents"),
            "commission": entry.mapped.commission_cents,
            "memo": entry.mapped.memo, "transfer_account_id": None,
        }
        dlg = InvestmentTransactionDialog(
            self.conn, self.account_id, txn=seed, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        got = dlg.values()
        M = InvestmentRegisterModel
        row = self.model.pending_row()
        for col, key in ((M.DATE, "date"), (M.ACTION, "action"),
                         (M.SECURITY, "symbol"), (M.QUANTITY, "quantity"),
                         (M.PRICE, "price")):
            self.model.setData(self.model.index(row, col),
                               got.get(key) or "", Qt.EditRole)
        if got.get("amount") is not None:
            self.model.setData(self.model.index(row, M.INV_AMT),
                               fmt_cents(abs(int(got["amount"]))), Qt.EditRole)
        # A cash line's DESCRIPTION comes back as memo (the dialog's Category /
        # Transfer picker writes free text there; see _read_catxfer). It has to
        # reach the row, or whatever the editor put there vanished the moment OK
        # was pressed.
        self.model.pending_set_memo(got.get("memo") or "")

    # ---- quotes -----------------------------------------------------------
    def quotable_holdings(self) -> tuple:
        """``(pairs, skipped)`` where ``pairs`` is ``[(holding_name, ticker)]``.

        A holding is a quote candidate only when its name yields a
        ticker-shaped leading token (:func:`investments.ticker_of`). A plan's
        internally-named funds -- "DOMESTIC BOND INDEX", "S&P 500 EQUITY INDEX"
        -- yield none and are reported as skipped.

        The pairing matters as much as the filter: ``price_history`` is keyed by
        the security name a holding is stored under (see
        :func:`investments.latest_price`), so a quote fetched for "ALTY" has to
        be recorded against "ALTY GLOBAL X SUPERDIVIDEND ALTER" to price the
        holding. Recording it under the bare ticker would look like it worked
        and value nothing."""
        pairs: list = []
        skipped: list = []
        for h in investments.list_holdings(self.conn, self.account_id):
            name = h["symbol"]
            if not name:
                continue
            try:
                if abs(Decimal(str(h["quantity"] or "0"))) == 0:
                    continue                      # closed position
            except (InvalidOperation, ValueError):
                pass
            tick = investments.ticker_of(name)
            if tick:
                pairs.append((name, tick))
            else:
                skipped.append(name)
        return pairs, skipped

    def get_quotes(self):
        """Gear menu: price the account's holdings from a quote source.

        The derived ticker is a GUESS and is shown for confirmation before any
        fetch. It has to be: a plan fund named "INTL EQUITY INDEX" yields INTL,
        which is a real listed company, so fetching it unasked would file a
        stranger's price against the user's holding and value the account
        wrongly. The user unticks those; nothing is fetched for them.

        The network stays behind investments.fetch_quotes' injectable
        QuoteSource -- a missing backend is reported as the setup step it is."""
        pairs, skipped = self.quotable_holdings()
        if not pairs:
            msg = "No securities here have a recognisable ticker."
            if skipped:
                msg += (chr(10) + chr(10) + "These are named internally and have "
                        "no public listing:" + chr(10) + "  "
                        + (chr(10) + "  ").join(skipped[:12]))
            QMessageBox.information(self, "Get Quotes", msg)
            return
        chosen, want_history = self._confirm_quote_targets(pairs, skipped)
        if not chosen:
            return
        # Hand the fetch the ticker -> holding-name mapping, so the quote is
        # written ONCE, under the name that values the holding. Passing only
        # tickers left a phantom two-row series filed under each bare ticker,
        # which is what a Security cell holding a ticker then charted.
        names: dict = {}
        for _n, _tk in chosen:
            names.setdefault(_tk.upper(), []).append(_n)
        try:
            quotes = self._fetch_quotes(sorted({tk for _n, tk in chosen}), names)
        except investments.QuoteSourceUnavailable as exc:
            QMessageBox.warning(
                self, "Get Quotes",
                "No quote source is configured." + chr(10) + chr(10) + str(exc))
            return
        except Exception as exc:                  # provider / network failure
            QMessageBox.warning(self, "Get Quotes",
                                "Could not fetch quotes: %s" % exc)
            return
        # The backfill is a separate, failable step: a provider that has no
        # history for one security must not lose the user the latest closes it
        # already returned, which are the numbers the register shows now.
        filled, history_error = 0, ""
        if want_history:
            try:
                filled = self._fetch_quote_history(chosen)
            except Exception as exc:
                history_error = str(exc)
        by_ticker = {q.symbol.upper(): q for q in quotes}
        priced, missing = [], []
        for name, tick in chosen:
            q = by_ticker.get(tick.upper())
            if q is None:
                missing.append(tick)
                continue
            # The row was already written under this holding's name by
            # fetch_quotes (it was handed the mapping); this loop only reports.
            priced.append(name)
        self.model.reload()
        self._refresh_header()
        self.changed.emit()
        lines = ["%d of %d securities priced." % (len(priced), len(chosen))]
        if want_history:
            lines.append("%d historical price%s filled in."
                         % (filled, "" if filled == 1 else "s"))
        if history_error:
            lines += ["", "History could not be fetched: " + history_error]
        if missing:
            lines += ["", "No quote returned for: " + ", ".join(sorted(set(missing)))]
        if skipped:
            lines += ["", "No ticker (not fetched): " + ", ".join(skipped[:8])]
        QMessageBox.information(self, "Get Quotes", chr(10).join(lines))

    def _confirm_quote_targets(self, pairs, skipped):
        """Ask which holdings to price, showing ``name -> ticker`` so a wrong
        guess is visible. Returns ``(chosen, history)`` -- the picked
        ``[(name, ticker)]`` (empty = cancelled) and whether to backfill a
        monthly series as well. Overridable so headless tests open no modal."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Get Quotes")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "Fetch the latest close for these holdings?" + chr(10)
            + "The ticker is taken from the security name -- untick any that is "
            "not the right symbol."))
        listw = QListWidget()
        for name, tick in pairs:
            it = QListWidgetItem("%s    \u2192  %s" % (name, tick))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            listw.addItem(it)
        listw.setMinimumWidth(420)
        lay.addWidget(listw)
        if skipped:
            note = QLabel("No ticker, not fetched: " + ", ".join(skipped[:6]))
            note.setWordWrap(True)
            lay.addWidget(note)
        history = QCheckBox("Also fetch monthly history back to each holding's "
                            "first transaction")
        history.setToolTip(
            "Net worth values a holding at the newest price on or before each "
            "sampled date, so sparsely recorded prices make the growth curve "
            "step on the dates prices happened to be recorded. A monthly series "
            "gives every holding the same resolution. Existing prices are never "
            "overwritten.")
        lay.addWidget(history)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec_() != QDialog.Accepted:
            return [], False
        return ([pairs[i] for i in range(listw.count())
                 if listw.item(i).checkState() == Qt.Checked],
                history.isChecked())

    # ---- renaming a security ----------------------------------------------
    def rename_security(self):
        """Gear menu: search/replace this account's security names.

        The rebuild lives in :func:`investments.apply_security_renames` -- a
        rename restates whole positions, so it is not left to the caller the way
        an ordinary row edit is -- hence _refresh_views here rather than
        _after_write, which would rebuild the account a second time."""
        pairs = self._ask_security_renames()
        if not pairs:
            return
        renamed = investments.apply_security_renames(
            self.conn, self.account_id, pairs)
        self._refresh_views()
        QMessageBox.information(
            self, "Rename Security",
            "%d transaction%s renamed, across %d securit%s." % (
                renamed, "" if renamed == 1 else "s",
                len(pairs), "y" if len(pairs) == 1 else "ies"))

    def _ask_security_renames(self):
        """Open the search/replace dialog, seeded with the security in view.
        Returns the chosen ``[(old, new)]`` (empty = cancelled). Overridable so
        headless tests drive the dialog directly and open no modal."""
        dlg = SecurityRenameDialog(self.conn, self.account_id, parent=self,
                                   initial=self._current_security())
        if dlg.exec_() != QDialog.Accepted:
            return []
        return dlg.chosen()

    def _current_security(self) -> str:
        """The security the register is looking at -- the filter's choice, else
        the selected row's -- so the dialog opens with it already in Find."""
        sym = self.security_filter.currentData()
        if sym:
            return sym
        txn = self.model.txn_at(self._selected_row())
        return (txn["symbol"] or "") if txn is not None else ""

    def _fetch_quotes(self, tickers, names=None):
        """Seam: the actual fetch. Overridden by headless tests so no test ever
        reaches the network (CLAUDE.md -- yfinance is not installed and quote
        tests inject fakes). ``names`` maps ticker -> holding names so the quote
        lands on the row that values the holding."""
        return investments.fetch_quotes(self.conn, tickers, names=names)

    def _fetch_quote_history(self, chosen):
        """Seam: backfill a monthly series for the chosen holdings.

        The range starts at each security's FIRST transaction in this account --
        nothing earlier can affect its valuation here, and asking a provider for
        twenty years of a stock bought last year is wasted. One request per
        distinct start date keeps that from becoming one request per holding."""
        firsts = investments.first_transaction_dates(self.conn, self.account_id)
        by_start: dict = {}
        for name, ticker in chosen:
            by_start.setdefault(firsts.get(name), []).append((name, ticker))
        total = 0
        for start, group in by_start.items():
            total += investments.fetch_quote_history(self.conn, group, start=start)
        return total

    def _delete_row(self, row):
        """Delete the selected investment transaction, after confirming."""
        txn = self.model.txn_at(row)
        if txn is None:
            return
        if txn.get("cash_leg"):
            QMessageBox.information(
                self, "Transfer leg",
                "This is a transfer from another account. Delete it in that "
                "account's register.")
            return
        bits = [fmt_date(txn.get("date")), txn.get("action"), txn.get("symbol")]
        label = "  ".join(str(b) for b in bits if b)
        if QMessageBox.question(
                self, "Delete transaction",
                "Delete this transaction?" + chr(10) + chr(10) + label,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        investments.delete_investment(self.conn, txn["id"])
        self._after_write()

    def _edit_row(self, row):
        # The PENDING review row has no investment_transactions id, so
        # txn_at() finds nothing for it and this returned silently -- Edit
        # appeared in the menu and did nothing. Route it to the buffer.
        if self.model.is_pending_row(row):
            self._edit_pending_row()
            return
        txn = self.model.txn_at(row)
        if txn is None:
            return
        # A cash-only transfer leg shown here lives in the `transactions` table
        # (a backfilled split mirror); its id is not an investment_transactions id,
        # so it must be edited from the OTHER account's register, not here.
        if txn.get("cash_leg"):
            QMessageBox.information(
                self, "Transfer leg",
                "This is a transfer from another account. Edit it in that "
                "account's register.")
            return
        full = investments.get_investment_txn(self.conn, txn["id"])
        dlg = InvestmentTransactionDialog(
            self.conn, self.account_id, txn=full, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            v = dlg.values()
            investments.update_investment(
                self.conn, txn["id"], v["date"], v["action"],
                symbol=v["symbol"], quantity=v["quantity"], price=v["price"],
                amount=v["amount"], commission=v["commission"], memo=v["memo"],
                transfer_account_id=v["transfer_account_id"],
                split_num=v["split_num"], split_den=v["split_den"])
            self._after_write()

    def _after_write(self):
        """Recompute holdings from the amended history and refresh the view."""
        investments.rebuild_holdings(self.conn, self.account_id)
        self._refresh_views()

    def _refresh_views(self):
        """Re-read the register, the security filter and the totals.

        Split out of _after_write for callers that have ALREADY rebuilt holdings
        -- a rename rebuilds inside the domain call, and replaying a 40-year
        account twice is not free."""
        self.model.reload()
        self._reload_security_filter()
        self._refresh_header()
        self._refresh_security_summary()
        self.changed.emit()

    # ---- MainWindow register-stack contract ------------------------------
    def apply_display_prefs(self, mode: str | None = None) -> None:
        """Re-apply the register font + alternating-row shading to this view.
        ``mode`` (the one/two-line choice) does not apply to an investment
        register, so it is accepted and ignored for a uniform call site."""
        self.view.setAlternatingRowColors(prefs.row_shading())
        font = QFont(prefs.font_family(), prefs.font_size())
        self.view.setFont(font)
        self.view.horizontalHeader().setFont(font)
        self._refresh_header()
        self.view.viewport().update()

    def set_view_mode(self, mode: str) -> None:
        """No-op: investment registers have no one/two-line variants."""
        return

    def scroll_to_newest(self) -> None:
        """Scroll to the newest (bottom) activity row on first open, matching
        the cash register's behaviour (by request)."""
        self.view.scrollToBottom()

    def has_open_editor(self) -> bool:
        """True while a cell editor is open on this register."""
        try:
            view = getattr(self, "view", None)
            return view is not None and view.state() == QAbstractItemView.EditingState
        except Exception:              # pragma: no cover - defensive
            return False

    def _open_editor(self):
        """The live editor widget, or None. Children of the VIEWPORT only."""
        view = getattr(self, "view", None)
        if view is None:
            return None
        editor = view.viewport().focusWidget()
        if editor is None:
            kids = [w for w in view.viewport().children() if isinstance(w, QWidget)]
            editor = kids[-1] if kids else None
        return editor

    def discard_open_editor(self) -> bool:
        """Close the editor WITHOUT writing its value back to the model."""
        try:
            if not self.has_open_editor():
                return False
            editor = self._open_editor()
            if editor is None:
                return False
            self.view.closeEditor(editor, QAbstractItemDelegate.RevertModelCache)
            return True
        except Exception:              # pragma: no cover - never block a switch
            return False

    def commit_open_editor(self) -> bool:
        """Commit and close any cell editor this register has open.

        Switching the stacked widget away from a view with a LIVE editor is a
        hard (C++) crash, not a Python exception -- which is why it left no
        traceback and no crash-log entry. The delegate's editor is reparented and
        destroyed while it is still mid-commit, and the write lands on a view
        that is no longer current.

        Returns True if an editor was actually closed."""
        try:
            view = getattr(self, "view", None)
            if view is None or view.state() != QAbstractItemView.EditingState:
                return False
            # focusWidget() alone is NOT enough. Clicking the accounts list moves
            # focus OUT of the editor before this runs, so the editor is still
            # alive and open while focusWidget() already returns None -- the first
            # version of this guard bailed out there and the crash survived. The
            # editor is a child of the viewport either way, so fall back to that.
            editor = view.viewport().focusWidget()
            if editor is None:
                # NOT filtered on isVisible(): inside hideEvent the children are
                # already hidden, so a visibility test finds nothing and the
                # editor survives into the teardown that crashes. Direct children
                # of a table viewport are the delegate's editor and little else.
                kids = [w for w in view.viewport().children()
                        if isinstance(w, QWidget)]
                editor = kids[-1] if kids else None
            if editor is None:
                return False
            view.commitData(editor)
            view.closeEditor(editor, QAbstractItemDelegate.NoHint)
            return True
        except Exception:              # pragma: no cover - never block a switch
            return False

    def hideEvent(self, event):
        """Close any live editor as this register is hidden.

        The hook lives HERE rather than at each switch site because every way of
        leaving a register -- the accounts list, the stack falling back to its
        placeholder, a future navigation path nobody has written yet -- ends in
        the widget being hidden. Guarding one caller only fixed one route."""
        self.commit_open_editor()
        super().hideEvent(event)

    def select_txn(self, txn_id) -> bool:
        """Select and scroll to an investment transaction by id -- the Find
        dialog lands here when a security/amount/memo hit is in this account. If
        an active security filter hides the target, clear it (back to "(All
        securities)") and retry so cross-security results still resolve."""
        row = self.model.row_for_txn(txn_id)
        if row < 0 and self.security_filter.currentIndex() != 0:
            self.security_filter.setCurrentIndex(0)  # -> _on_security_filter_changed reloads
            row = self.model.row_for_txn(txn_id)
        if row < 0:
            return False
        idx = self.model.index(row, 0)
        self.view.setCurrentIndex(idx)
        self.view.selectRow(row)
        self.view.scrollTo(idx, QAbstractItemView.PositionAtCenter)
        return True


# ---------------------------------------------------------------------------
# price-history chart (shared by the register + the holdings window)
# ---------------------------------------------------------------------------
def _chart_price_history(parent, conn, symbol):
    """Open a line chart of ``symbol``'s recorded price history, or an info note
    when nothing is recorded (never an empty chart). Shared by HoldingsDialog
    (double/right-click a holding) and InvestmentRegisterWidget (double/right-click
    a Security cell) so both entry points behave identically; the matplotlib chart
    classes are imported LAZILY here so base widgets stay matplotlib-free."""
    if not symbol:
        return
    # Bounds included, so a derived price plots its uncertainty (PriceHistoryCanvas).
    points = investments.price_history_bounds(conn, symbol)
    if not points:
        QMessageBox.information(
            parent, "Price History",
            f"No recorded price history for {symbol} yet.")
        return
    from mammon.ui.charts import ChartDialog, PriceHistoryCanvas
    canvas = PriceHistoryCanvas(symbol, points)
    ChartDialog(f"Price History - {symbol}", canvas, parent=parent).exec_()


def _chart_loan_projection(parent, conn, account_id):
    """Open the projected principal-payoff chart for a loan account: the
    outstanding balance declining into the future, read from the amortization
    schedule (:func:`mammon.loans.amortization_schedule`). Lazy matplotlib import
    keeps mammon.ui cheap/headless until a chart is actually requested."""
    schedule = loans.amortization_schedule(conn, account_id)
    from mammon.ui.charts import ChartDialog, LoanProjectionCanvas
    canvas = LoanProjectionCanvas(schedule)
    ChartDialog("Principal Projection", canvas, parent=parent).exec_()


# ---------------------------------------------------------------------------
# holdings window (positions + gain/loss, price chart on double/right-click)
# ---------------------------------------------------------------------------
class HoldingsDialog(QDialog):
    """The positions in one investment account: Symbol | Shares | Cost Basis |
    Price | Market Value | Gain/Loss, valued at each holding's latest recorded
    price as of the ledger's last activity (the SAME basis the account bar and
    the register's valuation header use, so the totals agree).

    The last row of Currently Held is CASH, and the footer totals cash plus
    securities. An investment account's displayed balance is its full market
    valuation (investments.display_balance), so a holdings window listing only
    securities disagreed with the very number in the accounts list the user
    clicked to get here -- by the account's whole cash position, with nothing on
    screen to explain the difference.

    Holdings are rebuilt from the account's investment transactions on open, so
    the table always reflects the current lots. A negative Gain/Loss paints red;
    an UNPRICED holding (no recorded price) shows blank Price / Market Value /
    Gain-Loss, matching Quicken. Double-clicking a holding opens its price-history
    line chart -- or, when nothing is recorded for that security, an informational
    message rather than an empty chart."""

    # Currently-Held tab columns. (Dividends slots in before Gain/Loss; the
    # older constant names/indices for Symbol..Market are unchanged.)
    # Symbol is the IDENTITY (the ticker) and Description is what it is -- two
    # columns because they are two facts (SRD 5.8e-2). One column carrying
    # "VGT VANGUARD INFO TECH ETF" is how a bare ticker from a later import had
    # nowhere to go but a second security.
    SYMBOL, DESCRIPTION, SHARES, COST, PRICE, MARKET, DIVIDENDS, GAIN = range(8)
    HEADERS = ["Symbol", "Description", "Shares", "Cost Basis", "Price",
               "Market Value", "Dividends", "Gain/Loss"]
    # Previously-Held tab columns: a sold-out position keeps no shares/market
    # value, so it reports its symbol, dividend total, and realized P/L only.
    C_SYMBOL, C_DESCRIPTION, C_DIV, C_PL = range(4)
    CLOSED_HEADERS = ["Symbol", "Description", "Dividends", "Realized P/L"]

    def __init__(self, conn, account_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = account_id
        acct = ledger.get_account(conn, account_id)
        self.account_name = acct["name"] if acct else ""
        self.setWindowTitle(
            f"Holdings - {self.account_name}" if self.account_name else "Holdings")

        # Value as of the ledger's last activity, not the newest quote on record
        # (which for a 1997 snapshot would be a far-later price).
        self.as_of = investments.valuation_as_of(conn)
        # Rebuild lots from the transactions so the table reflects current holdings.
        investments.rebuild_holdings(conn, account_id)
        # One replay feeds both tabs (and reconciles to the security-filtered
        # register): currently-held positions tie to holding_values row-for-row;
        # previously-held ones (now zero shares) carry their realized P/L.
        self._held = investments.held_positions(conn, account_id, as_of=self.as_of)
        self._closed = investments.closed_positions(conn, account_id, as_of=self.as_of)
        # The one number the accounts list shows for this account. Taken from the
        # same function it uses rather than re-added here, so the two cannot drift.
        self.valuation = investments.account_valuation(conn, account_id, self.as_of)

        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()

        # --- Currently Held tab (self.table kept as the public handle) ---
        self.table = QTableWidget(len(self._held) + 1, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(self.SYMBOL, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(self.DESCRIPTION, QHeaderView.Stretch)
        for col in (self.SHARES, self.COST, self.PRICE, self.MARKET,
                    self.DIVIDENDS, self.GAIN):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self._fill_table()
        self.tabs.addTab(self.table, f"Currently Held ({len(self._held)})")

        # --- Previously Held tab (sold-out positions + realized P/L) ---
        self.closed_table = QTableWidget(len(self._closed), len(self.CLOSED_HEADERS))
        self.closed_table.setHorizontalHeaderLabels(self.CLOSED_HEADERS)
        self.closed_table.verticalHeader().setVisible(False)
        self.closed_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.closed_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.closed_table.setSelectionMode(QAbstractItemView.SingleSelection)
        ch = self.closed_table.horizontalHeader()
        ch.setSectionResizeMode(self.C_SYMBOL, QHeaderView.ResizeToContents)
        ch.setSectionResizeMode(self.C_DESCRIPTION, QHeaderView.Stretch)
        for col in (self.C_DIV, self.C_PL):
            ch.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.closed_table.cellDoubleClicked.connect(self._on_closed_double_clicked)
        self.closed_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.closed_table.customContextMenuRequested.connect(self._on_closed_context_menu)
        self._fill_closed_table()
        self.tabs.addTab(self.closed_table, f"Previously Held ({len(self._closed)})")

        outer.addWidget(self.tabs)

        # Footer: the three figures the account bar is built from. Total is the
        # accounts-list balance for this account.
        securities = sum(p.market_value for p in self._held)
        self.total_label = QLabel(
            f"Securities: {fmt_money(securities)}     "
            f"Cash: {fmt_money(self.valuation.cash)}     "
            f"Total: {fmt_money(self.valuation.total)}")
        self.total_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.total_label.setStyleSheet("font-weight: bold;")
        outer.addWidget(self.total_label)

        hint = QLabel("Double-click a holding to chart its price history.")
        hint.setObjectName("registerSub")
        outer.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)
        self.resize(620, 400)

    def _description_cell(self, symbol):
        """The security's DESCRIPTION as its own cell.

        A table has room a dense register does not, so this is a column rather
        than a tooltip: two facts, two columns (SRD 5.8e-2). Falls back to blank
        rather than repeating the symbol, so an unsplit security reads as
        "description not recorded yet" instead of pretending to carry one."""
        from mammon import securities as _sec
        name = _sec.name_of(self.conn, symbol)
        return self._cell(name or "")

    def _fill_table(self):
        for i, p in enumerate(self._held):
            priced = p.price is not None
            sym = self._cell(p.symbol)
            sym.setData(Qt.UserRole, p.symbol)
            self.table.setItem(i, self.SYMBOL, sym)
            self.table.setItem(i, self.DESCRIPTION,
                               self._description_cell(p.symbol))
            self.table.setItem(i, self.SHARES, self._cell(fmt_qty(p.quantity), right=True))
            self.table.setItem(i, self.COST, self._cell(fmt_cents(p.cost_basis), right=True))
            # Unpriced holding: blank Price / Market Value / Gain-Loss (Quicken).
            self.table.setItem(
                i, self.PRICE,
                self._cell(fmt_qty(p.price) if priced else "", right=True))
            self.table.setItem(
                i, self.MARKET,
                self._cell(fmt_cents(p.market_value) if priced else "", right=True))
            self.table.setItem(
                i, self.DIVIDENDS, self._cell(fmt_cents(p.dividends), right=True))
            gain = self._cell(
                fmt_cents(p.unrealized_pl) if p.unrealized_pl is not None else "",
                right=True)
            if p.unrealized_pl is not None and p.unrealized_pl < 0:
                gain.setForeground(QBrush(QColor(style.negative_color())))
            self.table.setItem(i, self.GAIN, gain)
        self._fill_cash_row(len(self._held))

    def _fill_cash_row(self, row):
        """The account's uninvested cash, as the last row of Currently Held.

        Shares/Cost/Price/Dividends/Gain stay blank: cash has no lot, no basis
        and no gain, and filling them with zeros would invite adding them into
        the cost and gain columns. It carries no symbol in Qt.UserRole either, so
        double-click and the price-history menu pass over it (_chart_symbol
        ignores a blank symbol) -- there is no price history for cash."""
        cash = self._cell("Cash")
        font = cash.font()
        font.setItalic(True)
        cash.setFont(font)
        self.table.setItem(row, self.SYMBOL, cash)
        for col in (self.DESCRIPTION, self.SHARES, self.COST, self.PRICE,
                    self.DIVIDENDS, self.GAIN):
            self.table.setItem(row, col, self._cell(""))
        market = self._cell(fmt_cents(self.valuation.cash), right=True)
        if self.valuation.cash < 0:
            market.setForeground(QBrush(QColor(style.negative_color())))
        self.table.setItem(row, self.MARKET, market)

    def _fill_closed_table(self):
        for i, p in enumerate(self._closed):
            sym = self._cell(p.symbol)
            sym.setData(Qt.UserRole, p.symbol)
            self.closed_table.setItem(i, self.C_SYMBOL, sym)
            self.closed_table.setItem(i, self.C_DESCRIPTION,
                                      self._description_cell(p.symbol))
            self.closed_table.setItem(
                i, self.C_DIV, self._cell(fmt_cents(p.dividends), right=True))
            pl = self._cell(fmt_cents(p.realized_pl), right=True)
            if p.realized_pl < 0:
                pl.setForeground(QBrush(QColor(style.negative_color())))
            self.closed_table.setItem(i, self.C_PL, pl)

    @staticmethod
    def _cell(text, right=False):
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if right:
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return item

    def _on_row_double_clicked(self, row, _col):
        self._chart_row(row)

    def _on_context_menu(self, pos):
        """Right-click a holding -> a 'Price history' action (parity with the
        double-click, and with the register's Security-cell menu)."""
        self._context_menu_for(self.table, pos)

    def _on_closed_double_clicked(self, row, _col):
        self._chart_symbol(self._symbol_at(self.closed_table, row))

    def _on_closed_context_menu(self, pos):
        self._context_menu_for(self.closed_table, pos)

    def _context_menu_for(self, table, pos):
        item = table.itemAt(pos)
        if item is None:
            return
        symbol = self._symbol_at(table, item.row())
        if not symbol:
            return
        menu = QMenu(self)
        act = menu.addAction(f"Price history: {symbol}…")
        if menu.exec_(table.viewport().mapToGlobal(pos)) is act:
            self.show_price_history(symbol)

    @staticmethod
    def _symbol_at(table, row):
        item = table.item(row, 0)          # symbol is column 0 in both tabs
        return item.data(Qt.UserRole) if item else None

    def _chart_row(self, row):
        self._chart_symbol(self._symbol_at(self.table, row))

    def _chart_symbol(self, symbol):
        if symbol:
            self.show_price_history(symbol)

    def show_price_history(self, symbol):
        """Chart ``symbol``'s price history (or an info note when nothing is
        recorded). Delegates to the shared helper so this and the investment
        register's Security-cell path never diverge."""
        _chart_price_history(self, self.conn, symbol)


# ---------------------------------------------------------------------------
# crypto holdings window (coin positions + current valuation)
# ---------------------------------------------------------------------------
class CryptoHoldingsDialog(QDialog):
    """The coin positions in one crypto wallet: Coin | Quantity | Cost Basis |
    Price | Market Value | Gain/Loss, valued at each coin's latest recorded USD
    price (the shared ``{SYM}-USD`` price path) as of the ledger's last activity
    -- the SAME basis the account bar and the register header use, so the totals
    agree. The crypto twin of :class:`HoldingsDialog`.

    The last row is CASH (the fiat cash sleeve), and the footer totals cash plus
    coins to the account's displayed balance. An UNPRICED coin (no recorded
    quote) shows blank Price / Market Value / Gain-Loss. Everything is read
    through :mod:`mammon.crypto`; the dialog holds no SQL and no money math."""

    SYMBOL, QUANTITY, COST, PRICE, MARKET, GAIN = range(6)
    HEADERS = ["Coin", "Quantity", "Cost Basis", "Price", "Market Value",
               "Gain/Loss"]

    def __init__(self, conn, account_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = account_id
        acct = ledger.get_account(conn, account_id)
        self.account_name = acct["name"] if acct else ""
        self.setWindowTitle(
            f"Holdings - {self.account_name}" if self.account_name else "Holdings")

        # Value as of the ledger's last activity (not the newest quote on record).
        self.as_of = crypto.valuation_as_of(conn)
        # Rebuild the replay cache so the table reflects the current lots.
        crypto.rebuild_holdings(conn, account_id)
        self._held = crypto.holding_values(conn, account_id, as_of=self.as_of)
        # The one number the accounts list shows for this account, from the same
        # function it uses -- so the two cannot drift.
        self.valuation = crypto.account_valuation(conn, account_id, self.as_of)

        outer = QVBoxLayout(self)
        self.table = QTableWidget(len(self._held) + 1, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(self.SYMBOL, QHeaderView.Stretch)
        for col in (self.QUANTITY, self.COST, self.PRICE, self.MARKET, self.GAIN):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self._fill_table()
        outer.addWidget(self.table)

        # Read the coin total straight off the AccountValuation the accounts list
        # uses -- no re-summing in the view, so the footer cannot drift from it
        # (Coins + Cash == Total by construction).
        self.total_label = QLabel(
            f"Coins: {fmt_money(self.valuation.securities)}     "
            f"Cash: {fmt_money(self.valuation.cash)}     "
            f"Total: {fmt_money(self.valuation.total)}")
        self.total_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.total_label.setStyleSheet("font-weight: bold;")
        outer.addWidget(self.total_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)
        self.resize(560, 360)

    def _fill_table(self):
        for i, h in enumerate(self._held):
            priced = h.price is not None
            self.table.setItem(i, self.SYMBOL, self._cell(h.symbol))
            self.table.setItem(
                i, self.QUANTITY, self._cell(fmt_qty(h.quantity), right=True))
            self.table.setItem(
                i, self.COST, self._cell(fmt_cents(h.cost_basis), right=True))
            self.table.setItem(
                i, self.PRICE,
                self._cell(fmt_qty(h.price) if priced else "", right=True))
            self.table.setItem(
                i, self.MARKET,
                self._cell(fmt_cents(h.market_value) if priced else "", right=True))
            gain = self._cell(
                fmt_cents(h.gain) if h.gain is not None else "", right=True)
            if h.gain is not None and h.gain < 0:
                gain.setForeground(QBrush(QColor(style.negative_color())))
            self.table.setItem(i, self.GAIN, gain)
        self._fill_cash_row(len(self._held))

    def _fill_cash_row(self, row):
        """The wallet's fiat cash sleeve, as the last row. Quantity/Cost/Price/
        Gain stay blank -- cash has no lot, basis or gain."""
        cash = self._cell("Cash")
        font = cash.font()
        font.setItalic(True)
        cash.setFont(font)
        self.table.setItem(row, self.SYMBOL, cash)
        for col in (self.QUANTITY, self.COST, self.PRICE, self.GAIN):
            self.table.setItem(row, col, self._cell(""))
        market = self._cell(fmt_cents(self.valuation.cash), right=True)
        if self.valuation.cash < 0:
            market.setForeground(QBrush(QColor(style.negative_color())))
        self.table.setItem(row, self.MARKET, market)

    @staticmethod
    def _cell(text, right=False):
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if right:
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return item


# ---------------------------------------------------------------------------
# crypto register (crypto_transactions activity: buys, swaps, transfers, gas)
# ---------------------------------------------------------------------------
class CryptoRegisterWidget(QWidget):
    """A per-account view for CRYPTO wallets: their Buys/Sells, coin-for-coin
    Swaps (the paired SWAP_OUT/SWAP_IN legs), same-coin wallet Transfers (the
    mirror model), income and gas Fees from the ``crypto_transactions`` table --
    plus a header summarizing the wallet's market valuation (cash + coins). The
    crypto twin of :class:`InvestmentRegisterWidget`.

    Kept API-compatible with :class:`RegisterWidget` so MainWindow can stack it in
    ``self._registers`` / ``self.stack`` interchangeably: it exposes a ``changed``
    signal, a ``model`` with ``reload()``, and ``apply_display_prefs`` /
    ``set_view_mode`` / ``select_txn``. The model is a READ-ONLY projection over
    :mod:`mammon.crypto` (events are entered by import); Get Quotes prices the
    coins and the Holdings button opens :class:`CryptoHoldingsDialog`."""

    changed = pyqtSignal()               # kept for the register-stack contract
    holdingsRequested = pyqtSignal(int)  # account_id

    def __init__(self, conn, account_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = account_id
        self.model = CryptoRegisterModel(conn, account_id)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Account title box (shares the cash register's QFrame styling) with the
        # market valuation summary to the right.
        self.header_box = QFrame()
        self.header_box.setObjectName("registerTitleBox")
        header_layout = QHBoxLayout(self.header_box)
        header_layout.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel()
        self.header.setObjectName("registerTitle")
        header_layout.addWidget(self.header)
        header_layout.addStretch()
        self.valuation_label = QLabel()
        self.valuation_label.setObjectName("investmentValuation")
        header_layout.addWidget(self.valuation_label)

        # Account actions live in a GEAR beside the account name (as in the cash
        # and investment registers). AccountToolbar OWNS the actions and their
        # account-id wiring; it is never shown.
        self.toolbar = AccountToolbar(account_id, self)
        self.toolbar.setVisible(False)
        self.gear_menu = _GearMenu(self)
        for act in self.toolbar.actions():
            self.gear_menu.addAction(act)
        self.gear_menu.addSeparator()
        self.act_quotes = self.gear_menu.addAction("Get Quotes…")
        self.act_quotes.setToolTip(
            "Fetch the latest USD close for every coin held here and record it "
            "in price history.")
        self.act_quotes.triggered.connect(self.get_quotes)
        self.gear_button = QToolButton()
        self.gear_button.setObjectName("registerGear")
        self.gear_button.setText("⚙")
        self.gear_button.setToolTip("Account actions")
        self.gear_button.setAutoRaise(True)
        self.gear_button.setPopupMode(QToolButton.InstantPopup)
        self.gear_button.setMenu(self.gear_menu)
        header_layout.addWidget(self.gear_button)
        layout.addWidget(self.header_box)

        # Coin filter: pick one coin to see only its transactions (with the
        # running Coin Bal). '(All coins)' clears the filter.
        filter_bar = QHBoxLayout()
        filter_bar.addWidget(QLabel("Coin:"))
        self.coin_filter = QComboBox()
        self.coin_filter.setObjectName("coinFilter")
        self.coin_filter.setMinimumWidth(180)
        self._reload_coin_filter()
        self.coin_filter.currentIndexChanged.connect(self._on_coin_filter_changed)
        filter_bar.addWidget(self.coin_filter)
        filter_bar.addStretch()
        layout.addLayout(filter_bar)

        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().setVisible(False)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._configure_columns()
        layout.addWidget(self.view)

        bar = QHBoxLayout()
        self.holdings_btn = QPushButton("Holdings…")
        self.holdings_btn.clicked.connect(self._on_holdings)
        bar.addWidget(self.holdings_btn)
        bar.addStretch()
        self.balance_label = QLabel()
        bar.addWidget(self.balance_label)
        layout.addLayout(bar)

        self._refresh_header()

    def _configure_columns(self):
        """Fixed widths for Date + the right-aligned numeric columns, a tight
        interactive Action, a stretched Coin/Wallet column, and a fixed Fee."""
        hh = self.view.horizontalHeader()
        M = CryptoRegisterModel
        fixed = {M.DATE: 84, M.QUANTITY: 100, M.PRICE: 96, M.COIN_BAL: 100,
                 M.AMOUNT: 96, M.CASH_BAL: 100, M.FEE: 96}
        for col, width in fixed.items():
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.view.setColumnWidth(col, width)
        hh.setSectionResizeMode(M.ACTION, QHeaderView.Interactive)
        self.view.setColumnWidth(M.ACTION, 104)
        hh.setSectionResizeMode(M.COIN, QHeaderView.Stretch)

    def _refresh_header(self):
        """Re-read the account name and its market valuation (cash + coins),
        valued as of the ledger's last activity -- the same basis the account bar
        and net worth use, so the header total matches the sidebar balance."""
        self.header.setText(self.model.account_name())
        as_of = crypto.valuation_as_of(self.conn)
        val = crypto.account_valuation(self.conn, self.account_id, as_of)
        self.valuation_label.setText(
            f"Cash: {fmt_money(val.cash)}     "
            f"Coins: {fmt_money(val.securities)}     "
            f"Total: {fmt_money(val.total)}")
        self.balance_label.setText(f"Market Value: {fmt_money(val.total)}")

    # ---- coin filter -----------------------------------------------------
    def _reload_coin_filter(self):
        combo = self.coin_filter
        prev = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("(All coins)", None)
        for sym in crypto.symbols_used(self.conn, self.account_id):
            combo.addItem(sym, sym)
        idx = combo.findData(prev) if prev else 0
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _on_coin_filter_changed(self, _index):
        self.model.set_symbol_filter(self.coin_filter.currentData())

    def _on_holdings(self):
        """Open the wallet's holdings window (coin positions, cost basis, market
        value, gain/loss). Emits ``holdingsRequested`` first so a test (or a
        future listener) can observe the request without driving the modal."""
        self.holdingsRequested.emit(self.account_id)
        dlg = CryptoHoldingsDialog(self.conn, self.account_id, parent=self)
        dlg.exec_()

    def get_quotes(self):
        """Gear menu: price the wallet's coins. A coin symbol IS its ticker
        (``ETH`` -> ``ETH-USD``), so unlike equities there is no name-to-ticker
        guess to confirm; the network stays behind ``crypto.fetch_quotes``'
        injectable source (a missing backend is reported as the setup step it
        is)."""
        symbols = crypto.symbols_used(self.conn, self.account_id)
        if not symbols:
            QMessageBox.information(self, "Get Quotes",
                                    "No coins here to price yet.")
            return
        try:
            crypto.fetch_quotes(self.conn, symbols)
        except investments.QuoteSourceUnavailable as exc:
            QMessageBox.warning(
                self, "Get Quotes",
                "No quote source is configured." + chr(10) + chr(10) + str(exc))
            return
        except Exception as exc:                  # provider / network failure
            QMessageBox.warning(self, "Get Quotes",
                                "Could not fetch quotes: %s" % exc)
            return
        self.model.reload()
        self._refresh_header()
        self.changed.emit()
        QMessageBox.information(
            self, "Get Quotes",
            "Priced %d coin%s." % (len(symbols), "" if len(symbols) == 1 else "s"))

    # ---- MainWindow register-stack contract ------------------------------
    def apply_display_prefs(self, mode: str | None = None) -> None:
        """Re-apply the register font + alternating-row shading. ``mode`` (the
        one/two-line choice) does not apply to a crypto register, so it is
        accepted and ignored for a uniform call site."""
        self.view.setAlternatingRowColors(prefs.row_shading())
        font = QFont(prefs.font_family(), prefs.font_size())
        self.view.setFont(font)
        self.view.horizontalHeader().setFont(font)
        self._refresh_header()
        self.view.viewport().update()

    def set_view_mode(self, mode: str) -> None:
        """No-op: crypto registers have no one/two-line variants."""
        return

    def scroll_to_newest(self) -> None:
        """Scroll to the newest (bottom) activity row on first open."""
        self.view.scrollToBottom()

    def has_open_editor(self) -> bool:
        """Always False: the crypto register is read-only, so no cell editor
        ever opens (and the stack never needs to resolve one on the way out)."""
        return False

    def refresh(self) -> None:
        """Re-read the register, the coin filter and the header totals."""
        self.model.reload()
        self._reload_coin_filter()
        self._refresh_header()

    def select_txn(self, txn_id) -> bool:
        """Select and scroll to a crypto event by id -- where the Find dialog
        lands for a hit in this account. Clears an active coin filter that hides
        the target and retries, so cross-coin results still resolve."""
        row = self.model.row_for_txn(txn_id)
        if row < 0 and self.coin_filter.currentIndex() != 0:
            self.coin_filter.setCurrentIndex(0)   # -> reloads unfiltered
            row = self.model.row_for_txn(txn_id)
        if row < 0:
            return False
        idx = self.model.index(row, 0)
        self.view.setCurrentIndex(idx)
        self.view.selectRow(row)
        self.view.scrollTo(idx, QAbstractItemView.PositionAtCenter)
        return True


# ---------------------------------------------------------------------------
# new-account dialog
# ---------------------------------------------------------------------------
class NewAccountDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New account")
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.type = QComboBox()
        self.type.addItems(_ACCOUNT_TYPES)
        self.opening = QDoubleSpinBox()
        self.opening.setRange(-1_000_000_000, 1_000_000_000)
        self.opening.setDecimals(2)
        # Optional opening date: a calendar-backed editor shown in the user's
        # date-format preference (never a hardcoded YYYY-MM-DD), read back to ISO
        # by date_edit_iso -- like ReconcileStartDialog. blank_ok + starting at the
        # sentinel minimum keeps it empty until the user sets one, so an omitted
        # opening date stays None.
        self.opening_date = make_date_edit(blank_ok=True)
        self.opening_date.setDate(self.opening_date.minimumDate())
        form.addRow("Name", self.name)
        form.addRow("Type", self.type)
        form.addRow("Opening balance", self.opening)
        form.addRow("Opening date", self.opening_date)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return {
            "name": self.name.text().strip(),
            "type": self.type.currentText(),
            "opening_balance": parse_amount(str(self.opening.value())),
            "opening_date": date_edit_iso(self.opening_date) or None,
        }


# ---------------------------------------------------------------------------
# edit-account-details dialog (Settings > Edit Account Details)
# ---------------------------------------------------------------------------
class EditAccountDialog(QDialog):
    """Edit an existing account's name, type, institution, note, and open/closed
    state. Opening balance is deliberately not edited here (it changes every
    running balance) -- that is a separate, explicit action."""

    def __init__(self, account, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit account details")
        form = QFormLayout(self)
        self.name = QLineEdit(account["name"] or "")
        self.type = QComboBox()
        self.type.addItems(_ACCOUNT_TYPES)
        i = self.type.findText(account["type"] or "")
        if i >= 0:
            self.type.setCurrentIndex(i)
        self.institution = QLineEdit(account["institution"] or "")
        self.note = QLineEdit(account["note"] or "")
        self.closed = QCheckBox("Account is closed")
        self.closed.setChecked(bool(account["closed_flag"]))
        form.addRow("Name", self.name)
        form.addRow("Type", self.type)
        form.addRow("Institution", self.institution)
        form.addRow("Note", self.note)
        form.addRow("", self.closed)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return {
            "name": self.name.text().strip(),
            "type": self.type.currentText(),
            "institution": self.institution.text().strip() or None,
            "note": self.note.text().strip() or None,
            "closed_flag": 1 if self.closed.isChecked() else 0,
        }


# ---------------------------------------------------------------------------
# dynamic webSlinger input form (fields come from the MCP script schema)
# ---------------------------------------------------------------------------
def _parse_download_config(raw):
    """Parse an account's ``download_config`` (a JSON name->value object of saved
    non-secret field defaults) into a dict. Tolerant of NULL / malformed data --
    returns ``{}`` so a bad value never blocks the dialog."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def build_script_input_form(schema, *, prefill=None, editable=True):
    """Render a webSlinger script's declared inputs as a form, DRIVEN ENTIRELY by
    the MCP ``describe_script`` schema -- one editor per ``schema.inputs`` entry,
    in declared order. Nothing about any one institution is hardcoded: whatever
    fields the script declares (date range, export format, account number, or a
    bank TOTP for MFA) are what get rendered.

    Returns ``(container_widget, {input_name: QLineEdit})``. ``prefill`` (name ->
    value) seeds the editors, falling back to the schema's ``input_template``
    example. Secret-looking inputs (TOTP/password) render with a masked echo.
    With ``editable=False`` the editors are read-only (a preview in the
    Account-details Download section).
    """
    container = QWidget()
    form = QFormLayout(container)
    form.setContentsMargins(0, 0, 0, 0)
    editors: dict = {}
    prefill = prefill or {}
    for inp in schema.inputs:
        edit = QLineEdit()
        value = prefill.get(inp.name)
        if value in (None, ""):
            value = schema.input_template.get(inp.name, "")
        if value is None:
            text = ""
        elif isinstance(value, (list, dict)):
            # A real list/dict (array-typed default or saved value) must render as
            # JSON -- str()/repr() would emit single-quoted text that the run-time
            # coercion's json.loads cannot parse back into an array.
            text = json.dumps(value)
        else:
            text = str(value)
        edit.setText(text)
        if inp.hint:
            edit.setToolTip(inp.hint)
            edit.setPlaceholderText(inp.hint)
        if inp.is_secret:
            edit.setEchoMode(QLineEdit.Password)
        edit.setReadOnly(not editable)
        form.addRow(inp.name, edit)
        editors[inp.name] = edit
    if not schema.inputs:
        form.addRow(QLabel("This script takes no inputs."))
    return container, editors


# ---------------------------------------------------------------------------
# account details dialog (identity + online-banking url/account number + flags)
# ---------------------------------------------------------------------------
class AccountDetailsDialog(QDialog):
    """Edit an account's identity AND its online-banking details (by request):
    name, type, institution, bank login URL, account number, note, plus the
    closed and hidden flags. ``values()`` exposes every field so the caller can
    hand it straight to ``ledger.update_account``. Opening balance is edited
    elsewhere (it rewrites every running balance)."""

    def __init__(self, account, parent=None, client=None, conn=None):
        super().__init__(parent)
        self._client = client
        self._conn = conn
        self._account_id = _row_get(account, "id")
        self._schema_rows = None      # container holding the dynamic input form
        self._loaded_schema = None    # the ScriptSchema currently rendered (if any)
        self._field_editors = {}      # input name -> QLineEdit for the loaded schema
        self._download_config_raw = _row_get(account, "download_config") or ""
        self._download_config = _parse_download_config(self._download_config_raw)
        self.setWindowTitle("Account details")
        # Wide enough that long webSlinger script names (e.g.
        # GetCheckingTransactionsForRange) and their input values are readable
        # without horizontal scrolling.
        self.setMinimumWidth(600)
        # Host the fields inside a scroll area so that however many inputs the
        # download script declares, every row stays reachable and the OK/Cancel
        # buttons remain pinned and clickable below it (never pushed off-screen).
        form_host = QWidget()
        form = QFormLayout(form_host)
        self.name = QLineEdit(_row_get(account, "name") or "")
        self.type = QComboBox()
        self.type.addItems(_ACCOUNT_TYPES)
        i = self.type.findText(_row_get(account, "type") or "")
        if i >= 0:
            self.type.setCurrentIndex(i)
        self.institution = QLineEdit(_row_get(account, "institution") or "")
        # Where a PROPERTY is, so a valuation source can look it up. A separate
        # column from `institution`, which means "who holds this account"
        # everywhere else -- giving one field two meanings by account type is how
        # a schema rots. But the address people typed into Institution before
        # this field existed is real, so it SEEDS the field when the field is
        # empty; the user still confirms it, because nothing is ever fetched from
        # a value the user has not seen and accepted (see asset_values).
        address = _row_get(account, "property_address") or ""
        if not address and (_row_get(account, "type") or "") == "asset":
            address = _row_get(account, "institution") or ""
        self.property_address = QLineEdit(address)
        self.property_address.setPlaceholderText(
            "120 Cedar Ln, ANYTOWN ST  (used to look up the value)")
        # Which property secures THIS loan. On the loan, not the asset, because
        # one house carries many loans (a second mortgage, a refinance).
        self.secured_by = QComboBox()
        self.secured_by.addItem("(not secured by an asset)", None)
        self.url = QLineEdit(_row_get(account, "url") or "")
        self.url.setPlaceholderText("https://…  (online-banking login / download page)")
        self.account_number = QLineEdit(_row_get(account, "account_number") or "")
        self.account_number.setPlaceholderText("statement account number")
        self.note = QLineEdit(_row_get(account, "note") or "")
        self.closed = QCheckBox("Account is closed")
        self.closed.setChecked(bool(_row_get(account, "closed_flag")))
        self.hidden = QCheckBox("Hide from the account list")
        self.hidden.setChecked(bool(_row_get(account, "hidden")))
        form.addRow("Name", self.name)
        form.addRow("Type", self.type)
        # Investment accounts: how a sale is costed (roadmap item 7). Average
        # is what every earlier figure was computed under; brokerages report
        # stock sales FIFO unless lots were specified.
        self.lot_method = QComboBox()
        for key, label in (("average", "Average cost"),
                           ("fifo", "First in, first out"),
                           ("lifo", "Last in, first out")):
            self.lot_method.addItem(label, key)
        i = self.lot_method.findData(_row_get(account, "lot_method") or "average")
        self.lot_method.setCurrentIndex(max(i, 0))
        self.lot_method.setToolTip(
            "How a sale relieves cost basis: the position's average per share, the "
            "oldest lot first, or the newest lot first. A sale can still name its "
            "lots (Specify Lots on its row). Changing this recomputes every open "
            "position's basis and every realized gain.")
        self._details_form = form
        form.addRow("Cost basis", self.lot_method)
        self._populate_secured_by(account)
        form.addRow("Secured by", self.secured_by)
        form.addRow("Institution", self.institution)
        form.addRow("Address", self.property_address)
        self.type.currentTextChanged.connect(self._sync_type_rows)
        self._sync_type_rows(self.type.currentText())
        form.addRow("Bank URL", self.url)
        form.addRow("Account number", self.account_number)
        form.addRow("Note", self.note)
        form.addRow("", self.closed)
        form.addRow("", self.hidden)
        form.addRow(self._build_download_group(account))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form_host)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll, 1)
        outer.addWidget(buttons)

    def _build_download_group(self, account):
        """The Download (webSlinger) section: a slot for the automation SCRIPT
        NAME plus a 'Load fields' button that asks the webSlinger MCP server what
        inputs that script needs and renders them dynamically (so the user sees
        exactly what the Download action will ask for -- date range, format,
        account number, and any MFA/TOTP the bank requires)."""
        box = QGroupBox("Download (webSlinger)")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.download_script = QLineEdit(_row_get(account, "download_script") or "")
        self.download_script.setMinimumWidth(320)
        self.download_script.setPlaceholderText(
            "webSlinger script name (e.g. GetCheckingTransactionsForRange)")
        self.load_fields_btn = QPushButton("Load fields")
        self.load_fields_btn.clicked.connect(self._load_script_fields)
        row.addWidget(QLabel("Script"))
        row.addWidget(self.download_script, 1)
        row.addWidget(self.load_fields_btn)
        v.addLayout(row)
        self._fields_note = QLabel(
            "Set a script name, then Load fields to see the inputs it needs.")
        self._fields_note.setWordWrap(True)
        v.addWidget(self._fields_note)
        self._fields_holder = QVBoxLayout()
        v.addLayout(self._fields_holder)
        # Auto-load when a script is already set and a client is available, but
        # DEFER it past the current event-loop turn so the dialog PAINTS FIRST.
        # _load_script_fields -> client.describe_script spawns an MCP subprocess
        # and blocks on a JSON-RPC handshake (McpWebSlingerClient._call); calling
        # it inline from __init__ froze the whole dialog for that round trip
        # before it ever appeared on screen (the user's "takes a long time to appear").
        # QTimer.singleShot(0, ...) lets the window show, then fills the fields;
        # the client caches the schema, so any later open is instant.
        if self.download_script.text().strip() and self._client is not None:
            self._fields_note.setText("Loading fields…")
            QTimer.singleShot(0, self._load_script_fields)
        return box

    def _load_script_fields(self):
        """Fetch the script's input schema from the webSlinger MCP server and
        render its inputs as an EDITABLE, prefilled form so the user can fill them
        and SAVE them with the account (see :meth:`values`). Handles the
        not-configured / not-found cases with a precise, actionable message rather
        than an exception."""
        # Clear any previously rendered form + its editors.
        if self._schema_rows is not None:
            self._schema_rows.setParent(None)
            self._schema_rows = None
        self._loaded_schema = None
        self._field_editors = {}
        name = self.download_script.text().strip()
        if not name:
            self._fields_note.setText("Enter a script name first.")
            return
        if self._client is None or not self._client.available():
            # Exactly what to configure and where -- never a dead end. The script
            # name is still saved when you click OK.
            self._fields_note.setText(webslinger_mod.config_hint())
            return
        try:
            schema = self._client.describe_script(name)
        except Exception as exc:  # WebSlingerError or transport failure
            self._fields_note.setText(f"Could not load '{name}': {exc}")
            return
        # Deliberately DO NOT show schema.description: that text is the brief
        # written for the webSlinger script-generator LLM, not for the Mammon
        # user. It is long, hides the actual input fields, and used to push the
        # OK button off-screen. Show only the target site plus a short prompt.
        site = f" ({schema.target_website})" if schema.target_website else ""
        self._fields_note.setText(
            f"Fill these in{site} and click OK to save them with the account.")
        # Editable + prefilled from any previously-saved config for this account.
        self._schema_rows, self._field_editors = build_script_input_form(
            schema, prefill=self._download_config, editable=True)
        self._loaded_schema = schema
        self._fields_holder.addWidget(self._schema_rows)

    def _populate_secured_by(self, account) -> None:
        """Fill the Secured by picker with the open asset accounts, and select
        this loan's current one. Needs a connection; with none (a caller that
        did not pass one) the picker stays at its single empty entry and the row
        is simply never useful -- it is not an error."""
        if self._conn is None:
            return
        from mammon import asset_values
        current = _row_get(account, "secured_by_account_id")
        for a in asset_values.valuable_accounts(self._conn, include_unaddressed=True):
            self.secured_by.addItem(a["name"], int(a["id"]))
        # A lien pointing at a since-CLOSED property is still real history, so it
        # is offered rather than silently dropped to "(not secured)" -- which
        # would look like the user had cleared it.
        if current is not None and self.secured_by.findData(int(current)) < 0:
            acct = ledger.get_account(self._conn, int(current))
            if acct is not None:
                self.secured_by.addItem(f"{acct['name']} (closed)", int(acct["id"]))
        if current is not None:
            i = self.secured_by.findData(int(current))
            if i >= 0:
                self.secured_by.setCurrentIndex(i)

    def _sync_type_rows(self, type_text) -> None:
        """Show only the rows this account TYPE has: cost basis for an
        investment, Address for an asset, Secured by for a liability.

        Institution hides for an asset account -- a house has no institution,
        and the field is where addresses were typed before Address existed."""
        from mammon import asset_values
        kind = (type_text or "").strip()
        for widget, show in ((self.lot_method, kind == "investment"),
                             (self.property_address, kind == "asset"),
                             (self.institution, kind != "asset"),
                             (self.secured_by, kind in asset_values.SECURABLE_TYPES)):
            widget.setVisible(show)
            label = self._details_form.labelForField(widget)
            if label is not None:
                label.setVisible(show)

    def values(self):
        return {
            "name": self.name.text().strip(),
            "type": self.type.currentText(),
            "lot_method": self.lot_method.currentData(),
            "institution": self.institution.text().strip() or None,
            "url": self.url.text().strip() or None,
            "account_number": self.account_number.text().strip() or None,
            "note": self.note.text().strip() or None,
            "closed_flag": 1 if self.closed.isChecked() else 0,
            "hidden": 1 if self.hidden.isChecked() else 0,
            "download_script": self.download_script.text().strip() or None,
            "download_config": self._download_config_value(),
            "property_address": self.property_address.text().strip() or None,
            "secured_by_account_id": self.secured_by.currentData(),
        }

    def _download_config_value(self):
        """The JSON string to persist in ``accounts.download_config``. When the
        script's fields are loaded, capture what the user entered -- SKIPPING
        secret inputs (MFA/TOTP), which are one-time codes and must never be
        stored. When the fields are NOT loaded (webSlinger unconfigured, or the
        user never pressed Load fields), keep whatever config was already saved so
        editing an unrelated account field never wipes it."""
        if self._loaded_schema is not None and self._field_editors:
            cfg = {}
            for inp in self._loaded_schema.inputs:
                if inp.is_secret:
                    continue
                editor = self._field_editors.get(inp.name)
                if editor is None:
                    continue
                value = editor.text().strip()
                if not value:
                    continue
                if inp.is_array:
                    # Store array-typed inputs as a REAL JSON array so a save/reload
                    # round-trip preserves the list type (and never repr's it back
                    # into a string). Accepts JSON, a lone value, or comma/newline
                    # separated entries typed into the field.
                    cfg[inp.name] = webslinger_mod._as_list(value)
                else:
                    cfg[inp.name] = value
            return json.dumps(cfg) if cfg else None
        return self._download_config_raw or None


# ---------------------------------------------------------------------------
# Download date-range prompt (shown before running a date-ranged script)
# ---------------------------------------------------------------------------
class DownloadDateDialog(QDialog):
    """Ask for the Download date range before running a webSlinger script.

    Shown only when the script DECLARES date inputs. Start defaults to the day
    after the last download's end date and end to today (see
    :func:`downloads.default_download_dates`), so consecutive downloads chain
    without overlap or gap. OK reads "Download"; Cancel aborts the run. The
    chosen dates are read back with :meth:`dates` and persisted by the caller so
    the next prompt picks up where this one left off."""

    def __init__(self, schema, config, parent=None, today=None):
        super().__init__(parent)
        from PyQt5.QtWidgets import QDateEdit
        from PyQt5.QtCore import QDate
        self.setWindowTitle("Download date range")
        self.setMinimumWidth(360)
        start_inp, end_inp = downloads.classify_date_inputs(schema)
        start, end = downloads.default_download_dates(
            config, today,
            start_name=(start_inp.name if start_inp else None),
            end_name=(end_inp.name if end_inp else None))

        def _picker(d):
            # The shared app date editor, so a download script's date range is
            # entered in the same format as everything else.
            edit = make_date_edit()
            edit.setDate(QDate(d.year, d.month, d.day))
            return edit

        self.start_edit = _picker(start)
        self.end_edit = _picker(end)
        form = QFormLayout()
        form.addRow(start_inp.name if start_inp else "Start date", self.start_edit)
        form.addRow(end_inp.name if end_inp else "End date", self.end_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Download")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer = QVBoxLayout(self)
        outer.addLayout(form)
        outer.addWidget(buttons)

    def dates(self):
        """The chosen ``(start_date, end_date)`` as ``datetime.date`` objects."""
        import datetime
        s, e = self.start_edit.date(), self.end_edit.date()
        return (datetime.date(s.year(), s.month(), s.day()),
                datetime.date(e.year(), e.month(), e.day()))


# ---------------------------------------------------------------------------
# account-page action bar (Settings/toolbar at the top of a register)
# ---------------------------------------------------------------------------
class AccountToolbar(QToolBar):
    """The row of account actions pinned above a register. It does no work
    itself: each action emits an intent signal carrying the account id, and the
    MainWindow (which owns the dialogs and the cross-account refresh) responds.
    Shared by the cash and investment registers."""

    detailsRequested = pyqtSignal(int)
    reconcileRequested = pyqtSignal(int)
    importRequested = pyqtSignal(int)
    downloadRequested = pyqtSignal(int)
    reviewRequested = pyqtSignal(int)
    hideRequested = pyqtSignal(int)
    loanSetupRequested = pyqtSignal(int)
    enterPaymentRequested = pyqtSignal(int)

    def __init__(self, account_id, parent=None):
        super().__init__(parent)
        self.account_id = int(account_id)
        self.setObjectName("accountToolbar")
        self.setMovable(False)
        self.setFloatable(False)
        # Loan Setup is the entry point to the guided loan wizard. It sits FIRST,
        # at the top of the register, but only appears for loan/liability
        # accounts (configure_loan below shows it and picks Setup vs Edit text).
        self.act_loan = self._add("Loan Setup…", self.loanSetupRequested)
        self.act_loan.setVisible(False)
        # Enter Payment posts one loan payment the way the schedule would (on
        # the account that pays the loan, split shown first) -- the way to
        # record a payment when no pre-entry stands. Gray while one does.
        self.act_enter_payment = self._add("Enter Payment…", self.enterPaymentRequested)
        self.act_enter_payment.setVisible(False)
        self.act_details = self._add("Account Details…", self.detailsRequested)
        self.act_reconcile = self._add("Reconcile…", self.reconcileRequested)
        # Import (from a downloaded FILE) and Download (pull via webSlinger) are
        # DISTINCT actions. Download stays ALWAYS enabled/clickable so its click
        # handler always runs a preflight and reports exactly what setup is
        # missing (webSlinger, stored creds, or a script) -- it must
        # never be silently unresponsive. The tooltip mirrors that readiness.
        self.act_import = self._add("Import…", self.importRequested)
        self.act_download = self._add("Download…", self.downloadRequested)
        self.act_download.setToolTip("Download the latest statement via webSlinger.")
        # Re-open the import-review list for rows still awaiting accept/save.
        # Downloaded rows go to that review list first -- nothing lands in the
        # register until reviewed -- so this is the way back to a pending review.
        self.act_review = self._add("Review…", self.reviewRequested)
        self.act_review.setToolTip("Re-open the import-review list for rows "
                                   "awaiting accept or save.")
        self.act_review.setEnabled(False)
        self.addSeparator()
        self.act_hide = self._add("Hide Account", self.hideRequested)
        # NB: the Accounts… roster lives on the Tools menu (classic), not
        # on this per-account bar.

    def _add(self, text, signal):
        act = self.addAction(text)
        # triggered(bool) -> re-emit our int signal with this account's id.
        act.triggered.connect(lambda _checked=False, s=signal: s.emit(self.account_id))
        return act

    def configure_loan(self, is_liability, is_loan, pending=None):
        """Show the Loan Setup button only for loan/liability accounts. Its label
        reflects whether a loan is already configured: ``Edit Loan…`` re-opens the
        wizard on the stored parameters, ``Loan Setup…`` starts a fresh setup.
        ``Enter Payment…`` appears once a loan is configured. It stays enabled
        while a pending pre-entry stands (``pending``: its ``(id, date,
        account_id)`` from ``loans_schedule.pending_payment``) -- the tooltip
        says so, and entering then posts that row with what the user types
        (its account included), since the month you must pay from the card
        is exactly the month the pre-entry already sits on checking."""
        self.act_loan.setVisible(bool(is_liability))
        self.act_loan.setText("Edit Loan…" if is_loan else "Loan Setup…")
        self.act_enter_payment.setVisible(bool(is_loan))
        self.act_enter_payment.setEnabled(bool(is_loan))
        if pending is None:
            self.act_enter_payment.setToolTip(
                "Post one loan payment now -- from the account that pays this "
                "loan, or another one this once -- as interest, extras and principal.")
        else:
            from mammon.ui.models import fmt_date
            self.act_enter_payment.setToolTip(
                f"A pending pre-entry dated {fmt_date(pending[1])} stands; Enter "
                "Payment posts it with the date, amount and account you enter.")

    def set_download_hint(self, reason=""):
        """Update the Download tooltip to ``reason`` (why it is not yet ready, or
        what it does when it is). Download stays ENABLED regardless -- the click
        handler is responsible for reporting any missing prerequisite."""
        self.act_download.setEnabled(True)
        if reason:
            self.act_download.setToolTip(reason)


# ---------------------------------------------------------------------------
# download dialog (run a webSlinger script; inputs come from its MCP schema)
# ---------------------------------------------------------------------------
class DownloadDialog(QDialog):
    """Collect the inputs a webSlinger download script needs, then let the caller
    run it. The form is rendered DYNAMICALLY from the script's MCP schema (via
    :func:`build_script_input_form`) -- Mammon hardcodes nothing about any one
    bank. ``inputs()`` returns the field values to hand to ``run_script``."""

    def __init__(self, schema, *, prefill=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Download — {schema.name}")
        outer = QVBoxLayout(self)
        head = []
        if schema.target_website:
            head.append(schema.target_website)
        if schema.description:
            head.append(schema.description)
        if head:
            lbl = QLabel("  •  ".join(head))
            lbl.setWordWrap(True)
            outer.addWidget(lbl)
        self._form, self._editors = build_script_input_form(
            schema, prefill=prefill, editable=True)
        outer.addWidget(self._form)
        note = QLabel("A browser window may open to sign in; some banks require "
                      "an MFA/TOTP code you set up with them.")
        note.setWordWrap(True)
        outer.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Download")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def inputs(self):
        return {name: e.text() for name, e in self._editors.items()}


# ---------------------------------------------------------------------------
# accounts list dialog (classic roster -> account details)
# ---------------------------------------------------------------------------
class AccountsListDialog(QDialog):
    """A classic list of EVERY account (including hidden and closed) with
    quick access to each one's details. Double-click a row (or Details…) edits
    that account; Hide/Show toggles the hidden flag inline. Emits ``changed``
    whenever an edit or a hide/show happens so the window can refresh."""

    changed = pyqtSignal()

    def __init__(self, conn, parent=None, client=None):
        super().__init__(parent)
        self.conn = conn
        self._client = client
        self.setWindowTitle("Accounts")
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Account", "Type", "Institution", "Status"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(lambda _idx: self._edit_selected())
        layout.addWidget(self.table)

        bar = QHBoxLayout()
        self.details_btn = QPushButton("Details…")
        self.details_btn.clicked.connect(self._edit_selected)
        self.hide_btn = QPushButton("Hide / Show")
        self.hide_btn.clicked.connect(self._toggle_hidden_selected)
        bar.addWidget(self.details_btn)
        bar.addWidget(self.hide_btn)
        bar.addStretch()
        close_box = QDialogButtonBox(QDialogButtonBox.Close)
        close_box.rejected.connect(self.reject)
        bar.addWidget(close_box)
        layout.addLayout(bar)

        self._rows: list = []
        self._reload()

    def _reload(self):
        self._rows = list(ledger.list_accounts(
            self.conn, include_closed=True, include_hidden=True))
        self.table.setRowCount(len(self._rows))
        for i, a in enumerate(self._rows):
            flags = []
            if _row_get(a, "hidden"):
                flags.append("hidden")
            if _row_get(a, "closed_flag"):
                flags.append("closed")
            cells = [_row_get(a, "name") or "", _row_get(a, "type") or "",
                     _row_get(a, "institution") or "", ", ".join(flags) or "active"]
            for c, text in enumerate(cells):
                self.table.setItem(i, c, QTableWidgetItem(str(text)))

    def selected_account_id(self):
        r = self.table.currentRow()
        if 0 <= r < len(self._rows):
            return self._rows[r]["id"]
        return None

    def _edit_selected(self):
        aid = self.selected_account_id()
        if aid is None:
            return
        acct = ledger.get_account(self.conn, aid)
        if acct is None:
            return
        dlg = AccountDetailsDialog(acct, parent=self, client=self._client)
        if dlg.exec_() != QDialog.Accepted:
            return
        v = dlg.values()
        if not v["name"]:
            QMessageBox.warning(self, "Account details", "An account needs a name.")
            return
        try:
            ledger.update_account(self.conn, aid, **v)
        except Exception as exc:  # e.g. duplicate name (UNIQUE)
            QMessageBox.warning(self, "Account details", str(exc))
            return
        self._reload()
        self.changed.emit()

    def _toggle_hidden_selected(self):
        aid = self.selected_account_id()
        if aid is None:
            return
        acct = ledger.get_account(self.conn, aid)
        if acct is None:
            return
        ledger.set_account_hidden(self.conn, aid, not _row_get(acct, "hidden"))
        self._reload()
        self.changed.emit()


# ---------------------------------------------------------------------------
# split dialog (divide one transaction across categories)
# ---------------------------------------------------------------------------
class SplitDialog(QDialog):
    """Divide one transaction across several category lines. The transaction
    Total is editable here: changing it is allowed, and any signed difference
    between the total and the split-line sum is absorbed into an UNCATEGORIZED
    line on save rather than blocking it -- the register then flags the leftover
    with a warning triangle before '--Split--'. The running Remainder shows how
    far off the lines currently are; 'Adj' snaps the Total to the current line
    sum (zeroing it). Editing/clearing goes through ledger.set_splits /
    ledger.clear_splits."""

    def __init__(self, model, row, parent=None):
        super().__init__(parent)
        self.model = model
        self.conn = model.conn
        self.txn = model.txn_at(row)
        self.total = int(self.txn["amount"])
        self.removed = False
        # The most recent PRIOR same-payee split, if any -- offered for one-click
        # re-entry of a recurring paycheck/bill's many-row breakdown.
        self._prior_split = ledger.previous_split_for_payee(
            self.conn, self.txn["payee"], self.txn["id"])
        # Offer transfer targets ("[Account]") alongside categories: a split leg
        # may itself be a transfer -- a mortgage principal leg to the house/loan
        # account, a paycheck 401(k) deferral to the retirement account.
        self._cats = list(model.category_choices())
        # Tag identity colors (casefolded name -> #rrggbb) so a tagged leg row is
        # tinted with its tag's color, matching the register and By Tag report.
        self._tag_colors = ledger.tag_colors(self.conn)
        self._lines = []            # list of dicts: {frame, cat, amount, memo, tag}

        self.setWindowTitle("Split transaction")
        outer = QVBoxLayout(self)
        payee = self.txn["payee"] or "(no payee)"
        header = QHBoxLayout()
        header.addWidget(QLabel(f"{fmt_date(self.txn['date'])}  {payee}"))
        header.addStretch()
        header.addWidget(QLabel("Total"))
        # The transaction total is EDITABLE: changing it is allowed and any
        # difference from the split-line sum is absorbed into an uncategorized
        # line on save (see apply_split). Its valueChanged signal is wired at the
        # END of __init__, after ok_btn / remainder_label exist, so seeding the
        # value below never fires into _update_remainder before it is ready.
        self.total_spin = NoWheelDoubleSpinBox()
        self.total_spin.setRange(-1_000_000_000, 1_000_000_000)
        self.total_spin.setDecimals(2)
        self.total_spin.setValue(self.total / 100.0)
        header.addWidget(self.total_spin)
        self.adj_btn = QPushButton("Adj")
        self.adj_btn.setToolTip("Set the Total to the current sum of the split lines")
        self.adj_btn.clicked.connect(self._adjust_total_to_lines)
        header.addWidget(self.adj_btn)
        outer.addLayout(header)

        self._rows_box = QVBoxLayout()
        holder = QWidget()
        holder.setLayout(self._rows_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        controls = QHBoxLayout()
        add_btn = QPushButton("Add line")
        add_btn.clicked.connect(lambda: (self.add_line(), self._update_remainder()))
        controls.addWidget(add_btn)
        if self._prior_split:
            payee_name = self.txn["payee"] or ""
            self.copy_prev_btn = QPushButton(
                f"Copy from previous {payee_name} split")
            self.copy_prev_btn.clicked.connect(self._copy_previous_split)
            controls.addWidget(self.copy_prev_btn)
        controls.addStretch()
        self.remainder_label = QLabel()
        controls.addWidget(self.remainder_label)
        outer.addLayout(controls)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_btn = buttons.button(QDialogButtonBox.Ok)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        existing = ledger.get_splits(self.conn, self.txn["id"])
        if existing:
            rm = buttons.addButton("Remove Split", QDialogButtonBox.DestructiveRole)
            rm.clicked.connect(self._remove_split)
        outer.addWidget(buttons)

        if existing:
            for s in existing:
                self.add_line(s["category_label"], s["amount"] / 100.0, s["memo"],
                              s["tag"])
        else:
            # Seed line 1 with the transaction's existing single category, so a
            # split started from an already-categorized transaction KEEPS that
            # category (Quicken behaviour) instead of discarding it; line 2 is
            # empty. category_label is "" for an uncategorized transaction.
            self.add_line(self.txn.get("category_label") or "",
                          self.total / 100.0, "")
            self.add_line()
        # Safe to react to Total edits now that ok_btn/remainder_label exist.
        self.total_spin.valueChanged.connect(self._on_total_changed)
        self._update_remainder()
        self.resize(560, 320)

    # ---- line rows --------------------------------------------------------
    def add_line(self, category="", amount=0.0, memo="", tag=""):
        frame = QWidget()
        frame.setObjectName("splitrow")
        h = QHBoxLayout(frame)
        h.setContentsMargins(0, 0, 0, 0)
        # The very same builder the register's category cell uses, so a split
        # line accepts identical typing: the ':' parent-complete gesture, the
        # case-insensitive popup completer, and fragment resolution on commit.
        # This was a bare combo with no completer at all, so habits learned in
        # the register silently did not work one dialog away.
        cat = make_category_combo(None, self._cats)
        cat.setCurrentText(category or "")
        cat.setMinimumWidth(180)
        # Re-check the Remainder / OK state when the category changes, so picking
        # a transfer account ("[Account]") -- or any category, whether chosen from
        # the dropdown or typed -- on a line re-enables OK (previously only an
        # amount change did). Wired AFTER setCurrentText (above) to skip the seed.
        # Safe on every keystroke because _update_remainder no longer resolves
        # (get-or-creates) categories -- see _line_amounts.
        cat.currentTextChanged.connect(lambda *_: self._update_remainder())
        amt = SplitAmountSpinBox()
        amt.setRange(-1_000_000_000, 1_000_000_000)
        amt.setDecimals(2)
        amt.setValue(float(amount or 0.0))
        amt.valueChanged.connect(self._update_remainder)
        # Clearing the box (backspacing it empty) fires no valueChanged -- the spin
        # box treats "" as an intermediate edit -- so listen to the text too, and
        # SplitAmountSpinBox.value() reads a cleared box as 0. Together the live
        # Remainder is exact integer cents the instant a leg is cleared (BUG 4).
        amt.lineEdit().textChanged.connect(lambda *_: self._update_remainder())
        memo_edit = QLineEdit(memo or "")
        memo_edit.setPlaceholderText("memo")
        # A single tag per leg (Quicken's per-leg tag). Typing a tag tints the row
        # with that tag's color; an existing color (set in the Tag Manager) is
        # reused, and a brand-new name is get-or-created on save via set_splits.
        tag_edit = QLineEdit(tag or "")
        tag_edit.setPlaceholderText("tag")
        tag_edit.setMaximumWidth(120)
        remove = QPushButton("Remove")
        entry = {"frame": frame, "cat": cat, "amount": amt, "memo": memo_edit,
                 "tag": tag_edit}
        remove.clicked.connect(lambda: self._remove_line(entry))
        for w in (cat, amt, memo_edit, tag_edit, remove):
            h.addWidget(w)
        self._rows_box.addWidget(frame)
        self._lines.append(entry)
        tag_edit.textChanged.connect(lambda *_: self._recolor_line(entry))
        self._recolor_line(entry)

    def _recolor_line(self, entry):
        """Tint a split leg's row strip with its tag's identity color, or clear
        the tint when the leg is untagged/uncolored. The stylesheet is scoped to
        the row's object name so only the strip is colored, not the child
        editors."""
        name = entry["tag"].text().strip().casefold()
        color = self._tag_colors.get(name) if name else None
        entry["frame"].setStyleSheet(
            "QWidget#splitrow { background-color: %s; }" % color if color else "")

    def _remove_line(self, entry):
        if entry in self._lines:
            self._lines.remove(entry)
            entry["frame"].setParent(None)
            self._update_remainder()

    def _copy_previous_split(self):
        """Replace the current lines with the categories AND amounts of the most
        recent prior same-payee split, leaving the user to adjust any differing
        amounts. No-op when there is no prior split (the button is not shown)."""
        if not self._prior_split:
            return
        for e in list(self._lines):
            self._remove_line(e)
        for s in self._prior_split:
            self.add_line(s["category_label"], s["amount"] / 100.0, s["memo"],
                          s["tag"])
        self._update_remainder()

    # ---- computed state ---------------------------------------------------
    def lines_cents(self):
        """Non-empty lines as dicts for ledger.set_splits; a line counts when it
        has a nonzero amount or a category chosen. A leg whose label names a
        transfer target ("[Account]") is emitted as a transfer leg
        (transfer_account_id); anything else resolves to a category_id."""
        out = []
        for e in self._lines:
            cents = int(round(e["amount"].value() * 100))
            # Resolve an unambiguous fragment to its real path before saving, so
            # "fuel" on a split line stores Auto:Fuel rather than creating a new
            # top-level "fuel" -- the register cell has always done this.
            accept_category_text(e["cat"])
            label = e["cat"].currentText().strip()
            if cents == 0 and not label:
                continue
            memo = e["memo"].text().strip() or None
            tag = e["tag"].text().strip() or None
            target = self.model.transfer_target(label) if label else None
            if target is not None:
                out.append({"category_id": None, "transfer_account_id": target,
                            "amount": cents, "memo": memo, "tag": tag})
            else:
                cid = ledger.resolve_category(self.conn, label) if label else None
                out.append({"category_id": cid, "transfer_account_id": None,
                            "amount": cents, "memo": memo, "tag": tag})
        return out

    def _line_amounts(self):
        """Signed cents of each line that currently counts -- a nonzero amount or
        a chosen category/transfer label. PURE: unlike lines_cents(), it never
        resolves (get-or-creates) a category, so it is safe to call on every
        keystroke while a category name is being typed."""
        out = []
        for e in self._lines:
            cents = int(round(e["amount"].value() * 100))
            label = e["cat"].currentText().strip()
            if cents == 0 and not label:
                continue
            out.append(cents)
        return out

    def remainder_cents(self):
        return self.total - sum(self._line_amounts())

    def _on_total_changed(self):
        """The editable Total changed -- keep self.total in cents and refresh the
        remainder/OK state. A nonzero remainder is allowed; it lands in an
        uncategorized line on save."""
        self.total = int(round(self.total_spin.value() * 100))
        self._update_remainder()

    def _adjust_total_to_lines(self):
        """'Adj': set the Total to the current sum of the split lines, zeroing the
        remainder. Handy when the lines are right and the header total was wrong.
        Setting the spin fires valueChanged -> _on_total_changed."""
        self.total_spin.setValue(sum(self._line_amounts()) / 100.0)

    def _update_remainder(self):
        amts = self._line_amounts()
        rem = self.total - sum(amts)
        n = len(amts)
        if rem == 0:
            self.remainder_label.setText(f"Remainder: {fmt_money(rem)}")
            self.remainder_label.setStyleSheet("color:#2e7d32;")
        else:
            # A nonzero remainder is no longer an error -- it will be absorbed
            # into an uncategorized line on save -- so show it in amber, not red.
            self.remainder_label.setText(
                f"Remainder: {fmt_money(rem)} → Uncategorized")
            self.remainder_label.setStyleSheet("color:#c98a00;")
        # A nonzero remainder no longer blocks OK: set_splits folds it into an
        # uncategorized line. OK just needs the split to end up with >= 2 lines --
        # the counting lines plus the uncategorized line a nonzero remainder adds.
        effective = n + (1 if rem != 0 else 0)
        self.ok_btn.setEnabled(effective >= 2)

    # ---- actions ----------------------------------------------------------
    def apply_split(self) -> bool:
        """Persist the current lines as this transaction's split. The (possibly
        edited) Total is written first; ledger.set_splits then absorbs any
        difference between the total and the line sum into an uncategorized line,
        so an unbalanced split is never rejected. Returns True on success; shows a
        warning and returns False on a rule violation (e.g. fewer than two lines,
        or splitting a plain unsplit transfer)."""
        try:
            new_total = int(round(self.total_spin.value() * 100))
            if new_total != int(self.txn["amount"]):
                ledger.update_transaction(
                    self.conn, self.txn["id"], amount=new_total)
            ledger.set_splits(self.conn, self.txn["id"], self.lines_cents())
            return True
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Split", str(exc))
            return False

    def _accept(self):
        if self.apply_split():
            self.accept()

    def _remove_split(self):
        ledger.clear_splits(self.conn, self.txn["id"])
        self.removed = True
        self.accept()


# ---------------------------------------------------------------------------
# reconcile-to-statement dialogs (Settings > Reconcile to Statement)
# ---------------------------------------------------------------------------
def _set_date_edit(edit, iso) -> None:
    """Seed a date editor from a stored ISO date, leaving today's date when the
    stored value is missing or unparseable."""
    from PyQt5.QtCore import QDate
    d = QDate.fromString(str(iso or "").strip(), "yyyy-MM-dd")
    edit.setDate(d if d.isValid() else QDate.currentDate())


def _iso_date_edit(iso: str):
    """A calendar-backed statement-date field (ui.delegates.make_date_edit).

    The statement date used to be a bare QLineEdit pre-filled with a guess. Typing
    into it without selecting first APPENDED, producing '2026-03-152025-11-04',
    and nothing validated it. Every reconcile bound is a string comparison on ISO
    text, so a malformed or mistyped date does not fail -- it silently sorts after
    every real row and quietly widens the statement to include months that are not
    on it. A QDateEdit cannot be appended to and cannot hold a non-date.
    """
    return make_date_edit(iso=iso)


class ReconcileStartDialog(QDialog):
    """Step 1 of a reconcile (the classic desktop ledger): enter the THREE statement inputs --
    ending DATE, BEGINNING balance, ENDING balance -- before the two-pane
    reconcile workspace opens. Reused by the workspace's 'Balances...' button so
    the same three inputs can be changed mid-reconcile and the panes re-filter."""

    def __init__(self, statement_date, beginning_cents, ending_cents,
                 account_name="", parent=None):
        super().__init__(parent)
        title = "Reconcile Details"
        if account_name:
            title += f" - {account_name}"
        self.setWindowTitle(title)
        form = QFormLayout(self)
        self.date = _iso_date_edit(statement_date)
        self.beginning = QDoubleSpinBox()
        self.beginning.setRange(-1_000_000_000, 1_000_000_000)
        self.beginning.setDecimals(2)
        self.beginning.setValue(beginning_cents / 100.0)
        self.ending = QDoubleSpinBox()
        self.ending.setRange(-1_000_000_000, 1_000_000_000)
        self.ending.setDecimals(2)
        self.ending.setValue(ending_cents / 100.0)
        form.addRow("Statement ending date", self.date)
        form.addRow("Beginning balance", self.beginning)
        form.addRow("Ending balance", self.ending)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> dict:
        return {
            "date": date_edit_iso(self.date),
            "beginning": int(round(self.beginning.value() * 100)),
            "ending": int(round(self.ending.value() * 100)),
        }


class CreditReconcileStartDialog(QDialog):
    """Step 1 of a CREDIT-CARD reconcile (the classic desktop ledger): the setup
    screen for a card statement differs from a bank statement -- instead of a
    beginning/ending pair it collects the totals the card statement itself prints
    (ending DATE, CHARGES & cash advances, PAYMENTS, CREDITS, ENDING balance)
    plus a separate FINANCE-CHARGES box (amount + category). All dollar figures
    are entered the way they read on the paper statement, as POSITIVE numbers;
    the ending balance is the amount OWED.

    These totals are not decoration -- together they ARE the reconcile. A card
    statement is closed arithmetic (previous + charges + finance - payments -
    credits = ending), so the typed figures imply what was owed when the
    statement opened, and that implied beginning is what the checked items are
    measured against (ledger.implied_card_beginning). No beginning balance is
    asked for, and none is read out of the register: a card whose history arrived
    from Quicken already carrying R would otherwise open its reconcile thousands
    of dollars adrift, with no way to correct it from inside the window.

    Payments and Credits are separate rows because a statement lists them
    separately. The Credits box existed and round-tripped through values() from
    the start but was never added to the form, so whatever was typed for credits
    was silently dropped.

    The finance-charge box defaults to blank. Institutions now put the finance
    charge on the statement as its own transaction, so it arrives with the rest
    of the download and is simply checked off; filling the box in is the fallback
    for one that does not. Reused by the workspace's 'Balances...' button so the
    same inputs can be edited mid-run."""

    def __init__(self, conn, statement_date, charges_cents, payments_cents,
                 credits_cents, ending_owed_cents, finance_cents,
                 finance_category, account_name="", parent=None):
        super().__init__(parent)
        title = "Credit Card Statement Information"
        if account_name:
            title += f" - {account_name}"
        self.setWindowTitle(title)
        outer = QVBoxLayout(self)

        form = QFormLayout()
        self.date = _iso_date_edit(statement_date)
        self.charges = self._money_spin(charges_cents)
        self.payments = self._money_spin(payments_cents)
        self.credits = self._money_spin(credits_cents)
        self.ending = self._money_spin(ending_owed_cents)
        form.addRow("Statement ending date", self.date)
        form.addRow("Charges, cash advances", self.charges)
        form.addRow("Payments", self.payments)
        form.addRow("Credits", self.credits)
        form.addRow("Ending balance (owed)", self.ending)
        outer.addLayout(form)

        # -- separate finance-charges box (the classic desktop ledger credit-card reconcile) --
        fc_box = QGroupBox("Finance Charges")
        fc_form = QFormLayout(fc_box)
        self.finance = self._money_spin(finance_cents)
        self.finance_cat = QComboBox()
        self.finance_cat.setEditable(True)
        for c in ledger.list_categories(conn):
            self.finance_cat.addItem(c["path"])
        want = (finance_category or "Interest Exp").strip()
        idx = self.finance_cat.findText(want)
        if idx >= 0:
            self.finance_cat.setCurrentIndex(idx)
        else:
            self.finance_cat.setEditText(want)
        fc_form.addRow("Amount", self.finance)
        fc_form.addRow("Category", self.finance_cat)
        hint = QLabel("Posted as a charge on the statement ending date.")
        hint.setWordWrap(True)
        fc_form.addRow(hint)
        outer.addWidget(fc_box)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @staticmethod
    def _money_spin(cents):
        spin = QDoubleSpinBox()
        spin.setRange(-1_000_000_000, 1_000_000_000)
        spin.setDecimals(2)
        spin.setValue((cents or 0) / 100.0)
        return spin

    def values(self) -> dict:
        return {
            "date": date_edit_iso(self.date),
            "charges": int(round(self.charges.value() * 100)),
            "payments": int(round(self.payments.value() * 100)),
            "credits": int(round(self.credits.value() * 100)),
            "ending": int(round(self.ending.value() * 100)),
            "finance_charge": int(round(self.finance.value() * 100)),
            "finance_category": self.finance_cat.currentText().strip(),
        }


class ReconcileDialog(QDialog):
    """Reconcile an account against a bank statement, classic-desktop style. Opens
    AFTER the three-input ReconcileStartDialog and shows a two-pane workspace:
    'Payments and Checks' (debits) on the LEFT, 'Deposits' (credits) on the
    RIGHT, each Clr | Date | Chk# | Payee | Amount. Only transactions dated
    ON OR BEFORE the statement date appear. Clicking ANYWHERE on a row toggles
    its ``cleared`` mark (persisted live, shown as a green 'c' in the Clr
    column -- no checkbox to hit); 'Mark All' / 'Clear All' set every shown row;
    'Balances...' reopens the three-input dialog to change the inputs and
    re-filter. Finish (enabled only when the difference is zero) marks every
    cleared item ``reconciled`` and records the reconciliation. Cleared marks
    persist even if closed without finishing, so a reconcile can be resumed."""

    CLR, DATE, NUM, PAYEE, AMOUNT = range(5)

    def __init__(self, conn, account_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = account_id
        self.finished_ok = False
        acct = ledger.get_account(conn, account_id)
        self.account_name = acct["name"] if acct else ""
        # A credit-card account reconciles against a CARD statement (charges /
        # payments / credits / finance charge / ending-owed) instead of a bank
        # statement (beginning / ending). This flag branches the setup dialog,
        # the pane labels, and the summary wording -- the core reconcile math in
        # ledger.reconcile_summary is sign-agnostic and stays shared.
        self.is_credit = bool(acct is not None and acct["type"] == "credit")
        self.setWindowTitle(f"Reconcile - {self.account_name}")

        # Authoritative statement inputs, seeded from the last reconciliation and
        # the computed beginning balance; edited via the Balances... dialog. The
        # ending defaults to the beginning until the user types a real statement
        # ending (the Start dialog always sets it before the workspace shows).
        #
        # A CARD takes no beginning balance from the register: the statement's own
        # figures imply it, and set_credit_balances recomputes it from them. Until
        # those figures are typed there is nothing to imply, so it starts at zero
        # rather than at whatever the account's R rows happen to sum to.
        summary = ledger.reconcile_summary(conn, account_id, 0)
        last = ledger.last_reconciliation(conn, account_id)
        rows = ledger.unreconciled_rows(conn, account_id)
        self.statement_date = (last["statement_date"] if last
                               else (rows[-1]["date"] if rows else ""))
        self.beginning_cents = 0 if self.is_credit else summary["beginning_balance"]
        self.ending_cents = self.beginning_cents

        # Credit-card statement figures. Charges/payments/credits are not merely
        # informational: with the ending balance they are what the reconcile runs
        # on, via ledger.implied_card_beginning. The finance charge is additionally
        # posted as a real cleared charge, and is tracked so 'Balances...' re-seeds
        # it and it stays a single editable transaction instead of duplicating on
        # each reopen. It defaults to blank -- statements now list finance charges
        # as their own transactions, which simply get checked off like any charge.
        self._charges_cents = 0
        self._payments_cents = 0
        self._credits_cents = 0
        self._finance_cents = 0
        self._finance_category = "Interest Exp"
        self._finance_charge_id = None

        # A reconcile spans several sittings: the user closes the window to look
        # a charge up in the register and comes back. Restoring the saved draft
        # (rather than re-prompting from a blank statement form) is what makes
        # that round trip free. ``has_draft`` tells the caller to skip the setup
        # dialog entirely -- see MainWindow._reconcile_dialog.
        draft = ledger.get_reconcile_draft(conn, account_id)
        self.has_draft = draft is not None
        if draft is not None:
            self.statement_date = (self._clean_date(draft["statement_date"])
                                   or self.statement_date)
            self.beginning_cents = draft["beginning_cents"]
            self.ending_cents = draft["ending_cents"]
            self._charges_cents = draft["charges_cents"]
            self._payments_cents = draft["payments_cents"]
            self._credits_cents = draft["credits_cents"]
            self._finance_cents = draft["finance_cents"]
            self._finance_category = draft["finance_category"] or self._finance_category
            # Restoring this id is what stops a reopened reconcile from posting a
            # SECOND finance charge for the same statement: _apply_finance_charge
            # updates the row it names instead of inserting a new one.
            self._finance_charge_id = draft["finance_txn_id"]

        outer = QVBoxLayout(self)

        # -- two panes: debits left, credits right (labels differ by account type)
        panes = QHBoxLayout()
        if self.is_credit:
            left_title, right_title = "Charges and Cash Advances", "Payments and Credits"
        else:
            left_title, right_title = "Payments and Checks", "Deposits"
        self.debits_table, debit_box = self._make_pane(left_title)
        self.credits_table, credit_box = self._make_pane(right_title)
        panes.addWidget(debit_box, 1)
        panes.addWidget(credit_box, 1)
        outer.addLayout(panes, 1)

        # -- action buttons (Mark All / Clear All / Balances) --
        actions = QHBoxLayout()
        self.mark_all_btn = QPushButton("Mark All")
        self.mark_all_btn.clicked.connect(self.mark_all)
        self.clear_all_btn = QPushButton("Clear All")
        self.clear_all_btn.clicked.connect(self.clear_all)
        self.balances_btn = QPushButton("Balances…")
        self.balances_btn.clicked.connect(self.prompt_balances)
        actions.addWidget(self.mark_all_btn)
        actions.addWidget(self.clear_all_btn)
        actions.addWidget(self.balances_btn)
        actions.addStretch()
        outer.addLayout(actions)

        # -- running reconcile math --
        # Two lines, because "Cleared balance" alone is not self-explanatory: it
        # folds in the BEGINNING balance (opening + everything already carrying
        # R), so an account with reconciled history shows a large number even
        # with nothing checked off -- read, reasonably, as "it says $4,000 is
        # cleared" right after pressing Clear All. The detail line splits the two
        # halves apart so the figure has a visible provenance.
        self.detail_label = QLabel()
        self.detail_label.setObjectName("reconcileDetail")
        outer.addWidget(self.detail_label)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("reconcileSummary")
        outer.addWidget(self.summary_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.finish_btn = buttons.addButton("Finish", QDialogButtonBox.AcceptRole)
        self.finish_btn.clicked.connect(self._finish)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._debits: list = []
        self._credits: list = []
        self._reload_rows()
        self._recompute()
        self.resize(880, 540)

    # ---- panes ------------------------------------------------------------
    def _make_pane(self, title):
        """Build one titled pane (a bordered box with a header and a table) and
        return (table, box). Clicking a row anywhere toggles its cleared mark."""
        box = QFrame()
        box.setObjectName("reconcilePane")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        header = QLabel(title)
        header.setObjectName("reconcilePaneTitle")
        lay.addWidget(header)
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["Clr", "Date", "Chk#", "Payee", "Amount"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(self.PAYEE, QHeaderView.Stretch)
        table.setColumnWidth(self.CLR, 30)
        table.setColumnWidth(self.DATE, 84)
        table.setColumnWidth(self.NUM, 48)
        table.setColumnWidth(self.AMOUNT, 96)
        table.cellClicked.connect(
            lambda row, _col, t=table: self._toggle_at(t, row))
        lay.addWidget(table)
        return table, box

    def _reload_rows(self):
        """Re-filter unreconciled rows to those dated ON OR BEFORE the statement
        date and split them into the debit (left) and credit (right) panes."""
        rows = ledger.unreconciled_rows(self.conn, self.account_id)
        if self.statement_date:
            rows = [r for r in rows if r["date"] <= self.statement_date]
        self._debits = [r for r in rows if r["amount"] < 0]
        self._credits = [r for r in rows if r["amount"] > 0]
        self._fill_pane(self.debits_table, self._debits, payment=True)
        self._fill_pane(self.credits_table, self._credits, payment=False)

    # Cleared rows are dimmed to this; uncleared rows keep the palette default.
    _CLEARED_ROW = QColor("#9aa0a6")

    def _fill_pane(self, table, rows, payment):
        table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            clr = self._center_cell("c" if r["cleared"] else "")
            clr.setData(Qt.UserRole, int(r["id"]))
            table.setItem(i, self.CLR, clr)
            table.setItem(i, self.DATE, self._cell(fmt_date(r["date"])))
            table.setItem(i, self.NUM, self._cell(r["num"] or ""))
            table.setItem(i, self.PAYEE, self._cell(self._display_payee(r)))
            amt = -r["amount"] if payment else r["amount"]
            table.setItem(i, self.AMOUNT, self._cell(fmt_cents(amt), right=True))
            self._paint_row(table, i, bool(r["cleared"]))

    def _paint_row(self, table, row, cleared):
        """Gray a whole row once it is cleared.

        The Clr mark sits at the far LEFT and the amount at the far RIGHT, so
        confirming "have I already checked this one?" means crossing the whole
        row with your eyes. That is fine until the same amount appears twice --
        two $7.28 charges -- where it is easy to re-click the row you already
        cleared and never notice. Dimming the entire row makes the answer
        available at the amount itself: a grayed 7.28 means find the other one.

        The Clr glyph keeps its own green and is left alone.
        """
        for col in range(table.columnCount()):
            item = table.item(row, col)
            if item is None or col == self.CLR:
                continue
            item.setForeground(QBrush(self._CLEARED_ROW) if cleared
                               else QBrush(self._row_default_color()))

    @staticmethod
    def _row_default_color():
        """The normal cell text color for an UNcleared row. Uses the theme's
        explicit color where there is one (dark mode would otherwise fall back to
        a near-black palette default and be illegible)."""
        return QColor(style.cell_text_color() or "#202124")

    def _display_payee(self, r) -> str:
        """The Payee to show. A transfer usually has no payee text, so fall back
        to its linked-account label (e.g. '[Savings]') -- the user's report was that
        the old dialog left transfer payees blank."""
        if r["payee"]:
            return r["payee"]
        if r["transfer_account_id"] is not None:
            return r["category_label"] or "[Transfer]"
        return ""

    @staticmethod
    def _cell(text, right=False):
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if right:
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return item

    @staticmethod
    def _center_cell(text):
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        item.setTextAlignment(Qt.AlignCenter)
        item.setForeground(QBrush(QColor("#2e7d32")))
        return item

    # ---- cleared marks ----------------------------------------------------
    def _shown_ids(self):
        return [int(r["id"]) for r in (self._debits + self._credits)]

    def _toggle_at(self, table, row):
        """Toggle the cleared mark of the transaction in ``row`` of ``table``.
        Only the clicked row's Clr glyph is repainted (no full rebuild), so the
        selection stays put."""
        item = table.item(row, self.CLR)
        if item is None:
            return
        txn_id = item.data(Qt.UserRole)
        cur = ledger.get_transaction(self.conn, txn_id)
        if cur is None:
            return
        new = 0 if cur["cleared"] else 1
        ledger.update_transaction(self.conn, txn_id, cleared=new)
        item.setText("c" if new else "")
        self._paint_row(table, row, bool(new))
        self._recompute()

    def mark_all(self):
        self._set_all(1)

    def clear_all(self):
        self._set_all(0)

    def _set_all(self, cleared):
        for txn_id in self._shown_ids():
            ledger.update_transaction(self.conn, txn_id, cleared=cleared)
        self._reload_rows()
        self._recompute()

    # ---- balances (the three inputs) --------------------------------------
    def prompt_balances(self, initial=False):
        """Open the setup dialog seeded with the current values. On OK, apply
        them and re-filter both panes and return True; on Cancel return False (a
        cancelled INITIAL prompt abandons the whole reconcile). Credit-card
        accounts get the card-statement dialog; everyone else the bank one."""
        if self.is_credit:
            return self._prompt_credit_balances(initial)
        dlg = ReconcileStartDialog(
            self.statement_date, self.beginning_cents, self.ending_cents,
            account_name=self.account_name, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return False
        v = dlg.values()
        self.set_balances(v["date"], v["beginning"], v["ending"])
        return True

    def set_balances(self, statement_date, beginning_cents, ending_cents):
        """Apply the three statement inputs, save them as the account's
        in-progress draft, and re-filter/recompute. Called by the Balances...
        dialog and directly by tests."""
        self.statement_date = self._clean_date(statement_date)
        self.beginning_cents = int(beginning_cents)
        self.ending_cents = int(ending_cents)
        self._save_draft()
        self._reload_rows()
        self._recompute()

    @staticmethod
    def _clean_date(value) -> str:
        """An ISO date, or "" if it is not one.

        Every reconcile bound compares ISO text, so a malformed date does not
        raise -- it sorts after (or before) every real row and silently changes
        which statement the window is showing. A live ledger stored
        '2026-03-152025-11-04' this way and reported four payments from other
        months as cleared. Anything unparseable is dropped to "" (unbounded),
        which is visibly wrong rather than plausibly wrong.
        """
        text = (value or "").strip()
        if not text:
            return ""
        import datetime as _dt
        try:
            _dt.date.fromisoformat(text)
        except ValueError:
            return ""
        return text

    def _save_draft(self):
        """Persist the statement inputs so closing the window and reopening it
        does not cost the user the whole statement retyped."""
        ledger.save_reconcile_draft(
            self.conn, self.account_id,
            statement_date=self.statement_date,
            beginning_cents=self.beginning_cents,
            ending_cents=self.ending_cents,
            charges_cents=self._charges_cents,
            payments_cents=self._payments_cents,
            credits_cents=self._credits_cents,
            finance_cents=self._finance_cents,
            finance_category=self._finance_category,
            finance_txn_id=self._finance_charge_id,
        )
        self.has_draft = True

    # ---- credit-card statement inputs -------------------------------------
    def _prompt_credit_balances(self, initial=False):
        """Open the credit-card statement dialog (charges / payments / credits /
        finance charge / ending-owed) seeded with the current values, then apply
        them. Owed ending balance is entered statement-positive; the workspace
        stores it register-signed (owed => negative)."""
        dlg = CreditReconcileStartDialog(
            self.conn, self.statement_date,
            self._charges_cents, self._payments_cents, self._credits_cents,
            -self.ending_cents, self._finance_cents, self._finance_category,
            account_name=self.account_name, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return False
        self.set_credit_balances(dlg.values())
        return True

    def set_credit_balances(self, values):
        """Apply the credit-card statement inputs: stash the statement totals,
        seed the reconcile target from the ENDING-OWED balance (negated into
        register sign), derive the beginning balance those figures imply,
        post/update/remove the finance charge, then re-filter/recompute. Called
        by the dialog and directly by tests."""
        self.statement_date = self._clean_date(values.get("date"))
        self._charges_cents = int(values.get("charges", 0))
        self._payments_cents = int(values.get("payments", 0))
        self._credits_cents = int(values.get("credits", 0))
        self._finance_cents = int(values.get("finance_charge", 0))
        self._finance_category = values.get("finance_category", "") or ""
        # Statement ending balance is the amount OWED (positive); Mammon stores an
        # owed balance as a negative register balance, so negate to get the
        # reconcile target the shared math compares the cleared balance against.
        self.ending_cents = -int(values.get("ending", 0))
        # A card statement implies its own beginning balance; the register's R
        # rows are never consulted for it (ledger.implied_card_beginning).
        self.beginning_cents = ledger.implied_card_beginning(
            self._charges_cents, self._payments_cents, self._credits_cents,
            self._finance_cents, int(values.get("ending", 0)))
        try:
            self._apply_finance_charge(
                self._finance_cents, self._finance_category, self.statement_date)
        except ValueError as exc:
            QMessageBox.warning(
                self, "Reconcile", f"Finance charge not posted: {exc}")
        # Saved AFTER the finance charge is posted, so the draft records the id
        # that _apply_finance_charge just created or reused.
        self._save_draft()
        self._reload_rows()
        self._recompute()

    def _apply_finance_charge(self, finance_cents, category, date):
        """Post the statement's finance charge as a CLEARED charge (negative
        amount -- a charge increases what you owe) dated on the statement date,
        classic, so it clears against the statement whose ending balance
        already includes it. Idempotent across 'Balances...' reopens: editing the
        amount updates the same transaction; a zero amount removes it."""
        finance_cents = int(finance_cents)
        cat_path = (category or "").strip()
        cat_id = ledger.resolve_category(self.conn, cat_path) if cat_path else None
        existing = (
            ledger.get_transaction(self.conn, self._finance_charge_id)
            if self._finance_charge_id is not None else None)
        if finance_cents <= 0:
            if existing is not None:
                ledger.delete_transaction(self.conn, self._finance_charge_id)
            self._finance_charge_id = None
            return
        amt = -finance_cents
        if existing is not None:
            ledger.update_transaction(
                self.conn, self._finance_charge_id,
                amount=amt, date=date, category_id=cat_id, cleared=1)
        else:
            self._finance_charge_id = ledger.add_transaction(
                self.conn, self.account_id, date, amt,
                payee="Finance Charge", category_id=cat_id, cleared=1)

    # ---- reconcile math ---------------------------------------------------
    def _recompute(self):
        # The difference/Finish gate uses the domain's own reconcile math against
        # the typed ENDING balance -- identical to finish_reconciliation's guard,
        # so what the UI enables is exactly what the ledger will allow. The typed
        # beginning is informational (Quicken pre-fills it from the register).
        #
        # The statement date is passed through so the math counts exactly the
        # rows the panes show. Without it the cleared sum included rows dated
        # AFTER the statement -- invisible here, and therefore unreachable by a
        # row click or by Clear All, which only touch shown rows.
        s = ledger.reconcile_summary(
            self.conn, self.account_id, self.ending_cents, self.statement_date,
            self.beginning_cents if self.is_credit else None)
        diff = s["difference"]
        if self.is_credit:
            counts = (f"{self.debits_table.rowCount()} charges, "
                      f"{self.credits_table.rowCount()} payments/credits shown")
        else:
            counts = (f"{self.debits_table.rowCount()} payments, "
                      f"{self.credits_table.rowCount()} deposits shown")
        origin = ("implied by the statement" if self.is_credit
                  else "already reconciled")
        detail = (f"Beginning balance ({origin}): "
                  f"{fmt_money(s['beginning_balance'])}")
        # On a BANK account the register's own beginning keeps driving the math --
        # there the running balance is the thing being protected, and it has to
        # carry forward unbroken from the last reconcile. The typed beginning is
        # therefore a CHECK, not an override: if the statement disagrees with the
        # register, previously-reconciled history moved, and that is worth seeing
        # rather than silently absorbing. (Discarding the field outright, which is
        # what used to happen, hid exactly this.) A card has no such continuity to
        # protect -- see ledger.implied_card_beginning -- so it is exempt.
        if not self.is_credit and self.beginning_cents != s["beginning_balance"]:
            detail += (f"        ** statement says "
                       f"{fmt_money(self.beginning_cents)} -- reconciled history "
                       f"has changed since the last statement **")
        if s["cleared_after"]:
            detail += (f"        ({fmt_money(s['cleared_after'])} cleared after "
                       f"{fmt_date(self.statement_date)} -- held for the next statement)")
        self.detail_label.setText(detail)
        # 'Cleared balance' is the sum of the items CHECKED in this window and
        # nothing else. It used to show beginning + checked, so an account with
        # reconciled history read "$3,142.00 cleared" with every box unchecked.
        # The beginning balance still drives the difference -- it just belongs on
        # its own line, not folded into a figure the user is checking items to
        # move.
        self.summary_label.setText(
            f"{counts}        "
            f"Cleared balance: {fmt_money(s['cleared_total'])}    "
            f"Statement ending: {fmt_money(self.ending_cents)}    "
            f"Difference: {fmt_money(diff)}")
        self.summary_label.setStyleSheet(
            "color:#2e7d32;" if diff == 0 else "color:#c0392b;")
        self.finish_btn.setEnabled(diff == 0)

    def _finish(self):
        try:
            ledger.finish_reconciliation(
                self.conn, self.account_id,
                self.statement_date, self.ending_cents,
                beginning_balance=self.beginning_cents if self.is_credit else None)
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Reconcile", str(exc))
            return
        self.finished_ok = True
        self.accept()


# ---------------------------------------------------------------------------
# accounts overview widget
# ---------------------------------------------------------------------------
class AccountsWidget(QWidget):
    """Every account with its balance and the total net worth."""

    accountActivated = pyqtSignal(int)

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.model = AccountsModel(conn)

        layout = QVBoxLayout(self)
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.horizontalHeader().setSectionResizeMode(
            AccountsModel.NAME, QHeaderView.Stretch)
        self.view.doubleClicked.connect(self._open)
        layout.addWidget(self.view)

        bar = QHBoxLayout()
        new_btn = QPushButton("New Account…")
        new_btn.clicked.connect(self.on_new_account)
        open_btn = QPushButton("Open Register")
        open_btn.clicked.connect(self._open_selected)
        bar.addWidget(new_btn)
        bar.addWidget(open_btn)
        bar.addStretch()
        self.total_label = QLabel()
        self.total_label.setStyleSheet("font-weight: bold;")
        bar.addWidget(self.total_label)
        layout.addLayout(bar)
        self.refresh()

    def refresh(self):
        self.model.reload()
        self.total_label.setText(f"Net worth: {fmt_money(self.model.net_worth())}")

    def _open(self, index):
        aid = self.model.account_id_at(index.row())
        if aid is not None:
            self.accountActivated.emit(aid)

    def _open_selected(self):
        idxs = self.view.selectionModel().selectedRows()
        if idxs:
            self._open(idxs[0])

    def on_new_account(self):
        dlg = NewAccountDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        v = dlg.values()
        if not v["name"]:
            QMessageBox.warning(self, "New account", "An account needs a name.")
            return
        try:
            ledger.create_account(self.conn, v["name"], v["type"],
                                  opening_balance=v["opening_balance"],
                                  opening_date=v["opening_date"])
        except Exception as exc:  # e.g. duplicate name (UNIQUE)
            QMessageBox.warning(self, "New account", str(exc))
            return
        self.refresh()


# ---------------------------------------------------------------------------
# left account bar (classic: narrow, blue account names, grouped, net worth)
# ---------------------------------------------------------------------------
class _AccountRow(QFrame):
    """One clickable account line: blue name on the left, right-aligned balance
    (red when negative). Clicking activates the account."""

    def __init__(self, account_id, name, balance_cents, activate, parent=None,
                 has_pending=False):
        super().__init__(parent)
        self._account_id = account_id
        self._activate = activate
        self.pending = bool(has_pending)
        self.setObjectName("acctRow")
        self.setProperty("selected", False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 2, 8, 2)
        lay.setSpacing(6)
        if self.pending:
            # A small filled dot before the name flags an account with downloaded
            # rows still waiting in its import-review list (survives restarts).
            dot = QLabel("●")
            dot.setObjectName("acctPending")
            dot.setToolTip("Pending review")
            lay.addWidget(dot)
        name_lbl = QLabel(name)
        name_lbl.setObjectName("acctName")
        bal = QLabel(fmt_cents(balance_cents))
        bal.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bal.setMinimumWidth(80)
        bal.setStyleSheet(
            f"color:{style.negative_color() if balance_cents < 0 else style.balance_text_color()};")
        lay.addWidget(name_lbl)
        lay.addStretch()
        lay.addWidget(bal)

    def mousePressEvent(self, event):  # noqa: N802 (Qt override)
        self._activate(self._account_id)
        super().mousePressEvent(event)

    def set_selected(self, on):
        self.setProperty("selected", bool(on))
        self.style().unpolish(self)
        self.style().polish(self)


class AccountBar(QWidget):
    """The narrow left panel: accounts grouped into Banking / Investing /
    Property & Debt sections, EACH DRAWN AS A BOXED CATEGORY with a bold
    subtotal (``$`` prefixed) and right-aligned balances (red when negative).
    Net Worth is pinned at the bottom, its amount aligned to the same right
    edge. Selecting an account opens its register. Exposes ``.model``
    (AccountsModel) for balances and net worth."""

    accountActivated = pyqtSignal(int)

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.model = AccountsModel(conn)
        self.setMinimumWidth(210)
        self.setMaximumWidth(300)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("acctScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self._body = QWidget()
        self._body.setObjectName("acctBody")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(8, 8, 8, 8)
        self._body_layout.setSpacing(10)
        self.scroll.setWidget(self._body)
        outer.addWidget(self.scroll, 1)

        # Net Worth strip: name left, amount aligned to the same right edge as
        # the account/category balances above it.
        self.net_row = QWidget()
        self.net_row.setObjectName("netWorth")
        nlay = QHBoxLayout(self.net_row)
        nlay.setContentsMargins(16, 6, 8, 6)
        title = QLabel("Net Worth")
        title.setStyleSheet(f"color:{style.accent_color()}; font-weight:bold;")
        self.net_amount = QLabel()
        self.net_amount.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.net_amount.setMinimumWidth(80)
        nlay.addWidget(title)
        nlay.addStretch()
        nlay.addWidget(self.net_amount)
        outer.addWidget(self.net_row)

        self._item_by_account: dict[int, _AccountRow] = {}
        self._selected: _AccountRow | None = None
        self.refresh()

    def refresh(self):
        self.model.reload()
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._item_by_account.clear()
        self._selected = None

        rows = self.model.rows()
        by_type: dict[str, list] = {}
        for r in rows:
            by_type.setdefault(r["type"], []).append(r)

        groups = list(_BAR_GROUPS)
        leftover = sorted({r["type"] for r in rows} - {t for _, ts in groups for t in ts})
        if leftover:
            groups = groups + [("Other", tuple(leftover))]

        for title, types in groups:
            group_rows = [r for t in types for r in by_type.get(t, [])]
            if not group_rows:
                continue
            self._body_layout.addWidget(self._build_group(title, group_rows))
        self._body_layout.addStretch(1)

        nw = self.model.net_worth()
        self.net_amount.setText(fmt_money(nw))
        self.net_amount.setStyleSheet(
            f"color:{style.negative_color() if nw < 0 else style.accent_color()}; font-weight:bold;")

        self._apply_min_width()

    # Fixed chrome inside a row/header (must mirror the layouts built below):
    #   account row  : contentsMargins(16,_,8,_) + spacing 6 * 2 gaps + balance min 80
    #   group header : contentsMargins(8,_,8,_) + spacing 6 * 2 gaps + total   min 80
    #   net-worth row: contentsMargins(16,_,8,_) + spacing 6 * 2 gaps + amount min 80
    _ROW_CHROME = 16 + 8 + 6 * 2 + 80        # account/net rows (24 margins)
    _HEADER_CHROME = 8 + 8 + 6 * 2 + 80      # group headers (16 margins)
    _BODY_MARGINS = 8 + 8                     # _body_layout contentsMargins
    _WIDTH_SAFETY = 4                         # a hair of slack past exact fit

    def _required_width(self) -> int:
        """Minimum panel width so the widest row plus the vertical scrollbar fit
        WITHOUT a horizontal scrollbar - so the user never has to drag the splitter to
        reveal a clipped account name. Computed from the actual account names
        (fontMetrics) + fixed row chrome + the scrollbar extent; recomputed on
        every refresh so it tracks the current account list. Never a fixed pin:
        it only RAISES the floor, leaving the splitter draggable."""
        rows = self.model.rows()
        base_fm = QFontMetrics(self.font())
        bold = QFont(self.font())
        bold.setBold(True)
        bold_fm = QFontMetrics(bold)

        # widest account/net-worth row (base font) vs widest group header (bold)
        name_w = max((_text_width(base_fm, r["name"]) for r in rows), default=0)
        net_w = _text_width(bold_fm, "Net Worth")
        row_w = max(name_w, net_w) + self._ROW_CHROME

        titles = [t for t, _ in _BAR_GROUPS] + ["Other"]
        title_w = max((_text_width(bold_fm, t) for t in titles), default=0)
        header_w = title_w + self._HEADER_CHROME

        content_w = max(row_w, header_w) + self._BODY_MARGINS
        sb = self.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        return max(210, content_w + sb + self._WIDTH_SAFETY)

    def _apply_min_width(self):
        """Raise the panel's minimum width to fit content + vertical scrollbar,
        keeping the splitter draggable (only widen the max cap if the floor needs
        more room than the default narrow-bar cap)."""
        floor = self._required_width()
        self.setMinimumWidth(floor)
        if self.maximumWidth() < floor:
            self.setMaximumWidth(floor + 40)

    def _build_group(self, title, group_rows):
        box = QFrame()
        box.setObjectName("acctGroup")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        header = QWidget()
        header.setObjectName("acctGroupHeader")
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(8, 4, 8, 4)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("acctGroupTitle")
        total = sum(r["balance"] for r in group_rows)
        total_lbl = QLabel(fmt_money(total))
        total_lbl.setObjectName("acctGroupTotal")
        total_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        total_lbl.setMinimumWidth(80)
        total_lbl.setStyleSheet(
            f"color:{style.negative_color() if total < 0 else style.text_color()}; font-weight:bold;")
        hlay.addWidget(title_lbl)
        hlay.addStretch()
        hlay.addWidget(total_lbl)
        lay.addWidget(header)

        for r in group_rows:
            has_pending = False
            try:
                has_pending = import_review.count_pending(self.conn, r["id"]) > 0
            except Exception:  # pragma: no cover - defensive (e.g. legacy schema)
                has_pending = False
            row = _AccountRow(r["id"], r["name"], r["balance"], self._activate,
                              has_pending=has_pending)
            lay.addWidget(row)
            self._item_by_account[r["id"]] = row
        return box

    def pending_icon_visible(self, account_id) -> bool:
        """True when ``account_id``'s row shows the pending-review dot."""
        row = self._item_by_account.get(account_id)
        return bool(row is not None and getattr(row, "pending", False))

    def _activate(self, account_id):
        self.select_account(account_id)
        self.accountActivated.emit(int(account_id))

    def _on_item(self, item, _col=0):
        """Back-compat hook: activate the account for a row widget."""
        aid = getattr(item, "_account_id", None)
        if aid is not None:
            self._activate(aid)

    def select_account(self, account_id):
        row = self._item_by_account.get(account_id)
        if self._selected is not None and self._selected is not row:
            self._selected.set_selected(False)
        if row is not None:
            row.set_selected(True)
            self._selected = row

    def on_new_account(self):
        dlg = NewAccountDialog(self)
        if dlg.exec_() != QDialog.Accepted:
            return
        v = dlg.values()
        if not v["name"]:
            QMessageBox.warning(self, "New account", "An account needs a name.")
            return
        try:
            ledger.create_account(self.conn, v["name"], v["type"],
                                  opening_balance=v["opening_balance"],
                                  opening_date=v["opening_date"])
        except Exception as exc:  # e.g. duplicate name (UNIQUE)
            QMessageBox.warning(self, "New account", str(exc))
            return
        self.refresh()


# ---------------------------------------------------------------------------
# find dialog (search transactions: within one account or globally)
# ---------------------------------------------------------------------------
class SearchDialog(QDialog):
    """Find transactions by text or amount, scoped to a single account or across
    ALL accounts (the global find). Double-clicking a result asks the main
    window to open that account's register and select the transaction. Sits on
    :func:`mammon.ledger.search_transactions` (no SQL here)."""

    activated = pyqtSignal(int, int)   # (account_id, txn_id) -> open + select
    changed = pyqtSignal()             # a Replace wrote the DB; registers reload

    def __init__(self, conn, default_account_id=None, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Find transactions")
        # Modeless (by request): the register stays interactive so the user
        # can jump to a result, edit it, and come back to the (refreshed) list.
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)
        self.resize(640, 360)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)

        top = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText(
            "Search payee, security, memo, tag, category, amount…")
        self.query.returnPressed.connect(self.run_search)
        self.scope = QComboBox()
        self.scope.addItem("All accounts", None)
        for a in ledger.list_accounts(conn, include_closed=True):
            self.scope.addItem(a["name"], a["id"])
        if default_account_id is not None:
            i = self.scope.findData(default_account_id)
            if i >= 0:
                self.scope.setCurrentIndex(i)
        find_btn = QPushButton("Find")
        find_btn.clicked.connect(self.run_search)
        top.addWidget(self.query, 1)
        top.addWidget(self.scope)
        top.addWidget(find_btn)
        lay.addLayout(top)

        self.results = SearchResultsModel()
        self.view = QTableView()
        self.view.setModel(self.results)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.verticalHeader().setVisible(False)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.horizontalHeader().setSectionResizeMode(
            SearchResultsModel.MEMO, QHeaderView.Stretch)
        self.view.doubleClicked.connect(self._activate)
        # Denser results (by request): a smaller font and tight rows so the
        # dialog blocks as little of the register as possible.
        result_font = QFont(self.view.font())
        pt = result_font.pointSizeF()
        result_font.setPointSizeF(max(7.0, (pt if pt > 0 else 9.0) - 1.0))
        self.view.setFont(result_font)
        self.view.horizontalHeader().setFont(result_font)
        vh = self.view.verticalHeader()
        vh.setDefaultSectionSize(vh.fontMetrics().height() + 4)
        lay.addWidget(self.view)

        # Find and replace (parity): set ONE field -- payee, memo, tag, num or
        # category -- on the highlighted result or on every result. The whole
        # field is replaced (Quicken's semantics), blank clears it, and the
        # category form skips transfers and splits, whose category is not a
        # free field.
        rep = QHBoxLayout()
        self.replace_field = QComboBox()
        for label, key in (("Payee", "payee"), ("Memo", "memo"), ("Tag", "tag"),
                           ("Num", "num"), ("Category", "category")):
            self.replace_field.addItem(label, key)
        self.replace_with = QLineEdit()
        self.replace_with.setPlaceholderText("Replace with… (blank clears the field)")
        self.replace_selected_btn = QPushButton("Replace Selected")
        self.replace_all_btn = QPushButton("Replace All")
        for btn in (self.replace_selected_btn, self.replace_all_btn):
            btn.setAutoDefault(False)
        self.replace_selected_btn.clicked.connect(lambda: self.replace(all_results=False))
        self.replace_all_btn.clicked.connect(lambda: self.replace(all_results=True))
        rep.addWidget(QLabel("Replace"))
        rep.addWidget(self.replace_field)
        rep.addWidget(self.replace_with, 1)
        rep.addWidget(self.replace_selected_btn)
        rep.addWidget(self.replace_all_btn)
        lay.addLayout(rep)

        self.status = QLabel("")
        self.status.setObjectName("registerSub")
        lay.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.query.setFocus()

    def replace(self, *, all_results: bool) -> tuple[int, int]:
        """Apply the Replace row to the highlighted result or to all of them.
        Confirms first (the QMessageBox.question seam), writes through
        :func:`ledger.replace_field`, teaches the payee mapping for a category,
        then re-runs the search and tells the main window to reload registers.
        Returns ``(changed, skipped)``."""
        field = self.replace_field.currentData()
        value = self.replace_with.text()
        if all_results:
            ids = [int(self.results.result_at(i)["id"])
                   for i in range(self.results.rowCount())]
        else:
            tid = self._selected_txn_id()
            ids = [tid] if tid is not None else []
        if not ids:
            self.status.setText("Nothing to replace: select a result, or use Replace All.")
            return (0, 0)
        label = self.replace_field.currentText()
        what = f"Set {label} to {value.strip()!r}" if value.strip() else f"Clear {label}"
        if QMessageBox.question(
                self, "Replace", f"{what} on {len(ids)} transaction(s)?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return (0, 0)
        payees = set()
        if field == "category" and value.strip():
            for tid in ids:
                row = ledger.get_transaction(self.conn, tid)
                if row is not None and row["payee"]:
                    payees.add(row["payee"])
        changed, skipped = ledger.replace_field(self.conn, ids, field, value)
        if changed and payees:
            cid = ledger.resolve_category(self.conn, value)
            for payee in payees:
                categorize.record_user_categorization(self.conn, payee, cid)
        self.changed.emit()
        self.run_search()
        note = f", {skipped} skipped (transfers or splits)" if skipped else ""
        self.status.setText(f"Replaced on {changed} transaction(s){note}.")
        return changed, skipped

    def run_search(self):
        aid = self.scope.currentData()
        # Preserve the highlighted result across a refresh where possible, so an
        # in-place edit that leaves the row still-matching doesn't lose the spot.
        keep = self._selected_txn_id()
        hits = ledger.search_transactions(
            self.conn, self.query.text(), account_id=aid)
        self.results.set_results(hits)
        where = "all accounts" if aid is None else self.scope.currentText()
        q = self.query.text().strip()
        if not q:
            self.status.setText("Type something to search for.")
        else:
            self.status.setText(f"{len(hits)} match(es) in {where}")
        if keep is not None:
            self._reselect_txn(keep)
        return hits

    def refresh_results(self):
        """Re-run the current query (used by the main window after a register
        edit): a transaction that no longer matches drops out of the list, one
        that now matches appears -- all while the dialog stays open (modeless)."""
        if self.query.text().strip():
            self.run_search()

    def _selected_txn_id(self):
        idxs = self.view.selectionModel().selectedRows()
        if not idxs:
            return None
        r = self.results.result_at(idxs[0].row())
        return int(r["id"]) if r is not None else None

    def _reselect_txn(self, txn_id):
        for row in range(self.results.rowCount()):
            r = self.results.result_at(row)
            if r is not None and int(r["id"]) == txn_id:
                self.view.selectRow(row)
                return

    def _activate(self, index):
        r = self.results.result_at(index.row())
        if r is not None:
            self.activated.emit(int(r["account_id"]), int(r["id"]))


# ---------------------------------------------------------------------------
# display preferences dialog (Settings > Display Preferences)
# ---------------------------------------------------------------------------
class _ColorButton(QPushButton):
    """A push button that shows its current color as a swatch and opens a color
    picker when clicked. Holds the chosen color as a ``#rrggbb`` string."""

    def __init__(self, color: str, parent=None):
        super().__init__(parent)
        self._color = color
        self.clicked.connect(self._pick)
        self.set_color(color)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = color
        self.setText(color)
        c = QColor(color)
        # keep the label legible on both light and dark swatches
        fg = "#ffffff" if c.lightness() < 140 else "#222222"
        self.setStyleSheet(f"background:{color}; color:{fg}; padding:3px 12px;")

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self._color), self, "Choose color")
        if c.isValid():
            self.set_color(c.name())


class DisplayPreferencesDialog(QDialog):
    """Settings > Display Preferences: choose the register's appearance -- font
    family + point size, alternating-row shading on/off + its color, the
    negative-amount color, and the default one/two-line view. Values persist via
    mammon.ui.prefs (QSettings; no DB write) and apply live to open registers.
    Defaults reproduce the current look exactly, so nothing changes unless the
    user opts in; Restore Defaults resets every field to that look."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Display Preferences")
        form = QFormLayout(self)

        # Light / Dark theme. Switching it reseeds the color swatches to that
        # theme's defaults so what gets saved matches the chosen background.
        self.theme = QComboBox()
        self.theme.addItem("Light", "light")
        self.theme.addItem("Dark", "dark")
        self.theme.setCurrentIndex(1 if prefs.theme() == "dark" else 0)
        self.theme.currentIndexChanged.connect(self._on_theme_changed)

        self.font_family = QFontComboBox()
        self.font_family.setCurrentFont(QFont(prefs.font_family()))
        self.font_size = QSpinBox()
        self.font_size.setRange(6, 24)
        self.font_size.setValue(prefs.font_size())

        self.row_shading = QCheckBox("Shade alternating rows")
        self.row_shading.setChecked(prefs.row_shading())
        self.alt_row_btn = _ColorButton(prefs.alt_row_color())
        self.negative_btn = _ColorButton(prefs.negative_color())

        self.two_line = QCheckBox("Open registers in two-line view")
        self.two_line.setChecked(prefs.two_line_default())
        self.sound = QCheckBox("Play a sound when a transaction is saved")
        self.sound.setChecked(prefs.sound_enabled())

        # Date DISPLAY format (parsing/storage stay ISO). Every shown date -- the
        # register, the import-review list, report/plot axis labels -- renders
        # through fmt_date, which reads this preference.
        self.date_format = QComboBox()
        for f in prefs.DATE_FORMATS:
            self.date_format.addItem(f, f)
        di = self.date_format.findData(prefs.date_format())
        self.date_format.setCurrentIndex(di if di >= 0 else 0)

        form.addRow("Theme", self.theme)
        form.addRow("Font", self.font_family)
        form.addRow("Font size", self.font_size)
        form.addRow("", self.row_shading)
        form.addRow("Alternate-row color", self.alt_row_btn)
        form.addRow("Negative-amount color", self.negative_btn)
        form.addRow("Date format", self.date_format)
        form.addRow("", self.two_line)
        form.addRow("", self.sound)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
            | QDialogButtonBox.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(
            self.restore_defaults)
        form.addRow(buttons)

    def _on_theme_changed(self, _index) -> None:
        """Reseed the color swatches to the newly chosen theme's REMEMBERED
        colors -- that theme's own saved pick, or its palette default when unset.
        Each theme keeps its own colors (prefs stores them per theme), so seeding
        from the palette default here would clobber a saved pick the moment the
        user toggled the theme combo back and forth. An explicit later pick still
        wins."""
        name = self.theme.currentData()
        self.alt_row_btn.set_color(prefs.alt_row_color(theme_name=name))
        self.negative_btn.set_color(prefs.negative_color(theme_name=name))

    def restore_defaults(self) -> None:
        self.theme.setCurrentIndex(0)     # back to Light (also reseeds swatches)
        self.font_family.setCurrentFont(QFont(prefs.DEFAULT_FONT_FAMILY))
        self.font_size.setValue(prefs.DEFAULT_FONT_SIZE)
        self.row_shading.setChecked(prefs.DEFAULT_ROW_SHADING)
        self.alt_row_btn.set_color(prefs.DEFAULT_ALT_ROW_COLOR)
        self.negative_btn.set_color(prefs.DEFAULT_NEGATIVE_COLOR)
        self.two_line.setChecked(prefs.DEFAULT_TWO_LINE)
        self.sound.setChecked(prefs.DEFAULT_SOUND)
        dd = self.date_format.findData(prefs.DEFAULT_DATE_FORMAT)
        self.date_format.setCurrentIndex(dd if dd >= 0 else 0)

    def values(self) -> dict:
        return {
            "theme": self.theme.currentData(),
            "font_family": self.font_family.currentFont().family(),
            "font_size": self.font_size.value(),
            "row_shading": self.row_shading.isChecked(),
            "alt_row_color": self.alt_row_btn.color(),
            "negative_color": self.negative_btn.color(),
            "two_line": self.two_line.isChecked(),
            "sound": self.sound.isChecked(),
            "date_format": self.date_format.currentData(),
        }


class DownloadLogDialog(QDialog):
    """Read-only viewer for the persistent download log
    (``<data dir>/download.log``). Shows recent attempts newest-first, each with
    its raw MCP run summary/error AND Mammon's final success/failure decision, so
    a run that "returned good data but reported an error" is diagnosable. The log
    file path is shown so it can be opened outside Mammon too."""

    def __init__(self, parent=None, db_path=None):
        super().__init__(parent)
        from PyQt5.QtWidgets import QLabel, QVBoxLayout
        from mammon import download_log
        self._download_log = download_log
        self._db_path = db_path
        self.setWindowTitle("Download Log")
        self.setMinimumSize(760, 520)
        layout = QVBoxLayout(self)
        self._path_label = QLabel(self)
        try:
            self._path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        except Exception:
            pass
        self._path_label.setWordWrap(True)
        layout.addWidget(self._path_label)
        self._view = QPlainTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self._view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        refresh = buttons.addButton("Refresh", QDialogButtonBox.ActionRole)
        refresh.clicked.connect(self._reload)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self._reload()

    def _reload(self):
        path = self._download_log.default_log_path(self._db_path)
        self._path_label.setText("Log file: " + path)
        entries = self._download_log.read_recent(200, log_path=path)
        if not entries:
            self._view.setPlainText(
                "No download attempts have been logged yet.\n\n"
                "The log is written to:\n" + path)
            return
        blocks = [self._download_log.format_entry(e) for e in reversed(entries)]
        self._view.setPlainText("\n\n".join(blocks))


# ---------------------------------------------------------------------------
# main window: account bar on the left, the selected register on the right
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, conn, db_path=None, parent=None, webslinger=None,
                 db_key=None):
        super().__init__(parent)
        self.conn = conn
        self.db_path = db_path
        # The key for THIS session, in memory only: never written to QSettings,
        # the database, or any file (mammon.ui.password_dialog). None means the
        # ledger is plaintext, which is the default and stays completely silent.
        self.db_key = db_key
        # The automated-download collaborator. It degrades gracefully when
        # nothing is configured (Download simply explains what is missing);
        # tests inject a fake. Mammon holds NO credentials of its own -- the
        # webSlinger side owns login/MFA entirely.
        self.webslinger = (webslinger if webslinger is not None
                           else webslinger_mod.default_client())
        # Either a cash RegisterWidget or an InvestmentRegisterWidget per account.
        self._registers: dict[int, QWidget] = {}
        self._find_dialog = None    # the modeless Find dialog, when open
        # Register view: one-line vs two-line, seeded from the saved preference.
        self.register_view_mode = "two" if prefs.two_line_default() else "one"
        # Wide enough to read a register without maximizing. At 1100 the ten
        # one-line columns left Payee under 160px, so an ordinary payee like
        # "Anytown Water District" clipped on a freshly installed app -- the
        # first thing a new user sees. Clamped to the available screen so a
        # small or scaled display still gets a window that fits on it.
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        self.resize(min(1360, int(avail.width() * 0.92)) if avail else 1360,
                    min(820, int(avail.height() * 0.92)) if avail else 820)
        self._autobackup_timer = None
        self._last_backup_fingerprint = None   # see _autobackup_tick
        icon = _app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self._build_menu()
        # Reminders (parity): pre-enter every payment that has come due before
        # the registers load, so the rows are there the first time each is
        # opened. Switchable from the Scheduled Payments manager.
        self._auto_enter_scheduled()
        self._install_central(conn)
        self._update_title()
        self._sync_scheduled_label()
        self._start_autobackup()

    def _auto_enter_scheduled(self) -> int:
        """Pre-enter due scheduled payments (their placeholders) at launch when
        the preference is on. Never blocks a launch: a broken definition is
        logged by the crash log's excepthook path, not raised here."""
        if not prefs.auto_enter_on_launch():
            return 0
        try:
            return len(scheduled.generate_all_due(
                self.conn, QDate.currentDate().toString("yyyy-MM-dd")))
        except Exception:                     # pragma: no cover - defensive
            return 0

    def _sync_scheduled_label(self) -> None:
        """The Tools menu entry carries how many reminders need attention:
        'Scheduled Payments (2 due)…'."""
        act = getattr(self, "act_scheduled", None)
        if act is None:
            return
        try:
            counts = scheduled.due_counts(
                self.conn, QDate.currentDate().toString("yyyy-MM-dd"))
        except Exception:                     # pragma: no cover - defensive
            counts = {}
        n = counts.get("overdue", 0) + counts.get("due_today", 0) + counts.get("due_soon", 0)
        act.setText(f"Scheduled Payments ({n} due)…" if n else "Scheduled Payments…")

    # ---- construction -----------------------------------------------------
    def _build_menu(self):
        menu = self.menuBar().addMenu("&File")
        menu.addAction("New Account…", lambda: self.accounts.on_new_account())
        menu.addSeparator()
        menu.addAction("New Database…", self._new_database_dialog)
        menu.addAction("Open Database…", self._open_database_dialog)
        menu.addAction("Save Database As…", self._save_db_as_dialog)
        # A file operation, not a preference: it rewrites the database file the
        # same way Back Up and Restore do. It sat under Settings, where nothing
        # else touches the file at all.
        menu.addAction("Database Password…", self._database_password_dialog)
        menu.addAction("Import Quicken File (QIF)…", self._import_qif_dialog)
        menu.addAction("Export Ledger…", self._export_dialog)
        menu.addSeparator()
        menu.addAction("Back Up Database Now…", self._backup_now)
        menu.addAction("Restore from Backup…", self._restore_backup_dialog)
        menu.addSeparator()
        menu.addAction("Quit", self.close)

        edit = self.menuBar().addMenu("&Edit")
        self.act_undo = edit.addAction("Undo", self._undo_current)
        self.act_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self.act_redo = edit.addAction("Redo", self._redo_current)
        # Both the Windows redo (Ctrl+Y) and the common Ctrl+Shift+Z bind to redo.
        self.act_redo.setShortcuts(
            [QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        edit.addSeparator()
        # The Undo/Redo labels and enabled state track the focused register's
        # stack; refresh them whenever the menu opens (and on every write, wired
        # in open_register, so the shortcuts enable without opening the menu).
        edit.aboutToShow.connect(self._sync_edit_actions)
        find_act = edit.addAction("Find Transactions…", self._find_transactions_dialog)
        find_act.setShortcut("Ctrl+F")
        self._sync_edit_actions()

        view = self.menuBar().addMenu("&View")
        self._one_line_act = view.addAction(
            "One Line", lambda: self._set_register_view_mode("one"))
        self._two_line_act = view.addAction(
            "Two Lines", lambda: self._set_register_view_mode("two"))
        group = QActionGroup(self)
        group.setExclusive(True)
        for act, mode in ((self._one_line_act, "one"), (self._two_line_act, "two")):
            act.setCheckable(True)
            act.setChecked(self.register_view_mode == mode)
            group.addAction(act)

        tools = self.menuBar().addMenu("&Tools")
        tools.addAction("Accounts…", self._accounts_list_dialog)
        tools.addAction("Download Log…", self._download_log_dialog)
        tools.addAction("Category Manager…", self._manage_categories_dialog)
        tools.addAction("Tag Manager…", self._manage_tags_dialog)
        tools.addAction("Budgets…", self._budgets_dialog)
        tools.addAction("Payee Renaming…", self._rename_rules_dialog)
        # Securities is a FILE-wide operation, not an account one: the same
        # holding under two spellings is exactly the case where fixing it in
        # one register would leave the others broken, so it does not belong on
        # the investment register's gear beside the per-account actions.
        tools.addAction("Securities…", self._securities_dialog)
        tools.addAction("Rules Manager…", self._rules_manager_dialog)
        self.act_scheduled = tools.addAction("Scheduled Payments…",
                                             self._scheduled_payments_dialog)
        tools.addAction("Projected Balances…", self._projected_balances_dialog)
        tools.addAction("Financial Calendar", self.show_calendar)

        settings = self.menuBar().addMenu("&Settings")
        settings.addAction("Edit Account Details…", self._edit_account_details_dialog)
        # Loan Setup now lives at the TOP of the loan register (a toolbar button
        # shown only for loan/liability accounts), not here in Settings.
        settings.addAction("Reconcile to Statement…", self._reconcile_dialog)
        settings.addAction("Print Register…", self._print_register)
        settings.addSeparator()
        settings.addAction("Display Preferences…", self._display_preferences_dialog)

        reports = self.menuBar().addMenu("&Reports")
        reports.addAction("Spending by Category…", self._spending_report_dialog)
        reports.addAction("Itemize by Category…", self._itemize_window)
        reports.addAction("Spending Chart (Pie)…", self._spending_chart_dialog)
        reports.addAction("Income Chart (Pie)…", self._income_chart_dialog)
        reports.addAction("Net Worth Over Time…", self._net_worth_chart_dialog)
        reports.addSeparator()
        reports.addAction("Cash Flow (Window)…", self._cash_flow_window)
        reports.addAction("Income vs Expense (Window)…", self._income_expense_window)
        reports.addAction("Account Balances (Window)…", self._balances_window)
        reports.addAction("By Payee (Window)…", self._payee_window)
        reports.addAction("By Tag (Window)…", self._tag_window)
        reports.addAction("Transactions (Window)…", self._listing_window)
        reports.addAction("Investment Performance (Window)…",
                          self._investment_performance_window)
        reports.addSeparator()
        # Allocation is a REPORT, not a tool: it answers "where is my money",
        # which is what everything else on this menu answers. (Quicken hangs it
        # off the Investing tab, which Mammon does not have.)
        reports.addAction("Asset Allocation…", self._allocation_dialog)
        reports.addAction("Target && Drift…", self._rebalance_dialog)

    # ---- undo / redo ------------------------------------------------------
    def _current_undo_model(self):
        """The undo stack of the register on screen, or None. Investment and
        crypto registers have no stack yet, so this returns None for them and
        the Edit menu's Undo/Redo stay disabled."""
        reg = self._registers.get(getattr(self, "_current_account", None))
        model = getattr(reg, "model", None)
        return model if getattr(model, "can_undo", None) is not None else None

    def _undo_current(self):
        model = self._current_undo_model()
        if model is not None and model.can_undo():
            model.undo()

    def _redo_current(self):
        model = self._current_undo_model()
        if model is not None and model.can_redo():
            model.redo()

    def _sync_edit_actions(self):
        """Enable/disable Undo/Redo and show what they would reverse, from the
        focused register's stack state."""
        act_undo = getattr(self, "act_undo", None)
        act_redo = getattr(self, "act_redo", None)
        if act_undo is None or act_redo is None:
            return
        model = self._current_undo_model()
        can_undo = bool(model is not None and model.can_undo())
        can_redo = bool(model is not None and model.can_redo())
        act_undo.setEnabled(can_undo)
        act_redo.setEnabled(can_redo)
        ulabel = model.undo_label() if can_undo else None
        rlabel = model.redo_label() if can_redo else None
        act_undo.setText(f"Undo {ulabel}" if ulabel else "Undo")
        act_redo.setText(f"Redo {rlabel}" if rlabel else "Redo")

    # ---- database backup --------------------------------------------------
    def _start_autobackup(self):
        """Take a rotating snapshot roughly every minute for the life of the
        session. Needs a real file path to name the snapshot; an unsaved/in-memory
        database (no db_path) is skipped."""
        if not self.db_path:
            return
        # Seed the change signature from the CURRENT state, so an idle session
        # writes no snapshots at all: the first tick only fires if something was
        # actually edited after startup.
        self._last_backup_fingerprint = backup.db_fingerprint(
            self.conn, self.db_path)
        # One-time cleanup at startup so a folder that grew while the app was
        # closed (or across a db rename) is trimmed immediately, not only after
        # the first tick fires.
        self._purge_old_backups()
        timer = QTimer(self)
        timer.setInterval(backup.AUTO_INTERVAL_MS)
        timer.timeout.connect(self._autobackup_tick)
        timer.start()
        self._autobackup_timer = timer

    def _purge_old_backups(self):
        """Apply the time-based retention window to the auto-backup folder.
        Must never interrupt the session, so swallow any failure."""
        try:
            backup.purge_auto_backups()
        except Exception:  # pragma: no cover - defensive
            pass

    def _autobackup_tick(self):
        """Snapshot ONLY when the database actually changed since the last one.

        The timer fires on a fixed interval regardless of activity, so backing up
        unconditionally rewrites the whole file every minute whether or not
        anything happened. On a 40-year ledger that is ~12 MB a minute and, at
        DEFAULT_AUTO_KEEP, over a gigabyte of near-identical copies. Skipping an
        unchanged database keeps the one-minute granularity for the minutes that
        matter and costs nothing for the ones that do not.

        The snapshot itself is INCREMENTAL: only the pages that differ from the
        newest full snapshot are stored, which on the real ledger is ~15 KB
        instead of ~12 MB. The two guards address different halves of the same
        waste -- the fingerprint stops us writing when nothing changed at all,
        the delta stops us rewriting 99.5% of a file that barely moved.

        A backup must never interrupt the session, so any failure is swallowed --
        and the fingerprint is only advanced after a snapshot actually succeeds,
        so a failed write is retried on the next tick rather than skipped."""
        try:
            fingerprint = backup.db_fingerprint(self.conn, self.db_path)
            if fingerprint == getattr(self, "_last_backup_fingerprint", None):
                return                      # nothing changed; write nothing
            # The key goes with it: the online backup API refuses an unkeyed
            # target for an encrypted source, and this handler swallows failures,
            # so omitting it would silently stop backing up an encrypted ledger.
            backup.create_backup(self.conn, self.db_path, tag=backup.AUTO_TAG,
                                  keep=backup.DEFAULT_AUTO_KEEP,
                                  incremental=True, key=self.db_key)
            self._last_backup_fingerprint = fingerprint
            # Prune anything now past the retention window (idempotent, cheap).
            backup.purge_auto_backups()
        except Exception:  # pragma: no cover - defensive
            pass

    def _backup_now(self):
        """Manual, user-triggered snapshot (kept, never auto-pruned)."""
        if not self.db_path:
            QMessageBox.warning(self, "Back Up Database",
                                "This database has no file on disk to back up.")
            return
        try:
            path = backup.create_backup(self.conn, self.db_path,
                                        tag=backup.MANUAL_TAG, key=self.db_key)
        except Exception as exc:
            QMessageBox.critical(self, "Back Up Database", f"Backup failed:\n{exc}")
            return
        QMessageBox.information(self, "Back Up Database",
                                f"Backup written to:\n{path}")

    def _restore_backup_dialog(self):
        """File > Restore from Backup: replace the current database with a chosen
        snapshot. A ``.bak`` file is itself a standalone SQLite database and a
        ``.delta`` is rebuilt from its baseline, so the picker accepts either and
        ``backup.restore_backup`` hides the difference; the current file is
        snapshotted first (safety net) and the live connection is closed before
        the on-disk write so Windows lets us overwrite it.

        The picker opens on THIS database's own backup folder, and a snapshot
        belonging to a different database is refused outright rather than
        confirmed -- restoring one succeeds at the file level and leaves a valid,
        completely wrong ledger in place (see the mammon.backup docstring)."""
        import os
        from mammon import backup
        start_dir = ""
        if self.db_path:
            bdir = backup.backup_dir_for(self.db_path)
            if not os.path.isdir(str(bdir)):
                bdir = backup.DEFAULT_BACKUP_DIR
            start_dir = str(bdir) if os.path.isdir(str(bdir)) else \
                os.path.dirname(os.path.abspath(self.db_path))
        path, _ = QFileDialog.getOpenFileName(
            self, "Restore from Backup", start_dir,
            "Backup / database files (*.bak *.delta *.db);;All files (*)")
        if not path:
            return
        if not self.db_path:
            # Nothing to overwrite -- just open the chosen snapshot directly.
            self.open_database(path)
            return
        try:
            backup.check_same_database(path, self.db_path)
        except backup.ForeignSnapshotError as exc:
            QMessageBox.critical(self, "Restore from Backup", str(exc))
            return
        # Show what this snapshot changed (read through the backup module -- no
        # SQL or money logic lives here) so the user can tell restore points apart.
        summary = backup.snapshot_summary(path)
        changed = f"\n\nChanges in this backup:\n{summary}" if summary else ""
        if QMessageBox.question(
                self, "Restore from Backup",
                f"Replace the current database with:\n{path}{changed}\n\n"
                "A backup of the current database is taken first. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        target = self.db_path
        try:                                    # safety snapshot of current state
            backup.create_backup(self.conn, target, tag=backup.MANUAL_TAG,
                                 key=self.db_key)
        except Exception:                       # pragma: no cover - best effort
            pass
        if self._find_dialog is not None:       # dialogs bound to the old conn
            self._find_dialog.close()
            self._find_dialog = None
        try:
            self.conn.close()
        except Exception:                       # pragma: no cover - defensive
            pass
        self.conn = None
        try:
            # Handles both kinds, and verifies a rebuilt delta against the
            # checksum recorded when it was written -- restoring a file that
            # silently differs from the snapshot would be the worst outcome here.
            backup.restore_backup(path, target)
        except Exception as exc:
            self.open_database(target)          # reopen the untouched original
            QMessageBox.critical(self, "Restore from Backup",
                                 f"Restore failed:\n{exc}")
            return
        self.open_database(target)              # rebind everything to restored file
        QMessageBox.information(self, "Restore from Backup",
                                f"Restored from:\n{path}")

    def _export_dialog(self):
        """File > Export Ledger: the whole ledger (or part of it) out as QIF,
        JSON or CSV -- see mammon.export for what each carries."""
        from mammon.ui.export_dialog import ExportDialog, perform
        dlg = ExportDialog(self.conn, parent=self, db_path=self.db_path)
        if dlg.exec_() != QDialog.Accepted:
            return
        try:
            summary = perform(self.conn, dlg.values())
        except Exception as exc:
            QMessageBox.critical(self, "Export Ledger", f"Export failed:\n{exc}")
            return
        QMessageBox.information(self, "Export Ledger", summary)

    def _save_db_as_dialog(self):
        """File > Save Database As: write a consistent copy of the live database
        to a new file and switch the window to it (the old file is left as-is).
        Uses SQLite's online backup API, so no need to close the connection."""
        import os
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Database As", "",
            "SQLite database (*.db);;All files (*)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".db"
        if self.db_path and os.path.abspath(path) == os.path.abspath(self.db_path):
            QMessageBox.warning(self, "Save Database As",
                                "That is the current database file.")
            return
        try:
            dest = db.connect(path)
            try:
                self.conn.backup(dest)
            finally:
                dest.close()
        except Exception as exc:
            QMessageBox.critical(self, "Save Database As", f"Save failed:\n{exc}")
            return
        self.open_database(path)
        QMessageBox.information(self, "Save Database As",
                                f"Database saved to:\n{path}")

    def closeEvent(self, event):
        if self._autobackup_timer is not None:
            self._autobackup_timer.stop()
        super().closeEvent(event)

    def _install_central(self, conn):
        self.conn = conn
        self._registers = {}
        splitter = QSplitter(Qt.Horizontal)
        self.accounts = AccountBar(conn)
        self.accounts.accountActivated.connect(self.open_register)
        splitter.addWidget(self.accounts)

        self.stack = QStackedWidget()
        # Page 0 is the Financial Calendar, not a "select an account" label:
        # it is what the window opens on, what Tools > Financial Calendar comes
        # back to, and where the stack lands when a register goes away. A month
        # of what is coming is a far better empty state than an instruction.
        from mammon.ui.projection_dialogs import CalendarPanel
        self.calendar = CalendarPanel(conn, self)
        self.stack.addWidget(self.calendar)
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([230, 870])
        self.setCentralWidget(splitter)

    def _update_title(self):
        import os
        name = os.path.basename(self.db_path) if self.db_path else "(unsaved)"
        self.setWindowTitle(f"Mammon — {name}")

    # ---- database / import ------------------------------------------------
    def _new_database_dialog(self):
        """Create (or open) a Mammon database file and switch the window to it.

        Uses a *save* dialog so the user can type a brand-new filename -- the
        distinction from ``Open Database…`` (which requires an existing file).
        ``open_database()`` runs ``db.init_db()``, which builds a fresh schema
        for a new file or opens an existing one non-destructively, so "New"
        never deletes data; it just names and opens an empty ledger to start in.
        """
        import os
        path, _ = QFileDialog.getSaveFileName(
            self, "New Mammon database", "",
            "SQLite database (*.db);;All files (*)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".db"
        self.open_database(path)

    def _open_database_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Mammon database", "",
            "SQLite database (*.db);;All files (*)")
        if path:
            self.open_database(path)

    def open_database(self, path, key=None):
        """Point the whole window at a different Mammon database file.

        Prompts for a password when the file turns out to be encrypted and no
        ``key`` was supplied. A plaintext file never prompts, so the default path
        through here is exactly what it always was."""
        from mammon import encryption
        if key is None and encryption.is_encrypted(path):
            key = self._ask_db_password(path)
            if key is None:
                return                      # cancelled: leave the window as it is
        old = self.conn
        # A Find dialog bound to the old connection must not outlive it.
        if self._find_dialog is not None:
            self._find_dialog.close()
            self._find_dialog = None
        # NOT bootstrapped here, deliberately. Opening a database from this menu
        # is how a NEW ledger comes into being, and a new ledger must start
        # untrained: seeding it replays the register's memo->payee pairs as if
        # they were accepted renames, and for hand-entered or QIF-imported
        # history the memo is a note the USER typed, not bank text. A 1998 memo
        # of "deposit" on a Foothill Place row is not evidence that MOBILE
        # DEPOSIT means Foothill Place, but that is exactly what it taught.
        self._install_central(db.init_db(path, key))
        self.db_path = path
        self.db_key = key
        self._update_title()
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    def _ask_db_password(self, path):
        """Seam: ask the user for ``path``'s password. Overridden by tests, which
        must never open a modal (CLAUDE.md: a dialog exec_()-ed under the offscreen
        platform blocks forever)."""
        from mammon.ui.password_dialog import ask_password
        return ask_password(self, path)

    def _database_password_dialog(self):
        """File > Database Password: set, change, or remove it.

        The conversion writes a NEW file and verifies it before anything is
        swapped, so a failure at any point leaves the working ledger untouched --
        which matters more here than anywhere else in the app, because the failure
        mode is a database nobody can open."""
        import os
        from mammon import backup, encryption
        from mammon.ui.password_dialog import SetPasswordDialog
        if not self.db_path:
            QMessageBox.warning(self, "Database Password",
                                "This database has no file on disk.")
            return
        if not encryption.available():
            QMessageBox.information(
                self, "Database Password",
                "Encryption needs the sqlcipher3 driver, which is not installed."
                + chr(10) + chr(10) + "pip install mammon[encryption]")
            return
        encrypted = encryption.is_encrypted(self.db_path)
        dlg = SetPasswordDialog(self, encrypted=encrypted)
        if dlg.exec_() != QDialog.Accepted:
            return
        current, new = dlg.values()
        if not encrypted and not new:
            return                              # nothing asked for
        try:
            backup.create_backup(self.conn, self.db_path, tag=backup.MANUAL_TAG,
                                 key=self.db_key)
        except Exception:                       # pragma: no cover - best effort
            pass
        try:
            if encrypted and not new:
                out = encryption.decrypt_database(self.db_path, current)
                new_key = None
            elif encrypted:
                out = encryption.change_password(self.db_path, current, new)
                new_key = new
            else:
                out = encryption.encrypt_database(self.db_path, new)
                new_key = new
        except Exception as exc:
            QMessageBox.critical(self, "Database Password",
                                 "Nothing was changed." + chr(10) + chr(10) + str(exc))
            return
        # Swap the verified new file in, keeping the old one beside it until the
        # replacement has actually opened.
        target = self.db_path
        previous = str(target) + ".previous"
        try:
            self.conn.close()
        except Exception:                       # pragma: no cover - defensive
            pass
        self.conn = None
        try:
            os.replace(target, previous)
            os.replace(str(out), target)
        except Exception as exc:
            self.open_database(target, self.db_key)
            QMessageBox.critical(self, "Database Password",
                                 "Could not replace the database:" + chr(10) + str(exc))
            return
        self.open_database(target, new_key)
        try:
            os.remove(previous)
        except Exception:                       # pragma: no cover - best effort
            pass
        QMessageBox.information(
            self, "Database Password",
            "Encryption removed." if new_key is None else
            ("Password changed." if encrypted else
             "The database is now encrypted." + chr(10) + chr(10)
             + "There is no recovery if you forget this password."))
        if new_key is not None and not encrypted:
            self._offer_to_clear_plaintext_backups()

    def _offer_to_clear_plaintext_backups(self) -> None:
        """After encrypting for the first time, deal with the snapshots already on
        disk -- they are full PLAINTEXT copies of everything just protected.

        This is not a tidy-up. Backups are the copies most likely to be synced off
        the machine, which is the specific leak encryption is adopted to close;
        leaving them means the ledger is still readable by anyone who reaches that
        folder, and the password accomplished nothing against that threat.

        It is offered rather than done, and it defaults to KEEPING them, because
        for the next few minutes those snapshots are the only way back in if the
        new password was mistyped or is misremembered. Destroying the last
        recoverable copy at the exact moment the user is least sure of the
        password would trade a privacy problem for a data-loss one."""
        from mammon import backup
        try:
            stale = backup.plaintext_snapshots(self.db_path)
        except Exception:                       # pragma: no cover - defensive
            return
        if not stale:
            return
        if QMessageBox.question(
                self, "Unencrypted backups",
                f"{len(stale)} earlier snapshot(s) of this database are still "
                "unencrypted, including the one just taken." + chr(10) + chr(10)
                + "They are complete, readable copies of everything you just "
                "encrypted." + chr(10) + chr(10)
                + "Delete them now? Keep them if you are not yet certain of the "
                "new password -- they are the only way back in if it is wrong.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        removed, failed = 0, []
        for path in stale:
            try:
                path.unlink()
                removed += 1
            except Exception as exc:            # pragma: no cover - best effort
                failed.append(f"{path.name}: {exc}")
        msg = f"Deleted {removed} unencrypted snapshot(s)."
        if failed:
            msg += chr(10) + chr(10) + "Could not delete:" + chr(10) + chr(10).join(failed)
        QMessageBox.information(self, "Unencrypted backups", msg)

    def _import_qif_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Quicken file", "",
            "Quicken files (*.qif *.QIF);;All files (*)")
        if not path:
            return
        from mammon import importers
        from mammon.importers.qif import QifExtras, parse_qif
        # Parse once to decide the route. A QIF that carries BOTH accounts of a
        # transfer (multi-account, or one bearing investments/securities) imports
        # in bulk -- the two legs collapse into a single linked pair. A SINGLE
        # cash account's file is treated exactly like a live download: it goes
        # through the review list, where its one-sided transfer legs create the
        # counterparty mirror and matches to existing register rows prevent
        # double-entry.
        try:
            with open(path, "rb") as fh:
                text = importers._decode(fh.read())
            extras = QifExtras()
            records = parse_qif(text, collector=extras)
        except Exception as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return

        names = importers.distinct_account_names(records)
        has_investments = any(getattr(r, "is_investment", False) for r in records)
        # DIRECT (bulk) import is reserved for a MULTI-account QIF (both transfer
        # legs collapse into one linked pair) and for a QIF carrying a securities /
        # price master that only the bulk importer can apply. A SINGLE-account QIF
        # -- cash OR investment -- goes through the review queue like a download.
        direct = (len(names) != 1) or bool(extras.securities or extras.prices)

        if not direct:
            account_name = next(iter(names))
            if has_investments:
                # The investment register has no interactive review panel yet, so
                # route the file through the review PIPELINE in finalize mode
                # (classify + dedup + accept into investment_transactions) -- OUT
                # of a straight-to-register bulk write, still dedup-protected.
                try:
                    summary = importers.import_single_account(
                        self.conn, records=records, account=account_name,
                        account_type="investment", finalize=True)
                except Exception as exc:
                    QMessageBox.warning(self, "Import failed", str(exc))
                    return
                self._refresh_all()
                QMessageBox.information(
                    self, "Investment import complete",
                    f"{summary['added']} investment row(s) added, "
                    f"{summary['matched']} already present (skipped), for "
                    f"{account_name}. Re-imported rows dedupe instead of doubling.")
                return
            try:
                entries = importers.import_single_account(
                    self.conn, records=records, account=account_name, finalize=False)
            except Exception as exc:
                QMessageBox.warning(self, "Import failed", str(exc))
                return
            account_id = None
            for acct in ledger.list_accounts(self.conn, include_closed=True):
                if str(acct["name"]).strip().lower() == account_name.strip().lower():
                    account_id = int(acct["id"])
                    break
            batch_id = import_review.start_batch(
                self.conn, account_id, source="download", file_count=1,
                note=account_name)
            import_review.persist_entries(
                self.conn, account_id, entries, batch_id=batch_id)
            import_review.purge_old_batches(self.conn, account_id)
            entries = self._load_review_entries(account_id)
            self._refresh_all()
            self.open_register(account_id)
            reg = self._registers.get(account_id)
            if reg is not None and hasattr(reg, "show_review"):
                reg.show_review(entries)
            entries = [e for e in entries if not e.is_actioned]
            new = sum(1 for e in entries if e.is_new)
            QMessageBox.information(
                self, "Import ready for review",
                f"{len(entries)} row(s) from {account_name} are ready below: "
                f"{new} new, {len(entries) - new} matching. Nothing was added to "
                f"the register yet -- accept each row to post it. Single-account "
                f"transfers create the counterparty leg; matches to existing rows "
                f"prevent double-entry.")
            return

        # Multi-account (or securities-master) QIF: bulk import; both transfer
        # legs collapse into one linked pair (no fabricated mirror).
        try:
            res = importers.import_file(self.conn, path)
        except Exception as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self._refresh_all()
        QMessageBox.information(
            self, "Import complete",
            f"Added {res.added} transactions "
            f"({res.transfers} transfers, {res.investments} investments).\n"
            f"{res.duplicates} duplicates skipped, {res.errors} errors.")

    # ---- registers --------------------------------------------------------
    def _resolve_open_edit(self, register) -> bool:
        """Ask what to do with an in-progress edit. False means "stay put".

        Save / Discard / Cancel rather than an automatic save: clicking another
        account is a navigation gesture, not a decision to commit whatever is
        half-typed in a cell, and a wrong value written into the ledger is far
        worse than one extra click."""
        name = ""
        try:
            acct = ledger.get_account(self.conn, register.account_id)
            name = (acct["name"] if acct else "") or ""
        except Exception:                  # pragma: no cover - defensive
            pass
        resp = QMessageBox.question(
            self, "Finish editing?",
            f"You are still editing a transaction in {name or 'this account'}."
            f"\n\nSave the change, discard it, or stay here?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if resp == QMessageBox.Cancel:
            return False
        if resp == QMessageBox.Save:
            register.commit_open_editor()
        else:
            register.discard_open_editor()
        return True

    def open_register(self, account_id):
        if account_id not in self._registers:
            # Investment and crypto accounts each get their own register: their
            # activity lives in a separate table (investment_transactions /
            # crypto_transactions) with security/coin, quantity and price fields
            # the cash RegisterWidget never shows. Both are grouped as
            # INVESTMENT_LIKE_TYPES for net worth and the sidebar, but they read
            # DIFFERENT tables, so the register class is chosen by exact type here
            # rather than by that membership. Every other account type gets the
            # cash register.
            acct = ledger.get_account(self.conn, account_id)
            if acct is not None and (acct["type"] or "") == "investment":
                widget = InvestmentRegisterWidget(self.conn, account_id)
            elif acct is not None and (acct["type"] or "") == "crypto":
                widget = CryptoRegisterWidget(self.conn, account_id)
            else:
                widget = RegisterWidget(self.conn, account_id)
            # Exclude the committing register from the cross-register reload: it
            # already reloaded itself (and captured its selected row) before it
            # emitted `changed`, and a second reload here would clear its saved
            # position, snapping the view to the top after Enter. Its own
            # committed -> _restore_position (connected AFTER `changed`) then
            # re-selects the edited row.
            widget.changed.connect(
                lambda _aid=account_id: self._refresh_all(exclude_account_id=_aid))
            # Keep the Edit menu's Undo/Redo in step after every write, so their
            # shortcuts enable/disable without needing the menu opened first.
            widget.changed.connect(self._sync_edit_actions)
            # The register's account toolbar defers every action to the window,
            # which owns the dialogs and the cross-account refresh.
            tb = getattr(widget, "toolbar", None)
            if tb is not None:
                tb.detailsRequested.connect(self._account_details_dialog)
                tb.reconcileRequested.connect(self._reconcile_account)
                tb.importRequested.connect(self._import_account)
                tb.downloadRequested.connect(self._download_account)
                tb.reviewRequested.connect(self._reopen_review)
                tb.hideRequested.connect(self._hide_account)
                tb.loanSetupRequested.connect(self._loan_setup_for_account)
                tb.enterPaymentRequested.connect(self._enter_loan_payment)
                # Surface the Loan Setup button at the top of loan registers.
                is_liability = acct is not None and acct["type"] == "liability"
                is_loan = False
                if is_liability:
                    from mammon import loans
                    is_loan = loans.get_loan_params(self.conn, account_id) is not None
                tb.configure_loan(is_liability, is_loan)
                self._refresh_download_gate(account_id, tb)
            # Seed a newly-opened register with ALL persisted display preferences
            # (font, row shading, colors, one/two-line view), not just the view
            # mode -- so saved settings re-apply on reopen. The account's own
            # gear-menu choice wins over the global default when it has one.
            widget.apply_display_prefs(prefs.account_view_mode(account_id))
            self._registers[account_id] = widget
            self.stack.addWidget(widget)
        # Leaving a register with an edit in progress ASKS rather than guessing.
        # Silently committing a half-typed value is the dangerous default: the
        # user clicked away, which is not the same as saying "save this". It also
        # closes the editor deliberately, before the stack tears its view away.
        current = self.stack.currentWidget()
        if (current is not None and current is not self._registers[account_id]
                and getattr(current, "has_open_editor", None) is not None
                and current.has_open_editor()):
            if not self._resolve_open_edit(current):
                return current              # cancelled -- stay where we are
        widget = self._registers[account_id]
        self.stack.setCurrentWidget(widget)
        # First open of this account this session: land on the newest activity
        # (bottom) rather than the oldest (by request). Gated by a per-widget
        # flag (and the cached widget) so a later re-open keeps wherever the user
        # last scrolled. Done AFTER setCurrentWidget so the view is realized and
        # scrollToBottom actually reaches the last row.
        if not getattr(widget, "_did_initial_scroll", False):
            widget._did_initial_scroll = True
            if hasattr(widget, "scroll_to_newest"):
                widget.scroll_to_newest()
        self.accounts.select_account(account_id)
        self._current_account = account_id
        self._sync_edit_actions()
        return widget

    def _set_register_view_mode(self, mode: str) -> None:
        """View menu: flip every open register (and future ones) between the
        one-line and two-line layouts, and remember the choice for next run."""
        self.register_view_mode = "two" if mode == "two" else "one"
        prefs.set_two_line_default(self.register_view_mode == "two")
        self._one_line_act.setChecked(self.register_view_mode == "one")
        self._two_line_act.setChecked(self.register_view_mode == "two")
        # This sets the DEFAULT. An account that picked its own layout from its
        # gear menu keeps it -- otherwise the global switch would silently undo a
        # more specific choice the user already made.
        for aid, reg in self._registers.items():
            if not prefs.has_account_view_mode(aid):
                reg.set_view_mode(self.register_view_mode)

    # ---- display preferences ---------------------------------------------
    def _display_preferences_dialog(self):
        """Settings > Display Preferences: pick the register font/colors/shading
        and default view, persist them, and apply live to every open register."""
        dlg = DisplayPreferencesDialog(parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        self.apply_display_prefs(dlg.values())

    def apply_display_prefs(self, values):
        """PERSIST a Display-Preferences dict and apply it live: re-theme the app
        (font, colors, shading), keep the View menu's one/two-line choice in
        sync, rebuild the account bar's colored balances, and re-style every open
        register. Persisting first keeps the QSettings the per-register apply
        reads consistent with what was just chosen."""
        prefs.set_display_prefs(values)
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            # Re-read from prefs (not the raw `values`) so apply_theme adopts the
            # now-active theme's OWN persisted colors: the colors were just
            # written to the chosen theme's per-theme slot, and display_prefs()
            # reads that slot back. This keeps the live repaint consistent with
            # the per-register apply below, which also reads back from prefs.
            style.apply_theme(app, prefs.display_prefs())
        # the dialog can also change the default one/two-line view
        self.register_view_mode = "two" if prefs.two_line_default() else "one"
        self._one_line_act.setChecked(self.register_view_mode == "one")
        self._two_line_act.setChecked(self.register_view_mode == "two")
        # account-bar balances read style.negative_color() when (re)built
        self.accounts.refresh()
        for aid, reg in self._registers.items():
            # An account that chose its own layout keeps it; the rest follow the
            # default that was just set.
            reg.apply_display_prefs(
                prefs.account_view_mode(aid) if prefs.has_account_view_mode(aid)
                else self.register_view_mode)
        # The home Financial Calendar renders its day highlights, event colours
        # and spending bar chart from the ACTIVE theme at DRAW time, so a theme
        # switch must invalidate it: mark_stale repaints it now if it is the page
        # on screen, otherwise on its next show. Without this the spending chart,
        # built under the old theme, stayed dark after a dark->light toggle.
        cal = getattr(self, "calendar", None)
        if cal is not None:
            cal.mark_stale()
        # A date format is only "used throughout the application" if changing it
        # reaches windows already open. Displayed dates re-render through
        # fmt_date on the next repaint; date EDITORS hold their format, so every
        # one under this window is re-stamped here.
        refresh_date_format(self)

    # ---- find -------------------------------------------------------------
    def _find_transactions_dialog(self):
        # Modeless: reuse the open dialog if there is one (raise it), else create
        # it and show() (not exec_) so the register stays usable alongside it.
        dlg = getattr(self, "_find_dialog", None)
        if dlg is None:
            dlg = SearchDialog(
                self.conn,
                default_account_id=getattr(self, "_current_account", None),
                parent=self)
            dlg.activated.connect(self._open_search_result)
            dlg.changed.connect(self._reload_registers)
            dlg.finished.connect(self._on_find_closed)
            self._find_dialog = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        dlg.query.setFocus()

    def _on_find_closed(self, _result):
        self._find_dialog = None

    def _reload_registers(self) -> None:
        """A write made outside the registers (Find's Replace) reloads every
        open register so what they show matches the ledger."""
        for reg in list(self._registers.values()):
            model = getattr(reg, "model", None)
            if model is not None and hasattr(model, "reload"):
                model.reload()

    def _open_search_result(self, account_id, txn_id):
        """Open the result's account register (behind the Find dialog) and
        select the transaction, so a global find lands on the exact row."""
        reg = self.open_register(account_id)
        reg.select_txn(txn_id)

    # ---- settings / reports ----------------------------------------------
    def _edit_account_details_dialog(self):
        """Settings menu entry: edit the currently-selected account's details."""
        aid = getattr(self, "_current_account", None)
        if aid is None:
            QMessageBox.information(self, "Account Details",
                                    "Select an account on the left first.")
            return
        self._account_details_dialog(aid)

    def _account_details_dialog(self, account_id):
        """Open the account-details dialog for ``account_id`` (the toolbar's
        Account Details… action and the Settings menu both land here)."""
        acct = ledger.get_account(self.conn, account_id)
        if acct is None:
            return
        dlg = AccountDetailsDialog(acct, parent=self, client=self.webslinger,
                                   conn=self.conn)
        if dlg.exec_() != QDialog.Accepted:
            return
        v = dlg.values()
        lot_method = v.pop("lot_method", None)
        # A lien only means something on a securable account; a type change in
        # the same edit would otherwise leave a stale one pointing nowhere.
        from mammon import asset_values as _av
        if v["type"] not in _av.SECURABLE_TYPES:
            v["secured_by_account_id"] = None
        if v["type"] != "asset":
            v["property_address"] = None
        if not v["name"]:
            QMessageBox.warning(self, "Account Details", "An account needs a name.")
            return
        try:
            ledger.update_account(self.conn, account_id, **v)
        except Exception as exc:  # e.g. duplicate name (UNIQUE)
            QMessageBox.warning(self, "Account Details", str(exc))
            return
        # A changed cost-basis method replays the account: every open
        # position's basis, every realized gain and every snapshot follow.
        if (v["type"] == "investment" and lot_method
                and lot_method != investments.get_lot_method(self.conn, account_id)):
            investments.set_lot_method(self.conn, account_id, lot_method)
        # Hiding via the details dialog must also drop the open register.
        if v["hidden"]:
            self._drop_register(account_id)
        self._refresh_all()
        # A newly-set (or cleared) download script changes the Download gate.
        self._refresh_gate_if_open(account_id)

    def _refresh_gate_if_open(self, account_id):
        """Re-evaluate the Download gate for an account whose register is open."""
        reg = self._registers.get(account_id)
        tb = getattr(reg, "toolbar", None) if reg is not None else None
        if tb is not None:
            self._refresh_download_gate(account_id, tb)

    def _reconcile_account(self, account_id):
        """Toolbar Reconcile… -> the existing reconcile flow for this account."""
        self._current_account = account_id
        self._reconcile_dialog()

    def _import_account(self, account_id):
        """Toolbar Import… -> a per-account import from a downloaded FILE.

        A file import (CSV/OFX/QFX/JSON/QIF/TSV) now lands in the IMPORT REVIEW queue
        -- exactly like a scraped/downloaded batch -- rather than writing straight
        to the register (user: "no real difference between direct-download and
        file-download data"). The only exception, handled inside
        :meth:`_ingest_file_via_review`, is a multi-account QIF."""
        acct = ledger.get_account(self.conn, account_id)
        if acct is None:
            return
        paths = self._choose_import_files(acct)
        if not paths:
            return                      # cancelled; nothing to warn about
        self._ingest_files_as_batch(account_id, acct, paths, source_label="import")

    def _choose_import_files(self, acct):
        """Ask only for the FILE(S). There is deliberately no institution picker:
        format is a property of the file (auto-detected from its contents, with
        the column map inferred and correctable for delimited sources), not of the
        account it lands in. The picker that used to sit here also discarded the
        chosen institution outright -- the caller read only the path -- so it
        asked a question and threw the answer away.

        MULTI-select, because some institutions will not export an arbitrary date
        range: the history arrives piecemeal, typically a file per month, and
        those files are one import of one period -- they belong in one batch and
        one review.

        Split out as the seam headless tests override."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, f"Import into {_row_get(acct, 'name') or 'account'}", "",
            "Statements (*.ofx *.qfx *.qif *.csv *.tsv *.tab *.txt *.json);;"
            "All files (*)")
        return [p.strip() for p in (paths or []) if p and p.strip()]

    def _ingest_files_as_batch(self, account_id, acct, paths, *,
                               source_label="import"):
        """Import several files as ONE review batch, then report once.

        A batch is the unit the review panel shows and retention counts, so a
        multi-file import must not fragment into one batch per file: the files
        are one period of history and are reviewed together."""
        from pathlib import Path as _Path
        batch_id = import_review.start_batch(
            self.conn, account_id, source=source_label, file_count=len(paths),
            note=", ".join(_Path(p).name for p in paths)[:500])
        totals = {"parsed": 0, "inserted": 0, "prior": {}, "failed": [],
                  "direct_added": 0, "direct_matched": 0, "direct_kinds": set()}
        for path in paths:
            res = self._ingest_file_via_review(
                account_id, acct, path, source_label=source_label,
                batch_id=batch_id, report=False)
            if res is None:
                totals["failed"].append(_Path(path).name)
                continue
            if res.get("bulk"):
                # Imported straight through rather than into the review queue (a
                # multi-account QIF, or an investment file). Still counted -- the
                # rows are in the ledger and the user must be told so.
                totals["direct_added"] += res.get("direct_added", 0)
                totals["direct_matched"] += res.get("direct_matched", 0)
                totals["direct_kinds"].add(res.get("kind", "bulk"))
                continue
            totals["parsed"] += res.get("parsed", 0)
            totals["inserted"] += res.get("inserted", 0)
            for state, n in (res.get("prior") or {}).items():
                totals["prior"][state] = totals["prior"].get(state, 0) + n
        # Retention is counted in batches, so trim once the new one is complete.
        import_review.purge_old_batches(self.conn, account_id)
        self._show_review_after_import(account_id, acct, paths, totals)

    def _show_review_after_import(self, account_id, acct, paths, totals):
        """Open the panel on the new batch and report the whole import once."""
        self._refresh_all()
        self.open_register(account_id)
        reg = self._registers.get(account_id)
        entries = self._load_review_entries(account_id)
        new = sum(1 for e in entries if e.is_new and not e.is_actioned)
        if reg is not None and hasattr(reg, "show_review"):
            reg.show_review(entries)
        label = (paths[0] if len(paths) == 1
                 else f"{len(paths)} files")
        direct = totals.get("direct_added", 0) + totals.get("direct_matched", 0)
        if direct and not totals["parsed"]:
            # Nothing went through the review queue because this file imports
            # straight through. Report THAT, rather than the review queue's zero.
            kind = "investment " if "investment" in totals.get("direct_kinds", ()) else ""
            title = "Import complete"
            msg = (f"{totals['direct_added']} {kind}row(s) added, "
                   f"{totals['direct_matched']} already present (skipped), "
                   f"for {_row_get(acct, 'name')}.")
        else:
            title, msg = self._import_report(
                acct, label, totals["parsed"], totals["inserted"], totals["prior"],
                [e for e in entries if not e.is_actioned], new)
            if direct:
                extra = ("Also imported directly: "
                         f"{totals['direct_added']} row(s) added, "
                         f"{totals['direct_matched']} already present.")
                msg = msg + chr(10) + chr(10) + extra
        if totals["failed"]:
            msg += "\n\nCould not read: " + ", ".join(totals["failed"])
        QMessageBox.information(self, title, msg)

    def _ingest_file_via_review(self, account_id, acct, path, *,
                                source_label="import", batch_id=None, report=True):
        """Route ONE account's import FILE through the import review queue.

        Returns a dict of counts (``parsed``/``inserted``/``prior``, or
        ``{"bulk": True}`` for a multi-account file that reported itself), or
        ``None`` if the file could not be read. ``report=False`` suppresses the
        per-file dialog so a multi-file batch can report ONCE at the end;
        ``batch_id`` stamps every row with the batch it arrived in.

        The single choke point shared by the toolbar Import… button and the
        webSlinger EXPORT (site dropped a file) download path, so a new import
        source is reviewed BY DEFAULT. Routing:

          * MULTI-account file (a multi-account QIF, or a QIF with a securities /
            price master) -- the ONE direct path -- imports in bulk (both transfer
            legs collapse into a linked pair; the securities master prices
            holdings), since it fans out across accounts and has no per-row review
            surface.
          * SINGLE-account CASH file -> the two-step review the download flow uses
            (classify NEW/MATCHING, persist, show the register's review panel);
            nothing enters the register until the user accepts a row.
          * SINGLE-account INVESTMENT file -> the review PIPELINE in finalize mode
            (classify + dedup + accept) posting into ``investment_transactions``,
            because the investment register has no interactive review panel yet.
            This keeps it OUT of a straight-to-register bulk write. Field mapping
            is alias-based and share/price/total are cross-derived (see
            importers.csvimp).
        """
        from mammon import importers
        if importers.multi_account_file(path):
            try:
                # Pass the SELECTED account as the fallback. A broker's QIF
                # routinely carries a !Type:Security master, which routes it here
                # -- but such a file often names no account of its own, and
                # without a fallback every row parsed to an account of None and
                # the import silently added nothing while reporting success. A
                # genuinely multi-account file still uses its own !Account names;
                # this is only the fallback for a file that has none.
                res = importers.import_file(
                    self.conn, path,
                    account=_row_get(acct, "name"),
                    account_type=_row_get(acct, "type"))
            except Exception as exc:
                QMessageBox.warning(self, "Import failed", str(exc))
                return None
            self._refresh_all()
            if report:
                self._report_import("Import complete", res)
            return {"bulk": True, "direct_added": res.added,
                    "direct_matched": res.duplicates, "kind": "bulk"}
        # Investment files go through the SAME two-step review as cash. They used
        # to import straight through, on the reasoning that the investment
        # register had no review surface -- but that meant an unrecognised
        # security name was never questioned. Brokers rename funds and append
        # tickers, so a row routinely names a fund the ledger does not hold;
        # accepted unchallenged it created a phantom security, and the per-share
        # price derived from that row was recorded against the phantom, leaving
        # the real holding with no price history. The review list is where the
        # security is corrected, before any of that is written.
        try:
            entries = importers.import_single_account(
                self.conn, account=acct["name"], account_type=acct["type"],
                path=path, finalize=False)
        except Exception as exc:
            if report:
                QMessageBox.warning(self, "Import failed", str(exc))
            return None
        parsed = len(entries)
        inserted = import_review.persist_entries(
            self.conn, account_id, entries, batch_id=batch_id)
        prior = import_review.entry_states(self.conn, entries) if parsed else {}
        result = {"parsed": parsed, "inserted": inserted, "prior": prior}
        if not report:
            return result               # caller aggregates and reports once
        entries = self._load_review_entries(account_id)
        self._refresh_all()
        self.open_register(account_id)
        reg = self._registers.get(account_id)
        actionable = [e for e in entries if not e.is_actioned]
        new = sum(1 for e in actionable if e.is_new)
        if reg is not None and hasattr(reg, "show_review"):
            reg.show_review(entries)
        QMessageBox.information(
            self, *self._import_report(acct, path, parsed, inserted, prior,
                                       actionable, new))
        # New/changed tabular (CSV) format: offer to remember its column map as a
        # profile so future imports of this format need no interpretation prompt.
        self._offer_import_profile(acct, path)
        return result

    # ---- review visibility ------------------------------------------------
    def _load_review_entries(self, account_id):
        """The review rows to show, honouring this account's saved visibility."""
        return import_review.load_review(
            self.conn, account_id, prefs.review_visibility(account_id))

    @staticmethod
    def _import_report(acct, path, parsed, inserted, prior, pending, new):
        """(title, message) describing what an import actually DID.

        The old wording reported ``len(load_pending(...))`` while calling it
        "row(s) from the file". Re-importing an already-accepted file therefore
        announced "0 row(s) from the file", which reads as "the file was empty or
        unreadable" -- when what really happened is that every row was recognised
        and correctly refused re-entry. The file count and the queue count are
        different numbers and must be reported as such:

          * ``parsed``   -- rows the parser actually read out of the file
          * ``inserted`` -- rows NEWLY queued for review just now
          * ``prior``    -- stored state of rows that were already known
          * ``pending``  -- what is now waiting in the review panel
        """
        from pathlib import Path as _Path
        name = acct["name"]
        fname = _Path(path).name
        if parsed == 0:
            return ("Nothing to import",
                    f"No transactions could be read from {fname}. The file may be "
                    f"empty, or its layout may not be recognised — if it is a "
                    f"delimited file, use Adjust mapping to point out the date and "
                    f"amount columns.")
        if inserted == 0:
            done = ", ".join(f"{n} already {state}"
                             for state, n in sorted(prior.items()))
            waiting = (f" {len(pending)} row(s) from earlier imports are still "
                       f"waiting for review." if pending else "")
            return ("Already imported",
                    f"All {parsed} transaction(s) in {fname} were already imported "
                    f"into {name} ({done or 'previously handled'}), so nothing was "
                    f"queued again and nothing was duplicated.{waiting}")
        head = (f"{inserted} of {parsed} transaction(s) from {fname} were added to "
                f"{name}'s review queue")
        already = parsed - inserted
        if already:
            head += f"; the other {already} were already imported"
        return ("Import ready for review",
                f"{head}.\n\n{len(pending)} row(s) now await review: {new} new, "
                f"{len(pending) - new} matching. Nothing has been added to the "
                f"register yet — accept each row to post it.")

    # Delimited sources whose column map the tabular engine has to work out.
    # Not just ".csv": a bank happily ships the same comma/tab grid as .txt or
    # .tsv, and those need the same interpretation.
    _TABULAR_SUFFIXES = (".csv", ".tsv", ".txt", ".tab")
    _MAX_MAPPING_ROUNDS = 10      # refine loop must always terminate

    def _offer_import_profile(self, acct, path):
        """If ``path`` is a delimited download whose header signature has no saved
        profile yet, show how the columns were interpreted and let the user save
        it -- or open the mapping wizard to correct it first. A format that is
        already known returns silently. Never raises into the import flow.

        The prompt names the SOURCE COLUMN behind every mapped role. Showing only
        the parsed values hides the failure that matters: a wrong column still
        produces plausible output (a running-balance column parses as money just
        like an amount column), so the values alone cannot tell the user whether
        the mapping is right.

        The choice goes through ``QMessageBox.question`` deliberately -- it is the
        seam headless tests patch. Building a QMessageBox and calling ``exec_()``
        here instead deadlocks under the offscreen platform, where no one can
        click. The refine loop is bounded for the same reason: nothing that can
        run without a user should be able to spin forever."""
        try:
            from pathlib import Path as _Path
            from mammon.importers import _decode, tabular
            if _Path(path).suffix.lower() not in self._TABULAR_SUFFIXES:
                return
            # The prompt is about CASH roles -- which column is the date, which is
            # the amount. An investment file is mapped by its own profile
            # (importers.csvimp) onto security/action/shares/price, so asking the
            # user to confirm a cash column map for it is a question about the
            # wrong file. It also blocked: routing investment imports through the
            # cash review path brought them here for the first time, and the
            # modal has nobody to answer it during an unattended import.
            if str(_row_get(acct, "type") or "").strip().lower() == "investment":
                return
            text = _decode(_Path(path).read_bytes())
            plan = tabular.plan_tabular(
                self.conn, text, default_account=acct["name"],
                account_type=acct["type"])
            if plan is None or not plan.is_new:
                return

            for _ in range(self._MAX_MAPPING_ROUNDS):
                resp = QMessageBox.question(
                    self, "New import format", self._plan_prompt(acct, plan),
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                    QMessageBox.Yes)
                if resp == QMessageBox.Cancel:
                    return                      # import as-is, remember nothing
                if resp == QMessageBox.Yes:
                    tabular.accept_profile(
                        self.conn, plan, name=f"{acct['name']} import")
                    return
                revised = self._run_mapping_wizard(plan)
                if revised is None:
                    return                      # wizard cancelled
                plan = revised
        except Exception:
            return

    def _plan_prompt(self, acct, plan) -> str:
        """The interpretation, stated as role <- SOURCE COLUMN plus the rows that
        combination produces, so a mix-up is visible rather than merely plausible."""
        r = plan.roles
        if r.debit or r.credit:
            amt_src = " / ".join(x for x in (r.debit, r.credit) if x) or "(none)"
        else:
            amt_src = r.amount or "(none)"
        if r.payee_from or r.payee_to:
            payee_src = " / ".join(x for x in (r.payee_from, r.payee_to) if x)
        else:
            payee_src = r.payee or "(from description)"
        mapping = "\n".join([
            f"    Date    <-  {r.date or '(none)'}",
            f"    Amount  <-  {amt_src}",
            f"    Payee   <-  {payee_src}",
            f"    Memo    <-  {' + '.join(r.description) if r.description else '(none)'}",
        ])
        rows = "\n".join(
            f"    {p['date']}  {(p['amount_cents'] or 0) / 100:>10,.2f}  {p['payee']}"
            for p in plan.preview[:5]) or "    (no rows parsed)"
        return (
            f"This file's layout looks new for {acct['name']}.\n\n"
            f"Columns were mapped as:\n{mapping}\n\n"
            f"which produces:\n{rows}\n\n"
            f"{len(plan.records)} transaction(s) would import.\n\n"
            f"Yes - save this mapping (future files in this format import with no "
            f"prompt)\nNo - adjust the columns first\nCancel - import "
            f"this file without saving a mapping")

    def _run_mapping_wizard(self, plan):
        """Open the column-mapping wizard; return the revised plan, or None if the
        user cancelled. Split out as the seam headless tests override."""
        from mammon.ui.import_mapping import ImportMappingDialog
        dlg = ImportMappingDialog(plan, self)
        return dlg.plan if dlg.exec_() == QDialog.Accepted else None

    # ---- automated download (webSlinger) ---------------------------------
    def _download_preflight(self, acct):
        """Structured go/no-go for this account's Download action (see
        webslinger.preflight_download): names the first missing prerequisite --
        webSlinger reachable, a script configured, and its inputData filled in.
        Credentials are NOT a gate: Mammon stores none and never handles login."""
        return webslinger_mod.preflight_download(
            client=self.webslinger,
            script_name=_row_get(acct, "download_script"),
            input_data=_parse_download_config(_row_get(acct, "download_config")))

    def _refresh_download_gate(self, account_id, tb):
        acct = ledger.get_account(self.conn, account_id)
        if acct is None:
            return
        # Download is always clickable; only its tooltip reflects readiness.
        tb.set_download_hint(self._download_preflight(acct).message)

    def _download_account(self, account_id):
        """Toolbar Download… -> run this account's stored webSlinger script with
        its SAVED inputData and import the result INTO this account.

        The button is ALWAYS enabled, so this handler always runs. It first runs
        a preflight (webSlinger available -> a script configured -> its inputData
        filled in) and, if anything is missing, shows a modal naming EXACTLY
        which one and the concrete step to fix it -- Download must never be
        silently unresponsive. Mammon handles NO credentials here: webSlinger
        logs in with the credentials in the user's own browser.

        The run either returns data (imported directly) or drives the site's own
        Export button, dropping a file in ~/Downloads that Mammon then imports --
        :func:`downloads.download_account` handles both and reports the counts."""
        acct = ledger.get_account(self.conn, account_id)
        if acct is None:
            return
        pf = self._download_preflight(acct)
        if not pf.ok:
            QMessageBox.information(
                self, "Download setup incomplete — " + pf.title, pf.message)
            return
        script = (_row_get(acct, "download_script") or "").strip()
        config = _parse_download_config(_row_get(acct, "download_config"))
        input_data = config
        # If the script takes a date range, prompt for it (default start = day
        # after the last download's end, end = today) with a Download/Cancel
        # choice, and remember the chosen dates for next time.
        schema = None
        from PyQt5.QtWidgets import QApplication
        # describe_script is an MCP round trip (subprocess + handshake). Show a
        # busy cursor for it so the click is never silently unresponsive; it is a
        # cache hit (instant) whenever the account-details dialog was opened first.
        # Restore before any modal date dialog so it doesn't run under a wait
        # cursor.
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            schema = self.webslinger.describe_script(script)
        except Exception:
            schema = None
        finally:
            QApplication.restoreOverrideCursor()
        if schema is not None and any(i.is_date for i in schema.inputs):
            dlg = DownloadDateDialog(schema, config, self)
            if dlg.exec_() != QDialog.Accepted:
                return
            start, end = dlg.dates()
            input_data = downloads.build_download_input_data(
                schema, config, start, end)
            new_cfg = downloads.remember_download_dates(config, schema, start, end)
            try:
                ledger.update_account(self.conn, account_id,
                                      download_config=json.dumps(new_cfg))
            except Exception:
                pass
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = downloads.download_rows_for_review(
                self.conn, self.webslinger, script, input_data,
                account=acct["name"], account_type=_row_get(acct, "type"),
                account_number=_row_get(acct, "account_number"))
        except Exception as exc:
            QMessageBox.warning(self, "Download failed", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        # SCRAPE path: rows go to the review list first -- nothing enters the
        # register until the user accepts (MATCHING) or saves (NEW) each row.
        if result.needs_review:
            entries = import_review.build_review(
                self.conn, account_id, result.rows)
            # Persist the review so it survives a restart / account switch. A
            # re-download of an already-ACCEPTED row does not resurrect it: it
            # stays accepted, and whether it is VISIBLE is the toggle's business,
            # not this path's. (A DISCARDED row is deleted outright, so a
            # re-download legitimately brings it back -- that is the point.)
            batch_id = import_review.start_batch(
                self.conn, account_id, source="download", file_count=1)
            import_review.persist_entries(
                self.conn, account_id, entries, batch_id=batch_id)
            import_review.purge_old_batches(self.conn, account_id)
            entries = self._load_review_entries(account_id)
            self.accounts.refresh()
            reg = self._registers.get(account_id)
            if reg is not None and hasattr(reg, "show_review"):
                reg.show_review(entries)
                entries = [e for e in entries if not e.is_actioned]
                new = sum(1 for e in entries if e.is_new)
                QMessageBox.information(
                    self, "Download ready for review",
                    f"{len(entries)} downloaded row(s) are ready below: "
                    f"{new} new, {len(entries) - new} matching. Nothing was "
                    f"added to the register yet -- select a row to review and "
                    f"accept it (new rows are edited in the register).")
                return
        # EXPORT files: one run can drop SEVERAL. Route EACH single-account file
        # (cash OR investment) through the SAME review queue the toolbar file
        # import uses -- nothing was imported by the download itself. This closes
        # the investment / webSlinger EXPORT bypass (a broker's exported
        # .qfx/.csv lands in review). Any multi-account QIFs were imported
        # directly and are reported below.
        for fpath in result.file_reviews:
            self._ingest_file_via_review(
                account_id, acct, fpath, source_label="download")
        if result.import_results:
            self._refresh_all()
            for dr in result.import_results:
                self._report_import(
                    "Download complete",
                    dr.import_result if dr is not None else None,
                    self._recon_line(dr) if dr is not None else "")
        elif not result.file_reviews:
            # Degenerate: nothing to review and nothing imported directly.
            self._refresh_all()
            self._report_import("Download complete", None, "")

    def _reopen_review(self, account_id):
        """Toolbar Review… -> re-show this account's pending import-review list."""
        reg = self._registers.get(account_id)
        if reg is None or not hasattr(reg, "reopen_review"):
            return
        if not reg.review_panel.has_pending():
            QMessageBox.information(
                self, "No review pending",
                "There are no downloaded rows waiting to be reviewed. "
                "Use Download… to fetch new transactions.")
            return
        reg.reopen_review()

    def _download_log_dialog(self):
        """Tools -> Download Log…: show recent download attempts (incl. the raw
        MCP error AND Mammon's final decision for each) so the last error is
        retrievable after its transient dialog is gone."""
        from mammon import download_log
        db_path = self.db_path or download_log.db_path_from_conn(self.conn)
        DownloadLogDialog(self, db_path=db_path).exec_()

    @staticmethod
    def _recon_line(result):
        if result.reconciled is True:
            return "\nReconciled to the statement balance ✓"
        if result.reconciled is False:
            return (f"\nLedger {result.ledger_balance/100:.2f} does NOT match "
                    f"statement {result.statement_balance/100:.2f}.")
        return ""

    def _report_import(self, title, imp, recon_line=""):
        if imp is None:
            QMessageBox.information(self, title, "Nothing was imported." + recon_line)
            return
        QMessageBox.information(
            self, title,
            f"Added {imp.added} transactions "
            f"({imp.transfers} transfers, {imp.investments} investments).\n"
            f"{imp.duplicates} duplicates skipped, {imp.errors} errors."
            + recon_line)

    def _accounts_list_dialog(self):
        """Open the classic accounts list (reachable from the toolbar and
        the File menu). Refreshes the window when it reports a change."""
        dlg = AccountsListDialog(self.conn, parent=self, client=self.webslinger)
        dlg.changed.connect(self._refresh_all)
        dlg.exec_()
        # A details edit may have hidden the account whose register is open.
        for aid in list(self._registers):
            acct = ledger.get_account(self.conn, aid)
            if acct is None or _row_get(acct, "hidden"):
                self._drop_register(aid)
        self._refresh_all()
        # A details edit may also have changed a download script -> re-gate.
        for aid in list(self._registers):
            self._refresh_gate_if_open(aid)

    def _budgets_dialog(self):
        """Open the Budgets panel (Tools menu): choose/create a budget, edit
        per-category monthly targets, and see budgeted/actual/remaining for a
        month. Budgets never touch transaction rows (actuals are derived
        read-only), so nothing here needs to refresh the register."""
        from mammon.ui.budget_widget import BudgetWidget
        dlg = BudgetWidget(self.conn, parent=self)
        dlg.exec_()

    def _rename_rules_dialog(self):
        """Open the payee-renaming manager (learned rename tree; Tools menu)."""
        from mammon.ui.rename_rules_dialog import RenameRulesDialog
        dlg = RenameRulesDialog(self.conn, parent=self)
        dlg.exec_()

    def _securities_dialog(self):
        """Open the securities manager (Tools menu): confirm each security's
        ticker identity and its description, and merge two spellings of one
        holding into one. A merge restates the lot replay for every account that
        held it, so this refreshes the open registers rather than only itself."""
        from mammon.ui.securities_dialog import SecuritiesDialog
        dlg = SecuritiesDialog(self.conn, parent=self)
        dlg.changed.connect(self._refresh_all)
        dlg.exec_()

    def _rules_manager_dialog(self):
        """Open the unified Rules Manager (Tools menu): category rules, transfer
        rules, and learned payee mappings, with the migration-40
        account/amount/memo conditions editable on the keyword rules. A thin
        projection over category_rules / transfer_rules / categorize; it never
        touches transaction rows, so nothing here refreshes the register."""
        from mammon.ui.rules_manager_widget import RulesManagerWidget
        dlg = RulesManagerWidget(self.conn, parent=self)
        dlg.exec_()

    def _manage_categories_dialog(self):
        """Open the Category Manager (Tools menu): add / rename / reparent /
        merge / delete over the category tree. Merge repoints transactions and
        delete reassigns an in-use category first, so a change here can alter
        register category labels -> refresh the window."""
        from mammon.ui.categories_dialog import CategoriesDialog
        dlg = CategoriesDialog(self.conn, parent=self)
        dlg.changed.connect(self._refresh_all)
        dlg.exec_()

    def _manage_tags_dialog(self):
        """Open the Tag Manager (Tools menu): rename, color, or delete tags. A
        rename or delete changes register Tag cells and the By Tag report, and a
        color change repaints their swatches -> refresh the window."""
        from mammon.ui.tags_dialog import TagsDialog
        dlg = TagsDialog(self.conn, parent=self)
        dlg.changed.connect(self._refresh_all)
        dlg.exec_()

    def _scheduled_payments_dialog(self):
        """Open the Scheduled Payments manager (Tools menu). Generating pre-entries
        posts pending rows into registers, so a change here can affect balances ->
        refresh the window."""
        from mammon.ui.scheduled_payments_dialog import ScheduledPaymentsDialog
        dlg = ScheduledPaymentsDialog(self.conn, parent=self)
        dlg.changed.connect(self._refresh_all)
        dlg.exec_()
        self._sync_scheduled_label()

    def _allocation_dialog(self):
        """Reports ▸ Asset Allocation…: what the user owns by asset class,
        security and account, over the scope the window remembers (investments,
        those plus cash, or everything including property). Read-only except
        the asset class given to a security or to an account."""
        from mammon.ui.portfolio_dialogs import AllocationDialog
        AllocationDialog(self.conn, parent=self).exec_()

    def _rebalance_dialog(self):
        """Reports ▸ Target & Drift…: the target asset mix and how far the real
        one has strayed from it. Read-only over the ledger -- it proposes the
        arithmetic that closes the gap and trades nothing."""
        from mammon.ui.rebalance_dialog import RebalanceDialog
        RebalanceDialog(self.conn, parent=self).exec_()

    def _projected_balances_dialog(self):
        """Tools ▸ Projected Balances…: the balance ahead, from entered rows,
        reminders and loan schedules (read-only)."""
        from mammon.ui.projection_dialogs import ProjectedBalancesDialog
        ProjectedBalancesDialog(self.conn, parent=self,
                                account_id=getattr(self, "_current_account", None)).exec_()

    def show_calendar(self):
        """Tools ▸ Financial Calendar: bring the calendar page of the register
        area forward (a month of events and daily balances). Leaving a register
        with a half-typed row asks first, exactly as switching accounts does --
        the calendar is a page like any other, and the stack would otherwise
        tear the open editor away."""
        current = self.stack.currentWidget()
        if (current is not None and current is not self.calendar
                and getattr(current, "has_open_editor", None) is not None
                and current.has_open_editor()):
            if not self._resolve_open_edit(current):
                return current              # cancelled -- stay where we are
        self.stack.setCurrentWidget(self.calendar)
        # Explicitly, not just on showEvent: a page swap inside a window that is
        # itself hidden (or minimised) delivers no show event, and coming back
        # to a stale month is exactly the bug this page would have.
        self.calendar.refresh_if_stale()
        return self.calendar

    def _hide_account(self, account_id):
        """Toolbar Hide Account -> confirm, hide, and drop it off the bar and the
        open-register stack. It stays in the Accounts… list to bring back."""
        acct = ledger.get_account(self.conn, account_id)
        if acct is None:
            return
        name = acct["name"]
        if QMessageBox.question(
                self, "Hide account",
                f"Hide '{name}' from the account list?\n"
                "Its data is kept; bring it back from the Accounts… list.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        ledger.set_account_hidden(self.conn, account_id, True)
        self._drop_register(account_id)
        self._refresh_all()

    def _drop_register(self, account_id):
        """Remove an open register from the stack (used when an account is hidden
        so its page can't linger after it leaves the bar)."""
        reg = self._registers.pop(account_id, None)
        if reg is not None:
            self.stack.removeWidget(reg)
            reg.deleteLater()
        if getattr(self, "_current_account", None) == account_id:
            self._current_account = None
            self.stack.setCurrentIndex(0)  # the calendar page

    def _open_loan_wizard(self, preselect):
        """Open the guided loan-parameter wizard (Task 53), pre-selecting
        ``preselect`` (an account id) when given. On Finish the parameters are
        saved and every balance/register refreshes (a new loan account appears in
        the bar). Shared by the register's Loan Setup button."""
        from mammon.ui.loan_wizard import LoanSetupWizard
        dlg = LoanSetupWizard(self.conn, account_id=preselect, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self._refresh_all()

    def _loan_setup_for_account(self, account_id):
        """Loan Setup button (top of a loan register): open the wizard for this
        account -- editing its stored parameters if a loan is already set up, or
        starting fresh setup on the liability account otherwise."""
        acct = ledger.get_account(self.conn, account_id)
        preselect = account_id if (acct is not None and
                                   acct["type"] == "liability") else None
        self._open_loan_wizard(preselect)

    def _enter_loan_payment(self, account_id):
        """Gear ▸ Enter Payment… on a loan register: post one payment the way
        the schedule would -- on the account that pays the loan, with the split
        shown before it is saved."""
        from mammon import loans_schedule
        from mammon.ui.loan_payment_dialog import EnterLoanPaymentDialog
        dlg = EnterLoanPaymentDialog(self.conn, account_id, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            loans_schedule.enter_payment(self.conn, account_id, **dlg.values())
            self._refresh_all()

    def _loan_setup_dialog(self):
        """Guided loan wizard, pre-selecting the current account when it is a
        liability. Retained for the programmatic/menu-free entry path."""
        aid = getattr(self, "_current_account", None)
        preselect = None
        if aid is not None:
            acct = ledger.get_account(self.conn, aid)
            if acct is not None and acct["type"] == "liability":
                preselect = aid
        self._open_loan_wizard(preselect)

    def _reconcile_dialog(self):
        aid = getattr(self, "_current_account", None)
        if aid is None:
            QMessageBox.information(self, "Reconcile to Statement",
                                    "Select an account on the left first.")
            return
        dlg = ReconcileDialog(self.conn, aid, parent=self)
        # Quicken flow: enter the three statement inputs FIRST; cancelling that
        # first dialog abandons the reconcile (the two-pane workspace never
        # shows). OK opens the workspace filtered to the statement date.
        #
        # An UNFINISHED reconcile skips the prompt: its inputs were restored from
        # the draft, so reopening the window lands straight back in the workspace
        # with the statement figures intact. 'Balances...' still edits them.
        if not dlg.has_draft and not dlg.prompt_balances(initial=True):
            return
        dlg.exec_()
        # Cleared marks (and, on Finish, reconciled marks) may have changed
        # regardless of how the dialog closed -- refresh so the register's Clr
        # column reflects them.
        self._refresh_all()
        if dlg.finished_ok:
            QMessageBox.information(self, "Reconcile to Statement",
                                    "Reconciliation complete - cleared items are now marked R.")

    def _print_register(self):
        aid = getattr(self, "_current_account", None)
        if aid is None:
            QMessageBox.information(self, "Print Register",
                                    "Select an account on the left first.")
            return
        acct = ledger.get_account(self.conn, aid)
        if acct is None:
            return
        from mammon.ui import printing
        rows = ledger.register_rows(self.conn, aid)
        bal = ledger.account_balance(self.conn, aid)
        subtitle = f"{len(rows)} transactions" if rows else "No transactions"
        html_str = printing.register_html(acct["name"], rows, bal, subtitle=subtitle)
        from PyQt5.QtPrintSupport import QPrinter, QPrintDialog
        printer = QPrinter(QPrinter.HighResolution)
        printer.setDocName(f"Mammon - {acct['name']}")
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("Print Register")
        # The system dialog includes 'Print to PDF/File', so this one action
        # covers both a physical printout and a saved PDF (Quicken's Print).
        if dlg.exec_() != QDialog.Accepted:
            return
        printing.render_html_to_printer(html_str, printer)

    def _report_categories(self, start, end):
        """Top-level category names for the customization checklist, taken from
        the full-range report so the list is stable as the user narrows dates."""
        from mammon import reports
        return [r.name for r in
                reports.spending_by_category(self.conn, start, end).rows]

    def _report_period_header(self, lay, dlg, customize, refresh,
                              default_key=None):
        """Report header row: the shared Period dropdown (§5.8d, default
        'Year-to-Date') to the LEFT of the Customize gear -- the SAME dropdown
        the reusable report windows use. Selecting a preset re-filters
        immediately; 'Custom' opens the existing customize dialog, defaulting its
        dates to the session's last-used custom range (or the ledger's full span
        if none has been used yet). The last-used custom range is remembered on
        the window for the session. The dialog's range is set to match the default
        selection so the dropdown and the displayed report agree from the first
        paint. ``default_key`` overrides the initial selection for the one window
        that needs a different default -- Net Worth Over Time passes
        ``NET_WORTH_PERIOD_DEFAULT`` so its cumulative curve spans the whole
        ledger; every other caller inherits ``PERIOD_DEFAULT`` (Year-to-Date)."""
        import datetime as _dt
        from PyQt5.QtWidgets import QHBoxLayout, QLabel
        from mammon.ui.report_filters import (customize_button, resolve_period,
                                             make_period_combo, PERIOD_DEFAULT)
        if default_key is None:
            default_key = PERIOD_DEFAULT

        # Shared factory: same widened combo (sized for "Earliest to date") as the
        # generalized ReportWindow uses. The currentIndexChanged connect below runs
        # after this, so seeding the default selection fires no premature refresh.
        combo = make_period_combo(default_key)
        # Make the initial range agree with the default selection.
        default_rng = resolve_period(default_key, self.conn, _dt.date.today())
        if default_rng:
            customize.filters.set_range(*default_rng)

        def _remember():
            self._last_custom_range = (customize.filters.start_iso(),
                                       customize.filters.end_iso())
        customize.applied.connect(_remember)

        def _on_period(idx):
            key = combo.itemData(idx)
            if key == "custom":
                rng = (getattr(self, "_last_custom_range", None)
                       or resolve_period("earliest", self.conn, _dt.date.today()))
                customize.filters.set_range(*rng)
                customize.exec_()   # Apply -> refresh + _remember via applied
            else:
                customize.filters.set_range(
                    *resolve_period(key, self.conn, _dt.date.today()))
                refresh()

        combo.currentIndexChanged.connect(_on_period)

        header = QHBoxLayout()
        header.addWidget(QLabel("Period:"))
        header.addWidget(combo)
        header.addStretch(1)
        header.addWidget(customize_button(customize, dlg))
        # Insert at the top so the period row sits above the report body even
        # though this runs after the body/refresh have been wired up.
        lay.insertLayout(0, header)
        dlg._period_combo = combo   # exposed for tests
        return combo

    def _open_report_window(self, spec):
        """Open the reusable report window on ``spec``. Modeless, so the window
        is kept alive in a list — without a retained reference it is
        garbage-collected the moment this method returns, and a single attribute
        would evict the previous report kind when a second one is opened."""
        from mammon.ui.report_window import ReportWindow
        win = ReportWindow(self.conn, parent=self, spec=spec)
        self._report_windows = getattr(self, "_report_windows", [])
        self._report_windows.append(win)
        win.show()

    def _cash_flow_window(self):
        """Open the reusable report window on the Cash Flow report."""
        from mammon.ui.report_window import CASH_FLOW_SPEC
        self._open_report_window(CASH_FLOW_SPEC)

    def _income_expense_window(self):
        """Open the reusable report window on the Income vs Expense report."""
        from mammon.ui.report_window import INCOME_EXPENSE_SPEC
        self._open_report_window(INCOME_EXPENSE_SPEC)

    def _balances_window(self):
        """Open the reusable report window on the Account Balances report."""
        from mammon.ui.report_window import ACCOUNT_BALANCES_SPEC
        self._open_report_window(ACCOUNT_BALANCES_SPEC)

    def _payee_window(self):
        """Open the reusable report window on the By Payee report."""
        from mammon.ui.report_window import BY_PAYEE_SPEC
        self._open_report_window(BY_PAYEE_SPEC)

    def _tag_window(self):
        """Open the reusable report window on the By Tag report."""
        from mammon.ui.report_window import BY_TAG_SPEC
        self._open_report_window(BY_TAG_SPEC)

    def _listing_window(self):
        """Open the reusable report window on the Transactions listing report."""
        from mammon.ui.report_window import TRANSACTIONS_SPEC
        self._open_report_window(TRANSACTIONS_SPEC)

    def _investment_performance_window(self):
        """Open the reusable report window on the Investment Performance report."""
        from mammon.ui.report_window import INVESTMENT_PERFORMANCE_SPEC
        self._open_report_window(INVESTMENT_PERFORMANCE_SPEC)

    def _spending_report_dialog(self):
        import datetime as _dt
        from mammon import reports
        from mammon.ui.report_filters import CustomizeDialog, filter_spending_report
        bstart, bend = ledger.transaction_date_bounds(self.conn)
        if not bstart:
            QMessageBox.information(self, "Spending by Category",
                                    "No transactions to report yet.")
            return
        # Default period is Year-to-Date; the category checklist covers the full
        # ledger so it stays complete when the period changes.
        start, end = reports.preset_range("ytd", _dt.date.today())
        dlg = QDialog(self)
        dlg.setWindowTitle("Spending by Category")
        lay = QVBoxLayout(dlg)
        customize = CustomizeDialog(self.conn, start, end, show_accounts=True,
                                    categories=self._report_categories(bstart, bend),
                                    parent=dlg)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        view.setFont(mono)
        lay.addWidget(view)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        lay.addWidget(buttons)

        def refresh():
            f = customize.filters
            report = reports.spending_by_category(
                self.conn, f.start_iso(), f.end_iso(), f.selected_account_ids())
            report = filter_spending_report(report, f.selected_categories())
            view.setPlainText(reports.format_spending_report(report))

        customize.applied.connect(refresh)
        self._report_period_header(lay, dlg, customize, refresh)
        refresh()
        dlg.resize(560, 620)
        dlg.exec_()

    def _itemize_window(self):
        """Reports > Itemize by Category, on the unified report window (§5.9b).

        The classic register-style drill-down runs inside one shared
        ``ReportWindow``, now as an expandable ``QTreeWidget``: each section
        (INCOME / EXPENSES / TRANSFERS) expands to its categories, a category to
        its sub-categories, and a leaf to the individual dated transactions. Its
        four columns read Category, Date, Payee / Memo, Amount -- Category first,
        so the Date header sits over the dates, not over the category names (the
        bug the flat-table migration introduced). It keeps the same gear
        (accounts, saved filter sets), Period dropdown and CSV / HTML / Print
        export every other report has; export flattens the tree, indenting the
        Category column by depth so the hierarchy survives.
        """
        from mammon.ui.report_window import ITEMIZE_SPEC
        self._open_report_window(ITEMIZE_SPEC)

    def _spending_chart_dialog(self):
        """Reports > Spending Chart (Pie): by-category spending as a pie of
        top-level EXPENSE categories over a customizable range / accounts /
        categories (income categories are excluded -- see the income pie).

        The long tail of small categories rolls into a single ``Other`` slice
        (the smallest categories that together make up 10% of the total), and
        clicking ``Other`` drills into its components -- the same rollup + drill
        the Income and Asset-Allocation pies use, all three reusing
        ``SlicesPieCanvas`` / ``group_small_slices`` (SRD 5.8d) so the logic
        lives in one place. Wedge percentages read against the whole period
        total even inside the drill. The Back button (or a click off the pie)
        returns."""
        from mammon import reports
        from mammon.ui.report_filters import CustomizeDialog
        from mammon.ui.charts import SlicesPieCanvas
        from mammon.ui.models import fmt_date
        start, end = ledger.transaction_date_bounds(self.conn)
        if not start:
            QMessageBox.information(self, "Spending Chart",
                                    "No transactions to report yet.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Spending by Category")
        lay = QVBoxLayout(dlg)
        customize = CustomizeDialog(self.conn, start, end, show_accounts=True,
                                    categories=self._report_categories(start, end),
                                    parent=dlg)
        back_btn = QPushButton("← Back to all categories")
        back_btn.setVisible(False)
        lay.addWidget(back_btn)
        holder = QVBoxLayout()
        lay.addLayout(holder)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        lay.addWidget(buttons)
        state = {"canvas": None}

        def on_zoom(path):
            # A drill path is non-empty only below the top level; the canvas also
            # comes back out on a click off the pie, so Back only mirrors that.
            back_btn.setVisible(bool(path))

        def go_back():
            if state["canvas"] is not None:
                state["canvas"].zoom_out()

        def refresh():
            f = customize.filters
            rows = reports.spending_category_rows(
                self.conn, f.start_iso(), f.end_iso(), f.selected_account_ids())
            sel = f.selected_categories()
            if sel is not None:                 # honor the category filter by name
                rows = [(n, c) for n, c in rows if n in sel]
            if state["canvas"] is not None:
                holder.removeWidget(state["canvas"])
                state["canvas"].setParent(None)
                state["canvas"].deleteLater()
            title = (f"Spending by Category   {fmt_date(f.start_iso())} to "
                     f"{fmt_date(f.end_iso())}")
            canvas = SlicesPieCanvas(title, rows, parent=dlg,
                                     empty_text="No spending in this period")
            canvas.zoomChanged.connect(on_zoom)
            holder.addWidget(canvas)
            state["canvas"] = canvas
            on_zoom(canvas.zoom_path())        # a fresh pie starts at the top

        back_btn.clicked.connect(go_back)
        customize.applied.connect(refresh)
        self._report_period_header(lay, dlg, customize, refresh)
        refresh()
        dlg.resize(720, 640)
        dlg.exec_()

    def _income_chart_dialog(self):
        """Reports > Income Chart (Pie): by-category income as a pie of top-level
        INCOME categories over a customizable range / accounts.

        The long tail of tiny income categories is rolled into a single ``Other``
        slice (everything under 10% of the total), and clicking ``Other`` drills
        into its component categories -- the same rollup + drill-down the
        asset-allocation pie uses. Both reuse ``SlicesPieCanvas`` /
        ``group_small_slices`` (SRD 5.8d), so the threshold/rollup logic lives in
        exactly one place. The Back button (or a click off the pie) returns."""
        from mammon import reports
        from mammon.ui.report_filters import CustomizeDialog
        from mammon.ui.charts import SlicesPieCanvas
        from mammon.ui.models import fmt_date
        start, end = ledger.transaction_date_bounds(self.conn)
        if not start:
            QMessageBox.information(self, "Income Chart",
                                    "No transactions to report yet.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Income by Category")
        lay = QVBoxLayout(dlg)
        customize = CustomizeDialog(self.conn, start, end, show_accounts=True,
                                    categories=None, parent=dlg)
        back_btn = QPushButton("← Back to all categories")
        back_btn.setVisible(False)
        lay.addWidget(back_btn)
        holder = QVBoxLayout()
        lay.addLayout(holder)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        lay.addWidget(buttons)
        state = {"canvas": None}

        def on_zoom(path):
            # A drill path is non-empty only below the top level; the canvas also
            # comes back out on a click off the pie, so Back only mirrors that.
            back_btn.setVisible(bool(path))

        def go_back():
            if state["canvas"] is not None:
                state["canvas"].zoom_out()

        def refresh():
            f = customize.filters
            rows = reports.income_category_rows(
                self.conn, f.start_iso(), f.end_iso(), f.selected_account_ids())
            if state["canvas"] is not None:
                holder.removeWidget(state["canvas"])
                state["canvas"].setParent(None)
                state["canvas"].deleteLater()
            title = (f"Income by Category   {fmt_date(f.start_iso())} to "
                     f"{fmt_date(f.end_iso())}")
            canvas = SlicesPieCanvas(title, rows, parent=dlg,
                                     empty_text="No income in this period")
            canvas.zoomChanged.connect(on_zoom)
            holder.addWidget(canvas)
            state["canvas"] = canvas
            on_zoom(canvas.zoom_path())        # a fresh pie starts at the top

        back_btn.clicked.connect(go_back)
        customize.applied.connect(refresh)
        self._report_period_header(lay, dlg, customize, refresh)
        refresh()
        dlg.resize(720, 640)
        dlg.exec_()

    def _net_worth_chart_dialog(self):
        """Reports > Net Worth Over Time: net worth sampled across a customizable
        date range as a line chart.

        Accounts DO subset it -- "how did the retirement accounts grow" is a
        question about net worth -- and the Include-hidden toggle matters here
        most of all: a plan that accumulated for years and was hidden once it
        emptied still held real money throughout, so leaving it out draws a cliff
        in the curve on a day when nothing moved.

        Unticking a CATEGORY draws a what-if: net worth as if that spending had
        never happened, which is a different question from net worth and is
        titled as such so a saved image cannot be mistaken for the real curve."""
        from mammon import reports
        from mammon.ui.report_filters import (CustomizeDialog,
                                              NET_WORTH_PERIOD_DEFAULT)
        from mammon.ui.charts import NetWorthCanvas
        start, end = ledger.transaction_date_bounds(self.conn)
        if not start:
            QMessageBox.information(self, "Net Worth Over Time",
                                    "No transactions to chart yet.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Net Worth Over Time")
        lay = QVBoxLayout(dlg)
        all_categories = self._report_categories(start, end)
        customize = CustomizeDialog(self.conn, start, end, show_accounts=True,
                                    categories=all_categories, parent=dlg)
        holder = QVBoxLayout()
        lay.addLayout(holder)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        lay.addWidget(buttons)
        state = {"canvas": None}

        def refresh():
            f = customize.filters
            kept = f.selected_categories()
            # The bar reports what is KEPT; the series takes what is dropped.
            dropped = ([c for c in all_categories if c not in kept]
                       if kept is not None else [])
            series = reports.net_worth_series(
                self.conn, f.start_iso(), f.end_iso(),
                account_ids=f.selected_account_ids(),
                include_hidden=f.include_hidden(),
                exclude_categories=dropped)
            if state["canvas"] is not None:
                holder.removeWidget(state["canvas"])
                state["canvas"].setParent(None)
                state["canvas"].deleteLater()
            canvas = NetWorthCanvas(series)
            holder.addWidget(canvas)
            state["canvas"] = canvas
            # A what-if curve must never be mistaken for the real one, in the
            # window or in a saved image.
            if dropped:
                shown = ", ".join(dropped[:3])
                if len(dropped) > 3:
                    shown += ", +%d more" % (len(dropped) - 3)
                dlg.setWindowTitle("Net Worth Over Time - as if no " + shown)
            else:
                dlg.setWindowTitle("Net Worth Over Time")

        customize.applied.connect(refresh)
        # Net Worth keeps the whole-ledger default; a cumulative curve is
        # meaningless over a partial-year YTD slice (SRD §5.8d).
        self._report_period_header(lay, dlg, customize, refresh,
                                   default_key=NET_WORTH_PERIOD_DEFAULT)
        refresh()
        dlg.resize(720, 640)
        dlg.exec_()

    def _refresh_all(self, exclude_account_id=None):
        """A write in any register can affect balances everywhere (transfers,
        net worth), so refresh the account bar and every open register.

        ``exclude_account_id`` names a register that already reloaded itself for
        this write (the one that emitted ``changed``); it is NOT reloaded again
        here so its just-captured selection/scroll survives for its own
        ``_restore_position`` -- otherwise Enter snaps the view to the top."""
        self.accounts.refresh()
        self._sync_scheduled_label()
        for aid, reg in self._registers.items():
            if aid != exclude_account_id:
                reg.model.reload()
            # Re-read the register's title/balance header too -- a transfer from
            # another account changes this account's balance (and an investment
            # account's cash/valuation) without any local edit.
            reg._refresh_header()
        # Keep an open Find dialog in sync: a just-edited transaction that no
        # longer matches the query drops out of the results live (by request).
        if self._find_dialog is not None:
            self._find_dialog.refresh_results()
        # The calendar page lives as long as the window, so it has to be told:
        # any write moves the balances its month is drawn from. It recomputes
        # only when on screen (see CalendarPanel.mark_stale).
        cal = getattr(self, "calendar", None)
        if cal is not None:
            cal.mark_stale()

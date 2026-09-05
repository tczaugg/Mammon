"""Rules Manager -- one dialog over all three learned-rule engines (Tools menu).

Mammon learns three kinds of correction from the user and, until now, exposed
them piecemeal (payee renaming had its own dialog; category and transfer rules
had none). This dialog is the single management surface for:

* **category rules** (``keyword -> category``, :mod:`mammon.category_rules`),
* **transfer rules** (``keyword -> account``, :mod:`mammon.transfer_rules`), and
* **learned payee mappings** (normalized payee ``-> category``,
  :mod:`mammon.categorize`; read + forget only).

For the two keyword engines it also surfaces the migration-40 conditions that
narrow *when* a rule fires -- an account scope, an inclusive signed-cent amount
range, and a case-insensitive memo substring (SRD's "roughly which account /
how much / what memo" gate).

Design (mirrors :mod:`mammon.ui.budget_widget`): a THIN projection. Every read
and write goes through the domain layer (``category_rules`` / ``transfer_rules``
/ ``categorize``); this file holds no SQL and no money arithmetic -- amounts are
rendered/parsed only through the shared ``ui.models.fmt_cents`` / ``parse_amount``
chokepoints, and stored as signed integer cents like everywhere else. Condition
edits save through ``upsert_rule``, which preserves any condition you do not
pass, so editing one cell never wipes the others. (upsert cannot write a
condition back to NULL, so "clear a condition entirely" is not offered inline;
delete and re-add the rule for that.)

Deletes route through the ``QMessageBox.question`` seam tests patch, never a bare
``exec_()`` modal, so the dialog is safe under the offscreen platform.
"""
from __future__ import annotations

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyledItemDelegate,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mammon import categorize, category_rules, ledger, transfer_rules
from mammon.ui.delegates import MoneyDelegate
from mammon.ui.models import fmt_cents, parse_amount


class _RuleModel(QAbstractTableModel):
    """Table model over one keyword engine (category or transfer).

    Keyword and target are display-only (set when the rule is created); the four
    condition columns are editable and persist through ``module.upsert_rule``.
    """

    KEYWORD, TARGET, SCOPE, MIN, MAX, MEMO = range(6)

    def __init__(self, conn, kind, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.kind = kind  # "category" | "transfer"
        if kind == "category":
            self.module = category_rules
            self._target_key = "category_id"
            self._target_header = "Category"
        else:
            self.module = transfer_rules
            self._target_key = "transfer_account_id"
            self._target_header = "Transfer to"
        self._rows: list[dict] = []
        self._accounts: dict[int, str] = {}
        self.reload()

    # -- data plumbing -----------------------------------------------------
    def reload(self):
        self.beginResetModel()
        self._rows = self.module.list_rules(self.conn)
        self._accounts = {
            a["id"]: a["name"]
            for a in ledger.list_accounts(
                self.conn, include_closed=True, include_hidden=True)
        }
        self.endResetModel()

    def scope_choices(self):
        """(label, account_id) pairs for the scope combo; blank == all."""
        out = [("(all accounts)", None)]
        for a in ledger.list_accounts(
                self.conn, include_closed=True, include_hidden=True):
            out.append((a["name"], a["id"]))
        return out

    def rule_id_at(self, row):
        if 0 <= row < len(self._rows):
            return int(self._rows[row]["id"])
        return None

    def _account_name(self, aid):
        if aid is None:
            return ""
        return self._accounts.get(int(aid), "#%s" % aid)

    # -- Qt model API ------------------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 6

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return ["Keyword", self._target_header, "Account scope",
                "Min amount", "Max amount", "Memo contains"][section]

    def flags(self, index):
        base = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if index.column() in (self.SCOPE, self.MIN, self.MAX, self.MEMO):
            return base | Qt.ItemIsEditable
        return base

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.EditRole):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if col == self.KEYWORD:
            return row["keyword"]
        if col == self.TARGET:
            if self.kind == "category":
                return ledger.category_path(self.conn, row["category_id"])
            return self._account_name(row["transfer_account_id"])
        if col == self.SCOPE:
            if role == Qt.EditRole:
                return row["account_id"]
            return self._account_name(row["account_id"]) or "(all accounts)"
        if col == self.MIN:
            v = row["amount_min_cents"]
            return "" if v is None else fmt_cents(v)
        if col == self.MAX:
            v = row["amount_max_cents"]
            return "" if v is None else fmt_cents(v)
        if col == self.MEMO:
            return row["memo_contains"] or ""
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid():
            return False
        row = self._rows[index.row()]
        col = index.column()
        cond_key = None
        cond_val = None
        if col == self.SCOPE:
            aid = value if value else None
            if aid is None:
                # Clearing scope back to "all accounts" needs a NULL write, which
                # upsert_rule cannot express; ignore rather than silently no-op.
                return False
            cond_key, cond_val = "account_id", int(aid)
        elif col in (self.MIN, self.MAX):
            text = "" if value is None else str(value).strip()
            if not text:
                return False
            cond_key = "amount_min_cents" if col == self.MIN else "amount_max_cents"
            cond_val = parse_amount(text)
        elif col == self.MEMO:
            text = "" if value is None else str(value).strip()
            if not text:
                return False
            cond_key, cond_val = "memo_contains", text
        else:
            return False
        # compound=True reconstructs the stored keyword verbatim, so saving a
        # condition never re-normalizes a multi-token keyword into a new rule.
        self.module.upsert_rule(
            self.conn, row["keyword"], row[self._target_key],
            compound=True, **{cond_key: cond_val})
        row[cond_key] = cond_val
        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
        return True


class _AccountScopeDelegate(QStyledItemDelegate):
    """Combo editor for the account-scope column; blank maps to account_id NULL."""

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        for label, aid in index.model().scope_choices():
            combo.addItem(label, aid)
        return combo

    def setEditorData(self, editor, index):
        aid = index.data(Qt.EditRole)
        i = editor.findData(aid)
        editor.setCurrentIndex(i if i >= 0 else 0)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData(), Qt.EditRole)


class _MappingModel(QAbstractTableModel):
    """Read-only table over the payee->category mappings (``import_mappings``).

    No renaming column: renaming raw bank text to a payee is the rename tree's
    job (Tools > Rename Rules), and ``mapped_payee`` only ever repeated the
    pattern in display case, which made this tab look like a second, competing
    rename list. See :mod:`mammon.categorize`.
    """

    PATTERN, CATEGORY, SOURCE, HITS = range(4)

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self._rows: list[dict] = []
        self.reload()

    def reload(self):
        self.beginResetModel()
        self._rows = categorize.list_mappings(self.conn)
        self.endResetModel()

    def mapping_id_at(self, row):
        if 0 <= row < len(self._rows):
            return int(self._rows[row]["id"])
        return None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 4

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return ["Payee", "Category", "Source", "Hits"][section]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.EditRole):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if col == self.PATTERN:
            return row["payee_pattern"]
        if col == self.CATEGORY:
            cid = row["mapped_category_id"]
            return (ledger.category_path(self.conn, cid)
                    if cid is not None else "(uncategorized)")
        if col == self.SOURCE:
            return row["source"] or ""
        if col == self.HITS:
            return str(row["hit_count"])
        return None


class RulesManagerWidget(QDialog):
    """Unified manager for category rules, transfer rules and learned payees."""

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Rules Manager")
        self.resize(760, 480)

        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        # -- category rules -----------------------------------------------
        self.cat_model = _RuleModel(conn, "category", self)
        self.cat_view = self._make_rule_view(self.cat_model)
        self.tabs.addTab(
            self._rule_tab(self.cat_view, self._on_add_category,
                           self._on_delete_category),
            "Category Rules")

        # -- transfer rules -----------------------------------------------
        self.xfer_model = _RuleModel(conn, "transfer", self)
        self.xfer_view = self._make_rule_view(self.xfer_model)
        self.tabs.addTab(
            self._rule_tab(self.xfer_view, self._on_add_transfer,
                           self._on_delete_transfer),
            "Transfer Rules")

        # -- learned payee mappings ---------------------------------------
        self.map_model = _MappingModel(conn, self)
        self.map_view = QTableView()
        self.map_view.setModel(self.map_model)
        self.map_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.map_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.map_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.map_view.horizontalHeader().setStretchLastSection(True)
        self.tabs.addTab(self._mapping_tab(), "Payee Categories")

        foot = QHBoxLayout()
        foot.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        foot.addWidget(close_btn)
        outer.addLayout(foot)

    # -- view/tab construction --------------------------------------------
    def _make_rule_view(self, model):
        view = QTableView()
        view.setModel(model)
        view.setSelectionBehavior(QAbstractItemView.SelectRows)
        view.setSelectionMode(QAbstractItemView.SingleSelection)
        view.horizontalHeader().setStretchLastSection(True)
        view.setItemDelegateForColumn(
            _RuleModel.SCOPE, _AccountScopeDelegate(view))
        view.setItemDelegateForColumn(_RuleModel.MIN, MoneyDelegate(view))
        view.setItemDelegateForColumn(_RuleModel.MAX, MoneyDelegate(view))
        return view

    def _rule_tab(self, view, on_add, on_delete):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel("Double-click a condition cell to narrow when a rule fires "
                   "(scope / amount range / memo). Blank means no condition."))
        lay.addWidget(view, 1)
        btns = QHBoxLayout()
        add_btn = QPushButton("Add Rule…")
        add_btn.clicked.connect(on_add)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(on_delete)
        btns.addWidget(add_btn)
        btns.addWidget(del_btn)
        btns.addStretch(1)
        lay.addLayout(btns)
        return w

    def _mapping_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(
            QLabel("The category Mammon gives each payee. Delete one to forget it. "
                   "Renaming raw bank text to a payee is separate — Tools ▸ Rename Rules."))
        lay.addWidget(self.map_view, 1)
        btns = QHBoxLayout()
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._on_delete_mapping)
        btns.addWidget(del_btn)
        btns.addStretch(1)
        lay.addLayout(btns)
        return w

    # -- public add API (used by the Add buttons and by tests) ------------
    def add_category_rule(self, keyword, category_id, *, account_id=None,
                          amount_min_cents=None, amount_max_cents=None,
                          memo_contains=None):
        """Create/update a category rule via the domain layer, then refresh."""
        category_rules.upsert_rule(
            self.conn, keyword, category_id, account_id=account_id,
            amount_min_cents=amount_min_cents, amount_max_cents=amount_max_cents,
            memo_contains=memo_contains)
        self.cat_model.reload()

    def add_transfer_rule(self, keyword, transfer_account_id, *, account_id=None,
                          amount_min_cents=None, amount_max_cents=None,
                          memo_contains=None):
        """Create/update a transfer rule via the domain layer, then refresh."""
        transfer_rules.upsert_rule(
            self.conn, keyword, transfer_account_id, account_id=account_id,
            amount_min_cents=amount_min_cents, amount_max_cents=amount_max_cents,
            memo_contains=memo_contains)
        self.xfer_model.reload()

    # -- add-button seams (interactive; tests call the public API above) ---
    def _on_add_category(self):
        keyword, cid = self._prompt_new_rule("category")
        if not keyword or cid is None:
            return
        self.add_category_rule(keyword, cid)

    def _on_add_transfer(self):
        keyword, aid = self._prompt_new_rule("transfer")
        if not keyword or aid is None:
            return
        self.add_transfer_rule(keyword, aid)

    def _prompt_new_rule(self, kind):
        """Gather (keyword, target_id) for a new rule via input dialogs.

        Overridable seam: tests drive :meth:`add_category_rule` /
        :meth:`add_transfer_rule` directly rather than these blocking modals."""
        keyword, ok = QInputDialog.getText(
            self, "New Rule", "Keyword (from the bank description):")
        if not ok or not keyword.strip():
            return None, None
        if kind == "category":
            choices = [(c["path"], c["id"])
                       for c in ledger.list_categories(self.conn)]
            prompt = "Category:"
        else:
            choices = [(a["name"], a["id"])
                       for a in ledger.list_accounts(self.conn, include_closed=True)]
            prompt = "Transfer to account:"
        if not choices:
            return None, None
        labels = [c[0] for c in choices]
        label, ok = QInputDialog.getItem(
            self, "New Rule", prompt, labels, 0, False)
        if not ok:
            return None, None
        for text, val in choices:
            if text == label:
                return keyword.strip(), val
        return None, None

    # -- deletes (routed through the QMessageBox.question seam) ------------
    def _selected_row(self, view):
        idx = view.currentIndex()
        if idx.isValid():
            return idx.row()
        rows = view.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _confirm(self, title, text):
        return QMessageBox.question(
            self, title, text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def _on_delete_category(self):
        self._delete_rule(self.cat_view, self.cat_model,
                          category_rules, "category rule")

    def _on_delete_transfer(self):
        self._delete_rule(self.xfer_view, self.xfer_model,
                          transfer_rules, "transfer rule")

    def _delete_rule(self, view, model, module, label):
        row = self._selected_row(view)
        if row is None:
            return
        rid = model.rule_id_at(row)
        if rid is None:
            return
        if not self._confirm("Delete Rule", "Delete this %s?" % label):
            return
        module.delete_rule(self.conn, rid)
        model.reload()

    def _on_delete_mapping(self):
        row = self._selected_row(self.map_view)
        if row is None:
            return
        mid = self.map_model.mapping_id_at(row)
        if mid is None:
            return
        if not self._confirm(
                "Forget Mapping", "Forget this learned payee mapping?"):
            return
        categorize.forget_mapping(self.conn, mid)
        self.map_model.reload()

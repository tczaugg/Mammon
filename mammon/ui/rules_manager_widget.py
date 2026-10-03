"""Learned Categories -- what the category tree knows about each payee (Tools menu).

Every learned behavior in Mammon is a decision tree (the user's ruling, 2026-10).
Payee renaming has its own window (Tools > Rename Rules); this one is the other
tree, :mod:`mammon.category_tree`, which learns what goes in the Category cell --
a category, or a transfer account written ``[Account]`` -- under each payee and
under each payee in each account.

It replaces the Rules Manager, which edited keyword tables: ``keyword ->
category`` and ``keyword -> account`` rules with account/amount/memo conditions,
plus the old payee -> category mappings. Those tables were the Quicken mechanism,
and a keyword learned from one accept filed one card's autopay to another card
with identical bank text. Migration 116 dropped them.

So there is nothing here to write -- the tree learns from what the user does. The
window shows the evidence instead: per payee, every category and transfer
account it has carried with how many times, overall and in each account, and
whether the payee is consistent enough to be filled in automatically. Forgetting
is the one action: a whole payee, or one category or account for it, so a payee
that learned something wrong starts over.

A thin projection like every other manager panel: reads and the forget go
through :mod:`mammon.category_tree`; no SQL here. The forget confirmation routes
through the ``QMessageBox.question`` seam tests patch, never a bare ``exec_()``.
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from mammon import category_tree, ledger


def label_text(conn, label: int) -> str:
    """A label as the Category cell shows it: a category path, or ``[Account]``."""
    acct = category_tree.label_account(label)
    if acct is not None:
        row = ledger.get_account(conn, acct)
        return f"[{row['name']}]" if row is not None else ""
    cat = category_tree.label_category(label)
    if cat is None:
        return ""
    return ledger.category_path(conn, cat) or ""


class LearnedCategoriesDialog(QDialog):
    """Payees on top, the selected payee's categories and accounts below."""

    PAYEE, TIMES, CHOICES, FILLS = range(4)
    LABEL, LABEL_TIMES, BY_ACCOUNT = range(3)

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Learned Categories")
        self.resize(720, 560)

        hint = QLabel(
            "What Mammon has learned about each payee's category or transfer "
            "account, from the rows you accepted and the categories you set. "
            "A payee fills in by itself once it has been the same thing at "
            "least twice and is usually that one thing; otherwise its choices "
            "are offered first. Forget a payee, or one of its choices, to have "
            "Mammon learn it over.")
        hint.setWordWrap(True)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Find a payee")
        self.filter_edit.textChanged.connect(self._fill_payees)

        self.payees = QTableWidget(0, 4)
        self.payees.setHorizontalHeaderLabels(
            ["Payee", "Times", "Choices", "Fills in"])
        self._table_defaults(self.payees)
        self.payees.currentCellChanged.connect(lambda *_: self._fill_labels())

        self.labels = QTableWidget(0, 3)
        self.labels.setHorizontalHeaderLabels(
            ["Category or account", "Times", "By account"])
        self._table_defaults(self.labels)
        self.labels.currentCellChanged.connect(lambda *_: self._sync_buttons())

        self.forget_payee_button = QPushButton("Forget payee")
        self.forget_payee_button.clicked.connect(self.forget_selected_payee)
        self.forget_label_button = QPushButton("Forget this choice")
        self.forget_label_button.clicked.connect(self.forget_selected_label)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.forget_payee_button)
        buttons.addWidget(self.forget_label_button)
        buttons.addStretch(1)
        buttons.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addWidget(hint)
        lay.addWidget(self.filter_edit)
        lay.addWidget(self.payees, 3)
        lay.addWidget(QLabel("Choices for the selected payee"))
        lay.addWidget(self.labels, 2)
        lay.addLayout(buttons)

        self._rows: list[dict] = []
        self._label_rows: list[int] = []
        self.reload()

    @staticmethod
    def _table_defaults(table: QTableWidget) -> None:
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)

    # ---- data ------------------------------------------------------------
    def reload(self) -> None:
        """Re-read the tree. Payees are shown as the register most recently
        spelled them; the tree keys them normalized ("COSTCO WHSE")."""
        spelled: dict[str, str] = {}
        for name in ledger.list_payees(self.conn):
            spelled.setdefault(category_tree.normalized_key(name), name)
        self._all = []
        for row in category_tree.list_payees(self.conn):
            key = row["payee_key"]
            self._all.append({**row, "name": spelled.get(key, key.title())})
        self._all.sort(key=lambda r: (-r["votes"], r["name"].lower()))
        self._fill_payees()

    def _fill_payees(self) -> None:
        text = self.filter_edit.text().strip().lower()
        self._rows = [r for r in self._all
                      if not text or text in r["name"].lower()]
        self.payees.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            fills = r["coherence"] >= category_tree.PAYEE_COHERENCE
            for col, value in ((self.PAYEE, r["name"]), (self.TIMES, str(r["votes"])),
                               (self.CHOICES, str(r["categories"])),
                               (self.FILLS, "Yes" if fills else "Offers choices")):
                item = QTableWidgetItem(value)
                if col in (self.TIMES, self.CHOICES):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.payees.setItem(i, col, item)
        self.payees.resizeColumnsToContents()
        if self._rows:
            self.payees.setCurrentCell(0, 0)
        self._fill_labels()

    def selected_payee(self):
        i = self.payees.currentRow()
        return self._rows[i] if 0 <= i < len(self._rows) else None

    def selected_label(self):
        i = self.labels.currentRow()
        return self._label_rows[i] if 0 <= i < len(self._label_rows) else None

    def _fill_labels(self) -> None:
        payee = self.selected_payee()
        self._label_rows = []
        self.labels.setRowCount(0)
        if payee is not None:
            key = payee["payee_key"]
            per_account = category_tree.labels_by_account(self.conn, key)
            names = {a["id"]: a["name"]
                     for a in ledger.list_accounts(self.conn, include_closed=True)}
            known = category_tree.known_categories(self.conn, key)
            self.labels.setRowCount(len(known))
            for i, (label, count) in enumerate(known):
                parts = []
                for acct, labs in per_account.items():
                    n = dict(labs).get(label)
                    if n:
                        parts.append(f"{names.get(acct, '?')}: {n}")
                for col, value in ((self.LABEL, label_text(self.conn, label)),
                                   (self.LABEL_TIMES, str(count)),
                                   (self.BY_ACCOUNT, ", ".join(parts))):
                    item = QTableWidgetItem(value)
                    if col == self.LABEL_TIMES:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.labels.setItem(i, col, item)
                self._label_rows.append(label)
            self.labels.resizeColumnsToContents()
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.forget_payee_button.setEnabled(self.selected_payee() is not None)
        self.forget_label_button.setEnabled(self.selected_label() is not None)

    # ---- actions ---------------------------------------------------------
    def forget_selected_payee(self) -> bool:
        payee = self.selected_payee()
        if payee is None:
            return False
        if QMessageBox.question(
                self, "Forget Payee",
                f"Forget every category and transfer account Mammon learned for "
                f"{payee['name']}? It will learn them again from what you do next.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return False
        category_tree.forget_payee(self.conn, payee["payee_key"])
        self.reload()
        return True

    def forget_selected_label(self) -> bool:
        payee, label = self.selected_payee(), self.selected_label()
        if payee is None or label is None:
            return False
        what = label_text(self.conn, label) or "this choice"
        if QMessageBox.question(
                self, "Forget Choice",
                f"Forget that {payee['name']} goes to {what}?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return False
        category_tree.forget_payee(self.conn, payee["payee_key"], label=label)
        self.reload()
        return True

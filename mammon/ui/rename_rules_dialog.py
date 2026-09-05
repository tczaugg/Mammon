"""Management surface for the learned payee-renaming tree.

Replaces the old ``keyword -> payee`` rules dialog. Payee renaming now learns
into an online discriminative tree (:mod:`mammon.rename_tree`), so instead of
hand-edited keyword rows this dialog reports, per learned payee, how many times a
rename to it was APPLIED and how many times it was OVERRIDDEN, alongside how much
evidence the tree holds. A payee can be forgotten (removed from the tree).

Auto-rename is not a tunable knob: the tree decides by node payee CARDINALITY (a
single payee auto-renames, several offer a typeable dropdown), so there is no
confidence threshold to adjust here.

The selection wiring (``currentCellChanged`` -> ``_sync_buttons``) is connected so
the Delete button enables on a single click -- the omission that made the old
dialog's Delete button appear broken.
"""
from __future__ import annotations

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from mammon import rename_tree


class RenameRulesDialog(QDialog):
    """Per-payee applied/overridden report over the learned rename tree.

    ``changed`` fires whenever a payee is forgotten so the owner can refresh
    anything that depends on renaming behaviour.
    """

    changed = pyqtSignal()

    PAYEE, APPLIED, OVERRIDDEN, EXAMPLES = range(4)

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Payee Renaming")
        self.resize(560, 400)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Payee", "Times applied", "Times overridden", "Examples learned"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        # The fix the old dialog was missing: enable Delete on selection change.
        self.table.currentCellChanged.connect(lambda *_: self._sync_buttons())

        self.delete_btn = QPushButton("Forget payee")
        self.delete_btn.clicked.connect(self._delete_selected)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.delete_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(close_btn)

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Learned from your import-review renames. Mammon auto-fills a payee "
            "when the match points to a single payee, offers a typeable dropdown "
            "when several payees share the pattern, and leaves the row unchanged "
            "when the match is too weak.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(self.table)
        layout.addLayout(btn_row)

        self._rows: list = []
        self._reload()

    # ---- data ------------------------------------------------------------
    def _reload(self):
        self._rows = rename_tree.rename_stats(self.conn)
        self.table.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            self.table.setItem(i, self.PAYEE, QTableWidgetItem(r["payee"]))
            self.table.setItem(
                i, self.APPLIED, QTableWidgetItem(str(r["applied"])))
            self.table.setItem(
                i, self.OVERRIDDEN, QTableWidgetItem(str(r["overridden"])))
            self.table.setItem(
                i, self.EXAMPLES, QTableWidgetItem(str(r["examples"])))
        self._sync_buttons()

    def _selected_row(self):
        idx = self.table.currentRow()
        if 0 <= idx < len(self._rows):
            return self._rows[idx]
        return None

    def _sync_buttons(self):
        self.delete_btn.setEnabled(self._selected_row() is not None)

    # ---- actions ---------------------------------------------------------
    def _delete_selected(self):
        row = self._selected_row()
        if row is None:
            return
        resp = QMessageBox.question(
            self, "Forget Payee",
            "Forget everything Mammon learned about renaming to '%s'?" % row["payee"],
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if resp != QMessageBox.Yes:
            return
        rename_tree.forget_payee(self.conn, row["payee"])
        self._reload()
        self.changed.emit()

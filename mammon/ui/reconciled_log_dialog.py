"""Reconciled-change-log viewer -- a strictly read-only window over a single
account's ``reconciled_change_log`` (migration 63, written by ``mammon.ledger``).

A reconciled transaction should almost never change; when one does, it can
silently throw off the next reconcile of that account. The domain layer records
every such edit and deletion, and this dialog just renders that trail: it holds
no SQL and no money logic, calling :func:`mammon.ledger.reconciled_change_log`
and showing the stored TEXT verbatim. There is nothing to edit here on purpose --
the log is evidence, so the table is view-only and the only button is Close.
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from mammon import ledger


class ReconciledChangeLogDialog(QDialog):
    """Show every audited change to a reconciled transaction in one account."""

    COLUMNS = ("When", "Operation", "Field", "Old value", "New value")

    def __init__(self, conn, account_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.account_id = int(account_id)

        acct = ledger.get_account(conn, self.account_id)
        self.account_name = acct["name"] if acct is not None else str(account_id)
        self.setWindowTitle("Reconciled change log - %s" % self.account_name)
        self.resize(680, 420)

        lay = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.reload()

    def reload(self):
        """Re-read the log for this account and repaint the table."""
        entries = ledger.reconciled_change_log(self.conn, self.account_id)
        self.table.setRowCount(len(entries))
        for r, e in enumerate(entries):
            cells = (
                e["changed_at"] or "",
                e["operation"] or "",
                e["field"] or "",
                "" if e["old_value"] is None else e["old_value"],
                "" if e["new_value"] is None else e["new_value"],
            )
            for c, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        if entries:
            self.summary.setText(
                "%d recorded change%s to reconciled transactions."
                % (len(entries), "" if len(entries) == 1 else "s"))
        else:
            self.summary.setText(
                "No reconciled transaction in this account has been changed.")

"""File ▸ Export… (roadmap item 8): the whole ledger out as QIF, JSON or CSV.

A thin front for :mod:`mammon.export`: pick the format, which accounts, an
optional date range, whether a JSON export keeps the identifying columns, and
where to write. The file picker sits behind ``_pick_path`` so the dialog can be
driven headless; the export itself runs in the window after the dialog
accepts, through ``export.run``.
"""
from __future__ import annotations

import os
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout,
)

from mammon import export, ledger
from mammon.ui.delegates import date_edit_iso, make_date_edit

FORMAT_CHOICES = (
    ("qif", "Quicken Interchange Format (.qif): accounts, categories, transactions, "
            "securities, prices -- what other finance programs read"),
    ("json", "JSON (.json): every table, lossless -- the complete ledger"),
    ("csv", "CSV: one register per account, into a folder"),
)


class ExportDialog(QDialog):
    def __init__(self, conn, parent=None, db_path: Optional[str] = None):
        super().__init__(parent)
        self.conn = conn
        self.db_path = db_path
        self.setWindowTitle("Export Ledger")
        self.resize(620, 520)
        self.format = QComboBox()
        for key, label in FORMAT_CHOICES:
            self.format.addItem(label, key)
        self.format.currentIndexChanged.connect(lambda *_: self._sync())
        self.by_year = QCheckBox(
            "One file per year, <name>-YYYY.qif beside the chosen file "
            "(a fallback if the receiving program balks at one large file)")

        self.accounts = QListWidget()
        for a in ledger.list_accounts(conn, include_closed=True, include_hidden=True):
            item = QListWidgetItem(a["name"])
            item.setData(Qt.UserRole, int(a["id"]))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.accounts.addItem(item)
        self.accounts.setMaximumHeight(160)

        self.limit_dates = QCheckBox("Only transactions dated")
        self.start = make_date_edit(self, "", blank_ok=True)
        self.end = make_date_edit(self, "", blank_ok=True)
        self.limit_dates.toggled.connect(lambda *_: self._sync())
        dates = QHBoxLayout()
        dates.addWidget(self.limit_dates)
        dates.addWidget(QLabel("from"))
        dates.addWidget(self.start)
        dates.addWidget(QLabel("to"))
        dates.addWidget(self.end)
        dates.addStretch(1)

        self.include_sensitive = QCheckBox(
            "Include account numbers, bank URLs and download settings (JSON only)")
        self.path = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path, 1)
        path_row.addWidget(browse)

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: gray;")

        form = QFormLayout()
        form.addRow("Format", self.format)
        form.addRow("", self.by_year)
        form.addRow("Accounts", self.accounts)
        form.addRow("", self._wrap(dates))
        form.addRow("", self.include_sensitive)
        form.addRow("Write to", self._wrap(path_row))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Export")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.note)
        layout.addWidget(buttons)
        self._sync()

    @staticmethod
    def _wrap(inner):
        from PyQt5.QtWidgets import QWidget
        w = QWidget()
        w.setLayout(inner)
        return w

    # -- state -----------------------------------------------------------
    def _sync(self) -> None:
        fmt = self.format.currentData()
        self.by_year.setEnabled(fmt == "qif")
        self.include_sensitive.setEnabled(fmt == "json")
        self.accounts.setEnabled(fmt != "json")
        self.limit_dates.setEnabled(fmt != "json")
        on = self.limit_dates.isChecked() and fmt != "json"
        self.start.setEnabled(on)
        self.end.setEnabled(on)
        self.note.setText({
            "qif": "Tags, loan setups, scheduled payments and learned rules have no "
                   "place in QIF and stay behind; an odd stock split ratio is rounded. "
                   "Importing the file into an empty Mammon database reproduces every "
                   "balance and holding. Quicken will not read it back faithfully: its "
                   "own import lifts transfer lines out of split transactions, so a "
                   "paycheck or a loan payment arrives with the rest of the split "
                   "missing. The file is correct; that limitation is Quicken's.",
            "json": "Everything, exactly as stored, with the schema version. Identifying "
                    "columns are blanked unless ticked above.",
            "csv": "Each account's register with its running balance; split legs are "
                   "listed in the last column.",
        }.get(fmt, ""))
        if not self.path.text().strip():
            self.path.setText(self._default_path(fmt))

    def _default_path(self, fmt: str) -> str:
        base = os.path.splitext(os.path.basename(self.db_path or "mammon.db"))[0]
        folder = os.path.dirname(os.path.abspath(self.db_path)) if self.db_path else os.getcwd()
        if fmt == "csv":
            return os.path.join(folder, f"{base}-registers")
        return os.path.join(folder, f"{base}-export.{fmt}")

    def _pick_path(self, fmt: str, current: str) -> str:
        """The file/folder picker; a seam tests override."""
        if fmt == "csv":
            return QFileDialog.getExistingDirectory(self, "Folder for the CSV registers",
                                                    current)
        flt = {"qif": "Quicken Interchange Format (*.qif)", "json": "JSON (*.json)"}[fmt]
        path, _ = QFileDialog.getSaveFileName(self, "Export Ledger", current, flt)
        return path

    def _browse(self) -> None:
        fmt = self.format.currentData()
        chosen = self._pick_path(fmt, self.path.text().strip())
        if chosen:
            if fmt != "csv" and not os.path.splitext(chosen)[1]:
                chosen += f".{fmt}"
            self.path.setText(chosen)
            self.path.setText(chosen)

    def account_ids(self):
        """None for every account, else the ticked ids."""
        all_ids, ticked = [], []
        for i in range(self.accounts.count()):
            item = self.accounts.item(i)
            all_ids.append(item.data(Qt.UserRole))
            if item.checkState() == Qt.Checked:
                ticked.append(item.data(Qt.UserRole))
        return None if len(ticked) == len(all_ids) else ticked

    def values(self) -> dict:
        fmt = self.format.currentData()
        on = self.limit_dates.isChecked() and fmt != "json"
        return {"fmt": fmt, "path": self.path.text().strip(),
                "by_year": fmt == "qif" and self.by_year.isChecked(),
                "account_ids": None if fmt == "json" else self.account_ids(),
                "start": (date_edit_iso(self.start) or None) if on else None,
                "end": (date_edit_iso(self.end) or None) if on else None,
                "include_sensitive": fmt == "json" and self.include_sensitive.isChecked()}

    def validate(self):
        v = self.values()
        if not v["path"]:
            return False, "Choose where to write the export."
        if v["account_ids"] == []:
            return False, "Tick at least one account."
        if v["start"] and v["end"] and v["start"] > v["end"]:
            return False, "The date range ends before it starts."
        return True, ""

    def _warn(self, msg: str) -> None:
        QMessageBox.warning(self, "Export Ledger", msg)

    def accept(self) -> None:
        ok, msg = self.validate()
        if not ok:
            self._warn(msg)
            return
        super().accept()


def perform(conn, values: dict) -> str:
    """Run the export the dialog described; returns the summary line."""
    v = dict(values)
    return export.run(conn, v.pop("fmt"), v.pop("path"), **v)

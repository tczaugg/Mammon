"""Securities manager -- confirm each security's identity and its description.

This is the confirmation step :mod:`mammon.securities` refuses to skip. The
module can propose that "VGT VANGUARD INFO TECH ETF" is really ticker ``VGT``
plus the description "VANGUARD INFO TECH ETF", but it will not apply that on its
own, because the same derivation turns the plan fund "FID BALANCED K6" into
``FID`` and "INTL EQUITY INDEX" into ``INTL`` -- a real listed company whose
prices are already in the file. Getting that wrong files a stranger's prices
against a retirement fund and leaves nothing in the data to say so. So every
proposal is shown, every one is editable, and nothing moves until Apply.

The table is sorted so a MERGE and the rows it absorbs sit together, since a
merge is the one change here that is not reversible by re-running the tool: once
"VGT" and "VGT VANGUARD INFO TECH ETF" are one security, the file no longer
records which rows came from which spelling. Merges are ticked by default --
two spellings of one holding is the condition this exists to repair -- but the
Status column says so in words, and the count says how much moves.

Headless-safe: the confirmation goes through ``QMessageBox.question`` and the
work through an overridable ``_apply`` seam, so tests drive it without opening a
modal that would block forever under the offscreen platform (CLAUDE.md).
"""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from mammon import securities

# Column order. Identity and Description are the only editable cells; the rest
# describe what the choice will do.
INCLUDE, STORED, IDENTITY, DESCRIPTION, ROWS, STATUS = range(6)
HEADERS = ["", "Stored as", "Identity", "Description", "Rows", "What happens"]


class SecuritiesDialog(QDialog):
    """Review and apply the ticker/description split across the whole file."""

    changed = pyqtSignal()

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Securities")
        self.resize(900, 560)

        lay = QVBoxLayout(self)
        blurb = QLabel(
            "Each security has an IDENTITY (its ticker, used to key prices and "
            "quotes) and a DESCRIPTION (what you read). Both are editable. A "
            "security with no public ticker -- a plan's own fund -- keeps its "
            "name as its identity; clear the Identity cell back to the stored "
            "name to say so.")
        blurb.setWordWrap(True)
        lay.addWidget(blurb)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self.table)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        bar = QHBoxLayout()
        self.btn_all = QPushButton("Select all changes")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none = QPushButton("Select none")
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        bar.addWidget(self.btn_all)
        bar.addWidget(self.btn_none)
        bar.addStretch()
        lay.addLayout(bar)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.btn_apply = buttons.addButton("Apply", QDialogButtonBox.ApplyRole)
        self.btn_apply.clicked.connect(self.on_apply)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.reload()

    # ---- display ---------------------------------------------------------
    def reload(self):
        self._loading = True
        self._splits = securities.suggest_all(self.conn)
        self._counts = securities.usage_counts(self.conn)
        # Against the FILE, not just against the other proposals -- see
        # securities.merge_preview on why the two differ.
        groups = securities.merge_preview(self.conn, self._splits)
        # Merges first, then other changes, then the already-correct rows -- the
        # decisions that matter should not be hunted for among 20 no-ops.
        def rank(s):
            if s.symbol in groups:
                return (0, s.symbol.upper(), s.old.upper())
            return (1 if s.changes_key else 2, s.symbol.upper(), s.old.upper())
        self._splits.sort(key=rank)

        self.table.setRowCount(len(self._splits))
        for row, s in enumerate(self._splits):
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            # A no-op needs no confirmation; a merge is the repair this exists
            # for, so it starts ticked like any other change.
            actionable = s.changes_key or bool(s.name)
            tick.setCheckState(Qt.Checked if actionable else Qt.Unchecked)
            if not actionable:
                tick.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, INCLUDE, tick)

            stored = QTableWidgetItem(s.old)
            stored.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, STORED, stored)
            self.table.setItem(row, IDENTITY, QTableWidgetItem(s.symbol))
            self.table.setItem(row, DESCRIPTION, QTableWidgetItem(s.name or ""))

            n = QTableWidgetItem(f"{self._counts.get(s.old, 0):,}")
            n.setFlags(Qt.ItemIsEnabled)
            n.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, ROWS, n)

            status = QTableWidgetItem(self._status_text(s, groups))
            status.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, STATUS, status)

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(
            DESCRIPTION, QHeaderView.Stretch)
        self._loading = False
        self._refresh_summary()

    @staticmethod
    def _status_text(split, groups) -> str:
        if split.symbol in groups:
            others = [o for o in groups[split.symbol] if o != split.old]
            return ("merges with " + ", ".join(others)) if others else "merge"
        if split.changes_key:
            return "renamed to its ticker"
        if split.name:
            return "description recorded"
        return "unchanged"

    def _refresh_summary(self):
        chosen = self.chosen()
        merges = securities.merge_preview(self.conn, chosen)
        moving = sum(self._counts.get(s.old, 0) for s in chosen if s.changes_key)
        bits = [f"{len(chosen)} securit{'y' if len(chosen) == 1 else 'ies'} selected"]
        if moving:
            bits.append(f"{moving:,} rows re-keyed")
        if merges:
            bits.append(f"{len(merges)} merge{'' if len(merges) == 1 else 's'}")
        self.summary.setText("   ".join(bits))

    def _on_item_changed(self, _item):
        if getattr(self, "_loading", False):
            return
        self._refresh_summary()

    def _set_all(self, on: bool):
        self._loading = True
        for row in range(self.table.rowCount()):
            item = self.table.item(row, INCLUDE)
            if item is not None and item.flags() & Qt.ItemIsUserCheckable:
                item.setCheckState(Qt.Checked if on else Qt.Unchecked)
        self._loading = False
        self._refresh_summary()

    # ---- applying --------------------------------------------------------
    def chosen(self) -> list:
        """The ticked rows as :class:`~mammon.securities.Split`, reflecting any
        edit made in the Identity/Description cells."""
        out = []
        for row in range(self.table.rowCount()):
            tick = self.table.item(row, INCLUDE)
            if tick is None or tick.checkState() != Qt.Checked:
                continue
            old = self._splits[row].old
            ident = (self.table.item(row, IDENTITY).text() or "").strip() or old
            desc = (self.table.item(row, DESCRIPTION).text() or "").strip() or None
            out.append(securities.Split(old, ident, desc))
        return out

    def _apply(self, chosen):
        """Seam: the write. Overridden by headless tests."""
        return securities.apply_splits(self.conn, chosen)

    def on_apply(self):
        chosen = self.chosen()
        if not chosen:
            QMessageBox.information(self, "Securities", "Nothing is selected.")
            return
        # merge_preview, not collisions: a rename onto an identity ALREADY in the
        # file is a merge even though that spelling needs no change of its own,
        # so it is never among the proposals being compared.
        merges = securities.merge_preview(self.conn, chosen)
        lines = ["Apply %d change%s?" % (len(chosen),
                                         "" if len(chosen) == 1 else "s")]
        if merges:
            # A merge is the one thing here that cannot be undone by re-running
            # the tool, so it is spelled out rather than counted.
            lines += ["", "These will be MERGED into one security each:"]
            for key, olds in sorted(merges.items()):
                lines.append("  %s  <-  %s" % (key, ", ".join(olds)))
            lines += ["", "Merging cannot be undone from here."]
        if QMessageBox.question(
                self, "Securities", chr(10).join(lines),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        report = self._apply(chosen)
        self.reload()
        self.changed.emit()
        QMessageBox.information(
            self, "Securities",
            "%d rows re-keyed, %d description%s recorded, %d merge%s." % (
                report["renamed"], report["named"],
                "" if report["named"] == 1 else "s",
                len(report["merged"]),
                "" if len(report["merged"]) == 1 else "s"))

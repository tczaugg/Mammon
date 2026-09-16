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

Rows the module REFUSED to propose anything for -- an option contract, which is
never merged into its underlying -- are the other half of that sort: they start
unticked and not tickable, sit together at the foot of the list, and show
``Split.reason`` in the Status column instead of a blank. User-reported: "well
over 100 rows ... a bunch of securities like 'MS DJ' and 'MS FH' that it wants
to merge but I have no idea what they are". A row nobody can identify is a row
nobody can safely untick, so the dialog names it and unticks it in advance, and
a count line at the top says how few of the rows want a decision at all.

The Rows column is the honesty check on all of it, and it is load-bearing: it
counts the rows stored under ``Split.old``, which is the exact key
``apply_splits`` would act on, so it says how much this row moves. A row that
says 0 moves nothing, and is therefore never ticked and never tickable --
``securities._settle_unused`` reads the same counts and refuses those proposals
at the source. User-reported: "I'm seeing a row that says 0 description
recorded. Why would you propose something if it affects 0 items?" The usual
cause is a ``securities`` catalog entry that outlived its transactions.

Headless-safe: the confirmation goes through ``QMessageBox.question`` and the
work through an overridable ``_apply`` seam, so tests drive it without opening a
modal that would block forever under the offscreen platform (CLAUDE.md).

:class:`SecurityKindDialog` at the foot of this file is the second screen and
the deliberate opposite of the first: it confirms what each security *is*
(equity, option, fund) and writes nothing but the classification columns. The
two are kept apart because :func:`mammon.securities.apply_splits` re-keys and
deletes, while :func:`mammon.securities.set_kinds` only ever UPDATEs seven
columns -- so saying "this is an option" can never be the click that merges a
contract into its stock.
"""
from __future__ import annotations

import fnmatch

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from mammon import instruments, investments, securities
from mammon.reports import security_audit
from mammon.ui.delegates import NoWheelComboBox

# Column order. Identity and Description are the only editable cells; the rest
# describe what the choice will do.
INCLUDE, STORED, IDENTITY, DESCRIPTION, ROWS, HELD, STATUS = range(7)
HEADERS = ["", "Stored as", "Identity", "Description", "Rows", "Held",
           "What happens"]

# How many held periods a cell spells out before it starts counting them; the
# full list is always in the tooltip.
HELD_SHOWN = 3

# Column order of the kind-review table (below).
K_INCLUDE, K_SYMBOL, K_CURRENT, K_KIND, K_TERMS, K_NOTE = range(6)
K_HEADERS = ["", "Security", "Recorded as", "Is a", "Contract terms", "Notes"]

# The combo entry that means NULL. Shown first because NULL is the state every
# row starts in and is a legitimate answer, not a failure to answer.
UNCLASSIFIED = "(not classified)"


class SecuritiesDialog(QDialog):
    """Review and apply the ticker/description split across the whole file."""

    changed = pyqtSignal()

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Securities")
        self.resize(900, 560)

        lay = QVBoxLayout(self)
        # Both routes to "keep the stored name" are spelled out, and the
        # checkbox one first, because the earlier wording ("clear the Identity
        # cell back to the stored name") read as an instruction to TYPE the
        # stored name in. chosen() implements exactly these two: an unticked
        # row is skipped entirely, an empty Identity cell falls back to `old`.
        self.blurb = QLabel(
            "Nothing changes unless a row is ticked: clear a row's checkbox to "
            "keep the stored name as the identity and leave that security "
            "untouched. On a ticked row, IDENTITY is the ticker that keys "
            "prices and quotes and DESCRIPTION is the name you read; both are "
            "editable. A security with no public ticker -- a plan's own fund "
            "-- keeps the stored name if you leave the Identity cell empty, "
            "and a ticked row still records its Description edit.")
        self.blurb.setWordWrap(True)
        lay.addWidget(self.blurb)

        # How many rows are actually asking for a decision. User-reported: "this
        # is overwhelming, probably because there are well over 100 rows" -- most
        # of which propose nothing. One line at the top says how few matter.
        self.tally = QLabel()
        self.tally.setWordWrap(True)
        lay.addWidget(self.tally)

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
        # decisions that matter should not be hunted for among 20 no-ops. Rows
        # that suggest() REFUSED to propose anything for (an option contract,
        # which must never merge into its underlying) sort last as one block:
        # interleaved among real proposals they read as changes the user has to
        # understand before ticking, and there were dozens of them.
        def rank(s):
            if s.refused:
                return (3, s.old.upper(), s.symbol.upper())
            if s.symbol in groups:
                return (0, s.symbol.upper(), s.old.upper())
            return (1 if s.changes_key else 2, s.symbol.upper(), s.old.upper())
        self._splits.sort(key=rank)

        self.table.setRowCount(len(self._splits))
        for row, s in enumerate(self._splits):
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            # A no-op needs no confirmation; a merge is the repair this exists
            # for, so it starts ticked like any other change. A refused row is
            # never actionable even if it somehow carried a description: there
            # is nothing to apply, and a ticked box would imply there is. The
            # decision itself lives on the Split (securities.Split.actionable),
            # so a row that differs only in letter case reads as unchanged here,
            # in the tally and in the sort at once.
            actionable = s.actionable
            tick.setCheckState(Qt.Checked if actionable else Qt.Unchecked)
            if not actionable:
                tick.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, INCLUDE, tick)

            stored = QTableWidgetItem(s.old)
            stored.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, STORED, stored)
            self.table.setItem(row, IDENTITY, QTableWidgetItem(s.symbol))
            self.table.setItem(row, DESCRIPTION, QTableWidgetItem(s.name or ""))

            # Counted under s.old -- the spelling the rows are actually STORED
            # under, and the same key apply_splits/_rekey act on, so this number
            # is what would move. securities._settle_unused reads the identical
            # dict, which is what guarantees a row showing 0 is never ticked:
            # the count and the proposal cannot disagree.
            n = QTableWidgetItem(f"{self._counts.get(s.old, 0):,}")
            n.setFlags(Qt.ItemIsEnabled)
            n.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, ROWS, n)

            # WHEN this security was actually held, which is what makes a
            # proposed rename checkable by eye: a succession reads as one range
            # ending before the other begins. The same ranges drive the overlap
            # refusal in securities._settle_overlaps, so the evidence for a
            # refused row is on the row.
            ranges = investments.held_ranges(self.conn, s.old)
            held = QTableWidgetItem(
                investments.format_held_ranges(ranges, HELD_SHOWN))
            held.setFlags(Qt.ItemIsEnabled)
            held.setToolTip(investments.format_held_ranges(ranges))
            self.table.setItem(row, HELD, held)

            status = QTableWidgetItem(self._status_text(s, groups))
            status.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, STATUS, status)

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(
            DESCRIPTION, QHeaderView.Stretch)
        self._loading = False
        self._refresh_tally()
        self._refresh_summary()

    def _refresh_tally(self):
        """The fixed shape of the list, counted once: what the dialog FOUND.

        Distinct from `summary`, which counts what is currently TICKED and moves
        as the user works. Both are wanted -- the complaint was not knowing how
        much of a 100-row list needed reading at all."""
        total = len(self._splits)
        proposed = sum(1 for s in self._splits if s.actionable)
        # An unused symbol is refused too, but it is not an option contract --
        # counting it as one would put a false explanation on screen.
        unused = sum(1 for s in self._splits if s.unused)
        options = sum(1 for s in self._splits if s.refused and not s.unused)
        text = ("%d securit%s: %d proposed change%s, %d left alone"
                % (total, "y" if total == 1 else "ies",
                   proposed, "" if proposed == 1 else "s", total - proposed))
        notes = []
        if options:
            notes.append("%d option contract%s, never merged into the "
                         "underlying" % (options, "" if options == 1 else "s"))
        if unused:
            notes.append("%d used by no transaction, holding or price"
                         % unused)
        if notes:
            text += " (of those, %s)" % "; ".join(notes)
        self.tally.setText(text)

    @staticmethod
    def _status_text(split, groups) -> str:
        # Why nothing is proposed, in suggest()'s own words -- "MS DJ" is
        # unreadable otherwise, and the user has to know what a row IS before
        # they can agree to leave it alone.
        if split.refused:
            return split.reason
        if split.symbol in groups:
            others = [o for o in groups[split.symbol] if o != split.old]
            text = ("merges with " + ", ".join(others)) if others else "merge"
            # A case twin is the one merge that looks like a no-op in the table
            # ("vgt" -> "VGT"), so it says why it is there.
            return f"{text} -- {split.reason}" if split.reason else text
        if split.reason:
            return split.reason
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
            split = self._splits[row]
            old = split.old
            ident = (self.table.item(row, IDENTITY).text() or "").strip() or old
            desc = (self.table.item(row, DESCRIPTION).text() or "").strip() or None
            # case_merge is carried over: without it the rebuilt Split says
            # "case only, therefore no change" and the twin merge the user
            # ticked would silently apply nothing.
            out.append(securities.Split(old, ident, desc,
                                        case_merge=split.case_merge))
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


class SecurityKindDialog(QDialog):
    """Confirm what each security IS. Writes kinds and option terms, nothing else.

    The screen over :func:`mammon.reports.security_audit.audit`. The report
    proposes; a person confirms; :func:`mammon.securities.set_kinds` writes --
    and it can write only ``kind``, ``kind_source`` and the option terms, so
    there is no path from this dialog to a symbol, a ticker, a holding, a lot or
    a transaction. Re-running it with "(not classified)" puts any row back,
    which is what lets the whole screen be used without ceremony.

    Two ways in, because 900 securities and 40 years do not get reviewed one row
    at a time: a PATTERN ticks everything matching a symbol fragment or a
    proposed kind ("option" ticks every contract), and any single row can then be
    overridden in its own combo -- bulk by rule, exception by hand.

    Rows the report calls SUSPECT (a fused row, or a contract filed under its
    underlying's ticker) are never ticked by default and are named in the
    confirmation. The audit deliberately proposes NO kind change for a fused
    row: one row that is two instruments is not fixed by relabelling it, and
    un-fusing is a separate operation that does not exist yet.
    """

    changed = pyqtSignal()

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.setWindowTitle("Security types")
        self.resize(980, 600)
        self._loading = True

        lay = QVBoxLayout(self)
        self.blurb = QLabel(
            "What KIND of instrument each security is. Nothing here changes a "
            "security's identity: no symbol, ticker, holding, lot or "
            "transaction is touched, and setting a row back to "
            f"\"{UNCLASSIFIED}\" undoes the classification. Tick the proposals "
            "you agree with, correct any row in its own drop-down, then Apply.")
        self.blurb.setWordWrap(True)
        lay.addWidget(self.blurb)

        bar = QHBoxLayout()
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText(
            "Tick by pattern: a kind (option, equity) or a symbol (XYZ*)")
        self.pattern.returnPressed.connect(self.tick_matching)
        self.btn_match = QPushButton("Tick matching")
        self.btn_match.clicked.connect(self.tick_matching)
        bar.addWidget(self.pattern, 1)
        bar.addWidget(self.btn_match)
        lay.addLayout(bar)

        self.table = QTableWidget(0, len(K_HEADERS))
        self.table.setHorizontalHeaderLabels(K_HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self.table)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        picks = QHBoxLayout()
        self.btn_all = QPushButton("Tick every proposed change")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none = QPushButton("Tick none")
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        picks.addWidget(self.btn_all)
        picks.addWidget(self.btn_none)
        picks.addStretch()
        lay.addLayout(picks)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.btn_apply = buttons.addButton("Apply", QDialogButtonBox.ApplyRole)
        self.btn_apply.clicked.connect(self.on_apply)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.reload()

    # ---- display ---------------------------------------------------------
    def reload(self):
        self._loading = True
        self.audit = security_audit.audit(self.conn)
        # Changes first, suspect rows with them, settled rows last: the point of
        # the screen is the decisions, not the 400 rows that need none.
        self._rows = sorted(
            self.audit.rows,
            key=lambda r: (0 if (r.changes or r.suspect) else 1,
                           r.symbol.upper()))
        self._combos = []

        self.table.setRowCount(len(self._rows))
        for row, r in enumerate(self._rows):
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            # A suspect row is a question, not a proposal, so it starts blank
            # however confident the classifier sounds.
            tick.setCheckState(
                Qt.Checked if (r.changes and not r.suspect) else Qt.Unchecked)
            self.table.setItem(row, K_INCLUDE, tick)

            for col, text in ((K_SYMBOL, r.symbol),
                              (K_CURRENT, self._kind_label(r.kind)),
                              (K_TERMS, self._terms_text(r.proposal)),
                              (K_NOTE, self._note_text(r))):
                cell = QTableWidgetItem(text)
                cell.setFlags(Qt.ItemIsEnabled)
                self.table.setItem(row, col, cell)

            combo = self._kind_combo(r.proposal.kind)
            combo.currentIndexChanged.connect(
                lambda _i, n=row: self._on_kind_changed(n))
            self._combos.append(combo)
            self.table.setCellWidget(row, K_KIND, combo)

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(
            K_NOTE, QHeaderView.Stretch)
        self._loading = False
        self._refresh_summary()

    @staticmethod
    def _kind_label(kind) -> str:
        return (kind or "").replace("_", " ") or UNCLASSIFIED

    @staticmethod
    def _kind_combo(selected) -> NoWheelComboBox:
        """The 'Is a' cell editor. It is a :class:`NoWheelComboBox` because it
        lives INSIDE a table cell: a plain combo eats the wheel and silently
        reclassifies whatever security happens to be under the pointer while
        the user is only trying to scroll the table (USER UX BUG 2026-09-14).
        Ignoring the collapsed wheel event lets the table scroll instead; an
        OPEN dropdown is a separate widget and still scrolls normally."""
        combo = NoWheelComboBox()
        combo.addItem(UNCLASSIFIED, None)
        for k in instruments.Kind:
            combo.addItem(k.value.replace("_", " "), k.value)
        index = combo.findData(selected or None)
        combo.setCurrentIndex(index if index >= 0 else 0)
        return combo

    @staticmethod
    def _terms_text(proposal) -> str:
        """The contract in one line, with whatever the source could not say
        NAMED rather than blank -- a missing strike must look missing."""
        if not proposal.is_option:
            return ""
        bits = [proposal.underlying or "?",
                proposal.expiration or "?",
                proposal.strike or "?",
                proposal.option_right or "?"]
        if proposal.multiplier:
            bits.append("x" + proposal.multiplier)
        text = " ".join(bits)
        if proposal.unknown:
            text += "   (not stated: %s)" % ", ".join(proposal.unknown)
        return text

    @staticmethod
    def _note_text(row) -> str:
        if row.note:
            return row.note
        if not row.changes:
            return "already recorded" if row.classified else "leave unclassified"
        return "proposed"

    def _refresh_summary(self):
        chosen = self.chosen()
        counts = self.audit.counts
        bits = ["%d of %d selected" % (len(chosen), len(self._rows)),
                "%d unclassified" % counts["unclassified"]]
        if counts["proposed_options"]:
            bits.append("%d option%s" % (
                counts["proposed_options"],
                "" if counts["proposed_options"] == 1 else "s"))
        suspect = len(self.audit.suspect_rows)
        if suspect:
            bits.append("%d need%s a look" % (suspect,
                                              "s" if suspect == 1 else ""))
        self.summary.setText("   ".join(bits))

    def _on_item_changed(self, _item):
        if self._loading:
            return
        self._refresh_summary()

    def _on_kind_changed(self, row: int):
        """An edited row is a decision, so ticking it is not a second chore."""
        if self._loading:
            return
        tick = self.table.item(row, K_INCLUDE)
        if tick is not None:
            tick.setCheckState(Qt.Checked)
        self._refresh_summary()

    def _set_all(self, on: bool):
        self._loading = True
        for row, r in enumerate(self._rows):
            item = self.table.item(row, K_INCLUDE)
            if item is None:
                continue
            want = on and self._chosen_kind(row) != (r.kind or None) \
                and not r.suspect
            item.setCheckState(Qt.Checked if want else Qt.Unchecked)
        self._loading = False
        self._refresh_summary()

    # ---- selection -------------------------------------------------------
    def _chosen_kind(self, row: int):
        combo = self._combos[row]
        return combo.itemData(combo.currentIndex())

    def _matches(self, row: int, pattern: str) -> bool:
        """Pattern against the SYMBOL or the proposed kind, not the description.

        A description is prose and a pattern run over it ticks rows nobody
        meant; the two fields a person actually reasons in bulk about are what
        the thing is called and what it would become.
        """
        p = pattern.strip().upper()
        if not p:
            return False
        r = self._rows[row]
        targets = [r.symbol.upper(), (self._chosen_kind(row) or "").upper()]
        if any(ch in p for ch in "*?["):
            return any(fnmatch.fnmatchcase(t, p) for t in targets)
        return any(p in t for t in targets)

    def tick_matching(self):
        """Bulk confirmation: tick every row the pattern names. Additive -- it
        never unticks, so two patterns in a row build one selection."""
        pattern = self.pattern.text()
        self._loading = True
        hits = 0
        for row in range(self.table.rowCount()):
            item = self.table.item(row, K_INCLUDE)
            if item is None or not self._matches(row, pattern):
                continue
            item.setCheckState(Qt.Checked)
            hits += 1
        self._loading = False
        self._refresh_summary()
        return hits

    def chosen(self) -> list:
        """The ticked rows as :class:`~mammon.securities.KindUpdate`.

        Terms ride along only when the confirmed kind is still the contract the
        report parsed. Override a row to "equity" and its strike and expiry do
        not follow it -- a term that outlives the kind it described is how a
        stock ends up with an expiration date.
        """
        out = []
        for row, r in enumerate(self._rows):
            tick = self.table.item(row, K_INCLUDE)
            if tick is None or tick.checkState() != Qt.Checked:
                continue
            kind = self._chosen_kind(row)
            terms = {}
            proposal = r.proposal
            if kind and kind == proposal.kind and proposal.is_option:
                terms = {"multiplier": proposal.multiplier,
                         "underlying": proposal.underlying,
                         "expiration": proposal.expiration,
                         "strike": proposal.strike,
                         "option_right": proposal.option_right}
            out.append(securities.KindUpdate(
                symbol=r.symbol, kind=kind, kind_source="user", **terms))
        return out

    # ---- applying --------------------------------------------------------
    def _apply(self, updates):
        """Seam: the write. Overridden by headless tests."""
        return securities.set_kinds(self.conn, updates)

    def on_apply(self):
        updates = self.chosen()
        if not updates:
            QMessageBox.information(self, "Security types",
                                    "Nothing is selected.")
            return
        lines = ["Record the kind of %d securit%s?" % (
            len(updates), "y" if len(updates) == 1 else "ies")]
        clearing = [u.symbol for u in updates if not u.kind]
        suspect = [r.symbol for r in self._rows
                   if r.suspect and r.symbol in {u.symbol for u in updates}]
        if clearing:
            lines += ["", "Back to unclassified: " + ", ".join(sorted(clearing))]
        if suspect:
            # Named, not counted: these are the rows where the securities table
            # already disagrees with itself.
            lines += ["", "These rows are flagged for review:"]
            lines += ["  " + s for s in sorted(suspect)]
        lines += ["", "No symbol, ticker, holding or transaction is changed."]
        if QMessageBox.question(
                self, "Security types", chr(10).join(lines),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        written = self._apply(updates)
        self.reload()
        self.changed.emit()
        QMessageBox.information(
            self, "Security types",
            "%s securit%s classified." % (written,
                                          "y" if written == 1 else "ies"))
